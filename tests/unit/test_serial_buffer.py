"""LineRingBuffer unit tests (no console_ops)."""

from __future__ import annotations

from mcp_remote_control.serial.buffer import LineRingBuffer


def test_line_ring_tail_since_contains() -> None:
    b = LineRingBuffer(max_lines=100)
    b.feed(b"boot\nU-Boot 2020\nLinux starting\n")
    b.feed(b"Kernel panic - not syncing\nmore\n")
    tail = b.view_tail(2, include_partial=False)
    assert [x.text for x in tail] == ["Kernel panic - not syncing", "more"]
    since0 = b.view_since(0)
    assert len(since0) == 5
    mid = since0[1].seq
    assert len(b.view_since(mid)) == 3
    hit, total = b.view_contains("panic", context=1)
    texts = [x.text for x in hit]
    assert total >= 1
    assert "Kernel panic - not syncing" in texts


def test_line_ring_drops_oldest() -> None:
    b = LineRingBuffer(max_lines=3)
    for i in range(5):
        b.feed(f"L{i}\n".encode())
    assert b.line_count == 3
    assert b.dropped_lines == 2
    assert [x.text for x in b.view_tail(10, include_partial=False)] == [
        "L2",
        "L3",
        "L4",
    ]


def test_line_ring_utf8_cross_chunk_cjk_intact() -> None:
    """Multi-byte UTF-8 split across feeds must reassemble (no U+FFFD).

    ``\u4e2d`` = U+4E2D = ``b'\\xe4\\xb8\\xad'``. Per-chunk decode(errors=replace)
    would corrupt; the incremental decoder (same approach as ScreenSession)
    must not.
    """
    b = LineRingBuffer(max_lines=100)
    # Split "\u4e2d" as 1 + 2 bytes across two feeds, then complete the line.
    b.feed(b"\xe4")
    assert b.line_count == 0
    assert "\ufffd" not in b.peek_partial()
    b.feed(b"\xb8\xad hello\n")
    lines = b.view_tail(10, include_partial=False)
    assert len(lines) == 1
    assert lines[0].text == "\u4e2d hello"
    assert "\ufffd" not in lines[0].text

    # Split as 2 + 1 bytes mid-line with surrounding ASCII.
    b2 = LineRingBuffer(max_lines=100)
    b2.feed(b"prefix \xe4\xb8")
    b2.feed(b"\xad suffix\n")
    assert b2.view_tail(1, include_partial=False)[0].text == "prefix \u4e2d suffix"

    # Multi-char CJK stream with arbitrary 1-byte cuts (worst case).
    original = "\u542f\u52a8\u5b8c\u6210 OK\n"
    raw = original.encode("utf-8")
    b3 = LineRingBuffer(max_lines=100)
    for i in range(len(raw)):
        b3.feed(raw[i : i + 1])
    assert b3.view_tail(1, include_partial=False)[0].text == "\u542f\u52a8\u5b8c\u6210 OK"
    assert "\ufffd" not in b3.format_lines(b3.view_tail(10, include_partial=False))


def test_line_ring_utf8_whole_codepoint_and_ascii_unchanged() -> None:
    """Whole-codepoint and ASCII chunks decode without replacement characters."""
    b = LineRingBuffer(max_lines=100)
    b.feed(b"boot\n")
    b.feed("\u4e2d\u6587\n".encode())  # complete code points in one chunk
    b.feed("ascii only\n")  # str path
    tail = b.view_tail(10, include_partial=False)
    assert [x.text for x in tail] == ["boot", "\u4e2d\u6587", "ascii only"]
    assert "\ufffd" not in b.format_lines(tail)


def test_line_ring_utf8_flush_partial_finalizes_residual() -> None:
    """Incomplete trailing multi-byte at teardown becomes replacement + flush."""
    b = LineRingBuffer(max_lines=100)
    b.feed(b"ok \xe4\xb8")  # incomplete "\u4e2d"
    assert b.line_count == 0
    assert b.peek_partial() == "ok "
    b.flush_partial()
    lines = b.view_tail(5, include_partial=False)
    assert len(lines) == 1
    # final=True emits U+FFFD for the unfinished sequence
    assert lines[0].text.startswith("ok ")
    assert "\ufffd" in lines[0].text


def test_view_tail_synth_seq_matches_next_seq(monkeypatch) -> None:
    """view_tail captures _next_seq under the lock for the synthetic partial.

    On an empty buffer the synth seq is ``_next_seq`` (1), not
    ``lines[-1].seq + 1 if lines else 0``. It matches view_since's
    partial-synthetic seq so tail/since stay consistent.
    """
    b = LineRingBuffer(max_lines=100)
    b.feed(b"partial")  # no committed lines, partial="partial", _next_seq=1
    tail = b.view_tail(10)
    assert len(tail) == 1
    assert tail[0].seq == 1  # empty buffer: synth seq is _next_seq
    since = b.view_since(0, include_partial=True)
    assert len(since) == 1
    assert since[0].seq == tail[0].seq  # tail/since synth seqs agree


def test_feed_normalization_large_no_newline_bounded() -> None:
    """Feeding a large no-newline dump does not re-scan the whole partial each
    time; the partial stays bounded by the 1MB flush cap, and lines still
    split correctly when newlines finally arrive.
    """
    b = LineRingBuffer(max_lines=100_000)
    added = b.feed(b"x" * 2_000_000)
    # Cap pushed one 1MB line; partial is bounded at ~1M chars.
    assert added == 1
    assert b.line_count == 1
    assert len(b.peek_partial()) <= 1_000_000
    # Many small no-newline feeds accumulate without re-scanning / splitting.
    b2 = LineRingBuffer(max_lines=100_000)
    for _ in range(1000):
        b2.feed(b"y")
    assert b2.line_count == 0
    assert b2.peek_partial() == "y" * 1000
    b2.feed(b"\n")
    assert b2.line_count == 1
    assert b2.view_tail(1, include_partial=False)[0].text == "y" * 1000


def test_line_ring_byte_budget_caps_no_newline_stream() -> None:
    """Multi-MB no-newline feed cannot grow the ring past max_bytes (+ partial).

    Without a byte budget, 1MB partial flushes x max_lines could approach
    ~100GB. Stored occupancy stays near max_bytes and drop counters
    increase when over cap.
    """
    max_bytes = 2_000_000
    b = LineRingBuffer(max_lines=100_000, max_bytes=max_bytes)
    # 8 MiB of bare bytes -> several 1MB synthetic lines; only a few fit.
    chunk = b"Z" * (1024 * 1024)
    for _ in range(8):
        b.feed(chunk)
    meta = b.snapshot_meta()
    assert meta["stored_bytes"] <= max_bytes + 1_000_000 + 16, meta
    # Must have dropped older flushed lines under the byte budget.
    assert meta["dropped_lines"] >= 1, meta
    assert meta["dropped_bytes"] >= 1_000_000, meta
    assert meta["lines"] < 8, meta  # not all flush lines retained
    # Property API matches snapshot.
    assert b.stored_bytes == meta["stored_bytes"]
    assert b.dropped_lines == meta["dropped_lines"]
    # Newest data still visible (tail of stream).
    tail = b.view_tail(5, include_partial=True)
    assert tail, "ring should retain newest slice"
    joined = b.format_lines(tail)
    assert "Z" in joined


def test_line_ring_byte_budget_drop_oldest_preserves_newlines() -> None:
    """Byte eviction is drop-oldest; newline-delimited lines still work."""
    # Each line is ~100 ASCII bytes; budget holds ~3 lines.
    b = LineRingBuffer(max_lines=100, max_bytes=350)
    for i in range(10):
        b.feed(f"line-{i:04d}-{'x' * 80}\n".encode())
    assert b.line_count >= 1
    assert b.line_count <= 4  # budget ~3 + small slack
    assert b.dropped_lines >= 6
    assert b.dropped_bytes > 0
    texts = [x.text for x in b.view_tail(20, include_partial=False)]
    # Newest lines retained.
    assert texts[-1].startswith("line-0009")
    assert not any(t.startswith("line-0000") for t in texts)
    # Meta exposes both caps.
    meta = b.snapshot_meta()
    assert meta["max_bytes"] == 350
    assert meta["max_lines"] == 100
    assert meta["stored_bytes"] <= 350 + 64  # committed only; no partial


def test_line_ring_line_cap_still_binds_with_byte_budget() -> None:
    """Small max_lines still drops by count when under the byte budget."""
    b = LineRingBuffer(max_lines=3, max_bytes=10_000_000)
    for i in range(5):
        b.feed(f"L{i}\n".encode())
    assert b.line_count == 3
    assert b.dropped_lines == 2
    assert [x.text for x in b.view_tail(10, include_partial=False)] == [
        "L2",
        "L3",
        "L4",
    ]


def test_feed_crlf_cr_normalization_boundary() -> None:
    """CR/CRLF normalize per newly appended chunk.

    Within-feed \\r\\n collapses to one break, a lone \\r is an immediate
    line break, and a \\r\\n split across two feeds yields an empty line
    (the trailing \\r flushes within the first feed).
    """
    b = LineRingBuffer(max_lines=100)
    b.feed(b"a\r\nb\rc\r")  # -> "a", "b", "c"; partial empty (trailing \r->\n flushed)
    b.feed(b"\nd")  # -> "" (empty line from the bare \n), partial "d"
    tail = b.view_tail(10, include_partial=False)
    assert [x.text for x in tail] == ["a", "b", "c", ""]
    assert b.peek_partial() == "d"
    # \r\n across the feed boundary: the trailing \r flushes "line1" within the
    # first feed, then the leading \n flushes an empty line.
    b2 = LineRingBuffer(max_lines=100)
    b2.feed(b"line1\r")
    b2.feed(b"\nline2")
    tail2 = b2.view_tail(10, include_partial=False)
    assert [x.text for x in tail2] == ["line1", ""]
    assert b2.peek_partial() == "line2"


def test_view_tail_with_meta_returns_latest_at_view() -> None:
    """view_tail_with_meta returns (lines, latest_at_view) with
    latest_at_view captured under the same lock as the lines snapshot.
    Incremental follow must use this value, not a later snapshot_meta()."""
    b = LineRingBuffer(max_lines=100)
    b.feed(b"first\n")  # seq 1
    b.feed(b"second")  # partial; _next_seq == 2
    lines, latest_at_view = b.view_tail_with_meta(10)
    assert latest_at_view == 1  # last committed seq at view time
    assert len(lines) == 2  # first(1) + synth partial(2)
    assert lines[0].seq == 1 and lines[0].text == "first"
    assert lines[1].seq == 2 and lines[1].text == "second"  # synth

    # view_since_with_meta mirrors the contract.
    lines2, latest_at_view2 = b.view_since_with_meta(0, include_partial=True)
    assert latest_at_view2 == 1
    assert len(lines2) == 2
    assert lines2[0].seq == 1 and lines2[1].seq == 2


def test_feed_skips_already_committed_marker() -> None:
    """Bytes already committed under the RX order lock do not double-ingest."""

    class _Committed(bytes):
        _mrc_rx_committed = True

    b = LineRingBuffer(max_lines=100)
    assert b.feed(_Committed(b"ABC\n")) == 0
    assert b.line_count == 0
    assert b.feed(b"ABC\n") == 1
    assert b.view_tail(1, include_partial=False)[0].text == "ABC"
