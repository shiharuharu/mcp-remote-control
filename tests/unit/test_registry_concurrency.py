"""Registry thread-safety + endpoint hygiene tests (O4, C2).

Covers:
- EndpointRegistry concurrent open/close/ensure_connected: no double-live,
  no leaked transports, alive count == registered endpoint count.
- C2 per-name lock sharding: ``open profile=A`` + ``open profile=B`` with slow
  connectors run concurrently (barrier forces overlap); same-name concurrent
  opens still produce exactly one transport (no double-live).
- C2 ``ensure_connected`` dead-transport pop→reopen: a transport whose
  liveness probe reports dead triggers exactly one reopen (initial + one
  reconnect), no double-live, even under concurrent callers.
- get_registry singleton thread-safe.
- ``_seed_cwd``: ``~`` is local-expanded only for the local transport; ssh /
  winrm profiles keep ``~`` verbatim for the remote shell to resolve.
- ``_safe_close_transport``: a raising ``transport.close()`` is swallowed and
  logged at DEBUG (no exception escapes).
- SerialRegistry hygiene: ``clear()`` resets the id counter, ``list_open``
  sorts ``con_NN`` ids numerically, and ``remove`` exposes per-step close
  errors via ``SerialSession.close_errors``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from mcp_remote_control.config import Profile
from mcp_remote_control.endpoint.registry import (
    Endpoint,
    EndpointRegistry,
    _seed_cwd,
    get_registry,
    reset_registry,
)
from mcp_remote_control.serial.buffer import LineRingBuffer
from mcp_remote_control.serial.capture import CapturePump
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.registry import (
    SerialRegistry,
    SerialSession,
    reset_serial_registry,
)
from mcp_remote_control.transport.base import BaseTransport

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
    to exercise the ``ensure_connected`` pop→reopen branch.
    """

    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False
        # SSHTransport.is_alive checks this first; True → reports dead.
        self.is_closing = True

    def close(self) -> None:
        self.closed = True


class _BarrierOnCloseConn:
    """Mock conn whose ``close()`` blocks on a shared ``Barrier``.

    Simulates a slow SSH/WinRM teardown (real network IO:
    ``exit``/``close``/``wait_closed``; ``session.close()``) of a
    previously-CONNECTED transport. Used with ``force``-reopen and
    ``ensure_connected`` to prove the stale transport's ``close()`` runs
    OUTSIDE the main RLock — a concurrent open of a different profile name
    reaches its own connector (``_BarrierOnConnectConn``) and the barrier
    releases. If the close ran under the main RLock (the MED-re-review bug),
    the different-name open would block on the main RLock, only one party
    would reach the barrier, and the barrier would time out.
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
    ``is_alive`` → False) with ``_BarrierOnCloseConn`` (the subsequent
    teardown blocks on the barrier). Used to prove ``ensure_connected``'s
    dead-transport ``close()`` runs outside the main RLock.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__(barrier)
        self.is_closing = True  # SSHTransport.is_alive → False (dead)


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


class _DummyPump:
    """Pre-set pump so ``SerialSession.start_capture`` is a no-op (no thread)."""

    def stop(self, *, timeout_s: float = 1.0) -> None:
        pass


class _DummyConsole:
    """Minimal serial console stub (no real pyserial)."""

    def __init__(self, path: str = "/dev/ttyUSB0") -> None:
        self.port = path
        self.closed = False

    def is_alive(self) -> bool:
        return False

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


def _make_serial_session(sid: str, *, console: object | None = None) -> SerialSession:
    return SerialSession(
        id=sid,
        console=cast(
            SerialConsole,
            console if console is not None else _DummyConsole(),
        ),
        path=f"/dev/ttyUSB{sid}",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),  # non-None ⇒ start_capture is a no-op
    )


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
# C2: per-name lock sharding — different profiles connect concurrently
# ---------------------------------------------------------------------------


def test_open_different_profiles_connect_concurrently() -> None:
    """``open profile=A`` + ``open profile=B`` run their connectors in parallel.

    With per-name lock sharding, different names use different per-name locks,
    so ``transport.connect()`` for A and B overlap. A ``Barrier`` inside the
    connector forces BOTH connectors to reach it before either proceeds; if
    opens were serialized (one global lock across connect, the O4 behavior),
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
        # Serialized opens → only one connector runs → barrier times out.
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
        f"opens raised (barrier timeout ⇒ opens were serialized, expected "
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
    RLock — the entry is POPPED under the main RLock (short critical section),
    the main RLock is released, THEN ``_safe_close_transport`` runs (still
    under A's per-name lock). A ``Barrier`` is shared between A's stale
    ``close()`` (party 1) and B's connector (party 2); if the close ran under
    the main RLock (the MED-re-review bug), B's open would block on the main
    RLock, only one party would reach the barrier, and the barrier would time
    out → ``BrokenBarrierError`` (surfaced as a connect failure in B).
    """
    reg = EndpointRegistry()
    # 2 parties, 5s timeout: if B blocks on A's close (the bug), only A's
    # close reaches the barrier → 5s timeout → BrokenBarrierError.
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    state_lock = threading.Lock()
    a_connect_calls = 0

    def a_connector(**_kwargs: object) -> object:
        # 1st call (pre-open): conn whose close() hits the barrier (party 1
        # when force-reopen tears it down). 2nd call (force-reopen's NEW
        # transport): plain mock — its close() is NOT called during the
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
        f"opens raised (barrier timeout ⇒ B blocked on A's slow close, "
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
    """Concurrent ``open profile=A`` (same name) → exactly one transport.

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
# C2: ensure_connected dead-transport pop→reopen
# ---------------------------------------------------------------------------


def test_ensure_connected_dead_transport_reopens_once() -> None:
    """A transport whose liveness probe reports dead triggers exactly one
    reopen (initial connect + one reconnect), then stays alive.

    The connector returns a dead-on-arrival conn on the first call (initial
    ``open``) and a normal alive conn on every subsequent call (the reopen).
    """
    reg = EndpointRegistry()
    conns: list[_MockConn | _DeadOnArrivalConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn | _DeadOnArrivalConn:
        nonlocal connect_calls
        with state_lock:
            connect_calls += 1
            # First connect (initial open): dead-on-arrival → is_alive() False.
            # Every later connect (the reopen): normal alive conn.
            c: _MockConn | _DeadOnArrivalConn = (
                _DeadOnArrivalConn() if connect_calls == 1 else _MockConn()
            )
            conns.append(c)
        return c

    # Initial open registers a transport whose is_alive() reports dead.
    reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert connect_calls == 1, f"expected 1 connect after open, got {connect_calls}"

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
    # The initial dead conn was closed by ensure_connected's pop→close; the
    # reopened conn is the single alive one.
    assert len(alive) + len(closed) == len(conns) == connect_calls == 2


def test_ensure_connected_dead_transport_reopens_once_concurrent() -> None:
    """Concurrent ``ensure_connected`` on a dead transport → exactly one
    reopen (2 connects total), no double-live.

    The initial ``open`` registers a dead-on-arrival transport. Then N threads
    call ``ensure_connected`` concurrently. The per-name lock serializes them:
    the first detects dead and reopens (connect #2, alive); the rest see the
    alive transport and return without reconnecting.
    """
    reg = EndpointRegistry()
    conns: list[_MockConn | _DeadOnArrivalConn] = []
    state_lock = threading.Lock()
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn | _DeadOnArrivalConn:
        nonlocal connect_calls
        with state_lock:
            connect_calls += 1
            c: _MockConn | _DeadOnArrivalConn = (
                _DeadOnArrivalConn() if connect_calls == 1 else _MockConn()
            )
            conns.append(c)
        return c

    reg.open("lab-ssh", home=FIXTURES, connector=connector, probe=False)
    assert connect_calls == 1

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
    RLock (the MED-re-review bug), B's open would block on the main RLock,
    only one party would reach the barrier, and the barrier would time out.

    Also re-verifies the ``ensure_connected`` no-double-live invariant under
    the new out-of-lock close: A reopens exactly once (1 initial + 1 reopen)
    and ends with exactly one alive endpoint for A.
    """
    reg = EndpointRegistry()
    # 2 parties, 5s timeout: if B blocks on A's dead-transport close (the
    # bug), only A's close reaches the barrier → 5s timeout.
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    state_lock = threading.Lock()
    a_connect_calls = 0

    def a_connector(**_kwargs: object) -> object:
        # 1st call (initial open): dead-on-arrival conn (is_closing=True →
        # is_alive False) whose close() hits the barrier (party 1 when
        # ensure_connected tears it down). 2nd call (reopen): plain alive
        # mock — its close() is NOT called during the reopen.
        nonlocal a_connect_calls
        with state_lock:
            a_connect_calls += 1
            n = a_connect_calls
        if n == 1:
            return _DeadBarrierOnCloseConn(barrier)
        return _MockConn()

    def b_connector(**_kwargs: object) -> _BarrierOnConnectConn:
        # B's connector reaches the barrier (party 2) during connect().
        return _BarrierOnConnectConn(barrier)

    # Pre-open A with a dead-on-arrival transport. ensure_connected will
    # detect it as dead (is_alive → False), pop, close (barrier), and reopen.
    reg.open("lab-ssh", home=FIXTURES, connector=a_connector, probe=False)
    assert a_connect_calls == 1

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
        f"raised (barrier timeout ⇒ B blocked on A's dead-transport close, "
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
# SerialRegistry hygiene
# ---------------------------------------------------------------------------


def test_serial_clear_resets_counter() -> None:
    reg = SerialRegistry()
    assert reg.allocate_id() == "con_01"
    assert reg.allocate_id() == "con_02"
    assert reg.allocate_id() == "con_03"
    reg.clear()
    # After clear, the counter resets so the next id is con_01 again.
    assert reg.allocate_id() == "con_01"


def test_serial_list_open_numeric_sort() -> None:
    reg = SerialRegistry()
    # Insert in a deliberately non-numeric, lexicographically misleading order.
    for sid in ("con_100", "con_2", "con_11"):
        reg.add(_make_serial_session(sid))
    ordered = [s.id for s in reg.list_open()]
    # Numeric order, not lexicographic (lex would give con_100 < con_11 < con_2).
    assert ordered == ["con_2", "con_11", "con_100"]
    reg.clear()


def test_serial_remove_exposes_close_errors() -> None:
    reg = SerialRegistry()

    class _BoomConsole:
        def __init__(self) -> None:
            self.port = "/dev/ttyBOOM"

        def is_alive(self) -> bool:
            return False

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            raise OSError("device gone")

    sess = SerialSession(
        id="con_01",
        console=cast(SerialConsole, _BoomConsole()),
        path="/dev/ttyBOOM",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(sess)
    removed = reg.remove("con_01")
    assert removed is not None
    assert removed is sess
    # console.close raised → captured into close_errors with step + exc type.
    assert any(
        "console.close" in e and "OSError" in e and "device gone" in e
        for e in removed.close_errors
    ), f"expected console.close error in close_errors, got: {removed.close_errors}"
    # stop_capture and flush_partial succeeded → not in the error list.
    assert all("stop_capture" not in e for e in removed.close_errors)
    assert all("flush_partial" not in e for e in removed.close_errors)


def test_serial_remove_no_errors_on_clean_close() -> None:
    reg = SerialRegistry()
    reg.add(_make_serial_session("con_01"))
    removed = reg.remove("con_01")
    assert removed is not None
    assert removed.close_errors == []


def test_serial_remove_missing_returns_none() -> None:
    reg = SerialRegistry()
    assert reg.remove("nope") is None


def test_serial_clear_collects_close_errors() -> None:
    reg = SerialRegistry()

    class _BoomConsole:
        def __init__(self) -> None:
            self.port = "/dev/ttyBOOM"

        def is_alive(self) -> bool:
            return False

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            raise OSError("device gone")

    sess = SerialSession(
        id="con_01",
        console=cast(SerialConsole, _BoomConsole()),
        path="/dev/ttyBOOM",
        baud=115200,
        buffer=LineRingBuffer(),
        pump=cast(CapturePump | None, _DummyPump()),
    )
    reg.add(sess)
    reg.clear()
    # clear() removed the session; the close error is recorded on the session.
    assert reg.get("con_01") is None
    assert any("console.close" in e for e in sess.close_errors)


# ---------------------------------------------------------------------------
# SerialRegistry concurrency (smoke): allocate/list/remove under lock
# ---------------------------------------------------------------------------


def test_serial_allocate_add_remove_concurrent() -> None:
    reg = SerialRegistry()
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    N = 8
    PER = 10

    def worker() -> None:
        local_errs: list[BaseException] = []
        for _ in range(PER):
            try:
                sid = reg.allocate_id()
                reg.add(_make_serial_session(sid))
                reg.list_open()
                reg.remove(sid)
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
    # All allocated sessions were removed by their owning thread.
    assert reg.list_open() == []