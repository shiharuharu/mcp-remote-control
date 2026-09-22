"""Service tests: WinRM fs backend - all 8 ops + NOT_FOUND (mock only)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pypsrp.exceptions
import pytest

from _winrm_fakes import HOME, TEMP, FakePypsrpSession

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.fs.backends.winrm import PypsrpFileClient, WinrmFs
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport.base import ExecResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# Mock filesystem: dict of paths -> content / meta
# ---------------------------------------------------------------------------


class _MockAttrs:
    def __init__(
        self,
        kind: str,
        size: int = 0,
        mtime: float = 1.0,
        mode: str = "Archive",
    ) -> None:
        self.kind = kind
        self.size = size
        self.mtime = mtime
        self.mode = mode


class MockWinrmFileClient:
    """In-memory Windows-like file store for unit/service tests (no network)."""

    # Real in-memory copy/fetch - opt in so SupportsCopyFetch is treated as native.
    has_native_copy = True
    has_native_fetch = True

    def __init__(self) -> None:
        # Normalized paths use backslash; drive roots always present.
        self.files: dict[str, bytes] = {}
        # Final-component reparse/symlink map: link path -> target path.
        self.links: dict[str, str] = {}
        # Subset of ``links`` that are directory reparse points (junctions /
        # symlinks to a directory). Windows marks those ``Directory,
        # ReparsePoint``; ``stat`` reports the mode and the resolve rejects
        # them when the target cannot be read.
        self.dir_links: set[str] = set()
        # Store dirs without relying on raw-string trailing backslash.
        self.dirs: set[str] = {
            "C:\\",
            "C:\\Users",
            HOME,
            TEMP,
        }

    def _norm(self, path: str) -> str:
        p = str(path).strip().replace("/", "\\")
        # Collapse duplicate separators (preserve UNC \\server).
        if p.startswith("\\\\"):
            rest = p[2:]
            while "\\\\" in rest:
                rest = rest.replace("\\\\", "\\")
            p = "\\\\" + rest
        else:
            while "\\\\" in p:
                p = p.replace("\\\\", "\\")
        if len(p) > 3 and p.endswith("\\"):
            p = p.rstrip("\\")
        if len(p) == 2 and p[1] == ":":
            p = p + "\\"
        return p

    def _exists_dir(self, path: str) -> bool:
        path = self._norm(path)
        for d in self.dirs:
            if self._norm(d) == path:
                return True
        return False

    def _is_dir_entry(self, path: str) -> bool:
        """True if *path* is a directory or a reparse chain ending at a dir.

        Intermediate components may be junctions/symlinks (OpenSSH-style
        resolve); write/mkdir under ``C:\\srv\\www`` -> ``C:\\var\\www``.
        """
        path = self._norm(path)
        seen: set[str] = set()
        for _ in range(32):
            if path not in self.links:
                return self._exists_dir(path)
            if path in seen:
                return False
            seen.add(path)
            target = self.links[path]
            t = str(target).strip().replace("/", "\\")
            if not t:
                return False
            # Absolute (drive or UNC) vs relative join.
            if (len(t) >= 2 and t[1] == ":") or t.startswith("\\\\"):
                path = self._norm(t)
            else:
                parent = path.rsplit("\\", 1)[0]
                if len(parent) == 2 and parent[1] == ":":
                    parent = parent + "\\"
                path = self._norm(
                    (parent + "\\" + t.lstrip("\\")) if parent else t
                )
        return False

    def listdir(self, path: str) -> list[str]:
        path = self._norm(path)
        if not self._exists_dir(path):
            raise FileNotFoundError(path)
        names: list[str] = []
        if path.endswith("\\"):
            prefix = path
        else:
            prefix = path + "\\"
        for d in list(self.dirs):
            dn = self._norm(d)
            if dn == path:
                continue
            if dn.startswith(prefix):
                rest = dn[len(prefix) :]
                if rest and "\\" not in rest:
                    names.append(rest)
        for f in self.files:
            fn = self._norm(f)
            if fn.startswith(prefix):
                rest = fn[len(prefix) :]
                if rest and "\\" not in rest:
                    names.append(rest)
        for lnk in self.links:
            ln = self._norm(lnk)
            if ln.startswith(prefix):
                rest = ln[len(prefix) :]
                if rest and "\\" not in rest:
                    names.append(rest)
        return sorted(set(names))

    def stat(self, path: str) -> _MockAttrs:
        path = self._norm(path)
        if path in self.links:
            # A directory reparse point (junction / symlink-to-directory)
            # reports Windows' ``Directory, ReparsePoint``; the mode string is
            # what tells the two link kinds apart when the target is unreadable.
            mode = "Directory, ReparsePoint" if path in self.dir_links else "ReparsePoint"
            return _MockAttrs("link", 0, 1.0, mode)
        if self._exists_dir(path):
            return _MockAttrs("dir", 0, 1.0, "Directory")
        if path in self.files:
            data = self.files[path]
            return _MockAttrs("file", len(data), 1.0, "Archive")
        # Also accept forward-slash keys that got normalized into files
        for k, data in self.files.items():
            if self._norm(k) == path:
                return _MockAttrs("file", len(data), 1.0, "Archive")
        raise FileNotFoundError(path)

    def readlink(self, path: str) -> str:
        path = self._norm(path)
        if path not in self.links:
            raise OSError(f"not a reparse point: {path}")
        return self.links[path]

    def mkdir(self, path: str) -> None:
        path = self._norm(path)
        parent = path.rsplit("\\", 1)[0]
        if len(parent) == 2 and parent[1] == ":":
            parent = parent + "\\"
        # Parent may be a real dir or a reparse/symlink-to-directory.
        if parent and not self._is_dir_entry(parent) and parent != path:
            raise FileNotFoundError(parent)
        self.dirs.add(path)

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path in self.links:
            del self.links[path]
            self.dir_links.discard(path)
            return
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def rmdir(self, path: str) -> None:
        path = self._norm(path)
        if not self._exists_dir(path):
            raise FileNotFoundError(path)
        # non-empty?
        prefix = path.rstrip("\\") + "\\"
        for f in self.files:
            if self._norm(f).startswith(prefix):
                raise OSError("directory not empty")
        for d in self.dirs:
            dn = self._norm(d)
            if dn != path and dn.startswith(prefix):
                raise OSError("directory not empty")
        # Remove matching dir key(s)
        to_drop = [d for d in self.dirs if self._norm(d) == path]
        for d in to_drop:
            self.dirs.discard(d)

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        path = self._norm(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if max_bytes is not None:
            return data[: max(0, int(max_bytes))]
        return data

    def write_file(self, path: str, data: bytes) -> None:
        path = self._norm(path)
        # Final-component link resolution is PRODUCTION's job (WinrmFs.write /
        # put and PypsrpFileClient.write_file resolve the reparse chain first).
        # This store deliberately does not follow links: resolving here would
        # mask a missing resolve and let the symlink-preservation tests pass
        # against the fake instead of against the backend.
        self.files[path] = bytes(data)

    def copy(self, local: str, remote: str) -> None:
        remote = self._norm(remote)
        data = Path(local).read_bytes()
        self.files[remote] = data

    def fetch(self, remote: str, local: str) -> None:
        remote = self._norm(remote)
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])


class _MockWinRMSession:
    """Injectable WinRM session exposing open_fs + exec stubs."""

    def __init__(self, store: MockWinrmFileClient | None = None) -> None:
        self.cwd = HOME
        self.home = HOME
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self._store = store or MockWinrmFileClient()

    def close(self) -> None:
        self.closed = True

    def open_fs(self) -> MockWinrmFileClient:
        return self._store

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="", cwd=cwd or self.cwd)


def _connector_for(store: MockWinrmFileClient):
    def connector(**_kwargs: object) -> _MockWinRMSession:
        return _MockWinRMSession(store)

    return connector


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


def _backend(store: MockWinrmFileClient | None = None) -> tuple[MockWinrmFileClient, WinrmFs]:
    store = store or MockWinrmFileClient()
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    return store, backend


# ---------------------------------------------------------------------------
# Direct backend: all 8 ops
# ---------------------------------------------------------------------------


def test_winrm_fs_list() -> None:
    store, backend = _backend()
    store.files[rf"{TEMP}\readme.txt"] = b"hello\n"
    store.dirs.add(rf"{TEMP}\sub")

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("op") == "list"
    assert r.fields.get("path") == TEMP
    assert "\\" in r.fields["path"] or r.fields["path"].startswith("C:")
    body = r.body or ""
    assert "readme.txt" in body
    assert "sub" in body
    if r.fields.get("via"):
        assert r.fields["via"] == "winrm"
    text = r.render_text()
    assert text.startswith("@fs list ok")
    if "via=" in text:
        assert "via=winrm" in text


def test_winrm_fs_stat_file() -> None:
    store, backend = _backend()
    target = rf"{TEMP}\statme.txt"
    store.files[target] = b"hello"

    r = fs_ops.run(
        "stat",
        ep="lab-win",
        path=target,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("op") == "stat"
    assert r.fields.get("path") == target
    assert r.fields.get("type") == "file"
    assert r.fields.get("bytes") == 5


def test_winrm_fs_stat_not_found() -> None:
    _, backend = _backend()
    r = fs_ops.run(
        "stat",
        ep="lab-win",
        path=rf"{TEMP}\missing-xyz.txt",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"
    assert "not found" in (r.fields.get("msg") or "").lower()
    text = r.render_text()
    assert "NOT_FOUND" in text


def test_winrm_fs_read_write() -> None:
    store, backend = _backend()
    target = rf"{TEMP}\rw.txt"

    r_w = fs_ops.run(
        "write",
        ep="lab-win",
        path=target,
        content="line1\nline2\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r_w.status == "ok"
    assert r_w.fields.get("path") == target
    assert r_w.fields.get("bytes") == len("line1\nline2\n")
    assert store.files[target] == b"line1\nline2\n"

    r_r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        home=FIXTURES,
        backend=backend,
    )
    assert r_r.status == "ok"
    assert r_r.fields.get("type") == "text"
    assert r_r.body is not None
    assert "line1" in r_r.body
    assert r_r.fields.get("path") == target


def test_winrm_fs_put_get(tmp_path: Path) -> None:
    store, backend = _backend()
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-bytes")
    remote = rf"{TEMP}\remote\out.bin"

    r_put = fs_ops.run(
        "put",
        ep="lab-win",
        path=remote,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r_put.status == "ok"
    assert r_put.fields.get("path") == remote
    assert r_put.fields.get("bytes") == len(b"payload-bytes")
    assert store.files[remote] == b"payload-bytes"

    dest = tmp_path / "downloaded.bin"
    r_get = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(dest),
        home=FIXTURES,
        backend=backend,
    )
    assert r_get.status == "ok"
    assert r_get.fields.get("path") == remote
    assert dest.read_bytes() == b"payload-bytes"


def test_winrm_fs_mkdir_rm() -> None:
    store, backend = _backend()
    d = rf"{TEMP}\newdir\nested"

    r_m = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=d,
        home=FIXTURES,
        backend=backend,
    )
    assert r_m.status == "ok"
    assert r_m.fields.get("path") == d
    assert r_m.fields.get("type") == "dir"
    assert store._exists_dir(d)

    nested_file = rf"{d}\f.txt"
    store.files[nested_file] = b"x"

    # Non-recursive rm on dir fails clearly
    r_bad = fs_ops.run(
        "rm",
        ep="lab-win",
        path=d,
        home=FIXTURES,
        backend=backend,
    )
    assert r_bad.status == "error"
    assert r_bad.code in {"IS_A_DIR", "FS_ERROR"}

    r_rm = fs_ops.run(
        "rm",
        ep="lab-win",
        path=d,
        recursive=True,
        home=FIXTURES,
        backend=backend,
    )
    assert r_rm.status == "ok"
    assert r_rm.fields.get("path") == d
    assert not store._exists_dir(d)
    assert nested_file not in store.files

    # File rm
    f = rf"{TEMP}\gone.txt"
    store.files[f] = b"bye"
    r_f = fs_ops.run(
        "rm",
        ep="lab-win",
        path=f,
        home=FIXTURES,
        backend=backend,
    )
    assert r_f.status == "ok"
    assert f not in store.files


def test_winrm_fs_read_not_found() -> None:
    _, backend = _backend()
    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=rf"{TEMP}\definitely-missing-xyz.bin",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"


def test_winrm_fs_absolute_path_semantics() -> None:
    """Relative paths resolve against cwd; results use absolute Windows paths."""
    store, backend = _backend()
    store.files[rf"{HOME}\rel.txt"] = b"via-cwd\n"

    r = fs_ops.run(
        "stat",
        ep="lab-win",
        path="rel.txt",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("path") == rf"{HOME}\rel.txt"
    assert r.fields["path"].startswith("C:")


# ---------------------------------------------------------------------------
# end-to-end: ensure_endpoint + open_fs on mock connection
# ---------------------------------------------------------------------------


def test_winrm_fs_via_endpoint_open_fs() -> None:
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\hello.txt"] = b"winrm-hello\n"
    connector = _connector_for(store)

    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("path") == TEMP
    assert "hello.txt" in (r.body or "")
    if r.fields.get("via"):
        assert r.fields["via"] == "winrm"

    r2 = fs_ops.run(
        "read",
        ep="lab-win",
        path=rf"{TEMP}\hello.txt",
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok"
    assert "winrm-hello" in (r2.body or "")
    assert r2.fields["path"] == rf"{TEMP}\hello.txt"


def test_winrm_fs_via_file_client_injection() -> None:
    """file_client= injects store without requiring open_fs on session."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\inject.txt"] = b"injected\n"

    # Session without open_fs - backend uses injected file_client.
    def bare_connector(**_kwargs: object) -> _MockWinRMSession:
        sess = _MockWinRMSession(store)
        # Keep open_fs; injection path still works when file_client passed.
        return sess

    reg = get_registry()
    reg.winrm_connector = bare_connector  # type: ignore[assignment]

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=rf"{TEMP}\inject.txt",
        home=FIXTURES,
        connector=bare_connector,
        file_client=store,
    )
    assert r.status == "ok"
    assert "injected" in (r.body or "")
    if r.fields.get("via"):
        assert r.fields["via"] == "winrm"


def test_winrm_fs_endpoint_not_found() -> None:
    store = MockWinrmFileClient()
    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]

    r = fs_ops.run(
        "stat",
        ep="lab-win",
        path=rf"{TEMP}\nope.txt",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"


def test_winrm_fs_all_ops_roundtrip(tmp_path: Path) -> None:
    """Single store exercises list|stat|read|write|put|get|mkdir|rm."""
    store = MockWinrmFileClient()
    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]

    work = rf"{TEMP}\roundtrip"

    r_mkdir = fs_ops.run(
        "mkdir", ep="lab-win", path=work, home=FIXTURES, connector=connector
    )
    assert r_mkdir.status == "ok"

    note = rf"{work}\note.txt"
    r_write = fs_ops.run(
        "write",
        ep="lab-win",
        path=note,
        content="roundtrip-body\n",
        home=FIXTURES,
        connector=connector,
    )
    assert r_write.status == "ok"

    r_list = fs_ops.run(
        "list", ep="lab-win", path=work, home=FIXTURES, connector=connector
    )
    assert r_list.status == "ok"
    assert "note.txt" in (r_list.body or "")

    r_stat = fs_ops.run(
        "stat", ep="lab-win", path=note, home=FIXTURES, connector=connector
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "file"

    r_read = fs_ops.run(
        "read", ep="lab-win", path=note, home=FIXTURES, connector=connector
    )
    assert r_read.status == "ok"
    assert "roundtrip-body" in (r_read.body or "")

    local_src = tmp_path / "up.bin"
    local_src.write_bytes(b"\x00\x01\x02")
    remote_bin = rf"{work}\up.bin"
    r_put = fs_ops.run(
        "put",
        ep="lab-win",
        path=remote_bin,
        local=str(local_src),
        home=FIXTURES,
        connector=connector,
    )
    assert r_put.status == "ok"

    local_dst = tmp_path / "down.bin"
    r_get = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote_bin,
        local=str(local_dst),
        home=FIXTURES,
        connector=connector,
    )
    assert r_get.status == "ok"
    assert local_dst.read_bytes() == b"\x00\x01\x02"

    r_rm = fs_ops.run(
        "rm",
        ep="lab-win",
        path=work,
        recursive=True,
        home=FIXTURES,
        connector=connector,
    )
    assert r_rm.status == "ok"
    assert note not in store.files


# ---------------------------------------------------------------------------
# Focused pypsrp mock + per-fix regression cases
# (the ``FakePypsrpSession`` harness now lives in ``_winrm_fakes.py``).
# ---------------------------------------------------------------------------


def test_winrm_fs_empty_dir_returns_zero_not_fs_error() -> None:
    """listdir on an empty existing dir returns [], not FS_ERROR."""
    sess = FakePypsrpSession()
    empty = rf"{TEMP}\empty"
    sess.dirs.add(empty)

    # Direct adapter call: PypsrpFileClient.listdir must not raise on empty.
    client = PypsrpFileClient(sess)
    assert client.listdir(empty) == []
    # list_with_attrs also returns [] (the path WinrmFs.list takes).
    assert client.list_with_attrs(empty) == []

    # End-to-end via WinrmFs.list: ok (not FS_ERROR), 0 children.
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    res = backend.list(empty)
    assert [e.name for e in res.entries] == [".", ".."]

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=empty,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # 2 entries = only `.` and `..`; no real children.
    assert r.fields.get("n") == 2
    body = r.body or ""
    assert "." in body and ".." in body
    # No spurious child leaked through.
    assert "empty" not in body.replace("..", "")


def test_winrm_fs_read_max_bytes_pushes_bounded_read() -> None:
    """read max_bytes=K sends a bounded-read PS script, not ReadAllBytes."""
    sess = FakePypsrpSession()
    big = rf"{TEMP}\big.bin"
    # Pretend a 64 KiB file; the fix must only ship K bytes over WinRM.
    sess.files[big] = b"A" * 65536

    client = PypsrpFileClient(sess)
    sess.ps_calls.clear()
    data = client.read_file(big, max_bytes=10)
    assert len(data) == 10
    # Exactly one PS call, and it uses a bounded FileStream read.
    assert len(sess.ps_calls) == 1
    script = sess.ps_calls[0]
    assert "[IO.File]::Open(" in script
    assert "$maxN = 10" in script
    assert "ReadAllBytes" not in script

    # End-to-end via WinrmFs.read: only K bytes transferred & returned.
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    sess.ps_calls.clear()
    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=big,
        max_bytes=10,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == 10
    # The read PS script (not the stat script) is the bounded one.
    read_scripts = [
        s for s in sess.ps_calls if "[IO.File]::Open(" in s or "ReadAllBytes" in s
    ]
    assert read_scripts
    assert "[IO.File]::Open(" in read_scripts[-1]
    assert "ReadAllBytes" not in read_scripts[-1]


def test_winrm_fs_put_with_progress_streaming_copy_no_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_put_with_progress with a streaming copy client must not load the
    whole source into memory (no src.read_bytes() when copy succeeds)."""

    class _StreamCopyClient:
        """Has copy (streams) but no open; copy does not read_bytes locally."""

        has_native_copy = True

        def __init__(self) -> None:
            self.copy_calls: list[tuple[str, str]] = []
            self.rename_calls: list[tuple[str, str]] = []
            self.remove_calls: list[str] = []
            self.files: dict[str, bytes] = {}
            self._dirs: set[str] = {"C:\\", TEMP, rf"{TEMP}\dst"}

        def stat(self, path: str) -> _MockAttrs:
            if path in self._dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                data = self.files[path]
                return _MockAttrs("file", len(data), 1.0, "Archive")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self._dirs.add(path)

        def copy(self, local: str, remote: str) -> None:
            # Streaming copy: server-side, does NOT Path.read_bytes.
            # Length matches the source so native put can verify dest size.
            self.copy_calls.append((local, remote))
            self.files[remote] = b"X" * Path(local).stat().st_size

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            if src not in self.files:
                raise FileNotFoundError(src)
            self.files[dst] = self.files.pop(src)

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.files.pop(path, None)

    src = tmp_path / "big.bin"
    src.write_bytes(b"X" * 4096)
    remote = rf"{TEMP}\dst\out.bin"

    client = _StreamCopyClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    # Spy on Path.read_bytes; the fixed code path must not call it.
    read_calls = {"n": 0}
    orig_read_bytes = Path.read_bytes

    def _spy(self: Path) -> bytes:
        read_calls["n"] += 1
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _spy)

    progress_log: list[tuple[int, int | None]] = []
    result = backend.put(str(src), remote, progress=lambda d, t: progress_log.append((d, t)))

    assert client.copy_calls, "native copy must run"
    # Script-FS put copies onto a sibling temp, then promotes onto dest.
    assert client.copy_calls[0][1] != remote
    assert ".mrc-tmp-" in client.copy_calls[0][1]
    assert client.rename_calls and client.rename_calls[0][1] == remote
    assert client.files.get(remote) == b"X" * 4096
    assert read_calls["n"] == 0, "source read_bytes must not be called when copy streams"
    assert result.bytes_transferred == 4096
    # Progress reaches total (file size) without ever loading bytes.
    assert progress_log[-1] == (4096, 4096)



def test_winrm_fs_read_utf16le_text_returns_text_body() -> None:
    """A UTF-16LE text file (BOM) reads as is_text with a utf-16 encoding,
    not a binary-omitted body."""
    store, backend = _backend()
    target = rf"{TEMP}\u16.txt"
    text = "hello-winrm"
    # UTF-16LE with BOM (Notepad-style).
    store.files[target] = b"\xff\xfe" + text.encode("utf-16-le")

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    enc = r.fields.get("encoding")
    assert enc in {"utf-16", "utf-16-le", "utf-16-be"}
    body = r.body or ""
    assert text in body, "decoded body must contain the original text"
    assert r.fields.get("bytes") == len(b"\xff\xfe" + text.encode("utf-16-le"))


def test_winrm_fs_read_utf16le_no_bom_detected_as_text() -> None:
    """UTF-16LE without a BOM (alternating-NUL ASCII) is still detected as
    text via the heuristic, not misreported as binary."""
    store, backend = _backend()
    target = rf"{TEMP}\u16nobom.txt"
    text = "plain-ascii-content"
    raw = text.encode("utf-16-le")
    # Sanity: ASCII UTF-16LE has NUL at every odd index.
    assert b"\x00" in raw and raw[1] == 0
    store.files[target] = raw

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16-le"
    assert text in (r.body or "")


def test_winrm_fs_read_binary_with_nul_stays_binary() -> None:
    """Random binary with a NUL byte is not misclassified as UTF-16."""
    store, backend = _backend()
    target = rf"{TEMP}\bin.dat"
    # NUL present but NOT in an alternating pattern -> not UTF-16.
    store.files[target] = b"\x00\x01\x02\x03\x04\x05binary"

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


# ---------------------------------------------------------------------------
# Directory destination: put/write must reject, never "succeed" by moving the
# payload inside the directory (Move-Item container semantics).
# ---------------------------------------------------------------------------


def _assert_dir_dest_rejected(r: object, dest: str, store: MockWinrmFileClient) -> None:
    """The row is IS_A_DIR for *dest* and nothing was created anywhere."""
    assert r.status == "error", r
    assert r.code == "IS_A_DIR", (r.code, r.fields)  # type: ignore[attr-defined]
    assert r.fields.get("path") == dest  # type: ignore[attr-defined]
    assert dest not in store.files
    assert not any(".mrc-tmp-" in k for k in store.files), (
        "a rejected put must not leave a temp inside the destination directory"
    )


def test_winrm_put_directory_destination_rejected(tmp_path: Path) -> None:
    """``fs put`` onto an existing directory path fails IS_A_DIR.

    The promote is a replace (existing target) or an unforced ``Move-Item``
    (new target); for an existing *directory* the move would put the temp
    INSIDE it, so an ok row would mean "uploaded" while the requested
    path was never created. LocalFs/``WinrmFs.write`` reject the same input.
    """
    store, backend = _backend()
    drop = rf"{TEMP}\drop"
    store.dirs.add(drop)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=drop,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    _assert_dir_dest_rejected(r, drop, store)
    assert store.files == {}
    # Nothing was hidden inside the directory either.
    assert not any(k.startswith(drop + "\\") for k in store.files)


def test_winrm_write_directory_destination_rejected() -> None:
    """``fs write`` onto an existing directory path fails IS_A_DIR."""
    store, backend = _backend()
    drop = rf"{TEMP}\drop"
    store.dirs.add(drop)

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=drop,
        content="payload-must-not-land",
        home=FIXTURES,
        backend=backend,
    )
    _assert_dir_dest_rejected(r, drop, store)


def test_winrm_put_symlink_to_directory_rejected(tmp_path: Path) -> None:
    """A link whose referent is a directory is the same directory destination.

    The final-component resolve must not launder a directory into a "link"
    path and then let the promote drop the payload inside the referent.
    """
    store, backend = _backend()
    real_dir = rf"{TEMP}\realdir"
    link = rf"{TEMP}\dirlink"
    store.dirs.add(real_dir)
    store.links[link] = real_dir
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "IS_A_DIR"
    # The link entry survives and its referent stayed a directory.
    assert store.links[link] == real_dir
    assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_put_dangling_symlink_still_writes_referent(tmp_path: Path) -> None:
    """Control: a link to a *missing* path is still a writable destination."""
    store, backend = _backend()
    target = rf"{TEMP}\missing-target.bin"
    link = rf"{TEMP}\dangling.bin"
    store.links[link] = target
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-dangling-link")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.files[target] == b"payload-via-dangling-link"
    assert link not in store.files
    assert store.links[link] == target


def test_winrm_put_dir_reparse_unreadable_target_rejected(tmp_path: Path) -> None:
    """A directory reparse point with an unreadable target is IS_A_DIR.

    Windows marks a junction / symlink-to-directory ``Directory,
    ReparsePoint``. When the target cannot be read there is nothing to follow,
    and promoting onto the reparse entry itself would hit ``Move-Item``'s
    container rule - payload inside the referent, success reported. The
    entry's own attributes are the only signal left, and they decide the
    verdict (the same one the readable-link case reaches by following).
    """
    store, backend = _backend()
    real_dir = rf"{TEMP}\realdir"
    link = rf"{TEMP}\dirlink"
    store.dirs.add(real_dir)
    store.links[link] = ""  # target unreadable
    store.dir_links.add(link)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    _assert_dir_dest_rejected(r, link, store)
    assert store.links[link] == ""
    assert store.files == {}


def test_winrm_put_file_reparse_unreadable_target_still_writes(tmp_path: Path) -> None:
    """Control: a *file* reparse point with an unreadable target is not a
    container, so the promote-onto-the-entry fallback is unchanged."""
    store, backend = _backend()
    link = rf"{TEMP}\filelink.bin"
    store.links[link] = ""
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok", (r.code, r.fields)
    assert store.files[link] == b"payload"


def test_winrm_put_link_probe_link_failure_not_swallowed(tmp_path: Path) -> None:
    """A link failure while probing the reparse target fails the op.

    ``_readlink`` returns ``None`` for an unsupported or unreadable target so
    the promote keeps its old behavior, but a link-class failure means the
    session is gone: falling back would promote onto the reparse entry (the
    link is clobbered) and report success on an endpoint the transport has
    retired.
    """
    store = _LinkProbeFailingClient()
    real = rf"{TEMP}\realdir"
    link = rf"{TEMP}\dirlink"
    store.dirs.add(real)
    store.links[link] = real
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=WinrmFs(store, cwd=HOME, home=HOME),
    )
    assert r.status == "error", (r.code, r.fields)
    assert r.code == "FS_ERROR", (r.code, r.fields)
    assert store.files == {}
    assert store.links[link] == real


# ---------------------------------------------------------------------------
# Gated hosts (ps_script_fs=false): the native copy has no Move-Item promote,
# so the IS_A_DIR verdict comes from the best-effort destination probe.
# ---------------------------------------------------------------------------


class _UnservableProbeClient(MockWinrmFileClient):
    """Gated-host client whose ``stat`` cannot be served (NoLanguage shape)."""

    def stat(self, path: str) -> _MockAttrs:
        raise OSError("PSIsContainer unavailable: script FS is gated")


class _LinkProbeFailingClient(MockWinrmFileClient):
    """Client whose ``readlink`` probe dies with a link-class failure."""

    def readlink(self, path: str) -> str:
        raise ConnectionError(f"connection reset while reading link target: {path}")


def _gated_backend(store: MockWinrmFileClient) -> WinrmFs:
    return WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps={"ps_script_fs": False, "language_mode": "NoLanguage"},
    )


def test_winrm_gated_put_directory_destination_rejected(tmp_path: Path) -> None:
    """``ps_script_fs=false``: a directory destination is still IS_A_DIR.

    The gated branch streams the caller's path straight through the native
    copy, where a container destination would swallow the payload and report
    success. The client's ``stat`` is the only probe left, and a destination
    that answers "directory" is rejected like it is on the script-FS path.
    """
    store = MockWinrmFileClient()
    drop = rf"{TEMP}\drop"
    store.dirs.add(drop)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=drop,
        local=str(src),
        home=FIXTURES,
        backend=_gated_backend(store),
    )
    _assert_dir_dest_rejected(r, drop, store)
    assert store.files == {}


def test_winrm_gated_put_dir_reparse_destination_rejected(tmp_path: Path) -> None:
    """A directory reparse point is a container for the gated copy too."""
    store = MockWinrmFileClient()
    real_dir = rf"{TEMP}\realdir"
    link = rf"{TEMP}\dirlink"
    store.dirs.add(real_dir)
    store.links[link] = ""  # unreadable target: only the attributes say "dir"
    store.dir_links.add(link)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=_gated_backend(store),
    )
    _assert_dir_dest_rejected(r, link, store)
    assert store.files == {}


def test_winrm_gated_put_new_path_still_copies(tmp_path: Path) -> None:
    """Control: the probe must not block the accepted gated put."""
    store = MockWinrmFileClient()
    dest = rf"{TEMP}\plain.bin"
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=_gated_backend(store),
    )
    assert r.status == "ok", (r.code, r.fields)
    assert store.files[dest] == b"payload"


def test_winrm_gated_put_unservable_probe_still_copies(tmp_path: Path) -> None:
    """A probe the gated host cannot answer leaves the verdict open.

    Script FS is gated precisely because the host could not serve those
    scripts, so an unanswerable ``stat`` carries no information: the native
    copy must still run rather than turn the gate into a failure.
    """
    store = _UnservableProbeClient()
    dest = rf"{TEMP}\plain.bin"
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=_gated_backend(store),
    )
    assert r.status == "ok", (r.code, r.fields)
    assert store.files[dest] == b"payload"


# ---------------------------------------------------------------------------
# Error attribution: an HTTP rejection of the WSMan exchange is not a verdict
# about the remote path, whatever its body happens to say.
# ---------------------------------------------------------------------------


class _HttpRejectingClient(MockWinrmFileClient):
    """Client whose every session call raises the injected rejection."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    def stat(self, path: str) -> _MockAttrs:
        raise self._exc

    def mkdir(self, path: str) -> None:
        raise self._exc

    def write_file(self, path: str, data: bytes) -> None:
        raise self._exc


def _put_through(client: object, tmp_path: Path) -> object:
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload")
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    return fs_ops.run(
        "put",
        ep="lab-win",
        path=rf"{TEMP}\out.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )


def test_winrm_gateway_error_body_is_not_a_path_verdict(
    tmp_path: Path,
) -> None:
    """A gateway 404/403 page must not become NOT_FOUND / PERMISSION_DENIED.

    ``WinRMTransportError`` means the HTTP exchange was rejected: the body
    belongs to the server or an intermediary, not to the remote filesystem,
    so no path verdict may be read out of it (pypsrp only converts a
    *parseable* WSMan fault, which is the remote answer, into a fault error).
    """
    cases = [
        (404, "<html><head><title>404 Not Found</title></head></html>"),
        (403, "<html><body>Access is denied.</body></html>"),
    ]
    for status, body in cases:
        r = _put_through(
            _HttpRejectingClient(pypsrp.exceptions.WinRMTransportError("http", status, body)),
            tmp_path,
        )
        assert r.status == "error"
        assert r.code == "FS_ERROR", (status, r.code, r.fields)
        assert f"Code: {status}" in (r.fields.get("msg") or "")


def test_winrm_genuine_remote_verdicts_still_map(tmp_path: Path) -> None:
    """Control: text produced by the remote filesystem keeps its meaning."""
    r = _put_through(
        _HttpRejectingClient(
            RuntimeError(
                "Cannot find path 'C:\\temp\\out.bin' because it does not exist."
            )
        ),
        tmp_path,
    )
    assert r.code == "NOT_FOUND"

    r = _put_through(
        _HttpRejectingClient(
            FsError("NOT_FOUND", "path not found: C:\\temp\\out.bin")
        ),
        tmp_path,
    )
    assert r.code == "NOT_FOUND"
    assert "out.bin" in (r.fields.get("msg") or "")

    r = _put_through(
        _HttpRejectingClient(
            RuntimeError("Cannot remove item: Access is denied.")
        ),
        tmp_path,
    )
    assert r.code == "PERMISSION_DENIED"


def test_winrm_empty_body_rejection_stays_fs_error(tmp_path: Path) -> None:
    """The measured stale-framing shape (400, empty body) is FS_ERROR."""
    r = _put_through(
        _HttpRejectingClient(pypsrp.exceptions.WinRMTransportError("http", 400, "")),
        tmp_path,
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    assert "Code: 400" in (r.fields.get("msg") or "")
