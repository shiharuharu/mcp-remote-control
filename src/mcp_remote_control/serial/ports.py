"""Enumerate host serial consoles without directory scanning.

Uses pyserial's platform backends (Windows SetupAPI, Linux sysfs, macOS IOKit)
so agents get stable device names and metadata instead of grepping ``/dev`` or
drivers by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SerialConsoleInfo:
    """One system-visible serial console (USB, UART, BT-SPP mapped COM, …)."""

    device: str
    """OS path / name to open (e.g. COM5, /dev/ttyUSB0, /dev/rfcomm0)."""

    name: str
    """Short human label (often same as device or product fragment)."""

    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    # Best-effort link class for agent meta (not a second API surface).
    link: str = "serial"
    """usb | uart | bluetooth | unknown — heuristic from hwid/description."""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop empty optional fields for compact JSON.
        return {k: v for k, v in d.items() if v is not None and v != ""}

    def agent_line(self) -> str:
        """One greppable line for agent body text."""
        parts = [f"device={self.device}"]
        if self.name and self.name != self.device:
            parts.append(f"name={_tok(self.name)}")
        if self.description:
            parts.append(f"desc={_tok(self.description)}")
        if self.vid is not None and self.pid is not None:
            parts.append(f"vidpid={self.vid:04x}:{self.pid:04x}")
        if self.serial_number:
            parts.append(f"sn={_tok(self.serial_number)}")
        if self.manufacturer:
            parts.append(f"mfr={_tok(self.manufacturer)}")
        if self.link and self.link != "serial":
            parts.append(f"link={self.link}")
        return " ".join(parts)


def _tok(text: str) -> str:
    """Space-free token for agent body lines."""
    return " ".join(str(text).split())[:80].replace(" ", "_")


def _guess_link(
    *,
    device: str,
    hwid: str,
    description: str,
) -> str:
    blob = f"{device} {hwid} {description}".lower()
    if "bluetooth" in blob or "bthenum" in blob or "rfcomm" in blob:
        return "bluetooth"
    if "usb" in blob or "vid:" in blob or "ttyusb" in blob or "ttyacm" in blob:
        return "usb"
    if device.startswith("COM") or "ttyS" in device or "ttyAMA" in device:
        # Bare UART / legacy COM without USB markers.
        if "usb" not in blob:
            return "uart"
    return "unknown"


# Optional override for the platform port enumerator (or prebuilt infos).
PortLister = Callable[[], Sequence[Any]]


def list_serial_consoles(
    *,
    include_links: bool = True,
    lister: PortLister | None = None,
) -> list[SerialConsoleInfo]:
    """Return serial consoles visible on **this host** (MCP controller machine).

    Does not scan filesystem paths by hand — delegates to pyserial
    ``list_ports``. Raises ``ImportError`` if pyserial is not installed.
    """
    if lister is not None:
        raw_ports = list(lister())
    else:
        try:
            from serial.tools import list_ports
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pyserial is required for serial console listing "
                "(pip/uv install pyserial)"
            ) from exc
        raw_ports = list(list_ports.comports())

    out: list[SerialConsoleInfo] = []
    for p in raw_ports:
        if isinstance(p, SerialConsoleInfo):
            out.append(p)
            continue
        device = str(getattr(p, "device", "") or "").strip()
        if not device:
            continue
        desc = str(getattr(p, "description", "") or "")
        hwid = str(getattr(p, "hwid", "") or "")
        name = str(getattr(p, "name", None) or device)
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        try:
            vid_i = int(vid) if vid is not None else None
        except (TypeError, ValueError):
            vid_i = None
        try:
            pid_i = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_i = None
        sn = getattr(p, "serial_number", None)
        mfr = getattr(p, "manufacturer", None)
        product = getattr(p, "product", None)
        loc = getattr(p, "location", None)
        link = (
            _guess_link(device=device, hwid=hwid, description=desc)
            if include_links
            else "serial"
        )
        out.append(
            SerialConsoleInfo(
                device=device,
                name=name,
                description=desc,
                hwid=hwid,
                vid=vid_i,
                pid=pid_i,
                serial_number=str(sn) if sn else None,
                manufacturer=str(mfr) if mfr else None,
                product=str(product) if product else None,
                location=str(loc) if loc else None,
                link=link,
            )
        )
    # Stable sort by device name for deterministic agent output.
    out.sort(key=lambda x: x.device.lower())
    return out
