"""SSH identity fallback chain (004 / T06)."""

from __future__ import annotations

from pathlib import Path

from mcp_remote_control.config.models import AuthConfig, Profile
from mcp_remote_control.identity.ssh_keys import (
    DEFAULT_SSH_IDENTITY_BASENAMES,
    resolve_ssh_key_paths,
)


def test_default_order_id_rsa_before_ed25519() -> None:
    names = list(DEFAULT_SSH_IDENTITY_BASENAMES)
    assert names.index("id_rsa") < names.index("id_ed25519")
    assert names.index("id_ecdsa") < names.index("id_ed25519")


def test_explicit_key_path_only(tmp_path: Path) -> None:
    key = tmp_path / "only_this"
    key.write_text("dummy", encoding="utf-8")
    # Also plant default ids that must be ignored.
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("rsa", encoding="utf-8")
    (ssh_dir / "id_ed25519").write_text("ed", encoding="utf-8")

    profile = Profile(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth=AuthConfig(method="private_key_path", key_path=key),
    )
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [key]


def test_fallback_only_existing_in_order(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    # Create ed25519 and ecdsa only — rsa missing → not listed.
    ed = ssh_dir / "id_ed25519"
    ec = ssh_dir / "id_ecdsa"
    ed.write_text("ed", encoding="utf-8")
    ec.write_text("ec", encoding="utf-8")

    profile = Profile(name="x", transport="ssh", host="h", username="u", auth=None)
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [ec, ed]


def test_fallback_empty_when_none_exist(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    profile = Profile(name="x", transport="ssh", host="h", username="u")
    assert resolve_ssh_key_paths(profile, ssh_dir=ssh_dir) == []


def test_only_existing_false_returns_full_chain(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    profile = Profile(name="x", transport="ssh", host="h", username="u")
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir, only_existing=False)
    assert [p.name for p in paths] == list(DEFAULT_SSH_IDENTITY_BASENAMES)
