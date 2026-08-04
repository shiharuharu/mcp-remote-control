"""Load global ``config.toml`` and connection profiles.

Profile names are validated against a safe pattern *before* path join so
values like ``../config`` cannot escape ``profiles/``. Auth material is
represented only as paths or env-var names; secret file bodies are never
read by this module.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

from mcp_remote_control.config.errors import (
    ConfigInvalid,
    ProfileInvalid,
    ProfileNotFound,
)
from mcp_remote_control.config.models import (
    AuthConfig,
    DefaultsConfig,
    GlobalConfig,
    LoggingConfig,
    Profile,
    SecurityConfig,
    Transport,
)
from mcp_remote_control.config.paths import (
    config_toml_path,
    profiles_dir,
    resolve_under_home,
)

_PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_VALID_TRANSPORTS: frozenset[str] = frozenset({"local", "ssh", "winrm"})
# Union of SSH identity methods and WinRM protocol methods.
_VALID_AUTH_METHODS: frozenset[str] = frozenset(
    {
        # SSH / generic
        "password",
        "private_key",
        "private_key_path",
        "ssh_agent",
        "none",
        # WinRM enterprise (pypsrp SUPPORTED_AUTHS + password alias)
        "ntlm",
        "basic",
        "kerberos",
        "credssp",
        "certificate",
        "negotiate",  # pypsrp negotiate (NTLM/Kerberos auto)
    }
)
_SSH_AUTH_METHODS: frozenset[str] = frozenset(
    {
        "password",
        "private_key",
        "private_key_path",
        "ssh_agent",
        "none",
    }
)
_WINRM_AUTH_METHODS: frozenset[str] = frozenset(
    {
        "password",  # material recipe; protocol defaults to ntlm
        "ntlm",
        "basic",
        "kerberos",
        "credssp",
        "certificate",
        "negotiate",
    }
)
_VALID_CREDSSP_MECHS: frozenset[str] = frozenset({"auto", "ntlm", "kerberos"})


def load_config(home: Path) -> GlobalConfig:
    """Load optional ``{home}/config.toml``; missing file → built-in defaults."""
    path = config_toml_path(home)
    if not path.is_file():
        return GlobalConfig(from_defaults=True, source_path=None)

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigInvalid(f"cannot read config.toml: {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalid(f"invalid TOML in config.toml: {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigInvalid(f"config.toml is not valid UTF-8: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigInvalid(f"config.toml root must be a table: {path}")

    try:
        defaults = _parse_defaults(data.get("defaults"))
        logging_cfg = _parse_logging(data.get("logging"))
        security = _parse_security(data.get("security"))
    except ConfigInvalid:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ConfigInvalid(f"invalid config.toml values in {path}: {exc}") from exc

    return GlobalConfig(
        defaults=defaults,
        logging=logging_cfg,
        security=security,
        from_defaults=False,
        source_path=path.resolve(),
    )


def list_profiles(home: Path) -> list[str]:
    """Return sorted profile names (stems of ``profiles/*.toml``).

    Only ``*.toml`` regular files are considered. Invalid names are still listed
    so callers can attempt ``load_profile`` and surface typed errors.
    """
    pdir = profiles_dir(home)
    if not pdir.is_dir():
        return []

    names: list[str] = []
    for entry in pdir.iterdir():
        if entry.is_file() and entry.suffix == ".toml":
            names.append(entry.stem)
    return sorted(names)


def load_profile(home: Path, name: str) -> Profile:
    """Load and validate ``profiles/{name}.toml``.

    Validates *name* against a safe pattern before constructing the path so
    traversal values cannot cause reads outside ``profiles/``.

    Raises:
        ProfileNotFound: file missing
        ProfileInvalid: bad TOML, missing required fields, name mismatch, etc.
    """
    if not name or not isinstance(name, str):
        raise ProfileInvalid("profile name must be a non-empty string")
    # Validate before path join so traversal / malformed names never cause
    # reads outside profiles/.
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {_PROFILE_NAME_RE.pattern}"
        )

    path = profiles_dir(home) / f"{name}.toml"
    if not path.is_file():
        raise ProfileNotFound(f"profile not found: {name!r} ({path})")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileInvalid(f"cannot read profile {name!r}: {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileInvalid(f"invalid TOML in profile {name!r}: {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProfileInvalid(f"profile {name!r} is not valid UTF-8: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ProfileInvalid(f"profile {name!r} root must be a table: {path}")

    return _profile_from_table(home, name, path, data)


def _profile_from_table(
    home: Path,
    expected_name: str,
    path: Path,
    data: dict[str, Any],
) -> Profile:
    file_name = data.get("name")
    if file_name is None:
        raise ProfileInvalid(
            f"profile {expected_name!r}: missing required field 'name' ({path})"
        )
    if not isinstance(file_name, str):
        raise ProfileInvalid(
            f"profile {expected_name!r}: 'name' must be a string ({path})"
        )
    if file_name != expected_name:
        raise ProfileInvalid(
            f"profile name {file_name!r} does not match file stem "
            f"{expected_name!r} ({path})"
        )
    if not _PROFILE_NAME_RE.fullmatch(file_name):
        raise ProfileInvalid(
            f"profile name {file_name!r} must match "
            f"{_PROFILE_NAME_RE.pattern} ({path})"
        )

    transport_raw = data.get("transport")
    if transport_raw is None:
        raise ProfileInvalid(
            f"profile {expected_name!r}: missing required field 'transport' ({path})"
        )
    if not isinstance(transport_raw, str) or transport_raw not in _VALID_TRANSPORTS:
        raise ProfileInvalid(
            f"profile {expected_name!r}: transport must be one of "
            f"{sorted(_VALID_TRANSPORTS)}, got {transport_raw!r} ({path})"
        )
    # Validated against _VALID_TRANSPORTS == {"local","ssh","winrm"}.
    transport = cast(Transport, transport_raw)

    host = _optional_str(data, "host", expected_name, path)
    username = _optional_str(data, "username", expected_name, path)
    label = _optional_str(data, "label", expected_name, path)
    port = _optional_int(data, "port", expected_name, path)

    if transport in ("ssh", "winrm"):
        if not host:
            raise ProfileInvalid(
                f"profile {expected_name!r}: 'host' is required for "
                f"transport={transport!r} ({path})"
            )
        if not username:
            raise ProfileInvalid(
                f"profile {expected_name!r}: 'username' is required for "
                f"transport={transport!r} ({path})"
            )
        if port is None:
            port = 22 if transport == "ssh" else 5985

    auth_table = data.get("auth")
    auth: AuthConfig | None = None
    if auth_table is not None:
        if not isinstance(auth_table, dict):
            raise ProfileInvalid(
                f"profile {expected_name!r}: [auth] must be a table ({path})"
            )
        auth = _parse_auth(home, expected_name, path, auth_table)
    elif transport in ("ssh", "winrm"):
        # Auth section optional (e.g. ssh_agent-style setups).
        auth = None

    ssh = _optional_table(data, "ssh", expected_name, path)
    winrm = _optional_table(data, "winrm", expected_name, path)
    defaults = _optional_table(data, "defaults", expected_name, path)
    caps = _optional_table(data, "caps", expected_name, path)

    if auth is not None:
        _validate_auth_for_transport(
            expected_name, path, transport, auth, winrm=winrm
        )

    return Profile(
        name=file_name,
        transport=transport,
        host=host,
        port=port,
        username=username,
        label=label,
        auth=auth,
        ssh=ssh,
        winrm=winrm,
        defaults=defaults,
        caps=caps,
        source_path=path.resolve(),
    )


def _parse_auth(
    home: Path,
    profile_name: str,
    path: Path,
    table: dict[str, Any],
) -> AuthConfig:
    method = table.get("method")
    if method is None:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method is required ({path})"
        )
    if not isinstance(method, str) or method not in _VALID_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method must be one of "
            f"{sorted(_VALID_AUTH_METHODS)}, got {method!r} ({path})"
        )

    key_path = _optional_secret_path(home, table, "key_path", profile_name, path)
    passphrase_path = _optional_secret_path(
        home, table, "passphrase_path", profile_name, path
    )
    password_path = _optional_secret_path(
        home, table, "password_path", profile_name, path
    )
    password_env = table.get("password_env")
    if password_env is not None and not isinstance(password_env, str):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].password_env must be a string ({path})"
        )

    # Detect discouraged inline secrets without storing their values.
    has_inline_password = "password" in table and table.get("password") is not None
    has_inline_private_key = (
        "private_key_pem" in table and table.get("private_key_pem") is not None
    )
    # Inline cert PEM bodies are also discouraged (paths only).
    if table.get("certificate_pem") is not None and not isinstance(
        table.get("certificate_pem"), str
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].certificate_pem must be a path "
            f"string when set ({path})"
        )
    if any(
        k in table and table.get(k) is not None
        for k in ("certificate_body", "cert_pem_body", "private_key_body")
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: inline certificate/key bodies are not "
            f"allowed; use cert_path / cert_key_path ({path})"
        )

    # Certificate paths: accept cert_path / certificate_pem (path only).
    cert_path = _optional_secret_path(home, table, "cert_path", profile_name, path)
    if cert_path is None:
        cert_path = _optional_secret_path(
            home, table, "certificate_pem", profile_name, path
        )
    cert_key_path = _optional_secret_path(
        home, table, "cert_key_path", profile_name, path
    )
    if cert_key_path is None:
        cert_key_path = _optional_secret_path(
            home, table, "certificate_key_pem", profile_name, path
        )
    cert_key_password_path = _optional_secret_path(
        home, table, "cert_key_password_path", profile_name, path
    )
    if cert_key_password_path is None:
        cert_key_password_path = _optional_secret_path(
            home, table, "certificate_key_password_path", profile_name, path
        )

    spn = table.get("spn")
    if spn is not None and not isinstance(spn, str):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].spn must be a string ({path})"
        )

    negotiate_hostname_override = table.get("negotiate_hostname_override")
    if negotiate_hostname_override is not None and not isinstance(
        negotiate_hostname_override, str
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_hostname_override "
            f"must be a string ({path})"
        )
    negotiate_service = table.get("negotiate_service")
    if negotiate_service is not None and not isinstance(negotiate_service, str):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_service must be a "
            f"string ({path})"
        )
    negotiate_delegate = table.get("negotiate_delegate")
    if negotiate_delegate is not None and not isinstance(negotiate_delegate, bool):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_delegate must be a "
            f"bool ({path})"
        )

    credssp_auth_mechanism = table.get("credssp_auth_mechanism")
    if credssp_auth_mechanism is not None:
        if (
            not isinstance(credssp_auth_mechanism, str)
            or credssp_auth_mechanism not in _VALID_CREDSSP_MECHS
        ):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [auth].credssp_auth_mechanism must "
                f"be one of {sorted(_VALID_CREDSSP_MECHS)}, got "
                f"{credssp_auth_mechanism!r} ({path})"
            )
    credssp_disable_tlsv1_2 = table.get("credssp_disable_tlsv1_2")
    if credssp_disable_tlsv1_2 is not None and not isinstance(
        credssp_disable_tlsv1_2, bool
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].credssp_disable_tlsv1_2 must "
            f"be a bool ({path})"
        )
    credssp_minimum_version = table.get("credssp_minimum_version")
    if credssp_minimum_version is not None:
        if isinstance(credssp_minimum_version, bool) or not isinstance(
            credssp_minimum_version, int
        ):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [auth].credssp_minimum_version "
                f"must be an integer ({path})"
            )

    return AuthConfig(
        method=method,
        key_path=key_path,
        passphrase_path=passphrase_path,
        password_path=password_path,
        password_env=password_env,
        has_inline_password=bool(has_inline_password),
        has_inline_private_key=bool(has_inline_private_key),
        cert_path=cert_path,
        cert_key_path=cert_key_path,
        cert_key_password_path=cert_key_password_path,
        spn=spn if isinstance(spn, str) else None,
        negotiate_hostname_override=(
            negotiate_hostname_override
            if isinstance(negotiate_hostname_override, str)
            else None
        ),
        negotiate_service=(
            negotiate_service if isinstance(negotiate_service, str) else None
        ),
        negotiate_delegate=(
            negotiate_delegate if isinstance(negotiate_delegate, bool) else None
        ),
        credssp_auth_mechanism=(
            credssp_auth_mechanism
            if isinstance(credssp_auth_mechanism, str)
            else None
        ),
        credssp_disable_tlsv1_2=(
            credssp_disable_tlsv1_2
            if isinstance(credssp_disable_tlsv1_2, bool)
            else None
        ),
        credssp_minimum_version=(
            credssp_minimum_version
            if isinstance(credssp_minimum_version, int)
            else None
        ),
    )


def _validate_auth_for_transport(
    profile_name: str,
    path: Path,
    transport: Transport,
    auth: AuthConfig,
    *,
    winrm: dict[str, Any] | None = None,
) -> None:
    """Reject illegal transport/auth combinations with clear ProfileInvalid."""
    method = auth.method
    winrm = winrm or {}

    if transport == "ssh" and method not in _SSH_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method={method!r} is not valid "
            f"for transport=ssh (use one of {sorted(_SSH_AUTH_METHODS)}) ({path})"
        )

    if transport == "winrm" and method not in _WINRM_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method={method!r} is not valid "
            f"for transport=winrm (use one of {sorted(_WINRM_AUTH_METHODS)}) "
            f"({path})"
        )

    if transport != "winrm":
        return

    # Resolve effective protocol method (password → ntlm unless [winrm].auth set).
    protocol = _winrm_protocol_method(auth, winrm)

    has_password_material = bool(
        auth.password_path or auth.password_env or auth.has_inline_password
    )

    if protocol == "certificate":
        if auth.cert_path is None or auth.cert_key_path is None:
            raise ProfileInvalid(
                f"profile {profile_name!r}: certificate auth requires "
                f"[auth].cert_path and [auth].cert_key_path (paths only; "
                f"PEM bodies are not stored) ({path})"
            )
        scheme = str(winrm.get("scheme") or "http").lower()
        ssl_flag = winrm.get("ssl", False)
        if scheme not in ("https", "ssl") and not ssl_flag:
            raise ProfileInvalid(
                f"profile {profile_name!r}: certificate auth requires "
                f"[winrm].scheme = \"https\" (mutual TLS) ({path})"
            )

    if protocol in ("basic", "credssp") and not has_password_material:
        raise ProfileInvalid(
            f"profile {profile_name!r}: auth method {protocol!r} requires "
            f"password_path or password_env ({path})"
        )

    if protocol == "credssp":
        # CredSSP options may live under [winrm.credssp].
        credssp_tbl = winrm.get("credssp")
        if credssp_tbl is not None and not isinstance(credssp_tbl, dict):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [winrm.credssp] must be a table "
                f"({path})"
            )

    encryption = str(
        winrm.get("message_encryption") or winrm.get("encryption") or "auto"
    ).lower()
    # pypsrp: message encryption only with ntlm/kerberos/negotiate/credssp.
    if encryption == "always" and protocol in ("basic", "certificate"):
        raise ProfileInvalid(
            f"profile {profile_name!r}: message_encryption=always is "
            f"incompatible with auth={protocol!r} (use auto/never or "
            f"ntlm/kerberos/credssp) ({path})"
        )

    # Certificate fields without certificate method → invalid combo.
    if protocol != "certificate" and (
        auth.cert_path is not None or auth.cert_key_path is not None
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: cert_path/cert_key_path require "
            f"[auth].method = \"certificate\" (got {method!r}) ({path})"
        )


def _winrm_protocol_method(auth: AuthConfig, winrm: dict[str, Any]) -> str:
    """Map profile auth + [winrm].auth to a pypsrp auth protocol name."""
    explicit = winrm.get("auth") or winrm.get("auth_method")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()
    if auth.method == "password":
        return "ntlm"
    return auth.method.lower()


def _optional_secret_path(
    home: Path,
    table: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
) -> Path | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].{key} must be a non-empty string "
            f"({path})"
        )
    # Resolved path relative to home — never open/read the secret file here.
    return resolve_under_home(home, value)


def _optional_str(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileInvalid(
            f"profile {profile_name!r}: {key!r} must be a string ({path})"
        )
    return value


def _optional_int(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileInvalid(
            f"profile {profile_name!r}: {key!r} must be an integer ({path})"
        )
    return value


def _optional_table(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [{key}] must be a table ({path})"
        )
    return dict(value)


def _parse_defaults(table: Any) -> DefaultsConfig:
    if table is None:
        return DefaultsConfig()
    if not isinstance(table, dict):
        raise ConfigInvalid("[defaults] must be a table")
    base = DefaultsConfig()
    return DefaultsConfig(
        verbosity=str(table.get("verbosity", base.verbosity)),
        max_body_chars=int(table.get("max_body_chars", base.max_body_chars)),
        screen_cols=int(table.get("screen_cols", base.screen_cols)),
        screen_rows=int(table.get("screen_rows", base.screen_rows)),
        screen_term=str(table.get("screen_term", base.screen_term)),
        default_shell=str(table.get("default_shell", base.default_shell)),
        exec_timeout_ms=int(table.get("exec_timeout_ms", base.exec_timeout_ms)),
    )


def _parse_logging(table: Any) -> LoggingConfig:
    if table is None:
        return LoggingConfig()
    if not isinstance(table, dict):
        raise ConfigInvalid("[logging] must be a table")
    base = LoggingConfig()
    return LoggingConfig(
        level=str(table.get("level", base.level)),
        dir=str(table.get("dir", base.dir)),
        max_bytes=int(table.get("max_bytes", base.max_bytes)),
        backup_count=int(table.get("backup_count", base.backup_count)),
        audit=bool(table.get("audit", base.audit)),
    )


def _parse_security(table: Any) -> SecurityConfig:
    """Parse ``[security]``; unknown keys are ignored (forward-compatible load)."""
    if table is None:
        return SecurityConfig()
    if not isinstance(table, dict):
        raise ConfigInvalid("[security] must be a table")
    base = SecurityConfig()
    return SecurityConfig(
        strict_perms=bool(table.get("strict_perms", base.strict_perms)),
    )
