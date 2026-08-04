"""screen_send executor: ordered actions → wait → drain → shot.

Agent-facing contract: apply actions in order, wait for settle, optionally
refresh cwd via silent shell probe, then return a frame (or status=unchanged
when the hash is stable after a pure re-shot / poll).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.screen.buffer import (
    cursor_rc,
    dump_frame,
    find_text,
    mouse_tracking_enabled,
)
from mcp_remote_control.screen.cwd_probe import update_cwd_after_send
from mcp_remote_control.screen.keys import (
    KeyEncodeError,
    encode_key,
    encode_keys,
    encode_mouse_sgr,
    encode_paste,
    encode_raw_hex,
    encode_text,
)
from mcp_remote_control.screen.session import ScreenSession

# Default WaitSpec settle windows.
DEFAULT_IDLE_MS = 200
DEFAULT_TIMEOUT_MS = 30_000
# Max direction keys for a single go(how=keys)
GO_STEP_CAP = 200

# Supported action type catalog.
ACTION_TYPES: frozenset[str] = frozenset(
    {
        "nop",
        "key",
        "keys",
        "text",
        "paste",
        "raw",
        "go",
        "click",
        "move",
        "to_text",
        "submit",
        "clear_line",
        "interrupt",
        "eof",
        "escape",
        "resize",
        "wait",
    }
)

# Realization summary for each action type.
ACTION_IMPL: dict[str, str] = {
    "nop": "no-op",
    "key": "encode_key → PTY write",
    "keys": "encode_keys → PTY write",
    "text": "encode_text (+ enter if submit)",
    "paste": "bracketed paste (+ enter if submit)",
    "raw": "hex → raw bytes",
    "go": "click if mouse/how=click else direction keys from cur",
    "click": "SGR mouse press+release",
    "move": "relative direction keys",
    "to_text": "find_text on frame → go/click",
    "submit": "key enter",
    "clear_line": "key ctrl+u",
    "interrupt": "key ctrl+c",
    "eof": "key ctrl+d",
    "escape": "key escape",
    "resize": "session.resize",
    "wait": "mid-sequence drain/sleep",
}


class ActionError(Exception):
    """Action failed; carry a short code/msg for OpResult fields."""

    def __init__(self, code: str, msg: str, *, index: int | None = None) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.index = index


@dataclass
class SendOutcome:
    """Result of one send pipeline run (before wrapping as OpResult)."""

    status: str  # ok | unchanged | error | dead
    frame: str | None
    hash: str
    cur: str
    gen: int
    cols: int
    rows: int
    alive: bool
    exit_code: int | None
    did: list[str] = field(default_factory=list)
    wait_until: str | None = None
    nav: str | None = None
    pre_hash: str | None = None
    error_code: str | None = None
    error_msg: str | None = None
    shot: bool = True
    cwd: str | None = None


def normalize_actions(actions: Any) -> list[dict[str, Any]]:
    """Coerce *actions* to a list of dicts; empty/None → []."""
    if actions is None:
        return []
    if isinstance(actions, str):
        # Treat bare string as a single text action (CLI convenience).
        return [{"type": "text", "text": actions}]
    if not isinstance(actions, list):
        raise ActionError("INVALID_ARG", "actions must be a JSON array")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(actions):
        if not isinstance(item, Mapping):
            raise ActionError(
                "INVALID_ARG",
                f"actions[{i}] must be an object",
                index=i,
            )
        out.append(dict(item))
    return out


def normalize_wait(
    wait: Any,
    *,
    actions_nonempty: bool,
) -> dict[str, Any]:
    """Normalize WaitSpec with idle/timeout defaults."""
    if wait is None:
        if actions_nonempty:
            return {
                "until": "idle",
                "idle_ms": DEFAULT_IDLE_MS,
                "timeout_ms": DEFAULT_TIMEOUT_MS,
            }
        # Pure shot / empty actions: immediate drain+shot
        return {
            "until": "deadline",
            "timeout_ms": 0,
            "idle_ms": DEFAULT_IDLE_MS,
        }
    if not isinstance(wait, Mapping):
        raise ActionError("INVALID_ARG", "wait must be an object")
    w = dict(wait)
    until = str(w.get("until") or "idle").lower()
    w["until"] = until
    if "idle_ms" not in w:
        w["idle_ms"] = DEFAULT_IDLE_MS
    if "timeout_ms" not in w:
        w["timeout_ms"] = DEFAULT_TIMEOUT_MS
    return w


def apply_action(session: ScreenSession, action: Mapping[str, Any]) -> str:
    """Apply one action; return short type name for did= list.

    May raise ActionError / KeyEncodeError.
    """
    atype = str(action.get("type") or action.get("op") or "").strip().lower()
    if not atype:
        raise ActionError("INVALID_ARG", "action missing type")

    if atype == "nop":
        return "nop"

    if atype == "text":
        text = action.get("text")
        if text is None:
            raise ActionError("INVALID_ARG", "text action requires text=")
        session.write(encode_text(str(text)))
        if _truthy(action.get("submit")):
            session.write(encode_key("enter"))
            return "text+submit"
        return "text"

    if atype == "key":
        key = action.get("key")
        if key is None:
            raise ActionError("INVALID_ARG", "key action requires key=")
        session.write(encode_key(str(key)))
        return "key"

    if atype == "keys":
        keys = action.get("keys")
        if keys is None and action.get("key") is not None:
            keys = [action["key"]]
        if not isinstance(keys, list) or not keys:
            raise ActionError("INVALID_ARG", "keys action requires keys=[...]")
        session.write(encode_keys(str(k) for k in keys))
        return "keys"

    if atype == "paste":
        text = action.get("text")
        if text is None:
            raise ActionError("INVALID_ARG", "paste action requires text=")
        bracketed = True
        if "bracketed" in action:
            bracketed = _truthy(action.get("bracketed"))
        session.write(encode_paste(str(text), bracketed=bracketed))
        if _truthy(action.get("submit")):
            session.write(encode_key("enter"))
            return "paste+submit"
        return "paste"

    if atype == "raw":
        hex_str = action.get("hex") or action.get("data")
        if hex_str is None:
            raise ActionError("INVALID_ARG", "raw action requires hex=")
        session.write(encode_raw_hex(str(hex_str)))
        return "raw"

    if atype == "submit":
        session.write(encode_key("enter"))
        return "submit"

    if atype == "interrupt":
        session.write(encode_key("ctrl+c"))
        return "interrupt"

    if atype == "eof":
        session.write(encode_key("ctrl+d"))
        return "eof"

    if atype == "escape":
        session.write(encode_key("escape"))
        return "escape"

    if atype == "clear_line":
        session.write(encode_key("ctrl+u"))
        return "clear_line"

    if atype == "wait":
        # Mid-sequence wait (ms only)
        ms = action.get("ms", action.get("timeout_ms", 0))
        try:
            delay = max(0.0, float(ms) / 1000.0)
        except (TypeError, ValueError) as exc:
            raise ActionError("INVALID_ARG", f"wait.ms invalid: {ms!r}") from exc
        if delay > 0:
            # Drain while sleeping so buffer stays warm.
            remaining = delay
            while remaining > 0:
                step = min(0.05, remaining)
                session.drain(step)
                remaining -= step
        return "wait"

    if atype == "resize":
        cols = action.get("cols", session.cols)
        rows = action.get("rows", session.rows)
        try:
            session.resize(int(cols), int(rows))
        except Exception as exc:
            raise ActionError(
                "EXEC_FAILED",
                f"resize failed: {type(exc).__name__}: {exc}",
            ) from exc
        return "resize"

    if atype == "go":
        return _action_go(session, action)

    if atype == "click":
        return _action_click(session, action)

    if atype == "move":
        return _action_move(session, action)

    if atype == "to_text":
        return _action_to_text(session, action)

    raise ActionError("INVALID_ARG", f"unknown action type: {atype!r}")


def _action_go(session: ScreenSession, action: Mapping[str, Any]) -> str:
    if "row" not in action or "col" not in action:
        raise ActionError("INVALID_ARG", "go requires row= and col=")
    try:
        target_r = int(action["row"])
        target_c = int(action["col"])
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "go row/col must be ints") from exc

    how = str(action.get("how") or "auto").lower()
    if how == "auto":
        if mouse_tracking_enabled(session.screen):
            how = "click"
        else:
            how = "keys"

    if how == "click":
        _action_click(
            session,
            {
                "row": target_r,
                "col": target_c,
                "button": action.get("button", "left"),
                "mods": action.get("mods"),
                "clicks": action.get("clicks", 1),
            },
        )
        return "go"
    if how == "keys":
        _go_by_keys(session, target_r, target_c)
        return "go"
    raise ActionError("INVALID_ARG", f"go.how unsupported: {how!r}")


def _go_by_keys(session: ScreenSession, target_r: int, target_c: int) -> None:
    # Drain briefly so cur is current
    session.drain(0.02)
    cur_r, cur_c = cursor_rc(session.screen)
    dr = target_r - cur_r
    dc = target_c - cur_c
    steps = abs(dr) + abs(dc)
    if steps > GO_STEP_CAP:
        raise ActionError(
            "NAV_CAPPED",
            f"go keys would need {steps} steps (cap {GO_STEP_CAP})",
        )
    seq: list[str] = []
    if dr < 0:
        seq.extend(["up"] * (-dr))
    elif dr > 0:
        seq.extend(["down"] * dr)
    if dc < 0:
        seq.extend(["left"] * (-dc))
    elif dc > 0:
        seq.extend(["right"] * dc)
    if seq:
        session.write(encode_keys(seq))


def _action_click(session: ScreenSession, action: Mapping[str, Any]) -> str:
    if "row" not in action or "col" not in action:
        raise ActionError("INVALID_ARG", "click requires row= and col=")
    try:
        row = int(action["row"])
        col = int(action["col"])
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "click row/col must be ints") from exc
    button = str(action.get("button") or "left")
    mods = action.get("mods")
    mod_list = list(mods) if isinstance(mods, list) else None
    # *clicks* is the number of press+release pairs. Missing → 1; explicit 0
    # must emit nothing (do not treat 0 as falsy). Negatives clamp to 0.
    c = action.get("clicks", 1)
    clicks = int(c) if c is not None else 1
    clicks = max(0, clicks)
    # First press+release pair (gated so clicks=0 emits nothing).
    if clicks >= 1:
        session.write(
            encode_mouse_sgr(row, col, button=button, press=True, mods=mod_list)
        )
        session.write(
            encode_mouse_sgr(row, col, button=button, press=False, mods=mod_list)
        )
    # Additional pairs so total press+release count equals *clicks*.
    for _ in range(max(0, clicks - 1)):
        session.write(
            encode_mouse_sgr(row, col, button=button, press=True, mods=mod_list)
        )
        session.write(
            encode_mouse_sgr(row, col, button=button, press=False, mods=mod_list)
        )
    return "click"


def _action_move(session: ScreenSession, action: Mapping[str, Any]) -> str:
    rd = int(action.get("row_delta") or 0)
    cd = int(action.get("col_delta") or 0)
    seq: list[str] = []
    if rd < 0:
        seq.extend(["up"] * (-rd))
    elif rd > 0:
        seq.extend(["down"] * rd)
    if cd < 0:
        seq.extend(["left"] * (-cd))
    elif cd > 0:
        seq.extend(["right"] * cd)
    if abs(rd) + abs(cd) > GO_STEP_CAP:
        raise ActionError("NAV_CAPPED", "move exceeds step cap")
    if seq:
        session.write(encode_keys(seq))
    return "move"


def _action_to_text(session: ScreenSession, action: Mapping[str, Any]) -> str:
    """Find visible text on the current frame; go/click that cell."""
    text = action.get("text")
    if text is None or str(text) == "":
        raise ActionError("INVALID_ARG", "to_text requires text=")
    nth = action.get("nth", 1)
    try:
        nth_i = int(nth)
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "to_text.nth must be int") from exc

    row_hint = action.get("row")
    col_hint = action.get("col")
    try:
        row_i = int(row_hint) if row_hint is not None else None
        col_i = int(col_hint) if col_hint is not None else None
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "to_text row/col must be ints") from exc

    # Fresh drain so search sees current paint.
    session.drain(0.02)
    found = find_text(
        session.screen,
        str(text),
        nth=nth_i,
        row=row_i,
        col=col_i,
    )
    if found is None:
        raise ActionError(
            "NAV_TEXT_NOT_FOUND",
            f"to_text not found: {text!r} nth={nth_i}",
        )

    target_r, target_c = found
    do_click = True if action.get("click") is None else _truthy(action.get("click"))
    if do_click:
        # Prefer SGR click when mouse tracking is on; else keys approach.
        if mouse_tracking_enabled(session.screen) or _truthy(
            action.get("force_click")
        ):
            _action_click(
                session,
                {
                    "row": target_r,
                    "col": target_c,
                    "button": action.get("button", "left"),
                    "mods": action.get("mods"),
                    "clicks": action.get("clicks", 1),
                },
            )
        else:
            # When click=true, still emit SGR click bytes (apps without tracking
            # ignore them) and approach with keys so focus can move.
            _action_click(
                session,
                {
                    "row": target_r,
                    "col": target_c,
                    "button": action.get("button", "left"),
                },
            )
            # Keys fallback when the app ignores mouse events.
            try:
                _go_by_keys(session, target_r, target_c)
            except ActionError:
                pass
    else:
        _go_by_keys(session, target_r, target_c)
    return "to_text"


def wait_after(
    session: ScreenSession,
    wait: Mapping[str, Any],
) -> str:
    """Apply top-level WaitSpec; return the until mode used."""
    until = str(wait.get("until") or "idle").lower()
    try:
        idle_ms = float(wait.get("idle_ms", DEFAULT_IDLE_MS))
        timeout_ms = float(wait.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        min_ms = float(wait.get("min_ms", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", f"wait numeric fields invalid: {exc}") from exc

    idle_s = max(0.0, idle_ms / 1000.0)
    timeout_s = max(0.0, timeout_ms / 1000.0)
    min_s = max(0.0, min_ms / 1000.0)

    if until in ("deadline", "timeout"):
        if timeout_s > 0:
            _drain_for_duration(session, timeout_s)
        else:
            session.drain(0.02)
        return until

    if until == "text":
        needle = wait.get("text")
        if not needle:
            raise ActionError("INVALID_ARG", "wait.until=text requires text=")
        _wait_for_text(session, str(needle), timeout_s=timeout_s, gone=False)
        return "text"

    if until == "text_gone" or (until == "idle" and wait.get("text_gone")):
        # Support text_gone via until or field
        needle = wait.get("text_gone") or wait.get("text")
        if not needle:
            raise ActionError("INVALID_ARG", "wait text_gone requires text_gone=")
        _wait_for_text(session, str(needle), timeout_s=timeout_s, gone=True)
        return "text_gone"

    # default: idle (no PTY output for idle_ms, after optional min_ms)
    _wait_idle(session, idle_s=idle_s, timeout_s=timeout_s, min_s=min_s)
    return "idle"


def _drain_for_duration(session: ScreenSession, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        session.drain(min(0.05, max(0.0, remaining)))


def _wait_idle(
    session: ScreenSession,
    *,
    idle_s: float,
    timeout_s: float,
    min_s: float,
) -> None:
    start = time.monotonic()
    if min_s > 0:
        _drain_for_duration(session, min_s)

    # No idle required → just honor remaining timeout as a settle, or instant.
    if idle_s <= 0:
        remaining = timeout_s - (time.monotonic() - start)
        if remaining > 0:
            session.drain(min(0.05, remaining))
        else:
            session.drain(0.02)
        return

    last_data_at = time.monotonic()
    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed >= timeout_s:
            # Timed out waiting for idle — still ok; drain once more.
            session.drain(0.02)
            return
        got = session.drain(0.05)
        now = time.monotonic()
        if got > 0:
            last_data_at = now
            continue
        if (now - last_data_at) >= idle_s:
            return


def _wait_for_text(
    session: ScreenSession,
    needle: str,
    *,
    timeout_s: float,
    gone: bool,
) -> None:
    start = time.monotonic()
    while True:
        session.drain(0.05)
        frame = dump_frame(session.screen)
        present = needle in frame
        if gone and not present:
            return
        if not gone and present:
            return
        if time.monotonic() - start >= timeout_s:
            return  # timeout: still proceed to shot (caller returns frame)


def execute_send(
    session: ScreenSession,
    *,
    actions: Any = None,
    wait: Any = None,
    shot: bool = True,
    probe_cwd: bool = True,
) -> SendOutcome:
    """Full pipeline: actions → wait → cwd probe → drain → optional shot."""
    pre_hash = session.last_hash
    did: list[str] = []
    nav: str | None = None
    err_code: str | None = None
    err_msg: str | None = None

    try:
        acts = normalize_actions(actions)
    except ActionError as exc:
        return _error_outcome(
            session,
            pre_hash=pre_hash,
            code=exc.code,
            msg=exc.msg,
            shot=shot,
        )

    try:
        wait_spec = normalize_wait(wait, actions_nonempty=bool(acts))
    except ActionError as exc:
        return _error_outcome(
            session,
            pre_hash=pre_hash,
            code=exc.code,
            msg=exc.msg,
            shot=shot,
        )

    for i, act in enumerate(acts):
        try:
            name = apply_action(session, act)
            did.append(name)
            if name in ("go", "click", "move", "to_text"):
                nav = "ok"
        except KeyEncodeError as exc:
            err_code = "INVALID_ARG"
            err_msg = f"action_{i}_failed: {exc}"
            break
        except ActionError as exc:
            err_code = exc.code
            err_msg = f"action_{i}_failed: {exc.msg}"
            if exc.code == "NAV_TEXT_NOT_FOUND":
                nav = "text_not_found"
            elif exc.code == "NAV_CAPPED":
                nav = "capped"
            elif "nav" in (exc.code or "").lower():
                nav = "fail"
            break
        except Exception as exc:  # noqa: BLE001
            err_code = "EXEC_FAILED"
            err_msg = f"action_{i}_failed: {type(exc).__name__}: {exc}"
            break

    wait_until: str | None = None
    try:
        wait_until = wait_after(session, wait_spec)
    except ActionError as exc:
        if err_code is None:
            err_code = exc.code
            err_msg = exc.msg

    # Silent cwd probe on shell surfaces. Skip empty / nop-only sends so a pure
    # re-shot can return status=unchanged without the probe rewriting the hash.
    if probe_cwd and not _is_noop_actions(acts):
        try:
            update_cwd_after_send(session, acts, probe=True)
        except Exception:  # noqa: BLE001
            pass

    # Final opportunistic drain so late prompt paint lands in the buffer.
    session.drain(0.05)

    alive = (not session.closed) and session.pty.is_alive()
    exit_code = None if alive else session.pty.exit_code()

    if not shot:
        # Still update a lightweight snapshot for cur/gen meta.
        r, c = cursor_rc(session.screen)
        h = session.last_hash or ""
        return SendOutcome(
            status="error" if err_code else ("dead" if not alive else "ok"),
            frame=None,
            hash=h,
            cur=f"{r},{c}",
            gen=session.generation,
            cols=session.cols,
            rows=session.rows,
            alive=alive,
            exit_code=exit_code,
            did=did,
            wait_until=wait_until,
            nav=nav,
            pre_hash=pre_hash,
            error_code=err_code,
            error_msg=err_msg,
            shot=False,
            cwd=session.cwd,
        )

    snap = session.shot(settle_s=0.0)
    frame = snap["frame"]
    h = snap["hash"]
    # Empty / nop-only send with stable hash → unchanged, omit body frame.
    unchanged = (
        pre_hash is not None
        and h == pre_hash
        and err_code is None
        and alive
    )

    if err_code:
        status = "error"
    elif not alive:
        status = "dead"
    elif unchanged:
        status = "unchanged"
        frame = None  # omit body on unchanged
    else:
        status = "ok"

    return SendOutcome(
        status=status,
        frame=frame,
        hash=h,
        cur=snap["cur"],
        gen=snap["gen"],
        cols=snap["cols"],
        rows=snap["rows"],
        alive=alive,
        exit_code=exit_code,
        did=did,
        wait_until=wait_until,
        nav=nav,
        pre_hash=pre_hash,
        error_code=err_code,
        error_msg=err_msg,
        shot=True,
        cwd=session.cwd,
    )


def _error_outcome(
    session: ScreenSession,
    *,
    pre_hash: str | None,
    code: str,
    msg: str,
    shot: bool,
) -> SendOutcome:
    try:
        session.drain(0.05)
    except Exception:  # noqa: BLE001
        pass
    alive = (not session.closed) and session.pty.is_alive()
    if shot:
        try:
            snap = session.shot(settle_s=0.0)
            return SendOutcome(
                status="error",
                frame=snap["frame"] or None,
                hash=snap["hash"],
                cur=snap["cur"],
                gen=snap["gen"],
                cols=snap["cols"],
                rows=snap["rows"],
                alive=alive,
                exit_code=None if alive else session.pty.exit_code(),
                error_code=code,
                error_msg=msg,
                pre_hash=pre_hash,
                shot=True,
                cwd=session.cwd,
            )
        except Exception:  # noqa: BLE001
            pass
    r, c = cursor_rc(session.screen)
    return SendOutcome(
        status="error",
        frame=None,
        hash=session.last_hash or "",
        cur=f"{r},{c}",
        gen=session.generation,
        cols=session.cols,
        rows=session.rows,
        alive=alive,
        exit_code=None if alive else session.pty.exit_code(),
        error_code=code,
        error_msg=msg,
        pre_hash=pre_hash,
        shot=shot,
        cwd=session.cwd,
    )


def _truthy(val: Any) -> bool:
    if val is True or val is False:
        return bool(val)
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


# Action types that write no meaningful PTY input: the silent cwd probe
# (which injects ctrl+u + an echo command) can be skipped for sends whose
# actions are all in this set, so a pure wait/resize poll can still return
# status=unchanged without the probe rewriting the frame hash.
_NO_PROBE_TYPES: frozenset[str] = frozenset({"nop", "wait", "resize"})


def _is_noop_actions(actions: list[dict[str, Any]]) -> bool:
    """True when actions are empty or only nop/wait/resize (pure re-shot / poll)."""
    if not actions:
        return True
    for act in actions:
        atype = str(act.get("type") or act.get("op") or "").strip().lower()
        if atype and atype not in _NO_PROBE_TYPES:
            return False
    return True


def encode_actions_bytes(actions: list[Mapping[str, Any]]) -> bytes:
    """Encode a list of actions to concatenated PTY bytes (no session I/O).

    Session-dependent actions (go/click/move/to_text/resize/wait/nop) are skipped.
    """
    buf = bytearray()
    for act in actions:
        atype = str(act.get("type") or "").strip().lower()
        if atype == "text":
            buf.extend(encode_text(str(act.get("text") or "")))
            if _truthy(act.get("submit")):
                buf.extend(encode_key("enter"))
        elif atype == "key":
            buf.extend(encode_key(str(act["key"])))
        elif atype == "keys":
            buf.extend(encode_keys(str(k) for k in act.get("keys") or []))
        elif atype == "paste":
            bracketed = True
            if "bracketed" in act:
                bracketed = _truthy(act.get("bracketed"))
            buf.extend(encode_paste(str(act.get("text") or ""), bracketed=bracketed))
            if _truthy(act.get("submit")):
                buf.extend(encode_key("enter"))
        elif atype == "submit":
            buf.extend(encode_key("enter"))
        elif atype == "interrupt":
            buf.extend(encode_key("ctrl+c"))
        elif atype == "eof":
            buf.extend(encode_key("ctrl+d"))
        elif atype == "escape":
            buf.extend(encode_key("escape"))
        elif atype == "clear_line":
            buf.extend(encode_key("ctrl+u"))
        elif atype == "raw":
            buf.extend(encode_raw_hex(str(act.get("hex") or "")))
        elif atype in ACTION_TYPES:
            # Non-byte or session-dependent; skip in pure encoding mode.
            continue
        else:
            raise ActionError("INVALID_ARG", f"unknown action type: {atype!r}")
    return bytes(buf)
