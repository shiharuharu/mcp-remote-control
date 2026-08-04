"""SFTP filesystem backend over a minimal sync SFTP Protocol surface.

Agent-facing API remains ``fs_*``; ``via=sftp`` is optional meta only.
Clients implement :class:`~mcp_remote_control.transport.protocols.SyncSftpClient`
(asyncssh is adapted at the call site via awaitable driving). When a
factory is configured, a dead channel is dropped so the next op re-opens.
"""

from __future__ import annotations

import os
import stat as statmod
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def _run_maybe_async(result: Any) -> Any:
    """Drive awaitables on the shared permanent loop (not ``asyncio.run``)."""
    return run_coro(result)


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
    if "no such file" in low or "not found" in low or name in {
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
    """Heuristic: does *exc* indicate the SFTP/SSH channel is dead?"""
    if isinstance(exc, FsError):
        return False
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
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        factory: SftpFactory | None = None,
        cwd: str | None = None,
        home: str | None = None,
    ) -> None:
        if client is None and factory is None:
            raise ValueError("SftpFs requires client= or factory=")
        self._client = client
        self._factory = factory
        self._cwd = cwd
        self._home = home

    @property
    def via(self) -> str:
        return "sftp"

    def _sftp(self) -> Any:
        if self._client is not None:
            return self._client
        assert self._factory is not None
        client = _run_maybe_async(self._factory())
        self._client = client
        return client

    def _invalidate_on_channel_closed(self, exc: BaseException) -> None:
        """Drop the cached client when a channel-closed error is detected.

        The next op re-invokes the factory. Only applies when a factory is
        configured — an injected client cannot be recreated. No proactive
        health check; reconnect is lazy on the next op after a detected failure.
        """
        if self._factory is not None and _is_channel_closed(exc):
            self._client = None

    def resolve_path(self, path: str) -> str:
        """Return an absolute-looking remote path for output semantics."""
        text = str(path).strip()
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

        parent = remote_parent(abs_path)
        if parent:
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
            except Exception:  # noqa: BLE001
                entries.append(ListEntry(name="..", kind="d", size=0, path=parent))

        for name, child_attrs in sorted(pairs, key=lambda p: p[0]):
            if name in {".", ".."}:
                continue
            child = _posix_join(abs_path, name)
            if child_attrs is not None:
                # readdir already supplied attrs — no per-child stat needed.
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
                # Bare names only — fall back to a per-name stat.
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
                    self._invalidate_on_channel_closed(exc)
                    entries.append(ListEntry(name=name, kind="o", path=child))

        if recursive:
            # Walk subdirs discovered above. Nested list returns names relative
            # to each subdir; always prefix with the subdir name so the path is
            # relative to abs_path (prefix-by-basename would corrupt names that
            # share a parent basename, e.g. sub_file under sub).
            subdirs = [e for e in entries if e.kind == "d" and e.name not in {".", ".."}]
            for e in subdirs:
                sub = self.list(e.path or _posix_join(abs_path, e.name), recursive=True)
                for se in sub.entries:
                    if se.name in {".", ".."}:
                        continue
                    se_name = f"{e.name}/{se.name}"
                    entries.append(
                        ListEntry(
                            name=se_name,
                            kind=se.kind,
                            size=se.size,
                            mode=se.mode,
                            mtime=se.mtime,
                            path=se.path,
                        )
                    )

        return ListResult(path=abs_path, entries=entries)

    def stat(self, path: str) -> StatInfo:
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
        abs_path = self.resolve_path(path)
        sftp = self._sftp()
        raw = content.encode(encoding) if isinstance(content, str) else content
        created = True
        try:
            try:
                self._stat(sftp, abs_path)
                created = False
            except Exception:  # noqa: BLE001
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
            # Always stream through the atomic-aware path: when posix_rename is
            # available, upload to a temp then rename so a mid-upload failure
            # leaves the final remote path untouched.
            size = self._put_with_progress(sftp, src, abs_remote, size, progress)
        except FsError:
            raise
        except Exception as exc:
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, abs_remote) from exc
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
        abs_remote = self.resolve_path(remote_path)
        dst = Path(str(local_path)).expanduser()
        if not dst.is_absolute():
            dst = (Path.cwd() / dst).resolve()
        sftp = self._sftp()
        try:
            info = self.stat(abs_remote)
            if info.kind == "dir":
                raise FsError(
                    "IS_A_DIR",
                    f"is a directory: {abs_remote}",
                    details={"path": abs_remote},
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            total = int(info.size)
            # Download to a local temp in dst.parent, then os.replace over dst.
            # A mid-get failure removes the temp and leaves the original dst.
            tmp = (
                dst.parent
                / f".{dst.name}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                if progress is not None:
                    size = self._get_with_progress(
                        sftp, abs_remote, tmp, total, progress
                    )
                else:
                    get = getattr(sftp, "get", None)
                    if callable(get):
                        _run_maybe_async(get(abs_remote, str(tmp)))
                        size = tmp.stat().st_size if tmp.is_file() else info.size
                    else:
                        data = self._read_bytes(sftp, abs_remote, None)
                        tmp.write_bytes(data)
                        size = len(data)
                os.replace(tmp, dst)
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
            raise _map_sftp_error(exc, abs_remote) from exc
        return TransferResult(
            path=abs_remote,
            local=str(dst),
            bytes_transferred=int(size),
            direction="get",
        )

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo:
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
                info = self.stat(abs_path)
                if info.kind == "dir":
                    return info
            except FsError:
                pass
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, abs_path) from exc
        return self.stat(abs_path)

    def rm(self, path: str, *, recursive: bool = False) -> str:
        abs_path = self.resolve_path(path)
        sftp = self._sftp()
        try:
            info = self.stat(abs_path)
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
        # report_progress is a no-op when progress is None.
        report_progress(progress, 0, total)
        open_fn = getattr(sftp, "open", None)
        rename = getattr(sftp, "posix_rename", None)
        if callable(open_fn) and callable(rename):
            # Atomic: stream to a same-directory temp, then rename onto the
            # resolved target so a symlink destination stays a link.
            dest = self._resolve_final_link(sftp, abs_remote)
            tmp = self._remote_temp_path(dest)
            done = 0
            try:
                fh = _run_maybe_async(open_fn(tmp, "wb"))
                try:
                    write = getattr(fh, "write", None)
                    if not callable(write):
                        raise FsError("UNSUPPORTED", "sftp file has no write")
                    with src.open("rb") as fsrc:
                        while True:
                            chunk = fsrc.read(DEFAULT_TRANSFER_CHUNK)
                            if not chunk:
                                break
                            _run_maybe_async(write(chunk))
                            done += len(chunk)
                            report_progress(progress, done, total)
                finally:
                    close = getattr(fh, "close", None)
                    if callable(close):
                        try:
                            _run_maybe_async(close())
                        except Exception:  # noqa: BLE001
                            pass
                _run_maybe_async(rename(tmp, dest))
            except Exception:
                self._best_effort_remove(sftp, tmp)
                raise
            if done != total:
                report_progress(progress, done, total if total else done)
            return done

        if callable(open_fn):
            # No posix_rename — stream directly to the final path (non-atomic).
            fh = _run_maybe_async(open_fn(abs_remote, "wb"))
            done = 0
            try:
                write = getattr(fh, "write", None)
                if not callable(write):
                    raise FsError("UNSUPPORTED", "sftp file has no write")
                with src.open("rb") as fsrc:
                    while True:
                        chunk = fsrc.read(DEFAULT_TRANSFER_CHUNK)
                        if not chunk:
                            break
                        _run_maybe_async(write(chunk))
                        done += len(chunk)
                        report_progress(progress, done, total)
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        _run_maybe_async(close())
                    except Exception:  # noqa: BLE001
                        pass
            if done != total:
                report_progress(progress, done, total if total else done)
            return done

        # Whole-file write with start/end progress only.
        data = src.read_bytes()
        self._write_bytes(sftp, abs_remote, data)
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
            fh = _run_maybe_async(open_fn(abs_remote, "rb"))
            done = 0
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "sftp file has no read")
                with dst.open("wb") as fdst:
                    while True:
                        chunk = _run_maybe_async(read(DEFAULT_TRANSFER_CHUNK))
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
                        _run_maybe_async(close())
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
            return _run_maybe_async(fn(path))
        except Exception as exc:
            # Detect a dead channel before mapping swallows the
            # original exception class (public methods only see FsError).
            self._invalidate_on_channel_closed(exc)
            raise _map_sftp_error(exc, path) from exc

    def _listdir(self, sftp: Any, path: str) -> list[str]:
        """Return entry names only (used by ``_rmtree``); see ``_scandir`` for attrs."""
        return [name for name, _ in self._scandir(sftp, path)]

    def _scandir(self, sftp: Any, path: str) -> list[tuple[str, Any | None]]:
        """Return ``(name, attrs)`` pairs for directory entries.

        ``attrs`` comes from readdir when the client yields name-objects
        (asyncssh ``SFTPName``); otherwise ``None`` so the caller falls back to
        a per-name stat. Preferring readdir attrs avoids N+1 re-stat round-trips.
        """
        # asyncssh: readdir yields SFTPName (.filename/.name, .attrs).
        fn = getattr(sftp, "readdir", None)
        if callable(fn):
            raw = _run_maybe_async(fn(path))
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
                raw = _run_maybe_async(fn(path))
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
        """Read a symlink target; return ``None`` when unsupported or failing."""
        fn = getattr(sftp, "readlink", None)
        if not callable(fn):
            return None
        try:
            return str(_run_maybe_async(fn(path)))
        except Exception:  # noqa: BLE001
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
        suffix = f".{base}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
        if parent is None:
            return suffix
        return _posix_join(parent, suffix)

    def _best_effort_remove(self, sftp: Any, path: str) -> None:
        """Best-effort remote remove; swallow errors (cleanup path)."""
        for name in ("remove", "unlink"):
            fn = getattr(sftp, name, None)
            if callable(fn):
                try:
                    _run_maybe_async(fn(path))
                except Exception:  # noqa: BLE001
                    pass
                return

    def _resolve_final_link(self, sftp: Any, path: str) -> str:
        """Resolve the final-component symlink chain for atomic write/put.

        Only *path* (and successive referents) are followed — parent-directory
        links are left alone, matching the local backend. Content is written to
        the final referent so the original path stays a symlink. When *path*
        is not a link, ``readlink`` is unavailable, or a link target is
        missing, return the current path so callers keep existing behavior.
        Caps depth at ``_MAX_SYMLINK_FOLLOW`` to avoid cycles.
        """
        current = path
        for _ in range(_MAX_SYMLINK_FOLLOW):
            try:
                attrs = self._stat(sftp, current)
            except FsError as exc:
                if exc.code == "NOT_FOUND":
                    # Dangling or new path — write/rename here.
                    return current
                raise
            if _kind_from_attrs(attrs) != "link":
                return current
            target = self._readlink(sftp, current)
            if target is None:
                # Cannot resolve — rename onto the original path as before.
                return path
            target = str(target).strip()
            if not target:
                return path
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

    def _atomic_write_bytes(self, sftp: Any, path: str, data: bytes) -> None:
        """Write *data* to *path* atomically when ``posix_rename`` is available.

        Streams to a same-directory temp, then renames over the final path.
        If *path* is a symlink, the final-component chain is resolved so the
        *target* is updated and the symlink directory entry is preserved
        (same policy as the local backend). On write/rename failure the temp
        is best-effort removed so the final path is never left truncated.
        Falls back to a direct non-atomic write when the client lacks
        ``posix_rename``.
        """
        rename = getattr(sftp, "posix_rename", None)
        if not callable(rename):
            self._write_bytes(sftp, path, data)
            return
        dest = self._resolve_final_link(sftp, path)
        tmp = self._remote_temp_path(dest)
        try:
            self._write_bytes(sftp, tmp, data)
            _run_maybe_async(rename(tmp, dest))
        except Exception:
            self._best_effort_remove(sftp, tmp)
            raise

    def _read_bytes(self, sftp: Any, path: str, max_n: int | None) -> bytes:
        open_fn = getattr(sftp, "open", None)
        if callable(open_fn):
            fh = _run_maybe_async(open_fn(path, "rb"))
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "sftp file has no read")
                if max_n is None:
                    data = _run_maybe_async(read())
                else:
                    data = _run_maybe_async(read(max_n))
                if isinstance(data, str):
                    data = data.encode("utf-8")
                return bytes(data or b"")
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        _run_maybe_async(close())
                    except Exception:  # noqa: BLE001
                        pass
        # Clients may expose read_file(path) -> bytes.
        rf = getattr(sftp, "read_file", None)
        if callable(rf):
            data = _run_maybe_async(rf(path))
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
            fh = _run_maybe_async(open_fn(path, "wb"))
            try:
                write = getattr(fh, "write", None)
                if not callable(write):
                    raise FsError("UNSUPPORTED", "sftp file has no write")
                _run_maybe_async(write(data))
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        _run_maybe_async(close())
                    except Exception:  # noqa: BLE001
                        pass
            return
        wf = getattr(sftp, "write_file", None)
        if callable(wf):
            _run_maybe_async(wf(path, data))
            return
        raise FsError("UNSUPPORTED", "sftp client cannot write files")

    def _mkdir(self, sftp: Any, path: str) -> None:
        _run_maybe_async(sftp.mkdir(path))

    def _mkdir_p(self, sftp: Any, path: str) -> None:
        if not path or is_root_remote(path):
            return
        try:
            attrs = self._stat(sftp, path)
            kind = _kind_from_attrs(attrs)
            if kind == "dir":
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
                if _kind_from_attrs(attrs) == "dir":
                    return
            except Exception:  # noqa: BLE001
                pass
            raise _map_sftp_error(exc, path) from exc

    def _remove(self, sftp: Any, path: str) -> None:
        fn = getattr(sftp, "remove", None)
        if not callable(fn):
            fn = getattr(sftp, "unlink", None)
        if not callable(fn):
            raise FsError("UNSUPPORTED", "sftp client has no remove")
        _run_maybe_async(fn(path))

    def _rmdir(self, sftp: Any, path: str) -> None:
        _run_maybe_async(sftp.rmdir(path))

    def _rmtree(self, sftp: Any, path: str) -> None:
        # Prefer readdir attrs for child kind (same N+1 avoidance as list).
        # When attrs is None, fall back to a per-name stat.
        pairs = self._scandir(sftp, path)
        for name, attrs in pairs:
            if name in {".", ".."}:
                continue
            child = _posix_join(path, name)
            kind: str | None = _kind_from_attrs(attrs) if attrs is not None else None
            try:
                if kind is None:
                    st = self._stat(sftp, child)
                    kind = _kind_from_attrs(st)
                if kind == "dir":
                    self._rmtree(sftp, child)
                else:
                    self._remove(sftp, child)
            except FsError:
                # Best-effort: try remove then rmdir.
                try:
                    self._remove(sftp, child)
                except Exception:  # noqa: BLE001
                    self._rmdir(sftp, child)
        self._rmdir(sftp, path)
