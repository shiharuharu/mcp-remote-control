"""Transport base class and shared result/error types.

:class:`BaseTransport` is the concrete base every backend subclasses. It
owns the shared ``_connected`` flag plus optional probe seeds
(``cwd`` / ``home`` / ``meta``) and raises ``UNSUPPORTED`` for exec
methods not overridden.

Callers type against ``BaseTransport`` (or a concrete subclass).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class TransportError(Exception):
    """Structured transport failure mapped to OpResult by Core/endpoint."""

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
class ExecResult:
    """Result of a non-interactive remote/local process run.

    Security note: ``run_command`` is intentionally shell-interpreted on local
    (and typically remote shells). This is a remote-admin tool; callers must
    not pass untrusted command strings without review.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    cwd: str | None = None
    timed_out: bool = False


class BaseTransport:
    """Shared state and default stubs for concrete transport backends.

    Subclasses implement ``connect`` / ``close`` / ``run_*``. The base
    keeps a connected flag and optional probe-seeded fields that Endpoint
    and Core read after open. Unimplemented exec methods raise
    ``TransportError(UNSUPPORTED)`` so missing backends fail loudly.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._connected: bool = False
        # Optional probe / env seeds filled on connect.
        self.cwd: str | None = None
        self.home: str | None = None
        self.meta: dict[str, Any] = {}

    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:
        self._connected = False

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        raise TransportError(
            "UNSUPPORTED",
            f"{self.name} run_command not implemented",
            details={"transport": self.name},
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        raise TransportError(
            "UNSUPPORTED",
            f"{self.name} run_argv not implemented",
            details={"transport": self.name},
        )
