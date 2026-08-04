"""Process-local registry of open :class:`PsSession` objects.

The MCP server dispatches sync tools on a thread pool, so concurrent ``ps``
calls can race. All ``_sessions`` / ``_seq`` read-modify-write sequences are
guarded by an instance :class:`threading.RLock`. Session ``close`` runs
outside the lock after the entry is popped.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from mcp_remote_control.ps.session import PsSession


class PsRegistry:
    """In-process map of open PowerShell sessions (keyed by session id)."""

    def __init__(self) -> None:
        self._sessions: dict[str, PsSession] = {}
        self._seq: int = 0
        self._lock = threading.RLock()

    def allocate_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"ps_{self._seq:02d}"

    def get(self, session_id: str) -> PsSession | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(str(session_id).strip())

    def add(self, session: PsSession) -> PsSession:
        with self._lock:
            self._sessions[session.id] = session
            return session

    def remove(self, session_id: str) -> PsSession | None:
        if not session_id:
            return None
        with self._lock:
            sess = self._sessions.pop(str(session_id).strip(), None)
        if sess is not None:
            # Close outside the lock; the session is already unregistered.
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
        return sess

    def list_open(self) -> list[PsSession]:
        with self._lock:
            return [self._sessions[k] for k in sorted(self._sessions)]

    def list_for_endpoint(self, ep: str) -> list[PsSession]:
        name = str(ep).strip()
        return [s for s in self.list_open() if s.ep == name]

    def close_for_endpoint(self, ep: str) -> int:
        """Close all PS sessions attached to *ep*. Returns count closed."""
        n = 0
        for sess in list(self.list_for_endpoint(ep)):
            self.remove(sess.id)
            n += 1
        return n

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._seq = 0
        for sess in sessions:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __iter__(self) -> Iterable[PsSession]:
        return iter(self.list_open())


_registry: PsRegistry | None = None
_registry_lock = threading.Lock()


def get_ps_registry() -> PsRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = PsRegistry()
        return _registry


def reset_ps_registry() -> PsRegistry:
    """Replace the process registry (closes all open sessions)."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            try:
                _registry.clear()
            except Exception:  # noqa: BLE001
                pass
        _registry = PsRegistry()
        return _registry
