"""Fs Core ops: agent-facing filesystem dispatch for CLI and MCP.

Owns the public op set (``list``/``stat``/``read``/``write``/``put``/
``get``/``mkdir``/``rm``). Resolves the endpoint, selects a backend via
:func:`~mcp_remote_control.fs.service.backend_for_endpoint`, dispatches,
and maps results to structured :class:`~mcp_remote_control.core.result.OpResult`
rows. Backend factories live in ``fs.service``; this module is the sole
public Core entry for filesystem ops.
"""

from __future__ import annotations

import hashlib
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
from mcp_remote_control.fs.service import backend_for_endpoint
from mcp_remote_control.fs.types import (
    FsBackend,
    FsError,
    ListResult,
    ProgressCallback,
    ReadResult,
    StatInfo,
    TransferResult,
    WriteResult,
)
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.ssh import SSHConnector

VALID_OPS: frozenset[str] = frozenset(
    {"list", "stat", "read", "write", "put", "get", "mkdir", "rm"}
)

__all__ = ["VALID_OPS", "run"]

# Stable reopen guidance token, same text the exec/ps rows carry: an fs link
# failure is retired, not replayed (README:454), so the only remedy is a reopen.
_REOPEN_HINT = "endpoint close then open"

# Transport dead_reason for a lost link (``WinRMTransport._mark_link_dead``).
_LINK_LOST_REASON = "link lost"

# Field carrying the backend's own node path when it differs from the caller's
# (see ``_node_path_field``).
_NODE_PATH_FIELD = "node_path"


def run(
    op: str,
    *,
    ep: str | None = None,
    path: str | None = None,
    content: str | None = None,
    local: str | None = None,
    recursive: bool | None = None,
    max_bytes: int | None = None,
    progress: ProgressCallback | None = None,
    home: Path | str | None = None,
    connector: SSHConnector | None = None,
    backend: FsBackend | None = None,
    sftp_client: Any | None = None,
    file_client: Any | None = None,
    include_via: bool = True,
    **_kwargs: Any,
) -> OpResult:
    """Dispatch a filesystem op through the backend selected by the endpoint.

    When *backend* is omitted, lazy-connects *ep* via the endpoint registry,
    checks the ``fs`` capability, and builds a backend from the transport.
    Parameters such as *backend*, *sftp_client*, and *file_client* are
    injection hooks for tests; production callers pass only the public op
    arguments. Errors become structured ``OpResult`` rows (never raise to
    the agent layer).
    """
    op_norm = (op or "").strip().lower()
    if op_norm not in VALID_OPS:
        return OpResult(
            kind="fs",
            status="error",
            code="INVALID_OP",
            fields={
                "op": op_norm or op,
                "msg": "unknown fs op",
                "ep": ep,
                "path": path,
            },
            hint="use op=list|stat|read|write|put|get|mkdir|rm",
        )

    if backend is None:
        if not ep or not str(ep).strip():
            return OpResult(
                kind="fs",
                status="error",
                code="MISSING_ARG",
                fields={"op": op_norm, "msg": "ep is required"},
                hint="pass ep=<profile> (lazy connect)",
            )
        ep_name = str(ep).strip()
    else:
        ep_name = str(ep).strip() if ep else None

    # Path/content/local do not depend on the remote. Reject before lazy
    # connect so a connect fault cannot mask a missing argument.
    arg_err = _validate_args(
        op_norm,
        path=path,
        content=content,
        local=local,
        ep=ep_name,
    )
    if arg_err is not None:
        return arg_err

    # Set on the lazy-connect path; the failure rows read its recorded link
    # state. Stays None when a backend is injected (no endpoint in play).
    transport: BaseTransport | None = None

    if backend is None:
        # ep was required and stripped non-empty in the connect path above.
        assert ep_name is not None
        home_path = _home(home)
        try:
            endpoint = ensure_endpoint(
                ep_name,
                home=home_path,
                connector=connector,
            )
        except ProfileNotFound as exc:
            return _err(
                op_norm,
                "PROFILE_NOT_FOUND",
                _short(str(exc)),
                ep=ep_name,
                path=path,
            )
        except ProfileInvalid as exc:
            return _err(
                op_norm,
                "PROFILE_INVALID",
                _short(str(exc)),
                ep=ep_name,
                path=path,
            )
        except TransportError as exc:
            # A failed lazy connect is either "nothing answered" or "something
            # answered and rejected the request". The registry classifies it
            # (``_open_failure_tokens``); mirror those tokens so an Agent can
            # tell a refused reconnect from an unreachable host without
            # parsing the prose.
            return _err(
                op_norm,
                exc.code or "CONNECT_FAILED",
                exc.msg,
                ep=ep_name,
                path=path,
                extra=_connect_failure_fields(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return _err(
                op_norm,
                "CONNECT_FAILED",
                _short(f"{type(exc).__name__}: {exc}"),
                ep=ep_name,
                path=path,
            )

        if not endpoint.caps.get("fs", False):
            return _err(
                op_norm,
                "CAP_DENIED",
                "endpoint lacks fs capability",
                ep=ep_name,
                path=path,
                extra={"caps": endpoint.caps_token},
            )

        transport = endpoint.transport
        if transport is None or not transport.is_connected():
            # The recorded death cause beats the generic text (ps
            # ``_dead_transport_msg``): it names why the link is gone and
            # matches the dead_reason= token endpoint list/open report. The
            # link tokens keep "retired by a link failure" distinguishable
            # from "never opened".
            return _err(
                op_norm,
                "NOT_CONNECTED",
                _dead_transport_msg(transport),
                ep=ep_name,
                path=path,
                extra=_dead_link_fields(transport),
            )

        try:
            backend = backend_for_endpoint(
                endpoint,
                sftp_client=sftp_client,
                file_client=file_client,
            )
        except FsError as exc:
            return _err(
                op_norm,
                exc.code,
                exc.msg,
                ep=ep_name,
                path=path,
                extra=_dead_link_fields(transport),
            )
        cwd = endpoint.cwd or transport.cwd
    else:
        cwd = getattr(backend, "_cwd", None)

    t0 = time.monotonic()
    try:
        result = _dispatch(
            backend,
            op_norm,
            path=path,
            content=content,
            local=local,
            recursive=bool(recursive),
            max_bytes=max_bytes,
            progress=progress,
        )
    except FsError as exc:
        # A refused link (e.g. a WSMan 401 from an authenticated session) is a
        # dead endpoint, not a remote path verdict: retire it here so the next
        # call reconnects instead of handing back the same refused session.
        retire_refused_link(transport, exc)
        hint = None
        if exc.code == "UNSUPPORTED":
            hint = (
                "fs over winrm requires FullLanguage (see msg for lang_mode); "
                "use exec or native copy/fetch"
            )
        return _err(
            op_norm,
            exc.code,
            exc.msg,
            ep=ep_name,
            # The caller's path is what the failure is attributed to; the node
            # the op happened to be at (a put's parent dir, a temp file) is
            # kept beside it instead of replacing it. Only an omitted or empty
            # caller path defers to the node.
            path=path if not _omitted_or_empty(path) else exc.details.get("path"),
            cwd=cwd,
            hint=hint,
            extra=_dead_link_fields(transport) | _node_path_field(exc, path),
        )
    except TransportError as exc:
        retire_refused_link(transport, exc)
        return _err(
            op_norm,
            exc.code or "FS_ERROR",
            exc.msg,
            ep=ep_name,
            path=path,
            cwd=cwd,
            extra=_dead_link_fields(transport),
        )
    except Exception as exc:  # noqa: BLE001
        # Not a link verdict by itself: the transport decides, and only a
        # recorded death raises the tokens (a remote script failure must not).
        return _err(
            op_norm,
            "FS_ERROR",
            _short(f"{type(exc).__name__}: {exc}"),
            ep=ep_name,
            path=path,
            cwd=cwd,
            extra=_dead_link_fields(transport),
        )
    ms = int((time.monotonic() - t0) * 1000)

    return _ok_result(
        op_norm,
        result,
        ep=ep_name,
        cwd=cwd,
        via=backend.via if include_via else None,
        ms=ms,
        recursive=bool(recursive) if op_norm in {"list", "rm"} else None,
    )


def _omitted_or_empty(value: str | None) -> bool:
    """True only for a value that was omitted or is the empty string.

    Whitespace is part of a name: a whitespace-only path is the caller's own
    string, resolved (or refused) by the backend that owns the name, never
    dropped by a ``strip`` on the way there.
    """
    return value is None or not str(value)


def _validate_args(
    op: str,
    *,
    path: str | None,
    content: str | None,
    local: str | None,
    ep: str | None,
) -> OpResult | None:
    needs_path = op in {"list", "stat", "read", "write", "put", "get", "mkdir", "rm"}
    # Only omission and the empty string are a missing argument: whitespace is
    # part of a name, so a whitespace-only path is the backend's to resolve
    # (its own empty check is the last word on what a path may be).
    if needs_path and _omitted_or_empty(path):
        # put: path is remote destination; get: path is remote source.
        return _err(
            op,
            "MISSING_ARG",
            "path is required",
            ep=ep,
            path=path,
            hint="pass path= (absolute preferred)",
        )
    if op == "write" and content is None:
        return _err(
            op,
            "MISSING_ARG",
            "content is required for write",
            ep=ep,
            path=path,
        )
    if op in {"put", "get"} and (local is None or not str(local).strip()):
        return _err(
            op,
            "MISSING_ARG",
            "local is required for put/get",
            ep=ep,
            path=path,
            hint="pass local= controller-side path",
        )
    return None


def _dispatch(
    backend: FsBackend,
    op: str,
    *,
    path: str | None,
    content: str | None,
    local: str | None,
    recursive: bool,
    max_bytes: int | None,
    progress: ProgressCallback | None = None,
) -> Any:
    assert path is not None
    if op == "list":
        return backend.list(path, recursive=recursive)
    if op == "stat":
        return backend.stat(path)
    if op == "read":
        return backend.read(path, max_bytes=max_bytes)
    if op == "write":
        return backend.write(path, content if content is not None else "")
    if op == "put":
        assert local is not None
        return backend.put(local, path, progress=progress)
    if op == "get":
        assert local is not None
        return backend.get(path, local, progress=progress)
    if op == "mkdir":
        return backend.mkdir(path, parents=True)
    if op == "rm":
        return backend.rm(path, recursive=recursive)
    # Single gate: run() rejects unknown ops via VALID_OPS before _dispatch.
    raise AssertionError(f"unhandled fs op after VALID_OPS gate: {op!r}")


def _image_mime(data: bytes) -> str | None:
    """Return PNG/JPEG/GIF/WebP MIME from magic bytes, else None.

    Header match only; the payload is not decoded. Extension is ignored so a
    ``.png`` name is not enough and a header without a suffix still matches.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ok_result(
    op: str,
    result: Any,
    *,
    ep: str | None,
    cwd: str | None,
    via: str | None,
    ms: int,
    recursive: bool | None,
) -> OpResult:
    fields: dict[str, Any] = {"op": op}
    if ep is not None:
        fields["ep"] = ep
    body: str | None = None
    image_data: bytes | None = None
    out_cwd = cwd
    path_abs: str | None = None

    if op == "list" and isinstance(result, ListResult):
        path_abs = result.path
        fields["path"] = path_abs
        fields["n"] = len(result.entries)
        if result.truncated:
            fields["truncated"] = True
        body = _format_list_body(result)
    elif op == "stat" and isinstance(result, StatInfo):
        path_abs = result.path
        fields["path"] = path_abs
        fields["type"] = result.kind
        fields["bytes"] = result.size
        if result.mode:
            fields["mode"] = result.mode
        if result.mtime:
            fields["mtime"] = result.mtime
        if result.target:
            fields["target"] = result.target
    elif op == "read" and isinstance(result, ReadResult):
        path_abs = result.path
        fields["path"] = path_abs
        fields["bytes"] = len(result.data)
        mime = _image_mime(result.data)
        if mime is not None:
            # Identified images that hit the read budget are an error, not a
            # partial image. Construct the row here: this helper is outside
            # run()'s FsError catch and must not raise.
            if result.truncated:
                return _err(
                    op,
                    "READ_LIMIT_EXCEEDED",
                    "image exceeds read budget; increase max_bytes",
                    ep=ep,
                    path=path_abs,
                    cwd=cwd,
                    hint="increase max_bytes to read the full image",
                    extra={
                        "bytes": len(result.data),
                        "truncated": True,
                        "mime_type": mime,
                    },
                )
            fields["type"] = "image"
            fields["mime_type"] = mime
            image_data = result.data
        else:
            if result.truncated:
                fields["truncated"] = True
            if result.is_text:
                fields["type"] = "text"
                text = result.data.decode(result.encoding or "utf-8", errors="replace")
                if text:
                    fields["lines"] = text.count("\n") + (0 if text.endswith("\n") else 1)
                else:
                    fields["lines"] = 0
                body = text
                if result.encoding:
                    fields["encoding"] = result.encoding
            else:
                fields["type"] = "binary"
                digest = hashlib.sha256(result.data).hexdigest()[:12]
                fields["sha256"] = digest
                body = None
                # Binary content is omitted from the agent body (sha256 only).
                fields["note"] = "content omitted; binary or non-utf8"
    elif op == "write" and isinstance(result, WriteResult):
        path_abs = result.path
        fields["path"] = path_abs
        fields["bytes"] = result.bytes_written
        if not result.created:
            fields["overwritten"] = True
    elif op in {"put", "get"} and isinstance(result, TransferResult):
        path_abs = result.path
        fields["path"] = path_abs
        fields["bytes"] = result.bytes_transferred
        fields["local"] = result.local
    elif op == "mkdir" and isinstance(result, StatInfo):
        path_abs = result.path
        fields["path"] = path_abs
        fields["type"] = "dir"
    elif op == "rm" and isinstance(result, str):
        path_abs = result
        fields["path"] = path_abs
        if recursive:
            fields["recursive"] = True
    else:
        if hasattr(result, "path"):
            path_abs = result.path
            fields["path"] = path_abs

    fields["ms"] = ms
    if via:
        fields["via"] = via

    # Prefer the listed absolute path as cwd when the endpoint has none.
    if out_cwd is None and path_abs and op == "list":
        out_cwd = path_abs

    return OpResult(
        kind="fs",
        status="ok",
        cwd=out_cwd if out_cwd else None,
        fields=fields,
        body=body,
        image_data=image_data,
    )


def _format_list_body(result: ListResult) -> str:
    """Skimmable list body: ``d mode=1777 size=0 name`` per entry."""
    lines: list[str] = []
    for e in result.entries:
        mode = e.mode or "----"
        size = e.size
        lines.append(f"{e.kind} mode={mode} size={size} {e.name}")
    return "\n".join(lines)


def _dead_link_fields(transport: BaseTransport | None) -> dict[str, Any]:
    """Link-death tokens for a failed fs row, or ``{}``.

    The transport owns the verdict (it marks the link dead and records why);
    Core only mirrors the tokens so an Agent branches on fields instead of
    parsing pypsrp's English - the same contract ps rows carry
    (``ps_ops._link_lost_fields``). A healthy transport and an ordinary remote
    error gain no key.
    """
    meta = getattr(transport, "meta", None)
    if not isinstance(meta, dict):
        return {}
    link_lost = meta.get("link_lost") is True or (
        str(meta.get("dead_reason") or "").strip() == _LINK_LOST_REASON
    )
    if not link_lost:
        return {}
    out: dict[str, Any] = {"link_lost": 1}
    if meta.get("marked_dead") is True:
        out["marked_dead"] = True
    reopen_hint = meta.get("reopen_hint")
    out["reopen_hint"] = (
        str(reopen_hint).strip()
        if reopen_hint and str(reopen_hint).strip()
        else _REOPEN_HINT
    )
    return out


def _node_path_field(exc: FsError, path: str | None) -> dict[str, Any]:
    """``node_path`` when the backend failed at a node other than the caller's.

    A multi-round-trip op fails at whichever node it had reached - a put's
    parent-dir stat inside ``mkdir_p``, a temp file, a resolved link. Without
    this the row's ``path`` (the caller's destination/source) is the only
    attribution an Agent gets, and a failed ``put`` could not even be tied to
    the file it was asked to write. The comparison is exact: spellings that
    differ only in whitespace are different names, so the node is reported
    rather than folded into the caller's string.
    """
    node = exc.details.get("path")
    if node is None or not str(node).strip():
        return {}
    node_text = str(node)
    if path is not None and str(path) == node_text:
        return {}
    return {_NODE_PATH_FIELD: node_text}


def _connect_failure_fields(exc: TransportError) -> dict[str, Any]:
    """Classified connect-failure tokens from a lazy-connect ``TransportError``.

    Delegates to the registry so the fs row's vocabulary stays in step with
    every other surface that reports the same failure.
    """
    return connect_failure_fields(exc)


def _dead_transport_msg(transport: BaseTransport | None) -> str:
    """``msg`` for an op pre-checked against an already-dead transport.

    Prefer the transport's own recorded reason ("link lost", "peer_reset", ...)
    over generic text: it names why the link is gone and matches the
    ``dead_reason=`` token endpoint list/open report (ps ``_dead_transport_msg``).
    """
    meta = getattr(transport, "meta", None)
    if isinstance(meta, dict):
        reason = meta.get("dead_reason")
        if reason is not None and str(reason).strip():
            return _short(str(reason))
    return "endpoint transport not connected"


def _err(
    op: str,
    code: str,
    msg: str,
    *,
    ep: str | None = None,
    path: str | None = None,
    cwd: str | None = None,
    hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> OpResult:
    fields: dict[str, Any] = {"op": op, "msg": msg}
    if ep is not None:
        fields["ep"] = ep
    if not _omitted_or_empty(path):
        fields["path"] = path
    if extra:
        fields.update(extra)
    return OpResult(
        kind="fs",
        status="error",
        code=code,
        cwd=cwd,
        fields=fields,
        hint=hint,
    )


