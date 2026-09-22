"""Write config-home artifacts for agent self-configuration.

Owns layout creation, profile TOML writes, optional secret-file material,
and per-profile host notes under ``MRC_HOME``.

- **Passwords** may be written inline in ``[auth].password`` (not treated as
  private for this product). ``password_path`` / ``password_env`` remain
  optional alternatives.
- **Private key bodies** are not written into profiles; use ``secrets/`` +
  ``key_path`` (or strip on write).
- Profile, secret, and notes names are validated before path join so values
  like ``../config`` cannot escape ``profiles/``, ``secrets/``, or ``notes/``.
- Writes are atomic (same-dir temp + flush/fsync + ``os.replace``).
  Secret-file writes open the temp file at ``0o600``. Notes append/prepend
  concatenate in memory then replace the whole file the same way - never
  ``O_APPEND`` mid-file, so a crash cannot leave a half-written paragraph.
- When ``[security].strict_perms`` is true, profile files are also
  ``0o600`` and ``profiles/`` is ``0o700``.
- Host notes live in ``notes/{name}.md`` as raw UTF-8 bytes (no
  universal-newline translation). They are not secrets; PEM armor in a
  write/append/prepend fragment is still rejected. Concatenation is raw
  (no automatic newline). Empty write truncates; empty append/prepend is
  rejected. Same-name write/append/prepend/rm serialize in-process on a
  re-entrant per-name lock, which a caller may also hold across its own
  precondition check (:func:`notes_name_lock`).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

from mcp_remote_control.config.errors import (
    ConfigInvalid,
    NotesNotFound,
    ProfileInvalid,
    ProfileNotFound,
    public_path_for_msg,
    reject_dual_password_sources,
)
from mcp_remote_control.config.load import (
    load_config,
    load_profile,
    looks_like_pem_armor,
)
from mcp_remote_control.config.models import PROFILE_NAME_RE, VALID_TRANSPORTS
from mcp_remote_control.config.paths import (
    config_toml_path,
    notes_dir,
    profiles_dir,
    secrets_dir,
)

_SECRET_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._@+-]*")
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

# Keys forbidden outside [auth]. Password may live only under [auth].password
# (product: plain password OK there), not under ssh=/winrm=/defaults=.
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

# Allowed as plain values only at the top level of [auth].
_AUTH_ALLOWED_PLAIN_KEYS: frozenset[str] = frozenset({"password"})

# Top-level [auth] keys that are still stripped on write (not passwords).
_AUTH_INLINE_SECRET_KEYS: frozenset[str] = frozenset(
    {"private_key_pem", "private_key", "passphrase"}
)

# Auth fields that are filesystem paths (including certificate_pem aliases).
# A PEM body in any of these is never a path and must not be persisted.
_AUTH_CERT_PATH_FIELDS: frozenset[str] = frozenset(
    {
        "cert_path",
        "certificate_pem",
        "cert_key_path",
        "certificate_key_pem",
    }
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
    """Return a copy of *auth* for profile write.

    Keeps top-level ``password`` (plain text allowed). Strips private-key
    bodies. Nested auth sub-tables still reject secret-bearing keys.
    """
    cleaned: dict[str, Any] = {}
    for k, v in auth.items():
        if v is None:
            continue
        key = str(k)
        if key in _AUTH_INLINE_SECRET_KEYS:
            continue
        # cert_path-shaped fields are paths only - reject PEM armor before
        # write so a body is never persisted or Path-joined under MRC_HOME.
        if (
            key in _AUTH_CERT_PATH_FIELDS
            and isinstance(v, str)
            and looks_like_pem_armor(v)
        ):
            raise ProfileInvalid(
                f"[auth].{key} must be a filesystem path, not a PEM body"
            )
        # Plain password before the dict branch so password={} / nested tables
        # are typed errors, not accepted as [auth.password] sub-tables.
        # Empty / whitespace-only str is omitted (unset). Non-string
        # (int/bool/list/dict/...) raises - never silently dropped.
        if key in _AUTH_ALLOWED_PLAIN_KEYS:
            if key == "password":
                if not isinstance(v, str):
                    raise ProfileInvalid(
                        f"[auth].password must be a string, "
                        f"got {type(v).__name__}"
                    )
                if not v.strip():
                    continue
            cleaned[key] = v
            continue
        if isinstance(v, dict):
            leaks = _find_sensitive_keys(v)
            if leaks:
                raise ProfileInvalid(
                    f"section [auth] must not contain secret-bearing keys: "
                    f"{', '.join(f'{key}.{x}' for x in leaks)} "
                    f"(use secrets/ files + *_path fields instead)"
                )
            cleaned[key] = v
            continue
        if _is_sensitive_key(key):
            continue
        cleaned[key] = v
    # Dual password sources must fail before write (clear choose-one; no
    # silent priority). Shared helper with load / config_ops.
    reject_dual_password_sources(
        password=cleaned.get("password"),
        password_path=cleaned.get("password_path"),
        password_env=cleaned.get("password_env"),
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
        "notes": notes_dir(home),
        "state": home / "state",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    # secrets/ must be 0o700 - enforce on every ensure so a pre-existing
    # world-readable directory is tightened. Do not swallow OSError.
    os.chmod(dirs["secrets"], 0o700)
    cfg = config_toml_path(home)
    if not cfg.is_file():
        cfg.write_text(
            "# mcp-remote-control \u2014 agent/self managed\n"
            "[defaults]\n"
            'verbosity = "normal"\n'
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
    # str and anything else -> quoted string (best-effort, round-trippable).
    return _toml_str(str(v))


def _toml_inline_table(data: dict[str, Any]) -> str:
    """Render a TOML inline table ``{ k = v, ... }``.

    Nested dicts and arrays recurse (inline table / array). Never uses
    Python ``str(dict)`` repr. Used for dict elements inside non-AoT arrays
    (mixed lists) and as a safe fallback.
    """
    parts: list[str] = []
    for k, v in data.items():
        if v is None:
            continue
        key = _toml_key(str(k))
        if isinstance(v, dict):
            val = _toml_inline_table(v)
        elif isinstance(v, (list, tuple)):
            val = _toml_array(v)
        else:
            val = _toml_scalar(v)
        parts.append(f"{key} = {val}")
    return "{ " + ", ".join(parts) + " }" if parts else "{}"


def _toml_array(items: list[Any] | tuple[Any, ...]) -> str:
    """Render a TOML array of values.

    Dict elements become inline tables (``{ k = v }``), never Python
    ``str(dict)`` repr. Nested arrays recurse.
    Homogeneous list-of-dicts at table scope are preferably emitted as
    ``[[name]]`` via ``_collect_table_blocks`` / ``_render_profile_from_dict``;
    this helper covers mixed arrays and any remaining dict-in-array cases.
    """
    parts: list[str] = []
    for x in items:
        if x is None:
            continue
        if isinstance(x, dict):
            parts.append(_toml_inline_table(x))
        elif isinstance(x, (list, tuple)):
            parts.append(_toml_array(x))
        else:
            parts.append(_toml_scalar(x))
    return "[" + ", ".join(parts) + "]"


def _is_list_of_dicts(v: Any) -> bool:
    """True when *v* is a non-empty sequence of dicts (AoT candidate).

    Empty lists stay as ``key = []``. Mixed scalar/dict lists are not AoT
    (rendered via ``_toml_array`` with inline tables for any dict elements).
    ``None`` entries are ignored for the homogeneity check.
    """
    if not isinstance(v, (list, tuple)) or not v:
        return False
    dicts = [x for x in v if x is not None]
    return bool(dicts) and all(isinstance(x, dict) for x in dicts)


def _emit_value_line(key: str, v: Any) -> str:
    """Render ``key = <value>`` for a non-None, non-dict scalar/array value."""
    if isinstance(v, bool):
        return f"{key} = {'true' if v else 'false'}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{key} = {v}"
    if isinstance(v, float):
        return f"{key} = {v!r}"
    if isinstance(v, (list, tuple)):
        return f"{key} = {_toml_array(v)}"
    return f"{key} = {_toml_str(str(v))}"


def _partition_table_fields(
    data: dict[str, Any],
) -> tuple[
    list[str],
    list[tuple[str, dict[str, Any]]],
    list[tuple[str, list[Any]]],
]:
    """Split *data* into scalar lines, nested tables, and array-of-tables.

    List values that are homogeneous dict sequences become AoT candidates;
    other lists (scalars, mixed, empty) stay as array value lines.
    """
    scalar_lines: list[str] = []
    subtables: list[tuple[str, dict[str, Any]]] = []
    aots: list[tuple[str, list[Any]]] = []
    for k, v in data.items():
        if v is None:
            continue
        key = _toml_key(str(k))
        if isinstance(v, dict):
            if v:  # non-empty sub-table -> recurse
                subtables.append((str(k), v))
        elif isinstance(v, (list, tuple)) and _is_list_of_dicts(v):
            aots.append((str(k), [x for x in v if x is not None]))
        else:
            scalar_lines.append(_emit_value_line(key, v))
    return scalar_lines, subtables, aots


def _collect_aot_blocks(
    segments: list[str], items: list[Any], out: list[str]
) -> None:
    """Append ``[[seg1.seg2]]`` blocks for each dict in *items* (AoT)."""
    for item in items:
        if item is None or not isinstance(item, dict):
            continue
        _emit_aot_item(segments, item, out)


def _emit_aot_item(
    segments: list[str], item: dict[str, Any], out: list[str]
) -> None:
    """Emit one array-of-tables element and its nested tables / AoTs."""
    header = ".".join(_toml_key(s) for s in segments)
    scalar_lines, subtables, aots = _partition_table_fields(item)
    if scalar_lines:
        out.append(f"[[{header}]]\n" + "\n".join(scalar_lines))
    else:
        # Empty dict element still needs a header so the array slot exists.
        out.append(f"[[{header}]]")
    for child_name, child_data in subtables:
        # Nested [parent.child] attaches to this AoT element (TOML rules).
        _collect_table_blocks([*segments, child_name], child_data, out)
    for child_name, child_items in aots:
        _collect_aot_blocks([*segments, child_name], child_items, out)


def _collect_table_blocks(
    segments: list[str], data: dict[str, Any], out: list[str]
) -> None:
    """Append ``[seg1.seg2]`` block(s) for *data*, recursing into nested dicts
    as ``[parent.child]`` sub-tables and list-of-dicts as ``[[parent.child]]``
    array-of-tables (standard TOML). Each path segment and scalar key is
    quoted via ``_toml_key`` when it is not a bare key."""
    if not data:
        return
    header = ".".join(_toml_key(s) for s in segments)
    scalar_lines, subtables, aots = _partition_table_fields(data)
    # Emit the [header] when there are scalar/array lines, or when *data*
    # had only empty-subtable keys (preserve a marker for the parent table).
    # Pure-AoT / pure-subtable parents omit an empty header (valid TOML).
    if scalar_lines or (not subtables and not aots):
        out.append(f"[{header}]\n" + "\n".join(scalar_lines))
    for child_name, child_data in subtables:
        _collect_table_blocks([*segments, child_name], child_data, out)
    for child_name, child_items in aots:
        _collect_aot_blocks([*segments, child_name], child_items, out)


def _toml_table(name: str, data: dict[str, Any]) -> str:
    if not data:
        return ""
    blocks: list[str] = []
    _collect_table_blocks([name], data, blocks)
    return "\n\n".join(blocks) + "\n"


def _render_profile_from_dict(data: dict[str, Any]) -> str:
    """Re-serialize a parsed profile dict as TOML (top-level scalars, then tables).

    Used by the ``body=`` path after stripping inline auth secrets so the
    written file never contains the stripped secret bodies. List-of-dicts
    values become array-of-tables (``[[name]]``), never Python dict repr.
    """
    scalar_lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    aots: list[tuple[str, list[Any]]] = []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            if v:
                tables.append((str(k), v))
        elif isinstance(v, (list, tuple)) and _is_list_of_dicts(v):
            aots.append((str(k), [x for x in v if x is not None]))
        else:
            key = _toml_key(str(k))
            scalar_lines.append(_emit_value_line(key, v))
    parts: list[str] = []
    if scalar_lines:
        parts.append("\n".join(scalar_lines))
    for tbl_name, tbl in tables:
        chunk = _toml_table(tbl_name, tbl).rstrip("\n")
        if chunk:
            parts.append(chunk)
    for aot_name, aot_items in aots:
        blocks: list[str] = []
        _collect_aot_blocks([aot_name], aot_items, blocks)
        if blocks:
            parts.append("\n\n".join(blocks))
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
    """Build a profile TOML string.

    Auth may include plain ``password``; private key bodies are stripped.
    """
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {PROFILE_NAME_RE.pattern}"
        )
    if transport not in VALID_TRANSPORTS:
        raise ProfileInvalid(
            f"transport must be one of {sorted(VALID_TRANSPORTS)}, got {transport!r}"
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
    the atomic write. After a successful replace, a ``load_profile``
    failure restores the previous dest bytes when they existed; a first
    write that fails validation leaves dest absent.

    When ``[security].strict_perms`` is true, the profile file is set to
    ``0o600`` and ``profiles/`` to ``0o700`` after the write.
    """
    home = Path(home).expanduser().resolve()
    ensure_home_layout(home)
    # Validate name before path join so traversal values cannot escape
    # profiles/ (e.g. ``../config``).
    if not name or not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {PROFILE_NAME_RE.pattern}"
        )
    path = profiles_dir(home) / f"{name}.toml"
    if body is not None:
        text = body if body.endswith("\n") else body + "\n"
        # Parse body to decide whether a correct top-level name key is
        # present. Bare substring match is insufficient (``username=...``
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
    # strict_perms is true, also tighten profile.toml -> 0o600 and
    # profiles/ -> 0o700 (profiles/ is 0o755 by default mkdir).
    strict = _security_strict_perms(home)
    # Snapshot dest before replace. A successful os.replace already
    # swapped the file; unlink-on-reject would erase the only legal
    # profile. Restore those bytes if load_profile rejects the new body.
    prior_text: str | None = None
    if path.is_file():
        prior_text = path.read_text(encoding="utf-8")
    # Atomic write: temp file in the same directory, then os.replace.
    _atomic_write_text(profiles_dir(home), path, text)
    if strict:
        os.chmod(path, 0o600)
        os.chmod(profiles_dir(home), 0o700)
    try:
        load_profile(home, name)
    except Exception:
        _restore_or_remove_profile(path, prior_text)
        raise
    return path.resolve()


def delete_profile(home: Path, name: str) -> Path:
    """Remove ``profiles/{name}.toml``.

    Validates *name* before path construction so traversal values like
    ``../config`` cannot delete files outside ``profiles/``.
    """
    if not name or not isinstance(name, str) or not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {PROFILE_NAME_RE.pattern}"
        )
    home = Path(home).expanduser().resolve()
    path = profiles_dir(home) / f"{name}.toml"
    if not path.is_file():
        # ProfileNotFound is a ConfigError so direct library callers using
        # ``except ConfigError`` catch the missing case. Message uses a
        # home-relative path (profiles/...) so agents are not steered into
        # shell-browsing the absolute config tree.
        raise ProfileNotFound(
            f"profile not found: {name!r} ({public_path_for_msg(home, path)})"
        )
    path.unlink()
    return path


def _atomic_write_text(dir_path: Path, target: Path, text: str) -> None:
    """Write *text* to *target* via same-dir temp + fsync + ``os.replace``.

    Text mode (universal newlines). Profile TOML uses this helper; notes
    I/O must use :func:`_atomic_write_bytes` so CR is stored as written.

    ``flush`` + ``os.fsync`` run on the temp fd before the atomic rename so a
    crash after ``replace`` cannot leave an empty/truncated target (data was
    only in page cache). On any exception the temp is removed and the original
    is left intact. If ``os.fdopen`` itself fails, the raw fd is closed before
    cleanup.
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
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restore_or_remove_profile(path: Path, prior_text: str | None) -> None:
    """Undo dest after a rejected post-replace ``load_profile``.

    ``os.replace`` already installed the new body. Restore *prior_text*
    when dest previously existed so a legal profile is not deleted;
    otherwise unlink so a first write does not leave a rejected file.
    """
    try:
        if prior_text is not None:
            _atomic_write_text(path.parent, path, prior_text)
        else:
            os.unlink(path)
    except OSError:
        # Best-effort: the rejected load_profile error is what callers see.
        pass


def _atomic_write_bytes_restricted(
    dir_path: Path, target: Path, content: bytes, mode: int
) -> None:
    """Write *content* to *target* atomically with restrictive permissions.

    Uses ``tempfile.mkstemp`` (creates the file at ``0o600`` via ``os.open`` -
    no world-readable window), ``flush`` + ``os.fsync`` for durability, then
    ``os.chmod`` to the requested *mode*, then ``os.replace`` for the atomic
    swap. ``chmod`` ``OSError`` is not swallowed. If ``os.fdopen`` itself
    fails, the raw fd is closed before cleanup.
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
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_bytes(dir_path: Path, target: Path, content: bytes) -> None:
    """Write *content* to *target* via same-dir temp + fsync + ``os.replace``.

    Binary write: bytes reach disk unchanged (no universal-newline
    translation). ``flush`` + ``os.fsync`` run on the temp fd before the
    atomic rename. On any exception the temp is removed and the original
    is left intact. If ``os.fdopen`` itself fails, the raw fd is closed
    before cleanup.
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
            fh.flush()
            os.fsync(fh.fileno())
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
    artifacts (profile TOML -> ``0o600``, ``profiles/`` -> ``0o700``).

    A missing ``config.toml`` yields the default (False). Parse errors
    propagate as ``ConfigInvalid`` (not swallowed).
    """
    return bool(load_config(home).security.strict_perms)


def _infer_config_home(profile: Any) -> Path | None:
    """Best-effort config home from ``profiles/<name>.toml`` source_path."""
    sp = getattr(profile, "source_path", None)
    if sp is None:
        return None
    p = Path(sp)
    # Standard layout: <home>/profiles/<name>.toml
    if p.parent.name == "profiles":
        return p.parent.parent
    return None


def profile_public_dict(
    profile: Any,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Profile view for agents: includes inline password; not private keys.

    Auth path fields and ``source_path`` are expressed relative to the config
    home (``secrets/...``, ``profiles/...``) so agents are not steered into shell
    Read/Bash of absolute ``~/.config`` / ``/Users/...`` / ``/home/...`` trees.
    Paths the user deliberately set outside the config home stay absolute.
    Uses :func:`public_path_for_msg` (single relativize rule with error msgs).
    """
    h: Path | None
    if home is not None:
        h = Path(home)
    else:
        h = _infer_config_home(profile)

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
            auth["key_path"] = public_path_for_msg(h, a.key_path)
        if a.password_path is not None:
            auth["password_path"] = public_path_for_msg(h, a.password_path)
        if a.passphrase_path is not None:
            auth["passphrase_path"] = public_path_for_msg(h, a.passphrase_path)
        if a.password_env is not None:
            auth["password_env"] = a.password_env
        if a.password is not None:
            auth["password"] = a.password
        elif a.has_inline_password:
            auth["has_inline_password"] = True
        if a.has_inline_private_key:
            auth["has_inline_private_key"] = True
        # WinRM enterprise: paths / non-secret strings / flags only.
        # AuthConfig never stores secret bodies.
        if a.cert_path is not None:
            auth["cert_path"] = public_path_for_msg(h, a.cert_path)
        if a.cert_key_path is not None:
            auth["cert_key_path"] = public_path_for_msg(h, a.cert_key_path)
        if a.cert_key_password_path is not None:
            auth["cert_key_password_path"] = public_path_for_msg(
                h, a.cert_key_password_path
            )
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
    # Prefer relative source_path (profiles/<name>.toml). Never emit an
    # absolute config-home path that would lure agents into shell-browsing
    # ~/.config. Outside-home absolute paths are rare; still relativize when
    # under home, else omit (name is enough to re-fetch via get_profile).
    if profile.source_path is not None:
        if h is not None:
            rel = public_path_for_msg(h, profile.source_path)
            # Only keep when it actually became relative (no abs prefix leak).
            if rel and not Path(rel).is_absolute():
                out["source_path"] = rel
            else:
                # Fallback: logical profile path from name.
                out["source_path"] = f"profiles/{profile.name}.toml"
        else:
            out["source_path"] = f"profiles/{profile.name}.toml"
    return out


# Same-name notes write/append/prepend/rm serialize in-process. Different
# names (and different homes) use different locks. Read/stat do not take
# these locks: dest replace is already atomic.
# Re-entrant: a caller holds one lock across a precondition check plus the
# mutation it guards, and the mutation takes the same lock again.
_NOTES_LOCKS: dict[str, threading.RLock] = {}
_NOTES_LOCKS_GUARD = threading.Lock()


def _notes_name_lock(path: Path) -> threading.RLock:
    """Return the in-process lock for notes file *path*."""
    key = str(path)
    with _NOTES_LOCKS_GUARD:
        lk = _NOTES_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _NOTES_LOCKS[key] = lk
        return lk


def notes_name_lock(home: Path, name: str) -> threading.RLock:
    """Return the per-name lock serializing notes mutations for *name*.

    Callers hold it across a precondition check plus a notes mutation so the
    two cannot be split by a concurrent mutation of the same name (a profile
    delete, whose notes cascade runs under this lock, or another write).
    Re-entrant: the notes writers take the same lock, so holding it while
    calling them is safe. *name* is validated like :func:`notes_path`.
    """
    return _notes_name_lock(notes_path(home, name))


def notes_path(home: Path, name: str) -> Path:
    """Return ``{home}/notes/{name}.md`` after validating *name*.

    Validates against :data:`PROFILE_NAME_RE` *before* joining under
    ``notes/`` so values like ``../x`` cannot escape the notes directory.
    """
    if not name or not isinstance(name, str) or not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"notes name {name!r} must match {PROFILE_NAME_RE.pattern}"
        )
    home = Path(home).expanduser().resolve()
    return notes_dir(home) / f"{name}.md"


def _notes_missing(home: Path, name: str, path: Path) -> NotesNotFound:
    return NotesNotFound(
        f"notes not found: {name!r} ({public_path_for_msg(home, path)})"
    )


def _require_notes_text(content: object) -> str:
    """Return *content* as str, or raise if it is not a string (including None)."""
    if not isinstance(content, str):
        got = "None" if content is None else type(content).__name__
        raise ConfigInvalid(f"notes content must be a string, got {got}")
    return content


def _reject_notes_pem(content: str) -> None:
    if looks_like_pem_armor(content):
        raise ConfigInvalid("notes content must not be PEM armor")


def _reject_notes_too_large(text: str, limit: int) -> None:
    n = len(text)
    if n > limit:
        raise ConfigInvalid(
            f"notes content exceeds max_body_chars ({n} > {limit})"
        )


def _notes_max_body_chars(home: Path) -> int:
    return int(load_config(home).defaults.max_body_chars)


def _read_notes_file(path: Path) -> str:
    """Return UTF-8 text of an existing notes file, or empty if absent.

    Used by append/prepend (missing file means create). Does not raise
    ``NotesNotFound``. Decodes raw bytes so CR is not translated.
    """
    if not path.is_file():
        return ""
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def notes_present(home: Path, name: str) -> bool:
    """True when ``notes/{name}.md`` exists and has size greater than zero."""
    path = notes_path(home, name)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def write_notes(home: Path, name: str, content: str) -> Path:
    """Replace ``notes/{name}.md`` with *content* (atomic).

    Empty *content* is allowed and truncates to a zero-byte file.
    Concatenation is not performed; a second write replaces the first.
    """
    path = notes_path(home, name)
    text = _require_notes_text(content)
    _reject_notes_pem(text)
    home = Path(home).expanduser().resolve()
    with _notes_name_lock(path):
        ensure_home_layout(home)
        _reject_notes_too_large(text, _notes_max_body_chars(home))
        _atomic_write_bytes(notes_dir(home), path, text.encode("utf-8"))
    return path.resolve()


def append_notes(home: Path, name: str, content: str) -> Path:
    """Append *content* to ``notes/{name}.md`` (raw concat, then atomic replace).

    Empty *content* is rejected. A missing file is created with *content*.
    No newline is inserted between old text and *content*.
    """
    path = notes_path(home, name)
    fragment = _require_notes_text(content)
    if fragment == "":
        raise ConfigInvalid("notes append content must not be empty")
    _reject_notes_pem(fragment)
    home = Path(home).expanduser().resolve()
    with _notes_name_lock(path):
        ensure_home_layout(home)
        old = _read_notes_file(path)
        new = old + fragment
        _reject_notes_too_large(new, _notes_max_body_chars(home))
        _atomic_write_bytes(notes_dir(home), path, new.encode("utf-8"))
    return path.resolve()


def prepend_notes(home: Path, name: str, content: str) -> Path:
    """Prepend *content* to ``notes/{name}.md`` (raw concat, then atomic replace).

    Empty *content* is rejected. A missing file is created with *content*.
    No newline is inserted between *content* and old text.
    """
    path = notes_path(home, name)
    fragment = _require_notes_text(content)
    if fragment == "":
        raise ConfigInvalid("notes prepend content must not be empty")
    _reject_notes_pem(fragment)
    home = Path(home).expanduser().resolve()
    with _notes_name_lock(path):
        ensure_home_layout(home)
        old = _read_notes_file(path)
        new = fragment + old
        _reject_notes_too_large(new, _notes_max_body_chars(home))
        _atomic_write_bytes(notes_dir(home), path, new.encode("utf-8"))
    return path.resolve()


def read_notes(home: Path, name: str) -> str:
    """Return the UTF-8 body of ``notes/{name}.md``.

    Decodes raw file bytes (no universal-newline translation). Raises
    :class:`NotesNotFound` when the file is absent. An empty file returns
    an empty string.
    """
    path = notes_path(home, name)
    home = Path(home).expanduser().resolve()
    if not path.is_file():
        raise _notes_missing(home, name, path)
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise _notes_missing(home, name, path) from None


def stat_notes(home: Path, name: str) -> dict[str, Any]:
    """Return ``bytes`` / ``mtime`` for ``notes/{name}.md`` (no body).

    Raises :class:`NotesNotFound` when the file is absent.
    """
    path = notes_path(home, name)
    home = Path(home).expanduser().resolve()
    try:
        st = path.stat()
    except FileNotFoundError:
        raise _notes_missing(home, name, path) from None
    if not path.is_file():
        raise _notes_missing(home, name, path)
    return {"bytes": st.st_size, "mtime": st.st_mtime}


def delete_notes(home: Path, name: str) -> Path:
    """Remove ``notes/{name}.md``.

    Raises :class:`NotesNotFound` when the file is absent. Serializes with
    same-name write/append/prepend.
    """
    path = notes_path(home, name)
    home = Path(home).expanduser().resolve()
    with _notes_name_lock(path):
        if not path.is_file():
            raise _notes_missing(home, name, path)
        try:
            path.unlink()
        except FileNotFoundError:
            raise _notes_missing(home, name, path) from None
    return path
