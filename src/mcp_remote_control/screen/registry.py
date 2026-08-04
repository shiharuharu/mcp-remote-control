"""Process-local registry of open ScreenSession objects.

Thread safety: the MCP server dispatches sync tool functions to a thread
pool, so concurrent ``screen`` calls can race. All ``_sessions`` /
``_seq`` read-modify-write sequences are guarded by an instance
``threading.RLock``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from mcp_remote_control.screen.session import ScreenSession


class ScreenRegistry:
    """In-process map of open screens (keyed by screen id)."""

    def __init__(self) -> None:
        self._sessions: dict[str, ScreenSession] = {}
        self._seq: int = 0
        self._lock = threading.RLock()

    def allocate_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"scr_{self._seq:02d}"

    def get(self, screen_id: str) -> ScreenSession | None:
        if not screen_id:
            return None
        with self._lock:
            return self._sessions.get(str(screen_id).strip())

    def add(self, session: ScreenSession) -> ScreenSession:
        with self._lock:
            self._sessions[session.id] = session
            return session

    def remove(self, screen_id: str) -> ScreenSession | None:
        if not screen_id:
            return None
        with self._lock:
            sess = self._sessions.pop(str(screen_id).strip(), None)
        if sess is not None:
            # Best-effort close outside the lock; session already removed.
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
        return sess

    def list_open(self) -> list[ScreenSession]:
        with self._lock:
            return [self._sessions[k] for k in sorted(self._sessions)]

    def list_for_endpoint(self, ep: str) -> list[ScreenSession]:
        name = str(ep).strip()
        return [s for s in self.list_open() if s.ep == name]

    def close_for_endpoint(self, ep: str) -> int:
        """Close all screens attached to *ep*. Returns count closed."""
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
        # Best-effort close outside the lock.
        for sess in sessions:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __iter__(self) -> Iterable[ScreenSession]:
        return iter(self.list_open())


_registry: ScreenRegistry | None = None
_registry_lock = threading.Lock()


def get_screen_registry() -> ScreenRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ScreenRegistry()
        return _registry


def reset_screen_registry() -> ScreenRegistry:
    """Replace the process-global registry. Closes all open screens."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            try:
                _registry.clear()
            except Exception:  # noqa: BLE001
                pass
        _registry = ScreenRegistry()
        return _registry
