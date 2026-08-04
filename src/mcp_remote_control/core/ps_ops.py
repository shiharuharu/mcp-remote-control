"""Ps Core ops: open, invoke, and close persistent PowerShell runspaces.

WinRM-only (``caps.ps``). Sessions share runspace state across invokes and
are pruned from the registry when the transport reports a dead connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, cast

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, resolve_home
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.registry import ensure_endpoint
from mcp_remote_control.ps.registry import get_ps_registry
from mcp_remote_control.ps.session import PsSession
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.winrm import RunspaceResult

VALID_OPS: frozenset[str] = frozenset({"open", "invoke", "close"})


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

    def close_runspace(self, handle: Any) -> None: ...


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
            code="UNSUPPORTED",
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
        return _transport_error(exc, op="open", ep=ep_name)
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
    """Run *script* in an open PS session; shared state across invokes."""
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
            hint="mcp-remote-control-cli ps open --ep <winrm-profile>",
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

    invoker = getattr(transport, "runspace_invoke", None)
    if not callable(invoker):
        return OpResult(
            kind="ps",
            status="error",
            code="UNSUPPORTED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": sess.ep,
                "msg": "transport has no runspace_invoke",
            },
        )

    try:
        result = invoker(
            sess.handle,
            str(script),
            timeout_s=float(timeout) if timeout is not None else None,
        )
    except TransportError as exc:
        # Dropped WinRM sessions surface as NOT_CONNECTED or EXEC_FAILED.
        # Prune either so the next invoke does not re-hang on a dead handle;
        # the caller must reopen with ``ps open``. registry.remove closes the
        # session (and underlying runspace) best-effort.
        if exc.code in {"NOT_CONNECTED", "EXEC_FAILED"}:
            try:
                get_ps_registry().remove(sid)
            except Exception:  # noqa: BLE001
                pass
        return _transport_error(
            exc,
            op="invoke",
            ep=sess.ep,
            session_id=sid,
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="ps",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "invoke",
                "id": sid,
                "ep": sess.ep,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
            cwd=sess.location,
        )

    rs = _as_runspace_result(result, default_location=sess.location)
    if rs.location:
        sess.location = rs.location

    body = rs.stdout if rs.stdout else None
    # Prefer non-empty stderr when stdout is empty on failure.
    if body is None and rs.stderr:
        body = rs.stderr

    status = "ok" if rs.exit_code == 0 and not rs.had_errors else "fail"
    fields: dict[str, Any] = {
        "op": "invoke",
        "id": sid,
        "session_id": sid,
        "ep": sess.ep,
        "exit": rs.exit_code,
    }
    if rs.had_errors and rs.exit_code == 0:
        fields["exit"] = 1
        status = "fail"
    if rs.timed_out:
        fields["timed_out"] = True
        # Timeout is a failure even if the pipeline reported exit 0.
        status = "fail"

    return OpResult(
        kind="ps",
        status=status,
        cwd=sess.location,
        fields=fields,
        body=body,
    )


def close_session(
    *,
    id: str | None = None,
    session_id: str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Close and invalidate a PS session."""
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
        )
    return OpResult(
        kind="ps",
        status="ok",
        cwd=sess.location,
        fields={
            "op": "close",
            "id": sid,
            "session_id": sid,
            "ep": sess.ep,
            "closed": True,
        },
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch ps op → Core implementation."""
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


def _as_runspace_result(
    raw: Any,
    *,
    default_location: str | None,
) -> RunspaceResult:
    if isinstance(raw, RunspaceResult):
        if raw.location is None and default_location is not None:
            return RunspaceResult(
                stdout=raw.stdout,
                stderr=raw.stderr,
                exit_code=raw.exit_code,
                location=default_location,
                had_errors=raw.had_errors,
                timed_out=raw.timed_out,
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
        )
    return RunspaceResult(stdout=str(raw or ""), location=default_location)


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
    if exc.details.get("host"):
        fields["host"] = exc.details["host"]
    if exc.details.get("transport"):
        fields["transport"] = exc.details["transport"]
    return OpResult(
        kind="ps",
        status="error",
        code=exc.code or "EXEC_FAILED",
        fields=fields,
    )


def _home(home: Path | str | None) -> Path:
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()


def _short(msg: str, limit: int = 200) -> str:
    text = " ".join(str(msg).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
