"""Encoding and decoding helpers for remote I/O.

Maps locale charmaps and Windows code pages to Python codecs, and decodes
remote bytes with a preferred -> utf-8 -> gb18030 chain that never raises on
bad data for Agent-track paths.
"""

from __future__ import annotations

from mcp_remote_control.codec.text_codec import (
    DecodeResult,
    charmap_to_codec,
    codepage_to_codec,
    decode_auto,
    encode_for_remote,
)

__all__ = [
    "DecodeResult",
    "charmap_to_codec",
    "codepage_to_codec",
    "decode_auto",
    "encode_for_remote",
]
