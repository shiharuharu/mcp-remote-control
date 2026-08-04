"""SSH interactive shell PTY via asyncssh.

Accepts real asyncssh connections and process-like objects that expose the
same surface (``create_process`` / ``open_shell_pty`` / stdin-stdout).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable
from typing import Any, cast

from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.ssh import _run_maybe_async, _safe_connect_msg


class SshPty:
    """Interactive remote shell on an SSH connection channel."""

    def __init__(
        self,
        process: Any,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
    ) -> None:
        self._process = process
        self.cols = int(cols)
        self.rows = int(rows)
        # Explicit str | None for PtyHandle protocol (attribute invariance).
        self.cwd: str | None = cwd
        self._closed = False
        self._exit_code: int | None = None
        self._buf = bytearray()

    @classmethod
    def open_shell(
        cls,
        conn: Any,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        term_type: str = "xterm-256color",
        command: str | None = None,
        argv: list[str] | None = None,
    ) -> SshPty:
        """Open an interactive shell (or optional command) PTY on *conn*.

        *conn* may be a real asyncssh connection or any object exposing
        ``create_process`` / ``open_session`` / ``open_shell_pty``.
        """
        if conn is None:
            raise TransportError("NOT_CONNECTED", "ssh connection is None")

        # Optional factory when the connection exposes open_shell_pty.
        factory = getattr(conn, "open_shell_pty", None)
        if callable(factory):
            raw = _run_maybe_async(
                factory(
                    cols=cols,
                    rows=rows,
                    cwd=cwd,
                    env=env,
                    term_type=term_type,
                    command=command,
                    argv=argv,
                )
            )
            if isinstance(raw, SshPty):
                return raw
            # Factory may return a process-like object.
            return cls(raw, cols=cols, rows=rows, cwd=cwd)

        process = _create_ssh_process(
            conn,
            cols=cols,
            rows=rows,
            cwd=cwd,
            env=env,
            term_type=term_type,
            command=command,
            argv=argv,
        )
        return cls(process, cols=cols, rows=rows, cwd=cwd)

    @property
    def process(self) -> Any:
        return self._process

    def is_alive(self) -> bool:
        if self._closed:
            return False
        proc = self._process
        if proc is None:
            return False
        # asyncssh: process.exit_status is None while running
        if hasattr(proc, "exit_status"):
            status = proc.exit_status
            if status is not None:
                self._exit_code = int(status)
                return False
        if hasattr(proc, "returncode"):
            rc = proc.returncode
            if rc is not None:
                self._exit_code = int(rc)
                return False
        alive = getattr(proc, "is_alive", None)
        if callable(alive):
            return bool(alive())
        return not self._closed

    def exit_code(self) -> int | None:
        self.is_alive()
        return self._exit_code

    def read(self, max_bytes: int = 8192) -> bytes:
        if self._closed or self._process is None:
            return b""
        if self._buf:
            out = bytes(self._buf[:max_bytes])
            del self._buf[:max_bytes]
            return out
        return _ssh_read_once(self._process, max_bytes=max_bytes, timeout_s=0.05)

    def write(self, data: bytes) -> int:
        if self._closed or self._process is None:
            raise TransportError("NOT_CONNECTED", "ssh PTY is closed")
        if not data:
            return 0
        proc = self._process
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            writer = getattr(proc, "write", None)
            if callable(writer):
                _run_maybe_async(writer(data))
                return len(data)
            raise TransportError("UNSUPPORTED", "ssh process has no stdin")
        try:
            # asyncssh: encoding=None → bytes; default utf-8 → str.
            write = getattr(stdin, "write", None)
            if not callable(write):
                raise TransportError("UNSUPPORTED", "ssh stdin is not writable")
            try:
                _run_maybe_async(write(data))
            except TypeError:
                # Text-mode channel: encode as str (utf-8 wire for interactive shells).
                text = data.decode("utf-8", errors="replace")
                _run_maybe_async(write(text))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(
                "EXEC_FAILED",
                _safe_connect_msg(exc),
            ) from exc
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        proc = self._process
        if proc is None:
            return
        changer = getattr(proc, "change_terminal_size", None)
        if callable(changer):
            try:
                _run_maybe_async(changer(self.cols, self.rows))
            except Exception:  # noqa: BLE001
                pass
            return
        # Channel-level API
        chan = getattr(proc, "channel", None) or getattr(proc, "_chan", None)
        if chan is not None:
            cchange = getattr(chan, "change_terminal_size", None)
            if callable(cchange):
                try:
                    _run_maybe_async(cchange(self.cols, self.rows))
                except Exception:  # noqa: BLE001
                    pass

    def drain_for(
        self,
        seconds: float,
        *,
        on_data: Any | None = None,
    ) -> int:
        total = 0
        deadline = time.monotonic() + max(0.0, float(seconds))
        idle_rounds = 0
        while time.monotonic() < deadline:
            chunk = self.read()
            if not chunk:
                idle_rounds += 1
                if total > 0 and idle_rounds >= 2:
                    break
                # Brief pause for more remote data.
                time.sleep(0.03)
                if not self.is_alive() and not self._buf:
                    break
                continue
            idle_rounds = 0
            total += len(chunk)
            if on_data is not None:
                on_data(chunk)
        return total

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            close = getattr(proc, "close", None)
            if callable(close):
                close()
            # Terminate if still running.
            term = getattr(proc, "terminate", None)
            if callable(term):
                try:
                    _run_maybe_async(term())
                except Exception:  # noqa: BLE001
                    pass
            wait = getattr(proc, "wait", None)
            if callable(wait):
                try:
                    _run_maybe_async(
                        asyncio.wait_for(
                            cast(Awaitable[Any], wait()),
                            timeout=1.0,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(proc, "exit_status") and proc.exit_status is not None:
                self._exit_code = int(proc.exit_status)
        except Exception:  # noqa: BLE001
            pass


def _create_ssh_process(
    conn: Any,
    *,
    cols: int,
    rows: int,
    cwd: str | None,
    env: dict[str, str] | None,
    term_type: str,
    command: str | None,
    argv: list[str] | None,
) -> Any:
    """Create remote process/shell with PTY on *conn*."""
    # Prefer create_process (asyncssh).
    create = getattr(conn, "create_process", None)
    if callable(create):
        kwargs: dict[str, Any] = {
            "term_type": term_type,
            "term_size": (cols, rows),
        }
        # Prefer bytes for pyte feed.
        try:
            sig = inspect.signature(create)
            params = sig.parameters
        except (TypeError, ValueError):
            params = {}

        if "encoding" in params:
            kwargs["encoding"] = None
        if env is not None and "env" in params:
            kwargs["env"] = env

        # Remote command: default interactive shell (omit / None).
        remote_cmd: str | None
        if argv:
            import shlex

            remote_cmd = " ".join(shlex.quote(str(a)) for a in argv)
        elif command:
            remote_cmd = command
        else:
            remote_cmd = None

        if cwd and remote_cmd:
            import shlex

            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
        elif cwd and remote_cmd is None:
            # Interactive shell with initial cwd via a login-style wrapper.
            # Prefer plain shell; cwd applied after settle in session if needed.
            pass

        try:
            if remote_cmd is not None:
                # Positional command for asyncssh.
                return _run_maybe_async(create(remote_cmd, **kwargs))
            return _run_maybe_async(create(**kwargs))
        except TypeError:
            # Alternate signature: try keyword command=
            if remote_cmd is not None:
                kwargs["command"] = remote_cmd
            return _run_maybe_async(create(**kwargs))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(
                "EXEC_FAILED",
                f"ssh create_process failed: {_safe_connect_msg(exc)}",
            ) from exc

    raise TransportError(
        "UNSUPPORTED",
        "ssh connection cannot open PTY (no create_process)",
    )


def _ssh_read_once(process: Any, *, max_bytes: int, timeout_s: float) -> bytes:
    stdout = getattr(process, "stdout", None)
    if stdout is None:
        reader = getattr(process, "read", None)
        if callable(reader):
            raw = _run_maybe_async(reader(max_bytes))
            return _as_bytes(raw)
        return b""

    read = getattr(stdout, "read", None)
    if not callable(read):
        return b""

    async def _aread() -> bytes:
        try:
            raw = await asyncio.wait_for(
                cast(Awaitable[Any], read(max_bytes)),
                timeout=timeout_s,
            )
            return _as_bytes(raw)
        except TimeoutError:
            return b""
        except Exception:  # noqa: BLE001
            return b""

    # If read is sync, call directly with best-effort.
    if not inspect.iscoroutinefunction(read):
        try:
            raw = read(max_bytes)
            if inspect.isawaitable(raw):
                return _run_maybe_async(
                    asyncio.wait_for(
                        cast(Awaitable[Any], raw),
                        timeout=timeout_s,
                    )
                )
            return _as_bytes(raw)
        except Exception:  # noqa: BLE001
            return b""

    try:
        return _run_maybe_async(_aread())
    except Exception:  # noqa: BLE001
        return b""


def _as_bytes(raw: Any) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    return bytes(raw)
