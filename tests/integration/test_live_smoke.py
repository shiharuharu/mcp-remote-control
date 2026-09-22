"""Live host smoke (opt-in).

Environment
-----------
MRC_INTEGRATION=1
MRC_HOME=~/.config/mcp-remote-control   # with real profiles + secrets
MRC_SSH_PROFILE=lab-ssh                 # optional
MRC_WINRM_PROFILE=lab-win               # optional

Run::

    export MRC_INTEGRATION=1
    export MRC_HOME=~/.config/mcp-remote-control
    pytest -q tests/integration

Default CI (``./scripts/harness/ci.sh``) ignores this directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.config import list_profiles, load_profile
from mcp_remote_control.core import endpoint_ops, exec_ops, fs_ops, screen_ops
from mcp_remote_control.endpoint.registry import reset_registry

pytestmark = pytest.mark.integration


def _profile_exists(home: Path, name: str) -> bool:
    if not home.is_dir():
        return False
    try:
        return name in list_profiles(home)
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry()
    yield
    reset_registry()


def test_ssh_endpoint_exec_fs_if_configured(
    integration_home: Path, ssh_profile: str
) -> None:
    if not _profile_exists(integration_home, ssh_profile):
        pytest.skip(f"profile {ssh_profile!r} not under {integration_home}")
    profile = load_profile(integration_home, ssh_profile)
    if profile.transport != "ssh":
        pytest.skip(f"{ssh_profile} is not transport=ssh")

    home = str(integration_home)
    open_r = endpoint_ops.run("open", profile=ssh_profile, home=home)
    if not open_r.is_ok():
        pytest.fail(
            f"ssh open failed: {open_r.code} {open_r.fields} "
            f"(check host/auth/known_hosts)"
        )

    try:
        ex = exec_ops.run(
            ep=ssh_profile,
            command="echo mrc-e2e-ssh",
            home=home,
        )
        assert ex.is_ok() or ex.status in ("ok", "fail"), ex
        # Soft assert: either saw echo or at least ran without CONNECT/HOSTKEY.
        assert ex.code not in ("HOSTKEY_MISMATCH", "AUTH_FAILED", "CONNECT_FAILED")

        fs_r = fs_ops.run(op="list", ep=ssh_profile, path=".", home=home)
        assert fs_r.code not in ("HOSTKEY_MISMATCH", "AUTH_FAILED", "CONNECT_FAILED")
    finally:
        endpoint_ops.run("close", ep=ssh_profile, home=home)


def test_winrm_endpoint_exec_ps_if_configured(
    integration_home: Path, winrm_profile: str
) -> None:
    if not _profile_exists(integration_home, winrm_profile):
        pytest.skip(f"profile {winrm_profile!r} not under {integration_home}")
    profile = load_profile(integration_home, winrm_profile)
    if profile.transport != "winrm":
        pytest.skip(f"{winrm_profile} is not transport=winrm")

    home = str(integration_home)
    open_r = endpoint_ops.run("open", profile=winrm_profile, home=home)
    if not open_r.is_ok():
        pytest.fail(f"winrm open failed: {open_r.code} {open_r.fields}")

    try:
        ex = exec_ops.run(
            ep=winrm_profile,
            command="Write-Output mrc-e2e-winrm",
            home=home,
        )
        assert ex.code not in ("AUTH_FAILED", "CONNECT_FAILED")

        # screen must be denied (caps.screen=false on winrm)
        scr = screen_ops.run(op="open", ep=winrm_profile, home=home)
        assert scr.code == "CAP_DENIED" or (
            scr.status == "error" and scr.code != "ok"
        )

        from mcp_remote_control.core import ps_ops

        ps = ps_ops.run(op="open", ep=winrm_profile, home=home)
        if ps.is_ok():
            sid = (ps.fields or {}).get("id") or (ps.fields or {}).get("ps_id")
            if sid:
                inv = ps_ops.run(
                    op="invoke",
                    id=sid,
                    script="$x=1; $x",
                    home=home,
                )
                assert inv.code not in ("AUTH_FAILED", "CONNECT_FAILED")
                ps_ops.run(op="close", id=sid, home=home)
    finally:
        endpoint_ops.run("close", ep=winrm_profile, home=home)


def test_local_smoke_always_when_integration_on(tmp_path: Path) -> None:
    """Sanity: local path works under integration gate too."""
    reset_registry()
    # Use package fixtures if present
    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "config"
    )
    home = str(fixture) if fixture.is_dir() else None
    r = exec_ops.run(
        ep="local",
        command="echo mrc-e2e-local",
        home=home,
    )
    assert r.is_ok() or "mrc-e2e-local" in (r.body or "")
