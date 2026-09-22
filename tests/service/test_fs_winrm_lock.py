"""Service tests: WinRM fs serial-ops scope and link-failure reporting.

FS calls share the pypsrp ``wsman`` object with exec, so without taking the
transport op lock they can interleave with an in-flight exec, and without
reporting link loss the transport keeps an endpoint "connected" that is
permanently unusable. These tests pin both behaviours and the no-op default.

The same lock also bounds the fs operation budget: a peer holding it (a long
exec / ps call) must not push the fs operation past its whole-op deadline, so
the wait for the lock is clamped to the remaining budget and reported as a
TIMEOUT before any session call.
"""

from __future__ import annotations

import builtins
import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pypsrp.exceptions
import pytest
import requests.exceptions
from _winrm_fakes import HOME, TEMP, FakePypsrpSession, _ErrorStreams

from mcp_remote_control.fs.backends.winrm import PypsrpFileClient, WinrmFs
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport.winrm import WinRMTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


class _RecordingSerialOps:
    """Re-entrant serial-ops callable recording how the scope was entered.

    Stands in for ``WinRMTransport.serial_ops``: an RLock plus a depth
    counter, so a nested acquisition is observable instead of deadlocking.
    """

    def __init__(self) -> None:
        self._rlock = threading.RLock()
        self.enters = 0
        self.depth = 0
        self.max_depth = 0
        self.nested_enters = 0

    def __call__(self) -> Any:
        return self._scope()

    @contextlib.contextmanager
    def _scope(self) -> Iterator[None]:
        if self.depth:
            self.nested_enters += 1
        with self._rlock:
            self.enters += 1
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            try:
                yield
            finally:
                self.depth -= 1


class _LockProbeSession(FakePypsrpSession):
    """Fake session recording the serial-ops depth observed per ``execute_ps``."""

    def __init__(self, ops: _RecordingSerialOps) -> None:
        super().__init__()
        self._ops = ops
        self.observed_depth: list[int] = []

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> str:
        self.observed_depth.append(self._ops.depth)
        return super().execute_ps(script, environment=environment)


class _RaisingSession:
    """Session whose every ``execute_ps`` raises a fresh exception."""

    def __init__(self, factory: Callable[[], BaseException]) -> None:
        self._factory = factory
        self.calls = 0

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> str:
        del script, environment
        self.calls += 1
        raise self._factory()


class _ErrorSession:
    """Session reporting a remote script failure (``had_errors`` True)."""

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, Any, bool]:
        del script, environment
        return "", _ErrorStreams(["boom"]), True


# ---------------------------------------------------------------------------
# Serial-ops scope
# ---------------------------------------------------------------------------


def test_public_ops_hold_serial_lock() -> None:
    """Every session call made by a public op runs while the lock is held."""
    ops = _RecordingSerialOps()
    session = _LockProbeSession(ops)
    client = PypsrpFileClient(session, timeout_s=0, serial_ops=ops)

    client.mkdir(TEMP + r"\locked")
    client.write_file(TEMP + r"\locked\a.bin", b"payload")
    readback = client.read_file(TEMP + r"\locked\a.bin")
    listed = client.listdir(TEMP + r"\locked")

    assert readback == b"payload"
    assert listed == ["a.bin"]
    assert session.observed_depth, "no session calls recorded"
    assert min(session.observed_depth) >= 1
    assert ops.max_depth >= 2, "public op -> _execute_ps must nest, not deadlock"
    assert ops.depth == 0, "scope leaked past the outermost op"


def test_scope_is_reentrant_across_nested_public_ops() -> None:
    """``write_file`` resolving a symlink chain re-enters stat without deadlock."""
    ops = _RecordingSerialOps()
    session = _LockProbeSession(ops)
    client = PypsrpFileClient(session, timeout_s=0, serial_ops=ops)

    # Missing path -> stat probe NOT_FOUND -> still promotes on the real path.
    client.write_file(TEMP + r"\fresh.bin", b"x")

    assert session.files[TEMP + r"\fresh.bin"] == b"x"
    assert ops.nested_enters >= 1
    assert ops.depth == 0


def test_absent_serial_ops_leaves_behaviour_unchanged() -> None:
    """Default ``None`` runs the same ops with the same results and no lock."""
    plain_session = FakePypsrpSession()
    plain = PypsrpFileClient(plain_session, timeout_s=0)
    assert plain._serial_ops is None
    _exercise(plain, plain_session)

    ops = _RecordingSerialOps()
    locked_session = FakePypsrpSession()
    locked = PypsrpFileClient(locked_session, timeout_s=0, serial_ops=ops)
    _exercise(locked, locked_session)

    assert plain_session.files == locked_session.files
    assert plain_session.dirs == locked_session.dirs
    assert ops.enters > 0


def _exercise(client: PypsrpFileClient, session: FakePypsrpSession) -> None:
    """Run one of every PS-backed public op against the fake filesystem."""
    root = TEMP + r"\exercise"
    client.mkdir(root)
    client.write_file(root + r"\f.bin", b"hello")
    assert client.read_file(root + r"\f.bin") == b"hello"
    assert client.read_file(root + r"\f.bin", max_bytes=3) == b"hel"
    assert client.listdir(root) == ["f.bin"]
    assert [d["name"] for d in client.list_with_attrs(root)] == ["f.bin"]
    assert client.stat(root + r"\f.bin")["kind"] == "file"
    client.remove(root + r"\f.bin")
    client.rmtree(root)
    assert session.files == {}


def test_native_copy_and_fetch_run_inside_serial_lock(
    tmp_path: Path,
) -> None:
    """Native transfer delegation takes the same lock as the oneshot path."""
    ops = _RecordingSerialOps()
    session = FakePypsrpSession(has_copy=True, has_fetch=True)
    client = PypsrpFileClient(session, timeout_s=0, serial_ops=ops)
    local = tmp_path / "payload.bin"
    local.write_bytes(b"native")

    client.copy(str(local), TEMP + r"\native.bin")
    assert session.copy_calls
    client.fetch(TEMP + r"\native.bin", str(local))

    assert ops.enters >= 2
    assert ops.depth == 0


# ---------------------------------------------------------------------------
# Link-failure reporting
# ---------------------------------------------------------------------------


def test_pypsrp_transport_error_reports_link_failure_once() -> None:
    reported: list[BaseException] = []
    exc = pypsrp.exceptions.WinRMTransportError("POST", 400, "Bad Request")
    session = _RaisingSession(lambda: exc)
    client = PypsrpFileClient(
        session, timeout_s=0, serial_ops=_RecordingSerialOps(), on_link_failure=reported.append
    )

    with pytest.raises(pypsrp.exceptions.WinRMTransportError):
        client.stat(TEMP + r"\stale")

    assert reported == [exc]


def test_requests_connection_error_reports_link_failure() -> None:
    reported: list[BaseException] = []
    session = _RaisingSession(lambda: requests.exceptions.ConnectionError("reset"))
    client = PypsrpFileClient(session, timeout_s=0, on_link_failure=reported.append)

    with pytest.raises(requests.exceptions.ConnectionError):
        client._execute_ps("Get-Item")

    assert len(reported) == 1


def test_requests_timeout_subclass_reports_link_failure() -> None:
    """ReadTimeout is a requests Timeout and must count as a link failure."""
    reported: list[BaseException] = []
    session = _RaisingSession(lambda: requests.exceptions.ReadTimeout("read timed out"))
    client = PypsrpFileClient(session, timeout_s=0, on_link_failure=reported.append)

    with pytest.raises(requests.exceptions.ReadTimeout):
        client.listdir(TEMP)

    assert len(reported) == 1


def test_nested_link_failure_reports_exactly_once() -> None:
    """One public op whose nested calls all fail reports a single link loss.

    A link failure on the first nested call (the symlink-resolve probe) now
    propagates immediately instead of being swallowed and retried by the next
    step: proceeding past it meant the op kept working on a link that had
    already been reported dead. The one-report contract is what this pins.
    """
    reported: list[BaseException] = []
    session = _RaisingSession(lambda: requests.exceptions.ConnectionError("reset"))
    client = PypsrpFileClient(session, timeout_s=0, on_link_failure=reported.append)

    with pytest.raises(requests.exceptions.ConnectionError):
        client.write_file(TEMP + r"\nested.bin", b"data")

    assert session.calls >= 1, "the public op must have reached the session"
    assert len(reported) == 1


def test_remote_fs_error_does_not_report_link_failure() -> None:
    reported: list[BaseException] = []
    client = PypsrpFileClient(
        _ErrorSession(), timeout_s=0, on_link_failure=reported.append
    )

    with pytest.raises(FsError) as info:
        client._execute_ps("throw")

    assert info.value.code == "FS_ERROR"
    assert reported == []


def test_missing_path_does_not_report_link_failure() -> None:
    reported: list[BaseException] = []
    session = FakePypsrpSession()
    client = PypsrpFileClient(session, timeout_s=0, on_link_failure=reported.append)

    with pytest.raises(FileNotFoundError):
        client.stat(TEMP + r"\absent")

    assert reported == []


def test_budget_timeout_does_not_report_link_failure() -> None:
    """A user-budget hang is a local timeout, not a link loss."""
    reported: list[BaseException] = []
    session = _RaisingSession(lambda: builtins.TimeoutError("bridge budget"))
    client = PypsrpFileClient(
        session, timeout_s=0, serial_ops=_RecordingSerialOps(), on_link_failure=reported.append
    )

    with pytest.raises(FsError) as info:
        client._execute_ps("Start-Sleep 60")

    assert info.value.code == "TIMEOUT"
    assert reported == []


def test_raising_callback_does_not_mask_the_link_failure() -> None:
    def _boom(exc: BaseException) -> None:
        raise RuntimeError(f"callback exploded on {type(exc).__name__}")

    session = _RaisingSession(lambda: requests.exceptions.ConnectionError("reset"))
    client = PypsrpFileClient(session, timeout_s=0, on_link_failure=_boom)

    with pytest.raises(requests.exceptions.ConnectionError):
        client.stat(TEMP + r"\stale")


def test_no_callback_leaves_link_failure_unhandled() -> None:
    """Without ``on_link_failure`` the original exception still propagates."""
    exc = pypsrp.exceptions.WSManFaultError("fault")
    session = _RaisingSession(lambda: exc)
    client = PypsrpFileClient(session, timeout_s=0)

    with pytest.raises(pypsrp.exceptions.WSManFaultError):
        client.readlink(TEMP + r"\stale")


# ---------------------------------------------------------------------------
# Lock wait counts against the fs operation budget
# ---------------------------------------------------------------------------


class _BlockingPsSession(FakePypsrpSession):
    """Fake session whose ``execute_ps`` blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        self.entered.set()
        self.release.wait(timeout=5.0)
        return super().execute_ps(script, environment=environment)


def _connected_transport(
    session: FakePypsrpSession | None = None,
) -> tuple[WinRMTransport, FakePypsrpSession]:
    """Real transport whose connector returns *session* (no network sockets)."""
    sess = session if session is not None else FakePypsrpSession()
    sess.files[rf"{TEMP}\held.txt"] = b"ok"
    # Probe seeds the transport reads back after connect.
    sess.cwd = HOME
    sess.home = HOME
    sess.os = "windows"
    sess.shell = "powershell"
    sess.ps_version = "5.1.19041"
    sess.closed = False

    def connector(**_kwargs: object) -> object:
        return sess

    transport = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=connector,
    )
    transport.connect()
    return transport, sess


class _LockHold:
    """Peer thread that keeps the transport serial lock until released.

    The release is an event barrier, not a sleep: the lock stays held until
    the test sets ``release``, so an operation that returns earlier provably
    did so while the lock was still held. ``max_hold_s`` only bounds the
    hold so a regression cannot hang the suite.
    """

    def __init__(self, transport: WinRMTransport, *, max_hold_s: float = 5.0) -> None:
        self._transport = transport
        self._max_hold_s = max_hold_s
        self.held = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with self._transport.serial_ops():
            self.held.set()
            self.release.wait(timeout=self._max_hold_s)
        self.done.set()

    def __enter__(self) -> _LockHold:
        self._thread.start()
        assert self.held.wait(timeout=5.0), "peer never took the serial lock"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._thread.join(timeout=5.0)


def _assert_timed_out_without_session_call(
    excinfo: pytest.ExceptionInfo[FsError],
    *,
    elapsed: float,
    budget: float,
    session: FakePypsrpSession,
) -> None:
    """TIMEOUT at the budget, with no remote call started under the lock."""
    assert excinfo.value.code == "TIMEOUT"
    assert "timed out" in excinfo.value.msg.lower()
    assert excinfo.value.details.get("op_timeout_s") == budget
    assert elapsed < budget + 0.25, f"TIMEOUT overshot the budget: {elapsed:.3f}s"
    assert elapsed >= budget * 0.5, f"TIMEOUT fired before the budget: {elapsed:.3f}s"
    assert session.ps_calls == [], "no session call may start under a held lock"


def test_fs_lock_wait_counts_against_op_budget() -> None:
    """An existing client reports TIMEOUT at the budget, not at lock release."""
    budget = 0.05
    transport, sess = _connected_transport()
    backend = WinrmFs(
        transport.open_fs(), cwd=HOME, home=HOME, op_timeout_s=budget
    )

    with _LockHold(transport) as hold:
        t0 = time.monotonic()
        with pytest.raises(FsError) as ei:
            backend.stat(rf"{TEMP}\held.txt")
        elapsed = time.monotonic() - t0
        # Returned while the peer still held the lock: the budget, not the
        # release, ended the wait.
        assert not hold.done.is_set(), "waited for the lock to be released"

    _assert_timed_out_without_session_call(
        ei, elapsed=elapsed, budget=budget, session=sess
    )


def test_fs_lazy_open_lock_wait_counts_against_op_budget() -> None:
    """The lazy open reports TIMEOUT at the budget, not at lock release."""
    budget = 0.05
    transport, sess = _connected_transport()
    backend = WinrmFs(
        factory=transport.open_fs, cwd=HOME, home=HOME, op_timeout_s=budget
    )

    with _LockHold(transport) as hold:
        t0 = time.monotonic()
        with pytest.raises(FsError) as ei:
            backend.stat(rf"{TEMP}\held.txt")
        elapsed = time.monotonic() - t0
        assert not hold.done.is_set(), "waited for the lock to be released"

    _assert_timed_out_without_session_call(
        ei, elapsed=elapsed, budget=budget, session=sess
    )


def test_fs_after_lock_release_succeeds_with_nested_scopes() -> None:
    """Once the lock is free the lazy open and nested scopes work again."""
    budget = 0.05
    transport, sess = _connected_transport()
    backend = WinrmFs(
        factory=transport.open_fs, cwd=HOME, home=HOME, op_timeout_s=budget
    )

    with _LockHold(transport):
        with pytest.raises(FsError) as ei:
            backend.stat(rf"{TEMP}\held.txt")
        assert ei.value.code == "TIMEOUT"
        assert sess.ps_calls == []

    info = backend.stat(rf"{TEMP}\held.txt")
    assert info.kind == "file"
    assert sess.ps_calls, "the session must be reached after the lock is free"
    assert backend._client is not None  # noqa: SLF001 - lazy open cached

    # Nested scopes (write -> resolve probe -> execute_ps) still re-enter the
    # lock instead of waiting on themselves.
    target = rf"{TEMP}\after-release.txt"
    backend.write(target, "body\n")
    assert sess.files[target] == b"body\n"
    assert sess.files[rf"{TEMP}\held.txt"] == b"ok"


def test_fs_call_holds_transport_lock_against_a_peer() -> None:
    """A fs session call still serializes exec / ps through the op lock."""
    sess = _BlockingPsSession()
    transport, _sess = _connected_transport(sess)
    client = transport.open_fs()
    result: list[Any] = []

    def _stat() -> None:
        result.append(client.stat(rf"{TEMP}\held.txt"))

    worker = threading.Thread(target=_stat, daemon=True)
    worker.start()
    assert sess.entered.wait(timeout=5.0), "session call never started"
    peer_got_lock = False
    try:
        peer_got_lock = transport.op_lock.acquire(blocking=False)
        assert peer_got_lock is False, "fs call must hold the transport lock"
    finally:
        if peer_got_lock:  # pragma: no cover - the invariant above fails
            transport.op_lock.release()
        sess.release.set()
        worker.join(timeout=5.0)
    assert result and result[0]["kind"] == "file"
    # Released with the call: a peer can take the lock again.
    assert transport.op_lock.acquire(blocking=False) is True
    transport.op_lock.release()
