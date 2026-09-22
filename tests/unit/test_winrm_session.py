"""Unit tests for WinRMTransport mock connector / run_argv / auth heuristic."""

from __future__ import annotations

import pytest

from mcp_remote_control.transport import (
    ExecResult,
    TransportError,
)
from mcp_remote_control.transport.winrm import WinRMTransport


# ---------------------------------------------------------------------------
# WinRM mock connector / argv / auth heuristic.
# ---------------------------------------------------------------------------


def test_winrm_mock_connector_success() -> None:
    class Sess:
        cwd = r"C:\Users\Admin"
        home = r"C:\Users\Admin"
        os = "windows"
        shell = "powershell"
        ps_version = "5.1"

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout=f"ps:{command}", cwd=cwd or self.cwd)

    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> Sess:
        seen.update({k: v for k, v in kwargs.items() if k != "password"})
        return Sess()

    t = WinRMTransport(
        host="10.0.0.20",
        port=5985,
        username="Administrator",
        password="secret-should-not-appear",
        connector=connector,
    )
    t.connect()
    assert t.is_connected()
    assert seen["host"] == "10.0.0.20"
    assert seen["username"] == "Administrator"
    assert t.cwd == r"C:\Users\Admin"
    assert "password" not in repr(t)
    assert "secret" not in repr(t)
    r = t.run_command("Get-Date", cwd=r"C:\Temp")
    assert r.exit_code == 0
    assert r.stdout == "ps:Get-Date"
    assert r.cwd == r"C:\Temp"
    probe = t.collect_probe()
    assert probe.get("os") == "windows"
    assert probe.get("status") == "ok"
    t.close()
    assert t.is_connected() is False


def test_winrm_connector_failure_is_connect_failed() -> None:
    def connector(**_kwargs: object) -> None:
        raise ConnectionRefusedError("winrm port closed")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "CONNECT_FAILED"
    assert t.is_connected() is False


def test_winrm_auth_failed_passthrough() -> None:
    def connector(**_kwargs: object) -> None:
        raise TransportError("AUTH_FAILED", "bad password")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "AUTH_FAILED"


def test_winrm_auth_heuristic_from_generic_exc() -> None:
    def connector(**_kwargs: object) -> None:
        raise RuntimeError("401 Unauthorized: credentials rejected")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "AUTH_FAILED"


def test_winrm_mock_run_argv() -> None:
    class Sess:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout="|".join(argv), cwd=cwd or "C:\\")

    t = WinRMTransport(
        host="h",
        username="u",
        connector=lambda **_k: Sess(),
    )
    t.connect()
    r = t.run_argv(["ipconfig", "/all"])
    assert r.exit_code == 0
    assert r.stdout == "ipconfig|/all"


def test_winrm_not_connected_exec() -> None:
    t = WinRMTransport(host="h", username="u", connector=lambda **_k: object())
    with pytest.raises(TransportError) as ei:
        t.run_command("x")
    assert ei.value.code == "NOT_CONNECTED"
