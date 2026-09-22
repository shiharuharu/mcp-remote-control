"""Service tests: the WinRM op/read ordering invariant and replay-guard state.

Two invariants the WinRM retry/timeout machinery must never violate:

* The HTTP read timeout outlasts the WSMan operation timeout. Equal or
  inverted values make the client read-timeout fire first, and that
  ``ReadTimeout`` is classified ``link_fatal`` - so a self-inflicted ordering
  bug tears the endpoint down for a merely slow command.
* A replay guard that cannot observe the link says so, and stops trusting a
  rejection it cannot place. The round-trip counter needs pypsrp's private
  ``_send_request``; when it is missing the state is visible in ``meta`` and
  in the log, and a payload carrying the caller's work is replayed only when
  the rejection is provably pre-execution.

Every stop that runs on the caller's thread while it holds the transport op
lock - ``_stop_handle`` and the pooled adapter's ``prepare_invoke`` stop - is
bounded, like every other stop/close path in the transport.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests.exceptions as requests_exceptions

from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.winrm import WinRMTransport, _EXIT_MARKER
from mcp_remote_control.transport.winrm_runspace import _STOP_DEADLINE_S
from mcp_remote_control.transport.winrm_timeouts import (
    PYPSRP_DEFAULT_OPERATION_TIMEOUT_S,
    PYPSRP_HTTP_TIMEOUT_SLACK_S,
    resolve_pypsrp_op_read_timeouts,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _ordered(op: int | None, rd: int | None) -> bool:
    """True when the resolved pair satisfies the read-outlasts-op invariant."""
    if op is None or rd is None:
        return True
    return rd >= op + PYPSRP_HTTP_TIMEOUT_SLACK_S


# ---------------------------------------------------------------------------
# resolution: the read timeout always outlasts the operation timeout
# ---------------------------------------------------------------------------


def test_resolved_pair_always_outlasts_for_every_combination() -> None:
    """Every combination of explicit/budget/derived inputs keeps read >= op+slack.

    No combination is exempt: an explicit read is the ceiling on one HTTP
    exchange, so an operation timeout configured above it is capped, whether
    that op came from the profile or from the call budget.
    """
    combos = [
        # both explicit (the README's own 20/22 pairing)
        dict(timeout_s=5, operation_timeout_s=20, read_timeout_s=22),
        dict(timeout_s=None, operation_timeout_s=20, read_timeout_s=22),
        # both explicit and inverted by hand, with and without a call budget
        dict(timeout_s=5, operation_timeout_s=99, read_timeout_s=88),
        dict(timeout_s=None, operation_timeout_s=60, read_timeout_s=10),
        # only read explicit, with and without a call budget
        dict(timeout_s=60, read_timeout_s=22),
        dict(timeout_s=5, read_timeout_s=88),
        dict(timeout_s=None, read_timeout_s=15),
        dict(timeout_s=None, read_timeout_s=22),
        dict(timeout_s=None, read_timeout_s=2),
        # only op explicit
        dict(timeout_s=5, operation_timeout_s=60),
        dict(operation_timeout_s=120),
        # neither
        dict(timeout_s=5),
        dict(timeout_s=None),
        dict(),
    ]
    for kw in combos:
        op, rd = resolve_pypsrp_op_read_timeouts(**kw)
        assert _ordered(op, rd), f"{kw} -> {(op, rd)} violates read >= op + slack"


def test_read_only_profile_with_call_budget_caps_the_derived_op() -> None:
    """An explicit read_timeout_s is the operator's ceiling on the exchange.

    The call budget derives the operation timeout, but it may not exceed the
    read the operator declared, or the client read-timeout fires first.
    """
    assert resolve_pypsrp_op_read_timeouts(timeout_s=60, read_timeout_s=22) == (
        20,
        22,
    )
    # A budget that already fits under the read is untouched.
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5, read_timeout_s=88) == (5, 88)


def test_read_only_profile_without_budget_caps_against_library_default() -> None:
    """No call budget: pypsrp's default op is the value the read must outlast.

    ``read_timeout_s = 15`` would otherwise leave the default op (20) running
    past the client's read deadline, so the op is capped to 15 - slack.
    """
    assert resolve_pypsrp_op_read_timeouts(read_timeout_s=15) == (13, 15)
    # A read that already outlasts the default leaves the op at the default.
    assert resolve_pypsrp_op_read_timeouts(read_timeout_s=88) == (
        PYPSRP_DEFAULT_OPERATION_TIMEOUT_S,
        88,
    )
    # A read with room for no positive op at all moves the read instead.
    assert resolve_pypsrp_op_read_timeouts(read_timeout_s=2) == (1, 3)


def test_fully_explicit_pair_is_capped_to_the_read_timeout() -> None:
    """Both numbers explicit: the operator's pair, made to satisfy the invariant.

    An explicit read timeout is the ceiling on one HTTP exchange, so an
    operation timeout configured above it is dead configuration - the client
    stops waiting before the server-side op can ever fire. Forwarding such a
    pair verbatim rebuilds the client-read-first ordering by hand, and that
    ``ReadTimeout`` is classified ``link_fatal``: a slow-but-healthy command
    would tear the endpoint down and could be repeated. The op is therefore
    capped at ``read - slack``; the read is never lowered by the clamp.
    """
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=5, operation_timeout_s=99, read_timeout_s=88
    ) == (88 - PYPSRP_HTTP_TIMEOUT_SLACK_S, 88)
    # An ordered explicit pair is untouched: the clamp only removes ops the
    # read timeout could never let complete.
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=5, operation_timeout_s=60, read_timeout_s=88
    ) == (60, 88)


class _FakeHttpTransport:
    def __init__(self) -> None:
        self.read_timeout = 30


class _FakeWsman:
    def __init__(self) -> None:
        self.operation_timeout = 20
        self.transport = _FakeHttpTransport()


class _PeerModelSession:
    """Oneshot session modelling the HTTP/server ordering of one exchange.

    pypsrp builds its HTTP transport with ``http_timeout = operation_timeout +
    2``; the transport re-applies the resolved pair on the live wsman node for
    the call's duration. When the applied operation timeout outlasts the
    applied HTTP read timeout, the server is still working at the read
    deadline, so the client raises ``ReadTimeout`` - the shape the taxonomy
    classifies ``link_fatal``. The model answers from the applied pair alone,
    so a violated pair fails the call instead of sleeping.
    """

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.wsman = _FakeWsman()
        self.applied: list[tuple[int, int]] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del script, environment
        op = int(self.wsman.operation_timeout)
        rd = int(self.wsman.transport.read_timeout)
        self.applied.append((op, rd))
        if op + PYPSRP_HTTP_TIMEOUT_SLACK_S > rd:
            raise requests_exceptions.ReadTimeout(
                f"read timeout={rd} (op={op} still running)"
            )
        return (f"ok\n{_EXIT_MARKER}0\n", None, False)


def test_call_on_read_only_profile_keeps_the_endpoint_alive() -> None:
    """The trigger: read_timeout_s alone plus a 60s call budget.

    The applied pair must satisfy the ordering, so the call completes instead
    of tearing the endpoint down over a self-inflicted ReadTimeout.
    """
    sess = _PeerModelSession()
    t = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        read_timeout_s=22,
        connector=lambda **_k: sess,
    )
    t.connect()
    r = t.run_command("Get-Date", timeout_s=60)
    assert r.exit_code == 0, r.stderr
    assert t.is_connected() is True
    assert t.meta.get("link_lost") is None
    assert sess.applied, "execute_ps must run under the applied pair"
    op, rd = sess.applied[-1]
    assert (op, rd) == (20, 22)
    assert rd >= op + PYPSRP_HTTP_TIMEOUT_SLACK_S


# ---------------------------------------------------------------------------
# replay guard: an unobservable counter is visible, not silent
# ---------------------------------------------------------------------------


class _StateOnlyTransport:
    """pypsrp-shaped re-handshake state with no countable ``_send_request``."""

    def __init__(self) -> None:
        self.encryption: object = "auto"
        self.session: object = None


class _CountableTransport:
    """pypsrp-shaped HTTP node exposing the counter's private hook."""

    def __init__(self) -> None:
        self.encryption: object = "auto"
        self.session: object = None
        self.exchanges = 0

    def _send_request(self, request: object, timeout: object = None) -> bytes:
        del request, timeout
        self.exchanges += 1
        return b"ok"


class _GuardSession:
    """Session whose HTTP node is countable or not, per *countable*."""

    def __init__(self, *, countable: bool) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        transport = _CountableTransport() if countable else _StateOnlyTransport()
        self.transport = transport
        # Minimal pypsrp ``WSMan`` stand-in pointing at the HTTP node.
        self.wsman = SimpleNamespace(transport=transport)

    def close(self) -> None:
        self.closed = True


def _gateway_5xx() -> BaseException:
    """A front gateway's own error page: 502 with a body (may have forwarded)."""
    import pypsrp.exceptions as exc

    return exc.WinRMTransportError("http", 502, "Connection error: read ETIMEDOUT")


def _refusal_400() -> BaseException:
    """The stale-encryption signature: 4xx with an empty body (never ran)."""
    import pypsrp.exceptions as exc

    return exc.WinRMTransportError("http", 400, "")


def test_missing_counter_is_recorded_and_warned_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A counter-less session degrades visibly: meta + one warning at connect."""
    sess = _GuardSession(countable=False)
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.transport.winrm"):
        t.connect()
    assert t._round_trips is None  # noqa: SLF001
    assert t.meta.get("link_replay_guard") == "degraded"
    assert t.meta.get("link_replay_guard_reason") == "no_counter"
    warnings = [
        r for r in caplog.records if "replay guard degraded" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_unreadable_counter_is_recorded(caplog: pytest.LogCaptureFixture) -> None:
    """A reader that fails mid-call marks the degraded state and returns None."""
    sess = _GuardSession(countable=False)
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    t.meta.pop("link_replay_guard", None)
    t.meta.pop("link_replay_guard_reason", None)
    t._round_trips = _raising_reader  # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="mcp_remote_control.transport.winrm"):
        assert t._link_round_trips() is None  # noqa: SLF001
    assert t.meta.get("link_replay_guard") == "degraded"
    assert t.meta.get("link_replay_guard_reason") == "unreadable"
    assert any("replay guard degraded" in r.getMessage() for r in caplog.records)


def _raising_reader() -> int:
    raise RuntimeError("counter vanished")


def test_countable_session_reports_no_degradation() -> None:
    """An installable counter leaves meta clean (the guard is observing)."""
    sess = _GuardSession(countable=True)
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    assert t._round_trips is not None  # noqa: SLF001
    assert "link_replay_guard" not in t.meta


def test_missing_counter_does_not_replay_a_user_work_payload() -> None:
    """No counter: only a provable refusal is replayed on a user-work call.

    Without the counter the guard cannot tell a rejection of the operation's
    structural first exchange from one belonging to a later exchange that
    already dispatched the caller's script, so the oneshot exec surface
    refuses a body-carrying rejection. A provable refusal still replays, a
    readable-and-unmoved counter still clears the replay, and the read-only
    identity probe keeps the permissive path because repeating its fixed query
    repeats no caller work.
    """
    sess = _GuardSession(countable=False)
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    # A provable refusal dispatched nothing, so the replay stands.
    assert t._link_replay_allowed(None, _refusal_400()) is True  # noqa: SLF001
    # A body-carrying 5xx may have been forwarded: refused for user work.
    assert t._link_replay_allowed(None, _gateway_5xx()) is False  # noqa: SLF001
    # The read-only probe opts out of the user-work rule.
    assert (  # noqa: SLF001
        t._link_replay_allowed(None, _gateway_5xx(), carries_user_work=False) is True
    )
    # The pooled-invoke path stays refusal-only.
    assert (  # noqa: SLF001
        t._link_replay_allowed(None, _gateway_5xx(), first_payload_is_command=True)
        is False
    )

    # A readable, unmoved counter proves the rejected request was the first
    # exchange, so the replay is safe even for a user-work operation.
    countable = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        connector=lambda **_k: _GuardSession(countable=True),
    )
    countable.connect()
    assert countable._round_trips is not None  # noqa: SLF001
    assert countable._link_replay_allowed(0, _gateway_5xx()) is True  # noqa: SLF001


class _UncountableExecSession:
    """Oneshot ``execute_ps`` with no countable HTTP node; first call fails."""

    def __init__(self, error: BaseException) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.scripts: list[str] = []
        self._error = error
        self.transport = _StateOnlyTransport()
        self.wsman = SimpleNamespace(transport=self.transport)

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        if len(self.scripts) == 1:
            raise self._error
        return (f"ok\n{_EXIT_MARKER}0\n", None, False)


def _exec_transport(sess: object) -> WinRMTransport:
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    return t


def test_exec_body_rejection_without_a_counter_is_not_replayed() -> None:
    """End to end: the counter-less exec surface refuses a body-carrying 5xx.

    The whole ``execute_ps`` closure would otherwise run again, sending the
    script on a request the gateway may already have forwarded.
    """
    sess = _UncountableExecSession(_gateway_5xx())
    t = _exec_transport(sess)
    assert t._round_trips is None  # noqa: SLF001
    with pytest.raises(TransportError):
        t.run_command("echo hi", timeout_s=5)
    assert len(sess.scripts) == 1, "a user-work payload was replayed without proof"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("dead_reason") == "link lost"


def test_exec_provable_refusal_without_a_counter_still_self_heals() -> None:
    """Control: the refusal path keeps its single self-heal replay.

    Only the body-carrying shape is refused; a 4xx-with-empty-body rejection
    provably dispatched nothing, so the counter-less exec surface still
    re-handshakes and replays once.
    """
    sess = _UncountableExecSession(_refusal_400())
    t = _exec_transport(sess)
    r = t.run_command("echo hi", timeout_s=5)
    assert r.exit_code == 0, r.stderr
    assert len(sess.scripts) == 2, "exactly one replay"
    assert t.meta.get("session_resynced") is True
    assert t.meta.get("marked_dead") is None


# ---------------------------------------------------------------------------
# _stop_handle is bounded: a hung stop cannot pin the op lock
# ---------------------------------------------------------------------------


class _StopSession:
    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BlockingStopHandle:
    """Custom handle: invoke blocks, ``stop()`` blocks far longer."""

    def __init__(self, stop_sleep_s: float) -> None:
        self.location = r"C:\Users\mock"
        self.stopped = threading.Event()
        self.unblock = threading.Event()
        self._stop_sleep_s = stop_sleep_s

    def invoke(self, script: str) -> object:
        del script
        self.unblock.wait(timeout=30.0)
        return ("ok\n", None)

    def stop(self) -> None:
        self.stopped.set()
        time.sleep(self._stop_sleep_s)


def test_hung_handle_stop_is_bounded_and_releases_the_op_lock() -> None:
    """The invoke timeout must not be extended by a blocking ``handle.stop()``.

    ``_stop_handle`` is called on the caller's thread while it holds the
    transport op lock, so an unbounded stop would freeze every later op on the
    endpoint - including the documented remedy, ``endpoint close``.
    """
    sess = _StopSession()
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    handle = _BlockingStopHandle(stop_sleep_s=_STOP_DEADLINE_S * 4)
    result: dict[str, object] = {}

    def _invoke() -> None:
        t0 = time.monotonic()
        try:
            r = t.runspace_invoke(handle, "Get-Date", timeout_s=1.0)
            result["outcome"] = f"returned timed_out={r.timed_out}"
        except Exception as exc:  # noqa: BLE001
            result["outcome"] = f"raised {type(exc).__name__}"
        result["elapsed"] = time.monotonic() - t0

    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    try:
        # Well past the 1s invoke timeout + the stop deadline: the stop is
        # still sleeping inside its daemon thread, so the lock must be free.
        worker.join(timeout=_STOP_DEADLINE_S + 2.0)
        assert not worker.is_alive(), "runspace_invoke still pinned by handle.stop()"
        assert handle.stopped.is_set(), "stop() was attempted"
        assert result["outcome"] == "returned timed_out=True", result
        assert float(result["elapsed"]) < _STOP_DEADLINE_S + 2.0, result

        released = threading.Event()

        def _close() -> None:
            t.close()
            released.set()

        closer = threading.Thread(target=_close, daemon=True)
        closer.start()
        assert released.wait(timeout=3.0), "a later serial op could not take the lock"
    finally:
        handle.unblock.set()


def test_unbounded_stop_would_have_pinned_the_lock() -> None:
    """Control: the same handle with a direct, unbounded ``stop()`` pins longer.

    Keeps the bound above from being mistaken for the handle simply not
    blocking: calling ``stop()`` straight on this thread outlives the deadline
    the transport applies.
    """
    handle = _BlockingStopHandle(stop_sleep_s=_STOP_DEADLINE_S * 2)
    t0 = time.monotonic()
    handle.stop()
    assert time.monotonic() - t0 >= _STOP_DEADLINE_S * 2 - 0.1


def test_stop_handle_swallows_a_raising_stop() -> None:
    """Best-effort stays best-effort: a raising stop is not a call failure."""

    class _Raising:
        def stop(self) -> None:
            raise TransportError("PS_CLOSED", "boom")

    WinRMTransport._stop_handle(_Raising())  # noqa: SLF001


class _BlockingPrepareStopHandle:
    """Handle whose ``prepare_invoke`` returns a blocking run and stop."""

    def __init__(self, stop_sleep_s: float) -> None:
        self.location = r"C:\Users\mock"
        self.stopped = threading.Event()
        self.unblock = threading.Event()
        self._stop_sleep_s = stop_sleep_s

    def invoke(self, script: str) -> object:
        del script
        return ("ok\n", None)

    def prepare_invoke(self, script: str) -> tuple[object, object]:
        del script

        def _run() -> object:
            self.unblock.wait(timeout=30.0)
            return ("ok\n", None)

        def _stop() -> None:
            self.stopped.set()
            time.sleep(self._stop_sleep_s)

        return _run, _stop


def test_hung_prepare_invoke_stop_is_bounded_and_releases_the_op_lock() -> None:
    """A blocking per-invoke stop must not extend the invoke's timeout either.

    The pool adapter's ``stop`` runs on the caller's thread while it holds the
    transport op lock, exactly like ``handle.stop()``, so it gets the same
    wall-clock bound.
    """
    sess = _StopSession()
    t = WinRMTransport(
        host="win.example", username="u", password="p", connector=lambda **_k: sess
    )
    t.connect()
    handle = _BlockingPrepareStopHandle(stop_sleep_s=_STOP_DEADLINE_S * 4)
    result: dict[str, object] = {}

    def _invoke() -> None:
        t0 = time.monotonic()
        try:
            r = t.runspace_invoke(handle, "Get-Date", timeout_s=1.0)
            result["outcome"] = f"returned timed_out={r.timed_out}"
        except Exception as exc:  # noqa: BLE001
            result["outcome"] = f"raised {type(exc).__name__}"
        result["elapsed"] = time.monotonic() - t0

    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    try:
        worker.join(timeout=_STOP_DEADLINE_S + 2.0)
        assert not worker.is_alive(), "runspace_invoke still pinned by stop_fn()"
        assert handle.stopped.is_set(), "the per-invoke stop was attempted"
        assert result["outcome"] == "returned timed_out=True", result
        assert float(result["elapsed"]) < _STOP_DEADLINE_S + 2.0, result

        released = threading.Event()

        def _close() -> None:
            t.close()
            released.set()

        closer = threading.Thread(target=_close, daemon=True)
        closer.start()
        assert released.wait(timeout=3.0), "a later serial op could not take the lock"
    finally:
        handle.unblock.set()


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"read_timeout_s": 10},
        {"read_timeout_s": 5},
        {"read_timeout_s": 22},
        {"operation_timeout_s": 20, "read_timeout_s": 30},
        # Explicitly inverted: the connect path must clamp it rather than
        # forward it - no per-call resolver runs for open_fs / runspace opens.
        {"operation_timeout_s": 60, "read_timeout_s": 22},
        {"operation_timeout_s": 3, "read_timeout_s": 30},
    ],
)
def test_connect_kwargs_keeps_the_op_read_ordering(profile: dict) -> None:
    """The ordering rule holds on the connect path too, not only per call.

    ``connect_kwargs`` builds the pypsrp client for the whole session, and the
    connect-time pair is what open_fs / open_runspace / every fs call run on -
    none of which re-resolve per call. Forwarding the raw profile pair there
    reintroduces the client-read-first timeout that gets classified
    ``link_fatal``, tearing the endpoint down for a merely slow exchange.
    """
    from mcp_remote_control.transport.winrm_timeouts import (
        PYPSRP_HTTP_TIMEOUT_SLACK_S,
    )

    t = WinRMTransport(host="h", username="u", **profile)
    kwargs = t.connect_kwargs()
    op = kwargs.get("operation_timeout")
    rd = kwargs.get("read_timeout")

    if op is None and rd is None:
        # Nothing configured: leave pypsrp's own defaults alone.
        assert profile == {}, profile
        return
    assert op is not None and rd is not None, (op, rd)
    assert rd >= op + PYPSRP_HTTP_TIMEOUT_SLACK_S, (
        f"connect-time op/read inverted for {profile}: op={op} read={rd}"
    )
