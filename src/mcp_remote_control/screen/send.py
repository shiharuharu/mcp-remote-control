"""screen_send executor: ordered actions -> wait -> drain -> shot.

Agent-facing contract: apply actions in order, wait for settle, optionally
refresh cwd via silent shell probe, then return a frame (or status=unchanged
when the hash is stable after a pure re-shot / poll).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.screen.actions import (
    _is_noop_actions,
    action_truthy,
    actions_include_submit,
)
from mcp_remote_control.screen.buffer import (
    cursor_rc,
    dump_frame,
    find_text,
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

_actions_have_submit = actions_include_submit

# Default WaitSpec settle windows.
DEFAULT_IDLE_MS = 200
DEFAULT_TIMEOUT_MS = 30_000
# Max direction keys for a single go(how=keys)
GO_STEP_CAP = 200

# nav= labels. "ok" means the requested realization was written (an SGR mouse
# report for a click; clicks=0 requests nothing). "keys" means direction keys
# were written - only ever for a caller that asked for keys (an explicit false
# click value, go how=keys, move); no action falls back to them implicitly, and
# a zero-step keys/move writes no byte so it carries no label. "no_tracking"
# means no byte was written: the peer never announced SGR mouse reporting, so a
# report could not be decoded and a synthesized key press would only corrupt its
# input line.
NAV_OK = "ok"
NAV_KEYS = "keys"
NAV_NO_TRACKING = "no_tracking"

# Actionable text for NAV_NO_TRACKING. Key steering is still reachable - the
# caller only has to ask for it explicitly instead of getting it as a fallback.
NO_TRACKING_HINT = (
    "peer did not announce SGR mouse reporting (DECSET 1006); nothing was "
    "written. Ask for key steering explicitly (go how=keys, or to_text "
    "click=false), or pass force_click if this peer does decode SGR reports"
)

# Spellings accepted for a boolean action field that selects between two
# realizations. Everything outside both sets is an argument error, never a
# silent "no": action_truthy reports False for a string it does not recognise,
# and reading that as "not a click" would select the key-steering branch for a
# caller who asked for a click.
_TRUE_SPELLINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_SPELLINGS: frozenset[str] = frozenset({"0", "false", "no", "off"})

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

# DECSET private mode an application sets to announce that it parses xterm
# bracketed-paste delimiters (ESC[200~ ... ESC[201~). pyte records private modes
# shifted left 5 bits (see screen.buffer), so both spellings are checked.
PASTE_BRACKET_DECSET = 2004

# DECSET private mode an application sets to announce that it decodes xterm
# SGR mouse reports (ESC[<button;col;rowM / m), the only mouse encoding this
# module writes. The legacy tracking modes (1000/1002/1003) carry a different
# report layout and 1005/1015 carry their own extended encodings, so none of
# them say the SGR form is understood. Same <<5 encoding as above.
SGR_MOUSE_DECSET = 1006


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
    # NAV_* label of the last action that steered a pointer/cursor. One send
    # carries one label: a send mixing realizations (a delivered click followed
    # by a key-steering action) reports the last one, so the label is not a
    # per-action summary - ``did`` is.
    nav: str | None = None
    pre_hash: str | None = None
    error_code: str | None = None
    error_msg: str | None = None
    shot: bool = True
    cwd: str | None = None


def normalize_actions(actions: Any) -> list[dict[str, Any]]:
    """Coerce *actions* to a list of dicts; empty/None -> []."""
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


def bracketed_paste_enabled(screen: Any) -> bool:
    """True when the receiving program announced bracketed-paste support.

    DECSET 2004 is opt-in: only a program that sent ``ESC[?2004h`` parses the
    ``ESC[200~`` / ``ESC[201~`` delimiters. A line editor without it consumes
    the escape prefix as an unknown CSI sequence and inserts the remaining
    parameter text literally, so the payload is silently corrupted while the
    send still reports ok. Mirrors ``mouse_tracking_enabled``: the live pyte
    mode set is the signal.
    """
    modes = getattr(screen, "mode", None)
    if not modes:
        return False
    return PASTE_BRACKET_DECSET in modes or (PASTE_BRACKET_DECSET << 5) in modes


def resolve_bracketed_paste(
    session: ScreenSession,
    action: Mapping[str, Any],
) -> bool:
    """Decide whether one paste action wraps its payload for this session.

    Rule: an explicit ``bracketed`` field always wins - it is the caller's
    escape hatch for a peer whose state the buffer cannot show. Otherwise the
    decision follows the receiving program's own declaration (DECSET 2004, see
    ``bracketed_paste_enabled``). With no signal the payload is sent unwrapped:
    a wrapper the peer does not parse silently corrupts the input, whereas an
    unwrapped paste is delivered as literal keystrokes - the lesser evil, and
    the only form that cannot swallow the payload.
    """
    if "bracketed" in action:
        return action_truthy(action.get("bracketed"))
    return bracketed_paste_enabled(session.screen)


def _explicit_bool(value: Any, *, field: str, default: bool, hint: str) -> bool:
    """Read an optional boolean action field strictly; absent means *default*.

    ``action_truthy`` (``screen.actions``) maps every spelling outside its
    truthy set to False. That is the wrong reader for a field whose two
    branches are not equally safe: a caller who wrote ``force_click="always"``
    or ``click="auto"`` asked for the positive branch and would silently be
    given the negative one - refused without being told the value was not
    understood, or steered with keys they did not ask for. An unrecognised
    value is reported as a bad argument instead. Only literal booleans, numbers
    and the spellings in ``_TRUE_SPELLINGS`` / ``_FALSE_SPELLINGS`` are read.
    """
    if value is None:
        return default
    if value is True or value is False:
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    spelling = str(value).strip().lower()
    if spelling in _TRUE_SPELLINGS or spelling in _FALSE_SPELLINGS:
        return spelling in _TRUE_SPELLINGS
    raise ActionError("INVALID_ARG", f"{field} must be a boolean, got {value!r}; {hint}")


def _explicit_click(value: Any) -> bool:
    """Read ``to_text.click`` as a strict boolean; absent means click.

    ``click`` selects between a pointer report and direction keys, and the
    direction keys are the line editor's own commands - the up-key recalls a
    history entry, so the caller's next text lands inside the recalled line and
    the shell runs the splice. An unrecognised value must therefore be reported
    as a bad argument instead of being read as "no": ``action_truthy`` returns
    False for any string outside its truthy set, which would silently pick the
    key branch for a caller who asked for a click. Only a literal false
    spelling opts into key steering.
    """
    return _explicit_bool(
        value,
        field="to_text.click",
        default=True,
        hint=(
            "pass click=false to steer with direction keys, or omit click to "
            "click the cell"
        ),
    )


def apply_action(session: ScreenSession, action: Mapping[str, Any]) -> str:
    """Apply one action; return short type name for did= list.

    May raise ActionError / KeyEncodeError. Navigation labelling is dropped
    here; the send pipeline uses :func:`_apply_action_nav` to keep it.
    """
    name, _nav = _apply_action_nav(session, action)
    return name


def _apply_action_nav(
    session: ScreenSession,
    action: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Apply one action; return ``(did name, nav label)``.

    The nav label is None for actions that do not steer a cursor/pointer.
    """
    atype = str(action.get("type") or action.get("op") or "").strip().lower()
    if not atype:
        raise ActionError("INVALID_ARG", "action missing type")

    if atype == "nop":
        return "nop", None

    if atype == "text":
        text = action.get("text")
        if text is None:
            raise ActionError("INVALID_ARG", "text action requires text=")
        # Literal text goes out in the codec this session resolved for its own
        # reads: a cp936 console reads gbk, so utf-8 bytes would reach it as
        # mojibake while the send still reports ok. Named keys, raw and mouse
        # bytes below stay codec-independent; a key action whose base is a
        # single character is typed text and follows the same codec.
        session.write(encode_text(str(text), codec=session.text_codec))
        if action_truthy(action.get("submit")):
            session.write(encode_key("enter"))
            return "text+submit", None
        return "text", None

    if atype == "key":
        key = action.get("key")
        if key is None:
            raise ActionError("INVALID_ARG", "key action requires key=")
        session.write(encode_key(str(key), codec=session.text_codec))
        return "key", None

    if atype == "keys":
        keys = action.get("keys")
        if keys is None and action.get("key") is not None:
            keys = [action["key"]]
        if not isinstance(keys, list) or not keys:
            raise ActionError("INVALID_ARG", "keys action requires keys=[...]")
        session.write(encode_keys((str(k) for k in keys), codec=session.text_codec))
        return "keys", None

    if atype == "paste":
        text = action.get("text")
        if text is None:
            raise ActionError("INVALID_ARG", "paste action requires text=")
        bracketed = resolve_bracketed_paste(session, action)
        session.write(
            encode_paste(
                str(text),
                bracketed=bracketed,
                codec=session.text_codec,
            )
        )
        if action_truthy(action.get("submit")):
            session.write(encode_key("enter"))
            return "paste+submit", None
        return "paste", None

    if atype == "raw":
        hex_str = action.get("hex") or action.get("data")
        if hex_str is None:
            raise ActionError("INVALID_ARG", "raw action requires hex=")
        session.write(encode_raw_hex(str(hex_str)))
        return "raw", None

    if atype == "submit":
        session.write(encode_key("enter"))
        return "submit", None

    if atype == "interrupt":
        session.write(encode_key("ctrl+c"))
        return "interrupt", None

    if atype == "eof":
        session.write(encode_key("ctrl+d"))
        return "eof", None

    if atype == "escape":
        session.write(encode_key("escape"))
        return "escape", None

    if atype == "clear_line":
        session.write(encode_key("ctrl+u"))
        return "clear_line", None

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
        return "wait", None

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
        return "resize", None

    if atype == "go":
        return _action_go(session, action)

    if atype == "click":
        return _action_click(session, action)

    if atype == "move":
        return _action_move(session, action)

    if atype == "to_text":
        return _action_to_text(session, action)

    raise ActionError("INVALID_ARG", f"unknown action type: {atype!r}")


def _action_go(
    session: ScreenSession,
    action: Mapping[str, Any],
) -> tuple[str, str | None]:
    if "row" not in action or "col" not in action:
        raise ActionError("INVALID_ARG", "go requires row= and col=")
    try:
        target_r = int(action["row"])
        target_c = int(action["col"])
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "go row/col must be ints") from exc

    how = str(action.get("how") or "auto").lower()
    if how == "auto":
        # auto may only pick the pointer report. There is no implicit fallback
        # to direction keys: on a line-editing shell the up-key recalls a
        # history entry, so the caller's next text would land inside the
        # recalled line and the shell would run the splice - trading a refused
        # click for a corrupted command on the *next* send, which carries no
        # nav signal at all. Without a decodable report this falls through to
        # _action_click's refusal, whose hint names go how=keys as the opt-in.
        how = "click"

    if how == "click":
        _action_click(
            session,
            {
                "row": target_r,
                "col": target_c,
                "button": action.get("button", "left"),
                "mods": action.get("mods"),
                "clicks": action.get("clicks", 1),
                "force_click": action.get("force_click"),
            },
        )
        return "go", NAV_OK
    if how == "keys":
        # Label only what was written: a target the cursor already sits on
        # needs no steps, and nav=keys would claim the cursor moved.
        steered = _go_by_keys(session, target_r, target_c)
        return "go", NAV_KEYS if steered else None
    raise ActionError("INVALID_ARG", f"go.how unsupported: {how!r}")


def _go_by_keys(session: ScreenSession, target_r: int, target_c: int) -> bool:
    """Write the direction keys from the cursor to the target cell.

    Returns whether any byte was written: zero steps means the cursor was
    already there, so the caller must not report the cursors as steered.
    """
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
    if not seq:
        return False
    session.write(encode_keys(seq))
    return True


def sgr_mouse_enabled(screen: Any) -> bool:
    """True when the peer announced the SGR mouse report form (DECSET 1006).

    Narrower than ``buffer.mouse_tracking_enabled`` on purpose: that predicate
    answers "does this peer read mouse reports at all" (surface detection),
    while this one answers "does it read the encoding this module writes".
    """
    modes = getattr(screen, "mode", None)
    if not modes:
        return False
    return SGR_MOUSE_DECSET in modes or (SGR_MOUSE_DECSET << 5) in modes


def mouse_click_enabled(session: ScreenSession, action: Mapping[str, Any]) -> bool:
    """Whether SGR mouse bytes may be written for this click action.

    This module writes only the SGR form (``ESC[<b;x;yM``), so the signal is
    the mode that announces it, DECSET 1006. Any other tracking mode
    (1000/1002/1003 legacy, 1005 UTF-8, 1015 urxvt) means the peer reads *some*
    mouse report, not this one: those bytes are consumed as an unknown CSI
    prefix and the remaining parameters are inserted as literal text, which
    corrupts the next submitted command. A click that cannot be delivered this
    way is refused (``NAV_NO_TRACKING``) rather than translated into direction
    keys - those are the editor's own commands, just a different corruption.
    ``force_click`` is the caller's escape hatch for a peer whose modes the
    buffer cannot show; an unrecognised spelling of it is refused rather than
    read as "not set", which would refuse a caller who did ask to force.
    """
    if _explicit_bool(
        action.get("force_click"),
        field="click.force_click",
        default=False,
        hint=(
            "pass force_click=true to write the SGR report on a peer that "
            "never announced DECSET 1006, or omit it"
        ),
    ):
        return True
    return sgr_mouse_enabled(session.screen)


def _action_click(
    session: ScreenSession,
    action: Mapping[str, Any],
) -> tuple[str, str]:
    if "row" not in action or "col" not in action:
        raise ActionError("INVALID_ARG", "click requires row= and col=")
    try:
        row = int(action["row"])
        col = int(action["col"])
    except (TypeError, ValueError) as exc:
        raise ActionError("INVALID_ARG", "click row/col must be ints") from exc
    # *clicks* is the number of press+release pairs. Missing -> 1; explicit 0
    # must emit nothing (do not treat 0 as falsy). Negatives clamp to 0.
    raw_clicks = action.get("clicks", 1)
    try:
        clicks = max(0, int(raw_clicks)) if raw_clicks is not None else 1
    except (TypeError, ValueError) as exc:
        raise ActionError(
            "INVALID_ARG", f"click clicks must be int: {raw_clicks!r}"
        ) from exc
    if clicks == 0:
        return "click", NAV_OK
    if not mouse_click_enabled(session, action):
        # No tracking: neither realization is safe. An SGR report is not
        # ignored - a line editor strips the ``ESC[<`` introducer and types the
        # remaining parameters as literal text. Direction keys are no better:
        # on a line-editing shell the up-key recalls a history entry, so the
        # caller's next text lands inside the recalled line and the shell runs
        # the splice. Write nothing and say so; key steering stays reachable
        # through go(how=keys) / to_text(click=false), which label it nav=keys.
        raise ActionError("NAV_NO_TRACKING", NO_TRACKING_HINT)
    button = str(action.get("button") or "left")
    mods = action.get("mods")
    mod_list = list(mods) if isinstance(mods, list) else None
    # First press+release pair (clicks=0 already returned above).
    session.write(
        encode_mouse_sgr(row, col, button=button, press=True, mods=mod_list)
    )
    session.write(
        encode_mouse_sgr(row, col, button=button, press=False, mods=mod_list)
    )
    # Additional pairs so total press+release count equals *clicks*.
    for _ in range(clicks - 1):
        session.write(
            encode_mouse_sgr(row, col, button=button, press=True, mods=mod_list)
        )
        session.write(
            encode_mouse_sgr(row, col, button=button, press=False, mods=mod_list)
        )
    return "click", NAV_OK


def _action_move(
    session: ScreenSession, action: Mapping[str, Any]
) -> tuple[str, str | None]:
    try:
        rd = int(action.get("row_delta") or 0)
        cd = int(action.get("col_delta") or 0)
    except (TypeError, ValueError) as exc:
        # Same reader as ``click``/``go``: a delta the caller misspelled is an
        # argument error, not an execution failure. Letting ``int`` escape
        # would surface it as EXEC_FAILED from the action loop's catch-all,
        # pointing the caller at the session instead of at the action.
        raise ActionError(
            "INVALID_ARG", "move row_delta/col_delta must be ints"
        ) from exc
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
    if not seq:
        # Zero delta: the caller asked for no movement, so nothing was written.
        # NAV_KEYS would claim the cursor was steered, which an Agent branches
        # on; no label is the honest answer.
        return "move", None
    session.write(encode_keys(seq))
    # Explicit direction keys, never a pointer report: label the realization.
    return "move", NAV_KEYS


def _action_to_text(
    session: ScreenSession,
    action: Mapping[str, Any],
) -> tuple[str, str | None]:
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
    do_click = _explicit_click(action.get("click"))
    if do_click:
        # click defaults true, so this is also the implicit path. It stays a
        # click: ``_action_click`` writes SGR only when the peer announced
        # tracking and otherwise refuses with NAV_NO_TRACKING instead of
        # silently synthesizing direction keys that would recall a history
        # entry and splice the caller's next command into it.
        _action_click(
            session,
            {
                "row": target_r,
                "col": target_c,
                "button": action.get("button", "left"),
                "mods": action.get("mods"),
                "clicks": action.get("clicks", 1),
                "force_click": action.get("force_click"),
            },
        )
        return "to_text", NAV_OK
    # An explicit false click value is a request to steer with direction keys.
    # On a shell those are the line editor's own commands, so the caller opted
    # in and the result is labelled nav=keys - unless the found cell is already
    # under the cursor, in which case no key was written and no label is due.
    # Must not swallow ActionError (e.g. NAV_CAPPED): caller reports nav failure
    # instead of silent nav=ok.
    steered = _go_by_keys(session, target_r, target_c)
    return "to_text", NAV_KEYS if steered else None


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


def _pty_alive(session: ScreenSession) -> bool:
    """True when the session is open and the PTY channel is still live."""
    try:
        return session.is_alive()
    except Exception:  # noqa: BLE001
        return False


def _drain_for_duration(session: ScreenSession, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pty_alive(session):
            return
        remaining = deadline - time.monotonic()
        session.drain(min(0.05, max(0.0, remaining)))


def _wait_idle(
    session: ScreenSession,
    *,
    idle_s: float,
    timeout_s: float,
    min_s: float,
) -> None:
    """Settle for *idle_s* of silence inside one *timeout_s* deadline.

    The settle floor and every drain slice are shares of that single deadline:
    a min_ms above the timeout may not hold the caller past it, and no drain
    may run beyond it. On timeout the current frame is returned as it stands.
    """
    start = time.monotonic()
    deadline = start + timeout_s

    if min_s > 0:
        # The floor comes out of the budget, it is not a wait of its own.
        _drain_for_duration(session, min(min_s, timeout_s))

    # No idle required -> just honor remaining timeout as a settle, or instant.
    if idle_s <= 0:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            session.drain(min(0.05, remaining))
        return

    last_data_at = time.monotonic()
    while True:
        if not _pty_alive(session):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Timed out waiting for idle - still ok; return the current frame.
            return
        got = session.drain(min(0.05, remaining))
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
        if not _pty_alive(session):
            return
        session.drain(0.05)
        if not _pty_alive(session):
            return
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
    """Full pipeline: actions -> wait -> cwd probe -> drain -> optional shot.

    Holds ``session.serial_ops()`` for the entire pipeline so concurrent
    send/close on the same session cannot interleave PTY writes or pyte
    updates. Nested write/drain/feed/shot re-enter the RLock safely.
    """
    # Serialize the whole action/wait/drain/shot critical section. Do not
    # acquire again inside apply_action via a separate non-reentrant lock.
    with session.serial_ops():
        return _execute_send_locked(
            session,
            actions=actions,
            wait=wait,
            shot=shot,
            probe_cwd=probe_cwd,
        )


def _execute_send_locked(
    session: ScreenSession,
    *,
    actions: Any = None,
    wait: Any = None,
    shot: bool = True,
    probe_cwd: bool = True,
) -> SendOutcome:
    """Send pipeline body; caller must hold ``session.serial_ops()``."""
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
            name, act_nav = _apply_action_nav(session, act)
            did.append(name)
            if act_nav is not None:
                nav = act_nav
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
            elif exc.code == "NAV_NO_TRACKING":
                nav = NAV_NO_TRACKING
            elif "nav" in (exc.code or "").lower():
                nav = "fail"
            break
        except Exception as exc:  # noqa: BLE001
            err_code = "EXEC_FAILED"
            err_msg = f"action_{i}_failed: {type(exc).__name__}: {exc}"
            break

    # Action-loop errors are terminal: do not run the full top-level wait
    # (default until=idle timeout_ms=DEFAULT_TIMEOUT_MS) or inject a cwd probe.
    # Wait-only / success paths keep wait_after + probe unchanged.
    wait_until: str | None = None
    if err_code is None:
        try:
            wait_until = wait_after(session, wait_spec)
        except ActionError as exc:
            err_code = exc.code
            err_msg = exc.msg

        # Silent cwd probe on shell surfaces. Skip empty / nop-only sends so a pure
        # re-shot can return status=unchanged without the probe rewriting the hash.
        # Probe only when the last landed action is submit/enter: a mid-list
        # submit followed by typed text still leaves uncommitted input, and
        # silent_pwd_probe always starts with ctrl+u.
        # After a trailing submit/enter, probe still refreshes session.cwd.
        # ``update_cwd_after_send`` / ``_should_probe`` also refuse inject when the
        # buffer shows password/confirm prompts or a TUI surface - ctrl+u
        # must not wipe secrets or feed the probe into a non-shell program.
        if (
            probe_cwd
            and not _is_noop_actions(acts)
            and actions_include_submit(acts)
        ):
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
    # Empty / nop-only send with stable hash -> unchanged, omit body frame.
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


def encode_actions_bytes(
    actions: list[Mapping[str, Any]],
    *,
    codec: str | None = None,
) -> bytes:
    """Encode a list of actions to concatenated PTY bytes (no session I/O).

    Session-dependent actions (go/click/move/to_text/resize/wait/nop) are skipped.
    No session means no terminal-state signal, so a ``paste`` without an
    explicit ``bracketed`` field encodes unwrapped - see
    ``resolve_bracketed_paste`` for why the wrapped form is never assumed - and
    literal content is encoded in *codec* when the caller knows the peer's
    codec (``ScreenSession.text_codec``), utf-8 otherwise. Literal content is
    the text/paste payload plus any key whose base is a single character; named
    keys and hex payloads are protocol/control bytes and never follow the codec.
    """
    buf = bytearray()
    for act in actions:
        atype = str(act.get("type") or "").strip().lower()
        if atype == "text":
            buf.extend(encode_text(str(act.get("text") or ""), codec=codec))
            if action_truthy(act.get("submit")):
                buf.extend(encode_key("enter"))
        elif atype == "key":
            buf.extend(encode_key(str(act["key"]), codec=codec))
        elif atype == "keys":
            buf.extend(
                encode_keys(
                    (str(k) for k in act.get("keys") or []),
                    codec=codec,
                )
            )
        elif atype == "paste":
            bracketed = False
            if "bracketed" in act:
                bracketed = action_truthy(act.get("bracketed"))
            buf.extend(
                encode_paste(
                    str(act.get("text") or ""),
                    bracketed=bracketed,
                    codec=codec,
                )
            )
            if action_truthy(act.get("submit")):
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
