"""Service tests: exec three forms local + mock SSH (T07)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import exec_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.exec import run_script_on_transport
from mcp_remote_control.transport.base import ExecResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# local command / argv / script
# ---------------------------------------------------------------------------


def test_local_command_echo_hello() -> None:
    r = exec_ops.run(ep="local", command="echo hello", home=FIXTURES)
    assert r.kind == "exec"
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.fields.get("form") == "command"
    assert r.fields.get("ep") == "local"
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    assert r.body is not None
    assert "hello" in r.body
    text = r.render_text()
    assert text.startswith("@exec ok")
    assert "cwd=" in text
    assert "exit=0" in text
    assert "hello" in text


def test_local_argv_echo() -> None:
    r = exec_ops.run(ep="local", argv=["/bin/echo", "hello-argv"], home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("form") == "argv"
    assert r.fields.get("exit") == 0
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    assert r.body is not None
    assert "hello-argv" in r.body


def test_local_script_body_bash() -> None:
    r = exec_ops.run(
        ep="local",
        script="echo script-body-ok",
        runtime="bash",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "script"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "script-body-ok" in r.body


def test_local_script_body_python() -> None:
    r = exec_ops.run(
        ep="local",
        script="print('py-body')",
        runtime="python",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "py-body" in r.body


def test_local_script_path(tmp_path: Path) -> None:
    script = tmp_path / "hi.sh"
    script.write_text("#!/bin/sh\necho from-path\n", encoding="utf-8")
    r = exec_ops.run(
        ep="local",
        script_path=str(script),
        runtime="bash",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "from-path" in r.body


def test_local_timeout() -> None:
    r = exec_ops.run(
        ep="local",
        command="sleep 10",
        timeout=0.2,
        home=FIXTURES,
    )
    assert r.status == "timeout"
    assert r.fields.get("form") == "command"
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    text = r.render_text()
    assert text.startswith("@exec timeout")


def test_local_fail_nonzero_exit() -> None:
    r = exec_ops.run(ep="local", command="exit 7", home=FIXTURES)
    assert r.status == "fail"
    assert r.fields.get("exit") == 7
    assert r.cwd is not None


def test_local_lazy_connect_without_prior_open() -> None:
    reg = get_registry()
    assert reg.get("local") is None
    r = exec_ops.run(ep="local", command="echo lazy", home=FIXTURES)
    assert r.status == "ok"
    assert "lazy" in (r.body or "")
    ep = reg.get("local")
    assert ep is not None
    assert ep.connected is True


def test_local_cwd_absolute_when_requested(tmp_path: Path) -> None:
    r = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(tmp_path),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.cwd == str(tmp_path.resolve())
    # pwd body should match
    assert r.body is not None
    assert str(tmp_path.resolve()) in r.body or tmp_path.name in r.body


def test_missing_ep() -> None:
    r = exec_ops.run(command="echo x", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_missing_form() -> None:
    r = exec_ops.run(ep="local", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_mutual_exclusion() -> None:
    r = exec_ops.run(
        ep="local",
        command="echo a",
        argv=["echo", "b"],
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "INVALID_ARG"


# ---------------------------------------------------------------------------
# SSH mock
# ---------------------------------------------------------------------------


def test_ssh_mock_exec_command() -> None:
    class Conn:
        cwd = "/var/www"
        home = "/home/deploy"

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(
                exit_code=0,
                stdout=f"remote:{command}\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = exec_ops.run(
        ep="lab-ssh",
        command="echo hello",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("ep") == "lab-ssh"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "remote:echo hello" in r.body
    assert r.cwd is not None
    # profile defaults.cwd = /var/www
    assert r.cwd == "/var/www" or r.cwd.startswith("/")


def test_ssh_mock_exec_argv() -> None:
    class Conn:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(
                exit_code=0,
                stdout=" ".join(argv) + "\n",
                cwd=cwd or "/tmp",
            )

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    r = exec_ops.run(
        ep="lab-ssh",
        argv=["/bin/echo", "ssh-argv"],
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert "ssh-argv" in (r.body or "")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exec_command_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local", "--", "command", "echo hello"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@exec ok" in out
    assert "cwd=" in out
    assert "hello" in out
    assert "exit=0" in out


def test_cli_exec_command_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local", "--command", "echo via-flag"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "via-flag" in out


def test_cli_exec_argv_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["exec", "--ep", "local", "--", "argv", "/bin/echo", "cli-argv"]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "cli-argv" in out


def test_cli_exec_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["exec", "--ep", "local", "--json", "--command", "echo json-hi"]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["kind"] == "exec"
    assert data["status"] == "ok"
    assert data["exit"] == 0
    assert "json-hi" in (data.get("body") or "")
    assert data.get("cwd")
    assert Path(data["cwd"]).is_absolute()


def test_cli_exec_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "exec",
            "--ep",
            "local",
            "--timeout",
            "0.2",
            "--command",
            "sleep 10",
        ]
    )
    assert code != EXIT_OK
    out = capsys.readouterr().out
    assert "@exec timeout" in out


def test_cli_exec_missing_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@exec error" in out


# ---------------------------------------------------------------------------
# C3: dead local_path_read SSH-local-file-upload feature fully removed
# ---------------------------------------------------------------------------


def test_run_script_on_transport_no_local_path_read_param() -> None:
    """The dead local_path_read kwarg is removed from run_script_on_transport.

    It was always False at the only dispatch site (exec_ops._dispatch), so
    the `if path is not None and local_path_read and body is None:` branch
    was unreachable. C3 deletes the kwarg and the dead branch together.
    """
    sig = inspect.signature(run_script_on_transport)
    assert "local_path_read" not in sig.parameters
    # The remaining params are unchanged.
    expected = {
        "transport",
        "body",
        "path",
        "runtime",
        "args",
        "cwd",
        "timeout_s",
        "env",
        "dialect",
    }
    assert set(sig.parameters) == expected


def test_run_script_on_transport_rejects_local_path_read_kwarg() -> None:
    """Passing local_path_read= must raise TypeError now that the kwarg is
    gone (no silent acceptance of stale caller code)."""
    with pytest.raises(TypeError):
        run_script_on_transport(
            object(),  # transport unused: TypeError fires before body
            body="echo hi",
            local_path_read=True,  # pyright: ignore[reportCallIssue]
        )


# ---------------------------------------------------------------------------
# WinRM ps_oneshot gate: NoLanguage hosts fail pre-flight with UNSUPPORTED;
# ConstrainedLanguage (ps_oneshot=true) and probe-less sessions still run.
# ---------------------------------------------------------------------------


class _MockWinRMSession:
    """Injectable winrm session: identity seeds only, no network sockets."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
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


class _NoLanguageWinRMSession(_MockWinRMSession):
    """Identity + NoLanguage (JEA) capability seeds → ps_oneshot=false."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "NoLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


class _ConstrainedWinRMSession(_MockWinRMSession):
    """ConstrainedLanguage seeds → ps_oneshot=true but ps_script_fs=false."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "ConstrainedLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


def _winrm_connector(
    session: _MockWinRMSession,
) -> Callable[..., _MockWinRMSession]:
    def connector(**_kwargs: object) -> _MockWinRMSession:
        return session

    return connector


def test_winrm_exec_ps_oneshot_false_nolanguage_unsupported() -> None:
    """winrm_ps ps_oneshot=false gates exec before dispatch.

    On a NoLanguage (JEA) host the user's PowerShell would hard-fail
    mid-exec; exec must instead return a pre-flight UNSUPPORTED carrying
    the reported lang_mode, and never touch the transport exec surface.
    """
    sess = _NoLanguageWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.kind == "exec"
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("ep") == "lab-win"
    assert r.fields.get("form") == "command"
    assert r.fields.get("lang_mode") == "NoLanguage"
    hint = r.hint or ""
    assert "language mode" in hint
    assert "ps_oneshot" in hint
    text = r.render_text()
    assert "UNSUPPORTED" in text
    assert "lang_mode=NoLanguage" in text
    # The transport's exec surface was NOT called.
    assert sess.commands == []
    assert sess.argvs == []
    # Pin the gate input: the probe-derived flag is present and false.
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    winrm_ps = ep.transport.meta.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_oneshot") is False
    assert winrm_ps.get("language_mode") == "NoLanguage"


@pytest.mark.parametrize("form", ["command", "argv", "script"])
def test_winrm_exec_ps_oneshot_false_gates_all_forms(form: str) -> None:
    """The gate sits before dispatch, so all three exec forms are gated."""
    sess = _NoLanguageWinRMSession()
    connector = _winrm_connector(sess)
    if form == "command":
        r = exec_ops.run(
            ep="lab-win", command="Get-Date", home=FIXTURES, connector=connector
        )
    elif form == "argv":
        r = exec_ops.run(
            ep="lab-win",
            argv=["ipconfig", "/all"],
            home=FIXTURES,
            connector=connector,
        )
    else:
        r = exec_ops.run(
            ep="lab-win",
            script="Write-Output hi",
            runtime="powershell",
            home=FIXTURES,
            connector=connector,
        )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("form") == form
    assert r.fields.get("lang_mode") == "NoLanguage"
    assert sess.commands == []
    assert sess.argvs == []


def test_winrm_exec_ps_oneshot_true_constrained_runs() -> None:
    """ConstrainedLanguage reports ps_oneshot=true with ps_script_fs=false.

    Exec is explicitly not gated on ps_script_fs, so the command runs.
    """
    sess = _ConstrainedWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "command"
    assert r.fields.get("exit") == 0
    assert "winrm-out:Get-Date" in (r.body or "")
    assert sess.commands == ["Get-Date"]
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    winrm_ps = ep.transport.meta.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_oneshot") is True
    assert winrm_ps.get("ps_script_fs") is False


def test_winrm_exec_winrm_ps_absent_legacy_allow() -> None:
    """Identity-only seeds leave meta without winrm_ps (lab / probe off):
    exec keeps the historical allow path and runs."""
    sess = _MockWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert "winrm-out:Get-Date" in (r.body or "")
    assert sess.commands == ["Get-Date"]
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.meta.get("winrm_ps") is None


def test_winrm_exec_incomplete_probe_still_allows_oneshot() -> None:
    """Incomplete winrm_ps (ps_oneshot true, probe failed) must not gate exec.

    A hung/unparseable capability probe marks script_fs/runspace closed but
    keeps oneshot allowed — inject that shape via transport.meta after open.
    """
    from mcp_remote_control.transport.winrm import _incomplete_winrm_ps

    sess = _MockWinRMSession()
    # First open with identity-only seeds (no winrm_ps), then inject incomplete.
    r0 = exec_ops.run(
        ep="lab-win",
        command="hostname",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r0.status == "ok"
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    incomplete = _incomplete_winrm_ps(error="capability probe timed out")
    ep.transport.meta["winrm_ps"] = incomplete
    assert incomplete["ps_oneshot"] is True
    assert incomplete["ps_script_fs"] is False
    assert incomplete["ps_runspace"] is False

    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.code is None or r.code != "UNSUPPORTED"
    assert r.fields.get("exit") == 0
    assert "Get-Date" in sess.commands
