"""Unit tests: pypsrp reconnection kwargs pass-through (no network)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from mcp_remote_control.transport import assemble_pypsrp_kwargs
from mcp_remote_control.transport.winrm_session import default_winrm_connector

BASE_ARGS = {
    "host": "10.0.0.20",
    "username": "Administrator",
    "password": "s3cret",
    "auth": "ntlm",
}

# Keys the base call produces; reconnect knobs must be the only additions.
BASE_KEYS = {
    "host",
    "username",
    "password",
    "ssl",
    "auth",
    "cert_validation",
    "encryption",
}


def test_reconnect_kwargs_absent_when_unset() -> None:
    """Unset must not inject a key: pypsrp's own default of 0 stays in force."""
    kw = assemble_pypsrp_kwargs(**BASE_ARGS)
    assert "reconnection_retries" not in kw
    assert "reconnection_backoff" not in kw
    assert set(kw) == BASE_KEYS


def test_reconnect_kwargs_default_to_none() -> None:
    """Signature defaults stay None - a 0 default would erase "unset"."""
    sig = inspect.signature(assemble_pypsrp_kwargs)
    for name in ("reconnection_retries", "reconnection_backoff"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None


def test_reconnect_retries_present_and_int() -> None:
    kw = assemble_pypsrp_kwargs(**BASE_ARGS, reconnection_retries=3)
    assert kw["reconnection_retries"] == 3
    assert isinstance(kw["reconnection_retries"], int)
    assert not isinstance(kw["reconnection_retries"], bool)
    # Unrelated knob stays absent - the two are independent.
    assert "reconnection_backoff" not in kw
    assert set(kw) == BASE_KEYS | {"reconnection_retries"}


def test_reconnect_backoff_present_and_float() -> None:
    kw = assemble_pypsrp_kwargs(**BASE_ARGS, reconnection_backoff=0.5)
    assert kw["reconnection_backoff"] == 0.5
    assert isinstance(kw["reconnection_backoff"], float)
    assert "reconnection_retries" not in kw
    assert set(kw) == BASE_KEYS | {"reconnection_backoff"}


def test_reconnect_both_set_together() -> None:
    kw = assemble_pypsrp_kwargs(
        **BASE_ARGS,
        reconnection_retries=5,
        reconnection_backoff=1.25,
    )
    assert kw["reconnection_retries"] == 5
    assert kw["reconnection_backoff"] == 1.25
    assert set(kw) == BASE_KEYS | {"reconnection_retries", "reconnection_backoff"}


def test_reconnect_retries_zero_is_explicit_disable() -> None:
    """0 must survive assembly: it is a deliberate disable, not "unset"."""
    kw = assemble_pypsrp_kwargs(**BASE_ARGS, reconnection_retries=0)
    assert "reconnection_retries" in kw
    assert kw["reconnection_retries"] == 0
    assert isinstance(kw["reconnection_retries"], int)


def test_reconnect_backoff_zero_is_explicit() -> None:
    kw = assemble_pypsrp_kwargs(**BASE_ARGS, reconnection_backoff=0.0)
    assert "reconnection_backoff" in kw
    assert kw["reconnection_backoff"] == 0.0
    assert isinstance(kw["reconnection_backoff"], float)


def test_reconnect_kwargs_do_not_leak_into_other_entries() -> None:
    """The knobs live only as top-level kwargs - no nesting under meta/auth."""
    kw = assemble_pypsrp_kwargs(
        **BASE_ARGS,
        reconnection_retries=2,
        reconnection_backoff=0.5,
    )
    nested = {key: val for key, val in kw.items() if isinstance(val, (dict, list))}
    assert nested == {}
    for key, val in kw.items():
        assert "reconnection" not in key or key in (
            "reconnection_retries",
            "reconnection_backoff",
        )
        assert not (isinstance(val, str) and "reconnection" in val)


# ---------------------------------------------------------------------------
# default_winrm_connector - assembler output must reach Client/WSMan
# ---------------------------------------------------------------------------


def _capture_client_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``pypsrp.client.Client`` and return the kwargs it was built with."""
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, server: str, **kwargs: Any) -> None:
            captured["server"] = server
            captured.update(kwargs)
            self.wsman = object()

    monkeypatch.setattr("pypsrp.client.Client", _FakeClient)
    return captured


def test_connector_forwards_reconnect_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assembled knobs must reach the Client, not stop at the caller."""
    captured = _capture_client_kwargs(monkeypatch)
    default_winrm_connector(
        host="10.0.0.20",
        username="Administrator",
        password="s3cret",
        auth="ntlm",
        reconnection_retries=3,
        reconnection_backoff=0.25,
    )
    assert captured["reconnection_retries"] == 3
    assert isinstance(captured["reconnection_retries"], int)
    assert captured["reconnection_backoff"] == 0.25
    assert isinstance(captured["reconnection_backoff"], float)


def test_connector_omits_reconnect_kwargs_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset must not inject the key - pypsrp's own default of 0 stands."""
    captured = _capture_client_kwargs(monkeypatch)
    default_winrm_connector(**BASE_ARGS)
    assert "reconnection_retries" not in captured
    assert "reconnection_backoff" not in captured


def test_connector_forwards_explicit_zero_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit 0 is a disable, not "unset" - it must survive the factory."""
    captured = _capture_client_kwargs(monkeypatch)
    default_winrm_connector(**BASE_ARGS, reconnection_retries=0, reconnection_backoff=0.0)
    assert captured["reconnection_retries"] == 0
    assert captured["reconnection_backoff"] == 0.0


def test_connector_reconnect_kwargs_land_on_wsman() -> None:
    """Guards the stub above against pypsrp renaming its transport knobs.

    ``Client(**kwargs)`` forwards to ``WSMan``, so the values must be readable
    on the live HTTP transport once the factory returns a real adapter.
    """
    pytest.importorskip("pypsrp")
    adapter = default_winrm_connector(
        **BASE_ARGS,
        reconnection_retries=3,
        reconnection_backoff=0.25,
    )
    transport = adapter.wsman.transport
    assert transport.reconnection_retries == 3
    assert transport.reconnection_backoff == 0.25
