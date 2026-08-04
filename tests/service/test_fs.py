"""Service tests: fs local full + sftp mock (T08)."""

from __future__ import annotations

import json
import os
import stat as statmod
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.fs.backends.sftp import SftpFs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


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
    # via is optional meta — may be present
    if "via=" in text:
        assert "via=local" in text


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
    """C4: a UTF-16LE file with BOM reads as text (utf-16) via the local
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
    """C4: UTF-16LE without a BOM (alternating-NUL ASCII) is detected as text
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
    """C4 guard: random binary with a NUL byte (no alternating-NUL pattern)
    is NOT misclassified as UTF-16 — true binary stays binary."""
    target = tmp_path / "bin.dat"
    target.write_bytes(b"\x00\x01\x02\x03\x04\x05binary")

    r = fs_ops.run("read", ep="local", path=str(target), home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_local_read_big_endian_uint32_stays_binary(tmp_path: Path) -> None:
    """LOW re-review guard: a NUL-dense binary with NON-PRINTABLE non-NUL bytes
    is NOT misclassified as UTF-16. A 12-byte big-endian uint32 file
    (``\\x00\\x00\\x00\\x01...``) has every even byte NUL — the old
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
    """LOW re-review guard: real UTF-16BE without a BOM (ASCII letters with NUL
    at every even index) is STILL detected as text after the printable-ratio
    guard — the BE direction's true positive is preserved."""
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


def test_invalid_op() -> None:
    r = fs_ops.run("explode", ep="local", path="/tmp", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "INVALID_OP"


# ---------------------------------------------------------------------------
# local: symlink semantics (O2) — stat/rm/read must not follow links
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
    """rm on a symlink removes the link only — the target MUST survive."""
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
# local: atomic writes (O2) — failure must not truncate the original
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
    # Original content is intact — no truncation.
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
# local: atomic write mode preservation (O2 HIGH) — permissions must survive
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


# ---------------------------------------------------------------------------
# local: write/put/get to symlink follows to target (O2 MED) — link preserved
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
# sftp mock
# ---------------------------------------------------------------------------


class _MockSftpFile:
    def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
        self._store = store
        self._path = path
        self._mode = mode
        self._buf = bytearray()
        if "r" in mode:
            self._data = store.get(path, b"")
            self._pos = 0
        else:
            self._data = b""
            self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def close(self) -> None:
        if "w" in self._mode:
            self._store[self._path] = bytes(self._buf)


class _MockAttrs:
    def __init__(self, mode: int, size: int = 0, mtime: float = 0.0) -> None:
        self.permissions = mode
        self.size = size
        self.mtime = mtime


class _SFTPName:
    """asyncssh SFTPName stand-in: carries .filename and .attrs."""

    def __init__(self, filename: str, attrs: _MockAttrs) -> None:
        self.filename = filename
        self.attrs = attrs


class MockSftp:
    """In-memory SFTP-like client for unit/service tests (no network).

    Models dirs/files/symlinks and exposes both the asyncssh-style ``readdir``
    (yielding ``_SFTPName`` with attrs) and the bare-string ``listdir``. The
    call counters (``stat_calls``/``open_calls``/``readdir_calls``) let tests
    assert that ``list`` reuses readdir attrs and that ``read`` skips its
    pre-stat. ``posix_rename``/``readlink`` exercise the atomic-write and
    symlink-target code paths.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.links: dict[str, str] = {}
        self.stat_calls = 0
        self.open_calls = 0
        self.readdir_calls = 0

    def _norm(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def _children(self, path: str) -> dict[str, str]:
        path = self._norm(path)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        if path == "/":
            prefix = "/"
        result: dict[str, str] = {}
        for d in self.dirs:
            if d == path or d == "/":
                continue
            if d.startswith(prefix):
                rest = d[len(prefix) :] if prefix != "/" else d.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = d
        for f in self.files:
            if f.startswith(prefix) or (prefix == "/" and f.startswith("/")):
                rest = f[len(prefix) :] if prefix != "/" else f.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = f
        for link in self.links:
            if link.startswith(prefix) or (prefix == "/" and link.startswith("/")):
                rest = link[len(prefix) :] if prefix != "/" else link.lstrip("/")
                if rest and "/" not in rest:
                    result[rest] = link
        return result

    def _attrs_for(self, path: str) -> _MockAttrs:
        path = self._norm(path)
        if path in self.links:
            return _MockAttrs(statmod.S_IFLNK | 0o777, 0, 1.0)
        if path in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if path in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[path]), 1.0)
        raise FileNotFoundError(path)

    def listdir(self, path: str) -> list[str]:
        return sorted(self._children(path).keys())

    def readdir(self, path: str) -> list[_SFTPName]:
        self.readdir_calls += 1
        kids = self._children(path)
        return [
            _SFTPName(name, self._attrs_for(abs_p))
            for name, abs_p in sorted(kids.items())
        ]

    def stat(self, path: str) -> _MockAttrs:
        self.stat_calls += 1
        return self._attrs_for(path)

    def lstat(self, path: str) -> _MockAttrs:
        return self.stat(path)

    def readlink(self, path: str) -> str:
        path = self._norm(path)
        if path not in self.links:
            raise FileNotFoundError(path)
        return self.links[path]

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        elif src in self.links:
            self.links[dst] = self.links.pop(src)
        else:
            raise FileNotFoundError(src)

    def mkdir(self, path: str) -> None:
        path = self._norm(path)
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.dirs:
            raise FileNotFoundError(parent)
        self.dirs.add(path)

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path in self.files:
            del self.files[path]
            return
        if path in self.links:
            del self.links[path]
            return
        raise FileNotFoundError(path)

    def rmdir(self, path: str) -> None:
        path = self._norm(path)
        if path not in self.dirs or path == "/":
            raise FileNotFoundError(path)
        # non-empty?
        for f in self.files:
            if f.startswith(path + "/"):
                raise OSError("not empty")
        for d in self.dirs:
            if d != path and d.startswith(path + "/"):
                raise OSError("not empty")
        self.dirs.discard(path)

    def open(self, path: str, mode: str = "r") -> _MockSftpFile:
        path = self._norm(path)
        self.open_calls += 1
        if "r" in mode:
            if path in self.dirs:
                raise IsADirectoryError(path)
            if path not in self.files:
                raise FileNotFoundError(path)
        return _MockSftpFile(self.files, path, mode)

    def put(self, local: str, remote: str) -> None:
        remote = self._norm(remote)
        data = Path(local).read_bytes()
        self.files[remote] = data

    def get(self, remote: str, local: str) -> None:
        remote = self._norm(remote)
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])


def test_sftp_mock_list_and_read() -> None:
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.dirs.add("/var/log")
    mock.files["/var/log/syslog"] = b"log-line-1\nlog-line-2\n"

    backend = SftpFs(mock, cwd="/var", home="/home/deploy")
    r = fs_ops.run(
        "list",
        ep="lab-ssh",
        path="/var/log",
        home=FIXTURES,
        backend=backend,
    )
    # When backend is injected, ep may still be set; no real connect.
    assert r.status == "ok"
    assert r.fields.get("op") == "list"
    assert r.fields.get("path") == "/var/log"
    assert Path(r.fields["path"]).is_absolute() or r.fields["path"].startswith("/")
    assert "syslog" in (r.body or "")
    # via=sftp optional
    if r.fields.get("via"):
        assert r.fields["via"] == "sftp"

    r2 = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/var/log/syslog",
        home=FIXTURES,
        backend=backend,
    )
    assert r2.status == "ok"
    assert r2.fields.get("path") == "/var/log/syslog"
    assert r2.body is not None
    assert "log-line-1" in r2.body


def test_sftp_mock_via_ssh_connector() -> None:
    """end-to-end: ensure_endpoint + open_sftp on mock connection."""
    mock = MockSftp()
    mock.dirs.add("/tmp")
    mock.files["/tmp/hello.txt"] = b"sftp-hello\n"

    class Conn:
        cwd = "/tmp"
        home = "/home/deploy"

        def start_sftp_client(self) -> MockSftp:
            return mock

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = fs_ops.run(
        "list",
        ep="lab-ssh",
        path="/tmp",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("path") == "/tmp"
    assert "hello.txt" in (r.body or "")
    if r.fields.get("via"):
        assert r.fields["via"] == "sftp"

    r2 = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/tmp/hello.txt",
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok"
    assert "sftp-hello" in (r2.body or "")
    assert r2.fields["path"] == "/tmp/hello.txt"


def test_sftp_mock_missing_path() -> None:
    mock = MockSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "stat",
        ep="lab-ssh",
        path="/missing",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# sftp: O8 — recursive list, readdir attrs, reconnect, atomic write, symlinks
# ---------------------------------------------------------------------------


class ChannelClosed(Exception):
    """Stand-in for an asyncssh channel-closed error (duck-typed by name)."""


class _DeadSftp:
    """Every op raises a channel-closed error (simulates a dropped channel)."""

    def stat(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")

    def lstat(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")

    def readdir(self, path: str) -> None:
        raise ChannelClosed("channel closed by remote")


class _FailWriteFile:
    """A remote file handle whose write always fails (simulates mid-upload)."""

    def write(self, data: bytes) -> int:
        raise OSError("simulated disk full")

    def close(self) -> None:
        pass


class _AtomicFailSftp:
    """SFTP mock with posix_rename where writes to a temp path always fail.

    Used to prove write/put atomicity: the final remote file must survive a
    mid-upload failure, and the temp must be removed.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original"}
        self.dirs: set[str] = {"/", "/d"}
        self.removed_temps: list[str] = []

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def mkdir(self, p: str) -> None:
        self.dirs.add(self._norm(p))

    def open(self, p: str, mode: str = "r") -> object:
        p = self._norm(p)
        if "w" in mode and ".mrc-tmp-" in p:
            return _FailWriteFile()  # writes to the temp path fail
        if "r" in mode:
            if p in self.dirs:
                raise IsADirectoryError(p)
            if p not in self.files:
                raise FileNotFoundError(p)
            data = self.files[p]

            class _R:
                def __init__(self, d: bytes) -> None:
                    self._d = d
                    self._p = 0

                def read(self, n: int = -1) -> bytes:
                    if n is None or n < 0:
                        r = self._d[self._p :]
                        self._p = len(self._d)
                        return r
                    r = self._d[self._p : self._p + n]
                    self._p += len(r)
                    return r

                def close(self) -> None:
                    pass

            return _R(data)
        # write to a non-temp path: buffer + commit (not used by the atomic path).
        key = p
        store = self.files

        class _W:
            def __init__(self) -> None:
                self._buf = bytearray()

            def write(self, d: bytes) -> int:
                self._buf.extend(d)
                return len(d)

            def close(self) -> None:
                store[key] = bytes(self._buf)

        return _W()

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def remove(self, p: str) -> None:
        p = self._norm(p)
        if p in self.files:
            del self.files[p]
        if ".mrc-tmp-" in p:
            self.removed_temps.append(p)


class _FailReadFile:
    """A remote file handle whose read fails partway (simulates mid-download).

    A full ``read()`` (no arg / n<0, used by the no-progress ``_read_bytes``
    path) fails immediately so no bytes are ever returned. A chunked
    ``read(n)`` (n>0, used by the progress ``_get_with_progress`` path) returns
    a small chunk on the first call so a partial temp is written, then raises
    on every subsequent call. Either way the atomic get path must remove the
    temp and leave the pre-existing destination untouched.
    """

    def __init__(self, data: bytes, chunk: int = 10) -> None:
        self._data = data
        self._chunk = chunk
        self._calls = 0

    def read(self, n: int = -1) -> bytes:
        self._calls += 1
        if n is None or n < 0:
            # Full-read path: fail before returning any bytes.
            raise OSError("simulated mid-download drop")
        if self._calls > 1:
            # Chunked path: second+ chunk fails (mid-download).
            raise OSError("simulated mid-download drop")
        return self._data[: min(n, self._chunk)]

    def close(self) -> None:
        pass


class _GetFailSftp:
    """SFTP mock where reads fail partway through (simulates a mid-get drop).

    Used to prove sftp get atomicity: the local destination must survive a
    mid-download failure, and the local temp must be removed. Exposes only
    ``open`` (no ``get``/``read_file``/``posix_rename``) so both the progress
    and no-progress branches exercise the open+read code path.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/d/final.txt": b"original-remote-content"}
        self.dirs: set[str] = {"/", "/d"}

    def _norm(self, p: str) -> str:
        p = p if p.startswith("/") else "/" + p
        while "//" in p:
            p = p.replace("//", "/")
        if p != "/" and p.endswith("/"):
            p = p.rstrip("/")
        return p or "/"

    def stat(self, p: str) -> _MockAttrs:
        p = self._norm(p)
        if p in self.dirs:
            return _MockAttrs(statmod.S_IFDIR | 0o755, 0, 1.0)
        if p in self.files:
            return _MockAttrs(statmod.S_IFREG | 0o644, len(self.files[p]), 1.0)
        raise FileNotFoundError(p)

    def lstat(self, p: str) -> _MockAttrs:
        return self.stat(p)

    def open(self, p: str, mode: str = "r") -> _FailReadFile:
        p = self._norm(p)
        if "r" in mode:
            if p in self.dirs:
                raise IsADirectoryError(p)
            if p not in self.files:
                raise FileNotFoundError(p)
            return _FailReadFile(self.files[p])
        raise OSError("unexpected write")


def test_sftp_recursive_list_name_not_corrupted() -> None:
    """H2: recursive list at depth >=3 with a child basename starting with the
    parent basename must produce correct relative names (the old startswith
    heuristic dropped the prefix for sub_file under sub and sub2/bar)."""
    mock = MockSftp()
    mock.dirs.update({"/d", "/d/sub", "/d/sub/sub2"})
    mock.files["/d/sub/sub_file"] = b"x"
    mock.files["/d/sub/sub2/bar"] = b"yy"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.list("/d", recursive=True)
    by_name = {e.name: e for e in result.entries}
    # Correct relative paths preserved at every depth.
    assert "sub" in by_name and by_name["sub"].kind == "d"
    assert "sub/sub_file" in by_name and by_name["sub/sub_file"].kind == "f"
    assert by_name["sub/sub_file"].path == "/d/sub/sub_file"
    assert "sub/sub2" in by_name and by_name["sub/sub2"].kind == "d"
    assert "sub/sub2/bar" in by_name and by_name["sub/sub2/bar"].kind == "f"
    assert by_name["sub/sub2/bar"].path == "/d/sub/sub2/bar"
    # The corrupted unprefixed names must NOT appear.
    assert "sub_file" not in by_name
    assert "sub2/bar" not in by_name


def test_sftp_list_reuses_readdir_attrs_no_per_child_stat() -> None:
    """F + LOW: list reuses readdir attrs (no per-child stat) and reuses the
    dir attrs for "." (no second stat of the same path)."""
    mock = MockSftp()
    mock.dirs.update({"/dir"})
    mock.files["/dir/a"] = b"1"
    mock.files["/dir/b"] = b"22"
    mock.files["/dir/c"] = b"333"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    result = backend.list("/dir")
    by_name = {e.name: e for e in result.entries}
    assert set(by_name) >= {".", "..", "a", "b", "c"}
    # readdir supplied the children's attrs (one readdir for the dir contents).
    assert mock.readdir_calls == 1, f"expected 1 readdir, got {mock.readdir_calls}"
    # Only 2 stats: the dir (kind check, reused for ".") + the parent for "..".
    # A per-child stat or a second dir stat for "." would raise this to 3+.
    assert mock.stat_calls == 2, f"expected 2 stats, got {mock.stat_calls}"
    # "." carries the dir's mode/mtime (reused attrs, not the bare fallback).
    assert by_name["."].mode is not None
    assert by_name["."].mtime is not None
    assert by_name["a"].kind == "f" and by_name["a"].size == 1
    assert by_name["b"].kind == "f" and by_name["b"].size == 2
    assert by_name["c"].kind == "f" and by_name["c"].size == 3


def test_sftp_read_dir_maps_is_a_dir_without_pre_stat() -> None:
    """LOW: read on a dir maps to IS_A_DIR via the open failure, with no
    pre-stat (open is attempted; stat is not called)."""
    mock = MockSftp()
    mock.dirs.update({"/dir"})
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "read", ep="lab-ssh", path="/dir", home=FIXTURES, backend=backend
    )
    assert r.status == "error"
    assert r.code == "IS_A_DIR"
    # No pre-stat: read no longer calls stat.
    assert mock.stat_calls == 0
    # open was attempted (and its IsADirectoryError mapped to IS_A_DIR).
    assert mock.open_calls >= 1


def test_sftp_client_reconnects_after_channel_closed() -> None:
    """F: after a channel-closed error on an op, the cached client is reset
    and the next op re-invokes the factory (reconnect works)."""
    factory_calls = {"n": 0}
    working = MockSftp()
    working.dirs.update({"/x"})
    working.files["/x/hello.txt"] = b"reconnected\n"

    def factory() -> object:
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            return _DeadSftp()
        return working

    backend = SftpFs(factory=factory, cwd="/", home="/home/u")
    # First op fails on the dead client; the cached client is reset.
    r1 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r1.status == "error"
    assert backend._client is None
    assert factory_calls["n"] == 1
    # Second op re-invokes the factory and succeeds on the fresh client.
    r2 = fs_ops.run(
        "list", ep="lab-ssh", path="/x", home=FIXTURES, backend=backend
    )
    assert r2.status == "ok"
    assert factory_calls["n"] == 2
    assert backend._client is not None
    assert "hello.txt" in (r2.body or "")


def test_sftp_write_atomic_failure_preserves_remote() -> None:
    """Theme D: write that fails mid-upload leaves NO partial remote file at
    the final path (temp removed; final unchanged)."""
    mock = _AtomicFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/final.txt",
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    # Final file untouched (no partial write at the final path).
    assert mock.files.get("/d/final.txt") == b"original"
    # No temp file left behind.
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    # Temp cleanup was attempted.
    assert mock.removed_temps


def test_sftp_put_atomic_failure_preserves_remote(tmp_path: Path) -> None:
    """Theme D: put that fails mid-upload leaves NO partial remote file at the
    final path (temp removed; final unchanged)."""
    mock = _AtomicFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "local.bin"
    src.write_bytes(b"would-be-payload")
    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert mock.files.get("/d/final.txt") == b"original"
    assert [k for k in mock.files if ".mrc-tmp-" in k] == []
    assert mock.removed_temps


def test_sftp_get_atomic_failure_preserves_local(tmp_path: Path) -> None:
    """Theme D: sftp get that fails mid-download leaves NO partial file at the
    local destination (local temp removed; pre-existing dst unchanged). The
    progress branch streams chunks into a local temp, then os.replace — a
    mid-read failure must remove the temp and preserve the original dst."""
    mock = _GetFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    dst = tmp_path / "downloaded.bin"
    # Pre-existing destination must survive across the failed get.
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        progress=lambda d, t: None,  # exercise the chunked _get_with_progress path
    )
    assert r.status == "error"
    # Pre-existing destination intact — no partial write at the final path.
    assert dst.read_bytes() == b"pre-existing-local-content"
    # No local temp file left behind in dst.parent.
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_sftp_get_atomic_failure_no_progress_preserves_local(tmp_path: Path) -> None:
    """Theme D: the no-progress sftp get branch is also atomic — a mid-read
    failure (``_read_bytes`` open+read path) leaves the pre-existing dst
    unchanged and removes the local temp."""
    mock = _GetFailSftp()
    backend = SftpFs(mock, cwd="/", home="/home/u")
    dst = tmp_path / "downloaded.bin"
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-ssh",
        path="/d/final.txt",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        # no progress callback → _read_bytes open+read path
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"pre-existing-local-content"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert len(temps) == 0


def test_sftp_stat_symlink_reports_target() -> None:
    """LOW: stat on a symlink returns kind=link and the target via readlink."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.txt"] = b"content"
    mock.links["/d/link"] = "/d/target.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    r = fs_ops.run(
        "stat", ep="lab-ssh", path="/d/link", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "link"
    assert r.fields.get("target") == "/d/target.txt"


def test_sftp_write_symlink_preserves_link() -> None:
    """Atomic write through a symlink updates the target; link entry preserved.

    Matches local backend policy: posix_rename must land on the final referent,
    not replace the symlink directory entry with a regular file.
    """
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.txt"] = b"old-content"
    mock.links["/d/link"] = "/d/target.txt"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/link",
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Symlink directory entry still a link to the same target.
    assert "/d/link" in mock.links
    assert mock.links["/d/link"] == "/d/target.txt"
    assert "/d/link" not in mock.files
    # Referent content updated.
    assert mock.files["/d/target.txt"] == b"new-content"
    # Public stat still reports kind=link.
    r_stat = fs_ops.run(
        "stat", ep="lab-ssh", path="/d/link", home=FIXTURES, backend=backend
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "link"
    assert r_stat.fields.get("target") == "/d/target.txt"


def test_sftp_put_symlink_preserves_link(tmp_path: Path) -> None:
    """Atomic put through a symlink updates the target; link entry preserved."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/target.bin"] = b"old"
    mock.links["/d/link.bin"] = "/d/target.bin"
    backend = SftpFs(mock, cwd="/", home="/home/u")
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-link")

    r = fs_ops.run(
        "put",
        ep="lab-ssh",
        path="/d/link.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "/d/link.bin" in mock.links
    assert mock.links["/d/link.bin"] == "/d/target.bin"
    assert "/d/link.bin" not in mock.files
    assert mock.files["/d/target.bin"] == b"payload-via-link"


def test_sftp_write_symlink_chain_preserves_links() -> None:
    """Atomic write through a symlink chain resolves to the final target."""
    mock = MockSftp()
    mock.dirs.update({"/d"})
    mock.files["/d/real.txt"] = b"old"
    mock.links["/d/link1"] = "/d/real.txt"
    mock.links["/d/link2"] = "/d/link1"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    r = fs_ops.run(
        "write",
        ep="lab-ssh",
        path="/d/link2",
        content="via-chain",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert mock.links["/d/link1"] == "/d/real.txt"
    assert mock.links["/d/link2"] == "/d/link1"
    assert "/d/link1" not in mock.files
    assert "/d/link2" not in mock.files
    assert mock.files["/d/real.txt"] == b"via-chain"


# ---------------------------------------------------------------------------
# sftp: C4 — UTF-16 detection (shared detect_text) + _rmtree readdir-attrs
# ---------------------------------------------------------------------------


def test_sftp_read_utf16le_with_bom() -> None:
    """C4: SFTP read of a UTF-16LE file (BOM) returns text. SFTP from Windows
    servers commonly serves UTF-16; the simple any-NUL heuristic previously
    misclassified it as binary. The shared detect_text now detects it."""
    mock = MockSftp()
    mock.dirs.add("/var")
    text = "hello-sftp"
    mock.files["/var/u16.txt"] = b"\xff\xfe" + text.encode("utf-16-le")
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/u16.txt", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16"
    assert text in (r.body or "")


def test_sftp_read_utf16le_no_bom() -> None:
    """C4: SFTP read of no-BOM UTF-16LE (alternating-NUL ASCII) is detected as
    text via the shared heuristic; previously binary."""
    mock = MockSftp()
    mock.dirs.add("/var")
    text = "plain-ascii-content"
    raw = text.encode("utf-16-le")
    assert b"\x00" in raw and raw[1] == 0
    mock.files["/var/u16nobom.txt"] = raw
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read",
        ep="lab-ssh",
        path="/var/u16nobom.txt",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "text"
    assert r.fields.get("encoding") == "utf-16-le"
    assert text in (r.body or "")


def test_sftp_read_binary_with_nul_stays_binary() -> None:
    """C4 guard: random binary with a NUL byte (no alternating-NUL pattern)
    stays binary on sftp — the shared heuristic doesn't misfire."""
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.files["/var/bin.dat"] = b"\x00\x01\x02\x03\x04\x05binary"
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/bin.dat", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_sftp_read_big_endian_uint32_stays_binary() -> None:
    """LOW re-review guard: a NUL-dense binary with NON-PRINTABLE non-NUL bytes
    is NOT misclassified as UTF-16 on sftp (shared detect_text). The 12-byte
    big-endian uint32 repro (``\\x00\\x00\\x00\\x01...``) previously reported
    ``utf-16-be`` + gibberish text; the printable-ratio guard now rejects it
    as binary because the non-NUL bytes (0x01/0x02/0x03) are non-printable."""
    mock = MockSftp()
    mock.dirs.add("/var")
    mock.files["/var/u32.bin"] = b"\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03"
    backend = SftpFs(mock, cwd="/var", home="/home/u")

    r = fs_ops.run(
        "read", ep="lab-ssh", path="/var/u32.bin", home=FIXTURES, backend=backend
    )
    assert r.status == "ok"
    assert r.fields.get("type") == "binary"
    assert r.fields.get("encoding") is None
    assert r.body is None or r.body == ""


def test_sftp_rmtree_uses_readdir_attrs_no_per_child_stat() -> None:
    """C4: _rmtree uses _scandir (readdir attrs) to recover each child's
    kind — the same N+1 fix ``list`` got. No per-child _stat when readdir
    yields attrs (the asyncssh common path)."""
    mock = MockSftp()
    mock.dirs.update({"/tree", "/tree/sub"})
    mock.files["/tree/a"] = b"1"
    mock.files["/tree/b"] = b"22"
    mock.files["/tree/sub/c"] = b"333"
    backend = SftpFs(mock, cwd="/", home="/home/u")

    # Counters reset right before rm so the assertion only counts _rmtree's
    # work (rm itself still does 1 stat for the top-level dir kind check).
    mock.stat_calls = 0
    mock.readdir_calls = 0
    result = backend.rm("/tree", recursive=True)

    assert result == "/tree"
    # Tree fully removed.
    assert "/tree" not in mock.dirs
    assert "/tree/sub" not in mock.dirs
    assert all(not f.startswith("/tree/") for f in mock.files)
    # Only the rm top-level stat — _rmtree derives kinds from readdir attrs.
    # (The old per-child _stat path would have left this at 5: 1 + a/b/sub + c.)
    assert mock.stat_calls == 1, (
        f"expected 1 stat (rm top-level only), got {mock.stat_calls}"
    )
    # One readdir per dir level (tree + sub).
    assert mock.readdir_calls == 2, (
        f"expected 2 readdirs, got {mock.readdir_calls}"
    )


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
