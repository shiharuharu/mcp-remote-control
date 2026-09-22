"""Process-local endpoint registry: open / list / close / ensure_connected.

Two-tier locking lets profiles connect concurrently while each name keeps at
most one live transport; sync tools run in a thread pool, so open/close/exec
can race.

Locks:
- ``self._lock`` (RLock) guards ``_endpoints`` and ``_name_locks`` briefly, and
  is never held across ``transport.connect()``, session teardown
  ``transport.close()``, or a liveness probe (``is_connected`` / ``is_alive`` /
  ``mark_dead``): probes serialize on the transport ``_op_lock`` and can wait
  behind a long ``run_command``, stalling other registry critical sections.
  Pop stale entries under main and close them outside it; a race-loser may
  close its *unconnected* transport under main (no session yet, so no IO).
- ``self._name_locks[name]`` (RLock, lazy) serializes same-name open / close /
  ensure_connected, connect and close IO included - a name never has two live
  transports - while different names run in parallel. Never acquire one while
  holding the main RLock: acquire main -> get-or-create the per-name lock
  reference -> release main -> acquire per-name -> re-acquire main only for
  short dict ops. No path holds two per-name locks; they are RLocks, so
  ``ensure_connected`` may re-enter ``open``.
- **Transport ``_op_lock``** (per-instance RLock) wraps exec / sftp /
  mark_dead / connect / close, and is what covers concurrent ``run_command``
  after ensure returns - registry name locks do not: name locks = lifecycle
  registration, op lock = session liveness ops.

Invariants:
- Per-name locks live for the registry lifetime, never popped on close, so an
  in-flight open cannot race a new one on a lock created for the same name
  meanwhile. ``reset_registry`` discards all locks; ``clear()`` skips per-name
  locks and must not run while an open is in flight.
- Popping a dead or stale transport snapshots that generation's screen/ps
  session ids and closes them via ``close_ids`` outside the main RLock;
  ``close_if_same`` does the same for the matched generation only, never a
  name-wide ``close_for_endpoint`` after a new open under the same name. Plain
  ``close`` does **not** tear down sessions - ``close_endpoint`` owns that path.
- Dead-path cleanup must use ``close_if_same(name, handle)`` (object identity),
  never name-only ``close(name)``: ``open`` Phase-1 may return a live
  ``Endpoint`` under only the main RLock while a concurrent ``mark_dead`` +
  ``ensure_connected`` retires it. ``ensure_endpoint`` likewise returns before
  screen/ps open their PTY or runspace, so callers re-validate with
  :meth:`generation_still_open` before publishing the session - on mismatch
  close the orphan handle and return ``NOT_CONNECTED``.

Construction lives in ``connect``, probing in ``probe``; the module re-exports
``_build_winrm_transport``, ``_resolve_password`` and ``_seed_cwd``.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_remote_control.config import (
    Profile,
    list_profiles,
    load_profile,
    resolve_home,
)
from mcp_remote_control.endpoint.caps import (
    coerce_toml_bool,
    format_caps,
    merge_caps,
)
from mcp_remote_control.endpoint.connect import (
    _build_winrm_transport as _build_winrm_transport,
    _first_defined as _first_defined,
    _optional_int as _optional_int,
    _read_secret_file_first_line as _read_secret_file_first_line,
    _resolve_password as _resolve_password,
    _resolve_winrm_auth_protocol as _resolve_winrm_auth_protocol,
    _seed_cwd as _seed_cwd,
    _ssh_known_hosts as _ssh_known_hosts,
)
from mcp_remote_control.endpoint.probe import (
    _light_probe as _light_probe,
    _resolve_open_probe_mode as _resolve_open_probe_mode,
    _short_probe_err as _short_probe_err,
)
from mcp_remote_control.identity.ssh_keys import resolve_ssh_key_paths
from mcp_remote_control.transport import (
    LocalTransport,
    SSHTransport,
    TransportError,
)
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.ssh import SSHConnector
from mcp_remote_control.transport.winrm import WinRMConnector

# Generic injectable connector (ssh or winrm depending on profile).
AnyConnector = Callable[..., Any]

_log = logging.getLogger(__name__)


@dataclass
class Endpoint:
    """Runtime handle for a connected (or connecting) profile."""

    name: str
    transport_name: str
    caps: dict[str, bool]
    connected: bool = False
    profile: Profile | None = None
    transport: BaseTransport | None = None
    cwd: str | None = None
    probe: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def caps_token(self) -> str:
        return format_caps(self.caps)


# ---------------------------------------------------------------------------
# Refused-link classification
# ---------------------------------------------------------------------------

# pypsrp raises AuthenticationError for any HTTP 401 the WSMan endpoint returns
# (pypsrp ``wsman.py`` ``_send_request``): the server refused a request from a
# session that had already authenticated, so the local auth/encryption context
# is unusable and every later call on that session is refused too. The fs
# client's link-failure predicate (``transport/winrm_files.py``
# ``_LINK_FAILURE_TYPES``) does not list it - it matches
# ``WinRMTransportError``, and AuthenticationError derives from ``WinRMError``
# instead - so the transport keeps reporting connected and ``open`` reconnects
# nothing. Duck-typed on (module, qualname) so pypsrp need not be importable.
_WSMAN_REFUSAL_TYPES: frozenset[tuple[str, str]] = frozenset(
    {("pypsrp.exceptions", "AuthenticationError")}
)

# pypsrp renders a rejection's structured ("http", status, body) triple as
# ``Bad HTTP response returned from the server. Code: 400, Content: ''`` and
# the transport's identity probe stores only that string, so a reconnect
# failure's HTTP status is read back from the text.
_HTTP_STATUS_RE = re.compile(r"\bCode:\s*(\d{3})\b")


def _is_wsman_refusal(exc: BaseException | None) -> bool:
    """True when *exc* or a chained cause is a WSMan-layer auth refusal.

    Core sees the backend's ``FsError``; the session-layer failure that
    produced it stays reachable through ``__cause__`` / ``__context__``. The
    walk is bounded - a hostile chain must not become an infinite loop.
    """
    seen = 0
    while exc is not None and seen < 4:
        for cls in type(exc).__mro__:
            if (
                getattr(cls, "__module__", None),
                getattr(cls, "__qualname__", ""),
            ) in _WSMAN_REFUSAL_TYPES:
                return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def retire_refused_link(transport: Any, exc: BaseException | None) -> bool:
    """Retire a link the transport's own failure bookkeeping did not notice.

    Called by Core after an op failed with *exc*. True when the transport was
    marked dead, i.e. the next op reconnects instead of reusing a session the
    server is refusing.

    The transport owns the verdict for every failure it classifies; this covers
    the refusal it does not (see ``_WSMAN_REFUSAL_TYPES``). Retiring the link
    is the same end state the transport's own fs link callback produces
    (``WinRMTransport._on_fs_link_failure`` -> ``_mark_link_dead``): mark dead,
    record the machine-readable tokens, dispose the session. Never raises -
    a failed retirement must not replace the op's own error.
    """
    if transport is None or not _is_wsman_refusal(exc):
        return False
    # Prefer the transport's own link-death handler so the meta token contract
    # stays defined in one place; a transport without it still gets an honest
    # flag from the public mark_dead.
    retire = getattr(transport, "_mark_link_dead", None)
    try:
        if callable(retire):
            retire()
        else:
            transport.mark_dead("link lost")
    except Exception:  # noqa: BLE001 - best-effort, caller keeps its own error
        return False
    return True


def _open_failure_tokens(
    reason: str | None, probe_meta: dict[str, Any] | None
) -> dict[str, Any]:
    """Classified tokens for a failed open/reconnect, for the caller's row.

    A reconnect can fail because nothing answered (unreachable host, DNS,
    refused socket) or because something answered and *rejected* the request
    (a gateway's own error page, a stale WinRM auth context). Both surface as
    NOT_CONNECTED, so the classification travels as tokens an Agent can branch
    on instead of prose: without them "the host is down" and "the request was
    rejected - open again" are the same row.

    ``rejected`` follows the 4xx rule of :func:`is_winrm_refusal`
    (``transport/winrm_exec.py``): a 4xx means the receiver refused *this
    request*, while a 5xx means something failed *while handling* it - the
    request may already have been dispatched, so that class is reported as a
    bare ``http_status`` and never as a refusal.
    """
    tokens: dict[str, Any] = {}
    status = str((probe_meta or {}).get("status") or "").strip().lower()
    if status in ("fail", "error"):
        tokens["probe_failed"] = 1
    match = _HTTP_STATUS_RE.search(reason or "")
    if match:
        http_status = int(match.group(1))
        tokens["http_status"] = http_status
        if 400 <= http_status < 500:
            tokens["rejected"] = 1
    return tokens


# ``TransportError.details`` keys that name the *class* of a failed connect or
# of a link the transport retired. They travel from where the refusal is
# classified to every caller surface, so the vocabulary has to be defined once:
# a caller that copies a subset (or a surface that copies none) silently turns
# "the intermediary refused this request" back into an undifferentiated failure,
# which is the ambiguity these tokens exist to remove.
CONNECT_TOKEN_KEYS: tuple[str, ...] = ("probe_failed", "rejected", "http_status", "host")


def connect_failure_fields(exc: TransportError) -> dict[str, Any]:
    """Copy the classified connect/refusal tokens out of *exc* for a result row.

    Every Core surface that reports a failed connect - ``endpoint open``, and
    the lazy-connect legs of exec / ps / fs - funnels through here so a caller
    sees the same fields regardless of which tool it used.
    """
    details = exc.details if isinstance(exc.details, dict) else {}
    return {key: details[key] for key in CONNECT_TOKEN_KEYS if key in details}


def _endpoint_transport_live(
    ep: Endpoint, *, dead_reason: str = "stale"
) -> bool:
    """True when *ep* has a usable transport (same checks as ensure_connected).

    Never trust ``Endpoint.connected`` alone: after ``mark_dead`` the cache
    flag can stay True while ``transport.is_connected()`` is False. Syncs
    ``ep.connected`` to the observed liveness so list does not stick on a
    stale ``open=1``. When an optional ``is_alive`` probe fails, best-effort
    ``mark_dead`` then returns False.

    **Caller must NOT hold the registry main RLock.** Probes are
    local socket/flag checks (no network RTT), but SSH/WinRM ``is_connected``
    and this helper's dead-path ``mark_dead`` acquire the transport
    ``_op_lock`` and can wait behind a long ``run_command``. Holding main
    across that wait starves other names' Phase-1 open / dict ops.
    """
    transport = ep.transport
    if transport is None:
        ep.connected = False
        return False
    try:
        flagged = bool(transport.is_connected())
    except Exception:  # noqa: BLE001 - treat probe failure as dead
        flagged = False
    if not flagged:
        ep.connected = False
        return False
    # SSH and WinRM expose is_alive beyond the connected flag.
    # Their is_connected already includes is_alive; keep the extra probe for
    # backends that split the two, matching ensure_connected.
    alive_fn = getattr(transport, "is_alive", None)
    if not callable(alive_fn):
        ep.connected = True
        return True
    try:
        if alive_fn():
            ep.connected = True
            return True
    except Exception:  # noqa: BLE001
        pass
    # mark_dead is transport-serial (op_lock). Safe only because the
    # caller released the registry main RLock first (snapshot-then-probe).
    try:
        mark = getattr(transport, "mark_dead", None)
        if callable(mark):
            mark(dead_reason)
    except Exception:  # noqa: BLE001
        pass
    ep.connected = False
    return False


class EndpointRegistry:
    """In-process map of open endpoints (keyed by profile name)."""

    def __init__(self) -> None:
        self._endpoints: dict[str, Endpoint] = {}
        # Per-name RLocks, lazily created. See module docstring for locking
        # scheme, ordering, and lifecycle.
        self._name_locks: dict[str, threading.RLock] = {}
        # Guards _endpoints and _name_locks (short critical sections only).
        # Never held across connect() or close() network IO.
        self._lock = threading.RLock()
        # Optional connector factories for build_transport (tests / injection).
        self.ssh_connector: SSHConnector | None = None
        self.winrm_connector: WinRMConnector | None = None

    def _get_or_create_name_lock(self, name: str) -> threading.RLock:
        """Return the per-name RLock for *name*, creating it if needed.

        Caller MUST hold ``self._lock``: only the dict lookup/insert is
        guarded; the returned lock is then acquired/released by the caller
        WITHOUT holding ``self._lock`` (see module docstring - never hold
        the main RLock while acquiring a per-name lock).
        """
        lk = self._name_locks.get(name)
        if lk is None:
            lk = threading.RLock()
            self._name_locks[name] = lk
        return lk

    def get(self, name: str) -> Endpoint | None:
        with self._lock:
            return self._endpoints.get(name)

    def generation_still_open(self, handle: Endpoint | None) -> bool:
        """True when *handle* is still registered and its transport is live.

        Screen/ps open fence: call after PTY/runspace create and again
        immediately before session registry ``add``. On False the caller must
        close any orphan handle and must not publish a session for this
        generation. Identity check under the main RLock; ``is_connected``
        runs **outside** main.
        """
        if handle is None:
            return False
        name = str(handle.name or "").strip()
        if not name:
            return False
        with self._lock:
            if self._endpoints.get(name) is not handle:
                return False
        transport = handle.transport
        if transport is None:
            return False
        try:
            return bool(transport.is_connected())
        except Exception:  # noqa: BLE001 - treat probe failure as dead
            return False

    def list_open(self) -> list[Endpoint]:
        """Return open endpoints, syncing ``connected`` from each transport.

        ``Endpoint.connected`` can lag ``mark_dead``; list refreshes the flag
        from ``transport.is_connected()`` (local check) so callers do not
        report a long-lived false ``open=1``.

        Snapshot under the main RLock, then probe **outside** it:
        ``is_connected`` may call ``mark_dead`` (transport ``_op_lock``) and
        must not stall other registry critical sections for a long
        ``run_command`` on any one endpoint.
        """
        with self._lock:
            out = [self._endpoints[k] for k in sorted(self._endpoints)]
        for ep in out:
            # is_connected only (no separate is_alive round) - enough to
            # clear mark_dead zombies; full open path re-probes on use.
            transport = ep.transport
            if transport is None:
                ep.connected = False
                continue
            try:
                ep.connected = bool(transport.is_connected())
            except Exception:  # noqa: BLE001
                ep.connected = False
        return out

    def open(
        self,
        profile_name: str,
        *,
        home: Path | str | None = None,
        probe: bool = True,
        connector: AnyConnector | None = None,
        force: bool = False,
    ) -> Endpoint:
        """Load profile, connect transport, register endpoint.

        Idempotent: if already open *and the transport is live* (and not
        *force*), returns it. A stale ``Endpoint.connected`` flag alone is
        not enough - same liveness path as ``ensure_connected``
        (``is_connected`` + optional ``is_alive``); dead entries are popped
        and reconnected under the per-name lock.

        Locking: brief main-RLock hold to read ``_endpoints`` and look up the
        per-name lock; liveness probes run **outside** main so a long
        ``run_command`` holding another endpoint's ``_op_lock`` cannot starve
        Phase-1. Then the per-name lock is acquired and held across
        ``transport.connect()``. Different names use different per-name locks
        -> concurrent connects. Same name serializes -> no double-live. See
        module docstring.
        """
        if not profile_name or not str(profile_name).strip():
            raise ValueError("profile name is required")

        name = str(profile_name).strip()
        # Phase 1: snapshot dict + per-name lock ref under main RLock only.
        # Liveness is probed OUTSIDE main: is_connected/mark_dead
        # may wait on transport op_lock behind a long run_command.
        with self._lock:
            existing = self._endpoints.get(name)
            name_lock = self._get_or_create_name_lock(name)
        if existing is not None and not force:
            if _endpoint_transport_live(
                existing, dead_reason="stale_on_open"
            ):
                # Generation pin: still the registered object?
                with self._lock:
                    if self._endpoints.get(name) is existing:
                        return existing
                # Replaced while we probed - fall through under per-name lock.
            # Zombie / mark_dead: fall through under per-name lock to
            # pop + real reconnect (do not return early on cache flag).
        # Phase 2: serialize same-name opens (no double-live). Different names
        # use different locks -> connect concurrently. The per-name lock is
        # held across connect (network IO); the main RLock is NOT, so other
        # names keep progressing.
        with name_lock:
            return self._open_locked(
                name, home=home, probe=probe, connector=connector, force=force
            )

    def _open_locked(
        self,
        name: str,
        *,
        home: Path | str | None,
        probe: bool,
        connector: AnyConnector | None,
        force: bool,
    ) -> Endpoint:
        """Connect + register. Caller already holds the per-name lock for *name*.

        The per-name lock serializes same-name open/close/ensure (no double-live
        registration). It does **not** skip build work for race losers: a waiter
        that enters after a winner may still load the profile and construct a
        transport, then discard that unconnected transport when the live
        registered endpoint is re-checked. The main RLock guards only
        ``_endpoints`` (short critical sections). ``connect()`` and any
        previously registered transport's ``close()`` run under the per-name
        lock only - not the main RLock - so different profiles proceed
        concurrently. Stale entries are popped under the main lock, then closed
        outside it (same pattern as ``close()``).
        """
        # Build profile + transport outside the main RLock (disk reads, object
        # construction - no network). Still under the per-name lock so same-
        # name open/close/ensure are serialized; different names build
        # concurrently. Race losers still full-build here, then discard the
        # unconnected transport after the live winner re-check below.
        home_path = _resolve_home_arg(home)
        profile = load_profile(home_path, name)
        caps = merge_caps(profile.transport, profile.caps or None)
        transport = self._build_transport(profile, connector=connector)

        # Re-check liveness OUTSIDE the main RLock, then short
        # main-lock critical sections for identity-pinned dict ops only.
        # Under the per-name lock no concurrent same-name open/close/ensure
        # can replace the entry (clear() teardown is the exception). Liveness
        # matches ensure_connected (not Endpoint.connected alone).
        stale_ep: Endpoint | None = None
        with self._lock:
            existing = self._endpoints.get(name)
        if existing is not None and not force:
            if _endpoint_transport_live(
                existing, dead_reason="stale_on_open"
            ):
                with self._lock:
                    if self._endpoints.get(name) is existing:
                        # Race loser against a live winner: discard our
                        # unconnected transport (close is cheap with no
                        # session) and return the winner.
                        self._safe_close_transport_obj(transport, name)
                        return existing
                # Entry gone (e.g. clear) while probing - fall through to
                # fresh connect.
            else:
                # Dead/stale: pop under main if still this generation.
                with self._lock:
                    if self._endpoints.get(name) is existing:
                        self._endpoints.pop(name, None)
                        stale_ep = existing
        elif existing is not None:
            # force=True: replace whatever is registered for this name.
            with self._lock:
                stale_ep = self._endpoints.pop(name, None)

        # Close the previously-registered transport + generation-fenced
        # screen/ps outside the main RLock (still under the per-name lock).
        # Close is network IO; holding the main RLock across it would
        # serialize different-name opens. Snapshot+close_ids before reopen
        # registers a new transport so zombie sessions cannot outlive the
        # dead generation (same fence as close_endpoint).
        if stale_ep is not None:
            self._retire_stale_endpoint(stale_ep)

        # connect may raise TransportError / Profile*. Network IO under the
        # per-name lock only - NOT the main RLock - so different profiles
        # connect concurrently.
        transport.connect()

        cwd = _seed_cwd(profile, transport)
        probe_meta: dict[str, Any] | None = None
        winrm_probe_mode = _resolve_open_probe_mode(
            profile, home_path, explicit_probe=probe
        )
        if profile.transport == "winrm":
            if winrm_probe_mode == "skip":
                # probe=False / mode=skip: historical assume-runnable; record
                # skip so gates stay permissive and Agent sees skipped vs
                # probed. Risk: no identity RTT (lab acceleration only).
                probe_meta = {"ps_probe": "skipped"}
                transport.meta["winrm_ps"] = {"ps_probe": "skipped"}
            else:
                # full (default) or light - collect_probe intensity via mode.
                probe_meta = _light_probe(
                    profile, transport, probe_mode=winrm_probe_mode
                )
        elif probe:
            # Non-winrm: historical probe=bool (SSH/local).
            probe_meta = _light_probe(profile, transport)

        # connect() may return without raising while
        # peer-gone / probe mark_dead left is_connected False. Never insert a
        # DOA handle - ensure_connected and direct reg.open would otherwise
        # return a zombie (false success / list open=1). Dispose best-effort
        # then raise before the dict insert; open_endpoint post-check remains
        # defense-in-depth for races after a live return.
        try:
            live_after_open = bool(transport.is_connected())
        except Exception:  # noqa: BLE001 - treat probe failure as dead
            live_after_open = False
        if not live_after_open:
            dead_reason: str | None = None
            t_meta = getattr(transport, "meta", None) or {}
            if isinstance(t_meta, dict):
                raw_dead = t_meta.get("dead_reason") or t_meta.get("probe_error")
                if raw_dead:
                    dead_reason = str(raw_dead)[:200]
            if not dead_reason and isinstance(probe_meta, dict):
                raw_probe = probe_meta.get("error") or probe_meta.get("probe_error")
                if raw_probe:
                    dead_reason = str(raw_probe)[:200]
            self._safe_close_transport_obj(transport, name)
            details: dict[str, Any] = {"profile": name}
            if profile.host:
                details["host"] = profile.host
            # Probe-vs-connect and refusal class, so a caller of this open
            # (fs/exec/ps lazy connect) can branch on fields, not on prose.
            details.update(_open_failure_tokens(dead_reason, probe_meta))
            raise TransportError(
                "NOT_CONNECTED",
                dead_reason or "transport not connected after open",
                details=details,
            )

        ep = Endpoint(
            name=name,
            transport_name=profile.transport,
            caps=caps,
            connected=True,
            profile=profile,
            transport=transport,
            cwd=cwd,
            probe=probe_meta,
            meta={
                "host": profile.host,
                "label": profile.label,
            },
        )
        # Register under the main RLock. We still hold the per-name lock, so
        # no concurrent same-name open/ensure_connected/close can interleave
        # here (they block on the per-name lock).
        with self._lock:
            self._endpoints[name] = ep
        return ep

    def close(self, ep_name: str) -> Endpoint | None:
        """Disconnect and remove *ep_name*. Returns removed endpoint or None.

        Acquires the per-name lock so close serializes with same-name
        ``open``/``ensure_connected`` and cannot leave a mid-connect open
        registering after close returns.
        """
        if not ep_name:
            return None
        name = str(ep_name).strip()
        with self._lock:
            name_lock = self._get_or_create_name_lock(name)
        with name_lock:
            with self._lock:
                ep = self._endpoints.pop(name, None)
                if ep is None:
                    return None
            # Best-effort close outside the main RLock (and still inside the
            # per-name lock): the endpoint is already removed from the
            # registry, and no concurrent same-name open/close can re-register
            # while we hold the per-name lock.
            self._safe_close_transport(ep)
            ep.connected = False
            return ep

    def close_if_same(
        self, ep_name: str, handle: Endpoint | None
    ) -> Endpoint | None:
        """Disconnect and remove *ep_name* only when the registered object is *handle*.

        Generation fence for open dead-path cleanup. ``open``
        Phase-1 can return a live ``Endpoint`` under only the main RLock;
        concurrent ``mark_dead`` + ``ensure_connected`` may then pop that
        object and register a newer generation under the same name. Callers
        that later find their handle dead must not ``close(name)`` (name-pop
        would kill the newer transport). Identity compare under the same
        locking scheme as :meth:`close` closes only the matching generation.

        When the pin matches, also snapshot+close that generation's screen/ps
        sessions - same fence as ``_retire_stale_endpoint`` /
        explicit ``close_endpoint``. Snapshot runs after the identity-pinned
        pop under the per-name lock so only the dying generation's name-keyed
        ids are collected; a pin miss returns None with no session teardown
        (concurrent newer generation under the same name is left intact).

        Returns the removed endpoint, or None when the name is empty, *handle*
        is None, the name is not open, or a different generation is registered.
        """
        if not ep_name or handle is None:
            return None
        name = str(ep_name).strip()
        with self._lock:
            name_lock = self._get_or_create_name_lock(name)
        with name_lock:
            with self._lock:
                current = self._endpoints.get(name)
                if current is not handle:
                    return None
                ep = self._endpoints.pop(name, None)
                if ep is None:
                    return None
            # Matched dying generation: snapshot+close_ids + transport
            # teardown outside the main RLock (still under per-name lock so
            # same-name open cannot re-register mid-close of this generation).
            # Reuses _retire_stale_endpoint - never name-wide close_for_endpoint.
            self._retire_stale_endpoint(ep)
            ep.connected = False
            return ep

    def ensure_connected(
        self,
        ep_or_profile: str,
        *,
        home: Path | str | None = None,
        probe: bool = True,
        connector: AnyConnector | None = None,
    ) -> Endpoint:
        """Return open endpoint, opening (lazy connect) if needed.

        Holds the per-name lock across liveness check, pop, close, and reopen
        so a concurrent same-name ``close``/``open`` cannot observe a
        half-torn-down entry or double-open. The per-name lock is an RLock,
        so re-entering ``self.open`` is safe. Dead/stale transports are popped
        under the main RLock and closed outside it (still under the per-name
        lock) so a slow teardown of A does not block open of B.
        """
        name = str(ep_or_profile).strip()
        with self._lock:
            name_lock = self._get_or_create_name_lock(name)
        with name_lock:
            # Phase 1: snapshot under main, probe OUTSIDE main,
            # then identity-pinned return or pop under main. Shared helper
            # keeps open and ensure_connected on the same is_connected /
            # is_alive path - never wait on transport op_lock while holding
            # the registry main RLock.
            stale_ep: Endpoint | None = None
            with self._lock:
                existing = self._endpoints.get(name)
            if existing is not None:
                if _endpoint_transport_live(
                    existing, dead_reason="stale_on_ensure"
                ):
                    with self._lock:
                        if self._endpoints.get(name) is existing:
                            return existing
                    # Entry replaced/cleared while probing - fall through.
                else:
                    with self._lock:
                        if self._endpoints.get(name) is existing:
                            stale_ep = existing
                            self._endpoints.pop(name, None)
            # Phase 2: close dead/stale transport + its screen/ps outside the
            # main RLock (still under the per-name lock). Close is network IO.
            # Snapshot ids for this generation before reopen so zombie PTYs /
            # runspaces cannot survive mark_dead -> ensure reconnect.
            if stale_ep is not None:
                self._retire_stale_endpoint(stale_ep)
            # Phase 3: re-open under the same per-name RLock (re-entrant).
            return self.open(
                name,
                home=home,
                probe=probe,
                connector=connector,
            )

    def clear(self) -> None:
        """Close all endpoints (tests / process teardown)."""
        with self._lock:
            eps = list(self._endpoints.values())
            self._endpoints.clear()
        # Best-effort close outside the lock to avoid holding it across IO.
        for ep in eps:
            self._safe_close_transport(ep)
            ep.connected = False

    def _build_transport(
        self,
        profile: Profile,
        *,
        connector: AnyConnector | None,
    ) -> BaseTransport:
        if profile.transport == "local":
            return LocalTransport()
        if profile.transport == "ssh":
            if not profile.host or not profile.username:
                raise TransportError(
                    "CONNECT_FAILED",
                    "ssh profile missing host or username",
                )
            keys = resolve_ssh_key_paths(profile)
            timeout_ms = 15000
            ssh_table = profile.ssh or {}
            raw_timeout = ssh_table.get("connect_timeout_ms")
            if raw_timeout is not None:
                try:
                    timeout_ms = int(raw_timeout)
                except (TypeError, ValueError):
                    pass
            ssh_conn = (
                connector if connector is not None else self.ssh_connector
            )
            # Auth secrets (paths only in profile; load bodies for asyncssh).
            password = None
            passphrase = None
            auth = profile.auth
            if auth is not None:
                method = (auth.method or "").lower()
                if method in ("password", "ssh_password"):
                    password = _resolve_password(profile)
                if auth.passphrase_path is not None:
                    passphrase = _read_secret_file_first_line(auth.passphrase_path)
            # known_hosts / encoding / keepalive from [ssh]
            known_hosts = _ssh_known_hosts(ssh_table)
            keepalive = ssh_table.get("keepalive_interval_s")
            keepalive_s = None
            if keepalive is not None:
                try:
                    keepalive_s = float(keepalive)
                except (TypeError, ValueError):
                    keepalive_s = None
            encoding = ssh_table.get("encoding") or (
                profile.defaults or {}
            ).get("encoding")
            text_encoding = str(encoding).strip() if encoding else None
            # First defined key wins: TOML false/0 must not fall through.
            force_utf8 = coerce_toml_bool(
                _first_defined(
                    ssh_table.get("force_utf8_remote"),
                    ssh_table.get("force_utf8"),
                    (profile.defaults or {}).get("force_utf8_remote"),
                )
            )
            return SSHTransport(
                host=profile.host,
                port=profile.port or 22,
                username=profile.username,
                client_keys=keys,
                connect_timeout_ms=timeout_ms,
                known_hosts=known_hosts,
                password=password,
                passphrase=passphrase,
                keepalive_interval_s=keepalive_s,
                text_encoding=text_encoding,
                force_utf8_remote=force_utf8,
                connector=ssh_conn,
            )
        if profile.transport == "winrm":
            return _build_winrm_transport(
                profile,
                connector=(
                    connector if connector is not None else self.winrm_connector
                ),
            )
        raise TransportError(
            "UNSUPPORTED",
            f"unknown transport {profile.transport!r}",
        )

    @staticmethod
    def _safe_close_transport(ep: Endpoint) -> None:
        EndpointRegistry._safe_close_transport_obj(ep.transport, ep.name)

    @staticmethod
    def _safe_close_transport_obj(
        transport: BaseTransport | None, name: str | None = None
    ) -> None:
        """Best-effort ``transport.close()``; never raises. Used to discard a
        freshly-built transport when another opener won the race (and to close
        a registered endpoint's transport). A swallowed failure leaks a
        remote session invisibly - surface it at debug.
        """
        if transport is None:
            return
        try:
            transport.close()
        except Exception as exc:  # noqa: BLE001
            if name:
                _log.debug("transport close failed for %s: %s", name, exc)
            else:
                _log.debug("transport close failed: %s", exc)

    @staticmethod
    def _snapshot_attached_session_ids(
        ep_name: str,
    ) -> tuple[list[str], list[str]]:
        """Snapshot screen/ps session ids for *ep_name* (generation fence).

        Lazy-imports core helpers so the endpoint package does not import
        screen/ps at module load (avoids cycles with ensure_endpoint).
        Best-effort; never raises.
        """
        screen_ids: list[str] = []
        ps_ids: list[str] = []
        try:
            from mcp_remote_control.core.screen_ops import (
                snapshot_endpoint_session_ids as _scr_ids,
            )

            screen_ids = list(_scr_ids(ep_name))
        except Exception:  # noqa: BLE001
            screen_ids = []
        try:
            from mcp_remote_control.core.ps_ops import (
                snapshot_endpoint_session_ids as _ps_ids,
            )

            ps_ids = list(_ps_ids(ep_name))
        except Exception:  # noqa: BLE001
            ps_ids = []
        return screen_ids, ps_ids

    @staticmethod
    def _close_attached_session_ids(
        screen_ids: list[str],
        ps_ids: list[str],
    ) -> None:
        """Close only snapshotted screen/ps ids. Best-effort; never raises.

        Must run outside the registry main RLock (session close is IO).
        Never use name-wide close_for_endpoint after a same-name reopen -
        only the pre-reopen generation ids belong in the lists.
        """
        if screen_ids:
            try:
                from mcp_remote_control.core.screen_ops import (
                    close_sessions_by_ids as _scr_close,
                )

                _scr_close(screen_ids)
            except Exception:  # noqa: BLE001
                pass
        if ps_ids:
            try:
                from mcp_remote_control.core.ps_ops import (
                    close_sessions_by_ids as _ps_close,
                )

                _ps_close(ps_ids)
            except Exception:  # noqa: BLE001
                pass

    def _retire_stale_endpoint(self, stale_ep: Endpoint) -> None:
        """Close a popped dead/stale endpoint's transport and its screen/ps.

        Caller must have already removed *stale_ep* from ``_endpoints`` and
        must NOT hold the main RLock. Snapshot session ids for the dying
        generation before transport close, then ``close_ids`` only those -
        concurrent same-name reopen sessions are not in the snapshot.

        Used by reconnect pop (``open`` / ``ensure_connected``) and by
        identity-pinned ``close_if_same`` (open dead-path). Explicit
        ``close()`` does **not** use this path: ``close_endpoint`` owns the
        snapshot/teardown for user-facing close so field counts
        (screens_closed / ps_closed) stay accurate.
        """
        name = stale_ep.name
        screen_ids, ps_ids = self._snapshot_attached_session_ids(name)
        self._safe_close_transport(stale_ep)
        self._close_attached_session_ids(screen_ids, ps_ids)


# ---------------------------------------------------------------------------
# Process singleton
# ---------------------------------------------------------------------------

_registry: EndpointRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> EndpointRegistry:
    """Return the process-wide endpoint registry (created on first use)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = EndpointRegistry()
        return _registry


def reset_registry() -> EndpointRegistry:
    """Replace the process registry with a fresh empty one (tests)."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            try:
                _registry.clear()
            except Exception:  # noqa: BLE001
                pass
        _registry = EndpointRegistry()
        return _registry


def ensure_endpoint(
    ep: str,
    *,
    home: Path | str | None = None,
    probe: bool = True,
    connector: AnyConnector | None = None,
) -> Endpoint:
    """Lazy-connect helper for exec / fs / screen / ps tool paths."""
    return get_registry().ensure_connected(
        ep,
        home=home,
        probe=probe,
        connector=connector,
    )


def list_known_profiles(home: Path | str | None = None) -> list[str]:
    """Profile names from config home (disk)."""
    return list_profiles(_resolve_home_arg(home))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_home_arg(home: Path | str | None) -> Path:
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()
