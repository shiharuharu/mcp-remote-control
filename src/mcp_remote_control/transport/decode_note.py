"""Shared decode-note policy for the exec transports.

The local and SSH transports both decode child/host bytes with the configured
codec, and both have to tell the reader when that reading was not corroborated:
a fallback decode (the bytes were not valid UTF-8) or an ambiguous one (the
configured codec and UTF-8 both accept the bytes, so the text may be a
plausible reading of the wrong codec). This module owns that rule once; the
caller passes its own logger so a record keeps naming the module that read the
bytes, which is what per-module logging configuration selects on.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_remote_control.codec.text_codec import DecodeResult


def note_decode(
    logger: logging.Logger,
    transport: Any,
    result: DecodeResult,
    preferred: str | None,
    value: Any,
) -> None:
    """Warn once per kind when a stream's decode was not corroborated.

    A fallback decode is not an error - a host that really speaks gb18030 is
    decoded correctly through its leg - but the reader can no longer assume the
    text is right, and nothing else in the exec path says which codec produced
    it. The same goes for an ambiguous one (both the configured codec and
    UTF-8 accept the bytes): the text may be a plausible reading of the wrong
    codec. The WARNING is emitted once per (transport, codec, preferred) so a
    legacy peer does not log a line per command; later occurrences stay at
    DEBUG.

    *transport* carries the ``_decode_warned`` set that makes the warning
    once-per-codec; ``None`` reports nothing, because there is no per-transport
    place to keep that decision.
    """
    if transport is None:
        return
    # A stream with no bytes carries no encoding evidence, so it is never
    # reported as a fallback or ambiguous read.
    if value is None or value == b"" or value == "":
        return
    if not (result.fallback or result.ambiguous):
        return
    size = len(value) if isinstance(value, (bytes, bytearray, memoryview)) else 0
    kind = "fallback" if result.fallback else "ambiguous"
    key = f"{result.encoding_used}|{preferred or ''}|{result.replaced}|{kind}"
    if key in transport._decode_warned:
        logger.debug(
            "text decode %s to %s (%d bytes, preferred=%s, replaced=%s, errors=%d)",
            kind,
            result.encoding_used,
            size,
            preferred,
            result.replaced,
            result.errors,
        )
        return
    transport._decode_warned.add(key)
    if result.fallback:
        logger.warning(
            "text decode fell back to %s (%d bytes, preferred=%s, replaced=%s, errors=%d): "
            "the bytes were not valid UTF-8, so the text may be mis-decoded",
            result.encoding_used,
            size,
            preferred,
            result.replaced,
            result.errors,
        )
        return
    logger.warning(
        "text decode is ambiguous: %s accepted the %d bytes as well as utf-8 "
        "(preferred=%s): the text was read as utf-8 but the configured codec "
        "would read it differently",
        result.encoding_used,
        size,
        preferred,
    )
