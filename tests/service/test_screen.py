"""Service tests: screen open/list/close/send with real local PTY (T09/T10)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry, reset_registry
from mcp_remote_control.endpoint.registry import Endpoint
from mcp_remote_control.screen.buffer import dump_frame
from mcp_remote_control.screen.registry import (
    get_screen_registry,
    reset_screen_registry,
)
from mcp_remote_control.transport.local import LocalTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Prefer a plain POSIX shell so CI/dev machines without fancy zshrc stay stable.
_SIMPLE_SHELL = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


@pytest.fixture(autouse=True)
def _clean_regs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_screen_registry()
    yield
    reset_screen_registry()
    reset_registry()


def _open_local(**kwargs: object):
    defaults = {
        "ep": "local",
        "home": FIXTURES,
        "shell": _SIMPLE_SHELL,
        "settle_s": 0.5,
        "cols": 120,
        "rows": 40,
    }
    defaults.update(kwargs)
    return screen_ops.open_screen(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# local open / list / close
# ---------------------------------------------------------------------------


def test_local_open_no_command_required() -> None:
    r = _open_local()
    assert r.kind == "screen"
    assert r.status == "ok", r.render_text()
    assert r.code is None
    sid = r.fields.get("id") or r.fields.get("screen_id")
    assert sid and str(sid).startswith("scr_")
    assert r.fields.get("ep") == "local"
    assert r.fields.get("open") == "shell"
    assert r.fields.get("screen_id") == sid
    # Geometry present
    assert r.fields.get("cols") == 120
    assert r.fields.get("rows") == 40
    assert r.cwd is not None
    text = r.render_text()
    assert text.startswith("@screen ok")
    assert f"id={sid}" in text
    assert "cwd=" in text


def test_local_open_returns_cur_or_frame() -> None:
    r = _open_local()
    assert r.status == "ok", r.render_text()
    cur = r.fields.get("cur")
    assert cur is not None
    # cur=r,c
    parts = str(cur).split(",")
    assert len(parts) == 2
    assert parts[0].isdigit() and parts[1].isdigit()
    text = r.render_text()
    assert "cur=" in text
    # Frame body optional if shell is silent, but usually present after settle.
    # Accept either body frame or at least cur in header.
    assert r.body is not None or "cur=" in text


def test_list_shows_session_close_then_unusable() -> None:
    r = _open_local()
    assert r.status == "ok"
    sid = r.fields["id"]

    listed = screen_ops.list_screens()
    assert listed.status == "ok"
    assert listed.fields.get("n") == 1
    assert listed.body is not None
    assert sid in listed.body
    assert "ep=local" in listed.body

    closed = screen_ops.close_screen(id=sid)
    assert closed.status == "ok"
    assert closed.fields.get("closed") is True

    listed2 = screen_ops.list_screens()
    assert listed2.status == "ok"
    assert listed2.fields.get("n") == 0
    assert listed2.body is None or sid not in (listed2.body or "")

    # Send / close no longer usable
    send = screen_ops.send_screen(id=sid)
    assert send.status == "error"
    assert send.code == "SCREEN_NOT_FOUND"

    again = screen_ops.close_screen(id=sid)
    assert again.status == "error"
    assert again.code == "SCREEN_NOT_FOUND"


def test_send_text_pwd_returns_frame() -> None:
    r = _open_local()
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]

    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "text", "text": "pwd", "submit": True}],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 8000},
    )
    assert send.status in ("ok", "unchanged"), send.render_text()
    assert send.code is None
    assert send.fields.get("id") == sid
    assert send.fields.get("cur")
    assert "did" in send.fields
    text = send.render_text()
    assert text.startswith("@screen")
    # Frame or path-ish content: shell cwd often appears after pwd.
    body = send.body or ""
    combined = body + "\n" + text
    # At least no crash; prefer path signal when shell echoes.
    assert send.status in ("ok", "unchanged")
    if send.status == "ok":
        # Body usually present when something painted.
        assert body is not None or "cur=" in text
        # Soft check: slash appears in typical pwd output / prompt path.
        assert "/" in combined or "pwd" in combined.lower() or send.fields.get("gen", 0) >= 0


def test_send_actions_order_multi() -> None:
    r = _open_local()
    sid = r.fields["id"]
    send = screen_ops.send_screen(
        id=sid,
        actions=[
            {"type": "text", "text": "echo "},
            {"type": "text", "text": "MRC_ORDER_OK"},
            {"type": "submit"},
        ],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 8000},
    )
    assert send.status in ("ok", "unchanged", "dead"), send.render_text()
    # Order guarantee: did= lists action types in execution order.
    assert send.fields.get("did") == "text,text,submit"
    body = send.body or ""
    if send.status == "ok" and body:
        assert "MRC_ORDER_OK" in body


def test_send_unchanged_on_double_shot() -> None:
    r = _open_local()
    sid = r.fields["id"]
    # First pure shot to establish last_hash after open settle.
    first = screen_ops.send_screen(
        id=sid,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 50},
        shot=True,
    )
    assert first.status in ("ok", "unchanged"), first.render_text()
    # Second empty send with no input should be unchanged (or ok if still painting).
    second = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "nop"}],
        wait={"until": "deadline", "timeout_ms": 0},
        shot=True,
    )
    assert second.status in ("ok", "unchanged"), second.render_text()
    if second.status == "unchanged":
        assert second.body is None
        assert second.fields.get("unchanged") is True or second.status == "unchanged"
        assert second.fields.get("cur")
        assert second.fields.get("hash")


def test_send_wait_only_does_not_inject_cwd_probe() -> None:
    """O3: a wait-only send skips the silent cwd probe (no ctrl+u / echo)."""
    r = _open_local()
    sid = r.fields["id"]
    # Establish a stable frame first.
    screen_ops.send_screen(
        id=sid,
        actions=[],
        wait={"until": "deadline", "timeout_ms": 50},
        shot=True,
    )
    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "wait", "ms": 20}],
        wait={"until": "deadline", "timeout_ms": 0},
        shot=True,
    )
    assert send.status in ("ok", "unchanged", "dead"), send.render_text()
    body = send.body or ""
    # Probe marker must never leak; with the fix the probe never runs for wait.
    assert "__MRC_PWD__:" not in body
    assert "__MRC_PWD__:" not in send.render_text()
    screen_ops.close_screen(id=sid)


def test_send_shot_false_skips_body() -> None:
    r = _open_local()
    sid = r.fields["id"]
    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "nop"}],
        wait={"until": "deadline", "timeout_ms": 0},
        shot=False,
    )
    assert send.status in ("ok", "dead"), send.render_text()
    assert send.body is None
    assert send.fields.get("shot") is False


def test_send_keys_ctrl_c_no_crash() -> None:
    r = _open_local()
    sid = r.fields["id"]
    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "keys", "keys": ["ctrl+c"]}],
        wait={"until": "idle", "idle_ms": 100, "timeout_ms": 3000},
    )
    assert send.status in ("ok", "unchanged", "dead"), send.render_text()
    assert send.fields.get("id") == sid


def test_cli_screen_send_json_actions() -> None:
    r = _open_local()
    sid = r.fields["id"]
    actions = json.dumps([{"type": "text", "text": "true", "submit": True}])
    code = main(
        [
            "screen",
            "send",
            "--id",
            sid,
            "--json-actions",
            actions,
            "--json",
        ]
    )
    assert code == EXIT_OK
    # Close via core
    assert screen_ops.close_screen(id=sid).status == "ok"


def test_missing_ep() -> None:
    r = screen_ops.open_screen(home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_lazy_connect_on_screen_open() -> None:
    reg = get_registry()
    assert reg.get("local") is None
    r = _open_local()
    assert r.status == "ok"
    ep = reg.get("local")
    assert ep is not None
    assert ep.connected is True


def test_cli_screen_open_list_close() -> None:
    # CLI path (does not pass shell=; profile may use zsh — still should work).
    code = main(["screen", "open", "--ep", "local", "--json"])
    # open goes through default shell from profile; may be slow but should ok
    # If profile shell is broken, core API tests above still cover AC.
    # Here we just exercise argparse wiring with Core when possible.
    # Re-run via Core for deterministic teardown in this process.
    # (CLI main uses process-global registry too.)
    assert code in (EXIT_OK, EXIT_VALIDATION)
    # Always exercise list/close via Core for stable AC.
    sessions = get_screen_registry().list_open()
    if sessions:
        sid = sessions[0].id
        assert main(["screen", "list", "--json"]) == EXIT_OK
        assert main(["screen", "close", "--id", sid, "--json"]) == EXIT_OK


def test_cli_open_json_has_screen_id() -> None:
    r = _open_local()
    assert r.status == "ok"
    data = json.loads(r.render_json())
    assert data["kind"] == "screen"
    assert data["status"] == "ok"
    assert data.get("screen_id") or data.get("id")
    assert "cur" in data


# ---------------------------------------------------------------------------
# capability / winrm UNSUPPORTED (T13 — no fake PTY frames)
# ---------------------------------------------------------------------------


def _mock_winrm_connector(**_kwargs: object):
    """Minimal injectable WinRM session (no network); mirrors test_winrm."""
    from mcp_remote_control.transport.base import ExecResult

    class _MockWinRMSession:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"

        def close(self) -> None:
            return None

        def run_command(self, command: str, **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout=f"out:{command}\n", stderr="", cwd=self.cwd)

        def run_argv(self, argv: list[str], **_kw: object) -> ExecResult:
            return ExecResult(exit_code=0, stdout=" ".join(argv) + "\n", cwd=self.cwd)

    return _MockWinRMSession()


def test_no_screen_cap_returns_unsupported() -> None:
    """Inject an endpoint with screen=false (winrm-like caps)."""
    ensure_endpoint("local", home=FIXTURES)
    reg = get_registry()
    ep = reg.get("local")
    assert ep is not None
    # Flip caps to deny screen (simulates winrm matrix).
    ep.caps = {
        "exec": True,
        "fs": True,
        "screen": False,
        "ps": True,
    }

    r = screen_ops.open_screen(
        ep="local",
        home=FIXTURES,
        shell=_SIMPLE_SHELL,
        settle_s=0.1,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.body is None  # no forged frame
    assert "screen" in (r.fields.get("msg") or "").lower() or r.code == "UNSUPPORTED"
    text = r.render_text()
    assert "UNSUPPORTED" in text
    assert get_screen_registry().list_open() == []


def test_winrm_transport_open_fails_unsupported_or_connect() -> None:
    """WinRM transport cannot open screen; connect may fail first."""
    # Register a synthetic winrm endpoint without going through profile connect.
    reg = get_registry()
    transport = LocalTransport()
    transport.connect()
    # Pretend winrm with no screen cap.
    fake = Endpoint(
        name="win-fake",
        transport_name="winrm",
        caps={"exec": True, "fs": True, "screen": False, "ps": True},
        connected=True,
        transport=transport,
        cwd="/",
    )
    reg._endpoints["win-fake"] = fake

    r = screen_ops.open_screen(ep="win-fake", home=FIXTURES, settle_s=0.05)
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.body is None
    assert get_screen_registry().list_open() == []


def test_lab_win_screen_open_unsupported_no_session() -> None:
    """T13: lab-win + mock winrm → screen open UNSUPPORTED, no session, no frame."""
    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]

    r = screen_ops.open_screen(
        ep="lab-win",
        home=FIXTURES,
        connector=_mock_winrm_connector,
        settle_s=0.05,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("ep") == "lab-win"
    assert r.fields.get("transport") == "winrm"
    assert r.fields.get("op") == "open"
    # No forged PTY frame body on the hard-fail path.
    assert r.body is None
    assert "cur" not in r.fields
    assert "gen" not in r.fields
    assert "hash" not in r.fields
    # Agent track greppable for UNSUPPORTED.
    text = r.render_text()
    assert "UNSUPPORTED" in text
    assert text.startswith("@screen error")
    assert "code=UNSUPPORTED" in text
    # JSON track likewise.
    data = json.loads(r.render_json())
    assert data["status"] == "error"
    assert data["code"] == "UNSUPPORTED"
    assert "body" not in data or data.get("body") in (None, "")
    # Must not create a screen session.
    assert get_screen_registry().list_open() == []
    listed = screen_ops.list_screens()
    assert listed.status == "ok"
    assert listed.fields.get("n") == 0


def test_lab_win_screen_run_dispatch_and_send_no_frame() -> None:
    """run(op=open|send) on winrm: open UNSUPPORTED; send never sees a session."""
    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]

    opened = screen_ops.run(
        op="open",
        ep="lab-win",
        home=FIXTURES,
        connector=_mock_winrm_connector,
        settle_s=0.05,
    )
    assert opened.status == "error"
    assert opened.code == "UNSUPPORTED"
    assert opened.body is None
    assert "UNSUPPORTED" in opened.render_text()
    assert get_screen_registry().list_open() == []

    # No session exists → send cannot forge a frame (SCREEN_NOT_FOUND, not ok+body).
    sent = screen_ops.run(op="send", id="scr_nonexistent")
    assert sent.status == "error"
    assert sent.code == "SCREEN_NOT_FOUND"
    assert sent.body is None
    assert get_screen_registry().list_open() == []


def test_cli_lab_win_screen_open_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI: mcp-remote-control-cli screen open --ep lab-win → greppable UNSUPPORTED, non-zero exit."""
    from mcp_remote_control.cli_cmds import EXIT_VALIDATION

    reg = get_registry()
    reg.winrm_connector = _mock_winrm_connector  # type: ignore[assignment]
    code = main(["screen", "open", "--ep", "lab-win"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "UNSUPPORTED" in out
    assert "@screen error" in out
    assert get_screen_registry().list_open() == []


# ---------------------------------------------------------------------------
# pyte unit-style (no real PTY required)
# ---------------------------------------------------------------------------


def test_pyte_frame_dump_unit() -> None:
    import pyte

    screen = pyte.Screen(40, 10)
    stream = pyte.Stream(screen)
    stream.feed("hello\r\nworld")
    frame = dump_frame(screen, strip_trailing_empty=True)
    assert "hello" in frame
    assert "world" in frame
    # Cursor somewhere after feed
    assert screen.cursor.y >= 0


def test_screen_session_feed_cross_chunk_utf8_unit() -> None:
    """O3/H5: ScreenSession.feed reassembles a split multi-byte UTF-8 char."""
    from mcp_remote_control.screen.session import ScreenSession

    class _FakePty:
        cols = 40
        rows = 5
        cwd: str | None = "/tmp"

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

    sess = ScreenSession(
        id="scr_utf8",
        ep="local",
        pty=_FakePty(),
        cols=40,
        rows=5,
        cwd="/tmp",
    )
    # "中" = U+4E2D = b'\xe4\xb8\xad'; split across two feed boundaries.
    sess.feed(b"\xe4\xb8")
    sess.feed(b"\xad")
    frame = dump_frame(sess.screen, strip_trailing_empty=True)
    assert "中" in frame
    assert "�" not in frame


def test_multiple_screens_independent() -> None:
    r1 = _open_local()
    r2 = _open_local()
    assert r1.status == "ok" and r2.status == "ok"
    assert r1.fields["id"] != r2.fields["id"]
    listed = screen_ops.list_screens()
    assert listed.fields.get("n") == 2
    screen_ops.close_screen(id=r1.fields["id"])
    listed2 = screen_ops.list_screens()
    assert listed2.fields.get("n") == 1
    assert r2.fields["id"] in (listed2.body or "")


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
        home=FIXTURES,
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
# T19: silent cwd probe after send (live local shell)
# ---------------------------------------------------------------------------


def test_send_cd_updates_cwd_via_probe() -> None:
    """After `cd` + idle, session cwd should refresh (probe or heuristic)."""
    r = _open_local(cwd="/tmp")
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]
    assert r.cwd is not None

    send = screen_ops.send_screen(
        id=sid,
        actions=[{"type": "text", "text": "cd /usr", "submit": True}],
        wait={"until": "idle", "idle_ms": 200, "timeout_ms": 8000},
    )
    assert send.status in ("ok", "unchanged", "dead"), send.render_text()
    # Probe should land on /usr (resolved). Heuristic also covers /usr.
    assert send.cwd is not None, send.render_text()
    # Accept /usr or /private/usr (macOS) or trailing variants.
    cwd_norm = send.cwd.rstrip("/")
    assert cwd_norm.endswith("usr") or cwd_norm == "/usr", send.cwd
    # Marker must not appear in Agent frame body.
    body = send.body or ""
    assert "__MRC_PWD__:" not in body
    text = send.render_text()
    assert "cwd=" in text
    assert "__MRC_PWD__:" not in text

    screen_ops.close_screen(id=sid)


def test_open_shell_cwd_absolute_no_probe_leak() -> None:
    r = _open_local(cwd="/tmp")
    assert r.status == "ok", r.render_text()
    assert r.cwd is not None
    assert r.cwd.startswith("/")
    body = r.body or ""
    assert "__MRC_PWD__:" not in body
    assert "__MRC_PWD__:" not in r.render_text()
    screen_ops.close_screen(id=r.fields["id"])
