"""pyte ScreenBuffer helpers: frame dump, cursor, hash, text search.

Owns geometry bounds, Agent-facing frame serialization, cwd-probe marker
stripping, and visible-text search over a pyte screen.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, overload

# Geometry bounds and shell seed; adaptive grow/memory lives in geometry.py.
SEED_SHELL_COLS = 160
SEED_SHELL_ROWS = 48
MIN_COLS = 100
MIN_ROWS = 30
MAX_COLS = 240
MAX_ROWS = 64

DEFAULT_TERM = "xterm-256color"
DEFAULT_COLORTERM = "truecolor"

# Silent cwd-probe marker. Stripped from Agent-facing frames.
PWD_MARKER = "__MRC_PWD__:"
_PWD_LINE_RE = re.compile(rf"^\s*{re.escape(PWD_MARKER)}(.*)\s*$")

# xterm mouse tracking private modes (DECSET). pyte stores private as mode<<5.
_MOUSE_DECSET = (1000, 1002, 1003, 1005, 1006, 1015)


def clamp_geometry(cols: int, rows: int) -> tuple[int, int]:
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
    return cols, rows


def dump_frame(
    screen: Any,
    *,
    trim_trailing_ws: bool = True,
    strip_trailing_empty: bool = True,
    strip_probe: bool = True,
) -> str:
    """Serialize visible pyte screen to newline-joined text lines.

    Default Agent-facing policy: trim per-line trailing whitespace and strip
    trailing empty lines to save tokens while keeping layout signal.
    *strip_probe* drops internal cwd-probe marker lines so they never reach
    the Agent frame.
    """
    lines: list[str] = []
    display = list(screen.display)
    for raw in display:
        line = raw if isinstance(raw, str) else str(raw)
        if trim_trailing_ws:
            line = line.rstrip(" \t")
        lines.append(line)

    if strip_probe:
        lines = strip_probe_lines(lines)

    if strip_trailing_empty:
        while lines and lines[-1] == "":
            lines.pop()

    return "\n".join(lines)


@overload
def strip_probe_lines(lines: str) -> str: ...


@overload
def strip_probe_lines(lines: list[str]) -> list[str]: ...


def strip_probe_lines(lines: list[str] | str) -> list[str] | str:
    """Remove silent cwd-probe marker lines from a frame or line list."""
    if isinstance(lines, str):
        kept = [
            ln
            for ln in lines.splitlines()
            if PWD_MARKER not in ln
        ]
        return "\n".join(kept)
    return [ln for ln in lines if PWD_MARKER not in ln]


def parse_pwd_marker(text: str) -> str | None:
    """Extract absolute path from a buffer/frame containing ``__MRC_PWD__:``.

    Prefers the **last** valid marker line so repeated probes (open + send)
    are not stuck on a stale earlier value still visible on the screen.
    """
    if PWD_MARKER not in text:
        return None
    last: str | None = None
    for raw in text.splitlines():
        extracted: str | None = None
        m = _PWD_LINE_RE.match(raw)
        if m:
            captured = m.group(1)
            # re.Match.group(n) is typed str | None in some stubs.
            candidate = (captured if isinstance(captured, str) else "").strip()
            parts = candidate.split()
            extracted = parts[0] if parts else candidate
        elif PWD_MARKER in raw:
            candidate = raw.split(PWD_MARKER, 1)[1].strip()
            parts = candidate.split()
            extracted = parts[0] if parts else candidate
        if not extracted:
            continue
        # Ignore the typed command line where $(pwd) has not expanded yet.
        if "$(" in extracted or extracted.startswith("%"):
            continue
        if _path_looks_absolute(extracted):
            last = extracted
    return last


def _path_looks_absolute(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/"):
        return True
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
        return True
    if path.startswith("\\\\"):
        return True
    return False


def frame_hash(frame: str, *, n: int = 8) -> str:
    """Short hex hash of *frame* for unchanged detection later."""
    digest = hashlib.sha256(frame.encode("utf-8", errors="replace")).hexdigest()
    return digest[:n]


def cursor_rc(screen: Any) -> tuple[int, int]:
    """Return 0-based (row, col) cursor from a pyte Screen."""
    # pyte: cursor.y = row, cursor.x = col
    row = int(getattr(screen.cursor, "y", 0) or 0)
    col = int(getattr(screen.cursor, "x", 0) or 0)
    return row, col


def format_cur(screen: Any) -> str:
    r, c = cursor_rc(screen)
    return f"{r},{c}"


def mouse_tracking_enabled(screen: Any) -> bool:
    """True when xterm mouse tracking appears enabled on *screen* (pyte modes)."""
    modes = getattr(screen, "mode", None)
    if not modes:
        return False
    for m in _MOUSE_DECSET:
        if m in modes or (m << 5) in modes:
            return True
    return False


def find_text(
    screen: Any,
    text: str,
    *,
    nth: int = 1,
    row: int | None = None,
    col: int | None = None,
) -> tuple[int, int] | None:
    """Locate visible substring on *screen*; return 0-based (row, col) of match start.

    *nth* is 1-based among matches in row-major order. Optional *row*/*col*
    restrict search to that cell's line starting at *col* (or full line).
    """
    needle = str(text)
    if not needle:
        return None
    try:
        n = int(nth)
    except (TypeError, ValueError):
        n = 1
    n = max(n, 1)

    display = list(screen.display)
    matches: list[tuple[int, int]] = []
    for r, raw in enumerate(display):
        if row is not None and r != int(row):
            continue
        line = raw if isinstance(raw, str) else str(raw)
        # Search the full line so column indices remain valid.
        search_line = line
        start = 0
        if row is not None and col is not None and r == int(row):
            start = max(0, int(col))
        while True:
            idx = search_line.find(needle, start)
            if idx < 0:
                break
            matches.append((r, idx))
            start = idx + 1

    if n > len(matches):
        return None
    return matches[n - 1]
