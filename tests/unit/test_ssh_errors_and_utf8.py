"""SSH hostkey/auth error mapping, liveness, force_utf8_remote."""

from __future__ import annotations

import asyncio
import time

import pytest

from mcp_remote_control.config import Profile
from mcp_remote_control.endpoint.registry import EndpointRegistry
from mcp_remote_control.transport.async_bridge import AsyncLoopBridge
from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport import ssh as ssh_mod
from mcp_remote_control.transport.ssh import (
    SSHTransport,
    _coerce_exec_result,
    _exit_code_from_signal,
    _map_ssh_connect_error,
    _maybe_force_utf8,
)


def test_map_hostkey_mismatch() -> None:
    class HostKeyNotVerifiable(Exception):
        pass

    err = _map_ssh_connect_error(
        HostKeyNotVerifiable("Host key verification failed"),
        host="h",
        port=22,
    )
    assert err.code == "HOSTKEY_MISMATCH"


def test_map_auth_failed() -> None:
    err = _map_ssh_connect_error(
        PermissionError("Permission denied (publickey)"),
        host="h",
        port=22,
    )
    assert err.code == "AUTH_FAILED"


def test_map_connect_failed_generic() -> None:
    err = _map_ssh_connect_error(
        ConnectionRefusedError("Connection refused"),
        host="h",
        port=22,
    )
    assert err.code == "CONNECT_FAILED"


def test_force_utf8_posix_wrap() -> None:
    t = SSHTransport(
        host="h",
        username="u",
        force_utf8_remote=True,
        remote_shell_family="posix",
        connector=lambda **k: object(),
    )
    out = _maybe_force_utf8("echo hi", t)
    assert "LC_ALL" in out
    assert "echo hi" in out


def test_force_utf8_cmd_wrap() -> None:
    t = SSHTransport(
        host="h",
        username="u",
        force_utf8_remote=True,
        remote_shell_family="cmd",
        connector=lambda **k: object(),
    )
    out = _maybe_force_utf8("dir", t)
    assert "chcp 65001" in out
    assert "dir" in out


def test_force_utf8_false_does_not_wrap() -> None:
    t = SSHTransport(
        host="h",
        username="u",
        force_utf8_remote=False,
        remote_shell_family="posix",
        connector=lambda **k: object(),
    )
    assert _maybe_force_utf8("echo hi", t) == "echo hi"


def _ssh_profile(
    *,
    ssh: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
) -> Profile:
    return Profile(
        name="lab",
        transport="ssh",
        host="h",
        username="u",
        ssh=dict(ssh or {}),
        defaults=dict(defaults or {}),
    )


def _build_ssh_transport(
    *,
    ssh: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
    connector: object | None = None,
) -> SSHTransport:
    transport = EndpointRegistry()._build_transport(
        _ssh_profile(ssh=ssh, defaults=defaults),
        connector=connector if connector is not None else (lambda **_k: object()),
    )
    assert isinstance(transport, SSHTransport)
    return transport


def test_build_transport_honors_explicit_force_utf8_remote_false() -> None:
    t = _build_ssh_transport(
        ssh={"force_utf8_remote": False},
        defaults={"force_utf8_remote": True},
    )
    assert t.force_utf8_remote is False
    assert _maybe_force_utf8("echo hi", t) == "echo hi"


@pytest.mark.parametrize("raw", [False, 0, "false", "0"])
def test_build_transport_honors_force_utf8_alias_false(raw: object) -> None:
    t = _build_ssh_transport(
        ssh={"force_utf8": raw},
        defaults={"force_utf8_remote": True},
    )
    assert t.force_utf8_remote is False
    assert _maybe_force_utf8("echo hi", t) == "echo hi"


def test_build_transport_force_utf8_remote_wins_over_alias() -> None:
    t = _build_ssh_transport(
        ssh={"force_utf8_remote": False, "force_utf8": True},
        defaults={"force_utf8_remote": True},
    )
    assert t.force_utf8_remote is False


def test_build_transport_omitted_falls_back_to_defaults() -> None:
    t = _build_ssh_transport(defaults={"force_utf8_remote": True})
    assert t.force_utf8_remote is True
    out = _maybe_force_utf8("echo hi", t)
    assert "LC_ALL" in out
    assert "echo hi" in out


def test_run_command_skips_utf8_wrap_when_profile_disables() -> None:
    captured: dict[str, str] = {}

    class Conn:
        def is_closing(self) -> bool:
            return False

        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            class Raw:
                exit_status = 0
                stdout = b"ok"
                stderr = b""

            return Raw()

    t = _build_ssh_transport(
        ssh={"force_utf8_remote": False},
        defaults={"force_utf8_remote": True},
        connector=lambda **_k: Conn(),
    )
    t.connect()
    result = t.run_command("echo hi")
    assert result.exit_code == 0
    assert "LC_ALL" not in captured["command"]
    assert "chcp" not in captured["command"].lower()
    assert "echo hi" in captured["command"]


def test_is_alive_and_mark_dead() -> None:
    class Conn:
        def __init__(self) -> None:
            self._closing = False

        def is_closing(self) -> bool:
            return self._closing

    def connector(**_k: object) -> Conn:
        return Conn()

    t = SSHTransport(host="h", username="u", connector=connector)
    t.connect()
    assert t.is_alive() is True
    t.mark_dead("peer_reset")
    assert t.is_connected() is False
    assert t.is_alive() is False


def test_reconnect_after_mark_dead() -> None:
    opens = {"n": 0}
    closes = {"n": 0}

    class Conn:
        def __init__(self) -> None:
            self.dead = False
            self.close_calls = 0

        def is_closing(self) -> bool:
            return self.dead

        def close(self) -> None:
            self.close_calls += 1
            closes["n"] += 1

    def connector(**_k: object) -> Conn:
        opens["n"] += 1
        return Conn()

    t = SSHTransport(host="h", username="u", connector=connector)
    t.connect()
    first = opens["n"]
    first_conn = t.connection
    assert first >= 1
    assert t.is_alive() is True
    t.mark_dead("test")
    assert t.is_alive() is False
    # Fresh connect after mark_dead: new session + best-effort close of old.
    t.connect()
    assert opens["n"] == first + 1
    assert t.is_connected() is True
    # Reconnect must close the prior conn at least once (no zombie FD).
    assert closes["n"] >= 1
    assert first_conn is not None
    assert first_conn.close_calls >= 1
    assert t.connection is not first_conn


def test_happy_path_connect_skips_close() -> None:
    """Idempotent connect while live must not tear down the session (perf)."""
    closes = {"n": 0}

    class Conn:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            closes["n"] += 1

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    conn = t.connection
    t.connect()  # early return - must not dispose
    assert t.connection is conn
    assert closes["n"] == 0
    assert t.is_connected() is True


# ---------------------------------------------------------------------------
# dispose / SFTP teardown must not hang on blackholed wait_closed / exit
# ---------------------------------------------------------------------------


def test_dispose_hung_wait_closed_connect_and_close_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-completing wait_closed must not pin dispose/connect/close forever.

    timeout_s=None would make Future.result() unbounded.
    """
    monkeypatch.setattr(ssh_mod, "_DISPOSE_TIMEOUT_S", 0.25)

    class HangConn:
        def __init__(self) -> None:
            self.close_calls = 0

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            self.close_calls += 1

        def wait_closed(self) -> object:
            async def hang() -> None:
                await asyncio.sleep(3600)

            return hang()

    bridge = AsyncLoopBridge()
    bridge.start()
    try:
        t = SSHTransport(
            host="h",
            username="u",
            connector=lambda **_k: HangConn(),
            bridge=bridge,
        )
        t.connect()
        first = t.connection
        t.mark_dead("blackhole")

        t0 = time.monotonic()
        t.connect()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"reconnect dispose hung: {elapsed:.2f}s"
        assert t.is_connected() is True
        assert t.connection is not first
        assert first is not None
        assert first.close_calls >= 1

        t0 = time.monotonic()
        t.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"close dispose hung: {elapsed:.2f}s"
        assert t.is_connected() is False
        assert t.connection is None
    finally:
        bridge.stop()


def test_clear_sftp_hung_exit_invalidate_and_dispose_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-completing SFTP exit/close must not block invalidate/close forever."""
    monkeypatch.setattr(ssh_mod, "_DISPOSE_TIMEOUT_S", 0.25)

    class HangSftp:
        def exit(self) -> object:
            async def hang() -> None:
                await asyncio.sleep(3600)

            return hang()

    class Conn:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

        def wait_closed(self) -> object:
            async def done() -> None:
                return None

            return done()

    bridge = AsyncLoopBridge()
    bridge.start()
    try:
        t = SSHTransport(
            host="h",
            username="u",
            connector=lambda **_k: Conn(),
            bridge=bridge,
        )
        t.connect()
        t._sftp = HangSftp()

        t0 = time.monotonic()
        t.invalidate_sftp()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"invalidate_sftp hung: {elapsed:.2f}s"
        assert t._sftp is None
        assert t.is_connected() is True

        t._sftp = HangSftp()
        t0 = time.monotonic()
        t.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"dispose with hung SFTP hung: {elapsed:.2f}s"
        assert t._sftp is None
        assert t.is_connected() is False
    finally:
        bridge.stop()


def test_mark_dead_hung_sftp_exit_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung SFTP exit on mark_dead stays within _DISPOSE_TIMEOUT_S.

    Same budget as invalidate/dispose; must not hang forever under _op_lock.
    """
    monkeypatch.setattr(ssh_mod, "_DISPOSE_TIMEOUT_S", 0.25)

    class HangSftp:
        def exit(self) -> object:
            async def hang() -> None:
                await asyncio.sleep(3600)

            return hang()

    class Conn:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    bridge = AsyncLoopBridge()
    bridge.start()
    try:
        t = SSHTransport(
            host="h",
            username="u",
            connector=lambda **_k: Conn(),
            bridge=bridge,
        )
        t.connect()
        first_conn = t.connection
        t._sftp = HangSftp()

        t0 = time.monotonic()
        t.mark_dead("peer_reset")
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"mark_dead with hung SFTP hung: {elapsed:.2f}s"
        assert t._sftp is None
        assert t.is_connected() is False
        # SSH conn retained for connect/close dispose.
        assert t.connection is first_conn
    finally:
        bridge.stop()


def test_dispose_normal_wait_closed_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite dispose budget must not break happy-path wait_closed teardown."""
    monkeypatch.setattr(ssh_mod, "_DISPOSE_TIMEOUT_S", 0.5)
    waits = {"n": 0}

    class Conn:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

        def wait_closed(self) -> object:
            waits["n"] += 1

            async def done() -> None:
                return None

            return done()

    bridge = AsyncLoopBridge()
    bridge.start()
    try:
        t = SSHTransport(
            host="h",
            username="u",
            connector=lambda **_k: Conn(),
            bridge=bridge,
        )
        t.connect()
        t.close()
        assert waits["n"] >= 1
        assert t.is_connected() is False
        assert t.connection is None
    finally:
        bridge.stop()


# ---------------------------------------------------------------------------
# SFTP cache invalidation + drop-mid-op EXEC_FAILED -> mark_dead
# ---------------------------------------------------------------------------


def test_open_sftp_reopens_after_invalidate_sftp() -> None:
    """invalidate_sftp drops the cache; next open_sftp calls start_sftp again."""
    starts = {"n": 0}
    clients: list[object] = []

    class SftpClient:
        def __init__(self) -> None:
            self.closed = False

        def exit(self) -> None:
            self.closed = True

    class Conn:
        def is_closing(self) -> bool:
            return False

        def start_sftp_client(self) -> SftpClient:
            starts["n"] += 1
            c = SftpClient()
            clients.append(c)
            return c

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    c1 = t.open_sftp()
    assert starts["n"] == 1
    c1_again = t.open_sftp()
    assert c1_again is c1
    assert starts["n"] == 1  # live cache reused

    t.invalidate_sftp()
    assert t._sftp is None
    assert clients[0].closed is True

    c2 = t.open_sftp()
    assert starts["n"] == 2
    assert c2 is not c1
    assert t.is_connected() is True  # SSH session untouched


def test_open_sftp_rebuilds_dead_looking_cache() -> None:
    """open_sftp probes closed flags and reopens without explicit invalidate."""
    starts = {"n": 0}

    class LiveSftp:
        pass

    class Conn:
        def is_closing(self) -> bool:
            return False

        def start_sftp_client(self) -> LiveSftp:
            starts["n"] += 1
            return LiveSftp()

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()

    class DeadLooking:
        _closed = True

    t._sftp = DeadLooking()
    assert SSHTransport._sftp_looks_dead(t._sftp) is True
    fresh = t.open_sftp()
    assert starts["n"] == 1
    assert isinstance(fresh, LiveSftp)
    assert t._sftp is fresh


def test_mark_dead_clears_sftp_without_requiring_invalidate() -> None:
    """mark_dead nulls _sftp and exit/closes the client; reconnect rebuilds.

    Bare ``_sftp = None`` would orphan the remote SFTP subsystem; mark_dead
    must go through ``_clear_sftp_cache``.
    """
    starts = {"n": 0}
    exits = {"n": 0}

    class SftpClient:
        def exit(self) -> None:
            exits["n"] += 1

    class Conn:
        def __init__(self) -> None:
            self.dead = False

        def is_closing(self) -> bool:
            return self.dead

        def close(self) -> None:
            self.dead = True

        def start_sftp_client(self) -> SftpClient:
            starts["n"] += 1
            return SftpClient()

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    first_conn = t.connection
    t.open_sftp()
    assert starts["n"] == 1
    t.mark_dead("peer_reset")
    assert t._sftp is None
    assert t.is_connected() is False
    # SFTP peer teardown on mark_dead (not deferred until connect dispose).
    assert exits["n"] >= 1
    # _conn retained until connect/close dispose.
    assert t.connection is first_conn
    t.connect()
    t.open_sftp()
    assert starts["n"] == 2
    assert t.is_connected() is True


def test_drop_mid_op_exec_failed_marks_dead() -> None:
    """Non-timeout mid-op connection drop -> EXEC_FAILED + mark_dead.

    Peer reset mid-command is not a timeout path: _run_shell_on_conn maps
    it to EXEC_FAILED, and run_command marks the session dead when is_alive
    is False. Cached SFTP must be cleared with mark_dead.
    """

    class Conn:
        def __init__(self) -> None:
            self._closing = False

        def is_closing(self) -> bool:
            return self._closing

        def run(self, command: str, **_kwargs: object) -> object:
            # Peer drops mid-command (not a timeout).
            self._closing = True
            raise ConnectionResetError("Connection reset by peer")

    t = SSHTransport(host="h", username="u", connector=lambda **_k: Conn())
    t.connect()
    # Seed a cached SFTP so mark_dead clearing is observable.
    t._sftp = object()
    with pytest.raises(TransportError) as ei:
        t.run_command("echo hi")
    assert ei.value.code == "EXEC_FAILED"
    assert t.is_alive() is False
    assert t.is_connected() is False
    assert t._sftp is None
    assert t.meta.get("dead_reason")


# ---------------------------------------------------------------------------
# Signal-killed exit-code mapping in _coerce_exec_result.
# ---------------------------------------------------------------------------


class _Raw:
    """Minimal duck-typed asyncssh SSHCompletedProcess stand-in."""

    def __init__(self, **attrs: object) -> None:
        for k, v in attrs.items():
            setattr(self, k, v)


def test_coerce_exec_result_signal_killed_nonzero() -> None:
    """asyncssh exit_status=None + exit_signal='KILL' must NOT map to exit 0."""
    raw = _Raw(exit_status=None, exit_signal="KILL", stdout=b"", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code != 0
    # Shell convention: 128 + SIGKILL(9) = 137.
    assert r.exit_code == 137


def test_coerce_exec_result_signal_term_nonzero() -> None:
    raw = _Raw(exit_status=None, exit_signal="TERM", stdout=b"", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code != 0
    # 128 + SIGTERM(15) = 143.
    assert r.exit_code == 143


def test_coerce_exec_result_all_exit_attrs_missing_nonzero() -> None:
    """When every exit attr is None/missing, default to -1 (not 0)."""
    raw = _Raw(exit_status=None, exit_signal=None, stdout=b"partial", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code != 0
    assert r.exit_code == -1
    assert r.stdout == "partial"


def test_coerce_exec_result_no_exit_attrs_object_nonzero() -> None:
    """An object with stdout/stderr but no exit attrs at all -> non-zero."""
    raw = _Raw(exit_signal=None, stdout=b"x", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code != 0


def test_coerce_exec_result_normal_zero_preserved() -> None:
    """Regression guard: a clean exit 0 must still map to 0."""
    raw = _Raw(exit_status=0, stdout=b"ok", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code == 0
    assert r.stdout == "ok"


def test_coerce_exec_result_returncode_path_preserved() -> None:
    """subprocess-style returncode still wins when exit_status/exit_signal unset."""
    raw = _Raw(returncode=7, stdout=b"", stderr=b"boom")
    r = _coerce_exec_result(raw, default_cwd="/r")
    assert r.exit_code == 7
    assert r.stderr == "boom"


def test_exit_code_from_signal_mappings() -> None:
    assert _exit_code_from_signal("KILL") == 137
    assert _exit_code_from_signal("SIGKILL") == 137
    assert _exit_code_from_signal("TERM") == 143
    assert _exit_code_from_signal("") == -1
    assert _exit_code_from_signal("FROBNICATE") == -1
