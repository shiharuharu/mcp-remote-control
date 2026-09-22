"""Local filesystem backend (process host via pathlib/os).

Safety invariants:

- Path expansion joins the caller's string without lexical normalization
  (no ``abspath`` / ``normpath``), so ``..`` is resolved by the OS against
  the real directory chain and an intermediate symlink is followed before
  the parent step. The caller's spelling is kept, including whitespace.
  Destructive ops use ``lstat`` / ``unlink`` / ``readlink`` so symlinks are
  not followed into unexpected targets. ``rm`` on a symlink removes the link
  itself, never the referent.
- Writes and copies are atomic (temp file + fsync + ``os.replace``) and
  preserve the destination mode when replacing an existing file.
- Every path opened for reading (``read``, ``get``, the copy helper) is
  stat'ed first and refused unless it is a regular file, so a FIFO/socket/
  device cannot park the caller in the kernel forever.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as statmod
from datetime import UTC, datetime
from pathlib import Path

from mcp_remote_control.fs.atomic import mrc_tmp_name
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

# Cap recursive list descent (align SFTP/WinRM ~40). Bare os.walk has no
# bound; huge trees blow MCP payloads and wall time. Depth exceed raises
# FsError(DEPTH_EXCEEDED) - never silent incomplete ListResult success.
# (Same signal as SFTP; non-recursive list is unchanged.)
_MAX_RECURSE_DEPTH = 40


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


def _expand_user(text: str) -> Path:
    """``Path.expanduser`` that reports an unresolvable ``~user`` as INVALID_ARG.

    ``pathlib`` raises ``RuntimeError`` when a named user has no home
    directory here (``~`` alone is unaffected: it reads ``$HOME``). Leaving
    the tilde literal instead would join ``~deploy/x`` onto the current
    directory and name a location nobody asked for, so the failure is
    reported as the path diagnosis it is rather than escaping as interpreter
    text.
    """
    try:
        return Path(text).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise FsError(
            "INVALID_ARG",
            f"cannot expand home directory in path: {text}",
            details={"path": text},
        ) from exc


def _expand_abs(raw: str, *, base: str | None = None) -> str:
    """Absolutize *raw* while keeping the caller's spelling of every name.

    Only emptiness is an error. Whitespace is part of a name and ``..`` is
    left standing, so ``x`` and ``x `` stay different names and the OS
    resolves ``..`` against the real directory chain. ``_expand_user`` still
    expands ``~``, and ``Path`` still collapses redundant separators, a ``.``
    component and a trailing slash - rewrites that name the same object.
    """
    text = str(raw)
    if not text:
        raise FsError("INVALID_ARG", "path is empty")
    p = _expand_user(text)
    if not p.is_absolute():
        root = base if base else os.getcwd()
        # Plain join - never abspath/normpath: ".." must be resolved by the
        # OS against the real directory chain (a textual collapse steps over
        # an intermediate symlink and names a different object).
        return os.path.join(root, str(p))
    return str(p)


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


def _map_transfer_error(exc: OSError, *, src: str, dst: str) -> FsError:
    """Map a copy failure against the side of the transfer that raised it.

    ``OSError.filename`` names the object the failing syscall was given, which
    is the only attribution a copy that interleaves both sides can offer: an
    unreadable source reports the source path, while a temp-write, chmod or
    replace failure reports a path on the destination side. An exception with
    no filename (a bare ``OSError`` from an injected call, an in-flight write)
    is attributed to the destination: the source is only ever read through the
    handle opened for it, and every later step runs against the destination.
    """
    name = getattr(exc, "filename", None)
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    return _map_os_error(exc, src if name == src else dst)


def _require_regular_file(abs_path: str) -> os.stat_result:
    """Stat *abs_path* and refuse it unless it is a regular file.

    Every operation that reads a caller-named path funnels through here
    before opening it. ``stat`` (not ``lstat``) matches the symlink-following
    open/copy that follows, so a symlink to a regular file still reads.

    A FIFO, socket, or device has no end-of-file to reach, and no fs
    operation carries a wall-clock budget, so a read on one blocks in the
    kernel forever and strands the calling thread. A hang cannot be caught,
    so the refusal has to happen before the open. ``shutil`` only rejects
    FIFOs ("named pipe"), so a device source would otherwise copy until the
    disk fills.

    Returns the stat result so callers that want the size need only one
    lookup. Raises ``IS_A_DIR`` for directories (a distinct, more useful
    signal than ``NOT_A_FILE``).
    """
    try:
        st = os.stat(abs_path)
    except OSError as exc:
        raise _map_os_error(exc, abs_path) from exc
    if statmod.S_ISDIR(st.st_mode):
        raise FsError(
            "IS_A_DIR",
            f"is a directory: {abs_path}",
            details={"path": abs_path},
        )
    if not statmod.S_ISREG(st.st_mode):
        raise FsError(
            "NOT_A_FILE",
            f"not a regular file: {abs_path}",
            details={"path": abs_path, "kind": _kind_from_stat(st)},
        )
    return st


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
                self._list_recursive(
                    abs_path,
                    entries,
                    max_depth=_MAX_RECURSE_DEPTH,
                )
            else:
                # Include "." and ".." so agents can navigate without a second
                # call. A row's path is the listed directory's spelling plus
                # that row's own name - the rule the child rows below follow -
                # so ".." reaches the OS (which resolves it after any
                # intermediate symlink) rather than being collapsed textually
                # into a path that names a different object.
                for name in (".", ".."):
                    row_path = (
                        abs_path if name == "." else os.path.join(abs_path, "..")
                    )
                    try:
                        st = os.lstat(row_path)
                    except OSError:
                        continue
                    entries.append(
                        ListEntry(
                            name=name,
                            kind=_entry_kind(st),
                            size=0 if statmod.S_ISDIR(st.st_mode) else int(st.st_size),
                            mode=_mode_oct(st.st_mode),
                            mtime=_mtime_iso(st.st_mtime),
                            path=row_path,
                        )
                    )
                for child in sorted(p.iterdir(), key=lambda c: c.name):
                    entries.append(self._entry_for(child, base=abs_path, relative=False))
        except OSError as exc:
            raise _map_os_error(exc, abs_path) from exc
        return ListResult(path=abs_path, entries=entries)

    def _list_recursive(
        self,
        abs_path: str,
        entries: list[ListEntry],
        *,
        max_depth: int = _MAX_RECURSE_DEPTH,
    ) -> None:
        """Walk *abs_path* and append relative ListEntry rows to *entries*.

        Depth is the number of path components under *abs_path*. Entering a
        directory deeper than *max_depth* raises ``FsError(DEPTH_EXCEEDED)``
        (SFTP-aligned) so callers never get a silent partial tree. A directory
        the walk cannot scan raises through the same error mapping with that
        node's path, so an unreadable subtree is reported rather than dropped.
        ``os.walk`` does not follow symlinks (``followlinks=False``).
        """

        def _on_walk_error(exc: OSError) -> None:
            # Without a callback os.walk skips a directory it cannot scandir
            # and the walk still looks complete. Raising aborts the recursion
            # at the failing node: os.walk raises out of the scandir call it
            # was in, so the exception carries that node's path. An OSError
            # without a filename can only be attributed to the walk root, the
            # last node the walk definitely reached.
            node = exc.filename or abs_path
            raise _map_os_error(exc, str(node)) from exc

        for root, dirnames, filenames in os.walk(
            abs_path, topdown=True, onerror=_on_walk_error
        ):
            rel = os.path.relpath(root, abs_path)
            if rel in (os.curdir, ""):
                depth = 0
            else:
                depth = rel.count(os.sep) + 1
            # SFTP enters a path with N components under top using depth_param
            # N-1 and raises when depth_param >= max_depth (i.e. N > max_depth).
            if depth > max_depth:
                raise FsError(
                    "DEPTH_EXCEEDED",
                    f"maximum recursion depth ({max_depth}) exceeded at: {root}",
                    details={"path": root, "max_depth": max_depth},
                )
            root_p = Path(root)
            for name in sorted(dirnames):
                entries.append(
                    self._entry_for(root_p / name, base=abs_path, relative=True)
                )
            for name in sorted(filenames):
                entries.append(
                    self._entry_for(root_p / name, base=abs_path, relative=True)
                )

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
        _require_regular_file(abs_path)
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
        # Local backend: put is a same-host copy (controller path -> target path).
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
            raise _map_transfer_error(exc, src=src, dst=dst) from exc
        return TransferResult(
            path=dst,
            local=src,
            bytes_transferred=int(size),
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
        # Refuse a special-file source before anything is created, exactly as
        # read() does: the copy below would otherwise block in the kernel
        # (progress branch) or copy without end (shutil rejects only FIFOs).
        _require_regular_file(src)
        dst_p = Path(dst)
        try:
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            size = _copy_file(src_p, dst_p, progress=progress)
        except OSError as exc:
            raise _map_transfer_error(exc, src=src, dst=dst) from exc
        return TransferResult(
            path=src,
            local=str(dst_p),
            bytes_transferred=int(size),
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
                # Unlink the symlink itself - never the target.
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

    The stored target is joined verbatim: ``..`` inside it is resolved by the
    OS after the link is followed, never collapsed textually.
    """
    path = abs_path
    for _ in range(max_depth):
        if not os.path.islink(path):
            return path
        link_target = os.readlink(path)
        if os.path.isabs(link_target):
            path = link_target
        else:
            path = os.path.join(os.path.dirname(path), link_target)
    # Too many levels - treat as a circular link.
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
        return  # new file - keep the temp's default mode
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
    tmp = target.parent / mrc_tmp_name(target.name)
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
    """Copy *src* -> *dst* atomically; report progress when a callback is set.

    If *dst* is a symlink, the final-component chain is resolved so the
    *target* is updated and the symlink is preserved.

    Mode policy (aligned with ``_atomic_write_bytes`` / SFTP): when
    *dst* already exists, its permission bits are applied to the temp
    before ``os.replace`` so an overwrite never widens a restrictive
    destination (e.g. ``0o600``). New destinations keep the source mode
    (via ``copystat``) or the umask-derived temp mode if metadata copy
    fails. The temp stays writable until it has been fsynced, so a
    read-only source (``0o444``) is copyable on both branches.

    The source must be a regular file; see ``_require_regular_file``.
    """
    st = _require_regular_file(str(src))
    total = int(st.st_size)
    dst_resolved = _resolve_final_link(str(dst))
    dst_r = Path(dst_resolved)
    tmp = dst_r.parent / mrc_tmp_name(dst_r.name)
    try:
        if progress is None:
            # copyfile, not copy2: the temp must stay writable until the fsync
            # reopen below. copy2 applies the source mode first, so a read-only
            # source (0o444) leaves the temp unwritable and the reopen raises
            # PermissionError - the transfer would then be refused here but
            # succeed through the progress branch. Source metadata is applied
            # by copystat after the reopen instead.
            shutil.copyfile(src, tmp)
            # fsync for durability parity with the progress branch.
            with open(tmp, "ab") as fh:
                os.fsync(fh.fileno())
            try:
                shutil.copystat(src, tmp)
            except OSError:
                # Best-effort metadata, as on the progress branch: the temp
                # keeps its umask mode. Propagating would make a transfer's
                # success depend on whether a progress callback was supplied.
                pass
            # After copystat (source mode), re-apply dest mode when overwriting.
            _copy_mode_if_exists(tmp, dst_resolved)
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
        # After copystat (source mode), re-apply dest mode when overwriting.
        _copy_mode_if_exists(tmp, dst_resolved)
        os.replace(tmp, dst_r)
        if done != total:
            # Empty file: ensure a final event already matches size.
            report_progress(progress, done, total if total else done)
        return done
    except Exception:
        try:
            # copystat copies the source's st_flags onto the temp, so a temp
            # staged from an immutable (uchg) source can be neither replaced
            # nor unlinked until the flag is cleared; a failed transfer must
            # not leave the temp behind.
            if getattr(os.stat(tmp), "st_flags", 0):
                os.chflags(tmp, 0)
        except (OSError, AttributeError):
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
