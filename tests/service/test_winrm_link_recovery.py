"""Service tests: WinRM link self-heal, single retry, honest link death.

Plain-HTTP WinRM with message encryption (``encryption=auto``) invalidates
pypsrp's message-encryption context after a few idle seconds; every later
request is rejected with an empty-body HTTP 400 (a request that provably never
executed remotely). The transport must recognize that shape, re-handshake in
place, and retry exactly once - and when the link really is gone, report it
honestly and let the endpoint reconnect instead of staying "connected" but
permanently unusable.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import requests.exceptions as requests_exceptions

from mcp_remote_control.core import exec_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.transport import winrm_files
from mcp_remote_control.transport.base import ExecResult, TransportError
from mcp_remote_control.transport.winrm import WinRMTransport, _EXIT_MARKER
from mcp_remote_control.transport.winrm_runspace import RunspaceResult

try:  # pypsrp is a hard dependency; the fallback keeps the shape testable.
    from pypsrp import exceptions as pypsrp_exceptions
except ImportError:  # pragma: no cover - exercised only without pypsrp
    pypsrp_exceptions = None  # type: ignore[assignment]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


def _double(module: str, qualname: str) -> type[BaseException]:
    """Build an exception type the classifier matches by (module, qualname)."""
    return type(qualname, (Exception,), {"__module__": module, "__qualname__": qualname})


def _stale_encryption_error() -> BaseException:
    """The measured stale-encryption shape: HTTP 400, empty body, no cause."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", 400, "")
    return _double("pypsrp.exceptions", "WinRMTransportError")("http", 400, "")


def _gateway_error() -> BaseException:
    """A front gateway's own error page: HTTP 502 with a body."""
    body = "Connection error: read ETIMEDOUT"
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", 502, body)
    return _double("pypsrp.exceptions", "WinRMTransportError")("http", 502, body)


# ---------------------------------------------------------------------------
# Injectable pypsrp-shaped sessions (no network sockets)
# ---------------------------------------------------------------------------


class _FakeHttpSession:
    """Stand-in for the cached ``requests.Session`` pypsrp drops on resync."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePypsrpTransport:
    """Node holding the cached encryption context resync is expected to clear."""

    def __init__(self, session: _FakeHttpSession) -> None:
        self.encryption = "auto"
        self.session = session


class _FakeWsman:
    def __init__(self, transport: _FakePypsrpTransport) -> None:
        self.transport = transport


class _LinkFailureSession:
    """Session whose first *fail_times* exec calls raise, then succeed.

    The pypsrp-shaped ``wsman.transport`` node makes a real re-handshake
    observable: :func:`resync_winrm_session` clears ``encryption`` and closes
    the cached HTTP session.
    """

    def __init__(
        self,
        *,
        error: BaseException,
        fail_times: int = 1,
        stdout: str = "winrm-out:ok\n",
    ) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.calls: list[str] = []
        self._error = error
        self._fail_times = fail_times
        self._stdout = stdout
        self._http_session = _FakeHttpSession()
        self.transport = _FakePypsrpTransport(self._http_session)
        self.wsman = _FakeWsman(self.transport)

    def close(self) -> None:
        self.closed = True

    def _attempt(self, payload: str) -> None:
        self.calls.append(payload)
        if len(self.calls) <= self._fail_times:
            raise self._error

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout_s, env
        self._attempt(command)
        return ExecResult(
            exit_code=0,
            stdout=self._stdout,
            stderr="",
            cwd=cwd or self.cwd,
        )


class _OneshotLinkFailureSession:
    """No high-level exec surface: oneshot ``execute_ps`` carries the failure."""

    def __init__(self, *, error: BaseException, fail_times: int = 1) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.scripts: list[str] = []
        self._error = error
        self._fail_times = fail_times
        self.wsman = None

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
            raise self._error
        # The oneshot exec path appends the exit probe, so a real payload
        # carries its marker.
        return (f"oneshot-ok\n{_EXIT_MARKER}0\n", None, False)


class _RetryTimeoutSession:
    """First call rejected as retryable; the retry then burns the budget.

    Isolates the retry's failure class: the replay hangs instead of being
    rejected, so the retry failure must keep the hard-timeout contract.
    """

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.calls: list[str] = []
        self.block = threading.Event()
        self.transport = _FakePypsrpTransport(_FakeHttpSession())
        self.wsman = _FakeWsman(self.transport)

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout_s, env
        self.calls.append(command)
        if len(self.calls) == 1:
            raise _stale_encryption_error()
        self.block.wait(timeout=30.0)
        return ExecResult(exit_code=0, stdout="", stderr="", cwd=cwd or self.cwd)


class _BlockingPsSession:
    """oneshot ``execute_ps`` that blocks to burn the caller's wall-clock."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.block = threading.Event()
        self.wsman = None

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del script, environment
        self.block.wait(timeout=30.0)
        return ("", None, False)


class _ScriptedInvokeHandle:
    """Runspace handle whose first *fail_times* invokes raise, then succeed."""

    def __init__(self, *, error: BaseException, fail_times: int = 1) -> None:
        self.location = None
        self.invokes: list[str] = []
        self._error = error
        self._fail_times = fail_times

    def invoke(self, script: str) -> object:
        self.invokes.append(script)
        if len(self.invokes) <= self._fail_times:
            raise self._error
        return ("ps-out\n", None)


class _CountableHttpTransport:
    """pypsrp-shaped HTTP transport exposing a countable ``_send_request``.

    The round-trip counter is what lets the transport tell an operation's first
    payload exchange from a later one; a session built on this node is an
    *observable* link, unlike :class:`_LinkFailureSession`.
    """

    def __init__(self) -> None:
        self.encryption: object = "auto"
        self.session: object = _FakeHttpSession()
        # Stable handle for assertions: resync clears ``session`` but the
        # object it closed is still observable here.
        self.http_session: object = self.session
        self.attempts = 0

    def _send_request(self, request: object, timeout: object = None) -> bytes:
        del request, timeout
        self.attempts += 1
        return b"ok"


class _CountableLinkSession:
    """Session whose pypsrp HTTP transport makes the link observable."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.transport = _CountableHttpTransport()
        self.wsman = _FakeWsman(self.transport)

    def close(self) -> None:
        self.closed = True


class _CountableInvokeHandle:
    """Handle whose invoke completes *exchanges* then raises for *fail_times*.

    ``exchanges`` counts successful HTTP exchanges only, so 0 places the
    rejection on the invoke's first payload exchange while 1 places it after a
    completed one.
    """

    def __init__(
        self,
        transport: object,
        *,
        error: BaseException,
        fail_times: int = 1,
        exchanges: int = 0,
    ) -> None:
        self.location = None
        self.invokes: list[str] = []
        self._transport = transport
        self._error = error
        self._fail_times = fail_times
        self._exchanges = exchanges

    def invoke(self, script: str) -> object:
        self.invokes.append(script)
        if len(self.invokes) <= self._fail_times:
            for _ in range(self._exchanges):
                self._transport._send_request(object())
            raise self._error
        return ("ps-out\n", None)


def _connector(
    session: object,
) -> Callable[..., object]:
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


# ---------------------------------------------------------------------------
# connect_kwargs / probe budget (constructor wiring)
# ---------------------------------------------------------------------------


def test_connect_kwargs_reconnect_pair_wiring() -> None:
    """Reconnect defaults apply when unset; an explicit opt-out sends nothing."""
    t = _transport(_LinkFailureSession(error=_stale_encryption_error()))
    kwargs = t.connect_kwargs()
    assert kwargs["reconnection_retries"] == 2
    assert kwargs["reconnection_backoff"] == 0.5

    opt_out = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        reconnection_retries=0,
        connector=_connector(object()),
    )
    assert "reconnection_retries" not in opt_out.connect_kwargs()
    assert "reconnection_backoff" not in opt_out.connect_kwargs()

    explicit = WinRMTransport(
        host="win.example",
        username="u",
        password="p",
        reconnection_retries=3,
        reconnection_backoff=0.25,
        connector=_connector(object()),
    )
    kwargs = explicit.connect_kwargs()
    assert kwargs["reconnection_retries"] == 3
    assert kwargs["reconnection_backoff"] == 0.25


def test_probe_budget_comes_from_transport_attribute() -> None:
    """``probe_timeout_s`` replaces the default open-probe budget."""
    sess = _BlockingPsSession()
    t = _transport(sess, probe_timeout_s=0.2)
    t.connect()
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            t._execute_ps_stdout(t.session, "Get-Date")
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"probe budget not honored: {elapsed}s"
    finally:
        sess.block.set()


# ---------------------------------------------------------------------------
# link_retryable -> resync + retry once
# ---------------------------------------------------------------------------


def test_stale_link_self_heals_exec_op() -> None:
    """Core case: HTTP-400 rejection -> re-handshake -> op succeeds, marked in meta."""
    sess = _LinkFailureSession(error=_stale_encryption_error())
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_connector(sess),
    )
    assert r.status == "ok", r.fields
    assert r.fields.get("exit") == 0
    # The rejected request never ran remotely, so exactly one replay happened.
    assert sess.calls == ["Get-Date", "Get-Date"]
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.meta.get("session_resynced") is True
    assert ep.transport.is_connected() is True
    assert sess.closed is False
    # A real re-handshake: the stale encryption context was cleared and the
    # cached HTTP session closed before the retry.
    assert sess.transport.encryption is None
    assert sess.transport.session is None
    assert sess._http_session.closed is True


def test_stale_link_self_heals_oneshot_exec() -> None:
    """Oneshot ``execute_ps`` path shares the same self-heal + single retry."""
    sess = _OneshotLinkFailureSession(error=_stale_encryption_error())
    t = _transport(sess)
    t.connect()
    r = t.run_command("Get-Date")
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "oneshot-ok" in r.stdout
    assert len(sess.scripts) == 2, "oneshot must be retried exactly once"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True


def test_stale_link_self_heals_runspace_invoke_without_a_counter() -> None:
    """An uncountable double (no ``_send_request``) keeps the historical replay.

    The session exposes pypsrp's re-handshake state but no HTTP surface, so no
    round-trip counter can be installed and the invoke is fail-open: it
    re-handshakes and replays once. This is the *double* path, not evidence
    that an observable link may replay anything it likes - see
    :func:`test_stale_link_self_heals_runspace_invoke_on_a_countable_link`.
    """
    sess = _LinkFailureSession(error=_stale_encryption_error(), fail_times=0)
    t = _transport(sess)
    t.connect()
    assert t._round_trips is None
    handle = _ScriptedInvokeHandle(error=_stale_encryption_error())
    r = t.runspace_invoke(handle, "Get-Date")
    assert isinstance(r, RunspaceResult)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert handle.invokes == ["Get-Date", "Get-Date"]
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True


def test_stale_link_self_heals_runspace_invoke_on_a_countable_link() -> None:
    """The M1 rejection of the invoke's first exchange is provably unexecuted.

    A pooled invoke carries the script in its first WSMan message, so the
    round-trip counter cannot clear the replay on its own. An empty-body
    rejection does: the framing layer refused the request before any pipeline
    was built, so the transport re-handshakes and replays exactly once.
    """
    sess = _CountableLinkSession()
    t = _transport(sess)
    t.connect()
    assert t._round_trips is not None and t._round_trips() == 0
    handle = _CountableInvokeHandle(sess.transport, error=_stale_encryption_error())
    r = t.runspace_invoke(handle, "Get-Date")
    assert isinstance(r, RunspaceResult)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert handle.invokes == ["Get-Date", "Get-Date"], "exactly one replay"
    assert t.meta.get("session_resynced") is True
    assert t.is_connected() is True
    # A real re-handshake ran before the replay.
    assert sess.transport.encryption is None
    assert sess.transport.http_session.closed is True  # type: ignore[union-attr]
    assert sess.closed is False


def test_runspace_invoke_body_carrying_rejection_is_not_replayed() -> None:
    """A rejection carrying a body came from an intermediary: never replay.

    Whether the gateway forwarded the request before it failed is unknown, so
    the poisoned session is dropped instead of resubmitting the command.
    """
    sess = _CountableLinkSession()
    t = _transport(sess)
    t.connect()
    handle = _CountableInvokeHandle(sess.transport, error=_gateway_error())
    with pytest.raises(TransportError) as ei:
        t.runspace_invoke(handle, "Add-Content -Path C:\\x -Value 1")
    assert ei.value.code == "EXEC_FAILED"
    assert handle.invokes == ["Add-Content -Path C:\\x -Value 1"], "no replay"
    assert sess.transport.attempts == 0
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "link lost"
    assert t.meta.get("link_lost") is True
    assert t.meta.get("session_resynced") is None
    assert sess.transport.encryption == "auto", "refused replay does not re-handshake"
    assert sess.closed is True


def test_runspace_invoke_rejection_after_a_completed_exchange_is_not_replayed() -> None:
    """An exchange already completed, so the rejection is not the first one."""
    sess = _CountableLinkSession()
    t = _transport(sess)
    t.connect()
    handle = _CountableInvokeHandle(
        sess.transport, error=_stale_encryption_error(), exchanges=1
    )
    with pytest.raises(TransportError) as ei:
        t.runspace_invoke(handle, "Get-Date")
    assert ei.value.code == "EXEC_FAILED"
    assert handle.invokes == ["Get-Date"], "no replay after a completed exchange"
    assert sess.transport.attempts == 1
    assert t.is_connected() is False
    assert t.meta.get("link_lost") is True
    assert t.meta.get("session_resynced") is None
    assert sess.transport.encryption == "auto", "refused replay does not re-handshake"
    assert sess.closed is True


# ---------------------------------------------------------------------------
# retry fails again -> honest link death
# ---------------------------------------------------------------------------


def test_second_link_failure_marks_dead_honestly() -> None:
    """A retry that fails again drops the session and reports it truthfully."""
    sess = _LinkFailureSession(error=_stale_encryption_error(), fail_times=2)
    t = _transport(sess)
    t.connect()
    r = t.run_command("Get-Date")
    assert sess.calls == ["Get-Date", "Get-Date"], "exactly one retry, no more"
    assert r.exit_code == -1
    assert r.timed_out is False
    low = (r.stderr or "").lower()
    assert "link lost" in low
    assert "session closed" in low
    # Must claim neither a remote stop nor a remote no-op.
    assert "stopped" not in low
    assert "not guaranteed" not in low
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "link lost"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    assert t.meta.get("session_resynced") is None
    assert sess.closed is True
    assert t.session is None
    # The dead flag is real: the next call must reconnect, not reuse the shell.
    with pytest.raises(TransportError) as ei:
        t.run_command("Get-Date")
    assert ei.value.code == "NOT_CONNECTED"


def test_second_link_failure_runspace_reports_link_lost() -> None:
    """Runspace side: retry failure marks dead and returns a link-lost result."""
    sess = _LinkFailureSession(error=_stale_encryption_error(), fail_times=0)
    t = _transport(sess)
    t.connect()
    handle = _ScriptedInvokeHandle(error=_stale_encryption_error(), fail_times=2)
    r = t.runspace_invoke(handle, "Get-Date")
    assert handle.invokes == ["Get-Date", "Get-Date"], "exactly one retry, no more"
    assert r.exit_code == -1
    assert r.timed_out is False
    assert r.had_errors is True
    assert "link lost" in (r.stderr or "").lower()
    assert t.is_connected() is False
    assert t.meta.get("link_lost") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    assert t.session is None
    assert sess.closed is True


# ---------------------------------------------------------------------------
# link_fatal -> never retried
# ---------------------------------------------------------------------------


def test_read_timeout_is_not_retried_and_marks_dead() -> None:
    """A read timeout may have executed remotely: report, never replay."""
    sess = _LinkFailureSession(
        error=requests_exceptions.ReadTimeout("read timed out"),
        fail_times=99,
    )
    t = _transport(sess)
    t.connect()
    with pytest.raises(TransportError) as ei:
        t.run_command("Add-Content -Path C:\\x -Value 1")
    assert ei.value.code == "EXEC_FAILED"
    assert sess.calls == ["Add-Content -Path C:\\x -Value 1"], "no replay"
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "link lost"
    # No re-handshake was attempted for a fatal link failure.
    assert sess.transport.encryption == "auto"
    assert sess._http_session.closed is False
    assert sess.closed is True


# ---------------------------------------------------------------------------
# hard-timeout contract unchanged
# ---------------------------------------------------------------------------


def test_retry_budget_expiry_keeps_hard_timeout_contract() -> None:
    """A retry that burns the caller's budget is a timeout, not a link loss."""
    sess = _RetryTimeoutSession()
    t = _transport(sess)
    t.connect()
    try:
        r = t.run_command("Get-Date", timeout_s=0.3)
        assert sess.calls == ["Get-Date", "Get-Date"]
        assert r.timed_out is True
        assert r.exit_code == -1
        assert t.meta.get("dead_reason") == "hard timeout"
        assert t.meta.get("marked_dead") is True
        assert t.meta.get("session_disposed") is True
        assert t.meta.get("link_lost") is None
    finally:
        sess.block.set()


def test_hard_timeout_fields_do_not_regress() -> None:
    """Wall-clock budget expiry keeps mark_dead + dispose + timed_out fields."""
    sess = _BlockingPsSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]
    t = _transport(sess)
    t.connect()
    r = t.run_command("Read-Host hang", timeout_s=0.3)
    assert r.timed_out is True
    assert r.exit_code == -1
    assert t.is_connected() is False
    assert t.meta.get("dead_reason") == "hard timeout"
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("session_disposed") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    assert t.meta.get("link_lost") is None
    assert closes["n"] >= 1
    assert sess.closed is True
    assert "session closed" in (r.stderr or "").lower()
    assert "not guaranteed" in (r.stderr or "").lower()


# ---------------------------------------------------------------------------
# fs hook wiring
# ---------------------------------------------------------------------------


def test_open_fs_passes_serial_ops_and_link_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pypsrp fs client shares the transport lock and death callback."""
    captured: dict[str, object] = {}

    class _SpyClient:
        def __init__(self, session: object, **kwargs: object) -> None:
            captured["session"] = session
            captured["kwargs"] = kwargs

    monkeypatch.setattr(winrm_files, "PypsrpFileClient", _SpyClient)
    sess = _OneshotLinkFailureSession(error=_stale_encryption_error(), fail_times=0)
    t = _transport(sess)
    t.connect()
    client = t.open_fs()
    assert isinstance(client, _SpyClient)
    assert captured["session"] is sess
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    serial_ops = kwargs.get("serial_ops")
    assert callable(serial_ops)
    assert serial_ops.__self__ is t  # type: ignore[union-attr]
    on_link_failure = kwargs.get("on_link_failure")
    assert callable(on_link_failure)

    on_link_failure(RuntimeError("peer reset"))  # type: ignore[operator]
    assert t.is_connected() is False
    assert t.meta.get("marked_dead") is True
    assert t.meta.get("link_lost") is True
    assert t.meta.get("reopen_hint") == "endpoint close then open"
    assert t.session is None
    assert sess.closed is True
