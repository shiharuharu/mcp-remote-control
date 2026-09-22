"""WinRM transport: oneshot exec, persistent runspace, and fs hook over pypsrp.

Owns profile->pypsrp auth kwargs assembly, remote command/argv execution, and
PowerShell runspace open/invoke/close. A connector callable may supply the
session object (default constructs a real ``pypsrp.client.Client``).

Auth: ``password`` / ``ntlm`` / ``basic`` / ``negotiate`` / ``kerberos`` /
``credssp`` / ``certificate`` map to the same-named pypsrp ``auth=`` value, with
``certificate`` **requiring SSL**; extras live in :func:`assemble_pypsrp_kwargs`.
Profile path / password fields resolve to paths or loaded secret values only
inside that builder - never into Agent-track fields or ``repr``.

Timeouts (MaxShellsPerUser)
---------------------------

Every oneshot call and runspace open/close runs under a wall-clock budget, and so
does :meth:`WinRMTransport.connect` around the connector factory. Such a timeout
cannot cancel remote work: the executor thread keeps running and the server-side
runspace may stay allocated until the HTTP response finishes or the session is
closed, so repeated timeouts can exhaust WinRM ``MaxShellsPerUser``. On
oneshot/exec hard timeout and on identity-probe hard-fail the transport calls
:meth:`WinRMTransport.mark_dead` **and** immediately best-effort disposes the
session, so the pressure drops without waiting for the next connect. A timeout
never means the remote pipeline was Stopped - only the client wait ended; prefer
``endpoint close`` plus reopen when timeouts recur.

A ``link_retryable`` rejection (stale message-encryption framing) re-handshakes
the link and is replayed **once**, and only when it can be the operation's first
payload-carrying request (:meth:`WinRMTransport._link_replay_allowed`); every
other link-implicated failure marks the link dead instead.
:meth:`WinRMTransport.close_runspace` returns a landed / timeout / unconfirmed
verdict, so a caller reporting a teardown cannot claim a delete that never
happened. Open probe mode resolves from ``open(probe=False)``, env
``MRC_WINRM_PROBE``, then profile / global ``winrm_probe``; ``light`` and ``skip``
are seed-only, so connect success alone proves no identity. Connect forwards the
resolved ``operation_timeout_s`` / ``read_timeout_s`` pair, each per-call path
derives and restores its own, and the HTTP read timeout always outlasts the
WSMan operation timeout (:func:`resolve_pypsrp_op_read_timeouts`).
"""

from __future__ import annotations

import logging
import shlex
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError
from mcp_remote_control.transport.shell_wrap import coerce_cwd_path, wrap_with_cwd
from mcp_remote_control.transport.winrm_auth import (
    WINRM_AUTH_PROTOCOLS as WINRM_AUTH_PROTOCOLS,
    assemble_pypsrp_kwargs as assemble_pypsrp_kwargs,
    parse_spn as parse_spn,
)
from mcp_remote_control.transport.winrm_exec import (
    _EXIT_MARKER as _EXIT_MARKER,
    _append_ps_exit_probe as _append_ps_exit_probe,
    _coerce_exec_result as _coerce_exec_result,
    _decode_stream as _decode_stream,
    _exit_code_from_ps as _exit_code_from_ps,
    _format_ps_errors as _format_ps_errors,
    _format_ps_output as _format_ps_output,
    _is_execute_ps_result_tuple as _is_execute_ps_result_tuple,
    _is_timeout_exc as _is_timeout_exc,
    _isolate_user_script as _isolate_user_script,
    _parse_exit_marker_value as _parse_exit_marker_value,
    _ps_result_to_exec as _ps_result_to_exec,
    _ps_single_quote as _ps_single_quote,
    _split_exit_marker as _split_exit_marker,
    _win_quote as _win_quote,
    classify_winrm_failure as classify_winrm_failure,
    is_winrm_refusal as is_winrm_refusal,
)
from mcp_remote_control.transport.winrm_probe import (
    MRC_WINRM_PROBE_ENV as MRC_WINRM_PROBE_ENV,
    MRC_WINRM_PROBE_TIMEOUT_S as MRC_WINRM_PROBE_TIMEOUT_S,
    resolve_winrm_probe_timeout_s as resolve_winrm_probe_timeout_s,
    MRC_WINRM_PS_FS_MIN as MRC_WINRM_PS_FS_MIN,
    WINRM_PS_CAPABILITY_PROBE as WINRM_PS_CAPABILITY_PROBE,
    WinrmOpenProbeMode as WinrmOpenProbeMode,
    _WINRM_OPEN_PROBE_MODES as _WINRM_OPEN_PROBE_MODES,
    _WINRM_PS_RAW_KEYS as _WINRM_PS_RAW_KEYS,
    _as_bool as _as_bool,
    _incomplete_winrm_ps as _incomplete_winrm_ps,
    _is_probe_ps_version_line as _is_probe_ps_version_line,
    _normalize_winrm_ps_raw as _normalize_winrm_ps_raw,
    _parse_ps_version_tuple as _parse_ps_version_tuple,
    derive_winrm_ps_caps as derive_winrm_ps_caps,
    normalize_winrm_probe_mode as normalize_winrm_probe_mode,
    parse_winrm_ps_probe_output as parse_winrm_ps_probe_output,
    resolve_winrm_open_probe_mode as resolve_winrm_open_probe_mode,
)
from mcp_remote_control.transport.winrm_runspace import (
    InvokeRunspaceAdapter as InvokeRunspaceAdapter,
    PypsrpPoolRunspaceAdapter as PypsrpPoolRunspaceAdapter,
    RunspaceResult as RunspaceResult,
    _LOCATION_MARKER as _LOCATION_MARKER,
    _STOP_DEADLINE_S as _STOP_DEADLINE_S,
    _adapt_runspace_handle as _adapt_runspace_handle,
    _call_with_deadline as _call_with_deadline,
    _coerce_runspace_result as _coerce_runspace_result,
    _safe_stop_pipeline as _safe_stop_pipeline,
    _split_location_output as _split_location_output,
)
from mcp_remote_control.transport.winrm_session import (
    AdaptedWinRMSession as AdaptedWinRMSession,
    PypsrpClientAdapter as PypsrpClientAdapter,
    _WinRMConnectWatch as _WinRMConnectWatch,
    _bound as _bound,
    _use_winrm_connect_watch as _use_winrm_connect_watch,
    abandon_winrm_connect_watch as abandon_winrm_connect_watch,
    adapt_winrm_session as adapt_winrm_session,
    default_winrm_connector as default_winrm_connector,
    install_winrm_round_trip_counter as install_winrm_round_trip_counter,
    note_winrm_connect_handle as note_winrm_connect_handle,
    resync_winrm_session as resync_winrm_session,
)
from mcp_remote_control.transport.winrm_timeouts import (
    _BRIDGE_TIMEOUT_GRACE_S as _BRIDGE_TIMEOUT_GRACE_S,
    _RUNSPACE_OPEN_CLOSE_TIMEOUT_S as _RUNSPACE_OPEN_CLOSE_TIMEOUT_S,
    _SESSION_CLOSE_TIMEOUT_S as _SESSION_CLOSE_TIMEOUT_S,
    _push_wsman_timeouts as _push_wsman_timeouts,
    _session_wsman as _session_wsman,
    PYPSRP_HTTP_TIMEOUT_SLACK_S as PYPSRP_HTTP_TIMEOUT_SLACK_S,
    resolve_pypsrp_op_read_timeouts as resolve_pypsrp_op_read_timeouts,
    resolve_winrm_reconnect as resolve_winrm_reconnect,
)

_log = logging.getLogger(__name__)

# Connector: kwargs -> session/client handle (sync object).
WinRMConnector = Callable[..., Any]

# Verdicts returned by :meth:`WinRMTransport.close_runspace`. A caller that
# reports a teardown outcome reads one of these instead of assuming success:
# the WSMan ``Delete`` can be rejected by a stale framing context, and "the
# close helper is best-effort" must not read as "the remote runspace is gone".
_CLOSE_LANDED = "closed"           # closer returned: the Delete (if any) landed
_CLOSE_TIMEOUT = "timeout"         # wall-clock miss; the wait was abandoned
_CLOSE_UNCONFIRMED = "unconfirmed"  # closer failed; the Delete is not proven to have landed

# WSMan fault codes that prove the addressed shell no longer exists, so a
# rejected ``Delete`` has nothing left to delete. pypsrp reads the same code as
# "this runspace pool is gone" in ``RunspacePool.is_alive`` (it sets the pool
# state to CLOSED instead of re-raising), which is the authority here. Any
# other fault leaves the runspace's fate unknown and must stay unconfirmed -
# claiming a landed delete would report a released runspace that still lives.
# ``0x80338029`` (ERROR_WSMAN_OPERATION_TIMEDOUT) is deliberately absent, since
# for a Delete it means the server's own operation timed out, not a gone shell.
_RUNSPACE_GONE_FAULT_CODES = frozenset({
    0x8033805B,  # ERROR_WSMAN_UNEXPECTED_SELECTORS: no object matches the selectors
})


def _fault_shows_runspace_gone(exc: BaseException) -> bool:
    """Whether *exc* is a WSMan fault proving the addressed shell is gone.

    pypsrp raises ``WSManFaultError(code, machine, reason, ...)``: ``code`` is the
    WSMan fault's ``Code`` attribute when the server sent one and the SOAP
    subcode text otherwise. Only a code listed in
    :data:`_RUNSPACE_GONE_FAULT_CODES` counts, and an unparsed (non-numeric)
    code never does - an unrecognized fault is not evidence that the shell
    disappeared.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, bool):
        return False
    if isinstance(code, int):
        return code in _RUNSPACE_GONE_FAULT_CODES
    text = str(code or "").strip()
    if not text:
        return False
    try:
        return int(text, 0) in _RUNSPACE_GONE_FAULT_CODES
    except ValueError:
        return False


def _safe_msg(exc: BaseException) -> str:
    """Short exception text for transport errors (truncated; no secret material)."""
    text = str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:197] + "..."
    return text


def _ps_exit_probe_ran(raw: Any) -> bool:
    """True when *raw* is a result shape whose exit code comes from the probe.

    Only pypsrp's pipeline payloads derive their exit code from the appended
    ``__MRC_PS_EXIT_MARKER__`` (see :func:`_ps_result_to_exec`); an
    ``ExecResult`` or a duck-typed run result already carries an explicit code
    and is left alone.
    """
    if isinstance(raw, ExecResult):
        return True
    if not isinstance(raw, (list, tuple)):
        return True
    if isinstance(raw, tuple) and _is_execute_ps_result_tuple(raw):
        payload: Any = raw[0] if raw else None
    else:
        payload = raw
    return _EXIT_MARKER in str(payload)


def _missing_exit_probe_stderr(stderr: str) -> str:
    """Honest stderr for a run whose exit probe never emitted its marker.

    Bounded like :meth:`WinRMTransport._link_lost_detail` so an Agent/CLI line
    is not blown up by a noisy remote stderr.
    """
    base = (stderr or "").strip()
    note = (
        "exit probe did not run (the script ended its own pipeline, e.g. a "
        "top-level exit); exit code unknown"
    )
    detail = f"{base}; {note}" if base else note
    if len(detail) > 240:
        detail = detail[:237] + "..."
    return detail


def _coerce_ps_oneshot_exec(raw: Any, *, default_cwd: str | None) -> ExecResult:
    """``_ps_result_to_exec`` for the oneshot splice, with the probe contract.

    :meth:`WinRMTransport.run_command` / :meth:`WinRMTransport.run_argv` append
    an exit probe to the caller's script (:func:`_append_ps_exit_probe`), so a
    pypsrp payload with no marker means the probe never ran: the caller's own
    statement ended the pipeline (a top-level ``exit``), or the script aborted
    before its last statement. pypsrp reports such a pipeline as completed with
    no error records (``had_errors`` is set only on a FAILED state), so the
    bare ``_exit_code_from_ps(captured=None, had_errors=False)`` mapping turns a
    run that produced no evidence of success into ``exit_code=0`` /
    ``status=ok`` - the same "missing marker means unknown, not 0" rule the
    pooled runspace path states in ``RunspaceResult.exit_probe_ran``.

    A missing marker therefore never reads as success: the code falls back to
    ``had_errors`` when PowerShell recorded an error, and to -1 ("unknown",
    matching :func:`_coerce_exec_result` / SSH) otherwise, with stderr naming
    the reason so -1 is not read as the remote program's own exit code.
    """
    result = _ps_result_to_exec(raw, default_cwd=default_cwd)
    if _ps_exit_probe_ran(raw) or result.exit_code != 0:
        return result
    return ExecResult(
        exit_code=-1,
        stdout=result.stdout,
        stderr=_missing_exit_probe_stderr(result.stderr),
        cwd=result.cwd,
        timed_out=result.timed_out,
    )


def _looks_like_auth_failure(exc: BaseException) -> bool:
    """Heuristic: map connect exceptions to AUTH_FAILED vs CONNECT_FAILED.

    Network markers (timeout, refused, DNS) win over auth-looking tokens so a
    firewall drop is not reported as bad credentials.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    net_markers = (
        "refused",
        "timeout",
        "timed out",
        "unreachable",
        "name or service not known",
        "nodename nor servname",
        "getaddrinfo",
        "connection reset",
        "network is unreachable",
    )
    if any(m in text for m in net_markers):
        return False
    auth_markers = (
        "auth",
        "unauthor",
        "401",
        "403",
        "credential",
        "logon failure",
        "access is denied",
        "login failed",
        "password",
        "ntlm",
        "negotiate",
        "denied",
    )
    return any(m in text for m in auth_markers)


class WinRMTransport(BaseTransport):
    """WinRM session handle over a Protocol-compatible session object.

    Parameters
    ----------
    connector:
        Factory that receives :meth:`connect_kwargs` and returns a session.
        Defaults to :func:`default_winrm_connector` (``PypsrpClientAdapter``
        around a real pypsrp ``Client``).
    reconnection_retries / reconnection_backoff:
        pypsrp ``WSMan`` urllib3 retry knobs; ``None`` resolves through
        :func:`resolve_winrm_reconnect`. They retry connection-level failures
        on the WinRM POST, never HTTP status codes.
    probe_timeout_s:
        Open-time identity / capability probe budget; ``None`` resolves through
        :func:`resolve_winrm_probe_timeout_s` so a high-latency link can raise
        it per profile.

    On connect the raw session is wrapped in :class:`AdaptedWinRMSession`, which
    freezes capability discovery; production paths call that stable surface only
    - high-level ``run_command`` / ``run_argv``, oneshot ``execute_ps`` /
    ``execute_cmd`` with a fixed ``environment=``, ``open_runspace()`` (always a
    RunspaceHandle adapter), and ``open_fs()`` / the pypsrp copy+fetch+ps file
    client.
    """

    name = "winrm"

    def __init__(
        self,
        *,
        host: str,
        port: int = 5985,
        username: str,
        password: str | None = None,
        auth: str = "ntlm",
        ssl: bool = False,
        # Secure default: validate TLS peer certificates unless the caller
        # explicitly passes cert_validation=False (e.g. lab hosts with a
        # private CA not installed on the client).
        cert_validation: bool = True,
        connect_timeout_ms: int = 15000,
        operation_timeout_s: int | None = None,
        read_timeout_s: int | None = None,
        encryption: str = "auto",
        connector: WinRMConnector | None = None,
        # Link tuning. ``None`` on any of these means "resolve from the
        # environment / defaults" - see ``connect_kwargs`` / probe budget.
        reconnection_retries: int | None = None,
        reconnection_backoff: float | None = None,
        probe_timeout_s: float | None = None,
        # Enterprise auth: PEM paths as strings; secret values loaded by caller.
        certificate_pem: str | None = None,
        certificate_key_pem: str | None = None,
        certificate_key_password: str | None = None,
        spn: str | None = None,
        negotiate_hostname_override: str | None = None,
        negotiate_service: str | None = None,
        negotiate_delegate: bool | None = None,
        credssp_auth_mechanism: str | None = None,
        credssp_disable_tlsv1_2: bool | None = None,
        credssp_minimum_version: int | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = int(port)
        self.username = username
        # Password held only for connect; never logged or put in meta/repr.
        self._password = password
        self.auth = auth or "ntlm"
        self.ssl = bool(ssl)
        self.cert_validation = bool(cert_validation)
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.operation_timeout_s = operation_timeout_s
        self.read_timeout_s = read_timeout_s
        self.encryption = encryption or "auto"
        self._connector: WinRMConnector = connector or default_winrm_connector
        # Profile-level link knobs; None lets the resolvers apply env/defaults.
        self.reconnection_retries = reconnection_retries
        self.reconnection_backoff = reconnection_backoff
        self.probe_timeout_s = probe_timeout_s
        self._session: AdaptedWinRMSession | None = None
        # Reader for successfully completed HTTP exchanges on the live pypsrp
        # transport; None when the session has no countable transport (test
        # doubles, non-pypsrp connectors). See _link_replay_allowed.
        self._round_trips: Callable[[], int] | None = None
        # Once-per-session flag for the degraded replay-guard warning, so a
        # transport that cannot observe the link says so exactly once instead
        # of on every call.
        self._replay_guard_warned = False
        # Enterprise auth options (paths and non-secret flags).
        self.certificate_pem = certificate_pem
        self.certificate_key_pem = certificate_key_pem
        self._certificate_key_password = certificate_key_password
        self.spn = spn
        self.negotiate_hostname_override = negotiate_hostname_override
        self.negotiate_service = negotiate_service
        self.negotiate_delegate = negotiate_delegate
        self.credssp_auth_mechanism = credssp_auth_mechanism
        self.credssp_disable_tlsv1_2 = credssp_disable_tlsv1_2
        self.credssp_minimum_version = credssp_minimum_version
        # Last resolved op/read applied for a call (tests / diagnostics).
        # Not Agent-track fields; cleared only by subsequent resolves.
        self._last_applied_operation_timeout: int | None = None
        self._last_applied_read_timeout: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (
            f"WinRMTransport(host={self.host!r}, port={self.port!r}, "
            f"username={self.username!r}, auth={self.auth!r}, "
            f"ssl={self.ssl!r})"
        )

    def connect_kwargs(self) -> dict[str, Any]:
        """Assemble pypsrp/connector kwargs for this transport (no network).

        Includes Kerberos / CredSSP / certificate wiring so callers can inspect
        the effective Client parameters without connecting; ``connect_timeout``
        comes only from ``connect_timeout_ms``. The profile
        ``operation_timeout_s`` / ``read_timeout_s`` pair is resolved through
        :func:`resolve_pypsrp_op_read_timeouts`, the same resolver the per-call
        path uses, so the values pypsrp is constructed with already satisfy its
        ordering invariant and a non-positive profile value means "unset" here
        exactly as it does there.

        That pair is what every path that never re-resolves per call inherits:
        ``open_runspace`` / ``close_runspace``, ``open_fs`` and every fs call.
        Forwarding the raw pair would leave pypsrp's default operation timeout
        running against an explicit read the operator set lower, and the client
        read timeout that fires first is classified ``link_fatal``: the endpoint
        is torn down for a self-inflicted ordering bug instead of reporting a
        clean WSMan fault. Call-time derivation from exec ``timeout_s`` happens
        later on the live session (:meth:`_pypsrp_timeouts_for_call`), not here.

        ``reconnection_retries`` / ``reconnection_backoff`` resolve via
        :func:`resolve_winrm_reconnect`; an explicit ``0`` or negative count
        drops the pair so pypsrp's default of no retries stays in force.
        """
        timeout_s = max(self.connect_timeout_ms, 1) / 1000.0
        reconnect = resolve_winrm_reconnect(
            self.reconnection_retries, self.reconnection_backoff
        )
        retries, backoff = reconnect if reconnect is not None else (None, None)
        op_timeout, read_timeout = self.resolve_call_op_read_timeouts(None)
        self._note_timeout_adjustments(op_timeout, read_timeout)
        return assemble_pypsrp_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self._password,
            auth=self.auth,
            ssl=self.ssl,
            cert_validation=self.cert_validation,
            encryption=self.encryption,
            connect_timeout=timeout_s,
            operation_timeout=op_timeout,
            read_timeout=read_timeout,
            reconnection_retries=retries,
            reconnection_backoff=backoff,
            certificate_pem=self.certificate_pem,
            certificate_key_pem=self.certificate_key_pem,
            certificate_key_password=self._certificate_key_password,
            spn=self.spn,
            negotiate_hostname_override=self.negotiate_hostname_override,
            negotiate_service=self.negotiate_service,
            negotiate_delegate=self.negotiate_delegate,
            credssp_auth_mechanism=self.credssp_auth_mechanism,
            credssp_disable_tlsv1_2=self.credssp_disable_tlsv1_2,
            credssp_minimum_version=self.credssp_minimum_version,
        )

    def resolve_call_op_read_timeouts(
        self, timeout_s: float | None
    ) -> tuple[int | None, int | None]:
        """Resolve effective op/read for *timeout_s* with this profile.

        Profile ``operation_timeout_s`` / ``read_timeout_s`` take priority over
        derivation from *timeout_s*. See :func:`resolve_pypsrp_op_read_timeouts`.
        """
        return resolve_pypsrp_op_read_timeouts(
            timeout_s=timeout_s,
            operation_timeout_s=self.operation_timeout_s,
            read_timeout_s=self.read_timeout_s,
        )

    def _note_timeout_adjustments(self, op: int | None, rd: int | None) -> None:
        """Warn when the profile's op/read pair was not used verbatim.

        Two adjustments happen silently otherwise, and both change what the
        operator asked for: a non-positive value is read as "unset" (so the
        field falls back to the derived/library default instead of reaching
        pypsrp as a floor of 1 second), and an explicit pair whose operation
        timeout outlasts the read timeout is clamped by
        :func:`resolve_pypsrp_op_read_timeouts` so the client read timeout
        cannot fire first. Called from :meth:`connect_kwargs`, i.e. once per
        connect rather than once per call.
        """
        raw_op, raw_rd = self.operation_timeout_s, self.read_timeout_s

        def _configured(value: int | None) -> bool:
            try:
                return value is not None and int(value) > 0
            except (TypeError, ValueError, OverflowError):
                return False

        dropped = [
            name
            for name, value in (
                ("operation_timeout_s", raw_op),
                ("read_timeout_s", raw_rd),
            )
            if value is not None and not _configured(value)
        ]
        inverted = (
            _configured(raw_op)
            and _configured(raw_rd)
            and int(raw_rd) < int(raw_op) + PYPSRP_HTTP_TIMEOUT_SLACK_S
        )
        if not dropped and not inverted:
            return
        _log.warning(
            "winrm %s: profile operation_timeout_s=%r read_timeout_s=%r resolved "
            "to operation_timeout=%r read_timeout=%r (a non-positive value means "
            "unset; the HTTP read timeout must outlast the WSMan operation "
            "timeout by %ss or the client read timeout fires first and is "
            "reported as a lost link)",
            self.host,
            raw_op,
            raw_rd,
            op,
            rd,
            PYPSRP_HTTP_TIMEOUT_SLACK_S,
        )

    @contextmanager
    def _pypsrp_timeouts_for_call(
        self, timeout_s: float | None
    ) -> Iterator[tuple[int | None, int | None]]:
        """Apply resolved op/read onto the live session for one call; restore after.

        Records :attr:`_last_applied_operation_timeout` /
        :attr:`_last_applied_read_timeout` for tests. Mutates
        ``wsman.operation_timeout`` and ``wsman.transport.read_timeout`` when
        present (real pypsrp Client or a duck-typed double). Does not claim
        remote cancel; only aligns library HTTP/WSMan budgets with the call.
        """
        op, rd = self.resolve_call_op_read_timeouts(timeout_s)
        self._last_applied_operation_timeout = op
        self._last_applied_read_timeout = rd
        restores: list[tuple[Any, str, Any]] = []
        if op is not None or rd is not None:
            wsman = _session_wsman(self._session)
            if wsman is not None:
                _push_wsman_timeouts(
                    wsman,
                    operation_timeout=op,
                    read_timeout=rd,
                    restores=restores,
                )
        try:
            yield op, rd
        finally:
            for obj, attr, old in reversed(restores):
                try:
                    setattr(obj, attr, old)
                except Exception:  # noqa: BLE001 - best-effort restore
                    pass

    def connect(self) -> None:
        if self._connected and self._session is not None:
            return
        # After mark_dead (or a failed prior session) _session may still hold a
        # dead handle. Close it best-effort before opening a replacement so
        # ensure_connected / probe-fail reconnect cannot stack zombie WinRM
        # shells (MaxShellsPerUser). No-op when _session is already None.
        self._dispose_prior_session()
        try:
            connect_args = self.connect_kwargs()
        except TransportError:
            raise
        # Wall-clock around the connector: pypsrp connection_timeout alone does
        # not bound Client construction / DNS / SSL or a hung injectable
        # factory. Budget = connect_timeout + grace (same shape as SSH).
        timeout_s = max(self.connect_timeout_ms, 1) / 1000.0
        bridge_timeout = timeout_s + _BRIDGE_TIMEOUT_GRACE_S
        # Shared with the worker thread: the connector may construct a
        # Client (or adapter) and then hang. On wall-clock timeout we
        # abandon the watch so that already-built handle is closed and a
        # late factory return cannot leak a remote shell.
        watch = _WinRMConnectWatch()

        def _build_session() -> Any:
            with _use_winrm_connect_watch(watch):
                built = self._connector(**connect_args)
                note_winrm_connect_handle(built)
                return built

        try:
            session = self._run_blocking_with_timeout(
                _build_session,
                timeout_s=bridge_timeout,
            )
        except TimeoutError as exc:
            # Never set _connected / _session on a hung connector. Close
            # any Client the worker already constructed (or will return).
            abandon_winrm_connect_watch(watch)
            raise TransportError(
                "CONNECT_FAILED",
                _safe_msg(exc) or f"winrm connect timed out after {bridge_timeout}s",
                details={
                    "host": self.host,
                    "port": self.port,
                    "auth": self.auth,
                    "connect_timeout_s": timeout_s,
                    "bridge_timeout_s": bridge_timeout,
                },
            ) from exc
        except TransportError:
            raise
        except Exception as exc:
            code = "AUTH_FAILED" if _looks_like_auth_failure(exc) else "CONNECT_FAILED"
            raise TransportError(
                code,
                _safe_msg(exc),
                details={"host": self.host, "port": self.port, "auth": self.auth},
            ) from exc

        if session is None:
            raise TransportError(
                "CONNECT_FAILED",
                "winrm connector returned no session",
                details={"host": self.host, "port": self.port},
            )

        adapted = adapt_winrm_session(session)
        self._session = adapted
        self._connected = True
        # Count HTTP exchanges on the live pypsrp transport so a replayed
        # link rejection can be proven to be the operation's first request.
        # A replacement session may install a counter the previous one lacked,
        # so the once-per-session warning is re-armed here.
        self._replay_guard_warned = False
        self._round_trips = install_winrm_round_trip_counter(adapted)
        # Optional cwd/home seeds when the session object already exposes them.
        self.cwd = adapted.cwd or self.cwd
        self.home = adapted.home or self.home
        self.meta = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth": self.auth,
            "ssl": self.ssl,
        }
        if self.certificate_pem:
            # Path only - never PEM body.
            self.meta["cert_path"] = self.certificate_pem
        if self.spn:
            self.meta["spn"] = self.spn
        # Best-effort probe seeds captured at adapt time (no network).
        for key, val in adapted._meta_seeds.items():
            self.meta[key] = val
        if self._round_trips is None:
            # No countable transport: the replay guard cannot observe the link
            # (see _link_replay_allowed). Never silent.
            self._note_replay_guard_degraded("no_counter")
        # Secrets stay on private transport fields only (never meta/repr/Agent).

    def _dispose_prior_session(self) -> None:
        """Best-effort drop of the prior WinRM session (never raises).

        Used by :meth:`close`, by :meth:`connect` when replacing a dead handle
        after :meth:`mark_dead`, immediately on oneshot/exec **hard timeout**
        (after :meth:`mark_dead`), and immediately on **identity-probe
        hard-fail** (after :meth:`mark_dead`) so MaxShells pressure drops
        without waiting for the next reconnect. Clears ``_session`` first so
        concurrent readers see a clean slate even if teardown stalls. Closing
        the client session is the primary lever against remote shell leaks
        (``MaxShellsPerUser``); it does **not** guarantee the remote oneshot
        pipeline has stopped - only that the local pypsrp Client/session is
        torn down.

        ``session.close`` runs under :data:`_SESSION_CLOSE_TIMEOUT_S` via
        :meth:`_run_blocking_with_timeout`. On timeout the wait is abandoned
        (the executor thread may still finish later); ``_session`` stays
        cleared so a blackholed WSMan teardown cannot pin ``_op_lock``.
        """
        session = self._session
        self._session = None
        if session is None:
            return
        closer = getattr(session, "close", None)
        if not callable(closer):
            return
        budget = float(_SESSION_CLOSE_TIMEOUT_S)
        try:
            self._run_blocking_with_timeout(closer, timeout_s=budget)
        except TimeoutError:
            _log.warning(
                "winrm session.close timed out after %ss "
                "(best-effort abandon, host=%s)",
                budget,
                self.host,
            )
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    def close(self) -> None:
        self._dispose_prior_session()
        self._connected = False

    def is_connected(self) -> bool:
        """True only when flagged connected *and* the session is still alive."""
        if not self._connected or self._session is None:
            return False
        if not self.is_alive():
            # Explicit mark_dead or closed-flag probe left a zombie flag.
            self.mark_dead("winrm session closed")
            return False
        return True

    def is_alive(self) -> bool:
        """Best-effort liveness of the underlying WinRM session.

        WinRM is HTTP-based (no always-on socket flags like SSH). Returns False
        when the transport was :meth:`mark_dead`'d, ``_session`` is missing, or
        the adapted/raw session exposes a closed flag. Hard timeouts call
        :meth:`mark_dead` so this becomes False without a network probe.
        """
        if not self._connected or self._session is None:
            return False
        session = self._session
        # Duck-type closed signals on the adapter and the raw session object.
        for obj in (session, getattr(session, "raw", None)):
            if obj is None:
                continue
            for attr in ("closed", "is_closed", "_closed"):
                flag = getattr(obj, attr, None)
                if callable(flag):
                    try:
                        if flag():
                            return False
                    except Exception:  # noqa: BLE001
                        return False
                elif flag is True:
                    return False
        return True

    def mark_dead(self, reason: str | None = None) -> None:
        """Mark the WinRM session dead without raising.

        Used after wall-clock hard timeouts, identity-probe hard-fail, and
        other clear session-death signals: callers (registry
        ``ensure_connected`` / ``open``) reconnect rather than reuse a
        ``_connected=True`` shell.

        On **hard timeout** and **identity-probe hard-fail**, callers also
        invoke :meth:`_dispose_prior_session` immediately (session ``close``)
        so MaxShells pressure drops without waiting for the next connect.
        Other death paths may retain ``_session`` until the next
        :meth:`connect` or :meth:`close`, which best-effort closes it before
        opening a replacement.
        """
        self._connected = False
        if reason:
            self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}

    @property
    def session(self) -> Any:
        """Adapted session (:class:`AdaptedWinRMSession`); None if closed."""
        return self._session

    def collect_probe(
        self, *, mode: WinrmOpenProbeMode | str = "full"
    ) -> dict[str, Any]:
        """Lightweight post-connect probe for ``endpoint.probe``.

        Prefer already-seeded meta / session attributes. When *mode* is ``full``
        and the session exposes ``execute_ps`` without identity seeds
        (os/shell/ps_version), run the one-RTT PowerShell capability probe
        (language mode, cmdlet presence, file IO) and store the result in
        ``meta["winrm_ps"]``; a session that seeds identity without a capability
        surface keeps the skip (no remote probe, ``winrm_ps`` absent).

        *mode*: ``full`` (default) is a hard identity RTT plus the capability
        oneshot when unseeded; ``light`` is seeds only (partial without identity
        seeds, never ``mark_dead`` for a missing RTT); ``skip`` is marker-only.

        Serialized via transport ``_op_lock``, so the remote RTT cannot
        interleave with ``mark_dead`` / ``connect`` / ``close`` / exec on the
        same transport.

        Identity vs capability (mode=full): an exception, timeout or empty/junk
        output with no trusted identity seeds hard-fails open
        (:meth:`_identity_probe_hard_fail`: ``mark_dead`` + immediate dispose,
        ``status=fail``) so no fake connected endpoint is registered; an
        unparseable capability payload on a successful RTT stays soft
        ``status=partial``. Identity seeds alone never set ``ps_script_fs=true``,
        and the unseeded oneshot is one temporary shell (``MaxShellsPerUser``).
        """
        resolved = normalize_winrm_probe_mode(mode) or "full"
        data: dict[str, Any] = {}
        session: AdaptedWinRMSession | None = self._session
        for key in ("os", "shell", "ps_version", "home", "cwd"):
            if key == "home" and self.home:
                data["home"] = self.home
                continue
            if key == "cwd" and self.cwd:
                data["pwd"] = self.cwd
                continue
            val = self.meta.get(key)
            if val is not None:
                data[key] = val
            elif session is not None:
                seed = session.seed_attr(key)
                if seed is not None:
                    data[key] = seed

        if resolved == "skip":
            # Defensive: registry normally skips collect_probe entirely.
            marker = {"ps_probe": "skipped"}
            self.meta = {**(self.meta or {}), "winrm_ps": marker}
            data["ps_probe"] = "skipped"
            data["probe_mode"] = "skip"
            data.setdefault("status", "ok")
            return data

        # Explicit seed hard-fail (adapters that already know identity RTT failed).
        seed_status = str(self.meta.get("probe_status") or "").strip().lower()
        if seed_status in ("fail", "error"):
            err = self.meta.get("probe_error") or "identity probe failed"
            return self._identity_probe_hard_fail(str(err), data)

        if resolved == "light":
            return self._collect_probe_light(data, session)

        # Capability seeds may live on meta (from adapt) or session seeds.
        # Remote capability RTT may hard-fail identity when no seeds exist.
        self._merge_winrm_ps_into_probe(data, session)
        # Merge may hard-fail (empty/exception RTT) or soft-partial (non-empty
        # unparseable capability). Do not fall through to a second oneshot or
        # invent identity from os/shell defaults after either outcome.
        if data.get("status") in ("fail", "partial"):
            data.setdefault("probe_mode", "full")
            return data

        if self.meta.get("probe_status") == "partial":
            # Soft capability / adapter partial - identity already seeded or
            # not required for this surface; open may still succeed.
            data["status"] = "partial"
            err = self.meta.get("probe_error")
            if err:
                data["error"] = err
            data.setdefault("probe_mode", "full")
            return data

        # Session already supplied os/shell/ps_version - identity proven by
        # adapter seeds (no extra MaxShells oneshot). Capability handled above.
        # Only real seeds / probe fields count - invented defaults alone must
        # not satisfy this branch (see _apply_winrm_ps / empty-stdout hard-fail).
        if data.get("os") or data.get("shell") or data.get("ps_version"):
            data.setdefault("status", "ok")
            data.setdefault("os", data.get("os") or "windows")
            data.setdefault("probe_mode", "full")
            return data

        if session is None:
            return self._identity_probe_hard_fail("not connected", data)

        # Prefer high-level run_command when present; else pypsrp execute_ps.
        # Both are identity RTT only (capability was already attempted above
        # when execute_ps existed without seeds) and both run through the exec
        # wall-clock wrapper, so a hung session mock / pypsrp call is bounded.
        # The budget is the resolved probe budget on both branches
        # (env -> profile -> ``MRC_WINRM_PROBE_TIMEOUT_S``): a profile that raises
        # ``[winrm].probe_timeout_s`` for a high-latency link must not still be
        # capped by the default, which is read at call time and can be rebound.
        if session.has_run_command:
            probe_budget = resolve_winrm_probe_timeout_s(
                profile_value=self.probe_timeout_s or MRC_WINRM_PROBE_TIMEOUT_S
            )
            try:
                result = self._map_call_to_exec(
                    lambda: session.run_command(
                        "[Environment]::OSVersion.VersionString; "
                        "$PSVersionTable.PSVersion.ToString(); "
                        "$env:USERPROFILE; $PWD.Path",
                        cwd=None,
                        timeout_s=probe_budget,
                        env=None,
                    ),
                    timeout_s=probe_budget,
                    cwd=self.cwd,
                    coerce=_coerce_exec_result,
                )
                if getattr(result, "timed_out", False):
                    return self._identity_probe_hard_fail(
                        "identity probe timed out", data
                    )
                parsed = self._parse_probe_stdout(result.stdout, base=data)
                if not self._identity_proven(parsed):
                    return self._identity_probe_hard_fail(
                        parsed.get("error") or "identity probe empty or unusable",
                        parsed,
                    )
                parsed.setdefault("probe_mode", "full")
                return parsed
            except Exception as exc:
                return self._identity_probe_hard_fail(_safe_msg(exc), data)

        if session.has_execute_ps:
            try:
                script = (
                    "$PSVersionTable.PSVersion.ToString(); "
                    "$env:USERPROFILE; "
                    "(Get-Location).Path; "
                    "[Environment]::OSVersion.VersionString"
                )
                stdout = self._execute_ps_stdout(session, script)
                parsed = self._parse_probe_stdout(stdout, base=data)
                if not self._identity_proven(parsed):
                    return self._identity_probe_hard_fail(
                        parsed.get("error") or "identity probe empty or unusable",
                        parsed,
                    )
                parsed.setdefault("probe_mode", "full")
                return parsed
            except Exception as exc:
                return self._identity_probe_hard_fail(_safe_msg(exc), data)

        # No exec surface and no identity seeds - cannot prove reachability.
        return self._identity_probe_hard_fail(
            "no identity seeds and no exec surface for probe", data
        )

    def _collect_probe_light(
        self,
        data: dict[str, Any],
        session: AdaptedWinRMSession | None,
    ) -> dict[str, Any]:
        """Seed-only open probe (mode=light): no remote MaxShells oneshot.

        Uses identity / capability seeds already on meta or the session.
        Does **not** call ``execute_ps`` / ``run_command``. Without seeds,
        returns ``status=partial`` and ``ps_probe=light`` while leaving the
        transport live - lab acceleration; connect success alone is not an
        identity proof (documented risk).
        """
        data["probe_mode"] = "light"
        # Capability seeds only (no network).
        seeded_raw = self._capability_seeds_raw(session)
        if seeded_raw.get("language_mode") is not None or any(
            k in seeded_raw
            for k in ("can_get_item", "can_file_io", "has_convertto_json")
        ):
            derived = derive_winrm_ps_caps(seeded_raw)
            self.meta["winrm_ps"] = derived
            self._apply_winrm_ps(data, derived)
            data.setdefault("status", "ok")
            return data

        existing = self.meta.get("winrm_ps")
        if isinstance(existing, dict) and existing:
            self._apply_winrm_ps(data, existing)

        if data.get("os") or data.get("shell") or data.get("ps_version"):
            data.setdefault("status", "ok")
            data.setdefault("os", data.get("os") or "windows")
            return data

        # No identity seeds and no remote RTT - soft partial, stay connected.
        marker = {"ps_probe": "light"}
        self.meta = {**(self.meta or {}), "winrm_ps": marker}
        data["winrm_ps"] = marker
        data["ps_probe"] = "light"
        data["status"] = "partial"
        data.setdefault(
            "error",
            "open probe mode=light: no identity seeds and no remote RTT",
        )
        return data

    def _identity_proven(self, data: dict[str, Any]) -> bool:
        """True when probe *data* shows host identity (seed or successful RTT).

        Real os/shell/ps_version fields or capability language_mode/ps_version
        count. Incomplete winrm_ps leftovers and invent-only convenience
        defaults must not satisfy this check - ``_parse_probe_stdout`` only
        sets os/shell after a version-shaped token or a ``Microsoft Windows``
        banner hits (or seeds already present). A home/pwd ``C:\\`` path, a
        line that merely contains ``Windows``, or junk like ``Access Denied``
        / HTTP ``401`` / ``500`` leaves these fields absent.
        """
        if data.get("os") or data.get("shell") or data.get("ps_version"):
            return True
        # Successful capability merge carries real ps_version / language_mode.
        winrm_ps = data.get("winrm_ps")
        if isinstance(winrm_ps, dict) and (
            winrm_ps.get("ps_version") or winrm_ps.get("language_mode")
        ):
            return True
        return False

    def _identity_probe_hard_fail(
        self,
        reason: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark dead and dispose session after open-time identity/WSMan fail.

        Open must not report connected when a lightweight RTT cannot prove the
        session works. The open-time oneshot is one temporary shell against
        WinRM ``MaxShellsPerUser``; same lever as oneshot/exec hard-timeout:
        :meth:`mark_dead` **and** immediately best-effort
        :meth:`_dispose_prior_session` (``session.close``) so MaxShells
        pressure drops without waiting for the next connect/close. Does
        **not** claim the remote PowerShell pipeline was Stopped - only the
        local client/session is torn down.

        Does not raise - registry ``_light_probe`` must stay non-throwing;
        ``endpoint open`` fails via ``is_connected()`` False + registry pop.
        """
        msg = (reason or "identity probe failed").strip() or "identity probe failed"
        if len(msg) > 200:
            msg = msg[:197] + "..."
        self.mark_dead(f"identity probe failed: {msg}"[:200])
        # Dispose immediately on every identity hard-fail (timeout,
        # exception, empty/junk identity) so MaxShells pressure drops.
        self._dispose_prior_session()
        self.meta = {
            **(self.meta or {}),
            "probe_status": "fail",
            "probe_error": msg,
        }
        out = dict(data or {})
        out["status"] = "fail"
        out["error"] = msg
        # Capability surface unknown after identity failure - keep oneshot
        # allowed for later reconnect probes, but close script FS / runspace.
        incomplete = _incomplete_winrm_ps(error=msg)
        self.meta["winrm_ps"] = incomplete
        out["winrm_ps"] = incomplete
        for key in ("ps_script_fs", "ps_oneshot", "ps_runspace", "ps_probe"):
            if key in incomplete:
                out.setdefault(key, incomplete[key])
        return out

    def _merge_winrm_ps_into_probe(
        self,
        data: dict[str, Any],
        session: AdaptedWinRMSession | None,
    ) -> None:
        """Resolve ``winrm_ps`` (seeds and/or remote probe) into *data* and meta.

        Resolution order:
        1. Existing ``meta["winrm_ps"]`` (idempotent re-probe)
        2. Capability seeds on session/meta (language_mode / cmdlet flags)
        3. Remote oneshot via ``execute_ps`` when the session exposes it AND
           no identity seeds (os/shell/ps_version) are already present.
           Identity seeds alone mean the adapter supplied host identity
           without a capability surface; the historical skip leaves
           ``meta["winrm_ps"]`` absent (gates permissive). Production
           adapters that do not seed identity always probe.

        Remote oneshot cost: one temporary WinRM shell (``MaxShellsPerUser``).
        Identity RTT failure hard-fails (``status=fail`` + mark_dead + immediate
        session dispose), including empty/whitespace-only stdout with no real
        os/shell/ps_version. A successful RTT with non-empty but unparseable
        capability stays soft ``partial`` (does not invent identity solely via
        os/shell defaults).
        """
        existing = self.meta.get("winrm_ps")
        if isinstance(existing, dict) and existing:
            self._apply_winrm_ps(data, existing)
            # Incomplete snapshot without real identity fields: keep soft
            # partial so collect_probe does not issue a second MaxShells
            # oneshot. (Hard-fail already set status=fail + mark_dead.)
            if existing.get("ps_probe") == "failed" and not (
                data.get("os")
                or data.get("shell")
                or data.get("ps_version")
                or existing.get("language_mode")
                or existing.get("ps_version")
            ):
                data.setdefault("status", "partial")
                err = existing.get("error")
                if err:
                    data.setdefault("error", err)
            return

        seeded_raw = self._capability_seeds_raw(session)
        if seeded_raw.get("language_mode") is not None or any(
            k in seeded_raw for k in ("can_get_item", "can_file_io", "has_convertto_json")
        ):
            derived = derive_winrm_ps_caps(seeded_raw)
            self.meta["winrm_ps"] = derived
            self._apply_winrm_ps(data, derived)
            return

        if session is None or not session.has_execute_ps:
            # No live probe surface. Do not invent ps_script_fs=true.
            return
        # Identity seeds (os/shell/ps_version) already present mean the
        # adapter supplied host identity without a capability surface. Keep
        # the historical skip: do not run a remote capability probe, leave
        # winrm_ps absent so downstream gates stay permissive. Production
        # adapters that do not seed identity fall through to the probe below.
        # The explicit opt-out is probe=False on open, which skips
        # collect_probe entirely upstream.
        if data.get("os") or data.get("shell") or data.get("ps_version"):
            return

        try:
            stdout = self._execute_ps_stdout(session, WINRM_PS_CAPABILITY_PROBE)
            # Empty/whitespace-only stdout is not a successful identity RTT
            # (JEA/restricted endpoints often return only error stream). Do not
            # apply os/shell defaults that would fake a proven host.
            if not (stdout or "").strip():
                failed = self._identity_probe_hard_fail(
                    "identity probe empty or unusable", data
                )
                data.clear()
                data.update(failed)
                return
            raw = parse_winrm_ps_probe_output(stdout)
            if not raw or "language_mode" not in raw:
                # Non-empty RTT succeeded but capability incomplete - soft.
                # Caller must not treat invent-only os/shell as identity proof;
                # status=partial early-returns from collect_probe.
                incomplete = _incomplete_winrm_ps(
                    error="capability probe incomplete or unparseable",
                )
                self.meta["winrm_ps"] = incomplete
                self._apply_winrm_ps(data, incomplete)
                data["status"] = "partial"
                data.setdefault("error", incomplete.get("error"))
                return
            derived = derive_winrm_ps_caps(raw)
            self.meta["winrm_ps"] = derived
            self._apply_winrm_ps(data, derived)
        except Exception as exc:  # noqa: BLE001 - hard-fail identity, no raise
            # Unseeded path: this oneshot *is* the identity RTT. Failure means
            # open must not leave a fake connected session (MaxShells: mark_dead
            # + immediate dispose, same as exec hard-timeout).
            failed = self._identity_probe_hard_fail(_safe_msg(exc), data)
            data.clear()
            data.update(failed)

    def _capability_seeds_raw(
        self,
        session: AdaptedWinRMSession | None,
    ) -> dict[str, Any]:
        """Collect capability-related seeds from meta / session (no network)."""
        raw: dict[str, Any] = {}
        for key in _WINRM_PS_RAW_KEYS:
            val = self.meta.get(key)
            if val is None and session is not None:
                val = session.seed_attr(key)
            if val is not None:
                raw[key] = val
        return _normalize_winrm_ps_raw(raw)

    def _apply_winrm_ps(self, data: dict[str, Any], winrm_ps: dict[str, Any]) -> None:
        """Merge winrm_ps into probe *data* and flat convenience keys.

        os/shell convenience defaults are applied only when the merge carries a
        real identity/capability signal (ps_version, language_mode, os_version,
        or data already seeded). Incomplete probes (empty RTT leftovers,
        ``ps_probe=failed`` without those fields) must not invent os/shell that
        would satisfy :meth:`_identity_proven` / collect_probe early-return.
        """
        data["winrm_ps"] = winrm_ps
        # Flat keys used by Agent/open meta summaries when present.
        for key in (
            "ps_version",
            "language_mode",
            "ps_edition",
            "os_version",
            "ps_script_fs",
            "ps_oneshot",
            "ps_runspace",
        ):
            if key in winrm_ps and winrm_ps[key] is not None:
                data.setdefault(key, winrm_ps[key])
        # Keep meta.ps_version / shell convenience aligned when probe filled them.
        if winrm_ps.get("ps_version") is not None:
            self.meta.setdefault("ps_version", winrm_ps["ps_version"])
            data.setdefault("ps_version", winrm_ps["ps_version"])
        if winrm_ps.get("language_mode") is not None:
            self.meta["language_mode"] = winrm_ps["language_mode"]
        has_real_identity = bool(
            data.get("os")
            or data.get("shell")
            or data.get("ps_version")
            or winrm_ps.get("ps_version")
            or winrm_ps.get("language_mode")
            or winrm_ps.get("os_version")
        )
        if has_real_identity:
            data.setdefault("shell", data.get("shell") or "powershell")
            data.setdefault("os", data.get("os") or "windows")

    def _execute_ps_stdout(
        self,
        session: AdaptedWinRMSession,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> str:
        """Run oneshot ``execute_ps`` and return decoded stdout text.

        Uses the shared wall-clock bridge so a hung capability or identity probe
        cannot stall open past the probe budget. On timeout raises
        ``TimeoutError`` and never returns leftover stdout (callers must not
        read that as a successful RTT); the caller hard-fails identity when no
        seeds exist, or treats a capability-only gap as soft partial when
        identity is already proven. Orphaned remote work still counts toward
        ``MaxShellsPerUser`` until the session is closed (see module docstring).

        Link failures follow the oneshot exec policy: a ``link_retryable``
        rejection re-handshakes the link and replays the probe **once** inside
        the same budget, recording ``meta["session_resynced"]``. Because the
        probe's exchanges carry no user work, unlike a pooled invoke it does
        replay the rejection of its first exchange. ``budget_timeout`` /
        ``link_fatal`` / unrecognized failures are never retried, and a replay
        that fails again raises exactly as a first failure would, so the
        caller's identity hard-fail path still runs.

        stdout always comes from :func:`_ps_result_to_exec` so a list/tuple of
        pypsrp pipeline objects becomes parseable key=value / JSON lines. The
        default budget resolves through :func:`resolve_winrm_probe_timeout_s`
        (env ``MRC_WINRM_PROBE_TIMEOUT_S`` -> profile -> default) and is read at
        call time; pass a non-positive ``timeout_s`` to skip the wrapper.
        """
        def _call() -> Any:
            return session.execute_ps(script, environment=None)

        if timeout_s is None:
            budget = resolve_winrm_probe_timeout_s(
                profile_value=self.probe_timeout_s or MRC_WINRM_PROBE_TIMEOUT_S
            )
        else:
            budget = float(timeout_s)
        deadline = None if budget <= 0 else time.monotonic() + budget

        def _attempt(limit: float | None) -> str:
            # Align pypsrp op/read with this attempt's share of the budget.
            apply_budget = limit if limit is not None and limit > 0 else None
            with self._pypsrp_timeouts_for_call(apply_budget):
                if apply_budget is None:
                    raw = _call()
                else:
                    raw = self._run_blocking_with_timeout(_call, timeout_s=apply_budget)
            result = _ps_result_to_exec(raw, default_cwd=self.cwd)
            if result.timed_out:
                raise TimeoutError(result.stderr or "identity probe timed out")
            return result.stdout or ""

        before = self._link_round_trips()
        try:
            return _attempt(budget)
        except TimeoutError:
            raise
        except Exception as exc:
            if classify_winrm_failure(exc) != "link_retryable":
                raise
            if not self._link_replay_allowed(before, exc, carries_user_work=False):
                # Already exchanged successfully during this probe: the
                # rejection is not the stale-framing first request, so the
                # request it belongs to may have run. Hard-fail identity.
                raise
            # The probe's exchanges carry no user work, so a rejection of its
            # first payload exchange is safe to repeat: re-handshake and replay
            # once inside the remaining probe budget. Never raises.
            resync_winrm_session(self._session)
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "winrm link rejection recovered the session but the "
                        "probe budget was already spent"
                    ) from exc
            try:
                stdout = _attempt(remaining)
            except Exception as retry_exc:  # noqa: BLE001 - caller hard-fails identity
                # Keep the first rejection's text reachable on the traceback.
                raise retry_exc from exc
            self._note_session_resynced()
            return stdout

    def _parse_probe_stdout(
        self, stdout: str, *, base: dict[str, Any]
    ) -> dict[str, Any]:
        """Parse identity-probe stdout lines into *base* probe fields.

        Empty/whitespace-only output does **not** invent ``os``/``shell``;
        callers use :meth:`_identity_proven` and hard-fail when nothing real
        was observed.

        Non-empty junk (e.g. ``Access Denied``, bare ``error``, HTTP
        ``401 Unauthorized`` / ``500 ...``, a line that merely contains the
        substring ``Windows``) also does **not** invent
        ``os``/``shell``/``ps_version``. Convenience defaults apply only after
        a version-shaped token or a ``Microsoft Windows`` OS banner hits.
        A home/pwd path (``C:\\`` / ``/``) is recorded but does **not**
        invent identity. ``ps_version`` requires a version-shaped token
        (``^\\d+\\.\\d+`` or ``Version`` plus a digit). Otherwise
        :meth:`_identity_proven` stays false and identity RTT hard-fails.
        """
        data = dict(base)
        lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
        if not lines:
            # Do not setdefault os/shell here - invented defaults would make
            # _identity_proven true and leave open fake-connected.
            data["status"] = "partial"
            data["error"] = "empty probe output"
            return data
        # Heuristic parse: version-shaped -> ps_version; path-like -> home/pwd
        # (not identity); Microsoft Windows banner -> os. Track whether a
        # real identity signal was observed (paths alone do not count).
        hit = bool(
            data.get("os") or data.get("shell") or data.get("ps_version")
        )
        for line in lines:
            if "ps_version" not in data and _is_probe_ps_version_line(line):
                data["ps_version"] = line
                hit = True
                continue
            if ("home" not in data) and (
                line.startswith("C:\\") or line.startswith("C:/") or line.startswith("/")
            ):
                data["home"] = line
                self.home = self.home or line
                continue
            if ("pwd" not in data) and (
                line.startswith("C:\\") or line.startswith("C:/") or line.startswith("/")
            ):
                data["pwd"] = line
                self.cwd = self.cwd or line
                continue
            # OS banner only - a line that merely contains "Windows"
            # (401 / Access Denied noise) is not identity.
            if "Microsoft Windows" in line:
                data["os"] = line
                hit = True
        if hit:
            # Real probe content (or trusted base seeds): convenience defaults.
            data.setdefault("os", "windows")
            data.setdefault("shell", "powershell")
            data.setdefault("status", "ok")
        else:
            # Non-empty garbage without identity hits - leave os/shell absent
            # so _identity_proven is false (callers mark_dead / open fail).
            data["status"] = "partial"
            data.setdefault("error", "identity probe empty or unusable")
        return data

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if command is None or (isinstance(command, str) and command.strip() == ""):
            raise TransportError("INVALID_ARG", "command is required")
        session = self._require_session()
        # Same bleed guard as SSH: bool/True/"True"/None never become `cd True`.
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)

        if session.has_run_command:
            return self._map_call_to_exec(
                lambda: session.run_command(
                    command, cwd=work, timeout_s=timeout_s, env=env
                ),
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Prefer PowerShell oneshot for shell-string commands (Windows remote
        # default shell is PowerShell; execute_cmd is the fallback). The exit
        # probe captures $LASTEXITCODE so a native non-zero is not collapsed to
        # exit 0 when had_errors is false, and the cwd prefix comes from
        # shell_wrap (PS -ErrorAction Stop short-circuit). The caller's text is
        # dot-sourced so a top-level ``return`` ends only its own block and the
        # probe still runs, while a top-level ``exit`` ends the whole pipeline:
        # _coerce_ps_oneshot_exec reports it as an unknown exit code, not success.
        if session.has_execute_ps:
            script = _append_ps_exit_probe(
                _isolate_user_script(
                    wrap_with_cwd(command, work, shell_family="powershell")
                )
            )
            return self._call_and_coerce(
                session.execute_ps,
                script,
                env=env,
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_ps_oneshot_exec,
            )

        if session.has_execute_cmd:
            full = wrap_with_cwd(command, work, shell_family="cmd")
            return self._call_and_coerce(
                session.execute_cmd,
                full,
                env=env,
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        raise TransportError(
            "UNSUPPORTED",
            "winrm session has no run_command/execute_ps/execute_cmd for exec",
            details={"host": self.host},
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
        session = self._require_session()
        # Same bleed guard as SSH: bool/True/"True"/None never become `cd True`.
        work = coerce_cwd_path(cwd if cwd is not None else self.cwd)
        argv_list = [str(a) for a in argv]

        if session.has_run_argv:
            return self._map_call_to_exec(
                lambda: session.run_argv(
                    argv_list, cwd=work, timeout_s=timeout_s, env=env
                ),
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Prefer the PowerShell call-operator (``& exe @(args)``) over
        # execute_cmd: argv must reach the remote program verbatim, and cmd.exe
        # expands ``%VAR%`` even inside double quotes (see ``_win_quote``),
        # while the call-operator passes args as PowerShell strings with no cmd
        # interpolation. execute_cmd remains a fallback for sessions without
        # execute_ps. The exit probe maps native $LASTEXITCODE into ExecResult,
        # and the generated call is dot-sourced for the same reason as
        # run_command (an ``exit`` is reported as an unknown code).
        if session.has_execute_ps:
            if len(argv_list) == 1:
                script = f"& {_ps_single_quote(argv_list[0])}"
            else:
                exe = _ps_single_quote(argv_list[0])
                args = ", ".join(_ps_single_quote(a) for a in argv_list[1:])
                script = f"& {exe} @({args})"
            script = _append_ps_exit_probe(
                _isolate_user_script(
                    wrap_with_cwd(script, work, shell_family="powershell")
                )
            )
            return self._call_and_coerce(
                session.execute_ps,
                script,
                env=env,
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_ps_oneshot_exec,
            )

        # Fallback: cmd shell-join (``%`` expansion is inherent to cmd here).
        if session.has_execute_cmd:
            joined = " ".join(_win_quote(a) for a in argv_list)
            full = wrap_with_cwd(joined, work, shell_family="cmd")
            return self._call_and_coerce(
                session.execute_cmd,
                full,
                env=env,
                timeout_s=timeout_s,
                cwd=work,
                coerce=_coerce_exec_result,
            )

        # Last resort: shell-join via run_command when that surface exists.
        quoted = " ".join(shlex.quote(a) for a in argv_list)
        return self.run_command(quoted, cwd=work, timeout_s=timeout_s, env=env)

    def _require_session(self) -> AdaptedWinRMSession:
        if not self._connected or self._session is None:
            raise TransportError(
                "NOT_CONNECTED",
                "winrm transport is not connected",
                details={"host": self.host},
            )
        return self._session

    # ------------------------------------------------------------------
    # Filesystem client hook (fs backend)
    # ------------------------------------------------------------------

    def open_fs(self) -> Any:
        """Lazy-open a file client from the connected WinRM session.

        Resolution order (capability frozen at connect):

        - ``open_fs()`` -> file client
        - file methods on the session itself (``listdir`` / ``stat`` / ...)
        - pypsrp ``copy``/``fetch``/``execute_ps`` via ``PypsrpFileClient``

        Serialized with other serial ops via transport ``_op_lock``.
        """
        session = self._require_session()

        if session.has_open_fs:
            try:
                client = session.open_fs()
            except TransportError:
                raise
            except Exception as exc:
                raise TransportError(
                    "FS_ERROR",
                    f"open_fs failed: {_safe_msg(exc)}",
                    details={"host": self.host},
                ) from exc
            if client is None:
                raise TransportError(
                    "FS_ERROR",
                    "open_fs returned no client",
                    details={"host": self.host},
                )
            return client

        # Session itself is a file store (implements WinRMFileClient methods).
        if session.is_file_client:
            return session.raw

        # pypsrp Client surface: copy/fetch (+ execute_ps for list/stat/...).
        if session.can_build_pypsrp_fs:
            from mcp_remote_control.transport.winrm_files import PypsrpFileClient

            # Pass the raw object so optional copy/fetch presence is exact
            # (AdaptedWinRMSession always exposes methods). fs and exec share
            # one WSMan connection: hand the client the same serial-ops lock the
            # transport uses so fs calls cannot interleave with exec/ps, and a
            # link failure observed on the fs path drops the poisoned session
            # the same way an exec link failure does. The timed variant lets an
            # fs call bound its wait for that lock by the remaining whole-op
            # budget instead of overshooting it before its first remote call.
            return PypsrpFileClient(
                session.raw,
                serial_ops=self.serial_ops,
                serial_ops_within=self.serial_ops_within,
                on_link_failure=self._on_fs_link_failure,
            )

        raise TransportError(
            "UNSUPPORTED",
            "winrm session has no filesystem client (open_fs/copy/fetch)",
            details={"host": self.host},
        )

    # ------------------------------------------------------------------
    # Persistent PowerShell runspace (ps open / invoke / close)
    # ------------------------------------------------------------------

    def open_runspace(self) -> Any:
        """Open a persistent PSRP runspace; always return a RunspaceHandle adapter.

        Prefer ``session.open_runspace()`` when present; otherwise open a pypsrp
        ``RunspacePool`` against ``session.wsman``. Raw handles and pools are
        wrapped once here so :meth:`runspace_invoke` only calls adapter
        ``invoke`` / ``stop`` / ``close``.

        Blocking open (session factory or ``pool.open``) runs under a wall-clock
        budget (:data:`_RUNSPACE_OPEN_CLOSE_TIMEOUT_S`) so a blackholed WSMan
        peer cannot hang the calling thread (ps open / registry serial ops).

        Link failures follow the oneshot policy (:meth:`_map_call_to_exec`). The
        open's first payload exchange is the structural runspace ``Create``,
        which carries no caller work, so a ``link_retryable`` rejection that is
        provably pre-execution (or provably belongs to that first exchange; see
        :meth:`_link_replay_allowed`) re-handshakes the link and replays the open
        **once** inside the same budget. Every other link-implicated failure
        marks the link dead and raises ``EXEC_FAILED`` honestly, so the next op
        reconnects instead of the endpoint reporting "connected" while every
        later open fails identically. A WSMan fault, or any unrecognized
        failure, comes from a reachable server and leaves the link alone.

        Serialized with other serial ops via transport ``_op_lock``.
        """
        session = self._require_session()
        budget = float(_RUNSPACE_OPEN_CLOSE_TIMEOUT_S)
        if session.has_open_runspace:
            def _open_session_runspace(limit: float) -> Any:
                handle = self._run_blocking_with_timeout(
                    session.open_runspace,
                    timeout_s=limit,
                )
                if handle is None:
                    raise TransportError(
                        "EXEC_FAILED",
                        "open_runspace returned no handle",
                        details={"host": self.host},
                    )
                return handle

            handle = self._open_runspace_linked(
                _open_session_runspace,
                budget=budget,
                what="open_runspace",
            )
            return _adapt_runspace_handle(
                handle,
                default_location=self.cwd,
                op_lock=self.op_lock,
                # The transport's serial-zone registry travels with the lock:
                # a release this handle retains has to be retried by whatever
                # enters the transport's zone next, not only by this handle.
                serial_zone_hooks=self.serial_zone_hooks,
            )

        wsman = session.wsman
        if wsman is not None:
            try:
                from pypsrp.powershell import RunspacePool
            except ImportError as exc:  # pragma: no cover - hard dep in practice
                raise TransportError(
                    "UNSUPPORTED",
                    "pypsrp is required for PowerShell runspaces",
                    details={"host": self.host},
                ) from exc
            # A failed pool is closed before the next attempt builds its own:
            # pool.open() may have left a half-open remote runspace behind.
            stale_pools: list[Any] = []

            def _open_pypsrp_pool(limit: float) -> Any:
                # Constructed on the caller's thread so the reference exists
                # here even when the bridged ``open`` is abandoned on a
                # timeout; a replay needs its own pool (a failed open leaves
                # the previous object in an unusable state).
                pool: Any = RunspacePool(wsman)
                stale_pools.append(pool)
                self._run_blocking_with_timeout(pool.open, timeout_s=limit)
                return pool

            def _discard_stale_pools() -> None:
                for stale in stale_pools:
                    self._best_effort_close_runspace_obj(stale, timeout_s=budget)
                stale_pools.clear()

            handle = self._open_runspace_linked(
                _open_pypsrp_pool,
                budget=budget,
                what="RunspacePool open",
                discard=_discard_stale_pools,
            )
            return _adapt_runspace_handle(
                handle,
                default_location=self.cwd,
                op_lock=self.op_lock,
                # The transport's serial-zone registry travels with the lock:
                # a release this handle retains has to be retried by whatever
                # enters the transport's zone next, not only by this handle.
                serial_zone_hooks=self.serial_zone_hooks,
            )

        raise TransportError(
            "UNSUPPORTED",
            "winrm session cannot open a PowerShell runspace",
            details={"host": self.host},
        )

    def _open_runspace_linked(
        self,
        attempt: Callable[[float], Any],
        *,
        budget: float,
        what: str,
        discard: Callable[[], None] | None = None,
    ) -> Any:
        """Run one runspace-open attempt under the oneshot link policy.

        *attempt* takes the wall clock for its own try, performs the blocking
        open itself (so anything it constructs stays referenced on this
        thread even when the call is abandoned on a timeout) and returns the
        raw handle. *discard* is the best-effort teardown for an attempt that
        failed - a half-open pool - and runs on every failure path before any
        replay. *what* names the surface in the mapped error text so a failure
        keeps saying which open failed.
        """
        deadline = time.monotonic() + budget
        before = self._link_round_trips()

        def _timeout_error() -> TransportError:
            return TransportError(
                "EXEC_FAILED",
                f"{what} timed out after {budget}s",
                details={"host": self.host, "timeout_s": budget},
            )

        def _link_lost_error(exc: BaseException) -> TransportError:
            return TransportError(
                "EXEC_FAILED",
                f"winrm link lost during {what}: {_safe_msg(exc)}",
                details={"host": self.host},
            )

        def _plain_error(exc: BaseException) -> TransportError:
            return TransportError(
                "EXEC_FAILED",
                f"{what} failed: {_safe_msg(exc)}",
                details={"host": self.host},
            )

        def _discard_stale() -> None:
            if discard is not None:
                discard()

        try:
            return attempt(budget)
        except TimeoutError as exc:
            _discard_stale()
            raise _timeout_error() from exc
        except TransportError:
            # Already mapped by the session factory (or a caller-supplied
            # double): keep its code, but still drop a half-open pool.
            _discard_stale()
            raise
        except Exception as exc:
            kind = classify_winrm_failure(exc)
            if kind == "budget_timeout":
                _discard_stale()
                raise _timeout_error() from exc
            if kind not in ("link_retryable", "link_fatal"):
                # A reachable server answered (a WSMan SOAP fault) or the
                # failure is unrecognized: the link is not implicated.
                _discard_stale()
                raise _plain_error(exc) from exc
            if not (
                kind == "link_retryable"
                and self._link_replay_allowed(before, exc, carries_user_work=True)
            ):
                # The rejection is not provably pre-execution, or a
                # ``link_fatal`` failure may already have run remotely: report
                # and drop the poisoned session rather than replay into an
                # unknown state.
                _discard_stale()
                self._mark_link_dead()
                raise _link_lost_error(exc) from exc
            # Provably refused (empty-body 4xx from the framing layer, or a
            # rejection this open provably started with): the request never
            # reached a pipeline, so the Create is safe to repeat. Discard the
            # half-open attempt, re-handshake, and replay once sharing the same
            # wall clock (the budget rule of :meth:`_resync_then_retry`, which
            # cannot be used here because each attempt brings its own pool).
            _discard_stale()
            resync_winrm_session(self._session)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _timeout_error() from exc
            try:
                handle = attempt(remaining)
            except TimeoutError as retry_exc:
                _discard_stale()
                raise _timeout_error() from retry_exc
            except TransportError:
                # The replay's own error code is preserved, as on the first
                # attempt; the half-open pool of that attempt is still dropped.
                _discard_stale()
                raise
            except Exception as retry_exc:  # noqa: BLE001 - mapped below
                _discard_stale()
                retry_kind = classify_winrm_failure(retry_exc)
                if retry_kind == "budget_timeout":
                    raise _timeout_error() from retry_exc
                if retry_kind in ("link_retryable", "link_fatal"):
                    self._mark_link_dead()
                    raise _link_lost_error(retry_exc) from retry_exc
                raise _plain_error(retry_exc) from retry_exc
            self._note_session_resynced()
            return handle

    def runspace_invoke(
        self,
        handle: Any,
        script: str,
        *,
        timeout_s: float | None = None,
    ) -> RunspaceResult:
        """Invoke *script* on a RunspaceHandle adapter; return output + location.

        *handle* must come from :meth:`open_runspace` (always an adapter).
        ``timeout_s`` is a wall-clock budget: blocking ``invoke`` runs on the
        async-bridge loop via ``asyncio.to_thread``. On timeout ``handle.stop()``
        is called best-effort (pool adapters dispose the pipeline only so the
        runspace stays reusable).

        Link failures follow the oneshot policy (:meth:`_map_call_to_exec`),
        with one deliberate difference: an invoke's first WSMan message is the
        ``Command`` that carries the script, so no completed exchange is needed
        for a side effect to exist - a front gateway can fail behind a request
        it already delivered. The replay is therefore granted only to a request
        that is **provably refused** (:func:`is_winrm_refusal`: a 4xx rejection
        with an empty body, which never reached a pipeline) and never to a
        rejection carrying a body, or to one observed after this invoke already
        completed a payload exchange. A ``link_fatal`` failure is never
        replayed for the same reason. A replay that fails again marks the
        transport dead and returns a link-lost result; the runspace dies with
        the session, so there is nothing left to stop.

        Serialized with other serial ops via transport ``_op_lock``.
        """
        if handle is None:
            raise TransportError(
                "PS_CLOSED",
                "runspace handle is missing",
                details={"host": self.host},
            )
        if script is None:
            raise TransportError("INVALID_ARG", "script is required")

        invoker = getattr(handle, "invoke", None)
        if not callable(invoker):
            raise TransportError(
                "PS_CLOSED",
                "runspace handle has no invoke",
                details={"host": self.host},
            )

        default_loc = getattr(handle, "location", None) or self.cwd
        deadline = (
            None
            if timeout_s is None or timeout_s <= 0
            else time.monotonic() + float(timeout_s)
        )
        before = self._link_round_trips()
        try:
            raw = self._invoke_handle(handle, invoker, script, timeout_s=timeout_s)
        except TransportError:
            raise
        except TimeoutError as exc:
            return RunspaceResult(
                stdout="",
                stderr=str(exc) or "timeout",
                exit_code=-1,
                location=default_loc,
                had_errors=True,
                timed_out=True,
            )
        except Exception as exc:
            kind = classify_winrm_failure(exc)
            if kind == "budget_timeout":
                self._stop_handle(handle)
                return RunspaceResult(
                    stdout="",
                    stderr=_safe_msg(exc),
                    exit_code=-1,
                    location=default_loc,
                    had_errors=True,
                    timed_out=True,
                )
            if kind == "link_fatal" or (
                kind == "link_retryable"
                and not self._link_replay_allowed(
                    before, exc, first_payload_is_command=True
                )
            ):
                # The invoke may have run remotely: its first WSMan message is
                # the ``Command`` that carries the script, so a completed
                # exchange is not needed for a side effect to exist. A rejection
                # carrying a body came from an intermediary that may have
                # forwarded it, so neither is provably pre-execution: report and
                # drop the poisoned session.
                self._mark_link_dead()
                raise TransportError(
                    "EXEC_FAILED",
                    _safe_msg(exc),
                    details={"host": self.host},
                ) from exc
            if kind == "link_retryable":
                # Provably refused (empty-body framing rejection) and this
                # invoke had not completed an exchange: re-handshake, then
                # replay with the same handle; the pool lives on the session
                # that was just re-handshaked.
                recovered, payload = self._resync_then_retry(
                    lambda: self._invoke_handle(
                        handle, invoker, script, timeout_s=timeout_s
                    ),
                    timeout_s=timeout_s,
                    deadline=deadline,
                )
                if not recovered:
                    verdict = self._retry_failure_kind(payload)
                    if verdict == "timeout":
                        self._stop_handle(handle)
                        return RunspaceResult(
                            stdout="",
                            stderr=_safe_msg(payload) or "timeout",
                            exit_code=-1,
                            location=default_loc,
                            had_errors=True,
                            timed_out=True,
                        )
                    if verdict == "fatal":
                        self._mark_link_dead()
                        raise TransportError(
                            "EXEC_FAILED",
                            _safe_msg(payload),
                            details={"host": self.host},
                        ) from payload
                    return self._link_lost_runspace_result(
                        location=default_loc,
                        stderr=_safe_msg(payload),
                    )
                self._note_session_resynced()
                raw = payload
            elif _is_timeout_exc(exc):
                # Legacy text/name timeout heuristic for failures the taxonomy
                # does not recognize (e.g. a caller-supplied mock exception).
                self._stop_handle(handle)
                return RunspaceResult(
                    stdout="",
                    stderr=_safe_msg(exc),
                    exit_code=-1,
                    location=default_loc,
                    had_errors=True,
                    timed_out=True,
                )
            else:
                raise TransportError(
                    "EXEC_FAILED",
                    _safe_msg(exc),
                    details={"host": self.host},
                ) from exc

        # Location belongs to the runspace only (handle.location /
        # RunspaceResult.location). Do NOT write back into self.cwd: a
        # Set-Location inside ps must not pollute exec/fs default work paths.
        return _coerce_runspace_result(raw, default_location=default_loc)

    def _invoke_handle(
        self,
        handle: Any,
        invoker: Any,
        script: str,
        *,
        timeout_s: float | None,
    ) -> Any:
        """Run ``handle.invoke(script)`` with optional wall-clock timeout + stop.

        Prefer ``handle.prepare_invoke(script) -> (run, stop)`` when present
        (pool adapter): on timeout only that invoke's ``stop`` runs, so a
        concurrent invoke on the same session is not cancelled. Handles without
        ``prepare_invoke`` fall back to ``invoke`` + ``handle.stop()``.

        Either stop runs on the caller's thread while it holds the transport
        ``_op_lock``, so both are wall-clock bounded (see :meth:`_stop_handle`)
        rather than waited on indefinitely.
        """
        prepare = getattr(handle, "prepare_invoke", None)
        with self._pypsrp_timeouts_for_call(timeout_s):
            if callable(prepare):
                run, stop_fn = prepare(script)
                if timeout_s is None or timeout_s <= 0:
                    return run()
                try:
                    return self._run_blocking_with_timeout(run, timeout_s=timeout_s)
                except TimeoutError:
                    # Bounded by :data:`_STOP_DEADLINE_S` exactly like
                    # :meth:`_stop_handle`: this runs on the caller's thread
                    # while it holds the transport ``_op_lock``, so a blocking
                    # per-invoke stop would pin that lock past the invoke's own
                    # timeout and freeze every later op on the endpoint -
                    # including ``endpoint close``, the documented remedy.
                    _call_with_deadline(stop_fn, deadline_s=_STOP_DEADLINE_S)
                    raise

            if timeout_s is None or timeout_s <= 0:
                return invoker(script)
            try:
                return self._run_blocking_with_timeout(
                    lambda: invoker(script),
                    timeout_s=timeout_s,
                )
            except TimeoutError:
                self._stop_handle(handle)
                raise

    @staticmethod
    def _stop_handle(handle: Any) -> None:
        """Best-effort interrupt of an in-flight invoke (adapter or raw).

        Bounded by :data:`_STOP_DEADLINE_S` on a daemon thread, the same way
        :func:`_safe_stop_pipeline` and :meth:`_best_effort_close_runspace_obj`
        bound theirs. This runs on the caller's thread while it holds the
        transport ``_op_lock`` (``runspace_invoke`` is a serial op), so an
        unbounded ``stop()`` would pin that lock past the invoke's own timeout
        and freeze every later op on the endpoint - including
        ``endpoint close``, the documented remedy. The wait is abandoned at the
        deadline while the unfinished stop continues on the daemon thread.

        For pool adapters, ``stop()`` is intentionally a no-op (per-invoke
        cancel uses ``prepare_invoke``'s stop callback). Invoke adapters and
        raw handles that expose a real ``stop()`` still get it here.
        """
        stop = getattr(handle, "stop", None)
        if not callable(stop):
            return
        _call_with_deadline(stop, deadline_s=_STOP_DEADLINE_S)

    def close_runspace(self, handle: Any) -> str:
        """Close a runspace handle; return whether the WSMan ``Delete`` landed.

        Verdicts (module constants): :data:`_CLOSE_LANDED` when the closer
        returned - the remote runspace is gone, or there was nothing to close -
        :data:`_CLOSE_TIMEOUT` when the wall-clock budget elapsed and the wait
        was abandoned, :data:`_CLOSE_UNCONFIRMED` when the closer failed and the
        delete is not proven to have landed. A caller reporting a teardown must
        use this verdict: "best effort" is no licence to claim a delete that
        never happened. A WSMan fault naming the shell as unknown (see
        :func:`_fault_shows_runspace_gone`) proves the runspace gone and reports
        :data:`_CLOSE_LANDED`; any other fault reports :data:`_CLOSE_UNCONFIRMED`.

        The whole teardown runs under the transport serial lock, and the wait
        for that lock is drawn from the same wall-clock budget
        (:data:`_RUNSPACE_OPEN_CLOSE_TIMEOUT_S`), so a hung ``pool.close`` /
        WSMan delete or a peer op holding the lock cannot pin this caller. On
        timeout the wait is abandoned with a warning (``to_thread`` cannot
        cancel); a caller that owns a deadline uses
        :meth:`close_runspace_within` to share that one budget.

        Link policy: a provably pre-execution ``link_retryable`` rejection
        re-handshakes the link and issues the delete once more, sharing the same
        wall clock; any other link-implicated failure marks the link dead and
        reports ``_CLOSE_UNCONFIRMED``.
        """
        return self.close_runspace_within(
            handle,
            timeout_s=float(_RUNSPACE_OPEN_CLOSE_TIMEOUT_S),
        )

    def close_runspace_within(self, handle: Any, *, timeout_s: float) -> str:
        """Close *handle* inside a caller-owned wall-clock deadline.

        Same verdict contract and link policy as :meth:`close_runspace` (see
        there for the delete and link rules), but the entire teardown - the
        delete **and** any re-handshake replay - shares *timeout_s* instead of
        starting a fresh :data:`_RUNSPACE_OPEN_CLOSE_TIMEOUT_S` budget. A
        caller that already spent part of its deadline (a ps close waits for
        the serial lock first) passes the remainder here, so lock wait, delete
        and recovery cannot each be handed a full budget.

        The wait for the transport serial lock is part of the same deadline: a
        caller with no deadline of its own (:meth:`close_runspace`, and the
        ps-open fence cleanup that calls it) cannot be pinned by whatever holds
        the lock, and a caller that already owns the lock (a ps close) enters
        immediately because the lock is re-entrant on this thread.

        ``timeout_s`` that is not positive is an already-spent deadline, and so
        is a wait for the serial lock that exhausts it: the verdict is
        :data:`_CLOSE_TIMEOUT` and no delete is started, so a caller that ran
        out of wall clock never opens a new teardown it cannot wait for.
        """
        if not float(timeout_s) > 0.0:  # NaN and <= 0 are spent deadlines
            return _CLOSE_TIMEOUT
        limit = float(timeout_s)
        deadline = time.monotonic() + limit
        if not self.op_lock.acquire(timeout=limit):
            # The wait for the serial lock spent the whole deadline. A peer op
            # owns the transport, so the delete is not started at all.
            _log.warning(
                "winrm close_runspace timed out waiting for the transport "
                "serial lock after %ss (host=%s)",
                limit,
                self.host,
            )
            return _CLOSE_TIMEOUT
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:  # the acquire itself spent the deadline
                return _CLOSE_TIMEOUT
            return self._close_runspace_with_budget(handle, budget=remaining)
        finally:
            self.op_lock.release()

    def _best_effort_close_runspace_obj(
        self,
        handle: Any,
        *,
        timeout_s: float,
    ) -> None:
        """Best-effort ``handle.close()`` under wall-clock; never raises.

        The same policy as :meth:`close_runspace` for callers with no teardown
        status to report (half-open pool cleanup after a failed open); only the
        verdict is dropped.
        """
        self._close_runspace_with_budget(handle, budget=timeout_s)

    def _close_runspace_with_budget(self, handle: Any, *, budget: float) -> str:
        """Close *handle* under *budget*; never raises; returns a verdict."""
        if handle is None:
            return _CLOSE_LANDED
        closer = getattr(handle, "close", None)
        if not callable(closer):
            return _CLOSE_LANDED
        limit = float(budget) if budget and budget > 0 else float(
            _RUNSPACE_OPEN_CLOSE_TIMEOUT_S
        )
        deadline = time.monotonic() + limit
        before = self._link_round_trips()

        def _timeout_verdict() -> str:
            _log.warning(
                "winrm close_runspace timed out after %ss "
                "(best-effort abandon, host=%s)",
                limit,
                self.host,
            )
            return _CLOSE_TIMEOUT

        try:
            self._run_blocking_with_timeout(closer, timeout_s=limit)
            return _CLOSE_LANDED
        except TimeoutError:
            return _timeout_verdict()
        except Exception as exc:  # noqa: BLE001 - the verdict is the contract
            kind = classify_winrm_failure(exc)
            if kind == "budget_timeout":
                # A bridge budget that surfaced as a library timeout: the
                # closer may still run on its abandoned thread, so this is the
                # same meaning as a wall-clock miss.
                return _timeout_verdict()
            if kind not in ("link_retryable", "link_fatal"):
                # A reachable server answered (a WSMan fault) or the failure is
                # unrecognized; the link is not implicated, and the delete is
                # not proven either way - unless the fault itself names the
                # shell as unknown: then the runspace is provably gone already
                # and there is nothing left to delete.
                if _fault_shows_runspace_gone(exc):
                    _log.debug(
                        "winrm close_runspace: server reported no such shell "
                        "(host=%s): %s",
                        self.host,
                        _safe_msg(exc),
                    )
                    return _CLOSE_LANDED
                return _CLOSE_UNCONFIRMED
            if kind == "link_retryable" and self._link_replay_allowed(
                before, exc, carries_user_work=True
            ):
                # Provably refused before any pipeline existed: re-handshake
                # and replay the delete once, sharing the same wall clock.
                resync_winrm_session(self._session)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _timeout_verdict()
                try:
                    self._run_blocking_with_timeout(closer, timeout_s=remaining)
                except TimeoutError:
                    return _timeout_verdict()
                except Exception as retry_exc:  # noqa: BLE001 - reported below
                    if _fault_shows_runspace_gone(retry_exc):
                        # The server answers that no such shell exists, so one
                        # of the two Deletes landed (or the runspace was
                        # already gone): the wanted end state holds.
                        self._note_session_resynced()
                        return _CLOSE_LANDED
                    if classify_winrm_failure(retry_exc) in (
                        "link_retryable",
                        "link_fatal",
                    ):
                        self._mark_link_dead()
                    _log.warning(
                        "winrm close_runspace: replayed delete rejected "
                        "(host=%s): %s",
                        self.host,
                        _safe_msg(retry_exc),
                    )
                    return _CLOSE_UNCONFIRMED
                self._note_session_resynced()
                return _CLOSE_LANDED
            # Not provably pre-execution, or a ``link_fatal`` failure (read /
            # connect timeout): the delete may already have reached the
            # server, so replaying could repeat it. Report and drop the
            # poisoned session.
            self._mark_link_dead()
            return _CLOSE_UNCONFIRMED

    def _map_call_to_exec(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float | None,
        cwd: str | None,
        coerce: Callable[..., ExecResult],
    ) -> ExecResult:
        """Run *fn* with optional wall-clock timeout; map to ``ExecResult``.

        Shared by oneshot (via :meth:`_call_and_coerce`) and high-level
        ``session.run_command`` / ``run_argv``. ``TransportError`` propagates,
        other failures become ``EXEC_FAILED``. The resolved pypsrp
        ``operation_timeout`` / ``read_timeout`` pair (profile explicit wins,
        else ``ceil(timeout_s)``) is applied to the live session first.

        Hard timeout: wall-clock deadlines leave remote shells that the client
        cannot cancel, so this path calls :meth:`mark_dead` **and** immediately
        best-effort :meth:`_dispose_prior_session` (``session.close``) so
        MaxShells pressure drops without waiting for the next connect; registry
        ``ensure_connected`` then reopens a fresh session. It does **not**
        pretend the remote PowerShell was Stopped - only the local client
        session is closed (see module docstring / ``MaxShellsPerUser``).

        Link failure: every non-timeout failure is bucketed by
        :func:`classify_winrm_failure` first. A ``link_retryable`` rejection is
        gated on the round-trip counter (:meth:`_link_replay_allowed`) and, when
        the rejected request may be repeated, the link is re-handshaked and *fn*
        retried **once** - the plain-HTTP message-encryption self-heal. A
        ``link_fatal`` failure, or a retryable rejection seen after this
        operation already completed an exchange, may have run remotely: it marks
        the link dead and raises. Retrying is bounded to that one extra attempt.
        """
        with self._pypsrp_timeouts_for_call(timeout_s):
            deadline = (
                None
                if timeout_s is None or timeout_s <= 0
                else time.monotonic() + float(timeout_s)
            )
            before = self._link_round_trips()
            attempt = self._call_with_budget(fn, timeout_s=timeout_s)
            resynced = False
            try:
                raw = attempt()
            except TimeoutError as exc:
                return self._hard_timeout_exec_result(
                    cwd=cwd,
                    stderr=str(exc) or "timeout",
                )
            except TransportError:
                raise
            except Exception as exc:
                kind = classify_winrm_failure(exc)
                if kind == "budget_timeout":
                    return self._hard_timeout_exec_result(
                        cwd=cwd,
                        stderr=_safe_msg(exc) or "timeout",
                    )
                replayable = kind == "link_retryable" and self._link_replay_allowed(
                    before, exc, carries_user_work=True
                )
                if replayable:
                    recovered, payload = self._resync_then_retry(
                        fn, timeout_s=timeout_s, deadline=deadline
                    )
                    if not recovered:
                        return self._retry_failed_exec_result(payload, cwd=cwd)
                    raw = payload
                    resynced = True
                elif kind == "link_fatal" or kind == "link_retryable":
                    # A link_fatal failure may have run remotely, and a
                    # retryable rejection observed after this operation already
                    # completed a payload exchange is not provably its first
                    # request: either way a replay could repeat a side effect,
                    # so report and drop the poisoned session.
                    self._mark_link_dead()
                    raise TransportError(
                        "EXEC_FAILED",
                        _safe_msg(exc),
                        details={"host": self.host},
                    ) from exc
                else:
                    # Unrecognized, or an answer from a reachable server (a
                    # WSMan SOAP fault): the link is not implicated.
                    raise TransportError(
                        "EXEC_FAILED",
                        _safe_msg(exc),
                        details={"host": self.host},
                    ) from exc
            if resynced:
                self._note_session_resynced()
            return coerce(raw, default_cwd=cwd)

    def _call_with_budget(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float | None,
    ) -> Callable[[], Any]:
        """Wrap *fn* in the shared wall-clock bridge when a budget applies.

        ``None`` / non-positive ``timeout_s`` means unlimited: *fn* is returned
        unwrapped so no executor thread is spawned for a call without a budget.
        """
        if timeout_s is None or timeout_s <= 0:
            return fn
        return lambda: self._run_blocking_with_timeout(fn, timeout_s=timeout_s)

    def _note_replay_guard_degraded(self, reason: str) -> None:
        """Record that the replay guard cannot observe the link; warn once.

        The counter is installed by wrapping pypsrp's private
        ``_send_request``; a rename/rewrap there, or a session with no pypsrp
        HTTP node, leaves :meth:`_link_replay_allowed` without evidence about
        how many exchanges an operation completed. Replay decisions then fall
        back to the per-path policy (see that method for why), which is only
        safe while the session really is a double or a connector with no HTTP
        surface. Recording the state in ``meta`` and logging once keeps that
        fallback from being silent: an operator reading the endpoint sees which
        of the two worlds they are in. The warning is emitted once per session
        (re-armed by :meth:`connect`); ``meta`` is updated on every call, so a
        later unreadable counter is still recorded.

        *reason* is ``"no_counter"`` (nothing countable at connect) or
        ``"unreadable"`` (a reader failed mid-call).
        """
        self.meta["link_replay_guard"] = "degraded"
        self.meta["link_replay_guard_reason"] = reason
        if self._replay_guard_warned:
            return
        self._replay_guard_warned = True
        _log.warning(
            "winrm replay guard degraded (%s) for host=%s: the round-trip "
            "counter is not observable, so a link_retryable rejection cannot "
            "be proven to be the operation's first request; replay falls back "
            "to the per-path policy (see _link_replay_allowed)",
            reason,
            self.host,
        )

    def _link_round_trips(self) -> int | None:
        """Successful HTTP exchanges so far, or ``None`` when unobservable.

        ``None`` covers both a session with no countable transport and a reader
        that fails at call time: an unreadable counter must not break the
        operation it was meant to guard. Either way the degraded state is
        recorded in ``meta`` (and logged once) - a guard that has silently
        stopped observing the link is exactly the state an operator needs to
        see.
        """
        reader = self._round_trips
        if reader is None:
            return None
        try:
            return int(reader())
        except Exception:  # noqa: BLE001 - a broken counter is not a call failure
            self._note_replay_guard_degraded("unreadable")
            return None

    def _link_replay_allowed(
        self,
        before: int | None,
        exc: BaseException,
        *,
        first_payload_is_command: bool = False,
        carries_user_work: bool = True,
    ) -> bool:
        """Whether a ``link_retryable`` rejection may be replayed.

        The stale-framing signature can only hit an operation's **first**
        payload-carrying request - the cached message-encryption context needs
        an idle gap of several seconds to go stale, and any successful exchange
        in between rebuilds it - so a counter readable at start that has
        advanced since proves the rejected request is not that first one: a
        replay could repeat a side effect, and the failure must be fatal.
        ``before`` / ``after`` are that counter around the attempt.

        ``first_payload_is_command`` (a pooled ``invoke``, whose first
        ``Command`` already carries the script) is replayed only when *exc* is
        provably refused - a 4xx with an empty body from the framing layer,
        which never reached a pipeline (:func:`is_winrm_refusal`).

        ``carries_user_work`` marks an operation whose later exchanges send the
        caller's work while its first is structural (a oneshot ``execute_ps``
        opens a shell, then sends the ``Command``). With a readable counter the
        rule above proves the rejected request was that structural first
        exchange, so the replay is safe and the flag does not apply. With no
        readable counter nothing separates the two cases: ``True`` (the
        default - a guard assumes the worst) replays only a provable refusal,
        while ``False`` (the read-only identity probe) keeps the permissive
        self-heal with no side effect to protect.
        """
        after = self._link_round_trips()
        if before is not None and after is not None and after != before:
            return False
        if first_payload_is_command:
            return is_winrm_refusal(exc)
        if carries_user_work and (before is None or after is None):
            return is_winrm_refusal(exc)
        return True

    def _resync_then_retry(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float | None,
        deadline: float | None,
    ) -> tuple[bool, Any]:
        """Re-handshake the stale link and retry *fn* exactly once.

        The replay shares the caller's wall-clock budget rather than getting a
        fresh one: a ``timeout_s`` contract of "local wait" must not stretch to
        twice the budget just because the first attempt was rejected. When the
        budget is already spent the replay is skipped and reported as an
        expired budget, which maps to the hard-timeout contract.

        Returns ``(True, raw)`` on success and ``(False, exc)`` when the retry
        raised. Every retry failure is handed back for classification rather
        than raised, so each caller can keep its own contract (a budget expiry
        must still map to ``timed_out``); an already-mapped
        :class:`TransportError` propagates unchanged.
        """
        # Never raises; a session that exposes no pypsrp state is a no-op.
        resync_winrm_session(self._session)
        remaining = timeout_s
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, TimeoutError(
                    "winrm link rejection recovered the session but the call "
                    "budget was already spent"
                )
        retry = self._call_with_budget(fn, timeout_s=remaining)
        try:
            return True, retry()
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            return False, exc

    @staticmethod
    def _retry_failure_kind(exc: BaseException) -> str:
        """Verdict for a failed retry: ``"timeout"`` | ``"fatal"`` | ``"lost"``.

        Mirrors the first-attempt taxonomy: a budget expiry keeps the
        hard-timeout contract, a ``link_fatal`` failure keeps raising
        ``EXEC_FAILED`` (it may have run remotely), and anything else means the
        link did not heal after the re-handshake.
        """
        kind = classify_winrm_failure(exc)
        if kind == "budget_timeout":
            return "timeout"
        if kind == "link_fatal":
            return "fatal"
        return "lost"

    def _retry_failed_exec_result(self, exc: BaseException, *, cwd: str | None) -> ExecResult:
        """Map a failed retry of a ``link_retryable`` call to an ``ExecResult``."""
        verdict = self._retry_failure_kind(exc)
        if verdict == "timeout":
            return self._hard_timeout_exec_result(
                cwd=cwd,
                stderr=_safe_msg(exc) or "timeout",
            )
        if verdict == "fatal":
            self._mark_link_dead()
            raise TransportError(
                "EXEC_FAILED",
                _safe_msg(exc),
                details={"host": self.host},
            ) from exc
        return self._link_lost_exec_result(cwd=cwd, stderr=_safe_msg(exc))

    def _mark_link_dead(self) -> None:
        """Mark dead with an honest link reason and drop the poisoned session."""
        self.mark_dead("link lost")
        self._dispose_prior_session()
        self.meta = {
            **(self.meta or {}),
            "marked_dead": True,
            "link_lost": True,
            "reopen_hint": "endpoint close then open",
        }

    def _note_session_resynced(self) -> None:
        """Record that a call recovered by re-handshaking the link."""
        self.meta = {**(self.meta or {}), "session_resynced": True}

    def _link_lost_detail(self, stderr: str) -> str:
        """Honest link-lost stderr: local teardown only, reconnect on next call.

        Must not claim the remote pipeline was Stopped or that the request
        provably did not run - a failed retry is not proof of a remote no-op.
        """
        base = (stderr or "winrm link lost").strip() or "winrm link lost"
        detail = (
            f"{base}; winrm link lost after re-handshake, local session closed "
            "(endpoint reconnects on the next call)"
        )
        if len(detail) > 240:
            detail = detail[:237] + "..."
        return detail

    def _link_lost_exec_result(
        self,
        *,
        cwd: str | None,
        stderr: str,
    ) -> ExecResult:
        """Mark dead, dispose, and return an honest link-lost ``ExecResult``.

        The link was re-handshaked and still failed, so the local client/session
        is torn down and the endpoint reconnects on the next call.

        Machine-readable meta for Core/Agent - do not force English parsing of
        stderr::

            marked_dead=True
            link_lost=True
            reopen_hint="endpoint close then open"
        """
        self._mark_link_dead()
        return ExecResult(
            exit_code=-1,
            stdout="",
            stderr=self._link_lost_detail(stderr),
            cwd=cwd,
            timed_out=False,
        )

    def _link_lost_runspace_result(
        self,
        *,
        location: str | None,
        stderr: str,
    ) -> RunspaceResult:
        """Runspace-side counterpart of :meth:`_link_lost_exec_result`.

        Same death bookkeeping (mark_dead + dispose + machine-readable meta);
        the runspace itself is gone with the session, so no ``stop`` is issued.
        """
        self._mark_link_dead()
        return RunspaceResult(
            stdout="",
            stderr=self._link_lost_detail(stderr),
            exit_code=-1,
            location=location,
            had_errors=True,
            timed_out=False,
        )

    def _on_fs_link_failure(self, exc: BaseException) -> None:
        """Drop a poisoned session after an fs-path link failure.

        FS calls share the exec session's WSMan connection; a link failure seen
        there means exec would fail too. Same policy as the exec paths without
        an in-flight call to retry: mark dead and dispose, so ``is_connected()``
        is honest and the next op reconnects.
        """
        _log.debug("winrm fs link failure (host=%s): %s", self.host, _safe_msg(exc))
        self._mark_link_dead()

    def _hard_timeout_exec_result(
        self,
        *,
        cwd: str | None,
        stderr: str,
    ) -> ExecResult:
        """Mark dead, dispose session, return timed-out ``ExecResult``.

        Product lever against ``MaxShellsPerUser`` orphan shells after oneshot
        wall-clock timeout: best-effort ``session.close`` immediately (not only
        on the next :meth:`connect`). Honest stderr: remote pipeline is **not**
        claimed Stopped - only the local WinRM client/session was closed.

        Machine-readable meta for Core/Agent - do not force English
        parsing of stderr::

            marked_dead=True
            session_disposed=True
            reopen_hint="endpoint close then open"
        """
        self.mark_dead("hard timeout")
        self._dispose_prior_session()
        # Stable tokens for OpResult.fields (exec_ops copies on timed_out).
        self.meta = {
            **(self.meta or {}),
            "marked_dead": True,
            "session_disposed": True,
            "reopen_hint": "endpoint close then open",
        }
        base = (stderr or "timeout").strip() or "timeout"
        # Keep message short for Agent/CLI; no fake remote-cancel claim.
        detail = (
            f"{base}; winrm session closed "
            "(remote shell not guaranteed stopped)"
        )
        if len(detail) > 240:
            detail = detail[:237] + "..."
        return ExecResult(
            exit_code=-1,
            stdout="",
            stderr=detail,
            cwd=cwd,
            timed_out=True,
        )

    def _call_and_coerce(
        self,
        fn: Any,
        payload: str,
        *,
        env: dict[str, str] | None,
        timeout_s: float | None,
        cwd: str | None,
        coerce: Callable[..., ExecResult],
    ) -> ExecResult:
        """Run oneshot ``execute_ps`` / ``execute_cmd``; normalize to ``ExecResult``.

        Applies env via :meth:`_call_remote` and the shared wall-clock / hard-
        timeout dispose path from :meth:`_map_call_to_exec`.
        """
        return self._map_call_to_exec(
            lambda: self._call_remote(fn, payload, env=env),
            timeout_s=timeout_s,
            cwd=cwd,
            coerce=coerce,
        )

    def _call_remote(
        self,
        fn: Any,
        payload: str,
        *,
        env: dict[str, str] | None,
    ) -> Any:
        """Call oneshot ``execute_ps`` / ``execute_cmd`` with optional env.

        Env handling (Protocol contract): always pass ``environment=env`` when
        *env* is set. Implementers (``PypsrpClientAdapter``, mocks) accept that
        kwarg; no signature probing and no script-side inject on the production
        path.

        Timeout handling: oneshot APIs do not take a per-call timeout. The
        wall-clock deadline, the MaxShells dispose and the link policy are owned
        by the outer :meth:`_map_call_to_exec` / :meth:`_hard_timeout_exec_result`
        path, not this helper. ``asyncio.to_thread`` cannot cancel the blocking
        call, so on timeout the remote call may keep running and hold a
        server-side runspace (pypsrp opens a temporary RunspacePool per oneshot
        with no external stop handle); repeated timeouts can exhaust
        ``MaxShellsPerUser``. Operators should still ``endpoint close`` when
        timeouts recur rather than leave a degraded endpoint registered.
        """
        if env:
            return fn(payload, environment=env)
        return fn(payload)

    def _run_blocking_with_timeout(
        self,
        fn: Callable[[], Any],
        *,
        timeout_s: float,
    ) -> Any:
        """Run a blocking remote call on the shared bridge with a wall-clock timeout.

        Uses ``asyncio.to_thread`` on :class:`AsyncLoopBridge` so the call runs
        off the caller's thread. On timeout the bridge cancels the await and
        raises ``TimeoutError``; the executor thread keeps running until the
        remote side returns (e.g. after ``PowerShell.stop()``).

        Callers must act on ``TimeoutError``: oneshot/high-level exec paths
        (:meth:`_map_call_to_exec` / :meth:`_hard_timeout_exec_result`) call
        :meth:`mark_dead` **and** immediately dispose the client session
        (``MaxShellsPerUser``); runspace invoke paths call ``handle.stop()``
        for pipeline-only dispose while keeping the pool open.
        """
        import asyncio

        from mcp_remote_control.transport.async_bridge import get_shared_bridge

        async def _wrap() -> Any:
            return await asyncio.to_thread(fn)

        return get_shared_bridge().run(_wrap(), timeout_s=timeout_s)

