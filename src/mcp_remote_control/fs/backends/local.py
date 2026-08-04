"""Local filesystem backend (process host via pathlib/os).

Safety invariants:

- Path expansion is lexical (``abspath`` / join); destructive ops use
  ``lstat`` / ``unlink`` / ``readlink`` so symlinks are not followed into
  unexpected targets. ``rm`` on a symlink removes the link itself, never the
  referent.
- Writes and copies are atomic (temp file + fsync + ``os.replace``) and
  preserve the destination mode when replacing an existing file.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as statmod
import threading
from datetime import UTC, datetime
from pathlib import Path

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


def _mode_oct(mode: int) -> str:
    return format(statmod.S_IMODE(mode), "04o")


def _mtime_iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _kind_from_stat(st: os.stat_result) -> str:
    if statmod.S_ISDIR(st.st_mode):
        return "dir"
    if statmod.S_ISLNK(st.st_mode):
        return "link"
    if statmod.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _entry_kind(st: os.stat_result) -> str:
    k = _kind_from_stat(st)
    return {"dir": "d", "file": "f", "link": "l"}.get(k, "o")


def _expand_abs(raw: str, *, base: str | None = None) -> str:
    text = str(raw).strip()
    if not text:
        raise FsError("INVALID_ARG", "path is empty")
    p = Path(text).expanduser()
    if not p.is_absolute():
        root = base if base else os.getcwd()
        # Lexical normalize only — do not resolve symlinks.
        return os.path.abspath(os.path.join(root, str(p)))
    # Absolute: same lexical normalize so lstat/unlink/readlink see the
    # literal path, not a followed target.
    return os.path.abspath(str(p))


def _map_os_error(exc: OSError, path: str) -> FsError:
    if isinstance(exc, FileNotFoundError):
        return FsError("NOT_FOUND", f"path not found: {path}", details={"path": path})
    if isinstance(exc, PermissionError):
        return FsError(
            "PERMISSION_DENIED",
            f"permission denied: {path}",
            details={"path": path},
        )
    if isinstance(exc, NotADirectoryError):
        return FsError(
            "NOT_A_DIR",
            f"not a directory: {path}",
            details={"path": path},
        )
    if isinstance(exc, IsADirectoryError):
        return FsError(
            "IS_A_DIR",
            f"is a directory: {path}",
            details={"path": path},
        )
    errno = getattr(exc, "errno", None)
    msg = str(exc).strip() or type(exc).__name__
    msg = " ".join(msg.split())
    if len(msg) > 200:
        msg = msg[:197] + "..."
    return FsError(
        "FS_ERROR",
        msg,
        details={"path": path, "errno": errno},
    )


class LocalFs:
    """Process-local filesystem operations."""

    def __init__(self, *, cwd: str | None = None) -> None:
        self._cwd = cwd

    @property
    def via(self) -> str:
        return "local"

    def _abs(self, path: str) -> str:
        return _expand_abs(path, base=self._cwd)

    def list(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> ListResult:
        abs_path = self._abs(path)
        p = Path(abs_path)
        if not p.exists():
            raise FsError(
                "NOT_FOUND",
                f"path not found: {abs_path}",
                details={"path": abs_path},
            )
        if not p.is_dir():
            raise FsError(
                "NOT_A_DIR",
                f"not a directory: {abs_path}",
                details={"path": abs_path},
            )
        entries: list[ListEntry] = []
        try:
            if recursive:
                for root, dirnames, filenames in os.walk(abs_path):
                    root_p = Path(root)
                    for name in sorted(dirnames):
                        entries.append(
                            self._entry_for(root_p / name, base=abs_path, relative=True)
                        )
                    for name in sorted(filenames):
                        entries.append(
                            self._entry_for(root_p / name, base=abs_path, relative=True)
                        )
            else:
                # Include "." and ".." so agents can navigate without a second call.
                for name in (".", ".."):
                    target = p if name == "." else p.parent
                    try:
                        st = target.lstat()
                    except OSError:
                        continue
                    entries.append(
                        ListEntry(
                            name=name,
                            kind=_entry_kind(st),
                            size=0 if statmod.S_ISDIR(st.st_mode) else int(st.st_size),
                            mode=_mode_oct(st.st_mode),
                            mtime=_mtime_iso(st.st_mtime),
                            path=os.path.abspath(str(target)),
                        )
                    )
                for child in sorted(p.iterdir(), key=lambda c: c.name):
                    entries.append(self._entry_for(child, base=abs_path, relative=False))
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        return ListResult(path=abs_path, entries=entries)

    def _entry_for(
        self, child: Path, *, base: str, relative: bool
    ) -> ListEntry:
        try:
            st = child.lstat()
        except OSError as exc:
            raise _map_os_error(exc, str(child)) from exc
        if relative:
            try:
                name = str(child.relative_to(base))
            except ValueError:
                name = child.name
        else:
            name = child.name
        return ListEntry(
            name=name,
            kind=_entry_kind(st),
            size=0 if statmod.S_ISDIR(st.st_mode) else int(st.st_size),
            mode=_mode_oct(st.st_mode),
            mtime=_mtime_iso(st.st_mtime),
            path=str(child),
        )

    def stat(self, path: str) -> StatInfo:
        abs_path = self._abs(path)
        p = Path(abs_path)
        try:
            st = p.lstat()
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        kind = _kind_from_stat(st)
        target = None
        if kind == "link":
            try:
                target = os.readlink(abs_path)
            except OSError:
                target = None
        size = 0 if kind == "dir" else int(st.st_size)
        return StatInfo(
            path=abs_path,
            kind=kind,
            size=size,
            mode=_mode_oct(st.st_mode),
            mtime=_mtime_iso(st.st_mtime),
            target=target,
        )

    def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> ReadResult:
        abs_path = self._abs(path)
        p = Path(abs_path)
        if not p.exists():
            raise FsError(
                "NOT_FOUND",
                f"path not found: {abs_path}",
                details={"path": abs_path},
            )
        if p.is_dir():
            raise FsError(
                "IS_A_DIR",
                f"is a directory: {abs_path}",
                details={"path": abs_path},
            )
        limit = DEFAULT_READ_MAX_BYTES if max_bytes is None else int(max_bytes)
        if limit < 0:
            limit = DEFAULT_READ_MAX_BYTES
        try:
            with p.open("rb") as fh:
                data = fh.read(limit + 1)
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
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
        abs_path = self._abs(path)
        p = Path(abs_path)
        if p.exists() and p.is_dir():
            raise FsError(
                "IS_A_DIR",
                f"is a directory: {abs_path}",
                details={"path": abs_path},
            )
        raw = content.encode(encoding) if isinstance(content, str) else content
        created = not p.exists()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(p, raw)
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        return WriteResult(path=abs_path, bytes_written=len(raw), created=created)

    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        # Local backend: put is a same-host copy (controller path → target path).
        src = _expand_abs(local_path)
        dst = self._abs(remote_path)
        src_p = Path(src)
        if not src_p.is_file():
            raise FsError(
                "NOT_FOUND",
                f"local path not found or not a file: {src}",
                details={"path": src},
            )
        dst_p = Path(dst)
        try:
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            size = _copy_file(src_p, dst_p, progress=progress)
        except OSError as exc:
            raise _map_os_error(exc, dst) from exc
        return TransferResult(
            path=dst,
            local=src,
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
        src = self._abs(remote_path)
        dst = _expand_abs(local_path)
        src_p = Path(src)
        if not src_p.exists():
            raise FsError(
                "NOT_FOUND",
                f"path not found: {src}",
                details={"path": src},
            )
        if src_p.is_dir():
            raise FsError(
                "IS_A_DIR",
                f"is a directory: {src}",
                details={"path": src},
            )
        dst_p = Path(dst)
        try:
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            size = _copy_file(src_p, dst_p, progress=progress)
        except OSError as exc:
            raise _map_os_error(exc, src) from exc
        return TransferResult(
            path=src,
            local=str(dst_p),
            bytes_transferred=int(size),
            direction="get",
        )

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo:
        abs_path = self._abs(path)
        p = Path(abs_path)
        try:
            if parents:
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            if p.is_dir():
                return self.stat(abs_path)
            raise FsError(
                "ALREADY_EXISTS",
                f"path exists and is not a directory: {abs_path}",
                details={"path": abs_path},
            ) from exc
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        return self.stat(abs_path)

    def rm(self, path: str, *, recursive: bool = False) -> str:
        abs_path = self._abs(path)
        is_link = os.path.islink(abs_path)
        if not is_link and not os.path.exists(abs_path):
            raise FsError(
                "NOT_FOUND",
                f"path not found: {abs_path}",
                details={"path": abs_path},
            )
        try:
            if is_link:
                # Unlink the symlink itself — never the target.
                os.unlink(abs_path)
            elif os.path.isdir(abs_path):
                if not recursive:
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory (use recursive): {abs_path}",
                        details={"path": abs_path},
                    )
                shutil.rmtree(abs_path)
            else:
                os.unlink(abs_path)
        except FsError:
            raise
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        return abs_path


def _resolve_final_link(abs_path: str, *, max_depth: int = 40) -> str:
    """Resolve only the final-component symlink chain.

    Unlike ``os.path.realpath``, parent-directory symlinks are left as-is.
    The OS follows those when opening or creating; we only need the final
    component's target so atomic replace updates the referent and keeps the
    symlink path intact (consistent with ``read``).
    """
    path = abs_path
    for _ in range(max_depth):
        if not os.path.islink(path):
            return path
        link_target = os.readlink(path)
        if os.path.isabs(link_target):
            path = os.path.normpath(link_target)
        else:
            path = os.path.normpath(os.path.join(os.path.dirname(path), link_target))
    # Too many levels — treat as a circular link.
    raise OSError(errno.ELOOP, "too many symbolic links", path)


def _copy_mode_if_exists(tmp: Path, target_path: str) -> None:
    """Copy the mode of *target_path* onto *tmp* when the target exists.

    Preserves destination permissions across atomic replace. A same-inode
    truncate would keep mode automatically; temp+replace would otherwise turn
    a restrictive file (``0o600``) into the temp's default (often ``0o644``).
    """
    try:
        st = os.stat(target_path)  # follows remaining symlinks on the target
    except (FileNotFoundError, OSError):
        return  # new file — keep the temp's default mode
    os.chmod(tmp, statmod.S_IMODE(st.st_mode))


def _atomic_write_bytes(p: Path, raw: bytes) -> None:
    """Write *raw* to *p* atomically: temp file + fsync + ``os.replace``.

    If *p* is a symlink, the final-component chain is resolved so the
    *target* is updated and the symlink is preserved (same as ``read``).
    The temp lives in the same directory as the resolved target so
    ``os.replace`` is atomic on one filesystem. Existing mode is copied onto
    the temp before replace. On failure the temp is removed so the destination
    is never left truncated or partial.
    """
    target_path = _resolve_final_link(str(p))
    target = Path(target_path)
    tmp = target.parent / f".{target.name}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with tmp.open("wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        _copy_mode_if_exists(tmp, target_path)
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_file(
    src: Path,
    dst: Path,
    *,
    progress: ProgressCallback | None = None,
    chunk_size: int = DEFAULT_TRANSFER_CHUNK,
) -> int:
    """Copy *src* → *dst* atomically; report progress when a callback is set.

    If *dst* is a symlink, the final-component chain is resolved so the
    *target* is updated and the symlink is preserved.
    """
    total = int(src.stat().st_size)
    dst_resolved = _resolve_final_link(str(dst))
    dst_r = Path(dst_resolved)
    tmp = dst_r.parent / f".{dst_r.name}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        if progress is None:
            shutil.copy2(src, tmp)
            # fsync for durability parity with the progress branch.
            with open(tmp, "ab") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dst_r)
            return int(dst_r.stat().st_size)

        report_progress(progress, 0, total)
        done = 0
        with src.open("rb") as fsrc, tmp.open("wb") as fdst:
            while True:
                chunk = fsrc.read(chunk_size)
                if not chunk:
                    break
                fdst.write(chunk)
                done += len(chunk)
                report_progress(progress, done, total)
            fdst.flush()
            os.fsync(fdst.fileno())
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass
        os.replace(tmp, dst_r)
        if done != total:
            # Empty file: ensure a final event already matches size.
            report_progress(progress, done, total if total else done)
        return done
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
