"""Per-transport capability flags (exec / fs / screen / ps)."""

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

    Override values are coerced with ``bool()``. Unknown keys are ignored.
    """
    caps = caps_for_transport(transport)
    if not overrides:
        return caps
    for key in CAP_ORDER:
        if key in overrides:
            caps[key] = bool(overrides[key])
    return caps


def format_caps(caps: Mapping[str, bool]) -> str:
    """Comma-separated enabled capability names in CAP_ORDER."""
    return ",".join(k for k in CAP_ORDER if caps.get(k))
