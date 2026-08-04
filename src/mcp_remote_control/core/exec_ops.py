"""Exec Core ops: non-interactive command, argv, or script on an endpoint.

Exactly one form is accepted per call. Resolves the endpoint transport,
normalizes cwd/timeout, runs via the transport, and returns a structured
:class:`~mcp_remote_control.core.result.OpResult` for CLI/MCP render.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, resolve_home
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.registry import ensure_endpoint
from mcp_remote_control.exec import (
    format_command_echo,
    run_script_on_transport,
    script_summary,
)
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import BaseTransport, ExecResult
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path


def run(
    *,
    ep: str | None = None,
    command: str | None = None,
    argv: list[str] | None = None,
    script: str | None = None,
    script_path: str | None = None,
    runtime: str | None = None,
    script_args: list[str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    home: Path | str | None = None,
    connector: Any | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Run a remote/local command, argv, or script on *ep*.

    Forms (exactly one):
    - command: shell string
    - argv: list of strings (no shell when transport supports it)
    - script: body (*script*) and/or path (*script_path*) with optional runtime/args
    """
    form, err = _detect_form(
        command=command,
        argv=argv,
        script=script,
        script_path=script_path,
    )
    if err is not None:
        return err
    # _detect_form returns form only when err is None.
    assert form is not None

    if not ep or not str(ep).strip():
        return OpResult(
            kind="exec",
            status="error",
            code="MISSING_ARG",
            fields={"form": form, "msg": "ep is required"},
            hint="pass ep=<profile> (lazy connect)",
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
            kind="exec",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"ep": ep_name, "form": form, "msg": _short(str(exc))},
        )
    except ProfileInvalid as exc:
        return OpResult(
            kind="exec",
            status="error",
            code="PROFILE_INVALID",
            fields={"ep": ep_name, "form": form, "msg": _short(str(exc))},
        )
    except TransportError as exc:
        return _transport_error_result(exc, ep=ep_name, form=form)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="exec",
            status="error",
            code="CONNECT_FAILED",
            fields={
                "ep": ep_name,
                "form": form,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
        )

    if not endpoint.caps.get("exec", False):
        return OpResult(
            kind="exec",
            status="error",
            code="CAP_DENIED",
            fields={
                "ep": ep_name,
                "form": form,
                "msg": "endpoint lacks exec capability",
                "caps": endpoint.caps_token,
            },
        )

    transport = endpoint.transport
    if transport is None or not transport.is_connected():
        return OpResult(
            kind="exec",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "ep": ep_name,
                "form": form,
                "msg": "endpoint transport not connected",
            },
        )

    # Gate only when the probe *explicitly* reported oneshot unusable
    # (ps_oneshot is False — typically language_mode=NoLanguage / JEA).
    # Incomplete probes keep ps_oneshot=True so a hung or unparseable
    # capability check does not hard-block user oneshot exec. Absent
    # winrm_ps (probe disabled / lab) keeps the historical allow path.
    # Exec is not gated on ps_script_fs — ConstrainedLanguage with
    # ps_oneshot=true still runs.
    winrm_ps = (getattr(transport, "meta", None) or {}).get("winrm_ps")
    if isinstance(winrm_ps, dict) and winrm_ps.get("ps_oneshot") is False:
        lang_mode = winrm_ps.get("language_mode")
        gate_fields: dict[str, Any] = {
            "ep": ep_name,
            "form": form,
            "transport": endpoint.transport_name,
            "msg": "winrm exec unsupported on this endpoint (ps_oneshot=false)",
        }
        if lang_mode is not None and str(lang_mode).strip():
            gate_fields["lang_mode"] = str(lang_mode).strip()
        return OpResult(
            kind="exec",
            status="error",
            code="UNSUPPORTED",
            fields=gate_fields,
            hint=(
                "winrm exec requires a runnable PowerShell language mode; "
                "host reports NoLanguage (ps_oneshot=false). Raise language "
                "mode (e.g. FullLanguage) or use a non-JEA endpoint"
            ),
        )

    try:
        work_cwd = _resolve_cwd(
            requested=cwd,
            endpoint_cwd=endpoint.cwd,
            transport=transport,
        )
    except TransportError as exc:
        return _transport_error_result(exc, ep=ep_name, form=form)

    timeout_s = _normalize_timeout(timeout)

    t0 = time.monotonic()
    try:
        result = _dispatch(
            transport,
            form=form,
            command=command,
            argv=argv,
            script=script,
            script_path=script_path,
            runtime=runtime,
            script_args=script_args,
            cwd=work_cwd,
            timeout_s=timeout_s,
            env=env,
        )
    except TransportError as exc:
        return _transport_error_result(
            exc, ep=ep_name, form=form, cwd=work_cwd
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="exec",
            status="error",
            code="EXEC_FAILED",
            cwd=work_cwd,
            fields={
                "ep": ep_name,
                "form": form,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
        )
    ms = int((time.monotonic() - t0) * 1000)

    # Prefer absolute cwd from the result. coerce_cwd_path rejects non-path
    # probe bleed-through (e.g. bool True) so we never seed endpoint.cwd with it.
    out_cwd = coerce_cwd_path(result.cwd) or coerce_cwd_path(work_cwd)
    out_cwd = _absolutize_cwd_display(out_cwd, transport=transport)

    if out_cwd:
        endpoint.cwd = out_cwd

    status = _status_for(result)
    fields: dict[str, Any] = {
        "ep": ep_name,
        "form": form,
        "exit": result.exit_code,
        "ms": ms,
    }
    if result.timed_out:
        fields["partial"] = True if (result.stdout or result.stderr) else None
        fields = {k: v for k, v in fields.items() if v is not None}

    body = _build_body(
        form=form,
        command=command,
        argv=argv,
        script=script,
        script_path=script_path,
        runtime=runtime,
        script_args=script_args,
        result=result,
    )

    return OpResult(
        kind="exec",
        status=status,
        cwd=out_cwd,
        fields=fields,
        body=body,
    )


def _detect_form(
    *,
    command: str | None,
    argv: list[str] | None,
    script: str | None,
    script_path: str | None,
) -> tuple[str | None, OpResult | None]:
    has_command = command is not None
    has_argv = argv is not None
    has_script = script is not None or script_path is not None
    n = sum(1 for f in (has_command, has_argv, has_script) if f)
    if n == 0:
        return None, OpResult(
            kind="exec",
            status="error",
            code="MISSING_ARG",
            fields={
                "msg": "provide command= or argv= or script=/script_path=",
            },
            hint="mcp-remote-control-cli exec --ep local -- command 'echo hello'",
        )
    if n > 1:
        return None, OpResult(
            kind="exec",
            status="error",
            code="INVALID_ARG",
            fields={
                "msg": "command, argv, and script forms are mutually exclusive",
            },
        )
    if has_command:
        if not isinstance(command, str) or command.strip() == "":
            return None, OpResult(
                kind="exec",
                status="error",
                code="INVALID_ARG",
                fields={"form": "command", "msg": "command is empty"},
            )
        return "command", None
    if has_argv:
        if not argv:
            return None, OpResult(
                kind="exec",
                status="error",
                code="INVALID_ARG",
                fields={"form": "argv", "msg": "argv is empty"},
            )
        return "argv", None
    # Empty script="" / script_path="" would otherwise dispatch a no-op and
    # report silent exit 0 — require at least one non-empty body or path.
    script_body = script if script is not None else ""
    script_loc = script_path if script_path is not None else ""
    if not script_body.strip() and not script_loc.strip():
        return None, OpResult(
            kind="exec",
            status="error",
            code="INVALID_ARG",
            fields={
                "form": "script",
                "msg": "script body and path are both empty",
            },
        )
    return "script", None


def _dispatch(
    transport: BaseTransport,
    *,
    form: str,
    command: str | None,
    argv: list[str] | None,
    script: str | None,
    script_path: str | None,
    runtime: str | None,
    script_args: list[str] | None,
    cwd: str | None,
    timeout_s: float | None,
    env: dict[str, str] | None,
) -> ExecResult:
    if form == "command":
        assert command is not None
        return transport.run_command(
            command, cwd=cwd, timeout_s=timeout_s, env=env
        )
    if form == "argv":
        assert argv is not None
        return transport.run_argv(
            list(argv), cwd=cwd, timeout_s=timeout_s, env=env
        )

    # Script path semantics: local transport → controller filesystem;
    # SSH → path on the remote host. Prefer ``body`` when the script content
    # lives on the controller (no automatic local-file upload to remote).
    if transport.name == "local" and script_path and script is None:
        p = Path(str(script_path)).expanduser()
        if not p.is_file():
            raise TransportError(
                "INVALID_ARG",
                f"script path not found: {p}",
            )

    dialect = None
    tmeta = getattr(transport, "meta", None) or {}
    if isinstance(tmeta, dict):
        dialect = tmeta.get("dialect")
    return run_script_on_transport(
        transport,
        body=script,
        path=script_path,
        runtime=runtime,
        args=script_args,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        dialect=str(dialect) if dialect else None,
    )


def _status_for(result: ExecResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.exit_code == 0:
        return "ok"
    return "fail"


def _build_body(
    *,
    form: str,
    command: str | None,
    argv: list[str] | None,
    script: str | None,
    script_path: str | None,
    runtime: str | None,
    script_args: list[str] | None,
    result: ExecResult,
) -> str | None:
    echo = format_command_echo(
        form=form,
        command=command,
        argv=argv,
        script_summary=script_summary(
            body=script,
            path=script_path,
            runtime=runtime,
            args=script_args,
        )
        if form == "script"
        else None,
    )
    parts: list[str] = [echo]

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # Omit empty stderr; when present, append after stdout with a marker.
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr.strip():
        if stdout:
            parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")
        else:
            parts.append(stderr.rstrip("\n"))

    if len(parts) == 1 and not stdout and not stderr.strip():
        return echo

    return "\n".join(parts)


def _resolve_cwd(
    *,
    requested: str | None,
    endpoint_cwd: str | None,
    transport: BaseTransport,
) -> str | None:
    """Return an absolute cwd for the run when possible."""
    if transport.name == "local":
        return _resolve_local_cwd(
            requested=requested,
            endpoint_cwd=endpoint_cwd,
            transport_cwd=transport.cwd,
            strict_requested=requested is not None,
        )

    # Remote: expand ~ with transport.home when known; otherwise leave the
    # path for the remote shell to expand.
    raw = requested if requested is not None else endpoint_cwd
    if coerce_cwd_path(raw) is None:
        raw = transport.cwd
    text = coerce_cwd_path(raw)
    if text is None:
        return None
    if text.startswith("~"):
        home = transport.home
        if home and (text == "~" or text.startswith("~/")):
            text = home + text[1:]
    return text


def _resolve_local_cwd(
    *,
    requested: str | None,
    endpoint_cwd: str | None,
    transport_cwd: str | None,
    strict_requested: bool,
) -> str:
    candidates: list[str] = []
    for raw in (requested, endpoint_cwd, transport_cwd):
        path = coerce_cwd_path(raw)
        if path:
            candidates.append(path)
    candidates.append(os.getcwd())

    last_error: str | None = None
    for i, raw in enumerate(candidates):
        try:
            abs_path = _expand_abs_local(raw)
        except OSError as exc:
            last_error = str(exc)
            continue
        if Path(abs_path).is_dir():
            return abs_path
        last_error = f"not a directory: {abs_path}"
        # Explicit caller cwd must exist; softer fallbacks may skip missing dirs.
        if strict_requested and i == 0 and requested is not None:
            raise TransportError(
                "INVALID_CWD",
                f"cwd does not exist or is not a directory: {abs_path}",
                details={"cwd": abs_path},
            )
    fallback = os.getcwd()
    if last_error and strict_requested:
        raise TransportError(
            "INVALID_CWD",
            last_error,
            details={"cwd": requested},
        )
    return fallback


def _expand_abs_local(raw: str) -> str:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (Path(os.getcwd()) / p).resolve()
    else:
        p = p.resolve()
    return str(p)


def _absolutize_cwd_display(
    cwd: str | None, *, transport: BaseTransport
) -> str | None:
    text = coerce_cwd_path(cwd)
    if text is None:
        return None
    if transport.name == "local":
        try:
            return _expand_abs_local(text)
        except OSError:
            return text
    return text


def _normalize_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return t


def _transport_error_result(
    exc: TransportError,
    *,
    ep: str,
    form: str | None,
    cwd: str | None = None,
) -> OpResult:
    fields: dict[str, Any] = {
        "ep": ep,
        "msg": exc.msg,
    }
    if form is not None:
        fields["form"] = form
    if exc.details.get("host"):
        fields["host"] = exc.details["host"]
    if exc.details.get("cwd"):
        fields["cwd_arg"] = exc.details["cwd"]
    code = exc.code or "EXEC_FAILED"
    return OpResult(
        kind="exec",
        status="error",
        code=code,
        cwd=cwd,
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
