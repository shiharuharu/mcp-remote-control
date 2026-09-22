"""Background capture: always drain serial into the ring while a session is open.

RX must enter the ring whether or not the agent calls views/send. OS serial
buffers are tiny; only process-side capture survives reboot log storms.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from mcp_remote_control.serial.buffer import LineRingBuffer


class _Readable(Protocol):
    def is_alive(self) -> bool: ...

    def read(self, max_bytes: int = 8192) -> bytes: ...


# Join budget for ``stop()``. A pump blocked inside ``link.read()`` cannot
# observe ``_stop`` until that read returns; exceeding this is a failure,
# not a clean stop.
_STOP_JOIN_TIMEOUT_S = 1.0


class CapturePump:
    """Daemon thread: poll *link* and feed *buffer* until ``stop()``."""

    # Stop after this many consecutive read errors: persistent failure means
    # the link is effectively dead, and retrying forever would mask a broken
    # device as a healthy pump (running=True while no data flows). The
    # threshold tolerates short USB glitches (~1s at the default 0.1s back-off)
    # before declaring the link dead.
    _MAX_CONSECUTIVE_ERRORS = 10

    def __init__(
        self,
        link: _Readable,
        buffer: LineRingBuffer,
        *,
        poll_s: float = 0.01,
        chunk: int = 65536,
        name: str = "console-capture",
    ) -> None:
        self._link = link
        self._buffer = buffer
        self._poll_s = max(0.001, float(poll_s))
        self._chunk = max(256, int(chunk))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._error: str | None = None
        self._reads = 0
        self._bytes = 0
        self._consecutive_errors = 0
        self._link_closed = False

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "running": self._thread.is_alive() and not self._stop.is_set(),
            "reads": self._reads,
            "bytes": self._bytes,
            "error": self._error,
            "link_closed": self._link_closed,
        }

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._stop.clear()
        self._thread.start()

    def stop(self, *, timeout_s: float | None = None) -> None:
        """Signal the pump thread and join it.

        Raises:
            TimeoutError: the thread is still alive after the join budget.
                The registry records this on ``close_errors`` so a stuck
                pump is never reported as a clean close.
        """
        budget = (
            _STOP_JOIN_TIMEOUT_S
            if timeout_s is None
            else max(0.0, float(timeout_s))
        )
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=budget)
        if self._thread.is_alive():
            msg = f"capture pump join timed out after {budget}s"
            self._error = f"TimeoutError: {msg}"
            raise TimeoutError(msg)

    def _run(self) -> None:
        # Prefer ``read_into``: SerialConsole holds ``_read_lock`` across the
        # pyserial read *and* the ring feed. The sync snarf (``_brief_pump``)
        # takes the same lock, so two producers cannot reorder ABC+DEF into
        # ADEFBC. Links without ``read_into`` (test stubs) fall back to
        # read-then-feed; they are single-producer.
        read_into = getattr(self._link, "read_into", None)
        while not self._stop.is_set():
            try:
                if not self._link.is_alive():
                    # Link closed externally - exit quietly.
                    self._link_closed = True
                    break
                if read_into is not None:
                    data = read_into(self._buffer, self._chunk)
                else:
                    data = self._link.read(self._chunk)
                    if data:
                        self._buffer.feed(data)
                # Successful read (data or empty): link is healthy again - clear
                # sticky error so views/open do not keep advising reopen after a
                # short glitch. Consecutive-error stop still sets link_closed.
                self._consecutive_errors = 0
                self._error = None
                if data:
                    self._reads += 1
                    self._bytes += len(data)
                else:
                    time.sleep(self._poll_s)
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._MAX_CONSECUTIVE_ERRORS:
                    # Persistent failure: stop masking a dead link as healthy
                    # so the agent can reopen instead of trusting running=True.
                    self._link_closed = True
                    break
                # Brief back-off; keep trying for transient USB glitches.
                time.sleep(min(0.2, self._poll_s * 10))
