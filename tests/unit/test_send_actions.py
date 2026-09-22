"""Unit tests: screen send actions, encoding order, and nav."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.screen.buffer import (
    PWD_MARKER,
    dump_frame,
    find_text,
    mouse_tracking_enabled,
)
from mcp_remote_control.screen.keys import (
    CSI,
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
from mcp_remote_control.screen.send import (
    ACTION_IMPL,
    ACTION_TYPES,
    GO_STEP_CAP,
    ActionError,
    _actions_have_submit,
    _is_noop_actions,
    apply_action,
    bracketed_paste_enabled,
    encode_actions_bytes,
    execute_send,
    normalize_actions,
    normalize_wait,
    resolve_bracketed_paste,
    sgr_mouse_enabled,
    wait_after,
)
from mcp_remote_control.screen.session import ScreenSession

# ---------------------------------------------------------------------------
# Fake PTY for pure action tests (no live process)
# ---------------------------------------------------------------------------


class FakePty:
    def __init__(self, cols: int = 120, rows: int = 40, cwd: str | None = "/tmp") -> None:
        self.cols = cols
        self.rows = rows
        # Explicit for PtyHandle (mutable attrs are invariant).
        self.cwd: str | None = cwd
        self.written = bytearray()
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None if self._alive else 0

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        return 0

    def close(self) -> None:
        self._alive = False


def _session(cols: int = 80, rows: int = 24) -> tuple[ScreenSession, FakePty]:
    pty = FakePty(cols=cols, rows=rows)
    sess = ScreenSession(
        id="scr_test",
        ep="local",
        pty=pty,
        cols=cols,
        rows=rows,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    return sess, pty


def _paint(sess: ScreenSession, text: str, *, row: int = 0, col: int = 0) -> None:
    """Feed plain text + CUP so cursor sits at (row,col) after paint."""
    # 1-based CUP
    cup = f"\x1b[{row + 1};{col + 1}H"
    sess.feed(text if row == 0 and col == 0 else f"\x1b[H{text}")
    # Re-position cursor for go/move tests
    sess.feed(cup)


def _mouse_on(sess: ScreenSession) -> None:
    """Make the peer announce xterm mouse tracking so SGR reports are legal."""
    sess.feed("\x1b[?1000h\x1b[?1006h")
    assert mouse_tracking_enabled(sess.screen)


# ---------------------------------------------------------------------------
# Frozen send-action type set.
# ---------------------------------------------------------------------------


def test_action_types_match_catalog() -> None:
    expected = {
        "nop",
        "key",
        "keys",
        "text",
        "paste",
        "raw",
        "go",
        "click",
        "move",
        "to_text",
        "submit",
        "clear_line",
        "interrupt",
        "eof",
        "escape",
        "resize",
        "wait",
    }
    assert ACTION_TYPES == expected
    assert set(ACTION_IMPL) == expected


def test_normalize_actions_empty() -> None:
    assert normalize_actions(None) == []
    assert normalize_actions([]) == []


def test_normalize_actions_string_sugar() -> None:
    assert normalize_actions("pwd") == [{"type": "text", "text": "pwd"}]


def test_normalize_actions_rejects_non_list() -> None:
    with pytest.raises(ActionError) as ei:
        normalize_actions({"type": "text"})
    assert ei.value.code == "INVALID_ARG"


def test_normalize_wait_defaults_for_actions() -> None:
    w = normalize_wait(None, actions_nonempty=True)
    assert w["until"] == "idle"
    assert w["idle_ms"] == 200
    assert w["timeout_ms"] == 30_000


def test_normalize_wait_defaults_for_empty() -> None:
    w = normalize_wait(None, actions_nonempty=False)
    assert w["until"] == "deadline"
    assert w["timeout_ms"] == 0


# ---------------------------------------------------------------------------
# Byte-level encoding (order-sensitive)
# ---------------------------------------------------------------------------


def test_encode_actions_order_text_submit() -> None:
    raw = encode_actions_bytes(
        [
            {"type": "text", "text": "pwd", "submit": True},
        ]
    )
    assert raw == encode_text("pwd") + encode_key("enter")


def test_encode_actions_multi_sequence_order() -> None:
    acts = [
        {"type": "keys", "keys": ["escape", "up"]},
        {"type": "text", "text": "x"},
        {"type": "submit"},
        {"type": "interrupt"},
    ]
    raw = encode_actions_bytes(acts)
    expected = (
        encode_key("escape")
        + encode_key("up")
        + encode_text("x")
        + encode_key("enter")
        + encode_key("ctrl+c")
    )
    assert raw == expected


def test_encode_actions_paste_before_submit() -> None:
    raw = encode_actions_bytes(
        [
            {"type": "paste", "text": "hello world"},
            {"type": "submit"},
        ]
    )
    assert raw == encode_paste("hello world", bracketed=False) + encode_key("enter")


def test_encode_actions_paste_submit_flag() -> None:
    raw = encode_actions_bytes(
        [{"type": "paste", "text": "hi", "submit": True}],
    )
    assert raw == encode_paste("hi", bracketed=False) + encode_key("enter")


def test_encode_actions_paste_bracketed_opt_in() -> None:
    """Pure byte encoding has no terminal state, so the wrapper is opt-in."""
    raw = encode_actions_bytes([{"type": "paste", "text": "hi", "bracketed": True}])
    assert raw == PASTE_START + b"hi" + PASTE_END
    raw_off = encode_actions_bytes([{"type": "paste", "text": "hi", "bracketed": False}])
    assert raw_off == b"hi"


def test_encode_actions_clear_line_eof() -> None:
    raw = encode_actions_bytes(
        [
            {"type": "clear_line"},
            {"type": "eof"},
        ]
    )
    assert raw == encode_key("ctrl+u") + encode_key("ctrl+d")


def test_encode_actions_raw_and_escape() -> None:
    raw = encode_actions_bytes(
        [
            {"type": "raw", "hex": "1b5b41"},
            {"type": "escape"},
        ]
    )
    assert raw == encode_raw_hex("1b5b41") + encode_key("escape")


def test_encode_actions_skips_session_dependent() -> None:
    raw = encode_actions_bytes(
        [
            {"type": "nop"},
            {"type": "go", "row": 1, "col": 1},
            {"type": "text", "text": "a"},
        ]
    )
    assert raw == encode_text("a")


# ---------------------------------------------------------------------------
# Peer codec: literal text / paste are written in the session's code page
# ---------------------------------------------------------------------------

# \u4e2d\u6587 as a cp936 console's code page writes it; utf-8 puts e4b8ade69687 on the
# wire instead, which that console renders as mojibake.
CJK_TEXT = "\u4e2d\u6587"
CJK_GBK = CJK_TEXT.encode("gbk")  # b'\xd6\xd0\xce\xc4'
# One character of the same text, for the single-character forms of key/keys:
# the base there is peer text too, so it follows the same codec.
CJK_CHAR = "\u4e2d"
CJK_CHAR_GBK = CJK_CHAR.encode("gbk")  # b'\xd6\xd0'


def _codec_session(text_encoding: str | None) -> tuple[ScreenSession, FakePty]:
    """Session pinned to *text_encoding*; None keeps the historic utf-8."""
    pty = FakePty(cols=80, rows=24)
    sess = ScreenSession(
        id="scr_codec",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        text_encoding=text_encoding,
    )
    return sess, pty


def test_encode_actions_bytes_takes_a_peer_codec() -> None:
    """The session-less encoder speaks utf-8 unless told the peer codec."""
    acts: list[dict[str, Any]] = [
        {"type": "text", "text": CJK_TEXT},
        {"type": "paste", "text": CJK_TEXT, "bracketed": True},
    ]
    assert encode_actions_bytes(acts) == CJK_TEXT.encode("utf-8") + (
        PASTE_START + CJK_TEXT.encode("utf-8") + PASTE_END
    )
    assert encode_actions_bytes(acts, codec="gbk") == CJK_GBK + (
        PASTE_START + CJK_GBK + PASTE_END
    )


def test_text_action_writes_the_session_codec() -> None:
    sess, pty = _codec_session("gbk")
    assert sess.text_codec == "gb18030"
    assert apply_action(sess, {"type": "text", "text": CJK_TEXT}) == "text"
    assert bytes(pty.written) == CJK_GBK
    assert CJK_TEXT.encode("utf-8") not in bytes(pty.written)


def test_paste_action_writes_the_session_codec() -> None:
    """The payload follows the peer codec; the delimiters stay ASCII bytes."""
    sess, pty = _codec_session("gbk")
    sess.feed(b"\x1b[?2004h")  # peer announced it parses the delimiters
    assert bracketed_paste_enabled(sess.screen)
    assert apply_action(sess, {"type": "paste", "text": CJK_TEXT}) == "paste"
    assert bytes(pty.written) == PASTE_START + CJK_GBK + PASTE_END

    pty.written.clear()
    apply_action(sess, {"type": "paste", "text": CJK_TEXT, "bracketed": False})
    assert bytes(pty.written) == CJK_GBK


def test_direct_write_agrees_with_the_text_action() -> None:
    """``ScreenSession.write(str)`` and the action path emit the same bytes."""
    for enc, expected in (("gbk", CJK_GBK), (None, CJK_TEXT.encode("utf-8"))):
        act_sess, act_pty = _codec_session(enc)
        apply_action(act_sess, {"type": "text", "text": CJK_TEXT, "submit": True})
        direct_sess, direct_pty = _codec_session(enc)
        direct_sess.write(CJK_TEXT)
        direct_sess.write(encode_key("enter"))
        assert bytes(direct_pty.written) == bytes(act_pty.written), enc
        assert bytes(direct_pty.written) == expected + encode_key("enter"), enc


def test_default_session_still_writes_utf8() -> None:
    """No configured codec: literal text keeps the historic utf-8 bytes."""
    sess, pty = _codec_session(None)
    assert sess.text_codec == "utf-8"
    assert apply_action(sess, {"type": "text", "text": CJK_TEXT}) == "text"
    assert bytes(pty.written) == CJK_TEXT.encode("utf-8")


def test_control_and_raw_bytes_ignore_the_session_codec() -> None:
    """Keys and hex payloads are bytes already: never re-encoded."""
    sess, pty = _codec_session("gbk")
    assert apply_action(sess, {"type": "key", "key": "ctrl+c"}) == "key"
    assert apply_action(sess, {"type": "raw", "hex": CJK_GBK.hex()}) == "raw"
    assert bytes(pty.written) == encode_key("ctrl+c") + CJK_GBK


def test_key_literal_characters_follow_the_session_codec() -> None:
    """A single-character key base is peer text, so it follows the codec.

    Plain, ``keys``, ``alt+`` and ``shift+`` bases are what the operator types;
    the ESC that prefixes an alt form stays a control byte.
    """
    sess, pty = _codec_session("gbk")
    assert apply_action(sess, {"type": "key", "key": CJK_CHAR}) == "key"
    assert bytes(pty.written) == CJK_CHAR_GBK

    pty.written.clear()
    assert apply_action(sess, {"type": "keys", "keys": [CJK_CHAR]}) == "keys"
    assert bytes(pty.written) == CJK_CHAR_GBK

    pty.written.clear()
    assert apply_action(sess, {"type": "key", "key": "alt+" + CJK_CHAR}) == "key"
    assert bytes(pty.written) == b"\x1b" + CJK_CHAR_GBK

    pty.written.clear()
    assert apply_action(sess, {"type": "key", "key": "shift+" + CJK_CHAR}) == "key"
    assert bytes(pty.written) == CJK_CHAR_GBK


def test_named_keys_stay_control_bytes_with_a_peer_codec() -> None:
    """Named keys are protocol bytes: a configured codec must not touch them."""
    sess, pty = _codec_session("gbk")
    assert encode_key("enter", codec="gbk") == b"\r"
    assert encode_keys(["enter", "up"], codec="gbk") == b"\r" + CSI + b"A"
    assert apply_action(sess, {"type": "key", "key": "enter"}) == "key"
    assert bytes(pty.written) == b"\r"


def test_key_literal_defaults_to_utf8_without_a_session_codec() -> None:
    """No configured codec: the key path keeps the historic utf-8 bytes."""
    sess, pty = _codec_session(None)
    assert apply_action(sess, {"type": "key", "key": CJK_CHAR}) == "key"
    assert bytes(pty.written) == CJK_CHAR.encode("utf-8")
    assert encode_key(CJK_CHAR) == CJK_CHAR.encode("utf-8")


def test_key_literal_unrepresentable_is_replaced_not_raised() -> None:
    """A codec that cannot map the character writes '?' for it, never raises."""
    sess, pty = _codec_session("latin-1")
    assert apply_action(sess, {"type": "key", "key": CJK_CHAR}) == "key"
    assert bytes(pty.written) == b"?"


def test_encode_actions_bytes_key_literals_follow_the_peer_codec() -> None:
    """The session-less encoder routes key/keys single characters via *codec*."""
    acts: list[dict[str, Any]] = [
        {"type": "key", "key": CJK_CHAR},
        {"type": "keys", "keys": ["enter", CJK_CHAR]},
    ]
    utf8_char = CJK_CHAR.encode("utf-8")
    assert encode_actions_bytes(acts) == utf8_char + b"\r" + utf8_char
    assert encode_actions_bytes(acts, codec="gbk") == (
        CJK_CHAR_GBK + b"\r" + CJK_CHAR_GBK
    )


def test_unrepresentable_text_is_replaced_not_raised() -> None:
    """A peer codec that cannot map a character writes '?' for it."""
    sess, pty = _codec_session("latin-1")
    assert sess.text_codec == "latin-1"
    assert apply_action(sess, {"type": "text", "text": CJK_TEXT}) == "text"
    assert bytes(pty.written) == b"??"


# ---------------------------------------------------------------------------
# apply_action via FakePty
# ---------------------------------------------------------------------------


def test_apply_nop_key_submit_interrupt() -> None:
    sess, pty = _session()
    assert apply_action(sess, {"type": "nop"}) == "nop"
    assert apply_action(sess, {"type": "key", "key": "enter"}) == "key"
    assert apply_action(sess, {"type": "submit"}) == "submit"
    assert apply_action(sess, {"type": "interrupt"}) == "interrupt"
    assert apply_action(sess, {"type": "eof"}) == "eof"
    assert apply_action(sess, {"type": "escape"}) == "escape"
    assert apply_action(sess, {"type": "clear_line"}) == "clear_line"
    assert bytes(pty.written) == (
        encode_key("enter")
        + encode_key("enter")
        + encode_key("ctrl+c")
        + encode_key("ctrl+d")
        + encode_key("escape")
        + encode_key("ctrl+u")
    )


def test_apply_go_keys_from_cursor() -> None:
    sess, pty = _session()
    # Place cursor at (10, 5)
    sess.feed("\x1b[11;6H")  # 1-based -> (10,5)
    assert sess.screen.cursor.y == 10
    assert sess.screen.cursor.x == 5
    name = apply_action(sess, {"type": "go", "row": 7, "col": 2, "how": "keys"})
    assert name == "go"
    # Row delta -3 -> 3 up; column delta -3 -> 3 left.
    expected = encode_keys(["up", "up", "up", "left", "left", "left"])
    assert bytes(pty.written) == expected


def test_apply_go_auto_uses_click_when_mouse_on() -> None:
    sess, pty = _session()
    sess.feed("\x1b[?1000h\x1b[?1006h")  # mouse tracking on
    assert mouse_tracking_enabled(sess.screen)
    apply_action(sess, {"type": "go", "row": 3, "col": 4, "how": "auto"})
    press = encode_mouse_sgr(3, 4, button="left", press=True)
    release = encode_mouse_sgr(3, 4, button="left", press=False)
    assert press in bytes(pty.written)
    assert release in bytes(pty.written)


def test_apply_go_auto_without_mouse_refuses_and_writes_nothing() -> None:
    """A bare go on a non-tracking peer has no implicit key fallback.

    The up-key is the line editor's own command: on a shell it recalls a
    history entry and the caller's next text is spliced into the recalled
    line, which reaches the caller as a plain success on the send that gets
    corrupted. Key steering stays reachable through an explicit ``how=keys``.
    """
    sess, pty = _session()
    sess.feed("\x1b[5;5H")  # cur (4,4)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "go", "row": 4, "col": 6, "how": "auto"})
    assert ei.value.code == "NAV_NO_TRACKING"
    assert "go how=keys" in ei.value.msg
    assert bytes(pty.written) == b"", (
        f"implicit go synthesized input without tracking: {bytes(pty.written)!r}"
    )


def test_apply_click_double() -> None:
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 2},
    )
    # 2 press+release pairs
    assert bytes(pty.written).count(b"M") == 2
    assert bytes(pty.written).count(b"m") == 2


def test_apply_click_triple_emits_three_pairs() -> None:
    """clicks=3 must emit 3 press+release pairs (was capped at 2)."""
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 3},
    )
    # 3 press+release pairs -> 3 'M' (press) + 3 'm' (release) final bytes.
    assert bytes(pty.written).count(b"M") == 3
    assert bytes(pty.written).count(b"m") == 3


def test_apply_click_single_emits_one_pair() -> None:
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 1},
    )
    assert bytes(pty.written).count(b"M") == 1
    assert bytes(pty.written).count(b"m") == 1


def test_apply_click_zero_emits_no_pairs() -> None:
    """clicks=0 must emit 0 press+release pairs (was 1 pair via the old
    `int(action.get("clicks") or 1)` which treated 0 as falsy)."""
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 0},
    )
    assert bytes(pty.written) == b""
    assert bytes(pty.written).count(b"M") == 0
    assert bytes(pty.written).count(b"m") == 0


def test_apply_click_zero_without_tracking_emits_nothing() -> None:
    """clicks=0 emits nothing on the keys branch too, not arrow-key movement.

    Zero is the documented "emit nothing" request; deciding the branch before
    reading *clicks* wrote direction keys into a line editor instead.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")
    assert not mouse_tracking_enabled(sess.screen)
    assert apply_action(sess, {"type": "click", "row": 2, "col": 3, "clicks": 0}) == "click"
    assert bytes(pty.written) == b""
    # go(how=click) carries the same field through to the click branch.
    assert (
        apply_action(
            sess, {"type": "go", "row": 2, "col": 3, "how": "click", "clicks": 0}
        )
        == "go"
    )
    assert bytes(pty.written) == b""


def test_apply_click_negative_emits_no_pairs() -> None:
    """Negative clicks clamp to 0 -> 0 press+release pairs."""
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": -3},
    )
    assert bytes(pty.written) == b""


def test_apply_click_null_clicks_defaults_to_one_pair() -> None:
    """clicks=None (JSON null) preserves the old default -> 1 pair."""
    sess, pty = _session()
    _mouse_on(sess)
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": None},
    )
    assert bytes(pty.written).count(b"M") == 1
    assert bytes(pty.written).count(b"m") == 1


def test_apply_click_without_tracking_refuses_and_writes_nothing() -> None:
    """No tracking -> neither SGR nor direction keys; refuse with a label.

    SGR corrupts a line editor, and the direction-key fallback is no better:
    the up-key recalls a history entry, so the caller's next text is spliced
    into the recalled line. Writing nothing is the only honest realization.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J")
    assert not mouse_tracking_enabled(sess.screen)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "click", "row": 2, "col": 3})
    assert ei.value.code == "NAV_NO_TRACKING"
    assert "go how=keys" in ei.value.msg
    assert bytes(pty.written) == b"", (
        f"implicit click synthesized input without tracking: {bytes(pty.written)!r}"
    )


def test_apply_go_click_without_tracking_refuses() -> None:
    """how=click on a non-tracking peer is refused, not silently degraded."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")  # cur (4,4)
    assert not mouse_tracking_enabled(sess.screen)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "go", "row": 4, "col": 6, "how": "click"})
    assert ei.value.code == "NAV_NO_TRACKING"
    assert bytes(pty.written) == b""


def test_apply_go_keys_without_tracking_still_steers() -> None:
    """An explicit how=keys request keeps working on a non-tracking peer."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")  # cur (4,4)
    assert not mouse_tracking_enabled(sess.screen)
    assert apply_action(sess, {"type": "go", "row": 4, "col": 6, "how": "keys"}) == "go"
    blob = bytes(pty.written)
    assert CSI + b"<" not in blob
    assert encode_key("right") in blob


def test_apply_click_force_click_writes_mouse_bytes_without_tracking() -> None:
    """force_click is the escape hatch when the peer's modes are not visible."""
    sess, pty = _session()
    assert not mouse_tracking_enabled(sess.screen)
    apply_action(sess, {"type": "click", "row": 1, "col": 2, "force_click": True})
    assert encode_mouse_sgr(1, 2, button="left", press=True) in bytes(pty.written)


@pytest.mark.parametrize(
    "announce",
    ["\x1b[?1000h", "\x1b[?1002h", "\x1b[?1003h", "\x1b[?1005h", "\x1b[?1015h"],
)
def test_apply_click_needs_the_sgr_mode_not_any_tracking_mode(
    announce: str,
) -> None:
    """Only DECSET 1006 announces the SGR encoding this module writes.

    1000/1002/1003 carry the legacy report layout and 1005/1015 their own
    extended encodings. A peer that announced one of those reads *some* mouse
    report, not ``ESC[<b;x;yM``: writing it inserts the parameters as literal
    text into its input line. The broad predicate still says the peer reads
    mouse reports - which is why the click guard must not use it.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed(announce)
    assert mouse_tracking_enabled(sess.screen), "premise: peer reads mouse reports"
    assert not sgr_mouse_enabled(sess.screen)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "click", "row": 2, "col": 3})
    assert ei.value.code == "NAV_NO_TRACKING"
    assert bytes(pty.written) == b"", (
        f"SGR written to a peer that announced {announce!r}: {bytes(pty.written)!r}"
    )


def test_apply_move_relative() -> None:
    sess, pty = _session()
    apply_action(sess, {"type": "move", "row_delta": -2, "col_delta": 3})
    expected = encode_keys(["up", "up", "right", "right", "right"])
    assert bytes(pty.written) == expected


def test_apply_go_capped() -> None:
    sess, _pty = _session()
    sess.feed("\x1b[1;1H")
    with pytest.raises(ActionError) as ei:
        apply_action(
            sess,
            {"type": "go", "row": 0, "col": GO_STEP_CAP + 5, "how": "keys"},
        )
    assert ei.value.code == "NAV_CAPPED"


def test_apply_to_text_finds_and_clicks() -> None:
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    pos = find_text(sess.screen, "Save")
    assert pos == (0, 6)
    name = apply_action(sess, {"type": "to_text", "text": "Save", "click": True})
    assert name == "to_text"
    press = encode_mouse_sgr(0, 6, button="left", press=True)
    assert press in bytes(pty.written)


def test_apply_to_text_not_found() -> None:
    sess, _pty = _session()
    sess.feed("\x1b[Honly plain text")
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "to_text", "text": "Save"})
    assert ei.value.code == "NAV_TEXT_NOT_FOUND"


def test_apply_to_text_nth() -> None:
    sess, pty = _session(cols=60, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[Hfoo bar foo baz")
    # second "foo"
    apply_action(sess, {"type": "to_text", "text": "foo", "nth": 2, "click": True})
    press = encode_mouse_sgr(0, 8, button="left", press=True)
    assert press in bytes(pty.written)


# ---------------------------------------------------------------------------
# to_text position: cell coordinates, not display-string indexes
# ---------------------------------------------------------------------------


def test_find_text_wide_char_before_match_returns_the_cell_column() -> None:
    """``\u4e2d\u6587Save``: "Save" is display index 2 but occupies cell column 4.

    ``screen.display`` renders one character per cell it painted *except* the
    stub cell a wide character spans, so a display index counts characters
    where a click report and a cursor move both address cells.
    """
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u6587Save")
    assert sess.screen.display[0].startswith("\u4e2d\u6587Save"), "premise: wide paint"
    assert find_text(sess.screen, "Save") == (0, 4)


def test_apply_to_text_click_uses_the_cell_column_after_a_wide_char() -> None:
    """The click report addresses the cell the text sits in, not its index."""
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u6587Save")
    assert apply_action(sess, {"type": "to_text", "text": "Save", "click": True}) == (
        "to_text"
    )
    blob = bytes(pty.written)
    assert encode_mouse_sgr(0, 4, button="left", press=True) in blob
    assert encode_mouse_sgr(0, 2, button="left", press=True) not in blob, (
        f"click landed on the display index instead of the cell: {blob!r}"
    )


def test_execute_send_to_text_click_lands_on_the_cell_after_a_wide_char() -> None:
    """End-to-end: the mapped cell reaches the peer through execute_send."""
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u6587Save")
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": True}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert out.nav == "ok"
    assert encode_mouse_sgr(0, 4, button="left", press=True) in bytes(pty.written)


def test_apply_to_text_keys_walk_to_the_cell_after_a_wide_char() -> None:
    """click=false walks cells: two wide characters cost four right keys."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u6587Save")
    sess.feed("\x1b[1;1H")  # cursor to (0,0)
    assert not mouse_tracking_enabled(sess.screen)
    assert apply_action(sess, {"type": "to_text", "text": "Save", "click": False}) == (
        "to_text"
    )
    assert bytes(pty.written) == encode_keys(["right"] * 4)


def test_find_text_nth_limits_count_in_cell_coordinates() -> None:
    """nth selects among matches whose columns are cells, not indexes."""
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2dfoo\u4e2dfoo")
    assert sess.screen.display[0].startswith("\u4e2dfoo\u4e2dfoo"), "premise: wide paint"
    assert find_text(sess.screen, "foo") == (0, 2)
    assert find_text(sess.screen, "foo", nth=2) == (0, 7)
    assert find_text(sess.screen, "foo", nth=3) is None


def test_find_text_row_limit_keeps_cell_columns() -> None:
    """The row limit narrows the search; a match below it keeps its cell."""
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u6587Save")
    sess.feed("\x1b[4;1Hplain Save here")
    assert find_text(sess.screen, "Save", row=0) == (0, 4)
    assert find_text(sess.screen, "Save", row=3) == (3, 6)
    assert find_text(sess.screen, "Save", row=1) is None
    assert find_text(sess.screen, "Save", nth=2) == (3, 6)


def test_find_text_col_limit_is_a_cell_column() -> None:
    """col starts the search at a cell, so a match before it is skipped.

    The row is ``\u4e2dSave \u4e2dSave``: the first "Save" starts at cell 2 and the
    second at cell 9. A col of 3 must skip the first and report the second.
    """
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2dSave \u4e2dSave")
    assert find_text(sess.screen, "Save", row=0, col=0) == (0, 2)
    assert find_text(sess.screen, "Save", row=0, col=3) == (0, 9)
    assert find_text(sess.screen, "Save", row=0, col=10) is None


def test_find_text_ascii_line_is_unchanged() -> None:
    """A line with no wide character keeps index and cell identical."""
    sess, _pty = _session(cols=60, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert find_text(sess.screen, "Save") == (0, 6)
    assert find_text(sess.screen, "Save", row=0, col=3) == (0, 6)
    assert find_text(sess.screen, "Save", row=0, col=7) is None
    assert find_text(sess.screen, "o", nth=2) == (0, 12)


def test_find_text_combining_character_stays_with_its_base_cell() -> None:
    """A combining mark is normalized into its base cell, adding no column."""
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jcafe\u0301 Save")
    assert find_text(sess.screen, "caf\u00e9") == (0, 0)
    assert find_text(sess.screen, "Save") == (0, 5)


def test_find_text_combining_mark_on_a_wide_stub_adds_no_column() -> None:
    """A mark merged into a wide character's stub is not rendered at all.

    pyte paints the stub of a wide character with empty data and a combining
    mark merged into it stays there, out of the display row - so the row is
    one character shorter than the cells it spans and the match below it must
    still report the cell it was painted in.
    """
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u4e2d\u0301Save")
    assert sess.screen.display[0].startswith("\u4e2dSave"), "premise: stub dropped"
    assert find_text(sess.screen, "Save") == (0, 2)


def test_find_text_ignores_a_char_written_over_a_wide_char_stub() -> None:
    """A wide char's stub is dropped whatever its data says.

    ``\u4e2d`` spans cells 0-1; the ``a`` written at cell 1 is never rendered, so
    the visible run of four ``a``s starts at cell 2. Its first character is
    therefore display index 1 but cell 2 - and the click must address cell 2,
    not the dropped stub the display string has no character for.
    """
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2J\u4e2d\x1b[1;2Ha\x1b[1;3HaaaaSave")
    assert sess.screen.buffer[0][1].data == "a", "premise: stub cell overwritten"
    assert sess.screen.display[0].startswith("\u4e2daaaaSave"), "premise: stub dropped"
    assert find_text(sess.screen, "a") == (0, 2)
    assert find_text(sess.screen, "aa") == (0, 2)
    assert apply_action(sess, {"type": "to_text", "text": "aa", "click": True}) == (
        "to_text"
    )
    blob = bytes(pty.written)
    assert encode_mouse_sgr(0, 2, button="left", press=True) in blob
    assert encode_mouse_sgr(0, 1, button="left", press=True) not in blob, (
        f"click landed on the dropped stub cell instead of the visible text: {blob!r}"
    )


def test_find_text_keys_skip_an_erased_wide_char_stub() -> None:
    """ESC[X blanks a wide char's stub, which the display row drops.

    Cells 6-9 of the row are blanks but only cells 7-9 reach the display, so
    the `` a`` below them starts at display index 7 yet occupies cells 9-10:
    the key walk must count nine cells, not the seven of its index.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J\u5b57aaa\u4e2d \u5b57a  ba\u5b57\x1b[1;7H\x1b[4X\x1b[2;5H\x1b[K")
    sess.feed("\x1b[1;1H")  # cursor to (0,0)
    assert sess.screen.buffer[0][6].data == " ", "premise: stub erased to a blank"
    assert sess.screen.display[0].startswith("\u5b57aaa\u4e2d   a  ba\u5b57"), (
        "premise: the erased stub is not rendered"
    )
    assert find_text(sess.screen, " a") == (0, 9)
    assert not mouse_tracking_enabled(sess.screen)
    assert apply_action(sess, {"type": "to_text", "text": " a", "click": False}) == (
        "to_text"
    )
    assert bytes(pty.written) == encode_keys(["right"] * 9)


def test_apply_to_text_keys_fallback_nav_capped_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click path without mouse tracking must not swallow keys ActionError."""
    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=40, rows=10)
    # No mouse tracking -> the only key-steering path is the explicit click=false.
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert not mouse_tracking_enabled(sess.screen)

    def _boom(_session: Any, _r: int, _c: int) -> None:
        raise ActionError("NAV_CAPPED", "go keys would need 999 steps (cap 200)")

    monkeypatch.setattr(send_mod, "_go_by_keys", _boom)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "to_text", "text": "Save", "click": False})
    assert ei.value.code == "NAV_CAPPED"
    # No SGR report either: the peer never enabled mouse tracking.
    assert CSI + b"<" not in bytes(pty.written)


def test_execute_send_to_text_keys_fallback_fail_nav_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys fallback failure -> status=error, nav=capped (not silent ok)."""
    from mcp_remote_control.screen import send as send_mod

    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J[ Save ] button")

    def _boom(_session: Any, _r: int, _c: int) -> None:
        raise ActionError("NAV_CAPPED", "go keys would need 999 steps (cap 200)")

    monkeypatch.setattr(send_mod, "_go_by_keys", _boom)
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": False}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.error_code == "NAV_CAPPED"
    assert out.nav == "capped"
    assert "to_text" not in out.did


def test_apply_to_text_force_click_skips_keys_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force_click uses SGR only; no key path and no refusal."""
    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert not mouse_tracking_enabled(sess.screen)

    def _boom(_session: Any, _r: int, _c: int) -> None:
        raise AssertionError("_go_by_keys must not run when force_click is set")

    monkeypatch.setattr(send_mod, "_go_by_keys", _boom)
    name = apply_action(
        sess,
        {"type": "to_text", "text": "Save", "click": True, "force_click": True},
    )
    assert name == "to_text"
    press = encode_mouse_sgr(0, 6, button="left", press=True)
    assert press in bytes(pty.written)


def test_apply_to_text_mouse_tracking_skips_keys_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mouse tracking on -> click only, no keys fallback."""
    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[?1000h\x1b[?1006h\x1b[H\x1b[2Jhello Save world")
    assert mouse_tracking_enabled(sess.screen)

    def _boom(_session: Any, _r: int, _c: int) -> None:
        raise AssertionError("_go_by_keys must not run when mouse tracking is on")

    monkeypatch.setattr(send_mod, "_go_by_keys", _boom)
    name = apply_action(sess, {"type": "to_text", "text": "Save", "click": True})
    assert name == "to_text"
    press = encode_mouse_sgr(0, 6, button="left", press=True)
    assert press in bytes(pty.written)


def test_apply_to_text_implicit_click_refused_without_tracking() -> None:
    """click not given (defaults true) + no tracking -> nothing written."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert not mouse_tracking_enabled(sess.screen)
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "to_text", "text": "Save"})
    assert ei.value.code == "NAV_NO_TRACKING"
    assert "go how=keys" in ei.value.msg
    assert bytes(pty.written) == b""


def test_apply_to_text_click_false_steers_with_keys() -> None:
    """click=false is the explicit key-steering request; it survives."""
    sess, pty = _session(cols=40, rows=10)
    # After paint, cursor sits past the text; keys approach with left/right.
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert not mouse_tracking_enabled(sess.screen)
    name = apply_action(sess, {"type": "to_text", "text": "Save", "click": False})
    assert name == "to_text"
    blob = bytes(pty.written)
    assert CSI + b"<" not in blob
    assert (
        encode_key("left") in blob
        or encode_key("right") in blob
        or encode_key("up") in blob
        or encode_key("down") in blob
    )


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", False, 0, 0.0])
def test_apply_to_text_click_false_spellings_steer_with_keys(value: Any) -> None:
    """Every spelled-out false value is an explicit key-steering opt-in."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    name = apply_action(sess, {"type": "to_text", "text": "Save", "click": value})
    assert name == "to_text"
    blob = bytes(pty.written)
    assert CSI + b"<" not in blob
    assert encode_key("left") in blob or encode_key("right") in blob


@pytest.mark.parametrize("value", ["auto", "", "maybe", "y", "2", "click"])
def test_apply_to_text_unknown_click_value_is_rejected(value: Any) -> None:
    """An unrecognised click value is an argument error, never a silent "no".

    ``action_truthy`` is False for every string outside its truthy set, so
    reading an unknown value as "not a click" would select the key-steering
    branch - writing the line editor's own up/left keys on a peer with no
    mouse tracking, which is exactly the corruption the click/keys split
    exists to prevent. The caller asked for a click with a hint this tool does
    not know, so it must be told.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    with pytest.raises(ActionError) as ei:
        apply_action(sess, {"type": "to_text", "text": "Save", "click": value})
    assert ei.value.code == "INVALID_ARG"
    assert "click" in ei.value.msg
    assert bytes(pty.written) == b"", (
        f"unknown click value {value!r} wrote bytes: {bytes(pty.written)!r}"
    )


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", True, 1, 2])
def test_apply_to_text_true_click_spellings_click(value: Any) -> None:
    """A spelled-out true value keeps the click realization."""
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    name = apply_action(sess, {"type": "to_text", "text": "Save", "click": value})
    assert name == "to_text"
    assert CSI + b"<" in bytes(pty.written)


def test_execute_send_to_text_unknown_click_reports_invalid_arg() -> None:
    """End-to-end: the rejection reaches the caller as an error, not nav=keys."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": "auto"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.error_code == "INVALID_ARG"
    assert out.nav is None
    assert bytes(pty.written) == b""


def test_apply_resize() -> None:
    sess, pty = _session(cols=120, rows=40)
    name = apply_action(sess, {"type": "resize", "cols": 140, "rows": 50})
    assert name == "resize"
    assert sess.cols == 140
    assert sess.rows == 50
    assert pty.cols == 140


def test_apply_wait_mid_sequence() -> None:
    sess, _pty = _session()
    assert apply_action(sess, {"type": "wait", "ms": 10}) == "wait"


# ---------------------------------------------------------------------------
# paste: bracketed wrapper follows the receiving program's DECSET 2004 state
# ---------------------------------------------------------------------------


def test_bracketed_paste_enabled_reads_decset_2004() -> None:
    sess, _pty = _session()
    assert bracketed_paste_enabled(sess.screen) is False
    sess.feed("\x1b[?2004h")
    assert bracketed_paste_enabled(sess.screen) is True
    sess.feed("\x1b[?2004l")
    assert bracketed_paste_enabled(sess.screen) is False


def test_bracketed_paste_enabled_tolerates_object_without_mode() -> None:
    """Missing / empty mode set is 'not supported', never an exception."""

    class NoModes:
        pass

    assert bracketed_paste_enabled(NoModes()) is False


def test_apply_paste_unwrapped_without_decset_2004() -> None:
    """No 2004 -> raw payload; the ESC[200~ wrapper would be eaten by the peer."""
    sess, pty = _session()
    assert bracketed_paste_enabled(sess.screen) is False
    assert apply_action(sess, {"type": "paste", "text": "echo hi"}) == "paste"
    assert bytes(pty.written) == b"echo hi"


def test_apply_paste_wrapped_with_decset_2004() -> None:
    """2004 on -> the peer parses the delimiters, so the wrapper is used."""
    sess, pty = _session()
    sess.feed("\x1b[?2004h")
    apply_action(sess, {"type": "paste", "text": "echo hi"})
    assert bytes(pty.written) == PASTE_START + b"echo hi" + PASTE_END


def test_apply_paste_wrapped_after_decset_2004_reset() -> None:
    """A peer that turns bracketed paste back off must get raw bytes again."""
    sess, pty = _session()
    sess.feed("\x1b[?2004h")
    sess.feed("\x1b[?2004l")
    apply_action(sess, {"type": "paste", "text": "raw"})
    assert bytes(pty.written) == b"raw"


def test_apply_paste_explicit_bracketed_overrides_screen_state() -> None:
    """An explicit bracketed= field wins in both directions (escape hatch)."""
    sess, pty = _session()
    # Screen says no 2004; caller insists on wrapping.
    apply_action(sess, {"type": "paste", "text": "a", "bracketed": True})
    assert bytes(pty.written) == PASTE_START + b"a" + PASTE_END

    pty.written.clear()
    sess.feed("\x1b[?2004h")
    # Screen says 2004 on; caller insists on raw.
    apply_action(sess, {"type": "paste", "text": "b", "bracketed": False})
    assert bytes(pty.written) == b"b"


def test_resolve_bracketed_paste_uses_screen_when_field_absent() -> None:
    sess, _pty = _session()
    assert resolve_bracketed_paste(sess, {"type": "paste", "text": "x"}) is False
    sess.feed("\x1b[?2004h")
    assert resolve_bracketed_paste(sess, {"type": "paste", "text": "x"}) is True


def test_apply_paste_multiline_unwrapped_without_2004() -> None:
    """Multi-line payload without 2004 is delivered as typed, not wrapped."""
    sess, pty = _session()
    apply_action(sess, {"type": "paste", "text": "a\nb"})
    assert bytes(pty.written) == b"a\nb"
    assert PASTE_START not in bytes(pty.written)
    assert PASTE_END not in bytes(pty.written)


def test_execute_send_to_text_nav_meta() -> None:
    """A click the peer can receive is still nav=ok (SGR path unchanged)."""
    sess, _pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    sess.feed("\x1b[H\x1b[2J[ Save ] button")
    # Disable probe (FakePty has no real shell).
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": True}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.nav == "ok"
    assert "to_text" in out.did


def test_execute_send_implicit_click_without_tracking_nav_no_tracking() -> None:
    """Implicit click with no tracking -> status=error, nav=no_tracking + hint."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J[ Save ] button")
    assert not mouse_tracking_enabled(sess.screen)
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.nav == "no_tracking"
    assert out.error_code == "NAV_NO_TRACKING"
    # The hint must be actionable: name the explicit key-steering escape.
    assert "go how=keys" in (out.error_msg or "")
    assert bytes(pty.written) == b"", (
        f"implicit click synthesized input without tracking: {bytes(pty.written)!r}"
    )


def test_execute_send_click_action_without_tracking_nav_no_tracking() -> None:
    """The bare click action is refused the same way, and writes nothing."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2J")
    out = execute_send(
        sess,
        actions=[{"type": "click", "row": 2, "col": 3}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.nav == "no_tracking"
    assert out.error_code == "NAV_NO_TRACKING"
    assert bytes(pty.written) == b""


def test_execute_send_to_text_click_false_nav_labelled_keys() -> None:
    """to_text(click=false) on a non-tracking peer steers and labels it keys."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[H\x1b[2Jhello Save world")
    assert not mouse_tracking_enabled(sess.screen)
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": False}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert out.nav == "keys"
    assert out.did == ["to_text"]
    blob = bytes(pty.written)
    assert CSI + b"<" not in blob
    assert encode_key("left") in blob or encode_key("right") in blob


def test_execute_send_go_keys_nav_labelled_keys() -> None:
    """An explicit go(how=keys) still steers, and says so in nav."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")  # cur (4,4)
    out = execute_send(
        sess,
        actions=[{"type": "go", "row": 4, "col": 6, "how": "keys"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert out.nav == "keys"
    assert encode_key("right") in bytes(pty.written)


def test_execute_send_go_auto_without_tracking_refuses() -> None:
    """go(how=auto) on a non-tracking peer refuses rather than typing keys.

    Same rule as the implicit click: no action falls back to direction keys.
    The caller reads nav=no_tracking and the hint names go how=keys.
    """
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")  # cur (4,4)
    assert not mouse_tracking_enabled(sess.screen)
    out = execute_send(
        sess,
        actions=[{"type": "go", "row": 4, "col": 6}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.nav == "no_tracking"
    assert out.error_code == "NAV_NO_TRACKING"
    assert "go how=keys" in (out.error_msg or "")
    assert bytes(pty.written) == b""


def test_execute_send_go_click_with_tracking_nav_ok() -> None:
    """Tracking on -> go(how=auto) clicks and keeps nav=ok."""
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    out = execute_send(
        sess,
        actions=[{"type": "go", "row": 4, "col": 6}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert out.nav == "ok"
    assert encode_mouse_sgr(4, 6, button="left", press=True) in bytes(pty.written)


def test_execute_send_move_nav_labelled_keys() -> None:
    """move emits direction keys, so it is labelled keys rather than ok."""
    sess, _pty = _session(cols=40, rows=10)
    out = execute_send(
        sess,
        actions=[{"type": "move", "row_delta": 0, "col_delta": 1}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.nav == "keys"


def test_execute_send_move_zero_delta_writes_nothing_and_no_nav() -> None:
    """A zero-delta move steers nothing, so it must not claim nav=keys.

    nav=keys tells the caller the cursor was steered; an Agent that branches on
    it would proceed against an unmoved cursor. Nothing was written, so there is
    no realization to label.
    """
    sess, pty = _session(cols=40, rows=10)
    out = execute_send(
        sess,
        actions=[{"type": "move", "row_delta": 0, "col_delta": 0}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert bytes(pty.written) == b""
    assert out.nav is None, f"zero-delta move claimed nav={out.nav!r}"


def test_execute_send_go_keys_at_cursor_writes_nothing_and_no_nav() -> None:
    """Sibling of the zero-delta move: go to the cell the cursor already holds."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[5;5H")  # cur (4,4)
    out = execute_send(
        sess,
        actions=[{"type": "go", "row": 4, "col": 4, "how": "keys"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert bytes(pty.written) == b""
    assert out.nav is None, f"zero-step go claimed nav={out.nav!r}"


def test_execute_send_to_text_click_false_at_cursor_writes_nothing_and_no_nav() -> None:
    """Same sibling through to_text: the found cell is under the cursor."""
    sess, pty = _session(cols=40, rows=10)
    # Text starts at (0,0); park the cursor on it before the search.
    sess.feed("\x1b[H\x1b[2JSave here")
    sess.feed("\x1b[1;1H")
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Save", "click": False}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "ok"
    assert bytes(pty.written) == b""
    assert out.nav is None, f"zero-step to_text claimed nav={out.nav!r}"


def test_encode_mouse_sgr_rejects_unknown_button() -> None:
    """An unknown button name must not be delivered as a left click.

    The button table defaulted to 0, so a wheel name or a typo reached the peer
    as a left press+release at that cell - activating whatever control is under
    it - while the send reported ok.
    """
    with pytest.raises(KeyEncodeError) as ei:
        encode_mouse_sgr(3, 4, button="wheelup", press=True)
    assert "wheel" in str(ei.value)
    # The accepted set still encodes.
    assert encode_mouse_sgr(3, 4, button="right", press=True) == b"\x1b[<2;5;4M"


@pytest.mark.parametrize("button", ["wheelup", "wheel_up", "scroll", "4", "Left "])
def test_execute_send_unknown_button_writes_nothing(button: str) -> None:
    sess, pty = _session(cols=40, rows=10)
    _mouse_on(sess)
    out = execute_send(
        sess,
        actions=[{"type": "click", "row": 3, "col": 4, "button": button}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.error_code == "INVALID_ARG"
    assert bytes(pty.written) == b"", (
        f"unknown button {button!r} delivered bytes: {bytes(pty.written)!r}"
    )


def test_execute_send_to_text_not_found_nav() -> None:
    sess, _pty = _session()
    sess.feed("\x1b[Hzzz")
    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Nope"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
    )
    assert out.status == "error"
    assert out.nav == "text_not_found"
    assert out.error_code == "NAV_TEXT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Action-loop error must not run full wait (30s) or inject cwd probe
# ---------------------------------------------------------------------------


def test_execute_send_action_error_skips_wait_and_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """NAV_TEXT_NOT_FOUND mid-loop: return well under default idle 30s; no probe."""
    import time

    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")
    probe_calls: list[Any] = []

    def _spy_probe(session: Any, acts: Any, *, probe: bool = True) -> str | None:
        probe_calls.append({"probe": probe, "acts": list(acts)})
        return session.cwd

    monkeypatch.setattr(send_mod, "update_cwd_after_send", _spy_probe)

    # Default wait is until=idle, timeout_ms=30_000 - must not run full window.
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[
            {"type": "text", "text": "echo hi"},
            {"type": "to_text", "text": "NoSuchNavTarget"},
            {"type": "text", "text": "should_not_run"},
        ],
        wait=None,
        probe_cwd=True,
        shot=True,
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"error path hung for {elapsed:.2f}s (expected << 30s wait)"
    assert out.status == "error"
    assert out.error_code == "NAV_TEXT_NOT_FOUND"
    assert out.nav == "text_not_found"
    assert "text" in out.did
    assert "to_text" not in out.did
    assert out.wait_until is None
    # Probe must not run on the terminal action-error path.
    assert probe_calls == []
    blob = bytes(pty.written)
    assert b"__MRC_PWD__" not in blob
    assert encode_key("ctrl+u") not in blob
    # Later actions after the failed to_text must not have been applied.
    assert b"should_not_run" not in blob


def test_execute_send_action_error_skips_wait_after_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait_after must not be invoked when the action loop already failed."""
    from mcp_remote_control.screen import send as send_mod

    sess, _pty = _session()
    sess.feed("\x1b[Hzzz")
    wait_calls: list[Any] = []

    def _spy_wait(session: Any, wait: Any) -> str:
        wait_calls.append(dict(wait))
        return "idle"

    monkeypatch.setattr(send_mod, "wait_after", _spy_wait)
    monkeypatch.setattr(
        send_mod,
        "update_cwd_after_send",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )

    out = execute_send(
        sess,
        actions=[{"type": "to_text", "text": "Nope"}],
        wait={"until": "idle", "idle_ms": 200, "timeout_ms": 30_000},
        probe_cwd=True,
        shot=False,
    )
    assert out.status == "error"
    assert out.error_code == "NAV_TEXT_NOT_FOUND"
    assert wait_calls == []
    assert out.wait_until is None


def test_execute_send_empty_unchanged_omits_frame() -> None:
    """Empty / nop-only send with stable hash -> status=unchanged, no body."""
    sess, _pty = _session(cols=40, rows=10)
    sess.feed("\x1b[Hprompt$ ")
    first = execute_send(
        sess,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
        shot=True,
    )
    assert first.status in ("ok", "unchanged")
    assert first.hash

    second = execute_send(
        sess,
        actions=[{"type": "nop"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,  # must not inject probe on nop-only
        shot=True,
    )
    assert second.status == "unchanged", (
        f"expected unchanged, got {second.status} hash={second.hash} "
        f"pre={second.pre_hash}"
    )
    assert second.frame is None
    assert second.hash == first.hash


def test_execute_send_all_simple_actions_did_order() -> None:
    sess, pty = _session()
    acts = [
        {"type": "nop"},
        {"type": "text", "text": "x"},
        {"type": "key", "key": "tab"},
        {"type": "keys", "keys": ["up"]},
        {"type": "paste", "text": "p"},
        {"type": "raw", "hex": "41"},
        {"type": "submit"},
        {"type": "clear_line"},
        {"type": "interrupt"},
        {"type": "eof"},
        {"type": "escape"},
        {"type": "move", "row_delta": 0, "col_delta": 0},
        {"type": "wait", "ms": 0},
    ]
    out = execute_send(
        sess,
        actions=acts,
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
        shot=False,
    )
    assert out.status == "ok"
    assert out.did == [
        "nop",
        "text",
        "key",
        "keys",
        "paste",
        "raw",
        "submit",
        "clear_line",
        "interrupt",
        "eof",
        "escape",
        "move",
        "wait",
    ]
    # Written bytes non-empty for key-like actions
    assert len(pty.written) > 0


# ---------------------------------------------------------------------------
# Cross-chunk UTF-8 decode
# ---------------------------------------------------------------------------


def test_feed_cross_chunk_utf8_preserves_multibyte_char() -> None:
    """A 3-byte UTF-8 char split across two feed() calls decodes as one char."""
    sess, _pty = _session(cols=40, rows=5)
    # "\u4e2d" = U+4E2D = b'\xe4\xb8\xad' (3 bytes); split across the read boundary.
    sess.feed(b"\xe4\xb8")
    sess.feed(b"\xad")
    frame = dump_frame(sess.screen)
    assert "\u4e2d" in frame
    # No U+FFFD from a per-chunk replace decode.
    assert "\ufffd" not in frame


def test_feed_cross_chunk_utf8_4byte_emoji() -> None:
    """A 4-byte emoji split across chunks decodes correctly."""
    sess, _pty = _session(cols=40, rows=5)
    # "\U0001f600" = U+1F600 = b'\xf0\x9f\x98\x80'
    sess.feed(b"\xf0\x9f")
    sess.feed(b"\x98\x80")
    frame = dump_frame(sess.screen)
    assert "\U0001f600" in frame
    assert "\ufffd" not in frame


def test_feed_str_passes_through_unchanged() -> None:
    sess, _pty = _session(cols=40, rows=5)
    sess.feed("\u4e2d")
    assert "\u4e2d" in dump_frame(sess.screen)


# ---------------------------------------------------------------------------
# wait/resize sends are non-intrusive (skip cwd probe, allow unchanged)
# ---------------------------------------------------------------------------


def test_is_noop_actions_wait_and_resize() -> None:
    assert _is_noop_actions([]) is True
    assert _is_noop_actions([{"type": "nop"}]) is True
    assert _is_noop_actions([{"type": "wait", "ms": 500}]) is True
    assert _is_noop_actions([{"type": "resize", "cols": 100, "rows": 30}]) is True
    assert _is_noop_actions(
        [{"type": "nop"}, {"type": "wait", "ms": 5}, {"type": "resize"}]
    ) is True
    # Any meaningful input action is not a pure noop (probe eligibility is
    # further gated by _actions_have_submit for partial-line safety).
    assert _is_noop_actions([{"type": "text", "text": "x"}]) is False
    assert _is_noop_actions([{"type": "key", "key": "enter"}]) is False
    assert _is_noop_actions(
        [{"type": "wait", "ms": 5}, {"type": "text", "text": "x"}]
    ) is False


def test_actions_have_submit_helper() -> None:
    """Submit detection gates cwd probe after non-noop sends."""
    assert _actions_have_submit(None) is False
    assert _actions_have_submit([]) is False
    assert _actions_have_submit([{"type": "text", "text": "partial"}]) is False
    assert _actions_have_submit(
        [{"type": "text", "text": "partial", "submit": False}]
    ) is False
    assert _actions_have_submit(
        [{"type": "text", "text": "ls", "submit": True}]
    ) is True
    assert _actions_have_submit([{"type": "submit"}]) is True
    assert _actions_have_submit([{"type": "key", "key": "enter"}]) is True
    assert _actions_have_submit([{"type": "key", "key": "return"}]) is True
    assert _actions_have_submit([{"type": "keys", "keys": ["up", "enter"]}]) is True
    assert _actions_have_submit(
        [{"type": "paste", "text": "x", "submit": True}]
    ) is True
    assert _actions_have_submit([{"type": "key", "key": "a"}]) is False
    assert _actions_have_submit([{"type": "clear_line"}]) is False
    # Mixed: text without submit + later submit still counts.
    assert _actions_have_submit(
        [
            {"type": "text", "text": "echo hi"},
            {"type": "submit"},
        ]
    ) is True
    # Mid-list submit then uncommitted text is not a last-action commit.
    assert _actions_have_submit(
        [
            {"type": "submit"},
            {"type": "text", "text": "partial"},
        ]
    ) is False
    assert _actions_have_submit(
        [
            {"type": "text", "text": "ls", "submit": True},
            {"type": "text", "text": "more"},
        ]
    ) is False
    # Last key of a keys chord is the landed input (enter mid-chord is not).
    assert _actions_have_submit([{"type": "keys", "keys": ["enter", "up"]}]) is False
    # Trailing wait/nop/resize do not cancel a preceding commit.
    assert _actions_have_submit(
        [
            {"type": "text", "text": "ls", "submit": True},
            {"type": "wait", "ms": 10},
        ]
    ) is True


def test_execute_send_wait_only_skips_cwd_probe_and_unchanged() -> None:
    """A wait-only send writes no ctrl+u probe and can still be unchanged."""
    sess, pty = _session(cols=40, rows=10)
    sess.feed("\x1b[Hprompt$ ")
    first = execute_send(
        sess,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    assert first.status in ("ok", "unchanged")
    pty.written.clear()
    second = execute_send(
        sess,
        actions=[{"type": "wait", "ms": 5}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    # No ctrl+u (clear_line) bytes written by a wait-only send.
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert second.status == "unchanged", (
        f"expected unchanged, got {second.status} hash={second.hash} "
        f"pre={second.pre_hash}"
    )
    assert second.frame is None


# ---------------------------------------------------------------------------
# Password / confirm prompts must not receive silent cwd probe inject
# ---------------------------------------------------------------------------


def test_execute_send_skips_cwd_probe_on_password_prompt() -> None:
    """After sudo-style password paint, send must not inject ctrl+u + probe."""
    sess, pty = _session(cols=80, rows=24)
    # Simulate post-send settle at a password prompt (e.g. sudo apt).
    sess.feed("\x1b[H[sudo] password for agent: ")
    out = execute_send(
        sess,
        actions=[{"type": "text", "text": "secret", "submit": True}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    assert out.status in ("ok", "unchanged", "error")
    # Action text+enter may write, but the probe's leading ctrl+u must not
    # appear as a post-action clear (FakePty records all writes). With
    # probe skipped, written should be only the text action bytes - no
    # second ctrl+u from silent_pwd_probe, and no __MRC_PWD__ command.
    blob = bytes(pty.written)
    assert b"__MRC_PWD__" not in blob
    # text "secret" + enter only; probe would also write a long printf/echo.
    assert b"printf" not in blob
    assert b"echo " not in blob
    # ctrl+u only if clear_line action - not present here.
    assert encode_key("ctrl+u") not in blob


def test_execute_send_skips_cwd_probe_on_yes_no_confirm() -> None:
    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[HDo you want to continue? [Y/n] ")
    execute_send(
        sess,
        actions=[{"type": "text", "text": "y", "submit": True}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    blob = bytes(pty.written)
    assert encode_key("ctrl+u") not in blob
    assert b"__MRC_PWD__" not in blob
    assert b"printf" not in blob


def test_execute_send_resize_only_skips_cwd_probe() -> None:
    """A resize-only send writes no ctrl+u probe."""
    sess, pty = _session(cols=120, rows=40)
    sess.feed("\x1b[Hprompt$ ")
    execute_send(
        sess,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    pty.written.clear()
    # 140x50 is above MIN_COLS/MIN_ROWS so clamp_geometry leaves it intact.
    execute_send(
        sess,
        actions=[{"type": "resize", "cols": 140, "rows": 50}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert sess.cols == 140
    assert sess.rows == 50


# ---------------------------------------------------------------------------
# text-without-submit must not lose uncommitted input via probe clear_line
# ---------------------------------------------------------------------------


def test_execute_send_text_without_submit_skips_cwd_probe_ctrl_u() -> None:
    """Half-line text send: no silent_pwd_probe ctrl+u after the typed bytes."""
    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")
    out = execute_send(
        sess,
        actions=[{"type": "text", "text": "echo half"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    assert out.status in ("ok", "unchanged", "error")
    blob = bytes(pty.written)
    # Action wrote the typed text.
    assert b"echo half" in blob
    # Probe must not run: no second-stage clear_line / probe command.
    assert encode_key("ctrl+u") not in blob
    assert b"__MRC_PWD__" not in blob
    assert b"printf" not in blob


def test_execute_send_two_half_line_texts_preserve_typed_content() -> None:
    """Half-line then another text send does not wipe via probe."""
    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")
    first = execute_send(
        sess,
        actions=[{"type": "text", "text": "echo "}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    assert first.status in ("ok", "unchanged", "error")
    mid = bytes(pty.written)
    assert b"echo " in mid
    assert encode_key("ctrl+u") not in mid

    pty.written.clear()
    second = execute_send(
        sess,
        actions=[{"type": "text", "text": "more"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    assert second.status in ("ok", "unchanged", "error")
    blob = bytes(pty.written)
    assert b"more" in blob
    # Second send must also leave the (still uncommitted) buffer alone.
    assert encode_key("ctrl+u") not in blob
    assert b"__MRC_PWD__" not in blob
    assert b"printf" not in blob


def test_execute_send_text_with_submit_may_still_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After submit, update_cwd_after_send is still invoked when surface is safe."""
    from mcp_remote_control.screen import send as send_mod

    sess, _pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")
    probe_calls: list[Any] = []

    def _spy_probe(session: Any, acts: Any, *, probe: bool = True) -> str | None:
        probe_calls.append({"probe": probe, "acts": list(acts or [])})
        return session.cwd

    monkeypatch.setattr(send_mod, "update_cwd_after_send", _spy_probe)
    out = execute_send(
        sess,
        actions=[{"type": "text", "text": "true", "submit": True}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    assert out.status in ("ok", "unchanged", "error")
    assert len(probe_calls) == 1
    assert probe_calls[0]["probe"] is True
    assert _actions_have_submit(probe_calls[0]["acts"]) is True


def test_execute_send_text_without_submit_does_not_call_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text without submit must not call update_cwd_after_send at all."""
    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")

    def _boom(*_a: Any, **_k: Any) -> str | None:
        raise AssertionError("update_cwd_after_send must not run without submit")

    monkeypatch.setattr(send_mod, "update_cwd_after_send", _boom)
    out = execute_send(
        sess,
        actions=[{"type": "text", "text": "partial-line"}],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    assert out.status in ("ok", "unchanged", "error")
    assert b"partial-line" in bytes(pty.written)


def test_execute_send_submit_then_text_without_submit_skips_probe() -> None:
    """Mid-list submit + later typed text: probe must not wipe the new line."""
    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")
    out = execute_send(
        sess,
        actions=[
            {"type": "submit"},
            {"type": "text", "text": "echo_keep_this_partial"},
        ],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=True,
    )
    assert out.status in ("ok", "unchanged", "error")
    blob = bytes(pty.written)
    assert b"echo_keep_this_partial" in blob
    assert encode_key("ctrl+u") not in blob
    assert b"__MRC_PWD__" not in blob
    assert b"printf" not in blob


def test_execute_send_submit_then_text_does_not_call_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_remote_control.screen import send as send_mod

    sess, pty = _session(cols=80, rows=24)
    sess.feed("\x1b[Hprompt$ ")

    def _boom(*_a: Any, **_k: Any) -> str | None:
        raise AssertionError("update_cwd_after_send must not run when submit is not last")

    monkeypatch.setattr(send_mod, "update_cwd_after_send", _boom)
    out = execute_send(
        sess,
        actions=[
            {"type": "text", "text": "true", "submit": True},
            {"type": "text", "text": "typed-after"},
        ],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=True,
        shot=False,
    )
    assert out.status in ("ok", "unchanged", "error")
    assert b"typed-after" in bytes(pty.written)


# ---------------------------------------------------------------------------
# Dead PTY -> send fast-fail with DEAD / session id
# ---------------------------------------------------------------------------


def test_execute_send_dead_pty_status_dead_fast() -> None:
    """Already-dead PTY -> status=dead (or error DEAD) well under 1s."""
    import time

    sess, pty = _session()
    pty._alive = False
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[],
        wait={"until": "idle", "idle_ms": 200, "timeout_ms": 30_000},
        probe_cwd=False,
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"dead send hung for {elapsed:.2f}s"
    assert out.alive is False
    assert out.status == "dead"


def test_execute_send_dead_pty_write_reports_dead_and_session_id() -> None:
    """Write on dead channel surfaces DEAD + session id in error path."""
    import time

    sess, pty = _session()
    pty._alive = False
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[{"type": "text", "text": "pwd", "submit": True}],
        wait={"until": "idle", "idle_ms": 200, "timeout_ms": 30_000},
        probe_cwd=False,
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"dead write send hung for {elapsed:.2f}s"
    assert out.alive is False
    # Action write raises TransportError(DEAD) -> error with DEAD + id in msg.
    blob = f"{out.status} {out.error_code} {out.error_msg}"
    assert "DEAD" in blob or out.status == "dead"
    assert sess.id in blob or out.status == "dead"


def test_execute_send_until_text_dead_pty_returns_immediately() -> None:
    """until=text on a dead PTY ends DEAD well under the wait timeout."""
    import time

    sess, pty = _session()
    pty._alive = False
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[],
        wait={"until": "text", "text": "NEVER_APPEARS", "timeout_ms": 30_000},
        probe_cwd=False,
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"until=text dead wait hung for {elapsed:.2f}s"
    assert out.alive is False
    assert out.status == "dead"


def test_execute_send_until_deadline_dead_pty_returns_immediately() -> None:
    """until=deadline on a dead PTY ends DEAD well under the wait timeout."""
    import time

    sess, pty = _session()
    pty._alive = False
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 30_000},
        probe_cwd=False,
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"until=deadline dead wait hung for {elapsed:.2f}s"
    assert out.alive is False
    assert out.status == "dead"


def test_execute_send_until_text_dies_mid_wait_returns_immediately() -> None:
    """PTY that dies on first drain must not spin until=text to timeout."""
    import time

    class DiesOnDrain(FakePty):
        def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
            self._alive = False
            return 0

    pty = DiesOnDrain()
    sess = ScreenSession(
        id="scr_dies",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    t0 = time.monotonic()
    out = execute_send(
        sess,
        actions=[],
        wait={"until": "text", "text": "NEVER_APPEARS", "timeout_ms": 30_000},
        probe_cwd=False,
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"mid-wait death hung for {elapsed:.2f}s"
    assert out.alive is False
    assert out.status == "dead"


def test_session_require_alive_raises_dead_with_id() -> None:
    """ScreenSession.require_alive / write include DEAD + session id."""
    from mcp_remote_control.transport.base import TransportError

    sess, pty = _session()
    pty._alive = False
    with pytest.raises(TransportError) as ei:
        sess.require_alive()
    assert ei.value.code == "DEAD"
    assert "DEAD" in ei.value.msg
    assert sess.id in ei.value.msg
    with pytest.raises(TransportError) as ei2:
        sess.write(b"x")
    assert ei2.value.code == "DEAD"
    assert sess.id in ei2.value.msg


# ---------------------------------------------------------------------------
# Per-session op lock - concurrent send serializes; no self-deadlock
# ---------------------------------------------------------------------------


class _SerialFakePty(FakePty):
    """Fake PTY that records concurrent write depth (must stay <= 1)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        import threading

        self._write_lock = threading.Lock()
        self._depth = 0
        self.max_depth = 0
        self.write_events: list[bytes] = []

    def write(self, data: bytes) -> int:
        import time

        with self._write_lock:
            self._depth += 1
            if self._depth > self.max_depth:
                self.max_depth = self._depth
        # Brief hold so a racing second send would observe depth > 1 without
        # the session op lock serializing the full pipeline.
        time.sleep(0.002)
        try:
            self.write_events.append(bytes(data))
            return super().write(data)
        finally:
            with self._write_lock:
                self._depth -= 1


def test_session_op_lock_is_rlock() -> None:
    """ScreenSession exposes a re-entrant op lock + serial_ops()."""
    import threading

    sess, pty = _session()
    assert isinstance(sess.op_lock, type(threading.RLock()))
    # Nested serial_ops must not deadlock (RLock re-entry).
    with sess.serial_ops():
        with sess.serial_ops():
            sess.write(b"ok")
    assert bytes(pty.written) == b"ok"


def test_execute_send_reentrant_lock_no_deadlock() -> None:
    """execute_send holds serial_ops; nested write/drain/shot must not deadlock."""
    sess, pty = _session()
    out = execute_send(
        sess,
        actions=[
            {"type": "text", "text": "abc"},
            {"type": "submit"},
        ],
        wait={"until": "deadline", "timeout_ms": 0},
        probe_cwd=False,
        shot=True,
    )
    assert out.status in ("ok", "unchanged", "dead")
    assert encode_text("abc") in bytes(pty.written)
    assert encode_key("enter") in bytes(pty.written)


def test_concurrent_execute_send_serializes_pty_writes() -> None:
    """Two threads send on the same session - write order is not interleaved.

    Each send writes a unique multi-byte marker as a single action sequence.
    Without the session op lock, concurrent action loops would interleave
    marker bytes and max write-depth would exceed 1.
    """
    import threading

    pty = _SerialFakePty(cols=80, rows=24)
    sess = ScreenSession(
        id="scr_conc",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )

    marker_a = b"AAAA_MARKER_A_AAAA"
    marker_b = b"BBBB_MARKER_B_BBBB"
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def worker(marker: bytes) -> None:
        try:
            barrier.wait()
            out = execute_send(
                sess,
                actions=[{"type": "raw", "hex": marker.hex()}],
                wait={"until": "deadline", "timeout_ms": 0},
                probe_cwd=False,
                shot=False,
            )
            assert out.status == "ok", f"send failed: {out.status} {out.error_code}"
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_a = threading.Thread(target=worker, args=(marker_a,))
    t_b = threading.Thread(target=worker, args=(marker_b,))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)
    assert not t_a.is_alive() and not t_b.is_alive(), "send threads hung (deadlock?)"
    assert not errors, f"concurrent send raised: {errors[:3]}"

    # No overlapping PTY writes (depth would be 2 without serialization).
    assert pty.max_depth == 1, (
        f"session op lock failed to serialize writes: max_depth={pty.max_depth}"
    )

    blob = bytes(pty.written)
    # Each marker must appear intact (not byte-interleaved).
    assert marker_a in blob, f"marker A torn or missing in {blob!r}"
    assert marker_b in blob, f"marker B torn or missing in {blob!r}"
    # Exactly one of each, in some serial order (A then B, or B then A).
    assert blob.count(marker_a) == 1
    assert blob.count(marker_b) == 1
    pos_a = blob.index(marker_a)
    pos_b = blob.index(marker_b)
    if pos_a < pos_b:
        assert blob[pos_a : pos_b + len(marker_b)] == marker_a + marker_b
    else:
        assert blob[pos_b : pos_a + len(marker_a)] == marker_b + marker_a


def test_concurrent_execute_send_multi_action_no_interleave() -> None:
    """Multi-action sends keep their internal write order under concurrency."""
    import threading

    pty = _SerialFakePty(cols=80, rows=24)
    sess = ScreenSession(
        id="scr_multi",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )

    # Each worker sends two raw chunks that must stay adjacent.
    seq_x = (b"X1X1X1X1", b"X2X2X2X2")
    seq_y = (b"Y1Y1Y1Y1", b"Y2Y2Y2Y2")
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)

    def worker(parts: tuple[bytes, bytes]) -> None:
        try:
            barrier.wait()
            for _ in range(4):
                execute_send(
                    sess,
                    actions=[
                        {"type": "raw", "hex": parts[0].hex()},
                        {"type": "raw", "hex": parts[1].hex()},
                    ],
                    wait={"until": "deadline", "timeout_ms": 0},
                    probe_cwd=False,
                    shot=False,
                )
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t1 = threading.Thread(target=worker, args=(seq_x,))
    t2 = threading.Thread(target=worker, args=(seq_y,))
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    assert not t1.is_alive() and not t2.is_alive(), "multi-action send hung"
    assert not errors, f"errors: {errors[:3]}"
    assert pty.max_depth == 1

    blob = bytes(pty.written)
    # Pair adjacency: each send's first chunk is immediately followed by its second.
    assert blob.count(seq_x[0] + seq_x[1]) == 4
    assert blob.count(seq_y[0] + seq_y[1]) == 4
    # No torn cross-pairing.
    assert seq_x[0] + seq_y[0] not in blob
    assert seq_y[0] + seq_x[0] not in blob


def test_concurrent_send_and_close_no_deadlock() -> None:
    """Concurrent execute_send + session.close never deadlocks."""
    import threading

    sess, pty = _session()
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)
    done = threading.Event()

    def do_send() -> None:
        try:
            barrier.wait()
            for _ in range(20):
                if sess.closed:
                    break
                execute_send(
                    sess,
                    actions=[{"type": "text", "text": "x"}],
                    wait={"until": "deadline", "timeout_ms": 0},
                    probe_cwd=False,
                    shot=False,
                )
            done.set()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)
            done.set()

    def do_close() -> None:
        try:
            barrier.wait()
            # Let a few sends start, then close under the same op lock.
            import time

            time.sleep(0.01)
            sess.close()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_send = threading.Thread(target=do_send)
    t_close = threading.Thread(target=do_close)
    t_send.start()
    t_close.start()
    t_send.join(timeout=10.0)
    t_close.join(timeout=10.0)
    assert not t_send.is_alive() and not t_close.is_alive(), "send/close deadlock"
    assert sess.closed is True
    assert pty.is_alive() is False
    # DEAD on write after close is expected and not a harness failure; only
    # unexpected exceptions (e.g. RuntimeError from lock) count.
    bad = [e for e in errors if "DEAD" not in str(e)]
    assert not bad, f"unexpected errors: {bad[:3]}"


# ---------------------------------------------------------------------------
# remove holds session lock across close; send after close is not ok
# ---------------------------------------------------------------------------


def test_registry_remove_holds_serial_ops_send_after_close_not_ok() -> None:
    """remove() holds serial_ops across close; send after close is not ok."""
    import threading
    import time

    from mcp_remote_control.screen.registry import ScreenRegistry

    sess, _pty = _session()
    reg = ScreenRegistry()
    reg.add(sess)

    close_started = threading.Event()
    send_results: list[Any] = []
    orig_close = sess.close

    def gated_close() -> None:
        close_started.set()
        # Stay inside remove's serial_ops while a racer send tries to start.
        time.sleep(0.08)
        orig_close()

    sess.close = gated_close  # type: ignore[method-assign]

    def do_remove() -> None:
        reg.remove(sess.id)

    def do_send() -> None:
        assert close_started.wait(timeout=5.0)
        out = execute_send(
            sess,
            actions=[{"type": "text", "text": "after-close"}],
            wait={"until": "deadline", "timeout_ms": 0},
            probe_cwd=False,
            shot=False,
        )
        send_results.append(out)

    t_rm = threading.Thread(target=do_remove)
    t_sd = threading.Thread(target=do_send)
    t_rm.start()
    t_sd.start()
    t_rm.join(timeout=10.0)
    t_sd.join(timeout=10.0)
    assert not t_rm.is_alive() and not t_sd.is_alive(), "remove/send hung"
    assert send_results, "send did not return"
    out = send_results[0]
    assert out.status in ("dead", "error"), f"send after close was {out.status}"
    assert out.status != "ok"
    assert sess.closed is True


def test_registry_close_ids_holds_serial_ops_send_after_close_not_ok() -> None:
    """close_ids() uses remove; send after teardown is error/DEAD not ok."""
    import threading
    import time

    from mcp_remote_control.screen.registry import ScreenRegistry

    sess, _pty = _session()
    reg = ScreenRegistry()
    reg.add(sess)

    close_started = threading.Event()
    send_results: list[Any] = []
    orig_close = sess.close

    def gated_close() -> None:
        close_started.set()
        time.sleep(0.08)
        orig_close()

    sess.close = gated_close  # type: ignore[method-assign]

    def do_close_ids() -> None:
        n = reg.close_ids([sess.id])
        assert n == 1

    def do_send() -> None:
        assert close_started.wait(timeout=5.0)
        out = execute_send(
            sess,
            actions=[{"type": "text", "text": "after-close-ids"}],
            wait={"until": "deadline", "timeout_ms": 0},
            probe_cwd=False,
            shot=False,
        )
        send_results.append(out)

    t_rm = threading.Thread(target=do_close_ids)
    t_sd = threading.Thread(target=do_send)
    t_rm.start()
    t_sd.start()
    t_rm.join(timeout=10.0)
    t_sd.join(timeout=10.0)
    assert not t_rm.is_alive() and not t_sd.is_alive(), "close_ids/send hung"
    assert send_results, "send did not return"
    out = send_results[0]
    assert out.status in ("dead", "error"), f"send after close_ids was {out.status}"
    assert out.status != "ok"
    assert sess.closed is True
    assert reg.get(sess.id) is None


# ---------------------------------------------------------------------------
# Real-PTY regression: paste must execute on the peer it is sent to
# ---------------------------------------------------------------------------

_BASH = "/bin/bash" if Path("/bin/bash").is_file() else None

# A minimal line editor that DOES implement bracketed paste: it announces
# DECSET 2004, then reports the exact bytes it received in hex over stdout.
_APP_ENABLES_2004 = r'''
import os, sys, tty
tty.setraw(0)
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
buf = b""
while True:
    try:
        chunk = os.read(0, 4096)
    except OSError:
        break
    if not chunk:
        break
    buf += chunk
    if b"\x1b[201~" in buf:
        sys.stdout.write("\r\nGOT_BRACKETED:" + buf.hex() + "\r\n")
        sys.stdout.flush()
        buf = b""
'''


def _real_session(pty: Any) -> ScreenSession:
    return ScreenSession(
        id="scr_paste_real",
        ep="local",
        pty=pty,
        cols=pty.cols,
        rows=pty.rows,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_paste_executes_on_shell_that_never_enabled_bracketed_paste(
    tmp_path: Path,
) -> None:
    """Real bash PTY: the paste must run its command, not corrupt the line.

    A shell whose line editor never sends DECSET 2004 cannot parse the
    ``ESC[200~`` delimiters: it consumes the escape prefix and inserts the
    parameter text, so a wrapper-always-on paste lands as ``00~echo ...01~`` -
    the command never executes while the send still reports ok. Newer readline
    turns bracketed paste on by default, so an inputrc pins it off and keeps
    the premise deterministic across bash versions.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    inputrc = tmp_path / "inputrc"
    inputrc.write_text("set enable-bracketed-paste off\n", encoding="utf-8")
    marker = "MRC_PASTE_MARKER_AB3"
    pty = LocalPty(
        cols=120,
        rows=40,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"INPUTRC": str(inputrc)},
    )
    sess = _real_session(pty)
    try:
        sess.drain(1.2)
        if bracketed_paste_enabled(sess.screen):
            pytest.skip("this /bin/bash enables bracketed paste; premise unavailable")
        out = execute_send(
            sess,
            actions=[{"type": "paste", "text": f"echo {marker}", "submit": True}],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        frame = dump_frame(sess.screen)
        assert out.status == "ok", f"paste send failed: {out.status} {out.error_msg}"
        assert marker in [ln.strip() for ln in frame.splitlines()], (
            f"paste was never executed by the shell; frame:\n{frame}"
        )
        assert "command not found" not in frame, f"paste corrupted the line:\n{frame}"
    finally:
        sess.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a POSIX PTY",
)
def test_paste_is_bracketed_for_app_that_enabled_decset_2004() -> None:
    """Real PTY: a peer that announced 2004 still receives the delimiters."""
    from mcp_remote_control.screen.local_pty import LocalPty

    pty = LocalPty(
        cols=120,
        rows=40,
        cwd="/tmp",
        argv=[sys.executable, "-c", _APP_ENABLES_2004],
    )
    sess = _real_session(pty)
    try:
        # Poll for the app's startup declaration instead of guessing a delay.
        deadline = time.monotonic() + 5.0
        while not bracketed_paste_enabled(sess.screen) and time.monotonic() < deadline:
            sess.drain(0.05)
        if not bracketed_paste_enabled(sess.screen):
            pytest.skip("helper app did not announce DECSET 2004 in time")
        out = execute_send(
            sess,
            actions=[{"type": "paste", "text": "PAYLOAD"}],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        frame = dump_frame(sess.screen)
        assert out.status == "ok", f"paste send failed: {out.status} {out.error_msg}"
        received = ""
        for line in frame.splitlines():
            if "GOT_BRACKETED:" in line:
                received = line.split("GOT_BRACKETED:", 1)[1].strip()
        assert received, f"app reported no paste; frame:\n{frame}"
        assert received.startswith(PASTE_START.hex()), (
            f"missing ESC[200~ prefix, app got {received!r}"
        )
        assert PASTE_END.hex() in received, (
            f"missing ESC[201~ suffix, app got {received!r}"
        )
    finally:
        sess.close()


def _assert_ran(frame: str, output: str) -> None:
    """Assert the shell printed *output* and never saw a mangled command."""
    lines = [ln.strip() for ln in frame.splitlines()]
    assert output in lines, f"{output} never printed; frame:\n{frame}"
    assert "command not found" not in frame, f"command line was corrupted:\n{frame}"


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_implicit_click_on_non_tracking_shell_leaves_input_untouched(
    tmp_path: Path,
) -> None:
    """Real bash PTY: an implicit click must not steer the line editor at all.

    With the target above the prompt row the direction-key fallback is not
    harmless: readline's up-key recalls a history entry and the caller's next
    ``text+submit`` is spliced into the recalled line, so bash runs
    ``echo TARGETecho MARKER`` and prints it - while both sends report ok.
    Nothing may be written here; the next command has to run verbatim.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    histfile = tmp_path / "bash_history"
    histfile.write_text("", encoding="utf-8")
    target = "AB14_HIST_TARGET"
    marker = "AB14_HIST_OUT"
    pty = LocalPty(
        cols=120,
        rows=40,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        # Private history file: the only recallable entry is the one this test
        # submits, so a history-recall fallback is deterministic.
        env={"HISTFILE": str(histfile)},
    )
    sess = _real_session(pty)
    try:
        sess.drain(1.2)
        if mouse_tracking_enabled(sess.screen):
            pytest.skip("this /bin/bash enables mouse tracking; premise unavailable")
        execute_send(
            sess,
            actions=[{"type": "text", "text": f"echo {target}", "submit": True}],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.5)

        # click is not given -> defaults true. The peer declared no tracking, so
        # this must write nothing rather than synthesize direction keys.
        nav = execute_send(
            sess,
            actions=[{"type": "to_text", "text": target}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=False,
        )
        assert nav.nav == "no_tracking", f"expected no_tracking, got {nav.nav!r}"
        assert nav.error_code == "NAV_NO_TRACKING"
        assert "go how=keys" in (nav.error_msg or "")

        out = execute_send(
            sess,
            actions=[{"type": "text", "text": f"echo {marker}", "submit": True}],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        frame = dump_frame(sess.screen)
        assert out.status == "ok", f"send failed: {out.status} {out.error_msg}"
        # Splicing shows up as the recalled line and the new command on one
        # output line; the verbatim run prints the marker alone.
        _assert_ran(frame, marker)
        assert f"{target}echo" not in frame, (
            f"next command was spliced into the recalled history line:\n{frame}"
        )
    finally:
        sess.close()


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_explicit_keys_steering_on_non_tracking_shell_is_labelled_nav_keys(
    tmp_path: Path,
) -> None:
    """Real bash PTY: go(how=keys) is the opt-in path and reports nav=keys.

    The keys really reach readline - the up-key walks back through history and
    the recalled entry lands on the prompt line - but the caller asked for
    that, and the result says so instead of claiming a pointer click.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    histfile = tmp_path / "bash_history"
    histfile.write_text("", encoding="utf-8")
    recalled = "AB14_RECALLED_CMD"
    pty = LocalPty(
        cols=120,
        rows=40,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"HISTFILE": str(histfile)},
    )
    sess = _real_session(pty)
    try:
        sess.drain(1.2)
        if mouse_tracking_enabled(sess.screen):
            pytest.skip("this /bin/bash enables mouse tracking; premise unavailable")
        execute_send(
            sess,
            actions=[{"type": "text", "text": f"echo {recalled}", "submit": True}],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.5)

        out = execute_send(
            sess,
            actions=[{"type": "go", "row": 0, "col": 0, "how": "keys"}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        assert out.status == "ok", f"send failed: {out.status} {out.error_msg}"
        assert out.nav == "keys", f"expected nav=keys, got {out.nav!r}"
        assert out.did == ["go"]
        # Direction keys landed: the recalled history entry is on the prompt.
        assert f"echo {recalled}" in dump_frame(sess.screen)
    finally:
        sess.close()


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_to_text_click_false_on_non_tracking_shell_runs_the_line() -> None:
    """Real bash PTY: click=false is the documented key-steering opt-in.

    The target sits on the prompt row (typed but unsubmitted), so the walk is
    horizontal and the next submit still runs the line verbatim - and the
    result is labelled nav=keys rather than nav=ok.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    marker = "AB14_TOTEXT_OUT"
    command = f"echo {marker}"
    pty = LocalPty(
        cols=120,
        rows=40,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
    )
    sess = _real_session(pty)
    try:
        sess.drain(1.2)
        if mouse_tracking_enabled(sess.screen):
            pytest.skip("this /bin/bash enables mouse tracking; premise unavailable")
        # Type the command without submitting it, so to_text finds it on the
        # prompt row and its target is a short left-arrow walk away.
        execute_send(
            sess,
            actions=[{"type": "text", "text": command}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=False,
        )
        out = execute_send(
            sess,
            actions=[
                {"type": "to_text", "text": command, "click": False},
                {"type": "submit"},
            ],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        assert out.status == "ok", f"send failed: {out.status} {out.error_msg}"
        assert out.nav == "keys", f"expected nav=keys, got {out.nav!r}"
        _assert_ran(dump_frame(sess.screen), marker)
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Probe-echo stripping: the echoed probe command wraps over several display
# rows, and the wrap boundary moves with the prompt length and the width, so
# the whole wrap unit has to go - not just the row that carries the marker.
# ---------------------------------------------------------------------------


def _probe_screen(cols: int, ps1: str) -> Any:
    """pyte screen after one probe round-trip laid out at *cols*."""
    import pyte

    from mcp_remote_control.shell.dialect import POSIX_BASH, probe_cmd_for_dialect

    screen = pyte.Screen(cols, 12)
    cmd = probe_cmd_for_dialect(POSIX_BASH) or ""
    assert cmd, "no bash probe template; premise unavailable"
    pyte.Stream(screen).feed(f"{ps1}{cmd}\r\n{PWD_MARKER}/var/log\r\n{ps1}")
    return screen


def _assert_probe_stripped(frame: str, *, context: str) -> None:
    for needle in ("set +o history", "2>/dev/null", PWD_MARKER, "echo "):
        assert needle not in frame, f"{context}: probe text {needle!r} leaked:\n{frame}"
    assert "user@host$" in frame, f"{context}: real content dropped:\n{frame}"


@pytest.mark.parametrize("cols", [100, 120, 160, 200, 240])
@pytest.mark.parametrize("ps1_len", [42, 53, 64, 66, 90])
def test_probe_echo_wrap_unit_stripped_at_every_width(cols: int, ps1_len: int) -> None:
    """The echo's rows are removed as a unit wherever the wrap boundary falls.

    A prompt long enough to push the marker into the echo's second row (or to
    straddle the boundary so no row holds it whole) used to leave the first
    echo row - and the probe command text on it - in the Agent frame.
    """
    ps1 = "user@host$ " + "u" * (ps1_len - 11)
    frame = dump_frame(_probe_screen(cols, ps1))
    _assert_probe_stripped(frame, context=f"cols={cols} ps1={ps1_len}")


@pytest.mark.parametrize("widened", [120, 160, 200, 240])
def test_probe_echo_tail_stays_stripped_after_widening_resize(widened: int) -> None:
    """A widening resize must not resurrect the echo row it wrapped onto.

    pyte's Screen.resize only pops cells when narrowing, so a row that was
    written full at the old width keeps its last cell there. Measuring
    "fullness" against the *current* columns made every pre-resize row look
    short again, and the echo tail the strip had just dropped came back.
    """
    screen = _probe_screen(100, "user@host$ ")
    before = dump_frame(screen)
    _assert_probe_stripped(before, context="pre-resize")

    screen.resize(12, widened)
    _assert_probe_stripped(dump_frame(screen), context=f"widened to {widened}")


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_probe_echo_wrap_does_not_reach_the_agent_frame_on_bash() -> None:
    """Real bash PTY: a 66-char prompt wraps the echo past the marker row.

    The echo is 66 + 106 chars at cols=100, so the marker sits wholly on the
    second row and the first row carries no marker at all - the case that used
    to hand the Agent ``user@host:... $  set +o history 2>/dev/null||:;ech``.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    ps1 = "user@host:" + "u" * 54 + "$ "
    assert len(ps1) == 66
    pty = LocalPty(
        cols=100,
        rows=30,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"PS1": ps1, "PROMPT_COMMAND": "", "HISTFILE": "/dev/null"},
    )
    sess = ScreenSession(
        id="scr_probe_echo_real",
        ep="local",
        pty=pty,
        cols=100,
        rows=30,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        dialect="posix-bash",
    )
    try:
        sess.drain(1.2)
        sess.write(b"\x0c")  # clean screen so the echo rows are unambiguous
        sess.drain(0.5)
        out = execute_send(
            sess,
            actions=[{"type": "text", "text": "true", "submit": True}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=True,
        )
        assert out.cwd and out.cwd.startswith("/"), f"probe did not confirm: {out.cwd!r}"
        frame = dump_frame(sess.screen)
        for needle in ("set +o history", "2>/dev/null", PWD_MARKER, "echo "):
            assert needle not in frame, f"probe text {needle!r} leaked:\n{frame}"
        assert "$ true" in frame, f"real content dropped:\n{frame}"
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Probe text wider than the terminal: the printed path wraps as well as the
# echo, and the fill rule that groups the echo's rows also groups rows that
# are not probe text at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cols", [100, 120, 160, 200])
def test_probe_path_wrap_tail_is_not_handed_to_the_agent(cols: int) -> None:
    """A cwd wider than the terminal pushes the path's tail onto the next row.

    The marker lands on the row above it, so the marker-keyed second pass sees
    nothing to do once that row is gone: the tail row carries no marker and
    reads as ordinary frame content. The prompt that follows must survive.
    """
    import pyte

    from mcp_remote_control.shell.dialect import POSIX_BASH, probe_cmd_for_dialect

    ps1 = "user@host$ "
    cmd = probe_cmd_for_dialect(POSIX_BASH) or ""
    assert cmd, "no bash probe template; premise unavailable"
    path = "/p" + "a" * (cols + 60)
    screen = pyte.Screen(cols, 16)
    pyte.Stream(screen).feed(f"{ps1}{cmd}\r\n{PWD_MARKER}{path}\r\n{ps1}")

    frame = dump_frame(screen)
    assert PWD_MARKER not in frame
    assert "a" * 12 not in frame, f"wrapped path tail leaked at {cols} cols:\n{frame}"
    assert path[:40] not in frame, f"path prefix leaked at {cols} cols:\n{frame}"
    assert "user@host$" in frame, f"real content dropped at {cols} cols:\n{frame}"


@pytest.mark.parametrize("fill", [" ", "x"])
def test_probe_strip_keeps_a_full_width_row_above_the_echo(fill: str) -> None:
    """Only probe text is dropped: the row above the echo is content.

    A row that reached its last column is grouped with the row below it, so a
    command echo (or a program-output line) that exactly fills the width is
    drawn into the probe's wrap unit. It carries no probe template text, which
    is the only thing that can be echo text, and must stay in the frame.
    """
    import pyte

    from mcp_remote_control.screen.buffer import _full_rows
    from mcp_remote_control.shell.dialect import POSIX_BASH, probe_cmd_for_dialect

    cols = 100
    ps1 = "user@host$ "
    cmd = probe_cmd_for_dialect(POSIX_BASH) or ""
    assert cmd, "no bash probe template; premise unavailable"
    typed = ":" + fill * (cols - len(ps1) - 1)
    assert len(ps1) + len(typed) == cols
    screen = pyte.Screen(cols, 16)
    pyte.Stream(screen).feed(
        f"{ps1}{typed}\r\n{ps1}{cmd}\r\n{PWD_MARKER}/var/log\r\n{ps1}"
    )
    assert _full_rows(screen, 16)[0] is True, "premise: the echo fills its row"

    frame = dump_frame(screen)
    assert (ps1 + typed).rstrip() in frame, f"command echo dropped:\n{frame}"
    for needle in ("set +o history", "2>/dev/null", PWD_MARKER, "echo "):
        assert needle not in frame, f"probe text {needle!r} leaked:\n{frame}"


def test_probe_strip_measures_every_dumped_width() -> None:
    """A dump that does not strip still records the width it observed.

    "Did this row reach its last column" has to be asked of every width the
    screen was driven at, because a widening resize keeps the old last cell.
    A screen first dumped after the resize has no earlier width on record, so
    the echo row stops looking full and the tail row it wrapped onto comes
    back into the frame.
    """
    screen = _probe_screen(100, "user@host$ ")
    raw = dump_frame(screen, strip_probe=False)
    assert PWD_MARKER in raw, "premise: the echo is on screen"

    screen.resize(12, 200)
    _assert_probe_stripped(
        dump_frame(screen),
        context="widened after an unstripped dump at 100",
    )


def test_probe_cwd_is_whole_when_the_path_outruns_the_terminal() -> None:
    """The probe's own parse must not stop at the wrap boundary either.

    ``_marker_rows`` reads the marker line row by row, so a cwd wider than the
    terminal was recorded as the prefix that fit its first row - a path that
    names no directory on the peer, reported as the authoritative ``cwd=``
    with ``cwd_src='probe'`` and compounded by every later relative cd.
    """
    import pyte

    from mcp_remote_control.screen.buffer import _full_rows
    from mcp_remote_control.screen.cwd_probe import _marker_rows

    cols = 100
    path = "/p" + "a" * (cols + 60)
    screen = pyte.Screen(cols, 16)
    pyte.Stream(screen).feed(f"{PWD_MARKER}{path}\r\n$ ")

    text = dump_frame(screen, strip_probe=False)
    rows = _full_rows(screen, 16)
    assert [p for _, p in _marker_rows(text)] == [path[: cols - len(PWD_MARKER)]], (
        "premise: a row-local parse yields the prefix"
    )
    assert [p for _, p in _marker_rows(text, rows)] == [path]


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_probe_path_tail_and_cwd_are_whole_on_bash(tmp_path: Path) -> None:
    """Real bash PTY: one geometry, two failures, both fixed.

    A cwd wider than the terminal wraps the printed path. Its tail rows were
    handed to the Agent (and carried no marker for the second pass to key on),
    and the probe recorded the row-local prefix of its own path as the session
    cwd.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    long_dir = tmp_path / ("d" * 100)
    long_dir.mkdir()
    pty = LocalPty(
        cols=100,
        rows=30,
        shell=_BASH,
        cwd=str(long_dir),
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"PS1": "user@host$ ", "PROMPT_COMMAND": "", "HISTFILE": "/dev/null"},
    )
    sess = ScreenSession(
        id="scr_probe_path_tail",
        ep="local",
        pty=pty,
        cols=100,
        rows=30,
        cwd=str(long_dir),
        surface="shell",
        open_mode="shell",
        dialect="posix-bash",
    )
    try:
        sess.drain(1.2)
        sess.write(b"\x0c")  # clean screen so the echo rows are unambiguous
        sess.drain(0.5)
        out = execute_send(
            sess,
            actions=[{"type": "text", "text": "true", "submit": True}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=True,
        )
        assert out.cwd, "probe did not confirm"
        assert out.cwd.endswith("d" * 100), f"probe cwd truncated: {out.cwd!r}"
        frame = dump_frame(sess.screen)
        assert "d" * 20 not in frame, f"path tail leaked:\n{frame}"
        assert PWD_MARKER not in frame
        assert "user@host$ true" in frame, f"real content dropped:\n{frame}"
    finally:
        sess.close()


def _run_full_width_echo_probe() -> None:
    """Drive a real bash PTY with a full-width command and check its echo.

    The child's TERM is pinned rather than inherited: readline follows TERM,
    and a dumb child terminal draws a horizontal-scroll marker instead of the
    full-width echo this scenario asserts on.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    cols = 100
    ps1 = "user@host$ "
    typed = ":" + " " * (cols - len(ps1) - 1)
    pty = LocalPty(
        cols=cols,
        rows=30,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        # Pin the child's terminal type: under a dumb host terminal readline
        # draws a horizontal-scroll marker instead of the full-width echo, so
        # this scenario's outcome would otherwise depend on the caller's env.
        env={
            "TERM": "xterm-256color",
            "PS1": ps1,
            "PROMPT_COMMAND": "",
            "HISTFILE": "/dev/null",
        },
    )
    sess = ScreenSession(
        id="scr_probe_full_echo",
        ep="local",
        pty=pty,
        cols=cols,
        rows=30,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        dialect="posix-bash",
    )
    try:
        sess.drain(1.2)
        sess.write(b"\x0c")
        sess.drain(0.5)
        out = execute_send(
            sess,
            actions=[{"type": "text", "text": typed, "submit": True}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
            probe_cwd=True,
        )
        assert out.cwd, "probe did not confirm"
        frame = dump_frame(sess.screen)
        assert (ps1 + typed).rstrip() in frame, f"command echo dropped:\n{frame}"
        for needle in ("set +o history", "2>/dev/null", PWD_MARKER):
            assert needle not in frame, f"probe text {needle!r} leaked:\n{frame}"
    finally:
        sess.close()


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_probe_strip_keeps_a_full_width_command_echo_on_bash() -> None:
    """Real bash PTY: the Agent's own command echo survives the strip.

    The command is typed so its echo fills the row exactly, which puts it in
    the probe echo's wrap unit; it carries no probe text and must stay.
    """
    _run_full_width_echo_probe()


@pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="needs a POSIX PTY and /bin/bash",
)
def test_full_width_echo_probe_holds_with_a_dumb_host_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-width echo probe must not depend on the caller's terminal.

    The scenario pins the PTY child's TERM itself, so forcing the host TERM
    to "dumb" - where readline draws a horizontal-scroll marker instead of
    the full-width echo - must leave its outcome unchanged.
    """
    monkeypatch.setenv("TERM", "dumb")
    _run_full_width_echo_probe()


# ---------------------------------------------------------------------------
# Small contract defects in the same two modules: a flag read with the wrong
# reader, a tilde form the cd heuristic did not guard, and a zero-delta label.
# ---------------------------------------------------------------------------


def test_click_force_click_reads_only_a_boolean() -> None:
    """An unrecognised ``force_click`` spelling is refused, not read as unset.

    ``force_click`` is the caller's opt-in for a peer whose modes the buffer
    cannot show. ``action_truthy`` maps every spelling outside its truthy set
    to False, so a caller who wrote "always" would be told the peer has no
    mouse reporting - the same silent negative branch ``click`` no longer
    takes.
    """
    sess, pty = _session()
    assert not mouse_tracking_enabled(sess.screen)
    for spelling in ("always", "auto", "maybe", "", "TRUE!"):
        with pytest.raises(ActionError) as ei:
            apply_action(
                sess,
                {"type": "click", "row": 1, "col": 1, "force_click": spelling},
            )
        assert ei.value.code == "INVALID_ARG", spelling
    assert bytes(pty.written) == b"", "a refused click wrote bytes"

    for truthy in (True, 1, "true", "yes", "on", "1"):
        sess_on, pty_on = _session()
        apply_action(
            sess_on,
            {"type": "click", "row": 1, "col": 1, "force_click": truthy},
        )
        assert encode_mouse_sgr(1, 1, button="left", press=True) in bytes(
            pty_on.written
        ), truthy

    for falsy in (False, 0, "false", "no", "off", "0", None):
        sess_off, pty_off = _session()
        with pytest.raises(ActionError) as ei:
            apply_action(
                sess_off,
                {"type": "click", "row": 1, "col": 1, "force_click": falsy},
            )
        assert ei.value.code == "NAV_NO_TRACKING", falsy
        assert bytes(pty_off.written) == b""


def test_cd_tilde_user_is_not_joined_onto_the_current_cwd() -> None:
    """``cd ~other`` names a directory we cannot know, so cwd is left alone.

    The tilde guard exists because expanding a remote ``~`` against the
    controller's home records a path that does not exist on the peer.
    ``~other`` is that mistake one spelling out, and unlike the un-normalized
    ``cd ..`` case the recorded string names no directory at all - every later
    relative cd compounds it. Both spellings of a *known* home must normalize
    the same way, as the docstring promises.
    """
    from mcp_remote_control.screen.cwd_probe import _resolve_cd_target, apply_cd_heuristic

    assert _resolve_cd_target("~other", "/a/b/c", home="/home/u") is None
    assert _resolve_cd_target("~other/x", "/a/b/c", home="/home/u") is None
    assert _resolve_cd_target("~", None, home="/home/u/../v") == "/home/v"
    assert _resolve_cd_target("~/x", None, home="/home/u/../v") == "/home/v/x"

    sess, _ = _session()
    sess.cwd = "/a/b/c"
    apply_cd_heuristic(sess, [{"type": "text", "text": "cd ~other", "submit": True}])
    assert sess.cwd == "/a/b/c", "a home we cannot know was joined onto the cwd"


def test_move_non_int_delta_is_an_argument_error() -> None:
    """A misspelled move delta is INVALID_ARG, not an execution failure.

    ``click`` and ``go`` read their numeric fields through an int() guarded to
    ``ActionError``; ``move`` let the bare ``ValueError`` escape to the action
    loop's catch-all, which labelled it EXEC_FAILED - an argument mistake
    reported as a session failure, and nothing written either way.
    """
    for bad in ("abc", "1.5x", [1], {"a": 1}):
        sess, pty = _session()
        with pytest.raises(ActionError) as ei:
            apply_action(sess, {"type": "move", "row_delta": bad})
        assert ei.value.code == "INVALID_ARG", bad
        assert bytes(pty.written) == b"", "a refused move wrote bytes"

        sess2, pty2 = _session()
        out = execute_send(
            sess2,
            actions=[{"type": "move", "col_delta": bad}],
            wait={"until": "idle", "idle_ms": 0, "timeout_ms": 1000},
            probe_cwd=False,
        )
        assert out.status == "error", bad
        assert out.error_code == "INVALID_ARG", (bad, out.error_code)
        assert bytes(pty2.written) == b"", "a refused move wrote bytes"


# ---------------------------------------------------------------------------
# WaitSpec budget: min_ms and every drain slice are shares of one deadline
# ---------------------------------------------------------------------------


class _SleepingPty(FakePty):
    """Fake PTY whose drain sleeps the slice it is handed and records it.

    Recording the budgets makes the wait's deadline observable without reading
    a jittery wall clock: what the wait spends is exactly what it asked each
    drain for. *chunks* are handed out one per drain, ahead of the silence.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.drain_budgets: list[float] = []
        self.chunks: list[bytes] = []

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        self.drain_budgets.append(seconds)
        if seconds > 0:
            time.sleep(seconds)
        if self.chunks:
            chunk = self.chunks.pop(0)
            if on_data is not None:
                on_data(chunk)
            return len(chunk)
        return 0


def _wait_session(pty: FakePty) -> ScreenSession:
    return ScreenSession(
        id="scr_wait",
        ep="local",
        pty=pty,
        cols=pty.cols,
        rows=pty.rows,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )


def test_wait_min_ms_cannot_outlast_timeout() -> None:
    """min_ms is a floor inside the timeout, not a wait that precedes it.

    A settle floor above the timeout used to be drained in full before the
    timeout was ever consulted, and the drain after that check was a fixed
    slice: min_ms=150 with timeout_ms=10 held the caller for the whole floor
    and the budget was silently overridden.
    """
    pty = _SleepingPty(cols=80, rows=24)
    sess = _wait_session(pty)
    t0 = time.monotonic()
    mode = wait_after(
        sess,
        normalize_wait(
            {"until": "idle", "min_ms": 150, "timeout_ms": 10},
            actions_nonempty=True,
        ),
    )
    elapsed = time.monotonic() - t0

    assert mode == "idle"
    total = sum(pty.drain_budgets)
    assert total <= 0.011, (
        f"a 0.010s wait handed {total:.4f}s of drains to its PTY: "
        f"{pty.drain_budgets}"
    )
    assert total >= 0.005, (
        f"the wait skipped its settle floor instead of sharing the budget: "
        f"{pty.drain_budgets}"
    )
    assert elapsed < 0.12, f"min_ms=150 outlasted its 10ms timeout: {elapsed:.3f}s"


def test_wait_min_ms_floor_is_still_spent_when_it_fits_the_timeout() -> None:
    """A min_ms that fits inside the timeout is still settled for in full."""
    pty = _SleepingPty(cols=80, rows=24)
    sess = _wait_session(pty)
    t0 = time.monotonic()
    mode = wait_after(sess, {"until": "idle", "min_ms": 50, "idle_ms": 200, "timeout_ms": 5000})
    elapsed = time.monotonic() - t0

    assert mode == "idle"
    assert sum(pty.drain_budgets) >= 0.045, (
        f"the 0.050s floor was not spent: {pty.drain_budgets}"
    )
    assert elapsed < 1.0, f"wait ran to the 5s timeout: {elapsed:.3f}s"


def test_wait_zero_timeout_is_a_zero_budget() -> None:
    """timeout_ms=0 spends nothing: no fixed settle slice past the deadline.

    The wait returns the frame it already has; the pipeline's own post-wait
    drain is what refreshes it, and the caller granted no time here.
    """
    pty = _SleepingPty(cols=80, rows=24)
    sess = _wait_session(pty)
    t0 = time.monotonic()
    mode = wait_after(sess, {"until": "idle", "idle_ms": 200, "timeout_ms": 0})
    elapsed = time.monotonic() - t0

    assert mode == "idle"
    assert pty.drain_budgets == [], (
        f"a zero timeout still handed {pty.drain_budgets} to the PTY"
    )
    assert elapsed < 0.05, f"zero-budget wait took {elapsed:.3f}s"


def test_wait_idle_loop_slices_share_the_same_deadline() -> None:
    """Every drain the idle loop asks for is a share of the same budget.

    These waits carry no settle floor that could hide the loop, so what the
    wait spends is exactly the slices it hands the PTY: one budget-sized slice
    with no min_ms, and with min_ms the floor plus the loop must still fit.
    """
    quiet = _SleepingPty(cols=80, rows=24)
    assert wait_after(
        _wait_session(quiet), {"until": "idle", "idle_ms": 200, "timeout_ms": 10}
    ) == "idle"
    total = sum(quiet.drain_budgets)
    assert total <= 0.011, (
        f"a 0.010s idle wait handed {total:.4f}s of drains to its PTY: "
        f"{quiet.drain_budgets}"
    )
    assert total >= 0.002, f"the wait never spent its budget: {quiet.drain_budgets}"

    floored = _SleepingPty(cols=80, rows=24)
    assert wait_after(
        _wait_session(floored),
        {"until": "idle", "min_ms": 5, "idle_ms": 200, "timeout_ms": 10},
    ) == "idle"
    total = sum(floored.drain_budgets)
    assert total <= 0.011, (
        f"floor plus loop drains outran the 0.010s budget: {floored.drain_budgets}"
    )
    assert total >= 0.005, (
        f"the 0.005s floor was not spent out of the budget: {floored.drain_budgets}"
    )


def test_wait_idle_slice_is_clipped_to_a_mid_size_budget() -> None:
    """A budget under the loop's 50ms slice cap is drained in one clipped slice."""
    pty = _SleepingPty(cols=80, rows=24)
    assert wait_after(
        _wait_session(pty), {"until": "idle", "idle_ms": 200, "timeout_ms": 40}
    ) == "idle"
    total = sum(pty.drain_budgets)
    assert total <= 0.041, (
        f"a 0.040s idle wait handed {total:.4f}s of drains to its PTY: "
        f"{pty.drain_budgets}"
    )
    assert total >= 0.002, f"the wait never spent its budget: {pty.drain_budgets}"


def test_wait_idle_still_settles_on_the_quiet_window() -> None:
    """Data then silence: the wait ends on idle_ms, well inside the timeout."""
    pty = _SleepingPty(cols=80, rows=24)
    pty.chunks = [b"prompt$ "]
    sess = _wait_session(pty)
    t0 = time.monotonic()
    mode = wait_after(sess, {"until": "idle", "idle_ms": 50, "timeout_ms": 5000})
    elapsed = time.monotonic() - t0

    assert mode == "idle"
    assert "prompt$" in dump_frame(sess.screen), "the drain never reached the buffer"
    assert elapsed >= 0.05, f"the quiet window was not observed: {elapsed:.3f}s"
    assert elapsed < 1.0, f"idle settle ran to the 5s timeout: {elapsed:.3f}s"


def test_wait_idle_dead_peer_ends_the_wait_immediately() -> None:
    """A peer that dies mid-wait ends the wait at once, floor or not."""

    class _DiesOnDrain(_SleepingPty):
        def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
            self.drain_budgets.append(seconds)
            self._alive = False
            return 0

    pty = _DiesOnDrain(cols=80, rows=24)
    sess = _wait_session(pty)
    t0 = time.monotonic()
    mode = wait_after(
        sess,
        {"until": "idle", "min_ms": 150, "idle_ms": 200, "timeout_ms": 30_000},
    )
    elapsed = time.monotonic() - t0

    assert mode == "idle"
    assert elapsed < 1.0, f"wait on a dead peer ran for {elapsed:.3f}s"


def test_wait_text_behaviour_holds() -> None:
    """until=text still returns on a match and on its own timeout."""
    pty = _SleepingPty(cols=80, rows=24)
    pty.chunks = [b"READY\r\n"]
    sess = _wait_session(pty)
    t0 = time.monotonic()
    assert wait_after(sess, {"until": "text", "text": "READY", "timeout_ms": 5000}) == "text"
    assert time.monotonic() - t0 < 1.0, "a present needle ran to the timeout"

    pty2 = _SleepingPty(cols=80, rows=24)
    sess2 = _wait_session(pty2)
    t0 = time.monotonic()
    assert wait_after(sess2, {"until": "text", "text": "NEVER", "timeout_ms": 100}) == "text"
    elapsed = time.monotonic() - t0
    assert 0.05 <= elapsed < 0.5, f"absent needle returned after {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Real PTY: the peer receives the session's code page, not utf-8
# ---------------------------------------------------------------------------

# A minimal raw-mode peer that announces DECSET 2004 and reports the exact
# bytes it received, in hex, so a codec mistake is visible as bytes rather
# than as a frame that merely looks odd.
_APP_ECHOES_RX_HEX = r'''
import os, sys, tty
tty.setraw(0)
sys.stdout.write("\x1b[?2004h")
sys.stdout.flush()
while True:
    try:
        chunk = os.read(0, 4096)
    except OSError:
        break
    if not chunk:
        break
    sys.stdout.write("\r\nRX:" + chunk.hex() + "\r\n")
    sys.stdout.flush()
'''


def _rx_report_lines(frame: str) -> list[str]:
    """Lines of *frame* the peer wrote to report the bytes it read.

    One line per ``read()`` the peer performed, so its boundaries are a
    property of the peer's scheduling (load can coalesce several writes into
    one read) and never of what was sent. An assertion about the bytes on the
    wire therefore searches inside these lines instead of expecting a payload
    at the start of the report.
    """
    return [ln for ln in frame.splitlines() if ln.startswith("RX:")]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX PTY")
def test_gbk_session_puts_its_code_page_on_the_wire() -> None:
    """Real PTY: a gbk session writes \u4e2d\u6587 as d6d0cec4, text, paste and key alike.

    The receiving side pins the peer codec, so the sending side has to speak
    it too: utf-8 bytes on a cp936 console are mojibake for the peer while the
    send still reports ok. A ``key`` whose base is a single character is peer
    text as well, while the delimiters of the bracketed paste and the ESC of
    the alt form stay ASCII - they are terminal control bytes, not peer text.
    """
    from mcp_remote_control.screen.local_pty import LocalPty

    pty = LocalPty(
        cols=120,
        rows=40,
        cwd="/tmp",
        argv=[sys.executable, "-c", _APP_ECHOES_RX_HEX],
    )
    sess = ScreenSession(
        id="scr_codec_real",
        ep="local",
        pty=pty,
        cols=pty.cols,
        rows=pty.rows,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        text_encoding="gbk",
    )
    try:
        # Poll for the app's startup declaration instead of guessing a delay.
        deadline = time.monotonic() + 5.0
        while not bracketed_paste_enabled(sess.screen) and time.monotonic() < deadline:
            sess.drain(0.05)
        assert bracketed_paste_enabled(sess.screen), "peer never announced DECSET 2004"
        out = execute_send(
            sess,
            actions=[
                {"type": "text", "text": CJK_TEXT},
                {"type": "paste", "text": CJK_TEXT},
                {"type": "key", "key": "alt+" + CJK_CHAR},
            ],
            wait={"until": "idle", "idle_ms": 400, "timeout_ms": 5000},
            probe_cwd=False,
        )
        sess.drain(0.6)
        frame = dump_frame(sess.screen)
        assert out.status == "ok", f"send failed: {out.status} {out.error_msg}"
        reported = _rx_report_lines(frame)
        assert any(CJK_GBK.hex() in ln for ln in reported), (
            f"peer did not receive the gbk text bytes; frame:\n{frame}"
        )
        wrapped = (PASTE_START + CJK_GBK + PASTE_END).hex()
        assert any(wrapped in ln for ln in reported), (
            f"peer did not receive the wrapped gbk paste; frame:\n{frame}"
        )
        # ESC immediately followed by the gbk char: the alt-form key base went
        # out in the codec while its ESC stayed a raw control byte.
        alt_char = (b"\x1b" + CJK_CHAR_GBK).hex()
        assert any(alt_char in ln for ln in reported), (
            f"peer did not receive the alt-form gbk key char; frame:\n{frame}"
        )
        assert CJK_TEXT.encode("utf-8").hex() not in frame, (
            f"utf-8 bytes reached a gbk peer; frame:\n{frame}"
        )
        assert CJK_CHAR.encode("utf-8").hex() not in frame, (
            f"utf-8 key base reached a gbk peer; frame:\n{frame}"
        )
    finally:
        sess.close()

