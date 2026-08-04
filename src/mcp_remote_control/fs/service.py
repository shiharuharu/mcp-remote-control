"""FS backend factory: pick ``LocalFs`` / ``SftpFs`` / ``WinrmFs`` for an endpoint.

Agent-facing op dispatch lives in :mod:`mcp_remote_control.core.fs_ops`.
This module only builds the backend from a connected endpoint transport
(and optional injected clients for tests).
"""

from __future__ import annotations

from typing import Any

from mcp_remote_control.endpoint.registry import Endpoint
from mcp_remote_control.fs.backends.local import LocalFs
from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.fs.backends.winrm import WinrmFs
from mcp_remote_control.fs.types import FsBackend, FsError
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.ssh import SSHTransport
from mcp_remote_control.transport.winrm import WinRMTransport


def backend_for_endpoint(
    endpoint: Endpoint,
    *,
    sftp_client: Any | None = None,
    file_client: Any | None = None,
) -> FsBackend:
    """Select ``LocalFs`` / ``SftpFs`` / ``WinrmFs`` from the endpoint transport.

    Optional *sftp_client* / *file_client* inject a ready client (used when
    the transport object itself is not available or when a caller supplies a
    pre-opened channel).
    """
    transport = endpoint.transport
    if transport is None:
        raise FsError("NOT_CONNECTED", "endpoint transport not connected")

    name = transport.name
    if name == "local":
        return LocalFs(cwd=endpoint.cwd or transport.cwd)

    if name == "ssh":
        return _sftp_backend(
            transport,
            endpoint=endpoint,
            sftp_client=sftp_client,
        )

    if name == "winrm":
        return _winrm_backend(
            transport,
            endpoint=endpoint,
            file_client=file_client,
        )

    raise FsError(
        "UNSUPPORTED",
        f"no fs backend for transport {name!r}",
        details={"transport": name},
    )


def _sftp_backend(
    transport: BaseTransport,
    *,
    endpoint: Endpoint,
    sftp_client: Any | None,
) -> SftpFs:
    if sftp_client is not None:
        return SftpFs(
            sftp_client,
            cwd=endpoint.cwd or transport.cwd,
            home=transport.home,
        )

    if isinstance(transport, SSHTransport):
        return SftpFs(
            factory=transport.open_sftp,
            cwd=endpoint.cwd or transport.cwd,
            home=transport.home,
        )

    # Duck-type: any transport exposing open_sftp().
    opener = getattr(transport, "open_sftp", None)
    if callable(opener):
        return SftpFs(
            factory=opener,
            cwd=endpoint.cwd or transport.cwd,
            home=getattr(transport, "home", None),
        )
    raise FsError(
        "UNSUPPORTED",
        "ssh transport cannot open sftp",
        details={"transport": transport.name},
    )


def _winrm_ps_caps(transport: BaseTransport) -> dict[str, Any] | None:
    """Return ``transport.meta["winrm_ps"]`` when present as a dict.

    ``None`` means absent / unprobed — WinrmFs keeps legacy allow.
    """
    meta = getattr(transport, "meta", None)
    if not isinstance(meta, dict):
        return None
    caps = meta.get("winrm_ps")
    if isinstance(caps, dict):
        return caps
    return None


def _winrm_backend(
    transport: BaseTransport,
    *,
    endpoint: Endpoint,
    file_client: Any | None,
) -> WinrmFs:
    ps_caps = _winrm_ps_caps(transport)
    if file_client is not None:
        return WinrmFs(
            file_client,
            cwd=endpoint.cwd or transport.cwd,
            home=transport.home,
            ps_caps=ps_caps,
        )

    if isinstance(transport, WinRMTransport):
        return WinrmFs(
            factory=transport.open_fs,
            cwd=endpoint.cwd or transport.cwd,
            home=transport.home,
            ps_caps=ps_caps,
        )

    opener = getattr(transport, "open_fs", None)
    if callable(opener):
        return WinrmFs(
            factory=opener,
            cwd=endpoint.cwd or transport.cwd,
            home=getattr(transport, "home", None),
            ps_caps=ps_caps,
        )
    raise FsError(
        "UNSUPPORTED",
        "winrm transport cannot open fs client",
        details={"transport": transport.name},
    )
