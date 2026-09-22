"""Service tests: WinRM mark_dead, hard timeout, op/read."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pypsrp.powershell import RunspacePoolState
from pypsrp.shell import SignalCode

from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.winrm import WinRMTransport, _EXIT_MARKER
from mcp_remote_control.transport.winrm_timeouts import PYPSRP_HTTP_TIMEOUT_SLACK_S

# Real-pypsrp pool + fake protocol layer, shared with the runspace tests.
from test_winrm_runspace import (
    _PersistentPoolSession,
    _completed_output,
    _pool_with_fake_protocol,
    _wait_for_settled_releases,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# Mock session helpers
# ---------------------------------------------------------------------------

class _MockWinRMSession:
    """Injectable session: no network sockets."""

    def __init__(
        self,
        *,
        cwd: str = r"C:\Users\Administrator",
        home: str = r"C:\Users\Administrator",
        os_name: str = "windows",
        shell: str = "powershell",
        ps_version: str = "5.1.19041",
        probe_partial: bool = False,
    ) -> None:
        self.cwd = cwd
        self.home = home
        self.os = os_name
        self.shell = shell
        self.ps_version = ps_version
        if probe_partial:
            self.probe_status = "partial"
            self.probe_error = "mock probe partial"
        self.closed = False
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            exit_code=0,
            stdout=f"winrm-out:{command}\n",
            stderr="",
            cwd=cwd or self.cwd,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.argvs.append(list(argv))
        return ExecResult(
            exit_code=0,
            stdout=" ".join(argv) + "\n",
            cwd=cwd or self.cwd,
        )


class _BlockingPsSession:
    """execute_ps that blocks to simulate a hanging remote PowerShell."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.scripts: list[str] = []
        self.block = threading.Event()

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        # Simulate a hung remote call; released by the test after asserting.
        self.block.wait(timeout=30.0)
        return ("", None, False)


# ---------------------------------------------------------------------------
# mark_dead / is_alive / hard-timeout -> ensure reconnect (MaxShells)
# ---------------------------------------------------------------------------

def test_winrm_mark_dead_and_reconnect_closes_prior() -> None:
    """mark_dead -> connect opens a new session and best-effort closes old.

    Mirrors SSH reconnect: MaxShellsPerUser requires prior session.close
    so zombie shells do not stack on ensure/open reopen.
    """
    opens = {"n": 0}
    closes = {"n": 0}
    sessions: list[_MockWinRMSession] = []

    def connector(**_kw: object) -> _MockWinRMSession:
        opens["n"] += 1
        sess = _MockWinRMSession()
        # Track close on the mock (AdaptedWinRMSession.close -> raw.close).
        orig_close = sess.close

        def _close() -> None:
            closes["n"] += 1
            orig_close()

        sess.close = _close  # type: ignore[method-assign]
        sessions.append(sess)
        return sess

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=connector,
    )
    t.connect()
    assert t.is_connected() is True
    assert t.is_alive() is True
    assert opens["n"] == 1
    first = t.session
    assert first is not None

    t.mark_dead("peer_reset")
    assert t.is_connected() is False
    assert t.is_alive() is False
    assert t.meta.get("dead_reason") == "peer_reset"
    # Session retained until reconnect so dispose can close it.
    assert t.session is first

    t.connect()
    assert opens["n"] == 2
    assert t.is_connected() is True
    assert t.is_alive() is True
    assert closes["n"] >= 1, "reconnect must close prior session (MaxShells)"
    assert t.session is not first
    # Idempotent while live: no extra connect/close.
    live = t.session
    t.connect()
    assert opens["n"] == 2
    assert t.session is live


def test_winrm_hard_timeout_marks_dead() -> None:
    """Wall-clock oneshot timeout marks dead AND disposes the session.

    MaxShells lever: best-effort session.close on the hard-timeout path
    (not only on the next connect). Remote PS is not claimed Stopped.
    """
    sess = _BlockingPsSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    assert t.is_alive() is True
    assert closes["n"] == 0

    r = t.run_command("Read-Host hang", timeout_s=0.5)
    assert r.timed_out is True
    assert r.exit_code == -1
    assert t.is_alive() is False
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "hard timeout"
    # Hard-timeout path must dispose immediately (close count >=1).
    assert closes["n"] >= 1, "hard timeout must close the WinRM session"
    assert sess.closed is True
    assert t.session is None
    assert t.meta.get("session_disposed") is True
    # Machine-readable meta tokens for Core/Agent (not prose alone).
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    # Honest: do not pretend remote pipeline was Stopped.
    assert "session closed" in (r.stderr or "").lower()
    assert "not guaranteed" in (r.stderr or "").lower()
    # Next exec must fail fast with NOT_CONNECTED until reconnect.
    with pytest.raises(TransportError) as ei:
        t.run_command("Get-Date")
    assert ei.value.code == "NOT_CONNECTED"
    sess.block.set()


def test_winrm_hard_timeout_next_connect_reopens() -> None:
    """After hard-timeout dispose, connect opens a fresh session cleanly."""
    opens = {"n": 0}
    closes = {"n": 0}
    hang = {"on": True}

    class _Sess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\mock"
            self.home = r"C:\Users\mock"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.block = threading.Event()

        def close(self) -> None:
            closes["n"] += 1
            self.closed = True
            self.block.set()

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment
            if hang["on"]:
                self.block.wait(timeout=30.0)
                return ("", None, False)
            return (f"ok\n{_EXIT_MARKER}0\n", None, False)

    def connector(**_kw: object) -> _Sess:
        opens["n"] += 1
        return _Sess()

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=connector,
    )
    t.connect()
    assert opens["n"] == 1
    assert closes["n"] == 0

    r = t.run_command("Start-Sleep 30", timeout_s=0.5)
    assert r.timed_out is True
    assert closes["n"] >= 1
    assert t.is_connected() is False
    assert t.session is None

    hang["on"] = False
    t.connect()
    assert opens["n"] == 2
    assert t.is_connected() is True
    r2 = t.run_command("whoami")
    assert r2.timed_out is False
    assert r2.exit_code == 0
    assert "ok" in (r2.stdout or "")
    # Successful path must not extra-close the live session.
    closes_after_ok = closes["n"]
    r3 = t.run_command("whoami")
    assert r3.exit_code == 0
    assert closes["n"] == closes_after_ok


def test_winrm_success_path_does_not_dispose() -> None:
    """Non-timeout success must not dispose the WinRM session without cause."""
    closes = {"n": 0}

    class _Sess(_MockWinRMSession):
        def close(self) -> None:
            closes["n"] += 1
            super().close()

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment
            return ("done\n", None, False)

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: _Sess(),
    )
    t.connect()
    assert closes["n"] == 0
    live = t.session
    r = t.run_command("Get-Date")
    assert r.timed_out is False
    assert r.exit_code == 0
    assert closes["n"] == 0
    assert t.session is live
    assert t.is_connected() is True
    assert t.meta.get("session_disposed") is not True
    # Success path must not inject timeout dispose noise into meta.
    assert t.meta.get("marked_dead") is not True
    assert t.meta.get("reopen_hint") is None


def test_winrm_hard_timeout_meta_tokens_stable() -> None:
    """Hard-timeout meta exposes marked_dead/session_disposed/reopen_hint."""
    sess = _BlockingPsSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    r = t.run_command("Start-Sleep 30", timeout_s=0.4)
    assert r.timed_out is True
    assert r.exit_code == -1
    meta = t.meta or {}
    assert meta.get("marked_dead") is True
    assert meta.get("session_disposed") is True
    assert meta.get("dead_reason") == "hard timeout"
    assert meta.get("reopen_hint") == "endpoint close then open"
    sess.block.set()


def test_winrm_ensure_connected_after_mark_dead_reconnects() -> None:
    """After session death, ensure_connected triggers a new connect."""
    connect_calls = 0
    closed: list[_MockWinRMSession] = []

    def connector(**_kw: object) -> _MockWinRMSession:
        nonlocal connect_calls
        connect_calls += 1
        sess = _MockWinRMSession()
        orig_close = sess.close

        def _close() -> None:
            closed.append(sess)
            orig_close()

        sess.close = _close  # type: ignore[method-assign]
        return sess

    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES, probe=False)
    assert ep.connected is True
    assert connect_calls == 1
    assert ep.transport is not None
    first_transport = ep.transport

    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark), "WinRMTransport must expose mark_dead"
    mark("session_invalidated")
    assert ep.transport.is_connected() is False
    # Stale Endpoint.connected cache (connected flag not cleared with mark_dead).
    ep.connected = True

    ep2 = reg.ensure_connected("lab-win", home=FIXTURES, probe=False)
    assert ep2.connected is True
    assert connect_calls == 2, (
        f"ensure must reconnect after mark_dead, connect_calls={connect_calls}"
    )
    assert ep2.transport is not None
    assert ep2.transport.is_connected() is True
    # Stale transport was closed (registry pop + close) so MaxShells do not stack.
    assert closed, "prior WinRM session must be closed on ensure reopen"
    # Live ensure is idempotent.
    ep3 = reg.ensure_connected("lab-win", home=FIXTURES, probe=False)
    assert connect_calls == 2
    assert ep3.transport is ep2.transport
    assert first_transport is not ep2.transport


def test_winrm_maxshells_docs_present() -> None:
    """MaxShells risk and close/reopen responsibility are documented."""
    import mcp_remote_control.transport.winrm as winrm_mod

    mod_doc = winrm_mod.__doc__ or ""
    assert "MaxShellsPerUser" in mod_doc
    assert "mark_dead" in mod_doc
    call_doc = WinRMTransport._call_remote.__doc__ or ""
    assert "MaxShellsPerUser" in call_doc
    mark_doc = WinRMTransport.mark_dead.__doc__ or ""
    assert "MaxShellsPerUser" in mark_doc or "session" in mark_doc.lower()


def test_call_remote_no_env_mode_or_inner_timeout() -> None:
    """_call_remote drops dead env_mode + inner timeout branch.

    Hard-timeout stays on outer _map_call_to_exec; oneshot still passes
    environment= when env is set (covered by native-env tests).
    """
    import inspect

    sig = inspect.signature(WinRMTransport._call_remote)
    assert "env_mode" not in sig.parameters
    assert "timeout_s" not in sig.parameters
    assert "env" in sig.parameters
    # No del env_mode residue in the method body.
    src = inspect.getsource(WinRMTransport._call_remote)
    assert "env_mode" not in src
    assert "del env_mode" not in src


# ---------------------------------------------------------------------------
# operation_timeout / read_timeout aligned with call timeout_s
# ---------------------------------------------------------------------------

class _FakeWsmanTransport:
    def __init__(self, read_timeout: int = 30) -> None:
        self.read_timeout = read_timeout


class _FakeWsman:
    def __init__(
        self, *, operation_timeout: int = 20, read_timeout: int = 30
    ) -> None:
        self.operation_timeout = operation_timeout
        self.transport = _FakeWsmanTransport(read_timeout=read_timeout)


class _WsmanCaptureSession:
    """execute_ps session that records live wsman op/read during the call."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.wsman = _FakeWsman()
        self.captured: list[tuple[int, int]] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment, script
        self.captured.append(
            (
                int(self.wsman.operation_timeout),
                int(self.wsman.transport.read_timeout),
            )
        )
        from mcp_remote_control.transport.winrm import _EXIT_MARKER

        return (f"ok\n{_EXIT_MARKER}0\n", None, False)


def test_winrm_timeout_s_5_applies_op_read_ge_5() -> None:
    """run_command(timeout_s=5) carries >=5s op/read on the Client/wsman path."""
    sess = _WsmanCaptureSession()
    # Library defaults before call.
    assert sess.wsman.operation_timeout == 20
    assert sess.wsman.transport.read_timeout == 30

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    r = t.run_command("whoami", timeout_s=5)
    assert r.timed_out is False
    assert r.exit_code == 0
    assert sess.captured, "execute_ps must run under applied timeouts"
    op, rd = sess.captured[0]
    assert op >= 5
    assert rd >= 5
    assert op == 5
    # HTTP read timeout must outlast the WSMan operation timeout.
    assert rd == 5 + PYPSRP_HTTP_TIMEOUT_SLACK_S
    # Transport records last applied (mock-assertable without live wsman).
    assert t._last_applied_operation_timeout == 5  # noqa: SLF001
    assert t._last_applied_read_timeout == 5 + PYPSRP_HTTP_TIMEOUT_SLACK_S  # noqa: SLF001
    # Restored after call so unlimited follow-ups keep connect-time defaults.
    assert sess.wsman.operation_timeout == 20
    assert sess.wsman.transport.read_timeout == 30


def test_winrm_timeout_s_ceil_fractional() -> None:
    """Fractional timeout_s uses whole-second ceil for op/read."""
    sess = _WsmanCaptureSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    t.run_command("whoami", timeout_s=5.2)
    assert sess.captured[0] == (6, 6 + PYPSRP_HTTP_TIMEOUT_SLACK_S)
    assert t._last_applied_operation_timeout == 6  # noqa: SLF001
    assert t._last_applied_read_timeout == 6 + PYPSRP_HTTP_TIMEOUT_SLACK_S  # noqa: SLF001


def test_winrm_profile_op_read_override_derivation() -> None:
    """Profile explicit operation_timeout_s/read_timeout_s win over timeout_s.

    The pair is ordered (the invariant caps an op above the read; see
    test_winrm_timeouts_invariant), so no value here is re-derived from the
    call's 5s budget.
    """
    sess = _WsmanCaptureSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        operation_timeout_s=60,
        read_timeout_s=88,
        connector=lambda **_k: sess,
    )
    t.connect()
    # Connect kwargs carry profile values (Client path).
    kw = t.connect_kwargs()
    assert kw["operation_timeout"] == 60
    assert kw["read_timeout"] == 88
    t.run_command("whoami", timeout_s=5)
    assert sess.captured[0] == (60, 88)
    assert t._last_applied_operation_timeout == 60  # noqa: SLF001
    assert t._last_applied_read_timeout == 88  # noqa: SLF001


def test_winrm_no_timeout_does_not_force_short_op_read() -> None:
    """timeout_s omitted -> leave library/session defaults (not forced short)."""
    sess = _WsmanCaptureSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    r = t.run_command("whoami")  # no timeout_s
    assert r.exit_code == 0
    assert sess.captured, "call must still run"
    # Without derivation, wsman is left at its prior values (20/30).
    assert sess.captured[0] == (20, 30)
    assert t._last_applied_operation_timeout is None  # noqa: SLF001
    assert t._last_applied_read_timeout is None  # noqa: SLF001
    # connect kwargs must not inject tiny op/read when profile omits them.
    kw = t.connect_kwargs()
    assert "operation_timeout" not in kw
    assert "read_timeout" not in kw


def test_winrm_connector_receives_profile_op_read_kwargs() -> None:
    """Connector/Client kwargs include profile op/read at connect."""
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> _WsmanCaptureSession:
        seen.update(kwargs)
        return _WsmanCaptureSession()

    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        operation_timeout_s=45,
        read_timeout_s=50,
        connector=connector,
    )
    t.connect()
    assert seen.get("operation_timeout") == 45
    assert seen.get("read_timeout") == 50
    # connect_timeout still from connect_timeout_ms only (default 15s).
    assert seen.get("connect_timeout") == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Hung session.close must not pin dispose / _op_lock
# ---------------------------------------------------------------------------


class _HungCloseSession(_MockWinRMSession):
    """session.close blocks; dispose must abandon after the close budget."""

    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        self.close_release.wait(timeout=30.0)
        self.closed = True


def test_dispose_hung_session_close_returns_inside_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung session.close must not pin close/mark_dead dispose under _op_lock."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_SESSION_CLOSE_TIMEOUT_S", 0.2)
    sess = _HungCloseSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    assert t.is_connected() is True

    t.mark_dead("peer_reset")
    assert t.is_connected() is False

    t0 = time.monotonic()
    t.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"dispose hung on session.close: {elapsed:.2f}s"
    assert t.session is None
    assert t.is_connected() is False
    assert sess.close_entered.wait(timeout=1.0)

    acquired = {"ok": False}

    def _try_lock() -> None:
        if t._op_lock.acquire(timeout=0.4):  # noqa: SLF001
            acquired["ok"] = True
            t._op_lock.release()  # noqa: SLF001

    th = threading.Thread(target=_try_lock, name="winrm-dispose-lock")
    th.start()
    th.join(timeout=1.0)
    assert acquired["ok"] is True, "hung session.close must not pin _op_lock"
    sess.close_release.set()


def test_reconnect_after_mark_dead_hung_close_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect after mark_dead abandons a hung prior session.close."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_SESSION_CLOSE_TIMEOUT_S", 0.2)
    sessions: list[_HungCloseSession | _MockWinRMSession] = []

    def connector(**_kw: object) -> _HungCloseSession | _MockWinRMSession:
        if not sessions:
            sess = _HungCloseSession()
            sessions.append(sess)
            return sess
        nxt = _MockWinRMSession()
        sessions.append(nxt)
        return nxt

    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=connector,
    )
    t.connect()
    first = t.session
    t.mark_dead("peer_reset")

    t0 = time.monotonic()
    t.connect()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"reconnect dispose hung: {elapsed:.2f}s"
    assert t.is_connected() is True
    assert t.session is not first
    acquired = t._op_lock.acquire(timeout=0.4)  # noqa: SLF001
    assert acquired is True
    t._op_lock.release()  # noqa: SLF001
    first_raw = sessions[0]
    assert isinstance(first_raw, _HungCloseSession)
    first_raw.close_release.set()


# ---------------------------------------------------------------------------
# Runspace timeout: the pipeline is released once, the pool stays usable
# ---------------------------------------------------------------------------


def test_runspace_invoke_timeout_releases_pipeline_and_keeps_pool_usable() -> None:
    """A timed-out invoke releases its pipeline and leaves the pool usable.

    The wall-clock timeout stops the in-flight pipeline while its invoke is
    still inside the remote poll (pinned here: the poll cannot answer before
    the test releases it, so the two sides cannot interleave differently). The
    pipeline is signalled once and gone from the pool's registry before
    ``runspace_invoke`` returns, the runspace is not torn down, and a later
    invoke still runs on it.

    The once-only boundary itself - the abandoned invoke finishing afterwards
    must not release that pipeline a second time - is pinned in
    ``test_pool_adapter_timeout_release_is_not_repeated_by_the_abandoned_run``.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="timed-out-run"),
        _completed_output(location, stdout="after-timeout"),
    ]
    receive_gate = threading.Event()
    fake.receive_gate = receive_gate
    sess = _PersistentPoolSession(pool)
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()

    try:
        t0 = time.monotonic()
        result = t.runspace_invoke(handle, "Get-Date", timeout_s=0.5)
        elapsed = time.monotonic() - t0

        assert result.timed_out is True
        assert result.exit_code == -1
        assert fake.receive_entered.is_set(), "the invoke never reached the poll"
        assert pool.pipelines == {}, "the timed-out pipeline stayed registered"
        # Bounded by the timeout plus the stop deadline, not by the poll.
        assert elapsed < 6.0, f"runspace_invoke waited for the blocked poll: {elapsed}s"

        codes = fake.signal_codes()
        assert codes.count(SignalCode.PS_CTRL_C) == 1, codes
        assert codes.count(SignalCode.TERMINATE) == 1, codes

        # Same handle, same pool: the timeout release kept the runspace usable
        # while the abandoned invoke is still inside its poll.
        second = t.runspace_invoke(handle, "Write-Output after", timeout_s=None)
        assert (second.stdout, second.exit_code, second.location) == (
            "after-timeout\n",
            0,
            location,
        )
        assert pool.pipelines == {}
        _wait_for_settled_releases(fake, 2)
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 2
        assert fake.runspace_ids == [pool.id, pool.id]
        assert fake.deletes == 0, "the runspace pool shell must not be torn down"
        assert pool.state != RunspacePoolState.CLOSED
    finally:
        # Let the abandoned poll answer rather than leaving it blocked.
        receive_gate.set()


def test_runspace_invoke_timeout_while_the_command_is_outstanding_keeps_the_route() -> None:
    """A timed-out invoke whose Command is unanswered keeps the route it needs.

    An invoke's first WSMan message is the ``Command`` that carries the script,
    and pypsrp reports the pipeline ``NOT_STARTED`` until that response arrives
    - a state that cannot prove the remote refused it. The wall-clock timeout
    therefore ends the caller's wait and leaves the run that is still on the
    wire alone: the late Command answer is routed back through the pipeline's
    registration (its remote CommandId is what the polls are addressed with),
    the run consumes COMPLETED and exits by itself, and the pipeline is
    released exactly once. The pool stays usable for a later invoke.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [
        _completed_output(location, stdout="late-command"),
        _completed_output(location, stdout="after-timeout"),
    ]
    command_gate = threading.Event()
    fake.command_gate = command_gate
    sess = _PersistentPoolSession(pool)
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()

    results: list[object] = []
    worker = threading.Thread(
        target=lambda: results.append(
            t.runspace_invoke(handle, "Get-Date", timeout_s=0.5)
        ),
        daemon=True,
    )
    try:
        worker.start()
        assert fake.command_entered.wait(timeout=5.0), "the Command was never sent"
        t0 = time.monotonic()
        worker.join(timeout=15.0)
        elapsed = time.monotonic() - t0

        assert not worker.is_alive(), "runspace_invoke outlasted its own timeout"
        # Bounded by the timeout plus the deferred stop, not by the Command.
        assert elapsed < 5.0, f"runspace_invoke waited for the unanswered Command: {elapsed}s"
        assert len(results) == 1 and results[0].timed_out is True
        assert fake.signal_codes() == [], "an unanswered Command was signalled"
        # The run still awaiting its Command keeps the registration that routes
        # its late Command answer back to it.
        assert list(pool.pipelines) != [], (
            "the timeout dropped the pipeline its late Command must be routed to"
        )

        # The Command answers late: the run polls under the CommandId its
        # Command was given, finishes by itself and releases once.
        command_gate.set()
        _wait_for_settled_releases(fake, 1)
        remote_command_id = next(iter(fake.commands))
        assert fake.receive_command_ids == [remote_command_id], (
            "the late run polled without the CommandId its Command was given"
        )
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
        assert pool.pipelines == {}

        # Same handle, same pool: the deferred release kept the runspace usable.
        second = t.runspace_invoke(handle, "Write-Output after", timeout_s=None)
        assert (second.stdout, second.exit_code, second.location) == (
            "after-timeout\n",
            0,
            location,
        )
        assert pool.pipelines == {}
        _wait_for_settled_releases(fake, 2)
        assert fake.signal_codes().count(SignalCode.TERMINATE) == 2
        assert fake.runspace_ids == [pool.id, pool.id]
        assert fake.deletes == 0, "the runspace pool shell must not be torn down"
        assert pool.state != RunspacePoolState.CLOSED
    finally:
        # Answer the held Command rather than leaving the run blocked on it.
        command_gate.set()
        worker.join(timeout=10.0)


def test_runspace_invoke_result_survives_a_stalled_pipeline_release() -> None:
    """A stalled release must not turn a completed invoke into a timeout.

    The release is the invoke's last step and it waits on a WSMan exchange the
    peer can stall, so it runs off the caller's wall-clock path: the result has
    already been extracted when the release starts, and ``runspace_invoke``
    returns it whether or not the peer answers. The poll answers immediately
    here and only the release is held open - the case that a release on the
    caller's path reports as ``timed_out`` with the output discarded.
    """
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="completed-run")]
    fake.terminate_gate = threading.Event()
    sess = _PersistentPoolSession(pool)
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()

    try:
        result = t.runspace_invoke(handle, "Write-Output completed", timeout_s=0.5)

        assert result.timed_out is False
        assert result.stdout == "completed-run\n"
        assert result.exit_code == 0
        assert result.had_errors is False
        assert result.location == location
        assert pool.pipelines == {}, "the release left the pipeline registered"
        assert fake.terminate_started.wait(timeout=5.0), "the release was never attempted"
    finally:
        # Answer the held release rather than leaving it on the wire.
        fake.terminate_gate.set()

    _wait_for_settled_releases(fake, 1)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"


def test_runspace_timeout_reports_a_stalled_interrupt_and_still_releases(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop stalled past the deadline is reported, and the release still lands.

    The interrupt (PS_CTRL_C) of a timed-out pipeline can itself be stalled by
    the peer. pypsrp refuses to close a pipeline that is still STOPPING, so
    while that signal is on the wire there is no release: the pipeline stays in
    ``pool.pipelines`` and never receives its TERMINATE. That state is reported
    with the pipeline id instead of being swallowed, and the release is
    re-attempted once the interrupt answers - exactly once, just later.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    import mcp_remote_control.transport.winrm_runspace as runspace_mod

    # The release's own deadline is shorter than the caller's wait for the stop
    # callback, so the report below is emitted (on the stop's own thread)
    # before runspace_invoke returns rather than racing its abandon.
    monkeypatch.setattr(runspace_mod, "_STOP_DEADLINE_S", 0.3)
    monkeypatch.setattr(winrm_mod, "_STOP_DEADLINE_S", 1.5)
    location = r"C:\Users\mock"
    pool, fake = _pool_with_fake_protocol(location=location)
    fake.responses = [_completed_output(location, stdout="never")]
    receive_gate = threading.Event()
    fake.receive_gate = receive_gate
    ctrl_c_gate = threading.Event()
    fake.ctrl_c_gate = ctrl_c_gate
    sess = _PersistentPoolSession(pool)
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    handle = t.open_runspace()

    reported: list[str] = []
    registered: list[str] = []
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="mcp_remote_control.transport.winrm_runspace",
        ):
            result = t.runspace_invoke(handle, "Get-Date", timeout_s=0.4)

            assert result.timed_out is True
            assert fake.ctrl_c_started.wait(timeout=5.0), "the interrupt was never attempted"
            # The signal is stuck on the wire: nothing has released the
            # pipeline yet, so it is still the pool's - and that is reported.
            registered = list(pool.pipelines)
            assert len(registered) == 1, pool.pipelines
            assert fake.signal_codes().count(SignalCode.TERMINATE) == 0
            reported = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING
            ]
    finally:
        ctrl_c_gate.set()
        receive_gate.set()

    assert any(
        registered and registered[0] in message and "release" in message
        for message in reported
    ), reported
    # The interrupt answered: the release is re-attempted then, and lands once.
    _wait_for_settled_releases(fake, 1)
    assert fake.signal_codes().count(SignalCode.TERMINATE) == 1
    assert pool.pipelines == {}
    assert fake.deletes == 0, "the runspace pool shell must not be torn down"
