"""Unit tests for LocalTransport connect/run/timeout/cwd/killpg."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from mcp_remote_control.transport import (
    LocalTransport,
    TransportError,
)


def test_local_connect_close() -> None:
    t = LocalTransport()
    assert t.name == "local"
    assert t.is_connected() is False
    t.connect()
    assert t.is_connected() is True
    assert t.cwd is not None
    t.close()
    assert t.is_connected() is False


def test_local_run_command_and_argv() -> None:
    t = LocalTransport()
    t.connect()
    r = t.run_command("echo unit-cmd")
    assert r.exit_code == 0
    assert "unit-cmd" in r.stdout
    assert r.cwd is not None
    r2 = t.run_argv(["/bin/echo", "unit-argv"])
    assert r2.exit_code == 0
    assert "unit-argv" in r2.stdout


def test_local_run_timeout() -> None:
    t = LocalTransport()
    t.connect()
    r = t.run_command("sleep 5", timeout_s=0.15)
    assert r.timed_out is True
    assert r.exit_code == -1


def test_local_run_non_timeout_exception_kills_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-TimeoutExpired failure after Popen must kill the tree.

    Regression: communicate(timeout=nan) raised ValueError and left the
    child running because only TimeoutExpired called _kill_process_tree.
    """
    from mcp_remote_control.transport import local as local_mod

    class _Pipe:
        def close(self) -> None:
            self.closed = True

        def __init__(self) -> None:
            self.closed = False

    class FakeProc:
        pid = 7777
        returncode = None

        def __init__(self) -> None:
            self.stdout = _Pipe()
            self.stderr = _Pipe()
            self.stdin = _Pipe()
            self.wait_calls: list[float | None] = []

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            del timeout
            raise ValueError("timeout must be a non-negative number (got nan)")

        def wait(self, timeout: float | None = None) -> int | None:
            self.wait_calls.append(timeout)
            self.returncode = -9
            return self.returncode

    fake = FakeProc()
    kill_targets: list[int] = []

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProc:
        return fake

    def track_kill(self: LocalTransport, proc: object) -> None:
        del self
        pid = getattr(proc, "pid", None)
        if pid is not None:
            kill_targets.append(int(pid))

    monkeypatch.setattr(local_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(LocalTransport, "_kill_process_tree", track_kill)

    t = LocalTransport()
    t.connect()
    with pytest.raises(ValueError, match="nan"):
        t.run_command("sleep 30", timeout_s=float("nan"))

    assert kill_targets == [7777], (
        f"_kill_process_tree must run on non-timeout exception; got {kill_targets}"
    )
    assert fake.stdout.closed and fake.stderr.closed and fake.stdin.closed
    assert fake.wait_calls, "proc.wait should reap after kill"


def test_local_run_timeout_still_kills_without_double_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired path still kills once and returns timed_out=True."""
    from mcp_remote_control.transport import local as local_mod

    class FakeProc:
        pid = 8888
        returncode = None
        stdout = None
        stderr = None
        stdin = None

        def __init__(self) -> None:
            self._n = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            self._n += 1
            if self._n == 1:
                raise local_mod.subprocess.TimeoutExpired(cmd="sleep", timeout=timeout)
            return b"partial\n", b""

        def wait(self, timeout: float | None = None) -> int | None:
            del timeout
            return self.returncode

    kill_count = [0]

    def track_kill(self: LocalTransport, proc: object) -> None:
        del self, proc
        kill_count[0] += 1

    monkeypatch.setattr(
        local_mod.subprocess, "Popen", lambda *_a, **_k: FakeProc()
    )
    monkeypatch.setattr(LocalTransport, "_kill_process_tree", track_kill)

    t = LocalTransport()
    t.connect()
    r = t.run_command("sleep 99", timeout_s=0.01)
    assert r.timed_out is True
    assert r.exit_code == -1
    assert kill_count[0] == 1
    assert "partial" in r.stdout


def test_local_timeout_kills_process_group(tmp_path: Path) -> None:
    """A timeout must kill the whole process group so backgrounded children
    (grandchildren of the shell) do not survive the call."""
    if os.name != "posix":
        pytest.skip("process-group kill is POSIX-only")
    pidfile = tmp_path / "child.pid"
    t = LocalTransport()
    t.connect()
    # Background a long-lived sleep, publish its PID, then block the shell in
    # `wait` so the exec timeout fires while the shell is still alive.
    cmd = f"sleep 30 & echo $! > {pidfile}; wait"
    r = t.run_command(cmd, timeout_s=1.0)
    assert r.timed_out is True
    assert r.exit_code == -1
    # The backgrounded child must not survive the process-group kill.
    assert pidfile.exists()
    child_pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 2.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        except PermissionError:
            alive = False  # reaped / no longer ours
            break
        time.sleep(0.05)
    assert not alive, f"child sleep pid {child_pid} survived timeout"


def test_local_resolve_cwd_idempotent_fast_path(tmp_path: Path) -> None:
    """An already-absolute existing dir is returned without re-resolution."""
    if os.name != "posix":
        pytest.skip("symlink fast-path check is POSIX-only")
    from mcp_remote_control.transport.local import _resolve_local_cwd

    real = str(tmp_path)
    # tmp_path is already absolute and exists -> returned verbatim.
    assert _resolve_local_cwd(real) == real
    # None -> getcwd (still absolute + existing).
    resolved = Path(_resolve_local_cwd(None))
    assert resolved.is_absolute() and resolved.is_dir()
    # Nonexistent absolute path still raises INVALID_CWD.
    with pytest.raises(TransportError) as ei:
        _resolve_local_cwd("/definitely/not/here/mrc-o5")
    assert ei.value.code == "INVALID_CWD"


# ---------------------------------------------------------------------------
# _resolve_local_cwd fast/slow-path consistency (both use os.path.abspath,
# neither calls Path.resolve) + Windows taskkill tree-kill.
# ---------------------------------------------------------------------------


def test_local_resolve_cwd_fast_slow_paths_use_abspath_no_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fast and slow paths of _resolve_local_cwd must both return the lexical
    os.path.abspath form and never call Path.resolve (no symlink-following).

    This keeps them consistent: an already-absolute existing dir returned by
    exec_ops._resolve_cwd is a no-op when handed back through run_command,
    and a relative-path resolution does not silently canonicalize through
    a symlinked parent (e.g. /var -> /private/var on macOS).
    """
    if os.name != "posix":
        pytest.skip("symlink semantics checked on POSIX only")
    from mcp_remote_control.transport import local as local_mod
    from mcp_remote_control.transport.local import _resolve_local_cwd

    resolve_calls = [0]
    orig_resolve = Path.resolve

    def counting_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        resolve_calls[0] += 1
        return orig_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    real = str(tmp_path)
    # Fast path: already-absolute existing dir.
    fast = _resolve_local_cwd(real)
    assert fast == os.path.abspath(real)
    # Slow path: a relative "." after chdir into tmp_path forces the slow
    # branch (the input itself is not absolute, so the fast path's
    # is_absolute() check fails).
    cwd_prev = os.getcwd()
    try:
        os.chdir(real)
        slow = _resolve_local_cwd(".")
    finally:
        os.chdir(cwd_prev)
    assert Path(slow).is_absolute() and Path(slow).is_dir()
    # On macOS where /var -> /private/var, the lexical abspath of `real` is
    # /var/... (NOT the resolved /private/var/...). The slow path must
    # produce the same form as the fast path for the same dir.
    assert fast == os.path.abspath(real)
    assert resolve_calls[0] == 0, (
        f"_resolve_local_cwd called Path.resolve {resolve_calls[0]} times; "
        "both paths should use os.path.abspath (no symlink-following)"
    )
    # _kill_process_tree is unaffected by this monkeypatch; sanity reference
    # to keep the import meaningful even when the assertion above is trivial.
    assert hasattr(local_mod.LocalTransport, "_kill_process_tree")


def test_local_kill_tree_windows_uses_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows (os.name != 'posix'), _kill_process_tree must invoke
    `taskkill /F /T /PID` to kill the whole process tree. A plain
    proc.kill() only kills the immediate child shell - grandchildren of
    the shell (e.g. `sleep 3600 &`) survive. /T walks the tree.
    """
    from mcp_remote_control.transport import local as local_mod

    calls: list[list[str]] = []

    class _FakeRunResult:
        returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> _FakeRunResult:
        calls.append(list(args))
        return _FakeRunResult()

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(local_mod.os, "name", "nt")
    monkeypatch.setattr(local_mod.subprocess, "run", fake_run)

    t = local_mod.LocalTransport()
    t._kill_process_tree(FakeProc())  # type: ignore[arg-type]

    assert len(calls) == 1, f"taskkill should be called once, got {calls}"
    args = calls[0]
    assert args[0] == "taskkill"
    assert "/F" in args
    assert "/T" in args
    assert "/PID" in args
    assert "4242" in args


def test_local_kill_tree_windows_taskkill_failure_falls_back_to_proc_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If taskkill itself can't be invoked (FileNotFoundError / OSError),
    _kill_process_tree falls back to proc.kill() - best-effort tree-kill so
    at least the shell dies when taskkill is missing from PATH."""
    from mcp_remote_control.transport import local as local_mod

    killed: list[int] = []

    def fake_run(args: list[str], **_kwargs: object) -> object:
        raise FileNotFoundError("taskkill not on PATH")

    class FakeProc:
        pid = 4242

        def kill(self) -> None:
            killed.append(self.pid)

    monkeypatch.setattr(local_mod.os, "name", "nt")
    monkeypatch.setattr(local_mod.subprocess, "run", fake_run)

    t = local_mod.LocalTransport()
    t._kill_process_tree(FakeProc())  # type: ignore[arg-type]

    assert killed == [4242], (
        "proc.kill() should be invoked when taskkill raises OSError"
    )
