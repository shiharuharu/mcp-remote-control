"""Service tests: WinRM screen CAP_DENIED (no forged PTY frames)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry, reset_registry
from mcp_remote_control.endpoint.registry import Endpoint
from mcp_remote_control.screen.registry import (
    get_screen_registry,
    reset_screen_registry,
)
from mcp_remote_control.transport.local import LocalTransport

_CONFIG_HOME = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Prefer a plain POSIX shell so CI/dev machines without fancy zshrc stay stable.
_SIMPLE_SHELL = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


@pytest.fixture(autouse=True)
def _clean_regs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(_CONFIG_HOME))
    reset_registry()
    reset_screen_registry()
    yield
    reset_screen_registry()
    reset_registry()


# ---------------------------------------------------------------------------
# WinRM lacks screen capability: CAP_DENIED, never a forged PTY frame
# ---------------------------------------------------------------------------


def _mock_winrm_connector(**_kwargs: object):
    """Minimal injectable WinRM session (no network); mirrors test_winrm."""
    from mcp_remote_control.transport.base import ExecResult

    class _MockWinRMSession:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"

        def close(self) -> None:
            return None

        def run_command(self, command: str, **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout=f"out:{command}\n", stderr="", cwd=self.cwd)

        def run_argv(self, argv: list[str], **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout=" ".join(argv) + "\n", cwd=self.cwd)

    return _MockWinRMSession()


def test_no_screen_cap_returns_cap_denied() -> None:
    """Inject an endpoint with screen=false (winrm-like caps)."""
    ensure_endpoint("local", home=_CONFIG_HOME)
    reg = get_registry()
    ep = reg.get("local")
    assert ep is not None
    # Flip caps to deny screen (simulates winrm matrix).
    ep.caps = {
        "exec": True,
        "fs": True,
        "screen": False,
        "ps": True,
    }

    r = screen_ops.open_screen(
        ep="local",
        home=_CONFIG_HOME,
        shell=_SIMPLE_SHELL,
        settle_s=0.1,
    )
    assert r.status == "error"
    assert r.code == "CAP_DENIED"
    assert r.body is None  # no forged frame
    assert "lacks screen capability" in (r.fields.get("msg") or "").lower()
    text = r.render_text()
    assert "CAP_DENIED" in text
    assert get_screen_registry().list_open() == []


def test_winrm_transport_open_fails_cap_denied() -> None:
    """WinRM transport cannot open screen; caps.screen=false -> CAP_DENIED."""
    # Register a synthetic winrm endpoint without going through profile connect.
    reg = get_registry()
    transport = LocalTransport()
    transport.connect()
    # Pretend winrm with no screen cap.
    fake = Endpoint(
        name="win-fake",
        transport_name="winrm",
        caps={"exec": True, "fs": True, "screen": False, "ps": True},
        connected=True,
        transport=transport,
        cwd="/",
    )
    reg._endpoints["win-fake"] = fake

    r = screen_ops.open_screen(ep="win-fake", home=_CONFIG_HOME, settle_s=0.05)
    assert r.status == "error"
    assert r.code == "CAP_DENIED"
    assert r.body is None
    assert "lacks screen capability" in (r.fields.get("msg") or "").lower()
    assert get_screen_registry().list_open() == []


def test_lab_win_screen_open_cap_denied_no_session() -> None:
    """lab-win + mock winrm: screen open CAP_DENIED, no session, no frame."""
    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]

    r = screen_ops.open_screen(
        ep="lab-win",
        home=_CONFIG_HOME,
        connector=_mock_winrm_connector,
        settle_s=0.05,
    )
    assert r.status == "error"
    assert r.code == "CAP_DENIED"
    assert r.fields.get("ep") == "lab-win"
    assert r.fields.get("transport") == "winrm"
    assert r.fields.get("op") == "open"
    # No forged PTY frame body on the hard-fail path.
    assert r.body is None
    assert "cur" not in r.fields
    assert "gen" not in r.fields
    assert "hash" not in r.fields
    # Agent track greppable for CAP_DENIED + lacks screen capability.
    text = r.render_text()
    assert "CAP_DENIED" in text
    assert "lacks screen capability" in (r.fields.get("msg") or "").lower()
    assert text.startswith("@screen error")
    assert "code=CAP_DENIED" in text
    # JSON track likewise.
    data = json.loads(r.render_json())
    assert data["status"] == "error"
    assert data["code"] == "CAP_DENIED"
    assert "body" not in data or data.get("body") in (None, "")
    # Must not create a screen session.
    assert get_screen_registry().list_open() == []
    listed = screen_ops.list_screens()
    assert listed.status == "ok"
    assert listed.fields.get("n") == 0


def test_lab_win_screen_run_dispatch_and_send_no_frame() -> None:
    """run(op=open|send) on winrm: open CAP_DENIED; send never sees a session."""
    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]

    opened = screen_ops.run(
        op="open",
        ep="lab-win",
        home=_CONFIG_HOME,
        connector=_mock_winrm_connector,
        settle_s=0.05,
    )
    assert opened.status == "error"
    assert opened.code == "CAP_DENIED"
    assert opened.body is None
    assert "CAP_DENIED" in opened.render_text()
    assert get_screen_registry().list_open() == []

    # No session exists -> send cannot forge a frame (SCREEN_NOT_FOUND, not ok+body).
    sent = screen_ops.run(op="send", id="scr_nonexistent")
    assert sent.status == "error"
    assert sent.code == "SCREEN_NOT_FOUND"
    assert sent.body is None
    assert get_screen_registry().list_open() == []


def test_cli_lab_win_screen_open_cap_denied(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI: screen open --ep lab-win -> greppable CAP_DENIED, non-zero exit."""
    from mcp_remote_control.cli_cmds import EXIT_VALIDATION

    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]
    code = main(["screen", "open", "--ep", "lab-win"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "CAP_DENIED" in out
    assert "@screen error" in out
    assert get_screen_registry().list_open() == []
