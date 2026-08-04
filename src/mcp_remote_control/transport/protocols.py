"""Shared Protocol contracts for WinRM sessions and file clients.

Production transport/FS backends call these surfaces only. Mocks and fakes
implement the same contracts; real third-party clients (pypsrp, asyncssh) are
wrapped by thin adapters that normalize library-specific call shapes.

Duck-typing of external libraries belongs inside adapters — not in
``run_command`` / backend op loops.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WinRMOneshotSession(Protocol):
    """Oneshot remote exec with a stable ``environment`` kwarg.

    Implementations always accept ``environment`` (pass-through or no-op).
    Callers never probe signatures: when env is set they pass
    ``environment=env``; wall-clock timeout is enforced outside this surface.
    """

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Run a PowerShell script oneshot; return library-native result."""
        ...

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Run a cmd.exe oneshot; return library-native result."""
        ...


@runtime_checkable
class WinRMHighLevelSession(Protocol):
    """High-level exec returning :class:`~mcp_remote_control.transport.base.ExecResult`-like values."""

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        ...

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        ...


@runtime_checkable
class RunspaceHandle(Protocol):
    """Persistent PowerShell runspace: ``invoke`` + ``close``.

    Production ``open_runspace`` always returns an adapter implementing this
    surface (wrapping mock invoke handles or pypsrp ``RunspacePool``). Optional
    ``stop()`` interrupts an in-flight invoke; optional ``location`` reports cwd.
    """

    def invoke(self, script: str) -> Any:
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class SupportsOpenRunspace(Protocol):
    """Session that can open a persistent runspace handle."""

    def open_runspace(self) -> RunspaceHandle:
        ...


@runtime_checkable
class SupportsOpenFs(Protocol):
    """Session that exposes a ready-made file client."""

    def open_fs(self) -> Any:
        ...


@runtime_checkable
class WinRMFileClient(Protocol):
    """Stable file-client surface used by ``WinrmFs`` (aligned with ``PypsrpFileClient``).

    Optional extras (``list_with_attrs``, ``rmtree``, ``copy``, ``fetch``,
    ``open``) are separate optional Protocols; backends probe those once via
    ``isinstance`` / attribute presence, not multi-name method soup.
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
class SupportsCopyFetch(Protocol):
    def copy(self, local: str, remote: str) -> None:
        ...

    def fetch(self, remote: str, local: str) -> None:
        ...


@runtime_checkable
class SupportsFileOpen(Protocol):
    """Chunked file handle: ``open(path, mode)`` → object with ``read``/``write``/``close``."""

    def open(self, path: str, mode: str = "rb") -> Any:
        ...


@runtime_checkable
class SyncSftpClient(Protocol):
    """Minimal sync SFTP surface used by ``SftpFs``.

    Methods may return awaitables; ``SftpFs`` drives them via the async bridge.
    Optional extras (``readdir``, ``posix_rename``, ``get``, ``read_file``, …)
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
