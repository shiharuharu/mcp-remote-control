"""Endpoint Core ops: list, open, and close host connections.

Maps profile names to connected transports, surfaces capability tokens and a
compact probe summary on open, and tears down attached screen/ps sessions on
close. Serial hardware uses the separate console tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, resolve_home
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.caps import format_caps, merge_caps
from mcp_remote_control.endpoint.registry import (
    get_registry,
    list_known_profiles,
)
from mcp_remote_control.transport import TransportError

VALID_OPS: frozenset[str] = frozenset({"list", "open", "close"})


def list_endpoints(
    *,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """List known profiles and open endpoint status."""
    home_path = _home(home)
    try:
        names = list_known_profiles(home_path)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="endpoint",
            status="error",
            code="CONFIG_ERROR",
            fields={"op": "list", "msg": str(exc)},
        )

    reg = get_registry()
    open_map = {ep.name: ep for ep in reg.list_open()}

    lines: list[str] = []
    for name in names:
        ep = open_map.get(name)
        if ep is not None:
            caps = ep.caps_token
            transport = ep.transport_name
            open_flag = "1" if ep.connected else "0"
            extra = ""
            if ep.meta.get("host"):
                extra = f" host={ep.meta['host']}"
            lines.append(
                f"{name} transport={transport} open={open_flag} caps={caps}{extra}"
            )
        else:
            # Not open: derive caps from the on-disk profile when loadable.
            try:
                from mcp_remote_control.config import load_profile

                profile = load_profile(home_path, name)
                caps = format_caps(merge_caps(profile.transport, profile.caps or None))
                host_bit = f" host={profile.host}" if profile.host else ""
                lines.append(
                    f"{name} transport={profile.transport} open=0 caps={caps}{host_bit}"
                )
            except (ProfileNotFound, ProfileInvalid):
                lines.append(f"{name} open=0")

    # Still list open endpoints whose profile file was removed after connect.
    for name, ep in open_map.items():
        if name not in names:
            lines.append(
                f"{name} transport={ep.transport_name} open="
                f"{'1' if ep.connected else '0'} caps={ep.caps_token}"
            )

    body = "\n".join(lines) if lines else None
    return OpResult(
        kind="endpoint",
        status="ok",
        fields={
            "op": "list",
            "n": len(names),
            "open": len(open_map),
        },
        body=body,
    )


def open_endpoint(
    *,
    profile: str | None = None,
    ep: str | None = None,
    home: Path | str | None = None,
    probe: bool = True,
    connector: Any | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Open / connect an endpoint from a profile name."""
    name = profile or ep
    if not name or not str(name).strip():
        return OpResult(
            kind="endpoint",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "open",
                "msg": "profile name required (profile= or ep=)",
            },
            hint="mcp-remote-control-cli endpoint open --profile <name>",
        )

    name = str(name).strip()
    home_path = _home(home)
    reg = get_registry()
    try:
        endpoint = reg.open(
            name,
            home=home_path,
            probe=bool(probe),
            connector=connector,
        )
    except ProfileNotFound as exc:
        return OpResult(
            kind="endpoint",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "open", "profile": name, "msg": _short(str(exc))},
        )
    except ProfileInvalid as exc:
        return OpResult(
            kind="endpoint",
            status="error",
            code="PROFILE_INVALID",
            fields={"op": "open", "profile": name, "msg": _short(str(exc))},
        )
    except TransportError as exc:
        fields: dict[str, Any] = {
            "op": "open",
            "profile": name,
            "msg": exc.msg,
        }
        if exc.details.get("host"):
            fields["host"] = exc.details["host"]
        return OpResult(
            kind="endpoint",
            status="error",
            code=exc.code or "CONNECT_FAILED",
            fields=fields,
        )
    except ValueError as exc:
        return OpResult(
            kind="endpoint",
            status="error",
            code="INVALID_ARG",
            fields={"op": "open", "msg": _short(str(exc))},
        )
    except Exception as exc:  # noqa: BLE001 — keep the process alive on open
        return OpResult(
            kind="endpoint",
            status="error",
            code="CONNECT_FAILED",
            fields={
                "op": "open",
                "profile": name,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
        )

    fields_ok: dict[str, Any] = {
        "op": "open",
        "ep": endpoint.name,
        "transport": endpoint.transport_name,
        "caps": endpoint.caps_token,
    }
    if endpoint.meta.get("host"):
        fields_ok["host"] = endpoint.meta["host"]
    if endpoint.meta.get("label"):
        fields_ok["label"] = endpoint.meta["label"]

    # Compact shell/uname/locale tokens for Agent meta — not a full probe dump.
    if endpoint.probe:
        fields_ok.update(_probe_summary_fields(endpoint.probe))

    return OpResult(
        kind="endpoint",
        status="ok",
        cwd=endpoint.cwd,
        fields=fields_ok,
    )


def close_endpoint(
    *,
    ep: str | None = None,
    profile: str | None = None,
    home: Path | str | None = None,  # reserved for API symmetry with open/list
    **_kwargs: Any,
) -> OpResult:
    """Close a connected endpoint."""
    name = ep or profile
    if not name or not str(name).strip():
        return OpResult(
            kind="endpoint",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "close",
                "msg": "endpoint id required (ep= or profile=)",
            },
            hint="mcp-remote-control-cli endpoint close --ep <name>",
        )

    name = str(name).strip()
    reg = get_registry()
    removed = reg.close(name)
    if removed is None:
        return OpResult(
            kind="endpoint",
            status="error",
            code="ENDPOINT_NOT_FOUND",
            fields={
                "op": "close",
                "ep": name,
                "msg": f"endpoint not open: {name}",
            },
        )

    # Tear down screens attached to this endpoint; never fail the close on cleanup.
    screens_closed = 0
    try:
        from mcp_remote_control.screen.registry import get_screen_registry

        screens_closed = get_screen_registry().close_for_endpoint(name)
    except Exception:  # noqa: BLE001
        screens_closed = 0

    # Tear down PS runspaces attached to this endpoint.
    ps_closed = 0
    try:
        from mcp_remote_control.ps.registry import get_ps_registry

        ps_closed = get_ps_registry().close_for_endpoint(name)
    except Exception:  # noqa: BLE001
        ps_closed = 0

    fields: dict[str, Any] = {
        "op": "close",
        "ep": name,
        "disconnected": True,
    }
    if screens_closed:
        fields["screens_closed"] = screens_closed
    if ps_closed:
        fields["ps_closed"] = ps_closed

    return OpResult(
        kind="endpoint",
        status="ok",
        fields=fields,
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch endpoint op → Core implementation."""
    op_norm = (op or "").strip().lower().replace("-", "_")
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="endpoint",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown endpoint op (want list|open|close)",
            },
            hint="use op=list|open|close; serial console is the console tool",
        )
    dispatch = {
        "list": list_endpoints,
        "open": open_endpoint,
        "close": close_endpoint,
    }
    return dispatch[op_norm](**kwargs)


def _home(home: Path | str | None) -> Path:
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()


def _short(msg: str, limit: int = 200) -> str:
    text = " ".join(str(msg).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _probe_summary_fields(probe: dict[str, Any]) -> dict[str, Any]:
    """Map probe keys to short Agent meta tokens (non-empty only).

    ``shell_base``/``shell_path`` → ``shell=``; ``uname``; ``charmap`` /
    ``text_encoding`` → ``locale=``; ``dialect=``; optional ``busybox=1``.

    WinRM PS capability tokens (flat keys or nested ``winrm_ps``): ``ps_version=``,
    ``lang_mode=``, ``ps_fs=`` (0/1 from ``ps_script_fs``), optional ``ps_edition=``.
    """
    out: dict[str, Any] = {}

    shell = probe.get("shell_base") or probe.get("shell")
    if not shell:
        sp = probe.get("shell_path")
        if isinstance(sp, str) and sp.strip():
            base = sp.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base.lower().endswith(".exe"):
                base = base[:-4]
            shell = base or None
    if shell is not None and str(shell).strip():
        out["shell"] = str(shell).strip()

    dialect = probe.get("dialect")
    if dialect is not None and str(dialect).strip():
        out["dialect"] = str(dialect).strip()

    caps = probe.get("caps") if isinstance(probe.get("caps"), dict) else {}
    if probe.get("busybox") or (caps or {}).get("busybox"):
        out["busybox"] = 1

    uname = probe.get("uname")
    if uname is not None and str(uname).strip():
        out["uname"] = str(uname).strip()

    locale = probe.get("text_encoding") or probe.get("charmap")
    if locale is not None and str(locale).strip():
        out["locale"] = str(locale).strip()

    # WinRM PS capability summary: prefer flat probe keys, fall back to winrm_ps.
    _raw_wp = probe.get("winrm_ps")
    winrm_ps: dict[str, Any] = _raw_wp if isinstance(_raw_wp, dict) else {}

    def _pick(key: str) -> Any:
        val = probe.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            val = winrm_ps.get(key)
        return val

    ps_version = _pick("ps_version")
    if ps_version is not None and str(ps_version).strip():
        out["ps_version"] = str(ps_version).strip()

    lang_mode = _pick("language_mode")
    if lang_mode is not None and str(lang_mode).strip():
        out["lang_mode"] = str(lang_mode).strip()

    ps_script_fs = _pick("ps_script_fs")
    if ps_script_fs is not None and str(ps_script_fs).strip() != "":
        if isinstance(ps_script_fs, str):
            truthy = ps_script_fs.strip().lower() in ("1", "true", "yes")
        else:
            truthy = bool(ps_script_fs)
        out["ps_fs"] = 1 if truthy else 0

    ps_edition = _pick("ps_edition")
    if ps_edition is not None and str(ps_edition).strip():
        out["ps_edition"] = str(ps_edition).strip()

    ps_probe = _pick("ps_probe")
    if ps_probe is not None and str(ps_probe).strip():
        out["ps_probe"] = str(ps_probe).strip()

    return out
