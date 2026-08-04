"""Service tests: WinRM transport + endpoint open + exec (T12, mock only)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pypsrp.powershell import RunspacePool

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_TRANSPORT
from mcp_remote_control.core import endpoint_ops, exec_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.winrm import (
    InvokeRunspaceAdapter,
    PypsrpPoolRunspaceAdapter,
    WinRMTransport,
    _adapt_runspace_handle,
    _format_ps_errors,
    _format_ps_output,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
FAKE_PASSWORD = "dummy-winrm-password"


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


def _ok_connector(**kwargs: object) -> _MockWinRMSession:
    # Ensure password is present for real-path wiring but never asserted in output.
    assert "host" in kwargs
    assert "username" in kwargs
    return _MockWinRMSession()


def _partial_probe_connector(**_kwargs: object) -> _MockWinRMSession:
    return _MockWinRMSession(probe_partial=True)


def _fail_connector(**_kwargs: object) -> None:
    raise TransportError("CONNECT_FAILED", "mock winrm refused")


def _auth_fail_connector(**_kwargs: object) -> None:
    raise TransportError("AUTH_FAILED", "mock credentials rejected")


# ---------------------------------------------------------------------------
# endpoint open + caps + probe
# ---------------------------------------------------------------------------


def test_winrm_open_mock_success_caps() -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert r.status == "ok"
    assert r.fields.get("transport") == "winrm"
    assert r.fields.get("ep") == "lab-win"
    caps = r.fields.get("caps") or ""
    assert "exec" in caps
    assert "fs" in caps
    assert "ps" in caps
    # screen must not be enabled
    assert "screen" not in caps.split(",")
    text = r.render_text()
    assert text.startswith("@endpoint ok")
    assert "caps=exec,fs,ps" in text
    assert FAKE_PASSWORD not in text
    assert "password" not in text.lower() or "password_path" not in text

    ep = reg.get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.caps["exec"] is True
    assert ep.caps["fs"] is True
    assert ep.caps["ps"] is True
    assert ep.caps["screen"] is False
    assert ep.probe is not None
    assert ep.probe.get("transport") == "winrm"
    # probe present (ok or partial)
    assert ep.probe.get("status") in ("ok", "partial")
    assert ep.probe.get("os") == "windows" or ep.probe.get("shell") == "powershell"


def test_winrm_probe_partial_still_open_ok() -> None:
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_partial_probe_connector,
    )
    assert r.status == "ok"
    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.probe is not None
    assert ep.probe.get("status") == "partial"


# ---------------------------------------------------------------------------
# WinRM PS capability on open (notes/025 §八)
# ---------------------------------------------------------------------------


class _FullLangWinRMSession(_MockWinRMSession):
    """Identity + FullLanguage capability seeds (no remote execute_ps)."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "FullLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


class _ConstrainedWinRMSession(_MockWinRMSession):
    """Identity + ConstrainedLanguage capability seeds."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "ConstrainedLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


def test_winrm_open_probe_full_language_agent_meta() -> None:
    """Default open with FullLanguage seeds → probe + Agent meta ps_* fields."""

    def connector(**_kwargs: object) -> _FullLangWinRMSession:
        return _FullLangWinRMSession()

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("ps_version")
    assert r.fields.get("lang_mode") == "FullLanguage"
    assert r.fields.get("ps_fs") == 1
    assert r.fields.get("ps_edition") == "Desktop"
    text = r.render_text()
    assert "lang_mode=FullLanguage" in text
    assert "ps_fs=1" in text

    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.probe is not None
    assert ep.probe.get("language_mode") == "FullLanguage"
    winrm_ps = ep.probe.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_script_fs") is True
    assert winrm_ps.get("ps_runspace") is True
    assert ep.transport is not None
    assert ep.transport.meta.get("winrm_ps", {}).get("ps_script_fs") is True


def test_winrm_open_probe_constrained_ps_fs_zero() -> None:
    """ConstrainedLanguage seeds → ps_fs=0 and ps_script_fs false in probe."""

    def connector(**_kwargs: object) -> _ConstrainedWinRMSession:
        return _ConstrainedWinRMSession()

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("lang_mode") == "ConstrainedLanguage"
    assert r.fields.get("ps_fs") == 0
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.probe is not None
    assert ep.probe["winrm_ps"]["ps_script_fs"] is False
    assert ep.probe["winrm_ps"]["ps_runspace"] is False


def test_winrm_open_connect_failed() -> None:
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_fail_connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"
    text = r.render_text()
    assert "CONNECT_FAILED" in text
    assert FAKE_PASSWORD not in text


def test_winrm_open_auth_failed() -> None:
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_auth_fail_connector,
    )
    assert r.status == "error"
    assert r.code == "AUTH_FAILED"
    assert FAKE_PASSWORD not in r.render_text()


def test_winrm_registry_winrm_connector_injection() -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES)
    assert ep.connected is True
    assert ep.transport_name == "winrm"
    assert ep.probe is not None


# ---------------------------------------------------------------------------
# exec forms
# ---------------------------------------------------------------------------


def test_winrm_exec_command() -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]

    r = exec_ops.run(
        ep="lab-win",
        command="Get-Service",
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert r.status == "ok"
    assert r.fields.get("ep") == "lab-win"
    assert r.fields.get("form") == "command"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "winrm-out:Get-Service" in r.body
    assert r.cwd is not None
    text = r.render_text()
    assert text.startswith("@exec ok")
    assert FAKE_PASSWORD not in text


def test_winrm_exec_argv() -> None:
    r = exec_ops.run(
        ep="lab-win",
        argv=["ipconfig", "/all"],
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "argv"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "ipconfig" in r.body


def test_winrm_exec_script() -> None:
    r = exec_ops.run(
        ep="lab-win",
        script="Write-Output 'hi-script'",
        runtime="powershell",
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "script"
    assert r.fields.get("exit") == 0
    # script → run_argv on transport; mock returns joined argv
    assert r.body is not None
    assert "powershell" in r.body.lower() or "Write-Output" in r.body or "script" in r.body


def test_winrm_exec_connect_failed() -> None:
    r = exec_ops.run(
        ep="lab-win",
        command="echo x",
        home=FIXTURES,
        connector=_fail_connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_winrm_open_ok(capsys: pytest.CaptureFixture[str]) -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]
    code = main(["endpoint", "open", "--profile", "lab-win"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@endpoint ok" in out
    assert "transport=winrm" in out
    assert "caps=exec,fs,ps" in out
    assert FAKE_PASSWORD not in out


def test_cli_winrm_connect_failed_exit(capsys: pytest.CaptureFixture[str]) -> None:
    reg = get_registry()
    reg.winrm_connector = _fail_connector  # type: ignore[assignment]
    code = main(["endpoint", "open", "--profile", "lab-win"])
    assert code == EXIT_TRANSPORT
    out = capsys.readouterr().out
    assert "CONNECT_FAILED" in out
    assert "@endpoint error" in out


def test_cli_winrm_list_shows_profile(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoint", "list", "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out.strip())
    assert data["status"] == "ok"
    # body lists profiles; lab-win must appear when listed as text path
    r = endpoint_ops.run(op="list", home=FIXTURES)
    assert r.body is not None
    assert "lab-win" in r.body
    assert "winrm" in r.body


# ---------------------------------------------------------------------------
# O7: WinRM transport fixes — cert_validation default, env+timeout on pypsrp,
# run_argv call-operator (no cmd % expansion), open_runspace close-on-fail.
# ---------------------------------------------------------------------------


def test_winrm_cert_validation_default_true_when_omitted() -> None:
    """O7 finding 6: a caller omitting cert_validation gets TLS validation."""
    t = WinRMTransport(host="h", username="u", connector=lambda **_k: object())
    assert t.cert_validation is True
    kw = t.connect_kwargs()
    assert kw["cert_validation"] is True


def test_winrm_cert_validation_false_when_explicit() -> None:
    """Explicit False is still honored (non-TLS mock convenience)."""
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: object(),
    )
    assert t.cert_validation is False
    assert t.connect_kwargs()["cert_validation"] is False


class _RecordingPsSession:
    """Mock pypsrp-style session exposing only execute_ps (no run_command)."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.scripts: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        # When environment is provided (Protocol path), prepend inject so
        # env-setter tests still observe the payload effect.
        if environment:
            from mcp_remote_control.transport.winrm import _inject_ps_env
            script = _inject_ps_env(script, environment)
        self.scripts.append(script)
        return ("ps-out\n", None, False)


class _RecordingCmdSession:
    """Mock pypsrp-style session exposing only execute_cmd (no run_command/ps)."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.commands: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        if environment:
            from mcp_remote_control.transport.winrm import _inject_cmd_env
            command = _inject_cmd_env(command, environment)
        self.commands.append(command)
        return ("cmd-out\n", "", 0)


def test_winrm_exec_env_setter_execute_ps_payload() -> None:
    """Sessions without native env handling receive environment= and may inject."""
    sess = _RecordingPsSession()

    def conn(**_kw: object) -> _RecordingPsSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        env={"FOO": "bar"},
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.scripts, "execute_ps was called"
    payload = sess.scripts[0]
    assert "[Environment]::SetEnvironmentVariable('FOO','bar','Process')" in payload
    # cwd-wrapped user command still present after the setter block
    assert "Get-Date" in payload


def test_winrm_exec_env_setter_execute_cmd_payload() -> None:
    """Sessions that inject on environment= prefix set NAME=VALUE for cmd."""
    sess = _RecordingCmdSession()

    def conn(**_kw: object) -> _RecordingCmdSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="whoami",
        env={"FOO": "bar"},
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.commands, "execute_cmd was called"
    payload = sess.commands[0]
    assert 'set "FOO=bar"' in payload
    assert "whoami" in payload


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


def test_winrm_exec_per_call_timeout_enforced() -> None:
    """O7 finding 2: per-call timeout enforced on the pypsrp execute_ps path."""
    sess = _BlockingPsSession()

    def conn(**_kw: object) -> _BlockingPsSession:
        return sess

    t0 = time.monotonic()
    r = exec_ops.run(
        ep="lab-win",
        command="Read-Host 'hang'",
        timeout=1.0,
        home=FIXTURES,
        connector=conn,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "timeout"
    assert r.fields.get("exit") == -1
    # Returns within ~the timeout (allow scheduler slack), not 30s.
    assert elapsed < 3.0
    # Release the blocked worker thread so the test session tears down cleanly.
    sess.block.set()


class _RecordingPsAndCmdSession:
    """Session with both execute_ps and execute_cmd so we can assert which one
    run_argv chooses."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.ps_scripts: list[str] = []
        self.cmd_commands: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.ps_scripts.append(script)
        return ("ps-out\n", None, False)

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        del environment
        self.cmd_commands.append(command)
        return ("cmd-out\n", "", 0)


def test_winrm_run_argv_uses_call_operator_not_cmd() -> None:
    """O7 finding 5: run_argv uses the PS call-operator so %APPDATA% is not
    expanded by cmd (the call-operator path is used, execute_cmd is not)."""
    sess = _RecordingPsAndCmdSession()

    def conn(**_kw: object) -> _RecordingPsAndCmdSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        argv=["echo", "%APPDATA%"],
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.ps_scripts, "run_argv must use the execute_ps call-operator path"
    assert not sess.cmd_commands, "run_argv must NOT use execute_cmd (cmd % expansion)"
    script = sess.ps_scripts[0]
    # %APPDATA% preserved literally inside PS single quotes (no cmd expansion).
    assert "%APPDATA%" in script
    assert "& 'echo' @('%APPDATA%')" in script


def test_winrm_open_runspace_pool_open_fail_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O7 finding 3: when RunspacePool.open() fails, best-effort pool.close()
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
# O7 review fixes: native environment= kwarg, orphaned-server-runspace caveat.
# ---------------------------------------------------------------------------


class _NativeEnvPsSession:
    """Mock pypsrp session whose execute_ps declares ``environment`` (like the
    real pypsrp Client), so the env is passed as a native kwarg, NOT injected.
    """

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.calls: list[tuple[str, object]] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        configuration_name: str = "Microsoft.PowerShell",
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        self.calls.append((script, environment))
        return ("ps-out\n", None, False)


class _NativeEnvCmdSession:
    """Mock pypsrp session whose execute_cmd declares ``environment``."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.calls: list[tuple[str, object]] = []

    def close(self) -> None:
        self.closed = True

    def execute_cmd(
        self,
        command: str,
        encoding: str = "437",
        environment: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        self.calls.append((command, environment))
        return ("cmd-out\n", "", 0)


def test_winrm_exec_env_native_environment_kwarg_execute_ps() -> None:
    """Production always passes environment=; native-aware sessions keep payload clean."""
    sess = _NativeEnvPsSession()

    def conn(**_kw: object) -> _NativeEnvPsSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        env={"FOO": "bar"},
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.calls, "execute_ps was called"
    script, environment = sess.calls[0]
    assert environment == {"FOO": "bar"}, "native environment= kwarg must be passed"
    assert "[Environment]::SetEnvironmentVariable" not in script, (
        "must NOT inject setters when environment is a native kwarg"
    )
    assert "Get-Date" in script


def test_winrm_exec_env_native_environment_kwarg_execute_cmd() -> None:
    """Production always passes environment= to execute_cmd."""
    sess = _NativeEnvCmdSession()

    def conn(**_kw: object) -> _NativeEnvCmdSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="whoami",
        env={"FOO": "bar"},
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.calls, "execute_cmd was called"
    command, environment = sess.calls[0]
    assert environment == {"FOO": "bar"}, "native environment= kwarg must be passed"
    assert 'set "FOO=bar"' not in command, (
        "must NOT inject `set` prefix when environment is a native kwarg"
    )
    assert "whoami" in command


class _ReleasingBlockingPsSession:
    """execute_ps that blocks on the FIRST call (simulating a hung remote
    PowerShell) and returns normally thereafter. The blocked call is released
    via ``block`` so the orphaned executor thread finishes after the test
    asserts the timeout — exercising the orphaned-server-runspace scenario."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.calls = 0
        self.block = threading.Event()

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        self.calls += 1
        if self.calls == 1:
            # First call hangs until released (orphaned-server-runspace after timeout).
            self.block.wait(timeout=30.0)
        return ("", None, False)


def test_winrm_exec_timeout_second_call_does_not_silently_hang() -> None:
    """O7 review MED #1: after a timed-out run_command, a SECOND run_command on
    the same endpoint does NOT silently hang — it returns promptly (success or
    clear failure), exercising the orphaned-server-runspace caveat."""
    sess = _ReleasingBlockingPsSession()

    def conn(**_kw: object) -> _ReleasingBlockingPsSession:
        return sess

    t0 = time.monotonic()
    r = exec_ops.run(
        ep="lab-win",
        command="Read-Host 'hang'",
        timeout=1.0,
        home=FIXTURES,
        connector=conn,
    )
    elapsed1 = time.monotonic() - t0
    assert r.status == "timeout"
    assert elapsed1 < 3.0
    # Release the orphaned first call so its executor thread finishes.
    sess.block.set()
    time.sleep(0.1)

    # Second call on the same endpoint must return promptly (no silent hang).
    t1 = time.monotonic()
    r2 = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        timeout=2.0,
        home=FIXTURES,
        connector=conn,
    )
    elapsed2 = time.monotonic() - t1
    assert elapsed2 < 3.0, f"second call silently hung: {elapsed2}s"
    assert r2.status in ("ok", "fail", "timeout", "error")
    # Defensive: release any lingering block.
    sess.block.set()


# ---------------------------------------------------------------------------
# O11 Gap 3/4/6: production-path coverage flagged untested by the original
# review but NOT covered by O7 — runspace_invoke _format_ps_errors (with a
# non-empty error stream), _format_ps_output edge cases, runspace adapter
# wrap for pool vs invoke handles, and run_argv's execute_cmd
# fallback when execute_ps is absent.
# ---------------------------------------------------------------------------


def test_winrm_format_ps_output_edge_cases() -> None:
    """Gap 3: _format_ps_output (runspace_invoke stdout formatter) handles
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
    """Gap 3: _format_ps_errors (runspace_invoke stderr formatter) joins the
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


class _ErrStreams:
    """Minimal stand-in for pypsrp PSDataStreams with a populated error list."""

    def __init__(self, errors: list[object] | None) -> None:
        self.error = errors


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
        self.streams = _ErrStreams(list(_ErrPowerShell.errors or []))
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
    branch — exercising ``_format_ps_errors`` on the production path."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        self.location = location
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RunspaceOnlySession:
    """WinRM session exposing only ``open_runspace`` → a no-invoke handle."""

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
    """Gap 3: on the real-pypsrp runspace_invoke path, _format_ps_errors is
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
    # open_runspace always wraps: no-invoke handle → pool adapter.
    assert isinstance(handle, PypsrpPoolRunspaceAdapter)
    assert handle.inner is sess.handle

    # timeout_s=None → direct ps.invoke() (no async-bridge watchdog), keeping
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
    """open_runspace classification: invoke API → InvokeRunspaceAdapter;
    no invoke (pypsrp pool / duck pool) → PypsrpPoolRunspaceAdapter.
    Idempotent for already-adapted handles."""
    # Real pypsrp RunspacePool has no invoke → pool adapter.
    real_pool = RunspacePool.__new__(RunspacePool)
    adapted_pool = _adapt_runspace_handle(real_pool, default_location=r"C:\Users\mock")
    assert isinstance(adapted_pool, PypsrpPoolRunspaceAdapter)
    assert adapted_pool.inner is real_pool

    # Mock runspace: has .invoke → invoke adapter.
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

    # Duck-type pool: no .invoke → pool adapter.
    class _DuckPool:
        min_runspaces = 1
        max_runspaces = 4

    duck = _DuckPool()
    assert isinstance(_adapt_runspace_handle(duck), PypsrpPoolRunspaceAdapter)

    # Already-adapted: returned as-is.
    assert _adapt_runspace_handle(adapted_mock) is adapted_mock
    assert _adapt_runspace_handle(adapted_pool) is adapted_pool


def test_winrm_run_argv_falls_back_to_execute_cmd() -> None:
    """Gap 6: run_argv on a session exposing execute_cmd but NOT execute_ps
    falls back to cmd shell-join (``_win_quote``) + execute_cmd — the
    documented fallback when the PowerShell call-operator path is unavailable.
    Complements test_winrm_run_argv_uses_call_operator_not_cmd (which proves
    execute_ps is PREFERRED when both are present)."""
    sess = _RecordingCmdSession()  # only execute_cmd, no execute_ps / run_argv

    def conn(**_kw: object) -> _RecordingCmdSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        argv=["echo", "with space", "plain"],
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert sess.commands, "execute_cmd was called as the run_argv fallback"
    payload = sess.commands[0]
    # argv is shell-joined via _win_quote (spaces → double-quoted) + cwd wrap.
    assert 'echo "with space" plain' in payload
    assert "cmd-out" in (r.body or "")

# ---------------------------------------------------------------------------
# High-level run_command / run_argv wall-clock timeout (same mapping as oneshot)
# ---------------------------------------------------------------------------


class _BlockingHighLevelSession:
    """High-level session whose run_command / run_argv hang (ignore timeout_s)."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.block = threading.Event()

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
        del command, cwd, timeout_s, env
        self.block.wait(timeout=30.0)
        return ExecResult(exit_code=0, stdout="late\n", cwd=self.cwd)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del argv, cwd, timeout_s, env
        self.block.wait(timeout=30.0)
        return ExecResult(exit_code=0, stdout="late\n", cwd=self.cwd)


def test_winrm_high_level_run_command_timeout_enforced() -> None:
    """High-level run_command path maps wall-clock timeout to timed_out ExecResult."""
    sess = _BlockingHighLevelSession()

    t0 = time.monotonic()
    r = exec_ops.run(
        ep="lab-win",
        command="Read-Host hang",
        timeout=1.0,
        home=FIXTURES,
        connector=lambda **_k: sess,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "timeout"
    assert r.fields.get("exit") == -1
    assert elapsed < 3.0
    sess.block.set()


def test_winrm_high_level_run_argv_timeout_enforced() -> None:
    """High-level run_argv path maps wall-clock timeout to timed_out ExecResult."""
    sess = _BlockingHighLevelSession()

    t0 = time.monotonic()
    r = exec_ops.run(
        ep="lab-win",
        argv=["hang.exe"],
        timeout=1.0,
        home=FIXTURES,
        connector=lambda **_k: sess,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "timeout"
    assert r.fields.get("exit") == -1
    assert elapsed < 3.0
    sess.block.set()
