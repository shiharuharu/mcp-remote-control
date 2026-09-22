"""Service tests: SSH PTY open deadline, late close, and dead channel."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.screen.registry import reset_screen_registry

_CONFIG_HOME = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_regs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(_CONFIG_HOME))
    reset_registry()
    reset_screen_registry()
    yield
    reset_screen_registry()
    reset_registry()


def test_ssh_mock_screen_open() -> None:
    """SSH open uses create_process PTY path; mockable without network."""

    class MockProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None
            self._chunks = [b"mock-shell$\r\n"]

        def write(self, data: bytes) -> None:
            return None

        async def read(self, n: int = 8192) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self) -> None:
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> MockProc:
            return MockProc()

        def close(self) -> None:
            return None

    reg = get_registry()
    reg.ssh_connector = lambda **_k: MockConn()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        settle_s=0.15,
        cols=100,
        rows=30,
    )
    assert r.status == "ok", r.render_text()
    assert r.fields.get("ep") == "lab-ssh"
    assert r.fields.get("cur")
    assert r.fields.get("open") == "shell"
    body = r.body or ""
    assert "mock-shell" in body or r.fields.get("gen", 0) >= 0
    sid = r.fields["id"]
    assert screen_ops.close_screen(id=sid).status == "ok"


# ---------------------------------------------------------------------------
# SSH PTY create_process / open_shell_pty wall-clock open deadline
# ---------------------------------------------------------------------------


def test_ssh_pty_create_process_hang_open_screen_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung create_process -> open_screen errors TIMEOUT within budget."""
    import asyncio
    import time

    from mcp_remote_control.screen import ssh_pty as ssh_pty_mod

    budget = 0.15
    monkeypatch.setattr(ssh_pty_mod, "DEFAULT_SSH_PTY_OPEN_TIMEOUT_S", budget)

    class HangConn:
        async def create_process(self, *args: object, **kwargs: object) -> object:
            # Endpoint open runs collect_probe via create_process (no PTY).
            # Only hang interactive PTY open (term_type/term_size) so probe
            # soft-fails fast and the budget measures screen open, not probes.
            if "term_type" in kwargs or "term_size" in kwargs:
                await asyncio.sleep(3600.0)
                raise RuntimeError("hang should have timed out")
            raise OSError("connection lost")

        def close(self) -> None:
            return None

    reg = get_registry()
    reg.ssh_connector = lambda **_k: HangConn()
    t0 = time.monotonic()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        settle_s=0.05,
        cols=80,
        rows=24,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error", r.render_text()
    assert r.code == "TIMEOUT", r.render_text()
    msg = (r.fields.get("msg") or "").lower()
    assert "timed out" in msg or "timeout" in msg, r.render_text()
    # Must not park the thread forever; allow CI scheduling slack.
    # Bridge grace on open is budget+~1s; probes fail fast (no term).
    assert elapsed < budget + 3.0, f"elapsed={elapsed}s budget={budget}s"
    assert elapsed >= budget * 0.5


def test_ssh_pty_open_shell_pty_factory_hang_timeout() -> None:
    """Hung open_shell_pty factory also hits the create-only open budget."""
    import asyncio
    import time

    from mcp_remote_control.screen.ssh_pty import SshPty
    from mcp_remote_control.transport.base import TransportError

    budget = 0.15

    class HangFactoryConn:
        async def open_shell_pty(self, **kwargs: object) -> object:
            del kwargs
            await asyncio.sleep(3600.0)
            raise RuntimeError("hang should have timed out")

    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        SshPty.open_shell(HangFactoryConn(), cols=80, rows=24, open_timeout_s=budget)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert "timed out" in ei.value.msg.lower() or "timeout" in ei.value.msg.lower()
    assert elapsed < budget + 2.0, f"elapsed={elapsed}s budget={budget}s"
    assert elapsed >= budget * 0.5


def test_ssh_pty_open_deadline_default_budget() -> None:
    """Default create budget is named and connect/SFTP-aligned (~60s)."""
    from mcp_remote_control.screen.ssh_pty import DEFAULT_SSH_PTY_OPEN_TIMEOUT_S

    assert DEFAULT_SSH_PTY_OPEN_TIMEOUT_S == 60.0


def test_ssh_mock_screen_open_unchanged_under_open_deadline() -> None:
    """Normal sync create_process open still succeeds under the open deadline."""

    class MockProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None
            self._chunks = [b"mock-shell$\r\n"]

        def write(self, data: bytes) -> None:
            return None

        async def read(self, n: int = 8192) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self) -> None:
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> MockProc:
            return MockProc()

        def close(self) -> None:
            return None

    reg = get_registry()
    reg.ssh_connector = lambda **_k: MockConn()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        settle_s=0.1,
        cols=80,
        rows=24,
    )
    assert r.status == "ok", r.render_text()
    assert r.code is None
    sid = r.fields["id"]
    assert screen_ops.close_screen(id=sid).status == "ok"


# ---------------------------------------------------------------------------
# Open timeout -> best-effort close of late create_process / open_shell_pty
# ---------------------------------------------------------------------------


def test_ssh_pty_create_process_late_complete_closed_on_open_timeout() -> None:
    """create_process finishing after open budget -> process.close once.

    Open path still raises TransportError(TIMEOUT) within the budget; the
    late process is not left orphaned.
    """
    import asyncio
    import threading
    import time

    from mcp_remote_control.screen.ssh_pty import SshPty
    from mcp_remote_control.transport.base import TransportError

    budget = 0.15
    closed = {"n": 0}
    ready = threading.Event()

    class LateProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None

        def write(self, data: bytes) -> None:
            return None

        async def read(self, n: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            closed["n"] += 1
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    class LateConn:
        async def create_process(self, *args: object, **kwargs: object) -> LateProc:
            del args, kwargs
            await asyncio.sleep(budget + 0.25)
            proc = LateProc()
            ready.set()
            return proc

    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        SshPty.open_shell(LateConn(), cols=80, rows=24, open_timeout_s=budget)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert "timed out" in ei.value.msg.lower() or "timeout" in ei.value.msg.lower()
    # Caller must not wait for the late create_process.
    assert elapsed < budget + 2.0, f"elapsed={elapsed}s budget={budget}s"
    assert elapsed >= budget * 0.5

    assert ready.wait(timeout=3.0), "late create_process never finished"
    deadline = time.monotonic() + 2.0
    while closed["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert closed["n"] >= 1, "late process.close was not invoked"
    # close is idempotent-safe; should not thrash.
    assert closed["n"] <= 2


def test_ssh_pty_open_shell_pty_late_complete_closed_on_open_timeout() -> None:
    """open_shell_pty factory late-complete -> result.close after TIMEOUT."""
    import asyncio
    import threading
    import time

    from mcp_remote_control.screen.ssh_pty import SshPty
    from mcp_remote_control.transport.base import TransportError

    budget = 0.15
    closed = {"n": 0}
    ready = threading.Event()

    class LateProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None

        def close(self) -> None:
            closed["n"] += 1
            self.exit_status = 0

    class LateFactoryConn:
        async def open_shell_pty(self, **kwargs: object) -> LateProc:
            del kwargs
            await asyncio.sleep(budget + 0.25)
            proc = LateProc()
            ready.set()
            return proc

    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        SshPty.open_shell(
            LateFactoryConn(), cols=80, rows=24, open_timeout_s=budget
        )
    elapsed = time.monotonic() - t0
    assert ei.value.code == "TIMEOUT"
    assert elapsed < budget + 2.0, f"elapsed={elapsed}s budget={budget}s"

    assert ready.wait(timeout=3.0), "late open_shell_pty never finished"
    deadline = time.monotonic() + 2.0
    while closed["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert closed["n"] >= 1, "late factory process.close was not invoked"


def test_ssh_pty_in_budget_open_does_not_extra_close() -> None:
    """Open that finishes inside budget must not close the process."""
    import asyncio

    from mcp_remote_control.screen.ssh_pty import SshPty

    closed = {"n": 0}

    class QuickProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None

        def write(self, data: bytes) -> None:
            return None

        async def read(self, n: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            closed["n"] += 1
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    class QuickConn:
        async def create_process(self, *args: object, **kwargs: object) -> QuickProc:
            del args, kwargs
            await asyncio.sleep(0.01)
            return QuickProc()

    pty = SshPty.open_shell(QuickConn(), cols=80, rows=24, open_timeout_s=2.0)
    assert closed["n"] == 0, "successful open must not best-effort-close the process"
    pty.close()
    assert closed["n"] >= 1


# ---------------------------------------------------------------------------
# SSH channel death -> is_alive False + send fails fast with DEAD
# ---------------------------------------------------------------------------


class _ConnectionLost(Exception):
    """Mirrors asyncssh.ConnectionLost by class name for duck-typed detection."""


class _ChannelClosed(Exception):
    """Mirrors asyncssh.ChannelClosed by class name."""


def test_ssh_pty_connection_lost_marks_dead_is_alive_false() -> None:
    """Peer drop via ConnectionLost on read -> is_alive False (no _closed)."""
    from mcp_remote_control.screen.ssh_pty import SshPty

    class DeadReadProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            # Peer drop often leaves exit_status None forever - the zombie case.
            self.exit_status: int | None = None

        def write(self, data: bytes) -> None:
            return None

        async def read(self, n: int = 8192) -> bytes:
            raise _ConnectionLost("connection lost")

        def close(self) -> None:
            return None

    pty = SshPty(DeadReadProc(), cols=80, rows=24)
    assert pty.is_alive() is True  # not yet observed dead
    assert pty._closed is False
    chunk = pty.read()
    assert chunk == b""
    # Fatal exception must mark dead; is_alive must not fall back to not _closed.
    assert pty.is_alive() is False
    assert pty._closed is False
    assert pty._dead is True


def test_ssh_pty_channel_closed_on_write_marks_dead() -> None:
    """ChannelClosed on write -> DEAD TransportError + is_alive False."""
    from mcp_remote_control.screen.ssh_pty import SshPty
    from mcp_remote_control.transport.base import TransportError

    class DeadWriteProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None

        def write(self, data: bytes) -> None:
            raise _ChannelClosed("channel closed by remote")

        async def read(self, n: int = 8192) -> bytes:
            return b""

        def close(self) -> None:
            return None

    pty = SshPty(DeadWriteProc(), cols=80, rows=24)
    with pytest.raises(TransportError) as ei:
        pty.write(b"echo hi\n")
    assert ei.value.code == "DEAD"
    assert "dead" in ei.value.msg.lower() or "channel" in ei.value.msg.lower()
    assert pty.is_alive() is False
    assert pty._closed is False


def test_ssh_pty_is_alive_false_when_channel_flag_closed() -> None:
    """channel.is_closed without exit_status -> is_alive False."""
    from mcp_remote_control.screen.ssh_pty import SshPty

    class Chan:
        def is_closed(self) -> bool:
            return True

    class Proc:
        def __init__(self) -> None:
            self.exit_status: int | None = None
            self.channel = Chan()

    pty = SshPty(Proc(), cols=80, rows=24)
    assert pty.is_alive() is False
    assert pty._closed is False


def test_dead_ssh_screen_send_fails_fast_with_dead() -> None:
    """After channel death, send returns DEAD/session id in <1s (no 30s wait)."""
    import time

    class DeadAfterOpenProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None
            self._chunks = [b"mock-shell$\r\n"]
            self._dead = False

        def write(self, data: bytes) -> None:
            if self._dead:
                raise _ConnectionLost("connection lost")
            return None

        async def read(self, n: int = 8192) -> bytes:
            if self._dead:
                raise _ConnectionLost("connection lost")
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self) -> None:
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    procs: list[DeadAfterOpenProc] = []

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> DeadAfterOpenProc:
            p = DeadAfterOpenProc()
            procs.append(p)
            return p

        def close(self) -> None:
            return None

    reg = get_registry()
    reg.ssh_connector = lambda **_k: MockConn()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        settle_s=0.1,
        cols=80,
        rows=24,
    )
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]
    assert procs, "create_process should have run"
    # Kill the PTY process actually bound to the session (open may have run
    # collect_probe create_process first - procs[0] is not always the screen).
    from mcp_remote_control.screen.registry import get_screen_registry

    sess = get_screen_registry().get(sid)
    assert sess is not None
    bound = getattr(sess.pty, "_process", None)
    assert bound is not None
    bound._dead = True  # type: ignore[attr-defined]

    t0 = time.monotonic()
    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "text", "text": "pwd", "submit": True}],
        wait={"until": "idle", "idle_ms": 200, "timeout_ms": 30_000},
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"dead send hung for {elapsed:.2f}s (expected <1s)"
    text = send.render_text()
    # status=dead or error with DEAD in msg; session id always present.
    assert sid in text or send.fields.get("id") == sid
    combined = text + " " + str(send.code or "") + " " + str(send.fields.get("msg") or "")
    assert (
        send.status == "dead"
        or "DEAD" in combined
        or "dead" in combined.lower()
    ), combined
    # PTY under the session must report not alive.
    sess2 = get_screen_registry().get(sid)
    if sess2 is not None:
        assert sess2.pty.is_alive() is False
    screen_ops.close_screen(id=sid)


def test_dead_ssh_screen_until_text_fails_fast() -> None:
    """until=text on a dead SSH channel returns DEAD well under the wait timeout."""
    import time

    class DeadReadProc:
        def __init__(self) -> None:
            self.stdin = self
            self.stdout = self
            self.exit_status: int | None = None
            self._chunks = [b"mock-shell$\r\n"]
            self._dead = False

        def write(self, data: bytes) -> None:
            if self._dead:
                raise _ConnectionLost("connection lost")
            return None

        async def read(self, n: int = 8192) -> bytes:
            if self._dead:
                raise _ConnectionLost("connection lost")
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self) -> None:
            self.exit_status = 0

        def terminate(self) -> None:
            self.exit_status = 0

        def wait(self) -> None:
            return None

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> DeadReadProc:
            return DeadReadProc()

        def close(self) -> None:
            return None

    reg = get_registry()
    reg.ssh_connector = lambda **_k: MockConn()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        settle_s=0.1,
        cols=80,
        rows=24,
    )
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]
    from mcp_remote_control.screen.registry import get_screen_registry

    sess = get_screen_registry().get(sid)
    assert sess is not None
    bound = getattr(sess.pty, "_process", None)
    assert bound is not None
    bound._dead = True  # type: ignore[attr-defined]
    # Observe death on the next read so is_alive is False before wait.
    sess.pty.read()
    assert sess.pty.is_alive() is False

    t0 = time.monotonic()
    send = screen_ops.send_screen(
        id=sid,
        actions=[],
        wait={"until": "text", "text": "NEVER_APPEARS", "timeout_ms": 30_000},
        shot=True,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"until=text dead wait hung for {elapsed:.2f}s"
    assert send.status == "dead" or send.status == "error"
    assert send.status != "ok"
    screen_ops.close_screen(id=sid)


# ---------------------------------------------------------------------------
# Interactive SSH open must cd to work_cwd; probe fail is not confirmation
# ---------------------------------------------------------------------------


class _RecordProc:
    def __init__(self) -> None:
        self.stdin = self
        self.stdout = self
        self.exit_status: int | None = None
        self._chunks = [b"mock-shell$\r\n"]

    def write(self, data: bytes) -> None:
        return None

    async def read(self, n: int = 8192) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self) -> None:
        self.exit_status = 0

    def terminate(self) -> None:
        self.exit_status = 0

    def wait(self) -> None:
        return None


def test_ssh_interactive_create_process_includes_cd() -> None:
    """Default shell (no command/argv) still wraps create_process with cd."""
    from mcp_remote_control.screen.ssh_pty import SshPty

    recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> _RecordProc:
            recorded.append((args, dict(kwargs)))
            return _RecordProc()

    pty = SshPty.open_shell(MockConn(), cols=80, rows=24, cwd="/work/dir")
    assert recorded, "create_process should have run"
    args, kwargs = recorded[0]
    cmd = args[0] if args else kwargs.get("command")
    assert cmd is not None
    text = str(cmd)
    assert "cd" in text
    assert "/work/dir" in text
    pty.close()


def test_ssh_open_probe_fail_does_not_claim_unconfirmed_cwd() -> None:
    """Probe failure must not advertise the requested cwd as live/applied."""
    from mcp_remote_control.screen.registry import get_screen_registry

    recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class MockConn:
        def create_process(self, *args: object, **kwargs: object) -> _RecordProc:
            recorded.append((args, dict(kwargs)))
            return _RecordProc()

        def close(self) -> None:
            return None

    requested = "/opt/unconfirmed"
    reg = get_registry()
    reg.ssh_connector = lambda **_k: MockConn()
    r = screen_ops.open_screen(
        ep="lab-ssh",
        home=_CONFIG_HOME,
        cwd=requested,
        settle_s=0.1,
        cols=80,
        rows=24,
    )
    assert r.status == "ok", r.render_text()

    pty_cmds = []
    for args, kwargs in recorded:
        if "term_type" in kwargs or "term_size" in kwargs:
            cmd = args[0] if args else kwargs.get("command")
            pty_cmds.append(cmd)
    assert pty_cmds, "PTY create_process should have run"
    assert any(
        c is not None and "cd" in str(c) and requested in str(c) for c in pty_cmds
    ), pty_cmds

    assert r.fields.get("cwd_src") != "probe", r.render_text()
    assert r.cwd != requested
    header = (r.render_text() or "").splitlines()[0] if r.render_text() else ""
    assert f"cwd={requested}" not in header
    sid = r.fields["id"]
    sess = get_screen_registry().get(sid)
    assert sess is not None
    assert sess.cwd_src != "probe"
    assert sess.cwd != requested
    assert screen_ops.close_screen(id=sid).status == "ok"
