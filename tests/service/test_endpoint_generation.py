"""Service tests: endpoint generation pin, close_if_same, screen/ps teardown."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


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
# Open generation pin - dead cleanup must not kill a newer generation
# ---------------------------------------------------------------------------


def test_close_if_same_only_pops_matching_handle(mrc_home: Path) -> None:
    """close_if_same is identity-pinned: wrong generation is a no-op."""

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    reg = get_registry()
    e1 = reg.open(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert e1 is not None and e1.transport is not None

    # Newer generation under same name (mark_dead + ensure path).
    mark = getattr(e1.transport, "mark_dead", None)
    assert callable(mark)
    mark("peer_reset")
    e2 = reg.ensure_connected(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert e2 is not e1
    assert reg.get("lab-ssh") is e2
    assert e2.transport is not None
    assert e2.transport.is_connected()

    # Stale generation cleanup must not pop E2.
    removed = reg.close_if_same("lab-ssh", e1)
    assert removed is None
    still = reg.get("lab-ssh")
    assert still is e2
    assert still.transport is not None
    assert still.transport.is_connected()

    # Matching handle still closes (same-gen cleanup).
    removed2 = reg.close_if_same("lab-ssh", e2)
    assert removed2 is e2
    assert reg.get("lab-ssh") is None


def test_open_dead_cleanup_does_not_kill_newer_generation(
    mrc_home: Path,
) -> None:
    """Phase-1 returns E1; B mark_dead+ensure -> E2; A post-check
    fails on E1 -> open_endpoint dead path must not kill E2.
    """
    import threading

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    reg = get_registry()
    # Establish live E1 (what Thread A Phase-1 would return).
    e1 = reg.open(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert e1 is not None and e1.transport is not None
    assert e1.transport.is_connected()

    e2_holder: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=10)

    def thread_b_reconnect() -> None:
        try:
            barrier.wait()
            mark = getattr(e1.transport, "mark_dead", None)
            assert callable(mark)
            mark("peer_reset_from_b")
            e2 = reg.ensure_connected(
                "lab-ssh", home=mrc_home, connector=connector, probe=False
            )
            e2_holder.append(e2)
        except BaseException as exc:  # noqa: BLE001 - collect for main thread
            errors.append(exc)

    def thread_a_stale_open_dead_path() -> None:
        try:
            # Pin the Phase-1 handle (E1). Concurrent B may replace registry.
            pinned = e1
            barrier.wait()
            # Yield so B can mark_dead+ensure before our post-check cleanup.
            # Spin until registry no longer holds E1 (or timeout).
            deadline = __import__("time").monotonic() + 5.0
            while reg.get("lab-ssh") is pinned:
                if __import__("time").monotonic() > deadline:
                    break
                __import__("time").sleep(0.001)
            # Simulate open_endpoint post-check: pinned handle is dead.
            transport = pinned.transport
            live = False
            if transport is not None:
                try:
                    live = bool(transport.is_connected())
                except Exception:  # noqa: BLE001
                    live = False
            if not live:
                # Production path: identity-pinned close, not name-only.
                reg.close_if_same("lab-ssh", pinned)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=thread_a_stale_open_dead_path, name="q4-a")
    t_b = threading.Thread(target=thread_b_reconnect, name="q4-b")
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)
    assert not t_a.is_alive() and not t_b.is_alive(), "Q4 race threads hung"
    assert not errors, f"Q4 race raised: {errors[:3]}"

    assert e2_holder, "Thread B must register E2"
    e2 = e2_holder[0]
    current = reg.get("lab-ssh")
    assert current is e2, (
        "newer generation must remain registered after stale E1 cleanup"
    )
    assert current is not e1
    assert current.transport is not None
    assert current.transport.is_connected(), (
        "E2 must still be live after A reports dead on E1"
    )


def test_open_endpoint_stale_handle_cleanup_preserves_e2(
    mrc_home: Path,
) -> None:
    """open_endpoint dead path through public API leaves concurrent E2 live.

    After Phase-1-style handle E1 is pinned, mark_dead+ensure installs E2.
    A second open_endpoint call that would have cleaned up a dead same-name
    handle must not destroy E2 (close_if_same on its own returned handle only).
    """

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    # Live E1 via public open.
    r1 = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r1.status == "ok"
    reg = get_registry()
    e1 = reg.get("lab-ssh")
    assert e1 is not None and e1.transport is not None

    # Concurrent reconnect generation.
    e1.transport.mark_dead("peer_reset")  # type: ignore[union-attr]
    e2 = ensure_endpoint(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert e2 is not e1
    assert reg.get("lab-ssh") is e2

    # Stale E1 dead-path cleanup (what open_endpoint would do post-check).
    removed = reg.close_if_same("lab-ssh", e1)
    assert removed is None

    # Idempotent open still returns live same-gen (E2) handle.
    r2 = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r2.status == "ok"
    e_now = reg.get("lab-ssh")
    assert e_now is e2
    assert e_now is not None and e_now.transport is not None
    assert e_now.transport.is_connected()


def test_idempotent_open_returns_live_same_gen_handle(mrc_home: Path) -> None:
    """Phase-1 idempotent open returns the same live Endpoint object."""

    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    reg = get_registry()
    first = reg.open(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    second = reg.open(
        "lab-ssh", home=mrc_home, connector=connector, probe=False
    )
    assert second is first
    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
        probe=False,
    )
    assert r.status == "ok"
    assert reg.get("lab-ssh") is first
    assert first.transport is not None
    assert first.transport.is_connected()


# ---------------------------------------------------------------------------
# Dead-open teardown (close_if_same fences screen/ps)
# ---------------------------------------------------------------------------


def test_close_if_same_tears_down_attached_screen_ps(mrc_home: Path) -> None:
    """Matching close_if_same (dead-open path) closes generation screen/ps.

    Phase-1 live then death previously only closed transport; residual
    name-keyed screen/ps sessions must be snapshotted+closed for the dying
    generation.
    """
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        reg = get_registry()
        e1 = reg.open(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert e1 is not None and e1.transport is not None

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_pty = _m4_fake_pty()
        sreg.add(
            ScreenSession(
                id="scr_dead_open",
                ep="lab-ssh",
                pty=old_pty,  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_dead_open", ep="lab-ssh", handle=object()))
        # Unrelated endpoint sessions must not be touched.
        sreg.add(
            ScreenSession(
                id="scr_other_ep",
                ep="local",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_other_ep", ep="local", handle=object()))

        # Simulate post-open death while registry still holds E1.
        e1.transport.mark_dead("peer_gone_after_open")  # type: ignore[union-attr]
        assert not e1.transport.is_connected()

        removed = reg.close_if_same("lab-ssh", e1)
        assert removed is e1
        assert reg.get("lab-ssh") is None

        assert sreg.get("scr_dead_open") is None, "dying gen screen must close"
        assert preg.get("ps_dead_open") is None, "dying gen ps must close"
        assert getattr(old_pty, "closed", False) is True
        assert sreg.get("scr_other_ep") is not None
        assert preg.get("ps_other_ep") is not None
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_close_if_same_wrong_gen_preserves_newer_screen_ps(
    mrc_home: Path,
) -> None:
    """Pin miss on close_if_same must not tear down newer generation sessions.

    Stale E1 dead-path cleanup is a no-op for both transport and screen/ps
    when E2 is registered under the same name.
    """
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        reg = get_registry()
        e1 = reg.open(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert e1 is not None and e1.transport is not None

        # Reconnect installs E2; ensure retires any E1 sessions.
        e1.transport.mark_dead("peer_reset")  # type: ignore[union-attr]
        e2 = reg.ensure_connected(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert e2 is not e1
        assert reg.get("lab-ssh") is e2

        sreg = get_screen_registry()
        preg = get_ps_registry()
        e2_pty = _m4_fake_pty()
        sreg.add(
            ScreenSession(
                id="scr_e2",
                ep="lab-ssh",
                pty=e2_pty,  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_e2", ep="lab-ssh", handle=object()))

        # Stale E1 cleanup (open_endpoint dead path on Phase-1 handle).
        removed = reg.close_if_same("lab-ssh", e1)
        assert removed is None
        assert reg.get("lab-ssh") is e2
        assert e2.transport is not None
        assert e2.transport.is_connected()
        assert sreg.get("scr_e2") is not None, "E2 screen must survive pin miss"
        assert preg.get("ps_e2") is not None, "E2 ps must survive pin miss"
        assert getattr(e2_pty, "closed", False) is False
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_open_endpoint_dead_path_closes_attached_screen_ps(
    mrc_home: Path,
) -> None:
    """open_endpoint not-live cleanup closes residual screen/ps.

    Simulates Phase-1 returning a registered handle that is dead by the
    post-check: attach sessions, mark transport dead, stub reg.open to
    return that handle, then open_endpoint must error and tear down
    residual sessions.
    """
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        reg = get_registry()
        e1 = reg.open(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert e1 is not None and e1.transport is not None

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_pty = _m4_fake_pty()
        sreg.add(
            ScreenSession(
                id="scr_open_dead",
                ep="lab-ssh",
                pty=old_pty,  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_open_dead", ep="lab-ssh", handle=object()))

        # Death after sessions attached; handle still registered (no ensure).
        e1.transport.mark_dead("peer_gone")  # type: ignore[union-attr]
        assert reg.get("lab-ssh") is e1

        # Phase-1-style return of the same (now dead) handle without reconnect.
        def _return_dead_handle(*_a: object, **_k: object) -> object:
            return e1

        reg.open = _return_dead_handle  # type: ignore[method-assign]

        r = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        assert r.status == "error", f"expected dead-open error, got {r.status}"
        assert r.code in ("NOT_CONNECTED", "PROBE_FAILED")
        assert reg.get("lab-ssh") is None, "dead handle must be popped"
        assert sreg.get("scr_open_dead") is None, "open dead-path must close screen"
        assert preg.get("ps_open_dead") is None, "open dead-path must close ps"
        assert getattr(old_pty, "closed", False) is True
    finally:
        reset_screen_registry()
        reset_ps_registry()


# ---------------------------------------------------------------------------
# Close tears down only pre-close screen/ps sessions (not reopen race)
# ---------------------------------------------------------------------------


def test_close_endpoint_tears_down_attached_screens(mrc_home: Path) -> None:
    """Serial close still clears screen/ps sessions for the closed ep."""
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:

        class _FakePty:
            cols = 80
            rows = 24
            cwd = "/tmp"

            def is_alive(self) -> bool:
                return True

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
                return None

        endpoint_ops.run(op="open", profile="local", home=mrc_home)
        sreg = get_screen_registry()
        preg = get_ps_registry()
        sreg.add(
            ScreenSession(
                id="scr_close",
                ep="local",
                pty=_FakePty(),
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_close", ep="local", handle=object()))

        closed = endpoint_ops.run(op="close", ep="local")
        assert closed.status == "ok"
        assert closed.fields.get("screens_closed") == 1
        assert closed.fields.get("ps_closed") == 1
        assert sreg.get("scr_close") is None
        assert preg.get("ps_close") is None
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_close_endpoint_snapshot_survives_reopen_seed(mrc_home: Path) -> None:
    """Pre-close id snapshot: sessions added after snapshot (reopen generation)
    are not closed; only the snapshotted generation is.
    """
    import threading

    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:

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

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_scr = ScreenSession(
            id="scr_old", ep="lab-ssh", pty=_FakePty(), cols=80, rows=24
        )
        sreg.add(old_scr)
        preg.add(PsSession(id="ps_old", ep="lab-ssh", handle=object()))

        barrier = threading.Barrier(2, timeout=5.0)
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        real_close_ids = sreg.close_ids

        def close_ids_gate(session_ids: object) -> int:
            barrier.wait()
            return real_close_ids(session_ids)  # type: ignore[arg-type]

        sreg.close_ids = close_ids_gate  # type: ignore[method-assign]

        close_holder: list[object] = []

        def do_close() -> None:
            try:
                close_holder.append(endpoint_ops.run(op="close", ep="lab-ssh"))
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)

        def do_reopen() -> None:
            try:
                r = endpoint_ops.run(
                    op="open",
                    profile="lab-ssh",
                    home=mrc_home,
                    connector=connector,
                    probe=False,
                )
                assert r.status == "ok", r.fields
                sreg.add(
                    ScreenSession(
                        id="scr_new",
                        ep="lab-ssh",
                        pty=_FakePty(),
                        cols=80,
                        rows=24,
                    )
                )
                preg.add(PsSession(id="ps_new", ep="lab-ssh", handle=object()))
                barrier.wait()
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass

        t_c = threading.Thread(target=do_close)
        t_r = threading.Thread(target=do_reopen)
        t_c.start()
        for _ in range(200):
            if get_registry().get("lab-ssh") is None:
                break
            threading.Event().wait(0.01)
        t_r.start()
        t_c.join(timeout=10.0)
        t_r.join(timeout=10.0)

        assert not errors, f"workers raised: {errors[:3]}"
        assert close_holder and getattr(close_holder[0], "status") == "ok"
        assert sreg.get("scr_old") is None
        assert preg.get("ps_old") is None
        assert sreg.get("scr_new") is not None
        assert preg.get("ps_new") is not None
        assert get_registry().get("lab-ssh") is not None
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_close_endpoint_snapshot_fence_preserves_new_generation(
    mrc_home: Path,
) -> None:
    """Snapshot+close share the per-name fence.

    Concurrent reopen that would have slipped between a lock-external
    snapshot and name-close must not leave new-generation sessions orphaned
    on a killed transport, and must not be torn down by the dying
    generation's close_ids list.
    """
    import threading

    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    real_snap = screen_ops.snapshot_endpoint_session_ids
    reset_screen_registry()
    reset_ps_registry()
    try:

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

        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_scr = ScreenSession(
            id="scr_toctou_old",
            ep="lab-ssh",
            pty=_FakePty(),
            cols=80,
            rows=24,
        )
        sreg.add(old_scr)
        preg.add(PsSession(id="ps_toctou_old", ep="lab-ssh", handle=object()))

        barrier = threading.Barrier(2, timeout=5.0)
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        close_holder: list[object] = []

        def snap_gate(ep: str) -> list[str]:
            ids = real_snap(ep)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return ids

        screen_ops.snapshot_endpoint_session_ids = snap_gate  # type: ignore[assignment]

        def do_close() -> None:
            try:
                close_holder.append(endpoint_ops.run(op="close", ep="lab-ssh"))
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass

        def do_reopen() -> None:
            try:
                barrier.wait()
                r = endpoint_ops.run(
                    op="open",
                    profile="lab-ssh",
                    home=mrc_home,
                    connector=connector,
                    probe=False,
                )
                assert r.status == "ok", r.fields
                sreg.add(
                    ScreenSession(
                        id="scr_toctou_new",
                        ep="lab-ssh",
                        pty=_FakePty(),
                        cols=80,
                        rows=24,
                    )
                )
                preg.add(
                    PsSession(id="ps_toctou_new", ep="lab-ssh", handle=object())
                )
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)
                try:
                    barrier.abort()
                except Exception:  # noqa: BLE001
                    pass

        t_c = threading.Thread(target=do_close)
        t_r = threading.Thread(target=do_reopen)
        t_c.start()
        for _ in range(200):
            if barrier.n_waiting >= 1:
                break
            threading.Event().wait(0.01)
        t_r.start()
        t_c.join(timeout=10.0)
        t_r.join(timeout=10.0)

        assert not errors, f"workers raised: {errors[:3]}"
        assert close_holder and getattr(close_holder[0], "status") == "ok"
        fields = getattr(close_holder[0], "fields", {}) or {}
        assert fields.get("screens_closed") == 1
        assert fields.get("ps_closed") == 1
        # Dying generation only.
        assert sreg.get("scr_toctou_old") is None
        assert preg.get("ps_toctou_old") is None
        assert old_scr.closed is True
        # New generation fully alive after fence (not name-killed mid-swap).
        assert get_registry().get("lab-ssh") is not None
        assert sreg.get("scr_toctou_new") is not None
        assert preg.get("ps_toctou_new") is not None
    finally:
        screen_ops.snapshot_endpoint_session_ids = real_snap  # type: ignore[assignment]
        reset_screen_registry()
        reset_ps_registry()


# ---------------------------------------------------------------------------
# mark_dead / ensure-reconnect tears down generation screen/ps
# ---------------------------------------------------------------------------


def _m4_fake_pty() -> object:
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

    return _FakePty()


def test_ensure_reconnect_after_mark_dead_closes_screen_ps(
    mrc_home: Path,
) -> None:
    """mark_dead -> ensure_connected pop/reopen closes pre-generation
    screen/ps sessions (not only explicit endpoint close).
    """
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:
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

        sreg = get_screen_registry()
        preg = get_ps_registry()
        old_pty = _m4_fake_pty()
        sreg.add(
            ScreenSession(
                id="scr_m4_old",
                ep="lab-ssh",
                pty=old_pty,  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_m4_old", ep="lab-ssh", handle=object()))
        # Other endpoint sessions must not be touched.
        sreg.add(
            ScreenSession(
                id="scr_other",
                ep="local",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )

        reg = get_registry()
        ep = reg.get("lab-ssh")
        assert ep is not None and ep.transport is not None
        mark = getattr(ep.transport, "mark_dead", None)
        assert callable(mark)
        mark("peer_reset")
        ep.connected = True  # stale cache

        ep2 = ensure_endpoint(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert ep2.transport is not None
        assert ep2.transport.is_connected() is True
        assert connect_calls == 2

        assert sreg.get("scr_m4_old") is None, "old screen must die on reconnect"
        assert preg.get("ps_m4_old") is None, "old ps must die on reconnect"
        assert getattr(old_pty, "closed", False) is True
        assert sreg.get("scr_other") is not None, "other ep sessions must survive"
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_open_reconnect_after_mark_dead_closes_screen_ps(
    mrc_home: Path,
) -> None:
    """open() liveness path also retires generation screen/ps."""
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:
        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        assert (
            endpoint_ops.run(
                op="open",
                profile="lab-ssh",
                home=mrc_home,
                connector=connector,
                probe=False,
            ).status
            == "ok"
        )

        sreg = get_screen_registry()
        preg = get_ps_registry()
        sreg.add(
            ScreenSession(
                id="scr_open_old",
                ep="lab-ssh",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_open_old", ep="lab-ssh", handle=object()))

        ep = get_registry().get("lab-ssh")
        assert ep is not None and ep.transport is not None
        ep.transport.mark_dead("peer_reset")  # type: ignore[union-attr]
        ep.connected = True

        r2 = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        assert r2.status == "ok"
        assert sreg.get("scr_open_old") is None
        assert preg.get("ps_open_old") is None

        # Post-reconnect generation survives a later name-scoped fence.
        sreg.add(
            ScreenSession(
                id="scr_open_new",
                ep="lab-ssh",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_open_new", ep="lab-ssh", handle=object()))
        # Live open is idempotent - must not re-retire.
        r3 = endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        assert r3.status == "ok"
        assert sreg.get("scr_open_new") is not None
        assert preg.get("ps_open_new") is not None
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_reconnect_snapshot_does_not_kill_new_generation(
    mrc_home: Path,
) -> None:
    """Only pre-reconnect ids are closed; post-reconnect survive."""
    from mcp_remote_control.ps.registry import get_ps_registry, reset_ps_registry
    from mcp_remote_control.ps.session import PsSession
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )
    from mcp_remote_control.screen.session import ScreenSession

    reset_screen_registry()
    reset_ps_registry()
    try:
        def connector(**_kwargs: object) -> _MockConn:
            return _MockConn()

        endpoint_ops.run(
            op="open",
            profile="lab-ssh",
            home=mrc_home,
            connector=connector,
            probe=False,
        )
        sreg = get_screen_registry()
        preg = get_ps_registry()
        sreg.add(
            ScreenSession(
                id="scr_gen1",
                ep="lab-ssh",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_gen1", ep="lab-ssh", handle=object()))

        ep = get_registry().get("lab-ssh")
        assert ep is not None and ep.transport is not None
        ep.transport.mark_dead("peer_reset")  # type: ignore[union-attr]

        ensure_endpoint(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert sreg.get("scr_gen1") is None
        assert preg.get("ps_gen1") is None

        sreg.add(
            ScreenSession(
                id="scr_gen2",
                ep="lab-ssh",
                pty=_m4_fake_pty(),  # type: ignore[arg-type]
                cols=80,
                rows=24,
            )
        )
        preg.add(PsSession(id="ps_gen2", ep="lab-ssh", handle=object()))
        ensure_endpoint(
            "lab-ssh", home=mrc_home, connector=connector, probe=False
        )
        assert sreg.get("scr_gen2") is not None
        assert preg.get("ps_gen2") is not None
    finally:
        reset_screen_registry()
        reset_ps_registry()


def test_open_locked_doc_matches_race_loser_build() -> None:
    """_open_locked docs admit race losers still full-build then discard."""
    from mcp_remote_control.endpoint.registry import EndpointRegistry

    doc = EndpointRegistry._open_locked.__doc__ or ""
    assert "per-name" in doc.lower() or "serialize" in doc.lower()
    # Must not claim same-name openers never duplicate build work.
    assert "don't duplicate work" not in doc
    assert "do not duplicate work" not in doc
    # Reality: waiters/losers may still build then discard.
    assert "loser" in doc.lower() or "full-build" in doc.lower() or "discard" in doc.lower()
