"""Loop identity: connect / run / sftp / create_process share one asyncio loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from mcp_remote_control.screen.ssh_pty import SshPty
from mcp_remote_control.transport.async_bridge import (
    AsyncLoopBridge,
    reset_shared_bridge,
)
from mcp_remote_control.transport.ssh import SSHTransport, _run_maybe_async


@pytest.fixture(autouse=True)
def _clean_shared_bridge() -> Iterator[None]:
    reset_shared_bridge()
    yield
    reset_shared_bridge()


class LoopBoundConn:
    """Mock connection that records get_running_loop() on async entry points."""

    def __init__(self) -> None:
        self.loops: dict[str, asyncio.AbstractEventLoop] = {}
        self.cwd = "/remote"

    def _record(self, op: str) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        self.loops[op] = loop
        return loop

    async def run(
        self,
        command: str,
        **_kwargs: Any,
    ) -> Any:
        self._record("run")

        class _Result:
            exit_status = 0
            stdout = f"out:{command}"
            stderr = ""

        return _Result()

    async def start_sftp_client(self) -> Any:
        self._record("start_sftp_client")

        class _Sftp:
            pass

        return _Sftp()

    async def create_process(self, *args: Any, **kwargs: Any) -> Any:
        self._record("create_process")

        class _Stdout:
            async def read(self, _n: int = -1) -> bytes:
                return b""

        class _Proc:
            stdout = _Stdout()
            stdin = type("S", (), {"write": lambda self, d: None, "drain": None})()
            exit_status = None
            command = args[0] if args else kwargs.get("command")

            def close(self) -> None:
                pass

            async def wait(self) -> None:
                return None

        return _Proc()


async def _async_connect(**_kwargs: Any) -> LoopBoundConn:
    conn = LoopBoundConn()
    conn.loops["connect"] = asyncio.get_running_loop()
    return conn


def test_loop_identity_across_connect_run_sftp_pty() -> None:
    """All asyncssh-style ops must run on the same permanent loop."""
    bridge = AsyncLoopBridge()
    t: SSHTransport | None = None
    try:
        t = SSHTransport(
            host="h",
            username="u",
            connector=_async_connect,
            bridge=bridge,
        )
        t.connect()
        assert t.is_connected()
        conn = t.connection
        assert isinstance(conn, LoopBoundConn)

        r = t.run_command("uname -a")
        assert r.exit_code == 0
        assert "uname" in r.stdout

        sftp = t.open_sftp()
        assert sftp is not None

        # PTY path uses module-level _run_maybe_async; force same bridge.
        proc = _run_maybe_async(
            conn.create_process("/bin/bash", term_type="xterm"),
            bridge=bridge,
        )
        assert proc is not None

        loops = conn.loops
        assert set(loops) >= {
            "connect",
            "run",
            "start_sftp_client",
            "create_process",
        }
        ids = {id(loops[k]) for k in (
            "connect",
            "run",
            "start_sftp_client",
            "create_process",
        )}
        assert len(ids) == 1, f"loop mismatch: {loops}"
        assert loops["connect"] is bridge.loop
    finally:
        if t is not None:
            t.close()
        bridge.stop()


def test_loop_identity_via_shared_bridge_and_ssh_pty() -> None:
    """Production path: shared bridge + SshPty.create_process."""
    t = SSHTransport(
        host="h",
        username="u",
        connector=_async_connect,
    )
    try:
        t.connect()
        conn = t.connection
        assert isinstance(conn, LoopBoundConn)

        t.run_command("echo hi")
        t.open_sftp()

        # SshPty uses _run_maybe_async → shared bridge (same as transport).
        pty = SshPty.open_shell(conn, cols=80, rows=24)
        assert pty is not None
        assert "create_process" in conn.loops

        loops = conn.loops
        assert len({id(v) for v in loops.values()}) == 1
    finally:
        t.close()


def test_sync_mock_connector_still_works() -> None:
    """Existing mock style: sync connector must not require a running loop."""

    class SyncConn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> Any:
            from mcp_remote_control.transport.base import ExecResult

            return ExecResult(exit_code=0, stdout=command, cwd=cwd or "/")

    def connector(**_kwargs: Any) -> SyncConn:
        return SyncConn()

    t = SSHTransport(host="h", username="u", connector=connector)
    t.connect()
    assert t.run_command("ok").stdout == "ok"
    t.close()
