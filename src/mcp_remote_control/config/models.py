"""Config and Profile data models.

Secret material is represented only as **paths** (or env var *names*), never as
loaded file contents. ``repr`` / ``str`` intentionally omit any raw secret body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Transport = Literal["local", "ssh", "winrm"]
# SSH methods + WinRM protocol methods (see load._VALID_AUTH_METHODS).
AuthMethod = Literal[
    "password",
    "private_key",
    "private_key_path",
    "ssh_agent",
    "none",
    # WinRM / enterprise
    "ntlm",
    "basic",
    "kerberos",
    "credssp",
    "certificate",
]


@dataclass(frozen=True)
class AuthConfig:
    """Authentication recipe for a profile.

    Paths point at secret files; contents are never loaded here.
    Enterprise WinRM fields (cert paths, SPN, CredSSP options) are
    configuration only — PEM bodies and passwords stay on disk / env.
    """

    method: str
    key_path: Path | None = None
    passphrase_path: Path | None = None
    password_path: Path | None = None
    password_env: str | None = None
    # True if profile TOML contained an inline password= (discouraged).
    # Value is never stored so it cannot appear in repr.
    has_inline_password: bool = False
    # True if profile TOML contained inline private_key_pem= (discouraged).
    has_inline_private_key: bool = False
    # --- WinRM enterprise (paths / non-secret strings only) ---
    # Client certificate PEM path (pypsrp: certificate_pem).
    cert_path: Path | None = None
    # Client certificate private key PEM path (pypsrp: certificate_key_pem).
    cert_key_path: Path | None = None
    # Path to passphrase for encrypted cert key (never the passphrase body).
    cert_key_password_path: Path | None = None
    # SPN helper: ``SERVICE/host`` or bare hostname override for negotiate/kerberos.
    spn: str | None = None
    # Explicit negotiate overrides (optional; spn may populate these).
    negotiate_hostname_override: str | None = None
    negotiate_service: str | None = None
    negotiate_delegate: bool | None = None
    # CredSSP extras (pypsrp kwargs; non-secret).
    credssp_auth_mechanism: str | None = None  # auto | ntlm | kerberos
    credssp_disable_tlsv1_2: bool | None = None
    credssp_minimum_version: int | None = None

    def __repr__(self) -> str:
        parts = [f"method={self.method!r}"]
        if self.key_path is not None:
            parts.append(f"key_path={self.key_path!r}")
        if self.passphrase_path is not None:
            parts.append(f"passphrase_path={self.passphrase_path!r}")
        if self.password_path is not None:
            parts.append(f"password_path={self.password_path!r}")
        if self.password_env is not None:
            parts.append(f"password_env={self.password_env!r}")
        if self.has_inline_password:
            parts.append("has_inline_password=True")
        if self.has_inline_private_key:
            parts.append("has_inline_private_key=True")
        if self.cert_path is not None:
            parts.append(f"cert_path={self.cert_path!r}")
        if self.cert_key_path is not None:
            parts.append(f"cert_key_path={self.cert_key_path!r}")
        if self.cert_key_password_path is not None:
            parts.append(f"cert_key_password_path={self.cert_key_password_path!r}")
        if self.spn is not None:
            parts.append(f"spn={self.spn!r}")
        if self.negotiate_hostname_override is not None:
            parts.append(
                f"negotiate_hostname_override={self.negotiate_hostname_override!r}"
            )
        if self.negotiate_service is not None:
            parts.append(f"negotiate_service={self.negotiate_service!r}")
        if self.negotiate_delegate is not None:
            parts.append(f"negotiate_delegate={self.negotiate_delegate!r}")
        if self.credssp_auth_mechanism is not None:
            parts.append(f"credssp_auth_mechanism={self.credssp_auth_mechanism!r}")
        if self.credssp_disable_tlsv1_2 is not None:
            parts.append(
                f"credssp_disable_tlsv1_2={self.credssp_disable_tlsv1_2!r}"
            )
        if self.credssp_minimum_version is not None:
            parts.append(
                f"credssp_minimum_version={self.credssp_minimum_version!r}"
            )
        return f"AuthConfig({', '.join(parts)})"

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True)
class Profile:
    """Connection profile loaded from ``profiles/{name}.toml``."""

    name: str
    transport: Transport
    host: str | None = None
    port: int | None = None
    username: str | None = None
    label: str | None = None
    auth: AuthConfig | None = None
    # Optional transport / defaults tables kept as plain data (no secrets expected).
    ssh: dict[str, Any] = field(default_factory=dict)
    winrm: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)
    # Absolute path of the source file (for diagnostics).
    source_path: Path | None = None

    def __repr__(self) -> str:
        parts = [
            f"name={self.name!r}",
            f"transport={self.transport!r}",
        ]
        if self.host is not None:
            parts.append(f"host={self.host!r}")
        if self.port is not None:
            parts.append(f"port={self.port!r}")
        if self.username is not None:
            parts.append(f"username={self.username!r}")
        if self.label is not None:
            parts.append(f"label={self.label!r}")
        if self.auth is not None:
            parts.append(f"auth={self.auth!r}")
        if self.ssh:
            parts.append(f"ssh={self.ssh!r}")
        if self.winrm:
            parts.append(f"winrm={self.winrm!r}")
        if self.defaults:
            parts.append(f"defaults={self.defaults!r}")
        if self.caps:
            parts.append(f"caps={self.caps!r}")
        if self.source_path is not None:
            parts.append(f"source_path={self.source_path!r}")
        return f"Profile({', '.join(parts)})"

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True)
class DefaultsConfig:
    """Default UI / exec limits from ``[defaults]`` in config.toml."""

    verbosity: str = "normal"
    max_body_chars: int = 24000
    screen_cols: int = 120
    screen_rows: int = 40
    screen_term: str = "xterm-256color"
    default_shell: str = ""
    exec_timeout_ms: int = 60000


@dataclass(frozen=True)
class LoggingConfig:
    """Logging settings from ``[logging]`` in config.toml."""

    level: str = "info"
    dir: str = "logs"
    max_bytes: int = 10_485_760
    backup_count: int = 5
    audit: bool = False


@dataclass(frozen=True)
class SecurityConfig:
    """Global security flags parsed from ``[security]`` in config.toml.

    Secret-file permissions (``0o700`` ``secrets/``, ``0o600`` secret files)
    are always enforced by ``store.put_secret`` / ``ensure_home_layout``,
    independent of this config. When ``strict_perms`` is True,
    ``store.put_profile`` additionally tightens profile artifacts (profile
    TOML → ``0o600``, ``profiles/`` dir → ``0o700``).

    Unknown ``[security]`` keys (including removed legacy knobs such as
    ``redact_secrets_in_logs`` / ``allow_secret_paths_in_output``) are ignored
    on load so older config.toml files keep working.
    """

    strict_perms: bool = False


@dataclass(frozen=True)
class GlobalConfig:
    """Parsed global ``config.toml`` (or built-in defaults when absent)."""

    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    # True when no config.toml existed and defaults were used.
    from_defaults: bool = True
    source_path: Path | None = None
