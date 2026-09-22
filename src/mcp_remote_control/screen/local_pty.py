"""Local interactive shell PTY (stdlib pty + fork).

Opens a controlling TTY for a local shell or argv, with non-blocking master
reads and best-effort winsize updates.

POSIX-only modules (``fcntl`` / ``termios`` / ``pty``) are imported lazily
inside open/resize paths so this module (and mcp_server via screen_ops) can
load on Windows hosts. Local screen open on win32 is rejected in screen_ops
before LocalPty is constructed.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import signal
import struct
import threading
import time
from typing import Any

from mcp_remote_control.transport.base import TransportError

_log = logging.getLogger(__name__)

# Fallback when termios.TIOCSWINSZ is absent (ioctl winsize packing).
_TIOCSWINSZ_FALLBACK = 0x80087467

# After SIGKILL, poll waitpid(WNOHANG) up to this long then abandon.
# A child stuck in uninterruptible sleep (D-state, FUSE/NFS) never reaps;
# blocking waitpid(pid, 0) would hang the FastMCP / Core thread forever.
DEFAULT_WAITPID_TIMEOUT_S: float = 2.0
_WAITPID_POLL_INTERVAL_S: float = 0.05


def _status_to_exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def _tiocswinsz() -> int:
    """Resolve TIOCSWINSZ; import termios only when sizing a live PTY."""
    import termios

    return int(getattr(termios, "TIOCSWINSZ", _TIOCSWINSZ_FALLBACK))


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    # struct winsize { unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel }
    # fcntl is POSIX-only; import here so module import succeeds on win32.
    import fcntl

    packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
    try:
        fcntl.ioctl(fd, _tiocswinsz(), packed)
    except OSError:
        # Best-effort; some platforms/fds may reject.
        pass


def resolve_local_shell(preferred: str | None = None) -> str:
    """Pick interactive shell: preferred -> $SHELL -> /bin/bash -> /bin/sh."""
    candidates: list[str] = []
    if preferred and str(preferred).strip():
        candidates.append(str(preferred).strip())
    env_shell = os.environ.get("SHELL")
    if env_shell:
        candidates.append(env_shell)
    candidates.extend(["/bin/bash", "/bin/zsh", "/bin/sh"])
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise TransportError(
        "EXEC_FAILED",
        "no usable local shell found",
        details={"tried": candidates},
    )


def _resolve_cwd(cwd: str | None) -> str:
    if cwd is None or not str(cwd).strip():
        return os.getcwd()
    from pathlib import Path

    p = Path(str(cwd).strip()).expanduser()
    try:
        if not p.is_absolute():
            p = (Path(os.getcwd()) / p).resolve()
        else:
            p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(str(p)))
    if p.is_dir():
        return str(p)
    # Fall back rather than fail open (profile may seed non-existent cwd).
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home and Path(home).is_dir():
        return str(Path(home).resolve())
    return os.getcwd()


class LocalPty:
    """Interactive local shell attached to a PTY master fd."""

    def __init__(
        self,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
        shell: str | None = None,
        env: dict[str, str] | None = None,
        argv: list[str] | None = None,
    ) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        # Annotated as str | None to match PtyHandle (mutable attrs are invariant).
        self.cwd: str | None = _resolve_cwd(cwd)
        self._pid: int | None = None
        self._master: int | None = None
        self._closed = False
        self._exit_code: int | None = None
        # Serializes master-fd lifetime: close nulls + bumps generation under
        # the lock so concurrent drain/read never os.read a recycled fd number
        # after OS reclaim. select() snapshots fd+gen and re-validates after.
        self._io_lock = threading.Lock()
        self._fd_gen: int = 0

        shell_path = resolve_local_shell(shell)
        if argv:
            run_argv = [str(a) for a in argv]
        else:
            # Interactive shell; -i is widely supported (bash/zsh/sh).
            run_argv = [shell_path, "-i"]

        child_env = dict(os.environ)
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})
        child_env.setdefault("TERM", "xterm-256color")
        child_env.setdefault("COLORTERM", "truecolor")

        # Prefer pty.fork for correct controlling TTY on macOS/Linux.
        try:
            pid, master_fd = _fork_pty(
                run_argv=run_argv,
                cwd=self.cwd,
                env=child_env,
                cols=self.cols,
                rows=self.rows,
            )
        except OSError as exc:
            raise TransportError(
                "EXEC_FAILED",
                f"failed to open local PTY: {exc}",
                details={"cwd": self.cwd},
            ) from exc

        self._pid = pid
        self._master = master_fd
        # Non-blocking master for select/read loops (fcntl is POSIX-only).
        try:
            import fcntl

            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            pass

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def master_fd(self) -> int | None:
        with self._io_lock:
            return self._master

    def is_alive(self) -> bool:
        if self._closed or self._pid is None:
            return False
        if self._exit_code is not None:
            return False
        try:
            done_pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            # The status is already consumed - by a concurrent is_alive() or
            # by a host-level SIGCHLD reaper. This caller observed none, so
            # it records none: a fallback 0 here would overwrite the real
            # status another caller stored and report a clean exit for a
            # shell that failed.
            return False
        if done_pid == 0:
            return True
        if os.WIFEXITED(status):
            self._exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self._exit_code = -os.WTERMSIG(status)
        else:
            self._exit_code = -1
        return False

    def exit_code(self) -> int | None:
        self.is_alive()
        return self._exit_code

    def _snapshot_master(self) -> tuple[int | None, int]:
        """Return (master_fd, generation) under the I/O lock."""
        with self._io_lock:
            return self._master, self._fd_gen

    def _master_valid(self, fd: int, gen: int) -> bool:
        """True if *fd* is still the live master for *gen*."""
        with self._io_lock:
            return (
                self._master is not None
                and self._master == fd
                and self._fd_gen == gen
            )

    def _read_master_locked(self, fd: int, max_bytes: int) -> bytes:
        """Non-blocking read; caller holds ``_io_lock`` and owns *fd*."""
        try:
            return os.read(fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            return b""

    def read(self, max_bytes: int = 8192) -> bytes:
        # Hold the lock across the non-blocking os.read so close cannot
        # null+close the master while we still hold its number (recycled-fd).
        with self._io_lock:
            fd = self._master
            if fd is None:
                return b""
            return self._read_master_locked(fd, max_bytes)

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        with self._io_lock:
            if self._master is None or self._closed:
                raise TransportError("NOT_CONNECTED", "local PTY is closed")
            fd = self._master
            try:
                return os.write(fd, data)
            except OSError as exc:
                raise TransportError(
                    "EXEC_FAILED",
                    f"PTY write failed: {exc}",
                ) from exc

    def resize(self, cols: int, rows: int) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        with self._io_lock:
            fd = self._master
        if fd is not None:
            _set_winsize(fd, self.cols, self.rows)

    def drain_for(
        self,
        seconds: float,
        *,
        on_data: Any | None = None,
    ) -> int:
        """Read available PTY output for up to *seconds*; return total bytes.

        *on_data* if given is called with each bytes chunk.

        select() may block up to 50ms, so it runs on a snapshot of
        (fd, generation) without holding ``_io_lock``. After select returns,
        generation is re-checked under the lock before any os.read so a
        concurrent close cannot leave us reading a recycled fd number.
        """
        total = 0
        deadline = time.monotonic() + max(0.0, float(seconds))
        idle_rounds = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            fd, gen = self._snapshot_master()
            if fd is None:
                break
            try:
                ready, _, _ = select.select(
                    [fd], [], [], min(0.05, remaining)
                )
            except (ValueError, OSError):
                break
            if not ready:
                idle_rounds += 1
                # After we have already seen data, short idle ends early settle.
                if total > 0 and idle_rounds >= 2:
                    break
                continue
            idle_rounds = 0
            # Re-validate under lock, then read while still holding it so
            # close cannot reclaim the number mid-read.
            with self._io_lock:
                if (
                    self._master is None
                    or self._master != fd
                    or self._fd_gen != gen
                ):
                    break
                chunk = self._read_master_locked(fd, 8192)
            if not chunk:
                # EOF or would-block after select - check process.
                if not self.is_alive():
                    break
                continue
            total += len(chunk)
            if on_data is not None:
                on_data(chunk)
        return total

    def close(self) -> None:
        # Null master + bump generation under the lock so concurrent
        # drain/read refuse the old fd number before OS close reclaims it.
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            pid = self._pid
            master = self._master
            self._master = None
            self._fd_gen += 1
        # os.close outside the lock: short, and readers already see None/gen.
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if pid is None or self._exit_code is not None:
            return

        # SIGTERM: one WNOHANG, a short sleep, then another WNOHANG before SIGKILL.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            # Already gone - still try to reap below.
            pass
        else:
            if self._try_reap(pid):
                return
            time.sleep(_WAITPID_POLL_INTERVAL_S)
            if self._try_reap(pid):
                return

        # SIGKILL then bounded WNOHANG poll (never block forever).
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        if self._wait_reap(pid, timeout_s=DEFAULT_WAITPID_TIMEOUT_S):
            return
        _log.warning(
            "local PTY child pid=%s still unreaped after SIGKILL within %.1fs "
            "(possible D-state); abandoning waitpid to avoid blocking",
            pid,
            DEFAULT_WAITPID_TIMEOUT_S,
        )
        # Leave _exit_code unset: we never observed a wait status.

    def _try_reap(self, pid: int) -> bool:
        """Single non-blocking waitpid. True if child reaped or already gone."""
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Already reaped elsewhere: no status of this caller's own to
            # record (see is_alive).
            return True
        except OSError:
            return True
        if done == 0:
            return False
        self._exit_code = _status_to_exit_code(status)
        return True

    def _wait_reap(self, pid: int, *, timeout_s: float) -> bool:
        """Poll waitpid(WNOHANG) until reaped or *timeout_s* elapses."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if self._try_reap(pid):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_WAITPID_POLL_INTERVAL_S, remaining))


def _fork_pty(
    *,
    run_argv: list[str],
    cwd: str,
    env: dict[str, str],
    cols: int,
    rows: int,
) -> tuple[int, int]:
    """Fork a child with controlling PTY; return (pid, master_fd)."""
    import pty as pty_mod

    pid, master_fd = pty_mod.fork()
    if pid == 0:
        # Child: set winsize on stdin (slave side), then exec.
        try:
            _set_winsize(0, cols, rows)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.chdir(cwd)
        except OSError:
            pass
        try:
            os.execvpe(run_argv[0], run_argv, env)
        except OSError:
            os.write(2, f"mcp-remote-control: failed to exec {run_argv[0]!r}\n".encode())
            os._exit(127)
        os._exit(127)  # pragma: no cover

    # Parent: apply winsize on master as well (propagates on many systems).
    _set_winsize(master_fd, cols, rows)
    return pid, master_fd
