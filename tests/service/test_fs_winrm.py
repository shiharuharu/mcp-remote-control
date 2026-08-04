"""Service tests: fs winrm backend — all 8 ops + NOT_FOUND (T15, mock only)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# O11: shared pypsrp fakes live in the sibling support module so the O6 PS
# interpreter is reused (not duplicated) by the new PypsrpFileClient tests.
# pytest's default ``prepend`` import mode puts this file's directory on
# sys.path, so a bare ``from _winrm_fakes import ...`` resolves.
from _winrm_fakes import HOME, TEMP, FakePypsrpSession, _path_after

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.fs.backends.winrm import PypsrpFileClient, WinrmFs
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport.base import ExecResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# Mock filesystem: dict of paths → content / meta
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

    # Real in-memory copy/fetch — opt in so SupportsCopyFetch is treated as native.
    has_native_copy = True
    has_native_fetch = True

    def __init__(self) -> None:
        # Normalized paths use backslash; drive roots always present.
        self.files: dict[str, bytes] = {}
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
        return sorted(set(names))

    def stat(self, path: str) -> _MockAttrs:
        path = self._norm(path)
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

    def mkdir(self, path: str) -> None:
        path = self._norm(path)
        parent = path.rsplit("\\", 1)[0]
        if len(parent) == 2 and parent[1] == ":":
            parent = parent + "\\"
        if parent and not self._exists_dir(parent) and parent != path:
            raise FileNotFoundError(parent)
        self.dirs.add(path)

    def remove(self, path: str) -> None:
        path = self._norm(path)
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

    # Session without open_fs — backend uses injected file_client.
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
# O6 backend patches: focused pypsrp mock + per-fix regression cases
# (the ``FakePypsrpSession`` harness now lives in ``_winrm_fakes.py`` — O11
# consolidated the O6 local copy + extended it with copy/fetch delegation).
# ---------------------------------------------------------------------------


def test_winrm_fs_empty_dir_returns_zero_not_fs_error() -> None:
    """H3: listdir on an empty existing dir returns [], not FS_ERROR."""
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
    """H4: read max_bytes=K sends a bounded-read PS script, not ReadAllBytes."""
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


def test_winrm_fs_recursive_list_depth_and_prefix_overlap() -> None:
    """H2: recursive list names are correct at depth >=3 and when a child's
    basename starts with its parent's basename."""
    store, backend = _backend()
    # Depth-4 chain under TEMP\foo: foo\bar\baz\qux.txt
    for d in (rf"{TEMP}\foo", rf"{TEMP}\foo\bar", rf"{TEMP}\foo\bar\baz"):
        store.dirs.add(d)
    deep = rf"{TEMP}\foo\bar\baz\qux.txt"
    store.files[deep] = b"leaf"
    # Prefix-overlap direct child: foobar.txt under foo (starts with "foo").
    overlap = rf"{TEMP}\foo\foobar.txt"
    store.files[overlap] = b"ovr"

    res = backend.list(TEMP, recursive=True)
    by_path = {e.path: e for e in res.entries}
    # Depth-4 entry: name is the full relative path from TEMP, not truncated.
    e_deep = by_path[deep]
    assert e_deep.name == r"foo\bar\baz\qux.txt"
    assert e_deep.path == deep
    # Prefix-overlap entry: keeps the "foo\" prefix.
    e_ovr = by_path[overlap]
    assert e_ovr.name == r"foo\foobar.txt"
    assert e_ovr.path == overlap


def test_winrm_fs_list_uses_batched_attrs_no_per_child_stat() -> None:
    """F: list with N entries makes O(1) child round-trips; `.` reuses dir attrs."""
    sess = FakePypsrpSession()
    parent = rf"{TEMP}\batch"
    sess.dirs.add(parent)
    # N children (mix of files and dirs).
    for i in range(5):
        sess.files[rf"{parent}\f{i}.txt"] = b"x" * i
    sess.dirs.add(rf"{parent}\sub")

    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    sess.ps_calls.clear()
    res = backend.list(parent)

    # Expect exactly 3 PS round-trips: stat(dir), list_with_attrs(dir),
    # stat(parent-of-dir for `..`). No per-child stat.
    assert len(sess.ps_calls) == 3
    child_scripts = [s for s in sess.ps_calls if "name=$_.Name" in s]
    assert len(child_scripts) == 1, "list_with_attrs should fire exactly once"
    # `.` reuses the dir stat: the stat script targeting `parent` runs once.
    dir_stat_scripts = [
        s
        for s in sess.ps_calls
        if "Get-Item -LiteralPath" in s
        and _path_after(s, "Get-Item -LiteralPath ") == parent
    ]
    assert len(dir_stat_scripts) == 1, "dir stat must not be repeated for `.`"
    # All children present with correct kinds/sizes.
    by_name = {e.name: e for e in res.entries}
    assert by_name["sub"].kind == "d"
    assert by_name["f3.txt"].kind == "f"
    assert by_name["f3.txt"].size == 3


def test_winrm_fs_put_with_progress_streaming_copy_no_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F: _put_with_progress with a streaming copy client must not load the
    whole source into memory (no src.read_bytes() when copy succeeds)."""

    class _StreamCopyClient:
        """Has copy (streams) but no open; copy does not read_bytes locally."""

        def __init__(self) -> None:
            self.copy_calls: list[tuple[str, str]] = []
            self.remove_calls: list[str] = []
            self._dirs: set[str] = {"C:\\", TEMP, rf"{TEMP}\dst"}

        def stat(self, path: str) -> _MockAttrs:
            if path in self._dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self._dirs.add(path)

        def copy(self, local: str, remote: str) -> None:
            # Streaming copy: server-side, does NOT read_bytes in our process.
            self.copy_calls.append((local, remote))

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)

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

    assert client.copy_calls and client.copy_calls[0][1] == remote
    assert read_calls["n"] == 0, "source read_bytes must not be called when copy streams"
    assert result.bytes_transferred == 4096
    # Progress reaches total (file size) without ever loading bytes.
    assert progress_log[-1] == (4096, 4096)


def test_winrm_fs_partial_upload_cleans_up_remote(tmp_path: Path) -> None:
    """C: a failed chunked upload best-effort removes the partial remote file,
    AND the handle is closed BEFORE the remove — on Windows deleting an open
    file fails with "file in use", so the cleanup order matters."""

    class _FailingHandle:
        def __init__(self, path: str, log: list[str]) -> None:
            self._path = path
            self._log = log
            self.writes = 0

        def write(self, chunk: bytes) -> None:
            self.writes += 1
            if self.writes >= 2:
                raise OSError("simulated mid-stream transport failure")

        def close(self) -> None:
            self._log.append(f"close:{self._path}")

    class _OpenClient:
        def __init__(self) -> None:
            self.opened: list[str] = []
            self.remove_calls: list[str] = []
            # Ordered event log so we can assert close precedes remove.
            self.events: list[str] = []
            self._dirs: set[str] = {"C:\\", TEMP, rf"{TEMP}\dst"}

        def stat(self, path: str) -> _MockAttrs:
            if path in self._dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self._dirs.add(path)

        def open(self, path: str, mode: str) -> _FailingHandle:
            self.opened.append(path)
            return _FailingHandle(path, self.events)

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.events.append(f"remove:{path}")

    src = tmp_path / "payload.bin"
    # Larger than one DEFAULT_TRANSFER_CHUNK so the loop reaches write #2.
    src.write_bytes(b"Y" * (256 * 1024 + 10))
    remote = rf"{TEMP}\dst\partial.bin"

    client = _OpenClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    with pytest.raises(Exception):  # noqa: B017
        backend.put(str(src), remote, progress=lambda d, t: None)

    assert client.opened and client.opened[0] == remote
    assert client.remove_calls and client.remove_calls[0] == remote, (
        "partial remote file must be best-effort removed on failure"
    )
    # Handle closed before the remove: the close event must precede the remove
    # event so Windows doesn't reject the delete of an open file. The old
    # ordering (remove in `except`, close in `finally` after `raise`) left the
    # handle open during remove and would fail on a real Windows host.
    closes = [i for i, e in enumerate(client.events) if e.startswith("close:")]
    removes = [i for i, e in enumerate(client.events) if e.startswith("remove:")]
    assert closes and removes, "both close and remove must have fired"
    assert closes[0] < removes[0], (
        "handle must be closed BEFORE remove (Windows rejects deleting an "
        "open file); events=" + repr(client.events)
    )


def test_winrm_fs_recursive_list_terminates_on_junction_cycle() -> None:
    """C4: a junction reported by list_with_attrs as kind='dir' and pointing
    back through itself (each level reappears one path deeper) must terminate
    instead of stack-overflowing. Guards the visited-set + depth-limit cycle
    protection added to the recursive list branch."""

    class _CycleClient:
        """Every dir contains a 'loop' child dir (kind='dir'), modeling a
        junction that reappears through itself: listing <path> yields a child
        at <path>\\loop, so the recursion produces an ever-growing path that
        a visited-set alone never matches — only the depth backstop catches
        it. Without protection the recursion is unbounded (RecursionError)."""

        def stat(self, path: str) -> _MockAttrs:
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def list_with_attrs(self, path: str) -> list[dict[str, object]]:
            return [
                {
                    "name": "loop",
                    "kind": "dir",
                    "size": 0,
                    "mtime": "2024-01-01T00:00:00Z",
                    "mode": "Directory",
                }
            ]

    client = _CycleClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    # Must terminate (depth-limit backstop fires) rather than hang or
    # RecursionError. If cycle protection is missing this raises before the
    # assertion.
    res = backend.list(TEMP, recursive=True)
    names = [e.name for e in res.entries]
    # The first-level loop child is listed.
    assert "loop" in names, "first loop child must be listed"
    # Bounded by max_depth (40): the deepest name has a bounded number of
    # 'loop' segments. A broken guard would have raised RecursionError above
    # before reaching this assertion; this is a sanity bound on the depth cap.
    assert all(n.count("loop") <= 41 for n in names), (
        "recursion depth exceeded the max_depth cap"
    )


def test_winrm_fs_read_utf16le_text_returns_text_body() -> None:
    """G: a UTF-16LE text file (BOM) reads as is_text with a utf-16 encoding,
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
    """G: UTF-16LE without a BOM (alternating-NUL ASCII) is still detected as
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
    """G guard: random binary with a NUL byte is NOT misclassified as UTF-16."""
    store, backend = _backend()
    target = rf"{TEMP}\bin.dat"
    # NUL present but NOT in an alternating pattern → not UTF-16.
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
# O11 Gap 1/2/5: PypsrpFileClient production-path coverage. The original
# review flagged the real pypsrp adapter (stat / write_file / copy / fetch
# delegation / end-to-end WinrmFs THROUGH PypsrpFileClient) as zero-tested.
# These tests exercise those paths against FakePypsrpSession (the shared
# O6 PS interpreter) without a Windows host.
# ---------------------------------------------------------------------------


def test_winrm_pypsrp_file_client_stat_file_dir_not_found() -> None:
    """Gap 1: PypsrpFileClient.stat (production PS-stat path) returns the
    expected dict for a file and a dir, and raises FileNotFoundError for a
    missing path (the PS-stat NOT_FOUND branch)."""
    sess = FakePypsrpSession()
    file_path = rf"{TEMP}\statme.txt"
    sess.files[file_path] = b"hello"
    dir_path = rf"{TEMP}\statdir"
    sess.dirs.add(dir_path)

    client = PypsrpFileClient(sess)

    # File: kind=file, size=len(data), mode=Archive, mtime present.
    st_file = client.stat(file_path)
    assert st_file["kind"] == "file"
    assert st_file["size"] == 5
    assert st_file["mode"] == "Archive"
    assert st_file["mtime"] == FakePypsrpSession.MTIME

    # Dir: kind=dir, size=0, mode=Directory.
    st_dir = client.stat(dir_path)
    assert st_dir["kind"] == "dir"
    assert st_dir["size"] == 0
    assert st_dir["mode"] == "Directory"

    # Missing: FileNotFoundError (the PS-stat NOT_FOUND branch in _run_json).
    with pytest.raises(FileNotFoundError):
        client.stat(rf"{TEMP}\nope.txt")

    # One Get-Item + PSIsContainer stat script fired per call.
    stat_scripts = [
        s
        for s in sess.ps_calls
        if "Get-Item -LiteralPath" in s and "PSIsContainer" in s
    ]
    assert len(stat_scripts) == 3


def test_winrm_pypsrp_file_client_write_file_round_trips() -> None:
    """Gap 2: PypsrpFileClient.write_file emits a WriteAllBytes PS script and
    the bytes land in the session FS at the exact path."""
    sess = FakePypsrpSession()
    client = PypsrpFileClient(sess)
    target = rf"{TEMP}\wrote.bin"
    payload = b"\x00\x01\x02 hello"

    client.write_file(target, payload)

    assert sess.files[target] == payload
    assert sess.ps_calls, "execute_ps was called"
    script = sess.ps_calls[-1]
    assert "[IO.File]::WriteAllBytes(" in script
    assert "FromBase64String(" in script


def test_winrm_pypsrp_file_client_copy_delegates_when_session_has_copy(
    tmp_path: Path,
) -> None:
    """Gap 2: when session.copy is present, PypsrpFileClient.copy delegates to
    it (the production pypsrp Client.copy streaming path) — no write_file PS
    script is emitted."""
    sess = FakePypsrpSession(has_copy=True)
    client = PypsrpFileClient(sess)
    src = tmp_path / "local.bin"
    src.write_bytes(b"copy-payload")
    remote = rf"{TEMP}\copied.bin"

    client.copy(str(src), remote)

    assert sess.copy_calls == [(str(src), remote)]
    assert sess.files[remote] == b"copy-payload"
    # No write_file PS script — native delegation, not the fallback.
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_copy_falls_back_to_write_file(
    tmp_path: Path,
) -> None:
    """Gap 2: when session.copy is absent, PypsrpFileClient.copy falls back to
    write_file (PS WriteAllBytes) so the bytes still land remotely."""
    sess = FakePypsrpSession()  # has_copy=False (default) → no `copy` attribute
    client = PypsrpFileClient(sess)
    src = tmp_path / "local.bin"
    src.write_bytes(b"fallback-payload")
    remote = rf"{TEMP}\copied.bin"

    client.copy(str(src), remote)

    assert sess.copy_calls == [], "no native delegation without session.copy"
    assert sess.files[remote] == b"fallback-payload"
    assert any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_fetch_delegates_when_session_has_fetch(
    tmp_path: Path,
) -> None:
    """Gap 2: when session.fetch is present, PypsrpFileClient.fetch delegates
    to it (the production pypsrp Client.fetch streaming path) — no read_file PS
    script is emitted."""
    sess = FakePypsrpSession(has_fetch=True)
    client = PypsrpFileClient(sess)
    remote = rf"{TEMP}\remote.bin"
    sess.files[remote] = b"fetched-payload"
    dst = tmp_path / "downloaded.bin"

    client.fetch(remote, str(dst))

    assert sess.fetch_calls == [(remote, str(dst))]
    assert dst.read_bytes() == b"fetched-payload"
    # No read_file PS script — native delegation, not the fallback.
    assert not any(
        "ReadAllBytes" in s or "[IO.File]::Open(" in s for s in sess.ps_calls
    )


def test_winrm_pypsrp_file_client_fetch_falls_back_to_read_file(
    tmp_path: Path,
) -> None:
    """Gap 2: when session.fetch is absent, PypsrpFileClient.fetch falls back
    to read_file (PS ReadAllBytes) and writes the bytes locally."""
    sess = FakePypsrpSession()  # has_fetch=False (default) → no `fetch` attribute
    client = PypsrpFileClient(sess)
    remote = rf"{TEMP}\remote.bin"
    sess.files[remote] = b"fallback-fetch"
    dst = tmp_path / "downloaded.bin"

    client.fetch(remote, str(dst))

    assert sess.fetch_calls == [], "no native delegation without session.fetch"
    assert dst.read_bytes() == b"fallback-fetch"
    assert any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_end_to_end_all_ops_roundtrip(
    tmp_path: Path,
) -> None:
    """Gap 5: end-to-end WinrmFs ops (list / stat / read / write / put / get /
    mkdir / rm) THROUGH PypsrpFileClient + FakePypsrpSession — exercises the
    production pypsrp adapter path for every op, not just isolated method tests.

    ``has_copy=True / has_fetch=True`` selects the production delegation path
    for put/get (pypsrp Client.copy / Client.fetch); the PS scripts back
    list / stat / read / write / mkdir / rm.
    """
    sess = FakePypsrpSession(has_copy=True, has_fetch=True)
    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    work = rf"{TEMP}\roundtrip"

    # mkdir (parents=True → _mkdir_p walks the chain via stat + mkdir PS).
    r_mkdir = fs_ops.run(
        "mkdir", ep="lab-win", path=work, home=FIXTURES, backend=backend
    )
    assert r_mkdir.status == "ok"
    assert work in sess.dirs

    # write (PS WriteAllBytes after a stat-to-check-isdir + parent mkdir_p).
    note = rf"{work}\note.txt"
    r_write = fs_ops.run(
        "write",
        ep="lab-win",
        path=note,
        content="roundtrip-body\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r_write.status == "ok"
    assert sess.files[note] == b"roundtrip-body\n"

    # list (PS Get-Item + list_with_attrs in O(1) child round-trips).
    r_list = fs_ops.run(
        "list", ep="lab-win", path=work, home=FIXTURES, backend=backend
    )
    assert r_list.status == "ok"
    assert "note.txt" in (r_list.body or "")

    # stat (PS Get-Item single-shot).
    r_stat = fs_ops.run(
        "stat", ep="lab-win", path=note, home=FIXTURES, backend=backend
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "file"
    assert r_stat.fields.get("bytes") == len(b"roundtrip-body\n")

    # read (bounded read PS script → base64 decode → text body).
    r_read = fs_ops.run(
        "read", ep="lab-win", path=note, home=FIXTURES, backend=backend
    )
    assert r_read.status == "ok"
    assert "roundtrip-body" in (r_read.body or "")

    # put (PypsrpFileClient.copy → session.copy delegation).
    local_src = tmp_path / "up.bin"
    local_src.write_bytes(b"\x00\x01\x02")
    remote_bin = rf"{work}\up.bin"
    r_put = fs_ops.run(
        "put",
        ep="lab-win",
        path=remote_bin,
        local=str(local_src),
        home=FIXTURES,
        backend=backend,
    )
    assert r_put.status == "ok"
    assert sess.copy_calls and sess.copy_calls[-1][1] == remote_bin
    assert sess.files[remote_bin] == b"\x00\x01\x02"

    # get (PypsrpFileClient.fetch → session.fetch delegation).
    local_dst = tmp_path / "down.bin"
    r_get = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote_bin,
        local=str(local_dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r_get.status == "ok"
    assert sess.fetch_calls and sess.fetch_calls[-1][0] == remote_bin
    assert local_dst.read_bytes() == b"\x00\x01\x02"

    # rm recursive (PS Remove-Item -Recurse drops the dir + all descendants).
    r_rm = fs_ops.run(
        "rm",
        ep="lab-win",
        path=work,
        recursive=True,
        home=FIXTURES,
        backend=backend,
    )
    assert r_rm.status == "ok"
    assert work not in sess.dirs
    assert note not in sess.files
    assert remote_bin not in sess.files


# ---------------------------------------------------------------------------
# Bounded read without Int32 Length cast; atomic get (temp + replace)
# ---------------------------------------------------------------------------


def test_winrm_read_file_bounded_script_no_int32_length() -> None:
    """Bounded read must not cast FileStream.Length to Int32.

    Files larger than 2 GiB make ``[int]$fs.Length`` throw in PowerShell before
    any bytes are returned. The script streams Read up to max_bytes and does
    not depend on Length.
    """
    sess = FakePypsrpSession()
    huge = rf"{TEMP}\huge.bin"
    sess.files[huge] = b"Z" * 4096

    client = PypsrpFileClient(sess)
    sess.ps_calls.clear()
    data = client.read_file(huge, max_bytes=10)
    assert data == b"Z" * 10
    assert len(sess.ps_calls) == 1
    script = sess.ps_calls[0]
    assert "[IO.File]::Open(" in script
    assert "$maxN = 10" in script
    assert "ReadAllBytes" not in script
    # No Int32 Length cast (the >2GB failure mode).
    assert "[int]$fs.Length" not in script
    assert "[int] $fs.Length" not in script
    # Bounded path must not depend on Length at all.
    assert "$fs.Length" not in script


def test_winrm_read_file_bounded_works_for_large_payload_head() -> None:
    """WinrmFs.read with max_bytes returns the head and uses Length-free PS."""
    sess = FakePypsrpSession()
    target = rf"{TEMP}\big-head.bin"
    payload = b"ABCDEFGHIJ" * 1000  # 10_000 bytes
    sess.files[target] = payload
    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        max_bytes=7,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == 7
    assert r.fields.get("truncated") is True
    read_scripts = [
        s for s in sess.ps_calls if "[IO.File]::Open(" in s or "ReadAllBytes" in s
    ]
    assert read_scripts
    assert "[int]$fs.Length" not in read_scripts[-1]
    assert "$fs.Length" not in read_scripts[-1]


def test_winrm_fs_get_atomic_failure_preserves_local(tmp_path: Path) -> None:
    """get uses same-dir temp + os.replace; mid-fetch failure preserves dst."""

    class _FailingFetchClient(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_targets: list[str] = []
            self.files[rf"{TEMP}\remote.bin"] = b"new-payload"

        def fetch(self, remote: str, local: str) -> None:
            self.fetch_targets.append(local)
            Path(local).write_bytes(b"partial-corrupt")
            raise OSError("simulated winrm fetch failure")

    client = _FailingFetchClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    dst = tmp_path / "downloaded.bin"
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=rf"{TEMP}\remote.bin",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"pre-existing-local-content"
    assert client.fetch_targets
    assert client.fetch_targets[0] != str(dst)
    assert ".mrc-tmp-" in Path(client.fetch_targets[0]).name
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


def test_winrm_fs_get_atomic_failure_with_progress_preserves_local(
    tmp_path: Path,
) -> None:
    """Progress/chunked get branch is also atomic (temp + replace)."""

    class _FailingHandle:
        def read(self, n: int) -> bytes:
            raise OSError("simulated mid-stream read failure")

        def close(self) -> None:
            return None

    class _OpenFailClient(MockWinrmFileClient):
        def open(self, path: str, mode: str = "rb") -> _FailingHandle:
            return _FailingHandle()

    client = _OpenFailClient()
    remote = rf"{TEMP}\stream.bin"
    client.files[remote] = b"X" * 100
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    dst = tmp_path / "streamed.bin"
    dst.write_bytes(b"keep-me")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        progress=lambda d, t: None,
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"keep-me"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


def test_winrm_fs_get_atomic_success_replaces_destination(tmp_path: Path) -> None:
    """Successful get replaces the local destination with remote bytes."""
    store, backend = _backend()
    remote = rf"{TEMP}\atom-get.bin"
    store.files[remote] = b"from-remote"
    dst = tmp_path / "atom-get.bin"
    dst.write_bytes(b"old-local")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert dst.read_bytes() == b"from-remote"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


# ---------------------------------------------------------------------------
# WinRM PS capability gate (notes/025 §八)
# ---------------------------------------------------------------------------


def _constrained_ps_caps() -> dict:
    return {
        "ps_version": "5.1.19041",
        "language_mode": "ConstrainedLanguage",
        "ps_script_fs": False,
        "ps_oneshot": True,
        "ps_runspace": False,
    }


def _full_ps_caps() -> dict:
    return {
        "ps_version": "5.1.19041",
        "language_mode": "FullLanguage",
        "ps_script_fs": True,
        "ps_oneshot": True,
        "ps_runspace": True,
        "ps_edition": "Desktop",
    }


def test_winrm_fs_list_unsupported_when_ps_script_fs_false() -> None:
    """ConstrainedLanguage / ps_script_fs=false → list UNSUPPORTED; no listdir."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\gate.txt"] = b"should-not-list"
    listdir_calls: list[str] = []
    orig = store.listdir

    def tracking_listdir(path: str) -> list[str]:
        listdir_calls.append(path)
        return orig(path)

    store.listdir = tracking_listdir  # type: ignore[method-assign]

    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps=_constrained_ps_caps(),
    )
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert "FullLanguage" in (r.fields.get("msg") or "") or "ps_script_fs" in (
        r.fields.get("msg") or ""
    )
    assert listdir_calls == [], "business Get-ChildItem path must not run"
    assert r.hint and "FullLanguage" in r.hint, "UNSUPPORTED must carry a hint"


def test_winrm_fs_list_ok_when_ps_script_fs_true() -> None:
    """FullLanguage + caps true → fs list still works."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\ok.txt"] = b"x"
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps=_full_ps_caps(),
    )
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "ok.txt" in (r.body or "")


def test_winrm_fs_list_ok_when_winrm_ps_absent() -> None:
    """Absent winrm_ps (probe=False / legacy) → fs not blocked."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\compat.txt"] = b"x"
    backend = WinrmFs(store, cwd=HOME, home=HOME, ps_caps=None)
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "compat.txt" in (r.body or "")


def test_winrm_fs_list_ok_probe_false_skipped_marker() -> None:
    """Service path: open probe=False records ps_probe=skipped → list not blocked."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\probe-skip.txt"] = b"x"
    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)
    assert ep.transport is not None
    meta = getattr(ep.transport, "meta", {}) or {}
    # probe=False marks the skip (gates stay permissive: no ps_oneshot/ps_script_fs).
    assert meta.get("winrm_ps") == {"ps_probe": "skipped"}
    assert ep.probe == {"ps_probe": "skipped"}

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert "probe-skip.txt" in (r.body or "")


def test_winrm_fs_list_unsupported_via_transport_meta() -> None:
    """Service path: transport.meta winrm_ps.ps_script_fs=false blocks list."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\blocked.txt"] = b"x"
    listdir_calls: list[str] = []
    orig = store.listdir

    def tracking_listdir(path: str) -> list[str]:
        listdir_calls.append(path)
        return orig(path)

    store.listdir = tracking_listdir  # type: ignore[method-assign]

    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)
    assert ep.transport is not None
    ep.transport.meta["winrm_ps"] = _constrained_ps_caps()

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        connector=connector,
        file_client=store,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert listdir_calls == []


# ---------------------------------------------------------------------------
# Fix C: put/get native copy/fetch exemption must not run gated business PS
# when the client only has a PS-script fallback (no native copy/fetch).
# ---------------------------------------------------------------------------


def _winrm_fs_with_caps(sess: FakePypsrpSession, caps: dict) -> WinrmFs:
    return WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME, ps_caps=caps)


def test_winrm_fs_put_unsupported_when_script_only_no_native_copy(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + session without native copy → put UNSUPPORTED;
    the write_file PS fallback must NOT run (zero business execute_ps)."""
    sess = FakePypsrpSession()  # has_copy=False → copy falls back to write_file
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "UNSUPPORTED"
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_put_ok_native_copy_when_script_fs_blocked(tmp_path: Path) -> None:
    """ps_script_fs=false + native copy → put succeeds via native copy; no PS."""
    sess = FakePypsrpSession(has_copy=True)
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"native-payload")
    remote = rf"{TEMP}\out.bin"
    r = backend.put(str(src), remote)
    assert r.bytes_transferred == len(b"native-payload")
    assert sess.files[remote] == b"native-payload"
    assert sess.copy_calls == [(str(src), remote)]
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_get_unsupported_when_script_only_no_native_fetch(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + session without native fetch → get UNSUPPORTED;
    zero business execute_ps (no read_file PS fallback)."""
    sess = FakePypsrpSession()  # has_fetch=False
    sess.files[rf"{TEMP}\src.bin"] = b"remote-data"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    assert excinfo.value.code == "UNSUPPORTED"
    assert not any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_get_ok_native_fetch_when_script_fs_blocked(tmp_path: Path) -> None:
    """ps_script_fs=false + native fetch → get succeeds via native fetch; no PS."""
    sess = FakePypsrpSession(has_fetch=True)
    sess.files[rf"{TEMP}\src.bin"] = b"remote-data"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    dst = tmp_path / "out.bin"
    r = backend.get(rf"{TEMP}\src.bin", str(dst))
    assert r.bytes_transferred == len(b"remote-data")
    assert dst.read_bytes() == b"remote-data"
    assert not any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_put_progress_unsupported_when_script_fs_blocked(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + progress → put UNSUPPORTED (progress path gated)."""
    sess = FakePypsrpSession(has_copy=True)
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin", progress=lambda _c, _t: None)
    assert excinfo.value.code == "UNSUPPORTED"


@pytest.mark.parametrize("op", ["read", "stat", "mkdir", "rm", "list"])
def test_winrm_fs_script_ops_unsupported_when_ps_script_fs_false(
    op: str, tmp_path: Path
) -> None:
    """All script FS ops gated under ps_script_fs=false → UNSUPPORTED,
    zero business execute_ps (gate fires before any PS)."""
    sess = FakePypsrpSession()
    sess.files[rf"{TEMP}\gate.txt"] = b"x"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    n_before = len(sess.ps_calls)
    with pytest.raises(FsError) as excinfo:
        if op == "read":
            backend.read(rf"{TEMP}\gate.txt")
        elif op == "stat":
            backend.stat(rf"{TEMP}\gate.txt")
        elif op == "mkdir":
            backend.mkdir(rf"{TEMP}\sub")
        elif op == "rm":
            backend.rm(rf"{TEMP}\gate.txt")
        else:  # list
            backend.list(TEMP)
    assert excinfo.value.code == "UNSUPPORTED"
    assert len(sess.ps_calls) == n_before


# ---------------------------------------------------------------------------
# H3: has_native_copy/fetch default False; gated native failure → UNSUPPORTED
# ---------------------------------------------------------------------------


class _CopyFetchClientNoNativeFlag:
    """SupportsCopyFetch shape without has_native_* attributes.

    Used to prove missing flags default to False (not treated as native).
    """

    def __init__(self) -> None:
        self.copy_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {}
        self._dirs: set[str] = {"C:\\", TEMP, HOME}

    def stat(self, path: str) -> _MockAttrs:
        if path in self._dirs or path.rstrip("\\") in self._dirs:
            return _MockAttrs("dir", 0, 1.0, "Directory")
        if path in self.files:
            data = self.files[path]
            return _MockAttrs("file", len(data), 1.0, "Archive")
        raise FileNotFoundError(path)

    def mkdir(self, path: str) -> None:
        self._dirs.add(path)

    def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if max_bytes is not None:
            return data[: max(0, int(max_bytes))]
        return data

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.files[remote] = Path(local).read_bytes()

    def fetch(self, remote: str, local: str) -> None:
        self.fetch_calls.append((remote, local))
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])


class _NativeCopyFailsClient:
    """Claims native copy/fetch, but the native methods raise opaque errors."""

    has_native_copy = True
    has_native_fetch = True

    def __init__(
        self,
        *,
        copy_exc: BaseException | None = None,
        fetch_exc: BaseException | None = None,
    ) -> None:
        self._copy_exc = copy_exc or RuntimeError("native copy transport blip")
        self._fetch_exc = fetch_exc or RuntimeError("native fetch transport blip")

    def copy(self, local: str, remote: str) -> None:
        raise self._copy_exc

    def fetch(self, remote: str, local: str) -> None:
        raise self._fetch_exc


def test_try_copy_defaults_has_native_copy_false_when_missing(tmp_path: Path) -> None:
    """Missing has_native_copy must not be treated as native (default False)."""
    client = _CopyFetchClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    # Under the gate, no-native → UNSUPPORTED; copy must not have been called.
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.copy_calls == []
    msg = excinfo.value.msg or ""
    assert "ConstrainedLanguage" in msg or "ps_script_fs" in msg


def test_try_fetch_defaults_has_native_fetch_false_when_missing(
    tmp_path: Path,
) -> None:
    """Missing has_native_fetch must not be treated as native (default False)."""
    client = _CopyFetchClientNoNativeFlag()
    client.files[rf"{TEMP}\src.bin"] = b"remote"
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.fetch_calls == []


def test_put_uses_write_file_when_native_flag_missing_and_scripts_ok(
    tmp_path: Path,
) -> None:
    """Default-false native flag still allows put via write_file when scripts ok."""
    client = _CopyFetchClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_full_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"via-write")
    remote = rf"{TEMP}\via-write.bin"
    r = backend.put(str(src), remote)
    assert r.bytes_transferred == len(b"via-write")
    assert client.copy_calls == [], "must not call unflagged copy"
    assert client.files[remote] == b"via-write"


def test_gated_native_copy_opaque_failure_is_unsupported(tmp_path: Path) -> None:
    """ps_script_fs=false + native copy raises opaque error → UNSUPPORTED,
    not FS_ERROR (scripts cannot fall back; surface language_mode hint)."""
    client = _NativeCopyFailsClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"x")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    err = excinfo.value
    assert err.code == "UNSUPPORTED"
    assert err.details.get("ps_script_fs") is False
    assert err.details.get("language_mode") == "ConstrainedLanguage"
    assert "FullLanguage" in err.msg or "ps_script_fs" in err.msg


def test_gated_native_fetch_opaque_failure_is_unsupported(tmp_path: Path) -> None:
    """ps_script_fs=false + native fetch raises opaque error → UNSUPPORTED."""
    client = _NativeCopyFailsClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    err = excinfo.value
    assert err.code == "UNSUPPORTED"
    assert err.details.get("language_mode") == "ConstrainedLanguage"


def test_gated_native_copy_not_found_stays_not_found(tmp_path: Path) -> None:
    """Specific path errors from native still surface (not remapped to gate)."""
    client = _NativeCopyFailsClient(copy_exc=FileNotFoundError("no such file"))
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"x")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "NOT_FOUND"
