"""Service tests: WinRM exec command/argv/env/cwd, coerce, native-exit."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from _winrm_session_fake import _MockWinRMSession

from mcp_remote_control.core import exec_ops
from mcp_remote_control.endpoint import get_registry
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.shell_wrap import wrap_with_cwd
from mcp_remote_control.transport.winrm import (
    PypsrpPoolRunspaceAdapter,
    WinRMTransport,
    _EXIT_MARKER,
    _append_ps_exit_probe,
    _coerce_exec_result,
    _exit_code_from_ps,
    _ps_result_to_exec,
    _split_exit_marker,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
FAKE_PASSWORD = "dummy-winrm-password"


# ---------------------------------------------------------------------------
# Mock session helpers
# ---------------------------------------------------------------------------


def _ok_connector(**kwargs: object) -> _MockWinRMSession:
    # Ensure password is present for real-path wiring but never asserted in output.
    assert "host" in kwargs
    assert "username" in kwargs
    return _MockWinRMSession()


def _fail_connector(**_kwargs: object) -> None:
    raise TransportError("CONNECT_FAILED", "mock winrm refused")


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
    # script -> run_argv on transport; mock returns joined argv
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
        # Test double rewrites script when environment= is passed so env-setter
        # tests can observe the payload effect (prod passes environment= only).
        if environment:
            setters: list[str] = []
            for name, value in environment.items():
                n = str(name).replace("'", "''")
                v = "" if value is None else str(value).replace("'", "''")
                setters.append(
                    f"[Environment]::SetEnvironmentVariable('{n}','{v}','Process')"
                )
            script = "; ".join(setters) + "; " + script
        self.scripts.append(script)
        # The oneshot path appends the exit probe, so a real payload carries its
        # marker; a session that returns output without one means the probe did
        # not run and the transport reports an unknown exit code.
        return (f"ps-out\n{_EXIT_MARKER}0\n", None, False)


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
        # Test double prefixes set NAME=VALUE when environment= is passed.
        if environment:
            prefixes = [
                f'set "{name}={"" if value is None else value}"'
                for name, value in environment.items()
            ]
            command = " && ".join(prefixes) + " && " + command
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
    """Per-call timeout enforced on the pypsrp execute_ps path."""
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
        return (f"ps-out\n{_EXIT_MARKER}0\n", None, False)

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
    """run_argv uses the PS call-operator so %APPDATA% is not
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


# ---------------------------------------------------------------------------
# Native environment= kwarg, orphaned-server-runspace caveat.
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
        return (f"ps-out\n{_EXIT_MARKER}0\n", None, False)


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
    asserts the timeout - exercising the orphaned-server-runspace scenario."""

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
    """After a timed-out run_command, a SECOND run_command on
    the same endpoint does NOT silently hang - it returns promptly (success or
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
# PowerShell+cmd cwd wrap via shell_wrap + coerce_cwd_path
# ---------------------------------------------------------------------------

def test_winrm_cwd_wrap_parity_with_shell_wrap() -> None:
    """WinRM uses shell_wrap.wrap_with_cwd for PS/cmd (quotes, spaces, apostrophe)."""
    samples = [
        r"C:\Users\a",
        r"C:\Users\My Documents",
        r"C:\Users\O'Brien",
        r'C:\path\with"quote',
    ]
    ps_body = "Get-ChildItem"
    cmd_body = "dir"
    for path in samples:
        ps_out = wrap_with_cwd(ps_body, path, shell_family="powershell")
        assert "-ErrorAction Stop" in ps_out
        assert ps_out.endswith(ps_body)
        assert "Set-Location -LiteralPath '" in ps_out
        # Apostrophe doubling for PS single-quoted paths
        if "'" in path:
            assert "''" in ps_out

        cmd_out = wrap_with_cwd(cmd_body, path, shell_family="cmd")
        assert cmd_out.startswith("cd /d ")
        assert " && " in cmd_out
        assert " & " not in cmd_out
        assert cmd_out.endswith(cmd_body)
        # shell_wrap always double-quotes the path; embedded " -> ""
        if '"' in path:
            assert '""' in cmd_out

    # Identity when cwd is missing / empty / non-path
    assert wrap_with_cwd(ps_body, None, shell_family="powershell") == ps_body
    assert wrap_with_cwd(ps_body, "", shell_family="powershell") == ps_body
    assert wrap_with_cwd(cmd_body, None, shell_family="cmd") == cmd_body


def test_winrm_coerce_cwd_rejects_bool_true_no_ps_wrap() -> None:
    """Bool True / string 'True' must not produce Set-Location junk wraps."""
    sess = _RecordingPsSession()
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    # Bleed-through: transport.cwd set to cap bool (must not wrap).
    t.cwd = True  # type: ignore[assignment]
    r = t.run_command("Write-Output 'NO_WRAP'")
    assert sess.scripts, "execute_ps was called"
    payload = sess.scripts[-1]
    assert "Set-Location" not in payload
    assert "cd True" not in payload
    assert "NO_WRAP" in payload
    assert r.cwd is None or r.cwd != "True"

    # Explicit non-path string cwd also skips wrap.
    r2 = t.run_command("Write-Output 'STILL_NO'", cwd="True")  # type: ignore[arg-type]
    payload2 = sess.scripts[-1]
    assert "Set-Location" not in payload2
    assert "STILL_NO" in payload2
    assert r2.exit_code == 0


def test_winrm_coerce_cwd_rejects_bool_true_no_cmd_wrap() -> None:
    """Bool/string True on execute_cmd path must not produce `cd True`."""
    sess = _RecordingCmdSession()
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    t.cwd = True  # type: ignore[assignment]
    r = t.run_command("whoami")
    assert sess.commands, "execute_cmd was called"
    payload = sess.commands[-1]
    assert "cd " not in payload.lower() or "cd /d" not in payload.lower()
    assert "cd True" not in payload
    assert payload == "whoami" or payload.endswith("whoami")
    # Body unchanged (no cwd prefix)
    assert not payload.startswith("cd ")
    assert r.cwd is None or r.cwd != "True"

    r2 = t.run_command("echo ok", cwd="False")  # type: ignore[arg-type]
    assert not sess.commands[-1].startswith("cd ")
    assert "echo ok" in sess.commands[-1]
    assert r2.exit_code == 0


def test_winrm_run_argv_coerce_cwd_no_junk_wrap() -> None:
    """run_argv also coerces cwd before PS call-operator wrap."""
    sess = _RecordingPsSession()
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()
    r = t.run_argv(["cmd.exe", "/c", "echo", "x"], cwd=True)  # type: ignore[arg-type]
    assert sess.scripts, "execute_ps was called"
    payload = sess.scripts[-1]
    assert "Set-Location" not in payload
    assert "cd True" not in payload
    assert "&" in payload  # call-operator body still present
    assert r.exit_code == 0
    # Valid path still wraps via shell_wrap
    r_ok = t.run_argv(["hostname"], cwd=r"C:\Users\mock")
    assert "Set-Location -LiteralPath 'C:\\Users\\mock' -ErrorAction Stop" in sess.scripts[-1]
    assert r_ok.cwd == r"C:\Users\mock"


def test_winrm_failed_set_location_does_not_run_body() -> None:
    """Mock: failed Set-Location with -ErrorAction Stop skips body; exit non-zero.

    Simulates PS terminating-error short-circuit: when the wrap carries Stop and
    the path is missing, the mock does not execute the user body and reports
    had_errors. Success path still prefixes and runs the body.
    """

    class _ErrStream:
        error = ["Cannot find path 'C:\\missing\\nope' because it does not exist."]

    class _CwdAwarePsSession:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\mock"
            self.home = r"C:\Users\mock"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.scripts: list[str] = []
            self.body_ran: list[bool] = []

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
            # Identity / probe paths may call execute_ps without user body marker.
            if "BODY_MARKER" not in script and "SUCCESS_BODY" not in script:
                return ("", None, False)

            # Parse Set-Location from wrap (before first ';').
            first = script.split(";", 1)[0]
            if "Set-Location" in first and "-ErrorAction Stop" in first:
                if r"C:\missing\nope" in first:
                    # Terminating Set-Location: body after ';' not reached.
                    self.body_ran.append(False)
                    return ("", _ErrStream(), True)
            # Successful location or no wrap: body runs.
            self.body_ran.append(True)
            if "SUCCESS_BODY" in script:
                return (f"ok-out\n{_EXIT_MARKER}0", None, False)
            return (f"BODY_MARKER\n{_EXIT_MARKER}0", None, False)

    sess = _CwdAwarePsSession()
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: sess,
    )
    t.connect()

    # Failure: missing cwd -> body not run, non-zero exit, cwd reports request.
    bad = r"C:\missing\nope"
    fail = t.run_command("Write-Output 'BODY_MARKER'", cwd=bad)
    assert sess.scripts, "execute_ps was called"
    payload = sess.scripts[-1]
    assert "Set-Location -LiteralPath 'C:\\missing\\nope' -ErrorAction Stop" in payload
    assert "BODY_MARKER" in payload  # wrap still includes body text
    assert sess.body_ran[-1] is False, "failed Set-Location must not execute body"
    assert fail.exit_code != 0
    assert fail.cwd == bad  # Result.cwd matches the failed request (observable fail)
    assert "BODY_MARKER" not in (fail.stdout or "")

    # Success path: cwd wrap still prefixes and body runs.
    good = r"C:\Users\mock"
    ok = t.run_command("Write-Output 'SUCCESS_BODY'", cwd=good)
    assert sess.body_ran[-1] is True
    assert "Set-Location -LiteralPath 'C:\\Users\\mock' -ErrorAction Stop" in sess.scripts[-1]
    assert "SUCCESS_BODY" in sess.scripts[-1]
    assert ok.exit_code == 0
    assert ok.cwd == good
    assert "ok-out" in (ok.stdout or "")


def test_winrm_run_argv_falls_back_to_execute_cmd() -> None:
    """run_argv on a session exposing execute_cmd but NOT execute_ps
    falls back to cmd shell-join (``_win_quote``) + execute_cmd - the
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
    # argv is shell-joined via _win_quote (spaces -> double-quoted) + cwd wrap.
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


# ---------------------------------------------------------------------------
# _coerce_exec_result missing exit attrs -> -1 (not fake 0)
# ---------------------------------------------------------------------------

class _CoerceRaw:
    """Minimal duck-typed pypsrp/session result stand-in."""

    def __init__(self, **attrs: object) -> None:
        for k, v in attrs.items():
            setattr(self, k, v)


def test_coerce_exec_result_all_exit_attrs_missing_nonzero() -> None:
    """When every exit attr is None/missing, default to -1 (not 0)."""
    raw = _CoerceRaw(exit_code=None, exit_status=None, returncode=None, stdout=b"partial", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=r"C:\Users\Administrator")
    assert r.exit_code != 0
    assert r.exit_code == -1
    assert r.stdout == "partial"


def test_coerce_exec_result_returncode_attr_missing_defaults_minus_one() -> None:
    """Object has exit_code/status attrs set to None and no usable returncode -> -1."""
    raw = _CoerceRaw(exit_status=None, stdout=b"x", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=r"C:\tmp")
    assert r.exit_code == -1


def test_coerce_exec_result_normal_zero_preserved() -> None:
    """Regression guard: a clean exit 0 must still map to 0."""
    raw = _CoerceRaw(returncode=0, stdout=b"ok", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=r"C:\tmp")
    assert r.exit_code == 0
    assert r.stdout == "ok"


def test_coerce_exec_result_returncode_path_preserved() -> None:
    """subprocess-style returncode still wins when exit_code/exit_status unset."""
    raw = _CoerceRaw(returncode=7, stdout=b"", stderr=b"boom")
    r = _coerce_exec_result(raw, default_cwd=r"C:\tmp")
    assert r.exit_code == 7
    assert r.stderr == "boom"


def test_coerce_exec_result_exit_code_attr_zero_preserved() -> None:
    """exit_code=0 on the object is first-class success, not remapped."""
    raw = _CoerceRaw(exit_code=0, stdout=b"done\n", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=r"C:\tmp")
    assert r.exit_code == 0
    assert r.stdout == "done\n"


# ---------------------------------------------------------------------------
# execute_ps LASTEXITCODE mapping
# ---------------------------------------------------------------------------

class _NativeExitPsSession:
    """execute_ps mock: simulates remote running the exit probe.

    When the transport wraps the script with ``_append_ps_exit_probe``, the
    mock echoes ``__MRC_PS_EXIT_MARKER__{native_rc}`` so ``_ps_result_to_exec``
    maps the native code. ``had_errors`` is independent (PS error stream).
    """

    def __init__(
        self,
        *,
        native_rc: int = 7,
        had_errors: bool = False,
        user_stdout: str = "",
    ) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.scripts: list[str] = []
        self.native_rc = native_rc
        self.had_errors = had_errors
        self.user_stdout = user_stdout

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
        # Real remote: probe is last Write-Output; only emit when wrapped.
        if _EXIT_MARKER in script or "LASTEXITCODE" in script:
            body = self.user_stdout
            if body and not body.endswith("\n"):
                body += "\n"
            out = f"{body}{_EXIT_MARKER}{self.native_rc}\n"
        else:
            out = self.user_stdout
        return (out, None, self.had_errors)


def test_ps_result_to_exec_native_exit_7_not_false_zero() -> None:
    """Marker + had_errors=False -> exit_code=7 (not collapsed to 0)."""
    raw = (f"hello\n{_EXIT_MARKER}7\n", None, False)
    r = _ps_result_to_exec(raw, default_cwd=r"C:\Users\mock")
    assert r.exit_code == 7
    assert r.stdout == "hello\n"
    assert _EXIT_MARKER not in r.stdout


def test_ps_result_to_exec_pure_ps_success_exit_0() -> None:
    """Pure PS success (marker 0, no errors) stays exit_code=0."""
    raw = (f"ok-line\n{_EXIT_MARKER}0\n", None, False)
    r = _ps_result_to_exec(raw, default_cwd=r"C:\Users\mock")
    assert r.exit_code == 0
    assert "ok-line" in r.stdout
    assert _EXIT_MARKER not in r.stdout


def test_ps_result_to_exec_had_errors_forces_nonzero_when_rc_0() -> None:
    """PS had_errors still fails even if LASTEXITCODE probe says 0."""
    raw = (f"{_EXIT_MARKER}0\n", None, True)
    r = _ps_result_to_exec(raw, default_cwd=r"C:\Users\mock")
    assert r.exit_code == 1


def test_ps_result_to_exec_legacy_no_marker_had_errors_only() -> None:
    """Unwrapped mocks without exit marker keep had_errors -> 0/1 mapping."""
    r0 = _ps_result_to_exec(("out\n", None, False), default_cwd=None)
    assert r0.exit_code == 0
    r1 = _ps_result_to_exec(("out\n", None, True), default_cwd=None)
    assert r1.exit_code == 1


def test_ps_result_to_exec_list_output_strips_marker() -> None:
    """pypsrp may return output as a list of objects; marker is stripped."""
    raw = (["user-out", f"{_EXIT_MARKER}7"], None, False)
    r = _ps_result_to_exec(raw, default_cwd=None)
    assert r.exit_code == 7
    assert "user-out" in r.stdout
    assert _EXIT_MARKER not in r.stdout


def test_ps_result_to_exec_bare_list_formats_key_value() -> None:
    """Bare list of key=value lines becomes parseable stdout (not str(list))."""
    raw = [
        "language_mode=FullLanguage",
        "ps_version=5.1.19041",
        "can_file_io=True",
    ]
    r = _ps_result_to_exec(raw, default_cwd=None)
    assert r.timed_out is False
    assert "language_mode=FullLanguage" in r.stdout
    assert "ps_version=5.1.19041" in r.stdout
    assert not r.stdout.startswith("[")


def test_ps_result_to_exec_json_object_list_formats_json() -> None:
    """List of JSON objects is emitted as compact JSON lines."""
    raw = (
        [
            {
                "language_mode": "FullLanguage",
                "ps_version": "5.1.19041",
            }
        ],
        None,
        False,
    )
    r = _ps_result_to_exec(raw, default_cwd=None)
    assert '"language_mode":"FullLanguage"' in r.stdout
    assert '"ps_version":"5.1.19041"' in r.stdout


def test_split_exit_marker_and_exit_code_helpers() -> None:
    """Unit: split/parse helpers + combine logic."""
    rc, rest = _split_exit_marker(f"a\n{_EXIT_MARKER}42\n", _EXIT_MARKER)
    assert rc == 42
    assert rest == "a\n"
    assert _exit_code_from_ps(captured=0, had_errors=True) == 1
    assert _exit_code_from_ps(captured=7, had_errors=False) == 7
    assert _exit_code_from_ps(captured=None, had_errors=False) == 0
    assert "LASTEXITCODE" in _append_ps_exit_probe("Write-Output hi")
    assert _EXIT_MARKER in _append_ps_exit_probe("Write-Output hi")


def test_winrm_run_argv_cmd_exit_7_maps_native_rc() -> None:
    """run_argv(['cmd.exe','/c','exit','7']) via execute_ps -> exit_code=7."""
    sess = _NativeExitPsSession(native_rc=7, had_errors=False)

    def conn(**_kw: object) -> _NativeExitPsSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        argv=["cmd.exe", "/c", "exit", "7"],
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "fail" or r.fields.get("exit") != 0
    assert r.fields.get("exit") == 7
    assert sess.scripts, "execute_ps must be used (preferred path)"
    script = sess.scripts[0]
    assert "LASTEXITCODE" in script
    assert _EXIT_MARKER in script
    assert "& 'cmd.exe'" in script or "cmd.exe" in script


def test_winrm_run_command_pure_ps_success_exit_0() -> None:
    """Pure PS Write-Output success path remains exit 0."""
    sess = _NativeExitPsSession(
        native_rc=0, had_errors=False, user_stdout="hello-ps\n"
    )

    def conn(**_kw: object) -> _NativeExitPsSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="Write-Output 'hello-ps'",
        home=FIXTURES,
        connector=conn,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert "hello-ps" in (r.body or "")
    assert _EXIT_MARKER not in (r.body or "")
    assert sess.scripts and "LASTEXITCODE" in sess.scripts[0]


def test_pool_adapter_prepare_invoke_native_exit_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PypsrpPoolRunspaceAdapter maps LASTEXITCODE when had_errors=False."""

    class _Streams:
        error: list[object] = []

    class _Ps:
        instances: list[_Ps] = []

        def __init__(self, pool: object) -> None:
            self.pool = pool
            self.script: str | None = None
            self.had_errors = False
            self.streams = _Streams()
            self.stopped = False
            self.closed = False
            _Ps.instances.append(self)

        def add_script(self, script: str, use_local_scope: object = None) -> _Ps:
            self.script = script
            return self

        def invoke(self, input: object = None, **_kw: object) -> list[str]:
            del input, _kw
            loc = r"C:\Users\mock"
            # Simulate remote: user out + exit probe + location probe.
            return [
                "pool-user-out",
                f"{_EXIT_MARKER}7",
                f"__MRC_PS_CWD_MARKER__{loc}",
            ]

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
    run, _stop = adapter.prepare_invoke("& 'cmd.exe' @('/c','exit','7')")
    assert _Ps.instances, "PowerShell pipeline must be built"
    built = _Ps.instances[0].script or ""
    assert "LASTEXITCODE" in built
    assert _EXIT_MARKER in built
    assert "__MRC_PS_CWD_MARKER__" in built

    result = run()
    assert result.had_errors is False
    assert result.exit_code == 7
    assert "pool-user-out" in (result.stdout or "")
    assert _EXIT_MARKER not in (result.stdout or "")
    assert "__MRC_PS_CWD_MARKER__" not in (result.stdout or "")
    assert result.location == r"C:\Users\mock"


def test_pool_adapter_prepare_invoke_pure_ps_success_exit_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool pure-PS success with exit probe 0 -> exit_code=0."""

    class _Streams:
        error: list[object] = []

    class _Ps:
        def __init__(self, pool: object) -> None:
            self.pool = pool
            self.had_errors = False
            self.streams = _Streams()

        def add_script(self, script: str, use_local_scope: object = None) -> _Ps:
            return self

        def invoke(self, input: object = None, **_kw: object) -> list[str]:
            del input, _kw
            return [
                "ok",
                f"{_EXIT_MARKER}0",
                "__MRC_PS_CWD_MARKER__C:\\Users\\mock",
            ]

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("pypsrp.powershell.PowerShell", _Ps)

    class _Pool:
        location = r"C:\Users\mock"

        def close(self) -> None:
            return None

    adapter = PypsrpPoolRunspaceAdapter(_Pool(), default_location=r"C:\Users\mock")
    result = adapter.invoke("Write-Output 'ok'")
    assert result.exit_code == 0
    assert result.had_errors is False
    assert "ok" in (result.stdout or "")
