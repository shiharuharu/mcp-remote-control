"""Unit tests for codec/text_codec (004 encoding chain)."""

from __future__ import annotations

from mcp_remote_control.codec import (
    charmap_to_codec,
    codepage_to_codec,
    decode_auto,
    encode_for_remote,
)


def test_decode_gbk_chinese_with_preferred() -> None:
    raw = "你好".encode("gbk")
    r = decode_auto(raw, preferred="gb18030")
    assert r.text == "你好"
    assert r.encoding_used in ("gb18030", "gbk")
    assert r.replaced is False


def test_decode_utf8_chinese_not_garbled() -> None:
    raw = "你好世界".encode()
    r = decode_auto(raw, preferred="utf-8")
    assert r.text == "你好世界"
    assert r.encoding_used == "utf-8"
    assert r.replaced is False


def test_decode_auto_fallback_replace() -> None:
    # Invalid as utf-8 and as gb18030 in some cases — use lone invalid utf-8 byte
    raw = b"\xff\xfe not valid"
    r = decode_auto(raw, preferred="utf-8")
    assert isinstance(r.text, str)
    # May succeed as gb18030 or replace — either is fine if no raise
    assert r.encoding_used in ("utf-8", "gb18030")


def test_codepage_936_and_65001() -> None:
    assert codepage_to_codec(936) == "gb18030"
    assert codepage_to_codec(65001) == "utf-8"
    assert codepage_to_codec("Active code page: 936") == "gb18030"


def test_charmap_aliases() -> None:
    assert charmap_to_codec("UTF-8") == "utf-8"
    assert charmap_to_codec("GBK") == "gb18030"


def test_encode_for_remote() -> None:
    assert encode_for_remote("hi", "utf-8") == b"hi"
    assert isinstance(encode_for_remote("中", "gb18030"), bytes)
