"""Screen Core ops: open, send, close, and list interactive PTY sessions.

Owns the agent loop for host shells (local/ssh): open a PTY with adaptive
geometry, run ordered send actions with wait/shot, and tear down sessions.
WinRM endpoints lack screen capability and return ``UNSUPPORTED``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, resolve_home
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.registry import ensure_endpoint
from mcp_remote_control.screen.buffer import (
    DEFAULT_COLORTERM,
    DEFAULT_TERM,
)
from mcp_remote_control.screen.cwd_probe import probe_and_update_cwd
from mcp_remote_control.screen.geometry import GeometryAdapter, GeometryMemory
from mcp_remote_control.screen.local_pty import LocalPty
from mcp_remote_control.screen.registry import get_screen_registry
from mcp_remote_control.screen.send import execute_send
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.screen.ssh_pty import SshPty
from mcp_remote_control.shell.dialect import resolve_dialect
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import BaseTransport

VALID_OPS: frozenset[str] = frozenset({"open", "send", "close", "list"})

# Brief settle so shell MOTD/prompt can paint before the first frame.
_OPEN_SETTLE_S = 0.35


def _frame_body(frame: object | None) -> str | None:
    """Normalize a shot/outcome frame to ``str | None`` for OpResult.body."""
    if frame is None or frame is False:
        return None
    if isinstance(frame, list):
        text = "\n".join(str(x) for x in frame)
    elif isinstance(frame, str):
        text = frame
    else:
        text = str(frame)
    return text if text else None


def open_screen(
    *,
    ep: str | None = None,
    cwd: str | None = None,
    cols: int | None = None,
    rows: int | None = None,
    shell: str | None = None,
    command: str | None = None,
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    label: str | None = None,
    home: Path | str | None = None,
    connector: Any | None = None,
    settle_s: float | None = None,
    fit: bool | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Open an interactive shell PTY on *ep* (default: no business command).

    Geometry is adaptive: class seed → settle health → limited grow. Pass both
    *cols* and *rows* to force size (skips grow). *fit=False* uses seed only
    without grow. The adapter never shrinks the PTY for token savings.

    Advanced: *command* / *argv* replace the default shell as the PTY main process.
    """
    if not ep or not str(ep).strip():
        return OpResult(
            kind="screen",
            status="error",
            code="MISSING_ARG",
            fields={"op": "open", "msg": "ep is required"},
            hint="mcp-remote-control-cli screen open --ep <profile>",
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
            kind="screen",
            status="error",
            code="PROFILE_NOT_FOUND",
            fields={"op": "open", "ep": ep_name, "msg": _short(str(exc))},
        )
    except ProfileInvalid as exc:
        return OpResult(
            kind="screen",
            status="error",
            code="PROFILE_INVALID",
            fields={"op": "open", "ep": ep_name, "msg": _short(str(exc))},
        )
    except TransportError as exc:
        return _transport_error(exc, op="open", ep=ep_name)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="screen",
            status="error",
            code="CONNECT_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
        )

    if not endpoint.caps.get("screen", False):
        return OpResult(
            kind="screen",
            status="error",
            code="UNSUPPORTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "transport": endpoint.transport_name,
                "msg": "endpoint lacks screen capability (no interactive PTY)",
                "caps": endpoint.caps_token,
            },
            hint="use exec/fs on this endpoint; screen is local/ssh only",
        )

    transport = endpoint.transport
    if transport is None or not transport.is_connected():
        return OpResult(
            kind="screen",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": "endpoint transport not connected",
            },
        )

    # Class seed + geometry memory; explicit cols+rows force size and skip grow.
    do_fit = True if fit is None else bool(fit)
    adapter = GeometryAdapter(memory=GeometryMemory.for_home(home_path))
    plan = adapter.plan_open(
        command=command,
        argv=argv,
        cols=cols,
        rows=rows,
        endpoint_id=ep_name,
        fit=do_fit,
        profile_defaults=endpoint.profile.defaults if endpoint.profile else None,
    )
    g_cols, g_rows = plan.cols, plan.rows

    work_cwd = _resolve_open_cwd(
        requested=cwd,
        endpoint_cwd=endpoint.cwd,
        transport=transport,
    )

    open_mode = "shell"
    if command is not None or argv is not None:
        open_mode = "exec"

    pty_env = {
        "TERM": DEFAULT_TERM,
        "COLORTERM": DEFAULT_COLORTERM,
    }
    if env:
        pty_env.update({str(k): str(v) for k, v in env.items()})

    shell_path = shell
    if shell_path is None and endpoint.profile and endpoint.profile.defaults:
        raw_shell = endpoint.profile.defaults.get("shell")
        if isinstance(raw_shell, str) and raw_shell.strip():
            shell_path = raw_shell.strip()

    try:
        pty_handle = _open_pty(
            transport,
            cols=g_cols,
            rows=g_rows,
            cwd=work_cwd,
            shell=shell_path,
            command=command,
            argv=argv,
            env=pty_env,
        )
    except TransportError as exc:
        return _transport_error(exc, op="open", ep=ep_name)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="screen",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"PTY open failed: {type(exc).__name__}: {exc}"),
            },
        )

    reg = get_screen_registry()
    sid = reg.allocate_id()
    dialect, shell_path_hint, caps, meta = _dialect_from_endpoint(
        endpoint, shell_path=shell_path
    )
    session = ScreenSession(
        id=sid,
        ep=ep_name,
        pty=pty_handle,
        cols=g_cols,
        rows=g_rows,
        cwd=work_cwd or getattr(pty_handle, "cwd", None),
        open_mode=open_mode,
        label=label,
        surface="shell" if open_mode == "shell" else "unknown",
        dialect=dialect,
        shell_path=shell_path_hint,
        shell_caps=caps,
        cwd_src="open" if (work_cwd or getattr(pty_handle, "cwd", None)) else None,
        meta=meta,
    )
    reg.add(session)

    settle = _OPEN_SETTLE_S if settle_s is None else float(settle_s)
    try:
        fit_result, shot = adapter.adapt(
            session,
            plan,
            settle_s=settle,
            endpoint_id=ep_name,
            surface=session.surface,
        )
    except Exception as exc:  # noqa: BLE001
        reg.remove(sid)
        return OpResult(
            kind="screen",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"settle/shot failed: {type(exc).__name__}: {exc}"),
            },
        )

    # Prefer absolute cwd for Agent output; silent probe when still in a shell.
    out_cwd = session.cwd
    if out_cwd and transport.name == "local":
        try:
            out_cwd = str(Path(out_cwd).expanduser().resolve())
        except OSError:
            pass
    session.cwd = out_cwd
    if open_mode == "shell" and session.surface == "shell":
        try:
            probed = probe_and_update_cwd(session, timeout_s=1.2)
            if probed:
                out_cwd = probed
                session.cwd_src = "probe"
                # Re-shot so the Agent frame excludes probe marker lines.
                shot = session.shot(settle_s=0.05)
            elif session.cwd_src is None:
                session.cwd_src = "stale"
        except Exception:  # noqa: BLE001
            if session.cwd_src is None:
                session.cwd_src = "stale"

    # Frame geometry must match the live session after any grow.
    fields: dict[str, Any] = {
        "op": "open",
        "id": sid,
        "screen_id": sid,
        "ep": ep_name,
        "cols": session.cols,
        "rows": session.rows,
        "cur": shot["cur"],
        "gen": shot["gen"],
        "surface": session.surface,
        "open": open_mode,
        "hash": shot["hash"],
        "fit": fit_result.fit,
        "steps": fit_result.steps,
        "seed": f"{fit_result.seed_cols}x{fit_result.seed_rows}",
        "class": fit_result.cmd_class,
    }
    if session.dialect:
        fields["dialect"] = session.dialect
    if session.shell_caps and session.shell_caps.get("busybox"):
        fields["busybox"] = 1
    if session.cwd_src:
        fields["cwd_src"] = session.cwd_src
    if shot.get("alive"):
        fields["idle"] = True
    else:
        fields["alive"] = False
        if shot.get("exit") is not None:
            fields["exit"] = shot["exit"]
    if label:
        fields["label"] = label
    if command:
        # Short cmd token for Agent meta (basename when path-like).
        cmd_tok = plan.command_basename or str(command).strip().split()[0]
        fields["cmd"] = cmd_tok

    body = _frame_body(shot.get("frame"))
    # Never leak the silent-cwd probe marker into the Agent body.
    if body and "__MRC_PWD__:" in body:
        from mcp_remote_control.screen.buffer import strip_probe_lines

        body = strip_probe_lines(body)
        if not body.strip():
            body = None

    return OpResult(
        kind="screen",
        status="ok" if shot.get("alive", True) else "dead",
        cwd=out_cwd,
        fields=fields,
        body=body,
    )


def send_screen(
    *,
    id: str | None = None,
    screen_id: str | None = None,
    actions: Any = None,
    wait: Any = None,
    shot: bool | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Execute ordered actions, wait, drain, then take a frame shot by default."""
    sid = id or screen_id
    if not sid or not str(sid).strip():
        return OpResult(
            kind="screen",
            status="error",
            code="MISSING_ARG",
            fields={"op": "send", "msg": "screen id required (id=)"},
            hint="mcp-remote-control-cli screen send --id <screen_id>",
        )
    sid = str(sid).strip()
    sess = get_screen_registry().get(sid)
    if sess is None or sess.closed:
        return OpResult(
            kind="screen",
            status="error",
            code="SCREEN_NOT_FOUND",
            fields={
                "op": "send",
                "id": sid,
                "msg": f"screen not open: {sid}",
            },
        )

    do_shot = True if shot is None else bool(shot)

    try:
        outcome = execute_send(
            sess,
            actions=actions,
            wait=wait,
            shot=do_shot,
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="screen",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "send",
                "id": sid,
                "screen_id": sid,
                "msg": _short(f"{type(exc).__name__}: {exc}"),
            },
            cwd=sess.cwd,
        )

    fields: dict[str, Any] = {
        "op": "send",
        "id": sid,
        "screen_id": sid,
        "ep": sess.ep,
        "cols": outcome.cols,
        "rows": outcome.rows,
        "cur": outcome.cur,
        "gen": outcome.gen,
        "hash": outcome.hash,
        "surface": sess.surface,
    }
    if outcome.did:
        fields["did"] = ",".join(outcome.did)
    if outcome.wait_until:
        fields["wait"] = outcome.wait_until
    if outcome.nav:
        fields["nav"] = outcome.nav
    if outcome.alive:
        fields["idle"] = True
    else:
        fields["alive"] = False
        if outcome.exit_code is not None:
            fields["exit"] = outcome.exit_code
    if not do_shot:
        fields["shot"] = False
    if outcome.error_code:
        fields["msg"] = _short(outcome.error_msg or outcome.error_code)
    if outcome.status == "unchanged":
        fields["unchanged"] = True
    if sess.dialect:
        fields["dialect"] = sess.dialect
    if sess.shell_caps and sess.shell_caps.get("busybox"):
        fields["busybox"] = 1
    if sess.cwd_src:
        fields["cwd_src"] = sess.cwd_src

    body = _frame_body(outcome.frame)
    if body and "__MRC_PWD__:" in body:
        from mcp_remote_control.screen.buffer import strip_probe_lines

        body = strip_probe_lines(body)
        if not body.strip():
            body = None

    return OpResult(
        kind="screen",
        status=outcome.status,
        code=outcome.error_code if outcome.status == "error" else None,
        cwd=outcome.cwd if outcome.cwd is not None else sess.cwd,
        fields=fields,
        body=body,
        hint=(
            "prefer go|to_text|paste|submit; read cur= before next send"
            if outcome.status in ("ok", "unchanged") and not outcome.did
            else None
        ),
    )


def close_screen(
    *,
    id: str | None = None,
    screen_id: str | None = None,
    **_kwargs: Any,
) -> OpResult:
    sid = id or screen_id
    if not sid or not str(sid).strip():
        return OpResult(
            kind="screen",
            status="error",
            code="MISSING_ARG",
            fields={"op": "close", "msg": "screen id required (id=)"},
            hint="mcp-remote-control-cli screen close --id <screen_id>",
        )
    sid = str(sid).strip()
    reg = get_screen_registry()
    sess = reg.remove(sid)
    if sess is None:
        return OpResult(
            kind="screen",
            status="error",
            code="SCREEN_NOT_FOUND",
            fields={
                "op": "close",
                "id": sid,
                "msg": f"screen not open: {sid}",
            },
        )
    return OpResult(
        kind="screen",
        status="ok",
        fields={
            "op": "close",
            "id": sid,
            "screen_id": sid,
            "ep": sess.ep,
            "closed": True,
        },
        cwd=sess.cwd,
    )


def list_screens(**_kwargs: Any) -> OpResult:
    reg = get_screen_registry()
    sessions = reg.list_open()
    lines: list[str] = []
    for s in sessions:
        alive = "1" if (not s.closed and s.pty.is_alive()) else "0"
        size = f"{s.cols}x{s.rows}"
        cwd_bit = f" cwd={s.cwd}" if s.cwd else ""
        lines.append(
            f"id={s.id} ep={s.ep} {size} gen={s.generation} "
            f"alive={alive} open={s.open_mode}{cwd_bit}"
        )
    body = "\n".join(lines) if lines else None
    return OpResult(
        kind="screen",
        status="ok",
        fields={
            "op": "list",
            "n": len(sessions),
        },
        body=body,
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch screen op → Core implementation."""
    op_norm = (op or "").strip().lower()
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="screen",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown screen op (want open|send|close|list)",
            },
            hint="use op=open|send|close|list",
        )
    dispatch = {
        "open": open_screen,
        "send": send_screen,
        "close": close_screen,
        "list": list_screens,
    }
    return dispatch[op_norm](**kwargs)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_pty(
    transport: BaseTransport,
    *,
    cols: int,
    rows: int,
    cwd: str | None,
    shell: str | None,
    command: str | None,
    argv: list[str] | None,
    env: dict[str, str],
) -> LocalPty | SshPty:
    name = transport.name
    if name == "local":
        run_argv: list[str] | None
        if argv:
            run_argv = list(argv)
        elif command is not None:
            # Replace the shell main process with ``shell -lc command``.
            run_argv = [shell or "/bin/bash", "-lc", command]
        else:
            run_argv = None  # LocalPty default: interactive shell -i
        return LocalPty(
            cols=cols,
            rows=rows,
            cwd=cwd,
            shell=shell,
            env=env,
            argv=run_argv,
        )

    if name == "ssh":
        conn = getattr(transport, "connection", None)
        if conn is None:
            conn = getattr(transport, "_conn", None)
        if conn is None:
            raise TransportError(
                "NOT_CONNECTED",
                "ssh transport has no connection",
            )
        return SshPty.open_shell(
            conn,
            cols=cols,
            rows=rows,
            cwd=cwd,
            env=env,
            command=command,
            argv=list(argv) if argv else None,
        )

    if name == "winrm":
        raise TransportError(
            "UNSUPPORTED",
            "winrm has no interactive PTY screen",
            details={"transport": "winrm"},
        )

    raise TransportError(
        "UNSUPPORTED",
        f"screen PTY not supported on transport {name!r}",
        details={"transport": name},
    )


def _resolve_open_cwd(
    *,
    requested: str | None,
    endpoint_cwd: str | None,
    transport: BaseTransport,
) -> str | None:
    raw = requested if requested is not None else endpoint_cwd
    if raw is None or not str(raw).strip():
        raw = transport.cwd
    if raw is None or not str(raw).strip():
        if transport.name == "local":
            return os.getcwd()
        return None
    text = str(raw).strip()
    if transport.name == "local":
        p = Path(text).expanduser()
        try:
            if not p.is_absolute():
                p = (Path(os.getcwd()) / p).resolve()
            else:
                p = p.resolve()
        except OSError:
            return text
        if p.is_dir():
            return str(p)
        # Profile seed may point at a missing path; fall back to $HOME or cwd.
        home = os.environ.get("HOME")
        if home and Path(home).is_dir():
            return str(Path(home).resolve())
        return os.getcwd()
    # Remote: expand ~ with transport.home when known.
    if text.startswith("~") and transport.home:
        if text == "~" or text.startswith("~/"):
            text = transport.home + text[1:]
    return text


def _transport_error(
    exc: TransportError,
    *,
    op: str,
    ep: str,
) -> OpResult:
    fields: dict[str, Any] = {
        "op": op,
        "ep": ep,
        "msg": exc.msg,
    }
    if exc.details.get("host"):
        fields["host"] = exc.details["host"]
    if exc.details.get("transport"):
        fields["transport"] = exc.details["transport"]
    return OpResult(
        kind="screen",
        status="error",
        code=exc.code or "EXEC_FAILED",
        fields=fields,
    )


def _home(home: Path | str | None) -> Path:
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()


def _dialect_from_endpoint(
    endpoint: Any,
    *,
    shell_path: str | None = None,
) -> tuple[str | None, str | None, dict[str, Any] | None, dict[str, Any]]:
    """Bind dialect/caps from endpoint probe data and optional open shell= hint."""
    probe = getattr(endpoint, "probe", None) or {}
    transport = getattr(endpoint, "transport", None)
    tmeta = getattr(transport, "meta", None) or {}
    merged: dict[str, Any] = {}
    if isinstance(tmeta, dict):
        merged.update(tmeta)
    if isinstance(probe, dict):
        merged.update(probe)

    path_hint = shell_path or merged.get("shell_path")
    if isinstance(path_hint, str) and path_hint.strip():
        path_hint = path_hint.strip()
    else:
        path_hint = None

    # Explicit shell= on open overrides the path basename for dialect resolve.
    base_hint = None
    if shell_path:
        base = shell_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        base = base.removesuffix(".exe")
        base_hint = base or None

    caps_raw = merged.get("caps")
    caps: dict[str, Any]
    if isinstance(caps_raw, dict):
        caps = dict(caps_raw)
    else:
        caps = {
            "busybox": bool(merged.get("busybox")),
            "pwd_p": bool(merged.get("cap_pwd_p") or merged.get("pwd_p")),
        }

    dialect = merged.get("dialect")
    if not dialect or shell_path:
        bb_flag: bool | None = None
        if merged.get("busybox") is not None or caps_raw is not None:
            bb_flag = bool(merged.get("busybox") or caps.get("busybox"))
        dialect = resolve_dialect(
            shell_base=base_hint or str(merged.get("shell_base") or "") or None,
            shell_path=path_hint or str(merged.get("shell_path") or "") or None,
            shell_family=str(merged.get("shell_family") or "") or None,
            busybox=bb_flag,
            flags=merged,
            os_name=str(merged.get("os") or "") or None,
        )

    meta = {
        "shell_base": merged.get("shell_base") or base_hint,
        "shell_path": path_hint or merged.get("shell_path"),
        "shell_family": merged.get("shell_family"),
        "dialect": dialect,
        "busybox": merged.get("busybox"),
        "os": merged.get("os"),
    }
    return (
        str(dialect) if dialect else None,
        path_hint or (str(merged.get("shell_path")) if merged.get("shell_path") else None),
        caps,
        meta,
    )


def _short(msg: str, limit: int = 200) -> str:
    text = " ".join(str(msg).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
