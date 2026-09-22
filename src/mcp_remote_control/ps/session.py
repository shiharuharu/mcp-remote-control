"""PsSession: process-local handle for a persistent PowerShell runspace."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.transport.winrm import (
    _CLOSE_LANDED,
    _CLOSE_TIMEOUT,
    _CLOSE_UNCONFIRMED,
)

_log = logging.getLogger(__name__)

# Fallback wall-clock when the transport has no close_runspace / bridge helper
# (defensive; production WinRMTransport budgets close_runspace itself).
_PS_CLOSE_TIMEOUT_S = 5.0


@dataclass
class PsSession:
    """One PowerShell session bound to a winrm endpoint.

    Text invoke only (not a screen PTY). Runspace state is shared across
    sequential invokes until close.
    """

    id: str
    ep: str
    handle: Any
    transport: Any = None  # WinRMTransport (or compatible runspace API)
    location: str | None = None
    created_at: float = field(default_factory=time.time)
    _closed: bool = field(default=False, init=False, repr=False)
    # Verdict of the one budgeted teardown critical section (see ``close``):
    # a ``_CLOSE_*`` token, or None when no teardown ran (no transport/handle).
    # Core reports this value verbatim - nothing re-runs or re-derives a close.
    close_verdict: str | None = field(default=None, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_timed_out(self) -> bool:
        """True when the teardown missed its wall clock (timeout verdict)."""
        return self.close_verdict == _CLOSE_TIMEOUT

    @property
    def cwd(self) -> str | None:
        """Alias: PS location is the session cwd for Agent-track output."""
        return self.location

    @cwd.setter
    def cwd(self, value: str | None) -> None:
        self.location = value

    def abandon(self) -> None:
        """Mark closed and drop handle/transport without remote teardown.

        Used when the transport is already dead: ``handle.close`` /
        ``close_runspace`` can block on a blackholed WSMan session.
        """
        self._closed = True
        handle = self.handle
        self.handle = None
        self.transport = None
        _retire_handle_locally(handle)

    def close(self) -> None:
        """Release the runspace handle in one budgeted critical section.

        Marks the session closed and drops references first so concurrent
        readers never see a half-closed handle. The registry unregisters
        before this runs; subsequent invoke sees a missing session rather
        than a freed handle.

        The teardown is a single critical section: the wait for the transport
        serial lock, the delete and any refusal-recovery the transport runs
        (``close_runspace_within``) share one wall-clock deadline, and the
        transport's verdict is recorded on ``close_verdict`` for the caller to
        report. A miss during the lock wait does not start ``handle.close``
        (the in-flight invoke is not cancelled), and a hung teardown is never a
        clean ``ok``. The handle is retired locally before any of that runs
        (see :func:`_retire_handle_locally`), so a teardown that never starts
        leaves nothing of this session registered with the transport. Never
        raises.
        """
        if self._closed:
            return
        self._closed = True
        transport = self.transport
        handle = self.handle
        self.handle = None
        self.transport = None
        _retire_handle_locally(handle)
        if transport is None or handle is None:
            return
        self._release_handle(transport, handle)

    def _release_handle(self, transport: Any, handle: Any) -> None:
        """Best-effort handle teardown; records the transport's verdict."""
        budget = _ps_close_timeout_s()
        deadline = time.monotonic() + budget
        try:
            with _serial_ops_within(transport, timeout_s=budget):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_close_timeout(budget)
                    return
                self._close_handle_now(
                    transport,
                    handle,
                    timeout_s=remaining,
                    budget=budget,
                )
        except TimeoutError:
            # Serial lock stayed held (typically an unlimited invoke).
            # Session is already unregistered; do not start handle.close.
            self._mark_close_timeout(budget)

    def _close_handle_now(
        self,
        transport: Any,
        handle: Any,
        *,
        timeout_s: float,
        budget: float,
    ) -> None:
        """Run handle teardown; caller already holds the serial lock.

        Prefers the transport's budgeted close, which owns the delete +
        refusal-recovery policy and builds its verdict inside *timeout_s* -
        the deadline the lock wait already drew on. Transports without that
        surface keep the historical best-effort teardown for test doubles,
        which have no delete to report on.
        """
        within = getattr(transport, "close_runspace_within", None)
        if callable(within):
            try:
                verdict = within(handle, timeout_s=timeout_s)
            except Exception:  # noqa: BLE001 - a raising closer is not landed
                verdict = _CLOSE_UNCONFIRMED
            self._record_close_verdict(verdict, budget)
            return

        closer = getattr(transport, "close_runspace", None)
        if callable(closer):
            t0 = time.monotonic()
            try:
                reported = closer(handle)
            except TimeoutError:
                self._record_close_verdict(_CLOSE_TIMEOUT, budget)
                return
            except Exception:  # noqa: BLE001 - a raising closer is not landed
                self._record_close_verdict(_CLOSE_UNCONFIRMED, budget)
                return
            if reported in (_CLOSE_LANDED, _CLOSE_TIMEOUT, _CLOSE_UNCONFIRMED):
                self._record_close_verdict(str(reported), budget)
                return
            # No verdict reported: a closer that ate the whole deadline is a
            # wall-clock miss, not a teardown the caller can trust.
            self._record_close_verdict(
                _CLOSE_TIMEOUT
                if (time.monotonic() - t0) >= timeout_s
                else _CLOSE_LANDED,
                budget,
            )
            return

        raw_close = getattr(handle, "close", None)
        if not callable(raw_close):
            self._record_close_verdict(_CLOSE_LANDED, budget)
            return
        runner = getattr(transport, "_run_blocking_with_timeout", None)
        try:
            if callable(runner):
                runner(raw_close, timeout_s=timeout_s)
            else:
                raw_close()
        except TimeoutError:
            self._record_close_verdict(_CLOSE_TIMEOUT, budget)
            return
        except Exception:  # noqa: BLE001 - best-effort teardown
            # No transport side reports a delete outcome at all, so the
            # historical double contract stands: the release happened locally
            # and there is nothing that could have been left allocated.
            self._record_close_verdict(_CLOSE_LANDED, budget)
            return
        self._record_close_verdict(_CLOSE_LANDED, budget)

    def _record_close_verdict(self, verdict: str, budget: float) -> None:
        """Record the teardown verdict for the caller to report."""
        if verdict == _CLOSE_TIMEOUT:
            self._mark_close_timeout(budget)
            return
        self.close_verdict = verdict

    def _mark_close_timeout(self, budget: float) -> None:
        """Record a wall-clock miss as the teardown verdict."""
        self.close_verdict = _CLOSE_TIMEOUT
        _log.warning(
            "PsSession.close timed out after %ss "
            "(best-effort abandon, id=%s)",
            budget,
            self.id,
        )


def _retire_handle_locally(handle: Any) -> None:
    """Take a retired handle's serial-zone registration back, locally.

    A pool handle registers a drain on the transport's serial-zone registry,
    and that registry holds a strong reference to it - so a session that ends
    without ever reaching ``handle.close()`` (a close whose wait for the serial
    lock missed, an ``abandon`` of a session on a dead transport) would leave
    the handle registered, and with it the adapter's pool and the remote
    runspace's client-side state, for the life of the transport. The
    revocation is local by contract: no exchange, and no wait for the
    transport lock - the caller is spending a budget of its own. A handle that
    still owes a release keeps its registration (a later serial zone is the
    only thing that can land it), so the call is safe from any retirement
    path. Best-effort: a handle without that surface, or one whose retirement
    raises, is left as it was - retirement is never the failure.
    """
    retire = getattr(handle, "detach", None)
    if not callable(retire):
        return
    try:
        retire()
    except Exception:  # noqa: BLE001 - a retirement that raises is not a close
        _log.warning("ps session local retirement failed", exc_info=True)


@contextmanager
def _serial_ops_within(transport: Any, timeout_s: float) -> Iterator[None]:
    """Enter ``transport.serial_ops()`` on this thread within *timeout_s*.

    Lock wait is part of the close budget: a miss raises ``TimeoutError``
    and the caller must not start ``handle.close``. Timed acquire uses the
    public ``op_lock`` because ``serial_ops()`` has no deadline; the
    context then re-enters ``serial_ops()`` (RLock) for the critical
    section, so the whole teardown runs under that one lock. Transports
    without a serial lock proceed immediately.
    """
    serial = getattr(transport, "serial_ops", None)
    lock = getattr(transport, "op_lock", None)
    deadline = max(0.0, float(timeout_s))
    acquired: Any = None
    if lock is not None and hasattr(lock, "acquire"):
        if deadline <= 0.0 or not lock.acquire(timeout=deadline):
            raise TimeoutError("ps close timed out waiting for serial ops")
        acquired = lock
    try:
        if callable(serial):
            with serial():
                yield
        else:
            yield
    finally:
        if acquired is not None:
            acquired.release()


def _ps_close_timeout_s() -> float:
    """Resolve the wall-clock budget for one ``ps close`` teardown.

    Read from the transport module at call time: that constant is the one
    place the runspace open/close budget is configured, and a caller that
    re-binds it must move this deadline with it.
    """
    try:
        from mcp_remote_control.transport import winrm as winrm_mod

        return float(winrm_mod._RUNSPACE_OPEN_CLOSE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - defensive import / attr
        return float(_PS_CLOSE_TIMEOUT_S)
