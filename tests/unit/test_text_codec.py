"""Unit tests for codec/text_codec.

The decode policy is tested where it is decided (here) and where callers see
it (the transport funnels, driven through real ``run_command`` calls).
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from mcp_remote_control.codec import (
    charmap_to_codec,
    codepage_to_codec,
    decode_auto,
    encode_for_remote,
)
from mcp_remote_control.transport import TransportError
from mcp_remote_control.transport import local as local_mod
from mcp_remote_control.transport.local import LocalTransport
from mcp_remote_control.transport.ssh import SSHTransport


def test_decode_gbk_chinese_with_preferred() -> None:
    raw = "\u4f60\u597d".encode("gbk")
    r = decode_auto(raw, preferred="gb18030")
    assert r.text == "\u4f60\u597d"
    assert r.encoding_used in ("gb18030", "gbk")
    assert r.replaced is False


def test_decode_utf8_chinese_not_garbled() -> None:
    raw = "\u4f60\u597d\u4e16\u754c".encode()
    r = decode_auto(raw, preferred="utf-8")
    assert r.text == "\u4f60\u597d\u4e16\u754c"
    assert r.encoding_used == "utf-8"
    assert r.replaced is False


def test_decode_auto_fallback_replace() -> None:
    # Invalid as utf-8 and as gb18030 in some cases - use lone invalid utf-8 byte
    raw = b"\xff\xfe not valid"
    r = decode_auto(raw, preferred="utf-8")
    assert isinstance(r.text, str)
    # May succeed as gb18030 or replace - either is fine if no raise
    assert r.encoding_used in ("utf-8", "gb18030")


def test_codepage_936_and_65001() -> None:
    assert codepage_to_codec(936) == "gb18030"
    assert codepage_to_codec(65001) == "utf-8"
    assert codepage_to_codec("Active code page: 936") == "gb18030"


def test_charmap_aliases() -> None:
    assert charmap_to_codec("UTF-8") == "utf-8"
    assert charmap_to_codec("GBK") == "gb18030"


def test_encode_for_remote() -> None:
    assert encode_for_remote("hi", "utf-8") == b"hi"
    assert isinstance(encode_for_remote("\u4e2d", "gb18030"), bytes)


def test_decode_auto_strips_utf8_bom() -> None:
    """Leading UTF-8 BOM must not appear as U+FEFF in decoded text."""
    raw = b"\xef\xbb\xbfhello"
    r = decode_auto(raw, preferred="utf-8")
    assert r.text == "hello"
    assert "\ufeff" not in r.text
    assert r.replaced is False


def test_decode_auto_strips_bom_without_preferred() -> None:
    raw = b"\xef\xbb\xbf\xe4\xbd\xa0\xe5\xa5\xbd"  # BOM + \u4f60\u597d
    r = decode_auto(raw)
    assert r.text == "\u4f60\u597d"
    assert not r.text.startswith("\ufeff")


def test_decode_auto_no_bom_unchanged() -> None:
    """Plain UTF-8 without BOM is unchanged."""
    raw = "plain".encode()
    r = decode_auto(raw, preferred="utf-8")
    assert r.text == "plain"


def test_decode_auto_str_strips_leading_feff() -> None:
    r = decode_auto("\ufeffalready-unicode")
    assert r.text == "already-unicode"
    assert r.encoding_used == "unicode"


# ---------------------------------------------------------------------------
# Decode order: a self-validating UTF-8 read wins over a permissive preferred
# ---------------------------------------------------------------------------

REPLACEMENT = chr(0xFFFD)


def test_valid_utf8_wins_over_permissive_preferred() -> None:
    """gb18030/cp936/latin-1 strict-decode almost anything, so they cannot run
    first without silently re-reading valid UTF-8 as something else."""
    raw = "\u64cd\u4f5c\u6210\u529f".encode()
    for pref in ("gb18030", "cp936", "gbk", "latin-1", "cp1252", "big5"):
        r = decode_auto(raw, preferred=pref)
        assert r.text == "\u64cd\u4f5c\u6210\u529f", pref
        assert r.encoding_used == "utf-8", pref
        assert r.replaced is False
        assert r.errors == 0
        assert r.fallback is False


def test_ascii_body_is_identical_under_any_preferred() -> None:
    raw = b"plain ascii\n"
    assert decode_auto(raw, preferred="gb18030").text == "plain ascii\n"
    assert decode_auto(raw, preferred=None).text == "plain ascii\n"


# ---------------------------------------------------------------------------
# Decode granularity: one damaged byte must not re-read the whole body
# ---------------------------------------------------------------------------


def _has_utf8_evidence(raw: bytes) -> bool:
    """Same test the codec applies: the readable part is more than ASCII."""
    readable = raw.decode("utf-8", errors="ignore")
    return any(ch > "\x7f" for ch in readable)


def test_truncated_utf8_body_keeps_its_utf8_reading() -> None:
    """A read-boundary cut is repaired in place, never re-read body-wide."""
    line = "| \u4e3b\u673a | `endpoint` \u00b7 `exec` \u00b7 `fs` | \u8fdc\u7a0b\u63a7\u5236\u6a21\u5757 |\n".encode()
    for cut in range(1, len(line)):
        chunk = line[:-cut]
        if not _has_utf8_evidence(chunk):
            continue  # covered by the no-evidence case below
        damaged = False
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            damaged = True
        for pref in (None, "gb18030"):
            r = decode_auto(chunk, preferred=pref)
            assert r.encoding_used == "utf-8", (cut, pref)
            assert r.replaced is damaged, (cut, pref)
            if damaged:
                assert r.fallback is True
                assert REPLACEMENT in r.text
                assert r.text == chunk.decode("utf-8", errors="replace")
            if cut <= 30:
                # the healthy majority of the body survives intact
                assert "\u4e3b\u673a" in r.text and "endpoint" in r.text


def test_ascii_fragment_with_a_cut_lead_byte_is_flagged() -> None:
    """Trade-off: a fragment whose surviving readable part is all-ASCII carries
    no UTF-8 evidence, so the legacy leg decides \u2014 and says so via fallback."""
    r = decode_auto(b"| \xe4\xb8")
    assert r.encoding_used == "gb18030"
    assert r.fallback is True


def test_stray_byte_mid_body_keeps_the_utf8_reading() -> None:
    """A stray byte the legacy codec also rejects ends on the utf-8 side: the
    last-resort replacement reading keeps the healthy majority of the body."""
    raw = "| \u4e3b\u673a".encode() + b"\xff" + " | fs |\n".encode()
    for pref in (None, "gb18030"):
        r = decode_auto(raw, preferred=pref)
        assert r.encoding_used == "utf-8", pref
        assert r.text == "| \u4e3b\u673a" + REPLACEMENT + " | fs |\n"
        assert r.replaced is True
        assert r.errors == 1
        assert r.fallback is True


def test_mixed_encoding_body_is_left_to_the_legacy_legs() -> None:
    """Trade-off: damage the read boundary cannot explain (here a legacy word
    inside a UTF-8 line, which gb18030 accepts whole) is not repaired by the
    codec \u2014 it is announced through ``fallback`` instead of passing silently."""
    mixed = "| \u4e3b\u673a".encode() + "\u4e2d".encode("gbk") + " | fs |\n".encode()
    for pref in (None, "gb18030"):
        r = decode_auto(mixed, preferred=pref)
        assert r.encoding_used == "gb18030", pref
        assert r.fallback is True


def test_legacy_bodies_keep_their_configured_codec() -> None:
    """The repair rule must not hijack bodies that really are legacy text:
    the readable part of those is not multi-byte UTF-8."""
    cases = [
        ("caf\u00e9".encode("latin-1"), "latin-1", "caf\u00e9"),
        ("caf\u00e9 au lait \u00fcnicode".encode("latin-1"), "latin-1", "caf\u00e9 au lait \u00fcnicode"),
        ("\u4f60\u597d".encode("gbk"), "gb18030", "\u4f60\u597d"),
        ("name: \u5f20\u4e09\n".encode("gbk"), "gb18030", "name: \u5f20\u4e09\n"),
        ("\u4e2d\u6587\u6e2c\u8a66".encode("big5"), "big5", "\u4e2d\u6587\u6e2c\u8a66"),
    ]
    for raw, pref, expected in cases:
        r = decode_auto(raw, preferred=pref)
        assert r.text == expected, (raw, pref)
        assert r.encoding_used in (pref, "gb18030"), (raw, pref)
        assert r.replaced is False
        assert r.fallback is True


def test_default_chain_decodes_gbk_without_a_preferred() -> None:
    """The historical CJK leg still carries a host that says nothing about
    its code page."""
    r = decode_auto("\u4f60\u597d".encode("gbk"))
    assert r.text == "\u4f60\u597d"
    assert r.encoding_used == "gb18030"
    assert r.replaced is False


def test_fallback_flag_marks_unverifiable_reads() -> None:
    assert decode_auto("\u4f60\u597d".encode()).fallback is False
    assert decode_auto("\u4f60\u597d".encode("gbk")).fallback is True
    assert decode_auto(b"\xff\xfe").fallback is True


# ---------------------------------------------------------------------------
# Decode visibility: the transport a caller actually uses must report the codec
# ---------------------------------------------------------------------------

_LOCAL_LOGGER = "mcp_remote_control.transport.local"
_SSH_LOGGER = "mcp_remote_control.transport.ssh"


def _child(source: str) -> str:
    return f'{sys.executable} -c "{source}"'


def _decode_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [rec for rec in caplog.records if "fell back to" in rec.getMessage()]


def test_local_run_reports_the_codec_that_produced_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = LocalTransport()
    t.connect()
    with caplog.at_level(logging.WARNING, logger=_LOCAL_LOGGER):
        r = t.run_command(_child("import sys; sys.stdout.buffer.write('\u4e2d\u6587'.encode('gbk'))"))
    t.close()
    assert r.exit_code == 0
    assert r.stdout == "\u4e2d\u6587"
    assert t.last_decode == {
        "encoding": "gb18030",
        "preferred": t.text_encoding,
        "replaced": False,
        "errors": 0,
        "fallback": True,
        "ambiguous": False,
    }
    assert len(_decode_warnings(caplog)) == 1


def test_local_repeat_fallback_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    t = LocalTransport()
    t.connect()
    cmd = _child("import sys; sys.stdout.buffer.write('\u4e2d\u6587'.encode('gbk'))")
    with caplog.at_level(logging.WARNING, logger=_LOCAL_LOGGER):
        t.run_command(cmd)
        t.run_command(cmd)
    t.close()
    assert len(_decode_warnings(caplog)) == 1


def test_local_clean_utf8_run_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    t = LocalTransport()
    t.connect()
    with caplog.at_level(logging.WARNING, logger=_LOCAL_LOGGER):
        r = t.run_command(_child("import sys; sys.stdout.buffer.write('\u4e2d\u6587'.encode())"))
    t.close()
    assert r.stdout == "\u4e2d\u6587"
    assert t.last_decode is not None
    assert t.last_decode["encoding"] == "utf-8"
    assert t.last_decode["replaced"] is False
    assert _decode_warnings(caplog) == []


def test_local_transport_uses_configured_codec() -> None:
    """A controller whose console code page is not gb18030 needs its own codec."""
    t = LocalTransport(text_encoding="big5")
    t.connect()
    r = t.run_command(
        _child("import sys; sys.stdout.buffer.write('\u4e2d\u6587\u6e2c\u8a66'.encode('big5'))")
    )
    t.close()
    assert r.stdout == "\u4e2d\u6587\u6e2c\u8a66"


def test_local_transport_falls_back_to_controller_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_mod, "_controller_encoding", lambda: "cp932")
    assert LocalTransport().text_encoding == "cp932"
    monkeypatch.setattr(local_mod, "_controller_encoding", lambda: None)
    assert LocalTransport().text_encoding is None


class _StubConn:
    """Minimal asyncssh-shaped connection returning raw bytes for any command."""

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self._stdout = stdout
        self._stderr = stderr

    def run(self, command: str, **kwargs: object) -> object:
        return SimpleNamespace(
            exit_code=0, stdout=self._stdout, stderr=self._stderr
        )


def _ssh_transport(stdout: bytes, **kwargs: object) -> SSHTransport:
    t = SSHTransport(
        host="h",
        username="u",
        connector=lambda **k: _StubConn(stdout),
        **kwargs,  # type: ignore[arg-type]
    )
    t.connect()
    return t


def test_ssh_keeps_utf8_output_when_preferred_is_gb18030(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A chcp-936 host whose tool emits UTF-8 must not come back mojibaked."""
    t = _ssh_transport("| \u4e3b\u673a |\n".encode(), text_encoding="gb18030")
    with caplog.at_level(logging.WARNING, logger=_SSH_LOGGER):
        r = t.run_command("dir", cwd="/tmp")
    t.close()
    assert r.stdout == "| \u4e3b\u673a |\n"
    assert t.last_decode is not None
    assert t.last_decode["encoding"] == "utf-8"
    assert _decode_warnings(caplog) == []


def test_ssh_reports_legacy_decode_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = _ssh_transport("\u4f60\u597d".encode("gbk"), text_encoding="gb18030")
    with caplog.at_level(logging.WARNING, logger=_SSH_LOGGER):
        first = t.run_command("dir", cwd="/tmp")
        t.run_command("dir", cwd="/tmp")
    t.close()
    assert first.stdout == "\u4f60\u597d"
    assert t.last_decode == {
        "encoding": "gb18030",
        "preferred": "gb18030",
        "replaced": False,
        "errors": 0,
        "fallback": True,
        "ambiguous": False,
    }
    assert len(_decode_warnings(caplog)) == 1


class _ExecResultConn:
    """Connector that answers through ``run_command`` with bytes in a
    pre-built ExecResult \u2014 the shape that bypasses the decode helpers."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout

    def run_command(self, command: str, **kwargs: object) -> object:
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(exit_code=0, stdout=self._stdout)  # type: ignore[arg-type]


def test_ssh_decodes_bytes_from_a_pre_built_exec_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = SSHTransport(
        host="h",
        username="u",
        text_encoding="gb18030",
        connector=lambda **k: _ExecResultConn("\u4f60\u597d".encode("gbk")),
    )
    t.connect()
    with caplog.at_level(logging.WARNING, logger=_SSH_LOGGER):
        r = t.run_command("dir", cwd="/tmp")
    t.close()
    assert r.stdout == "\u4f60\u597d"
    assert t.last_decode is not None
    assert t.last_decode["encoding"] == "gb18030"
    assert len(_decode_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# Ambiguous reads: UTF-8 wins, but only a read the configured codec disagrees
# with is marked \u2014 the bytes cannot corroborate either codec
# ---------------------------------------------------------------------------


def _ambiguous_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [rec for rec in caplog.records if "is ambiguous" in rec.getMessage()]


def test_gbk_bytes_that_are_valid_utf8_are_marked_ambiguous() -> None:
    """A chcp-936 host's ``echo \u4e00\u76f4`` returns GBK bytes that strict-decode as
    UTF-8 to different text. The UTF-8 reading still wins (AE1), but the result
    must say the choice was not corroborated instead of looking verified."""
    raw = "\u4e00\u76f4".encode("gbk")
    r = decode_auto(raw, preferred="gb18030")
    assert r.text == raw.decode("utf-8"), "the utf-8 leg still decides the text"
    assert r.text != "\u4e00\u76f4", "the reading really is wrong for a GBK host"
    assert r.encoding_used == "utf-8"
    assert r.fallback is False
    assert r.ambiguous is True


def test_only_a_disagreeing_second_reading_is_ambiguous() -> None:
    """Nothing is ambiguous when the two codecs agree (ASCII, or a body both
    read the same), or when there is no configured codec to disagree with."""
    assert decode_auto(b"plain ascii\n", preferred="gb18030").ambiguous is False
    assert decode_auto("\u4e2d\u6587".encode("gbk"), preferred="gb18030").ambiguous is False
    assert decode_auto("| \u4e3b\u673a |\n".encode(), preferred=None).ambiguous is False
    assert decode_auto("| \u4e3b\u673a |\n".encode(), preferred="utf-8").ambiguous is False
    # Same bytes, same text under the legacy leg (its own codec): fallback
    # decides that read, ambiguity does not.
    latin = decode_auto("caf\u00e9".encode("latin-1"), preferred="latin-1")
    assert latin.text == "caf\u00e9"
    assert latin.fallback is True
    assert latin.ambiguous is False


def test_ambiguous_read_cannot_reach_the_fallback_branch() -> None:
    """``ambiguous`` implies the utf-8 leg produced the text, so the two flags
    are never both set: a caller can gate on either one."""
    r = decode_auto("\u4e00\u76f4".encode("gbk"), preferred="gb18030")
    assert not (r.fallback and r.ambiguous)


def test_ssh_transport_reports_an_ambiguous_read_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = _ssh_transport("\u4e00\u76f4".encode("gbk"), text_encoding="gb18030")
    with caplog.at_level(logging.WARNING, logger=_SSH_LOGGER):
        r = t.run_command("dir", cwd="/tmp")
        t.run_command("dir", cwd="/tmp")
    t.close()
    assert r.stdout == "\u4e00\u76f4".encode("gbk").decode("utf-8")
    assert t.last_decode is not None
    assert t.last_decode["ambiguous"] is True
    assert t.last_decode["fallback"] is False
    # A fallback and an ambiguous read are separate signals: this one must not
    # masquerade as the legacy-codec warning.
    assert _decode_warnings(caplog) == []
    assert len(_ambiguous_warnings(caplog)) == 1


def test_ssh_clean_utf8_read_is_not_ambiguous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A chcp-936 host whose tool emits UTF-8 is only ambiguous when the two
    readings differ; an ASCII body (the bulk of CLI output) never is."""
    t = _ssh_transport(b"total 0\n", text_encoding="gb18030")
    with caplog.at_level(logging.WARNING, logger=_SSH_LOGGER):
        r = t.run_command("dir", cwd="/tmp")
    t.close()
    assert r.stdout == "total 0\n"
    assert t.last_decode is not None
    assert t.last_decode["ambiguous"] is False
    assert _ambiguous_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Decode record scope: last_decode describes one command, never the previous one
# ---------------------------------------------------------------------------


def test_local_last_decode_is_reset_per_command() -> None:
    """A command that decodes no bytes must not leave the previous command's
    codec in the record: it would attribute a codec to text nobody produced."""
    t = LocalTransport()
    t.connect()
    t.run_command(_child("import sys; sys.stdout.buffer.write('\u4e2d\u6587'.encode('gbk'))"))
    assert t.last_decode is not None
    assert t.last_decode["encoding"] == "gb18030"
    r = t.run_command(_child("pass"))
    t.close()
    assert r.stdout == ""
    assert t.last_decode is None


def test_local_last_decode_is_reset_when_the_command_never_ran() -> None:
    """A caller-side failure before the child starts is still this command's
    record: the previous command's codec must not survive it."""
    t = LocalTransport()
    t.connect()
    t.run_command(_child("import sys; sys.stdout.buffer.write('\u4e2d\u6587'.encode('gbk'))"))
    assert t.last_decode is not None
    with pytest.raises(TransportError):
        t.run_command("echo hi", cwd="/nonexistent-mrc-cwd")
    t.close()
    assert t.last_decode is None


def test_ssh_last_decode_is_reset_per_command() -> None:
    conn = _StubConn("\u4e2d\u6587".encode("gbk"))
    t = SSHTransport(
        host="h", username="u", text_encoding="gb18030", connector=lambda **k: conn
    )
    t.connect()
    first = t.run_command("dir", cwd="/tmp")
    assert first.stdout == "\u4e2d\u6587"
    assert t.last_decode == {
        "encoding": "gb18030",
        "preferred": "gb18030",
        "replaced": False,
        "errors": 0,
        "fallback": True,
        "ambiguous": False,
    }
    conn._stdout = b""
    second = t.run_command("dir", cwd="/tmp")
    t.close()
    assert second.stdout == ""
    assert t.last_decode is None
