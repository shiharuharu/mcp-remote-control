"""Unit tests: small correctness / diagnostics defects.

Four independent checks:

- a concurrent ``LocalPty.is_alive()`` must not replace another caller's
  observed exit status with a fallback;
- ``doctor`` must probe every declared required dependency (pyserial ships
  the ``serial`` package and gates the whole console tool);
- the PowerShell silent-cwd probe must not claim a history removal it cannot
  perform, and must not carry metadata nothing reads;
- an idempotent ``endpoint open`` must not credit itself with a link recovery
  another caller performed on the reused handle.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tomllib
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.cli_cmds import doctor as doctor_mod
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.cli_cmds.doctor import format_report, run_doctor
from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint.registry import Endpoint
from mcp_remote_control.screen import local_pty
from mcp_remote_control.shell.dialect import (
    DIALECT_PROBES,
    POWERSHELL,
    ProbeSpec,
    probe_cmd_for_dialect,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
_CODE_ROOT = Path(__file__).resolve().parents[2]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalPty is POSIX-only (fork/PTY)",
)


# ---------------------------------------------------------------------------
# local PTY - the waitpid race loser must not invent an exit status
# ---------------------------------------------------------------------------


@_posix_only
def test_loser_of_the_waitpid_race_keeps_the_observed_exit_status() -> None:
    """A concurrent is_alive() must not clobber the reaper's real status.

    Interleaving: the loser enters is_alive(), passes the ``_exit_code is
    not None`` check and blocks inside waitpid; the winner then reaps the
    child (real status 3); the loser's waitpid raises ChildProcessError
    because the status is already consumed.
    """
    pty = local_pty.LocalPty(cols=80, rows=24, argv=["/bin/sh", "-c", "exit 3"])
    real_waitpid = os.waitpid
    entered = threading.Event()
    winner_done = threading.Event()
    observed: dict[str, Any] = {}

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        if threading.current_thread() is threading.main_thread():
            result = real_waitpid(pid, options)
            observed["winner"] = result
            return result
        entered.set()
        assert winner_done.wait(5.0), "winner never recorded a status"
        return real_waitpid(pid, options)

    os.waitpid = fake_waitpid  # type: ignore[assignment]
    try:
        time.sleep(1.0)  # child exits; stays a zombie until reaped
        loser = threading.Thread(target=pty.is_alive)
        loser.start()
        try:
            assert entered.wait(5.0), "loser never reached waitpid"
            pty.is_alive()  # winner: real waitpid status
            assert observed["winner"][0] == pty.pid, "winner did not reap the child"
            assert pty._exit_code == 3
            winner_done.set()
            loser.join(5.0)
            assert not loser.is_alive()
            assert pty.exit_code() == 3
        finally:
            winner_done.set()
            loser.join(5.0)
    finally:
        os.waitpid = real_waitpid  # type: ignore[assignment]
        pty.close()


@_posix_only
def test_foreign_reap_reports_unknown_not_a_clean_exit() -> None:
    """A status consumed outside LocalPty leaves no false ``0`` behind."""
    pty = local_pty.LocalPty(cols=80, rows=24, argv=["/bin/sh", "-c", "exit 3"])
    try:
        time.sleep(1.0)
        pid = pty.pid
        assert pid is not None
        _done, status = os.waitpid(pid, 0)  # a host-level reaper took it
        assert os.WEXITSTATUS(status) == 3
        assert pty.is_alive() is False
        assert pty.exit_code() is None
        # close() reaps through the same helper: a status consumed elsewhere
        # must still not be replaced by a fallback.
        assert pty._try_reap(pid) is True
        assert pty._exit_code is None
    finally:
        pty.close()


# ---------------------------------------------------------------------------
# doctor - hard-dependency coverage
# ---------------------------------------------------------------------------


class _BlockImport(MetaPathFinder):
    """Make one top-level distribution unimportable (simulates --no-deps)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname == self._name or fullname.startswith(f"{self._name}."):
            raise ImportError(f"{self._name} not installed")
        return None


def test_doctor_fails_when_pyserial_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor must not report PASS on an install where console open fails."""
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    for mod in [m for m in sys.modules if m == "serial" or m.startswith("serial.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockImport("serial"), *sys.meta_path])

    report = run_doctor()
    serial_check = [c for c in report.checks if c.name == "import serial"]
    assert serial_check, "doctor has no `import serial` check"
    assert serial_check[0].soft is False
    assert serial_check[0].ok is False
    assert report.ok is False
    assert report.exit_code() == EXIT_VALIDATION
    assert "doctor: FAIL" in format_report(report)


def test_doctor_probes_every_declared_required_dependency() -> None:
    """Every ``[project] dependencies`` entry is covered by a doctor check.

    The mapping is name-based (import name may differ from the distribution
    name), so an alias table makes the correspondence explicit.
    """
    dist_to_import = {"pyserial": "serial"}
    with (_CODE_ROOT / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    required = set()
    for spec in project["dependencies"]:
        name = spec.split("[", 1)[0]
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<", ";"):
            name = name.split(sep, 1)[0]
        dist = name.strip().lower().replace("-", "_")
        required.add(dist_to_import.get(dist, dist))

    covered = set(doctor_mod._HARD_DEPS) | set(doctor_mod._SOFT_DEPS)
    assert required <= covered, f"unprobed required deps: {sorted(required - covered)}"


def test_doctor_still_passes_with_the_full_dependency_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The added check must not turn a complete install into a failure."""
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    report = run_doctor()
    assert report.ok, "\n".join(c.line() for c in report.checks)
    assert report.exit_code() == EXIT_OK


# ---------------------------------------------------------------------------
# shell dialect - the PowerShell probe's history claim
# ---------------------------------------------------------------------------


def test_powershell_probe_has_no_unrunnable_history_step() -> None:
    """``Remove-History`` is not a cmdlet; the try/catch hid the failure."""
    cmd = probe_cmd_for_dialect(POWERSHELL)
    assert cmd is not None
    assert "Remove-History" not in cmd
    assert "history_mode" not in ProbeSpec.__dataclass_fields__
    assert len(cmd) <= DIALECT_PROBES[POWERSHELL].max_len  # type: ignore[index]


def test_powershell_probe_still_emits_the_cwd_marker() -> None:
    """Dropping the history step must not break the probe's actual job."""
    from mcp_remote_control.screen.buffer import PWD_MARKER

    cmd = probe_cmd_for_dialect(POWERSHELL)
    assert cmd is not None
    assert PWD_MARKER in cmd
    assert "(Get-Location).Path" in cmd


# ---------------------------------------------------------------------------
# endpoint open - session_resynced attribution
# ---------------------------------------------------------------------------


class _StubTransport:
    """Transport double: ``is_connected`` runs another thread's heal."""

    def __init__(self, *, heal_during_probe: bool) -> None:
        self.meta: dict[str, Any] = {}
        self._heal = heal_during_probe
        self._connected = True

    def is_connected(self) -> bool:
        if self._heal:
            # A concurrent call recovers the reused link while this open is
            # in flight - after open_endpoint's pre-open snapshot.
            healer = threading.Thread(
                target=lambda: self.meta.__setitem__("session_resynced", True)
            )
            healer.start()
            healer.join()
        return self._connected


class _StubRegistry:
    """Registry double: ``open`` returns the handle this open established."""

    def __init__(self, prior: Endpoint | None, opened: Endpoint | None = None) -> None:
        self._prior = prior
        self._opened = opened if opened is not None else prior
        self.open_calls = 0

    def get(self, name: str) -> Endpoint | None:
        return self._prior

    def open(self, name: str, **_kwargs: Any) -> Endpoint:
        self.open_calls += 1
        assert self._opened is not None
        return self._opened

    def close_if_same(self, name: str, handle: Endpoint | None) -> None:
        return None


def _stub_endpoint(transport: _StubTransport) -> Endpoint:
    return Endpoint(
        name="lab-win",
        transport_name="winrm",
        caps={},
        connected=True,
        transport=transport,  # type: ignore[arg-type]
    )


def test_reused_handle_does_not_credit_a_concurrent_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A heal another caller performed is not reported by this open.

    ``registry.open`` hands back the live handle verbatim (no reconnect, no
    probe) and the marker is sticky, so a marker that appears on that handle
    during this call belongs to the other caller.
    """
    transport = _StubTransport(heal_during_probe=True)
    ep = _stub_endpoint(transport)
    registry = _StubRegistry(ep)
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)

    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)

    assert result.status == "ok"
    assert registry.open_calls == 1
    assert "session_resynced" not in result.fields
    assert "session_resynced" not in result.render_text()


def test_replaced_handle_still_reports_its_own_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transport this open established keeps its own heal marker."""
    prior = _stub_endpoint(_StubTransport(heal_during_probe=False))
    ep = _stub_endpoint(_StubTransport(heal_during_probe=True))
    registry = _StubRegistry(prior, opened=ep)
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)

    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)

    assert result.status == "ok"
    assert ep is not prior
    assert result.fields["session_resynced"] == 1
