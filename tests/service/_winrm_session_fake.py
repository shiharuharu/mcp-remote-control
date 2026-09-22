"""Shared injectable WinRM session double for exec/open/timeout suites.

One owner for the session fake the WinRM transport tests wire through
``connector=``: a session with no network sockets that records the commands and
argvs it is asked to run. Keeping one copy means the double cannot drift
between the suites that assert on the same transport surface.

The double implements the transport's session Protocol (``run_command`` /
``run_argv`` / ``close`` plus probe attributes), so tests assert against
``ExecResult`` values produced here, imported from ``transport.base``.
"""

from __future__ import annotations

from mcp_remote_control.transport.base import ExecResult


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
