"""Config Core ops for agent self-configuration (MCP/CLI tool ``config``).

Agents can manage the config home without hand-edited TOML or shell access:
ensure layout, put/get/delete profiles, put/list secrets, and read/write
per-profile host notes. Secret *bodies* are written via ``put_secret`` and
never returned by any read API. Notes bodies are returned only by
``op=notes action=read`` - list/get_profile expose a ``notes=1`` flag.

Agent-facing fields prefer logical relative names (``profiles/...``,
``secrets/...``, ``notes/...``) over absolute filesystem paths so hosts do not
steer agents into browsing ``~/.config`` with shell tools.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from mcp_remote_control.config import (
    ConfigInvalid,
    NotesNotFound,
    ProfileInvalid,
    ProfileNotFound,
    list_profiles,
    load_config,
    load_profile,
    notes_present,
    profiles_dir,
    resolve_home,
)
from mcp_remote_control.config.errors import (
    public_path_for_msg,
    reject_dual_password_sources,
)
from mcp_remote_control.config.load import (
    auth_path_escapes_home,
    looks_like_pem_body_in_path,
)
from mcp_remote_control.config.models import PROFILE_NAME_RE
from mcp_remote_control.config.store import (
    append_notes,
    delete_notes,
    delete_profile,
    ensure_home_layout,
    list_secret_names,
    notes_name_lock,
    notes_path,
    prepend_notes,
    profile_public_dict,
    put_profile,
    put_secret,
    read_notes,
    stat_notes,
    write_notes,
)
from mcp_remote_control.core.result import OpResult

VALID_OPS: frozenset[str] = frozenset(
    {
        "home",
        "ensure_home",
        "list_profiles",
        "get_profile",
        "put_profile",
        "delete_profile",
        "put_secret",
        "list_secrets",
        "get",
        "help",
        "notes",
    }
)

# Agent-facing directory enumeration (home / ensure_home). Notes live beside
# profiles, not under state/.
_LAYOUT_DIRS = "profiles,secrets,notes,logs,state"

_NOTES_ACTIONS: frozenset[str] = frozenset(
    {"read", "write", "append", "prepend", "stat", "rm"}
)
_NOTES_WRITE_ACTIONS: frozenset[str] = frozenset({"write", "append", "prepend"})
_NOTES_ACTION_HINT = "action=read|write|append|prepend|stat|rm name=<profile>"

# Shown on help / error hints - keep in sync with store auth + load methods.
# Preferred bootstrap is inline password; put_secret is optional (keys / certs).
_AUTH_RECIPES = (
    "SSH password (preferred \u2014 password is NOT private in this product): "
    'put_profile name=<ep> transport=ssh host=\u2026 username=\u2026 '
    'auth={"method":"password","password":"<plain>"} '
    'ssh={"known_hosts":"none"}  # lab: skip host key file'
    "\n"
    "SSH password_env (password from process env; choose one of password|"
    "password_path|password_env): "
    'put_profile \u2026 auth={"method":"password","password_env":"MRC_PASSWORD"} '
    'ssh={"known_hosts":"none"}'
    "\n"
    "SSH key (optional put_secret for key files): put_secret name=<id> content=<pem>; "
    'put_profile \u2026 auth={"method":"private_key_path","key_path":"secrets/<id>"} '
    'ssh={"known_hosts":"none"}'
    "\n"
    "SSH agent (ssh_agent \u2014 keys already loaded in agent; no password/key path): "
    'put_profile \u2026 auth={"method":"ssh_agent"} '
    'ssh={"known_hosts":"none"}'
    "\n"
    "WinRM password: "
    'put_profile name=<ep> transport=winrm host=\u2026 username=\u2026 '
    'auth={"method":"password","password":"<plain>"} '
    'winrm={"scheme":"http","auth":"ntlm"}'
    "\n"
    "WinRM CredSSP (credssp; prefer plain password; https recommended): "
    'put_profile \u2026 transport=winrm auth={"method":"credssp","password":"<plain>"} '
    'winrm={"scheme":"https","auth":"credssp"}  # optional: '
    'winrm={"scheme":"https","credssp":{"auth_mechanism":"ntlm"}}'
    "\n"
    "WinRM certificate (client cert; paths only, put_secret for PEM files): "
    "put_secret name=client-cert content=<cert.pem>; "
    "put_secret name=client-key content=<key.pem>; "
    'put_profile \u2026 transport=winrm auth={"method":"certificate",'
    '"cert_path":"secrets/client-cert","cert_key_path":"secrets/client-key"} '
    'winrm={"scheme":"https"}  # scheme=https required for certificate'
    "\n"
    "Optional file-based password (compat only, choose one source): "
    "put_secret then password_path=secrets/<id> \u2014 do not also set plain password. "
    "Do not shell-edit profiles \u2014 use this tool only."
    "\n"
    "Host notes (optional; write/append/prepend require an existing profile): "
    "notes action=read|write|append|prepend|stat|rm name=<ep> [content=\u2026]. "
    "Body only on action=read; list_profiles/get_profile show notes=1 when "
    "non-empty. Empty write truncates (clears the flag). Do not use fs on "
    "the config home."
)


def _home(home: Path | str | None) -> Path:
    """Resolve config root to an absolute path (same rules as ``resolve_home``)."""
    if home is None:
        return resolve_home()
    # Treat explicit home= like MRC_HOME: expand ~ / $HOME; reject placeholders.
    return resolve_home(env={"MRC_HOME": str(home)})


def _rel_under_home(home: Path, path: Path | str) -> str:
    """Return path relative to *home* when possible (agent-facing).

    Under home -> ``profiles/...`` / ``secrets/...``. Outside-home absolute paths
    are kept absolute (do not collapse to basename only).
    Single rule: :func:`public_path_for_msg`.
    """
    return public_path_for_msg(Path(home), Path(path))


def _auth_path_is_rooted(raw: str) -> bool:
    """True when *raw* names a location the caller chose, not a bare secret id.

    Uses the same expansion as :func:`resolve_under_home` (``$VAR`` then ``~``)
    so "rooted" means exactly "will not resolve under MRC_HOME". ``/abs``,
    ``~/...`` and ``$VAR/...`` therefore all count; ``secrets/<name>`` and
    ``<name>`` do not.
    """
    try:
        return Path(os.path.expanduser(os.path.expandvars(raw))).is_absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def _auth_path_value(raw: str) -> str:
    """Return the stored form of an auth path / secret-id value.

    Caller-rooted values and explicit ``secrets/...`` references are kept
    verbatim - re-rooting them under ``secrets/`` would silently point the
    profile at a path the caller never supplied (README: ``key_path`` is
    "relative to MRC_HOME or absolute"). Only a bare relative id is expanded
    to the documented ``secrets/<id>`` shorthand.
    """
    if raw.startswith("secrets/") or _auth_path_is_rooted(raw):
        return raw
    return f"secrets/{raw}"


def _reject_unusable_auth_path(field: str, raw: str) -> None:
    """Reject a secret body or a home-escaping traversal in an auth path field.

    Runs before any rewriting so the guards below see what the caller sent.
    A PEM body is never a path (``put_secret`` owns body writes) - the test
    looks past the ``secrets/`` shorthand and a BOM, which is how a body would
    otherwise reach storage. A *relative* path with a ``..`` component climbs
    out of the config home, the same policy ``put_secret`` applies to secret
    names; absolute paths are a caller-chosen location and stay allowed.
    """
    if looks_like_pem_body_in_path(raw):
        raise ProfileInvalid(
            f"[auth].{field} must be a filesystem path, not a PEM body; "
            f"store the key with put_secret and reference it as secrets/<name>"
        )
    if auth_path_escapes_home(raw):
        raise ProfileInvalid(
            f"[auth].{field} must not contain '..' path segments; use an "
            f"absolute path or store the file under the config home "
            f"(secrets/<name>) instead"
        )


def _normalize_auth(auth: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize agent auth dict for put_profile.

    Preferred password form (no privacy):
      ``{"method":"password","password":"<plain>"}``

    File-based aliases still work:
      ``password_secret`` / ``password_name`` -> password_path=secrets/<id>
      ``key_secret`` -> key_path=secrets/<id>

    Path-valued fields keep the caller's own location: an absolute / ``~`` /
    ``$VAR`` value is stored verbatim, and only a bare relative id is expanded
    to the ``secrets/<id>`` shorthand. A PEM body, or a relative value whose
    ``..`` / ``$VAR`` expansion climbs out of the config home, is rejected here,
    before rewriting - see :func:`_reject_unusable_auth_path`. A secret-id alias
    is only validated when its value is the one that will be stored: an alias
    the canonical field overrides is discarded, so it cannot fail the call.

    Empty / whitespace-only ``password`` is treated as unset. Dual password
    sources (plain + path, plain + env, path + env) are hard-rejected with a
    choose-one hint - no silent priority.
    """
    if not auth:
        return None
    a: dict[str, Any] = {str(k): v for k, v in auth.items()}

    # Blank / whitespace-only plain password -> unset (no material claim).
    # Non-string password is rejected here and again in store._clean_auth_table
    # (body= path only hits store) so put_profile never succeeds without
    # material after a silent type drop.
    if "password" in a:
        raw_pw = a.get("password")
        if raw_pw is None or (isinstance(raw_pw, str) and not raw_pw.strip()):
            a.pop("password", None)
        elif not isinstance(raw_pw, str):
            raise ProfileInvalid(
                f"[auth].password must be a string, got {type(raw_pw).__name__}"
            )

    for alias in ("password_secret", "password_name"):
        if alias in a and a[alias] is not None and str(a[alias]).strip():
            raw = str(a.pop(alias)).strip()
            # An already-present canonical field wins (``setdefault``): the
            # alias value never reaches the profile, so it must not be
            # validated - only the value that will be stored can fail the call.
            if "password_path" not in a:
                _reject_unusable_auth_path(alias, raw)
                a["password_path"] = _auth_path_value(raw)
            if not a.get("method"):
                a["method"] = "password"
            break

    for alias in ("key_secret", "key_name", "private_key_secret"):
        if alias in a and a[alias] is not None and str(a[alias]).strip():
            raw = str(a.pop(alias)).strip()
            if "key_path" not in a:
                _reject_unusable_auth_path(alias, raw)
                a["key_path"] = _auth_path_value(raw)
            if not a.get("method"):
                a["method"] = "private_key_path"
            break

    for path_key, method in (
        ("password_path", "password"),
        ("key_path", "private_key_path"),
        ("passphrase_path", None),
    ):
        if path_key in a and a[path_key] is not None and str(a[path_key]).strip():
            raw = str(a[path_key]).strip()
            _reject_unusable_auth_path(path_key, raw)
            a[path_key] = _auth_path_value(raw)
            if method and not a.get("method"):
                a["method"] = method

    # Whitespace-only password_env -> drop.
    if "password_env" in a:
        pe = a.get("password_env")
        if pe is None or (isinstance(pe, str) and not pe.strip()):
            a.pop("password_env", None)

    # Infer method=password when only plain password remains.
    if a.get("password") is not None and str(a.get("password")).strip() != "":
        if not a.get("method"):
            a["method"] = "password"

    # Dual sources: hard fail with choose-one (before write / silent priority).
    # Shared helper with config load / store.
    reject_dual_password_sources(
        password=a.get("password"),
        password_path=a.get("password_path"),
        password_env=a.get("password_env"),
    )

    return a


def _notes_flag(home: Path, name: str) -> bool:
    """True when ``notes/{name}.md`` exists and size > 0.

    Invalid names (listed as profile stems but not ``PROFILE_NAME_RE``) must
    not raise - listing still has to return the other rows.
    """
    try:
        return notes_present(home, name)
    except (ProfileInvalid, OSError):
        return False


def _profile_toml_exists(home: Path, name: str) -> bool:
    """True when ``profiles/{name}.toml`` is a regular file."""
    return (profiles_dir(home) / f"{name}.toml").is_file()


def _notes_error(
    code: str,
    msg: str,
    *,
    action: str | None = None,
    name: str | None = None,
    hint: str | None = None,
) -> OpResult:
    fields: dict[str, Any] = {"op": "notes", "msg": msg}
    if action:
        fields["action"] = action
    if name:
        fields["name"] = name
    return OpResult(
        kind="config",
        status="error",
        code=code,
        fields=fields,
        hint=hint,
    )


def _map_notes_config_invalid(
    exc: ConfigInvalid, *, action: str, name: str
) -> OpResult:
    msg = str(exc)
    code = "NOTES_TOO_LARGE" if "max_body_chars" in msg else "INVALID_ARG"
    return _notes_error(code, msg, action=action, name=name)


def _truncate_notes_read_body(text: str, max_chars: int) -> tuple[str, bool]:
    """Keep head+tail of *text* when it exceeds *max_chars* (read path only).

    Inserts a one-line middle-omission marker. Does not modify disk; the
    caller must still report ``bytes=`` as the original UTF-8 length.
    """
    total = len(text)
    if total <= max_chars:
        return text, False
    keep = min(max(int(max_chars), 0), total - 1)
    kept_head = keep // 2
    kept_tail = keep - kept_head
    while True:
        head = text[:kept_head] if kept_head else ""
        tail = text[-kept_tail:] if kept_tail else ""
        marker = (
            f"\u2026(truncated middle: kept_head={kept_head} kept_tail={kept_tail} "
            f"total={total} chars)\u2026"
        )
        parts: list[str] = []
        if head:
            parts.append(head)
        parts.append(marker)
        if tail:
            parts.append(tail)
        body = "\n".join(parts)
        if len(body) < total or (kept_head == 0 and kept_tail == 0):
            return body, True
        if kept_head >= kept_tail and kept_head:
            kept_head -= 1
        elif kept_tail:
            kept_tail -= 1
        else:
            return body, True


def _notes_write_locked(
    home: Path,
    name: str,
    action: str,
    writer: Callable[[Path, str, str], Path],
    content: str,
) -> OpResult:
    """Run a notes write action with its profile precondition under the lock.

    "write/append/prepend require an existing profile" is a precondition on
    the mutation, so the existence check and the mutation are one step: with a
    gap between them, a profile delete can complete in between and the writer
    then re-creates notes for a profile that is gone - a state no serial order
    of the two calls can produce. ``op_delete_profile`` runs its profile +
    notes cascade under the same per-name lock, so a delete either completes
    before this check (-> PROFILE_NOT_FOUND) or starts after the mutation (->
    its cascade removes the notes it finds). The lock is re-entrant: *writer*
    takes it again.
    """
    with notes_name_lock(home, name):
        if not _profile_toml_exists(home, name):
            loc = public_path_for_msg(home, profiles_dir(home) / f"{name}.toml")
            return _notes_error(
                "PROFILE_NOT_FOUND",
                f"profile not found: {name!r} ({loc})",
                action=action,
                name=name,
                hint="put_profile first; notes cannot exist without a profile",
            )
        path = writer(home, name, content)
        info = stat_notes(home, name)
        rel = public_path_for_msg(home, path)
        nbytes = int(info["bytes"])
        fields: dict[str, Any] = {
            "op": "notes",
            "action": action,
            "name": name,
            "path": rel,
            "bytes": nbytes,
        }
        if nbytes > 0:
            fields["notes"] = 1
        return OpResult(
            kind="config",
            status="ok",
            fields=fields,
            body=f"name={name} path={rel} bytes={nbytes}",
        )


def op_home(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "home",
            "ready": 1 if h.is_dir() else 0,
            "exists": 1 if h.is_dir() else 0,
            "layout": _LAYOUT_DIRS,
        },
        body=(
            f"ready={1 if h.is_dir() else 0}\n"
            "use ensure_home if ready=0; do not shell-browse the config tree"
        ),
        hint=(
            "ensure_home \u2192 put_profile auth="
            '{"method":"password","password":"\u2026"} '
            'ssh={"known_hosts":"none"} \u2192 endpoint open; '
            "put_secret optional (keys/compat)"
        ),
    )


def op_ensure_home(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    paths = ensure_home_layout(h)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "ensure_home",
            "ready": 1,
            "n": len(paths),
            "dirs": _LAYOUT_DIRS,
        },
        body=f"ready=1 dirs={_LAYOUT_DIRS} config=config.toml",
        hint=(
            "next: put_profile \u2026 auth="
            '{"method":"password","password":"<plain>"} '
            'ssh={"known_hosts":"none"}; then endpoint open profile=<name>. '
            "put_secret optional (SSH keys / password_path compat)"
        ),
    )


def op_list_profiles(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    names = list_profiles(h)
    lines = []
    for name in names:
        flag = " notes=1" if _notes_flag(h, name) else ""
        try:
            p = load_profile(h, name)
            host = p.host or "-"
            lines.append(f"name={name} transport={p.transport} host={host}{flag}")
        except Exception:  # noqa: BLE001
            lines.append(f"name={name} invalid=1{flag}")
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "list_profiles",
            "n": len(names),
            "names": ",".join(names) if names else None,
        },
        body="\n".join(lines) if lines else None,
        hint="empty list is ok \u2014 put_profile to add; get_profile name= for details",
    )


def op_get_profile(
    *,
    name: str | None = None,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    if not name or not str(name).strip():
        return OpResult(
            kind="config",
            status="error",
            code="MISSING_ARG",
            fields={"op": "get_profile", "msg": "name required"},
        )
    h = _home(home)
    try:
        p = load_profile(h, str(name).strip())
    except ProfileNotFound as exc:
        return OpResult(
            kind="config",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "get_profile", "name": name, "msg": str(exc)},
        )
    except ProfileInvalid as exc:
        return OpResult(
            kind="config",
            status="error",
            code="PROFILE_INVALID",
            fields={"op": "get_profile", "name": name, "msg": str(exc)},
        )
    # Pass home so auth/source paths stay relative (secrets/..., profiles/...).
    pub = profile_public_dict(p, home=h)
    # Compact key lines only; profile_public_dict already omits secret bodies
    # and avoids absolute /Users|/home config-home prefixes for Agent UX.
    # Notes body is never inlined here - only a presence flag.
    body_lines = [f"{k}={v}" for k, v in pub.items() if not isinstance(v, dict)]
    for k, v in pub.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                body_lines.append(f"{k}.{sk}={sv}")
    fields: dict[str, Any] = {
        "op": "get_profile",
        "name": p.name,
        "transport": p.transport,
        "profile": pub,
    }
    if _notes_flag(h, p.name):
        fields["notes"] = 1
        body_lines.append("notes=1")
    return OpResult(
        kind="config",
        status="ok",
        fields=fields,
        body="\n".join(body_lines),
    )


def op_put_profile(
    *,
    name: str | None = None,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    label: str | None = None,
    auth: dict[str, Any] | str | None = None,
    ssh: dict[str, Any] | str | None = None,
    winrm: dict[str, Any] | str | None = None,
    defaults: dict[str, Any] | str | None = None,
    caps: dict[str, Any] | str | None = None,
    body: str | None = None,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    if not name or not str(name).strip():
        return OpResult(
            kind="config",
            status="error",
            code="MISSING_ARG",
            fields={"op": "put_profile", "msg": "name required"},
            hint="put_profile name=lab transport=ssh host=\u2026 username=\u2026 auth={\u2026}",
        )

    def _obj(v: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v.strip():
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ConfigInvalid(f"invalid JSON object: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ConfigInvalid("JSON value must be an object")
            return parsed
        return None

    h = _home(home)
    try:
        auth_norm = _normalize_auth(_obj(auth))
        path = put_profile(
            h,
            name=str(name).strip(),
            transport=transport,
            host=host,
            port=int(port) if port is not None else None,
            username=username,
            label=label,
            auth=auth_norm,
            ssh=_obj(ssh),
            winrm=_obj(winrm),
            defaults=_obj(defaults),
            caps=_obj(caps),
            body=body,
        )
    except (ProfileInvalid, ConfigInvalid) as exc:
        return OpResult(
            kind="config",
            status="error",
            code="PROFILE_INVALID",
            fields={"op": "put_profile", "name": name, "msg": str(exc)},
            hint=_AUTH_RECIPES.split("\n", 1)[0],
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={
                "op": "put_profile",
                "name": name,
                "msg": f"{type(exc).__name__}: {exc}",
            },
        )
    rel = _rel_under_home(h, path)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "put_profile",
            "name": str(name).strip(),
            "path": rel,
            "transport": transport,
        },
        body=f"name={name} path={rel}",
        hint="endpoint open profile=<name> to connect (no shell/TOML edit needed)",
    )


def op_delete_profile(
    *,
    name: str | None = None,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    if not name or not str(name).strip():
        return OpResult(
            kind="config",
            status="error",
            code="MISSING_ARG",
            fields={"op": "delete_profile", "msg": "name required"},
        )
    h = _home(home)
    nm = str(name).strip()
    if not PROFILE_NAME_RE.fullmatch(nm):
        # Reject before the per-name lock is resolved: the lock is keyed by
        # the notes path, whose validation would report the notes namespace
        # for a delete_profile call.
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={
                "op": "delete_profile",
                "msg": f"profile name {nm!r} must match {PROFILE_NAME_RE.pattern}",
            },
        )
    notes_cleanup_error: OSError | None = None
    try:
        # Profile + notes cascade under the per-name notes lock: a concurrent
        # write/append/prepend must not pass its profile-existence check
        # between the two steps and leave notes behind without a profile.
        with notes_name_lock(h, nm):
            path = delete_profile(h, nm)
            # Notes share the profile name identity: remove the file when
            # present. Missing notes is not an error (the profile delete
            # already succeeded).
            try:
                delete_notes(h, nm)
            except NotesNotFound:
                pass
            except OSError as exc:
                # The profile is already gone, so this cannot be undone here;
                # report the failed cleanup instead of letting it escape Core.
                notes_cleanup_error = exc
    except ProfileNotFound as exc:
        return OpResult(
            kind="config",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "delete_profile", "name": name, "msg": str(exc)},
        )
    except FileNotFoundError:
        # store.delete_profile raises ProfileNotFound; map raw FileNotFoundError
        # the same way if a lower layer surfaces it.
        return OpResult(
            kind="config",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "delete_profile", "name": name, "msg": "profile file missing"},
        )
    except OSError as exc:
        # A failed profile unlink must not leak the absolute config path: the
        # message carries the home-relative profile path and the errno text
        # only. The profile may still be on disk, so nothing here claims the
        # delete happened or promises a rollback.
        profile_rel = public_path_for_msg(h, profiles_dir(h) / f"{nm}.toml")
        detail = exc.strerror or type(exc).__name__
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={
                "op": "delete_profile",
                "name": nm,
                "msg": f"profile {nm!r} deletion failed: {profile_rel}: {detail}",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={"op": "delete_profile", "msg": str(exc)},
        )
    if notes_cleanup_error is not None:
        profile_rel = public_path_for_msg(h, profiles_dir(h) / f"{nm}.toml")
        notes_rel = public_path_for_msg(h, notes_path(h, nm))
        detail = notes_cleanup_error.strerror or type(notes_cleanup_error).__name__
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={
                "op": "delete_profile",
                "name": nm,
                "msg": (
                    f"profile {nm!r} deleted ({profile_rel}); notes cleanup "
                    f"failed: {notes_rel}: {detail}"
                ),
                "profile_deleted": 1,
            },
            hint=(
                f"the profile is gone; notes may still exist at {notes_rel} \u2014 "
                f"fix the notes directory permissions and remove it explicitly"
            ),
        )
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "delete_profile",
            "name": name,
            "path": _rel_under_home(h, path),
            "deleted": 1,
        },
    )


def op_put_secret(
    *,
    name: str | None = None,
    content: str | None = None,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Write a secret file; response contains path and size only (never content)."""
    if not name or content is None:
        return OpResult(
            kind="config",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "put_secret",
                "msg": "name and content required",
            },
            hint=(
                "put_secret is optional (keys / password_path compat): "
                "name=<id> content=<body>; then put_profile key_path or "
                "password_path=secrets/<id>. Preferred password bootstrap is "
                'inline auth={"method":"password","password":"<plain>"} \u2014 '
                "see config op=help"
            ),
        )
    h = _home(home)
    try:
        path = put_secret(h, name=str(name).strip(), content=content)
    except ConfigInvalid as exc:
        return OpResult(
            kind="config",
            status="error",
            code="INVALID_ARG",
            fields={"op": "put_secret", "msg": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={"op": "put_secret", "msg": f"{type(exc).__name__}: {exc}"},
        )
    rel = public_path_for_msg(h, path)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "put_secret",
            "name": str(name).strip(),
            "path": rel,
            "bytes": len(content.encode("utf-8")),
        },
        body=f"name={name} path={rel} bytes={len(content.encode('utf-8'))}",
        hint=(
            f'for keys: put_profile auth={{"method":"private_key_path",'
            f'"key_path":"{rel}"}}; for file password (compat, not dual with '
            f'plain): password_path="{rel}" or password_secret="{name}". '
            "Prefer inline password when possible \u2014 config op=help"
        ),
    )


def op_list_secrets(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    names = list_secret_names(h)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "list_secrets",
            "n": len(names),
            "names": ",".join(names) if names else None,
        },
        body="\n".join(f"name={n}" for n in names) if names else None,
        hint="bodies are never returned; only filenames under secrets/",
    )


def op_get(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    """Summary of global config + layout (no secrets, no absolute paths)."""
    h = _home(home)
    cfg = load_config(h)
    profiles = list_profiles(h)
    secrets = list_secret_names(h)
    lines = [
        f"ready={1 if h.is_dir() else 0}",
        f"config_from_defaults={1 if cfg.from_defaults else 0}",
        f"verbosity={cfg.defaults.verbosity}",
        f"profiles_n={len(profiles)}",
        f"secrets_n={len(secrets)}",
    ]
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "get",
            "ready": 1 if h.is_dir() else 0,
            "from_defaults": 1 if cfg.from_defaults else 0,
            "profiles_n": len(profiles),
            "secrets_n": len(secrets),
        },
        body="\n".join(lines),
        hint="list_profiles / list_secrets / get_profile for details; op=help for recipes",
    )


def op_notes(
    *,
    action: str | None = None,
    name: str | None = None,
    content: str | None = None,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Read or edit ``notes/{name}.md``. Body is returned only for ``read``."""
    if not action or not str(action).strip():
        return _notes_error(
            "MISSING_ARG",
            "action required",
            name=name,
            hint=_NOTES_ACTION_HINT,
        )
    act = str(action).strip().lower()
    if act not in _NOTES_ACTIONS:
        return _notes_error(
            "INVALID_ARG",
            "unknown notes action (want read|write|append|prepend|stat|rm)",
            action=act,
            name=name,
            hint=_NOTES_ACTION_HINT,
        )
    if not name or not str(name).strip():
        return _notes_error(
            "MISSING_ARG",
            "name required",
            action=act,
            hint=_NOTES_ACTION_HINT,
        )
    nm = str(name).strip()
    h = _home(home)
    try:
        notes_path(h, nm)
    except ProfileInvalid as exc:
        return _notes_error(
            "PROFILE_INVALID",
            str(exc),
            action=act,
            name=nm,
        )

    if act in _NOTES_WRITE_ACTIONS:
        if content is None:
            return _notes_error(
                "MISSING_ARG",
                "content required",
                action=act,
                name=nm,
                hint="write content=\"\" truncates; append/prepend reject empty",
            )

    writers = {
        "write": write_notes,
        "append": append_notes,
        "prepend": prepend_notes,
    }
    try:
        if act in writers:
            # content is str here: write actions returned earlier when None.
            return _notes_write_locked(
                h, nm, act, writers[act], content if content is not None else ""
            )
        if act == "read":
            text = read_notes(h, nm)
            nbytes = len(text.encode("utf-8"))
            max_chars = int(load_config(h).defaults.max_body_chars)
            body_text, truncated = _truncate_notes_read_body(text, max_chars)
            fields = {
                "op": "notes",
                "action": "read",
                "name": nm,
                "bytes": nbytes,
            }
            if nbytes > 0:
                fields["notes"] = 1
            if truncated:
                fields["truncated"] = 1
            # Empty file: ok, no body line (presence flag stays off).
            return OpResult(
                kind="config",
                status="ok",
                fields=fields,
                body=body_text if body_text else None,
            )
        if act == "stat":
            info = stat_notes(h, nm)
            nbytes = int(info["bytes"])
            fields = {
                "op": "notes",
                "action": "stat",
                "name": nm,
                "bytes": nbytes,
            }
            if nbytes > 0:
                fields["notes"] = 1
            return OpResult(
                kind="config",
                status="ok",
                fields=fields,
            )
        # act == "rm"
        path = delete_notes(h, nm)
        rel = public_path_for_msg(h, path)
        return OpResult(
            kind="config",
            status="ok",
            fields={
                "op": "notes",
                "action": "rm",
                "name": nm,
                "path": rel,
                "deleted": 1,
            },
        )
    except NotesNotFound as exc:
        return _notes_error(
            "NOTES_NOT_FOUND",
            str(exc),
            action=act,
            name=nm,
        )
    except ProfileInvalid as exc:
        return _notes_error(
            "PROFILE_INVALID",
            str(exc),
            action=act,
            name=nm,
        )
    except ConfigInvalid as exc:
        return _map_notes_config_invalid(exc, action=act, name=nm)
    except UnicodeDecodeError as exc:
        # The stored bytes are not UTF-8. Nothing was written: the write
        # actions read the existing body before they touch disk, so this must
        # not surface as a write failure - the file is left unchanged.
        loc = public_path_for_msg(h, notes_path(h, nm))
        return _notes_error(
            "NOTES_ENCODING_INVALID",
            f"{loc} is not valid UTF-8 ({exc}); file left unchanged",
            action=act,
            name=nm,
            hint=(
                "re-encode the notes file as UTF-8, or overwrite it with "
                "action=write"
            ),
        )
    except OSError as exc:
        # The file or its directory cannot be reached (EACCES, EISDIR,
        # ENOTDIR, ...). A read-only action performed no write, so it must not
        # report CONFIG_WRITE_FAILED; the message names the file relative to
        # the config home rather than echoing an absolute host path.
        loc = public_path_for_msg(h, notes_path(h, nm))
        detail = exc.strerror or type(exc).__name__
        if act in _NOTES_WRITE_ACTIONS:
            return _notes_error(
                "CONFIG_WRITE_FAILED",
                f"{loc}: {detail}",
                action=act,
                name=nm,
            )
        return _notes_error(
            "NOTES_UNREADABLE",
            f"{loc}: {detail}",
            action=act,
            name=nm,
            hint="check file and directory permissions under $MRC_HOME/notes",
        )
    except Exception as exc:  # noqa: BLE001
        # Last resort: an unexpected failure still gets a structured code.
        # Any absolute path under the config home is rewritten to the
        # relative form so the message cannot steer an agent into
        # shell-browsing the host config tree.
        detail = str(exc).replace(f"{h}{os.sep}", "").replace(str(h), ".")
        return _notes_error(
            "CONFIG_WRITE_FAILED",
            f"{type(exc).__name__}: {detail}",
            action=act,
            name=nm,
        )


def op_help(**_kwargs: Any) -> OpResult:
    """Auth / bootstrap recipes for agents (no filesystem access required)."""
    return OpResult(
        kind="config",
        status="ok",
        fields={"op": "help"},
        body=_AUTH_RECIPES,
        hint="use these recipes via config tool only \u2014 never cat/edit profile TOML",
    )


def run(op: str, **kwargs: Any) -> OpResult:
    op_norm = (op or "").strip().lower().replace("-", "_")
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="config",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": (
                    "unknown config op (want home|ensure_home|help|list_profiles|"
                    "get_profile|put_profile|delete_profile|put_secret|"
                    "list_secrets|get|notes)"
                ),
            },
            hint=(
                "ensure_home \u2192 put_profile (password plain) \u2192 endpoint open; "
                "put_secret optional (keys); config op=help for auth recipes"
            ),
        )
    dispatch = {
        "home": op_home,
        "ensure_home": op_ensure_home,
        "list_profiles": op_list_profiles,
        "get_profile": op_get_profile,
        "put_profile": op_put_profile,
        "delete_profile": op_delete_profile,
        "put_secret": op_put_secret,
        "list_secrets": op_list_secrets,
        "get": op_get,
        "help": op_help,
        "notes": op_notes,
    }
    return dispatch[op_norm](**kwargs)
