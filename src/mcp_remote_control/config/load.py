"""Load global ``config.toml`` and connection profiles.

Profile names are validated against a safe pattern *before* path join so
values like ``../config`` cannot escape ``profiles/``. Auth material is
represented only as paths or env-var names; secret file bodies are never
read by this module.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, cast

from mcp_remote_control.config.errors import (
    ConfigInvalid,
    ProfileInvalid,
    ProfileNotFound,
    public_path_for_msg,
    reject_dual_password_sources,
)
from mcp_remote_control.config.models import (
    PROFILE_NAME_RE,
    VALID_TRANSPORTS,
    AuthConfig,
    DefaultsConfig,
    GlobalConfig,
    Profile,
    SecurityConfig,
    Transport,
)
from mcp_remote_control.config.paths import (
    config_toml_path,
    profiles_dir,
    resolve_under_home,
)

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
# WinRM open-time probe intensity (profile [winrm].probe / [defaults].winrm_probe).
_VALID_WINRM_PROBE_MODES: frozenset[str] = frozenset({"skip", "light", "full"})
# String tokens for config bools (case-insensitive, stripped). Unlike bare
# ``bool()``, ``"false"`` / ``"0"`` map to False (not True).
_FALSE_BOOL_STRINGS: frozenset[str] = frozenset({"false", "0", "no", "off", ""})
_TRUE_BOOL_STRINGS: frozenset[str] = frozenset({"true", "1", "yes", "on"})


def _loc(home: Path, path: Path | str | None) -> str:
    """Home-relative path fragment for exception messages (profiles/..., etc.)."""
    return public_path_for_msg(home, path)


def load_config(home: Path) -> GlobalConfig:
    """Load optional ``{home}/config.toml``; missing file -> built-in defaults."""
    path = config_toml_path(home)
    if not path.is_file():
        return GlobalConfig(from_defaults=True, source_path=None)

    loc = _loc(home, path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigInvalid(f"cannot read config.toml: {loc}: {exc}") from exc

    try:
        # utf-8-sig strips a leading UTF-8 BOM (Notepad/PowerShell saves).
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalid(f"invalid TOML in config.toml: {loc}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigInvalid(f"config.toml is not valid UTF-8: {loc}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigInvalid(f"config.toml root must be a table: {loc}")

    try:
        defaults = _parse_defaults(data.get("defaults"))
        security = _parse_security(data.get("security"))
    except ConfigInvalid:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ConfigInvalid(f"invalid config.toml values in {loc}: {exc}") from exc

    return GlobalConfig(
        defaults=defaults,
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
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileInvalid(
            f"profile name {name!r} must match {PROFILE_NAME_RE.pattern}"
        )

    path = profiles_dir(home) / f"{name}.toml"
    loc = _loc(home, path)
    if not path.is_file():
        raise ProfileNotFound(f"profile not found: {name!r} ({loc})")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileInvalid(f"cannot read profile {name!r}: {loc}: {exc}") from exc

    try:
        # utf-8-sig strips a leading UTF-8 BOM (Notepad/PowerShell saves).
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileInvalid(f"invalid TOML in profile {name!r}: {loc}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProfileInvalid(f"profile {name!r} is not valid UTF-8: {loc}: {exc}") from exc

    if not isinstance(data, dict):
        raise ProfileInvalid(f"profile {name!r} root must be a table: {loc}")

    return _profile_from_table(home, name, path, data)


def _profile_from_table(
    home: Path,
    expected_name: str,
    path: Path,
    data: dict[str, Any],
) -> Profile:
    loc = _loc(home, path)
    file_name = data.get("name")
    if file_name is None:
        raise ProfileInvalid(
            f"profile {expected_name!r}: missing required field 'name' ({loc})"
        )
    if not isinstance(file_name, str):
        raise ProfileInvalid(
            f"profile {expected_name!r}: 'name' must be a string ({loc})"
        )
    if file_name != expected_name:
        raise ProfileInvalid(
            f"profile name {file_name!r} does not match file stem "
            f"{expected_name!r} ({loc})"
        )
    if not PROFILE_NAME_RE.fullmatch(file_name):
        raise ProfileInvalid(
            f"profile name {file_name!r} must match "
            f"{PROFILE_NAME_RE.pattern} ({loc})"
        )

    transport_raw = data.get("transport")
    if transport_raw is None:
        raise ProfileInvalid(
            f"profile {expected_name!r}: missing required field 'transport' ({loc})"
        )
    if not isinstance(transport_raw, str) or transport_raw not in VALID_TRANSPORTS:
        raise ProfileInvalid(
            f"profile {expected_name!r}: transport must be one of "
            f"{sorted(VALID_TRANSPORTS)}, got {transport_raw!r} ({loc})"
        )
    # Validated against VALID_TRANSPORTS == {"local","ssh","winrm"}.
    transport = cast(Transport, transport_raw)

    host = _optional_str(data, "host", expected_name, path, home=home)
    username = _optional_str(data, "username", expected_name, path, home=home)
    label = _optional_str(data, "label", expected_name, path, home=home)
    port = _optional_int(data, "port", expected_name, path, home=home)

    if transport in ("ssh", "winrm"):
        if not host:
            raise ProfileInvalid(
                f"profile {expected_name!r}: 'host' is required for "
                f"transport={transport!r} ({loc})"
            )
        if not username:
            raise ProfileInvalid(
                f"profile {expected_name!r}: 'username' is required for "
                f"transport={transport!r} ({loc})"
            )
        if port is None:
            port = 22 if transport == "ssh" else 5985

    auth_table = data.get("auth")
    auth: AuthConfig | None = None
    if auth_table is not None:
        if not isinstance(auth_table, dict):
            raise ProfileInvalid(
                f"profile {expected_name!r}: [auth] must be a table ({loc})"
            )
        auth = _parse_auth(home, expected_name, path, auth_table)
    elif transport in ("ssh", "winrm"):
        # Auth section optional (e.g. ssh_agent-style setups).
        auth = None

    ssh = _optional_table(data, "ssh", expected_name, path, home=home)
    winrm = _optional_table(data, "winrm", expected_name, path, home=home)
    defaults = _optional_table(data, "defaults", expected_name, path, home=home)
    caps = _optional_table(data, "caps", expected_name, path, home=home)

    if winrm:
        _validate_winrm_probe_field(
            winrm, key="probe", profile_name=expected_name, path=path, home=home
        )
    if defaults:
        _validate_winrm_probe_field(
            defaults,
            key="winrm_probe",
            profile_name=expected_name,
            path=path,
            home=home,
            section="defaults",
        )

    if auth is not None:
        _validate_auth_for_transport(
            expected_name, path, transport, auth, home=home, winrm=winrm
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
    loc = _loc(home, path)
    method = table.get("method")
    if method is None:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method is required ({loc})"
        )
    if not isinstance(method, str) or method not in _VALID_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method must be one of "
            f"{sorted(_VALID_AUTH_METHODS)}, got {method!r} ({loc})"
        )

    key_path = _optional_secret_path(home, table, "key_path", profile_name, path)
    passphrase_path = _optional_secret_path(
        home, table, "passphrase_path", profile_name, path
    )
    password_path = _optional_secret_path(
        home, table, "password_path", profile_name, path
    )
    password_env = table.get("password_env")
    if password_env is not None:
        if not isinstance(password_env, str):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [auth].password_env must be a "
                f"string ({loc})"
            )
        # Whitespace-only env name is unset (no material).
        password_env = password_env.strip() or None

    # Inline password is allowed and stored for connect (product: not private).
    # Empty / whitespace-only password is treated as unset - never claim material.
    password_inline: str | None = None
    raw_pw = table.get("password")
    if raw_pw is not None:
        if not isinstance(raw_pw, str):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [auth].password must be a string ({loc})"
            )
        if raw_pw.strip():
            password_inline = raw_pw
    has_inline_password = password_inline is not None

    # Hard-reject dual password sources (no silent priority / ambiguous material).
    reject_dual_password_sources(
        password=has_inline_password,
        password_path=password_path,
        password_env=password_env,
        profile_name=profile_name,
        loc=loc,
    )

    # Private key PEM bodies remain discouraged / not stored on AuthConfig.
    has_inline_private_key = (
        "private_key_pem" in table and table.get("private_key_pem") is not None
    )
    # Inline cert PEM bodies are also discouraged (paths only).
    if table.get("certificate_pem") is not None and not isinstance(
        table.get("certificate_pem"), str
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].certificate_pem must be a path "
            f"string when set ({loc})"
        )
    if any(
        k in table and table.get(k) is not None
        for k in ("certificate_body", "cert_pem_body", "private_key_body")
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: inline certificate/key bodies are not "
            f"allowed; use cert_path / cert_key_path ({loc})"
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
            f"profile {profile_name!r}: [auth].spn must be a string ({loc})"
        )

    negotiate_hostname_override = table.get("negotiate_hostname_override")
    if negotiate_hostname_override is not None and not isinstance(
        negotiate_hostname_override, str
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_hostname_override "
            f"must be a string ({loc})"
        )
    negotiate_service = table.get("negotiate_service")
    if negotiate_service is not None and not isinstance(negotiate_service, str):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_service must be a "
            f"string ({loc})"
        )
    negotiate_delegate = table.get("negotiate_delegate")
    if negotiate_delegate is not None and not isinstance(negotiate_delegate, bool):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].negotiate_delegate must be a "
            f"bool ({loc})"
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
                f"{credssp_auth_mechanism!r} ({loc})"
            )
    credssp_disable_tlsv1_2 = table.get("credssp_disable_tlsv1_2")
    if credssp_disable_tlsv1_2 is not None and not isinstance(
        credssp_disable_tlsv1_2, bool
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].credssp_disable_tlsv1_2 must "
            f"be a bool ({loc})"
        )
    credssp_minimum_version = table.get("credssp_minimum_version")
    if credssp_minimum_version is not None:
        if isinstance(credssp_minimum_version, bool) or not isinstance(
            credssp_minimum_version, int
        ):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [auth].credssp_minimum_version "
                f"must be an integer ({loc})"
            )

    return AuthConfig(
        method=method,
        key_path=key_path,
        passphrase_path=passphrase_path,
        password_path=password_path,
        password_env=password_env,
        password=password_inline,
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
    home: Path | None = None,
    winrm: dict[str, Any] | None = None,
) -> None:
    """Reject illegal transport/auth combinations with clear ProfileInvalid."""
    loc = _loc(home, path) if home is not None else public_path_for_msg(None, path)
    method = auth.method
    winrm = winrm or {}

    if transport == "ssh" and method not in _SSH_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method={method!r} is not valid "
            f"for transport=ssh (use one of {sorted(_SSH_AUTH_METHODS)}) ({loc})"
        )

    if transport == "winrm" and method not in _WINRM_AUTH_METHODS:
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].method={method!r} is not valid "
            f"for transport=winrm (use one of {sorted(_WINRM_AUTH_METHODS)}) "
            f"({loc})"
        )

    if transport != "winrm":
        return

    # Resolve effective protocol method (password -> ntlm unless [winrm].auth set).
    protocol = _winrm_protocol_method(auth, winrm)

    # Empty inline password is already treated as unset in _parse_auth; still
    # require a non-empty password body (whitespace-only is not material).
    has_password_material = bool(
        auth.password_path
        or (auth.password_env and str(auth.password_env).strip())
        or (auth.password is not None and str(auth.password).strip())
        or auth.has_inline_password
    )

    if protocol == "certificate":
        if auth.cert_path is None or auth.cert_key_path is None:
            raise ProfileInvalid(
                f"profile {profile_name!r}: certificate auth requires "
                f"[auth].cert_path and [auth].cert_key_path (paths only; "
                f"PEM bodies are not stored) ({loc})"
            )
        scheme = str(winrm.get("scheme") or "http").lower()
        ssl_flag = winrm.get("ssl", False)
        if scheme not in ("https", "ssl") and not ssl_flag:
            raise ProfileInvalid(
                f"profile {profile_name!r}: certificate auth requires "
                f"[winrm].scheme = \"https\" (mutual TLS) ({loc})"
            )

    if protocol in ("basic", "credssp") and not has_password_material:
        # Plain [auth].password is first-class (preferred bootstrap); also
        # accept password_path / password_env. Match dual-source wording so
        # agents see all three options, not only path/env.
        raise ProfileInvalid(
            f"profile {profile_name!r}: auth method {protocol!r} requires "
            f"password | password_path | password_env ({loc})"
        )

    if protocol == "credssp":
        # CredSSP options may live under [winrm.credssp].
        credssp_tbl = winrm.get("credssp")
        if credssp_tbl is not None and not isinstance(credssp_tbl, dict):
            raise ProfileInvalid(
                f"profile {profile_name!r}: [winrm.credssp] must be a table "
                f"({loc})"
            )

    encryption = str(
        winrm.get("message_encryption") or winrm.get("encryption") or "auto"
    ).lower()
    # pypsrp: message encryption only with ntlm/kerberos/negotiate/credssp.
    if encryption == "always" and protocol in ("basic", "certificate"):
        raise ProfileInvalid(
            f"profile {profile_name!r}: message_encryption=always is "
            f"incompatible with auth={protocol!r} (use auto/never or "
            f"ntlm/kerberos/credssp) ({loc})"
        )

    # Certificate fields without certificate method -> invalid combo.
    if protocol != "certificate" and (
        auth.cert_path is not None or auth.cert_key_path is not None
    ):
        raise ProfileInvalid(
            f"profile {profile_name!r}: cert_path/cert_key_path require "
            f"[auth].method = \"certificate\" (got {method!r}) ({loc})"
        )


def _winrm_protocol_method(auth: AuthConfig, winrm: dict[str, Any]) -> str:
    """Map profile auth + [winrm].auth to a pypsrp auth protocol name."""
    explicit = winrm.get("auth") or winrm.get("auth_method")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()
    if auth.method == "password":
        return "ntlm"
    return auth.method.lower()


def looks_like_pem_armor(value: str) -> bool:
    """True when stripped *value* is PEM armor (a body), not a filesystem path.

    Path fields must never ``Path.resolve`` a body. Armor starts with
    ``-----BEGIN`` (CERTIFICATE / PRIVATE KEY / RSA / EC / OPENSSH).
    """
    text = value.strip()
    if not text.startswith("-----BEGIN"):
        return False
    first = text.splitlines()[0].upper()
    return any(
        token in first
        for token in ("CERTIFICATE", "PRIVATE KEY", "RSA", "EC", "OPENSSH")
    )


# Path shorthand the module itself introduces for files under the config home.
_SECRETS_PREFIX = "secrets/"


def looks_like_pem_body_in_path(value: str) -> bool:
    """True when *value* offered for a path field actually carries a PEM body.

    :func:`looks_like_pem_armor` inspects only the leading line, but a path
    field is normalized on the way to storage, and every normalizer can hide
    the armor behind a wrapper: the module's own ``secrets/<name>`` shorthand,
    a UTF-8 BOM, or a stray line ahead of the armor. A wrapped body that slips
    through is persisted into ``profiles/<name>.toml`` as if it were a path and
    echoed verbatim by ``get_profile``, so scan every line with those wrappers
    removed.
    """
    for line in value.splitlines():
        text = line.strip().lstrip("\ufeff").strip()
        if text.startswith(_SECRETS_PREFIX):
            text = text[len(_SECRETS_PREFIX) :]
        # Strip BOM/space again: the shorthand and the BOM can be nested in
        # either order, and both are invisible in a rendered profile.
        text = text.strip().lstrip("\ufeff").strip()
        if looks_like_pem_armor(text):
            return True
    return False


def _expand_user_lenient(text: str) -> Path:
    """``Path.expanduser`` that tolerates a ``~user`` with no home on this host.

    ``pathlib`` raises ``RuntimeError`` when the named user cannot be looked
    up, while ``os.path.expanduser`` leaves the ``~user`` component untouched.
    An unresolvable ``~user`` names no location here, so callers judge the
    literal form; without this, one legal TOML value (``~deploy/.ssh/id_rsa``)
    escapes as an unhandled ``RuntimeError`` out of every profile load.
    """
    try:
        return Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.expanduser(text))


def auth_path_escapes_home(value: str) -> bool:
    """True when *value* is a relative path that climbs out with a ``..`` step.

    Expansion order matches :func:`resolve_under_home` - ``$VAR`` then ``~`` -
    so a variable that expands to a traversal is caught instead of being stored
    as a literal ``$VAR`` component. Only relative paths are tested: an
    absolute path names a location the caller chose outright and never resolves
    through the config home, so a ``..`` in one escapes nothing that
    ``resolve_under_home`` does not already permit.

    An unresolvable ``~user`` is not a home escape and is not reported as one:
    it stays literal here, and :func:`_optional_secret_path` rejects it as an
    unusable path instead.
    """
    text = os.path.expandvars(value.strip())
    p = _expand_user_lenient(text)
    if p.is_absolute():
        return False
    return ".." in p.parts


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
        loc = _loc(home, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].{key} must be a non-empty string "
            f"({loc})"
        )
    # Path fields are filesystem paths only - never resolve a PEM body. The
    # stored value can carry the ``secrets/`` shorthand or a BOM, so the armor
    # test has to look past those wrappers (see looks_like_pem_body_in_path).
    if looks_like_pem_body_in_path(value):
        loc = _loc(home, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].{key} must be a filesystem "
            f"path, not a PEM body ({loc})"
        )
    # A relative path with a ``..`` component climbs out of the config home,
    # the same thing ``put_secret`` rejects in a secret name. Absolute paths
    # stay allowed - pointing at a key outside the home is an intended
    # capability of these fields (see resolve_under_home).
    if auth_path_escapes_home(value):
        loc = _loc(home, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].{key} must not contain '..' "
            f"path segments; use an absolute path or store the file under the "
            f"config home (secrets/<name>) ({loc})"
        )
    # Resolved path relative to home - never open/read the secret file here.
    # Resolution can itself fail on a value pathlib refuses to expand (an
    # unresolvable ``~user``, a symlink loop, an embedded NUL). That is a
    # defect in this profile, not in the config home, so it must surface as
    # PROFILE_INVALID rather than as an escaping RuntimeError/OSError.
    try:
        return resolve_under_home(home, value)
    except (OSError, RuntimeError, ValueError) as exc:
        loc = _loc(home, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: [auth].{key} {value!r} cannot be "
            f"resolved to a filesystem path ({exc}) ({loc})"
        ) from exc


def _optional_str(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
    *,
    home: Path | None = None,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        loc = _loc(home, path) if home is not None else public_path_for_msg(None, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: {key!r} must be a string ({loc})"
        )
    return value


def _optional_int(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
    *,
    home: Path | None = None,
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        loc = _loc(home, path) if home is not None else public_path_for_msg(None, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: {key!r} must be an integer ({loc})"
        )
    return value


def _optional_table(
    data: dict[str, Any],
    key: str,
    profile_name: str,
    path: Path,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        loc = _loc(home, path) if home is not None else public_path_for_msg(None, path)
        raise ProfileInvalid(
            f"profile {profile_name!r}: [{key}] must be a table ({loc})"
        )
    return dict(value)


def _section_str(
    table: dict[str, Any], section: str, key: str, default: str
) -> str:
    """Require a real ``str`` for ``[{section}].*`` string fields.

    Rejects bool/int/float/containers so ``str(True) -> "True"`` cannot
    silently poison verbosity.
    """
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str):
        raise ConfigInvalid(
            f"[{section}].{key} must be a string (got {type(value).__name__})"
        )
    return value


def _section_int(
    table: dict[str, Any], section: str, key: str, default: int
) -> int:
    """Require a real ``int`` (not bool) for ``[{section}].*`` int fields.

    Aligns with ``_optional_int``: ``bool`` is an ``int`` subclass, so
    ``int(True) == 1`` must not pass as a bytes value.
    Floats, strings, and non-scalars raise ``ConfigInvalid``.
    """
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigInvalid(
            f"[{section}].{key} must be an integer (got {type(value).__name__})"
        )
    return value


def _defaults_str(table: dict[str, Any], key: str, default: str) -> str:
    """Require a real ``str`` for ``[defaults].*`` string fields."""
    return _section_str(table, "defaults", key, default)


def _defaults_int(table: dict[str, Any], key: str, default: int) -> int:
    """Require a real ``int`` (not bool) for ``[defaults].*`` int fields."""
    return _section_int(table, "defaults", key, default)


def _parse_defaults(table: Any) -> DefaultsConfig:
    if table is None:
        return DefaultsConfig()
    if not isinstance(table, dict):
        raise ConfigInvalid("[defaults] must be a table")
    base = DefaultsConfig()
    winrm_probe = base.winrm_probe
    if "winrm_probe" in table:
        winrm_probe = _parse_winrm_probe_mode_value(
            table["winrm_probe"],
            where="[defaults].winrm_probe",
            error_cls=ConfigInvalid,
        )
    return DefaultsConfig(
        verbosity=_defaults_str(table, "verbosity", base.verbosity),
        max_body_chars=_defaults_int(table, "max_body_chars", base.max_body_chars),
        winrm_probe=winrm_probe,
    )


def _parse_winrm_probe_mode_value(
    value: Any,
    *,
    where: str,
    error_cls: type[Exception] = ConfigInvalid,
) -> str:
    """Normalize and validate a winrm open-probe mode token.

    Accepts ``skip`` / ``light`` / ``full`` (case-insensitive) and bool
    convenience (``true``->full, ``false``->skip). Raises *error_cls* on junk.
    """
    if isinstance(value, bool):
        return "full" if value else "skip"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _VALID_WINRM_PROBE_MODES:
            return text
        # Common aliases used in lab configs / env.
        if text in ("skipped", "none", "off", "false", "0", "no"):
            return "skip"
        if text in ("soft", "partial"):
            return "light"
        if text in ("hard", "true", "1", "yes", "on"):
            return "full"
        raise error_cls(
            f"{where} must be one of {sorted(_VALID_WINRM_PROBE_MODES)}, "
            f"got {value!r}"
        )
    raise error_cls(
        f"{where} must be a string or bool (got {type(value).__name__})"
    )


def _validate_winrm_probe_field(
    table: dict[str, Any],
    *,
    key: str,
    profile_name: str,
    path: Path,
    home: Path | None,
    section: str | None = None,
) -> None:
    """If *key* is present on a profile table, require a valid probe mode.

    Mutates *table* in place to store the normalized token so runtime resolve
    sees a canonical ``skip|light|full`` string.
    """
    if key not in table:
        return
    loc = _loc(home, path) if home is not None else public_path_for_msg(None, path)
    section_label = section or "winrm"
    where = f"profile {profile_name!r}: [{section_label}].{key} ({loc})"
    try:
        normalized = _parse_winrm_probe_mode_value(
            table[key], where=where, error_cls=ProfileInvalid
        )
    except ProfileInvalid:
        raise
    table[key] = normalized


def _parse_config_bool(value: object, *, section: str, key: str) -> bool:
    """Strict config bool (TOML/string-safe).

    Native ``bool`` passes through. Strings use conventional tokens
    (``\"false\"`` / ``\"0\"`` / ``\"no\"`` / ``\"off\"`` -> False;
    ``\"true\"`` / ``\"1\"`` / ``\"yes\"`` / ``\"on\"`` -> True;
    case-insensitive). Integers/floats: 0 -> False, non-zero -> True.
    Unknown strings and non-scalars (dict/list/etc.) raise
    ``ConfigInvalid`` - never ``bool(value)``, which would invert
    ``\"false\"`` to True and treat non-empty containers as True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # bool is an int subclass but already handled above.
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_BOOL_STRINGS:
            return True
        if text in _FALSE_BOOL_STRINGS:
            return False
        raise ConfigInvalid(
            f"[{section}].{key} must be a boolean (got string {value!r})"
        )
    raise ConfigInvalid(
        f"[{section}].{key} must be a boolean (got {type(value).__name__})"
    )


def _parse_security(table: Any) -> SecurityConfig:
    """Parse ``[security]``; unknown keys are ignored (forward-compatible load)."""
    if table is None:
        return SecurityConfig()
    if not isinstance(table, dict):
        raise ConfigInvalid("[security] must be a table")
    base = SecurityConfig()
    strict_raw = table.get("strict_perms", base.strict_perms)
    return SecurityConfig(
        strict_perms=_parse_config_bool(
            strict_raw, section="security", key="strict_perms"
        ),
    )
