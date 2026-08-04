"""Silent shell cwd probe for interactive screen sessions.

After settle/send when ``surface=shell``, inject a one-shot command that prints
``__MRC_PWD__:<abs>``; parse it, update ``session.cwd``, and rely on
``dump_frame(strip_probe=True)`` so the marker never reaches the Agent frame.

Dialect choice drives the probe template: unbound sessions get a best-effort
zsh-compatible default; explicit ``unknown`` (or dialects without a template)
skip inject entirely.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from mcp_remote_control.screen.buffer import dump_frame, parse_pwd_marker
from mcp_remote_control.screen.keys import encode_key, encode_text
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.shell.dialect import (
    CMD,
    POSIX_BASH,
    POSIX_BUSYBOX,
    POSIX_SH,
    POSIX_ZSH,
    POWERSHELL,
    probe_cmd_for_dialect,
    resolve_dialect,
)

# Named probe-command aliases. Source of truth: shell.dialect.
_PROBE_CMD_POSIX = probe_cmd_for_dialect(POSIX_ZSH) or ""
_PROBE_CMD_BASH = probe_cmd_for_dialect(POSIX_BASH) or ""
_PROBE_CMD_CMD = probe_cmd_for_dialect(CMD) or ""
_PROBE_CMD_PWSH = probe_cmd_for_dialect(POWERSHELL) or ""
_PROBE_CMD_SH = probe_cmd_for_dialect(POSIX_SH) or ""
_PROBE_CMD_BUSYBOX = probe_cmd_for_dialect(POSIX_BUSYBOX) or ""

# Heuristic: text action that looks like `cd <path>` (+ submit).
_CD_RE = re.compile(
    r"^\s*cd\s+(?P<path>(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+))\s*$",
    re.IGNORECASE,
)


def is_shell_surface(session: ScreenSession) -> bool:
    surf = (session.surface or "").lower()
    return surf in ("shell", "", "unknown") and session.open_mode == "shell"


def _session_dialect(session: ScreenSession) -> str:
    """Resolve dialect from session fields (preferred) or meta/shell path.

    Empty string means unbound: the probe layer may apply a best-effort default.
    Explicit ``unknown`` dialects skip inject (no probe template).
    """
    d = getattr(session, "dialect", None)
    if isinstance(d, str) and d.strip():
        return d.strip().lower()
    meta = getattr(session, "meta", None) or {}
    caps = getattr(session, "shell_caps", None)
    busybox = None
    if isinstance(caps, dict):
        busybox = caps.get("busybox")
    elif caps is not None:
        busybox = getattr(caps, "busybox", None)
    shell_path = str(
        meta.get("shell_path")
        or getattr(session, "shell_path", None)
        or getattr(session, "shell", None)
        or ""
    )
    shell_base = str(
        meta.get("shell_base")
        or meta.get("shell")
        or meta.get("shell_family")
        or meta.get("os")
        or ""
    )
    if not shell_base and not shell_path and busybox is None and not meta:
        return ""  # unbound
    return resolve_dialect(
        shell_base=shell_base or None,
        shell_path=shell_path or None,
        shell_family=str(meta.get("shell_family") or "") or None,
        busybox=bool(busybox) if busybox is not None else None,
        flags=meta if isinstance(meta, dict) else None,
    )


def _probe_cmd_for_session(session: ScreenSession) -> str | None:
    """Pick silent pwd command by session dialect; None = skip inject."""
    dialect = _session_dialect(session)
    if not dialect:
        # Unbound session: best-effort zsh-compatible short probe; helpers
        # soft-fail on bash. Prefer binding dialect from the endpoint when known.
        dialect = POSIX_ZSH
    return probe_cmd_for_dialect(dialect)


def silent_pwd_probe(
    session: ScreenSession,
    *,
    timeout_s: float = 1.5,
    clear_line: bool = True,
) -> str | None:
    """Inject a detachable pwd probe on the same PTY; return absolute path or None.

    Only intended for interactive shell surfaces. Failures are silent — caller
    keeps the previous ``session.cwd``. When dialect has no probe template
    (unknown/fish), returns None without writing to the PTY.
    """
    if session.closed:
        return None
    try:
        if not session.pty.is_alive():
            return None
    except Exception:  # noqa: BLE001
        return None

    cmd = _probe_cmd_for_session(session)
    if not cmd:
        # Mark cwd provenance stale for Agent meta when no probe template.
        try:
            session.cwd_src = "stale"  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return None

    try:
        if clear_line:
            session.write(encode_key("ctrl+u"))
        session.write(encode_text(cmd))
        session.write(encode_key("enter"))
    except Exception:  # noqa: BLE001
        return None

    found: str | None = None
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            session.drain(0.05)
        except Exception:  # noqa: BLE001
            break
        # Do not strip probe while hunting for the marker.
        frame = dump_frame(session.screen, strip_probe=False)
        path = parse_pwd_marker(frame)
        if path and _looks_absolute(path):
            found = _normalize_path(path)
            break

    # Clear any leftover partial line / failed helper noise on the prompt.
    try:
        session.write(encode_key("ctrl+u"))
        session.drain(0.05)
    except Exception:  # noqa: BLE001
        pass
    return found


def _looks_absolute(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/"):
        return True
    # Windows drive or UNC
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
        return True
    if path.startswith("\\\\"):
        return True
    return False


def probe_and_update_cwd(
    session: ScreenSession,
    *,
    timeout_s: float = 1.5,
    force: bool = False,
) -> str | None:
    """Run silent probe when surface is shell; update ``session.cwd`` on success."""
    if not force and not _should_probe(session):
        return session.cwd
    path = silent_pwd_probe(session, timeout_s=timeout_s)
    if path:
        session.cwd = path
        session.cwd_src = "probe"
        return path
    if getattr(session, "cwd_src", None) not in ("probe", "heuristic"):
        session.cwd_src = "stale"
    return session.cwd


def apply_cd_heuristic(
    session: ScreenSession,
    actions: Sequence[Mapping[str, Any]] | None,
) -> str | None:
    """If actions contain an obvious ``cd <path>`` + submit, update session.cwd.

    Used as fallback when silent probe fails or is skipped (TUI). Relative paths
    join against the previous absolute cwd when known.
    """
    if not actions:
        return session.cwd
    new_cwd: str | None = None
    for act in actions:
        atype = str(act.get("type") or act.get("op") or "").strip().lower()
        if atype not in ("text", "paste"):
            continue
        text = act.get("text")
        if text is None:
            continue
        submit = act.get("submit")
        # text with submit=true, or a following submit is handled by scanning
        # only self-contained text+submit / paste+submit here.
        if not _truthy(submit):
            continue
        m = _CD_RE.match(str(text).strip())
        if not m:
            continue
        raw = m.group("path").strip()
        if (raw.startswith("'") and raw.endswith("'")) or (
            raw.startswith('"') and raw.endswith('"')
        ):
            raw = raw[1:-1]
        new_cwd = _resolve_cd_target(raw, session.cwd)
    if new_cwd:
        session.cwd = new_cwd
        session.cwd_src = "heuristic"
    return session.cwd


def update_cwd_after_send(
    session: ScreenSession,
    actions: Sequence[Mapping[str, Any]] | None,
    *,
    probe: bool = True,
    timeout_s: float = 1.5,
) -> str | None:
    """Preferred post-send cwd refresh: probe shell, else cd heuristic."""
    if probe and _should_probe(session):
        path = silent_pwd_probe(session, timeout_s=timeout_s)
        if path:
            session.cwd = path
            session.cwd_src = "probe"
            # Brief settle so the re-drawn prompt lands before the Agent shot.
            try:
                session.drain(0.08)
            except Exception:  # noqa: BLE001
                pass
            return path
    # Fallback / always apply cd heuristic as soft update
    before = session.cwd
    apply_cd_heuristic(session, actions)
    if session.cwd != before and session.cwd_src != "heuristic":
        # heuristic may have set cwd_src already
        if getattr(session, "cwd_src", None) != "heuristic":
            session.cwd_src = "stale"
    elif session.cwd == before and getattr(session, "cwd_src", None) not in (
        "probe",
        "heuristic",
        "open",
    ):
        session.cwd_src = "stale"
    return session.cwd


def _should_probe(session: ScreenSession) -> bool:
    if session.closed:
        return False
    # Do not inject into TUI / alt-screen surfaces.
    surf = (session.surface or "shell").lower()
    if surf in ("tui", "alt", "alt_screen", "pager"):
        return False
    if session.open_mode != "shell" and surf != "shell":
        return False
    try:
        return bool(session.pty.is_alive())
    except Exception:  # noqa: BLE001
        return False


def _normalize_path(path: str) -> str:
    text = path.strip()
    if not text:
        return text
    # Local absolute posix — resolve when possible.
    if text.startswith("/"):
        try:
            return str(Path(text).resolve())
        except OSError:
            return text
    # Windows-style: leave as-is (no local resolve).
    return text


def _resolve_cd_target(raw: str, base: str | None) -> str | None:
    if not raw or raw == "-":
        return None
    if raw == "~" or raw.startswith("~/"):
        home = os.environ.get("HOME") or os.path.expanduser("~")
        if raw == "~":
            return str(Path(home).resolve()) if home else None
        joined = str(PurePosixPath(home) / raw[2:])
        try:
            return str(Path(joined).expanduser().resolve())
        except OSError:
            return joined
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        try:
            if raw.startswith("/"):
                return str(Path(raw).resolve())
        except OSError:
            pass
        return raw
    if base:
        try:
            return str((Path(base) / raw).resolve())
        except OSError:
            return str(PurePosixPath(base) / raw)
    return raw


def _truthy(val: Any) -> bool:
    if val is True or val is False:
        return bool(val)
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")
