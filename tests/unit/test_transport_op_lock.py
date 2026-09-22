"""Transport-level _op_lock serialization tests.

Covers concurrent run_command / mark_dead / open_fs / runspace /
collect_probe serialization, serial_ops re-entrancy, and WinRM
collect_probe holding op_lock across remote RTT.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

import pytest

from mcp_remote_control.endpoint.registry import (
    Endpoint,
    reset_registry,
)
from mcp_remote_control.serial.registry import reset_serial_registry
from mcp_remote_control.transport.base import (
    BaseTransport,
    ExecResult,
    TransportError,
)


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    reset_registry()
    reset_serial_registry()
    yield
    reset_registry()
    reset_serial_registry()


# ---------------------------------------------------------------------------
# Transport-level per-endpoint serial lock (exec/sftp/mark_dead)
# ---------------------------------------------------------------------------


class _SerialProbeTransport(BaseTransport):
    """Transport that fails with NOT_CONNECTED if two ops overlap without lock.

    The auto-wrapped ``run_command`` / ``mark_dead`` on BaseTransport subclasses
    must keep ``_depth`` at most 1. Without the op lock, concurrent calls
    would observe depth > 1 and raise intermittent NOT_CONNECTED.
    """

    name = "serial-probe"

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._depth_lock = threading.Lock()
        self._max_depth = 0
        self.run_count = 0
        self.mark_count = 0

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del command, cwd, timeout_s, env
        with self._depth_lock:
            self._depth += 1
            self._max_depth = max(self._max_depth, self._depth)
            depth = self._depth
            self.run_count += 1
        if depth > 1:
            # Concurrent entry - the op lock failed to serialize.
            self._connected = False
            with self._depth_lock:
                self._depth -= 1
            raise TransportError(
                "NOT_CONNECTED",
                "concurrent run_command without op_lock",
            )
        try:
            # Hold the critical section long enough for a peer thread to race.
            time.sleep(0.02)
            if not self._connected:
                raise TransportError("NOT_CONNECTED", "transport marked dead")
            return ExecResult(exit_code=0, stdout="ok\n", cwd="/")
        finally:
            with self._depth_lock:
                self._depth -= 1

    def mark_dead(self, reason: str | None = None) -> None:
        with self._depth_lock:
            self._depth += 1
            self._max_depth = max(self._max_depth, self._depth)
            depth = self._depth
            self.mark_count += 1
        if depth > 1:
            # Overlap with run_command / another mark_dead - lock failed.
            with self._depth_lock:
                self._depth -= 1
            raise RuntimeError(
                f"concurrent mark_dead (depth={depth}) reason={reason!r}"
            )
        try:
            time.sleep(0.01)
            self._connected = False
            if reason:
                self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}
        finally:
            with self._depth_lock:
                self._depth -= 1


def test_transport_op_lock_serializes_concurrent_run_command() -> None:
    """Two threads calling run_command never interleave.

    Without the per-transport op lock, ``_SerialProbeTransport`` would see
    depth > 1 and raise intermittent NOT_CONNECTED.
    """
    t = _SerialProbeTransport()
    t.connect()
    assert t.op_lock is t._op_lock

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    N = 8
    PER = 6

    def worker() -> None:
        local_errs: list[BaseException] = []
        for _ in range(PER):
            try:
                t.run_command("echo race")
            except BaseException as exc:  # noqa: BLE001
                local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"concurrent run_command raised: {errors[:3]}"
    assert t._max_depth == 1, (
        f"op_lock failed to serialize: max concurrent depth={t._max_depth}"
    )
    assert t.run_count == N * PER
    assert t.is_connected() is True


def test_transport_op_lock_serializes_run_command_and_mark_dead() -> None:
    """run_command and mark_dead are mutually exclusive on one transport."""
    t = _SerialProbeTransport()
    t.connect()

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def do_run() -> None:
        try:
            barrier.wait()
            for _ in range(10):
                if t.is_connected():
                    try:
                        t.run_command("echo x")
                    except TransportError as exc:
                        # mark_dead may win between is_connected and run -
                        # only NOT_CONNECTED after a deliberate mark is ok;
                        # concurrent-depth NOT_CONNECTED is not.
                        if "without op_lock" in exc.msg:
                            raise
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_mark() -> None:
        try:
            barrier.wait()
            for i in range(10):
                t.mark_dead(f"race-{i}")
                # Flip connected back so run_command can proceed again.
                t._connected = True
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_run = threading.Thread(target=do_run)
    t_mark = threading.Thread(target=do_mark)
    t_run.start()
    t_mark.start()
    t_run.join()
    t_mark.join()

    assert not errors, f"run/mark race raised: {errors[:3]}"
    assert t._max_depth == 1, (
        f"run_command and mark_dead overlapped: max_depth={t._max_depth}"
    )
    assert t.mark_count == 10


def test_endpoint_op_lock_delegates_to_transport() -> None:
    """Endpoint.op_lock exposes the transport serial lock."""
    t = _SerialProbeTransport()
    t.connect()
    ep = Endpoint(
        name="probe",
        transport_name="serial-probe",
        caps={"exec": True},
        connected=True,
        transport=t,
    )
    assert ep.op_lock is t.op_lock
    ep2 = Endpoint(name="empty", transport_name="local", caps={})
    assert ep2.op_lock is None


def test_serial_ops_context_reentrant_with_run_command() -> None:
    """serial_ops() is re-entrant with wrapped run_command (RLock)."""
    t = _SerialProbeTransport()
    t.connect()
    with t.serial_ops():
        # Nested: serial_ops holds lock; run_command wrapper re-enters.
        result = t.run_command("nested")
    assert result.exit_code == 0  # type: ignore[union-attr]
    assert t._max_depth == 1


# ---------------------------------------------------------------------------
# open_fs / open_runspace / runspace_invoke / collect_probe must serialize
# with mark_dead/connect/close.
# ---------------------------------------------------------------------------


class _SerialExtendedTransport(BaseTransport):
    """Depth-tracking transport for serial-op expansion acceptance.

    Implements the four methods added to ``_SERIAL_OP_METHODS`` plus
    ``mark_dead``. Without auto-wrap, concurrent entry would see depth > 1.
    """

    name = "serial-extended"

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._depth_lock = threading.Lock()
        self._max_depth = 0
        self.op_counts: dict[str, int] = {
            "open_fs": 0,
            "open_runspace": 0,
            "runspace_invoke": 0,
            "collect_probe": 0,
            "mark_dead": 0,
            "connect": 0,
            "close": 0,
        }

    def _enter(self, name: str) -> int:
        with self._depth_lock:
            self._depth += 1
            self._max_depth = max(self._max_depth, self._depth)
            depth = self._depth
            self.op_counts[name] = self.op_counts.get(name, 0) + 1
        return depth

    def _leave(self) -> None:
        with self._depth_lock:
            self._depth -= 1

    def _hold(self, name: str, hold_s: float = 0.02) -> None:
        depth = self._enter(name)
        if depth > 1:
            self._leave()
            raise RuntimeError(
                f"concurrent {name} without op_lock (depth={depth})"
            )
        try:
            time.sleep(hold_s)
            if name == "mark_dead":
                self._connected = False
            elif name == "connect":
                self._connected = True
            elif name == "close":
                self._connected = False
        finally:
            self._leave()

    def connect(self) -> None:
        self._hold("connect", hold_s=0.005)

    def close(self) -> None:
        self._hold("close", hold_s=0.005)

    def mark_dead(self, reason: str | None = None) -> None:
        del reason
        self._hold("mark_dead", hold_s=0.015)

    def open_fs(self) -> object:
        self._hold("open_fs")
        return object()

    def open_runspace(self) -> object:
        self._hold("open_runspace")
        return object()

    def runspace_invoke(
        self,
        handle: object,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, object]:
        del handle, script, timeout_s
        self._hold("runspace_invoke")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def collect_probe(self) -> dict[str, object]:
        # Mimic WinRM unseeded path: remote RTT under the serial wrap.
        self._hold("collect_probe", hold_s=0.03)
        return {"status": "ok", "os": "windows"}


def test_serial_op_methods_include_q9_gaps() -> None:
    """_SERIAL_OP_METHODS covers open_fs/runspace/collect_probe."""
    from mcp_remote_control.transport.base import _SERIAL_OP_METHODS

    for name in (
        "open_fs",
        "open_runspace",
        "runspace_invoke",
        "collect_probe",
    ):
        assert name in _SERIAL_OP_METHODS, f"{name} missing from serial set"
        fn = getattr(_SerialExtendedTransport, name)
        assert getattr(fn, "_mrc_op_serial", False), (
            f"{name} not auto-wrapped with op_lock"
        )


def test_transport_op_lock_serializes_open_fs_and_mark_dead() -> None:
    """open_fs and mark_dead are mutually exclusive on one transport."""
    t = _SerialExtendedTransport()
    t.connect()

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def do_open_fs() -> None:
        try:
            barrier.wait()
            for _ in range(12):
                t.open_fs()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_mark() -> None:
        try:
            barrier.wait()
            for i in range(12):
                t.mark_dead(f"fs-race-{i}")
                t._connected = True
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    th_fs = threading.Thread(target=do_open_fs)
    th_mark = threading.Thread(target=do_mark)
    th_fs.start()
    th_mark.start()
    th_fs.join()
    th_mark.join()

    assert not errors, f"open_fs/mark_dead race: {errors[:3]}"
    assert t._max_depth == 1, (
        f"open_fs and mark_dead overlapped: max_depth={t._max_depth}"
    )
    assert t.op_counts["open_fs"] == 12
    assert t.op_counts["mark_dead"] == 12


def test_transport_op_lock_serializes_runspace_ops_and_mark_dead() -> None:
    """open_runspace / runspace_invoke mutually exclusive with mark_dead."""
    t = _SerialExtendedTransport()
    t.connect()

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5.0)

    def do_open_rs() -> None:
        try:
            barrier.wait()
            for _ in range(8):
                t.open_runspace()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_invoke() -> None:
        try:
            barrier.wait()
            handle = object()
            for _ in range(8):
                t.runspace_invoke(handle, "Write-Output 1")
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_mark() -> None:
        try:
            barrier.wait()
            for i in range(8):
                t.mark_dead(f"rs-race-{i}")
                t._connected = True
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=do_open_rs),
        threading.Thread(target=do_invoke),
        threading.Thread(target=do_mark),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"runspace/mark_dead race: {errors[:3]}"
    assert t._max_depth == 1, (
        f"runspace ops and mark_dead overlapped: max_depth={t._max_depth}"
    )
    assert t.op_counts["open_runspace"] == 8
    assert t.op_counts["runspace_invoke"] == 8
    assert t.op_counts["mark_dead"] == 8


def test_transport_op_lock_serializes_collect_probe_and_mark_dead() -> None:
    """collect_probe and mark_dead never interleave (WinRM RTT path)."""
    t = _SerialExtendedTransport()
    t.connect()

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def do_probe() -> None:
        try:
            barrier.wait()
            for _ in range(10):
                out = t.collect_probe()
                assert out.get("status") == "ok"
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_mark() -> None:
        try:
            barrier.wait()
            for i in range(10):
                t.mark_dead(f"probe-race-{i}")
                t._connected = True
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    th_p = threading.Thread(target=do_probe)
    th_m = threading.Thread(target=do_mark)
    th_p.start()
    th_m.start()
    th_p.join()
    th_m.join()

    assert not errors, f"collect_probe/mark_dead race: {errors[:3]}"
    assert t._max_depth == 1, (
        f"collect_probe and mark_dead overlapped: max_depth={t._max_depth}"
    )
    assert t.op_counts["collect_probe"] == 10
    assert t.op_counts["mark_dead"] == 10


def test_transport_op_lock_serializes_collect_probe_and_close() -> None:
    """collect_probe is mutually exclusive with close on one transport."""
    t = _SerialExtendedTransport()
    t.connect()

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def do_probe() -> None:
        try:
            barrier.wait()
            for _ in range(8):
                t.collect_probe()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def do_close() -> None:
        try:
            barrier.wait()
            for _ in range(8):
                t.close()
                t._connected = True
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    th_p = threading.Thread(target=do_probe)
    th_c = threading.Thread(target=do_close)
    th_p.start()
    th_c.start()
    th_p.join()
    th_c.join()

    assert not errors, f"collect_probe/close race: {errors[:3]}"
    assert t._max_depth == 1, (
        f"collect_probe and close overlapped: max_depth={t._max_depth}"
    )


def test_winrm_collect_probe_holds_op_lock_during_remote_rtt() -> None:
    """WinRM collect_probe holds op_lock while session RTT runs.

    A peer thread must not enter mark_dead until the remote oneshot returns;
    otherwise unseeded probe could use a disposed session.
    """
    from mcp_remote_control.transport.winrm import WinRMTransport

    started = threading.Event()
    release = threading.Event()
    seen_depth: list[int] = []
    depth_lock = threading.Lock()
    active = {"n": 0}

    class _SlowPsSession:
        """execute_ps blocks until release; tracks concurrent active count."""

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del script, environment
            with depth_lock:
                active["n"] += 1
                seen_depth.append(active["n"])
            started.set()
            # Hold long enough for mark_dead thread to contend on op_lock.
            assert release.wait(timeout=5.0), "release never signaled"
            with depth_lock:
                active["n"] -= 1
            payload = (
                '{"ps_version":"5.1","language_mode":"FullLanguage",'
                '"os_version":"10.0","has_convertto_json":true,'
                '"can_get_item":true,"can_file_io":true}'
            )
            return (payload + "\n", None, False)

    sess = _SlowPsSession()
    t = WinRMTransport(
        host="10.0.0.9",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    assert getattr(type(t).collect_probe, "_mrc_op_serial", False)
    assert getattr(type(t).mark_dead, "_mrc_op_serial", False)

    errors: list[BaseException] = []
    mark_entered = threading.Event()
    probe_done = threading.Event()

    def do_probe() -> None:
        try:
            out = t.collect_probe()
            assert out.get("status") in ("ok", "partial", "fail")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            probe_done.set()

    def do_mark() -> None:
        try:
            assert started.wait(timeout=5.0)
            # While probe holds op_lock + remote RTT, mark_dead must block.
            # Try non-blocking first: should fail while probe is inside.
            acquired = t.op_lock.acquire(blocking=False)
            if acquired:
                t.op_lock.release()
                errors.append(
                    RuntimeError(
                        "op_lock free during collect_probe remote RTT \u2014 "
                        "collect_probe bypassed serial wrap"
                    )
                )
                release.set()
                return
            release.set()
            t.mark_dead("after-probe-rtt")
            mark_entered.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            release.set()

    th_p = threading.Thread(target=do_probe)
    th_m = threading.Thread(target=do_mark)
    th_p.start()
    th_m.start()
    th_p.join(timeout=10.0)
    th_m.join(timeout=10.0)

    assert not errors, f"WinRM probe lock proof failed: {errors[:3]}"
    assert probe_done.is_set()
    assert mark_entered.is_set()
    assert t.is_connected() is False
    # execute_ps never saw nested concurrent use of the session path.
    assert max(seen_depth or [0]) == 1


def test_winrm_close_runspace_holds_op_lock_during_teardown() -> None:
    """The runspace Delete and its recovery run under the transport op_lock.

    A teardown that ran outside the lock could overlap an exec / probe /
    invoke on the same endpoint - and a refused delete's replay would overlap
    it in the same way. The close must hold the lock until the (possibly
    replayed) delete is done.
    """
    from mcp_remote_control.transport.winrm import RunspaceResult, WinRMTransport

    close_entered = threading.Event()
    close_release = threading.Event()

    class _BlockingCloseHandle:
        location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            return RunspaceResult(stdout=script, exit_code=0, location=self.location)

        def close(self) -> None:
            close_entered.set()
            assert close_release.wait(timeout=5.0), "the close was never released"

    class _Sess:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def close(self) -> None:
            return None

        def open_runspace(self) -> _BlockingCloseHandle:
            return _BlockingCloseHandle()

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del script, environment
            return "ok", None, False

        def execute_cmd(
            self,
            command: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, str, int]:
            del command, environment
            return "ok", "", 0

    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: _Sess(),
    )
    t.connect()
    handle = t.open_runspace()
    errors: list[BaseException] = []

    def do_close() -> None:
        try:
            t.close_runspace(handle)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=do_close)
    th.start()
    try:
        assert close_entered.wait(timeout=5.0), "close never entered the handle"
        # While the delete is in flight the serial lock must be held, so a
        # peer op on this endpoint waits instead of interleaving with it.
        acquired = t.op_lock.acquire(blocking=False)
        if acquired:
            t.op_lock.release()
            errors.append(
                RuntimeError(
                    "op_lock free while the runspace delete was in flight \u2014 "
                    "close_runspace ran outside the serial lock"
                )
            )
    finally:
        close_release.set()
        th.join(timeout=10.0)

    assert not errors, errors
    assert not th.is_alive()


# ---------------------------------------------------------------------------
# Serial-zone hooks: both ends of a zone run them, and a failing hook never
# reaches the operation that entered the zone.
# ---------------------------------------------------------------------------


class _RaisingTransport(BaseTransport):
    """Serial op that fails, to observe a zone that ends with an exception."""

    name = "raising-probe"

    def connect(self) -> None:
        self._connected = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del command, cwd, timeout_s, env
        raise TransportError("EXEC_FAILED", "the operation failed")


def test_serial_zone_hooks_run_at_both_ends_of_a_zone() -> None:
    """A wrapped serial op and serial_ops() both run the hooks at both ends.

    The zone is the transport's scheduling point for work that outlives the
    operation which created it: entering hands that work off, and ending - with
    the lock the operation held about to be free - retries a hand-off whose own
    bounded wait for that lock expired. So every zone, whether a wrapped serial
    op or an explicit ``serial_ops`` critical section, runs the registered hooks
    at its start and again at its end, and a hook that was discarded stops
    being run.
    """
    t = _SerialProbeTransport()
    t.connect()
    ran: list[str] = []

    def _first() -> None:
        ran.append("first")

    def _second() -> None:
        ran.append("second")

    t.serial_zone_hooks.add(_first)

    with t.serial_ops():
        assert ran == ["first"], "serial_ops() entered the zone without running the hook"
    assert ran == ["first", "first"], "serial_ops() left the zone without running the hook"

    # The op's own work is observable between the two runs of the hook: the end
    # run happens after the body - the op has already run once by then - and
    # the entry run happens before it.
    body_progress: list[int] = []
    t.serial_zone_hooks.add(lambda: body_progress.append(t.run_count))
    assert t.run_command("x").exit_code == 0  # type: ignore[union-attr]
    assert body_progress == [0, 1], (
        "the end-of-zone hook did not run after the wrapped op's own work"
    )
    assert ran == ["first"] * 4, (
        "a wrapped serial op did not run the hook at both ends of its zone"
    )

    # Registering is idempotent, and every registered hook runs at both ends.
    t.serial_zone_hooks.add(_first)
    t.serial_zone_hooks.add(_second)
    ran.clear()
    with t.serial_ops():
        pass
    assert ran == ["first", "second", "first", "second"], (
        "the hooks did not run once each at both ends of the zone"
    )

    t.serial_zone_hooks.discard(_first)
    ran.clear()
    with t.serial_ops():
        pass
    assert ran == ["second", "second"], "a discarded hook was still run"


def test_serial_zone_hooks_run_when_a_serial_op_raises() -> None:
    """A zone that ends on a failure still runs its end-of-zone hooks.

    The end run is the retry of work whose bounded hand-off expired while this
    operation held the lock, so it happens whether the operation returned or
    raised: the zone ended either way, and the lock is about to be free either
    way. The operation's own failure still reaches its caller unchanged.
    """
    t = _RaisingTransport()
    t.connect()
    ran: list[str] = []
    t.serial_zone_hooks.add(lambda: ran.append("hook"))

    with pytest.raises(TransportError, match="the operation failed"):
        t.run_command("boom")

    assert ran == ["hook", "hook"], (
        "the zone that ended on a failure did not run its end-of-zone hooks"
    )


def test_a_serial_zone_hook_that_discards_itself_is_honoured() -> None:
    """A hook may take its own registration back while the zone runs.

    A drain that owes nothing any more stops being asked from inside the zone
    that entered it, so the registry is snapshotted for each pass: a hook that
    discards itself during a run neither cuts that pass short nor comes back
    for the other end of the zone, and the hooks after it still run.
    """
    t = _SerialProbeTransport()
    t.connect()
    ran: list[str] = []

    def _drain_once() -> None:
        ran.append("drain")
        t.serial_zone_hooks.discard(_drain_once)

    def _after() -> None:
        ran.append("after")

    t.serial_zone_hooks.add(_drain_once)
    t.serial_zone_hooks.add(_after)

    with t.serial_ops():
        pass

    assert ran == ["drain", "after", "after"], (
        "a hook that discarded itself changed what the rest of the zone ran"
    )


def test_a_raising_serial_zone_hook_never_breaks_the_zone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hook's failure is reported, never raised into the entering operation.

    A hook runs inside the serial zone of the operation that entered it - an
    exec or an fs call - at both ends of that zone, so a hook that raises must
    not turn that operation into an error at either end, and it must not stop
    the hooks registered after it either: the zone continues and the failure is
    reported.
    """
    t = _SerialProbeTransport()
    t.connect()

    def _boom() -> None:
        raise RuntimeError("drain failed")

    ran: list[str] = []
    t.serial_zone_hooks.add(_boom)
    t.serial_zone_hooks.add(lambda: ran.append("after"))

    with caplog.at_level(
        logging.WARNING, logger="mcp_remote_control.transport.base"
    ):
        result = t.run_command("still runs")
        assert result.exit_code == 0  # type: ignore[union-attr]
        assert ran == ["after", "after"], (
            "a failing hook stopped the ones after it at one end of the zone"
        )
        with t.serial_ops():
            pass
        assert ran == ["after"] * 4, (
            "a failing hook stopped the ones after it at one end of the zone"
        )

    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ] == ["serial zone hook failed"] * 4, caplog.records

