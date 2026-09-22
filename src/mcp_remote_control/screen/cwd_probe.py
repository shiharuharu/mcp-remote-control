"""Silent shell cwd probe for interactive screen sessions.

After settle/send when ``surface=shell``, inject a one-shot command that prints
``__MRC_PWD__:<abs>``; parse it, update ``session.cwd``, and rely on
``dump_frame(strip_probe=True)`` to drop the whole wrapped echo unit and the
marker output row, so neither the marker nor the echoed command reaches the
Agent frame.

Dialect choice drives the probe template: unbound sessions get a best-effort
zsh-compatible default; explicit ``unknown`` (or dialects without a template)
skip inject entirely.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from mcp_remote_control.screen.actions import action_truthy, actions_include_submit
from mcp_remote_control.screen.buffer import (
    PWD_MARKER,
    _full_rows,
    collect_pwd_markers,
    continues_printed_path,
    detect_surface_from_screen,
    dump_frame,
    live_tui_modes,
)
from mcp_remote_control.screen.keys import encode_key, encode_text
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.shell.dialect import (
    CMD,
    POSIX_BASH,
    POSIX_BUSYBOX,
    POSIX_SH,
    POSIX_ZSH,
    POWERSHELL,
    probe_cmd_for_dialect,
    resolve_dialect,
)

# Named probe-command aliases. Source of truth: shell.dialect.
_PROBE_CMD_POSIX = probe_cmd_for_dialect(POSIX_ZSH) or ""
_PROBE_CMD_BASH = probe_cmd_for_dialect(POSIX_BASH) or ""
_PROBE_CMD_CMD = probe_cmd_for_dialect(CMD) or ""
_PROBE_CMD_PWSH = probe_cmd_for_dialect(POWERSHELL) or ""
_PROBE_CMD_SH = probe_cmd_for_dialect(POSIX_SH) or ""
_PROBE_CMD_BUSYBOX = probe_cmd_for_dialect(POSIX_BUSYBOX) or ""

# Heuristic: text action that looks like `cd <path>` (+ submit).
_CD_RE = re.compile(
    r"^\s*cd\s+(?P<path>(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+))\s*$",
    re.IGNORECASE,
)

# Surfaces where a silent shell probe (ctrl+u + echo) is never safe.
_NO_PROBE_SURFACES: frozenset[str] = frozenset(
    {
        "tui",
        "tui_heavy",
        "alt",
        "alt_screen",
        "pager",
    }
)

# Surfaces we may set from live pyte modes (and may restore to shell when modes clear).
_DYNAMIC_TUI_SURFACES: frozenset[str] = frozenset(
    {
        "tui",
        "tui_heavy",
        "alt",
        "alt_screen",
    }
)

# Last-line heuristics: password / passphrase / OTP style prompts.
# Scoped to the bottom of the buffer so mid-frame "password" in docs/logs
# does not false-positive skip every frame.
_PASSWORD_LINE_RE = re.compile(
    r"(?i)"
    r"("
    r"(?:^|.*\s)(?:password|passphrase|passwd)\s*[:=]?\s*$"
    r"|\[sudo\]\s*.*password"
    r"|password\s+for\s+\S+"
    r"|'s\s+password\s*:"
    r"|enter\s+(?:the\s+)?(?:password|passphrase|pin|code|otp)\b"
    r"|(?:current|old|new|login|unix)\s+password"
    r"|verification\s+code"
    r"|(?:one[-\s]?time|otp|2fa|mfa)\s*(?:code|password|token)?\s*:?\s*$"
    r"|passphrase\s+for\s+"
    r")"
)

# Confirm / yes-no / continue prompts waiting for interactive answer.
_CONFIRM_LINE_RE = re.compile(
    r"(?i)"
    r"("
    r"\[y/n\]|\[Y/n\]|\[y/N\]|\[yes/no\]|\[Yes/No\]"
    r"|\(y/n\)|\(Y/n\)|\(y/N\)|\(yes/no\)"
    r"|yes\s*/\s*no"
    r"|do you want to continue"
    r"|are you sure"
    r"|proceed\s*\?"
    r"|overwrite\s*\?"
    r"|press\s+(?:any\s+key|enter|return)\s+to\s+continue"
    r"|continue\??\s*[\[(]"
    r")"
)

# zsh-style named secondary prompts (PS2) and bash bare ">".
# Match at line start so primary prompts like "PS C:\\>" do not false-positive.
_PS2_LINE_RE = re.compile(
    r"(?i)^\s*(?:"
    r"(?:heredoc|quote|dquote|bquote|cmdsubst|mathsubst|"
    r"for|while|if|then|else|elif|do|case|select|function|"
    r"pipe|cond|forall|foreach|repeat|until|coproc|"
    r"cmdand|cmdor|glob|subst)\s*>"
    r"|>"
    r")(?:\s|$)"
)

# Heredoc opener: <<TOKEN / <<-TOKEN / <<'TOKEN' / <<"TOKEN".
# Requires a non-`<` (or start) before `<<` so `<<<` here-strings are ignored.
_HEREDOC_OPEN_RE = re.compile(
    r"(?:^|[^<])<<-?\s*(?:'([^'\n]+)'|\"([^\n\"]+)\"|\\?([^\s;|&)<>]+))"
)

# Last line is a REPL / debugger prompt (python, pdb, gdb-style).
# ctrl+u would wipe the REPL line or inject a shell probe into the debugger.
_REPL_PROMPT_RE = re.compile(
    r"^\s*(?:"
    r">>>"
    r"|\.\.\."
    r"|\((?:Pdb|gdb|lldb|ipdb)\)"
    r"|(?:ipdb|pdb)>"
    r")(?:\s|$)",
    re.IGNORECASE,
)


def is_shell_surface(session: ScreenSession) -> bool:
    surf = (session.surface or "").lower()
    if surf not in ("shell", "", "unknown") or session.open_mode != "shell":
        return False
    # Live buffer modes beat a stale open-time surface=shell (vim/htop still
    # look like open_mode=shell until surface is refreshed).
    try:
        if live_tui_modes(session.screen):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def refresh_session_surface(session: ScreenSession) -> str:
    """Update ``session.surface`` from live pyte modes; return current surface.

    open_mode=shell sessions:
    - alt-screen / mouse tracking -> ``alt_screen`` / ``tui``
    - modes clear after a dynamic TUI label -> restore ``shell``
    - explicit static labels outside the dynamic set (e.g. ``pager``) are kept
      unless live TUI modes force a more specific label

    Non-shell open_mode keeps its open-time surface unless live TUI modes
    are detected (then label is refreshed for Agent meta).
    """
    try:
        live = detect_surface_from_screen(session.screen)
    except Exception:  # noqa: BLE001
        live = None

    current = (session.surface or "").strip().lower() or "shell"

    if live is not None:
        if current != live:
            session.surface = live
        return session.surface

    # Modes cleared: restore shell only when we (or open) left a dynamic TUI label.
    if session.open_mode == "shell" and current in _DYNAMIC_TUI_SURFACES:
        session.surface = "shell"
        return session.surface
    return session.surface or current


def _live_tui_blocking(session: ScreenSession) -> bool:
    """True when the pyte buffer has alt-screen or mouse tracking enabled."""
    try:
        return bool(live_tui_modes(session.screen))
    except Exception:  # noqa: BLE001
        return False


def _session_dialect(session: ScreenSession) -> str:
    """Resolve dialect from session fields (preferred) or meta/shell path.

    Empty string means unbound: the probe layer may apply a best-effort default.
    Explicit ``unknown`` dialects skip inject (no probe template).
    """
    d = getattr(session, "dialect", None)
    if isinstance(d, str) and d.strip():
        return d.strip().lower()
    meta = getattr(session, "meta", None) or {}
    caps = getattr(session, "shell_caps", None)
    # Caps are a plain mapping on every session the open path builds.
    busybox = caps.get("busybox") if isinstance(caps, dict) else None
    shell_path = str(
        meta.get("shell_path")
        or getattr(session, "shell_path", None)
        or getattr(session, "shell", None)
        or ""
    )
    shell_base = str(
        meta.get("shell_base")
        or meta.get("shell")
        or meta.get("shell_family")
        or meta.get("os")
        or ""
    )
    if not shell_base and not shell_path and busybox is None and not meta:
        return ""  # unbound
    return resolve_dialect(
        shell_base=shell_base or None,
        shell_path=shell_path or None,
        shell_family=str(meta.get("shell_family") or "") or None,
        busybox=bool(busybox) if busybox is not None else None,
        flags=meta if isinstance(meta, dict) else None,
    )


def _probe_cmd_for_session(session: ScreenSession) -> str | None:
    """Pick silent pwd command by session dialect; None = skip inject."""
    dialect = _session_dialect(session)
    if not dialect:
        # Unbound session: best-effort zsh-compatible short probe; helpers
        # soft-fail on bash. Prefer binding dialect from the endpoint when known.
        dialect = POSIX_ZSH
    return probe_cmd_for_dialect(dialect)


def silent_pwd_probe(
    session: ScreenSession,
    *,
    timeout_s: float = 1.5,
    clear_line: bool = True,
) -> str | None:
    """Inject a detachable pwd probe on the same PTY; return absolute path or None.

    Only intended for interactive shell surfaces. Failures are silent - caller
    keeps the previous ``session.cwd``. When dialect has no probe template
    (unknown/fish), returns None without writing to the PTY.

    The probe command is peer text, so it is written in the codec the session
    resolved for its own reads (``ScreenSession.text_codec``), like every other
    literal-text send; the ctrl+u / enter around it are protocol bytes.

    Never injects when the screen buffer shows a password/confirm prompt,
    a REPL/debugger prompt (``>>>`` / ``(Pdb)`` / ``(gdb)``), or a shell
    continuation (PS2 / heredoc / open quote / trailing ``\\``) - ctrl+u
    would wipe typed secrets, REPL input, or unfinished multi-line input.

    A leftover ``__MRC_PWD__:`` line already on screen is not a confirmation
    of this inject; the hunt requires a marker that was not visible before.
    """
    if session.closed:
        return None
    try:
        if not session.pty.is_alive():
            return None
    except Exception:  # noqa: BLE001
        return None
    # Defense in depth: interactive prompts / live TUI modes block inject even
    # if the caller bypassed ``_should_probe`` (e.g. force paths).
    if _interactive_input_blocking(session):
        return None
    if _live_tui_blocking(session):
        return None

    cmd = _probe_cmd_for_session(session)
    if not cmd:
        # Mark cwd provenance stale for Agent meta when no probe template.
        try:
            session.cwd_src = "stale"  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return None

    # Snapshot leftover markers *before* inject. parse last-wins would
    # otherwise treat an old line as this probe's result. The echo rows are
    # snapshotted too: the hunt's positional rule may only anchor on a command
    # echo painted after this inject.
    try:
        before_frame = dump_frame(session.screen, strip_probe=False)
        before_markers = [
            path for _, path in _marker_rows(before_frame, _screen_rows_full(session))
        ]
        before_echo = _echo_snapshot(before_frame)
    except Exception:  # noqa: BLE001
        before_markers = []
        before_echo = None

    try:
        if clear_line:
            session.write(encode_key("ctrl+u"))
        # The probe command is literal text like any other send, so it goes out
        # in the codec this session resolved for its own reads; the surrounding
        # control keys stay protocol bytes.
        session.write(encode_text(cmd, codec=session.text_codec))
        session.write(encode_key("enter"))
    except Exception:  # noqa: BLE001
        return None

    found: str | None = None
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            session.drain(0.05)
        except Exception:  # noqa: BLE001
            break
        # Do not strip probe while hunting for the marker.
        frame = dump_frame(session.screen, strip_probe=False)
        path = _fresh_probe_marker(
            frame,
            before_markers,
            before_echo=before_echo,
            row_full=_screen_rows_full(session),
        )
        if path and _looks_absolute(path):
            found = _normalize_path(path)
            break

    # Clear any leftover partial line / failed helper noise on the prompt.
    try:
        session.write(encode_key("ctrl+u"))
        session.drain(0.05)
    except Exception:  # noqa: BLE001
        pass
    return found


def _fresh_probe_marker(
    frame: str,
    before: Sequence[str],
    *,
    before_echo: tuple[int, int | None] | None = None,
    row_full: Sequence[bool] | None = None,
) -> str | None:
    """Marker in *frame* that this inject can claim as its own, or None.

    Combines the visible marker paths with their rows and the row of this
    inject's command echo, so a re-printed marker can still be recognised
    once the earlier one has scrolled off (see ``_fresh_pwd_marker``).

    The positional rule is only as good as the echo row it anchors on: a
    leftover echo of an earlier probe sits below its own leftover marker, so
    an unguarded row comparison would certify stale output. *before_echo* is
    the pre-inject ``_echo_snapshot``; without it (or when it shows no newer
    echo paint) the row test is skipped and the list rules decide alone.
    """
    rows = _marker_rows(frame, row_full)
    echo_rows = _probe_echo_rows(frame)
    echo_row = echo_rows[-1] if echo_rows else None
    if not _echo_after_snapshot((len(echo_rows), echo_row), before_echo):
        echo_row = None
    return _fresh_pwd_marker(
        [path for _, path in rows],
        before,
        after_rows=[row for row, _ in rows],
        echo_row=echo_row,
    )


def _screen_rows_full(session: ScreenSession) -> list[bool] | None:
    """Per-row fill flags for the session's current screen, or None.

    The screen, not the dumped frame, because the fill signal lives in the
    buffer cells (see ``buffer._full_rows``).
    """
    try:
        display = list(session.screen.display)
        return _full_rows(session.screen, len(display))
    except Exception:  # noqa: BLE001
        return None


def _marker_rows(
    text: str,
    row_full: Sequence[bool] | None = None,
) -> list[tuple[int, str]]:
    """``(row, path)`` for every valid marker in *text*, in screen order.

    A printed marker line wider than the terminal continues onto the rows
    below it, so a path read from one row alone is a prefix of the real cwd -
    a value that names no directory on the peer, that the send result reports
    as authoritative (``cwd_src='probe'``), and that every later relative
    ``cd`` compounds. *row_full* marks the rows that continue below (see
    ``buffer._full_rows``); the rows of one printed line are joined before the
    path is extracted, and the row reported stays the one the marker sits on,
    so callers' row arithmetic is unchanged. Without *row_full* each row is
    read on its own.
    """
    lines = text.splitlines()
    rows: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        paths = collect_pwd_markers(line)
        if not paths:
            # Echo rows (unexpanded ``$(pwd)``) and rows with no marker at all
            # start nothing; only a row that already yielded a path is the
            # marker output line a wrap could have split.
            continue
        if row_full:
            joined = line
            j = i
            while (
                j + 1 < len(lines)
                and j < len(row_full)
                and row_full[j]
                and continues_printed_path(lines[j + 1], joined)
            ):
                joined += lines[j + 1]
                j += 1
            if j > i:
                paths = collect_pwd_markers(joined)
        rows.extend((i, path) for path in paths)
    return rows


def _probe_echo_rows(text: str) -> list[int]:
    """Rows of un-expanded command echoes in *text*, in screen order.

    The shell echoes the probe command we injected. That echo carries the
    marker text but no expanded path, which is exactly the marker-shaped row
    ``collect_pwd_markers`` skips.
    """
    return [
        i
        for i, line in enumerate(text.splitlines())
        if PWD_MARKER in line and not collect_pwd_markers(line)
    ]


def _echo_snapshot(text: str) -> tuple[int, int | None]:
    """``(count, last row)`` of command-echo rows in *text*.

    Lets the hunt tell this inject's echo from a leftover: the screen only
    ever scrolls rows up, so a strictly larger count, or a last echo row below
    the snapshotted one, is a paint this inject caused.
    """
    rows = _probe_echo_rows(text)
    return (len(rows), rows[-1] if rows else None)


def _echo_after_snapshot(
    now: tuple[int, int | None],
    before: tuple[int, int | None] | None,
) -> bool:
    """True when *now*'s last echo row cannot predate the pre-inject snapshot."""
    if before is None:
        return False
    count_now, row_now = now
    count_before, row_before = before
    if row_now is None:
        return False
    if row_before is None:
        return True
    return row_now > row_before or count_now > count_before


def _fresh_pwd_marker(
    after: Sequence[str],
    before: Sequence[str],
    *,
    after_rows: Sequence[int] | None = None,
    echo_row: int | None = None,
) -> str | None:
    """Return the last marker in *after* only when it is a new confirmation.

    Leftover ``__MRC_PWD__:`` lines still painted from an earlier probe are
    not a result of this inject. A longer list (new line printed) or a last
    path that was not previously visible counts as fresh - but on a full
    buffer the previous marker scrolls off the top while the shell prints an
    identical path again, leaving both lists equal though the paint is new.

    *echo_row* (the row of this inject's command echo) decides that case
    positionally: the marker is printed below the echo, so a marker lower
    than the echo was painted after the inject, while one above it is a
    leftover that only moved with the scroll. ``_fresh_probe_marker`` owns
    proving that *echo_row* is this inject's own paint; a caller passing a
    row straight out of the frame can certify a leftover.
    """
    if not after:
        return None
    after_list = list(after)
    before_list = list(before)
    rows = list(after_rows) if after_rows is not None else []
    if echo_row is not None and rows and rows[-1] > echo_row:
        return after_list[-1]
    if after_list == before_list:
        return None
    if len(after_list) > len(before_list):
        return after_list[-1]
    if after_list[-1] not in before_list:
        return after_list[-1]
    return None


def _looks_absolute(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/"):
        return True
    # Windows drive or UNC
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
        return True
    if path.startswith("\\\\"):
        return True
    return False


def probe_and_update_cwd(
    session: ScreenSession,
    *,
    timeout_s: float = 1.5,
    force: bool = False,
) -> str | None:
    """Run silent probe when surface is shell; update ``session.cwd`` on success."""
    if not force and not _should_probe(session):
        return session.cwd
    path = silent_pwd_probe(session, timeout_s=timeout_s)
    if path:
        session.cwd = path
        session.cwd_src = "probe"
        return path
    if getattr(session, "cwd_src", None) not in ("probe", "heuristic"):
        session.cwd_src = "stale"
    return session.cwd


def apply_cd_heuristic(
    session: ScreenSession,
    actions: Sequence[Mapping[str, Any]] | None,
) -> str | None:
    """If actions contain an obvious ``cd <path>`` + submit, update session.cwd.

    Used as fallback when silent probe fails or is skipped (TUI). Relative paths
    join against the previous absolute cwd when known.
    """
    if not actions:
        return session.cwd
    new_cwd: str | None = None
    for act in actions:
        atype = str(act.get("type") or act.get("op") or "").strip().lower()
        if atype not in ("text", "paste"):
            continue
        text = act.get("text")
        if text is None:
            continue
        submit = act.get("submit")
        # text with submit=true, or a following submit is handled by scanning
        # only self-contained text+submit / paste+submit here.
        if not action_truthy(submit):
            continue
        m = _CD_RE.match(str(text).strip())
        if not m:
            continue
        raw = m.group("path").strip()
        if (raw.startswith("'") and raw.endswith("'")) or (
            raw.startswith('"') and raw.endswith('"')
        ):
            raw = raw[1:-1]
        new_cwd = _resolve_cd_target(
            raw,
            session.cwd,
            home=_session_home(session),
        )
    if new_cwd:
        session.cwd = new_cwd
        session.cwd_src = "heuristic"
    return session.cwd


def update_cwd_after_send(
    session: ScreenSession,
    actions: Sequence[Mapping[str, Any]] | None,
    *,
    probe: bool = True,
    timeout_s: float = 1.5,
) -> str | None:
    """Preferred post-send cwd refresh: probe shell, else cd heuristic.

    Silent probe (ctrl+u + pwd marker) runs only when the last landed action
    is a line commit (submit / enter / text|paste with submit). A mid-list
    submit followed by more typed text still leaves uncommitted input that
    must not be wiped. Cd heuristic still runs as a soft fallback (it itself
    requires submit on the cd action).
    """
    # Never inject a clear_line probe when actions left a partial typed line.
    # *actions* is None on force / open-adjacent refresh: those paths may probe.
    may_probe = bool(probe) and (
        actions is None or actions_include_submit(actions)
    )
    if may_probe and _should_probe(session):
        path = silent_pwd_probe(session, timeout_s=timeout_s)
        if path:
            session.cwd = path
            session.cwd_src = "probe"
            # Brief settle so the re-drawn prompt lands before the Agent shot.
            try:
                session.drain(0.08)
            except Exception:  # noqa: BLE001
                pass
            return path
    # Fallback / always apply cd heuristic as soft update
    before = session.cwd
    apply_cd_heuristic(session, actions)
    if session.cwd != before and session.cwd_src != "heuristic":
        # heuristic may have set cwd_src already
        if getattr(session, "cwd_src", None) != "heuristic":
            session.cwd_src = "stale"
    elif session.cwd == before and getattr(session, "cwd_src", None) not in (
        "probe",
        "heuristic",
        "open",
    ):
        session.cwd_src = "stale"
    return session.cwd


def _should_probe(session: ScreenSession) -> bool:
    """Whether it is safe to inject the silent cwd probe into the PTY.

    Gates (any fail -> no inject):
    - session closed / PTY dead
    - live pyte modes: alt-screen and/or mouse tracking (not open-time only)
    - TUI / alt-screen / pager surfaces (static label or after refresh)
    - non-shell open_mode when surface is not shell
    - screen buffer last lines look like password / confirm / similar
      interactive input, a REPL/debugger prompt (``>>>`` / ``(Pdb)`` /
      ``(gdb)``), or shell continuation (PS2 / heredoc / open quote /
      backslash-continued line) - ctrl+u would wipe secrets, REPL input,
      or unfinished multi-line input

    Last-line heuristics only for password/confirm/REPL/continuation - do
    **not** skip merely because the frame lacks a classic ``$`` prompt
    (avoids false-positive skip on noisy but still-shell frames).
    """
    if session.closed:
        return False
    # Static label gate before live refresh so explicit surface=tui|pager
    # (caller-set) is never clobbered into a false allow by restore-to-shell.
    surf = (session.surface or "shell").lower()
    if surf in _NO_PROBE_SURFACES:
        return False
    if session.open_mode != "shell" and surf != "shell":
        return False
    # Live detection: open_screen only stamps surface once; after vim/htop/less
    # the label may still be "shell" while pyte modes show TUI.
    try:
        refresh_session_surface(session)
    except Exception:  # noqa: BLE001
        pass
    if _live_tui_blocking(session):
        return False
    # Re-check after refresh (shell -> alt_screen/tui upgrade).
    surf = (session.surface or "shell").lower()
    if surf in _NO_PROBE_SURFACES:
        return False
    try:
        if not session.pty.is_alive():
            return False
    except Exception:  # noqa: BLE001
        return False
    if _interactive_input_blocking(session):
        return False
    return True


def _interactive_input_blocking(session: ScreenSession) -> bool:
    """True when the visible buffer waits on password/confirm, REPL, or PS2.

    Inspects only the last few non-empty lines so incidental matches in
    scrollback (docs, logs) do not suppress probing on a healthy shell.

    Also treats REPL/debugger last-line prompts (``>>>``, ``(Pdb)``,
    ``(gdb)``) and shell continuation modes as blocking - heredoc in
    progress, unclosed quotes, trailing backslash line-continuation, and
    PS2-style secondary prompts (``>``, ``quote>``, ``heredoc>``, ...).
    Injecting ctrl+u + probe would destroy unfinished input or send
    shell text into the debugger.
    """
    try:
        frame = dump_frame(session.screen, strip_probe=True)
    except Exception:  # noqa: BLE001
        return False
    if not frame or not str(frame).strip():
        return False
    lines = [ln.rstrip() for ln in str(frame).splitlines() if ln.strip()]
    if not lines:
        return False
    # Password/confirm prompts almost always sit on the bottom line(s).
    for ln in lines[-3:]:
        if _PASSWORD_LINE_RE.search(ln):
            return True
        if _CONFIRM_LINE_RE.search(ln):
            return True
    # REPL / debugger: last line only (scrollback ``>>>`` must not block).
    if _REPL_PROMPT_RE.match(lines[-1]):
        return True
    # PS2 / heredoc / quote / backslash continuation.
    if _continuation_input_blocking(lines):
        return True
    return False


def _continuation_input_blocking(lines: Sequence[str]) -> bool:
    """True when bottom-of-buffer looks like a shell multi-line continuation.

    Signals (any -> block probe):
    - last few lines start with a PS2-style secondary prompt
    - last line ends with an unescaped trailing backslash
    - last line has unbalanced single/double quotes
    - last line contains a heredoc opener (``<<TOKEN``)
    - recent window has an unclosed heredoc and the last line is not a
      primary shell prompt (avoids scrollback docs that mention ``<<EOF``)
    """
    if not lines:
        return False
    for ln in lines[-3:]:
        if _PS2_LINE_RE.match(ln):
            return True
    last = lines[-1]
    if _has_trailing_backslash_continuation(last):
        return True
    if _has_unclosed_quotes(last):
        return True
    if _HEREDOC_OPEN_RE.search(last):
        return True
    # Mid-heredoc body without a painted PS2 prefix: only when we are not
    # already back at a primary prompt (healthy shell after closed input).
    if not _looks_like_primary_prompt(last) and _heredoc_in_progress(lines):
        return True
    return False


def _looks_like_primary_prompt(line: str) -> bool:
    """Heuristic: last line is a shell primary prompt (PS1), not PS2 body."""
    s = line.rstrip()
    if not s:
        return False
    # Classic sh/bash/zsh primary: ends with $ / # / % (optional spaces).
    if re.search(r"[$#%]\s*$", s):
        return True
    # PowerShell / cmd primary often ends with '>' but is not bare PS2.
    if s.endswith(">") and not _PS2_LINE_RE.match(s):
        # Bare ">" is PS2; "PS C:\\>" / "C:\\>" are primary.
        if not re.match(r"^\s*>\s*$", s):
            return True
    return False


def _has_trailing_backslash_continuation(line: str) -> bool:
    """True when *line* ends with an unescaped ``\\`` (odd trailing count)."""
    raw = line.rstrip("\r\n")
    # Shell line-continuation requires ``\\`` as the final character (no
    # trailing spaces). Do not rstrip spaces.
    if not raw.endswith("\\"):
        return False
    n = 0
    for ch in reversed(raw):
        if ch == "\\":
            n += 1
        else:
            break
    return n % 2 == 1


def _has_unclosed_quotes(line: str) -> bool:
    """True when single- or double-quotes are left open (shell PS2 style).

    Tracks escapes outside quotes and does not treat the other quote type
    as special while inside a quoted region. Backtick command-sub is out of
    scope (zsh/bash often surface that as ``cmdsubst>`` PS2 instead).
    """
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2  # skip escaped next char (or lone \\ at EOL)
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        # Outside quotes.
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        i += 1
    return in_single or in_double


def _heredoc_token_from_match(m: re.Match[str]) -> str | None:
    token = m.group(1) or m.group(2) or m.group(3)
    if token is None:
        return None
    text = str(token).strip()
    return text or None


def _strip_ps2_prefix(line: str) -> str:
    """Remove a leading PS2-style prefix so heredoc closers compare cleanly."""
    m = _PS2_LINE_RE.match(line)
    if not m:
        return line.strip()
    return line[m.end() :].strip()


def _heredoc_in_progress(lines: Sequence[str]) -> bool:
    """True when a recent ``<<TOKEN`` opener has no matching closer yet.

    Windowed to the last ~16 non-empty lines so ancient scrollback (docs
    mentioning ``<<EOF``) does not permanently suppress probes. A closer is
    a subsequent line whose body (after optional PS2 prefix) equals TOKEN.
    """
    window = list(lines[-16:])
    open_token: str | None = None
    open_idx = -1
    for i, ln in enumerate(window):
        for m in _HEREDOC_OPEN_RE.finditer(ln):
            token = _heredoc_token_from_match(m)
            if not token:
                continue
            open_token = token
            open_idx = i
    if open_token is None or open_idx < 0:
        return False
    # Opener on the last line is also caught by the last-line check; keep
    # this path for mid-heredoc bodies without a visible PS2 prefix.
    if open_idx >= len(window) - 1:
        return True
    for ln in window[open_idx + 1 :]:
        body = _strip_ps2_prefix(ln)
        if body == open_token:
            return False
    return True


def _session_home(session: ScreenSession) -> str | None:
    """Best-effort home for ``cd ~`` heuristic - never invent a remote path.

    Order:
    1. ``session.meta['home']`` / ``user_home`` (endpoint open probe)
    2. Infer ``/home/<user>`` or ``/Users/<user>`` from absolute ``session.cwd``
    3. Local endpoint only: ``$HOME`` / ``$USERPROFILE``

    Remote sessions without a known home return ``None`` so ``~`` is **not**
    expanded against the controller machine's local HOME.
    """
    meta = getattr(session, "meta", None) or {}
    if isinstance(meta, dict):
        for key in ("home", "user_home"):
            val = meta.get(key)
            if isinstance(val, str):
                text = val.strip()
                if text and not text.startswith("~"):
                    return text
    cwd = getattr(session, "cwd", None)
    if isinstance(cwd, str):
        for prefix in ("/home/", "/Users/"):
            if cwd.startswith(prefix):
                rest = cwd[len(prefix) :]
                user = rest.split("/", 1)[0].strip()
                if user and user not in (".", ".."):
                    return prefix + user
    ep = str(getattr(session, "ep", "") or "").strip().lower()
    if ep in ("local", "localhost"):
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if home and str(home).strip():
            return str(home).strip()
        try:
            return str(Path.home())
        except (OSError, RuntimeError):
            return None
    return None


def _normalize_path(path: str) -> str:
    """Strip probe path text. Do not realpath against the controller disk.

    A remote ``/home/...`` must stay that string. ``Path.resolve()`` follows
    controller-local symlinks (macOS ``/home`` -> ``/System/Volumes/Data/home``).
    """
    return path.strip()


def _normalize_remote_home(home_path: str) -> str:
    """Lexically normalize a known remote home for a bare ``cd ~``.

    ``normpath``, never ``resolve()``: resolving would rewrite a remote path
    through controller-local symlinks. Same normalization the ``~/...`` form
    gets, so both spellings of the same home compare equal.
    """
    if home_path.startswith("/"):
        return posixpath.normpath(home_path)
    try:
        return ntpath.normpath(home_path)
    except OSError:
        return home_path.rstrip("/\\") or home_path


def _resolve_cd_target(
    raw: str,
    base: str | None,
    *,
    home: str | None = None,
) -> str | None:
    """Resolve a ``cd`` target for the heuristic fallback.

    ``~`` / ``~/...`` expand only against an explicit *home* (session meta or
    inferred remote home). Without a known home, tilde targets are skipped
    rather than expanding against the controller's local ``$HOME`` (which
    would poison remote session.cwd with a path that does not exist there).

    Every returned path is lexically normalized (``.`` and ``..`` collapsed) so
    the value equals the directory the peer moved to: the raw join would record
    ``cd ..`` from ``/a/b/c`` as ``/a/b/c/..``, a different string for the same
    directory, and each later relative cd would compound the fragment. This is
    ``normpath``, never ``resolve()`` - resolving would rewrite remote paths
    through controller-local symlinks.
    """
    if not raw or raw == "-":
        return None
    if raw == "~" or raw.startswith("~/"):
        home_path = (home or "").strip() if home else ""
        if not home_path:
            return None
        if raw == "~":
            # Never Path.resolve() a remote home against the controller disk.
            return _normalize_remote_home(home_path)
        rest = raw[2:]
        if home_path.startswith("/"):
            return posixpath.normpath(str(PurePosixPath(home_path) / rest))
        # Windows-style home (C:\Users\...)
        try:
            return ntpath.normpath(str(Path(home_path) / rest))
        except OSError:
            return ntpath.normpath(home_path.rstrip("\\/") + "\\" + rest.replace("/", "\\"))
    if raw.startswith("~"):
        # ``cd ~other`` / ``~other/x``: another account's home on the peer is
        # not knowable from here, and joining the token onto the current cwd
        # records a path that does not exist there - the failure the tilde
        # guard above exists to prevent, one spelling further out. Worse than
        # the un-normalized ``..`` case, because every later relative cd
        # compounds it. Skip the update rather than guess.
        return None
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        # Keep the typed path (no symlink resolution), only collapsed: a bare
        # ``cd /a/b/..`` moves the peer to /a, not to a path spelled /a/b/..
        return posixpath.normpath(raw) if raw.startswith("/") else ntpath.normpath(raw)
    if base:
        if str(base).startswith("/"):
            return posixpath.normpath(str(PurePosixPath(base) / raw))
        try:
            return ntpath.normpath(str(Path(base) / raw))
        except OSError:
            return posixpath.normpath(str(PurePosixPath(base) / raw))
    return raw

