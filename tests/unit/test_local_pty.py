"""Unit tests for LocalPty close / waitpid timeout and lazy POSIX load.

Covers:
- close() never uses blocking waitpid(pid, 0); hung children return within budget
- normal exit still records _exit_code
- live children killed by SIGTERM/SIGKILL still reap with exit/signal code
- no top-level fcntl/termios/pty; mcp_server import without POSIX tty modules
- local screen open on win32 -> UNSUPPORTED
- concurrent close+drain must not read recycled master fd
"""

from __future__ import annotations

import ast
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from mcp_remote_control.screen import local_pty as local_pty_mod
from mcp_remote_control.screen.local_pty import (
    DEFAULT_WAITPID_TIMEOUT_S,
    LocalPty,
    _status_to_exit_code,
)

_SIMPLE_SHELL = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
_CODE_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalPty is POSIX-only (fork/PTY)",
)


def _bare_pty(*, pid: int = 4242) -> LocalPty:
    """Minimal LocalPty without forking (for waitpid/kill monkeypatches)."""
    pty = object.__new__(LocalPty)
    pty.cols = 80
    pty.rows = 24
    pty.cwd = "/"
    pty._pid = pid
    pty._master = None
    pty._closed = False
    pty._exit_code = None
    pty._io_lock = threading.Lock()
    pty._fd_gen = 0
    return pty


def test_default_waitpid_timeout_is_two_seconds() -> None:
    assert DEFAULT_WAITPID_TIMEOUT_S == 2.0


def test_status_to_exit_code_helpers() -> None:
    child = os.fork()
    if child == 0:
        os._exit(3)
    _done, status = os.waitpid(child, 0)
    assert _status_to_exit_code(status) == 3

    child2 = os.fork()
    if child2 == 0:
        time.sleep(30)
        os._exit(0)
    os.kill(child2, signal.SIGTERM)
    _done2, status2 = os.waitpid(child2, 0)
    assert _status_to_exit_code(status2) == -signal.SIGTERM


def test_close_returns_within_budget_when_waitpid_never_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stuck D-state child: waitpid WNOHANG never reaps -> close must not hang."""
    budget = 0.25
    monkeypatch.setattr(local_pty_mod, "DEFAULT_WAITPID_TIMEOUT_S", budget)
    monkeypatch.setattr(local_pty_mod, "_WAITPID_POLL_INTERVAL_S", 0.02)

    flags_seen: list[int] = []

    def never_reap(pid: int, flags: int = 0) -> tuple[int, int]:
        flags_seen.append(flags)
        # Blocking waitpid would hang forever; must not be used.
        if flags == 0:
            raise AssertionError("blocking waitpid(pid, 0) is forbidden after I9")
        return (0, 0)

    monkeypatch.setattr(os, "waitpid", never_reap)
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)

    pty = _bare_pty(pid=99901)
    t0 = time.monotonic()
    pty.close()
    elapsed = time.monotonic() - t0

    assert pty._closed is True
    assert pty.exit_code() is None, "unreaped child must leave _exit_code unset"
    assert elapsed < budget + 0.5, f"close hung too long: {elapsed:.3f}s"
    assert elapsed >= budget * 0.5, f"close returned too early: {elapsed:.3f}s"
    assert flags_seen, "waitpid must have been polled"
    assert all(f == os.WNOHANG for f in flags_seen), flags_seen


def test_close_logs_warning_on_waitpid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(local_pty_mod, "DEFAULT_WAITPID_TIMEOUT_S", 0.15)
    monkeypatch.setattr(local_pty_mod, "_WAITPID_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(os, "waitpid", lambda *_a, **_k: (0, 0))
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)

    pty = _bare_pty(pid=99902)
    with caplog.at_level("WARNING", logger=local_pty_mod.__name__):
        pty.close()
    assert any(
        "unreaped" in r.message or "abandoning" in r.message for r in caplog.records
    )


def test_close_sets_exit_code_when_waitpid_reaps_after_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGKILL path reaps on poll -> _exit_code from status."""
    child = os.fork()
    if child == 0:
        time.sleep(30)
        os._exit(0)
    try:
        os.kill(child, signal.SIGKILL)
        _done, status = os.waitpid(child, 0)
    except OSError:
        pytest.skip("could not fabricate SIGKILL wait status")

    calls = {"n": 0}

    def reap_once(pid: int, flags: int = 0) -> tuple[int, int]:
        calls["n"] += 1
        if flags == 0:
            raise AssertionError("blocking waitpid forbidden")
        # First WNOHANGs (SIGTERM grace): still alive; later: reaped.
        if calls["n"] <= 2:
            return (0, 0)
        return (pid, status)

    monkeypatch.setattr(os, "waitpid", reap_once)
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)
    monkeypatch.setattr(local_pty_mod, "_WAITPID_POLL_INTERVAL_S", 0.01)

    pty = _bare_pty(pid=77777)
    pty.close()
    assert pty.exit_code() == -signal.SIGKILL


def test_close_sigterm_reap_skips_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Child dies on SIGTERM -> exit_code set; SIGKILL not required."""
    killed: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append(sig)

    child = os.fork()
    if child == 0:
        os._exit(0)
    _done, status = os.waitpid(child, 0)

    def reap_first(pid: int, flags: int = 0) -> tuple[int, int]:
        if flags == 0:
            raise AssertionError("blocking waitpid forbidden")
        return (pid, status)

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "waitpid", reap_first)

    pty = _bare_pty(pid=111)
    pty.close()
    assert pty.exit_code() == 0
    assert killed == [signal.SIGTERM]


def test_close_already_exited_keeps_exit_code() -> None:
    """Real child: natural exit, then close is a no-op on wait and keeps code."""
    pty = LocalPty(
        cols=40,
        rows=10,
        argv=["/bin/sh", "-c", "exit 7"],
        shell=_SIMPLE_SHELL,
    )
    try:
        deadline = time.monotonic() + 3.0
        while pty.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not pty.is_alive(), "child should have exited"
        assert pty.exit_code() == 7
        t0 = time.monotonic()
        pty.close()
        assert time.monotonic() - t0 < 1.0
        assert pty.exit_code() == 7
    finally:
        if not pty._closed:
            pty.close()


def test_close_live_child_sets_exit_code_quickly() -> None:
    """Live sleep child: close SIGTERM/SIGKILL reaps without hanging."""
    pty = LocalPty(
        cols=40,
        rows=10,
        argv=["/bin/sh", "-c", "exec sleep 60"],
        shell=_SIMPLE_SHELL,
    )
    try:
        assert pty.is_alive()
        t0 = time.monotonic()
        pty.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, f"close too slow for live child: {elapsed:.3f}s"
        code = pty.exit_code()
        assert code is not None, "normal kill path must set _exit_code"
        # SIGTERM (-15) or SIGKILL (-9) depending on who reaped first.
        assert code in (-signal.SIGTERM, -signal.SIGKILL) or code < 0
    finally:
        if not pty._closed:
            pty.close()


def test_close_idempotent() -> None:
    pty = LocalPty(
        cols=40,
        rows=10,
        argv=["/bin/sh", "-c", "exit 0"],
        shell=_SIMPLE_SHELL,
    )
    pty.close()
    pty.close()
    assert pty._closed is True


# ---------------------------------------------------------------------------
# close / drain master-fd race (lock + generation)
# ---------------------------------------------------------------------------


def _make_nonblock_pipe() -> tuple[int, int]:
    """Return (read_fd, write_fd) with the read end non-blocking."""
    import fcntl

    r, w = os.pipe()
    flags = fcntl.fcntl(r, fcntl.F_GETFL)
    fcntl.fcntl(r, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return r, w


def test_close_bumps_fd_generation_and_nulls_master() -> None:
    r, w = _make_nonblock_pipe()
    try:
        pty = _bare_pty()
        pty._pid = None
        pty._master = r
        r = -1  # ownership transferred; close() will os.close it
        gen0 = pty._fd_gen
        pty.close()
        assert pty._closed is True
        assert pty._master is None
        assert pty._fd_gen == gen0 + 1
        assert pty.master_fd is None
        # Second close is a no-op (generation stays).
        gen1 = pty._fd_gen
        pty.close()
        assert pty._fd_gen == gen1
    finally:
        if r >= 0:
            try:
                os.close(r)
            except OSError:
                pass
        try:
            os.close(w)
        except OSError:
            pass


def test_read_write_after_close_safe() -> None:
    r, w = _make_nonblock_pipe()
    try:
        pty = _bare_pty()
        pty._pid = None
        pty._master = r
        r = -1
        os.write(w, b"hello")
        assert pty.read() == b"hello"
        pty.close()
        assert pty.read() == b""
        with pytest.raises(Exception) as ei:
            pty.write(b"x")
        # TransportError NOT_CONNECTED
        assert "closed" in str(ei.value).lower() or getattr(
            ei.value, "code", None
        ) == "NOT_CONNECTED"
    finally:
        if r >= 0:
            try:
                os.close(r)
            except OSError:
                pass
        try:
            os.close(w)
        except OSError:
            pass


def test_concurrent_close_and_drain_no_exceptions() -> None:
    """Concurrent close + drain_for must not raise or hang."""
    pty = LocalPty(
        cols=40,
        rows=10,
        argv=["/bin/sh", "-c", "while true; do echo tick; sleep 0.01; done"],
        shell=_SIMPLE_SHELL,
    )
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def drain_loop() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(40):
                pty.drain_for(0.05)
        except BaseException as exc:  # noqa: BLE001 - collect any race fault
            errors.append(exc)

    def closer() -> None:
        try:
            barrier.wait(timeout=5)
            time.sleep(0.03)
            pty.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_drain = threading.Thread(target=drain_loop, name="o4-drain")
    t_close = threading.Thread(target=closer, name="o4-close")
    t_drain.start()
    t_close.start()
    t_drain.join(timeout=10)
    t_close.join(timeout=10)
    assert not t_drain.is_alive(), "drain thread hung"
    assert not t_close.is_alive(), "close thread hung"
    assert errors == [], f"concurrent close/drain errors: {errors!r}"
    assert pty._closed is True
    assert pty._master is None


def test_concurrent_close_and_read_no_exceptions() -> None:
    """Concurrent close + read must not raise."""
    pty = LocalPty(
        cols=40,
        rows=10,
        argv=["/bin/sh", "-c", "while true; do printf x; sleep 0.01; done"],
        shell=_SIMPLE_SHELL,
    )
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                pty.read(4096)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=reader, name="o4-read")
    t.start()
    time.sleep(0.05)
    try:
        pty.close()
    finally:
        stop.set()
        t.join(timeout=5)
    assert not t.is_alive()
    assert errors == [], f"concurrent close/read errors: {errors!r}"
    assert pty.read() == b""


def test_drain_rejects_recycled_fd_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After close, stale drain must not os.read a recycled fd number.

    Simulates: drain snapshots master fd N, close nulls+closes N and bumps
    generation, OS reuses N for a new pipe holding POISON. Generation check
    must prevent reading POISON into the drain total / on_data callback.
    """
    import fcntl

    r_old, w_old = _make_nonblock_pipe()
    poison_fds: list[int] = []
    pty = _bare_pty()
    pty._pid = None
    pty._master = r_old
    old_fd = r_old
    try:
        entered_select = threading.Event()
        close_done = threading.Event()
        poison_written = threading.Event()

        real_select = select.select

        def select_then_recycle(
            rlist: list,
            wlist: list,
            xlist: list,
            timeout: float | None = None,
        ) -> tuple[list, list, list]:
            # Only intercept the drain's first wait on the old master fd.
            if list(rlist) == [old_fd] and not entered_select.is_set():
                entered_select.set()
                # Let close run while we "block" in select.
                assert close_done.wait(timeout=5), "close did not run"
                # close() already os.close'd old_fd; reopen that number with
                # a pipe carrying poison so a naive os.read(old_fd) would see it.
                r_new, w_new = os.pipe()
                poison_fds.append(w_new)
                if r_new != old_fd:
                    os.dup2(r_new, old_fd)
                    os.close(r_new)
                    r_new = old_fd
                poison_fds.append(r_new)
                flags = fcntl.fcntl(r_new, fcntl.F_GETFL)
                fcntl.fcntl(r_new, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                os.write(w_new, b"POISON-RECYCLED-FD")
                poison_written.set()
                # Report ready so drain proceeds to the post-select read path.
                return ([old_fd], [], [])
            return real_select(rlist, wlist, xlist, timeout)

        monkeypatch.setattr(local_pty_mod.select, "select", select_then_recycle)

        collected: list[bytes] = []
        errors: list[BaseException] = []

        def drain_worker() -> None:
            try:
                n = pty.drain_for(2.0, on_data=collected.append)
                # Stale generation must yield no poison bytes.
                assert n == 0
                assert b"POISON" not in b"".join(collected)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=drain_worker, name="o4-stale-drain")
        t.start()
        assert entered_select.wait(timeout=5), "drain never entered select"
        # close while drain is "in" select with old_fd snapshotted.
        pty.close()
        close_done.set()
        t.join(timeout=10)
        assert not t.is_alive()
        assert errors == [], f"drain errors: {errors!r}"
        assert collected == []
        assert pty._master is None
        assert pty._fd_gen >= 1
        # Ensure poison was actually present on the recycled number (else weak).
        assert poison_written.is_set()
        # Sanity: recycled fd really holds poison if read without gen check.
        try:
            leaked = os.read(old_fd, 64)
        except OSError:
            leaked = b""
        assert leaked == b"POISON-RECYCLED-FD" or poison_written.is_set()
    finally:
        for fd in set(poison_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(w_old)
        except OSError:
            pass
        if not pty._closed:
            try:
                os.close(r_old)
            except OSError:
                pass


def test_snapshot_master_invalid_after_close() -> None:
    r, w = _make_nonblock_pipe()
    try:
        pty = _bare_pty()
        pty._pid = None
        pty._master = r
        r = -1
        fd, gen = pty._snapshot_master()
        assert fd is not None
        # The live fence drain/read use: a snapshot is usable only while the
        # master fd and its generation still match.
        assert pty._master == fd and pty._fd_gen == gen
        pty.close()
        assert not (pty._master == fd and pty._fd_gen == gen)
        fd2, gen2 = pty._snapshot_master()
        assert fd2 is None
        assert gen2 == gen + 1
    finally:
        if r >= 0:
            try:
                os.close(r)
            except OSError:
                pass
        try:
            os.close(w)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Lazy POSIX imports / Windows local screen UNSUPPORTED
# ---------------------------------------------------------------------------


def test_local_pty_has_no_toplevel_posix_tty_imports() -> None:
    """fcntl/termios/pty must not be module-level imports."""
    src = Path(local_pty_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = frozenset({"fcntl", "termios", "pty"})
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden, f"top-level import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                assert root not in forbidden, f"top-level from {node.module}"


def test_screen_ops_does_not_eager_import_local_pty() -> None:
    """screen_ops must not bind LocalPty at import time (lazy in _open_pty)."""
    from mcp_remote_control.core import screen_ops

    assert not hasattr(screen_ops, "LocalPty")
    src = Path(screen_ops.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == (
            "mcp_remote_control.screen.local_pty"
        ):
            # Only allowed under TYPE_CHECKING if/for blocks - not module body.
            pytest.fail("eager from \u2026local_pty import at module body")


def test_mcp_server_imports_without_fcntl_termios() -> None:
    """import mcp_server succeeds when fcntl/termios are unavailable.

    Simulates a Windows-like host for *our* packages: the MCP SDK on POSIX may
    already have pulled fcntl into site-packages; we load that first, then
    block fcntl/termios and require mrc (screen_ops -> was eager local_pty) to
    import without them - the regression that blocked win32 service start.
    """
    # Fresh subprocess so blocking __import__ cannot poison the pytest process.
    script = textwrap.dedent(
        """
        import builtins
        import sys

        # Warm MCP SDK (may use fcntl on POSIX stdio) before blocking.
        import mcp.server.mcpserver  # noqa: F401

        _real = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".", 1)[0]
            if root in ("fcntl", "termios"):
                raise ImportError(f"N1 mock: no {root} on this platform")
            return _real(name, globals, locals, fromlist, level)

        builtins.__import__ = _blocked
        for key in list(sys.modules):
            if key in ("fcntl", "termios") or key.startswith(
                ("fcntl.", "termios.", "mcp_remote_control")
            ):
                del sys.modules[key]

        import mcp_remote_control.mcp_server as ms
        import mcp_remote_control.core.screen_ops as screen_ops
        import mcp_remote_control.screen.local_pty as local_pty

        assert hasattr(ms, "TOOL_NAMES")
        assert "screen" in ms.TOOL_NAMES
        assert hasattr(screen_ops, "open_screen")
        assert hasattr(local_pty, "LocalPty")
        print("import-ok")
        """
    )
    env = dict(os.environ)
    # Prefer package under code-root/src (editable install may already cover this).
    src = str(_CODE_ROOT / "src")
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")
    env.setdefault("MRC_HOME", str(_CODE_ROOT / "tests" / "fixtures" / "config"))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"mcp_server import failed without fcntl/termios\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "import-ok" in proc.stdout


def test_open_pty_local_win32_raises_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local transport on win32 -> TransportError UNSUPPORTED (no LocalPty)."""
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.transport.base import TransportError

    monkeypatch.setattr(screen_ops.sys, "platform", "win32")

    class _LocalTransport:
        name = "local"

        def is_connected(self) -> bool:
            return True

    with pytest.raises(TransportError) as ei:
        screen_ops._open_pty(
            _LocalTransport(),  # type: ignore[arg-type]
            cols=80,
            rows=24,
            cwd=None,
            shell=None,
            command=None,
            argv=None,
            env={},
        )
    assert ei.value.code == "UNSUPPORTED"
    assert "Windows" in ei.value.msg or "win" in ei.value.msg.lower()


def test_open_screen_local_win32_returns_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_screen on local ep under win32 -> OpResult code=UNSUPPORTED."""
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.screen.registry import (
        get_screen_registry,
        reset_screen_registry,
    )

    monkeypatch.setattr(screen_ops.sys, "platform", "win32")
    reset_screen_registry()

    class _FakeEndpoint:
        transport_name = "local"
        caps = {"screen": True, "exec": True, "fs": True}
        caps_token = "e+f+s"
        cwd = "/"
        profile = None
        probe: dict = {}
        transport = type(
            "T",
            (),
            {
                "name": "local",
                "is_connected": lambda self: True,
                "cwd": "/",
                "home": None,
                "meta": {},
            },
        )()

    def _fake_ensure(ep: str, **_kw: object) -> _FakeEndpoint:
        assert ep == "local-win"
        return _FakeEndpoint()

    monkeypatch.setattr(
        "mcp_remote_control.core.screen_ops.ensure_endpoint",
        _fake_ensure,
    )

    r = screen_ops.open_screen(ep="local-win", settle_s=0.0)
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert get_screen_registry().list_open() == []


def test_open_pty_ssh_still_importable_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH PTY path remains importable; win32 gate is local-only."""
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.transport.base import TransportError

    monkeypatch.setattr(screen_ops.sys, "platform", "win32")

    class _SshTransport:
        name = "ssh"
        connection = None
        _conn = None

        def is_connected(self) -> bool:
            return True

    # No connection -> NOT_CONNECTED (not win32 UNSUPPORTED).
    with pytest.raises(TransportError) as ei:
        screen_ops._open_pty(
            _SshTransport(),  # type: ignore[arg-type]
            cols=80,
            rows=24,
            cwd=None,
            shell=None,
            command=None,
            argv=None,
            env={},
        )
    assert ei.value.code == "NOT_CONNECTED"


class _LocalCwdTransport:
    name = "local"
    cwd: str | None = None
    home: str | None = None


def test_resolve_open_cwd_local_missing_raises_invalid_cwd() -> None:
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.transport.base import TransportError

    with pytest.raises(TransportError) as ei:
        screen_ops._resolve_open_cwd(
            requested="/definitely/not/here/mrc-screen-cwd",
            endpoint_cwd=None,
            transport=_LocalCwdTransport(),  # type: ignore[arg-type]
        )
    assert ei.value.code == "INVALID_CWD"
    assert ei.value.details.get("cwd")


def test_resolve_open_cwd_local_file_raises_invalid_cwd(tmp_path: Path) -> None:
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.transport.base import TransportError

    target = tmp_path / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(TransportError) as ei:
        screen_ops._resolve_open_cwd(
            requested=str(target),
            endpoint_cwd=None,
            transport=_LocalCwdTransport(),  # type: ignore[arg-type]
        )
    assert ei.value.code == "INVALID_CWD"
    details = str(ei.value.details.get("cwd") or "")
    assert Path(details).resolve() == target.resolve()


def test_resolve_open_cwd_local_existing_dir(tmp_path: Path) -> None:
    from mcp_remote_control.core import screen_ops

    resolved = screen_ops._resolve_open_cwd(
        requested=str(tmp_path),
        endpoint_cwd=None,
        transport=_LocalCwdTransport(),  # type: ignore[arg-type]
    )
    assert resolved is not None
    assert Path(resolved).resolve() == tmp_path.resolve()


def test_resolve_open_cwd_local_missing_seed_falls_back_to_getcwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profile/transport seed may skip a missing dir; explicit cwd must not."""
    from mcp_remote_control.core import screen_ops

    monkeypatch.chdir(tmp_path)
    transport = _LocalCwdTransport()
    transport.cwd = str(tmp_path / "missing-seed")
    resolved = screen_ops._resolve_open_cwd(
        requested=None,
        endpoint_cwd=str(tmp_path / "missing-endpoint"),
        transport=transport,  # type: ignore[arg-type]
    )
    assert Path(resolved).resolve() == tmp_path.resolve()
