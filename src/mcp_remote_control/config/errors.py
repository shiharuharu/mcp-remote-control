"""Typed errors for configuration and profile loading."""

from __future__ import annotations

from pathlib import Path


class ConfigError(Exception):
    """Base error for config home, global config, or profile issues."""


class ProfileNotFound(ConfigError):
    """Requested profile file does not exist under ``profiles/``."""


class ProfileInvalid(ConfigError):
    """Profile TOML is unreadable, has bad shape, or fails validation."""


class ConfigInvalid(ConfigError):
    """Global ``config.toml`` is unreadable or has invalid values."""


class NotesNotFound(ConfigError):
    """Requested notes file does not exist under ``notes/``."""


def public_path_for_msg(
    home: Path | str | None,
    path: Path | str | None,
) -> str:
    """Format a path for config exception / agent-facing error messages.

    Paths under *home* become posix-relative (``profiles/...``, ``secrets/...``,
    ``notes/...``, ``config.toml``) so agents are not steered into shell-browsing
    ``/Users/...`` or ``/home/...`` config trees.

    Paths the user deliberately set *outside* the config home stay absolute
    (do not invent a fake relative).

    Returns an empty string when *path* is ``None``.
    """
    if path is None:
        return ""
    p = Path(path)
    if home is None:
        return str(p).replace("\\", "/")
    try:
        rel = p.resolve().relative_to(Path(home).expanduser().resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        # Outside config home: keep absolute (user-deliberate exception).
        return str(p).replace("\\", "/")
    except OSError:
        # Unresolvable path: best-effort posix string, still no crash in msg.
        return str(p).replace("\\", "/")


def _password_source_present(value: object | None) -> bool:
    """True when a password source field counts as material.

    ``True`` means already-resolved inline material (load path). ``False`` /
    ``None`` / empty / whitespace-only strings do not count as a second source.
    """
    if value is True:
        return True
    if value is False or value is None:
        return False
    return bool(str(value).strip())


def reject_dual_password_sources(
    *,
    password: object | None = None,
    password_path: object | None = None,
    password_env: object | None = None,
    profile_name: str | None = None,
    loc: Path | str | None = None,
) -> None:
    """Hard-reject when more than one password material source is set.

    Agents must pick plain ``password``, ``password_path``, *or*
    ``password_env`` - never combine them. Silent priority is ambiguous.

    Empty / whitespace-only ``password`` (and blank path/env) do not count as
    a source. Shared by config load (TOML parse), store put/write, and
    ``config_ops._normalize_auth`` so choose-one detection and message cannot
    drift.

    *password* may be the raw value or a precomputed bool (``True`` =
    inline material present). When *profile_name* is set, the message is
    load-style (``profile 'name': ... (loc)``); otherwise write-style
    (``[auth] has multiple...``).
    """
    present: list[str] = []
    if _password_source_present(password):
        present.append("password")
    if _password_source_present(password_path):
        present.append("password_path")
    if _password_source_present(password_env):
        present.append("password_env")
    if len(present) <= 1:
        return
    core = (
        f"[auth] has multiple password sources ({', '.join(present)}); "
        f"choose exactly one of: password | password_path | password_env"
    )
    if profile_name is not None:
        suffix = f" ({loc})" if loc is not None else ""
        raise ProfileInvalid(f"profile {profile_name!r}: {core}{suffix}")
    raise ProfileInvalid(core)
