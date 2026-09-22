"""Unit tests: row-fullness across resizes, widening and narrow-then-widening.

A row that reached the last column of the width it was written under continues
onto the row below it, and the cwd-probe strip logic depends on that signal to
drop a wrapped echo tail (``strip_probe_lines``). pyte's ``Screen.resize`` does
not re-wrap history: widening only raises ``Screen.columns`` and leaves every
row at the width it was written under. Asking "did this row reach its last
column" of the *current* width alone therefore loses the signal on the first
widening resize, and the probe tail the strip had been dropping reappears in
the Agent-facing frame.

``buffer._seen_columns`` keeps that signal: the check is made against every
width the screen was observed at - frame dumps and resizes alike, not just the
current one. Narrowing is the destructive direction: pyte pops the cells beyond
the new width with no trace, so a narrow-then-widen (two resize actions in one
send, with no dump between them) erases the last column of a full row and no
later dump can recover it. The resize observation is what records the width in
between (``_watch_resizes``); these tests pin both the record and the invariant
that keeps it complete in the live path - the cwd probe dumps the frame at the
current width immediately before it writes, and the open path dumps before
anything can resize the screen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import pyte

from mcp_remote_control.screen import buffer as buffer_mod
from mcp_remote_control.screen.buffer import (
    PWD_MARKER,
    _full_rows,
    _holds_text_rows,
    dump_frame,
)
from mcp_remote_control.screen.cwd_probe import _PROBE_CMD_BASH, silent_pwd_probe
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.shell.dialect import POSIX_BASH

_NARROW_COLS = 100
_WIDE_COLS = 200
_ROWS = 20
# 40 chars, mirroring the reviewed geometry: long enough that the injected probe
# echo wraps, short enough that the wrap boundary does not fall inside the
# marker text. At 120 cols the same echo wraps too: 40 + 106 = 146 chars.
_MID_COLS = 120
_PS1 = "user@host:~/proj [git:feature/x] (venv) $ "
_PS1_MID = "user@host:~/a/very/long/path/that/goes [git:br] $ "
_LONG_PATH = "/var/log"
# Text that can only come from the injected probe command.
_PROBE_ONLY_TEXT = ("set +o history", "2>/dev/null||", "pwd -P", PWD_MARKER)


def _leaked_probe_lines(frame: str) -> list[str]:
    return [
        ln
        for ln in frame.splitlines()
        if any(token in ln for token in _PROBE_ONLY_TEXT)
    ]


def _echoed_screen(
    cols: int = _NARROW_COLS, ps1: str = _PS1, path: str = _LONG_PATH
) -> Any:
    """Screen showing a wrapped probe echo + its printed marker + next prompt."""
    screen = pyte.Screen(cols, _ROWS)
    stream = pyte.Stream(screen)
    stream.feed(
        ps1
        + _PROBE_CMD_BASH
        + "\r\n"
        + PWD_MARKER
        + path
        + "\r\n"
        + ps1
    )
    return screen


def _seed_width(screen: Any) -> None:
    """Observe *screen* at its current width, as any frame dump does.

    The live path reaches this through the probe's pre-inject frame dump, not
    through a bare dump of its own (see
    ``test_probe_records_its_write_width_before_writing``).
    """
    dump_frame(screen, strip_probe=False)


# ---------------------------------------------------------------------------
# The guard: a row written through a narrower width stays "full" after widening
# ---------------------------------------------------------------------------


def test_full_rows_keeps_prewiden_row_full() -> None:
    """The echo row still reads as full once the screen has been widened."""
    screen = _echoed_screen()
    _seed_width(screen)
    assert _full_rows(screen, _ROWS)[0] is True

    screen.resize(_ROWS, _WIDE_COLS)

    assert screen.columns == _WIDE_COLS
    # Row 0 was written through column 99 and pyte left it there; the last
    # column of the new width is 199, which the row does not reach.
    assert _full_rows(screen, _ROWS)[0] is True


def test_holds_text_rows_keeps_prewiden_row() -> None:
    """Sibling flag: row_holds_text is asked of the same widths as row_full."""
    screen = _echoed_screen()
    _seed_width(screen)
    assert _holds_text_rows(screen, _ROWS)[0] is True

    screen.resize(_ROWS, _WIDE_COLS)

    assert _holds_text_rows(screen, _ROWS)[0] is True


def test_dump_frame_keeps_stripping_echo_tail_after_widening() -> None:
    """Frame-level: the widened screen still yields prompt-only text."""
    screen = _echoed_screen()
    narrow = dump_frame(screen)
    assert _leaked_probe_lines(narrow) == []
    assert narrow == _PS1.rstrip()

    screen.resize(_ROWS, _WIDE_COLS)
    wide = dump_frame(screen)

    assert _leaked_probe_lines(wide) == []
    assert wide == _PS1.rstrip()


def test_widening_before_any_observation_would_leak() -> None:
    """The record's blind spot, pinned deliberately.

    Nothing has observed this screen - no dump and no resize watch, which is
    installed on first observation - so both the row's paint width and the
    widen are invisible and the probe tail leaks. The live path cannot reach
    this state: the open path dumps a frame before anything can resize the
    screen (see ``test_probe_records_its_write_width_before_writing``), and
    every later resize is watched. The sibling test below pins the dump that
    keeps the live path out (the probe paints only after dumping the frame).
    """
    screen = _echoed_screen()
    screen.resize(_ROWS, _WIDE_COLS)  # widen a screen nothing has observed yet

    assert _full_rows(screen, _ROWS)[0] is False
    assert _leaked_probe_lines(dump_frame(screen)) != []


# ---------------------------------------------------------------------------
# Narrow-then-widen: the destructive direction
# ---------------------------------------------------------------------------


def test_narrow_then_widen_keeps_row_full() -> None:
    """Two resizes with one dump at the end still keep the echo row full.

    pyte pops the cells beyond 100 when narrowing, so row 0 keeps cells 0..99
    and no later dump can see column 119 again. The resize observation records
    the width in between, which is the only evidence that the row's surviving
    last column (99) is a wrap point.
    """
    screen = _echoed_screen(_MID_COLS, _PS1_MID, _LONG_PATH)
    _seed_width(screen)
    assert _full_rows(screen, _ROWS)[0] is True

    screen.resize(_ROWS, _NARROW_COLS)  # resize action 1: cells 100..119 popped
    screen.resize(_ROWS, _WIDE_COLS)  # resize action 2: widens, restores nothing

    # Row 0's last surviving cell is column 99, the last column of the width in
    # between; the final dump is taken at 200.
    assert _full_rows(screen, _ROWS)[0] is True
    assert _holds_text_rows(screen, _ROWS)[0] is True


def test_dump_frame_keeps_stripping_echo_tail_after_narrow_then_widen() -> None:
    """Frame-level: one dump at 200, after 120 -> 100 -> 200, still strips."""
    screen = _echoed_screen(_MID_COLS, _PS1_MID, _LONG_PATH)
    before = dump_frame(screen)
    assert _leaked_probe_lines(before) == []
    assert before == _PS1_MID.rstrip()

    screen.resize(_ROWS, _NARROW_COLS)
    screen.resize(_ROWS, _WIDE_COLS)
    after = dump_frame(screen)

    assert _leaked_probe_lines(after) == []
    assert after == _PS1_MID.rstrip()
    # The record learned the width between the two resizes.
    assert buffer_mod._seen_columns[screen] >= {
        _MID_COLS,
        _NARROW_COLS,
        _WIDE_COLS,
    }


def test_resize_records_width_in_between_without_any_dump() -> None:
    """The resize watch alone is enough: no dump at all, one width left, one
    entered, and the row written at the width left behind stays full."""
    screen = pyte.Screen(_MID_COLS, _ROWS)
    stream = pyte.Stream(screen)
    stream.feed(_PS1_MID + _PROBE_CMD_BASH)  # row 0 filled through column 119

    # First observation installs the watch, as the very first frame dump of a
    # session does on the live path.
    buffer_mod._row_end_indexes(screen, _MID_COLS)
    screen.resize(_ROWS, _NARROW_COLS)
    screen.resize(_ROWS, _WIDE_COLS)

    assert _full_rows(screen, _ROWS)[0] is True


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_seen_widths_records_every_dumped_width() -> None:
    """Both stripped and unstripped dumps observe the width they were taken at."""
    screen = pyte.Screen(_NARROW_COLS, _ROWS)
    assert buffer_mod._seen_columns.get(screen) is None

    dump_frame(screen, strip_probe=False)
    assert buffer_mod._seen_columns[screen] == frozenset({_NARROW_COLS})

    dump_frame(screen)
    assert buffer_mod._seen_columns[screen] == frozenset({_NARROW_COLS})

    screen.resize(_ROWS, _WIDE_COLS)
    dump_frame(screen)
    assert buffer_mod._seen_columns[screen] == frozenset({_NARROW_COLS, _WIDE_COLS})


def test_resize_records_both_widths_it_moves_between() -> None:
    """A resize notes the width left behind and the width entered, no dump due."""
    screen = pyte.Screen(_NARROW_COLS, _ROWS)
    _seed_width(screen)

    screen.resize(_ROWS, _WIDE_COLS)
    assert buffer_mod._seen_columns[screen] == frozenset({_NARROW_COLS, _WIDE_COLS})

    screen.resize(_ROWS, _MID_COLS)
    assert buffer_mod._seen_columns[screen] == frozenset(
        {_MID_COLS, _NARROW_COLS, _WIDE_COLS}
    )


def test_row_end_indexes_unions_seen_and_current() -> None:
    """Widths seen earlier survive: the union is what keeps a row looking full."""
    screen = pyte.Screen(_NARROW_COLS, _ROWS)
    dump_frame(screen, strip_probe=False)
    screen.resize(_ROWS, _WIDE_COLS)

    ends = buffer_mod._row_end_indexes(screen, _WIDE_COLS)

    assert ends == frozenset({_NARROW_COLS - 1, _WIDE_COLS - 1})


# ---------------------------------------------------------------------------
# Live-path invariant: the probe observes its width before it paints
# ---------------------------------------------------------------------------


class _ScriptedPty:
    """PTY that replies with a printed marker line after any write."""

    def __init__(self, path: str = "/home/agent/work", cols: int = _NARROW_COLS) -> None:
        self.cols = cols
        self.rows = 24
        self.cwd: str | None = path
        self._path = path
        self._alive = True
        self._pending = b""
        self.written = bytearray()

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None

    def read(self, max_bytes: int = 8192) -> bytes:
        chunk, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return chunk

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        if data.endswith(b"\r") or data.endswith(b"\n"):
            self._pending += (
                f"\r\n{PWD_MARKER}{self._path}\r\n$ ".encode()
            )
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        total = 0
        while True:
            chunk = self.read()
            if not chunk:
                break
            total += len(chunk)
            if on_data:
                on_data(chunk)
        return total

    def close(self) -> None:
        self._alive = False


def _session(pty: Any, cols: int = _NARROW_COLS, rows: int = 24) -> ScreenSession:
    return ScreenSession(
        id="s",
        ep="local",
        pty=pty,
        cols=cols,
        rows=rows,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        dialect=POSIX_BASH,
    )


def test_probe_records_its_write_width_before_writing(monkeypatch) -> None:
    """The probe dumps the frame at its width before it paints the echo.

    This is what makes ``_seen_columns`` sufficient in the live path: a probe
    row can only ever be written at a width a dump has already observed. If a
    probe ever painted first, the row could outlive the record of its width and
    the widening resize would leak its tail again (see
    ``test_widening_before_any_observation_would_leak``).
    """
    pty = _ScriptedPty()
    sess = _session(pty)
    recorded_at_write: list[bool] = []
    real_write = pty.write

    def spy_write(data: bytes) -> int:
        seen = buffer_mod._seen_columns.get(sess.screen) or frozenset()
        recorded_at_write.append(int(sess.screen.columns) in seen)
        return real_write(data)

    monkeypatch.setattr(pty, "write", spy_write)

    silent_pwd_probe(sess, timeout_s=0.5)

    assert recorded_at_write, "probe never wrote to the PTY"
    assert all(recorded_at_write)


def test_session_resize_widening_keeps_echo_tail_stripped() -> None:
    """Session-level: a widened session still strips a pre-widen probe echo."""
    pty = _ScriptedPty()
    sess = _session(pty)
    # Paint a wrapped probe echo at the narrow width, as the shell would, after
    # the pre-inject dump that the live probe path takes.
    sess.feed(
        (_PS1 + _PROBE_CMD_BASH + "\r\n" + PWD_MARKER + _LONG_PATH + "\r\n").encode()
    )
    before = dump_frame(sess.screen)
    assert _leaked_probe_lines(before) == []

    sess.resize(_WIDE_COLS, 24)

    after = dump_frame(sess.screen)
    assert _leaked_probe_lines(after) == []


def test_session_narrow_then_widen_keeps_echo_tail_stripped() -> None:
    """Session-level: the destructive direction, with one dump at the end.

    Both resizes happen before any dump, as one send carrying two resize
    actions does (resizes take no probe). Only the frame taken at the end is
    served, so nothing but the resize observation can have seen 100.
    """
    pty = _ScriptedPty()
    sess = _session(pty)
    sess.feed(
        (_PS1 + _PROBE_CMD_BASH + "\r\n" + PWD_MARKER + _LONG_PATH + "\r\n").encode()
    )
    assert _leaked_probe_lines(dump_frame(sess.screen)) == []

    sess.resize(_NARROW_COLS - 10, 24)
    sess.resize(_WIDE_COLS, 24)

    after = dump_frame(sess.screen)
    assert _leaked_probe_lines(after) == []


# ---------------------------------------------------------------------------
# End to end: a real PTY, resized mid-session
# ---------------------------------------------------------------------------

_BASH = "/bin/bash"
_HAS_BASH = sys.platform != "win32" and Path(_BASH).is_file()


@pytest.mark.skipif(not _HAS_BASH, reason="needs a POSIX PTY and /bin/bash")
def test_real_pty_probe_tail_stays_stripped_after_widening() -> None:
    """Real bash PTY at 100 cols, widened to 200 after the probe echo landed.

    The widening is the trigger: pyte keeps the echo row at its 100-column
    width, so a fullness check made against the current 200 columns alone would
    call the row short and keep the wrapped tail it had been dropping.
    """
    from mcp_remote_control.screen.local_pty import LocalPty
    from mcp_remote_control.screen.send import execute_send

    pty = LocalPty(
        cols=_NARROW_COLS,
        rows=32,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"PS1": _PS1, "TERM": "xterm-256color"},
    )
    sess = ScreenSession(
        id="s",
        ep="local",
        pty=pty,
        cols=_NARROW_COLS,
        rows=32,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        dialect=POSIX_BASH,
    )
    try:
        sess.drain(1.2)
        execute_send(
            sess,
            actions=[{"type": "text", "text": "true", "submit": True}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
        )
        execute_send(
            sess,
            actions=[{"type": "resize", "cols": _WIDE_COLS, "rows": 32}],
            wait={"until": "deadline", "timeout_ms": 0},
        )
        # A send whose text changes the frame, so the body is not omitted.
        outcome = execute_send(
            sess,
            actions=[{"type": "text", "text": "abc"}],
            wait={"until": "deadline", "timeout_ms": 0},
        )
        assert outcome.frame is not None
        assert _leaked_probe_lines(outcome.frame) == []
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.skipif(not _HAS_BASH, reason="needs a POSIX PTY and /bin/bash")
def test_real_pty_probe_tail_stays_stripped_after_narrow_then_widen() -> None:
    """Real bash PTY at 120 cols: one send narrows to 100, then widens to 200.

    Resize actions take no probe (``_NO_PROBE_TYPES``), so no frame is dumped
    between the two resizes: pyte pops the echo row's cells 100..119 at the
    narrowing step, and the only dump of that send is taken at 200. The echo
    row's surviving last column is 99 - the last column of a width nothing but
    the resize observation records - and without it the wrapped tail is served
    to the Agent as if it were output.
    """
    from mcp_remote_control.screen.local_pty import LocalPty
    from mcp_remote_control.screen.send import execute_send

    pty = LocalPty(
        cols=_MID_COLS,
        rows=32,
        shell=_BASH,
        cwd="/tmp",
        argv=[_BASH, "--norc", "--noprofile", "-i"],
        env={"PS1": _PS1_MID, "TERM": "xterm-256color"},
    )
    sess = ScreenSession(
        id="s",
        ep="local",
        pty=pty,
        cols=_MID_COLS,
        rows=32,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        dialect=POSIX_BASH,
    )
    try:
        sess.drain(1.2)
        execute_send(
            sess,
            actions=[{"type": "text", "text": "true", "submit": True}],
            wait={"until": "idle", "idle_ms": 300, "timeout_ms": 5000},
        )
        assert _leaked_probe_lines(sess.shot()["frame"]) == []

        execute_send(
            sess,
            actions=[
                {"type": "resize", "cols": _NARROW_COLS, "rows": 32},
                {"type": "resize", "cols": _WIDE_COLS, "rows": 32},
            ],
            wait={"until": "deadline", "timeout_ms": 0},
        )
        # A send whose text changes the frame, so the body is not omitted.
        outcome = execute_send(
            sess,
            actions=[{"type": "text", "text": "abc"}],
            wait={"until": "deadline", "timeout_ms": 0},
        )
        assert sess.screen.columns == _WIDE_COLS
        assert outcome.frame is not None
        assert _leaked_probe_lines(outcome.frame) == []
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
