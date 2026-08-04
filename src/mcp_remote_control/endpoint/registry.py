"""Process-local endpoint registry: open / list / close / ensure_connected.

Two-tier locking lets different profiles connect concurrently while each name
keeps at most one live transport. The MCP server runs sync tools in a thread
pool, so concurrent open/close/exec can race.

Locks:
- ``self._lock`` (RLock): guards ``_endpoints`` and ``_name_locks``. Held only
  for short dict critical sections. Never held across ``transport.connect()``
  or session teardown ``transport.close()`` (network IO: SSH exit/close/
  wait_closed, WinRM ``session.close()``). Registered or stale endpoints are
  popped under this lock, then closed outside it (still under the per-name
  lock). Exception: a race-loser may close its *unconnected* transport under
  the main lock — no session exists yet, so close is cheap and does no
  network IO.
- ``self._name_locks[name]`` (RLock, lazy): serializes same-name open / close /
  ensure_connected, including connect and close IO. Different names use
  different locks and proceed in parallel.

Lock ordering: never hold the main RLock while acquiring a per-name lock.
Acquire main → get-or-create the per-name lock reference → release main →
acquire per-name → re-acquire main only for short dict ops. No path holds two
per-name locks. Per-name locks are RLocks so ``ensure_connected`` may re-enter
``open`` while already holding the same-name lock.

Invariants:
- No double-live transport per name: after taking the per-name lock, open
  re-checks under the main lock and returns any live entry already registered.
- Pop under main lock, close outside it (under per-name lock) so a slow
  teardown of A does not block open of B; same-name ops still serialize.
- Per-name locks live for the registry lifetime (not popped on close) so an
  in-flight open cannot race a new open on a freshly created lock for the same
  name. ``reset_registry`` replaces the instance and discards all locks.

``clear()`` does not take per-name locks (process teardown /
``reset_registry`` only); do not call it while an open is in flight.
"""

from __future__ import annotations

import logging
import os
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
from mcp_remote_control.endpoint.caps import format_caps, merge_caps
from mcp_remote_control.identity.ssh_keys import resolve_ssh_key_paths
from mcp_remote_control.transport import (
    LocalTransport,
    SSHTransport,
    TransportError,
    WinRMTransport,
)
from mcp_remote_control.transport.base import BaseTransport
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path
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
        WITHOUT holding ``self._lock`` (see module docstring — never hold
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

    def list_open(self) -> list[Endpoint]:
        with self._lock:
            return [self._endpoints[k] for k in sorted(self._endpoints)]

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

        Idempotent: if already open and connected (and not *force*), returns it.

        Locking: brief main-RLock hold to read ``_endpoints`` and look up the
        per-name lock, then the per-name lock is acquired (main RLock released)
        and held across ``transport.connect()``. Different names use different
        per-name locks → concurrent connects. Same name serializes → no
        double-live. See module docstring.
        """
        if not profile_name or not str(profile_name).strip():
            raise ValueError("profile name is required")

        name = str(profile_name).strip()
        # Phase 1: quick idempotent check + get the per-name lock reference.
        # Main RLock held only for the dict read + lock lookup.
        with self._lock:
            existing = self._endpoints.get(name)
            if existing is not None and existing.connected and not force:
                return existing
            name_lock = self._get_or_create_name_lock(name)
        # Phase 2: serialize same-name opens (no double-live). Different names
        # use different locks → connect concurrently. The per-name lock is
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

        The per-name lock serializes same-name opens (no double-live). The
        main RLock guards only ``_endpoints`` (short critical sections).
        ``connect()`` and any previously registered transport's ``close()``
        run under the per-name lock only — not the main RLock — so different
        profiles proceed concurrently. Stale entries are popped under the main
        lock, then closed outside it (same pattern as ``close()``).
        """
        # Build profile + transport outside the main RLock (disk reads, object
        # construction — no network). Inside the per-name lock so same-name
        # callers don't duplicate work; different names build concurrently.
        home_path = _resolve_home_arg(home)
        profile = load_profile(home_path, name)
        caps = merge_caps(profile.transport, profile.caps or None)
        transport = self._build_transport(profile, connector=connector)

        # Re-check under the main RLock: another opener may have registered a
        # live endpoint while we waited on the per-name lock. Stale / partial /
        # force-replace entries are popped here and closed outside below.
        stale_ep: Endpoint | None = None
        with self._lock:
            existing = self._endpoints.get(name)
            if existing is not None and existing.connected and not force:
                # Race loser: discard our unconnected transport (close is cheap
                # with no session) and return the winner's endpoint. No network
                # IO under the main RLock.
                self._safe_close_transport_obj(transport, name)
                return existing
            # Pop previous partial/stale entry under the main RLock; close
            # outside it (still under the per-name lock).
            if existing is not None:
                self._endpoints.pop(name, None)
                stale_ep = existing

        # Close the previously-registered transport outside the main RLock
        # (still under the per-name lock). Close is network IO; holding the
        # main RLock across it would serialize different-name opens.
        if stale_ep is not None:
            self._safe_close_transport(stale_ep)

        # connect may raise TransportError / Profile*. Network IO under the
        # per-name lock only — NOT the main RLock — so different profiles
        # connect concurrently.
        transport.connect()

        cwd = _seed_cwd(profile, transport)
        probe_meta: dict[str, Any] | None = None
        if probe:
            probe_meta = _light_probe(profile, transport)
        elif profile.transport == "winrm":
            # probe disabled: keep the historical assume-runnable behavior,
            # but record the skip so downstream gates stay permissive (no
            # ps_oneshot/ps_script_fs keys → gates allow) and the Agent can
            # see the probe was skipped rather than probed-and-capable.
            probe_meta = {"ps_probe": "skipped"}
            transport.meta["winrm_ps"] = {"ps_probe": "skipped"}

        ep = Endpoint(
            name=name,
            transport_name=profile.transport,
            caps=caps,
            connected=transport.is_connected(),
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
            # Phase 1: liveness check + pop under the main RLock. Live
            # endpoints return immediately; dead/stale ones are collected for
            # close outside the main RLock (Phase 2).
            stale_ep: Endpoint | None = None
            with self._lock:
                existing = self._endpoints.get(name)
                if existing is not None and existing.connected:
                    transport = existing.transport
                    if transport is not None and transport.is_connected():
                        # SSH may still report connected after peer drop —
                        # probe liveness when available.
                        alive = getattr(transport, "is_alive", None)
                        if callable(alive):
                            try:
                                if alive():
                                    return existing
                            except Exception:  # noqa: BLE001
                                pass
                            # Dead transport: mark, pop under main RLock;
                            # close runs in Phase 2.
                            try:
                                mark = getattr(transport, "mark_dead", None)
                                if callable(mark):
                                    mark("stale_on_ensure")
                            except Exception:  # noqa: BLE001
                                pass
                            stale_ep = existing
                            self._endpoints.pop(name, None)
                        else:
                            return existing
                    else:
                        # Flagged connected but transport says no.
                        stale_ep = existing
                        self._endpoints.pop(name, None)
            # Phase 2: close dead/stale transport outside the main RLock
            # (still under the per-name lock). Close is network IO.
            if stale_ep is not None:
                self._safe_close_transport(stale_ep)
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
            force_utf8 = _truthy(
                ssh_table.get("force_utf8_remote")
                or ssh_table.get("force_utf8")
                or (profile.defaults or {}).get("force_utf8_remote")
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
        remote session invisibly — surface it at debug.
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


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "on", "enable", "enabled")


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_winrm_transport(
    profile: Profile,
    *,
    connector: WinRMConnector | None,
) -> WinRMTransport:
    """Map a WinRM profile (incl. enterprise auth) → WinRMTransport."""
    if not profile.host or not profile.username:
        raise TransportError(
            "CONNECT_FAILED",
            "winrm profile missing host or username",
        )
    winrm_cfg = profile.winrm or {}
    port = profile.port or 5985
    scheme = str(winrm_cfg.get("scheme") or "http").lower()
    ssl = scheme in ("https", "ssl", "true", "1") or bool(
        winrm_cfg.get("ssl", False)
    )
    # Port default: 5986 for https when profile port was the winrm default.
    if profile.port is None and ssl:
        port = 5986

    auth_protocol = _resolve_winrm_auth_protocol(profile, winrm_cfg)

    cert_validation = True
    scv = winrm_cfg.get("server_cert_validation")
    if scv is not None:
        cert_validation = str(scv).lower() not in (
            "ignore",
            "false",
            "0",
            "no",
        )
    elif "cert_validation" in winrm_cfg:
        cert_validation = bool(winrm_cfg.get("cert_validation"))

    timeout_ms = 15000
    raw_timeout = winrm_cfg.get("connect_timeout_ms")
    if raw_timeout is not None:
        try:
            timeout_ms = int(raw_timeout)
        except (TypeError, ValueError):
            pass

    encryption = str(
        winrm_cfg.get("message_encryption")
        or winrm_cfg.get("encryption")
        or "auto"
    )

    operation_timeout_s = _optional_int(winrm_cfg.get("operation_timeout_s"))
    read_timeout_s = _optional_int(winrm_cfg.get("read_timeout_s"))

    password = _resolve_password(profile)
    auth = profile.auth

    # Certificate paths (string form for pypsrp); never load PEM bodies here.
    certificate_pem: str | None = None
    certificate_key_pem: str | None = None
    certificate_key_password: str | None = None
    spn: str | None = None
    negotiate_hostname_override: str | None = None
    negotiate_service: str | None = None
    negotiate_delegate: bool | None = None
    credssp_auth_mechanism: str | None = None
    credssp_disable_tlsv1_2: bool | None = None
    credssp_minimum_version: int | None = None

    if auth is not None:
        if auth.cert_path is not None:
            certificate_pem = str(auth.cert_path)
        if auth.cert_key_path is not None:
            certificate_key_pem = str(auth.cert_key_path)
        if auth.cert_key_password_path is not None:
            certificate_key_password = _read_secret_file_first_line(
                auth.cert_key_password_path
            )
        spn = auth.spn
        negotiate_hostname_override = auth.negotiate_hostname_override
        negotiate_service = auth.negotiate_service
        negotiate_delegate = auth.negotiate_delegate
        credssp_auth_mechanism = auth.credssp_auth_mechanism
        credssp_disable_tlsv1_2 = auth.credssp_disable_tlsv1_2
        credssp_minimum_version = auth.credssp_minimum_version

    # Optional [winrm.credssp] table overlays profile auth fields when unset.
    credssp_tbl = winrm_cfg.get("credssp")
    if isinstance(credssp_tbl, dict):
        if credssp_auth_mechanism is None and credssp_tbl.get("auth_mechanism"):
            credssp_auth_mechanism = str(credssp_tbl.get("auth_mechanism"))
        if credssp_disable_tlsv1_2 is None and "disable_tlsv1_2" in credssp_tbl:
            credssp_disable_tlsv1_2 = bool(credssp_tbl.get("disable_tlsv1_2"))
        if credssp_minimum_version is None and "minimum_version" in credssp_tbl:
            credssp_minimum_version = _optional_int(credssp_tbl.get("minimum_version"))

    return WinRMTransport(
        host=profile.host,
        port=int(port),
        username=profile.username,
        password=password,
        auth=auth_protocol,
        ssl=ssl,
        cert_validation=cert_validation,
        connect_timeout_ms=timeout_ms,
        operation_timeout_s=operation_timeout_s,
        read_timeout_s=read_timeout_s,
        encryption=encryption,
        connector=connector,
        certificate_pem=certificate_pem,
        certificate_key_pem=certificate_key_pem,
        certificate_key_password=certificate_key_password,
        spn=spn,
        negotiate_hostname_override=negotiate_hostname_override,
        negotiate_service=negotiate_service,
        negotiate_delegate=negotiate_delegate,
        credssp_auth_mechanism=credssp_auth_mechanism,
        credssp_disable_tlsv1_2=credssp_disable_tlsv1_2,
        credssp_minimum_version=credssp_minimum_version,
    )


def _resolve_winrm_auth_protocol(
    profile: Profile, winrm_cfg: dict[str, Any]
) -> str:
    """Resolve pypsrp auth protocol from [winrm].auth and [auth].method."""
    explicit = winrm_cfg.get("auth") or winrm_cfg.get("auth_method")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()

    if profile.auth is not None:
        method = profile.auth.method
        if method == "password":
            return "ntlm"
        if method in (
            "ntlm",
            "basic",
            "negotiate",
            "kerberos",
            "credssp",
            "certificate",
        ):
            return method
    return "ntlm"


def _resolve_password(profile: Profile) -> str | None:
    """Load password from password_env or password_path; never log contents.

    Returns None when no password material is configured (connector/mock may
    still succeed). Missing env/path for a password-method profile surfaces as
    AUTH_FAILED only when the real connector needs credentials.
    """
    auth = profile.auth
    if auth is None:
        return None

    if auth.password_env:
        env_name = str(auth.password_env).strip()
        if env_name:
            val = os.environ.get(env_name)
            if val is not None:
                return val

    if auth.password_path is not None:
        return _read_secret_file_first_line(auth.password_path)

    return None


def _read_secret_file_first_line(path: Path | str) -> str | None:
    """Read first line of a secret file; never raise (defer to connect)."""
    p = Path(path).expanduser()
    try:
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            line = text.splitlines()[0] if text.splitlines() else text
            return line.rstrip("\r\n")
    except OSError:
        return None
    return None


def _ssh_known_hosts(ssh_table: dict[str, Any]) -> Any:
    """Map profile [ssh] known_hosts to asyncssh value.

    - missing → None (asyncssh default / system)
    - "none" / false / "off" → () disable checking (lab only)
    - path string → path
    """
    if "known_hosts" not in ssh_table:
        return None
    raw = ssh_table.get("known_hosts")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return () if not raw else None
    text = str(raw).strip()
    if not text:
        return None
    if text.lower() in ("none", "off", "false", "0", "disable", "disabled"):
        return ()
    return text


def _seed_cwd(profile: Profile, transport: BaseTransport) -> str | None:
    """Default cwd seed: profile defaults.cwd → transport.cwd → local getcwd.

    A leading ``~`` is expanded against the LOCAL home only for the local
    transport. For ssh/winrm, ``Path.expanduser`` would substitute the local
    ``$HOME`` (e.g. ``/Users/shiharu``) which does not exist remotely; the
    tilde is returned verbatim so the remote shell resolves it against the
    remote user's home.
    """
    raw = None
    if profile.defaults:
        raw = profile.defaults.get("cwd")
    if isinstance(raw, str) and raw.strip():
        text = str(raw).strip()
        if text.startswith("~"):
            # Only expand ~ for local; remote shells resolve ~ themselves.
            if profile.transport == "local":
                return str(Path(text).expanduser())
            return text
        return text
    # Reject bool/str(True) probe-cap bleed-through (cwd must be path-like).
    seeded = coerce_cwd_path(transport.cwd)
    if seeded:
        return seeded
    if profile.transport == "local":
        return os.getcwd()
    return None


def _light_probe(profile: Profile, transport: BaseTransport) -> dict[str, Any]:
    """Lightweight open-time probe. Failure yields partial status; never raises."""
    data: dict[str, Any] = {
        "status": "ok",
        "transport": profile.transport,
    }
    if transport.home:
        data["home"] = transport.home
    if transport.cwd:
        data["pwd"] = transport.cwd
    if profile.transport == "local":
        data["user"] = os.environ.get("USER") or os.environ.get("USERNAME")
        # Local open-summary seeds (shell / uname / locale).
        shell_path = os.environ.get("SHELL")
        if shell_path:
            data["shell_path"] = shell_path
            base = shell_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base:
                data["shell_base"] = base
        try:
            import platform

            data["uname"] = f"{platform.system()}-{platform.machine()}"
        except Exception:  # noqa: BLE001
            pass
        try:
            import locale as _locale

            enc = _locale.getpreferredencoding(False) or ""
            if enc:
                data["text_encoding"] = enc
        except Exception:  # noqa: BLE001
            pass
    if profile.host:
        data["host"] = profile.host

    # Merge transport.meta seeds when present (os / shell / ps_version / …).
    meta = getattr(transport, "meta", None) or {}
    for key in ("os", "shell", "ps_version", "auth", "dialect", "shell_base", "shell_path"):
        if key in meta and meta[key] is not None:
            data[key] = meta[key]

    # Transport-specific best-effort probe (e.g. WinRM collect_probe).
    collector = getattr(transport, "collect_probe", None)
    if callable(collector):
        try:
            extra = collector() or {}
            if isinstance(extra, dict):
                # Keep outer status=ok unless extra marks partial/fail.
                status = extra.pop("status", None)
                data.update(extra)
                if status in ("partial", "fail", "error"):
                    data["status"] = status
                elif "error" in data and data.get("status") == "ok":
                    data["status"] = "partial"
        except Exception as exc:  # noqa: BLE001 — probe must not fail open
            data["status"] = "partial"
            data["error"] = _short_probe_err(exc)

    if meta.get("probe_status") == "partial":
        data["status"] = "partial"
        if meta.get("probe_error") and "error" not in data:
            data["error"] = meta["probe_error"]

    # Ensure dialect is present for screen/exec wiring.
    if "dialect" not in data or not data.get("dialect"):
        try:
            from mcp_remote_control.shell.dialect import resolve_dialect

            data["dialect"] = resolve_dialect(
                shell_base=str(data.get("shell_base") or "") or None,
                shell_path=str(data.get("shell_path") or "") or None,
                shell_family=str(data.get("shell_family") or "") or None,
                busybox=bool(data.get("busybox")),
                flags=data,
                os_name=str(data.get("os") or "") or None,
            )
        except Exception:  # noqa: BLE001
            pass

    return data


def _short_probe_err(exc: BaseException, limit: int = 120) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
