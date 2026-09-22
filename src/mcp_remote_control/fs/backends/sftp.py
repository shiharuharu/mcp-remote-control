"""SFTP filesystem backend over a minimal sync SFTP Protocol surface.

Agent-facing API remains ``fs_*``; ``via=sftp`` is optional meta only.
Clients implement :class:`~mcp_remote_control.transport.protocols.SyncSftpClient`
(asyncssh is adapted at the call site via awaitable driving). When a
factory is configured, a dead channel is dropped so the next op re-opens.
"""

from __future__ import annotations

import inspect
import os
import stat as statmod
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_remote_control.fs.atomic import mrc_tmp_name
from mcp_remote_control.fs.backends.local import (
    _copy_mode_if_exists as _copy_local_mode_if_exists,
)
from mcp_remote_control.fs.backends.local import (
    _resolve_final_link as _resolve_local_final_link,
)
from mcp_remote_control.fs.remote_path import (
    is_abs_remote as _is_abs_remote,
)
from mcp_remote_control.fs.remote_path import (
    is_root_remote,
    remote_parent,
)
from mcp_remote_control.fs.remote_path import (
    remote_join as _posix_join,
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
from mcp_remote_control.transport.async_bridge import run_coro
from mcp_remote_control.transport.protocols import SyncSftpClient

# Factory: () -> sftp client (sync object or awaitable open).
SftpFactory = Callable[[], SyncSftpClient | Any]


# Cap final-component symlink follow for atomic write/put (loop guard).
_MAX_SYMLINK_FOLLOW = 32

# Cap recursive list / rmtree descent (align WinRM ~40). Visited-set catches
# same-path cycles; depth is the backstop for ever-growing path shapes
# (self-ref junction/symlink that reappears one level deeper each time).
# Unlike WinRM (skip at cap), SFTP raises a clear FsError when depth is hit.
_MAX_RECURSE_DEPTH = 40

# Default wall-clock budget for each SFTP await on the async bridge: 60s, so MCP
# fs ops never block the FastMCP thread pool forever on a silent remote hang (no
# keepalive). Override per instance via ``SftpFs(..., timeout_s=...)``.
DEFAULT_SFTP_TIMEOUT_S: float = 60.0


def _mode_oct(mode: int) -> str:
    return format(statmod.S_IMODE(int(mode)), "04o")


def _mtime_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _kind_from_attrs(attrs: Any) -> str:
    mode = getattr(attrs, "permissions", None)
    if mode is None:
        mode = getattr(attrs, "st_mode", None)
    if mode is None:
        # Duck-type type flags (asyncssh SFTPAttrs FILEXFER_TYPE_*: 1=file, 2=dir, 3=symlink).
        if getattr(attrs, "type", None) is not None:
            t = int(attrs.type)
            if t == 2:
                return "dir"
            if t == 3:
                return "link"
            if t == 1:
                return "file"
        return "file"
    mode = int(mode)
    if statmod.S_ISDIR(mode):
        return "dir"
    if statmod.S_ISLNK(mode):
        return "link"
    if statmod.S_ISREG(mode):
        return "file"
    return "other"


def _entry_kind(kind: str) -> str:
    return {"dir": "d", "file": "f", "link": "l"}.get(kind, "o")


def _size_of(attrs: Any) -> int:
    size = getattr(attrs, "size", None)
    if size is None:
        size = getattr(attrs, "st_size", None)
    return int(size) if size is not None else 0


def _perms_of(attrs: Any) -> str | None:
    mode = getattr(attrs, "permissions", None)
    if mode is None:
        mode = getattr(attrs, "st_mode", None)
    if mode is None:
        return None
    return _mode_oct(int(mode))


def _mtime_of(attrs: Any) -> str | None:
    mtime = getattr(attrs, "mtime", None)
    if mtime is None:
        mtime = getattr(attrs, "st_mtime", None)
    return _mtime_iso(mtime)


def _map_sftp_error(exc: BaseException, path: str) -> FsError:
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
        # Keep the bridge message when present; otherwise a short default.
        if "timed out" not in low:
            text = f"sftp operation timed out: {path}"
    elif "no such file" in low or "not found" in low or name in {
        "SFTPNoSuchFile",
        "FileNotFoundError",
    }:
        code = "NOT_FOUND"
        text = f"path not found: {path}"
    elif "permission" in low or name in {"SFTPPermissionDenied", "PermissionError"}:
        code = "PERMISSION_DENIED"
        text = f"permission denied: {path}"
    elif "not a directory" in low or name == "NotADirectoryError":
        code = "NOT_A_DIR"
    elif "is a directory" in low or name == "IsADirectoryError":
        code = "IS_A_DIR"
    if len(text) > 200:
        text = text[:197] + "..."
    return FsError(code, text, details={"path": path})


def _is_local_failure(exc: BaseException, local_paths: Collection[str]) -> bool:
    """Is *exc* a failure of a call made against a local path?

    ``OSError.filename`` names the object the failing syscall was given, so a
    local source read, local destination mkdir, temp write or replace is
    recognizable by the path it carries. A remote client raises its own
    exception classes and names its own paths, which match nothing in
    *local_paths*; an exception with no filename matches nothing either, so
    the caller keeps its remote-side default.
    """
    name = getattr(exc, "filename", None)
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    return name is not None and str(name) in local_paths


def _raise_if_timeout(exc: BaseException, path: str) -> None:
    """Re-raise a hang as ``FsError(TIMEOUT)``; no-op for every other error.

    Callers that treat a failed stat/readlink as a soft miss (not-a-dir,
    stub ``..``, kind=o, rename onto the original path) must invoke this
    first so a wall-clock timeout is never disguised as those outcomes.
    """
    if isinstance(exc, FsError):
        if exc.code == "TIMEOUT":
            raise exc
        return
    mapped = _map_sftp_error(exc, path)
    if mapped.code == "TIMEOUT":
        raise mapped from exc


# Indicators that the cached SFTP/SSH channel is dead and must be re-opened.
# Duck-typed by exception class name + message so the backend stays decoupled
# from asyncssh's concrete classes (the client is injected). Mapped capability
# FsErrors are excluded.
_CHANNEL_CLOSED_NAMES = frozenset(
    {
        "ConnectionLost",
        "SFTPConnectionLost",
        "SFTPNoConnection",
        "DisconnectError",
        "ChannelClosed",
        "ChannelOpenError",
        "EOFError",
        "TransportError",
    }
)
_CHANNEL_CLOSED_MARKERS = (
    "channel closed",
    "connection lost",
    "not connected",
    "session closed",
    "sftp connection",
    "sftp session",
)


def _is_channel_closed(exc: BaseException) -> bool:
    """Heuristic: does *exc* indicate the SFTP/SSH channel is dead?

    Bridge wall-clock ``TimeoutError`` is treated the same way: a silent hang
    may leave the channel wedged, so the cached client must be dropped before
    the next op (see :meth:`SftpFs._run_maybe_async`).
    """
    if isinstance(exc, FsError):
        return False
    # Bridge / asyncio timeout: treat as dead channel for cache drop.
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
        return True
    if type(exc).__name__ in _CHANNEL_CLOSED_NAMES:
        return True
    text = str(exc).strip().lower()
    if text:
        return any(m in text for m in _CHANNEL_CLOSED_MARKERS)
    return False


class SftpFs:
    """Filesystem ops over an SFTP client (asyncssh or duck-typed).

    Parameters
    ----------
    client:
        Connected SFTP client, or ``None`` when using *factory*.
    factory:
        Callable returning a client when *client* is ``None``. Enables lazy
        open and re-connect after a detected channel failure.
    cwd / home:
        Used to absolutize relative remote paths in results.
    timeout_s:
        Wall-clock budget (seconds) for each awaitable driven by the
        backend. Defaults to :data:`DEFAULT_SFTP_TIMEOUT_S` (60s). Never
        ``None`` at runtime - omit the arg to use the default.
    op_timeout_s:
        Whole public-op wall-clock budget (seconds) shared across every
        await inside one ``list`` / ``rm`` / ``put`` / ... call. Defaults to
        the resolved *timeout_s* (same 60s) so multi-await ops cannot
        approach N times the per-await cap. Long multi-chunk transfers should
        pass a larger value (per-await cap still applies to each hang).
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        factory: SftpFactory | None = None,
        cwd: str | None = None,
        home: str | None = None,
        timeout_s: float | None = None,
        op_timeout_s: float | None = None,
    ) -> None:
        if client is None and factory is None:
            raise ValueError("SftpFs requires client= or factory=")
        self._client = client
        self._factory = factory
        self._cwd = cwd
        self._home = home
        # Wall-clock budget for every awaitable driven by this backend.
        # ``None`` -> module default (not "no deadline"); use a large float only
        # when a caller truly needs a longer single-await budget.
        self._timeout_s = (
            DEFAULT_SFTP_TIMEOUT_S if timeout_s is None else float(timeout_s)
        )
        # Whole-op budget: omit -> mirror the per-await budget above.
        # Explicit op_timeout_s wins.
        self._op_timeout_s = (
            self._timeout_s if op_timeout_s is None else float(op_timeout_s)
        )
        # Absolute monotonic deadline for the current public op, or None when
        # no public method has entered :meth:`_op_budget` (legacy single
        # await paths still use only ``_timeout_s``).
        self._op_deadline: float | None = None

    @property
    def via(self) -> str:
        return "sftp"

    @contextmanager
    def _op_budget(self) -> Iterator[None]:
        """Bind a whole-op wall-clock deadline for nested awaits.

        Public methods enter this once. Nested re-entry (recursive ``list``,
        ``rm`` -> ``stat``) keeps the outer deadline so remaining budget is
        shared across the whole agent-facing op.
        """
        prev = self._op_deadline
        if prev is None:
            self._op_deadline = time.monotonic() + max(0.0, self._op_timeout_s)
        try:
            yield
        finally:
            self._op_deadline = prev

    def _await_budget_s(self) -> float:
        """Seconds for the next await: the smaller of the per-await budget
        and what is left of the whole-op budget.
        """
        per = max(0.0, float(self._timeout_s))
        deadline = self._op_deadline
        if deadline is None:
            return per
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(per, remaining)

    def _run_maybe_async(self, result: Any) -> Any:
        """Drive awaitables on the shared permanent loop with a wall-clock budget.

        All SFTP await sites funnel here so a silent remote hang returns
        ``FsError(TIMEOUT)`` within ``self._timeout_s`` (and, when a public
        op is in flight, within the remaining whole-op budget) instead of
        blocking the calling thread forever. Non-fs callers that need
        unbounded waits use :func:`run_coro` /
        :meth:`AsyncLoopBridge.run` with ``timeout_s=None``.

        On ``TimeoutError`` the cached client (and transport SFTP cache when
        the factory is a bound ``open_sftp``) is dropped - same as
        channel-closed - so the next op re-opens instead of reusing a wedged
        channel. Invalidate runs before the exception is mapped to
        ``FsError(TIMEOUT)`` because public handlers re-raise ``FsError``
        without calling :meth:`_invalidate_on_channel_closed`.

        Exhausted whole-op remaining (no bridge wait) raises TIMEOUT without
        invalidating - the channel may still be healthy.
        """
        budget = self._await_budget_s()
        if budget <= 0:
            # Op deadline already spent across prior awaits in this public call.
            # Close un-started coroutines so callers that built them eagerly
            # (``_run_maybe_async(fn(...))``) do not leak "never awaited".
            if inspect.iscoroutine(result):
                result.close()
            raise FsError(
                "TIMEOUT",
                f"sftp operation timed out after {self._op_timeout_s}s",
                details={
                    "timeout_s": self._op_timeout_s,
                    "op_timeout_s": self._op_timeout_s,
                },
            )
        try:
            return run_coro(result, timeout_s=budget)
        except TimeoutError as exc:
            # Drop self._client + transport.invalidate_sftp (when factory-bound)
            # before mapping; FsError re-raise paths skip channel-closed hooks.
            self._invalidate_on_channel_closed(exc)
            details: dict[str, Any] = {"timeout_s": budget}
            if self._op_deadline is not None:
                details["op_timeout_s"] = self._op_timeout_s
            raise FsError(
                "TIMEOUT",
                f"sftp operation timed out after {budget}s",
                details=details,
            ) from exc

    def _sftp(self) -> Any:
        if self._client is not None:
            return self._client
        assert self._factory is not None
        client = self._run_maybe_async(self._factory())
        self._client = client
        return client

    def _invalidate_on_channel_closed(self, exc: BaseException) -> None:
        """Drop the cached client when a channel-closed or TIMEOUT is detected.

        The next op re-invokes the factory. Only applies when a factory is
        configured - an injected client cannot be recreated. No proactive
        health check; reconnect is lazy on the next op after a detected failure.

        Covers both classic channel-closed exceptions and bridge
        ``TimeoutError`` (silent hang may leave the SFTP session wedged).

        Production wires ``factory=transport.open_sftp`` (a bound method).
        That transport keeps its own ``_sftp`` cache independent of this
        backend; if we only clear ``self._client``, the next factory call
        returns the same dead client forever. When the factory owner exposes
        ``invalidate_sftp``, clear that cache too so the next open builds a
        fresh SFTP channel without reopening the SSH endpoint.
        """
        if self._factory is not None and _is_channel_closed(exc):
            self._client = None
            owner = getattr(self._factory, "__self__", None)
            inv = getattr(owner, "invalidate_sftp", None) if owner is not None else None
            if callable(inv):
                try:
                    inv()
                except Exception:  # noqa: BLE001 - best-effort transport cache drop
                    pass

    def resolve_path(self, path: str) -> str:
        """Return an absolute-looking remote path for output semantics.

        Only an empty path is refused; the spelling is kept verbatim so the
        path that reaches the server is the object the caller named (trailing
        or leading whitespace included).
        """
        text = str(path)
        if not text:
            raise FsError("INVALID_ARG", "path is empty")
        if text == "~" or text.startswith("~/"):
            home = self._home or ""
            if home:
                text = home.rstrip("/") + text[1:]
            elif text == "~":
                text = "/"
            else:
                text = text[2:]
        if _is_abs_remote(text):
            return text
        base = self._cwd or self._home or "/"
        if base.endswith("/") or base.endswith("\\"):
            return base + text
        sep = "\\" if "\\" in base and "/" not in base else "/"
        return base + sep + text

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
        abs_path = self.resolve_path(path)
        sftp = self._sftp()
        try:
            attrs = self._stat(sftp, abs_path)
            if _kind_from_attrs(attrs) != "dir":
                raise FsError(
                    "NOT_A_DIR",
                    f"not a directory: {abs_path}",
                    details={"path": abs_path},
                )
            pairs = self._scandir(sftp, abs_path)
        except FsError:
            raise
        except Exception as exc:
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, abs_path) from exc

        entries: list[ListEntry] = []
        # "." reuses the dir attrs already fetched above (no second stat).
        entries.append(
            ListEntry(
                name=".",
                kind="d",
                size=0,
                mode=_perms_of(attrs),
                mtime=_mtime_of(attrs),
                path=abs_path,
            )
        )

        if remote_parent(abs_path):
            # The row names the listed directory's parent by joining the
            # caller's own spelling with ".." - the rule the local backend's
            # rows follow - so the server resolves it (through a trailing ".."
            # and any intermediate link) instead of the text being collapsed
            # onto a path that can name a different directory.
            parent = _posix_join(abs_path, "..")
            try:
                st_par = self._stat(sftp, parent)
                entries.append(
                    ListEntry(
                        name="..",
                        kind="d",
                        size=0,
                        mode=_perms_of(st_par),
                        mtime=_mtime_of(st_par),
                        path=parent,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # TIMEOUT is a whole-op failure; other parent-stat misses
                # still stub ".." so a vanished parent is not fatal.
                _raise_if_timeout(exc, parent)
                entries.append(ListEntry(name="..", kind="d", size=0, path=parent))

        for name, child_attrs in sorted(pairs, key=lambda p: p[0]):
            if name in {".", ".."}:
                continue
            child = _posix_join(abs_path, name)
            if child_attrs is not None:
                # readdir already supplied attrs - no per-child stat needed.
                kind = _kind_from_attrs(child_attrs)
                entries.append(
                    ListEntry(
                        name=name,
                        kind=_entry_kind(kind),
                        size=0 if kind == "dir" else _size_of(child_attrs),
                        mode=_perms_of(child_attrs),
                        mtime=_mtime_of(child_attrs),
                        path=child,
                    )
                )
            else:
                # Bare names only - fall back to a per-name stat.
                try:
                    st = self._stat(sftp, child)
                    kind = _kind_from_attrs(st)
                    entries.append(
                        ListEntry(
                            name=name,
                            kind=_entry_kind(kind),
                            size=0 if kind == "dir" else _size_of(st),
                            mode=_perms_of(st),
                            mtime=_mtime_of(st),
                            path=child,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    # TIMEOUT fails the listing; other per-name misses stay
                    # kind=o so one vanished child is not a whole-op error.
                    _raise_if_timeout(exc, child)
                    self._invalidate_on_channel_closed(exc)
                    entries.append(ListEntry(name=name, kind="o", path=child))

        if recursive:
            # Walk real dirs only (kind "d"). Symlinks report kind "l" via
            # lstat/readdir attrs and are NOT descended - avoids following a
            # self-ref or dir-via-symlink into an infinite walk.
            subdirs = [
                e for e in entries if e.kind == "d" and e.name not in {".", ".."}
            ]
            # Cycle protection: visited catches same-path re-entry; depth is a
            # backstop for ever-growing path shapes (junction/symlink reappears
            # one level deeper so visited alone never matches). Cap at 40 -
            # enough for any realistic tree; SFTP raises when the cap is hit
            # (WinRM skips; Acceptance requires a clear depth error here).
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

        Reuses single-level ``list`` per subdir. Descendant names are
        ``{parent_rel}/{basename}`` so depth >=3 and prefix-overlapping
        basenames (e.g. ``sub_file`` under ``sub``) stay correct. DFS -
        each subdir's full subtree before the next sibling. Only ``kind=="d"``
        children are descended (symlinks stay leaves).
        """
        for e in subdirs:
            sub_path = e.path or _posix_join(top, e.name)
            if sub_path in visited:
                continue
            if depth >= max_depth:
                raise FsError(
                    "DEPTH_EXCEEDED",
                    f"maximum recursion depth ({max_depth}) exceeded at: {sub_path}",
                    details={"path": sub_path, "max_depth": max_depth},
                )
            visited.add(sub_path)
            # _list (not list): stay under the outer public-op deadline.
            sub = self._list(sub_path, recursive=False)
            child_dirs: list[ListEntry] = []
            for se in sub.entries:
                if se.name in {".", ".."}:
                    continue
                se_abs = se.path or _posix_join(sub_path, se.name)
                # e.name is already relative to top (basename at depth 0;
                # "a/b" deeper). Prefix keeps names rooted at *top*.
                se_name = f"{e.name}/{se.name}"
                rel = ListEntry(
                    name=se_name,
                    kind=se.kind,
                    size=se.size,
                    mode=se.mode,
                    mtime=se.mtime,
                    path=se_abs,
                )
                entries.append(rel)
                # Descend only real directories; kind "l" (symlink) is a leaf
                # even when the target is a directory.
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

    def stat(self, path: str) -> StatInfo:
        with self._op_budget():
            return self._stat_info(path)

    def _stat_info(self, path: str) -> StatInfo:
        abs_path = self.resolve_path(path)
        sftp = self._sftp()
        try:
            attrs = self._stat(sftp, abs_path)
        except FsError:
            raise
        except Exception as exc:
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, abs_path) from exc
        kind = _kind_from_attrs(attrs)
        target: str | None = None
        if kind == "link":
            target = self._readlink(sftp, abs_path)
        return StatInfo(
            path=abs_path,
            kind=kind,
            size=0 if kind == "dir" else _size_of(attrs),
            mode=_perms_of(attrs),
            mtime=_mtime_of(attrs),
            target=target,
        )

    def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> ReadResult:
        with self._op_budget():
            abs_path = self.resolve_path(path)
            sftp = self._sftp()
            limit = DEFAULT_READ_MAX_BYTES if max_bytes is None else int(max_bytes)
            if limit < 0:
                limit = DEFAULT_READ_MAX_BYTES
            try:
                # No pre-stat: opening a directory fails on the remote and maps to IS_A_DIR.
                data = self._read_bytes(sftp, abs_path, limit + 1)
            except FsError:
                raise
            except Exception as exc:
                self._invalidate_on_channel_closed(exc)
                raise _map_sftp_error(exc, abs_path) from exc
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
            abs_path = self.resolve_path(path)
            sftp = self._sftp()
            raw = content.encode(encoding) if isinstance(content, str) else content
            created = True
            try:
                try:
                    self._stat(sftp, abs_path)
                    created = False
                except Exception as exc:  # noqa: BLE001
                    # A hang is not "path missing"; fail instead of writing as new.
                    _raise_if_timeout(exc, abs_path)
                    created = True
                parent = remote_parent(abs_path)
                if parent:
                    self._mkdir_p(sftp, parent)
                self._atomic_write_bytes(sftp, abs_path, raw)
            except FsError:
                raise
            except Exception as exc:
                self._invalidate_on_channel_closed(exc)
                raise _map_sftp_error(exc, abs_path) from exc
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
            sftp = self._sftp()
            try:
                parent = remote_parent(abs_remote)
                if parent:
                    self._mkdir_p(sftp, parent)
                size = int(src.stat().st_size)
                # Always stream through temp+promote: mid-upload failure must
                # leave the final remote path untouched (even without posix_rename).
                size = self._put_with_progress(sftp, src, abs_remote, size, progress)
            except FsError:
                raise
            except Exception as exc:
                self._invalidate_on_channel_closed(exc)
                # An unreadable local source is a failure of the source, not of
                # the remote destination the upload never reached.
                failed = (
                    str(src) if _is_local_failure(exc, (str(src),)) else abs_remote
                )
                raise _map_sftp_error(exc, failed) from exc
            return TransferResult(
                path=abs_remote,
                local=str(src),
                bytes_transferred=int(size),
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
            sftp = self._sftp()
            # Local objects this get can fail at, collected as the op reaches
            # each one: the caller's destination, every directory on the way to
            # it (mkdir(parents=True) reports the level it was refused), the
            # referent a final link resolves to, and the same-directory temp
            # the bytes are staged in. A failure naming one of them is local.
            local_paths = {str(dst)}
            try:
                # _stat_info keeps the outer whole-op deadline (not a new budget).
                info = self._stat_info(abs_remote)
                if info.kind == "dir":
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory: {abs_remote}",
                        details={"path": abs_remote},
                    )
                # Final-component local symlink chain -> replace updates the
                # referent and keeps the link inode (same policy as LocalFs.get).
                # A chain the walk cannot finish (a cycle, a level it may not
                # read) is a failure of the local destination: the path its
                # exception carries is an element of that chain, which is
                # learned here and nowhere earlier, so attribute it to the
                # destination the transfer asked for.
                try:
                    dst_resolved = Path(_resolve_local_final_link(str(dst)))
                except OSError as exc:
                    raise _map_sftp_error(exc, str(dst)) from exc
                local_paths.add(str(dst_resolved))
                local_paths.update(str(parent) for parent in dst_resolved.parents)
                dst_resolved.parent.mkdir(parents=True, exist_ok=True)
                total = int(info.size)
                # Download to a local temp in the resolved parent's dir, then
                # os.replace over the referent. Mid-get failure removes the temp
                # and leaves the original destination intact.
                tmp = dst_resolved.parent / mrc_tmp_name(dst_resolved.name)
                local_paths.add(str(tmp))
                try:
                    if progress is not None:
                        size = self._get_with_progress(
                            sftp, abs_remote, tmp, total, progress
                        )
                    else:
                        get = getattr(sftp, "get", None)
                        if callable(get):
                            self._run_maybe_async(get(abs_remote, str(tmp)))
                            size = tmp.stat().st_size if tmp.is_file() else info.size
                        else:
                            data = self._read_bytes(sftp, abs_remote, None)
                            tmp.write_bytes(data)
                            size = len(data)
                    # Promoting the temp would otherwise adopt its mode and
                    # rewrite an existing destination's permissions, so copy the
                    # destination's mode onto it first. A missing destination
                    # keeps the transfer client's default - unlike LocalFs.get,
                    # which has the source inode to read from. Best effort:
                    # where the destination's filesystem has no chmod, the
                    # finished transfer must still land instead of surfacing the
                    # local failure against the readable remote path.
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
                self._invalidate_on_channel_closed(exc)
                # A refused local destination is a failure of that destination,
                # not of the readable remote source the row used to name.
                failed = (
                    str(dst) if _is_local_failure(exc, local_paths) else abs_remote
                )
                raise _map_sftp_error(exc, failed) from exc
            return TransferResult(
                path=abs_remote,
                local=str(dst),
                bytes_transferred=int(size),
            )

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo:
        with self._op_budget():
            abs_path = self.resolve_path(path)
            sftp = self._sftp()
            try:
                if parents:
                    self._mkdir_p(sftp, abs_path)
                else:
                    self._mkdir(sftp, abs_path)
            except FsError:
                raise
            except Exception as exc:
                # Already exists as a directory is success.
                try:
                    info = self._stat_info(abs_path)
                    if info.kind == "dir":
                        return info
                except FsError:
                    pass
                self._invalidate_on_channel_closed(exc)
                raise _map_sftp_error(exc, abs_path) from exc
            return self._stat_info(abs_path)

    def rm(self, path: str, *, recursive: bool = False) -> str:
        with self._op_budget():
            abs_path = self.resolve_path(path)
            sftp = self._sftp()
            try:
                info = self._stat_info(abs_path)
                if info.kind == "dir":
                    if not recursive:
                        raise FsError(
                            "IS_A_DIR",
                            f"is a directory (use recursive): {abs_path}",
                            details={"path": abs_path},
                        )
                    self._rmtree(sftp, abs_path)
                else:
                    self._remove(sftp, abs_path)
            except FsError:
                raise
            except Exception as exc:
                self._invalidate_on_channel_closed(exc)
                raise _map_sftp_error(exc, abs_path) from exc
            return abs_path

    # ------------------------------------------------------------------
    # progress-aware transfer helpers
    # ------------------------------------------------------------------

    def _put_with_progress(
        self,
        sftp: Any,
        src: Path,
        abs_remote: str,
        total: int,
        progress: ProgressCallback | None,
    ) -> int:
        """Upload with progress; always temp-then-promote when ``open`` exists.

        Streams to a same-directory temp, fail-closes the write handle, then
        promotes onto the resolved destination. The final path is never opened
        with ``wb`` until promote (after temp content is committed), so a
        mid-stream or close failure cannot destroy the only prior remote copy.
        With ``posix_rename`` the promote is atomic; without it, best-effort
        rename/copy is used (same policy as WinRM open-path put).
        """
        # report_progress is a no-op when progress is None.
        report_progress(progress, 0, total)
        open_fn = getattr(sftp, "open", None)
        if callable(open_fn):
            # Stream to temp then promote (posix_rename when available).
            dest = self._resolve_final_link(sftp, abs_remote)
            tmp = self._remote_temp_path(dest)
            done = 0
            try:
                fh = self._run_maybe_async(open_fn(tmp, "wb"))
                write_exc: BaseException | None = None
                try:
                    write = getattr(fh, "write", None)
                    if not callable(write):
                        raise FsError("UNSUPPORTED", "sftp file has no write")
                    with src.open("rb") as fsrc:
                        while True:
                            chunk = fsrc.read(DEFAULT_TRANSFER_CHUNK)
                            if not chunk:
                                break
                            self._run_maybe_async(write(chunk))
                            done += len(chunk)
                            report_progress(progress, done, total)
                except Exception as exc:
                    write_exc = exc
                try:
                    self._close_write_handle(fh, tmp)
                except Exception as close_exc:
                    if write_exc is None:
                        write_exc = close_exc
                if write_exc is not None:
                    raise write_exc
                # Promote only after close succeeds and temp size matches.
                self._ensure_remote_size(sftp, tmp, done)
                # Preserve dest mode on the temp before promote: temp is
                # created under server umask (often 0644); without this,
                # overwriting ~/.aws/credentials etc. silently demotes 0600.
                # Capture mode before promote; re-apply after copy-promote when
                # rename is unavailable so dest is not left at umask default.
                preserve_mode = self._existing_file_mode(sftp, dest)
                if preserve_mode is not None:
                    self._set_remote_mode(sftp, tmp, preserve_mode)
                self._promote_temp_file(sftp, tmp, dest)
                if preserve_mode is not None:
                    self._set_remote_mode(sftp, dest, preserve_mode)
            except Exception:
                self._best_effort_remove(sftp, tmp)
                raise
            if done != total:
                report_progress(progress, done, total if total else done)
            return done

        # Whole-file write with start/end progress only (no open surface).
        # Still temp+promote so mid-write failure cannot truncate dest.
        data = src.read_bytes()
        self._atomic_write_bytes(sftp, abs_remote, data)
        report_progress(progress, len(data), total if total else len(data))
        return len(data)

    def _get_with_progress(
        self,
        sftp: Any,
        abs_remote: str,
        dst: Path,
        total: int,
        progress: ProgressCallback,
    ) -> int:
        report_progress(progress, 0, total if total else None)
        open_fn = getattr(sftp, "open", None)
        if callable(open_fn):
            fh = self._run_maybe_async(open_fn(abs_remote, "rb"))
            done = 0
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "sftp file has no read")
                with dst.open("wb") as fdst:
                    while True:
                        chunk = self._run_maybe_async(read(DEFAULT_TRANSFER_CHUNK))
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        chunk = bytes(chunk or b"")
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
                        self._run_maybe_async(close())
                    except Exception:  # noqa: BLE001
                        pass
            if total and done != total:
                report_progress(progress, done, total)
            elif not total:
                report_progress(progress, done, done)
            return done

        data = self._read_bytes(sftp, abs_remote, None)
        dst.write_bytes(data)
        report_progress(progress, len(data), total if total else len(data))
        return len(data)

    # ------------------------------------------------------------------
    # low-level client adapters
    # ------------------------------------------------------------------

    def _stat(self, sftp: Any, path: str) -> Any:
        # Prefer lstat (symlink-aware) when present; Protocol requires stat.
        fn = getattr(sftp, "lstat", None)
        if not callable(fn):
            fn = getattr(sftp, "stat", None)
        if not callable(fn):
            raise FsError("UNSUPPORTED", "sftp client has no stat/lstat")
        try:
            return self._run_maybe_async(fn(path))
        except Exception as exc:
            # Detect a dead channel before mapping swallows the
            # original exception class (public methods only see FsError).
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, path) from exc

    def _scandir(self, sftp: Any, path: str) -> list[tuple[str, Any | None]]:
        """Return ``(name, attrs)`` pairs for directory entries.

        ``attrs`` comes from readdir when the client yields name-objects
        (asyncssh ``SFTPName``); otherwise ``None`` so the caller falls back to
        a per-name stat. Preferring readdir attrs avoids N+1 re-stat round-trips.
        """
        # asyncssh: readdir yields SFTPName (.filename/.name, .attrs).
        fn = getattr(sftp, "readdir", None)
        if callable(fn):
            raw = self._run_maybe_async(fn(path))
            out: list[tuple[str, Any | None]] = []
            for item in raw:
                n = getattr(item, "filename", None) or getattr(item, "name", None)
                if n is None:
                    n = str(item)
                out.append((str(n), getattr(item, "attrs", None)))
            return out
        # Generic listdir/list: bare strings or name-objects.
        for name in ("listdir", "list"):
            fn = getattr(sftp, name, None)
            if callable(fn):
                raw = self._run_maybe_async(fn(path))
                out = []
                for item in raw:
                    if isinstance(item, str):
                        out.append((item, None))
                        continue
                    n = getattr(item, "filename", None) or getattr(item, "name", None)
                    if n is None:
                        n = str(item)
                    out.append((str(n), getattr(item, "attrs", None)))
                return out
        raise FsError("UNSUPPORTED", "sftp client has no listdir")

    def _readlink(self, sftp: Any, path: str) -> str | None:
        """Read a symlink target; return ``None`` when unsupported or failing.

        ``FsError(TIMEOUT)`` is re-raised: a hang is not "no target" (which
        would let mkdir treat a link as a non-dir, or let atomic write
        rename over the link).
        """
        fn = getattr(sftp, "readlink", None)
        if not callable(fn):
            return None
        try:
            return str(self._run_maybe_async(fn(path)))
        except Exception as exc:  # noqa: BLE001
            _raise_if_timeout(exc, path)
            return None

    def _remote_temp_path(self, path: str) -> str:
        """Temp remote path in the same directory as *path*.

        Same-directory placement keeps ``posix_rename`` atomic (same fs).
        """
        base = path
        for sep in ("/", "\\"):
            idx = base.rfind(sep)
            if idx >= 0:
                base = base[idx + 1 :]
                break
        parent = remote_parent(path)
        suffix = mrc_tmp_name(base)
        if parent is None:
            return suffix
        return _posix_join(parent, suffix)

    def _best_effort_remove(self, sftp: Any, path: str) -> None:
        """Best-effort remote remove; swallow errors (cleanup path)."""
        for name in ("remove", "unlink"):
            fn = getattr(sftp, name, None)
            if callable(fn):
                try:
                    self._run_maybe_async(fn(path))
                except Exception:  # noqa: BLE001
                    pass
                return

    def _close_write_handle(self, fh: Any, path: str) -> None:
        """Flush (if present) and close a write handle; never swallow errors.

        Remote FXP_CLOSE can fail after a successful write stream (disk full,
        quota, NFS writeback). Callers must not rename temps when this raises.
        """
        primary: BaseException | None = None
        flush = getattr(fh, "flush", None)
        if callable(flush):
            try:
                self._run_maybe_async(flush())
            except Exception as exc:  # noqa: BLE001
                primary = exc
        close = getattr(fh, "close", None)
        if callable(close):
            try:
                self._run_maybe_async(close())
            except Exception as exc:  # noqa: BLE001
                # Prefer close as the definitive end-of-write signal.
                primary = exc
        if primary is None:
            return
        self._invalidate_on_channel_closed(primary)
        if isinstance(primary, FsError):
            raise primary
        text = str(primary).strip() or type(primary).__name__
        text = " ".join(text.split())
        if len(text) > 200:
            text = text[:197] + "..."
        raise FsError(
            "FS_ERROR",
            f"sftp write close failed: {path}: {text}",
            details={"path": path},
        ) from primary

    def _ensure_remote_size(self, sftp: Any, path: str, expected: int) -> None:
        """Fail closed when remote *path* reports a size other than *expected*.

        Used before rename so a truncated temp is never promoted to the final
        path. Clients that omit size on attrs skip the check.
        """
        try:
            attrs = self._stat(sftp, path)
        except FsError:
            raise
        except Exception as exc:
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, path) from exc
        size = getattr(attrs, "size", None)
        if size is None:
            size = getattr(attrs, "st_size", None)
        if size is None:
            return
        if int(size) != int(expected):
            raise FsError(
                "FS_ERROR",
                f"incomplete remote write: {path} size={int(size)} expected={int(expected)}",
                details={
                    "path": path,
                    "size": int(size),
                    "expected": int(expected),
                },
            )

    def _resolve_final_link(self, sftp: Any, path: str) -> str:
        """Resolve the final-component symlink chain for atomic write/put.

        Only *path* (and successive referents) are followed - parent-directory
        links are left alone, matching the local backend. Content is written to
        the final referent so the original path stays a symlink. When *path*
        is not a link or a referent is missing, return the current path so
        callers keep existing behavior. When the current path is a known
        link and ``readlink`` is unavailable or fails, raise - never promote
        a temp onto the link entry. Caps depth at ``_MAX_SYMLINK_FOLLOW``.

        The stored target is used verbatim (only an empty target is an
        error): whitespace is part of the name it points at.
        """
        current = path
        for _ in range(_MAX_SYMLINK_FOLLOW):
            try:
                attrs = self._stat(sftp, current)
            except FsError as exc:
                if exc.code == "NOT_FOUND":
                    # Dangling or new path - write/rename here.
                    return current
                raise
            if _kind_from_attrs(attrs) != "link":
                return current
            target = self._readlink(sftp, current)
            if target is None:
                raise FsError(
                    "FS_ERROR",
                    f"cannot resolve symbolic link: {current}",
                    details={"path": current},
                )
            target = str(target)
            if not target:
                raise FsError(
                    "FS_ERROR",
                    f"cannot resolve symbolic link: {current}",
                    details={"path": current},
                )
            if _is_abs_remote(target):
                current = target
            else:
                parent = remote_parent(current)
                current = target if parent is None else _posix_join(parent, target)
        raise FsError(
            "FS_ERROR",
            f"too many symbolic links: {path}",
            details={"path": path},
        )

    def _existing_file_mode(self, sftp: Any, path: str) -> int | None:
        """Return permission bits of an existing regular file, else ``None``.

        Used before atomic promote so a restrictive destination (``0o600``)
        is not replaced by a temp created under the server umask (``0o644``).
        Missing path / non-file / attrs without mode -> ``None`` (leave umask).
        A dest-stat hang is re-raised as ``TIMEOUT`` so a restrictive file is
        never replaced by a temp at the server umask default.
        """
        try:
            attrs = self._stat(sftp, path)
        except FsError as exc:
            _raise_if_timeout(exc, path)
            return None
        except Exception as exc:  # noqa: BLE001
            _raise_if_timeout(exc, path)
            return None
        if _kind_from_attrs(attrs) != "file":
            return None
        mode = getattr(attrs, "permissions", None)
        if mode is None:
            mode = getattr(attrs, "st_mode", None)
        if mode is None:
            return None
        return int(statmod.S_IMODE(int(mode)))

    def _set_remote_mode(self, sftp: Any, path: str, mode: int) -> None:
        """Apply permission bits to a remote path via chmod or setstat.

        Prefer ``chmod`` (asyncssh / OpenSSH sftp-server). Fall back to
        ``setstat`` with a permissions-only attrs object for duck-typed
        clients. When neither is available the call is a no-op (cannot
        preserve mode on that client).
        """
        imode = int(statmod.S_IMODE(int(mode)))
        chmod = getattr(sftp, "chmod", None)
        if callable(chmod):
            self._run_maybe_async(chmod(path, imode))
            return
        setstat = getattr(sftp, "setstat", None)
        if callable(setstat):
            # Minimal attrs duck-type: asyncssh SFTPAttrs uses .permissions.
            class _PermAttrs:
                permissions = imode

            self._run_maybe_async(setstat(path, _PermAttrs()))
            return

    def _promote_temp_file(self, sftp: Any, tmp: str, dest: str) -> None:
        """Promote a fully-written temp onto *dest* after successful close.

        Preference order (atomic -> best-effort):
        1. ``posix_rename`` when callable (atomic same-fs replace).
        2. ``rename`` when callable (many SFTP servers replace on same dir).
        3. Read temp then ``_write_bytes`` to *dest* and remove temp.

        Dest is only opened for write after temp content is complete, so a
        mid-stream put/write failure never truncates an existing remote copy.
        Callers own temp cleanup on failure; on successful copy-promote the
        temp is best-effort removed here.
        """
        for name in ("posix_rename", "rename"):
            fn = getattr(sftp, name, None)
            if callable(fn):
                self._run_maybe_async(fn(tmp, dest))
                return

        # Copy promote: open dest only after temp is fully committed.
        data = self._read_bytes(sftp, tmp, None)
        self._write_bytes(sftp, dest, data)
        self._best_effort_remove(sftp, tmp)

    def _atomic_write_bytes(self, sftp: Any, path: str, data: bytes) -> None:
        """Write *data* to *path* via temp then promote (never wb-first on final).

        Streams to a same-directory temp, then promotes over the final path.
        If *path* is a symlink, the final-component chain is resolved so the
        *target* is updated and the symlink directory entry is preserved
        (same policy as the local backend). Existing destination mode is
        copied onto the temp before promote so a restrictive file (e.g.
        ``0o600`` credentials) is not demoted to the server umask default.
        On write/promote failure the temp is best-effort removed so the final
        path is never left truncated by a mid-write. Uses ``posix_rename`` when
        available; otherwise best-effort ``rename``/copy promote.
        """
        dest = self._resolve_final_link(sftp, path)
        tmp = self._remote_temp_path(dest)
        try:
            # Capture mode before writing temp (dest may disappear mid-op).
            preserve_mode = self._existing_file_mode(sftp, dest)
            self._write_bytes(sftp, tmp, data)
            # Close already ran inside _write_bytes; verify temp before promote.
            self._ensure_remote_size(sftp, tmp, len(data))
            if preserve_mode is not None:
                self._set_remote_mode(sftp, tmp, preserve_mode)
            self._promote_temp_file(sftp, tmp, dest)
            # Re-apply after promote so copy-promote (no rename) still lands
            # with the original restrictive mode, not umask default.
            if preserve_mode is not None:
                self._set_remote_mode(sftp, dest, preserve_mode)
        except Exception:
            self._best_effort_remove(sftp, tmp)
            raise

    def _read_bytes(self, sftp: Any, path: str, max_n: int | None) -> bytes:
        open_fn = getattr(sftp, "open", None)
        if callable(open_fn):
            fh = self._run_maybe_async(open_fn(path, "rb"))
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "sftp file has no read")
                if max_n is None:
                    data = self._run_maybe_async(read())
                else:
                    data = self._run_maybe_async(read(max_n))
                if isinstance(data, str):
                    data = data.encode("utf-8")
                return bytes(data or b"")
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        self._run_maybe_async(close())
                    except Exception:  # noqa: BLE001
                        pass
        # Clients may expose read_file(path) -> bytes.
        rf = getattr(sftp, "read_file", None)
        if callable(rf):
            data = self._run_maybe_async(rf(path))
            if isinstance(data, str):
                data = data.encode("utf-8")
            data = bytes(data or b"")
            if max_n is not None:
                return data[:max_n]
            return data
        raise FsError("UNSUPPORTED", "sftp client cannot read files")

    def _write_bytes(self, sftp: Any, path: str, data: bytes) -> None:
        open_fn = getattr(sftp, "open", None)
        if callable(open_fn):
            fh = self._run_maybe_async(open_fn(path, "wb"))
            write_exc: BaseException | None = None
            try:
                write = getattr(fh, "write", None)
                if not callable(write):
                    raise FsError("UNSUPPORTED", "sftp file has no write")
                self._run_maybe_async(write(data))
            except Exception as exc:
                write_exc = exc
            try:
                self._close_write_handle(fh, path)
            except Exception as close_exc:
                if write_exc is None:
                    write_exc = close_exc
            if write_exc is not None:
                raise write_exc
            return
        wf = getattr(sftp, "write_file", None)
        if callable(wf):
            self._run_maybe_async(wf(path, data))
            return
        raise FsError("UNSUPPORTED", "sftp client cannot write files")

    def _mkdir(self, sftp: Any, path: str) -> None:
        self._run_maybe_async(sftp.mkdir(path))

    def _is_existing_dir(
        self, sftp: Any, path: str, *, attrs: Any | None = None
    ) -> bool:
        """True when *path* is a directory for ``_mkdir_p`` parent purposes.

        Real directories and symlink-to-directory chains count as already
        present so writes under ``/srv/www`` (-> ``/var/www``) succeed.
        File / other / broken-link paths return False so ``_mkdir_p`` raises
        ``ALREADY_EXISTS``. ``FsError(TIMEOUT)`` from stat / follow-stat /
        readlink is re-raised so a hang is never reported as "exists but
        not a directory". Scoped to the exists-as-dir branch only -
        list/rmtree still treat kind=link as leaves.
        """
        if attrs is None:
            try:
                attrs = self._stat(sftp, path)
            except FsError as exc:
                _raise_if_timeout(exc, path)
                return False
        kind = _kind_from_attrs(attrs)
        if kind == "dir":
            return True
        if kind != "link":
            return False

        # Prefer client follow-stat (asyncssh ``stat`` follows; ``lstat`` does
        # not). When follow still reports link or is unavailable, resolve via
        # readlink using the same join rules as ``_resolve_final_link``.
        follow = getattr(sftp, "stat", None)
        if callable(follow):
            try:
                fattrs = self._run_maybe_async(follow(path))
                fk = _kind_from_attrs(fattrs)
                if fk == "dir":
                    return True
                if fk != "link":
                    return False
            except Exception as exc:  # noqa: BLE001
                _raise_if_timeout(exc, path)
                self._invalidate_on_channel_closed(exc)
                # Broken follow-stat - try readlink before giving up.

        current = path
        for _ in range(_MAX_SYMLINK_FOLLOW):
            target = self._readlink(sftp, current)
            if target is None:
                return False
            # Verbatim target (only empty is unusable): the referent's name is
            # what the link stores, whitespace included.
            target = str(target)
            if not target:
                return False
            if _is_abs_remote(target):
                current = target
            else:
                parent = remote_parent(current)
                current = (
                    target if parent is None else _posix_join(parent, target)
                )
            try:
                sattrs = self._stat(sftp, current)
            except FsError as exc:
                _raise_if_timeout(exc, current)
                return False
            k = _kind_from_attrs(sattrs)
            if k == "dir":
                return True
            if k != "link":
                return False
        return False

    def _mkdir_p(self, sftp: Any, path: str) -> None:
        if not path or is_root_remote(path):
            return
        try:
            attrs = self._stat(sftp, path)
            if self._is_existing_dir(sftp, path, attrs=attrs):
                return
            raise FsError(
                "ALREADY_EXISTS",
                f"path exists and is not a directory: {path}",
                details={"path": path},
            )
        except FsError as exc:
            if exc.code != "NOT_FOUND":
                raise
        parent = remote_parent(path)
        if parent and parent != path:
            self._mkdir_p(sftp, parent)
        try:
            self._mkdir(sftp, path)
        except Exception as exc:
            try:
                attrs = self._stat(sftp, path)
                if self._is_existing_dir(sftp, path, attrs=attrs):
                    return
            except Exception as exist_exc:  # noqa: BLE001
                _raise_if_timeout(exist_exc, path)
            raise _map_sftp_error(exc, path) from exc

    def _remove(self, sftp: Any, path: str) -> None:
        fn = getattr(sftp, "remove", None)
        if not callable(fn):
            fn = getattr(sftp, "unlink", None)
        if not callable(fn):
            raise FsError("UNSUPPORTED", "sftp client has no remove")
        self._run_maybe_async(fn(path))

    def _rmdir(self, sftp: Any, path: str) -> None:
        self._run_maybe_async(sftp.rmdir(path))

    def _rmtree(
        self,
        sftp: Any,
        path: str,
        *,
        visited: set[str] | None = None,
        depth: int = 0,
        max_depth: int = _MAX_RECURSE_DEPTH,
    ) -> None:
        """Recursively remove a remote directory tree.

        Prefer readdir attrs for child kind (same N+1 avoidance as list).
        When attrs is None, fall back to a per-name lstat. Only real
        directories (``kind=="dir"``) are descended - symlinks are unlinked
        as leaves so a self-ref / dir-via-symlink cannot loop. *visited*
        and *max_depth* guard junction-style cycles; depth exceed raises a
        clear ``FsError`` (partial deletes may already have occurred).
        """
        if visited is None:
            visited = set()
        if path in visited:
            return
        if depth >= max_depth:
            raise FsError(
                "DEPTH_EXCEEDED",
                f"maximum recursion depth ({max_depth}) exceeded at: {path}",
                details={"path": path, "max_depth": max_depth},
            )
        visited.add(path)
        pairs = self._scandir(sftp, path)
        for name, attrs in pairs:
            if name in {".", ".."}:
                continue
            child = _posix_join(path, name)
            kind: str | None = _kind_from_attrs(attrs) if attrs is not None else None
            try:
                if kind is None:
                    # _stat prefers lstat - symlink stays kind "link", not dir.
                    st = self._stat(sftp, child)
                    kind = _kind_from_attrs(st)
                if kind == "dir":
                    self._rmtree(
                        sftp,
                        child,
                        visited=visited,
                        depth=depth + 1,
                        max_depth=max_depth,
                    )
                else:
                    # file / link / other - remove entry, do not follow.
                    self._remove(sftp, child)
            except FsError as exc:
                # Depth / whole-op timeout must propagate; only swallow
                # path-level remove failures for best-effort cleanup of
                # stubborn children (partial tree delete may already exist).
                if exc.code in {"DEPTH_EXCEEDED", "TIMEOUT"}:
                    raise
                try:
                    self._remove(sftp, child)
                except Exception:  # noqa: BLE001
                    self._rmdir(sftp, child)
        self._rmdir(sftp, path)
