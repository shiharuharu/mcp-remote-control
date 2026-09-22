"""Process-local registry of open serial consoles and their capture pumps.

Thread safety: the MCP server may dispatch sync tool calls on a thread pool,
so concurrent ``console`` ops can race. All ``_sessions`` / ``_n`` /
``_by_path`` read-modify-write sequences are guarded by an instance
``threading.RLock``.

Device exclusivity: each serial *path* (normalized device name) may belong
to at most one open session. Two CapturePumps on the same port would split
the RX stream between rings. ``add`` rejects a second session for an
occupied path with :class:`DeviceBusyError` (existing session id).
``get_by_path`` is the read-side probe used by ``open_console`` for a
fast reject before opening hardware.

Close-error contract (for ``core/console_ops.close_console`` and peers):
``SerialRegistry.remove`` and ``clear`` perform a best-effort three-step
teardown (``stop_capture`` -> ``buffer.flush_partial`` -> ``console.close``)
and never raise. Per-step failures are collected on the removed session as
``SerialSession.close_errors: list[str]`` - each entry is
``"<step>: <ExcType>: <message>"`` where *step* is one of ``stop_capture``,
``flush_partial``, ``console.close``. A capture-pump join or console close
that exceeds its budget raises ``TimeoutError`` and is recorded the same
way; a silent timeout is not a successful step. The list is empty when
every step succeeded. Callers may surface a ``close_error`` field when the
list is non-empty. Return type stays ``SerialSession | None``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole


class DeviceBusyError(Exception):
    """Raised when ``SerialRegistry.add`` finds *path* already open.

    ``existing_id`` is the session id that currently owns the device so
    agents can ``views``/``close`` that id instead of opening a second pump.
    """

    def __init__(self, path: str, existing_id: str) -> None:
        self.path = path
        self.existing_id = existing_id
        super().__init__(f"device {path!r} already open as {existing_id}")


@dataclass
class SerialSession:
    id: str
    console: SerialConsole
    path: str
    baud: int
    buffer: LineRingBuffer
    pump: CapturePump | None = None
    label: str | None = None
    # Peer text codec this session's ring reads with, resolved once at open
    # (``resolve_text_codec``): the session never re-reads a configuration, so
    # a live capture cannot switch codecs mid-stream. None keeps the historic
    # utf-8 read; an unusable configured name also lands here as utf-8, having
    # warned at open. The buffer carries its own resolved copy for decoding;
    # this one is what result rows report, so a reader with only the session
    # id can tell which codec produced the text.
    text_encoding: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # Filled by SerialRegistry.remove / clear on best-effort teardown.
    # Empty list means every step succeeded. See module docstring.
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
        # path -> session id (at most one open console per device).
        self._by_path: dict[str, str] = {}
        self._n = 0
        self._lock = threading.RLock()

    def allocate_id(self) -> str:
        with self._lock:
            self._n += 1
            return f"con_{self._n:02d}"

    def add(self, session: SerialSession) -> SerialSession:
        """Register *session* and start its capture pump.

        Raises:
            DeviceBusyError: another session already owns ``session.path``.
                The new session is not registered and its pump is not started.
            Exception: ``start_capture`` failed. The session is not left in
                ``_sessions`` / ``_by_path``.
        """
        with self._lock:
            occupied_by = self._by_path.get(session.path)
            if occupied_by is not None:
                raise DeviceBusyError(session.path, occupied_by)
            bind = getattr(session.console, "bind_rx_buffer", None)
            if callable(bind):
                bind(session.buffer)
            self._sessions[session.id] = session
            self._by_path[session.path] = session.id
            try:
                session.start_capture()
            except Exception:
                self._sessions.pop(session.id, None)
                if self._by_path.get(session.path) == session.id:
                    del self._by_path[session.path]
                if callable(bind):
                    bind(None)
                try:
                    session.stop_capture()
                except Exception:  # noqa: BLE001
                    pass
                raise
            return session

    def get(self, sid: str) -> SerialSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def get_by_path(self, path: str) -> SerialSession | None:
        """Return the open session that owns *path*, or None."""
        with self._lock:
            sid = self._by_path.get(path)
            if sid is None:
                return None
            return self._sessions.get(sid)

    def remove(self, sid: str) -> SerialSession | None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
            if sess is not None and self._by_path.get(sess.path) == sid:
                del self._by_path[sess.path]
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
            self._by_path.clear()
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
