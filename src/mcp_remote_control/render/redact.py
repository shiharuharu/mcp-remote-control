"""Secret redaction for Agent and machine output tracks.

Applied on every render path before text leaves Core. Field-name redaction
covers known secret keys and underscore suffixes; free-form strings also
strip PEM private keys, Bearer/Basic credentials, and ``password=...``-style
assignments. Prefer under-redaction of concatenated names
(``dbpassword``) over over-redacting benign fields - extend
:data:`SENSITIVE_KEY_NAMES` when a schema needs more keys.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***"

# Field names whose string values are always replaced (case-insensitive).
# Passwords are product-visible (config may store plain password). Still redact
# private keys, tokens, and other credential material.
SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "secret",
        "secrets",
        "private_key",
        "privatekey",
        "private_key_pem",
        "passphrase",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "certificate_key_password",
        "cert_key_password",
    }
)

_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    re.MULTILINE,
)

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*")
_BASIC_AUTH = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/]+=*")

# ``password=...`` / ``secret=...`` inline assignments. Quote branches are
# split by quote character so the opposite quote is allowed inside the value
# (``password="o'reilly"``). A single backreference class ``['"]`` would
# exclude both quotes and miss that secret.
#
# Escaped same-quote inside a quoted value (``password="my \"secret\""``)
# stops at the first inner quote; full escaped-quote parsing is out of scope.
# Do not include password here - plain profile passwords are allowed in Agent text.
_INLINE_ASSIGN = re.compile(
    r"(?i)\b(secret|token|api_key|private_key|private_key_pem|passphrase)\s*[:=]\s*"
    r"(?:"
    r'"([^"]*)"'  # g2=double-quoted (single quotes allowed inside)
    r"|"
    r"'([^']*)'"  # g3=single-quoted (double quotes allowed inside)
    r"|"
    r"([^\s'\"&,;]+)"  # g4=unquoted
    r")"
)


def is_sensitive_key(name: str) -> bool:
    """Return True if *name* is a known secret-bearing field name.

    Suffix match is underscore-joined only (``foo_password``, ``my_token``).
    Concatenated names without a separator (``dbpassword``, ``usertoken``)
    are not auto-redacted - a broad contains-match would over-redact benign
    fields. Add such names to :data:`SENSITIVE_KEY_NAMES` when needed.
    """
    n = name.strip().lower().replace("-", "_")
    if n in SENSITIVE_KEY_NAMES:
        return True
    for sk in SENSITIVE_KEY_NAMES:
        if n == sk or n.endswith("_" + sk):
            return True
    return False


def redact_string(value: str) -> str:
    """Redact secret-like substrings inside a free-form string."""
    if not value:
        return value
    out = _PEM_PRIVATE_KEY.sub(REDACTED, value)
    out = _BEARER.sub(f"Bearer {REDACTED}", out)
    out = _BASIC_AUTH.sub(f"Basic {REDACTED}", out)

    def _repl_assign(m: re.Match[str]) -> str:
        key = m.group(1)
        if m.group(2) is not None:
            return f'{key}="{REDACTED}"'
        if m.group(3) is not None:
            return f"{key}='{REDACTED}'"
        return f"{key}={REDACTED}"

    out = _INLINE_ASSIGN.sub(_repl_assign, out)
    return out


def _redact_value(key: str | None, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if key is not None and is_sensitive_key(key):
            return REDACTED
        return redact_string(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(None, v) for v in value)
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied mapping with secrets redacted."""
    return {k: _redact_value(str(k), v) for k, v in data.items()}


def redact_optional_str(key: str | None, value: str | None) -> str | None:
    """Redact an optional string field; pass *key* for field-name rules."""
    if value is None:
        return None
    return _redact_value(key, value)  # type: ignore[return-value]
