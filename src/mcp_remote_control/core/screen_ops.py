"""Screen Core ops: open, send, close, and list interactive PTY sessions.

Owns the agent loop for host shells (local/ssh): open a PTY with adaptive
geometry, run ordered send actions with wait/shot, and tear down sessions.
Endpoints lacking ``caps.screen`` (e.g. WinRM) return ``CAP_DENIED``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound
from mcp_remote_control.core.result import OpResult, _home, _short
from mcp_remote_control.endpoint.registry import ensure_endpoint, get_registry
from mcp_remote_control.screen.buffer import (
    DEFAULT_COLORTERM,
    DEFAULT_TERM,
)
from mcp_remote_control.screen.cwd_probe import (
    probe_and_update_cwd,
    refresh_session_surface,
)
from mcp_remote_control.screen.geometry import GeometryAdapter, GeometryMemory
from mcp_remote_control.screen.registry import get_screen_registry
from mcp_remote_control.screen.send import execute_send
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.screen.ssh_pty import SshPty
from mcp_remote_control.serial.buffer import resolve_text_codec, text_codec_known
from mcp_remote_control.shell.dialect import resolve_dialect
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path

if TYPE_CHECKING:
    from mcp_remote_control.screen.local_pty import LocalPty

VALID_OPS: frozenset[str] = frozenset({"open", "send", "close", "list"})

# Brief settle so shell MOTD/prompt can paint before the first frame.
_OPEN_SETTLE_S = 0.35

# ``msg`` cap for a failed send. An action-loop failure carries a curated
# remedy from ``screen/send.py`` - the longest today is the SGR no-tracking
# advice (205 chars) behind the ``action_<i>_failed: `` prefix (17) - and that
# text is what the caller acts on, so the cap must clear it whole. It still
# bounds a runaway interpolated exception repr; it is not a display budget.
_SEND_MSG_CHARS = 300

# Elision marker for a capped send ``msg``; keeps the ``diagnosis; remedy``
# shape readable across the cut.
_SEND_MSG_ELISION = "...; "


def _send_msg(msg: str, limit: int = _SEND_MSG_CHARS) -> str:
    """Cap a failed-send ``msg`` without cutting its trailing remedy.

    A caller-supplied value can sit *in front of* the curated remedy - an
    unrecognised ``force_click`` value is interpolated into ``"<field> must be
    a boolean, got <value!r>; <hint>"``, and ``value!r`` is unbounded - so a
    plain head truncation spends the whole budget on the value and the remedy
    never arrives. ``screen/send.py`` writes every remedy as the message's
    last ``"; "`` clause, and the remedy is the part the caller acts on, so
    the cut falls on the diagnosis instead: that clause is kept whole and only
    the text before it is shortened.

    Falls back to the plain head cut when the message has no such clause or
    when keeping it would leave the diagnosis less than half the budget - the
    field stays bounded either way, and a runaway repr with no remedy still
    renders exactly as before.
    """
    text = " ".join(str(msg).split())
    if len(text) <= limit:
        return text
    cut = text.rfind("; ")
    if cut > 0:
        tail = text[cut + 2 :]
        if tail and len(tail) * 2 <= limit:
            room = limit - len(tail) - len(_SEND_MSG_ELISION)
            return text[:room] + _SEND_MSG_ELISION + tail
    return _short(text, limit=limit)


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


def _codec_fields(session: ScreenSession) -> dict[str, Any]:
    """Which codec read this frame, and whether its decode dropped bytes.

    Every row that carries frame text needs this, not just ``open``: a send
    draws its frame from the same pinned decoder, so an agent holding only the
    session id has to be able to tell a legacy-console frame from a utf-8 one
    without the open result. Both keys are omitted on the historic path
    (utf-8, no replacements) so routine frames keep their token count.
    """
    out: dict[str, Any] = {}
    if session.text_codec != "utf-8":
        out["encoding"] = session.text_codec
    if session.replaced_chars:
        out["repl"] = 1
    return out


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

    Geometry is adaptive: class seed -> settle health -> limited grow. Pass both
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
            code="CAP_DENIED",
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

    # Peer text codec for the PTY byte->text boundary. The transport already
    # carries it (profile ``encoding``, or the probe's charmap/chcp adoption),
    # so a screen on a non-UTF-8 console decodes its own frames instead of
    # rendering replacement characters for every non-ASCII glyph. Resolved once
    # here, at open: an unusable name warns before the PTY exists and leaves
    # utf-8 in force, and a later re-probe cannot switch it mid-session.
    peer_encoding = getattr(transport, "text_encoding", None)
    peer_codec = resolve_text_codec(peer_encoding)
    unknown_peer_codec = (
        f"unusable text codec {str(peer_encoding)!r}; screen frames are decoded "
        "as utf-8 (set the profile encoding to a Python codec name such as "
        "gb18030)"
        if peer_encoding is not None and not text_codec_known(peer_encoding)
        else None
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

    try:
        work_cwd = _resolve_open_cwd(
            requested=cwd,
            endpoint_cwd=endpoint.cwd,
            transport=transport,
        )
    except TransportError as exc:
        return _transport_error(exc, op="open", ep=ep_name)

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

    # Long PTY open may outlive concurrent close/retire. Re-pin
    # generation + transport liveness before building a session.
    if not get_registry().generation_still_open(endpoint):
        try:
            pty_handle.close()
        except Exception:  # noqa: BLE001
            pass
        return OpResult(
            kind="screen",
            status="error",
            code="NOT_CONNECTED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": "endpoint closed or transport died during screen open",
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
        text_encoding=peer_codec,
    )

    # Ready fence + serial open probe.
    # Hold serial_ops across settle/adapt and multi-step cwd probe so a
    # concurrent send cannot interleave writes (ctrl+u -> cmd -> enter -> drain).
    # Delay reg.add until mark_ready so mid-open send gets SCREEN_NOT_FOUND
    # (predictable) instead of polluting probe markers / the first frame.
    settle = _OPEN_SETTLE_S if settle_s is None else float(settle_s)
    out_cwd = session.cwd
    try:
        with session.serial_ops():
            session.mark_not_ready()
            try:
                fit_result, shot = adapter.adapt(
                    session,
                    plan,
                    settle_s=settle,
                    endpoint_id=ep_name,
                    surface=session.surface,
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
                return OpResult(
                    kind="screen",
                    status="error",
                    code="EXEC_FAILED",
                    fields={
                        "op": "open",
                        "ep": ep_name,
                        "msg": _short(
                            f"settle/shot failed: {type(exc).__name__}: {exc}"
                        ),
                    },
                )

            # Prefer absolute cwd for Agent output; silent probe when still shell.
            out_cwd = session.cwd
            if out_cwd and transport.name == "local":
                try:
                    out_cwd = str(Path(out_cwd).expanduser().resolve())
                except OSError:
                    pass
            session.cwd = out_cwd
            # Refresh surface from live pyte modes (alt-screen / mouse)
            # before deciding whether to inject a silent cwd probe.
            try:
                refresh_session_surface(session)
            except Exception:  # noqa: BLE001
                pass
            if open_mode == "shell" and session.surface == "shell":
                try:
                    probed = probe_and_update_cwd(session, timeout_s=1.2)
                    # probe_and_update_cwd returns the prior cwd on failure;
                    # only cwd_src=probe means the requested path was confirmed.
                    if session.cwd_src == "probe" and probed:
                        out_cwd = probed
                        # Re-shot so the Agent frame excludes probe marker lines.
                        shot = session.shot(settle_s=0.05)
                    elif session.cwd_src == "stale":
                        # Probe ran and failed. Local chdir already applied a
                        # verified directory; remote cwd is unconfirmed.
                        if transport.name != "local":
                            out_cwd = None
                            session.cwd = None
                    elif session.cwd_src is None:
                        session.cwd_src = "stale"
                except Exception:  # noqa: BLE001
                    if session.cwd_src == "probe":
                        session.cwd_src = "stale"
                    if session.cwd_src is None:
                        session.cwd_src = "stale"
                    if transport.name != "local":
                        out_cwd = None
                        session.cwd = None
                # Re-evaluate surface after probe I/O (cheap).
                try:
                    refresh_session_surface(session)
                except Exception:  # noqa: BLE001
                    pass

            # Re-validate generation/liveness after settle/probe (long
            # window) immediately before publish. Concurrent close_endpoint
            # must not leave a screen session on a dead/retired transport.
            if not get_registry().generation_still_open(endpoint):
                try:
                    if not session.closed:
                        session.close()
                except Exception:  # noqa: BLE001
                    pass
                return OpResult(
                    kind="screen",
                    status="error",
                    code="NOT_CONNECTED",
                    fields={
                        "op": "open",
                        "ep": ep_name,
                        "msg": (
                            "endpoint closed or transport died during "
                            "screen open settle"
                        ),
                    },
                )

            # Publish only when ready: concurrent send can attach after this.
            session.mark_ready()
            reg.add(session)
            # Post-add fence: if close raced between check and add, drop the
            # zombie immediately (close may have snapshotted empty ids).
            if not get_registry().generation_still_open(endpoint):
                try:
                    reg.remove(sid)
                except Exception:  # noqa: BLE001
                    pass
                return OpResult(
                    kind="screen",
                    status="error",
                    code="NOT_CONNECTED",
                    fields={
                        "op": "open",
                        "ep": ep_name,
                        "msg": (
                            "endpoint closed or transport died during "
                            "screen open register"
                        ),
                    },
                )
    except Exception as exc:  # noqa: BLE001
        # Unexpected failure outside adapt (should be rare); tear down PTY.
        try:
            if not session.closed:
                session.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            reg.remove(sid)
        except Exception:  # noqa: BLE001
            pass
        return OpResult(
            kind="screen",
            status="error",
            code="EXEC_FAILED",
            fields={
                "op": "open",
                "ep": ep_name,
                "msg": _short(f"open finalize failed: {type(exc).__name__}: {exc}"),
            },
        )

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
    # Which codec produced this frame, and whether it dropped bytes: a
    # non-default codec reads the same bytes as different text, so a reader
    # must be able to tell them apart.
    fields.update(_codec_fields(session))
    if unknown_peer_codec:
        fields["warning"] = unknown_peer_codec
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
    """Execute ordered actions, wait, drain, then take a frame shot by default.

    Concurrent send/close on the same session is serialized by the session
    op lock: ``execute_send`` holds ``session.serial_ops()`` for the
    full pipeline; nested RLock re-entry from write/drain/feed is safe.

    Sessions are only registered after open settle/probe marks them ready.
    A not-ready session (defense if observed early) returns SCREEN_NOT_READY
    rather than interleaving with the open-path probe writes.
    """
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

    # Re-check closed/ready under the session op lock so a concurrent close or
    # mid-open race is not followed by a half-started send. execute_send
    # re-enters the same RLock for the action/wait/drain pipeline.
    with sess.serial_ops():
        if sess.closed:
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
        if not sess.ready:
            return OpResult(
                kind="screen",
                status="error",
                code="SCREEN_NOT_READY",
                fields={
                    "op": "send",
                    "id": sid,
                    "screen_id": sid,
                    "msg": f"screen not ready (open settle/probe in progress): {sid}",
                },
                cwd=sess.cwd,
            )
        try:
            # _execute_send_locked via execute_send - nested serial_ops (RLock).
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
        # Agent meta surface tracks live alt-screen / mouse after send
        # (probe path also refreshes inside _should_probe; cover noop/skip paths).
        try:
            refresh_session_surface(sess)
        except Exception:  # noqa: BLE001
            pass

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
        fields["msg"] = _send_msg(outcome.error_msg or outcome.error_code)
    if outcome.status == "unchanged":
        fields["unchanged"] = True
    if sess.dialect:
        fields["dialect"] = sess.dialect
    if sess.shell_caps and sess.shell_caps.get("busybox"):
        fields["busybox"] = 1
    if sess.cwd_src:
        fields["cwd_src"] = sess.cwd_src
    # Same codec evidence as the open row: this frame was drawn by the pinned
    # decoder, and a reader with only the session id can see which one.
    fields.update(_codec_fields(sess))

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
    """Close one screen; serialized with concurrent send on the same session."""
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
    # Lookup first so we can hold the session op lock across pop+close and
    # wait out any in-flight send. Registry map lock is only held briefly
    # inside remove(); never hold registry lock across PTY I/O.
    sess = reg.get(sid)
    if sess is None or sess.closed:
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
    with sess.serial_ops():
        if sess.closed:
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
        # remove() pops under the registry map lock then calls sess.close(),
        # which re-enters this RLock. Concurrent send waits on serial_ops.
        removed = reg.remove(sid)
        if removed is None:
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
        sess = removed
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
    """Dispatch screen op -> Core implementation."""
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
        # Local interactive PTY needs POSIX fcntl/termios/pty; reject on Windows
        # before importing local_pty so win32 hosts can still import mcp_server.
        if sys.platform.startswith("win"):
            raise TransportError(
                "UNSUPPORTED",
                "local interactive PTY is not supported on Windows",
                details={"transport": "local", "platform": sys.platform},
            )
        # Lazy: avoid pulling POSIX-only modules at screen_ops / mcp_server import.
        from mcp_remote_control.screen.local_pty import LocalPty

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
    """Resolve screen-open cwd with the same fail-close rules as exec.

    Explicit local missing/non-dir cwd raises ``INVALID_CWD`` - never
    ``$HOME`` / ``getcwd`` fallback. Profile/transport seeds may still
    skip a missing directory and land on ``getcwd()``. Non-path values
    (``True``, unexpanded probe placeholders) are rejected by
    ``coerce_cwd_path``.
    """
    requested_path = coerce_cwd_path(requested)
    if transport.name == "local":
        return _resolve_local_open_cwd(
            requested=requested_path,
            endpoint_cwd=coerce_cwd_path(endpoint_cwd),
            transport_cwd=coerce_cwd_path(transport.cwd),
            strict_requested=requested_path is not None,
        )

    raw = requested_path
    if raw is None:
        raw = coerce_cwd_path(endpoint_cwd)
    if raw is None:
        raw = coerce_cwd_path(transport.cwd)
    if raw is None:
        return None
    if raw.startswith("~") and transport.home:
        if raw == "~" or raw.startswith("~/"):
            raw = transport.home + raw[1:]
    return raw


def _resolve_local_open_cwd(
    *,
    requested: str | None,
    endpoint_cwd: str | None,
    transport_cwd: str | None,
    strict_requested: bool,
) -> str:
    candidates: list[str] = []
    for raw in (requested, endpoint_cwd, transport_cwd):
        if raw:
            candidates.append(raw)
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


# ---------------------------------------------------------------------------
# Generation-fence helpers.
# Used by endpoint registry on stale transport pop/reconnect and by
# close_endpoint. Snapshot ids first, then close_ids only - never name-wide
# close_for_endpoint after a same-name reopen can register new sessions.
# ---------------------------------------------------------------------------


def snapshot_endpoint_session_ids(ep: str) -> list[str]:
    """Snapshot open screen session ids attached to *ep*.

    Safe under concurrent registration: returns only ids present at call time.
    Best-effort; returns ``[]`` on registry failure.
    """
    if not ep or not str(ep).strip():
        return []
    try:
        return get_screen_registry().ids_for_endpoint(str(ep).strip())
    except Exception:  # noqa: BLE001
        return []


def close_sessions_by_ids(session_ids: Any) -> int:
    """Close only the given screen session ids if still registered.

    Returns count closed. Ids registered after the snapshot are never touched.
    Best-effort; never raises (reconnect/close paths must not fail on cleanup).
    """
    if not session_ids:
        return 0
    try:
        return int(get_screen_registry().close_ids(session_ids))
    except Exception:  # noqa: BLE001
        return 0
