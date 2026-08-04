"""Line-oriented capture buffer for open console sessions.

Default capacity is large (~1e5 lines): observability over thrift. Agent
``views`` queries this buffer; a one-shot driver read is never the sole API.

Thread-safe: the background CapturePump feeds from one thread while
views/send read under the same lock.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class BufLine:
    seq: int
    text: str


class LineRingBuffer:
    """Append-only (drop-oldest) line ring; seq is monotonic for ``since=``."""

    def __init__(self, max_lines: int = 99_999) -> None:
        self.max_lines = max(1, int(max_lines))
        self._lines: deque[BufLine] = deque()
        self._partial = ""
        self._next_seq = 1
        self.dropped_lines = 0
        self.total_bytes = 0
        self._lock = threading.RLock()

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

    def feed(self, data: bytes | str) -> int:
        """Ingest raw bytes/text; return number of complete lines added."""
        if not data:
            return 0
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = data
        with self._lock:
            self.total_bytes += len(text.encode("utf-8", errors="replace"))
            # Normalize only the newly appended chunk (O(chunk)), not the
            # whole accumulated partial. A bare-byte dump can grow the partial
            # toward the 1MB flush cap; re-scanning the entire partial on every
            # small feed would be quadratic.
            #
            # CR/CRLF are folded to LF in the chunk that introduces them. A
            # split across feeds (chunk ends with CR, next starts with LF)
            # produces a spurious empty line: the trailing CR flushes within
            # the first feed, then the leading LF flushes empty in the second.
            norm = text.replace("\r\n", "\n").replace("\r", "\n")
            self._partial = self._partial + norm
            added = 0
            while True:
                i = self._partial.find("\n")
                if i < 0:
                    break
                line = self._partial[:i]
                self._partial = self._partial[i + 1 :]
                self._push_unlocked(line)
                added += 1
            if len(self._partial) > 1_000_000:
                self._push_unlocked(self._partial[:1_000_000] + "…")
                self._partial = self._partial[1_000_000:]
                added += 1
            return added

    def _push_unlocked(self, line: str) -> None:
        seq = self._next_seq
        self._next_seq += 1
        self._lines.append(BufLine(seq=seq, text=line))
        while len(self._lines) > self.max_lines:
            self._lines.popleft()
            self.dropped_lines += 1

    def flush_partial(self) -> None:
        with self._lock:
            if self._partial:
                self._push_unlocked(self._partial)
                self._partial = ""

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
        value for the follow cursor — not a separate ``snapshot_meta()`` call.
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
                "max_lines": self.max_lines,
            }
