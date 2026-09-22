"""Serial console package - unified embedded control link (USB/UART/BT ports)."""

from __future__ import annotations

from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.ports import (
    SerialConsoleInfo,
    list_serial_consoles,
)
from mcp_remote_control.serial.registry import (
    get_serial_registry,
    reset_serial_registry,
)

__all__ = [
    "CapturePump",
    "LineRingBuffer",
    "SerialConsole",
    "SerialConsoleInfo",
    "get_serial_registry",
    "list_serial_consoles",
    "reset_serial_registry",
]
