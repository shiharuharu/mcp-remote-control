"""Remote shell command wrapping and endpoint shell/OS probe scripts.

Owns cwd prefixing for POSIX / cmd / PowerShell remote shells, the dual
POSIX+Windows probe scripts run after SSH connect, and probe-output
parsing into dialect / shell_family / encoding hints.

The POSIX probe is busybox ash-friendly (no bashisms: no arrays, no
``[[``, no ``local``, no process substitution) so embedded and minimal
systems still return usable ``uname`` / shell / home / pwd data.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from mcp_remote_control.shell.dialect import (
    resolve_dialect,
    transport_family_for_dialect,
)


def normalize_shell_family(value: str | None) -> str:
    """Map probe/shell labels to: posix | cmd | powershell."""
    if not value:
        return "posix"
    text = str(value).strip().lower()
    if text in ("cmd", "cmd.exe", "command", "command.com"):
        return "cmd"
    if text in (
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "ps",
        "ps1",
    ):
        return "powershell"
    if text in ("windows", "win32", "win"):
        # OS hint without shell — prefer cmd for OpenSSH default shell legacy.
        return "cmd"
    # Fine-grained dialects still wrap as posix.
    if text.startswith("posix-") or text in ("bash", "zsh", "sh", "fish", "busybox"):
        return "posix"
    return "posix"


def coerce_cwd_path(cwd: Any) -> str | None:
    """Return a usable cwd path string, or None if *cwd* is not path-like.

    Guards against probe-cap bleed-through (``pwd`` path overwritten by
    boolean ``cap_pwd`` → ``True`` → ``cd True``).
    """
    if cwd is None or isinstance(cwd, bool):
        return None
    if isinstance(cwd, (int, float, complex, bytes, bytearray)):
        return None
    text = str(cwd).strip()
    if not text:
        return None
    # str(True)/str(False)/str(None) and similar non-paths
    if text in {"True", "False", "None", "true", "false", "none"}:
        return None
    return text


def wrap_with_cwd(
    command: str,
    cwd: str | None,
    *,
    shell_family: str | None = "posix",
) -> str:
    """Prefix *command* so it runs under *cwd* for the remote shell family."""
    path = coerce_cwd_path(cwd)
    if not path:
        return command
    family = normalize_shell_family(shell_family)
    if family == "cmd":
        esc = _escape_cmd_path(path)
        return f'cd /d "{esc}" & {command}'
    if family == "powershell":
        esc = _escape_ps_single(path)
        return f"Set-Location -LiteralPath '{esc}'; {command}"
    # POSIX / bash / zsh / sh / busybox
    return f"cd {shlex.quote(path)} && {command}"


def _escape_cmd_path(path: str) -> str:
    # Inside double quotes, double any embedded quotes.
    return str(path).replace('"', '""')


def _escape_ps_single(path: str) -> str:
    # PowerShell single-quoted: double single quotes.
    return str(path).replace("'", "''")


_CHCP_RE = re.compile(r"(?:Active code page|代码页)[:\s]*(\d+)", re.IGNORECASE)
_UNAME_RE = re.compile(r"^uname=(.+)$", re.MULTILINE | re.IGNORECASE)
_SHELL_RE = re.compile(r"^shell_path=(.*)$", re.MULTILINE | re.IGNORECASE)
_CHARMAP_RE = re.compile(r"^charmap=(.*)$", re.MULTILINE | re.IGNORECASE)
_HOME_RE = re.compile(r"^home=(.*)$", re.MULTILINE | re.IGNORECASE)
_PWD_RE = re.compile(r"^pwd=(.*)$", re.MULTILINE | re.IGNORECASE)
_OS_RE = re.compile(r"^os=(.*)$", re.MULTILINE | re.IGNORECASE)
_COMSPEC_RE = re.compile(r"^comspec=(.*)$", re.MULTILINE | re.IGNORECASE)
_SHELL_BASE_RE = re.compile(r"^shell_base=(.*)$", re.MULTILINE | re.IGNORECASE)
_KV_RE = re.compile(r"^(busybox|busybox_banner|sh_link|cap_pwd|cap_pwd_p|cap_printf)=(.*)$", re.MULTILINE | re.IGNORECASE)


# Dual-path probe: POSIX first; then PowerShell / cmd for Windows OpenSSH.
# Busybox ash-friendly: no bash arrays, no [[, no local, no process substitution.
POSIX_PROBE_SCRIPT = (
    "echo uname=$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null); "
    "echo shell_path=${SHELL:-}; "
    "echo home=${HOME:-}; "
    "echo pwd=$(pwd 2>/dev/null); "
    "echo busybox=$(command -v busybox 2>/dev/null); "
    "echo sh_link=$(readlink /bin/sh 2>/dev/null || true); "
    "busybox 2>/dev/null | head -n 1 | sed 's/^/busybox_banner=/'; "
    "if pwd -P >/dev/null 2>&1; then echo cap_pwd_p=1; else echo cap_pwd_p=0; fi; "
    "if command -v pwd >/dev/null 2>&1; then echo cap_pwd=1; else echo cap_pwd=0; fi; "
    "if command -v printf >/dev/null 2>&1; then echo cap_printf=1; else echo cap_printf=0; fi; "
    "locale charmap 2>/dev/null | sed 's/^/charmap=/'; "
    "echo os=posix"
)

# Windows OpenSSH often defaults to PowerShell. Bare ``echo a & echo b`` and
# ``chcp`` (or ``cmd /c ver``) can close the SSH session. Prefer a native
# PowerShell one-liner; fall back to a single ``cmd /c`` without chcp/ver.
POWERSHELL_PROBE_SCRIPT = (
    'Write-Output "os=windows"; '
    'Write-Output "shell_base=powershell"; '
    'Write-Output ("comspec=" + $env:ComSpec); '
    'Write-Output ("home=" + $env:USERPROFILE); '
    'Write-Output ("pwd=" + (Get-Location).Path)'
)

WINDOWS_PROBE_SCRIPT = (
    'cmd /c "echo os=windows& echo comspec=%COMSPEC%& '
    'echo home=%USERPROFILE%& echo pwd=%CD%& echo shell_base=cmd"'
)


def parse_probe_output(text: str) -> dict[str, Any]:
    """Parse k=v probe lines + chcp into a meta dict (includes dialect/caps).

    Values are heterogeneous (str / int / bool / nested dict); typed as Any
    so callers can use ``.get`` on nested maps without object-attribute noise.
    """
    data: dict[str, Any] = {}
    if not text:
        return data
    body = text.replace("\r\n", "\n").replace("\r", "\n")

    m = _UNAME_RE.search(body)
    if m:
        data["uname"] = m.group(1).strip()
        u = data["uname"].lower()
        if "windows" in u or "mingw" in u or "cygwin" in u or "msys" in u:
            data["os"] = "windows"
        else:
            data["os"] = "posix"

    m = _OS_RE.search(body)
    if m:
        os_val = m.group(1).strip().lower()
        if os_val.startswith("win"):
            data["os"] = "windows"
        elif os_val:
            data["os"] = "posix" if os_val == "posix" else os_val

    m = _SHELL_RE.search(body)
    if m and m.group(1).strip():
        data["shell_path"] = m.group(1).strip()

    m = _CHARMAP_RE.search(body)
    if m and m.group(1).strip():
        data["charmap"] = m.group(1).strip()

    m = _HOME_RE.search(body)
    if m and m.group(1).strip():
        data["home"] = m.group(1).strip()

    m = _PWD_RE.search(body)
    if m and m.group(1).strip():
        data["pwd"] = m.group(1).strip()

    # Explicit shell_base= from PowerShell/cmd probes (before comspec default).
    m = _SHELL_BASE_RE.search(body)
    if m and m.group(1).strip():
        base = m.group(1).strip().lower().removesuffix(".exe")
        if base:
            data["shell_base"] = base
            if base in ("cmd", "powershell", "pwsh", "command"):
                data["os"] = "windows"

    m = _COMSPEC_RE.search(body)
    if m and m.group(1).strip():
        data["comspec"] = m.group(1).strip()
        data["os"] = "windows"
        # comspec presence does not mean the login shell is cmd — Windows
        # OpenSSH often defaults to PowerShell while COMSPEC still points at
        # cmd.exe. Only default shell_base=cmd when probe did not say otherwise.
        data.setdefault("shell_base", "cmd")

    chcp = _CHCP_RE.search(body)
    if chcp:
        data["chcp"] = int(chcp.group(1))
        data["os"] = "windows"

    for km in _KV_RE.finditer(body):
        key = km.group(1).lower()
        val = km.group(2).strip()
        if key in ("cap_pwd", "cap_pwd_p", "cap_printf"):
            # Keep only cap_* keys as bools. Do NOT also set data["pwd"]=True —
            # that collides with the path field pwd=/home/... from _PWD_RE and
            # becomes transport.cwd → wrap_with_cwd → `cd True`.
            data[key] = val in ("1", "true", "yes")
        else:
            data[key] = val

    # Derive shell_base from shell_path.
    sp = str(data.get("shell_path") or "")
    if sp:
        base = sp.replace("\\", "/").rsplit("/", 1)[-1].lower()
        base = base.removesuffix(".exe")
        if base in ("bash", "zsh", "sh", "fish", "dash", "ksh", "ash", "busybox"):
            data["shell_base"] = base
            data.setdefault("os", "posix")
        elif base in ("cmd", "command"):
            data["shell_base"] = "cmd"
            data["os"] = "windows"
        elif base in ("powershell", "pwsh"):
            data["shell_base"] = "powershell" if base == "powershell" else "pwsh"
            data["os"] = "windows"

    if data.get("os") == "windows" and "shell_base" not in data:
        data["shell_base"] = "cmd"

    # Enrich: dialect + caps + coarse shell_family for transport wrap.
    _enrich_dialect(data)
    return data


def _enrich_dialect(data: dict[str, Any]) -> None:
    """Attach dialect / shell_family / busybox flag using resolve_dialect."""
    bb_path = str(data.get("busybox") or "").strip()
    banner = str(data.get("busybox_banner") or "")
    sh_link = str(data.get("sh_link") or "")
    is_bb = bool(bb_path) or ("busybox" in banner.lower()) or ("busybox" in sh_link.lower())
    if is_bb:
        data["busybox"] = bb_path or "1"
    elif "busybox" in data and not data["busybox"]:
        data.pop("busybox", None)

    # Capability flags live under caps= only; never overwrite path field "pwd".
    caps: dict[str, bool] = {
        "pwd": bool(data["cap_pwd"]) if "cap_pwd" in data else True,
        "pwd_p": bool(data["cap_pwd_p"]) if "cap_pwd_p" in data else False,
        "printf": bool(data["cap_printf"]) if "cap_printf" in data else False,
        "busybox": is_bb,
    }
    data["caps"] = caps

    dialect = resolve_dialect(
        shell_base=str(data.get("shell_base") or "") or None,
        shell_path=str(data.get("shell_path") or "") or None,
        shell_family=None,
        busybox=is_bb,
        flags=data,
        os_name=str(data.get("os") or "") or None,
    )
    # Windows shell_base from comspec path
    if data.get("os") == "windows":
        sb = str(data.get("shell_base") or "cmd").lower()
        if sb in ("powershell", "pwsh"):
            dialect = resolve_dialect(shell_base=sb, os_name="windows")
        else:
            dialect = resolve_dialect(shell_base="cmd", os_name="windows")

    data["dialect"] = dialect
    data["shell_family"] = transport_family_for_dialect(dialect)
