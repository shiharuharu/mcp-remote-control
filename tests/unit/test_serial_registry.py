"""SerialRegistry hygiene and DeviceBusy tests.

Covers ``clear()`` id-counter reset, numeric ``list_open`` sort, remove/clear
close-error reporting, concurrent allocate/add/remove, and same-path
DeviceBusyError.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import cast

import pytest

from mcp_remote_control.endpoint.registry import reset_registry
from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.registry import (
    DeviceBusyError,
    SerialRegistry,
    SerialSession,
    reset_serial_registry,
)


class _DummyPump:
    """Pre-set pump so ``SerialSession.start_capture`` is a no-op (no thread)."""

    def stop(self, *, timeout_s: float = 1.0) -> None:
        pass


class _DummyConsole:
    """Minimal serial console stub (no real pyserial)."""

    def __init__(self, path: str = "/dev/ttyUSB0") -> None:
        self.port = path
        self.closed = False

    def is_alive(self) -> bool:
        return False

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


def _make_serial_session(sid: str, *, console: object | None = None) -> SerialSession:
    return SerialSession(
        id=sid,
        console=cast(
            SerialConsole,
            console if console is not None else _DummyConsole(),
        ),
        path=f"/dev/ttyUSB{sid}",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),  # non-None => start_capture is a no-op
    )


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    reset_registry()
    reset_serial_registry()
    yield
    reset_registry()
    reset_serial_registry()


# ---------------------------------------------------------------------------
# SerialRegistry hygiene
# ---------------------------------------------------------------------------


def test_serial_clear_resets_counter() -> None:
    reg = SerialRegistry()
    assert reg.allocate_id() == "con_01"
    assert reg.allocate_id() == "con_02"
    assert reg.allocate_id() == "con_03"
    reg.clear()
    # After clear, the counter resets so the next id is con_01 again.
    assert reg.allocate_id() == "con_01"


def test_serial_list_open_numeric_sort() -> None:
    reg = SerialRegistry()
    # Insert in a deliberately non-numeric, lexicographically misleading order.
    for sid in ("con_100", "con_2", "con_11"):
        reg.add(_make_serial_session(sid))
    ordered = [s.id for s in reg.list_open()]
    # Numeric order, not lexicographic (lex would give con_100 < con_11 < con_2).
    assert ordered == ["con_2", "con_11", "con_100"]
    reg.clear()


def test_serial_remove_exposes_close_errors() -> None:
    reg = SerialRegistry()

    class _BoomConsole:
        def __init__(self) -> None:
            self.port = "/dev/ttyBOOM"

        def is_alive(self) -> bool:
            return False

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            raise OSError("device gone")

    sess = SerialSession(
        id="con_01",
        console=cast(SerialConsole, _BoomConsole()),
        path="/dev/ttyBOOM",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(sess)
    removed = reg.remove("con_01")
    assert removed is not None
    assert removed is sess
    # console.close raised -> captured into close_errors with step + exc type.
    assert any(
        "console.close" in e and "OSError" in e and "device gone" in e
        for e in removed.close_errors
    ), f"expected console.close error in close_errors, got: {removed.close_errors}"
    # stop_capture and flush_partial succeeded -> not in the error list.
    assert all("stop_capture" not in e for e in removed.close_errors)
    assert all("flush_partial" not in e for e in removed.close_errors)


def test_serial_remove_no_errors_on_clean_close() -> None:
    reg = SerialRegistry()
    reg.add(_make_serial_session("con_01"))
    removed = reg.remove("con_01")
    assert removed is not None
    assert removed.close_errors == []


def test_serial_remove_missing_returns_none() -> None:
    reg = SerialRegistry()
    assert reg.remove("nope") is None


def test_serial_clear_collects_close_errors() -> None:
    reg = SerialRegistry()

    class _BoomConsole:
        def __init__(self) -> None:
            self.port = "/dev/ttyBOOM"

        def is_alive(self) -> bool:
            return False

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            raise OSError("device gone")

    sess = SerialSession(
        id="con_01",
        console=cast(SerialConsole, _BoomConsole()),
        path="/dev/ttyBOOM",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(sess)
    reg.clear()
    # clear() removed the session; the close error is recorded on the session.
    assert reg.get("con_01") is None
    assert any("console.close" in e for e in sess.close_errors)


# ---------------------------------------------------------------------------
# SerialRegistry concurrency (smoke): allocate/list/remove under lock
# ---------------------------------------------------------------------------


def test_serial_allocate_add_remove_concurrent() -> None:
    reg = SerialRegistry()
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    N = 8
    PER = 10

    def worker() -> None:
        local_errs: list[BaseException] = []
        for _ in range(PER):
            try:
                sid = reg.allocate_id()
                reg.add(_make_serial_session(sid))
                reg.list_open()
                reg.remove(sid)
            except BaseException as exc:  # noqa: BLE001
                local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    # All allocated sessions were removed by their owning thread.
    assert reg.list_open() == []


def test_serial_add_same_path_raises_device_busy() -> None:
    """Registry enforces at most one session per device path."""
    reg = SerialRegistry()
    s1 = SerialSession(
        id="con_01",
        console=cast(SerialConsole, _DummyConsole()),
        path="/dev/ttyUSB0",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    s2 = SerialSession(
        id="con_02",
        console=cast(SerialConsole, _DummyConsole()),
        path="/dev/ttyUSB0",
        baud=9600,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(s1)
    with pytest.raises(DeviceBusyError) as ei:
        reg.add(s2)
    assert ei.value.existing_id == "con_01"
    assert ei.value.path == "/dev/ttyUSB0"
    assert reg.get("con_02") is None
    assert reg.get_by_path("/dev/ttyUSB0") is s1
    # Distinct path still accepted.
    s3 = SerialSession(
        id="con_03",
        console=cast(SerialConsole, _DummyConsole()),
        path="/dev/ttyUSB1",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(s3)
    assert reg.get_by_path("/dev/ttyUSB1") is s3
    reg.remove("con_01")
    assert reg.get_by_path("/dev/ttyUSB0") is None
    # Path free after remove - re-add succeeds.
    s4 = SerialSession(
        id="con_04",
        console=cast(SerialConsole, _DummyConsole()),
        path="/dev/ttyUSB0",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(s4)
    assert reg.get_by_path("/dev/ttyUSB0") is s4
    reg.clear()
    assert reg.get_by_path("/dev/ttyUSB0") is None
    assert reg.get_by_path("/dev/ttyUSB1") is None


def test_add_start_capture_failure_rolls_back_maps() -> None:
    """start_capture raise must not leave _by_path / _sessions residue."""
    reg = SerialRegistry()
    sess = _make_serial_session("con_01")
    # Pre-set pump is a no-op start; replace with a raising start_capture.
    sess.pump = None

    def _boom() -> None:
        raise RuntimeError("pump start failed")

    sess.start_capture = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="pump start failed"):
        reg.add(sess)
    assert reg.get("con_01") is None
    assert reg.get_by_path(sess.path) is None
    assert "con_01" not in reg._sessions
    assert sess.path not in reg._by_path
