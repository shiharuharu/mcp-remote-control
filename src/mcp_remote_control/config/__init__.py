"""Configuration loading and profiles.

Public API for resolving ``MRC_HOME``, loading optional ``config.toml``, and
reading connection profiles from ``profiles/*.toml``. Secret material is
represented as paths only; bodies live under ``secrets/`` and are never
returned by these read APIs. Mutations (put profile/secret) live in
``config.store``.
"""

from __future__ import annotations

from mcp_remote_control.config.errors import (
    ConfigError,
    ConfigInvalid,
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
    profiles_dir,
    resolve_home,
    resolve_under_home,
    secrets_dir,
)

__all__ = [
    "AuthConfig",
    "ConfigError",
    "ConfigInvalid",
    "DefaultsConfig",
    "GlobalConfig",
    "LoggingConfig",
    "Profile",
    "ProfileInvalid",
    "ProfileNotFound",
    "SecurityConfig",
    "config_toml_path",
    "list_profiles",
    "load_config",
    "load_profile",
    "profiles_dir",
    "resolve_home",
    "resolve_under_home",
    "secrets_dir",
]
