"""Observability tests: list dead-reason token, open link markers, WinRM doctor.

A registered-but-dead endpoint must say *why* in ``endpoint list``, ``endpoint
open`` must surface the transport's link markers without adding noise to a
plain success, and ``doctor`` must self-check WinRM profiles offline without
changing the exit code.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.cli_cmds import EXIT_OK
from mcp_remote_control.cli_cmds.doctor import run_doctor
from mcp_remote_control.config.store import put_profile
from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.endpoint.registry import Endpoint

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def mrc_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    return FIXTURES


class _MockConn:
    """Minimal SSH connector double (mark_dead lives on the transport)."""

    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _open_lab_ssh(home: Path) -> Endpoint:
    """Register fixture ``lab-ssh`` against a stub connector (no network)."""
    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=home,
        connector=lambda **_kwargs: _MockConn(),
        probe=False,
    )
    assert r.status == "ok", f"{r.code} {r.fields}"
    ep = get_registry().get("lab-ssh")
    assert ep is not None
    return ep


def _line_for(body: str, name: str) -> str:
    lines = [ln for ln in body.splitlines() if ln.startswith(f"{name} ")]
    assert lines, f"{name} missing from list body:\n{body}"
    return lines[0]


# ---------------------------------------------------------------------------
# endpoint list - dead reason token
# ---------------------------------------------------------------------------


def test_list_shows_dead_reason_for_marked_dead_endpoint(mrc_home: Path) -> None:
    """A dead registered endpoint names its reason; machine fields unchanged."""
    ep = _open_lab_ssh(mrc_home)
    assert ep.transport is not None
    ep.transport.mark_dead("peer_reset")

    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.status == "ok"
    assert listed.body is not None
    line = _line_for(listed.body, "lab-ssh")
    assert "open=0" in line
    assert "dead_reason=peer_reset" in line
    # Machine-readable fields keep their meaning: n = rows returned (on-disk
    # profiles plus open endpoints whose profile file is gone), open =
    # still-live registrations (this dead one is excluded).
    assert listed.fields["n"] == 3
    assert listed.fields["open"] == 0


def test_list_omits_dead_reason_for_healthy_endpoint(mrc_home: Path) -> None:
    """A live endpoint carries no dead_reason token (no noise on success)."""
    assert endpoint_ops.run(op="open", profile="local", home=mrc_home).status == "ok"

    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.status == "ok"
    assert listed.body is not None
    line = _line_for(listed.body, "local")
    assert "open=1" in line
    assert "dead_reason=" not in line
    assert listed.fields["open"] == 1


def test_list_truncates_long_dead_reason(mrc_home: Path) -> None:
    """A verbose mark_dead message cannot stretch the list line."""
    ep = _open_lab_ssh(mrc_home)
    assert ep.transport is not None
    ep.transport.mark_dead("boom " + "x" * 200)

    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.body is not None
    line = _line_for(listed.body, "lab-ssh")
    value = next(tok for tok in line.split() if tok.startswith("dead_reason="))
    value = value.split("=", 1)[1]
    assert len(value) <= 40, value
    assert value.endswith("...")
    assert value.startswith("boom_")


def test_list_dead_reason_token_is_single_space_free_token(mrc_home: Path) -> None:
    """Free-form reasons are collapsed into one token, not left with spaces."""
    ep = _open_lab_ssh(mrc_home)
    assert ep.transport is not None
    ep.transport.mark_dead("identity probe failed")

    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.body is not None
    line = _line_for(listed.body, "lab-ssh")
    assert " dead_reason=identity_probe_failed" in line


# ---------------------------------------------------------------------------
# endpoint open - link markers
# ---------------------------------------------------------------------------


class _StubTransport:
    """Transport double exposing only what ``open_endpoint`` reads."""

    def __init__(self, *, connected: bool, meta: dict[str, Any] | None = None):
        self._connected = connected
        self.meta = dict(meta or {})
        self.cwd = None

    def is_connected(self) -> bool:
        return self._connected


class _StubRegistry:
    """Registry double: hands ``open`` one endpoint, ``get`` the prior one."""

    def __init__(self, ep: Endpoint, prior: Endpoint | None = None) -> None:
        self._ep = ep
        self._prior = prior
        self.closed: list[str] = []

    def get(self, name: str) -> Endpoint | None:
        return self._prior

    def open(self, name: str, **_kwargs: Any) -> Endpoint:
        return self._ep

    def close_if_same(self, name: str, handle: Endpoint | None) -> None:
        self.closed.append(name)


def _stub_endpoint(
    transport: _StubTransport, *, connected: bool
) -> Endpoint:
    return Endpoint(
        name="lab-win",
        transport_name="winrm",
        caps={},
        connected=connected,
        transport=transport,  # type: ignore[arg-type]
        cwd=None,
    )


def _open_with_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    connected: bool,
    meta: dict[str, Any],
) -> tuple[Any, _StubRegistry]:
    transport = _StubTransport(connected=connected, meta=meta)
    registry = _StubRegistry(_stub_endpoint(transport, connected=connected))
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)
    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)
    return result, registry


def test_open_failure_surfaces_link_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A link that died mid-flight is flagged, not just reported as dead."""
    result, registry = _open_with_stub(
        monkeypatch,
        tmp_path,
        connected=False,
        meta={"dead_reason": "link lost", "link_lost": True},
    )
    assert result.status == "error"
    assert result.code == "NOT_CONNECTED"
    assert result.fields["msg"] == "link lost"
    assert result.fields["link_lost"] == 1
    assert registry.closed == ["lab-win"]
    assert "link_lost=1" in result.render_text()


def test_open_failure_without_link_lost_has_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ordinary refused/dead connection keeps its historical field set."""
    result, _ = _open_with_stub(
        monkeypatch,
        tmp_path,
        connected=False,
        meta={"dead_reason": "stale_on_open"},
    )
    assert result.status == "error"
    assert result.fields["msg"] == "stale_on_open"
    assert "link_lost" not in result.fields


def test_open_success_surfaces_session_resynced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recovering by re-handshaking is recorded on the successful open."""
    result, _ = _open_with_stub(
        monkeypatch,
        tmp_path,
        connected=True,
        meta={"session_resynced": True},
    )
    assert result.status == "ok"
    assert result.fields["session_resynced"] == 1
    assert "session_resynced=1" in result.render_text()


def test_open_success_without_recovery_has_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plain success carries no recovery key and no link noise."""
    result, _ = _open_with_stub(
        monkeypatch, tmp_path, connected=True, meta={}
    )
    assert result.status == "ok"
    assert "session_resynced" not in result.fields
    assert "link_lost" not in result.fields


def test_reopen_of_already_healed_endpoint_keeps_the_marker_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A no-op reopen must not replay a heal it did not witness.

    ``registry.open`` returns a still-live endpoint unchanged (no reconnect,
    no probe), and the transport's ``session_resynced`` marker is never
    cleared, so reading it here would tag every later plain success.
    """
    transport = _StubTransport(connected=True, meta={"session_resynced": True})
    ep = _stub_endpoint(transport, connected=True)
    registry = _StubRegistry(ep, prior=ep)
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)

    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)
    assert result.status == "ok"
    assert "session_resynced" not in result.fields
    assert "session_resynced" not in result.render_text()


def test_reconnect_after_a_stale_marker_still_reports_its_own_heal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replaced transport's own heal survives the prior handle's marker.

    ``registry.open`` drops a dead generation and connects a *new* transport;
    the old handle's sticky marker must not suppress the fresh one's.
    """
    prior_transport = _StubTransport(
        connected=False, meta={"session_resynced": True}
    )
    prior = _stub_endpoint(prior_transport, connected=False)
    transport = _StubTransport(connected=True, meta={"session_resynced": True})
    ep = _stub_endpoint(transport, connected=True)
    registry = _StubRegistry(ep, prior=prior)
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)

    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)
    assert result.status == "ok"
    assert result.fields["session_resynced"] == 1
    assert "session_resynced=1" in result.render_text()


# ---------------------------------------------------------------------------
# doctor - WinRM self-check section
# ---------------------------------------------------------------------------


def _doctor_env(home: Path, **extra: str) -> dict[str, str]:
    """Isolated env: config home only, so no ambient WinRM env leaks in."""
    return {"MRC_HOME": str(home), **extra}


def test_doctor_reports_winrm_profile_knobs() -> None:
    """Fixture ``lab-win`` gets one self-check line with the effective knobs."""
    report = run_doctor(env=_doctor_env(FIXTURES))
    assert report.ok, "\n".join(c.line() for c in report.checks)
    assert report.exit_code() == EXIT_OK

    winrm_checks = [c for c in report.checks if c.name == "winrm lab-win"]
    assert len(winrm_checks) == 1, [c.name for c in report.checks]
    detail = winrm_checks[0].detail
    assert winrm_checks[0].ok is True
    for token in (
        "scheme=http",
        "auth=ntlm",
        "encryption=auto",
        "reconnection_retries=2",
        "probe=full",
        "probe_timeout_s=5",
    ):
        assert token in detail, f"{token} missing from {detail!r}"


def test_doctor_notes_http_encryption_staleness_without_failing() -> None:
    """http + encryption!=never is a soft note: visible, exit code unchanged."""
    report = run_doctor(env=_doctor_env(FIXTURES))
    note = next(
        c for c in report.checks if c.name == "winrm http+encryption (lab-win)"
    )
    assert note.soft is True
    assert note.ok is False
    assert note.line().startswith("warn")
    assert "~4-6s idle" in note.detail
    assert "self-heals" in note.detail
    assert "scheme=https has no such window" in note.detail
    # A soft note never turns into a hard failure.
    assert not report.hard_failures
    assert report.exit_code() == EXIT_OK


def test_doctor_notes_default_probe_budget_without_failing() -> None:
    """The platform-default probe budget is flagged as too small for slow links."""
    report = run_doctor(env=_doctor_env(FIXTURES))
    note = next(c for c in report.checks if c.name == "winrm probe budget (lab-win)")
    assert note.soft is True
    assert note.line().startswith("warn")
    assert "platform default" in note.detail
    assert "MRC_WINRM_PROBE_TIMEOUT_S" in note.detail
    assert report.exit_code() == EXIT_OK


def test_doctor_flags_a_budget_below_the_default(tmp_path: Path) -> None:
    """Lowering ``probe_timeout_s`` is the worst case, not a silent fix."""
    home = _winrm_home(
        tmp_path,
        name="lab-slow",
        winrm={
            "scheme": "https",
            "message_encryption": "never",
            "probe_timeout_s": 3,
        },
    )
    report = run_doctor(env=_doctor_env(home))
    note = next(c for c in report.checks if c.name == "winrm probe budget (lab-slow)")
    assert note.soft is True
    assert "at or below the platform default" in note.detail
    assert "MRC_WINRM_PROBE_TIMEOUT_S" in note.detail
    assert report.exit_code() == EXIT_OK


def test_doctor_winrm_probe_mode_honours_env(tmp_path: Path) -> None:
    """``MRC_WINRM_PROBE`` resolves through doctor's env, like the transport."""
    home = _winrm_home(tmp_path, name="lab-http", winrm={"scheme": "http"})
    report = run_doctor(env=_doctor_env(home, MRC_WINRM_PROBE="light"))
    check = next(c for c in report.checks if c.name == "winrm lab-http")
    assert "probe=light" in check.detail


def _winrm_home(
    tmp_path: Path,
    *,
    name: str,
    winrm: dict[str, Any],
) -> Path:
    home = tmp_path / name
    put_profile(
        home,
        name=name,
        transport="winrm",
        host="10.0.0.20",
        username="Administrator",
        auth={"method": "password", "password": "hunter2"},
        winrm=winrm,
    )
    return home


def test_doctor_winrm_https_needs_no_risk_notes(tmp_path: Path) -> None:
    """HTTPS + an explicit budget has nothing to warn about."""
    home = _winrm_home(
        tmp_path,
        name="lab-https",
        winrm={
            "scheme": "https",
            "message_encryption": "never",
            "probe_timeout_s": 30,
        },
    )
    report = run_doctor(env=_doctor_env(home))
    assert report.ok, "\n".join(c.line() for c in report.checks)
    names = [c.name for c in report.checks]
    assert "winrm lab-https" in names
    assert not [n for n in names if n.startswith("winrm http+encryption")]
    assert not [n for n in names if n.startswith("winrm probe budget")]
    check = next(c for c in report.checks if c.name == "winrm lab-https")
    assert "scheme=https" in check.detail
    assert "encryption=never" in check.detail
    assert "probe_timeout_s=30" in check.detail


def test_doctor_without_winrm_profile_adds_no_lines(tmp_path: Path) -> None:
    """No WinRM profile -> no WinRM section, no exit-code change."""
    home = tmp_path / "no-winrm"
    put_profile(home, name="box", transport="local")

    report = run_doctor(env=_doctor_env(home))
    assert report.ok, "\n".join(c.line() for c in report.checks)
    assert report.exit_code() == EXIT_OK
    names = [c.name for c in report.checks]
    assert names == [n for n in names if not n.startswith("winrm")]
    assert "profile box" in names


@pytest.mark.parametrize(
    "winrm",
    [
        {"scheme": "http"},
        {"scheme": "https", "message_encryption": "never"},
        {"ssl": "true", "message_encryption": "NEVER"},
        {"scheme": "http", "ssl": "false"},
    ],
)
def test_doctor_winrm_tokens_match_the_transport_the_open_builds(
    winrm: dict[str, Any],
) -> None:
    """doctor's scheme/auth/encryption tokens equal the transport's own knobs.

    A doctor line that disagrees with what ``endpoint open`` builds would
    report a policy the session does not use, so both sides read the same
    resolvers.
    """
    from mcp_remote_control.cli_cmds.doctor import _winrm_self_checks
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.endpoint.connect import _build_winrm_transport

    profile = Profile(
        name="lab",
        transport="winrm",
        host="10.0.0.20",
        username="Administrator",
        auth=AuthConfig(method="credssp", password="hunter2"),
        winrm=winrm,
    )
    checks = _winrm_self_checks(profile, global_winrm_probe=None, env={})
    detail = checks[0].detail
    transport = _build_winrm_transport(profile, connector=None)

    assert f"scheme={'https' if transport.ssl else 'http'}" in detail
    assert f"auth={transport.auth}" in detail
    assert f"encryption={transport.encryption}" in detail
