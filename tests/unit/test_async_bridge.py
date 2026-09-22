"""Unit tests for AsyncLoopBridge (permanent loop for asyncssh I/O)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from mcp_remote_control.transport.async_bridge import (
    AsyncLoopBridge,
    get_shared_bridge,
    reset_shared_bridge,
    run_coro,
)


@pytest.fixture(autouse=True)
def _clean_shared_bridge() -> Iterator[None]:
    reset_shared_bridge()
    yield
    reset_shared_bridge()


def test_run_returns_value() -> None:
    bridge = AsyncLoopBridge()
    try:

        async def _add(a: int, b: int) -> int:
            await asyncio.sleep(0)
            return a + b

        assert bridge.run(_add(2, 3)) == 5
    finally:
        bridge.stop()


def test_run_propagates_error() -> None:
    bridge = AsyncLoopBridge()
    try:

        async def _boom() -> None:
            raise ValueError("expected-fail")

        with pytest.raises(ValueError, match="expected-fail"):
            bridge.run(_boom())
    finally:
        bridge.stop()


def test_run_timeout() -> None:
    bridge = AsyncLoopBridge()
    try:

        async def _slow() -> str:
            await asyncio.sleep(2.0)
            return "done"

        with pytest.raises(TimeoutError) as ei:
            bridge.run(_slow(), timeout_s=0.05)
        assert "AsyncLoopBridge" in str(ei.value)
        assert "0.05" in str(ei.value)
    finally:
        bridge.stop()


def test_run_none_timeout_waits_until_complete() -> None:
    """timeout_s=None is unbounded: short coroutines complete (no false timeout)."""
    bridge = AsyncLoopBridge()
    try:

        async def _brief() -> str:
            await asyncio.sleep(0.05)
            return "ok"

        # Explicit None must not impose a deadline (non-fs callers rely on this).
        assert bridge.run(_brief(), timeout_s=None) == "ok"
    finally:
        bridge.stop()


def test_run_coro_timeout_propagates() -> None:
    bridge = AsyncLoopBridge()
    try:

        async def _slow() -> str:
            await asyncio.sleep(2.0)
            return "done"

        with pytest.raises(TimeoutError, match="AsyncLoopBridge"):
            run_coro(_slow(), timeout_s=0.05, bridge=bridge)
    finally:
        bridge.stop()


def test_start_is_idempotent() -> None:
    bridge = AsyncLoopBridge()
    try:
        bridge.start()
        loop1 = bridge.loop
        bridge.start()
        assert bridge.loop is loop1
        assert bridge.is_running
    finally:
        bridge.stop()


def test_same_loop_across_multiple_runs() -> None:
    bridge = AsyncLoopBridge()
    try:
        loops: list[asyncio.AbstractEventLoop] = []

        async def _capture() -> int:
            loops.append(asyncio.get_running_loop())
            return id(asyncio.get_running_loop())

        a = bridge.run(_capture())
        b = bridge.run(_capture())
        assert a == b
        assert len(loops) == 2
        assert loops[0] is loops[1]
        assert loops[0] is bridge.loop
    finally:
        bridge.stop()


def test_run_from_bridge_thread_raises() -> None:
    bridge = AsyncLoopBridge()
    try:
        bridge.start()
        err: list[BaseException] = []
        done = threading.Event()

        def _on_loop_thread() -> None:
            try:

                async def _noop() -> None:
                    return None

                bridge.run(_noop())
            except BaseException as exc:  # noqa: BLE001
                err.append(exc)
            finally:
                done.set()

        assert bridge.loop is not None
        bridge.loop.call_soon_threadsafe(_on_loop_thread)
        assert done.wait(timeout=2.0), "bridge thread callback did not run"
        assert err
        assert isinstance(err[0], RuntimeError)
        assert "bridge" in str(err[0]).lower() or "deadlock" in str(err[0]).lower()
    finally:
        bridge.stop()


def test_run_coro_passthrough_non_awaitable() -> None:
    assert run_coro(42) == 42
    assert run_coro("sync") == "sync"
    assert run_coro(None) is None


def test_run_coro_awaitable_uses_bridge() -> None:
    bridge = AsyncLoopBridge()
    try:

        async def _val() -> str:
            return "ok"

        assert run_coro(_val(), bridge=bridge) == "ok"
    finally:
        bridge.stop()


def test_shared_bridge_singleton() -> None:
    a = get_shared_bridge()
    b = get_shared_bridge()
    assert a is b
    assert a.is_running

    async def _id() -> int:
        return id(asyncio.get_running_loop())

    assert run_coro(_id()) == run_coro(_id())
    reset_shared_bridge()
    c = get_shared_bridge()
    assert c is not a
