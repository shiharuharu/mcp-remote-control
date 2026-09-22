"""Remote path helpers for SFTP (POSIX and Windows OpenSSH path shapes).

String-level helpers only: absolute detection, join, parent, and root checks.
UNC share roots (``\\\\server\\share``) never climb above the share; drive roots
(``C:\\``) and POSIX ``/`` are terminal.
"""

from __future__ import annotations


def is_abs_remote(path: str) -> bool:
    """True for POSIX absolute, drive-absolute (``C:\\``), or UNC paths.

    The string is classified as given: whitespace is part of the name, so
    ``" /tmp"`` is relative and ``"/tmp "`` is absolute. Trimming first would
    make two different names look like one absolute path.
    """
    text = path or ""
    if not text:
        return False
    if text.startswith("/"):
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if len(text) >= 3 and text[1] == ":" and text[2] in "\\/":
        return True
    return False


def remote_join(base: str, name: str) -> str:
    """Join *base* and *name* using the separator style of *base*."""
    if not base:
        return name
    if not name or name in (".",):
        return base
    # Prefer backslash only when base is pure Windows-style (no forward slashes).
    if "\\" in base and "/" not in base.rstrip("\\"):
        sep = "\\"
        if base.endswith("\\") or base.endswith(":"):
            return base + name if base.endswith("\\") else base + "\\" + name
        return base + sep + name
    if base.endswith("/"):
        return base + name
    return base + "/" + name


def remote_parent(path: str) -> str | None:
    """Parent directory of a remote path, or ``None`` at a root.

    UNC: ``\\\\server\\share`` is the share root (no parent). Deeper paths climb
    toward the share root and never above it - ``\\\\server`` alone is not a
    valid operable target. Drive roots (``C:`` / ``C:/`` / ``C:\\``) and
    POSIX ``/`` also return ``None``.
    """
    text = (path or "").rstrip("/\\")
    if not text:
        return None
    # Windows drive root: C: or C:/
    if len(text) == 2 and text[1] == ":":
        return None
    if len(text) == 3 and text[1] == ":" and text[2] in "/\\":
        return None
    if text in ("/", "\\"):
        return None

    is_unc = text.startswith("\\\\") or text.startswith("//")
    if is_unc:
        # Accept both \\ and // forms; normalize to \\server\share style.
        parts = [p for p in text.replace("/", "\\").split("\\") if p]
        if len(parts) <= 2:
            return None
        return "\\\\" + "\\".join(parts[:-1])
    if "\\" in text and text[1:2] == ":":
        # Drive-letter backslash style (C:\foo\bar).
        idx = text.rfind("\\")
        if idx < 0:
            return None
        parent = text[:idx]
        if len(parent) == 2 and parent[1] == ":":
            return parent + "\\"
        return parent or None

    # POSIX
    if text == "/":
        return None
    idx = text.rfind("/")
    if idx < 0:
        return None
    if idx == 0:
        return "/"
    return text[:idx]


def is_root_remote(path: str) -> bool:
    """True for POSIX ``/``, drive roots, and UNC share roots (or above).

    Server-only UNC (``\\\\server``) is treated as a root so callers never
    try to mkdir/rm above the share.
    """
    text = (path or "").rstrip("/\\")
    if text in ("", "/"):
        return True
    if len(text) == 2 and text[1] == ":":
        return True
    # UNC share root: \\server\share (or //server/share).
    if text.startswith("\\\\") or text.startswith("//"):
        parts = [p for p in text.replace("/", "\\").split("\\") if p]
        if len(parts) <= 2:
            return True
    return False
