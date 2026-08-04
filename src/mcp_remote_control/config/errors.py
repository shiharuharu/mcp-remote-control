"""Typed errors for configuration and profile loading."""

from __future__ import annotations


class ConfigError(Exception):
    """Base error for config home, global config, or profile issues."""


class ProfileNotFound(ConfigError):
    """Requested profile file does not exist under ``profiles/``."""


class ProfileInvalid(ConfigError):
    """Profile TOML is unreadable, has bad shape, or fails validation."""


class ConfigInvalid(ConfigError):
    """Global ``config.toml`` is unreadable or has invalid values."""
