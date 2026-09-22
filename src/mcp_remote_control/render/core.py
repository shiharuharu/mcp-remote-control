"""Unified dual-track renderer: Agent semantic text and compact JSON.

Agent track layout: status header, optional meta lines, optional body, optional
``@hint``. Sensitive keys and secret-like substrings are redacted before emit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp_remote_control.render.redact import (
    REDACTED,
    is_sensitive_key,
    redact_mapping,
    redact_optional_str,
    redact_string,
)

# Fixed status vocabulary for Agent and JSON tracks.
VALID_STATUSES: frozenset[str] = frozenset(
    {"ok", "fail", "timeout", "dead", "unchanged", "error"}
)

# Kind tokens this renderer formats (tool kinds plus generic error).
VALID_KINDS: frozenset[str] = frozenset(
    {"endpoint", "exec", "fs", "screen", "ps", "error"}
)

# Fields rendered on ``| meta`` lines rather than the status header.
_META_KEYS: frozenset[str] = frozenset(
    {
        "cursor_line",
        "title",
        "label",
        "changed_lines",
        "encoding",
        "note",
        "resolved_from",
        "redacted",
        # Free-text values whose spaces carry meaning: prose messages, prose
        # failure detail, and documented machine tokens the caller matches
        # literally. Header tokens are space-free, so folding one here would
        # hand back a string the caller never wrote and cannot match.
        "msg",
        "message",
        "warning",
        "reopen_hint",
        "pump_error",
        "close_error",
        # Adaptive geometry open meta (grouped or omitted below).
        "fit",
        "steps",
        "seed",
        "class",
        "cmd",
        # Endpoint probe summary (collapsed onto one meta line).
        "shell",
        "uname",
        "locale",
        "dialect",
        "busybox",
        "cwd_src",
        # WinRM PS Agent tokens (same collapse line as shell/dialect).
        "ps_version",
        "lang_mode",
        "ps_fs",
        "ps_edition",
        "ps_probe",
    }
)

# Path-valued fields. A spaced path spelled with underscores names a different
# file, so a whitespace-bearing value moves to the meta line where spaces
# survive; a space-free value stays a header token (same line, no ``|`` line).
# ``cwd`` shares this rule but arrives as its own argument, not a field.
# ``cwd_arg`` is the caller-supplied cwd echoed back on the INVALID_CWD error
# path (exec_ops); a caller retries from it, so folding it would aim the retry
# at a directory the caller never named.
_PATH_FIELD_KEYS: tuple[str, ...] = ("path", "local", "target", "cwd_arg")

# Meta keys that keep spaces verbatim (only CR/LF are flattened).
_PATH_META_KEYS: frozenset[str] = frozenset((*_PATH_FIELD_KEYS, "cwd"))

# Geometry keys merged into ``| geom=...`` or omitted when trivial.
_GEOM_META_KEYS: tuple[str, ...] = ("fit", "steps", "seed", "class", "cmd")

# Probe summary keys merged into one ``| shell=... dialect=... ps_*=...`` line.
_PROBE_META_KEYS: tuple[str, ...] = (
    "shell",
    "dialect",
    "busybox",
    "uname",
    "locale",
    "ps_version",
    "lang_mode",
    "ps_fs",
    "ps_edition",
    "ps_probe",
)

# Preferred header token order; remaining keys follow alphabetically.
_HEADER_ORDER: tuple[str, ...] = (
    "id",
    "ep",
    "transport",
    "host",
    "path",
    "type",
    "exit",
    "ms",
    "bytes",
    "lines",
    "n",
    "wrote",
    "gen",
    "hash",
    "surface",
    "mode",
    "sha256",
    "caps",
    "op",
    "partial",
    "truncated",
    "overwritten",
    "recursive",
    "next_offset",
    "screens_closed",
    "disconnected",
    "alive",
    "idle",
    "busy",
    "alt",
    "empty",
    "via",
)

# Boolean-ish flags render as bare tokens when true (``idle``, ``busy``, ...).
# Single source of truth: the tuple fixes emission order on the status line
# and the derived set drives membership checks. Add a flag only here so both
# stay in sync (a set-only entry would be silently dropped from Agent output).
_FLAG_ORDER: tuple[str, ...] = (
    "idle",
    "busy",
    "alive",
    "dead",
    "alt",
    "partial",
    "truncated",
    "empty",
    "overwritten",
    "recursive",
    "disconnected",
)
_FLAG_KEYS: frozenset[str] = frozenset(_FLAG_ORDER)


def _format_scalar(value: Any) -> str:
    """Format a field value for Agent-track header k=v tokens (space-free)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, (list, tuple)):
        return ",".join(_format_scalar(v) for v in value)
    s = str(value)
    # Header tokens must be space-free so the status line stays one token stream.
    # Free-text msg/message is routed to the meta line (spaces preserved there).
    # cwd/path with whitespace are also routed to | meta (not rewritten here).
    if " " in s or "\n" in s or "\t" in s:
        s = " ".join(s.split())
        s = s.replace(" ", "_")
    return s


def _format_meta_value(value: Any) -> str:
    """Format a meta-line value: collapse whitespace, keep spaces readable."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, (list, tuple)):
        return ",".join(_format_meta_value(v) for v in value)
    return " ".join(str(value).split())


def _header_would_rewrite(value: Any) -> bool:
    """True when ``_format_scalar`` would collapse or underscore the value."""
    s = str(value)
    return " " in s or "\n" in s or "\t" in s


def _format_path_meta(value: Any) -> str:
    """Emit cwd/path on a meta line: keep spaces, flatten CR/LF only."""
    if value is None:
        return ""
    s = str(value)
    if "\n" in s or "\r" in s:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = " ".join(s.split("\n"))
    return s


def _route_spaced_paths(
    header: dict[str, Any],
    meta: dict[str, Any],
    cwd: str | None,
) -> str | None:
    """Move whitespace-bearing path values onto | meta so they stay verbatim.

    Header tokens are space-free, and ``_format_scalar`` makes them so by
    underscoring whitespace - which invents a filesystem path that does not
    exist. Each entry of ``_PATH_FIELD_KEYS`` is routed only when the value
    would be rewritten, so the common space-free case keeps costing one token.
    """
    for key in _PATH_FIELD_KEYS:
        val = header.get(key)
        if val is not None and _header_would_rewrite(val):
            meta.setdefault(key, header.pop(key))
    if cwd is not None and _header_would_rewrite(cwd):
        meta.setdefault("cwd", cwd)
        return None
    return cwd


def _format_cur(cur: Any) -> str | None:
    """Normalize cursor to `r,c` string."""
    if cur is None:
        return None
    if isinstance(cur, str):
        return cur.replace(" ", "")
    if isinstance(cur, (list, tuple)) and len(cur) >= 2:
        return f"{cur[0]},{cur[1]}"
    if isinstance(cur, Mapping) and "r" in cur and "c" in cur:
        return f"{cur['r']},{cur['c']}"
    return str(cur)


def _format_size(fields: Mapping[str, Any]) -> str | None:
    """Render terminal geometry as `colsxrows` when present."""
    if "size" in fields and fields["size"] is not None:
        return str(fields["size"]).replace(" ", "")
    cols = fields.get("cols")
    rows = fields.get("rows")
    if cols is not None and rows is not None:
        return f"{cols}x{rows}"
    return None


def _split_fields(
    fields: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Partition into header fields vs meta fields."""
    header: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    if not fields:
        return header, meta
    for k, v in fields.items():
        if v is None:
            continue
        if k in _META_KEYS:
            meta[k] = v
        else:
            header[k] = v
    return header, meta


def _dedupe_screen_id(header: dict[str, Any]) -> None:
    """Prefer single primary ``id=`` when ``screen_id`` is an identical alias."""
    sid = header.get("screen_id")
    primary = header.get("id")
    if sid is not None and primary is not None and str(sid) == str(primary):
        header.pop("screen_id", None)


def _collapse_geometry_meta(meta: dict[str, Any]) -> None:
    """Omit trivial geometry meta, else merge into one ``| geom=...`` line.

    When ``fit=ok`` and ``steps`` is 0/None, drop fit/steps/seed/class
    (common healthy shell open) but keep a non-empty ``cmd`` so agents see
    the opened command. Non-trivial cases become::

        | geom=ok steps=1 seed=100x30 class=tui
    """
    if not any(k in meta for k in _GEOM_META_KEYS):
        return

    fit = meta.pop("fit", None)
    steps = meta.pop("steps", None)
    seed = meta.pop("seed", None)
    cmd_class = meta.pop("class", None)
    cmd = meta.pop("cmd", None)

    steps_n: int | None
    try:
        steps_n = int(steps) if steps is not None and steps != "" else None
    except (TypeError, ValueError):
        steps_n = None

    fit_s = str(fit).strip().lower() if fit is not None else ""
    trivial_ok = fit_s in ("ok", "") and (steps_n is None or steps_n == 0)
    if trivial_ok and fit_s == "ok":
        # Healthy open: seed/class are noise for agents; keep cmd visible.
        if cmd is not None and str(cmd) != "":
            meta["geom"] = f"cmd={_format_meta_value(cmd)}"
        return
    if trivial_ok and fit_s == "" and steps_n in (None, 0):
        if seed is None and cmd_class is None and cmd is None:
            return

    parts: list[str] = []
    if fit is not None and str(fit) != "":
        parts.append(str(fit).strip())
    if steps_n is not None and steps_n != 0:
        parts.append(f"steps={steps_n}")
    elif steps is not None and steps_n is None and str(steps) not in ("", "0"):
        parts.append(f"steps={_format_meta_value(steps)}")
    if seed is not None and str(seed) != "":
        parts.append(f"seed={_format_meta_value(seed)}")
    if cmd_class is not None and str(cmd_class) != "":
        parts.append(f"class={_format_meta_value(cmd_class)}")
    if cmd is not None and str(cmd) != "":
        parts.append(f"cmd={_format_meta_value(cmd)}")
    if parts:
        meta["geom"] = " ".join(parts)


def _collapse_probe_meta(meta: dict[str, Any]) -> list[str]:
    """Pull shell/uname/locale/ps_* into one multi-token meta line (or empty)."""
    tokens: list[str] = []
    for key in _PROBE_META_KEYS:
        if key not in meta:
            continue
        val = meta.pop(key)
        if val is None or str(val).strip() == "":
            continue
        tokens.append(f"{key}={_format_meta_value(val)}")
    return tokens


def _ordered_header_items(header: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Stable, readable order for status-line tokens."""
    skip = {
        "cols",
        "rows",
        "size",
        "cur",
        "cwd",
        "code",
        "op",  # fs op is special-cased before status
        "body",
        "hint",
        # Nested / bulky fields: JSON track only.
        "ports",
        "profile",
    }

    items: list[tuple[str, Any]] = []
    seen: set[str] = set()

    for key in _HEADER_ORDER:
        if key in skip or key not in header:
            continue
        if key in _FLAG_KEYS:
            continue
        items.append((key, header[key]))
        seen.add(key)

    for key in sorted(header.keys()):
        if key in skip or key in seen or key in _FLAG_KEYS:
            continue
        items.append((key, header[key]))
        seen.add(key)

    # Flags at the end as bare tokens when truthy; order from _FLAG_ORDER.
    for key in _FLAG_ORDER:
        if key not in header:
            continue
        val = header[key]
        if val is True or val == 1 or val == "1":
            items.append((key, True))
        elif val is False or val == 0 or val == "0":
            continue
        else:
            items.append((key, val))

    return items


def _build_status_tokens(
    kind: str,
    status: str,
    *,
    cwd: str | None,
    code: str | None,
    header: Mapping[str, Any],
) -> list[str]:
    tokens: list[str] = [f"@{kind}"]

    # fs embeds op between kind and status: `@fs put ok ...`
    op = header.get("op")
    if kind == "fs" and op is not None:
        tokens.append(_format_scalar(op))

    tokens.append(status)

    # error code immediately after status when present
    emitted_code = False
    if code is not None:
        tokens.append(f"code={_format_scalar(code)}")
        emitted_code = True
    elif status == "error" and header.get("code") is not None:
        tokens.append(f"code={_format_scalar(header['code'])}")
        emitted_code = True

    # Geometry for screen: `120x40`
    size = _format_size(header)
    if size is not None:
        tokens.append(size)

    # Cursor: `cur=r,c`
    cur = _format_cur(header.get("cur"))
    if cur is not None:
        tokens.append(f"cur={cur}")

    for key, val in _ordered_header_items(header):
        if key == "code" and emitted_code:
            continue
        if val is True:
            tokens.append(key)
        else:
            if is_sensitive_key(key):
                tokens.append(f"{key}={REDACTED}")
            else:
                tokens.append(f"{key}={_format_scalar(val)}")

    if cwd is not None:
        tokens.append(f"cwd={_format_scalar(cwd)}")

    return tokens


def _redact_inputs(
    cwd: str | None,
    code: str | None,
    fields: dict[str, Any] | None,
    body: str | None,
    hint: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None, str | None, str | None]:
    safe_fields = redact_mapping(fields) if fields else None
    safe_cwd = redact_optional_str("cwd", cwd)
    safe_code = redact_optional_str("code", code)
    safe_body = redact_optional_str(None, body)
    safe_hint = redact_optional_str(None, hint)
    return safe_cwd, safe_code, safe_fields, safe_body, safe_hint


def render_agent_text(
    kind: str,
    status: str,
    *,
    cwd: str | None = None,
    code: str | None = None,
    fields: dict[str, Any] | None = None,
    body: str | None = None,
    hint: str | None = None,
) -> str:
    """Render the Agent semantic text track.

    Layout::

        @<kind> <status> <tokens...>          # status header (required)
        | <meta>                            # optional meta lines
                                            # blank line only when body present
        <body>                              # optional body (stdout/frame/...)
        @hint <text>                        # optional trailing hint
    """
    kind = kind.strip().lower()
    status = status.strip().lower()

    cwd, code, fields, body, hint = _redact_inputs(cwd, code, fields, body, hint)

    header, meta = _split_fields(fields)
    cwd = _route_spaced_paths(header, meta, cwd)
    _dedupe_screen_id(header)
    _collapse_geometry_meta(meta)
    probe_tokens = _collapse_probe_meta(meta)

    if fields and any(is_sensitive_key(k) for k in fields):
        meta.setdefault("redacted", "secrets")

    tokens = _build_status_tokens(kind, status, cwd=cwd, code=code, header=header)
    head_lines: list[str] = [" ".join(tokens)]

    # Probe summary first (endpoint open): one skimmable meta line.
    if probe_tokens:
        head_lines.append("| " + " ".join(probe_tokens))

    for mk, mv in meta.items():
        if mv is None:
            continue
        if is_sensitive_key(mk):
            head_lines.append(f"| {mk}={REDACTED}")
            continue
        raw = redact_string(mv) if isinstance(mv, str) else mv
        # Path fields keep spaces (a rewritten path is a different path).
        # Other meta may contain spaces (msg, cursor_line, geom payload).
        text = (
            _format_path_meta(raw)
            if mk in _PATH_META_KEYS
            else _format_meta_value(raw)
        )
        head_lines.append(f"| {mk}={text}")

    parts: list[str] = ["\n".join(head_lines)]

    # Blank-line separator only when a body is present (empty body -> no gap).
    if body is not None and body != "":
        parts.append(body.rstrip("\n"))

    text_out = "\n\n".join(parts) if len(parts) > 1 else parts[0]

    if hint:
        text_out += f"\n@hint {' '.join(str(hint).split())}"
    return text_out


def render_json(
    kind: str,
    status: str,
    *,
    cwd: str | None = None,
    code: str | None = None,
    fields: dict[str, Any] | None = None,
    body: str | None = None,
    **extra: Any,
) -> str:
    """Render the machine track: compact JSON (no indent, omit nulls)."""
    kind = kind.strip().lower()
    status = status.strip().lower()

    cwd, code, fields, body, _ = _redact_inputs(cwd, code, fields, body, None)
    extra = redact_mapping(extra) if extra else {}

    payload: dict[str, Any] = {
        "kind": kind,
        "status": status,
    }
    if code is not None:
        payload["code"] = code
    if cwd is not None:
        payload["cwd"] = cwd

    if fields:
        for k, v in fields.items():
            if v is None:
                continue
            if k == "cur":
                payload["cur"] = _format_cur(v)
            elif k in ("cols", "rows") and "cols" in fields and "rows" in fields:
                # emit both; also size convenience
                payload[k] = v
            else:
                payload[k] = v
        if "cols" in fields and "rows" in fields and fields["cols"] is not None and fields["rows"] is not None:
            payload.setdefault("size", f"{fields['cols']}x{fields['rows']}")

    if body is not None:
        payload["body"] = body

    for k, v in extra.items():
        if v is not None and k not in payload:
            payload[k] = v

    payload = {k: v for k, v in payload.items() if v is not None}

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
