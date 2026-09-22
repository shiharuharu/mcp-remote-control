"""pypsrp session adapter implementing the WinRM file-client Protocol.

``WinRMTransport.open_fs`` builds this client from a connected pypsrp session
(``execute_ps``, optional ``copy``/``fetch``). ``WinrmFs`` consumes the
Protocol surface; this module is re-exported from ``fs.backends.winrm`` so
existing ``from ...fs.backends.winrm import PypsrpFileClient`` imports keep
working.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

# ``(module, qualname)`` pairs identifying a WinRM link failure. Matched across
# the exception MRO so subclasses count (requests ReadTimeout / ConnectTimeout
# are requests Timeout). Duck-typed names rather than imports keep this
# module's session surface free of requests / pypsrp coupling.
#
# ``WSManFaultError`` is deliberately absent: a SOAP fault is an answer from a
# reachable server, so it must not mark the session dead - the same rule that
# keeps remote ``FsError`` results out of this predicate.
_LINK_FAILURE_TYPES: frozenset[tuple[str, str]] = frozenset(
    {
        ("requests.exceptions", "ConnectionError"),
        ("requests.exceptions", "ProtocolError"),
        ("urllib3.exceptions", "ProtocolError"),
        ("urllib3.exceptions", "ReadTimeoutError"),
        ("requests.exceptions", "Timeout"),
        ("pypsrp.exceptions", "WinRMTransportError"),
        ("builtins", "ConnectionError"),
        ("builtins", "ConnectionResetError"),
        ("builtins", "BrokenPipeError"),
    }
)


def _is_link_failure(exc: BaseException) -> bool:
    """True when *exc* is a transport-level link failure, not a remote result.

    Only link failures are worth reporting back to the transport; a remote
    failure (``FsError`` from a failed script, NOT_FOUND, ...) is an answer from
    a live link and must not mark it dead.
    """
    for cls in type(exc).__mro__:
        if (cls.__module__, cls.__qualname__) in _LINK_FAILURE_TYPES:
            return True
    return False


# ``(module, qualname)`` pairs identifying a transport-level HTTP rejection:
# the WSMan exchange failed at the HTTP layer (pypsrp raises this from
# ``raise_for_status``), so the response body belongs to the server or an
# intermediary - it is not an answer from the remote filesystem. Classifying
# that body by substring would rewrite e.g. a gateway 404 page into "path not
# found: <path>" against a path that was never examined.
#
# ``WSManFaultError`` is deliberately absent: a *parsed* WSMan fault is the
# remote server's own answer (pypsrp converts a parseable fault before this
# class escapes), so its text keeps the filesystem classification.
_HTTP_REJECTION_TYPES: frozenset[tuple[str, str]] = frozenset(
    {("pypsrp.exceptions", "WinRMTransportError")}
)


def _is_http_rejection(exc: BaseException) -> bool:
    """True when *exc* is an HTTP-level rejection of the WSMan exchange.

    Matched across the exception MRO so subclasses count; duck-typed by
    ``(module, qualname)`` like :func:`_is_link_failure` so this module keeps
    no pypsrp import.
    """
    for cls in type(exc).__mro__:
        if (cls.__module__, cls.__qualname__) in _HTTP_REJECTION_TYPES:
            return True
    return False


def _serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run *method* inside the client's serial-ops scope (see ``_serial_scope``)."""

    @functools.wraps(method)
    def wrapper(self: PypsrpFileClient, *args: Any, **kwargs: Any) -> Any:
        with self._serial_scope():
            return method(self, *args, **kwargs)

    return wrapper


def _op_timeout_error(op_timeout_s: float) -> FsError:
    """Whole-op budget miss, in the same shape as the per-call funnel.

    Raised both when a session call would start with no budget left and when
    the wait for the transport serial lock cannot fit the remaining budget.
    """
    return FsError(
        "TIMEOUT",
        f"winrm fs operation timed out after {op_timeout_s}s",
        details={"timeout_s": op_timeout_s, "op_timeout_s": op_timeout_s},
    )


def _ps_streams_stderr(streams: Any) -> str:
    """Extract stderr text from pypsrp ``PSDataStreams`` (or str/bytes).

    Same shape as transport ``_ps_result_to_exec``: prefer ``streams.error``
    (list of error records); fall back to str/bytes stream bodies.
    """
    if streams is None:
        return ""
    err_list = getattr(streams, "error", None)
    if err_list:
        return "\n".join(str(item) for item in err_list)
    if isinstance(streams, bytes):
        return streams.decode("utf-8", errors="replace")
    if isinstance(streams, str):
        return streams
    return ""


def _promote_backup_path(tmp: str) -> str:
    """Same-directory backup name for the promote's replace primitive.

    ``[IO.File]::Replace`` moves the replaced file's content to this name for
    the duration of the call, so the backup must sit beside the promote's temp
    (same volume) and must differ from it: the promote's failure path restores
    or removes this name while the caller's temp cleanup targets the temp.
    Derived from the temp name, so the two cannot collide.
    """
    parent, sep, name = tmp.rpartition("\\")
    name = (
        name.replace(".mrc-tmp-", ".mrc-bak-", 1)
        if ".mrc-tmp-" in name
        else name + ".mrc-bak"
    )
    return f"{parent}{sep}{name}" if parent else name


def _promote_statements(q_tmp: str, q_dest: str, q_bak: str) -> str:
    """PowerShell body that promotes a fully written *q_tmp* onto *q_dest*.

    An existing destination file is replaced by the runtime's replace primitive
    (``[IO.File]::Replace`` -> Win32 ``ReplaceFile``), which keeps the
    destination's own file identity and parks the prior content at *q_bak*;
    anything else is created by a ``Move-Item`` **without** ``-Force``, so a
    file that appeared at the destination after the probe makes the move fail
    instead of being overwritten, and a directory that appeared there fails the
    promote rather than reporting success for a path it never created.
    ``Move-Item -Force`` is never emitted: its force-overwrite branch deletes
    the destination first and moves the source afterwards, which is two steps.

    ``$parked`` is set immediately before the primitive, so the catch body
    knows whether *this* run may have parked the destination and cannot restore
    a stale backup left by an earlier interrupted promote.

    A stale backup left by an interrupted replace is dropped before the
    primitive runs, because the primitive fails on a backup name that already
    exists. That drop is best-effort: when it cannot run (an open handle, a
    scanner holding the file) the promote still replaces the destination and
    the prior content stays at *q_bak*, a hidden sibling that a successful
    promote does not report. The name derives from the temp name, so only a
    promote from the same process and thread drops it.
    """
    return (
        f"if (Test-Path -LiteralPath {q_dest} -PathType Leaf) {{ "
        f"if (Test-Path -LiteralPath {q_bak}) {{ "
        f"Remove-Item -LiteralPath {q_bak} -Force "
        f"-ErrorAction SilentlyContinue }}; "
        f"$parked = $true; "
        f"[IO.File]::Replace({q_tmp}, {q_dest}, {q_bak}); "
        f"Remove-Item -LiteralPath {q_bak} -Force "
        f"-ErrorAction SilentlyContinue "
        f"}} elseif (Test-Path -LiteralPath {q_dest} -PathType Container) {{ "
        f"throw ('destination is a directory: ' + {q_dest}) "
        f"}} else {{ "
        f"Move-Item -LiteralPath {q_tmp} -Destination {q_dest} "
        f"}}"
    )


def _promote_catch(q_tmp: str, q_dest: str, q_bak: str) -> str:
    """PowerShell catch body that keeps the prior target across a failure.

    The replace primitive can fail after it has moved the destination entry to
    the backup, so the first step puts the backup back when the destination is
    gone. That restore is guarded by ``$parked`` (see
    :func:`_promote_statements`): a failure that never reached the primitive -
    a mid-write failure of the whole-file path, or a refused move - must not
    put the content of a stale backup at a destination this run never touched.
    The restore is best-effort, and a restore that cannot run leaves the prior
    content at the backup name rather than destroying it. A backup is dropped
    only once the destination exists again, the temp is removed last so a failed
    promote leaves no stray sibling, and the original error is rethrown either
    way.
    """
    return (
        f"if ($parked -and (-not (Test-Path -LiteralPath {q_dest})) -and "
        f"(Test-Path -LiteralPath {q_bak})) {{ "
        f"Move-Item -LiteralPath {q_bak} -Destination {q_dest} "
        f"-ErrorAction SilentlyContinue }}; "
        f"if ((Test-Path -LiteralPath {q_dest}) -and "
        f"(Test-Path -LiteralPath {q_bak})) {{ "
        f"Remove-Item -LiteralPath {q_bak} -Force "
        f"-ErrorAction SilentlyContinue }}; "
        f"if (Test-Path -LiteralPath {q_tmp}) {{ "
        f"Remove-Item -LiteralPath {q_tmp} -Force "
        f"-ErrorAction SilentlyContinue }}; "
        f"throw "
    )


class PypsrpFileClient:
    """Adapter: pypsrp-compatible session -> :class:`WinRMFileClient` for ``WinrmFs``.

    Uses ``copy``/``fetch`` for put/get when present and PowerShell oneshots for
    the rest, implementing the stable file Protocol for ``WinrmFs``.

    Parameters
    ----------
    session: pypsrp-compatible session (``execute_ps``, optional ``copy``/``fetch``).
    timeout_s:
        Wall-clock budget for each oneshot ``execute_ps`` and native
        ``copy``/``fetch``, default :data:`DEFAULT_WINRM_FS_TIMEOUT_S`; a hung
        remote call returns ``FsError(TIMEOUT)`` while its executor thread may
        still run until the remote side finishes. A bound whole-op deadline caps
        each call at the remaining op budget.
    serial_ops:
        Zero-argument callable returning a context manager that serializes
        WSMan calls (the transport op lock), shared with exec because both use
        the same pypsrp wsman object; ``None`` runs unsynchronized.
    serial_ops_within:
        Timed counterpart: takes a timeout, raises ``TimeoutError`` if the lock
        is not free in time, and is what the outermost scope of a deadline-bound
        op acquires through so the lock wait spends that budget.
    on_link_failure:
        Called with the exception on a link-class failure (the request never
        reached the remote runspace) so the transport can reconnect or mark the
        session dead. Remote failures never call it; ``None`` disables it.
    """

    def __init__(
        self,
        session: Any,
        *,
        timeout_s: float | None = None,
        serial_ops: Callable[[], Any] | None = None,
        serial_ops_within: Callable[[float], Any] | None = None,
        on_link_failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._session = session
        # ``None`` -> module default (not "no deadline"); mirrors SftpFs.
        self._timeout_s = (
            DEFAULT_WINRM_FS_TIMEOUT_S if timeout_s is None else float(timeout_s)
        )
        # Bound by WinrmFs._op_budget when a public op is in flight.
        self._op_deadline: float | None = None
        self._op_timeout_s: float | None = None
        self._serial_ops = serial_ops
        self._serial_ops_within = serial_ops_within
        self._on_link_failure = on_link_failure
        # Per-thread nesting state: fs ops form a re-entrant call chain
        # (public op -> _execute_ps), so the lock is taken once and a link
        # failure is reported once per outermost call.
        self._scope_state = threading.local()

    @contextlib.contextmanager
    def _serial_scope(self) -> Iterator[None]:
        """Hold the shared serial-ops lock around a session call.

        Nested scopes are safe: ``serial_ops`` returns an RLock and the
        transport op acquisition is re-entrant, so a public op that calls
        ``_execute_ps`` (or another public op) does not deadlock. The wait
        for that lock is spent from the whole-op budget; see
        :meth:`_lock_scope`.

        When a call raises a link-class exception the first nested frame that
        observes it invokes ``on_link_failure`` exactly once and re-raises the
        original exception for the caller to map; a failure raised by the
        callback itself must not replace it.
        """
        state = self._scope_state
        depth = getattr(state, "depth", 0)
        state.depth = depth + 1
        try:
            if self._serial_ops is None:
                yield
            else:
                with self._lock_scope(depth):
                    yield
        except Exception as exc:
            if (
                not getattr(state, "link_reported", False)
                and self._on_link_failure is not None
                and _is_link_failure(exc)
            ):
                state.link_reported = True
                with contextlib.suppress(Exception):
                    self._on_link_failure(exc)
            raise
        finally:
            if depth == 0:
                state.link_reported = False
            state.depth = depth

    @contextlib.contextmanager
    def _lock_scope(self, depth: int) -> Iterator[None]:
        """Enter the serial-ops lock, bounded by the remaining op budget.

        Waiting for a lock a long exec / ps holds is time the fs operation
        cannot spend remotely, and it happens before any session call - so
        the outermost frame acquires through the timed gate within the
        remaining whole-op budget and reports ``FsError(TIMEOUT)`` when the
        lock is not free in time. Nested frames already hold the lock and
        must not wait on themselves; without a bound deadline or a gate the
        wait stays unbounded.
        """
        if (
            depth > 0
            or self._serial_ops_within is None
            or self._op_deadline is None
        ):
            with self._serial_ops():
                yield
            return
        remaining = self._op_deadline - time.monotonic()
        # ExitStack: only the acquire is turned into FsError(TIMEOUT); an
        # exception raised later by the guarded body keeps its own meaning.
        gate = contextlib.ExitStack()
        try:
            gate.enter_context(self._serial_ops_within(remaining))
        except TimeoutError as exc:
            raise _op_timeout_error(self._op_budget_s()) from exc
        with gate:
            yield

    def _op_budget_s(self) -> float:
        """Whole-op budget the caller sees in a TIMEOUT (op ceiling, else fallback)."""
        if self._op_timeout_s is not None:
            return float(self._op_timeout_s)
        return float(self._timeout_s)

    def _run_blocking_with_timeout(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float,
    ) -> Any:
        """Run a blocking session call on the shared bridge with a wall-clock.

        Mirrors :meth:`WinRMTransport._run_blocking_with_timeout`:
        ``asyncio.to_thread`` on :class:`AsyncLoopBridge` so the caller's
        MCP/worker thread is not pinned. On timeout the bridge raises
        ``TimeoutError``; the executor thread keeps running until the remote
        side returns (cannot cancel pypsrp oneshot mid-flight).
        """
        import asyncio

        from mcp_remote_control.transport.async_bridge import get_shared_bridge

        async def _wrap() -> Any:
            return await asyncio.to_thread(fn)

        return get_shared_bridge().run(_wrap(), timeout_s=timeout_s)

    def _call_budget_s(self) -> float:
        """Seconds allowed for the next session call (per-call, capped by the op).

        Returns ``0.0`` when the whole-op deadline is already spent (caller
        raises TIMEOUT without starting a new oneshot). Non-positive
        ``_timeout_s`` means unbounded per-call; whole-op remaining still
        clamps when a deadline is bound.
        """
        per = float(self._timeout_s)
        deadline = self._op_deadline
        if deadline is None:
            return max(0.0, per) if per > 0 else per
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        if per <= 0:
            # Unbounded per-call escape still respects whole-op remaining.
            return remaining
        return min(per, remaining)

    def _invoke_with_budget(self, fn: Callable[[], Any], *, label: str) -> Any:
        """Run a blocking session call within the per-call and remaining-op budget.

        Native ``copy``/``fetch`` share this funnel with ``execute_ps`` so a
        hung pypsrp transfer cannot pin the worker past the whole-op deadline.
        Non-positive ``_timeout_s`` stays unbounded unless an op deadline is
        bound (then remaining clamps). Spent deadline raises TIMEOUT without
        starting the call.
        """
        budget = self._call_budget_s()
        if budget == 0.0 and self._op_deadline is not None:
            # Op deadline already spent across prior RTs in this public call.
            raise _op_timeout_error(self._op_budget_s())
        try:
            if budget is not None and budget > 0:
                return self._run_blocking_with_timeout(fn, timeout_s=budget)
            # Explicit non-positive budget: unbounded (test / lab escape).
            return fn()
        except TimeoutError as exc:
            details: dict[str, Any] = {"timeout_s": budget}
            if self._op_deadline is not None:
                details["op_timeout_s"] = (
                    self._op_timeout_s
                    if self._op_timeout_s is not None
                    else self._timeout_s
                )
            raise FsError(
                "TIMEOUT",
                f"winrm fs {label} timed out after {budget}s",
                details=details,
            ) from exc

    @_serialized
    def _execute_ps(self, script: str) -> str:
        """Run oneshot ``execute_ps``; return stdout or raise on remote errors.

        pypsrp 0.9.1 returns ``(output, streams, had_errors)``. Discarding
        ``had_errors`` / ``streams.error`` caused silent success on failed
        write/delete/read. Mirror transport ``_ps_result_to_exec``: when
        ``had_errors`` is true, raise ``FsError`` with remote stderr.

        Each call is bounded by ``self._timeout_s`` (and, when a public op
        deadline is bound, the remaining whole-op budget) so a hung remote
        oneshot surfaces ``FsError(TIMEOUT)`` instead of parking the worker
        forever or letting multi-RT ops approach N times per-call.
        """
        execute_ps = getattr(self._session, "execute_ps", None)
        if not callable(execute_ps):
            raise FsError("UNSUPPORTED", "winrm session has no execute_ps")

        def _call() -> Any:
            # Protocol-stable call: always pass environment= (None here).
            try:
                return execute_ps(script, environment=None)
            except TypeError:
                # Last-resort for third-party shapes that reject the kwarg.
                return execute_ps(script)

        raw = self._invoke_with_budget(_call, label="execute_ps")

        stdout = ""
        stderr = ""
        had_errors = False

        if isinstance(raw, tuple) and raw:
            # pypsrp: (output, streams, had_errors)
            stdout = str(raw[0] or "")
            if len(raw) >= 3:
                had_errors = bool(raw[2])
            if len(raw) >= 2 and raw[1] is not None:
                stderr = _ps_streams_stderr(raw[1])
        elif isinstance(raw, str):
            stdout = raw
        else:
            # Object-shaped results (stdout / had_errors / streams attrs).
            out_attr = getattr(raw, "stdout", None)
            if out_attr is not None:
                stdout = str(out_attr or "")
            else:
                stdout = str(raw or "")
            had_errors = bool(getattr(raw, "had_errors", False))
            streams = getattr(raw, "streams", None)
            if streams is not None:
                stderr = _ps_streams_stderr(streams)

        if had_errors:
            detail = stderr.strip()
            msg = detail if detail else "remote PowerShell reported errors"
            raise FsError(
                "FS_ERROR",
                msg,
                details={"stderr": stderr} if stderr else None,
            )
        return stdout

    @_serialized
    def _run_json(self, script: str, path: str) -> Any:
        out = self._execute_ps(script).strip()
        # Strip BOM / noise lines; take last JSON object/array.
        if not out:
            raise FsError("FS_ERROR", f"empty ps output for path: {path}", details={"path": path})
        # Prefer last line that looks like JSON.
        candidate = out
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                candidate = line
                break
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            low = out.lower()
            if "not found" in low or "cannot find" in low or "does not exist" in low:
                raise FsError("NOT_FOUND", f"path not found: {path}", details={"path": path}) from exc
            raise FsError(
                "FS_ERROR",
                f"invalid ps json for path: {path}",
                details={"path": path},
            ) from exc

    @_serialized
    def stat(self, path: str) -> dict[str, Any]:
        q = _ps_single_quote(path)
        # ReparsePoint (symlink/junction) -> kind=link so write/put final-component
        # resolve can follow. Target is included when available.
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ $p = Get-Item -LiteralPath {q} -Force; "
            f"$isReparse = [bool]($p.Attributes -band "
            f"[IO.FileAttributes]::ReparsePoint); "
            f"$kind = if ($isReparse) {{ 'link' }} "
            f"elseif ($p.PSIsContainer) {{ 'dir' }} else {{ 'file' }}; "
            f"$size = if ($p.PSIsContainer -and -not $isReparse) {{ 0 }} "
            f"else {{ try {{ [int64]$p.Length }} catch {{ 0 }} }}; "
            f"$mtime = $p.LastWriteTimeUtc.ToString('o'); "
            f"$target = $null; "
            f"if ($isReparse) {{ "
            f"if ($null -ne $p.Target) {{ "
            f"if ($p.Target -is [array]) {{ $target = [string]$p.Target[0] }} "
            f"else {{ $target = [string]$p.Target }} "
            f"}} elseif ($null -ne $p.LinkTarget) {{ "
            f"$target = [string]$p.LinkTarget "
            f"}} "
            f"}}; "
            f"$o = @{{ kind=$kind; size=$size; mtime=$mtime; "
            f"mode=$p.Attributes.ToString() }}; "
            f"if ($null -ne $target -and $target -ne '') {{ $o.target = $target }}; "
            f"$o | ConvertTo-Json -Compress "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        return data if isinstance(data, dict) else {"kind": "file", "size": 0}

    @_serialized
    def readlink(self, path: str) -> str:
        """Return the reparse/symlink target for *path* (final-component resolve).

        Raises ``FileNotFoundError`` when *path* is missing, ``OSError`` when
        *path* exists but is not a reparse point. Prefer ``Get-Item.Target``
        (PS 5.1+ / symlink target); fall back to ``LinkTarget`` (PS 7+).
        """
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ $p = Get-Item -LiteralPath {q} -Force; "
            f"if (-not ($p.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ "
            f"Write-Output '{{\"error\":\"NOT_A_LINK\"}}'; return "
            f"}}; "
            f"$t = $null; "
            f"if ($null -ne $p.Target) {{ "
            f"if ($p.Target -is [array]) {{ $t = [string]$p.Target[0] }} "
            f"else {{ $t = [string]$p.Target }} "
            f"}} "
            f"if ((-not $t) -and ($null -ne $p.LinkTarget)) {{ "
            f"$t = [string]$p.LinkTarget "
            f"}}; "
            f"@{{ target=$t }} | ConvertTo-Json -Compress "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        if isinstance(data, dict) and data.get("error") == "NOT_A_LINK":
            raise OSError(f"not a reparse point: {path}")
        if not isinstance(data, dict):
            raise OSError(f"cannot read link target: {path}")
        target = data.get("target")
        if target is None or str(target).strip() == "":
            raise OSError(f"empty reparse target: {path}")
        return str(target).strip()

    def _resolve_final_link(self, path: str) -> str:
        """Resolve final-component reparse chain for the write_file promote.

        Same policy as :meth:`WinrmFs._resolve_final_link`: follow only the
        final component, cap depth, dangling -> write at current path.

        Stat/readlink ``TIMEOUT`` is raised so ``Move-Item`` cannot replace a
        symlink whose referent could not be resolved. A link-class failure is
        raised for the same reason: the transport has already retired the
        session, so falling back to *path* would promote onto a path whose
        referent was never read and report success on a dead endpoint. Other
        non-NOT_FOUND probe failures still fall back to *path* so minimal
        mocks / remote errors on the probe still reach the temp+promote
        script.

        ``IS_A_DIR`` is raised when the resolved entry is a directory: a move
        onto a directory destination is a container move that puts the temp
        inside that directory instead of creating *path*, so the write would
        report success while leaving a stray temp behind. The promote refuses
        such a destination as well, but the refusal here keeps the op from
        emitting any script at all. A link whose own attributes mark it a
        directory is rejected the same way even when its referent cannot be
        read - its container rule applies to the reparse path.
        """
        current = path
        for _ in range(_MAX_SYMLINK_FOLLOW):
            try:
                attrs = self.stat(current)
            except FileNotFoundError:
                return current
            except FsError as exc:
                if exc.code == "NOT_FOUND":
                    return current
                _raise_if_timeout(exc, current)
                # Probe failed (empty output, had_errors, ...) - keep original
                # path so write_file still attempts the promote.
                return path
            except Exception as exc:  # noqa: BLE001
                _raise_if_timeout(exc, current)
                if _is_link_failure(exc):
                    raise
                return path
            kind = _kind_from_attrs(attrs)
            if kind == "dir":
                raise FsError(
                    "IS_A_DIR",
                    f"is a directory: {current}",
                    details={"path": path},
                )
            if kind != "link":
                return current
            try:
                target = self.readlink(current)
            except Exception as exc:  # noqa: BLE001 - cannot resolve -> original
                _raise_if_timeout(exc, current)
                if _is_link_failure(exc):
                    raise
                _reject_dir_reparse(attrs, current, path)
                return path
            target = str(target).strip()
            # PowerShell Target may be multi-valued; take first token.
            if "\n" in target:
                target = target.splitlines()[0].strip()
            if not target:
                # Nothing to follow: a directory reparse point is still a
                # container destination and is rejected; anything else keeps
                # the promote-onto-*path* fallback.
                _reject_dir_reparse(attrs, current, path)
                return path
            if _is_abs_win(target):
                current = _norm_win_path(target)
            else:
                parent = _parent_win(current)
                current = (
                    _norm_win_path(target)
                    if parent is None
                    else _norm_win_path(_win_sep_join(parent, target))
                )
        raise FsError(
            "FS_ERROR",
            f"too many symbolic links: {path}",
            details={"path": path},
        )

    @_serialized
    def listdir(self, path: str) -> list[str]:
        q = _ps_single_quote(path)
        # Always emit a JSON array: for an empty existing dir the pipeline is
        # empty and ConvertTo-Json would emit nothing, which _run_json treats
        # as FS_ERROR. Emit '[]' explicitly when Count is 0.
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ "
            f"$names = @(Get-ChildItem -LiteralPath {q} -Force | ForEach-Object {{ $_.Name }}); "
            f"if ($names.Count -eq 0) {{ Write-Output '[]' }} "
            f"else {{ $names | ConvertTo-Json -Compress }} "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        if data is None:
            return []
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, str):
            return [data]
        return []

    @_serialized
    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        q = _ps_single_quote(path)
        if max_bytes is not None:
            # Bounded read: open a FileStream and read at most max_bytes so a
            # large file only transfers ~K bytes instead of base64-ing the
            # whole file and truncating locally.
            n = int(max_bytes)
            n = max(n, 0)
            # Stream Read up to max_bytes. Do not cast $fs.Length to Int32 -
            # files larger than 2 GiB would fail before any bytes are returned.
            # FileStream.Read stops at EOF, so Length is unnecessary.
            script = (
                f"$ErrorActionPreference='Stop'; "
                f"$maxN = {n}; "
                f"try {{ "
                f"$fs = [IO.File]::Open({q}, [IO.FileMode]::Open, "
                f"[IO.FileAccess]::Read, [IO.FileShare]::Read); "
                f"try {{ "
                f"$buf = New-Object byte[] $maxN; "
                f"$offset = 0; "
                f"while ($offset -lt $maxN) {{ "
                f"$r = $fs.Read($buf, $offset, $maxN - $offset); "
                f"if ($r -le 0) {{ break }}; "
                f"$offset += $r "
                f"}}; "
                f"if ($offset -lt $maxN) {{ "
                f"$final = New-Object byte[] $offset; "
                f"[Array]::Copy($buf, $final, $offset); "
                f"$buf = $final "
                f"}}; "
                f"[Convert]::ToBase64String($buf) "
                f"}} finally {{ $fs.Close() }} "
                f"}} catch {{ "
                f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
                f"Write-Output 'NOT_FOUND' }} else {{ throw }} }}"
            )
        else:
            script = (
                f"$ErrorActionPreference='Stop'; "
                f"try {{ "
                f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({q})) "
                f"}} catch {{ "
                f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
                f"Write-Output 'NOT_FOUND' }} else {{ throw }} }}"
            )
        out = self._execute_ps(script).strip()
        if out == "NOT_FOUND" or out.endswith("\nNOT_FOUND"):
            raise FileNotFoundError(path)
        # Take last non-empty line as base64 payload.
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            return b""
        b64 = lines[-1]
        if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", b64):
            low = out.lower()
            if "not found" in low or "cannot find" in low:
                raise FileNotFoundError(path)
            raise FsError("FS_ERROR", f"invalid base64 read for: {path}", details={"path": path})
        return base64.b64decode(b64)

    @_serialized
    def list_with_attrs(self, path: str) -> list[dict[str, Any]]:
        """Return name/kind/size/mtime/mode per child in one PowerShell round-trip.

        Lets ``WinrmFs.list`` avoid N per-child stat calls. Empty existing dir
        -> ``[]``. Callers fall back to listdir + per-name stat when absent.
        """
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ "
            f"$items = @(Get-ChildItem -LiteralPath {q} -Force | ForEach-Object {{ "
            f"$kind = if ($_.PSIsContainer) {{ 'dir' }} else {{ 'file' }}; "
            f"$size = if ($_.PSIsContainer) {{ 0 }} else {{ [int64]$_.Length }}; "
            f"@{{ name=$_.Name; kind=$kind; size=$size; "
            f"mtime=$_.LastWriteTimeUtc.ToString('o'); "
            f"mode=$_.Attributes.ToString() }} "
            f"}}); "
            f"if ($items.Count -eq 0) {{ Write-Output '[]' }} "
            f"else {{ $items | ConvertTo-Json -Compress }} "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        if data is None:
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    @_serialized
    def write_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* via a same-dir temp then a promote.

        Payload is written to a sibling ``.name.mrc-tmp-*`` file first; only a
        successful full write is promoted onto *path*. A mid-write failure
        never leaves a truncated success-path target: the catch block removes
        the temp and rethrows so ``had_errors`` / ``FsError`` surface.

        The promote branches on what the host sees at *path* (see
        :func:`_promote_statements`): an existing file is replaced by the
        runtime's replace primitive with a same-directory backup name, a
        directory is refused, and anything else is created by an unforced move.
        The runtime's replace can fail after it has moved the destination entry
        to the backup, so the catch restores the prior content from the backup
        (see :func:`_promote_catch`) instead of leaving a destination that a
        failed promote simply deleted. A backup that the success path cannot
        drop (an open handle, a scanner holding the file) stays at the hidden
        sibling name instead of being reported.

        When *path* is a reparse/symlink (final component), the chain is
        resolved first so the promote updates the referent and does not replace
        the reparse directory entry with a regular file (same policy as
        SFTP/local ``_resolve_final_link``).
        """
        dest = self._resolve_final_link(path)
        tmp = _win_temp_path(dest)
        q_tmp = _ps_single_quote(tmp)
        q_dest = _ps_single_quote(dest)
        q_bak = _ps_single_quote(_promote_backup_path(tmp))
        # Embed base64 in a single-quoted PS string (no quotes inside b64).
        b64 = base64.b64encode(data).decode("ascii")
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"$bytes = [Convert]::FromBase64String('{b64}'); "
            f"$parked = $false; "
            f"try {{ "
            f"[IO.File]::WriteAllBytes({q_tmp}, $bytes); "
            f"{_promote_statements(q_tmp, q_dest, q_bak)} "
            f"}} catch {{ "
            f"{_promote_catch(q_tmp, q_dest, q_bak)}"
            f"}}"
        )
        self._execute_ps(script)

    @_serialized
    def rename(self, src: str, dest: str) -> None:
        """Promote *src* onto *dest* with a remote replace/move in one round trip.

        The native put uploads to a sibling ``.mrc-tmp-*`` file and verifies its
        size before promoting, so both paths already live on the same volume:
        the promote renames the temp into place and no content leaves the remote
        host (``read_file`` + ``write_file`` would move the whole payload off the
        host and back).

        An existing destination file is replaced by the runtime's replace
        primitive (``[IO.File]::Replace`` -> Win32 ``ReplaceFile``), which keeps
        the destination's identity and parks the prior content at a
        same-directory backup name; a directory destination is refused, and
        anything else is created by a ``Move-Item`` without ``-Force``. A
        primitive failure can leave the destination entry gone, so the catch
        restores the prior content from the backup (see :func:`_promote_catch`),
        removes the temp, and rethrows: a failed promote neither destroys the
        prior target nor leaves a stray temp behind. A backup that the success
        path cannot drop stays at the hidden sibling name.

        When *dest* is a reparse/symlink (final component), the chain is
        resolved first so the rename updates the referent and does not replace
        the reparse directory entry with a regular file (same policy as
        :meth:`write_file`).
        """
        target = self._resolve_final_link(dest)
        q_src = _ps_single_quote(src)
        q_dest = _ps_single_quote(target)
        q_bak = _ps_single_quote(_promote_backup_path(src))
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"$parked = $false; "
            f"try {{ "
            f"{_promote_statements(q_src, q_dest, q_bak)} "
            f"}} catch {{ "
            f"{_promote_catch(q_src, q_dest, q_bak)}"
            f"}}"
        )
        self._execute_ps(script)

    @_serialized
    def mkdir(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"New-Item -ItemType Directory -Path {q} -Force | Out-Null"
        )
        self._execute_ps(script)

    @_serialized
    def remove(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Force"
        )
        self._execute_ps(script)

    @_serialized
    def rmdir(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Force"
        )
        self._execute_ps(script)

    @_serialized
    def rmtree(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Recurse -Force"
        )
        self._execute_ps(script)

    @_serialized
    def copy(self, local: str, remote: str) -> None:
        fn = getattr(self._session, "copy", None)
        if not callable(fn):
            data = Path(local).read_bytes()
            self.write_file(remote, data)
            return
        # Native pypsrp copy has no per-call timeout of its own; share the
        # execute_ps wall-clock so a hung transfer cannot block forever.
        self._invoke_with_budget(lambda: fn(local, remote), label="copy")

    @_serialized
    def fetch(self, remote: str, local: str) -> None:
        fn = getattr(self._session, "fetch", None)
        if not callable(fn):
            data = self.read_file(remote)
            Path(local).write_bytes(data)
            return
        self._invoke_with_budget(lambda: fn(remote, local), label="fetch")

    @property
    def has_native_copy(self) -> bool:
        """True only when the wrapped session provides a real ``copy`` callable.

        The PS ``write_file`` fallback is NOT native - running it under
        ``ps_script_fs=false`` would execute the gated business scripts, so
        put/get's native exemption must not apply when only the fallback exists.
        """
        return callable(getattr(self._session, "copy", None))

    @property
    def has_native_fetch(self) -> bool:
        """True only when the wrapped session provides a real ``fetch`` callable."""
        return callable(getattr(self._session, "fetch", None))


# FsError and WinRM path helpers live on the fs backend. Import after the
# class so fs.backends.winrm can finish defining WinrmFs and re-export us.
from mcp_remote_control.fs.backends.winrm import (  # noqa: E402
    DEFAULT_WINRM_FS_TIMEOUT_S,
    _MAX_SYMLINK_FOLLOW,
    _is_abs_win,
    _kind_from_attrs,
    _norm_win_path,
    _parent_win,
    _ps_single_quote,
    _raise_if_timeout,
    _reject_dir_reparse,
    _win_sep_join,
    _win_temp_path,
)
from mcp_remote_control.fs.types import FsError  # noqa: E402
