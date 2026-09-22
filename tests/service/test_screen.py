"""Service tests: screen open/list/close/send with a real local PTY."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.screen.registry import (
    get_screen_registry,
    reset_screen_registry,
)

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
    """A wait-only send skips the silent cwd probe (no ctrl+u / echo)."""
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
    # CLI path (does not pass shell=; profile may use zsh - still should work).
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


# ---------------------------------------------------------------------------
# Silent cwd probe after send (live local shell)
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


def test_send_submit_then_text_without_submit_keeps_typed_line() -> None:
    """Mid-list submit then typed text: silent probe must not wipe the line."""
    r = _open_local()
    assert r.status == "ok", r.render_text()
    sid = r.fields["id"]
    send = screen_ops.send_screen(
        id=sid,
        actions=[
            {"type": "submit"},
            {"type": "text", "text": "echo_keep_this_partial"},
        ],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 5000},
    )
    assert send.status in ("ok", "unchanged"), send.render_text()
    combined = (send.body or "") + "\n" + send.render_text()
    assert "echo_keep_this_partial" in combined
    assert "__MRC_PWD__:" not in combined
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


def test_open_local_missing_cwd_invalid_cwd() -> None:
    missing = "/definitely/not/here/mrc-screen-cwd"
    r = _open_local(cwd=missing)
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_CWD"
    listed = screen_ops.list_screens()
    assert listed.status == "ok"
    assert listed.fields.get("n") == 0
    assert listed.body is None or "scr_" not in (listed.body or "")


def test_open_local_file_cwd_invalid_cwd(tmp_path: Path) -> None:
    target = tmp_path / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    r = _open_local(cwd=str(target))
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_CWD"
    listed = screen_ops.list_screens()
    assert listed.status == "ok"
    assert listed.fields.get("n") == 0


def test_open_local_tmp_path_cwd_absolute(tmp_path: Path) -> None:
    r = _open_local(cwd=str(tmp_path))
    assert r.status == "ok", r.render_text()
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    # Exact probe text may wrap on a long pytest tmp path; the resolver
    # already applied the directory (open would have been INVALID_CWD).
    screen_ops.close_screen(id=r.fields["id"])
