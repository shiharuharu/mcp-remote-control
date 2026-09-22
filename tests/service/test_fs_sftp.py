"""Service tests: SFTP list, read, reconnect (mock client)."""

from __future__ import annotations

import os
import stat as statmod
from collections.abc import Iterator
from pathlib import Path

import pytest

from _sftp_fakes import MockSftp, _MockAttrs, _SFTPName

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.fs.backends.sftp import _MAX_RECURSE_DEPTH, SftpFs
from mcp_remote_control.fs.types import FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# sftp mock - list / read / missing path
# ---------------------------------------------------------------------------


def test_sftp_mock_list_and_read() -> None:
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.dirs.add("/var/log")
    mock.files["/var/log/syslog"] = b"log-line-1\nlog-line-2\n"

    backend = SftpFs(mock, cwd="/var", home="/home/deploy")
    r = fs_ops.run(
        "list",
        ep="lab-ssh",
        path="/var/log",
        home=FIXTURES,
        backend=backend,
    )
    # When backend is injected, ep may still be set; no real connect.
    assert r.status == "ok"
    assert r.fields.get("op") == "list"
    assert r.fields.get("path") == "/var/log"
    assert Path(r.fields["path"]).is_absolute() or r.fields["path"].startswith("/")
    assert "syslog" in (r.body or "")
    # via=sftp optional
    if r.fields.get("via"):
        assert r.fields["via"] == "sftp"

    r2 = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/var/log/syslog",
        home=FIXTURES,
        backend=backend,
    )
    assert r2.status == "ok"
    assert r2.fields.get("path") == "/var/log/syslog"
    assert r2.body is not None
    assert "log-line-1" in r2.body


def test_sftp_mock_via_ssh_connector() -> None:
    """end-to-end: ensure_endpoint + open_sftp on mock connection."""
    mock = MockSftp()
    mock.dirs.add("/tmp")
    mock.files["/tmp/hello.txt"] = b"sftp-hello\n"

    class Conn:
        cwd = "/tmp"
        home = "/home/deploy"

        def start_sftp_client(self) -> MockSftp:
            return mock

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = fs_ops.run(
        "list",
        ep="lab-ssh",
        path="/tmp",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("path") == "/tmp"
    assert "hello.txt" in (r.body or "")
    if r.fields.get("via"):
        assert r.fields["via"] == "sftp"

    r2 = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/tmp/hello.txt",
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok"
    assert "sftp-hello" in (r2.body or "")
    assert r2.fields["path"] == "/tmp/hello.txt"


def test_sftp_mock_missing_path() -> None:
    mock = MockSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "stat",
        ep="lab-ssh",
        path="/missing",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"


def test_sftp_names_with_whitespace_stay_distinct() -> None:
    """``x``, ``x `` and `` x`` are distinct remote names.

    Trimming the caller's path collapses them onto one object: a read issued
    for ``x `` would return ``x``'s bytes, and rm would delete the wrong
    entry. Each op must resolve and report the string as given.
    """
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/x"] = b"PLAIN"
    mock.files["/d/x "] = b"TRAILING"
    mock.files["/d/ x"] = b"LEADING"
    backend = SftpFs(mock, cwd="/d", home="/home/u")

    for name, text in (("x", "PLAIN"), ("x ", "TRAILING"), (" x", "LEADING")):
        r = fs_ops.run(
            "read", ep="lab-ssh", path=name, home=FIXTURES, backend=backend
        )
        assert r.status == "ok", r.render_text()
        assert r.fields.get("path") == f"/d/{name}"
        assert text in (r.body or ""), (name, r.render_text())
        s = fs_ops.run(
            "stat", ep="lab-ssh", path=name, home=FIXTURES, backend=backend
        )
        assert s.status == "ok", s.render_text()
        assert s.fields.get("path") == f"/d/{name}"

    w = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="x ",
        content="NEW-TRAILING",
        home=FIXTURES,
        backend=backend,
    )
    assert w.status == "ok", w.render_text()
    assert mock.files["/d/x "] == b"NEW-TRAILING"
    assert mock.files["/d/x"] == b"PLAIN"
    assert mock.files["/d/ x"] == b"LEADING"

    rm = fs_ops.run("rm", ep="lab-ssh", path=" x", home=FIXTURES, backend=backend)
    assert rm.status == "ok", rm.render_text()
    assert "/d/ x" not in mock.files
    assert mock.files["/d/x"] == b"PLAIN"
    assert mock.files["/d/x "] == b"NEW-TRAILING"


class _LinkOnlySftp:
    """Minimal SFTP surface (lstat/stat/readlink) for link-follow checks.

    ``stat`` is lstat-shaped - it never follows - so the backend's own
    readlink resolution decides the referent rather than the client's
    follow-stat.
    """

    def __init__(self, *, links: dict[str, str], dirs: set[str]) -> None:
        self.links = links
        self.dirs = dirs

    def lstat(self, path: str) -> _MockAttrs:
        if path in self.links:
            return _MockAttrs(statmod.S_IFLNK | 0o777, 0, 1.0)
        if path in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        raise FileNotFoundError(path)

    def stat(self, path: str) -> _MockAttrs:
        return self.lstat(path)

    def readlink(self, path: str) -> str:
        if path not in self.links:
            raise FileNotFoundError(path)
        return self.links[path]


def test_sftp_dir_link_target_keeps_whitespace() -> None:
    """A link-to-directory whose stored target ends in a space is still an
    existing directory - the target is resolved verbatim, not trimmed."""
    sftp = _LinkOnlySftp(links={"/srv/www": "/var/www "}, dirs={"/var/www "})
    backend = SftpFs(sftp, cwd="/", home="/home/u")
    info = backend.mkdir("/srv/www")
    assert info.kind == "link"
    assert info.target == "/var/www "


# ---------------------------------------------------------------------------
# sftp: recursive list, readdir attrs, reconnect
# ---------------------------------------------------------------------------


class ChannelClosed(Exception):
    """Stand-in for an asyncssh channel-closed error (duck-typed by name)."""


class _DeadSftp:
    """Every op raises a channel-closed error (simulates a dropped channel)."""

    def stat(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")

    def lstat(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")

    def readdir(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")


def test_sftp_recursive_list_name_not_corrupted() -> None:
    """Recursive list at depth >=3 with a child basename starting with the
    parent basename must produce correct relative names (the old startswith
    heuristic dropped the prefix for sub_file under sub and sub2/bar)."""
    mock = MockSftp()
    mock.dirs.update({"/d", "/d/sub", "/d/sub/sub2"})
    mock.files["/d/sub/sub_file"] = b"x"
    mock.files["/d/sub/sub2/bar"] = b"yy"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.list("/d", recursive=True)
    by_name = {e.name: e for e in result.entries}
    # Correct relative paths preserved at every depth.
    assert "sub" in by_name and by_name["sub"].kind == "d"
    assert "sub/sub_file" in by_name and by_name["sub/sub_file"].kind == "f"
    assert by_name["sub/sub_file"].path == "/d/sub/sub_file"
    assert "sub/sub2" in by_name and by_name["sub/sub2"].kind == "d"
    assert "sub/sub2/bar" in by_name and by_name["sub/sub2/bar"].kind == "f"
    assert by_name["sub/sub2/bar"].path == "/d/sub/sub2/bar"
    # The corrupted unprefixed names must NOT appear.
    assert "sub_file" not in by_name
    assert "sub2/bar" not in by_name


def test_sftp_recursive_list_self_ref_symlink_terminates() -> None:
    """A self-referential symlink reported as kind=link (lstat/readdir)
    must not be walked as a directory - recursive list terminates and the
    entry stays a link leaf."""
    mock = MockSftp()
    mock.dirs.update({"/d", "/d/sub"})
    mock.files["/d/a.txt"] = b"x"
    mock.links["/d/self"] = "/d"  # self-ref symlink into the tree root
    mock.links["/d/sub/up"] = "/d"  # self-ref one level deeper
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.list("/d", recursive=True)
    by_name = {e.name: e for e in result.entries}
    assert "self" in by_name and by_name["self"].kind == "l"
    assert "sub" in by_name and by_name["sub"].kind == "d"
    assert "sub/up" in by_name and by_name["sub/up"].kind == "l"
    assert "a.txt" in by_name and by_name["a.txt"].kind == "f"
    # No infinite descent through the symlink (would yield self/self/... names).
    assert all(not n.startswith("self/") for n in by_name)
    assert all(not n.startswith("sub/up/") for n in by_name)


def test_sftp_recursive_list_depth_exceeded_raises() -> None:
    """Ever-growing path cycle (each dir contains a 'loop' child dir)
    must raise a clear FsError when max_depth is hit - not hang / RecursionError.
    (WinRM skips at cap; SFTP requires an explicit depth error.)"""

    class _CycleSftp:
        """Every dir listing yields a single 'loop' child as kind=dir."""

        def __init__(self) -> None:
            self.readdir_calls = 0

        def lstat(self, path: str) -> _MockAttrs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)

        def stat(self, path: str) -> _MockAttrs:
            return self.lstat(path)

        def readdir(self, path: str) -> list[_SFTPName]:
            self.readdir_calls += 1
            return [
                _SFTPName(
                    "loop",
                    _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0),
                )
            ]

    backend = SftpFs(_CycleSftp(), cwd="/", home="/home/u")
    with pytest.raises(FsError) as ei:
        backend.list("/tmp", recursive=True)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth" in err.msg
    assert str(_MAX_RECURSE_DEPTH) in err.msg
    assert err.details.get("max_depth") == _MAX_RECURSE_DEPTH
    assert err.details.get("path")  # path of the overflowing node


def test_sftp_list_reuses_readdir_attrs_no_per_child_stat() -> None:
    """List reuses readdir attrs (no per-child stat) and reuses the
    dir attrs for "." (no second stat of the same path)."""
    mock = MockSftp()
    mock.dirs.update({"/dir"})
    mock.files["/dir/a"] = b"1"
    mock.files["/dir/b"] = b"22"
    mock.files["/dir/c"] = b"333"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.list("/dir")
    by_name = {e.name: e for e in result.entries}
    assert set(by_name) >= {".", "..", "a", "b", "c"}
    # readdir supplied the children's attrs (one readdir for the dir contents).
    assert mock.readdir_calls == 1, f"expected 1 readdir, got {mock.readdir_calls}"
    # Only 2 stats: the dir (kind check, reused for ".") + the parent for "..".
    # A per-child stat or a second dir stat for "." would raise this to 3+.
    assert mock.stat_calls == 2, f"expected 2 stats, got {mock.stat_calls}"
    # "." carries the dir's mode/mtime (reused attrs, not the bare fallback).
    assert by_name["."].mode is not None
    assert by_name["."].mtime is not None
    assert by_name["a"].kind == "f" and by_name["a"].size == 1
    assert by_name["b"].kind == "f" and by_name["b"].size == 2
    assert by_name["c"].kind == "f" and by_name["c"].size == 3


class _RealFsSftp:
    """SFTP client surface backed by the real filesystem (no network).

    Paths and attrs come from the OS, so a derived row's ``..`` is resolved
    the way a server resolves it - through a trailing ``..`` and any
    intermediate link - and ``os.path.samefile`` can be asked what object a
    row actually names.
    """

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def stat(self, path: str) -> os.stat_result:
        return os.stat(path)

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def readdir(self, path: str) -> list[str]:
        return os.listdir(path)


def test_sftp_list_derived_rows_name_the_objects_they_describe(
    tmp_path: Path,
) -> None:
    """The "." and ".." rows resolve to the listed directory and its parent.

    A caller path that walks ".." through an intermediate link lists the
    directory the server resolves it to, so the row naming that directory and
    the row naming its parent must resolve to that directory and its real
    parent. Collapsing the text lexically names the link's own directory - a
    different object.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "sub").mkdir(parents=True)
    (tmp_path / "b" / "sub" / "child.txt").write_text("REAL-B", encoding="utf-8")
    (tmp_path / "a" / "victim").write_text("DECOY-A", encoding="utf-8")
    (tmp_path / "a" / "link").symlink_to("../b/sub")

    backend = SftpFs(_RealFsSftp(), cwd="/", home="/home/u")

    # The link is followed before "..", so this lists b/sub.
    rows = {e.name: e for e in backend.list(f"{tmp_path}/a/link/../sub").entries}
    assert set(rows) >= {".", "..", "child.txt"}
    for name in (".", ".."):
        assert os.path.exists(rows[name].path), (name, rows[name].path)
    assert os.path.samefile(rows["."].path, tmp_path / "b" / "sub")
    assert os.path.samefile(rows[".."].path, tmp_path / "b")

    # A caller path ending in ".." lists b too, whose parent is the root here.
    rows = {e.name: e for e in backend.list(f"{tmp_path}/a/link/..").entries}
    for name in (".", ".."):
        assert os.path.exists(rows[name].path), (name, rows[name].path)
    assert os.path.samefile(rows["."].path, tmp_path / "b")
    assert os.path.samefile(rows[".."].path, tmp_path)

    # An ordinary listing keeps both rows on the objects they name.
    rows = {e.name: e for e in backend.list(str(tmp_path / "b")).entries}
    assert os.path.samefile(rows["."].path, tmp_path / "b")
    assert os.path.samefile(rows[".."].path, tmp_path)


def test_sftp_read_dir_maps_is_a_dir_without_pre_stat() -> None:
    """Read on a dir maps to IS_A_DIR via the open failure, with no
    pre-stat (open is attempted; stat is not called)."""
    mock = MockSftp()
    mock.dirs.update({"/dir"})
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "read", ep="lab-ssh", path="/dir", home=FIXTURES, backend=backend
    )
    assert r.status == "error"
    assert r.code == "IS_A_DIR"
    # No pre-stat: read no longer calls stat.
    assert mock.stat_calls == 0
    # open was attempted (and its IsADirectoryError mapped to IS_A_DIR).
    assert mock.open_calls >= 1


def test_sftp_client_reconnects_after_channel_closed() -> None:
    """After a channel-closed error on an op, the cached client is reset
    and the next op re-invokes the factory (reconnect works)."""
    factory_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/hello.txt"] = b"reconnected\n"

    def factory() -> object:
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            return _DeadSftp()
        return working

    backend = SftpFs(factory=factory, cwd="/", home="/home/u")
    # First op fails on the dead client; the cached client is reset.
    r1 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r1.status == "error"
    assert backend._client is None
    assert factory_calls["n"] == 1
    # Second op re-invokes the factory and succeeds on the fresh client.
    r2 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r2.status == "ok"
    assert factory_calls["n"] == 2
    assert backend._client is not None
    assert "hello.txt" in (r2.body or "")


def test_sftp_open_sftp_cache_reopens_after_channel_closed() -> None:
    """Production factory (SSHTransport.open_sftp) must not keep returning
    a dead cached ``_sftp`` after channel-closed.

    After the first failing op, the transport SFTP cache is cleared and the
    next list/read succeeds with ``start_sftp_client`` call count +1. The
    SSH endpoint stays connected (no permanent fail without endpoint reopen).
    """
    from mcp_remote_control.transport.ssh import SSHTransport

    start_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/hello.txt"] = b"reconnected\n"

    class Conn:
        def is_closing(self) -> bool:
            return False

        def start_sftp_client(self) -> object:
            start_calls["n"] += 1
            if start_calls["n"] == 1:
                return _DeadSftp()
            return working

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    assert t.is_connected() is True

    # Same factory binding production uses (backend_for_endpoint).
    backend = SftpFs(factory=t.open_sftp, cwd="/", home="/home/u")
    r1 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r1.status == "error"
    assert start_calls["n"] == 1
    # Transport cache dropped; SSH session still live (endpoint not reopened).
    assert t._sftp is None
    assert t.is_connected() is True
    assert t.is_alive() is True

    r2 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r2.status == "ok"
    assert start_calls["n"] == 2, (
        f"expected start_sftp_client +1 after channel-closed, got {start_calls['n']}"
    )
    assert "hello.txt" in (r2.body or "")
    assert t.is_connected() is True

    # Fresh SftpFs instance (fs_ops production path rebuilds backend each op)
    # must reuse the live cache, not a dead one.
    backend2 = SftpFs(factory=t.open_sftp, cwd="/", home="/home/u")
    r3 = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/x/hello.txt",
        home=FIXTURES,
        backend=backend2,
    )
    assert r3.status == "ok"
    assert "reconnected" in (r3.body or "")
    # Live cache reused - no extra start_sftp_client.
    assert start_calls["n"] == 2


def test_sftp_open_sftp_rebuilds_when_cached_client_looks_dead() -> None:
    """open_sftp probes a dead-looking cache and reopens without needing
    a prior SftpFs invalidate (defense in depth for stale _sftp)."""
    from mcp_remote_control.transport.ssh import SSHTransport

    start_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/a.txt"] = b"alive\n"

    class Conn:
        def is_closing(self) -> bool:
            return False

        def start_sftp_client(self) -> object:
            start_calls["n"] += 1
            return working

    class DeadLooking:
        """SFTP stand-in that advertises closed without raising until used."""

        _closed = True

        def lstat(self, path: str) -> None:
            raise ChannelClosed("channel closed by remote")

        def readdir(self, path: str) -> None:
            raise ChannelClosed("channel closed by remote")

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    # Seed a dead cache the way a prior session would leave it.
    t._sftp = DeadLooking()
    assert t._sftp_looks_dead(t._sftp) is True

    backend = SftpFs(factory=t.open_sftp, cwd="/", home="/home/u")
    r = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert start_calls["n"] == 1
    assert "a.txt" in (r.body or "")
    assert t._sftp is working


# ---------------------------------------------------------------------------
# sftp: UTF-16 detection (shared detect_text) + _rmtree readdir-attrs
# ---------------------------------------------------------------------------


def test_sftp_read_utf16le_with_bom() -> None:
    """SFTP read of a UTF-16LE file (BOM) returns text. SFTP from Windows
    servers commonly serves UTF-16; the simple any-NUL heuristic previously
    misclassified it as binary. The shared detect_text now detects it."""
    mock = MockSftp()
    mock.dirs.add("/var")
    text = "hello-sftp"
    mock.files["/var/u16.txt"] = b"\xff\xfe" + text.encode("utf-16-le")
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/u16.txt", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16"
    assert text in (r.body or "")


def test_sftp_read_utf16le_no_bom() -> None:
    """SFTP read of no-BOM UTF-16LE (alternating-NUL ASCII) is detected as
    text via the shared heuristic; previously binary."""
    mock = MockSftp()
    mock.dirs.add("/var")
    text = "plain-ascii-content"
    raw = text.encode("utf-16-le")
    assert b"\x00" in raw and raw[1] == 0
    mock.files["/var/u16nobom.txt"] = raw
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/var/u16nobom.txt",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16-le"
    assert text in (r.body or "")


def test_sftp_read_binary_with_nul_stays_binary() -> None:
    """Random binary with a NUL byte (no alternating-NUL pattern)
    stays binary on sftp - the shared heuristic doesn't misfire."""
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.files["/var/bin.dat"] = b"\x00\x01\x02\x03\x04\x05binary"
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/bin.dat", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_sftp_read_big_endian_uint32_stays_binary() -> None:
    """A NUL-dense binary with NON-PRINTABLE non-NUL bytes
    is NOT misclassified as UTF-16 on sftp (shared detect_text). The 12-byte
    big-endian uint32 repro (``\\x00\\x00\\x00\\x01...``) previously reported
    ``utf-16-be`` + gibberish text; the printable-ratio guard now rejects it
    as binary because the non-NUL bytes (0x01/0x02/0x03) are non-printable."""
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.files["/var/u32.bin"] = b"\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03"
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/u32.bin", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_sftp_rmtree_uses_readdir_attrs_no_per_child_stat() -> None:
    """_rmtree uses _scandir (readdir attrs) to recover each child's
    kind - the same N+1 fix ``list`` got. No per-child _stat when readdir
    yields attrs (the asyncssh common path)."""
    mock = MockSftp()
    mock.dirs.update({"/tree", "/tree/sub"})
    mock.files["/tree/a"] = b"1"
    mock.files["/tree/b"] = b"22"
    mock.files["/tree/sub/c"] = b"333"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    # Counters reset right before rm so the assertion only counts _rmtree's
    # work (rm itself still does 1 stat for the top-level dir kind check).
    mock.stat_calls = 0
    mock.readdir_calls = 0
    result = backend.rm("/tree", recursive=True)

    assert result == "/tree"
    # Tree fully removed.
    assert "/tree" not in mock.dirs
    assert "/tree/sub" not in mock.dirs
    assert all(not f.startswith("/tree/") for f in mock.files)
    # Only the rm top-level stat - _rmtree derives kinds from readdir attrs.
    # (The old per-child _stat path would have left this at 5: 1 + a/b/sub + c.)
    assert mock.stat_calls == 1, (
        f"expected 1 stat (rm top-level only), got {mock.stat_calls}"
    )
    # One readdir per dir level (tree + sub).
    assert mock.readdir_calls == 2, (
        f"expected 2 readdirs, got {mock.readdir_calls}"
    )


def test_sftp_rmtree_self_ref_symlink_terminates() -> None:
    """rmtree must unlink self-ref symlinks as leaves (not follow) and
    still remove the rest of the tree without infinite recursion."""
    mock = MockSftp()
    mock.dirs.update({"/tree", "/tree/sub"})
    mock.files["/tree/a"] = b"1"
    mock.files["/tree/sub/b"] = b"22"
    mock.links["/tree/self"] = "/tree"
    mock.links["/tree/sub/up"] = "/tree"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.rm("/tree", recursive=True)
    assert result == "/tree"
    assert "/tree" not in mock.dirs
    assert "/tree/sub" not in mock.dirs
    assert all(not f.startswith("/tree/") for f in mock.files)
    assert all(not link.startswith("/tree") for link in mock.links)


def test_sftp_rmtree_depth_exceeded_raises() -> None:
    """rmtree on an ever-growing dir cycle raises a clear depth FsError
    instead of RecursionError / hang."""

    class _CycleSftp:
        def lstat(self, path: str) -> _MockAttrs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)

        def stat(self, path: str) -> _MockAttrs:
            return self.lstat(path)

        def readdir(self, path: str) -> list[_SFTPName]:
            return [
                _SFTPName(
                    "loop",
                    _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0),
                )
            ]

        def rmdir(self, path: str) -> None:
            return None

        def remove(self, path: str) -> None:
            return None

    backend = SftpFs(_CycleSftp(), cwd="/", home="/home/u")
    with pytest.raises(FsError) as ei:
        backend.rm("/tmp", recursive=True)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth" in err.msg
    assert err.details.get("max_depth") == _MAX_RECURSE_DEPTH


# ---------------------------------------------------------------------------
# sftp: a failed transfer names the object on the side that failed
# ---------------------------------------------------------------------------


def test_sftp_put_unreadable_local_source_names_the_local_source(
    tmp_path: Path,
) -> None:
    """An unreadable local source is reported against that source.

    The remote destination is writable and the transfer never created it, so
    the old row named the one object on the other side of the transfer.
    """
    mock = MockSftp()
    mock.dirs.add("/d")
    backend = SftpFs(mock, cwd="/", home="/home/u")
    # resolve() so the path matches the one the backend reports (it resolves
    # an absolute source before reading it).
    src = (tmp_path / "unreadable.bin").resolve()
    src.write_bytes(b"payload")
    os.chmod(src, 0o000)
    try:
        r = fs_ops.run(
            "put",
            ep="lab-ssh",
            path="/d/out.bin",
            local=str(src),
            home=FIXTURES,
            backend=backend,
        )
    finally:
        os.chmod(src, 0o644)

    assert r.status == "error", r.render_text()
    assert r.code == "PERMISSION_DENIED"
    # path stays the caller's remote destination; node_path is the failed object.
    assert r.fields.get("path") == "/d/out.bin", r.render_text()
    assert r.fields.get("node_path") == str(src), r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(src) in msg, r.render_text()
    assert "/d/out.bin" not in msg, r.render_text()
    assert "/d/out.bin" not in mock.files
    assert [name for name in mock.files if ".mrc-tmp-" in name] == []


@pytest.mark.parametrize("existing", [True, False], ids=["existing_dir", "missing_dir"])
def test_sftp_get_denied_local_destination_names_the_local_destination(
    tmp_path: Path,
    existing: bool,
) -> None:
    """A get the local destination refuses is reported against that destination.

    The remote source is readable and the download never touched it; the old
    row named the remote path instead.
    """
    mock = MockSftp()
    mock.files["/d/x.bin"] = b"remote-payload"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    locked = tmp_path / "locked"
    locked.mkdir()
    dest = locked / "sub" / "out.bin"
    if existing:
        dest.parent.mkdir()
    # The level that refuses the write: the destination's own directory when it
    # is already there, otherwise the directory its creation goes through.
    refused = dest.parent if existing else locked
    os.chmod(refused, 0o555)
    try:
        r = fs_ops.run(
            "get",
            ep="lab-ssh",
            path="/d/x.bin",
            local=str(dest),
            home=FIXTURES,
            backend=backend,
        )
    finally:
        os.chmod(refused, 0o755)

    assert r.status == "error", r.render_text()
    assert r.code == "PERMISSION_DENIED"
    assert r.fields.get("path") == "/d/x.bin", r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(dest) in msg, r.render_text()
    assert "/d/x.bin" not in msg, r.render_text()
    assert not dest.exists()
    assert mock.files["/d/x.bin"] == b"remote-payload"
    assert [name for name in mock.files if ".mrc-tmp-" in name] == []
    assert not any(".mrc-tmp-" in p.name for p in refused.iterdir())


def test_sftp_get_directory_destination_names_the_local_destination(
    tmp_path: Path,
) -> None:
    """A local directory at the destination is reported against that directory.

    The remote source is a regular file, so a row naming it said the wrong
    object was a directory.
    """
    mock = MockSftp()
    mock.files["/d/x.bin"] = b"remote-payload"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    dest = tmp_path / "dest-dir"
    dest.mkdir()

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/x.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "IS_A_DIR"
    assert r.fields.get("path") == "/d/x.bin", r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    # The client's own message is truncated at 200 chars; what it must not do
    # is name the regular remote file.
    msg = str(r.fields.get("msg") or "")
    assert "is a directory" in msg.lower(), r.render_text()
    assert "/d/x.bin" not in msg, r.render_text()
    assert dest.is_dir() and list(dest.iterdir()) == []
    assert [p.name for p in tmp_path.iterdir()] == [dest.name]


class _RemoteGetFailSftp(MockSftp):
    """MockSftp whose download fails on the remote side, naming the remote path."""

    def get(self, remote: str, local: str) -> None:
        raise PermissionError(remote)


def test_sftp_get_remote_failure_still_names_the_remote_path(
    tmp_path: Path,
) -> None:
    """Remote-side failures keep the remote path and gain no node_path.

    An error carrying the remote path must not be mistaken for a local-side
    failure just because it is an OSError.
    """
    dest = tmp_path / "downloaded.bin"

    # Missing remote source: the pre-stat names the remote path.
    backend = SftpFs(MockSftp(), cwd="/", home="/home/u")
    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/missing.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "NOT_FOUND"
    assert r.fields.get("path") == "/d/missing.bin", r.render_text()
    assert "node_path" not in r.fields, r.render_text()
    assert not dest.exists()

    # A remote read failure that carries the remote path stays remote.
    mock = _RemoteGetFailSftp()
    mock.files["/d/x.bin"] = b"remote-payload"
    backend2 = SftpFs(mock, cwd="/", home="/home/u")
    r2 = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/x.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend2,
    )
    assert r2.status == "error", r2.render_text()
    assert r2.code == "PERMISSION_DENIED"
    assert r2.fields.get("path") == "/d/x.bin", r2.render_text()
    assert "node_path" not in r2.fields, r2.render_text()
    assert not dest.exists()



def test_sftp_get_destination_link_cycle_names_the_local_destination(
    tmp_path: Path,
) -> None:
    """A local destination link chain that cannot be walked is a local failure.

    The walk's exception names an element of the chain, learned at that call
    and nowhere earlier, so the row must name the local destination (the
    backend reports the destination the transfer asked for) instead of the
    readable remote source. The remote source is never touched.
    """
    mock = MockSftp()
    mock.files["/d/x.bin"] = b"remote-payload"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    cycle = tmp_path / "cycle"
    cycle.mkdir()
    for name, target in (("cyc0", "cyc1"), ("cyc1", "cyc2"), ("cyc2", "cyc0")):
        (cycle / name).symlink_to(target)
    dest = cycle / "cyc0"

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/x.bin",
        local=str(dest),
        home=FIXTURES,
        backend=backend,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "FS_ERROR"
    assert r.fields.get("path") == "/d/x.bin", r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    assert dest.is_symlink(), r.render_text()
    assert mock.files["/d/x.bin"] == b"remote-payload"
    assert [name for name in mock.files if ".mrc-tmp-" in name] == []
