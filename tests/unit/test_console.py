"""Console session: background capture + console_* ops / pump / allowlist."""

from __future__ import annotations

import base64
import threading
import time

import pytest

from mcp_remote_control.core import console_ops
from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.ports import SerialConsoleInfo
from mcp_remote_control.serial.registry import (
    SerialRegistry,
    SerialSession,
    get_serial_registry,
    reset_serial_registry,
)


class _ThreadSafeFakeSer:
    """Serial mock safe for CapturePump background thread."""

    def __init__(self) -> None:
        self.is_open = True
        self._lock = threading.Lock()
        self._rx = bytearray()
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def inject(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)

    def read(self, n: int) -> bytes:
        with self._lock:
            chunk = bytes(self._rx[:n])
            del self._rx[:n]
            return chunk

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
            self._rx.extend(b"U-Boot ok\n" + data)
            return len(data)

    def close(self) -> None:
        self.is_open = False


def test_background_capture_without_views_calls(monkeypatch) -> None:
    """RX enters buffer while agent is idle (no send/views)."""
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    r = console_ops.run("open", path="COM9", baud=115200, max_lines=5000)
    assert r.is_ok(), r.fields
    assert r.fields.get("capture") == "background"
    assert r.fields.get("pump_running") == 1
    sid = str(r.fields["id"])

    # Agent idle: device reboots and spams log
    for i in range(30):
        fake.inject(f"boot-line-{i}\n".encode())
    # Wait for CapturePump to drain
    deadline = time.time() + 2.0
    body = ""
    while time.time() < deadline:
        v = console_ops.run("views", id=sid, mode="tail", n=50, settle_ms=0)
        body = v.body or ""
        if "boot-line-29" in body and "boot-line-0" in (
            console_ops.run(
                "views", id=sid, mode="contains", contains="boot-line-0", settle_ms=0
            ).body
            or ""
        ):
            break
        time.sleep(0.02)

    assert "boot-line-29" in body
    # earliest lines still findable if buffer large enough
    v0 = console_ops.run(
        "views", id=sid, mode="contains", contains="boot-line-0", settle_ms=0
    )
    assert v0.is_ok() and v0.body and "boot-line-0" in v0.body

    sess = get_serial_registry().get(sid)
    assert sess is not None and sess.pump is not None
    assert sess.pump.stats.get("running") is True
    assert sess.buffer.line_count >= 30

    console_ops.run("close", id=sid)
    assert get_serial_registry().get(sid) is None


def test_console_send_views_with_fake_link(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    fake.inject(b"power-on\n")

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    r = console_ops.run("open", path="COM9", baud=115200, max_lines=1000)
    assert r.is_ok(), r.fields
    sid = r.fields["id"]
    assert str(sid).startswith("con_")

    # Wait for background pump to ingest preload
    body = ""
    for _ in range(50):
        v0 = console_ops.run("views", id=sid, mode="tail", n=50, settle_ms=0)
        body = v0.body or ""
        if "power-on" in body:
            break
        time.sleep(0.02)
    assert "power-on" in body

    s = console_ops.run("send", id=sid, data="help", newline=True)
    assert s.is_ok()
    assert s.fields.get("bytes") == 5

    found = False
    for _ in range(50):
        v1 = console_ops.run(
            "views", id=sid, mode="contains", contains="U-Boot", settle_ms=0
        )
        if v1.body and "U-Boot" in v1.body:
            found = True
            break
        time.sleep(0.02)
    assert found

    c = console_ops.run("close", id=sid)
    assert c.is_ok()
    assert get_serial_registry().get(sid) is None


def test_views_hard_cap_truncated(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)
    r = console_ops.run("open", path="COM1", max_lines=50_000)
    sid = str(r.fields["id"])
    for i in range(300):
        fake.inject(f"L{i}\n".encode())
    # wait pump
    for _ in range(100):
        if get_serial_registry().get(sid).buffer.line_count >= 300:  # type: ignore[union-attr]
            break
        time.sleep(0.01)
    v = console_ops.run(
        "views", id=sid, mode="since", since=0, max_lines=50, settle_ms=0
    )
    assert v.is_ok()
    assert v.fields.get("truncated") == 1
    assert int(v.fields.get("n") or 0) == 50
    console_ops.run("close", id=sid)


def test_console_list_prefix(monkeypatch) -> None:
    fake = [
        SerialConsoleInfo(device="COM5", name="COM5", description="USB", link="usb"),
    ]
    monkeypatch.setattr(
        "mcp_remote_control.core.console_ops.list_serial_consoles",
        lambda **kw: fake,
    )
    r = console_ops.run("list")
    assert r.kind == "console"
    assert r.fields.get("op") == "list"
    assert "device=COM5" in (r.body or "")


def test_endpoint_rejects_console_ops() -> None:
    from mcp_remote_control.core import endpoint_ops

    r = endpoint_ops.run("console_list")
    assert r.status == "error"
    assert r.code == "INVALID_OP"


# ---------------------------------------------------------------------------
# Serial/console: incremental follow, view_tail race, O(N^2) feed,
# pump error surfacing, send failure/partial, open device validation,
# data_b64 newline, int coercion guard, close-error surfacing.
# ---------------------------------------------------------------------------


def _patch_console_open(monkeypatch, fake) -> None:
    """Monkeypatch console_ops.SerialConsole to build from *fake*."""

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)


def test_tail_then_since_keeps_committed_line(monkeypatch) -> None:
    """to_seq points at the last COMMITTED seq, so since=<to_seq> returns the
    line that commits at the synthetic partial's seq (was skipped before)."""
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()  # rx empty -> pump idles, no buffer mutation
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", baud=115200, max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None

    buf = sess.buffer
    buf.feed(b"first\n")  # commits "first" at seq 1
    buf.feed(b"second")  # partial "second"; _next_seq == 2

    v = console_ops.run("views", id=sid, mode="tail", n=10, settle_ms=0)
    assert v.is_ok(), v.fields
    assert v.body and "first" in v.body and "second" in v.body  # partial shown
    latest = v.fields["latest_seq"]
    assert latest == 1
    # The cursor must be the last COMMITTED seq (1), not the synth's seq (2).
    assert v.fields["to_seq"] == latest

    # The partial commits AT the synth's seq (2). since=<to_seq> (=1) must
    # return it. Before the fix, to_seq was 2 and since=2 skipped seq 2.
    buf.feed(b"\n")  # commits "second" at seq 2
    v2 = console_ops.run("views", id=sid, mode="since", since=latest, settle_ms=0)
    assert v2.is_ok(), v2.fields
    assert v2.body and "second" in v2.body

    console_ops.run("close", id=sid)


def test_view_tail_with_pump_appending_on_read_not_hidden(monkeypatch) -> None:
    """GENUINE race test: a fake that appends lines on read (the pump feeds the
    buffer from read()) concurrent with the views_console call must not hide
    the committed line from the next ``since=<to_seq>``.

    The pump's ``read()`` is gated by a ``threading.Event`` released inside an
    instrumented ``snapshot_meta`` AFTER it reads ``_lines`` (so the pump's
    feed lands AFTER ``snapshot_meta`` observes ``latest_seq`` - the "Case A"
    window the cursor fix addresses). The cursor (``to_seq``) stays on the
    last committed seq (``latest_seq=1``), and ``since=<to_seq>`` returns the
    concurrently committed "real" line (seq=2).

    The fake returns ``b""`` (no data) until the gate is set, so the
    ``_brief_pump`` call inside ``open_console`` and the background pump's
    pre-gate polls do not consume any "real" lines or block on a wait.

    The Case B window (feed lands BETWEEN ``view_tail`` and the cursor
    computation) is now closed by anchoring the cursor to ``latest_at_view``
    captured under ``view_tail``'s lock - see
    ``test_view_tail_case_b_feed_between_view_and_meta``. The earlier note
    that ``to_seq = min(last_seq, latest)`` would fix Case B was WRONG:
    ``min(2, 2) == 2`` still skips seq=2. The real fix captures the latest
    committed seq AT VIEW TIME under the same lock as the lines snapshot
    (``view_tail_with_meta`` / ``view_since_with_meta``) so a later
    ``snapshot_meta()`` cannot observe a newer feed and push the cursor
    past a committed line.
    """
    reset_serial_registry()

    # Gate: when set, the fake starts returning "real\n" from read(). Before
    # set, read() returns b"" so _brief_pump (in open_console) and the pump's
    # pre-gate polls consume nothing.
    gate = threading.Event()
    read_done = threading.Event()

    class _GatedAppendOnReadFakeSer:
        """Returns `real\\n` from read() once the gate is set, else b""."""

        def __init__(self) -> None:
            self.is_open = True
            self._lock = threading.Lock()
            self._remaining = 3
            self.written = bytearray()

        @property
        def in_waiting(self) -> int:
            if not gate.is_set():
                return 0
            with self._lock:
                return 1 if self._remaining > 0 else 0

        def read(self, n: int) -> bytes:
            if not gate.is_set():
                return b""
            with self._lock:
                if self._remaining <= 0:
                    return b""
                self._remaining -= 1
                read_done.set()
                return b"real\n"

        def write(self, data: bytes) -> int:
            with self._lock:
                self.written.extend(data)
            return len(data)

        def close(self) -> None:
            self.is_open = False

    fake = _GatedAppendOnReadFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None
    buf = sess.buffer

    # Pre-load a committed line + a partial so view_tail constructs a synth.
    buf.feed(b"first\n")  # seq 1
    buf.feed(b"partial")  # partial; _next_seq == 2

    # Instrument snapshot_meta to release the gate AFTER it reads _lines
    # (Case A). The pump's next read() returns "real\n", the pump's feed
    # acquires the lock after snapshot_meta releases it, so the feed lands
    # after snapshot_meta observed latest_seq=1 - the cursor drops to 1.
    orig_snapshot_meta = buf.snapshot_meta

    def _gated_snapshot_meta() -> dict[str, int]:
        result = orig_snapshot_meta()
        gate.set()
        return result

    buf.snapshot_meta = _gated_snapshot_meta  # type: ignore[method-assign]

    v = console_ops.run("views", id=sid, mode="tail", n=20, settle_ms=0)
    assert v.is_ok(), v.fields
    assert v.body and "partial" in v.body  # synth shown
    # snapshot_meta read _lines BEFORE the gate was set (the pump's feed had
    # not landed yet), so latest_seq is still 1 (first.seq).
    assert v.fields["latest_seq"] == 1, v.fields
    # Cursor drops to latest (1), not the synth's seq (2).
    assert v.fields["to_seq"] == 1, v.fields

    # Wait for the pump to have pushed at least one real line.
    assert read_done.wait(timeout=5.0)
    # Drain remaining pump pushes so the buffer is settled before since=.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if fake._remaining == 0:
            break
        time.sleep(0.01)

    v2 = console_ops.run("views", id=sid, mode="since", since=1, settle_ms=0)
    assert v2.is_ok(), v2.fields
    assert v2.body and "real" in v2.body, (
        f"real line hidden: view_tail={v.body!r}, since={v2.body!r}, "
        f"to_seq={v.fields['to_seq']}, latest={v.fields['latest_seq']}"
    )

    console_ops.run("close", id=sid)


class _AlwaysRaisesReadFakeSer:
    """read() always raises - simulates a dead/broken link."""

    def __init__(self) -> None:
        self.is_open = True
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return 0

    def read(self, n: int) -> bytes:
        raise OSError("link gone")

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False


def test_pump_persistent_error_surfaced(monkeypatch) -> None:
    """A persistent read failure surfaces pump_error and (after the pump
    thread exits) link_closed; running reflects reality instead of masking
    a dead link as healthy."""
    reset_serial_registry()
    fake = _AlwaysRaisesReadFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    # The first read error lands within the 50ms settle; pump_error is set.
    assert r.fields.get("pump_error")
    sid = str(r.fields["id"])

    # Poll until the consecutive-error threshold breaks the pump thread.
    deadline = time.time() + 3.0
    link_closed = False
    while time.time() < deadline:
        v = console_ops.run("views", id=sid, mode="tail", n=10, settle_ms=0)
        assert v.is_ok(), v.fields
        assert v.fields.get("pump_error")  # error stays surfaced
        if v.fields.get("link_closed") == 1:
            link_closed = True
            assert v.fields.get("pump_running") == 0  # thread exited
            break
        time.sleep(0.05)
    assert link_closed, "pump did not surface link_closed after persistent errors"

    s = console_ops.run("sessions")
    assert s.is_ok(), s.fields
    assert s.body and "error=" in s.body and "link_closed=1" in s.body

    console_ops.run("close", id=sid)


def test_pump_error_clears_after_successful_reads(monkeypatch) -> None:
    """A transient read glitch must not leave sticky pump_error forever.

    After the link recovers and the pump completes successful reads, views
    fields and pump.stats["error"] must drop the prior error so the agent is
    not told to reopen a healthy session. Persistent failure still stops the
    pump and sets link_closed (covered by test_pump_persistent_error_surfaced).
    """
    reset_serial_registry()

    class _ControllableFakeSer:
        """Toggle ``fail`` to inject a glitch; otherwise behave like a quiet link."""

        def __init__(self) -> None:
            self.is_open = True
            self.written = bytearray()
            self._lock = threading.Lock()
            self._rx = bytearray()
            self.fail = False

        @property
        def in_waiting(self) -> int:
            with self._lock:
                return len(self._rx)

        def inject(self, data: bytes) -> None:
            with self._lock:
                self._rx.extend(data)

        def read(self, n: int) -> bytes:
            with self._lock:
                if self.fail:
                    raise OSError("transient glitch")
                chunk = bytes(self._rx[:n])
                del self._rx[:n]
                return chunk

        def write(self, data: bytes) -> int:
            with self._lock:
                self.written.extend(data)
                return len(data)

        def close(self) -> None:
            self.is_open = False

    fake = _ControllableFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None and sess.pump is not None

    # Inject a glitch while the pump is running; wait until it is recorded.
    fake.fail = True
    saw_error = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if sess.pump.stats.get("error"):
            saw_error = True
            break
        time.sleep(0.01)
    assert saw_error, f"expected pump error during glitch, stats={sess.pump.stats!r}"
    v_err = console_ops.run("views", id=sid, mode="tail", n=5, settle_ms=0)
    assert v_err.is_ok(), v_err.fields
    assert v_err.fields.get("pump_error"), v_err.fields

    # Recover: stop failing and deliver a successful read (with payload).
    fake.fail = False
    fake.inject(b"recovered-line\n")

    cleared = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        v = console_ops.run("views", id=sid, mode="tail", n=20, settle_ms=0)
        assert v.is_ok(), v.fields
        if (
            "pump_error" not in v.fields
            and sess.pump.stats.get("error") is None
            and v.fields.get("link_closed") != 1
        ):
            cleared = True
            break
        time.sleep(0.02)
    assert cleared, (
        f"pump_error stuck after recovery: "
        f"views={console_ops.run('views', id=sid, mode='tail', n=5, settle_ms=0).fields!r} "
        f"stats={sess.pump.stats!r}"
    )
    assert sess.pump.stats.get("running") is True
    assert sess.pump.stats.get("link_closed") is False
    console_ops.run("close", id=sid)


def test_send_dead_link_console_closed(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    fake.is_open = False  # link is dead
    s = console_ops.run("send", id=sid, data="x")
    assert s.status == "error"
    assert s.code == "CONSOLE_CLOSED"
    assert s.fields.get("bytes") == 0
    console_ops.run("close", id=sid)


def test_send_write_failed(monkeypatch) -> None:
    reset_serial_registry()

    class _WriteFailsFakeSer:
        def __init__(self) -> None:
            self.is_open = True
            self.written = bytearray()

        @property
        def in_waiting(self) -> int:
            return 0

        def read(self, n: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return 0  # write reports 0 bytes (failure)

        def close(self) -> None:
            self.is_open = False

    fake = _WriteFailsFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    s = console_ops.run("send", id=sid, data="hello")
    assert s.status == "error"
    assert s.code == "CONSOLE_WRITE_FAILED"
    assert s.fields.get("bytes") == 0
    assert s.fields.get("expected") == 5
    console_ops.run("close", id=sid)


def test_send_partial_write(monkeypatch) -> None:
    reset_serial_registry()

    class _PartialWriteFakeSer:
        def __init__(self) -> None:
            self.is_open = True
            self.written = bytearray()

        @property
        def in_waiting(self) -> int:
            return 0

        def read(self, n: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            self.written.extend(data[:2])  # only 2 bytes go through
            return 2

        def close(self) -> None:
            self.is_open = False

    fake = _PartialWriteFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    s = console_ops.run("send", id=sid, data="hello")  # 5 bytes
    assert s.is_ok(), s.fields  # partial success
    assert s.code == "PARTIAL_WRITE"
    assert s.fields.get("bytes") == 2
    assert s.fields.get("expected") == 5
    assert "warning" in s.fields
    console_ops.run("close", id=sid)


def test_open_rejects_non_serial_device(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)

    for bad in ("/dev/tty", "/dev/pts/3", "/dev/null", "ttyS0", "/dev/ttyUSB"):
        r = console_ops.run("open", path=bad)
        assert r.status == "error", bad
        assert r.code == "INVALID_ARG", bad

    # Legitimate serial device names are accepted (fake prevents a real open).
    # Covers Linux USB-serial, macOS cu.*, macOS Bluetooth, Windows COM,
    # and the legacy FreeBSD/Solaris callout device. The broadened
    # /dev/tty[A-Z]+[0-9]+ alternation also accepts real USB-serial chip
    # families returned by pyserial comports that the old explicit
    # (S|USB|ACM|AMA|AP) list rejected: Exar XRUSB, National Instruments
    # USBTR, Maxlinear MAX, Hilscher HS, gadget GS, UL.
    for good in (
        "/dev/ttyUSB0",
        "/dev/ttyS0",
        "/dev/ttyACM0",
        "/dev/ttyAMA0",
        "/dev/ttyAP0",
        "/dev/ttyXRUSB0",
        "/dev/ttyUSBTR0",
        "/dev/ttyMAX0",
        "/dev/ttyHS0",
        "/dev/ttyGS0",
        "/dev/ttyUL0",
        "/dev/cu.USBSERIAL",
        "/dev/rfcomm0",
        "COM9",
        "com3",  # case-insensitive Windows COM
        "\\\\.\\COM10",  # Windows extended device path
        "/dev/cua0",
    ):
        r = console_ops.run("open", path=good, max_lines=1000)
        assert r.is_ok(), (good, r.fields)
        console_ops.run("close", id=r.fields["id"])


def test_open_com_case_insensitive_and_win_device_prefix(monkeypatch) -> None:
    """com3 and COM3 are the same port; \\\\.\\COM10 normalizes then opens as COM10."""
    reset_serial_registry()
    opened_ports: list[str] = []

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        opened_ports.append(port)
        # Fresh fake per open so close() does not leave is_open=False.
        return SerialConsole(
            port, baudrate=baudrate, serial_factory=lambda: _ThreadSafeFakeSer()
        )

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    r_lo = console_ops.run("open", path="com3", max_lines=1000)
    assert r_lo.is_ok(), r_lo.fields
    assert r_lo.fields.get("path") == "COM3"
    console_ops.run("close", id=r_lo.fields["id"])

    r_hi = console_ops.run("open", path="COM3", max_lines=1000)
    assert r_hi.is_ok(), r_hi.fields
    assert r_hi.fields.get("path") == "COM3"
    console_ops.run("close", id=r_hi.fields["id"])

    r_ext = console_ops.run("open", path="\\\\.\\COM10", max_lines=1000)
    assert r_ext.is_ok(), r_ext.fields
    assert r_ext.fields.get("path") == "COM10"
    console_ops.run("close", id=r_ext.fields["id"])

    assert opened_ports == ["COM3", "COM3", "COM10"]

    # Name-shape helpers: direct unit surface for the allowlist path.
    assert console_ops._is_serial_port_name("com1")
    assert console_ops._is_serial_port_name("COM1")
    assert console_ops._is_serial_port_name("\\\\.\\COM10")
    assert console_ops._normalize_serial_port_name("com3") == "COM3"
    assert console_ops._normalize_serial_port_name("\\\\.\\COM10") == "COM10"
    # Non-COM paths unchanged (Linux/macOS rules preserved).
    assert console_ops._normalize_serial_port_name("/dev/ttyUSB0") == "/dev/ttyUSB0"
    assert console_ops._is_serial_port_name("/dev/ttyUSB0")
    assert not console_ops._is_serial_port_name("/dev/tty")


def test_console_hints_use_op_not_legacy_tool_names(monkeypatch) -> None:
    """Error/success hints advertise console op=... not console_open/list."""
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)

    legacy = (
        "console_open",
        "console_list",
        "console_send",
        "console_views",
        "console_close",
    )

    r_miss = console_ops.run("open", path="")
    assert r_miss.status == "error"
    assert r_miss.code == "MISSING_ARG"
    for name in legacy:
        assert name not in (r_miss.hint or ""), r_miss.hint
    assert "console op=" in (r_miss.hint or "")

    r_bad = console_ops.run("open", path="/dev/null")
    assert r_bad.status == "error"
    assert r_bad.code == "INVALID_ARG"
    for name in legacy:
        assert name not in (r_bad.hint or ""), r_bad.hint
    assert "console op=" in (r_bad.hint or "")

    r_list = console_ops.run("list")
    # list may fail without pyserial; still check hint when present
    if r_list.hint:
        for name in legacy:
            assert name not in r_list.hint, r_list.hint

    r_bad_op = console_ops.run("not_an_op")
    assert r_bad_op.status == "error"
    for name in legacy:
        assert name not in (r_bad_op.hint or ""), r_bad_op.hint
    assert "console op=" in (r_bad_op.hint or "")

    r_ok = console_ops.run("open", path="COM9", max_lines=1000)
    assert r_ok.is_ok(), r_ok.fields
    for name in legacy:
        assert name not in (r_ok.hint or ""), r_ok.hint
    assert "console op=" in (r_ok.hint or "")
    console_ops.run("close", id=r_ok.fields["id"])


def test_send_data_b64_newline_applied(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    # newline=True must append \n to a data_b64 payload (was silently ignored).
    s = console_ops.run(
        "send", id=sid, data_b64=base64.b64encode(b"hello").decode(), newline=True
    )
    assert s.is_ok(), s.fields
    assert s.fields.get("bytes") == 6
    assert fake.written.endswith(b"\n")
    assert fake.written == b"hello\n"

    # newline=False does not append.
    fake.written.clear()
    s2 = console_ops.run(
        "send", id=sid, data_b64=base64.b64encode(b"hello").decode(), newline=False
    )
    assert s2.is_ok(), s2.fields
    assert s2.fields.get("bytes") == 5
    assert fake.written == b"hello"
    console_ops.run("close", id=sid)


def test_send_data_b64_invalid_rejected(monkeypatch) -> None:
    """Illegal/truncated data_b64 must error (INVALID_ARG), not silent ok.

    Without validate=True, stdlib b64decode ignores non-alphabet chars so
    '@@@' becomes b'' and the empty-payload branch reports status=ok.
    """
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    for bad in ("@@@", "!!!", "YQ", "abc", "===="):
        s = console_ops.run("send", id=sid, data_b64=bad)
        assert s.status == "error", (bad, s.fields)
        assert s.code == "INVALID_ARG", (bad, s.code, s.fields)
        assert "data_b64" in (s.fields.get("msg") or ""), s.fields
        # Agent-facing hint uses console op=send syntax (not legacy tool names).
        assert "console op=send" in (s.hint or ""), s.hint
        assert len(fake.written) == 0

    # Valid payload still succeeds after the reject path.
    good = console_ops.run(
        "send", id=sid, data_b64=base64.b64encode(b"ok").decode()
    )
    assert good.is_ok(), good.fields
    assert good.fields.get("bytes") == 2
    assert fake.written == b"ok"
    console_ops.run("close", id=sid)


def test_send_empty_data_no_newline_ok_bytes_zero(monkeypatch) -> None:
    """send data="" newline=False -> ok, bytes=0, no code (no-op, not a failure).

    The empty-payload branch returns a plain ok (code=None) with bytes=0 and
    does NOT touch the link - an explicit no-op so the agent can probe liveness
    without writing. Pins this contract so a future refactor does not turn the
    empty case into MISSING_ARG or CONSOLE_CLOSED.
    """
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    s = console_ops.run("send", id=sid, data="", newline=False)
    assert s.is_ok(), s.fields
    assert s.code is None  # plain ok, no code
    assert s.fields.get("bytes") == 0
    assert s.fields.get("expected") == 0
    # Nothing was written to the fake link.
    assert len(fake.written) == 0
    console_ops.run("close", id=sid)


def test_open_baud_non_int_invalid_arg(monkeypatch) -> None:
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", baud="fast")
    assert r.status == "error"
    assert r.code == "INVALID_ARG"  # not a raw ValueError
    assert "baud" in (r.hint or "")

    r = console_ops.run("open", path="COM9", max_lines="lots")
    assert r.status == "error"
    assert r.code == "INVALID_ARG"

    # Numeric coercion in views is also guarded.
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    v = console_ops.run("views", id=sid, mode="since", since="abc")
    assert v.status == "error"
    assert v.code == "INVALID_ARG"
    v = console_ops.run("views", id=sid, mode="tail", n="many")
    assert v.status == "error"
    assert v.code == "INVALID_ARG"
    console_ops.run("close", id=sid)


def test_close_surfaces_close_error(monkeypatch) -> None:
    """When console.close() raises, close still reports closed=True but
    surfaces a close_error (SerialSession.close_errors contract) so the
    agent knows the port may not have released."""
    reset_serial_registry()

    class _CloseRaisesFakeSer(_ThreadSafeFakeSer):
        def close(self) -> None:
            raise OSError("busy")

    fake = _CloseRaisesFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    c = console_ops.run("close", id=sid)
    assert c.is_ok(), c.fields  # registry entry removed -> op succeeded
    assert c.fields.get("closed") is True
    assert "close_error" in c.fields
    assert "console.close" in c.fields["close_error"]
    assert c.code == "CONSOLE_CLOSE_PARTIAL"
    assert get_serial_registry().get(sid) is None


# ---------------------------------------------------------------------------
# Case B incremental-follow, read-lock serialization,
# write exception propagation, regex chip-family acceptance.
# ---------------------------------------------------------------------------


def test_view_tail_case_b_feed_between_view_and_meta(monkeypatch) -> None:
    """Case B (tail): a feed lands BETWEEN view_tail releasing its lock and
    the cursor computation. The cursor (to_seq) and the latest_seq field must
    use latest_at_view (captured under view_tail's lock, BEFORE the feed
    lands), NOT a separate snapshot_meta() that observes the later feed.

    Deterministic: the feed is injected synchronously inside a
    view_tail_with_meta wrapper right after the original returns (after the
    lock release), simulating the pump's feed landing in the Case B window.
    The background pump's fake returns b"" so it never feeds - the only feed
    is the injected one.

    Before the fix, the cursor used snapshot_meta().latest_seq (which
    observed the feed -> latest_seq=2); last_seq==latest (2), the drop did
    NOT trigger, to_seq=2, and since=2 (strict >) skipped seq=2 -> the
    committed "real" line was hidden. With latest_at_view=1 (at view time),
    to_seq=1 and since=1 returns "real".
    """
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()  # rx empty -> pump idles, never feeds
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None
    buf = sess.buffer

    buf.feed(b"first\n")  # seq 1
    buf.feed(b"partial")  # partial; _next_seq == 2

    orig_view_tail_with_meta = buf.view_tail_with_meta
    armed = True  # one-shot: inject the Case B feed only on the first call

    def _case_b_view_tail_with_meta(n, *, include_partial=True):  # type: ignore[no-untyped-def]
        nonlocal armed
        lines, latest_at_view = orig_view_tail_with_meta(
            n, include_partial=include_partial
        )
        if armed:
            armed = False
            # Feed lands AFTER view_tail released its lock (latest_at_view
            # already captured under the lock) but BEFORE snapshot_meta
            # acquires it - the Case B window. The real line commits at
            # _next_seq (== the synth's seq 2).
            buf.feed(b"real\n")
        return lines, latest_at_view

    buf.view_tail_with_meta = _case_b_view_tail_with_meta  # type: ignore[method-assign]

    v = console_ops.run("views", id=sid, mode="tail", n=20, settle_ms=0)
    assert v.is_ok(), v.fields
    assert v.body and "partial" in v.body  # synth still shown for reading
    # latest_at_view was captured under view_tail's lock BEFORE the feed
    # landed -> latest_seq field reflects the at-view value (1), not the
    # post-feed value (2) that snapshot_meta would observe.
    assert v.fields["latest_seq"] == 1, v.fields
    # Cursor drops to latest_at_view (1), not the synth's seq (2).
    assert v.fields["to_seq"] == 1, v.fields

    v2 = console_ops.run("views", id=sid, mode="since", since=1, settle_ms=0)
    assert v2.is_ok(), v2.fields
    assert v2.body and "real" in v2.body, (
        f"real line hidden: view_tail={v.body!r}, since={v2.body!r}, "
        f"to_seq={v.fields['to_seq']}, latest={v.fields['latest_seq']}"
    )

    console_ops.run("close", id=sid)


def test_view_since_case_b_feed_between_view_and_meta(monkeypatch) -> None:
    """Case B (since mode): same gap as tail mode - a feed lands between
    view_since releasing its lock and the cursor computation. latest_at_view
    captured under view_since's lock anchors the cursor (to_seq=1) so
    since=1 returns the line that committed at seq=2. Both tail and since
    modes share the gap; both must be fixed.
    """
    reset_serial_registry()
    fake = _ThreadSafeFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None
    buf = sess.buffer

    buf.feed(b"first\n")  # seq 1
    buf.feed(b"partial")  # partial; _next_seq == 2

    orig_view_since_with_meta = buf.view_since_with_meta
    armed = True

    def _case_b_view_since_with_meta(since_seq, *, include_partial=True):  # type: ignore[no-untyped-def]
        nonlocal armed
        lines, latest_at_view = orig_view_since_with_meta(
            since_seq, include_partial=include_partial
        )
        if armed:
            armed = False
            # Case B: feed lands after view_since's lock release, before the
            # cursor computation.
            buf.feed(b"real\n")
        return lines, latest_at_view

    buf.view_since_with_meta = _case_b_view_since_with_meta  # type: ignore[method-assign]

    v = console_ops.run("views", id=sid, mode="since", since=0, settle_ms=0)
    assert v.is_ok(), v.fields
    assert v.body and "partial" in v.body  # synth shown
    assert v.fields["latest_seq"] == 1, v.fields  # at-view, not post-feed
    assert v.fields["to_seq"] == 1, v.fields  # drop to latest_at_view

    v2 = console_ops.run("views", id=sid, mode="since", since=1, settle_ms=0)
    assert v2.is_ok(), v2.fields
    assert v2.body and "real" in v2.body, (
        f"real line hidden (since): v1={v.body!r}, v2={v2.body!r}, "
        f"to_seq={v.fields['to_seq']}, latest={v.fields['latest_seq']}"
    )

    console_ops.run("close", id=sid)


class _ConcurrencyTrackingFakeSer:
    """Fake serial whose read() tracks concurrent readers.

    ``SerialConsole.read()`` holds a per-console ``_read_lock`` around the
    underlying ``self._ser.read()``; this fake's ``read()`` sleeps to widen
    the overlap window and records the max concurrent-reader count. With the
    read lock, the CapturePump and ``_brief_pump`` (which both go through
    ``SerialConsole.read()``) serialize -> max_concurrent == 1. Without the
    lock, the two readers' 20ms read windows overlap -> max_concurrent >= 2.
    """

    def __init__(self) -> None:
        self.is_open = True
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return 0  # SerialConsole.read falls through to a full read

    def read(self, n: int) -> bytes:
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(0.02)  # widen the race window so overlap is detectable
            return b""
        finally:
            with self._lock:
                self._active -= 1

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False


def test_brief_pump_and_capture_pump_reads_serialized(monkeypatch) -> None:
    """Two readers (background CapturePump + sync _brief_pump) on the same
    SerialConsole never read the underlying serial concurrently - the
    per-console read lock (inside SerialConsole.read()) serializes them.
    Without the lock, a byte burst during the overlap would split bytes
    between the two readers and feed the locked buffer mis-ordered.

    Deterministic: the fake's read() records the concurrent-reader count and
    sleeps 20ms to widen the overlap window; assert max_concurrent == 1
    after repeated _brief_pump calls while the pump is running.
    """
    reset_serial_registry()
    fake = _ConcurrencyTrackingFakeSer()
    _patch_console_open(monkeypatch, fake)

    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None
    assert sess.pump is not None and sess.pump.stats.get("running") is True

    # Repeatedly call _brief_pump from the main thread while the background
    # pump is reading. The fake's read() sleeps 20ms to widen the overlap
    # window; without the read lock, max_concurrent would exceed 1.
    deadline = time.time() + 0.6
    while time.time() < deadline:
        console_ops._brief_pump(sess)
        time.sleep(0.002)

    assert fake.max_concurrent == 1, (
        f"reads overlapped without the per-console read lock: "
        f"max_concurrent={fake.max_concurrent}"
    )
    console_ops.run("close", id=sid)


def test_send_write_exception_partial_recovered(monkeypatch) -> None:
    """write() propagates an exception that carries a recoverable partial
    count (``.written``) -> send_console reports PARTIAL_WRITE with the
    partial bytes/expected (NOT a generic bytes=0 failure) so the agent
    resends only the tail instead of the full payload.
    """
    reset_serial_registry()

    class _PartialWriteThenRaiseFakeSer:
        def __init__(self) -> None:
            self.is_open = True
            self.written = bytearray()

        @property
        def in_waiting(self) -> int:
            return 0

        def read(self, n: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            self.written.extend(data[:2])  # 2 bytes go through, then timeout
            # Partial-write count is a dynamic attr consumed by console_ops.
            exc = OSError("write timeout")
            setattr(exc, "written", 2)
            raise exc

        def close(self) -> None:
            self.is_open = False

    fake = _PartialWriteThenRaiseFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    s = console_ops.run("send", id=sid, data="hello")  # 5 bytes
    assert s.is_ok(), s.fields  # partial success (bytes went through)
    assert s.code == "PARTIAL_WRITE"
    assert s.fields.get("bytes") == 2
    assert s.fields.get("expected") == 5
    assert "warning" in s.fields
    assert "OSError" in s.fields["warning"]  # real error type surfaced
    assert "write timeout" in s.fields["warning"]  # real error message
    # The partial bytes are on the link; the agent resends only the tail.
    assert bytes(fake.written) == b"he"
    console_ops.run("close", id=sid)


def test_send_write_exception_no_partial(monkeypatch) -> None:
    """write() propagates an exception with no recoverable partial count ->
    send_console reports CONSOLE_WRITE_FAILED with the ACTUAL error
    type/message in msg (not the generic "write returned 0 bytes"), so the
    agent can diagnose instead of blindly retrying the full payload."""
    reset_serial_registry()

    class _WriteRaisesFakeSer:
        def __init__(self) -> None:
            self.is_open = True
            self.written = bytearray()

        @property
        def in_waiting(self) -> int:
            return 0

        def read(self, n: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            raise OSError("link gone")

        def close(self) -> None:
            self.is_open = False

    fake = _WriteRaisesFakeSer()
    _patch_console_open(monkeypatch, fake)
    r = console_ops.run("open", path="COM9", max_lines=1000)
    assert r.is_ok(), r.fields
    sid = str(r.fields["id"])

    s = console_ops.run("send", id=sid, data="hello")
    assert s.status == "error"
    assert s.code == "CONSOLE_WRITE_FAILED"
    assert s.fields.get("bytes") == 0
    assert s.fields.get("expected") == 5
    # The actual error is surfaced (not "write returned 0 bytes").
    msg = s.fields.get("msg", "")
    assert "OSError" in msg
    assert "link gone" in msg
    console_ops.run("close", id=sid)


# ---------------------------------------------------------------------------
# Per-device exclusive open
# ---------------------------------------------------------------------------


def test_open_same_device_twice_rejected_with_existing_id(monkeypatch) -> None:
    """Second open of the same path fails with CONSOLE_IN_USE + existing id."""
    reset_serial_registry()
    opened: list[str] = []

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        opened.append(port)
        # Fresh fake per successful construction; second open may still build
        # a handle if it races past get_by_path (add then rejects).
        return SerialConsole(
            port, baudrate=baudrate, serial_factory=lambda: _ThreadSafeFakeSer()
        )

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    r1 = console_ops.run("open", path="/dev/ttyUSB0", baud=115200, max_lines=1000)
    assert r1.is_ok(), r1.fields
    sid = str(r1.fields["id"])
    assert sid.startswith("con_")

    r2 = console_ops.run("open", path="/dev/ttyUSB0", baud=115200, max_lines=1000)
    assert r2.status == "error"
    assert r2.code == "CONSOLE_IN_USE"
    assert r2.fields.get("id") == sid
    assert r2.fields.get("path") == "/dev/ttyUSB0"
    msg = str(r2.fields.get("msg", ""))
    assert sid in msg
    assert "already open" in msg.lower() or "already" in msg.lower()

    # Only one session lives; the existing id still works.
    assert get_serial_registry().get(sid) is not None
    assert len(get_serial_registry().list_open()) == 1

    # COM alias of a different path does not free ttyUSB0; close frees it.
    c = console_ops.run("close", id=sid)
    assert c.is_ok()
    r3 = console_ops.run("open", path="/dev/ttyUSB0", max_lines=1000)
    assert r3.is_ok(), r3.fields
    console_ops.run("close", id=r3.fields["id"])


def test_open_same_device_normalized_aliases_collide(monkeypatch) -> None:
    """com3 / COM3 / \\\\.\\COM3 normalize to one occupancy key."""
    reset_serial_registry()

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(
            port, baudrate=baudrate, serial_factory=lambda: _ThreadSafeFakeSer()
        )

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    r1 = console_ops.run("open", path="com3", max_lines=500)
    assert r1.is_ok(), r1.fields
    sid = str(r1.fields["id"])
    assert r1.fields.get("path") == "COM3"

    r2 = console_ops.run("open", path="COM3", max_lines=500)
    assert r2.status == "error" and r2.code == "CONSOLE_IN_USE"
    assert r2.fields.get("id") == sid

    r3 = console_ops.run("open", path="\\\\.\\COM3", max_lines=500)
    assert r3.status == "error" and r3.code == "CONSOLE_IN_USE"
    assert r3.fields.get("id") == sid

    console_ops.run("close", id=sid)


def test_open_two_distinct_devices_succeeds(monkeypatch) -> None:
    """Different devices may be open concurrently (each gets its own id)."""
    reset_serial_registry()

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        return SerialConsole(
            port, baudrate=baudrate, serial_factory=lambda: _ThreadSafeFakeSer()
        )

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    a = console_ops.run("open", path="/dev/ttyUSB0", max_lines=500)
    b = console_ops.run("open", path="/dev/ttyUSB1", max_lines=500)
    assert a.is_ok(), a.fields
    assert b.is_ok(), b.fields
    id_a, id_b = str(a.fields["id"]), str(b.fields["id"])
    assert id_a != id_b
    open_ids = {s.id for s in get_serial_registry().list_open()}
    assert open_ids == {id_a, id_b}

    console_ops.run("close", id=id_a)
    console_ops.run("close", id=id_b)
    assert get_serial_registry().list_open() == []


def test_open_same_device_concurrent_only_one_wins(monkeypatch) -> None:
    """N concurrent opens of one path: exactly one ok; losers CONSOLE_IN_USE."""
    reset_serial_registry()

    def _open(port: str, baudrate: int = 115200, **kw):  # type: ignore[no-untyped-def]
        # Tiny delay so threads can overlap get_by_path / SerialConsole.
        time.sleep(0.01)
        return SerialConsole(
            port, baudrate=baudrate, serial_factory=lambda: _ThreadSafeFakeSer()
        )

    monkeypatch.setattr(console_ops, "SerialConsole", _open)

    results: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=5.0)
        r = console_ops.run("open", path="/dev/ttyACM0", max_lines=200)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()

    oks = [r for r in results if getattr(r, "is_ok", lambda: False)()]
    errs = [r for r in results if not getattr(r, "is_ok", lambda: True)()]
    assert len(oks) == 1, f"expected exactly one winner, got {len(oks)}"
    assert len(errs) == 7
    winner_id = str(oks[0].fields["id"])  # type: ignore[attr-defined]
    for e in errs:
        assert e.code == "CONSOLE_IN_USE"  # type: ignore[attr-defined]
        assert e.fields.get("id") == winner_id  # type: ignore[attr-defined]
    assert len(get_serial_registry().list_open()) == 1
    console_ops.run("close", id=winner_id)


def test_console_ops_time_is_module_level() -> None:
    """console_ops uses top-level ``import time`` only (no lazy in-function)."""
    import ast
    import inspect

    import mcp_remote_control.core.console_ops as mod

    assert hasattr(mod, "time")
    assert mod.time is time

    tree = ast.parse(inspect.getsource(mod))
    lazy: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        if alias.name == "time" or alias.name.startswith("time."):
                            lazy.append(node.name)
                elif isinstance(child, ast.ImportFrom) and child.module == "time":
                    lazy.append(node.name)
    assert lazy == [], f"function-local time imports: {lazy}"


# ---------------------------------------------------------------------------
# Budgeted serial close: join, read-lock, and ser.close must fail visibly.
# ---------------------------------------------------------------------------


class _BlockingReadLink:
    """Link whose read() ignores stop until *release* is set."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def is_alive(self) -> bool:
        return True

    def read(self, max_bytes: int = 8192) -> bytes:
        self.entered.set()
        self.release.wait(timeout=30.0)
        return b""


class _BlockingReadFakeSer:
    """pyserial stand-in: read() holds until *release* (holds _read_lock)."""

    def __init__(self) -> None:
        self.is_open = True
        self.entered = threading.Event()
        self.release = threading.Event()
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return 1

    def read(self, n: int) -> bytes:
        self.entered.set()
        self.release.wait(timeout=30.0)
        return b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False


class _HangCloseFakeSer(_ThreadSafeFakeSer):
    """pyserial stand-in: close() blocks until *release*."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def close(self) -> None:
        self.entered.set()
        self.release.wait(timeout=30.0)
        self.is_open = False


def test_capture_pump_stop_join_timeout_raises() -> None:
    """A pump blocked in read() does not join; stop() raises TimeoutError."""
    buf = LineRingBuffer()
    link = _BlockingReadLink()
    pump = CapturePump(link, buf, name="stuck-pump")
    pump.start()
    assert link.entered.wait(timeout=2.0), "pump never entered read()"
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="join timed out"):
        pump.stop(timeout_s=0.15)
    assert time.monotonic() - t0 < 1.0
    assert pump.stats.get("error")
    assert "join" in str(pump.stats.get("error"))
    link.release.set()
    pump.stop(timeout_s=2.0)


def test_stuck_pump_join_timeout_recorded_on_close_errors(monkeypatch) -> None:
    """Pump that ignores stop -> close_errors has stop_capture; not a clean close.

    Built via the registry (not console open) so open's ``_brief_pump`` cannot
    serialize behind the same blocking read and hang the test.
    """
    import mcp_remote_control.serial.capture as capture_mod
    import mcp_remote_control.serial.handle as handle_mod

    monkeypatch.setattr(capture_mod, "_STOP_JOIN_TIMEOUT_S", 0.15)
    monkeypatch.setattr(handle_mod, "_CLOSE_TIMEOUT_S", 0.15)

    reset_serial_registry()
    fake = _BlockingReadFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    sid = "con_01"
    sess = SerialSession(
        id=sid,
        console=con,
        path="COM9",
        baud=115200,
        buffer=LineRingBuffer(),
    )
    get_serial_registry().add(sess)
    assert fake.entered.wait(timeout=2.0), "pump never entered blocking read"

    t0 = time.monotonic()
    c = console_ops.run("close", id=sid)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"stuck close hung for {elapsed:.2f}s"
    # Registry entry is gone, but teardown was not clean.
    assert get_serial_registry().get(sid) is None
    assert c.fields.get("closed") is True
    err = str(c.fields.get("close_error") or "")
    assert "stop_capture" in err or "join" in err, err
    assert "TimeoutError" in err, err
    assert c.code == "CONSOLE_CLOSE_PARTIAL"
    # Not a clean ok: close_error is set and the code is a close warning.
    assert c.fields.get("close_error")
    fake.release.set()


def test_console_close_read_lock_returns_inside_budget() -> None:
    """close() times out on _read_lock instead of waiting unbounded.

    The lock timeout still nulls ``_ser`` and invokes ``ser.close`` so the
    fd is not leaked for the next open.
    """
    fake = _ThreadSafeFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    holding = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        con._read_lock.acquire()
        holding.set()
        release.wait(timeout=30.0)
        con._read_lock.release()

    holder = threading.Thread(target=_hold, name="hold-read-lock", daemon=True)
    holder.start()
    assert holding.wait(timeout=2.0)

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="read lock"):
        con.close(timeout_s=0.15)
    assert time.monotonic() - t0 < 1.0
    assert con._ser is None
    assert fake.is_open is False
    release.set()
    holder.join(timeout=2.0)


def test_hung_ser_close_budgeted_and_visible_on_close_errors(monkeypatch) -> None:
    """Hung ser.close() returns inside the budget and lands on close_errors."""
    import mcp_remote_control.serial.handle as handle_mod

    monkeypatch.setattr(handle_mod, "_CLOSE_TIMEOUT_S", 0.15)

    fake = _HangCloseFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    buf = LineRingBuffer()

    class _QuietPump:
        def stop(self, *, timeout_s: float | None = None) -> None:
            return None

    sess = SerialSession(
        id="con_99",
        console=con,
        path="COM9",
        baud=115200,
        buffer=buf,
        pump=_QuietPump(),  # type: ignore[arg-type]
    )
    reg = SerialRegistry()
    reg.add(sess)

    t0 = time.monotonic()
    removed = reg.remove("con_99")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.5, f"hung ser.close pinned remove for {elapsed:.2f}s"
    assert removed is not None
    assert any(
        "console.close" in e and "TimeoutError" in e and "ser.close" in e
        for e in removed.close_errors
    ), removed.close_errors
    fake.release.set()


def test_serial_console_hung_close_raises_timeout() -> None:
    """Direct SerialConsole.close on a hung port raises inside the budget."""
    fake = _HangCloseFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="ser.close"):
        con.close(timeout_s=0.15)
    assert time.monotonic() - t0 < 1.0
    fake.release.set()


def test_close_lock_timeout_still_releases_fd() -> None:
    """Dummy reader holds _read_lock; close still nulls _ser and calls ser.close."""
    fake = _BlockingReadFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)

    def _reader() -> None:
        con.read(16)

    reader = threading.Thread(target=_reader, name="dummy-reader", daemon=True)
    reader.start()
    assert fake.entered.wait(timeout=2.0), "reader never entered ser.read"

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="read lock"):
        con.close(timeout_s=0.05)
    assert time.monotonic() - t0 < 1.0
    assert con._ser is None
    assert fake.is_open is False
    fake.release.set()
    reader.join(timeout=2.0)


def test_add_start_capture_failure_rolls_back_maps() -> None:
    """start_capture raise leaves _sessions / _by_path empty for that path."""
    reset_serial_registry()
    reg = SerialRegistry()
    fake = _ThreadSafeFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    sess = SerialSession(
        id="con_01",
        console=con,
        path="COM9",
        baud=115200,
        buffer=LineRingBuffer(),
    )

    def _boom() -> None:
        raise RuntimeError("pump start failed")

    sess.start_capture = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="pump start failed"):
        reg.add(sess)
    assert reg.get("con_01") is None
    assert reg.get_by_path("COM9") is None
    assert "con_01" not in reg._sessions
    assert "COM9" not in reg._by_path
    con.close()


class _TwoChunkFakeSer:
    """Yields ABC then DEF so two producers each get one wire chunk."""

    def __init__(self) -> None:
        self.is_open = True
        self._lock = threading.Lock()
        self._chunks = [b"ABC", b"DEF"]
        self.wire_order: list[bytes] = []

    @property
    def in_waiting(self) -> int:
        return 1

    def read(self, n: int) -> bytes:
        with self._lock:
            if not self._chunks:
                return b""
            chunk = self._chunks.pop(0)
            self.wire_order.append(chunk)
            return chunk

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.is_open = False


def test_pump_read_into_and_snarf_keep_wire_order() -> None:
    """Pump read_into and snarf/read+feed commit ABC+DEF in wire order.

    Never ADEFBC. DEFABC is allowed only if DEF left the wire first.
    """
    fake = _TwoChunkFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    buf = LineRingBuffer()
    con.bind_rx_buffer(buf)

    def _pump() -> None:
        con.read_into(buf, 8192)

    def _snarf() -> None:
        # Same shape as core.console_ops._brief_pump.
        more = con.read(65536)
        if more:
            buf.feed(more)

    t_pump = threading.Thread(target=_pump, name="pump-read-into", daemon=True)
    t_snarf = threading.Thread(target=_snarf, name="brief-snarf", daemon=True)
    t_pump.start()
    t_snarf.start()
    t_pump.join(timeout=2.0)
    t_snarf.join(timeout=2.0)

    buf.flush_partial()
    committed = buf.format_lines(buf.view_tail(10, include_partial=False))
    wire = b"".join(fake.wire_order).decode("ascii")
    assert committed in {"ABCDEF", "DEFABC"}, committed
    assert "ADEFBC" not in committed
    # With the shared order lock, commit order matches the true wire order.
    assert committed == wire
    con.close()


def test_read_into_holds_lock_across_feed() -> None:
    """A second producer cannot read while the first is still feeding."""
    fake = _TwoChunkFakeSer()
    con = SerialConsole("COM9", serial_factory=lambda: fake)
    feed_entered = threading.Event()
    feed_release = threading.Event()
    second_read_started = threading.Event()
    order: list[str] = []
    lock = threading.Lock()

    class _GatedBuf(LineRingBuffer):
        def feed(self, data: bytes | str) -> int:  # type: ignore[override]
            with lock:
                order.append("feed-start")
            feed_entered.set()
            feed_release.wait(timeout=2.0)
            with lock:
                order.append("feed-end")
            return super().feed(data)

    buf = _GatedBuf()

    def _first() -> None:
        con.read_into(buf, 8192)

    def _second() -> None:
        assert feed_entered.wait(timeout=2.0)
        second_read_started.set()
        con.snarf(buf, 8192)

    t1 = threading.Thread(target=_first, name="first-read-into", daemon=True)
    t2 = threading.Thread(target=_second, name="second-snarf", daemon=True)
    t1.start()
    assert feed_entered.wait(timeout=2.0)
    t2.start()
    # Give the second thread time to try; it must not enter ser.read yet.
    assert second_read_started.wait(timeout=2.0)
    time.sleep(0.05)
    assert fake.wire_order == [b"ABC"], fake.wire_order
    feed_release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    buf.flush_partial()
    committed = buf.format_lines(buf.view_tail(10, include_partial=False))
    assert committed == "ABCDEF"
    assert order[:2] == ["feed-start", "feed-end"]
    con.close()
