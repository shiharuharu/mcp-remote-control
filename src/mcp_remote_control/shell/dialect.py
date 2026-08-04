"""Shell dialect registry for silent cwd probes and exec defaults.

Dialects are coarse labels (not a full shell zoo). Busybox is first-class:
shortest POSIX probe, never bashisms such as ``fc`` or ``history -d``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Local copy of the silent-cwd marker to avoid a shell ↔ screen import cycle.
# Must match mcp_remote_control.screen.buffer.PWD_MARKER.
PWD_MARKER = "__MRC_PWD__:"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUSYBOX_PROBE_MAX_LEN = 80

# Dialect string IDs (stable Agent meta tokens).
POSIX_BASH = "posix-bash"
POSIX_ZSH = "posix-zsh"
POSIX_SH = "posix-sh"
POSIX_BUSYBOX = "posix-busybox"
FISH = "fish"
CMD = "cmd"
POWERSHELL = "powershell"
UNKNOWN = "unknown"

ShellDialect = str  # alias for type hints; values are the constants above

_ALL_DIALECTS: frozenset[str] = frozenset(
    {
        POSIX_BASH,
        POSIX_ZSH,
        POSIX_SH,
        POSIX_BUSYBOX,
        FISH,
        CMD,
        POWERSHELL,
        UNKNOWN,
    }
)


@dataclass(frozen=True)
class ShellCaps:
    """Best-effort capability bits from endpoint probe / PTY sniff."""

    pwd: bool = True
    pwd_p: bool = False
    printf: bool = False
    fc_p: bool = False
    set_history: bool = False
    busybox: bool = False
    fish: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "pwd": self.pwd,
            "pwd_p": self.pwd_p,
            "printf": self.printf,
            "fc_p": self.fc_p,
            "set_history": self.set_history,
            "busybox": self.busybox,
            "fish": self.fish,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ShellCaps:
        if not data:
            return cls()
        def _b(key: str, default: bool = False) -> bool:
            # Prefer cap_* / nested caps so a path field like "pwd=/home/…"
            # is never treated as a capability flag (collides with short name).
            raw = data.get(f"cap_{key}")
            if raw is None:
                nested = data.get("caps")
                if isinstance(nested, Mapping):
                    raw = nested.get(key)
            if raw is None:
                raw = data.get(key)
            if raw is None:
                return default
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if key == "pwd" and ("/" in s or "\\" in s or s.startswith("~")):
                return default
            # Non-empty busybox path means the capability is present.
            if key == "busybox" and s and s not in ("0", "false", "no", "off"):
                return True
            return s in ("1", "true", "yes", "on")

        return cls(
            pwd=_b("pwd", True),
            pwd_p=_b("pwd_p", False),
            printf=_b("printf", False),
            fc_p=_b("fc_p", False),
            set_history=_b("set_history", False),
            busybox=_b("busybox", False),
            fish=_b("fish", False),
        )


@dataclass(frozen=True)
class ProbeSpec:
    """One silent-cwd injection template for a dialect."""

    cmd: str
    max_len: int = 200
    history_mode: str = "none"  # none | leading_space | fc_p | set_history | remove_last
    # If False, silent probe must not run (unknown / unsupported).
    enabled: bool = True

    def fits(self) -> bool:
        return len(self.cmd) <= self.max_len


# ---------------------------------------------------------------------------
# Probe command templates (keep short — live PTY truncation risk)
# ---------------------------------------------------------------------------
#
# Leading space → HISTCONTROL=ignorespace / HIST_IGNORE_SPACE when enabled.
# Soft-fail helpers with ||: so missing options cannot trip errexit.
# End with ``:`` so compound $? is 0 (avoids a dirty prompt status).
# Never use bash-only ``history -d`` (breaks under zsh).

_PROBE_ZSH = (
    f" fc -p 2>/dev/null||:;"
    f"echo {PWD_MARKER}$(pwd -P 2>/dev/null||pwd);"
    f"fc -P 2>/dev/null||:;"
    f":"
)

_PROBE_BASH = (
    f" set +o history 2>/dev/null||:;"
    f"echo {PWD_MARKER}$(pwd -P 2>/dev/null||pwd);"
    f"set -o history 2>/dev/null||:;"
    f":"
)

# dash/ash/busybox: shortest path; no fc, no set ±o history.
_PROBE_SH = f" echo {PWD_MARKER}$(pwd -P 2>/dev/null||pwd)"

_PROBE_BUSYBOX = f" echo {PWD_MARKER}$(pwd 2>/dev/null||pwd)"

_PROBE_CMD = f"echo {PWD_MARKER}%CD%"

_PROBE_PWSH = (
    f"Write-Output {PWD_MARKER}$((Get-Location).Path); "
    f"try {{ Get-History -Count 1 | Remove-History -ErrorAction SilentlyContinue }} "
    f"catch {{ }}; "
    f"$null"
)

# fish: skip silent inject (non-POSIX syntax); rely on cd heuristics only.

DIALECT_PROBES: dict[str, ProbeSpec | None] = {

    POSIX_ZSH: ProbeSpec(
        cmd=_PROBE_ZSH,
        max_len=120,
        history_mode="fc_p",
    ),
    POSIX_BASH: ProbeSpec(
        cmd=_PROBE_BASH,
        max_len=140,
        history_mode="set_history",
    ),
    POSIX_SH: ProbeSpec(
        cmd=_PROBE_SH,
        max_len=BUSYBOX_PROBE_MAX_LEN,
        history_mode="leading_space",
    ),
    POSIX_BUSYBOX: ProbeSpec(
        cmd=_PROBE_BUSYBOX,
        max_len=BUSYBOX_PROBE_MAX_LEN,
        history_mode="leading_space",
    ),
    FISH: None,
    CMD: ProbeSpec(cmd=_PROBE_CMD, max_len=40, history_mode="none"),
    POWERSHELL: ProbeSpec(
        cmd=_PROBE_PWSH,
        max_len=220,
        history_mode="remove_last",
    ),
    UNKNOWN: None,
}


def probe_cmd_for_dialect(dialect: str | None) -> str | None:
    """Return the silent pwd command for *dialect*, or None to skip inject."""
    d = (dialect or UNKNOWN).strip().lower()
    if d not in _ALL_DIALECTS:
        d = UNKNOWN
    spec = DIALECT_PROBES.get(d)
    if spec is None or not spec.enabled:
        return None
    return spec.cmd


def resolve_dialect(
    *,
    shell_base: str | None = None,
    shell_path: str | None = None,
    shell_family: str | None = None,
    busybox: bool | None = None,
    flags: Mapping[str, Any] | None = None,
    os_name: str | None = None,
) -> str:
    """Map probe fields / hints to a frozen dialect id.

    Priority:
    1. Explicit Windows family (cmd / powershell)
    2. busybox flag / path / banner in flags
    3. shell_base basename
    4. shell_path basename
    5. shell_family coarse (posix → posix-sh)
    6. unknown
    """
    flags = flags or {}
    base = (shell_base or "").strip().lower()
    path = (shell_path or "").strip().lower().replace("\\", "/")
    family = (shell_family or "").strip().lower()
    os_l = (os_name or str(flags.get("os") or "")).strip().lower()

    # busybox detection
    is_bb = bool(busybox)
    if not is_bb:
        is_bb = _truthy(flags.get("busybox")) or _truthy(flags.get("cap_busybox"))
    banner = str(flags.get("busybox_banner") or flags.get("sh_link") or "").lower()
    if not is_bb and "busybox" in banner:
        is_bb = True
    if not is_bb and "busybox" in path:
        is_bb = True
    if not is_bb and base == "busybox":
        is_bb = True

    # Windows first
    if family in ("cmd", "powershell") or base in (
        "cmd",
        "cmd.exe",
        "command",
        "command.com",
    ):
        if base in ("powershell", "pwsh", "powershell.exe", "pwsh.exe") or family == "powershell":
            return POWERSHELL
        if family == "cmd" or base in ("cmd", "cmd.exe", "command", "command.com"):
            return CMD
    if base in ("powershell", "pwsh", "powershell.exe", "pwsh.exe"):
        return POWERSHELL
    if "powershell" in path or path.endswith("pwsh") or path.endswith("pwsh.exe"):
        return POWERSHELL
    if base in ("cmd", "cmd.exe") or (
        "cmd.exe" in path and "powershell" not in path
    ):
        return CMD
    if os_l in ("windows", "win32", "win") and family in ("", "cmd"):
        # OpenSSH DefaultShell is often cmd when only the OS is known.
        if family == "powershell":
            return POWERSHELL
        if base or path:
            pass  # fall through to basename rules
        elif not base and not path:
            return CMD

    if not base and path:
        base = path.rstrip("/").rsplit("/", 1)[-1]
        base = base.removesuffix(".exe")

    if is_bb and base in ("", "sh", "ash", "busybox", "hush"):
        return POSIX_BUSYBOX
    if is_bb and base in ("bash", "zsh", "fish"):
        # Rare: busybox applet name only — still treat as busybox env.
        if base == "bash":
            return POSIX_BASH
        if base == "zsh":
            return POSIX_ZSH
        if base == "fish":
            return FISH

    if base in ("bash",):
        return POSIX_BASH
    if base in ("zsh",):
        return POSIX_ZSH
    if base in ("fish",):
        return FISH
    if base in ("dash", "ash", "hush", "sh"):
        return POSIX_BUSYBOX if is_bb else POSIX_SH
    if base in ("ksh", "mksh", "yash", "oksh"):
        return POSIX_SH
    if base in ("csh", "tcsh"):
        return UNKNOWN  # no silent probe

    if "bash" in path and "busybox" not in path:
        return POSIX_BASH
    if path.endswith("/zsh") or "/zsh" in path:
        return POSIX_ZSH
    if path.endswith("/fish") or "/fish" in path:
        return FISH

    if family == "posix":
        return POSIX_BUSYBOX if is_bb else POSIX_SH
    if family in ("bash",):
        return POSIX_BASH
    if family in ("zsh",):
        return POSIX_ZSH

    if base or path:
        # Unknown named shell — skip silent inject conservatively.
        return UNKNOWN
    return UNKNOWN


def default_runtime_for_dialect(dialect: str | None) -> str:
    """Interpreter token for exec script body when runtime=auto."""
    d = (dialect or UNKNOWN).strip().lower()
    if d == POSIX_BASH:
        return "bash"
    if d == POSIX_ZSH:
        # Body scripts are usually bash-friendly; default to bash -c.
        return "bash"
    if d in (POSIX_SH, POSIX_BUSYBOX, UNKNOWN):
        return "sh"
    if d == POWERSHELL:
        return "pwsh"
    if d == CMD:
        return "cmd"
    if d == FISH:
        return "sh"  # fish is not a script runtime here
    return "sh"


def transport_family_for_dialect(dialect: str | None) -> str:
    """Map dialect → wrap_with_cwd family: posix | cmd | powershell."""
    d = (dialect or UNKNOWN).strip().lower()
    if d == CMD:
        return "cmd"
    if d == POWERSHELL:
        return "powershell"
    return "posix"


def _truthy(val: Any) -> bool:
    if val is True:
        return True
    if val is False or val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("", "0", "false", "no", "off", "none", "null"):
        return False
    # Non-empty path-like values count as true (e.g. busybox=/bin/busybox).
    return True


# Re-exports for cwd_probe and other callers that import probe strings directly.
PROBE_CMD_ZSH = _PROBE_ZSH
PROBE_CMD_BASH = _PROBE_BASH
PROBE_CMD_SH = _PROBE_SH
PROBE_CMD_BUSYBOX = _PROBE_BUSYBOX
PROBE_CMD_CMD = _PROBE_CMD
PROBE_CMD_PWSH = _PROBE_PWSH
