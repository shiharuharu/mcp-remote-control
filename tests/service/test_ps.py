"""Service tests: ps open / invoke / close on WinRM (T14, mock only)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import ps_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.ps import get_ps_registry, reset_ps_registry
from mcp_remote_control.ps.mock import MockWinRMSessionWithRunspace
from mcp_remote_control.transport import TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
FAKE_PASSWORD = "dummy-winrm-password"


def _ok_connector(**kwargs: object) -> MockWinRMSessionWithRunspace:
    assert "host" in kwargs
    assert "username" in kwargs
    return MockWinRMSessionWithRunspace(cwd=r"C:\Users\mock")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_ps_registry()
    yield
    reset_ps_registry()
    reset_registry()


def _open_ps(**kwargs: object):
    defaults: dict = {
        "ep": "lab-win",
        "home": FIXTURES,
        "connector": _ok_connector,
    }
    defaults.update(kwargs)
    return ps_ops.open_session(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# shared runspace state (mock)
# ---------------------------------------------------------------------------


def test_open_returns_session_id_and_cwd() -> None:
    r = _open_ps()
    assert r.status == "ok"
    assert r.kind == "ps"
    sid = r.fields.get("id")
    assert sid and str(sid).startswith("ps_")
    assert r.fields.get("session_id") == sid
    assert r.fields.get("ep") == "lab-win"
    assert r.cwd is not None
    assert "Users" in r.cwd or r.cwd.startswith("C:")
    text = r.render_text()
    assert text.startswith("@ps ok")
    assert f"id={sid}" in text
    assert "cwd=" in text
    assert FAKE_PASSWORD not in text


def test_invoke_shares_variable_state() -> None:
    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]

    set_r = ps_ops.invoke(id=sid, script="$x=1")
    assert set_r.status == "ok"
    assert set_r.fields.get("exit") == 0

    get_r = ps_ops.invoke(id=sid, script="$x")
    assert get_r.status == "ok"
    assert get_r.body is not None
    assert "1" in get_r.body
    assert get_r.cwd is not None
    text = get_r.render_text()
    assert text.startswith("@ps ok")
    assert "cwd=" in text


def test_invoke_updates_location_cwd() -> None:
    opened = _open_ps()
    sid = opened.fields["id"]
    target = r"C:\Users\mock\work"

    r = ps_ops.invoke(id=sid, script=f"Set-Location '{target}'")
    assert r.status == "ok"
    assert r.cwd == target
    text = r.render_text()
    assert f"cwd={target}" in text or "cwd=C:\\Users\\mock\\work" in text

    # Subsequent invoke still at new location
    r2 = ps_ops.invoke(id=sid, script="(Get-Location).Path")
    assert r2.status == "ok"
    assert r2.cwd == target
    assert r2.body is not None
    assert "work" in r2.body


def test_close_then_invoke_fails() -> None:
    opened = _open_ps()
    sid = opened.fields["id"]

    closed = ps_ops.close_session(id=sid)
    assert closed.status == "ok"
    assert closed.fields.get("closed") is True

    again = ps_ops.invoke(id=sid, script="$x")
    assert again.status == "error"
    assert again.code == "PS_NOT_FOUND"
    assert "not open" in (again.fields.get("msg") or "").lower()


def test_close_twice_not_found() -> None:
    opened = _open_ps()
    sid = opened.fields["id"]
    assert ps_ops.close_session(id=sid).status == "ok"
    r = ps_ops.close_session(id=sid)
    assert r.status == "error"
    assert r.code == "PS_NOT_FOUND"


# ---------------------------------------------------------------------------
# local / ssh UNSUPPORTED
# ---------------------------------------------------------------------------


def test_local_ps_open_unsupported() -> None:
    r = ps_ops.open_session(ep="local", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    text = r.render_text()
    assert "UNSUPPORTED" in text
    assert "ps" in text.lower() or "winrm" in text.lower()
    # no session registered
    assert len(get_ps_registry()) == 0


def test_ssh_ps_open_unsupported() -> None:
    """ssh caps.ps=false → UNSUPPORTED (mock connect, no network)."""
    from mcp_remote_control.transport.base import ExecResult

    class _FakeSsh:
        cwd = "/home/lab"
        home = "/home/lab"

        def close(self) -> None:
            return None

        def run_command(self, command: str, **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout="ok\n", cwd=self.cwd)

        def run_argv(self, argv: list[str], **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout=" ".join(argv) + "\n", cwd=self.cwd)

    def _ssh_conn(**_kw: object) -> _FakeSsh:
        return _FakeSsh()

    r = ps_ops.open_session(ep="lab-ssh", home=FIXTURES, connector=_ssh_conn)
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("transport") == "ssh"
    text = r.render_text()
    assert "UNSUPPORTED" in text


def test_ps_run_dispatch() -> None:
    opened = ps_ops.run(
        "open",
        ep="lab-win",
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert opened.status == "ok"
    sid = opened.fields["id"]
    inv = ps_ops.run("invoke", id=sid, script="$y=42")
    assert inv.status == "ok"
    inv2 = ps_ops.run("invoke", id=sid, script="$y")
    assert inv2.status == "ok"
    assert inv2.body and "42" in inv2.body
    closed = ps_ops.run("close", id=sid)
    assert closed.status == "ok"


def test_missing_args() -> None:
    r = ps_ops.open_session(home=FIXTURES)
    assert r.code == "MISSING_ARG"
    r2 = ps_ops.invoke(script="$x")
    assert r2.code == "MISSING_ARG"
    r3 = ps_ops.invoke(id="ps_01")
    assert r3.code in ("MISSING_ARG", "PS_NOT_FOUND")
    r4 = ps_ops.close_session()
    assert r4.code == "MISSING_ARG"


def test_invalid_op() -> None:
    r = ps_ops.run("explode")
    assert r.status == "error"
    assert r.code == "INVALID_OP"


# ---------------------------------------------------------------------------
# CLI + endpoint teardown
# ---------------------------------------------------------------------------


def test_cli_ps_open_invoke_close(capsys: pytest.CaptureFixture[str]) -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]

    code = main(["ps", "open", "--ep", "lab-win", "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out.strip())
    assert data["status"] == "ok"
    assert data["kind"] == "ps"
    sid = data.get("id") or data.get("session_id")
    assert sid

    code = main(["ps", "invoke", "--id", sid, "--script", "$z=7", "--json"])
    assert code == EXIT_OK
    inv = json.loads(capsys.readouterr().out.strip())
    assert inv["status"] == "ok"
    assert "cwd" in inv

    code = main(["ps", "invoke", "--id", sid, "--script", "$z", "--json"])
    assert code == EXIT_OK
    inv2 = json.loads(capsys.readouterr().out.strip())
    assert inv2["status"] == "ok"
    assert inv2.get("body") and "7" in inv2["body"]

    code = main(["ps", "close", "--id", sid, "--json"])
    assert code == EXIT_OK
    closed = json.loads(capsys.readouterr().out.strip())
    assert closed["status"] == "ok"

    code = main(["ps", "invoke", "--id", sid, "--script", "$z"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "PS_NOT_FOUND" in out or "not open" in out.lower()


def test_cli_local_ps_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ps", "open", "--ep", "local"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@ps" in out
    assert "UNSUPPORTED" in out


def test_endpoint_close_tears_down_ps() -> None:
    from mcp_remote_control.core import endpoint_ops

    opened = _open_ps()
    sid = opened.fields["id"]
    assert get_ps_registry().get(sid) is not None

    closed = endpoint_ops.run(op="close", ep="lab-win")
    assert closed.status == "ok"
    assert closed.fields.get("ps_closed") == 1
    assert get_ps_registry().get(sid) is None


def test_no_network_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure mock path never opens a real socket."""
    import socket

    def _blocked(*_a: object, **_k: object) -> None:
        raise AssertionError("network socket used in ps mock test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)

    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    assert ps_ops.invoke(id=sid, script="$n=99").status == "ok"
    got = ps_ops.invoke(id=sid, script="$n")
    assert got.body and "99" in got.body
    assert ps_ops.close_session(id=sid).status == "ok"


# ---------------------------------------------------------------------------
# O7: runspace_invoke timeout + location-probe fold + dead-session prune.
# ---------------------------------------------------------------------------


class _FakeStreams:
    """Minimal stand-in for pypsrp PSDataStreams (error stream)."""

    def __init__(self) -> None:
        self.error: list[object] = []


class _FakePowerShell:
    """Fake pypsrp PowerShell for runspace_invoke real-path tests.

    Records invoke() calls so the location-probe fold can be asserted, and
    blocks on ``Read-Host`` scripts until ``stop()`` releases it (so the
    timeout path disposes the pipeline without closing the runspace pool).
    """

    instances: list[_FakePowerShell] = []

    def __init__(self, pool: _RunspacePoolFake) -> None:
        self.pool = pool
        self.script: str | None = None
        self.invoke_count = 0
        self.stopped = False
        self.closed = False
        self.had_errors = False
        self.streams = _FakeStreams()
        self._block = threading.Event()
        _FakePowerShell.instances.append(self)

    def add_script(self, script: str, use_local_scope: object = None) -> _FakePowerShell:
        self.script = script
        return self

    def invoke(self, input: object = None, **_kw: object) -> list[object]:
        self.invoke_count += 1
        if self.script and "Read-Host" in self.script:
            # Simulate a hanging pipeline until stop() releases us.
            self._block.wait(timeout=30.0)
            return []
        loc = getattr(self.pool, "location", r"C:\Users\mock")
        # Canned output: a user line + the sentinel-tagged location probe.
        return ["user-out", f"__MRC_PS_CWD_MARKER__{loc}"]

    def stop(self) -> None:
        self.stopped = True
        self._block.set()

    def close(self) -> None:
        self.closed = True


class _RunspacePoolFake:
    """Pool-like handle without ``invoke`` so open_runspace wraps it in
    ``PypsrpPoolRunspaceAdapter`` (real PowerShell pipeline path: timeout +
    folded location probe).
    """

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.location = location
        self.closed = False

    def open(self) -> None:  # pragma: no cover - transport calls session.open_runspace
        return None

    def close(self) -> None:
        self.closed = True


class _PypsrpSession:
    """Mock WinRM session that returns a RunspacePool-named handle."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.cwd = location
        self.home = location
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.pool: _RunspacePoolFake | None = None

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> _RunspacePoolFake:
        self.pool = _RunspacePoolFake(location=self.cwd)
        return self.pool


def test_ps_invoke_timeout_hang_returns_timed_out_and_runspace_reusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O7 finding 1: a hanging Read-Host with timeout returns timed_out within
    ~the timeout, and the runspace is still usable afterward."""
    _FakePowerShell.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _FakePowerShell)

    sess = _PypsrpSession()

    def conn(**_kw: object) -> _PypsrpSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok"
    sid = opened.fields["id"]

    t0 = time.monotonic()
    r = ps_ops.invoke(id=sid, script="Read-Host 'hang'", timeout=1.0)
    elapsed = time.monotonic() - t0

    # Timed-out: status fail, surfaced in fields, within ~the timeout.
    assert r.status == "fail"
    assert r.fields.get("timed_out") is True
    assert r.fields.get("exit") == -1
    assert elapsed < 3.0

    # The pipeline was stopped/disposed but the runspace pool stays open.
    assert sess.pool is not None
    assert not sess.pool.closed, "runspace pool must NOT be closed on timeout"
    assert _FakePowerShell.instances, "a pipeline was constructed"
    assert _FakePowerShell.instances[-1].stopped is True, "pipeline.stop() was called"

    # The runspace is still usable: a second invoke succeeds.
    r2 = ps_ops.invoke(id=sid, script="$x = 1")
    assert r2.status == "ok"
    assert r2.fields.get("exit") == 0


def test_ps_invoke_location_probe_folded_into_single_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O7 finding 7: runspace_invoke runs the location probe in the SAME
    pipeline as the user script — PowerShell.invoke() called ONCE, not twice."""
    _FakePowerShell.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _FakePowerShell)

    sess = _PypsrpSession()

    def conn(**_kw: object) -> _PypsrpSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok"
    sid = opened.fields["id"]

    r = ps_ops.invoke(id=sid, script="$x = 1")
    assert r.status == "ok"

    # Exactly ONE invoke() call per ps invoke (the probe is folded in, no
    # separate Get-Location round-trip).
    assert _FakePowerShell.instances, "a pipeline was constructed"
    total = sum(p.invoke_count for p in _FakePowerShell.instances)
    assert total == 1, f"expected 1 invoke (probe folded), got {total}"

    # Location parsed from the sentinel-marked probe output.
    assert r.cwd == r"C:\Users\mock"
    # The marker must NOT leak into the rendered stdout; user output preserved.
    assert r.body is not None
    assert "user-out" in r.body
    assert "__MRC_PS_CWD_MARKER__" not in r.body


class _DeadInvokeRunspace:
    """Runspace handle whose invoke() raises NOT_CONNECTED (session dropped)."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.location = location
        self.closed = False

    def invoke(self, script: str) -> object:
        raise TransportError("NOT_CONNECTED", "winrm session dropped")

    def close(self) -> None:
        self.closed = True


class _DeadSession:
    """WinRM session returning a dead runspace handle."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.handle: _DeadInvokeRunspace | None = None

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> _DeadInvokeRunspace:
        self.handle = _DeadInvokeRunspace(location=self.cwd)
        return self.handle


def test_ps_dead_session_pruned_after_not_connected() -> None:
    """O7 finding 4: after a NOT_CONNECTED TransportError on invoke, the dead
    session is pruned from the registry; the next invoke does NOT reuse it."""
    sess = _DeadSession()

    def conn(**_kw: object) -> _DeadSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok"
    sid = opened.fields["id"]
    assert get_ps_registry().get(sid) is not None

    r = ps_ops.invoke(id=sid, script="$x")
    assert r.status == "error"
    assert r.code == "NOT_CONNECTED"

    # Pruned from the registry (the dead handle is not reused).
    assert get_ps_registry().get(sid) is None
    # The runspace handle was best-effort closed by the prune.
    assert sess.handle is not None
    assert sess.handle.closed is True

    # Next invoke does NOT reuse the dead handle — PS_NOT_FOUND now.
    r2 = ps_ops.invoke(id=sid, script="$x")
    assert r2.status == "error"
    assert r2.code == "PS_NOT_FOUND"


class _SlowStopFakePowerShell(_FakePowerShell):
    """Fake PowerShell whose stop() releases the hung invoke but then blocks
    well past the stop deadline — verifies _safe_stop_pipeline is bounded."""

    def stop(self) -> None:
        self.stopped = True
        # Release the hung invoke so its executor thread finishes.
        self._block.set()
        # Then block past the _STOP_DEADLINE_S to prove cleanup is bounded.
        time.sleep(5.0)


def test_ps_invoke_stop_blocks_still_returns_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O7 review LOW #3: when pipeline.stop() blocks, runspace_invoke still
    returns within ~timeout + stop deadline (does NOT wait for stop() to
    finish)."""
    _FakePowerShell.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _SlowStopFakePowerShell)

    sess = _PypsrpSession()

    def conn(**_kw: object) -> _PypsrpSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok"
    sid = opened.fields["id"]

    t0 = time.monotonic()
    r = ps_ops.invoke(id=sid, script="Read-Host 'hang'", timeout=1.0)
    elapsed = time.monotonic() - t0

    assert r.fields.get("timed_out") is True
    # Even though stop() sleeps 5s, runspace_invoke returns within ~timeout
    # (1s) + stop deadline (2s) ≈ 3s — NOT 5s+.
    assert elapsed < 4.5, f"runspace_invoke hung past the stop deadline: {elapsed}s"
    # stop() was at least initiated (the daemon thread sets stopped=True early).
    assert _FakePowerShell.instances[-1].stopped is True

# ---------------------------------------------------------------------------
# WinRM PS capability gate: ps_runspace (notes/025 §八)
# ---------------------------------------------------------------------------


def test_ps_open_unsupported_when_ps_runspace_false() -> None:
    """ps open with transport.meta winrm_ps.ps_runspace=false → UNSUPPORTED."""
    from mcp_remote_control.endpoint import ensure_endpoint

    ep = ensure_endpoint("lab-win", home=FIXTURES, connector=_ok_connector)
    assert ep.transport is not None
    ep.transport.meta["winrm_ps"] = {
        "ps_version": "5.1.19041",
        "language_mode": "ConstrainedLanguage",
        "ps_script_fs": False,
        "ps_oneshot": True,
        "ps_runspace": False,
    }

    r = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=_ok_connector)
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("lang_mode") == "ConstrainedLanguage"
    assert "ps_runspace" in (r.fields.get("msg") or "") or "runspace" in (
        r.fields.get("msg") or ""
    ).lower()
    assert len(get_ps_registry()) == 0
    text = r.render_text()
    assert "UNSUPPORTED" in text

