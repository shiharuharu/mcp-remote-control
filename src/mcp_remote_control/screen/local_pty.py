"""Local interactive shell PTY (stdlib pty + fork).

Opens a controlling TTY for a local shell or argv, with non-blocking master
reads and best-effort winsize updates.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import struct
import termios
import time
from typing import Any

from mcp_remote_control.transport.base import TransportError

# ioctl TIOCSWINSZ / TIOCGWINSZ
_TIOCSWINSZ = getattr(termios, "TIOCSWINSZ", 0x80087467)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    # struct winsize { unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel }
    packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
    try:
        fcntl.ioctl(fd, _TIOCSWINSZ, packed)
    except OSError:
        # Best-effort; some platforms/fds may reject.
        pass


def resolve_local_shell(preferred: str | None = None) -> str:
    """Pick interactive shell: preferred → $SHELL → /bin/bash → /bin/sh."""
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
        # Non-blocking master for select/read loops.
        try:
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            pass

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def master_fd(self) -> int | None:
        return self._master

    def is_alive(self) -> bool:
        if self._closed or self._pid is None:
            return False
        if self._exit_code is not None:
            return False
        try:
            done_pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._exit_code = 0
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

    def read(self, max_bytes: int = 8192) -> bytes:
        if self._master is None:
            return b""
        try:
            return os.read(self._master, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            return b""

    def write(self, data: bytes) -> int:
        if self._master is None or self._closed:
            raise TransportError("NOT_CONNECTED", "local PTY is closed")
        if not data:
            return 0
        try:
            return os.write(self._master, data)
        except OSError as exc:
            raise TransportError(
                "EXEC_FAILED",
                f"PTY write failed: {exc}",
            ) from exc

    def resize(self, cols: int, rows: int) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        if self._master is not None:
            _set_winsize(self._master, self.cols, self.rows)

    def drain_for(
        self,
        seconds: float,
        *,
        on_data: Any | None = None,
    ) -> int:
        """Read available PTY output for up to *seconds*; return total bytes.

        *on_data* if given is called with each bytes chunk.
        """
        if self._master is None:
            return 0
        total = 0
        deadline = time.monotonic() + max(0.0, float(seconds))
        idle_rounds = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select(
                    [self._master], [], [], min(0.05, remaining)
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
            chunk = self.read()
            if not chunk:
                # EOF or would-block after select — check process.
                if not self.is_alive():
                    break
                continue
            total += len(chunk)
            if on_data is not None:
                on_data(chunk)
        return total

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pid = self._pid
        master = self._master
        self._master = None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if pid is not None and self._exit_code is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except OSError:
                    break
                try:
                    done, status = os.waitpid(pid, 0 if sig == signal.SIGKILL else os.WNOHANG)
                except ChildProcessError:
                    self._exit_code = 0
                    break
                if done == 0:
                    time.sleep(0.05)
                    continue
                if os.WIFEXITED(status):
                    self._exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    self._exit_code = -os.WTERMSIG(status)
                else:
                    self._exit_code = -1
                break
            else:
                try:
                    _, status = os.waitpid(pid, 0)
                    if os.WIFEXITED(status):
                        self._exit_code = os.WEXITSTATUS(status)
                    else:
                        self._exit_code = -1
                except (ChildProcessError, OSError):
                    self._exit_code = 0


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
