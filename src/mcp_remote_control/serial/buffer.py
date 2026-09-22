"""Line-oriented capture buffer for open console sessions.

Default capacity is large (~1e5 lines): observability over thrift. Agent
``views`` queries this buffer; a one-shot driver read is never the sole API.

In addition to the line-count cap, a total **stored-byte** budget bounds
committed line text plus the incomplete partial. No-newline streams (flash
dumps, binary telemetry) flush ~1MB chunks as synthetic lines; without a
byte budget those lines alone could approach ``max_lines * 1MB`` of RAM.

Wire bytes are decoded incrementally, because a multi-byte character can
split across driver reads. The codec is the peer's, passed at construction
(``text_encoding``); unset means utf-8, the historic read.

Thread-safe: the background CapturePump feeds from one thread while
views/send read under the same lock.
"""

from __future__ import annotations

import codecs
import logging
import threading
from collections import deque
from dataclasses import dataclass

from mcp_remote_control.codec import charmap_to_codec

_log = logging.getLogger(__name__)

# Default stored-byte budget: plenty for normal newline-delimited console
# logs under max_lines; hard-caps no-newline 1MB flush storms.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# Partial without newline is flushed as a synthetic line at this char count
# (then re-accumulated). Truncated lines count toward the byte budget.
_PARTIAL_FLUSH_CHARS = 1_000_000

# Byte string the stream-codec probe decodes: a NUL (legal in a PTY read), a
# complete multi-byte UTF-8 character and a lone high byte, which every
# byte-oriented codec consumes under ``errors="replace"``.
_CODEC_PROBE_BYTES = b"\x00\xe4\xb8\xad\xff"


def _utf8_nbytes(text: str) -> int:
    """UTF-8 size of *text* (budget unit for ring occupancy)."""
    return len(text.encode("utf-8", errors="replace"))


def _decodes_stream(codec: str) -> bool:
    """Can *codec* consume arbitrary stream bytes under ``errors="replace"``?

    The boundary decodes bytes chunk by chunk, so a name that only transforms
    whole strings is not usable here even though ``str.encode`` accepts it:
    utf-16/utf-32 raise without a leading BOM, and idna/punycode reject raw
    bytes instead of mapping them. Probed with the construction the boundary
    itself uses, on bytes a terminal/console stream can contain.
    """
    try:
        codecs.getincrementaldecoder(codec)(errors="replace").decode(
            _CODEC_PROBE_BYTES
        )
        return True
    except (LookupError, UnicodeError, ValueError):
        return False


def text_codec_known(name: str | None) -> bool:
    """True when *name* resolves to a codec this boundary can use (unset: utf-8).

    Callers use this to tell a peer that really is utf-8 from a name the
    boundary had to fall back to, so a bad configured value can be reported
    instead of looking like a clean utf-8 read. ``ascii`` counts as known: it
    resolves to utf-8 by the subset rule below, not by fallback.
    """
    text = "" if name is None else str(name).strip()
    if not text:
        return True
    try:
        codec = charmap_to_codec(text)
    except (LookupError, UnicodeError, ValueError):
        # The stdlib 'undefined' codec raises on lookup rather than returning
        # None when the name is probed with the empty-string encode below.
        return False
    if codec is None:
        return False
    if codec == "ascii":
        return True
    return _decodes_stream(codec)


def resolve_text_codec(name: str | None) -> str:
    """Resolve a configured peer codec to a Python codec name; never raises.

    Shared by both byte->text boundaries - the screen session's stream decoder
    and this ring - so one configured value (``[ssh].encoding``, a probe's
    ``charmap``/``chcp``) decodes the same way everywhere and spellings like
    ``gbk``/``cp936`` land on the canonical codec the transport already uses.

    Unset or empty means utf-8, the historic read. A name that is not a codec,
    a name that only transforms whole strings (utf-16 needs its BOM, idna and
    punycode reject raw bytes) and the stdlib ``undefined`` codec (which raises
    on lookup) are all configuration errors that must not break a live stream,
    so each is resolved once at construction/open, warns here, and leaves utf-8
    in force instead of raising on the first byte. ``ascii`` also resolves to
    utf-8: see the subset rule below.

    The resolved codec is the one and only reading for the whole session: it
    mirrors the peer console, so a byte stream the peer's code page would show
    as mojibake shows as mojibake here too (see ScreenSession.feed).
    """
    text = "" if name is None else str(name).strip()
    if not text:
        return "utf-8"
    try:
        codec = charmap_to_codec(text)
    except (LookupError, UnicodeError, ValueError):
        codec = None
    if codec is None:
        _log.warning(
            "unknown text codec %r: peer bytes will be read as utf-8 "
            "(errors=replace); use a Python codec name such as gb18030",
            name,
        )
        return "utf-8"
    if codec == "ascii":
        # ascii is a strict subset of utf-8, so pinning it would only add
        # replacement characters: every byte it accepts reads the same through
        # utf-8, and its rejection of high bytes replaces characters the
        # historic utf-8 read decodes. A controller locale of C resolves here.
        return "utf-8"
    if not _decodes_stream(codec):
        _log.warning(
            "text codec %r is not a byte-stream decoder (it transforms whole "
            "strings, or needs a BOM): peer bytes will be read as utf-8 "
            "(errors=replace); use a Python codec name such as gb18030",
            name,
        )
        return "utf-8"
    return codec


@dataclass(frozen=True)
class BufLine:
    seq: int
    text: str


class LineRingBuffer:
    """Append-only (drop-oldest) line ring; seq is monotonic for ``since=``."""

    def __init__(
        self,
        max_lines: int = 99_999,
        max_bytes: int = DEFAULT_MAX_BYTES,
        text_encoding: str | None = None,
    ) -> None:
        self.max_lines = max(1, int(max_lines))
        self.max_bytes = max(1, int(max_bytes))
        self._lines: deque[BufLine] = deque()
        # Parallel UTF-8 sizes so drop-oldest is O(1) (no re-encode on eviction).
        self._line_nbytes: deque[int] = deque()
        self._partial = ""
        self._partial_nbytes = 0
        self._stored_bytes = 0  # sum of committed line UTF-8 sizes
        self._next_seq = 1
        self.dropped_lines = 0
        self.dropped_bytes = 0
        # Peer text codec, resolved once: the boundary never re-reads the
        # configuration per chunk, so a peer cannot change codec mid-stream.
        self.text_encoding = resolve_text_codec(text_encoding)
        self._lock = threading.RLock()
        # Incremental decode in the peer's codec: multi-byte characters may
        # split across driver read boundaries. Per-chunk decode(...,
        # errors="replace") would emit U+FFFD for each incomplete fragment.
        # Residual incomplete sequences stay in the decoder until the
        # completing bytes arrive.
        self._decoder = codecs.getincrementaldecoder(self.text_encoding)(
            errors="replace"
        )

    @property
    def line_count(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def latest_seq(self) -> int:
        with self._lock:
            if not self._lines:
                return max(0, self._next_seq - 1)
            return self._lines[-1].seq

    @property
    def stored_bytes(self) -> int:
        """Committed lines + partial UTF-8 occupancy (under lock)."""
        with self._lock:
            return self._stored_bytes + self._partial_nbytes

    def feed(self, data: bytes | str) -> int:
        """Ingest raw bytes/text; return number of complete lines added."""
        if not data:
            return 0
        # ``SerialConsole.read()`` already committed this chunk under the
        # RX order lock; the caller's follow-up feed is a no-op.
        if getattr(data, "_mrc_rx_committed", False):
            return 0
        with self._lock:
            if isinstance(data, bytes):
                # Decode under lock: residual multi-byte state is mutable and
                # must not race a concurrent feed (pump + brief snarf).
                text = self._decoder.decode(data)
            else:
                text = data
            if not text:
                return 0
            # Normalize only the newly appended chunk (O(chunk)), not the
            # whole partial: a bare-byte dump can grow it toward the 1MB flush
            # cap, so re-scanning on every small feed would be quadratic.
            #
            # CR/CRLF are folded to LF in the chunk that introduces them. A
            # split across feeds (chunk ends with CR, next starts with LF)
            # produces a spurious empty line: the trailing CR flushes within
            # the first feed, then the leading LF flushes empty in the second.
            norm = text.replace("\r\n", "\n").replace("\r", "\n")
            self._partial = self._partial + norm
            self._partial_nbytes += _utf8_nbytes(norm)
            added = 0
            while True:
                i = self._partial.find("\n")
                if i < 0:
                    break
                line = self._partial[:i]
                line_n = _utf8_nbytes(line)
                self._partial = self._partial[i + 1 :]
                # "\n" is always 1 UTF-8 byte after CR normalization.
                self._partial_nbytes = max(0, self._partial_nbytes - line_n - 1)
                self._push_unlocked(line, line_n)
                added += 1
            if len(self._partial) > _PARTIAL_FLUSH_CHARS:
                head = self._partial[:_PARTIAL_FLUSH_CHARS]
                self._partial = self._partial[_PARTIAL_FLUSH_CHARS:]
                head_n = _utf8_nbytes(head)
                self._partial_nbytes = max(0, self._partial_nbytes - head_n)
                # Truncation marker; counts toward the byte budget.
                ellipsis = "\u2026"
                self._push_unlocked(head + ellipsis, head_n + _utf8_nbytes(ellipsis))
                added += 1
            # Partial-only growth can push committed+partial over the budget;
            # drop oldest committed lines (partial itself stays until flush).
            self._evict_unlocked()
            return added

    def _push_unlocked(self, line: str, nbytes: int | None = None) -> None:
        n = _utf8_nbytes(line) if nbytes is None else int(nbytes)
        seq = self._next_seq
        self._next_seq += 1
        self._lines.append(BufLine(seq=seq, text=line))
        self._line_nbytes.append(n)
        self._stored_bytes += n
        self._evict_unlocked()

    def _drop_oldest_unlocked(self) -> None:
        self._lines.popleft()
        n = self._line_nbytes.popleft()
        self._stored_bytes -= n
        if self._stored_bytes < 0:
            self._stored_bytes = 0
        self.dropped_lines += 1
        self.dropped_bytes += n

    def _evict_unlocked(self) -> None:
        """Drop oldest until under max_lines and (soft) max_bytes.

        Line-count cap is hard. Byte budget drops oldest while more than one
        committed line remains; a single oversized line (e.g. 1MB partial
        flush under a tiny test budget) is retained so the agent still sees
        the newest data. Partial occupancy counts toward the budget but is
        not discarded here - the 1MB flush path commits it as a line.
        """
        while len(self._lines) > self.max_lines:
            self._drop_oldest_unlocked()
        while (
            len(self._lines) > 1
            and (self._stored_bytes + self._partial_nbytes) > self.max_bytes
        ):
            self._drop_oldest_unlocked()

    def flush_partial(self) -> None:
        with self._lock:
            # Session teardown: emit any incomplete code sequence as
            # replacement chars, then reset the decoder for a clean state.
            # Reset to this buffer's own codec - a hardcoded utf-8 reset here
            # would silently switch the peer codec for everything fed after.
            residual = self._decoder.decode(b"", final=True)
            self._decoder = codecs.getincrementaldecoder(self.text_encoding)(
                errors="replace"
            )
            if residual:
                norm = residual.replace("\r\n", "\n").replace("\r", "\n")
                self._partial = self._partial + norm
                self._partial_nbytes += _utf8_nbytes(norm)
            if self._partial:
                self._push_unlocked(self._partial, self._partial_nbytes)
                self._partial = ""
                self._partial_nbytes = 0

    def peek_partial(self) -> str:
        with self._lock:
            return self._partial

    def view_tail(self, n: int = 100, *, include_partial: bool = True) -> list[BufLine]:
        """Return the last *n* lines (compat wrapper; no meta)."""
        lines, _latest = self.view_tail_with_meta(n, include_partial=include_partial)
        return lines

    def view_tail_with_meta(
        self, n: int = 100, *, include_partial: bool = True
    ) -> tuple[list[BufLine], int]:
        """Tail view plus latest committed seq at snapshot time.

        Returns ``(lines, latest_at_view)``. ``latest_at_view`` is the last
        committed seq captured under the **same lock** as the line list.
        Callers that drive incremental follow (``views_console``) must use this
        value for the follow cursor - not a separate ``snapshot_meta()`` call.
        A feed that lands between this method releasing the lock and a later
        ``snapshot_meta()`` would observe a newer ``latest_seq`` than the lines
        snapshot, so ``to_seq`` could skip a line that commits at ``_next_seq``.
        Anchoring the cursor to ``latest_at_view`` keeps it consistent with the
        lines the agent just saw.
        """
        n = max(1, int(n))
        # Hold the lock for the full result: lines, partial, and next_seq.
        # Building a synthetic partial with ``lines[-1].seq + 1`` outside the
        # lock races the pump: a real line can take the same seq, hide under
        # the synth, and be skipped by the next ``since=<to_seq>``.
        with self._lock:
            lines = list(self._lines)
            partial = self._partial if include_partial else ""
            next_seq = self._next_seq
            latest_at_view = lines[-1].seq if lines else max(0, next_seq - 1)
            if n < len(lines):
                lines = lines[-n:]
            if include_partial and partial:
                # Synthetic seq = next (not committed) for display only.
                # Uses locked ``_next_seq`` so it cannot collide with a real
                # line the pump is about to push.
                synth = BufLine(seq=next_seq, text=partial)
                lines = lines + [synth]
                if len(lines) > n >= 1:
                    lines = lines[-n:]
            return lines, latest_at_view

    def view_since(
        self, since_seq: int, *, include_partial: bool = False
    ) -> list[BufLine]:
        """Return lines with seq > *since_seq* (compat wrapper; no meta)."""
        lines, _latest = self.view_since_with_meta(
            since_seq, include_partial=include_partial
        )
        return lines

    def view_since_with_meta(
        self, since_seq: int, *, include_partial: bool = False
    ) -> tuple[list[BufLine], int]:
        """Lines with seq > *since_seq*, plus latest committed seq under lock.

        Same cursor rule as ``view_tail_with_meta``: use ``latest_at_view`` for
        incremental follow, not a later ``snapshot_meta()``.
        """
        s = int(since_seq)
        with self._lock:
            out = [ln for ln in self._lines if ln.seq > s]
            latest_at_view = (
                self._lines[-1].seq if self._lines else max(0, self._next_seq - 1)
            )
            if include_partial and self._partial:
                # Synthetic seq = next (not committed); same locked allocator
                # as view_tail so the pump cannot assign the same seq.
                out = out + [BufLine(seq=self._next_seq, text=self._partial)]
            return out, latest_at_view

    def view_contains(
        self,
        pattern: str,
        *,
        context: int = 3,
        max_hits: int = 20,
    ) -> tuple[list[BufLine], int]:
        """Matching lines plus context. Returns ``(lines, total_hit_count)``."""
        if not pattern:
            return self.view_tail(50), 0
        pat = pattern
        with self._lock:
            all_lines = list(self._lines)
        idxs = [i for i, ln in enumerate(all_lines) if pat in ln.text]
        total_hits = len(idxs)
        if not idxs:
            return [], 0
        ctx = max(0, int(context))
        hit_limit = max(1, int(max_hits))
        chosen: set[int] = set()
        for i in idxs[:hit_limit]:
            for j in range(max(0, i - ctx), min(len(all_lines), i + ctx + 1)):
                chosen.add(j)
        return [all_lines[i] for i in sorted(chosen)], total_hits

    def format_lines(self, lines: list[BufLine], *, with_seq: bool = False) -> str:
        if not lines:
            return ""
        if with_seq:
            return "\n".join(f"{ln.seq}\t{ln.text}" for ln in lines)
        return "\n".join(ln.text for ln in lines)

    def snapshot_meta(self) -> dict[str, int]:
        with self._lock:
            return {
                "lines": len(self._lines),
                "latest_seq": (
                    self._lines[-1].seq if self._lines else max(0, self._next_seq - 1)
                ),
                "dropped_lines": self.dropped_lines,
                "dropped_bytes": self.dropped_bytes,
                "max_lines": self.max_lines,
                "max_bytes": self.max_bytes,
                "stored_bytes": self._stored_bytes + self._partial_nbytes,
            }
