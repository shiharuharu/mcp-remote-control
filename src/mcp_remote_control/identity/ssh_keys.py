"""SSH identity file resolution and default fallback chain.

Rules:

- If ``profile.auth.key_path`` is set → only that path (explicit key).
- Else default chain under ``~/.ssh``: ``id_rsa``, ``id_ecdsa``, ``id_ed25519``
  (and optional ``*_sk`` variants). Order is intentional: **id_rsa before
  id_ed25519**. By default only existing files are returned.

Never loads key **contents** into returned structures or logs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_remote_control.config.models import Profile

# Default identity basenames under ~/.ssh. Order: id_rsa before id_ed25519.
DEFAULT_SSH_IDENTITY_BASENAMES: tuple[str, ...] = (
    "id_rsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
)


def default_ssh_dir() -> Path:
    """Return ``~/.ssh`` (expanded)."""
    return (Path.home() / ".ssh").expanduser()


def resolve_ssh_key_paths(
    profile: Profile,
    *,
    ssh_dir: Path | None = None,
    only_existing: bool = True,
) -> list[Path]:
    """Resolve ordered client key paths for *profile*.

    Args:
        profile: Loaded connection profile.
        ssh_dir: Override for ``~/.ssh`` (injection / alternate homes).
        only_existing: When True (default), skip missing files in the default
            chain. Explicit ``auth.key_path`` is always returned even if missing
            so the transport can surface a clear connect error.

    Returns:
        Ordered list of key **paths** only (never file contents).
    """
    auth = profile.auth
    if auth is not None and auth.key_path is not None:
        # Explicit single key: do not scan the default chain.
        return [Path(auth.key_path).expanduser()]

    root = Path(ssh_dir) if ssh_dir is not None else default_ssh_dir()
    root = root.expanduser()
    paths: list[Path] = []
    for name in DEFAULT_SSH_IDENTITY_BASENAMES:
        candidate = root / name
        if only_existing:
            if candidate.is_file():
                paths.append(candidate)
        else:
            paths.append(candidate)
    return paths


def identity_labels(paths: list[Path]) -> list[str]:
    """Human-safe labels for tried keys (basename only)."""
    return [p.name for p in paths]
