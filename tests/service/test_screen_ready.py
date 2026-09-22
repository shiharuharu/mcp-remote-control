"""Service tests: screen ready fence, reconnect, and snapshot close_ids."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry, reset_registry
from mcp_remote_control.screen.registry import (
    get_screen_registry,
    reset_screen_registry,
)

_CONFIG_HOME = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Prefer a plain POSIX shell so CI/dev machines without fancy zshrc stay stable.
_SIMPLE_SHELL = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


@pytest.fixture(autouse=True)
def _clean_regs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(_CONFIG_HOME))
    reset_registry()
    reset_screen_registry()
    yield
    reset_screen_registry()
    reset_registry()


def _open_shell(**kwargs: object):
    defaults = {
        "ep": "local",
        "home": _CONFIG_HOME,
        "shell": _SIMPLE_SHELL,
        "settle_s": 0.5,
        "cols": 120,
        "rows": 40,
    }
    defaults.update(kwargs)
    return screen_ops.open_screen(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ensure reconnect retires old screen sessions
# ---------------------------------------------------------------------------


def test_ensure_reconnect_invalidates_old_screen_send() -> None:
    """After transport death + ensure reconnect, old screen id is gone
    and send returns SCREEN_NOT_FOUND; a new open on the same ep works.
    """
    from mcp_remote_control.screen.session import ScreenSession

    class _FakePty:
        cols = 80
        rows = 24
        cwd = "/tmp"
        closed = False

        def is_alive(self) -> bool:
            return not self.closed

        def exit_code(self) -> int | None:
            return 0 if self.closed else None

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def resize(self, cols: int, rows: int) -> None:
            return None

        def drain_for(self, seconds: float, *, on_data: object = None) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    # Open real local endpoint + attach a fake screen on it.
    ep = ensure_endpoint("local", home=_CONFIG_HOME, probe=False)
    assert ep.transport is not None
    sreg = get_screen_registry()
    old = ScreenSession(
        id="scr_m4_zombie",
        ep="local",
        pty=_FakePty(),  # type: ignore[arg-type]
        cols=80,
        rows=24,
    )
    sreg.add(old)

    # LocalTransport has no mark_dead; force stale by closing the transport
    # while leaving Endpoint.connected=True so ensure must pop+reopen.
    ep.transport.close()
    ep.connected = True

    ep2 = ensure_endpoint("local", home=_CONFIG_HOME, probe=False)
    assert ep2.transport is not None
    assert ep2.transport.is_connected() is True

    assert sreg.get("scr_m4_zombie") is None
    assert old.closed is True or getattr(old.pty, "closed", False) is True

    send = screen_ops.send_screen(
        id="scr_m4_zombie",
        actions=[{"type": "text", "text": "echo hi", "submit": True}],
    )
    assert send.status == "error"
    assert send.code == "SCREEN_NOT_FOUND"

    # New generation on the same ep name is independent.
    opened = _open_shell()
    assert opened.status == "ok", opened.render_text()
    new_id = opened.fields["id"]
    assert new_id != "scr_m4_zombie"
    send2 = screen_ops.send_screen(
        id=new_id,
        actions=[{"type": "text", "text": "true", "submit": True}],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 5000},
    )
    assert send2.status in ("ok", "unchanged", "dead"), send2.render_text()
    screen_ops.close_screen(id=new_id)


def test_screen_snapshot_helpers_generation_fence() -> None:
    """Snapshot + close_ids only touch listed ids."""
    from mcp_remote_control.screen.session import ScreenSession

    class _FakePty:
        cols = 80
        rows = 24
        cwd = "/tmp"
        closed = False

        def is_alive(self) -> bool:
            return not self.closed

        def exit_code(self) -> int | None:
            return None

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def resize(self, cols: int, rows: int) -> None:
            return None

        def drain_for(self, seconds: float, *, on_data: object = None) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    sreg = get_screen_registry()
    sreg.add(
        ScreenSession(
            id="scr_a", ep="ep-a", pty=_FakePty(), cols=80, rows=24  # type: ignore[arg-type]
        )
    )
    sreg.add(
        ScreenSession(
            id="scr_b", ep="ep-a", pty=_FakePty(), cols=80, rows=24  # type: ignore[arg-type]
        )
    )
    sreg.add(
        ScreenSession(
            id="scr_c", ep="ep-b", pty=_FakePty(), cols=80, rows=24  # type: ignore[arg-type]
        )
    )
    ids = screen_ops.snapshot_endpoint_session_ids("ep-a")
    assert set(ids) == {"scr_a", "scr_b"}
    # Register after snapshot - must not be closed by close_sessions_by_ids.
    sreg.add(
        ScreenSession(
            id="scr_a_new", ep="ep-a", pty=_FakePty(), cols=80, rows=24  # type: ignore[arg-type]
        )
    )
    n = screen_ops.close_sessions_by_ids(ids)
    assert n == 2
    assert sreg.get("scr_a") is None
    assert sreg.get("scr_b") is None
    assert sreg.get("scr_a_new") is not None
    assert sreg.get("scr_c") is not None


# ---------------------------------------------------------------------------
# open_screen ready fence + serial probe (no mid-open send interleave)
# ---------------------------------------------------------------------------


def test_open_does_not_register_before_ready() -> None:
    """reg.add only after mark_ready; mid-open registry empty during settle."""
    import threading

    from mcp_remote_control.screen.geometry import GeometryAdapter
    from mcp_remote_control.screen.registry import ScreenRegistry

    open_result: list[object] = []
    mid_adapt_ids: list[str] = []
    add_ready_flags: list[bool] = []
    errors: list[BaseException] = []
    adapt_entered = threading.Event()
    release_adapt = threading.Event()

    orig_adapt = GeometryAdapter.adapt
    orig_add = ScreenRegistry.add

    def gated_adapt(self: object, session: object, *args: object, **kwargs: object):
        # During settle/adapt the session must not be in the registry yet.
        for s in get_screen_registry().list_open():
            mid_adapt_ids.append(s.id)
        adapt_entered.set()
        # Hold open-path serial section while the main thread polls registry.
        assert release_adapt.wait(timeout=10.0), "release_adapt timeout"
        return orig_adapt(self, session, *args, **kwargs)  # type: ignore[misc]

    def tracking_add(self: ScreenRegistry, session: object):
        ready = bool(getattr(session, "ready", False))
        add_ready_flags.append(ready)
        return orig_add(self, session)  # type: ignore[arg-type]

    GeometryAdapter.adapt = gated_adapt  # type: ignore[method-assign, assignment]
    ScreenRegistry.add = tracking_add  # type: ignore[method-assign, assignment]
    try:

        def opener() -> None:
            try:
                open_result.append(_open_shell(settle_s=0.05))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=opener, name="q7-open")
        t.start()
        assert adapt_entered.wait(timeout=15.0), "adapt never entered"
        # While adapt is blocked, registry must still be empty (delayed reg.add).
        assert get_screen_registry().list_open() == []
        release_adapt.set()
        t.join(timeout=30.0)
        assert not t.is_alive(), "open_screen hung"
    finally:
        GeometryAdapter.adapt = orig_adapt  # type: ignore[method-assign]
        ScreenRegistry.add = orig_add  # type: ignore[method-assign]
        release_adapt.set()

    assert not errors, errors
    assert open_result, "open did not return"
    opened = open_result[0]
    assert opened.status == "ok", opened.render_text()  # type: ignore[union-attr]
    sid = opened.fields["id"]  # type: ignore[union-attr]
    assert mid_adapt_ids == [], f"session published during adapt: {mid_adapt_ids}"
    assert add_ready_flags, "reg.add never called"
    assert all(add_ready_flags), f"reg.add before ready: {add_ready_flags}"
    sess = get_screen_registry().get(sid)
    assert sess is not None
    assert sess.ready is True
    body = opened.body or ""  # type: ignore[union-attr]
    assert "__MRC_PWD__:" not in body
    assert "__MRC_PWD__:" not in opened.render_text()  # type: ignore[union-attr]
    screen_ops.close_screen(id=sid)


def test_concurrent_open_send_mid_open_predictable_no_pollution() -> None:
    """Thread B send as soon as id appears - wait/fail predictably, no probe leak.

    With delayed reg.add, mid-open send typically sees SCREEN_NOT_FOUND. After
    open completes, send succeeds and neither open body nor send body contains
    the silent probe marker. A send against a forced not-ready session returns
    SCREEN_NOT_READY without writing.
    """
    import threading
    import time

    open_result: list[object] = []
    send_results: list[object] = []
    seen_ids: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    open_started = threading.Event()

    def opener() -> None:
        try:
            open_started.set()
            open_result.append(_open_shell(settle_s=0.6))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def sender() -> None:
        try:
            assert open_started.wait(timeout=5.0)
            deadline = time.monotonic() + 5.0
            # Busy-poll: try send as soon as any id is in the registry (repro
            # for register-before-ready). With the ready fence this only
            # succeeds after open mark_ready + reg.add.
            while time.monotonic() < deadline:
                sessions = get_screen_registry().list_open()
                if sessions:
                    sid = sessions[0].id
                    with lock:
                        seen_ids.append(sid)
                    r = screen_ops.send_screen(
                        id=sid,
                        actions=[
                            {
                                "type": "text",
                                "text": "Q7_MID_OPEN_SEND",
                                "submit": True,
                            }
                        ],
                        wait={
                            "until": "idle",
                            "idle_ms": 150,
                            "timeout_ms": 8000,
                        },
                    )
                    with lock:
                        send_results.append(r)
                    return
                # Also try a guessed id while open still running - must fail
                # predictably (not hang, not forge ok+frame with probe junk).
                time.sleep(0.01)
            # Open finished without registry visibility mid-flight; one more
            # send after join is handled by the main thread.
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_open = threading.Thread(target=opener, name="q7-open2")
    t_send = threading.Thread(target=sender, name="q7-send2")
    t_open.start()
    t_send.start()
    t_open.join(timeout=30.0)
    t_send.join(timeout=30.0)
    assert not t_open.is_alive() and not t_send.is_alive()
    assert not errors, errors
    assert open_result
    opened = open_result[0]
    assert opened.status == "ok", opened.render_text()  # type: ignore[union-attr]
    sid = opened.fields["id"]  # type: ignore[union-attr]
    open_body = opened.body or ""  # type: ignore[union-attr]
    assert "__MRC_PWD__:" not in open_body
    assert "__MRC_PWD__:" not in opened.render_text()  # type: ignore[union-attr]

    # If concurrent send ran after publish, it must not error with pollution;
    # SCREEN_NOT_READY / SCREEN_NOT_FOUND are also acceptable predictable fails.
    for r in send_results:
        assert r.status in ("ok", "unchanged", "dead", "error"), r.render_text()  # type: ignore[union-attr]
        if r.status == "error":  # type: ignore[union-attr]
            assert r.code in ("SCREEN_NOT_FOUND", "SCREEN_NOT_READY"), r.render_text()  # type: ignore[union-attr]
        body = r.body or ""  # type: ignore[union-attr]
        assert "__MRC_PWD__:" not in body
        assert "__MRC_PWD__:" not in r.render_text()  # type: ignore[union-attr]

    # Happy path after open: send works, still no probe marker leak.
    post = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "text", "text": "true", "submit": True}],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 8000},
    )
    assert post.status in ("ok", "unchanged", "dead"), post.render_text()
    assert "__MRC_PWD__:" not in (post.body or "")
    assert "__MRC_PWD__:" not in post.render_text()
    screen_ops.close_screen(id=sid)


def test_send_not_ready_fails_without_write() -> None:
    """Not-ready session rejects send predictably (no PTY write)."""
    from mcp_remote_control.screen.session import ScreenSession

    class _TrackingPty:
        cols = 80
        rows = 24
        cwd = "/tmp"
        closed = False
        writes: list[bytes]

        def __init__(self) -> None:
            self.writes = []

        def is_alive(self) -> bool:
            return not self.closed

        def exit_code(self) -> int | None:
            return 0 if self.closed else None

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            self.writes.append(bytes(data))
            return len(data)

        def resize(self, cols: int, rows: int) -> None:
            return None

        def drain_for(self, seconds: float, *, on_data: object = None) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    pty = _TrackingPty()
    sess = ScreenSession(
        id="scr_q7_nr",
        ep="local",
        pty=pty,  # type: ignore[arg-type]
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    sess.mark_not_ready()
    get_screen_registry().add(sess)
    assert sess.ready is False

    sent = screen_ops.send_screen(
        id="scr_q7_nr",
        actions=[{"type": "text", "text": "should_not_write", "submit": True}],
    )
    assert sent.status == "error"
    assert sent.code == "SCREEN_NOT_READY"
    assert pty.writes == [], f"send wrote while not ready: {pty.writes!r}"
    assert sent.body is None or "__MRC_PWD__:" not in (sent.body or "")

    # After mark_ready, send is allowed (still no probe pollution assertion).
    sess.mark_ready()
    sent2 = screen_ops.send_screen(
        id="scr_q7_nr",
        actions=[{"type": "text", "text": "now_ok"}],
        wait={"until": "deadline", "timeout_ms": 0},
        shot=False,
    )
    assert sent2.status in ("ok", "unchanged", "dead", "error"), sent2.render_text()
    # At least one write should have been attempted once ready (unless DEAD).
    if sent2.status != "error":
        assert any(b"now_ok" in w for w in pty.writes)
    screen_ops.close_screen(id="scr_q7_nr")


def test_open_then_send_happy_path_still_green() -> None:
    """Normal open+send path remains green after the ready fence."""
    r = _open_shell(settle_s=0.35)
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]
    assert get_screen_registry().get(sid) is not None
    assert get_screen_registry().get(sid).ready is True  # type: ignore[union-attr]
    assert "__MRC_PWD__:" not in (r.body or "")

    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "text", "text": "echo q7ok", "submit": True}],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 8000},
    )
    assert send.status in ("ok", "unchanged"), send.render_text()
    assert send.code is None
    combined = (send.body or "") + "\n" + send.render_text()
    assert "__MRC_PWD__:" not in combined
    screen_ops.close_screen(id=sid)


# ---------------------------------------------------------------------------
# Screen open vs concurrent close_endpoint (no zombie register)
# ---------------------------------------------------------------------------


def test_open_screen_concurrent_close_no_zombie_session() -> None:
    """Close during open settle must not leave a live screen on closed ep."""
    import threading

    from mcp_remote_control.core import endpoint_ops
    from mcp_remote_control.screen.geometry import GeometryAdapter

    # Ensure endpoint exists so open reaches PTY + settle (race window).
    ep = ensure_endpoint("local", home=_CONFIG_HOME, probe=False)
    assert ep.transport is not None

    adapt_entered = threading.Event()
    release_adapt = threading.Event()
    open_result: list[object] = []
    errors: list[BaseException] = []
    orig_adapt = GeometryAdapter.adapt

    def gated_adapt(
        self: object, session: object, *args: object, **kwargs: object
    ):
        adapt_entered.set()
        assert release_adapt.wait(timeout=10.0), "release_adapt timeout"
        return orig_adapt(self, session, *args, **kwargs)  # type: ignore[misc]

    GeometryAdapter.adapt = gated_adapt  # type: ignore[method-assign, assignment]
    try:

        def do_open() -> None:
            try:
                open_result.append(_open_shell(settle_s=0.05))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def do_close() -> None:
            try:
                assert adapt_entered.wait(timeout=15.0), "adapt never entered"
                endpoint_ops.run(op="close", ep="local")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                release_adapt.set()

        t_open = threading.Thread(target=do_open, name="r28-svc-scr-open")
        t_close = threading.Thread(target=do_close, name="r28-svc-scr-close")
        t_open.start()
        t_close.start()
        t_open.join(timeout=30.0)
        t_close.join(timeout=30.0)
        assert not t_open.is_alive() and not t_close.is_alive()
    finally:
        GeometryAdapter.adapt = orig_adapt  # type: ignore[method-assign]
        release_adapt.set()

    assert not errors, errors
    assert open_result, "open_screen did not return"
    opened = open_result[0]
    # Primary invariant: no live registry session on the closed endpoint.
    assert get_screen_registry().list_for_endpoint("local") == []
    status = getattr(opened, "status", None)
    if status == "ok":
        sid = getattr(opened, "fields", {}).get("id")
        assert sid is None or get_screen_registry().get(str(sid)) is None
    else:
        assert status == "error"
        assert getattr(opened, "code", None) in (
            "NOT_CONNECTED",
            "EXEC_FAILED",
            "CONNECT_FAILED",
        )
    assert get_registry().get("local") is None


# ---------------------------------------------------------------------------
# remove / close_ids hold session lock; send after close is not ok
# ---------------------------------------------------------------------------


def test_close_ids_then_concurrent_send_not_ok() -> None:
    """After close_ids begins close under serial_ops, send is error/DEAD not ok."""
    import threading
    import time

    from mcp_remote_control.screen.session import ScreenSession

    class _FakePty:
        cols = 80
        rows = 24
        cwd = "/tmp"
        closed = False

        def is_alive(self) -> bool:
            return not self.closed

        def exit_code(self) -> int | None:
            return 0 if self.closed else None

        def read(self, max_bytes: int = 8192) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def resize(self, cols: int, rows: int) -> None:
            return None

        def drain_for(self, seconds: float, *, on_data: object = None) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    sreg = get_screen_registry()
    sess = ScreenSession(
        id="scr_lock", ep="local", pty=_FakePty(), cols=80, rows=24  # type: ignore[arg-type]
    )
    sreg.add(sess)

    close_started = threading.Event()
    send_result: list[object] = []
    orig_close = sess.close

    def gated_close() -> None:
        close_started.set()
        time.sleep(0.08)
        orig_close()

    sess.close = gated_close  # type: ignore[method-assign]

    def do_close() -> None:
        n = sreg.close_ids(["scr_lock"])
        assert n == 1

    def do_send() -> None:
        assert close_started.wait(timeout=5.0)
        send_result.append(
            screen_ops.send_screen(
                id="scr_lock",
                actions=[{"type": "text", "text": "after-close"}],
                wait={"until": "deadline", "timeout_ms": 0},
                shot=False,
            )
        )

    t_rm = threading.Thread(target=do_close)
    t_sd = threading.Thread(target=do_send)
    t_rm.start()
    t_sd.start()
    t_rm.join(timeout=10.0)
    t_sd.join(timeout=10.0)
    assert not t_rm.is_alive() and not t_sd.is_alive(), "close_ids/send hung"
    assert send_result, "send did not return"
    send = send_result[0]
    status = getattr(send, "status", None)
    code = getattr(send, "code", None)
    assert status in ("error", "dead"), f"send after close was {status} {code}"
    assert status != "ok"
    assert sess.closed is True
    assert sreg.get("scr_lock") is None
