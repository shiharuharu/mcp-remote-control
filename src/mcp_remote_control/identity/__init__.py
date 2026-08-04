"""Identity helpers: SSH client key path resolution and related chains."""

from __future__ import annotations

from mcp_remote_control.identity.ssh_keys import (
    DEFAULT_SSH_IDENTITY_BASENAMES,
    resolve_ssh_key_paths,
)

__all__ = [
    "DEFAULT_SSH_IDENTITY_BASENAMES",
    "resolve_ssh_key_paths",
]
