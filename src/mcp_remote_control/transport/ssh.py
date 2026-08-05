"""SSH transport via asyncssh on a permanent asyncio loop bridge.

All awaitable I/O (connect, exec, SFTP, close) shares one process-wide
:class:`~mcp_remote_control.transport.async_bridge.AsyncLoopBridge` so
connection-bound objects stay valid across calls. A wall-clock bridge
deadline sits on top of asyncssh's own timeouts so hung DNS or stalled
cancels cannot block the sync caller indefinitely; a bridge timeout may
mark the session dead for reconnect.

An injectable *connector* factory and optional per-instance bridge support
alternate connection implementations (including lightweight fakes) without
importing asyncssh at module load.
"""

from __future__ import annotations

import inspect
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mcp_remote_control.codec import decode_to_str
from mcp_remote_control.codec.text_codec import charmap_to_codec, codepage_to_codec
from mcp_remote_control.transport.async_bridge import AsyncLoopBridge, run_coro
from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError
from mcp_remote_control.transport.shell_wrap import (
    POSIX_PROBE_SCRIPT,
    POWERSHELL_PROBE_SCRIPT,
    WINDOWS_PROBE_SCRIPT,
    coerce_cwd_path,
    normalize_shell_family,
    parse_probe_output,
    wrap_with_cwd,
)

# Connector: kwargs → connection handle (sync object or awaitable).
SSHConnector = Callable[..., Any]

# Grace added on top of asyncssh's internal timeout when forwarding a
# belt-and-suspenders deadline to AsyncLoopBridge.run. Keeps the sync caller
# from blocking indefinitely if asyncssh's own timeout/cancel stalls (e.g.
# hung DNS before connect_timeout starts).
_BRIDGE_TIMEOUT_GRACE_S = 5.0


def _run_maybe_async(
    result: Any,
    *,
    timeout_s: float | None = None,
    bridge: AsyncLoopBridge | None = None,
) -> Any:
    """If *result* is a coroutine, run it on the async bridge; else return as-is.

    All asyncssh I/O must share one permanent loop so connection-bound
    objects stay valid across connect / run / sftp / pty.
    """
    return run_coro(result, timeout_s=timeout_s, bridge=bridge)


def _decode_stream(value: Any, preferred: str | None = None) -> str:
    return decode_to_str(value, preferred=preferred)


def _wrap_with_cwd(
    command: str,
    cwd: str | None,
    *,
    shell_family: str | None = "posix",
) -> str:
    return wrap_with_cwd(command, cwd, shell_family=shell_family)


async def _default_asyncssh_connect(
    *,
    host: str,
    port: int,
    username: str,
    client_keys: Sequence[Path | str] | None,
    connect_timeout: float,
    known_hosts: Any,
    password: str | None = None,
    passphrase: str | None = None,
    keepalive_interval: float | None = None,
) -> Any:
    """Real asyncssh connect (lazy import so the module loads without network deps)."""
    import asyncssh  # local import keeps module import cheap

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": known_hosts,
        "connect_timeout": connect_timeout,
    }
    if client_keys:
        # Paths only — asyncssh loads contents itself.
        kwargs["client_keys"] = [str(p) for p in client_keys]
    if password is not None:
        kwargs["password"] = password
    if passphrase is not None:
        kwargs["passphrase"] = passphrase
    if keepalive_interval is not None:
        try:
            kwargs["keepalive_interval"] = float(keepalive_interval)
        except (TypeError, ValueError):
            pass

    return await asyncssh.connect(**kwargs)


def default_ssh_connector(**kwargs: Any) -> Any:
    """Default connector: return the asyncssh connect coroutine (unrun).

    SSHTransport.connect drives the coroutine on the loop bridge with a
    wall-clock deadline (connect_timeout + grace). The connector must not
    pre-run the coroutine, or that bridge timeout would never apply.
    """
    return _default_asyncssh_connect(**kwargs)


class SSHTransport(BaseTransport):
    """SSH session handle over asyncssh (or a connector-supplied substitute).

    Parameters
    ----------
    connector:
        Injectable factory. Signature matches ``default_ssh_connector`` /
        ``_default_asyncssh_connect`` kwargs. May return a sync object or a
        coroutine.
    bridge:
        Optional :class:`AsyncLoopBridge`. When omitted, the process-wide
        shared bridge is used for all awaitable I/O.

    When the connection object exposes higher-level helpers they are
    preferred over the asyncssh ``run`` path:

    - ``run_command(command, *, cwd=None, timeout_s=None, env=None) -> ExecResult``
    - ``run_argv(argv, *, cwd=None, timeout_s=None, env=None) -> ExecResult``
    - ``run(command, ...)`` asyncssh-style (exit_status / stdout / stderr)
    """

    name = "ssh"

    def __init__(
        self,
        *,
        host: str,
        port: int = 22,
        username: str,
        client_keys: Sequence[Path | str] | None = None,
        connect_timeout_ms: int = 15000,
        known_hosts: Any = None,
        password: str | None = None,
        passphrase: str | None = None,
        keepalive_interval_s: float | None = None,
        text_encoding: str | None = None,
        remote_shell_family: str = "posix",
        force_utf8_remote: bool = False,
        connector: SSHConnector | None = None,
        bridge: AsyncLoopBridge | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = int(port)
        self.username = username
        self.client_keys = list(client_keys) if client_keys else []
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.known_hosts = known_hosts
        self.password = password
        self.passphrase = passphrase
        self.keepalive_interval_s = keepalive_interval_s
        self.text_encoding = text_encoding
        self.remote_shell_family = normalize_shell_family(remote_shell_family)
        self.force_utf8_remote = bool(force_utf8_remote)
        self._connector: SSHConnector = connector or default_ssh_connector
        self._bridge = bridge
        self._conn: Any = None
        self._sftp: Any = None

    def _await(self, result: Any, *, timeout_s: float | None = None) -> Any:
        return _run_maybe_async(result, timeout_s=timeout_s, bridge=self._bridge)

    def connect(self) -> None:
        if self._connected and self._conn is not None:
            return
        timeout_s = max(self.connect_timeout_ms, 1) / 1000.0
        # Belt-and-suspenders bridge deadline: asyncssh's connect_timeout
        # only starts after DNS resolution. Add grace so a hung DNS / stalled
        # cancel cannot block the sync caller indefinitely.
        bridge_timeout = timeout_s + _BRIDGE_TIMEOUT_GRACE_S
        try:
            conn = self._await(
                self._connector(
                    host=self.host,
                    port=self.port,
                    username=self.username,
                    client_keys=self.client_keys or None,
                    connect_timeout=timeout_s,
                    known_hosts=self.known_hosts,
                    password=self.password,
                    passphrase=self.passphrase,
                    keepalive_interval=self.keepalive_interval_s,
                ),
                timeout_s=bridge_timeout,
            )
        except TransportError:
            raise
        except Exception as exc:
            raise _map_ssh_connect_error(
                exc, host=self.host, port=self.port
            ) from exc

        self._conn = conn
        self._connected = True
        # Optional seeds when the connection object already carries them.
        self.cwd = getattr(conn, "cwd", None)
        self.home = getattr(conn, "home", None)
        self.meta = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
        }

    def close(self) -> None:
        sftp = self._sftp
        self._sftp = None
        if sftp is not None:
            try:
                close_sftp = getattr(sftp, "exit", None) or getattr(sftp, "close", None)
                if callable(close_sftp):
                    self._await(close_sftp())
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        conn = self._conn
        self._conn = None
        self._connected = False
        if conn is None:
            return
        try:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
            wait = getattr(conn, "wait_closed", None)
            if callable(wait):
                self._await(wait())
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass

    @property
    def connection(self) -> Any:
        """Underlying asyncssh connection handle; None if closed."""
        return self._conn

    def open_sftp(self) -> Any:
        """Lazy-open SFTP client from the SSH connection.

        Tries ``start_sftp_client`` / ``open_sftp`` on the connection, then
        an already-attached ``sftp`` attribute. Raises ``UNSUPPORTED`` when
        none of those are available.
        """
        if self._sftp is not None:
            return self._sftp
        conn = self._require_conn()

        for name in ("start_sftp_client", "open_sftp"):
            fn = getattr(conn, name, None)
            if callable(fn):
                try:
                    client = self._await(fn())
                except TransportError:
                    raise
                except Exception as exc:
                    raise TransportError(
                        "SFTP_FAILED",
                        _safe_connect_msg(exc),
                        details={"host": self.host},
                    ) from exc
                self._sftp = client
                return client

        existing = getattr(conn, "sftp", None)
        if existing is not None:
            self._sftp = existing
            return existing

        raise TransportError(
            "UNSUPPORTED",
            "ssh connection has no SFTP client factory",
            details={"host": self.host},
        )

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        conn = self._require_conn()
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)

        # Prefer an explicit run_command helper when the connection exposes one.
        runner = getattr(conn, "run_command", None)
        if callable(runner):
            result = self._await(
                runner(command, cwd=work, timeout_s=timeout_s, env=env)
            )
            return _coerce_exec_result(
                result, default_cwd=work, preferred=self.text_encoding
            )

        full = _wrap_with_cwd(
            command, work, shell_family=self.remote_shell_family
        )
        full = _maybe_force_utf8(full, self)
        try:
            return self._run_shell_on_conn(
                conn, full, cwd=work, timeout_s=timeout_s, env=env
            )
        except TransportError as exc:
            if exc.code in ("EXEC_FAILED", "NOT_CONNECTED") and not self.is_alive():
                self.mark_dead(exc.msg)
            raise

    def collect_probe(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """Best-effort remote shell/OS/encoding probe (never raises).

        Order: POSIX → PowerShell (Windows OpenSSH default) → cmd /c.
        A failed probe must not leave a dead session usable by exec: if the
        peer closed mid-probe we reconnect once before returning.
        """
        out: dict[str, Any] = {"status": "ok"}
        parsed: dict[str, Any] = {}

        def _ensure_live() -> bool:
            if self.is_connected():
                return True
            try:
                self.connect()
                return self.is_connected()
            except Exception:  # noqa: BLE001
                return False

        def _try_script(script: str) -> dict[str, Any]:
            if not _ensure_live():
                return {}
            try:
                result = self.run_command(script, timeout_s=timeout_s)
            except Exception:  # noqa: BLE001
                if not self.is_alive():
                    self.mark_dead("probe exec failed")
                return {}
            # Soft-fail scripts (exit -1 / empty) may still close OpenSSH.
            if not self.is_alive():
                self.mark_dead("probe closed ssh peer")
                return {}
            text = (result.stdout or "") + "\n" + (result.stderr or "")
            return parse_probe_output(text)

        try:
            parsed = _try_script(POSIX_PROBE_SCRIPT)
            uname_val = str(parsed.get("uname") or "")
            # cmd.exe may echo `uname=$(uname …)` literally — not a real uname.
            echoed_posix = (
                "$(" in uname_val
                or "2>/dev" in uname_val
                or "uname -" in uname_val
                or uname_val.strip() in {"", "-", "--"}
            )
            credible_posix = bool(
                uname_val
                and parsed.get("os") != "windows"
                and not echoed_posix
            )
            # Windows OpenSSH often defaults to PowerShell: the POSIX script
            # can close the session. Prefer a native PS probe, then cmd /c.
            if not credible_posix and not _looks_credibly_windows(parsed):
                parsed_ps = _try_script(POWERSHELL_PROBE_SCRIPT)
                if _looks_credibly_windows(parsed_ps) or parsed_ps.get(
                    "shell_base"
                ) in ("powershell", "pwsh"):
                    parsed = parsed_ps
                else:
                    parsed_w = _try_script(WINDOWS_PROBE_SCRIPT)
                    if _looks_credibly_windows(parsed_w):
                        parsed = parsed_w
                    elif not parsed:
                        out["status"] = "partial"
            elif parsed.get("os") != "windows" and not credible_posix:
                parsed_ps = _try_script(POWERSHELL_PROBE_SCRIPT)
                if _looks_credibly_windows(parsed_ps):
                    parsed = parsed_ps
                else:
                    parsed_w = _try_script(WINDOWS_PROBE_SCRIPT)
                    if _looks_credibly_windows(parsed_w):
                        parsed = parsed_w
                    elif not parsed:
                        out["status"] = "partial"

            out.update({k: v for k, v in parsed.items() if v is not None})

            # Shell family for cwd wrap.
            if out.get("os") == "windows" or out.get("shell_base") in (
                "cmd",
                "powershell",
                "pwsh",
            ):
                fam = normalize_shell_family(
                    str(out.get("shell_base") or "cmd")
                )
                self.remote_shell_family = fam
                out["shell_family"] = fam
            else:
                self.remote_shell_family = "posix"
                out["shell_family"] = "posix"

            # Encoding preference.
            enc = None
            if out.get("chcp") is not None:
                enc = codepage_to_codec(out["chcp"])  # type: ignore[arg-type]
            if enc is None and out.get("charmap"):
                enc = charmap_to_codec(str(out["charmap"]))
            if enc:
                self.text_encoding = enc
                out["text_encoding"] = enc

            if out.get("home") and not isinstance(out.get("home"), bool):
                home = str(out["home"]).strip()
                if home and home not in {"True", "False", "None"}:
                    self.home = home
            # Only path-like pwd — never bool cap bleed-through (cwd=True).
            pwd = coerce_cwd_path(out.get("pwd"))
            if pwd:
                self.cwd = pwd

            self.meta = {**(self.meta or {}), **out}
        except Exception as exc:  # noqa: BLE001
            out["status"] = "partial"
            out["error"] = _safe_connect_msg(exc)
        # Probe must not strand callers with a dead transport after open.
        if not self.is_connected():
            try:
                self.connect()
            except Exception as exc:  # noqa: BLE001
                out["status"] = "partial"
                out.setdefault("error", _safe_connect_msg(exc))
        if not parsed and out.get("status") == "ok":
            out["status"] = "partial"
        return out

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
        conn = self._require_conn()
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)

        runner = getattr(conn, "run_argv", None)
        if callable(runner):
            result = self._await(
                runner(list(argv), cwd=work, timeout_s=timeout_s, env=env)
            )
            return _coerce_exec_result(
                result, default_cwd=work, preferred=self.text_encoding
            )

        # Fall back to a shell-quoted remote command (portable across OpenSSH
        # default shells and connection objects without a native argv path).
        fam = self.remote_shell_family
        if fam == "cmd":
            quoted = " ".join(
                f'"{str(a).replace(chr(34), chr(34) + chr(34))}"' for a in argv
            )
        elif fam == "powershell":
            # PowerShell treats adjacent single-quoted strings as SEPARATE
            # string-literal expressions, not a command invocation. The call
            # operator `&` invokes the first string as a command with the
            # rest as arguments: `& 'echo' 'hello'`. Without it every
            # run_argv fallback (and the pwsh -Command script form) is a
            # no-op on a PowerShell-remote SSH host.
            quoted = "& " + " ".join(
                "'" + str(a).replace("'", "''") + "'" for a in argv
            )
        else:
            quoted = " ".join(shlex.quote(str(a)) for a in argv)
        full = _wrap_with_cwd(quoted, work, shell_family=fam)
        full = _maybe_force_utf8(full, self)
        try:
            return self._run_shell_on_conn(
                conn, full, cwd=work, timeout_s=timeout_s, env=env
            )
        except TransportError as exc:
            if exc.code in ("EXEC_FAILED", "NOT_CONNECTED") and not self.is_alive():
                self.mark_dead(exc.msg)
            raise


    def _require_conn(self) -> Any:
        if not self._connected or self._conn is None:
            raise TransportError(
                "NOT_CONNECTED",
                "ssh transport is not connected",
                details={"host": self.host},
            )
        return self._conn

    def is_connected(self) -> bool:
        """True only when flagged connected *and* the SSH socket is alive."""
        if not self._connected or self._conn is None:
            return False
        if not self.is_alive():
            # Peer drop / bad probe left a zombie flag — publish dead state.
            self.mark_dead("ssh peer closed")
            return False
        return True

    def is_alive(self) -> bool:
        """Best-effort liveness of the underlying SSH connection."""
        if not self._connected or self._conn is None:
            return False
        conn = self._conn
        # Common asyncssh (and compatible) liveness signals.
        for attr in ("is_closing", "_closing"):
            flag = getattr(conn, attr, None)
            if callable(flag):
                try:
                    if flag():
                        return False
                except Exception:  # noqa: BLE001
                    return False
            elif flag is True:
                return False
        closed = getattr(conn, "is_closed", None)
        if callable(closed):
            try:
                if closed():
                    return False
            except Exception:  # noqa: BLE001
                return False
        elif closed is True:
            return False
        return True

    def mark_dead(self, reason: str | None = None) -> None:
        """Mark connection dead without raising (stale after disconnect).

        Used after bridge timeouts and failed exec when the socket is no
        longer usable: callers should reconnect rather than reuse ``_conn``.
        SFTP cache is dropped; ``_conn`` is kept for best-effort ``close()``.
        """
        self._connected = False
        if reason:
            self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}
        # Drop SFTP cache; keep _conn for close() best-effort.
        self._sftp = None

    def _run_shell_on_conn(
        self,
        conn: Any,
        command: str,
        *,
        cwd: str | None,
        timeout_s: float | None,
        env: dict[str, str] | None,
    ) -> ExecResult:
        run = getattr(conn, "run", None)
        if not callable(run):
            raise TransportError(
                "UNSUPPORTED",
                "ssh connection has no run/run_command for exec",
                details={"host": self.host},
            )

        kwargs: dict[str, Any] = {}
        # asyncssh uses timeout=; some substitutes accept timeout_s=.
        sig_params: set[str] = set()
        try:
            sig_params = set(inspect.signature(run).parameters)
        except (TypeError, ValueError):
            pass

        if "check" in sig_params or not sig_params:
            kwargs["check"] = False
        if timeout_s is not None:
            if "timeout" in sig_params or not sig_params:
                kwargs["timeout"] = timeout_s
            elif "timeout_s" in sig_params:
                kwargs["timeout_s"] = timeout_s
        if env is not None and ("env" in sig_params or not sig_params):
            # asyncssh env is process environment overrides; pass through best-effort.
            kwargs["env"] = env

        # Bridge-level deadline = exec timeout + grace. When the caller set no
        # exec timeout, leave the bridge deadline open (no internal timeout to
        # wait out). A bridge timeout cancels the coroutine mid-flight; the
        # connection may be stale, so we mark it dead for reconnect below.
        bridge_timeout = (
            timeout_s + _BRIDGE_TIMEOUT_GRACE_S if timeout_s is not None else None
        )
        try:
            raw = self._await(run(command, **kwargs), timeout_s=bridge_timeout)
        except TimeoutError as exc:
            msg = str(exc)
            # AsyncLoopBridge.run raises TimeoutError with "AsyncLoopBridge" in
            # the message when its own deadline fires — distinct from asyncssh's
            # internal timeout (handled cleanly by asyncssh). A bridge timeout
            # cancels the coroutine mid-flight; the connection may be stale, so
            # mark it dead so callers reconnect rather than reuse it.
            if "AsyncLoopBridge" in msg or "bridge" in msg.lower():
                self.mark_dead("bridge timeout")
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=msg or "timeout",
                cwd=cwd,
                timed_out=True,
            )
        except TransportError:
            raise
        except Exception as exc:
            # asyncssh.TimeoutError and similar
            name = type(exc).__name__
            if "timeout" in name.lower() or "timeout" in str(exc).lower():
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=_safe_connect_msg(exc),
                    cwd=cwd,
                    timed_out=True,
                )
            raise TransportError(
                "EXEC_FAILED",
                _safe_connect_msg(exc),
                details={"host": self.host},
            ) from exc

        return _coerce_exec_result(
            raw, default_cwd=cwd, preferred=self.text_encoding
        )


def _coerce_exec_result(
    raw: Any,
    *,
    default_cwd: str | None,
    preferred: str | None = None,
) -> ExecResult:
    """Normalize connection-layer results into ExecResult."""
    if isinstance(raw, ExecResult):
        if raw.cwd is None and default_cwd is not None:
            return ExecResult(
                exit_code=raw.exit_code,
                stdout=raw.stdout,
                stderr=raw.stderr,
                cwd=default_cwd,
                timed_out=raw.timed_out,
            )
        return raw

    if raw is None:
        raise TransportError("EXEC_FAILED", "remote run returned no result")

    # Duck-type asyncssh SSHCompletedProcess (and compatible result shapes).
    if (
        hasattr(raw, "exit_code")
        or hasattr(raw, "exit_status")
        or hasattr(raw, "returncode")
        or hasattr(raw, "exit_signal")
    ):
        exit_code = getattr(raw, "exit_code", None)
        if exit_code is None:
            exit_code = getattr(raw, "exit_status", None)
        # asyncssh sets exit_status=None + exit_signal="KILL" for signal-killed
        # processes. Map to shell convention 128+signum (or -1 if unknown) so
        # signal-killed runs are not reported as exit 0 (success).
        exit_signal = getattr(raw, "exit_signal", None)
        if exit_code is None and exit_signal:
            exit_code = _exit_code_from_signal(exit_signal)
        if exit_code is None:
            # Fall back to subprocess-style returncode; missing/None → -1
            # (not 0) to avoid false success when all exit attrs are absent.
            exit_code = getattr(raw, "returncode", None)
        if exit_code is None:
            exit_code = -1
        timed_out = bool(getattr(raw, "timed_out", False))
        cwd = getattr(raw, "cwd", None) or default_cwd
        return ExecResult(
            exit_code=int(exit_code),
            stdout=_decode_stream(getattr(raw, "stdout", ""), preferred),
            stderr=_decode_stream(getattr(raw, "stderr", ""), preferred),
            cwd=cwd,
            timed_out=timed_out,
        )

    if isinstance(raw, tuple) and len(raw) >= 2:
        # (exit, stdout[, stderr])
        exit_code = int(raw[0])
        stdout = _decode_stream(raw[1], preferred)
        stderr = _decode_stream(raw[2], preferred) if len(raw) > 2 else ""
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=default_cwd,
        )

    raise TransportError(
        "EXEC_FAILED",
        f"unrecognized remote run result type: {type(raw).__name__}",
    )


def _exit_code_from_signal(signal_name: Any) -> int:
    """Map an asyncssh ``exit_signal`` to a shell-style exit code (128 + signum).

    Falls back to ``-1`` when the signal name cannot be resolved to a number,
    so signal-killed runs are never reported as exit 0 (success).
    """
    import signal as signal_mod

    text = str(signal_name or "").strip()
    if not text:
        return -1
    upper = text.upper()
    # Numeric signal (rare) → 128 + n.
    try:
        return 128 + int(upper)
    except ValueError:
        pass
    # Named signal: "KILL", "TERM", "SIGKILL", etc.
    name = upper.removeprefix("SIG")
    try:
        signum = getattr(signal_mod, f"SIG{name}", None)
        if isinstance(signum, int):
            return 128 + int(signum)
    except Exception:  # noqa: BLE001 — best-effort signal lookup
        pass
    return -1


def _looks_credibly_windows(parsed: dict[str, Any]) -> bool:
    """True when a probe parse credibly identifies Windows (not echoed literals).

    The Windows probe uses ``echo os=windows & echo comspec=%COMSPEC% & ...``
    which a POSIX shell happily echoes back (printing ``os=windows`` and
    ``comspec=%COMSPEC%`` verbatim). Require a real COMSPEC path (no ``%``
    placeholder and a path separator) or an actual ``chcp`` code page before
    adopting the Windows parse — otherwise a genuine POSIX host that falls
    into the Windows retry would be misclassified as windows.
    """
    if parsed.get("os") != "windows":
        return False
    comspec = str(parsed.get("comspec") or "")
    if comspec and "%" not in comspec and ("\\" in comspec or "/" in comspec):
        return True
    if parsed.get("chcp") is not None:
        return True
    return False


def _safe_connect_msg(exc: BaseException) -> str:
    """Short connect error message without secret material."""
    text = str(exc).strip() or type(exc).__name__
    # Collapse whitespace; cap length for Agent track.
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:197] + "..."
    return text


def _map_ssh_connect_error(
    exc: BaseException,
    *,
    host: str,
    port: int,
) -> TransportError:
    """Map asyncssh/OS exceptions to stable TransportError codes."""
    name = type(exc).__name__
    msg = _safe_connect_msg(exc)
    blob = f"{name} {msg}".lower()
    details: dict[str, Any] = {"host": host, "port": port, "exc_type": name}

    # Host key / known_hosts failures (asyncssh: HostKeyNotVerifiable, …).
    hostkey_markers = (
        "hostkey",
        "host key",
        "known_hosts",
        "key verification",
        "notverifiable",
        "mismatch",
        "changed",
        "man-in-the-middle",
        "remote host identification",
    )
    if any(m in blob for m in hostkey_markers) or "HostKey" in name:
        return TransportError("HOSTKEY_MISMATCH", msg, details=details)

    auth_markers = (
        "auth",
        "permission denied",
        "publickey",
        "password",
        "keyboard-interactive",
        "too many authentication",
        "no matching authentication",
        "credentials",
    )
    if any(m in blob for m in auth_markers) or "PermissionDenied" in name:
        return TransportError("AUTH_FAILED", msg, details=details)

    return TransportError("CONNECT_FAILED", msg, details=details)


def _maybe_force_utf8(command: str, transport: SSHTransport) -> str:
    """Optionally wrap remote command to force UTF-8 locale / code page."""
    if not getattr(transport, "force_utf8_remote", False):
        return command
    fam = normalize_shell_family(getattr(transport, "remote_shell_family", "posix"))
    if fam == "cmd":
        # chcp 65001 for this command chain only.
        return f"chcp 65001 >nul & {command}"
    if fam == "powershell":
        return (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"{command}"
        )
    # POSIX: C.UTF-8 / en_US.UTF-8 best-effort without failing if missing.
    return (
        "export LC_ALL=C.UTF-8 2>/dev/null || export LC_ALL=en_US.UTF-8 2>/dev/null || true; "
        "export LANG=\"${LC_ALL:-C.UTF-8}\"; "
        f"{command}"
    )
