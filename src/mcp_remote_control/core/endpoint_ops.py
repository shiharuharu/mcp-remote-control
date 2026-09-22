"""Endpoint Core ops: list, open, and close host connections.

Maps profile names to connected transports, surfaces capability tokens and a
compact probe summary on open, and tears down attached screen/ps sessions on
close. Serial hardware uses the separate console tool. list/open include a
``notes=1`` flag when ``notes/{name}.md`` exists and size > 0; the notes body
is never inlined. A registered endpoint whose transport is not alive shows a
truncated ``dead_reason=`` token in the list body, and open adds the transport's
``link_lost`` / ``session_resynced`` markers when it recorded them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, notes_present
from mcp_remote_control.core.result import OpResult, _home, _short
from mcp_remote_control.endpoint.caps import format_caps, merge_caps
from mcp_remote_control.endpoint.registry import (
    Endpoint,
    connect_failure_fields,
    get_registry,
    list_known_profiles,
)
from mcp_remote_control.transport import TransportError

VALID_OPS: frozenset[str] = frozenset({"list", "open", "close"})

# Death reasons are free-form diagnostic prose (``link lost``, ``stale_on_open``,
# an HTTP status line). The list body token is collapsed onto one token and
# truncated so one verbose mark_dead message cannot stretch the line.
_MAX_DEAD_REASON_CHARS = 40


def _notes_flag(home: Path, name: str) -> bool:
    """True when ``notes/{name}.md`` exists and size > 0.

    Invalid names (listed as profile stems but not ``PROFILE_NAME_RE``) must
    not raise - listing still has to return the other rows.
    """
    try:
        return notes_present(home, name)
    except (ProfileInvalid, OSError):
        return False


def _notes_suffix(home: Path, name: str) -> str:
    """`` notes=1`` when present, else empty (omit by default)."""
    return " notes=1" if _notes_flag(home, name) else ""


def _transport_meta(transport: Any | None) -> dict[str, Any]:
    """Transport ``meta`` mapping, or ``{}`` when the backend exposes none.

    Never raises: list/open must still render when a transport double or a
    partly torn-down backend has no usable meta.
    """
    meta = getattr(transport, "meta", None) if transport is not None else None
    return meta if isinstance(meta, dict) else {}


@contextmanager
def _name_fence(reg: Any, name: str) -> Iterator[None]:
    """Hold the registry's per-name RLock for *name* (no-op for a bare double).

    ``open`` reads the registered handle and then opens; a concurrent same-name
    open can otherwise replace a dead handle between the two, and the live
    replacement it registered - already carrying its own recovery - is then
    credited to this call's ``session_resynced`` field. Fencing both steps
    serializes same-name opens, so the handle this call reports on is either
    the one it saw or the one it established itself.

    Acquire order matches ``close_endpoint`` and the registry's own: take the
    main RLock only long enough to fetch the per-name lock object, release it,
    then hold the per-name lock. It is an RLock, so the ``reg.open`` call made
    inside re-enters on this thread. A registry double exposing neither
    attribute simply forgoes the fence; the identity check still applies.

    The fence necessarily covers ``reg.open``'s liveness probe (which runs
    outside the main RLock) as well as the connect; snapshot and open must be
    atomic or the identity check means nothing. It adds no lock edge
    ``reg.open`` does not already take: a same-name rival blocks on this same
    lock in its Phase 2, so the probe window lengthens an existing wait
    rather than creating a new one.
    """
    main = getattr(reg, "_lock", None)
    getter = getattr(reg, "_get_or_create_name_lock", None)
    if main is None or not callable(getter):
        yield
        return
    with main:
        name_lock = getter(name)
    with name_lock:
        yield


def _dead_reason_suffix(ep: Endpoint) -> str:
    """`` dead_reason=...`` for a registered endpoint whose transport is dead.

    Reason precedence matches the open failure path: ``meta["dead_reason"]``
    (set by ``mark_dead``) then ``meta["probe_error"]``. Live endpoints and
    endpoints with no recorded reason stay token-free, so a healthy list line
    keeps its historical shape. The value stays a single space-free token
    (internal whitespace becomes ``_``, as on the Agent status line) and is
    truncated so one verbose reason cannot stretch the row.
    """
    if ep.connected:
        return ""
    meta = _transport_meta(ep.transport)
    reason = meta.get("dead_reason") or meta.get("probe_error")
    if reason is None or not str(reason).strip():
        return ""
    token = _short(str(reason), limit=_MAX_DEAD_REASON_CHARS).replace(" ", "_")
    return f" dead_reason={token}"


def list_endpoints(
    *,
    home: Path | str | None = None,
    **_kwargs: Any,
) -> OpResult:
    """List known profiles and open endpoint status."""
    home_path = _home(home)
    try:
        names = list_known_profiles(home_path)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            kind="endpoint",
            status="error",
            code="CONFIG_ERROR",
            fields={"op": "list", "msg": str(exc)},
        )

    reg = get_registry()
    # list_open syncs Endpoint.connected from transport.is_connected() so a
    # mark_dead zombie is not reported as open=1 (never trust cache alone).
    open_map = {ep.name: ep for ep in reg.list_open()}
    # Count only still-live registrations for fields.open.
    live_open = sum(1 for ep in open_map.values() if ep.connected)

    lines: list[str] = []
    for name in names:
        note = _notes_suffix(home_path, name)
        ep = open_map.get(name)
        if ep is not None:
            caps = ep.caps_token
            transport = ep.transport_name
            open_flag = "1" if ep.connected else "0"
            extra = ""
            if ep.meta.get("host"):
                extra = f" host={ep.meta['host']}"
            lines.append(
                f"{name} transport={transport} open={open_flag} "
                f"caps={caps}{extra}{_dead_reason_suffix(ep)}{note}"
            )
        else:
            # Not open: derive caps from the on-disk profile when loadable.
            try:
                from mcp_remote_control.config import load_profile

                profile = load_profile(home_path, name)
                caps = format_caps(merge_caps(profile.transport, profile.caps or None))
                host_bit = f" host={profile.host}" if profile.host else ""
                lines.append(
                    f"{name} transport={profile.transport} open=0 "
                    f"caps={caps}{host_bit}{note}"
                )
            except (ProfileNotFound, ProfileInvalid):
                lines.append(f"{name} open=0{note}")

    # Still list open endpoints whose profile file was removed after connect.
    for name, ep in open_map.items():
        if name not in names:
            note = _notes_suffix(home_path, name)
            lines.append(
                f"{name} transport={ep.transport_name} open="
                f"{'1' if ep.connected else '0'} caps={ep.caps_token}"
                f"{_dead_reason_suffix(ep)}{note}"
            )

    body = "\n".join(lines) if lines else None
    return OpResult(
        kind="endpoint",
        status="ok",
        fields={
            "op": "list",
            # n counts the rows this result actually carries. len(names) only
            # covers on-disk profiles, while an endpoint that stays open after
            # its profile file is removed still gets a row appended above.
            "n": len(lines),
            "open": live_open,
        },
        body=body,
    )


def open_endpoint(
    *,
    profile: str | None = None,
    ep: str | None = None,
    home: Path | str | None = None,
    probe: bool = True,
    connector: Any | None = None,
    **_kwargs: Any,
) -> OpResult:
    """Open / connect an endpoint from a profile name."""
    name = profile or ep
    if not name or not str(name).strip():
        return OpResult(
            kind="endpoint",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "open",
                "msg": "profile name required (profile= or ep=)",
            },
            hint="mcp-remote-control-cli endpoint open --profile <name>",
        )

    name = str(name).strip()
    home_path = _home(home)
    reg = get_registry()
    # Handle identity pins attribution of the sticky ``session_resynced``
    # marker: ``registry.open`` returns a still-live endpoint verbatim (its
    # liveness check is ``is_connected`` / ``is_alive`` only - no reconnect, no
    # probe) and the marker is never cleared, so a marker on the reused handle
    # belongs to another call's recovery, whether it was set before this open
    # or during it. Report below only for a different handle.
    #
    # Identity alone is not authorship: a concurrent same-name open that
    # replaces a dead handle also returns a different object. This call runs
    # the snapshot and the open inside one per-name fence, so a rival open
    # either lands wholly before it - and this call then snapshots the live
    # replacement and reports no recovery - or wholly after it, and then this
    # call is the one that reconnected and owns the marker it reports.
    with _name_fence(reg, name):
        prior = reg.get(name)
        try:
            endpoint = reg.open(
                name,
                home=home_path,
                probe=bool(probe),
                connector=connector,
            )
        except ProfileNotFound as exc:
            return OpResult(
                kind="endpoint",
                status="error",
                code="PROFILE_NOT_FOUND",
                fields={"op": "open", "profile": name, "msg": _short(str(exc))},
            )
        except ProfileInvalid as exc:
            return OpResult(
                kind="endpoint",
                status="error",
                code="PROFILE_INVALID",
                fields={"op": "open", "profile": name, "msg": _short(str(exc))},
            )
        except TransportError as exc:
            # A failed open is either "nothing answered" or "something answered
            # and refused the request": the registry classifies it, and this is
            # the surface the reopen remedy points at, so it must carry the same
            # tokens as the fs/exec/ps rows rather than host+prose alone.
            fields: dict[str, Any] = {
                "op": "open",
                "profile": name,
                "msg": exc.msg,
            }
            fields.update(connect_failure_fields(exc))
            return OpResult(
                kind="endpoint",
                status="error",
                code=exc.code or "CONNECT_FAILED",
                fields=fields,
            )
        except ValueError as exc:
            return OpResult(
                kind="endpoint",
                status="error",
                code="INVALID_ARG",
                fields={"op": "open", "msg": _short(str(exc))},
            )
        except Exception as exc:  # noqa: BLE001 - keep the process alive on open
            return OpResult(
                kind="endpoint",
                status="error",
                code="CONNECT_FAILED",
                fields={
                    "op": "open",
                    "profile": name,
                    "msg": _short(f"{type(exc).__name__}: {exc}"),
                },
            )

    # Probe / peer-drop must not leave a registered-but-dead endpoint.
    # Capture liveness + dead reason before close - after reg.close the
    # endpoint object must not be trusted (transport torn down).
    #
    # WinRM: collect_probe hard-fails identity/WSMan RTT by mark_dead so
    # is_connected() is False here; open returns error and pops the registry
    # (no fake connected). Capability-only incomplete stays partial + live.
    transport = endpoint.transport
    probe_meta = endpoint.probe if isinstance(endpoint.probe, dict) else None
    probe_status = (
        str(probe_meta.get("status") or "").strip().lower() if probe_meta else ""
    )
    live = False
    if transport is not None:
        try:
            live = bool(transport.is_connected())
        except Exception:  # noqa: BLE001 - treat probe failure as dead
            live = False
    # Defensive: identity probe reported fail/error but flag still live.
    if live and probe_status in ("fail", "error"):
        mark = getattr(transport, "mark_dead", None) if transport is not None else None
        if callable(mark):
            reason = None
            if probe_meta:
                reason = probe_meta.get("error") or probe_meta.get("probe_error")
            mark(str(reason or "identity probe failed")[:200])
        live = False
    if not live:
        t_meta = _transport_meta(transport)
        dead = t_meta.get("dead_reason") or t_meta.get("probe_error")
        if not dead and probe_meta:
            dead = probe_meta.get("error") or probe_meta.get("probe_error")
        # Pop only this generation's handle before error return so Agent
        # retry / list / ensure never sees a zombie from a failed open.
        # Identity-pinned (close_if_same): Phase-1 open may return E1 under
        # the main RLock only; concurrent mark_dead+ensure can register E2
        # under the same name before this post-check. Name-only close would
        # kill E2. close_if_same also generation-fences screen/ps for the
        # matched dying generation - same snapshot+close_ids as
        # close_endpoint / _retire_stale_endpoint; a pin miss is a no-op so
        # E2's sessions survive. TransportError above never registers
        # (reg.open raises before insert) - only clean up when open returned
        # a registered handle that is not live *and* still maps to that
        # object.
        reg.close_if_same(name, endpoint)
        code = (
            "PROBE_FAILED"
            if probe_status in ("fail", "error")
            or (dead and "probe" in str(dead).lower())
            else "NOT_CONNECTED"
        )
        fields_err: dict[str, Any] = {
            "op": "open",
            "profile": name,
            "ep": name,
            "msg": dead or "transport not connected after open",
        }
        # A link that died in flight (mark_dead after a failed re-handshake)
        # is a different remedy than a refused/absent connection: surface the
        # transport's flag so Agent can close+open instead of re-reading auth.
        if t_meta.get("link_lost") is True:
            fields_err["link_lost"] = 1
        return OpResult(
            kind="endpoint",
            status="error",
            code=code,
            fields=fields_err,
            hint="check auth (password= plain ok) and ssh.known_hosts=none for lab hosts",
        )

    # Keep Endpoint.connected in sync with transport (probe may have flipped it).
    endpoint.connected = True

    fields_ok: dict[str, Any] = {
        "op": "open",
        "ep": endpoint.name,
        "transport": endpoint.transport_name,
        "caps": endpoint.caps_token,
        "open": 1 if endpoint.connected else 0,
    }
    if endpoint.meta.get("host"):
        fields_ok["host"] = endpoint.meta["host"]
    if endpoint.meta.get("label"):
        fields_ok["label"] = endpoint.meta["label"]
    # Link self-heal marker: a probe (or a resumed call) that recovered by
    # re-handshaking the WinRM session sets this on the transport. Report it
    # only for a handle other than the one seen before this open - a reused
    # one carries another caller's recovery (see the identity note above).
    if endpoint is not prior and (
        _transport_meta(transport).get("session_resynced") is True
    ):
        fields_ok["session_resynced"] = 1
    # Presence bit only; notes body is never returned on open.
    if _notes_flag(home_path, endpoint.name):
        fields_ok["notes"] = 1

    # Compact shell/uname/locale tokens for Agent meta - not a full probe dump.
    if endpoint.probe:
        fields_ok.update(_probe_summary_fields(endpoint.probe))

    return OpResult(
        kind="endpoint",
        status="ok",
        cwd=endpoint.cwd,
        fields=fields_ok,
    )


def close_endpoint(
    *,
    ep: str | None = None,
    profile: str | None = None,
    home: Path | str | None = None,  # reserved for API symmetry with open/list
    **_kwargs: Any,
) -> OpResult:
    """Close a connected endpoint."""
    name = ep or profile
    if not name or not str(name).strip():
        return OpResult(
            kind="endpoint",
            status="error",
            code="MISSING_ARG",
            fields={
                "op": "close",
                "msg": "endpoint id required (ep= or profile=)",
            },
            hint="mcp-remote-control-cli endpoint close --ep <name>",
        )

    name = str(name).strip()
    reg = get_registry()

    # screen/ps id snapshot must complete inside the same per-name generation
    # fence as reg.close (mirror close_if_same / _retire_stale_endpoint). A
    # lock-external snapshot then name-close can kill a concurrent
    # ensure/open's newer generation while close_ids only tears the pre-swap
    # ids -> wrong transport killed + zombie sessions.
    #
    # Hold the registry per-name RLock across pop (via re-entrant reg.close)
    # and the post-pop id snapshot. Same-name open/ensure block until the
    # fence releases; only the dying generation's name-keyed ids are listed.
    # Field counts stay here (not inside EndpointRegistry.close).
    screen_ids: list[str] = []
    ps_ids: list[str] = []
    with reg._lock:
        name_lock = reg._get_or_create_name_lock(name)
    with name_lock:
        removed = reg.close(name)
        if removed is not None:
            # Snapshot AFTER pop, still under the name lock - concurrent
            # same-name re-register cannot land mid-fence; dying generation
            # sessions remain name-keyed until close_ids below.
            try:
                from mcp_remote_control.core.screen_ops import (
                    snapshot_endpoint_session_ids as _scr_ids,
                )

                screen_ids = list(_scr_ids(name))
            except Exception:  # noqa: BLE001
                screen_ids = []
            try:
                from mcp_remote_control.core.ps_ops import (
                    snapshot_endpoint_session_ids as _ps_ids,
                )

                ps_ids = list(_ps_ids(name))
            except Exception:  # noqa: BLE001
                ps_ids = []

    if removed is None:
        return OpResult(
            kind="endpoint",
            status="error",
            code="ENDPOINT_NOT_FOUND",
            fields={
                "op": "close",
                "ep": name,
                "msg": f"endpoint not open: {name}",
            },
        )

    # Tear down only snapshotted sessions; never fail the close on cleanup.
    # close_ids runs outside the name lock (session close is IO; no deadlock
    # with screen/ps locks). Concurrent reopen after the fence may register
    # new sessions that are not in screen_ids/ps_ids and survive.
    screens_closed = 0
    try:
        from mcp_remote_control.core.screen_ops import (
            close_sessions_by_ids as _scr_close,
        )

        screens_closed = int(_scr_close(screen_ids) or 0)
    except Exception:  # noqa: BLE001
        screens_closed = 0

    ps_closed = 0
    try:
        from mcp_remote_control.core.ps_ops import (
            close_sessions_by_ids as _ps_close,
        )

        ps_closed = int(_ps_close(ps_ids) or 0)
    except Exception:  # noqa: BLE001
        ps_closed = 0

    fields: dict[str, Any] = {
        "op": "close",
        "ep": name,
        "disconnected": True,
    }
    if screens_closed:
        fields["screens_closed"] = screens_closed
    if ps_closed:
        fields["ps_closed"] = ps_closed

    return OpResult(
        kind="endpoint",
        status="ok",
        fields=fields,
    )


def run(op: str, **kwargs: Any) -> OpResult:
    """Dispatch endpoint op -> Core implementation."""
    op_norm = (op or "").strip().lower().replace("-", "_")
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="endpoint",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown endpoint op (want list|open|close)",
            },
            hint="use op=list|open|close; serial console is the console tool",
        )
    dispatch = {
        "list": list_endpoints,
        "open": open_endpoint,
        "close": close_endpoint,
    }
    return dispatch[op_norm](**kwargs)


def _probe_summary_fields(probe: dict[str, Any]) -> dict[str, Any]:
    """Map probe keys to short Agent meta tokens (non-empty only).

    ``shell_base``/``shell_path`` -> ``shell=``; ``uname``; ``charmap`` /
    ``text_encoding`` -> ``locale=``; ``dialect=``; optional ``busybox=1``.

    WinRM PS capability tokens (flat keys or nested ``winrm_ps``): ``ps_version=``,
    ``lang_mode=``, ``ps_fs=`` (0/1 from ``ps_script_fs``), optional ``ps_edition=``.
    """
    out: dict[str, Any] = {}

    shell = probe.get("shell_base") or probe.get("shell")
    if not shell:
        sp = probe.get("shell_path")
        if isinstance(sp, str) and sp.strip():
            base = sp.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base.lower().endswith(".exe"):
                base = base[:-4]
            shell = base or None
    if shell is not None and str(shell).strip():
        out["shell"] = str(shell).strip()

    dialect = probe.get("dialect")
    if dialect is not None and str(dialect).strip():
        out["dialect"] = str(dialect).strip()

    caps = probe.get("caps") if isinstance(probe.get("caps"), dict) else {}
    if probe.get("busybox") or (caps or {}).get("busybox"):
        out["busybox"] = 1

    uname = probe.get("uname")
    if uname is not None and str(uname).strip():
        out["uname"] = str(uname).strip()

    locale = probe.get("text_encoding") or probe.get("charmap")
    if locale is not None and str(locale).strip():
        out["locale"] = str(locale).strip()

    # WinRM PS capability summary: prefer flat probe keys, fall back to winrm_ps.
    _raw_wp = probe.get("winrm_ps")
    winrm_ps: dict[str, Any] = _raw_wp if isinstance(_raw_wp, dict) else {}

    def _pick(key: str) -> Any:
        val = probe.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            val = winrm_ps.get(key)
        return val

    ps_version = _pick("ps_version")
    if ps_version is not None and str(ps_version).strip():
        out["ps_version"] = str(ps_version).strip()

    lang_mode = _pick("language_mode")
    if lang_mode is not None and str(lang_mode).strip():
        out["lang_mode"] = str(lang_mode).strip()

    ps_script_fs = _pick("ps_script_fs")
    if ps_script_fs is not None and str(ps_script_fs).strip() != "":
        if isinstance(ps_script_fs, str):
            truthy = ps_script_fs.strip().lower() in ("1", "true", "yes")
        else:
            truthy = bool(ps_script_fs)
        out["ps_fs"] = 1 if truthy else 0

    ps_edition = _pick("ps_edition")
    if ps_edition is not None and str(ps_edition).strip():
        out["ps_edition"] = str(ps_edition).strip()

    ps_probe = _pick("ps_probe")
    if ps_probe is not None and str(ps_probe).strip():
        out["ps_probe"] = str(ps_probe).strip()

    return out
