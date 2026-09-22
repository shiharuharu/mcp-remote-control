"""In-process mock PowerShell runspace (no network).

Implements the same runspace / high-level WinRM session surface used by
production ``WinRMTransport``: ``open_runspace`` returns a handle with
``invoke`` / ``close`` (transport wraps it in ``InvokeRunspaceAdapter``);
optional ``run_command`` / ``run_argv``. Stores variables and location across
sequential invokes without pypsrp or a live host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.transport.base import TransportError

# Default Windows-style location for mock sessions.
DEFAULT_MOCK_LOCATION = r"C:\Users\mock"


@dataclass
class RunspaceResult:
    """Normalized result of a runspace invoke (mock or real)."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    location: str | None = None
    had_errors: bool = False


@dataclass
class MockRunspace:
    """Minimal in-process PS state machine for shared-runspace tests.

    Satisfies the RunspaceHandle surface (``invoke`` / ``close`` + ``location``).
    Transport wraps instances in ``InvokeRunspaceAdapter`` at open time.

    Supports a tiny script dialect:

    - ``$name = value`` / ``$name=value`` assignments (int or quoted string)
    - bare ``$name`` reads
    - ``Set-Location`` / ``cd`` updates location
    - ``Get-Location`` / ``(Get-Location).Path`` / ``$PWD.Path``
    - ``Write-Output`` / bare string literals
    - ``exit N`` maps to ``exit_code=N`` (no silent success)
    - unknown statements fail with non-zero exit and stderr containing ``UNKNOWN``
    """

    location: str = DEFAULT_MOCK_LOCATION
    vars: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def invoke(self, script: str) -> RunspaceResult:
        if self.closed:
            raise TransportError(
                "PS_CLOSED",
                "runspace is closed",
            )
        text = script if script is not None else ""
        return self._eval(text)

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------
    # tiny interpreter
    # ------------------------------------------------------------------

    def _eval(self, script: str) -> RunspaceResult:
        parts = [
            p.strip()
            for p in re.split(r"[\r\n;]+", script)
            if p.strip() and not p.strip().startswith("#")
        ]
        if not parts:
            return RunspaceResult(stdout="", location=self.location)

        stdout_chunks: list[str] = []
        stderr = ""
        exit_code = 0
        for stmt in parts:
            out, err, code = self._eval_stmt(stmt)
            if out:
                stdout_chunks.append(out.rstrip("\n"))
            if err:
                stderr = err if not stderr else f"{stderr}\n{err}"
            if code != 0:
                exit_code = code
        body = "\n".join(c for c in stdout_chunks if c is not None and c != "")
        if body and not body.endswith("\n"):
            body = body + "\n"
        return RunspaceResult(
            stdout=body,
            stderr=stderr,
            exit_code=exit_code,
            location=self.location,
            had_errors=exit_code != 0 or bool(stderr),
        )

    def _eval_stmt(self, stmt: str) -> tuple[str, str, int]:
        s = stmt.strip()
        if not s:
            return "", "", 0

        m = re.match(
            r"(?is)^(?:Set-Location|cd)(?:\s+-LiteralPath)?\s+(.+)$",
            s,
        )
        if m:
            path = _strip_ps_quotes(m.group(1).strip())
            self.location = path
            return "", "", 0

        if re.match(r"(?is)^\(Get-Location\)\.Path\s*$", s):
            return self.location + "\n", "", 0
        if re.match(r"(?is)^Get-Location\s*$", s):
            return self.location + "\n", "", 0
        if re.match(r"(?is)^\$PWD(?:\.Path)?\s*$", s):
            return self.location + "\n", "", 0

        m = re.match(r"^\$(\w+)\s*=\s*(.+)$", s, re.DOTALL)
        if m:
            name = m.group(1)
            raw_val = m.group(2).strip()
            parsed = _parse_ps_value(raw_val)
            if parsed is _NOT_A_LITERAL:
                # RHS must be int or quoted-string (bool literals already
                # accepted). Bare cmdlets like Get-Foo must not store as text.
                return "", f"UNKNOWN: unsupported mock statement: {s}", 1
            self.vars[name] = parsed
            return "", "", 0

        m = re.match(r"^\$(\w+)\s*$", s)
        if m:
            name = m.group(1)
            if name not in self.vars:
                return (
                    "",
                    f"Variable ${name} is not set",
                    1,
                )
            return str(self.vars[name]) + "\n", "", 0

        m = re.match(
            r"(?is)^Write-(?:Output|Host)\s+(.+)$",
            s,
        )
        if m:
            val = _strip_ps_quotes(m.group(1).strip())
            if val.startswith("$") and re.match(r"^\$\w+$", val):
                vname = val[1:]
                if vname in self.vars:
                    return str(self.vars[vname]) + "\n", "", 0
            return val + "\n", "", 0

        if (s.startswith("'") and s.endswith("'")) or (
            s.startswith('"') and s.endswith('"')
        ):
            return _strip_ps_quotes(s) + "\n", "", 0

        # exit N -> map to that exit code (PowerShell-style session exit).
        m = re.match(r"(?is)^exit(?:\s+(-?\d+))?\s*$", s)
        if m:
            raw = m.group(1)
            code = int(raw) if raw is not None else 0
            return "", "", code

        # Unknown statements must not succeed silently.
        return "", f"UNKNOWN: unsupported mock statement: {s}", 1


def _strip_ps_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and (
        (v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')
    ):
        return v[1:-1]
    return v


# Sentinel: assignment RHS is not a supported literal (not stored).
_NOT_A_LITERAL = object()


def _parse_ps_value(raw: str) -> Any:
    """Parse an assignment RHS. ``_NOT_A_LITERAL`` if not int / quoted / bool."""
    v = raw.strip()
    if len(v) >= 2 and (
        (v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')
    ):
        return v[1:-1]
    if v.lower() in ("$true", "true"):
        return True
    if v.lower() in ("$false", "false"):
        return False
    try:
        if re.fullmatch(r"-?\d+", v):
            return int(v)
    except ValueError:
        pass
    return _NOT_A_LITERAL


class MockWinRMSessionWithRunspace:
    """WinRM session double implementing the high-level + runspace Protocols.

    Drop-in for a ``WinRMTransport`` connector return value: provides
    ``run_command`` / ``run_argv`` for exec plus ``open_runspace`` for ps.
    """

    def __init__(
        self,
        *,
        cwd: str = DEFAULT_MOCK_LOCATION,
        home: str = DEFAULT_MOCK_LOCATION,
        os_name: str = "windows",
        shell: str = "powershell",
        ps_version: str = "5.1.19041",
    ) -> None:
        self.cwd = cwd
        self.home = home
        self.os = os_name
        self.shell = shell
        self.ps_version = ps_version
        self.closed = False
        self._runspaces: list[MockRunspace] = []

    def close(self) -> None:
        self.closed = True
        for rs in self._runspaces:
            try:
                rs.close()
            except Exception:  # noqa: BLE001
                pass

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        from mcp_remote_control.transport.base import ExecResult

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
    ) -> Any:
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(
            exit_code=0,
            stdout=" ".join(argv) + "\n",
            cwd=cwd or self.cwd,
        )

    def open_runspace(self) -> MockRunspace:
        rs = MockRunspace(location=self.cwd or DEFAULT_MOCK_LOCATION)
        self._runspaces.append(rs)
        return rs
