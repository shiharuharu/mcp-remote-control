"""Open-time identity probe (non-raising; WinRM intensity + light probe)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp_remote_control.config import Profile, load_config
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.winrm import resolve_winrm_open_probe_mode


def _resolve_open_probe_mode(
    profile: Profile,
    home_path: Path,
    *,
    explicit_probe: bool,
) -> str:
    """Resolve WinRM open probe intensity; non-winrm ignores config (bool only)."""
    if profile.transport != "winrm":
        return "full" if explicit_probe else "skip"
    global_winrm_probe: Any = None
    try:
        cfg = load_config(home_path)
        global_winrm_probe = getattr(cfg.defaults, "winrm_probe", None)
    except Exception:  # noqa: BLE001 - missing/bad global config -> fall through
        global_winrm_probe = None
    return resolve_winrm_open_probe_mode(
        explicit_probe=explicit_probe,
        winrm_cfg=profile.winrm or None,
        profile_defaults=profile.defaults or None,
        global_winrm_probe=global_winrm_probe,
        env=os.environ,
    )


def _light_probe(
    profile: Profile,
    transport: BaseTransport,
    *,
    probe_mode: str = "full",
) -> dict[str, Any]:
    """Lightweight open-time probe. Failure yields partial status; never raises.

    *probe_mode* is forwarded to WinRM ``collect_probe(mode=...)`` as
    ``full`` or ``light``. Other transports ignore it.
    """
    data: dict[str, Any] = {
        "status": "ok",
        "transport": profile.transport,
    }
    if transport.home:
        data["home"] = transport.home
    if transport.cwd:
        data["pwd"] = transport.cwd
    if profile.transport == "local":
        data["user"] = os.environ.get("USER") or os.environ.get("USERNAME")
        # Local open-summary seeds (shell / uname / locale).
        shell_path = os.environ.get("SHELL")
        if shell_path:
            data["shell_path"] = shell_path
            base = shell_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base:
                data["shell_base"] = base
        try:
            import platform

            data["uname"] = f"{platform.system()}-{platform.machine()}"
        except Exception:  # noqa: BLE001
            pass
        try:
            import locale as _locale

            enc = _locale.getpreferredencoding(False) or ""
            if enc:
                data["text_encoding"] = enc
        except Exception:  # noqa: BLE001
            pass
    if profile.host:
        data["host"] = profile.host

    # Merge transport.meta seeds when present (os / shell / ps_version / ...).
    meta = getattr(transport, "meta", None) or {}
    for key in ("os", "shell", "ps_version", "auth", "dialect", "shell_base", "shell_path"):
        if key in meta and meta[key] is not None:
            data[key] = meta[key]

    # Transport-specific best-effort probe (e.g. WinRM collect_probe).
    collector = getattr(transport, "collect_probe", None)
    if callable(collector):
        try:
            if profile.transport == "winrm":
                extra = collector(mode=probe_mode) or {}
            else:
                extra = collector() or {}
            if isinstance(extra, dict):
                # Keep outer status=ok unless extra marks partial/fail.
                status = extra.pop("status", None)
                data.update(extra)
                if status in ("partial", "fail", "error"):
                    data["status"] = status
                elif "error" in data and data.get("status") == "ok":
                    data["status"] = "partial"
        except Exception as exc:  # noqa: BLE001 - probe must not fail open
            data["status"] = "partial"
            data["error"] = _short_probe_err(exc)

    if meta.get("probe_status") == "partial":
        data["status"] = "partial"
        if meta.get("probe_error") and "error" not in data:
            data["error"] = meta["probe_error"]

    # Ensure dialect is present for screen/exec wiring.
    if "dialect" not in data or not data.get("dialect"):
        try:
            from mcp_remote_control.shell.dialect import resolve_dialect

            data["dialect"] = resolve_dialect(
                shell_base=str(data.get("shell_base") or "") or None,
                shell_path=str(data.get("shell_path") or "") or None,
                shell_family=str(data.get("shell_family") or "") or None,
                busybox=bool(data.get("busybox")),
                flags=data,
                os_name=str(data.get("os") or "") or None,
            )
        except Exception:  # noqa: BLE001
            pass

    return data


def _short_probe_err(exc: BaseException, limit: int = 120) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
