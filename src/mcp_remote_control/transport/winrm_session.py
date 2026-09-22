"""WinRM session adapters (AdaptedWinRMSession, PypsrpClientAdapter)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mcp_remote_control.transport.base import TransportError


class _WinRMConnectWatch:
    """Shared bucket of Client/session handles built during one connect().

    The waiter thread (``connect``) and the worker thread (connector factory)
    share one instance. The worker records handles as they are constructed;
    a wall-clock timeout marks the watch abandoned and best-effort closes
    every recorded handle so a late ``Client`` cannot leak a WinRM shell.
    """

    __slots__ = ("handles", "lock", "abandoned")

    def __init__(self) -> None:
        self.handles: list[Any] = []
        self.lock = threading.Lock()
        self.abandoned = False


_connect_watch_tls = threading.local()


def _current_connect_watch() -> _WinRMConnectWatch | None:
    return getattr(_connect_watch_tls, "watch", None)


@contextmanager
def _use_winrm_connect_watch(watch: _WinRMConnectWatch) -> Iterator[None]:
    """Bind *watch* on this thread so :func:`note_winrm_connect_handle` sees it."""
    prev = _current_connect_watch()
    _connect_watch_tls.watch = watch
    try:
        yield
    finally:
        _connect_watch_tls.watch = prev


def note_winrm_connect_handle(obj: Any) -> None:
    """Record a Client/session built during ``connect`` so timeout can close it.

    No-op when no connect watch is bound (construction outside
    :meth:`WinRMTransport.connect`). If the waiter's wall-clock already
    expired, close *obj* immediately so a late factory return cannot leak
    a remote shell (``MaxShellsPerUser``).
    """
    if obj is None:
        return
    watch = _current_connect_watch()
    if watch is None:
        return
    close_now = False
    with watch.lock:
        watch.handles.append(obj)
        close_now = bool(watch.abandoned)
    if close_now:
        close_winrm_connect_handle(obj)


def close_winrm_connect_handle(obj: Any) -> None:
    """Best-effort ``close()`` on a connect-time Client/session; never raises."""
    if obj is None:
        return
    closer = getattr(obj, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def abandon_winrm_connect_watch(watch: _WinRMConnectWatch) -> None:
    """Mark *watch* abandoned and close every handle already recorded."""
    with watch.lock:
        watch.abandoned = True
        leftover = list(watch.handles)
        watch.handles.clear()
    for obj in leftover:
        close_winrm_connect_handle(obj)


def _bound(obj: Any, name: str) -> Callable[..., Any] | None:
    """Return a callable attribute, or None when missing/non-callable."""
    fn = getattr(obj, name, None)
    return fn if callable(fn) else None


class AdaptedWinRMSession:
    """Stable WinRM session surface for production transport paths.

    Built once at connect from a connector return value. Capability discovery
    (which methods exist) happens only here; ``WinRMTransport`` then calls this
    surface with fixed kwargs (``environment=`` on oneshot exec; wall-clock
    timeout outside the session API).

    Real ``pypsrp.client.Client`` instances are wrapped by
    :class:`PypsrpClientAdapter` in :func:`default_winrm_connector` so the
    library call shape is fixed. Test doubles implement the same methods
    directly (accept ``environment=`` even when unused).
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.cwd = getattr(raw, "cwd", None)
        self.home = getattr(raw, "home", None)
        self.wsman = getattr(raw, "wsman", None)
        self._run_command = _bound(raw, "run_command")
        self._run_argv = _bound(raw, "run_argv")
        self._execute_ps = _bound(raw, "execute_ps")
        self._execute_cmd = _bound(raw, "execute_cmd")
        self._open_runspace = _bound(raw, "open_runspace")
        self._open_fs = _bound(raw, "open_fs")
        self._close = _bound(raw, "close")
        self._copy = _bound(raw, "copy")
        self._fetch = _bound(raw, "fetch")
        # Seed probe attributes when the raw session already exposes them.
        # Capability bits (language_mode / cmdlet flags) may be pre-seeded by
        # adapters; identity seeds (os/shell/ps_version) alone never imply
        # ps_script_fs=true - that requires capability fields or a live probe.
        self._meta_seeds: dict[str, Any] = {}
        for key in (
            "os",
            "shell",
            "ps_version",
            "probe_status",
            "probe_error",
            "language_mode",
            "ps_edition",
            "os_version",
            "has_convertto_json",
            "can_get_item",
            "can_file_io",
        ):
            val = getattr(raw, key, None)
            if val is not None:
                self._meta_seeds[key] = val
        # File-store methods directly on the session (duck-typed file client).
        self._file_stat = _bound(raw, "stat")
        self._file_listdir = _bound(raw, "listdir")
        self._file_read = _bound(raw, "read_file")
        self._file_write = _bound(raw, "write_file")

    @property
    def has_run_command(self) -> bool:
        return self._run_command is not None

    @property
    def has_run_argv(self) -> bool:
        return self._run_argv is not None

    @property
    def has_execute_ps(self) -> bool:
        return self._execute_ps is not None

    @property
    def has_execute_cmd(self) -> bool:
        return self._execute_cmd is not None

    @property
    def has_open_runspace(self) -> bool:
        return self._open_runspace is not None

    @property
    def has_open_fs(self) -> bool:
        return self._open_fs is not None

    @property
    def has_wsman(self) -> bool:
        return self.wsman is not None

    @property
    def is_file_client(self) -> bool:
        return bool(
            self._file_stat
            and self._file_listdir
            and self._file_read
            and self._file_write
        )

    @property
    def can_build_pypsrp_fs(self) -> bool:
        return bool(self._copy or self._fetch or self._execute_ps)

    def close(self) -> None:
        if self._close is not None:
            self._close()

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        if self._run_command is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no run_command",
            )
        return self._run_command(command, cwd=cwd, timeout_s=timeout_s, env=env)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        if self._run_argv is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no run_argv",
            )
        return self._run_argv(argv, cwd=cwd, timeout_s=timeout_s, env=env)

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Oneshot PowerShell; always accepts ``environment`` (Protocol contract)."""
        if self._execute_ps is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no execute_ps",
            )
        return self._execute_ps(script, environment=environment)

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Oneshot cmd; always accepts ``environment`` (Protocol contract)."""
        if self._execute_cmd is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no execute_cmd",
            )
        return self._execute_cmd(command, environment=environment)

    def open_runspace(self) -> Any:
        if self._open_runspace is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session cannot open a PowerShell runspace",
            )
        return self._open_runspace()

    def open_fs(self) -> Any:
        if self._open_fs is None:
            raise TransportError(
                "UNSUPPORTED",
                "winrm session has no open_fs",
            )
        return self._open_fs()

    def copy(self, local: str, remote: str) -> None:
        if self._copy is None:
            raise TransportError("UNSUPPORTED", "winrm session has no copy")
        self._copy(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        if self._fetch is None:
            raise TransportError("UNSUPPORTED", "winrm session has no fetch")
        self._fetch(remote, local)

    def seed_attr(self, key: str) -> Any:
        return self._meta_seeds.get(key)


def adapt_winrm_session(raw: Any) -> AdaptedWinRMSession:
    """Normalize a connector return value to :class:`AdaptedWinRMSession`.

    Idempotent: already-adapted sessions are returned as-is.
    """
    if isinstance(raw, AdaptedWinRMSession):
        return raw
    return AdaptedWinRMSession(raw)


class PypsrpClientAdapter:
    """Adapter: real pypsrp ``Client`` -> oneshot Protocol surface.

    Knows the fixed pypsrp call shape (``environment=`` always supported;
    no per-call timeout kwarg). Confines library-specific kwargs here so
    production never uses ``inspect.signature``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.wsman = getattr(client, "wsman", None)
        self.cwd = getattr(client, "cwd", None)
        self.home = getattr(client, "home", None)
        # Record as soon as the adapter exists so a hung connector that
        # already built the Client can still be closed on connect timeout.
        note_winrm_connect_handle(self)

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        return self._client.execute_ps(script, environment=environment)

    def execute_cmd(
        self,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        return self._client.execute_cmd(command, environment=environment)

    def copy(self, local: str, remote: str) -> None:
        self._client.copy(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        self._client.fetch(remote, local)


# Adapter-chain attributes linking an adapted session to the pypsrp HTTP
# transport that caches the authentication / message-encryption context.
_RESYNC_LINK_ATTRS = ("raw", "_client", "wsman", "transport")
# Cached state on that transport which makes the next send() re-handshake.
_RESYNC_STATE_ATTRS = ("encryption", "session")
# The chain is four links deep; the bound only guards against a self-referential
# double turning the walk into an infinite one.
_RESYNC_MAX_NODES = 16

# Slot on the pypsrp HTTP transport holding an installed round-trip reader, so
# a second install reuses it instead of wrapping ``_send_request`` twice.
_ROUND_TRIP_READER_ATTR = "_mrc_round_trips"


def _close_quietly(obj: Any) -> None:
    """Best-effort ``close()``; never raises."""
    closer = getattr(obj, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _iter_link_nodes(session: Any) -> Iterator[Any]:
    """Yield *session* and every node reachable through the adapter chain.

    The chain is ``AdaptedWinRMSession`` -> ``PypsrpClientAdapter`` -> pypsrp
    ``Client`` -> ``WSMan`` -> HTTP transport; nodes that do not expose a link
    attribute are skipped, so test doubles and non-pypsrp sessions simply
    match nothing. The node budget bounds a self-referential double.
    """
    seen: set[int] = set()
    frontier: list[Any] = [session]
    while frontier and len(seen) < _RESYNC_MAX_NODES:
        node = frontier.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for attr in _RESYNC_LINK_ATTRS:
            try:
                child = getattr(node, attr, None)
            except Exception:  # noqa: BLE001 - a hostile double must not break the walk
                continue
            if child is not None and id(child) not in seen:
                frontier.append(child)


def _is_bodyless_handshake(request: Any) -> bool:
    """Whether *request* is pypsrp's bodyless authentication POST.

    ``_TransportHTTP.send`` sends ``requests.Request("POST", endpoint,
    data=None)`` to establish the security context before it wraps the
    operation message. That exchange carries no operation, so it must stay out
    of the round-trip count. Only a *prepared* request without a body qualifies
    (a prepared request always carries a ``url``), so opaque test doubles keep
    counting as operation exchanges.
    """
    try:
        if request.body is not None:
            return False
    except AttributeError:
        return False
    return isinstance(getattr(request, "url", None), str)


def install_winrm_round_trip_counter(session: Any) -> Callable[[], int] | None:
    """Count payload-carrying HTTP exchanges that completed on *session*.

    Returns a reader for the number of exchanges the pypsrp HTTP transport has
    completed successfully since install, or ``None`` when *session* has no
    pypsrp transport, that transport exposes no ``_send_request``, or the wrap
    fails. Never raises.

    The count is what makes a replay provably free of side effects: a stale
    message-encryption frame can only strike an operation's **first**
    payload-carrying request (the context needs an idle gap of several seconds
    to go stale, and any successful exchange in between rebuilds it). A
    rejection observed after the count advanced during the operation therefore
    cannot be that first request, and the operation may have run remotely.

    pypsrp's bodyless authentication POST is excluded (see
    :func:`_is_bodyless_handshake`). It is not the operation's request, and
    counting it made the *first* operation exchange look like a later one: on a
    fresh link the handshake succeeds and the operation's own POST is then
    rejected, which would refuse exactly the replay the counter exists to
    allow.

    Idempotent per transport object: the reader is stored on the transport and
    returned unchanged by a later install. The transport's own ``_send_request``
    is wrapped, so the counter survives :func:`resync_winrm_session` - which
    clears cached state, not the transport object.
    """
    if session is None:
        return None
    target: Any = None
    send: Callable[..., Any] | None = None
    for node in _iter_link_nodes(session):
        try:
            candidate = getattr(node, "_send_request", None)
        except Exception:  # noqa: BLE001 - a hostile double must not break install
            continue
        if callable(candidate):
            target, send = node, candidate
            break
    if target is None or send is None:
        return None
    existing = getattr(target, _ROUND_TRIP_READER_ATTR, None)
    if callable(existing):
        return existing

    state = {"count": 0}
    # The reader gates replay decisions, so a lost increment is not cosmetic:
    # an undercount would read as "nothing was exchanged yet".
    guard = threading.Lock()

    def _wrapped_send(*args: Any, **kwargs: Any) -> Any:
        result = send(*args, **kwargs)
        request = args[0] if args else kwargs.get("request")
        if _is_bodyless_handshake(request):
            return result
        with guard:
            state["count"] += 1
        return result

    def _reader() -> int:
        with guard:
            return state["count"]

    try:
        setattr(target, _ROUND_TRIP_READER_ATTR, _reader)
    except Exception:  # noqa: BLE001 - uncountable transport, not an error
        return None
    try:
        target._send_request = _wrapped_send
    except Exception:  # noqa: BLE001 - leave no half-installed marker behind
        try:
            delattr(target, _ROUND_TRIP_READER_ATTR)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        return None
    return _reader


def resync_winrm_session(session: Any) -> bool:
    """Drop pypsrp's cached HTTP authentication/encryption state.

    A plain-HTTP WinRM link with message encryption (``encryption=auto`` ->
    ``wrap_required=True``) derives a message-encryption context from one
    authentication exchange. Once that context goes stale the server rejects
    every later request with an empty-body HTTP 400 - a request that provably
    never executed remotely. Clearing ``encryption`` and the cached
    ``requests.Session`` makes pypsrp's next ``send()`` replay the initial
    authentication request and rebuild the context, so the link heals in place
    instead of staying "connected" but permanently unusable.

    Walks ``session.raw`` -> ``_client`` -> ``wsman`` -> ``transport`` (an
    :class:`AdaptedWinRMSession` wraps a :class:`PypsrpClientAdapter`, which
    wraps a pypsrp ``Client``); nodes that do not expose the cached state are
    skipped, so test doubles and non-pypsrp sessions simply match nothing.
    The stale session's socket pool is closed before it is dropped rather than
    left for the garbage collector.

    Returns True only when a non-None cached value was actually cleared.
    Never raises.
    """
    if session is None:
        return False
    cleared = False
    for node in _iter_link_nodes(session):
        for attr in _RESYNC_STATE_ATTRS:
            try:
                current = getattr(node, attr, None)
            except Exception:  # noqa: BLE001 - a hostile double must not break resync
                continue
            if current is None:
                continue
            try:
                setattr(node, attr, None)
            except Exception:  # noqa: BLE001 - read-only state is not resyncable
                continue
            if attr == "session":
                # Released only once the reference is really gone, so a failed
                # setattr can never leave the transport holding a closed pool.
                _close_quietly(current)
            cleared = True
    return cleared


def default_winrm_connector(**kwargs: Any) -> Any:
    """Construct a real pypsrp ``Client`` wrapped in :class:`PypsrpClientAdapter`.

    Expected kwargs match :func:`assemble_pypsrp_kwargs` / transport connect:
    host, port, username, password, auth, ssl, cert_validation, encryption,
    connect_timeout, operation_timeout, read_timeout, reconnection_retries,
    reconnection_backoff, certificate_*, negotiate_*, credssp_*. Maps
    ``connect_timeout`` -> pypsrp ``connection_timeout`` (seconds, int).
    """
    from pypsrp.client import Client  # lazy: keep module import cheap without pypsrp

    host = kwargs.get("host")
    if not host:
        raise TransportError(
            "CONNECT_FAILED",
            "winrm host is required",
        )

    client_kwargs: dict[str, Any] = {
        "username": kwargs.get("username"),
        "password": kwargs.get("password"),
        "ssl": bool(kwargs.get("ssl", False)),
        "auth": kwargs.get("auth") or "ntlm",
        "cert_validation": bool(kwargs.get("cert_validation", True)),
        "encryption": kwargs.get("encryption") or "auto",
    }
    port = kwargs.get("port")
    if port is not None:
        client_kwargs["port"] = int(port)

    connect_timeout = kwargs.get("connect_timeout")
    if connect_timeout is not None:
        # pypsrp Client uses connection_timeout in whole seconds.
        client_kwargs["connection_timeout"] = max(int(float(connect_timeout)), 1)

    op_timeout = kwargs.get("operation_timeout")
    if op_timeout is not None:
        client_kwargs["operation_timeout"] = max(int(float(op_timeout)), 1)

    read_timeout = kwargs.get("read_timeout")
    if read_timeout is not None:
        client_kwargs["read_timeout"] = max(int(float(read_timeout)), 1)

    # urllib3 reconnect knobs: pypsrp forwards these from ``Client`` to
    # ``WSMan``. Absent must stay absent so the library default (0 retries)
    # stands; an explicit 0 is a deliberate disable and is forwarded as such.
    # Coerced here because ``Retry(total=...)`` needs a real int / float and a
    # direct caller may not have gone through assemble_pypsrp_kwargs.
    reconnect_retries = kwargs.get("reconnection_retries")
    if reconnect_retries is not None:
        client_kwargs["reconnection_retries"] = int(reconnect_retries)
    reconnect_backoff = kwargs.get("reconnection_backoff")
    if reconnect_backoff is not None:
        client_kwargs["reconnection_backoff"] = float(reconnect_backoff)

    # Pass through enterprise auth paths/flags (and secret values when present).
    for key in (
        "certificate_pem",
        "certificate_key_pem",
        "certificate_key_password",
        "negotiate_hostname_override",
        "negotiate_service",
        "negotiate_delegate",
        "credssp_auth_mechanism",
        "credssp_disable_tlsv1_2",
        "credssp_minimum_version",
    ):
        if key in kwargs and kwargs[key] is not None:
            client_kwargs[key] = kwargs[key]

    client = Client(str(host), **client_kwargs)
    # Client is live (and may already hold a server shell) before the
    # adapter wraps it. Note it so a later hang in this factory still
    # lets connect() close the handle on wall-clock timeout.
    note_winrm_connect_handle(client)
    return PypsrpClientAdapter(client)

