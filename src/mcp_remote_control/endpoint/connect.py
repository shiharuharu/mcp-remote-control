"""Profile -> transport construction (WinRM/SSH secrets, known_hosts, cwd seed).

``EndpointRegistry._build_transport`` stays on the registry class so tests
and injection can still supply ``ssh_connector`` / ``winrm_connector``. This
module owns the mapping helpers that class method calls.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from mcp_remote_control.config import Profile
from mcp_remote_control.endpoint.caps import coerce_toml_bool
from mcp_remote_control.transport import TransportError, WinRMTransport
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path
from mcp_remote_control.transport.ssh import (
    KNOWN_HOSTS_UNSET as KNOWN_HOSTS_UNSET,
)
from mcp_remote_control.transport.winrm import WinRMConnector


def _optional_int(value: Any) -> int | None:
    """Coerce a ``[winrm]`` value to ``int``, or None when it is not usable.

    ``bool`` is an ``int`` subclass and is rejected explicitly: otherwise
    ``operation_timeout_s = true`` silently becomes a one-second operation
    timeout. Non-finite floats are rejected as well - ``int(nan)`` raises
    ValueError and ``int(inf)`` raises OverflowError, and both spellings are
    legal TOML (``inf``, ``1e400``). A junk tuning knob degrades to "unset"
    so the transport keeps its own default instead of making the endpoint
    unopenable.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_winrm_retries(value: Any) -> int | None:
    """Non-negative retry count for ``[winrm].reconnection_retries``, else None.

    Bools are rejected (``int(True)`` would silently become 1 retry) along with
    unparsable strings and negative counts: the transport keeps its own default
    rather than receiving a nonsensical policy. ``0`` survives as a deliberate
    opt-out, which is distinct from ``None`` ("unset").
    """
    if value is None or isinstance(value, bool):
        return None
    retries = _optional_int(value)
    if retries is None or retries < 0:
        return None
    return retries


def _optional_winrm_float(value: Any, *, allow_zero: bool) -> float | None:
    """Finite float for a ``[winrm]`` tuning knob, else None.

    Junk (bool / unparsable string / NaN / infinity) degrades to None so a typo
    in a tuning knob cannot make an endpoint unopenable. ``allow_zero``
    separates a delay where 0 means "retry immediately" from a probe budget
    where 0 would be unusable.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if number < 0 or (number == 0 and not allow_zero):
        return None
    return number


def _first_defined(*values: object) -> object:
    """Return the first non-None value so TOML false/0 stay defined.

    ``a or b`` treats False/0 as missing and walks to defaults. Callers that
    need omit-vs-disable must use this before ``coerce_toml_bool``.
    """
    for value in values:
        if value is not None:
            return value
    return None


def _build_winrm_transport(
    profile: Profile,
    *,
    connector: WinRMConnector | None,
) -> WinRMTransport:
    """Map a WinRM profile (incl. enterprise auth) -> WinRMTransport."""
    if not profile.host or not profile.username:
        raise TransportError(
            "CONNECT_FAILED",
            "winrm profile missing host or username",
        )
    winrm_cfg = profile.winrm or {}
    port = profile.port or 5985
    ssl = _resolve_winrm_ssl(winrm_cfg)
    # Port default: 5986 for https when profile port was the winrm default.
    if profile.port is None and ssl:
        port = 5986

    auth_protocol = _resolve_winrm_auth_protocol(profile, winrm_cfg)

    cert_validation = True
    scv = winrm_cfg.get("server_cert_validation")
    if scv is not None:
        cert_validation = str(scv).lower() not in (
            "ignore",
            "false",
            "0",
            "no",
        )
    elif "cert_validation" in winrm_cfg:
        # TOML/JSON string "false"/"0"/"no"/"off" disable (not bool("false")).
        cert_validation = coerce_toml_bool(winrm_cfg.get("cert_validation"))

    # Unset or junk keeps the 15s default; a bool must not arrive as 1ms.
    timeout_ms = 15000
    raw_timeout = winrm_cfg.get("connect_timeout_ms")
    if raw_timeout is not None:
        coerced_timeout = _optional_int(raw_timeout)
        if coerced_timeout is not None:
            timeout_ms = coerced_timeout

    encryption = _resolve_winrm_encryption(winrm_cfg)

    operation_timeout_s = _optional_int(winrm_cfg.get("operation_timeout_s"))
    read_timeout_s = _optional_int(winrm_cfg.get("read_timeout_s"))

    # Link-recovery / probe-budget knobs. Only a usable profile value is
    # forwarded: None lets the transport (and, for the probe budget, the
    # MRC_WINRM_PROBE_TIMEOUT_S env override) pick its own default.
    reconnection_retries = _optional_winrm_retries(
        winrm_cfg.get("reconnection_retries")
    )
    reconnection_backoff = _optional_winrm_float(
        winrm_cfg.get("reconnection_backoff"), allow_zero=True
    )
    probe_timeout_s = _optional_winrm_float(
        winrm_cfg.get("probe_timeout_s"), allow_zero=False
    )

    password = _resolve_password(profile)
    auth = profile.auth

    # Certificate paths (string form for pypsrp); never load PEM bodies here.
    certificate_pem: str | None = None
    certificate_key_pem: str | None = None
    certificate_key_password: str | None = None
    spn: str | None = None
    negotiate_hostname_override: str | None = None
    negotiate_service: str | None = None
    negotiate_delegate: bool | None = None
    credssp_auth_mechanism: str | None = None
    credssp_disable_tlsv1_2: bool | None = None
    credssp_minimum_version: int | None = None

    if auth is not None:
        if auth.cert_path is not None:
            certificate_pem = str(auth.cert_path)
        if auth.cert_key_path is not None:
            certificate_key_pem = str(auth.cert_key_path)
        if auth.cert_key_password_path is not None:
            certificate_key_password = _read_secret_file_first_line(
                auth.cert_key_password_path
            )
        spn = auth.spn
        negotiate_hostname_override = auth.negotiate_hostname_override
        negotiate_service = auth.negotiate_service
        negotiate_delegate = auth.negotiate_delegate
        credssp_auth_mechanism = auth.credssp_auth_mechanism
        credssp_disable_tlsv1_2 = auth.credssp_disable_tlsv1_2
        credssp_minimum_version = auth.credssp_minimum_version

    # Optional [winrm.credssp] table overlays profile auth fields when unset.
    credssp_tbl = winrm_cfg.get("credssp")
    if isinstance(credssp_tbl, dict):
        if credssp_auth_mechanism is None and credssp_tbl.get("auth_mechanism"):
            credssp_auth_mechanism = str(credssp_tbl.get("auth_mechanism"))
        if credssp_disable_tlsv1_2 is None and "disable_tlsv1_2" in credssp_tbl:
            # String-safe: disable_tlsv1_2="false" must be False, not bool("false").
            credssp_disable_tlsv1_2 = coerce_toml_bool(
                credssp_tbl.get("disable_tlsv1_2")
            )
        if credssp_minimum_version is None and "minimum_version" in credssp_tbl:
            credssp_minimum_version = _optional_int(credssp_tbl.get("minimum_version"))

    return WinRMTransport(
        host=profile.host,
        port=int(port),
        username=profile.username,
        password=password,
        auth=auth_protocol,
        ssl=ssl,
        cert_validation=cert_validation,
        connect_timeout_ms=timeout_ms,
        operation_timeout_s=operation_timeout_s,
        read_timeout_s=read_timeout_s,
        encryption=encryption,
        connector=connector,
        certificate_pem=certificate_pem,
        certificate_key_pem=certificate_key_pem,
        certificate_key_password=certificate_key_password,
        spn=spn,
        negotiate_hostname_override=negotiate_hostname_override,
        negotiate_service=negotiate_service,
        negotiate_delegate=negotiate_delegate,
        credssp_auth_mechanism=credssp_auth_mechanism,
        credssp_disable_tlsv1_2=credssp_disable_tlsv1_2,
        credssp_minimum_version=credssp_minimum_version,
        reconnection_retries=reconnection_retries,
        reconnection_backoff=reconnection_backoff,
        probe_timeout_s=probe_timeout_s,
    )


def _resolve_winrm_ssl(winrm_cfg: dict[str, Any]) -> bool:
    """Effective TLS flag for a ``[winrm]`` table.

    ``scheme`` decides; a bare ``ssl`` flag switches to TLS under string-safe
    truthiness, so ``ssl = "false"`` / ``"0"`` / ``"no"`` / ``"off"`` never
    enables it via ``bool(str)``.
    """
    scheme = str(winrm_cfg.get("scheme") or "http").strip().lower()
    if scheme in ("https", "ssl", "true", "1"):
        return True
    return coerce_toml_bool(winrm_cfg.get("ssl", False))


def _resolve_winrm_encryption(winrm_cfg: dict[str, Any]) -> str:
    """Effective message-encryption token for a ``[winrm]`` table.

    ``message_encryption`` wins over the ``encryption`` alias; an unset pair
    means ``auto``. The token is normalized (stripped, lowercased) to the
    ``auto`` / ``always`` / ``never`` vocabulary the transport validates
    against, so every reader compares and reports the same value.
    """
    raw = winrm_cfg.get("message_encryption") or winrm_cfg.get("encryption") or "auto"
    return str(raw).strip().lower()


def _resolve_winrm_auth_protocol(
    profile: Profile, winrm_cfg: dict[str, Any]
) -> str:
    """Resolve pypsrp auth protocol from [winrm].auth and [auth].method."""
    explicit = winrm_cfg.get("auth") or winrm_cfg.get("auth_method")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()

    if profile.auth is not None:
        method = profile.auth.method
        if method == "password":
            return "ntlm"
        if method in (
            "ntlm",
            "basic",
            "negotiate",
            "kerberos",
            "credssp",
            "certificate",
        ):
            return method
    return "ntlm"


def _resolve_password(profile: Profile) -> str | None:
    """Resolve password: inline first, then password_env, then password_path.

    Inline profile passwords are supported (not treated as private). File/env
    paths remain optional. Missing material surfaces as AUTH_FAILED at connect
    when the connector needs credentials.
    """
    auth = profile.auth
    if auth is None:
        return None

    if auth.password is not None and str(auth.password) != "":
        return str(auth.password)

    if auth.password_env:
        env_name = str(auth.password_env).strip()
        if env_name:
            val = os.environ.get(env_name)
            if val is not None:
                return val

    if auth.password_path is not None:
        return _read_secret_file_first_line(auth.password_path)

    return None


def _read_secret_file_first_line(path: Path | str) -> str | None:
    """Read first line of a secret file; never raise (defer to connect).

    ``Path.expanduser`` raises ``RuntimeError`` for a ``~user`` with no home
    on this host, and ``Path`` itself can raise on an embedded NUL. Profile
    loads reject such auth paths, but a directly constructed profile can
    still carry one, and a missing secret must surface as a connect-time
    auth failure rather than as interpreter text escaping the open path.
    """
    try:
        p = Path(path).expanduser()
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            line = text.splitlines()[0] if text.splitlines() else text
            return line.rstrip("\r\n")
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _ssh_known_hosts(ssh_table: dict[str, Any]) -> Any:
    """Map profile ``[ssh] known_hosts`` to the transport's three-state value.

    - key missing / ``None`` / empty -> :data:`KNOWN_HOSTS_UNSET` (asyncssh's own
      default: ``~/.ssh/known_hosts``)
    - ``"none"`` / ``false`` / ``"off"`` -> ``None`` (verification disabled)
    - anything else -> path string (verify against that file)

    Only an explicit ``None`` disables validation in asyncssh; a specified but
    empty value makes it fall back to the default file and verify against that,
    so ``known_hosts = "none"`` must map to ``None`` rather than to ``()`` or
    the escape hatch silently does the opposite of what it says.
    """
    if "known_hosts" not in ssh_table:
        return KNOWN_HOSTS_UNSET
    raw = ssh_table.get("known_hosts")
    if raw is None:
        return KNOWN_HOSTS_UNSET
    if isinstance(raw, bool):
        return KNOWN_HOSTS_UNSET if raw else None
    text = str(raw).strip()
    if not text:
        return KNOWN_HOSTS_UNSET
    if text.lower() in ("none", "off", "false", "0", "disable", "disabled"):
        return None
    return text


def _seed_cwd(profile: Profile, transport: BaseTransport) -> str | None:
    """Default cwd seed: profile defaults.cwd -> transport.cwd -> local getcwd.

    A leading ``~`` is expanded against the LOCAL home only for the local
    transport. For ssh/winrm, ``Path.expanduser`` would substitute the local
    ``$HOME`` (e.g. ``/Users/shiharu``) which does not exist remotely; the
    tilde is returned verbatim so the remote shell resolves it against the
    remote user's home.

    A ``~user`` naming a user with no home on this host cannot be expanded at
    all, and cannot be passed through either: the local transport would later
    resolve the literal tilde against the current directory and fail on a path
    nobody asked for. It is reported as INVALID_CWD so the profile is named as
    the cause instead of a connect failure pointing at the network.
    """
    raw = None
    if profile.defaults:
        raw = profile.defaults.get("cwd")
    if isinstance(raw, str) and raw.strip():
        text = str(raw).strip()
        if text.startswith("~"):
            # Only expand ~ for local; remote shells resolve ~ themselves.
            if profile.transport == "local":
                try:
                    return str(Path(text).expanduser())
                except (OSError, RuntimeError, ValueError) as exc:
                    raise TransportError(
                        "INVALID_CWD",
                        f"defaults.cwd {text!r} cannot be expanded on this "
                        f"host: {exc}",
                        details={"cwd": text},
                    ) from exc
            return text
        return text
    # Reject bool/str(True) probe-cap bleed-through (cwd must be path-like).
    seeded = coerce_cwd_path(transport.cwd)
    if seeded:
        return seeded
    if profile.transport == "local":
        return os.getcwd()
    return None
