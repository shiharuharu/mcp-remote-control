"""Configuration loading and profiles.

Public API for resolving ``MRC_HOME``, loading optional ``config.toml``, and
reading connection profiles from ``profiles/*.toml``. Secret material is
represented as paths only; bodies live under ``secrets/`` and are never
returned by these read APIs. Host notes live under ``notes/{name}.md``
(not inside profile TOML). Mutations (put profile/secret, notes write)
live in ``config.store``.
"""

from __future__ import annotations

from mcp_remote_control.config.errors import (
    ConfigError,
    ConfigInvalid,
    NotesNotFound,
    ProfileInvalid,
    ProfileNotFound,
)
from mcp_remote_control.config.load import list_profiles, load_config, load_profile
from mcp_remote_control.config.models import (
    AuthConfig,
    DefaultsConfig,
    GlobalConfig,
    LoggingConfig,
    Profile,
    SecurityConfig,
)
from mcp_remote_control.config.paths import (
    config_toml_path,
    default_home,
    notes_dir,
    profiles_dir,
    resolve_home,
    resolve_under_home,
    secrets_dir,
)
from mcp_remote_control.config.store import (
    notes_path,
    notes_present,
)

__all__ = [
    "AuthConfig",
    "ConfigError",
    "ConfigInvalid",
    "DefaultsConfig",
    "GlobalConfig",
    "LoggingConfig",
    "NotesNotFound",
    "Profile",
    "ProfileInvalid",
    "ProfileNotFound",
    "SecurityConfig",
    "config_toml_path",
    "default_home",
    "list_profiles",
    "load_config",
    "load_profile",
    "notes_dir",
    "notes_path",
    "notes_present",
    "profiles_dir",
    "resolve_home",
    "resolve_under_home",
    "secrets_dir",
]
