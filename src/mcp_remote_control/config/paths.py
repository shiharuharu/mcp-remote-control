"""Resolve the user config home directory (``MRC_HOME``)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_HOME_REL = Path(".config") / "mcp-remote-control"

# Prefer MRC_HOME, then legacy MCP_REMOTE_CONTROL_HOME, then the default path.
_ENV_MRC_HOME = "MRC_HOME"
_ENV_LEGACY_HOME = "MCP_REMOTE_CONTROL_HOME"


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """Resolve config root directory.

    Order:
      1. ``MRC_HOME``
      2. ``MCP_REMOTE_CONTROL_HOME`` (legacy alias)
      3. ``~/.config/mcp-remote-control``

    Paths are expanded (``~``) and resolved to absolute.
    """
    mapping: Mapping[str, str] = os.environ if env is None else env

    for key in (_ENV_MRC_HOME, _ENV_LEGACY_HOME):
        raw = mapping.get(key)
        if raw is not None and str(raw).strip() != "":
            return Path(raw).expanduser().resolve()

    return (Path.home() / DEFAULT_HOME_REL).expanduser().resolve()


def profiles_dir(home: Path) -> Path:
    """Return ``{home}/profiles``."""
    return Path(home) / "profiles"


def secrets_dir(home: Path) -> Path:
    """Return ``{home}/secrets``."""
    return Path(home) / "secrets"


def config_toml_path(home: Path) -> Path:
    """Return ``{home}/config.toml``."""
    return Path(home) / "config.toml"


def resolve_under_home(home: Path, value: str | Path) -> Path:
    """Resolve a path relative to *home*, expanding ``~``; absolute paths kept."""
    p = Path(value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (Path(home) / p).resolve()
