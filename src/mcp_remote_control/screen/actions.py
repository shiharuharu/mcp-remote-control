"""Screen action helpers: submit/enter scanning and truthy flags.

Owner of the enter/submit scan used after send and by the silent cwd probe.
Not a general-purpose utils module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def action_truthy(val: Any) -> bool:
    if val is True or val is False:
        return bool(val)
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


# Keys that commit the current shell input line (same as submit / enter).
# Restricted to spellings ``encode_key`` accepts: the key text is stripped
# before the lookup, so "\r" / "\n" fold to "" and can never match, and
# "kp_enter" / "c-m" / "ctrl-m" raise KeyEncodeError instead of landing input.
_SUBMIT_KEY_NAMES: frozenset[str] = frozenset(
    {
        "enter",
        "return",
        "ctrl+m",
    }
)

# Action types that write no PTY input. Trailing nop / wait / resize do not
# land input when scanning for the last action that touched the shell line, and
# a send made only of these needs no cwd probe (the probe injects ctrl+u).
_NON_INPUT_TYPES: frozenset[str] = frozenset({"nop", "wait", "resize"})


def _action_type(act: Mapping[str, Any]) -> str:
    return str(act.get("type") or act.get("op") or "").strip().lower()


def _is_noop_actions(actions: Sequence[Mapping[str, Any]] | None) -> bool:
    """True when actions are empty or only nop/wait/resize (pure re-shot / poll)."""
    if not actions:
        return True
    for act in actions:
        atype = _action_type(act)
        if atype and atype not in _NON_INPUT_TYPES:
            return False
    return True


def _is_submit_action(act: Mapping[str, Any]) -> bool:
    """True when this single action commits the current shell line."""
    atype = _action_type(act)
    if atype == "submit":
        return True
    if atype in ("text", "paste") and action_truthy(act.get("submit")):
        return True
    if atype == "key":
        key = str(act.get("key") or "").strip().lower()
        return key in _SUBMIT_KEY_NAMES
    if atype == "keys":
        keys = list(act.get("keys") or [])
        if not keys:
            return False
        # Last key in the chord is the last landed input of this action.
        return str(keys[-1]).strip().lower() in _SUBMIT_KEY_NAMES
    return False


def actions_include_submit(
    actions: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """True when the last landed action commits a shell line (safe to probe).

    A silent probe always injects ctrl+u first. That is only safe when the
    last action that wrote input is submit / enter (or text|paste with
    submit). A mid-list submit followed by more typed text leaves
    uncommitted input that must not be wiped. Trailing nop / wait / resize
    are not input and do not cancel a preceding commit.
    """
    if not actions:
        return False
    for act in reversed(actions):
        if _action_type(act) in _NON_INPUT_TYPES:
            continue
        return _is_submit_action(act)
    return False
