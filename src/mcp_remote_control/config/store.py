"""Write config-home artifacts for agent self-configuration.

Owns layout creation, profile TOML writes, and secret-file material under
``MRC_HOME``. Security invariants:

- Secret *bodies* are written only under ``secrets/`` (mode ``0o600``);
  ``secrets/`` itself is ``0o700``. Profile TOML never persists secret
  bodies outside ``[auth]``, and even there only ``*_path`` / env-name
  fields are kept (inline bodies are stripped or rejected).
- Profile and secret names are validated before path join so values like
  ``../config`` cannot escape ``profiles/`` or ``secrets/``.
- Writes are atomic (same-dir temp + ``os.replace``). Secret writes open
  the temp file at ``0o600`` so there is no world-readable window.
- When ``[security].strict_perms`` is true, profile files are also
  ``0o600`` and ``profiles/`` is ``0o700``. Secret modes are always
  enforced regardless of that flag.
- Read APIs and ``profile_public_dict`` expose paths and flags only —
  never secret bodies.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from mcp_remote_control.config.errors import (
    ConfigInvalid,
    ProfileInvalid,
    ProfileNotFound,
)
from mcp_remote_control.config.load import load_config, load_profile
from mcp_remote_control.config.paths import (
    config_toml_path,
    profiles_dir,
    secrets_dir,
)

# fullmatch rejects names with a trailing newline (a $ anchor would still allow it).
_PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SECRET_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._@+-]*")
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_VALID_TRANSPORTS = frozenset({"local", "ssh", "winrm"})

# Local equivalent of render.redact.is_sensitive_key. Duplicated here to avoid
# a config→render layering dependency (render is the output layer; config is
# foundational and must not import from it). Keep in sync with redact.py.
_SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "private_key",
        "privatekey",
        "private_key_pem",
        "passphrase",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "certificate_key_password",
        "cert_key_password",
    }
)

# Known top-level [auth] inline secret *body* field names. Stripped on write
# (tolerated-discouraged path). Also present in _SENSITIVE_KEY_NAMES so non-auth
# sections reject them; listed here so the strip set is explicit.
_AUTH_INLINE_SECRET_KEYS: frozenset[str] = frozenset(
    {"password", "private_key_pem", "private_key", "passphrase"}
)


def _is_sensitive_key(name: str) -> bool:
    """Return True if *name* looks like a secret-bearing field name."""
    n = name.strip().lower().replace("-", "_")
    if n in _SENSITIVE_KEY_NAMES:
        return True
    for sk in _SENSITIVE_KEY_NAMES:
        if n == sk or n.endswith("_" + sk):
            return True
    return False


def _find_sensitive_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Return dotted paths of sensitive keys anywhere in *data* (recurses)."""
    found: list[str] = []
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if _is_sensitive_key(str(k)):
            found.append(path)
        if isinstance(v, dict):
            found.extend(_find_sensitive_keys(v, path))
    return found


def _clean_auth_table(auth: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *auth* with top-level inline secret-body keys removed.

    Raises ``ProfileInvalid`` if any nested sub-table contains a sensitive key
    (recurse-and-reject mirrors the non-auth section policy — flat strip alone
    would miss nested values such as ``auth.credssp.client_secret``).
    """
    cleaned = {
        k: v
        for k, v in auth.items()
        if k not in _AUTH_INLINE_SECRET_KEYS
        and not _is_sensitive_key(str(k))
        and v is not None
    }
    leaks = _find_sensitive_keys(cleaned)
    if leaks:
        raise ProfileInvalid(
            f"section [auth] must not contain secret-bearing keys: "
            f"{', '.join(leaks)} (use secrets/ files + *_path fields instead)"
        )
    return cleaned


def _enforce_no_secret_bodies(parsed: dict[str, Any]) -> None:
    """Reject any secret-bearing key outside the [auth] section.

    Scans every top-level non-auth dict section recursively, plus top-level
    scalar sensitive keys. Use for the ``body=`` path so agent-supplied TOML
    cannot persist secrets in ssh/winrm/defaults/caps (or arbitrary sections).
    Does not mutate; the [auth] table is handled separately by the caller via
    ``_clean_auth_table``.
    """
    for k, v in parsed.items():
        if k == "auth":
            continue
        if isinstance(v, dict):
            if v:
                leaks = _find_sensitive_keys(v)
                if leaks:
                    raise ProfileInvalid(
                        f"section [{k}] must not contain secret-bearing "
                        f"keys: {', '.join(leaks)} (use secrets/ files + "
                        f"auth.*_path fields instead)"
                    )
        elif v is not None and _is_sensitive_key(str(k)):
            raise ProfileInvalid(
                f"top-level key {k!r} must not be a secret-bearing field "
                f"(use [auth].*_path fields instead)"
            )


def ensure_home_layout(home: Path) -> dict[str, str]:
    """Create standard MRC_HOME directories; return created/existing paths.

    Always sets ``secrets/`` to mode ``0o700`` (including when the directory
    already existed with looser permissions). ``chmod`` failures propagate.
    """
    home = Path(home).expanduser().resolve()
    dirs = {
        "home": home,
        "profiles": profiles_dir(home),
        "secrets": secrets_dir(home),
        "logs": home / "logs",
        "state": home / "state",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    # secrets/ must be 0o700 — enforce on every ensure so a pre-existing
    # world-readable directory is tightened. Do not swallow OSError.
    os.chmod(dirs["secrets"], 0o700)
    cfg = config_toml_path(home)
    if not cfg.is_file():
        cfg.write_text(
            "# mcp-remote-control — agent/self managed\n"
            "[defaults]\n"
            'verbosity = "normal"\n'
            "\n"
            "[logging]\n"
            'level = "info"\n'
            'dir = "logs"\n'
            "\n"
            "[security]\n"
            "strict_perms = false\n",
            encoding="utf-8",
        )
    return {k: str(v) for k, v in dirs.items()} | {"config": str(cfg)}


def _toml_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _toml_str(s: str) -> str:
    return f'"{_toml_escape(s)}"'


def _toml_key(k: str) -> str:
    """Render a TOML key: bare if safe, else quoted (handles spaces/dots/special)."""
    return k if _BARE_KEY_RE.fullmatch(k) else _toml_str(k)


def _toml_scalar(v: Any) -> str:
    """Render a single TOML scalar value (bool/int/float/str)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    # str and anything else → quoted string (best-effort, round-trippable).
    return _toml_str(str(v))


def _toml_array(items: list[Any] | tuple[Any, ...]) -> str:
    """Render a TOML array of scalars. Dict elements fall back to quoted str."""
    parts: list[str] = []
    for x in items:
        if x is None:
            continue
        if isinstance(x, dict):
            # Array-of-tables needs [[name]] syntax — out of scope here.
            parts.append(_toml_str(str(x)))
        else:
            parts.append(_toml_scalar(x))
    return "[" + ", ".join(parts) + "]"


def _collect_table_blocks(
    segments: list[str], data: dict[str, Any], out: list[str]
) -> None:
    """Append ``[seg1.seg2]`` block(s) for *data*, recursing into nested dicts
    as ``[parent.child]`` sub-tables (standard TOML). Each path segment and
    scalar key is quoted via ``_toml_key`` when it is not a bare key."""
    if not data:
        return
    header = ".".join(_toml_key(s) for s in segments)
    scalar_lines: list[str] = []
    subtables: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if v is None:
            continue
        key = _toml_key(str(k))
        if isinstance(v, dict):
            if v:  # non-empty sub-table → recurse
                subtables.append((str(k), v))
        elif isinstance(v, bool):
            scalar_lines.append(f"{key} = {'true' if v else 'false'}")
        elif isinstance(v, int) and not isinstance(v, bool):
            scalar_lines.append(f"{key} = {v}")
        elif isinstance(v, float):
            scalar_lines.append(f"{key} = {v!r}")
        elif isinstance(v, (list, tuple)):
            scalar_lines.append(f"{key} = {_toml_array(v)}")
        else:
            scalar_lines.append(f"{key} = {_toml_str(str(v))}")
    # Emit the [header] when there are scalar lines, or when *data* had only
    # empty-subtable keys (preserve a marker for the parent table).
    if scalar_lines or not subtables:
        out.append(f"[{header}]\n" + "\n".join(scalar_lines))
    for child_name, child_data in subtables:
        _collect_table_blocks([*segments, child_name], child_data, out)


def _toml_table(name: str, data: dict[str, Any]) -> str:
    if not data:
        return ""
    blocks: list[str] = []
    _collect_table_blocks([name], data, blocks)
    return "\n\n".join(blocks) + "\n"


def _render_profile_from_dict(data: dict[str, Any]) -> str:
    """Re-serialize a parsed profile dict as TOML (top-level scalars, then tables).

    Used by the ``body=`` path after stripping inline auth secrets so the
    written file never contains the stripped secret bodies.
    """
    scalar_lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            if v:
                tables.append((str(k), v))
        else:
            key = _toml_key(str(k))
            if isinstance(v, bool):
                scalar_lines.append(f"{key} = {'true' if v else 'false'}")
            elif isinstance(v, int) and not isinstance(v, bool):
                scalar_lines.append(f"{key} = {v}")
            elif isinstance(v, float):
                scalar_lines.append(f"{key} = {v!r}")
            elif isinstance(v, (list, tuple)):
                scalar_lines.append(f"{key} = {_toml_array(v)}")
            else:
                scalar_lines.append(f"{key} = {_toml_str(str(v))}")
    parts: list[str] = []
    if scalar_lines:
        parts.append("\n".join(scalar_lines))
    for tbl_name, tbl in tables:
        chunk = _toml_table(tbl_name, tbl).rstrip("\n")
        if chunk:
            parts.append(chunk)
    return "\n\n".join(parts) + "\n"


def render_profile_toml(
    *,
    name: str,
    transport: str,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    label: str | None = None,
    auth: dict[str, Any] | None = None,
    ssh: dict[str, Any] | None = None,
    winrm: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
) -> str:
    """Build a profile TOML string (no secret bodies — paths only in auth)."""
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {_PROFILE_NAME_RE.pattern}"
        )
    if transport not in _VALID_TRANSPORTS:
        raise ProfileInvalid(
            f"transport must be one of {sorted(_VALID_TRANSPORTS)}, got {transport!r}"
        )
    if transport in ("ssh", "winrm"):
        if not host or not username:
            raise ProfileInvalid(
                f"transport={transport!r} requires host= and username="
            )

    lines: list[str] = [
        f"name = {_toml_str(name)}",
        f"transport = {_toml_str(transport)}",
    ]
    if host:
        lines.append(f"host = {_toml_str(host)}")
    if port is not None:
        lines.append(f"port = {int(port)}")
    if username:
        lines.append(f"username = {_toml_str(username)}")
    if label:
        lines.append(f"label = {_toml_str(label)}")
    lines.append("")
    if auth:
        # Strip top-level inline secret bodies; reject sensitive keys in
        # nested auth sub-tables (flat strip alone would leave them).
        cleaned = _clean_auth_table(auth)
        chunk = _toml_table("auth", cleaned)
        if chunk:
            lines.append(chunk.rstrip("\n"))
            lines.append("")
    # Non-auth sections never carry secret bodies: reject (clearer than strip).
    for section, data in (
        ("ssh", ssh),
        ("winrm", winrm),
        ("defaults", defaults),
        ("caps", caps),
    ):
        if data:
            leaks = _find_sensitive_keys(data)
            if leaks:
                raise ProfileInvalid(
                    f"section [{section}] must not contain secret-bearing "
                    f"keys: {', '.join(leaks)} (use secrets/ files + "
                    f"auth.*_path fields instead)"
                )
            lines.append(_toml_table(section, data).rstrip("\n"))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def put_profile(
    home: Path,
    *,
    name: str,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    label: str | None = None,
    auth: dict[str, Any] | None = None,
    ssh: dict[str, Any] | None = None,
    winrm: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    caps: dict[str, Any] | None = None,
    body: str | None = None,
) -> Path:
    """Write ``profiles/{name}.toml`` and validate via ``load_profile``.

    Validates *name* before joining under ``profiles/``. Secret bodies are
    never written outside ``[auth]`` path/env fields: structured fields are
    scanned, and the ``body=`` path strips/rejects inline secrets before
    the atomic write. On post-write validation failure the new file is
    unlinked best-effort so a rejected profile is not left at rest.

    When ``[security].strict_perms`` is true, the profile file is set to
    ``0o600`` and ``profiles/`` to ``0o700`` after the write.
    """
    home = Path(home).expanduser().resolve()
    ensure_home_layout(home)
    # Validate name before path join so traversal values cannot escape
    # profiles/ (e.g. ``../config``).
    if not name or not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {_PROFILE_NAME_RE.pattern}"
        )
    path = profiles_dir(home) / f"{name}.toml"
    if body is not None:
        text = body if body.endswith("\n") else body + "\n"
        # Parse body to decide whether a correct top-level name key is
        # present. Bare substring match is insufficient (``username=…``
        # contains the letters ``name``). Reject invalid/non-dict bodies
        # before writing so no bad file is persisted.
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileInvalid(f"body is not valid TOML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProfileInvalid("body must be a TOML table (root key/values)")
        existing = parsed.get("name")
        name_was_missing = existing is None
        if name_was_missing:
            parsed["name"] = name
        elif existing != name:
            raise ProfileInvalid(
                f"body name {existing!r} does not match profile name "
                f"{name!r}"
            )
        # Scan non-auth sections + top-level scalars for secret bodies;
        # strip [auth] inline secrets and reject nested [auth.*] secrets.
        auth_table = parsed.get("auth")
        auth_stripped = False
        if isinstance(auth_table, dict):
            cleaned_auth = _clean_auth_table(auth_table)
            if cleaned_auth != auth_table:
                parsed["auth"] = cleaned_auth
                auth_stripped = True
        elif auth_table is not None:
            # Non-dict [auth] (string/list/etc.) is neither cleaned nor
            # scanned by the section walk (which skips the "auth" key).
            # Without a pre-write reject, a body like
            # ``auth = "password=s3cr3t"`` would reach disk before
            # load_profile fails. Reject before the atomic write so
            # nothing persists.
            raise ProfileInvalid(
                f"profile {name!r}: [auth] must be a table"
            )
        _enforce_no_secret_bodies(parsed)
        # If we stripped auth secrets, re-render from the cleaned dict so
        # stripped bodies never reach disk. If only name was missing,
        # prepend to the original text (preserves comments/formatting).
        if auth_stripped:
            text = _render_profile_from_dict(parsed)
        elif name_was_missing:
            text = f'name = "{_toml_escape(name)}"\n' + text
        # else: keep text as-is
    else:
        if not transport:
            raise ProfileInvalid("transport required when body is not provided")
        text = render_profile_toml(
            name=name,
            transport=transport,
            host=host,
            port=port,
            username=username,
            label=label,
            auth=auth,
            ssh=ssh,
            winrm=winrm,
            defaults=defaults,
            caps=caps,
        )
    # Read strict_perms before the atomic write so a broken config.toml
    # surfaces without leaving a half-written profile. Secret modes
    # (0o700 secrets/, 0o600 secret files) are always enforced; when
    # strict_perms is true, also tighten profile.toml → 0o600 and
    # profiles/ → 0o700 (profiles/ is 0o755 by default mkdir).
    strict = _security_strict_perms(home)
    # Atomic write: temp file in the same directory, then os.replace.
    _atomic_write_text(profiles_dir(home), path, text)
    if strict:
        os.chmod(path, 0o600)
        os.chmod(profiles_dir(home), 0o700)
    # Round-trip validate. On any post-write failure, unlink the new file
    # best-effort so a rejected profile is not left at rest. Atomic write
    # only preserves the prior file on mid-write failure; it does not undo
    # a successful os.replace when validation rejects the new content.
    try:
        load_profile(home, name)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path.resolve()


def delete_profile(home: Path, name: str) -> Path:
    """Remove ``profiles/{name}.toml``.

    Validates *name* before path construction so traversal values like
    ``../config`` cannot delete files outside ``profiles/``.
    """
    if not name or not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {_PROFILE_NAME_RE.pattern}"
        )
    home = Path(home).expanduser().resolve()
    path = profiles_dir(home) / f"{name}.toml"
    if not path.is_file():
        # ProfileNotFound is a ConfigError so direct library callers using
        # ``except ConfigError`` catch the missing case.
        raise ProfileNotFound(str(path))
    path.unlink()
    return path


def _atomic_write_text(dir_path: Path, target: Path, text: str) -> None:
    """Write *text* to *target* via a same-dir temp + ``os.replace`` swap.

    On any exception the temp file is removed and the original is left intact.
    If ``os.fdopen`` itself fails, the raw fd is closed before cleanup.
    """
    fd, tmp = tempfile.mkstemp(
        dir=dir_path, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with fh:
            fh.write(text)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_bytes_restricted(
    dir_path: Path, target: Path, content: bytes, mode: int
) -> None:
    """Write *content* to *target* atomically with restrictive permissions.

    Uses ``tempfile.mkstemp`` (creates the file at ``0o600`` via ``os.open`` —
    no world-readable window), then ``os.chmod`` to the requested *mode*, then
    ``os.replace`` for the atomic swap. ``chmod`` ``OSError`` is not swallowed.
    If ``os.fdopen`` itself fails, the raw fd is closed before cleanup.
    """
    fd, tmp = tempfile.mkstemp(
        dir=dir_path, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        try:
            fh = os.fdopen(fd, "wb")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def put_secret(
    home: Path,
    *,
    name: str,
    content: str,
    mode: int = 0o600,
) -> Path:
    """Write secret material under ``secrets/`` with restrictive permissions.

    Creates the file at mode ``0o600`` (or *mode*) with no world-readable
    window and replaces the destination atomically. Secret bodies are never
    returned by config read APIs and must not be logged by callers.

    Validates *name* (and rejects path separators / ``..``) before joining
    under ``secrets/``.
    """
    home = Path(home).expanduser().resolve()
    ensure_home_layout(home)
    if not name or not _SECRET_NAME_RE.fullmatch(name):
        raise ConfigInvalid(
            f"secret name {name!r} must match {_SECRET_NAME_RE.pattern}"
        )
    if content is None:
        raise ConfigInvalid("secret content is required")
    # Disallow path traversal in the secret name.
    if "/" in name or "\\" in name or ".." in name:
        raise ConfigInvalid("secret name must not contain path separators")
    path = secrets_dir(home) / name
    # Atomic, restrictive-perm write (no world-readable window, no partial
    # file on failure). chmod OSError propagates.
    _atomic_write_bytes_restricted(
        secrets_dir(home), path, content.encode("utf-8"), mode
    )
    return path.resolve()


def list_secret_names(home: Path) -> list[str]:
    """Return sorted secret file names under ``secrets/`` (names only, no bodies)."""
    sdir = secrets_dir(home)
    if not sdir.is_dir():
        return []
    names = [p.name for p in sdir.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(names)


def _security_strict_perms(home: Path) -> bool:
    """Return ``[security].strict_perms`` from the global config (default False).

    Secret-file permissions (``0o700`` ``secrets/``, ``0o600`` secret files)
    are always enforced by ``ensure_home_layout`` / ``put_secret``, independent
    of this flag. When True, ``put_profile`` additionally hardens profile
    artifacts (profile TOML → ``0o600``, ``profiles/`` → ``0o700``).

    A missing ``config.toml`` yields the default (False). Parse errors
    propagate as ``ConfigInvalid`` (not swallowed).
    """
    return bool(load_config(home).security.strict_perms)


def profile_public_dict(profile: Any) -> dict[str, Any]:
    """JSON-safe profile view (paths and flags only — no secret bodies).

    Exposes every non-secret ``AuthConfig`` field, including WinRM enterprise
    auth fields (cert paths, SPN, negotiate/CredSSP options,
    ``has_inline_*`` flags). Secret bodies are never stored on ``AuthConfig``,
    so they cannot appear here.
    """
    out: dict[str, Any] = {
        "name": profile.name,
        "transport": profile.transport,
    }
    if profile.host:
        out["host"] = profile.host
    if profile.port is not None:
        out["port"] = profile.port
    if profile.username:
        out["username"] = profile.username
    if profile.label:
        out["label"] = profile.label
    if profile.auth is not None:
        a = profile.auth
        auth: dict[str, Any] = {"method": a.method}
        if a.key_path is not None:
            auth["key_path"] = str(a.key_path)
        if a.password_path is not None:
            auth["password_path"] = str(a.password_path)
        if a.passphrase_path is not None:
            auth["passphrase_path"] = str(a.passphrase_path)
        if a.password_env is not None:
            auth["password_env"] = a.password_env
        if a.has_inline_password:
            auth["has_inline_password"] = True
        if a.has_inline_private_key:
            auth["has_inline_private_key"] = True
        # WinRM enterprise: paths / non-secret strings / flags only.
        # AuthConfig never stores secret bodies.
        if a.cert_path is not None:
            auth["cert_path"] = str(a.cert_path)
        if a.cert_key_path is not None:
            auth["cert_key_path"] = str(a.cert_key_path)
        if a.cert_key_password_path is not None:
            auth["cert_key_password_path"] = str(a.cert_key_password_path)
        if a.spn is not None:
            auth["spn"] = a.spn
        if a.negotiate_hostname_override is not None:
            auth["negotiate_hostname_override"] = a.negotiate_hostname_override
        if a.negotiate_service is not None:
            auth["negotiate_service"] = a.negotiate_service
        if a.negotiate_delegate is not None:
            auth["negotiate_delegate"] = a.negotiate_delegate
        if a.credssp_auth_mechanism is not None:
            auth["credssp_auth_mechanism"] = a.credssp_auth_mechanism
        if a.credssp_disable_tlsv1_2 is not None:
            auth["credssp_disable_tlsv1_2"] = a.credssp_disable_tlsv1_2
        if a.credssp_minimum_version is not None:
            auth["credssp_minimum_version"] = a.credssp_minimum_version
        out["auth"] = auth
    if profile.ssh:
        out["ssh"] = dict(profile.ssh)
    if profile.winrm:
        out["winrm"] = dict(profile.winrm)
    if profile.defaults:
        out["defaults"] = dict(profile.defaults)
    if profile.caps:
        out["caps"] = dict(profile.caps)
    if profile.source_path is not None:
        out["source_path"] = str(profile.source_path)
    return out


def secret_rel_path(home: Path, abs_path: Path) -> str:
    """Prefer secrets-relative path for profile auth fields."""
    try:
        rel = abs_path.resolve().relative_to(Path(home).resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(abs_path)
