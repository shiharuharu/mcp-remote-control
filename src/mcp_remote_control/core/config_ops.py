"""Config Core ops for agent self-configuration (MCP/CLI tool ``config``).

Agents can manage ``MRC_HOME`` without hand-edited TOML: ensure layout,
put/get/delete profiles, and put/list secrets. Secret *bodies* are written
via ``put_secret`` and never returned by any read API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_remote_control.config import (
    ConfigInvalid,
    ProfileInvalid,
    ProfileNotFound,
    list_profiles,
    load_config,
    load_profile,
    resolve_home,
)
from mcp_remote_control.config.paths import config_toml_path, profiles_dir, secrets_dir
from mcp_remote_control.config.store import (
    delete_profile,
    ensure_home_layout,
    list_secret_names,
    profile_public_dict,
    put_profile,
    put_secret,
    secret_rel_path,
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
    }
)


def _home(home: Path | str | None) -> Path:
    """Resolve config root to an absolute path (same rules as ``resolve_home``)."""
    if home is None:
        return resolve_home()
    # Treat explicit home= like MRC_HOME: expand ~ / $HOME; reject placeholders.
    return resolve_home(env={"MRC_HOME": str(home)})


def op_home(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "home",
            "home": str(h),
            "profiles": str(profiles_dir(h)),
            "secrets": str(secrets_dir(h)),
            "config": str(config_toml_path(h)),
            "exists": 1 if h.is_dir() else 0,
        },
        body=f"home={h}",
        hint="set env MRC_HOME to override; config ensure_home creates layout",
    )


def op_ensure_home(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    paths = ensure_home_layout(h)
    return OpResult(
        kind="config",
        status="ok",
        fields={"op": "ensure_home", "home": paths["home"], "n": len(paths)},
        body="\n".join(f"{k}={v}" for k, v in sorted(paths.items())),
        hint="next: put_secret + put_profile, then endpoint open profile=",
    )


def op_list_profiles(*, home: Path | str | None = None, **_kwargs: Any) -> OpResult:
    h = _home(home)
    names = list_profiles(h)
    lines = []
    for name in names:
        try:
            p = load_profile(h, name)
            host = p.host or "-"
            lines.append(f"name={name} transport={p.transport} host={host}")
        except Exception:  # noqa: BLE001
            lines.append(f"name={name} invalid=1")
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "list_profiles",
            "home": str(h),
            "n": len(names),
            "names": ",".join(names) if names else None,
        },
        body="\n".join(lines) if lines else None,
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
    pub = profile_public_dict(p)
    # Compact key lines only; profile_public_dict already omits secret bodies.
    body_lines = [f"{k}={v}" for k, v in pub.items() if not isinstance(v, dict)]
    for k, v in pub.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                body_lines.append(f"{k}.{sk}={sv}")
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "get_profile",
            "name": p.name,
            "transport": p.transport,
            "profile": pub,
        },
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
            hint="put_profile name=lab transport=ssh host=… username=… auth={…}",
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
        path = put_profile(
            h,
            name=str(name).strip(),
            transport=transport,
            host=host,
            port=int(port) if port is not None else None,
            username=username,
            label=label,
            auth=_obj(auth),
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
    return OpResult(
        kind="config",
        status="ok",
        fields={
            "op": "put_profile",
            "name": str(name).strip(),
            "path": str(path),
            "transport": transport,
        },
        body=f"name={name} path={path}",
        hint="endpoint open profile=<name> to connect",
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
    try:
        path = delete_profile(h, str(name).strip())
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
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="config",
            status="error",
            code="CONFIG_WRITE_FAILED",
            fields={"op": "delete_profile", "msg": str(exc)},
        )
    return OpResult(
        kind="config",
        status="ok",
        fields={"op": "delete_profile", "name": name, "path": str(path), "deleted": 1},
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
            hint="put_secret name=id_ed25519 content=<pem>; then auth.key_path=secrets/id_ed25519",
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
    rel = secret_rel_path(h, path)
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
        hint="reference path in profile auth.key_path or auth.password_path",
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
    """Summary of global config + layout (no secrets)."""
    h = _home(home)
    cfg = load_config(h)
    profiles = list_profiles(h)
    secrets = list_secret_names(h)
    lines = [
        f"home={h}",
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
            "home": str(h),
            "from_defaults": 1 if cfg.from_defaults else 0,
            "profiles_n": len(profiles),
            "secrets_n": len(secrets),
        },
        body="\n".join(lines),
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
                    "unknown config op (want home|ensure_home|list_profiles|"
                    "get_profile|put_profile|delete_profile|put_secret|"
                    "list_secrets|get)"
                ),
            },
            hint=(
                "agent self-config: ensure_home → put_secret → put_profile → "
                "endpoint open profile="
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
    }
    return dispatch[op_norm](**kwargs)
