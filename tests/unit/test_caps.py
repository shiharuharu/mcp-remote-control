"""Capability matrix (019 / T06)."""

from __future__ import annotations

from mcp_remote_control.endpoint.caps import caps_for_transport, format_caps, merge_caps


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
