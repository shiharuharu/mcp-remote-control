"""A refused WinRM link must be retired, and the refusal must travel as tokens.

Two defects found by the post-fix review, both of which made a *refused* request
look like an ordinary failure:

- A WSMan 401 arriving as a plain auth error is classified ``other`` by the
  transport's taxonomy, so nothing marked the session dead: the poisoned
  generation stayed registered, every later op failed with the identical
  message, and ``endpoint open`` handed the same handle back. Only the fs path
  retired such a link, and only for its own errors.
- The registry classifies a refused reconnect (``rejected`` / ``http_status`` /
  ``probe_failed``) but only the fs row mirrored those tokens, so the surface the
  reopen remedy points at - ``endpoint open`` - reported the same
  undifferentiated ``NOT_CONNECTED`` an unreachable host produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.core import endpoint_ops, exec_ops, ps_ops
from mcp_remote_control.endpoint import get_registry, reset_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _auth_error() -> BaseException:
    """A pypsrp ``AuthenticationError`` (the 401 shape), or a faithful double."""
    try:
        from pypsrp.exceptions import AuthenticationError

        return AuthenticationError("Failed to authenticate the user lab with ntlm")
    except Exception:  # noqa: BLE001 - pypsrp is a hard dep, but keep the double
        return type(
            "AuthenticationError", (Exception,), {"__module__": "pypsrp.exceptions"}
        )("Failed to authenticate the user lab with ntlm")


def _rejection(status: int, body: str = "") -> BaseException:
    """A pypsrp ``WinRMTransportError`` carrying an HTTP status."""
    try:
        from pypsrp.exceptions import WinRMTransportError

        return WinRMTransportError("http", status, body)
    except Exception:  # noqa: BLE001
        return type(
            "WinRMTransportError", (Exception,), {"__module__": "pypsrp.exceptions"}
        )("http", status, body)


class _Session:
    """Oneshot session: raises *exc* from every call, or answers a probe.

    ``open_runspace`` is present so the ps surface can reach its own link
    handling - a session without it fails earlier, as UNSUPPORTED, and never
    exercises the refusal path this module is about.
    """

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc

    def execute_ps(self, script: str, *, environment: Any = None) -> Any:
        if self._exc is not None:
            raise self._exc
        return ("windows\r\npowershell\r\n5.1\r\n", None, False)

    def open_runspace(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return object()

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    reset_registry()
    yield
    reset_registry()


def test_a_refused_exec_session_is_retired_not_handed_back() -> None:
    """A WSMan refusal on a live exec session must not poison the generation.

    Without retirement the identical failure repeats forever and ``endpoint
    open`` returns the same handle, which is what made this defect hard to see
    from the outside: the endpoint looked open and healthy.
    """
    exc = _auth_error()
    r = endpoint_ops.run(
        op="open", profile="lab-win", home=FIXTURES, connector=lambda **k: _Session()
    )
    assert r.status == "ok", r.fields

    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    transport = ep.transport
    # Make every oneshot raise the refusal, as an expired auth context would.
    transport.session._execute_ps = lambda script, environment=None: (_ for _ in ()).throw(exc)

    first = exec_ops.run(ep="lab-win", command="echo hi", home=FIXTURES)
    assert first.status == "error"
    assert transport.is_connected() is False, (
        "a refused session must not stay marked live"
    )

    second = exec_ops.run(ep="lab-win", command="echo hi", home=FIXTURES)
    assert second.status == "error"
    assert second.code == "NOT_CONNECTED", (
        "the retry must fail as a dead endpoint (and reconnect), not repeat the "
        f"poisoned failure: {second.code} {second.fields}"
    )


def test_ps_refuses_to_reuse_a_refused_link() -> None:
    """The same rule on the ps surface: a refused runspace open retires the link.

    The refusal has to come from the session call itself - an UNSUPPORTED
    ("this session has no runspace") is a capability answer, not a refusal, and
    must not retire anything.
    """
    exc = _auth_error()
    # ``probe=False`` keeps the capability gate out of the way: an injected
    # session whose probe yields no runspace evidence is legitimately gated as
    # UNSUPPORTED before any refusal could occur, which is a different (and
    # correct) behaviour from the one under test here.
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        probe=False,
        connector=lambda **k: _Session(),
    )
    assert r.status == "ok", r.fields

    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    transport = ep.transport
    refusal = _raise(exc)
    transport.session._execute_ps = refusal
    transport.session._open_runspace = refusal

    opened = ps_ops.run(op="open", ep="lab-win", home=FIXTURES)
    assert opened.status == "error"
    assert transport.is_connected() is False, (
        "a refused runspace open must not leave the endpoint looking live"
    )


def _raise(exc: BaseException) -> Any:
    """A callable that raises *exc* however it is invoked."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _boom


def test_endpoint_open_reports_a_refused_reconnect_as_a_refusal() -> None:
    """``endpoint open`` is the documented remedy - it must name the refusal.

    "The host is unreachable" and "the intermediary refused this request" need
    different responses from an operator, and both surface as NOT_CONNECTED.
    """
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=lambda **k: _Session(_rejection(400, "")),
    )
    assert r.status == "error"
    assert r.code == "NOT_CONNECTED"
    fields = dict(r.fields or {})
    assert fields.get("rejected") == 1, fields
    assert fields.get("http_status") == 400, fields


def test_exec_reports_a_refused_reconnect_as_a_refusal() -> None:
    """The same tokens on the exec row, which had the identical gap."""
    r = exec_ops.run(
        ep="lab-win",
        command="echo hi",
        home=FIXTURES,
        connector=lambda **k: _Session(_rejection(400, "")),
    )
    assert r.status == "error"
    fields = dict(r.fields or {})
    assert fields.get("rejected") == 1, fields
    assert fields.get("http_status") == 400, fields


def test_a_5xx_is_not_reported_as_a_refusal() -> None:
    """A 5xx may already have been dispatched, so it is never a refusal token."""
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=lambda **k: _Session(_rejection(503, "gateway")),
    )
    assert r.status == "error"
    fields = dict(r.fields or {})
    assert fields.get("http_status") == 503, fields
    assert "rejected" not in fields, fields
