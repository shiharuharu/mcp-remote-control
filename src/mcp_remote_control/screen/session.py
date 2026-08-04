"""ScreenSession: PTY handle + pyte buffer + generation/cwd metadata.

Feeds PTY bytes through an incremental UTF-8 decoder so multi-byte characters
split across read chunks reassemble correctly before entering pyte.
"""

from __future__ import annotations

import codecs
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pyte

from mcp_remote_control.screen.buffer import (
    SEED_SHELL_COLS,
    SEED_SHELL_ROWS,
    clamp_geometry,
    cursor_rc,
    dump_frame,
    format_cur,
    frame_hash,
)


@runtime_checkable
class PtyHandle(Protocol):
    cols: int
    rows: int
    cwd: str | None

    def is_alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...

    def read(self, max_bytes: int = 8192) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def drain_for(self, seconds: float, *, on_data: Any | None = None) -> int: ...

    def close(self) -> None: ...


@dataclass
class ScreenSession:
    """One interactive screen (remote or local PTY + ScreenBuffer)."""

    id: str
    ep: str
    pty: PtyHandle
    cols: int
    rows: int
    cwd: str | None = None
    generation: int = 0
    open_mode: str = "shell"  # shell | exec
    label: str | None = None
    surface: str = "shell"
    created_at: float = field(default_factory=time.time)
    last_hash: str | None = None
    # Shell dialect from endpoint probe or open-time shell= hint.
    dialect: str | None = None
    shell_path: str | None = None
    shell_caps: dict[str, Any] | None = None
    # cwd provenance for Agent meta: probe | heuristic | stale | open | None
    cwd_src: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # pyte objects created in __post_init__
    screen: Any = field(init=False, repr=False)
    stream: Any = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    # Incremental UTF-8 decoder: multi-byte chars (CJK, emoji, box-drawing)
    # split across read boundaries reassemble instead of one U+FFFD per chunk.
    _decoder: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.cols, self.rows = clamp_geometry(self.cols, self.rows)
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.Stream(self.screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, data: bytes | str) -> None:
        if not data:
            return
        if isinstance(data, bytes):
            # Incremental decode: only the residual partial-sequence carries
            # across chunks, so a split multi-byte char decodes correctly once
            # its final bytes arrive (instead of U+FFFD per chunk).
            text = self._decoder.decode(data)
        else:
            text = data
        if text:
            self.stream.feed(text)
        self.generation += 1

    def drain(self, settle_s: float = 0.25) -> int:
        """Pull PTY output into pyte for up to *settle_s* seconds."""
        if self._closed:
            return 0

        def _on(chunk: bytes) -> None:
            self.feed(chunk)

        return self.pty.drain_for(settle_s, on_data=_on)

    def shot(
        self,
        *,
        settle_s: float = 0.0,
        strip_trailing_empty: bool = True,
    ) -> dict[str, Any]:
        """Optional settle + full frame dump + cursor meta."""
        if settle_s > 0:
            self.drain(settle_s)
        frame = dump_frame(
            self.screen,
            trim_trailing_ws=True,
            strip_trailing_empty=strip_trailing_empty,
        )
        h = frame_hash(frame)
        self.last_hash = h
        r, c = cursor_rc(self.screen)
        alive = (not self._closed) and self.pty.is_alive()
        return {
            "frame": frame,
            "hash": h,
            "cur": f"{r},{c}",
            "cur_tuple": (r, c),
            "cols": self.cols,
            "rows": self.rows,
            "gen": self.generation,
            "alive": alive,
            "exit": None if alive else self.pty.exit_code(),
        }

    def write(self, data: bytes | str) -> int:
        if self._closed:
            raise RuntimeError(f"screen {self.id} is closed")
        if isinstance(data, str):
            raw = data.encode("utf-8")
        else:
            raw = data
        return self.pty.write(raw)

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = clamp_geometry(cols, rows)
        self.cols = cols
        self.rows = rows
        self.screen.resize(rows, cols)
        self.pty.resize(cols, rows)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.pty.close()
        except Exception:  # noqa: BLE001
            pass

    def cur_token(self) -> str:
        return format_cur(self.screen)


def default_shell_geometry(
    *,
    cols: int | None = None,
    rows: int | None = None,
) -> tuple[int, int]:
    """Resolve open geometry (shell seed; explicit cols/rows override)."""
    if cols is not None and rows is not None:
        return clamp_geometry(cols, rows)
    return clamp_geometry(SEED_SHELL_COLS, SEED_SHELL_ROWS)
