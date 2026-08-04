"""Unit tests: serial console enumeration."""

from __future__ import annotations

from types import SimpleNamespace

from mcp_remote_control.core import console_ops
from mcp_remote_control.serial.ports import (
    SerialConsoleInfo,
    list_serial_consoles,
)


def test_list_serial_consoles_from_mock_lister() -> None:
    fake = [
        SimpleNamespace(
            device="COM5",
            name="COM5",
            description="USB Serial Device",
            hwid="USB VID:PID=1A86:7523 SER=0001",
            vid=0x1A86,
            pid=0x7523,
            serial_number="0001",
            manufacturer="QinHeng",
            product="USB Serial",
            location="1-1",
        ),
        SimpleNamespace(
            device="/dev/rfcomm0",
            name="rfcomm0",
            description="Bluetooth RFCOMM",
            hwid="Bluetooth",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            location=None,
        ),
    ]
    ports = list_serial_consoles(lister=lambda: fake)
    assert len(ports) == 2
    by_dev = {p.device: p for p in ports}
    assert by_dev["COM5"].link == "usb"
    assert by_dev["COM5"].vid == 0x1A86
    assert by_dev["/dev/rfcomm0"].link == "bluetooth"
    line = by_dev["COM5"].agent_line()
    assert "device=COM5" in line
    assert "vidpid=1a86:7523" in line


def test_agent_line_no_spaces() -> None:
    info = SerialConsoleInfo(
        device="/dev/ttyUSB0",
        name="ttyUSB0",
        description="FTDI USB UART",
        link="usb",
    )
    assert "device=/dev/ttyUSB0" in info.agent_line()


def test_console_ops_list(monkeypatch) -> None:
    fake = [
        SerialConsoleInfo(device="COM3", name="COM3", description="BT", link="bluetooth"),
    ]
    monkeypatch.setattr(
        "mcp_remote_control.core.console_ops.list_serial_consoles",
        lambda **kw: fake,
    )
    r = console_ops.run("list")
    assert r.is_ok()
    assert r.fields.get("n") == 1
    assert r.fields.get("devices") == "COM3"
    assert r.body and "device=COM3" in r.body
