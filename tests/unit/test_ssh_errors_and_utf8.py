"""SSH hostkey/auth error mapping, liveness, force_utf8_remote."""

from __future__ import annotations

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

    class Conn:
        def __init__(self) -> None:
            self.dead = False

        def is_closing(self) -> bool:
            return self.dead

        def close(self) -> None:
            pass

    def connector(**_k: object) -> Conn:
        opens["n"] += 1
        return Conn()

    t = SSHTransport(host="h", username="u", connector=connector)
    t.connect()
    first = opens["n"]
    assert first >= 1
    assert t.is_alive() is True
    t.mark_dead("test")
    assert t.is_alive() is False
    # Fresh connect after mark_dead clears connected flag
    t.connect()
    assert opens["n"] == first + 1
    assert t.is_connected() is True


# ---------------------------------------------------------------------------
# O5: signal-killed exit-code mapping in _coerce_exec_result.
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
    """An object with stdout/stderr but no exit attrs at all → non-zero."""
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
