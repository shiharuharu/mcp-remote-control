"""Unit tests: peer codec at the screen/serial byte->text boundary.

The screen session and the serial ring both decode wire bytes into pyte /
line text. Unconfigured, that boundary reads utf-8 with ``errors="replace"``
(the historic behaviour); configured with the peer's codec it must decode a
GBK-emitting console instead of rendering U+FFFD, keep a multi-byte character
that splits across reads intact, and fall back to utf-8 with a warning when
the configured name is not a codec at all.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.screen.buffer import dump_frame
from mcp_remote_control.screen.keys import PASTE_END, PASTE_START
from mcp_remote_control.screen.registry import (
    get_screen_registry,
    reset_screen_registry,
)
from mcp_remote_control.screen.session import ScreenSession
from mcp_remote_control.serial.buffer import (
    LineRingBuffer,
    resolve_text_codec,
    text_codec_known,
)

_CODE_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_HOME = _CODE_ROOT / "tests" / "fixtures" / "config"

# \u8df3\u8fc7 as a cp936 console writes it.
GBK_TEXT = "\u8df3\u8fc7"
GBK_BYTES = GBK_TEXT.encode("gbk")  # b'\xcc\xf8\xb9\xfd'
GBK_AS_UTF8 = GBK_BYTES.decode("utf-8", errors="replace")

UTF8_TEXT = "\u4e2d\u6587"
UTF8_BYTES = UTF8_TEXT.encode("utf-8")


class FakePty:
    """PtyHandle stub: one seeded chunk per ``read``/``drain_for`` pull."""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.cols = 80
        self.rows = 24
        self.cwd: str | None = "/tmp"
        self._chunks = list(chunks or [])
        self._alive = True
        self.written = bytearray()

    def feed_chunk(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None if self._alive else 0

    def read(self, max_bytes: int = 8192) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: Any | None = None) -> int:
        total = 0
        while self._chunks:
            chunk = self._chunks.pop(0)
            total += len(chunk)
            if on_data is not None:
                on_data(chunk)
        return total

    def close(self) -> None:
        self._alive = False


def _session(*chunks: bytes, text_encoding: str | None = None) -> ScreenSession:
    return ScreenSession(
        id="scr_codec",
        ep="local",
        pty=FakePty(list(chunks)),  # type: ignore[arg-type]
        cols=80,
        rows=24,
        text_encoding=text_encoding,
    )


def _frame(session: ScreenSession) -> str:
    return dump_frame(session.screen, trim_trailing_ws=True)


# ---------------------------------------------------------------------------
# codec resolution
# ---------------------------------------------------------------------------


def test_resolve_text_codec_defaults_and_aliases() -> None:
    assert resolve_text_codec(None) == "utf-8"
    assert resolve_text_codec("") == "utf-8"
    assert resolve_text_codec("  ") == "utf-8"
    assert resolve_text_codec("utf-8") == "utf-8"
    # Host-local spellings land on the canonical codec the transport uses.
    assert resolve_text_codec("gbk") == "gb18030"
    assert resolve_text_codec("GBK") == "gb18030"
    assert resolve_text_codec("cp936") == "gb18030"
    assert resolve_text_codec("big5") == "big5"


def test_resolve_text_codec_keeps_utf8_for_ascii() -> None:
    """ascii is a subset of utf-8, so pinning it would only add U+FFFD."""
    assert resolve_text_codec("ascii") == "utf-8"
    # The charmap a C-locale controller reports.
    assert resolve_text_codec("ANSI_X3.4-1968") == "utf-8"


def test_resolve_text_codec_unknown_warns_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert resolve_text_codec("gbk2") == "utf-8"
    assert any(
        "unknown text codec" in rec.message and "gbk2" in rec.message
        for rec in caplog.records
    ), caplog.text
    assert text_codec_known(None)
    assert text_codec_known("gbk")
    assert not text_codec_known("gbk2")


def test_resolve_text_codec_rejects_names_that_cannot_read_a_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A name that transforms whole strings must not be pinned either.

    utf-16/utf-32 need a leading BOM and idna/punycode reject raw bytes; all
    four are accepted by ``str.encode`` and would raise out of the first feed
    (breaking the live stream mid-frame) instead of falling back. ``undefined``
    raises during resolution itself. Each must resolve to utf-8 with a warning,
    so a configured value is reported rather than looking like a clean read.
    """
    for name in ("utf-16", "utf-32", "idna", "punycode", "undefined"):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert resolve_text_codec(name) == "utf-8", name
        assert caplog.records, name
        assert not text_codec_known(name), name
    # utf-8's own sibling spellings stay usable, and ascii keeps its rule.
    assert text_codec_known("utf-8") is True
    assert text_codec_known("ascii") is True


def test_screen_session_unknown_codec_warns_and_stays_utf8(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        sess = _session(GBK_BYTES + b"\r\n", text_encoding="not-a-codec")
    assert sess.text_codec == "utf-8"
    assert any("unknown text codec" in rec.message for rec in caplog.records)
    sess.drain(0.0)
    # Session stays usable: the historic utf-8/replace read is still in force.
    assert _frame(sess).strip() == GBK_AS_UTF8


# ---------------------------------------------------------------------------
# screen session decode
# ---------------------------------------------------------------------------


def test_screen_session_default_decoder_is_unchanged() -> None:
    """No configured codec: utf-8/replace, split utf-8 char reassembled."""
    sess = _session(UTF8_BYTES + b"\r\n")
    assert sess.text_codec == "utf-8"
    sess.drain(0.0)
    assert _frame(sess).strip() == UTF8_TEXT

    split = _session(UTF8_BYTES[:1], UTF8_BYTES[1:] + b"\r\n")
    split.drain(0.0)
    assert _frame(split).strip() == UTF8_TEXT

    garbled = _session(GBK_BYTES + b"\r\n")
    garbled.drain(0.0)
    assert _frame(garbled).strip() == GBK_AS_UTF8


def test_screen_session_decodes_with_peer_codec() -> None:
    sess = _session(GBK_BYTES + b"\r\n", text_encoding="gbk")
    assert sess.text_codec == "gb18030"
    sess.drain(0.0)
    assert _frame(sess).strip() == GBK_TEXT


def test_screen_session_legacy_codec_keeps_the_console_reading() -> None:
    """A pinned legacy codec is a mirror of the peer console, UTF-8 or not.

    UTF-8 bytes from an app inside a cp936 console show as mojibake on that
    console, so the frame mirrors that reading rather than silently re-reading
    the stream as utf-8: gb18030 accepts the same bytes, so nothing in a chunk
    can tell the two apart, and a mid-stream switch would re-read characters
    already on the frame. What the frame *was* read with is reported instead
    (``encoding`` token on every frame row), and a console that really is
    utf-8 is served by leaving the codec unset (see the default-codec tests).
    """
    sess = _session(UTF8_BYTES + b"\r\n", text_encoding="gbk")
    sess.drain(0.0)
    frame = _frame(sess).strip()
    assert frame == UTF8_BYTES.decode("gb18030")
    assert frame != UTF8_TEXT
    # No replacement characters: the legacy reading is complete, which is why
    # it cannot announce itself through U+FFFD.
    assert "\ufffd" not in frame
    assert sess.replaced_chars == 0


def test_screen_session_unusable_codec_keeps_the_stream_alive() -> None:
    """A name that cannot decode bytes falls back instead of breaking the feed."""
    sess = _session(GBK_BYTES + b"\r\n", text_encoding="utf-16")
    assert sess.text_codec == "utf-8"
    sess.drain(0.0)  # pre-fix: UnicodeError out of the first feed
    assert _frame(sess).strip() == GBK_AS_UTF8
    # The replacements the fallback read produced are counted for the frame row.
    assert sess.replaced_chars == GBK_AS_UTF8.count("\ufffd")
    assert sess.replaced_chars > 0


def test_screen_session_split_char_survives_with_peer_codec() -> None:
    """A GBK character split across reads must reassemble, not U+FFFD."""
    sess = _session(GBK_BYTES[:1], GBK_BYTES[1:] + b"\r\n", text_encoding="gbk")
    sess.drain(0.0)
    frame = _frame(sess).strip()
    assert frame == GBK_TEXT
    assert "\ufffd" not in frame


def test_probe_strip_still_matches_marker_under_peer_codec() -> None:
    """The cwd-probe strip runs on decoded text: ASCII marker, any codec."""
    path = "/home/\u7528\u6237"
    probe = (
        b" echo __MRC_PWD__:$(pwd -P)\r\n"
        + ("__MRC_PWD__:" + path).encode("gbk")
        + b"\r\nprompt$ "
    )
    sess = _session(probe, text_encoding="gbk")
    sess.drain(0.0)
    # The path is the shell's own output, so it arrives in the peer codec and
    # must decode correctly for the marker row to be recognized and dropped.
    assert path in dump_frame(sess.screen, trim_trailing_ws=True, strip_probe=False)
    stripped = dump_frame(sess.screen, trim_trailing_ws=True, strip_probe=True)
    assert "__MRC_PWD__" not in stripped
    assert "prompt$" in stripped


# ---------------------------------------------------------------------------
# serial ring decode
# ---------------------------------------------------------------------------


def test_line_ring_default_decoder_is_unchanged() -> None:
    ring = LineRingBuffer(max_lines=10)
    assert ring.text_encoding == "utf-8"
    ring.feed(GBK_BYTES + b"\n")
    assert [ln.text for ln in ring.view_tail(5)] == [GBK_AS_UTF8]

    split = LineRingBuffer(max_lines=10)
    split.feed(UTF8_BYTES[:1])
    split.feed(UTF8_BYTES[1:] + b"\n")
    assert [ln.text for ln in split.view_tail(5)] == [UTF8_TEXT]


def test_line_ring_decodes_with_peer_codec() -> None:
    ring = LineRingBuffer(max_lines=10, text_encoding="gbk")
    assert ring.text_encoding == "gb18030"
    # Split across feeds: the residual lead byte is held by the decoder.
    ring.feed(GBK_BYTES[:1])
    assert ring.peek_partial() == ""
    ring.feed(GBK_BYTES[1:] + b"\n")
    assert [ln.text for ln in ring.view_tail(5)] == [GBK_TEXT]


def test_line_ring_flush_partial_keeps_peer_codec() -> None:
    """Teardown reset must restore the configured codec, not utf-8."""
    ring = LineRingBuffer(max_lines=10, text_encoding="gbk")
    ring.feed(GBK_BYTES[:1])  # incomplete character left in the decoder
    ring.flush_partial()
    # A second flush_partial (registry teardown) must not raise, and the ring
    # must still read the peer codec afterwards.
    ring.flush_partial()
    ring.feed(GBK_BYTES + b"\n")
    assert [ln.text for ln in ring.view_tail(5)][-1] == GBK_TEXT


def test_line_ring_unusable_codec_keeps_the_stream_alive() -> None:
    """Same fallback at the ring: a name that cannot decode must not raise."""
    ring = LineRingBuffer(max_lines=10, text_encoding="idna")
    assert ring.text_encoding == "utf-8"
    ring.feed(GBK_BYTES + b"\n")  # pre-fix: UnicodeError out of the first feed
    assert [ln.text for ln in ring.view_tail(5)] == [GBK_AS_UTF8]
    # Teardown on the fallback codec stays quiet too.
    ring.flush_partial()


# ---------------------------------------------------------------------------
# open_screen plumbing (real local PTY; POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="local interactive PTY is POSIX-only",
)
class TestOpenPlumbing:
    """``open_screen`` must feed the transport's codec into the session."""

    home: Path

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        home = tmp_path / "config"
        shutil.copytree(_CONFIG_HOME, home)
        monkeypatch.setenv("MRC_HOME", str(home))
        reset_registry()
        reset_screen_registry()
        self.home = home
        yield
        reset_screen_registry()
        reset_registry()

    def _open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        encoding: str,
        *,
        child_cmd: str = "printf '\\314\\370\\271\\375\\n'",
    ) -> Any:
        """Open a screen whose transport reports *encoding*.

        *child_cmd* defaults to a child writing GBK \u8df3\u8fc7 bytes; an ASCII child
        is what the unconfigured case looks like with no decode loss at all.
        """
        real_ensure = screen_ops.ensure_endpoint

        def _ensure(*args: Any, **kwargs: Any) -> Any:
            endpoint = real_ensure(*args, **kwargs)
            endpoint.transport.text_encoding = encoding
            return endpoint

        monkeypatch.setattr(screen_ops, "ensure_endpoint", _ensure)
        return screen_ops.open_screen(
            ep="local",
            argv=["/bin/sh", "-c", f"{child_cmd}; sleep 0.5"],
            cols=80,
            rows=24,
            fit=False,
            settle_s=0.5,
            home=self.home,
        )

    def test_open_uses_peer_codec_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = self._open(monkeypatch, "gbk")
        assert r.status == "ok", r.render_text()
        assert r.fields.get("encoding") == "gb18030"
        assert "warning" not in r.fields
        assert GBK_TEXT in (r.body or ""), r.render_text()
        assert "\ufffd" not in (r.body or "")
        sid = r.fields["id"]
        assert get_screen_registry().get(sid).text_codec == "gb18030"  # type: ignore[union-attr]
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_open_unknown_codec_warns_and_keeps_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            r = self._open(monkeypatch, "gbk2")
        assert r.status == "ok", r.render_text()
        # Unknown name: utf-8 stays in force (so no "encoding" token), the
        # fallback is reported, and the frame is the utf-8/replace reading.
        assert "encoding" not in r.fields
        warn = str(r.fields.get("warning") or "")
        assert "gbk2" in warn and "utf-8" in warn, r.render_text()
        assert GBK_AS_UTF8 in (r.body or ""), r.render_text()
        assert any("unknown text codec" in rec.message for rec in caplog.records)
        sid = r.fields["id"]
        assert get_screen_registry().get(sid).text_codec == "utf-8"  # type: ignore[union-attr]
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_open_unusable_codec_warns_and_keeps_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A known name that cannot decode a stream is reported, not pinned.

        ``utf-16`` is a real codec, so the resolution accepts it - but its
        decoder raises on the first BOM-less chunk, so the boundary has to fall
        back and the open row has to say so (pre-fix: no ``warning`` token at
        all, and the session died on its first feed).
        """
        r = self._open(monkeypatch, "utf-16")
        assert r.status == "ok", r.render_text()
        assert "encoding" not in r.fields
        warn = str(r.fields.get("warning") or "")
        assert "utf-16" in warn and "utf-8" in warn, r.render_text()
        sid = r.fields["id"]
        assert get_screen_registry().get(sid).text_codec == "utf-8"  # type: ignore[union-attr]
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_open_default_reports_nothing_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing configured, nothing dropped: the open result is historic."""
        r = self._open(monkeypatch, "utf-8", child_cmd="printf 'hello\\n'")
        assert r.status == "ok", r.render_text()
        assert "encoding" not in r.fields
        assert "warning" not in r.fields
        assert "repl" not in r.fields
        sid = r.fields["id"]
        assert get_screen_registry().get(sid).text_codec == "utf-8"  # type: ignore[union-attr]
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_open_reports_a_decode_that_dropped_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GBK bytes on the default codec: the frame says the decode replaced.

        The token is what distinguishes a frame that really is utf-8 from one
        the utf-8/replace fallback read; without it the U+FFFD characters are
        the only signal, and they can scroll off.
        """
        r = self._open(monkeypatch, "utf-8")
        assert r.status == "ok", r.render_text()
        assert "encoding" not in r.fields
        assert r.fields.get("repl") == 1, r.render_text()
        assert GBK_AS_UTF8 in (r.body or ""), r.render_text()
        sid = r.fields["id"]
        assert get_screen_registry().get(sid).replaced_chars > 0  # type: ignore[union-attr]
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_send_frame_reports_the_codec_and_replacements(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every frame row carries the codec evidence, not just ``open``.

        An agent holding only the session id gets a frame from ``send``; with
        no token there it cannot tell a legacy-console reading from utf-8.
        """
        r = self._open(monkeypatch, "gbk")
        assert r.status == "ok", r.render_text()
        sid = r.fields["id"]
        try:
            sent = screen_ops.send_screen(
                id=sid, actions=[{"type": "text", "text": "x"}], shot=True
            )
            assert sent.status in ("ok", "unchanged"), sent.render_text()
            assert sent.fields.get("encoding") == "gb18030", sent.render_text()
            # gb18030 maps every byte here, so nothing was replaced.
            assert "repl" not in sent.fields, sent.render_text()
        finally:
            assert screen_ops.close_screen(id=sid).status == "ok"

        # And the replacement flag reaches the send row of an unconfigured
        # session whose decoder did drop bytes.
        garbled = self._open(monkeypatch, "utf-8")
        assert garbled.status == "ok", garbled.render_text()
        gsid = garbled.fields["id"]
        try:
            sent2 = screen_ops.send_screen(
                id=gsid, actions=[{"type": "text", "text": "x"}], shot=True
            )
            assert sent2.status in ("ok", "unchanged"), sent2.render_text()
            assert "encoding" not in sent2.fields
            assert sent2.fields.get("repl") == 1, sent2.render_text()
        finally:
            assert screen_ops.close_screen(id=gsid).status == "ok"

    def test_open_pins_codec_against_later_transport_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-probed transport value must not switch a live session."""
        real_ensure = screen_ops.ensure_endpoint
        endpoint_box: list[Any] = []

        def _ensure(*args: Any, **kwargs: Any) -> Any:
            endpoint = real_ensure(*args, **kwargs)
            endpoint.transport.text_encoding = "gbk"
            endpoint_box.append(endpoint)
            return endpoint

        handle = FakePty()
        monkeypatch.setattr(screen_ops, "ensure_endpoint", _ensure)
        monkeypatch.setattr(screen_ops, "_open_pty", lambda *a, **k: handle)
        r = screen_ops.open_screen(
            ep="local",
            argv=["/bin/sh", "-c", "true"],
            cols=80,
            rows=24,
            fit=False,
            settle_s=0.0,
            home=self.home,
        )
        assert r.status == "ok", r.render_text()
        sid = r.fields["id"]
        sess = get_screen_registry().get(sid)
        assert sess is not None
        assert sess.text_codec == "gb18030"

        # Transport re-probe lands after the session is live.
        endpoint_box[0].transport.text_encoding = "utf-8"
        handle.feed_chunk(GBK_BYTES + b"\r\n")
        sess.drain(0.0)
        assert _frame(sess).strip() == GBK_TEXT
        assert screen_ops.close_screen(id=sid).status == "ok"

    def test_send_writes_the_session_code_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gbk session puts its code page on the wire, text and paste alike.

        The session resolved the transport's codec to decode its frames; the
        same resolved codec has to encode literal text and paste content, or
        the peer's console receives bytes it cannot read (\u4e2d\u6587 arrives as
        e4b8ade69687 where its code page needs d6d0cec4) while the send reports
        ok. The paste delimiters stay ASCII control bytes either way.
        """
        real_ensure = screen_ops.ensure_endpoint

        def _ensure(*args: Any, **kwargs: Any) -> Any:
            endpoint = real_ensure(*args, **kwargs)
            endpoint.transport.text_encoding = "gbk"
            return endpoint

        handle = FakePty()
        monkeypatch.setattr(screen_ops, "ensure_endpoint", _ensure)
        monkeypatch.setattr(screen_ops, "_open_pty", lambda *a, **k: handle)
        r = screen_ops.open_screen(
            ep="local",
            argv=["/bin/sh", "-c", "true"],
            cols=80,
            rows=24,
            fit=False,
            settle_s=0.0,
            home=self.home,
        )
        assert r.status == "ok", r.render_text()
        sid = r.fields["id"]
        sess = get_screen_registry().get(sid)
        assert sess is not None
        assert sess.text_codec == "gb18030"
        try:
            # Drop the open-path probe (ASCII shell command) so the assertion
            # below covers only what the send wrote.
            handle.written.clear()
            sess.feed(b"\x1b[?2004h")  # peer announced it parses the delimiters
            sent = screen_ops.send_screen(
                id=sid,
                actions=[
                    {"type": "text", "text": UTF8_TEXT},
                    {"type": "paste", "text": UTF8_TEXT},
                ],
                shot=True,
            )
            assert sent.status in ("ok", "unchanged"), sent.render_text()
            assert sent.fields.get("encoding") == "gb18030", sent.render_text()
            written = bytes(handle.written)
            assert written == UTF8_TEXT.encode("gb18030") + (
                PASTE_START + UTF8_TEXT.encode("gb18030") + PASTE_END
            ), f"peer bytes: {written.hex()}"
        finally:
            assert screen_ops.close_screen(id=sid).status == "ok"
