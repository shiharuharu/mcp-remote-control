"""Service tests: WinRM runspace adapters, invoke, hang/open-close."""

from __future__ import annotations

import base64
import gc
import json
import logging
import struct
import threading
import time
import uuid
import weakref
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from pypsrp.messages import Message, MessageType, PipelineState
from pypsrp.powershell import Fragment, PSInvocationState, RunspacePool, RunspacePoolState
from pypsrp.shell import SignalCode

from _winrm_fakes import _ErrorStreams

from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult, SerialZoneHooks
from mcp_remote_control.transport.winrm import (
    InvokeRunspaceAdapter,
    PypsrpPoolRunspaceAdapter,
    RunspaceResult,
    WinRMTransport,
    _EXIT_MARKER,
    _LOCATION_MARKER,
    _adapt_runspace_handle,
    _format_ps_errors,
    _format_ps_output,
)

try:  # pypsrp is a hard dependency; the fallback keeps the shape testable.
    from pypsrp import exceptions as pypsrp_exceptions
except ImportError:  # pragma: no cover - exercised only without pypsrp
    pypsrp_exceptions = None  # type: ignore[assignment]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def test_winrm_open_runspace_pool_open_fail_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RunspacePool.open() fails, best-effort pool.close()
    is attempted before raising EXEC_FAILED (no half-open runspace leak)."""

    class _FailPool:
        def __init__(self, wsman: object) -> None:
            self.wsman = wsman
            self.closed = False
            constructed.append(self)

        def open(self) -> None:
            raise RuntimeError("open handshake failed")

        def close(self) -> None:
            self.closed = True

    constructed: list[_FailPool] = []

    monkeypatch.setattr("pypsrp.powershell.RunspacePool", _FailPool)

    class _WsmanSession:
        wsman = object()

        def close(self) -> None:  # pragma: no cover - not reached
            pass

    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: _WsmanSession(),
    )
    t.connect()

    with pytest.raises(TransportError) as ei:
        t.open_runspace()
    assert ei.value.code == "EXEC_FAILED"
    assert constructed, "RunspacePool was constructed"
    assert constructed[0].closed is True, "pool.close() was attempted on open() failure"


# ---------------------------------------------------------------------------
# Production-path coverage: runspace_invoke _format_ps_errors (with a
# non-empty error stream), _format_ps_output edge cases, runspace adapter
# wrap for pool vs invoke handles, and run_argv's execute_cmd
# fallback when execute_ps is absent.
# ---------------------------------------------------------------------------

def test_winrm_format_ps_output_edge_cases() -> None:
    """_format_ps_output (runspace_invoke stdout formatter) handles
    None / empty / str / list / tuple / non-str shapes without losing a
    trailing newline."""
    assert _format_ps_output(None) == ""
    assert _format_ps_output("") == ""
    assert _format_ps_output("abc") == "abc\n"
    assert _format_ps_output("abc\n") == "abc\n"
    assert _format_ps_output(["a", "b"]) == "a\nb\n"
    assert _format_ps_output(("x", "y")) == "x\ny\n"
    # Non-string element is str()'d.
    assert _format_ps_output([42]) == "42\n"
    assert _format_ps_output(42) == "42\n"


def test_winrm_format_ps_errors_streams() -> None:
    """_format_ps_errors (runspace_invoke stderr formatter) joins the
    error stream, str()'s non-string items, and returns "" for empty / None."""

    class _Streams:
        def __init__(self, errs: list[object] | None) -> None:
            self.error = errs

    class _Ps:
        def __init__(self, errs: list[object] | None) -> None:
            self.streams = _Streams(errs)

    class _PsNoStreams:
        streams = None

    class _PsNoneError:
        class streams:
            error = None

    assert _format_ps_errors(_Ps([])) == ""
    assert _format_ps_errors(_Ps(["e1", "e2"])) == "e1\ne2"
    # Non-string items are stringified (pypsrp error records are objects).
    assert _format_ps_errors(_Ps([42, "x"])) == "42\nx"
    assert _format_ps_errors(_PsNoStreams()) == ""
    assert _format_ps_errors(_PsNoneError()) == ""


class _ErrPowerShell:
    """Fake pypsrp PowerShell for the runspace_invoke error-formatting path.

    Class-level ``errors`` / ``had_errors`` config (set via
    ``monkeypatch.setattr`` before the invoke) so the transport's
    ``PowerShell(handle)`` construction picks up the test's error stream without
    changing the constructor signature. ``invoke()`` returns a marker-tagged
    location so the folded location probe is parsed (no second round-trip);
    the marker is stripped from stdout by ``_split_location_output``.
    """

    errors: list[object] | None = None
    had_errors: bool = False

    def __init__(self, pool: object) -> None:
        self.pool = pool
        self.script: str | None = None
        self.streams = _ErrorStreams(list(_ErrPowerShell.errors or []))
        self.had_errors = _ErrPowerShell.had_errors
        self.stopped = False
        self.closed = False

    def add_script(self, script: str, use_local_scope: object = None) -> _ErrPowerShell:
        self.script = script
        return self

    def invoke(self, input: object = None, **_kw: object) -> list[str]:
        loc = getattr(self.pool, "location", r"C:\Users\mock")
        return [f"__MRC_PS_CWD_MARKER__{loc}"]

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _NoInvokeRunspaceHandle:
    """Runspace handle with NO ``invoke`` method so ``runspace_invoke`` skips
    the mock path (``callable(invoker)`` is False) and takes the real-pypsrp
    branch - exercising ``_format_ps_errors`` on the production path."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.location = location
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RunspaceOnlySession:
    """WinRM session exposing only ``open_runspace`` -> a no-invoke handle."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.cwd = location
        self.home = location
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.handle: _NoInvokeRunspaceHandle | None = None

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> _NoInvokeRunspaceHandle:
        self.handle = _NoInvokeRunspaceHandle(location=self.cwd)
        return self.handle


def test_winrm_runspace_invoke_formats_ps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the real-pypsrp runspace_invoke path, _format_ps_errors is
    called and the error stream is surfaced in RunspaceResult.stderr with
    had_errors=True / exit_code=1, while the folded location probe still
    populates ``location`` and the marker stays out of stdout."""
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _ErrPowerShell)
    monkeypatch.setattr(_ErrPowerShell, "errors", ["Permission denied", "boom"])
    monkeypatch.setattr(_ErrPowerShell, "had_errors", True)

    sess = _RunspaceOnlySession()

    def conn(**_kw: object) -> _RunspaceOnlySession:
        return sess

    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=conn,
    )
    t.connect()
    handle = t.open_runspace()
    # open_runspace always wraps: no-invoke handle -> pool adapter.
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is sess.handle

    # timeout_s=None -> direct ps.invoke() (no async-bridge watchdog), keeping
    # the test single-threaded while still exercising the real path.
    result = t.runspace_invoke(handle, "Write-Error 'boom'")

    assert result.had_errors is True
    assert result.exit_code == 1
    assert "Permission denied" in result.stderr
    assert "boom" in result.stderr
    # Location parsed from the folded probe (no separate Get-Location RT).
    assert result.location == r"C:\Users\mock"
    # The marker must NOT leak into stdout.
    assert "__MRC_PS_CWD_MARKER__" not in (result.stdout or "")


def test_winrm_adapt_runspace_handle_invoke_vs_pool() -> None:
    """open_runspace classification: invoke API -> InvokeRunspaceAdapter;
    no invoke (pypsrp pool / duck pool) -> PypsrpPoolRunspaceAdapter.
    Idempotent for already-adapted handles."""
    # Real pypsrp RunspacePool has no invoke -> pool adapter.
    real_pool = RunspacePool.__new__(RunspacePool)
    adapted_pool = _adapt_runspace_handle(real_pool, default_location=r"C:\Users\mock")
    assert isinstance(adapted_pool, PypsrpPoolRunspaceAdapter)
    assert adapted_pool.inner is real_pool

    # Mock runspace: has .invoke -> invoke adapter.
    class _MockRunspace:
        def __init__(self) -> None:
            self.location = r"C:\Users\mock"

        def invoke(self, script: str) -> None:
            return None

        def close(self) -> None:
            return None

    mock = _MockRunspace()
    adapted_mock = _adapt_runspace_handle(mock)
    assert isinstance(adapted_mock, InvokeRunspaceAdapter)
    assert adapted_mock.inner is mock
    assert adapted_mock.location == r"C:\Users\mock"

    # Duck-type pool: no .invoke -> pool adapter.
    class _DuckPool:
        min_runspaces = 1
        max_runspaces = 4

    duck = _DuckPool()
    assert isinstance(_adapt_runspace_handle(duck), PypsrpPoolRunspaceAdapter)

    # Already-adapted: returned as-is.
    assert _adapt_runspace_handle(adapted_mock) is adapted_mock
    assert _adapt_runspace_handle(adapted_pool) is adapted_pool


def test_winrm_adapt_runspace_handle_carries_the_op_lock() -> None:
    """A pool adapter keeps the serial lock its release must exchange under.

    The pool adapter's release runs after its invoke returned, on no thread
    that holds a serial zone, so the transport has to hand its lock to the
    handle it adapts - including a handle that is already an adapter, which is
    returned as-is and would otherwise drop the lock silently. A handle adapted
    without one keeps releasing as before.
    """
    lock = threading.RLock()

    fresh = _adapt_runspace_handle(
        RunspacePool.__new__(RunspacePool),
        default_location=r"C:\Users\mock",
        op_lock=lock,
    )
    assert isinstance(fresh, PypsrpPoolRunspaceAdapter)
    assert fresh.op_lock is lock

    # Already an adapter: the lock still arrives with the handle.
    re_adapted = _adapt_runspace_handle(fresh, op_lock=lock)
    assert re_adapted is fresh
    assert re_adapted.op_lock is lock

    # No lock given: the adapter releases without one, as it did before.
    assert _adapt_runspace_handle(RunspacePool.__new__(RunspacePool)).op_lock is None

    class _MockRunspace:
        def invoke(self, script: str) -> None:
            return None

    # An invoke adapter exchanges inside its caller's zone and takes no lock.
    mock = _MockRunspace()
    adapted_mock = _adapt_runspace_handle(mock, op_lock=lock)
    assert isinstance(adapted_mock, InvokeRunspaceAdapter)
    assert not hasattr(adapted_mock, "op_lock")


class _ZoneHookRegistry:
    """Minimal serial-zone registry: records what a handle registers with it."""

    def __init__(self) -> None:
        self.hooks: list[Any] = []

    def add(self, hook: Any) -> None:
        if hook not in self.hooks:
            self.hooks.append(hook)

    def discard(self, hook: Any) -> None:
        if hook in self.hooks:
            self.hooks.remove(hook)

    def run(self) -> None:
        for hook in tuple(self.hooks):
            hook()


def test_pool_adapter_registers_its_drain_with_the_transports_zones() -> None:
    """The adapter's retained-release drain is the transport's to schedule.

    A release a handle retains has to be retried by the next serial zone the
    transport enters, whichever handle or surface enters it, so the handle
    registers its drain on the transport's registry - handed to it with the
    lock by ``open_runspace`` - and never keeps a schedule of its own. Closing
    the handle takes the registration back: the pool is gone, so a later zone
    must not ask it for a release. Re-binding the same registry leaves one
    registration, not two.
    """
    pool, _fake = _pool_with_fake_protocol(location=r"C:\Users\mock")
    registry = _ZoneHookRegistry()

    adapter = PypsrpPoolRunspaceAdapter(pool, serial_zone_hooks=registry)
    assert len(registry.hooks) == 1
    assert registry.hooks[0].__self__ is adapter, "another handle's drain was registered"
    registry.run()  # a zone entered elsewhere reaches this handle's drain

    re_adapted = _adapt_runspace_handle(adapter, serial_zone_hooks=registry)
    assert re_adapted is adapter
    assert len(registry.hooks) == 1, "the drain was registered twice"

    adapter.close()
    assert registry.hooks == [], "a closed handle was still asked to drain"

    # A handle adapted without a registry owes nothing and closes on its own:
    # no registration was made, so there is none to take back.
    standalone = _adapt_runspace_handle(
        _pool_with_fake_protocol(location=r"C:\Users\mock")[0]
    )
    assert isinstance(standalone, PypsrpPoolRunspaceAdapter)
    assert standalone.pending_release_count == 0
    standalone.close()


def test_pool_adapter_detach_keeps_its_registration_while_a_release_is_owed() -> None:
    """A retired handle is held only for the releases it still owes.

    Retirement is local - nothing is exchanged and no lock is waited for - and
    what it has to take back is the registration the handle made on the
    transport's registry: the registry holds the handle, so a retired one that
    owes nothing would keep its pool alive for the life of the transport. A
    handle that still owes a release keeps the registration, because a later
    serial zone is the only thing that can land it, and the drain drops the
    registration itself once it is retired and owes nothing.
    """
    pool, _fake = _pool_with_fake_protocol(location=r"C:\Users\mock")
    registry = _ZoneHookRegistry()
    adapter = PypsrpPoolRunspaceAdapter(pool, serial_zone_hooks=registry)
    assert len(registry.hooks) == 1

    # Nothing owed: the retirement takes the registration back, and asking
    # twice answers the same way.
    adapter.detach()
    assert registry.hooks == [], "a retired handle that owes nothing stayed registered"
    adapter.detach()
    assert registry.hooks == []

    # Owed: the registration stays until a drain lands the release.
    adapter._retain_pending_release(object())
    adapter.detach()
    assert adapter.pending_release_count == 1
    assert len(registry.hooks) == 1, (
        "a retired handle that still owes a release lost the only route to it"
    )
    assert registry.hooks[0].__self__ is adapter

    registry.run()  # the next zone drains the debt
    assert adapter.pending_release_count == 0
    assert registry.hooks == [], (
        "a retired handle that owes nothing is still held by the registry"
    )

    adapter.close()  # the local retirement is not a substitute for the teardown
    assert registry.hooks == []


def test_pool_adapter_prepare_invoke_stop_is_per_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_invoke returns a stop that only affects that pipeline;
    handle.stop() is a no-op so concurrent invokes cannot cross-kill."""

    class _Streams:
        error: list[object] = []

    class _Ps:
        instances: list[_Ps] = []

        def __init__(self, pool: object) -> None:
            self.pool = pool
            self.script: str | None = None
            self.stopped = False
            self.closed = False
            self.had_errors = False
            self.streams = _Streams()
            _Ps.instances.append(self)

        def add_script(self, script: str, use_local_scope: object = None) -> _Ps:
            self.script = script
            return self

        def invoke(self, input: object = None, **_kw: object) -> list[str]:
            del input, _kw
            loc = r"C:\Users\mock"
            return [f"__MRC_PS_CWD_MARKER__{loc}"]

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    _Ps.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _Ps)

    class _Pool:
        location = r"C:\Users\mock"

        def close(self) -> None:
            return None

    adapter = PypsrpPoolRunspaceAdapter(_Pool(), default_location=r"C:\Users\mock")
    run_a, stop_a = adapter.prepare_invoke("script-A")
    run_b, stop_b = adapter.prepare_invoke("script-B")
    assert len(_Ps.instances) == 2
    ps_a, ps_b = _Ps.instances[0], _Ps.instances[1]

    # Per-invoke stop only touches its own pipeline.
    stop_a()
    assert ps_a.stopped is True
    assert ps_b.stopped is False

    stop_b()
    assert ps_b.stopped is True

    # Shared handle.stop() must not kill pipelines (concurrent-safe no-op).
    ps_a.stopped = False
    ps_b.stopped = False
    adapter.stop()
    assert ps_a.stopped is False
    assert ps_b.stopped is False

    # run still works after prepare (B pipeline not stopped above reset).
    result = run_b()
    assert result.exit_code == 0
    assert result.location == r"C:\Users\mock"
    # Silence unused run_a (prepare still exercised construction).
    del run_a


def test_pool_adapter_in_flight_stop_does_not_cancel_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight prepare_invoke pipelines: A's stop must not cancel B."""

    class _Streams:
        error: list[object] = []

    class _Ps:
        instances: list[_Ps] = []
        both = threading.Barrier(2, timeout=10.0)
        release_b = threading.Event()
        b_entered = threading.Event()
        a_stopped = threading.Event()

        def __init__(self, pool: object) -> None:
            self.pool = pool
            self.script: str | None = None
            self.stopped = False
            self.closed = False
            self.had_errors = False
            self.streams = _Streams()
            self._block = threading.Event()
            _Ps.instances.append(self)

        def add_script(self, script: str, use_local_scope: object = None) -> _Ps:
            self.script = script
            return self

        def invoke(self, input: object = None, **_kw: object) -> list[str]:
            del input, _kw
            script = self.script or ""
            if "HANG" in script:
                _Ps.both.wait()
                self._block.wait(timeout=30.0)
                return []
            _Ps.b_entered.set()
            _Ps.both.wait()
            _Ps.a_stopped.wait(timeout=30.0)
            if self.stopped:
                return []
            return ["b-ok", r"__MRC_PS_CWD_MARKER__C:\Users\mock"]

        def stop(self) -> None:
            self.stopped = True
            self._block.set()

        def close(self) -> None:
            self.closed = True

    _Ps.instances.clear()
    _Ps.release_b.clear()
    _Ps.b_entered.clear()
    _Ps.a_stopped.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _Ps)

    class _Pool:
        location = r"C:\Users\mock"

        def close(self) -> None:
            return None

    adapter = PypsrpPoolRunspaceAdapter(_Pool(), default_location=r"C:\Users\mock")
    run_a, stop_a = adapter.prepare_invoke("Write-Output HANG")
    run_b, stop_b = adapter.prepare_invoke("$x = 'b-ok'; $x")
    del stop_b
    assert len(_Ps.instances) == 2
    ps_a, ps_b = _Ps.instances[0], _Ps.instances[1]

    out: dict[str, object] = {}
    errs: list[BaseException] = []

    def call_a() -> None:
        try:
            out["a"] = run_a()
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    def call_b() -> None:
        try:
            out["b"] = run_b()
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    ta = threading.Thread(target=call_a, daemon=True)
    tb = threading.Thread(target=call_b, daemon=True)
    ta.start()
    tb.start()
    assert _Ps.b_entered.wait(timeout=5.0), "B never entered invoke"
    # Both pipelines are inside invoke (barrier). Timeout stop only A's.
    stop_a()
    _Ps.a_stopped.set()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)
    assert not ta.is_alive()
    assert not tb.is_alive()
    assert not errs, errs

    rb = out["b"]
    assert getattr(rb, "exit_code", -1) == 0
    assert "b-ok" in (getattr(rb, "stdout", "") or "")
    assert ps_a.stopped is True
    assert ps_b.stopped is False


def test_runspace_invoke_concurrent_timeout_stops_only_own_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight adapter pipelines: A timeout stops A only; B is not stopped.

    Uses ``runspace_invoke``'s unlocked body so both ``prepare_invoke``
    pipelines overlap. The ``_op_lock`` wrapper would serialize the calls
    and only rendezvous B with A's leftover worker after A timed out.
    """

    class _Streams:
        error: list[object] = []

    class _Ps:
        instances: list[_Ps] = []
        both = threading.Barrier(2, timeout=10.0)
        release_b = threading.Event()
        b_entered = threading.Event()
        a_returned = threading.Event()

        def __init__(self, pool: object) -> None:
            self.pool = pool
            self.script: str | None = None
            self.stopped = False
            self.closed = False
            self.had_errors = False
            self.streams = _Streams()
            self._block = threading.Event()
            _Ps.instances.append(self)

        def add_script(self, script: str, use_local_scope: object = None) -> _Ps:
            self.script = script
            return self

        def invoke(self, input: object = None, **_kw: object) -> list[str]:
            del input, _kw
            script = self.script or ""
            if "HANG" in script:
                _Ps.both.wait()
                self._block.wait(timeout=30.0)
                return []
            _Ps.b_entered.set()
            _Ps.both.wait()
            if _Ps.a_returned.is_set():
                raise AssertionError(
                    "sibling pipeline entered invoke after A timed out; "
                    "the two invokes did not overlap"
                )
            _Ps.release_b.wait(timeout=30.0)
            if self.stopped:
                return []
            return ["b-ok", r"__MRC_PS_CWD_MARKER__C:\Users\mock"]

        def stop(self) -> None:
            self.stopped = True
            self._block.set()

        def close(self) -> None:
            self.closed = True

    _Ps.instances.clear()
    _Ps.release_b.clear()
    _Ps.b_entered.clear()
    _Ps.a_returned.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _Ps)

    class _PoolSess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\mock"
            self.home = r"C:\Users\mock"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.pool = type("P", (), {"location": r"C:\Users\mock", "close": lambda s: None})()

        def close(self) -> None:
            self.closed = True

        def open_runspace(self) -> object:
            return self.pool

    sess = _PoolSess()
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    unlocked = getattr(t.runspace_invoke, "__wrapped__", None)
    assert callable(unlocked), "runspace_invoke must expose the unlocked body"

    out: dict[str, object] = {}
    errs: list[BaseException] = []

    def call_a() -> None:
        try:
            out["a"] = unlocked(t, handle, "Write-Output HANG", timeout_s=0.7)
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)
        finally:
            _Ps.a_returned.set()

    def call_b() -> None:
        try:
            out["b"] = unlocked(
                t, handle, "Write-Output sibling", timeout_s=10.0
            )
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    ta = threading.Thread(target=call_a, daemon=True)
    tb = threading.Thread(target=call_b, daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=8.0)
    assert not ta.is_alive()
    assert _Ps.b_entered.is_set(), "B must have entered invoke while A was in-flight"
    _Ps.release_b.set()
    tb.join(timeout=8.0)
    assert not tb.is_alive()
    assert not errs, errs

    ra = out["a"]
    rb = out["b"]
    assert getattr(ra, "timed_out", False) is True
    assert getattr(rb, "timed_out", False) is False
    assert getattr(rb, "exit_code", -1) == 0
    assert "b-ok" in (getattr(rb, "stdout", "") or "")

    hang = [p for p in _Ps.instances if p.script and "HANG" in p.script]
    sib = [p for p in _Ps.instances if p.script and "sibling" in p.script]
    assert hang and hang[0].stopped is True
    assert sib and sib[0].stopped is False


def test_runspace_invoke_location_does_not_pollute_transport_cwd() -> None:
    """Set-Location via runspace_invoke updates result/handle location
    but must not write back into WinRMTransport.cwd (exec/fs default work)."""
    from mcp_remote_control.transport.winrm import RunspaceResult

    seed = r"C:\Users\mock"
    moved = r"C:\Users\mock\work"

    class _LocRunspace:
        def __init__(self) -> None:
            self.location: str | None = seed

        def invoke(self, script: str) -> RunspaceResult:
            # Mimic mock Set-Location + location probe.
            if "Set-Location" in script or "cd " in script.lower():
                self.location = moved
            return RunspaceResult(
                stdout="",
                exit_code=0,
                location=self.location,
            )

        def close(self) -> None:
            return None

    class _Sess:
        def __init__(self) -> None:
            self.cwd = seed
            self.home = seed
            self.os = "windows"
            self.shell = "powershell"
            self.closed = False
            self.handle: _LocRunspace | None = None

        def close(self) -> None:
            self.closed = True

        def open_runspace(self) -> _LocRunspace:
            self.handle = _LocRunspace()
            return self.handle

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            del command, timeout_s, env
            return ExecResult(exit_code=0, stdout="ok\n", cwd=cwd or self.cwd)

    sess = _Sess()

    def conn(**_kw: object) -> _Sess:
        return sess

    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=conn,
    )
    t.connect()
    assert t.cwd == seed

    handle = t.open_runspace()
    # open_runspace seeds default_location from transport.cwd.
    assert getattr(handle, "location", None) in (seed, None) or (
        getattr(handle, "location", None) == seed
    )

    result = t.runspace_invoke(handle, f"Set-Location '{moved}'")
    assert result.location == moved
    # Per-runspace tracking still updates (adapter/inner).
    assert handle.location == moved

    # Transport default cwd for exec/fs stays at open seed - not polluted.
    assert t.cwd == seed
    assert t.cwd != moved

    # Default work path for run_command (no explicit cwd) uses transport.cwd.
    exec_r = t.run_command("hostname")
    assert exec_r.cwd == seed


# ---------------------------------------------------------------------------
# open_runspace / close_runspace wall-clock
# ---------------------------------------------------------------------------

def test_winrm_open_runspace_hang_fails_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-returning open_runspace must not hang the caller forever."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangOpenSession:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def close(self) -> None:
            return None

        def open_runspace(self) -> object:
            block.wait(timeout=30.0)
            return object()

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
        connector=lambda **_k: _HangOpenSession(),
    )
    t.connect()
    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        t.open_runspace()
    elapsed = time.monotonic() - t0
    assert ei.value.code == "EXEC_FAILED"
    assert "timed out" in (ei.value.msg or "").lower() or "timeout" in str(
        ei.value
    ).lower()
    # Budget 0.25s; allow scheduler slack, never a 30s hang.
    assert elapsed < 2.0, f"open_runspace wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_winrm_close_runspace_hang_returns_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-returning handle.close must return within budget (best-effort)."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangCloseHandle:
        def close(self) -> None:
            block.wait(timeout=30.0)

    class _Sess:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def close(self) -> None:
            return None

        def open_runspace(self) -> _HangCloseHandle:
            return _HangCloseHandle()

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
    t0 = time.monotonic()
    verdict = t.close_runspace(handle)  # must not raise
    elapsed = time.monotonic() - t0
    assert verdict == "timeout", "an abandoned teardown is not a landed delete"
    assert elapsed < 2.0, f"close_runspace wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_winrm_open_close_runspace_normal_path_unchanged() -> None:
    """Normal open_runspace + close still succeeds and releases the handle."""

    class _Handle:
        def __init__(self) -> None:
            self.closed = False
            self.location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(
                stdout="1",
                exit_code=0,
                location=self.location,
            )

        def close(self) -> None:
            self.closed = True

    class _Sess:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def __init__(self) -> None:
            self.handle = _Handle()

        def close(self) -> None:
            return None

        def open_runspace(self) -> _Handle:
            return self.handle

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

    sess = _Sess()
    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()
    assert handle is not None
    r = t.runspace_invoke(handle, "Write-Output 1")
    assert r.exit_code == 0
    t.close_runspace(handle)
    assert sess.handle.closed is True


# ---------------------------------------------------------------------------
# Runspace open/close link policy (stale-framing refusal)
#
# Plain-HTTP WinRM with message encryption rejects a request made with a stale
# context with an empty-body HTTP 400 - provably pre-execution. The runspace
# Create and the WSMan Delete are payload-carrying requests on that link, so
# both must classify the rejection, re-handshake and replay once instead of
# reporting a generic failure (or, for the delete, silently swallowing it).
# ---------------------------------------------------------------------------


def _stale_encryption_error() -> BaseException:
    """The measured stale-framing shape: HTTP 400, empty body."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", 400, "")
    return type(
        "WinRMTransportError",
        (Exception,),
        {"__module__": "pypsrp.exceptions", "__qualname__": "WinRMTransportError"},
    )("http", 400, "")


def _gateway_error() -> BaseException:
    """A gateway's own error page: a body proves an intermediary answered."""
    body = "Connection error: read ETIMEDOUT"
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", 502, body)
    return type(
        "WinRMTransportError",
        (Exception,),
        {"__module__": "pypsrp.exceptions", "__qualname__": "WinRMTransportError"},
    )("http", 502, body)


class _FakeHttpSession:
    """Stand-in for the cached ``requests.Session`` a re-handshake drops."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePypsrpTransport:
    """Node holding the cached encryption context resync clears."""

    def __init__(self, session: _FakeHttpSession) -> None:
        self.encryption: str | None = "auto"
        self.session = session


class _FakeWsman:
    def __init__(self, transport: _FakePypsrpTransport) -> None:
        self.transport = transport


class _RunspaceHandle:
    """Minimal runspace handle (``invoke`` / ``close`` / ``location``)."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.location = location
        self.deleted = False

    def invoke(self, script: str) -> object:
        return RunspaceResult(stdout="ok", exit_code=0, location=self.location)

    def close(self) -> None:
        self.deleted = True


class _RefusingRunspaceSession:
    """Session whose runspace Create is refused while the link is stale.

    ``wsman.transport`` mirrors pypsrp's link state, so a real re-handshake is
    observable (resync clears ``encryption`` and closes the cached HTTP
    session). ``always`` models a server-side refusal that survives the
    re-handshake.
    """

    def __init__(self, *, error: BaseException, always: bool = False) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.closed = False
        self.opens = 0
        self.http_session = _FakeHttpSession()
        self.transport = _FakePypsrpTransport(self.http_session)
        self.wsman = _FakeWsman(self.transport)
        self._error = error
        self._always = always

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> _RunspaceHandle:
        self.opens += 1
        if self._always or self.transport.encryption is not None:
            raise self._error
        return _RunspaceHandle()


def _refusing_transport(session: _RefusingRunspaceSession) -> WinRMTransport:
    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: session,
    )
    t.connect()
    return t


def test_winrm_open_runspace_provable_refusal_rehandshakes_and_replays() -> None:
    """An empty-body 4xx on the runspace Create heals the link in place.

    The rejection is provably pre-execution, so the open re-handshakes and
    replays once: the caller gets a handle, the replayed open is recorded, and
    the session is neither reported dead nor left with a stale context.
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)

    handle = t.open_runspace()

    assert handle is not None
    assert sess.opens == 2, "exactly one replay"
    assert sess.transport.encryption is None, "re-handshake cleared the context"
    assert sess.http_session.closed is True
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True
    assert t.meta.get("link_lost") is None


def test_winrm_open_runspace_body_carrying_rejection_is_not_replayed() -> None:
    """With an unobservable exchange counter, a body-carrying rejection is
    refused - it may have been forwarded, so the open reports the loss honestly
    (link marked dead, reopen advice recorded) instead of retrying.

    The gate is progress-based, not body-based, so this pins the *degraded*
    shape only: this double exposes no ``_send_request``, so the transport
    cannot tell whether the rejected request was this operation's first payload
    exchange and must assume the worst. With an observable counter that has not
    advanced the same rejection IS replayed - the documented oneshot policy,
    covered by
    :func:`test_winrm_open_runspace_body_rejection_replays_without_progress`,
    while a progressed counter refuses it
    (:func:`test_winrm_open_runspace_body_rejection_refused_after_progress`).
    """
    sess = _RefusingRunspaceSession(error=_gateway_error())
    t = _refusing_transport(sess)
    assert t._round_trips is None, "this test pins the degraded (no counter) shape"

    with pytest.raises(TransportError) as ei:
        t.open_runspace()
    assert ei.value.code == "EXEC_FAILED"
    assert "link lost during open_runspace" in ei.value.msg
    assert sess.opens == 1, "no replay for a non-provable rejection"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    assert t.is_connected() is False


class _CountingPypsrpTransport(_FakePypsrpTransport):
    """Link node exposing ``_send_request``, so the round-trip counter installs.

    Production pypsrp's HTTP transport has this method; the base double omits it
    on purpose (that is what the degraded-shape tests exercise).
    """

    def __init__(self, session: _FakeHttpSession) -> None:
        super().__init__(session)
        self.sends = 0

    def _send_request(self, request: object = None, timeout: object = None) -> bytes:
        self.sends += 1
        return b""


class _FakePreparedRequest:
    """Body-carrying request shape (pypsrp's bodyless auth POST is not counted)."""

    def __init__(self) -> None:
        self.body = b"<wsman/>"
        self.url = "http://h:5985/wsman"


class _CountingRefusingSession(_RefusingRunspaceSession):
    """``_RefusingRunspaceSession`` whose link exchange counter is observable.

    ``exchanges_before_rejection`` models exchanges this operation already
    completed: a counter that advanced proves the rejected request was *not* the
    operation's first payload exchange.
    """

    def __init__(
        self,
        *,
        error: BaseException,
        always: bool = False,
        exchanges_before_rejection: int = 0,
    ) -> None:
        super().__init__(error=error, always=always)
        self.transport = _CountingPypsrpTransport(self.http_session)
        self.wsman = _FakeWsman(self.transport)
        self._exchanges = exchanges_before_rejection

    def open_runspace(self) -> _RunspaceHandle:
        self.opens += 1
        for _ in range(self._exchanges):
            self.transport._send_request(_FakePreparedRequest())
        if self._always or self.transport.encryption is not None:
            raise self._error
        return _RunspaceHandle()


def test_winrm_open_runspace_body_rejection_replays_without_progress() -> None:
    """An observable counter with no progress proves the rejected request was
    this open's first payload exchange (a structural shell Create), so the
    documented oneshot policy replays it even though it carried a body."""
    sess = _CountingRefusingSession(error=_gateway_error())
    t = _refusing_transport(sess)
    assert t._round_trips is not None, "the counter must be observable here"

    handle = t.open_runspace()

    assert handle is not None
    assert sess.opens == 2, "exactly one replay"
    assert sess.transport.encryption is None, "re-handshake cleared the context"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True
    assert t.meta.get("link_lost") is None


def test_winrm_open_runspace_body_rejection_refused_after_progress() -> None:
    """The same rejection is refused once the counter advanced: some exchange
    completed during this open, so the rejected request is not its first and a
    replay could repeat work the gateway may already have forwarded."""
    sess = _CountingRefusingSession(
        error=_gateway_error(), exchanges_before_rejection=1
    )
    t = _refusing_transport(sess)
    assert t._round_trips is not None, "the counter must be observable here"

    with pytest.raises(TransportError) as ei:
        t.open_runspace()
    assert ei.value.code == "EXEC_FAILED"
    assert "link lost during open_runspace" in ei.value.msg
    assert sess.opens == 1, "no replay after a completed exchange"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.is_connected() is False


def test_winrm_open_runspace_server_side_failure_keeps_link_alive() -> None:
    """A WSMan fault is an answer from a reachable server: link untouched."""
    sess = _RefusingRunspaceSession(error=RuntimeError("open handshake failed"))
    t = _refusing_transport(sess)

    with pytest.raises(TransportError) as ei:
        t.open_runspace()
    assert ei.value.code == "EXEC_FAILED"
    assert "open_runspace failed: open handshake failed" in ei.value.msg
    assert sess.opens == 1
    assert t.is_connected() is True
    assert t.meta.get("marked_dead") is None


def test_winrm_close_runspace_replays_a_refused_delete() -> None:
    """The WSMan Delete refused once is re-handshaked and replayed to landed.

    The verdict is what a caller must report, so the transport confirms the
    delete really reached the server instead of treating the refusal as a
    finished (best-effort) teardown.
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()

    def close_refused_until_rehandshake() -> None:
        if sess.transport.encryption is not None:
            raise _stale_encryption_error()
        handle.deleted = True

    handle.close = close_refused_until_rehandshake  # type: ignore[method-assign]

    verdict = t.close_runspace(handle)

    assert verdict == "closed"
    assert handle.deleted is True, "the delete landed after the replay"
    assert sess.transport.encryption is None, "re-handshake ran before the replay"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True


def test_winrm_close_runspace_unconfirmed_delete_is_not_reported_landed() -> None:
    """A delete that never lands reports unconfirmed and drops the link."""
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()

    def close_always_refused() -> None:
        raise _stale_encryption_error()

    handle.close = close_always_refused  # type: ignore[method-assign]

    verdict = t.close_runspace(handle)

    assert verdict == "unconfirmed"
    assert handle.deleted is False
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.is_connected() is False


def _shell_gone_fault() -> BaseException:
    """The WSMan fault meaning "no object matches these selectors" (0x8033805B),
    i.e. the shell was reaped; pypsrp reads it as "the pool is closed"."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WSManFaultError(
            0x8033805B, "host", "The shell was not found", None, None, None
        )
    return type(  # pragma: no cover - pypsrp is a hard dependency
        "WSManFaultError",
        (Exception,),
        {
            "__module__": "pypsrp.exceptions",
            "__qualname__": "WSManFaultError",
            "code": 0x8033805B,
        },
    )(0x8033805B, "host", "The shell was not found")


def _other_wsman_fault() -> BaseException:
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WSManFaultError(
            5, "host", "Access is denied.", None, None, None
        )
    return type(  # pragma: no cover - pypsrp is a hard dependency
        "WSManFaultError",
        (Exception,),
        {
            "__module__": "pypsrp.exceptions",
            "__qualname__": "WSManFaultError",
            "code": 5,
        },
    )(5, "host", "Access is denied.")


def test_winrm_close_runspace_shell_gone_fault_reads_as_landed() -> None:
    """A Delete answered with "no such shell" proves the runspace is gone.

    The server answered, so the fault is readable evidence: reporting
    "unconfirmed" (and telling the operator the runspace may still be
    allocated) would be a false claim about the host.
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()

    def close_raises_shell_gone() -> None:
        raise _shell_gone_fault()

    handle.close = close_raises_shell_gone  # type: ignore[method-assign]

    verdict = t.close_runspace(handle)

    assert verdict == "closed"
    assert t.is_connected() is True, "a SOAP fault is an answer from a live link"
    assert t.meta.get("marked_dead") is None


def test_winrm_close_runspace_other_fault_stays_unconfirmed() -> None:
    """Any other WSMan fault leaves the runspace's fate unknown: the delete is
    not proven to have landed, so the verdict must not claim it did."""
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()

    def close_raises_other_fault() -> None:
        raise _other_wsman_fault()

    handle.close = close_raises_other_fault  # type: ignore[method-assign]

    verdict = t.close_runspace(handle)

    assert verdict == "unconfirmed"
    assert t.is_connected() is True
    assert t.meta.get("marked_dead") is None


def test_winrm_close_runspace_replay_answered_by_shell_gone_fault() -> None:
    """The *replayed* Delete can itself be answered with "no such shell".

    The first Delete is refused by the stale framing layer (provably
    pre-execution, so it is replayed after a re-handshake); the server then
    answers the replay with a shell-gone fault. Either Delete may have landed,
    and the server's own answer proves the shell no longer exists, so the
    verdict must be landed rather than "unconfirmed".
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()

    def close_refused_then_shell_gone() -> None:
        if sess.transport.encryption is not None:
            raise _stale_encryption_error()
        raise _shell_gone_fault()

    handle.close = close_refused_then_shell_gone  # type: ignore[method-assign]

    verdict = t.close_runspace(handle)

    assert verdict == "closed"
    assert sess.transport.encryption is None, "the replay ran after a re-handshake"
    assert t.meta.get("session_resynced") is True
    assert t.meta.get("link_lost") is None, "a live server answer must not kill the link"
    assert t.is_connected() is True


# ---------------------------------------------------------------------------
# close_runspace_within: a caller-owned deadline covers delete + recovery.
# ---------------------------------------------------------------------------


def test_winrm_close_runspace_within_shares_the_callers_deadline() -> None:
    """The delete and its replayed recovery share the caller's one deadline.

    The first Delete is refused after 80ms and a replayed Delete needs another
    80ms. Handing the recovery a fresh budget would let both stages run and
    report a landed delete under a 100ms deadline - the replay is cut instead
    and the verdict is a wall-clock miss.
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()
    deletes: list[int] = []

    def close_refused_then_slow() -> None:
        deletes.append(1)
        if len(deletes) == 1:
            time.sleep(0.08)
            raise _stale_encryption_error()
        time.sleep(0.08)
        handle.deleted = True

    handle.close = close_refused_then_slow  # type: ignore[method-assign]

    t0 = time.monotonic()
    verdict = t.close_runspace_within(handle, timeout_s=0.1)
    elapsed = time.monotonic() - t0

    assert verdict == "timeout"
    assert handle.deleted is False, "the replay outran the caller's deadline"
    assert sess.transport.encryption is None, "the recovery re-handshake did run"
    assert len(deletes) >= 2, deletes
    assert elapsed < 0.15, f"a second full close stage was paid for: {elapsed}s"


def test_winrm_close_runspace_within_spent_deadline_starts_no_delete() -> None:
    """A spent deadline starts no delete: the verdict is the wall-clock miss.

    A caller whose wait for the serial lock consumed the budget has nothing
    left to spend, so the transport must not open a teardown it cannot wait
    for (that would be the fresh-budget defect in a different shape).
    """
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()
    calls: list[int] = []

    def close_counting() -> None:
        calls.append(1)

    handle.close = close_counting  # type: ignore[method-assign]

    assert t.close_runspace_within(handle, timeout_s=0.0) == "timeout"
    assert t.close_runspace_within(handle, timeout_s=-1.0) == "timeout"
    assert calls == [], "a delete was started with no wall clock to wait for it"


def test_winrm_close_runspace_lock_wait_is_bounded_by_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait for the transport serial lock is drawn from the close budget.

    A caller with no deadline of its own - ``close_runspace`` is called bare by
    the ps-open fence cleanup - must not be pinned by whatever holds the lock.
    The wait is abandoned at the budget with a wall-clock verdict and no delete
    started, so the runspace's fate is never claimed from a teardown that never
    ran.
    """
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.1)
    sess = _RefusingRunspaceSession(error=_stale_encryption_error())
    t = _refusing_transport(sess)
    handle = _RunspaceHandle()
    deletes: list[int] = []
    handle.close = lambda: deletes.append(1)  # type: ignore[method-assign]

    held = threading.Event()
    release = threading.Event()
    verdicts: list[str] = []

    def hold_lock() -> None:
        with t.serial_ops():
            held.set()
            assert release.wait(timeout=10.0), "the lock was never released"

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert held.wait(timeout=5.0), "the lock was never taken"

    closer = threading.Thread(
        target=lambda: verdicts.append(t.close_runspace(handle)), daemon=True
    )
    t0 = time.monotonic()
    closer.start()
    closer.join(timeout=2.0)
    elapsed = time.monotonic() - t0
    release.set()
    holder.join(timeout=10.0)

    assert not closer.is_alive(), "close_runspace waited for the lock without a deadline"
    assert verdicts == ["timeout"]
    assert deletes == [], "a delete was started with no wall clock left to wait for it"
    assert elapsed < 0.5, f"the lock wait outran the close budget: {elapsed}s"

    # Positive control: with the lock free the same call lands the delete, so
    # the timeout above is a budget fact and not a blanket refusal.
    assert t.close_runspace(handle) == "closed"
    assert deletes == [1]


# ---------------------------------------------------------------------------
# Pipeline release on a persistent pool: real pypsrp RunspacePool/PowerShell
# driven by a fake protocol layer. A fake PowerShell that never registers a
# pipeline in ``pool.pipelines`` would prove nothing here - the accumulation
# lives in pypsrp's own registry, so both sides of it are real.
# ---------------------------------------------------------------------------

_PSRP_RSP_NS = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell"
_PSRP_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
_PSRP_DESTINATION_SERVER = 0x00000002


def _soap_body(payload: object) -> object:
    """WSMan answers with the SOAP Body; pypsrp reads ``rsp:*`` below it."""
    body = ET.Element(f"{{{_PSRP_SOAP_NS}}}Body")
    body.append(payload)  # type: ignore[arg-type]
    return body


class _FakePsrpProtocol:
    """Fake WSMan/PSRP peer for a real pypsrp ``RunspacePool``.

    Implements the WSMan surface ``WinRS`` drives (``command`` / ``send`` /
    ``receive`` / ``signal`` / ``delete``) and answers a pipeline's poll with
    PIPELINE_OUTPUT messages plus a COMPLETED ``PipelineState``, so a real
    ``PowerShell.invoke()`` finishes without a host. Responses are framed with
    pypsrp's own fragmenter/serializer, so the pool receives real PSRP bytes.

    Records what the runspace actually did (commands, the runspace pool id each
    command ran on, the CommandId each poll was addressed with, signals, shell
    deletes), and offers the hooks the failure and blocked-cleanup tests need:
    ``command_failures`` raises on that many ``command`` calls,
    ``command_gate`` holds the next Command's response open (one-shot), so a
    Command can be left outstanding while the run that sent it is still on the
    wire, ``receive_failures`` raises on that many polls (a fault during the
    invoke, with the pipeline already RUNNING), ``receive_gate`` holds the next
    poll open (one-shot), ``terminate_gate`` holds the TERMINATE that
    ``close()`` sends, and ``ctrl_c_gate`` holds the PS_CTRL_C that ``stop()``
    sends. A ``command`` that starts while a TERMINATE is still unanswered is
    counted: those two exchanges run on one shared session, so the transport
    serial lock must keep them from overlapping.
    """

    max_payload_size = 153600

    def __init__(self, *, location: str) -> None:
        self.location = location
        self.pool: RunspacePool | None = None
        # Output values per receive, oldest first (one entry per pipeline).
        self.responses: list[list[str]] = []
        self.command_failures = 0
        self.receive_failures = 0
        self.command_gate: threading.Event | None = None
        self.receive_gate: threading.Event | None = None
        self.terminate_gate: threading.Event | None = None
        self.ctrl_c_gate: threading.Event | None = None
        self.receive_entered = threading.Event()
        self.terminate_started = threading.Event()
        self.ctrl_c_started = threading.Event()
        self.command_entered = threading.Event()
        self.commands_during_terminate = 0
        self.commands: dict[str, str] = {}
        self.runspace_ids: list[str] = []
        # The CommandId selector of each poll, in order: None means the poll was
        # addressed to the runspace pool instead of to a pipeline.
        self.receive_command_ids: list[str | None] = []
        self.signals: list[tuple[str, str]] = []
        self.deletes = 0
        # TERMINATE exchanges the runspace answered: the point at which a
        # release has actually completed. Guarded, because every released
        # pipeline completes its release on its own daemon thread.
        self.terminates_settled = 0
        self._signals_lock = threading.Lock()
        self._next_command = 0
        self._next_fragment = 0

    def signal_codes(self) -> list[str]:
        """Signal codes in the order the runspace sent them."""
        return [code for _command_id, code in self.signals]

    # -- PSRP message construction --------------------------------------
    def _fragment(self, message: bytes) -> bytes:
        self._next_fragment += 1
        return Fragment(self._next_fragment, 0, message, True, True).pack()

    def _header(self, message_type: int, pipeline_id: str) -> bytes:
        pool = self.pool
        assert pool is not None, "the fake must be attached to its pool"
        header = struct.pack("<I", _PSRP_DESTINATION_SERVER)
        header += struct.pack("<I", message_type)
        return header + uuid.UUID(pool.id).bytes_le + uuid.UUID(pipeline_id).bytes_le

    def _output_message(self, value: str, pipeline_id: str) -> bytes:
        pool = self.pool
        assert pool is not None, "the fake must be attached to its pool"
        # PIPELINE_OUTPUT carries the serialized object itself, unencapsulated.
        payload = ET.tostring(
            pool._serializer.serialize(value), encoding="unicode"
        ).encode("utf-8")
        return self._fragment(
            self._header(MessageType.PIPELINE_OUTPUT, pipeline_id) + payload
        )

    def _state_message(self, state: int, pipeline_id: str) -> bytes:
        pool = self.pool
        assert pool is not None, "the fake must be attached to its pool"
        return self._fragment(
            Message(
                _PSRP_DESTINATION_SERVER,
                pool.id,
                pipeline_id,
                PipelineState(state=state),
                pool._serializer,
            ).pack()
        )

    # -- WSMan surface used by WinRS / RunspacePool ---------------------
    def command(
        self, resource_uri: str, cmd: object, option_set: object = None,
        selector_set: object = None,
    ) -> object:
        if self.command_failures > 0:
            self.command_failures -= 1
            raise RuntimeError("command rejected before the pipeline started")
        with self._signals_lock:
            if self.terminate_started.is_set() and self.terminates_settled == 0:
                self.commands_during_terminate += 1
        self.command_entered.set()
        self._next_command += 1
        command_id = f"cmd-{self._next_command}"
        self.commands[command_id] = cmd.attrib.get("CommandId", "")  # type: ignore[attr-defined]
        # The first PSRP fragment of the pipeline names the runspace pool it
        # runs on, so repeated invokes can be proven to share one runspace.
        arguments = cmd.findall(f"{{{_PSRP_RSP_NS}}}Arguments")  # type: ignore[attr-defined]
        if arguments and arguments[0].text:
            fragment, _rest = Fragment.unpack(base64.b64decode(arguments[0].text))
            # pypsrp ids are upper-case; the wire form is lower-case.
            self.runspace_ids.append(
                str(uuid.UUID(bytes_le=fragment.data[8:24])).upper()
            )
        gate = self.command_gate
        if gate is not None:
            # One-shot: the gate is armed for the Command the test means to pin.
            self.command_gate = None
            gate.wait(timeout=30.0)
        body = ET.Element(f"{{{_PSRP_RSP_NS}}}CommandResponse")
        ET.SubElement(body, f"{{{_PSRP_RSP_NS}}}CommandId").text = command_id
        return _soap_body(body)

    def receive(
        self, resource_uri: str, receive: object, option_set: object = None,
        selector_set: object = None, timeout: object = None,
    ) -> object:
        self.receive_entered.set()
        if self.receive_failures > 0:
            self.receive_failures -= 1
            raise RuntimeError("poll rejected while the pipeline was running")
        values = self.responses.pop(0) if self.responses else []
        gate = self.receive_gate
        if gate is not None:
            # One-shot: the gate is armed for the poll the test means to pin.
            self.receive_gate = None
            gate.wait(timeout=30.0)
        stream = receive.find(f"{{{_PSRP_RSP_NS}}}DesiredStream")  # type: ignore[attr-defined]
        command_id = stream.attrib.get("CommandId") if stream is not None else None
        self.receive_command_ids.append(command_id)
        pipeline_id = self.commands.get(command_id or "", "")
        chunks = b"".join(self._output_message(v, pipeline_id) for v in values)
        chunks += self._state_message(PSInvocationState.COMPLETED, pipeline_id)
        body = ET.Element(f"{{{_PSRP_RSP_NS}}}ReceiveResponse")
        state = ET.SubElement(body, f"{{{_PSRP_RSP_NS}}}CommandState", {"State": "Done"})
        ET.SubElement(state, f"{{{_PSRP_RSP_NS}}}ExitCode").text = "0"
        out = ET.SubElement(body, f"{{{_PSRP_RSP_NS}}}Stream", {"Name": "stdout"})
        out.text = base64.b64encode(chunks).decode("utf-8")
        return _soap_body(body)

    def send(
        self, resource_uri: str, send: object, selector_set: object = None
    ) -> object:
        return _soap_body(ET.Element(f"{{{_PSRP_RSP_NS}}}SendResponse"))

    def signal(
        self, resource_uri: str, signal: object, selector_set: object = None
    ) -> object:
        code = signal.find(f"{{{_PSRP_RSP_NS}}}Code")  # type: ignore[attr-defined]
        code_text = code.text if code is not None else ""
        # Recorded before any gate: a blocked signal was still attempted.
        with self._signals_lock:
            self.signals.append(
                (signal.attrib.get("CommandId", ""), code_text)  # type: ignore[attr-defined]
            )
        if code_text == SignalCode.TERMINATE:
            self.terminate_started.set()
            gate = self.terminate_gate
            if gate is not None:
                gate.wait(timeout=30.0)
            with self._signals_lock:
                self.terminates_settled += 1
        elif code_text == SignalCode.PS_CTRL_C:
            self.ctrl_c_started.set()
            gate = self.ctrl_c_gate
            if gate is not None:
                gate.wait(timeout=30.0)
        return _soap_body(ET.Element(f"{{{_PSRP_RSP_NS}}}SignalResponse"))

    def delete(self, resource_uri: str, selector_set: object = None) -> object:
        self.deletes += 1
        return _soap_body(ET.Element(f"{{{_PSRP_RSP_NS}}}DeleteResponse"))


def _pool_with_fake_protocol(
    *, location: str = r"C:\Users\mock"
) -> tuple[RunspacePool, _FakePsrpProtocol]:
    """A real pypsrp ``RunspacePool`` talking to the fake protocol layer.

    The pool is not ``open()``ed - that handshake is not what these tests
    exercise - so the fields ``open()`` would learn are seeded here;
    ``add_script`` needs the protocol version to pick its Command flags.
    """
    fake = _FakePsrpProtocol(location=location)
    pool = RunspacePool(fake)
    fake.pool = pool
    pool.protocol_version = "2.3"
    pool.ps_version = "5.1"
    return pool, fake


def _completed_output(location: str, *, stdout: str) -> list[str]:
    """Output values a completed invoke emits: user output, both probes."""
    return [stdout, f"{_EXIT_MARKER}0", f"{_LOCATION_MARKER}{location}"]


def _wait_for_settled_releases(
    fake: _FakePsrpProtocol, expected: int, *, timeout_s: float = 5.0
) -> None:
    """Wait until *expected* releases have completed on the peer.

    The completion path deregisters the pipeline before the invoke returns and
    completes the remote release on a daemon thread, so a test that asserts how
    the release landed must first wait for it to land - the wait is the test's
    barrier, not the assertion.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fake.terminates_settled >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"only {fake.terminates_settled} pipeline releases completed, "
        f"expected {expected}"
    )


def _reported(caplog: pytest.LogCaptureFixture) -> list[str]:
    """WARNING-or-worse messages captured so far, in the order they landed."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def _release_thread_ids() -> set[int]:
    """Ids of the pipeline-release threads that are still running.

    A release runs on a daemon thread of its own (``mrc-ps-release``), so its
    attempt is over exactly when that thread is gone - which is what a test
    that means to pin "the release was attempted and nothing was sent" has to
    wait for: the report alone can be written while the thread is still inside
    its own wait for the transport lock.
    """
    return {id(t) for t in threading.enumerate() if t.name == "mrc-ps-release"}


def _unreleased_reports(
    caplog: pytest.LogCaptureFixture, fake: _FakePsrpProtocol
) -> list[str]:
    """Release reports naming one of *fake*'s own pipelines.

    A release report is written on the release's own thread once its deadline
    has passed, so a caller that has just seen the invoke fail has to wait for
    it: only a message naming this fake's pipeline counts, never an unrelated
    warning that happened to land first. The pipeline id goes on the wire in
    upper case, hence the case-insensitive match.
    """
    ids = {value.lower() for value in fake.commands.values()}
    return [
        message
        for message in _reported(caplog)
        if "did not land" in message.lower()
        and any(command_id in message.lower() for command_id in ids)
    ]


class _PersistentPoolSession:
    """WinRM session whose persistent runspace is a real pypsrp pool."""

    def __init__(self, pool: RunspacePool) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = self.cwd
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.pool = pool

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> RunspacePool:
        return self.pool


def test_pool_adapter_releases_each_completed_pipeline() -> None:
    """A completed invoke deregisters its pipeline and releases it remotely.

    pypsrp registers each pipeline in ``pool.pipelines`` and only ``close()``
    deregisters it (and asks the server to release it), so a persistent ps
    session that never closes its pipelines accumulates one entry - script
    text, streams and output included - per invoke. The result extraction is
    unchanged, the pool stays open, and every invoke runs on the same runspace.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout=f"run-{index}") for index in range(3)
    ]
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    for index in range(3):
        result = adapter.invoke(f"Write-Output 'run-{index}'")

        assert result.stdout == f"run-{index}\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.location == location
        assert result.timed_out is False
        assert result.exit_probe_ran is True

        assert pool.pipelines == {}, "a completed pipeline stayed registered"
        _wait_for_settled_releases(fake, index + 1)
        assert (
            fake.signal_codes().count(SignalCode.TERMINATE) == index + 1
        ), "each completed pipeline is released exactly once"

    # One runspace for every invoke: the release never rebuilt the pool.
    assert fake.runspace_ids == [pool.id] * 3
    assert len(fake.commands) == 3
    assert pool.state != RunspacePoolState.CLOSED
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


def test_runspace_invoke_releases_pipelines_on_the_shared_runspace() -> None:
    """ps invoke: repeated calls share one pool and leave no pipeline behind.

    The registry is empty the moment each invoke returns; the remote release
    it launched is awaited here only so the count below is the final one.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="ps-0"),
        _completed_output(location, stdout="ps-1"),
    ]
    sess = _PersistentPoolSession(pool)
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is pool

    first = t.runspace_invoke(handle, "Write-Output 'ps-0'")
    assert (first.stdout, first.exit_code, first.location) == ("ps-0\n", 0, location)
    assert pool.pipelines == {}
    _wait_for_settled_releases(fake, 1)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 1

    second = t.runspace_invoke(handle, "Write-Output 'ps-1'")
    assert (second.stdout, second.exit_code, second.location) == ("ps-1\n", 0, location)
    assert pool.pipelines == {}
    _wait_for_settled_releases(fake, 2)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 2

    assert handle.inner is pool, "the handle must keep using the same pool"
    assert fake.runspace_ids == [pool.id, pool.id]
    assert t.is_connected() is True


def test_pool_adapter_releases_a_pipeline_whose_command_never_started() -> None:
    """A failed invoke leaves no registration behind and no broken pool.

    The Command round-trip is a pipeline's first request, and pypsrp registers
    the pipeline before it: when that request fails, the pipeline is
    ``NOT_STARTED`` - a state neither ``stop()`` nor ``close()`` releases - so
    the release boundary is the registration itself. No command id was ever
    learned, so nothing is signalled remotely and the pool stays usable.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="after-failure")]
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)
    fake.command_failures = 1

    with pytest.raises(RuntimeError, match="command rejected"):
        adapter.invoke("Write-Output 'never started'")

    assert pool.pipelines == {}, "a failed pipeline stayed registered"
    assert fake.signals == [], "nothing was addressed remotely to signal"

    # Bounded to that invoke: the same pool serves the next one.
    result = adapter.invoke("Write-Output after")
    assert result.stdout == "after-failure\n"
    assert result.exit_code == 0
    assert pool.pipelines == {}


def test_pool_adapter_release_is_bounded_when_cleanup_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup that never returns is abandoned off the caller's path.

    ``close()`` signals the server and only then deregisters the pipeline, so
    a peer that never answers that signal must not be able to pin an invoke
    whose result has already been extracted: the registration is dropped
    before the invoke returns and the bounded remote release is left to a
    daemon thread. With the release held open (and its deadline raised well
    above any plausible scheduling noise), the invoke returns at once - a
    release on the caller's path would burn that deadline - and the release
    still lands, once, when the peer answers.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 5.0)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="blocked-cleanup")]
    fake.terminate_gate = threading.Event()
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    t0 = time.monotonic()
    result = adapter.invoke("Write-Output blocked")
    elapsed = time.monotonic() - t0

    assert result.stdout == "blocked-cleanup\n"
    assert result.exit_code == 0
    assert result.location == location
    assert pool.pipelines == {}, "the registry kept the released pipeline"
    assert fake.terminate_started.wait(timeout=5.0), "the release was never attempted"
    assert (
        fake.terminates_settled == 0
    ), "the invoke waited for the blocked release to finish"
    assert elapsed < 2.0, f"the invoke waited for the blocked cleanup: {elapsed}s"

    # The abandoned release keeps going: it still lands, once.
    fake.terminate_gate.set()
    _wait_for_settled_releases(fake, 1)
    assert pool.pipelines == {}
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 1


def test_pool_adapter_late_stop_does_not_release_the_pipeline_again() -> None:
    """A stop arriving after the invoke released must not signal again.

    The other ordering of the same meeting: ``run()`` completes and releases
    its pipeline, and the wall-clock timeout callback fires afterwards - the
    transport calls it whenever the bridge reports the invoke as timed out,
    which can lag the thread that finished. The pipeline is held open in the
    release itself (the peer gates the TERMINATE), so the stop meets a
    pipeline that is being released rather than one already gone, and must
    leave it at exactly one release.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="late-stop")]
    receive_gate = threading.Event()
    fake.receive_gate = receive_gate
    fake.terminate_gate = threading.Event()
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    run, stop = adapter.prepare_invoke("Get-Date")
    finished: list[object] = []
    worker = threading.Thread(target=lambda: finished.append(run()), daemon=True)
    worker.start()
    try:
        assert fake.receive_entered.wait(timeout=5.0), "the invoke never reached the poll"
        receive_gate.set()
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert len(finished) == 1 and finished[0].stdout == "late-stop\n"

        # run()'s release is on the wire and still unanswered.
        assert fake.terminate_started.wait(timeout=5.0), "the release never started"
        stop()  # what runspace_invoke's wall-clock timeout calls
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
        assert pool.pipelines == {}, "the completed pipeline stayed registered"

        # The one release already claimed is the one that lands.
        fake.terminate_gate.set()
        _wait_for_settled_releases(fake, 1)
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
        assert pool.pipelines == {}
    finally:
        receive_gate.set()
        fake.terminate_gate.set()


def test_pool_adapter_timeout_release_is_not_repeated_by_the_abandoned_run() -> None:
    """One pipeline, one release - even when the timeout and run() both get there.

    A wall-clock timeout stops the pipeline while its ``run()`` is still inside
    the remote poll, so both sides can meet on the same pipeline: the stop
    releases it (stop + close) and the run() that finishes afterwards must not
    release it again. The poll is held open, so the two sides cannot interleave
    any other way.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="late")]
    receive_gate = threading.Event()
    fake.receive_gate = receive_gate
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    run, stop = adapter.prepare_invoke("Get-Date")
    finished: list[object] = []
    worker = threading.Thread(target=lambda: finished.append(run()), daemon=True)
    worker.start()
    try:
        assert fake.receive_entered.wait(timeout=5.0), "the invoke never reached the poll"

        stop()  # what runspace_invoke's wall-clock timeout calls

        assert pool.pipelines == {}, "the stopped pipeline stayed registered"
        assert fake.signal_codes() == [SignalCode.PS_CTRL_C, SignalCode.TERMINATE]

        # The abandoned run() finishes on its own and must not release again.
        receive_gate.set()
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert fake.signal_codes() == [SignalCode.PS_CTRL_C, SignalCode.TERMINATE]
        assert pool.pipelines == {}
        assert len(finished) == 1, "the abandoned invoke never finished"
        assert finished[0].timed_out is False
    finally:
        receive_gate.set()


def test_pool_adapter_timeout_keeps_the_route_of_an_outstanding_command() -> None:
    """A timeout before the Command answered must not drop the late run's route.

    pypsrp registers a pipeline before its Command round-trip and reports
    ``NOT_STARTED`` until that response arrives, so a wall-clock timeout meets
    a pipeline that cannot prove the remote refused the command: the run is
    still on the wire and its Command may still be accepted. The registration
    is the route such a late response needs - the pool reads it to learn the
    pipeline's remote CommandId and to hand the pipeline its own messages - so
    the stop leaves it and the release this run still owes to the run, which
    consumes the late COMPLETED message under that CommandId, finishes with its
    own result, and releases the pipeline exactly once.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="late-command")]
    command_gate = threading.Event()
    fake.command_gate = command_gate
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    run, stop = adapter.prepare_invoke("Get-Date")
    finished: list[object] = []
    worker = threading.Thread(target=lambda: finished.append(run()), daemon=True)
    worker.start()
    try:
        assert fake.command_entered.wait(timeout=5.0), "the Command was never sent"

        stop()  # what runspace_invoke's wall-clock timeout calls

        # No CommandId was ever learned, so nothing can be addressed remotely -
        # and the run that is still awaiting its Command keeps its registration.
        assert fake.signal_codes() == [], "an unanswered Command was signalled"
        assert list(pool.pipelines) != [], (
            "the timeout dropped the pipeline its late Command must be routed to"
        )

        # The Command answers late: the run polls under the CommandId its
        # Command was given, consumes COMPLETED, and finishes on its own.
        command_gate.set()
        worker.join(timeout=10.0)
        assert not worker.is_alive(), "the late run never finished on its own"
        assert len(finished) == 1, "the late run returned no result"
        assert finished[0].stdout == "late-command\n"
        assert finished[0].timed_out is False
        remote_command_id = next(iter(fake.commands))
        assert fake.receive_command_ids == [remote_command_id], (
            "the late run polled without the CommandId its Command was given"
        )
        _wait_for_settled_releases(fake, 1)
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
        assert pool.pipelines == {}
    finally:
        command_gate.set()
        worker.join(timeout=10.0)


def test_pool_adapter_stop_before_the_command_keeps_the_release_for_the_run() -> None:
    """A stop that lands before the Command is sent must not consume the release.

    The timeout can arrive before the run has made its first request, so
    ``stop()`` meets a ``NOT_STARTED`` pipeline that is not even registered in
    the pool yet: there is no remote work to dispose of and no CommandId to
    address. Consuming the pipeline's single release there would leave the run
    that follows - which does register the pipeline, send the Command and
    finish it - with no way to release it, so the registration would stay in
    the pool for the pool's lifetime. The stop therefore leaves the release to
    the run, whose terminal cleanup deregisters the pipeline and signals it
    exactly once.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="pre-send")]
    adapter = PypsrpPoolRunspaceAdapter(pool, default_location=location)

    run, stop = adapter.prepare_invoke("Get-Date")
    # Deterministic pre-send: the run thread does not exist yet, so nothing can
    # be on the wire when the stop lands.
    stop()
    assert list(pool.pipelines) == [], "a pipeline was registered before it ran"

    finished: list[object] = []
    worker = threading.Thread(target=lambda: finished.append(run()), daemon=True)
    worker.start()
    try:
        worker.join(timeout=10.0)
        assert not worker.is_alive(), "the run never finished on its own"
        assert len(finished) == 1, "the run returned no result"
        assert finished[0].stdout == "pre-send\n"
        assert finished[0].timed_out is False
        assert pool.pipelines == {}, "the run's pipeline stayed registered"
        _wait_for_settled_releases(fake, 1)
        assert fake.signal_codes() == [SignalCode.TERMINATE]
    finally:
        worker.join(timeout=10.0)


def _transport_with_pool(
    pool: RunspacePool,
    *,
    location: str,
) -> tuple[WinRMTransport, PypsrpPoolRunspaceAdapter]:
    """A connected transport whose persistent runspace is *pool*, plus its handle.

    The handle is the one ``open_runspace`` hands out. The release of a
    completed pipeline takes the transport's serial lock for its exchange -
    that release runs after its invoke returned, so it has no caller's serial
    zone to inherit. ``open_runspace`` does not carry the lock to the handle,
    so it is attached here for the release tests below; once the transport
    supplies it, this assignment stands down and those tests exercise the
    production path unchanged.
    """
    sess = _PersistentPoolSession(pool)
    transport = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    transport.connect()
    handle = transport.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is pool
    if handle.op_lock is None:
        handle.op_lock = transport.op_lock
    return transport, handle


def test_pool_adapter_release_holds_the_transport_lock_against_a_peer_invoke() -> None:
    """A detached release exchanges under the transport's serial lock.

    Terminating a finished pipeline is WSMan traffic on the one session every
    operation of the transport shares, so it belongs to the same
    one-exchange-at-a-time serial zone as those operations. With the release's
    TERMINATE held on the wire, a peer invoke on the same transport must not
    reach its own exchange - while the invoke whose result was already
    extracted returns it regardless, without waiting for either.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="released"),
        _completed_output(location, stdout="peer"),
    ]
    fake.terminate_gate = threading.Event()
    transport, handle = _transport_with_pool(pool, location=location)

    peer: list[object] = []

    def _peer_invoke() -> None:
        peer.append(transport.runspace_invoke(handle, "Write-Output peer", timeout_s=None))

    worker: threading.Thread | None = None
    try:
        result = transport.runspace_invoke(handle, "Write-Output released", timeout_s=0.5)

        assert result.timed_out is False, "the release turned the result into a timeout"
        assert result.stdout == "released\n"
        assert result.exit_code == 0
        assert fake.terminate_started.wait(timeout=5.0), "the release was never attempted"

        fake.command_entered.clear()
        worker = threading.Thread(target=_peer_invoke, daemon=True)
        worker.start()
        assert not fake.command_entered.wait(timeout=0.5), (
            "a peer invoke exchanged while the release's TERMINATE was on the wire"
        )
        # One release, for the one pipeline whose invoke finished: the peer's
        # own pipeline has not even run yet.
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
        assert fake.commands_during_terminate == 0, "the peer exchange overlapped the release"
    finally:
        fake.terminate_gate.set()
        if worker is not None:
            worker.join(timeout=10.0)

    assert worker is not None and not worker.is_alive()
    assert peer and getattr(peer[0], "stdout", "") == "peer\n"
    # Each completed pipeline is released exactly once.
    _wait_for_settled_releases(fake, 2)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 2
    assert pool.pipelines == {}


def test_pool_adapter_release_waits_for_the_transport_lock() -> None:
    """A release does not exchange while a peer owns the serial zone.

    The same invariant from the other side: with the transport's serial lock
    held (a peer operation's own zone), the completed invoke's detached release
    queues on it instead of putting its TERMINATE on the wire, and the invoke
    itself has already returned its output - the wait for the lock is the
    release's alone.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="queued")]
    transport, handle = _transport_with_pool(pool, location=location)

    transport.op_lock.acquire()  # a peer operation owns the serial zone
    try:
        result = transport.runspace_invoke(handle, "Write-Output queued", timeout_s=0.5)

        assert result.timed_out is False
        assert result.stdout == "queued\n"
        assert not fake.terminate_started.wait(timeout=0.5), (
            "the release exchanged while a peer held the transport serial lock"
        )
    finally:
        transport.op_lock.release()

    assert fake.terminate_started.wait(timeout=5.0), "the queued release never landed"
    _wait_for_settled_releases(fake, 1)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
    assert pool.pipelines == {}


def test_open_runspace_hands_the_pool_adapter_the_transport_lock() -> None:
    """The transport supplies the serial lock with the handle it adapts.

    The adapter's release runs after its invoke returned, on no thread that
    holds a serial zone, so the lock has to arrive with the handle: only
    ``open_runspace`` can pass it, and a handle that arrives unlocked releases
    its TERMINATE while another operation is exchanging on the same session.
    """
    location = r"C:\Users\mock"
    pool, _fake = _pool_with_fake_protocol(location=location)
    sess = _PersistentPoolSession(pool)
    transport = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    transport.connect()

    handle = transport.open_runspace()

    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.op_lock is transport.op_lock


def test_pool_adapter_detached_release_serializes_its_own_interrupt() -> None:
    """A detached release's interrupt exchange runs under the serial lock too.

    A pipeline left RUNNING by a fault during the invoke is released by the
    completion path with a stop: PS_CTRL_C first, then the TERMINATE. Both are
    WSMan exchanges on the one session every operation of the transport shares,
    and this release runs on its own thread, so it takes the transport serial
    lock for the interrupt the same way it does for the release: with the
    signal held on the wire no other thread can hold the lock and no peer
    operation reaches its own exchange, and the release still lands exactly
    once when the peer answers.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="peer")]
    fake.receive_failures = 1  # the poll fails with the pipeline already RUNNING
    fake.ctrl_c_gate = threading.Event()
    transport, handle = _transport_with_pool(pool, location=location)

    peer: list[object] = []

    def _peer_invoke() -> None:
        peer.append(
            transport.runspace_invoke(handle, "Write-Output peer", timeout_s=None)
        )

    worker: threading.Thread | None = None
    try:
        with pytest.raises(TransportError):
            transport.runspace_invoke(handle, "Get-Date", timeout_s=None)

        assert fake.ctrl_c_started.wait(timeout=5.0), "the release never interrupted"
        free = transport.op_lock.acquire(timeout=0.5)
        if free:
            transport.op_lock.release()
        assert not free, (
            "the release interrupted outside the transport serial lock"
        )

        fake.command_entered.clear()
        worker = threading.Thread(target=_peer_invoke, daemon=True)
        worker.start()
        assert not fake.command_entered.wait(timeout=0.5), (
            "a peer invoke exchanged while the release's interrupt was on the wire"
        )
        assert fake.commands_during_terminate == 0
        assert fake.signal_codes() == [SignalCode.PS_CTRL_C], (
            "the interrupt is the RUNNING pipeline's first signal"
        )
        assert fake.terminates_settled == 0
    finally:
        fake.ctrl_c_gate.set()
        if worker is not None:
            worker.join(timeout=10.0)

    assert worker is not None and not worker.is_alive()
    assert peer and getattr(peer[0], "stdout", "") == "peer\n"
    # Both pipelines land their release exactly once: the RUNNING one through
    # stop + close, the peer's through the completion path.
    _wait_for_settled_releases(fake, 2)
    assert fake.signal_codes() == [
        SignalCode.PS_CTRL_C,
        SignalCode.TERMINATE,
        SignalCode.TERMINATE,
    ]
    assert pool.pipelines == {}


def test_pool_adapter_retries_a_release_that_lost_the_lock_race(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A release that lost the lock race is retried at the next serial zone.

    A completed invoke deregisters its pipeline and hands the remote release to
    its own thread, which takes the transport serial lock for the exchange. A
    peer operation that holds that lock past the release's own deadline leaves
    nothing sent - and the pipeline is already deregistered, so the attempt is
    all the local bookkeeping there was for the one release that pipeline still
    owes. The attempt ending must not end the responsibility: the completing
    invoke returns its result without waiting for any of it, and the release is
    retried at the next serial zone the transport enters, which sends the
    pipeline its TERMINATE exactly once - with no manual close anywhere.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="raced"),
        _completed_output(location, stdout="after"),
        _completed_output(location, stdout="later"),
    ]
    transport, handle = _transport_with_pool(pool, location=location)

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        # A peer operation owns the serial zone: this thread takes the lock and
        # its own invoke enters re-entrantly, so the release the invoke hands
        # off queues behind a lock that stays taken past the release deadline.
        transport.op_lock.acquire()
        try:
            before = _release_thread_ids()
            result = transport.runspace_invoke(
                handle, "Write-Output raced", timeout_s=0.5
            )

            assert result.timed_out is False
            assert result.stdout == "raced\n"
            assert result.exit_code == 0
            assert result.location == location
            assert pool.pipelines == {}, "the completed pipeline stayed registered"
            # The result is extracted and handed back before the release's own
            # deadline could pass: a release on this caller's path would report
            # the give-up here instead, and the result would wait for it.
            assert not _unreleased_reports(caplog, fake), (
                "the invoke waited for the release it had already handed off"
            )

            # The attempt is over only once its own thread is gone, and the
            # report it (or its caller) writes is the barrier for that. Holding
            # the lock until both are there pins the attempt: the release that
            # never started cannot be handed the lock afterwards and land after
            # all, which is what the retry below is for.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (
                _unreleased_reports(caplog, fake)
                and not (_release_thread_ids() - before)
            ):
                time.sleep(0.01)
            assert _unreleased_reports(caplog, fake), (
                "the release never gave up on the peer's serial zone"
            )
            assert not (_release_thread_ids() - before), (
                "the release attempt was still running when the peer's zone ended"
            )
            assert fake.signals == [], (
                "the release exchanged while the peer held the serial lock"
            )
        finally:
            transport.op_lock.release()

    first_selector = next(iter(fake.commands))

    def _first_terminates() -> list[str]:
        return [
            code for command_id, code in fake.signals if command_id == first_selector
        ]

    # The peer's zone is gone: the next runspace op is the serial zone the
    # retained release is retried under. Nothing else is done about it - no
    # manual close of the pipeline, no second release of its own.
    second = transport.runspace_invoke(handle, "Write-Output after", timeout_s=0.5)
    assert (second.stdout, second.exit_code) == ("after\n", 0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not _first_terminates():
        time.sleep(0.01)
    assert _first_terminates() == [SignalCode.TERMINATE], (
        "the release the peer's zone displaced was never retried: "
        f"{fake.signals}"
    )
    assert pool.pipelines == {}

    # Each pipeline is released exactly once: the retry sent the one release
    # the displaced pipeline owed, and a later invoke on the same runspace
    # neither repeats it nor disturbs the releases that follow.
    _wait_for_settled_releases(fake, 2)
    third = transport.runspace_invoke(handle, "Write-Output later", timeout_s=0.5)
    assert third.stdout == "later\n"
    _wait_for_settled_releases(fake, 3)
    assert _first_terminates() == [SignalCode.TERMINATE], fake.signals
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


def test_pool_adapter_retry_is_made_again_at_the_end_of_a_zone_that_outlasted_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retry that expires inside the next zone is made again at its end.

    The retained release is retried when the transport enters the next serial
    zone, and that attempt waits for the lock the zone holds: an invoke whose
    exchange outlasts the release deadline leaves the attempt with nothing
    taken, exactly like the first one. The invoke's completion is the other end
    of that same zone - where the lock is about to be released - so the retry
    starts again there and this time reaches the session. The invoke is pinned
    by its poll, so it cannot finish before the test answers it.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="raced"),
        _completed_output(location, stdout="slow"),
    ]
    receive_gate = threading.Event()
    transport, handle = _transport_with_pool(pool, location=location)

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        transport.op_lock.acquire()
        try:
            before = _release_thread_ids()
            result = transport.runspace_invoke(
                handle, "Write-Output raced", timeout_s=0.5
            )
            assert result.stdout == "raced\n"
            assert pool.pipelines == {}

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (
                _unreleased_reports(caplog, fake)
                and not (_release_thread_ids() - before)
            ):
                time.sleep(0.01)
            assert _unreleased_reports(caplog, fake), (
                "the release never gave up on the peer's serial zone"
            )
            assert not (_release_thread_ids() - before), (
                "the release attempt was still running when the peer's zone ended"
            )
        finally:
            transport.op_lock.release()

        first_selector = next(iter(fake.commands))
        first_pipeline_id = fake.commands[first_selector].lower()

        def _first_reports() -> list[str]:
            return [
                message
                for message in _reported(caplog)
                if "did not land" in message.lower()
                and first_pipeline_id in message.lower()
            ]

        reported = len(_first_reports())
        assert reported == 1, _first_reports()

        # The next invoke is answered only by this test, so its serial zone is
        # held past the release deadline: the retry started at the zone's entry
        # gives up inside the zone and is reported a second time, still without
        # a signal - and without releasing the pipeline it owes.
        fake.receive_gate = receive_gate
        second: list[object] = []
        worker = threading.Thread(
            target=lambda: second.append(
                transport.runspace_invoke(handle, "Write-Output slow", timeout_s=None)
            ),
            daemon=True,
        )
        worker.start()
        try:
            assert fake.receive_entered.wait(timeout=5.0), (
                "the invoke never reached its poll"
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and len(_first_reports()) <= reported:
                time.sleep(0.01)
            assert len(_first_reports()) > reported, (
                "the release was not retried inside the zone the transport entered"
            )
            assert fake.signals == [], (
                "a release exchanged while the invoke's zone held the lock"
            )
        finally:
            receive_gate.set()
            worker.join(timeout=10.0)

        assert not worker.is_alive(), "the pinned invoke never finished"
        assert second and getattr(second[0], "stdout", "") == "slow\n"

    # The pinned invoke's completion releases the lock, and the retry made at
    # that end of the zone is the one that reaches the session.
    _wait_for_settled_releases(fake, 2)
    assert [
        code for command_id, code in fake.signals if command_id == first_selector
    ] == [SignalCode.TERMINATE], fake.signals
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


def test_pool_adapter_release_reports_a_lock_it_cannot_take(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop the serial lock keeps off the session is dropped and reported.

    An operation holding the transport serial lock keeps the release off the
    session: exchanging anyway would interleave two operations on the one
    session, so the attempt is dropped - and the drop is reported, because a
    silent one would leave the remote pipeline's missing TERMINATE
    unexplained. This pipeline is RUNNING when the lock is taken (its poll
    failed), so what it owes is an interrupt, not a terminal pipeline's single
    release: nothing is kept for a later zone here. A pipeline that is terminal
    still owes exactly one release, and that one is kept - see
    test_pool_adapter_retries_a_release_that_lost_the_lock_race.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="held")]
    fake.receive_failures = 1  # the poll fails with the pipeline already RUNNING
    transport, handle = _transport_with_pool(pool, location=location)

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        transport.op_lock.acquire()  # a peer operation owns the serial zone
        try:
            with pytest.raises(TransportError):
                transport.runspace_invoke(handle, "Get-Date", timeout_s=None)

            # The release gives the lock up at its own deadline and reports.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not _unreleased_reports(caplog, fake):
                time.sleep(0.01)
        finally:
            transport.op_lock.release()

    assert fake.signals == [], "the release exchanged while the peer held the lock"
    assert fake.terminates_settled == 0, "the release landed without the lock"
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"
    # The report names the pipeline the peer's own id belongs to.
    unreleased = _unreleased_reports(caplog, fake)
    assert unreleased, [record.getMessage() for record in caplog.records]
    assert all("did not land" in message for message in unreleased), unreleased


# ---------------------------------------------------------------------------
# The retry's trigger is the transport's serial zone, whichever surface enters
# it: exec and fs share that zone with the runspace, so a release retained by a
# handle that is never invoked again is still drained by them.
# ---------------------------------------------------------------------------


class _CombinedSurfaceSession:
    """One session behind all three surfaces the transport serializes.

    A real WinRM session serves exec (oneshot ``execute_ps``), fs (the pypsrp
    file client's scripts) and the persistent runspace (``open_runspace``) over
    the one WSMan connection the transport serializes, so any of the three can
    be the operation that enters the serial zone next.
    """

    def __init__(self, pool: RunspacePool, location: str) -> None:
        self.cwd = location
        self.home = location
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.pool = pool
        self.ps_calls: list[str] = []

    def close(self) -> None:
        pass

    def open_runspace(self) -> RunspacePool:
        return self.pool

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        del environment
        self.ps_calls.append(script)
        if "ConvertTo-Json -Compress" in script:
            # The fs stat script: the pypsrp file client parses this answer.
            return json.dumps(
                {
                    "kind": "file",
                    "size": 4,
                    "mtime": "2024-01-01T00:00:00Z",
                    "mode": "Archive",
                }
            )
        # Oneshot exec: the exit probe marker the transport reads, so the run
        # reports the code it earned instead of "probe did not run".
        return ["exec-ok", f"{_EXIT_MARKER}0"]


def _transport_with_combined_surfaces(
    pool: RunspacePool,
    *,
    location: str,
) -> tuple[WinRMTransport, PypsrpPoolRunspaceAdapter, _CombinedSurfaceSession]:
    """A connected transport serving exec, fs and the runspace of *pool*."""
    sess = _CombinedSurfaceSession(pool, location)
    transport = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    transport.connect()
    handle = transport.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is pool
    return transport, handle, sess


def _retain_a_release_behind_the_serial_lock(
    transport: WinRMTransport,
    handle: PypsrpPoolRunspaceAdapter,
    pool: RunspacePool,
    fake: _FakePsrpProtocol,
    caplog: pytest.LogCaptureFixture,
    *,
    script: str,
    stdout: str,
) -> str:
    """Complete one invoke whose release loses the lock race, retaining it.

    The transport serial lock is held on this thread while the invoke runs
    (re-entrant), so the release the completed invoke hands off queues behind
    a lock that stays taken past the release deadline: nothing is sent, and
    the pipeline - already deregistered - keeps the one release it owes, held
    by the handle for a later serial zone. The ``mrc-ps-release`` thread ids
    are the barrier: the attempt is over exactly when its thread is gone, so
    the lock is not released before the responsibility is provably retained.
    Returns the wire selector of the pipeline that now owes its release.
    """
    known = set(fake.commands)
    transport.op_lock.acquire()  # a peer operation owns the serial zone
    try:
        before = _release_thread_ids()
        result = transport.runspace_invoke(handle, script, timeout_s=0.5)

        assert result.timed_out is False
        assert result.stdout == stdout
        assert pool.pipelines == {}, "the completed pipeline stayed registered"
        selectors = set(fake.commands) - known
        assert len(selectors) == 1, fake.commands
        selector = selectors.pop()

        def _reports() -> list[str]:
            """Reports naming this pipeline as one whose release never started.

            Only the lock-that-was-not-free outcome keeps the release: the
            report says so, and it is what makes the missing trigger a defect
            rather than a release that was already on the wire.
            """
            pipeline_id = fake.commands[selector].lower()
            return [
                message
                for message in _reported(caplog)
                if "did not land" in message.lower()
                and "not free within the release deadline" in message.lower()
                and pipeline_id in message.lower()
            ]

        def _terminates() -> list[str]:
            return [
                code
                for command_id, code in fake.signals
                if command_id == selector
            ]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (
            _reports() and not (_release_thread_ids() - before)
        ):
            time.sleep(0.01)
        assert _reports(), "the release never gave up on the peer's serial zone"
        assert not (_release_thread_ids() - before), (
            "the release attempt was still running when the peer's zone ended"
        )
        assert _terminates() == [], (
            "the release exchanged while the peer held the serial lock: "
            f"{fake.signals}"
        )
    finally:
        transport.op_lock.release()
    return selector


def _wait_for_pipeline_terminate(
    fake: _FakePsrpProtocol, selector: str, *, timeout_s: float = 5.0
) -> list[str]:
    """Wait until the pipeline behind *selector* has been signalled, and report.

    The wait is the test's barrier, not the assertion: a retry runs on its own
    thread, so the TERMINATE is compared against the expected list once it has
    been observed.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        codes = [code for command_id, code in fake.signals if command_id == selector]
        if codes:
            return codes
        time.sleep(0.01)
    return [code for command_id, code in fake.signals if command_id == selector]


def test_transport_serial_zone_retries_a_retained_release_of_a_dormant_handle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exec, fs and a sibling handle drain a release the original never retries.

    The retry of a release that lost the lock race belongs to the transport,
    not to the handle that owes it: the next serial zone the transport enters
    has to drain it, whichever surface or handle enters that zone. The handle
    below is never invoked again after any invoke that retained its release -
    a same-transport ``exec``, a same-transport fs call and an invoke on a
    second handle of the same transport are the only zones that follow, and
    each has to send the release it finds outstanding, exactly once, leaving
    nothing owed behind it.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="dormant"),
        _completed_output(location, stdout="dormant-again"),
        _completed_output(location, stdout="dormant-last"),
        _completed_output(location, stdout="sibling"),
    ]
    transport, handle, _sess = _transport_with_combined_surfaces(
        pool, location=location
    )
    fs = transport.open_fs()  # opened while the handle owes nothing
    sibling = transport.open_runspace()  # a second handle of the same transport
    assert isinstance(sibling, PypsrpPoolRunspaceAdapter)
    assert sibling is not handle

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        dormant = _retain_a_release_behind_the_serial_lock(
            transport,
            handle,
            pool,
            fake,
            caplog,
            script="Write-Output dormant",
            stdout="dormant\n",
        )

        # An exec on the same transport is the next serial zone, and the only
        # thing that can drain the retained release: the handle that owes it is
        # not invoked again anywhere below.
        exec_result = transport.run_command("Write-Output exec")
        assert (exec_result.exit_code, exec_result.stdout) == (0, "exec-ok\n")

        assert _wait_for_pipeline_terminate(fake, dormant) == [
            SignalCode.TERMINATE
        ], f"the exec's serial zone did not drain the retained release: {fake.signals}"
        assert handle.pending_release_count == 0

        # The same from the fs surface: a second retained release, this time
        # drained by an fs call entering the transport's serial zone.
        again = _retain_a_release_behind_the_serial_lock(
            transport,
            handle,
            pool,
            fake,
            caplog,
            script="Write-Output dormant-again",
            stdout="dormant-again\n",
        )

        stat = fs.stat(r"C:\temp\dormant.txt")
        assert stat["kind"] == "file"

        assert _wait_for_pipeline_terminate(fake, again) == [
            SignalCode.TERMINATE
        ], f"the fs call's serial zone did not drain the retained release: {fake.signals}"
        assert handle.pending_release_count == 0

        # And from a second handle of the same transport: a zone entered by an
        # invoke that owes nothing still drains the handle that does.
        last = _retain_a_release_behind_the_serial_lock(
            transport,
            handle,
            pool,
            fake,
            caplog,
            script="Write-Output dormant-last",
            stdout="dormant-last\n",
        )

        sibling_result = transport.runspace_invoke(
            sibling, "Write-Output sibling", timeout_s=0.5
        )
        assert sibling_result.stdout == "sibling\n"

        assert _wait_for_pipeline_terminate(fake, last) == [
            SignalCode.TERMINATE
        ], (
            "the sibling handle's serial zone did not drain the retained "
            f"release: {fake.signals}"
        )

    assert handle.pending_release_count == 0
    assert sibling.pending_release_count == 0
    _wait_for_settled_releases(fake, 4)
    # Each pipeline was released exactly once, and the ops that followed
    # neither repeated a release nor left one owing.
    for selector in (dormant, again, last):
        assert [
            code for command_id, code in fake.signals if command_id == selector
        ] == [SignalCode.TERMINATE], fake.signals
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


# ---------------------------------------------------------------------------
# The transport-wide retry is bounded, so a triggering zone that outlives that
# bound is drained by the zone ENDING.
# ---------------------------------------------------------------------------

_SLOW_ZONE_S = 0.9  # > the release deadline the test below shrinks to 0.3


class _SlowZoneSession(_CombinedSurfaceSession):
    """The combined-surface session, with a long exec and a long fs exchange.

    Both legs hold the transport serial zone for longer than the release
    deadline, which is what makes the zone's own entry-time retry give up
    inside the zone and the responsibility survive to the zone's end. The
    flags let the test turn the slow behaviour off again for the short ops
    that follow.
    """

    def __init__(self, pool: RunspacePool, location: str) -> None:
        super().__init__(pool, location)
        self.slow_exec = True
        self.slow_fs = True
        self.slow_exec_entered = threading.Event()
        self.slow_fs_entered = threading.Event()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        if "ConvertTo-Json -Compress" in script:
            if self.slow_fs:
                self.slow_fs_entered.set()
                time.sleep(_SLOW_ZONE_S)
        elif "SLOW-OP" in script and self.slow_exec:
            self.slow_exec_entered.set()
            time.sleep(_SLOW_ZONE_S)
        return super().execute_ps(script, environment=environment)


def _slow_zone_transport(
    pool: RunspacePool, *, location: str
) -> tuple[WinRMTransport, PypsrpPoolRunspaceAdapter, _SlowZoneSession]:
    """A connected transport whose exec and fs exchanges outlast the deadline."""
    sess = _SlowZoneSession(pool, location)
    transport = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    transport.connect()
    handle = transport.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is pool
    return transport, handle, sess


def test_a_zone_outlasting_the_release_deadline_drains_it_as_it_ends(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trigger longer than the release deadline still drains, at the zone end.

    The retry a zone entry schedules is bounded: it hands the release to a
    thread that waits no longer than the release deadline for the transport
    lock, and the operation that entered the zone holds that lock for its whole
    exchange. The entry-time attempt of an operation that outlives the deadline
    therefore sends nothing and the release is retained again - and the zone
    ending is the first moment that release can take the lock, so the retry has
    to run again there. Both legs below reach that state, with a long exec and
    with a long fs call, and assert on the wire that the zone which ended
    drained the release exactly once. The handle that owes it is never invoked
    again.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="dormant"),
        _completed_output(location, stdout="dormant-again"),
    ]
    transport, handle, sess = _slow_zone_transport(pool, location=location)
    fs = transport.open_fs()  # opened while the handle owes nothing
    selectors: list[str] = []

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        # Leg 1: the next serial zone is a long exec on the same transport.
        selectors.append(
            _retain_a_release_behind_the_serial_lock(
                transport,
                handle,
                pool,
                fake,
                caplog,
                script="Write-Output dormant",
                stdout="dormant\n",
            )
        )
        started = time.monotonic()
        exec_result = transport.run_command("SLOW-OP")
        exec_seconds = time.monotonic() - started
        assert (exec_result.exit_code, exec_result.stdout) == (0, "exec-ok\n")
        assert sess.slow_exec_entered.is_set(), "the slow exec never ran"
        assert exec_seconds > runspace_mod._STOP_DEADLINE_S, (
            "the exec did not outlast the release deadline it has to outlast"
        )
        sess.slow_exec = False  # later ops are short again

        assert _wait_for_pipeline_terminate(fake, selectors[-1]) == [
            SignalCode.TERMINATE
        ], (
            "the zone that outlasted the release deadline did not drain the "
            f"retained release as it ended: {fake.signals}"
        )
        assert handle.pending_release_count == 0

        # Leg 2: the same from the fs surface, whose exchange is also long.
        selectors.append(
            _retain_a_release_behind_the_serial_lock(
                transport,
                handle,
                pool,
                fake,
                caplog,
                script="Write-Output dormant-again",
                stdout="dormant-again\n",
            )
        )
        started = time.monotonic()
        stat = fs.stat(r"C:\temp\dormant.txt")
        fs_seconds = time.monotonic() - started
        assert stat["kind"] == "file"
        assert sess.slow_fs_entered.is_set(), "the slow fs call never ran"
        assert fs_seconds > runspace_mod._STOP_DEADLINE_S, (
            "the fs call did not outlast the release deadline it has to outlast"
        )
        sess.slow_fs = False

        assert _wait_for_pipeline_terminate(fake, selectors[-1]) == [
            SignalCode.TERMINATE
        ], (
            "the zone that outlasted the release deadline did not drain the "
            f"retained release as it ended: {fake.signals}"
        )
        assert handle.pending_release_count == 0

    # Neither drain was repeated: ops on the same transport after the zone that
    # drained each release leave exactly one TERMINATE per pipeline, nothing
    # owing, and the pool shell untouched.
    assert transport.run_command("Write-Output after").exit_code == 0
    assert fs.stat(r"C:\temp\after.txt")["kind"] == "file"
    _wait_for_settled_releases(fake, 2)
    for selector in selectors:
        assert [
            code for command_id, code in fake.signals if command_id == selector
        ] == [SignalCode.TERMINATE], fake.signals
    assert handle.pending_release_count == 0
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


# ---------------------------------------------------------------------------
# A session retired locally while a release is still owed keeps its drain: the
# registration is the only route a later serial zone has to that release, and
# the retirement must not take it away. A retirement that owes nothing has to
# take it away, or the transport holds the retired adapter and its pool for the
# rest of its life and every later zone walks an empty drain.
# ---------------------------------------------------------------------------


def _zone_hook_count(transport: WinRMTransport) -> int:
    """How many callables the transport's serial-zone registry holds.

    The registry's list is the strong reference a retired handle must not stay
    in: it holds the pool adapter, and the adapter holds its pool.
    """
    return len(transport.serial_zone_hooks._hooks)


def _pipeline_signals(fake: _FakePsrpProtocol, selector: str) -> list[str]:
    """Signal codes the peer received for one pipeline, in order."""
    return [code for command_id, code in fake.signals if command_id == selector]


def _wait_for_release_threads(before: set[int], *, timeout_s: float = 5.0) -> None:
    """Wait until the release threads started after *before* are gone.

    A release runs on its own daemon thread, so "the retry is over" is only
    observable once that thread has ended: the wait is the test's barrier.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and (_release_thread_ids() - before):
        time.sleep(0.01)
    assert not (_release_thread_ids() - before), "the release attempt was still running"


def test_a_retired_session_that_owes_a_release_is_still_drained_by_a_later_zone(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``abandon`` keeps the registration while a release is still owed.

    The session is retired locally, so the retirement itself sends nothing -
    what it must leave in place is the registration, because the transport's
    serial zones are the only thing that can land that release once the
    session is gone. A later same-transport zone sends it exactly once, and
    the drain then takes the registration back by itself: the retired adapter,
    its pool and the remote runspace's client-side state stop being held as
    soon as nothing is owed.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod
    from mcp_remote_control.ps.session import PsSession

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="owed")]
    transport, handle, _sess = _transport_with_combined_surfaces(
        pool, location=location
    )
    session = PsSession(id="ps_retired", ep="lab-win", handle=handle, transport=transport)
    ref = weakref.ref(handle)

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        owed = _retain_a_release_behind_the_serial_lock(
            transport,
            handle,
            pool,
            fake,
            caplog,
            script="Write-Output owed",
            stdout="owed\n",
        )
        assert handle.pending_release_count == 1
        before = _release_thread_ids()

        session.abandon()

        assert session.handle is None and session.transport is None
        assert handle.pending_release_count == 1, (
            "the retirement dropped the release the pipeline still owes"
        )
        assert _zone_hook_count(transport) == 1, (
            "the retirement took away the registration the pending release "
            "needs to be landed by a later zone"
        )
        assert _pipeline_signals(fake, owed) == [], (
            "the retirement sent the owed release itself"
        )

        exec_result = transport.run_command("Write-Output after")
        assert (exec_result.exit_code, exec_result.stdout) == (0, "exec-ok\n")

        assert _wait_for_pipeline_terminate(fake, owed) == [SignalCode.TERMINATE], (
            "the zone did not land the release the retired session owed: "
            f"{fake.signals}"
        )
        assert handle.pending_release_count == 0
        _wait_for_release_threads(before)

    assert _zone_hook_count(transport) == 0, (
        "the retired adapter is still held by the transport after it stopped "
        "owing anything"
    )
    # The zone that follows neither repeats the release nor puts the
    # registration back.
    assert transport.run_command("Write-Output later").exit_code == 0
    assert _pipeline_signals(fake, owed) == [SignalCode.TERMINATE], fake.signals
    assert _zone_hook_count(transport) == 0

    del handle
    gc.collect()
    assert ref() is None, (
        "the retired adapter is still strongly referenced after its release landed"
    )
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


def test_a_close_that_missed_its_lock_keeps_the_owed_release_drainable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close whose lock wait missed still leaves the owed release drainable.

    The teardown never starts on that path (the in-flight operation keeps the
    serial lock), so the local revocation the close performs is all that runs:
    it takes the registration back only when nothing is owed. With a release
    still pending, the registration survives, and the next serial zone -
    an exec that owes nothing itself - sends that release exactly once.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    import mcp_remote_control.transport.winrm_runspace as runspace_mod
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import _CLOSE_TIMEOUT

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.05)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="owed")]
    transport, handle, _sess = _transport_with_combined_surfaces(
        pool, location=location
    )
    session = PsSession(
        id="ps_retired_close", ep="lab-win", handle=handle, transport=transport
    )
    ref = weakref.ref(handle)

    with caplog.at_level(
        logging.WARNING,
        logger="mcp_remote_control.transport.winrm_runspace",
    ):
        owed = _retain_a_release_behind_the_serial_lock(
            transport,
            handle,
            pool,
            fake,
            caplog,
            script="Write-Output owed",
            stdout="owed\n",
        )
        assert handle.pending_release_count == 1
        before = _release_thread_ids()

        held = threading.Event()
        release = threading.Event()

        def _hold_the_serial_zone() -> None:
            with transport.op_lock:
                held.set()
                release.wait(timeout=30.0)

        holder = threading.Thread(
            target=_hold_the_serial_zone, name="ps-serial-zone-peer", daemon=True
        )
        try:
            holder.start()
            assert held.wait(timeout=10.0), "the peer never took the serial zone"
            session.close()
        finally:
            release.set()
            holder.join(timeout=10.0)
        assert not holder.is_alive()

        assert session.close_verdict == _CLOSE_TIMEOUT, session.close_verdict
        assert handle.pending_release_count == 1, (
            "the retirement dropped the release the pipeline still owes"
        )
        assert _zone_hook_count(transport) == 1, (
            "the retirement took away the registration the pending release "
            "needs to be landed by a later zone"
        )
        assert _pipeline_signals(fake, owed) == [], (
            "the retirement sent the owed release itself"
        )

        assert transport.run_command("Write-Output after").exit_code == 0

        assert _wait_for_pipeline_terminate(fake, owed) == [SignalCode.TERMINATE], (
            "the zone did not land the release the retired session owed: "
            f"{fake.signals}"
        )
        assert handle.pending_release_count == 0
        _wait_for_release_threads(before)

    assert _zone_hook_count(transport) == 0, (
        "the retired adapter is still held by the transport after it stopped "
        "owing anything"
    )
    del handle
    gc.collect()
    assert ref() is None, (
        "the retired adapter is still strongly referenced after its release landed"
    )
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


class _GatedZoneHookRegistry(SerialZoneHooks):
    """The transport's registry with a one-shot pause inside ``add``.

    The pause is the test's barrier, not a change to the code under test: it
    lands inside the registration a retention makes, which is the window a
    concurrent ``close()`` can run in. Every registration still reaches the
    real registry, and every discard is recorded, so a test can observe a
    close that got to the registry before the registration did.
    """

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.discarded = threading.Event()

    def add(self, hook: Any) -> None:
        if self.armed:
            self.armed = False
            self.entered.set()
            self.resume.wait(timeout=10.0)
        super().add(hook)

    def discard(self, hook: Any) -> None:
        super().discard(hook)
        self.discarded.set()


def _wait_for_a_release_attempt(before: set[int], *, timeout_s: float = 5.0) -> None:
    """Wait until a release thread started after *before* is running.

    The release runs on its own daemon thread, so an attempt queued behind the
    serial lock - before its bounded wait for that lock elapses - is
    observable: the wait is the test's barrier.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not (_release_thread_ids() - before):
        time.sleep(0.005)
    assert _release_thread_ids() - before, "the release attempt never started"


def _close_while_the_registration_is_gated(
    handle: Any,
    registry: _GatedZoneHookRegistry,
    *,
    timeout_s: float = 10.0,
) -> None:
    """Close *handle* while the registry's gate holds a registration open.

    The close runs on a worker thread because it may have to wait for the
    handle's retention lock, which the gated registration holds. Its first
    visit to the registry is the barrier: a close that is not serialized
    behind that lock gets there while the registration is still open - it
    discards a drain that is not registered yet - and the gate is released as
    soon as that visit is observed, so the registration that follows is
    ordered after it. A close that is serialized behind the lock cannot get
    there first, and the wait elapses instead.
    """
    registry.discarded.clear()  # only the close under test may set it from here
    worker = threading.Thread(target=handle.close, name="ps-close-race", daemon=True)
    worker.start()
    registry.discarded.wait(timeout=1.0)
    registry.resume.set()
    worker.join(timeout=timeout_s)
    assert not worker.is_alive(), "the close did not return"


def test_a_retention_racing_a_close_cannot_re_register_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retention ordered against a close leaves no registration behind.

    A retained release puts the drain back on the transport's serial-zone
    registry, and ``close()`` leaves that registry for good. If the
    registration is made outside the lock the close holds while it leaves, a
    close landing in that window discards a drain that is not registered yet,
    forgets the registry, and the registration that follows lands on the
    transport afterwards - where nothing can take it back, because the handle
    no longer knows the registry. The transport then holds the closed adapter
    and its pool for the life of the transport, and every later serial zone
    walks the dead drain.

    The registration itself is the barrier: the close runs while the retention
    is inside it. What must hold afterwards is that the transport holds
    nothing, a later zone is not asked to walk the drain, and the closed
    adapter is collectable.
    """
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 1.0)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="race\n")]
    transport = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: _CombinedSurfaceSession(pool, location),
    )
    transport.connect()
    registry = _GatedZoneHookRegistry()
    transport._serial_zone_hooks = registry
    handle = transport.open_runspace()
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert _zone_hook_count(transport) == 1
    ref = weakref.ref(handle)

    before = _release_thread_ids()
    registry.armed = True
    transport.op_lock.acquire()  # the completed invoke's release queues here
    try:
        result = transport.runspace_invoke(handle, "Write-Output race", timeout_s=2.0)
        assert result.exit_code == 0, result
        _wait_for_a_release_attempt(before)
        # Retire locally while the release attempt is still queued behind this
        # thread's lock: that attempt retains itself, and the retention is what
        # puts the drain back on the registry.
        handle.detach()
        assert _zone_hook_count(transport) == 0, (
            "the retirement did not take the drain back"
        )
        assert registry.entered.wait(timeout=10.0), (
            "the retention never reached the registry"
        )
        # The close reaches the registry here and finds no drain yet whenever
        # the registration is not serialized on the lock the close holds while
        # it leaves the registry; with that serialization the gate's release
        # waits for the registration to finish instead.
        _close_while_the_registration_is_gated(handle, registry)
    finally:
        registry.resume.set()
        transport.op_lock.release()
        _wait_for_release_threads(before)

    assert _zone_hook_count(transport) == 0, (
        "a retention that raced the close left the closed adapter registered "
        "with the transport"
    )
    assert handle.pending_release_count == 1, (
        "the release retained during the close is still the handle's to carry"
    )
    assert transport.run_command("Write-Output after").exit_code == 0
    assert _zone_hook_count(transport) == 0, (
        "a later serial zone was asked to walk the dead drain"
    )
    # A closed handle is never bound again, so a release arriving after the
    # close has no registry to put the drain back on.
    handle._bind_serial_zone_hooks(registry)
    assert _zone_hook_count(transport) == 0, (
        "a re-adaptation bound a closed handle to a live registry"
    )
    handle._retain_pending_release(object())
    assert _zone_hook_count(transport) == 0, (
        "a release retained after the close put the drain back on the registry"
    )
    del handle
    gc.collect()
    assert ref() is None, (
        "the closed adapter is still strongly referenced after the close"
    )

