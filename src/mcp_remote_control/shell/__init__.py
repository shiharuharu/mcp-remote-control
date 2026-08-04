"""Shell dialect model: probe templates, capability flags, resolve helpers.

Coarse dialect labels (posix-bash, busybox, powershell, …) drive silent cwd
probes and exec script runtime defaults. Not a full shell taxonomy.
"""

from __future__ import annotations

from mcp_remote_control.shell.dialect import (
    BUSYBOX_PROBE_MAX_LEN,
    DIALECT_PROBES,
    ProbeSpec,
    ShellCaps,
    ShellDialect,
    default_runtime_for_dialect,
    probe_cmd_for_dialect,
    resolve_dialect,
    transport_family_for_dialect,
)

__all__ = [
    "BUSYBOX_PROBE_MAX_LEN",
    "DIALECT_PROBES",
    "ProbeSpec",
    "ShellCaps",
    "ShellDialect",
    "default_runtime_for_dialect",
    "probe_cmd_for_dialect",
    "resolve_dialect",
    "transport_family_for_dialect",
]
