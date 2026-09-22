"""Shared FS types, errors, and text/binary detection.

Capability-level models used by every backend (local / sftp / winrm). Backend
id is optional meta via ``FsBackend.via``; the agent-facing surface is the
unified ``fs_*`` API, not protocol-named tools.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Default cap for fs_read content pulled into agent context (1 MiB).
DEFAULT_READ_MAX_BYTES: int = 1_048_576

# Chunk size for put/get when a progress callback is active.
DEFAULT_TRANSFER_CHUNK: int = 256 * 1024

# progress(bytes_done, total_or_none) - total is None when size is unknown.
ProgressCallback = Callable[[int, int | None], None]


def report_progress(
    progress: ProgressCallback | None,
    bytes_done: int,
    total: int | None,
) -> None:
    """Invoke *progress* if provided; no-op when the callback is ``None``."""
    if progress is not None:
        progress(bytes_done, total)


def _utf16_ascii_half_is_printable(sample: bytes, indices: list[int]) -> bool:
    """True when the non-NUL bytes at *indices* look like ASCII text.

    For ASCII-range UTF-16LE/BE the non-NUL half of each 16-bit unit is the
    character byte (``0x20``-``0x7E`` printable, or ``0x09``/``0x0A``/``0x0D``
    for tab/LF/CR). NULs in that half are allowed (U+0000). If the non-NUL
    bytes are mostly non-printable control/high bytes - e.g. a big-endian
    uint32 record ``b"\\x00\\x00\\x00\\x01"`` where the non-NULs are
    ``0x01``/``0x02`` - the payload is binary, not UTF-16 text.
    """
    if not indices:
        return True
    non_nul = [sample[i] for i in indices if sample[i] != 0]
    if not non_nul:
        # All-NUL ASCII half (e.g. b"\x00\x00"): degenerate. Allow; the caller
        # still must decode successfully, and the UTF-8 fallback covers edges.
        return True
    printable = sum(
        1
        for b in non_nul
        if b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E
    )
    return printable / len(non_nul) >= 0.8


def _looks_like_utf16(data: bytes) -> tuple[bool, str]:
    """Heuristic for ASCII-range UTF-16LE/BE without a BOM.

    Requires even length and that >=80% of the high bytes in the first 64 bytes
    are NUL - the common shape of ASCII-range UTF-16. Random binary with NULs
    rarely matches that alternating pattern.

    Alternating NULs alone are not enough: a small big-endian uint32 stream
    (``b"\\x00\\x00\\x00\\x01\\x00\\x00\\x00\\x02..."``) has every even byte NUL
    but the non-NUL bytes are control values, not characters. After the NUL
    check, the non-NUL half of each unit must also be mostly printable ASCII
    or common controls (``\\n``/``\\r``/``\\t``). NUL-dense binary with
    control/high bytes is rejected; real ASCII-range UTF-16LE/BE is accepted.
    """
    if len(data) < 2 or len(data) % 2 != 0:
        return False, ""
    sample = data[:64]
    n = len(sample)
    le_odds = list(range(1, n, 2))
    be_evens = list(range(0, n, 2))
    le_ratio = (
        sum(1 for i in le_odds if sample[i] == 0) / len(le_odds) if le_odds else 0.0
    )
    be_ratio = (
        sum(1 for i in be_evens if sample[i] == 0) / len(be_evens) if be_evens else 0.0
    )
    # UTF-16LE: high byte at odd indices (NUL check), char at even (printable).
    # UTF-16BE: the reverse.
    if le_ratio >= 0.8 and _utf16_ascii_half_is_printable(sample, be_evens):
        return True, "utf-16-le"
    if be_ratio >= 0.8 and _utf16_ascii_half_is_printable(sample, le_odds):
        return True, "utf-16-be"
    return False, ""


def detect_text(data: bytes) -> tuple[bool, str | None]:
    """Classify *data* as text vs binary and report a decode encoding.

    Shared by every backend so local / sftp / winrm agree. Windows hosts often
    serve UTF-16; matching that on local and SFTP is intentional.

    Returns ``(True, encoding)`` for text (a name accepted by ``bytes.decode``)
    or ``(False, None)`` for binary.

    Detection order:

    1. BOM-prefixed UTF-16 (``FF FE`` / ``FE FF``) -> encoding ``"utf-16"`` so
       ``decode("utf-16")`` picks endianness and strips the BOM.
    2. Any interior NUL -> alternating-NUL UTF-16 heuristic for no-BOM
       ASCII-range LE/BE, plus the printable-ASCII-half guard (rejects
       NUL-dense binary with control/high non-NUL bytes).
    3. Strict UTF-8 decode -> ``"utf-8"``.
    4. Otherwise binary.
    """
    # BOM-prefixed UTF-16: report "utf-16" so decode auto-detects endianness
    # and strips the BOM.
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            data.decode("utf-16")
            return True, "utf-16"
        except UnicodeDecodeError:
            return False, None
    if b"\x00" in data:
        # Windows text is commonly UTF-16LE without a BOM; ASCII letters
        # alternate with NUL. Try that before declaring binary.
        ok, enc = _looks_like_utf16(data)
        if ok:
            try:
                data.decode(enc)
                return True, enc
            except UnicodeDecodeError:
                return False, None
        return False, None
    try:
        data.decode("utf-8")
        return True, "utf-8"
    except UnicodeDecodeError:
        return False, None


class FsError(Exception):
    """Structured filesystem failure (``code`` / ``msg`` / optional ``details``).

    Backends raise this for expected path/permission failures; the fs ops
    layer maps it to ``OpResult`` for agent-facing output.
    """

    def __init__(
        self,
        code: str,
        msg: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.details: dict[str, Any] = dict(details) if details else {}


@dataclass
class ListEntry:
    """One directory listing row."""

    name: str
    kind: str  # f | d | l | o
    size: int = 0
    mode: str | None = None  # e.g. 0644
    mtime: str | None = None
    path: str | None = None  # absolute when known


@dataclass
class StatInfo:
    """Path metadata (kind, size, mode, mtime, optional symlink target)."""

    path: str
    kind: str  # file | dir | link | other
    size: int = 0
    mode: str | None = None
    mtime: str | None = None
    target: str | None = None  # symlink target when available


@dataclass
class ReadResult:
    """Outcome of a bounded file read."""

    path: str
    data: bytes
    truncated: bool = False
    encoding: str | None = "utf-8"
    is_text: bool = True


@dataclass
class WriteResult:
    """Outcome of a write (create or overwrite)."""

    path: str
    bytes_written: int
    created: bool = True


@dataclass
class TransferResult:
    """Outcome of put (local->remote) or get (remote->local)."""

    path: str  # remote path (absolute preferred)
    local: str
    bytes_transferred: int
    direction: str  # put | get


@dataclass
class ListResult:
    """Directory listing with optional truncation flag."""

    path: str
    entries: list[ListEntry] = field(default_factory=list)
    truncated: bool = False


@runtime_checkable
class FsBackend(Protocol):
    """Unified filesystem backend (local | sftp | winrm)."""

    @property
    def via(self) -> str:
        """Backend id for optional ``via=`` meta (``local``|``sftp``|``winrm``)."""
        ...

    def list(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> ListResult: ...

    def stat(self, path: str) -> StatInfo: ...

    def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> ReadResult: ...

    def write(
        self,
        path: str,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
    ) -> WriteResult: ...

    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult: ...

    def get(
        self,
        remote_path: str,
        local_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult: ...

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo: ...

    def rm(self, path: str, *, recursive: bool = False) -> str: ...
