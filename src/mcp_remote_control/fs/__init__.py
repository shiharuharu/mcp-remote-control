"""Filesystem operations over local, SFTP, and WinRM backends.

Agent-facing API is capability-level (``fs_*``). ``via=`` is optional meta
identifying which backend handled the call, not a separate tool family.
"""

from mcp_remote_control.fs import service
from mcp_remote_control.fs.types import (
    DEFAULT_READ_MAX_BYTES,
    DEFAULT_TRANSFER_CHUNK,
    FsBackend,
    FsError,
    ListEntry,
    ListResult,
    ProgressCallback,
    ReadResult,
    StatInfo,
    TransferResult,
    WriteResult,
    report_progress,
)

__all__ = [
    "DEFAULT_READ_MAX_BYTES",
    "DEFAULT_TRANSFER_CHUNK",
    "FsBackend",
    "FsError",
    "ListEntry",
    "ListResult",
    "ProgressCallback",
    "ReadResult",
    "StatInfo",
    "TransferResult",
    "WriteResult",
    "report_progress",
    "service",
]
