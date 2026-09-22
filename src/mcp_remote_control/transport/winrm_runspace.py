"""WinRM persistent runspace adapters and result coercion."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.winrm_exec import (
    _EXIT_MARKER,
    _append_ps_exit_probe,
    _decode_stream,
    _exit_code_from_ps,
    _format_ps_errors,
    _format_ps_output,
    _isolate_user_script,
    _split_exit_marker,
)

_log = logging.getLogger(__name__)


@dataclass
class RunspaceResult:
    """Result of a persistent PowerShell runspace invoke (ps tool)."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    location: str | None = None
    had_errors: bool = False
    # True when the invoke hit the caller's wall-clock timeout. The pipeline is
    # stopped/disposed but the runspace pool is left usable for later invokes.
    # Same meaning as ExecResult.timed_out on run_command / run_argv.
    timed_out: bool = False
    # False when the exit/location probes appended by the pool adapter produced
    # no marker, i.e. the user script ended its own script block (top-level
    # ``return`` / ``exit``) before they ran. ``exit_code`` then carries no
    # evidence about this invoke - it is the ``had_errors``-only fallback, not a
    # reported native code - and ``location`` is the previously known location
    # rather than this invoke's. Handles that do not append the probes (mock /
    # custom runspaces) leave it True: they report their own exit_code.
    exit_probe_ran: bool = True


# Sentinel prefixes for probes appended to PowerShell pipelines.
# Chosen to be collision-free with realistic PowerShell output.
_LOCATION_MARKER = "__MRC_PS_CWD_MARKER__"


class InvokeRunspaceAdapter:
    """Adapter for handles that already implement ``invoke`` / ``close``.

    Wraps mock and custom runspaces so :meth:`WinRMTransport.open_runspace`
    always returns a stable RunspaceHandle surface. ``runspace_invoke`` only
    calls this API - no pool heuristics on the hot path.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        """Underlying handle (tests / diagnostics)."""
        return self._inner

    @property
    def location(self) -> str | None:
        return getattr(self._inner, "location", None)

    def invoke(self, script: str) -> Any:
        return self._inner.invoke(script)

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def stop(self) -> None:
        """Best-effort interrupt when the inner handle exposes ``stop()``."""
        stop = getattr(self._inner, "stop", None)
        if callable(stop):
            stop()


class PypsrpPoolRunspaceAdapter:
    """Adapter: pypsrp ``RunspacePool`` -> RunspaceHandle (``invoke`` / ``close`` / ``stop``).

    ``invoke`` / ``prepare_invoke`` build a ``PowerShell`` pipeline with a folded
    location probe (sentinel-tagged ``Get-Location``) so one WSMan round trip
    returns output and cwd together. Concurrent invokes each own a distinct
    pipeline, and a timeout cancels through that invoke's own stop callback, so
    no timeout stops another in-flight pipeline; the pool stays open after a
    wall-clock timeout (pipeline stop+close only).

    Each pipeline is released exactly once - deregistered from the pool and
    disposed - so a long-lived pool does not accumulate finished pipelines; the
    completion path never waits on the remote exchange, so a stalled release
    cannot turn an already extracted result into the caller's timeout. See
    :func:`_release_pipeline` for the claiming rules and for the pipeline still
    awaiting its Command, which keeps its registration and cleans up itself.

    A release whose bounded wait for the transport serial lock elapsed with
    nothing sent is retained and retried at the next serial zone the
    **transport** enters, and again as that zone ends (see
    :meth:`_retry_pending_releases` and :meth:`_bind_serial_zone_hooks`).

    ``op_lock`` is the transport's serial lock (``BaseTransport.op_lock``), held
    across every exchange on the shared WSMan session; ``serial_zone_hooks`` is
    the transport's hook registry, which the drain is registered with. A session
    that ends without reaching :meth:`close` retires the handle locally instead
    (see :meth:`detach`).
    """

    def __init__(
        self,
        pool: Any,
        *,
        default_location: str | None = None,
        op_lock: Any = None,
        serial_zone_hooks: Any = None,
    ) -> None:
        self._pool = pool
        self._default_location = default_location
        # Read when a release runs, not at construction, so the transport can
        # supply its own lock with the handle it hands out (see
        # _adapt_runspace_handle). Without one the release exchanges as before.
        self.op_lock = op_lock
        self.location: str | None = default_location or getattr(pool, "location", None)
        # Pipelines whose terminal release never started because the transport
        # serial lock was not free within the release deadline: deregistered
        # locally and terminal remotely, still owing their one server-side
        # release. Guarded, because the completion path retains from a release
        # thread while the next serial zone drains the list.
        self._pending_releases: list[Any] = []
        self._pending_lock = threading.Lock()
        # True once this handle is retired - the session that owned it is gone,
        # or its pool has been closed. A retired handle keeps its drain
        # registration only for the releases it still owes.
        self._retired = False
        # The transport's serial-zone registry; bound last so
        # _bind_serial_zone_hooks sees a fully built handle.
        self._serial_zone_hooks: Any = None
        self._bind_serial_zone_hooks(serial_zone_hooks)

    @property
    def inner(self) -> Any:
        """Underlying RunspacePool (tests / diagnostics)."""
        return self._pool

    @property
    def pending_release_count(self) -> int:
        """Releases retained for the next serial zone (tests / diagnostics).

        Non-zero means this handle still owes the remote runspace a release
        that no attempt has been able to send yet.
        """
        with self._pending_lock:
            return len(self._pending_releases)

    def _bind_serial_zone_hooks(self, hooks: Any) -> None:
        """Register this handle's drain with the transport's serial-zone hooks.

        Called at construction and when the transport re-adapts a handle it
        already owns (it supplies the registry with the lock, see
        :func:`_adapt_runspace_handle`). The registration deliberately outlives
        this handle's own invokes: a retained release belongs to the transport
        the release has to travel on, so the next zone entered by a different
        handle, by ``exec`` or by an fs call - and the end of that zone, when
        the lock it held becomes free - is where it is retried, and the
        handle can never rely on being invoked again. Re-binding the registry
        that is already bound is a no-op, so re-adapting never registers the
        drain twice.

        The registry stays known to this handle even while the drain is not
        registered on it (see :meth:`detach`): a release that arrives
        afterwards is owed just the same, and putting the drain back is how a
        later serial zone still finds it.

        Every mutation of that registry happens under the retention lock, which
        is the lock :meth:`close` holds while it leaves the registry for good,
        so a registration can neither be made on nor left behind on a registry
        this handle has already left. A handle whose pool is closed is never
        bound again: its registry was forgotten on purpose, and a release
        arriving afterwards has nothing left to be released on.
        """
        with self._pending_lock:
            if hooks is self._serial_zone_hooks:
                return
            if hooks is not None and self._retired and self._serial_zone_hooks is None:
                return
            self._unbind_serial_zone_hooks()
            self._serial_zone_hooks = hooks
            if hooks is not None and (not self._retired or self._pending_releases):
                hooks.add(self._retry_pending_releases)

    def _unbind_serial_zone_hooks(self) -> None:
        """Take the drain registration back; a second call is a no-op.

        The registry itself is still known to this handle afterwards, but it
        no longer holds anything: a retired handle that owes nothing must stop
        being reachable from the transport, or its pool would outlive the
        session that owns it. Only :meth:`close` forgets the registry, because
        a closed pool has nothing left to drain. Callers hold the retention
        lock (``_pending_lock``); every registry mutation this handle makes is
        serialized on it, which is what keeps a registration from landing on a
        registry the handle has already left.
        """
        hooks = self._serial_zone_hooks
        if hooks is None:
            return
        hooks.discard(self._retry_pending_releases)

    def _retain_pending_release(self, ps: Any) -> None:
        """Keep one release that never started, for the next serial zone.

        Called by :func:`_close_pipeline` when its bounded wait for the
        transport lock elapsed with nothing sent: the pipeline owes its
        server-side release and its local registration is already gone, so the
        responsibility is held here instead of ending with the attempt. The
        same pipeline is never held twice - the one release it owes is all a
        retry can send - and a close that was sent (or left on the wire) is
        never retained at all.

        A release retained after :meth:`detach` took the registration back is
        owed like any other: the drain goes back on the transport registry this
        handle already belongs to, so the next serial zone still finds it, and
        the registration is made under the retention lock, the same one
        :meth:`close` holds while it leaves that registry for good: either the
        close sees the release and takes the drain back with it, or the close
        has already forgotten the registry and nothing is registered at all.
        :meth:`close` is the one path that forgets that registry - the pool is
        going away, and there is nothing left for a retry to release.
        """
        with self._pending_lock:
            if any(retained is ps for retained in self._pending_releases):
                return
            self._pending_releases.append(ps)
            if self._retired and self._serial_zone_hooks is not None:
                self._serial_zone_hooks.add(self._retry_pending_releases)

    def _retry_pending_releases(self) -> None:
        """Retry releases whose first attempt never reached the session.

        A retained pipeline is terminal and already deregistered locally: the
        one thing it still owes is the server-side release, which belongs to
        the transport it has to travel on. It is retried at the next serial
        zone the transport enters and again as that zone ends (this method is
        registered as a serial-zone hook for that, see
        :meth:`_bind_serial_zone_hooks`), and from this handle's own
        prepare/run paths, which are zones of their own. A zone's end matters
        as much as its entry: the retry started at entry is bounded, so a zone
        whose own exchange outlasted that bound retries it again as the lock it
        held is freed.

        Each retry is the same bounded release as the attempt that failed - it
        takes the transport lock itself with the release deadline, so it
        exchanges as the zone is released, never alongside the operation
        holding it - and it runs on its own daemon thread, so no caller waits
        for it inside the zone it entered. An attempt that still cannot take
        the lock stays retained for the next zone; one that hands the pipeline
        to the release path is dropped here, since a retry can only send the
        release the first attempt left owed.

        A retired handle stops being asked once it owes nothing: the last drain
        drops the registration itself (see
        :meth:`_unbind_when_retired_and_idle`), so the transport never keeps
        holding the adapter - and the pool behind it - of a session that is
        gone.
        """
        with self._pending_lock:
            pending = self._pending_releases
            self._pending_releases = []
        for ps in pending:
            if not _release_pipeline_detached(
                ps,
                op_lock=self.op_lock,
                retain=self._retain_pending_release,
            ):
                # No release thread was started, so nothing was sent and the
                # responsibility is still this handle's: keep it for the next
                # zone instead of dropping it with the attempt.
                self._retain_pending_release(ps)
        self._unbind_when_retired_and_idle()

    def _unbind_when_retired_and_idle(self) -> None:
        """Stop being asked once a retired handle owes nothing.

        A retired handle is registered with the transport only for the
        releases it still owes (see :meth:`detach`); once a drain has handed
        the last of them over there is nothing left to ask it for, and holding
        it would keep the adapter, its pool and the remote runspace's
        client-side state alive for a session that is already gone. A release
        retained again in the meantime leaves the registration in place.
        """
        with self._pending_lock:
            if not self._retired or self._pending_releases:
                return
            self._unbind_serial_zone_hooks()

    def prepare_invoke(
        self, script: str
    ) -> tuple[Callable[[], RunspaceResult], Callable[[], None]]:
        """Build a per-invoke ``(run, stop)`` pair for wall-clock timeout.

        ``run()`` executes this invoke's pipeline. ``stop()`` disposes *only*
        that pipeline (bounded stop+close), never a concurrent invoke's. Both
        can meet on the same pipeline; each pipeline is released by whichever
        claims it first and by that one alone. The one exception is a pipeline
        whose ``run()`` is still awaiting its Command: that run may still be
        started remotely, so ``stop()`` leaves it - and its release - alone
        rather than dropping the registration the late Command answer is routed
        through.

        The transport reaches this call inside the serial zone it holds for the
        invoke being prepared, so retained releases are retried here first,
        before this invoke's pipeline is built.
        """
        try:
            from pypsrp.powershell import PowerShell
        except ImportError as exc:  # pragma: no cover - hard dep in practice
            raise TransportError(
                "UNSUPPORTED",
                "pypsrp is required for PowerShell runspaces",
            ) from exc

        # This call is the transport's serial zone entered for a runspace
        # invoke: release anything a previous attempt left owed before this
        # invoke's own pipeline exists (see _retry_pending_releases).
        self._retry_pending_releases()

        # Exit probe before location probe: the exit probe reads the code left
        # by the user script's own native commands, the location probe follows
        # it, and location stays last for existing split semantics. Both are
        # pure cmdlets, so neither clobbers $LASTEXITCODE. Markers are stripped
        # from stdout in ``run``.
        #
        # The reset statement that precedes the exit probe must not become the
        # user script's first statement (a ``param(...)`` block is only valid
        # there), so the user's text is isolated as its own dot-sourced block;
        # see _isolate_user_script. A top-level ``exit`` in that script still
        # ends the block before the probes, so a missing marker is reported as
        # "no evidence" rather than as exit 0.
        probe_script = (
            f"{_append_ps_exit_probe(_isolate_user_script(script))}\n"
            f"Write-Output ('{_LOCATION_MARKER}' + (Get-Location).Path)"
        )
        ps = PowerShell(self._pool)
        ps.add_script(probe_script)

        # One release per pipeline: the wall-clock timeout stop()s a pipeline
        # that run() may still be finishing on the abandoned bridge thread, so
        # both paths can meet on the same pipeline. Whichever claims the
        # release first is the only one that signals the server for it.
        release_lock = threading.Lock()
        release_claimed = False
        # Set once run() has left this pipeline's exchange for good. Until then
        # the pipeline is still on the wire, and a pypsrp pipeline reads
        # NOT_STARTED while it waits for its Command answer (see
        # _awaits_command).
        run_finished = threading.Event()

        def claim_release() -> bool:
            nonlocal release_claimed
            with release_lock:
                if release_claimed:
                    return False
                release_claimed = True
                return True

        def run() -> RunspaceResult:
            try:
                output = ps.invoke()
                location, after_loc = _split_location_output(output, _LOCATION_MARKER)
                captured_rc, user_output = _split_exit_marker(after_loc, _EXIT_MARKER)
                stdout = _format_ps_output(user_output)
                stderr = _format_ps_errors(ps)
                had_errors = bool(getattr(ps, "had_errors", False)) or bool(stderr)
                exit_code = _exit_code_from_ps(
                    captured=captured_rc, had_errors=had_errors
                )
                if location:
                    self.location = location
                return RunspaceResult(
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    location=location or self.location or self._default_location,
                    had_errors=had_errors,
                    # No usable marker means the probe did not run (or its
                    # payload was not an integer): that is not the same as a
                    # probe that reported 0, and the caller must be able to
                    # tell.
                    exit_probe_ran=captured_rc is not None,
                )
            finally:
                # The result is extracted (or the exchange failed): release this
                # pipeline so completed pipelines do not pile up in the pool's
                # registry. The deregistration is immediate - a pipeline whose
                # result has been read must never be readable as live in the
                # registry - while the bounded remote release runs on a daemon
                # thread: a peer that stalls that exchange must not stretch the
                # caller's wall clock and turn an extracted result into its
                # timeout. The release is detached from this invoke, so it takes
                # the transport serial lock for its own exchange. The pool is
                # left open for later invokes.
                run_finished.set()
                if claim_release():
                    _drop_pipeline_registration(ps)
                    _release_pipeline_detached(
                        ps,
                        op_lock=self.op_lock,
                        retain=self._retain_pending_release,
                    )
                # This invoke is finishing, so the serial zone it was started
                # under is about to be released: the moment a retained release
                # can take the transport lock without exchanging alongside an
                # operation. Retry them here too, so an invoke that outlasted
                # the release deadline does not defer them to a later invoke.
                self._retry_pending_releases()

        def stop() -> None:
            # Bounded stop + close of this pipeline only (never a concurrent
            # invoke's). The release is claimed before it runs, so it stays the
            # pipeline's only one: a run() that already released this pipeline -
            # or is releasing it on its own thread - never sends a second
            # signal for it, and the single attempt remains best effort.
            if not run_finished.is_set() and _awaits_command(ps):
                # This run is still waiting for its Command answer, and pypsrp
                # reports NOT_STARTED from before that Command is sent until its
                # response arrives: the state cannot prove the remote refused
                # the command, and no CommandId has been learned to address a
                # release with. The registration is the route the late response
                # needs - the pool reads it to learn this pipeline's CommandId
                # and to hand the pipeline its own messages - so the stop leaves
                # the registration and the release to the run that is still on
                # the wire, whose completion path performs that one terminal
                # cleanup when it finishes.
                return
            if claim_release():
                _release_pipeline(
                    ps,
                    interrupt=True,
                    op_lock=self.op_lock,
                    retain=self._retain_pending_release,
                )

        return run, stop

    def invoke(self, script: str) -> RunspaceResult:
        """Run *script* on a fresh pipeline (no shared active-slot)."""
        run, _stop = self.prepare_invoke(script)
        return run()

    def stop(self) -> None:
        """Handle-level stop is a no-op for concurrent safety.

        Per-invoke cancellation must use the ``stop`` callback returned by
        :meth:`prepare_invoke` (wired by :meth:`WinRMTransport._invoke_handle`).
        A shared single-slot stop would race concurrent invokes and could kill
        the wrong pipeline.
        """
        return

    def detach(self) -> None:
        """Retire this handle locally: no exchange, no wait for the lock.

        The session owning this handle can end without ever reaching
        :meth:`close` - a lock-wait miss on ps close, or an ``abandon`` of a
        session whose transport is already gone - and the transport's registry
        holds this handle through the drain it registered. Retirement
        therefore has to take that registration back here, or a deregistered
        session keeps its adapter, its pool and the remote runspace's
        client-side state alive for the life of the transport, and every later
        serial zone walks an empty drain. The caller is on its own wall clock,
        so nothing is exchanged and no lock is waited for.

        A handle that still owes a release keeps its registration - a later
        serial zone is the only thing that can land it once the session is gone
        - and the drain drops the registration itself as soon as it is retired
        and owes nothing (see :meth:`_unbind_when_retired_and_idle`).
        Idempotent, and never a substitute for :meth:`close`, which still
        releases the pool.
        """
        with self._pending_lock:
            self._retired = True
        self._unbind_when_retired_and_idle()

    def close(self) -> None:
        # The runspace is going away, so nothing is left for a later zone to
        # drain: stop being asked before the pool is closed, so a release that
        # cannot land during teardown is not retried against a runspace that no
        # longer exists. The registry is forgotten outright, unlike the
        # retirement of detach: a release that arrives during the teardown has
        # no pool left to be released on. The three fields move together under
        # the retention lock, so a release arriving now cannot put the drain
        # back on a registry this handle has already left.
        with self._pending_lock:
            self._retired = True
            self._unbind_serial_zone_hooks()
            self._serial_zone_hooks = None
        closer = getattr(self._pool, "close", None)
        if callable(closer):
            closer()


def _adapt_runspace_handle(
    handle: Any,
    *,
    default_location: str | None = None,
    op_lock: Any = None,
    serial_zone_hooks: Any = None,
) -> Any:
    """Normalize any ``open_runspace`` return value to a RunspaceHandle adapter.

    Classification is one-shot at open time:
    - already an adapter -> returned as-is, taking *op_lock* and
      *serial_zone_hooks* when they are given
    - has callable ``invoke`` -> :class:`InvokeRunspaceAdapter` (mocks / custom)
    - otherwise -> :class:`PypsrpPoolRunspaceAdapter` (pypsrp ``RunspacePool``)

    *op_lock* is the transport's serial lock, carried to the pool adapter (a
    handle that invokes synchronously never needs it: every exchange it makes
    is already inside its caller's serial zone). It is supplied here because a
    pool adapter's release runs after its invoke returned - on no thread that
    holds a serial zone - so the lock has to arrive with the handle.

    *serial_zone_hooks* is the transport's serial-zone registry
    (:class:`~mcp_remote_control.transport.base.SerialZoneHooks`), carried to
    the pool adapter for the same reason: the release a pool adapter retains
    is retried when the transport enters a serial zone, and the zones that
    matter (a different handle's invoke, ``exec``, an fs call) are entered on
    threads that never touch this handle.
    """
    if isinstance(handle, PypsrpPoolRunspaceAdapter):
        if op_lock is not None:
            handle.op_lock = op_lock
        if serial_zone_hooks is not None:
            handle._bind_serial_zone_hooks(serial_zone_hooks)
        return handle
    if isinstance(handle, InvokeRunspaceAdapter):
        return handle
    invoker = getattr(handle, "invoke", None)
    if callable(invoker):
        return InvokeRunspaceAdapter(handle)
    return PypsrpPoolRunspaceAdapter(
        handle,
        default_location=default_location,
        op_lock=op_lock,
        serial_zone_hooks=serial_zone_hooks,
    )


def _split_location_output(
    output: Any,
    marker: str,
) -> tuple[str | None, list[Any]]:
    """Extract the sentinel-tagged location from a pipeline's output list.

    The probe ``Write-Output ('<marker>' + (Get-Location).Path)`` emits one
    string starting with *marker*. All marker-prefixed elements are stripped
    and the location is taken from the last one (the probe is the final
    statement), so a coincidental user line that starts with the marker cannot
    leave a stale location. Returns ``(location, remaining_output)``.
    """
    if output is None:
        return None, []
    if not isinstance(output, (list, tuple)):
        text = str(output)
        if text.startswith(marker):
            loc = text[len(marker):].strip() or None
            return loc, []
        return None, [output]

    remaining = list(output)
    indices = [i for i, v in enumerate(remaining) if str(v).startswith(marker)]
    if not indices:
        return None, remaining
    location = str(remaining[indices[-1]])[len(marker):].strip() or None
    for i in sorted(indices, reverse=True):
        del remaining[i]
    return location, remaining

# Bound for best-effort pipeline stop/close after a runspace timeout so a slow
# WSMan cannot make runspace_invoke hang past the caller's budget. Daemon
# threads ensure a stuck stop() cannot block interpreter shutdown.
_STOP_DEADLINE_S = 2.0

# What a release's caller waits for one close attempt, as a multiple of
# :data:`_STOP_DEADLINE_S`. The wait for the transport lock and the wait for
# the close itself are both drawn from that deadline, so a lock that was never
# free concludes just after it - and that conclusion is what the caller has to
# observe: "nothing was sent" leaves the one release the pipeline owes in the
# caller's hands, while "the close is on the wire" must never be attempted
# again. The extra half of the deadline is only for observing that conclusion;
# nothing is exchanged during it, and the only waiters are the release's own
# thread and the stop path (bounded by its own wait).
_RELEASE_WAIT_SHARE = 1.5


def _call_with_deadline(fn: Any, *, deadline_s: float) -> bool:
    """Run ``fn()`` on a daemon thread, waiting at most ``deadline_s`` seconds.

    True when ``fn`` finished within the deadline (raising counts as finished:
    what a failure means is the caller's call), False when the wait was
    abandoned with ``fn`` still running. The thread is a daemon, so an
    unfinished call cannot block interpreter shutdown, and it keeps running so
    a late answer is still observed. Errors are swallowed either way.
    """
    if fn is None or not callable(fn):
        return True
    done = threading.Event()

    def _run() -> None:
        try:
            fn()
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True, name="mrc-ps-stop")
    t.start()
    return done.wait(timeout=deadline_s)


@contextmanager
def _hold_serial(op_lock: Any, *, deadline_s: float) -> Iterator[bool]:
    """Hold the transport's serial lock for one release exchange.

    Yields True when the lock is held (or when there is no lock to take) and
    False without entering when the wait spent *deadline_s*: a release must not
    exchange while another operation owns the shared session, and it must not
    queue behind one past its own deadline either - the lock is held for a
    whole operation, so an unbounded wait would outlive the release. Re-entrant
    on a thread that already owns it.

    Only the wait is drawn from the deadline: once held, the lock stays held
    while the guarded exchange is on the wire, because releasing it earlier
    would let the next operation exchange alongside the one still in flight.
    """
    acquire = getattr(op_lock, "acquire", None)
    release = getattr(op_lock, "release", None)
    if not callable(acquire) or not callable(release):
        yield True
        return
    try:
        held = bool(acquire(timeout=deadline_s))
    except TypeError:  # a lock whose acquire takes no timeout
        held = bool(acquire())
    if not held:
        yield False
        return
    try:
        yield True
    finally:
        release()


def _warn_release_not_landed(ps: Any, *, why: str) -> None:
    """Report a pipeline whose release did not land, naming it and the reason.

    A release that does not land leaves the server never asked to release the
    pipeline (a close that refused, a lock the attempt could not take) or asked
    without an answer, so the pipeline is not proven gone: it keeps its script,
    streams and output, and its remote slot, for the life of the pool. That is
    never swallowed - the same rule the close path follows for a delete that
    does not land - and the wording claims no more than is known: a close
    abandoned with its exchange on the wire may still land when the peer
    answers.
    """
    _log.warning(
        "winrm runspace release did not land (%s): pipeline %s may still be "
        "allocated on the remote runspace",
        why,
        getattr(ps, "id", None),
    )


def _close_pipeline(
    ps: Any,
    *,
    op_lock: Any = None,
    retain: Callable[[Any], None] | None = None,
) -> bool:
    """Bounded, serialized ``ps.close()``; True only when the release landed.

    ``close`` is the release itself - it sends the pipeline's TERMINATE and
    deregisters it from the pool - and it is WSMan traffic on the session every
    operation shares, so it runs under *op_lock*. The lock wait and the call
    wait are both drawn from :data:`_STOP_DEADLINE_S`. The call is not
    interruptible, so an unanswered close keeps running on a daemon thread and
    keeps *op_lock* until the session's own HTTP read timeout ends the exchange:
    holding the lock for the whole exchange is what keeps two exchanges off the
    one session. That abandoned close is therefore what a caller's later serial
    op may be waiting on, and it is never abandoned silently - the caller's wait
    covers both and then some (:data:`_RELEASE_WAIT_SHARE`).

    False means the release did not land, and the reason is reported: the close
    raised (pypsrp refuses to close a non-terminal pipeline, which is what a
    stop still on the wire leaves behind), the lock was not free within the
    deadline, or the call did not answer in time. A lock that was not free is
    the one outcome a caller can act on - nothing was sent and the pipeline is
    terminal, so this one release is provably still owed - and *retain* is
    handed the pipeline for it, before the report is written so a caller
    reacting to the report already sees it retained. Every other outcome either
    sent the close or left it on the wire, and is never retried.
    """
    closer = getattr(ps, "close", None)
    if not callable(closer):
        return True
    outcome: list[bool] = []
    reasons: list[str] = []

    def _close_held() -> None:
        with _hold_serial(op_lock, deadline_s=_STOP_DEADLINE_S) as held:
            if not held:
                outcome.append(False)
                reasons.append(
                    "the transport lock was not free within the release "
                    "deadline; the release is kept and retried at the next "
                    "serial zone"
                )
                if retain is not None:
                    retain(ps)
                return
            try:
                closer()
            except Exception:  # noqa: BLE001 - a close that raised did not land
                outcome.append(False)
                reasons.append("the close refused to release the pipeline")
                return
            outcome.append(True)

    finished = _call_with_deadline(
        _close_held,
        deadline_s=_STOP_DEADLINE_S * _RELEASE_WAIT_SHARE,
    )
    if not finished:
        # The wait is over, the exchange is not: it may still land later.
        _warn_release_not_landed(
            ps, why="the close was still on the wire at the release deadline"
        )
        return False
    landed = bool(outcome) and bool(outcome[0])
    if not landed:
        _warn_release_not_landed(
            ps,
            why=reasons[0] if reasons else "the close did not land",
        )
    return landed


def _safe_stop_pipeline(
    ps: Any,
    *,
    op_lock: Any = None,
    interrupt: bool = False,
    retain: Callable[[Any], None] | None = None,
) -> None:
    """Best-effort, bounded stop+close of a pypsrp PowerShell pipeline (not the pool).

    On runspace timeout, dispose the pipeline so the pool remains usable.
    ``PowerShell.stop()`` signals the remote host to abort; ``close()`` is the
    release (TERMINATE + deregistration). Both are WSMan exchanges on the
    session every operation shares, so both run in one serial zone capped by
    :data:`_STOP_DEADLINE_S`. The release follows the stop on the same thread,
    once the interrupt has answered early or late, so the server sees one
    TERMINATE per pipeline.

    *interrupt* marks the cancellation of an invoke whose caller is still inside
    its own serial zone while it waits for this stop (see
    ``WinRMTransport._invoke_handle``): the first attempt exchanges under that
    zone, because waiting for a lock the waiting caller holds would push the
    abort signal past the deadline that asked for it, while a re-attempt that
    follows a stalled stop answers after that zone is gone and takes *op_lock*
    itself. Without an interrupt the release has no caller zone (it runs on the
    detached completion thread), so the stop takes *op_lock* exactly as the
    release does; a lock it cannot take within the deadline drops the attempt
    and reports it rather than exchanging alongside the operation that owns the
    session.

    A stop still on the wire at the deadline leaves the pipeline in pypsrp's
    STOPPING state, where ``close()`` refuses to release it: the pipeline would
    stay registered and never receive its TERMINATE, silently. That state is
    reported with the pipeline id, and the release is re-attempted on the stop's
    own thread once the interrupt answers. A stop the lock keeps off the session
    is dropped and reported with nothing retained: unlike a terminal release,
    the cancellation was never sent, so the pipeline keeps its registration and
    its claim. A release that follows a stop that did answer is terminal and
    passes *retain* on (see :func:`_close_pipeline`).
    """
    if ps is None:
        return
    stop = getattr(ps, "stop", None)
    serial = None if interrupt else op_lock
    # Set once this thread's wait for the stop is over: from then on the
    # caller's serial zone is gone (or going), so the release takes the lock.
    wait_ended = threading.Event()

    def _stop_then_release() -> None:
        if callable(stop):
            with _hold_serial(serial, deadline_s=_STOP_DEADLINE_S) as held:
                if not held:
                    # Exchanging anyway would interleave this signal with the
                    # operation that owns the session right now. The attempt is
                    # dropped and the registration it leaves behind reported:
                    # unlike a terminal release, this cancellation was never
                    # sent and stays the pipeline's to complete.
                    _warn_release_not_landed(
                        ps,
                        why=(
                            "the transport lock was not free within the release "
                            "deadline"
                        ),
                    )
                    return
                try:
                    stop()
                except Exception:  # noqa: BLE001 - a raising stop answered it too
                    pass
        # The interrupt answered, early or late. Once it has, the pipeline
        # is terminal and close() can release it - the only way a release
        # survives a stop that outlasted the deadline, where the close
        # could not run at all. Outside the zone above: the close takes the
        # lock itself when the stop outlasted the deadline, where the caller's
        # zone is gone by then.
        _close_pipeline(
            ps,
            op_lock=op_lock if wait_ended.is_set() else serial,
            retain=retain,
        )

    if _call_with_deadline(_stop_then_release, deadline_s=_STOP_DEADLINE_S):
        return
    wait_ended.set()
    _log.warning(
        "winrm runspace interrupt for pipeline %s did not answer within %ss: "
        "its release has not landed and is re-attempted when it does",
        getattr(ps, "id", None),
        _STOP_DEADLINE_S,
    )


def _drop_pipeline_registration(ps: Any) -> None:
    """Drop *ps* from its pool's pipeline registry (local bookkeeping only)."""
    pipelines = getattr(getattr(ps, "runspace_pool", None), "pipelines", None)
    if isinstance(pipelines, dict):
        pipelines.pop(getattr(ps, "id", None), None)


def _awaits_command(ps: Any) -> bool:
    """True when *ps* is a pypsrp pipeline still waiting for its Command answer.

    pypsrp sets ``NOT_STARTED`` when the pipeline is built and keeps it until
    the ``Command`` round-trip returns, so the state covers both "nothing sent
    yet" and "sent, unanswered": it holds no evidence that the remote refused
    the command, and no CommandId exists to address a release with. A handle
    with no pypsrp state to read (mock / custom) is never in this one.
    """
    try:
        from pypsrp.complex_objects import PSInvocationState
    except ImportError:  # pragma: no cover - hard dep in practice
        return False
    return getattr(ps, "state", None) == PSInvocationState.NOT_STARTED


def _release_pipeline(
    ps: Any,
    *,
    interrupt: bool = False,
    op_lock: Any = None,
    retain: Callable[[Any], None] | None = None,
) -> None:
    """Bounded release of one pypsrp pipeline; never the pool, never raises.

    pypsrp deregisters a pipeline from ``runspace_pool.pipelines`` only when
    ``close()`` runs on one in a terminal state, and that call is also what
    sends the server its release (TERMINATE); without it a finished invoke keeps
    its ``PowerShell`` object (script text, streams, output) in the registry for
    the life of the pool. The pool itself is left open and usable.

    *interrupt* is set by the wall-clock timeout stop, where the pipeline may
    still be RUNNING remotely, and by a completion path that finds it RUNNING
    (its invoke failed or was abandoned mid-flight): such a pipeline is stopped
    (PS_CTRL_C) before it is closed.

    A pipeline registered but never started - pypsrp registers it before its
    first round-trip, and neither ``stop()`` nor ``close()`` releases that
    state, so a Command that never answered would leak the entry - is released
    by dropping the registration: no command id was ever learned, so there is
    nothing to signal remotely. That drop belongs to the completion path, which
    runs once the invoke's own thread has left the pipeline; while that thread
    still awaits the Command answer the run may still start remotely, so it
    keeps the registration and the release (see
    :meth:`PypsrpPoolRunspaceAdapter.prepare_invoke`).

    A handle with no pypsrp state to reason about (mock / custom runspace) is
    disposed with ``close()``, and stopped as well only when the caller asked
    for an interrupt. Every exchange is capped by :data:`_STOP_DEADLINE_S` and
    runs under *op_lock* unless the caller's own serial zone still covers it. A
    release that does not land is reported, never swallowed. *retain* is passed
    to every close this release makes, so a terminal pipeline whose bounded wait
    for the transport lock elapsed with nothing sent is handed to it rather than
    losing the one release it owes (see :func:`_close_pipeline`).
    """
    if ps is None:
        return
    try:
        from pypsrp.complex_objects import PSInvocationState

        state = getattr(ps, "state", None)
        if not isinstance(state, int):
            # No pypsrp state to read (mock / custom handle).
            if interrupt:
                _safe_stop_pipeline(
                    ps, op_lock=op_lock, interrupt=True, retain=retain
                )
            else:
                # The close reports its own failure, with the reason.
                _close_pipeline(ps, op_lock=op_lock, retain=retain)
            return
        if state == PSInvocationState.NOT_STARTED:
            _drop_pipeline_registration(ps)
            return
        if state in (
            PSInvocationState.STOPPED,
            PSInvocationState.COMPLETED,
            PSInvocationState.FAILED,
        ):
            # Terminal: the close is the release, and no interrupt is needed.
            # An interrupting caller is still inside its own serial zone (the
            # invoke it belongs to holds the lock across this stop), so it must
            # not queue for a lock its own caller is holding.
            _close_pipeline(
                ps,
                op_lock=None if interrupt else op_lock,
                retain=retain,
            )
            return
        _safe_stop_pipeline(ps, op_lock=op_lock, interrupt=interrupt, retain=retain)
    except Exception:  # noqa: BLE001 - best-effort release
        pass


def _release_pipeline_detached(
    ps: Any,
    *,
    op_lock: Any = None,
    retain: Callable[[Any], None] | None = None,
) -> bool:
    """Run :func:`_release_pipeline` on a daemon thread; returns immediately.

    The completion path drops the pipeline's registration synchronously and
    hands the bounded remote release to this thread, so the invoke returns its
    already extracted result without waiting for a WSMan exchange a slow peer
    can stall - the same bound the timeout path applies to its own stop, and
    the same reason it abandons the wait at the deadline. The thread is a
    daemon, so a stuck release cannot pin interpreter shutdown. *op_lock* is
    the transport serial lock the release takes for its exchange: detached from
    the invoke it belongs to, it has no serial zone of its own to run under.
    The lock is held while the exchange is on the wire, so a peer that stalls
    the release delays later serial ops until the session's own read timeout
    ends it - the price of keeping the two exchanges from overlapping, which is
    why the caller is never made to wait for any of it.

    True means the release thread was started; False means it could not be,
    so nothing was sent and the release is still owed to the caller - a retry
    keeps it, and a completion path that has no place to keep it drops it as
    before: the pipeline is already deregistered, and the remote signal was
    always best effort. *retain* is passed on to the release, which hands the
    pipeline back to it when the release never starts (see
    :func:`_close_pipeline`).
    """
    if ps is None:
        return True
    try:
        threading.Thread(
            target=_release_pipeline,
            args=(ps,),
            kwargs={"op_lock": op_lock, "retain": retain},
            daemon=True,
            name="mrc-ps-release",
        ).start()
    except Exception:  # noqa: BLE001 - the release is best effort, the result is not
        return False
    return True


def _coerce_runspace_result(
    raw: Any,
    *,
    default_location: str | None,
) -> RunspaceResult:
    """Normalize invoke return shapes into ``RunspaceResult``.

    ``exit_probe_ran`` is carried over when the source result has it and is
    assumed True otherwise: only the pool adapter appends the probes, so any
    other shape (mock runspace, tuple, dict, duck-typed result) reports its own
    exit code rather than "probe did not run".
    """
    if isinstance(raw, RunspaceResult):
        if raw.location is None and default_location is not None:
            return RunspaceResult(
                stdout=raw.stdout,
                stderr=raw.stderr,
                exit_code=raw.exit_code,
                location=default_location,
                had_errors=raw.had_errors,
                timed_out=raw.timed_out,
                exit_probe_ran=raw.exit_probe_ran,
            )
        return raw

    if isinstance(raw, tuple):
        # (stdout, location) or (stdout, stderr, exit, location)
        if len(raw) == 2:
            return RunspaceResult(
                stdout=_decode_stream(raw[0]),
                location=_decode_stream(raw[1]) if raw[1] is not None else default_location,
            )
        if len(raw) >= 3:
            stdout = _decode_stream(raw[0])
            stderr = _decode_stream(raw[1]) if raw[1] is not None else ""
            exit_code = int(raw[2]) if isinstance(raw[2], (int, float)) else 0
            loc = (
                _decode_stream(raw[3])
                if len(raw) > 3 and raw[3] is not None
                else default_location
            )
            return RunspaceResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                location=loc,
                had_errors=exit_code != 0 or bool(stderr),
            )

    if isinstance(raw, dict):
        exit_code = int(raw.get("exit_code", 0) or 0)
        stderr = _decode_stream(raw.get("stderr", "") or "")
        return RunspaceResult(
            stdout=_decode_stream(raw.get("stdout", "") or ""),
            stderr=stderr,
            exit_code=exit_code,
            location=raw.get("location") or default_location,
            had_errors=bool(raw.get("had_errors", False)) or exit_code != 0,
            timed_out=bool(raw.get("timed_out", False)),
        )

    if isinstance(raw, str):
        return RunspaceResult(stdout=raw, location=default_location)

    if raw is None:
        return RunspaceResult(stdout="", location=default_location)

    # Duck-type objects with stdout / exit_code attributes.
    if hasattr(raw, "stdout") or hasattr(raw, "exit_code"):
        exit_code = int(getattr(raw, "exit_code", 0) or 0)
        stderr = _decode_stream(getattr(raw, "stderr", "") or "")
        return RunspaceResult(
            stdout=_decode_stream(getattr(raw, "stdout", "") or ""),
            stderr=stderr,
            exit_code=exit_code,
            location=getattr(raw, "location", None)
            or getattr(raw, "cwd", None)
            or default_location,
            had_errors=bool(getattr(raw, "had_errors", False)) or exit_code != 0,
            timed_out=bool(getattr(raw, "timed_out", False)),
        )

    raise TransportError(
        "EXEC_FAILED",
        f"unrecognized runspace invoke result type: {type(raw).__name__}",
    )

