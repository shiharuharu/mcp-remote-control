"""Ps Core ops: open, invoke, and close persistent PowerShell runspaces.

WinRM-only (``caps.ps``). Sessions share runspace state across invokes and
are pruned from the registry when the transport reports a dead connection.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, cast

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound
from mcp_remote_control.core.result import OpResult, _home, _short
from mcp_remote_control.endpoint.registry import (
    connect_failure_fields,
    ensure_endpoint,
    get_registry,
    retire_refused_link,
)
from mcp_remote_control.ps.registry import get_ps_registry
from mcp_remote_control.ps.session import PsSession
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import normalize_timeout_s as _normalize_timeout
from mcp_remote_control.transport.winrm import (
    RunspaceResult,
    # Close verdicts: a teardown status must never be inferred from a
    # best-effort close helper that cannot report one.
    _CLOSE_LANDED,
    _CLOSE_TIMEOUT,
    _CLOSE_UNCONFIRMED,
)

VALID_OPS: frozenset[str] = frozenset({"open", "invoke", "close"})

_log = logging.getLogger(__name__)

# PS sessions live only in this process's registry. A separate CLI process
# never sees ids from a prior open - PS_NOT_FOUND is expected, not a bug.
_PS_PROCESS_LOCAL_HINT = (
    "ps sessions are process-local (same process only; "
    "a new CLI process always gets PS_NOT_FOUND \u2014 no daemon); "
    "open then invoke/close in one process: "
    "mcp-remote-control-cli ps open --ep <winrm-profile>"
)

# Reason the transport records when it tears down a link it could not
# re-handshake (see WinRMTransport._mark_link_dead).
_LINK_LOST_REASON = "link lost"

# A runspace lives in the transport's local WSMan session. When that session is
# torn down the runspace and every PowerShell variable/function it held are gone,
# so the session id must never be presented as reusable - recovery is a fresh
# endpoint open followed by ps open.
_PS_REOPEN_ADVICE = (
    "(this session is not reusable); recover with endpoint open then ps open: "
    "mcp-remote-control-cli endpoint open --profile <name>, then "
    "mcp-remote-control-cli ps open --ep <winrm-profile>"
)

_PS_LINK_LOST_HINT = (
    "endpoint link was lost: the local WinRM session was closed and the "
    "runspace did not survive it " + _PS_REOPEN_ADVICE
)

# Same consequence on a different cause: the transport was already marked dead
# (peer reset, hard timeout, ...) and its local session went down with it. The
# recovery is identical, but the wording must not claim a link loss that the
# transport never recorded.
_PS_DEAD_TRANSPORT_HINT = (
    "endpoint transport is not connected: the local WinRM session was torn "
    "down and the runspace did not survive it " + _PS_REOPEN_ADVICE
)

# A top-level ``exit`` in the user script ends the pipeline before the appended
# probes write their markers, so nothing carries this invoke's exit code or
# current location. The tool must say so instead of defaulting to a successful
# exit 0 with the previously known cwd.
_PS_PROBE_MISSING_HINT = (
    "the exit/location probe did not run: a top-level exit ended the script "
    "before it, so exit=-1 means unknown and cwd is the last known location, "
    "not this invoke's; drop the exit or use exec"
)


class SupportsRunspace(Protocol):
    """Transport surface required for persistent PowerShell runspaces."""

    def open_runspace(self) -> Any: ...

    def runspace_invoke(
        self,
        handle: Any,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> Any: ...

    # Returns a close verdict (a string on WinRMTransport, see its
    # ``close_runspace``); mock transports may return None. A transport that
    # can also take a caller-owned deadline exposes ``close_runspace_within``
    # (optional), which is how a ps close keeps lock wait, delete and recovery
    # on one budget.
    def close_runspace(self, handle: Any) -> Any: ...


def open_session(
    *,
    ep: str | None = None,
    home: Path | str | None = None,
    connector: Any | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Open a persistent PowerShell runspace on *ep* (winrm / caps.ps only)."""
    if not ep or not str(ep).strip():
        return OpResult(
            kind="ps",
            status="error",
            code="MISSING_ARG",
            fields={"op": "open", "msg": "ep is required"},
            hint="mcp-remote-control-cli ps open --ep <winrm-profile>",
        )

    ep_name = str(ep).strip()
    home_path = _home(home)

    try:
        endpoint = ensure_endpoint(
            ep_name,
            home=home_path,
            connector=connector,
        )
    except ProfileNotFound as exc:
        return OpResult(
            kind="ps",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "open", "ep": ep_name, "msg": _short(str(exc))},
        )
    except ProfileInvalid as exc:
        return OpResult(
            kind="ps",
            status="error",
            code="PROFILE_INVALID",
            fields={"op": "open", "ep": ep_name, "msg": _short(str(exc))},
        )
    except TransportError as exc:
        return _transport_error(exc, op="open", ep=ep_name)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="ps",
            status="error",
            code="CONNECT_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
        )

    if not endpoint.caps.get("ps", False):
        return OpResult(
            kind="ps",
            status="error",
            code="CAP_DENIED",
            fields={
                "op": "open",
                "ep": ep_name,
                "transport": endpoint.transport_name,
                "msg": (
                    "endpoint lacks ps capability "
                    "(persistent PowerShell is winrm-only)"
                ),
                "caps": endpoint.caps_token,
            },
            hint="use exec or screen on local/ssh; ps requires winrm",
        )

    transport = endpoint.transport
    if transport is None or not transport.is_connected():
        return OpResult(
            kind="ps",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": "endpoint transport not connected",
            },
        )

    if not hasattr(transport, "open_runspace"):
        return OpResult(
            kind="ps",
            status="error",
            code="UNSUPPORTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "transport": endpoint.transport_name,
                "msg": f"transport {endpoint.transport_name!r} has no runspace API",
            },
            hint="ps open requires WinRMTransport open_runspace",
        )

    # When probe populated winrm_ps and runspaces are disabled, fail closed.
    # Absent winrm_ps (probe=False / lab) keeps the historical allow path.
    winrm_ps = (getattr(transport, "meta", None) or {}).get("winrm_ps")
    if isinstance(winrm_ps, dict) and winrm_ps.get("ps_runspace") is False:
        lang_mode = winrm_ps.get("language_mode")
        fields: dict[str, Any] = {
            "op": "open",
            "ep": ep_name,
            "transport": endpoint.transport_name,
            "msg": (
                "PowerShell runspace unsupported on this endpoint "
                "(ps_runspace=false)"
            ),
        }
        if lang_mode is not None and str(lang_mode).strip():
            fields["lang_mode"] = str(lang_mode).strip()
        return OpResult(
            kind="ps",
            status="error",
            code="UNSUPPORTED",
            fields=fields,
            hint=(
                "persistent PowerShell requires FullLanguage "
                "(ConstrainedLanguage / JEA NoLanguage block runspaces); "
                "use exec oneshot scripts or raise host language mode"
            ),
        )

    runspace = cast(SupportsRunspace, transport)
    try:
        handle = runspace.open_runspace()
    except TransportError as exc:
        # Same rule as exec: a refusal the transport's taxonomy does not
        # classify (a WSMan 401 that surfaces as a plain auth error) leaves a
        # live-but-poisoned session registered, and every later op fails
        # identically until something retires it.
        retire_refused_link(transport, exc)
        return _transport_error_with_link(
            exc,
            op="open",
            ep=ep_name,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="ps",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"open_runspace failed: {type(exc).__name__}: {exc}"),
            },
        )

    # open_runspace may outlive concurrent close/retire. Re-pin
    # generation + transport liveness before registering a PS session.
    if not get_registry().generation_still_open(endpoint):
        closer = getattr(transport, "close_runspace", None)
        if callable(closer):
            try:
                closer(handle)
            except Exception:  # noqa: BLE001
                pass
        else:
            raw_close = getattr(handle, "close", None)
            if callable(raw_close):
                try:
                    raw_close()
                except Exception:  # noqa: BLE001
                    pass
        return OpResult(
            kind="ps",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": "endpoint closed or transport died during ps open",
            },
        )

    # Seed location from the runspace handle, then transport/endpoint cwd.
    location = (
        getattr(handle, "location", None)
        or getattr(transport, "cwd", None)
        or endpoint.cwd
    )
    if isinstance(location, str):
        location = location.strip() or None

    reg = get_ps_registry()
    sid = reg.allocate_id()
    session = PsSession(
        id=sid,
        ep=ep_name,
        handle=handle,
        transport=transport,
        location=location,
    )
    reg.add(session)
    # Post-add fence: concurrent close may have disposed the generation
    # between the pre-check and reg.add - drop the zombie immediately.
    if not get_registry().generation_still_open(endpoint):
        try:
            reg.remove(sid)
        except Exception:  # noqa: BLE001
            pass
        return OpResult(
            kind="ps",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": "endpoint closed or transport died during ps open register",
            },
        )

    return OpResult(
        kind="ps",
        status="ok",
        cwd=location,
        fields={
            "op": "open",
            "id": sid,
            "session_id": sid,
            "ep": ep_name,
            "transport": endpoint.transport_name,
        },
    )


def invoke(
    *,
    id: str | None = None,
    session_id: str | None = None,
    script: str | None = None,
    timeout: float | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Run *script* in an open PS session; shared state across invokes.

    *timeout* is a wall-clock budget in seconds forwarded as ``timeout_s`` to
    the transport (same contract as exec): ``None`` means unlimited; positive
    finite values are enforced; non-positive (including 0), unparseable, and
    non-finite (NaN / +/-Inf) are rejected as ``INVALID_ARG``. On timeout the
    result uses ``status='timeout'`` with ``fields.timed_out=True`` (exit -1
    when the transport reports it); the pipeline is stopped and the runspace
    may stay reusable. WinRM sync cancel can still leave a stuck remote shell:
    fall back to endpoint close+reopen if timeouts recur - this is not a
    remote-kill guarantee. The stopped pipeline never reported a location, so
    a timed-out result also carries ``fields.cwd_stale=True`` and ``cwd`` is
    the transport's last known location - the runspace's last probed one, or
    the transport's configured working directory when the handle has none to
    remember - not this invoke's, and not necessarily ever probe-confirmed.

    A top-level ``exit`` in *script* ends the pipeline before the probes
    appended for this invoke can report: the result is ``status='fail'`` with
    ``fields.exit=-1`` (unknown, not zero), ``fields.probe='missing'`` and
    ``fields.cwd_stale=True``. A top-level ``return`` does **not** skip them:
    the caller's text is dot-sourced as its own block here and on the oneshot
    exec path, so ``return`` ends only that block and the probes still run
    (measured on pwsh 7.4: ``. { & /bin/sh -c 'exit 7'; return }`` still emits
    the exit marker ``7``, while the same text ending in ``exit 7`` emits
    none).
    """
    sid = id or session_id
    if not sid or not str(sid).strip():
        return OpResult(
            kind="ps",
            status="error",
            code="MISSING_ARG",
            fields={"op": "invoke", "msg": "session id required (id=)"},
            hint="mcp-remote-control-cli ps invoke --id <session_id> --script '...'",
        )
    sid = str(sid).strip()

    if script is None:
        return OpResult(
            kind="ps",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "invoke",
                "id": sid,
                "msg": "script is required",
            },
            hint="mcp-remote-control-cli ps invoke --id <id> --script 'Get-Location'",
        )

    sess = get_ps_registry().get(sid)
    if sess is None or sess.closed:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_NOT_FOUND",
            fields={
                "op": "invoke",
                "id": sid,
                "msg": f"ps session not open: {sid}",
            },
            hint=_PS_PROCESS_LOCAL_HINT,
        )

    transport = sess.transport
    if transport is None:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_CLOSED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": sess.ep,
                "msg": "ps session has no transport",
            },
        )

    # Re-check session/handle, invoke, and location update share one serial
    # critical section so close cannot release the handle mid-invoke.
    serial = getattr(transport, "serial_ops", None)
    if callable(serial):
        with serial():
            return _invoke_on_session(sid, str(script), timeout)
    return _invoke_on_session(sid, str(script), timeout)


def close_session(
    *,
    id: str | None = None,
    session_id: str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Close and invalidate a PS session.

    Unregisters first, then reports the verdict of the session's own teardown.
    ``PsSession.close`` is one budgeted critical section - the wait for the
    transport serial lock, the WSMan ``Delete`` and any refusal-recovery all
    share a single deadline - and the transport's verdict is what this op
    reports. Nothing re-enters the handle after it, so a second round can
    neither overlap another op on the endpoint nor spend a fresh budget.

    The status distinguishes the outcomes of that single teardown: ``ok``
    (delete confirmed, nothing to delete, or the server answered that the shell
    no longer exists - the wanted end state either way), ``timeout`` (the
    wall-clock wait was abandoned, lock wait included) and ``error`` /
    ``PS_CLOSE_UNCONFIRMED`` (a fault that leaves the runspace's fate unknown:
    the delete is not proven to have landed). ``closed=True`` means only that
    this process released the session.
    """
    sid = id or session_id
    if not sid or not str(sid).strip():
        return OpResult(
            kind="ps",
            status="error",
            code="MISSING_ARG",
            fields={"op": "close", "msg": "session id required (id=)"},
            hint="mcp-remote-control-cli ps close --id <session_id>",
        )
    sid = str(sid).strip()
    reg = get_ps_registry()
    sess = reg.get(sid)
    if sess is None:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_NOT_FOUND",
            fields={
                "op": "close",
                "id": sid,
                "msg": f"ps session not open: {sid}",
            },
            hint=_PS_PROCESS_LOCAL_HINT,
        )
    fields: dict[str, Any] = {
        "op": "close",
        "id": sid,
        "session_id": sid,
        "ep": sess.ep,
        "closed": True,
    }
    # Pop is the gate: a concurrent close of the same id loses it here. The
    # pop also runs the session's close, i.e. the whole budgeted teardown.
    sess = reg.remove(sid)
    if sess is None:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_NOT_FOUND",
            fields={
                "op": "close",
                "id": sid,
                "msg": f"ps session not open: {sid}",
            },
            hint=_PS_PROCESS_LOCAL_HINT,
        )
    # Session is unregistered either way. The verdict its close recorded is
    # the single teardown result: a hung handle or an unlanded delete is never
    # a clean ok, and no confirmation pass re-runs the close to find out.
    verdict = sess.close_verdict or _CLOSE_LANDED
    if verdict == _CLOSE_TIMEOUT:
        fields["timed_out"] = True
        fields["msg"] = "runspace close timed out; session unregistered"
        return OpResult(
            kind="ps",
            status="timeout",
            cwd=sess.location,
            fields=fields,
        )
    if verdict == _CLOSE_UNCONFIRMED:
        fields["msg"] = (
            "runspace delete not confirmed: the remote runspace may still be "
            "allocated (the session is unregistered here)"
        )
        return OpResult(
            kind="ps",
            status="error",
            code="PS_CLOSE_UNCONFIRMED",
            cwd=sess.location,
            fields=fields,
            hint=(
                "the WSMan Delete was rejected and not proven to have landed; "
                "the endpoint re-handshakes on the next op, then re-issue "
                "ps open. Treat the host's runspace count as possibly stale"
            ),
        )
    return OpResult(
        kind="ps",
        status="ok",
        cwd=sess.location,
        fields=fields,
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch ps op -> Core implementation."""
    op_norm = (op or "").strip().lower()
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="ps",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown ps op (want open|invoke|close)",
            },
            hint="use op=open|invoke|close",
        )
    dispatch = {
        "open": open_session,
        "invoke": invoke,
        "close": close_session,
    }
    return dispatch[op_norm](**kwargs)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _invoke_on_session(
    sid: str,
    script: str,
    timeout: float | None,
) -> OpResult:
    """Invoke on a live session. Caller holds ``serial_ops()`` when present."""
    live = get_ps_registry().get(sid)
    if live is None or live.closed:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_NOT_FOUND",
            fields={
                "op": "invoke",
                "id": sid,
                "msg": f"ps session not open: {sid}",
            },
            hint=_PS_PROCESS_LOCAL_HINT,
        )
    handle = live.handle
    transport = live.transport
    if handle is None:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_NOT_FOUND",
            fields={
                "op": "invoke",
                "id": sid,
                "msg": f"ps session not open: {sid}",
            },
            hint=_PS_PROCESS_LOCAL_HINT,
        )
    if transport is None:
        return OpResult(
            kind="ps",
            status="error",
            code="PS_CLOSED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": live.ep,
                "msg": "ps session has no transport",
            },
        )

    # Fail before runspace_invoke: a still-registered session on a dead
    # transport (mark_dead without ensure/reconnect) must not block on a
    # blackholed WSMan handle. Prune without handle.close - that path can
    # hang the same way.
    is_conn = getattr(transport, "is_connected", None)
    if callable(is_conn):
        try:
            connected = bool(is_conn())
        except Exception:  # noqa: BLE001
            connected = False
        if not connected:
            dropped = get_ps_registry().pop(sid)
            if dropped is not None:
                dropped.abandon()
            fields: dict[str, Any] = {
                "op": "invoke",
                "id": sid,
                "ep": live.ep,
                "msg": _dead_transport_msg(transport),
            }
            # The registered id is stale: its runspace went down with the local
            # session. Surface the transport's link marker so the Agent sees
            # that this is a dead link and not a transport that never opened.
            # The reopen advice holds for every dead transport, but only a lost
            # link may be described as one - a peer reset or hard timeout keeps
            # the neutral wording.
            link_lost = _link_lost_fields(transport)
            fields.update(link_lost)
            return OpResult(
                kind="ps",
                status="error",
                code="NOT_CONNECTED",
                fields=fields,
                hint=_PS_LINK_LOST_HINT if link_lost else _PS_DEAD_TRANSPORT_HINT,
            )

    invoker = getattr(transport, "runspace_invoke", None)
    if not callable(invoker):
        return OpResult(
            kind="ps",
            status="error",
            code="UNSUPPORTED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": live.ep,
                "msg": "transport has no runspace_invoke",
            },
        )

    try:
        timeout_s = _normalize_timeout(timeout)
    except TransportError as exc:
        return _transport_error(
            exc,
            op="invoke",
            ep=live.ep,
            session_id=sid,
        )

    try:
        result = invoker(
            handle,
            script,
            timeout_s=timeout_s,
        )
    except TransportError as exc:
        # Dropped WinRM sessions surface as NOT_CONNECTED or EXEC_FAILED.
        # Prune either so the next invoke does not re-hang on a dead handle;
        # the caller must reopen with ``ps open``. registry.remove closes the
        # session (and underlying runspace) best-effort. A transport-recorded
        # link death is the exception: that transport reports itself
        # disconnected, so the pre-check above already answers the follow-up
        # invoke with the recorded cause and the reopen path, while pruning
        # here would replace it with a bare "session not open".
        retire_refused_link(transport, exc)
        link_lost = _link_lost_fields(transport)
        if not link_lost and exc.code in {"NOT_CONNECTED", "EXEC_FAILED"}:
            try:
                get_ps_registry().remove(sid)
            except Exception:  # noqa: BLE001
                pass
        result = _transport_error_with_link(
            exc,
            op="invoke",
            ep=live.ep,
            transport=transport,
            session_id=sid,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        # No result came back, so nothing confirmed this invoke's location: the
        # op reports the session's last known one. Mark it, as the timeout and
        # probe-missing branches do - the session is not pruned here, so the
        # caller may keep using it and must not treat this cwd as current.
        return OpResult(
            kind="ps",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": live.ep,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
                "cwd_stale": True,
            },
            cwd=live.location,
        )

    rs = _as_runspace_result(result, default_location=live.location)
    if rs.location:
        live.location = rs.location

    # The error stream carries the reason a script failed. exec merges it into
    # the body behind a [stderr] marker rather than dropping it, and ps must do
    # the same: without it a failing invoke whose stdout is non-empty shows the
    # symptom with no cause on either track. Status is unchanged by this - it
    # still comes from exit_code / had_errors below - so a clean invoke with no
    # error records keeps its body exactly as before.
    #
    # The marker is emitted for every shape, a stderr-only body included: bare
    # error text is otherwise byte-for-byte indistinguishable from a body the
    # invoke wrote to stdout, so the caller cannot tell a diagnostic from the
    # run's data. Only stdout stays unlabelled - it is the body the invoke
    # produced.
    body = rs.stdout or ""
    if rs.stderr.strip():
        err = rs.stderr.rstrip(chr(10))
        head = body.rstrip(chr(10))
        body = f"{head}\n[stderr]\n{err}" if head.strip() else f"[stderr]\n{err}"
    body = body or None

    status = "ok" if rs.exit_code == 0 and not rs.had_errors else "fail"
    fields: dict[str, Any] = {
        "op": "invoke",
        "id": sid,
        "session_id": sid,
        "ep": live.ep,
        "exit": rs.exit_code,
    }
    if rs.had_errors and rs.exit_code == 0:
        fields["exit"] = 1
        status = "fail"
    probe_missing = not rs.exit_probe_ran and not rs.timed_out
    if probe_missing:
        # The probe never ran, so there is no evidence for this invoke's exit
        # code: report it as unknown rather than as the "probe said 0" default
        # that would read as success. The location probe sits in the same
        # skipped block, so the location is the last known one, not this
        # invoke's - say that too instead of presenting it as current.
        fields["exit"] = -1
        fields["probe"] = "missing"
        fields["cwd_stale"] = True
        status = "fail"
    if rs.timed_out:
        # Align with exec: machine-readable timeout status + timed_out flag.
        # Agents must branch on status=='timeout' / fields.timed_out, not prose.
        fields["timed_out"] = True
        status = "timeout"
        # Stable exit token when transport left exit at 0 on a wall-clock stop.
        if fields.get("exit") in (0, None):
            fields["exit"] = -1
        # The deadline stopped the pipeline before the location probe reported,
        # so the transport fills ``location`` with the handle's last known one
        # rather than this invoke's. Mark it as the probe-missing branch does:
        # a caller that reads ``cwd`` after a timeout, or feeds it to a later
        # exec/fs path, would otherwise use a directory the stopped script may
        # already have left.
        fields["cwd_stale"] = True

    # A timeout stops the pipeline but leaves the runspace usable, so only a
    # completed invoke can be reporting the link death recorded during its own
    # call. The transport's own stderr already says the session was closed;
    # the token is what an Agent can branch on.
    link_lost = {} if rs.timed_out else _link_lost_fields(transport)
    fields.update(link_lost)

    if link_lost:
        hint = _PS_LINK_LOST_HINT
    elif probe_missing:
        hint = _PS_PROBE_MISSING_HINT
    else:
        hint = None

    return OpResult(
        kind="ps",
        status=status,
        cwd=live.location,
        fields=fields,
        body=body,
        hint=hint,
    )


def _as_runspace_result(
    raw: Any,
    *,
    default_location: str | None,
) -> RunspaceResult:
    """Normalize a transport invoke result into ``RunspaceResult``.

    ``exit_probe_ran`` is carried over when the source result has it and is
    assumed True otherwise: only the pooled PowerShell adapter appends the exit
    probe, so every other shape (mock runspace, duck-typed result) reports its
    own exit code rather than "probe did not run".
    """
    if isinstance(raw, RunspaceResult):
        if raw.location is None and default_location is not None:
            return RunspaceResult(
                stdout=raw.stdout,
                stderr=raw.stderr,
                exit_code=raw.exit_code,
                location=default_location,
                had_errors=raw.had_errors,
                timed_out=raw.timed_out,
                exit_probe_ran=raw.exit_probe_ran,
            )
        return raw
    # Accept any result object with stdout/exit_code attributes (mock or real).
    if hasattr(raw, "stdout"):
        exit_code = int(getattr(raw, "exit_code", 0) or 0)
        return RunspaceResult(
            stdout=str(getattr(raw, "stdout", "") or ""),
            stderr=str(getattr(raw, "stderr", "") or ""),
            exit_code=exit_code,
            location=getattr(raw, "location", None) or default_location,
            had_errors=bool(getattr(raw, "had_errors", False)) or exit_code != 0,
            timed_out=bool(getattr(raw, "timed_out", False)),
            exit_probe_ran=bool(getattr(raw, "exit_probe_ran", True)),
        )
    return RunspaceResult(stdout=str(raw or ""), location=default_location)


def _link_lost_fields(transport: Any) -> dict[str, Any]:
    """``{"link_lost": 1}`` when the transport reports a lost link, else ``{}``.

    The transport owns the verdict: ``meta["link_lost"]`` is set when a failed
    re-handshake tore the local session down, and it records the matching
    ``dead_reason``. Core only mirrors it as a token so an Agent branches on
    fields instead of parsing pypsrp's message text. Empty on every other
    path - a healthy invoke and an ordinary error must gain no key.
    """
    meta = getattr(transport, "meta", None)
    if not isinstance(meta, dict):
        return {}
    if meta.get("link_lost") is True:
        return {"link_lost": 1}
    if str(meta.get("dead_reason") or "").strip() == _LINK_LOST_REASON:
        return {"link_lost": 1}
    return {}


def _dead_transport_msg(transport: Any) -> str:
    """``msg`` for an invoke pre-checked against an already-dead transport.

    Prefer the transport's own ``dead_reason`` ("link lost", "peer_reset", ...)
    over the generic text: it names why the link is gone and matches the
    ``dead_reason=`` token endpoint open/list report. No recorded reason keeps
    the historical message.
    """
    meta = getattr(transport, "meta", None)
    if isinstance(meta, dict):
        reason = meta.get("dead_reason")
        if reason is not None and str(reason).strip():
            return _short(str(reason))
    return "endpoint transport not connected"


def _transport_error(
    exc: TransportError,
    *,
    op: str,
    ep: str,
    session_id: str | None = None,
) -> OpResult:
    fields: dict[str, Any] = {
        "op": op,
        "ep": ep,
        "msg": exc.msg,
    }
    if session_id:
        fields["id"] = session_id
    fields.update(connect_failure_fields(exc))
    if exc.details.get("transport"):
        fields["transport"] = exc.details["transport"]
    return OpResult(
        kind="ps",
        status="error",
        code=exc.code or "EXEC_FAILED",
        fields=fields,
    )


def _transport_error_with_link(
    exc: TransportError,
    *,
    op: str,
    ep: str,
    transport: Any,
    session_id: str | None = None,
) -> OpResult:
    """``_transport_error`` plus the transport's link-death token, if recorded.

    A failure the transport answered by tearing the link down is not an
    ordinary host-side error: the runspace died with the local session, so the
    row must carry the same machine-readable verdict (``link_lost``) and reopen
    advice the invoke path gives. The token is added only when the transport
    really recorded the death, so a healthy row gains no key.
    """
    result = _transport_error(exc, op=op, ep=ep, session_id=session_id)
    if _link_lost_fields(transport):
        result.fields["link_lost"] = 1
        result.hint = _PS_LINK_LOST_HINT
    return result


# ---------------------------------------------------------------------------
# Generation-fence helpers.
# Used by endpoint registry on stale transport pop/reconnect and by
# close_endpoint. Snapshot ids first, then close_ids only - never name-wide
# close_for_endpoint after a same-name reopen can register new sessions.
# ---------------------------------------------------------------------------


def snapshot_endpoint_session_ids(ep: str) -> list[str]:
    """Snapshot open PS session ids attached to *ep*.

    Safe under concurrent registration: returns only ids present at call time.
    Best-effort; returns ``[]`` on registry failure.
    """
    if not ep or not str(ep).strip():
        return []
    try:
        return get_ps_registry().ids_for_endpoint(str(ep).strip())
    except Exception:  # noqa: BLE001
        return []


def close_sessions_by_ids(session_ids: Any) -> int:
    """Close only the given PS session ids if still registered.

    Returns count closed. Ids registered after the snapshot are never touched.
    Best-effort; never raises (reconnect/close paths must not fail on cleanup).

    The count is a *local release* count, never a claim that the remote
    runspace is gone: each session is unregistered here and its own close is
    the single budgeted teardown ``ps close`` runs - lock wait, WSMan
    ``Delete`` and any refusal-recovery share one deadline, and the verdict
    that close recorded is the result. A Delete that a stale framing context
    provably refused is replayed once inside that deadline; one that still did
    not land is logged with the transport's verdict instead of being swallowed,
    so a teardown that left a remote runspace allocated is recoverable from the
    log. Nothing re-enters the handle afterwards, so a handle that already
    spent its budget on a hung close is not given a second one - exactly as
    ``ps close`` refuses to.
    """
    if not session_ids:
        return 0
    try:
        reg = get_ps_registry()
    except Exception:  # noqa: BLE001 - teardown callers must not fail here
        return 0
    closed = 0
    for sid in list(session_ids):
        try:
            sess = reg.pop(sid)
        except Exception:  # noqa: BLE001 - one bad id must not stop the sweep
            continue
        if sess is None:
            continue
        closed += 1
        try:
            sess.close()
        except Exception:  # noqa: BLE001 - close is best-effort by contract
            pass
        # Same rule as ``ps close``: the session's close is the whole teardown
        # and the verdict it recorded is the only result. No confirmation pass
        # re-enters the handle, so a recovery cannot be handed a second budget
        # and cannot run outside the lock.
        verdict = sess.close_verdict or _CLOSE_LANDED
        if verdict != _CLOSE_LANDED:
            _log.warning(
                "ps teardown: WSMan Delete not confirmed for %s "
                "(verdict=%s, ep=%s): the remote runspace may still be "
                "allocated",
                sid,
                verdict,
                sess.ep,
            )
    return closed
