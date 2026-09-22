"""Service tests: SFTP atomic write, mode preservation, and symlink parents."""

from __future__ import annotations

import os
import stat as statmod
from pathlib import Path

import pytest

from _sftp_fakes import MockSftp, _MockAttrs

from mcp_remote_control.core import fs_ops
from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.fs.types import FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


class _FailWriteFile:
    """A remote file handle whose write always fails (simulates mid-upload)."""

    def write(self, data: bytes) -> int:
        raise OSError("simulated disk full")

    def close(self) -> None:
        pass


class _AtomicFailSftp:
    """SFTP mock with posix_rename where writes to a temp path always fail.

    Used to prove write/put atomicity: the final remote file must survive a
    mid-upload failure, and the temp must be removed.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original"}
        self.dirs: set[str] = {"/", "/d"}
        self.removed_temps: list[str] = []

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def mkdir(self, p: str) -> None:
        self.dirs.add(self._norm(p))

    def open(self, p: str, mode: str = "r") -> object:
        p = self._norm(p)
        if "w" in mode and ".mrc-tmp-" in p:
            return _FailWriteFile()  # writes to the temp path fail
        if "r" in mode:
            if p in self.dirs:
                raise IsADirectoryError(p)
            if p not in self.files:
                raise FileNotFoundError(p)
            data = self.files[p]

            class _R:
                def __init__(self, d: bytes) -> None:
                    self._d = d
                    self._p = 0

                def read(self, n: int = -1) -> bytes:
                    if n is None or n < 0:
                        r = self._d[self._p :]
                        self._p = len(self._d)
                        return r
                    r = self._d[self._p : self._p + n]
                    self._p += len(r)
                    return r

                def close(self) -> None:
                    pass

            return _R(data)
        # write to a non-temp path: buffer + commit (not used by the atomic path).
        key = p
        store = self.files

        class _W:
            def __init__(self) -> None:
                self._buf = bytearray()

            def write(self, d: bytes) -> int:
                self._buf.extend(d)
                return len(d)

            def close(self) -> None:
                store[key] = bytes(self._buf)

        return _W()

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def remove(self, p: str) -> None:
        p = self._norm(p)
        if p in self.files:
            del self.files[p]
        if ".mrc-tmp-" in p:
            self.removed_temps.append(p)


class _FailReadFile:
    """A remote file handle whose read fails partway (simulates mid-download).

    A full ``read()`` (no arg / n<0, used by the no-progress ``_read_bytes``
    path) fails immediately so no bytes are ever returned. A chunked
    ``read(n)`` (n>0, used by the progress ``_get_with_progress`` path) returns
    a small chunk on the first call so a partial temp is written, then raises
    on every subsequent call. Either way the atomic get path must remove the
    temp and leave the pre-existing destination untouched.
    """

    def __init__(self, data: bytes, chunk: int = 10) -> None:
        self._data = data
        self._chunk = chunk
        self._calls = 0

    def read(self, n: int = -1) -> bytes:
        self._calls += 1
        if n is None or n < 0:
            # Full-read path: fail before returning any bytes.
            raise OSError("simulated mid-download drop")
        if self._calls > 1:
            # Chunked path: second+ chunk fails (mid-download).
            raise OSError("simulated mid-download drop")
        return self._data[: min(n, self._chunk)]

    def close(self) -> None:
        pass


class _GetFailSftp:
    """SFTP mock where reads fail partway through (simulates a mid-get drop).

    Used to prove sftp get atomicity: the local destination must survive a
    mid-download failure, and the local temp must be removed. Exposes only
    ``open`` (no ``get``/``read_file``/``posix_rename``) so both the progress
    and no-progress branches exercise the open+read code path.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original-remote-content"}
        self.dirs: set[str] = {"/", "/d"}

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def open(self, p: str, mode: str = "r") -> _FailReadFile:
        p = self._norm(p)
        if "r" in mode:
            if p in self.dirs:
                raise IsADirectoryError(p)
            if p not in self.files:
                raise FileNotFoundError(p)
            return _FailReadFile(self.files[p])
        raise OSError("unexpected write")


def test_sftp_write_atomic_failure_preserves_remote() -> None:
    """Write that fails mid-upload leaves NO partial remote file at
    the final path (temp removed; final unchanged)."""
    mock = _AtomicFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/final.txt",
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    # Final file untouched (no partial write at the final path).
    assert mock.files.get("/d/final.txt") == b"original"
    # No temp file left behind.
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    # Temp cleanup was attempted.
    assert mock.removed_temps


def test_sftp_put_atomic_failure_preserves_remote(tmp_path: Path) -> None:
    """Put that fails mid-upload leaves NO partial remote file at the
    final path (temp removed; final unchanged)."""
    mock = _AtomicFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "local.bin"
    src.write_bytes(b"would-be-payload")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert mock.files.get("/d/final.txt") == b"original"
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.removed_temps


def test_sftp_get_atomic_failure_preserves_local(tmp_path: Path) -> None:
    """SFTP get that fails mid-download leaves NO partial file at the
    local destination (local temp removed; pre-existing dst unchanged). The
    progress branch streams chunks into a local temp, then os.replace - a
    mid-read failure must remove the temp and preserve the original dst."""
    mock = _GetFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    dst = tmp_path / "downloaded.bin"
    # Pre-existing destination must survive across the failed get.
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        progress=lambda d, t: None,  # exercise the chunked _get_with_progress path
    )
    assert r.status == "error"
    # Pre-existing destination intact - no partial write at the final path.
    assert dst.read_bytes() == b"pre-existing-local-content"
    # No local temp file left behind in dst.parent.
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_sftp_get_atomic_failure_no_progress_preserves_local(tmp_path: Path) -> None:
    """The no-progress sftp get branch is also atomic - a mid-read
    failure (``_read_bytes`` open+read path) leaves the pre-existing dst
    unchanged and removes the local temp."""
    mock = _GetFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    dst = tmp_path / "downloaded.bin"
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        # no progress callback -> _read_bytes open+read path
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"pre-existing-local-content"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_sftp_get_symlink_dst_follows_to_target(tmp_path: Path) -> None:
    """SFTP get to a local symlink dst updates the referent; link preserved.

    Mirrors ``test_local_get_symlink_dst_follows_to_target``: remote get must
    resolve the final-component local symlink chain before ``os.replace`` so
    the link inode is not replaced (same policy as LocalFs.get / write).
    """
    mock = MockSftp()
    mock.files["/d/payload.bin"] = b"payload-data"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    target = tmp_path / "target.bin"
    target.write_bytes(b"old-target")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/payload.bin",
        local=str(link),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert os.path.islink(link), "local symlink inode must not be replaced"
    assert target.read_bytes() == b"payload-data"
    # Normal (non-symlink) dst still works - content written, no spurious error.
    plain = tmp_path / "plain.bin"
    r2 = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/payload.bin",
        local=str(plain),
        home=FIXTURES,
        backend=backend,
    )
    assert r2.status == "ok"
    assert plain.is_file() and not os.path.islink(plain)
    assert plain.read_bytes() == b"payload-data"


# ---------------------------------------------------------------------------
# sftp: write/put close failure must not swallow or promote temp
# ---------------------------------------------------------------------------


class _CloseFailWriteFile:
    """Write handle that buffers bytes then fails on close (FXP_CLOSE fail).

    Simulates disk-full/quota/NFS writeback surfaced only at close: writes
    appear to succeed, but the remote file is never committed. Optional
    ``commit_before_fail`` leaves bytes in the store so cleanup/remove can be
    observed (temp present then removed).
    """

    def __init__(
        self,
        store: dict[str, bytes],
        path: str,
        *,
        commit_before_fail: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self._store = store
        self._path = path
        self._buf = bytearray()
        self._commit_before_fail = commit_before_fail
        self._close_error = close_error or OSError("simulated FXP_CLOSE failure")
        self.close_calls = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1
        if self._commit_before_fail:
            self._store[self._path] = bytes(self._buf)
        raise self._close_error


class _CloseFailSftp:
    """SFTP mock with posix_rename where write-handle close always fails.

    Used to prove FXP_CLOSE failure must report fs error, leave the final
    remote path untouched, and best-effort remove the temp (no rename).
    """

    def __init__(self, *, commit_temp_before_close_fail: bool = True) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original"}
        self.dirs: set[str] = {"/", "/d"}
        self.removed_temps: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.close_fail_handles: list[_CloseFailWriteFile] = []
        self._commit_temp = commit_temp_before_close_fail

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def mkdir(self, p: str) -> None:
        self.dirs.add(self._norm(p))

    def open(self, p: str, mode: str = "r") -> object:
        p = self._norm(p)
        if "w" in mode:
            fh = _CloseFailWriteFile(
                self.files,
                p,
                commit_before_fail=self._commit_temp,
            )
            self.close_fail_handles.append(fh)
            return fh
        if p in self.dirs:
            raise IsADirectoryError(p)
        if p not in self.files:
            raise FileNotFoundError(p)
        data = self.files[p]

        class _R:
            def __init__(self, d: bytes) -> None:
                self._d = d
                self._p = 0

            def read(self, n: int = -1) -> bytes:
                if n is None or n < 0:
                    r = self._d[self._p :]
                    self._p = len(self._d)
                    return r
                r = self._d[self._p : self._p + n]
                self._p += len(r)
                return r

            def close(self) -> None:
                pass

        return _R(data)

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        self.rename_calls.append((src, dst))
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def remove(self, p: str) -> None:
        p = self._norm(p)
        if p in self.files:
            del self.files[p]
        if ".mrc-tmp-" in p:
            self.removed_temps.append(p)


class _CloseFailNoRenameSftp(_CloseFailSftp):
    """Same close-fail semantics but no callable posix_rename (non-atomic put)."""

    def __init__(self, *, commit_temp_before_close_fail: bool = True) -> None:
        super().__init__(commit_temp_before_close_fail=commit_temp_before_close_fail)
        # Instance attr shadows the method so getattr(..., "posix_rename") is None.
        self.posix_rename = None  # type: ignore[assignment]
        self.open_write_paths: list[str] = []

    def open(self, p: str, mode: str = "r") -> object:
        p = self._norm(p)
        if "w" in mode:
            self.open_write_paths.append(p)
        return super().open(p, mode)


class _MidWriteFailNoRenameSftp:
    """No posix_rename; write fails mid-stream so dest must stay original."""

    def __init__(self, *, fail_after: int = 4) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original-seed"}
        self.dirs: set[str] = {"/", "/d"}
        self.removed_temps: list[str] = []
        self.open_write_paths: list[str] = []
        self.fail_after = fail_after
        self.posix_rename = None  # type: ignore[assignment]

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def mkdir(self, p: str) -> None:
        self.dirs.add(self._norm(p))

    def open(self, p: str, mode: str = "r") -> object:
        p = self._norm(p)
        if "w" in mode:
            self.open_write_paths.append(p)
            store = self.files
            fail_after = self.fail_after

            class _MidFail:
                def __init__(self) -> None:
                    self._buf = bytearray()

                def write(self, data: bytes) -> int:
                    if len(self._buf) + len(data) > fail_after:
                        raise OSError("simulated mid-write drop")
                    self._buf.extend(data)
                    store[p] = bytes(self._buf)
                    return len(data)

                def flush(self) -> None:
                    return None

                def close(self) -> None:
                    return None

            return _MidFail()
        if p not in self.files:
            raise FileNotFoundError(p)
        data = self.files[p]

        class _R:
            def __init__(self, d: bytes) -> None:
                self._d = d

            def read(self, n: int = -1) -> bytes:
                if n is None or n < 0:
                    return self._d
                return self._d[:n]

            def close(self) -> None:
                pass

        return _R(data)

    def remove(self, p: str) -> None:
        p = self._norm(p)
        if p in self.files:
            del self.files[p]
        if ".mrc-tmp-" in p:
            self.removed_temps.append(p)


# ---------------------------------------------------------------------------
# sftp: atomic overwrite preserves destination mode
# ---------------------------------------------------------------------------


def test_sftp_write_preserves_file_mode() -> None:
    """Overwrite of 0600 remote file keeps mode after temp+posix_rename.

    Models OpenSSH sftp-server: temp is created under umask (0644); without
    an explicit chmod/setstat on the temp before rename, credentials files
    like ~/.aws/credentials silently become world-readable.
    """
    mock = MockSftp()
    mock.dirs.add("/home")
    mock.dirs.add("/home/u")
    mock.dirs.add("/home/u/.aws")
    secret = "/home/u/.aws/credentials"
    mock.files[secret] = b"aws_access_key_id=OLD\n"
    mock.file_modes[secret] = 0o600

    backend = SftpFs(mock, cwd="/home/u", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path=secret,
        content="aws_access_key_id=NEW\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files[secret] == b"aws_access_key_id=NEW\n"
    mode = mock._file_mode(secret)
    assert mode == 0o600, f"expected 0o600 after overwrite, got {oct(mode)}"
    # chmod must have run on the temp (before rename) - not a silent demote.
    assert any(m == 0o600 for _, m in mock.chmod_calls), mock.chmod_calls
    # No leftover temps.
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    # Public API also reports preserved mode.
    info = backend.stat(secret)
    assert info.mode == "0600"


def test_sftp_write_new_file_default_mode() -> None:
    """New remote file keeps umask default (no invented restrictive mode)."""
    mock = MockSftp()
    mock.dirs.add("/d")
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/fresh.txt",
        content="brand-new",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files["/d/fresh.txt"] == b"brand-new"
    mode = mock._file_mode("/d/fresh.txt")
    assert mode == 0o644, f"expected default 0o644 for new file, got {oct(mode)}"
    # No chmod applied for missing prior destination.
    assert mock.chmod_calls == []


def test_sftp_put_preserves_file_mode(tmp_path: Path) -> None:
    """Put atomic stream also preserves destination 0600 mode."""
    mock = MockSftp()
    mock.dirs.add("/d")
    dest = "/d/secret.bin"
    mock.files[dest] = b"old-payload"
    mock.file_modes[dest] = 0o600

    src = tmp_path / "local.bin"
    src.write_bytes(b"new-payload-from-put")

    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files[dest] == b"new-payload-from-put"
    mode = mock._file_mode(dest)
    assert mode == 0o600, f"expected 0o600 after put, got {oct(mode)}"
    assert any(m == 0o600 for _, m in mock.chmod_calls), mock.chmod_calls


def test_sftp_write_symlink_preserves_target_mode() -> None:
    """Write through symlink preserves resolved target's mode (not link)."""
    mock = MockSftp()
    mock.dirs.add("/d")
    target = "/d/secret.txt"
    link = "/d/link"
    mock.files[target] = b"old"
    mock.file_modes[target] = 0o600
    mock.links[link] = target

    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path=link,
        content="new",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Symlink entry preserved; content on target.
    assert link in mock.links
    assert mock.files[target] == b"new"
    mode = mock._file_mode(target)
    assert mode == 0o600, f"expected target 0o600, got {oct(mode)}"


def test_sftp_write_close_failure_preserves_remote() -> None:
    """Write path close failure -> error, final untouched, temp cleaned."""
    mock = _CloseFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/final.txt",
        content="new-content-after-close-fail",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    msg = str(r.fields.get("msg") or "")
    assert "close" in msg.lower()
    assert "FXP_CLOSE" in msg
    assert mock.files.get("/d/final.txt") == b"original"
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.rename_calls == []
    assert mock.removed_temps
    assert mock.close_fail_handles
    assert all(h.close_calls >= 1 for h in mock.close_fail_handles)


def test_sftp_put_close_failure_preserves_remote(tmp_path: Path) -> None:
    """Put atomic stream close failure -> error, no rename, temp cleaned."""
    mock = _CloseFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "local.bin"
    src.write_bytes(b"would-be-payload-on-close-fail")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    assert mock.files.get("/d/final.txt") == b"original"
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.rename_calls == []
    assert mock.removed_temps
    assert mock.close_fail_handles
    assert all(h.close_calls >= 1 for h in mock.close_fail_handles)


def test_sftp_put_close_failure_non_atomic_reports_error(tmp_path: Path) -> None:
    """Non-atomic put (no posix_rename) still surfaces close failure.

    Close fails on the temp handle; final path is never opened with wb, so the
    pre-seeded destination bytes stay intact and the temp is best-effort cleaned.
    """
    mock = _CloseFailNoRenameSftp(commit_temp_before_close_fail=False)
    # Pre-seed destination so we can prove it is not replaced on close fail.
    mock.files["/d/final.txt"] = b"original"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "local.bin"
    src.write_bytes(b"partial-would-be")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    # close failed before commit -> original still present (handle did not store).
    assert mock.files.get("/d/final.txt") == b"original"
    assert mock.close_fail_handles
    assert all(h.close_calls >= 1 for h in mock.close_fail_handles)
    # Never open the final path for write; only same-dir temps.
    assert mock.open_write_paths, "expected at least one write open"
    assert all(
        p != "/d/final.txt" and ".mrc-tmp-" in p for p in mock.open_write_paths
    ), mock.open_write_paths
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.removed_temps


def test_sftp_put_mid_write_failure_non_atomic_preserves_dest(tmp_path: Path) -> None:
    """Mid-stream put without posix_rename must not truncate final path."""
    mock = _MidWriteFailNoRenameSftp(fail_after=4)
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload-long-enough-to-fail-mid-stream")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert mock.files.get("/d/final.txt") == b"original-seed"
    assert all(
        p != "/d/final.txt" and ".mrc-tmp-" in p for p in mock.open_write_paths
    ), mock.open_write_paths
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.removed_temps


def test_sftp_write_mid_write_failure_non_atomic_preserves_dest() -> None:
    """Mid write without posix_rename must not truncate final path."""
    mock = _MidWriteFailNoRenameSftp(fail_after=3)
    backend = SftpFs(mock, cwd="/", home="/home/u")
    with pytest.raises(FsError):
        backend.write("/d/final.txt", "this-is-long-enough")
    assert mock.files.get("/d/final.txt") == b"original-seed"
    assert all(
        p != "/d/final.txt" and ".mrc-tmp-" in p for p in mock.open_write_paths
    ), mock.open_write_paths
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.removed_temps


def test_sftp_put_write_success_without_posix_rename(tmp_path: Path) -> None:
    """Without posix_rename, put/write still land via temp+promote."""
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/final.txt"] = b"old-bytes"
    mock.file_modes["/d/final.txt"] = 0o600
    # Shadow method so getattr(..., "posix_rename") is not callable.
    mock.posix_rename = None  # type: ignore[assignment]

    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/final.txt",
        content="new-via-write",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files["/d/final.txt"] == b"new-via-write"
    assert mock._file_mode("/d/final.txt") == 0o600
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []

    src = tmp_path / "up.bin"
    src.write_bytes(b"new-via-put")
    r2 = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r2.status == "ok"
    assert mock.files["/d/final.txt"] == b"new-via-put"
    assert mock._file_mode("/d/final.txt") == 0o600
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []


def test_sftp_write_close_failure_preserves_underlying_cause() -> None:
    """FsError from close keeps the underlying exception as __cause__."""
    mock = _CloseFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    with pytest.raises(FsError) as ei:
        backend.write("/d/final.txt", "payload")
    err = ei.value
    assert err.code == "FS_ERROR"
    assert "close" in err.msg.lower()
    assert isinstance(err.__cause__, OSError)
    assert "FXP_CLOSE" in str(err.__cause__)
    assert mock.files.get("/d/final.txt") == b"original"
    assert mock.rename_calls == []


def test_sftp_stat_symlink_reports_target() -> None:
    """Stat on a symlink returns kind=link and the target via readlink."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.txt"] = b"content"
    mock.links["/d/link"] = "/d/target.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "stat", ep="lab-ssh", path="/d/link", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "link"
    assert r.fields.get("target") == "/d/target.txt"


def test_sftp_write_symlink_preserves_link() -> None:
    """Atomic write through a symlink updates the target; link entry preserved.

    Matches local backend policy: posix_rename must land on the final referent,
    not replace the symlink directory entry with a regular file.
    """
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.txt"] = b"old-content"
    mock.links["/d/link"] = "/d/target.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/link",
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Symlink directory entry still a link to the same target.
    assert "/d/link" in mock.links
    assert mock.links["/d/link"] == "/d/target.txt"
    assert "/d/link" not in mock.files
    # Referent content updated.
    assert mock.files["/d/target.txt"] == b"new-content"
    # Public stat still reports kind=link.
    r_stat = fs_ops.run(
        "stat", ep="lab-ssh", path="/d/link", home=FIXTURES, backend=backend
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "link"
    assert r_stat.fields.get("target") == "/d/target.txt"


def test_sftp_put_symlink_preserves_link(tmp_path: Path) -> None:
    """Atomic put through a symlink updates the target; link entry preserved."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.bin"] = b"old"
    mock.links["/d/link.bin"] = "/d/target.bin"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-link")

    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/link.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "/d/link.bin" in mock.links
    assert mock.links["/d/link.bin"] == "/d/target.bin"
    assert "/d/link.bin" not in mock.files
    assert mock.files["/d/target.bin"] == b"payload-via-link"


def test_sftp_write_symlink_chain_preserves_links() -> None:
    """Atomic write through a symlink chain resolves to the final target."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/real.txt"] = b"old"
    mock.links["/d/link1"] = "/d/real.txt"
    mock.links["/d/link2"] = "/d/link1"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/link2",
        content="via-chain",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.links["/d/link1"] == "/d/real.txt"
    assert mock.links["/d/link2"] == "/d/link1"
    assert "/d/link1" not in mock.files
    assert "/d/link2" not in mock.files
    assert mock.files["/d/real.txt"] == b"via-chain"


def test_sftp_write_symlink_target_keeps_trailing_space() -> None:
    """A stored link target is used verbatim: ``/link`` -> ``/x `` updates the
    real referent ``/x `` and leaves the lookalike ``/x`` alone.

    Trimming the readlink result lands the write on ``/x`` - a different
    remote object than the one the link names.
    """
    mock = MockSftp()
    mock.files["/x "] = b"spaced-referent"
    mock.files["/x"] = b"plain-referent"
    mock.links["/link"] = "/x "
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/link",
        content="NEW",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok", r.render_text()
    assert mock.files["/x "] == b"NEW"
    assert mock.files["/x"] == b"plain-referent"
    # The link entry survives; stat still reports its target verbatim.
    assert mock.links["/link"] == "/x "
    assert "/link" not in mock.files
    s = fs_ops.run(
        "stat", ep="lab-ssh", path="/link", home=FIXTURES, backend=backend
    )
    assert s.status == "ok"
    assert s.fields.get("type") == "link"
    assert s.fields.get("target") == "/x "


# ---------------------------------------------------------------------------
# sftp: symlink-to-directory parents must not raise ALREADY_EXISTS
# ---------------------------------------------------------------------------


def test_sftp_write_under_symlink_to_dir_parent() -> None:
    """Write under a symlink-to-directory parent succeeds.

    Production failure mode: ``/srv/www`` -> ``/var/www`` reports kind=link via
    lstat; ``_mkdir_p`` must treat the link as an existing dir, not
    ALREADY_EXISTS.
    """
    mock = MockSftp()
    mock.dirs.update({"/srv", "/var", "/var/www"})
    mock.links["/srv/www"] = "/var/www"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/srv/www/app.conf",
        content="listen=80\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files.get("/srv/www/app.conf") == b"listen=80\n"
    # Parent symlink entry preserved (not replaced by a real dir).
    assert mock.links.get("/srv/www") == "/var/www"
    assert "/srv/www" not in mock.dirs


def test_sftp_mkdir_under_symlink_to_dir_parent() -> None:
    """mkdir under a symlink-to-directory parent succeeds."""
    mock = MockSftp()
    mock.dirs.update({"/srv", "/var", "/var/www"})
    mock.links["/srv/www"] = "/var/www"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "mkdir",
        ep="lab-ssh",
        path="/srv/www/releases",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "/srv/www/releases" in mock.dirs
    assert mock.links.get("/srv/www") == "/var/www"


def test_sftp_mkdir_symlink_to_dir_itself_is_ok() -> None:
    """mkdir parents=True on an existing symlink-to-dir is a no-op success."""
    mock = MockSftp()
    mock.dirs.update({"/srv", "/var", "/var/www"})
    mock.links["/srv/www"] = "/var/www"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "mkdir",
        ep="lab-ssh",
        path="/srv/www",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Still a link, not materialised into dirs.
    assert mock.links.get("/srv/www") == "/var/www"
    assert "/srv/www" not in mock.dirs


def test_sftp_write_under_symlink_to_file_parent_rejected() -> None:
    """Symlink-to-file as a parent path still raises ALREADY_EXISTS."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/real.txt"] = b"body"
    mock.links["/d/notdir"] = "/d/real.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/notdir/child.txt",
        content="nope",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"
    assert "/d/notdir/child.txt" not in mock.files


def test_sftp_mkdir_symlink_to_file_rejected() -> None:
    """mkdir on a symlink-to-file path is rejected (not treated as dir)."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/real.txt"] = b"body"
    mock.links["/d/filelink"] = "/d/real.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "mkdir",
        ep="lab-ssh",
        path="/d/filelink",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"


def test_sftp_write_under_dangling_symlink_parent_rejected() -> None:
    """Dangling symlink parent is not mis-allowed as a directory."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.links["/d/broken"] = "/d/missing-target"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/broken/child.txt",
        content="nope",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"
    assert "/d/broken/child.txt" not in mock.files


def test_sftp_write_under_relative_symlink_to_dir_parent() -> None:
    """Relative symlink target still counts as existing dir parent."""
    mock = MockSftp()
    mock.dirs.update({"/var", "/var/www"})
    # /var/site -> www  (relative; resolves to /var/www)
    mock.links["/var/site"] = "www"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/var/site/rel.conf",
        content="ok\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files.get("/var/site/rel.conf") == b"ok\n"
    assert mock.links.get("/var/site") == "www"
