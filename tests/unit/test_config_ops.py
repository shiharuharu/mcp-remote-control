"""Core config_ops.run proxy: ensure_home, put/get/delete, help, body rejects."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

from mcp_remote_control.config import list_profiles, load_profile
from mcp_remote_control.core import config_ops, endpoint_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint import reset_registry

_NOTES_TRUNC_RE = re.compile(
    r"\u2026\(truncated middle: kept_head=(\d+) kept_tail=(\d+) "
    r"total=(\d+) chars\)\u2026"
)
_PEM_NOTES = (
    "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n"
)


def test_ensure_home_and_put_local_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    r = config_ops.run("ensure_home")
    assert r.is_ok()
    assert (tmp_path / "mrc" / "profiles").is_dir()
    assert (tmp_path / "mrc" / "notes").is_dir()
    assert (tmp_path / "mrc" / "config.toml").is_file()
    assert "notes" in (r.fields.get("dirs") or "")

    r2 = config_ops.run(
        "put_profile",
        name="box",
        transport="local",
        label="dev box",
        defaults={"cwd": "/tmp"},
    )
    assert r2.is_ok(), r2.fields
    assert "box" in list_profiles(tmp_path / "mrc")
    p = load_profile(tmp_path / "mrc", "box")
    assert p.transport == "local"
    assert p.defaults.get("cwd") == "/tmp"

    r3 = config_ops.run("list_profiles")
    assert r3.is_ok()
    assert "name=box" in (r3.body or "")

    r4 = config_ops.run("get_profile", name="box")
    assert r4.is_ok()
    assert r4.fields.get("transport") == "local"
    assert "password" not in (r4.body or "").lower() or "password_path" in (
        r4.body or ""
    )


def test_put_profile_then_endpoint_open(tmp_path: Path, monkeypatch) -> None:
    """Agent bootstrap path: config write -> endpoint open (no hand-edited TOML)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    try:
        assert config_ops.run("ensure_home").is_ok()
        put = config_ops.run(
            "put_profile",
            name="localdev",
            transport="local",
            label="agent-made",
        )
        assert put.is_ok(), put.fields
        opened = endpoint_ops.run("open", profile="localdev")
        assert opened.is_ok(), opened.fields
        assert opened.fields.get("transport") == "local"
        assert opened.fields.get("ep") == "localdev"
        closed = endpoint_ops.run("close", ep="localdev")
        assert closed.is_ok(), closed.fields
    finally:
        reset_registry()


def test_put_secret_and_ssh_profile(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")

    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    rs = config_ops.run("put_secret", name="id_test", content=pem)
    assert rs.is_ok()
    assert rs.fields.get("path") == "secrets/id_test"
    assert "BEGIN" not in (rs.body or "")
    assert (home / "secrets" / "id_test").is_file()
    assert pem in (home / "secrets" / "id_test").read_text()

    rp = config_ops.run(
        "put_profile",
        name="edge",
        transport="ssh",
        host="10.1.2.3",
        username="root",
        port=22,
        auth={
            "method": "private_key_path",
            "key_path": "secrets/id_test",
        },
        ssh={"known_hosts": "none", "connect_timeout_ms": 10000},
    )
    assert rp.is_ok(), rp.fields
    assert rp.fields.get("path") == "profiles/edge.toml"
    p = load_profile(home, "edge")
    assert p.host == "10.1.2.3"
    assert p.auth is not None
    assert p.auth.method == "private_key_path"

    ls = config_ops.run("list_secrets")
    assert ls.is_ok()
    assert "id_test" in (ls.fields.get("names") or "")

    d = config_ops.run("delete_profile", name="edge")
    assert d.is_ok()
    assert "edge" not in list_profiles(home)


def test_put_profile_password_secret_alias(tmp_path: Path, monkeypatch) -> None:
    """Agent shorthand auth.password_secret= maps to password_path under secrets/."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run(
        "put_secret", name="win210-pass", content="c20051010"
    ).is_ok()
    r = config_ops.run(
        "put_profile",
        name="win210",
        transport="ssh",
        host="10.5.10.210",
        username="shiharu",
        auth={"password_secret": "win210-pass"},
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "win210")
    assert p.auth is not None
    assert p.auth.method == "password"
    assert p.auth.password_path is not None
    assert p.auth.password_path.name == "win210-pass"


def test_put_profile_inline_password_plain(tmp_path: Path, monkeypatch) -> None:
    """Product rule: password may be written plain into the profile."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    r = config_ops.run(
        "put_profile",
        name="win210",
        transport="ssh",
        host="10.5.10.210",
        username="shiharu",
        auth={"method": "password", "password": "c20051010"},
        ssh={"known_hosts": "none"},
    )
    assert r.is_ok(), r.fields
    text = (home / "profiles" / "win210.toml").read_text()
    assert 'password = "c20051010"' in text
    p = load_profile(home, "win210")
    assert p.auth is not None
    assert p.auth.password == "c20051010"
    from mcp_remote_control.endpoint.registry import _resolve_password

    assert _resolve_password(p) == "c20051010"


def test_config_help_and_no_absolute_home_in_list(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    help_r = config_ops.run("help")
    assert help_r.is_ok()
    body = help_r.body or ""
    # Preferred bootstrap is inline password recipe + lab known_hosts.
    assert 'auth={"method":"password","password":' in body
    assert "known_hosts" in body and "none" in body
    assert "put_profile" in body
    # Alternate auth recipes must be keyword-searchable in help body.
    body_l = body.lower()
    assert "ssh_agent" in body
    assert "password_env" in body
    assert "certificate" in body_l or "cert_path" in body
    assert "credssp" in body_l  # CredSSP / credssp
    # put_secret labeled optional; do not guide shell/TOML hand-edit.
    assert "optional put_secret" in body or "put_secret optional" in body.lower() or (
        "optional" in body.lower() and "put_secret" in body
    )
    assert "shell-edit" in body or "Do not shell-edit" in body
    # Must not steer agents into shell-editing ~/.config or MRC_HOME TOML.
    assert "vi " not in body_l and "nano " not in body_l
    assert "echo " not in body_l or "shell-edit" in body
    assert help_r.hint is None or "shell" in (help_r.hint or "").lower() or "TOML" in (
        help_r.hint or ""
    )
    # Bootstrap hints must not put_secret-first.
    eh = config_ops.run("ensure_home")
    assert eh.is_ok()
    eh_hint = eh.hint or ""
    assert "password" in eh_hint
    assert eh_hint.find("put_profile") < eh_hint.find("put_secret") or "optional" in (
        eh_hint.lower()
    )
    home_r = config_ops.run("home")
    home_hint = home_r.hint or ""
    assert "password" in home_hint
    assert "put_secret optional" in home_hint.lower() or "optional" in home_hint.lower()
    assert "notes" in (home_r.fields.get("layout") or "")
    assert "notes" in body.lower()
    inv = config_ops.run("not_a_real_op")
    assert inv.code == "INVALID_OP"
    inv_hint = inv.hint or ""
    assert "put_profile" in inv_hint
    assert "password" in inv_hint or "help" in inv_hint
    # put_secret must not be the sole bootstrap step before put_profile.
    assert inv_hint.find("put_profile") <= inv_hint.find("put_secret") or "optional" in (
        inv_hint.lower()
    )
    lp = config_ops.run("list_profiles")
    assert lp.is_ok()
    assert "home=" not in (lp.fields or {})
    # Agent meta must not push absolute MRC_HOME for list_profiles.
    assert str(home) not in str(lp.fields)
    assert str(home) not in str(eh.fields)
    assert eh.fields.get("ready") == 1


def test_op_delete_profile_missing_returns_profile_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    """op_delete_profile on a missing name must surface PROFILE_NOT_FOUND
    (not CONFIG_WRITE_FAILED) now that store.delete_profile raises
    ProfileNotFound instead of FileNotFoundError."""
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    config_ops.run("ensure_home")
    r = config_ops.run("delete_profile", name="does_not_exist")
    assert r.status == "error"
    assert r.code == "PROFILE_NOT_FOUND"


def test_put_profile_rejects_bad_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    config_ops.run("ensure_home")
    r = config_ops.run("put_profile", name="../evil", transport="local")
    assert r.status == "error"


def test_put_profile_rejects_secrets_in_non_auth_sections(
    tmp_path: Path, monkeypatch
) -> None:
    """ssh=/winrm= must not persist secret bodies."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")

    r_ssh = config_ops.run(
        "put_profile",
        name="badssh",
        transport="ssh",
        host="10.0.0.1",
        username="root",
        ssh={"password": "leak"},
    )
    assert r_ssh.status == "error"
    assert not (home / "profiles" / "badssh.toml").is_file()

    r_win = config_ops.run(
        "put_profile",
        name="badwin",
        transport="winrm",
        host="10.0.0.2",
        username="Admin",
        winrm={"client_secret": "leak"},
    )
    assert r_win.status == "error"
    assert not (home / "profiles" / "badwin.toml").is_file()

    # Nested secret under a sub-table must also be rejected.
    r_nested = config_ops.run(
        "put_profile",
        name="badnest",
        transport="winrm",
        host="10.0.0.3",
        username="Admin",
        winrm={"credssp": {"client_secret": "leak"}},
    )
    assert r_nested.status == "error"
    assert not (home / "profiles" / "badnest.toml").is_file()


def test_put_profile_rejects_ssh_private_key_pem(
    tmp_path: Path, monkeypatch
) -> None:
    """Non-auth ``private_key_pem`` must be rejected (not only password)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")

    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "fake-body\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    r = config_ops.run(
        "put_profile",
        name="badpem",
        transport="ssh",
        host="10.0.0.9",
        username="root",
        ssh={"private_key_pem": pem},
    )
    assert r.status == "error"
    assert not (home / "profiles" / "badpem.toml").is_file()
    assert "badpem" not in list_profiles(home)

    # body= path must also reject and leave nothing on disk.
    body = (
        'name = "badpem2"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[ssh]\n"
        'private_key_pem = "PEM_BODY_LEAK"\n'
    )
    r2 = config_ops.run("put_profile", name="badpem2", body=body)
    assert r2.status == "error"
    assert not (home / "profiles" / "badpem2.toml").is_file()


def test_put_profile_auth_path_based_key_still_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """Path-based auth keys remain accepted; only secret bodies are blocked."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    config_ops.run("put_secret", name="id_ok", content="fake-key-material\n")
    r = config_ops.run(
        "put_profile",
        name="okssh",
        transport="ssh",
        host="10.0.0.8",
        username="root",
        auth={
            "method": "private_key_path",
            "key_path": "secrets/id_ok",
        },
        ssh={"known_hosts": "none"},
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "okssh")
    assert p.auth is not None
    assert p.auth.key_path is not None
    assert "id_ok" in str(p.auth.key_path)
    text = (home / "profiles" / "okssh.toml").read_text(encoding="utf-8")
    assert "fake-key-material" not in text
    assert "private_key_pem" not in text


def test_put_profile_body_missing_name_prepended(tmp_path: Path, monkeypatch) -> None:
    """Body whose value contains the substring 'name' (e.g. username="name")
    but has NO top-level name key must still get a real name= prepended.

    Unquoted ``transport=local`` is invalid TOML, so the quoted equivalent
    is used. A naive ``"name" in text`` would still fire via
    ``username = "name"``.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="box",
        body='transport = "local"\nusername = "name"\n',
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "box")
    assert p.name == "box"
    assert p.transport == "local"
    assert p.username == "name"


def test_put_profile_body_invalid_toml_handled_gracefully(
    tmp_path: Path, monkeypatch
) -> None:
    """An unparseable body is rejected with a clear ProfileInvalid (no crash,
    no partial file). ``transport=local`` is invalid TOML (unquoted value)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="box",
        body="transport=local\nusername=name\n",
    )
    assert r.status == "error"
    # Round-trip validation surfaces the TOML error; no profile persisted.
    assert "box" not in list_profiles(home)


def test_put_profile_body_correct_name_kept(
    tmp_path: Path, monkeypatch
) -> None:
    """Body with a matching name key is kept verbatim (no duplicate)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="box",
        body='name = "box"\ntransport = "local"\n',
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "box")
    assert p.name == "box"
    # No duplicate name key (would be invalid TOML -> load would have failed).
    text = (home / "profiles" / "box.toml").read_text()
    assert text.count("name =") == 1


def test_put_profile_body_wrong_name_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """Body with a mismatched name is rejected with ProfileInvalid."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="box",
        body='name = "other"\ntransport = "local"\n',
    )
    assert r.status == "error"
    assert not (home / "profiles" / "box.toml").is_file()


def test_put_profile_nested_winrm_dict_roundtrips(
    tmp_path: Path, monkeypatch
) -> None:
    """Nested dict under winrm renders as [winrm.credssp] and round-trips."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="wintest",
        transport="winrm",
        host="10.0.0.20",
        username="Admin",
        winrm={
            "scheme": "http",
            "credssp": {"auth_mechanism": "ntlm"},
        },
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "wintest")
    assert isinstance(p.winrm.get("credssp"), dict)
    assert p.winrm["credssp"]["auth_mechanism"] == "ntlm"
    # The file on disk must use a [winrm.credssp] sub-table header.
    text = (home / "profiles" / "wintest.toml").read_text()
    assert "[winrm.credssp]" in text


def test_put_profile_body_with_ssh_secret_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """body= with a secret in a non-auth section must be rejected and
    no file persisted."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    body = (
        'name = "leak2"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[ssh]\n"
        'password = "SSH_PW_LEAK"\n'
    )
    r = config_ops.run("put_profile", name="leak2", body=body)
    assert r.status == "error"
    # No file persisted.
    assert not (home / "profiles" / "leak2.toml").is_file()
    assert "leak2" not in list_profiles(home)


def test_put_profile_body_clean_roundtrips(tmp_path: Path, monkeypatch) -> None:
    """A body= with no secrets still works (round-trips)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    body = (
        'name = "clean"\n'
        'transport = "ssh"\n'
        'host = "10.0.0.5"\n'
        'username = "deploy"\n'
        "[ssh]\n"
        "connect_timeout_ms = 15000\n"
    )
    r = config_ops.run("put_profile", name="clean", body=body)
    assert r.is_ok(), r.fields
    p = load_profile(home, "clean")
    assert p.name == "clean"
    assert p.transport == "ssh"
    assert p.ssh.get("connect_timeout_ms") == 15000


def test_put_profile_body_auth_inline_password_kept(
    tmp_path: Path, monkeypatch
) -> None:
    """body= with [auth].password keeps the plain password (product rule).
    on disk."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    body = (
        'name = "stripped"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[auth]\n"
        'method = "password"\n'
        'password = "INLINE_LEAK"\n'
    )
    r = config_ops.run("put_profile", name="stripped", body=body)
    assert r.is_ok(), r.fields
    on_disk = (home / "profiles" / "stripped.toml").read_text()
    assert 'password = "INLINE_LEAK"' in on_disk
    p = load_profile(home, "stripped")
    assert p.auth is not None
    assert p.auth.method == "password"
    assert p.auth.password == "INLINE_LEAK"


def test_put_profile_body_auth_strip_preserves_ssh_proxies_aot(
    tmp_path: Path, monkeypatch
) -> None:
    """body= that strips [auth] inline secrets must re-render without
    corrupting [[ssh.proxies]] (array-of-tables).

    Trigger: private_key_pem in [auth] -> auth_stripped -> _render_profile_from_dict.
    Dict elements must not become quoted Python repr or the proxies structure
    is lost / unloadable as tables.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    config_ops.run("put_secret", name="id_aot", content="fake-key-material\n")
    body = (
        'name = "aot-strip"\n'
        'transport = "ssh"\n'
        'host = "10.0.0.9"\n'
        'username = "deploy"\n'
        "[auth]\n"
        'method = "private_key_path"\n'
        'private_key_pem = "-----BEGIN FAKE KEY-----\\nLEAK\\n-----END FAKE KEY-----"\n'
        'key_path = "secrets/id_aot"\n'
        "[ssh]\n"
        "connect_timeout_ms = 12000\n"
        "[[ssh.proxies]]\n"
        'host = "jump1.example"\n'
        "port = 22\n"
        "[[ssh.proxies]]\n"
        'host = "jump2.example"\n'
        "port = 2222\n"
        'user = "jump"\n'
    )
    r = config_ops.run("put_profile", name="aot-strip", body=body)
    assert r.is_ok(), r.fields
    on_disk = (home / "profiles" / "aot-strip.toml").read_text()
    # Secret stripped; no Python dict-repr corruption.
    assert "BEGIN FAKE KEY" not in on_disk
    assert "private_key_pem" not in on_disk
    assert "{'" not in on_disk
    assert "[[ssh.proxies]]" in on_disk or "proxies = [" in on_disk
    # Structurally round-trippable via load_profile.
    p = load_profile(home, "aot-strip")
    assert p.transport == "ssh"
    assert p.ssh.get("connect_timeout_ms") == 12000
    proxies = p.ssh.get("proxies")
    assert isinstance(proxies, list)
    assert len(proxies) == 2
    assert proxies[0]["host"] == "jump1.example"
    assert proxies[0]["port"] == 22
    assert proxies[1]["host"] == "jump2.example"
    assert proxies[1]["port"] == 2222
    assert proxies[1]["user"] == "jump"
    # Auth kept path field; method preserved; inline PEM stripped.
    assert p.auth is not None
    assert p.auth.method == "private_key_path"
    assert p.auth.key_path is not None


def test_put_profile_body_no_strip_keeps_aot_verbatim(
    tmp_path: Path, monkeypatch
) -> None:
    """Non-strip body path leaves original [[ssh.proxies]] text intact."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    body = (
        'name = "aot-keep"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[ssh]\n"
        "connect_timeout_ms = 5000\n"
        "[[ssh.proxies]]\n"
        'host = "bastion"\n'
        "port = 22\n"
    )
    r = config_ops.run("put_profile", name="aot-keep", body=body)
    assert r.is_ok(), r.fields
    on_disk = (home / "profiles" / "aot-keep.toml").read_text()
    # No auth strip -> original body formatting (AoT headers) preserved.
    assert "[[ssh.proxies]]" in on_disk
    p = load_profile(home, "aot-keep")
    assert p.ssh["proxies"][0]["host"] == "bastion"


def _assert_no_abs_home_leak(text: str) -> None:
    """Agent UX gate: get_profile must not emit absolute config-home paths."""
    assert "/Users/" not in text
    assert "/home/" not in text


def test_get_profile_body_relative_paths_no_abs_home(
    tmp_path: Path, monkeypatch
) -> None:
    """get_profile body/fields use secrets/... and profiles/... only."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    assert config_ops.run("put_secret", name="id_j1", content=pem).is_ok()
    put = config_ops.run(
        "put_profile",
        name="edge-j1",
        transport="ssh",
        host="10.1.2.3",
        username="root",
        auth={"method": "private_key_path", "key_path": "secrets/id_j1"},
        ssh={"known_hosts": "none"},
    )
    assert put.is_ok(), put.fields

    r = config_ops.run("get_profile", name="edge-j1")
    assert r.is_ok(), r.fields
    body = r.body or ""
    _assert_no_abs_home_leak(body)
    _assert_no_abs_home_leak(repr(r.fields))
    assert "auth.key_path=secrets/id_j1" in body
    assert "source_path=profiles/edge-j1.toml" in body
    # Nested profile dict also relative.
    prof = r.fields.get("profile") or {}
    auth = prof.get("auth") or {}
    assert auth.get("key_path") == "secrets/id_j1"
    assert prof.get("source_path") == "profiles/edge-j1.toml"
    assert str(home) not in body
    assert str(home) not in repr(prof)


def test_get_profile_fixture_lab_ssh_no_abs_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture secrets under MRC_HOME=tests/fixtures/config stay relative."""
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "config"
    monkeypatch.setenv("MRC_HOME", str(fixtures))
    r = config_ops.run("get_profile", name="lab-ssh")
    assert r.is_ok(), r.fields
    body = r.body or ""
    _assert_no_abs_home_leak(body)
    _assert_no_abs_home_leak(repr(r.fields.get("profile")))
    assert "auth.key_path=secrets/lab_ssh_ed25519" in body
    assert "source_path=profiles/lab-ssh.toml" in body
    # Absolute fixture path must not appear.
    assert str(fixtures) not in body
    assert str(fixtures) not in repr(r.fields.get("profile"))


def test_get_profile_fixture_lab_win_password_path_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lab-win password_path is secrets/lab_win_password in get_profile."""
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "config"
    monkeypatch.setenv("MRC_HOME", str(fixtures))
    r = config_ops.run("get_profile", name="lab-win")
    assert r.is_ok(), r.fields
    body = r.body or ""
    _assert_no_abs_home_leak(body)
    assert "auth.password_path=secrets/lab_win_password" in body
    assert "source_path=profiles/lab-win.toml" in body
    # Plain password product rule still holds (no secret body from file).
    assert "dummy-winrm-password" not in body
    assert "dummy-winrm-password" not in repr(r.fields)


def test_put_profile_open_no_shell_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent completes put_profile + open without shell/Read."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    try:
        assert config_ops.run("ensure_home").is_ok()
        # Password first-class - no secrets/ shell write required.
        put = config_ops.run(
            "put_profile",
            name="agent-local",
            transport="local",
            label="no-shell",
        )
        assert put.is_ok(), put.fields
        gp = config_ops.run("get_profile", name="agent-local")
        assert gp.is_ok()
        _assert_no_abs_home_leak(gp.body or "")
        opened = endpoint_ops.run("open", profile="agent-local")
        assert opened.is_ok(), opened.fields
        assert opened.fields.get("ep") == "agent-local"
        assert endpoint_ops.run("close", ep="agent-local").is_ok()
    finally:
        reset_registry()


def test_put_profile_empty_password_not_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """put_profile with password=\"\" / whitespace does not claim material."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    r = config_ops.run(
        "put_profile",
        name="blankpw",
        transport="ssh",
        host="10.0.0.9",
        username="root",
        auth={"method": "password", "password": "   "},
        ssh={"known_hosts": "none"},
    )
    assert r.is_ok(), r.fields
    p = load_profile(home, "blankpw")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    on_disk = (home / "profiles" / "blankpw.toml").read_text(encoding="utf-8")
    # Blank password must not be written as a password= key (method=password ok).
    assert "password =" not in on_disk


def test_put_profile_non_string_password_profile_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-string password (int/bool/list) -> PROFILE_INVALID; no silent ok."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()

    for i, bad in enumerate((12345, True, False, ["x"], {"k": "v"})):
        name = f"badtype{i}"
        r = config_ops.run(
            "put_profile",
            name=name,
            transport="ssh",
            host="10.0.0.9",
            username="root",
            auth={"method": "password", "password": bad},
            ssh={"known_hosts": "none"},
        )
        assert r.status == "error", f"case {bad!r} should fail"
        assert r.code == "PROFILE_INVALID", r.fields
        msg = (r.fields.get("msg") or "").lower()
        assert "password" in msg
        assert "string" in msg
        assert not (home / "profiles" / f"{name}.toml").is_file()

    # Positive control: plain string password still written + reloadable.
    r_ok = config_ops.run(
        "put_profile",
        name="goodpw",
        transport="ssh",
        host="10.0.0.9",
        username="root",
        auth={"method": "password", "password": "s3cr3t"},
        ssh={"known_hosts": "none"},
    )
    assert r_ok.is_ok(), r_ok.fields
    text = (home / "profiles" / "goodpw.toml").read_text(encoding="utf-8")
    assert 'password = "s3cr3t"' in text
    p = load_profile(home, "goodpw")
    assert p.auth is not None
    assert p.auth.password == "s3cr3t"


def test_put_profile_body_non_string_password_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """body= TOML with password = 12345 (int) -> PROFILE_INVALID; no file."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    body = (
        'name = "bodyintpw"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[auth]\n"
        'method = "password"\n'
        "password = 12345\n"
    )
    r = config_ops.run("put_profile", name="bodyintpw", body=body)
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    msg = (r.fields.get("msg") or "").lower()
    assert "password" in msg
    assert "string" in msg
    assert not (home / "profiles" / "bodyintpw.toml").is_file()


def test_put_profile_dual_password_sources_fail_clear_msg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dual password sources -> put fails with choose-exactly-one message."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run(
        "put_secret", name="dual_pw", content="from-secret\n"
    ).is_ok()

    dual_cases = [
        {
            "method": "password",
            "password": "inline",
            "password_path": "secrets/dual_pw",
        },
        {
            "method": "password",
            "password": "inline",
            "password_env": "MRC_J2_DUAL",
        },
        {
            "method": "password",
            "password_path": "secrets/dual_pw",
            "password_env": "MRC_J2_DUAL",
        },
        # Alias that expands to password_path must also dual-fail with plain.
        {
            "method": "password",
            "password": "inline",
            "password_secret": "dual_pw",
        },
    ]
    for i, auth in enumerate(dual_cases):
        r = config_ops.run(
            "put_profile",
            name=f"dual{i}",
            transport="ssh",
            host="10.0.0.9",
            username="root",
            auth=auth,
            ssh={"known_hosts": "none"},
        )
        assert r.status == "error", f"case {i} should fail: {auth}"
        assert r.code == "PROFILE_INVALID"
        msg = r.fields.get("msg") or ""
        assert "multiple password sources" in msg, msg
        assert "choose exactly one" in msg, msg
        assert not (home / "profiles" / f"dual{i}.toml").is_file()


def test_put_profile_single_source_plain_path_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each single password source succeeds via put_profile + reload."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run(
        "put_secret", name="single_path_pw", content="path-material\n"
    ).is_ok()

    # Plain
    r_plain = config_ops.run(
        "put_profile",
        name="src-plain",
        transport="ssh",
        host="10.0.0.1",
        username="u",
        auth={"method": "password", "password": "plain-ok"},
        ssh={"known_hosts": "none"},
    )
    assert r_plain.is_ok(), r_plain.fields
    p = load_profile(home, "src-plain")
    assert p.auth is not None
    assert p.auth.password == "plain-ok"
    assert p.auth.has_inline_password is True
    assert p.auth.password_path is None
    assert p.auth.password_env is None

    # Path
    r_path = config_ops.run(
        "put_profile",
        name="src-path",
        transport="ssh",
        host="10.0.0.2",
        username="u",
        auth={
            "method": "password",
            "password_path": "secrets/single_path_pw",
        },
        ssh={"known_hosts": "none"},
    )
    assert r_path.is_ok(), r_path.fields
    p = load_profile(home, "src-path")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    assert p.auth.password_path is not None
    assert p.auth.password_path.name == "single_path_pw"
    assert p.auth.password_env is None

    # Env
    r_env = config_ops.run(
        "put_profile",
        name="src-env",
        transport="ssh",
        host="10.0.0.3",
        username="u",
        auth={"method": "password", "password_env": "MRC_J2_SINGLE_ENV"},
        ssh={"known_hosts": "none"},
    )
    assert r_env.is_ok(), r_env.fields
    p = load_profile(home, "src-env")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    assert p.auth.password_path is None
    assert p.auth.password_env == "MRC_J2_SINGLE_ENV"


def test_put_profile_body_dual_password_sources_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """body= with password + password_path is rejected; no file left."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run("put_secret", name="bpw", content="x\n").is_ok()
    body = (
        'name = "bodydual"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[auth]\n"
        'method = "password"\n'
        'password = "inline"\n'
        'password_path = "secrets/bpw"\n'
    )
    r = config_ops.run("put_profile", name="bodydual", body=body)
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    msg = r.fields.get("msg") or ""
    assert "multiple password sources" in msg
    assert "choose exactly one" in msg
    assert not (home / "profiles" / "bodydual.toml").is_file()


def _assert_op_msg_no_home_abs(msg: str, home: Path) -> None:
    home_s = str(Path(home).resolve())
    assert home_s not in msg, f"home abs in op msg: {msg!r}"
    for prefix in ("/Users/", "/home/"):
        if home_s.startswith(prefix):
            assert prefix not in msg, f"{prefix!r} in op msg: {msg!r}"


def test_get_profile_not_found_msg_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_profile PROFILE_NOT_FOUND msg uses profiles/..., not abs home."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    r = config_ops.run("get_profile", name="nope")
    assert r.status == "error"
    assert r.code == "PROFILE_NOT_FOUND"
    msg = str(r.fields.get("msg") or "")
    assert "profiles/nope.toml" in msg
    _assert_op_msg_no_home_abs(msg, home)


def test_delete_profile_not_found_msg_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_profile PROFILE_NOT_FOUND msg is home-relative."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    r = config_ops.run("delete_profile", name="does_not_exist")
    assert r.status == "error"
    assert r.code == "PROFILE_NOT_FOUND"
    msg = str(r.fields.get("msg") or "")
    assert "profiles/does_not_exist.toml" in msg
    _assert_op_msg_no_home_abs(msg, home)


def test_get_profile_invalid_msg_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_profile on invalid TOML surfaces PROFILE_INVALID with relative loc."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    bad = home / "profiles" / "bad.toml"
    bad.write_text("name = [broken\n", encoding="utf-8")
    r = config_ops.run("get_profile", name="bad")
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    msg = str(r.fields.get("msg") or "")
    assert "profiles/bad.toml" in msg
    _assert_op_msg_no_home_abs(msg, home)


def test_put_profile_dual_sources_msg_no_home_abs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """put_profile dual-password error msg has no absolute home prefix."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    r = config_ops.run(
        "put_profile",
        name="dual",
        transport="ssh",
        host="h",
        username="u",
        auth={
            "method": "password",
            "password": "inline",
            "password_path": "secrets/p",
        },
    )
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    msg = str(r.fields.get("msg") or "")
    assert "multiple password sources" in msg or "choose exactly one" in msg
    _assert_op_msg_no_home_abs(msg, home)


def _put_local(name: str = "box") -> None:
    r = config_ops.run("put_profile", name=name, transport="local")
    assert r.is_ok(), r.fields


def _set_max_body_chars(home: Path, n: int) -> None:
    cfg = home / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    if "max_body_chars" in text:
        lines = []
        for line in text.splitlines(keepends=True):
            if line.startswith("max_body_chars"):
                lines.append(f"max_body_chars = {n}\n")
            else:
                lines.append(line)
        cfg.write_text("".join(lines), encoding="utf-8")
        return
    cfg.write_text(
        text.replace("[defaults]\n", f"[defaults]\nmax_body_chars = {n}\n"),
        encoding="utf-8",
    )


def _assert_notes_payload_absent(r: OpResult, payload: str) -> None:
    """Fail if notes content leaked into an OpResult that must not carry a body."""
    assert payload not in (r.body or "")
    assert payload not in repr(r.fields)
    assert payload not in r.render_text()


def test_notes_write_ok_on_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    content = "## dirs\n/opt/app\n"
    r = config_ops.run("notes", action="write", name="box", content=content)
    assert r.is_ok(), r.fields
    assert r.fields.get("bytes") == len(content.encode("utf-8"))
    assert r.fields.get("path") == "notes/box.md"
    assert (home / "notes" / "box.md").read_text(encoding="utf-8") == content
    _assert_notes_payload_absent(r, content)
    _assert_notes_payload_absent(r, "/opt/app")
    _assert_notes_payload_absent(r, "## dirs")


def test_notes_write_append_prepend_without_profile_not_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    for action in ("write", "append", "prepend"):
        r = config_ops.run(
            "notes", action=action, name="ghost", content="should-not-land"
        )
        assert r.status == "error", action
        assert r.code == "PROFILE_NOT_FOUND", r.fields
        assert not (home / "notes" / "ghost.md").exists()
        leftover = list((home / "notes").iterdir()) if (home / "notes").is_dir() else []
        assert leftover == [], leftover


def test_notes_read_returns_body_stat_has_bytes_no_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    text = "host layout /var/www"
    assert config_ops.run("notes", action="write", name="box", content=text).is_ok()

    rd = config_ops.run("notes", action="read", name="box")
    assert rd.is_ok(), rd.fields
    assert rd.body == text
    assert rd.fields.get("bytes") == len(text.encode("utf-8"))
    assert "truncated" not in rd.fields
    assert "truncated middle" not in (rd.body or "")

    st = config_ops.run("notes", action="stat", name="box")
    assert st.is_ok(), st.fields
    assert st.fields.get("bytes") == len(text.encode("utf-8"))
    assert st.body in (None, "")
    assert text not in (st.body or "")
    rendered = st.render_text()
    assert text not in rendered
    assert "bytes=" in rendered


def test_list_profiles_notes_flag_present_and_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("alpha")
    _put_local("beta")
    lp0 = config_ops.run("list_profiles")
    assert lp0.is_ok()
    body0 = lp0.body or ""
    assert "notes=1" not in body0
    assert "name=alpha" in body0
    assert "name=beta" in body0

    assert config_ops.run(
        "notes", action="write", name="alpha", content="keep-me"
    ).is_ok()
    lp1 = config_ops.run("list_profiles")
    body1 = lp1.body or ""
    alpha_line = next(ln for ln in body1.splitlines() if "name=alpha" in ln)
    beta_line = next(ln for ln in body1.splitlines() if "name=beta" in ln)
    assert "notes=1" in alpha_line
    assert "notes=1" not in beta_line
    _assert_notes_payload_absent(lp1, "keep-me")


def test_get_profile_notes_flag_without_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    marker = "UNIQUE-NOTES-BODY-DO-NOT-INLINE"
    assert config_ops.run(
        "notes", action="write", name="box", content=marker
    ).is_ok()

    gp = config_ops.run("get_profile", name="box")
    assert gp.is_ok(), gp.fields
    assert gp.fields.get("notes") == 1
    assert marker not in (gp.body or "")
    assert marker not in repr(gp.fields)
    assert "notes=1" in (gp.body or "")
    prof = gp.fields.get("profile") or {}
    assert "notes" not in prof or prof.get("notes") != marker


def test_delete_profile_removes_notes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    assert config_ops.run(
        "notes", action="write", name="box", content="cascade-me"
    ).is_ok()
    notes = home / "notes" / "box.md"
    assert notes.is_file()
    d = config_ops.run("delete_profile", name="box")
    assert d.is_ok(), d.fields
    assert not notes.exists()
    assert "box" not in list_profiles(home)


def test_delete_profile_removes_empty_notes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write \"\" leaves a zero-byte notes file; delete_profile must unlink it.

    A presence check that treats size==0 as absent would skip the unlink
    and leave the empty path on disk.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    empty = config_ops.run("notes", action="write", name="box", content="")
    assert empty.is_ok(), empty.fields
    notes = home / "notes" / "box.md"
    assert notes.is_file()
    assert notes.stat().st_size == 0
    d = config_ops.run("delete_profile", name="box")
    assert d.is_ok(), d.fields
    assert not notes.exists()
    assert "box" not in list_profiles(home)


def test_empty_write_clears_list_notes_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    assert config_ops.run("notes", action="write", name="box", content="x").is_ok()
    lp = config_ops.run("list_profiles")
    assert "notes=1" in (lp.body or "")
    empty = config_ops.run("notes", action="write", name="box", content="")
    assert empty.is_ok(), empty.fields
    assert (home / "notes" / "box.md").is_file()
    assert (home / "notes" / "box.md").stat().st_size == 0
    lp2 = config_ops.run("list_profiles")
    assert "notes=1" not in (lp2.body or "")
    gp = config_ops.run("get_profile", name="box")
    assert gp.fields.get("notes") != 1
    assert "notes=1" not in (gp.body or "")


def test_notes_read_stat_rm_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    for action in ("read", "stat", "rm"):
        r = config_ops.run("notes", action=action, name="box")
        assert r.status == "error", action
        assert r.code == "NOTES_NOT_FOUND", r.fields


def test_notes_append_prepend_and_rm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    frag_a = "UNIQUE-APPEND-A"
    frag_b = "UNIQUE-APPEND-B"
    frag_x = "UNIQUE-PREPEND-X"
    ra = config_ops.run("notes", action="append", name="box", content=frag_a)
    assert ra.is_ok(), ra.fields
    _assert_notes_payload_absent(ra, frag_a)
    rb = config_ops.run("notes", action="append", name="box", content=frag_b)
    assert rb.is_ok(), rb.fields
    _assert_notes_payload_absent(rb, frag_b)
    assert config_ops.run("notes", action="read", name="box").body == frag_a + frag_b
    rx = config_ops.run("notes", action="prepend", name="box", content=frag_x)
    assert rx.is_ok(), rx.fields
    _assert_notes_payload_absent(rx, frag_x)
    joined = frag_x + frag_a + frag_b
    assert config_ops.run("notes", action="read", name="box").body == joined
    empty_ap = config_ops.run("notes", action="append", name="box", content="")
    assert empty_ap.status == "error"
    assert empty_ap.code == "INVALID_ARG"
    assert config_ops.run("notes", action="read", name="box").body == joined
    rm = config_ops.run("notes", action="rm", name="box")
    assert rm.is_ok(), rm.fields
    assert not (home / "notes" / "box.md").exists()


def test_delete_profile_without_notes_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("plain")
    d = config_ops.run("delete_profile", name="plain")
    assert d.is_ok(), d.fields
    assert "plain" not in list_profiles(home)


@pytest.mark.parametrize(
    "action", ["write", "append", "prepend"], ids=["write", "append", "prepend"]
)
def test_notes_write_cannot_interleave_with_profile_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A profile delete must not land between the precondition and the write.

    The barrier pins the interleaving: the writer is held after it passed the
    profile-existence check, and the delete is allowed to run before the writer
    resumes. Without mutual exclusion the delete removes profile + notes and
    the writer then re-creates the notes, leaving notes with no profile - a
    state no serial order of the two calls can produce. A delete that holds the
    same per-name lock can only finish after the writer returns, so the wait
    below is bounded rather than a rendezvous.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    assert config_ops.run("notes", action="write", name="box", content="seed").is_ok()

    checked = threading.Event()  # writer is past the profile-existence check
    delete_finished = threading.Event()
    real_writer = getattr(config_ops, f"{action}_notes")

    def pausing_writer(h: Path, name: str, content: str) -> Path:
        checked.set()
        delete_finished.wait(2.0)
        return real_writer(h, name, content)

    monkeypatch.setattr(config_ops, f"{action}_notes", pausing_writer)

    results: dict[str, OpResult] = {}

    def delete_after_check() -> None:
        assert checked.wait(5.0), "writer never reached the notes mutation"
        results["delete"] = config_ops.run("delete_profile", name="box")
        delete_finished.set()

    deleter = threading.Thread(target=delete_after_check)
    deleter.start()
    results["write"] = config_ops.run(
        "notes", action=action, name="box", content="late-fragment"
    )
    deleter.join(10.0)
    assert not deleter.is_alive(), "delete_profile did not return"

    write = results["write"]
    delete = results["delete"]
    assert delete.is_ok(), delete.fields
    profile_on_disk = (home / "profiles" / "box.toml").is_file()
    notes_on_disk = (home / "notes" / "box.md").is_file()
    assert not profile_on_disk
    if write.is_ok():
        assert not notes_on_disk, (
            f"profile deleted and notes re-created: write={write.fields!r} "
            f"delete={delete.fields!r}"
        )
    else:
        # Legal serial order: the delete landed first, so the write's
        # precondition was already false and nothing reached disk.
        assert write.code == "PROFILE_NOT_FOUND", write.fields
        assert not notes_on_disk


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate writes only for a non-root POSIX user",
)
def test_delete_profile_notes_cleanup_failure_is_op_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable notes dir must not escape Core as a raw OSError.

    The profile itself was deleted, so the message says so, names the notes
    path relative to the config home, and never promises a rollback.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    assert config_ops.run(
        "notes", action="write", name="box", content="keep-me"
    ).is_ok()

    notes_dir = home / "notes"
    os.chmod(notes_dir, 0o500)
    try:
        r = config_ops.run("delete_profile", name="box")
    finally:
        os.chmod(notes_dir, 0o700)

    assert r.status == "error", r.fields
    assert r.code == "CONFIG_WRITE_FAILED", r.fields
    msg = str(r.fields.get("msg") or "")
    assert "notes/box.md" in msg
    assert "deleted" in msg.lower()
    assert "notes" in msg.lower()
    _assert_op_msg_no_home_abs(msg, home)
    # The profile delete succeeded; only the notes cleanup failed.
    assert not (home / "profiles" / "box.toml").exists()
    assert (home / "notes" / "box.md").is_file()


def test_delete_profile_invalid_name_stays_in_profile_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid delete_profile name is rejected in profile wording.

    The per-name lock is keyed by the notes path, so a name check that runs
    only inside lock resolution reports ``notes name ...`` for a
    ``delete_profile`` call; the caller must see the profile namespace.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()

    r = config_ops.run("delete_profile", name="BAD name")
    assert r.status == "error", r.fields
    assert r.code == "CONFIG_WRITE_FAILED", r.fields
    msg = str(r.fields.get("msg") or "")
    assert "profile name 'BAD name' must match" in msg, msg
    assert "notes name" not in msg, msg


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate unlink only for a non-root POSIX user",
)
def test_delete_profile_unlink_failure_is_op_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed profile unlink must not escape Core nor name the abs path.

    The unlink itself failed, so the profile is still on disk: the message
    must not claim it was deleted, and it carries the home-relative profile
    path plus the errno text only.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")

    profiles = home / "profiles"
    os.chmod(profiles, 0o500)
    try:
        r = config_ops.run("delete_profile", name="box")
    finally:
        os.chmod(profiles, 0o700)

    assert r.status == "error", r.fields
    assert r.code == "CONFIG_WRITE_FAILED", r.fields
    msg = str(r.fields.get("msg") or "")
    assert "profiles/box.toml" in msg, msg
    assert "Permission denied" in msg, msg
    assert "deleted" not in msg.lower(), msg
    _assert_op_msg_no_home_abs(msg, home)
    # The unlink failed, so the profile is still there.
    assert (home / "profiles" / "box.toml").is_file()


def test_notes_read_truncates_over_max_body_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-budget read keeps head+tail with a middle marker; disk is unchanged."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    head = "HEAD-KEEP-UNIQUE"
    tail = "TAIL-KEEP-UNIQUE"
    middle = "MIDDLE-DROP-UNIQUE" + ("\u4e2d" * 80)
    original = head + middle + tail
    assert config_ops.run(
        "notes", action="write", name="box", content=original
    ).is_ok()
    notes = home / "notes" / "box.md"
    assert notes.read_text(encoding="utf-8") == original

    rd0 = config_ops.run("notes", action="read", name="box")
    assert rd0.is_ok(), rd0.fields
    assert rd0.body == original
    assert "truncated" not in rd0.fields
    assert "truncated middle" not in (rd0.body or "")
    assert rd0.fields.get("bytes") == len(original.encode("utf-8"))

    _set_max_body_chars(home, 40)
    rd = config_ops.run("notes", action="read", name="box")
    assert rd.is_ok(), rd.fields
    body = rd.body or ""
    assert body != original
    assert len(body) < len(original)
    match = _NOTES_TRUNC_RE.search(body)
    assert match is not None, body
    kept_head = int(match.group(1))
    kept_tail = int(match.group(2))
    total = int(match.group(3))
    assert total == len(original)
    assert body.startswith(original[:kept_head])
    assert body.endswith(original[-kept_tail:] if kept_tail else "")
    assert "MIDDLE-DROP-UNIQUE" not in body
    assert rd.fields.get("truncated") == 1
    assert rd.fields.get("bytes") == len(original.encode("utf-8"))
    assert notes.read_text(encoding="utf-8") == original
    rendered = rd.render_text()
    assert match.group(0) in rendered
    assert "truncated" in rendered.split("\n", 1)[0]


def test_notes_write_append_too_large_ops_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ops-layer oversize write/append/prepend is NOTES_TOO_LARGE; disk stays."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    short = "keep"
    assert config_ops.run("notes", action="write", name="box", content=short).is_ok()
    _set_max_body_chars(home, 8)
    notes = home / "notes" / "box.md"
    oversize = "this-is-too-long"
    for action in ("write", "append", "prepend"):
        r = config_ops.run("notes", action=action, name="box", content=oversize)
        assert r.status == "error", action
        assert r.code == "NOTES_TOO_LARGE", r.fields
        msg = str(r.fields.get("msg") or "")
        assert "max_body_chars" in msg
        assert notes.read_text(encoding="utf-8") == short


def test_notes_pem_rejected_ops_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ops-layer PEM armor on write/append/prepend is error; disk stays."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    _put_local("box")
    keep = "keep-plain"
    assert config_ops.run("notes", action="write", name="box", content=keep).is_ok()
    notes = home / "notes" / "box.md"
    for action in ("write", "append", "prepend"):
        r = config_ops.run("notes", action=action, name="box", content=_PEM_NOTES)
        assert r.status == "error", action
        assert not r.is_ok()
        msg = str(r.fields.get("msg") or "")
        assert "PEM" in msg
        assert notes.read_text(encoding="utf-8") == keep

