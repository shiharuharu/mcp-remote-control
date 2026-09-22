"""Service tests: SFTP hang, TIMEOUT invalidate, and whole-op budget."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from _sftp_fakes import MockSftp

from mcp_remote_control.core import fs_ops
from mcp_remote_control.fs.backends.sftp import (
    DEFAULT_SFTP_TIMEOUT_S,
    SftpFs,
)
from mcp_remote_control.fs.types import DEFAULT_TRANSFER_CHUNK, FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# sftp: wall-clock deadline on _run_maybe_async funnel
# ---------------------------------------------------------------------------


class _HangingSftp:
    """SFTP duck-type whose every awaitable never completes (silent hang)."""

    def lstat(self, path: str):  # noqa: ANN201
        return self._hang()

    def stat(self, path: str):  # noqa: ANN201
        return self._hang()

    def readdir(self, path: str):  # noqa: ANN201
        return self._hang()

    def open(self, path: str, mode: str = "r"):  # noqa: ANN201
        return self._hang()

    def mkdir(self, path: str):  # noqa: ANN201
        return self._hang()

    def remove(self, path: str):  # noqa: ANN201
        return self._hang()

    @staticmethod
    async def _hang() -> None:
        await asyncio.sleep(3600.0)
        raise RuntimeError("hang should have timed out")


def test_sftp_default_timeout_budget() -> None:
    """SftpFs default wall-clock budget aligns with exec (60s)."""
    mock = MockSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    assert backend._timeout_s == DEFAULT_SFTP_TIMEOUT_S
    assert DEFAULT_SFTP_TIMEOUT_S == 60.0
    # Whole-op budget defaults to the same ceiling (configurable).
    assert backend._op_timeout_s == DEFAULT_SFTP_TIMEOUT_S


def test_sftp_stat_hang_returns_timeout_within_budget() -> None:
    """Hung SFTP stat surfaces as TIMEOUT within the configured budget."""
    budget = 0.15
    backend = SftpFs(
        _HangingSftp(),
        cwd="/",
        home="/home/u",
        timeout_s=budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.stat("/remote/file.txt")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert "timed out" in ei.value.msg.lower()
    # Must not wait forever; allow generous slack for CI scheduling.
    assert elapsed < budget + 2.0, f"elapsed={elapsed}s budget={budget}s"
    assert elapsed >= budget * 0.5


def test_sftp_read_hang_maps_to_fs_ops_timeout() -> None:
    """fs_ops.run path returns OpResult code=TIMEOUT on hung open/read."""
    budget = 0.15
    backend = SftpFs(
        _HangingSftp(),
        cwd="/",
        home="/home/u",
        timeout_s=budget,
    )
    t0 = time.monotonic()
    r = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/remote/hang.bin",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert "timed out" in (r.fields.get("msg") or "").lower()
    assert elapsed < budget + 2.0, f"elapsed={elapsed}s budget={budget}s"


def test_sftp_factory_hang_returns_timeout() -> None:
    """Hanging factory open (awaitable client) also hits the funnel budget."""
    budget = 0.15

    async def _never_open() -> MockSftp:
        await asyncio.sleep(3600.0)
        return MockSftp()

    backend = SftpFs(
        factory=lambda: _never_open(),
        cwd="/",
        home="/home/u",
        timeout_s=budget,
    )
    t0 = time.monotonic()
    r = fs_ops.run(
        "stat",
        ep="lab-ssh",
        path="/x",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert elapsed < budget + 2.0


def test_sftp_normal_path_unchanged_with_timeout_budget() -> None:
    """Sync mock paths still succeed under the default budget."""
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.files["/var/ok.txt"] = b"still-works\n"
    backend = SftpFs(mock, cwd="/var", home="/home/u")  # default 60s budget
    r = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/var/ok.txt",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "still-works" in (r.body or "")


# ---------------------------------------------------------------------------
# sftp: TIMEOUT invalidates cached client / transport SFTP cache
# ---------------------------------------------------------------------------


def test_sftp_timeout_invalidates_client_next_op_uses_new_factory() -> None:
    """Bridge TimeoutError drops self._client; next op re-invokes factory.

    Mirrors channel-closed reconnect (test_sftp_client_reconnects_after_channel_closed)
    but the failure mode is a hung await mapped to FsError(TIMEOUT).
    """
    factory_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/hello.txt"] = b"after-timeout\n"
    budget = 0.15

    def factory() -> object:
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            return _HangingSftp()
        return working

    backend = SftpFs(
        factory=factory,
        cwd="/",
        home="/home/u",
        timeout_s=budget,
    )
    with pytest.raises(FsError) as ei:
        backend.stat("/x/hello.txt")
    assert ei.value.code == "TIMEOUT"
    assert backend._client is None, "TIMEOUT must drop cached client"
    assert factory_calls["n"] == 1

    # Next op re-invokes factory and succeeds on the fresh client.
    info = backend.stat("/x/hello.txt")
    assert factory_calls["n"] == 2, (
        f"expected factory call count 2 after TIMEOUT, got {factory_calls['n']}"
    )
    assert backend._client is not None
    assert info.path == "/x/hello.txt"
    assert info.kind == "file"


def test_sftp_timeout_invokes_transport_invalidate_sftp() -> None:
    """factory=transport.open_sftp -> TIMEOUT calls invalidate_sftp.

    Production path: only clearing self._client is not enough because
    open_sftp would return the same wedged transport._sftp cache. After
    TIMEOUT the transport cache is None and start_sftp_client is called
    again on the next op.
    """
    from mcp_remote_control.transport.ssh import SSHTransport

    start_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/hello.txt"] = b"recovered\n"
    budget = 0.15

    class Conn:
        def is_closing(self) -> bool:
            return False

        def start_sftp_client(self) -> object:
            start_calls["n"] += 1
            if start_calls["n"] == 1:
                return _HangingSftp()
            return working

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    assert t.is_connected() is True

    backend = SftpFs(
        factory=t.open_sftp,
        cwd="/",
        home="/home/u",
        timeout_s=budget,
    )
    with pytest.raises(FsError) as ei:
        backend.stat("/x/hello.txt")
    assert ei.value.code == "TIMEOUT"
    assert backend._client is None
    assert start_calls["n"] == 1
    # Transport SFTP cache dropped (invalidate_sftp); SSH stays up.
    assert t._sftp is None
    assert t.is_connected() is True
    assert t.is_alive() is True

    info = backend.stat("/x/hello.txt")
    assert start_calls["n"] == 2, (
        f"expected start_sftp_client +1 after TIMEOUT, got {start_calls['n']}"
    )
    assert info.kind == "file"
    assert t.is_connected() is True


def test_sftp_timeout_without_factory_leaves_injected_client() -> None:
    """Injected client= (no factory) cannot be recreated - no drop.

    Aligns with channel-closed invalidate which only applies when factory
    is configured. Hang tests keep using client= without factory.
    """
    hang = _HangingSftp()
    backend = SftpFs(hang, cwd="/", home="/home/u", timeout_s=0.15)
    with pytest.raises(FsError) as ei:
        backend.stat("/remote/file.txt")
    assert ei.value.code == "TIMEOUT"
    # No factory -> cannot reconnect; client reference stays (wedged).
    assert backend._client is hang


# ---------------------------------------------------------------------------
# sftp: whole-op wall-clock budget across multi-await ops
# ---------------------------------------------------------------------------


class _DelayedSftp:
    """SFTP duck-type: every RPC is an awaitable that sleeps then delegates.

    Each await is short enough to pass a generous per-await budget, but a
    multi-await public op can exceed a tight whole-op budget.

    Side effects run *after* the sleep (inside the coroutine) so a cancelled
    / un-started await does not mutate the inner mock.
    """

    def __init__(self, inner: MockSftp, delay_s: float) -> None:
        self._inner = inner
        self.delay_s = float(delay_s)
        self.await_count = 0

    def _call(self, fn, *args, **kwargs):  # noqa: ANN001, ANN202
        delay = self.delay_s
        self_outer = self

        async def _go() -> object:
            self_outer.await_count += 1
            await asyncio.sleep(delay)
            return fn(*args, **kwargs)

        return _go()

    def lstat(self, path: str):  # noqa: ANN201
        return self._call(self._inner.lstat, path)

    def stat(self, path: str):  # noqa: ANN201
        return self._call(self._inner.stat, path)

    def readdir(self, path: str):  # noqa: ANN201
        return self._call(self._inner.readdir, path)

    def listdir(self, path: str):  # noqa: ANN201
        return self._call(self._inner.listdir, path)

    def mkdir(self, path: str):  # noqa: ANN201
        return self._call(self._inner.mkdir, path)

    def remove(self, path: str):  # noqa: ANN201
        return self._call(self._inner.remove, path)

    def rmdir(self, path: str):  # noqa: ANN201
        return self._call(self._inner.rmdir, path)

    def readlink(self, path: str):  # noqa: ANN201
        return self._call(self._inner.readlink, path)

    def chmod(self, path: str, mode: int):  # noqa: ANN201
        return self._call(self._inner.chmod, path, mode)

    def posix_rename(self, src: str, dst: str):  # noqa: ANN201
        return self._call(self._inner.posix_rename, src, dst)

    def open(self, path: str, mode: str = "r"):  # noqa: ANN201
        delay = self.delay_s
        self_outer = self
        inner = self._inner

        class _DelayedHandle:
            def __init__(self) -> None:
                self._fh: object | None = None

            def _ensure(self) -> object:
                if self._fh is None:
                    self._fh = inner.open(path, mode)
                return self._fh

            def write(self, data: bytes):  # noqa: ANN202
                async def _w() -> object:
                    self_outer.await_count += 1
                    await asyncio.sleep(delay)
                    return self._ensure().write(data)  # type: ignore[union-attr]

                return _w()

            def read(self, n: int = -1):  # noqa: ANN202
                async def _r() -> object:
                    self_outer.await_count += 1
                    await asyncio.sleep(delay)
                    return self._ensure().read(n)  # type: ignore[union-attr]

                return _r()

            def close(self):  # noqa: ANN202
                async def _c() -> None:
                    self_outer.await_count += 1
                    await asyncio.sleep(delay)
                    fh = self._ensure()
                    close = getattr(fh, "close", None)
                    if callable(close):
                        close()

                return _c()

            def flush(self):  # noqa: ANN202
                async def _f() -> None:
                    self_outer.await_count += 1
                    await asyncio.sleep(delay)
                    fh = self._ensure()
                    flush = getattr(fh, "flush", None)
                    if callable(flush):
                        flush()

                return _f()

        async def _open() -> _DelayedHandle:
            self_outer.await_count += 1
            await asyncio.sleep(delay)
            h = _DelayedHandle()
            h._ensure()  # open after delay
            return h

        return _open()


def test_sftp_list_multi_await_hits_op_budget() -> None:
    """List with many slow-but-under-per-await RPCs hits whole-op TIMEOUT.

    Per-await budget is generous; each await is short; sum exceeds op budget.
    """
    mock = MockSftp()
    mock.dirs.update({"/tree", "/tree/a", "/tree/b"})
    mock.files["/tree/a/f.txt"] = b"x"
    mock.files["/tree/b/g.txt"] = b"y"
    # ~0.12s per await; list+recursive needs several (stat/readdir/parent/...).
    delayed = _DelayedSftp(mock, delay_s=0.12)
    per_await = 1.0  # each await well under this
    op_budget = 0.30  # sum of a few 0.12s sleeps exceeds this
    backend = SftpFs(
        delayed,
        cwd="/",
        home="/home/u",
        timeout_s=per_await,
        op_timeout_s=op_budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.list("/tree", recursive=True)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert "timed out" in ei.value.msg.lower()
    # Whole-op ceiling - not N times per_await (would be multi-second).
    assert elapsed < op_budget + 2.0, f"elapsed={elapsed}s op={op_budget}s"
    assert elapsed < per_await + 1.0, "must not wait a full per-await hang"
    assert delayed.await_count >= 2, "expected multi-await path before TIMEOUT"


def test_sftp_rmtree_multi_await_hits_op_budget() -> None:
    """Recursive rm multi-await sum exceeds op budget -> TIMEOUT."""
    mock = MockSftp()
    mock.dirs.update({"/wipe", "/wipe/sub"})
    mock.files["/wipe/a.txt"] = b"1"
    mock.files["/wipe/sub/b.txt"] = b"2"
    delayed = _DelayedSftp(mock, delay_s=0.12)
    backend = SftpFs(
        delayed,
        cwd="/",
        home="/home/u",
        timeout_s=1.0,
        op_timeout_s=0.30,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.rm("/wipe", recursive=True)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < 0.30 + 2.0
    assert delayed.await_count >= 2


def test_sftp_chunked_put_multi_await_hits_op_budget(tmp_path: Path) -> None:
    """Chunked put (open + many write awaits) hits whole-op TIMEOUT."""
    mock = MockSftp()
    mock.dirs.add("/dest")
    delayed = _DelayedSftp(mock, delay_s=0.10)
    # File size forces >=3 chunk writes with DEFAULT_TRANSFER_CHUNK.
    src = tmp_path / "big.bin"
    n_chunks = 4
    src.write_bytes(b"x" * (DEFAULT_TRANSFER_CHUNK * n_chunks))
    backend = SftpFs(
        delayed,
        cwd="/",
        home="/home/u",
        timeout_s=1.0,
        op_timeout_s=0.28,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.put(str(src), "/dest/big.bin")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < 0.28 + 2.0
    # open + several write/close awaits - not a single-await hang.
    assert delayed.await_count >= 2


def test_sftp_multi_await_under_op_budget_succeeds() -> None:
    """Multi-await list still succeeds when sum stays under op budget."""
    mock = MockSftp()
    mock.dirs.update({"/ok", "/ok/sub"})
    mock.files["/ok/a.txt"] = b"hi"
    delayed = _DelayedSftp(mock, delay_s=0.02)
    backend = SftpFs(
        delayed,
        cwd="/",
        home="/home/u",
        timeout_s=1.0,
        op_timeout_s=2.0,
    )
    result = backend.list("/ok", recursive=True)
    assert result.path == "/ok"
    names = {e.name for e in result.entries}
    assert "a.txt" in names
    assert "sub" in names or any(n.startswith("sub") for n in names)
    assert delayed.await_count >= 2


def test_sftp_op_timeout_configurable_independent_of_per_await() -> None:
    """op_timeout_s is independently configurable from timeout_s."""
    mock = MockSftp()
    backend = SftpFs(
        mock,
        cwd="/",
        home="/home/u",
        timeout_s=30.0,
        op_timeout_s=120.0,
    )
    assert backend._timeout_s == 30.0
    assert backend._op_timeout_s == 120.0
    # Omit op_timeout_s -> mirrors timeout_s.
    backend2 = SftpFs(mock, cwd="/", home="/home/u", timeout_s=5.0)
    assert backend2._timeout_s == 5.0
    assert backend2._op_timeout_s == 5.0


# ---------------------------------------------------------------------------
# sftp: TIMEOUT on follow-stat / readlink / list parent-or-child stat
# ---------------------------------------------------------------------------


class _SelectiveHangSftp:
    """Delegate to ``MockSftp``; selected methods hang on selected paths.

    ``no_follow`` makes ``stat`` report the lstat kind (link stays a link)
    so ``_is_existing_dir`` falls through to ``readlink``. ``no_readdir``
    hides readdir so list uses bare ``listdir`` + per-child stat.
    """

    def __init__(
        self,
        inner: MockSftp,
        *,
        hang_stat: frozenset[str] = frozenset(),
        hang_lstat: frozenset[str] = frozenset(),
        hang_readlink: frozenset[str] = frozenset(),
        no_follow: bool = False,
        no_readdir: bool = False,
    ) -> None:
        self._inner = inner
        self._hang_stat = hang_stat
        self._hang_lstat = hang_lstat
        self._hang_readlink = hang_readlink
        self._no_follow = no_follow
        self._no_readdir = no_readdir

    def __getattr__(self, name: str) -> object:
        if name == "readdir" and self._no_readdir:
            raise AttributeError(name)
        return getattr(self._inner, name)

    @staticmethod
    async def _hang() -> None:
        await asyncio.sleep(3600.0)
        raise RuntimeError("hang should have timed out")

    def lstat(self, path: str) -> object:
        if self._inner._norm(path) in self._hang_lstat:
            return self._hang()
        return self._inner.lstat(path)

    def stat(self, path: str) -> object:
        if self._inner._norm(path) in self._hang_stat:
            return self._hang()
        if self._no_follow:
            return self._inner.lstat(path)
        return self._inner.stat(path)

    def readlink(self, path: str) -> object:
        if self._inner._norm(path) in self._hang_readlink:
            return self._hang()
        return self._inner.readlink(path)


def _symlink_to_dir_tree() -> MockSftp:
    mock = MockSftp()
    mock.dirs.update({"/srv", "/var", "/var/www"})
    mock.links["/srv/www"] = "/var/www"
    return mock


def test_sftp_write_follow_stat_hang_on_symlink_dir_parent_is_timeout() -> None:
    """Hung follow-stat on a symlink-to-dir parent is TIMEOUT, not ALREADY_EXISTS."""
    mock = _symlink_to_dir_tree()
    wrapped = _SelectiveHangSftp(mock, hang_stat=frozenset({"/srv/www"}))
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/srv/www/app.conf",
        content="listen=80\n",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert r.code != "ALREADY_EXISTS"
    assert "/srv/www/app.conf" not in mock.files
    assert mock.links.get("/srv/www") == "/var/www"
    assert elapsed < budget + 2.0


def test_sftp_mkdir_readlink_hang_on_symlink_dir_parent_is_timeout() -> None:
    """Hung readlink on a symlink-to-dir parent is TIMEOUT, not ALREADY_EXISTS."""
    mock = _symlink_to_dir_tree()
    wrapped = _SelectiveHangSftp(
        mock,
        hang_readlink=frozenset({"/srv/www"}),
        no_follow=True,
    )
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    r = fs_ops.run(
        "mkdir",
        ep="lab-ssh",
        path="/srv/www/releases",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert r.code != "ALREADY_EXISTS"
    assert "/srv/www/releases" not in mock.dirs
    assert mock.links.get("/srv/www") == "/var/www"
    assert elapsed < budget + 2.0


def test_sftp_write_readlink_timeout_does_not_replace_symlink() -> None:
    """Dest is a link; hung readlink fails write and leaves the symlink."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.txt"] = b"old-content"
    mock.links["/d/link"] = "/d/target.txt"
    wrapped = _SelectiveHangSftp(mock, hang_readlink=frozenset({"/d/link"}))
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/link",
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert "/d/link" in mock.links
    assert mock.links["/d/link"] == "/d/target.txt"
    assert "/d/link" not in mock.files
    assert mock.files["/d/target.txt"] == b"old-content"
    assert elapsed < budget + 2.0


def test_sftp_put_readlink_timeout_does_not_replace_symlink(tmp_path: Path) -> None:
    """Dest is a link; hung readlink fails put and leaves the symlink."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.bin"] = b"old"
    mock.links["/d/link.bin"] = "/d/target.bin"
    wrapped = _SelectiveHangSftp(mock, hang_readlink=frozenset({"/d/link.bin"}))
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-link")
    t0 = time.monotonic()
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/link.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert "/d/link.bin" in mock.links
    assert mock.links["/d/link.bin"] == "/d/target.bin"
    assert "/d/link.bin" not in mock.files
    assert mock.files["/d/target.bin"] == b"old"
    assert elapsed < budget + 2.0


def test_sftp_list_parent_stat_timeout_fails() -> None:
    """Hung parent ``_stat`` fails list; do not return ok with a stub ``..``."""
    mock = MockSftp()
    mock.dirs.update({"/tree"})
    mock.files["/tree/a.txt"] = b"x"
    wrapped = _SelectiveHangSftp(mock, hang_lstat=frozenset({"/", "/tree/.."}))
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.list("/tree")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < budget + 2.0


def test_sftp_list_child_stat_timeout_fails() -> None:
    """Hung per-child ``_stat`` fails list; do not return ok with kind=o."""
    mock = MockSftp()
    mock.dirs.update({"/tree"})
    mock.files["/tree/a.txt"] = b"x"
    wrapped = _SelectiveHangSftp(
        mock,
        hang_lstat=frozenset({"/tree/a.txt"}),
        no_readdir=True,
    )
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.list("/tree")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < budget + 2.0


def test_sftp_list_child_stat_not_found_still_kind_o() -> None:
    """Non-timeout per-child stat miss still stubs kind=o (listing stays ok)."""
    mock = MockSftp()
    mock.dirs.update({"/tree"})
    mock.files["/tree/a.txt"] = b"x"

    class _VanishChild:
        def __init__(self, inner: MockSftp) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            if name == "readdir":
                raise AttributeError(name)
            return getattr(self._inner, name)

        def lstat(self, path: str) -> object:
            if self._inner._norm(path) == "/tree/a.txt":
                raise FileNotFoundError(path)
            return self._inner.lstat(path)

        def stat(self, path: str) -> object:
            return self.lstat(path)

    backend = SftpFs(_VanishChild(mock), cwd="/", home="/home/u")
    result = backend.list("/tree")
    by_name = {e.name: e for e in result.entries}
    assert "a.txt" in by_name
    assert by_name["a.txt"].kind == "o"


# ---------------------------------------------------------------------------
# dest-stat TIMEOUT: do not treat dest as new / do not demote 0600
# ---------------------------------------------------------------------------


class _HangPathStatAfter:
    """Delegate to ``MockSftp``; dest lstat/stat succeed ``allow`` times then hang.

    Write exist-stat + resolve + mode-stat each hit dest once; put skips
    exist-stat (resolve then mode-stat). Shared hit counter covers both
    lstat and follow-stat on the same path.
    """

    def __init__(self, inner: MockSftp, hang_path: str, *, allow: int) -> None:
        self._inner = inner
        self._hang_path = inner._norm(hang_path)
        self._allow = int(allow)
        self._hits = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @staticmethod
    async def _hang() -> None:
        await asyncio.sleep(3600.0)
        raise RuntimeError("hang should have timed out")

    def _maybe_hang(self, path: str) -> object | None:
        if self._inner._norm(path) != self._hang_path:
            return None
        self._hits += 1
        if self._hits > self._allow:
            return self._hang()
        return None

    def lstat(self, path: str) -> object:
        hung = self._maybe_hang(path)
        if hung is not None:
            return hung
        return self._inner.lstat(path)

    def stat(self, path: str) -> object:
        hung = self._maybe_hang(path)
        if hung is not None:
            return hung
        return self._inner.stat(path)


def _restrictive_dest() -> tuple[MockSftp, str]:
    mock = MockSftp()
    mock.dirs.add("/home")
    mock.dirs.add("/home/u")
    mock.dirs.add("/home/u/.aws")
    dest = "/home/u/.aws/credentials"
    mock.files[dest] = b"aws_access_key_id=OLD\n"
    mock.file_modes[dest] = 0o600
    return mock, dest


def test_sftp_write_dest_mode_stat_hang_is_timeout() -> None:
    """Hung dest-stat while capturing mode fails write; dest stays 0600."""
    mock, dest = _restrictive_dest()
    # exist-stat + resolve-stat succeed; mode-stat hangs.
    wrapped = _HangPathStatAfter(mock, dest, allow=2)
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/home/u", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path=dest,
        content="aws_access_key_id=NEW\n",
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert mock.files[dest] == b"aws_access_key_id=OLD\n"
    assert mock._file_mode(dest) == 0o600
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert elapsed < budget + 2.0


def test_sftp_put_dest_mode_stat_hang_is_timeout(tmp_path: Path) -> None:
    """Hung dest-stat while capturing mode fails put; dest stays 0600."""
    mock = MockSftp()
    mock.dirs.add("/d")
    dest = "/d/secret.bin"
    mock.files[dest] = b"old-payload"
    mock.file_modes[dest] = 0o600
    # resolve-stat succeeds; mode-stat (after temp write) hangs.
    wrapped = _HangPathStatAfter(mock, dest, allow=1)
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/", home="/home/u", timeout_s=budget)
    src = tmp_path / "local.bin"
    src.write_bytes(b"new-payload-from-put")
    t0 = time.monotonic()
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert mock.files[dest] == b"old-payload"
    assert mock._file_mode(dest) == 0o600
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert elapsed < budget + 2.0


def test_sftp_write_exist_stat_hang_does_not_continue() -> None:
    """First dest-stat hang fails write; no temp promote, created stays unset."""
    mock, dest = _restrictive_dest()
    wrapped = _HangPathStatAfter(mock, dest, allow=0)
    budget = 0.15
    backend = SftpFs(wrapped, cwd="/home/u", home="/home/u", timeout_s=budget)
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.write(dest, "aws_access_key_id=NEW\n")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert mock.files[dest] == b"aws_access_key_id=OLD\n"
    assert mock._file_mode(dest) == 0o600
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert elapsed < budget + 2.0

