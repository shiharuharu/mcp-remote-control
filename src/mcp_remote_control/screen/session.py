"""ScreenSession: PTY handle + pyte buffer + generation/cwd metadata.

Feeds PTY bytes through an incremental decoder so multi-byte characters split
across read chunks reassemble correctly before entering pyte. The codec is the
peer console's, taken from the transport at open (``text_encoding``); unset
means utf-8, and an unusable name falls back to utf-8 with a warning instead
of raising mid-frame. The same codec encodes literal text written as ``str`` -
what this console reads, it also writes.

The codec is pinned for the whole session, and the frame mirrors the *peer
console*: bytes an app writes in a different codec than the console's show
here the way that console shows them - a UTF-8-emitting child on a cp936
console renders as mojibake, because the console itself renders it that way.
Re-deciding per chunk from the bytes is not an option: legacy codecs and utf-8
accept most of the same byte pairs, so a "valid utf-8" chunk is no evidence
about a stream that is genuinely legacy, and a switch mid-stream would leave
the characters already on screen read the other way round. A console that
really is utf-8 needs no codec at all - that is what unset ``[ssh].encoding``
gets - so the mixed case is answered by configuration, not by sniffing.

Per-session serial ops
----------------------
Each session owns ``_op_lock`` (an :class:`threading.RLock`). Concurrent
FastMCP thread-pool ``screen send`` / ``close`` calls on the **same**
session serialize PTY write, drain, and pyte buffer updates so the action
stream and screen state never interleave. The lock is re-entrant so
``execute_send`` -> ``write`` / ``drain`` / ``feed`` / ``shot`` never
self-deadlock.

``open_screen`` also holds ``serial_ops()`` across settle / geometry adapt
and the multi-step cwd probe (ctrl+u -> cmd -> enter -> marker drain), and only
publishes the session to the registry after ``mark_ready()``. That way a
concurrent send cannot interleave probe marker bytes mid-open.

Registry map locks are a separate layer and must not be held across PTY I/O.
"""

from __future__ import annotations

import codecs
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
from mcp_remote_control.screen.keys import encode_text
from mcp_remote_control.serial.buffer import resolve_text_codec
from mcp_remote_control.transport.base import TransportError


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
    """One interactive screen (remote or local PTY + ScreenBuffer).

    The peer text codec is resolved once, when the session is built: the
    transport's ``text_encoding`` can be re-probed later, but a stream decoder
    cannot switch codecs mid-stream without corrupting the character in
    flight, so a changed value applies to the next open, not to this session.
    """

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
    # Peer text codec (e.g. "gbk" for a cp936 console). None keeps the
    # historic utf-8 read; resolved at construction into ``text_codec``.
    text_encoding: str | None = None
    # pyte objects created in __post_init__
    screen: Any = field(init=False, repr=False)
    stream: Any = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    # Incremental decoder: multi-byte chars (CJK, emoji, box-drawing)
    # split across read boundaries reassemble instead of one U+FFFD per chunk.
    _decoder: Any = field(init=False, repr=False)
    # Codec ``_decoder`` actually runs (canonical, never unusable).
    text_codec: str = field(init=False, repr=False, compare=False)
    # U+FFFD characters the decoder wrote for bytes its codec cannot map; a
    # frame drawn after any of them is not a clean read of ``text_codec``. The
    # count is cumulative for the session, so damage that scrolled off the
    # frame still shows in the ``repl`` token.
    replaced_chars: int = field(default=0, init=False, repr=False, compare=False)
    # Per-session serial lock for send / close / drain / pyte / open-probe.
    # RLock so execute_send -> write/drain/feed/shot re-enter safely (no self-deadlock).
    _op_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )
    # Ready fence: False while open_screen settle/probe runs; True once the
    # session may accept send. Default True so a session registered without
    # the open path is already sendable (no extra mark_ready).
    _ready: bool = field(default=True, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.cols, self.rows = clamp_geometry(self.cols, self.rows)
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.Stream(self.screen)
        # Resolved before the first byte, so an unusable configured name warns
        # once at open and leaves utf-8 in force (see resolve_text_codec).
        self.text_codec = resolve_text_codec(self.text_encoding)
        self._decoder = codecs.getincrementaldecoder(self.text_codec)(errors="replace")

    @property
    def op_lock(self) -> threading.RLock:
        """Per-session lock serializing PTY write / drain / pyte / close."""
        return self._op_lock

    @contextmanager
    def serial_ops(self) -> Iterator[None]:
        """Hold ``_op_lock`` for a multi-step critical section.

        Used by send pipeline, close, and open-path settle/probe. Prefer
        this over touching ``_op_lock`` directly. Nested use from already-locked
        write/drain/feed/shot/close is safe (RLock).
        """
        with self._op_lock:
            yield

    @property
    def ready(self) -> bool:
        """True when settle/probe finished and send is allowed."""
        return self._ready

    def mark_not_ready(self) -> None:
        """Clear the ready fence (open path, before settle/probe)."""
        with self._op_lock:
            self._ready = False

    def mark_ready(self) -> None:
        """Publish-ready: settle/probe complete; send may proceed."""
        with self._op_lock:
            self._ready = True

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, data: bytes | str) -> None:
        if not data:
            return
        with self._op_lock:
            if isinstance(data, bytes):
                # Incremental decode: only the residual partial-sequence carries
                # across chunks, so a split multi-byte char decodes correctly once
                # its final bytes arrive (instead of U+FFFD per chunk). The codec
                # is fixed for the session (see class docstring).
                text = self._decoder.decode(data)
                # What the pinned codec could not map: counted so a frame can
                # report that it came from a decode with replacements.
                self.replaced_chars += text.count("\ufffd")
            else:
                text = data
            if text:
                self.stream.feed(text)
            self.generation += 1

    def is_alive(self) -> bool:
        """True when session is open and the underlying PTY channel is live."""
        return (not self._closed) and self.pty.is_alive()

    def require_alive(self) -> None:
        """Raise ``TransportError(DEAD)`` when the session/PTY is not usable.

        Used by write (and callers that need a fast fail) so dead SSH channels
        surface as ``DEAD`` + session id instead of hanging on wait/drain.
        """
        if self._closed:
            raise TransportError("DEAD", f"DEAD screen {self.id} is closed")
        if not self.pty.is_alive():
            raise TransportError("DEAD", f"DEAD screen {self.id} channel not alive")

    def drain(self, settle_s: float = 0.25) -> int:
        """Pull PTY output into pyte for up to *settle_s* seconds."""
        with self._op_lock:
            if self._closed or not self.pty.is_alive():
                return 0

            def _on(chunk: bytes) -> None:
                # feed() re-enters _op_lock (RLock); keeps pyte update atomic
                # with the surrounding drain/send critical section.
                self.feed(chunk)

            return self.pty.drain_for(settle_s, on_data=_on)

    def shot(
        self,
        *,
        settle_s: float = 0.0,
        strip_trailing_empty: bool = True,
    ) -> dict[str, Any]:
        """Optional settle + full frame dump + cursor meta."""
        with self._op_lock:
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
            alive = self.is_alive()
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
        with self._op_lock:
            # Fast-fail dead channels with DEAD + session id (no 30s wait hang).
            self.require_alive()
            if isinstance(data, str):
                # Literal text is the peer's text: it goes out in the codec this
                # session reads with, so a direct write agrees with the action
                # path (``screen.send`` passes ``text_codec`` to the same
                # encoder). Bytes are already wire bytes and pass through.
                raw = encode_text(data, codec=self.text_codec)
            else:
                raw = data
            try:
                return self.pty.write(raw)
            except TransportError as exc:
                # Normalize PTY-layer death to include session id for Agent greps.
                if exc.code in ("DEAD", "NOT_CONNECTED"):
                    raise TransportError(
                        "DEAD",
                        f"DEAD screen {self.id}: {exc.msg}",
                    ) from exc
                raise

    def resize(self, cols: int, rows: int) -> None:
        with self._op_lock:
            cols, rows = clamp_geometry(cols, rows)
            self.cols = cols
            self.rows = rows
            self.screen.resize(rows, cols)
            self.pty.resize(cols, rows)

    def close(self) -> None:
        # Registry remove / close_ids hold serial_ops() across unregister +
        # this close so a concurrent send cannot observe a live PTY after pop.
        with self._op_lock:
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
