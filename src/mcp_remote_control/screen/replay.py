"""PTY fixture replay for CI - no live TUI required.

Load recorded ANSI/PTY bytes, feed a pyte buffer, return frame + hash + cur.
Optional JSON meta next to the fixture can pin expected hash / substrings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyte

from mcp_remote_control.screen.buffer import (
    SEED_SHELL_COLS,
    SEED_SHELL_ROWS,
    clamp_geometry,
    cursor_rc,
    dump_frame,
    frame_hash,
)


def _default_fixture_dir() -> Path:
    """Locate tests/fixtures/pty from an editable checkout when present."""
    here = Path(__file__).resolve()
    # repo root/src/mcp_remote_control/screen/replay.py -> parents[3] = repo root
    for parent in here.parents:
        cand = parent / "tests" / "fixtures" / "pty"
        if cand.is_dir():
            return cand
    return here.parents[3] / "tests" / "fixtures" / "pty"


DEFAULT_FIXTURE_DIR = _default_fixture_dir()


@dataclass
class ReplayResult:
    """Outcome of replaying recorded PTY output into a ScreenBuffer."""

    frame: str
    hash: str
    cur: str
    cols: int
    rows: int
    gen: int = 1
    path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    expect_ok: bool | None = None
    expect_detail: str | None = None


def load_bytes(path: Path | str) -> bytes:
    p = Path(path)
    return p.read_bytes()


def load_meta(path: Path | str) -> dict[str, Any]:
    """Load optional ``*.meta.json`` beside a fixture (or the path itself if .json)."""
    p = Path(path)
    candidates = []
    if p.suffix == ".json":
        candidates.append(p)
    else:
        candidates.append(p.with_suffix(p.suffix + ".meta.json"))
        candidates.append(p.with_name(p.name + ".meta.json"))
        candidates.append(p.with_suffix(".meta.json"))
        # e.g. bash_prompt.bin -> bash_prompt.meta.json
        candidates.append(p.with_name(p.stem + ".meta.json"))
    for c in candidates:
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
    return {}


# Soft caps for recorded fixtures (may be narrower than production MIN).
MAX_SOFT_COLS = 300
MAX_SOFT_ROWS = 120


def replay_ansi(
    data: bytes | str,
    *,
    cols: int | None = None,
    rows: int | None = None,
    strip_probe: bool = True,
) -> ReplayResult:
    """Feed *data* into a fresh pyte screen and return frame/hash/cur."""
    g_cols = int(cols) if cols is not None else SEED_SHELL_COLS
    g_rows = int(rows) if rows is not None else SEED_SHELL_ROWS
    # Fixture replay may use sizes below production MIN_* (recorded labs).
    g_cols = max(20, min(MAX_SOFT_COLS, g_cols))
    g_rows = max(5, min(MAX_SOFT_ROWS, g_rows))

    screen = pyte.Screen(g_cols, g_rows)
    stream = pyte.Stream(screen)
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data
    stream.feed(text)

    frame = dump_frame(
        screen,
        trim_trailing_ws=True,
        strip_trailing_empty=True,
        strip_probe=strip_probe,
    )
    h = frame_hash(frame)
    r, c = cursor_rc(screen)
    return ReplayResult(
        frame=frame,
        hash=h,
        cur=f"{r},{c}",
        cols=g_cols,
        rows=g_rows,
        gen=1,
    )


def replay_fixture(
    path: Path | str,
    *,
    cols: int | None = None,
    rows: int | None = None,
) -> ReplayResult:
    """Load a fixture file (+ optional meta) and replay into a buffer."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"pty fixture not found: {p}")
    meta = load_meta(p)
    use_cols = cols if cols is not None else meta.get("cols")
    use_rows = rows if rows is not None else meta.get("rows")
    raw = load_bytes(p)
    result = replay_ansi(raw, cols=use_cols, rows=use_rows)
    result.path = str(p)
    result.meta = meta

    # Optional assertions from meta (CLI replay --check / verification).
    detail_parts: list[str] = []
    ok = True
    expect_hash = meta.get("hash") or meta.get("expect_hash")
    if expect_hash and str(expect_hash) != result.hash:
        ok = False
        detail_parts.append(f"hash want={expect_hash} got={result.hash}")
    for key in ("expect_contains", "contains", "substrings"):
        needles = meta.get(key)
        if needles is None:
            continue
        if isinstance(needles, str):
            needles = [needles]
        for n in needles:
            if str(n) not in result.frame:
                ok = False
                detail_parts.append(f"missing substring {n!r}")
    if meta:
        result.expect_ok = ok if (expect_hash or any(
            meta.get(k) for k in ("expect_contains", "contains", "substrings")
        )) else None
        result.expect_detail = "; ".join(detail_parts) if detail_parts else None
    return result


def format_replay_agent_text(result: ReplayResult) -> str:
    """Agent-track-ish line for ``mcp-remote-control-cli replay`` stdout."""
    parts = [
        f"@replay ok cols={result.cols} rows={result.rows} cur={result.cur} hash={result.hash}",
    ]
    if result.path:
        parts[0] += f" fixture={Path(result.path).name}"
    if result.expect_ok is False:
        parts[0] = parts[0].replace("@replay ok", "@replay fail", 1)
        if result.expect_detail:
            parts.append(f"| {result.expect_detail}")
    elif result.expect_ok is True:
        parts.append("| expect=ok")
    body = result.frame
    return "\n".join(parts) + ("\n\n" + body if body else "")


def format_replay_json(result: ReplayResult) -> str:
    payload: dict[str, Any] = {
        "kind": "replay",
        "status": "ok" if result.expect_ok is not False else "fail",
        "cols": result.cols,
        "rows": result.rows,
        "cur": result.cur,
        "hash": result.hash,
        "frame": result.frame,
    }
    if result.path:
        payload["fixture"] = result.path
    if result.expect_ok is not None:
        payload["expect_ok"] = result.expect_ok
    if result.expect_detail:
        payload["expect_detail"] = result.expect_detail
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def resolve_fixture_path(name_or_path: str, *, base: Path | None = None) -> Path:
    """Resolve a fixture path; bare names search tests/fixtures/pty."""
    p = Path(name_or_path)
    if p.is_file():
        return p.resolve()
    root = base or DEFAULT_FIXTURE_DIR
    candidate = root / name_or_path
    if candidate.is_file():
        return candidate.resolve()
    # Try with .bin
    if not name_or_path.endswith(".bin"):
        c2 = root / f"{name_or_path}.bin"
        if c2.is_file():
            return c2.resolve()
    raise FileNotFoundError(f"pty fixture not found: {name_or_path}")


# Keep clamp available for callers that want production geometry.
def production_geometry(cols: int, rows: int) -> tuple[int, int]:
    return clamp_geometry(cols, rows)
