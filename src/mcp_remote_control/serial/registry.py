"""Process-local registry of open serial consoles and their capture pumps.

Thread safety: the MCP server may dispatch sync tool calls on a thread pool,
so concurrent ``console`` ops can race. All ``_sessions`` / ``_n``
read-modify-write sequences are guarded by an instance ``threading.RLock``.

Close-error contract (for ``core/console_ops.close_console`` and peers):
``SerialRegistry.remove`` and ``clear`` perform a best-effort three-step
teardown (``stop_capture`` → ``buffer.flush_partial`` → ``console.close``)
and never raise. Per-step failures are collected on the removed session as
``SerialSession.close_errors: list[str]`` — each entry is
``"<step>: <ExcType>: <message>"`` where *step* is one of ``stop_capture``,
``flush_partial``, ``console.close``. The list is empty when every step
succeeded. Callers may surface a ``close_error`` field when the list is
non-empty. Return type stays ``SerialSession | None``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole


@dataclass
class SerialSession:
    id: str
    console: SerialConsole
    path: str
    baud: int
    buffer: LineRingBuffer
    pump: CapturePump | None = None
    label: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # Filled by SerialRegistry.remove / clear on best-effort teardown.
    # Empty list ⇒ every step succeeded. See module docstring.
    close_errors: list[str] = field(default_factory=list)

    def start_capture(self) -> None:
        if self.pump is not None:
            return
        self.pump = CapturePump(
            self.console,
            self.buffer,
            name=f"console-capture-{self.id}",
        )
        self.pump.start()

    def stop_capture(self) -> None:
        pump = self.pump
        self.pump = None
        if pump is not None:
            pump.stop()


def _con_sort_key(sid: str) -> tuple[int, str]:
    """Natural numeric sort key for ``con_NN`` ids.

    Lexicographic sort would order ``con_100`` before ``con_11``; sorting by
    the integer suffix gives ``con_2 < con_11 < con_100``. The string id is a
    tiebreaker for unexpected non-numeric ids.
    """
    digits = "".join(ch for ch in sid if ch.isdigit())
    return (int(digits) if digits else 0, sid)


class SerialRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, SerialSession] = {}
        self._n = 0
        self._lock = threading.RLock()

    def allocate_id(self) -> str:
        with self._lock:
            self._n += 1
            return f"con_{self._n:02d}"

    def add(self, session: SerialSession) -> SerialSession:
        with self._lock:
            self._sessions[session.id] = session
            session.start_capture()
            return session

    def get(self, sid: str) -> SerialSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def remove(self, sid: str) -> SerialSession | None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return None
        self._close_session(sess)
        return sess

    def list_open(self) -> list[SerialSession]:
        with self._lock:
            return [
                self._sessions[i]
                for i in sorted(self._sessions, key=_con_sort_key)
            ]

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._n = 0  # reset id counter (mirror screen/ps registries)
        for sess in sessions:
            self._close_session(sess)

    @staticmethod
    def _close_session(sess: SerialSession) -> None:
        """Best-effort three-step teardown; collect errors on *sess*.

        Never raises. Each per-step exception is appended to
        ``sess.close_errors`` as ``"<step>: <ExcType>: <message>"`` so callers
        can surface ``close_error`` without the registry raising.
        """
        errors: list[str] = []
        try:
            sess.stop_capture()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stop_capture: {type(exc).__name__}: {exc}")
        try:
            sess.buffer.flush_partial()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"flush_partial: {type(exc).__name__}: {exc}")
        try:
            sess.console.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"console.close: {type(exc).__name__}: {exc}")
        sess.close_errors = errors


_reg: SerialRegistry | None = None
_reg_lock = threading.Lock()


def get_serial_registry() -> SerialRegistry:
    global _reg
    with _reg_lock:
        if _reg is None:
            _reg = SerialRegistry()
        return _reg


def reset_serial_registry() -> SerialRegistry:
    global _reg
    with _reg_lock:
        if _reg is not None:
            try:
                _reg.clear()
            except Exception:  # noqa: BLE001
                pass
        _reg = SerialRegistry()
        return _reg
