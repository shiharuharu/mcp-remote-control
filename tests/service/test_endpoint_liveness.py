"""Service tests: endpoint liveness (mark_dead, hanging WinRM, op_lock)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry, reset_registry
from mcp_remote_control.endpoint.registry import retire_refused_link
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport import TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

try:  # pypsrp is an optional extra; the double below covers its absence.
    from pypsrp import exceptions as _pypsrp_exceptions
except Exception:  # noqa: BLE001 - importability probe
    _pypsrp_exceptions = None


def _pypsrp_exc(qualname: str, *args: object) -> BaseException:
    """A pypsrp exception (real when importable, else a name-matching double)."""
    if _pypsrp_exceptions is not None:
        real = getattr(_pypsrp_exceptions, qualname, None)
        if real is not None:
            return real(*args)
    return type(qualname, (Exception,), {"__module__": "pypsrp.exceptions"})(*args)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def mrc_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    return FIXTURES


class _MockConn:
    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Open liveness after mark_dead + list open flag sync
# ---------------------------------------------------------------------------


def test_open_reconnects_when_transport_mark_dead(mrc_home: Path) -> None:
    """Registered ep with Endpoint.connected=True but transport dead must reconnect.

    After mark_dead the cache flag can stay True; open must not return the
    zombie - same liveness path as ensure_connected (mock connect count).
    """
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        connect_calls += 1
        return _MockConn()

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "ok"
    assert connect_calls == 1

    reg = get_registry()
    ep = reg.get("lab-ssh")
    assert ep is not None
    assert ep.transport is not None
    assert ep.connected is True

    # Simulate peer drop / exec path mark_dead without clearing Endpoint flag.
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark), "SSHTransport must expose mark_dead"
    mark("peer_reset")
    assert ep.transport.is_connected() is False
    # Stale cache: connected flag still True.
    ep.connected = True

    r2 = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r2.status == "ok", f"reopen failed: {r2.code} {r2.fields}"
    assert connect_calls == 2, (
        f"open must reconnect after mark_dead, connect_calls={connect_calls}"
    )
    ep2 = reg.get("lab-ssh")
    assert ep2 is not None
    assert ep2.connected is True
    assert ep2.transport is not None
    assert ep2.transport.is_connected() is True
    # Idempotent while live: third open must not reconnect again.
    r3 = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r3.status == "ok"
    assert connect_calls == 2, (
        f"live open must be idempotent, connect_calls={connect_calls}"
    )


def test_list_open_flag_after_mark_dead(mrc_home: Path) -> None:
    """After mark_dead, list body open= and fields.open must match live is_connected."""
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        connect_calls += 1
        return _MockConn()

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "ok"

    # Also open a live local ep so fields.open can be non-trivial (not just 0).
    r_local = endpoint_ops.run(op="open", profile="local", home=mrc_home)
    assert r_local.status == "ok"

    reg = get_registry()
    ep = reg.get("lab-ssh")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark)
    mark("peer_reset")
    # Leave a stale Endpoint.connected=True cache (mark_dead does not clear it).
    ep.connected = True

    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.status == "ok"
    assert listed.body is not None
    lab_lines = [ln for ln in listed.body.splitlines() if ln.startswith("lab-ssh")]
    assert lab_lines, f"lab-ssh missing from list body:\n{listed.body}"
    assert "open=0" in lab_lines[0], (
        f"list must not stick open=1 after mark_dead, line={lab_lines[0]!r}"
    )
    assert "open=1" not in lab_lines[0]
    # local still live -> body open=1 and fields.open == actual connected count.
    local_lines = [ln for ln in listed.body.splitlines() if ln.startswith("local ")]
    assert local_lines, f"local missing from list body:\n{listed.body}"
    assert any(tok == "open=1" for tok in local_lines[0].split())
    # body open=1 tokens must equal fields.open (live only; dead lab-ssh excluded).
    body_open = sum(
        1
        for ln in listed.body.splitlines()
        if any(tok == "open=1" for tok in ln.split())
    )
    assert listed.fields.get("open") == body_open
    assert listed.fields.get("open") == 1
    # Registry cache must have been synced by list_open.
    assert ep.connected is False


# ---------------------------------------------------------------------------
# WinRM mark_dead -> ensure/open reconnect (same registry liveness path)
# ---------------------------------------------------------------------------


def test_winrm_open_hanging_connector_no_fake_connected(
    mrc_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open with forever-sleep WinRM connector errors; no registered ep."""
    import threading
    import time

    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.25)
    real_init = winrm_mod.WinRMTransport.__init__

    def short_timeout_init(self: object, *args: object, **kwargs: object) -> None:
        kwargs = dict(kwargs)
        kwargs["connect_timeout_ms"] = 200
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(winrm_mod.WinRMTransport, "__init__", short_timeout_init)

    block = threading.Event()

    def hang_connector(**_kwargs: object) -> object:
        block.wait(timeout=30.0)
        return object()

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=mrc_home,
        connector=hang_connector,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"
    assert get_registry().get("lab-win") is None
    assert elapsed < 2.0, f"open wall-clock not bounded: {elapsed}s"
    block.set()


def test_winrm_ensure_reconnects_when_transport_mark_dead(mrc_home: Path) -> None:
    """WinRM session death -> ensure_connected opens a fresh transport.

    Registry liveness uses is_connected + is_alive + mark_dead. WinRM plugs
    into that path the same way SSH does.
    """

    class _WinSess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\mock"
            self.home = r"C:\Users\mock"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            from mcp_remote_control.transport.base import ExecResult

            del timeout_s, env
            return ExecResult(
                exit_code=0,
                stdout=f"out:{command}\n",
                cwd=cwd or self.cwd,
            )

    connect_calls = 0

    def connector(**_kwargs: object) -> _WinSess:
        nonlocal connect_calls
        connect_calls += 1
        return _WinSess()

    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "ok", f"open failed: {r.code} {r.fields}"
    assert connect_calls == 1

    ep = reg.get("lab-win")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark), "WinRMTransport must expose mark_dead"
    mark("session_invalidated")
    assert ep.transport.is_connected() is False
    ep.connected = True  # stale cache

    ep2 = ensure_endpoint("lab-win", home=mrc_home, probe=False, connector=connector)
    assert ep2.connected is True
    assert connect_calls == 2, (
        f"ensure must reconnect after WinRM mark_dead, connect_calls={connect_calls}"
    )
    assert ep2.transport is not None
    assert ep2.transport.is_connected() is True
    # open while live remains idempotent.
    r3 = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r3.status == "ok"
    assert connect_calls == 2


def test_endpoint_op_lock_present_after_open(mrc_home: Path) -> None:
    """Open local endpoint exposes transport op_lock on Endpoint."""
    r = endpoint_ops.run(op="open", profile="local", home=mrc_home)
    assert r.status == "ok"
    ep = get_registry().get("local")
    assert ep is not None
    assert ep.transport is not None
    assert ep.op_lock is ep.transport.op_lock
    # serial_ops re-enters cleanly (RLock).
    with ep.transport.serial_ops():
        assert ep.transport.is_connected() is True


def test_concurrent_mark_dead_and_list_no_crash(mrc_home: Path) -> None:
    """mark_dead under op_lock coexists with list/ensure without crash.

    Registry name locks and transport op_lock are distinct; mark_dead must
    not deadlock with list_open / ensure_connected under thread-pool load.
    """
    import threading

    endpoint_ops.run(op="open", profile="local", home=mrc_home)
    ep = get_registry().get("local")
    assert ep is not None and ep.transport is not None
    transport = ep.transport
    mark = getattr(transport, "mark_dead", None)
    # LocalTransport has no mark_dead - attach a serial-wrapped style mark
    # via the transport lock for the stress path, or use SSH mock.
    # Prefer SSH mock so real mark_dead (op_lock-wrapped) is exercised.
    reset_registry()

    class _Conn:
        def __init__(self) -> None:
            self.cwd = "/var/www"
            self.home = "/home/deploy"
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def connector(**_kwargs: object) -> _Conn:
        return _Conn()

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]
    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "ok", f"open failed: {r.code} {r.fields}"
    ep = reg.get("lab-ssh")
    assert ep is not None and ep.transport is not None
    mark = getattr(ep.transport, "mark_dead", None)
    assert callable(mark)

    errors: list[BaseException] = []
    err_lock = threading.Lock()
    stop = threading.Event()

    def marker() -> None:
        try:
            while not stop.is_set():
                mark("stress")
                # Re-arm so ensure/list keep seeing a usable flag until stop.
                ep.transport._connected = True  # type: ignore[union-attr]
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    def lister() -> None:
        try:
            for _ in range(30):
                endpoint_ops.run(op="list", home=mrc_home)
        except BaseException as exc:  # noqa: BLE001
            with err_lock:
                errors.append(exc)

    t_mark = threading.Thread(target=marker)
    t_list = threading.Thread(target=lister)
    t_mark.start()
    t_list.start()
    t_list.join(timeout=10)
    stop.set()
    t_mark.join(timeout=5)
    assert not errors, f"mark_dead/list race raised: {errors[:3]}"


def test_long_run_command_does_not_starve_other_name_open(
    mrc_home: Path,
) -> None:
    """Long run_command on A must not starve open of profile B.

    Service-level: hold SSH transport op_lock (as run_command does), force
    the dead-path mark_dead via is_connected, and assert concurrent open of
    a different profile completes without waiting for the full hold RTT.
    """
    import threading
    import time

    from mcp_remote_control.endpoint.registry import Endpoint
    from mcp_remote_control.transport.base import BaseTransport, ExecResult

    class _SlowOpTransport(BaseTransport):
        name = "slow-op"

        def __init__(self) -> None:
            super().__init__()
            self._alive = True

        def connect(self) -> None:
            self._connected = True
            self._alive = True

        def close(self) -> None:
            self._connected = False
            self._alive = False

        def is_alive(self) -> bool:
            return bool(self._connected and self._alive)

        def is_connected(self) -> bool:
            if not self._connected:
                return False
            if not self.is_alive():
                self.mark_dead("peer closed")
                return False
            return True

        def mark_dead(self, reason: str | None = None) -> None:
            self._connected = False
            self._alive = False
            if reason:
                self.meta = {**(self.meta or {}), "dead_reason": reason[:200]}

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            del command, cwd, timeout_s, env
            time.sleep(0.02)
            return ExecResult(exit_code=0, stdout="ok\n", cwd="/")

    reg = get_registry()
    hold_s = 1.0
    t_a = _SlowOpTransport()
    t_a.connect()
    with reg._lock:
        reg._endpoints["lab-ssh"] = Endpoint(
            name="lab-ssh",
            transport_name="slow-op",
            caps={"exec": True},
            connected=True,
            transport=t_a,
        )

    op_held = threading.Event()
    stop_hold = threading.Event()

    def hold_op() -> None:
        with t_a.serial_ops():
            op_held.set()
            stop_hold.wait(timeout=hold_s)

    th = threading.Thread(target=hold_op)
    th.start()
    assert op_held.wait(timeout=2.0)
    t_a._alive = False

    # Pressure list so mark_dead contends on A's op_lock without holding main.
    stop_list = threading.Event()

    def list_pressure() -> None:
        while not stop_list.is_set():
            endpoint_ops.run(op="list", home=mrc_home)
            time.sleep(0.005)

    t_list = threading.Thread(target=list_pressure)
    t_list.start()
    time.sleep(0.05)

    class _Conn:
        def __init__(self) -> None:
            self.cwd = "/var/www"
            self.home = "/home/deploy"
            self.closed = False

        def close(self) -> None:
            self.closed = True

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=mrc_home,
        connector=lambda **_k: _Conn(),
        probe=False,
    )
    elapsed = time.monotonic() - t0

    stop_list.set()
    stop_hold.set()
    t_list.join(timeout=5.0)
    th.join(timeout=hold_s + 2.0)

    assert r.status == "ok", f"open lab-win failed: {r.code} {r.fields}"
    assert elapsed < hold_s * 0.5, (
        f"open of other name starved during A op_lock hold: "
        f"elapsed={elapsed:.3f}s hold_s={hold_s}"
    )


def test_open_dead_transport_pops_registry(mrc_home: Path) -> None:
    """open ending not is_connected must error and leave no registry zombie.

    Mock SSH conn reports is_closed=True so SSHTransport.is_connected is False
    immediately after connect (peer-gone / probe-dead path). reg.open
    refuses to insert DOA and raises NOT_CONNECTED (dispose first);
    open_endpoint maps that to error without ever observing a registered
    zombie. open_endpoint post-check remains defense-in-depth.
    """

    class _DeadConn:
        def __init__(self) -> None:
            self.cwd = "/var/www"
            self.home = "/home/deploy"
            self.closed = False
            # SSHTransport.is_alive treats is_closed is True as peer gone.
            self.is_closed = True

        def close(self) -> None:
            self.closed = True

    dead_conns: list[_DeadConn] = []

    def connector(**_kwargs: object) -> _DeadConn:
        c = _DeadConn()
        dead_conns.append(c)
        return c

    # Registry-level: open never inserts a dead-on-arrival handle.
    reg = get_registry()
    with pytest.raises(TransportError) as ei:
        reg.open(
            "lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
    assert ei.value.code == "NOT_CONNECTED"
    assert reg.get("lab-ssh") is None, (
        "reg.open must not insert a dead-after-connect endpoint"
    )
    assert dead_conns, "connector should have produced a DOA conn"
    assert all(c.closed for c in dead_conns), (
        "DOA transport must be disposed (close) before raise \u2014 no FD leak"
    )

    # ensure_connected after DOA open must not return a live-looking Endpoint.
    with pytest.raises(TransportError) as ei2:
        reg.ensure_connected(
            "lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
    assert ei2.value.code == "NOT_CONNECTED"
    assert reg.get("lab-ssh") is None

    # Public open_endpoint path still surfaces NOT_CONNECTED + empty registry.
    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code == "NOT_CONNECTED"
    assert get_registry().get("lab-ssh") is None, (
        "failed open must not leave a zombie registration"
    )
    # list must not count a zombie open either.
    listed = endpoint_ops.run(op="list", home=mrc_home)
    assert listed.status == "ok"
    assert listed.fields.get("open") == 0
    if listed.body:
        for ln in listed.body.splitlines():
            if ln.startswith("lab-ssh"):
                assert not any(tok == "open=1" for tok in ln.split())


def test_reg_open_live_still_registers(mrc_home: Path) -> None:
    """Live successful open still registers and returns a connected endpoint."""

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    reg = get_registry()
    ep = reg.open(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert reg.get("lab-ssh") is ep
    assert ep.connected is True
    assert ep.transport is not None
    assert ep.transport.is_connected() is True


# ---------------------------------------------------------------------------
# Refused reconnect / refused link (open failure classification)
# ---------------------------------------------------------------------------


class _NoSeedSess:
    """WinRM session with no identity seeds: open must run an identity RTT."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def execute_ps(self, script: str, *, environment: object = None) -> object:
        raise self._exc


def test_open_rejection_tokens_distinguish_refused_from_unreachable(
    mrc_home: Path,
) -> None:
    """A refused reconnect carries a token an Agent can branch on.

    Both shapes fail the open-time identity probe and both surface as
    NOT_CONNECTED "identity probe failed: ..."; the refusal adds the HTTP
    status tokens, the unreachable host has none.
    """
    reg = get_registry()

    def refused(**_kwargs: object) -> _NoSeedSess:
        return _NoSeedSess(
            _pypsrp_exc("WinRMTransportError", "http", 400, "")
        )

    with pytest.raises(TransportError) as ei:
        reg.open("lab-win", home=mrc_home, connector=refused, probe=True)
    assert ei.value.code == "NOT_CONNECTED"
    assert ei.value.details.get("probe_failed") == 1
    assert ei.value.details.get("rejected") == 1
    assert ei.value.details.get("http_status") == 400
    assert ei.value.details.get("host") == "10.0.0.20"
    assert reg.get("lab-win") is None, "failed open must not register a handle"

    def unreachable(**_kwargs: object) -> _NoSeedSess:
        return _NoSeedSess(
            ConnectionError(
                "HTTPConnectionPool(host='10.0.0.20', port=5985): Max retries "
                "exceeded with url: /wsman"
            )
        )

    with pytest.raises(TransportError) as ei2:
        reg.open("lab-win", home=mrc_home, connector=unreachable, probe=True)
    assert ei2.value.code == "NOT_CONNECTED"
    assert ei2.value.details.get("probe_failed") == 1
    assert "rejected" not in ei2.value.details
    assert "http_status" not in ei2.value.details


def test_refused_link_retired_so_open_reconnects(mrc_home: Path) -> None:
    """A WSMan 401 refusal marks the link dead so ``open`` reconnects.

    The refusal is not in the fs client's link-failure predicate, so the
    transport keeps reporting connected and ``open`` (the documented remedy)
    reconnects nothing. Core retires it from the failure it observed; an
    ordinary remote failure must leave the link alone.
    """
    connect_calls = 0

    def connector(**_kwargs: object) -> _MockConn:
        nonlocal connect_calls
        connect_calls += 1
        return _MockConn()

    reg = get_registry()
    ep = reg.open("lab-win", home=mrc_home, connector=connector, probe=False)
    assert ep.transport is not None

    # A remote verdict (NOT_FOUND ...) is an answer from a live link.
    remote_err = FsError("NOT_FOUND", "path not found: C:\\temp\\x")
    assert retire_refused_link(ep.transport, remote_err) is False
    assert ep.transport.is_connected() is True
    assert connect_calls == 1

    # Core catches the backend's FsError; the refusal stays on the chain.
    err = FsError("FS_ERROR", "Failed to authenticate the user lab with ntlm")
    err.__cause__ = _pypsrp_exc(
        "AuthenticationError", "Failed to authenticate the user lab with ntlm"
    )
    assert retire_refused_link(ep.transport, err) is True
    assert ep.transport.is_connected() is False
    assert ep.transport.meta.get("link_lost") is True
    assert ep.transport.meta.get("reopen_hint") == "endpoint close then open"

    ep2 = reg.open("lab-win", home=mrc_home, connector=connector, probe=False)
    assert ep2 is not ep
    assert connect_calls == 2, "refused link must be replaced, not handed back"
    assert ep2.transport is not None and ep2.transport.is_connected() is True
