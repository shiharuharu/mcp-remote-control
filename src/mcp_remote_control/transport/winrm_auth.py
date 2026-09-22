"""WinRM auth protocol names and pypsrp Client kwargs assembly."""

from __future__ import annotations

from typing import Any

from mcp_remote_control.transport.base import TransportError

# Auth protocol names accepted by assemble_pypsrp_kwargs / Client(auth=...).
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


def parse_spn(spn: str | None) -> tuple[str | None, str | None]:
    """Split ``SERVICE/host`` SPN into (service, hostname_override).

    Bare hostname (no ``/``) -> (None, host). Empty/None -> (None, None).
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
    # urllib3-level transport retries, forwarded to pypsrp ``WSMan``.
    reconnection_retries: int | None = None,
    reconnection_backoff: float | None = None,
) -> dict[str, Any]:
    """Build connector / pypsrp ``Client`` kwargs for a WinRM auth method.

    Pure assembly (no network). Raises ``TransportError`` for invalid combos
    (unsupported protocol, certificate without SSL, encryption incompatibilities,
    missing password for basic/credssp). Secret values (password, cert key
    password) may appear in the returned dict for the connector only - callers
    must not log them or place them on the Agent track.

    ``reconnection_retries`` / ``reconnection_backoff`` are written only when
    not ``None``: ``None`` omits the key entirely, which means "no opinion"
    rather than "0 retries". The end-to-end default is supplied by the caller,
    not here - ``WinRMTransport`` resolves both knobs through
    ``resolve_winrm_reconnect``, which turns an unset value into
    ``(2, 0.5)`` and forwards that pair, so a real pypsrp ``Client`` gets 2
    retries. pypsrp's own library default of 0 applies only when this function
    is called directly, bypassing the transport. An explicit ``0`` is a
    distinct "disable" and still reaches the client. Both are urllib3 ``Retry``
    knobs underneath, and urllib3's ``Retry.DEFAULT_ALLOWED_METHODS`` does not
    contain ``POST`` - so they retry *connection/exception* failures on WinRM
    POSTs but never retry on an HTTP status code. A status-code retry knob
    would be inert here; do not add one.
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

    # Only non-None reaches the client: an omitted key means "no opinion", and
    # the caller's own default (WinRMTransport resolves one) applies instead of
    # this function inventing a value.
    if reconnection_retries is not None:
        kwargs["reconnection_retries"] = int(reconnection_retries)
    if reconnection_backoff is not None:
        kwargs["reconnection_backoff"] = float(reconnection_backoff)

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

