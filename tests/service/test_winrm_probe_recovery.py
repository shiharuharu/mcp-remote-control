"""Service tests: WinRM open-probe self-heal and the exact replay guard.

The open-time identity probe drives a real transport, so a stale
message-encryption frame (or a front gateway's 502) rejects its first HTTP
exchange exactly like an exec call does. The probe must re-handshake and replay
once **inside the same budget** instead of failing ``endpoint open`` outright,
while still hard-failing identity when the replay fails too.

A replay is only provably free of side effects when no *payload-carrying* HTTP
exchange completed during the operation: the stale frame needs an idle gap of
several seconds, and any successful exchange in between rebuilds the encryption
context. pypsrp's bodyless authentication POST is not such an exchange - a
fresh link sends it before the operation's own POST, so counting it refused the
self-heal exactly on a cold link. The installed round-trip counter turns that
argument into a check: once the operation has exchanged a payload successfully,
a link rejection is not provably its first request and must not be replayed.

One operation needs more than the counter: a pooled runspace ``invoke`` sends
the command in its very first WSMan message, so no completed exchange is needed
for a side effect to exist. It is replayed only when the rejection is provably
a refusal - an empty-body one, which the framing layer produced before any
pipeline could be built. A rejection carrying a body came from an intermediary
and is never replayed.
"""

from __future__ import annotations

import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import requests
import requests.exceptions as requests_exceptions

from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import get_registry
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.winrm import WinRMTransport, _EXIT_MARKER
from mcp_remote_control.transport.winrm_runspace import RunspaceResult
from mcp_remote_control.transport.winrm_session import (
    AdaptedWinRMSession,
    install_winrm_round_trip_counter,
)

try:  # pypsrp is a hard dependency; the fallback keeps the shape testable.
    from pypsrp import exceptions as pypsrp_exceptions
except ImportError:  # pragma: no cover - exercised only without pypsrp
    pypsrp_exceptions = None  # type: ignore[assignment]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Capability-probe stdout in its key=value fallback shape: parseable identity,
# so a successful probe leaves open ok.
CAPABILITY_STDOUT = "\n".join(
    (
        "ps_version=5.1.19041.1",
        "ps_edition=Desktop",
        "language_mode=FullLanguage",
        "os_version=10.0.19041.0",
        "has_convertto_json=True",
        "can_get_item=True",
        "can_file_io=True",
    )
)


def _rejection(status: int = 400) -> BaseException:
    """The measured stale-encryption / framing shape: HTTP status, empty body."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", status, "")
    return type(
        "WinRMTransportError",
        (Exception,),
        {"__module__": "pypsrp.exceptions", "__qualname__": "WinRMTransportError"},
    )("http", status, "")


def _body_rejection(
    body: str = "Connection error: read ETIMEDOUT",
    status: int = 502,
) -> BaseException:
    """A front gateway's own error page: the rejection carries a body."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", status, body)
    return type(
        "WinRMTransportError",
        (Exception,),
        {"__module__": "pypsrp.exceptions", "__qualname__": "WinRMTransportError"},
    )("http", status, body)


# ---------------------------------------------------------------------------
# pypsrp-shaped doubles (no network sockets)
# ---------------------------------------------------------------------------


class _FakeHttpSession:
    """Stand-in for the cached ``requests.Session`` a re-handshake drops."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeStateTransport:
    """Cached encryption state resync clears, with no countable send.

    Sessions built on this node stand in for doubles that expose pypsrp's
    re-handshake state but no HTTP surface, so no round-trip counter can be
    installed.
    """

    def __init__(self) -> None:
        self.encryption: object = "auto"
        self.session: object = _FakeHttpSession()
        # Stable handle for assertions: resync clears ``session`` but the
        # object it closed is still observable here.
        self.http_session: object = self.session


class _FakeCountableTransport:
    """pypsrp-shaped HTTP transport: scripted outcomes, one per exchange.

    ``outcomes`` entries are responses (ignored) or exceptions raised in place
    of one; the last entry repeats once the list is exhausted. Only exchanges
    that return normally are counted, so a raised entry never reaches the
    installed counter.
    """

    def __init__(self, outcomes: list[object]) -> None:
        self.encryption: object = "auto"
        self.session: object = _FakeHttpSession()
        self.http_session: object = self.session
        self.attempts = 0
        self._outcomes = list(outcomes)

    def _send_request(self, request: object, timeout: object = None) -> bytes:
        del request, timeout
        outcome = self._outcomes[min(self.attempts, len(self._outcomes) - 1)]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return b"ok"


class _FakeWsman:
    """Links a session to its transport the way pypsrp ``WSMan`` does."""

    def __init__(self, transport: object) -> None:
        self.transport = transport


class _ProbeSession:
    """Seedless oneshot session driving a scripted transport.

    Without identity seeds the open probe must issue a remote RTT, and
    ``execute_ps`` performs ``exchanges_per_call`` HTTP exchanges so a
    rejection can be placed on the first exchange of an operation or after a
    completed one.
    """

    def __init__(
        self,
        outcomes: list[object],
        *,
        exchanges_per_call: int = 1,
        stdout: str = CAPABILITY_STDOUT,
    ) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []
        self.exchanges_per_call = exchanges_per_call
        self.stdout = stdout
        self.transport = _FakeCountableTransport(outcomes)
        self.wsman = _FakeWsman(self.transport)

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
        for _ in range(self.exchanges_per_call):
            self.transport._send_request(object())
        # The oneshot exec path appends an exit probe to the caller's script, so
        # a real payload carries its marker. Probe scripts (identity /
        # capability) carry none, so their payloads stay marker-free.
        stdout = self.stdout
        if _EXIT_MARKER in script:
            stdout = f"{stdout}\n{_EXIT_MARKER}0"
        return (stdout, None, False)


class _UncountableProbeSession:
    """No ``_send_request`` anywhere: no round-trip counter can be installed."""

    def __init__(self, *, fail_times: int = 1) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []
        self._fail_times = fail_times
        self.transport = _FakeStateTransport()
        self.wsman = _FakeWsman(self.transport)

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
        if len(self.scripts) <= self._fail_times:
            raise _rejection()
        return (CAPABILITY_STDOUT, None, False)


class _ReplayBlocksSession:
    """First exchange rejected as retryable; the replay then burns the budget.

    ``first_delay`` makes the first attempt consume a measurable share of the
    budget before it is rejected, so a replay granted a *fresh* budget runs
    visibly past the caller's deadline.
    """

    def __init__(self, first_delay: float = 0.0) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []
        self.first_delay = first_delay
        self.block = threading.Event()
        self.transport = _FakeStateTransport()
        self.wsman = _FakeWsman(self.transport)

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
            time.sleep(self.first_delay)
            raise _rejection()
        self.block.wait(timeout=30.0)
        return (CAPABILITY_STDOUT, None, False)


class _ExchangeInvokeHandle:
    """Runspace handle whose invoke performs scripted HTTP exchanges."""

    def __init__(
        self, transport: object, exchanges: int, *, fail_times: int = 0
    ) -> None:
        self.location = None
        self.invokes: list[str] = []
        self._transport = transport
        self._exchanges = exchanges
        self._fail_times = fail_times

    def invoke(self, script: str) -> object:
        self.invokes.append(script)
        if len(self.invokes) <= self._fail_times:
            raise _rejection()
        for _ in range(self._exchanges):
            self._transport._send_request(object())
        return ("ps-out\n", None)


# ---------------------------------------------------------------------------
# real-pypsrp doubles: pypsrp owns the bodyless authentication POST
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    """Minimal ``requests`` response: enough for pypsrp's ``_send_request``."""

    def __init__(self, status: int = 200, body: bytes = b"<rsp/>") -> None:
        self.status_code = status
        self.headers = {"content-type": "application/soap+xml;charset=UTF-8"}
        self.content = body
        self.text = body.decode()
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _FakeAuthContext:
    """Pypsrp auth context: the encryption helper only needs ``wrap_winrm``."""

    response_auth_header = "ntlm"

    def wrap_winrm(self, payload: bytes) -> tuple[bytes, bytes, int]:
        return (b"", payload, 0)


class _HttpScript:
    """Ordered HTTP outcomes shared by every session a re-handshake builds.

    Records each prepared request, so a test can tell pypsrp's bodyless
    authentication POST from the operation's own message.
    """

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[Any] = []

    def respond(self, prepared: Any) -> object:
        self.sent.append(prepared)
        outcome = self._outcomes[min(len(self.sent), len(self._outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def shapes(self) -> list[str]:
        """``bodyless`` for an authentication POST, ``payload`` otherwise."""
        return ["bodyless" if item.body is None else "payload" for item in self.sent]


class _ScriptedHttpSession:
    """Socket-level stand-in for pypsrp's cached ``requests.Session``."""

    def __init__(self, script: _HttpScript) -> None:
        self.headers: dict[str, str] = {}
        self.auth = types.SimpleNamespace(contexts={"127.0.0.1": _FakeAuthContext()})
        self.closed = False
        self._script = script

    def prepare_request(self, request: Any) -> Any:
        return request.prepare()

    def send(self, prepared: Any, timeout: object = None) -> object:
        del timeout
        return self._script.respond(prepared)

    def close(self) -> None:
        self.closed = True


class _PypsrpProbeSession:
    """Probe session whose ``execute_ps`` drives pypsrp's real HTTP transport.

    ``Client`` / ``WSMan`` / ``_TransportHTTP`` are the real objects, so the
    bodyless authentication POST and the wrapped operation POST are produced by
    pypsrp itself and only the socket-level session is scripted. A re-handshake
    (:func:`resync_winrm_session` drops the cached session) builds a fresh
    scripted session from the same script, so one outcome list covers both
    attempts.
    """

    def __init__(self, script: _HttpScript, *, payload_sends: int = 1) -> None:
        from pypsrp.client import Client  # lazy: the doubles above need no pypsrp

        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []
        self.payload_sends = payload_sends
        self._client = Client(
            "127.0.0.1", username="u", password="p", ssl=False, auth="ntlm"
        )
        self.wsman = self._client.wsman
        self.transport = self.wsman.transport
        self.transport.session = _ScriptedHttpSession(script)
        self.transport._build_session = lambda: _ScriptedHttpSession(script)  # type: ignore[method-assign]

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
        # Real pypsrp: one bodyless authentication POST when no security
        # context is cached, then the operation's wrapped messages.
        for _ in range(self.payload_sends):
            self.transport.send(b"<envelope/>")
        return (CAPABILITY_STDOUT, None, False)


def _connector(session: object) -> Callable[..., object]:
    def connector(**_kwargs: object) -> object:
        return session

    return connector


def _transport(
    session: object,
    *,
    probe_timeout_s: float | None = None,
) -> WinRMTransport:
    return WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        probe_timeout_s=probe_timeout_s,
        connector=_connector(session),
    )


def _ep_transport() -> object:
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    return ep.transport


# ---------------------------------------------------------------------------
# install_winrm_round_trip_counter
# ---------------------------------------------------------------------------


class _SlottedTransport:
    """Transport that accepts ``_send_request`` but no extra attributes."""

    __slots__ = ("_send_request",)

    def __init__(self) -> None:
        self._send_request = lambda *args, **kwargs: b"ok"


def test_round_trip_counter_counts_successful_exchanges_only() -> None:
    """A raised exchange is not counted; a returned one is."""
    rejection = _rejection()
    transport = _FakeCountableTransport([rejection, b"ok", b"ok"])
    reader = install_winrm_round_trip_counter(_FakeWsman(transport))
    assert reader is not None
    assert reader() == 0
    with pytest.raises(type(rejection)):
        transport._send_request(object())
    assert reader() == 0, "a rejected exchange never completed"
    transport._send_request(object())
    transport._send_request(object())
    assert reader() == 2
    assert transport.attempts == 3


def test_round_trip_counter_ignores_pypsrp_bodyless_handshake() -> None:
    """pypsrp's authentication POST is not the operation's exchange.

    ``_TransportHTTP.send`` establishes the security context with a bodyless
    POST before the operation message; counting it made the operation's own
    first exchange look like a later one.
    """
    transport = _FakeCountableTransport([b"ok", b"ok", b"ok"])
    reader = install_winrm_round_trip_counter(_FakeWsman(transport))
    assert reader is not None
    url = "http://win.example:5985/wsman"
    transport._send_request(requests.Request("POST", url, data=None).prepare())
    assert reader() == 0, "the bodyless handshake carries no operation"
    transport._send_request(requests.Request("POST", url, data=b"<payload/>").prepare())
    assert reader() == 1
    # An opaque double is not a prepared request: it counts as an exchange.
    transport._send_request(object())
    assert reader() == 2


def test_round_trip_counter_is_idempotent_per_transport() -> None:
    """A second install returns the same reader instead of wrapping twice."""
    transport = _FakeCountableTransport([b"ok"])
    session = _FakeWsman(transport)
    first = install_winrm_round_trip_counter(session)
    second = install_winrm_round_trip_counter(session)
    assert first is not None and first is second
    transport._send_request(object())
    assert first() == 1, "the wrapped send must be installed exactly once"


def test_round_trip_counter_none_without_transport_or_send() -> None:
    """Nothing countable -> None, never a raise."""
    assert install_winrm_round_trip_counter(None) is None
    assert install_winrm_round_trip_counter(object()) is None
    # pypsrp-shaped state but no HTTP surface.
    assert install_winrm_round_trip_counter(_FakeWsman(_FakeStateTransport())) is None
    # ``_send_request`` present but not callable.
    node = _FakeWsman(object())
    node.transport = type("_T", (), {"_send_request": None})()
    assert install_winrm_round_trip_counter(node) is None


def test_round_trip_counter_none_when_wrapping_fails() -> None:
    """A transport that cannot hold the reader leaves the send untouched."""
    transport = _SlottedTransport()
    assert install_winrm_round_trip_counter(_FakeWsman(transport)) is None
    assert transport._send_request(object()) == b"ok"


def test_round_trip_counter_walks_the_installed_pypsrp_layout() -> None:
    """Guards the hand-written doubles: the real pypsrp chain is walkable."""
    pytest.importorskip("pypsrp")
    from pypsrp.client import Client

    client = Client("127.0.0.1", username="u", password="p", ssl=False, auth="ntlm")
    assert hasattr(client.wsman.transport, "_send_request")
    reader = install_winrm_round_trip_counter(AdaptedWinRMSession(client))
    assert reader is not None
    assert reader() == 0
    # Idempotent across adapted sessions: the transport object carries the reader.
    assert (
        install_winrm_round_trip_counter(AdaptedWinRMSession(client)) is reader
    )


# ---------------------------------------------------------------------------
# open probe: rejected first exchange self-heals
# ---------------------------------------------------------------------------


def test_probe_first_exchange_rejected_self_heals_open() -> None:
    """400 on the probe's first POST -> re-handshake, replay, open ok."""
    sess = _ProbeSession([_rejection(400), b"ok"])
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "ok", f"expected ok, got {r.status} {r.fields}"
    assert sess.transport.attempts >= 2, "probe must exchange again after resync"
    assert len(sess.scripts) == 2, "probe replay is exactly one extra attempt"
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.connected is True
    transport = _ep_transport()
    assert transport.meta.get("session_resynced") is True  # type: ignore[union-attr]
    assert transport.is_connected() is True  # type: ignore[union-attr]
    assert transport._round_trips is not None  # type: ignore[union-attr]
    assert transport._round_trips() == 1  # type: ignore[union-attr]
    # A real re-handshake: the stale encryption context was cleared and the
    # cached HTTP session closed before the replay.
    assert sess.transport.encryption is None
    assert sess.transport.session is None
    assert sess.transport.http_session.closed is True  # type: ignore[union-attr]
    assert sess.closed is False


def test_probe_gateway_502_also_self_heals() -> None:
    """A bodyless 502 heals like the 400: the probe's first exchange is structural.

    The probe's own exchanges carry no user work, so the replay does not depend
    on the refusal discriminator that governs the pooled-invoke path.
    """
    sess = _ProbeSession([_rejection(502), b"ok"])
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "ok", f"expected ok, got {r.status} {r.fields}"
    assert sess.transport.attempts == 2


def test_probe_heals_after_pypsrp_handshake_post() -> None:
    """A cold link: pypsrp's bodyless handshake succeeds, then the probe's POST
    is rejected as a 502.

    The handshake is not the probe's exchange, so this must still self-heal
    (open ok, exactly one replay) instead of hard-failing the endpoint on the
    coldest possible link.
    """
    pytest.importorskip("pypsrp")
    script = _HttpScript(
        [
            _FakeHttpResponse(),
            _rejection(502),
            _FakeHttpResponse(),
            _FakeHttpResponse(),
        ]
    )
    sess = _PypsrpProbeSession(script)
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "ok", f"expected ok, got {r.status} {r.fields}"
    assert script.shapes() == ["bodyless", "payload", "bodyless", "payload"]
    assert len(sess.scripts) == 2, "probe replay is exactly one extra attempt"
    transport = _ep_transport()
    assert transport.meta.get("session_resynced") is True  # type: ignore[union-attr]
    assert transport._round_trips is not None  # type: ignore[union-attr]
    assert transport._round_trips() == 1  # type: ignore[union-attr]
    assert sess.closed is False


def test_probe_heals_when_the_handshake_post_is_rejected() -> None:
    """The measured 502 can land on the bodyless authentication POST itself."""
    pytest.importorskip("pypsrp")
    script = _HttpScript(
        [_rejection(502), _FakeHttpResponse(), _FakeHttpResponse()]
    )
    sess = _PypsrpProbeSession(script)
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "ok", f"expected ok, got {r.status} {r.fields}"
    assert script.shapes() == ["bodyless", "bodyless", "payload"]
    assert len(sess.scripts) == 2, "probe replay is exactly one extra attempt"
    transport = _ep_transport()
    assert transport.meta.get("session_resynced") is True  # type: ignore[union-attr]
    assert transport._round_trips() == 1  # type: ignore[union-attr]


def test_probe_mid_exchange_pypsrp_rejection_is_not_replayed() -> None:
    """A payload exchange that completed before the rejection still blocks it.

    Here the handshake and the probe's POST both succeed, and the *next*
    exchange is rejected: the probe is past its first request, so nothing
    may be replayed.
    """
    pytest.importorskip("pypsrp")
    script = _HttpScript(
        [
            _FakeHttpResponse(),
            _FakeHttpResponse(),
            _rejection(400),
            _FakeHttpResponse(),
        ]
    )
    sess = _PypsrpProbeSession(script, payload_sends=2)
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert len(sess.scripts) == 1, "no replay after a completed payload exchange"
    assert script.shapes() == ["bodyless", "payload", "payload"]
    assert sess.closed is True


def test_probe_replay_failure_hard_fails_and_disposes() -> None:
    """A replay that is rejected too still hard-fails identity and disposes."""
    sess = _ProbeSession([_rejection()])
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    assert "400" in (r.fields.get("msg") or ""), r.fields
    assert len(sess.scripts) == 2, "one replay, no more"
    assert sess.transport.attempts == 2
    assert closes["n"] >= 1
    assert sess.closed is True
    assert get_registry().get("lab-win") is None


def test_probe_read_timeout_is_not_retried_and_keeps_encryption() -> None:
    """A read timeout may have run remotely: no replay, no re-handshake."""
    sess = _ProbeSession([requests_exceptions.ReadTimeout("read timed out")])
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert len(sess.scripts) == 1, "a fatal link failure is never replayed"
    assert sess.transport.attempts == 1
    assert sess.transport.encryption == "auto", "resync must not run"
    assert sess.transport.http_session.closed is False  # type: ignore[union-attr]
    assert sess.closed is True


def test_probe_rejection_after_completed_exchange_is_not_replayed() -> None:
    """A rejected second exchange cannot be the probe's first request."""
    sess = _ProbeSession([b"ok", _rejection()], exchanges_per_call=2)
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert sess.transport.attempts == 2, "no replay after a completed exchange"
    assert len(sess.scripts) == 1
    assert sess.transport.encryption == "auto", "resync must not run"


def test_probe_without_countable_transport_keeps_wave_y_replay() -> None:
    """A double with no HTTP surface keeps the historical single replay."""
    sess = _UncountableProbeSession()
    t = _transport(sess)
    t.connect()
    assert t._round_trips is None
    data = t.collect_probe(mode="full")
    assert data.get("status") == "ok", data
    assert len(sess.scripts) == 2, "replayed exactly once"
    assert t.meta.get("session_resynced") is True
    assert sess.transport.encryption is None
    assert sess.transport.session is None
    assert sess.transport.http_session.closed is True  # type: ignore[union-attr]


def test_probe_replay_shares_the_probe_budget() -> None:
    """The replay gets the first attempt's remaining budget, not a fresh one.

    The first attempt spends a quarter of the budget before it is rejected, so a
    replay handed a *fresh* budget would end at ``budget + first_delay``; only
    sharing the remaining budget keeps the whole probe near ``budget``.
    """
    budget = 0.6
    first_delay = 0.25
    sess = _ReplayBlocksSession(first_delay)
    t = _transport(sess, probe_timeout_s=budget)
    t.connect()
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            t._execute_ps_stdout(t.session, "Get-Date")
        elapsed = time.monotonic() - t0
    finally:
        sess.block.set()
    assert len(sess.scripts) == 2, "the replay happened"
    assert elapsed >= first_delay * 0.9, f"first attempt did not spend budget: {elapsed}s"
    assert elapsed < budget + first_delay, f"replay got a fresh budget: {elapsed}s"


# ---------------------------------------------------------------------------
# exact replay guard on exec / runspace
# ---------------------------------------------------------------------------


def test_exec_mid_operation_rejection_is_not_replayed_and_marks_dead() -> None:
    """One exchange done, then a 400: the op may have run, so never replay."""
    sess = _ProbeSession([b"ok", _rejection()], exchanges_per_call=2)
    t = _transport(sess)
    t.connect()
    with pytest.raises(TransportError) as ei:
        t.run_command("Add-Content -Path C:\\x -Value 1")
    assert ei.value.code == "EXEC_FAILED"
    assert len(sess.scripts) == 1, "no replay after a completed exchange"
    assert sess.transport.attempts == 2
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "link lost"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.meta.get("session_resynced") is None
    assert t.session is None
    assert sess.closed is True
    # Refused replays do not re-handshake either.
    assert sess.transport.encryption == "auto"


def test_exec_first_exchange_rejection_is_still_replayed() -> None:
    """The guard must not block the stale-framing self-heal it was built for."""
    sess = _ProbeSession([_rejection(), b"ok"])
    t = _transport(sess)
    t.connect()
    r = t.run_command("Get-Date")
    assert isinstance(r, ExecResult)
    assert r.exit_code == 0
    assert len(sess.scripts) == 2, "replayed exactly once"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True


def test_runspace_invoke_refusal_self_heals_on_an_observable_link() -> None:
    """An empty-body refusal of the invoke's first exchange heals the invoke.

    A pooled invoke sends the script in its very first WSMan message, so the
    round-trip counter cannot clear the replay on its own. An empty-body
    rejection does: the framing layer refused the request before any pipeline
    existed, so the command provably never ran and the historical single replay
    is safe again.
    """
    sess = _ProbeSession([_rejection(), b"ok"])
    t = _transport(sess)
    t.connect()
    handle = _ExchangeInvokeHandle(sess.transport, exchanges=1)
    r = t.runspace_invoke(handle, "Add-Content -Path C:\\x -Value 1")
    assert isinstance(r, RunspaceResult)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert handle.invokes == [
        "Add-Content -Path C:\\x -Value 1",
        "Add-Content -Path C:\\x -Value 1",
    ], "replayed exactly once"
    assert sess.transport.attempts == 2
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True
    # A real re-handshake: the stale encryption context was cleared and the
    # cached HTTP session closed before the replay.
    assert sess.transport.encryption is None
    assert sess.transport.http_session.closed is True  # type: ignore[union-attr]
    assert sess.closed is False


def test_runspace_invoke_body_carrying_rejection_is_not_replayed() -> None:
    """A rejection carrying a body may have been delivered: never replay.

    The intermediary answered with its own error page, so the request may
    already have reached the server and its pipeline run. The observable link
    therefore refuses the replay and drops the poisoned session.
    """
    sess = _ProbeSession([_body_rejection(), b"ok"])
    t = _transport(sess)
    t.connect()
    handle = _ExchangeInvokeHandle(sess.transport, exchanges=1)
    with pytest.raises(TransportError) as ei:
        t.runspace_invoke(handle, "Add-Content -Path C:\\x -Value 1")
    assert ei.value.code == "EXEC_FAILED"
    assert handle.invokes == ["Add-Content -Path C:\\x -Value 1"], "no replay"
    assert sess.transport.attempts == 1
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "link lost"
    assert t.meta.get("link_lost") is True
    assert t.meta.get("session_resynced") is None
    assert t.session is None
    assert sess.closed is True
    assert sess.transport.encryption == "auto", "refused replay does not re-handshake"


def test_runspace_invoke_without_a_counter_keeps_wave_y_replay() -> None:
    """An unobservable link has no counter, so the historical replay stands."""
    sess = _UncountableProbeSession()
    t = _transport(sess)
    t.connect()
    assert t._round_trips is None
    handle = _ExchangeInvokeHandle(sess.transport, exchanges=0, fail_times=1)
    r = t.runspace_invoke(handle, "Get-Date")
    assert r.exit_code == 0
    assert handle.invokes == ["Get-Date", "Get-Date"], "replayed exactly once"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True


def test_runspace_mid_operation_rejection_is_not_replayed() -> None:
    """An invoke that already exchanged is never replayed; link goes dead."""
    sess = _ProbeSession([b"ok", _rejection()], exchanges_per_call=2)
    t = _transport(sess)
    t.connect()
    handle = _ExchangeInvokeHandle(sess.transport, exchanges=2)
    with pytest.raises(TransportError) as ei:
        t.runspace_invoke(handle, "Get-Date")
    assert ei.value.code == "EXEC_FAILED"
    assert handle.invokes == ["Get-Date"], "no replay after a completed exchange"
    assert sess.transport.attempts == 2
    assert t.is_connected() is False
    assert t.meta.get("link_lost") is True
    assert t.meta.get("session_resynced") is None
    assert t.session is None
    assert sess.closed is True
