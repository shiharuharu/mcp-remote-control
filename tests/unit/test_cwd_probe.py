"""Unit tests: silent cwd probe + marker strip."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp_remote_control.screen.buffer import (
    PWD_MARKER,
    _full_rows,
    alt_screen_active,
    collect_pwd_markers,
    detect_surface_from_screen,
    dump_frame,
    live_tui_modes,
    mouse_tracking_enabled,
    parse_pwd_marker,
    strip_probe_lines,
)
from mcp_remote_control.screen.cwd_probe import (
    _PROBE_CMD_BASH,
    _PROBE_CMD_POSIX,
    _actions_include_submit,
    _echo_snapshot,
    _fresh_probe_marker,
    _fresh_pwd_marker,
    _interactive_input_blocking,
    _live_tui_blocking,
    _marker_rows,
    _normalize_path,
    _probe_echo_rows,
    _resolve_cd_target,
    _session_home,
    _should_probe,
    apply_cd_heuristic,
    refresh_session_surface,
    silent_pwd_probe,
    update_cwd_after_send,
)
from mcp_remote_control.screen.keys import encode_key, encode_text
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.shell.dialect import POSIX_BASH


class ScriptedPty:
    """PTY that echoes a marker path after any write (simulates printf)."""

    def __init__(
        self,
        path: str = "/home/agent/work",
        *,
        reply_marker: bool = True,
    ) -> None:
        self.cols = 80
        self.rows = 24
        # Match PtyHandle: mutable cwd is invariant -> must be str | None.
        self.cwd: str | None = path
        self._path = path
        self._reply_marker = reply_marker
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
        if not self._reply_marker:
            return len(data)
        # When probe printf is submitted, queue marker line + fake prompt.
        if b"printf" in data or PWD_MARKER.encode() in data or data in (b"\r", b"\n"):
            # Defer full response until enter is seen in cumulative written
            pass
        blob = bytes(self.written)
        # Trigger on echo/printf probe once enter is in the stream.
        is_probe = (
            b"echo " in blob
            or b"printf" in blob
            or PWD_MARKER.encode() in blob
            or b"pwd -P" in blob
        )
        if is_probe and (blob.endswith(b"\r") or b"\r" in blob[-4:]):
            if PWD_MARKER.encode() not in self._pending:
                self._pending += f"\r\n{PWD_MARKER}{self._path}\r\n$ ".encode()
                # reset so we don't re-queue forever
                self.written.clear()
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


def test_posix_probe_cmd_avoids_history() -> None:
    """Probe line should not land in interactive shell history when possible."""
    cmd = _PROBE_CMD_POSIX
    # Leading space -> HISTCONTROL=ignorespace / HIST_IGNORE_SPACE
    assert cmd.startswith(" ")
    assert "fc -p" in cmd and "fc -P" in cmd
    assert PWD_MARKER in cmd
    # Shared POSIX line must stay short and zsh-safe (no bare bash-only opts).
    assert "history -d" not in cmd
    assert "set +o history" not in cmd
    assert "||:" in cmd or "|| :" in cmd
    assert cmd.rstrip().endswith(":")
    # bash variant uses set +o history, still no history -d
    assert _PROBE_CMD_BASH.startswith(" ")
    assert "set +o history" in _PROBE_CMD_BASH
    assert "history -d" not in _PROBE_CMD_BASH
    assert PWD_MARKER in _PROBE_CMD_BASH


def test_strip_probe_lines_from_frame() -> None:
    frame = f"prompt$\n{PWD_MARKER}/var/log\nnext$"
    cleaned = strip_probe_lines(frame)
    assert PWD_MARKER not in cleaned
    assert "/var/log" not in cleaned or "var" not in cleaned.splitlines()[0]
    assert "prompt$" in cleaned
    assert "next$" in cleaned


def test_parse_pwd_marker() -> None:
    assert parse_pwd_marker(f"x\n{PWD_MARKER}/tmp/foo\ny") == "/tmp/foo"
    assert parse_pwd_marker("no marker") is None
    # Spaces in path must not truncate at first whitespace.
    assert parse_pwd_marker(f"{PWD_MARKER}/home/my dir") == "/home/my dir"
    assert (
        parse_pwd_marker(f"x\n{PWD_MARKER}/home/my dir\ny") == "/home/my dir"
    )
    # Windows drive path with spaces (and UNC without spaces).
    assert (
        parse_pwd_marker(f"{PWD_MARKER}C:\\Program Files\\x")
        == "C:\\Program Files\\x"
    )
    assert (
        parse_pwd_marker(f"{PWD_MARKER}C:/Program Files/x")
        == "C:/Program Files/x"
    )
    # Quoted path (probe may quote; unwrap and keep interior spaces).
    assert parse_pwd_marker(f'{PWD_MARKER}"/home/my dir"') == "/home/my dir"
    assert parse_pwd_marker(f"{PWD_MARKER}'/home/my dir'") == "/home/my dir"
    # No marker -> None; unexpanded $(pwd) / %CD% command lines ignored.
    assert parse_pwd_marker(f"{PWD_MARKER}$(pwd -P 2>/dev/null||pwd)") is None
    assert parse_pwd_marker(f"{PWD_MARKER}%CD%") is None
    # Last valid marker wins (space path over earlier plain path).
    frame = f"{PWD_MARKER}/tmp/foo\n{PWD_MARKER}/home/my dir\nprompt$"
    assert parse_pwd_marker(frame) == "/home/my dir"


def test_dump_frame_strips_probe_by_default() -> None:
    import pyte

    screen = pyte.Screen(40, 10)
    stream = pyte.Stream(screen)
    stream.feed(f"hi\r\n{PWD_MARKER}/secret\r\n$ ")
    frame = dump_frame(screen)
    assert PWD_MARKER not in frame
    assert "/secret" not in frame
    raw = dump_frame(screen, strip_probe=False)
    assert PWD_MARKER in raw


def test_apply_cd_heuristic_absolute() -> None:
    pty = ScriptedPty("/home/u")
    sess = ScreenSession(
        id="s1",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/home/u",
        surface="shell",
        open_mode="shell",
    )
    apply_cd_heuristic(
        sess,
        [{"type": "text", "text": "cd /usr/local", "submit": True}],
    )
    cwd = sess.cwd
    assert cwd is not None
    assert cwd == "/usr/local" or cwd.endswith("/usr/local")


def test_apply_cd_heuristic_relative() -> None:
    pty = ScriptedPty("/tmp")
    sess = ScreenSession(
        id="s1",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    apply_cd_heuristic(
        sess,
        [{"type": "text", "text": "cd work", "submit": True}],
    )
    assert sess.cwd is not None
    assert sess.cwd.endswith("work") or "work" in sess.cwd


def test_silent_pwd_probe_updates_from_scripted_pty() -> None:
    pty = ScriptedPty("/home/agent/work")
    sess = ScreenSession(
        id="s1",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    path = silent_pwd_probe(sess, timeout_s=0.5)
    assert path is not None
    assert "work" in path or path.endswith("/home/agent/work")


def test_update_cwd_after_send_probe() -> None:
    pty = ScriptedPty("/opt/app")
    sess = ScreenSession(
        id="s1",
        ep="local",
        pty=pty,
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    # Paint a normal shell prompt so interactive-surface gates allow inject.
    sess.feed("user@host:/tmp$ ")
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.5,
    )
    assert cwd is not None
    assert "app" in cwd or cwd == "/opt/app"


# ---------------------------------------------------------------------------
# cwd probe must not touch interactive surfaces (password / confirm / TUI)
# ---------------------------------------------------------------------------


def _shell_sess(
    *,
    ep: str = "local",
    cwd: str = "/tmp",
    surface: str = "shell",
    meta: dict | None = None,
    reply_marker: bool = True,
) -> tuple[ScreenSession, ScriptedPty]:
    pty = ScriptedPty(cwd, reply_marker=reply_marker)
    sess = ScreenSession(
        id="s1",
        ep=ep,
        pty=pty,
        cols=80,
        rows=24,
        cwd=cwd,
        surface=surface,
        open_mode="shell",
        meta=dict(meta or {}),
    )
    return sess, pty


def test_should_probe_skips_password_prompt() -> None:
    sess, _pty = _shell_sess()
    sess.feed("[sudo] password for agent: ")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False


def test_should_probe_skips_ssh_password_line() -> None:
    sess, _pty = _shell_sess()
    sess.feed("agent@buildbox's password: ")
    assert _should_probe(sess) is False


def test_should_probe_skips_yes_no_confirm() -> None:
    sess, _pty = _shell_sess()
    sess.feed("Do you want to continue? [Y/n] ")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False


def test_should_probe_skips_tui_surface() -> None:
    sess, _pty = _shell_sess(surface="tui")
    sess.feed("$ ")  # even with a shell-looking last line
    assert _should_probe(sess) is False
    sess.surface = "tui_heavy"
    assert _should_probe(sess) is False
    sess.surface = "pager"
    assert _should_probe(sess) is False


def test_should_probe_allows_normal_shell_prompt() -> None:
    sess, _pty = _shell_sess()
    sess.feed("user@host:/home/user$ ")
    assert _interactive_input_blocking(sess) is False
    assert _should_probe(sess) is True


# ---------------------------------------------------------------------------
# Live alt-screen / mouse modes beat stale open-time surface=shell
# ---------------------------------------------------------------------------


def test_alt_screen_active_and_mouse_helpers() -> None:
    """buffer helpers: DECSET 1049 / 1000 map through pyte mode<<5."""
    import pyte

    screen = pyte.Screen(40, 10)
    stream = pyte.Stream(screen)
    assert alt_screen_active(screen) is False
    assert mouse_tracking_enabled(screen) is False
    assert live_tui_modes(screen) is False
    assert detect_surface_from_screen(screen) is None

    stream.feed("\x1b[?1049h")  # enter alternate screen (vim/less/htop)
    assert alt_screen_active(screen) is True
    assert live_tui_modes(screen) is True
    assert detect_surface_from_screen(screen) == "alt_screen"

    stream.feed("\x1b[?1049l")
    assert alt_screen_active(screen) is False
    assert detect_surface_from_screen(screen) is None

    stream.feed("\x1b[?1000h")  # xterm mouse tracking
    assert mouse_tracking_enabled(screen) is True
    assert live_tui_modes(screen) is True
    assert detect_surface_from_screen(screen) == "tui"

    stream.feed("\x1b[?1000l")
    assert mouse_tracking_enabled(screen) is False
    assert detect_surface_from_screen(screen) is None


def test_should_probe_skips_alt_screen_with_stale_shell_surface() -> None:
    """Alt-screen buffer + surface still 'shell' -> no probe."""
    sess, pty = _shell_sess(surface="shell")
    # Stale open-time label + shell-looking junk would previously allow inject.
    sess.feed("user@host:~$ ")
    sess.feed("\x1b[?1049h")  # vim-style alternate screen
    assert sess.surface == "shell"  # not yet refreshed
    assert _live_tui_blocking(sess) is True
    assert _should_probe(sess) is False
    # _should_probe refreshes surface for Agent meta.
    assert sess.surface == "alt_screen"
    before = bytes(pty.written)
    path = silent_pwd_probe(sess, timeout_s=0.2)
    assert path is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_should_probe_skips_mouse_tracking_with_stale_shell_surface() -> None:
    """Mouse modes alone (no alt buffer) still block ctrl+u inject."""
    sess, pty = _shell_sess(surface="shell")
    sess.feed("host:~$ ")
    sess.feed("\x1b[?1002h")  # button-event mouse tracking (common TUI)
    assert _should_probe(sess) is False
    assert sess.surface == "tui"
    before = bytes(pty.written)
    assert silent_pwd_probe(sess, timeout_s=0.15) is None
    assert bytes(pty.written) == before


def test_update_cwd_after_send_skips_probe_on_alt_screen() -> None:
    """Post-send path: surface=shell but alt-screen -> no ctrl+u / no probe bytes.

    Uses submit=True so the probe path is attempted (text-without-submit would
    skip inject before the live surface gate); alt-screen must still refuse
    inject and refresh session.surface for Agent meta.
    """
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ ")
    sess.feed("\x1b[?1049h")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "j", "submit": True}],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)
    assert sess.surface == "alt_screen"


def test_refresh_session_surface_restores_shell_when_alt_exits() -> None:
    """Leaving alt-screen restores shell so probe can run again."""
    sess, _pty = _shell_sess(surface="shell")
    sess.feed("\x1b[?1049h")
    assert refresh_session_surface(sess) == "alt_screen"
    assert sess.surface == "alt_screen"
    sess.feed("\x1b[?1049l")
    assert refresh_session_surface(sess) == "shell"
    assert sess.surface == "shell"
    sess.feed("user@host:~$ ")
    assert _should_probe(sess) is True


def test_should_probe_does_not_false_positive_on_password_in_scrollback() -> None:
    """Incidental 'password' mid-buffer must not block when last line is a prompt."""
    sess, _pty = _shell_sess()
    # "password" appears in older output; bottom line is a healthy shell prompt.
    sess.feed("updated password hash for user\r\nuser@host:~$ ")
    assert _interactive_input_blocking(sess) is False
    assert _should_probe(sess) is True


def test_silent_pwd_probe_no_inject_on_password_prompt() -> None:
    """ctrl+u / probe bytes must not be written while at a password prompt."""
    sess, pty = _shell_sess(cwd="/home/agent")
    sess.feed("[sudo] password for agent: ")
    before = bytes(pty.written)
    path = silent_pwd_probe(sess, timeout_s=0.2)
    assert path is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_update_cwd_after_send_skips_probe_on_password() -> None:
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("Password: ")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "sudo apt update", "submit": True}],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    # Probe command must not appear either.
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)


def test_update_cwd_after_send_probe_still_works_on_shell() -> None:
    """Normal shell prompt keeps cwd tracking via probe."""
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ ")
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.5,
    )
    assert cwd is not None
    assert "tmp" in cwd or cwd == "/tmp" or "work" in (cwd or "")
    # Probe inject path used ctrl+u at least once on a healthy shell.
    assert encode_key("ctrl+u") in bytes(pty.written) or b"__MRC_PWD__" in bytes(
        pty.written
    ) or b"printf" in bytes(pty.written) or b"echo " in bytes(pty.written)


# ---------------------------------------------------------------------------
# Post-send probe must not wipe uncommitted half-line typed text
# ---------------------------------------------------------------------------


def test_actions_include_submit_helper() -> None:
    assert _actions_include_submit(None) is False
    assert _actions_include_submit([]) is False
    assert _actions_include_submit([{"type": "text", "text": "ls"}]) is False
    assert _actions_include_submit(
        [{"type": "text", "text": "ls", "submit": False}]
    ) is False
    assert _actions_include_submit(
        [{"type": "text", "text": "ls", "submit": True}]
    ) is True
    assert _actions_include_submit([{"type": "submit"}]) is True
    assert _actions_include_submit([{"type": "key", "key": "enter"}]) is True
    assert _actions_include_submit([{"type": "keys", "keys": ["a", "enter"]}]) is True
    assert _actions_include_submit([{"type": "paste", "text": "x", "submit": True}]) is True
    assert _actions_include_submit([{"type": "key", "key": "a"}]) is False
    # Last landed action must be the commit - mid-list submit does not count.
    assert _actions_include_submit(
        [{"type": "submit"}, {"type": "text", "text": "partial"}]
    ) is False
    assert _actions_include_submit(
        [
            {"type": "text", "text": "ls", "submit": True},
            {"type": "text", "text": "more"},
        ]
    ) is False
    assert _actions_include_submit([{"type": "keys", "keys": ["enter", "up"]}]) is False
    assert _actions_include_submit(
        [{"type": "submit"}, {"type": "wait", "ms": 5}]
    ) is True


def test_update_cwd_after_send_skips_probe_on_text_without_submit() -> None:
    """Text without submit -> no ctrl+u / no probe inject (partial line safe)."""
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ ")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "echo half-line", "submit": False}],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)
    assert b"echo " not in bytes(pty.written)


def test_update_cwd_after_send_skips_probe_when_submit_not_last() -> None:
    """submit then more typed text: no ctrl+u / no probe (typed line stays)."""
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ ")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [
            {"type": "submit"},
            {"type": "text", "text": "echo half-line", "submit": False},
        ],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)
    assert b"echo " not in bytes(pty.written)


def test_update_cwd_after_send_probe_after_submit_still_runs() -> None:
    """After submit, probe can still refresh session.cwd."""
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ ")
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.5,
    )
    assert cwd is not None
    # ScriptedPty replies with its path; probe path should land.
    assert "tmp" in (cwd or "") or cwd == "/tmp" or "work" in (cwd or "")
    blob = bytes(pty.written)
    assert (
        encode_key("ctrl+u") in blob
        or b"__MRC_PWD__" in blob
        or b"printf" in blob
        or b"echo " in blob
    )


# ---------------------------------------------------------------------------
# Probe must not gate-destroy shell continuation (PS2 / heredoc / quotes)
# ---------------------------------------------------------------------------


def test_should_probe_skips_heredoc_opener_on_last_line() -> None:
    """Frame ending with <<EOF (unclosed heredoc) -> no probe."""
    sess, _pty = _shell_sess()
    sess.feed("user@host:~$ cat <<EOF")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False


def test_should_probe_skips_heredoc_ps2_prompt() -> None:
    """Heredoc in progress with bash/zsh PS2 secondary prompt."""
    sess, _pty = _shell_sess()
    sess.feed("user@host:~$ cat <<EOF\r\n> ")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False

    sess2, _pty2 = _shell_sess()
    sess2.feed("user@host:~$ cat <<'END'\r\nheredoc> partial body")
    assert _interactive_input_blocking(sess2) is True
    assert _should_probe(sess2) is False


def test_should_probe_skips_unclosed_quote_continuation() -> None:
    """Open quote on last line -> PS2-style, no probe."""
    sess, _pty = _shell_sess()
    sess.feed('user@host:~$ echo "hello')
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False

    sess2, _pty2 = _shell_sess()
    sess2.feed("user@host:~$ echo 'still open")
    assert _should_probe(sess2) is False

    sess3, _pty3 = _shell_sess()
    sess3.feed("user@host:~$ echo \"hi\"\r\ndquote> ")
    assert _interactive_input_blocking(sess3) is True
    assert _should_probe(sess3) is False


def test_should_probe_skips_backslash_continuation() -> None:
    """Trailing unescaped \\ (line continuation) -> no probe."""
    sess, _pty = _shell_sess()
    sess.feed("user@host:~$ echo foo\\")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False


def test_should_probe_allows_balanced_quotes_and_closed_heredoc() -> None:
    """Normal prompt regressions: completed input does not block probe."""
    sess, _pty = _shell_sess()
    sess.feed('user@host:~$ echo "hello"\r\nhello\r\nuser@host:~$ ')
    assert _interactive_input_blocking(sess) is False
    assert _should_probe(sess) is True

    sess2, _pty2 = _shell_sess()
    sess2.feed("user@host:~$ cat <<EOF\r\nhi\r\nEOF\r\nhi\r\nuser@host:~$ ")
    assert _interactive_input_blocking(sess2) is False
    assert _should_probe(sess2) is True

    # Even number of trailing backslashes = literal \, not continuation.
    sess3, _pty3 = _shell_sess()
    sess3.feed("user@host:~$ echo foo\\\\")
    # Last char is \, but count is 2 (even) after `foo` - wait: feed is
    # Python string "echo foo\\\\" -> shell-visible "echo foo\\" -> 2 backslashes.
    # Odd-count check -> not continuation. Line still may not look like PS1;
    # without $ at end this is a partial typed line (wipe is covered elsewhere),
    # but _interactive_input_blocking should not flag even-backslash as PS2.
    from mcp_remote_control.screen.cwd_probe import _has_trailing_backslash_continuation

    assert _has_trailing_backslash_continuation("user@host:~$ echo foo\\\\") is False


def test_silent_pwd_probe_no_inject_on_heredoc_continuation() -> None:
    """ctrl+u / probe bytes must not be written during heredoc/PS2 input."""
    sess, pty = _shell_sess(cwd="/home/agent")
    sess.feed("user@host:~$ cat <<EOF\r\n> body line")
    before = bytes(pty.written)
    path = silent_pwd_probe(sess, timeout_s=0.2)
    assert path is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_update_cwd_after_send_skips_probe_on_continuation() -> None:
    """Post-send with submit still refuses inject while PS2/heredoc active."""
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed("host:/tmp$ cat <<EOF\r\n> ")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "line", "submit": True}],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)


def test_should_probe_password_confirm_unchanged_with_continuation_gate() -> None:
    """Password/confirm skip and normal prompt allow must still hold."""
    sess, _pty = _shell_sess()
    sess.feed("[sudo] password for agent: ")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False

    sess2, _pty2 = _shell_sess()
    sess2.feed("Do you want to continue? [Y/n] ")
    assert _should_probe(sess2) is False

    sess3, _pty3 = _shell_sess()
    sess3.feed("user@host:/home/user$ ")
    assert _interactive_input_blocking(sess3) is False
    assert _should_probe(sess3) is True


def test_resolve_cd_tilde_uses_session_home_not_local_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Remote ~ must expand against session home, not controller $HOME."""
    monkeypatch.setenv("HOME", "/Users/controller-local")
    # No home known -> do not invent local HOME.
    assert _resolve_cd_target("~", "/var/tmp", home=None) is None
    assert _resolve_cd_target("~/work", "/var/tmp", home=None) is None
    # Explicit remote home.
    assert _resolve_cd_target("~", None, home="/home/remote") == "/home/remote"
    joined = _resolve_cd_target("~/work", None, home="/home/remote")
    assert joined == "/home/remote/work"


def test_session_home_prefers_meta_and_infers_from_cwd() -> None:
    sess, _ = _shell_sess(
        ep="buildbox-210",
        cwd="/home/agent/proj",
        meta={"home": "/home/agent"},
    )
    assert _session_home(sess) == "/home/agent"

    sess2, _ = _shell_sess(ep="buildbox-210", cwd="/home/agent/proj", meta={})
    assert _session_home(sess2) == "/home/agent"

    # Remote ep without meta/cwd home -> None (not local $HOME).
    sess3, _ = _shell_sess(ep="buildbox-210", cwd="/var/tmp", meta={})
    assert _session_home(sess3) is None


def test_apply_cd_heuristic_tilde_remote_home() -> None:
    sess, _ = _shell_sess(
        ep="buildbox-210",
        cwd="/home/agent",
        meta={"home": "/home/agent"},
    )
    apply_cd_heuristic(
        sess,
        [{"type": "text", "text": "cd ~/work", "submit": True}],
    )
    assert sess.cwd == "/home/agent/work"
    assert sess.cwd_src == "heuristic"


def test_apply_cd_heuristic_tilde_remote_without_home_skips(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without session home, cd ~ must not pollute cwd with local HOME."""
    monkeypatch.setenv("HOME", "/Users/controller-local")
    sess, _ = _shell_sess(ep="buildbox-210", cwd="/opt/app", meta={})
    apply_cd_heuristic(
        sess,
        [{"type": "text", "text": "cd ~", "submit": True}],
    )
    assert sess.cwd == "/opt/app"
    assert "/Users/controller-local" not in (sess.cwd or "")


# ---------------------------------------------------------------------------
# Stale leftover marker is not a new probe confirmation
# ---------------------------------------------------------------------------


def test_collect_pwd_markers_order() -> None:
    frame = f"{PWD_MARKER}/tmp/foo\n{PWD_MARKER}/home/my dir\nprompt$"
    assert collect_pwd_markers(frame) == ["/tmp/foo", "/home/my dir"]
    assert collect_pwd_markers("no marker") == []
    # Unexpanded command line is not a confirmation.
    assert collect_pwd_markers(f"{PWD_MARKER}$(pwd -P 2>/dev/null||pwd)") == []


def test_fresh_pwd_marker_rejects_unchanged_leftover() -> None:
    assert _fresh_pwd_marker(["/old"], ["/old"]) is None
    assert _fresh_pwd_marker(["/old", "/new"], ["/old"]) == "/new"
    assert _fresh_pwd_marker(["/old", "/old"], ["/old"]) == "/old"
    assert _fresh_pwd_marker(["/new"], ["/old"]) == "/new"
    assert _fresh_pwd_marker(["/old"], ["/other", "/old"]) is None
    assert _fresh_pwd_marker([], ["/old"]) is None


def test_silent_pwd_probe_ignores_stale_marker() -> None:
    """Pre-seeded __MRC_PWD__:/old is not a confirmation when the probe is silent."""
    sess, pty = _shell_sess(cwd="/prior", reply_marker=False)
    sess.feed(f"{PWD_MARKER}/old\r\nuser@host:/prior$ ")
    raw = dump_frame(sess.screen, strip_probe=False)
    assert parse_pwd_marker(raw) == "/old"
    cwd_before = sess.cwd
    path = silent_pwd_probe(sess, timeout_s=0.25)
    assert path is None
    assert sess.cwd == cwd_before
    assert sess.cwd == "/prior"
    assert sess.cwd != "/old"
    # Inject ran (healthy shell prompt) but must not adopt the leftover path.
    assert encode_key("ctrl+u") in bytes(pty.written) or b"__MRC_PWD__" in bytes(
        pty.written
    )


def test_update_cwd_after_send_stale_marker_keeps_prior_cwd() -> None:
    sess, _pty = _shell_sess(cwd="/prior", reply_marker=False)
    sess.feed(f"{PWD_MARKER}/old\r\nuser@host:/prior$ ")
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.25,
    )
    assert cwd == "/prior"
    assert sess.cwd == "/prior"
    assert sess.cwd_src != "probe"


# ---------------------------------------------------------------------------
# REPL / pdb / gdb last-line prompts: no ctrl+u / no probe inject
# ---------------------------------------------------------------------------


def test_should_probe_skips_python_repl_prompt() -> None:
    sess, pty = _shell_sess()
    sess.feed(">>>")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False
    before = bytes(pty.written)
    assert silent_pwd_probe(sess, timeout_s=0.2) is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_should_probe_skips_pdb_prompt() -> None:
    sess, pty = _shell_sess()
    sess.feed("(Pdb)")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False
    before = bytes(pty.written)
    assert silent_pwd_probe(sess, timeout_s=0.2) is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_should_probe_skips_gdb_prompt() -> None:
    sess, pty = _shell_sess()
    sess.feed("(gdb) ")
    assert _interactive_input_blocking(sess) is True
    assert _should_probe(sess) is False
    before = bytes(pty.written)
    assert silent_pwd_probe(sess, timeout_s=0.2) is None
    assert bytes(pty.written) == before
    assert encode_key("ctrl+u") not in bytes(pty.written)


def test_update_cwd_after_send_skips_probe_on_repl() -> None:
    sess, pty = _shell_sess(cwd="/tmp")
    sess.feed(">>>")
    cwd_before = sess.cwd
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.2,
    )
    assert cwd == cwd_before
    assert encode_key("ctrl+u") not in bytes(pty.written)
    assert b"__MRC_PWD__" not in bytes(pty.written)
    assert b"printf" not in bytes(pty.written)


def test_should_probe_allows_shell_after_repl_in_scrollback() -> None:
    """A >>> / (Pdb) line in older output must not block a healthy PS1."""
    sess, _pty = _shell_sess()
    sess.feed(">>>\r\n2\r\nuser@host:~$ ")
    assert _interactive_input_blocking(sess) is False
    assert _should_probe(sess) is True


# ---------------------------------------------------------------------------
# Remote /home/... must not be Path.resolve()'d onto the controller disk
# ---------------------------------------------------------------------------


def test_normalize_path_keeps_remote_home_string() -> None:
    remote = "/home/does-not-exist-on-controller"
    resolved = str(Path(remote).resolve())
    assert _normalize_path(remote) == remote
    if resolved != remote:
        assert _normalize_path(remote) != resolved


def test_resolve_cd_target_absolute_remote_not_local_resolve() -> None:
    remote = "/home/does-not-exist-on-controller"
    resolved = str(Path(remote).resolve())
    got = _resolve_cd_target(remote, "/tmp", home=None)
    assert got == remote
    if resolved != remote:
        assert got != resolved
    joined = _resolve_cd_target("work", remote, home=None)
    assert joined == f"{remote}/work"
    assert "/System/Volumes" not in (joined or "")


def test_remote_session_cwd_not_rewritten_by_probe() -> None:
    remote = "/home/does-not-exist-on-controller"
    resolved = str(Path(remote).resolve())
    sess, _pty = _shell_sess(ep="buildbox-210", cwd=remote)
    sess.feed("user@host:/home$ ")
    path = silent_pwd_probe(sess, timeout_s=0.5)
    assert path == remote
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.5,
    )
    assert cwd == remote
    assert sess.cwd == remote
    if resolved != remote:
        assert cwd != resolved


def test_apply_cd_heuristic_remote_absolute_not_local_resolve() -> None:
    remote = "/home/does-not-exist-on-controller"
    sess, _ = _shell_sess(ep="buildbox-210", cwd="/opt/app", meta={})
    apply_cd_heuristic(
        sess,
        [{"type": "text", "text": f"cd {remote}", "submit": True}],
    )
    assert sess.cwd == remote
    resolved = str(Path(remote).resolve())
    if resolved != remote:
        assert sess.cwd != resolved


# ---------------------------------------------------------------------------
# Probe rows that wrap: the echo of the injected command spans more than one
# display row, so dropping only the marker row leaves probe text behind.
# ---------------------------------------------------------------------------


def test_strip_probe_lines_drops_wrapped_probe_echo() -> None:
    import pyte

    cols = 100
    screen = pyte.Screen(cols, 12)
    stream = pyte.Stream(screen)
    echo = f"user@host$ {_PROBE_CMD_BASH}"
    stream.feed(f"{echo}\r\n{PWD_MARKER}/var/log\r\nuser@host$ ")

    raw_lines = dump_frame(screen, strip_probe=False).splitlines()
    # Premise: the echoed command is wider than the terminal and wrapped.
    assert len(echo) > cols
    tail = raw_lines[1]
    assert tail and PWD_MARKER not in tail, f"no wrapped tail row: {raw_lines!r}"

    frame = dump_frame(screen)
    assert PWD_MARKER not in frame
    assert "/var/log" not in frame
    assert tail.strip() not in frame, f"wrapped echo survived:\n{frame}"
    assert "user@host$" in frame


def test_strip_probe_lines_keeps_row_after_a_full_marker_output_row() -> None:
    """A marker *output* line that reaches the last column wrapped nothing.

    pyte records explicit cells for ``ESC[K`` and for exact-width lines, so
    the row below a full marker-output row is the next real line (a prompt),
    not a wrap continuation - dropping it silently removes Agent-visible text.
    """
    import pyte

    cols = 100
    screen = pyte.Screen(cols, 8)
    marker = PWD_MARKER + "/tmp/" + "p" * (cols - len(PWD_MARKER) - len("/tmp/"))
    assert len(marker) == cols
    pyte.Stream(screen).feed(f"user@host$ cd somewhere\r\n{marker}\r\nuser@host$ ")

    raw = [ln.rstrip() for ln in screen.display]
    assert _full_rows(screen, 8)[1] is True
    kept = strip_probe_lines(raw, row_full=_full_rows(screen, 8))
    assert kept[0] == "user@host$ cd somewhere"
    assert kept[1] == "user@host$"
    assert "user@host$" in dump_frame(screen)

    cols = 20
    screen = pyte.Screen(cols, 6)
    pyte.Stream(screen).feed(f"{PWD_MARKER}/tmp/p\x1b[K\r\nreal")
    raw = [ln.rstrip() for ln in screen.display]
    kept = strip_probe_lines(raw, row_full=_full_rows(screen, 6))
    assert "real" in kept, f"real row dropped after a full marker row: {kept!r}"


def test_fresh_pwd_marker_uses_echo_row_position() -> None:
    """Rows break the tie when the visible marker list is unchanged.

    On a full buffer the previous marker scrolls off while the shell prints an
    identical path again, so both lists read the same. The marker sits below
    this inject's echo either way - a leftover cannot.
    """
    # Re-printed marker below the echo of this inject: fresh.
    assert (
        _fresh_pwd_marker(["/same"], ["/same"], after_rows=[23], echo_row=21)
        == "/same"
    )
    # Leftover that only moved with the scroll: above the echo -> stale.
    assert _fresh_pwd_marker(["/old"], ["/old"], after_rows=[2], echo_row=21) is None
    # No echo row (peer does not echo): the list rules still decide.
    assert _fresh_pwd_marker(["/old"], ["/old"], after_rows=[2], echo_row=None) is None
    assert _fresh_pwd_marker(["/new"], ["/old"], after_rows=[2], echo_row=None) == "/new"


class EchoProbePty:
    """Shell-like PTY: echoes typed bytes and answers the probe with a marker.

    A real PTY paints the command echo before the marker output, which is what
    lets the probe tell a re-printed marker from a leftover one.
    """

    def __init__(
        self,
        path: str = "/private/tmp",
        *,
        cols: int = 80,
        rows: int = 24,
        reply_marker: bool = True,
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.cwd: str | None = path
        self._path = path
        self._reply_marker = reply_marker
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
        for byte in data:
            if byte == 0x0D:
                answer = f"\r\n{PWD_MARKER}{self._path}\r\n" if self._reply_marker else "\r\n"
                self._pending += f"{answer}user@host$ ".encode()
            elif 0x20 <= byte < 0x7F:
                self._pending += bytes([byte])
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


def _full_buffer_sess(
    *,
    filler: int = 28,
    reply_marker: bool = True,
) -> tuple[ScreenSession, EchoProbePty]:
    """Session whose 32-row buffer holds a previous probe result near the top.

    Geometry is clamped to at least ``MIN_COLS`` x ``MIN_ROWS``, so the filler
    count is sized for the clamped grid: echo (2 wrapped rows) + marker +
    *filler* + prompt exactly fills it.
    """
    pty = EchoProbePty("/private/tmp", cols=100, rows=32, reply_marker=reply_marker)
    sess = ScreenSession(
        id="s_fresh",
        ep="local",
        pty=pty,
        cols=100,
        rows=32,
        cwd="/prior",
        surface="shell",
        open_mode="shell",
        dialect=POSIX_BASH,
    )
    sess.feed(f"user@host$ {_PROBE_CMD_BASH}\r\n")
    sess.feed(f"{PWD_MARKER}/private/tmp\r\n")
    for i in range(filler):
        sess.feed(f"filler {i}\r\n")
    sess.feed("user@host$ ")
    return sess, pty


def test_probe_sees_marker_after_previous_one_scrolled_off() -> None:
    """Full buffer: the re-printed marker is still this inject's confirmation."""
    sess, _pty = _full_buffer_sess()
    before = collect_pwd_markers(dump_frame(sess.screen, strip_probe=False))
    assert before == ["/private/tmp"]

    path = silent_pwd_probe(sess, timeout_s=0.4)

    after = collect_pwd_markers(dump_frame(sess.screen, strip_probe=False))
    # The state under test: same count and same path visible before and after,
    # because the earlier line scrolled off the top as the new one printed.
    assert after == before
    assert path == "/private/tmp"


def test_probe_rejects_leftover_marker_above_the_echo() -> None:
    """Echo present but no marker printed: the visible leftover stays stale."""
    sess, _pty = _full_buffer_sess(reply_marker=False)
    assert collect_pwd_markers(dump_frame(sess.screen, strip_probe=False)) == [
        "/private/tmp"
    ]
    assert silent_pwd_probe(sess, timeout_s=0.3) is None
    assert sess.cwd == "/prior"


def test_probe_command_follows_the_session_codec() -> None:
    """A non-utf-8 session's probe command goes out in the session's codec.

    The probe command is literal peer text like every other send, so on a
    console that resolves to a legacy code page it must not reach the peer as
    utf-8 bytes. The fake peer answers in utf-8, which this session's codec
    reads as mojibake - the wire bytes are what this test pins.
    """
    pty = EchoProbePty("/srv/app")
    sess = ScreenSession(
        id="s_codec",
        ep="local",
        pty=pty,
        cols=100,
        rows=32,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
        text_encoding="cp037",  # EBCDIC: ASCII text is not a utf-8 byte string
    )
    assert sess.text_codec == "cp037"
    sess.feed("user@host:/tmp$ ")

    silent_pwd_probe(sess, timeout_s=0.4)

    blob = bytes(pty.written)
    assert encode_text(_PROBE_CMD_POSIX, codec="cp037") in blob
    assert _PROBE_CMD_POSIX.encode("utf-8") not in blob
    # The probe's own structure is unchanged: ctrl+u, command, enter.
    assert blob.startswith(encode_key("ctrl+u"))
    assert encode_key("enter") in blob


def test_probe_command_default_codec_stays_utf8() -> None:
    """Unset codec: the probe command keeps the historic utf-8 bytes."""
    pty = EchoProbePty("/srv/app")
    sess = ScreenSession(
        id="s_utf8",
        ep="local",
        pty=pty,
        cols=100,
        rows=32,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    assert sess.text_codec == "utf-8"
    sess.feed("user@host:/tmp$ ")

    path = silent_pwd_probe(sess, timeout_s=0.5)

    assert path == "/srv/app"
    assert encode_text(_PROBE_CMD_POSIX) in bytes(pty.written)


# An echoed probe command carries the marker text but no expanded path, which
# is what marks a row as a command echo rather than a marker result.
_ECHO_ROW = f"user@host$ {_PROBE_CMD_BASH}"
_OLD_MARKER_ROW = f"{PWD_MARKER}/tmp/OLD"
_NEW_MARKER_ROW = f"{PWD_MARKER}/srv/new"


def test_fresh_probe_marker_gates_row_rule_on_an_echo_of_this_inject() -> None:
    """A leftover echo must not vouch for the leftover marker beneath it.

    After an earlier probe the frame keeps both its echoed command and its
    marker line, in that order - the shape the row rule reads as "printed
    after this inject". Only an echo row the inject itself painted may act as
    that anchor.
    """
    before_frame = "\n".join(
        ["filler"] * 7 + [_ECHO_ROW, _OLD_MARKER_ROW, "user@host$ "]
    )
    before_markers = collect_pwd_markers(before_frame)
    assert before_markers == ["/tmp/OLD"]
    snapshot = _echo_snapshot(before_frame)
    assert snapshot == (1, 7)
    assert _probe_echo_rows(before_frame) == [7]
    assert [row for row, _ in _marker_rows(before_frame)] == [8]

    # No new paint since the snapshot: the pair on screen is the earlier
    # probe's, so neither candidate may be reported as this probe's result.
    assert _fresh_probe_marker(before_frame, before_markers, before_echo=snapshot) is None
    # Without the snapshot the row rule would certify the stale marker.
    assert (
        _fresh_pwd_marker(["/tmp/OLD"], before_markers, after_rows=[8], echo_row=7)
        == "/tmp/OLD"
    )

    # This inject's paint: the old pair scrolled up, a new echo + marker below.
    after_frame = "\n".join(
        ["filler"] * 5
        + [_ECHO_ROW, _OLD_MARKER_ROW, _ECHO_ROW, _NEW_MARKER_ROW, "user@host$ "]
    )
    assert (
        _fresh_probe_marker(after_frame, before_markers, before_echo=snapshot)
        == "/srv/new"
    )


class DelayedEchoPty:
    """PTY whose echo/marker reply only becomes readable after *delay_s*.

    Models a peer whose paint is slower than the probe's first drain (SSH or
    WinRM round trip, or a shell still busy with earlier input), so the first
    frames the hunt reads still show the previous probe's output.
    """

    def __init__(
        self,
        path: str,
        *,
        delay_s: float = 0.06,
        cols: int = 100,
        rows: int = 32,
        prior: str | None = None,
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.cwd: str | None = prior if prior is not None else path
        self._path = path
        self.delay_s = delay_s
        self._alive = True
        self._pending = b""
        self._queued = b""
        self._queued_at: float | None = None
        self.written = bytearray()

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None

    def _release(self) -> None:
        if (
            self._queued
            and self._queued_at is not None
            and time.monotonic() - self._queued_at >= self.delay_s
        ):
            self._pending += self._queued
            self._queued = b""
            self._queued_at = None

    def read(self, max_bytes: int = 8192) -> bytes:
        self._release()
        chunk, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return chunk

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        out: list[str] = []
        for byte in data:
            if byte == 0x0D:
                out.append(f"\r\n{PWD_MARKER}{self._path}\r\nuser@host$ ")
            elif 0x20 <= byte < 0x7F:
                out.append(chr(byte))
        self._queued += "".join(out).encode()
        if self._queued_at is None:
            self._queued_at = time.monotonic()
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        deadline = time.monotonic() + seconds
        total = 0
        while True:
            chunk = self.read()
            if chunk:
                total += len(chunk)
                if on_data:
                    on_data(chunk)
            if time.monotonic() >= deadline:
                return total
            if not chunk:
                time.sleep(0.005)

    def close(self) -> None:
        self._alive = False


def test_probe_waits_for_its_own_echo_instead_of_adopting_the_leftover() -> None:
    """Slow echo: the stale pair on screen must not answer this probe."""
    pty = DelayedEchoPty("/srv/new", delay_s=0.06, prior="/tmp/OLD")
    sess = ScreenSession(
        id="s_delayed",
        ep="buildbox",
        pty=pty,
        cols=100,
        rows=32,
        cwd="/tmp/OLD",
        surface="shell",
        open_mode="shell",
        dialect=POSIX_BASH,
    )
    sess.feed(f"user@host$ {_PROBE_CMD_BASH}\r\n")
    sess.feed(f"{PWD_MARKER}/tmp/OLD\r\n")
    sess.feed("user@host$ ")
    assert collect_pwd_markers(dump_frame(sess.screen, strip_probe=False)) == ["/tmp/OLD"]

    path = silent_pwd_probe(sess, timeout_s=1.0)

    assert path == "/srv/new"
    assert sess.cwd == "/tmp/OLD"  # probe does not write session.cwd itself
