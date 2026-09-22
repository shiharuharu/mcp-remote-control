"""Registry thread-safety + endpoint hygiene tests.

Covers:
- EndpointRegistry concurrent open/close/ensure_connected: no double-live,
  no leaked transports, alive count == registered endpoint count.
- Per-name lock sharding: ``open profile=A`` + ``open profile=B`` with slow
  connectors run concurrently (barrier forces overlap); same-name concurrent
  opens still produce exactly one transport (no double-live).
- ``ensure_connected`` dead-transport pop->reopen: a transport whose
  liveness probe reports dead triggers exactly one reopen (initial + one
  reconnect), no double-live, even under concurrent callers.
- get_registry singleton thread-safe.
- ``_seed_cwd``: ``~`` is local-expanded only for the local transport; ssh /
  winrm profiles keep ``~`` verbatim for the remote shell to resolve.
- ``_safe_close_transport``: a raising ``transport.close()`` is swallowed and
  logged at DEBUG (no exception escapes).
- close_endpoint session teardown vs concurrent reopen; close_if_same
  generation pin; liveness probes must not hold the main RLock across
  transport op_lock; generation_still_open fence.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.config import Profile
from mcp_remote_control.endpoint.registry import (
    Endpoint,
    EndpointRegistry,
    _seed_cwd,
    get_registry,
    reset_registry,
)
from mcp_remote_control.serial.registry import reset_serial_registry
from mcp_remote_control.transport.base import (
    BaseTransport,
    ExecResult,
    TransportError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _MockConn:
    """Mock asyncssh-style connection returned by the injectable connector.

    ``SSHTransport.is_alive`` probes ``is_closing`` / ``is_closed`` attrs and
    treats their absence as "alive", so this mock stays alive until ``close``.
    """

    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DeadOnArrivalConn:
    """Mock conn that ``SSHTransport.is_alive`` reports as dead.

    ``is_closing=True`` makes ``is_alive`` return False on the first probe
    (the SSH liveness check reads ``is_closing`` before ``is_closed``). Used
    to exercise the ``ensure_connected`` pop->reopen branch.
    """

    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False
        # SSHTransport.is_alive checks this first; True -> reports dead.
        self.is_closing = True

    def close(self) -> None:
        self.closed = True


class _BarrierOnCloseConn:
    """Mock conn whose ``close()`` blocks on a shared ``Barrier``.

    Simulates a slow SSH/WinRM teardown (real network IO:
    ``exit``/``close``/``wait_closed``; ``session.close()``) of a
    previously-CONNECTED transport. Used with ``force``-reopen and
    ``ensure_connected`` to prove the stale transport's ``close()`` runs
    OUTSIDE the main RLock - a concurrent open of a different profile name
    reaches its own connector (``_BarrierOnConnectConn``) and the barrier
    releases. If the close ran under the main RLock, the different-name
    open would block on the main RLock, only one party would reach the
    barrier, and the barrier would time out.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        # Stale transport teardown reaches the barrier. Party 1.
        self._barrier.wait()
        self.closed = True


class _DeadBarrierOnCloseConn(_BarrierOnCloseConn):
    """Dead-on-arrival conn (``is_closing=True``) whose ``close()`` hits a barrier.

    Combines ``_DeadOnArrivalConn`` (``ensure_connected`` detects dead via
    ``is_alive`` -> False) with ``_BarrierOnCloseConn`` (the subsequent
    teardown blocks on the barrier). Used to prove ``ensure_connected``'s
    dead-transport ``close()`` runs outside the main RLock.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__(barrier)
        self.is_closing = True  # SSHTransport.is_alive -> False (dead)


class _BarrierOnConnectConn:
    """Mock conn whose construction blocks on a shared ``Barrier``.

    The connector returns ``_BarrierOnConnectConn()``; ``__init__`` waits on
    the barrier, so the barrier is reached during ``transport.connect()`` of a
    different-name open. Party 2 (paired with ``_BarrierOnCloseConn`` /
    ``_DeadBarrierOnCloseConn`` as party 1).
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        # B's connector reaches the barrier. Party 2.
        barrier.wait()
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubTransport(BaseTransport):
    """Minimal transport stub for ``_seed_cwd`` (only ``cwd``/``home`` read)."""

    name = "stub"

    def __init__(self, cwd: str | None = "/var/www", home: str | None = "/home/deploy") -> None:
        super().__init__()
        self.cwd = cwd
        self.home = home

    def connect(self) -> None:
        self._connected = True


# ---------------------------------------------------------------------------
# EndpointRegistry concurrency
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    reset_registry()
    reset_serial_registry()
    yield
    reset_registry()
    reset_serial_registry()


def test_endpoint_open_close_concurrent_no_double_live() -> None:
    """N threads mix open/close/ensure_connected on one name: no double-live."""
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        c = _MockConn()
        with state_lock:
            connect_calls += 1
            conns.append(c)
        return c

    N = 16
    ITERS = 30
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker() -> None:
        local_errs: list[BaseException] = []
        for i in range(ITERS):
            try:
                if i % 3 == 0:
                    reg.open(
                        "lab-ssh", home=FIXTURES, connector=connector, probe=False
                    )
                elif i % 3 == 1:
                    reg.close("lab-ssh")
                else:
                    reg.ensure_connected(
                        "lab-ssh", home=FIXTURES, connector=connector, probe=False
                    )
            except BaseException as exc:  # noqa: BLE001
                local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"

    # At most one endpoint registered for "lab-ssh".
    lab_eps = [e for e in reg.list_open() if e.name == "lab-ssh"]
    assert len(lab_eps) <= 1

    # Invariant: alive transports == registered endpoints (no leak/double-live).
    alive = [c for c in conns if not c.closed]
    closed = [c for c in conns if c.closed]
    if lab_eps:
        assert len(alive) == 1, (
            f"double-live: {len(alive)} alive transports for one endpoint "
            f"(connect_calls={connect_calls})"
        )
    else:
        assert len(alive) == 0, (
            f"leak: {len(alive)} alive transports but no endpoint registered "
            f"(connect_calls={connect_calls})"
        )

    # Bookkeeping: every created conn is either alive or closed; connect count
    # matches the number of conns created.
    assert len(alive) + len(closed) == len(conns) == connect_calls


def test_ensure_connected_concurrent_single_connect() -> None:
    """Concurrent ensure_connected on one name connects exactly once."""
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        c = _MockConn()
        with state_lock:
            connect_calls += 1
            conns.append(c)
        return c

    N = 16
    ITERS = 20
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker() -> None:
        local_errs: list[BaseException] = []
        for _ in range(ITERS):
            try:
                reg.ensure_connected(
                    "lab-ssh", home=FIXTURES, connector=connector, probe=False
                )
            except BaseException as exc:  # noqa: BLE001
                local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    assert reg.get("lab-ssh") is not None
    alive = [c for c in conns if not c.closed]
    assert len(alive) == 1, (
        f"expected exactly 1 alive transport, got {len(alive)} "
        f"(connect_calls={connect_calls})"
    )
    # Idempotent: the first call opened; every later call saw connected+alive
    # and returned the existing endpoint without reconnecting.
    assert connect_calls == 1, f"expected 1 connect, got {connect_calls}"


# ---------------------------------------------------------------------------
# Per-name lock sharding - different profiles connect concurrently
# ---------------------------------------------------------------------------


def test_open_different_profiles_connect_concurrently() -> None:
    """``open profile=A`` + ``open profile=B`` run their connectors in parallel.

    With per-name lock sharding, different names use different per-name locks,
    so ``transport.connect()`` for A and B overlap. A ``Barrier`` inside the
    connector forces BOTH connectors to reach it before either proceeds; if
    opens were serialized (one global lock across connect),
    only one connector would run and the barrier would time out.
    """
    reg = EndpointRegistry()
    # 2 parties, 5s timeout: if opens serialize, the barrier breaks (one party
    # waits alone for 5s) and the opens raise BrokenBarrierError.
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def slow_connector(**_kwargs: object) -> _MockConn:
        # Both connectors MUST reach this barrier before either proceeds.
        # Serialized opens -> only one connector runs -> barrier times out.
        barrier.wait()
        return _MockConn()

    def run_open(name: str) -> None:
        try:
            reg.open(name, home=FIXTURES, connector=slow_connector, probe=False)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    # lab-ssh (ssh transport) + lab-win (winrm transport): both invoke the
    # injected connector during connect(), both hit the shared barrier.
    t_ssh = threading.Thread(target=run_open, args=("lab-ssh",))
    t_win = threading.Thread(target=run_open, args=("lab-win",))
    t_ssh.start()
    t_win.start()
    t_ssh.join()
    t_win.join()

    assert not errors, (
        f"opens raised (barrier timeout \u21d2 opens were serialized, expected "
        f"concurrent): {errors[:3]}"
    )
    assert reg.get("lab-ssh") is not None, "lab-ssh endpoint not registered"
    assert reg.get("lab-win") is not None, "lab-win endpoint not registered"
    # Exactly one transport per name (no double-live, no cross-name confusion).
    open_names = {e.name for e in reg.list_open()}
    assert open_names == {"lab-ssh", "lab-win"}


def test_force_reopen_slow_close_does_not_block_different_name_open() -> None:
    """``force``-reopen of name A with a slow-close stale transport does NOT
    block a concurrent ``open`` of name B (different name).

    The stale transport's ``close()`` (real network IO) runs OUTSIDE the main
    RLock - the entry is POPPED under the main RLock (short critical section),
    the main RLock is released, THEN ``_safe_close_transport`` runs (still
    under A's per-name lock). A ``Barrier`` is shared between A's stale
    ``close()`` (party 1) and B's connector (party 2); if the close ran under
    the main RLock, B's open would block on the main RLock, only one party
    would reach the barrier, and the barrier would time out ->
    ``BrokenBarrierError`` (surfaced as a connect failure in B).
    """
    reg = EndpointRegistry()
    # 2 parties, 5s timeout: if B blocks on A's close (the bug), only A's
    # close reaches the barrier -> 5s timeout -> BrokenBarrierError.
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    state_lock = threading.Lock()
    a_connect_calls = 0

    def a_connector(**_kwargs: object) -> object:
        # 1st call (pre-open): conn whose close() hits the barrier (party 1
        # when force-reopen tears it down). 2nd call (force-reopen's NEW
        # transport): plain mock - its close() is NOT called during the
        # reopen, only the OLD transport's close is.
        nonlocal a_connect_calls
        with state_lock:
            a_connect_calls += 1
            n = a_connect_calls
        if n == 1:
            return _BarrierOnCloseConn(barrier)
        return _MockConn()

    def b_connector(**_kwargs: object) -> _BarrierOnConnectConn:
        # B's connector reaches the barrier (party 2) during connect().
        return _BarrierOnConnectConn(barrier)

    # Pre-open A with a transport whose close() will block on the barrier
    # when force-reopen tears it down.
    reg.open("lab-ssh", home=FIXTURES, connector=a_connector, probe=False)
    assert a_connect_calls == 1

    def force_reopen_a() -> None:
        try:
            reg.open(
                "lab-ssh",
                home=FIXTURES,
                connector=a_connector,
                force=True,
                probe=False,
            )
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def open_b() -> None:
        try:
            reg.open("lab-win", home=FIXTURES, connector=b_connector, probe=False)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_a = threading.Thread(target=force_reopen_a)
    t_b = threading.Thread(target=open_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert not errors, (
        f"opens raised (barrier timeout \u21d2 B blocked on A's slow close, "
        f"expected concurrent): {errors[:3]}"
    )
    assert reg.get("lab-ssh") is not None, "lab-ssh endpoint not re-registered"
    assert reg.get("lab-win") is not None, "lab-win endpoint not registered"
    # A was force-reopened (2 connects: pre-open + reopen).
    assert a_connect_calls == 2, (
        f"expected 2 A connects (pre-open + force-reopen), got {a_connect_calls}"
    )
    # Exactly one endpoint per name (no double-live, no cross-name confusion).
    open_names = {e.name for e in reg.list_open()}
    assert open_names == {"lab-ssh", "lab-win"}


def test_open_same_name_concurrent_exactly_one_transport() -> None:
    """Concurrent ``open profile=A`` (same name) -> exactly one transport.

    Same-name opens serialize on the per-name lock; the second opener
    re-checks under the main RLock and returns the first opener's endpoint.
    No double-live, no leaked transport.
    """
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        c = _MockConn()
        with state_lock:
            connect_calls += 1
            conns.append(c)
        return c

    N = 12
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker() -> None:
        local_errs: list[BaseException] = []
        try:
            reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
        except BaseException as exc:  # noqa: BLE001
            local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    ep = reg.get("lab-ssh")
    assert ep is not None, "lab-ssh endpoint not registered"
    # No double-live: exactly one endpoint registered.
    lab_eps = [e for e in reg.list_open() if e.name == "lab-ssh"]
    assert len(lab_eps) == 1, f"expected 1 endpoint, got {len(lab_eps)}"
    # No leaked transport: every conn the connector created is either the
    # registered (alive) one or has been closed by the loser openers.
    alive = [c for c in conns if not c.closed]
    closed = [c for c in conns if c.closed]
    assert len(alive) == 1, (
        f"expected exactly 1 alive transport, got {len(alive)} "
        f"(connect_calls={connect_calls})"
    )
    assert len(alive) + len(closed) == len(conns) == connect_calls


# ---------------------------------------------------------------------------
# ensure_connected dead-transport pop->reopen
# ---------------------------------------------------------------------------


def test_ensure_connected_dead_transport_reopens_once() -> None:
    """A transport whose liveness probe reports dead triggers exactly one
    reopen (initial connect + one reconnect), then stays alive.

    reg.open refuses DOA registration, so plant dead via live open +
    ``mark_dead`` (realistic peer-drop path), then ensure reopens once.
    """
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        with state_lock:
            connect_calls += 1
            c = _MockConn()
            conns.append(c)
        return c

    # Initial open registers a live transport, then mark_dead -> peer gone.
    ep0 = reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert connect_calls == 1, f"expected 1 connect after open, got {connect_calls}"
    assert ep0.transport is not None
    mark = getattr(ep0.transport, "mark_dead", None)
    assert callable(mark)
    mark("peer_reset")

    # ensure_connected detects the dead transport, pops, and reopens once.
    ep = reg.ensure_connected("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert connect_calls == 2, (
        f"expected 2 connects after ensure_connected (1 initial + 1 reopen), "
        f"got {connect_calls}"
    )
    # The reopened endpoint is registered and connected.
    assert ep is reg.get("lab-ssh")
    assert ep.connected is True

    # A second ensure_connected sees the alive transport and does NOT reopen.
    ep2 = reg.ensure_connected(
        "lab-ssh", home=FIXTURES, connector=connector, probe=False
    )
    assert connect_calls == 2, (
        f"expected no further reconnect, got {connect_calls - 2} extra "
        f"connect(s)"
    )
    assert ep2 is reg.get("lab-ssh")

    # No double-live: exactly one endpoint registered; exactly one alive conn.
    lab_eps = [e for e in reg.list_open() if e.name == "lab-ssh"]
    assert len(lab_eps) == 1, f"expected 1 endpoint, got {len(lab_eps)}"
    alive = [c for c in conns if not c.closed]
    closed = [c for c in conns if c.closed]
    assert len(alive) == 1, (
        f"expected exactly 1 alive transport (no double-live), got {len(alive)}"
    )
    # The initial dead conn was closed by ensure_connected's pop->close; the
    # reopened conn is the single alive one.
    assert len(alive) + len(closed) == len(conns) == connect_calls == 2


def test_ensure_connected_dead_transport_reopens_once_concurrent() -> None:
    """Concurrent ``ensure_connected`` on a dead transport -> exactly one
    reopen (2 connects total), no double-live.

    Plant dead via live open + mark_dead (DOA insert is refused). Then N
    threads call ``ensure_connected`` concurrently. The per-name lock
    serializes them: the first detects dead and reopens (connect #2, alive);
    the rest see the alive transport and return without reconnecting.
    """
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        with state_lock:
            connect_calls += 1
            c = _MockConn()
            conns.append(c)
        return c

    ep0 = reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert connect_calls == 1
    assert ep0.transport is not None
    mark = getattr(ep0.transport, "mark_dead", None)
    assert callable(mark)
    mark("peer_reset")

    N = 12
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker() -> None:
        local_errs: list[BaseException] = []
        try:
            reg.ensure_connected(
                "lab-ssh", home=FIXTURES, connector=connector, probe=False
            )
        except BaseException as exc:  # noqa: BLE001
            local_errs.append(exc)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    # Exactly one reopen across all concurrent callers (1 initial + 1 reopen).
    assert connect_calls == 2, (
        f"expected exactly 2 connects (1 initial + 1 reopen), got "
        f"{connect_calls}"
    )
    # No double-live: exactly one endpoint, exactly one alive transport.
    lab_eps = [e for e in reg.list_open() if e.name == "lab-ssh"]
    assert len(lab_eps) == 1, f"expected 1 endpoint, got {len(lab_eps)}"
    alive = [c for c in conns if not c.closed]
    closed = [c for c in conns if c.closed]
    assert len(alive) == 1, (
        f"expected exactly 1 alive transport (no double-live), got {len(alive)}"
    )
    assert len(alive) + len(closed) == len(conns) == connect_calls == 2


def test_ensure_connected_dead_transport_close_does_not_block_different_name_open() -> None:
    """``ensure_connected`` of a dead transport on name A with a slow close
    does NOT block a concurrent ``open`` of name B (different name).

    ``ensure_connected`` detects the dead transport, POPs it under the main
    RLock (short critical section), releases the main RLock, THEN closes the
    stale transport OUTSIDE the main RLock (still under A's per-name lock),
    then reopens. A ``Barrier`` is shared between A's stale ``close()``
    (party 1) and B's connector (party 2); if the close ran under the main
    RLock, B's open would block on the main RLock, only one party would
    reach the barrier, and the barrier would time out.

    Also re-verifies the ``ensure_connected`` no-double-live invariant under
    the new out-of-lock close: A reopens exactly once (1 initial + 1 reopen)
    and ends with exactly one alive endpoint for A.

    Plant dead via live open + mark_dead (DOA open no longer registers).
    """
    reg = EndpointRegistry()
    # 2 parties, 5s timeout: if B blocks on A's dead-transport close (the
    # bug), only A's close reaches the barrier -> 5s timeout.
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    state_lock = threading.Lock()
    a_connect_calls = 0

    def a_connector(**_kwargs: object) -> object:
        # 1st call (initial open): live conn whose close() hits the barrier
        # (party 1 when ensure_connected tears it down after mark_dead).
        # 2nd call (reopen): plain alive mock - its close() is NOT called
        # during the reopen.
        nonlocal a_connect_calls
        with state_lock:
            a_connect_calls += 1
            n = a_connect_calls
        if n == 1:
            return _BarrierOnCloseConn(barrier)
        return _MockConn()

    def b_connector(**_kwargs: object) -> _BarrierOnConnectConn:
        # B's connector reaches the barrier (party 2) during connect().
        return _BarrierOnConnectConn(barrier)

    # Pre-open A live, then mark_dead. ensure_connected will detect dead
    # (is_connected -> False), pop, close (barrier), and reopen.
    ep_a = reg.open("lab-ssh", home=FIXTURES, connector=a_connector, probe=False)
    assert a_connect_calls == 1
    assert ep_a.transport is not None
    mark = getattr(ep_a.transport, "mark_dead", None)
    assert callable(mark)
    mark("peer_reset")

    def ensure_a() -> None:
        try:
            reg.ensure_connected(
                "lab-ssh", home=FIXTURES, connector=a_connector, probe=False
            )
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def open_b() -> None:
        try:
            reg.open("lab-win", home=FIXTURES, connector=b_connector, probe=False)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_a = threading.Thread(target=ensure_a)
    t_b = threading.Thread(target=open_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert not errors, (
        f"raised (barrier timeout \u21d2 B blocked on A's dead-transport close, "
        f"expected concurrent): {errors[:3]}"
    )
    assert reg.get("lab-ssh") is not None, "lab-ssh endpoint not re-registered"
    assert reg.get("lab-win") is not None, "lab-win endpoint not registered"
    # ensure_connected reopens exactly once (1 initial + 1 reopen = 2 A connects).
    assert a_connect_calls == 2, (
        f"expected 2 A connects (1 initial + 1 reopen), got {a_connect_calls}"
    )
    # No double-live: exactly one endpoint per name; A's endpoint is connected.
    open_names = {e.name for e in reg.list_open()}
    assert open_names == {"lab-ssh", "lab-win"}
    lab_eps = [e for e in reg.list_open() if e.name == "lab-ssh"]
    assert len(lab_eps) == 1, f"expected 1 lab-ssh endpoint, got {len(lab_eps)}"
    assert lab_eps[0].connected is True


def test_open_doa_refuses_registry_insert() -> None:
    """Connect succeeds but is_connected False -> no registry insert.

    Registry-level: DOA conn disposed, TransportError NOT_CONNECTED raised,
    get(name) is None. ensure_connected on empty name also fails without
    leaving a zombie.
    """
    reg = EndpointRegistry()
    closed: list[_DeadOnArrivalConn] = []

    def connector(**_kwargs: object) -> _DeadOnArrivalConn:
        c = _DeadOnArrivalConn()
        closed.append(c)
        return c

    with pytest.raises(TransportError) as ei:
        reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert ei.value.code == "NOT_CONNECTED"
    assert reg.get("lab-ssh") is None
    assert closed and all(c.closed for c in closed)

    with pytest.raises(TransportError) as ei2:
        reg.ensure_connected(
            "lab-ssh", home=FIXTURES, connector=connector, probe=False
        )
    assert ei2.value.code == "NOT_CONNECTED"
    assert reg.get("lab-ssh") is None
    assert len(reg.list_open()) == 0


def test_get_registry_singleton_thread_safe() -> None:
    """Concurrent get_registry() calls all return the same instance."""
    reset_registry()
    instances: list[EndpointRegistry] = []
    inst_lock = threading.Lock()
    N = 16

    def worker() -> None:
        r = get_registry()
        with inst_lock:
            instances.append(r)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == N
    assert all(i is instances[0] for i in instances), (
        "get_registry returned distinct instances under concurrency"
    )


# ---------------------------------------------------------------------------
# _seed_cwd: remote ~ must not be local-expanded
# ---------------------------------------------------------------------------


def test_seed_cwd_remote_tilde_not_local_expanded() -> None:
    local_home = str(Path("~").expanduser())
    stub = _StubTransport()

    ssh_profile = Profile(
        name="lab-ssh", transport="ssh", host="h", username="u",
        defaults={"cwd": "~"},
    )
    win_profile = Profile(
        name="lab-win", transport="winrm", host="h", username="u",
        defaults={"cwd": "~"},
    )
    local_profile = Profile(name="local", transport="local", defaults={"cwd": "~"})

    ssh_cwd = _seed_cwd(ssh_profile, stub)
    win_cwd = _seed_cwd(win_profile, stub)
    local_cwd = _seed_cwd(local_profile, stub)

    # Remote: tilde passed through verbatim (NOT local $HOME).
    assert ssh_cwd == "~"
    assert win_cwd == "~"
    assert ssh_cwd is not None and not ssh_cwd.startswith(local_home)
    assert win_cwd is not None and not win_cwd.startswith(local_home)

    # Local: tilde expanded against local $HOME.
    assert local_cwd == local_home
    assert local_cwd != "~"


def test_seed_cwd_remote_tilde_path_not_local_expanded() -> None:
    """A remote ``~/work`` style cwd is also passed through verbatim."""
    local_home = str(Path("~").expanduser())
    stub = _StubTransport()
    ssh_profile = Profile(
        name="lab-ssh", transport="ssh", host="h", username="u",
        defaults={"cwd": "~/work"},
    )
    cwd = _seed_cwd(ssh_profile, stub)
    assert cwd == "~/work"
    assert cwd is not None and not cwd.startswith(local_home)

    # Local still expands ~/work.
    local_profile = Profile(name="local", transport="local", defaults={"cwd": "~/work"})
    local_cwd = _seed_cwd(local_profile, stub)
    assert local_cwd == str(Path("~/work").expanduser())
    assert local_cwd is not None and local_cwd.startswith(local_home)


# ---------------------------------------------------------------------------
# _safe_close_transport: debug log + no raise
# ---------------------------------------------------------------------------


class _BoomTransport:
    def close(self) -> None:
        raise RuntimeError("close exploded")


def test_safe_close_transport_logs_debug_no_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ep = Endpoint(name="boom", transport_name="local", caps={})
    ep.transport = _BoomTransport()  # type: ignore[assignment]

    with caplog.at_level(
        logging.DEBUG, logger="mcp_remote_control.endpoint.registry"
    ):
        # Must not raise.
        EndpointRegistry._safe_close_transport(ep)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("close failed" in m and "boom" in m for m in msgs), (
        f"expected a debug log mentioning close failure for 'boom', got: {msgs}"
    )


def test_safe_close_transport_no_log_when_transport_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ep = Endpoint(name="empty", transport_name="local", caps={})
    ep.transport = None
    with caplog.at_level(
        logging.DEBUG, logger="mcp_remote_control.endpoint.registry"
    ):
        EndpointRegistry._safe_close_transport(ep)
    assert not caplog.records


# ---------------------------------------------------------------------------
# close_endpoint session teardown vs concurrent same-name reopen
# ---------------------------------------------------------------------------


class _FakePty:
    """Minimal PTY stub for ScreenSession registration tests."""

    def __init__(self) -> None:
        self.cols = 80
        self.rows = 24
        self.cwd: str | None = "/tmp"
        self.closed = False

    def is_alive(self) -> bool:
        return not self.closed

    def exit_code(self) -> int | None:
        return 0 if self.closed else None

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: object = None) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


def _make_screen_session(sid: str, ep: str = "lab-ssh") -> object:
    from mcp_remote_control.screen.session import ScreenSession

    return ScreenSession(
        id=sid,
        ep=ep,
        pty=_FakePty(),
        cols=80,
        rows=24,
        cwd="/tmp",
    )


def _make_ps_session(sid: str, ep: str = "lab-ssh") -> object:
    from mcp_remote_control.ps.session import PsSession

    return PsSession(id=sid, ep=ep, handle=object())


def test_close_ids_only_closes_snapshotted_not_later_same_ep() -> None:
    """close_ids ignores sessions registered after the id snapshot."""
    from mcp_remote_control.ps.registry import PsRegistry
    from mcp_remote_control.screen.registry import ScreenRegistry

    sreg = ScreenRegistry()
    preg = PsRegistry()
    old_scr = _make_screen_session("scr_01", "lab-ssh")
    old_ps = _make_ps_session("ps_01", "lab-ssh")
    sreg.add(old_scr)  # type: ignore[arg-type]
    preg.add(old_ps)  # type: ignore[arg-type]

    scr_ids = sreg.ids_for_endpoint("lab-ssh")
    ps_ids = preg.ids_for_endpoint("lab-ssh")
    assert scr_ids == ["scr_01"]
    assert ps_ids == ["ps_01"]

    # Simulate concurrent reopen: new sessions under the same ep name.
    new_scr = _make_screen_session("scr_02", "lab-ssh")
    new_ps = _make_ps_session("ps_02", "lab-ssh")
    sreg.add(new_scr)  # type: ignore[arg-type]
    preg.add(new_ps)  # type: ignore[arg-type]

    assert sreg.close_ids(scr_ids) == 1
    assert preg.close_ids(ps_ids) == 1
    assert sreg.get("scr_01") is None
    assert preg.get("ps_01") is None
    # New generation sessions survive.
    assert sreg.get("scr_02") is not None
    assert preg.get("ps_02") is not None
    assert getattr(old_scr, "closed") is True
    assert getattr(new_scr, "closed") is False


def test_close_endpoint_concurrent_reopen_preserves_new_sessions() -> None:
    """After close+reopen, new screen/ps sessions must not be killed
    by the old close's session teardown.

    Forces interleaving: Thread A snapshots old session ids and closes the
    endpoint, then blocks inside close_ids; Thread B reopens the same name and
    registers new screen/ps sessions; then A finishes teardown. Only the
    pre-close ids may be closed.
    """
    from mcp_remote_control.core import endpoint_ops
    from mcp_remote_control.ps.registry import (
        get_ps_registry,
        reset_ps_registry,
    )
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    reset_registry()
    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        # Open endpoint + seed old sessions for this ep name.
        r_open = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=FIXTURES,
            connector=connector,
            probe=False,
        )
        assert r_open.status == "ok", r_open.fields

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_scr = _make_screen_session("scr_old", "lab-ssh")
        old_ps = _make_ps_session("ps_old", "lab-ssh")
        sreg.add(old_scr)  # type: ignore[arg-type]
        preg.add(old_ps)  # type: ignore[arg-type]

        # Barrier: party1 = close_ids teardown (after reg.close), party2 = reopen
        # worker that has already registered new sessions.
        barrier = threading.Barrier(2, timeout=5.0)
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        close_result: list[object] = []

        real_close_ids = sreg.close_ids

        def close_ids_with_barrier(session_ids: object) -> int:
            # reg.close already returned; reopen may run before we close ids.
            barrier.wait()
            return real_close_ids(session_ids)  # type: ignore[arg-type]

        sreg.close_ids = close_ids_with_barrier  # type: ignore[method-assign]

        def do_close() -> None:
            try:
                result = endpoint_ops.run(op="close", ep="lab-ssh")
                close_result.append(result)
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)

        def do_reopen_and_seed() -> None:
            try:
                r = endpoint_ops.run(
                    op="open",
                    profile="lab-ssh",
                    home=FIXTURES,
                    connector=connector,
                    probe=False,
                )
                if r.status != "ok":
                    raise AssertionError(f"reopen failed: {r.code} {r.fields}")
                # New generation sessions under the same ep name.
                sreg.add(_make_screen_session("scr_new", "lab-ssh"))  # type: ignore[arg-type]
                preg.add(_make_ps_session("ps_new", "lab-ssh"))  # type: ignore[arg-type]
                barrier.wait()
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass

        t_close = threading.Thread(target=do_close)
        t_reopen = threading.Thread(target=do_reopen_and_seed)
        t_close.start()
        # Give close a head start so it can snapshot + reg.close before reopen
        # races the barrier; small spin until endpoint is gone or timeout.
        for _ in range(200):
            if get_registry().get("lab-ssh") is None:
                break
            threading.Event().wait(0.01)
        t_reopen.start()
        t_close.join(timeout=10.0)
        t_reopen.join(timeout=10.0)

        assert not errors, f"workers raised: {errors[:3]}"
        assert close_result, "close_endpoint did not return"
        closed = close_result[0]
        assert getattr(closed, "status") == "ok", getattr(closed, "fields", None)
        assert getattr(closed, "fields", {}).get("screens_closed") == 1
        # Old sessions torn down.
        assert sreg.get("scr_old") is None
        assert preg.get("ps_old") is None
        assert getattr(old_scr, "closed") is True
        # New sessions from concurrent reopen must survive old teardown.
        assert sreg.get("scr_new") is not None, "reopened screen was killed by old close"
        assert preg.get("ps_new") is not None, "reopened ps was killed by old close"
        assert get_registry().get("lab-ssh") is not None, "reopened endpoint missing"
    finally:
        reset_screen_registry()
        reset_ps_registry()
        reset_registry()


def test_close_endpoint_still_tears_down_sessions_serial() -> None:
    """Teardown semantics preserved: closed ep still clears its screen/ps."""
    from mcp_remote_control.core import endpoint_ops
    from mcp_remote_control.ps.registry import (
        get_ps_registry,
        reset_ps_registry,
    )
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    reset_registry()
    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        r_open = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=FIXTURES,
            connector=connector,
            probe=False,
        )
        assert r_open.status == "ok"

        sreg = get_screen_registry()
        preg = get_ps_registry()
        sreg.add(_make_screen_session("scr_01", "lab-ssh"))  # type: ignore[arg-type]
        preg.add(_make_ps_session("ps_01", "lab-ssh"))  # type: ignore[arg-type]
        # Unrelated ep sessions must not be touched.
        sreg.add(_make_screen_session("scr_other", "other-ep"))  # type: ignore[arg-type]
        preg.add(_make_ps_session("ps_other", "other-ep"))  # type: ignore[arg-type]

        closed = endpoint_ops.run(op="close", ep="lab-ssh")
        assert closed.status == "ok"
        assert closed.fields.get("screens_closed") == 1
        assert closed.fields.get("ps_closed") == 1
        assert sreg.get("scr_01") is None
        assert preg.get("ps_01") is None
        assert sreg.get("scr_other") is not None
        assert preg.get("ps_other") is not None
        assert get_registry().get("lab-ssh") is None
    finally:
        reset_screen_registry()
        reset_ps_registry()
        reset_registry()


def test_close_endpoint_fence_blocks_ensure_mid_snapshot_no_zombie() -> None:
    """Concurrent ensure+seed cannot land between session-id snapshot and
    name-pop (same per-name fence as reg.close).

    Old defect: external snapshot of S_old -> concurrent mark_dead+ensure
    installs E2+S_new -> name-close kills E2 while close_ids only tears S_old
    -> wrong generation killed + zombie sessions. Fence: snapshot runs under
    the name lock with close; ensure serializes; after both complete either
    the reopened generation is fully live (ep + sessions) or fully gone -
    never ep-missing with name-keyed sessions left behind.
    """
    from mcp_remote_control.core import endpoint_ops
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.ps.registry import (
        get_ps_registry,
        reset_ps_registry,
    )
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    real_snap = screen_ops.snapshot_endpoint_session_ids
    reset_registry()
    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        r_open = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=FIXTURES,
            connector=connector,
            probe=False,
        )
        assert r_open.status == "ok", r_open.fields

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_scr = _make_screen_session("scr_fence_old", "lab-ssh")
        old_ps = _make_ps_session("ps_fence_old", "lab-ssh")
        sreg.add(old_scr)  # type: ignore[arg-type]
        preg.add(old_ps)  # type: ignore[arg-type]

        # Barrier inside post-pop snapshot (under name lock): close holds the
        # fence while ensure is started - ensure must not complete install of
        # E2 until the fence releases.
        barrier = threading.Barrier(2, timeout=5.0)
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        close_result: list[object] = []
        ensure_done = threading.Event()

        def snap_with_barrier(ep: str) -> list[str]:
            ids = real_snap(ep)
            # Signal that close is inside the name-lock fence mid-snapshot.
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return ids

        screen_ops.snapshot_endpoint_session_ids = snap_with_barrier  # type: ignore[assignment]

        def do_close() -> None:
            try:
                close_result.append(endpoint_ops.run(op="close", ep="lab-ssh"))
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass

        def do_ensure_and_seed() -> None:
            try:
                # Wait until close is inside the fence (post-pop snapshot).
                barrier.wait()
                r = endpoint_ops.run(
                    op="open",
                    profile="lab-ssh",
                    home=FIXTURES,
                    connector=connector,
                    probe=False,
                )
                if r.status != "ok":
                    raise AssertionError(f"reopen failed: {r.code} {r.fields}")
                sreg.add(
                    _make_screen_session("scr_fence_new", "lab-ssh")
                )  # type: ignore[arg-type]
                preg.add(
                    _make_ps_session("ps_fence_new", "lab-ssh")
                )  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                ensure_done.set()

        t_close = threading.Thread(target=do_close)
        t_ensure = threading.Thread(target=do_ensure_and_seed)
        t_close.start()
        # Wait until close has entered the fence (or fail fast).
        for _ in range(200):
            if barrier.n_waiting >= 1:
                break
            threading.Event().wait(0.01)
        t_ensure.start()
        t_close.join(timeout=10.0)
        t_ensure.join(timeout=10.0)
        assert ensure_done.wait(timeout=1.0)

        assert not errors, f"workers raised: {errors[:3]}"
        assert close_result, "close_endpoint did not return"
        closed = close_result[0]
        assert getattr(closed, "status") == "ok", getattr(closed, "fields", None)
        # Dying generation sessions torn down.
        assert sreg.get("scr_fence_old") is None
        assert preg.get("ps_fence_old") is None
        assert getattr(old_scr, "closed") is True
        # Reopen after fence: new generation fully live (ep + sessions).
        # Never: endpoint absent while name-keyed new sessions remain (zombie).
        assert get_registry().get("lab-ssh") is not None, (
            "reopened endpoint missing after fence"
        )
        assert sreg.get("scr_fence_new") is not None, "new screen killed/orphaned"
        assert preg.get("ps_fence_new") is not None, "new ps killed/orphaned"
    finally:
        screen_ops.snapshot_endpoint_session_ids = real_snap  # type: ignore[assignment]
        reset_screen_registry()
        reset_ps_registry()
        reset_registry()


# ---------------------------------------------------------------------------
# close_if_same generation fence (dead-open generation teardown)
# ---------------------------------------------------------------------------


def test_close_if_same_m4_teardown_and_generation_pin() -> None:
    """close_if_same matching pin retires screen/ps; pin miss leaves E2 alone.

    Dead-open path (open_endpoint not-live) uses close_if_same - must
    snapshot+close_ids for the dying generation only, never kill a
    concurrent newer Endpoint's sessions.
    """
    from mcp_remote_control.ps.registry import (
        get_ps_registry,
        reset_ps_registry,
    )
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    reset_registry()
    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        reg = get_registry()
        e1 = reg.open(
            "lab-ssh", home=FIXTURES, connector=connector, probe=False
        )
        assert e1 is not None and e1.transport is not None

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_scr = _make_screen_session("scr_q10_old", "lab-ssh")
        old_ps = _make_ps_session("ps_q10_old", "lab-ssh")
        sreg.add(old_scr)  # type: ignore[arg-type]
        preg.add(old_ps)  # type: ignore[arg-type]

        # Matching pin: dead-open cleanup for E1 generation.
        e1.transport.mark_dead("q10_dead_open")  # type: ignore[union-attr]
        removed = reg.close_if_same("lab-ssh", e1)
        assert removed is e1
        assert reg.get("lab-ssh") is None
        assert sreg.get("scr_q10_old") is None
        assert preg.get("ps_q10_old") is None
        assert getattr(old_scr, "closed") is True

        # Install E2 + sessions; stale close_if_same(E1) must be a no-op.
        e2 = reg.open(
            "lab-ssh", home=FIXTURES, connector=connector, probe=False
        )
        assert e2 is not e1
        new_scr = _make_screen_session("scr_q10_e2", "lab-ssh")
        new_ps = _make_ps_session("ps_q10_e2", "lab-ssh")
        sreg.add(new_scr)  # type: ignore[arg-type]
        preg.add(new_ps)  # type: ignore[arg-type]

        assert reg.close_if_same("lab-ssh", e1) is None
        assert reg.get("lab-ssh") is e2
        assert e2.transport is not None
        assert e2.transport.is_connected()
        assert sreg.get("scr_q10_e2") is not None
        assert preg.get("ps_q10_e2") is not None
        assert getattr(new_scr, "closed") is False

        # Matching pin still tears down the live generation's sessions.
        removed2 = reg.close_if_same("lab-ssh", e2)
        assert removed2 is e2
        assert reg.get("lab-ssh") is None
        assert sreg.get("scr_q10_e2") is None
        assert preg.get("ps_q10_e2") is None
        assert getattr(new_scr, "closed") is True
    finally:
        reset_screen_registry()
        reset_ps_registry()
        reset_registry()


# ---------------------------------------------------------------------------
# Liveness probes must not hold main RLock across transport op_lock
# (long run_command must not starve different-name Phase-1 open).
# ---------------------------------------------------------------------------


class _OpLockAwareTransport(BaseTransport):
    """Transport whose is_connected -> mark_dead waits on the op_lock.

    Mirrors SSH/WinRM: dead-path ``is_connected`` calls ``mark_dead``, which
    is auto-wrapped with ``_op_lock``. A peer holding ``serial_ops()`` (as a
    long ``run_command`` would) makes that mark_dead block for the hold
    duration. Registry must not hold the main RLock across that wait.
    """

    name = "op-lock-aware"

    def __init__(self) -> None:
        super().__init__()
        self._alive = True
        self.mark_dead_calls = 0

    def connect(self) -> None:
        self._connected = True
        self._alive = True

    def close(self) -> None:
        self._connected = False
        self._alive = False

    def is_alive(self) -> bool:
        return bool(self._connected and self._alive)

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        if not self.is_alive():
            # Same pattern as SSHTransport / WinRMTransport: dead path
            # publishes via mark_dead (serial-wrapped -> waits on op_lock).
            self.mark_dead("peer closed")
            return False
        return True

    def mark_dead(self, reason: str | None = None) -> None:
        self.mark_dead_calls += 1
        self._connected = False
        self._alive = False
        if reason:
            self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del command, cwd, timeout_s, env
        # Body runs under auto-wrapped op_lock.
        time.sleep(0.05)
        if not self._connected:
            raise TransportError("NOT_CONNECTED", "transport marked dead")
        return ExecResult(exit_code=0, stdout="ok\n", cwd="/")


def test_long_op_lock_hold_does_not_starve_different_name_phase1_open() -> None:
    """Long op_lock hold on ep-A must not starve Phase-1 open of B.

    Thread A holds transport A's ``_op_lock`` (simulating long run_command).
    Concurrent list_open / open-A dead-path probes call mark_dead and would
    formerly wait on that op_lock **while holding the registry main RLock**,
    blocking every other name's Phase-1. With the snapshot-then-probe fix,
    open of a different name completes well under the hold duration.
    """
    reg = EndpointRegistry()
    hold_s = 1.2
    t_a = _OpLockAwareTransport()
    t_a.connect()
    ep_a = Endpoint(
        name="lab-ssh",
        transport_name="op-lock-aware",
        caps={"exec": True},
        connected=True,
        transport=t_a,
    )
    with reg._lock:
        reg._endpoints["lab-ssh"] = ep_a

    op_held = threading.Event()
    stop_hold = threading.Event()
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def hold_op_lock() -> None:
        try:
            with t_a.serial_ops():
                op_held.set()
                # Stay until stop or hold_s - whichever is longer enough to
                # prove B does not wait the full RTT.
                stop_hold.wait(timeout=hold_s)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_hold = threading.Thread(target=hold_op_lock, name="hold-op-A")
    t_hold.start()
    assert op_held.wait(timeout=2.0), "holder never acquired op_lock"

    # Make A look dead so list_open / open Phase-1 take the mark_dead path
    # (the path that contends on op_lock).
    t_a._alive = False

    # Background pressure: list_open + open(A) keep hitting A's dead-path
    # mark_dead while the op_lock is held.
    stop_pressure = threading.Event()

    def pressure_a() -> None:
        try:
            while not stop_pressure.is_set():
                reg.list_open()
                try:
                    # Phase-1 may try liveness on A; dead path -> mark_dead.
                    reg.open(
                        "lab-ssh",
                        home=FIXTURES,
                        connector=lambda **_k: _MockConn(),
                        probe=False,
                    )
                except BaseException:  # noqa: BLE001 - reconnect may race
                    pass
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_pressure = threading.Thread(target=pressure_a, name="pressure-A")
    t_pressure.start()
    # Let pressure enter the dead-path / mark_dead wait at least once.
    time.sleep(0.05)

    def b_connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    t0 = time.monotonic()
    try:
        ep_b = reg.open(
            "lab-win", home=FIXTURES, connector=b_connector, probe=False
        )
    except BaseException as exc:  # noqa: BLE001
        with err_lock:
            errors.append(exc)
        ep_b = None
    elapsed = time.monotonic() - t0

    stop_pressure.set()
    stop_hold.set()
    t_pressure.join(timeout=5.0)
    t_hold.join(timeout=hold_s + 2.0)

    assert not errors, f"workers raised: {errors[:3]}"
    assert ep_b is not None, "lab-win open failed under A op_lock pressure"
    assert reg.get("lab-win") is ep_b
    # Must not wait for the full op_lock hold (starvation). Allow generous
    # headroom for CI scheduling but stay well under hold_s.
    assert elapsed < hold_s * 0.5, (
        f"different-name Phase-1 open starved: elapsed={elapsed:.3f}s "
        f"hold_s={hold_s}s (EP-03: main RLock held across mark_dead/op_lock?)"
    )


def test_list_open_probes_outside_main_lock_under_op_lock_hold() -> None:
    """list_open must not hold main RLock while waiting on transport op_lock.

    While A's op_lock is held and A is dead, list_open may block on
    mark_dead for A - but a concurrent main-lock dict op (get / Phase-1
    snapshot for B) must still complete quickly.
    """
    reg = EndpointRegistry()
    hold_s = 1.0
    t_a = _OpLockAwareTransport()
    t_a.connect()
    ep_a = Endpoint(
        name="lab-ssh",
        transport_name="op-lock-aware",
        caps={"exec": True},
        connected=True,
        transport=t_a,
    )
    with reg._lock:
        reg._endpoints["lab-ssh"] = ep_a

    op_held = threading.Event()
    stop_hold = threading.Event()

    def hold_op_lock() -> None:
        with t_a.serial_ops():
            op_held.set()
            stop_hold.wait(timeout=hold_s)

    t_hold = threading.Thread(target=hold_op_lock)
    t_hold.start()
    assert op_held.wait(timeout=2.0)
    t_a._alive = False

    # Kick list_open in background - blocks on mark_dead/op_lock if dead.
    list_started = threading.Event()
    list_done = threading.Event()
    list_errors: list[BaseException] = []

    def do_list() -> None:
        list_started.set()
        try:
            reg.list_open()
        except BaseException as exc:  # noqa: BLE001
            list_errors.append(exc)
        finally:
            list_done.set()

    t_list = threading.Thread(target=do_list)
    t_list.start()
    assert list_started.wait(timeout=2.0)
    # Give list_open time to reach the probe (and, under the bug, main lock).
    time.sleep(0.05)

    # Main-lock critical section for a different name must stay short.
    t0 = time.monotonic()
    with reg._lock:
        _ = reg._endpoints.get("lab-win")
        name_lock = reg._get_or_create_name_lock("lab-win")
    elapsed = time.monotonic() - t0

    stop_hold.set()
    t_list.join(timeout=hold_s + 2.0)
    t_hold.join(timeout=hold_s + 2.0)

    assert not list_errors, f"list_open raised: {list_errors[:3]}"
    assert name_lock is not None
    assert elapsed < 0.25, (
        f"main RLock acquisition starved during list_open probe: "
        f"elapsed={elapsed:.3f}s (EP-03 regression)"
    )
    # After hold released, list should finish and A should look disconnected.
    assert list_done.is_set()
    assert ep_a.connected is False


def test_concurrent_mark_dead_open_list_no_deadlock() -> None:
    """Concurrent mark_dead + open + list_open must not deadlock."""
    reg = EndpointRegistry()
    conns: list[_MockConn] = []
    state_lock = threading.Lock()

    def connector(**_kwargs: object) -> _MockConn:
        c = _MockConn()
        with state_lock:
            conns.append(c)
        return c

    reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    ep = reg.get("lab-ssh")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark)

    stop = threading.Event()
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def marker() -> None:
        try:
            while not stop.is_set():
                mark("q11-stress")
                # Re-arm so open/ensure can treat as live between marks.
                ep.transport._connected = True  # type: ignore[union-attr]
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def opener() -> None:
        try:
            for _ in range(40):
                reg.open(
                    "lab-ssh", home=FIXTURES, connector=connector, probe=False
                )
                reg.open(
                    "lab-win", home=FIXTURES, connector=connector, probe=False
                )
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def lister() -> None:
        try:
            for _ in range(60):
                reg.list_open()
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=marker),
        threading.Thread(target=opener),
        threading.Thread(target=lister),
        threading.Thread(target=opener),
    ]
    for th in threads:
        th.start()
    for th in threads[1:]:
        th.join(timeout=15.0)
        assert not th.is_alive(), "open/list thread hung (possible deadlock)"
    stop.set()
    threads[0].join(timeout=5.0)
    assert not errors, f"mark_dead/open/list race raised: {errors[:3]}"


# ---------------------------------------------------------------------------
# generation_still_open (screen/ps open registration fence)
# ---------------------------------------------------------------------------


def test_generation_still_open_pin() -> None:
    """generation_still_open is identity-pinned and needs a live transport."""
    reg = EndpointRegistry()

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    e1 = reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert e1 is not None and e1.transport is not None
    assert reg.generation_still_open(e1) is True
    assert reg.generation_still_open(None) is False

    # Wrong generation pin after force-replace.
    e2 = reg.open(
        "lab-ssh", home=FIXTURES, connector=connector, probe=False, force=True
    )
    assert e2 is not e1
    assert reg.generation_still_open(e1) is False
    assert reg.generation_still_open(e2) is True

    # Dead transport: identity may still match until pop, but liveness fails.
    e2.transport.mark_dead("r28_dead")  # type: ignore[union-attr]
    assert reg.generation_still_open(e2) is False

    # After close the generation pin is gone.
    reg.close("lab-ssh")
    assert reg.generation_still_open(e2) is False


def test_open_screen_mid_settle_close_endpoint_no_zombie_session() -> None:
    """Concurrent close during screen open settle -> no zombie screen id.

    Thread A open_screen is gated inside GeometryAdapter.adapt (after ensure
    + PTY). Thread B close_endpoint disposes the transport. A must return
    NOT_CONNECTED (or error) and leave the screen registry empty for that ep.
    """
    from mcp_remote_control.core import endpoint_ops, screen_ops
    from mcp_remote_control.screen.geometry import GeometryAdapter
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    reset_registry()
    reset_screen_registry()
    orig_adapt = GeometryAdapter.adapt
    try:
        # Local PTY: fence is generation/liveness, not transport type.
        r_local = endpoint_ops.run(
            op="open",
            profile="local",
            home=FIXTURES,
            probe=False,
        )
        assert r_local.status == "ok", r_local.fields

        adapt_entered = threading.Event()
        release_adapt = threading.Event()
        open_result: list[object] = []
        errors: list[BaseException] = []

        def gated_adapt(
            self: object, session: object, *args: object, **kwargs: object
        ):
            adapt_entered.set()
            assert release_adapt.wait(timeout=10.0), "release_adapt timeout"
            return orig_adapt(self, session, *args, **kwargs)  # type: ignore[misc]

        GeometryAdapter.adapt = gated_adapt  # type: ignore[method-assign, assignment]

        def do_open() -> None:
            try:
                open_result.append(
                    screen_ops.open_screen(
                        ep="local",
                        home=FIXTURES,
                        settle_s=0.05,
                        cols=80,
                        rows=24,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def do_close() -> None:
            try:
                assert adapt_entered.wait(timeout=15.0), "adapt never entered"
                endpoint_ops.run(op="close", ep="local")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                release_adapt.set()

        t_open = threading.Thread(target=do_open, name="r28-scr-open")
        t_close = threading.Thread(target=do_close, name="r28-scr-close")
        t_open.start()
        t_close.start()
        t_open.join(timeout=30.0)
        t_close.join(timeout=30.0)
        assert not t_open.is_alive() and not t_close.is_alive()

        assert not errors, errors
        assert open_result, "open_screen did not return"
        opened = open_result[0]
        sreg = get_screen_registry()
        assert sreg.ids_for_endpoint("local") == [], (
            f"zombie screen sessions after concurrent close: "
            f"{sreg.ids_for_endpoint('local')}"
        )
        status = getattr(opened, "status", None)
        code = getattr(opened, "code", None)
        if status == "ok":
            # Open won the race before close - close must tear down the id.
            sid = getattr(opened, "fields", {}).get("id")
            assert sid is None or sreg.get(str(sid)) is None
        else:
            assert status == "error"
            assert code in (
                "NOT_CONNECTED",
                "EXEC_FAILED",
                "CONNECT_FAILED",
            ), (code, getattr(opened, "fields", None))
        assert get_registry().get("local") is None
    finally:
        GeometryAdapter.adapt = orig_adapt  # type: ignore[method-assign]
        reset_screen_registry()
        reset_registry()


def test_open_ps_mid_runspace_close_endpoint_no_zombie_session() -> None:
    """Concurrent close during open_runspace -> no zombie ps session."""
    from mcp_remote_control.core import endpoint_ops, ps_ops
    from mcp_remote_control.ps.registry import (
        get_ps_registry,
        reset_ps_registry,
    )
    from mcp_remote_control.ps.mock import MockWinRMSessionWithRunspace

    reset_registry()
    reset_ps_registry()
    try:
        runspace_entered = threading.Event()
        release_runspace = threading.Event()
        open_calls = {"n": 0}

        class _GatedSession(MockWinRMSessionWithRunspace):
            def open_runspace(self) -> object:  # type: ignore[override]
                open_calls["n"] += 1
                runspace_entered.set()
                assert release_runspace.wait(timeout=10.0), "release timeout"
                return super().open_runspace()

        def connector(**_kwargs: object) -> _GatedSession:
            return _GatedSession(cwd=r"C:\Users\mock")

        r_ep = endpoint_ops.run(
            op="open",
            profile="lab-win",
            home=FIXTURES,
            connector=connector,
            probe=False,
        )
        assert r_ep.status == "ok", r_ep.fields

        open_result: list[object] = []
        errors: list[BaseException] = []

        def do_open() -> None:
            try:
                open_result.append(
                    ps_ops.open_session(
                        ep="lab-win",
                        home=FIXTURES,
                        connector=connector,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def do_close() -> None:
            try:
                assert runspace_entered.wait(timeout=15.0), "runspace never entered"
                endpoint_ops.run(op="close", ep="lab-win")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                release_runspace.set()

        t_open = threading.Thread(target=do_open, name="r28-ps-open")
        t_close = threading.Thread(target=do_close, name="r28-ps-close")
        t_open.start()
        t_close.start()
        t_open.join(timeout=30.0)
        t_close.join(timeout=30.0)
        assert not t_open.is_alive() and not t_close.is_alive()
        assert not errors, errors
        assert open_result, "open_session did not return"
        opened = open_result[0]
        preg = get_ps_registry()
        zombie_ids = preg.ids_for_endpoint("lab-win")
        assert zombie_ids == [], f"zombie ps sessions: {zombie_ids}"
        status = getattr(opened, "status", None)
        code = getattr(opened, "code", None)
        if status == "ok":
            sid = getattr(opened, "fields", {}).get("id")
            assert sid is None or preg.get(str(sid)) is None
        else:
            assert status == "error"
            assert code in (
                "NOT_CONNECTED",
                "EXEC_FAILED",
                "CONNECT_FAILED",
            ), (code, getattr(opened, "fields", None))
        assert get_registry().get("lab-win") is None
        assert open_calls["n"] >= 1
    finally:
        reset_ps_registry()
        reset_registry()
