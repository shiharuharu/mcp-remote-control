"""Service tests for put/get transfer progress callbacks."""

from __future__ import annotations

import stat as statmod
from pathlib import Path


from _sftp_fakes import MockSftp, _MockAttrs
from test_fs_winrm import MockWinrmFileClient

from mcp_remote_control.core import fs_ops
from mcp_remote_control.fs.backends.local import LocalFs
from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.fs.backends.winrm import WinrmFs
from mcp_remote_control.fs.types import DEFAULT_TRANSFER_CHUNK

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Large enough to span multiple transfer chunks -> >=1 intermediate progress.
LARGE_SIZE = DEFAULT_TRANSFER_CHUNK + 12_345


def _events() -> list[tuple[int, int | None]]:
    return []


def _cb(events: list[tuple[int, int | None]]):
    def progress(bytes_done: int, total: int | None) -> None:
        events.append((bytes_done, total))

    return progress


# ---------------------------------------------------------------------------
# local
# ---------------------------------------------------------------------------


def test_local_put_large_progress_fires(tmp_path: Path) -> None:
    src = tmp_path / "large.bin"
    payload = b"A" * LARGE_SIZE
    src.write_bytes(payload)
    remote = tmp_path / "dst" / "large.bin"
    events = _events()

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(remote),
        local=str(src),
        home=FIXTURES,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert remote.read_bytes() == payload
    assert len(events) >= 1
    # Final event must reach full size with matching total.
    assert events[-1] == (LARGE_SIZE, LARGE_SIZE)
    # Monotonic non-decreasing bytes_done.
    dones = [e[0] for e in events]
    assert dones == sorted(dones)
    assert max(dones) == LARGE_SIZE


def test_local_get_large_progress_fires(tmp_path: Path) -> None:
    remote = tmp_path / "src.bin"
    payload = b"B" * LARGE_SIZE
    remote.write_bytes(payload)
    dest = tmp_path / "got.bin"
    events = _events()

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(remote),
        local=str(dest),
        home=FIXTURES,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert dest.read_bytes() == payload
    assert len(events) >= 1
    assert events[-1] == (LARGE_SIZE, LARGE_SIZE)


def test_local_put_without_progress_still_works(tmp_path: Path) -> None:
    src = tmp_path / "small.bin"
    src.write_bytes(b"no-progress-token")
    remote = tmp_path / "out.bin"

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(remote),
        local=str(src),
        home=FIXTURES,
        # progress omitted
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == len(b"no-progress-token")
    assert remote.read_bytes() == b"no-progress-token"


def test_local_backend_put_progress_direct(tmp_path: Path) -> None:
    """Direct LocalFs.put with progress list append (mock-assert style)."""
    src = tmp_path / "x.bin"
    payload = b"Z" * (DEFAULT_TRANSFER_CHUNK * 2 + 7)
    src.write_bytes(payload)
    dst = tmp_path / "y.bin"
    seen: list[tuple[int, int | None]] = []

    result = LocalFs().put(str(src), str(dst), progress=lambda d, t: seen.append((d, t)))
    assert result.bytes_transferred == len(payload)
    assert dst.read_bytes() == payload
    assert len(seen) >= 1
    assert seen[-1][0] == len(payload)
    assert seen[-1][1] == len(payload)


# ---------------------------------------------------------------------------
# sftp mock
# ---------------------------------------------------------------------------


def test_sftp_put_progress_fires(tmp_path: Path) -> None:
    mock = MockSftp()
    mock.dirs.add("/tmp")
    backend = SftpFs(mock, cwd="/tmp", home="/home/u")
    src = tmp_path / "up.bin"
    payload = b"S" * LARGE_SIZE
    src.write_bytes(payload)
    events = _events()

    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/tmp/up.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert mock.files["/tmp/up.bin"] == payload
    assert len(events) >= 1
    assert events[-1][0] == LARGE_SIZE


def test_sftp_get_progress_fires(tmp_path: Path) -> None:
    mock = MockSftp()
    mock.dirs.add("/tmp")
    payload = b"G" * LARGE_SIZE
    mock.files["/tmp/down.bin"] = payload
    backend = SftpFs(mock, cwd="/tmp", home="/home/u")
    dest = tmp_path / "down.bin"
    events = _events()

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/tmp/down.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert dest.read_bytes() == payload
    assert len(events) >= 1
    assert events[-1][0] == LARGE_SIZE


def test_sftp_put_without_progress_still_works(tmp_path: Path) -> None:
    mock = MockSftp()
    mock.dirs.add("/tmp")
    backend = SftpFs(mock, cwd="/tmp", home="/home/u")
    src = tmp_path / "s.bin"
    src.write_bytes(b"sftp-ok")

    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/tmp/s.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.files["/tmp/s.bin"] == b"sftp-ok"
    assert r.fields.get("bytes") == len(b"sftp-ok")


# ---------------------------------------------------------------------------
# sftp: close failure on progress put path
# ---------------------------------------------------------------------------


class _CloseFailProgressFile:
    """Write handle for progress put: streams ok, close raises (FXP_CLOSE)."""

    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self._buf = bytearray()
        self.close_calls = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1
        # Commit temp so remove can observe it, then fail close.
        self._store[self._path] = bytes(self._buf)
        raise OSError("simulated FXP_CLOSE failure on progress put")


class _CloseFailProgressSftp:
    """Atomic progress put mock: open+posix_rename, close always fails."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/tmp/up.bin": b"original-remote"}
        self.dirs: set[str] = {"/", "/tmp"}
        self.removed_temps: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.handles: list[_CloseFailProgressFile] = []

    def _norm(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, path: str) -> _MockAttrs:
        path = self._norm(path)
        if path in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if path in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[path]), 1.0)
        raise FileNotFoundError(path)

    def lstat(self, path: str) -> _MockAttrs:
        return self.stat(path)

    def mkdir(self, path: str) -> None:
        self.dirs.add(self._norm(path))

    def open(self, path: str, mode: str = "r") -> object:
        path = self._norm(path)
        if "w" in mode:
            fh = _CloseFailProgressFile(self.files, path)
            self.handles.append(fh)
            return fh
        raise FileNotFoundError(path)

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        self.rename_calls.append((src, dst))
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path in self.files:
            del self.files[path]
        if ".mrc-tmp-" in path:
            self.removed_temps.append(path)


def test_sftp_put_progress_close_failure_preserves_remote(tmp_path: Path) -> None:
    """Progress put streaming close fail -> error, no rename, temp cleaned."""
    mock = _CloseFailProgressSftp()
    backend = SftpFs(mock, cwd="/tmp", home="/home/u")
    src = tmp_path / "up.bin"
    payload = b"S" * LARGE_SIZE
    src.write_bytes(payload)
    events = _events()

    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/tmp/up.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
        progress=_cb(events),
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    msg = str(r.fields.get("msg") or "")
    assert "close" in msg.lower()
    assert mock.files.get("/tmp/up.bin") == b"original-remote"
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.rename_calls == []
    assert mock.removed_temps
    assert mock.handles
    assert all(h.close_calls >= 1 for h in mock.handles)
    # Progress may have fired for streamed chunks before close failed.
    assert len(events) >= 1


# ---------------------------------------------------------------------------
# winrm mock
# ---------------------------------------------------------------------------


def test_winrm_put_progress_fires(tmp_path: Path) -> None:
    client = MockWinrmFileClient()
    backend = WinrmFs(client, cwd=r"C:\temp", home=r"C:\Users\u")
    src = tmp_path / "w.bin"
    payload = b"W" * LARGE_SIZE
    src.write_bytes(payload)
    events = _events()

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=r"C:\temp\large.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert client.files[r"C:\temp\large.bin"] == payload
    assert len(events) >= 1
    assert events[-1][0] == LARGE_SIZE


def test_winrm_get_progress_fires(tmp_path: Path) -> None:
    client = MockWinrmFileClient()
    payload = b"X" * LARGE_SIZE
    client.files[r"C:\temp\dl.bin"] = payload
    backend = WinrmFs(client, cwd=r"C:\temp", home=r"C:\Users\u")
    dest = tmp_path / "dl.bin"
    events = _events()

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=r"C:\temp\dl.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend,
        progress=_cb(events),
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == LARGE_SIZE
    assert dest.read_bytes() == payload
    assert len(events) >= 1
    assert events[-1][0] == LARGE_SIZE


def test_winrm_put_without_progress_still_works(tmp_path: Path) -> None:
    client = MockWinrmFileClient()
    backend = WinrmFs(client, cwd=r"C:\temp", home=r"C:\Users\u")
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"winrm-ok")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=r"C:\temp\tiny.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert client.files[r"C:\temp\tiny.bin"] == b"winrm-ok"
    assert r.fields.get("bytes") == len(b"winrm-ok")
