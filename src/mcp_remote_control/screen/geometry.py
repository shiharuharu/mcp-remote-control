"""Adaptive screen geometry: classify -> seed -> health -> grow -> memory.

Remote PTYs have no host window to inherit. Winsize is chosen from the
command class (seed), optional endpoint memory, and first-frame layout
health, then may grow. Adaptive geometry never auto-shrinks the PTY -
token trimming is an output-layer concern, not a winsize policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_remote_control.screen.buffer import (
    MAX_COLS,
    SEED_SHELL_COLS,
    SEED_SHELL_ROWS,
    clamp_geometry,
)

# ---------------------------------------------------------------------------
# Implementation knobs (not user config)
# ---------------------------------------------------------------------------

SEED_SHELL = (SEED_SHELL_COLS, SEED_SHELL_ROWS)  # 160x48
SEED_REPL = (160, 48)
SEED_PAGER = (160, 50)
SEED_TUI = (180, 50)
SEED_HEAVY = (200, 52)

GROW_COLS = 20
GROW_ROWS = 4
MAX_GROW_STEPS = 3
# Extra columns when parsing "need at least N columns"
_NEED_COLS_MARGIN = 10

# Command class tables (basename, case-insensitive).
_SHELL = frozenset({"bash", "zsh", "sh", "fish", "ash", "dash", "ksh", "csh", "tcsh"})
_REPL = frozenset(
    {
        "python",
        "python2",
        "python3",
        "ipython",
        "ipython3",
        "node",
        "nodejs",
        "psql",
        "mysql",
        "mysqlsh",
        "redis-cli",
        "irb",
        "lua",
        "pry",
        "deno",
        "bun",
    }
)
_PAGER = frozenset({"less", "more", "man", "most", "pg"})
_TUI = frozenset(
    {
        "htop",
        "btop",
        "top",
        "vim",
        "nvim",
        "vi",
        "nano",
        "mc",
        "ranger",
        "emacs",
        "micro",
        "helix",
        "hx",
        "tmux",
        "screen",
    }
)
_TUI_HEAVY = frozenset(
    {
        "grok",
        "lazygit",
        "k9s",
        "lazydocker",
        "yazi",
        "claude",
        "codex",
        "gemini",
        "aichat",
        "lf",
        "fff",
    }
)

_SEEDS: dict[str, tuple[int, int]] = {
    "shell": SEED_SHELL,
    "repl": SEED_REPL,
    "pager": SEED_PAGER,
    "tui": SEED_TUI,
    "tui_heavy": SEED_HEAVY,
    "unknown": SEED_TUI,  # bias wide to avoid crushing unknown TUIs
}

# Layout health: too-small terminal messages from common TUI apps.
_TOO_SMALL_RE = re.compile(
    r"(?i)"
    r"(?:terminal\s+(?:is\s+)?too\s+small)"
    r"|(?:width\s+(?:is\s+)?(?:less|too)\b)"
    r"|(?:need(?:s)?\s+at\s+least\s+\d+\s+columns?)"
    r"|(?:min(?:imum)?\s+size)"
    r"|(?:screen\s+too\s+small)"
    r"|(?:window\s+(?:is\s+)?too\s+small)"
)
_NEED_COLS_RE = re.compile(r"(?i)need(?:s)?\s+at\s+least\s+(\d+)\s+columns?")

# Ellipsis / truncation marks often left when content is clipped.
_ELLIPSIS_CHARS = ("\u2026", "...", "\u2026")


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeometryPlan:
    """Seed plan for opening a PTY (before settle/grow)."""

    cols: int
    rows: int
    cmd_class: str
    seed_cols: int
    seed_rows: int
    forced: bool
    fit_enabled: bool
    command_basename: str | None = None


@dataclass(frozen=True)
class FitResult:
    """Outcome of post-open layout adapt (settle + optional grow)."""

    cols: int
    rows: int
    cmd_class: str
    seed_cols: int
    seed_rows: int
    steps: int
    fit: str  # ok | poor | forced
    health: str  # healthy | cramped | too_small
    forced: bool = False


# ---------------------------------------------------------------------------
# classify / seed
# ---------------------------------------------------------------------------


def classify_command(
    command: str | None = None,
    argv: list[str] | None = None,
) -> str:
    """Map command/argv to class in {shell, repl, pager, tui, tui_heavy, unknown}.

    No command (interactive shell open) -> ``shell``.
    Unknown basenames bias to ``unknown`` (seeded like tui).
    """
    name = _primary_basename(command, argv)
    if name is None:
        return "shell"
    return _class_for_basename(name)


def seed_for_class(cmd_class: str) -> tuple[int, int]:
    """Return clamped seed geometry for *cmd_class*."""
    seed = _SEEDS.get(cmd_class) or _SEEDS["unknown"]
    return clamp_geometry(seed[0], seed[1])


def command_basename(
    command: str | None = None,
    argv: list[str] | None = None,
) -> str | None:
    """Public helper: basename used for classification / memory key."""
    return _primary_basename(command, argv)


def _class_for_basename(name: str) -> str:
    n = name.lower().strip()
    if not n:
        return "shell"
    # strip common suffixes (.exe on Windows remote rare for TUI)
    n = n.removesuffix(".exe")
    if n in _SHELL:
        return "shell"
    if n in _REPL:
        return "repl"
    if n in _PAGER:
        return "pager"
    if n in _TUI_HEAVY:
        return "tui_heavy"
    if n in _TUI:
        return "tui"
    return "unknown"


def _primary_basename(
    command: str | None,
    argv: list[str] | None,
) -> str | None:
    """Pick the program name that should drive geometry class."""
    if argv:
        tokens = [str(a) for a in argv if a is not None and str(a).strip() != ""]
        if tokens:
            return _basename_from_tokens(tokens)
    if command is not None and str(command).strip():
        text = str(command).strip()
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = text.split()
        if tokens:
            return _basename_from_tokens(tokens)
        return Path(text).name or None
    return None


def _basename_from_tokens(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    head = Path(tokens[0]).name.lower()
    # shell -c / -lc 'inner' -> classify inner program when present
    if head in _SHELL and len(tokens) >= 2:
        inner = _extract_shell_inner(tokens)
        if inner:
            return inner
    return Path(tokens[0]).name or None


def _extract_shell_inner(tokens: list[str]) -> str | None:
    """From ``bash -lc 'grok'`` / ``sh -c htop`` return inner basename."""
    # Find -c / -lc / --command and take the following argument's first word.
    for i, tok in enumerate(tokens):
        t = tok.lower()
        if t in ("-c", "-lc", "--command", "-command") and i + 1 < len(tokens):
            script = tokens[i + 1]
            try:
                inner_tokens = shlex.split(script, posix=True)
            except ValueError:
                inner_tokens = script.split()
            if inner_tokens:
                # skip env assignments FOO=bar
                for w in inner_tokens:
                    if "=" in w and not w.startswith("-"):
                        continue
                    return Path(w).name or None
            return None
        # combined -lc without separate -c (already handled as -lc)
    return None


# ---------------------------------------------------------------------------
# Layout health + grow (never shrink)
# ---------------------------------------------------------------------------


def assess_layout(
    frame: str,
    cols: int,
    rows: int,
    cmd_class: str = "shell",
    *,
    surface: str | None = None,
) -> str:
    """Score a frame: ``healthy`` | ``cramped`` | ``too_small``.

    Does **not** recommend shrink - slightly large is preferred over unusable.
    """
    text = frame or ""
    if _TOO_SMALL_RE.search(text):
        return "too_small"

    # TUI-class / recognized surface under shell-ish width -> cramped
    surf = (surface or "").lower()
    is_tuiish = cmd_class in ("tui", "tui_heavy", "unknown") or surf in (
        "tui",
        "grok",
        "alt",
    )
    if is_tuiish and cols < 160:
        return "cramped"

    if _looks_cramped(text, cols):
        return "cramped"

    return "healthy"


def grow_geometry(
    cols: int,
    rows: int,
    health: str,
    frame: str = "",
) -> tuple[int, int]:
    """Return a larger-or-equal geometry. Never shrinks."""
    cols = int(cols)
    rows = int(rows)
    new_c, new_r = cols, rows

    # Explicit "need at least N columns" -> jump with margin
    m = _NEED_COLS_RE.search(frame or "")
    if m:
        needed = int(m.group(1)) + _NEED_COLS_MARGIN
        new_c = max(new_c, needed)

    health_l = (health or "").lower()
    if health_l == "too_small":
        new_c = max(new_c, cols + GROW_COLS)
        new_r = max(new_r, rows + GROW_ROWS)
    elif health_l == "cramped":
        new_c = max(new_c, cols + GROW_COLS)
        # rows only if already at/near max width (still never shrink)
        if cols >= MAX_COLS - GROW_COLS:
            new_r = max(new_r, rows + GROW_ROWS)
    # healthy / unknown -> no change (still enforce non-shrink below)

    # Absolute floor for tui-ish cramped under 160: step toward SEED_TUI
    if health_l in ("cramped", "too_small") and new_c < SEED_TUI[0]:
        new_c = max(new_c, min(SEED_TUI[0], cols + GROW_COLS * 2))

    # Hard rule: never shrink
    new_c = max(new_c, cols)
    new_r = max(new_r, rows)
    return clamp_geometry(new_c, new_r)


def _looks_cramped(frame: str, cols: int) -> bool:
    if not frame or cols <= 0:
        return False
    lines = frame.splitlines()
    nonempty = [ln.rstrip("\n") for ln in lines if ln.strip()]
    if not nonempty:
        return False

    # High fraction of lines that fill the width (hard wrap / edge flush).
    full_width = 0
    ellipsis_lines = 0
    total_fill = 0
    for ln in nonempty:
        # visible length approx (no ANSI in pyte dump)
        length = len(ln)
        total_fill += length
        if length >= max(1, cols - 1):
            full_width += 1
        if any(mark in ln for mark in ("\u2026", "...")):
            ellipsis_lines += 1

    n = len(nonempty)
    full_ratio = full_width / n
    avg_occ = (total_fill / n) / cols if cols else 0.0

    if full_ratio >= 0.45 and n >= 3:
        return True
    if avg_occ > 0.92 and ellipsis_lines >= 2:
        return True
    if ellipsis_lines >= max(3, n // 4) and avg_occ > 0.85:
        return True
    return False


# ---------------------------------------------------------------------------
# Endpoint memory (automatic; not user config)
# ---------------------------------------------------------------------------


def memory_key(
    endpoint_id: str,
    cmd_class: str,
    basename: str | None,
) -> str:
    raw = f"{endpoint_id}|{cmd_class}|{basename or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# Process-wide locks for GeometryMemory RMW (put load-then-save). Separate
# GeometryMemory instances sharing a path must serialize so concurrent puts
# of different keys do not last-writer-wins drop earlier keys.
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _geometry_file_lock(path: Path) -> threading.Lock:
    """Return a process-internal Lock for *path* (absolute string key)."""
    key = str(path.expanduser().absolute())
    with _FILE_LOCKS_GUARD:
        lk = _FILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _FILE_LOCKS[key] = lk
        return lk


class GeometryMemory:
    """JSON map at ``{home}/state/geometry_memory.json`` (best-effort)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._cache: dict[str, Any] | None = None

    @classmethod
    def for_home(cls, home: Path | str | None) -> GeometryMemory:
        if home is None:
            return cls(None)
        p = Path(home) / "state" / "geometry_memory.json"
        return cls(p)

    def get(
        self,
        endpoint_id: str,
        cmd_class: str,
        basename: str | None,
    ) -> tuple[int, int] | None:
        data = self._load()
        if not data:
            return None
        key = memory_key(endpoint_id, cmd_class, basename)
        entry = data.get(key)
        if not isinstance(entry, Mapping):
            return None
        try:
            c = int(entry["cols"])
            r = int(entry["rows"])
        except (KeyError, TypeError, ValueError):
            return None
        if entry.get("healthy") is False:
            return None
        return clamp_geometry(c, r)

    def put(
        self,
        endpoint_id: str,
        cmd_class: str,
        basename: str | None,
        cols: int,
        rows: int,
        *,
        healthy: bool = True,
    ) -> None:
        if self.path is None:
            return
        # Serialize RMW across instances that share this path; re-read disk
        # under the lock so we merge this key into current file state rather
        # than overwriting with a stale in-memory full map.
        with _geometry_file_lock(self.path):
            data = dict(self._reload())
            key = memory_key(endpoint_id, cmd_class, basename)
            c, r = clamp_geometry(cols, rows)
            data[key] = {
                "cols": c,
                "rows": r,
                "healthy": bool(healthy),
                "updated_at": time.time(),
                "endpoint": endpoint_id,
                "class": cmd_class,
                "basename": basename,
            }
            self._save(data)

    def _load(self) -> dict[str, Any] | None:
        if self._cache is not None:
            return self._cache
        return self._reload()

    def _reload(self) -> dict[str, Any]:
        """Read path from disk into ``_cache`` (empty dict on miss/error)."""
        if self.path is None or not self.path.is_file():
            self._cache = {}
            return self._cache
        try:
            raw = self.path.read_text(encoding="utf-8")
            obj = json.loads(raw)
            if isinstance(obj, dict):
                self._cache = obj
            else:
                self._cache = {}
        except (OSError, json.JSONDecodeError, UnicodeError):
            self._cache = {}
        return self._cache

    def _save(self, data: dict[str, Any]) -> None:
        """Persist *data* via a unique same-dir temp + ``os.replace``.

        Unique temp names avoid concurrent writers clobbering a shared
        ``*.tmp`` path (torn / invalid JSON after replace). Best-effort:
        ``OSError`` is swallowed; other errors re-raise after cleanup.
        Callers that RMW (``put``) must hold ``_geometry_file_lock`` so
        field-level merges are not lost to last-writer full-map overwrite.
        """
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
        except OSError:
            return
        try:
            try:
                fh = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            with fh:
                fh.write(
                    json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                )
            os.replace(tmp, self.path)
            self._cache = data
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # Best-effort only
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# GeometryAdapter
# ---------------------------------------------------------------------------


class GeometryAdapter:
    """Seed -> settle health -> limited grow. Never auto-shrinks the PTY."""

    def __init__(
        self,
        memory: GeometryMemory | None = None,
        *,
        max_grow_steps: int = MAX_GROW_STEPS,
    ) -> None:
        self.memory = memory if memory is not None else GeometryMemory(None)
        self.max_grow_steps = max(0, int(max_grow_steps))

    def plan_open(
        self,
        *,
        command: str | None = None,
        argv: list[str] | None = None,
        cols: int | None = None,
        rows: int | None = None,
        endpoint_id: str = "",
        fit: bool = True,
        profile_defaults: Mapping[str, Any] | None = None,
    ) -> GeometryPlan:
        """Resolve open geometry.

        * Explicit ``cols`` + ``rows`` -> forced (skip adaptive grow).
        * Else: memory seed or class seed (profile screen_cols/rows only as
          seed override when both set - not a hard lock unless also forced).
        """
        cmd_class = classify_command(command, argv)
        basename = command_basename(command, argv)

        if cols is not None and rows is not None:
            c, r = clamp_geometry(int(cols), int(rows))
            return GeometryPlan(
                cols=c,
                rows=r,
                cmd_class=cmd_class,
                seed_cols=c,
                seed_rows=r,
                forced=True,
                fit_enabled=False,
                command_basename=basename,
            )

        # Optional profile seed (escape hatch, still may grow if fit=True)
        p_cols = p_rows = None
        if profile_defaults:
            try:
                raw_c = profile_defaults.get("screen_cols")
                raw_r = profile_defaults.get("screen_rows")
                if raw_c is not None and raw_r is not None:
                    p_cols = int(raw_c)
                    p_rows = int(raw_r)
            except (TypeError, ValueError):
                p_cols = p_rows = None

        mem = None
        if endpoint_id:
            mem = self.memory.get(endpoint_id, cmd_class, basename)

        if mem is not None:
            seed_c, seed_r = mem
        elif p_cols is not None and p_rows is not None:
            seed_c, seed_r = clamp_geometry(p_cols, p_rows)
        else:
            seed_c, seed_r = seed_for_class(cmd_class)

        seed_c, seed_r = clamp_geometry(seed_c, seed_r)
        return GeometryPlan(
            cols=seed_c,
            rows=seed_r,
            cmd_class=cmd_class,
            seed_cols=seed_c,
            seed_rows=seed_r,
            forced=False,
            fit_enabled=bool(fit),
            command_basename=basename,
        )

    def assess(
        self,
        frame: str,
        cols: int,
        rows: int,
        cmd_class: str = "shell",
        *,
        surface: str | None = None,
    ) -> str:
        return assess_layout(frame, cols, rows, cmd_class, surface=surface)

    def grow(
        self,
        cols: int,
        rows: int,
        health: str,
        frame: str = "",
    ) -> tuple[int, int]:
        return grow_geometry(cols, rows, health, frame)

    def adapt(
        self,
        session: Any,
        plan: GeometryPlan,
        *,
        settle_s: float = 0.35,
        endpoint_id: str = "",
        surface: str | None = None,
    ) -> tuple[FitResult, dict[str, Any]]:
        """Settle + optional grow on an open *session*.

        Returns ``(FitResult, last_shot_dict)``. Session cols/rows updated
        in place when growing. Never shrinks.
        """
        steps = 0
        health = "healthy"
        max_steps = 0 if (plan.forced or not plan.fit_enabled) else self.max_grow_steps
        last_shot: dict[str, Any] = {}
        surf = surface if surface is not None else getattr(session, "surface", None)

        for step in range(max_steps + 1):
            # First pass: full settle; after grow: shorter re-paint window.
            settle = settle_s if step == 0 else min(0.35, max(0.15, settle_s))
            last_shot = session.shot(settle_s=settle)
            frame = last_shot.get("frame") or ""
            cols = int(session.cols)
            rows = int(session.rows)

            if plan.forced:
                result = FitResult(
                    cols=cols,
                    rows=rows,
                    cmd_class=plan.cmd_class,
                    seed_cols=plan.seed_cols,
                    seed_rows=plan.seed_rows,
                    steps=0,
                    fit="forced",
                    health="healthy",
                    forced=True,
                )
                return result, last_shot

            if not plan.fit_enabled:
                result = FitResult(
                    cols=cols,
                    rows=rows,
                    cmd_class=plan.cmd_class,
                    seed_cols=plan.seed_cols,
                    seed_rows=plan.seed_rows,
                    steps=0,
                    fit="ok",
                    health=self.assess(frame, cols, rows, plan.cmd_class, surface=surf),
                    forced=False,
                )
                return result, last_shot

            health = self.assess(frame, cols, rows, plan.cmd_class, surface=surf)
            if health == "healthy":
                if endpoint_id:
                    self.memory.put(
                        endpoint_id,
                        plan.cmd_class,
                        plan.command_basename,
                        cols,
                        rows,
                        healthy=True,
                    )
                return (
                    FitResult(
                        cols=cols,
                        rows=rows,
                        cmd_class=plan.cmd_class,
                        seed_cols=plan.seed_cols,
                        seed_rows=plan.seed_rows,
                        steps=steps,
                        fit="ok",
                        health=health,
                        forced=False,
                    ),
                    last_shot,
                )

            if step >= max_steps:
                break

            new_c, new_r = self.grow(cols, rows, health, frame)
            # Must not shrink; if no progress, stop as poor
            if new_c <= cols and new_r <= rows:
                break
            # Also stop if clamp prevented growth
            if new_c == cols and new_r == rows:
                break
            session.resize(new_c, new_r)
            steps += 1

        # Exhausted grow budget or stuck
        cols = int(session.cols)
        rows = int(session.rows)
        if not last_shot:
            last_shot = session.shot(settle_s=0.0)
        fit = "ok" if health == "healthy" else "poor"
        if fit == "ok" and endpoint_id:
            self.memory.put(
                endpoint_id,
                plan.cmd_class,
                plan.command_basename,
                cols,
                rows,
                healthy=True,
            )
        return (
            FitResult(
                cols=cols,
                rows=rows,
                cmd_class=plan.cmd_class,
                seed_cols=plan.seed_cols,
                seed_rows=plan.seed_rows,
                steps=steps,
                fit=fit,
                health=health,
                forced=False,
            ),
            last_shot,
        )


def default_adapter(home: Path | str | None = None) -> GeometryAdapter:
    """Adapter with optional on-disk memory under *home*/state/."""
    return GeometryAdapter(memory=GeometryMemory.for_home(home))
