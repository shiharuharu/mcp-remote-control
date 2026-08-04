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

from mcp_remote_control.config import ProfileInvalid, ProfileNotFound, resolve_home
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.registry import ensure_endpoint
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
from mcp_remote_control.transport.ssh import SSHConnector

VALID_OPS: frozenset[str] = frozenset(
    {"list", "stat", "read", "write", "put", "get", "mkdir", "rm"}
)

__all__ = ["VALID_OPS", "run"]


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
            return _err(
                op_norm,
                exc.code or "CONNECT_FAILED",
                exc.msg,
                ep=ep_name,
                path=path,
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
            return _err(
                op_norm,
                "NOT_CONNECTED",
                "endpoint transport not connected",
                ep=ep_name,
                path=path,
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
            )
        cwd = endpoint.cwd or transport.cwd
    else:
        ep_name = str(ep).strip() if ep else None
        cwd = getattr(backend, "_cwd", None)

    arg_err = _validate_args(
        op_norm,
        path=path,
        content=content,
        local=local,
        ep=ep_name,
    )
    if arg_err is not None:
        return arg_err

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
            path=exc.details.get("path") or path,
            cwd=cwd,
            hint=hint,
        )
    except TransportError as exc:
        return _err(
            op_norm,
            exc.code or "FS_ERROR",
            exc.msg,
            ep=ep_name,
            path=path,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(
            op_norm,
            "FS_ERROR",
            _short(f"{type(exc).__name__}: {exc}"),
            ep=ep_name,
            path=path,
            cwd=cwd,
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


def _validate_args(
    op: str,
    *,
    path: str | None,
    content: str | None,
    local: str | None,
    ep: str | None,
) -> OpResult | None:
    needs_path = op in {"list", "stat", "read", "write", "put", "get", "mkdir", "rm"}
    if needs_path and (path is None or not str(path).strip()):
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
    raise FsError("INVALID_OP", f"unknown fs op: {op}")


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
    )


def _format_list_body(result: ListResult) -> str:
    """Skimmable list body: ``d mode=1777 size=0 name`` per entry."""
    lines: list[str] = []
    for e in result.entries:
        mode = e.mode or "----"
        size = e.size
        lines.append(f"{e.kind} mode={mode} size={size} {e.name}")
    return "\n".join(lines)


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
    if path is not None and str(path).strip():
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


def _home(home: Path | str | None) -> Path:
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()


def _short(msg: str, limit: int = 200) -> str:
    text = " ".join(str(msg).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
