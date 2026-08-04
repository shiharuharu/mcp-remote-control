"""Unit tests: full 008 action set + encoding order + nav (T10/T19)."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_remote_control.screen.buffer import (
    dump_frame,
    find_text,
    mouse_tracking_enabled,
)
from mcp_remote_control.screen.keys import (
    CSI,
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
    _is_noop_actions,
    apply_action,
    encode_actions_bytes,
    execute_send,
    normalize_actions,
    normalize_wait,
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


# ---------------------------------------------------------------------------
# Catalog coverage (008 §3.1)
# ---------------------------------------------------------------------------


def test_action_types_match_008_catalog() -> None:
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
    assert raw == encode_paste("hello world") + encode_key("enter")


def test_encode_actions_paste_submit_flag() -> None:
    raw = encode_actions_bytes(
        [{"type": "paste", "text": "hi", "submit": True}],
    )
    assert raw == encode_paste("hi") + encode_key("enter")


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
    sess.feed("\x1b[11;6H")  # 1-based → (10,5)
    assert sess.screen.cursor.y == 10
    assert sess.screen.cursor.x == 5
    name = apply_action(sess, {"type": "go", "row": 7, "col": 2, "how": "keys"})
    assert name == "go"
    # Δrow = -3 → 3 up; Δcol = -3 → 3 left
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


def test_apply_go_auto_uses_keys_without_mouse() -> None:
    sess, pty = _session()
    sess.feed("\x1b[5;5H")  # cur (4,4)
    apply_action(sess, {"type": "go", "row": 4, "col": 6, "how": "auto"})
    assert encode_key("right") in bytes(pty.written) or bytes(pty.written).count(
        encode_key("right")
    ) >= 1
    assert CSI + b"<" not in bytes(pty.written)


def test_apply_click_double() -> None:
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 2},
    )
    # 2 press+release pairs
    assert bytes(pty.written).count(b"M") == 2
    assert bytes(pty.written).count(b"m") == 2


def test_apply_click_triple_emits_three_pairs() -> None:
    """O3: clicks=3 must emit 3 press+release pairs (was capped at 2)."""
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 3},
    )
    # 3 press+release pairs → 3 'M' (press) + 3 'm' (release) final bytes.
    assert bytes(pty.written).count(b"M") == 3
    assert bytes(pty.written).count(b"m") == 3


def test_apply_click_single_emits_one_pair() -> None:
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 1},
    )
    assert bytes(pty.written).count(b"M") == 1
    assert bytes(pty.written).count(b"m") == 1


def test_apply_click_zero_emits_no_pairs() -> None:
    """C1: clicks=0 must emit 0 press+release pairs (was 1 pair via the old
    `int(action.get("clicks") or 1)` which treated 0 as falsy)."""
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": 0},
    )
    assert bytes(pty.written) == b""
    assert bytes(pty.written).count(b"M") == 0
    assert bytes(pty.written).count(b"m") == 0


def test_apply_click_negative_emits_no_pairs() -> None:
    """C1: negative clicks clamp to 0 → 0 press+release pairs."""
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": -3},
    )
    assert bytes(pty.written) == b""


def test_apply_click_null_clicks_defaults_to_one_pair() -> None:
    """C1: clicks=None (JSON null) preserves the old default → 1 pair."""
    sess, pty = _session()
    apply_action(
        sess,
        {"type": "click", "row": 1, "col": 2, "button": "left", "clicks": None},
    )
    assert bytes(pty.written).count(b"M") == 1
    assert bytes(pty.written).count(b"m") == 1


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
    sess.feed("\x1b[Hfoo bar foo baz")
    # second "foo"
    apply_action(sess, {"type": "to_text", "text": "foo", "nth": 2, "click": True})
    press = encode_mouse_sgr(0, 8, button="left", press=True)
    assert press in bytes(pty.written)


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


def test_execute_send_to_text_nav_meta() -> None:
    sess, _pty = _session(cols=40, rows=10)
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


def test_execute_send_empty_unchanged_omits_frame() -> None:
    """Empty / nop-only send with stable hash → status=unchanged, no body."""
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
# O3: cross-chunk UTF-8 decode (H5)
# ---------------------------------------------------------------------------


def test_feed_cross_chunk_utf8_preserves_multibyte_char() -> None:
    """A 3-byte UTF-8 char split across two feed() calls decodes as one char."""
    sess, _pty = _session(cols=40, rows=5)
    # "中" = U+4E2D = b'\xe4\xb8\xad' (3 bytes); split across the read boundary.
    sess.feed(b"\xe4\xb8")
    sess.feed(b"\xad")
    frame = dump_frame(sess.screen)
    assert "中" in frame
    # No U+FFFD from a per-chunk replace decode.
    assert "�" not in frame


def test_feed_cross_chunk_utf8_4byte_emoji() -> None:
    """A 4-byte emoji split across chunks decodes correctly."""
    sess, _pty = _session(cols=40, rows=5)
    # "😀" = U+1F600 = b'\xf0\x9f\x98\x80'
    sess.feed(b"\xf0\x9f")
    sess.feed(b"\x98\x80")
    frame = dump_frame(sess.screen)
    assert "😀" in frame
    assert "�" not in frame


def test_feed_str_passes_through_unchanged() -> None:
    sess, _pty = _session(cols=40, rows=5)
    sess.feed("中")
    assert "中" in dump_frame(sess.screen)


# ---------------------------------------------------------------------------
# O3: wait/resize sends are non-intrusive (skip cwd probe, allow unchanged)
# ---------------------------------------------------------------------------


def test_is_noop_actions_wait_and_resize() -> None:
    assert _is_noop_actions([]) is True
    assert _is_noop_actions([{"type": "nop"}]) is True
    assert _is_noop_actions([{"type": "wait", "ms": 500}]) is True
    assert _is_noop_actions([{"type": "resize", "cols": 100, "rows": 30}]) is True
    assert _is_noop_actions(
        [{"type": "nop"}, {"type": "wait", "ms": 5}, {"type": "resize"}]
    ) is True
    # Any meaningful input action still triggers the probe.
    assert _is_noop_actions([{"type": "text", "text": "x"}]) is False
    assert _is_noop_actions([{"type": "key", "key": "enter"}]) is False
    assert _is_noop_actions(
        [{"type": "wait", "ms": 5}, {"type": "text", "text": "x"}]
    ) is False


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
