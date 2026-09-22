"""Auth path fields: verbatim round-trip, PEM rejection, ``..`` containment.

Covers the config-surface defects around ``[auth]`` path values:

- a caller-rooted path (``/abs``, ``~/...``, ``$VAR/...``) is stored verbatim -
  ``_normalize_auth`` must not destroy the absoluteness signal before testing
  it, and must not re-root the value under ``secrets/``;
- a PEM *body* offered as a path field is rejected before any rewriting, in
  every wrapper it can arrive under (``secrets/`` shorthand, BOM, leading
  line), so key material is never persisted into ``profiles/<name>.toml``
  nor echoed back by ``get_profile``;
- a *relative* path with a ``..`` component - including one produced by
  ``$VAR`` expansion - is rejected on write *and* on load, while an absolute
  path is exempt (it never resolves through the home);
- a secret-id alias that the canonical field overrides is discarded, so it
  must not be the thing that fails the call.

Legitimate relative shorthand (``secrets/lab_pass``, bare ``lab_pass``) keeps
working; see the round-trip assertions below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import ProfileInvalid, load_profile, profiles_dir
from mcp_remote_control.core import config_ops

_PEM_BODY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

_SSH = dict(transport="ssh", host="lab", username="deploy")


def _put(home: Path, name: str, **auth: object):
    return config_ops.run("put_profile", name=name, auth=dict(auth), **_SSH)


def _profile_toml(home: Path, name: str) -> str:
    return (profiles_dir(home) / f"{name}.toml").read_text()


def _auth_value(home: Path, name: str, key: str) -> str:
    for line in _profile_toml(home, name).splitlines():
        if line.startswith(f"{key} = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError(f"{key} not found in profiles/{name}.toml")


# --- absolute / rooted paths are kept as given -------------------------------


def test_absolute_key_path_round_trips_unchanged(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "abs", method="private_key_path", key_path="/home/me/.ssh/id_rsa")
    assert r.is_ok(), r.fields
    # The caller's absolute path is exactly what lands in the profile file.
    assert _auth_value(home, "abs", "key_path") == "/home/me/.ssh/id_rsa"

    # resolve_under_home realpaths what it is given and macOS maps /home into
    # /System/Volumes/Data/home, so the read side compares against the realpath.
    real = Path("/home/me/.ssh/id_rsa").resolve()
    assert load_profile(home, "abs").auth.key_path == real
    got = config_ops.run("get_profile", name="abs")
    assert got.is_ok(), got.fields
    assert got.fields["profile"]["auth"]["key_path"] == str(real)


def test_absolute_path_outside_home_is_kept_and_usable(
    tmp_path: Path, monkeypatch
) -> None:
    """An absolute key outside the home is an intended capability - kept as-is."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    key = outside / "id_rsa"
    key.write_text("FAKEKEY\n")

    r = _put(home, "out", method="private_key_path", key_path=str(key))
    assert r.is_ok(), r.fields
    assert _auth_value(home, "out", "key_path") == str(key)
    assert load_profile(home, "out").auth.key_path == key.resolve()
    got = config_ops.run("get_profile", name="out")
    assert got.fields["profile"]["auth"]["key_path"] == str(key)
    assert f"auth.key_path={key}" in (got.body or "")


def test_tilde_path_is_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "tilde", method="private_key_path", key_path="~/.ssh/id_rsa")
    assert r.is_ok(), r.fields
    assert _auth_value(home, "tilde", "key_path") == "~/.ssh/id_rsa"


def test_env_var_path_is_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    monkeypatch.setenv("MRC_TEST_KEY_DIR", "/opt/keys")

    r = _put(
        home, "env", method="private_key_path", key_path="$MRC_TEST_KEY_DIR/id_rsa"
    )
    assert r.is_ok(), r.fields
    assert _auth_value(home, "env", "key_path") == "$MRC_TEST_KEY_DIR/id_rsa"


def test_all_path_fields_keep_absolute_values(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(
        home,
        "fields",
        method="private_key_path",
        key_path="/opt/keys/id",
        passphrase_path="/opt/keys/pp",
        password_path="/opt/keys/pw",
    )
    assert r.is_ok(), r.fields
    assert _auth_value(home, "fields", "key_path") == "/opt/keys/id"
    assert _auth_value(home, "fields", "passphrase_path") == "/opt/keys/pp"
    assert _auth_value(home, "fields", "password_path") == "/opt/keys/pw"


# --- relative shorthand still expands ---------------------------------------


def test_bare_secret_id_still_expands_under_secrets(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "rel", method="private_key_path", key_path="lab_key")
    assert r.is_ok(), r.fields
    assert _auth_value(home, "rel", "key_path") == "secrets/lab_key"

    # An explicit secrets/ reference is idempotent.
    r2 = _put(home, "rel2", method="private_key_path", key_path="secrets/lab_key")
    assert r2.is_ok(), r2.fields
    assert _auth_value(home, "rel2", "key_path") == "secrets/lab_key"

    p = load_profile(home, "rel")
    assert p.auth.key_path == home / "secrets" / "lab_key"


def test_alias_secret_id_expands_but_rooted_value_is_kept(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "alias", password_secret="lab_pass")
    assert r.is_ok(), r.fields
    assert _auth_value(home, "alias", "password_path") == "secrets/lab_pass"

    r2 = _put(home, "alias_abs", password_secret="/opt/keys/pw")
    assert r2.is_ok(), r2.fields
    assert _auth_value(home, "alias_abs", "password_path") == "/opt/keys/pw"


# --- PEM bodies are never persisted as a path -------------------------------


@pytest.mark.parametrize("key", ["key_path", "password_path", "passphrase_path"])
def test_pem_body_in_path_field_is_rejected(
    tmp_path: Path, monkeypatch, key: str
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    methods = {
        "key_path": "private_key_path",
        "passphrase_path": "private_key_path",
        "password_path": "password",
    }

    r = config_ops.run(
        "put_profile",
        name="pem",
        auth={"method": methods[key], key: _PEM_BODY},
        **_SSH,
    )
    assert r.status == "error", r.fields
    assert r.code == "PROFILE_INVALID"
    msg = str(r.fields.get("msg") or "")
    assert "PEM body" in msg
    assert "put_secret" in msg
    # Nothing persisted: the body never reached profiles/<name>.toml.
    assert not (profiles_dir(home) / "pem.toml").exists()


def test_pem_body_in_secret_alias_is_rejected(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "pem_alias", key_secret=_PEM_BODY)
    assert r.status == "error", r.fields
    assert "put_secret" in str(r.fields.get("msg") or "")
    assert not (profiles_dir(home) / "pem_alias.toml").exists()


# A body can arrive wrapped - behind the module's own ``secrets/`` shorthand,
# a UTF-8 BOM, or a stray leading line. The wrapper is exactly what a
# prefix-only armor test misses, and the body is then stored as a "path" and
# echoed by get_profile. Cover every wrapper on every path field.
_PEM_WRAPPERS = {
    "secrets_prefix": "secrets/" + _PEM_BODY,
    "bom": "\ufeff" + _PEM_BODY,
    "leading_line": "host-key\n" + _PEM_BODY,
    # Both wrappers at once: the shorthand and the BOM can nest either way.
    "secrets_prefix_bom": "secrets/" + "\ufeff" + _PEM_BODY,
}


@pytest.mark.parametrize("key", ["key_path", "password_path", "passphrase_path"])
@pytest.mark.parametrize("wrapper", sorted(_PEM_WRAPPERS))
def test_wrapped_pem_body_is_rejected(
    tmp_path: Path, monkeypatch, key: str, wrapper: str
) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    methods = {
        "key_path": "private_key_path",
        "passphrase_path": "private_key_path",
        "password_path": "password",
    }

    r = config_ops.run(
        "put_profile",
        name="wrapped",
        auth={"method": methods[key], key: _PEM_WRAPPERS[wrapper]},
        **_SSH,
    )
    assert r.status == "error", (wrapper, key, r.fields)
    assert r.code == "PROFILE_INVALID"
    assert "PEM body" in str(r.fields.get("msg") or "")
    assert not (profiles_dir(home) / "wrapped.toml").exists()


def test_wrapped_pem_body_in_handwritten_profile_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """Containment holds on the read path too: store is not the only writer."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    (profiles_dir(home) / "raw_pem.toml").write_text(
        'name = "raw_pem"\n'
        'transport = "ssh"\n'
        'host = "lab"\n'
        'username = "deploy"\n'
        "\n[auth]\n"
        'method = "private_key_path"\n'
        'key_path = "secrets/-----BEGIN OPENSSH PRIVATE KEY-----\\n'
        'b3BlbnNzaC1rZXk\\n-----END OPENSSH PRIVATE KEY-----"\n'
    )
    with pytest.raises(ProfileInvalid, match="PEM body"):
        load_profile(home, "raw_pem")


def test_secret_alias_overridden_by_canonical_field_is_not_validated(
    tmp_path: Path, monkeypatch
) -> None:
    """An alias the canonical field discards never lands in the profile.

    Validating it would reject input whose value is thrown away - the call
    used to succeed, and the alias is still consumed (not persisted).
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "alias_lost", key_secret="../x", key_path="/opt/keys/k")
    assert r.is_ok(), r.fields
    assert _auth_value(home, "alias_lost", "key_path") == "/opt/keys/k"
    assert "key_secret" not in _profile_toml(home, "alias_lost")

    r2 = _put(home, "alias_lost_pw", password_secret="../x", password_path="/opt/pw")
    assert r2.is_ok(), r2.fields
    assert _auth_value(home, "alias_lost_pw", "password_path") == "/opt/pw"
    assert "password_secret" not in _profile_toml(home, "alias_lost_pw")

    # The alias is only skipped when it is discarded: on its own it still fails.
    r3 = _put(home, "alias_used", key_secret="../x")
    assert r3.status == "error", r3.fields


# --- '..' traversal is rejected on write and on load ------------------------


def test_dotdot_path_is_rejected_before_write(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(
        home,
        "esc",
        method="password",
        password_path="secrets/../../outside/canary.txt",
    )
    assert r.status == "error", r.fields
    assert r.code == "PROFILE_INVALID"
    assert ".." in str(r.fields.get("msg") or "")
    assert not (profiles_dir(home) / "esc.toml").exists()


def test_dotdot_absolute_path_is_kept(tmp_path: Path, monkeypatch) -> None:
    """An absolute path is exempt from containment - it never resolves via home.

    ``resolve_under_home`` keeps absolute paths verbatim, so a ``..`` in one
    cannot reach anywhere the caller could not already name outright. Rejecting
    it would make an existing profile unloadable over its spelling alone.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    r = _put(home, "esc_abs", method="private_key_path", key_path="/opt/keys/../id")
    assert r.is_ok(), r.fields
    assert _auth_value(home, "esc_abs", "key_path") == "/opt/keys/../id"
    assert load_profile(home, "esc_abs").auth.key_path == Path("/opt/keys/../id").resolve()


def test_dotdot_under_home_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """A relative path that climbs is still rejected, in any path field."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))

    for field, method in (
        ("key_path", "private_key_path"),
        ("password_path", "password"),
        ("passphrase_path", "private_key_path"),
    ):
        r = _put(home, f"esc_{field}", method=method, **{field: "../../outside/id"})
        assert r.status == "error", (field, r.fields)
        assert not (profiles_dir(home) / f"esc_{field}.toml").exists()


def test_env_var_expanding_to_traversal_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard runs on the expanded value, the same text resolve_under_home sees."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    monkeypatch.setenv("MRC_ESC_DIR", "../../outside")

    r = _put(
        home, "var_esc", method="password", password_path="$MRC_ESC_DIR/canary.txt"
    )
    assert r.status == "error", r.fields
    assert ".." in str(r.fields.get("msg") or "")
    assert not (profiles_dir(home) / "var_esc.toml").exists()


def test_legacy_profile_with_dotdot_absolute_path_still_loads(
    tmp_path: Path, monkeypatch
) -> None:
    """Profiles written before containment existed must stay readable."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    (profiles_dir(home) / "legacy.toml").write_text(
        'name = "legacy"\n'
        'transport = "ssh"\n'
        'host = "lab"\n'
        'username = "deploy"\n'
        "\n[auth]\n"
        'method = "private_key_path"\n'
        'key_path = "/opt/keys/../id_rsa"\n'
    )
    p = load_profile(home, "legacy")
    assert p.auth.key_path == Path("/opt/keys/../id_rsa").resolve()


def test_normalize_auth_rejects_dotdot_before_any_write() -> None:
    """The write-side guard fails fast.

    ``put_profile`` would also catch this via ``load_profile`` before
    persisting, but the guard belongs on the way in: it keeps the traversal
    policy in one place with the PEM guard and never touches the profile file.
    """
    with pytest.raises(ProfileInvalid, match=r"\.\."):
        config_ops._normalize_auth(
            {"method": "password", "password_path": "secrets/../../outside/x"}
        )
    with pytest.raises(ProfileInvalid, match=r"\.\."):
        config_ops._normalize_auth({"method": "private_key_path", "key_path": "../k"})


def test_load_rejects_dotdot_in_handwritten_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """Containment holds even for a profile written outside config_ops."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    config_ops.run("ensure_home")
    (profiles_dir(home) / "raw.toml").write_text(
        'name = "raw"\n'
        'transport = "ssh"\n'
        'host = "lab"\n'
        'username = "deploy"\n'
        "\n[auth]\n"
        'method = "password"\n'
        'password_path = "secrets/../../outside/canary.txt"\n'
    )
    with pytest.raises(ProfileInvalid, match=r"\.\."):
        load_profile(home, "raw")
