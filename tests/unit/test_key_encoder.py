"""Unit tests: xterm key encoder (T10)."""

from __future__ import annotations

import pytest

from mcp_remote_control.screen.keys import (
    CSI,
    ESC,
    PASTE_END,
    PASTE_START,
    KeyEncodeError,
    encode_key,
    encode_keys,
    encode_mouse_sgr,
    encode_paste,
    encode_raw_hex,
    encode_text,
)


def test_enter_tab_escape_space_backspace() -> None:
    assert encode_key("enter") == b"\r"
    assert encode_key("return") == b"\r"
    assert encode_key("tab") == b"\t"
    assert encode_key("escape") == ESC
    assert encode_key("esc") == ESC
    assert encode_key("space") == b" "
    assert encode_key("backspace") == b"\x7f"


def test_arrows() -> None:
    assert encode_key("up") == CSI + b"A"
    assert encode_key("down") == CSI + b"B"
    assert encode_key("right") == CSI + b"C"
    assert encode_key("left") == CSI + b"D"


def test_ctrl_c_and_common_controls() -> None:
    assert encode_key("ctrl+c") == b"\x03"
    assert encode_key("ctrl+d") == b"\x04"
    assert encode_key("ctrl+z") == b"\x1a"
    assert encode_key("ctrl+l") == b"\x0c"
    assert encode_key("ctrl+u") == b"\x15"
    assert encode_key("CTRL+C") == b"\x03"


def test_shift_and_ctrl_arrows() -> None:
    # shift = +1 → param 2; ctrl = +4 → param 5
    assert encode_key("shift+up") == CSI + b"1;2A"
    assert encode_key("ctrl+left") == CSI + b"1;5D"
    assert encode_key("ctrl+shift+right") == CSI + b"1;6C"


def test_alt_letter() -> None:
    assert encode_key("alt+x") == ESC + b"x"
    assert encode_key("alt+a") == ESC + b"a"


def test_function_keys() -> None:
    assert encode_key("f1") == ESC + b"OP"
    assert encode_key("f5") == CSI + b"15~"
    assert encode_key("f12") == CSI + b"24~"


def test_page_home_end_delete() -> None:
    assert encode_key("pageup") == CSI + b"5~"
    assert encode_key("pagedown") == CSI + b"6~"
    assert encode_key("home") == CSI + b"H"
    assert encode_key("end") == CSI + b"F"
    assert encode_key("delete") == CSI + b"3~"


def test_backtab() -> None:
    assert encode_key("backtab") == CSI + b"Z"
    assert encode_key("shift+tab") == CSI + b"Z"


def test_plain_char() -> None:
    assert encode_key("a") == b"a"
    assert encode_key(":") == b":"


def test_encode_keys_sequence_order() -> None:
    assert encode_keys(["escape", "up", "up"]) == (
        encode_key("escape") + encode_key("up") + encode_key("up")
    )


def test_encode_text_and_paste() -> None:
    assert encode_text("hello") == b"hello"
    paste = encode_paste("line1\nline2")
    assert paste.startswith(PASTE_START)
    assert paste.endswith(PASTE_END)
    assert b"line1\nline2" in paste
    assert encode_paste("x", bracketed=False) == b"x"


def test_encode_raw_hex() -> None:
    assert encode_raw_hex("1b5b41") == ESC + b"[A"
    assert encode_raw_hex("1b 5b 41") == ESC + b"[A"
    with pytest.raises(KeyEncodeError):
        encode_raw_hex("zzz")


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyEncodeError):
        encode_key("ctrl+shift+alt+super+unknownbase")


def test_mouse_sgr_click() -> None:
    # row=0,col=0 → 1-based 1;1
    press = encode_mouse_sgr(0, 0, button="left", press=True)
    assert press == CSI + b"<0;1;1M"
    release = encode_mouse_sgr(5, 10, button="left", press=False)
    assert release == CSI + b"<0;11;6m"


# ---------------------------------------------------------------------------
# O3: base-char case preservation (modifiers stay case-insensitive)
# ---------------------------------------------------------------------------


def test_single_char_case_preserved() -> None:
    # Plain single char must keep original case (was lowercased to b"a").
    assert encode_key("A") == b"A"
    assert encode_key("a") == b"a"
    assert encode_key("Z") == b"Z"
    assert encode_key("z") == b"z"
    # Non-letter single chars unchanged.
    assert encode_key(":") == b":"
    assert encode_key("/") == b"/"


def test_alt_letter_case_preserved() -> None:
    # alt+letter preserves the original-case base char.
    assert encode_key("alt+X") == ESC + b"X"
    assert encode_key("alt+x") == ESC + b"x"
    assert encode_key("alt+A") == ESC + b"A"


def test_alt_shift_letter_uppercase() -> None:
    # alt+shift+letter → ESC + uppercase (shift forces upper).
    assert encode_key("alt+shift+x") == ESC + b"X"
    assert encode_key("alt+shift+X") == ESC + b"X"
    assert encode_key("Alt+Shift+a") == ESC + b"A"


def test_shift_letter_uppercase() -> None:
    assert encode_key("shift+a") == b"A"
    assert encode_key("shift+A") == b"A"


def test_ctrl_case_insensitive_still() -> None:
    # ctrl is case-insensitive on both modifier name and base letter.
    assert encode_key("ctrl+C") == b"\x03"
    assert encode_key("Ctrl+c") == b"\x03"
    assert encode_key("CTRL+C") == b"\x03"


def test_named_keys_case_insensitive_still() -> None:
    assert encode_key("ENTER") == b"\r"
    assert encode_key("Tab") == b"\t"
    assert encode_key("UP") == CSI + b"A"
    assert encode_key("Backspace") == b"\x7f"
    assert encode_key("F5") == CSI + b"15~"
