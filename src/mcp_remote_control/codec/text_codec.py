"""Auto text codec for remote/local byte streams.

Decoding is ordered by trust. A strict UTF-8 read of the whole buffer wins
outright because UTF-8 is the only self-validating leg here: gb18030/cp936/
latin-1 accept almost any byte string, so a "successful" strict decode through
them is not evidence of anything. A caller-supplied codec therefore only ever
sees bytes that are not UTF-8, and a body whose only damage is a cut
multi-byte character is repaired in place instead of being re-read whole.
Replacement is the last resort so Agent-track paths never raise on bad bytes.
A body both legs accept is reported as ``ambiguous``: the UTF-8 reading still
wins, but no caller has to mistake it for a corroborated one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecodeResult:
    """Result of decoding remote/local byte streams to Unicode text.

    ``encoding_used`` names the leg that produced ``text``. ``replaced`` is set
    when that leg had to drop bytes, and ``errors`` counts the U+FFFD
    characters it wrote for them - a damaged run of several bytes can leave one
    replacement character, so it is a count of *characters* in ``text``, not of
    bytes lost. ``fallback`` is set whenever the text did *not* come from a
    whole-buffer strict UTF-8 read - a legacy codec leg, an in-place repair, or
    replacement - i.e. whenever the codec choice could not be corroborated by
    the bytes themselves. ``ambiguous`` is the other half of that: the text
    *did* come from a strict UTF-8 read, but the caller's configured codec
    strict-decoded the same bytes to *different* text, so the byte string
    carries no evidence for either reading and the choice was not corroborated
    either. Callers that surface output to a reader use both flags to say which
    codec produced the text; a clean UTF-8 read leaves them False and needs no
    comment.
    """

    text: str
    encoding_used: str
    replaced: bool
    errors: int = 0
    fallback: bool = False
    ambiguous: bool = False


# Common locale charmap / code page -> Python codec names.
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

# Windows console / chcp code pages -> Python codec.
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

    Examples: ``936 -> gb18030``, ``65001 -> utf-8``.
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


def _strip_leading_bom(text: str) -> str:
    """Remove leading U+FEFF (UTF-8/UTF-16 BOM as a character after decode)."""
    if text.startswith("\ufeff"):
        return text.lstrip("\ufeff")
    return text


def _utf8_repair(raw: bytes) -> tuple[str, bool]:
    """UTF-8 reading of a body that failed strict decoding, plus its evidence.

    Returns ``(text, has_multibyte)``: *text* is the UTF-8 reading with the
    invalid byte run replaced by U+FFFD, and *has_multibyte* reports whether
    the readable part is more than ASCII \u2014 in a UTF-8 reading that means real
    multi-byte text. A body whose readable part is ASCII-only is a fragment
    (or legacy text) and carries no evidence for reading it as UTF-8.

    Only called for buffers that already failed strict UTF-8.
    """
    readable = raw.decode("utf-8", errors="ignore")
    return raw.decode("utf-8", errors="replace"), any(
        ch > "\x7f" for ch in readable
    )


def _other_reading_differs(raw: bytes, text: str, pref: str | None) -> bool:
    """Whether the caller's configured codec reads *raw* as different text.

    Both legs accepting the bytes is only a problem when they disagree: an
    ASCII-only body (most CLI output) reads identically under every codec here,
    so nothing is ambiguous and nothing needs reporting. When they do
    disagree, the byte string carries no evidence for either reading: on a
    chcp-936 host ``echo \u4e00\u76f4`` writes GBK bytes that are also valid
    UTF-8, which decode to two unrelated characters instead of the intended
    text, and no bytes can say which codec wrote it. The UTF-8 reading still
    wins - see :func:`decode_auto` - but a read this ambiguous must be
    visible to the caller instead of looking like a corroborated UTF-8 read.
    """
    if not pref or pref == "utf-8":
        return False
    try:
        return raw.decode(pref) != text
    except (UnicodeDecodeError, LookupError):
        return False


def decode_auto(
    data: bytes | str | None,
    preferred: str | None = None,
) -> DecodeResult:
    """Decode *data* with a trust-ordered chain that never raises.

    Policy:
    - ``None`` -> empty text, encoding ``unicode``; ``str`` -> returned as-is
      (already Unicode, encoding ``unicode``).
    - ``bytes``, in order:

      1. strict UTF-8 over the whole buffer -> ``utf-8``: the only
         self-validating leg, so it is decided first. A permissive *preferred*
         (gb18030 / cp936 / latin-1) strict-decodes almost any byte string and
         would silently re-read valid UTF-8 as something else. Trade-off: a
         legacy body that happens to be valid UTF-8 is read as UTF-8 - short
         bodies can line up by accident, ASCII-only bodies are identical either
         way. When *preferred* reads the same bytes as different text, both
         readings are unevidenced and the result carries ``ambiguous=True``.
      2. a body cut mid-character - one incomplete sequence at the end of the
         buffer with a readable multi-byte prefix -> ``utf-8`` with that run
         replaced, ``replaced=True``. Re-reading such a body through a
         permissive leg would turn one cut character into whole-body mojibake;
         damage anywhere else reaches the legacy legs below.
      3. strict *preferred* (when set) -> the configured codec; 4. strict
         gb18030, the historical CJK default; 5. utf-8 with ``errors=replace``
         as the last resort, so Agent-track paths never raise on bad bytes.

    Leading U+FEFF is stripped from decoded text so callers never see a BOM
    character from Notepad-style UTF-8 output.
    """
    if data is None:
        return DecodeResult(text="", encoding_used="unicode", replaced=False, errors=0)
    if isinstance(data, str):
        return DecodeResult(
            text=_strip_leading_bom(data),
            encoding_used="unicode",
            replaced=False,
            errors=0,
        )
    if not isinstance(data, (bytes, bytearray, memoryview)):
        text = _strip_leading_bom(str(data))
        return DecodeResult(
            text=text, encoding_used="unicode", replaced=False, errors=0
        )

    raw = bytes(data)
    pref = _normalize_preferred(preferred)
    if not raw:
        return DecodeResult(
            text="", encoding_used=pref or "utf-8", replaced=False, errors=0
        )

    # 1. Whole-buffer strict UTF-8: self-validating, so it is decided first.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Bytes past the first invalid run decode only if the run is the last
        # thing in the buffer: that is a character cut by the read boundary.
        # Anything else is data the stream really contains (see the docstring).
        cut_at_end = exc.end == len(raw)
    else:
        return DecodeResult(
            text=_strip_leading_bom(text),
            encoding_used="utf-8",
            replaced=False,
            errors=0,
            ambiguous=_other_reading_differs(raw, text, pref),
        )

    # 2. Cut-at-the-end body: repair the one damaged run in place rather than
    #    re-reading the whole body through a codec that accepts anything.
    if cut_at_end:
        repaired, has_multibyte = _utf8_repair(raw)
        if has_multibyte:
            text = _strip_leading_bom(repaired)
            return DecodeResult(
                text=text,
                encoding_used="utf-8",
                replaced=True,
                errors=text.count("\ufffd"),
                fallback=True,
            )

    if pref and pref != "utf-8":
        # 3. The caller-configured codec, for bodies that are not UTF-8.
        try:
            return DecodeResult(
                text=_strip_leading_bom(raw.decode(pref)),
                encoding_used=pref,
                replaced=False,
                errors=0,
                fallback=True,
            )
        except (UnicodeDecodeError, LookupError):
            pass

    if pref != "gb18030":
        # 4. Historical CJK default.
        try:
            return DecodeResult(
                text=_strip_leading_bom(raw.decode("gb18030")),
                encoding_used="gb18030",
                replaced=False,
                errors=0,
                fallback=True,
            )
        except (UnicodeDecodeError, LookupError):
            pass

    # Last resort: utf-8 replace (Agent-track default - never raise on bad bytes).
    text = _strip_leading_bom(raw.decode("utf-8", errors="replace"))
    errors = text.count("\ufffd")
    return DecodeResult(
        text=text,
        encoding_used="utf-8",
        replaced=True,
        errors=errors,
        fallback=True,
    )


def encode_for_remote(text: str, preferred: str = "utf-8") -> bytes:
    """Encode Unicode text for remote command/PTY input in a legacy codec.

    The encode-side twin of :func:`decode_auto`, for a caller that must speak
    the same legacy codec it configured for decoding: *preferred* is used when
    it can represent the text, and encoding never raises - a character the
    codec cannot represent becomes ``?`` instead of failing the send path.
    UTF-8 is the parameter default, and the codec every input path falls back
    to when the peer session resolved none; this helper exists so such a caller
    does not have to re-derive the fallback.
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
