"""Console peer codec: ``console open encoding=`` end to end.

Unlike an endpoint, a serial device has no profile and nothing to probe, so
the operator supplies the peer console's codec explicitly at open. The value
must reach the capture ring (a GBK-emitting device then renders text instead
of U+FFFD), be kept on the session (later views keep reading with it), and be
reported on every row that carries decoded text. Unset must stay the historic
utf-8/replace read, and a name that is not a byte-stream decoder must warn and
leave the session alive on utf-8 rather than break the capture. The CLI output
boundary must likewise degrade a CJK body instead of raising when the stdout
cannot encode it.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.cli import main as cli_main
from mcp_remote_control.core import console_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.mcp_server import create_server
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.registry import (
    get_serial_registry,
    reset_serial_registry,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# \u8df3\u8fc7 as a cp936 console writes it.
GBK_TEXT = "\u8df3\u8fc7"
GBK_BYTES = GBK_TEXT.encode("gbk")  # b'\xcc\xf8\xb9\xfd'
GBK_AS_UTF8 = GBK_BYTES.decode("utf-8", errors="replace")


class _FakeSer:
    """Thread-safe minimal pyserial stand-in for the background pump."""

    def __init__(self) -> None:
        self.is_open = True
        self._lock = threading.Lock()
        self._rx = bytearray()
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def inject(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)

    def read(self, n: int) -> bytes:
        with self._lock:
            chunk = bytes(self._rx[:n])
            del self._rx[:n]
            return chunk

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
            self._rx.extend(b"echo\n" + data)
            return len(data)

    def close(self) -> None:
        self.is_open = False


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_serial_registry()
    yield
    reset_serial_registry()


def _patch_console_open(monkeypatch: pytest.MonkeyPatch, fake: _FakeSer) -> None:
    """Redirect ``console_ops.SerialConsole`` to wrap *fake*."""

    def _open(port: str, baudrate: int = 115200, **_kw: object) -> SerialConsole:
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)


def _open(fake: _FakeSer, **kwargs: Any) -> OpResult:
    return console_ops.run("open", path="COM9", baud=115200, max_lines=100, **kwargs)


def _views(sid: str, **kwargs: Any) -> OpResult:
    return console_ops.run("views", id=sid, mode="tail", n=10, settle_ms=50, **kwargs)


def _wait_body(
    fetch: Callable[[], OpResult], needle: str, timeout: float = 2.0
) -> str:
    """Poll *fetch* until its body carries *needle* (or timeout)."""
    deadline = time.time() + timeout
    body = ""
    while time.time() < deadline:
        body = fetch().body or ""
        if needle in body:
            return body
        time.sleep(0.02)
    return body


# ---------------------------------------------------------------------------
# open: the codec reaches the ring and the session
# ---------------------------------------------------------------------------


def test_open_reads_with_peer_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """encoding=gbk: the ring decodes GBK bytes and both rows say so."""
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    r = _open(fake, encoding="gbk")
    assert r.is_ok(), r.fields
    assert r.fields.get("encoding") == "gb18030"
    assert "warning" not in r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None
    assert sess.text_encoding == "gb18030"
    assert sess.buffer.text_encoding == "gb18030"

    v = _views(sid)
    assert v.is_ok(), v.fields
    assert v.fields.get("encoding") == "gb18030"
    assert v.body == GBK_TEXT, repr(v.body)
    assert "\ufffd" not in (v.body or "")
    assert console_ops.run("close", id=sid).is_ok()


def test_session_keeps_codec_across_views_and_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned codec survives later RX and a send, not just the first view."""
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake, encoding="gbk").fields["id"])
    assert _wait_body(lambda: _views(sid), GBK_TEXT) == GBK_TEXT

    # New GBK RX after the first view, then a write that echoes ASCII.
    fake.inject(GBK_BYTES + b"\n")
    s = console_ops.run("send", id=sid, data="help", newline=True)
    assert s.is_ok(), s.fields
    body = _wait_body(lambda: _views(sid), "help")
    assert body.splitlines()[0] == GBK_TEXT, repr(body)
    v = _views(sid)
    assert v.fields.get("encoding") == "gb18030"
    assert "\ufffd" not in (v.body or "")
    assert console_ops.run("close", id=sid).is_ok()


def test_open_default_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No encoding supplied: the historic utf-8/replace read, no extra tokens."""
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    r = _open(fake)
    assert r.is_ok(), r.fields
    assert "encoding" not in r.fields
    assert "warning" not in r.fields
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None and sess.buffer.text_encoding == "utf-8"

    body = _wait_body(lambda: _views(sid), GBK_AS_UTF8)
    assert body == GBK_AS_UTF8, repr(body)
    v = _views(sid)
    assert "encoding" not in v.fields
    assert console_ops.run("close", id=sid).is_ok()


# ---------------------------------------------------------------------------
# send: literal text goes out in the codec the console reads with
# ---------------------------------------------------------------------------


def test_send_data_uses_the_console_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gb18030 console must be sent \u4e2d\u6587 as d6d0cec4, not utf-8.

    The ring decodes with the codec the console was opened with, so the same
    codec has to encode an operator's literal text: utf-8 bytes on a cp936
    console arrive as mojibake for the peer, not as the text that was asked
    for. The byte count reports what went on the link, not the utf-8 size.
    """
    fake = _FakeSer()
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake, encoding="gbk").fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None and sess.text_encoding == "gb18030"
    fake.written.clear()

    s = console_ops.run("send", id=sid, data="\u4e2d\u6587", newline=True)
    assert s.is_ok(), s.fields
    assert bytes(fake.written) == "\u4e2d\u6587".encode("gb18030") + b"\n", bytes(
        fake.written
    ).hex()
    assert s.fields.get("bytes") == 5
    assert console_ops.run("close", id=sid).is_ok()


def test_send_data_keeps_utf8_on_an_unconfigured_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No encoding supplied: literal text keeps the historic utf-8 bytes."""
    fake = _FakeSer()
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake).fields["id"])
    fake.written.clear()

    s = console_ops.run("send", id=sid, data="\u4e2d\u6587")
    assert s.is_ok(), s.fields
    assert bytes(fake.written) == "\u4e2d\u6587".encode("utf-8")
    assert console_ops.run("close", id=sid).is_ok()


def test_send_b64_bytes_stay_verbatim_on_a_legacy_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``data_b64`` is already wire bytes: the codec must not touch them."""
    fake = _FakeSer()
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake, encoding="gbk").fields["id"])
    fake.written.clear()

    payload = "\u4e2d\u6587".encode("gb18030")
    s = console_ops.run("send", id=sid, data_b64=base64.b64encode(payload).decode())
    assert s.is_ok(), s.fields
    assert bytes(fake.written) == payload
    assert console_ops.run("close", id=sid).is_ok()


def test_send_data_replacement_policy_holds_on_a_legacy_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character the console codec cannot map is replaced, never raised."""
    fake = _FakeSer()
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake, encoding="latin-1").fields["id"])
    fake.written.clear()

    s = console_ops.run("send", id=sid, data="\u4e2d\u6587")
    assert s.is_ok(), s.fields
    assert bytes(fake.written) == b"??"
    assert console_ops.run("close", id=sid).is_ok()


# ---------------------------------------------------------------------------
# open: unusable names degrade, never break the session
# ---------------------------------------------------------------------------


def test_open_unknown_codec_warns_and_keeps_utf8(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        r = _open(fake, encoding="gbk2")
    assert r.is_ok(), r.fields
    # Nothing configured was pinned: the read is utf-8/replace, and the row
    # says the requested name was dropped instead of looking unconfigured.
    assert "encoding" not in r.fields
    warning = str(r.fields.get("warning") or "")
    assert "gbk2" in warning and "utf-8" in warning, r.fields
    assert any("unknown text codec" in rec.message for rec in caplog.records)
    sid = str(r.fields["id"])
    sess = get_serial_registry().get(sid)
    assert sess is not None and sess.buffer.text_encoding == "utf-8"

    body = _wait_body(lambda: _views(sid), GBK_AS_UTF8)
    assert body == GBK_AS_UTF8, repr(body)
    assert "encoding" not in _views(sid).fields
    assert console_ops.run("close", id=sid).is_ok()


def test_open_unusable_known_codec_warns_and_keeps_utf8(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A real codec that cannot read a byte stream falls back, not raises.

    ``utf-16`` passes a plain codec lookup but its decoder raises on the first
    BOM-less chunk, so pinning it would kill the capture at its first byte.
    """
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        r = _open(fake, encoding="utf-16")
    assert r.is_ok(), r.fields
    assert "encoding" not in r.fields
    warning = str(r.fields.get("warning") or "")
    assert "utf-16" in warning and "utf-8" in warning, r.fields
    assert any("not a byte-stream decoder" in rec.message for rec in caplog.records)
    sid = str(r.fields["id"])
    assert _wait_body(lambda: _views(sid), GBK_AS_UTF8) == GBK_AS_UTF8
    assert console_ops.run("close", id=sid).is_ok()


# ---------------------------------------------------------------------------
# the codec is pinned per session
# ---------------------------------------------------------------------------


def test_second_open_keeps_the_live_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy device keeps the codec it was opened with.

    The registry allows one session per path, so a second ``console open`` for
    the same device is rejected (CONSOLE_IN_USE) and cannot re-codec the live
    capture: the ring's decoder is pinned for the session, and a mid-stream
    switch would re-read the character in flight. Changing the codec means
    close, then reopen with the new value - the re-open reads with what that
    open passed, not with what the previous session used.
    """
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)
    sid = str(_open(fake, encoding="gbk").fields["id"])

    again = _open(fake, encoding="utf-8")
    assert again.status == "error"
    assert again.code == "CONSOLE_IN_USE"
    assert again.fields.get("id") == sid
    v = _views(sid)
    assert v.fields.get("encoding") == "gb18030"
    assert _wait_body(lambda: _views(sid), GBK_TEXT) == GBK_TEXT
    assert console_ops.run("close", id=sid).is_ok()

    # Re-open takes the value passed to it: utf-8 here, so the historic read.
    plain = _FakeSer()
    plain.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, plain)
    sid2 = str(_open(plain).fields["id"])
    assert sid2 != sid
    assert _wait_body(lambda: _views(sid2), GBK_AS_UTF8) == GBK_AS_UTF8
    assert "encoding" not in _views(sid2).fields
    assert console_ops.run("close", id=sid2).is_ok()


# ---------------------------------------------------------------------------
# surfaces: MCP tool and CLI are thin shells over Core
# ---------------------------------------------------------------------------


def _mcp_tools() -> dict[str, object]:
    async def _list() -> dict[str, object]:
        mcp = create_server()
        return {t.name: t for t in await mcp.list_tools()}

    return asyncio.run(_list())


def _tool_text(out: object) -> str:
    blocks = getattr(out, "content", out)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)  # type: ignore[union-attr]


def test_mcp_console_tool_exposes_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _mcp_tools()["console"]
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    props = set((schema or {}).get("properties", {}))
    assert "encoding" in props, props
    desc = str(getattr(tool, "description", "") or "")
    assert "encoding=" in desc and "gb18030" in desc, desc

    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    async def _open_and_view() -> tuple[str, str]:
        mcp = create_server()
        opened = _tool_text(
            await mcp.call_tool(
                "console",
                {"op": "open", "path": "COM9", "max_lines": 100, "encoding": "gbk"},
            )
        )
        sid = ""
        for token in opened.split():
            if token.startswith("id="):
                sid = token.split("=", 1)[1]
        views = ""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            views = _tool_text(
                await mcp.call_tool(
                    "console",
                    {"op": "views", "id": sid, "n": 5, "settle_ms": 50},
                )
            )
            if GBK_TEXT in views:
                break
            await asyncio.sleep(0.02)
        await mcp.call_tool("console", {"op": "close", "id": sid})
        return opened, views

    opened, views = asyncio.run(_open_and_view())
    assert "encoding=gb18030" in opened, opened
    assert GBK_TEXT in views, views


def test_cli_console_open_encoding_threads_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    code = cli_main(["console", "open", "--path", "COM9", "--encoding", "gbk"])
    assert code == 0
    out = capsys.readouterr().out
    assert "encoding=gb18030" in out, out

    deadline = time.time() + 2.0
    seen = ""
    while time.time() < deadline:
        assert cli_main(["console", "views", "--id", "con_01", "--n", "5"]) == 0
        seen = capsys.readouterr().out
        if GBK_TEXT in seen:
            break
        time.sleep(0.02)
    assert GBK_TEXT in seen, seen
    assert cli_main(["console", "close", "--id", "con_01"]) == 0
    capsys.readouterr()


def test_cli_views_body_survives_unencodable_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ascii stdout degrades the body to escapes instead of raising.

    A peer that speaks a legacy codec answers in its code page, so the decoded
    views body is CJK by design; a stdout that cannot represent it
    (``PYTHONIOENCODING=ascii``, a C-locale pipe) must still deliver the
    result, with the codepoints visible as escapes, rather than exit through a
    ``UnicodeEncodeError`` traceback.
    """
    fake = _FakeSer()
    fake.inject(GBK_BYTES + b"\n")
    _patch_console_open(monkeypatch, fake)

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    assert cli_main(["console", "open", "--path", "COM9", "--encoding", "gbk"]) == 0

    deadline = time.time() + 2.0
    seen = ""
    while time.time() < deadline:
        assert cli_main(["console", "views", "--id", "con_01", "--n", "5"]) == 0
        sys.stdout.flush()
        seen = raw.getvalue().decode("ascii")
        if "\\u8df3\\u8fc7" in seen:
            break
        time.sleep(0.02)
    assert "\\u8df3\\u8fc7" in seen, seen
    assert cli_main(["console", "close", "--id", "con_01"]) == 0
