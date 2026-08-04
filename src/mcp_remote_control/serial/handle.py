"""Open a serial console as a PtyHandle-compatible byte stream."""

from __future__ import annotations

import threading
import time
from typing import Any


class SerialConsole:
    """Bidirectional serial link for embedded control (PtyHandle-shaped)."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 0.05,
        write_timeout: float = 2.0,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        exclusive: bool | None = None,
        serial_factory: Any | None = None,
    ) -> None:
        self.port = str(port)
        self.baudrate = int(baudrate)
        self.cols = 80
        self.rows = 24
        self.cwd: str | None = None
        self._closed = False
        self._ser: Any = None
        # Per-console read lock: serializes every read of the underlying
        # pyserial.Serial. Background CapturePump and the sync snarf
        # (``_brief_pump``) both call ``self.read()``, so a burst during
        # overlap cannot split between two readers and reorder the ring.
        # ``drain_for`` also uses ``self.read()`` and is covered.
        self._read_lock = threading.Lock()

        if serial_factory is not None:
            self._ser = serial_factory()
        else:
            try:
                import serial as pyserial
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "pyserial is required for serial console (pip/uv install pyserial)"
                ) from exc
            kwargs: dict[str, Any] = {
                "port": self.port,
                "baudrate": self.baudrate,
                "timeout": timeout,
                "write_timeout": write_timeout,
                "bytesize": bytesize,
                "parity": parity,
                "stopbits": stopbits,
                "xonxoff": xonxoff,
                "rtscts": rtscts,
                "dsrdtr": dsrdtr,
            }
            if exclusive is not None:
                kwargs["exclusive"] = exclusive
            self._ser = pyserial.Serial(**kwargs)

    def is_alive(self) -> bool:
        if self._closed or self._ser is None:
            return False
        try:
            return bool(self._ser.is_open)
        except Exception:  # noqa: BLE001
            return False

    def exit_code(self) -> int | None:
        return None if self.is_alive() else 0

    def read(self, max_bytes: int = 8192) -> bytes:
        if not self.is_alive():
            return b""
        # Hold the lock for the full (timeout-bounded) pyserial read so two
        # concurrent readers never split one burst between them.
        with self._read_lock:
            # close() may have nulled _ser after our is_alive() check.
            if self._ser is None:
                return b""
            waiting = getattr(self._ser, "in_waiting", None)
            if waiting is not None:
                n = min(int(waiting), max_bytes)
                if n <= 0:
                    # Still try a short read when timeout is set.
                    n = max_bytes
            else:
                n = max_bytes
            # Propagate read errors so CapturePump can record them and stop
            # after persistent failure. Callers that need tolerance (pump,
            # _brief_pump, drain_for) catch at their own layer.
            data = self._ser.read(n)
            return bytes(data) if data else b""

    def write(self, data: bytes) -> int:
        if not self.is_alive():
            return 0
        if not data:
            return 0
        # Propagate write errors (symmetric with read). Core send_console
        # classifies: real exception type/message, or PARTIAL_WRITE when a
        # recoverable partial count is available (e.g. ``.written``). Only
        # send uses write; the pump only reads. Re-check _ser: close() may
        # have torn it down after is_alive() returned True.
        ser = self._ser
        if ser is None:
            return 0
        n = ser.write(data)
        return int(n) if n is not None else len(data)

    def resize(self, cols: int, rows: int) -> None:
        # Serial has no winsize; kept for PtyHandle compatibility.
        self.cols = int(cols)
        self.rows = int(rows)

    def drain_for(self, seconds: float, *, on_data: Any | None = None) -> int:
        deadline = time.monotonic() + max(0.0, float(seconds))
        total = 0
        while time.monotonic() < deadline:
            try:
                chunk = self.read(4096)
            except Exception:  # noqa: BLE001
                # read() propagates link errors; keep drain_for tolerant so a
                # flaky link does not abort a settle/snarf loop.
                chunk = b""
            if chunk:
                total += len(chunk)
                if on_data is not None:
                    on_data(chunk)
            else:
                time.sleep(0.01)
        return total

    def close(self) -> None:
        # Mark closed first so is_alive() fails for new read/write callers.
        self._closed = True
        # Hold the read lock while tearing down _ser so an in-flight
        # CapturePump / _brief_pump / drain_for read finishes (or never
        # starts on a half-nulled handle). Without this, nulling _ser while
        # another thread is inside self._ser.read() races.
        with self._read_lock:
            ser = self._ser
            self._ser = None
        if ser is not None:
            # Propagate close failure so SerialRegistry best-effort teardown
            # can record it on SerialSession.close_errors. The registry never
            # raises; it catches and collects. If close failures were silent,
            # the next open would hit "device busy" with an empty close_error.
            # ser.close() runs outside the lock so a slow OS close does not
            # block other threads waiting only to observe _ser is None.
            ser.close()
