"""Unit tests for load_config types, BOM, logging, and bools."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import (
    ConfigError,
    ConfigInvalid,
    GlobalConfig,
    load_config,
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



# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert isinstance(cfg, GlobalConfig)
    assert cfg.from_defaults is True
    assert cfg.source_path is None
    assert cfg.defaults.verbosity == "normal"


def test_load_config_fixture() -> None:
    cfg = load_config(FIXTURES)
    assert cfg.from_defaults is False
    assert cfg.source_path is not None
    assert cfg.source_path.name == "config.toml"
    assert cfg.defaults.max_body_chars == 24000
    # Fixture may still list removed legacy [security] keys; they are ignored.
    assert cfg.security.strict_perms is False


def test_load_config_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[[[not valid toml", encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    assert isinstance(ei.value, ConfigError)


def test_load_config_utf8_bom(tmp_path: Path) -> None:
    """Notepad/PowerShell UTF-8 BOM must not break config.toml load."""
    body = b"\xef\xbb\xbf" + b'[defaults]\nverbosity = "debug"\n'
    (tmp_path / "config.toml").write_bytes(body)
    cfg = load_config(tmp_path)
    assert cfg.from_defaults is False
    assert cfg.defaults.verbosity == "debug"


def test_load_config_no_bom_utf8_unchanged(tmp_path: Path) -> None:
    """Plain UTF-8 without BOM still loads."""
    (tmp_path / "config.toml").write_text(
        '[defaults]\nverbosity = "warning"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.defaults.verbosity == "warning"


# ---------------------------------------------------------------------------
# [defaults] strict types - no bare int(True)/str(True)
# ---------------------------------------------------------------------------

def test_load_config_defaults_valid_types(tmp_path: Path) -> None:
    """Legal int/str defaults load; missing keys keep built-in defaults."""
    (tmp_path / "config.toml").write_text(
        "[defaults]\n"
        'verbosity = "quiet"\n'
        "max_body_chars = 1000\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.defaults.verbosity == "quiet"
    assert cfg.defaults.max_body_chars == 1000


def test_load_config_defaults_partial_keeps_rest(tmp_path: Path) -> None:
    """Only provided keys override; others stay DefaultsConfig defaults."""
    (tmp_path / "config.toml").write_text(
        "[defaults]\nmax_body_chars = 500\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.defaults.max_body_chars == 500
    assert cfg.defaults.verbosity == "normal"


@pytest.mark.parametrize(
    "body,key",
    [
        ("[defaults]\nmax_body_chars = false\n", "max_body_chars"),
        ("[defaults]\nmax_body_chars = 1.5\n", "max_body_chars"),
        ('[defaults]\nmax_body_chars = "60000"\n', "max_body_chars"),
        ("[defaults]\nmax_body_chars = {}\n", "max_body_chars"),
        ("[defaults]\nmax_body_chars = []\n", "max_body_chars"),
    ],
)
def test_load_config_defaults_int_bad_type_raises(
    tmp_path: Path, body: str, key: str
) -> None:
    """bool/float/str/non-scalar into int fields -> ConfigInvalid."""
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    msg = str(ei.value)
    assert f"[defaults].{key}" in msg
    assert "integer" in msg.lower()
    assert isinstance(ei.value, ConfigError)


@pytest.mark.parametrize(
    "body,key",
    [
        ("[defaults]\nverbosity = true\n", "verbosity"),
        ("[defaults]\nverbosity = false\n", "verbosity"),
        ("[defaults]\nverbosity = 0\n", "verbosity"),
        ("[defaults]\nverbosity = {}\n", "verbosity"),
    ],
)
def test_load_config_defaults_str_bad_type_raises(
    tmp_path: Path, body: str, key: str
) -> None:
    """bool/int/non-scalar into str fields -> ConfigInvalid (not str(True))."""
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    msg = str(ei.value)
    assert f"[defaults].{key}" in msg
    assert "string" in msg.lower()
    assert isinstance(ei.value, ConfigError)


def test_load_config_defaults_not_table_raises(tmp_path: Path) -> None:
    """[defaults] must be a table."""
    (tmp_path / "config.toml").write_text("defaults = true\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    assert "[defaults]" in str(ei.value)
    assert "table" in str(ei.value).lower()


def test_load_config_defaults_winrm_probe_default_full(tmp_path: Path) -> None:
    """Missing winrm_probe -> DefaultsConfig.winrm_probe == full."""
    (tmp_path / "config.toml").write_text(
        "[defaults]\nverbosity = \"normal\"\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.defaults.winrm_probe == "full"
    # Fixture global config also leaves default full.
    fix = load_config(FIXTURES)
    assert fix.defaults.winrm_probe == "full"


def test_load_config_defaults_winrm_probe_skip(tmp_path: Path) -> None:
    """Global [defaults].winrm_probe = skip loads."""
    (tmp_path / "config.toml").write_text(
        "[defaults]\nwinrm_probe = \"skip\"\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.defaults.winrm_probe == "skip"


def test_load_config_defaults_winrm_probe_invalid_raises(tmp_path: Path) -> None:
    """Junk winrm_probe -> ConfigInvalid."""
    (tmp_path / "config.toml").write_text(
        "[defaults]\nwinrm_probe = \"turbo\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    assert "winrm_probe" in str(ei.value)


def test_load_config_strict_perms_true(tmp_path: Path) -> None:
    """A config with [security] strict_perms=true loads into SecurityConfig
    (field is kept, not removed - load.py constructs it as a kwarg, so removal
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


# ---------------------------------------------------------------------------
# [security] bool strict parse (no bool("false") invert)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "literal,expected",
    [
        ("true", True),
        ("false", False),
    ],
)
def test_load_config_strict_perms_toml_bool(
    tmp_path: Path, literal: str, expected: bool
) -> None:
    """Native TOML bool true/false for [security].strict_perms."""
    (tmp_path / "config.toml").write_text(
        f"[security]\nstrict_perms = {literal}\n", encoding="utf-8"
    )
    assert load_config(tmp_path).security.strict_perms is expected


@pytest.mark.parametrize(
    "token,expected",
    [
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
    ],
)
def test_load_config_strict_perms_string_tokens(
    tmp_path: Path, token: str, expected: bool
) -> None:
    """String \"false\"/\"0\" (etc.) must not invert via bool(str)."""
    (tmp_path / "config.toml").write_text(
        f'[security]\nstrict_perms = "{token}"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).security.strict_perms is expected


@pytest.mark.parametrize(
    "section,key,body",
    [
        ("security", "strict_perms", "[security]\nstrict_perms = {}\n"),
        ("security", "strict_perms", "[security]\nstrict_perms = []\n"),
    ],
)
def test_load_config_bool_non_scalar_raises(
    tmp_path: Path, section: str, key: str, body: str
) -> None:
    """dict/list must raise ConfigInvalid - not bool() truthy."""
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    msg = str(ei.value)
    assert f"[{section}].{key}" in msg
    assert "boolean" in msg.lower()


def test_load_config_bool_unknown_string_raises(tmp_path: Path) -> None:
    """Unknown string tokens are invalid (not fail-closed enable)."""
    (tmp_path / "config.toml").write_text(
        '[security]\nstrict_perms = "maybe"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigInvalid) as ei:
        load_config(tmp_path)
    assert "strict_perms" in str(ei.value)


def test_load_config_invalid_msg_relative(tmp_path: Path) -> None:
    """ConfigInvalid for bad config.toml uses relative config.toml loc."""
    home = tmp_path / "mrc"
    home.mkdir()
    (home / "config.toml").write_text("not = [valid\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid) as ei:
        load_config(home)
    msg = str(ei.value)
    assert "config.toml" in msg
    _assert_no_home_abs_in_msg(msg, home)
