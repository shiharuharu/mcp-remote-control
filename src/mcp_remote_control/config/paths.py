"""Resolve the user config home directory (``MRC_HOME``)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# Absolute default: ``{Path.home()}/.config/mcp-remote-control`` (joined parts,
# never string-templated with ``/Users/<you>`` placeholders).
DEFAULT_HOME_REL = Path(".config") / "mcp-remote-control"

# Prefer MRC_HOME, then legacy MCP_REMOTE_CONTROL_HOME, then the default path.
_ENV_MRC_HOME = "MRC_HOME"
_ENV_LEGACY_HOME = "MCP_REMOTE_CONTROL_HOME"

# Doc / copy-paste placeholders that must never become real filesystem roots.
# Examples: ``/Users/<you>/.config/...``, ``/home/<user>/...``, bare ``<you>``.
_PLACEHOLDER_TOKEN = re.compile(r"<[^>\s/]+>")
# Unexpanded shell vars after expandvars (Host often does not expand env in JSON).
_UNEXPANDED_VAR = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")


def default_home() -> Path:
    """Return the absolute default config root (no env override).

    Built by joining ``Path.home().resolve()`` with ``.config/mcp-remote-control``
    so the path is always a real absolute location on this machine.
    """
    return Path.home().resolve() / DEFAULT_HOME_REL


def _looks_like_unusable_home(text: str) -> bool:
    """True if *text* is a doc placeholder or still contains unexpanded ``$VAR``."""
    s = text.strip()
    if not s:
        return True
    if _PLACEHOLDER_TOKEN.search(s):
        return True
    # After expandvars, residual ``$FOO`` / ``${FOO}`` means Host did not supply them.
    if _UNEXPANDED_VAR.search(s):
        return True
    return False


def _coerce_home_path(raw: str) -> Path | None:
    """Expand ``~`` / ``$HOME`` and return an absolute Path, or None if unusable.

    Order: strip → expandvars → expanduser → reject placeholders / leftover
    ``$VAR`` → resolve absolute (relative values resolve against cwd).
    """
    text = str(raw).strip()
    if not text:
        return None
    # Expand ``$HOME`` / ``${HOME}`` first so Host JSON can pass that form.
    expanded = os.path.expandvars(text)
    expanded = os.path.expanduser(expanded)
    if _looks_like_unusable_home(expanded):
        return None
    path = Path(expanded)
    # Relative override: resolve against process cwd to an absolute path.
    try:
        return path.expanduser().resolve()
    except OSError:
        return None


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """Resolve config root directory to an absolute path.

    Order:
      1. ``MRC_HOME`` (expanded; doc placeholders / unexpanded ``$VAR`` ignored)
      2. ``MCP_REMOTE_CONTROL_HOME`` (legacy alias; same rules)
      3. ``default_home()`` = ``Path.home().resolve() / .config / mcp-remote-control``

    Always returns an absolute path. Invalid overrides fall through to the
    default instead of creating under a non-existent ``/Users/<you>/…`` tree.
    """
    mapping: Mapping[str, str] = os.environ if env is None else env

    for key in (_ENV_MRC_HOME, _ENV_LEGACY_HOME):
        raw = mapping.get(key)
        if raw is None:
            continue
        coerced = _coerce_home_path(str(raw))
        if coerced is not None:
            return coerced

    return default_home()


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
    """Resolve a path relative to *home*, expanding ``~`` / ``$VAR``; absolute kept."""
    text = os.path.expandvars(str(value).strip())
    p = Path(text).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (Path(home) / p).resolve()
