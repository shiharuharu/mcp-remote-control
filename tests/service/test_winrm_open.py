"""Service tests: WinRM open, identity/probe, connect, caps, CLI."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from _winrm_session_fake import _MockWinRMSession

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_TRANSPORT
from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import get_registry
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.winrm import WinRMTransport, note_winrm_connect_handle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
FAKE_PASSWORD = "dummy-winrm-password"


# ---------------------------------------------------------------------------
# Mock session helpers
# ---------------------------------------------------------------------------


def _ok_connector(**kwargs: object) -> _MockWinRMSession:
    # Ensure password is present for real-path wiring but never asserted in output.
    assert "host" in kwargs
    assert "username" in kwargs
    return _MockWinRMSession()


def _partial_probe_connector(**_kwargs: object) -> _MockWinRMSession:
    """Soft capability partial (identity seeds present) - open may still ok."""
    return _MockWinRMSession(probe_partial=True)


class _IdentityFailWinRMSession:
    """No identity seeds; execute_ps raises -> identity RTT hard-fail."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        raise RuntimeError("mock WSMan identity probe refused")


class _IdentityHangWinRMSession:
    """No identity seeds; execute_ps hangs past probe budget -> hard-fail."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []
        self.block = threading.Event()

    def close(self) -> None:
        self.closed = True
        self.block.set()

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        # Hang longer than the shortened probe budget; released on close.
        self.block.wait(timeout=30.0)
        return ("never", None, False)


class _IdentityEmptyStdoutWinRMSession:
    """No identity seeds; execute_ps returns empty stdout -> hard-fail."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.scripts: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        # JEA/restricted: success exit but empty stdout (error stream only).
        return ("", None, False)


def _identity_empty_stdout_connector(
    **_kwargs: object,
) -> _IdentityEmptyStdoutWinRMSession:
    return _IdentityEmptyStdoutWinRMSession()


def _fail_connector(**_kwargs: object) -> None:
    raise TransportError("CONNECT_FAILED", "mock winrm refused")


def _auth_fail_connector(**_kwargs: object) -> None:
    raise TransportError("AUTH_FAILED", "mock credentials rejected")


# ---------------------------------------------------------------------------
# endpoint open + caps + probe
# ---------------------------------------------------------------------------

def test_winrm_open_mock_success_caps() -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_ok_connector,
    )
    assert r.status == "ok"
    assert r.fields.get("transport") == "winrm"
    assert r.fields.get("ep") == "lab-win"
    caps = r.fields.get("caps") or ""
    assert "exec" in caps
    assert "fs" in caps
    assert "ps" in caps
    # screen must not be enabled
    assert "screen" not in caps.split(",")
    text = r.render_text()
    assert text.startswith("@endpoint ok")
    assert "caps=exec,fs,ps" in text
    assert FAKE_PASSWORD not in text
    assert "password" not in text.lower() or "password_path" not in text

    ep = reg.get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.caps["exec"] is True
    assert ep.caps["fs"] is True
    assert ep.caps["ps"] is True
    assert ep.caps["screen"] is False
    assert ep.probe is not None
    assert ep.probe.get("transport") == "winrm"
    # probe present (ok or partial)
    assert ep.probe.get("status") in ("ok", "partial")
    assert ep.probe.get("os") == "windows" or ep.probe.get("shell") == "powershell"


def test_winrm_capability_partial_still_open_ok() -> None:
    """Capability/adapter soft partial with identity seeds -> open still ok."""
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_partial_probe_connector,
    )
    assert r.status == "ok"
    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.probe is not None
    assert ep.probe.get("status") == "partial"


def test_winrm_identity_probe_fail_open_error() -> None:
    """Identity/WSMan RTT failure -> open error + dispose; no fake connected."""
    sess = _IdentityFailWinRMSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]

    def connector(**_kwargs: object) -> _IdentityFailWinRMSession:
        return sess

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    msg = (r.fields.get("msg") or "").lower()
    assert "probe" in msg or "wsman" in msg or "identity" in msg or "refused" in msg
    # Must not leave a zombie registration (pop path after mark_dead).
    assert get_registry().get("lab-win") is None
    # Probe exception hard-fail disposes the session immediately.
    assert closes["n"] >= 1
    assert sess.closed is True
    listed = endpoint_ops.run(op="list", home=FIXTURES)
    assert listed.status == "ok"
    assert listed.fields.get("open") == 0 or listed.fields.get("open") is None or (
        isinstance(listed.fields.get("open"), int) and listed.fields.get("open") == 0
    )
    if listed.body:
        for ln in listed.body.splitlines():
            if ln.startswith("lab-win"):
                assert not any(tok == "open=1" for tok in ln.split())


def test_winrm_identity_empty_stdout_open_error() -> None:
    """Empty probe stdout without seeds -> open error, no fake connected."""
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_identity_empty_stdout_connector,
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    assert get_registry().get("lab-win") is None
    listed = endpoint_ops.run(op="list", home=FIXTURES)
    assert listed.status == "ok"
    if listed.body:
        for ln in listed.body.splitlines():
            if ln.startswith("lab-win"):
                assert not any(tok == "open=1" for tok in ln.split())


class _IdentityJunkStdoutWinRMSession:
    """No identity seeds; run_command returns non-heuristic junk.

    Uses run_command only (no execute_ps) so open identity RTT goes through
    ``_parse_probe_stdout`` rather than the capability soft-partial path.
    """

    def __init__(self, stdout: str = "Access Denied") -> None:
        self.stdout = stdout
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.commands: list[str] = []

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout_s, env
        self.commands.append(command)
        return ExecResult(
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            cwd=cwd or self.cwd,
        )


def _identity_access_denied_connector(
    **_kwargs: object,
) -> _IdentityJunkStdoutWinRMSession:
    return _IdentityJunkStdoutWinRMSession("Access Denied\n")


def test_winrm_identity_access_denied_open_error() -> None:
    """Access Denied probe stdout -> open error, no fake connected."""
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_identity_access_denied_connector,
    )
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    assert get_registry().get("lab-win") is None
    listed = endpoint_ops.run(op="list", home=FIXTURES)
    assert listed.status == "ok"
    if listed.body:
        for ln in listed.body.splitlines():
            if ln.startswith("lab-win"):
                assert not any(tok == "open=1" for tok in ln.split())


def test_winrm_identity_real_probe_lines_open_ok() -> None:
    """Real probe lines open ok; successful identity does not dispose."""
    sess = _IdentityJunkStdoutWinRMSession(
        "Microsoft Windows NT 10.0.19041.0\n"
        "5.1.19041.1\n"
        r"C:\Users\Administrator" + "\n"
        r"C:\Users\Administrator" + "\n"
    )
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]

    def connector(**_kwargs: object) -> _IdentityJunkStdoutWinRMSession:
        return sess

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok", f"expected ok, got {r.status} {r.fields}"
    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.probe is not None
    assert ep.probe.get("status") in ("ok", "partial")
    assert ep.probe.get("ps_version") == "5.1.19041.1" or ep.probe.get("os")
    # Successful identity / normal open must not gratuitously dispose.
    assert closes["n"] == 0
    assert sess.closed is False
    assert ep.transport is not None
    assert ep.transport.session is not None


def test_winrm_identity_probe_timeout_open_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung identity RTT fails open; dispose session; no fake connected."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "MRC_WINRM_PROBE_TIMEOUT_S", 0.2)
    sess = _IdentityHangWinRMSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]

    def connector(**_kwargs: object) -> _IdentityHangWinRMSession:
        return sess

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    # Wall-clock upper bound: probe budget + small scheduler slack (not 30s hang).
    assert elapsed < 3.0, f"identity probe timeout not bounded: {elapsed}s"
    assert get_registry().get("lab-win") is None
    assert sess.scripts, "probe must have attempted execute_ps"
    # Open-time identity hard-fail must dispose the hung oneshot session
    # (close count >=1) - not leave MaxShells pressure until a later connect.
    assert closes["n"] >= 1, "identity probe hard-fail must close the WinRM session"
    assert sess.closed is True
    sess.block.set()


class _IdentityHangRunCommandWinRMSession:
    """No identity seeds; run_command hangs past probe budget."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.closed = False
        self.commands: list[str] = []
        self.block = threading.Event()

    def close(self) -> None:
        self.closed = True
        self.block.set()

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del cwd, timeout_s, env
        self.commands.append(command)
        self.block.wait(timeout=30.0)
        return ExecResult(
            exit_code=0,
            stdout="never",
            stderr="",
            cwd=self.cwd,
        )


def test_winrm_identity_run_command_timeout_open_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung identity run_command fails open inside the probe wall-clock."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "MRC_WINRM_PROBE_TIMEOUT_S", 0.2)
    sess = _IdentityHangRunCommandWinRMSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]

    def connector(**_kwargs: object) -> _IdentityHangRunCommandWinRMSession:
        return sess

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    assert elapsed < 3.0, f"identity run_command timeout not bounded: {elapsed}s"
    assert get_registry().get("lab-win") is None
    assert sess.commands, "identity path must have attempted run_command"
    assert closes["n"] >= 1, "identity probe hard-fail must close the WinRM session"
    assert sess.closed is True
    sess.block.set()


# ---------------------------------------------------------------------------
# Open probe mode (skip | light | full) via env / profile
# ---------------------------------------------------------------------------

def test_winrm_open_default_full_still_probes_identity() -> None:
    """Default open (no env/profile override) still runs identity oneshot."""
    sess = _IdentityFailWinRMSession()

    def connector(**_kwargs: object) -> _IdentityFailWinRMSession:
        return sess

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "error"
    assert r.code in ("PROBE_FAILED", "NOT_CONNECTED")
    assert sess.scripts, "default/full must attempt identity execute_ps"
    assert get_registry().get("lab-win") is None


def test_winrm_open_probe_skip_env_no_oneshot_stays_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MRC_WINRM_PROBE=skip -> no oneshot; open ok if connect works (no fake)."""
    monkeypatch.setenv("MRC_WINRM_PROBE", "skip")
    sess = _IdentityFailWinRMSession()

    def connector(**_kwargs: object) -> _IdentityFailWinRMSession:
        return sess

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "ok", f"skip should open on connect success: {r.fields}"
    assert elapsed < 2.0, f"skip open should be fast, got {elapsed}s"
    assert sess.scripts == [], "skip must not issue identity/capability oneshot"
    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.probe == {"ps_probe": "skipped"}
    assert ep.transport is not None
    assert ep.transport.meta.get("winrm_ps") == {"ps_probe": "skipped"}
    # Connect really succeeded - not a fake after identity fail.
    assert ep.transport.is_connected() is True


def test_winrm_open_probe_light_env_no_oneshot_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MRC_WINRM_PROBE=light -> no oneshot; unseeded -> partial + connected."""
    monkeypatch.setenv("MRC_WINRM_PROBE", "light")
    sess = _IdentityFailWinRMSession()

    def connector(**_kwargs: object) -> _IdentityFailWinRMSession:
        return sess

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok", f"light should open on connect: {r.fields}"
    assert sess.scripts == [], "light must not issue remote oneshot"
    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.connected is True
    assert ep.probe is not None
    assert ep.probe.get("status") == "partial"
    assert ep.probe.get("ps_probe") == "light" or ep.probe.get("probe_mode") == "light"
    assert ep.transport is not None
    assert ep.transport.is_connected() is True


def test_winrm_open_probe_false_api_still_skip() -> None:
    """open(probe=False) keeps historical skip marker (API opt-out)."""
    reg = get_registry()
    sess = _IdentityFailWinRMSession()

    def connector(**_kwargs: object) -> _IdentityFailWinRMSession:
        return sess

    ep = reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)
    assert ep.connected is True
    assert sess.scripts == []
    assert ep.probe == {"ps_probe": "skipped"}


def test_winrm_open_probe_full_env_hard_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MRC_WINRM_PROBE=full forces hard identity (same as default)."""
    monkeypatch.setenv("MRC_WINRM_PROBE", "full")
    sess = _IdentityEmptyStdoutWinRMSession()

    def connector(**_kwargs: object) -> _IdentityEmptyStdoutWinRMSession:
        return sess

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "error"
    assert sess.scripts, "full must run oneshot"
    assert get_registry().get("lab-win") is None


# ---------------------------------------------------------------------------
# WinRM PS capability on open
# ---------------------------------------------------------------------------

class _FullLangWinRMSession(_MockWinRMSession):
    """Identity + FullLanguage capability seeds (no remote execute_ps)."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "FullLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


class _ConstrainedWinRMSession(_MockWinRMSession):
    """Identity + ConstrainedLanguage capability seeds."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "ConstrainedLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


def test_winrm_open_probe_full_language_agent_meta() -> None:
    """Default open with FullLanguage seeds -> probe + Agent meta ps_* fields."""

    def connector(**_kwargs: object) -> _FullLangWinRMSession:
        return _FullLangWinRMSession()

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("ps_version")
    assert r.fields.get("lang_mode") == "FullLanguage"
    assert r.fields.get("ps_fs") == 1
    assert r.fields.get("ps_edition") == "Desktop"
    text = r.render_text()
    assert "lang_mode=FullLanguage" in text
    assert "ps_fs=1" in text

    ep = get_registry().get("lab-win")
    assert ep is not None
    assert ep.probe is not None
    assert ep.probe.get("language_mode") == "FullLanguage"
    winrm_ps = ep.probe.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_script_fs") is True
    assert winrm_ps.get("ps_runspace") is True
    assert ep.transport is not None
    assert ep.transport.meta.get("winrm_ps", {}).get("ps_script_fs") is True


def test_winrm_open_probe_constrained_ps_fs_zero() -> None:
    """ConstrainedLanguage seeds -> ps_fs=0 and ps_script_fs false in probe."""

    def connector(**_kwargs: object) -> _ConstrainedWinRMSession:
        return _ConstrainedWinRMSession()

    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("lang_mode") == "ConstrainedLanguage"
    assert r.fields.get("ps_fs") == 0
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.probe is not None
    assert ep.probe["winrm_ps"]["ps_script_fs"] is False
    assert ep.probe["winrm_ps"]["ps_runspace"] is False


def test_winrm_open_connect_failed() -> None:
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_fail_connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"
    text = r.render_text()
    assert "CONNECT_FAILED" in text
    assert FAKE_PASSWORD not in text


def test_winrm_open_auth_failed() -> None:
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=_auth_fail_connector,
    )
    assert r.status == "error"
    assert r.code == "AUTH_FAILED"
    assert FAKE_PASSWORD not in r.render_text()


def test_winrm_registry_winrm_connector_injection() -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES)
    assert ep.connected is True
    assert ep.transport_name == "winrm"
    assert ep.probe is not None


# ---------------------------------------------------------------------------
# WinRM connect wall-clock around connector
# ---------------------------------------------------------------------------

def test_winrm_connect_hanging_connector_fails_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung connector cannot block forever.

    Inject a connector that sleeps until released; connect must raise
    CONNECT_FAILED within connect_timeout + grace and leave _connected False.
    """
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.25)
    block = threading.Event()

    def hang_connector(**_kwargs: object) -> object:
        block.wait(timeout=30.0)
        return _MockWinRMSession()

    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connect_timeout_ms=200,
        connector=hang_connector,
    )
    assert t._connected is False
    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        t.connect()
    elapsed = time.monotonic() - t0
    assert ei.value.code == "CONNECT_FAILED"
    assert t._connected is False
    assert t.is_connected() is False
    assert t._session is None
    # Budget ~= 0.2 + 0.25 = 0.45s; allow scheduler slack, not a 30s hang.
    assert elapsed < 2.0, f"connect wall-clock not bounded: {elapsed}s"
    assert elapsed >= 0.15, f"timed out too early: {elapsed}s"
    block.set()


def test_winrm_connect_sets_connected_only_after_success() -> None:
    """Happy-path connect leaves _connected False until session returns."""
    state: dict[str, bool | None] = {"during": None}

    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connect_timeout_ms=5000,
        connector=lambda **_k: _MockWinRMSession(),
    )

    def connector_bound(**_kwargs: object) -> _MockWinRMSession:
        state["during"] = t._connected
        return _MockWinRMSession()

    t._connector = connector_bound
    assert t._connected is False
    t.connect()
    assert state["during"] is False
    assert t._connected is True
    assert t.is_connected() is True
    assert t._session is not None


def test_winrm_open_hanging_connector_fails_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint open with forever-sleep connector fails within budget."""
    import mcp_remote_control.transport.winrm as winrm_mod

    # Short grace + force a short connect_timeout on every WinRMTransport.
    monkeypatch.setattr(winrm_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.25)
    real_init = winrm_mod.WinRMTransport.__init__

    def short_timeout_init(self: WinRMTransport, *args: object, **kwargs: object) -> None:
        kwargs = dict(kwargs)
        kwargs["connect_timeout_ms"] = 200
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(winrm_mod.WinRMTransport, "__init__", short_timeout_init)

    block = threading.Event()

    def hang_connector(**_kwargs: object) -> object:
        block.wait(timeout=30.0)
        return _MockWinRMSession()

    t0 = time.monotonic()
    r = endpoint_ops.run(
        op="open",
        profile="lab-win",
        home=FIXTURES,
        connector=hang_connector,
    )
    elapsed = time.monotonic() - t0
    assert r.status == "error", f"expected error, got {r.status} {r.fields}"
    assert r.code == "CONNECT_FAILED"
    assert get_registry().get("lab-win") is None
    assert elapsed < 2.0, f"open wall-clock not bounded: {elapsed}s"
    block.set()


def test_winrm_connect_timeout_closes_late_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connector builds a closeable Client then hangs - close it on timeout.

    Existing hang_connector tests sleep *before* constructing a session.
    A late object (Client already built in the worker) must still be closed
    so MaxShells cannot leak after CONNECT_FAILED.
    """
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.25)
    block = threading.Event()
    closes = {"n": 0}

    class _LateClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True
            closes["n"] += 1

    client = _LateClient()

    def hang_after_construct(**_kwargs: object) -> object:
        # The connector notes the handle as soon as it exists; then we hang.
        note_winrm_connect_handle(client)
        block.wait(timeout=30.0)
        return client

    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connect_timeout_ms=200,
        connector=hang_after_construct,
    )
    t0 = time.monotonic()
    with pytest.raises(TransportError) as ei:
        t.connect()
    elapsed = time.monotonic() - t0
    assert ei.value.code == "CONNECT_FAILED"
    assert t._connected is False
    assert t.is_connected() is False
    assert t._session is None
    assert client.closed is True
    assert closes["n"] >= 1, "connect timeout must close the already-built Client"
    assert elapsed < 2.0, f"connect wall-clock not bounded: {elapsed}s"
    block.set()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_winrm_open_ok(capsys: pytest.CaptureFixture[str]) -> None:
    reg = get_registry()
    reg.winrm_connector = _ok_connector  # type: ignore[assignment]
    code = main(["endpoint", "open", "--profile", "lab-win"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@endpoint ok" in out
    assert "transport=winrm" in out
    assert "caps=exec,fs,ps" in out
    assert FAKE_PASSWORD not in out


def test_cli_winrm_connect_failed_exit(capsys: pytest.CaptureFixture[str]) -> None:
    reg = get_registry()
    reg.winrm_connector = _fail_connector  # type: ignore[assignment]
    code = main(["endpoint", "open", "--profile", "lab-win"])
    assert code == EXIT_TRANSPORT
    out = capsys.readouterr().out
    assert "CONNECT_FAILED" in out
    assert "@endpoint error" in out


def test_cli_winrm_list_shows_profile(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoint", "list", "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out.strip())
    assert data["status"] == "ok"
    # body lists profiles; lab-win must appear when listed as text path
    r = endpoint_ops.run(op="list", home=FIXTURES)
    assert r.body is not None
    assert "lab-win" in r.body
    assert "winrm" in r.body


# ---------------------------------------------------------------------------
# WinRM transport: cert_validation default, env+timeout on pypsrp,
# run_argv call-operator (no cmd % expansion), open_runspace close-on-fail.
# ---------------------------------------------------------------------------

def test_winrm_cert_validation_default_true_when_omitted() -> None:
    """A caller omitting cert_validation gets TLS validation."""
    t = WinRMTransport(host="h", username="u", connector=lambda **_k: object())
    assert t.cert_validation is True
    kw = t.connect_kwargs()
    assert kw["cert_validation"] is True


def test_winrm_cert_validation_false_when_explicit() -> None:
    """Explicit False is still honored (non-TLS mock convenience)."""
    t = WinRMTransport(
        host="h",
        username="u",
        cert_validation=False,
        connector=lambda **_k: object(),
    )
    assert t.cert_validation is False
    assert t.connect_kwargs()["cert_validation"] is False


def test_winrm_profile_string_bool_flags_map_to_transport() -> None:
    """Profile winrm ssl/cert_validation/credssp string \"false\" disable."""
    from mcp_remote_control.config.models import AuthConfig, Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    t = _build_winrm_transport(
        Profile(
            name="lab",
            transport="winrm",
            host="10.0.0.1",
            username="admin",
            port=5985,
            auth=AuthConfig(method="credssp", password="s3cret"),
            winrm={
                "scheme": "http",
                "ssl": "false",
                "cert_validation": "false",
                "credssp": {"disable_tlsv1_2": "false"},
            },
        ),
        connector=lambda **_k: object(),
    )
    assert t.ssl is False
    assert t.cert_validation is False
    assert t.credssp_disable_tlsv1_2 is False
    kw = t.connect_kwargs()
    assert kw["ssl"] is False
    assert kw["cert_validation"] is False
    assert kw.get("credssp_disable_tlsv1_2") is False
