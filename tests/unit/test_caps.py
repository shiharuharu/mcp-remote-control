"""Capability matrix, strict bool merge, and shared truthiness."""

from __future__ import annotations

import pytest

from mcp_remote_control.endpoint.caps import (
    _coerce_cap_bool,
    caps_for_transport,
    coerce_toml_bool,
    format_caps,
    merge_caps,
)


def test_local_caps() -> None:
    c = caps_for_transport("local")
    assert c["exec"] is True
    assert c["fs"] is True
    assert c["screen"] is True
    assert c["ps"] is False


def test_ssh_caps() -> None:
    c = caps_for_transport("ssh")
    assert c == caps_for_transport("local")


def test_winrm_caps() -> None:
    c = caps_for_transport("winrm")
    assert c["exec"] is True
    assert c["fs"] is True
    assert c["screen"] is False
    assert c["ps"] is True


def test_unknown_transport_all_false() -> None:
    c = caps_for_transport("rdp")
    assert c == {"exec": False, "fs": False, "screen": False, "ps": False}


def test_format_and_merge() -> None:
    assert format_caps(caps_for_transport("local")) == "exec,fs,screen"
    assert format_caps(caps_for_transport("winrm")) == "exec,fs,ps"
    merged = merge_caps("ssh", {"screen": False})
    assert merged["screen"] is False
    assert merged["exec"] is True


# --- strict bool coerce (bool("false")/bool("0") must not enable) ---


@pytest.mark.parametrize(
    "value",
    [
        False,
        0,
        0.0,
        "false",
        "False",
        "FALSE",
        "  false  ",
        "0",
        " 0 ",
        "no",
        "NO",
        "off",
        "Off",
        "",
        "   ",
        None,
    ],
)
def test_coerce_cap_bool_falsey(value: object) -> None:
    assert coerce_toml_bool(value) is False
    # Alias kept for historical callers / tests.
    assert _coerce_cap_bool(value) is False


@pytest.mark.parametrize(
    "value",
    [
        True,
        1,
        1.0,
        2,
        "true",
        "True",
        "TRUE",
        "  true  ",
        "1",
        " 1 ",
        "yes",
        "YES",
        "on",
        "On",
    ],
)
def test_coerce_cap_bool_truthy(value: object) -> None:
    assert coerce_toml_bool(value) is True
    assert _coerce_cap_bool(value) is True


def test_coerce_cap_bool_unknown_string_fail_closed() -> None:
    """Non-token strings must not enable (bool(\"maybe\") would be True)."""
    assert coerce_toml_bool("maybe") is False
    assert coerce_toml_bool("enabled") is False
    assert _coerce_cap_bool("enabled") is False


def test_coerce_cap_bool_non_scalar_fail_closed() -> None:
    """dict/list/tuple and other non-scalars must not enable via bool(value)."""
    assert coerce_toml_bool({}) is False
    assert coerce_toml_bool([]) is False
    # Non-empty containers are truthy under bool() but still fail closed.
    assert coerce_toml_bool({"enabled": True}) is False
    assert coerce_toml_bool([True]) is False
    assert coerce_toml_bool((1,)) is False
    assert coerce_toml_bool(b"true") is False
    assert coerce_toml_bool(object()) is False


def test_coerce_toml_bool_is_shared_endpoint_truthiness() -> None:
    """Single helper; alias identity + string false/0 stay false."""
    assert _coerce_cap_bool is coerce_toml_bool
    assert coerce_toml_bool("false") is False
    assert coerce_toml_bool("0") is False
    assert coerce_toml_bool("no") is False
    assert coerce_toml_bool("off") is False


def test_merge_caps_non_scalar_does_not_enable() -> None:
    """Mis-shaped TOML under [caps] (table/array) must not enable a cap."""
    # ssh base has screen=True; override with {} must disable, not leave enabled
    # via bool({})==False path is ok, but non-empty table must also not enable.
    m_empty = merge_caps("ssh", {"screen": {}})
    assert m_empty["screen"] is False
    assert m_empty["exec"] is True

    m_list = merge_caps("ssh", {"screen": []})
    assert m_list["screen"] is False

    # winrm base has screen=False; non-scalar override must stay disabled
    m_winrm = merge_caps("winrm", {"screen": {"nested": True}})
    assert m_winrm["screen"] is False


def test_merge_caps_string_false_and_zero_disable() -> None:
    """TOML/JSON string overrides: \"false\" / \"0\" must disable, not enable."""
    # ssh base has screen=True; override with string false/"0"
    m_false = merge_caps("ssh", {"screen": "false"})
    assert m_false["screen"] is False
    assert m_false["exec"] is True

    m_zero_str = merge_caps("ssh", {"screen": "0"})
    assert m_zero_str["screen"] is False

    m_zero_int = merge_caps("ssh", {"screen": 0})
    assert m_zero_int["screen"] is False

    m_no = merge_caps("ssh", {"screen": "no", "ps": "off"})
    assert m_no["screen"] is False
    assert m_no["ps"] is False


def test_merge_caps_true_true_string_one_enable() -> None:
    """True / \"true\" / 1 still enable (incl. winrm screen off by default)."""
    m_bool = merge_caps("winrm", {"screen": True})
    assert m_bool["screen"] is True

    m_str = merge_caps("winrm", {"screen": "true"})
    assert m_str["screen"] is True

    m_one = merge_caps("winrm", {"screen": 1})
    assert m_one["screen"] is True

    m_one_str = merge_caps("winrm", {"screen": "1"})
    assert m_one_str["screen"] is True

    m_yes = merge_caps("winrm", {"screen": "yes", "ps": "on"})
    assert m_yes["screen"] is True
    assert m_yes["ps"] is True


def test_merge_caps_unknown_key_ignored() -> None:
    merged = merge_caps("local", {"bogus": True, "screen": "false"})
    assert "bogus" not in merged
    assert merged["screen"] is False
