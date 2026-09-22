"""Local process-backed transport (always-on, no network).

Runs shell commands and argv lists via ``subprocess`` on the host that
owns the MRC process. On timeout, kills the whole process tree
(``killpg`` on POSIX / ``taskkill /T`` on Windows) so compound commands
do not leave orphan grandchildren.

Children never inherit this process's stdin: the MCP JSON-RPC stdio
stream may be attached to it, so a command that reads stdin would steal
request bytes and could deadlock. Both exec forms get ``DEVNULL``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp_remote_control.codec import decode_auto
from mcp_remote_control.codec.text_codec import DecodeResult
from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError

_log = logging.getLogger(__name__)


def _controller_encoding() -> str | None:
    """The controller's own text codec (locale charmap), or None if unknown.

    A local child writes to a pipe in the encoding of the console/locale it
    inherited - the same source ``endpoint open`` reports as ``text_encoding``.
    Using it as the decode's preferred codec is what keeps a non-UTF-8 child
    readable; decoding it as UTF-8-first still protects UTF-8 output.
    """
    try:
        import locale

        return locale.getpreferredencoding(False) or None
    except Exception:  # noqa: BLE001 - a broken locale must not break exec
        return None


def _reset_decode_record(transport: Any) -> None:
    """Start a new command's decode record, dropping the previous one.

    ``last_decode`` describes the streams of *one* command. A command that
    decodes no bytes at all - empty output, or a failure before the child ran -
    must not leave the previous command's codec in place: a reader (or a debug
    dump) would otherwise attribute that codec to text this command never
    produced.
    """
    if transport is not None:
        transport.last_decode = None


def _note_decode(
    transport: Any,
    result: DecodeResult,
    preferred: str | None,
    value: Any,
) -> None:
    """Record a stream's decode decision on *transport*, and warn once per kind.

    A fallback decode is not an error - a child that really writes gbk is
    decoded correctly through its leg - but the reader can no longer assume the
    text is right, and nothing else in the exec path says which codec produced
    it. The same goes for an ambiguous one (both the configured codec and
    UTF-8 accept the bytes): the text may be a plausible reading of the wrong
    codec. The WARNING is emitted once per (transport, codec, preferred) so a
    legacy child does not log a line per command; later occurrences stay at
    DEBUG and remain visible in ``transport.last_decode``.
    """
    if transport is None:
        return
    # A stream with no bytes carries no encoding evidence. Keep the previous
    # decision (normally the stdout read) instead of overwriting it with the
    # empty stderr read that runs last.
    if value is None or value == b"" or value == "":
        return
    transport.last_decode = {
        "encoding": result.encoding_used,
        "preferred": preferred,
        "replaced": result.replaced,
        "errors": result.errors,
        "fallback": result.fallback,
        "ambiguous": result.ambiguous,
    }
    if not (result.fallback or result.ambiguous):
        return
    size = len(value) if isinstance(value, (bytes, bytearray, memoryview)) else 0
    kind = "fallback" if result.fallback else "ambiguous"
    key = f"{result.encoding_used}|{preferred or ''}|{result.replaced}|{kind}"
    if key in transport._decode_warned:
        _log.debug(
            "text decode %s to %s (%d bytes, preferred=%s, replaced=%s, errors=%d)",
            kind,
            result.encoding_used,
            size,
            preferred,
            result.replaced,
            result.errors,
        )
        return
    transport._decode_warned.add(key)
    if result.fallback:
        _log.warning(
            "text decode fell back to %s (%d bytes, preferred=%s, replaced=%s, errors=%d): "
            "the bytes were not valid UTF-8, so the text may be mis-decoded",
            result.encoding_used,
            size,
            preferred,
            result.replaced,
            result.errors,
        )
        return
    _log.warning(
        "text decode is ambiguous: %s accepted the %d bytes as well as utf-8 "
        "(preferred=%s): the text was read as utf-8 but the configured codec "
        "would read it differently",
        result.encoding_used,
        size,
        preferred,
    )


def _decode(
    data: bytes | str | None,
    preferred: str | None = None,
    *,
    transport: Any = None,
) -> str:
    """Decode child output, noting a non-UTF-8 read on *transport*."""
    result = decode_auto(data, preferred=preferred)
    _note_decode(transport, result, preferred, data)
    return result.text


def _resolve_local_cwd(cwd: str | None) -> str:
    """Expand ~ and return an absolute existing directory path.

    Idempotent: an already-absolute existing directory is returned as-is so
    that ``exec_ops._resolve_cwd`` does not trigger a second redundant
    ``resolve()`` syscall when it hands us an already-resolved path.

    Returns the *lexical* absolute path via ``os.path.abspath`` - does NOT
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

    def __init__(self, *, text_encoding: str | None = None) -> None:
        super().__init__()
        # Codec for child output. An explicit profile value wins; otherwise the
        # controller's own locale encoding, which is what a child inheriting
        # this process's console actually writes. Without it a non-gb18030
        # controller (big5, cp932, latin-1) silently mis-reads its children.
        self.text_encoding = text_encoding or _controller_encoding()
        # Decode bookkeeping for the current command's streams. Reset at the
        # start of every command (see _reset_decode_record / _note_decode), so
        # a command that decoded no bytes leaves no stale codec behind.
        self.last_decode: dict[str, Any] | None = None
        self._decode_warned: set[str] = set()

    def _decode(self, data: bytes | str | None) -> str:
        """Decode one child stream with this transport's codec policy."""
        return _decode(data, self.text_encoding, transport=self)

    def connect(self) -> None:
        # No network; capture seeds for endpoint meta / default cwd.
        self.cwd = os.getcwd()
        try:
            self.home = str(Path.home())
        except (RuntimeError, OSError):
            self.home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        # On Windows, seed shell family / dialect so runtime=auto does not
        # hardcode bash (local script form would fail with "bash not found").
        # POSIX local leaves family unset -> platform_default_runtime -> bash.
        self.meta = {
            "cwd": self.cwd,
            "home": self.home,
        }
        if sys.platform.startswith("win"):
            # Prefer PowerShell for modern Windows local hosts. COMSPEC may
            # still point at cmd.exe; pwsh is the safer body-script runtime.
            self.remote_shell_family = "powershell"
            self.meta["shell_family"] = "powershell"
            self.meta["dialect"] = "powershell"
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
        _reset_decode_record(self)
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
        _reset_decode_record(self)
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
            # The exec surface never feeds a child's stdin, and this process's
            # own fd 0 may be the MCP JSON-RPC stdio stream. An inheriting
            # child would consume request bytes and, when it reads to EOF with
            # no exec timeout, deadlock client, server and child. Detach by
            # default so a sibling `ssh`/`cat`/bare interpreter gets EOF.
            "stdin": subprocess.DEVNULL,
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
                stdout=self._decode(out_b),
                stderr=self._decode(err_b),
                cwd=cwd,
                timed_out=True,
            )
        except Exception:
            # Non-timeout failure after Popen (e.g. ValueError when
            # timeout_s is NaN/Inf, pipe OSError). Kill the tree and close
            # pipes so the child cannot outlive this call as an orphan.
            # TimeoutExpired is handled above and returns - no double-kill.
            self._kill_process_tree(proc)
            self._close_proc_pipes(proc)
            try:
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
            raise

        return ExecResult(
            exit_code=int(proc.returncode),
            stdout=self._decode(out_b),
            stderr=self._decode(err_b),
            cwd=cwd,
            timed_out=False,
        )

    @staticmethod
    def _close_proc_pipes(proc: subprocess.Popen) -> None:
        """Best-effort close of stdin/stdout/stderr after a failed run."""
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """Kill the process group (POSIX) or whole tree (Windows).

        On POSIX, ``start_new_session=True`` puts the shell in a fresh
        process group so ``killpg(SIGKILL)`` reaches grandchildren too.

        On Windows there is no process-group analogue, and ``proc.kill()``
        only kills the immediate child shell - grandchildren survive. We
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
                    # Same stdin rule as _run: never hand a child the MCP
                    # stdio stream.
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
