"""pyte ScreenBuffer helpers: frame dump, cursor, hash, text search.

Owns geometry bounds, Agent-facing frame serialization, cwd-probe stripping
(the echoed command and the printed marker line, wrap units included), and
visible-text search over a pyte screen.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Sequence
from typing import Any, overload
from weakref import WeakKeyDictionary, WeakSet

from mcp_remote_control.shell.dialect import DIALECT_PROBES

try:
    # The width test pyte's own render loop applies to decide which cells of a
    # row a display line keeps; reusing it keeps the walk below dropping
    # exactly the cells ``screen.display`` drops.
    from pyte.screens import wcwidth as _char_width
except ImportError:  # pragma: no cover - pyte is a declared dependency
    try:
        # pyte takes its widths from this package, so the same table answers.
        from wcwidth import wcwidth as _char_width
    except ImportError:
        _char_width = None

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

# Probe command templates (shell dialect registry). The shell echoes back the
# command the probe typed, so an echo row carries a slice of one of these - and
# nothing else on screen can. Used to decide whether a full-width row above the
# marker row is echo text, which the "reached its last column" signal alone
# cannot: program output and the Agent's own command echo are full-width rows
# too (see ``_probe_echo_row``).
_PROBE_ECHO_TEMPLATES: tuple[str, ...] = tuple(
    spec.cmd
    for spec in DIALECT_PROBES.values()
    if spec is not None and spec.enabled and spec.cmd
)
# Shortest template slice accepted as echo evidence for a row that holds no
# prompt in front of it; below this, coincidence with unrelated text is no
# longer implausible.
_MIN_ECHO_FRAGMENT = 8

# xterm mouse tracking private modes (DECSET). pyte stores private as mode<<5.
_MOUSE_DECSET = (1000, 1002, 1003, 1005, 1006, 1015)

# xterm alternate screen private modes (DECSET).
# 47 = classic alt buffer; 1047 = alt buffer; 1049 = alt + save/restore (vim/less/htop).
_ALT_SCREEN_DECSET = (47, 1047, 1049)


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
    *strip_probe* drops internal cwd-probe text: the echoed probe command and
    the printed marker line, each with the display rows it wrapped onto - so no
    probe text reaches the Agent frame.
    """
    lines: list[str] = []
    display = list(screen.display)
    for raw in display:
        line = raw if isinstance(raw, str) else str(raw)
        if trim_trailing_ws:
            line = line.rstrip(" \t")
        lines.append(line)

    # Measured even when the frame is not stripped: this dump observes the
    # width the rows on screen were written under (see ``_seen_columns``).
    row_full = _full_rows(screen, len(display))
    if strip_probe:
        lines = strip_probe_lines(
            lines,
            row_full=row_full,
            row_holds_text=_holds_text_rows(screen, len(display)),
        )

    if strip_trailing_empty:
        while lines and lines[-1] == "":
            lines.pop()

    return "\n".join(lines)


# Column counts each live screen has been observed at. A row written through
# the last column of an earlier geometry keeps that cell after a widening
# resize - pyte's Screen.resize only pops cells when narrowing - so "did this
# row reach its last column" must be asked of every width the row could have
# been written under, not just the current one.
#
# Two things observe a width: a frame dump (its own width) and a resize (the
# width it leaves, which is the one rows on screen were painted under, and the
# width it enters, which is the one the rows it did not pop survive at; see
# ``_watch_resizes``). Dumps alone are not enough: one send can carry two
# resize actions with no dump between them, so a width the screen passed
# through would never be seen - and a narrow-then-widen erases the last column
# of a full row for good, so the record could never learn it afterwards.
# Nothing else paints the screen without one of the two observations first on
# the live paths: the cwd probe dumps a frame immediately before it writes,
# and the open / geometry-adapt paths dump at every width they pass through.
#
# Weak keys: the record must not outlive the screen it describes.
_seen_columns: WeakKeyDictionary[Any, frozenset[int]] = WeakKeyDictionary()

# Screens whose ``resize`` has already been wrapped (see ``_watch_resizes``).
# Weak so a dropped screen stays collectable.
_watched_screens: WeakSet[Any] = WeakSet()


def _note_width(screen: Any, columns: int) -> frozenset[int]:
    """Record *columns* as a width *screen* has been observed at.

    Returns the widths recorded so far, *columns* included. A screen that
    cannot be weakly referenced is not tracked at all; callers then judge it
    on the width they hold.
    """
    try:
        seen = _seen_columns.get(screen)
        recorded = (seen or frozenset()) | {columns}
        _seen_columns[screen] = recorded
        return recorded
    except TypeError:
        return frozenset({columns})


def _watch_resizes(screen: Any) -> None:
    """Make every later resize of *screen* note the widths it moves between.

    pyte's ``Screen.resize`` pops the cells beyond a narrowed width with no
    trace left behind, so a narrowing followed by a widening destroys the
    "reached the last column" evidence of rows written at the wider one - and
    a dump taken only after both resizes cannot recover the width in between.
    Noting the width on the way into a resize and on the way out of it is what
    keeps ``_seen_columns`` complete: the width left behind is the one rows on
    screen were painted under, the width entered is the one rows that were not
    popped survive at.

    Installed lazily, on first observation of a screen; the live open path
    dumps a frame before anything can resize it. A screen whose ``resize``
    cannot be replaced is left alone - dumps still record their own width,
    only the widths moved between without a dump are lost.
    """
    try:
        if screen in _watched_screens:
            return
        inner = screen.resize
    except (AttributeError, TypeError):
        return
    if not callable(inner):
        return

    @functools.wraps(inner)
    def resize(lines: int | None = None, columns: int | None = None) -> Any:
        _note_width(screen, int(getattr(screen, "columns", 0) or 0))
        result = inner(lines, columns)
        _note_width(screen, int(getattr(screen, "columns", 0) or 0))
        return result

    try:
        screen.resize = resize
        _watched_screens.add(screen)
    except (AttributeError, TypeError):
        return


def _row_end_indexes(screen: Any, columns: int) -> frozenset[int]:
    """Last-column indexes a row on *screen* could have been written through.

    The current width always counts; so do the widths the screen was observed
    at before - every frame dump and every resize, which notes both the width
    it leaves and the width it enters (see ``_seen_columns`` and
    ``_watch_resizes``). The union is only as complete as that record, so it
    covers rows painted by a writer that dumped or resized first. A screen
    that cannot be weakly referenced is judged on the current width alone.
    """
    seen = _note_width(screen, columns)
    _watch_resizes(screen)
    return frozenset({columns - 1} | {c - 1 for c in seen})


def _full_rows(screen: Any, count: int) -> list[bool] | None:
    """Per-row flag: did the program write row *y* through its last column?

    Read from the screen, not from the text: ``display`` pads short rows with
    blanks, so a row whose wrap point holds a space looks exactly like an early
    newline. A written last column is *necessary* for a wrap but not proof of
    one - pyte stores explicit cells for cells a program erased to end of line
    (``ESC[K``) and for trailing blanks, so a row can reach its last column
    without anything continuing below it. Callers use the flag to bound probe
    text, where an over-eager bound is corrected by the text of the rows it
    groups; where nothing corrects it, ask ``_holds_text_rows`` instead.
    Returns ``None`` when the screen does not expose a pyte buffer.
    """
    buffer = getattr(screen, "buffer", None)
    columns = getattr(screen, "columns", None)
    if buffer is None or not columns:
        return None
    ends = _row_end_indexes(screen, int(columns))
    full: list[bool] = []
    for y in range(count):
        try:
            row = buffer[y]
            full.append(any(end in row for end in ends))
        except (IndexError, TypeError):
            return None
    return full


def _holds_text_rows(screen: Any, count: int) -> list[bool] | None:
    """Per-row flag: does the row's last written column hold a character?

    Narrower than ``_full_rows`` on purpose. That one answers "did the program
    reach this row's last column", which an erase to end of line (``ESC[K``) or
    a run of trailing blanks also does; this answers "...and is there still
    something there", which is what a line that continues onto the row below
    looks like. A row that merely ran out to the last column with padding
    ends its visible text there, so the row below it starts a new line.
    Returns ``None`` when the screen does not expose a pyte buffer.
    """
    buffer = getattr(screen, "buffer", None)
    columns = getattr(screen, "columns", None)
    if buffer is None or not columns:
        return None
    ends = _row_end_indexes(screen, int(columns))
    held: list[bool] = []
    for y in range(count):
        try:
            row = buffer[y]
            held.append(any(_cell_holds_text(row[end]) for end in ends if end in row))
        except (IndexError, TypeError):
            return None
    return held


def _cell_holds_text(cell: Any) -> bool:
    """True when a pyte buffer cell holds a visible (non-blank) character."""
    data = cell if isinstance(cell, str) else getattr(cell, "data", "")
    return bool(str(data).strip())


@overload
def strip_probe_lines(
    lines: str,
    *,
    row_full: Sequence[bool] | None = None,
    row_holds_text: Sequence[bool] | None = None,
) -> str: ...


@overload
def strip_probe_lines(
    lines: list[str],
    *,
    row_full: Sequence[bool] | None = None,
    row_holds_text: Sequence[bool] | None = None,
) -> list[str]: ...


def strip_probe_lines(
    lines: list[str] | str,
    *,
    row_full: Sequence[bool] | None = None,
    row_holds_text: Sequence[bool] | None = None,
) -> list[str] | str:
    """Remove silent cwd-probe rows from a frame or line list.

    Dropping the marker row alone is not enough: the shell echoes the injected
    probe command, and when that echo is wider than the terminal it occupies
    several display rows, only one of which holds the marker text - the wrap
    boundary can even fall inside the marker. The echo is therefore removed as
    a unit: *row_full* (see ``_full_rows``) marks rows the program filled to
    their last column, and such a row continues onto the row below it, so rows
    are grouped into wrap units and the probe text inside a unit is dropped.
    Without *row_full* there are no units, and only marker rows are removed.

    Removal starts where the echoed command starts, not where the unit does: a
    full-width row above the echo is not echo text (the Agent's own command
    echo, or program output that reached the last column), so rows are only
    added upwards while they carry probe template text (``_probe_echo_row``).

    Removal ends with the printed marker line, the path the probe echoed out
    (``_PWD_LINE_RE`` shape). Rows below it in the unit are real content, and a
    full row alone cannot disprove that (``ESC[K`` and trailing blanks both
    reach the last column). When the printed line is itself wider than the
    terminal, the rows it wrapped onto carry its tail and are dropped with it -
    decided on *row_holds_text* and ``continues_printed_path``; without that
    flag the fill signal alone cannot tell a wrapped tail from the next line,
    and dropping a real row on it hands the Agent an incomplete frame.
    """
    if isinstance(lines, str):
        return "\n".join(
            strip_probe_lines(
                lines.splitlines(),
                row_full=row_full,
                row_holds_text=row_holds_text,
            )
        )
    n = len(lines)
    kept: list[str] = []
    i = 0
    while i < n:
        end = _wrap_unit_end(i, n, row_full)
        unit = lines[i:end]
        joined = "".join(seg.rstrip() for seg in unit)
        if PWD_MARKER not in joined:
            kept.append(lines[i])
            i += 1
            continue
        start = _echo_start_index(unit, row_full, i)
        out_at = _marker_output_index(unit)
        if out_at is None:
            stop = end
        else:
            stop = i + out_at + 1
            # The probe printed one path line. While it fills its last column
            # with a character the line continues onto the row below, and those
            # rows carry the path's tail. A row that has started something else
            # ends it; see ``continues_printed_path``.
            printed = lines[i + out_at]
            while (
                stop < end
                and row_full
                and row_holds_text
                and row_full[stop - 1]
                and row_holds_text[stop - 1]
                and continues_printed_path(lines[stop], printed)
            ):
                stop += 1
        kept.extend(lines[i:start])
        kept.extend(lines[stop:end])
        i = end
    return kept


def continues_printed_path(row: str, printed: str) -> bool:
    """True when *row* reads as the wrap tail of the printed path line.

    The probe prints one path token, so its tail carries no whitespace - and a
    row that does carry whitespace has begun something else: the shell's next
    prompt, or output. A path may itself contain spaces, in which case the
    token test cannot separate a tail from a prompt and only a row that adds no
    whitespace is accepted. Dropping what may be a prompt row is the cheaper
    error here: keeping probe text hands the Agent a path it never asked for,
    and this row is the difference between the marker line and the row below.
    """
    text = row.rstrip()
    if not text:
        return False
    if any(ch.isspace() for ch in text):
        return any(ch.isspace() for ch in printed.rstrip())
    return True


def _echo_start_index(
    unit: Sequence[str],
    row_full: Sequence[bool] | None,
    base: int,
) -> int:
    """Absolute index of the echoed probe command's first row within *unit*.

    Anchored on the row where the first marker occurrence starts, then walked
    upwards one row at a time while both hold: the row above wraps into the one
    below (they are one display line), and that row carries probe template text
    (``_probe_echo_row``). The template test is what keeps an Agent command
    echo or a full-width output row above the echo - neither of which carries
    template text - in the frame.

    ``base`` when the unit's marker is not found there, i.e. the whole unit is
    treated as probe text, which is the pre-existing behaviour.
    """
    at = _marker_start_index(unit)
    if at is None:
        return base
    start = base + at
    while start > base:
        if row_full is None or not row_full[start - 1]:
            break
        if not _probe_echo_row(unit[start - base - 1]):
            break
        start -= 1
    return start


def _marker_start_index(unit: Sequence[str]) -> int | None:
    """Index within *unit* of the row the first marker occurrence starts on.

    The marker is a token, so it is normally found in one row's text. When the
    wrap boundary falls inside it no row holds it whole and only the pair of
    adjacent rows does; the row the occurrence *starts* on is the answer then,
    since a token spanning a wrap can only span one boundary. ``None`` when the
    unit holds no marker at all.
    """
    for idx, row in enumerate(unit):
        if row.find(PWD_MARKER) >= 0:
            return idx
        if idx + 1 < len(unit):
            across = row + unit[idx + 1]
            at = across.find(PWD_MARKER)
            if 0 <= at < len(row) and at + len(PWD_MARKER) > len(row):
                return idx
    return None


def _probe_echo_row(text: str) -> bool:
    """True when *text* can only be the shell's echo of an injected probe.

    Two shapes, matching the two positions a row can hold in the echo:

    * the echo's **first** row - the prompt, then the command template from its
      start. The prompt string is not known here, so every split point is
      tried: the template must *begin* with what follows it.
    * a **middle** row - the template continues from the row above, so the row
      holds a slice of the template with no prompt in front of it.

    A row carrying no template text is not echo text, however full it is.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    for template in _PROBE_ECHO_TEMPLATES:
        head = template[0]
        for start, ch in enumerate(stripped):
            if ch == head and template.startswith(stripped[start:]):
                return True
        if len(stripped) >= _MIN_ECHO_FRAGMENT and stripped in template:
            return True
    return False


def _wrap_unit_end(start: int, count: int, row_full: Sequence[bool] | None) -> int:
    """Exclusive end of the wrap unit beginning at row *start*.

    A row that wrote its last column continues onto the next row, so the unit
    grows while the last row it holds is full. Without *row_full* every row is
    its own unit.
    """
    end = start + 1
    if not row_full:
        return end
    limit = min(count, len(row_full))
    while end < limit and row_full[end - 1]:
        end += 1
    return end


def _marker_output_index(unit: Sequence[str]) -> int | None:
    """Index within *unit* of the last row holding a valid marker output line.

    An *unexpanded* echo row also carries the marker text, but its ``$(pwd...)``
    was never substituted, so no path is found and the row does not count as
    output. Returns ``None`` when the unit holds no marker output at all - the
    whole unit is then echo and is dropped.
    """
    found: int | None = None
    for idx, row in enumerate(unit):
        if collect_pwd_markers(row):
            found = idx
    return found


def collect_pwd_markers(text: str) -> list[str]:
    """Return every valid ``__MRC_PWD__:`` path in *text*, in screen order.

    Extraction keeps spaces in a path, unwraps quoted paths, and ignores
    unexpanded ``$(pwd)`` / ``%CD%`` command lines. The silent probe snapshots
    this list before inject and requires a new confirmation; a caller that
    wants the newest cwd takes the tail of the list.
    """
    if not text or PWD_MARKER not in text:
        return []
    found: list[str] = []
    for raw in text.splitlines():
        extracted: str | None = None
        m = _PWD_LINE_RE.match(raw)
        if m:
            captured = m.group(1)
            # re.Match.group(n) is typed str | None in some stubs.
            candidate = (captured if isinstance(captured, str) else "").strip()
            extracted = _extract_pwd_path(candidate)
        elif PWD_MARKER in raw:
            candidate = raw.split(PWD_MARKER, 1)[1].strip()
            extracted = _extract_pwd_path(candidate)
        if not extracted:
            continue
        # Ignore the typed command line where $(pwd) has not expanded yet.
        if "$(" in extracted or extracted.startswith("%"):
            continue
        if _path_looks_absolute(extracted):
            found.append(extracted)
    return found


def _extract_pwd_path(candidate: str) -> str:
    """Normalize marker remainder into a full path (spaces preserved).

    * Line-oriented probes print ``__MRC_PWD__:<path>`` alone; take the whole
      remainder after strip (do **not** split on whitespace).
    * If the remainder is single- or double-quoted, unwrap the quotes and
      return the interior (spaces inside quotes kept).
    """
    if not candidate:
        return ""
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "'\"":
        return candidate[1:-1]
    # Quoted path followed by trailing junk (rare): take interior of first quote.
    if candidate[0] in "'\"":
        q = candidate[0]
        end = candidate.find(q, 1)
        if end > 0:
            return candidate[1:end]
    return candidate


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


def mouse_tracking_enabled(screen: Any) -> bool:
    """True when xterm mouse tracking appears enabled on *screen* (pyte modes)."""
    modes = getattr(screen, "mode", None)
    if not modes:
        return False
    for m in _MOUSE_DECSET:
        if m in modes or (m << 5) in modes:
            return True
    return False


def alt_screen_active(screen: Any) -> bool:
    """True when xterm alternate screen buffer appears enabled (pyte modes).

    vim / less / htop / most full-screen TUIs enter DECSET 1049 (or 47/1047).
    pyte records private modes as ``mode << 5`` in ``screen.mode``.
    """
    modes = getattr(screen, "mode", None)
    if not modes:
        return False
    for m in _ALT_SCREEN_DECSET:
        if m in modes or (m << 5) in modes:
            return True
    return False


def live_tui_modes(screen: Any) -> bool:
    """True when buffer shows TUI-like terminal modes (alt-screen and/or mouse)."""
    return alt_screen_active(screen) or mouse_tracking_enabled(screen)


def detect_surface_from_screen(screen: Any) -> str | None:
    """Surface label implied by live pyte modes, or ``None`` when shell-like.

    Prefers ``alt_screen`` when the alternate buffer is active; otherwise
    ``tui`` when only mouse tracking is on. Callers map ``None`` -> shell.
    """
    if alt_screen_active(screen):
        return "alt_screen"
    if mouse_tracking_enabled(screen):
        return "tui"
    return None


def _cell_columns(screen: Any, row: int, line: str) -> list[int] | None:
    """Cell column each character of a display *line* was painted in.

    ``screen.display`` assembles a row by walking the pyte buffer and dropping
    the stub cell a wide character spans. That drop is decided by the width of
    the previous cell alone and never by the dropped cell's data: a character
    written over a stub is dropped with it, even though its data would match
    the line at that offset. A display index therefore counts characters while
    the terminal - and with it every click report and cursor move - counts
    cells: a match found in the display string sits short of its own cell by
    one per wide character painted before it. Repeating pyte's walk (same width
    test, same unconditional drop) maps every index back to the column it came
    from. Combining characters need no case of their own: pyte normalizes a
    mark into the cell it follows, stub included, and a mark left in a dropped
    stub is absent from the display exactly as it is absent here.

    Returns ``None`` when the screen exposes no usable pyte buffer, when no
    width test is importable, or when the walk does not reproduce *line*;
    callers then read a display index as a column, which is exact for a line
    that paints no wide character.
    """
    if _char_width is None:
        return None
    buffer = getattr(screen, "buffer", None)
    if buffer is None:
        return None
    try:
        cells = buffer[row]
        width = int(getattr(screen, "columns", 0) or 0) or len(cells)
    except (IndexError, KeyError, TypeError):
        return None
    columns: list[int] = []
    at = 0
    skip = False
    for x in range(width):
        try:
            data = getattr(cells[x], "data", "")
        except (IndexError, KeyError, TypeError):
            return None
        if not isinstance(data, str):
            data = str(data)
        if skip:
            skip = False
            continue
        if not data or not line.startswith(data, at):
            return None
        try:
            skip = _char_width(data[0]) == 2
        except (TypeError, ValueError):
            return None
        columns.extend([x] * len(data))
        at += len(data)
    if at != len(line):
        return None
    return columns


def _index_at_cell(columns: list[int] | None, line: str, cell: int) -> int:
    """Display index the search starts at to cover cell *cell* and beyond.

    Without a cell map a column reads as its own index, which is exact for a
    line that paints no wide character; a column past the last rendered one
    starts after the line, where nothing can match.
    """
    if columns is None:
        return max(0, cell)
    for idx, column in enumerate(columns):
        if column >= cell:
            return idx
    return len(line)


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
    Both the returned column and *col* are pyte cell coordinates - the unit a
    click report and a cursor move address - so the match maps back through the
    buffer rather than reporting its display-string index (see
    ``_cell_columns``).
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
        columns = _cell_columns(screen, r, line)
        # Search the full line so column indices remain valid.
        search_line = line
        start = 0
        if row is not None and col is not None and r == int(row):
            start = _index_at_cell(columns, line, int(col))
        while True:
            idx = search_line.find(needle, start)
            if idx < 0:
                break
            matches.append((r, idx if columns is None else columns[idx]))
            start = idx + 1

    if n > len(matches):
        return None
    return matches[n - 1]
