"""Unit tests: ``[winrm]`` link-tuning knobs reach ``WinRMTransport``.

The profile is the only source for ``reconnection_retries`` /
``reconnection_backoff`` / ``probe_timeout_s``. A usable value is forwarded so
the transport owns the pypsrp retry policy and the open-time probe budget;
anything unusable (bool, unparsable string, negative or non-finite number)
degrades to ``None`` so the transport default applies - a typo in a tuning
knob must not make an endpoint unopenable. ``0`` is *not* junk: it is the
deliberate opt-out and must survive as a value distinct from "unset".

All knobs are non-secret, so nothing here should widen what ``repr`` or the
endpoint meta exposes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.config import load_profile
from mcp_remote_control.endpoint import connect as connect_mod
from mcp_remote_control.endpoint.connect import _build_winrm_transport
from mcp_remote_control.endpoint.registry import EndpointRegistry
from mcp_remote_control.transport import WinRMTransport
from mcp_remote_control.transport.base import ExecResult

KNOBS = ("reconnection_retries", "reconnection_backoff", "probe_timeout_s")

SECRET = "s3cret-keepalive-password"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_profile(home: Path, name: str, lines: list[str]) -> None:
    """Write a minimal WinRM profile whose ``[winrm]`` table is *lines*."""
    pdir = home / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        [
            f'name = "{name}"',
            'transport = "winrm"',
            'host = "win.example"',
            'username = "lab"',
            "[auth]",
            'method = "ntlm"',
            f'password = "{SECRET}"',
            "[winrm]",
            *lines,
        ]
    )
    (pdir / f"{name}.toml").write_text(body + "\n", encoding="utf-8")


def _stub_connector(**kwargs: object) -> object:
    """Session double so ``connect`` succeeds without touching the network."""

    class _Session:
        cwd = r"C:\Windows"
        os = "windows"
        shell = "powershell"

        def run_command(self, *args: object, **kw: object) -> ExecResult:
            return ExecResult(0, "ok", "", cwd=self.cwd)

    return _Session()


@pytest.fixture
def ctor_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the kwargs profile mapping hands to the transport constructor."""
    recorded: list[dict[str, Any]] = []

    def _recorder(**kwargs: Any) -> Any:
        recorded.append(kwargs)
        return kwargs

    monkeypatch.setattr(connect_mod, "WinRMTransport", _recorder)
    return recorded


# ---------------------------------------------------------------------------
# Profile -> constructor kwargs (mapping)
# ---------------------------------------------------------------------------


def test_knobs_forwarded_when_set(
    tmp_path: Path, ctor_kwargs: list[dict[str, Any]]
) -> None:
    _write_profile(
        tmp_path,
        "knobbed",
        [
            "reconnection_retries = 3",
            "reconnection_backoff = 0.25",
            "probe_timeout_s = 12.5",
        ],
    )
    _build_winrm_transport(load_profile(tmp_path, "knobbed"), connector=None)

    kwargs = ctor_kwargs[-1]
    assert kwargs["reconnection_retries"] == 3
    assert not isinstance(kwargs["reconnection_retries"], bool)
    assert kwargs["reconnection_backoff"] == 0.25
    assert isinstance(kwargs["reconnection_backoff"], float)
    assert kwargs["probe_timeout_s"] == 12.5
    assert isinstance(kwargs["probe_timeout_s"], float)


def test_knobs_absent_forward_none(
    tmp_path: Path, ctor_kwargs: list[dict[str, Any]]
) -> None:
    """An unset knob must stay distinguishable from an explicit 0."""
    _write_profile(tmp_path, "plain", ['scheme = "http"'])
    _build_winrm_transport(load_profile(tmp_path, "plain"), connector=None)

    kwargs = ctor_kwargs[-1]
    for name in KNOBS:
        assert kwargs[name] is None


def test_string_numeric_knobs_are_accepted(
    tmp_path: Path, ctor_kwargs: list[dict[str, Any]]
) -> None:
    """TOML/JSON strings are common in generated profiles; do not reject them."""
    _write_profile(
        tmp_path,
        "stringy",
        [
            'reconnection_retries = "4"',
            'reconnection_backoff = "0.5"',
            'probe_timeout_s = "9"',
        ],
    )
    _build_winrm_transport(load_profile(tmp_path, "stringy"), connector=None)

    kwargs = ctor_kwargs[-1]
    assert kwargs["reconnection_retries"] == 4
    assert kwargs["reconnection_backoff"] == 0.5
    assert kwargs["probe_timeout_s"] == 9.0


def test_zero_is_an_opt_out_not_unset(
    tmp_path: Path, ctor_kwargs: list[dict[str, Any]]
) -> None:
    """0 retries / 0.0 backoff mean "disable", so they must survive as values."""
    _write_profile(
        tmp_path,
        "zeroed",
        [
            "reconnection_retries = 0",
            "reconnection_backoff = 0.0",
        ],
    )
    _build_winrm_transport(load_profile(tmp_path, "zeroed"), connector=None)

    kwargs = ctor_kwargs[-1]
    assert kwargs["reconnection_retries"] == 0
    assert kwargs["reconnection_backoff"] == 0.0
    # A 0s probe budget is unusable, so it degrades instead.
    assert kwargs["probe_timeout_s"] is None


@pytest.mark.parametrize(
    ("knob", "toml_value"),
    [
        ("reconnection_retries", '"abc"'),
        ("reconnection_retries", "-1"),
        ("reconnection_retries", "true"),
        ("reconnection_retries", "false"),
        ("reconnection_retries", "inf"),
        ("reconnection_retries", "nan"),
        ("reconnection_backoff", '"abc"'),
        ("reconnection_backoff", "-0.5"),
        ("reconnection_backoff", "true"),
        ("reconnection_backoff", "nan"),
        ("reconnection_backoff", "inf"),
        ("probe_timeout_s", '"abc"'),
        ("probe_timeout_s", "-1"),
        ("probe_timeout_s", "0"),
        ("probe_timeout_s", "true"),
        ("probe_timeout_s", "nan"),
    ],
)
def test_junk_knob_degrades_to_none(
    tmp_path: Path,
    ctor_kwargs: list[dict[str, Any]],
    knob: str,
    toml_value: str,
) -> None:
    _write_profile(tmp_path, "junk", [f"{knob} = {toml_value}"])
    # Never raises: opening with a typo'd knob must fall back, not fail.
    _build_winrm_transport(load_profile(tmp_path, "junk"), connector=None)

    assert ctor_kwargs[-1][knob] is None


# ---------------------------------------------------------------------------
# Registry open -> live transport attributes
# ---------------------------------------------------------------------------


def test_transport_attributes_match_profile(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "knobbed",
        [
            "reconnection_retries = 3",
            "reconnection_backoff = 0.25",
            "probe_timeout_s = 12.5",
        ],
    )
    reg = EndpointRegistry()
    ep = reg.open("knobbed", home=tmp_path, connector=_stub_connector, probe=False)
    transport = ep.transport
    assert isinstance(transport, WinRMTransport)
    assert transport.reconnection_retries == 3
    assert transport.reconnection_backoff == 0.25
    assert transport.probe_timeout_s == 12.5


def test_transport_attributes_none_when_unset(tmp_path: Path) -> None:
    _write_profile(tmp_path, "plain", ['scheme = "http"'])
    reg = EndpointRegistry()
    ep = reg.open("plain", home=tmp_path, connector=_stub_connector, probe=False)
    for name in KNOBS:
        assert getattr(ep.transport, name, "missing") is None


def test_junk_knobs_still_open(tmp_path: Path) -> None:
    """Opening with unusable knob values succeeds and keeps the defaults."""
    _write_profile(
        tmp_path,
        "junk",
        [
            'reconnection_retries = "abc"',
            "reconnection_backoff = -1",
            "probe_timeout_s = true",
        ],
    )
    reg = EndpointRegistry()
    ep = reg.open("junk", home=tmp_path, connector=_stub_connector, probe=False)
    assert ep.connected is True
    for name in KNOBS:
        assert getattr(ep.transport, name, "missing") is None


def test_non_finite_retries_still_open(tmp_path: Path) -> None:
    """``inf`` must not reach ``int()``: the overflow would fail the open."""
    _write_profile(tmp_path, "overflow", ["reconnection_retries = inf"])
    reg = EndpointRegistry()
    ep = reg.open("overflow", home=tmp_path, connector=_stub_connector, probe=False)
    assert ep.connected is True
    assert ep.transport.reconnection_retries is None


# ---------------------------------------------------------------------------
# No secret widening
# ---------------------------------------------------------------------------


def test_knobs_do_not_leak_secrets_into_profile_repr(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "knobbed",
        [
            "reconnection_retries = 3",
            "reconnection_backoff = 0.25",
            "probe_timeout_s = 12.5",
        ],
    )
    profile = load_profile(tmp_path, "knobbed")
    for render in (repr(profile), str(profile)):
        assert SECRET not in render


def test_knobs_do_not_leak_secrets_into_repr_or_meta(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "knobbed",
        [
            "reconnection_retries = 3",
            "reconnection_backoff = 0.25",
            "probe_timeout_s = 12.5",
        ],
    )
    reg = EndpointRegistry()
    ep = reg.open("knobbed", home=tmp_path, connector=_stub_connector, probe=False)
    renders = [repr(ep.transport), repr(ep.meta), str(ep.meta)]
    for render in renders:
        assert SECRET not in render
    # The knobs themselves are inspectable, not hidden as secrets.
    assert ep.transport.reconnection_retries == 3
