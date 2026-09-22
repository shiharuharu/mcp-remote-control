"""Service tests: the WinRM oneshot exit probe and the op/read pair per path.

Two contracts are pinned here.

**Oneshot exit probe.** ``run_command`` / ``run_argv`` reach pypsrp through
``execute_ps`` with the caller's command and an appended exit probe composed
into one script. The caller's text is dot-sourced so a top-level ``return``
ends only its own block and the probe still runs, and a payload that carries
no probe marker is reported with an *unknown* exit code (``-1``) rather than as
success: pypsrp reports a pipeline that ended before the probe as completed
with no error records, so the missing marker is the only evidence available -
the same "missing marker means no evidence, not 0" rule the pooled runspace
path states in ``RunspaceResult.exit_probe_ran``.

Ground truth for the PowerShell wording was measured in
``mcr.microsoft.com/powershell:lts-ubuntu-22.04`` (pwsh 7.4), one process per
case, over the exact composed script (``Invoke-Expression``, as pypsrp runs
it)::

    body                          spliced        dot-sourced
    exit 2                        no marker      no marker
    /bin/sh -c 'exit 3'           marker 3       marker 3
    /bin/sh -c 'exit 7'; return   no marker      marker 7
    return                        no marker      marker 0

A top-level ``exit`` ends the whole pipeline either way - hence the
missing-marker signal - while ``return`` ends only its own block - hence the
dot-sourcing. ``_PwshModelSession`` below transcribes that table.

**Op/read ordering.** The HTTP read timeout must outlast the WSMan operation
timeout on every path that reaches the transport: at connect, on the oneshot
exec path, and on the paths that never re-resolve per call (``open_runspace``
/ ``close_runspace``, ``open_fs`` and every fs call). ``_PeerModelSession``
models pypsrp's client read timeout: with ``op + slack > read`` the server is
still working at the client's read deadline, so it raises ``ReadTimeout`` -
the shape classified ``link_fatal``, which tears the endpoint down.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
import requests.exceptions as requests_exceptions

from mcp_remote_control.core import exec_ops
from mcp_remote_control.transport.winrm import WinRMTransport, _EXIT_MARKER
from mcp_remote_control.transport.winrm_timeouts import (
    PYPSRP_DEFAULT_OPERATION_TIMEOUT_S,
    PYPSRP_HTTP_TIMEOUT_SLACK_S,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# The reset the exit probe emits before the caller's text.
_RESET = "$global:LASTEXITCODE = $null"
_PROBE = "Write-Output ('" + _EXIT_MARKER + "' + [string]($(if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 })))"


# ---------------------------------------------------------------------------
# oneshot exec: the probe contract
# ---------------------------------------------------------------------------


class _PwshModelSession:
    """pypsrp session double applying the measured pwsh exit/return table.

    ``execute_ps`` receives the composed script and decides whether the
    appended probe still runs, exactly as the container-measured cases show.
    When it does not, the session reports what pypsrp reports for such a
    pipeline: a completed run, no error records, and no marker in the output.

    The code the probe reports is read out of the composed script, not handed
    in: $LASTEXITCODE at probe time is whatever the caller's own command left
    there, so a double that answers from a constructor argument would report a
    value the script never carried and no assertion could falsify the wrapper.
    """

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
        self, script: str, *, environment: dict[str, str] | None = None
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        marker_at = script.rfind(_PROBE)
        assert marker_at > 0, "the oneshot path must append the exit probe"
        body = script.split(_RESET, 1)[-1][:marker_at]
        # The dot-sourced wrapper is what keeps a top-level ``return`` local.
        isolated = ". {" in body
        if isolated:
            open_at = body.index(". {")
            body = body[open_at + 3 : body.rindex("}")]
        statements = [
            part.strip()
            for line in body.splitlines()
            for part in line.split(";")
            if part.strip()
        ]
        native_rc = _native_code_in(statements)
        for statement in statements:
            if statement == "exit" or statement.startswith("exit "):
                # A top-level exit ends the pipeline before the probe.
                return ("", None, False)
            if statement == "return" and not isolated:
                # Spliced text: the return ends the whole script.
                return ("", None, False)
        marker = f"{_EXIT_MARKER}0\n" if native_rc is None else f"{_EXIT_MARKER}{native_rc}\n"
        return (marker, None, False)


def _native_code_in(statements: list[str]) -> int | None:
    """The native exit code the caller's statements leave in ``$LASTEXITCODE``.

    A native command spells its code as a trailing literal - ``cmd.exe /c exit
    7`` in the shell form, ``@('/c', 'exit', '7')`` in the argv splice - so the
    model takes the last integer literal of the last statement naming ``exit``.
    """
    code: int | None = None
    for statement in statements:
        if "exit" not in statement:
            continue
        literals = re.findall(r"\b(\d+)\b", statement)
        if literals:
            code = int(literals[-1])
    return code


def _run_command(command: str) -> tuple[_PwshModelSession, object]:
    sess = _PwshModelSession()
    t = WinRMTransport(host="h", username="u", password="p", connector=lambda **_k: sess)
    t.connect()
    return sess, t.run_command(command, timeout_s=10)


def test_oneshot_script_dot_sources_the_callers_text() -> None:
    """The caller's text runs in its own block; the probe stays outside it."""
    sess, _ = _run_command("Write-Output hi")
    script = sess.scripts[-1]
    assert script.startswith(_RESET + "\n. {\n")
    assert script.endswith(_PROBE)
    assert script.index("Write-Output hi") < script.index("\n}\n")


def test_top_level_return_still_reports_the_native_exit_code() -> None:
    """``return`` ends only the dot-sourced block, so the probe still runs.

    The code must be the one the caller's own command carried into the composed
    script, and the probe must sit outside the block that command ran in.
    """
    sess, res = _run_command("& cmd.exe /c exit 7; return")
    script = sess.scripts[-1]
    assert "& cmd.exe /c exit 7" in script, "the caller's text is composed in"
    assert ". {" in script
    assert script.index("\n}\n") < script.index(_PROBE), "probe left inside the block"
    assert res.exit_code == 7, res.stderr
    assert res.stderr == ""


def test_top_level_exit_is_reported_as_unknown_not_success() -> None:
    """A script that ends its own pipeline leaves no evidence of success."""
    sess, res = _run_command("exit 2")
    assert "exit 2" in sess.scripts[-1]
    assert res.exit_code == -1, res.stderr
    assert "exit probe did not run" in res.stderr
    assert res.timed_out is False


def test_top_level_exit_through_exec_ops_reports_fail() -> None:
    """End to end: ``exec ... exit 2`` is status=fail with an honest body."""
    sess = _PwshModelSession()

    def connector(**_kwargs: object) -> _PwshModelSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="exit 2",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "fail"
    assert r.fields.get("exit") == -1
    assert "exit probe did not run" in (r.body or "")


def test_run_argv_splice_is_isolated_and_reports_the_probe_code() -> None:
    """The argv splice is a generated body: it carries the same probe contract."""
    sess = _PwshModelSession()
    t = WinRMTransport(host="h", username="u", password="p", connector=lambda **_k: sess)
    t.connect()
    res = t.run_argv(["cmd.exe", "/c", "exit", "7"], timeout_s=10)
    script = sess.scripts[-1]
    assert ". {" in script
    assert "@('" in script, "argv must use the call operator form"
    assert script.endswith(_PROBE)
    assert res.exit_code == 7, res.stderr


# ---------------------------------------------------------------------------
# op/read ordering on every path that reaches the transport
# ---------------------------------------------------------------------------


class _HttpNode:
    def __init__(self, read_timeout: int) -> None:
        self.read_timeout = read_timeout


class _WsmanNode:
    def __init__(self, operation_timeout: int, read_timeout: int) -> None:
        self.operation_timeout = operation_timeout
        self.transport = _HttpNode(read_timeout)


class _Handle:
    """Minimal runspace handle; ``invoke`` marks it as an invoke-style handle."""

    def __init__(self, session: _PeerModelSession) -> None:
        self._session = session
        self.closed = False

    def invoke(self, script: str, **kwargs: object) -> object:
        del script, kwargs
        self._session.exchange()
        return ("", None, False)

    def stop(self) -> None:
        self._session.exchange()

    def close(self) -> None:
        self.closed = True
        self._session.exchange()


class _PeerModelSession:
    """Session whose client read timeout models the applied op/read pair.

    Built from the connect kwargs the way pypsrp builds its client: the WSMan
    pair starts at the library defaults (20/30) and the connect kwargs overwrite
    the fields they carry. Every exchange then checks the live pair, so an
    unclamped path fails here instead of silently tearing the endpoint down.
    """

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.wsman = _WsmanNode(PYPSRP_DEFAULT_OPERATION_TIMEOUT_S, 30)
        self.pairs: list[tuple[int, int]] = []
        self.last_handle: _Handle | None = None

    def close(self) -> None:
        self.closed = True

    def pair(self) -> tuple[int, int]:
        return int(self.wsman.operation_timeout), int(self.wsman.transport.read_timeout)

    def exchange(self) -> None:
        op, rd = self.pair()
        self.pairs.append((op, rd))
        if op + PYPSRP_HTTP_TIMEOUT_SLACK_S > rd:
            raise requests_exceptions.ReadTimeout(
                f"read timeout={rd} (op={op} still running)"
            )

    def execute_ps(
        self, script: str, *, environment: dict[str, str] | None = None
    ) -> tuple[str, object, bool]:
        del script, environment
        self.exchange()
        return (f"{_EXIT_MARKER}0\n", None, False)

    def fetch(self, remote: str, local: str) -> None:
        del remote, local
        self.exchange()

    def copy(self, local: str, remote: str) -> None:
        del local, remote
        self.exchange()

    def open_runspace(self) -> _Handle:
        self.exchange()
        self.last_handle = _Handle(self)
        return self.last_handle


def _peer_transport(sess: _PeerModelSession, **kwargs: object) -> WinRMTransport:
    def connector(**connect_kwargs: object) -> _PeerModelSession:
        # pypsrp Client construction: only the fields the kwargs carry are set.
        op = connect_kwargs.get("operation_timeout")
        rd = connect_kwargs.get("read_timeout")
        if op is not None:
            sess.wsman.operation_timeout = int(op)  # type: ignore[arg-type]
        if rd is not None:
            sess.wsman.transport.read_timeout = int(rd)  # type: ignore[arg-type]
        return sess

    t = WinRMTransport(
        host="h", username="u", password="p", connector=connector, **kwargs  # type: ignore[arg-type]
    )
    t.connect()
    return t


def _ordered(pair: tuple[int, int]) -> bool:
    op, rd = pair
    return rd >= op + PYPSRP_HTTP_TIMEOUT_SLACK_S


def test_connect_kwargs_resolve_an_ordered_pair() -> None:
    """A read-only profile must not leave pypsrp's default op (20) above it."""
    t = WinRMTransport(host="h", username="u", password="p", read_timeout_s=10)
    kwargs = t.connect_kwargs()
    assert kwargs["read_timeout"] == 10
    op = kwargs.get("operation_timeout", PYPSRP_DEFAULT_OPERATION_TIMEOUT_S)
    assert _ordered((int(op), 10))


def test_non_positive_profile_value_is_unset_on_the_connect_path_too() -> None:
    """The per-call resolver reads a non-positive value as unset; so must connect.

    The connector floors a forwarded value at 1 second - a deadline no exchange
    can meet - so the two meanings must not diverge.
    """
    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        read_timeout_s=0,
        operation_timeout_s=0,
    )
    kwargs = t.connect_kwargs()
    assert "read_timeout" not in kwargs
    assert "operation_timeout" not in kwargs
    assert t.resolve_call_op_read_timeouts(5)[1] != 1


def test_explicit_inverted_pair_is_clamped_not_forwarded() -> None:
    """An op above the read is dead configuration; the read caps it."""
    t = WinRMTransport(
        host="h", username="u", password="p", operation_timeout_s=60, read_timeout_s=10
    )
    op, rd = t.resolve_call_op_read_timeouts(30)
    assert (op, rd) == (10 - PYPSRP_HTTP_TIMEOUT_SLACK_S, 10)
    kwargs = t.connect_kwargs()
    assert _ordered((int(kwargs["operation_timeout"]), int(kwargs["read_timeout"])))


def test_every_transport_path_runs_an_ordered_pair(tmp_path: Path) -> None:
    """connect, exec, runspace open/close and fs all keep read > op.

    The peer model raises ``ReadTimeout`` for an inverted pair, so a path that
    never re-resolves per call cannot quietly tear the endpoint down here.
    """
    sess = _PeerModelSession()
    t = _peer_transport(sess, read_timeout_s=10)
    assert _ordered(sess.pair()), sess.pair()

    result = t.run_command("Get-Date", timeout_s=12)
    assert result.exit_code == 0, result.stderr
    assert _ordered(sess.pairs[-1])

    handle = t.open_runspace()
    assert _ordered(sess.pairs[-1])
    t.close_runspace(handle)
    assert sess.last_handle is not None and sess.last_handle.closed is True
    assert _ordered(sess.pairs[-1])

    client = t.open_fs()
    client.fetch(r"C:\temp\n", str(tmp_path / "n"))
    assert _ordered(sess.pairs[-1])

    assert t.is_connected() is True
    assert not t.meta.get("link_lost"), t.meta
    assert all(_ordered(p) for p in sess.pairs), sess.pairs


def test_oneshot_exec_narrows_the_pair_to_the_call_budget() -> None:
    """The exec path still applies the call budget under the read ceiling."""
    sess = _PeerModelSession()
    t = _peer_transport(sess, read_timeout_s=10)
    result = t.run_command("Get-Date", timeout_s=4)
    assert result.exit_code == 0, result.stderr
    assert sess.pairs[-1] == (4, 10)


def test_adjusted_profile_pair_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Clamping and "unset" are not silent: connect says which pair it used."""
    logger = "mcp_remote_control.transport.winrm"
    with caplog.at_level(logging.WARNING, logger=logger):
        WinRMTransport(
            host="h",
            username="u",
            password="p",
            operation_timeout_s=60,
            read_timeout_s=10,
        ).connect_kwargs()
        WinRMTransport(
            host="h", username="u", password="p", read_timeout_s=0
        ).connect_kwargs()
    messages = [r.getMessage() for r in caplog.records if r.name == logger]
    assert any("operation_timeout_s=60" in m for m in messages), messages
    assert any("read_timeout_s=0" in m for m in messages), messages

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=logger):
        # An ordered pair and an unset profile are used as configured.
        WinRMTransport(
            host="h",
            username="u",
            password="p",
            operation_timeout_s=20,
            read_timeout_s=22,
        ).connect_kwargs()
        WinRMTransport(host="h", username="u", password="p").connect_kwargs()
    assert [r.getMessage() for r in caplog.records if r.name == logger] == []
