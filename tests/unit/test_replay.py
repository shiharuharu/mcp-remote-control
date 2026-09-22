"""Unit tests: PTY fixture replay without live TUI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.screen.buffer import frame_hash
from mcp_remote_control.screen.replay import (
    format_replay_agent_text,
    format_replay_json,
    replay_ansi,
    replay_fixture,
    resolve_fixture_path,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pty"
BASH_BIN = FIXTURE_DIR / "bash_prompt.bin"
BASH_META = FIXTURE_DIR / "bash_prompt.meta.json"
TUI_BIN = FIXTURE_DIR / "tui_menu.bin"


@pytest.fixture(scope="module")
def bash_meta() -> dict:
    return json.loads(BASH_META.read_text(encoding="utf-8"))


def test_fixtures_exist() -> None:
    assert BASH_BIN.is_file()
    assert BASH_META.is_file()
    assert TUI_BIN.is_file()


def test_replay_ansi_hash_stable(bash_meta: dict) -> None:
    raw = BASH_BIN.read_bytes()
    r1 = replay_ansi(raw, cols=bash_meta["cols"], rows=bash_meta["rows"])
    r2 = replay_ansi(raw, cols=bash_meta["cols"], rows=bash_meta["rows"])
    assert r1.hash == r2.hash
    assert r1.hash == bash_meta["hash"]
    assert r1.hash == frame_hash(r1.frame)


def test_replay_fixture_expect_contains(bash_meta: dict) -> None:
    result = replay_fixture(BASH_BIN)
    assert result.expect_ok is True
    for needle in bash_meta["expect_contains"]:
        assert needle in result.frame
    assert result.cur  # e.g. "2,20"
    assert "," in result.cur


def test_replay_tui_menu_find_save() -> None:
    result = replay_fixture(TUI_BIN)
    assert "Save" in result.frame
    assert result.expect_ok is True
    # find_text via a temp screen from replay path - use frame content
    assert "Type a message" in result.frame


def test_replay_fixture_by_name() -> None:
    path = resolve_fixture_path("bash_prompt", base=FIXTURE_DIR)
    assert path == BASH_BIN.resolve()
    result = replay_fixture(path)
    assert "user@host" in result.frame


def test_replay_hash_mismatch_sets_expect_fail(tmp_path: Path) -> None:
    bin_path = tmp_path / "x.bin"
    bin_path.write_bytes(b"hello world\n")
    meta = tmp_path / "x.meta.json"
    meta.write_text(
        json.dumps({"cols": 40, "rows": 10, "hash": "deadbeef", "expect_contains": ["hello"]}),
        encoding="utf-8",
    )
    result = replay_fixture(bin_path)
    assert result.expect_ok is False
    assert result.expect_detail and "hash" in result.expect_detail


def test_format_replay_outputs() -> None:
    result = replay_fixture(BASH_BIN)
    text = format_replay_agent_text(result)
    assert text.startswith("@replay ok")
    assert "hash=" in text
    assert "user@host" in text
    data = json.loads(format_replay_json(result))
    assert data["kind"] == "replay"
    assert data["status"] == "ok"
    assert data["hash"] == result.hash


def test_cli_mrc_replay(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["replay", "screen", "--fixture", str(BASH_BIN), "--check"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@replay ok" in out
    assert "hash=" in out


def test_cli_mrc_replay_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "replay", "--fixture", str(BASH_BIN)])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out.strip())
    assert data["kind"] == "replay"
    assert data["hash"]
    assert "hello" in data["frame"]


def test_cli_mrc_replay_missing_fixture() -> None:
    code = main(["replay", "--fixture", "definitely-missing-xyz.bin"])
    assert code == EXIT_VALIDATION


# ---------------------------------------------------------------------------
# replay --check failure path + bare-positional fixture heuristic
# ---------------------------------------------------------------------------


def test_cli_mrc_replay_check_hash_mismatch_returns_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``replay --check`` with a fixture whose ``expect_hash`` mismatches must
    exit with ``EXIT_VALIDATION`` (3), not ``EXIT_OK``. ``replay.py:108``
    flips ``--check`` to ``EXIT_VALIDATION`` only when ``result.expect_ok is
    False``; a stale meta hash must therefore fail the run.
    """
    bin_path = tmp_path / "stale_hash.bin"
    bin_path.write_bytes(b"hello world\n")
    meta = tmp_path / "stale_hash.meta.json"
    meta.write_text(
        json.dumps(
            {
                "cols": 40,
                "rows": 10,
                "hash": "deadbeef",
                "expect_contains": ["hello"],
            }
        ),
        encoding="utf-8",
    )

    code = main(["replay", "--fixture", str(bin_path), "--check"])
    assert code == EXIT_VALIDATION, (
        f"--check with stale expect_hash must be EXIT_VALIDATION, got {code}"
    )
    out = capsys.readouterr().out
    # Rendered Agent track flips status to fail and surfaces the hash delta.
    assert "@replay fail" in out
    assert "deadbeef" in out  # expected hash from meta
    # The bare replay (no --check) on the same stale fixture must NOT fail.
    code_bare = main(["replay", "--fixture", str(bin_path)])
    assert code_bare == EXIT_OK


def test_cli_mrc_replay_check_valid_fixture_returns_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sanity guard: ``--check`` on the well-formed bash_prompt fixture stays
    EXIT_OK. Pinned so a future regression that always-fails ``--check`` is
    caught alongside the mismatch test above.
    """
    code = main(["replay", "screen", "--fixture", str(BASH_BIN), "--check"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@replay ok" in out
    assert "expect=ok" in out


def test_cli_mrc_replay_bare_positional_fixture_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare fixture name as the first positional (e.g. ``replay bash_prompt``)
    must work via the heuristic in ``replay.py:66-70``: a non-target positional
    with no ``--fixture`` is reinterpreted as the fixture name.
    """
    code = main(["replay", "bash_prompt"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@replay ok" in out
    assert "fixture=bash_prompt.bin" in out
    assert "user@host" in out  # contents actually replayed, not just listed
