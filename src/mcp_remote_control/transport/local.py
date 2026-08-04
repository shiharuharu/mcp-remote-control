"""Local process-backed transport (always-on, no network).

Runs shell commands and argv lists via ``subprocess`` on the host that
owns the MRC process. On timeout, kills the whole process tree
(``killpg`` on POSIX / ``taskkill /T`` on Windows) so compound commands
do not leave orphan grandchildren.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError


def _decode(data: bytes | str | None) -> str:
    from mcp_remote_control.codec import decode_to_str

    return decode_to_str(data)


def _resolve_local_cwd(cwd: str | None) -> str:
    """Expand ~ and return an absolute existing directory path.

    Idempotent: an already-absolute existing directory is returned as-is so
    that ``exec_ops._resolve_cwd`` does not trigger a second redundant
    ``resolve()`` syscall when it hands us an already-resolved path.

    Returns the *lexical* absolute path via ``os.path.abspath`` — does NOT
    follow symlinks (``Path.resolve``). Both fast and slow paths use the
    same form so handing the result back through ``run_command`` is a no-op.
    Callers that need the symlink-resolved form (e.g. for display) can
    call ``Path.resolve()`` themselves.
    """
    if cwd is None or not str(cwd).strip():
        return os.getcwd()
    text = str(cwd).strip()
    p = Path(text)
    # Fast path: caller already resolved it; skip redundant syscall work.
    # os.path.abspath is a lexical no-op on a clean absolute path but
    # normalizes stray "."/".." components without following symlinks.
    if p.is_absolute() and p.is_dir():
        return os.path.abspath(str(p))
    p = p.expanduser()
    # Slow path: still lexical (no Path.resolve symlink-following) so the
    # fast and slow paths return a consistent form. is_dir() below still
    # follows symlinks for the existence check.
    p = Path(os.path.abspath(str(p)))
    if not p.is_dir():
        raise TransportError(
            "INVALID_CWD",
            f"cwd does not exist or is not a directory: {p}",
            details={"cwd": str(p)},
        )
    return str(p)


def _merge_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = dict(os.environ)
    merged.update({str(k): str(v) for k, v in env.items()})
    return merged


class LocalTransport(BaseTransport):
    """Local machine: connect is a lightweight probe of home/cwd.

    ``run_command`` uses ``shell=True`` intentionally for remote-admin parity
    with SSH shell commands. Prefer ``run_argv`` when shell features are not
    required.
    """

    name = "local"

    def connect(self) -> None:
        # No network; capture seeds for endpoint meta / default cwd.
        self.cwd = os.getcwd()
        try:
            self.home = str(Path.home())
        except (RuntimeError, OSError):
            self.home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        self.meta = {
            "cwd": self.cwd,
            "home": self.home,
        }
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if command is None:
            raise TransportError("INVALID_ARG", "command is required")
        work = _resolve_local_cwd(cwd if cwd is not None else self.cwd)
        return self._run(
            args=command,
            shell=True,
            cwd=work,
            timeout_s=timeout_s,
            env=env,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if not argv:
            raise TransportError("INVALID_ARG", "argv is empty")
        work = _resolve_local_cwd(cwd if cwd is not None else self.cwd)
        return self._run(
            args=[str(a) for a in argv],
            shell=False,
            cwd=work,
            timeout_s=timeout_s,
            env=env,
        )

    def _run(
        self,
        *,
        args: str | list[str],
        shell: bool,
        cwd: str,
        timeout_s: float | None,
        env: dict[str, str] | None,
    ) -> ExecResult:
        run_env = _merge_env(env)
        popen_kwargs: dict[str, Any] = {
            "shell": shell,
            "cwd": cwd,
            "env": run_env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "posix":
            # New session / process group so a timeout can kill the whole
            # tree. Without this, compound commands like `sleep 3600; echo`
            # reparent surviving children to init and leave them running.
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **popen_kwargs)
        except FileNotFoundError as exc:
            raise TransportError(
                "EXEC_FAILED",
                f"executable not found: {exc.filename or args!r}",
                details={"cwd": cwd},
            ) from exc
        except OSError as exc:
            raise TransportError(
                "EXEC_FAILED",
                f"os error: {exc}",
                details={"cwd": cwd},
            ) from exc

        try:
            out_b, err_b = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Kill the whole process group (POSIX) so grandchildren are
            # reaped too, then collect whatever output was buffered.
            self._kill_process_tree(proc)
            try:
                out_b, err_b = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                out_b, err_b = b"", b""
            return ExecResult(
                exit_code=-1,
                stdout=_decode(out_b),
                stderr=_decode(err_b),
                cwd=cwd,
                timed_out=True,
            )

        return ExecResult(
            exit_code=int(proc.returncode),
            stdout=_decode(out_b),
            stderr=_decode(err_b),
            cwd=cwd,
            timed_out=False,
        )

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """Kill the process group (POSIX) or whole tree (Windows).

        On POSIX, ``start_new_session=True`` puts the shell in a fresh
        process group so ``killpg(SIGKILL)`` reaches grandchildren too.

        On Windows there is no process-group analogue, and ``proc.kill()``
        only kills the immediate child shell — grandchildren survive. We
        therefore shell out to ``taskkill /F /T /PID`` which walks the
        whole process tree. The call is best-effort: if ``taskkill`` itself
        cannot be invoked (stripped Windows, missing PATH) we fall back to
        ``proc.kill()`` so at least the shell dies.
        """
        if proc.pid is None:
            return
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            # Windows: taskkill /T walks the process tree and kills all
            # descendants. /F forces termination. Best-effort: fall back to
            # proc.kill() only if taskkill itself can't be invoked.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
