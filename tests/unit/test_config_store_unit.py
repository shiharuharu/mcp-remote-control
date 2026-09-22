"""Unit tests for config.store helpers (auth table, TOML AoT, public dict)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import (
    ProfileInvalid,
    ProfileNotFound,
    load_profile,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _assert_no_home_abs_in_msg(msg: str, home: Path) -> None:
    """Error messages must not embed the resolved config-home absolute prefix."""
    home_s = str(home.resolve())
    assert home_s not in msg, f"home abs leaked into msg: {msg!r}"
    # Common agent-lure prefixes (when home is under them).
    for prefix in ("/Users/", "/home/"):
        if home_s.startswith(prefix):
            assert prefix not in msg, f"{prefix!r} leaked into msg: {msg!r}"



def test_clean_auth_table_rejects_non_string_password() -> None:
    """_clean_auth_table raises for non-str password.

    Empty/whitespace string still treated as unset. Plain string kept.
    """
    from mcp_remote_control.config.store import _clean_auth_table

    for bad in (12345, True, False, ["x"], 3.14, {"k": "v"}):
        with pytest.raises(ProfileInvalid) as ei:
            _clean_auth_table({"method": "password", "password": bad})
        msg = str(ei.value).lower()
        assert "password" in msg
        assert "string" in msg

    # Empty / whitespace -> omit password key (unset), not type error.
    cleaned_empty = _clean_auth_table(
        {"method": "password", "password": ""}
    )
    assert "password" not in cleaned_empty
    assert cleaned_empty.get("method") == "password"
    cleaned_ws = _clean_auth_table(
        {"method": "password", "password": "  \t  "}
    )
    assert "password" not in cleaned_ws

    # Normal string password preserved (plain-password first-class).
    cleaned_ok = _clean_auth_table(
        {"method": "password", "password": "s3cr3t"}
    )
    assert cleaned_ok["password"] == "s3cr3t"


# ---------------------------------------------------------------------------
# list[dict] re-render must be legal TOML AoT (or inline table array),
# never Python str(dict) repr fragments.
# ---------------------------------------------------------------------------

def test_toml_array_dict_elements_are_inline_tables_not_python_repr() -> None:
    """_toml_array on list[dict] must not emit \"{'\" Python repr."""
    import tomllib

    from mcp_remote_control.config.store import _toml_array

    rendered = _toml_array(
        [{"host": "proxy1", "port": 22}, {"host": "proxy2", "port": 2222}]
    )
    assert "{'" not in rendered
    assert '"{' not in rendered  # quoted Python dict string
    assert "host" in rendered
    # Must be loadable as a bare array value.
    parsed = tomllib.loads(f"proxies = {rendered}\n")
    assert parsed["proxies"] == [
        {"host": "proxy1", "port": 22},
        {"host": "proxy2", "port": 2222},
    ]


def test_render_profile_from_dict_list_of_dicts_emits_aot() -> None:
    """Nested list[dict] under a table becomes [[seg]] AoT, not str(dict)."""
    import tomllib

    from mcp_remote_control.config.store import _render_profile_from_dict

    data = {
        "name": "aot-unit",
        "transport": "ssh",
        "host": "h",
        "username": "u",
        "ssh": {
            "connect_timeout_ms": 15000,
            "proxies": [
                {"host": "jump1.example", "port": 22},
                {"host": "jump2.example", "port": 2222, "user": "jump"},
            ],
        },
    }
    text = _render_profile_from_dict(data)
    assert "{'" not in text
    assert "[[ssh.proxies]]" in text
    # Scalar arrays still inline (not AoT).
    assert "connect_timeout_ms = 15000" in text
    parsed = tomllib.loads(text)
    assert parsed["ssh"]["proxies"] == [
        {"host": "jump1.example", "port": 22},
        {"host": "jump2.example", "port": 2222, "user": "jump"},
    ]
    assert parsed["ssh"]["connect_timeout_ms"] == 15000


def test_render_profile_from_dict_scalar_arrays_unchanged() -> None:
    """Normal scalar arrays stay as ``key = [..]`` (not AoT)."""
    import tomllib

    from mcp_remote_control.config.store import _render_profile_from_dict

    data = {
        "name": "scalars",
        "transport": "local",
        "caps": {"allowed_tools": ["exec", "fs"], "max_jobs": 3},
    }
    text = _render_profile_from_dict(data)
    assert "[[caps.allowed_tools]]" not in text
    assert "allowed_tools = [" in text
    parsed = tomllib.loads(text)
    assert parsed["caps"]["allowed_tools"] == ["exec", "fs"]
    assert parsed["caps"]["max_jobs"] == 3


def test_render_profile_from_dict_top_level_aot() -> None:
    """Top-level list[dict] also becomes [[name]] AoT."""
    import tomllib

    from mcp_remote_control.config.store import _render_profile_from_dict

    data = {
        "name": "top-aot",
        "transport": "local",
        "hooks": [{"on": "open", "cmd": "echo hi"}, {"on": "close", "cmd": "x"}],
    }
    text = _render_profile_from_dict(data)
    assert "[[hooks]]" in text
    assert "{'" not in text
    parsed = tomllib.loads(text)
    assert parsed["hooks"] == [
        {"on": "open", "cmd": "echo hi"},
        {"on": "close", "cmd": "x"},
    ]


# ---------------------------------------------------------------------------
# profile_public_dict exposes WinRM enterprise auth fields; strict_perms
# field kept in SecurityConfig (load.py constructs it as a kwarg).
# ---------------------------------------------------------------------------

def test_profile_public_dict_exposes_winrm_enterprise_auth_fields() -> None:
    """profile_public_dict must expose ALL non-secret AuthConfig fields
    (cert paths, spn, negotiate/credssp sub-fields, has_inline_private_key) so
    a WinRM cert/CredSSP profile's get_profile response is complete. Only
    paths / flags / non-secret strings - never secret bodies.

    Paths under config home are relative (secrets/...), not /home/u/... absolutes.
    """
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.config.store import profile_public_dict

    home = Path("/home/u")
    auth = AuthConfig(
        method="certificate",
        cert_path=home / "secrets" / "cert.pem",
        cert_key_path=home / "secrets" / "cert_key.pem",
        cert_key_password_path=home / "secrets" / "cert_key_pass",
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
        source_path=home / "profiles" / "win-enterprise.toml",
    )
    pub = profile_public_dict(profile, home=home)
    assert pub["name"] == "win-enterprise"
    assert pub["transport"] == "winrm"
    a = pub["auth"]
    # Every WinRM enterprise auth field is present and relative under home.
    assert a["method"] == "certificate"
    assert a["cert_path"] == "secrets/cert.pem"
    assert a["cert_key_path"] == "secrets/cert_key.pem"
    assert a["cert_key_password_path"] == "secrets/cert_key_pass"
    assert a["spn"] == "HOST/win.example.com"
    assert a["negotiate_hostname_override"] == "win.example.com"
    assert a["negotiate_service"] == "HOST"
    assert a["negotiate_delegate"] is True
    assert a["credssp_auth_mechanism"] == "ntlm"
    assert a["credssp_disable_tlsv1_2"] is False
    assert a["credssp_minimum_version"] == 2
    assert a["has_inline_private_key"] is True
    assert pub["source_path"] == "profiles/win-enterprise.toml"
    # No absolute home prefixes that would lure agents into shell ~/.config.
    blob = repr(pub) + str(pub)
    assert "/home/" not in blob
    assert "/Users/" not in blob
    # Sanity: no secret BODIES leak (AuthConfig never stores them, but assert
    # so a future field addition cannot smuggle one in via this view).
    for k, v in a.items():
        s = str(v)
        assert "BEGIN " not in s
        assert "PRIVATE KEY" not in s.upper()


def test_profile_public_dict_credssp_profile_omits_unset_fields() -> None:
    """Unset enterprise fields are omitted (not emitted as null) - matches
    the existing key_path/password_path style. A CredSSP profile with only
    password material must not synthesize cert_path etc. Paths relative."""
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.config.store import profile_public_dict

    home = Path("/home/u")
    auth = AuthConfig(
        method="credssp",
        password_path=home / "secrets" / "win_pw",
    )
    profile = Profile(
        name="win-credssp",
        transport="winrm",
        host="win.example.com",
        username="Admin",
        auth=auth,
        winrm={"scheme": "http"},
    )
    a = profile_public_dict(profile, home=home)["auth"]
    assert a["method"] == "credssp"
    assert a["password_path"] == "secrets/win_pw"
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
    """lab-win fixture serializes with relative secrets/ paths only - no
    secret body, no absolute /Users or /home prefixes."""
    from mcp_remote_control.config.store import profile_public_dict

    profile = load_profile(FIXTURES, "lab-win")
    # home inferred from source_path OR explicit - either must be relative.
    pub = profile_public_dict(profile, home=FIXTURES)
    a = pub["auth"]
    assert a["method"] == "password"
    assert a["password_path"] == "secrets/lab_win_password"
    assert pub.get("source_path") == "profiles/lab-win.toml"
    blob = repr(pub) + str(pub)
    assert "dummy-winrm-password" not in blob
    assert "BEGIN" not in blob
    assert "/home/" not in blob
    assert "/Users/" not in blob
    # Absolute on-disk path must not appear as source_path.
    assert str(FIXTURES) not in blob


def test_profile_public_dict_paths_relative_and_outside_home_kept(
    tmp_path: Path,
) -> None:
    """Under-home paths -> secrets/...; deliberate absolute outside home kept."""
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.config.store import profile_public_dict

    home = tmp_path / "mrc"
    (home / "secrets").mkdir(parents=True)
    (home / "profiles").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "id_rsa"
    outside.parent.mkdir(parents=True)
    outside.write_text("k", encoding="utf-8")

    auth = AuthConfig(
        method="private_key_path",
        key_path=outside,  # user-deliberate absolute outside config home
        password_path=home / "secrets" / "pw",
    )
    profile = Profile(
        name="mixed",
        transport="ssh",
        host="h",
        username="u",
        auth=auth,
        source_path=home / "profiles" / "mixed.toml",
    )
    pub = profile_public_dict(profile, home=home)
    a = pub["auth"]
    assert a["password_path"] == "secrets/pw"
    # Outside-home absolute is preserved (exception in acceptance).
    assert a["key_path"] == str(outside) or Path(a["key_path"]).is_absolute()
    assert str(outside.name) in a["key_path"]
    assert Path(a["key_path"]).is_absolute()
    assert pub["source_path"] == "profiles/mixed.toml"


def test_profile_public_dict_infers_home_from_source_path() -> None:
    """Without explicit home, source_path under profiles/ still relativizes."""
    from mcp_remote_control.config.store import profile_public_dict

    profile = load_profile(FIXTURES, "lab-ssh")
    pub = profile_public_dict(profile)  # no home= - infer from source_path
    assert pub["auth"]["key_path"] == "secrets/lab_ssh_ed25519"
    assert pub["source_path"] == "profiles/lab-ssh.toml"
    blob = repr(pub) + str(pub)
    assert "/home/" not in blob
    assert "/Users/" not in blob


def test_delete_profile_not_found_msg_relative(tmp_path: Path) -> None:
    """store.delete_profile ProfileNotFound uses profiles/... fragment."""
    from mcp_remote_control.config.store import delete_profile, ensure_home_layout

    home = tmp_path / "mrc"
    ensure_home_layout(home)
    with pytest.raises(ProfileNotFound) as ei:
        delete_profile(home, "gone")
    msg = str(ei.value)
    assert "profiles/gone.toml" in msg
    assert "gone" in msg
    _assert_no_home_abs_in_msg(msg, home)
