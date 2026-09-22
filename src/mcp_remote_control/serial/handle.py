"""Open a serial console as a bounded, thread-safe byte stream."""

from __future__ import annotations

import threading
from typing import Any

# Wall-clock budget for ``close()``: acquiring ``_read_lock`` and the
# underlying ``ser.close()`` each get this many seconds. A hang is a
# failure, not a clean return.
_CLOSE_TIMEOUT_S = 1.0


class _RxCommitted(bytes):
    """Wire bytes already fed to the bound ring under the order lock.

    ``read()`` auto-commits when a session ring is bound, then returns this
    marker so a later ``buffer.feed`` of the same object (the sync snarf
    shape) does not commit twice.
    """

    _mrc_rx_committed = True


def _call_with_timeout(fn: Any, *, timeout_s: float, what: str) -> None:
    """Run a blocking teardown call; raise if it exceeds *timeout_s*."""
    box: list[BaseException] = []
    done = threading.Event()

    def _run() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            box.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=_run, name=f"serial-{what}", daemon=True)
    worker.start()
    if not done.wait(timeout=max(0.0, float(timeout_s))):
        raise TimeoutError(f"{what} timed out after {timeout_s}s")
    if box:
        raise box[0]


class SerialConsole:
    """Bidirectional serial link for embedded control.

    The console ops and the capture pump use one shape: ``read`` / ``read_into``
    / ``write`` / ``close`` / ``is_alive`` plus the ``cols`` / ``rows`` label
    fields.
    """

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
        # Session ring, if bound. ``read()`` commits into it under
        # ``_read_lock`` so the sync snarf (read then feed) cannot pass a
        # pump ``read_into`` and reorder RX.
        self._rx_buffer: Any | None = None
        # Per-console order lock: held across the underlying pyserial read
        # *and* the matching ring feed. CapturePump uses ``read_into``;
        # ``_brief_pump`` uses ``read()`` + ``buffer.feed``. Both paths
        # take this lock so two producers cannot commit ABC+DEF as ADEFBC.
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
            # Default exclusive=True so a second process (or a second
            # SerialConsole that slipped past the process-local registry)
            # cannot open the same POSIX TTY and split RX. pyserial only
            # enforces this on POSIX; Windows ignores the flag. Callers
            # may pass exclusive=False to opt out.
            kwargs["exclusive"] = True if exclusive is None else bool(exclusive)
            self._ser = pyserial.Serial(**kwargs)

    def bind_rx_buffer(self, buffer: Any | None) -> None:
        """Attach (or detach) the session ring used by ``read()`` auto-commit."""
        self._rx_buffer = buffer

    def is_alive(self) -> bool:
        if self._closed or self._ser is None:
            return False
        try:
            return bool(self._ser.is_open)
        except Exception:  # noqa: BLE001
            return False

    def _read_unlocked(self, max_bytes: int) -> bytes:
        """Single pyserial read. Caller must hold ``_read_lock``."""
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
        # _brief_pump) catch at their own layer.
        data = self._ser.read(n)
        return bytes(data) if data else b""

    def read(self, max_bytes: int = 8192) -> bytes:
        if not self.is_alive():
            return b""
        # Hold the lock for the pyserial read *and* the bound-ring feed so
        # a concurrent ``read_into`` cannot commit a later wire chunk first.
        with self._read_lock:
            data = self._read_unlocked(max_bytes)
            if data and self._rx_buffer is not None:
                self._rx_buffer.feed(data)
                # Mark so the caller's subsequent ``buffer.feed(data)``
                # (the ``_brief_pump`` shape) is a no-op, not a double commit.
                return _RxCommitted(data)
            return data

    def read_into(self, buffer: Any, max_bytes: int = 8192) -> bytes:
        """Read RX and feed *buffer* under the same order lock.

        CapturePump uses this so read+feed is atomic vs the sync snarf.
        """
        if not self.is_alive():
            return b""
        with self._read_lock:
            data = self._read_unlocked(max_bytes)
            if data:
                buffer.feed(data)
            return data

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

    def close(self, *, timeout_s: float | None = None) -> None:
        """Tear down the link. Lock wait and ``ser.close`` are budgeted.

        Raises:
            TimeoutError: ``_read_lock`` stayed held (an in-flight read)
                or ``ser.close`` did not finish inside the budget. The
                registry records this on ``close_errors``; a hang is never
                a clean close.
        """
        budget = (
            _CLOSE_TIMEOUT_S if timeout_s is None else max(0.0, float(timeout_s))
        )
        # Mark closed first so is_alive() fails for new read/write callers.
        self._closed = True
        # Timed acquire: do not wait forever while CapturePump / _brief_pump
        # / drain_for holds the lock inside a blocking pyserial read.
        # Lock timeout still clears _ser and invokes ser.close so the OS
        # fd is not leaked (a later open would otherwise see DeviceBusy).
        got_lock = self._read_lock.acquire(timeout=budget)
        try:
            ser = self._ser
            self._ser = None
        finally:
            if got_lock:
                self._read_lock.release()
        close_err: BaseException | None = None
        if ser is not None:
            # Propagate close failure so SerialRegistry best-effort teardown
            # can record it on SerialSession.close_errors. The registry never
            # raises; it catches and collects. If close failures were silent,
            # the next open would hit "device busy" with an empty close_error.
            # ser.close() runs outside the lock so a slow OS close does not
            # block other threads waiting only to observe _ser is None. The
            # call itself is also budgeted: a hung OS close is TimeoutError.
            try:
                _call_with_timeout(ser.close, timeout_s=budget, what="ser.close")
            except BaseException as exc:  # noqa: BLE001
                close_err = exc
        if not got_lock:
            raise TimeoutError(
                f"serial close timed out waiting for read lock after {budget}s"
            ) from close_err
        if close_err is not None:
            raise close_err
