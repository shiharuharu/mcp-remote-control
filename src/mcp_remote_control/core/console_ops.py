"""Console Core ops (agent prefix ``console_``): list|open|send|views|close|sessions.

Model: buffered console session - send writes the link; views queries the
capture buffer. Background CapturePump always fills the buffer while the
session is open (independent of agent views/send cadence).
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from mcp_remote_control.codec.text_codec import encode_for_remote
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.serial.buffer import (
    BufLine,
    LineRingBuffer,
    resolve_text_codec,
    text_codec_known,
)
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.ports import list_serial_consoles
from mcp_remote_control.serial.registry import (
    DeviceBusyError,
    SerialSession,
    get_serial_registry,
)

# Observability-first default (~1e5 lines; fine on modern hosts).
DEFAULT_MAX_LINES = 99_999
DEFAULT_VIEW_TAIL = 100
# Hard cap on a single views body (token safety); the ring itself stays large.
DEFAULT_VIEWS_MAX_LINES = 2_000

VALID_OPS: frozenset[str] = frozenset(
    {"list", "open", "send", "views", "close", "sessions"}
)

# Device-name allowlist for console open. This is a name-shape check only
# (not a /dev scan - enumeration stays in ports.py). It prevents open() from
# attaching the capture pump to the controlling terminal (/dev/tty), a shared
# PTY (/dev/pts/N), or other non-serial char devices that answer termios and
# would let the pump exfiltrate or inject on the wrong byte stream.
#
# A ``stat.S_ISCHR`` check is intentionally not required: fake-serial test
# paths and Windows COM names need not resolve to a real char device on the
# host, and the name-shape guard already excludes the dangerous cases.
#
# Linux UART/USB-serial: ``/dev/tty`` + uppercase letter prefix + digits
# (``ttyS``, ``ttyUSB``, ``ttyACM``, ``ttyAMA``, ``ttyXRUSB``, ``ttyGS``, ...).
# Anchored form rejects ``/dev/tty`` (no prefix+digits), ``/dev/pts/3``,
# ``/dev/null``, bare ``ttyS0``, and ``/dev/ttyUSB`` (no digit). ``/`` is
# excluded from character classes so there is no path traversal.
#
# Windows COM: match after ``_normalize_serial_port_name`` so agents may pass
# ``com3`` / ``COM3`` / ``\\.\COM10`` equivalently (prefix stripped, COM
# uppercased). Linux/macOS paths are left unchanged.
_SERIAL_PORT_RE = re.compile(
    r"^(?:"
    r"/dev/tty[A-Z]+[0-9]+"  # Linux UART / USB-serial (any uppercase prefix)
    r"|/dev/cu\.[\w.-]+"  # macOS cu.* (USB / Bluetooth)
    r"|/dev/cua[0-9]+"  # legacy FreeBSD/Solaris callout
    r"|/dev/rfcomm[0-9]+"  # Linux Bluetooth SPP
    r"|COM[0-9]+"  # Windows (post-normalize)
    r")$"
)

# Windows extended device path prefix (``\\.\`` -> four chars: \ \ . \).
_WIN_DEVICE_PREFIX = "\\\\.\\"
_WIN_COM_BARE_RE = re.compile(r"^COM[0-9]+$", re.IGNORECASE)


def _normalize_serial_port_name(port: str) -> str:
    """Normalize Windows COM device names for allowlist check and open.

    ``\\.\\COMn`` -> ``COMn``; bare ``comN``/``ComN`` -> ``COMN``. Other paths
    (Linux ``/dev/tty*``, macOS ``/dev/cu.*``, ...) are returned unchanged.
    """
    p = port.strip()
    if p.startswith(_WIN_DEVICE_PREFIX):
        rest = p[len(_WIN_DEVICE_PREFIX) :]
        if _WIN_COM_BARE_RE.fullmatch(rest):
            return rest.upper()
        return p
    if _WIN_COM_BARE_RE.fullmatch(p):
        return p.upper()
    return p


def _is_serial_port_name(port: str) -> bool:
    return bool(_SERIAL_PORT_RE.match(_normalize_serial_port_name(port)))


def _brief_pump(sess: SerialSession) -> None:
    """Optional sync snarf of pending RX into the session buffer.

    Used after open's short settle and after views ``settle_ms``. Background
    CapturePump is the primary RX path; ``settle_ms`` hides most latency.
    This helper does one bounded ``read(65536)`` so bytes already sitting in
    the OS (or driver) buffer land before the caller snapshots views. Cost is
    one read per open/views that opts in; steady-state capture still relies
    on the pump.
    """
    try:
        more = sess.console.read(65536)
        if more:
            sess.buffer.feed(more)
    except Exception:  # noqa: BLE001
        pass


def _pump_fields(sess: SerialSession) -> dict[str, Any]:
    """Pump status for open/views/sessions - surface errors and link death.

    Persistent read failure or an externally closed link can leave the pump
    looking alive while no data flows. Surface ``pump_error`` / ``link_closed``
    so the agent can reopen instead of trusting a dead pump.
    """
    pump = sess.pump
    stats = pump.stats if pump else {}
    fields: dict[str, Any] = {
        "pump_running": 1 if stats.get("running") else 0,
    }
    if stats.get("error"):
        fields["pump_error"] = stats["error"]
    if stats.get("link_closed"):
        fields["link_closed"] = 1
    return fields


def _codec_fields(sess: SerialSession) -> dict[str, Any]:
    """Which codec read this session's buffer text.

    A non-default codec reads the same bytes as different text, so every row
    that carries decoded console text must let a reader tell the readings
    apart - the session id alone cannot say which codec produced the body.
    Omitted on the historic path (utf-8) so routine rows keep their token
    count. Mirrors the screen boundary's ``_codec_fields``.
    """
    codec = sess.text_encoding or sess.buffer.text_encoding
    if codec != "utf-8":
        return {"encoding": codec}
    return {}


def list_consoles(**_kwargs: Any) -> OpResult:
    try:
        ports = list_serial_consoles()
    except ImportError as exc:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_DEP",
            fields={"op": "list", "msg": str(exc)},
            hint="install pyserial",
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_LIST_FAILED",
            fields={"op": "list", "msg": f"{type(exc).__name__}: {exc}"},
        )
    lines = [p.agent_line() for p in ports]
    devices = [p.device for p in ports]
    return OpResult(
        kind="console",
        status="ok",
        fields={
            "op": "list",
            "n": len(ports),
            "devices": ",".join(devices) if devices else None,
            "ports": [p.to_dict() for p in ports],
        },
        body="\n".join(lines) if lines else None,
        hint="use device= from list with console op=open; do not scan /dev or drivers",
    )


def open_console(
    *,
    path: str | None = None,
    device: str | None = None,
    baud: int | None = None,
    label: str | None = None,
    max_lines: int | None = None,
    encoding: str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Open *path*/*device* and start background capture.

    *encoding* is the peer console's text codec (a Python codec name such as
    ``gb18030``) for a device that does not speak utf-8: a serial device has
    no profile and nothing to probe, so the operator supplies it here. Unset
    keeps the historic utf-8/replace read.
    """
    # Normalize Windows COM (com3 / \\.\COM10 -> COM3 / COM10) before
    # allowlist + open so case and device-namespace forms are equivalent.
    port = _normalize_serial_port_name(path or device or "")
    if not port:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_ARG",
            fields={"op": "open", "msg": "path/device required"},
            hint="console op=list first \u2192 console op=open path=<device>",
        )
    # Name-shape guard: reject non-serial devices (controlling terminal,
    # PTY, ...) that would answer termios and mis-route the capture pump.
    # console op=list (ports.py) enumerates real devices; this blocks
    # arbitrary paths from bypassing that list.
    if not _is_serial_port_name(port):
        return OpResult(
            kind="console",
            status="error",
            code="INVALID_ARG",
            fields={
                "op": "open",
                "path": port,
                "msg": (
                    "not a serial device name; expected "
                    "/dev/tty<UPPERCASE><N> (e.g. /dev/ttyS0, /dev/ttyUSB0, "
                    "/dev/ttyXRUSB0) or /dev/cu.* or /dev/cua<N> or "
                    "/dev/rfcomm<N> or COM<N> (case-insensitive; \\\\.\\COMn ok)"
                ),
            },
            hint="console op=list \u2192 console op=open path=<device> from that list",
        )
    reg = get_serial_registry()
    # Per-device exclusivity: two CapturePumps on the same path would split
    # RX. Fast-path reject before opening hardware (process-local index).
    # Concurrent open races still hit DeviceBusyError in reg.add below.
    existing = reg.get_by_path(port)
    if existing is not None:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_IN_USE",
            fields={
                "op": "open",
                "path": port,
                "id": existing.id,
                "msg": f"device already open as {existing.id}",
            },
            hint=(
                f"use existing id={existing.id} (views/send/close); "
                "close it before reopening this path"
            ),
        )
    try:
        # Coerce numerics: bad baud/max_lines -> INVALID_ARG, not a raw raise.
        rate = int(baud) if baud is not None else 115200
        cap = int(max_lines) if max_lines is not None else DEFAULT_MAX_LINES
        # exclusive=True (SerialConsole default): OS-level TTY lock on POSIX
        # so a second process cannot open the same device either.
        console = SerialConsole(port, baudrate=rate)
    except (TypeError, ValueError) as exc:
        return OpResult(
            kind="console",
            status="error",
            code="INVALID_ARG",
            fields={
                "op": "open",
                "path": port,
                "msg": f"{type(exc).__name__}: {exc}",
            },
            hint="baud and max_lines must be integers",
        )
    except ImportError as exc:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_DEP",
            fields={"op": "open", "msg": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_OPEN_FAILED",
            fields={
                "op": "open",
                "path": port,
                "msg": f"{type(exc).__name__}: {exc}",
            },
        )

    # Peer text codec for the byte->text boundary. Resolved once here, at
    # open: unlike an endpoint a serial device has no profile and nothing to
    # probe, so the operator supplies it, and the ring's incremental decoder
    # is pinned for the session - a value that arrived later could not be
    # switched in without corrupting the character in flight. Validation is
    # this resolver's job (shared with the screen boundary): a name that is
    # not a byte-stream decoder warns and leaves utf-8 in force, so a typo
    # degrades the read instead of breaking the capture at its first byte.
    peer_codec = resolve_text_codec(encoding)
    unknown_peer_codec = (
        f"unusable text codec {str(encoding)!r}; console views are decoded as "
        "utf-8 (pass a Python codec name such as gb18030 in encoding=)"
        if encoding is not None and not text_codec_known(encoding)
        else None
    )

    buf = LineRingBuffer(max_lines=cap, text_encoding=peer_codec)
    con_id = reg.allocate_id()
    sess = SerialSession(
        id=con_id,
        console=console,
        path=port,
        baud=rate,
        buffer=buf,
        label=label,
        text_encoding=peer_codec,
        meta={"max_lines": cap},
    )
    # add() starts CapturePump - continuous RX -> buffer. Path exclusivity is
    # enforced under the registry lock (closes the get_by_path->add race).
    try:
        reg.add(sess)
    except DeviceBusyError as exc:
        # Concurrent open won the path; close the unused handle so the port
        # is not left held without a registry entry.
        try:
            console.close()
        except Exception:  # noqa: BLE001
            pass
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_IN_USE",
            fields={
                "op": "open",
                "path": port,
                "id": exc.existing_id,
                "msg": f"device already open as {exc.existing_id}",
            },
            hint=(
                f"use existing id={exc.existing_id} (views/send/close); "
                "close it before reopening this path"
            ),
        )
    # Short settle so the first views call often sees already-pending bytes.
    time.sleep(0.05)
    _brief_pump(sess)
    meta = buf.snapshot_meta()
    fields: dict[str, Any] = {
        "op": "open",
        "id": con_id,
        "path": port,
        "baud": rate,
        "surface": "console",
        "max_lines": cap,
        "capture": "background",
        "buf_lines": meta["lines"],
        "latest_seq": meta["latest_seq"],
        "dropped_lines": meta["dropped_lines"],
    }
    fields.update(_codec_fields(sess))
    if unknown_peer_codec:
        fields["warning"] = unknown_peer_codec
    fields.update(_pump_fields(sess))
    hint = (
        "background capture is on (RX always buffered). "
        "console op=send to write; console op=views to observe (tail|since|contains)"
    )
    if "pump_error" in fields or "link_closed" in fields:
        hint = (
            "capture pump reported a problem (pump_error/link_closed set); "
            "reopen the console if no new data flows. " + hint
        )
    return OpResult(kind="console", status="ok", fields=fields, hint=hint)


def send_console(
    *,
    id: str | None = None,
    data: str | None = None,
    data_b64: str | None = None,
    newline: bool = False,
    **_kwargs: Any,
) -> OpResult:
    sid = (id or "").strip()
    if not sid:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_ARG",
            fields={"op": "send", "msg": "id required"},
        )
    sess = get_serial_registry().get(sid)
    if sess is None:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_NOT_FOUND",
            fields={"op": "send", "id": sid, "msg": f"console not open: {sid}"},
        )
    if data_b64:
        # validate=True rejects non-alphabet / truncated padding. Without it,
        # garbage like "@@@" silently decodes to b"" and hits the empty-payload
        # ok branch - agents then treat junk input as a successful no-op send.
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            return OpResult(
                kind="console",
                status="error",
                code="INVALID_ARG",
                fields={"op": "send", "msg": f"bad data_b64: {exc}"},
                hint=(
                    "data_b64 must be standard base64 (alphabet + padding); "
                    "console op=send id=<id> data=... or data_b64=<b64>"
                ),
            )
    elif data is not None:
        # Literal operator text goes out in the codec this console reads with:
        # the capture ring decodes the device with it (``encoding=``), so the
        # peer's console reads back the same code page. A character the codec
        # cannot represent is replaced, never raised (see
        # ``codec.text_codec.encode_for_remote``). ``data_b64`` above is already
        # wire bytes and is never re-encoded.
        peer_codec = sess.text_encoding or sess.buffer.text_encoding
        raw = encode_for_remote(data, peer_codec or "utf-8")
    else:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_ARG",
            fields={"op": "send", "msg": "data or data_b64 required"},
        )
    # ``newline`` applies to both ``data`` and ``data_b64``. The terminator is
    # the send verb's own control byte, not operator text: it stays a raw LF in
    # every code page, like the screen side's submit key stays a raw CR. Only
    # the payload follows ``peer_codec``.
    if newline and not raw.endswith(b"\n"):
        raw += b"\n"

    # Brief pre-write yield so the background pump can drain pending RX and
    # latest_seq in the response reflects recent capture (not a post-write
    # echo wait - write happens below).
    time.sleep(0.02)
    meta = sess.buffer.snapshot_meta()
    expected = len(raw)
    base_fields: dict[str, Any] = {
        "op": "send",
        "id": sid,
        "expected": expected,
        "path": sess.path,
        "latest_seq": meta["latest_seq"],
    }
    # Empty payload (e.g. data="" newline=False) is a no-op success.
    if not raw:
        return OpResult(
            kind="console",
            status="ok",
            fields={**base_fields, "bytes": 0},
        )
    # Classify write outcomes: dead link, exception, zero-byte write, and
    # partial write are surfaced so the agent does not treat a failed send as
    # success. ``SerialConsole.write()`` propagates exceptions; if one carries
    # a recoverable partial count (``.written``), report PARTIAL_WRITE so the
    # agent resends only the tail (full retry would duplicate bytes already on
    # the link).
    alive = sess.console.is_alive()
    if not alive:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_CLOSED",
            fields={**base_fields, "bytes": 0, "msg": "console link is not alive"},
            hint="reopen the console (console op=close then console op=open)",
        )
    try:
        n = sess.console.write(raw)
    except Exception as exc:  # noqa: BLE001
        # Link error (e.g. SerialTimeoutException). Prefer PARTIAL_WRITE when
        # ``.written`` is a recoverable count; else CONSOLE_WRITE_FAILED with
        # the real error type/message.
        partial = getattr(exc, "written", None)
        if isinstance(partial, int) and 0 < partial < expected:
            return OpResult(
                kind="console",
                status="ok",
                code="PARTIAL_WRITE",
                fields={
                    **base_fields,
                    "bytes": partial,
                    "warning": (
                        f"write raised {type(exc).__name__}: {exc} "
                        f"after {partial}/{expected} bytes"
                    ),
                },
                hint="resend the remaining bytes; link may be slow or saturated",
            )
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_WRITE_FAILED",
            fields={
                **base_fields,
                "bytes": 0,
                "msg": f"{type(exc).__name__}: {exc}",
            },
            hint="link may be busy or disconnected; reopen or retry",
        )
    if n == 0:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_WRITE_FAILED",
            fields={**base_fields, "bytes": 0, "msg": "write returned 0 bytes"},
            hint="link may be busy or disconnected; reopen or retry",
        )
    if n < expected:
        # Partial success: status ok with PARTIAL_WRITE so the agent resends
        # only the unwritten tail.
        return OpResult(
            kind="console",
            status="ok",
            code="PARTIAL_WRITE",
            fields={
                **base_fields,
                "bytes": n,
                "warning": f"only {n}/{expected} bytes written",
            },
            hint="resend the remaining bytes; link may be slow or saturated",
        )
    return OpResult(
        kind="console",
        status="ok",
        fields={
            **base_fields,
            "bytes": n,
        },
    )


def views_console(
    *,
    id: str | None = None,
    mode: str | None = None,
    n: int | None = None,
    since: int | None = None,
    contains: str | None = None,
    context: int | None = None,
    with_seq: bool = False,
    max_lines: int | None = None,
    settle_ms: int | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Query the capture buffer filled by the background pump.

    mode: tail (default) | since | contains
    """
    sid = (id or "").strip()
    if not sid:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_ARG",
            fields={"op": "views", "msg": "id required"},
        )
    sess = get_serial_registry().get(sid)
    if sess is None:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_NOT_FOUND",
            fields={"op": "views", "id": sid, "msg": f"console not open: {sid}"},
        )
    # Coerce numerics up front so bad values become INVALID_ARG, not a raise.
    try:
        settle_i = int(settle_ms) if settle_ms is not None else 0
        hard_cap = int(max_lines) if max_lines is not None else DEFAULT_VIEWS_MAX_LINES
        since_i = int(since) if since is not None else 0
        context_i = int(context) if context is not None else 3
        count_i = int(n) if n is not None else DEFAULT_VIEW_TAIL
    except (TypeError, ValueError) as exc:
        return OpResult(
            kind="console",
            status="error",
            code="INVALID_ARG",
            fields={
                "op": "views",
                "id": sid,
                "msg": f"{type(exc).__name__}: {exc}",
            },
            hint="settle_ms/max_lines/since/context/n must be integers",
        )
    hard_cap = max(1, hard_cap)

    # Optional short settle so just-sent echo lands (pump is primary).
    if settle_i > 0:
        time.sleep(min(0.5, settle_i / 1000.0))
        _brief_pump(sess)

    buf = sess.buffer
    m = (mode or "tail").strip().lower()
    if m not in ("tail", "since", "contains"):
        return OpResult(
            kind="console",
            status="error",
            code="INVALID_ARG",
            fields={
                "op": "views",
                "msg": f"mode must be tail|since|contains, got {m!r}",
            },
        )

    hits_total = 0
    truncated = False
    lines_out: list[BufLine]
    # latest_at_view: last committed seq under the same lock as the lines
    # snapshot (from view_*_with_meta). Do not derive the follow cursor from a
    # separate snapshot_meta() call: a feed between the view releasing its
    # lock and snapshot_meta acquiring it could make last_seq == latest_seq
    # while to_seq still pointed at a synthetic partial, so the next
    # since=<to_seq> (strict >) would skip the line that commits at _next_seq.
    latest_at_view: int

    if m == "since":
        lines_out, latest_at_view = buf.view_since_with_meta(
            since_i, include_partial=True
        )
    elif m == "contains":
        pat = contains or ""
        lines_out, hits_total = buf.view_contains(pat, context=context_i)
        # contains has no synthetic partial - no cursor drop. Use
        # snapshot_meta for latest_seq (current state); cursor stays at the
        # last matched committed seq.
        latest_at_view = buf.snapshot_meta()["latest_seq"]
    else:
        lines_out, latest_at_view = buf.view_tail_with_meta(
            count_i, include_partial=True
        )

    if len(lines_out) > hard_cap:
        truncated = True
        # Keep newest slice for tail/since; for contains keep first hard_cap.
        if m in ("since", "tail"):
            lines_out = lines_out[-hard_cap:]
        else:
            lines_out = lines_out[:hard_cap]

    body = buf.format_lines(lines_out, with_seq=with_seq)
    # buf_lines / dropped_lines are current-buffer observability, not the
    # follow cursor; a separate snapshot_meta() is fine for these fields.
    meta = buf.snapshot_meta()
    first_seq = lines_out[0].seq if lines_out else latest_at_view
    last_seq = lines_out[-1].seq if lines_out else latest_at_view
    # Synthetic partial (tail/since, include_partial=True) uses
    # seq = _next_seq (== latest_at_view + 1) for display only. Pointing
    # to_seq at it would make the next since=<to_seq> skip the line that
    # commits at _next_seq. Drop the cursor to the last committed seq at
    # view time (latest_at_view); the body still shows the partial.
    # latest_at_view was captured under the same lock as the lines, so a
    # concurrent feed cannot push the cursor past a committed line.
    if lines_out and last_seq > latest_at_view:
        to_seq = latest_at_view
    else:
        to_seq = last_seq
    fields: dict[str, Any] = {
        "op": "views",
        "id": sid,
        "mode": m,
        "n": len(lines_out),
        "from_seq": first_seq,
        "to_seq": to_seq,
        # latest_seq matches view-time state (consistent with to_seq) so the
        # agent does not see a latest_seq newer than its cursor.
        "latest_seq": latest_at_view,
        "buf_lines": meta["lines"],
        "dropped_lines": meta["dropped_lines"],
        "path": sess.path,
        "capture": "background",
    }
    # Same codec evidence as the open row: this body was decoded by the
    # session's pinned codec, and a reader holding only the id must be able
    # to tell a legacy-console reading from a utf-8 one.
    fields.update(_codec_fields(sess))
    fields.update(_pump_fields(sess))
    if truncated:
        fields["truncated"] = 1
        fields["views_cap"] = hard_cap
    if m == "contains":
        fields["hits"] = hits_total
        if hits_total > 20:
            fields["hits_capped"] = 1
    hint = (
        "buffer is filled by background capture; "
        "use mode=since since=<to_seq> for incremental follow; "
        "large views may set truncated=1"
    )
    if "pump_error" in fields or "link_closed" in fields:
        hint = (
            "capture pump reported a problem (pump_error/link_closed set); "
            "reopen the console if no new data flows. " + hint
        )
    return OpResult(
        kind="console",
        status="ok",
        fields=fields,
        body=body if body else None,
        hint=hint,
    )


def close_console(*, id: str | None = None, **_kwargs: Any) -> OpResult:
    sid = (id or "").strip()
    if not sid:
        return OpResult(
            kind="console",
            status="error",
            code="MISSING_ARG",
            fields={"op": "close", "msg": "id required"},
        )
    removed = get_serial_registry().remove(sid)
    if removed is None:
        return OpResult(
            kind="console",
            status="error",
            code="CONSOLE_NOT_FOUND",
            fields={"op": "close", "id": sid, "msg": f"console not open: {sid}"},
        )
    # SerialRegistry.remove does best-effort stop_capture -> flush_partial ->
    # console.close and collects per-step failures on removed.close_errors
    # (empty on success; never raises). Surface them so the agent knows the
    # port may still be busy. The registry entry is gone either way, so
    # closed=True and status=ok remain (warning, not close-op failure).
    fields: dict[str, Any] = {
        "op": "close",
        "id": sid,
        "path": removed.path,
        "closed": True,
    }
    if removed.close_errors:
        fields["close_error"] = "; ".join(removed.close_errors)
        return OpResult(
            kind="console",
            status="ok",
            code="CONSOLE_CLOSE_PARTIAL",
            fields=fields,
            hint=(
                "console removed but a close step failed (see close_error); "
                "the next open may report 'device busy' \u2014 reopen or retry"
            ),
        )
    return OpResult(kind="console", status="ok", fields=fields)


def list_sessions(**_kwargs: Any) -> OpResult:
    sessions = get_serial_registry().list_open()
    lines = []
    for s in sessions:
        m = s.buffer.snapshot_meta()
        stats = s.pump.stats if s.pump else {}
        run = "1" if stats.get("running") else "0"
        extras = []
        if stats.get("error"):
            extras.append(f"error={stats['error']}")
        if stats.get("link_closed"):
            extras.append("link_closed=1")
        lines.append(
            f"id={s.id} path={s.path} baud={s.baud} "
            f"lines={m['lines']} latest_seq={m['latest_seq']} pump={run}"
            + (f" label={s.label}" if s.label else "")
            + (f" {' '.join(extras)}" if extras else "")
        )
    return OpResult(
        kind="console",
        status="ok",
        fields={"op": "sessions", "n": len(sessions)},
        body="\n".join(lines) if lines else None,
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch console op. Accept bare names (list|open|...) or console_* prefix."""
    op_norm = (op or "").strip().lower().replace("-", "_")
    if op_norm.startswith("console_"):
        op_norm = op_norm.removeprefix("console_")
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="console",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown console op (want list|open|send|views|close|sessions)",
            },
            hint="console op=list|open|send|views|close|sessions",
        )
    dispatch = {
        "list": list_consoles,
        "open": open_console,
        "send": send_console,
        "views": views_console,
        "close": close_console,
        "sessions": list_sessions,
    }
    return dispatch[op_norm](**kwargs)
