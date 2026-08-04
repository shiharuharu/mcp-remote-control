"""Persistent PowerShell sessions over WinRM/PSRP runspaces (not screen).

Test doubles live in ``mcp_remote_control.ps.mock`` and are not part of the
public package surface.
"""

from __future__ import annotations

from mcp_remote_control.ps.registry import (
    PsRegistry,
    get_ps_registry,
    reset_ps_registry,
)
from mcp_remote_control.ps.session import PsSession

__all__ = [
    "PsRegistry",
    "PsSession",
    "get_ps_registry",
    "reset_ps_registry",
]
