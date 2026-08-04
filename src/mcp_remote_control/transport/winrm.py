"""WinRM transport built on pypsrp (oneshot exec, persistent runspace, FS hook).

Owns profile→pypsrp auth kwargs assembly, remote command/argv execution, and
PowerShell runspace open/invoke/close. A connector callable may supply the
session object (default constructs a real ``pypsrp.client.Client``).

Enterprise auth mapping (profile / transport → pypsrp ``Client`` / ``WSMan`` kwargs)
------------------------------------------------------------------------------------

| Profile ``[auth].method`` / protocol | pypsrp ``auth=`` | Extra kwargs |
|--------------------------------------|------------------|--------------|
| ``password`` (material) + default    | ``ntlm``         | ``username``, ``password`` |
| ``ntlm``                             | ``ntlm``         | ``username``, ``password`` |
| ``basic``                            | ``basic``        | ``username``, ``password`` |
| ``negotiate``                        | ``negotiate``    | ``username``, ``password`` (optional) |
| ``kerberos``                         | ``kerberos``     | optional password; ``negotiate_hostname_override``, ``negotiate_service``, ``negotiate_delegate`` |
| ``credssp``                          | ``credssp``      | ``username``, ``password``; ``credssp_auth_mechanism``, ``credssp_disable_tlsv1_2``, ``credssp_minimum_version`` |
| ``certificate``                      | ``certificate``  | ``certificate_pem``, ``certificate_key_pem``, optional ``certificate_key_password``; **requires SSL** |

Paths in the profile (``cert_path``, ``cert_key_path``, ``password_path``) are
resolved to filesystem paths / loaded secret *values* only inside the connector
kwargs builder — never into Agent-track fields or ``repr``.

Timed-out oneshot pypsrp calls may leave server-side runspaces; callers should
``endpoint close`` + reopen when timeouts recur (see ``_call_remote``).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError

# Auth protocol names accepted by assemble_pypsrp_kwargs / Client(auth=…).
WINRM_AUTH_PROTOCOLS: frozenset[str] = frozenset(
    {
        "basic",
        "certificate",
        "credssp",
        "kerberos",
        "negotiate",
        "ntlm",
    }
)


@dataclass
class RunspaceResult:
    """Result of a persistent PowerShell runspace invoke (ps tool)."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    location: str | None = None
    had_errors: bool = False
    # True when the invoke hit the caller's wall-clock timeout. The pipeline is
    # stopped/disposed but the runspace pool is left usable for later invokes.
    # Same meaning as ExecResult.timed_out on run_command / run_argv.
    timed_out: bool = False


# Connector: kwargs → session/client handle (sync object).
WinRMConnector = Callable[..., Any]


def parse_spn(spn: str | None) -> tuple[str | None, str | None]:
    """Split ``SERVICE/host`` SPN into (service, hostname_override).

    Bare hostname (no ``/``) → (None, host). Empty/None → (None, None).
    """
    if not spn or not str(spn).strip():
        return None, None
    text = str(spn).strip()
    if "/" in text:
        service, _, host = text.partition("/")
        service = service.strip() or None
        host = host.strip() or None
        return service, host
    return None, text


def assemble_pypsrp_kwargs(
    *,
    host: str,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    auth: str = "ntlm",
    ssl: bool = False,
    cert_validation: bool = True,
    encryption: str = "auto",
    connect_timeout: float | None = None,
    operation_timeout: int | None = None,
    read_timeout: int | None = None,
    # Certificate auth: PEM *paths* only (never PEM body in kwargs assembly).
    certificate_pem: str | None = None,
    certificate_key_pem: str | None = None,
    certificate_key_password: str | None = None,
    # Kerberos / negotiate SPN and host override.
    spn: str | None = None,
    negotiate_hostname_override: str | None = None,
    negotiate_service: str | None = None,
    negotiate_delegate: bool | None = None,
    # CredSSP-only options.
    credssp_auth_mechanism: str | None = None,
    credssp_disable_tlsv1_2: bool | None = None,
    credssp_minimum_version: int | None = None,
) -> dict[str, Any]:
    """Build connector / pypsrp ``Client`` kwargs for a WinRM auth method.

    Pure assembly (no network). Raises ``TransportError`` for invalid combos
    (unsupported protocol, certificate without SSL, encryption incompatibilities,
    missing password for basic/credssp). Secret values (password, cert key
    password) may appear in the returned dict for the connector only — callers
    must not log them or place them on the Agent track.
    """
    if not host:
        raise TransportError("CONNECT_FAILED", "winrm host is required")

    protocol = (auth or "ntlm").strip().lower()
    if protocol not in WINRM_AUTH_PROTOCOLS:
        raise TransportError(
            "INVALID_ARG",
            f"unsupported winrm auth protocol {protocol!r}; "
            f"expected one of {sorted(WINRM_AUTH_PROTOCOLS)}",
        )

    if protocol == "certificate":
        if not certificate_pem or not certificate_key_pem:
            raise TransportError(
                "INVALID_ARG",
                "certificate auth requires certificate_pem and "
                "certificate_key_pem paths",
            )
        if not ssl:
            raise TransportError(
                "INVALID_ARG",
                "certificate auth requires ssl/https",
            )

    enc = (encryption or "auto").strip().lower()
    if enc not in ("auto", "always", "never"):
        raise TransportError(
            "INVALID_ARG",
            f"encryption must be auto|always|never, got {encryption!r}",
        )
    if enc == "always" and protocol in ("basic", "certificate"):
        raise TransportError(
            "INVALID_ARG",
            f"message encryption=always is incompatible with auth={protocol!r}",
        )

    if protocol in ("basic", "credssp") and not password:
        raise TransportError(
            "AUTH_FAILED",
            f"{protocol} auth requires a password (password_path/password_env)",
        )

    kwargs: dict[str, Any] = {
        "host": str(host),
        "username": username,
        "password": password,
        "ssl": bool(ssl),
        "auth": protocol,
        "cert_validation": bool(cert_validation),
        "encryption": enc,
    }
    if port is not None:
        kwargs["port"] = int(port)

    if connect_timeout is not None:
        kwargs["connect_timeout"] = float(connect_timeout)
    if operation_timeout is not None:
        kwargs["operation_timeout"] = int(operation_timeout)
    if read_timeout is not None:
        kwargs["read_timeout"] = int(read_timeout)

    if protocol == "certificate":
        kwargs["certificate_pem"] = str(certificate_pem)
        kwargs["certificate_key_pem"] = str(certificate_key_pem)
        if certificate_key_password is not None:
            kwargs["certificate_key_password"] = certificate_key_password
        # Certificate HTTP auth does not use password; clear it. Username may
        # still appear in Agent meta for display.
        kwargs["password"] = None

    # Map SPN / explicit overrides onto pypsrp negotiate_* kwargs.
    spn_service, spn_host = parse_spn(spn)
    host_override = negotiate_hostname_override or spn_host
    service = negotiate_service or spn_service
    if host_override:
        kwargs["negotiate_hostname_override"] = str(host_override)
    if service:
        kwargs["negotiate_service"] = str(service)
    if negotiate_delegate is not None:
        kwargs["negotiate_delegate"] = bool(negotiate_delegate)

    if protocol == "credssp":
        if credssp_auth_mechanism:
            kwargs["credssp_auth_mechanism"] = str(credssp_auth_mechanism)
        if credssp_disable_tlsv1_2 is not None:
            kwargs["credssp_disable_tlsv1_2"] = bool(credssp_disable_tlsv1_2)
        if credssp_minimum_version is not None:
            kwargs["credssp_minimum_version"] = int(credssp_minimum_version)

    return kwargs


def redact_connect_kwargs_for_log(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of connect kwargs safe for logs / Agent track."""
    sensitive = {
        "password",
        "certificate_key_password",
        "passphrase",
        "certificate_pem_body",
        "certificate_key_pem_body",
    }
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in sensitive or key.endswith("_password"):
            out[key] = "***" if value is not None else None
        else:
            out[key] = value
    return out


# Minimum PS version for built-in script FS (major, minor). Below this,
# ps_script_fs is forced false when the version string is parseable.
MRC_WINRM_PS_FS_MIN: tuple[int, int] = (5, 1)

# Wall-clock budget for the post-connect capability / identity probe.
# Short so a hung remote PowerShell cannot stall connect indefinitely.
# On timeout the probe is marked incomplete (partial); connect still succeeds.
MRC_WINRM_PROBE_TIMEOUT_S: float = 5.0

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
    """Parse ``major.minor…`` from a PSVersion string; None if unparseable."""
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
    # than NoLanguage; missing language_mode means incomplete → false.
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


def _decode_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe_msg(exc: BaseException) -> str:
    """Short exception text for transport errors (truncated; no secret material)."""
    text = str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:197] + "..."
    return text


def _looks_like_auth_failure(exc: BaseException) -> bool:
    """Heuristic: map connect exceptions to AUTH_FAILED vs CONNECT_FAILED.

    Network markers (timeout, refused, DNS) win over auth-looking tokens so a
    firewall drop is not reported as bad credentials.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    net_markers = (
        "refused",
        "timeout",
        "timed out",
        "unreachable",
        "name or service not known",
        "nodename nor servname",
        "getaddrinfo",
        "connection reset",
        "network is unreachable",
    )
    if any(m in text for m in net_markers):
        return False
    auth_markers = (
        "auth",
        "unauthor",
        "401",
        "403",
        "credential",
        "logon failure",
        "access is denied",
        "login failed",
        "password",
        "ntlm",
        "negotiate",
        "denied",
    )
    return any(m in text for m in auth_markers)


def _win_quote(arg: str) -> str:
    """Minimal Windows command-line quoting for joined argv strings.

    Note on ``%``: ``cmd.exe`` expands ``%VAR%`` even inside double quotes, and
    there is no reliable command-line escape for ``%`` (``^%`` leaves a literal
    ``^`` and breaks the value; ``%%`` only works inside batch files). This
    quoter is therefore reserved for shell-string ``run_command`` (where ``%``
    expansion is the caller's intent). The no-shell ``run_argv`` contract is
    satisfied via the PowerShell call-operator path (``& exe @(args)``) which
    never goes through ``cmd.exe`` — see :meth:`WinRMTransport.run_argv`.
    """
    if not arg:
        return '""'
    if any(c in arg for c in ' \t"&|<>^'):
        return '"' + arg.replace('"', r'\"') + '"'
    return arg


def _wrap_ps_with_cwd(script: str, cwd: str | None) -> str:
    if not cwd:
        return script
    # Escape single quotes for PowerShell single-quoted string.
    safe = str(cwd).replace("'", "''")
    return f"Set-Location -LiteralPath '{safe}'; {script}"


def _inject_ps_env(script: str, env: dict[str, str] | None) -> str:
    """Prepend process-scoped env setters into a PowerShell script.

    Production oneshot always passes ``environment=`` on the Protocol surface
    and never calls this helper. Kept for specialized adapters or test doubles
    that accept ``environment=`` but apply it by rewriting the script payload.
    """
    if not env:
        return script
    setters: list[str] = []
    for name, value in env.items():
        n = str(name).replace("'", "''")
        v = "" if value is None else str(value).replace("'", "''")
        setters.append(f"[Environment]::SetEnvironmentVariable('{n}','{v}','Process')")
    return "; ".join(setters) + "; " + script


def _inject_cmd_env(command: str, env: dict[str, str] | None) -> str:
    """Prefix ``set "NAME=VALUE"`` for each env entry onto a cmd string.

    Production oneshot always passes ``environment=`` and never calls this
    helper. Limitation when used: a ``"`` inside VALUE can truncate the ``set``
    (cmd closes the quote early). Real pypsrp clients never need this path.
    """
    if not env:
        return command
    prefixes: list[str] = []
    for name, value in env.items():
        n = str(name)
        v = "" if value is None else str(value)
        # Quotes protect spaces/special characters from the cmd parser.
        prefixes.append(f'set "{n}={v}"')
    return " && ".join(prefixes) + " && " + command


def _wrap_cmd_with_cwd(command: str, cwd: str | None) -> str:
    if not cwd:
        return command
    return f"cd /d {_win_quote(cwd)} && {command}"


def _bound(obj: Any, name: str) -> Callable[..., Any] | None:
    """Return a callable attribute, or None when missing/non-callable."""
    fn = getattr(obj, name, None)
    return fn if callable(fn) else None


class AdaptedWinRMSession:
    """Stable WinRM session surface for production transport paths.

    Built once at connect from a connector return value. Capability discovery
    (which methods exist) happens only here; ``WinRMTransport`` then calls this
    surface with fixed kwargs (``environment=`` on oneshot exec; wall-clock
    timeout outside the session API).

    Real ``pypsrp.client.Client`` instances are wrapped by
    :class:`PypsrpClientAdapter` in :func:`default_winrm_connector` so the
    library call shape is fixed. Test doubles implement the same methods
    directly (accept ``environment=`` even when unused).
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.cwd = getattr(raw, "cwd", None)
        self.home = getattr(raw, "home", None)
        self.wsman = getattr(raw, "wsman", None)
        self._run_command = _bound(raw, "run_command")
        self._run_argv = _bound(raw, "run_argv")
        self._execute_ps = _bound(raw, "execute_ps")
        self._execute_cmd = _bound(raw, "execute_cmd")
        self._open_runspace = _bound(raw, "open_runspace")
        self._open_fs = _bound(raw, "open_fs")
        self._close = _bound(raw, "close")
        self._copy = _bound(raw, "copy")
        self._fetch = _bound(raw, "fetch")
        # Seed probe attributes when the raw session already exposes them.
        # Capability bits (language_mode / cmdlet flags) may be pre-seeded by
        # adapters; identity seeds (os/shell/ps_version) alone never imply
        # ps_script_fs=true — that requires capability fields or a live probe.
        self._meta_seeds: dict[str, Any] = {}
        for key in (
            "os",
            "shell",
            "ps_version",
            "probe_status",
            "probe_error",
            "language_mode",
            "ps_edition",
            "os_version",
            "has_convertto_json",
            "can_get_item",
            "can_file_io",
        ):
            val = getattr(raw, key, None)
            if val is not None:
                self._meta_seeds[key] = val
        # File-store methods directly on the session (duck-typed file client).
        self._file_stat = _bound(raw, "stat")
        self._file_listdir = _bound(raw, "listdir")
        self._file_read = _bound(raw, "read_file")
        self._file_write = _bound(raw, "write_file")

    @property
    def has_run_command(self) -> bool:
        return self._run_command is not None

    @property
    def has_run_argv(self) -> bool:
        return self._run_argv is not None

    @property
    def has_execute_ps(self) -> bool:
        return self._execute_ps is not None

    @property
    def has_execute_cmd(self) -> bool:
        return self._execute_cmd is not None

    @property
    def has_open_runspace(self) -> bool:
        return self._open_runspace is not None

    @property
    def has_open_fs(self) -> bool:
        return self._open_fs is not None

    @property
    def has_wsman(self) -> bool:
        return self.wsman is not None

    @property
    def is_file_client(self) -> bool:
        return bool(
            self._file_stat
            and self._file_listdir
            and self._file_read
            and self._file_write
        )

    @property
    def can_build_pypsrp_fs(self) -> bool:
        return bool(self._copy or self._fetch or self._execute_ps)

    def close(self) -> None:
        if self._close is not None:
            self._close()

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        if self._run_command is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no run_command",
            )
        return self._run_command(command, cwd=cwd, timeout_s=timeout_s, env=env)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        if self._run_argv is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no run_argv",
            )
        return self._run_argv(argv, cwd=cwd, timeout_s=timeout_s, env=env)

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Oneshot PowerShell; always accepts ``environment`` (Protocol contract)."""
        if self._execute_ps is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no execute_ps",
            )
        return self._execute_ps(script, environment=environment)

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Oneshot cmd; always accepts ``environment`` (Protocol contract)."""
        if self._execute_cmd is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no execute_cmd",
            )
        return self._execute_cmd(command, environment=environment)

    def open_runspace(self) -> Any:
        if self._open_runspace is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session cannot open a PowerShell runspace",
            )
        return self._open_runspace()

    def open_fs(self) -> Any:
        if self._open_fs is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no open_fs",
            )
        return self._open_fs()

    def copy(self, local: str, remote: str) -> None:
        if self._copy is None:
            raise TransportError("UNSUPPORTED", "winrm session has no copy")
        self._copy(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        if self._fetch is None:
            raise TransportError("UNSUPPORTED", "winrm session has no fetch")
        self._fetch(remote, local)

    def seed_attr(self, key: str) -> Any:
        return self._meta_seeds.get(key)


def adapt_winrm_session(raw: Any) -> AdaptedWinRMSession:
    """Normalize a connector return value to :class:`AdaptedWinRMSession`.

    Idempotent: already-adapted sessions are returned as-is.
    """
    if isinstance(raw, AdaptedWinRMSession):
        return raw
    return AdaptedWinRMSession(raw)


class PypsrpClientAdapter:
    """Adapter: real pypsrp ``Client`` → oneshot Protocol surface.

    Knows the fixed pypsrp call shape (``environment=`` always supported;
    no per-call timeout kwarg). Confines library-specific kwargs here so
    production never uses ``inspect.signature``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.wsman = getattr(client, "wsman", None)
        self.cwd = getattr(client, "cwd", None)
        self.home = getattr(client, "home", None)

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        return self._client.execute_ps(script, environment=environment)

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        return self._client.execute_cmd(command, environment=environment)

    def copy(self, local: str, remote: str) -> None:
        self._client.copy(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        self._client.fetch(remote, local)


def default_winrm_connector(**kwargs: Any) -> Any:
    """Construct a real pypsrp ``Client`` wrapped in :class:`PypsrpClientAdapter`.

    Expected kwargs match :func:`assemble_pypsrp_kwargs` / transport connect:
    host, port, username, password, auth, ssl, cert_validation, encryption,
    connect_timeout, operation_timeout, read_timeout, certificate_*,
    negotiate_*, credssp_*. Maps ``connect_timeout`` → pypsrp
    ``connection_timeout`` (seconds, int).
    """
    from pypsrp.client import Client  # lazy: keep module import cheap without pypsrp

    host = kwargs.get("host")
    if not host:
        raise TransportError(
            "CONNECT_FAILED",
            "winrm host is required",
        )

    client_kwargs: dict[str, Any] = {
        "username": kwargs.get("username"),
        "password": kwargs.get("password"),
        "ssl": bool(kwargs.get("ssl", False)),
        "auth": kwargs.get("auth") or "ntlm",
        "cert_validation": bool(kwargs.get("cert_validation", True)),
        "encryption": kwargs.get("encryption") or "auto",
    }
    port = kwargs.get("port")
    if port is not None:
        client_kwargs["port"] = int(port)

    connect_timeout = kwargs.get("connect_timeout")
    if connect_timeout is not None:
        # pypsrp Client uses connection_timeout in whole seconds.
        client_kwargs["connection_timeout"] = max(int(float(connect_timeout)), 1)

    op_timeout = kwargs.get("operation_timeout")
    if op_timeout is not None:
        client_kwargs["operation_timeout"] = max(int(float(op_timeout)), 1)

    read_timeout = kwargs.get("read_timeout")
    if read_timeout is not None:
        client_kwargs["read_timeout"] = max(int(float(read_timeout)), 1)

    # Pass through enterprise auth paths/flags (and secret values when present).
    for key in (
        "certificate_pem",
        "certificate_key_pem",
        "certificate_key_password",
        "negotiate_hostname_override",
        "negotiate_service",
        "negotiate_delegate",
        "credssp_auth_mechanism",
        "credssp_disable_tlsv1_2",
        "credssp_minimum_version",
    ):
        if key in kwargs and kwargs[key] is not None:
            client_kwargs[key] = kwargs[key]

    return PypsrpClientAdapter(Client(str(host), **client_kwargs))


def _coerce_exec_result(raw: Any, *, default_cwd: str | None) -> ExecResult:
    """Normalize session API / pypsrp return shapes into ``ExecResult``."""
    if isinstance(raw, ExecResult):
        if raw.cwd is None and default_cwd is not None:
            return ExecResult(
                exit_code=raw.exit_code,
                stdout=raw.stdout,
                stderr=raw.stderr,
                cwd=default_cwd,
                timed_out=raw.timed_out,
            )
        return raw

    if raw is None:
        raise TransportError("EXEC_FAILED", "remote run returned no result")

    # Duck-type objects exposing exit_code / exit_status / returncode.
    if hasattr(raw, "exit_code") or hasattr(raw, "exit_status") or hasattr(raw, "returncode"):
        exit_code = getattr(raw, "exit_code", None)
        if exit_code is None:
            exit_code = getattr(raw, "exit_status", None)
        if exit_code is None:
            exit_code = getattr(raw, "returncode", 0)
        timed_out = bool(getattr(raw, "timed_out", False))
        cwd = getattr(raw, "cwd", None) or default_cwd
        return ExecResult(
            exit_code=int(exit_code) if exit_code is not None else 0,
            stdout=_decode_stream(getattr(raw, "stdout", "")),
            stderr=_decode_stream(getattr(raw, "stderr", "")),
            cwd=cwd,
            timed_out=timed_out,
        )

    # pypsrp execute_cmd → (stdout, stderr, rc); also (exit, stdout[, stderr]).
    if isinstance(raw, tuple) and len(raw) >= 2:
        if len(raw) >= 3 and isinstance(raw[2], (int, float)):
            return ExecResult(
                exit_code=int(raw[2]),
                stdout=_decode_stream(raw[0]),
                stderr=_decode_stream(raw[1]),
                cwd=default_cwd,
            )
        exit_code = int(raw[0])
        stdout = _decode_stream(raw[1])
        stderr = _decode_stream(raw[2]) if len(raw) > 2 else ""
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=default_cwd,
        )

    raise TransportError(
        "EXEC_FAILED",
        f"unrecognized remote run result type: {type(raw).__name__}",
    )



# Sentinel prefix for the location probe appended to a runspace invoke pipeline.
# Chosen to be collision-free with realistic PowerShell output.
_LOCATION_MARKER = "__MRC_PS_CWD_MARKER__"


class InvokeRunspaceAdapter:
    """Adapter for handles that already implement ``invoke`` / ``close``.

    Wraps mock and custom runspaces so :meth:`WinRMTransport.open_runspace`
    always returns a stable RunspaceHandle surface. ``runspace_invoke`` only
    calls this API — no pool heuristics on the hot path.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        """Underlying handle (tests / diagnostics)."""
        return self._inner

    @property
    def location(self) -> str | None:
        return getattr(self._inner, "location", None)

    def invoke(self, script: str) -> Any:
        return self._inner.invoke(script)

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def stop(self) -> None:
        """Best-effort interrupt when the inner handle exposes ``stop()``."""
        stop = getattr(self._inner, "stop", None)
        if callable(stop):
            stop()


class PypsrpPoolRunspaceAdapter:
    """Adapter: pypsrp ``RunspacePool`` → RunspaceHandle (``invoke`` / ``close`` / ``stop``).

    ``invoke`` builds a ``PowerShell`` pipeline with a folded location probe
    (sentinel-tagged ``Get-Location``) so one WSMan round-trip returns both
    output and cwd. ``stop`` disposes the *active pipeline only* so the pool
    stays open after a wall-clock timeout.
    """

    def __init__(self, pool: Any, *, default_location: str | None = None) -> None:
        self._pool = pool
        self._default_location = default_location
        self._active_ps: Any = None
        self.location: str | None = default_location or getattr(pool, "location", None)

    @property
    def inner(self) -> Any:
        """Underlying RunspacePool (tests / diagnostics)."""
        return self._pool

    def invoke(self, script: str) -> RunspaceResult:
        try:
            from pypsrp.powershell import PowerShell
        except ImportError as exc:  # pragma: no cover - hard dep in practice
            raise TransportError(
                "UNSUPPORTED",
                "pypsrp is required for PowerShell runspaces",
            ) from exc

        probe_script = (
            f"{script}\nWrite-Output ('{_LOCATION_MARKER}' + (Get-Location).Path)"
        )
        ps = PowerShell(self._pool)
        # Keep the active pipeline visible for stop() for the whole call, including
        # wall-clock timeout (bridge aborts the await while this still runs).
        self._active_ps = ps
        ps.add_script(probe_script)
        output = ps.invoke()
        location, user_output = _split_location_output(output, _LOCATION_MARKER)
        stdout = _format_ps_output(user_output)
        stderr = _format_ps_errors(ps)
        had_errors = bool(getattr(ps, "had_errors", False)) or bool(stderr)
        exit_code = 1 if had_errors else 0
        if location:
            self.location = location
        return RunspaceResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            location=location or self.location or self._default_location,
            had_errors=had_errors,
        )

    def stop(self) -> None:
        """Bounded stop+close of the active pipeline (not the pool)."""
        _safe_stop_pipeline(self._active_ps)

    def close(self) -> None:
        closer = getattr(self._pool, "close", None)
        if callable(closer):
            closer()


def _adapt_runspace_handle(
    handle: Any,
    *,
    default_location: str | None = None,
) -> Any:
    """Normalize any ``open_runspace`` return value to a RunspaceHandle adapter.

    Classification is one-shot at open time:
    - already an adapter → returned as-is
    - has callable ``invoke`` → :class:`InvokeRunspaceAdapter` (mocks / custom)
    - otherwise → :class:`PypsrpPoolRunspaceAdapter` (pypsrp ``RunspacePool``)
    """
    if isinstance(handle, (InvokeRunspaceAdapter, PypsrpPoolRunspaceAdapter)):
        return handle
    invoker = getattr(handle, "invoke", None)
    if callable(invoker):
        return InvokeRunspaceAdapter(handle)
    return PypsrpPoolRunspaceAdapter(handle, default_location=default_location)


class WinRMTransport(BaseTransport):
    """WinRM session handle over a Protocol-compatible session object.

    Parameters
    ----------
    connector:
        Factory that receives :meth:`connect_kwargs` and returns a session.
        Defaults to :func:`default_winrm_connector` (``PypsrpClientAdapter``
        around a real pypsrp ``Client``).

    On connect the raw session is wrapped in :class:`AdaptedWinRMSession`,
    which freezes capability discovery. Production paths call that stable
    surface only:

    - high-level ``run_command`` / ``run_argv`` when present (same wall-clock
      timeout → ``timed_out`` mapping as oneshot)
    - oneshot ``execute_ps`` / ``execute_cmd`` with fixed ``environment=``
    - ``open_runspace()`` always returns a RunspaceHandle adapter (invoke API
      or pypsrp ``RunspacePool`` wrapped once at open)
    - ``open_fs()`` / file-client methods / pypsrp copy+fetch+ps
    """

    name = "winrm"

    def __init__(
        self,
        *,
        host: str,
        port: int = 5985,
        username: str,
        password: str | None = None,
        auth: str = "ntlm",
        ssl: bool = False,
        # Secure default: validate TLS peer certificates unless the caller
        # explicitly passes cert_validation=False (e.g. lab hosts with a
        # private CA not installed on the client).
        cert_validation: bool = True,
        connect_timeout_ms: int = 15000,
        operation_timeout_s: int | None = None,
        read_timeout_s: int | None = None,
        encryption: str = "auto",
        connector: WinRMConnector | None = None,
        # Enterprise auth: PEM paths as strings; secret values loaded by caller.
        certificate_pem: str | None = None,
        certificate_key_pem: str | None = None,
        certificate_key_password: str | None = None,
        spn: str | None = None,
        negotiate_hostname_override: str | None = None,
        negotiate_service: str | None = None,
        negotiate_delegate: bool | None = None,
        credssp_auth_mechanism: str | None = None,
        credssp_disable_tlsv1_2: bool | None = None,
        credssp_minimum_version: int | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = int(port)
        self.username = username
        # Password held only for connect; never logged or put in meta/repr.
        self._password = password
        self.auth = auth or "ntlm"
        self.ssl = bool(ssl)
        self.cert_validation = bool(cert_validation)
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.operation_timeout_s = operation_timeout_s
        self.read_timeout_s = read_timeout_s
        self.encryption = encryption or "auto"
        self._connector: WinRMConnector = connector or default_winrm_connector
        self._session: AdaptedWinRMSession | None = None
        # Enterprise auth options (paths and non-secret flags).
        self.certificate_pem = certificate_pem
        self.certificate_key_pem = certificate_key_pem
        self._certificate_key_password = certificate_key_password
        self.spn = spn
        self.negotiate_hostname_override = negotiate_hostname_override
        self.negotiate_service = negotiate_service
        self.negotiate_delegate = negotiate_delegate
        self.credssp_auth_mechanism = credssp_auth_mechanism
        self.credssp_disable_tlsv1_2 = credssp_disable_tlsv1_2
        self.credssp_minimum_version = credssp_minimum_version

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (
            f"WinRMTransport(host={self.host!r}, port={self.port!r}, "
            f"username={self.username!r}, auth={self.auth!r}, "
            f"ssl={self.ssl!r})"
        )

    def connect_kwargs(self) -> dict[str, Any]:
        """Assemble pypsrp/connector kwargs for this transport (no network).

        Includes Kerberos / CredSSP / certificate wiring so operators and
        callers can inspect the effective Client parameters without connecting.
        """
        timeout_s = max(self.connect_timeout_ms, 1) / 1000.0
        return assemble_pypsrp_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self._password,
            auth=self.auth,
            ssl=self.ssl,
            cert_validation=self.cert_validation,
            encryption=self.encryption,
            connect_timeout=timeout_s,
            operation_timeout=self.operation_timeout_s,
            read_timeout=self.read_timeout_s,
            certificate_pem=self.certificate_pem,
            certificate_key_pem=self.certificate_key_pem,
            certificate_key_password=self._certificate_key_password,
            spn=self.spn,
            negotiate_hostname_override=self.negotiate_hostname_override,
            negotiate_service=self.negotiate_service,
            negotiate_delegate=self.negotiate_delegate,
            credssp_auth_mechanism=self.credssp_auth_mechanism,
            credssp_disable_tlsv1_2=self.credssp_disable_tlsv1_2,
            credssp_minimum_version=self.credssp_minimum_version,
        )

    def connect(self) -> None:
        if self._connected and self._session is not None:
            return
        try:
            connect_args = self.connect_kwargs()
        except TransportError:
            raise
        try:
            session = self._connector(**connect_args)
        except TransportError:
            raise
        except Exception as exc:
            code = "AUTH_FAILED" if _looks_like_auth_failure(exc) else "CONNECT_FAILED"
            raise TransportError(
                code,
                _safe_msg(exc),
                details={"host": self.host, "port": self.port, "auth": self.auth},
            ) from exc

        if session is None:
            raise TransportError(
                "CONNECT_FAILED",
                "winrm connector returned no session",
                details={"host": self.host, "port": self.port},
            )

        adapted = adapt_winrm_session(session)
        self._session = adapted
        self._connected = True
        # Optional cwd/home seeds when the session object already exposes them.
        self.cwd = adapted.cwd or self.cwd
        self.home = adapted.home or self.home
        self.meta = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth": self.auth,
            "ssl": self.ssl,
        }
        if self.certificate_pem:
            # Path only — never PEM body.
            self.meta["cert_path"] = self.certificate_pem
        if self.spn:
            self.meta["spn"] = self.spn
        # Best-effort probe seeds captured at adapt time (no network).
        for key, val in adapted._meta_seeds.items():
            self.meta[key] = val
        # Secrets stay on private transport fields only (never meta/repr/Agent).

    def close(self) -> None:
        session = self._session
        self._session = None
        self._connected = False
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass

    @property
    def session(self) -> Any:
        """Adapted session (:class:`AdaptedWinRMSession`); None if closed."""
        return self._session

    def collect_probe(self) -> dict[str, Any]:
        """Lightweight post-connect probe for ``endpoint.probe``.

        Prefer already-seeded meta / session attributes. When the session
        exposes ``execute_ps`` and did NOT already supply identity seeds
        (os/shell/ps_version), run the one-RTT PowerShell capability probe
        (language mode, cmdlet presence, file IO) and store the derived result
        in ``meta["winrm_ps"]``. Sessions that seed identity without a
        capability surface keep the historical skip (no remote probe,
        ``winrm_ps`` absent, gates permissive).

        Failures yield ``status=partial`` without raising (connect already
        succeeded). Identity seeds alone (os/shell/ps_version) never set
        ``ps_script_fs=true``; that requires capability fields or a successful
        capability probe. A session without ``execute_ps`` cannot be probed
        remotely, so seeds are reported as-is and FS stays ungated only in
        that no-probe-surface case. ``probe=False`` on open skips this whole
        method upstream (explicit opt-out).
        """
        data: dict[str, Any] = {}
        session: AdaptedWinRMSession | None = self._session
        for key in ("os", "shell", "ps_version", "home", "cwd"):
            if key == "home" and self.home:
                data["home"] = self.home
                continue
            if key == "cwd" and self.cwd:
                data["pwd"] = self.cwd
                continue
            val = self.meta.get(key)
            if val is not None:
                data[key] = val
            elif session is not None:
                seed = session.seed_attr(key)
                if seed is not None:
                    data[key] = seed

        # Capability seeds may live on meta (from adapt) or session seeds.
        self._merge_winrm_ps_into_probe(data, session)

        if self.meta.get("probe_status") == "partial":
            data["status"] = "partial"
            err = self.meta.get("probe_error")
            if err:
                data["error"] = err
            return data

        # Session already supplied os/shell/ps_version — no legacy version probe.
        # (Capability probe was handled above when applicable.)
        if data.get("os") or data.get("shell") or data.get("ps_version"):
            data.setdefault("status", "ok")
            data.setdefault("os", data.get("os") or "windows")
            return data

        if session is None:
            data["status"] = "partial"
            data["error"] = "not connected"
            return data

        # Prefer high-level run_command when present; else pypsrp execute_ps.
        if session.has_run_command:
            try:
                raw = session.run_command(
                    "[Environment]::OSVersion.VersionString; "
                    "$PSVersionTable.PSVersion.ToString(); "
                    "$env:USERPROFILE; $PWD.Path",
                    cwd=None,
                    timeout_s=MRC_WINRM_PROBE_TIMEOUT_S,
                    env=None,
                )
                result = _coerce_exec_result(raw, default_cwd=self.cwd)
                return self._parse_probe_stdout(result.stdout, base=data)
            except Exception as exc:
                data["status"] = "partial"
                data["error"] = _safe_msg(exc)
                return data

        if session.has_execute_ps:
            try:
                script = (
                    "$PSVersionTable.PSVersion.ToString(); "
                    "$env:USERPROFILE; "
                    "(Get-Location).Path; "
                    "[Environment]::OSVersion.VersionString"
                )
                stdout = self._execute_ps_stdout(session, script)
                return self._parse_probe_stdout(stdout, base=data)
            except Exception as exc:
                data["status"] = "partial"
                data["error"] = _safe_msg(exc)
                return data

        # No exec surface — report transport seeds only.
        data.setdefault("status", "ok")
        data.setdefault("os", "windows")
        return data

    def _merge_winrm_ps_into_probe(
        self,
        data: dict[str, Any],
        session: AdaptedWinRMSession | None,
    ) -> None:
        """Resolve ``winrm_ps`` (seeds and/or remote probe) into *data* and meta.

        Resolution order:
        1. Existing ``meta["winrm_ps"]`` (idempotent re-probe)
        2. Capability seeds on session/meta (language_mode / cmdlet flags)
        3. Remote oneshot via ``execute_ps`` when the session exposes it AND
           no identity seeds (os/shell/ps_version) are already present.
           Identity seeds alone mean the adapter supplied host identity
           without a capability surface; the historical skip leaves
           ``meta["winrm_ps"]`` absent (gates permissive). Production
           adapters that do not seed identity always probe.
        """
        existing = self.meta.get("winrm_ps")
        if isinstance(existing, dict) and existing:
            self._apply_winrm_ps(data, existing)
            return

        seeded_raw = self._capability_seeds_raw(session)
        if seeded_raw.get("language_mode") is not None or any(
            k in seeded_raw for k in ("can_get_item", "can_file_io", "has_convertto_json")
        ):
            derived = derive_winrm_ps_caps(seeded_raw)
            self.meta["winrm_ps"] = derived
            self._apply_winrm_ps(data, derived)
            return

        if session is None or not session.has_execute_ps:
            # No live probe surface. Do not invent ps_script_fs=true.
            return
        # Identity seeds (os/shell/ps_version) already present mean the
        # adapter supplied host identity without a capability surface. Keep
        # the historical skip: do not run a remote capability probe, leave
        # winrm_ps absent so downstream gates stay permissive. Production
        # adapters that do not seed identity fall through to the probe below.
        # The explicit opt-out is probe=False on open, which skips
        # collect_probe entirely upstream.
        if data.get("os") or data.get("shell") or data.get("ps_version"):
            return

        try:
            stdout = self._execute_ps_stdout(session, WINRM_PS_CAPABILITY_PROBE)
            raw = parse_winrm_ps_probe_output(stdout)
            if not raw or "language_mode" not in raw:
                incomplete = _incomplete_winrm_ps(
                    error="capability probe incomplete or unparseable",
                )
                self.meta["winrm_ps"] = incomplete
                self._apply_winrm_ps(data, incomplete)
                data["status"] = "partial"
                data.setdefault("error", incomplete.get("error"))
                return
            derived = derive_winrm_ps_caps(raw)
            self.meta["winrm_ps"] = derived
            self._apply_winrm_ps(data, derived)
        except Exception as exc:  # noqa: BLE001 — probe must not fail open
            incomplete = _incomplete_winrm_ps(error=_safe_msg(exc))
            self.meta["winrm_ps"] = incomplete
            self._apply_winrm_ps(data, incomplete)
            data["status"] = "partial"
            data["error"] = incomplete["error"]

    def _capability_seeds_raw(
        self,
        session: AdaptedWinRMSession | None,
    ) -> dict[str, Any]:
        """Collect capability-related seeds from meta / session (no network)."""
        raw: dict[str, Any] = {}
        for key in _WINRM_PS_RAW_KEYS:
            val = self.meta.get(key)
            if val is None and session is not None:
                val = session.seed_attr(key)
            if val is not None:
                raw[key] = val
        return _normalize_winrm_ps_raw(raw)

    def _apply_winrm_ps(self, data: dict[str, Any], winrm_ps: dict[str, Any]) -> None:
        """Merge winrm_ps into probe *data* and flat convenience keys."""
        data["winrm_ps"] = winrm_ps
        # Flat keys used by Agent/open meta summaries when present.
        for key in (
            "ps_version",
            "language_mode",
            "ps_edition",
            "os_version",
            "ps_script_fs",
            "ps_oneshot",
            "ps_runspace",
        ):
            if key in winrm_ps and winrm_ps[key] is not None:
                data.setdefault(key, winrm_ps[key])
        # Keep meta.ps_version / shell convenience aligned when probe filled them.
        if winrm_ps.get("ps_version") is not None:
            self.meta.setdefault("ps_version", winrm_ps["ps_version"])
            data.setdefault("ps_version", winrm_ps["ps_version"])
        if winrm_ps.get("language_mode") is not None:
            self.meta["language_mode"] = winrm_ps["language_mode"]
        data.setdefault("shell", data.get("shell") or "powershell")
        data.setdefault("os", data.get("os") or "windows")

    def _execute_ps_stdout(
        self,
        session: AdaptedWinRMSession,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> str:
        """Run oneshot ``execute_ps`` and return decoded stdout text.

        Uses the same async-bridge wall-clock helper as oneshot exec so a hung
        capability or identity probe cannot stall connect. On timeout the
        caller treats the probe as incomplete (partial); connect still succeeds.

        Default budget is ``MRC_WINRM_PROBE_TIMEOUT_S`` (read at call time).
        Pass a non-positive ``timeout_s`` to skip the wall-clock wrapper.
        """
        def _call() -> Any:
            return session.execute_ps(script, environment=None)

        budget = MRC_WINRM_PROBE_TIMEOUT_S if timeout_s is None else float(timeout_s)
        if budget <= 0:
            raw = _call()
        else:
            raw = self._run_blocking_with_timeout(_call, timeout_s=budget)
        if isinstance(raw, tuple) and raw:
            return _decode_stream(raw[0])
        if isinstance(raw, ExecResult):
            return raw.stdout or ""
        return _decode_stream(getattr(raw, "stdout", raw))

    def _parse_probe_stdout(
        self, stdout: str, *, base: dict[str, Any]
    ) -> dict[str, Any]:
        data = dict(base)
        lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
        data.setdefault("os", "windows")
        data.setdefault("status", "ok")
        if not lines:
            data["status"] = "partial"
            data["error"] = "empty probe output"
            return data
        # Heuristic parse: version-like tokens → ps_version; path-like → home/pwd.
        for line in lines:
            if "ps_version" not in data and any(c.isdigit() for c in line) and len(line) < 40:
                if line[0].isdigit() or "Version" in line:
                    data["ps_version"] = line
                    continue
            if ("home" not in data) and (
                line.startswith("C:\\") or line.startswith("C:/") or line.startswith("/")
            ):
                data["home"] = line
                self.home = self.home or line
                continue
            if ("pwd" not in data) and (
                line.startswith("C:\\") or line.startswith("C:/") or line.startswith("/")
            ):
                data["pwd"] = line
                self.cwd = self.cwd or line
                continue
            if "Microsoft Windows" in line or "Windows" in line:
                data["os"] = line if "Windows" in line else "windows"
        data.setdefault("shell", "powershell")
        return data

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if command is None or (isinstance(command, str) and command.strip() == ""):
            raise TransportError("INVALID_ARG", "command is required")
        session = self._require_session()
        work = cwd if cwd is not None else self.cwd

        if session.has_run_command:
            return self._map_call_to_exec(
                lambda: session.run_command(
                    command, cwd=work, timeout_s=timeout_s, env=env
                ),
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Prefer PowerShell oneshot for shell-string commands (Windows remote
        # default shell is PowerShell; execute_cmd is the fallback).
        if session.has_execute_ps:
            script = _wrap_ps_with_cwd(command, work)
            return self._call_and_coerce(
                session.execute_ps,
                script,
                env=env,
                timeout_s=timeout_s,
                env_mode="ps",
                cwd=work,
                coerce=_ps_result_to_exec,
            )

        if session.has_execute_cmd:
            full = _wrap_cmd_with_cwd(command, work)
            return self._call_and_coerce(
                session.execute_cmd,
                full,
                env=env,
                timeout_s=timeout_s,
                env_mode="cmd",
                cwd=work,
                coerce=_coerce_exec_result,
            )

        raise TransportError(
            "UNSUPPORTED",
            "winrm session has no run_command/execute_ps/execute_cmd for exec",
            details={"host": self.host},
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if not argv:
            raise TransportError("INVALID_ARG", "argv is empty")
        session = self._require_session()
        work = cwd if cwd is not None else self.cwd
        argv_list = [str(a) for a in argv]

        if session.has_run_argv:
            return self._map_call_to_exec(
                lambda: session.run_argv(
                    argv_list, cwd=work, timeout_s=timeout_s, env=env
                ),
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Prefer PowerShell call-operator (``& exe @(args)``) over execute_cmd:
        # argv must reach the remote program verbatim, and cmd.exe expands
        # ``%VAR%`` even inside double quotes (see ``_win_quote``). The
        # call-operator passes args as PowerShell strings with no cmd
        # interpolation. execute_cmd remains a fallback for sessions without
        # execute_ps.
        if session.has_execute_ps:
            if len(argv_list) == 1:
                script = f"& {_ps_single_quote(argv_list[0])}"
            else:
                exe = _ps_single_quote(argv_list[0])
                args = ", ".join(_ps_single_quote(a) for a in argv_list[1:])
                script = f"& {exe} @({args})"
            script = _wrap_ps_with_cwd(script, work)
            return self._call_and_coerce(
                session.execute_ps,
                script,
                env=env,
                timeout_s=timeout_s,
                env_mode="ps",
                cwd=work,
                coerce=_ps_result_to_exec,
            )

        # Fallback: cmd shell-join (``%`` expansion is inherent to cmd here).
        if session.has_execute_cmd:
            joined = " ".join(_win_quote(a) for a in argv_list)
            full = _wrap_cmd_with_cwd(joined, work)
            return self._call_and_coerce(
                session.execute_cmd,
                full,
                env=env,
                timeout_s=timeout_s,
                env_mode="cmd",
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Last resort: shell-join via run_command when that surface exists.
        quoted = " ".join(shlex.quote(a) for a in argv_list)
        return self.run_command(quoted, cwd=work, timeout_s=timeout_s, env=env)

    def _require_session(self) -> AdaptedWinRMSession:
        if not self._connected or self._session is None:
            raise TransportError(
                "NOT_CONNECTED",
                "winrm transport is not connected",
                details={"host": self.host},
            )
        return self._session

    # ------------------------------------------------------------------
    # Filesystem client hook (fs backend)
    # ------------------------------------------------------------------

    def open_fs(self) -> Any:
        """Lazy-open a file client from the connected WinRM session.

        Resolution order (capability frozen at connect):

        - ``open_fs()`` → file client
        - file methods on the session itself (``listdir`` / ``stat`` / …)
        - pypsrp ``copy``/``fetch``/``execute_ps`` via ``PypsrpFileClient``
        """
        session = self._require_session()

        if session.has_open_fs:
            try:
                client = session.open_fs()
            except TransportError:
                raise
            except Exception as exc:
                raise TransportError(
                    "FS_ERROR",
                    f"open_fs failed: {_safe_msg(exc)}",
                    details={"host": self.host},
                ) from exc
            if client is None:
                raise TransportError(
                    "FS_ERROR",
                    "open_fs returned no client",
                    details={"host": self.host},
                )
            return client

        # Session itself is a file store (implements WinRMFileClient methods).
        if session.is_file_client:
            return session.raw

        # pypsrp Client surface: copy/fetch (+ execute_ps for list/stat/…).
        if session.can_build_pypsrp_fs:
            from mcp_remote_control.fs.backends.winrm import PypsrpFileClient

            # Pass the raw object so optional copy/fetch presence is
            # exact (AdaptedWinRMSession always exposes methods).
            return PypsrpFileClient(session.raw)

        raise TransportError(
            "UNSUPPORTED",
            "winrm session has no filesystem client (open_fs/copy/fetch)",
            details={"host": self.host},
        )

    # ------------------------------------------------------------------
    # Persistent PowerShell runspace (ps open / invoke / close)
    # ------------------------------------------------------------------

    def open_runspace(self) -> Any:
        """Open a persistent PSRP runspace; always return a RunspaceHandle adapter.

        Prefer ``session.open_runspace()`` when present; otherwise open a
        pypsrp ``RunspacePool`` against ``session.wsman``. Raw handles and pools
        are wrapped once here so :meth:`runspace_invoke` only calls adapter
        ``invoke`` / ``stop`` / ``close``.
        """
        session = self._require_session()
        if session.has_open_runspace:
            try:
                handle = session.open_runspace()
            except TransportError:
                raise
            except Exception as exc:
                raise TransportError(
                    "EXEC_FAILED",
                    f"open_runspace failed: {_safe_msg(exc)}",
                    details={"host": self.host},
                ) from exc
            if handle is None:
                raise TransportError(
                    "EXEC_FAILED",
                    "open_runspace returned no handle",
                    details={"host": self.host},
                )
            return _adapt_runspace_handle(handle, default_location=self.cwd)

        wsman = session.wsman
        if wsman is not None:
            try:
                from pypsrp.powershell import RunspacePool
            except ImportError as exc:  # pragma: no cover - hard dep in practice
                raise TransportError(
                    "UNSUPPORTED",
                    "pypsrp is required for PowerShell runspaces",
                    details={"host": self.host},
                ) from exc
            pool: Any = None
            try:
                # Local binding narrows ``session.wsman`` (Any | None) for the
                # type checker; RunspacePool expects a WSMan connection.
                pool = RunspacePool(wsman)
                pool.open()
                return _adapt_runspace_handle(pool, default_location=self.cwd)
            except Exception as exc:
                # pool.open() may leave a half-open remote runspace on failure.
                # Best-effort close the local pool before raising so retries do
                # not accumulate leaked remote runspaces.
                if pool is not None:
                    try:
                        pool.close()
                    except Exception:
                        pass
                raise TransportError(
                    "EXEC_FAILED",
                    f"RunspacePool open failed: {_safe_msg(exc)}",
                    details={"host": self.host},
                ) from exc

        raise TransportError(
            "UNSUPPORTED",
            "winrm session cannot open a PowerShell runspace",
            details={"host": self.host},
        )

    def runspace_invoke(
        self,
        handle: Any,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> RunspaceResult:
        """Invoke *script* on a RunspaceHandle adapter; return output + location.

        *handle* must come from :meth:`open_runspace` (always an adapter).
        ``timeout_s`` is a wall-clock budget: blocking ``invoke`` runs on the
        async-bridge loop via ``asyncio.to_thread``. On timeout ``handle.stop()``
        is called best-effort (pool adapters dispose the pipeline only so the
        runspace stays reusable).
        """
        if handle is None:
            raise TransportError(
                "PS_CLOSED",
                "runspace handle is missing",
                details={"host": self.host},
            )
        if script is None:
            raise TransportError("INVALID_ARG", "script is required")

        invoker = getattr(handle, "invoke", None)
        if not callable(invoker):
            raise TransportError(
                "PS_CLOSED",
                "runspace handle has no invoke",
                details={"host": self.host},
            )

        default_loc = getattr(handle, "location", None) or self.cwd
        try:
            raw = self._invoke_handle(handle, invoker, script, timeout_s=timeout_s)
        except TransportError:
            raise
        except TimeoutError as exc:
            return RunspaceResult(
                stdout="",
                stderr=str(exc) or "timeout",
                exit_code=-1,
                location=default_loc,
                had_errors=True,
                timed_out=True,
            )
        except Exception as exc:
            if _is_timeout_exc(exc):
                self._stop_handle(handle)
                return RunspaceResult(
                    stdout="",
                    stderr=_safe_msg(exc),
                    exit_code=-1,
                    location=default_loc,
                    had_errors=True,
                    timed_out=True,
                )
            raise TransportError(
                "EXEC_FAILED",
                _safe_msg(exc),
                details={"host": self.host},
            ) from exc

        result = _coerce_runspace_result(raw, default_location=default_loc)
        if result.location:
            self.cwd = result.location
        return result

    def _invoke_handle(
        self,
        handle: Any,
        invoker: Any,
        script: str,
        *,
        timeout_s: float | None,
    ) -> Any:
        """Run ``handle.invoke(script)`` with optional wall-clock timeout + stop."""
        if timeout_s is None or timeout_s <= 0:
            return invoker(script)
        try:
            return self._run_blocking_with_timeout(
                lambda: invoker(script),
                timeout_s=timeout_s,
            )
        except TimeoutError:
            self._stop_handle(handle)
            raise

    @staticmethod
    def _stop_handle(handle: Any) -> None:
        """Best-effort interrupt of an in-flight invoke (adapter or raw)."""
        stop = getattr(handle, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:
            pass

    def close_runspace(self, handle: Any) -> None:
        """Close a runspace handle (``close()`` if present). Best-effort."""
        if handle is None:
            return
        closer = getattr(handle, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _map_call_to_exec(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float | None,
        cwd: str | None,
        coerce: Callable[..., ExecResult],
    ) -> ExecResult:
        """Run *fn* with optional wall-clock timeout; map to ``ExecResult``.

        Shared by oneshot (via :meth:`_call_and_coerce`) and high-level
        ``session.run_command`` / ``run_argv`` so timeout → ``timed_out`` mapping
        is identical. ``TransportError`` propagates; other failures become
        ``EXEC_FAILED``.

        Timed-out oneshot pypsrp calls may leave server-side runspaces (the
        remote work cannot be cancelled from the client). Callers should
        ``endpoint close`` + reopen when timeouts recur — see ``_call_remote``.
        """
        try:
            if timeout_s is None or timeout_s <= 0:
                raw = fn()
            else:
                raw = self._run_blocking_with_timeout(fn, timeout_s=timeout_s)
        except TimeoutError as exc:
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc) or "timeout",
                cwd=cwd,
                timed_out=True,
            )
        except TransportError:
            raise
        except Exception as exc:
            if _is_timeout_exc(exc):
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=_safe_msg(exc),
                    cwd=cwd,
                    timed_out=True,
                )
            raise TransportError(
                "EXEC_FAILED",
                _safe_msg(exc),
                details={"host": self.host},
            ) from exc
        return coerce(raw, default_cwd=cwd)

    def _call_and_coerce(
        self,
        fn: Any,
        payload: str,
        *,
        env: dict[str, str] | None,
        timeout_s: float | None,
        env_mode: str,
        cwd: str | None,
        coerce: Callable[..., ExecResult],
    ) -> ExecResult:
        """Run oneshot ``execute_ps`` / ``execute_cmd``; normalize to ``ExecResult``.

        Applies env via :meth:`_call_remote` and the shared timeout mapping
        from :meth:`_map_call_to_exec`.
        """
        return self._map_call_to_exec(
            lambda: self._call_remote(
                fn,
                payload,
                env=env,
                timeout_s=None,
                env_mode=env_mode,
            ),
            timeout_s=timeout_s,
            cwd=cwd,
            coerce=coerce,
        )

    def _call_remote(
        self,
        fn: Any,
        payload: str,
        *,
        env: dict[str, str] | None,
        timeout_s: float | None,
        env_mode: str,
    ) -> Any:
        """Call oneshot ``execute_ps`` / ``execute_cmd`` with env + timeout.

        Env handling (Protocol contract):
        - Always pass ``environment=env`` when *env* is set. Implementers
          (``PypsrpClientAdapter``, mocks) accept that kwarg; no signature
          probing and no script-side inject on the production path.

        Timeout handling:
        - Oneshot APIs do not take a per-call timeout. Enforce a wall-clock
          deadline via the async-bridge watchdog
          (:meth:`_run_blocking_with_timeout`).

        Caveat (timed-out exec): ``asyncio.to_thread`` runs the blocking call
        on an executor thread that cannot be cancelled. On timeout the caller
        gets ``TimeoutError`` but the remote call may keep running and hold a
        server-side runspace (pypsrp opens a temporary RunspacePool per
        oneshot with no external stop handle). Repeated timeouts can leak
        server shells and exhaust ``MaxShellsPerUser``. If timeouts recur,
        the caller should ``endpoint close`` + reopen to drop the degraded
        session.

        *env_mode* is retained for call-site clarity (ps vs cmd) but is unused
        on the Protocol path (no inject fallback).
        """
        del env_mode  # Protocol path never injects; kept for API stability.
        invoke_kwargs: dict[str, Any] = {}
        if env:
            invoke_kwargs["environment"] = env

        if timeout_s is None or timeout_s <= 0:
            return fn(payload, **invoke_kwargs)
        return self._run_blocking_with_timeout(
            lambda: fn(payload, **invoke_kwargs),
            timeout_s=timeout_s,
        )

    def _run_blocking_with_timeout(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float,
    ) -> Any:
        """Run a blocking remote call on the shared bridge with a wall-clock timeout.

        Uses ``asyncio.to_thread`` on :class:`AsyncLoopBridge` so the call runs
        off the caller's thread. On timeout the bridge cancels the await and
        raises ``TimeoutError``; the executor thread keeps running until the
        remote side returns (e.g. after ``PowerShell.stop()``). Callers must
        perform any remote-side stop/dispose on ``TimeoutError``.
        """
        import asyncio

        from mcp_remote_control.transport.async_bridge import get_shared_bridge

        async def _wrap() -> Any:
            return await asyncio.to_thread(fn)

        return get_shared_bridge().run(_wrap(), timeout_s=timeout_s)



def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _is_timeout_exc(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timeout" in text or "timed out" in text


def _split_location_output(
    output: Any,
    marker: str,
) -> tuple[str | None, list[Any]]:
    """Extract the sentinel-tagged location from a pipeline's output list.

    The probe ``Write-Output ('<marker>' + (Get-Location).Path)`` emits one
    string starting with *marker*. All marker-prefixed elements are stripped
    and the location is taken from the last one (the probe is the final
    statement), so a coincidental user line that starts with the marker cannot
    leave a stale location. Returns ``(location, remaining_output)``.
    """
    if output is None:
        return None, []
    if not isinstance(output, (list, tuple)):
        text = str(output)
        if text.startswith(marker):
            loc = text[len(marker):].strip() or None
            return loc, []
        return None, [output]

    remaining = list(output)
    indices = [i for i, v in enumerate(remaining) if str(v).startswith(marker)]
    if not indices:
        return None, remaining
    location = str(remaining[indices[-1]])[len(marker):].strip() or None
    for i in sorted(indices, reverse=True):
        del remaining[i]
    return location, remaining


# Bound for best-effort pipeline stop/close after a runspace timeout so a slow
# WSMan cannot make runspace_invoke hang past the caller's budget. Daemon
# threads ensure a stuck stop() cannot block interpreter shutdown.
_STOP_DEADLINE_S = 2.0


def _call_with_deadline(fn: Any, *, deadline_s: float) -> None:
    """Run ``fn()`` on a daemon thread, waiting at most ``deadline_s`` seconds.

    Returns after the deadline even if ``fn`` is still running (daemon thread
    cannot block shutdown). Errors are swallowed.
    """
    if fn is None or not callable(fn):
        return
    import threading

    done = threading.Event()

    def _run() -> None:
        try:
            fn()
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True, name="mrc-ps-stop")
    t.start()
    done.wait(timeout=deadline_s)


def _safe_stop_pipeline(ps: Any) -> None:
    """Best-effort, bounded stop+close of a pypsrp PowerShell pipeline (not the pool).

    On runspace timeout, dispose the pipeline so the pool remains usable.
    ``PowerShell.stop()`` signals the remote host to abort; ``close()`` releases
    local resources. Each step is capped by ``_STOP_DEADLINE_S``; unfinished
    cleanup continues on a daemon thread.
    """
    if ps is None:
        return
    _call_with_deadline(getattr(ps, "stop", None), deadline_s=_STOP_DEADLINE_S)
    _call_with_deadline(getattr(ps, "close", None), deadline_s=_STOP_DEADLINE_S)


def _ps_result_to_exec(raw: Any, *, default_cwd: str | None) -> ExecResult:
    """Map pypsrp ``execute_ps`` ``(output, streams, had_errors)`` → ``ExecResult``."""
    if isinstance(raw, ExecResult):
        return _coerce_exec_result(raw, default_cwd=default_cwd)

    if isinstance(raw, tuple) and len(raw) >= 1:
        stdout = _decode_stream(raw[0])
        stderr = ""
        had_errors = False
        if len(raw) >= 3:
            had_errors = bool(raw[2])
        if len(raw) >= 2 and raw[1] is not None:
            streams = raw[1]
            # PSDataStreams.error is a list of error records.
            err_list = getattr(streams, "error", None)
            if err_list:
                parts = []
                for item in err_list:
                    parts.append(str(item))
                stderr = "\n".join(parts)
            elif not isinstance(streams, (str, bytes)):
                pass
            else:
                stderr = _decode_stream(streams)
        exit_code = 1 if had_errors else 0
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=default_cwd,
        )

    return _coerce_exec_result(raw, default_cwd=default_cwd)


def _format_ps_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output if output.endswith("\n") or not output else output + "\n"
    if isinstance(output, (list, tuple)):
        lines = [str(item) for item in output]
        body = "\n".join(lines)
        if body and not body.endswith("\n"):
            body += "\n"
        return body
    text = str(output)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _format_ps_errors(ps: Any) -> str:
    streams = getattr(ps, "streams", None)
    if streams is None:
        return ""
    err_list = getattr(streams, "error", None) or []
    if not err_list:
        return ""
    return "\n".join(str(item) for item in err_list)


def _coerce_runspace_result(
    raw: Any,
    *,
    default_location: str | None,
) -> RunspaceResult:
    """Normalize invoke return shapes into ``RunspaceResult``."""
    if isinstance(raw, RunspaceResult):
        if raw.location is None and default_location is not None:
            return RunspaceResult(
                stdout=raw.stdout,
                stderr=raw.stderr,
                exit_code=raw.exit_code,
                location=default_location,
                had_errors=raw.had_errors,
                timed_out=raw.timed_out,
            )
        return raw

    # Same shape as this module's RunspaceResult but a different class object
    # (avoid importing sibling packages at module load).
    if type(raw).__name__ == "RunspaceResult" and hasattr(raw, "stdout"):
        loc = getattr(raw, "location", None) or default_location
        exit_code = int(getattr(raw, "exit_code", 0) or 0)
        stderr = _decode_stream(getattr(raw, "stderr", "") or "")
        return RunspaceResult(
            stdout=_decode_stream(getattr(raw, "stdout", "") or ""),
            stderr=stderr,
            exit_code=exit_code,
            location=loc,
            had_errors=bool(getattr(raw, "had_errors", False)) or exit_code != 0,
            timed_out=bool(getattr(raw, "timed_out", False)),
        )

    if isinstance(raw, tuple):
        # (stdout, location) or (stdout, stderr, exit, location)
        if len(raw) == 2:
            return RunspaceResult(
                stdout=_decode_stream(raw[0]),
                location=_decode_stream(raw[1]) if raw[1] is not None else default_location,
            )
        if len(raw) >= 3:
            stdout = _decode_stream(raw[0])
            stderr = _decode_stream(raw[1]) if raw[1] is not None else ""
            exit_code = int(raw[2]) if isinstance(raw[2], (int, float)) else 0
            loc = (
                _decode_stream(raw[3])
                if len(raw) > 3 and raw[3] is not None
                else default_location
            )
            return RunspaceResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                location=loc,
                had_errors=exit_code != 0 or bool(stderr),
            )

    if isinstance(raw, dict):
        exit_code = int(raw.get("exit_code", 0) or 0)
        stderr = _decode_stream(raw.get("stderr", "") or "")
        return RunspaceResult(
            stdout=_decode_stream(raw.get("stdout", "") or ""),
            stderr=stderr,
            exit_code=exit_code,
            location=raw.get("location") or default_location,
            had_errors=bool(raw.get("had_errors", False)) or exit_code != 0,
            timed_out=bool(raw.get("timed_out", False)),
        )

    if isinstance(raw, str):
        return RunspaceResult(stdout=raw, location=default_location)

    if raw is None:
        return RunspaceResult(stdout="", location=default_location)

    # Duck-type objects with stdout / exit_code attributes.
    if hasattr(raw, "stdout") or hasattr(raw, "exit_code"):
        exit_code = int(getattr(raw, "exit_code", 0) or 0)
        stderr = _decode_stream(getattr(raw, "stderr", "") or "")
        return RunspaceResult(
            stdout=_decode_stream(getattr(raw, "stdout", "") or ""),
            stderr=stderr,
            exit_code=exit_code,
            location=getattr(raw, "location", None)
            or getattr(raw, "cwd", None)
            or default_location,
            had_errors=bool(getattr(raw, "had_errors", False)) or exit_code != 0,
            timed_out=bool(getattr(raw, "timed_out", False)),
        )

    raise TransportError(
        "EXEC_FAILED",
        f"unrecognized runspace invoke result type: {type(raw).__name__}",
    )
