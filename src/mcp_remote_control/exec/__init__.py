"""Exec helpers: script form normalization and runtime → argv mapping.

Builds argv for script body/path forms without shelling the body, and
formats the ``$ …`` echo line used on the Agent track.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError

# Canonical runtime tokens accepted by script form.
RUNTIME_ALIASES: dict[str, str] = {
    "auto": "auto",
    "bash": "bash",
    "sh": "sh",
    "zsh": "zsh",
    "python": "python",
    "python3": "python",
    "py": "python",
    "pwsh": "pwsh",
    "powershell": "powershell",
    "cmd": "cmd",
}


def normalize_runtime(runtime: str | None) -> str:
    """Map free-form runtime string to a canonical token."""
    if runtime is None or not str(runtime).strip():
        return "auto"
    key = str(runtime).strip().lower()
    if key in RUNTIME_ALIASES:
        return RUNTIME_ALIASES[key]
    # Absolute interpreter paths pass through; alias-check basename only.
    base = Path(key).name.lower()
    if base in RUNTIME_ALIASES:
        return RUNTIME_ALIASES[base]
    return key


def _python_exe() -> str:
    return sys.executable or "python3"


def resolve_script_path_runtime(path: str, runtime: str) -> str:
    """Pick runtime for a script path when runtime=auto."""
    if runtime != "auto":
        return runtime
    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".pyw"}:
        return "python"
    if suffix in {".ps1"}:
        return "pwsh"
    if suffix in {".cmd", ".bat"}:
        return "cmd"
    if suffix in {".sh", ".bash"}:
        return "bash"
    return "bash"


def resolve_body_runtime(
    runtime: str,
    *,
    dialect: str | None = None,
) -> str:
    """Map runtime token; ``auto`` follows shell dialect (busybox → sh)."""
    if runtime == "auto":
        if dialect:
            from mcp_remote_control.shell.dialect import default_runtime_for_dialect

            return default_runtime_for_dialect(dialect)
        return "bash"
    return runtime


def build_script_argv(
    *,
    body: str | None = None,
    path: str | None = None,
    runtime: str | None = None,
    args: list[str] | None = None,
    dialect: str | None = None,
) -> list[str]:
    """Build argv for a script form without using a shell for the body.

    body:
        Interpreted via ``bash -c`` / ``python -c`` / etc.
    path:
        Invoked as ``runtime path args…`` (path is on the *target* host).
    dialect:
        When runtime is auto, selects sh vs bash for body scripts.
    """
    if body is not None and path is not None:
        raise TransportError(
            "INVALID_ARG",
            "script body and path are mutually exclusive",
        )
    if body is None and path is None:
        raise TransportError(
            "INVALID_ARG",
            "script requires body or path",
        )

    rt = normalize_runtime(runtime)
    extra = [str(a) for a in (args or [])]

    if path is not None:
        path_s = str(path)
        rt = resolve_script_path_runtime(path_s, rt)
        return _argv_for_path(rt, path_s, extra)

    body_s = body if body is not None else ""
    rt = resolve_body_runtime(rt, dialect=dialect)
    return _argv_for_body(rt, body_s, extra)


def _argv_for_path(runtime: str, path: str, args: list[str]) -> list[str]:
    if runtime == "python":
        return [_python_exe(), path, *args]
    if runtime == "bash":
        return ["bash", path, *args]
    if runtime == "sh":
        return ["sh", path, *args]
    if runtime == "zsh":
        return ["zsh", path, *args]
    if runtime == "pwsh":
        return [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            path,
            *args,
        ]
    if runtime == "powershell":
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            path,
            *args,
        ]
    if runtime == "cmd":
        return ["cmd", "/c", path, *args]
    # Treat runtime as an interpreter executable path/name.
    return [runtime, path, *args]


def _argv_for_body(runtime: str, body: str, args: list[str]) -> list[str]:
    if runtime == "python":
        # python -c code [args…] — args land in sys.argv after the '-c' slot.
        return [_python_exe(), "-c", body, *args]
    if runtime == "bash":
        # bash -c 'body' name arg1… → $0=name, $1=arg1
        return ["bash", "-c", body, "bash", *args]
    if runtime == "sh":
        return ["sh", "-c", body, "sh", *args]
    if runtime == "zsh":
        return ["zsh", "-c", body, "zsh", *args]
    if runtime == "pwsh":
        return [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            body,
            *args,
        ]
    if runtime == "powershell":
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            body,
            *args,
        ]
    if runtime == "cmd":
        return ["cmd", "/c", body, *args]
    return [runtime, "-c", body, *args]


def run_script_on_transport(
    transport: BaseTransport,
    *,
    body: str | None = None,
    path: str | None = None,
    runtime: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: float | None = None,
    env: dict[str, str] | None = None,
    dialect: str | None = None,
) -> ExecResult:
    """Execute script form via ``transport.run_argv``.

    ``path`` is interpreted by the *target* host: local filesystem for the
    local transport, remote path for SSH. When the script content lives on
    the controller, pass ``body`` instead — there is no automatic
    local-file upload to the remote host.

    dialect:
        Endpoint shell dialect for ``runtime=auto`` (busybox → ``sh -c``).
    """
    eff_dialect = dialect
    if not eff_dialect:
        tmeta = getattr(transport, "meta", None) or {}
        if isinstance(tmeta, dict):
            eff_dialect = tmeta.get("dialect")  # type: ignore[assignment]
        rem = getattr(transport, "remote_shell_family", None)
        if not eff_dialect and rem:
            from mcp_remote_control.shell.dialect import resolve_dialect

            eff_dialect = resolve_dialect(shell_family=str(rem))

    argv = build_script_argv(
        body=body,
        path=path,
        runtime=runtime,
        args=args,
        dialect=str(eff_dialect) if eff_dialect else None,
    )
    return transport.run_argv(argv, cwd=cwd, timeout_s=timeout_s, env=env)


def format_command_echo(
    *,
    form: str,
    command: str | None = None,
    argv: list[str] | None = None,
    script_summary: str | None = None,
) -> str:
    """Single-line ``$ …`` echo for the Agent-track body."""
    if form == "command" and command is not None:
        return f"$ {command}"
    if form == "argv" and argv is not None:
        return f"$ {shlex.join(str(a) for a in argv)}"
    if form == "script":
        return f"$ script {script_summary or ''}".rstrip()
    return f"$ {form}"


def script_summary(
    *,
    body: str | None,
    path: str | None,
    runtime: str | None,
    args: list[str] | None,
) -> str:
    rt = normalize_runtime(runtime)
    parts = [f"runtime={rt}"]
    if path:
        parts.append(f"path={path}")
    elif body is not None:
        one = " ".join(body.strip().split())
        if len(one) > 60:
            one = one[:57] + "..."
        parts.append(f"body={one!r}" if one else "body=")
    if args:
        parts.append(f"args={len(args)}")
    return " ".join(parts)


__all__ = [
    "RUNTIME_ALIASES",
    "build_script_argv",
    "format_command_echo",
    "normalize_runtime",
    "run_script_on_transport",
    "script_summary",
]
