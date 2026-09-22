"""Transport base class and shared result/error types.

:class:`BaseTransport` is the concrete base every backend subclasses. It
owns the shared ``_connected`` flag plus optional probe seeds
(``cwd`` / ``home`` / ``meta``) and raises ``UNSUPPORTED`` for exec
methods not overridden.

Callers type against ``BaseTransport`` (or a concrete subclass).

Per-endpoint serial ops
-----------------------
Each transport instance owns ``_op_lock`` (an :class:`threading.RLock`).
Subclass methods that touch connection liveness or shared session state
- ``run_command`` / ``run_argv`` / ``open_sftp`` / ``open_fs`` /
``open_runspace`` / ``runspace_invoke`` / ``collect_probe`` /
``mark_dead`` / ``invalidate_sftp`` / ``connect`` / ``close`` - are
wrapped at class definition time so concurrent FastMCP thread-pool calls
on the **same** endpoint serialize. Different endpoints keep independent
locks and proceed in parallel.

Registry per-name RLocks (open/close/ensure_connected) are a separate
layer; they do **not** cover mid-session exec/sftp/fs/runspace/probe.
Prefer ``with transport.serial_ops():`` for ad-hoc critical sections. The
lock is re-entrant so ``run_command`` -> ``mark_dead`` (failure path),
``collect_probe`` -> ``mark_dead`` / nested ``run_command``, and
``ensure_connected`` -> ``open`` -> ``connect`` never self-deadlock.

The zone is also where the transport schedules work that outlives the
operation which created it: hooks registered on
:attr:`BaseTransport.serial_zone_hooks` run when the zone is entered and
again as it ends, so a responsibility owed by an earlier operation (a
remote release that could not be sent while another operation held the
lock) is picked up by the next zone this transport enters, whichever
handle or surface enters it. The end of the zone is what covers a
responsibility whose hand-off has its own bounded wait for the lock: an
operation that outlasts that wait holds the lock past it, and the end of
its zone is the first moment the handed-off work can take the lock.
"""

from __future__ import annotations

import functools
import logging
import math
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar, cast

_log = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# Methods that mutate or depend on connection liveness / shared session
# state. Wrapped with ``_op_lock`` on every concrete subclass that defines
# them (see ``BaseTransport.__init_subclass__``).
# open_fs, open_runspace, runspace_invoke, and collect_probe must
# serialize with mark_dead/connect/close so FastMCP threads cannot
# interleave dispose with session use (half-open pool / disposed session).
_SERIAL_OP_METHODS = frozenset(
    {
        "run_command",
        "run_argv",
        "open_sftp",
        "open_fs",
        "open_runspace",
        "runspace_invoke",
        "collect_probe",
        "mark_dead",
        "invalidate_sftp",
        "connect",
        "close",
    }
)


def _wrap_with_op_lock(fn: _F) -> _F:
    """Serialize *fn* on ``self._op_lock`` (re-entrant).

    The hooks run at both ends of the zone: entering hands off work owed by an
    earlier operation, and ending re-runs the same hand-off because the work it
    started may have been waiting, bounded, for this operation to release the
    lock. Both runs are inside the lock, so a hook never waits for it.
    """

    @functools.wraps(fn)
    def wrapper(self: BaseTransport, *args: Any, **kwargs: Any) -> Any:
        with self._op_lock:
            self._serial_zone_hooks.run()
            try:
                return fn(self, *args, **kwargs)
            finally:
                # The zone is ending and its lock is about to be free, which
                # is the first moment a hand-off that gave up waiting for it
                # can run: retry it here rather than deferring it to whatever
                # zone comes next. A hook only hands work off, so this adds no
                # wait of its own to the operation's return.
                self._serial_zone_hooks.run()

    # type: ignore[attr-defined] - marker so we never double-wrap
    wrapper._mrc_op_serial = True  # type: ignore[attr-defined]
    return cast(_F, wrapper)


class SerialZoneHooks:
    """Callables a transport runs at both ends of a serial zone.

    Some work a transport owes outlives the operation that created it: a
    remote release that could not be sent because the serial lock was not free
    is nobody's to retry once that operation returns, and the handle it belongs
    to may never be used again. The transport, not the caller, owns the
    schedule - every serial op and :meth:`BaseTransport.serial_ops` enter the
    zone - so the zone is where such a responsibility is handed to a retry.
    Registration is explicit (see :meth:`add` / :meth:`discard`) so a handle
    that no longer owes anything stops being asked.

    Hooks run at both ends of a zone, always inside its lock. The entry starts
    the hand-off while the entering operation still holds the lock; the end
    repeats it, because that hand-off may only wait for the lock a bounded
    while - an operation whose own exchange outlasts that bound holds the lock
    past it, and the zone ending is the first moment the work can take it.
    Without the end run such a responsibility would stay owed for as long as
    the transport keeps serving operations that outlive the bound.

    A hook runs inside the zone it was entered by, so it must hand its work off
    rather than wait for it there (a wait for the release would be a wait on
    the lock the caller already holds), and it must not be able to break the
    operation that entered the zone: :meth:`run` isolates every call, so a
    raising hook is reported and the zone continues.
    """

    def __init__(self) -> None:
        self._hooks: list[Callable[[], None]] = []

    def add(self, hook: Callable[[], None]) -> None:
        """Register *hook*; a hook already registered is not added twice."""
        if hook not in self._hooks:
            self._hooks.append(hook)

    def discard(self, hook: Callable[[], None]) -> None:
        """Drop *hook*; a hook that was not registered is ignored."""
        try:
            self._hooks.remove(hook)
        except ValueError:
            pass

    def run(self) -> None:
        """Run every registered hook, isolated from the zone that entered it.

        Called at both ends of a zone, so the registry is snapshotted first: a
        hook that adds or discards a hook cannot change what this pass runs.
        No hook's failure reaches the serial op that entered the zone - it is
        reported and the zone runs on - and no hook may wait here for the lock
        the zone holds. An empty registry makes this a no-op, so a transport
        with nothing pending pays no more than the call.
        """
        for hook in tuple(self._hooks):
            try:
                hook()
            except Exception:  # noqa: BLE001 - a hook never breaks its zone
                _log.warning("serial zone hook failed", exc_info=True)


def _install_serial_wrappers(cls: type) -> None:
    """Wrap serial-op methods defined directly on *cls* (not inherited)."""
    for name in _SERIAL_OP_METHODS:
        fn = cls.__dict__.get(name)
        if fn is None or not callable(fn):
            continue
        if getattr(fn, "_mrc_op_serial", False):
            continue
        # staticmethod / classmethod: leave alone (none of the serial ops
        # are static today; guard against future misuse).
        if isinstance(fn, (staticmethod, classmethod)):
            continue
        setattr(cls, name, _wrap_with_op_lock(fn))


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


def normalize_timeout_s(timeout: float | None) -> float | None:
    """Map a surface timeout to a transport ``timeout_s``.

    ``None`` means unlimited. Positive finite values are passed through.
    Non-positive (including 0), unparseable, and non-finite (NaN / +/-Inf,
    including string forms that float to them) raise
    ``TransportError(INVALID_ARG)`` so callers never silently lose their
    deadline budget (and NaN never reaches ``communicate`` / invoke stop).
    """
    if timeout is None:
        return None
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        raise TransportError(
            "INVALID_ARG",
            f"timeout must be a finite number, got {timeout!r}",
        ) from None
    if not math.isfinite(t):
        raise TransportError(
            "INVALID_ARG",
            f"timeout must be a finite number, got {timeout!r}",
        )
    if t <= 0:
        raise TransportError(
            "INVALID_ARG",
            f"timeout must be > 0, got {timeout!r}",
        )
    return t


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

    Each instance owns ``_op_lock`` (RLock). Serial ops listed in
    ``_SERIAL_OP_METHODS`` are auto-wrapped on subclasses so concurrent
    exec / sftp / open_fs / runspace / collect_probe / mark_dead on one
    endpoint cannot interleave. Both ends of that zone also run
    :attr:`serial_zone_hooks`, which is how a responsibility an earlier
    operation left owed is picked up by a later operation on any handle of
    this transport.
    """

    name: str = "base"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _install_serial_wrappers(cls)

    def __init__(self) -> None:
        self._connected: bool = False
        # Optional probe / env seeds filled on connect.
        self.cwd: str | None = None
        self.home: str | None = None
        self.meta: dict[str, Any] = {}
        # Per-instance serial lock for exec/sftp/fs/runspace/probe/
        # mark_dead/connect/close. RLock: failure paths (run_command ->
        # mark_dead, collect_probe -> mark_dead) and registry
        # ensure_connected -> open -> connect re-enter safely.
        self._op_lock = threading.RLock()
        # Responsibilities that outlive the operation which created them are
        # handed a retry at both ends of every serial zone on this transport.
        self._serial_zone_hooks = SerialZoneHooks()

    @property
    def op_lock(self) -> threading.RLock:
        """Per-transport lock serializing liveness-sensitive operations."""
        return self._op_lock

    @property
    def serial_zone_hooks(self) -> SerialZoneHooks:
        """Registry of hooks run at both ends of this transport's serial zone.

        See :class:`SerialZoneHooks`. Supplied to collaborators that hold a
        responsibility across operations, so their retry is scheduled by the
        transport's zones rather than by their own next call.
        """
        return self._serial_zone_hooks

    @contextmanager
    def serial_ops(self) -> Iterator[None]:
        """Hold ``_op_lock`` for a multi-step critical section.

        Prefer this over touching ``_op_lock`` directly. Nested use
        (including from already-wrapped serial methods) is safe. Both ends of
        the zone run the registered :attr:`serial_zone_hooks`.
        """
        with self._op_lock:
            self._serial_zone_hooks.run()
            try:
                yield
            finally:
                self._serial_zone_hooks.run()

    @contextmanager
    def serial_ops_within(self, timeout_s: float) -> Iterator[None]:
        """Enter :meth:`serial_ops` on this thread within *timeout_s*.

        The wait for the lock is part of the caller's own budget: the same
        lock is held across a whole ``run_command`` / runspace invoke / fs
        call, so a caller with a deadline must not spend all of it queued
        behind one. A miss raises ``TimeoutError`` with nothing entered, so
        the caller reports its own deadline instead of starting work it can
        no longer bound. The body then re-enters :meth:`serial_ops` (RLock),
        so the whole critical section runs under that one lock. A thread
        that already holds the lock re-enters immediately and never waits.
        """
        deadline = max(0.0, float(timeout_s))
        if deadline <= 0.0 or not self._op_lock.acquire(timeout=deadline):
            raise TimeoutError(f"timed out waiting {deadline}s for serial ops")
        try:
            with self.serial_ops():
                yield
        finally:
            self._op_lock.release()

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


# Wrap serial methods defined on the base itself (close / run_* stubs).
# Subclasses get their own wraps via ``__init_subclass__``.
_install_serial_wrappers(BaseTransport)
