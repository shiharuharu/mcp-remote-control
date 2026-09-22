"""Service tests: ps invoke tells the Agent what a dead link means.

A runspace lives in the transport's local WSMan session, so a link death takes
the runspace with it: the session id is stale, its variables are gone, and only
a fresh ``endpoint open`` + ``ps open`` brings PowerShell back. These tests pin
the machine-readable token and the recovery hint on every surface of that story
- the invoke whose link died mid-call (whether the transport returned a
link-lost result or raised after marking the link dead), the follow-up invoke
that finds the transport already dead - and pin that healthy and
ordinary-error invokes stay free of both.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.core import ps_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.ps import get_ps_registry, reset_ps_registry
from mcp_remote_control.ps.session import PsSession
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.winrm_runspace import RunspaceResult

try:  # pypsrp is a hard dependency; the double keeps the shape testable.
    from pypsrp import exceptions as pypsrp_exceptions
except ImportError:  # pragma: no cover - exercised only without pypsrp
    pypsrp_exceptions = None  # type: ignore[assignment]

try:  # requests ships with pypsrp; the double keeps the shape testable.
    from requests import exceptions as requests_exceptions
except ImportError:  # pragma: no cover - exercised only without requests
    requests_exceptions = None  # type: ignore[assignment]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Measured shape of the stale message-encryption rejection: the framing layer
# refuses the request before any WSMan pipeline exists.
_EMPTY_BODY_REJECTION = "Bad HTTP response returned from the server. Code: 400, Content: ''"

# Measured shape of the lab gateway's own error page: a body proves an
# intermediary answered, so the request may already have been delivered.
_BODY_502_REJECTION = "Connection error: read ETIMEDOUT"


def _duck_type(module: str, qualname: str) -> type:
    """An exception type carrying a real dependency's (module, qualname)."""
    return type(
        qualname.rsplit(".", 1)[-1],
        (Exception,),
        {"__module__": module, "__qualname__": qualname},
    )


def _winrm_transport_error(status: int, body: str) -> BaseException:
    """A pypsrp-shaped HTTP rejection, real class when pypsrp is importable."""
    if pypsrp_exceptions is not None:
        return pypsrp_exceptions.WinRMTransportError("http", status, body)
    double = _duck_type("pypsrp.exceptions", "WinRMTransportError")
    return double("http", status, body)


def _stale_encryption_error() -> BaseException:
    return _winrm_transport_error(400, "")


def _connection_aborted_error() -> BaseException:
    """A connection-level failure: the socket died, execution unknown."""
    if requests_exceptions is not None:
        return requests_exceptions.ConnectionError("Connection aborted")
    double = _duck_type("requests.exceptions", "ConnectionError")
    return double("Connection aborted")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_ps_registry()
    yield
    reset_ps_registry()
    reset_registry()


# ---------------------------------------------------------------------------
# Registry-injected transport double (no network, no pypsrp)
# ---------------------------------------------------------------------------


class _FakePsTransport:
    """Transport surface a registered ps session needs for one invoke."""

    def __init__(
        self,
        *,
        connected: bool = True,
        meta: dict[str, Any] | None = None,
        result: RunspaceResult | None = None,
        error: TransportError | None = None,
    ) -> None:
        self.meta: dict[str, Any] = dict(meta or {})
        self._connected = connected
        self._result = result
        self._error = error
        self.invokes: list[str] = []

    def is_connected(self) -> bool:
        return self._connected

    def close_runspace(self, handle: Any) -> None:
        del handle

    def runspace_invoke(
        self,
        handle: Any,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        del handle, timeout_s
        self.invokes.append(script)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _LinkDiedTransport(_FakePsTransport):
    """Transport whose invoke tears the link down, as the real one does.

    Mirrors :meth:`WinRMTransport._mark_link_dead`: the session is dropped, the
    reason is recorded, and the runspace result reports the loss.
    """

    def runspace_invoke(
        self,
        handle: Any,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        del handle, timeout_s
        self.invokes.append(script)
        self._connected = False
        self.meta.update(
            {
                "marked_dead": True,
                "link_lost": True,
                "dead_reason": "link lost",
                "reopen_hint": "endpoint close then open",
            }
        )
        return RunspaceResult(
            stdout="",
            stderr=(
                f"{_EMPTY_BODY_REJECTION}; winrm link lost after re-handshake, "
                "local session closed (endpoint reconnects on the next call)"
            ),
            exit_code=-1,
            location=r"C:\Users\mock",
            had_errors=True,
            timed_out=False,
        )


def _register(transport: Any, *, sid: str = "ps_aa2") -> str:
    get_ps_registry().add(
        PsSession(
            id=sid,
            ep="lab-win",
            handle=object(),
            transport=transport,
            location=r"C:\Users\mock",
        )
    )
    return sid


# ---------------------------------------------------------------------------
# invoke whose link died mid-call
# ---------------------------------------------------------------------------


def test_invoke_link_loss_reports_token_and_reopen_hint() -> None:
    """A link-lost invoke carries ``link_lost`` and says how to recover."""
    transport = _LinkDiedTransport()
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.status == "fail"
    assert r.fields["link_lost"] == 1
    assert r.fields["exit"] == -1
    assert transport.invokes == ["Get-Date"]
    # The transport's own honest detail survives in the body.
    assert "link lost" in (r.body or "")
    assert "link_lost=1" in r.render_text()

    hint = r.hint or ""
    assert "link" in hint and "lost" in hint
    assert "runspace" in hint, "hint must say the runspace did not survive"
    assert "endpoint open" in hint and "ps open" in hint, "recovery path"
    assert "not reusable" in hint, "hint must not imply the session is reusable"


def test_next_invoke_after_link_loss_names_the_dead_reason() -> None:
    """The follow-up invoke reports the transport's reason, not a generic one."""
    transport = _LinkDiedTransport()
    sid = _register(transport)
    assert ps_ops.invoke(id=sid, script="Get-Date").fields["link_lost"] == 1

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.status == "error"
    assert r.code == "NOT_CONNECTED"
    assert r.fields["msg"] == "link lost"
    assert r.fields["link_lost"] == 1
    hint = r.hint or ""
    assert "endpoint open" in hint and "ps open" in hint
    # The dead session is pruned: the id is stale and never reused.
    assert get_ps_registry().get(sid) is None
    assert transport.invokes == ["Get-Date"], "no second call on a dead transport"


def test_timeout_result_does_not_claim_link_loss() -> None:
    """A wall-clock timeout leaves the runspace usable - never a link token."""
    transport = _FakePsTransport(
        meta={"link_lost": True, "dead_reason": "link lost"},
        result=RunspaceResult(
            stdout="",
            stderr="timed out",
            exit_code=-1,
            had_errors=True,
            timed_out=True,
        ),
    )
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="Start-Sleep 9", timeout=1)

    assert r.status == "timeout"
    assert r.fields["timed_out"] is True
    assert "link_lost" not in r.fields
    assert r.hint is None


# ---------------------------------------------------------------------------
# paths that must gain no key
# ---------------------------------------------------------------------------


def test_healthy_invoke_has_no_link_keys() -> None:
    transport = _FakePsTransport(
        result=RunspaceResult(stdout="ok\n", location=r"C:\Users\mock")
    )
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="echo ok")

    assert r.status == "ok"
    assert "link_lost" not in r.fields
    assert r.hint is None
    assert "link_lost" not in r.render_text()


def test_ordinary_transport_error_has_no_link_keys() -> None:
    transport = _FakePsTransport(
        error=TransportError("EXEC_FAILED", "runspace blew up")
    )
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="boom")

    assert r.status == "error"
    assert r.code == "EXEC_FAILED"
    assert "link_lost" not in r.fields
    assert r.hint is None


def test_ordinary_transport_error_still_prunes_the_session() -> None:
    """A raise without a link marker keeps the prune-on-failure path.

    The transport stays connected, so the handle may be unusable while the id
    looks live: the session must not be offered to a follow-up invoke.
    """
    transport = _FakePsTransport(
        error=TransportError("EXEC_FAILED", "runspace blew up")
    )
    sid = _register(transport)

    assert ps_ops.invoke(id=sid, script="boom").code == "EXEC_FAILED"
    assert get_ps_registry().get(sid) is None

    r = ps_ops.invoke(id=sid, script="boom")

    assert r.code == "PS_NOT_FOUND"
    assert transport.invokes == ["boom"], "no second call on the dead handle"


def test_dead_transport_without_reason_keeps_generic_msg() -> None:
    transport = _FakePsTransport(connected=False, meta={})
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.code == "NOT_CONNECTED"
    assert r.fields["msg"] == "endpoint transport not connected"
    assert "link_lost" not in r.fields


def test_dead_transport_with_unrelated_reason_gets_no_link_token() -> None:
    """A non-link death surfaces its reason but must not claim a lost link."""
    transport = _FakePsTransport(connected=False, meta={"dead_reason": "peer_reset"})
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.code == "NOT_CONNECTED"
    assert r.fields["msg"] == "peer_reset"
    assert "link_lost" not in r.fields
    # The reopen advice holds for any dead transport, but the wording must not
    # describe a link loss the transport never recorded.
    hint = r.hint or ""
    assert "endpoint open" in hint and "ps open" in hint
    assert "link was lost" not in hint


def test_dead_transport_reason_is_blank_falls_back() -> None:
    transport = _FakePsTransport(connected=False, meta={"dead_reason": "   "})
    sid = _register(transport)

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.fields["msg"] == "endpoint transport not connected"
    assert "link_lost" not in r.fields


# ---------------------------------------------------------------------------
# end-to-end through the real transport (the measured idle-link failure)
# ---------------------------------------------------------------------------


class _IdleEncryptionHandle:
    """Runspace handle whose first *fail_times* invokes hit the stale link."""

    def __init__(self, *, fail_times: int) -> None:
        self.location = r"C:\Users\mock"
        self.closed = False
        self.invokes: list[str] = []
        self._fail_times = fail_times

    def invoke(self, script: str) -> Any:
        self.invokes.append(script)
        if len(self.invokes) <= self._fail_times:
            raise _stale_encryption_error()
        return ("ok\n", None)

    def close(self) -> None:
        self.closed = True


class _IdleEncryptionSession:
    """WinRM session exposing a runspace that cannot re-handshake in place."""

    def __init__(self, *, fail_times: int = 2) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.wsman = None
        self.handle: _IdleEncryptionHandle | None = None
        self._fail_times = fail_times

    @property
    def has_open_runspace(self) -> bool:
        return True

    def open_runspace(self) -> _IdleEncryptionHandle:
        self.handle = _IdleEncryptionHandle(fail_times=self._fail_times)
        return self.handle

    def close(self) -> None:
        self.closed = True


def test_idle_link_death_end_to_end_reports_token_and_hint() -> None:
    """Real transport: idle rejection -> resync + replay -> still dead.

    Drives ``ps open`` / ``ps invoke`` through :class:`WinRMTransport` so the
    detection is proven against the transport's own link-lost result rather
    than a hand-built one.
    """
    session = _IdleEncryptionSession(fail_times=2)

    def connector(**_kwargs: object) -> object:
        return session

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=connector)
    assert opened.status == "ok", opened.render_text()
    sid = opened.fields["id"]

    r = ps_ops.invoke(id=sid, script="Get-Date")

    assert r.status == "fail"
    assert r.fields["link_lost"] == 1
    assert "link lost" in (r.body or "")
    hint = r.hint or ""
    assert "endpoint open" in hint and "ps open" in hint
    assert session.handle is not None
    assert session.handle.invokes == ["Get-Date", "Get-Date"], "one replay, no more"

    # Second invoke: the transport is dead, so the message names the reason and
    # the session is pruned instead of being offered as reusable.
    r2 = ps_ops.invoke(id=sid, script="Get-Date")
    assert r2.code == "NOT_CONNECTED"
    assert r2.fields["msg"] == "link lost"
    assert r2.fields["link_lost"] == 1
    assert get_ps_registry().get(sid) is None


class _RejectingHandle:
    """Runspace handle whose every invoke raises the given failure."""

    def __init__(self, exc: BaseException) -> None:
        self.location = r"C:\Users\mock"
        self.closed = False
        self.invokes: list[str] = []
        self._exc = exc

    def invoke(self, script: str) -> Any:
        self.invokes.append(script)
        raise self._exc

    def close(self) -> None:
        self.closed = True


class _RejectingSession(_IdleEncryptionSession):
    """WinRM session whose runspace invoke raises instead of returning."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(fail_times=0)
        self._exc = exc

    def open_runspace(self) -> Any:
        self.handle = _RejectingHandle(self._exc)
        return self.handle


@pytest.mark.parametrize(
    ("exc_factory", "fragment"),
    [
        (lambda: _winrm_transport_error(502, _BODY_502_REJECTION), "Connection error"),
        (_connection_aborted_error, "Connection aborted"),
    ],
)
def test_transport_raised_link_death_names_the_link_and_the_reopen(
    exc_factory: Callable[[], BaseException], fragment: str
) -> None:
    """Real transport: a raise after mark_dead still reports the link loss.

    Both measured shapes reach the same place - a gateway rejection carrying a
    body and a connection-level failure are never replayable, so the transport
    marks the link dead and raises instead of returning a result.
    """
    session = _RejectingSession(exc_factory())

    def connector(**_kwargs: object) -> object:
        return session

    opened = ps_ops.open_session(ep="lab-win", home=FIXTURES, connector=connector)
    assert opened.status == "ok", opened.render_text()
    sid = opened.fields["id"]

    r = ps_ops.invoke(id=sid, script="Add-Content x.txt 1")

    assert r.status == "error"
    assert r.code == "EXEC_FAILED"
    assert fragment in r.fields["msg"], "the transport's own text stays in msg"
    assert r.fields["link_lost"] == 1, "the link token mirrors the transport meta"
    hint = r.hint or ""
    assert "endpoint open" in hint and "ps open" in hint
    assert "not reusable" in hint
    assert session.handle is not None
    assert session.handle.invokes == ["Add-Content x.txt 1"], "never replayed"

    # The id survives one more call so the follow-up can name the dead link
    # instead of reporting a bare stale id.
    assert get_ps_registry().get(sid) is not None
    r2 = ps_ops.invoke(id=sid, script="Add-Content x.txt 1")
    assert r2.code == "NOT_CONNECTED"
    assert r2.fields["msg"] == "link lost"
    assert r2.fields["link_lost"] == 1
    r2_hint = r2.hint or ""
    assert "endpoint open" in r2_hint and "ps open" in r2_hint

    # The dead session is pruned by that call, so a third invoke is stale.
    assert get_ps_registry().get(sid) is None
    assert ps_ops.invoke(id=sid, script="Add-Content x.txt 1").code == "PS_NOT_FOUND"
