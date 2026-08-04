"""PsSession: process-local handle for a persistent PowerShell runspace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PsSession:
    """One PowerShell session bound to a winrm endpoint.

    Text invoke only (not a screen PTY). Runspace state is shared across
    sequential invokes until close.
    """

    id: str
    ep: str
    handle: Any
    transport: Any = None  # WinRMTransport (or compatible runspace API)
    location: str | None = None
    created_at: float = field(default_factory=time.time)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def cwd(self) -> str | None:
        """Alias: PS location is the session cwd for Agent-track output."""
        return self.location

    @cwd.setter
    def cwd(self, value: str | None) -> None:
        self.location = value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        transport = self.transport
        handle = self.handle
        self.handle = None
        self.transport = None
        if transport is None or handle is None:
            return
        closer = getattr(transport, "close_runspace", None)
        if callable(closer):
            try:
                closer(handle)
            except Exception:  # noqa: BLE001
                pass
        else:
            raw_close = getattr(handle, "close", None)
            if callable(raw_close):
                try:
                    raw_close()
                except Exception:  # noqa: BLE001
                    pass
