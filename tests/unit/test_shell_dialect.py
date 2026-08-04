"""Unit tests: ShellDialect resolve + probe registry (W7 T21)."""

from __future__ import annotations

from mcp_remote_control.screen.buffer import PWD_MARKER
from mcp_remote_control.shell.dialect import (
    BUSYBOX_PROBE_MAX_LEN,
    CMD,
    DIALECT_PROBES,
    FISH,
    POSIX_BASH,
    POSIX_BUSYBOX,
    POSIX_SH,
    POSIX_ZSH,
    POWERSHELL,
    UNKNOWN,
    default_runtime_for_dialect,
    probe_cmd_for_dialect,
    resolve_dialect,
    transport_family_for_dialect,
)


def test_resolve_common_bases() -> None:
    assert resolve_dialect(shell_base="bash") == POSIX_BASH
    assert resolve_dialect(shell_base="zsh") == POSIX_ZSH
    assert resolve_dialect(shell_base="dash") == POSIX_SH
    assert resolve_dialect(shell_base="sh") == POSIX_SH
    assert resolve_dialect(shell_base="ash") == POSIX_SH
    assert resolve_dialect(shell_base="fish") == FISH
    assert resolve_dialect(shell_base="cmd") == CMD
    assert resolve_dialect(shell_base="pwsh") == POWERSHELL
    assert resolve_dialect(shell_base="powershell") == POWERSHELL
    assert resolve_dialect(shell_base="csh") == UNKNOWN


def test_resolve_busybox() -> None:
    assert (
        resolve_dialect(shell_base="sh", busybox=True) == POSIX_BUSYBOX
    )
    assert (
        resolve_dialect(
            shell_base="sh",
            flags={"busybox": "/bin/busybox"},
        )
        == POSIX_BUSYBOX
    )
    assert (
        resolve_dialect(
            shell_base="sh",
            flags={"busybox_banner": "BusyBox v1.36.1"},
        )
        == POSIX_BUSYBOX
    )
    assert (
        resolve_dialect(shell_path="/bin/busybox") == POSIX_BUSYBOX
    )
    # plain sh without busybox signal stays posix-sh
    assert resolve_dialect(shell_base="sh", busybox=False) == POSIX_SH


def test_resolve_from_path() -> None:
    assert resolve_dialect(shell_path="/usr/local/bin/bash") == POSIX_BASH
    assert resolve_dialect(shell_path="/bin/zsh") == POSIX_ZSH
    assert resolve_dialect(shell_path=r"C:\Windows\System32\cmd.exe") == CMD
    assert resolve_dialect(shell_path="/usr/bin/pwsh") == POWERSHELL


def test_resolve_family_coarse() -> None:
    assert resolve_dialect(shell_family="posix") == POSIX_SH
    assert resolve_dialect(shell_family="posix", busybox=True) == POSIX_BUSYBOX
    assert resolve_dialect(shell_family="cmd") == CMD
    assert resolve_dialect(shell_family="powershell") == POWERSHELL


def test_probe_cmds_registered() -> None:
    for d in (POSIX_BASH, POSIX_ZSH, POSIX_SH, POSIX_BUSYBOX, CMD, POWERSHELL):
        cmd = probe_cmd_for_dialect(d)
        assert cmd is not None, d
        assert PWD_MARKER in cmd
        spec = DIALECT_PROBES[d]
        assert spec is not None
        assert spec.fits()


def test_zsh_probe_safe() -> None:
    cmd = probe_cmd_for_dialect(POSIX_ZSH)
    assert cmd is not None
    assert cmd.startswith(" ")
    assert "fc -p" in cmd and "fc -P" in cmd
    assert "set +o history" not in cmd
    assert "history -d" not in cmd
    assert cmd.rstrip().endswith(":")


def test_bash_probe() -> None:
    cmd = probe_cmd_for_dialect(POSIX_BASH)
    assert cmd is not None
    assert cmd.startswith(" ")
    assert "set +o history" in cmd
    assert "history -d" not in cmd
    assert "fc -p" not in cmd


def test_busybox_probe_shortest() -> None:
    cmd = probe_cmd_for_dialect(POSIX_BUSYBOX)
    assert cmd is not None
    assert len(cmd) <= BUSYBOX_PROBE_MAX_LEN
    assert "fc" not in cmd
    assert "set +o history" not in cmd
    assert "history -d" not in cmd
    assert "[[" not in cmd
    assert cmd.startswith(" ")
    assert PWD_MARKER in cmd


def test_sh_probe_no_fc() -> None:
    cmd = probe_cmd_for_dialect(POSIX_SH)
    assert cmd is not None
    assert "fc -p" not in cmd
    assert "set +o history" not in cmd
    assert len(cmd) <= BUSYBOX_PROBE_MAX_LEN


def test_skip_unknown_and_fish() -> None:
    assert probe_cmd_for_dialect(UNKNOWN) is None
    assert probe_cmd_for_dialect(FISH) is None
    assert probe_cmd_for_dialect(None) is None
    assert probe_cmd_for_dialect("nope") is None


def test_default_runtime() -> None:
    assert default_runtime_for_dialect(POSIX_BASH) == "bash"
    assert default_runtime_for_dialect(POSIX_BUSYBOX) == "sh"
    assert default_runtime_for_dialect(POSIX_SH) == "sh"
    assert default_runtime_for_dialect(UNKNOWN) == "sh"
    assert default_runtime_for_dialect(POWERSHELL) == "pwsh"
    assert default_runtime_for_dialect(CMD) == "cmd"


def test_transport_family() -> None:
    assert transport_family_for_dialect(POSIX_ZSH) == "posix"
    assert transport_family_for_dialect(CMD) == "cmd"
    assert transport_family_for_dialect(POWERSHELL) == "powershell"


def test_session_probe_routing() -> None:
    """cwd_probe picks template from session.dialect (T23)."""
    from mcp_remote_control.screen.cwd_probe import _probe_cmd_for_session
    from mcp_remote_control.screen.session import ScreenSession

    class _Dead:
        cols = 80
        rows = 24
        cwd = None

        def is_alive(self) -> bool:
            return True

        def exit_code(self) -> None:
            return None

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def resize(self, cols: int, rows: int) -> None:
            pass

        def drain_for(self, seconds: float, *, on_data=None) -> int:
            return 0

        def close(self) -> None:
            pass

    def _sess(dialect: str) -> ScreenSession:
        return ScreenSession(
            id="s1",
            ep="e",
            pty=_Dead(),  # type: ignore[arg-type]
            cols=80,
            rows=24,
            dialect=dialect,
        )

    bash = _probe_cmd_for_session(_sess(POSIX_BASH))
    assert bash is not None and "set +o history" in bash and "fc -p" not in bash
    bb = _probe_cmd_for_session(_sess(POSIX_BUSYBOX))
    assert bb is not None and "fc" not in bb and len(bb) <= BUSYBOX_PROBE_MAX_LEN
    assert _probe_cmd_for_session(_sess(UNKNOWN)) is None
    assert _probe_cmd_for_session(_sess(FISH)) is None


def test_build_script_argv_auto_dialect() -> None:
    from mcp_remote_control.exec import build_script_argv

    assert build_script_argv(
        body="echo ok", runtime="auto", dialect=POSIX_BUSYBOX
    )[0] == "sh"
    assert build_script_argv(
        body="echo ok", runtime="auto", dialect=POSIX_BASH
    )[0] == "bash"
    # Explicit runtime wins
    assert build_script_argv(
        body="echo ok", runtime="bash", dialect=POSIX_BUSYBOX
    )[0] == "bash"
