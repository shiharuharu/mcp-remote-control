"""Auto text codec for remote/local byte streams.

Decode chain is preferred (when set) → utf-8 → gb18030, each strict; the final
fallback is utf-8 with replacement so Agent-track paths never raise on bad
bytes. Encode for remote input prefers the requested codec and falls back the
same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecodeResult:
    """Result of decoding remote/local byte streams to Unicode text."""

    text: str
    encoding_used: str
    replaced: bool
    errors: int = 0


# Common locale charmap / code page → Python codec names.
_CHARMAP_ALIASES: dict[str, str] = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "utf8mb4": "utf-8",
    "ansi_x3.4-1968": "ascii",
    "us-ascii": "ascii",
    "ascii": "ascii",
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "gb18030": "gb18030",
    "cp936": "gb18030",
    "windows-936": "gb18030",
    "ms936": "gb18030",
    "iso-8859-1": "latin-1",
    "iso8859-1": "latin-1",
    "latin1": "latin-1",
    "latin-1": "latin-1",
    "cp1252": "cp1252",
    "windows-1252": "cp1252",
    "big5": "big5",
    "cp950": "big5",
    "euc-jp": "euc_jp",
    "eucjp": "euc_jp",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "cp932": "cp932",
}

# Windows console / chcp code pages → Python codec.
_CODEPAGE_TO_CODEC: dict[int, str] = {
    65001: "utf-8",
    936: "gb18030",
    54936: "gb18030",
    950: "big5",
    932: "cp932",
    949: "cp949",
    1252: "cp1252",
    437: "cp437",
    850: "cp850",
    20127: "ascii",
    28591: "latin-1",
}


def charmap_to_codec(name: str | None) -> str | None:
    """Map a locale charmap / encoding label to a Python codec name.

    Returns None when *name* is empty or unrecognized.
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None
    # Strip trailing punctuation sometimes left by shell probes.
    text = text.strip("\"'`").rstrip(".;,")
    key = text.lower().replace(" ", "")
    if key in _CHARMAP_ALIASES:
        return _CHARMAP_ALIASES[key]
    # Already a known Python codec?
    try:
        "".encode(text)
        return text.lower().replace("_", "-") if text.lower() == "utf_8" else text
    except LookupError:
        pass
    # Try normalized form (underscores).
    try:
        alt = text.lower().replace("-", "_")
        "".encode(alt)
        return alt
    except LookupError:
        return None


def codepage_to_codec(n: int | str | None) -> str | None:
    """Map a Windows code page number (e.g. chcp) to a Python codec.

    Examples: ``936 → gb18030``, ``65001 → utf-8``.
    """
    if n is None:
        return None
    try:
        if isinstance(n, str):
            text = n.strip()
            # Accept "Active code page: 936" style fragments.
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits:
                return None
            code = int(digits)
        else:
            code = int(n)
    except (TypeError, ValueError):
        return None
    if code in _CODEPAGE_TO_CODEC:
        return _CODEPAGE_TO_CODEC[code]
    # Fall back to Python's cpXXXX name when available.
    candidate = f"cp{code}"
    try:
        "".encode(candidate)
        return candidate
    except LookupError:
        return None


def decode_auto(
    data: bytes | str | None,
    preferred: str | None = None,
) -> DecodeResult:
    """Decode *data* with a strict preferred → utf-8 → gb18030 chain.

    Policy:
    - ``None`` → empty text, encoding ``unicode``
    - ``str`` → returned as-is (already Unicode), encoding ``unicode``
    - ``bytes``: try preferred (if set), then utf-8, then gb18030, each *strict*;
      final fallback is utf-8 with ``errors=replace`` (``replaced=True``).
    """
    if data is None:
        return DecodeResult(text="", encoding_used="unicode", replaced=False, errors=0)
    if isinstance(data, str):
        return DecodeResult(
            text=data, encoding_used="unicode", replaced=False, errors=0
        )
    if not isinstance(data, (bytes, bytearray, memoryview)):
        text = str(data)
        return DecodeResult(
            text=text, encoding_used="unicode", replaced=False, errors=0
        )

    raw = bytes(data)
    if not raw:
        enc = _normalize_preferred(preferred) or "utf-8"
        return DecodeResult(text="", encoding_used=enc, replaced=False, errors=0)

    chain: list[str] = []
    pref = _normalize_preferred(preferred)
    if pref:
        chain.append(pref)
    for enc in ("utf-8", "gb18030"):
        if enc not in chain:
            chain.append(enc)

    for enc in chain:
        try:
            text = raw.decode(enc)  # strict
            return DecodeResult(
                text=text, encoding_used=enc, replaced=False, errors=0
            )
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: utf-8 replace (Agent-track default — never raise on bad bytes).
    text = raw.decode("utf-8", errors="replace")
    errors = text.count("\ufffd")
    return DecodeResult(
        text=text, encoding_used="utf-8", replaced=True, errors=errors
    )


def encode_for_remote(text: str, preferred: str = "utf-8") -> bytes:
    """Encode Unicode text for remote command/PTY input.

    Uses *preferred* when possible; falls back to utf-8 with replacement so
    a send path never raises on exotic characters.
    """
    if text is None:
        return b""
    if not isinstance(text, str):
        text = str(text)
    enc = _normalize_preferred(preferred) or "utf-8"
    try:
        return text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        try:
            return text.encode(enc, errors="replace")
        except LookupError:
            return text.encode("utf-8", errors="replace")


def _normalize_preferred(preferred: str | None) -> str | None:
    if preferred is None:
        return None
    text = str(preferred).strip()
    if not text or text.lower() in ("unicode", "none", "auto"):
        return None
    mapped = charmap_to_codec(text)
    return mapped or text


def decode_to_str(
    data: Any,
    preferred: str | None = None,
) -> str:
    """Convenience: ``decode_auto(...).text`` for transport stream helpers."""
    return decode_auto(data, preferred=preferred).text
