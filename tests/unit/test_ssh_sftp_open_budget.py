"""Wall-clock budget for the SSH SFTP channel open, and cached-client liveness.

An unbounded open parks ``open_sftp`` on ``Future.result()`` while holding the
transport serial op-lock, freezing every later op and ``close`` on that
endpoint when the peer completes TCP and auth but never answers the SFTP
subsystem request. The open must instead spend a finite, connect-derived
budget, cache nothing, and raise ``TransportError(TIMEOUT)``.

The cached-client probe is pinned here against the real asyncssh classes: it
must read the closed state of the channel behind ``SFTPClient``, not the
attribute names asyncssh does not define.
"""

from __future__ import annotations

import threading
import time

import pytest
from asyncssh.sftp import SFTPClient, SFTPClientHandler
from asyncssh.stream import SSHReader, SSHWriter

from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.transport import ssh as ssh_module
from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.ssh import SSHTransport


class _NeverAnsweringConn:
    """Established connection whose SFTP subsystem request is black-holed."""

    def __init__(self) -> None:
        self.sftp_starts = 0
        self.close_calls = 0

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        """Sync initiate-close, as asyncssh's connection exposes it."""
        self.close_calls += 1

    async def start_sftp_client(self):
        self.sftp_starts += 1
        import asyncio

        await asyncio.Event().wait()  # reply never arrives


def _open_in_thread(
    t: SSHTransport, deadline: float
) -> tuple[list[str], BaseException | None]:
    """Call ``open_sftp`` on a worker thread; return (outcome, raised).

    The worker is a daemon so a regression that restores the unbounded wait
    fails the assertion below instead of hanging the test session.
    """
    outcome: list[str] = []
    raised: list[BaseException] = []
    started = time.monotonic()

    def _run() -> None:
        try:
            t.open_sftp()
            outcome.append("returned")
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            raised.append(exc)
            outcome.append(f"{type(exc).__name__}[{getattr(exc, 'code', '?')}]: {exc}")
        outcome.append(f"elapsed={time.monotonic() - started:.2f}")

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=deadline)
    if worker.is_alive():
        outcome.append("STILL BLOCKED")
    return outcome, (raised[0] if raised else None)


def test_sftp_open_budget_derives_from_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_module, "_SFTP_OPEN_TIMEOUT_GRACE_S", 5.0)
    t = SSHTransport(host="h", username="u", connect_timeout_ms=15000)
    assert t._sftp_open_budget_s() == pytest.approx(20.0)

    fast = SSHTransport(host="h", username="u", connect_timeout_ms=1000)
    assert fast._sftp_open_budget_s() == pytest.approx(6.0)

    # A non-positive profile timeout still yields a finite, usable budget.
    broken = SSHTransport(host="h", username="u", connect_timeout_ms=0)
    assert 0.0 < broken._sftp_open_budget_s() <= 6.0


def test_open_sftp_times_out_instead_of_parking_the_op_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_module, "_SFTP_OPEN_TIMEOUT_GRACE_S", 0.3)
    conn = _NeverAnsweringConn()
    t = SSHTransport(
        host="h",
        username="u",
        connect_timeout_ms=200,
        connector=lambda **_k: conn,
    )
    t.connect()
    budget = t._sftp_open_budget_s()

    outcome, raised = _open_in_thread(t, deadline=budget + 4.0)

    assert outcome[-1] != "STILL BLOCKED", outcome
    assert isinstance(raised, TransportError), outcome
    assert raised.code == "TIMEOUT", outcome
    assert raised.details["timeout_s"] == pytest.approx(budget)
    assert float(outcome[-1].split("=")[1].rstrip("s")) < budget + 3.0

    # Nothing half-opened is handed back, and the serial op-lock is free so
    # endpoint close / later ops can run.
    assert t._sftp is None
    acquired = t._op_lock.acquire(timeout=1.0)
    if acquired:
        t._op_lock.release()
    assert acquired is True

    # A cancelled mid-flight request can leave the connection stale; the
    # transport must not offer it for further ops, and it initiates the close
    # so the remote-side subsystem channel the open half-created is released.
    assert t.is_connected() is False
    assert (t.meta or {}).get("dead_reason") == "sftp channel open timeout"
    assert conn.close_calls == 1


def test_open_sftp_retry_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A black-holed peer is not remembered as a cached broken client."""
    monkeypatch.setattr(ssh_module, "_SFTP_OPEN_TIMEOUT_GRACE_S", 0.3)
    conn = _NeverAnsweringConn()
    t = SSHTransport(
        host="h",
        username="u",
        connect_timeout_ms=200,
        connector=lambda **_k: conn,
    )
    t.connect()

    outcome, raised = _open_in_thread(t, deadline=t._sftp_open_budget_s() + 4.0)
    assert isinstance(raised, TransportError), outcome
    assert raised.code == "TIMEOUT", outcome
    assert t._sftp is None

    t.connect()  # reconnect after the timeout marked the session dead
    outcome, raised = _open_in_thread(t, deadline=t._sftp_open_budget_s() + 4.0)
    assert isinstance(raised, TransportError), outcome
    assert raised.code == "TIMEOUT", outcome
    assert conn.sftp_starts == 2


def test_fs_backend_is_bounded_when_the_subsystem_open_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-facing path: ``SftpFs(factory=transport.open_sftp)``.

    The backend evaluates its factory in the calling thread before it can
    consult its own budget, so the bound has to come from the transport - a
    hung channel-open must surface as an error there instead of wedging the
    fs op and the transport op-lock behind it.
    """
    monkeypatch.setattr(ssh_module, "_SFTP_OPEN_TIMEOUT_GRACE_S", 0.3)
    conn = _NeverAnsweringConn()
    t = SSHTransport(
        host="lab-ssh",
        username="u",
        connect_timeout_ms=200,
        connector=lambda **_k: conn,
    )
    t.connect()
    backend = SftpFs(
        factory=t.open_sftp,
        cwd="/",
        home="/home/u",
        timeout_s=1.0,
        op_timeout_s=1.0,
    )
    outcome: list[str] = []
    started = time.monotonic()

    def _list() -> None:
        try:
            backend.list("/")
            outcome.append("returned")
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            outcome.append(f"{type(exc).__name__}[{getattr(exc, 'code', '?')}]")
        outcome.append(f"elapsed={time.monotonic() - started:.2f}")

    worker = threading.Thread(target=_list, daemon=True)
    worker.start()
    worker.join(timeout=t._sftp_open_budget_s() + 4.0)

    assert not worker.is_alive(), outcome
    assert outcome[0] == "TransportError[TIMEOUT]", outcome
    acquired = t._op_lock.acquire(timeout=1.0)
    if acquired:
        t._op_lock.release()
    assert acquired is True


# ---------------------------------------------------------------------------
# Cached-client liveness probe against the real asyncssh classes
# ---------------------------------------------------------------------------


class _Log:
    def get_child(self, *_a: object) -> _Log:
        return self

    def info(self, *_a: object, **_k: object) -> None:
        pass

    def warning(self, *_a: object, **_k: object) -> None:
        pass


class _StubChannel:
    """Only the surface asyncssh's reader/writer use, plus the closed flag."""

    logger = _Log()

    def __init__(self, *, closing: bool) -> None:
        self._closing = closing

    def is_closing(self) -> bool:
        return self._closing


class _StubSession:
    logger = _Log()


def _real_sftp_client(*, closing: bool) -> SFTPClient:
    """A genuine ``asyncssh.sftp.SFTPClient`` over a channel in *closing* state."""
    chan = _StubChannel(closing=closing)
    reader = SSHReader(_StubSession(), chan)
    writer = SSHWriter(_StubSession(), chan)
    handler = SFTPClientHandler(None, "strict", reader, writer, 3)
    return SFTPClient(handler, "utf-8", "strict")


def test_sftp_channel_reaches_real_asyncssh_client_channel() -> None:
    client = _real_sftp_client(closing=False)
    # A real SFTPClient defines none of these; the channel is behind the handler.
    for attr in ("_closed", "is_closed", "closed", "_channel", "channel"):
        assert not hasattr(client, attr)

    chan = SSHTransport._sftp_channel(client)
    assert chan is client._handler._reader.channel


def test_sftp_looks_dead_detects_closing_real_client_channel() -> None:
    live = _real_sftp_client(closing=False)
    dead = _real_sftp_client(closing=True)

    assert dead._handler._reader.channel.is_closing() is True
    assert SSHTransport._sftp_looks_dead(dead) is True
    assert SSHTransport._sftp_looks_dead(live) is False
