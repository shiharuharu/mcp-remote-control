"""Permanent asyncio event-loop bridge for asyncssh I/O.

asyncssh connection objects are bound to the loop that created them.
Calling ``asyncio.run()`` per awaitable creates (and closes) a fresh loop
each time, which breaks later ops on the same connection.

This module keeps one long-lived loop on a background thread and runs
coroutines there via ``asyncio.run_coroutine_threadsafe``. Sync callers
(Core, CLI, MCP) use :func:`run_coro` / :meth:`AsyncLoopBridge.run` and
must never call from the bridge thread itself (would deadlock).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncLoopBridge:
    """Background-thread asyncio loop for sync callers (Core, CLI, MCP).

    Production path: Core always calls from non-bridge threads. Calling
    :meth:`run` from the bridge loop thread raises ``RuntimeError`` to
    avoid deadlock (``Future.result()`` would block the loop).

    An optional wall-clock *timeout_s* cancels the in-flight future when
    the deadline elapses. SSH layers treat that as a bridge timeout and
    may mark the connection dead, because cancelling mid-flight can leave
    the connection object stale.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.RLock()
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def is_running(self) -> bool:
        return self._started and self._loop is not None and self._loop.is_running()

    def start(self) -> None:
        """Start the background loop (idempotent)."""
        with self._lock:
            if self._started:
                return
            self._ready.clear()
            loop = asyncio.new_event_loop()
            self._loop = loop

            def _runner() -> None:
                asyncio.set_event_loop(loop)
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        self._drain_pending(loop)
                    finally:
                        loop.close()

            thread = threading.Thread(
                target=_runner,
                name="mrc-async-loop-bridge",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            if not self._ready.wait(timeout=10.0):
                raise RuntimeError("AsyncLoopBridge failed to start event loop")
            self._started = True

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the background loop and join the thread."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            if not self._started or loop is None:
                self._reset_state()
                return
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout_s)
            self._reset_state()

    def run(
        self,
        coro: Coroutine[Any, Any, T],
        timeout_s: float | None = None,
    ) -> T:
        """Run *coro* on the bridge loop and return its result.

        Raises
        ------
        RuntimeError
            If called from the bridge loop thread (would deadlock).
        TimeoutError
            If *timeout_s* elapses before the coroutine completes. The
            message includes ``AsyncLoopBridge`` so callers can distinguish
            a bridge wall-clock deadline from library-internal timeouts.
        """
        self.start()
        loop = self._loop
        assert loop is not None

        if threading.current_thread() is self._thread:
            # Sync API cannot await; blocking on Future would freeze the loop.
            coro.close()
            raise RuntimeError(
                "AsyncLoopBridge.run() must not be called from the bridge "
                "loop thread (would deadlock)"
            )

        future: concurrent.futures.Future[T] = asyncio.run_coroutine_threadsafe(
            coro, loop
        )
        try:
            if timeout_s is None:
                return future.result()
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"AsyncLoopBridge.run timed out after {timeout_s}s"
            ) from exc
        except concurrent.futures.CancelledError as exc:
            raise RuntimeError("AsyncLoopBridge coroutine was cancelled") from exc

    def _reset_state(self) -> None:
        self._started = False
        self._loop = None
        self._thread = None
        self._ready.clear()

    @staticmethod
    def _drain_pending(loop: asyncio.AbstractEventLoop) -> None:
        try:
            pending = asyncio.all_tasks(loop)
        except RuntimeError:
            return
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001 — best-effort shutdown
            pass


_shared_lock = threading.Lock()
_shared_bridge: AsyncLoopBridge | None = None


def get_shared_bridge() -> AsyncLoopBridge:
    """Process-wide bridge (lazy start). Prefer for production SSH/SFTP I/O."""
    global _shared_bridge
    with _shared_lock:
        if _shared_bridge is None:
            bridge = AsyncLoopBridge()
            bridge.start()
            _shared_bridge = bridge
        return _shared_bridge


def reset_shared_bridge() -> None:
    """Stop and clear the process-wide bridge (for process teardown / isolation)."""
    global _shared_bridge
    with _shared_lock:
        if _shared_bridge is not None:
            _shared_bridge.stop()
            _shared_bridge = None


def run_coro(
    result: Any,
    timeout_s: float | None = None,
    bridge: AsyncLoopBridge | None = None,
) -> Any:
    """Run *result* if awaitable; otherwise return it unchanged.

    Parameters
    ----------
    result:
        Sync value or awaitable (coroutine / Future).
    timeout_s:
        Optional wall-clock timeout for the bridge wait.
    bridge:
        Explicit bridge; defaults to :func:`get_shared_bridge`.
    """
    if not inspect.isawaitable(result):
        return result
    b = bridge if bridge is not None else get_shared_bridge()
    # Awaitables that are not bare coroutines (e.g. Task) need wrapping.
    if asyncio.iscoroutine(result):
        return b.run(result, timeout_s=timeout_s)
    return b.run(_await_any(result), timeout_s=timeout_s)


async def _await_any(awaitable: Any) -> Any:
    return await awaitable
