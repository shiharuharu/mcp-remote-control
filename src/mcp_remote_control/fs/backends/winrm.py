"""WinRM filesystem backend over the ``WinRMFileClient`` Protocol.

Agent-facing API remains ``fs_*``; ``via=winrm`` is optional meta only.
Recursive list and rmtree fallback guard against junction/reparse cycles
(visited set + depth cap); depth exceed raises ``FsError(DEPTH_EXCEEDED)``
(aligned with SFTP). Reads always request bounded transfer via
``read_file(..., max_bytes=)`` on the Protocol surface.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mcp_remote_control.fs.atomic import mrc_tmp_name
from mcp_remote_control.fs.backends.local import (
    _copy_mode_if_exists as _copy_local_mode_if_exists,
)
from mcp_remote_control.fs.backends.local import (
    _resolve_final_link as _resolve_local_final_link,
)
from mcp_remote_control.fs.types import (
    DEFAULT_READ_MAX_BYTES,
    DEFAULT_TRANSFER_CHUNK,
    FsError,
    ListEntry,
    ListResult,
    ProgressCallback,
    ReadResult,
    StatInfo,
    TransferResult,
    WriteResult,
    detect_text,
    report_progress,
)
from mcp_remote_control.transport.protocols import (
    SupportsFileOpen,
    SupportsListWithAttrs,
    SupportsRmtree,
    WinRMFileClient,
)

# Default wall-clock budget for each oneshot ``execute_ps`` driven by
# :class:`PypsrpFileClient`. Aligns with :data:`DEFAULT_SFTP_TIMEOUT_S` (60s)
# and ``[defaults] exec_timeout_ms`` so MCP/worker threads never block forever
# on a hung remote PowerShell oneshot. Override per client via
# ``PypsrpFileClient(..., timeout_s=...)`` or ``WinrmFs(..., timeout_s=...)``.
DEFAULT_WINRM_FS_TIMEOUT_S: float = 60.0

# Default whole-public-op wall-clock budget (shared remaining across RTs).
# Without this, multi-RT ops (recursive list / rmtree fallback / chunked put /
# mkdir_p) can approach N times DEFAULT_WINRM_FS_TIMEOUT_S. Override via
# ``WinrmFs(..., op_timeout_s=...)``; omit to mirror the resolved per-call
# ``timeout_s`` (or this default when both are omitted).
DEFAULT_WINRM_FS_OP_TIMEOUT_S: float = DEFAULT_WINRM_FS_TIMEOUT_S

# Bounded budget (seconds) for the error-path temp cleanup that follows a
# failed put. The whole-op deadline is already spent exactly when cleanup
# matters most, so cleanup spends this budget of its own instead of consulting
# it. Two invariants pin the value: a stalled delete may not pin the caller
# (cleanup is bounded, never an unbounded teardown), and cleanup is added
# wall-clock on top of a spent whole-op budget, so it must stay well inside
# the grace a failed put is allowed past that budget - a hang is reported as
# TIMEOUT, not as an unbounded stall.
DEFAULT_WINRM_FS_CLEANUP_TIMEOUT_S: float = 1.0

# Floor under the cleanup budget: a budget of zero would issue no remote call
# at all, which is the leak this cleanup exists to prevent.
_CLEANUP_MIN_BUDGET_S: float = 0.05

# An abandoned transfer thread can land the temp AFTER the put already
# reported TIMEOUT, so the error-path cleanup retries the remove for as long
# as its own budget lasts (never past it) to sweep that late arrival. The
# budget, not an attempt count, is what bounds the retries: a landing inside
# the budget is erased, a later one is out of scope (waiting for the
# abandoned thread would be unbounded).
_CLEANUP_SWEEP_GAP_S = 0.1

# Recursive-list / rmtree depth backstop (same policy as SFTP). Enough for any
# realistic tree; pathological junction cycles raise DEPTH_EXCEEDED instead of
# returning a silent incomplete listing.
_MAX_RECURSE_DEPTH = 40

# Cap final-component reparse/symlink follow for atomic write/put (loop guard).
# Aligns with SFTP ``_MAX_SYMLINK_FOLLOW`` so cycles raise a clear FsError
# instead of hanging or replacing the link entry with a regular file.
_MAX_SYMLINK_FOLLOW = 32

# Factory: () -> file client (sync object).
WinrmFsFactory = Callable[[], WinRMFileClient | Any]


def _coerce_bytes(data: object) -> bytes:
    """Normalize duck-typed client read results to ``bytes``."""
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    return bytes(cast(Any, data))


def _coerce_str_list(raw: object) -> list[str]:
    """Normalize duck-typed client listdir results to ``list[str]``."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return [str(x) for x in cast(Any, raw)]


def _mtime_iso(ts: float | str | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        text = ts.strip()
        if not text:
            return None
        # Already ISO-ish from PowerShell.
        if "T" in text or text.endswith("Z"):
            return text.replace("+00:00", "Z")
        try:
            ts = float(text)
        except ValueError:
            return text
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _is_abs_win(path: str) -> bool:
    text = path.strip()
    if not text:
        return False
    # Drive-absolute: C:\... or C:/...
    if len(text) >= 2 and text[1] == ":":
        return True
    # UNC
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return False


def _win_sep_join(base: str, name: str) -> str:
    base = base.rstrip("\\/")
    name = name.lstrip("\\/")
    if not base:
        return name
    return base + "\\" + name


def _norm_win_path(path: str) -> str:
    """Normalize Windows path separators; keep drive/UNC shape."""
    text = str(path).strip()
    if not text:
        return text
    # Preserve UNC prefix.
    unc = text.startswith("\\\\") or text.startswith("//")
    text = text.replace("/", "\\")
    while "\\\\" in text[2:] if unc else "\\\\" in text:
        if unc:
            text = "\\\\" + text[2:].replace("\\\\", "\\")
        else:
            text = text.replace("\\\\", "\\")
    # Strip trailing slash except roots: C:\ or \\server\share
    if len(text) > 3 and text.endswith("\\"):
        # C:\ stays; C:\foo\ -> C:\foo
        if not (len(text) == 3 and text[1] == ":"):
            text = text.rstrip("\\")
    # Drive root without slash: C: -> C:\
    if len(text) == 2 and text[1] == ":":
        text = text + "\\"
    return text


def _entry_kind(kind: str) -> str:
    return {"dir": "d", "file": "f", "link": "l"}.get(kind, "o")


def _kind_from_attrs(attrs: Any) -> str:
    if attrs is None:
        return "file"
    kind: str | None = None
    if isinstance(attrs, dict):
        k = attrs.get("kind") or attrs.get("type") or attrs.get("Type")
        if k is not None:
            kind = _normalize_kind(str(k))
    if kind is None:
        k = getattr(attrs, "kind", None)
        if k is None:
            k = getattr(attrs, "type", None)
        if k is not None:
            kind = _normalize_kind(str(k))
    if kind is None:
        # POSIX-ish mode bits if present
        mode = getattr(attrs, "mode", None)
        if mode is None:
            mode = getattr(attrs, "st_mode", None)
        if mode is not None and isinstance(mode, int):
            import stat as statmod

            if statmod.S_ISDIR(mode):
                kind = "dir"
            elif statmod.S_ISLNK(mode):
                kind = "link"
            elif statmod.S_ISREG(mode):
                kind = "file"
    if kind is None:
        if getattr(attrs, "is_dir", None) is True or getattr(attrs, "isdir", None) is True:
            kind = "dir"
        else:
            kind = "file"
    # Windows reparse points often surface as file/dir + ReparsePoint in
    # Attributes.ToString() when the remote does not set kind=link. Promote so
    # final-component resolve can follow the referent.
    if kind != "link":
        mode_str = _mode_of(attrs)
        if mode_str and "reparsepoint" in mode_str.lower():
            return "link"
    return kind


def _normalize_kind(raw: str) -> str:
    low = raw.strip().lower()
    if low in {"d", "dir", "directory", "container"}:
        return "dir"
    if low in {"f", "file", "reg", "regular"}:
        return "file"
    if low in {"l", "link", "symlink", "junction"}:
        return "link"
    return "other"


def _size_of(attrs: Any) -> int:
    if isinstance(attrs, dict):
        size = attrs.get("size", attrs.get("Length", 0))
        return int(size) if size is not None else 0
    size = getattr(attrs, "size", None)
    if size is None:
        size = getattr(attrs, "st_size", None)
    if size is None:
        size = getattr(attrs, "Length", None)
    return int(size) if size is not None else 0


def _mode_of(attrs: Any) -> str | None:
    if isinstance(attrs, dict):
        mode = attrs.get("mode") or attrs.get("Attributes")
        return str(mode) if mode is not None else None
    mode = getattr(attrs, "mode", None)
    if mode is None:
        mode = getattr(attrs, "attributes", None)
    if mode is None:
        return None
    if isinstance(mode, int):
        return format(mode & 0o7777, "04o")
    return str(mode)


def _is_dir_reparse(attrs: Any) -> bool:
    """True when a reparse-point entry's own attributes mark it a directory.

    ``_kind_from_attrs`` promotes every reparse point to ``kind=link`` so the
    resolve follows the referent, which hides the entry's own nature: Windows
    reports a junction / symlink-to-directory as ``Directory, ReparsePoint``.
    The distinction decides whether promoting onto the entry is safe, and it
    is the only signal left once the referent cannot be read (see
    :func:`_reject_dir_reparse`).
    """
    mode = _mode_of(attrs)
    return bool(mode) and "directory" in mode.lower()


def _reject_dir_reparse(attrs: Any, shown: str, path: str) -> None:
    """Reject a *directory* reparse point whose referent could not be read.

    Same verdict the readable case reaches one resolve step later (the
    referent is a directory): the promote refuses a directory destination
    instead of treating it as a container, so without this the write would
    end in a generic remote error rather than the verdict the caller can act
    on. A non-directory reparse point keeps the caller's existing fallback
    behavior.
    """
    if _is_dir_reparse(attrs):
        raise FsError(
            "IS_A_DIR",
            f"is a directory: {shown}",
            details={"path": path},
        )


def _mtime_of(attrs: Any) -> str | None:
    if isinstance(attrs, dict):
        return _mtime_iso(attrs.get("mtime") or attrs.get("LastWriteTimeUtc"))
    mtime = getattr(attrs, "mtime", None)
    if mtime is None:
        mtime = getattr(attrs, "st_mtime", None)
    return _mtime_iso(mtime)


def _map_fs_error(exc: BaseException, path: str) -> FsError:
    if isinstance(exc, FsError):
        return exc
    name = type(exc).__name__
    text = str(exc).strip() or name
    text = " ".join(text.split())
    low = text.lower()
    code = "FS_ERROR"
    if isinstance(exc, TimeoutError) or name == "TimeoutError" or (
        "timed out" in low and "asyncloopbridge" in low
    ):
        code = "TIMEOUT"
        if "timed out" not in low:
            text = f"winrm fs operation timed out: {path}"
    elif not _is_http_rejection(exc):
        # Only text that came from the remote filesystem may be read as a
        # path/permission verdict. An HTTP-level rejection carries the
        # server's or an intermediary's body (an IIS/nginx error page can
        # read like a file error), so it keeps FS_ERROR and its raw transport
        # text instead of telling the caller about a path nobody examined.
        if (
            "no such file" in low
            or "not found" in low
            or "cannot find path" in low
            or "does not exist" in low
            or name in {"FileNotFoundError", "ItemNotFoundException"}
        ):
            code = "NOT_FOUND"
            text = f"path not found: {path}"
        elif "permission" in low or "access is denied" in low or name in {
            "PermissionError",
            "UnauthorizedAccessException",
        }:
            code = "PERMISSION_DENIED"
            text = f"permission denied: {path}"
        elif "not a directory" in low:
            code = "NOT_A_DIR"
        elif "is a directory" in low or "is a container" in low:
            code = "IS_A_DIR"
    if len(text) > 200:
        text = text[:197] + "..."
    return FsError(code, text, details={"path": path})


def _raise_if_timeout(exc: BaseException, path: str) -> None:
    """Re-raise a hang as ``FsError(TIMEOUT)``; no-op for every other error.

    Callers that treat a failed stat/readlink as a soft miss (not-a-dir,
    dangling parent) must invoke this first so a wall-clock timeout is
    never disguised as ``ALREADY_EXISTS`` or a partial rmtree success.
    """
    if isinstance(exc, FsError):
        if exc.code == "TIMEOUT":
            raise exc
        return
    mapped = _map_fs_error(exc, path)
    if mapped.code == "TIMEOUT":
        raise mapped from exc


def _is_timeout_failure(exc: BaseException) -> bool:
    """True when *exc* is a hang, so a transfer thread may still be running.

    Only a timed-out transfer can land its temp after the caller already saw
    the put fail (the abandoned worker keeps streaming). Every other failure
    is complete by the time it is raised, so the temp either exists now or
    never will.
    """
    if isinstance(exc, FsError):
        return exc.code == "TIMEOUT"
    return isinstance(exc, TimeoutError)


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class WinrmFs:
    """Filesystem ops over a :class:`WinRMFileClient` (or ``PypsrpFileClient``).

    Pass a connected *client*, or a *factory* to keep open lazy; *cwd* / *home*
    absolutize relative remote paths in results. The production factory is a
    transport's bound ``open_fs``, which takes the transport op lock an
    in-flight exec / ps may hold; that wait stays inside the whole-op budget.

    ``ps_caps`` (``transport.meta["winrm_ps"]``) is a capability boundary: with
    it present and ``ps_script_fs`` ``False``, script-based FS ops raise
    ``FsError("UNSUPPORTED")``; a missing dict or ``probe_skipped`` keeps the
    legacy allow (lab / ``probe=False`` compatibility).

    *timeout_s* overrides the oneshot ``execute_ps`` budget on a
    :class:`PypsrpFileClient` (omitted: the client's own budget,
    :data:`DEFAULT_WINRM_FS_TIMEOUT_S`). *op_timeout_s* is the whole public-op
    budget shared across every RT in one ``list`` / ``rm`` / ``put`` call,
    defaulting to the resolved per-call ceiling, so a multi-RT op cannot
    approach N times it; each hang still stays capped per call.

    Error-path temp cleanup (mid-copy / promote / progress-copy failure,
    including a put whose whole-op budget is spent) spends
    :data:`DEFAULT_WINRM_FS_CLEANUP_TIMEOUT_S`, not the spent whole-op
    deadline, so a budget-expired put leaves no full-payload temp behind;
    ``_cleanup_timeout_s`` overrides the module default.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        factory: WinrmFsFactory | None = None,
        cwd: str | None = None,
        home: str | None = None,
        ps_caps: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        op_timeout_s: float | None = None,
    ) -> None:
        if client is None and factory is None:
            raise ValueError("WinrmFs requires client= or factory=")
        self._client = client
        self._factory = factory
        # The production factory is a transport's bound ``open_fs``: that
        # method is a serial op, so calling it takes the transport op lock an
        # in-flight exec / ps may hold. When the owner exposes the timed gate,
        # the lazy open waits for the lock only within the remaining op
        # budget instead of overshooting it before any remote call.
        self._open_gate = getattr(
            getattr(factory, "__self__", None), "serial_ops_within", None
        )
        self._cwd = cwd
        self._home = home
        self._ps_caps = ps_caps
        # Explicit override only: None keeps the client's own budget so a
        # short PypsrpFileClient(timeout_s=0.4) is not clobbered by the
        # module default when wrapping with WinrmFs(client).
        self._timeout_s = float(timeout_s) if timeout_s is not None else None
        # Whole-op budget: omit -> mirror resolved per-call ceiling.
        self._op_timeout_s = (
            self._resolved_per_call_s(client)
            if op_timeout_s is None
            else float(op_timeout_s)
        )
        # Absolute monotonic deadline for the current public op, or None when
        # no public method has entered :meth:`_op_budget`.
        self._op_deadline: float | None = None
        # Own budget for error-path temp cleanup; never the whole-op one (see
        # _best_effort_remove).
        self._cleanup_timeout_s = DEFAULT_WINRM_FS_CLEANUP_TIMEOUT_S
        if self._timeout_s is not None:
            self._apply_timeout_to_client(self._client)
        # Bind op fields even when timeout_s is omitted so execute_ps can
        # clamp to remaining once a public op is in flight.
        self._bind_client_op_deadline(self._op_deadline)

    def _resolved_per_call_s(self, client: Any | None = None) -> float:
        """Per-call ceiling used when defaulting ``op_timeout_s``."""
        if self._timeout_s is not None:
            return float(self._timeout_s)
        src = client if client is not None else self._client
        if src is not None:
            raw = getattr(src, "_timeout_s", None)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return DEFAULT_WINRM_FS_TIMEOUT_S

    def _apply_timeout_to_client(self, client: Any) -> None:
        """Push this backend's per-call budget onto a Pypsrp-shaped client."""
        if client is None or self._timeout_s is None:
            return
        # Duck-type: setattr keeps clients that lack the attr untouched when
        # they are not Pypsrp-shaped.
        if isinstance(client, PypsrpFileClient) or (
            hasattr(client, "_timeout_s") and hasattr(client, "_execute_ps")
        ):
            try:
                client._timeout_s = self._timeout_s  # noqa: SLF001
            except Exception:  # noqa: BLE001 - best-effort config only
                pass

    def _bind_client_op_deadline(self, deadline: float | None) -> None:
        """Propagate whole-op deadline onto a Pypsrp-shaped client (if any)."""
        client = self._client
        if client is None:
            return
        if not (
            isinstance(client, PypsrpFileClient)
            or (hasattr(client, "_execute_ps") and hasattr(client, "_timeout_s"))
        ):
            return
        try:
            client._op_deadline = deadline  # noqa: SLF001
            client._op_timeout_s = (  # noqa: SLF001
                self._op_timeout_s if deadline is not None else None
            )
        except Exception:  # noqa: BLE001 - best-effort config only
            pass

    def _bind_cleanup_deadline(
        self, client: Any, deadline: float, budget_s: float
    ) -> tuple[float | None, float | None]:
        """Point a Pypsrp-shaped client at the cleanup deadline.

        Returns the binding it replaced so the caller can restore the whole-op
        state afterwards. The whole-op binding is what makes a spent put
        refuse to start any further remote call, so cleanup replaces it for
        the duration of the remove; a client without those fields (duck type,
        test double) keeps its own behaviour and is bounded by the bridge wait
        instead.
        """
        if not (hasattr(client, "_execute_ps") and hasattr(client, "_timeout_s")):
            return (None, None)
        prev = (
            getattr(client, "_op_deadline", None),
            getattr(client, "_op_timeout_s", None),
        )
        try:
            client._op_deadline = deadline  # noqa: SLF001
            client._op_timeout_s = budget_s  # noqa: SLF001
        except Exception:  # noqa: BLE001 - best-effort config only
            pass
        return prev

    def _restore_client_deadline(
        self, client: Any, prev: tuple[float | None, float | None]
    ) -> None:
        """Restore the whole-op binding replaced for a cleanup remove."""
        if not (hasattr(client, "_execute_ps") and hasattr(client, "_timeout_s")):
            return
        try:
            client._op_deadline, client._op_timeout_s = prev  # noqa: SLF001
        except Exception:  # noqa: BLE001 - best-effort config only
            pass

    @contextmanager
    def _op_budget(self) -> Iterator[None]:
        """Bind a whole-op wall-clock deadline for nested remote RTs.

        Public methods enter this once. Nested re-entry (recursive ``list``,
        ``rm`` -> ``stat``, ``read`` -> ``stat``) keeps the outer deadline so
        remaining budget is shared across the whole agent-facing op.
        """
        prev = self._op_deadline
        if prev is None:
            self._op_deadline = time.monotonic() + max(0.0, self._op_timeout_s)
            self._bind_client_op_deadline(self._op_deadline)
        try:
            yield
        finally:
            self._op_deadline = prev
            self._bind_client_op_deadline(prev)

    def _ensure_op_budget(self) -> None:
        """Raise TIMEOUT when the whole-op deadline is already spent.

        Called before each remote RT (client adapter / list_with_attrs / ...)
        so multi-RT and Pypsrp paths share the same wall-clock ceiling
        without waiting for a per-call hang timeout.
        """
        deadline = self._op_deadline
        if deadline is None:
            return
        if time.monotonic() < deadline:
            return
        raise FsError(
            "TIMEOUT",
            f"winrm fs operation timed out after {self._op_timeout_s}s",
            details={
                "timeout_s": self._op_timeout_s,
                "op_timeout_s": self._op_timeout_s,
            },
        )

    def _run_blocking_with_timeout(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float,
    ) -> Any:
        """Run a blocking native client call on the shared bridge.

        Same shape as :meth:`PypsrpFileClient._run_blocking_with_timeout`:
        ``asyncio.to_thread`` so a hung ``copy``/``fetch`` cannot pin the
        caller. On timeout the bridge raises ``TimeoutError``; the executor
        thread may still run until the remote side returns.
        """
        import asyncio

        from mcp_remote_control.transport.async_bridge import get_shared_bridge

        async def _wrap() -> Any:
            return await asyncio.to_thread(fn)

        return get_shared_bridge().run(_wrap(), timeout_s=timeout_s)

    def _native_transfer_budget_s(self) -> float | None:
        """Remaining whole-op budget, else the per-call / default ceiling."""
        deadline = self._op_deadline
        if deadline is not None:
            return max(0.0, deadline - time.monotonic())
        if self._timeout_s is not None:
            return float(self._timeout_s)
        return float(self._op_timeout_s)

    def _run_native_transfer(self, fn: Callable[[], Any], *, what: str) -> Any:
        """Run native copy/fetch under the remaining whole-op wall-clock.

        Pre-checking remaining budget is not enough: the transfer itself
        must be abandoned when the deadline elapses, or a hung pypsrp
        ``copy``/``fetch`` parks the worker forever.
        """
        self._ensure_op_budget()
        budget = self._native_transfer_budget_s()
        if budget is not None and budget > 0:
            try:
                return self._run_blocking_with_timeout(fn, timeout_s=budget)
            except TimeoutError as exc:
                raise FsError(
                    "TIMEOUT",
                    f"winrm fs {what} timed out after {budget}s",
                    details={
                        "timeout_s": (
                            self._op_timeout_s
                            if self._op_deadline is not None
                            else budget
                        ),
                        "op_timeout_s": self._op_timeout_s,
                    },
                ) from exc
        return fn()

    @property
    def via(self) -> str:
        return "winrm"

    def _fs(self) -> Any:
        if self._client is not None:
            return self._client
        assert self._factory is not None
        client = self._open_client()
        if self._timeout_s is not None:
            self._apply_timeout_to_client(client)
        self._client = client
        # Lazy open may land mid-op - bind current deadline if any.
        if self._op_deadline is not None:
            self._bind_client_op_deadline(self._op_deadline)
        return self._client

    def _open_client(self) -> Any:
        """Run the lazy factory within the remaining whole-op budget.

        ``transport.open_fs`` serializes on the transport op lock, which a
        long exec / ps call holds for as long as its own budget allows.
        Waiting for it unbounded would spend the whole op budget (or more)
        before the first remote call, so when a deadline is bound and the
        factory's owner exposes a timed gate the wait is clamped to the
        remaining budget and reported as ``FsError(TIMEOUT)`` when the lock
        is not free in time. Factories without an owner or a gate (test
        doubles, plain callables) run as before.
        """
        assert self._factory is not None
        gate = self._open_gate
        deadline = self._op_deadline
        if gate is None or deadline is None:
            return self._factory()
        remaining = deadline - time.monotonic()
        # ExitStack: only the acquire is turned into FsError(TIMEOUT); a
        # failure raised later by the factory keeps its own meaning.
        stack = ExitStack()
        try:
            stack.enter_context(self._open_gate(remaining))
        except TimeoutError as exc:
            raise _op_timeout_error(self._op_timeout_s) from exc
        with stack:
            return self._factory()

    def _ps_script_fs_allowed(self) -> bool:
        """Return True when script FS may run (or probe was skipped / absent)."""
        caps = self._ps_caps
        if caps is None:
            # No winrm_ps meta -> legacy allow (probe=False / older transports).
            return True
        if caps.get("probe_skipped") is True:
            return True
        if caps.get("ps_probe") == "skipped":
            return True
        # Only block when the probe explicitly derived False.
        return caps.get("ps_script_fs") is not False

    def _require_ps_script_fs(self) -> None:
        """Raise UNSUPPORTED when probe says PowerShell script FS is unavailable.

        Call at the start of public ops that depend on Get-Item / File IO /
        ConvertTo-Json (list, stat, read, write, mkdir, rm, and script put/get).
        """
        if self._ps_script_fs_allowed():
            return
        caps = self._ps_caps or {}
        lang = caps.get("language_mode") or caps.get("lang_mode") or "unknown"
        raise FsError(
            "UNSUPPORTED",
            (
                "WinRM script filesystem requires FullLanguage "
                f"(language_mode={lang}); host reports ps_script_fs=false"
            ),
            details={
                "language_mode": lang,
                "ps_script_fs": False,
            },
        )

    def _reraise_gated_native_failure(self, exc: BaseException, path: str) -> None:
        """Prefer UNSUPPORTED when native transfer fails and script FS is gated.

        Specific path errors (NOT_FOUND, PERMISSION_DENIED, ...) are kept.
        Opaque ``FS_ERROR`` is replaced by the capability gate message so the
        Agent sees language_mode / ``ps_script_fs`` rather than a bare native
        exception - script write/read is not available as a fallback.
        """
        if isinstance(exc, FsError):
            if exc.code != "FS_ERROR":
                raise exc
            self._require_ps_script_fs()
        mapped = _map_fs_error(exc, path)
        if mapped.code != "FS_ERROR":
            raise mapped from exc
        self._require_ps_script_fs()

    def resolve_path(self, path: str) -> str:
        """Return an absolute-looking remote path for Windows output semantics."""
        text = str(path).strip()
        if not text:
            raise FsError("INVALID_ARG", "path is empty")
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            home = self._home or ""
            if home:
                rest = text[1:].lstrip("\\/")
                text = home if not rest else _win_sep_join(home, rest)
            elif text == "~":
                text = "C:\\"
            else:
                text = text[2:]
        if _is_abs_win(text):
            return _norm_win_path(text)
        base = self._cwd or self._home or "C:\\"
        return _norm_win_path(_win_sep_join(base, text))

    def list(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> ListResult:
        with self._op_budget():
            return self._list(path, recursive=recursive)

    def _list(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> ListResult:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            dir_attrs = self._stat(client, abs_path)
            if _kind_from_attrs(dir_attrs) != "dir":
                raise FsError(
                    "NOT_A_DIR",
                    f"not a directory: {abs_path}",
                    details={"path": abs_path},
                )
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc

        entries: list[ListEntry] = []
        # "." reuses the dir attrs already fetched above (no second stat RT).
        entries.append(
            ListEntry(
                name=".",
                kind="d",
                size=0,
                mode=_mode_of(dir_attrs),
                mtime=_mtime_of(dir_attrs),
                path=abs_path,
            )
        )

        # ".." requires statting the parent (1 RT, unavoidable).
        parent = _parent_win(abs_path)
        if parent and parent != abs_path:
            try:
                st_par = self._stat(client, parent)
                entries.append(
                    ListEntry(
                        name="..",
                        kind="d",
                        size=0,
                        mode=_mode_of(st_par),
                        mtime=_mtime_of(st_par),
                        path=parent,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # TIMEOUT is a whole-op failure; other parent-stat misses
                # still stub ".." so a vanished parent is not fatal.
                _raise_if_timeout(exc, parent)
                entries.append(ListEntry(name="..", kind="d", size=0, path=parent))

        # Children: prefer a single batched attrs round-trip when the client
        # supports it; fall back to per-name stat (N RTs) otherwise.
        if isinstance(client, SupportsListWithAttrs):
            try:
                self._ensure_op_budget()
                child_attrs = client.list_with_attrs(abs_path)
            except FsError:
                raise
            except Exception as exc:  # noqa: BLE001 - fall back unless this is a hang
                _raise_if_timeout(exc, abs_path)
                child_attrs = None
            if child_attrs is not None:
                child_entries: list[ListEntry] = []
                for c in child_attrs:
                    if not isinstance(c, dict):
                        continue
                    name = str(c.get("name") or c.get("Name") or "")
                    if not name or name in {".", ".."}:
                        continue
                    kind = _kind_from_attrs(c)
                    child_entries.append(
                        ListEntry(
                            name=name,
                            kind=_entry_kind(kind),
                            size=0 if kind == "dir" else _size_of(c),
                            mode=_mode_of(c),
                            mtime=_mtime_of(c),
                            path=_win_sep_join(abs_path, name),
                        )
                    )
                # Stable sorted-by-name ordering (matches the per-stat path).
                child_entries.sort(key=lambda e: e.name)
                entries.extend(child_entries)
            else:
                self._list_children_by_stat(client, abs_path, entries)
        else:
            try:
                names = self._listdir(client, abs_path)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_path) from exc
            self._append_children_from_names(client, abs_path, names, entries)

        if recursive:
            subdirs = [e for e in entries if e.kind == "d" and e.name not in {".", ".."}]
            # Cycle protection: a junction/reparse-point reported by
            # list_with_attrs as kind="dir" and pointing to an ancestor (or
            # reappearing one level deeper each time) would recurse forever.
            # *visited* catches same-path cycles immediately; *depth* is the
            # backstop for the ever-growing-path shape, where each level adds
            # a longer path so a visited-set alone never matches. Cap at
            # _MAX_RECURSE_DEPTH - enough for any realistic tree; when hit,
            # raise DEPTH_EXCEEDED (aligned with SFTP), never a partial tree.
            self._collect_recursive(
                abs_path,
                subdirs,
                entries,
                visited={abs_path},
                depth=0,
            )

        return ListResult(path=abs_path, entries=entries)

    def _collect_recursive(
        self,
        top: str,
        subdirs: list[ListEntry],
        entries: list[ListEntry],
        *,
        visited: set[str],
        depth: int,
        max_depth: int = _MAX_RECURSE_DEPTH,
    ) -> None:
        """Append descendants of *subdirs* to *entries*, named relative to *top*.

        Reuses single-level ``list`` per subdir. Descendant names come from
        absolute ``se.path`` relative to *top* via ``_rel_name_under`` so
        depth >=3 and prefix-overlapping basenames stay correct. DFS - each
        subdir's full subtree before the next sibling. Same-path re-entry is
        skipped via *visited*; *max_depth* exceed raises
        ``FsError(DEPTH_EXCEEDED)`` (SFTP-aligned - never silent partial ok).
        """
        for e in subdirs:
            sub_path = e.path or _win_sep_join(top, e.name)
            if sub_path in visited:
                continue
            if depth >= max_depth:
                raise FsError(
                    "DEPTH_EXCEEDED",
                    f"maximum recursion depth ({max_depth}) exceeded at: {sub_path}",
                    details={"path": sub_path, "max_depth": max_depth},
                )
            visited.add(sub_path)
            # Nested list keeps outer whole-op deadline (re-enters _op_budget).
            sub = self.list(sub_path, recursive=False)
            child_dirs: list[ListEntry] = []
            for se in sub.entries:
                if se.name in {".", ".."}:
                    continue
                # ListEntry.path is optional; fall back like *sub_path* above so
                # _rel_name_under always receives a concrete Windows path.
                se_abs = se.path or _win_sep_join(sub_path, se.name)
                se_name = _rel_name_under(top, se_abs)
                if not se_name:
                    se_name = se.name
                rel = ListEntry(
                    name=se_name,
                    kind=se.kind,
                    size=se.size,
                    mode=se.mode,
                    mtime=se.mtime,
                    path=se_abs,
                )
                entries.append(rel)
                if se.kind == "d":
                    child_dirs.append(rel)
            if child_dirs:
                self._collect_recursive(
                    top,
                    child_dirs,
                    entries,
                    visited=visited,
                    depth=depth + 1,
                    max_depth=max_depth,
                )

    def _list_children_by_stat(
        self,
        client: Any,
        abs_path: str,
        entries: list[ListEntry],
    ) -> None:
        try:
            names = self._listdir(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        self._append_children_from_names(client, abs_path, names, entries)

    def _append_children_from_names(
        self,
        client: Any,
        abs_path: str,
        names: list[str],
        entries: list[ListEntry],
    ) -> None:
        for name in sorted(names):
            if name in {".", ".."}:
                continue
            child = _win_sep_join(abs_path, name)
            try:
                st = self._stat(client, child)
                kind = _kind_from_attrs(st)
                entries.append(
                    ListEntry(
                        name=name,
                        kind=_entry_kind(kind),
                        size=0 if kind == "dir" else _size_of(st),
                        mode=_mode_of(st),
                        mtime=_mtime_of(st),
                        path=child,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # TIMEOUT fails the listing; other per-name misses stay
                # kind=o so one vanished child is not a whole-op error.
                _raise_if_timeout(exc, child)
                entries.append(ListEntry(name=name, kind="o", path=child))

    def stat(self, path: str) -> StatInfo:
        with self._op_budget():
            return self._stat_info(path)

    def _stat_info(self, path: str) -> StatInfo:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            attrs = self._stat(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        kind = _kind_from_attrs(attrs)
        return StatInfo(
            path=abs_path,
            kind=kind,
            size=0 if kind == "dir" else _size_of(attrs),
            mode=_mode_of(attrs),
            mtime=_mtime_of(attrs),
        )

    def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> ReadResult:
        with self._op_budget():
            self._require_ps_script_fs()
            abs_path = self.resolve_path(path)
            client = self._fs()
            limit = DEFAULT_READ_MAX_BYTES if max_bytes is None else int(max_bytes)
            if limit < 0:
                limit = DEFAULT_READ_MAX_BYTES
            try:
                # Nested stat keeps outer whole-op deadline.
                info = self._stat_info(abs_path)
                if info.kind == "dir":
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory: {abs_path}",
                        details={"path": abs_path},
                    )
                data = self._read_bytes(client, abs_path, limit + 1)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_path) from exc
            truncated = len(data) > limit
            if truncated:
                data = data[:limit]
            is_text, encoding = detect_text(data)
            return ReadResult(
                path=abs_path,
                data=data,
                truncated=truncated,
                encoding=encoding if is_text else None,
                is_text=is_text,
            )

    def write(
        self,
        path: str,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
    ) -> WriteResult:
        with self._op_budget():
            self._require_ps_script_fs()
            abs_path = self.resolve_path(path)
            client = self._fs()
            raw = content.encode(encoding) if isinstance(content, str) else content
            created = True
            try:
                try:
                    st = self._stat(client, abs_path)
                    if _kind_from_attrs(st) == "dir":
                        raise FsError(
                            "IS_A_DIR",
                            f"is a directory: {abs_path}",
                            details={"path": abs_path},
                        )
                    created = False
                except FsError as exc:
                    if exc.code == "IS_A_DIR":
                        raise
                    # A hang is not "path missing"; fail instead of writing as new.
                    _raise_if_timeout(exc, abs_path)
                    created = True
                except Exception as exc:  # noqa: BLE001
                    _raise_if_timeout(exc, abs_path)
                    created = True
                parent = _parent_win(abs_path)
                if parent:
                    self._mkdir_p(client, parent)
                # Resolve final-component reparse/symlink so content updates the
                # referent and the link entry is not replaced by a regular file.
                dest = self._resolve_final_link(client, abs_path)
                self._write_bytes(client, dest, raw)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_path) from exc
            return WriteResult(path=abs_path, bytes_written=len(raw), created=created)

    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        with self._op_budget():
            src = Path(str(local_path)).expanduser()
            if not src.is_absolute():
                src = (Path.cwd() / src).resolve()
            else:
                src = src.resolve()
            if not src.is_file():
                raise FsError(
                    "NOT_FOUND",
                    f"local path not found or not a file: {src}",
                    details={"path": str(src)},
                )
            abs_remote = self.resolve_path(remote_path)
            client = self._fs()
            # When script FS is blocked, still attempt native copy (no PS mkdir/write).
            # Progress and write_file paths need script FS -> UNSUPPORTED with hint.
            if not self._ps_script_fs_allowed():
                if progress is not None:
                    self._require_ps_script_fs()
                try:
                    if self._try_copy(client, str(src), abs_remote):
                        size = int(src.stat().st_size)
                        return TransferResult(
                            path=abs_remote,
                            local=str(src),
                            bytes_transferred=int(size),
                            direction="put",
                        )
                except Exception as exc:  # noqa: BLE001 - native client surface
                    self._reraise_gated_native_failure(exc, abs_remote)
                self._require_ps_script_fs()
            try:
                parent = _parent_win(abs_remote)
                if parent:
                    self._mkdir_p(client, parent)
                # Final-component reparse resolve (same policy as SFTP/local): put
                # lands on the referent so Move-Item / native copy does not replace
                # the reparse directory entry with a regular file.
                dest = self._resolve_final_link(client, abs_remote)
                size = int(src.stat().st_size)
                if progress is not None:
                    size = self._put_with_progress(client, src, dest, size, progress)
                elif self._try_copy(client, str(src), dest):
                    pass
                else:
                    data = src.read_bytes()
                    self._write_bytes(client, dest, data)
                    size = len(data)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_remote) from exc
            return TransferResult(
                path=abs_remote,
                local=str(src),
                bytes_transferred=int(size),
                direction="put",
            )

    def get(
        self,
        remote_path: str,
        local_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        with self._op_budget():
            abs_remote = self.resolve_path(remote_path)
            dst = Path(str(local_path)).expanduser()
            if not dst.is_absolute():
                dst = (Path.cwd() / dst).resolve()
            # Final-component local symlink chain -> replace updates the referent
            # and keeps the link inode (same policy as LocalFs.get / SftpFs.get).
            dst_resolved = Path(_resolve_local_final_link(str(dst)))
            client = self._fs()
            # When script FS is blocked, still attempt native fetch (no PS stat/read).
            # Progress and read_file paths need script FS -> UNSUPPORTED with hint.
            if not self._ps_script_fs_allowed():
                if progress is not None:
                    self._require_ps_script_fs()
                dst_resolved.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst_resolved.parent / mrc_tmp_name(dst_resolved.name)
                try:
                    if self._try_fetch(client, abs_remote, str(tmp)):
                        size = tmp.stat().st_size if tmp.is_file() else 0
                        # The temp carries the client's own mode (real pypsrp
                        # fetch copies an mkstemp file), so promoting it would
                        # rewrite an existing destination's permissions. Best
                        # effort: where the destination's filesystem has no
                        # chmod, the finished transfer must still land instead
                        # of surfacing the local failure against the readable
                        # remote path.
                        try:
                            _copy_local_mode_if_exists(tmp, str(dst_resolved))
                        except (OSError, NotImplementedError):
                            pass
                        os.replace(tmp, dst_resolved)
                        return TransferResult(
                            path=abs_remote,
                            local=str(dst),
                            bytes_transferred=int(size),
                            direction="get",
                        )
                except Exception as exc:  # noqa: BLE001 - native client surface
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._reraise_gated_native_failure(exc, abs_remote)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._require_ps_script_fs()
            try:
                # Nested stat keeps the outer whole-op deadline.
                info = self._stat_info(abs_remote)
                if info.kind == "dir":
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory: {abs_remote}",
                        details={"path": abs_remote},
                    )
                dst_resolved.parent.mkdir(parents=True, exist_ok=True)
                total = int(info.size)
                # Download to a local temp in the resolved parent's dir, then
                # os.replace over the referent. Mid-get failure removes the temp
                # and leaves any prior destination intact.
                tmp = dst_resolved.parent / mrc_tmp_name(dst_resolved.name)
                try:
                    if progress is not None:
                        size = self._get_with_progress(
                            client, abs_remote, tmp, total, progress
                        )
                    elif self._try_fetch(client, abs_remote, str(tmp)):
                        size = tmp.stat().st_size if tmp.is_file() else info.size
                    else:
                        data = self._read_bytes(client, abs_remote, None)
                        tmp.write_bytes(data)
                        size = len(data)
                    # Same policy as the native branch: the temp was written
                    # under the local umask, so an existing destination's mode
                    # is re-applied before the replace; a missing destination
                    # keeps the temp's default. Best effort: where the
                    # destination's filesystem has no chmod, the finished
                    # transfer must still land instead of surfacing the local
                    # failure against the readable remote path.
                    try:
                        _copy_local_mode_if_exists(tmp, str(dst_resolved))
                    except (OSError, NotImplementedError):
                        pass
                    os.replace(tmp, dst_resolved)
                except Exception:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_remote) from exc
            return TransferResult(
                path=abs_remote,
                local=str(dst),
                bytes_transferred=int(size),
                direction="get",
            )

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo:
        with self._op_budget():
            self._require_ps_script_fs()
            abs_path = self.resolve_path(path)
            client = self._fs()
            try:
                if parents:
                    self._mkdir_p(client, abs_path)
                else:
                    self._mkdir(client, abs_path)
            except FsError:
                raise
            except Exception as exc:
                try:
                    info = self._stat_info(abs_path)
                    if info.kind == "dir":
                        return info
                except FsError:
                    pass
                raise _map_fs_error(exc, abs_path) from exc
            return self._stat_info(abs_path)

    def rm(self, path: str, *, recursive: bool = False) -> str:
        with self._op_budget():
            self._require_ps_script_fs()
            abs_path = self.resolve_path(path)
            client = self._fs()
            try:
                info = self._stat_info(abs_path)
                if info.kind == "dir":
                    if not recursive:
                        raise FsError(
                            "IS_A_DIR",
                            f"is a directory (use recursive): {abs_path}",
                            details={"path": abs_path},
                        )
                    self._rmtree(client, abs_path)
                else:
                    self._remove(client, abs_path)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_path) from exc
            return abs_path

    # ------------------------------------------------------------------
    # progress-aware transfer helpers
    # ------------------------------------------------------------------

    def _put_with_progress(
        self,
        client: Any,
        src: Path,
        abs_remote: str,
        total: int,
        progress: ProgressCallback,
    ) -> int:
        """Upload with progress; prefer chunked open/write, else whole-file.

        SupportsFileOpen path streams to a same-directory temp, fail-closes
        the write handle (close/flush errors are not swallowed), then promotes
        onto the destination. Dest is never opened with ``wb`` until promote,
        so a mid-stream failure cannot destroy the only prior remote copy.
        Aligns with ``PypsrpFileClient.write_file`` (temp + replace promote)
        and SFTP atomic put / ``_close_write_handle``. When *abs_remote* is a
        reparse/symlink, the final-component chain is resolved first so the
        promote lands on the referent and the link entry is preserved.
        """
        report_progress(progress, 0, total)
        # Resolve once for both open-promote and whole-file branches.
        dest = self._resolve_final_link(client, abs_remote)
        if isinstance(client, SupportsFileOpen):
            tmp = _win_temp_path(dest)
            done = 0
            try:
                self._ensure_op_budget()
                fh = client.open(tmp, "wb")
                write_exc: BaseException | None = None
                try:
                    write = getattr(fh, "write", None)
                    if not callable(write):
                        raise FsError("UNSUPPORTED", "winrm file has no write")
                    with src.open("rb") as fsrc:
                        while True:
                            chunk = fsrc.read(DEFAULT_TRANSFER_CHUNK)
                            if not chunk:
                                break
                            self._ensure_op_budget()
                            write(chunk)
                            done += len(chunk)
                            report_progress(progress, done, total)
                except Exception as exc:
                    write_exc = exc
                try:
                    self._close_write_handle(fh, tmp)
                except Exception as close_exc:
                    # Prefer the original write error when both fail.
                    if write_exc is None:
                        write_exc = close_exc
                if write_exc is not None:
                    raise write_exc
                # Promote only after close succeeds - never half-ok dest.
                self._promote_temp_file(client, tmp, dest)
            except Exception as exc:
                # Clean temp only; never remove dest (prior good copy). A hang
                # may have abandoned a thread that still streams the temp onto
                # the host, so the cleanup sweeps for that late arrival.
                self._best_effort_remove(
                    client, tmp, sweep=_is_timeout_failure(exc)
                )
                raise
            if done != total:
                report_progress(progress, done, total if total else done)
            return done

        # Whole-file path (copy/write_file): report start + complete.
        # Prefer copy so the source path is streamed without loading the whole
        # file into memory; only read bytes when write_file is needed.
        # Native copy under script FS lands on a temp then promote (see
        # _try_copy); dest is not opened until that promote.
        if self._try_copy(client, str(src), dest):
            report_progress(progress, total, total)
            return total
        data = src.read_bytes()
        self._write_bytes(client, dest, data)
        report_progress(progress, len(data), total if total else len(data))
        return len(data)

    def _get_with_progress(
        self,
        client: Any,
        abs_remote: str,
        dst: Path,
        total: int,
        progress: ProgressCallback,
    ) -> int:
        report_progress(progress, 0, total if total else None)
        if isinstance(client, SupportsFileOpen):
            self._ensure_op_budget()
            fh = client.open(abs_remote, "rb")
            done = 0
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "winrm file has no read")
                with dst.open("wb") as fdst:
                    while True:
                        self._ensure_op_budget()
                        chunk = _coerce_bytes(read(DEFAULT_TRANSFER_CHUNK))
                        if not chunk:
                            break
                        fdst.write(chunk)
                        done += len(chunk)
                        report_progress(
                            progress, done, total if total else None
                        )
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            if total and done != total:
                report_progress(progress, done, total)
            elif not total:
                report_progress(progress, done, done)
            return done

        if self._try_fetch(client, abs_remote, str(dst)):
            size = dst.stat().st_size if dst.is_file() else total
        else:
            data = self._read_bytes(client, abs_remote, None)
            dst.write_bytes(data)
            size = len(data)
        report_progress(progress, int(size), total if total else int(size))
        return int(size)

    # ------------------------------------------------------------------
    # low-level client adapters (Protocol surface)
    # ------------------------------------------------------------------

    def _stat(self, client: Any, path: str) -> Any:
        self._ensure_op_budget()
        try:
            return client.stat(path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, path) from exc

    def _readlink(self, client: Any, path: str) -> str | None:
        """Read a reparse/symlink target; return ``None`` when unsupported.

        Preference order:
        1. Client ``readlink(path)`` when present.
        2. ``target`` / ``Target`` / ``LinkTarget`` on stat attrs (dict or object).

        ``FsError(TIMEOUT)`` is re-raised: a hang is not "no target". A
        link-class failure is re-raised too: the transport has already
        retired the session, so ``None`` here would let the caller promote
        onto a path whose referent was never read while the row reports
        success (README: the fs path reports a link failure instead).
        """
        fn = getattr(client, "readlink", None)
        if callable(fn):
            try:
                self._ensure_op_budget()
                target = fn(path)
            except FsError as exc:
                _raise_if_timeout(exc, path)
                raise
            except Exception as exc:  # noqa: BLE001 - best-effort resolve
                _raise_if_timeout(exc, path)
                if _is_link_failure(exc):
                    raise
                return None
            if target is None:
                return None
            text = str(target).strip()
            return text or None
        try:
            attrs = self._stat(client, path)
        except FsError as exc:
            _raise_if_timeout(exc, path)
            return None
        if isinstance(attrs, dict):
            raw = (
                attrs.get("target")
                or attrs.get("Target")
                or attrs.get("LinkTarget")
            )
            if raw is not None:
                text = str(raw).strip()
                return text or None
            return None
        raw = getattr(attrs, "target", None)
        if raw is None:
            raw = getattr(attrs, "Target", None)
        if raw is None:
            raw = getattr(attrs, "LinkTarget", None)
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    def _resolve_final_link(self, client: Any, path: str) -> str:
        """Resolve the final-component reparse/symlink chain for write/put.

        Only *path* (and successive referents) are followed - parent-directory
        reparse points are left alone, matching SFTP ``_resolve_final_link``
        and the local backend. Content is written to the final referent so the
        original path stays a reparse/symlink entry. When *path* is not a
        link, ``readlink`` is unavailable, or a link target is missing,
        return the current path so callers keep existing behavior (including
        the temp + replace promote onto ordinary files). Caps depth at
        ``_MAX_SYMLINK_FOLLOW`` to avoid cycles.

        Raises ``IS_A_DIR`` when the final entry (after any link resolution)
        is a directory: the promote refuses a directory destination, so
        without this check the op would fail with a generic remote error
        instead of the verdict the caller can act on. A link whose own
        attributes mark it a directory is rejected the same way even when its
        referent cannot be read - the container rule applies to the reparse
        path itself.
        ``LocalFs`` / ``WinrmFs.write`` reject a directory destination the
        same way.
        """
        current = path
        for _ in range(_MAX_SYMLINK_FOLLOW):
            try:
                attrs = self._stat(client, current)
            except FsError as exc:
                if exc.code == "NOT_FOUND":
                    # Dangling or new path - write/promote here.
                    return current
                raise
            except Exception as exc:
                mapped = _map_fs_error(exc, current)
                if mapped.code == "NOT_FOUND":
                    return current
                raise mapped from exc
            kind = _kind_from_attrs(attrs)
            if kind == "dir":
                # A directory destination is not a writable target. Reporting
                # it is the only honest outcome: the promote refuses a
                # directory destination, so without this check the op would
                # fail with a generic remote error and *path* would never be
                # created. Same verdict as ``WinrmFs.write`` and ``LocalFs``.
                raise FsError(
                    "IS_A_DIR",
                    f"is a directory: {current}",
                    details={"path": path},
                )
            if kind != "link":
                return current
            target = self._readlink(client, current)
            if target is not None:
                target = str(target).strip()
                # PowerShell Target may be multi-valued; take first token.
                if "\n" in target:
                    target = target.splitlines()[0].strip()
            if not target:
                # Nothing to follow: a directory reparse point is still a
                # container destination and is rejected; anything else keeps
                # promoting onto the original path as before.
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

    def _listdir(self, client: Any, path: str) -> list[str]:
        self._ensure_op_budget()
        return _coerce_str_list(client.listdir(path))

    def _read_bytes(self, client: Any, path: str, max_n: int | None) -> bytes:
        # Protocol: read_file always accepts max_bytes (None = unbounded).
        self._ensure_op_budget()
        return _coerce_bytes(client.read_file(path, max_bytes=max_n))

    def _write_bytes(self, client: Any, path: str, data: bytes) -> None:
        # path is expected already resolved by write/put; resolve again as a
        # safety net for any internal callers that pass a raw remote path.
        dest = self._resolve_final_link(client, path)
        self._ensure_op_budget()
        client.write_file(dest, data)

    def _try_copy(self, client: Any, local: str, remote: str) -> bool:
        # Native only when the client opts in: flag is True and copy is callable.
        # Missing flag defaults False so a copy-only duck type cannot bypass
        # the ps_script_fs gate. The transfer itself is bounded by the
        # remaining whole-op wall-clock (not only a pre-check).
        if getattr(client, "has_native_copy", False) is True:
            copy_fn = getattr(client, "copy", None)
            if callable(copy_fn):
                if self._ps_script_fs_allowed():
                    # Script FS can promote: never stream onto dest in place.
                    self._native_copy_atomic(client, copy_fn, local, remote)
                else:
                    # Gated hosts have no Move-Item/write_file promote, so the
                    # destination never reaches _resolve_final_link's IS_A_DIR
                    # verdict; without one, a container destination would move
                    # the payload *inside* it and still report success.
                    self._reject_gated_dir_dest(client, remote)
                    self._run_native_transfer(
                        lambda: copy_fn(local, remote), what="copy"
                    )
                return True
        return False

    def _reject_gated_dir_dest(self, client: Any, path: str) -> None:
        """Best-effort IS_A_DIR check for a gated native-copy destination.

        The script-FS path rejects a container destination through
        :meth:`_resolve_final_link`; a gated host streams straight onto
        *path*, where the client's copy decides what a directory means (the
        shipped client fails, container semantics would move the payload
        inside and report success). ``stat`` is the only probe left, and on a
        host whose script FS is gated it may itself be unservable - that is
        the same capability that gated this branch, so an unanswerable probe
        leaves the verdict open and the copy runs as before instead of
        guessing. A probe that does answer rejects a directory (and a
        directory reparse point, whose container rule applies to the reparse
        path itself).
        """
        try:
            self._ensure_op_budget()
            attrs = self._stat(client, path)
        except FsError as exc:
            _raise_if_timeout(exc, path)
            return
        kind = _kind_from_attrs(attrs)
        if kind == "dir" or (kind == "link" and _is_dir_reparse(attrs)):
            raise FsError(
                "IS_A_DIR",
                f"is a directory: {path}",
                details={"path": path},
            )

    def _native_copy_atomic(
        self,
        client: Any,
        copy_fn: Callable[..., Any],
        local: str,
        remote: str,
    ) -> None:
        """Native copy onto a same-directory temp, then promote onto *remote*.

        Dest is not overwritten until promote. After copy, the temp is
        statted and must match the local source size; a short or missing
        remote file fails and is not promoted. Mid-copy / size-check
        failure removes the temp and leaves any prior dest intact (same
        policy as get / SFTP).
        """
        tmp = _win_temp_path(remote)
        try:
            self._run_native_transfer(lambda: copy_fn(local, tmp), what="copy")
            expected = int(Path(local).stat().st_size)
            remote_size = _size_of(self._stat(client, tmp))
            if remote_size != expected:
                raise FsError(
                    "FS_ERROR",
                    f"native copy size mismatch: {tmp} ({remote_size} != {expected})",
                    details={
                        "path": tmp,
                        "remote_size": remote_size,
                        "expected_size": expected,
                    },
                )
            self._promote_temp_file(client, tmp, remote)
        except Exception as exc:
            # A hang can abandon a thread that still streams the temp onto the
            # host after the caller is told the put failed, so the cleanup
            # sweeps for that late arrival.
            self._best_effort_remove(client, tmp, sweep=_is_timeout_failure(exc))
            raise

    def _try_fetch(self, client: Any, remote: str, local: str) -> bool:
        if getattr(client, "has_native_fetch", False) is True:
            fetch_fn = getattr(client, "fetch", None)
            if callable(fetch_fn):
                self._run_native_transfer(
                    lambda: fetch_fn(remote, local), what="fetch"
                )
                return True
        return False

    def _mkdir(self, client: Any, path: str) -> None:
        self._ensure_op_budget()
        client.mkdir(path)

    def _is_existing_dir(
        self, client: Any, path: str, *, attrs: Any | None = None
    ) -> bool:
        """True when *path* is a directory for ``_mkdir_p`` parent purposes.

        Real directories and reparse/symlink-to-directory chains count as
        already present so writes under ``C:\\srv\\www`` (-> ``C:\\var\\www``)
        succeed. File / other / broken-link paths return False so
        ``_mkdir_p`` raises ``ALREADY_EXISTS``. ``FsError(TIMEOUT)`` from
        stat / follow-stat / readlink is re-raised so a hang is never
        reported as "exists but not a directory". Scoped to the
        exists-as-dir branch only - list/rmtree still treat kind=link as
        leaves.
        """
        norm = _norm_win_path(path)
        if not norm:
            return False
        if attrs is None:
            try:
                attrs = self._stat(client, norm)
            except FsError as exc:
                _raise_if_timeout(exc, norm)
                return False
        kind = _kind_from_attrs(attrs)
        if kind == "dir":
            return True
        if kind != "link":
            return False

        # Prefer follow-stat when a client resolves reparse points (mirrors
        # SFTP ``stat`` vs ``lstat``). Protocol ``stat`` is often non-following
        # (kind=link for ReparsePoint); when follow still reports link or
        # fails, resolve via ``_readlink`` with the same join rules as
        # ``_resolve_final_link``.
        follow = getattr(client, "stat", None)
        if callable(follow):
            try:
                fattrs = follow(norm)
                fk = _kind_from_attrs(fattrs)
                if fk == "dir":
                    return True
                if fk != "link":
                    return False
            except Exception as exc:  # noqa: BLE001 - try readlink before giving up
                _raise_if_timeout(exc, norm)

        current = norm
        for _ in range(_MAX_SYMLINK_FOLLOW):
            target = self._readlink(client, current)
            if target is None:
                return False
            target = str(target).strip()
            if not target:
                return False
            # PowerShell Target may be multi-valued; take first token.
            if "\n" in target:
                target = target.splitlines()[0].strip()
            if not target:
                return False
            if _is_abs_win(target):
                current = _norm_win_path(target)
            else:
                parent = _parent_win(current)
                current = (
                    _norm_win_path(target)
                    if parent is None
                    else _norm_win_path(_win_sep_join(parent, target))
                )
            try:
                sattrs = self._stat(client, current)
            except FsError as exc:
                _raise_if_timeout(exc, current)
                return False
            k = _kind_from_attrs(sattrs)
            if k == "dir":
                return True
            if k != "link":
                return False
        return False

    def _mkdir_p(self, client: Any, path: str) -> None:
        if not path:
            return
        norm = _norm_win_path(path)
        # Drive root always "exists".
        if len(norm) == 3 and norm[1] == ":" and norm[2] == "\\":
            return
        try:
            attrs = self._stat(client, norm)
            if self._is_existing_dir(client, norm, attrs=attrs):
                return
            raise FsError(
                "ALREADY_EXISTS",
                f"path exists and is not a directory: {norm}",
                details={"path": norm},
            )
        except FsError as exc:
            if exc.code != "NOT_FOUND":
                raise
        parent = _parent_win(norm)
        if parent and parent != norm:
            self._mkdir_p(client, parent)
        try:
            self._mkdir(client, norm)
        except Exception as exc:
            try:
                attrs = self._stat(client, norm)
                if self._is_existing_dir(client, norm, attrs=attrs):
                    return
            except Exception as exist_exc:  # noqa: BLE001
                _raise_if_timeout(exist_exc, norm)
            raise _map_fs_error(exc, norm) from exc

    def _remove(self, client: Any, path: str) -> None:
        self._ensure_op_budget()
        client.remove(path)

    def _best_effort_remove(
        self,
        client: Any,
        path: str,
        *,
        sweep: bool = False,
    ) -> None:
        """Best-effort remote remove under its own small bounded budget.

        Error paths - mid-copy / promote / progress-copy failures, including a
        put whose whole-op budget is already spent - must still erase the temp
        they created. The whole-op deadline is therefore deliberately not
        consulted: consulting it is what leaves a spent put issuing no cleanup
        call at all and a full-payload temp on the host. Every attempt is
        bounded by the remaining cleanup budget, and the client's own deadline
        is pointed at that same budget while the call runs, so a stalled delete
        returns instead of pinning the caller.

        *sweep* retries the remove for as long as the cleanup budget lasts, so
        a temp that an abandoned transfer thread lands after the put already
        reported TIMEOUT is erased as long as it lands inside that budget. The
        loop removes and then waits, and it ends on a delete whichever way it
        leaves the budget: the closing delete is issued *after* the final wait
        - or after the delete that consumed what was left of the budget -
        instead of before the final waiting slice, so a temp that lands inside
        the budget while the sweep is waiting, or while its last delete is in
        flight, is still erased. That closing attempt carries the min-budget
        floor (never the whole-op deadline), so the cleanup stays bounded. A
        landing after the budget is out of scope by design, because waiting
        for the abandoned thread would be unbounded. Failures are swallowed:
        this is the error path.
        """
        budget = max(float(self._cleanup_timeout_s), _CLEANUP_MIN_BUDGET_S)
        deadline = time.monotonic() + budget
        # The closing attempt is issued once the budget is spent and is floored
        # like every other attempt, so the client's own binding carries that
        # floor too; the sweep's waits and periodic deletes still end at
        # *deadline*. Without it the client would refuse the closing call as
        # already spent and the final slice would go without its delete.
        prev = self._bind_cleanup_deadline(
            client,
            deadline + _CLEANUP_MIN_BUDGET_S,
            budget + _CLEANUP_MIN_BUDGET_S,
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._remove_within(client, path, remaining)
                if not sweep:
                    break
                if remaining > 0:
                    # A retry re-checks a path that was not there yet, so it
                    # waits first: the sweep spans the whole budget without
                    # hammering the host. The wait is clamped to the budget.
                    gap = min(_CLEANUP_SWEEP_GAP_S, deadline - time.monotonic())
                    if gap > 0:
                        time.sleep(gap)
                        continue
                # Sweep only: the budget is spent - the last wait exhausted it,
                # or the last delete consumed what was left of it. This closing
                # delete is what the final slice would otherwise go without, so
                # a temp the abandoned transfer landed inside the budget is
                # still erased. It carries the min-budget floor and never
                # consults the whole-op deadline, so the wait stays bounded.
                self._remove_within(client, path, _CLEANUP_MIN_BUDGET_S)
                break
        finally:
            self._restore_client_deadline(client, prev)

    def _remove_within(self, client: Any, path: str, timeout_s: float) -> None:
        """Issue one best-effort remote remove bounded by *timeout_s*.

        The caller's wait is bounded by *timeout_s* itself, so a stalled
        delete returns instead of pinning it; a failure is swallowed because
        this runs on the error path.
        """
        try:
            self._run_blocking_with_timeout(
                lambda: client.remove(path),
                timeout_s=timeout_s,
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    def _close_write_handle(self, fh: Any, path: str) -> None:
        """Flush (if present) and close a write handle; never swallow errors.

        Remote close/flush can fail after a successful write stream (disk full,
        quota, session drop). Callers must not promote temps when this raises.
        """
        primary: BaseException | None = None
        flush = getattr(fh, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as exc:  # noqa: BLE001
                primary = exc
        close = getattr(fh, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                # Prefer close as the definitive end-of-write signal.
                primary = exc
        if primary is None:
            return
        if isinstance(primary, FsError):
            raise primary
        text = str(primary).strip() or type(primary).__name__
        text = " ".join(text.split())
        if len(text) > 200:
            text = text[:197] + "..."
        raise FsError(
            "FS_ERROR",
            f"winrm write close failed: {path}: {text}",
            details={"path": path},
        ) from primary

    def _read_all_via_open(self, client: Any, path: str) -> bytes:
        """Read entire remote *path* via ``open``/``read`` (SupportsFileOpen)."""
        fh = client.open(path, "rb")
        chunks: list[bytes] = []
        try:
            read = getattr(fh, "read", None)
            if not callable(read):
                raise FsError("UNSUPPORTED", "winrm file has no read")
            while True:
                chunk = _coerce_bytes(read(DEFAULT_TRANSFER_CHUNK))
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            close = getattr(fh, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        return b"".join(chunks)

    def _promote_temp_file(self, client: Any, tmp: str, dest: str) -> None:
        """Promote a fully-written temp onto *dest* after successful close.

        Preference order:
        1. ``rename`` / ``move`` / ``replace`` when the client provides it
           (same-volume promote that moves no content; ``PypsrpFileClient``
           replaces an existing destination with the runtime's replace
           primitive and restores the prior content when that replace fails).
        2. ``write_file`` of the temp payload (a full content round trip, used
           only by clients without a rename) then remove temp.
        3. Stream via ``open`` (last resort for open-only clients).
        """
        for name in ("rename", "move", "replace"):
            fn = getattr(client, name, None)
            if callable(fn):
                fn(tmp, dest)
                return

        write_fn = getattr(client, "write_file", None)
        if callable(write_fn):
            read_fn = getattr(client, "read_file", None)
            if callable(read_fn):
                try:
                    data = _coerce_bytes(read_fn(tmp, max_bytes=None))
                except TypeError:
                    # Some clients accept only path.
                    data = _coerce_bytes(read_fn(tmp))
            else:
                data = self._read_all_via_open(client, tmp)
            write_fn(dest, data)
            self._best_effort_remove(client, tmp)
            return

        # Open-only client: load temp then single write to dest (dest opened
        # only after temp is complete so mid-stream put failures never touch
        # dest). Local source remains the recovery copy if promote fails.
        data = self._read_all_via_open(client, tmp)
        fh = client.open(dest, "wb")
        write_exc: BaseException | None = None
        try:
            write = getattr(fh, "write", None)
            if not callable(write):
                raise FsError("UNSUPPORTED", "winrm file has no write")
            write(data)
        except Exception as exc:
            write_exc = exc
        try:
            self._close_write_handle(fh, dest)
        except Exception as close_exc:
            if write_exc is None:
                write_exc = close_exc
        if write_exc is not None:
            raise write_exc
        self._best_effort_remove(client, tmp)

    def _rmdir(self, client: Any, path: str) -> None:
        self._ensure_op_budget()
        client.rmdir(path)

    def _rmtree(
        self,
        client: Any,
        path: str,
        *,
        visited: set[str] | None = None,
        depth: int = 0,
        max_depth: int = _MAX_RECURSE_DEPTH,
    ) -> None:
        """Recursively remove a remote directory tree.

        Prefer native ``SupportsRmtree.rmtree`` when the client provides it
        (production pypsrp). Otherwise fall back to listdir/stat recursion.
        Only real directories (``kind=="dir"``) are descended - files and
        reparse/link-shaped entries are removed as leaves. *visited* and
        *max_depth* guard junction/reparse cycles (same policy as recursive
        list and SFTP); depth exceed raises ``FsError(DEPTH_EXCEEDED)``
        (partial deletes may already have occurred).
        """
        if isinstance(client, SupportsRmtree):
            self._ensure_op_budget()
            client.rmtree(path)
            return
        if visited is None:
            visited = set()
        if path in visited:
            # Same-path re-entry (junction/reparse back to an ancestor).
            # Skip rather than loop; parent cleanup may still remove the
            # junction entry via best-effort remove/rmdir below.
            return
        if depth >= max_depth:
            raise FsError(
                "DEPTH_EXCEEDED",
                f"maximum recursion depth ({max_depth}) exceeded at: {path}",
                details={"path": path, "max_depth": max_depth},
            )
        visited.add(path)
        names = self._listdir(client, path)
        for name in names:
            if name in {".", ".."}:
                continue
            child = _win_sep_join(path, name)
            try:
                st = self._stat(client, child)
                if _kind_from_attrs(st) == "dir":
                    self._rmtree(
                        client,
                        child,
                        visited=visited,
                        depth=depth + 1,
                        max_depth=max_depth,
                    )
                else:
                    # file / link / other - remove entry, do not follow.
                    self._remove(client, child)
            except FsError as exc:
                # Depth / whole-op timeout must propagate; only swallow
                # path-level remove failures for best-effort cleanup of
                # stubborn children (partial tree delete may already exist).
                if exc.code in {"DEPTH_EXCEEDED", "TIMEOUT"}:
                    raise
                try:
                    self._remove(client, child)
                except Exception:  # noqa: BLE001
                    self._rmdir(client, child)
        self._rmdir(client, path)



def _rel_name_under(base: str, path: str) -> str:
    """Return *path* relative to *base* using Windows separators.

    Used by recursive ``list`` so descendant names come from absolute paths
    rather than per-level basename heuristics (which break on prefix overlap).
    When *path* is not under *base*, returns the normalized path (defensive;
    normal recursion always stays under *base*).
    """
    nb = _norm_win_path(base).rstrip("\\")
    np_ = _norm_win_path(path)
    if not nb or not np_:
        return ""
    prefix = nb + "\\"
    if np_.startswith(prefix):
        return np_[len(prefix):]
    if np_ == nb:
        return ""
    return np_


def _parent_win(path: str) -> str | None:
    norm = _norm_win_path(path)
    if not norm:
        return None
    # Drive root
    if len(norm) == 3 and norm[1] == ":" and norm[2] == "\\":
        return norm
    if norm.startswith("\\\\"):
        parts = [p for p in norm.split("\\") if p]
        if len(parts) <= 2:
            return norm
        return "\\\\" + "\\".join(parts[:-1])
    parent = norm.rsplit("\\", 1)[0]
    if len(parent) == 2 and parent[1] == ":":
        return parent + "\\"
    return parent or None


def _win_temp_path(path: str) -> str:
    """Same-directory temp path for the remote write's promote.

    Placing the temp beside *path* keeps the promote on one volume, so the
    temp is renamed into place rather than copied across volumes, and the
    promote's replace backup lands in the same directory as well.
    """
    norm = _norm_win_path(path)
    base = norm
    for sep in ("\\", "/"):
        idx = base.rfind(sep)
        if idx >= 0:
            base = base[idx + 1 :]
            break
    if not base:
        base = "file"
    parent = _parent_win(norm)
    name = mrc_tmp_name(base)
    if parent is None:
        return name
    return _win_sep_join(parent, name)


from mcp_remote_control.transport.winrm_files import (  # noqa: E402
    PypsrpFileClient as PypsrpFileClient,
    _is_http_rejection as _is_http_rejection,
    _is_link_failure as _is_link_failure,
    _op_timeout_error as _op_timeout_error,
    _ps_streams_stderr as _ps_streams_stderr,
)
