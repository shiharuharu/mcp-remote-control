"""SSH interactive shell PTY via asyncssh.

Accepts real asyncssh connections and process-like objects that expose the
same surface (``create_process`` / ``open_shell_pty`` / stdin-stdout).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path
from mcp_remote_control.transport.ssh import _run_maybe_async, _safe_connect_msg

# Wall-clock budget for create_process / open_shell_pty only (channel open).
# Aligns with DEFAULT_SFTP_TIMEOUT_S so a wedged remote channel-open cannot
# park the FastMCP / Core thread forever (async_bridge timeout_s=None would
# block on Future.result() indefinitely). Read/write keep their own short
# budgets; do not blanket-timeout every I/O with this value.
DEFAULT_SSH_PTY_OPEN_TIMEOUT_S: float = 60.0

# Duck-typed fatal channel/peer-drop signals (asyncssh + compatible mocks).
# Matched by exception class name so we stay decoupled from asyncssh imports.
_FATAL_CHANNEL_NAMES = frozenset(
    {
        "ConnectionLost",
        "SFTPConnectionLost",
        "SFTPNoConnection",
        "DisconnectError",
        "ChannelClosed",
        "ChannelOpenError",
        "BrokenPipeError",
        "ConnectionResetError",
        "ConnectionAbortedError",
    }
)
_FATAL_CHANNEL_MARKERS = (
    "connection lost",
    "channel closed",
    "channel is closed",
    "not connected",
    "session closed",
    "connection reset",
    "broken pipe",
    "disconnect",
)


def _is_fatal_channel_exc(exc: BaseException) -> bool:
    """True when *exc* means the SSH PTY channel/peer is gone."""
    # Soft timeouts are empty-read, not death.
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return False
    name = type(exc).__name__
    if name in _FATAL_CHANNEL_NAMES:
        return True
    text = str(exc).strip().lower()
    if text:
        return any(m in text for m in _FATAL_CHANNEL_MARKERS)
    return False


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
        # Set when read/write sees a fatal channel/peer-drop exception. asyncssh
        # often leaves exit_status=None after silent peer drop, so is_alive()
        # must not trust process exit alone.
        self._dead = False
        self._dead_reason: str | None = None
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
        open_timeout_s: float | None = None,
    ) -> SshPty:
        """Open an interactive shell (or optional command) PTY on *conn*.

        *conn* may be a real asyncssh connection or any object exposing
        ``create_process`` / ``open_session`` / ``open_shell_pty``.

        *open_timeout_s* is the wall-clock budget for the create/factory
        await only (default :data:`DEFAULT_SSH_PTY_OPEN_TIMEOUT_S`). A bridge
        timeout becomes ``TransportError(TIMEOUT)`` so ``open_screen`` fails
        within budget instead of parking the caller thread forever.
        """
        if conn is None:
            raise TransportError("NOT_CONNECTED", "ssh connection is None")

        budget = (
            DEFAULT_SSH_PTY_OPEN_TIMEOUT_S
            if open_timeout_s is None
            else float(open_timeout_s)
        )

        # Optional factory when the connection exposes open_shell_pty.
        factory = getattr(conn, "open_shell_pty", None)
        if callable(factory):
            raw = _await_pty_open(
                factory(
                    cols=cols,
                    rows=rows,
                    cwd=cwd,
                    env=env,
                    term_type=term_type,
                    command=command,
                    argv=argv,
                ),
                timeout_s=budget,
                what="open_shell_pty",
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
            open_timeout_s=budget,
        )
        return cls(process, cols=cols, rows=rows, cwd=cwd)

    @property
    def process(self) -> Any:
        return self._process

    def mark_dead(self, reason: str | None = None) -> None:
        """Record that the SSH channel is unusable (idempotent)."""
        self._dead = True
        if reason and not self._dead_reason:
            self._dead_reason = str(reason)[:200]

    def is_alive(self) -> bool:
        if self._closed or self._dead:
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
            try:
                if not bool(alive()):
                    self.mark_dead("process is_alive() returned False")
                    return False
            except Exception as exc:  # noqa: BLE001
                self.mark_dead(f"process is_alive() failed: {_safe_connect_msg(exc)}")
                return False
        # Peer drop / channel tear-down flags (asyncssh often leaves exit_status
        # None; is_closing/is_closed on process or channel is the only signal).
        if _channel_looks_closed(proc):
            self.mark_dead("ssh channel closed")
            return False
        # Still running as far as process metadata shows. Liveness after silent
        # peer drop is refined by read/write paths calling mark_dead.
        return True

    def exit_code(self) -> int | None:
        self.is_alive()
        return self._exit_code

    def read(self, max_bytes: int = 8192) -> bytes:
        if self._closed or self._dead or self._process is None:
            return b""
        if self._buf:
            out = bytes(self._buf[:max_bytes])
            del self._buf[:max_bytes]
            return out

        def _on_fatal(exc: BaseException) -> None:
            self.mark_dead(_safe_connect_msg(exc) or type(exc).__name__)

        return _ssh_read_once(
            self._process,
            max_bytes=max_bytes,
            timeout_s=0.05,
            on_fatal=_on_fatal,
        )

    def write(self, data: bytes) -> int:
        if self._closed or self._process is None or self._dead:
            code = "DEAD" if self._dead else "NOT_CONNECTED"
            reason = self._dead_reason or "ssh PTY is closed"
            raise TransportError(code, reason)
        if not data:
            return 0
        proc = self._process
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            writer = getattr(proc, "write", None)
            if callable(writer):
                try:
                    _run_maybe_async(writer(data))
                except Exception as exc:  # noqa: BLE001
                    self._raise_write_exc(exc)
                return len(data)
            raise TransportError("UNSUPPORTED", "ssh process has no stdin")
        try:
            # asyncssh: encoding=None -> bytes; default utf-8 -> str.
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
            self._raise_write_exc(exc)
        return len(data)

    def _raise_write_exc(self, exc: BaseException) -> None:
        """Map a write failure to TransportError; mark channel dead when fatal."""
        if _is_fatal_channel_exc(exc):
            msg = _safe_connect_msg(exc) or type(exc).__name__
            self.mark_dead(msg)
            raise TransportError("DEAD", f"ssh PTY channel dead: {msg}") from exc
        raise TransportError(
            "EXEC_FAILED",
            _safe_connect_msg(exc),
        ) from exc

    def resize(self, cols: int, rows: int) -> None:
        self.cols = int(cols)
        self.rows = int(rows)
        proc = self._process
        if proc is None or self._dead or self._closed:
            return
        changer = getattr(proc, "change_terminal_size", None)
        if callable(changer):
            try:
                _run_maybe_async(changer(self.cols, self.rows))
            except Exception as exc:  # noqa: BLE001
                if _is_fatal_channel_exc(exc):
                    self.mark_dead(_safe_connect_msg(exc) or type(exc).__name__)
            return
        # Channel-level API
        chan = getattr(proc, "channel", None) or getattr(proc, "_chan", None)
        if chan is not None:
            cchange = getattr(chan, "change_terminal_size", None)
            if callable(cchange):
                try:
                    _run_maybe_async(cchange(self.cols, self.rows))
                except Exception as exc:  # noqa: BLE001
                    if _is_fatal_channel_exc(exc):
                        self.mark_dead(_safe_connect_msg(exc) or type(exc).__name__)

    def drain_for(
        self,
        seconds: float,
        *,
        on_data: Any | None = None,
    ) -> int:
        if self._closed or self._dead or self._process is None:
            return 0
        total = 0
        deadline = time.monotonic() + max(0.0, float(seconds))
        idle_rounds = 0
        while time.monotonic() < deadline:
            if self._dead or not self.is_alive():
                break
            chunk = self.read()
            if not chunk:
                idle_rounds += 1
                if total > 0 and idle_rounds >= 2:
                    break
                # Brief pause for more remote data.
                time.sleep(0.03)
                if self._dead or (not self.is_alive() and not self._buf):
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


def _channel_looks_closed(proc: Any) -> bool:
    """Best-effort: process or its channel reports closed/closing."""
    targets: list[Any] = [proc]
    chan = getattr(proc, "channel", None) or getattr(proc, "_chan", None)
    if chan is not None:
        targets.append(chan)
    for obj in targets:
        for attr in ("is_closing", "_closing", "is_closed", "_closed"):
            flag = getattr(obj, attr, None)
            if callable(flag):
                try:
                    if flag():
                        return True
                except Exception:  # noqa: BLE001
                    return True
            elif flag is True:
                return True
    return False


def _best_effort_close_pty_handle(handle: Any) -> None:
    """Best-effort close of a process / channel / SshPty. Never raises.

    Aligns with :meth:`SshPty.close` duck-typed ``process.close`` (sync close
    preferred). Awaitable ``close()`` is drained without blocking the bridge
    loop thread forever.
    """
    if handle is None:
        return
    targets: list[Any] = [handle]
    # Factory may return SshPty; unwrap so process.close is still reached.
    proc = getattr(handle, "process", None)
    if proc is None:
        proc = getattr(handle, "_process", None)
    if proc is not None and proc is not handle:
        targets.append(proc)

    closed_something = False
    for obj in targets:
        close = getattr(obj, "close", None)
        if not callable(close):
            continue
        try:
            maybe = close()
        except Exception:  # noqa: BLE001 - best-effort
            continue
        closed_something = True
        if inspect.isawaitable(maybe):
            _fire_and_forget_awaitable(maybe)
        # Prefer top-level close; nested channel usually torn down with process.
        break

    if closed_something:
        return

    for obj in targets:
        chan = getattr(obj, "channel", None) or getattr(obj, "_chan", None)
        if chan is None:
            continue
        cclose = getattr(chan, "close", None)
        if not callable(cclose):
            continue
        try:
            maybe = cclose()
            if inspect.isawaitable(maybe):
                _fire_and_forget_awaitable(maybe)
        except Exception:  # noqa: BLE001 - best-effort
            pass
        return


def _fire_and_forget_awaitable(aw: Any) -> None:
    """Drain an awaitable close without hanging the caller (best-effort)."""

    async def _drain() -> None:
        try:
            await asyncio.wait_for(cast(Awaitable[Any], aw), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        try:
            loop.create_task(_drain())
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        _run_maybe_async(_drain(), timeout_s=1.0)
    except Exception:  # noqa: BLE001
        pass


def _schedule_late_pty_close(task: asyncio.Task[Any]) -> None:
    """If *task* completes after open timeout, best-effort close its result.

    ``close`` is treated as idempotent; cancel / exception results are no-ops.
    Callback runs on the bridge loop when the task finishes.
    """

    def _on_done(t: asyncio.Task[Any]) -> None:
        try:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                return
            _best_effort_close_pty_handle(t.result())
        except Exception:  # noqa: BLE001 - never raise into the event loop
            pass

    if task.done():
        _on_done(task)
    else:
        task.add_done_callback(_on_done)


def _await_pty_open(
    result: Any,
    *,
    timeout_s: float,
    what: str = "create_process",
) -> Any:
    """Await *result* on the SSH bridge with a wall-clock open deadline.

    Maps open ``TimeoutError`` to ``TransportError(TIMEOUT)`` so Core
    ``open_screen`` surfaces a clear code instead of parking forever.

    When the open budget elapses, any *late* ``create_process`` /
    ``open_shell_pty`` result is closed best-effort (``close`` idempotent)
    so a channel that completes after timeout is not left orphaned with no
    owner. In-budget opens are unchanged (no extra close).
    """
    if not inspect.isawaitable(result):
        return result

    budget = max(0.0, float(timeout_s))

    async def _guarded() -> Any:
        # Own Task + shield so wait_for timeout does not cancel the open;
        # a late-completing process remains observable for best-effort close.
        if isinstance(result, asyncio.Task):
            task: asyncio.Task[Any] = result
        elif asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
        else:

            async def _await_any() -> Any:
                return await result

            task = asyncio.create_task(_await_any())

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except (TimeoutError, asyncio.CancelledError):
            # TIMEOUT or outer bridge cancel: still reap a late handle.
            _schedule_late_pty_close(task)
            raise

    # Bridge ceiling slightly above the open budget so the inner wait_for
    # owns the TIMEOUT mapping; a pure hang still cannot park forever.
    bridge_budget = budget + max(1.0, min(5.0, budget * 0.25 if budget > 0 else 1.0))
    try:
        return _run_maybe_async(_guarded(), timeout_s=bridge_budget)
    except TimeoutError as exc:
        msg = _safe_connect_msg(exc)
        raise TransportError(
            "TIMEOUT",
            f"ssh {what} timed out after {timeout_s}s: {msg}",
            details={"timeout_s": timeout_s, "what": what},
        ) from exc


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
    open_timeout_s: float | None = None,
) -> Any:
    """Create remote process/shell with PTY on *conn*.

    *open_timeout_s* bounds only the create await (default
    :data:`DEFAULT_SSH_PTY_OPEN_TIMEOUT_S`).
    """
    budget = (
        DEFAULT_SSH_PTY_OPEN_TIMEOUT_S
        if open_timeout_s is None
        else float(open_timeout_s)
    )
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

        work_cwd = coerce_cwd_path(cwd)
        if work_cwd:
            import shlex

            # Interactive default (no remote command) still wraps with cd so
            # the PTY starts in work_cwd rather than the login home.
            if remote_cmd is None:
                remote_cmd = "exec ${SHELL:-/bin/bash} -i"
            remote_cmd = f"cd {shlex.quote(work_cwd)} && {remote_cmd}"

        try:
            if remote_cmd is not None:
                # Positional command for asyncssh.
                return _await_pty_open(
                    create(remote_cmd, **kwargs),
                    timeout_s=budget,
                    what="create_process",
                )
            return _await_pty_open(
                create(**kwargs),
                timeout_s=budget,
                what="create_process",
            )
        except TypeError:
            # Alternate signature: try keyword command=
            if remote_cmd is not None:
                kwargs["command"] = remote_cmd
            return _await_pty_open(
                create(**kwargs),
                timeout_s=budget,
                what="create_process",
            )
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


def _ssh_read_once(
    process: Any,
    *,
    max_bytes: int,
    timeout_s: float,
    on_fatal: Callable[[BaseException], None] | None = None,
) -> bytes:
    """Read up to *max_bytes* from process stdout once.

    Soft timeouts and transient empty reads return ``b""``. Fatal channel
    exceptions invoke *on_fatal* (mark dead) and return ``b""`` so callers can
    observe ``is_alive() is False`` - they are not silently treated as idle.
    """

    def _handle_exc(exc: BaseException) -> bytes:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return b""
        if _is_fatal_channel_exc(exc):
            if on_fatal is not None:
                on_fatal(exc)
            return b""
        # Unknown errors: do not mark dead; still avoid raising into drain loops.
        return b""

    stdout = getattr(process, "stdout", None)
    if stdout is None:
        reader = getattr(process, "read", None)
        if callable(reader):
            try:
                raw = _run_maybe_async(reader(max_bytes))
                return _as_bytes(raw)
            except Exception as exc:  # noqa: BLE001
                return _handle_exc(exc)
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
        except Exception as exc:  # noqa: BLE001
            return _handle_exc(exc)

    # If read is sync, call directly with best-effort.
    if not inspect.iscoroutinefunction(read):
        try:
            raw = read(max_bytes)
            if inspect.isawaitable(raw):
                try:
                    return _run_maybe_async(
                        asyncio.wait_for(
                            cast(Awaitable[Any], raw),
                            timeout=timeout_s,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    return _handle_exc(exc)
            return _as_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            return _handle_exc(exc)

    try:
        return _run_maybe_async(_aread())
    except Exception as exc:  # noqa: BLE001
        return _handle_exc(exc)


def _as_bytes(raw: Any) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    return bytes(raw)
