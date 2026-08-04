"""Agent self-config: ensure_home / put_profile / put_secret."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_remote_control.config import list_profiles, load_profile
from mcp_remote_control.config.errors import ProfileInvalid
from mcp_remote_control.config.store import delete_profile, put_secret
from mcp_remote_control.core import config_ops, endpoint_ops
from mcp_remote_control.endpoint import reset_registry


def test_ensure_home_and_put_local_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    r = config_ops.run("ensure_home")
    assert r.is_ok()
    assert (tmp_path / "mrc" / "profiles").is_dir()
    assert (tmp_path / "mrc" / "config.toml").is_file()

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
    """Agent bootstrap path: config write → endpoint open (no hand-edited TOML)."""
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


# ---------------------------------------------------------------------------
# O1 hardening: path traversal, secret persistence, atomic writes, nested
# tables, body= name handling.
# ---------------------------------------------------------------------------


def test_delete_profile_rejects_traversal_name(
    tmp_path: Path, monkeypatch
) -> None:
    """H8: delete_profile must reject traversal names with ProfileInvalid and
    never delete files outside profiles/."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    sentinel = home / "config.toml"
    assert sentinel.is_file()
    for bad in ("../config", "..%2f", "a/../b", ".."):
        with pytest.raises(ProfileInvalid):
            delete_profile(home, bad)
    # Sentinel outside profiles/ must still exist.
    assert sentinel.is_file()


def test_put_profile_rejects_secrets_in_non_auth_sections(
    tmp_path: Path, monkeypatch
) -> None:
    """H8/secret-persist: ssh=/winrm= must not persist secret bodies."""
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
    """M2/F2: non-auth ``private_key_pem`` must be rejected (not only password)."""
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


def test_put_secret_restrictive_perms(tmp_path: Path, monkeypatch) -> None:
    """H8/perms: secret file 0o600, secrets/ dir 0o700."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run("put_secret", name="pw", content="hunter2")
    assert r.is_ok(), r.fields
    p = home / "secrets" / "pw"
    assert p.is_file()
    assert (os.stat(p).st_mode & 0o777) == 0o600
    assert (os.stat(home / "secrets").st_mode & 0o777) == 0o700
    # And a second put_secret still leaves dir at 0o700.
    config_ops.run("put_secret", name="pw2", content="x")
    assert (os.stat(home / "secrets").st_mode & 0o777) == 0o700


def test_put_profile_atomic_write(tmp_path: Path, monkeypatch) -> None:
    """Theme D: a mid-write failure leaves the prior file intact + no temps."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert (
        config_ops.run(
            "put_profile", name="atom", transport="local", label="v1"
        ).is_ok()
    )
    p = home / "profiles" / "atom.toml"
    original = p.read_text()
    assert "v1" in original

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(os, "replace", boom)
    r = config_ops.run(
        "put_profile", name="atom", transport="local", label="v2"
    )
    assert r.status == "error"
    # Original content untouched.
    assert p.read_text() == original
    # No temp files left behind in profiles/.
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_secret_atomic_write(tmp_path: Path, monkeypatch) -> None:
    """Theme D: a mid-write failure leaves the prior secret intact + no temps."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run("put_secret", name="pw", content="v1").is_ok()
    p = home / "secrets" / "pw"
    assert p.read_text() == "v1"

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(os, "replace", boom)
    r = config_ops.run("put_secret", name="pw", content="v2")
    assert r.status == "error"
    assert p.read_text() == "v1"
    leftovers = [q for q in (home / "secrets").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_profile_body_missing_name_prepended(tmp_path: Path, monkeypatch) -> None:
    """G: body whose value contains the substring 'name' (e.g. username="name")
    but has NO top-level name key must still get a real name= prepended.

    The acceptance's literal ``transport=local`` is unquoted and thus not valid
    TOML, so we use the quoted equivalent that still triggers the original
    bare-substring bug (``"name" in text`` was True via ``username = "name"``).
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
    """G: an unparseable body is rejected with a clear ProfileInvalid (no crash,
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
    """G: body with a matching name key is kept verbatim (no duplicate)."""
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
    # No duplicate name key (would be invalid TOML → load would have failed).
    text = (home / "profiles" / "box.toml").read_text()
    assert text.count("name =") == 1


def test_put_profile_body_wrong_name_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """G: body with a mismatched name is rejected with ProfileInvalid."""
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
    """G: nested dict under winrm renders as [winrm.credssp] and round-trips."""
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


def test_render_profile_toml_strips_auth_inline_secrets() -> None:
    """auth inline secret bodies are stripped (tolerated-discouraged path)."""
    from mcp_remote_control.config.store import render_profile_toml

    text = render_profile_toml(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth={"method": "password", "password": "leak", "private_key_pem": "PEM"},
    )
    assert "leak" not in text
    assert "PEM" not in text
    assert 'method = "password"' in text
    assert "password =" not in text  # the inline body field is gone
    assert "private_key_pem" not in text


def test_render_profile_toml_rejects_nested_auth_secret() -> None:
    """MED: a sensitive key nested under an auth sub-table must be rejected
    (flat strip was insufficient — auth.credssp.client_secret persisted)."""
    from mcp_remote_control.config.errors import ProfileInvalid as PI
    from mcp_remote_control.config.store import render_profile_toml

    with pytest.raises(PI):
        render_profile_toml(
            name="w",
            transport="winrm",
            host="h",
            username="u",
            auth={
                "method": "credssp",
                "password_path": "secrets/p",
                "credssp": {"client_secret": "TOPSECRET"},
            },
        )


def test_put_profile_body_with_ssh_secret_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """HIGH: body= with a secret in a non-auth section must be rejected and
    NO file persisted (previously body= bypassed render_profile_toml)."""
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


def test_put_profile_body_non_dict_auth_with_secret_rejected_pre_write(
    tmp_path: Path, monkeypatch
) -> None:
    """MED re-review: a non-dict [auth] (string/list/...) is silently skipped by
    both ``_clean_auth_table`` (only runs for dicts) AND ``_enforce_no_secret_bodies``
    (skips the "auth" key). Without a pre-write reject, ``auth="password=s3cr3t"``
    is written verbatim and the secret is at rest even though load_profile then
    raises ``[auth] must be a table``. Must be rejected BEFORE the write with NO
    file persisted.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="lab",
        body='transport = "local"\nauth = "password=s3cr3t"\n',
    )
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    assert "[auth] must be a table" in (r.fields.get("msg") or "")
    # No file persisted (pre-write reject).
    assert not (home / "profiles" / "lab.toml").is_file()
    assert "lab" not in list_profiles(home)


def test_put_profile_body_non_dict_auth_no_secret_rejected_pre_write(
    tmp_path: Path, monkeypatch
) -> None:
    """MED re-review: a non-dict [auth] must be rejected pre-write even when it
    carries NO secret — the shape itself is invalid (load_profile would reject
    it post-write). Asserts the explicit ``[auth] must be a table`` reject fires
    BEFORE ``_atomic_write_text`` so no broken file is left on disk.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    r = config_ops.run(
        "put_profile",
        name="lab",
        body='transport = "local"\nauth = "not_a_secret"\n',
    )
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    assert "[auth] must be a table" in (r.fields.get("msg") or "")
    # No file persisted.
    assert not (home / "profiles" / "lab.toml").is_file()
    assert "lab" not in list_profiles(home)


def test_put_profile_body_non_dict_auth_rejected_before_atomic_write(
    tmp_path: Path, monkeypatch
) -> None:
    """MED re-review (Fix 1 pin): the non-dict [auth] reject must fire BEFORE
    ``_atomic_write_text`` is ever called — the secret never reaches disk even
    momentarily. Without this, Fix 2 (post-write rollback) alone would still
    unlink the file, so the observable ``not path.is_file()`` would hold while
    the secret was briefly at rest. The spy asserts the write was never
    reached, pinning the stronger pre-write guarantee.
    """
    from mcp_remote_control.config import store as store_mod

    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")

    calls: list[tuple[Path, Path, str]] = []
    real_write = store_mod._atomic_write_text

    def spy(dir_path: Path, target: Path, text: str) -> None:
        calls.append((dir_path, target, text))
        return real_write(dir_path, target, text)

    monkeypatch.setattr(store_mod, "_atomic_write_text", spy)
    r = config_ops.run(
        "put_profile",
        name="lab",
        body='transport = "local"\nauth = "password=s3cr3t"\n',
    )
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    # Fix 1: the pre-write reject MUST fire before the write — never called.
    assert calls == []
    assert not (home / "profiles" / "lab.toml").is_file()


def test_put_profile_body_post_write_validation_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    """MED re-review: a body that PASSES the pre-write secret scan but FAILS
    ``load_profile`` validation must not leave a broken profile on disk. The
    atomic write only guarantees the prior file is intact on a mid-write
    failure; this asserts the post-write validation rollback unlinks the new
    file. ``transport = "bogus"`` passes the body= secret scan (no sensitive
    keys) but load_profile rejects it ("transport must be one of …").
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # Body parses, name matches, no secrets — pre-write checks all pass; the
    # invalid transport only surfaces in the post-write load_profile round-trip.
    body = 'name = "lab"\ntransport = "bogus"\n'
    r = config_ops.run("put_profile", name="lab", body=body)
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    assert "transport must be one of" in (r.fields.get("msg") or "")
    # Rollback: the just-written file must be removed.
    assert not (home / "profiles" / "lab.toml").is_file()
    assert "lab" not in list_profiles(home)
    # No temp files left behind either.
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_profile_body_clean_roundtrips(tmp_path: Path, monkeypatch) -> None:
    """HIGH positive: a body= with no secrets still works (round-trips)."""
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


def test_put_profile_body_auth_inline_secret_stripped(
    tmp_path: Path, monkeypatch
) -> None:
    """HIGH: body= with an inline [auth] password has it stripped (parity with
    the structured path); the profile still round-trips and the secret is not
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
    assert "INLINE_LEAK" not in on_disk
    assert "password =" not in on_disk  # inline body removed
    p = load_profile(home, "stripped")
    assert p.auth is not None
    assert p.auth.method == "password"


def test_put_secret_rejects_trailing_newline_name(
    tmp_path: Path, monkeypatch
) -> None:
    """LOW 1: fullmatch rejects a trailing newline in the secret name.

    The config_ops wrapper ``str(name).strip()``s before calling put_secret,
    so this must be tested at the store level (direct library callers)."""
    from mcp_remote_control.config.errors import ConfigInvalid

    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    with pytest.raises(ConfigInvalid):
        put_secret(home, name="pw\n", content="x")
    assert not (home / "secrets" / "pw\n").is_file()


def test_delete_profile_missing_raises_profile_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    """LOW 2: delete_profile raises ProfileNotFound (a ConfigError) for a
    missing file so direct library callers using ``except ConfigError`` catch."""
    from mcp_remote_control.config.errors import ConfigError, ProfileNotFound

    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    with pytest.raises(ProfileNotFound) as ei:
        delete_profile(home, "nope")
    assert isinstance(ei.value, ConfigError)


def test_toml_table_quotes_unsafe_keys() -> None:
    """LOW 3: keys with spaces/dots are quoted so the render is valid TOML."""
    import tomllib

    from mcp_remote_control.config.store import _toml_table

    text = _toml_table("winrm", {"my sub": {"a.b": "v"}})
    # Round-trips through tomllib as nested tables with the literal keys.
    parsed = tomllib.loads(text)
    assert parsed["winrm"]["my sub"]["a.b"] == "v"


# ---------------------------------------------------------------------------
# C5: strict_perms wire — when [security].strict_perms=true, put_profile
# tightens profile artifacts (profile file → 0o600, profiles/ dir → 0o700).
# O1 still enforces 0o700 secrets + 0o600 secret files unconditionally.
# ---------------------------------------------------------------------------


def test_put_profile_strict_perms_tightens_profile_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    """C5: strict_perms=true → put_profile chmods profiles/ to 0o700 (the
    observable strictness; profiles/ is 0o755 by default) and the profile file
    to 0o600 (defensive guarantee). The flag is no longer a dead no-op."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # ensure_home_layout creates config.toml with a [security] table; add
    # strict_perms = true to it (must be in the existing table, not a new one).
    cfg = home / "config.toml"
    txt = cfg.read_text(encoding="utf-8")
    assert "[security]" in txt
    txt = txt.replace(
        "strict_perms = false\n",
        "strict_perms = true\n",
    )
    cfg.write_text(txt, encoding="utf-8")

    r = config_ops.run("put_profile", name="strict", transport="local")
    assert r.is_ok(), r.fields
    profiles = home / "profiles"
    profile_file = profiles / "strict.toml"
    # Observable wire: profiles/ tightened from 0o755 → 0o700.
    assert (os.stat(profiles).st_mode & 0o777) == 0o700
    # Defensive guarantee: profile file is 0o600.
    assert (os.stat(profile_file).st_mode & 0o777) == 0o600
    # O1 baseline still holds: secrets/ is 0o700 regardless of strict_perms.
    assert (os.stat(home / "secrets").st_mode & 0o777) == 0o700

    # A second put_profile keeps profiles/ at 0o700 (idempotent).
    config_ops.run("put_profile", name="strict2", transport="local")
    assert (os.stat(profiles).st_mode & 0o777) == 0o700


def test_put_profile_default_no_strict_perms_keeps_profile_file_0o600(
    tmp_path: Path, monkeypatch
) -> None:
    """C5: without strict_perms, put_profile still works and the profile file is
    0o600 — that is the O1 mkstemp baseline, NOT the strict_perms wire. The
    profiles/ dir is left at the mkdir default (not tightened to 0o700)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # Default ensure_home config.toml has strict_perms = false.
    r = config_ops.run("put_profile", name="loose", transport="local")
    assert r.is_ok(), r.fields
    profile_file = home / "profiles" / "loose.toml"
    # O1 baseline: profile file is 0o600 via mkstemp (independent of strict_perms).
    assert (os.stat(profile_file).st_mode & 0o777) == 0o600
    # Round-trip still works (no regression from reading the global config in
    # the put_profile path).
    p = load_profile(home, "loose")
    assert p.transport == "local"


def test_put_profile_strict_perms_broken_config_surfaces_before_write(
    tmp_path: Path, monkeypatch
) -> None:
    """C5: strict_perms is read BEFORE the atomic write, so a broken config.toml
    surfaces as an error with NO profile file persisted (no half-write)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # Corrupt config.toml so load_config raises ConfigInvalid.
    (home / "config.toml").write_text("[[[not valid toml", encoding="utf-8")
    r = config_ops.run("put_profile", name="nope", transport="local")
    assert r.status == "error"
    # No profile file written (the read happens before _atomic_write_text).
    assert not (home / "profiles" / "nope.toml").is_file()
