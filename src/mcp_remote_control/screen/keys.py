"""xterm-dialect key / paste encoder for PTY stdin bytes.

Encodes named keys (enter, ctrl+c, arrows, ...), modifiers, SGR mouse events,
and bracketed paste. Single source for screen_send action encoding.

Named keys, mouse reports and paste delimiters are terminal control bytes and
are written as-is. Literal text is the peer's text, so it is encoded in the
codec the receiving session resolved for its own reads (see ``encode_text``).
That covers the single-character forms of ``key``/``keys`` actions (plain,
``alt+``, ``shift+``, ``alt+shift+``) as well: the base is a character the peer
receives as text, not a control sequence.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp_remote_control.codec.text_codec import encode_for_remote

ESC = b"\x1b"
CSI = ESC + b"["
# Bracketed paste (xterm): ESC [ 200 ~ ... ESC [ 201 ~
PASTE_START = CSI + b"200~"
PASTE_END = CSI + b"201~"

# Modifier bitmask used by xterm CSI 1;<mod><letter> sequences.
# 1 = none; +1 shift, +2 alt, +4 ctrl, +8 meta/super (xterm convention: base 1).
_MOD_SHIFT = 1
_MOD_ALT = 2
_MOD_CTRL = 4
_MOD_SUPER = 8

# Special keys that are not plain characters.
_NAMED_BASE: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "space": b" ",
    "escape": ESC,
    "esc": ESC,
    "backspace": b"\x7f",
    "bs": b"\x7f",
    "delete": CSI + b"3~",
    "del": CSI + b"3~",
    "insert": CSI + b"2~",
    "ins": CSI + b"2~",
    "home": CSI + b"H",
    "end": CSI + b"F",
    "pageup": CSI + b"5~",
    "page_up": CSI + b"5~",
    "pgup": CSI + b"5~",
    "pagedown": CSI + b"6~",
    "page_down": CSI + b"6~",
    "pgdn": CSI + b"6~",
    "up": CSI + b"A",
    "down": CSI + b"B",
    "right": CSI + b"C",
    "left": CSI + b"D",
    "backtab": CSI + b"Z",  # shift+tab
}

# Arrow / navigation letter used with CSI 1;<mod>X form.
_CSI_LETTER: dict[str, bytes] = {
    "up": b"A",
    "down": b"B",
    "right": b"C",
    "left": b"D",
    "home": b"H",
    "end": b"F",
}

# Function keys F1-F12 (xterm).
_F_KEYS: dict[str, bytes] = {
    "f1": ESC + b"OP",
    "f2": ESC + b"OQ",
    "f3": ESC + b"OR",
    "f4": ESC + b"OS",
    "f5": CSI + b"15~",
    "f6": CSI + b"17~",
    "f7": CSI + b"18~",
    "f8": CSI + b"19~",
    "f9": CSI + b"20~",
    "f10": CSI + b"21~",
    "f11": CSI + b"23~",
    "f12": CSI + b"24~",
}

# Ctrl+letter -> ASCII control byte (ctrl+a=0x01 ... ctrl+z=0x1a; also @[\\]^_).
_CTRL_LETTER: dict[str, int] = {
    "@": 0x00,
    "a": 0x01,
    "b": 0x02,
    "c": 0x03,
    "d": 0x04,
    "e": 0x05,
    "f": 0x06,
    "g": 0x07,
    "h": 0x08,
    "i": 0x09,
    "j": 0x0A,
    "k": 0x0B,
    "l": 0x0C,
    "m": 0x0D,
    "n": 0x0E,
    "o": 0x0F,
    "p": 0x10,
    "q": 0x11,
    "r": 0x12,
    "s": 0x13,
    "t": 0x14,
    "u": 0x15,
    "v": 0x16,
    "w": 0x17,
    "x": 0x18,
    "y": 0x19,
    "z": 0x1A,
    "[": 0x1B,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "?": 0x7F,
}


class KeyEncodeError(ValueError):
    """Raised when a key name cannot be encoded."""


def encode_text(text: str, *, codec: str | None = None) -> bytes:
    """Encode literal text for the peer, in *codec* (default utf-8).

    *codec* is the receiving session's resolved peer codec (``ScreenSession.
    text_codec``, the console ring's ``text_encoding``): a console that reads a
    legacy code page reads typed text in that same code page, so utf-8 bytes
    land on it as mojibake. Encoding never raises - a character the codec
    cannot represent is written as its replacement character (see
    ``codec.text_codec.encode_for_remote``). Control bytes and raw payloads are
    not text and must not pass through here.
    """
    if not text:
        return b""
    return encode_for_remote(text, codec or "utf-8")


def encode_paste(
    text: str,
    *,
    bracketed: bool = True,
    codec: str | None = None,
) -> bytes:
    """Encode paste payload; wrap with bracketed-paste sequences when enabled.

    *bracketed* must reflect the receiving program's state, not a hope:
    xterm's delimiters are only meaningful to a program that announced DECSET
    2004 (see ``screen.send.resolve_bracketed_paste``). A line editor without
    it eats the ``ESC[2`` prefix and inserts the rest of the delimiter
    literally, corrupting the payload. The default exists for byte-level
    round-trips (tests, fixtures); interactive sends must pass the resolved
    capability explicitly.

    The payload is text, so it follows *codec* like :func:`encode_text`. The
    delimiters themselves are terminal control bytes and stay ASCII.
    """
    body = encode_text(text, codec=codec)
    if not bracketed:
        return body
    return PASTE_START + body + PASTE_END


def encode_raw_hex(hex_str: str) -> bytes:
    """Decode a hex string (spaces allowed) to raw bytes."""
    cleaned = "".join(str(hex_str).split())
    if not cleaned:
        return b""
    if len(cleaned) % 2:
        raise KeyEncodeError(f"odd-length hex: {hex_str!r}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise KeyEncodeError(f"invalid hex: {hex_str!r}") from exc


def encode_key(key: str, *, codec: str | None = None) -> bytes:
    """Encode a single named key (xterm dialect) to PTY bytes.

    Syntax: ``[mod+]*base`` where mod is one of ctrl, alt, shift, or
    super/meta/cmd, and base is a named key or single character.

    A single-character base is literal text and follows *codec* (the peer
    session's codec; utf-8 when unset - see :func:`encode_text`). Named keys,
    modifier parameters and control bytes are terminal protocol bytes and are
    written as-is whatever *codec* says.
    """
    if key is None:
        raise KeyEncodeError("key is None")
    raw = str(key).strip()
    if not raw:
        raise KeyEncodeError("empty key")

    # Split on "+" preserving the ORIGINAL case of each segment. Modifier names
    # are matched case-insensitively (ctrl/alt/shift/super), but the base char
    # must keep its case so encode_key("A") -> b"A" and alt+X -> ESC + b"X".
    parts = [p for p in raw.split("+") if p]
    if not parts:
        raise KeyEncodeError(f"empty key: {key!r}")

    mods = 0
    base_parts: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl in ("ctrl", "control", "ctl"):
            mods |= _MOD_CTRL
        elif pl in ("alt", "option", "opt"):
            mods |= _MOD_ALT
        elif pl in ("shift", "sh"):
            mods |= _MOD_SHIFT
        elif pl in ("super", "meta", "cmd", "win", "command"):
            mods |= _MOD_SUPER
        else:
            base_parts.append(p)

    if not base_parts:
        raise KeyEncodeError(f"key has modifiers only: {key!r}")
    if len(base_parts) > 1:
        # Re-join in case of weird names; prefer last segment as base.
        base = base_parts[-1]
    else:
        base = base_parts[0]

    return _encode_base(base, mods=mods, original=raw, codec=codec)


def encode_keys(keys: Iterable[str], *, codec: str | None = None) -> bytes:
    """Encode an ordered sequence of named keys (see :func:`encode_key`)."""
    out = bytearray()
    for k in keys:
        out.extend(encode_key(k, codec=codec))
    return bytes(out)


def _encode_base(
    base: str,
    *,
    mods: int,
    original: str,
    codec: str | None = None,
) -> bytes:
    # Named keys are matched case-insensitively on a lowercased view, while the
    # original-case ``base`` is used for single-character encoding so that
    # encode_key("A") -> b"A" and alt+X -> ESC + b"X" (not lowercased).
    base_lower = base.lower()

    # Function keys
    if base_lower in _F_KEYS:
        # F-keys currently ignore modifiers; emit the unmodified sequence.
        return _F_KEYS[base_lower]

    # Named specials without modifiers
    if base_lower in _NAMED_BASE and mods == 0:
        return _NAMED_BASE[base_lower]

    # backtab is already shift+tab
    if base_lower == "backtab":
        return _NAMED_BASE["backtab"]

    # Arrows / home / end with modifiers -> CSI 1;<n>X
    if base_lower in _CSI_LETTER:
        if mods == 0:
            return _NAMED_BASE[base_lower]
        # xterm: modifier param = mods + 1
        param = mods + 1
        return CSI + f"1;{param}".encode("ascii") + _CSI_LETTER[base_lower]

    # pageup/pagedown/delete/insert with mods -> CSI n;<mod>~
    _tilde_codes = {
        "pageup": 5,
        "page_up": 5,
        "pgup": 5,
        "pagedown": 6,
        "page_down": 6,
        "pgdn": 6,
        "insert": 2,
        "ins": 2,
        "delete": 3,
        "del": 3,
    }
    if base_lower in _tilde_codes:
        code = _tilde_codes[base_lower]
        if mods == 0:
            return _NAMED_BASE.get(base_lower, CSI + f"{code}~".encode("ascii"))
        param = mods + 1
        return CSI + f"{code};{param}~".encode("ascii")

    # shift+tab -> backtab (plain tab is handled above when mods == 0).
    if base_lower == "tab" and mods == _MOD_SHIFT:
        return _NAMED_BASE["backtab"]

    # ctrl+letter / ctrl+symbol (case-insensitive: ctrl+C == ctrl+c)
    if mods == _MOD_CTRL and len(base) == 1:
        ch = base_lower
        if ch in _CTRL_LETTER:
            return bytes([_CTRL_LETTER[ch]])

    # alt+char -> ESC + char (xterm classic); preserve original char case.
    # The char is peer text, so it follows the peer codec; ESC stays a byte.
    if mods == _MOD_ALT and len(base) == 1:
        return ESC + encode_text(base, codec=codec)

    # alt+shift+letter -> ESC + uppercase letter (shift forces upper).
    if mods == (_MOD_ALT | _MOD_SHIFT) and len(base) == 1 and base.isalpha():
        return ESC + encode_text(base.upper(), codec=codec)

    # alt+ctrl+char
    if mods == (_MOD_ALT | _MOD_CTRL) and len(base) == 1:
        ch = base_lower
        if ch in _CTRL_LETTER:
            return ESC + bytes([_CTRL_LETTER[ch]])

    # Plain single character - preserve original case (mods == 0). Text typed
    # into the peer, so it follows the peer codec like any other literal text.
    if mods == 0 and len(base) == 1:
        return encode_text(base, codec=codec)

    # shift+letter -> uppercase
    if mods == _MOD_SHIFT and len(base) == 1 and base.isalpha():
        return encode_text(base.upper(), codec=codec)

    raise KeyEncodeError(f"unsupported key: {original!r}")


# Accepted button names and their xterm Cb base value. Anything else is a
# caller error: defaulting an unrecognised name to 0 would deliver a LEFT
# press+release - activating whatever control is under the cell - for a caller
# who asked for something else (a wheel name, a typo), and report ok.
_MOUSE_BUTTONS: dict[str, int] = {
    "left": 0,
    "middle": 1,
    "right": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}


def encode_mouse_sgr(
    row: int,
    col: int,
    *,
    button: str = "left",
    press: bool = True,
    mods: Iterable[str] | None = None,
) -> bytes:
    """Encode an SGR mouse event (1-based cell coords as xterm expects).

    *row*/*col* are 0-based agent coords; converted to 1-based for the wire.
    *button* must be one of the names in ``_MOUSE_BUTTONS``; an unrecognised
    value raises rather than defaulting (see that table).
    """
    try:
        b = _MOUSE_BUTTONS[str(button).lower()]
    except KeyError as exc:
        raise KeyEncodeError(
            f"unsupported mouse button: {button!r} "
            f"(expected one of {sorted(_MOUSE_BUTTONS)})"
        ) from exc
    mod_bits = 0
    for m in mods or ():
        ml = str(m).lower()
        if ml in ("shift", "sh"):
            mod_bits |= 4
        elif ml in ("alt", "option", "meta"):
            mod_bits |= 8
        elif ml in ("ctrl", "control"):
            mod_bits |= 16
    cb = b | mod_bits
    # xterm SGR: CSI < Cb ; Cx ; Cy M (press) / m (release)
    cx = max(1, int(col) + 1)
    cy = max(1, int(row) + 1)
    final = b"M" if press else b"m"
    return CSI + b"<" + f"{cb};{cx};{cy}".encode("ascii") + final
