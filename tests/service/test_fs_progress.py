"""Service tests: put/get progress callbacks (T16)."""

from __future__ import annotations

import stat as statmod
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.fs.backends.local import LocalFs
from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.fs.backends.winrm import WinrmFs
from mcp_remote_control.fs.types import DEFAULT_TRANSFER_CHUNK

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Large enough to span multiple transfer chunks → ≥1 intermediate progress.
LARGE_SIZE = DEFAULT_TRANSFER_CHUNK + 12_345


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


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


class MockSftp:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}

    def _norm(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def listdir(self, path: str) -> list[str]:
        path = self._norm(path)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return []

    def stat(self, path: str) -> _MockAttrs:
        path = self._norm(path)
        if path in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if path in self.files:
            data = self.files[path]
            return _MockAttrs(statmod.S_IFREG | 0o644, len(data), 1.0)
        raise FileNotFoundError(path)

    def lstat(self, path: str) -> _MockAttrs:
        return self.stat(path)

    def mkdir(self, path: str) -> None:
        path = self._norm(path)
        self.dirs.add(path)

    def open(self, path: str, mode: str = "r") -> _MockSftpFile:
        path = self._norm(path)
        if "r" in mode and path not in self.files:
            raise FileNotFoundError(path)
        return _MockSftpFile(self.files, path, mode)

    def put(self, local: str, remote: str) -> None:
        remote = self._norm(remote)
        self.files[remote] = Path(local).read_bytes()

    def get(self, remote: str, local: str) -> None:
        remote = self._norm(remote)
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])


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
# winrm mock
# ---------------------------------------------------------------------------


class _MockWinAttrs:
    def __init__(self, kind: str, size: int = 0) -> None:
        self.kind = kind
        self.size = size
        self.mtime = 1.0
        self.mode = "Archive"


class MockWinrmFileClient:
    """In-memory WinRM file client with real native copy/fetch (opt-in flags)."""

    # Explicit True: production-like native transfer (H3 default is False when missing).
    has_native_copy = True
    has_native_fetch = True

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"C:\\", "C:\\temp"}

    def _norm(self, path: str) -> str:
        p = str(path).strip().replace("/", "\\")
        while "\\\\" in p:
            p = p.replace("\\\\", "\\")
        if len(p) > 3 and p.endswith("\\"):
            p = p.rstrip("\\")
        if len(p) == 2 and p[1] == ":":
            p = p + "\\"
        return p

    def listdir(self, path: str) -> list[str]:
        return []

    def stat(self, path: str) -> _MockWinAttrs:
        path = self._norm(path)
        if path in self.dirs or any(self._norm(d) == path for d in self.dirs):
            return _MockWinAttrs("dir", 0)
        if path in self.files:
            return _MockWinAttrs("file", len(self.files[path]))
        raise FileNotFoundError(path)

    def mkdir(self, path: str) -> None:
        self.dirs.add(self._norm(path))

    def write_file(self, path: str, data: bytes) -> None:
        self.files[self._norm(path)] = data

    def read_file(self, path: str) -> bytes:
        path = self._norm(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def copy(self, local: str, remote: str) -> None:
        self.files[self._norm(remote)] = Path(local).read_bytes()

    def fetch(self, remote: str, local: str) -> None:
        path = self._norm(remote)
        if path not in self.files:
            raise FileNotFoundError(path)
        Path(local).write_bytes(self.files[path])


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
