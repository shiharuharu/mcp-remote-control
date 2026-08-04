"""Unit tests: silent cwd probe + marker strip (T19 / 014)."""

from __future__ import annotations

from typing import Any

from mcp_remote_control.screen.buffer import (
    PWD_MARKER,
    dump_frame,
    parse_pwd_marker,
    strip_probe_lines,
)
from mcp_remote_control.screen.cwd_probe import (
    _PROBE_CMD_POSIX,
    apply_cd_heuristic,
    silent_pwd_probe,
    update_cwd_after_send,
)
from mcp_remote_control.screen.session import ScreenSession


class ScriptedPty:
    """PTY that echoes a marker path after any write (simulates printf)."""

    def __init__(self, path: str = "/home/agent/work") -> None:
        self.cols = 80
        self.rows = 24
        # Match PtyHandle: mutable cwd is invariant → must be str | None.
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
    from mcp_remote_control.screen.cwd_probe import _PROBE_CMD_BASH

    cmd = _PROBE_CMD_POSIX
    # Leading space → HISTCONTROL=ignorespace / HIST_IGNORE_SPACE
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
    cwd = update_cwd_after_send(
        sess,
        [{"type": "text", "text": "true", "submit": True}],
        probe=True,
        timeout_s=0.5,
    )
    assert cwd is not None
    assert "app" in cwd or cwd == "/opt/app"
