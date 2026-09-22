"""WinRM / pypsrp operation and read timeout resolution."""

from __future__ import annotations

import math
from typing import Any

# Grace added on top of connect_timeout when wrapping the connector in
# ``_run_blocking_with_timeout``. pypsrp ``connection_timeout`` only covers
# the HTTP handshake after Client construction / DNS / SSL setup; a hung
# injectable connector or stalled DNS would otherwise pin the registry
# per-name lock indefinitely. Aligns with SSH ``bridge_timeout``.
_BRIDGE_TIMEOUT_GRACE_S = 5.0

# Wall-clock budget for ``open_runspace`` / ``close_runspace`` so a blackholed
# WSMan peer (``pool.open`` / ``pool.close`` -> ``wsman.delete``) cannot hang
# the calling thread. Same magnitude as SSH dispose / bridge grace.
_RUNSPACE_OPEN_CLOSE_TIMEOUT_S = _BRIDGE_TIMEOUT_GRACE_S

# Wall-clock budget for best-effort ``session.close`` in
# ``WinRMTransport._dispose_prior_session``. A hung WSMan teardown must not
# pin transport ``_op_lock`` after mark_dead / identity fail / hard timeout.
# Timeout abandons the handle (``_session`` is already cleared) and continues.
_SESSION_CLOSE_TIMEOUT_S = _BRIDGE_TIMEOUT_GRACE_S

# pypsrp builds the HTTP transport with ``http_timeout = timeout + 2``, where
# ``timeout`` is the WSMan OperationTimeout. Mirror that slack when deriving a
# read timeout: the HTTP read timeout MUST exceed the WSMan OperationTimeout.
# With equal values the client-side read timeout fires first, so the caller
# sees a ``ReadTimeout`` that is indistinguishable from a lost link instead of
# a clean server-side operation timeout.
PYPSRP_HTTP_TIMEOUT_SLACK_S: int = 2

# pypsrp's own default WSMan ``OperationTimeout``. Used only when a profile
# sets ``read_timeout_s`` without ``operation_timeout_s`` and the call has no
# wall-clock budget: the live session then keeps this value, so it is the op
# the operator's read timeout has to outlast.
PYPSRP_DEFAULT_OPERATION_TIMEOUT_S: int = 20

# Ceiling for pypsrp ``reconnection_retries``. Every retry re-sends the request
# after a urllib3 backoff, so an unbounded value would multiply the caller's
# wall-clock budget without bound; a small ceiling still rides out a transient
# link blip.
_MAX_RECONNECT_RETRIES = 10

_DEFAULT_RECONNECT_RETRIES = 2
_DEFAULT_RECONNECT_BACKOFF = 0.5


def resolve_pypsrp_op_read_timeouts(
    *,
    timeout_s: float | None = None,
    operation_timeout_s: int | None = None,
    read_timeout_s: int | None = None,
) -> tuple[int | None, int | None]:
    """Resolve effective pypsrp ``operation_timeout`` / ``read_timeout`` (seconds).

    Whole seconds only (pypsrp Client/WSMan contract). Per field, an explicit
    profile/transport value wins; otherwise a positive call wall-clock
    ``timeout_s`` derives ``max(ceil(timeout_s), 1)`` so library defaults (20/30)
    are not badly decoupled from CLI/exec budgets; otherwise ``None``, leaving
    connect-time / library defaults alone rather than forcing an extremely short
    op/read.

    Ordering invariant: the HTTP read timeout outlasts the WSMan operation
    timeout by :data:`PYPSRP_HTTP_TIMEOUT_SLACK_S`, because pypsrp builds the
    HTTP transport with ``http_timeout = timeout + 2``. Equal or inverted values
    make the client read-timeout fire first, surfacing a ``ReadTimeout`` that
    looks like a lost link (and is classified as one) instead of a clean
    server-side operation timeout. Every combination returned here satisfies it,
    including a pair the operator configured with the op above the read.

    An explicit ``read_timeout_s`` is the operator's ceiling on one HTTP
    exchange, so the operation timeout is what bends to it: a derived or
    explicit op is capped at ``read_timeout_s - slack``, and with neither the op
    is capped against pypsrp's default
    (:data:`PYPSRP_DEFAULT_OPERATION_TIMEOUT_S`). An explicit op above the
    ceiling is dead configuration - the client stops waiting before the
    server-side op can fire - so bending it cannot lose an observable exchange,
    while forwarding it would tear the endpoint down over a self-inflicted
    ordering mistake and a command still running remotely could then be
    repeated. Only a read too small to admit any positive op
    (``read_timeout_s <= 1``) is raised, to ``1 + slack``.

    A non-positive or non-numeric explicit value means **unset**, not a small
    number, and callers that hand values to pypsrp themselves must apply the
    same meaning (see ``WinRMTransport.connect_kwargs``): one profile must not
    yield two different timeouts.

    Returns ``(operation_timeout, read_timeout)``, each ``int | None``.
    """
    derived: int | None = None
    if timeout_s is not None:
        try:
            budget = float(timeout_s)
        except (TypeError, ValueError):
            budget = float("nan")
        # Finite and positive only; NaN/Inf/<=0 -> no derivation.
        if math.isfinite(budget) and budget > 0:
            derived = max(int(math.ceil(budget)), 1)

    def _explicit(value: int | float | None) -> int | None:
        """Coerce an explicit profile/call value; ``None`` means "unset".

        A non-positive value is "unset" rather than a duration, matching how
        the config layer accepts a placeholder like ``0`` (see the module
        docstring) - the field then keeps the derived/library default.
        """
        if value is None:
            return None
        try:
            n = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if n <= 0:
            return None
        return n

    op_explicit = _explicit(operation_timeout_s)
    rd_explicit = _explicit(read_timeout_s)
    op = op_explicit if op_explicit is not None else derived
    if rd_explicit is not None:
        rd = rd_explicit
        # Largest op the explicit read timeout can outlast. Applied to the op
        # whatever its source: an op that outlasts the read is dead
        # configuration (the client stops waiting first) and forwarding it
        # would rebuild the client-read-first ordering the invariant exists to
        # prevent - that ReadTimeout is classified link_fatal, so a slow-but-
        # healthy command would tear the endpoint down.
        ceiling = max(rd - PYPSRP_HTTP_TIMEOUT_SLACK_S, 1)
        if op is not None:
            op = min(op, ceiling)
        else:
            op = min(PYPSRP_DEFAULT_OPERATION_TIMEOUT_S, ceiling)
        if rd < op + PYPSRP_HTTP_TIMEOUT_SLACK_S:
            # No positive op fits under this read; the read is the only
            # value left that can move.
            rd = op + PYPSRP_HTTP_TIMEOUT_SLACK_S
    elif op is not None:
        rd = op + PYPSRP_HTTP_TIMEOUT_SLACK_S
    else:
        rd = None
    return op, rd


def _coerce_positive_int(value: Any, fallback: int) -> int:
    """Coerce *value* to a positive int, falling back to *fallback* if invalid.

    ``None``, non-numeric strings, NaN/Inf, and ``bool`` (a subclass of ``int``
    that users read as a flag, not a count) are all invalid. Floats truncate.
    """
    if value is None or isinstance(value, bool):
        return fallback
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if n <= 0:
        return fallback
    return n


def _coerce_finite_float(value: Any, fallback: float) -> float:
    """Coerce *value* to a finite non-negative float, else return *fallback*."""
    if value is None or isinstance(value, bool):
        return fallback
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(n) or n < 0:
        return fallback
    return n


def resolve_winrm_reconnect(
    retries: Any = None,
    backoff: Any = None,
    *,
    default_retries: int = _DEFAULT_RECONNECT_RETRIES,
    default_backoff: float = _DEFAULT_RECONNECT_BACKOFF,
) -> tuple[int, float] | None:
    """Resolve pypsrp WSMan ``(reconnection_retries, reconnection_backoff)``.

    Values feed ``pypsrp.wsman.WSMan(reconnection_retries=...,
    reconnection_backoff=...)``. Those parameters configure a urllib3 ``Retry``
    whose exception/connect retries do apply to the WinRM POST, while status
    retries never do: ``Retry.DEFAULT_ALLOWED_METHODS`` excludes ``POST``, so a
    non-2xx response is surfaced to the caller rather than retried here.
    Recovering from an HTTP-rejected request is therefore the transport's job
    (re-handshake, then retry once), not the retry policy's.

    ``None`` / invalid / non-finite input -> the defaults. ``retries <= 0`` is
    read as an explicit opt-out and returns ``None`` so callers pass no kwarg
    and the library default (0 retries) stands. ``backoff`` must be finite and
    ``>= 0``; anything else falls back to the default. An oversized ``retries``
    is clamped to a sane ceiling.
    """
    safe_retries = _coerce_positive_int(default_retries, _DEFAULT_RECONNECT_RETRIES)
    safe_backoff = _coerce_finite_float(default_backoff, _DEFAULT_RECONNECT_BACKOFF)

    if retries is None or isinstance(retries, bool):
        n = safe_retries
    else:
        try:
            n = int(retries)
        except (TypeError, ValueError, OverflowError):
            n = safe_retries
        else:
            if n <= 0:
                # Explicit opt-out: no reconnection kwargs reach pypsrp.
                return None
    if n > _MAX_RECONNECT_RETRIES:
        n = _MAX_RECONNECT_RETRIES

    delay = _coerce_finite_float(backoff, safe_backoff)
    return n, delay


def _session_wsman(session: Any | None) -> Any | None:
    """Locate a pypsrp-like ``WSMan`` on an adapted or raw WinRM session."""
    if session is None:
        return None
    wsman = getattr(session, "wsman", None)
    if wsman is not None:
        return wsman
    raw = getattr(session, "raw", None)
    if raw is None:
        return None
    wsman = getattr(raw, "wsman", None)
    if wsman is not None:
        return wsman
    # PypsrpClientAdapter keeps the real Client on ``_client``.
    client = getattr(raw, "_client", None)
    if client is not None:
        return getattr(client, "wsman", None)
    return None


def _push_wsman_timeouts(
    wsman: Any,
    *,
    operation_timeout: int | None,
    read_timeout: int | None,
    restores: list[tuple[Any, str, Any]],
) -> None:
    """Best-effort set op/read on *wsman* (and its HTTP transport); record restores."""
    if operation_timeout is not None and hasattr(wsman, "operation_timeout"):
        restores.append(
            (wsman, "operation_timeout", getattr(wsman, "operation_timeout", None))
        )
        try:
            wsman.operation_timeout = int(operation_timeout)
        except Exception:  # noqa: BLE001 - best-effort alignment only
            restores.pop()

    if read_timeout is None:
        return
    transport = getattr(wsman, "transport", None)
    if transport is not None and hasattr(transport, "read_timeout"):
        restores.append(
            (transport, "read_timeout", getattr(transport, "read_timeout", None))
        )
        try:
            transport.read_timeout = int(read_timeout)
        except Exception:  # noqa: BLE001 - best-effort
            restores.pop()
        return
    # Some doubles put read_timeout on the wsman-like object itself.
    if hasattr(wsman, "read_timeout"):
        restores.append(
            (wsman, "read_timeout", getattr(wsman, "read_timeout", None))
        )
        try:
            wsman.read_timeout = int(read_timeout)
        except Exception:  # noqa: BLE001 - best-effort
            restores.pop()

