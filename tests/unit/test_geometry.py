"""Unit tests: adaptive GeometryAdapter (T18 / notes/007)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyte

from mcp_remote_control.screen.buffer import (
    MAX_COLS,
    MAX_ROWS,
    MIN_COLS,
    MIN_ROWS,
    SEED_SHELL_COLS,
    SEED_SHELL_ROWS,
    clamp_geometry,
)
from mcp_remote_control.screen.geometry import (
    GROW_COLS,
    GROW_ROWS,
    MAX_GROW_STEPS,
    SEED_HEAVY,
    SEED_TUI,
    FitResult,
    GeometryAdapter,
    GeometryMemory,
    GeometryPlan,
    assess_layout,
    classify_command,
    command_basename,
    grow_geometry,
    memory_key,
    seed_for_class,
)

# ---------------------------------------------------------------------------
# seed / classify
# ---------------------------------------------------------------------------


def test_seed_shell_default() -> None:
    assert seed_for_class("shell") == (SEED_SHELL_COLS, SEED_SHELL_ROWS)
    assert seed_for_class("shell") == (160, 48)


def test_seed_tui_and_heavy() -> None:
    assert seed_for_class("tui") == SEED_TUI
    assert seed_for_class("tui_heavy") == SEED_HEAVY
    assert seed_for_class("unknown") == SEED_TUI  # bias wide
    assert seed_for_class("pager") == (160, 50)
    assert seed_for_class("repl") == (160, 48)


def test_classify_no_command_is_shell() -> None:
    assert classify_command(None) == "shell"
    assert classify_command("") == "shell"
    assert classify_command(None, argv=None) == "shell"


def test_classify_common_basenames() -> None:
    assert classify_command("bash") == "shell"
    assert classify_command("/usr/bin/zsh") == "shell"
    assert classify_command("python3") == "repl"
    assert classify_command("htop") == "tui"
    assert classify_command("vim") == "tui"
    assert classify_command("grok") == "tui_heavy"
    assert classify_command("lazygit") == "tui_heavy"
    assert classify_command("less") == "pager"
    assert classify_command("totally-unknown-app") == "unknown"


def test_classify_shell_inner_command() -> None:
    assert classify_command("bash -lc 'grok'") == "tui_heavy"
    assert classify_command("sh -c htop") == "tui"
    assert classify_command(None, argv=["/bin/bash", "-lc", "lazygit"]) == "tui_heavy"


def test_command_basename() -> None:
    assert command_basename("grok") == "grok"
    assert command_basename("/usr/local/bin/nvim") == "nvim"
    assert command_basename("bash -lc 'grok'") == "grok"


def test_plan_open_forced_skips_fit() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open(cols=120, rows=40, command="htop")
    assert plan.forced is True
    assert plan.fit_enabled is False
    assert plan.cols == 120
    assert plan.rows == 40
    assert plan.cmd_class == "tui"


def test_plan_open_seed_from_class() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open(command="grok", endpoint_id="lab")
    assert plan.forced is False
    assert plan.fit_enabled is True
    assert (plan.cols, plan.rows) == SEED_HEAVY
    assert plan.seed_cols == SEED_HEAVY[0]
    assert plan.cmd_class == "tui_heavy"


def test_plan_open_shell_seed_without_command() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open()
    assert plan.cmd_class == "shell"
    assert (plan.cols, plan.rows) == (SEED_SHELL_COLS, SEED_SHELL_ROWS)


def test_plan_open_profile_seed_override() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open(
        profile_defaults={"screen_cols": 140, "screen_rows": 44},
    )
    assert plan.cols == 140
    assert plan.rows == 44
    assert plan.forced is False
    assert plan.fit_enabled is True


def test_plan_open_fit_false() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open(command="htop", fit=False)
    assert plan.fit_enabled is False
    assert plan.forced is False
    assert (plan.cols, plan.rows) == SEED_TUI


# ---------------------------------------------------------------------------
# grow / no default shrink
# ---------------------------------------------------------------------------


def test_grow_increases_on_cramped() -> None:
    c, r = grow_geometry(160, 48, "cramped", "")
    assert c == 160 + GROW_COLS
    assert r == 48  # cramped grows cols first
    assert c > 160
    assert r >= 48


def test_grow_too_small_grows_both() -> None:
    c, r = grow_geometry(100, 30, "too_small", "terminal is too small")
    assert c >= 100 + GROW_COLS
    assert r >= 30 + GROW_ROWS


def test_grow_never_shrinks() -> None:
    # healthy must not reduce
    assert grow_geometry(200, 52, "healthy", "") == clamp_geometry(200, 52)
    # even weird health strings must not shrink
    c, r = grow_geometry(200, 52, "too_wide_for_tokens", "huge frame")
    assert c >= 200 and r >= 52
    # grow from max stays at max (no wrap/underflow)
    c2, r2 = grow_geometry(MAX_COLS, MAX_ROWS, "too_small", "terminal too small")
    assert c2 == MAX_COLS and r2 == MAX_ROWS


def test_grow_parses_need_n_columns() -> None:
    frame = "Error: need at least 180 columns"
    c, r = grow_geometry(100, 30, "too_small", frame)
    # 180 + margin (10) = 190
    assert c >= 180
    assert c >= 190 or c == MAX_COLS


def test_grow_respects_clamp_bounds() -> None:
    c, r = grow_geometry(MIN_COLS, MIN_ROWS, "too_small", "")
    assert MIN_COLS <= c <= MAX_COLS
    assert MIN_ROWS <= r <= MAX_ROWS


# ---------------------------------------------------------------------------
# assess
# ---------------------------------------------------------------------------


def test_assess_healthy_shell_prompt() -> None:
    frame = "user@host:~$ "
    assert assess_layout(frame, 160, 48, "shell") == "healthy"


def test_assess_too_small_message() -> None:
    frame = "Error: terminal is too small\nPlease resize."
    assert assess_layout(frame, 100, 30, "tui") == "too_small"


def test_assess_tui_under_160_is_cramped() -> None:
    assert assess_layout("htop", 120, 40, "tui") == "cramped"
    assert assess_layout("x", 100, 30, "tui_heavy") == "cramped"


def test_assess_cramped_full_width_lines() -> None:
    cols = 100
    # Many lines flush to the right edge → hard-wrap signal
    line = "x" * cols
    frame = "\n".join([line] * 8)
    assert assess_layout(frame, cols, 40, "shell") == "cramped"


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_geometry_memory_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "geometry_memory.json"
    mem = GeometryMemory(path)
    assert mem.get("ep1", "tui_heavy", "grok") is None
    mem.put("ep1", "tui_heavy", "grok", 200, 52, healthy=True)
    got = mem.get("ep1", "tui_heavy", "grok")
    assert got == (200, 52)
    # reload from disk
    mem2 = GeometryMemory(path)
    assert mem2.get("ep1", "tui_heavy", "grok") == (200, 52)


def test_geometry_memory_corrupt_ignored(tmp_path: Path) -> None:
    path = tmp_path / "geometry_memory.json"
    path.write_text("{not json", encoding="utf-8")
    mem = GeometryMemory(path)
    assert mem.get("ep", "shell", None) is None


def test_plan_uses_memory_seed(tmp_path: Path) -> None:
    mem = GeometryMemory(tmp_path / "m.json")
    mem.put("prod", "tui", "htop", 220, 56)
    ad = GeometryAdapter(memory=mem)
    plan = ad.plan_open(command="htop", endpoint_id="prod")
    assert plan.cols == 220
    assert plan.rows == 56


def test_memory_key_stable() -> None:
    k1 = memory_key("ep", "tui", "htop")
    k2 = memory_key("ep", "tui", "htop")
    assert k1 == k2
    assert k1 != memory_key("ep", "tui", "vim")


# ---------------------------------------------------------------------------
# adapt loop with fake session
# ---------------------------------------------------------------------------


class _FakePty:
    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        return 0

    def close(self) -> None:
        self._alive = False


class _FakeSession:
    """Minimal session stand-in for GeometryAdapter.adapt."""

    def __init__(
        self,
        cols: int,
        rows: int,
        *,
        frames: list[str] | None = None,
        surface: str = "shell",
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.surface = surface
        self.generation = 0
        self.last_hash: str | None = None
        self._closed = False
        self.pty = _FakePty(cols, rows)
        self.screen = pyte.Screen(cols, rows)
        self._frames = list(frames or [""])
        self._idx = 0
        self.resize_history: list[tuple[int, int]] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = clamp_geometry(cols, rows)
        self.pty.resize(self.cols, self.rows)
        self.screen.resize(self.rows, self.cols)
        self.resize_history.append((self.cols, self.rows))

    def shot(self, *, settle_s: float = 0.0, strip_trailing_empty: bool = True) -> dict:
        if self._idx < len(self._frames):
            frame = self._frames[self._idx]
            self._idx += 1
        else:
            frame = self._frames[-1] if self._frames else ""
        self.generation += 1
        return {
            "frame": frame,
            "hash": f"h{self.generation}",
            "cur": "0,0",
            "cur_tuple": (0, 0),
            "cols": self.cols,
            "rows": self.rows,
            "gen": self.generation,
            "alive": True,
            "exit": None,
        }


def test_adapt_healthy_no_grow() -> None:
    ad = GeometryAdapter()
    plan = GeometryPlan(
        cols=160,
        rows=48,
        cmd_class="shell",
        seed_cols=160,
        seed_rows=48,
        forced=False,
        fit_enabled=True,
    )
    sess = _FakeSession(160, 48, frames=["user@host:~$ "])
    result, shot = ad.adapt(sess, plan, settle_s=0.0)
    assert isinstance(result, FitResult)
    assert result.fit == "ok"
    assert result.steps == 0
    assert result.cols == 160
    assert result.rows == 48
    assert sess.resize_history == []
    assert shot["cols"] == sess.cols


def test_adapt_grows_on_too_small() -> None:
    ad = GeometryAdapter(max_grow_steps=3)
    plan = GeometryPlan(
        cols=100,
        rows=30,
        cmd_class="tui",
        seed_cols=100,
        seed_rows=30,
        forced=False,
        fit_enabled=True,
        command_basename="htop",
    )
    # First frame cramped/too_small; after grow report healthy-ish wide content
    frames = [
        "terminal is too small",
        "terminal is too small",
        "ok panel content",
    ]
    sess = _FakeSession(100, 30, frames=frames)
    result, shot = ad.adapt(sess, plan, settle_s=0.0, endpoint_id="")
    assert result.steps >= 1
    assert result.cols > 100 or result.rows > 30
    assert sess.cols == result.cols
    assert sess.rows == result.rows
    assert shot["cols"] == sess.cols
    assert shot["rows"] == sess.rows
    # Never shrunk below seed
    assert result.cols >= 100
    assert result.rows >= 30


def test_adapt_forced_no_grow() -> None:
    ad = GeometryAdapter()
    plan = GeometryPlan(
        cols=100,
        rows=30,
        cmd_class="tui",
        seed_cols=100,
        seed_rows=30,
        forced=True,
        fit_enabled=False,
    )
    sess = _FakeSession(100, 30, frames=["terminal is too small"])
    result, _shot = ad.adapt(sess, plan, settle_s=0.0)
    assert result.fit == "forced"
    assert result.steps == 0
    assert result.cols == 100
    assert sess.resize_history == []


def test_adapt_fit_disabled_no_grow() -> None:
    ad = GeometryAdapter()
    plan = ad.plan_open(command="htop", fit=False)
    # seed is SEED_TUI 180x50 — still too small message shouldn't grow
    sess = _FakeSession(plan.cols, plan.rows, frames=["terminal is too small"])
    result, _ = ad.adapt(sess, plan, settle_s=0.0)
    assert result.steps == 0
    assert result.fit == "ok"
    assert sess.resize_history == []


def test_adapt_respects_max_grow_steps_poor() -> None:
    ad = GeometryAdapter(max_grow_steps=2)
    plan = GeometryPlan(
        cols=100,
        rows=30,
        cmd_class="tui",
        seed_cols=100,
        seed_rows=30,
        forced=False,
        fit_enabled=True,
    )
    # Always too small
    sess = _FakeSession(100, 30, frames=["terminal is too small"] * 10)
    result, _ = ad.adapt(sess, plan, settle_s=0.0)
    assert result.steps <= MAX_GROW_STEPS
    assert result.steps == 2
    assert result.fit == "poor"
    # Monotonic non-decreasing sizes in resize history
    prev = (100, 30)
    for c, r in sess.resize_history:
        assert c >= prev[0] and r >= prev[1]
        prev = (c, r)


def test_open_frame_geometry_matches_session_after_grow() -> None:
    """Acceptance: open frame cols/rows consistent with session."""
    ad = GeometryAdapter(max_grow_steps=3)
    plan = ad.plan_open(command="vim")  # tui seed 180x50
    # Force cramped by using tiny session first
    sess = _FakeSession(
        100,
        30,
        frames=[
            "terminal is too small",
            "vim ready",
        ],
        surface="tui",
    )
    # Align plan seed with tiny open (as if forced seed was small)
    plan = GeometryPlan(
        cols=100,
        rows=30,
        cmd_class="tui",
        seed_cols=100,
        seed_rows=30,
        forced=False,
        fit_enabled=True,
        command_basename="vim",
    )
    result, shot = ad.adapt(sess, plan, settle_s=0.0)
    assert shot["cols"] == sess.cols == result.cols
    assert shot["rows"] == sess.rows == result.rows


def test_adapter_grow_method_delegates() -> None:
    ad = GeometryAdapter()
    assert ad.grow(160, 48, "cramped")[0] > 160
    assert ad.assess("terminal too small", 80, 24, "tui") == "too_small"
