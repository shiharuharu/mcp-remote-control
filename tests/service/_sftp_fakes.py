"""In-memory SFTP fakes for service tests (no network).

Mirrors ``_winrm_fakes.py``: one owner module for the full ``MockSftp``
client used by SFTP list/reconnect, atomic write, and hang/budget suites.

``MockSftp`` models dirs/files/symlinks and exposes both the asyncssh-style
``readdir`` (yielding ``_SFTPName`` with attrs) and the bare-string
``listdir``. Call counters (``stat_calls`` / ``open_calls`` / ``readdir_calls``)
let tests assert that ``list`` reuses readdir attrs and that ``read`` skips
its pre-stat. ``posix_rename`` / ``readlink`` exercise atomic-write and
symlink-target paths. ``file_modes`` + ``chmod`` model OpenSSH sftp-server
permission bits so mode-preservation can be asserted.

This module is the single owner of these fakes: every service suite, including
the transfer-progress suite, imports them from here.
"""

from __future__ import annotations

import stat as statmod
from pathlib import Path


class _MockSftpFile:
    def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
        self._store = store
        self._path = path
        self._mode = mode
        self._buf = bytearray()
        if "r" in mode:
            self._data = store.get(path, b"")
            self._pos = 0
        else:
            self._data = b""
            self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def close(self) -> None:
        if "w" in self._mode:
            self._store[self._path] = bytes(self._buf)


class _MockAttrs:
    def __init__(self, mode: int, size: int = 0, mtime: float = 0.0) -> None:
        self.permissions = mode
        self.size = size
        self.mtime = mtime


class _SFTPName:
    """asyncssh SFTPName stand-in: carries .filename and .attrs."""

    def __init__(self, filename: str, attrs: _MockAttrs) -> None:
        self.filename = filename
        self.attrs = attrs


class MockSftp:
    """In-memory SFTP-like client for unit/service tests (no network).

    Models dirs/files/symlinks and exposes both the asyncssh-style ``readdir``
    (yielding ``_SFTPName`` with attrs) and the bare-string ``listdir``. The
    call counters (``stat_calls``/``open_calls``/``readdir_calls``) let tests
    assert that ``list`` reuses readdir attrs and that ``read`` skips its
    pre-stat. ``posix_rename``/``readlink`` exercise the atomic-write and
    symlink-target code paths. ``file_modes`` + ``chmod`` model OpenSSH
    sftp-server permission bits so overwrite mode-preservation can be asserted.
    """

    # Default mode for newly created files (server umask 022 -> 0644).
    _DEFAULT_FILE_MODE = 0o644

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.links: dict[str, str] = {}
        # path -> permission bits (S_IMODE); missing entries use default 0644.
        self.file_modes: dict[str, int] = {}
        self.stat_calls = 0
        self.open_calls = 0
        self.readdir_calls = 0
        self.chmod_calls: list[tuple[str, int]] = []

    def _norm(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def _children(self, path: str) -> dict[str, str]:
        path = self._norm(path)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        if path == "/":
            prefix = "/"
        result: dict[str, str] = {}
        for d in self.dirs:
            if d == path or d == "/":
                continue
            if d.startswith(prefix):
                rest = d[len(prefix) :] if prefix != "/" else d.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = d
        for f in self.files:
            if f.startswith(prefix) or (prefix == "/" and f.startswith("/")):
                rest = f[len(prefix) :] if prefix != "/" else f.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = f
        for link in self.links:
            if link.startswith(prefix) or (prefix == "/" and link.startswith("/")):
                rest = link[len(prefix) :] if prefix != "/" else link.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = link
        return result

    def _file_mode(self, path: str) -> int:
        path = self._norm(path)
        return int(self.file_modes.get(path, self._DEFAULT_FILE_MODE))

    def _attrs_for(self, path: str) -> _MockAttrs:
        path = self._norm(path)
        if path in self.links:
            return _MockAttrs(statmod.S_IFLNK | 0o777, 0, 1.0)
        if path in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if path in self.files:
            return _MockAttrs(
                statmod.S_IFREG | self._file_mode(path),
                len(self.files[path]),
                1.0,
            )
        raise FileNotFoundError(path)

    def _follow(self, path: str, *, max_depth: int = 32) -> str:
        """Resolve a symlink chain to its final path (OpenSSH-style ``stat``)."""
        path = self._norm(path)
        seen: set[str] = set()
        for _ in range(max_depth):
            if path not in self.links:
                return path
            if path in seen:
                raise OSError("too many symbolic links")
            seen.add(path)
            target = self.links[path]
            if target.startswith("/"):
                path = self._norm(target)
            else:
                parent = path.rsplit("/", 1)[0] or "/"
                path = self._norm(f"{parent}/{target}")
        raise OSError("too many symbolic links")

    def _is_dir_entry(self, path: str) -> bool:
        """True if *path* is a directory or a symlink chain ending at a dir."""
        path = self._norm(path)
        try:
            resolved = self._follow(path)
        except OSError:
            return False
        return resolved in self.dirs

    def listdir(self, path: str) -> list[str]:
        return sorted(self._children(path).keys())

    def readdir(self, path: str) -> list[_SFTPName]:
        self.readdir_calls += 1
        kids = self._children(path)
        return [
            _SFTPName(name, self._attrs_for(abs_p))
            for name, abs_p in sorted(kids.items())
        ]

    def stat(self, path: str) -> _MockAttrs:
        """Follow symlinks (asyncssh/OpenSSH ``stat`` semantics)."""
        self.stat_calls += 1
        return self._attrs_for(self._follow(path))

    def lstat(self, path: str) -> _MockAttrs:
        """Do not follow symlinks (backend ``_stat`` prefers this)."""
        self.stat_calls += 1
        return self._attrs_for(self._norm(path))

    def readlink(self, path: str) -> str:
        path = self._norm(path)
        if path not in self.links:
            raise FileNotFoundError(path)
        return self.links[path]

    def chmod(self, path: str, mode: int) -> None:
        """Apply permission bits (OpenSSH sftp-server / asyncssh surface)."""
        path = self._norm(path)
        imode = int(statmod.S_IMODE(int(mode)))
        self.chmod_calls.append((path, imode))
        if path not in self.files and path not in self.dirs:
            raise FileNotFoundError(path)
        if path in self.files:
            self.file_modes[path] = imode

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src in self.files:
            self.files[dst] = self.files.pop(src)
            # Mode travels with the inode (temp->dest promote preserves chmod).
            if src in self.file_modes:
                self.file_modes[dst] = self.file_modes.pop(src)
            else:
                self.file_modes.pop(dst, None)
        elif src in self.links:
            self.links[dst] = self.links.pop(src)
        else:
            raise FileNotFoundError(src)

    def mkdir(self, path: str) -> None:
        path = self._norm(path)
        parent = path.rsplit("/", 1)[0] or "/"
        # Parent may be a real dir or a symlink-to-directory (OpenSSH resolves
        # intermediate components; write/mkdir under /srv/www -> /var/www).
        if not self._is_dir_entry(parent):
            raise FileNotFoundError(parent)
        self.dirs.add(path)

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path in self.files:
            del self.files[path]
            self.file_modes.pop(path, None)
            return
        if path in self.links:
            del self.links[path]
            return
        raise FileNotFoundError(path)

    def rmdir(self, path: str) -> None:
        path = self._norm(path)
        if path not in self.dirs or path == "/":
            raise FileNotFoundError(path)
        # non-empty?
        for f in self.files:
            if f.startswith(path + "/"):
                raise OSError("not empty")
        for d in self.dirs:
            if d != path and d.startswith(path + "/"):
                raise OSError("not empty")
        self.dirs.discard(path)

    def open(self, path: str, mode: str = "r") -> _MockSftpFile:
        path = self._norm(path)
        self.open_calls += 1
        if "r" in mode:
            if path in self.dirs:
                raise IsADirectoryError(path)
            if path not in self.files:
                raise FileNotFoundError(path)
        else:
            # New file (or truncate via temp): default umask mode until chmod.
            # Existing path open("wb") keeps mode when present (in-place trunc).
            if path not in self.files:
                self.file_modes.setdefault(path, self._DEFAULT_FILE_MODE)
        return _MockSftpFile(self.files, path, mode)

    def put(self, local: str, remote: str) -> None:
        remote = self._norm(remote)
        data = Path(local).read_bytes()
        self.files[remote] = data
        self.file_modes.setdefault(remote, self._DEFAULT_FILE_MODE)

    def get(self, remote: str, local: str) -> None:
        remote = self._norm(remote)
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])
