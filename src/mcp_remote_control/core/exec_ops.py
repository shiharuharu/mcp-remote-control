"""Exec Core ops: non-interactive command, argv, or script on an endpoint.

Exactly one form is accepted per call. Resolves the endpoint transport,
normalizes cwd/timeout, runs via the transport, and returns a structured
:class:`~mcp_remote_control.core.result.OpResult` for CLI/MCP render.

Timeout semantics (surface honesty): ``timeout`` is a **local wait wall-clock**
budget. It does **not** guarantee remote process/pipeline cancel (especially
WinRM oneshot shells). On ``timed_out``, OpResult exposes machine-readable
fields (``timed_out``, ``exit=-1``, and when WinRM hard-timeout dispose ran:
``marked_dead`` / ``session_disposed`` / ``reopen_hint``) plus a short prose
hint. Agents must not parse English prose alone. Prefer ``endpoint close``
then open (or ensure reconnect) if timeouts recur - not an SSH-style kill.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound
from mcp_remote_control.core.result import OpResult, _home, _short
from mcp_remote_control.endpoint.registry import (
    connect_failure_fields,
    ensure_endpoint,
    retire_refused_link,
)
from mcp_remote_control.exec import (
    format_command_echo,
    normalize_runtime,
    run_script_on_transport,
    script_summary,
)
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import (
    BaseTransport,
    ExecResult,
    normalize_timeout_s as _normalize_timeout,
)
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

    *script_args* belongs to the script form; passing it with command= or
    argv= is INVALID_ARG rather than a silently unused argument list. The same
    holds for a *runtime* naming an interpreter: command=/argv= run as given,
    so the named interpreter would never run.
    """
    form, err = _detect_form(
        command=command,
        argv=argv,
        script=script,
        script_path=script_path,
        runtime=runtime,
        script_args=script_args,
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

    # Timeout is a local wait budget. Reject 0 / NaN / unparseable values
    # before lazy connect so a connect fault cannot mask INVALID_ARG.
    try:
        timeout_s = _normalize_timeout(timeout)
    except TransportError as exc:
        return _transport_error_result(exc, ep=ep_name, form=form)

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
    # (ps_oneshot is False - typically language_mode=NoLanguage / JEA).
    # Incomplete probes keep ps_oneshot=True so a hung or unparseable
    # capability check does not hard-block user oneshot exec. Absent
    # winrm_ps (probe disabled / lab) keeps the historical allow path.
    # Exec is not gated on ps_script_fs - ConstrainedLanguage with
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

    t0 = time.monotonic()
    try:
        # One value for the check, the dispatched argv and the rendered echo:
        # a local script path names a file the launched child opens, so the
        # path vetted here is the path handed to it (see _script_dispatch_path).
        script_dispatch_path = _script_dispatch_path(
            transport,
            script_path=script_path,
            script=script,
            cwd=work_cwd,
        )
        result = _dispatch(
            transport,
            form=form,
            command=command,
            argv=argv,
            script=script,
            script_path=script_dispatch_path,
            runtime=runtime,
            script_args=script_args,
            cwd=work_cwd,
            timeout_s=timeout_s,
            env=env,
        )
    except TransportError as exc:
        # A refusal that the transport's own taxonomy does not classify (e.g. a
        # WSMan 401 surfacing as a plain auth error) leaves a live-but-poisoned
        # session registered: every later call fails identically and
        # ``endpoint open`` hands the same generation back. Retire it here so
        # the next call reconnects.
        retire_refused_link(transport, exc)
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

    status = _status_for(result)
    # Only advance tracked endpoint.cwd on a clean success. Failed/timeout
    # runs (e.g. `cd /nonexistent`) must not pollute the default for later
    # execs that omit an explicit cwd.
    if out_cwd and status == "ok":
        endpoint.cwd = out_cwd

    fields: dict[str, Any] = {
        "ep": ep_name,
        "form": form,
        "exit": result.exit_code,
        "ms": ms,
    }
    timeout_hint: str | None = None
    if result.timed_out:
        # Machine-readable timeout tokens (align with ps_ops timed_out).
        # Agents must not rely on English prose alone.
        fields["timed_out"] = True
        fields["exit"] = -1
        if result.stdout or result.stderr:
            fields["partial"] = True
        _apply_timeout_dispose_fields(fields, transport)
        # Honest surface: client wall-clock != remote kill (SSH mental model).
        # WinRM oneshot may keep a remote shell until session dispose.
        timeout_hint = (
            "timeout is local wait wall-clock, not a remote kill guarantee; "
            "WinRM cannot guarantee remote pipeline cancel; "
            "if timeouts recur: endpoint close then open (close+reopen)"
        )
    # Drop None-valued noise only; never inject timeout keys on success.
    fields = {k: v for k, v in fields.items() if v is not None}

    body = _build_body(
        form=form,
        command=command,
        argv=argv,
        script=script,
        script_path=script_dispatch_path,
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
        hint=timeout_hint,
    )


# Stable reopen guidance token. Prefer this field over prose parse.
_REOPEN_HINT = "endpoint close then open"


def _apply_timeout_dispose_fields(
    fields: dict[str, Any],
    transport: BaseTransport,
) -> None:
    """Copy WinRM hard-timeout dispose markers into OpResult.fields.

    Only when transport meta reports mark_dead / session dispose (WinRM
    oneshot hard-timeout path). Does not claim remote pipeline Stopped.
    Local/SSH timeouts without those meta flags stay free of dispose noise.
    """
    tmeta = getattr(transport, "meta", None) or {}
    if not isinstance(tmeta, dict):
        return
    marked = tmeta.get("marked_dead") is True
    disposed = tmeta.get("session_disposed") is True
    # Fallback: mark_dead may set dead_reason before the marked_dead flag.
    if not marked and tmeta.get("dead_reason") == "hard timeout":
        marked = True
    if marked:
        fields["marked_dead"] = True
    if disposed:
        fields["session_disposed"] = True
    if marked or disposed:
        rh = tmeta.get("reopen_hint")
        fields["reopen_hint"] = (
            str(rh).strip() if rh and str(rh).strip() else _REOPEN_HINT
        )


def _detect_form(
    *,
    command: str | None,
    argv: list[str] | None,
    script: str | None,
    script_path: str | None,
    runtime: str | None = None,
    script_args: list[str] | None = None,
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
    # script_args is the script form's parameter list. On command/argv there is
    # no script to bind it to, and a dropped argument is invisible in the
    # result (status ok, exit 0, values never used), so refuse the combination
    # rather than ignore it.
    if script_args and (has_command or has_argv):
        form_flag = "command" if has_command else "argv"
        return None, OpResult(
            kind="exec",
            status="error",
            code="INVALID_ARG",
            fields={
                "form": form_flag,
                "msg": (
                    f"script_args cannot be combined with {form_flag}=: "
                    "there is no script to bind them to, and they would be "
                    "dropped without effect"
                ),
            },
            hint=(
                f"include the values in {form_flag}= itself, or pass them "
                "with script=/script_path="
            ),
        )
    # runtime= names the interpreter that runs a script body. A command= is a
    # shell line and an argv= an executable plus its arguments: neither consults
    # it, so the caller's named interpreter never runs and the result says
    # nothing (status ok, exit 0, the name in no field) - the same invisible
    # drop as script_args above. ``auto`` names no interpreter, so it is
    # accepted as the no-op it is.
    if (has_command or has_argv) and normalize_runtime(runtime) != "auto":
        form_flag = "command" if has_command else "argv"
        return None, OpResult(
            kind="exec",
            status="error",
            code="INVALID_ARG",
            fields={
                "form": form_flag,
                "msg": (
                    f"runtime cannot be combined with {form_flag}=: "
                    f"{form_flag}= runs as given, so the named interpreter "
                    "would never run and the name would be dropped without "
                    "effect"
                ),
            },
            hint=(
                f"put the interpreter in {form_flag}= itself "
                "(argv=['python3','job.py']), or pass the script through "
                "script=/script_path= with runtime="
            ),
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
    # report silent exit 0 - require at least one non-empty body or path.
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

    # Script path semantics: local transport -> controller filesystem;
    # SSH -> path on the remote host. Prefer ``body`` when the script content
    # lives on the controller (no automatic local-file upload to remote).
    # A local path reaches here already resolved and vetted by the caller,
    # so the argv below names the file the existence check examined.

    # Dialect for runtime=auto: endpoint probe meta first, then transport
    # remote_shell_family (SSH/WinRM), then local platform inside
    # run_script_on_transport. Never hardcode bash here.
    dialect = None
    tmeta = getattr(transport, "meta", None) or {}
    if isinstance(tmeta, dict):
        dialect = tmeta.get("dialect")
    if not dialect:
        rem = getattr(transport, "remote_shell_family", None)
        if rem:
            from mcp_remote_control.shell.dialect import resolve_dialect

            dialect = resolve_dialect(shell_family=str(rem))
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


def _script_dispatch_path(
    transport: BaseTransport,
    *,
    script_path: str | None,
    script: str | None,
    cwd: str | None,
) -> str | None:
    """Return the script path to dispatch, vetting a local one.

    A local ``script_path`` names a file the launched child opens, so the
    path checked here is the path that is dispatched: ``~`` is expanded and a
    relative path is taken against the run's cwd - the directory the child is
    started in. Checking the control process cwd instead rejects a script that
    is present in the run's cwd, accepts one that exists only next to the
    controller, and leaves an expanded ``~`` path to be opened as a literal
    directory named ``~``. A remote path addresses the endpoint's own
    filesystem: it is neither checked nor rewritten.

    Expansion and joining stay on the string; a ``Path`` object would collapse
    a trailing separator or a ``.`` component, so a spelling that names no file
    at all (``job.sh/``, ``job.sh/.``) would be vetted and dispatched as the
    different, valid path the interpreter refuses on its own. One string is
    expanded, joined, checked, dispatched and echoed.
    """
    if transport.name != "local" or not script_path or script is not None:
        return script_path
    try:
        p = os.path.expanduser(str(script_path))
    except (OSError, RuntimeError, ValueError) as exc:
        # Expanding a ``~user`` can fail outright, e.g. no home directory for
        # that user on this host: the caller named a path that resolves to no
        # location here, so it is refused as the argument fault it is. Letting
        # it escape would report a successful dispatch of an argument that
        # never named a file.
        raise TransportError(
            "INVALID_ARG",
            f"cannot expand home directory in path: {script_path}",
        ) from exc
    if p[:1] == "~":
        # An unresolvable ``~user`` is returned unchanged rather than raising,
        # so an unexpanded ``~`` head is the same argument fault as above: no
        # home directory here, and dispatching the literal spelling would name
        # a file the caller never provided.
        raise TransportError(
            "INVALID_ARG",
            f"cannot expand home directory in path: {script_path}",
        )
    if not os.path.isabs(p):
        p = os.path.join(cwd if cwd is not None else os.getcwd(), p)
    if not os.path.isfile(p):
        raise TransportError(
            "INVALID_ARG",
            f"script path not found: {p}",
        )
    return p


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

    # Omit empty stderr. A non-empty stderr is always labelled: without the
    # marker a stderr-only run renders exactly like program output, so the
    # caller cannot tell a warning or diagnostic from the run's data. Bare
    # stdout stays unlabelled - it is the body the run produced.
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")

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
        # Only a coerce-surviving path is an explicit caller cwd. Empty
        # string, bool True, and unexpanded probe tokens fall through to
        # endpoint/transport/getcwd instead of fail-closed INVALID_CWD.
        requested_path = coerce_cwd_path(requested)
        return _resolve_local_cwd(
            requested=requested_path,
            endpoint_cwd=endpoint_cwd,
            transport_cwd=transport.cwd,
            strict_requested=requested_path is not None,
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
    requested_path = coerce_cwd_path(requested)
    # Fail closed only when the caller supplied a real path. Non-path
    # values (empty, True, unexpanded ${HOME:-} / %CD%) are not explicit.
    strict = bool(strict_requested) and requested_path is not None

    candidates: list[str] = []
    for raw in (requested_path, endpoint_cwd, transport_cwd):
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
        if strict and i == 0:
            raise TransportError(
                "INVALID_CWD",
                f"cwd does not exist or is not a directory: {abs_path}",
                details={"cwd": abs_path},
            )
    fallback = os.getcwd()
    if last_error and strict:
        raise TransportError(
            "INVALID_CWD",
            last_error,
            details={"cwd": requested_path},
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
    # Probe-vs-refusal class travels with the row: "the intermediary refused
    # this request" and "the host is unreachable" are the same NOT_CONNECTED
    # otherwise, and only the first one is fixed by reopening.
    fields.update(connect_failure_fields(exc))
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


