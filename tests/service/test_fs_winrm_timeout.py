"""Service tests: WinRM FS hang deadline and whole-op budget."""

from __future__ import annotations

import re
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from _winrm_fakes import HOME, TEMP, FakePypsrpSession
from test_fs_winrm import MockWinrmFileClient

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.fs.backends.winrm import (
    _CLEANUP_MIN_BUDGET_S,
    _CLEANUP_SWEEP_GAP_S,
    PypsrpFileClient,
    WinrmFs,
)
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport.base import BaseTransport


class _PsStreams:
    """Minimal stand-in for pypsrp PSDataStreams (``.error`` list)."""

    def __init__(self, errors: list[str] | None = None) -> None:
        self.error = list(errors or [])


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# PypsrpFileClient / WinrmFs execute_ps wall-clock deadline.
# Hung remote oneshot must surface FsError(TIMEOUT) within budget so
# MCP/worker threads are not pinned forever.
# ---------------------------------------------------------------------------


class _HangForeverPsSession:
    """Session whose execute_ps blocks until released (or forever).

    Uses threading.Event so tests can assert TIMEOUT without orphaning a
    daemon forever when the suite ends - the hang event is set in teardown
    paths if needed; the timeout path abandons the bridge await.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.ps_calls: list[str] = []

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, _PsStreams, bool]:
        del environment
        self.ps_calls.append(script)
        self.entered.set()
        # Block forever unless a test explicitly releases (cleanup).
        self.release.wait()
        return ("", _PsStreams([]), False)


def test_pypsrp_execute_ps_hang_raises_timeout() -> None:
    """Mock execute_ps that never returns -> FsError(TIMEOUT) within budget."""
    import time

    from mcp_remote_control.fs.backends.winrm import DEFAULT_WINRM_FS_TIMEOUT_S

    assert DEFAULT_WINRM_FS_TIMEOUT_S == 60.0

    sess = _HangForeverPsSession()
    budget = 0.4
    client = PypsrpFileClient(sess, timeout_s=budget)

    t0 = time.monotonic()
    with pytest.raises(FsError) as excinfo:
        client.stat(rf"{TEMP}\hang.txt")
    elapsed = time.monotonic() - t0

    err = excinfo.value
    assert err.code == "TIMEOUT"
    assert "timed out" in err.msg.lower()
    assert err.details.get("timeout_s") == budget
    # Must fail within budget + small bridge slack, not hang for minutes.
    assert elapsed < budget + 2.0, f"TIMEOUT took too long: {elapsed:.2f}s"
    assert elapsed >= budget * 0.5, f"TIMEOUT fired too early: {elapsed:.2f}s"
    # Caller returned; release the orphaned worker so the suite can exit cleanly.
    sess.release.set()
    assert sess.entered.wait(timeout=1.0)


def test_winrm_fs_stat_surfaces_execute_ps_timeout() -> None:
    """WinrmFs.stat through hung PypsrpFileClient -> TIMEOUT within budget."""
    import time

    sess = _HangForeverPsSession()
    budget = 0.4
    client = PypsrpFileClient(sess, timeout_s=budget)
    backend = WinrmFs(client, cwd=HOME, home=HOME, timeout_s=budget)

    t0 = time.monotonic()
    with pytest.raises(FsError) as excinfo:
        backend.stat(rf"{TEMP}\hang-stat.txt")
    elapsed = time.monotonic() - t0

    err = excinfo.value
    assert err.code == "TIMEOUT"
    assert elapsed < budget + 2.0
    sess.release.set()


def test_winrm_fs_list_surfaces_execute_ps_timeout() -> None:
    """WinrmFs.list also surfaces TIMEOUT (not only stat)."""
    import time

    sess = _HangForeverPsSession()
    budget = 0.4
    client = PypsrpFileClient(sess, timeout_s=budget)
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    t0 = time.monotonic()
    with pytest.raises(FsError) as excinfo:
        backend.list(TEMP)
    elapsed = time.monotonic() - t0

    assert excinfo.value.code == "TIMEOUT"
    assert elapsed < budget + 2.0
    sess.release.set()


def test_winrm_fs_timeout_s_pushed_onto_pypsrp_client() -> None:
    """WinrmFs(timeout_s=...) overwrites PypsrpFileClient budget."""
    sess = FakePypsrpSession()
    # Client starts with a large default; backend shortens it.
    client = PypsrpFileClient(sess, timeout_s=99.0)
    backend = WinrmFs(client, cwd=HOME, home=HOME, timeout_s=1.5)
    assert client._timeout_s == 1.5  # noqa: SLF001
    # Normal ops still green under a finite budget.
    target = rf"{TEMP}\budget-ok.txt"
    sess.files[target] = b"ok"
    info = backend.stat(target)
    assert info.kind == "file"
    assert info.size == 2


def test_winrm_fs_normal_ops_still_green_with_default_timeout() -> None:
    """list/stat/read/write/put/get unchanged under default budget."""
    import tempfile

    sess = FakePypsrpSession(has_copy=True, has_fetch=True)
    client = PypsrpFileClient(sess)  # default DEFAULT_WINRM_FS_TIMEOUT_S
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    assert client._timeout_s == 60.0  # noqa: SLF001

    work = rf"{TEMP}\q5-green"
    backend.mkdir(work)
    note = rf"{work}\note.txt"
    backend.write(note, "q5-body\n")
    st = backend.stat(note)
    assert st.kind == "file"
    lst = backend.list(work)
    assert any(e.name == "note.txt" for e in lst.entries)
    rd = backend.read(note)
    assert b"q5-body" in rd.data

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "up.bin"
        src.write_bytes(b"\x01\x02")
        remote = rf"{work}\up.bin"
        backend.put(str(src), remote)
        dst = Path(td) / "down.bin"
        backend.get(remote, str(dst))
        assert dst.read_bytes() == b"\x01\x02"


# ---------------------------------------------------------------------------
# Whole-op wall-clock budget across multi-RT WinRM ops.
# Each execute_ps / client RT is under the per-call limit, but the sum
# must hit op_timeout_s so recursive list / rmtree fallback / multi-step
# put cannot approach N times 60s.
# ---------------------------------------------------------------------------


class _DelayedPsSession:
    """Wrap a FakePypsrpSession: every execute_ps sleeps then delegates.

    Each RT is short enough to pass a generous per-call budget, but a multi-RT
    public op can exceed a tight whole-op budget.
    """

    def __init__(self, inner: FakePypsrpSession, delay_s: float) -> None:
        self._inner = inner
        self.delay_s = float(delay_s)
        self.call_count = 0
        # Expose FS stores for test setup (same objects as inner).
        self.dirs = inner.dirs
        self.files = inner.files
        self.ps_calls = inner.ps_calls

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        self.call_count += 1
        import time

        time.sleep(self.delay_s)
        return self._inner.execute_ps(script, environment=environment)


class _DelayedMockWinrm:
    """MockWinrmFileClient wrapper: sleep on every RT-shaped method.

    Does not implement ``rmtree`` so WinrmFs uses the multi-RT fallback path.
    """

    # Keep native copy/fetch so put/get paths stay simple when used.
    has_native_copy = True
    has_native_fetch = True

    def __init__(self, inner: MockWinrmFileClient, delay_s: float) -> None:
        self._inner = inner
        self.delay_s = float(delay_s)
        self.call_count = 0
        self.files = inner.files
        self.dirs = inner.dirs
        self.links = inner.links

    def _rt(self) -> None:
        import time

        self.call_count += 1
        time.sleep(self.delay_s)

    def listdir(self, path: str) -> list[str]:
        self._rt()
        return self._inner.listdir(path)

    def stat(self, path: str) -> object:
        self._rt()
        return self._inner.stat(path)

    def readlink(self, path: str) -> str:
        self._rt()
        return self._inner.readlink(path)

    def mkdir(self, path: str) -> None:
        self._rt()
        return self._inner.mkdir(path)

    def remove(self, path: str) -> None:
        self._rt()
        return self._inner.remove(path)

    def rmdir(self, path: str) -> None:
        self._rt()
        return self._inner.rmdir(path)

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        self._rt()
        return self._inner.read_file(path, max_bytes=max_bytes)

    def write_file(self, path: str, data: bytes) -> None:
        self._rt()
        return self._inner.write_file(path, data)

    def copy(self, local: str, remote: str) -> None:
        self._rt()
        return self._inner.copy(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        self._rt()
        return self._inner.fetch(remote, local)

    def open(self, path: str, mode: str = "rb") -> object:
        """SupportsFileOpen surface with delayed write/read for multi-chunk put."""
        self._rt()
        outer = self
        # Materialize a simple handle over the in-memory store.
        store = self._inner
        buf = bytearray()
        is_write = "w" in mode

        class _H:
            def write(self, data: bytes) -> int:  # noqa: ANN001
                outer._rt()
                buf.extend(data)
                return len(data)

            def read(self, n: int = -1) -> bytes:  # noqa: ANN001
                outer._rt()
                return b""

            def flush(self) -> None:
                outer._rt()

            def close(self) -> None:
                outer._rt()
                if is_write:
                    store.write_file(path, bytes(buf))

        return _H()

    def rename(self, src: str, dst: str) -> None:
        self._rt()
        data = self._inner.files.pop(self._inner._norm(src), b"")
        self._inner.write_file(dst, data)


def test_winrm_fs_op_timeout_configurable_independent_of_per_call() -> None:
    """op_timeout_s is independently configurable from timeout_s."""
    from mcp_remote_control.fs.backends.winrm import (
        DEFAULT_WINRM_FS_OP_TIMEOUT_S,
        DEFAULT_WINRM_FS_TIMEOUT_S,
    )

    assert DEFAULT_WINRM_FS_OP_TIMEOUT_S == DEFAULT_WINRM_FS_TIMEOUT_S
    assert DEFAULT_WINRM_FS_TIMEOUT_S == 60.0

    sess = FakePypsrpSession()
    client = PypsrpFileClient(sess, timeout_s=30.0)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=30.0,
        op_timeout_s=120.0,
    )
    assert backend._timeout_s == 30.0  # noqa: SLF001
    assert backend._op_timeout_s == 120.0  # noqa: SLF001
    # Omit op_timeout_s -> mirrors timeout_s.
    backend2 = WinrmFs(client, cwd=HOME, home=HOME, timeout_s=5.0)
    assert backend2._timeout_s == 5.0  # noqa: SLF001
    assert backend2._op_timeout_s == 5.0  # noqa: SLF001
    # Both omitted -> default 60s whole-op ceiling.
    client3 = PypsrpFileClient(FakePypsrpSession())
    backend3 = WinrmFs(client3, cwd=HOME, home=HOME)
    assert backend3._op_timeout_s == DEFAULT_WINRM_FS_TIMEOUT_S  # noqa: SLF001
    # timeout_s omitted on WinrmFs -> mirror client per-call for op default.
    client4 = PypsrpFileClient(FakePypsrpSession(), timeout_s=7.5)
    backend4 = WinrmFs(client4, cwd=HOME, home=HOME)
    assert backend4._timeout_s is None  # noqa: SLF001
    assert backend4._op_timeout_s == 7.5  # noqa: SLF001


def test_winrm_list_multi_rt_hits_op_budget() -> None:
    """Recursive list multi-RT sum exceeds op budget -> TIMEOUT.

    Per-call budget is generous; each execute_ps is short; sum exceeds op.
    """
    import time

    sess = FakePypsrpSession()
    tree = rf"{TEMP}\tree"
    sess.dirs.update({tree, rf"{tree}\a", rf"{tree}\b", rf"{tree}\a\nested"})
    sess.files[rf"{tree}\a\f.txt"] = b"x"
    sess.files[rf"{tree}\b\g.txt"] = b"y"
    delayed = _DelayedPsSession(sess, delay_s=0.12)
    per_call = 1.0
    op_budget = 0.30
    client = PypsrpFileClient(delayed, timeout_s=per_call)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=per_call,
        op_timeout_s=op_budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.list(tree, recursive=True)
    elapsed = time.monotonic() - t0
    err = ei.value
    assert err.code == "TIMEOUT"
    assert "timed out" in err.msg.lower()
    # Machine-readable TIMEOUT fields.
    assert "timeout_s" in (err.details or {})
    assert err.details.get("op_timeout_s") == op_budget or (
        err.details.get("timeout_s") == op_budget
    )
    # Whole-op ceiling - not N times per_call (would be multi-second).
    assert elapsed < op_budget + 2.0, f"elapsed={elapsed}s op={op_budget}s"
    assert elapsed < per_call + 1.0, "must not wait a full per-call hang"
    assert delayed.call_count >= 2, "expected multi-RT path before TIMEOUT"


def test_winrm_rmtree_multi_rt_hits_op_budget() -> None:
    """Recursive rm fallback multi-RT sum exceeds op budget -> TIMEOUT."""
    import time

    store = MockWinrmFileClient()
    wipe = rf"{TEMP}\wipe"
    store.dirs.update({wipe, rf"{wipe}\sub", rf"{wipe}\sub\deep"})
    store.files[rf"{wipe}\a.txt"] = b"1"
    store.files[rf"{wipe}\sub\b.txt"] = b"2"
    delayed = _DelayedMockWinrm(store, delay_s=0.12)
    op_budget = 0.30
    backend = WinrmFs(
        delayed,
        cwd=HOME,
        home=HOME,
        timeout_s=1.0,
        op_timeout_s=op_budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.rm(wipe, recursive=True)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < op_budget + 2.0
    assert delayed.call_count >= 2
    assert ei.value.details is not None
    assert "timeout_s" in ei.value.details
    assert ei.value.details.get("op_timeout_s") == op_budget


def test_winrm_put_multi_rt_hits_op_budget(tmp_path: Path) -> None:
    """Multi-step put (mkdir_p parents + open/write chunks) hits op budget."""
    import time

    from mcp_remote_control.fs.types import DEFAULT_TRANSFER_CHUNK

    store = MockWinrmFileClient()
    # Deep parent forces several mkdir_p stats/mkdirs before open/write.
    deep = rf"{TEMP}\put\a\b\c"
    store.dirs.add(rf"{TEMP}\put")  # only shallow root exists
    delayed = _DelayedMockWinrm(store, delay_s=0.10)
    src = tmp_path / "big.bin"
    n_chunks = 4
    src.write_bytes(b"x" * (DEFAULT_TRANSFER_CHUNK * n_chunks))
    op_budget = 0.28
    backend = WinrmFs(
        delayed,
        cwd=HOME,
        home=HOME,
        timeout_s=1.0,
        op_timeout_s=op_budget,
    )
    # progress= forces SupportsFileOpen multi-chunk path (open + writes).
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.put(str(src), rf"{deep}\big.bin", progress=lambda _c, _t: None)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < op_budget + 2.0
    assert delayed.call_count >= 2


def test_winrm_multi_rt_under_op_budget_succeeds() -> None:
    """Multi-RT list still succeeds when sum stays under op budget."""
    sess = FakePypsrpSession()
    ok = rf"{TEMP}\ok"
    sess.dirs.update({ok, rf"{ok}\sub"})
    sess.files[rf"{ok}\a.txt"] = b"hi"
    delayed = _DelayedPsSession(sess, delay_s=0.02)
    client = PypsrpFileClient(delayed, timeout_s=1.0)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=1.0,
        op_timeout_s=2.0,
    )
    result = backend.list(ok, recursive=True)
    assert result.path == ok
    names = {e.name for e in result.entries}
    assert "a.txt" in names
    assert "sub" in names or any(n.startswith("sub") for n in names)
    assert delayed.call_count >= 2


# ---------------------------------------------------------------------------
# Native copy/fetch wall-clock; junction-parent / rmtree TIMEOUT honesty
# ---------------------------------------------------------------------------


class _HangCopyFetchSession:
    """Session whose native copy/fetch block until released.

    ``execute_ps`` stays instant so mkdir_p / stat around the transfer can
    finish; only the native transfer hangs.
    """

    def __init__(self, inner: FakePypsrpSession) -> None:
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.copy_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.dirs = inner.dirs
        self.files = inner.files
        self.ps_calls = inner.ps_calls

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        return self._inner.execute_ps(script, environment=environment)

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.entered.set()
        self.release.wait()

    def fetch(self, remote: str, local: str) -> None:
        self.fetch_calls.append((remote, local))
        self.entered.set()
        self.release.wait()


class _HangNativeCopyClient(MockWinrmFileClient):
    """Mock client whose ``copy``/``fetch`` hang (no pypsrp adapter)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def copy(self, local: str, remote: str) -> None:
        del local, remote
        self.entered.set()
        self.release.wait()

    def fetch(self, remote: str, local: str) -> None:
        del remote, local
        self.entered.set()
        self.release.wait()


def test_pypsrp_native_copy_hang_raises_timeout(tmp_path: Path) -> None:
    """Hung session.copy -> FsError(TIMEOUT) inside the per-call budget."""
    import time

    inner = FakePypsrpSession()
    sess = _HangCopyFetchSession(inner)
    budget = 0.4
    client = PypsrpFileClient(sess, timeout_s=budget)
    src = tmp_path / "up.bin"
    src.write_bytes(b"payload")
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        client.copy(str(src), rf"{TEMP}\hang-copy.bin")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert "timed out" in ei.value.msg.lower()
    assert elapsed < budget + 2.0
    assert elapsed >= budget * 0.5
    sess.release.set()


def test_pypsrp_native_fetch_hang_raises_timeout(tmp_path: Path) -> None:
    """Hung session.fetch -> FsError(TIMEOUT) inside the per-call budget."""
    import time

    inner = FakePypsrpSession()
    inner.files[rf"{TEMP}\hang-fetch.bin"] = b"remote"
    sess = _HangCopyFetchSession(inner)
    budget = 0.4
    client = PypsrpFileClient(sess, timeout_s=budget)
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        client.fetch(rf"{TEMP}\hang-fetch.bin", str(tmp_path / "out.bin"))
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < budget + 2.0
    assert elapsed >= budget * 0.5
    sess.release.set()


def test_winrm_put_native_copy_hang_hits_op_budget(tmp_path: Path) -> None:
    """Hung native copy through WinrmFs.put times out inside op_timeout_s."""
    import time

    store = _HangNativeCopyClient()
    src = tmp_path / "up.bin"
    src.write_bytes(b"payload")
    op_budget = 0.35
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.put(str(src), rf"{TEMP}\hang-put.bin")
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < op_budget + 2.0
    store.release.set()


class _AbandonedCopySession:
    """FakePypsrpSession wrapper: event-gated native copy and remote remove.

    ``copy`` lands the full payload on the sibling temp at once and then keeps
    the round trip open, so the put's whole-op budget expires while the temp is
    already on the host - the state that used to leave a hidden full-payload
    temp behind. ``remove`` holds its round trip until the test releases it, so
    a stalled delete is pinned by an event instead of by wall-clock jitter.
    """

    def __init__(self, inner: FakePypsrpSession) -> None:
        self._inner = inner
        self.dirs = inner.dirs
        self.files = inner.files
        self.links = inner.links
        self.ps_calls = inner.ps_calls
        self.copy_calls: list[tuple[str, str]] = []
        self.remove_calls: list[str] = []
        self.copy_entered = threading.Event()
        self.copy_release = threading.Event()
        self.remove_entered = threading.Event()
        self.remove_release = threading.Event()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        # The standalone delete script only: the promote's and the write's
        # catch blocks name Remove-Item too, and they are not cleanup.
        if (
            "Remove-Item" in script
            and "Move-Item" not in script
            and "WriteAllBytes" not in script
        ):
            self.remove_calls.append(script)
            self.remove_entered.set()
            self.remove_release.wait(timeout=10.0)
        return self._inner.execute_ps(script, environment=environment)

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self._inner.copy(local, remote)
        self.copy_entered.set()
        self.copy_release.wait(timeout=10.0)


class _LateLandingCopySession:
    """FakePypsrpSession wrapper: the temp lands only once the copy is released.

    The native copy keeps its round trip open with the payload still local, so
    the temp appears on the host after the put's whole-op budget expired - the
    late arrival the cleanup sweeps for. ``release_after_removes`` releases that
    copy when the cleanup has already retried the remove that many times, and
    later attempts wait for the landing itself, so the interleaving is pinned
    by events instead of by wall-clock jitter.
    """

    def __init__(
        self, inner: FakePypsrpSession, *, release_after_removes: int
    ) -> None:
        self._inner = inner
        self.dirs = inner.dirs
        self.files = inner.files
        self.links = inner.links
        self.ps_calls = inner.ps_calls
        self.copy_calls: list[tuple[str, str]] = []
        self.remove_calls: list[str] = []
        self.copy_entered = threading.Event()
        self.copy_release = threading.Event()
        self.copy_landed = threading.Event()
        self.release_after_removes = release_after_removes

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        # The standalone delete script only: the promote's and the write's
        # catch blocks name Remove-Item too, and they are not cleanup.
        if (
            "Remove-Item" in script
            and "Move-Item" not in script
            and "WriteAllBytes" not in script
        ):
            self.remove_calls.append(script)
            if len(self.remove_calls) == self.release_after_removes:
                self.copy_release.set()
            elif len(self.remove_calls) > self.release_after_removes:
                # Only a retry after the landing can see the temp, so this
                # attempt waits for it: the sweep, not the release, is what
                # erases the late arrival.
                assert self.copy_landed.wait(timeout=5.0), (
                    "the abandoned copy never landed its payload"
                )
        return self._inner.execute_ps(script, environment=environment)

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.copy_entered.set()
        self.copy_release.wait(timeout=10.0)
        self._inner.copy(local, remote)
        self.copy_landed.set()


def _client_under_the_transport_lock(store: Any) -> PypsrpFileClient:
    """Client wired to the production transport lock.

    Production passes the transport's serial ops, so a cleanup remove must pass
    the same timed lock gate as any other session call before its delete can be
    issued.
    """
    transport = BaseTransport()
    return PypsrpFileClient(
        store,
        timeout_s=2.0,
        serial_ops=transport.serial_ops,
        serial_ops_within=transport.serial_ops_within,
    )


def test_winrm_put_budget_spent_after_native_copy_leaves_no_temp(
    tmp_path: Path,
) -> None:
    """A budget-expired native put erases its full-payload temp.

    The native copy lands the payload on the sibling temp, then the budget
    runs out while that round trip is still open: the put reports TIMEOUT with
    the payload already on the host. Cleanup must still erase the temp (its own
    bounded budget, not the spent whole-op deadline) and the prior destination
    must stay intact.
    """
    import time

    sess = FakePypsrpSession(has_copy=True)
    store = _AbandonedCopySession(sess)
    dest = rf"{TEMP}\budget-put.bin"
    sess.files[dest] = b"prior-good"
    op_budget = 0.4
    client = _client_under_the_transport_lock(store)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = 1.0  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"fresh-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never landed"
    # The barrier: the spent budget must not stop the cleanup from issuing its
    # own bounded remote remove, so wait for that call before releasing the
    # blocked round trips and letting the put return.
    cleanup_called = store.remove_entered.wait(timeout=op_budget + 2.0)
    store.remove_release.set()
    store.copy_release.set()
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - t0

    assert not worker.is_alive(), "put did not return"
    assert errors and isinstance(errors[0], FsError)
    assert errors[0].code == "TIMEOUT"
    assert store.copy_calls and ".mrc-tmp-" in store.copy_calls[0][1]
    assert sess.files.get(dest) == b"prior-good", "prior destination changed"
    assert not any(".mrc-tmp-" in k for k in sess.files), (
        "a budget-expired native put left its full-payload temp on the host"
    )
    assert cleanup_called, "the error-path cleanup issued no remote remove"
    # Bounded by the cleanup's own budget (1.0s here), never by the spent op
    # deadline and never unbounded.
    assert elapsed < op_budget + 1.0 + 1.0, f"elapsed={elapsed:.2f}s"


def test_winrm_put_stalled_cleanup_delete_is_bounded(tmp_path: Path) -> None:
    """A stalled cleanup delete cannot pin the call past its own budget.

    Same budget-expired native put, but the cleanup's own remote remove never
    returns. The put must still come back within the op budget plus the cleanup
    budget: cleanup is a bounded best-effort step, never an unbounded teardown.
    """
    import time

    sess = FakePypsrpSession(has_copy=True)
    store = _AbandonedCopySession(sess)
    dest = rf"{TEMP}\stall-put.bin"
    sess.files[dest] = b"prior-good"
    op_budget = 0.4
    cleanup_budget = 0.5
    client = _client_under_the_transport_lock(store)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = cleanup_budget  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"fresh-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never landed"
    # The copy stays abandoned: the budget expires while its round trip is
    # still open, and the cleanup's own delete is the thing that stalls.
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - t0
    try:
        assert not worker.is_alive(), "a stalled cleanup delete pinned the put"
        assert errors and isinstance(errors[0], FsError)
        assert errors[0].code == "TIMEOUT"
        assert store.remove_entered.is_set(), "cleanup never attempted the delete"
        assert elapsed < op_budget + cleanup_budget + 1.0, f"elapsed={elapsed:.2f}s"
    finally:
        # Release the stalled round trips so no helper thread outlives the test.
        store.remove_release.set()
        store.copy_release.set()


def test_winrm_put_sweep_erases_a_temp_landed_late_in_its_budget(
    tmp_path: Path,
) -> None:
    """The cleanup sweep spans its budget, not a fixed number of attempts.

    The abandoned copy lands its payload only after the cleanup has retried the
    remove several times, so the temp appears on the host for the tail of the
    sweep. A sweep bounded by its own budget still sees that late arrival and
    erases it, and the prior destination stays intact.
    """
    import time

    sess = FakePypsrpSession(has_copy=True)
    store = _LateLandingCopySession(sess, release_after_removes=6)
    dest = rf"{TEMP}\sweep-put.bin"
    sess.files[dest] = b"prior-good"
    op_budget = 0.4
    cleanup_budget = 1.5
    client = _client_under_the_transport_lock(store)
    backend = WinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = cleanup_budget  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"late-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never started"
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - t0

    assert not worker.is_alive(), "put did not return"
    assert store.copy_landed.is_set(), "the abandoned copy never landed"
    assert errors and isinstance(errors[0], FsError)
    assert errors[0].code == "TIMEOUT"
    assert store.copy_calls and ".mrc-tmp-" in store.copy_calls[0][1]
    assert sess.files.get(dest) == b"prior-good", "prior destination changed"
    assert not any(".mrc-tmp-" in k for k in sess.files), (
        "a temp that landed inside the sweep budget survived the cleanup"
    )
    assert len(store.remove_calls) > store.release_after_removes, (
        "the sweep gave up before its own budget was spent"
    )
    assert elapsed < op_budget + cleanup_budget + 1.0, f"elapsed={elapsed:.2f}s"


class _SweepBoundaryRecordingWinrmFs(WinrmFs):
    """WinrmFs recording when each cleanup sweep starts.

    A sweep's own end is that start plus the cleanup budget, so a test can tell
    a delete issued after the sweep spent its budget from a retry that ran
    inside it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sweep_starts: list[float] = []

    def _best_effort_remove(
        self, client: Any, path: str, *, sweep: bool = False
    ) -> None:
        if sweep:
            self.sweep_starts.append(time.monotonic())
        return super()._best_effort_remove(client, path, sweep=sweep)


class _FinalSliceLandingCopySession:
    """FakePypsrpSession wrapper: the payload lands in the sweep's LAST wait.

    The native copy is abandoned while it waits on an event and then performs a
    real ``shutil.copyfile`` into a real directory, and the cleanup's standalone
    delete script is a real ``os.unlink`` of that file: neither the late landing
    nor the delete's result is modelled. The copy is released by the delete that
    opens the sweep's final waiting slice - the last periodic delete, the one
    after which the sweep only waits - and that delete waits for the payload
    before returning, so the landing strictly precedes every delete issued
    after it instead of relying on the sweep's final sleep to order them.
    """

    def __init__(
        self,
        inner: FakePypsrpSession,
        *,
        disk_root: Path,
        release_after_removes: int,
    ) -> None:
        self._inner = inner
        self._disk_root = disk_root
        self.release_after_removes = release_after_removes
        self.dirs = inner.dirs
        self.files = inner.files
        self.links = inner.links
        self.ps_calls = inner.ps_calls
        self.copy_calls: list[tuple[str, str]] = []
        self.remove_calls: list[str] = []
        # 1-based indexes of the delete attempts that found the late payload and
        # erased it, i.e. the deletes that ran after the transfer finished, and
        # when each of those ran.
        self.erased_landings: list[int] = []
        self.erase_times: list[float] = []
        self.copy_entered = threading.Event()
        self.copy_release = threading.Event()
        self.copy_landed = threading.Event()

    def temp_files(self) -> list[Path]:
        """Temps the abandoned transfer really landed in the remote dir."""
        return sorted(self._disk_root.glob("*.mrc-tmp-*"))

    def _disk_path(self, remote: str) -> Path:
        return self._disk_root / remote.rsplit("\\", 1)[-1]

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.copy_entered.set()
        # Abandoned round trip: the payload lands only when the cleanup's final
        # waiting slice releases it.
        self.copy_release.wait(timeout=10.0)
        shutil.copyfile(local, self._disk_path(remote))
        self.copy_landed.set()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        # The standalone delete script only: the promote's and the write's
        # catch blocks name Remove-Item too, and they are not cleanup.
        if (
            "Remove-Item" in script
            and "Move-Item" not in script
            and "WriteAllBytes" not in script
        ):
            self.remove_calls.append(script)
            match = re.search(r"Remove-Item -LiteralPath '([^']*)'", script)
            assert match is not None, "the delete script names no path"
            target = self._disk_path(match.group(1))
            existed = target.exists()
            target.unlink(missing_ok=True)
            if existed:
                self.erased_landings.append(len(self.remove_calls))
                self.erase_times.append(time.monotonic())
            if len(self.remove_calls) == self.release_after_removes:
                # This delete ends the sweep's periodic removals: the sweep only
                # waits after it, so the abandoned copy lands in that final wait,
                # after every delete issued before it.
                self.copy_release.set()
                self.copy_landed.wait(timeout=1.0)
            return ""
        return self._inner.execute_ps(script, environment=environment)


def test_winrm_put_cleanup_erases_a_temp_landed_in_the_final_wait(
    tmp_path: Path,
) -> None:
    """A temp landing in the sweep's final waiting slice is still erased.

    The abandoned copy is released by the last delete the sweep makes before it
    settles into waiting, so its payload reaches the host inside the sweep's own
    budget but after every delete the loop's remove-then-wait body would make:
    the next iteration only re-checks the budget and leaves. The cleanup owes
    that arrival a delete all the same - bounded by its own budget, never by the
    whole-op deadline that is already spent - and the prior destination must
    stay intact.
    """
    sess = FakePypsrpSession(has_copy=True)
    disk_root = tmp_path / "remote-temp"
    disk_root.mkdir()
    # Two sweep gaps of cleanup budget: the sweep deletes, waits a gap, deletes
    # again, and its second delete is the last one - the wait after it is the
    # slice that ends the budget.
    cleanup_budget = 2 * _CLEANUP_SWEEP_GAP_S
    store = _FinalSliceLandingCopySession(
        sess, disk_root=disk_root, release_after_removes=2
    )
    dest = rf"{TEMP}\final-slice-put.bin"
    sess.files[dest] = b"prior-good"
    op_budget = 0.15
    client = _client_under_the_transport_lock(store)
    backend = _SweepBoundaryRecordingWinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = cleanup_budget  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"late-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never started"
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - t0

    try:
        assert not worker.is_alive(), "put did not return"
        assert errors and isinstance(errors[0], FsError)
        assert errors[0].code == "TIMEOUT"
        assert store.copy_calls and ".mrc-tmp-" in store.copy_calls[0][1]
        assert store.copy_landed.is_set(), "the abandoned copy never landed"
        assert len(store.remove_calls) >= store.release_after_removes, (
            "the sweep never reached its final waiting slice"
        )
        assert sess.files.get(dest) == b"prior-good", "prior destination changed"
        assert store.erased_landings, (
            "every delete ran before the late payload landed, so the temp it "
            "landed in the sweep's final waiting slice survived the cleanup"
        )
        # The erase must come from the delete that closes the sweep, not from
        # one more retry inside the budget: an in-budget round would erase
        # before the sweep's own end, which is what leaves the next slice open.
        assert store.erase_times[-1] >= (
            backend.sweep_starts[0] + cleanup_budget
        ), "the erasing delete ran inside the sweep budget, not at its boundary"
        assert not store.temp_files(), (
            "a temp that landed inside the sweep budget survived the cleanup"
        )
        assert elapsed < (
            op_budget + cleanup_budget + _CLEANUP_MIN_BUDGET_S + 0.5
        ), f"elapsed={elapsed:.2f}s"
    finally:
        # Release the abandoned round trip so no helper thread outlives the test.
        store.copy_release.set()


class _SlowDeleteRoundTripSession:
    """FakePypsrpSession wrapper: the first cleanup delete eats the budget.

    The abandoned native copy lands its payload - a real ``shutil.copyfile``
    into a real directory - while the sweep's first delete is in flight, and
    that delete's round trip is then held open past the whole cleanup budget.
    The sweep reaches its budget with the delete still running, so only a delete
    issued after the budget was spent can still erase the landing. The delete
    script is a real ``os.unlink``: neither the landing nor the delete's result
    is modelled.
    """

    def __init__(self, inner: FakePypsrpSession, *, disk_root: Path) -> None:
        self._inner = inner
        self._disk_root = disk_root
        self.dirs = inner.dirs
        self.files = inner.files
        self.links = inner.links
        self.ps_calls = inner.ps_calls
        self.copy_calls: list[tuple[str, str]] = []
        self.remove_calls: list[str] = []
        # 1-based indexes of the deletes that found the landed payload and
        # erased it, and when each of those ran.
        self.erased_landings: list[int] = []
        self.erase_times: list[float] = []
        self.copy_entered = threading.Event()
        self.copy_release = threading.Event()
        self.copy_landed = threading.Event()
        self.copy_landed_at: float | None = None
        # Released by the test once the put has returned, so the delete's round
        # trip stays open for as long as the caller is still waiting.
        self.delete_release = threading.Event()

    def temp_files(self) -> list[Path]:
        """Temps the abandoned transfer really landed in the remote dir."""
        return sorted(self._disk_root.glob("*.mrc-tmp-*"))

    def _disk_path(self, remote: str) -> Path:
        return self._disk_root / remote.rsplit("\\", 1)[-1]

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.copy_entered.set()
        # Abandoned round trip: the payload lands only once the cleanup's first
        # delete releases it, inside the sweep's budget.
        self.copy_release.wait(timeout=10.0)
        shutil.copyfile(local, self._disk_path(remote))
        self.copy_landed_at = time.monotonic()
        self.copy_landed.set()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        # The standalone delete script only: the promote's and the write's
        # catch blocks name Remove-Item too, and they are not cleanup.
        if (
            "Remove-Item" in script
            and "Move-Item" not in script
            and "WriteAllBytes" not in script
        ):
            self.remove_calls.append(script)
            match = re.search(r"Remove-Item -LiteralPath '([^']*)'", script)
            assert match is not None, "the delete script names no path"
            target = self._disk_path(match.group(1))
            existed = target.exists()
            target.unlink(missing_ok=True)
            if existed:
                self.erased_landings.append(len(self.remove_calls))
                self.erase_times.append(time.monotonic())
            if len(self.remove_calls) == 1:
                # This delete's round trip is the one that spends the rest of
                # the sweep budget: the payload lands while it is in flight, and
                # the sweep only gets to act again once its budget is gone.
                self.copy_release.set()
                self.copy_landed.wait(timeout=5.0)
                self.delete_release.wait(timeout=10.0)
            return ""
        return self._inner.execute_ps(script, environment=environment)


def test_winrm_put_cleanup_closes_on_a_delete_that_spent_the_budget(
    tmp_path: Path,
) -> None:
    """The sweep's closing delete covers a landing its last delete outran.

    The abandoned copy lands inside the sweep's budget while the sweep's first
    delete is in flight, and that delete's round trip then outruns what is left
    of the budget: the loop must still close on a delete once the budget is
    spent, and that closing delete - never a retry inside the budget - is what
    erases the temp. The prior destination stays intact and the caller's wait
    never includes the abandoned round trip, which is still open when the put
    returns.
    """
    sess = FakePypsrpSession(has_copy=True)
    disk_root = tmp_path / "remote-temp"
    disk_root.mkdir()
    store = _SlowDeleteRoundTripSession(sess, disk_root=disk_root)
    dest = rf"{TEMP}\slow-delete-put.bin"
    sess.files[dest] = b"prior-good"
    op_budget = 0.15
    cleanup_budget = 3 * _CLEANUP_SWEEP_GAP_S
    # No transport lock here: this pins the sweep's own closing delete rather
    # than how a delete already in flight interacts with the serial ops gate.
    client = PypsrpFileClient(store, timeout_s=2.0)
    backend = _SweepBoundaryRecordingWinrmFs(
        client,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = cleanup_budget  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"late-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never started"
    worker.join(timeout=10.0)
    elapsed = time.monotonic() - t0

    try:
        assert not worker.is_alive(), "put did not return"
        assert errors and isinstance(errors[0], FsError)
        assert errors[0].code == "TIMEOUT"
        assert store.copy_calls and ".mrc-tmp-" in store.copy_calls[0][1]
        assert store.copy_landed.is_set(), "the abandoned copy never landed"
        assert store.copy_landed_at is not None
        assert store.copy_landed_at < backend.sweep_starts[0] + cleanup_budget, (
            "the payload did not land inside the sweep budget"
        )
        # The landing happened while the first delete was in flight, so no
        # delete inside the budget could have seen it: the erase has to come
        # from the delete issued once the budget was spent.
        assert len(store.remove_calls) >= 2, (
            "the sweep spent its budget without closing on a delete"
        )
        assert store.erased_landings, (
            "a temp that landed inside the sweep budget survived the cleanup"
        )
        assert store.erase_times[-1] >= (
            backend.sweep_starts[0] + cleanup_budget
        ), "the erasing delete ran inside the sweep budget, not at its boundary"
        assert sess.files.get(dest) == b"prior-good", "prior destination changed"
        assert not store.temp_files(), (
            "a temp that landed inside the sweep budget survived the cleanup"
        )
        assert elapsed < (
            op_budget + cleanup_budget + _CLEANUP_MIN_BUDGET_S + 0.5
        ), f"elapsed={elapsed:.2f}s"
    finally:
        # Release the round trip the sweep abandoned so no helper thread
        # outlives the test.
        store.delete_release.set()
        store.copy_release.set()


def test_winrm_get_native_fetch_hang_hits_op_budget(tmp_path: Path) -> None:
    """Hung native fetch through WinrmFs.get times out inside op_timeout_s."""
    import time

    store = _HangNativeCopyClient()
    store.files[rf"{TEMP}\hang-get.bin"] = b"remote"
    op_budget = 0.35
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    t0 = time.monotonic()
    with pytest.raises(FsError) as ei:
        backend.get(rf"{TEMP}\hang-get.bin", str(tmp_path / "out.bin"))
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < op_budget + 2.0
    store.release.set()


def test_winrm_write_junction_parent_stat_timeout_is_timeout() -> None:
    """Hung/timeout target-stat on a junction parent is TIMEOUT, not ALREADY_EXISTS."""
    store = MockWinrmFileClient()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"

    class _TimeoutTargetStat(MockWinrmFileClient):
        def __init__(self, inner: MockWinrmFileClient) -> None:
            super().__init__()
            self.files = inner.files
            self.dirs = inner.dirs
            self.links = inner.links

        def stat(self, path: str) -> object:
            norm = self._norm(path)
            if norm == self._norm(r"C:\var\www"):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().stat(path)

    wrapped = _TimeoutTargetStat(store)
    backend = WinrmFs(wrapped, cwd=HOME, home=HOME)
    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=r"C:\srv\www\app.conf",
        content="listen=80\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert r.code != "ALREADY_EXISTS"
    assert r"C:\srv\www\app.conf" not in store.files
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"


def test_winrm_mkdir_junction_parent_readlink_timeout_is_timeout() -> None:
    """readlink TIMEOUT on a junction parent is TIMEOUT, not ALREADY_EXISTS."""
    store = MockWinrmFileClient()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"

    class _TimeoutReadlink(MockWinrmFileClient):
        def __init__(self, inner: MockWinrmFileClient) -> None:
            super().__init__()
            self.files = inner.files
            self.dirs = inner.dirs
            self.links = inner.links

        def readlink(self, path: str) -> str:
            if self._norm(path) == self._norm(r"C:\srv\www"):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().readlink(path)

    wrapped = _TimeoutReadlink(store)
    backend = WinrmFs(wrapped, cwd=HOME, home=HOME)
    r = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=r"C:\srv\www\releases",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert r.code != "ALREADY_EXISTS"
    assert not store._exists_dir(r"C:\srv\www\releases")
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"


def test_winrm_rmtree_child_stat_timeout_is_timeout() -> None:
    """Child ``_stat`` TIMEOUT during fallback rmtree is TIMEOUT, not ok."""

    class _TimeoutChildStat:
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.rmdired: list[str] = []

        def listdir(self, path: str) -> list[str]:
            p = path.replace("/", "\\").rstrip("\\")
            if p == TEMP.rstrip("\\"):
                return ["keep.txt", "hang-child"]
            return []

        def stat(self, path: str) -> object:
            p = path.replace("/", "\\")
            if p.endswith("hang-child"):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            if p.endswith("keep.txt"):
                return type("A", (), {"kind": "file", "size": 1, "mtime": 1.0, "mode": "Archive"})()
            return type("A", (), {"kind": "dir", "size": 0, "mtime": 1.0, "mode": "Directory"})()

        def remove(self, path: str) -> None:
            self.removed.append(path)

        def rmdir(self, path: str) -> None:
            self.rmdired.append(path)

    client = _TimeoutChildStat()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.rm(TEMP, recursive=True)
    assert ei.value.code == "TIMEOUT"
    # Sibling removed before the hung child is fine (partial delete).
    assert any(c.endswith("keep.txt") for c in client.removed)
    # Root must not be reported removed after TIMEOUT.
    assert not any(
        c.replace("/", "\\").rstrip("\\") == TEMP.rstrip("\\") for c in client.rmdired
    )


def test_winrm_shallow_ops_unchanged_under_default_op_budget() -> None:
    """Normal shallow/single-RT ops succeed under default whole-op budget."""
    sess = FakePypsrpSession()
    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    assert backend._op_timeout_s == 60.0  # noqa: SLF001
    work = rf"{TEMP}\x14-shallow"
    backend.mkdir(work)
    note = rf"{work}\n.txt"
    backend.write(note, "ok\n")
    st = backend.stat(note)
    assert st.kind == "file"
    lst = backend.list(work)
    assert any(e.name == "n.txt" for e in lst.entries)
    backend.rm(note)
    backend.rm(work, recursive=True)


# ---------------------------------------------------------------------------
# list parent/child stat TIMEOUT is a whole-op failure (not stub .. / kind=o)
# write exist-stat TIMEOUT does not continue; resolve TIMEOUT keeps the link
# ---------------------------------------------------------------------------


def test_winrm_list_parent_stat_timeout_fails() -> None:
    """Hung parent ``_stat`` fails list; do not return ok with a stub ``..``."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\a.txt"] = b"x"

    class _TimeoutParent(MockWinrmFileClient):
        def __init__(self, inner: MockWinrmFileClient) -> None:
            super().__init__()
            self.files = inner.files
            self.dirs = inner.dirs
            self.links = inner.links

        def stat(self, path: str) -> object:
            if self._norm(path) == "C:\\":
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().stat(path)

    backend = WinrmFs(_TimeoutParent(store), cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.list(TEMP)
    assert ei.value.code == "TIMEOUT"


def test_winrm_list_child_stat_timeout_fails() -> None:
    """Hung per-child ``_stat`` fails list; do not return ok with kind=o."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\a.txt"] = b"x"

    class _TimeoutChild(MockWinrmFileClient):
        def __init__(self, inner: MockWinrmFileClient) -> None:
            super().__init__()
            self.files = inner.files
            self.dirs = inner.dirs
            self.links = inner.links

        def stat(self, path: str) -> object:
            if self._norm(path).endswith("\\a.txt"):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().stat(path)

    backend = WinrmFs(_TimeoutChild(store), cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.list(TEMP)
    assert ei.value.code == "TIMEOUT"


def test_winrm_list_child_stat_not_found_still_kind_o() -> None:
    """Non-timeout per-child stat miss still stubs kind=o (listing stays ok)."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\a.txt"] = b"x"

    class _VanishChild(MockWinrmFileClient):
        def __init__(self, inner: MockWinrmFileClient) -> None:
            super().__init__()
            self.files = inner.files
            self.dirs = inner.dirs
            self.links = inner.links

        def stat(self, path: str) -> object:
            if self._norm(path).endswith("\\a.txt"):
                raise FileNotFoundError(path)
            return super().stat(path)

    backend = WinrmFs(_VanishChild(store), cwd=HOME, home=HOME)
    result = backend.list(TEMP)
    by_name = {e.name: e for e in result.entries}
    assert "a.txt" in by_name
    assert by_name["a.txt"].kind == "o"


def test_winrm_write_exist_stat_timeout_does_not_continue() -> None:
    """First dest-stat TIMEOUT fails write; no created=True promote."""
    dest = rf"{TEMP}\exist.txt"

    class _TimeoutExistOnce(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self._hits = 0
            self.write_calls: list[str] = []

        def stat(self, path: str) -> object:
            if self._norm(path) == self._norm(dest):
                self._hits += 1
                if self._hits == 1:
                    raise FsError(
                        "TIMEOUT",
                        "winrm fs operation timed out after 0.2s",
                        details={"path": path, "timeout_s": 0.2},
                    )
            return super().stat(path)

        def write_file(self, path: str, data: bytes) -> None:
            self.write_calls.append(path)
            super().write_file(path, data)

    store = _TimeoutExistOnce()
    store.files[dest] = b"old-content"
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.write(dest, "new-must-not-land")
    assert ei.value.code == "TIMEOUT"
    assert store.files[dest] == b"old-content"
    assert store.write_calls == []
    assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_write_resolve_timeout_does_not_replace_symlink() -> None:
    """Dest is a reparse/symlink; resolve stat/readlink TIMEOUT keeps the link."""
    link = rf"{TEMP}\link.bin"
    target = rf"{TEMP}\target.bin"

    class _TimeoutReadlink(MockWinrmFileClient):
        def readlink(self, path: str) -> str:
            if self._norm(path) == self._norm(link):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().readlink(path)

    store = _TimeoutReadlink()
    store.files[target] = b"old"
    store.links[link] = target
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=link,
        content="new-must-not-land",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert link in store.links
    assert store.links[link] == target
    assert link not in store.files
    assert store.files[target] == b"old"


def test_winrm_put_resolve_timeout_does_not_replace_symlink(tmp_path: Path) -> None:
    """Dest is a reparse/symlink; put resolve TIMEOUT keeps the link."""
    link = rf"{TEMP}\link.bin"
    target = rf"{TEMP}\target.bin"

    class _TimeoutReadlink(MockWinrmFileClient):
        def readlink(self, path: str) -> str:
            if self._norm(path) == self._norm(link):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().readlink(path)

    store = _TimeoutReadlink()
    store.files[target] = b"old"
    store.links[link] = target
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-link")
    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "TIMEOUT"
    assert link in store.links
    assert store.links[link] == target
    assert link not in store.files
    assert store.files[target] == b"old"
