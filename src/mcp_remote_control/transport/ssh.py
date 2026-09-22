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

import asyncio
import errno
import inspect
import logging
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mcp_remote_control.codec import decode_auto
from mcp_remote_control.codec.text_codec import (
    DecodeResult,
    charmap_to_codec,
    codepage_to_codec,
)
from mcp_remote_control.identity.ssh_keys import key_load_failure_message
from mcp_remote_control.transport.async_bridge import AsyncLoopBridge, run_coro
from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError
from mcp_remote_control.transport.shell_wrap import (
    POSIX_PROBE_SCRIPT,
    POWERSHELL_PROBE_SCRIPT,
    WINDOWS_PROBE_SCRIPT,
    _is_unexpanded_probe_value,
    coerce_cwd_path,
    normalize_shell_family,
    parse_probe_output,
    wrap_with_cwd,
)

_log = logging.getLogger(__name__)

# Connector: kwargs -> connection handle (sync object or awaitable).
SSHConnector = Callable[..., Any]

# Grace added on top of asyncssh's internal timeout when forwarding a
# belt-and-suspenders deadline to AsyncLoopBridge.run. Keeps the sync caller
# from blocking indefinitely if asyncssh's own timeout/cancel stalls (e.g.
# hung DNS before connect_timeout starts).
_BRIDGE_TIMEOUT_GRACE_S = 5.0

# Finite wall-clock budget for best-effort session dispose / SFTP exit.
# ``timeout_s=None`` would park ``Future.result()`` forever on a blackholed
# peer. Bridge cancels the in-flight future when this elapses; dispose still
# never raises to the caller.
_DISPOSE_TIMEOUT_S = _BRIDGE_TIMEOUT_GRACE_S

# Grace on top of the profile connect timeout for the SFTP subsystem open.
# The open request rides an already-authenticated connection, so it needs no
# DNS/TCP/auth budget of its own: the connect timeout covers the server's
# sftp-server startup and this absorbs a stalled cancel. Kept finite because
# ``open_sftp`` runs under the serial op-lock - an unbounded wait there
# freezes every later op and even ``close`` on the endpoint.
_SFTP_OPEN_TIMEOUT_GRACE_S = _BRIDGE_TIMEOUT_GRACE_S


class _KnownHostsUnset:
    """Type of :data:`KNOWN_HOSTS_UNSET` - distinct from a ``None`` value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "KNOWN_HOSTS_UNSET"


# Three-state host-key contract, because asyncssh overloads ``None``:
#
# - ``KNOWN_HOSTS_UNSET`` - the profile said nothing. The ``known_hosts`` kwarg
#   is omitted, which is what asks asyncssh to use its own default
#   (``~/.ssh/known_hosts``, or no trusted keys when that file is missing).
# - ``None`` - the profile asked to skip verification (``known_hosts = "none"``
#   / ``false`` / ``off``). asyncssh disables host key validation for an
#   explicit ``None``; this is the documented escape hatch for lab hosts and
#   rebuilt VMs whose key changed.
# - a path - verify against that file.
#
# The distinction matters: passing a specified-but-empty value (``()``, ``""``)
# is NOT "skip" - asyncssh treats it as "no configured source" and falls back to
# ``~/.ssh/known_hosts``, so a host that is absent from (or changed in) that file
# fails the handshake even though the profile asked to skip checking.
KNOWN_HOSTS_UNSET: Any = _KnownHostsUnset()


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


def _reset_decode_record(transport: Any) -> None:
    """Start a new command's decode record, dropping the previous one.

    ``last_decode`` describes the streams of *one* command. A command that
    decodes no bytes at all - empty output, or a failure before it ran - must
    not leave the previous command's codec in place: a reader (or a debug dump)
    would otherwise attribute that codec to text this command never produced.
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

    A fallback decode is not an error - a host that really speaks gb18030 is
    decoded correctly through its leg - but the reader can no longer assume the
    text is right, and nothing else in the exec path says which codec produced
    it. The same goes for an ambiguous one (both the configured codec and
    UTF-8 accept the bytes): the text may be a plausible reading of the wrong
    codec. The WARNING is emitted once per (transport, codec, preferred) so a
    legacy host does not log a line per command; later occurrences stay at
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


def _decode_stream(
    value: Any,
    preferred: str | None = None,
    *,
    transport: Any = None,
) -> str:
    """Decode a remote stream, noting a non-UTF-8 read on *transport*."""
    result = decode_auto(value, preferred=preferred)
    _note_decode(transport, result, preferred, value)
    return result.text


async def _default_asyncssh_connect(
    *,
    host: str,
    port: int,
    username: str,
    client_keys: Sequence[Path | str] | None,
    connect_timeout: float,
    known_hosts: Any = KNOWN_HOSTS_UNSET,
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
        "connect_timeout": connect_timeout,
    }
    # asyncssh reads an *explicit* ``known_hosts=None`` as "disable host key
    # validation", while any specified-but-empty value (``()``, ``""``) makes it
    # fall back to ``~/.ssh/known_hosts`` and verify against that. Leaving the
    # argument out entirely is what asks for asyncssh's own default, so the
    # unset sentinel must not be forwarded as a value.
    if known_hosts is not KNOWN_HOSTS_UNSET:
        kwargs["known_hosts"] = known_hosts
    if client_keys:
        # Paths only - asyncssh loads contents itself.
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


# asyncssh refuses the connect with one of these when a client key file cannot
# be imported; both subclass ``ValueError``, neither subclasses ``OSError``.
# Matched by name *and owning module*: this module stays importable without
# asyncssh (the SSH stack is imported lazily at connect), and an injected
# connector raising its own class of the same name is not relabelled as a key
# failure - only asyncssh's own two error classes count.
_KEY_LOAD_ERROR_NAMES: frozenset[str] = frozenset(
    {"KeyImportError", "KeyEncryptionError"}
)
_KEY_LOAD_ERROR_MODULE = "asyncssh"


def _is_key_load_error(exc: BaseException) -> bool:
    """True when *exc* is asyncssh failing to import a private key file."""
    cls = type(exc)
    if cls.__name__ not in _KEY_LOAD_ERROR_NAMES:
        return False
    module = getattr(cls, "__module__", "") or ""
    return module == _KEY_LOAD_ERROR_MODULE or module.startswith(
        _KEY_LOAD_ERROR_MODULE + "."
    )


# errno values that mean *this host* cannot open the file: a mode-000 or
# foreign-owned key (EACCES/EPERM) or a directory handed over as a client key
# (EISDIR). A refused dial or a peer rejection carries no such errno+filename.
_LOCAL_KEY_ERRNOS: frozenset[int] = frozenset(
    {errno.EACCES, errno.EPERM, errno.EISDIR}
)


def _unreadable_client_key(
    exc: BaseException, client_keys: Sequence[Path | str] | None
) -> Path | None:
    """The client-key path a local OSError is about, when one is identifiable.

    Discriminates by errno *and* path, because an OSError alone is not a key
    problem (a refused connection is one too): the exception must carry a
    local-access errno and name a file from the ``client_keys`` list this
    transport handed to asyncssh. Only then is the failing open demonstrably
    about a key file of ours.
    """
    if not client_keys or not isinstance(exc, OSError):
        return None
    if exc.errno not in _LOCAL_KEY_ERRNOS:
        return None
    name = getattr(exc, "filename", None)
    if not name:
        return None
    named = Path(str(name))
    wanted = {str(Path(str(key)).expanduser()) for key in client_keys}
    if str(named) in wanted or str(named.expanduser()) in wanted:
        return named
    return None


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
        known_hosts: Any = KNOWN_HOSTS_UNSET,
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
        # Decode bookkeeping for the current command's streams: which codec
        # produced the text, whether bytes had to be replaced, and which
        # decisions already logged a warning (one line per codec, not one per
        # command). Reset at the start of every command
        # (_reset_decode_record), so a command that decoded no bytes leaves no
        # stale codec behind.
        self.last_decode: dict[str, Any] | None = None
        self._decode_warned: set[str] = set()
        self.remote_shell_family = normalize_shell_family(remote_shell_family)
        self.force_utf8_remote = bool(force_utf8_remote)
        self._connector: SSHConnector = connector or default_ssh_connector
        self._bridge = bridge
        self._conn: Any = None
        self._sftp: Any = None

    def _await(self, result: Any, *, timeout_s: float | None = None) -> Any:
        return _run_maybe_async(result, timeout_s=timeout_s, bridge=self._bridge)

    def _connector_kwargs(self) -> dict[str, Any]:
        """Connector/asyncssh kwargs for this transport.

        The single place the ``known_hosts`` rule is applied, so an injected
        connector and the real asyncssh call can never disagree about it: the
        unset sentinel is dropped from the mapping (omitting the argument is
        what selects asyncssh's own default), while an explicit ``None`` is
        forwarded and disables validation.
        """
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "client_keys": self.client_keys or None,
            "connect_timeout": max(self.connect_timeout_ms, 1) / 1000.0,
            "password": self.password,
            "passphrase": self.passphrase,
            "keepalive_interval": self.keepalive_interval_s,
        }
        if self.known_hosts is not KNOWN_HOSTS_UNSET:
            kwargs["known_hosts"] = self.known_hosts
        return kwargs

    def connect(self) -> None:
        if self._connected and self._conn is not None:
            return
        # After mark_dead (or a failed prior session) _conn may still hold a
        # dead handle. Close it best-effort before opening a replacement so
        # probe-fail reconnect / ensure_connected cannot stack zombie sessions
        # (esp. Windows OpenSSH). No-op when _conn is already None (happy path).
        self._dispose_prior_session()
        timeout_s = max(self.connect_timeout_ms, 1) / 1000.0
        # Belt-and-suspenders bridge deadline: asyncssh's connect_timeout
        # only starts after DNS resolution. Add grace so a hung DNS / stalled
        # cancel cannot block the sync caller indefinitely.
        bridge_timeout = timeout_s + _BRIDGE_TIMEOUT_GRACE_S
        try:
            conn = self._await(
                self._connector(**self._connector_kwargs()),
                timeout_s=bridge_timeout,
            )
        except TransportError:
            raise
        except Exception as exc:
            blocker = (
                key_load_failure_message(
                    self.client_keys, passphrase=self.passphrase
                )
                if _is_key_load_error(exc)
                else None
            )
            if blocker is not None:
                # The connect died inside asyncssh's key import, before any
                # dial: name the file, the reason and the remedy. asyncssh's
                # own text names none of the three, and reads like the peer
                # refused the session. Which credentials get attempted is
                # unchanged - the key is still in the list, so the connect
                # still fails here.
                raise TransportError(
                    "CONNECT_FAILED",
                    blocker,
                    details={
                        "host": self.host,
                        "port": self.port,
                        "exc_type": type(exc).__name__,
                    },
                ) from exc
            raise _map_ssh_connect_error(
                exc, host=self.host, port=self.port, client_keys=self.client_keys
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

    def _clear_sftp_cache(self) -> None:
        """Best-effort drop of the cached SFTP client (never raises).

        Nulls ``_sftp`` first so concurrent readers never see a half-closed
        handle, then exits/closes the old client if the object exposes that.
        Awaitables run under :data:`_DISPOSE_TIMEOUT_S` so a wedged SFTP
        exit cannot pin ``_op_lock`` forever.
        """
        sftp = self._sftp
        self._sftp = None
        if sftp is None:
            return
        try:
            close_sftp = getattr(sftp, "exit", None) or getattr(sftp, "close", None)
            if callable(close_sftp):
                self._await(close_sftp(), timeout_s=_DISPOSE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            _log.debug(
                "SSH SFTP cache clear failed (best-effort, host=%s): %s",
                self.host,
                exc,
            )

    def invalidate_sftp(self) -> None:
        """Drop the cached SFTP client so the next :meth:`open_sftp` reopens.

        Used when an SFTP channel dies while the SSH session may still be
        live (channel-only failure). Does **not** mark the SSH transport
        dead - callers keep the endpoint open and rebuild only the SFTP
        subsystem. Both this method and :meth:`mark_dead` tear down the
        cached client via :meth:`_clear_sftp_cache` (finite-timeout
        exit/close). SSH ``_conn`` teardown still happens on the next
        connect/close, not here.
        """
        self._clear_sftp_cache()

    @staticmethod
    def _sftp_channel(sftp: Any) -> Any:
        """Return the session channel behind an SFTP client, or None.

        asyncssh's ``SFTPClient`` exposes no channel of its own: the client
        holds an ``SFTPClientHandler``, the handler holds the stream reader
        and writer created from the channel, and the channel itself is the
        public ``SSHReader.channel`` / ``SSHWriter.channel``. Substitutes
        (and earlier asyncssh layouts) may instead carry ``_channel`` /
        ``channel`` directly.
        """
        for attr in ("_channel", "channel"):
            chan = getattr(sftp, attr, None)
            if chan is not None:
                return chan
        handler = getattr(sftp, "_handler", None)
        if handler is None:
            return None
        for holder in (
            getattr(handler, "_reader", None),
            getattr(handler, "_writer", None),
        ):
            chan = getattr(holder, "channel", None)
            if chan is not None:
                return chan
        return None

    @classmethod
    def _sftp_looks_dead(cls, sftp: Any) -> bool:
        """Best-effort: is a cached SFTP client no longer usable?

        Probes asyncssh's real layout - the ``is_closing()`` state of the
        channel reachable through ``_channel`` / ``channel`` or the
        handler's reader/writer - plus the common ``_closed`` /
        ``is_closed()`` / ``closed`` flags a substitute may expose. A flag
        access that raises counts as dead (the object cannot be trusted to
        serve the next op). When no signal is present at all, returns False
        so a healthy cache is reused.
        """
        if sftp is None:
            return True
        if getattr(sftp, "_closed", False) is True:
            return True
        for attr in ("is_closed", "closed"):
            flag = getattr(sftp, attr, None)
            if callable(flag):
                try:
                    if flag():
                        return True
                except Exception:  # noqa: BLE001
                    return True
            elif flag is True:
                return True
        chan = cls._sftp_channel(sftp)
        if chan is None:
            return False
        for attr in ("is_closing", "_closing", "is_closed"):
            flag = getattr(chan, attr, None)
            if callable(flag):
                try:
                    if flag():
                        return True
                except Exception:  # noqa: BLE001
                    return True
            elif flag is True:
                return True
        return False

    def _dispose_prior_session(self) -> None:
        """Best-effort drop of cached SFTP + prior SSH conn (never raises).

        Used by :meth:`close` and by :meth:`connect` when replacing a dead
        handle after :meth:`mark_dead`. Clears ``_sftp`` / ``_conn`` first so
        concurrent readers see a clean slate even if teardown stalls.
        ``wait_closed`` (and SFTP exit via :meth:`_clear_sftp_cache`) use a
        finite :data:`_DISPOSE_TIMEOUT_S` so a blackholed peer cannot hang
        reconnect/close under ``_op_lock``.
        """
        self._clear_sftp_cache()
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
            wait = getattr(conn, "wait_closed", None)
            if callable(wait):
                self._await(wait(), timeout_s=_DISPOSE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            _log.debug(
                "SSH dispose prior session failed (best-effort, host=%s): %s",
                self.host,
                exc,
            )

    def close(self) -> None:
        self._dispose_prior_session()
        self._connected = False

    @property
    def connection(self) -> Any:
        """Underlying asyncssh connection handle; None if closed."""
        return self._conn

    def _initiate_conn_close(self) -> None:
        """Start closing the retained connection without waiting for it.

        Cancelling an in-flight await does not release remote-side channels
        that op had half-opened, so a failing op initiates the close itself.
        ``close()`` only starts the teardown; ``wait_closed`` stays with the
        next connect/close dispose so the failing op keeps its own budget.
        Never raises.
        """
        close = getattr(self._conn, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            _log.debug(
                "SSH conn close initiate failed (best-effort, host=%s): %s",
                self.host,
                exc,
            )

    def _sftp_open_budget_s(self) -> float:
        """Wall clock allowed for one SFTP subsystem open.

        Derived from the profile connect timeout (default 15s) plus the
        shared bridge grace: the subsystem request is a single round trip on
        a connection that is already up, so it must not outlive the budget
        that was accepted for the far heavier connect. A peer that completes
        TCP and auth but never answers the request therefore surfaces as a
        timeout instead of parking the calling thread.
        """
        return max(self.connect_timeout_ms, 1) / 1000.0 + _SFTP_OPEN_TIMEOUT_GRACE_S

    def open_sftp(self) -> Any:
        """Lazy-open SFTP client from the SSH connection.

        Reuses a live cached client. When the cache is missing or looks
        dead (channel closing), drops it and opens a fresh client via
        ``start_sftp_client`` / ``open_sftp`` on the connection, then an
        already-attached ``sftp`` attribute. Raises ``UNSUPPORTED`` when
        none of those are available.

        The open awaits under a finite budget (see
        :meth:`_sftp_open_budget_s`) and raises ``TIMEOUT`` when the peer
        never answers the subsystem request; nothing is cached in that case,
        so the next call retries instead of handing back a half-opened
        client. Pair with :meth:`invalidate_sftp` after channel-closed
        errors so the next call never returns a permanently dead cache
        while the SSH endpoint stays open.
        """
        if self._sftp is not None:
            if not self._sftp_looks_dead(self._sftp):
                return self._sftp
            # Cached client is dead - drop and re-open below.
            self._clear_sftp_cache()
        conn = self._require_conn()
        budget = self._sftp_open_budget_s()

        for name in ("start_sftp_client", "open_sftp"):
            fn = getattr(conn, name, None)
            if callable(fn):
                try:
                    client = self._await(fn(), timeout_s=budget)
                except TimeoutError as exc:
                    # Bridge abandoned (cancelled) the in-flight open. Nothing
                    # is cached - ``_sftp`` is only assigned on success - and
                    # the session is marked dead plus closed so the remote-side
                    # subsystem channel the open half-created is released now.
                    self.mark_dead("sftp channel open timeout")
                    self._initiate_conn_close()
                    raise TransportError(
                        "TIMEOUT",
                        f"sftp channel open timed out after {budget:.1f}s",
                        details={"host": self.host, "timeout_s": budget},
                    ) from exc
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
        _reset_decode_record(self)
        conn = self._require_conn()
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)

        # Prefer an explicit run_command helper when the connection exposes one.
        # Same bridge wall-clock as shell fallback: exec timeout + grace so a
        # wedged runner coroutine cannot pin op_lock / Future.result forever.
        runner = getattr(conn, "run_command", None)
        if callable(runner):
            bridge_timeout = (
                timeout_s + _BRIDGE_TIMEOUT_GRACE_S if timeout_s is not None else None
            )
            try:
                result = self._await(
                    runner(command, cwd=work, timeout_s=timeout_s, env=env),
                    timeout_s=bridge_timeout,
                )
            except TimeoutError as exc:
                msg = str(exc)
                if "AsyncLoopBridge" in msg or "bridge" in msg.lower():
                    self.mark_dead("bridge timeout")
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=msg or "timeout",
                    cwd=work,
                    timed_out=True,
                )
            return _coerce_exec_result(
                result,
                default_cwd=work,
                preferred=self.text_encoding,
                transport=self,
            )

        full = wrap_with_cwd(
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

        Order: POSIX -> PowerShell (Windows OpenSSH default) -> cmd /c.
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
            # cmd.exe may echo `uname=$(uname ...)` literally - not a real uname.
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
                if _looks_credibly_windows(parsed_ps):
                    parsed = parsed_ps
                else:
                    parsed_w = _try_script(WINDOWS_PROBE_SCRIPT)
                    if _looks_credibly_windows(parsed_w):
                        parsed = parsed_w
                    elif not _has_trusted_probe_identity(parsed):
                        out["status"] = "partial"
            elif parsed.get("os") != "windows" and not credible_posix:
                parsed_ps = _try_script(POWERSHELL_PROBE_SCRIPT)
                if _looks_credibly_windows(parsed_ps):
                    parsed = parsed_ps
                else:
                    parsed_w = _try_script(WINDOWS_PROBE_SCRIPT)
                    if _looks_credibly_windows(parsed_w):
                        parsed = parsed_w
                    elif not _has_trusted_probe_identity(parsed):
                        out["status"] = "partial"

            out.update({k: v for k, v in parsed.items() if v is not None})
            # Enrich always writes dialect/caps/shell_family; those are wrap
            # hints, not identity. Empty/junk probes stay partial.
            if not _has_trusted_probe_identity(out) and out.get("status") == "ok":
                out["status"] = "partial"

            # Shell family for cwd wrap. Require a credible Windows probe
            # (COMSPEC path or chcp); shell_base=powershell/pwsh/cmd is a
            # wrap hint and is hardcoded in the PS/cmd scripts.
            if _looks_credibly_windows(out):
                fam = normalize_shell_family(
                    str(out.get("shell_base") or "cmd")
                )
                self.remote_shell_family = fam
                out["shell_family"] = fam
            elif _has_trusted_probe_identity(out):
                self.remote_shell_family = "posix"
                out["shell_family"] = "posix"
            else:
                out["shell_family"] = self.remote_shell_family

            # Encoding preference.
            enc = None
            if out.get("chcp") is not None:
                enc = codepage_to_codec(out["chcp"])  # type: ignore[arg-type]
            if enc is None and out.get("charmap"):
                enc = charmap_to_codec(str(out["charmap"]))
            if enc:
                self.text_encoding = enc
                out["text_encoding"] = enc

            if _is_trusted_identity_value(out.get("home")):
                home = str(out["home"]).strip()
                if home not in {"True", "False", "None"}:
                    self.home = home
            # Only path-like pwd - never bool cap bleed-through (cwd=True)
            # or unexpanded probe placeholders.
            pwd = coerce_cwd_path(out.get("pwd"))
            if pwd and _is_trusted_identity_value(pwd):
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
        if not _has_trusted_probe_identity(out) and out.get("status") == "ok":
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
        _reset_decode_record(self)
        conn = self._require_conn()
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)

        # Explicit run_argv helper: same bridge deadline as run_command helper
        # and _run_shell_on_conn (timeout_s + grace; None stays unbounded).
        runner = getattr(conn, "run_argv", None)
        if callable(runner):
            bridge_timeout = (
                timeout_s + _BRIDGE_TIMEOUT_GRACE_S if timeout_s is not None else None
            )
            try:
                result = self._await(
                    runner(list(argv), cwd=work, timeout_s=timeout_s, env=env),
                    timeout_s=bridge_timeout,
                )
            except TimeoutError as exc:
                msg = str(exc)
                if "AsyncLoopBridge" in msg or "bridge" in msg.lower():
                    self.mark_dead("bridge timeout")
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=msg or "timeout",
                    cwd=work,
                    timed_out=True,
                )
            return _coerce_exec_result(
                result,
                default_cwd=work,
                preferred=self.text_encoding,
                transport=self,
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
        full = wrap_with_cwd(quoted, work, shell_family=fam)
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
            # Peer drop / bad probe left a zombie flag - publish dead state.
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
        Cached SFTP is torn down immediately via :meth:`_clear_sftp_cache`
        (null first, then best-effort exit/close under
        :data:`_DISPOSE_TIMEOUT_S`) so mid-op death does not orphan a remote
        SFTP subsystem until later connect dispose. ``_conn`` is retained
        until the next :meth:`connect` (or :meth:`close`), which best-effort
        closes it before opening a replacement - avoids stacking zombie SSH
        sessions.
        """
        self._connected = False
        if reason:
            self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}
        # Close SFTP subsystem; keep _conn so reconnect/close can tear it down.
        self._clear_sftp_cache()

    def _run_shell_on_conn(
        self,
        conn: Any,
        command: str,
        *,
        cwd: str | None,
        timeout_s: float | None,
        env: dict[str, str] | None,
    ) -> ExecResult:
        create = getattr(conn, "create_process", None)
        run = getattr(conn, "run", None)
        if not callable(create) and not callable(run):
            raise TransportError(
                "UNSUPPORTED",
                "ssh connection has no run/run_command for exec",
                details={"host": self.host},
            )

        # When a wall-clock exec timeout is set, prefer create_process so we
        # hold a process handle for timeout kill (asyncssh.run is
        # create_process + wait; without the handle a bridge cancel leaves
        # remote sleep/channel running - LocalTransport parity). Without an
        # exec timeout, keep the historical run() path (loop-identity mocks
        # and recording fakes observe sync run() even if the bridge does not
        # execute the returned coroutine).
        # process_box is filled on the bridge loop after create; the sync
        # TimeoutError handler closes it best-effort if cancel races cleanup.
        process_box: list[Any] = []
        use_create = callable(create) and timeout_s is not None

        # Bridge-level deadline = exec timeout + grace. When the caller set no
        # exec timeout, leave the bridge deadline open (no internal timeout to
        # wait out). A bridge timeout cancels the coroutine mid-flight; the
        # connection may be stale, so we mark it dead for reconnect below.
        bridge_timeout = (
            timeout_s + _BRIDGE_TIMEOUT_GRACE_S if timeout_s is not None else None
        )

        try:
            if use_create:
                raw = self._await(
                    _exec_via_create_process(
                        create,
                        command,
                        timeout_s=timeout_s,
                        env=env,
                        process_box=process_box,
                        cwd=cwd,
                        text_encoding=self.text_encoding,
                        transport=self,
                    ),
                    timeout_s=bridge_timeout,
                )
            else:
                # run() path (or create_process-only conn with no timeout).
                if callable(run):
                    kwargs = _ssh_run_kwargs(run, timeout_s=timeout_s, env=env)
                    raw = self._await(
                        run(command, **kwargs), timeout_s=bridge_timeout
                    )
                else:
                    # create_process without exec timeout (unbounded wait).
                    raw = self._await(
                        _exec_via_create_process(
                            create,
                            command,
                            timeout_s=None,
                            env=env,
                            process_box=process_box,
                            cwd=cwd,
                            text_encoding=self.text_encoding,
                            transport=self,
                        ),
                        timeout_s=None,
                    )
        except TimeoutError as exc:
            # Always best-effort close any process we already opened. Covers
            # bridge cancel (process_box) when CancelledError cleanup races
            # the sync return, and plain TimeoutError without a process.
            for proc in list(process_box):
                _best_effort_close_ssh_process(proc)
            msg = str(exc)
            # AsyncLoopBridge.run raises TimeoutError with "AsyncLoopBridge" in
            # the message when its own wall-clock deadline fires - distinct
            # from asyncssh's process.wait timeout (which closes the process
            # above but leaves the session usable). A bridge timeout cancels
            # the coroutine mid-flight; the connection object may be stale, so
            # mark_dead so callers reconnect rather than reuse it. mark_dead
            # does not close _conn (next connect/close disposes it); the
            # remote process/channel is closed best-effort above.
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
            # asyncssh.TimeoutError and similar (process already closed above
            # when we held a create_process handle).
            if _is_timeout_exc(exc):
                for proc in list(process_box):
                    _best_effort_close_ssh_process(proc)
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
            raw,
            default_cwd=cwd,
            preferred=self.text_encoding,
            transport=self,
        )


async def _exec_via_create_process(
    create: Any,
    command: str,
    *,
    timeout_s: float | None,
    env: dict[str, str] | None,
    process_box: list[Any],
    cwd: str | None,
    text_encoding: str | None,
    transport: Any = None,
) -> Any:
    """create_process + wait with timeout kill (LocalTransport parity).

    On process-wait timeout, closes the process and returns an
    ``ExecResult(timed_out=True)`` instead of raising ``TimeoutError`` so
    AsyncLoopBridge cannot rewrite it as a wall-clock bridge timeout
    (Python 3.10+ ``TimeoutError`` is an alias of
    ``concurrent.futures.TimeoutError``).
    """
    create_kwargs = _ssh_create_process_kwargs(create, env=env)
    proc = create(command, **create_kwargs)
    if inspect.isawaitable(proc):
        proc = await proc
    process_box.append(proc)
    try:
        result = await _await_ssh_process_wait(proc, timeout_s=timeout_s)
        # Some substitutes' wait() returns None; coerce from the process
        # object itself (exit_status / stdout on SSHClientProcess).
        return proc if result is None else result
    except asyncio.CancelledError:
        # Bridge wall-clock cancel mid-wait: tear down remote process/channel
        # so it does not outlive this call.
        _best_effort_close_ssh_process(proc)
        raise
    except Exception as exc:
        if _is_timeout_exc(exc):
            _best_effort_close_ssh_process(proc)
            return ExecResult(
                exit_code=-1,
                stdout=_decode_stream(
                    getattr(exc, "stdout", "") or "",
                    text_encoding,
                    transport=transport,
                ),
                stderr=_safe_connect_msg(exc) or "timeout",
                cwd=cwd,
                timed_out=True,
            )
        raise


def _is_timeout_exc(exc: BaseException) -> bool:
    """True when *exc* looks like a command / wait timeout (not bridge)."""
    name = type(exc).__name__
    return "timeout" in name.lower() or "timeout" in str(exc).lower()


def _callable_accepts_kwarg(fn: Any, name: str) -> bool:
    """True if *fn* can accept *name* as a keyword argument.

    True when the signature is empty/uninspectable (best-effort pass-through),
    names *name* explicitly, or has a ``**kwargs`` catch-all. Real asyncssh
    ``run`` / ``create_process`` expose ``env`` only via ``**kwargs`` - gating
    solely on ``'env' in parameters`` silently drops MCP exec env.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if not sig.parameters:
        return True
    for pname, param in sig.parameters.items():
        if pname == name:
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _ssh_run_kwargs(
    run: Any,
    *,
    timeout_s: float | None,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    """Build kwargs for ``conn.run`` (check / timeout / env duck-typed).

    When *env* is not None it is always either forwarded or rejected with
    ``TransportError(UNSUPPORTED)`` - never silently discarded.
    """
    kwargs: dict[str, Any] = {}
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
    if env is not None:
        if not _callable_accepts_kwarg(run, "env"):
            raise TransportError(
                "UNSUPPORTED",
                "ssh conn.run does not accept env= process environment",
            )
        # asyncssh env is process environment overrides; pass through.
        kwargs["env"] = env
    return kwargs


def _ssh_create_process_kwargs(
    create: Any,
    *,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    """Build kwargs for ``conn.create_process`` (no check/timeout - those are wait).

    When *env* is not None it is always either forwarded or rejected with
    ``TransportError(UNSUPPORTED)`` - never silently discarded.
    """
    kwargs: dict[str, Any] = {}
    if env is not None:
        if not _callable_accepts_kwarg(create, "env"):
            raise TransportError(
                "UNSUPPORTED",
                "ssh conn.create_process does not accept env= process environment",
            )
        kwargs["env"] = env
    return kwargs


async def _await_ssh_process_wait(
    proc: Any,
    *,
    timeout_s: float | None,
) -> Any:
    """Await ``proc.wait`` with optional timeout; return *proc* if no wait."""
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return proc

    wait_params: set[str] = set()
    try:
        wait_params = set(inspect.signature(wait).parameters)
    except (TypeError, ValueError):
        pass

    wkwargs: dict[str, Any] = {}
    if "check" in wait_params:
        wkwargs["check"] = False
    if timeout_s is not None:
        if "timeout" in wait_params:
            wkwargs["timeout"] = timeout_s
        elif "timeout_s" in wait_params:
            wkwargs["timeout_s"] = timeout_s

    result = wait(**wkwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _best_effort_close_ssh_process(proc: Any) -> None:
    """Best-effort terminate/close of a remote SSH process or channel.

    Mirrors LocalTransport process-tree kill on timeout: once the wall-clock
    budget elapses the remote command must not keep running (e.g. ``sleep 3600``).
    Prefer ``terminate`` then ``close``; fall back to nested channel close.
    Never raises. Success-path callers must not invoke this (no extra close).
    """
    if proc is None:
        return

    def _invoke(obj: Any, name: str) -> bool:
        method = getattr(obj, name, None)
        if not callable(method):
            return False
        try:
            maybe = method()
        except Exception:  # noqa: BLE001 - best-effort
            return False
        if inspect.isawaitable(maybe):
            _schedule_awaitable_close(maybe)
        return True

    for method_name in ("terminate", "close", "kill"):
        _invoke(proc, method_name)

    chan = getattr(proc, "channel", None) or getattr(proc, "_chan", None)
    if chan is not None and chan is not proc:
        for method_name in ("close", "abort"):
            _invoke(chan, method_name)


def _schedule_awaitable_close(aw: Any) -> None:
    """Drain an awaitable close without hanging the caller (best-effort)."""

    async def _drain() -> None:
        try:
            await asyncio.wait_for(aw, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        try:
            loop.create_task(_drain())
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        run_coro(_drain(), timeout_s=1.0)
    except Exception:  # noqa: BLE001
        pass


def _coerce_exec_result(
    raw: Any,
    *,
    default_cwd: str | None,
    preferred: str | None = None,
    transport: Any = None,
) -> ExecResult:
    """Normalize connection-layer results into ExecResult.

    *transport*, when given, receives the decode decision for byte streams
    (``last_decode`` plus a warning on the first non-UTF-8 read).
    """
    if isinstance(raw, ExecResult):
        # A connector may hand back an ExecResult whose streams are still raw
        # bytes (the dataclass declares str). Decode those here so no byte
        # stream reaches a caller unread, and every read is noted.
        stdout = _decode_stream(raw.stdout, preferred, transport=transport)
        stderr = _decode_stream(raw.stderr, preferred, transport=transport)
        if raw.cwd is None and default_cwd is not None:
            return ExecResult(
                exit_code=raw.exit_code,
                stdout=stdout,
                stderr=stderr,
                cwd=default_cwd,
                timed_out=raw.timed_out,
            )
        if stdout is not raw.stdout or stderr is not raw.stderr:
            return ExecResult(
                exit_code=raw.exit_code,
                stdout=stdout,
                stderr=stderr,
                cwd=raw.cwd,
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
            # Fall back to subprocess-style returncode; missing/None -> -1
            # (not 0) to avoid false success when all exit attrs are absent.
            exit_code = getattr(raw, "returncode", None)
        if exit_code is None:
            exit_code = -1
        timed_out = bool(getattr(raw, "timed_out", False))
        cwd = getattr(raw, "cwd", None) or default_cwd
        return ExecResult(
            exit_code=int(exit_code),
            stdout=_decode_stream(
                getattr(raw, "stdout", ""), preferred, transport=transport
            ),
            stderr=_decode_stream(
                getattr(raw, "stderr", ""), preferred, transport=transport
            ),
            cwd=cwd,
            timed_out=timed_out,
        )

    if isinstance(raw, tuple) and len(raw) >= 2:
        # (exit, stdout[, stderr])
        exit_code = int(raw[0])
        stdout = _decode_stream(raw[1], preferred, transport=transport)
        stderr = (
            _decode_stream(raw[2], preferred, transport=transport)
            if len(raw) > 2
            else ""
        )
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
    # Numeric signal (rare) -> 128 + n.
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
    except Exception:  # noqa: BLE001 - best-effort signal lookup
        pass
    return -1


# Host identity keys. dialect / caps / shell_family / shell_base are wrap
# hints only - probe scripts hardcode shell_base=powershell|pwsh|cmd.
_PROBE_IDENTITY_KEYS: tuple[str, ...] = (
    "os",
    "uname",
    "user",
    "home",
    "pwd",
    "shell_path",
)

def _is_trusted_identity_value(value: Any) -> bool:
    """True when *value* is a real identity token, not an echo placeholder."""
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        return False
    text = str(value).strip()
    if not text:
        return False
    # Unexpanded ${VAR} / $(...) / %VAR% are probe echoes, not identity.
    if _is_unexpanded_probe_value(text):
        return False
    # Hardcoded probe labels are not host identity.
    if text.lower() in {
        "posix",
        "windows",
        "powershell",
        "pwsh",
        "cmd",
        "command",
    }:
        return False
    return True


def _has_trusted_probe_identity(data: dict[str, Any]) -> bool:
    """True when any host-identity field is a trusted (non-echo) value.

    ``parse_probe_output`` always runs dialect enrich on non-empty text, so
    a non-empty dict is not identity. dialect / caps / shell_family do not
    count.
    """
    return any(
        _is_trusted_identity_value(data.get(key)) for key in _PROBE_IDENTITY_KEYS
    )


def _looks_credibly_windows(parsed: dict[str, Any]) -> bool:
    """True when a probe parse credibly identifies Windows (not echoed literals).

    The Windows probe uses ``echo os=windows & echo comspec=%COMSPEC% & ...``
    which a POSIX shell happily echoes back (printing ``os=windows`` and
    ``comspec=%COMSPEC%`` verbatim). The PowerShell probe hardcodes
    ``os=windows`` / ``shell_base=powershell`` the same way. Require a real
    COMSPEC path (no ``%`` placeholder and a path separator) or an actual
    ``chcp`` code page - ``shell_base`` alone is not evidence.
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
    client_keys: Sequence[Path | str] | None = None,
) -> TransportError:
    """Map asyncssh/OS exceptions to stable TransportError codes."""
    name = type(exc).__name__
    msg = _safe_connect_msg(exc)
    blob = f"{name} {msg}".lower()
    details: dict[str, Any] = {"host": host, "port": port, "exc_type": name}

    # A client-key file this host cannot open aborts the connect while asyncssh
    # prepares it, before any dial. Its message says "Permission denied", which
    # otherwise maps to AUTH_FAILED below and reads as the peer rejecting a
    # credential; it is a local file problem with a local remedy.
    local_key = _unreadable_client_key(exc, client_keys)
    if local_key is not None:
        details["client_key"] = str(local_key)
        return TransportError(
            "CONNECT_FAILED",
            f"cannot read ssh client key {local_key}: {msg}; make it a readable "
            "file this user owns (permissions/ownership), or drop it from the "
            "chain \u2014 the connect stopped before any dial, so no credential was "
            "tried",
            details=details,
        )

    # Host key / known_hosts failures (asyncssh: HostKeyNotVerifiable, ...).
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
