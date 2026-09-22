"""Unit tests: pypsrp op/read slack invariant and reconnect param resolution."""

from __future__ import annotations

import math

import pytest

from mcp_remote_control.transport.winrm_timeouts import (
    PYPSRP_HTTP_TIMEOUT_SLACK_S,
    resolve_pypsrp_op_read_timeouts,
    resolve_winrm_reconnect,
)


# ---------------------------------------------------------------------------
# PYPSRP_HTTP_TIMEOUT_SLACK_S invariant
# ---------------------------------------------------------------------------


def test_slack_constant_is_positive_int() -> None:
    """Slack mirrors pypsrp's own ``http_timeout = timeout + 2``."""
    assert PYPSRP_HTTP_TIMEOUT_SLACK_S == 2
    assert isinstance(PYPSRP_HTTP_TIMEOUT_SLACK_S, int)


# ---------------------------------------------------------------------------
# resolve_pypsrp_op_read_timeouts - read outlives op
# ---------------------------------------------------------------------------


def test_derived_read_exceeds_op_by_slack() -> None:
    """Derived read is op + slack so the HTTP read timeout never fires first."""
    assert resolve_pypsrp_op_read_timeouts(timeout_s=1) == (1, 3)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=60) == (60, 62)


def test_derived_read_uses_ceiled_whole_seconds() -> None:
    """Fractional budgets ceil to whole seconds before slack is added."""
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5) == (5, 7)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5.0) == (5, 7)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5.01) == (6, 8)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=0.5) == (1, 3)


def test_derived_slack_holds_for_every_positive_budget() -> None:
    """Invariant: read > op for all derived pairs (no equal-value self-harm)."""
    for budget in (0.1, 1, 2, 3.7, 10, 60, 3600):
        op, rd = resolve_pypsrp_op_read_timeouts(timeout_s=budget)
        assert op is not None and rd is not None
        assert rd == op + PYPSRP_HTTP_TIMEOUT_SLACK_S
        assert rd > op


def test_explicit_profile_read_wins_over_derived() -> None:
    """Profile read_timeout_s still overrides the derived value."""
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5, read_timeout_s=88) == (5, 88)


def test_explicit_op_wins_and_read_derives_from_effective_op() -> None:
    """Explicit op drives the derived read (gap must clear the real op value)."""
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=5, operation_timeout_s=60
    ) == (60, 62)


def test_explicit_both_wins_over_derivation() -> None:
    """Both explicit -> no derivation; the profile read still caps the op."""
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=5, operation_timeout_s=60, read_timeout_s=88
    ) == (60, 88)


def test_explicit_op_without_timeout_still_gets_read_slack() -> None:
    """An explicit op alone yields read > op even with no call timeout."""
    assert resolve_pypsrp_op_read_timeouts(operation_timeout_s=120) == (120, 122)


def test_neither_applies_returns_none_pair() -> None:
    """No timeout and no profile values -> leave library defaults alone."""
    assert resolve_pypsrp_op_read_timeouts(timeout_s=None) == (None, None)
    assert resolve_pypsrp_op_read_timeouts() == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=0) == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=-1) == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=float("nan")) == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=float("inf")) == (None, None)


def test_invalid_explicit_values_fall_back_to_derivation() -> None:
    """Non-positive/garbage explicit values do not suppress derivation."""
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=4, operation_timeout_s=0, read_timeout_s="junk"
    ) == (4, 6)


# ---------------------------------------------------------------------------
# resolve_winrm_reconnect - defaults, opt-out, clamping
# ---------------------------------------------------------------------------


def test_reconnect_defaults_when_unset() -> None:
    """Unset/invalid input resolves to the documented defaults."""
    assert resolve_winrm_reconnect(None, None) == (2, 0.5)
    assert resolve_winrm_reconnect() == (2, 0.5)


def test_reconnect_non_numeric_falls_back_to_defaults() -> None:
    """Garbage config values never raise; they resolve to defaults."""
    assert resolve_winrm_reconnect("abc", "abc") == (2, 0.5)
    assert resolve_winrm_reconnect(float("nan"), float("nan")) == (2, 0.5)
    assert resolve_winrm_reconnect(float("inf"), float("inf")) == (2, 0.5)
    assert resolve_winrm_reconnect(True, True) == (2, 0.5)


def test_reconnect_zero_or_negative_disables() -> None:
    """retries <= 0 is an explicit opt-out -> None (pypsrp default 0 stands)."""
    assert resolve_winrm_reconnect(0, 0.5) is None
    assert resolve_winrm_reconnect(-1, 0.5) is None
    assert resolve_winrm_reconnect(-3, None) is None


def test_reconnect_zero_disables_regardless_of_backoff() -> None:
    """A disabled retry policy ignores an otherwise valid backoff."""
    assert resolve_winrm_reconnect(0, 5) is None


def test_reconnect_positive_values_pass_through() -> None:
    """Configured values reach pypsrp unchanged."""
    assert resolve_winrm_reconnect(3, 1.5) == (3, 1.5)
    assert resolve_winrm_reconnect(1, 0) == (1, 0.0)
    assert resolve_winrm_reconnect("4", "2.5") == (4, 2.5)


def test_reconnect_retries_clamped_to_ceiling() -> None:
    """Oversized retries are clamped so the wall-clock cost stays bounded."""
    assert resolve_winrm_reconnect(1000, 0.5) == (10, 0.5)
    assert resolve_winrm_reconnect(10 ** 9, 0.5) == (10, 0.5)
    # At the ceiling the value is untouched.
    assert resolve_winrm_reconnect(10, 0.5) == (10, 0.5)


def test_reconnect_negative_backoff_falls_back() -> None:
    """Backoff must be finite and >= 0; negatives use the default."""
    n, backoff = resolve_winrm_reconnect(3, -1)
    assert n == 3
    assert backoff == 0.5


def test_reconnect_custom_defaults_are_honoured() -> None:
    """Callers can override the fallback pair."""
    assert resolve_winrm_reconnect(None, None, default_retries=5, default_backoff=2.0) == (
        5,
        2.0,
    )
    assert resolve_winrm_reconnect("bad", None, default_retries=1, default_backoff=0.25) == (
        1,
        0.25,
    )


def test_reconnect_invalid_defaults_fall_back_to_module_defaults() -> None:
    """A bogus caller-supplied default still yields a usable pair."""
    assert resolve_winrm_reconnect(
        None, None, default_retries="junk", default_backoff=float("nan")
    ) == (2, 0.5)


@pytest.mark.parametrize("retries", [None, "x", float("nan"), 0, -2])
def test_reconnect_never_raises(retries: object) -> None:
    """Resolution is total: every input either yields a pair or None."""
    out = resolve_winrm_reconnect(retries, None)
    assert out is None or (
        isinstance(out[0], int)
        and isinstance(out[1], float)
        and math.isfinite(out[1])
        and out[1] >= 0
    )
