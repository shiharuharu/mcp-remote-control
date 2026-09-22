"""Per-transport capability flags (exec / fs / screen / ps).

Also owns the endpoint-package string-safe TOML truthiness helper used by
caps overrides and registry WinRM/SSH flag parsing (ssl, cert_validation,
credssp, force_utf8, ...) so ``\"false\"`` / ``\"0\"`` never enable via
``bool(str)``.
"""

from __future__ import annotations

from collections.abc import Mapping

# Stable order for caps= summary tokens.
CAP_ORDER: tuple[str, ...] = ("exec", "fs", "screen", "ps")

# local/ssh: shell-family tools; winrm: PowerShell (ps), no screen PTY.
_MATRIX: dict[str, dict[str, bool]] = {
    "local": {"exec": True, "fs": True, "screen": True, "ps": False},
    "ssh": {"exec": True, "fs": True, "screen": True, "ps": False},
    "winrm": {"exec": True, "fs": True, "screen": False, "ps": True},
}

# String tokens accepted as False / True (case-insensitive, stripped).
_FALSE_STRINGS: frozenset[str] = frozenset({"false", "0", "no", "off", ""})
_TRUE_STRINGS: frozenset[str] = frozenset({"true", "1", "yes", "on"})


def coerce_toml_bool(value: object) -> bool:
    """String-safe TOML/JSON bool for caps and endpoint profile flags.

    Unlike ``bool()``, non-empty strings such as ``\"false\"`` / ``\"0\"`` /
    ``\"no\"`` / ``\"off\"`` are False. ``\"true\"`` / ``\"1\"`` / ``\"yes\"`` /
    ``\"on\"`` are True (case-insensitive, stripped). Unknown string tokens
    and non-scalars (dict / list / bytes / other objects) fail closed (False)
    so a typo or mis-shaped table cannot enable a cap or flag.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        # 0 / 0.0 -> False; non-zero (incl. 1) -> True
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        # Unknown string: do not enable (bool(\"false\") would be True).
        return False
    # Mapping / Sequence (non-str) / bytes / other objects: fail closed.
    return False


def caps_for_transport(transport: str) -> dict[str, bool]:
    """Return capability flags for *transport* (copy; never shared mutable)."""
    base = _MATRIX.get(transport)
    if base is None:
        return {k: False for k in CAP_ORDER}
    return dict(base)


def merge_caps(
    transport: str,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, bool]:
    """Base matrix for *transport*, optionally merged with profile ``caps`` table.

    Override values use :func:`coerce_toml_bool`: ``False`` / ``0`` /
    ``\"false\"`` / ``\"0\"`` / ``\"no\"`` / ``\"off\"`` disable; ``True`` /
    ``1`` / ``\"true\"`` / ``\"1\"`` / ``\"yes\"`` / ``\"on\"`` enable (strings
    case-insensitive). Unknown string tokens and non-scalars (dict / list /
    etc.) fail closed (False). Unknown keys are ignored.
    """
    caps = caps_for_transport(transport)
    if not overrides:
        return caps
    for key in CAP_ORDER:
        if key in overrides:
            caps[key] = coerce_toml_bool(overrides[key])
    return caps


def format_caps(caps: Mapping[str, bool]) -> str:
    """Comma-separated enabled capability names in CAP_ORDER."""
    return ",".join(k for k in CAP_ORDER if caps.get(k))
