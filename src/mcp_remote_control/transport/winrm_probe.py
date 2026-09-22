"""WinRM open-time probe mode and PowerShell capability parse/derive."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any, Literal

# Minimum PS version for built-in script FS (major, minor). Below this,
# ps_script_fs is forced false when the version string is parseable.
MRC_WINRM_PS_FS_MIN: tuple[int, int] = (5, 1)

# Default wall-clock budget (seconds) for the post-connect identity / capability
# probe. Short so a hung remote PowerShell cannot stall open indefinitely.
# This is a default, not a hard cap: a high-latency link whose single
# PowerShell RTT exceeds it needs a larger budget and would otherwise never
# open. Override via profile ``[winrm].probe_timeout_s`` or env
# ``MRC_WINRM_PROBE_TIMEOUT_S`` - see :func:`resolve_winrm_probe_timeout_s`.
#
# Policy (mode=full only):
# - **Identity / WSMan RTT hard-fail**: if the open-time oneshot cannot prove
#   the session is reachable (exception, timeout, empty/junk output with no
#   trusted identity seeds), :meth:`WinRMTransport.collect_probe` calls
#   :meth:`WinRMTransport._identity_probe_hard_fail` which :meth:`mark_dead`
#   **and** immediately best-effort :meth:`_dispose_prior_session` (same
#   MaxShells lever as exec hard-timeout) so ``is_connected()`` is False and
#   ``endpoint open`` returns error (no fake connected registration).
# - **Capability incomplete soft**: parse/language_mode gaps after a
#   successful RTT stay ``status=partial``; open may still succeed.
#
# MaxShellsPerUser cost: the unseeded production path issues one extra
# oneshot (capability script doubles as identity RTT). That temporary
# server shell counts against WinRM ``MaxShellsPerUser`` until the HTTP
# response finishes or the session is closed - same orphan risk as other
# hard-timeout oneshots (see module docstring). Lab modes ``light`` / ``skip``
# avoid this oneshot (see :func:`resolve_winrm_open_probe_mode`).
MRC_WINRM_PROBE_TIMEOUT_S: float = 5.0

# Env override for open-time probe intensity (skip | light | full).
MRC_WINRM_PROBE_ENV: str = "MRC_WINRM_PROBE"

# Env override for the open-time probe budget in seconds.
MRC_WINRM_PROBE_TIMEOUT_ENV: str = "MRC_WINRM_PROBE_TIMEOUT_S"

WinrmOpenProbeMode = Literal["skip", "light", "full"]
_WINRM_OPEN_PROBE_MODES: frozenset[str] = frozenset({"skip", "light", "full"})


def normalize_winrm_probe_mode(value: Any) -> WinrmOpenProbeMode | None:
    """Return canonical ``skip|light|full`` or ``None`` when *value* is unset/junk.

    Accepts bool (``True``->full, ``False``->skip) and common string aliases.
    Does not raise - callers that need hard validation use config load.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "full" if value else "skip"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 0 -> skip, non-zero -> full (TOML/env numeric convenience).
        try:
            return "skip" if float(value) == 0 else "full"
        except (TypeError, ValueError):
            return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _WINRM_OPEN_PROBE_MODES:
        return text  # type: ignore[return-value]
    if text in ("skipped", "none", "off", "false", "no"):
        return "skip"
    if text in ("soft", "partial"):
        return "light"
    if text in ("hard", "true", "yes", "on"):
        return "full"
    return None


def resolve_winrm_open_probe_mode(
    *,
    explicit_probe: bool | None = None,
    winrm_cfg: Mapping[str, Any] | None = None,
    profile_defaults: Mapping[str, Any] | None = None,
    global_winrm_probe: Any = None,
    env: Mapping[str, str] | None = None,
) -> WinrmOpenProbeMode:
    """Resolve WinRM open-time probe intensity.

    Priority (highest first):

    1. ``explicit_probe is False`` -> ``skip`` (API ``open(probe=False)``).
       ``explicit_probe is True`` does **not** force full; config/env still apply.
    2. Env ``MRC_WINRM_PROBE`` (when set to a recognized token).
    3. Profile ``[winrm].probe``.
    4. Profile ``[defaults].winrm_probe``.
    5. Global config ``[defaults].winrm_probe``.
    6. Default ``full`` (hard identity failure -> open error).

    Unknown tokens at each layer are ignored so a bad env does not block open
    when profile/default is valid (config load already rejects bad profile TOML).
    """
    if explicit_probe is False:
        return "skip"

    env_map = env if env is not None else os.environ
    raw_env = env_map.get(MRC_WINRM_PROBE_ENV)
    if raw_env is not None and str(raw_env).strip():
        mode = normalize_winrm_probe_mode(raw_env)
        if mode is not None:
            return mode

    if winrm_cfg:
        mode = normalize_winrm_probe_mode(winrm_cfg.get("probe"))
        if mode is not None:
            return mode

    if profile_defaults:
        mode = normalize_winrm_probe_mode(profile_defaults.get("winrm_probe"))
        if mode is not None:
            return mode

    mode = normalize_winrm_probe_mode(global_winrm_probe)
    if mode is not None:
        return mode

    return "full"


def _coerce_probe_timeout_s(value: Any) -> float | None:
    """Return *value* as a finite positive float seconds, else ``None``.

    Never raises. Bools are rejected: ``float(True)`` would silently become a
    1s budget, which is never a meaningful probe timeout.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def resolve_winrm_probe_timeout_s(
    *,
    profile_value: Any = None,
    env: Mapping[str, str] | None = None,
) -> float:
    """Resolve the open-time identity / capability probe budget in seconds.

    Priority (highest first):

    1. Env ``MRC_WINRM_PROBE_TIMEOUT_S``.
    2. Profile ``[winrm].probe_timeout_s`` (*profile_value*).
    3. Default ``MRC_WINRM_PROBE_TIMEOUT_S`` (5.0).

    A layer whose value is unset, non-numeric, non-finite (NaN/Inf) or ``<= 0``
    is ignored, so one bad knob cannot make ``open`` uncallable; when every
    layer is unusable the default applies. Never raises - a high-latency link
    needs a larger budget, not a config error. ``env=None`` reads
    ``os.environ`` (same convention as :func:`resolve_winrm_open_probe_mode`).
    """
    env_map = env if env is not None else os.environ
    env_budget = _coerce_probe_timeout_s(env_map.get(MRC_WINRM_PROBE_TIMEOUT_ENV))
    if env_budget is not None:
        return env_budget

    profile_budget = _coerce_probe_timeout_s(profile_value)
    if profile_budget is not None:
        return profile_budget

    return MRC_WINRM_PROBE_TIMEOUT_S

# Oneshot PowerShell capability probe. Last stdout line is compressed JSON
# when ConvertTo-Json exists; otherwise multi-line key=value (parser is dual-mode).
WINRM_PS_CAPABILITY_PROBE = """
$ErrorActionPreference = 'Stop'
$lm = $ExecutionContext.SessionState.LanguageMode.ToString()
$ver = $PSVersionTable.PSVersion.ToString()
$ed = $null
if ($PSVersionTable.PSEdition) { $ed = $PSVersionTable.PSEdition.ToString() }
$hasJson = [bool](Get-Command -Name ConvertTo-Json -ErrorAction SilentlyContinue)
$hasGi = [bool](Get-Command -Name Get-Item -ErrorAction SilentlyContinue)
$canFile = $false
try { [void][IO.File]; $canFile = $true } catch { $canFile = $false }
$osv = [Environment]::OSVersion.Version.ToString()
if ($hasJson) {
  @{
    ps_version = $ver
    ps_edition = $ed
    language_mode = $lm
    os_version = $osv
    has_convertto_json = $hasJson
    can_get_item = $hasGi
    can_file_io = $canFile
  } | ConvertTo-Json -Compress
} else {
  Write-Output ("ps_version=" + $ver)
  Write-Output ("ps_edition=" + $ed)
  Write-Output ("language_mode=" + $lm)
  Write-Output ("os_version=" + $osv)
  Write-Output ("has_convertto_json=" + $hasJson)
  Write-Output ("can_get_item=" + $hasGi)
  Write-Output ("can_file_io=" + $canFile)
}
""".strip()

# Raw capability keys produced by the probe (before local derive).
_WINRM_PS_RAW_KEYS: tuple[str, ...] = (
    "ps_version",
    "ps_edition",
    "language_mode",
    "os_version",
    "has_convertto_json",
    "can_get_item",
    "can_file_io",
)


def _as_bool(value: Any) -> bool:
    """Coerce probe bools from JSON or key=value text."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", "", "null", "none"):
        return False
    return bool(text)


def _parse_ps_version_tuple(value: Any) -> tuple[int, int] | None:
    """Parse ``major.minor...`` from a PSVersion string; None if unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\.(\d+)", text)
    if not m:
        m = re.match(r"^\s*(\d+)", text)
        if not m:
            return None
        return int(m.group(1)), 0
    return int(m.group(1)), int(m.group(2))


# Identity-probe stdout: dotted versions only, or a Version label with a digit.
# HTTP statuses (401 Unauthorized, 500 ...) start with a digit but are not
# PowerShell versions - they must not become ``ps_version``.
_PROBE_PS_VERSION_DOTTED_RE = re.compile(r"^\d+\.\d+")


def _is_probe_ps_version_line(line: str) -> bool:
    """True when *line* is a version-shaped identity token.

    Accepts ``5.1`` / ``5.1.19041.1`` (``^\\d+\\.\\d+``) and labels that
    contain ``Version`` plus a digit (``PSVersion 7.4``). Bare integers
    and HTTP statuses (``401 Unauthorized``, ``500 ...``) are rejected.
    Lines 40+ characters are ignored (same bound as the identity parser).
    """
    text = (line or "").strip()
    if not text or len(text) >= 40:
        return False
    if _PROBE_PS_VERSION_DOTTED_RE.match(text):
        return True
    return "Version" in text and any(c.isdigit() for c in text)


def parse_winrm_ps_probe_output(stdout: str) -> dict[str, Any]:
    """Parse capability-probe stdout into a raw field dict.

    Prefers the last non-empty line as compressed JSON. Falls back to
    multi-line ``key=value`` text when ConvertTo-Json is unavailable on the
    remote host. Unknown / unparseable input yields an empty dict (caller
    treats that as an incomplete probe).
    """
    if not stdout or not str(stdout).strip():
        return {}
    text = str(stdout).replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return {}

    # JSON mode: last non-empty line is a compressed object.
    last = lines[-1]
    if last.startswith("{") and last.endswith("}"):
        try:
            obj = json.loads(last)
        except (TypeError, ValueError, json.JSONDecodeError):
            obj = None
        if isinstance(obj, dict):
            return _normalize_winrm_ps_raw(obj)

    # key=value fallback (and whole-stdout scan if JSON failed).
    raw: dict[str, Any] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        raw[key] = value.strip()
    if raw:
        return _normalize_winrm_ps_raw(raw)
    return {}


def _normalize_winrm_ps_raw(obj: dict[str, Any]) -> dict[str, Any]:
    """Keep known probe keys; coerce bool fields; drop empty optionals."""
    out: dict[str, Any] = {}
    bool_keys = {"has_convertto_json", "can_get_item", "can_file_io"}
    for key in _WINRM_PS_RAW_KEYS:
        if key not in obj:
            continue
        val = obj[key]
        if val is None:
            continue
        if key in bool_keys:
            out[key] = _as_bool(val)
        else:
            text = str(val).strip()
            if text == "" or text.lower() in ("null", "none"):
                continue
            out[key] = text
    return out


def derive_winrm_ps_caps(raw: dict[str, Any]) -> dict[str, Any]:
    """Derive ``ps_script_fs`` / ``ps_oneshot`` / ``ps_runspace`` from probe raw.

    Rules (capability contract):

    - ``ps_script_fs``: case-insensitive FullLanguage **and** can_get_item **and**
      can_file_io **and** has_convertto_json; when PS version is parseable it
      must be >= ``MRC_WINRM_PS_FS_MIN`` (5.1). Unparseable / missing version
      does not alone reject.
    - ``ps_oneshot``: language_mode present and not NoLanguage.
    - ``ps_runspace``: FullLanguage with a successful language_mode read.

    Missing language_mode yields all three derived flags false here. Live
    incomplete probes (timeout, unparseable, remote error) use
    :func:`_incomplete_winrm_ps` instead, which keeps ``ps_oneshot`` true so
    oneshot exec is not blocked when language mode is simply unknown.
    """
    out = dict(raw) if raw else {}
    language_mode = str(out.get("language_mode") or "").strip()
    lm_lower = language_mode.lower()
    full_lang = lm_lower == "fulllanguage"
    no_lang = lm_lower == "nolanguage"
    has_lm = bool(language_mode)

    can_get_item = _as_bool(out.get("can_get_item"))
    can_file_io = _as_bool(out.get("can_file_io"))
    has_json = _as_bool(out.get("has_convertto_json"))

    version_ok = True
    parsed_ver = _parse_ps_version_tuple(out.get("ps_version"))
    if parsed_ver is not None:
        version_ok = parsed_ver >= MRC_WINRM_PS_FS_MIN

    ps_script_fs = bool(
        has_lm
        and full_lang
        and can_get_item
        and can_file_io
        and has_json
        and version_ok
    )
    # Session oneshot is usable when the probe reported a language mode other
    # than NoLanguage; missing language_mode means incomplete -> false.
    ps_oneshot = bool(has_lm and not no_lang)
    # Runspace open assumed viable only under FullLanguage.
    ps_runspace = bool(has_lm and full_lang)

    out["ps_script_fs"] = ps_script_fs
    out["ps_oneshot"] = ps_oneshot
    out["ps_runspace"] = ps_runspace
    return out


def _incomplete_winrm_ps(*, error: str | None = None) -> dict[str, Any]:
    """winrm_ps when the capability probe was attempted but incomplete.

    Script FS and runspace stay closed (unknown language mode is not enough
    to open those surfaces). Oneshot exec stays allowed (``ps_oneshot=True``):
    an incomplete probe must not hard-gate user commands the way a known
    ``NoLanguage`` result does. Callers set ``ps_probe=failed`` and optional
    ``error`` so Agent/meta can show the probe did not complete.
    """
    out: dict[str, Any] = {
        "ps_script_fs": False,
        "ps_oneshot": True,
        "ps_runspace": False,
        "ps_probe": "failed",
    }
    if error:
        out["error"] = error
    return out

