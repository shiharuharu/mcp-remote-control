"""Service tests: fs local full + fs_ops + CLI."""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
from pathlib import Path

import pytest

# Re-export so `from test_fs import MockSftp` still resolves after the split.
from _sftp_fakes import MockSftp as MockSftp
# Real pypsrp-shaped WinRM sessions for the link-failure row tests.
from _winrm_fakes import HOME, TEMP, FakePypsrpSession

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import fs_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint import get_registry
from mcp_remote_control.fs.backends.local import LocalFs, _MAX_RECURSE_DEPTH as _LOCAL_MAX_RECURSE_DEPTH
from mcp_remote_control.fs.types import FsError, ProgressCallback
from mcp_remote_control.transport.base import TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# local: list / stat / read / write / put / get / mkdir / rm
# ---------------------------------------------------------------------------


def test_local_list_absolute_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    r = fs_ops.run("list", ep="local", path=str(tmp_path), home=FIXTURES)
    assert r.kind == "fs"
    assert r.status == "ok"
    assert r.fields.get("op") == "list"
    assert r.fields.get("ep") == "local"
    path = r.fields.get("path")
    assert path is not None
    assert Path(path).is_absolute()
    assert path == os.path.abspath(str(tmp_path))
    assert r.fields.get("n", 0) >= 2
    body = r.body or ""
    assert "a.txt" in body
    assert "sub" in body
    text = r.render_text()
    assert text.startswith("@fs list ok")
    assert f"path={path}" in text or f"path={tmp_path}" in text
    # via is optional meta - may be present
    if "via=" in text:
        assert "via=local" in text


def test_local_recursive_list_shallow_returns_full_tree(tmp_path: Path) -> None:
    """Shallow recursive list still returns the full tree (no false truncate)."""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("y", encoding="utf-8")
    deep = sub / "nested"
    deep.mkdir()
    (deep / "c.txt").write_text("z", encoding="utf-8")

    backend = LocalFs()
    result = backend.list(str(tmp_path), recursive=True)
    names = {e.name for e in result.entries}
    assert "a.txt" in names
    assert "sub" in names
    assert "sub/b.txt" in names or any(n.endswith("b.txt") for n in names)
    assert "sub/nested" in names or any(n.endswith("nested") for n in names)
    assert "sub/nested/c.txt" in names or any(n.endswith("c.txt") for n in names)
    # Non-recursive list is unchanged: only top-level (+ . / ..).
    flat = backend.list(str(tmp_path), recursive=False)
    flat_names = {e.name for e in flat.entries}
    assert "a.txt" in flat_names
    assert "sub" in flat_names
    assert "sub/b.txt" not in flat_names
    assert "." in flat_names and ".." in flat_names


def test_local_recursive_list_depth_exceeded_raises(tmp_path: Path) -> None:
    """Chain deeper than max_depth raises DEPTH_EXCEEDED (not silent partial ok)."""
    # Chain of max_depth+1 dirs under tmp so walk visits depth > max_depth.
    cur = tmp_path
    for i in range(_LOCAL_MAX_RECURSE_DEPTH + 1):
        cur = cur / f"d{i}"
        cur.mkdir()
    (cur / "leaf.txt").write_text("x", encoding="utf-8")

    backend = LocalFs()
    with pytest.raises(FsError) as ei:
        backend.list(str(tmp_path), recursive=True)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth" in err.msg
    assert str(_LOCAL_MAX_RECURSE_DEPTH) in err.msg
    assert err.details.get("max_depth") == _LOCAL_MAX_RECURSE_DEPTH
    assert err.details.get("path")


def test_local_recursive_list_depth_cap_fast_path(tmp_path: Path) -> None:
    """Small max_depth proves explicit incompleteness signal (fast path)."""
    # Chain: tmp/a/b/c/leaf - exceeds max_depth=2 when entering c (depth 3).
    for d in ("a", "a/b", "a/b/c"):
        (tmp_path / d).mkdir()
    (tmp_path / "a" / "b" / "c" / "leaf.txt").write_text("x", encoding="utf-8")

    backend = LocalFs()
    entries: list = []
    with pytest.raises(FsError) as ei:
        backend._list_recursive(str(tmp_path), entries, max_depth=2)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth (2)" in err.msg
    assert err.details.get("max_depth") == 2
    assert err.details.get("path")
    # Partial progress before the raise is fine; signal is the error.


def _deny_scandir(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    """Make ``os.scandir`` raise EACCES for *denied*, leaving every other path open.

    The scan failure is injected rather than produced with a ``chmod 000``
    directory so the test holds for a privileged (root) test process, which
    bypasses directory permissions and would scan the tree anyway.
    """
    real_scandir = os.scandir

    def fake_scandir(path: str = ".") -> object:
        if os.fspath(path) == str(denied):
            raise PermissionError(errno.EACCES, "Permission denied", str(denied))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)


def test_local_recursive_list_root_scan_denied_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable root is PERMISSION_DENIED, not an empty ok tree."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x", encoding="utf-8")
    _deny_scandir(monkeypatch, tmp_path)

    backend = LocalFs()
    with pytest.raises(FsError) as ei:
        backend.list(str(tmp_path), recursive=True)
    err = ei.value
    assert err.code == "PERMISSION_DENIED"
    assert err.details.get("path") == str(tmp_path)

    # The same failure surfaces through the fs ops layer.
    r = fs_ops.run(
        "list",
        ep="local",
        path=str(tmp_path),
        recursive=True,
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "PERMISSION_DENIED"


def test_local_recursive_list_subdir_scan_denied_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable subdirectory aborts the walk naming that node."""
    (tmp_path / "top.txt").write_text("y", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("z", encoding="utf-8")
    _deny_scandir(monkeypatch, locked)

    backend = LocalFs()
    with pytest.raises(FsError) as ei:
        backend.list(str(tmp_path), recursive=True)
    err = ei.value
    assert err.code == "PERMISSION_DENIED"
    assert err.details.get("path") == str(locked)
    assert str(locked) in err.msg
    # The scan failure stays reachable as the mapped error's cause.
    assert isinstance(err.__cause__, OSError)


def test_local_stat_file(tmp_path: Path) -> None:
    f = tmp_path / "statme.txt"
    f.write_text("hello", encoding="utf-8")
    r = fs_ops.run("stat", ep="local", path=str(f), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("op") == "stat"
    assert Path(r.fields["path"]).is_absolute()
    assert r.fields.get("type") == "file"
    assert r.fields.get("bytes") == 5
    assert r.fields.get("mode")


def test_local_stat_missing() -> None:
    r = fs_ops.run(
        "stat",
        ep="local",
        path="/no/such/path/mrc-fs-missing-xyz",
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"
    assert r.fields.get("op") == "stat"
    assert "not found" in (r.fields.get("msg") or "").lower()
    text = r.render_text()
    assert text.startswith("@fs stat error")
    assert "NOT_FOUND" in text


def test_local_read_write(tmp_path: Path) -> None:
    target = tmp_path / "rw.txt"
    r_w = fs_ops.run(
        "write",
        ep="local",
        path=str(target),
        content="line1\nline2\n",
        home=FIXTURES,
    )
    assert r_w.status == "ok"
    assert Path(r_w.fields["path"]).is_absolute()
    assert r_w.fields.get("bytes") == len("line1\nline2\n")

    r_r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r_r.status == "ok"
    assert r_r.fields.get("type") == "text"
    assert r_r.body is not None
    assert "line1" in r_r.body
    assert Path(r_r.fields["path"]).is_absolute()


def test_local_read_utf16le_with_bom(tmp_path: Path) -> None:
    """A UTF-16LE file with BOM reads as text (utf-16) via the local
    backend. Previously the simple any-NUL heuristic misclassified it as
    binary; the shared detect_text now detects UTF-16 consistently."""
    target = tmp_path / "u16.txt"
    text = "hello-local"
    raw = b"\xff\xfe" + text.encode("utf-16-le")
    target.write_bytes(raw)

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16"
    assert text in (r.body or "")
    assert r.fields.get("bytes") == len(raw)


def test_local_read_utf16le_no_bom(tmp_path: Path) -> None:
    """UTF-16LE without a BOM (alternating-NUL ASCII) is detected as text
    via the shared heuristic; previously binary for the local backend."""
    target = tmp_path / "u16nobom.txt"
    text = "plain-ascii-content"
    raw = text.encode("utf-16-le")
    # Sanity: ASCII UTF-16LE has NUL at every odd index.
    assert b"\x00" in raw and raw[1] == 0
    target.write_bytes(raw)

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16-le"
    assert text in (r.body or "")


def test_local_read_binary_with_nul_stays_binary(tmp_path: Path) -> None:
    """Random binary with a NUL byte (no alternating-NUL pattern)
    is NOT misclassified as UTF-16 - true binary stays binary."""
    target = tmp_path / "bin.dat"
    target.write_bytes(b"\x00\x01\x02\x03\x04\x05binary")

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_local_read_big_endian_uint32_stays_binary(tmp_path: Path) -> None:
    """A NUL-dense binary with NON-PRINTABLE non-NUL bytes
    is NOT misclassified as UTF-16. A 12-byte big-endian uint32 file
    (``\\x00\\x00\\x00\\x01...``) has every even byte NUL - the old
    alternating-NUL heuristic reported ``utf-16-be`` and ``fs read`` returned
    gibberish text. The printable-ratio guard now rejects it as binary because
    the non-NUL bytes (0x01/0x02/0x03) are non-printable control chars."""
    target = tmp_path / "u32.bin"
    target.write_bytes(b"\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03")

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_local_read_utf16be_no_bom(tmp_path: Path) -> None:
    """Real UTF-16BE without a BOM (ASCII letters with NUL
    at every even index) is STILL detected as text after the printable-ratio
    guard - the BE direction's true positive is preserved."""
    target = tmp_path / "u16be.txt"
    text = "plain-ascii-content"
    raw = text.encode("utf-16-be")
    # Sanity: ASCII UTF-16BE has NUL at every even index.
    assert raw[0] == 0
    target.write_bytes(raw)

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16-be"
    assert text in (r.body or "")


# Magic-only fixtures: classification is header match, not a decoded bitmap.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
_GIF = b"GIF89a" + b"\x00" * 24
_WEBP = b"RIFF" + (28).to_bytes(4, "little") + b"WEBP" + b"\x00" * 16


def _assert_image_render_has_no_body(r: OpResult, payload: bytes) -> None:
    """CLI/text tracks must show image meta only, never the file bytes."""
    text = r.render_text()
    dumped = r.render_json()
    b64 = base64.b64encode(payload).decode("ascii")
    assert b64 not in text
    assert b64 not in dumped
    assert "image_data" not in dumped


@pytest.mark.parametrize(
    ("name", "payload", "mime"),
    [
        ("pic.png", _PNG, "image/png"),
        ("pic.jpg", _JPEG, "image/jpeg"),
        ("pic.gif", _GIF, "image/gif"),
        ("pic.webp", _WEBP, "image/webp"),
    ],
)
def test_local_read_image_ok(
    tmp_path: Path, name: str, payload: bytes, mime: str
) -> None:
    """Recognized complete images succeed with raw bytes and MIME, no body."""
    target = tmp_path / name
    target.write_bytes(payload)
    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.code is None
    assert r.fields.get("type") == "image"
    assert r.fields.get("mime_type") == mime
    assert r.fields.get("bytes") == len(payload)
    assert r.image_data == payload
    assert r.body is None
    assert r.fields.get("truncated") is None
    _assert_image_render_has_no_body(r, payload)
    assert "image_data" not in repr(r)


def test_local_read_png_without_extension(tmp_path: Path) -> None:
    """A PNG header is enough; the path suffix is not required."""
    target = tmp_path / "noext"
    target.write_bytes(_PNG)
    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.fields.get("type") == "image"
    assert r.fields.get("mime_type") == "image/png"
    assert r.image_data == _PNG
    assert r.body is None


def test_local_read_png_named_file_is_still_jpeg(tmp_path: Path) -> None:
    """MIME comes from bytes; a .png suffix does not override JPEG magic."""
    target = tmp_path / "photo.png"
    target.write_bytes(_JPEG)
    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.fields.get("type") == "image"
    assert r.fields.get("mime_type") == "image/jpeg"
    assert r.image_data == _JPEG


def test_local_read_text_named_png_stays_text(tmp_path: Path) -> None:
    """A .png name is not enough; UTF-8 text still takes the text branch."""
    target = tmp_path / "x.png"
    target.write_text("hello world\n", encoding="utf-8")
    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.fields.get("type") == "text"
    assert r.fields.get("mime_type") is None
    assert r.image_data is None
    assert "hello world" in (r.body or "")


def test_local_read_truncated_image_is_error(tmp_path: Path) -> None:
    """Identified image that hits the budget is READ_LIMIT_EXCEEDED, no bytes."""
    target = tmp_path / "big.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    target.write_bytes(payload)
    r = fs_ops.run(
        "read",
        ep="local",
        path=str(target),
        max_bytes=20,
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "READ_LIMIT_EXCEEDED"
    assert r.image_data is None
    assert r.body is None
    assert r.fields.get("truncated") is True
    assert r.fields.get("mime_type") == "image/png"
    text = r.render_text()
    dumped = r.render_json()
    b64 = base64.b64encode(payload).decode("ascii")
    assert b64 not in text
    assert b64 not in dumped
    assert "increase max_bytes" in (r.hint or "")
    assert "READ_LIMIT_EXCEEDED" in text


def test_local_read_image_budget_too_small_stays_binary(tmp_path: Path) -> None:
    """A budget shorter than the PNG signature keeps the binary truncated path."""
    target = tmp_path / "tiny.png"
    target.write_bytes(_PNG)
    r = fs_ops.run(
        "read",
        ep="local",
        path=str(target),
        max_bytes=2,
        home=FIXTURES,
    )
    assert r.status == "ok", r.render_text()
    assert r.code != "READ_LIMIT_EXCEEDED"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("truncated") is True
    assert r.fields.get("mime_type") is None
    assert r.image_data is None
    assert r.fields.get("sha256")
    assert "omitted" in (r.fields.get("note") or "")
    assert r.body is None or r.body == ""


def test_local_read_truncated_text_still_ok(tmp_path: Path) -> None:
    """Non-image truncation stays a successful text read with truncated=True."""
    target = tmp_path / "big.txt"
    target.write_text("hello world " * 40, encoding="utf-8")
    r = fs_ops.run(
        "read",
        ep="local",
        path=str(target),
        max_bytes=10,
        home=FIXTURES,
    )
    assert r.status == "ok", r.render_text()
    assert r.fields.get("type") == "text"
    assert r.fields.get("truncated") is True
    assert r.image_data is None
    assert r.body is not None


def test_local_read_riff_wav_stays_binary(tmp_path: Path) -> None:
    """RIFF/WAVE is not WebP; other binary still omits the body and hashes."""
    target = tmp_path / "sound.wav"
    payload = b"RIFF" + (16).to_bytes(4, "little") + b"WAVE" + b"\x00" * 8
    target.write_bytes(payload)
    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.fields.get("type") == "binary"
    assert r.fields.get("mime_type") is None
    assert r.image_data is None
    assert r.body is None or r.body == ""
    digest = r.fields.get("sha256")
    assert isinstance(digest, str) and len(digest) == 12
    assert "omitted" in (r.fields.get("note") or "")


def test_local_put_get(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-bytes")
    remote = tmp_path / "remote" / "out.bin"
    r_put = fs_ops.run(
        "put",
        ep="local",
        path=str(remote),
        local=str(src),
        home=FIXTURES,
    )
    assert r_put.status == "ok"
    assert Path(r_put.fields["path"]).is_absolute()
    assert r_put.fields.get("bytes") == len(b"payload-bytes")
    assert remote.is_file()
    assert remote.read_bytes() == b"payload-bytes"

    dest = tmp_path / "downloaded.bin"
    r_get = fs_ops.run(
        "get",
        ep="local",
        path=str(remote),
        local=str(dest),
        home=FIXTURES,
    )
    assert r_get.status == "ok"
    assert Path(r_get.fields["path"]).is_absolute()
    assert dest.read_bytes() == b"payload-bytes"


def test_local_mkdir_rm(tmp_path: Path) -> None:
    d = tmp_path / "newdir" / "nested"
    r_m = fs_ops.run("mkdir", ep="local", path=str(d), home=FIXTURES)
    assert r_m.status == "ok"
    assert Path(r_m.fields["path"]).is_absolute()
    assert d.is_dir()

    nested_file = d / "f.txt"
    nested_file.write_text("x", encoding="utf-8")

    # Non-recursive rm on dir fails clearly
    r_bad = fs_ops.run("rm", ep="local", path=str(d), home=FIXTURES)
    assert r_bad.status == "error"
    assert r_bad.code in {"IS_A_DIR", "FS_ERROR"}

    r_rm = fs_ops.run(
        "rm",
        ep="local",
        path=str(d),
        recursive=True,
        home=FIXTURES,
    )
    assert r_rm.status == "ok"
    assert Path(r_rm.fields["path"]).is_absolute()
    assert not d.exists()

    # File rm
    f = tmp_path / "gone.txt"
    f.write_text("bye", encoding="utf-8")
    r_f = fs_ops.run("rm", ep="local", path=str(f), home=FIXTURES)
    assert r_f.status == "ok"
    assert not f.exists()


def test_local_read_missing() -> None:
    r = fs_ops.run(
        "read",
        ep="local",
        path="/tmp/mrc-definitely-missing-file-xyz",
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"


def test_local_lazy_connect() -> None:
    reg = get_registry()
    assert reg.get("local") is None
    r = fs_ops.run("list", ep="local", path="/tmp", home=FIXTURES)
    assert r.status == "ok"
    ep = reg.get("local")
    assert ep is not None
    assert ep.connected is True
    assert Path(r.fields["path"]).is_absolute()


def test_missing_ep() -> None:
    r = fs_ops.run("list", path="/tmp", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_missing_path() -> None:
    r = fs_ops.run("list", ep="local", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_read_missing_path_rejected_before_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fs read without path is MISSING_ARG without attempting a lazy connect."""
    calls: list[object] = []

    def _boom(*_args: object, **_kwargs: object) -> object:
        calls.append((_args, _kwargs))
        raise TransportError("CONNECT_FAILED", "sentinel connect")

    monkeypatch.setattr(fs_ops, "ensure_endpoint", _boom)
    r = fs_ops.run("read", ep="probe-target", home=FIXTURES)
    assert r.status == "error", r.render_text()
    assert r.code == "MISSING_ARG", r.render_text()
    assert r.code != "CONNECT_FAILED"
    msg = str((r.fields or {}).get("msg") or "")
    assert "path" in msg.lower(), msg
    assert calls == []


def test_read_with_path_still_calls_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal read path still lazy-connects; validation does not skip ensure."""
    calls: list[object] = []

    def _boom(*_args: object, **_kwargs: object) -> object:
        calls.append((_args, _kwargs))
        raise TransportError("CONNECT_FAILED", "sentinel connect")

    monkeypatch.setattr(fs_ops, "ensure_endpoint", _boom)
    r = fs_ops.run(
        "read",
        ep="probe-target",
        path="/tmp/exists-for-arg-check",
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "CONNECT_FAILED", r.render_text()
    assert len(calls) == 1


def test_invalid_op() -> None:
    r = fs_ops.run("explode", ep="local", path="/tmp", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "INVALID_OP"


# ---------------------------------------------------------------------------
# local: symlink semantics - stat/rm/read must not follow links
# ---------------------------------------------------------------------------


def test_local_stat_symlink_reports_link(tmp_path: Path) -> None:
    """stat on a symlink reports kind=link with the target path."""
    target = tmp_path / "target.txt"
    target.write_text("target-content", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    r = fs_ops.run("stat", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "link"
    assert r.fields.get("target") is not None
    # os.readlink returns the stored target string (absolute here).
    assert r.fields.get("target") == str(target)


def test_local_stat_symlink_relative_target(tmp_path: Path) -> None:
    """stat on a symlink with a relative target stores the raw target string."""
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("target.txt")  # relative target

    r = fs_ops.run("stat", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "link"
    assert r.fields.get("target") == "target.txt"


def test_local_rm_symlink_preserves_target(tmp_path: Path) -> None:
    """rm on a symlink removes the link only - the target MUST survive."""
    target = tmp_path / "target.txt"
    target.write_text("precious-content", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    r = fs_ops.run("rm", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert not link.is_symlink()
    # Target is untouched.
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "precious-content"


def test_local_rm_symlink_to_dir_preserves_target(tmp_path: Path) -> None:
    """rm on a symlink-to-directory removes the link, not the directory."""
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_text("inner", encoding="utf-8")
    link = tmp_path / "linkdir"
    link.symlink_to(real_dir)

    r = fs_ops.run("rm", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert not link.is_symlink()
    # Real directory and its contents survive.
    assert real_dir.is_dir()
    assert (real_dir / "inner.txt").read_text(encoding="utf-8") == "inner"


def test_local_rm_dangling_symlink(tmp_path: Path) -> None:
    """rm on a dangling symlink (missing target) removes the link."""
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nonexistent")

    r = fs_ops.run("rm", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert not link.is_symlink()


def test_local_read_symlink_follows_target(tmp_path: Path) -> None:
    """read on a symlink follows to the target content (expected behavior)."""
    target = tmp_path / "target.txt"
    target.write_text("follow-me", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    r = fs_ops.run("read", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok"
    assert r.body is not None
    assert "follow-me" in r.body


def test_local_stat_regular_file_unchanged(tmp_path: Path) -> None:
    """stat on a regular file still reports kind=file (no regression)."""
    f = tmp_path / "plain.txt"
    f.write_text("hello", encoding="utf-8")
    r = fs_ops.run("stat", ep="local", path=str(f), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "file"
    assert r.fields.get("bytes") == 5
    assert r.fields.get("target") is None


def test_local_stat_dir_unchanged(tmp_path: Path) -> None:
    """stat on a directory still reports kind=dir (no regression)."""
    d = tmp_path / "subdir"
    d.mkdir()
    r = fs_ops.run("stat", ep="local", path=str(d), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "dir"
    assert r.fields.get("target") is None


# ---------------------------------------------------------------------------
# local: atomic writes - failure must not truncate the original
# ---------------------------------------------------------------------------


def test_local_write_atomic_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write that fails mid-operation leaves original content intact."""
    target = tmp_path / "preserved.txt"
    target.write_text("original-content", encoding="utf-8")

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    r = fs_ops.run(
        "write",
        ep="local",
        path=str(target),
        content="new-content",
        home=FIXTURES,
    )
    assert r.status == "error"
    # Original content is intact - no truncation.
    assert target.read_text(encoding="utf-8") == "original-content"
    # No temp file left behind.
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_local_write_atomic_success(tmp_path: Path) -> None:
    """write to a new path creates the file with correct content."""
    target = tmp_path / "atomically-written.txt"
    r = fs_ops.run(
        "write",
        ep="local",
        path=str(target),
        content="atomic-payload",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert target.read_text(encoding="utf-8") == "atomic-payload"
    # No temp file left behind after successful write.
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_local_get_atomic_failure_no_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get that fails leaves no partial file at the destination."""
    src = tmp_path / "source.bin"
    src.write_bytes(b"source-data-payload")
    dst = tmp_path / "downloaded.bin"

    def fail_replace(src_p: str, dst_p: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(src),
        local=str(dst),
        home=FIXTURES,
    )
    assert r.status == "error"
    # No partial file at the destination.
    assert not dst.exists()
    # No temp file left behind.
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_local_put_atomic_failure_no_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """put that fails leaves no partial file at the remote destination."""
    src = tmp_path / "source.bin"
    src.write_bytes(b"source-data-payload")
    dst = tmp_path / "remote" / "out.bin"
    (tmp_path / "remote").mkdir()

    def fail_replace(src_p: str, dst_p: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(dst),
        local=str(src),
        home=FIXTURES,
    )
    assert r.status == "error"
    assert not dst.exists()
    temps = [f for f in os.listdir(tmp_path / "remote") if ".mrc-tmp-" in f]
    assert len(temps) == 0


# ---------------------------------------------------------------------------
# local: atomic write mode preservation - permissions must survive
# ---------------------------------------------------------------------------


def test_local_write_preserves_file_mode(tmp_path: Path) -> None:
    """write preserves the existing file's mode (no silent chmod to 0o644)."""
    target = tmp_path / "secret.txt"
    target.write_text("original", encoding="utf-8")
    os.chmod(target, 0o600)

    r = fs_ops.run(
        "write",
        ep="local",
        path=str(target),
        content="new-content",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert target.read_text(encoding="utf-8") == "new-content"
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_local_write_new_file_default_mode(tmp_path: Path) -> None:
    """write to a new file creates it with a reasonable default mode."""
    target = tmp_path / "fresh.txt"
    r = fs_ops.run(
        "write",
        ep="local",
        path=str(target),
        content="content",
        home=FIXTURES,
    )
    assert r.status == "ok"
    mode = os.stat(target).st_mode & 0o777
    # Default open("wb") mode under typical umask is 0o644.
    assert mode == 0o644, f"expected 0o644, got {oct(mode)}"


def test_local_put_preserves_dest_mode_on_overwrite(tmp_path: Path) -> None:
    """put overwrite keeps destination mode (no silent widen 0o600->source)."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new-payload")
    os.chmod(src, 0o644)
    dest = tmp_path / "secret.bin"
    dest.write_bytes(b"old-secret")
    os.chmod(dest, 0o600)

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(dest),
        local=str(src),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert dest.read_bytes() == b"new-payload"
    mode = os.stat(dest).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_local_put_preserves_dest_mode_with_progress(tmp_path: Path) -> None:
    """put overwrite with progress callback also preserves dest mode."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new-payload-progress")
    os.chmod(src, 0o644)
    dest = tmp_path / "secret.bin"
    dest.write_bytes(b"old-secret")
    os.chmod(dest, 0o600)

    events: list[tuple[int, int | None]] = []
    r = fs_ops.run(
        "put",
        ep="local",
        path=str(dest),
        local=str(src),
        home=FIXTURES,
        progress=lambda d, t: events.append((d, t)),
    )
    assert r.status == "ok"
    assert events, "progress callback must be invoked"
    assert dest.read_bytes() == b"new-payload-progress"
    mode = os.stat(dest).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_local_get_preserves_dest_mode_on_overwrite(tmp_path: Path) -> None:
    """get overwrite keeps local destination mode (no silent widen)."""
    remote = tmp_path / "remote.bin"
    remote.write_bytes(b"remote-payload")
    os.chmod(remote, 0o644)
    dest = tmp_path / "local-secret.bin"
    dest.write_bytes(b"old-local")
    os.chmod(dest, 0o600)

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(remote),
        local=str(dest),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert dest.read_bytes() == b"remote-payload"
    mode = os.stat(dest).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_local_get_preserves_dest_mode_with_progress(tmp_path: Path) -> None:
    """get overwrite with progress callback also preserves dest mode."""
    remote = tmp_path / "remote.bin"
    remote.write_bytes(b"remote-payload-progress")
    os.chmod(remote, 0o644)
    dest = tmp_path / "local-secret.bin"
    dest.write_bytes(b"old-local")
    os.chmod(dest, 0o600)

    events: list[tuple[int, int | None]] = []
    r = fs_ops.run(
        "get",
        ep="local",
        path=str(remote),
        local=str(dest),
        home=FIXTURES,
        progress=lambda d, t: events.append((d, t)),
    )
    assert r.status == "ok"
    assert events, "progress callback must be invoked"
    assert dest.read_bytes() == b"remote-payload-progress"
    mode = os.stat(dest).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# local: read-only source - copyability must not depend on a progress callback
# ---------------------------------------------------------------------------

READONLY_PAYLOAD = b"read-only-source-payload"


def _st_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def _temp_files(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if ".mrc-tmp-" in name]


def _readonly_source(directory: Path) -> Path:
    """A mode 0444 regular file, e.g. a checked-out artifact or a CD-mounted one."""
    src = directory / "readonly-source.bin"
    src.write_bytes(READONLY_PAYLOAD)
    os.chmod(src, 0o444)
    return src


def _transfer(
    op: str,
    src: Path,
    dest: Path,
    *,
    progress: ProgressCallback | None = None,
) -> OpResult:
    """Run the same-host copy in the direction *op* names."""
    if op == "put":
        return fs_ops.run(
            "put",
            ep="local",
            path=str(dest),
            local=str(src),
            home=FIXTURES,
            progress=progress,
        )
    return fs_ops.run(
        "get",
        ep="local",
        path=str(src),
        local=str(dest),
        home=FIXTURES,
        progress=progress,
    )


@pytest.mark.parametrize("op", ["put", "get"])
@pytest.mark.parametrize("with_progress", [False, True], ids=["no_progress", "progress"])
def test_local_transfer_readonly_source_new_destination(
    tmp_path: Path,
    op: str,
    with_progress: bool,
) -> None:
    """A 0444 source transfers to a new destination with and without progress.

    The no-callback branch stages the copy in a temp file and reopens it to
    fsync; applying the source mode before that reopen leaves the temp
    unwritable, so the transfer is refused with PERMISSION_DENIED while the
    same transfer carrying a progress callback succeeds.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    src = _readonly_source(src_dir)
    dest = dest_dir / "out.bin"

    events: list[tuple[int, int | None]] = []
    progress = (lambda d, t: events.append((d, t))) if with_progress else None
    r = _transfer(op, src, dest, progress=progress)

    assert r.status == "ok", f"{op}: {r.code} {r.body}"
    assert dest.read_bytes() == READONLY_PAYLOAD
    # A new destination keeps the source mode.
    assert _st_mode(dest) == 0o444, f"expected 0o444, got {oct(_st_mode(dest))}"
    assert _st_mode(src) == 0o444, "the source was modified"
    if with_progress:
        assert events, "progress callback must be invoked"
    assert _temp_files(dest_dir) == []


@pytest.mark.parametrize("op", ["put", "get"])
@pytest.mark.parametrize("with_progress", [False, True], ids=["no_progress", "progress"])
def test_local_transfer_readonly_source_overwrite(
    tmp_path: Path,
    op: str,
    with_progress: bool,
) -> None:
    """A 0444 source overwrites an existing destination with and without progress."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    src = _readonly_source(src_dir)
    dest = dest_dir / "out.bin"
    dest.write_bytes(b"old-content")
    os.chmod(dest, 0o600)

    events: list[tuple[int, int | None]] = []
    progress = (lambda d, t: events.append((d, t))) if with_progress else None
    r = _transfer(op, src, dest, progress=progress)

    assert r.status == "ok", f"{op}: {r.code} {r.body}"
    assert dest.read_bytes() == READONLY_PAYLOAD
    # An overwrite keeps the existing destination mode, not the source's.
    assert _st_mode(dest) == 0o600, f"expected 0o600, got {oct(_st_mode(dest))}"
    assert _temp_files(dest_dir) == []


@pytest.mark.parametrize("with_progress", [False, True], ids=["no_progress", "progress"])
def test_local_transfer_readonly_source_failure_keeps_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_progress: bool,
) -> None:
    """A failing replace leaves the old destination intact and removes the temp."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    src = _readonly_source(src_dir)
    dest = dest_dir / "out.bin"
    dest.write_bytes(b"old-content")
    os.chmod(dest, 0o600)

    def fail_replace(src_p: str, dst_p: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    events: list[tuple[int, int | None]] = []
    progress = (lambda d, t: events.append((d, t))) if with_progress else None
    r = _transfer("put", src, dest, progress=progress)

    assert r.status == "error", f"{r.code} {r.body}"
    assert dest.read_bytes() == b"old-content"
    assert _st_mode(dest) == 0o600
    assert _temp_files(dest_dir) == []


immutable_flag_only = pytest.mark.skipif(
    getattr(os, "chflags", None) is None or not hasattr(stat, "UF_IMMUTABLE"),
    reason="BSD user-immutable flag not supported",
)


@immutable_flag_only
@pytest.mark.parametrize("op", ["put", "get"])
@pytest.mark.parametrize("with_progress", [False, True], ids=["no_progress", "progress"])
def test_local_transfer_immutable_source_leaves_no_temp(
    tmp_path: Path,
    op: str,
    with_progress: bool,
) -> None:
    """A flag-immutable source cannot be staged, and must not leak the temp.

    ``copystat`` copies ``st_flags`` onto the temp, so a temp staged from a
    user-immutable (``uchg``) source can be neither renamed into place nor
    unlinked; the failure path must clear the flag before cleaning up, so the
    transfer fails without leaving a temp in the destination directory.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    src = _readonly_source(src_dir)
    os.chflags(src, stat.UF_IMMUTABLE)
    if not os.stat(src).st_flags:
        pytest.skip("filesystem ignores the user immutable flag")
    dest = dest_dir / "out.bin"
    progress = (lambda d, t: None) if with_progress else None
    try:
        r = _transfer(op, src, dest, progress=progress)

        assert r.status == "error", f"{op}: {r.code} {r.body}"
        assert not dest.exists()
        assert _temp_files(dest_dir) == []
    finally:
        # tmp_path teardown must be able to remove the immutable source and
        # any temp left behind by a regression.
        for path in (src, *dest_dir.iterdir()):
            if os.stat(path).st_flags:
                os.chflags(path, 0)


# ---------------------------------------------------------------------------
# local: a failed transfer names the object on the side that failed
# ---------------------------------------------------------------------------


def test_local_put_unreadable_source_names_the_local_source(
    tmp_path: Path,
) -> None:
    """An unreadable source is reported against that source.

    The destination is writable and the transfer never created it, so a row
    naming the destination sends an Agent to chmod a file that had nothing to
    do with the failure.
    """
    src = tmp_path / "unreadable.bin"
    src.write_bytes(b"payload")
    os.chmod(src, 0o000)
    dest = tmp_path / "dest.bin"
    try:
        r = fs_ops.run(
            "put",
            ep="local",
            path=str(dest),
            local=str(src),
            home=FIXTURES,
        )
    finally:
        os.chmod(src, 0o644)

    assert r.status == "error", r.render_text()
    assert r.code == "PERMISSION_DENIED"
    # path stays the caller's destination; node_path is the object that failed.
    assert r.fields.get("path") == str(dest), r.render_text()
    assert r.fields.get("node_path") == str(src), r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(src) in msg, r.render_text()
    assert str(dest) not in msg, r.render_text()
    assert not dest.exists()
    assert _temp_files(tmp_path) == []


@pytest.mark.parametrize("existing", [True, False], ids=["existing_dir", "missing_dir"])
def test_local_get_denied_destination_names_the_local_destination(
    tmp_path: Path,
    existing: bool,
) -> None:
    """A get the local destination refuses is reported against that destination.

    The source is a readable regular file the transfer never touched, so the
    old row blamed the object that was working.
    """
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload")
    locked = tmp_path / "locked"
    locked.mkdir()
    dest = locked / "sub" / "out.bin"
    if existing:
        dest.parent.mkdir()
    # The level that refuses the write: the destination's own directory when it
    # is already there, otherwise the directory its creation goes through.
    refused = dest.parent if existing else locked
    os.chmod(refused, 0o555)
    try:
        r = fs_ops.run(
            "get",
            ep="local",
            path=str(src),
            local=str(dest),
            home=FIXTURES,
        )
    finally:
        os.chmod(refused, 0o755)

    assert r.status == "error", r.render_text()
    assert r.code == "PERMISSION_DENIED"
    assert r.fields.get("path") == str(src), r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(dest) in msg, r.render_text()
    assert str(src) not in msg, r.render_text()
    assert src.read_bytes() == b"payload"
    assert not dest.exists()
    assert _temp_files(refused) == []


def test_local_get_directory_destination_names_the_local_destination(
    tmp_path: Path,
) -> None:
    """"is a directory" is reported against the local directory that is there.

    The source is a regular file, so the old row made a false statement about
    it.
    """
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest-dir"
    dest.mkdir()

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(src),
        local=str(dest),
        home=FIXTURES,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "IS_A_DIR"
    assert r.fields.get("path") == str(src), r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(dest) in msg, r.render_text()
    assert str(src) not in msg, r.render_text()
    assert dest.is_dir() and list(dest.iterdir()) == []


def test_local_put_directory_destination_stays_on_the_destination(
    tmp_path: Path,
) -> None:
    """A destination-side failure keeps naming the destination.

    The caller's path is the object that failed, so the row gains no node_path.
    """
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest-dir"
    dest.mkdir()

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(dest),
        local=str(src),
        home=FIXTURES,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "IS_A_DIR"
    assert r.fields.get("path") == str(dest), r.render_text()
    assert "node_path" not in r.fields, r.render_text()
    msg = str(r.fields.get("msg") or "")
    assert str(dest) in msg, r.render_text()
    assert str(src) not in msg, r.render_text()


def test_local_get_destination_link_cycle_names_the_local_destination(
    tmp_path: Path,
) -> None:
    """A local destination link chain that cannot be walked is a local failure.

    The walk's exception names an element of the chain; the row must still
    name the local destination rather than the readable source the transfer
    only read from.
    """
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload")
    cycle = tmp_path / "cycle"
    cycle.mkdir()
    for name, target in (("cyc0", "cyc1"), ("cyc1", "cyc2"), ("cyc2", "cyc0")):
        (cycle / name).symlink_to(target)
    dest = cycle / "cyc0"

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(src),
        local=str(dest),
        home=FIXTURES,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "FS_ERROR"
    assert r.fields.get("path") == str(src), r.render_text()
    assert r.fields.get("node_path") == str(dest), r.render_text()
    assert src.read_bytes() == b"payload"
    assert dest.is_symlink(), r.render_text()
    assert _temp_files(cycle) == []


# ---------------------------------------------------------------------------
# local: write/put/get to symlink follows to target - link preserved
# ---------------------------------------------------------------------------


def test_local_write_symlink_follows_to_target(tmp_path: Path) -> None:
    """write to a symlink-to-file updates the target; symlink is preserved."""
    target = tmp_path / "target.txt"
    target.write_text("old-content", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    r = fs_ops.run(
        "write",
        ep="local",
        path=str(link),
        content="new-content",
        home=FIXTURES,
    )
    assert r.status == "ok"
    # Symlink still exists, pointing to the same target.
    assert os.path.islink(link)
    # Target content is updated.
    assert target.read_text(encoding="utf-8") == "new-content"
    # Reading via the link sees the new content (round-trip consistency).
    assert link.read_text(encoding="utf-8") == "new-content"


def test_local_write_symlink_preserves_target_mode(tmp_path: Path) -> None:
    """write through a symlink preserves the resolved target's mode."""
    target = tmp_path / "secret.txt"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o600)
    link = tmp_path / "link"
    link.symlink_to(target)

    r = fs_ops.run(
        "write",
        ep="local",
        path=str(link),
        content="new",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert os.path.islink(link)
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_local_write_symlink_chain_follows(tmp_path: Path) -> None:
    """write through a chain of symlinks resolves to the final target."""
    target = tmp_path / "real.txt"
    target.write_text("old", encoding="utf-8")
    link1 = tmp_path / "link1"
    link1.symlink_to(target)
    link2 = tmp_path / "link2"
    link2.symlink_to(link1)

    r = fs_ops.run(
        "write",
        ep="local",
        path=str(link2),
        content="via-chain",
        home=FIXTURES,
    )
    assert r.status == "ok"
    # Both symlinks preserved.
    assert os.path.islink(link1)
    assert os.path.islink(link2)
    # Final target updated.
    assert target.read_text(encoding="utf-8") == "via-chain"


def test_local_get_symlink_dst_follows_to_target(tmp_path: Path) -> None:
    """get to a symlink destination follows the link; symlink preserved."""
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload-data")
    target = tmp_path / "target.bin"
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    r = fs_ops.run(
        "get",
        ep="local",
        path=str(src),
        local=str(link),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert os.path.islink(link)
    assert target.read_bytes() == b"payload-data"


def test_local_put_symlink_dst_follows_to_target(tmp_path: Path) -> None:
    """put to a symlink destination follows the link; symlink preserved."""
    src = tmp_path / "source.bin"
    src.write_bytes(b"payload-data")
    target = tmp_path / "target.bin"
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    r = fs_ops.run(
        "put",
        ep="local",
        path=str(link),
        local=str(src),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert os.path.islink(link)
    assert target.read_bytes() == b"payload-data"


# ---------------------------------------------------------------------------
# local: the caller's path names the object the OS resolves
# ---------------------------------------------------------------------------


def test_local_dotdot_through_symlink_hits_referent_sibling(tmp_path: Path) -> None:
    """``a/link/../victim`` must reach the object the OS resolves (b/victim).

    Lexically collapsing ``..`` walks over the intermediate link
    (``a/link`` -> ``b/sub``) and names ``a/victim`` instead. read/write/rm
    must all land on b/victim and leave the sibling a/victim alone.
    """
    real = tmp_path / "b"
    (real / "sub").mkdir(parents=True)
    (real / "victim").write_text("REAL-B", encoding="utf-8")
    decoy = tmp_path / "a"
    decoy.mkdir()
    (decoy / "victim").write_text("DECOY-A", encoding="utf-8")
    (decoy / "link").symlink_to("../b/sub")

    through = f"{tmp_path}/a/link/../victim"

    r = fs_ops.run("read", ep="local", path=through, home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.body is not None and "REAL-B" in r.body

    w = fs_ops.run("write", ep="local", path=through, content="NEW-B", home=FIXTURES)
    assert w.status == "ok", w.render_text()
    assert (real / "victim").read_text(encoding="utf-8") == "NEW-B"
    assert (decoy / "victim").read_text(encoding="utf-8") == "DECOY-A"

    rm = fs_ops.run("rm", ep="local", path=through, home=FIXTURES)
    assert rm.status == "ok", rm.render_text()
    assert not (real / "victim").exists()
    assert (decoy / "victim").read_text(encoding="utf-8") == "DECOY-A"
    # The intermediate link is untouched by rm (only the final component goes).
    assert (decoy / "link").is_symlink()


def test_local_symlink_target_with_dotdot_resolves_via_os(tmp_path: Path) -> None:
    """A stored link target containing ``dirlink/../file`` is joined verbatim.

    Collapsing the target lexically names ``file`` in the link's own
    directory; letting the OS resolve ``..`` after following ``dirlink``
    names ``sub/file``. The referent the caller gets must be the latter.
    """
    (tmp_path / "sub" / "inner").mkdir(parents=True)
    (tmp_path / "sub" / "file").write_text("REAL-SUB", encoding="utf-8")
    (tmp_path / "file").write_text("DECOY-ROOT", encoding="utf-8")
    (tmp_path / "dirlink").symlink_to("sub/inner")
    link = tmp_path / "link"
    link.symlink_to("dirlink/../file")

    r = fs_ops.run("read", ep="local", path=str(link), home=FIXTURES)
    assert r.status == "ok", r.render_text()
    assert r.body is not None and "REAL-SUB" in r.body

    w = fs_ops.run("write", ep="local", path=str(link), content="NEW-SUB", home=FIXTURES)
    assert w.status == "ok", w.render_text()
    assert (tmp_path / "sub" / "file").read_text(encoding="utf-8") == "NEW-SUB"
    assert (tmp_path / "file").read_text(encoding="utf-8") == "DECOY-ROOT"
    # write updates the referent; the link entry stays a link.
    assert link.is_symlink()


def test_local_list_derived_rows_name_the_objects_they_describe(
    tmp_path: Path,
) -> None:
    """The "." and ".." rows of a listing resolve to the listed objects.

    A caller path that walks ".." through a symlink is listed from the
    directory the OS resolves it to, so the row naming that directory and the
    row naming its parent must resolve to that directory and its real parent.
    Deriving them lexically instead names the collapse target - a different
    object, or no object at all.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "sub").mkdir(parents=True)
    (tmp_path / "a" / "victim").write_text("DECOY-A", encoding="utf-8")
    (tmp_path / "b" / "sub" / "child.txt").write_text("REAL-B", encoding="utf-8")
    (tmp_path / "a" / "link").symlink_to("../b/sub")

    backend = LocalFs()

    # The link is followed before "..", so this lists b/sub.
    rows = {e.name: e for e in backend.list(f"{tmp_path}/a/link/../sub").entries}
    assert set(rows) >= {".", "..", "child.txt"}
    for name in (".", ".."):
        assert os.path.exists(rows[name].path), (name, rows[name].path)
    assert os.path.samefile(rows["."].path, tmp_path / "b" / "sub")
    assert os.path.samefile(rows[".."].path, tmp_path / "b")

    # A caller path ending in ".." lists b too, whose parent is the root here.
    rows = {e.name: e for e in backend.list(f"{tmp_path}/a/link/..").entries}
    for name in (".", ".."):
        assert os.path.exists(rows[name].path), (name, rows[name].path)
    assert os.path.samefile(rows["."].path, tmp_path / "b")
    assert os.path.samefile(rows[".."].path, tmp_path)

    # An ordinary directory listing keeps both rows on the objects they name.
    rows = {e.name: e for e in backend.list(str(tmp_path / "b")).entries}
    assert os.path.samefile(rows["."].path, tmp_path / "b")
    assert os.path.samefile(rows[".."].path, tmp_path)


def test_local_whitespace_names_stay_distinct(tmp_path: Path) -> None:
    """``x``, ``x `` and `` x`` are three different files.

    Trimming the caller's path collapses them onto one object, so a read or
    rm issued for one would hit another. Every op must use the string as
    given and report it back unchanged.
    """
    contents = {"x": "PLAIN", "x ": "TRAILING", " x": "LEADING"}
    for name, text in contents.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    for name, text in contents.items():
        r = fs_ops.run("read", ep="local", path=f"{tmp_path}/{name}", home=FIXTURES)
        assert r.status == "ok", r.render_text()
        assert r.body is not None and text in r.body, (name, r.render_text())
        s = fs_ops.run("stat", ep="local", path=f"{tmp_path}/{name}", home=FIXTURES)
        assert s.status == "ok", s.render_text()
        assert s.fields.get("path") == f"{tmp_path}/{name}"
        assert s.fields.get("bytes") == len(text)

    w = fs_ops.run(
        "write", ep="local", path=f"{tmp_path}/x", content="NEW-PLAIN", home=FIXTURES
    )
    assert w.status == "ok", w.render_text()
    assert (tmp_path / "x").read_text(encoding="utf-8") == "NEW-PLAIN"
    assert (tmp_path / "x ").read_text(encoding="utf-8") == "TRAILING"
    assert (tmp_path / " x").read_text(encoding="utf-8") == "LEADING"

    rm = fs_ops.run("rm", ep="local", path=f"{tmp_path}/ x", home=FIXTURES)
    assert rm.status == "ok", rm.render_text()
    assert not (tmp_path / " x").exists()
    assert (tmp_path / "x").read_text(encoding="utf-8") == "NEW-PLAIN"
    assert (tmp_path / "x ").read_text(encoding="utf-8") == "TRAILING"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_fs_list_local(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["fs", "list", "--ep", "local", "--path", "/tmp"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@fs list ok")
    assert "path=" in out
    # absolute path in output
    assert "path=/tmp" in out or "path=/private/tmp" in out


def test_cli_fs_list_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(
        ["fs", "list", "--ep", "local", "--path", str(tmp_path), "--json"]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["kind"] == "fs"
    assert data["status"] == "ok"
    assert data["op"] == "list"
    assert Path(data["path"]).is_absolute()


def test_cli_fs_missing_path(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["fs", "list", "--ep", "local"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@fs list error" in out
    assert "MISSING_ARG" in out


def test_cli_fs_read_image_meta_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """CLI image read prints type/MIME/bytes, not Base64 file bytes."""
    target = tmp_path / "pic.png"
    target.write_bytes(_PNG)
    code = main(["fs", "read", "--ep", "local", "--path", str(target)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@fs read ok")
    assert "type=image" in out
    assert "mime_type=image/png" in out
    assert f"bytes={len(_PNG)}" in out
    assert base64.b64encode(_PNG).decode("ascii") not in out


def test_cli_fs_read_image_json_omits_bytes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "pic.png"
    target.write_bytes(_PNG)
    code = main(["fs", "read", "--ep", "local", "--path", str(target), "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["type"] == "image"
    assert data["mime_type"] == "image/png"
    assert data["bytes"] == len(_PNG)
    assert "image_data" not in data
    assert "body" not in data
    assert base64.b64encode(_PNG).decode("ascii") not in out


# ---------------------------------------------------------------------------
# winrm: failure rows carry machine-readable link tokens + the caller's path
# ---------------------------------------------------------------------------

try:  # pypsrp is an optional extra; the doubles below cover its absence.
    from pypsrp import exceptions as _pypsrp_exceptions
except Exception:  # noqa: BLE001 - importability probe
    _pypsrp_exceptions = None


def _pypsrp_error(qualname: str, *args: object) -> BaseException:
    """A pypsrp exception (real when importable, else a name-matching double).

    The link classifiers match on the type's own (module, qualname), so a
    double describes a pypsrp failure exactly as the real class would.
    """
    if _pypsrp_exceptions is not None:
        real = getattr(_pypsrp_exceptions, qualname, None)
        if real is not None:
            return real(*args)
    return type(qualname, (Exception,), {"__module__": "pypsrp.exceptions"})(*args)


class _RejectingLinkSession(FakePypsrpSession):
    """pypsrp-shaped session whose live link starts refusing on demand.

    Identity seeds let the endpoint open without a probe round trip; setting
    *reject* then poisons the live session in place, the way a real link's
    security context goes stale under an idle connection.
    """

    def __init__(self) -> None:
        super().__init__()
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.cwd = HOME
        self.home = HOME
        self.reject: BaseException | None = None

    def close(self) -> None:
        self.closed = True

    def execute_ps(self, script: str, *, environment: object = None) -> object:
        if self.reject is not None:
            raise self.reject
        return super().execute_ps(script, environment=environment)


class _ProbeFailingSession(FakePypsrpSession):
    """Session that refuses the open-time identity probe (no identity seeds)."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    def close(self) -> None:
        self.closed = True

    def execute_ps(self, script: str, *, environment: object = None) -> object:
        raise self._exc


def _session_factory(
    exc: BaseException | None = None,
) -> tuple[list[object], object]:
    """Connector building a fresh session per call; returns (sessions, connector)."""
    sessions: list[object] = []

    def connector(**_kwargs: object) -> object:
        session = (
            _ProbeFailingSession(exc) if exc is not None else _RejectingLinkSession()
        )
        sessions.append(session)
        return session

    return sessions, connector


def test_fs_winrm_link_failure_row_carries_tokens_and_destination(
    tmp_path: Path,
) -> None:
    """A failed put names the caller's destination and the transport's tokens.

    The row used to carry only op/ep/path and a raw pypsrp message, with the
    path pointing at whichever node the op had reached (the parent dir the
    mkdir_p stat touched), so an Agent could not attribute the failure to the
    file it asked for nor tell a dead link from a remote path verdict.
    """
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 4096)
    sessions, connector = _session_factory()
    dest = rf"{TEMP}\b.bin"

    warm = fs_ops.run(
        "put",
        ep="lab-win",
        path=rf"{TEMP}\warm.bin",
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )
    assert warm.status == "ok", warm.render_text()
    # A healthy row gains no link keys.
    assert "link_lost" not in warm.fields
    assert "reopen_hint" not in warm.fields
    assert "node_path" not in warm.fields

    assert isinstance(sessions[0], _RejectingLinkSession)
    sessions[0].reject = _pypsrp_error("WinRMTransportError", "http", 400, "")
    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )

    assert r.status == "error", r.render_text()
    assert r.code == "FS_ERROR"
    assert r.fields.get("path") == dest, r.render_text()
    assert r.fields.get("node_path") == TEMP, r.render_text()
    assert r.fields.get("link_lost") == 1, r.render_text()
    assert r.fields.get("marked_dead") is True, r.render_text()
    assert r.fields.get("reopen_hint") == "endpoint close then open"

    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.is_connected() is False

    # README contract: the fs path retires the endpoint, so the next call
    # lazily reconnects and lands the payload.
    r2 = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok", r2.render_text()
    assert len(sessions) == 2


def test_fs_winrm_auth_refusal_retires_link_not_handed_back_live(
    tmp_path: Path,
) -> None:
    """A WSMan-layer 401 refusal must not leave a live-looking handle.

    pypsrp's AuthenticationError is not in the fs client's link-failure
    predicate, so nothing retires the poisoned session: the transport keeps
    reporting connected, ``endpoint open`` reconnects nothing, and every later
    call fails identically. Core retires it from the failure the op observed.
    """
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 4096)
    sessions, connector = _session_factory()

    warm = fs_ops.run(
        "put",
        ep="lab-win",
        path=rf"{TEMP}\warm.bin",
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )
    assert warm.status == "ok", warm.render_text()

    assert isinstance(sessions[0], _RejectingLinkSession)
    sessions[0].reject = _pypsrp_error(
        "AuthenticationError", "Failed to authenticate the user lab with ntlm"
    )
    dest = rf"{TEMP}\refused.bin"
    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )

    assert r.status == "error", r.render_text()
    assert r.fields.get("path") == dest, r.render_text()
    assert r.fields.get("link_lost") == 1, r.render_text()
    assert r.fields.get("reopen_hint") == "endpoint close then open"
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    # The dead handle is not handed back as live: the remedy reconnects.
    assert ep.transport.is_connected() is False

    r2 = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok", r2.render_text()
    assert len(sessions) == 2, "refused link must be replaced, not reused"


def test_fs_winrm_reconnect_rejection_row_is_distinguishable(
    tmp_path: Path,
) -> None:
    """A refused reconnect and an unreachable host are different rows.

    Both surface as NOT_CONNECTED "identity probe failed: ...". The refusal
    carries the HTTP status tokens; an unreachable host has none, so an Agent
    can branch (reopen vs. treat the host as down) on fields, not prose.
    """
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 4096)
    dest = rf"{TEMP}\x.bin"

    _, refused_connector = _session_factory(_pypsrp_error("WinRMTransportError", "http", 400, ""))
    refused = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=refused_connector,
    )
    assert refused.status == "error", refused.render_text()
    assert refused.code == "NOT_CONNECTED"
    assert refused.fields.get("path") == dest
    assert refused.fields.get("probe_failed") == 1, refused.render_text()
    assert refused.fields.get("rejected") == 1, refused.render_text()
    assert refused.fields.get("http_status") == 400, refused.render_text()

    _, down_connector = _session_factory(
        ConnectionError(
            "HTTPConnectionPool(host='10.0.0.20', port=5985): Max retries "
            "exceeded with url: /wsman"
        )
    )
    down = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(payload),
        home=FIXTURES,
        connector=down_connector,
    )
    assert down.status == "error", down.render_text()
    assert down.code == "NOT_CONNECTED"
    assert down.fields.get("probe_failed") == 1, down.render_text()
    # Nothing answered with an HTTP status: not a refusal.
    assert "rejected" not in down.fields
    assert "http_status" not in down.fields
