"""Service tests: ps open / invoke / close on WinRM (mock only)."""

from __future__ import annotations

import gc
import json
import logging
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import ps_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.ps import get_ps_registry, reset_ps_registry
from mcp_remote_control.ps.mock import MockRunspace, MockWinRMSessionWithRunspace
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.winrm_exec import _EXIT_MARKER

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

    # Snapshot transport.cwd before Set-Location (exec/fs default work path).
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    transport_cwd_before = ep.transport.cwd

    r = ps_ops.invoke(id=sid, script=f"Set-Location '{target}'")
    assert r.status == "ok"
    assert r.cwd == target
    text = r.render_text()
    assert f"cwd={target}" in text or "cwd=C:\\Users\\mock\\work" in text

    # Subsequent invoke still at new location (per-runspace / session tracking).
    r2 = ps_ops.invoke(id=sid, script="(Get-Location).Path")
    assert r2.status == "ok"
    assert r2.cwd == target
    assert r2.body is not None
    assert "work" in r2.body

    # ps Set-Location must not pollute transport.cwd used by exec/fs.
    assert ep.transport.cwd == transport_cwd_before
    assert ep.transport.cwd != target


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
    # PS_NOT_FOUND hint explains process-local sessions (not a daemon bug).
    hint = (again.hint or "").lower()
    assert "process-local" in hint or "process local" in hint
    assert "same process" in hint or "one process" in hint or "\u540c\u8fdb\u7a0b" in (again.hint or "")


def test_close_twice_not_found() -> None:
    opened = _open_ps()
    sid = opened.fields["id"]
    assert ps_ops.close_session(id=sid).status == "ok"
    r = ps_ops.close_session(id=sid)
    assert r.status == "error"
    assert r.code == "PS_NOT_FOUND"
    # close PS_NOT_FOUND also carries the process-local hint.
    hint = (r.hint or "").lower()
    assert "process-local" in hint or "process local" in hint


def test_ps_not_found_hint_process_local() -> None:
    """Unknown session id -> PS_NOT_FOUND with process-local hint text."""
    r = ps_ops.invoke(id="ps_does_not_exist", script="$x=1")
    assert r.status == "error"
    assert r.code == "PS_NOT_FOUND"
    hint = r.hint or ""
    lower = hint.lower()
    assert "process-local" in lower or "process local" in lower
    assert (
        "same process" in lower
        or "one process" in lower
        or "\u540c\u8fdb\u7a0b" in hint
    )
    assert "ps open" in lower or "open --ep" in lower
    # Agent track / render must surface the cue (not only the OpResult field).
    text = r.render_text().lower()
    assert "process-local" in text or "process local" in text
    assert "ps_not_found" in text or "ps-not-found" in text


# ---------------------------------------------------------------------------
# local / ssh CAP_DENIED (caps.ps=false)
# ---------------------------------------------------------------------------


def test_local_ps_open_cap_denied() -> None:
    r = ps_ops.open_session(ep="local", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "CAP_DENIED"
    text = r.render_text()
    assert "CAP_DENIED" in text
    assert "lacks ps capability" in (r.fields.get("msg") or "").lower()
    assert "ps" in text.lower() or "winrm" in text.lower()
    # no session registered
    assert get_ps_registry().list_open() == []


def test_ssh_ps_open_cap_denied() -> None:
    """ssh caps.ps=false -> CAP_DENIED (mock connect, no network)."""
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
    assert r.code == "CAP_DENIED"
    assert r.fields.get("transport") == "ssh"
    assert "lacks ps capability" in (r.fields.get("msg") or "").lower()
    text = r.render_text()
    assert "CAP_DENIED" in text


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


def test_cli_ps_invoke_forwards_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ``ps invoke --timeout`` reaches Core ps_ops.run."""
    captured: dict[str, object] = {}

    def _capture_run(op: str, **kwargs: object) -> object:
        captured["op"] = op
        captured.update(kwargs)
        from mcp_remote_control.core.result import OpResult

        return OpResult(
            kind="ps",
            status="ok",
            fields={"op": op, "id": kwargs.get("id") or "ps_01", "exit": 0},
            body="ok",
        )

    monkeypatch.setattr(ps_ops, "run", _capture_run)

    code = main(
        [
            "ps",
            "invoke",
            "--id",
            "ps_01",
            "--script",
            "$x=1",
            "--timeout",
            "7.5",
            "--json",
        ]
    )
    assert code == EXIT_OK
    _ = capsys.readouterr()
    assert captured.get("op") == "invoke"
    assert captured.get("id") == "ps_01"
    assert captured.get("script") == "$x=1"
    assert captured.get("timeout") == 7.5


def test_ps_invoke_timeout_none_unlimited_and_positive_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=None -> unlimited; positive -> forwarded (aligned with exec)."""
    calls: list[float | None] = []

    class _Handle:
        location = r"C:\Users\mock"

    class _Transport:
        def runspace_invoke(
            self,
            handle: object,
            script: str,
            *,
            timeout_s: float | None = None,
        ) -> object:
            calls.append(timeout_s)
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(
                stdout="ok",
                stderr="",
                exit_code=0,
                location=r"C:\Users\mock",
            )

    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None
    sess.transport = _Transport()  # type: ignore[assignment]
    sess.handle = _Handle()

    r1 = ps_ops.invoke(id=sid, script="$x=1", timeout=None)
    assert r1.status == "ok"
    r2 = ps_ops.invoke(id=sid, script="$x=1", timeout=3.0)
    assert r2.status == "ok"

    assert calls == [None, 3.0]


@pytest.mark.parametrize(
    "bad_timeout",
    [0, 0.0, -5, -0.1, "abc", object()],
    ids=["zero-int", "zero-float", "neg-int", "neg-float", "unparseable", "obj"],
)
def test_ps_invoke_non_positive_or_unparseable_timeout_rejected(
    bad_timeout: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout<=0 and unparseable -> INVALID_ARG (no silent unlimited; same as exec)."""
    calls: list[float | None] = []

    class _Handle:
        location = r"C:\Users\mock"

    class _Transport:
        def runspace_invoke(
            self,
            handle: object,
            script: str,
            *,
            timeout_s: float | None = None,
        ) -> object:
            calls.append(timeout_s)
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(
                stdout="ok",
                stderr="",
                exit_code=0,
                location=r"C:\Users\mock",
            )

    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None
    sess.transport = _Transport()  # type: ignore[assignment]
    sess.handle = _Handle()

    r = ps_ops.invoke(
        id=sid,
        script="$x=1",
        timeout=bad_timeout,  # type: ignore[arg-type]
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "timeout" in msg.lower(), msg
    text = r.render_text()
    assert text.startswith("@ps error") or "INVALID_ARG" in text
    assert calls == [], "runspace_invoke must not be called with bad timeout"


@pytest.mark.parametrize(
    "bad_timeout",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "-inf",
    ],
    ids=["nan", "inf", "-inf", "str-nan", "str-inf", "str--inf"],
)
def test_ps_invoke_non_finite_timeout_rejected(
    bad_timeout: float | str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NaN and +/-Inf must be INVALID_ARG (never enter invoke stop pipeline)."""
    calls: list[float | None] = []

    class _Handle:
        location = r"C:\Users\mock"

    class _Transport:
        def runspace_invoke(
            self,
            handle: object,
            script: str,
            *,
            timeout_s: float | None = None,
        ) -> object:
            calls.append(timeout_s)
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(
                stdout="ok",
                stderr="",
                exit_code=0,
                location=r"C:\Users\mock",
            )

    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None
    sess.transport = _Transport()  # type: ignore[assignment]
    sess.handle = _Handle()

    r = ps_ops.invoke(
        id=sid,
        script="$x=1",
        timeout=bad_timeout,  # type: ignore[arg-type]
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "finite" in msg.lower() or "timeout" in msg.lower(), msg
    text = r.render_text()
    assert text.startswith("@ps error") or "INVALID_ARG" in text
    assert calls == [], "runspace_invoke must not be called with non-finite timeout"


@pytest.mark.parametrize(
    "timeout_argv",
    [
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        # argparse treats a bare "-inf" token as a flag; use = form.
        ["--timeout=-inf"],
    ],
    ids=["str-nan", "str-inf", "str--inf"],
)
def test_cli_ps_invoke_non_finite_timeout_rejected(
    timeout_argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --timeout nan/inf -> INVALID_ARG, never reaches Core stop path."""
    called: list[dict[str, object]] = []

    def _capture_run(op: str, **kwargs: object) -> object:
        called.append({"op": op, **kwargs})
        from mcp_remote_control.core.result import OpResult

        return OpResult(
            kind="ps",
            status="ok",
            fields={"op": op, "id": kwargs.get("id") or "ps_01", "exit": 0},
            body="ok",
        )

    monkeypatch.setattr(ps_ops, "run", _capture_run)

    code = main(
        [
            "ps",
            "invoke",
            "--id",
            "ps_01",
            "--script",
            "$x=1",
            *timeout_argv,
            "--json",
        ]
    )
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "INVALID_ARG" in out
    assert called == [], "ps_ops.run must not be called with non-finite --timeout"


def test_cli_local_ps_cap_denied(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ps", "open", "--ep", "local"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@ps" in out
    assert "CAP_DENIED" in out
    assert "lacks ps capability" in out.lower()


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
# runspace_invoke timeout + location-probe fold + dead-session prune.
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
        # Canned output: the user line plus BOTH probes the adapter appends.
        # A completed pipeline always emits the exit marker, so leaving it out
        # would describe a script that ended its own block (probe never ran).
        return ["user-out", f"{_EXIT_MARKER}0", f"__MRC_PS_CWD_MARKER__{loc}"]

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
    """A hanging Read-Host with timeout returns timed_out within ~the timeout,
    and the runspace is still usable afterward."""
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

    # Timed-out: status=timeout (aligned with exec), fields.timed_out, exit -1.
    assert r.status == "timeout"
    assert r.fields.get("timed_out") is True
    assert r.fields.get("exit") == -1
    assert elapsed < 3.0
    text = r.render_text()
    assert text.startswith("@ps timeout") or "timeout" in text.lower()

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
    """runspace_invoke runs the location probe in the SAME pipeline as the
    user script - PowerShell.invoke() called ONCE, not twice."""
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
    """After a NOT_CONNECTED TransportError on invoke, the dead session is
    pruned from the registry; the next invoke does NOT reuse it."""
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

    # Next invoke does NOT reuse the dead handle - PS_NOT_FOUND now.
    r2 = ps_ops.invoke(id=sid, script="$x")
    assert r2.status == "error"
    assert r2.code == "PS_NOT_FOUND"


class _SlowStopFakePowerShell(_FakePowerShell):
    """Fake PowerShell whose stop() releases the hung invoke but then blocks
    well past the stop deadline - verifies _safe_stop_pipeline is bounded."""

    def stop(self) -> None:
        self.stopped = True
        # Release the hung invoke so its executor thread finishes.
        self._block.set()
        # Then block past the _STOP_DEADLINE_S to prove cleanup is bounded.
        time.sleep(5.0)


def test_ps_invoke_stop_blocks_still_returns_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pipeline.stop() blocks, runspace_invoke still returns within
    ~timeout + stop deadline (does NOT wait for stop() to finish)."""
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
    # (1s) + stop deadline (2s) ~= 3s - NOT 5s+.
    assert elapsed < 4.5, f"runspace_invoke hung past the stop deadline: {elapsed}s"
    # stop() was at least initiated (the daemon thread sets stopped=True early).
    assert _FakePowerShell.instances[-1].stopped is True


def test_ps_concurrent_invoke_timeout_does_not_stop_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight pipelines on the pool adapter: A's wall-clock timeout
    must stop only A's pipeline; B completes with b-ok and is not stopped.

    Goes through ``runspace_invoke``'s unlocked body (``prepare_invoke`` +
    per-pipeline stop) so both pipelines overlap. The transport ``_op_lock``
    wrapper is skipped - that lock serializes the two calls and would only
    rendezvous B with A's leftover worker after A already timed out.
    """
    from mcp_remote_control.transport.winrm import PypsrpPoolRunspaceAdapter

    _FakePowerShell.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _FakePowerShell)

    both_running = threading.Barrier(2, timeout=10.0)
    b_hold = threading.Event()
    b_entered = threading.Event()
    a_returned = threading.Event()

    class _ConcurrentFakePowerShell(_FakePowerShell):
        def invoke(self, input: object = None, **_kw: object) -> list[object]:
            self.invoke_count += 1
            script = self.script or ""
            if "Read-Host" in script:
                both_running.wait()
                self._block.wait(timeout=30.0)
                return []
            b_entered.set()
            both_running.wait()
            if a_returned.is_set():
                raise AssertionError(
                    "sibling pipeline entered invoke after A timed out; "
                    "the two invokes did not overlap"
                )
            b_hold.wait(timeout=30.0)
            if self.stopped:
                return []
            loc = getattr(self.pool, "location", r"C:\Users\mock")
            return ["b-ok", f"{_EXIT_MARKER}0", f"__MRC_PS_CWD_MARKER__{loc}"]

    monkeypatch.setattr("pypsrp.powershell.PowerShell", _ConcurrentFakePowerShell)
    _FakePowerShell.instances.clear()

    sess = _PypsrpSession()

    def conn(**_kw: object) -> _PypsrpSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok"
    sid = opened.fields["id"]
    ps_sess = get_ps_registry().get(sid)
    assert ps_sess is not None
    transport = ps_sess.transport
    handle = ps_sess.handle
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    unlocked = getattr(transport.runspace_invoke, "__wrapped__", None)
    assert callable(unlocked), "runspace_invoke must expose the unlocked body"

    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def run_a() -> None:
        try:
            results["a"] = unlocked(
                transport, handle, "Read-Host 'hang-A'", timeout_s=0.8
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            a_returned.set()

    def run_b() -> None:
        try:
            results["b"] = unlocked(
                transport, handle, "$x = 'b-ok'; $x", timeout_s=10.0
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ta = threading.Thread(target=run_a, name="m9-a", daemon=True)
    tb = threading.Thread(target=run_b, name="m9-b", daemon=True)
    ta.start()
    tb.start()

    ta.join(timeout=8.0)
    assert not ta.is_alive(), "invoke A did not return after timeout"
    assert b_entered.is_set(), "B must have entered invoke while A was in-flight"
    b_hold.set()
    tb.join(timeout=8.0)
    assert not tb.is_alive(), "invoke B did not return"

    assert not errors, f"worker raised: {errors!r}"
    ra = results["a"]
    rb = results["b"]
    assert getattr(ra, "timed_out", False) is True
    assert getattr(rb, "timed_out", False) is False
    assert getattr(rb, "exit_code", -1) == 0
    assert "b-ok" in (getattr(rb, "stdout", "") or "")

    pipelines = list(_FakePowerShell.instances)
    assert len(pipelines) >= 2, f"expected 2 pipelines, got {len(pipelines)}"
    hang = [p for p in pipelines if p.script and "Read-Host" in (p.script or "")]
    sib = [p for p in pipelines if p.script and "b-ok" in (p.script or "")]
    assert hang and hang[0].stopped is True
    assert sib and sib[0].stopped is False
    assert sess.pool is not None
    assert not sess.pool.closed

# ---------------------------------------------------------------------------
# WinRM PS capability gate: ps_runspace.
# ---------------------------------------------------------------------------


def test_ps_open_unsupported_when_ps_runspace_false() -> None:
    """ps open with transport.meta winrm_ps.ps_runspace=false -> UNSUPPORTED."""
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
    assert get_ps_registry().list_open() == []
    text = r.render_text()
    assert "UNSUPPORTED" in text


# ---------------------------------------------------------------------------
# mark_dead / ensure reconnect retires old PS sessions.
# ---------------------------------------------------------------------------


def test_ensure_reconnect_after_mark_dead_invalidates_old_ps() -> None:
    """mark_dead -> ensure_connected closes pre-generation PS sessions;
    invoke on the old id is PS_NOT_FOUND; a new open on the same ep works.
    """
    from mcp_remote_control.endpoint import ensure_endpoint

    opened = _open_ps()
    assert opened.status == "ok"
    old_sid = opened.fields["id"]
    assert get_ps_registry().get(old_sid) is not None

    # Seed state so a live session would succeed invoke.
    set_r = ps_ops.invoke(id=old_sid, script="$x=1")
    assert set_r.status == "ok"

    reg = get_registry()
    ep = reg.get("lab-win")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark), "WinRMTransport must expose mark_dead"
    mark("peer_reset")
    ep.connected = True

    ep2 = ensure_endpoint(
        "lab-win", home=FIXTURES, connector=_ok_connector, probe=False
    )
    assert ep2.transport is not None
    assert ep2.transport.is_connected() is True

    assert get_ps_registry().get(old_sid) is None
    inv = ps_ops.invoke(id=old_sid, script="$x")
    assert inv.status == "error"
    assert inv.code == "PS_NOT_FOUND"

    opened2 = _open_ps()
    assert opened2.status == "ok"
    new_sid = opened2.fields["id"]
    assert new_sid != old_sid
    inv2 = ps_ops.invoke(id=new_sid, script="$y=2; $y")
    assert inv2.status == "ok"
    ps_ops.close_session(id=new_sid)


def test_ps_invoke_after_mark_dead_fails_immediately() -> None:
    """mark_dead without ensure: invoke fails NOT_CONNECTED and does not hang."""
    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    assert get_ps_registry().get(sid) is not None

    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark), "WinRMTransport must expose mark_dead"
    mark("peer_reset")
    assert ep.transport.is_connected() is False
    # Session stays registered until invoke (no ensure/reconnect).
    assert get_ps_registry().get(sid) is not None

    t0 = time.monotonic()
    r = ps_ops.invoke(id=sid, script="$x=1", timeout=30.0)
    elapsed = time.monotonic() - t0

    assert r.status == "error", r.render_text()
    assert r.code == "NOT_CONNECTED"
    assert elapsed < 1.0, f"invoke waited on a dead transport: {elapsed}s"
    text = r.render_text()
    assert "NOT_CONNECTED" in text
    # Pruned so the next invoke is PS_NOT_FOUND (same as invoke-error prune).
    assert get_ps_registry().get(sid) is None
    r2 = ps_ops.invoke(id=sid, script="$x=1")
    assert r2.status == "error"
    assert r2.code == "PS_NOT_FOUND"


def test_ps_invoke_after_mark_dead_never_enters_hanging_invoke() -> None:
    """Hanging runspace_invoke after mark_dead must never be entered."""
    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None and sess.transport is not None

    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None

    entered = threading.Event()
    hang = threading.Event()
    orig_invoke = sess.transport.runspace_invoke

    def hanging_invoke(*args: object, **kwargs: object) -> object:
        entered.set()
        hang.wait(timeout=30.0)
        return orig_invoke(*args, **kwargs)

    sess.transport.runspace_invoke = hanging_invoke  # type: ignore[method-assign]
    handle = sess.handle
    orig_handle_invoke = getattr(handle, "invoke", None)
    if callable(orig_handle_invoke):

        def hanging_handle_invoke(*args: object, **kwargs: object) -> object:
            entered.set()
            hang.wait(timeout=30.0)
            return orig_handle_invoke(*args, **kwargs)

        handle.invoke = hanging_handle_invoke  # type: ignore[method-assign]

    ep.transport.mark_dead("peer_reset")
    # Do not ensure_endpoint / reconnect.

    t0 = time.monotonic()
    r = ps_ops.invoke(id=sid, script="$x=1", timeout=30.0)
    elapsed = time.monotonic() - t0

    assert r.status == "error", r.render_text()
    assert r.code == "NOT_CONNECTED"
    assert elapsed < 1.0, f"invoke waited on hanging mock: {elapsed}s"
    assert not entered.is_set(), "invoke entered handle/runspace_invoke after mark_dead"
    hang.set()


def test_ps_snapshot_helpers_generation_fence() -> None:
    """snapshot + close_ids only touch listed ids (generation fence)."""
    from mcp_remote_control.ps.session import PsSession

    preg = get_ps_registry()
    preg.add(PsSession(id="ps_a", ep="ep-a", handle=object()))
    preg.add(PsSession(id="ps_b", ep="ep-a", handle=object()))
    preg.add(PsSession(id="ps_c", ep="ep-b", handle=object()))
    ids = ps_ops.snapshot_endpoint_session_ids("ep-a")
    assert set(ids) == {"ps_a", "ps_b"}
    preg.add(PsSession(id="ps_a_new", ep="ep-a", handle=object()))
    n = ps_ops.close_sessions_by_ids(ids)
    assert n == 2
    assert preg.get("ps_a") is None
    assert preg.get("ps_b") is None
    assert preg.get("ps_a_new") is not None
    assert preg.get("ps_c") is not None


# ---------------------------------------------------------------------------
# ps open vs concurrent close_endpoint (no zombie register).
# ---------------------------------------------------------------------------


def test_open_ps_concurrent_close_no_zombie_session() -> None:
    """close during open_runspace must not leave a live ps on a closed ep."""
    from mcp_remote_control.core import endpoint_ops
    from mcp_remote_control.endpoint import ensure_endpoint

    runspace_entered = threading.Event()
    release_runspace = threading.Event()

    class _GatedSession(MockWinRMSessionWithRunspace):
        def open_runspace(self) -> object:  # type: ignore[override]
            runspace_entered.set()
            assert release_runspace.wait(timeout=10.0), "release timeout"
            return super().open_runspace()

    def gated_connector(**_kwargs: object) -> _GatedSession:
        return _GatedSession(cwd=r"C:\Users\mock")

    ep = ensure_endpoint(
        "lab-win", home=FIXTURES, connector=gated_connector, probe=False
    )
    assert ep.transport is not None

    open_result: list[object] = []
    errors: list[BaseException] = []

    def do_open() -> None:
        try:
            open_result.append(
                ps_ops.open_session(
                    ep="lab-win",
                    home=FIXTURES,
                    connector=gated_connector,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_close() -> None:
        try:
            assert runspace_entered.wait(timeout=15.0), "runspace never entered"
            endpoint_ops.run(op="close", ep="lab-win")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            release_runspace.set()

    t_open = threading.Thread(target=do_open, name="r28-svc-ps-open")
    t_close = threading.Thread(target=do_close, name="r28-svc-ps-close")
    t_open.start()
    t_close.start()
    t_open.join(timeout=30.0)
    t_close.join(timeout=30.0)
    assert not t_open.is_alive() and not t_close.is_alive()
    assert not errors, errors
    assert open_result, "open_session did not return"
    opened = open_result[0]
    assert get_ps_registry().ids_for_endpoint("lab-win") == []
    status = getattr(opened, "status", None)
    if status == "ok":
        sid = getattr(opened, "fields", {}).get("id")
        assert sid is None or get_ps_registry().get(str(sid)) is None
    else:
        assert status == "error"
        assert getattr(opened, "code", None) in (
            "NOT_CONNECTED",
            "EXEC_FAILED",
            "CONNECT_FAILED",
        )
    assert get_registry().get("lab-win") is None


# ---------------------------------------------------------------------------
# PsSession.close / open_runspace must observe a wall-clock budget.
# ---------------------------------------------------------------------------


def test_ps_close_hanging_close_runspace_returns_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blackhole close_runspace must return within budget (no hang)."""
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import WinRMTransport

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangCloseHandle:
        location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(stdout="ok", exit_code=0, location=self.location)

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
    sess = PsSession(id="ps_hang", ep="lab-win", handle=handle, transport=t)
    t0 = time.monotonic()
    sess.close()  # must not raise / hang
    elapsed = time.monotonic() - t0
    assert sess.closed is True
    assert sess.handle is None
    assert elapsed < 2.0, f"PsSession.close wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_ps_close_session_hanging_close_not_silent_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hanging handle.close via close_session is not status=ok."""
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import WinRMTransport

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangCloseHandle:
        location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            from mcp_remote_control.transport.winrm import RunspaceResult

            return RunspaceResult(stdout="ok", exit_code=0, location=self.location)

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
    sess = PsSession(id="ps_hang_close", ep="lab-win", handle=handle, transport=t)
    get_ps_registry().add(sess)

    t0 = time.monotonic()
    closed = ps_ops.close_session(id="ps_hang_close")
    elapsed = time.monotonic() - t0

    assert closed.status in {"error", "partial", "timeout"}
    assert closed.status != "ok"
    assert closed.fields.get("closed") is True
    assert closed.fields.get("timed_out") is True
    text = closed.render_text()
    assert "timeout" in text.lower() or "timed_out" in text.lower()
    assert sess.closed is True
    assert sess.handle is None
    assert get_ps_registry().get("ps_hang_close") is None
    assert elapsed < 2.0, f"close_session wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_ps_open_hanging_open_runspace_fails_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blackhole open_runspace fails open within budget (no session registered)."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangOpenSession:
        # Seed identity + FullLanguage so endpoint open stays connected and
        # the ps_runspace gate stays open; only open_runspace itself hangs.
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"
        os = "windows"
        shell = "powershell"
        ps_version = "5.1.19041"
        language_mode = "FullLanguage"
        has_convertto_json = True
        can_get_item = True
        can_file_io = True

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

    def hang_connector(**kwargs: object) -> _HangOpenSession:
        assert "host" in kwargs
        return _HangOpenSession()

    t0 = time.monotonic()
    r = _open_ps(connector=hang_connector)
    elapsed = time.monotonic() - t0
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code == "EXEC_FAILED"
    msg = (r.fields.get("msg") or "").lower()
    assert "timed out" in msg or "timeout" in msg
    assert get_ps_registry().list_open() == []
    assert elapsed < 3.0, f"ps open wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_ps_close_normal_still_releases_handle() -> None:
    """Normal close still marks closed and releases the runspace handle."""
    opened = _open_ps()
    assert opened.status == "ok"
    sid = opened.fields["id"]
    reg = get_ps_registry()
    sess = reg.get(sid)
    assert sess is not None
    assert sess.closed is False

    closed = ps_ops.close_session(id=sid)
    assert closed.status == "ok"
    assert sess.closed is True
    assert sess.handle is None


# ---------------------------------------------------------------------------
# invoke vs handle.close share the transport serial lock.
# ---------------------------------------------------------------------------


def _winrm_ps_with_gated_handle(
    *,
    invoke_hold: threading.Event,
    invoke_entered: threading.Event,
    close_calls: list[float],
    overlap: list[bool],
) -> tuple[object, str]:
    """Real WinRMTransport + fake handle; invoke blocks until *invoke_hold*."""
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import RunspaceResult, WinRMTransport

    invoke_active = threading.Event()

    class _GatedHandle:
        location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            del script
            if invoke_active.is_set() or bool(close_calls):
                overlap.append(True)
            invoke_active.set()
            invoke_entered.set()
            assert invoke_hold.wait(timeout=30.0), "invoke hold timeout"
            invoke_active.clear()
            return RunspaceResult(
                stdout="ok",
                exit_code=0,
                location=self.location,
            )

        def close(self) -> None:
            if invoke_active.is_set():
                overlap.append(True)
            close_calls.append(time.monotonic())

    class _Sess:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def close(self) -> None:
            return None

        def open_runspace(self) -> _GatedHandle:
            return _GatedHandle()

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

    transport = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: _Sess(),
    )
    transport.connect()
    handle = transport.open_runspace()
    sid = "ps_serial_close"
    get_ps_registry().add(
        PsSession(id=sid, ep="lab-win", handle=handle, transport=transport)
    )
    return transport, sid


def test_ps_invoke_close_handle_not_overlapped() -> None:
    """Blocking invoke and concurrent close never overlap on handle.close."""
    invoke_hold = threading.Event()
    invoke_entered = threading.Event()
    close_calls: list[float] = []
    overlap: list[bool] = []
    _transport, sid = _winrm_ps_with_gated_handle(
        invoke_hold=invoke_hold,
        invoke_entered=invoke_entered,
        close_calls=close_calls,
        overlap=overlap,
    )

    invoke_result: list[object] = []
    close_result: list[object] = []
    errors: list[BaseException] = []

    def do_invoke() -> None:
        try:
            invoke_result.append(ps_ops.invoke(id=sid, script="$x=1"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_close() -> None:
        try:
            close_result.append(ps_ops.close_session(id=sid))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_inv = threading.Thread(target=do_invoke, name="ps-invoke-hold")
    t_close = threading.Thread(target=do_close, name="ps-close-wait")
    try:
        t_inv.start()
        assert invoke_entered.wait(timeout=10.0), "invoke never entered handle"
        t_close.start()
        # Close unregisters before waiting for the serial lock so a later
        # invoke cannot use the handle that close still has to release.
        unreg_deadline = time.monotonic() + 5.0
        while get_ps_registry().get(sid) is not None:
            assert time.monotonic() < unreg_deadline, "close did not unregister"
            time.sleep(0.01)
        time.sleep(0.2)
        assert close_calls == [], "handle.close overlapped in-flight invoke"
        again = ps_ops.invoke(id=sid, script="$x=1")
        assert again.status == "error"
        assert again.code == "PS_NOT_FOUND"
        invoke_hold.set()
        t_inv.join(timeout=15.0)
        t_close.join(timeout=15.0)
    finally:
        invoke_hold.set()
        t_inv.join(timeout=15.0)
        t_close.join(timeout=15.0)

    assert not t_inv.is_alive() and not t_close.is_alive()
    assert not errors, errors
    assert overlap == [], "handle.close overlapped handle.invoke"
    assert close_calls, "handle.close never ran after invoke released the lock"
    assert invoke_result and getattr(invoke_result[0], "status", None) == "ok"
    assert close_result and getattr(close_result[0], "status", None) == "ok"
    assert get_ps_registry().get(sid) is None


def test_ps_close_lock_wait_exhausted_skips_handle_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock wait counts toward the close budget; a miss skips handle.close."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    invoke_hold = threading.Event()
    invoke_entered = threading.Event()
    close_calls: list[float] = []
    overlap: list[bool] = []
    _transport, sid = _winrm_ps_with_gated_handle(
        invoke_hold=invoke_hold,
        invoke_entered=invoke_entered,
        close_calls=close_calls,
        overlap=overlap,
    )

    invoke_result: list[object] = []
    close_result: list[object] = []
    errors: list[BaseException] = []

    def do_invoke() -> None:
        try:
            invoke_result.append(ps_ops.invoke(id=sid, script="$x=1"))
        except BaseException as extra:  # noqa: BLE001
            errors.append(extra)

    t_inv = threading.Thread(target=do_invoke, name="ps-invoke-budget")
    try:
        t_inv.start()
        assert invoke_entered.wait(timeout=10.0), "invoke never entered handle"
        t0 = time.monotonic()
        closed = ps_ops.close_session(id=sid)
        elapsed = time.monotonic() - t0
        close_result.append(closed)

        assert closed.status in {"error", "partial", "timeout"}
        assert closed.status != "ok"
        assert closed.fields.get("closed") is True
        assert closed.fields.get("timed_out") is True
        assert close_calls == [], "handle.close started after lock wait expired"
        assert overlap == []
        assert elapsed < 2.0, f"close wall-clock not bounded: {elapsed}s"
        assert elapsed >= 0.15, f"lock wait timed out too early: {elapsed}s"
        assert get_ps_registry().get(sid) is None
        again = ps_ops.invoke(id=sid, script="$x=1")
        assert again.status == "error"
        assert again.code == "PS_NOT_FOUND"
    finally:
        invoke_hold.set()
        t_inv.join(timeout=15.0)

    assert not t_inv.is_alive()
    assert not errors, errors
    assert invoke_result, "in-flight invoke did not return"
    assert close_calls == [], "handle.close must stay skipped after budget miss"


# ---------------------------------------------------------------------------
# Local retirement takes the transport's serial-zone registration back.
#
# A pool adapter registers its retained-release drain on the transport's
# serial-zone registry (``SerialZoneHooks``), and that registry holds the
# adapter through the bound method it stores. The retirement paths that never
# reach ``adapter.close()`` - a close whose wait for the serial lock missed,
# and ``PsSession.abandon`` - have to take that registration back themselves:
# locally, with no exchange and no wait for the transport lock, and without
# changing the close budget or the verdict it reports. A handle that still
# owes a release keeps its registration, because a later serial zone is the
# only thing that can land it.
# ---------------------------------------------------------------------------


class _RetiringPool:
    """Pool-like handle without ``invoke``: ``open_runspace`` adapts it to a pool adapter."""

    def __init__(self, location: str) -> None:
        self.location = location
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _RetiringSession:
    """WinRM session whose runspace is one pool adapter.

    Every exchange a retirement that must stay local could make is a tripwire
    here: these sessions answer nothing, they fail the test.
    """

    cwd = r"C:\Users\mock"
    home = r"C:\Users\mock"
    os = "windows"
    shell = "powershell"
    ps_version = "5.1"

    def __init__(self) -> None:
        self.pool: _RetiringPool | None = None
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def open_runspace(self) -> _RetiringPool:
        self.pool = _RetiringPool(self.cwd)
        return self.pool

    def execute_ps(self, script: str, *, environment: object = None) -> object:
        raise AssertionError(f"a locally retired session sent this exchange: {script}")


def _retiring_transport() -> tuple[Any, Any, _RetiringSession]:
    """A connected transport, the pool adapter it opened, and its session."""
    from mcp_remote_control.transport.winrm import WinRMTransport

    session = _RetiringSession()
    transport = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: session,
    )
    transport.connect()
    handle = transport.open_runspace()
    return transport, handle, session


def _zone_hook_count(transport: Any) -> int:
    """How many callables the transport's serial-zone registry holds.

    The registry's list is the strong reference a retired handle must not stay
    in: it holds the adapter, and the adapter holds its pool. Zero is what a
    retirement has to reach once nothing is owed.
    """
    return len(transport.serial_zone_hooks._hooks)


def _assert_adapter_collectable(ref: weakref.ref, *, label: str) -> None:
    """The retired adapter is garbage once no registration holds it."""
    gc.collect()
    assert ref() is None, (
        f"{label}: the transport's registry still holds the retired adapter"
    )


def test_ps_close_timeout_revokes_the_zone_hook_of_a_session_that_owes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that never reaches the handle still revokes its zone hook.

    The wait for the transport serial lock is drawn from the close budget, and
    a miss leaves the handle untouched on purpose so an in-flight invoke is not
    cancelled. That path never calls ``handle.close()``, so the registration
    the adapter made on the transport's serial-zone registry has to be taken
    back by the retirement itself - no exchange, no wait for the lock, and the
    verdict the close reports unchanged.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import _CLOSE_TIMEOUT

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.05)
    transport, handle, session = _retiring_transport()
    assert _zone_hook_count(transport) == 1, (
        "the opened adapter did not register its drain"
    )
    ref = weakref.ref(handle)
    sess = PsSession(id="ps_timeout", ep="lab-win", handle=handle, transport=transport)

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
        started = time.monotonic()
        sess.close()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(timeout=10.0)

    assert not holder.is_alive()
    assert sess.close_verdict == _CLOSE_TIMEOUT, sess.close_verdict
    assert elapsed < 1.0, f"the bounded close waited too long: {elapsed}s"
    assert session.pool is not None and session.pool.close_calls == 0, (
        "the retirement sent a release it had no serial lock for"
    )
    assert _zone_hook_count(transport) == 0, (
        "the retired session's adapter is still registered with the transport"
    )
    del handle
    _assert_adapter_collectable(ref, label="close-timeout retirement")


def test_ps_abandon_revokes_the_zone_hook_without_waiting_for_the_serial_lock() -> None:
    """``abandon`` retires locally, and the peer's zone is never waited for.

    The transport is already gone on this path, so nothing may be exchanged
    and the serial lock must not be waited for. What the retirement still owes
    the transport is the adapter's registration: the registry's list would
    otherwise hold the adapter - and through it the pool - for the life of the
    transport, and every later serial zone would walk its empty drain.
    """
    from mcp_remote_control.ps.session import PsSession

    transport, handle, session = _retiring_transport()
    assert _zone_hook_count(transport) == 1, (
        "the opened adapter did not register its drain"
    )
    ref = weakref.ref(handle)
    sess = PsSession(id="ps_abandon", ep="lab-win", handle=handle, transport=transport)

    held = threading.Event()
    release = threading.Event()
    left_the_zone: list[bool] = []

    def _hold_the_serial_zone() -> None:
        with transport.op_lock:
            held.set()
            release.wait(timeout=30.0)
        left_the_zone.append(True)

    holder = threading.Thread(
        target=_hold_the_serial_zone, name="ps-serial-zone-peer", daemon=True
    )
    errors: list[BaseException] = []

    def _abandon() -> None:
        try:
            sess.abandon()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    worker = threading.Thread(target=_abandon, name="ps-abandon", daemon=True)
    try:
        holder.start()
        assert held.wait(timeout=10.0), "the peer never took the serial zone"
        started = time.monotonic()
        worker.start()
        worker.join(timeout=10.0)
        elapsed = time.monotonic() - started
        assert not worker.is_alive(), (
            "abandon did not return while the peer held the serial zone"
        )
        assert not left_the_zone, "abandon waited for the peer's serial zone"
    finally:
        release.set()
        holder.join(timeout=10.0)

    assert not errors, errors
    assert elapsed < 1.0, f"the local retirement took {elapsed}s"
    assert session.close_calls == 0, "abandon exchanged with the session"
    assert sess.handle is None and sess.transport is None
    assert _zone_hook_count(transport) == 0, (
        "the abandoned session's adapter is still registered with the transport"
    )
    del handle
    _assert_adapter_collectable(ref, label="abandon")


def test_ps_local_retirement_is_idempotent_and_keeps_the_handle_closeable() -> None:
    """A second retirement is a no-op, and ``close`` still releases the pool.

    Every retirement path asks for the local revocation, so it has to be
    repeatable, and it is not a substitute for the remote teardown: an
    explicit ``handle.close()`` afterwards still closes the pool and stays a
    no-op on the registry it has already left.
    """
    from mcp_remote_control.ps.session import PsSession

    transport, handle, session = _retiring_transport()
    sess = PsSession(id="ps_twice", ep="lab-win", handle=handle, transport=transport)

    sess.abandon()
    sess.abandon()  # the same answer for work that is already done

    assert _zone_hook_count(transport) == 0
    assert session.pool is not None and session.pool.close_calls == 0, (
        "a local retirement must not release the remote runspace"
    )

    handle.close()  # the remote teardown is untouched by the local retirement
    assert session.pool.close_calls == 1, "handle.close must still close the pool"
    assert _zone_hook_count(transport) == 0


def test_ps_normal_close_revokes_the_zone_hook_of_its_session() -> None:
    """The normal close path leaves no registration behind either.

    ``handle.close`` is the remote teardown and it takes its own registration
    back; the local retirement running before it must not leave one behind for
    a session the transport can no longer mean anything for.
    """
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import _CLOSE_LANDED

    transport, handle, session = _retiring_transport()
    ref = weakref.ref(handle)
    sess = PsSession(id="ps_closed", ep="lab-win", handle=handle, transport=transport)

    sess.close()

    assert sess.close_verdict == _CLOSE_LANDED, sess.close_verdict
    assert session.pool is not None and session.pool.close_calls == 1
    assert _zone_hook_count(transport) == 0
    del handle
    _assert_adapter_collectable(ref, label="normal close")


# ---------------------------------------------------------------------------
# MockRunspace unknown statements must fail; ``exit N`` maps to exit_code.
# ---------------------------------------------------------------------------


def test_mock_runspace_unknown_statement_not_silent_ok() -> None:
    """Get-Foo must not return exit 0 / empty stderr (false confidence)."""
    rs = MockRunspace()
    r = rs.invoke("Get-Foo")
    assert r.exit_code != 0
    assert r.had_errors is True
    err = (r.stderr or "").upper()
    assert "UNKNOWN" in err
    assert "Get-Foo" in (r.stderr or "")


def test_mock_runspace_exit_n_maps_exit_code() -> None:
    """exit N maps to exit_code=N (and had_errors when N != 0)."""
    rs = MockRunspace()
    r = rs.invoke("exit 5")
    assert r.exit_code == 5
    assert r.had_errors is True

    r0 = MockRunspace().invoke("exit 0")
    assert r0.exit_code == 0
    assert r0.had_errors is False


def test_mock_runspace_known_dialect_still_succeeds() -> None:
    """Supported dialect paths still return exit 0."""
    rs = MockRunspace()
    assert rs.invoke("$x=1").exit_code == 0
    got = rs.invoke("$x")
    assert got.exit_code == 0
    assert "1" in (got.stdout or "")
    assert rs.invoke("$x='b-ok'").exit_code == 0
    quoted = rs.invoke("$x")
    assert quoted.exit_code == 0
    assert "b-ok" in (quoted.stdout or "")
    assert rs.invoke("Write-Output 'hi'").exit_code == 0
    assert "hi" in (rs.invoke("Write-Output 'hi'").stdout or "")
    assert rs.invoke(f"Set-Location '{rs.location}'").exit_code == 0
    loc = rs.invoke("Get-Location")
    assert loc.exit_code == 0
    assert rs.location in (loc.stdout or "")
    lit = rs.invoke("'literal'")
    assert lit.exit_code == 0
    assert "literal" in (lit.stdout or "")


def test_mock_runspace_non_literal_assignment_is_unknown() -> None:
    """Assignment RHS that is not int/quoted-string must not silently succeed."""
    rs = MockRunspace()
    r = rs.invoke("$x = Get-Foo")
    assert r.exit_code != 0
    assert r.had_errors is True
    err = (r.stderr or "").upper()
    assert "UNKNOWN" in err
    assert "Get-Foo" in (r.stderr or "")
    # Failed assignment must not store the bare word as a variable value.
    readback = rs.invoke("$x")
    assert readback.exit_code != 0

    lit_int = MockRunspace().invoke("$x=1")
    assert lit_int.exit_code == 0
    lit_str = MockRunspace()
    assert lit_str.invoke("$x='b-ok'").exit_code == 0
    got = lit_str.invoke("$x")
    assert got.exit_code == 0
    assert "b-ok" in (got.stdout or "")


def test_mock_runspace_unknown_via_ps_ops_surfaces_fail() -> None:
    """Service path via MockWinRMSessionWithRunspace also fails recognizably."""
    opened = _open_ps()
    sid = opened.fields["id"]
    r = ps_ops.invoke(id=sid, script="Get-Foo")
    assert r.status == "fail"
    assert r.fields.get("exit") not in (0, None)
    # The stderr payload is the body here, behind its [stderr] marker.
    body = (r.body or "").upper()
    text = r.render_text().upper()
    assert "UNKNOWN" in body or "UNKNOWN" in text

    assign = ps_ops.invoke(id=sid, script="$x = Get-Foo")
    assert assign.status == "fail"
    assert assign.fields.get("exit") not in (0, None)
    assign_body = (assign.body or "").upper()
    assign_text = assign.render_text().upper()
    assert "UNKNOWN" in assign_body or "UNKNOWN" in assign_text


# ---------------------------------------------------------------------------
# Body shape: stderr is labelled for every shape; timeout cwd is marked stale.
# ---------------------------------------------------------------------------


def test_stderr_only_body_carries_the_marker() -> None:
    """A stderr-only invoke must label its error text, not return it bare.

    Bare error text is byte-for-byte indistinguishable from a body the invoke
    wrote to stdout, so a caller cannot tell a diagnostic from the run's data.
    exec labels every non-empty stderr; ps follows the same contract.
    """
    opened = _open_ps()
    sid = opened.fields["id"]

    r = ps_ops.invoke(id=sid, script="Get-Foo")
    assert r.status == "fail"
    assert (r.body or "").startswith("[stderr]\n"), r.render_text()
    assert "UNKNOWN" in (r.body or ""), r.render_text()
    # Both tracks carry the labelled body, not the raw error text.
    assert "[stderr]" in r.render_text()
    assert json.loads(r.render_json())["body"].startswith("[stderr]\n")


def test_stderr_follows_stdout_body_behind_the_marker() -> None:
    """stdout stays the unlabelled body; stderr follows it behind the marker."""
    opened = _open_ps()
    sid = opened.fields["id"]

    r = ps_ops.invoke(id=sid, script="Write-Output 'user-out'\nGet-Foo")
    assert r.status == "fail"
    body = r.body or ""
    assert body.startswith("user-out\n[stderr]\n"), r.render_text()
    assert "UNKNOWN" in body, r.render_text()


def test_invoke_docstring_states_the_real_probe_skip_trigger() -> None:
    """Documented contract: only a top-level ``exit`` skips the probes.

    Measured on pwsh 7.4 (the lts image): the user text is dot-sourced as its
    own block on every path that appends the probes, so a top-level ``return``
    ends that block only and the probes after it still emit their markers -
    ``. { & /bin/sh -c 'exit 7'; return }`` reports exit 7 - while the same
    text ending in a top-level ``exit 7`` emits no marker at all.

    The docstring must name only ``exit`` as the trigger and must not revive
    the claim that some path splices the caller's text straight into the probe
    script: winrm.py isolates the oneshot exec path too (see
    tests/service/test_winrm_oneshot_probe.py), so a maintainer reasoning from
    such a claim would hunt for a trigger that no longer exists.
    """
    doc = " ".join(
        (ps_ops.invoke.__doc__ or "").replace("`", "").replace("*", "").split()
    ).lower()
    assert "top-level exit" in doc, doc
    assert "top-level return does not" in doc, doc
    assert "splic" not in doc, doc
    # No sentence may pair ``return`` with a skip claim; the documented rule is
    # that the probes still run after it.
    for sentence in doc.split(". "):
        if "return" in sentence and ("skip" in sentence or "missing" in sentence):
            assert "not" in sentence, sentence
    # The timed-out cwd is the transport's last known location - an unprobed
    # handle falls back to the transport's own configured directory - so the
    # docstring must not describe it as one a probe confirmed.
    assert "last known location" in doc, doc
    assert "probe confirmed" not in doc, doc


class _TimedOutInvokeTransport:
    """Invoke that always hits the deadline, like a stopped WSMan pipeline.

    Mirrors ``WinRMTransport.runspace_invoke``'s timeout result: ``location`` is
    the handle's *last known* one, because the pipeline was stopped before the
    appended location probe could report this invoke's.
    """

    def __init__(self, last_known: str) -> None:
        self.last_known = last_known

    def is_connected(self) -> bool:
        return True

    def runspace_invoke(
        self,
        handle: object,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> object:
        from mcp_remote_control.transport.winrm import RunspaceResult

        return RunspaceResult(
            stdout="",
            stderr="",
            exit_code=-1,
            location=self.last_known,
            timed_out=True,
        )


def test_timeout_marks_cwd_stale() -> None:
    """A timed-out invoke reports the last known location - so say so.

    The deadline stops the pipeline before the location probe reports, so the
    transport supplies the handle's last known location. A caller that reads
    ``cwd`` after a timeout, or feeds it to a later exec/fs path, would
    otherwise use a directory the stopped script may already have left.
    """
    last_known = r"C:\Users\mock"
    opened = _open_ps()
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None
    sess.transport = _TimedOutInvokeTransport(last_known)  # type: ignore[assignment]

    r = ps_ops.invoke(
        id=sid,
        script="Set-Location C:\\other; Start-Sleep 60",
        timeout=5,
    )
    assert r.status == "timeout"
    assert r.fields.get("timed_out") is True
    assert r.fields.get("cwd_stale") is True, r.render_text()
    assert r.cwd == last_known
    text = r.render_text()
    assert "timed_out" in text
    assert "cwd_stale" in text
    payload = json.loads(r.render_json())
    assert payload["cwd_stale"] is True
    assert payload["timed_out"] is True


class _RaisingInvokeTransport:
    """Invoke that escapes with a non-TransportError, returning no result."""

    def __init__(self, last_known: str) -> None:
        self.last_known = last_known

    def is_connected(self) -> bool:
        return True

    def runspace_invoke(
        self,
        handle: object,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> object:
        raise RuntimeError("invoker blew up")


def test_invoke_without_a_result_also_marks_cwd_stale() -> None:
    """An invoke that returned no result reported no location either.

    Same shape as the timeout path: the session survives (only a transport
    error prunes it), so the caller may keep using it - and ``cwd`` is the
    last known location, not one this invoke confirmed.
    """
    last_known = r"C:\Users\mock"
    opened = _open_ps()
    sid = opened.fields["id"]
    sess = get_ps_registry().get(sid)
    assert sess is not None
    sess.transport = _RaisingInvokeTransport(last_known)  # type: ignore[assignment]

    r = ps_ops.invoke(id=sid, script="$x = 1")
    assert r.status == "error"
    assert r.code == "EXEC_FAILED"
    assert r.fields.get("cwd_stale") is True, r.render_text()
    assert r.cwd == last_known
    assert "cwd_stale" in r.render_text()
    assert get_ps_registry().get(sid) is not None, "session must not be pruned"



# ---------------------------------------------------------------------------
# ps open / ps close link policy
#
# Plain-HTTP WinRM with message encryption rejects any request made with a
# stale context with an empty-body HTTP 400 - provably pre-execution. The
# runspace Create (ps open) and the WSMan Delete (ps close) are payload
# requests on that link, so both must re-handshake and replay once, and the
# close status must say what happened to the remote runspace.
# ---------------------------------------------------------------------------

try:  # pypsrp is a hard dependency; the fallback keeps the shape testable.
    from pypsrp import exceptions as pypsrp_exceptions
except ImportError:  # pragma: no cover - exercised only without pypsrp
    pypsrp_exceptions = None  # type: ignore[assignment]


def _stale_encryption_rejection() -> BaseException:
    """The measured stale-framing rejection: HTTP 400, empty body."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", 400, "")
    return type(
        "WinRMTransportError",
        (Exception,),
        {"__module__": "pypsrp.exceptions", "__qualname__": "WinRMTransportError"},
    )("http", 400, "")


class _HttpSession:
    """Stand-in for the cached ``requests.Session`` a re-handshake drops."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _LinkTransport:
    """Node holding the cached message-encryption context."""

    def __init__(self, http_session: _HttpSession) -> None:
        self.encryption: str | None = "auto"
        self.session = http_session


class _Wsman:
    def __init__(self, link_transport: _LinkTransport) -> None:
        self.transport = link_transport


class _LinkRunspace(MockRunspace):
    """Runspace whose WSMan Delete is refused while the link is stale."""

    def __init__(self, session: _LinkSession) -> None:
        super().__init__()
        self._session = session
        self.close_attempts = 0
        self.deleted = False

    def close(self) -> None:
        self.close_attempts += 1
        if self._session.delete_always_refused or self._session.stale:
            raise _stale_encryption_rejection()
        self.deleted = True
        self.closed = True


class _LinkSession(MockWinRMSessionWithRunspace):
    """Session with pypsrp-shaped link state and a delete-refusing runspace.

    ``delete_always_refused`` models a delete refusal that survives the
    re-handshake, i.e. a delete that provably never reaches the server; the
    open still heals once the link is re-handshaked.
    """

    def __init__(self, *, delete_always_refused: bool = False) -> None:
        super().__init__()
        self.delete_always_refused = delete_always_refused
        self.http_session = _HttpSession()
        self.link_transport = _LinkTransport(self.http_session)
        self.wsman = _Wsman(self.link_transport)
        self.runspace = _LinkRunspace(self)
        self.opens = 0

    @property
    def stale(self) -> bool:
        return self.link_transport.encryption is not None

    def open_runspace(self) -> _LinkRunspace:  # type: ignore[override]
        self.opens += 1
        if self.stale:
            raise _stale_encryption_rejection()
        return self.runspace


def test_ps_open_provable_refusal_rehandshakes_and_replays() -> None:
    """A stale-context ps open heals in place instead of wedging.

    Before the transport learned this policy the first ps op after an idle
    gap failed EXEC_FAILED forever (nothing re-handshaked, nothing marked the
    link dead, so every retry failed identically).
    """
    sess = _LinkSession()
    opened = _open_ps(connector=lambda **_k: sess)

    assert opened.status == "ok", opened.render_text()
    assert sess.opens == 2, "exactly one replay"
    assert sess.link_transport.encryption is None, "re-handshake cleared the context"
    sid = opened.fields.get("id")
    assert sid and get_ps_registry().get(str(sid)) is not None


def test_ps_close_replays_refused_delete_and_confirms_it() -> None:
    """ps close must land the WSMan Delete, not just report one.

    The registry teardown is best-effort and swallows the rejection; the
    transport then re-handshakes and replays the delete, and ``ok`` is
    reported only because the remote runspace really is gone.
    """
    sess = _LinkSession()
    opened = _open_ps(connector=lambda **_k: sess)
    assert opened.status == "ok"
    sid = opened.fields["id"]
    # The idle gap between ps open and ps close invalidates the cached
    # encryption context again: this close starts on a stale link.
    sess.link_transport.encryption = "auto"

    closed = ps_ops.close_session(id=sid)

    assert closed.status == "ok", closed.render_text()
    assert closed.fields.get("closed") is True
    assert sess.runspace.deleted is True, "the WSMan Delete reached the server"
    assert sess.link_transport.encryption is None, "re-handshake ran before the replay"
    assert sess.runspace.close_attempts >= 2
    assert get_ps_registry().get(sid) is None


def test_ps_close_unconfirmed_delete_is_not_reported_ok() -> None:
    """A delete that provably never landed must not read as a clean close.

    The session is released locally either way, but the status says the remote
    runspace may still be allocated - "best effort" is not a licence to claim
    a teardown that did not happen.
    """
    sess = _LinkSession(delete_always_refused=True)
    opened = _open_ps(connector=lambda **_k: sess)
    assert opened.status == "ok"
    sid = opened.fields["id"]

    closed = ps_ops.close_session(id=sid)

    assert closed.status == "error", closed.render_text()
    assert closed.code == "PS_CLOSE_UNCONFIRMED"
    assert closed.fields.get("closed") is True, "the local release still happened"
    msg = str(closed.fields.get("msg") or "")
    assert "not confirmed" in msg, msg
    assert sess.runspace.deleted is False
    assert get_ps_registry().get(sid) is None
    assert "PS_CLOSE_UNCONFIRMED" in closed.render_text()


def _shell_gone_fault() -> BaseException:
    """The WSMan fault a server answers with when the shell does not exist.

    0x8033805B ERROR_WSMAN_UNEXPECTED_SELECTORS: no object matches the
    selectors - the runspace was reaped (service restart, idle cleanup, another
    client), which is what pypsrp's own ``RunspacePool.is_alive`` reads as "the
    pool is closed". Positional args, as pypsrp raises it:
    ``WSManFaultError(code, machine, reason, provider, provider_path, ...)``.
    """
    if pypsrp_exceptions is None:  # pragma: no cover - pypsrp is a hard dep
        return type(
            "WSManFaultError",
            (Exception,),
            {"__module__": "pypsrp.exceptions", "__qualname__": "WSManFaultError"},
        )(0x8033805B, "host", "The shell was not found")
    return pypsrp_exceptions.WSManFaultError(
        0x8033805B, "host", "The shell was not found", None, None, None
    )


def _other_wsman_fault() -> BaseException:
    """A WSMan fault that says nothing about the runspace's existence."""
    if pypsrp_exceptions is None:  # pragma: no cover - pypsrp is a hard dep
        return type(
            "WSManFaultError",
            (Exception,),
            {"__module__": "pypsrp.exceptions", "__qualname__": "WSManFaultError"},
        )(5, "host", "Access is denied.")
    return pypsrp_exceptions.WSManFaultError(
        5, "host", "Access is denied.", None, None, None
    )


class _FaultingDeleteRunspace(MockRunspace):
    """Runspace whose WSMan Delete is answered with a fault, not a refusal."""

    def __init__(self, fault: BaseException) -> None:
        super().__init__()
        self.fault = fault
        self.delete_attempts = 0

    def close(self) -> None:
        self.delete_attempts += 1
        raise self.fault


class _FaultingSession(MockWinRMSessionWithRunspace):
    def __init__(self, fault: BaseException) -> None:
        super().__init__()
        self.runspace = _FaultingDeleteRunspace(fault)

    def open_runspace(self) -> MockRunspace:  # type: ignore[override]
        return self.runspace


def test_ps_close_treats_a_shell_gone_fault_as_a_landed_delete() -> None:
    """A server answering "no such shell" proves the runspace is gone.

    Reporting ``PS_CLOSE_UNCONFIRMED``/"may still be allocated" there would be a
    false claim about the host: the delete has nothing left to delete, and the
    pre-fix ``ok`` was the truthful verdict for this sequence.
    """
    fault = _shell_gone_fault()
    sess = _FaultingSession(fault)
    opened = _open_ps(connector=lambda **_k: sess)
    assert opened.status == "ok", opened.render_text()
    sid = opened.fields["id"]

    closed = ps_ops.close_session(id=sid)

    assert closed.status == "ok", closed.render_text()
    assert closed.code is None
    assert closed.fields.get("closed") is True
    row = closed.render_text()
    assert "may still be allocated" not in row
    assert sess.runspace.delete_attempts >= 1, "the fault was actually raised"
    assert get_ps_registry().get(sid) is None


def test_ps_close_keeps_unconfirmed_for_a_fault_that_proves_nothing() -> None:
    """Any other WSMan fault leaves the runspace's fate unknown - a landed
    delete must never be reported on that evidence."""
    sess = _FaultingSession(_other_wsman_fault())
    opened = _open_ps(connector=lambda **_k: sess)
    sid = opened.fields["id"]

    closed = ps_ops.close_session(id=sid)

    assert closed.status == "error", closed.render_text()
    assert closed.code == "PS_CLOSE_UNCONFIRMED"
    assert "may still be allocated" in closed.render_text()


# ---------------------------------------------------------------------------
# ps close: one lock, one budget, one verdict.
#
# The teardown must be a single budgeted critical section. A close that is
# split into a locked best-effort pass plus a later "confirmation" pass can
# overlap a sibling ps op on the same transport (the second pass takes no
# lock) and can pay a second full wall-clock budget, so the operation could
# report a teardown the budget never covered.
# ---------------------------------------------------------------------------


class _CloseBarrier:
    """Counters and events that make one close teardown observable."""

    def __init__(self) -> None:
        self.deletes = 0
        self.refusals = 0
        self.deleted = False
        # Set while a replayed Delete is inside the caller's critical section.
        self.recovery_in_flight = threading.Event()
        self.recovery_entered = threading.Event()
        self.recovery_release = threading.Event()


class _StaleFramingDelete:
    """Runspace handle whose first WSMan ``Delete`` is refused by a stale link.

    The refusal is the measured empty-body HTTP 400 - provably pre-execution -
    so the transport's close policy re-handshakes the link and replays the
    delete. ``hold_recovery`` keeps the replayed delete inside the caller's
    critical section until the test releases it, which is what pins the
    interleaving with a sibling op instead of relying on ordering luck.
    """

    location = r"C:\Users\mock"

    def __init__(
        self,
        barrier: _CloseBarrier,
        *,
        refuse_after_s: float = 0.0,
        land_after_s: float = 0.0,
        hold_recovery: bool = False,
    ) -> None:
        self._barrier = barrier
        self._refuse_after_s = refuse_after_s
        self._land_after_s = land_after_s
        self._hold_recovery = hold_recovery

    def invoke(self, script: str) -> object:
        from mcp_remote_control.transport.winrm import RunspaceResult

        return RunspaceResult(
            stdout=f"ok:{script}",
            exit_code=0,
            location=self.location,
        )

    def close(self) -> None:
        self._barrier.deletes += 1
        if self._barrier.deletes == 1:
            time.sleep(self._refuse_after_s)
            self._barrier.refusals += 1
            raise _stale_encryption_rejection()
        time.sleep(self._land_after_s)
        self._barrier.recovery_in_flight.set()
        self._barrier.recovery_entered.set()
        try:
            if self._hold_recovery:
                assert self._barrier.recovery_release.wait(timeout=30.0), (
                    "the replayed delete was never released"
                )
            self._barrier.deleted = True
        finally:
            self._barrier.recovery_in_flight.clear()


class _SiblingInvokeProbe:
    """Runspace handle that records whether a teardown was still in flight."""

    location = r"C:\Users\mock"

    def __init__(self, barrier: _CloseBarrier) -> None:
        self._barrier = barrier
        self.overlap: list[bool] = []
        self.entered = threading.Event()

    def invoke(self, script: str) -> object:
        from mcp_remote_control.transport.winrm import RunspaceResult

        self.overlap.append(self._barrier.recovery_in_flight.is_set())
        self.entered.set()
        return RunspaceResult(
            stdout=f"ok:{script}",
            exit_code=0,
            location=self.location,
        )


class _SlowHttpSession(_HttpSession):
    """Cached HTTP session whose teardown costs wall clock (a re-handshake)."""

    def __init__(self, close_s: float) -> None:
        super().__init__()
        self._close_s = close_s

    def close(self) -> None:
        time.sleep(self._close_s)
        super().close()


class _CloseHandlesSession(MockWinRMSessionWithRunspace):
    """Session with pypsrp-shaped link state whose runspaces are test handles.

    ``open_runspace`` drains *handles* in order, so a test can give the ps
    session that closes and the sibling session that probes overlap their own
    handles while both share one transport.
    """

    def __init__(
        self,
        handles: list[object],
        *,
        link_close_s: float = 0.0,
    ) -> None:
        super().__init__()
        self._handles = list(handles)
        self.opens = 0
        self.http_session = (
            _SlowHttpSession(link_close_s) if link_close_s > 0 else _HttpSession()
        )
        self.link_transport = _LinkTransport(self.http_session)
        self.wsman = _Wsman(self.link_transport)

    def open_runspace(self) -> object:
        handle = self._handles[self.opens]
        self.opens += 1
        return handle


def _close_on_transport(session: object) -> object:
    """Real ``WinRMTransport`` over *session* (the close path under test)."""
    from mcp_remote_control.transport.winrm import WinRMTransport

    transport = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connector=lambda **_k: session,
    )
    transport.connect()
    return transport


def test_ps_close_recovery_does_not_overlap_another_sessions_invoke() -> None:
    """The refused-delete recovery stays inside the close's critical section.

    The first WSMan Delete is refused by the stale framing layer, so the
    transport re-handshakes the link and replays it. That replay must run under
    the same lock every other ps op takes: a sibling session's invoke on the
    shared transport may not enter its handle while the delete is still
    recovering.
    """
    from mcp_remote_control.ps.session import PsSession

    barrier = _CloseBarrier()
    deleting = _StaleFramingDelete(barrier, hold_recovery=True)
    probe = _SiblingInvokeProbe(barrier)
    session = _CloseHandlesSession([deleting, probe])
    transport = _close_on_transport(session)
    deleting_handle = transport.open_runspace()
    probe_handle = transport.open_runspace()
    reg = get_ps_registry()
    reg.add(
        PsSession(
            id="ps_close_recovery",
            ep="lab-win",
            handle=deleting_handle,
            transport=transport,
        )
    )
    reg.add(
        PsSession(
            id="ps_sibling_invoke",
            ep="lab-win",
            handle=probe_handle,
            transport=transport,
        )
    )

    closed: list[object] = []
    invoked: list[object] = []
    errors: list[BaseException] = []
    invoke_started = threading.Event()

    def do_close() -> None:
        try:
            closed.append(ps_ops.close_session(id="ps_close_recovery"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_invoke() -> None:
        try:
            invoke_started.set()
            invoked.append(ps_ops.invoke(id="ps_sibling_invoke", script="$y = 1"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_close = threading.Thread(target=do_close, name="ps-close-recovery")
    t_invoke = threading.Thread(target=do_invoke, name="ps-sibling-invoke")
    try:
        t_close.start()
        assert barrier.recovery_entered.wait(timeout=10.0), (
            "the refused delete was never replayed"
        )
        t_invoke.start()
        assert invoke_started.wait(timeout=10.0), "sibling invoke never started"
        # The replay is held open by the barrier, so an invoke that reaches its
        # handle here overlapped the teardown.
        entered_while_recovering = probe.entered.wait(timeout=0.5)
    finally:
        barrier.recovery_release.set()
        t_close.join(timeout=15.0)
        t_invoke.join(timeout=15.0)

    assert not errors, errors
    assert not t_close.is_alive() and not t_invoke.is_alive()
    assert entered_while_recovering is False, (
        "the sibling invoke entered while the refused delete was recovering"
    )
    assert probe.overlap == [False], probe.overlap
    assert closed and closed[0].status == "ok", closed[0].render_text()
    assert closed[0].fields.get("closed") is True
    assert barrier.deleted is True, "the replayed Delete landed"
    assert barrier.deletes == 2, barrier.deletes
    assert get_ps_registry().get("ps_close_recovery") is None
    assert invoked and invoked[0].status == "ok", invoked[0].render_text()


def test_ps_close_budget_cannot_run_two_full_teardown_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 100ms budget must not pay for two 80ms teardown stages.

    The first Delete is refused after 80ms and a replayed Delete takes another
    80ms. Both stages completing would mean the recovery was handed a fresh
    budget, and the ``ok`` that follows would report a teardown the close never
    had the wall clock to confirm.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.1)
    barrier = _CloseBarrier()
    deleting = _StaleFramingDelete(barrier, refuse_after_s=0.08, land_after_s=0.08)
    session = _CloseHandlesSession([deleting])
    transport = _close_on_transport(session)
    handle = transport.open_runspace()
    get_ps_registry().add(
        PsSession(id="ps_two_stage", ep="lab-win", handle=handle, transport=transport)
    )

    t0 = time.monotonic()
    closed = ps_ops.close_session(id="ps_two_stage")
    elapsed = time.monotonic() - t0

    assert closed.status != "ok", closed.render_text()
    assert closed.fields.get("closed") is True
    assert closed.fields.get("timed_out") is True
    assert barrier.deleted is False, "the recovery delete outran the shared budget"
    assert barrier.deletes >= 1, "the first delete was attempted"
    assert elapsed < 0.15, f"a second full teardown stage was paid for: {elapsed}s"
    assert get_ps_registry().get("ps_two_stage") is None


def test_ps_close_does_not_recover_after_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recovery attempt once the lock wait plus the first Delete spend it.

    The re-handshake itself costs wall clock inside the same deadline, so by
    the time the refused delete returns there is nothing left: the close must
    report the wall-clock miss instead of starting a second, freshly budgeted
    delete.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.4)
    barrier = _CloseBarrier()
    deleting = _StaleFramingDelete(barrier)
    handle_hold = threading.Event()
    handle_entered = threading.Event()

    class _GatedInvoke:
        """Sibling handle whose invoke holds the serial lock until released."""

        location = r"C:\Users\mock"

        def invoke(self, script: str) -> object:
            from mcp_remote_control.transport.winrm import RunspaceResult

            del script
            handle_entered.set()
            assert handle_hold.wait(timeout=30.0), "invoke hold was never released"
            return RunspaceResult(stdout="ok", exit_code=0, location=self.location)

        def close(self) -> None:
            return None

    session = _CloseHandlesSession([deleting, _GatedInvoke()], link_close_s=0.3)
    transport = _close_on_transport(session)
    deleting_handle = transport.open_runspace()
    gated_handle = transport.open_runspace()
    reg = get_ps_registry()
    reg.add(
        PsSession(
            id="ps_budget_spent",
            ep="lab-win",
            handle=deleting_handle,
            transport=transport,
        )
    )
    reg.add(
        PsSession(
            id="ps_lock_holder",
            ep="lab-win",
            handle=gated_handle,
            transport=transport,
        )
    )

    invoked: list[object] = []
    errors: list[BaseException] = []
    closed: list[object] = []

    def do_invoke() -> None:
        try:
            invoked.append(ps_ops.invoke(id="ps_lock_holder", script="$x = 1"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_close(elapsed: list[float]) -> None:
        try:
            t0 = time.monotonic()
            closed.append(ps_ops.close_session(id="ps_budget_spent"))
            elapsed.append(time.monotonic() - t0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_invoke = threading.Thread(target=do_invoke, name="ps-lock-holder")
    elapsed: list[float] = []
    t_close = threading.Thread(target=do_close, args=(elapsed,), name="ps-close-waits")
    try:
        t_invoke.start()
        assert handle_entered.wait(timeout=10.0), "sibling invoke never entered"
        # The sibling invoke holds the serial lock for the next 200ms of the
        # close's budget: the wait is part of the deadline the delete shares.
        t_close.start()
        unreg_deadline = time.monotonic() + 5.0
        while get_ps_registry().get("ps_budget_spent") is not None:
            assert time.monotonic() < unreg_deadline, "close did not unregister"
            time.sleep(0.01)
        time.sleep(0.2)
        handle_hold.set()
        t_invoke.join(timeout=15.0)
        t_close.join(timeout=15.0)
    finally:
        handle_hold.set()
        t_invoke.join(timeout=15.0)
        t_close.join(timeout=15.0)

    assert not errors, errors
    assert not t_invoke.is_alive() and not t_close.is_alive()
    assert closed and closed[0].status == "timeout", closed[0].render_text()
    assert closed[0].fields.get("timed_out") is True
    assert barrier.deletes <= 1, (
        f"a recovery delete was started after the budget was spent: {barrier.deletes}"
    )
    assert barrier.deleted is False
    assert elapsed and elapsed[0] < 1.0, f"close never returned: {elapsed}s"


class _LinkLostOpenSession(MockWinRMSessionWithRunspace):
    """Session whose runspace Create meets a gateway's own error page.

    The rejection carries a body, so it is not provably pre-execution: the
    transport marks the link dead and reports the loss (AF4). The session
    object mirrors pypsrp's link state so the death is observable.
    """

    def __init__(self) -> None:
        super().__init__()
        self.http_session = _HttpSession()
        self.link_transport = _LinkTransport(self.http_session)
        self.wsman = _Wsman(self.link_transport)

    def open_runspace(self) -> MockRunspace:  # type: ignore[override]
        raise pypsrp_exceptions.WinRMTransportError(  # type: ignore[union-attr]
            "http", 502, "Connection error: read ETIMEDOUT"
        )


def test_ps_open_link_death_carries_the_reopen_token() -> None:
    """The link death recorded during ``ps open`` must reach the row.

    The transport tears the local session down (the runspace died with it), so
    an Agent branching on fields - not on pypsrp's English - needs the same
    ``link_lost`` token and reopen advice the invoke path already carries.
    """
    sess = _LinkLostOpenSession()
    opened = _open_ps(connector=lambda **_k: sess)

    assert opened.status == "error", opened.render_text()
    assert opened.code == "EXEC_FAILED"
    assert opened.fields.get("link_lost") == 1, opened.fields
    assert "link lost" in str(opened.fields.get("msg") or "").lower()
    hint = str(opened.hint or "")
    assert "endpoint open" in hint and "ps open" in hint, hint


class _TeardownRunspace(MockRunspace):
    """Runspace whose Delete is refused while the link is stale."""

    def __init__(self, session: "_TeardownSession", *, always_refused: bool) -> None:
        super().__init__()
        self._session = session
        self._always = always_refused
        self.delete_attempts = 0

    def close(self) -> None:
        self.delete_attempts += 1
        if self._always or self._session.link_transport.encryption is not None:
            raise _stale_encryption_rejection()
        self.closed = True


class _TeardownSession(MockWinRMSessionWithRunspace):
    def __init__(self, *, always_refused: bool = False) -> None:
        super().__init__()
        self.http_session = _HttpSession()
        self.link_transport = _LinkTransport(self.http_session)
        self.wsman = _Wsman(self.link_transport)
        self.runspace = _TeardownRunspace(self, always_refused=always_refused)

    def open_runspace(self) -> MockRunspace:  # type: ignore[override]
        return self.runspace


def test_endpoint_teardown_confirms_the_refused_delete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Endpoint close/retire must not swallow a refused WSMan Delete.

    ``close_sessions_by_ids`` is the teardown entry both endpoint close and a
    retiring reconnect use. It reports a *local* release count, so the remote
    runspace must be confirmed through the same transport policy ``ps close``
    uses: a provably pre-execution refusal is replayed, and a delete that still
    did not land is logged instead of vanishing into a count.
    """
    sess = _TeardownSession()
    opened = _open_ps(connector=lambda **_k: sess)
    sid = opened.fields["id"]
    sess.link_transport.encryption = "auto"  # idle gap re-stales the link

    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.core.ps_ops"):
        count = ps_ops.close_sessions_by_ids(
            ps_ops.snapshot_endpoint_session_ids("lab-win")
        )

    assert count == 1
    assert sess.runspace.closed is True, "the replayed Delete really landed"
    assert sess.link_transport.encryption is None, "re-handshake ran before the replay"
    assert get_ps_registry().get(str(sid)) is None
    assert not [
        rec for rec in caplog.records if "not confirmed" in rec.getMessage()
    ], "a landed delete must not be reported as unconfirmed"


def test_endpoint_teardown_logs_a_delete_that_never_landed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delete that survives the re-handshake is recorded, not swallowed."""
    sess = _TeardownSession(always_refused=True)
    opened = _open_ps(connector=lambda **_k: sess)
    sid = opened.fields["id"]

    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.core.ps_ops"):
        count = ps_ops.close_sessions_by_ids(
            ps_ops.snapshot_endpoint_session_ids("lab-win")
        )

    assert count == 1, "the local release still happened"
    assert sess.runspace.closed is False
    assert get_ps_registry().get(str(sid)) is None
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "not confirmed" in msg and str(sid) in msg for msg in messages
    ), messages


def test_endpoint_teardown_does_not_restart_a_hung_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A handle that already spent its close budget is not given a second one.

    ``ps close`` refuses to start another teardown attempt on a hung handle;
    the endpoint-teardown sweep must do the same, or every endpoint close would
    pay the budget twice per hung session. The delete stays unconfirmed, and
    says so in the log.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.transport.winrm import WinRMTransport

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.25)
    block = threading.Event()

    class _HangCloseHandle:
        location = r"C:\Users\mock"

        def __init__(self) -> None:
            self.calls = 0

        def close(self) -> None:
            self.calls += 1
            block.wait(timeout=30.0)

    class _Sess:
        cwd = r"C:\Users\mock"
        home = r"C:\Users\mock"

        def __init__(self) -> None:
            self.handle: _HangCloseHandle | None = None

        def close(self) -> None:
            return None

        def open_runspace(self) -> _HangCloseHandle:
            self.handle = _HangCloseHandle()
            return self.handle

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del script, environment
            return "ok", None, False

    created: list[_Sess] = []

    def _connector(**_kw: object) -> _Sess:
        session = _Sess()
        created.append(session)
        return session

    t = WinRMTransport(host="h", username="u", password="p", connector=_connector)
    t.connect()
    sid = "ps_hang"
    get_ps_registry().add(
        PsSession(id=sid, ep="lab-win", handle=t.open_runspace(), transport=t)
    )
    raw_handle = created[-1].handle
    assert raw_handle is not None

    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.core.ps_ops"):
        count = ps_ops.close_sessions_by_ids([sid])
    elapsed = time.monotonic() - start
    block.set()

    assert count == 1, "the local release still happened"
    assert get_ps_registry().get(sid) is None
    assert raw_handle.calls == 1, "a hung handle must not be given a second budget"
    assert elapsed < 1.0, f"a second close budget was spent: {elapsed}s"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "not confirmed" in msg and sid in msg and "timeout" in msg for msg in messages
    ), messages


# ---------------------------------------------------------------------------
# The batch sweep (``close_sessions_by_ids``) runs the same close path.
# ---------------------------------------------------------------------------


def test_endpoint_teardown_recovery_holds_the_serial_lock() -> None:
    """The sweep's refused-delete recovery stays in the close's critical section.

    ``close_sessions_by_ids`` is the endpoint-teardown entry. A delete it has to
    re-handshake and replay belongs in the same budgeted critical section, so a
    sibling ps op on the transport cannot overlap it there either.
    """
    from mcp_remote_control.ps.session import PsSession

    barrier = _CloseBarrier()
    deleting = _StaleFramingDelete(barrier, hold_recovery=True)
    probe = _SiblingInvokeProbe(barrier)
    session = _CloseHandlesSession([deleting, probe])
    transport = _close_on_transport(session)
    deleting_handle = transport.open_runspace()
    probe_handle = transport.open_runspace()
    reg = get_ps_registry()
    reg.add(
        PsSession(
            id="ps_sweep_close",
            ep="lab-win",
            handle=deleting_handle,
            transport=transport,
        )
    )
    reg.add(
        PsSession(
            id="ps_sweep_probe",
            ep="lab-win",
            handle=probe_handle,
            transport=transport,
        )
    )

    swept: list[int] = []
    invoked: list[object] = []
    errors: list[BaseException] = []
    invoke_started = threading.Event()

    def do_sweep() -> None:
        try:
            swept.append(ps_ops.close_sessions_by_ids(["ps_sweep_close"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_invoke() -> None:
        try:
            invoke_started.set()
            invoked.append(ps_ops.invoke(id="ps_sweep_probe", script="$z = 1"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_sweep = threading.Thread(target=do_sweep, name="ps-sweep-recovery")
    t_invoke = threading.Thread(target=do_invoke, name="ps-sibling-invoke")
    try:
        t_sweep.start()
        assert barrier.recovery_entered.wait(timeout=10.0), (
            "the refused delete was never replayed"
        )
        t_invoke.start()
        assert invoke_started.wait(timeout=10.0), "sibling invoke never started"
        entered_while_recovering = probe.entered.wait(timeout=0.5)
    finally:
        barrier.recovery_release.set()
        t_sweep.join(timeout=15.0)
        t_invoke.join(timeout=15.0)

    assert not errors, errors
    assert not t_sweep.is_alive() and not t_invoke.is_alive()
    assert entered_while_recovering is False, (
        "the sibling invoke entered while the sweep was recovering a delete"
    )
    assert probe.overlap == [False], probe.overlap
    assert swept == [1], swept
    assert barrier.deleted is True, "the replayed Delete landed"
    assert barrier.deletes == 2, barrier.deletes
    assert get_ps_registry().get("ps_sweep_close") is None
    assert invoked and invoked[0].status == "ok", invoked[0].render_text()


def test_endpoint_teardown_does_not_restart_the_close_budget(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep spends one teardown budget per session, not two.

    The refused delete is replayable, but the recovery cannot fit the same
    deadline. Paying for a second full stage would report the delete as landed
    under a budget that never covered it; the sweep logs the timeout verdict it
    recorded instead.
    """
    import mcp_remote_control.transport.winrm as winrm_mod
    from mcp_remote_control.ps.session import PsSession

    monkeypatch.setattr(winrm_mod, "_RUNSPACE_OPEN_CLOSE_TIMEOUT_S", 0.1)
    barrier = _CloseBarrier()
    deleting = _StaleFramingDelete(barrier, refuse_after_s=0.08, land_after_s=0.08)
    session = _CloseHandlesSession([deleting])
    transport = _close_on_transport(session)
    handle = transport.open_runspace()
    get_ps_registry().add(
        PsSession(
            id="ps_sweep_budget",
            ep="lab-win",
            handle=handle,
            transport=transport,
        )
    )

    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.core.ps_ops"):
        count = ps_ops.close_sessions_by_ids(["ps_sweep_budget"])
    elapsed = time.monotonic() - start

    assert count == 1, "the local release still happened"
    assert get_ps_registry().get("ps_sweep_budget") is None
    assert barrier.deleted is False, "the recovery delete outran the shared budget"
    assert elapsed < 0.15, f"a second full teardown stage was paid for: {elapsed}s"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "not confirmed" in msg and "ps_sweep_budget" in msg and "timeout" in msg
        for msg in messages
    ), messages

