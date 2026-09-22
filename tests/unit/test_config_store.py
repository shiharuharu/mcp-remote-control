"""config.store writes: atomic/fsync, body= pre-write reject, render, perms."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from mcp_remote_control.config import list_profiles, load_profile, store as store_mod
from mcp_remote_control.config.errors import (
    ConfigError,
    ConfigInvalid,
    ProfileInvalid,
    ProfileNotFound,
)
from mcp_remote_control.core import config_ops


def test_delete_profile_rejects_traversal_name(
    tmp_path: Path, monkeypatch
) -> None:
    """delete_profile must reject traversal names with ProfileInvalid and
    never delete files outside profiles/."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    sentinel = home / "config.toml"
    assert sentinel.is_file()
    for bad in ("../config", "..%2f", "a/../b", ".."):
        with pytest.raises(ProfileInvalid):
            store_mod.delete_profile(home, bad)
    # Sentinel outside profiles/ must still exist.
    assert sentinel.is_file()


def test_put_secret_restrictive_perms(tmp_path: Path, monkeypatch) -> None:
    """Secret file 0o600, secrets/ dir 0o700."""
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
    """A mid-write failure leaves the prior file intact + no temps."""
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

    monkeypatch.setattr(store_mod.os, "replace", boom)
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
    """A mid-write failure leaves the prior secret intact + no temps."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run("put_secret", name="pw", content="v1").is_ok()
    p = home / "secrets" / "pw"
    assert p.read_text() == "v1"

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    r = config_ops.run("put_secret", name="pw", content="v2")
    assert r.status == "error"
    assert p.read_text() == "v1"
    leftovers = [q for q in (home / "secrets").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_profile_atomic_write_fsync_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """Text path fsyncs temp fd before os.replace."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()

    order: list[str] = []
    real_fsync = store_mod.os.fsync
    real_replace = store_mod.os.replace

    def spy_fsync(fd: int) -> None:
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "fsync", spy_fsync)
    monkeypatch.setattr(store_mod.os, "replace", spy_replace)

    r = config_ops.run(
        "put_profile", name="fsync-text", transport="local", label="durable"
    )
    assert r.is_ok(), r.fields
    p = home / "profiles" / "fsync-text.toml"
    assert p.is_file()
    assert "durable" in p.read_text()
    assert "fsync" in order
    assert "replace" in order
    assert order.index("fsync") < order.index("replace")
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_secret_atomic_write_fsync_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """bytes_restricted path fsyncs before os.replace."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()

    order: list[str] = []
    real_fsync = store_mod.os.fsync
    real_replace = store_mod.os.replace

    def spy_fsync(fd: int) -> None:
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "fsync", spy_fsync)
    monkeypatch.setattr(store_mod.os, "replace", spy_replace)

    r = config_ops.run("put_secret", name="fsync-secret", content="secret-v1")
    assert r.is_ok(), r.fields
    p = home / "secrets" / "fsync-secret"
    assert p.read_text() == "secret-v1"
    assert "fsync" in order
    assert "replace" in order
    assert order.index("fsync") < order.index("replace")
    leftovers = [q for q in (home / "secrets").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_profile_fsync_failure_cleans_temp_and_preserves(
    tmp_path: Path, monkeypatch
) -> None:
    """fsync OSError on text path cleans temp; prior profile intact."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert (
        config_ops.run(
            "put_profile", name="atom-fs", transport="local", label="v1"
        ).is_ok()
    )
    p = home / "profiles" / "atom-fs.toml"
    original = p.read_text()

    def boom_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(store_mod.os, "fsync", boom_fsync)
    r = config_ops.run(
        "put_profile", name="atom-fs", transport="local", label="v2"
    )
    assert r.status == "error"
    assert p.read_text() == original
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_secret_fsync_failure_cleans_temp_and_preserves(
    tmp_path: Path, monkeypatch
) -> None:
    """fsync OSError on bytes_restricted path cleans temp; prior secret intact."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    assert config_ops.run("ensure_home").is_ok()
    assert config_ops.run("put_secret", name="pw-fs", content="v1").is_ok()
    p = home / "secrets" / "pw-fs"
    assert p.read_text() == "v1"

    def boom_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(store_mod.os, "fsync", boom_fsync)
    r = config_ops.run("put_secret", name="pw-fs", content="v2")
    assert r.status == "error"
    assert p.read_text() == "v1"
    leftovers = [q for q in (home / "secrets").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_render_profile_toml_keeps_password_strips_private_key() -> None:
    """auth.password is kept; private_key_pem is still stripped."""

    text = store_mod.render_profile_toml(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth={"method": "password", "password": "leak", "private_key_pem": "PEM"},
    )
    assert 'password = "leak"' in text  # plain password allowed
    assert "PEM" not in text
    assert 'method = "password"' in text
    assert "private_key_pem" not in text


def test_render_profile_toml_rejects_nested_auth_secret() -> None:
    """A sensitive key nested under an auth sub-table must be rejected
    (flat strip is insufficient - auth.credssp.client_secret must not persist)."""
    with pytest.raises(ProfileInvalid):
        store_mod.render_profile_toml(
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


def test_render_profile_toml_rejects_non_string_password() -> None:
    """Non-str password -> ProfileInvalid (not omit)."""
    for bad in (12345, True, False, ["x"], {"k": "v"}):
        with pytest.raises(ProfileInvalid) as ei:
            store_mod.render_profile_toml(
                name="x",
                transport="ssh",
                host="h",
                username="u",
                auth={"method": "password", "password": bad},
            )
        msg = str(ei.value)
        assert "password" in msg.lower()
        assert "string" in msg.lower()


def test_put_profile_body_non_dict_auth_with_secret_rejected_pre_write(
    tmp_path: Path, monkeypatch
) -> None:
    """A non-dict [auth] (string/list/...) is skipped by both
    ``_clean_auth_table`` (only runs for dicts) and ``_enforce_no_secret_bodies``
    (skips the "auth" key). Without a pre-write reject, ``auth="password=s3cr3t"``
    would be written verbatim and the secret would be at rest even though
    load_profile then raises ``[auth] must be a table``. Must be rejected
    before the write with no file persisted.
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
    """A non-dict [auth] must be rejected pre-write even when it carries no
    secret - the shape itself is invalid (load_profile would reject it
    post-write). The explicit ``[auth] must be a table`` reject fires before
    ``_atomic_write_text`` so no broken file is left on disk.
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
    """The non-dict [auth] reject must fire before ``_atomic_write_text`` is
    ever called - the secret never reaches disk even momentarily. A
    post-write rollback would still unlink the file, so ``not path.is_file()``
    could hold while the secret was briefly at rest. The spy asserts the
    write was never reached, pinning the stronger pre-write guarantee.
    """
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
    # The pre-write reject must fire before the write: the writer is never called.
    assert calls == []
    assert not (home / "profiles" / "lab.toml").is_file()


def test_put_profile_body_post_write_validation_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    """A body that passes the pre-write secret scan but fails load_profile
    must not replace an existing legal profile. After the rejected write,
    dest still loads the previous transport/label (same on-disk TOML).
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    first = config_ops.run(
        "put_profile", name="lab", transport="local", label="keep-me"
    )
    assert first.is_ok(), first.fields
    path = home / "profiles" / "lab.toml"
    prior = path.read_text(encoding="utf-8")
    assert 'transport = "local"' in prior
    assert 'label = "keep-me"' in prior

    # Body parses, name matches, no secrets - pre-write checks all pass;
    # invalid transport only surfaces in the post-write load_profile trip.
    r = config_ops.run(
        "put_profile",
        name="lab",
        body='name = "lab"\ntransport = "bogus"\nlabel = "gone"\n',
    )
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    assert "transport must be one of" in (r.fields.get("msg") or "")
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == prior
    kept = load_profile(home, "lab")
    assert kept.transport == "local"
    assert kept.label == "keep-me"
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []

    # Illegal auth method is the same class of post-write reject.
    r2 = config_ops.run(
        "put_profile",
        name="lab",
        body=(
            'name = "lab"\n'
            'transport = "local"\n'
            'label = "gone"\n'
            "[auth]\n"
            'method = "not-a-method"\n'
        ),
    )
    assert r2.status == "error"
    assert r2.code == "PROFILE_INVALID"
    assert path.read_text(encoding="utf-8") == prior
    kept2 = load_profile(home, "lab")
    assert kept2.transport == "local"
    assert kept2.label == "keep-me"


def test_put_profile_first_write_validation_failure_leaves_no_file(
    tmp_path: Path, monkeypatch
) -> None:
    """First write of a body that fails load_profile must not leave dest."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    body = 'name = "lab"\ntransport = "bogus"\n'
    r = config_ops.run("put_profile", name="lab", body=body)
    assert r.status == "error"
    assert r.code == "PROFILE_INVALID"
    assert "transport must be one of" in (r.fields.get("msg") or "")
    assert not (home / "profiles" / "lab.toml").is_file()
    assert "lab" not in list_profiles(home)
    leftovers = [q for q in (home / "profiles").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_put_secret_rejects_trailing_newline_name(
    tmp_path: Path, monkeypatch
) -> None:
    """fullmatch rejects a trailing newline in the secret name.

    The config_ops wrapper ``str(name).strip()``s before calling put_secret,
    so this must be tested at the store level (direct library callers)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    with pytest.raises(ConfigInvalid):
        store_mod.put_secret(home, name="pw\n", content="x")
    assert not (home / "secrets" / "pw\n").is_file()


def test_delete_profile_missing_raises_profile_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    """delete_profile raises ProfileNotFound (a ConfigError) for a
    missing file so direct library callers using ``except ConfigError`` catch."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    with pytest.raises(ProfileNotFound) as ei:
        store_mod.delete_profile(home, "nope")
    assert isinstance(ei.value, ConfigError)


def test_toml_table_quotes_unsafe_keys() -> None:
    """Keys with spaces/dots are quoted so the render is valid TOML."""
    text = store_mod._toml_table("winrm", {"my sub": {"a.b": "v"}})
    # Round-trips through tomllib as nested tables with the literal keys.
    parsed = tomllib.loads(text)
    assert parsed["winrm"]["my sub"]["a.b"] == "v"


def test_put_profile_strict_perms_tightens_profile_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    """strict_perms=true -> put_profile chmods profiles/ to 0o700 (the
    observable strictness; profiles/ is 0o755 by default) and the profile file
    to 0o600 (defensive guarantee)."""
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
    # Observable wire: profiles/ tightened from 0o755 -> 0o700.
    assert (os.stat(profiles).st_mode & 0o777) == 0o700
    # Defensive guarantee: profile file is 0o600.
    assert (os.stat(profile_file).st_mode & 0o777) == 0o600
    # secrets/ is 0o700 regardless of strict_perms.
    assert (os.stat(home / "secrets").st_mode & 0o777) == 0o700

    # A second put_profile keeps profiles/ at 0o700 (idempotent).
    config_ops.run("put_profile", name="strict2", transport="local")
    assert (os.stat(profiles).st_mode & 0o777) == 0o700


def test_put_profile_default_no_strict_perms_keeps_profile_file_0o600(
    tmp_path: Path, monkeypatch
) -> None:
    """Without strict_perms, put_profile still works and the profile file is
    0o600 - the mkstemp baseline, not the strict_perms wire. The
    profiles/ dir is left at the mkdir default (not tightened to 0o700)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # Default ensure_home config.toml has strict_perms = false.
    r = config_ops.run("put_profile", name="loose", transport="local")
    assert r.is_ok(), r.fields
    profile_file = home / "profiles" / "loose.toml"
    # Profile file is 0o600 via mkstemp (independent of strict_perms).
    assert (os.stat(profile_file).st_mode & 0o777) == 0o600
    # Round-trip still works (no regression from reading the global config in
    # the put_profile path).
    p = load_profile(home, "loose")
    assert p.transport == "local"


def test_put_profile_strict_perms_broken_config_surfaces_before_write(
    tmp_path: Path, monkeypatch
) -> None:
    """strict_perms is read before the atomic write, so a broken config.toml
    surfaces as an error with no profile file persisted (no half-write)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    # Corrupt config.toml so load_config raises ConfigInvalid.
    (home / "config.toml").write_text("[[[not valid toml", encoding="utf-8")
    r = config_ops.run("put_profile", name="nope", transport="local")
    assert r.status == "error"
    # No profile file written (the read happens before _atomic_write_text).
    assert not (home / "profiles" / "nope.toml").is_file()


_PEM_CERT = (
    "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
)
_PEM_KEY = "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n"


@pytest.mark.parametrize(
    "field,body",
    [
        ("cert_path", _PEM_CERT),
        ("certificate_pem", _PEM_CERT),
        ("cert_key_path", _PEM_KEY),
        ("certificate_key_pem", _PEM_KEY),
    ],
)
def test_put_profile_rejects_pem_body_as_cert_path(
    tmp_path: Path, field: str, body: str
) -> None:
    """PEM armor in a cert/key path field is rejected and leaves no profile."""
    home = tmp_path / "mrc"
    store_mod.ensure_home_layout(home)
    with pytest.raises(ProfileInvalid) as ei:
        store_mod.put_profile(
            home,
            name="win-pem",
            transport="winrm",
            host="h",
            username="u",
            auth={"method": "certificate", field: body},
            winrm={"scheme": "https"},
        )
    msg = str(ei.value)
    assert field in msg
    assert "PEM" in msg or "path" in msg.lower()
    assert not (home / "profiles" / "win-pem.toml").is_file()
    assert "win-pem" not in list_profiles(home)


def test_put_profile_accepts_real_cert_file_path(tmp_path: Path) -> None:
    """A real existing cert/key file path is still accepted by put_profile."""
    home = tmp_path / "mrc"
    store_mod.ensure_home_layout(home)
    (home / "secrets" / "client.pem").write_text(_PEM_CERT, encoding="utf-8")
    (home / "secrets" / "client-key.pem").write_text(_PEM_KEY, encoding="utf-8")
    path = store_mod.put_profile(
        home,
        name="win-cert",
        transport="winrm",
        host="h",
        username="u",
        auth={
            "method": "certificate",
            "cert_path": "secrets/client.pem",
            "cert_key_path": "secrets/client-key.pem",
        },
        winrm={"scheme": "https"},
    )
    assert path.is_file()
    profile = load_profile(home, "win-cert")
    assert profile.auth is not None
    assert profile.auth.cert_path is not None
    assert profile.auth.cert_path.is_file()
    assert profile.auth.cert_path.name == "client.pem"
    assert profile.auth.cert_key_path is not None
    assert profile.auth.cert_key_path.is_file()

