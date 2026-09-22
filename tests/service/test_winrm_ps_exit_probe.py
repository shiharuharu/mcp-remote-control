"""Service tests: what a pooled ``ps invoke`` reports about exit code and cwd.

The exit/cwd probes appended to every pooled invoke are the only machine-
readable source for the invoke's native exit code and current location, so
their behaviour on a *persistent* runspace is load-bearing. PowerShell
semantics below were measured against real PowerShell (pwsh 7.4, the lts
image, one pipeline per invoke against a single persistent runspace, which is
how pypsrp drives a pooled runspace):

- ``$LASTEXITCODE`` is an automatic variable of the runspace, not of the
  pipeline, so the probe has to neutralise it before the user script runs, or
  a pure-PowerShell invoke reports the previous invoke's exit code. Measured:
  ``bash -c 'exit 3'`` followed by ``Write-Output done`` plus the shipped probe
  printed the stale ``3``, while prepending ``$global:LASTEXITCODE = $null``
  made the same probe print ``0`` - and a native ``exit 9`` or a user
  ``Set-Variable -Name LASTEXITCODE -Value 5`` in that block still printed its
  own value.
- The probes are appended in one script block, so a top-level ``return`` /
  ``exit`` ends the block before they run. A probe that never ran must not be
  reported as "probe ran and said 0"; measured, a top-level ``exit N`` emitted
  neither marker.
- The reset statement that neutralises ``$LASTEXITCODE`` must not become the
  user script's first statement: PowerShell accepts a ``param(...)`` block only
  there, so the user's text is isolated as its own dot-sourced block. Measured:
  ``param(...)`` as the block's first statement ran, while the identical
  statement preceded by the reset failed with "The term 'param' is not
  recognized as a name of a cmdlet"; a top-level ``return`` inside the
  dot-sourced block ended that block only, so the probes after it still emitted
  their markers, and assignments and function definitions in that block stayed
  visible to the next pipeline (dot-sourcing, not a child scope).
- The PowerShell error stream carries the failure reason and has to reach the
  agent even when stdout is non-empty.

The ``PowerShell`` double below interprets the probe text instead of returning
canned output, because the value under test is runspace session state.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.core import ps_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.ps import reset_ps_registry
from mcp_remote_control.transport.winrm_exec import _EXIT_MARKER
from mcp_remote_control.transport.winrm_runspace import _LOCATION_MARKER

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

_NATIVE_EXIT_RE = re.compile(r"cmd(?:\.exe)?\s*/c\s+exit\s+(-?\d+)")
_SET_VAR_RE = re.compile(r"Set-Variable\s+-Name\s+LASTEXITCODE\s+-Value\s+(-?\d+)")
# Statements PowerShell accepts only as the first statement of a block. Placed
# anywhere else the parser hands the keyword to the command resolver, which
# fails; the double reproduces that so a regression cannot hide.
_BLOCK_LEADING_RE = re.compile(r"^(param\s*\(|using\s+(?:namespace|module)\b)")


def _block_leading_keyword(statement: str) -> str:
    return statement.split("(", 1)[0].strip()


class _RunspaceState:
    """Session state shared by every pipeline on one persistent runspace."""

    def __init__(self, location: str = r"C:\Users\mock") -> None:
        # $LASTEXITCODE starts unset on a fresh runspace and survives across
        # invokes; a value set by one pipeline is read by the next.
        self.last_exit_code: int | None = None
        self.location = location


def _ends_block(statement: str) -> bool:
    """True for a statement that ends a script block (skips the rest of it).

    ``exit`` ends the whole invocation; ``return`` ends only the block it sits
    in. Both are matched by keyword, not by exit code - the fake only needs to
    know the block ended.
    """
    return statement == "return" or statement.startswith(("return ", "exit "))


class _FakeStreams:
    def __init__(self, errors: list[object] | None = None) -> None:
        self.error = errors or []


class _ProbeAwarePowerShell:
    """pypsrp ``PowerShell`` double that evaluates the probe script.

    Statement handling is lexical and covers only the probe plus the handful of
    user statements the tests use; ordering matters exactly as it does on a real
    runspace, so a reset placed after the user script hides that script's own
    native exit code, a script that ends its block emits no markers, and a
    ``param(...)`` statement that is not first in its block fails.
    """

    instances: list[_ProbeAwarePowerShell] = []

    def __init__(self, pool: _PoolFake) -> None:
        self.pool = pool
        self.state = pool.state
        self.script: str | None = None
        self.invoke_count = 0
        self.stopped = False
        self.closed = False
        self.had_errors = False
        self.streams = _FakeStreams()
        _ProbeAwarePowerShell.instances.append(self)

    def add_script(
        self, script: str, use_local_scope: object = None
    ) -> _ProbeAwarePowerShell:
        self.script = script
        return self

    def invoke(self, input: object = None, **_kw: object) -> list[object]:
        self.invoke_count += 1
        out: list[object] = []
        lines = [line.strip() for line in (self.script or "").splitlines()]
        index = 0
        depth = 0
        # A block's first statement may be a param(...) declaration; any later
        # statement may not.
        at_block_start = True
        while index < len(lines):
            statement = lines[index]
            index += 1
            if not statement:
                continue
            if statement == ". {":
                depth += 1
                at_block_start = True
                continue
            if statement == "}":
                depth = max(0, depth - 1)
                at_block_start = True
                continue
            if _BLOCK_LEADING_RE.match(statement) and not at_block_start:
                keyword = _block_leading_keyword(statement)
                self.streams.error.append(
                    f"The term '{keyword}' is not recognized as the name of a "
                    "cmdlet, function, script file, or operable program."
                )
                self.had_errors = True
                at_block_start = False
                continue
            at_block_start = False
            if _ends_block(statement):
                if statement.startswith("exit") or depth == 0:
                    break
                # return: ends the dot-sourced user block only, so the probes
                # that follow it outside the block still run.
                while index < len(lines) and lines[index] != "}":
                    index += 1
                if index < len(lines):
                    index += 1
                depth = 0
                at_block_start = True
                continue
            if statement.startswith(("$global:LASTEXITCODE", "$LASTEXITCODE")):
                # The probe's own reset: clears the runspace-level value, so
                # what the probe reads belongs to this invoke.
                self.state.last_exit_code = None
                continue
            native = _NATIVE_EXIT_RE.search(statement)
            if native:
                self.state.last_exit_code = int(native.group(1))
                continue
            assigned = _SET_VAR_RE.search(statement)
            if assigned:
                self.state.last_exit_code = int(assigned.group(1))
                continue
            if statement.startswith("Set-Location "):
                moved = statement[len("Set-Location ") :].strip().strip("'")
                self.state.location = moved
                self.pool.location = moved
                continue
            if statement.startswith("Write-Error "):
                text = statement[len("Write-Error ") :].strip().strip("'")
                self.streams.error.append(text)
                self.had_errors = True
                continue
            if statement.startswith("Write-Output "):
                payload = statement[len("Write-Output ") :].strip()
                if _EXIT_MARKER in payload:
                    code = self.state.last_exit_code
                    out.append(f"{_EXIT_MARKER}{0 if code is None else code}")
                elif _LOCATION_MARKER in payload:
                    out.append(f"{_LOCATION_MARKER}{self.state.location}")
                else:
                    out.append(payload.strip("'"))
                continue
        return out

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _PoolFake:
    """Pool-like handle without ``invoke`` so open_runspace wraps it in
    ``PypsrpPoolRunspaceAdapter`` (the real PowerShell pipeline path)."""

    def __init__(self, state: _RunspaceState) -> None:
        self.state = state
        self.location = state.location
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PypsrpSession:
    """Mock WinRM session whose runspace is a persistent pool."""

    def __init__(self, state: _RunspaceState) -> None:
        self.state = state
        self.cwd = state.location
        self.home = state.location
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.pool: _PoolFake | None = None

    def close(self) -> None:
        self.closed = True

    def open_runspace(self) -> _PoolFake:
        self.pool = _PoolFake(self.state)
        return self.pool


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_ps_registry()
    yield
    reset_ps_registry()
    reset_registry()


def _open_pooled(
    monkeypatch: pytest.MonkeyPatch,
    state: _RunspaceState | None = None,
) -> tuple[str, _RunspaceState]:
    """Open a ps session backed by a persistent pypsrp pool double."""
    _ProbeAwarePowerShell.instances.clear()
    monkeypatch.setattr("pypsrp.powershell.PowerShell", _ProbeAwarePowerShell)
    state = state or _RunspaceState()
    sess = _PypsrpSession(state)

    def conn(**_kw: object) -> _PypsrpSession:
        return sess

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=conn)
    assert opened.status == "ok", opened.render_text()
    return opened.fields["id"], state


def test_pooled_invoke_exit_code_belongs_to_this_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure-PowerShell invoke after a native non-zero command must not report
    that command's exit code: $LASTEXITCODE lives in the runspace session."""
    sid, _state = _open_pooled(monkeypatch)

    native = ps_ops.invoke(id=sid, script="& cmd.exe /c exit 3")
    assert native.status == "fail", native.render_text()
    assert native.fields.get("exit") == 3, native.render_text()

    pure = ps_ops.invoke(id=sid, script="Write-Output done")
    assert pure.fields.get("exit") == 0, pure.render_text()
    assert pure.status == "ok", (
        "a pure-PowerShell invoke reported the previous invoke's exit code "
        f"from the persistent runspace: {pure.render_text()}"
    )
    assert pure.body is not None and "done" in pure.body

    # ... and the next native command is still reported with its own code.
    again = ps_ops.invoke(id=sid, script="& cmd.exe /c exit 4")
    assert again.fields.get("exit") == 4, again.render_text()


def test_pooled_invoke_reports_last_exit_code_the_script_sets_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neutralising inherited state must not mask a value the user script sets."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(id=sid, script="Set-Variable -Name LASTEXITCODE -Value 5")
    assert r.fields.get("exit") == 5, r.render_text()
    assert r.status == "fail", r.render_text()


def test_missing_exit_probe_is_not_reported_as_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level ``exit`` skips the appended probes; the missing probe must
    be visible as such, not collapsed into "probe said 0"."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(id=sid, script="& cmd.exe /c exit 7\nexit 7")
    assert r.status != "ok", (
        "an invoke whose probe never ran was reported as success: "
        f"{r.render_text()}"
    )
    assert r.fields.get("exit") == -1, r.render_text()
    assert r.fields.get("probe") == "missing", r.render_text()
    assert "probe" in r.render_text()


def test_missing_location_probe_marks_cwd_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same skipped-probe path: the session cwd may have moved, so the result
    must not present the last known location as this invoke's."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(id=sid, script="Set-Location C:\\foo\nexit 1")
    assert r.fields.get("cwd_stale") is True, r.render_text()
    assert r.cwd == r"C:\Users\mock", r.render_text()


def test_top_level_return_still_reports_this_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``return`` ends the isolated user block, not the whole script, so the
    probes after it run and the invoke keeps its own exit code and location."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(id=sid, script="& cmd.exe /c exit 7\nreturn")
    assert r.fields.get("exit") == 7, r.render_text()
    assert r.status == "fail", r.render_text()
    assert "probe" not in r.fields, r.render_text()


def test_user_script_may_start_with_param_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset statement must not become the user script's first statement:
    PowerShell accepts a ``param(...)`` declaration only there, so a text prefix
    turns the whole script into an unresolvable command."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(
        id=sid,
        script="param([string]$p = 'unset')\nWrite-Output PARAM-RAN",
    )
    assert r.status == "ok", (
        "a script starting with a param(...) block did not run: " f"{r.render_text()}"
    )
    assert r.fields.get("exit") == 0, r.render_text()
    assert r.body is not None and "PARAM-RAN" in r.body, r.render_text()
    assert "not recognized" not in r.render_text(), r.render_text()


def test_invoke_failure_reason_survives_non_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PowerShell error stream is the failure reason; dropping it whenever
    stdout is non-empty leaves the agent with the symptom and no cause."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(
        id=sid,
        script="Write-Output partial-out\nWrite-Error 'Access denied'",
    )
    assert r.status == "fail", r.render_text()
    assert r.fields.get("exit") != 0, r.render_text()
    body = r.body or ""
    assert "partial-out" in body, r.render_text()
    assert "Access denied" in body, (
        f"the error stream was dropped because stdout was non-empty: {r.render_text()}"
    )


def test_invoke_success_with_stdout_keeps_ok_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surfacing the error stream must not turn a clean invoke into a failure
    or add stderr noise to a body that has none."""
    sid, _state = _open_pooled(monkeypatch)

    r = ps_ops.invoke(id=sid, script="Write-Output fine")
    assert r.status == "ok", r.render_text()
    assert r.fields.get("exit") == 0, r.render_text()
    assert r.body == "fine\n", r.render_text()
    assert "[stderr]" not in (r.body or "")
