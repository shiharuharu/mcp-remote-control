"""Shared Protocol contracts for WinRM / SFTP file clients.

FS backends probe these surfaces via ``isinstance`` (optional extras) or
type annotations. Session/runspace shapes live as adapters in
``transport.winrm`` - not as unused Protocols here.

Duck-typing of external libraries belongs inside adapters - not in
backend op loops.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WinRMFileClient(Protocol):
    """Stable file-client surface used by ``WinrmFs`` (aligned with ``PypsrpFileClient``).

    Optional extras (``list_with_attrs``, ``rmtree``, ``open``) are separate
    optional Protocols; backends probe those once via ``isinstance`` /
    attribute presence, not multi-name method soup. A native ``copy`` /
    ``fetch`` transfer is gated by the client's own ``has_native_copy`` /
    ``has_native_fetch`` flags, not by a Protocol: the same methods also exist
    as scripted fallbacks, which are not native.
    """

    def stat(self, path: str) -> Any:
        ...

    def listdir(self, path: str) -> list[str]:
        ...

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        ...

    def write_file(self, path: str, data: bytes) -> None:
        ...

    def mkdir(self, path: str) -> None:
        ...

    def remove(self, path: str) -> None:
        ...

    def rmdir(self, path: str) -> None:
        ...


@runtime_checkable
class SupportsListWithAttrs(Protocol):
    def list_with_attrs(self, path: str) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class SupportsRmtree(Protocol):
    def rmtree(self, path: str) -> None:
        ...


@runtime_checkable
class SupportsFileOpen(Protocol):
    """Chunked file handle: ``open(path, mode)`` -> object with ``read``/``write``/``close``."""

    def open(self, path: str, mode: str = "rb") -> Any:
        ...


@runtime_checkable
class SyncSftpClient(Protocol):
    """Minimal sync SFTP surface used by ``SftpFs``.

    Methods may return awaitables; ``SftpFs`` drives them via the async bridge.
    Optional extras (``readdir``, ``posix_rename``, ``get``, ``read_file``, ...)
    use the same optional-Protocol pattern as the WinRM file client.
    """

    def stat(self, path: str) -> Any:
        ...

    def listdir(self, path: str) -> Any:
        ...

    def open(self, path: str, mode: str = "rb") -> Any:
        ...

    def mkdir(self, path: str) -> Any:
        ...

    def remove(self, path: str) -> Any:
        ...

    def rmdir(self, path: str) -> Any:
        ...
