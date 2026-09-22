"""Transport backends for local, SSH, and WinRM execution.

Public surface re-exports the shared types (:class:`BaseTransport`,
:class:`ExecResult`, :class:`TransportError`) plus concrete backends.
Prefer importing from this package for callers outside ``transport/``.
"""

from __future__ import annotations

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError
from mcp_remote_control.transport.local import LocalTransport
from mcp_remote_control.transport.ssh import SSHTransport
from mcp_remote_control.transport.winrm import (
    WINRM_AUTH_PROTOCOLS,
    RunspaceResult,
    WinRMConnector,
    WinRMTransport,
    assemble_pypsrp_kwargs,
    parse_spn,
)

__all__ = [
    "WINRM_AUTH_PROTOCOLS",
    "BaseTransport",
    "ExecResult",
    "LocalTransport",
    "RunspaceResult",
    "SSHTransport",
    "TransportError",
    "WinRMConnector",
    "WinRMTransport",
    "assemble_pypsrp_kwargs",
    "parse_spn",
]
