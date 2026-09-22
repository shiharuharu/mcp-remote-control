"""Unit tests: WinRM open-probe budget resolution (env / profile / default)."""

from __future__ import annotations

import os

import pytest

from mcp_remote_control.transport.winrm_probe import (
    MRC_WINRM_PROBE_TIMEOUT_ENV,
    MRC_WINRM_PROBE_TIMEOUT_S,
    resolve_winrm_open_probe_mode,
    resolve_winrm_probe_timeout_s,
)

DEFAULT_BUDGET = 5.0


def test_default_constant_is_five_seconds() -> None:
    # Wire contract: an unconfigured probe budget stays at the historical 5.0s.
    assert MRC_WINRM_PROBE_TIMEOUT_S == DEFAULT_BUDGET


def test_env_var_name() -> None:
    assert MRC_WINRM_PROBE_TIMEOUT_ENV == "MRC_WINRM_PROBE_TIMEOUT_S"


# ---------------------------------------------------------------------------
# Priority paths
# ---------------------------------------------------------------------------


def test_env_path() -> None:
    got = resolve_winrm_probe_timeout_s(env={MRC_WINRM_PROBE_TIMEOUT_ENV: "12.5"})
    assert got == 12.5
    assert isinstance(got, float)


def test_env_beats_profile() -> None:
    got = resolve_winrm_probe_timeout_s(profile_value=3.0, env={MRC_WINRM_PROBE_TIMEOUT_ENV: "9"})
    assert got == 9.0


def test_profile_path() -> None:
    assert resolve_winrm_probe_timeout_s(profile_value=8.0, env={}) == 8.0


def test_profile_path_accepts_int_and_numeric_string() -> None:
    assert resolve_winrm_probe_timeout_s(profile_value=20, env={}) == 20.0
    assert resolve_winrm_probe_timeout_s(profile_value="30.5", env={}) == 30.5


def test_default_path_ignores_other_env_vars() -> None:
    got = resolve_winrm_probe_timeout_s(env={"MRC_WINRM_PROBE": "light"})
    assert got == DEFAULT_BUDGET


def test_default_path_unset_profile_and_env() -> None:
    assert resolve_winrm_probe_timeout_s(env={}) == DEFAULT_BUDGET


def test_env_none_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MRC_WINRM_PROBE_TIMEOUT_ENV, "17")
    assert resolve_winrm_probe_timeout_s() == 17.0


def test_env_none_ignores_ambient_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MRC_WINRM_PROBE_TIMEOUT_ENV, raising=False)
    assert resolve_winrm_probe_timeout_s(profile_value=4.0) == 4.0


def test_custom_env_mapping_does_not_touch_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MRC_WINRM_PROBE_TIMEOUT_ENV, "99")
    assert resolve_winrm_probe_timeout_s(env={}) == DEFAULT_BUDGET
    assert resolve_winrm_probe_timeout_s(env={MRC_WINRM_PROBE_TIMEOUT_ENV: "2"}) == 2.0


# ---------------------------------------------------------------------------
# Invalid values fall back instead of raising
# ---------------------------------------------------------------------------

_INVALID_ENV_VALUES = ["", "   ", "abc", "nan", "NaN", "inf", "-inf", "Infinity", "0", "-2", "0.0"]


@pytest.mark.parametrize("raw", _INVALID_ENV_VALUES)
def test_invalid_env_falls_back_to_default(raw: str) -> None:
    got = resolve_winrm_probe_timeout_s(env={MRC_WINRM_PROBE_TIMEOUT_ENV: raw})
    assert got == DEFAULT_BUDGET


@pytest.mark.parametrize(
    "value",
    [None, 0, 0.0, -1, -0.5, float("nan"), float("inf"), float("-inf"), "abc", "", object(), [], {}],
)
def test_invalid_profile_falls_back_to_default(value: object) -> None:
    assert resolve_winrm_probe_timeout_s(profile_value=value, env={}) == DEFAULT_BUDGET


def test_invalid_env_falls_through_to_valid_profile() -> None:
    # A junk env token is treated as "unset" (same policy as MRC_WINRM_PROBE),
    # so a valid profile value still applies.
    got = resolve_winrm_probe_timeout_s(profile_value=6.0, env={MRC_WINRM_PROBE_TIMEOUT_ENV: "nope"})
    assert got == 6.0


def test_invalid_env_and_invalid_profile_falls_to_default() -> None:
    got = resolve_winrm_probe_timeout_s(profile_value=-3, env={MRC_WINRM_PROBE_TIMEOUT_ENV: "junk"})
    assert got == DEFAULT_BUDGET


def test_bool_profile_value_is_rejected() -> None:
    # float(True) would be a silent 1s budget; a bool is never a real timeout.
    assert resolve_winrm_probe_timeout_s(profile_value=True, env={}) == DEFAULT_BUDGET
    assert resolve_winrm_probe_timeout_s(profile_value=False, env={}) == DEFAULT_BUDGET


def test_never_raises_on_hostile_input() -> None:
    # 10**400 overflows float(); bytes / containers raise TypeError/ValueError.
    for value in (10**400, b"abc", b"bytes", object(), [], {}, ()):
        assert resolve_winrm_probe_timeout_s(profile_value=value, env={}) == DEFAULT_BUDGET


def test_positive_finite_values_pass_through_unchanged() -> None:
    for value in (0.001, 1, 3.4, 8.7, 600):
        assert resolve_winrm_probe_timeout_s(profile_value=value, env={}) == float(value)


# ---------------------------------------------------------------------------
# Pre-existing probe-mode resolution is unaffected by this task
# ---------------------------------------------------------------------------


def test_probe_mode_env_still_wins() -> None:
    assert resolve_winrm_open_probe_mode(env={"MRC_WINRM_PROBE": "light"}) == "light"


def test_probe_mode_default_still_full() -> None:
    assert resolve_winrm_open_probe_mode(env={}) == "full"


def test_probe_timeout_env_does_not_leak_into_mode() -> None:
    assert resolve_winrm_open_probe_mode(env={MRC_WINRM_PROBE_TIMEOUT_ENV: "30"}) == "full"


def test_mode_and_budget_resolvers_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_WINRM_PROBE", "skip")
    monkeypatch.setenv(MRC_WINRM_PROBE_TIMEOUT_ENV, "11")
    assert resolve_winrm_open_probe_mode(env=os.environ) == "skip"
    assert resolve_winrm_probe_timeout_s(env=os.environ) == 11.0
