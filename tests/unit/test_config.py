"""Unit tests for mcp_remote_control.config (T03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import (
    ConfigError,
    ConfigInvalid,
    GlobalConfig,
    Profile,
    ProfileInvalid,
    ProfileNotFound,
    list_profiles,
    load_config,
    load_profile,
    resolve_home,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# resolve_home
# ---------------------------------------------------------------------------


def test_resolve_home_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MRC_HOME", raising=False)
    monkeypatch.delenv("MCP_REMOTE_CONTROL_HOME", raising=False)
    # Point HOME at tmp so we do not touch the real user config dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() on some platforms uses pwd, not HOME — also patch if needed.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()


def test_resolve_home_mrc_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "mrc-root"
    target.mkdir()
    monkeypatch.setenv("MRC_HOME", str(target))
    monkeypatch.setenv("MCP_REMOTE_CONTROL_HOME", str(tmp_path / "legacy"))

    home = resolve_home()
    assert home == target.resolve()


def test_resolve_home_legacy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "legacy-root"
    target.mkdir()
    monkeypatch.delenv("MRC_HOME", raising=False)
    monkeypatch.setenv("MCP_REMOTE_CONTROL_HOME", str(target))

    home = resolve_home()
    assert home == target.resolve()


def test_resolve_home_explicit_env_mapping(tmp_path: Path) -> None:
    target = tmp_path / "explicit"
    target.mkdir()
    home = resolve_home(env={"MRC_HOME": str(target)})
    assert home == target.resolve()


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert isinstance(cfg, GlobalConfig)
    assert cfg.from_defaults is True
    assert cfg.source_path is None
    assert cfg.defaults.verbosity == "normal"
    assert cfg.logging.level == "info"


def test_load_config_fixture() -> None:
    cfg = load_config(FIXTURES)
    assert cfg.from_defaults is False
    assert cfg.source_path is not None
    assert cfg.source_path.name == "config.toml"
    assert cfg.defaults.screen_cols == 120
    assert cfg.logging.audit is False
    # Fixture may still list removed legacy [security] keys; they are ignored.
    assert cfg.security.strict_perms is False


def test_load_config_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[[[not valid toml", encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    assert isinstance(ei.value, ConfigError)


# ---------------------------------------------------------------------------
# list_profiles / load_profile
# ---------------------------------------------------------------------------


def test_list_profiles_fixture() -> None:
    names = list_profiles(FIXTURES)
    assert "lab-ssh" in names
    assert "local" in names
    assert names == sorted(names)


def test_list_profiles_empty(tmp_path: Path) -> None:
    assert list_profiles(tmp_path) == []
    (tmp_path / "profiles").mkdir()
    assert list_profiles(tmp_path) == []


def test_load_good_ssh_profile() -> None:
    profile = load_profile(FIXTURES, "lab-ssh")
    assert isinstance(profile, Profile)
    assert profile.name == "lab-ssh"
    assert profile.transport == "ssh"
    assert profile.host == "10.0.0.5"
    assert profile.port == 22
    assert profile.username == "deploy"
    assert profile.label == "Lab SSH fixture"
    assert profile.auth is not None
    assert profile.auth.method == "private_key_path"
    assert profile.auth.key_path is not None
    assert profile.auth.key_path.name == "lab_ssh_ed25519"
    assert "secrets" in profile.auth.key_path.parts
    assert profile.defaults.get("cwd") == "/var/www"


def test_load_local_profile() -> None:
    profile = load_profile(FIXTURES, "local")
    assert profile.transport == "local"
    assert profile.host is None
    assert profile.auth is None


def test_load_profile_not_found(tmp_path: Path) -> None:
    (tmp_path / "profiles").mkdir()
    with pytest.raises(ProfileNotFound) as ei:
        load_profile(tmp_path, "missing")
    assert isinstance(ei.value, ConfigError)


def test_load_profile_bad_toml(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "broken.toml").write_text("name = [unterminated\n", encoding="utf-8")
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "broken")
    assert "TOML" in str(ei.value) or "invalid" in str(ei.value).lower()
    assert isinstance(ei.value, ConfigError)


def test_load_profile_missing_required_field(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    # transport missing
    (pdir / "no-transport.toml").write_text(
        'name = "no-transport"\nhost = "1.2.3.4"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "no-transport")
    assert "transport" in str(ei.value)


def test_load_profile_missing_host_for_ssh(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "nohost.toml").write_text(
        'name = "nohost"\ntransport = "ssh"\nusername = "u"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "nohost")
    assert "host" in str(ei.value)


def test_load_profile_name_mismatch(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "alpha.toml").write_text(
        'name = "beta"\ntransport = "local"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "alpha")
    assert "does not match" in str(ei.value)


def test_load_profile_rejects_traversal_name(tmp_path: Path) -> None:
    """H8: load_profile must reject path-traversal names with ProfileInvalid
    and never read files outside profiles/."""
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    # Plant a toml file OUTSIDE profiles/ that must never be read.
    planted = tmp_path / "config.toml"
    planted_content = 'name = "config"\ntransport = "local"\n'
    planted.write_text(planted_content, encoding="utf-8")
    for bad in ("../config", "..%2f", "a/../b", "..", "../", "/etc/passwd"):
        with pytest.raises(ProfileInvalid):
            load_profile(tmp_path, bad)
    # The planted outside file must be byte-for-byte untouched (proves the
    # ``../config`` case never opened it).
    assert planted.read_text() == planted_content


def test_load_profile_rejects_trailing_newline_name(tmp_path: Path) -> None:
    """LOW 1: fullmatch (not ``$``) rejects a trailing newline in the name."""
    (tmp_path / "profiles").mkdir()
    for bad in ("box\n", "box "):
        with pytest.raises(ProfileInvalid):
            load_profile(tmp_path, bad)


def test_secret_path_stored_but_contents_not_in_repr() -> None:
    profile = load_profile(FIXTURES, "lab-ssh")
    assert profile.auth is not None
    assert profile.auth.key_path is not None
    # Path is recorded…
    assert profile.auth.key_path.exists()
    secret_body = profile.auth.key_path.read_text(encoding="utf-8")
    assert "DUMMY_FIXTURE_KEY" in secret_body

    # …but file contents must not appear in str/repr of profile or auth.
    text = repr(profile) + str(profile) + repr(profile.auth) + str(profile.auth)
    assert "DUMMY_FIXTURE_KEY" not in text
    assert "BEGIN OPENSSH" not in text
    # Path (or at least the filename) may appear — that is fine.
    assert "lab_ssh_ed25519" in text or "key_path" in text


def test_inline_password_not_stored_in_repr(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "with-pass.toml").write_text(
        "\n".join(
            [
                'name = "with-pass"',
                'transport = "ssh"',
                'host = "10.0.0.1"',
                'username = "root"',
                "[auth]",
                'method = "password"',
                'password = "s3cr3t-should-not-leak"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    profile = load_profile(tmp_path, "with-pass")
    assert profile.auth is not None
    assert profile.auth.has_inline_password is True
    blob = repr(profile) + str(profile)
    assert "s3cr3t-should-not-leak" not in blob


# ---------------------------------------------------------------------------
# C5: profile_public_dict exposes WinRM enterprise auth fields; strict_perms
# field kept in SecurityConfig (load.py constructs it as a kwarg).
# ---------------------------------------------------------------------------


def test_profile_public_dict_exposes_winrm_enterprise_auth_fields() -> None:
    """C5: profile_public_dict must expose ALL non-secret AuthConfig fields
    (cert paths, spn, negotiate/credssp sub-fields, has_inline_private_key) so
    a WinRM cert/CredSSP profile's get_profile response is complete. Only
    paths / flags / non-secret strings — never secret bodies."""
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.config.store import profile_public_dict

    auth = AuthConfig(
        method="certificate",
        cert_path=Path("/home/u/secrets/cert.pem"),
        cert_key_path=Path("/home/u/secrets/cert_key.pem"),
        cert_key_password_path=Path("/home/u/secrets/cert_key_pass"),
        spn="HOST/win.example.com",
        negotiate_hostname_override="win.example.com",
        negotiate_service="HOST",
        negotiate_delegate=True,
        credssp_auth_mechanism="ntlm",
        credssp_disable_tlsv1_2=False,
        credssp_minimum_version=2,
        has_inline_private_key=True,
    )
    profile = Profile(
        name="win-enterprise",
        transport="winrm",
        host="win.example.com",
        port=5986,
        username="Admin",
        auth=auth,
        winrm={"scheme": "https", "auth": "certificate"},
    )
    pub = profile_public_dict(profile)
    assert pub["name"] == "win-enterprise"
    assert pub["transport"] == "winrm"
    a = pub["auth"]
    # Every WinRM enterprise auth field is present and correct.
    assert a["method"] == "certificate"
    assert a["cert_path"] == "/home/u/secrets/cert.pem"
    assert a["cert_key_path"] == "/home/u/secrets/cert_key.pem"
    assert a["cert_key_password_path"] == "/home/u/secrets/cert_key_pass"
    assert a["spn"] == "HOST/win.example.com"
    assert a["negotiate_hostname_override"] == "win.example.com"
    assert a["negotiate_service"] == "HOST"
    assert a["negotiate_delegate"] is True
    assert a["credssp_auth_mechanism"] == "ntlm"
    assert a["credssp_disable_tlsv1_2"] is False
    assert a["credssp_minimum_version"] == 2
    assert a["has_inline_private_key"] is True
    # Sanity: no secret BODIES leak (AuthConfig never stores them, but assert
    # so a future field addition cannot smuggle one in via this view).
    for k, v in a.items():
        s = str(v)
        assert "BEGIN " not in s
        assert "PRIVATE KEY" not in s.upper()


def test_profile_public_dict_credssp_profile_omits_unset_fields() -> None:
    """C5: unset enterprise fields are omitted (not emitted as null) — matches
    the existing key_path/password_path style. A CredSSP profile with only
    password material must not synthesize cert_path etc."""
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.config.store import profile_public_dict

    auth = AuthConfig(
        method="credssp",
        password_path=Path("/home/u/secrets/win_pw"),
    )
    profile = Profile(
        name="win-credssp",
        transport="winrm",
        host="win.example.com",
        username="Admin",
        auth=auth,
        winrm={"scheme": "http"},
    )
    a = profile_public_dict(profile)["auth"]
    assert a["method"] == "credssp"
    assert a["password_path"] == "/home/u/secrets/win_pw"
    # Unset enterprise fields are absent (not None).
    for absent in (
        "cert_path",
        "cert_key_path",
        "cert_key_password_path",
        "spn",
        "negotiate_hostname_override",
        "negotiate_service",
        "negotiate_delegate",
        "credssp_auth_mechanism",
        "credssp_disable_tlsv1_2",
        "credssp_minimum_version",
        "has_inline_private_key",
        "has_inline_password",
        "key_path",
        "passphrase_path",
        "password_env",
    ):
        assert absent not in a, f"{absent!r} should be omitted when unset"


def test_profile_public_dict_lab_win_fixture_no_secret_bodies() -> None:
    """C5 regression: the existing lab-win fixture (password method) still
    serializes via profile_public_dict with paths only — no secret body."""
    from mcp_remote_control.config.store import profile_public_dict

    profile = load_profile(FIXTURES, "lab-win")
    pub = profile_public_dict(profile)
    a = pub["auth"]
    assert a["method"] == "password"
    assert a["password_path"].endswith("secrets/lab_win_password")
    # The secret file's BODY must not appear in the public view.
    blob = repr(pub) + str(pub)
    assert "dummy-winrm-password" not in blob
    assert "BEGIN" not in blob


def test_load_config_strict_perms_true(tmp_path: Path) -> None:
    """C5: a config with [security] strict_perms=true loads into SecurityConfig
    (field is kept, not removed — load.py constructs it as a kwarg, so removal
    would raise TypeError). Verify the field round-trips through load_config."""
    (tmp_path / "config.toml").write_text(
        "[security]\nstrict_perms = true\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.security.strict_perms is True
    # Default (unset) remains False.
    other = tmp_path / "other"
    other.mkdir()
    assert load_config(other).security.strict_perms is False


def test_load_config_ignores_unknown_security_keys(tmp_path: Path) -> None:
    """Removed security knobs must not break load (migration: ignore unknown)."""
    (tmp_path / "config.toml").write_text(
        "[security]\n"
        "redact_secrets_in_logs = false\n"
        "allow_secret_paths_in_output = false\n"
        "strict_perms = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.security.strict_perms is True
    assert not hasattr(cfg.security, "redact_secrets_in_logs")
    assert not hasattr(cfg.security, "allow_secret_paths_in_output")
