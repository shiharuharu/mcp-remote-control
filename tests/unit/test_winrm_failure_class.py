"""Unit tests: WinRM link failure classification and session resync primitive."""

from __future__ import annotations

import concurrent.futures
import http.client
import socket
from typing import Any

import pytest
import requests

from mcp_remote_control.transport.winrm_exec import (
    _is_timeout_exc,
    classify_winrm_failure,
    is_winrm_refusal,
)
from mcp_remote_control.transport.winrm_session import (
    AdaptedWinRMSession,
    resync_winrm_session,
)

try:  # pypsrp is an optional extra; doubles below cover its absence.
    from pypsrp import exceptions as pypsrp_exceptions
except Exception:  # noqa: BLE001 - importability probe
    pypsrp_exceptions = None


def _double(module: str, qualname: str) -> type[Exception]:
    """Exception type carrying *module*/*qualname* so name matching can see it.

    Classification matches on the type's own module + qualname rather than
    importing pypsrp/requests, so a double describes a pypsrp failure exactly
    as the real class would in an environment where pypsrp is not installed.
    """
    return type(qualname, (Exception,), {"__module__": module})


def _transport_error(*args: Any) -> BaseException:
    """A pypsrp ``WinRMTransportError`` (real when importable, else a double)."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError(*args)
    return _double("pypsrp.exceptions", "WinRMTransportError")(*args)


# ---------------------------------------------------------------------------
# classify_winrm_failure - budget_timeout
# ---------------------------------------------------------------------------


def test_builtin_timeout_error_is_budget_timeout() -> None:
    assert classify_winrm_failure(TimeoutError("AsyncLoopBridge.run timed out after 5.0s")) == (
        "budget_timeout"
    )


def test_futures_timeout_error_follows_builtin_alias() -> None:
    assert concurrent.futures.TimeoutError is TimeoutError
    assert classify_winrm_failure(concurrent.futures.TimeoutError()) == "budget_timeout"


def test_socket_timeout_follows_builtin_alias() -> None:
    # CPython aliases socket.timeout to the builtin TimeoutError, so a bare
    # socket timeout is indistinguishable from the bridge budget expiry. The
    # socket timeouts that actually reach the WinRM path are wrapped by
    # requests as ReadTimeout / ConnectTimeout and are classified fatal above.
    expected = "budget_timeout" if socket.timeout is TimeoutError else "link_fatal"
    assert classify_winrm_failure(socket.timeout("timed out")) == expected


def test_classification_ignores_exception_text() -> None:
    # _is_timeout_exc matches on text; classification must not inherit that
    # bug, or any "timed out" message becomes a bridge budget expiry.
    assert _is_timeout_exc(ValueError("operation timed out")) is True
    assert classify_winrm_failure(ValueError("operation timed out")) == "other"


# ---------------------------------------------------------------------------
# classify_winrm_failure - link_retryable
# ---------------------------------------------------------------------------


def test_pypsrp_transport_error_400_empty_body_is_retryable() -> None:
    # The stale-encryption shape: HTTP 400 with an empty body, no __cause__.
    exc = (
        pypsrp_exceptions.WinRMTransportError("http", 400, "")
        if pypsrp_exceptions is not None
        else _double("pypsrp.exceptions", "WinRMTransportError")("http", 400, "")
    )
    assert exc.__cause__ is None
    assert classify_winrm_failure(exc) == "link_retryable"


def test_pypsrp_transport_error_double_matches_real_class() -> None:
    double = _double("pypsrp.exceptions", "WinRMTransportError")
    assert classify_winrm_failure(double("http", 400, "")) == "link_retryable"


def test_pypsrp_wsman_fault_error_is_not_retryable() -> None:
    # A SOAP fault is an answer from a reachable server: it must not mark the
    # session dead, and one of its codes is the operation timeout, which the
    # pipeline may already have run through.
    exc = (
        pypsrp_exceptions.WSManFaultError("s:Sender", "machine", "reason", "provider", "path", None)
        if pypsrp_exceptions is not None
        else _double("pypsrp.exceptions", "WSManFaultError")()
    )
    assert classify_winrm_failure(exc) == "other"


def test_pypsrp_subclass_classified_like_its_base() -> None:
    base = _double("pypsrp.exceptions", "WinRMTransportError")
    subclass = type("MyTransportError", (base,), {"__module__": "tests.doubles"})
    assert classify_winrm_failure(subclass("http", 500, "boom")) == "link_retryable"


# ---------------------------------------------------------------------------
# is_winrm_refusal - an empty rejection body proves nothing was dispatched
# ---------------------------------------------------------------------------


def test_empty_body_rejection_is_a_refusal() -> None:
    # The measured M1 shape: HTTP 400 with no content at all.
    assert is_winrm_refusal(_transport_error("http", 400, "")) is True


def test_https_and_4xx_bodyless_refusals_are_interchangeable() -> None:
    # Frame either over http or https, and let the receiver pick any 4xx: the
    # refusal is what matters, not the code or the scheme that carried it.
    assert is_winrm_refusal(_transport_error("https", 400, "")) is True
    assert is_winrm_refusal(_transport_error("http", 401, "")) is True
    assert is_winrm_refusal(_transport_error("http", 403, "")) is True


def test_bodyless_5xx_is_not_a_refusal() -> None:
    # A 5xx means something failed while *handling* the request, so it may
    # already have run. A gateway that strips its own error page to an empty
    # body must not license a replay - on the pooled-invoke path the rejected
    # request is the one carrying the user's script.
    assert is_winrm_refusal(_transport_error("http", 502, "")) is False
    assert is_winrm_refusal(_transport_error("http", 500, "")) is False
    assert is_winrm_refusal(_transport_error("http", 504, "")) is False


def test_whitespace_only_body_is_a_refusal() -> None:
    assert is_winrm_refusal(_transport_error("http", 400, "  \r\n\t ")) is True


def test_body_carrying_rejection_is_not_a_refusal() -> None:
    # A front gateway's own error page: an intermediary answered, so whether it
    # forwarded the request first is unknown.
    exc = _transport_error("http", 502, "Connection error: read ETIMEDOUT")
    assert is_winrm_refusal(exc) is False


def test_subclass_of_winrm_transport_error_is_a_refusal() -> None:
    base = _double("pypsrp.exceptions", "WinRMTransportError")
    subclass = type("MyTransportError", (base,), {"__module__": "tests.doubles"})
    assert is_winrm_refusal(subclass("http", 400, "")) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom"),
        RuntimeError("boom"),
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.ReadTimeout("read timed out"),
        _double("pypsrp.exceptions", "WSManFaultError")("s:Sender", "machine"),
        _double("builtins", "ConnectionResetError")("reset"),
    ],
)
def test_non_winrm_transport_errors_are_never_refusals(exc: BaseException) -> None:
    assert is_winrm_refusal(exc) is False


@pytest.mark.parametrize(
    "args",
    [
        (),  # no args at all
        ("http",),  # truncated
        ("http", 400),  # no body slot
        ("ftp", 400, ""),  # protocol is not http/https
        (400, 400, ""),  # protocol is not the scheme
        ("http", "400", ""),  # status is not an int
        ("http", None, ""),  # status missing
        ("http", 400, None),  # body unknown, so nothing is proven
        ("http", 400, 0),  # body of an unexpected type
        ("http", 400, "", None),  # longer than the observed triple
        ("http", 502, "", object()),  # extra slots do not prove the body slot
    ],
)
def test_malformed_args_are_never_refusals(args: tuple[Any, ...]) -> None:
    assert is_winrm_refusal(_transport_error(*args)) is False


def test_over_long_args_is_a_shape_mismatch_not_a_refusal() -> None:
    # pypsrp raises exactly ("http", <status>, <body>). A fork or a double that
    # appends a fourth element (a response object, a context) no longer
    # matches the shape the empty-body proof was established on, so the extra
    # slots must not be read as "the third one is empty, therefore refused".
    exc = _transport_error("http", 502, "", object())
    assert len(exc.args) == 4
    assert is_winrm_refusal(exc) is False


def test_empty_bytes_body_is_a_refusal() -> None:
    # pypsrp passes ``response.text``, but a byte-level double is accepted.
    assert is_winrm_refusal(_transport_error("http", 400, b"")) is True


def test_refusal_never_raises_on_hostile_args() -> None:
    class Hostile(BaseException):
        __module__ = "pypsrp.exceptions"
        __qualname__ = "WinRMTransportError"

        @property
        def args(self) -> Any:
            raise RuntimeError("boom")

    assert is_winrm_refusal(Hostile()) is False


# Connection-level failures say nothing about whether the request executed:
# requests raises the same ConnectionError for a socket that died before the
# request was sent and for one that died after the command ran. They are fatal
# so a replay can never repeat a side effect.
@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("refused"),
        _double("requests.exceptions", "ProtocolError")("dropped"),
        http.client.RemoteDisconnected("closed"),
        ConnectionResetError("reset by peer"),
        BrokenPipeError("broken pipe"),
        ConnectionRefusedError("refused"),
        ConnectionAbortedError("aborted"),
    ],
)
def test_connection_level_failures_are_never_retryable(exc: BaseException) -> None:
    assert classify_winrm_failure(exc) == "link_fatal"


# The remaining _LINK_FATAL_TYPES entries: requests' wrapper for a failed
# exchange, and urllib3's own raise / retry-exhaustion exceptions. A
# MaxRetryError means the POST was attempted and its outcome is unknown, so
# these are fatal for the same reason as the connection-level set above.
@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("refused"),
        _double("requests.exceptions", "ProtocolError")("dropped"),
        _double("urllib3.exceptions", "ProtocolError")("bad status line"),
        _double("urllib3.exceptions", "NewConnectionError")("no route"),
        _double("urllib3.exceptions", "ReadTimeoutError")("read timed out"),
        _double("urllib3.exceptions", "TimeoutError")("pool timed out"),
        _double("urllib3.exceptions", "MaxRetryError")("too many retries"),
    ],
)
def test_link_fatal_table_entries_are_fatal(exc: BaseException) -> None:
    assert classify_winrm_failure(exc) == "link_fatal"


def test_real_urllib3_failures_match_the_type_table() -> None:
    # Guards the doubles above: classification matches on module + qualname, so
    # a rename or relocation inside urllib3 would otherwise silently drop these
    # failures out of link_fatal.
    urllib3_exceptions = pytest.importorskip("urllib3.exceptions")
    assert (
        classify_winrm_failure(urllib3_exceptions.ProtocolError("bad status line"))
        == "link_fatal"
    )
    assert (
        classify_winrm_failure(urllib3_exceptions.NewConnectionError(None, "no route"))
        == "link_fatal"
    )
    assert (
        classify_winrm_failure(
            urllib3_exceptions.ReadTimeoutError(None, "http://host", "read timed out")
        )
        == "link_fatal"
    )
    assert (
        classify_winrm_failure(urllib3_exceptions.TimeoutError("pool timed out"))
        == "link_fatal"
    )
    assert (
        classify_winrm_failure(
            urllib3_exceptions.MaxRetryError(None, "http://host", "too many retries")
        )
        == "link_fatal"
    )


def test_urllib3_timeout_error_is_not_the_builtin_alias() -> None:
    # urllib3.exceptions.TimeoutError shares the *name* of the builtin alias
    # but is an unrelated class, so it is matched by the type table rather than
    # read as a bridge budget expiry.
    urllib3_exceptions = pytest.importorskip("urllib3.exceptions")
    assert urllib3_exceptions.TimeoutError is not TimeoutError
    assert not isinstance(urllib3_exceptions.TimeoutError("pool timed out"), TimeoutError)


def test_socket_timeout_alias_is_pinned_to_budget_timeout() -> None:
    # CPython >= 3.10 aliases socket.timeout to the builtin TimeoutError, which
    # is itself an OSError subclass, so a bare socket timeout also matches the
    # OSError entry of _LINK_FATAL_TYPES. Only the explicit TimeoutError check
    # ahead of the table lookup keeps it in budget_timeout; the socket timeouts
    # that actually reach the WinRM path are wrapped by requests, and both
    # wrappers classify fatal.
    assert socket.timeout is TimeoutError
    assert issubclass(TimeoutError, OSError)
    assert classify_winrm_failure(socket.timeout("timed out")) == "budget_timeout"
    read_timeout = requests.exceptions.ReadTimeout("read timed out")
    connect_timeout = requests.exceptions.ConnectTimeout("connect timed out")
    assert classify_winrm_failure(read_timeout) == "link_fatal"
    assert classify_winrm_failure(connect_timeout) == "link_fatal"


# ---------------------------------------------------------------------------
# classify_winrm_failure - link_fatal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectTimeout("connect timed out"),
    ],
)
def test_requests_timeouts_are_link_fatal(exc: BaseException) -> None:
    assert classify_winrm_failure(exc) == "link_fatal"


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectTimeout("connect timed out"),
    ],
)
def test_requests_timeouts_are_never_budget_timeout(exc: BaseException) -> None:
    assert classify_winrm_failure(exc) != "budget_timeout"


def test_connect_timeout_is_not_retryable_despite_connection_error_base() -> None:
    # ConnectTimeout also derives from requests ConnectionError; it must be
    # classified by the timeout branch, since the request may have reached
    # the server before the response was lost.
    exc = requests.exceptions.ConnectTimeout("connect timed out")
    assert isinstance(exc, requests.exceptions.ConnectionError)
    assert classify_winrm_failure(exc) == "link_fatal"


def test_bare_oserror_is_fatal() -> None:
    assert classify_winrm_failure(OSError("network unreachable")) == "link_fatal"


def test_oserror_subclass_from_foreign_module_is_fatal() -> None:
    # Subclasses are matched through their MRO, so a driver-specific OSError
    # still lands in link_fatal rather than falling through to "other".
    exc = type("DriverError", (OSError,), {"__module__": "winrm.driver"})("network down")
    assert classify_winrm_failure(exc) == "link_fatal"


# ---------------------------------------------------------------------------
# classify_winrm_failure - other
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad argument"),
        RuntimeError("unexpected"),
        KeyError("missing"),
        ZeroDivisionError(),
        NotImplementedError("unsupported"),
    ],
)
def test_unrelated_exceptions_are_other(exc: BaseException) -> None:
    assert classify_winrm_failure(exc) == "other"


def test_transport_error_is_other() -> None:
    from mcp_remote_control.transport.base import TransportError

    assert classify_winrm_failure(TransportError("EXEC_FAILED", "boom")) == "other"


# ---------------------------------------------------------------------------
# _is_timeout_exc - legacy semantics must not change
# ---------------------------------------------------------------------------


def test_is_timeout_exc_legacy_semantics() -> None:
    class FakeTimeout(Exception):
        pass

    assert _is_timeout_exc(TimeoutError("budget")) is True
    assert _is_timeout_exc(FakeTimeout("nope")) is True  # class name carries "timeout"
    assert _is_timeout_exc(RuntimeError("operation timed out")) is True  # text carries it
    assert _is_timeout_exc(requests.exceptions.ReadTimeout("x")) is True
    assert _is_timeout_exc(ValueError("bad")) is False
    assert _is_timeout_exc(OSError("network unreachable")) is False
    assert _is_timeout_exc(ConnectionResetError("reset")) is False


# ---------------------------------------------------------------------------
# resync_winrm_session
# ---------------------------------------------------------------------------


class _FakeRequestsSession:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeTransport:
    """pypsrp ``_TransportHTTP`` shape: caches session + encryption context."""

    def __init__(self) -> None:
        self.session: Any = _FakeRequestsSession()
        self.encryption: Any = object()


class _FakeWSMan:
    def __init__(self) -> None:
        self.transport = _FakeTransport()


class _FakeClient:
    """pypsrp ``Client`` shape: owns a ``wsman``."""

    def __init__(self) -> None:
        self.wsman = _FakeWSMan()


class _FakeClientAdapter:
    """Session wrapper holding its Client on ``_client``, a link the walk follows."""

    def __init__(self) -> None:
        self._client = _FakeClient()
        self.wsman = self._client.wsman


def _fake_chain() -> tuple[_FakeClientAdapter, _FakeTransport]:
    adapter = _FakeClientAdapter()
    return adapter, adapter._client.wsman.transport


def test_resync_clears_pypsrp_transport_state() -> None:
    adapter, transport = _fake_chain()
    old_session = transport.session

    assert resync_winrm_session(adapter) is True

    assert transport.encryption is None
    assert transport.session is None
    assert old_session.close_calls == 1


def test_resync_through_adapted_session() -> None:
    adapter, transport = _fake_chain()
    session = AdaptedWinRMSession(adapter)

    assert resync_winrm_session(session) is True
    assert transport.encryption is None
    assert transport.session is None


def test_resync_walks_the_installed_pypsrp_layout() -> None:
    # Guards the hand-written doubles above: if pypsrp moves the cached
    # encryption/session state, production resync would silently no-op.
    pytest.importorskip("pypsrp")
    from pypsrp.client import Client

    client = Client("127.0.0.1", username="u", password="p", ssl=False, auth="ntlm")
    transport = client.wsman.transport
    transport.encryption = object()
    transport.session = requests.Session()

    assert resync_winrm_session(AdaptedWinRMSession(client)) is True
    assert transport.encryption is None
    assert transport.session is None
    assert resync_winrm_session(AdaptedWinRMSession(client)) is False


def test_resync_second_call_is_a_no_op() -> None:
    adapter, transport = _fake_chain()
    assert resync_winrm_session(adapter) is True
    assert resync_winrm_session(adapter) is False
    assert transport.encryption is None
    assert transport.session is None


def test_resync_handles_pypsrp_shaped_root_without_adapters() -> None:
    client = _FakeClient()
    assert resync_winrm_session(client) is True
    assert client.wsman.transport.encryption is None
    assert client.wsman.transport.session is None


def test_resync_session_without_wsman_returns_false() -> None:
    class NoWSMan:
        cwd = r"C:\Users\Admin"

        def execute_ps(self, script: str, *, environment: Any = None) -> Any:
            raise AssertionError("not called")

    assert resync_winrm_session(NoWSMan()) is False


def test_resync_none_returns_false() -> None:
    assert resync_winrm_session(None) is False


def test_resync_fresh_transport_returns_false() -> None:
    client = _FakeClient()
    client.wsman.transport.encryption = None
    client.wsman.transport.session = None
    assert resync_winrm_session(client) is False


def test_resync_clears_duck_typed_state_without_pypsrp() -> None:
    class DuckTyped:
        raw = "not-a-client"
        wsman = 42
        session = "not-a-pool"

    obj = DuckTyped()
    # ``session`` is a str without close(); clearing it must stay silent and
    # still be reported as a successful reset.
    assert resync_winrm_session(obj) is True
    assert obj.session is None
    assert obj.wsman == 42


def test_resync_never_raises_on_hostile_attributes() -> None:
    class Hostile:
        @property
        def raw(self) -> Any:
            raise RuntimeError("boom")

        @property
        def wsman(self) -> Any:
            raise RuntimeError("boom")

        @property
        def session(self) -> Any:
            raise RuntimeError("boom")

        @property
        def encryption(self) -> Any:
            raise RuntimeError("boom")

    assert resync_winrm_session(Hostile()) is False


def test_resync_never_raises_on_read_only_state() -> None:
    class ReadOnly:
        @property
        def session(self) -> Any:
            return _FakeRequestsSession()

        @property
        def encryption(self) -> Any:
            return object()

    assert resync_winrm_session(ReadOnly()) is False


def test_resync_never_raises_on_failing_closer() -> None:
    class BadPool:
        def close(self) -> None:
            raise RuntimeError("close failed")

    class Client:
        def __init__(self) -> None:
            self.transport = type("T", (), {"session": BadPool(), "encryption": None})()

    client = Client()
    assert resync_winrm_session(client) is True
    assert client.transport.session is None


def test_resync_terminates_on_self_referential_chain() -> None:
    class Loop:
        def __init__(self) -> None:
            self.raw = self
            self.wsman = self
            self.transport = self
            self.encryption = object()
            self.session = object()

    node = Loop()
    assert resync_winrm_session(node) is True
    assert node.encryption is None
    assert node.session is None
