"""Service tests: PypsrpFileClient scripts, had_errors, and write_file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pypsrp.exceptions
import pytest

from _winrm_fakes import HOME, TEMP, FakePypsrpSession, _ErrorStreams

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry
from mcp_remote_control.fs.backends.winrm import PypsrpFileClient, WinrmFs
from mcp_remote_control.fs.types import FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _promote_script(sess: FakePypsrpSession) -> str:
    """The one standalone promote script of the last op (``rename``)."""
    promotes = [
        s
        for s in sess.ps_calls
        if "Move-Item -LiteralPath" in s and "WriteAllBytes" not in s
    ]
    assert len(promotes) == 1, sess.ps_calls
    return promotes[0]


# ---------------------------------------------------------------------------
# PypsrpFileClient production-path coverage: the real pypsrp adapter
# (stat / write_file / copy / fetch delegation / end-to-end WinrmFs
# THROUGH PypsrpFileClient) against FakePypsrpSession (the shared
# PS interpreter) without a Windows host.
# ---------------------------------------------------------------------------


def test_winrm_pypsrp_file_client_stat_file_dir_not_found() -> None:
    """PypsrpFileClient.stat (production PS-stat path) returns the
    expected dict for a file and a dir, and raises FileNotFoundError for a
    missing path (the PS-stat NOT_FOUND branch)."""
    sess = FakePypsrpSession()
    file_path = rf"{TEMP}\statme.txt"
    sess.files[file_path] = b"hello"
    dir_path = rf"{TEMP}\statdir"
    sess.dirs.add(dir_path)

    client = PypsrpFileClient(sess)

    # File: kind=file, size=len(data), mode=Archive, mtime present.
    st_file = client.stat(file_path)
    assert st_file["kind"] == "file"
    assert st_file["size"] == 5
    assert st_file["mode"] == "Archive"
    assert st_file["mtime"] == FakePypsrpSession.MTIME

    # Dir: kind=dir, size=0, mode=Directory.
    st_dir = client.stat(dir_path)
    assert st_dir["kind"] == "dir"
    assert st_dir["size"] == 0
    assert st_dir["mode"] == "Directory"

    # Missing: FileNotFoundError (the PS-stat NOT_FOUND branch in _run_json).
    with pytest.raises(FileNotFoundError):
        client.stat(rf"{TEMP}\nope.txt")

    # One Get-Item + PSIsContainer stat script fired per call.
    stat_scripts = [
        s
        for s in sess.ps_calls
        if "Get-Item -LiteralPath" in s and "PSIsContainer" in s
    ]
    assert len(stat_scripts) == 3


def test_winrm_pypsrp_file_client_write_file_round_trips() -> None:
    """PypsrpFileClient.write_file emits atomic temp + promote PS and
    the bytes land in the session FS at the exact path (no temp left)."""
    sess = FakePypsrpSession()
    client = PypsrpFileClient(sess)
    target = rf"{TEMP}\wrote.bin"
    payload = b"\x00\x01\x02 hello"

    client.write_file(target, payload)

    assert sess.files[target] == payload
    assert not any(".mrc-tmp-" in k for k in sess.files)
    assert sess.ps_calls, "execute_ps was called"
    script = sess.ps_calls[-1]
    assert "[IO.File]::WriteAllBytes(" in script
    assert "FromBase64String(" in script
    assert ".mrc-tmp-" in script
    assert "Move-Item -LiteralPath" in script
    assert "-Destination" in script


def test_winrm_pypsrp_file_client_copy_delegates_when_session_has_copy(
    tmp_path: Path,
) -> None:
    """When session.copy is present, PypsrpFileClient.copy delegates to
    it (the production pypsrp Client.copy streaming path) - no write_file PS
    script is emitted."""
    sess = FakePypsrpSession(has_copy=True)
    client = PypsrpFileClient(sess)
    src = tmp_path / "local.bin"
    src.write_bytes(b"copy-payload")
    remote = rf"{TEMP}\copied.bin"

    client.copy(str(src), remote)

    assert sess.copy_calls == [(str(src), remote)]
    assert sess.files[remote] == b"copy-payload"
    # No write_file PS script - native delegation, not the fallback.
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_copy_falls_back_to_write_file(
    tmp_path: Path,
) -> None:
    """When session.copy is absent, PypsrpFileClient.copy falls back to
    write_file (PS WriteAllBytes) so the bytes still land remotely."""
    sess = FakePypsrpSession()  # has_copy=False (default) -> no `copy` attribute
    client = PypsrpFileClient(sess)
    src = tmp_path / "local.bin"
    src.write_bytes(b"fallback-payload")
    remote = rf"{TEMP}\copied.bin"

    client.copy(str(src), remote)

    assert sess.copy_calls == [], "no native delegation without session.copy"
    assert sess.files[remote] == b"fallback-payload"
    assert any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_fetch_delegates_when_session_has_fetch(
    tmp_path: Path,
) -> None:
    """When session.fetch is present, PypsrpFileClient.fetch delegates
    to it (the production pypsrp Client.fetch streaming path) - no read_file PS
    script is emitted."""
    sess = FakePypsrpSession(has_fetch=True)
    client = PypsrpFileClient(sess)
    remote = rf"{TEMP}\remote.bin"
    sess.files[remote] = b"fetched-payload"
    dst = tmp_path / "downloaded.bin"

    client.fetch(remote, str(dst))

    assert sess.fetch_calls == [(remote, str(dst))]
    assert dst.read_bytes() == b"fetched-payload"
    # No read_file PS script - native delegation, not the fallback.
    assert not any(
        "ReadAllBytes" in s or "[IO.File]::Open(" in s for s in sess.ps_calls
    )


def test_winrm_pypsrp_file_client_fetch_falls_back_to_read_file(
    tmp_path: Path,
) -> None:
    """When session.fetch is absent, PypsrpFileClient.fetch falls back
    to read_file (PS ReadAllBytes) and writes the bytes locally."""
    sess = FakePypsrpSession()  # has_fetch=False (default) -> no `fetch` attribute
    client = PypsrpFileClient(sess)
    remote = rf"{TEMP}\remote.bin"
    sess.files[remote] = b"fallback-fetch"
    dst = tmp_path / "downloaded.bin"

    client.fetch(remote, str(dst))

    assert sess.fetch_calls == [], "no native delegation without session.fetch"
    assert dst.read_bytes() == b"fallback-fetch"
    assert any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_pypsrp_file_client_end_to_end_all_ops_roundtrip(
    tmp_path: Path,
) -> None:
    """End-to-end WinrmFs ops (list / stat / read / write / put / get /
    mkdir / rm) THROUGH PypsrpFileClient + FakePypsrpSession - exercises the
    production pypsrp adapter path for every op, not just isolated method tests.

    ``has_copy=True / has_fetch=True`` selects the production delegation path
    for put/get (pypsrp Client.copy / Client.fetch); the PS scripts back
    list / stat / read / write / mkdir / rm.
    """
    sess = FakePypsrpSession(has_copy=True, has_fetch=True)
    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    work = rf"{TEMP}\roundtrip"

    # mkdir (parents=True -> _mkdir_p walks the chain via stat + mkdir PS).
    r_mkdir = fs_ops.run(
        "mkdir", ep="lab-win", path=work, home=FIXTURES, backend=backend
    )
    assert r_mkdir.status == "ok"
    assert work in sess.dirs

    # write (PS WriteAllBytes after a stat-to-check-isdir + parent mkdir_p).
    note = rf"{work}\note.txt"
    r_write = fs_ops.run(
        "write",
        ep="lab-win",
        path=note,
        content="roundtrip-body\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r_write.status == "ok"
    assert sess.files[note] == b"roundtrip-body\n"

    # list (PS Get-Item + list_with_attrs in O(1) child round-trips).
    r_list = fs_ops.run(
        "list", ep="lab-win", path=work, home=FIXTURES, backend=backend
    )
    assert r_list.status == "ok"
    assert "note.txt" in (r_list.body or "")

    # stat (PS Get-Item single-shot).
    r_stat = fs_ops.run(
        "stat", ep="lab-win", path=note, home=FIXTURES, backend=backend
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "file"
    assert r_stat.fields.get("bytes") == len(b"roundtrip-body\n")

    # read (bounded read PS script -> base64 decode -> text body).
    r_read = fs_ops.run(
        "read", ep="lab-win", path=note, home=FIXTURES, backend=backend
    )
    assert r_read.status == "ok"
    assert "roundtrip-body" in (r_read.body or "")

    # put (PypsrpFileClient.copy -> session.copy delegation).
    local_src = tmp_path / "up.bin"
    local_src.write_bytes(b"\x00\x01\x02")
    remote_bin = rf"{work}\up.bin"
    r_put = fs_ops.run(
        "put",
        ep="lab-win",
        path=remote_bin,
        local=str(local_src),
        home=FIXTURES,
        backend=backend,
    )
    assert r_put.status == "ok"
    assert sess.copy_calls
    # Native put under script FS copies onto a same-dir temp, then promote.
    assert sess.copy_calls[-1][1] != remote_bin
    assert ".mrc-tmp-" in sess.copy_calls[-1][1]
    assert sess.files[remote_bin] == b"\x00\x01\x02"

    # get (PypsrpFileClient.fetch -> session.fetch delegation).
    local_dst = tmp_path / "down.bin"
    r_get = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote_bin,
        local=str(local_dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r_get.status == "ok"
    assert sess.fetch_calls and sess.fetch_calls[-1][0] == remote_bin
    assert local_dst.read_bytes() == b"\x00\x01\x02"

    # rm recursive (PS Remove-Item -Recurse drops the dir + all descendants).
    r_rm = fs_ops.run(
        "rm",
        ep="lab-win",
        path=work,
        recursive=True,
        home=FIXTURES,
        backend=backend,
    )
    assert r_rm.status == "ok"
    assert work not in sess.dirs
    assert note not in sess.files
    assert remote_bin not in sess.files


# ---------------------------------------------------------------------------
# Bounded read without Int32 Length cast; atomic get (temp + replace)
# ---------------------------------------------------------------------------


def test_winrm_read_file_bounded_script_no_int32_length() -> None:
    """Bounded read must not cast FileStream.Length to Int32.

    Files larger than 2 GiB make ``[int]$fs.Length`` throw in PowerShell before
    any bytes are returned. The script streams Read up to max_bytes and does
    not depend on Length.
    """
    sess = FakePypsrpSession()
    huge = rf"{TEMP}\huge.bin"
    sess.files[huge] = b"Z" * 4096

    client = PypsrpFileClient(sess)
    sess.ps_calls.clear()
    data = client.read_file(huge, max_bytes=10)
    assert data == b"Z" * 10
    assert len(sess.ps_calls) == 1
    script = sess.ps_calls[0]
    assert "[IO.File]::Open(" in script
    assert "$maxN = 10" in script
    assert "ReadAllBytes" not in script
    # No Int32 Length cast (the >2GB failure mode).
    assert "[int]$fs.Length" not in script
    assert "[int] $fs.Length" not in script
    # Bounded path must not depend on Length at all.
    assert "$fs.Length" not in script


def test_winrm_not_found_answer_keeps_its_dialect_per_script() -> None:
    """Each script answers a missing path in the dialect its decoder reads.

    ``read_file``'s scripts answer the bare token ``NOT_FOUND``, decoded by an
    equality check; ``stat`` / ``readlink`` / ``listdir`` / ``list_with_attrs``
    answer ``{"error":"NOT_FOUND"}``, decoded by a dict lookup. The two are not
    interchangeable: a dict answer fed to the base64 read surfaces FS_ERROR
    ("invalid base64 read") instead of FileNotFoundError. The fake session
    answers by op marker rather than by evaluating the emitted catch, so only a
    script-shape assertion like this one can see the swap.
    """
    sess = FakePypsrpSession()
    client = PypsrpFileClient(sess)

    for call in (
        lambda: client.stat(rf"{TEMP}\missing.bin"),
        lambda: client.readlink(rf"{TEMP}\missing.bin"),
        lambda: client.listdir(rf"{TEMP}\missing-dir"),
        lambda: client.list_with_attrs(rf"{TEMP}\missing-dir"),
    ):
        sess.ps_calls.clear()
        with pytest.raises(FileNotFoundError):
            call()
        script = sess.ps_calls[0]
        assert 'Write-Output \'{"error":"NOT_FOUND"}\'' in script
        assert 'Write-Output \'NOT_FOUND\'' not in script

    for call in (
        lambda: client.read_file(rf"{TEMP}\missing.bin"),
        lambda: client.read_file(rf"{TEMP}\missing.bin", max_bytes=8),
    ):
        sess.ps_calls.clear()
        with pytest.raises(FileNotFoundError):
            call()
        script = sess.ps_calls[0]
        assert 'Write-Output \'NOT_FOUND\'' in script
        assert '{"error":"NOT_FOUND"}' not in script


def test_winrm_read_file_bounded_works_for_large_payload_head() -> None:
    """WinrmFs.read with max_bytes returns the head and uses Length-free PS."""
    sess = FakePypsrpSession()
    target = rf"{TEMP}\big-head.bin"
    payload = b"ABCDEFGHIJ" * 1000  # 10_000 bytes
    sess.files[target] = payload
    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    r = fs_ops.run(
        "read",
        ep="lab-win",
        path=target,
        max_bytes=7,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert r.fields.get("bytes") == 7
    assert r.fields.get("truncated") is True
    read_scripts = [
        s for s in sess.ps_calls if "[IO.File]::Open(" in s or "ReadAllBytes" in s
    ]
    assert read_scripts
    assert "[int]$fs.Length" not in read_scripts[-1]
    assert "$fs.Length" not in read_scripts[-1]


# ---------------------------------------------------------------------------
# PypsrpFileClient must not swallow execute_ps had_errors / streams.error.
# pypsrp returns (output, streams, had_errors); silent success on remote
# write/delete/read failure is a false-success bug.
# ---------------------------------------------------------------------------


class _HadErrorsSession:
    """Session whose execute_ps always returns pypsrp (out, streams, True)."""

    def __init__(self, stderr: str = "Access is denied") -> None:
        self.stderr = stderr
        self.ps_calls: list[str] = []

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, _ErrorStreams, bool]:
        del environment
        self.ps_calls.append(script)
        return ("", _ErrorStreams([self.stderr]), True)


class _OkTupleSession:
    """Session that returns pypsrp success tuple (out, streams, False)."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.ps_calls: list[str] = []

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, _ErrorStreams, bool]:
        del environment
        self.ps_calls.append(script)
        return (self.stdout, _ErrorStreams([]), False)


def test_pypsrp_execute_ps_had_errors_write_file_raises() -> None:
    """write_file must raise FsError with remote stderr when had_errors."""
    sess = _HadErrorsSession(stderr="Access is denied")
    client = PypsrpFileClient(sess)
    with pytest.raises(FsError) as excinfo:
        client.write_file(rf"{TEMP}\x.bin", b"data")
    err = excinfo.value
    assert err.code == "FS_ERROR"
    assert "Access is denied" in err.msg
    assert "Access is denied" in (err.details.get("stderr") or "")


def test_pypsrp_execute_ps_had_errors_mkdir_remove_rmdir_rmtree() -> None:
    """mkdir / remove / rmdir / rmtree all surface had_errors + stderr."""
    remote_err = "Cannot remove item: being used by another process"
    sess = _HadErrorsSession(stderr=remote_err)
    client = PypsrpFileClient(sess)
    path = rf"{TEMP}\locked"

    for op in (
        lambda: client.mkdir(path),
        lambda: client.remove(path),
        lambda: client.rmdir(path),
        lambda: client.rmtree(path),
    ):
        with pytest.raises(FsError) as excinfo:
            op()
        err = excinfo.value
        assert err.code == "FS_ERROR"
        assert remote_err in err.msg
        assert remote_err in (err.details.get("stderr") or "")


def test_pypsrp_execute_ps_had_errors_read_and_copy_fallback(
    tmp_path: Path,
) -> None:
    """read_file and copy/fetch PS fallbacks also fail on had_errors."""
    remote_err = "The system cannot find the path specified"
    sess = _HadErrorsSession(stderr=remote_err)
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.read_file(rf"{TEMP}\missing.bin")
    assert remote_err in excinfo.value.msg

    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    # No session.copy -> write_file PS fallback -> had_errors.
    with pytest.raises(FsError) as excinfo:
        client.copy(str(src), rf"{TEMP}\out.bin")
    assert remote_err in excinfo.value.msg

    # No session.fetch -> read_file PS fallback -> had_errors.
    with pytest.raises(FsError) as excinfo:
        client.fetch(rf"{TEMP}\remote.bin", str(tmp_path / "got.bin"))
    assert remote_err in excinfo.value.msg


def test_pypsrp_execute_ps_ok_tuple_stdout_unchanged() -> None:
    """Success path (had_errors=False) still returns stdout for consumers."""
    # Unbounded read: last non-empty line of stdout is the base64 payload.
    # "hello-ok" -> aGVsbG8tb2s=
    payload = b"hello-ok"
    sess = _OkTupleSession(stdout="aGVsbG8tb2s=")
    client = PypsrpFileClient(sess)
    data = client.read_file(rf"{TEMP}\ok.bin")
    assert data == payload
    assert sess.ps_calls, "execute_ps was called"
    assert "[IO.File]::ReadAllBytes(" in sess.ps_calls[-1]


def test_pypsrp_execute_ps_ok_tuple_write_does_not_raise() -> None:
    """had_errors=False write_file completes (no false failure)."""
    sess = _OkTupleSession(stdout="")
    client = PypsrpFileClient(sess)
    client.write_file(rf"{TEMP}\ok.bin", b"x")
    assert sess.ps_calls
    assert "WriteAllBytes" in sess.ps_calls[-1]
    assert "Move-Item -LiteralPath" in sess.ps_calls[-1]


# ---------------------------------------------------------------------------
# Native put promote: the verified temp is renamed remotely, not re-uploaded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_progress", [False, True])
def test_winrm_put_promotes_remotely_without_content_round_trip(
    tmp_path: Path, with_progress: bool
) -> None:
    """A native put promotes the temp with a remote rename, not a re-upload.

    The upload lands on a same-directory temp and is size-verified, so the
    promote only has to replace the destination with a file that is already
    there: one remote promote round trip. Emitting the promote as
    ``read_file`` + ``write_file`` instead moves the whole payload off the host
    and back (two full Base64 round trips); no script of this put may carry any
    part of the payload. Both the plain and the progress-callback put take the
    same native-copy + remote-promote route.
    """
    payload = bytes(range(256)) * 256  # 65_536 bytes
    sess = FakePypsrpSession(has_copy=True, has_fetch=True)
    dest = rf"{TEMP}\promote-put.bin"
    sess.files[dest] = b"prior-dest-bytes"
    backend = WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)
    seen: list[int] = []

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
        progress=(lambda done, _total: seen.append(done)) if with_progress else None,
    )

    assert r.status == "ok"
    assert r.fields.get("bytes") == len(payload)
    # Exactly one native copy, and it targets a sibling temp.
    assert len(sess.copy_calls) == 1
    assert sess.copy_calls[0][0] == str(src)
    tmp = sess.copy_calls[0][1]
    assert tmp.startswith(rf"{TEMP}\.")
    assert ".mrc-tmp-" in tmp
    # The promote is one remote rename of that temp onto the destination.
    promotes = [
        s
        for s in sess.ps_calls
        if "Move-Item -LiteralPath" in s and "WriteAllBytes" not in s
    ]
    assert len(promotes) == 1, sess.ps_calls
    assert f"Move-Item -LiteralPath '{tmp}'" in promotes[0]
    assert f"-Destination '{dest}'" in promotes[0]
    # No script carries the payload out of the temp and back in.
    assert not any(
        "ReadAllBytes" in s
        or "WriteAllBytes" in s
        or "ToBase64String" in s
        or "FromBase64String" in s
        for s in sess.ps_calls
    ), [s for s in sess.ps_calls if len(s) > 200]
    assert max(len(s) for s in sess.ps_calls) < 4096, (
        "no script may carry the payload: the base64 form of the payload is "
        "more than 20x this bound"
    )
    # Result and cleanup unchanged: new bytes at dest, no temp left.
    assert sess.files[dest] == payload
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    if with_progress:
        assert seen and seen[-1] == len(payload)


def test_pypsrp_rename_resolves_reparse_dest_to_referent() -> None:
    """``rename`` onto a reparse/symlink updates the referent, keeps the link.

    A promote onto the reparse entry itself would touch the link inode rather
    than its referent, so the promote resolves the final component first (same
    policy as ``write_file``).
    """
    sess = FakePypsrpSession()
    link = rf"{TEMP}\promote-link.bin"
    target = rf"{TEMP}\promote-target.bin"
    tmp = rf"{TEMP}\.promote-link.bin.mrc-tmp-abc"
    sess.files[tmp] = b"new-referent"
    sess.files[target] = b"old-referent"
    sess.links[link] = target
    client = PypsrpFileClient(sess)

    client.rename(tmp, link)

    assert sess.files[target] == b"new-referent"
    assert sess.links[link] == target
    assert link not in sess.files
    assert tmp not in sess.files


# ---------------------------------------------------------------------------
# Promote: an existing destination goes to the runtime replace primitive, a new
# one to an unforced move, and a failed promote keeps the prior target
# ---------------------------------------------------------------------------


def test_winrm_promote_existing_target_uses_replace_primitive() -> None:
    """An existing destination is replaced by ``[IO.File]::Replace``.

    ``Move-Item -Force`` two-steps an overwrite (the provider deletes the
    destination and then moves the source in), so the promote uses the
    runtime's replace primitive with a same-directory backup name instead, and
    the create branch of the same script carries an unforced move. The
    destination keeps its prior identity, so the content swap is all that is
    asserted here; the failure semantics are covered below.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\replace-shape.bin"
    tmp = rf"{TEMP}\.replace-shape.bin.mrc-tmp-abc"
    sess.files[dest] = b"old-content"
    sess.files[tmp] = b"new-content"
    client = PypsrpFileClient(sess)

    client.rename(tmp, dest)

    assert sess.files[dest] == b"new-content"
    assert tmp not in sess.files
    assert [k for k in sess.files if ".mrc-bak-" in k] == []
    script = _promote_script(sess)
    m = re.search(
        r"\[IO\.File\]::Replace\('([^']*)', '([^']*)', '([^']*)'\)", script
    )
    assert m is not None, script
    assert m.group(1) == tmp
    assert m.group(2) == dest
    backup = m.group(3)
    assert backup != tmp
    assert backup.startswith(rf"{TEMP}\."), backup
    # The create branch of the same script must not force-overwrite.
    assert f"Move-Item -LiteralPath '{tmp}' -Destination '{dest}'" in script
    assert f"Move-Item -LiteralPath '{tmp}' -Destination '{dest}' -Force" not in script


def test_winrm_promote_new_target_is_created_by_unforced_move() -> None:
    """A destination that does not exist is created by an unforced move.

    The script still carries the replace branch (the branching decision is the
    host's), but nothing exists to park, so the move creates the destination.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\create-shape.bin"
    tmp = rf"{TEMP}\.create-shape.bin.mrc-tmp-abc"
    sess.files[tmp] = b"fresh-content"
    client = PypsrpFileClient(sess)

    client.rename(tmp, dest)

    assert sess.files[dest] == b"fresh-content"
    assert tmp not in sess.files
    assert [k for k in sess.files if ".mrc-bak-" in k] == []
    script = _promote_script(sess)
    assert f"Move-Item -LiteralPath '{tmp}' -Destination '{dest}'" in script
    assert f"Move-Item -LiteralPath '{tmp}' -Destination '{dest}' -Force" not in script


def test_winrm_put_promote_failure_after_target_parked_keeps_prior_target(
    tmp_path: Path,
) -> None:
    """A promote that fails after the prior target was parked still keeps it.

    The fake models the emitted command's phases: an existing destination goes
    to the replace primitive, which parks the replaced content at the backup
    name before the replacement takes the destination name. The injected
    failure fires at that point - where a two-step ``Move-Item -Force`` would
    already have deleted the destination - so the prior bytes surviving at
    *dest* can only come from the promote script's own restore step.
    """
    sess = FakePypsrpSession(has_copy=True)
    dest = rf"{TEMP}\kept-through-replace.bin"
    old = b"prior-good-copy"
    sess.files[dest] = old
    sess.fail_write_at = "promote"
    backend = WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"new-payload")

    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), dest)

    assert excinfo.value.code == "FS_ERROR"
    assert "promote" in excinfo.value.msg
    assert sess.promote_removed_target, (
        "the failure must have fired after the destination entry was parked, "
        "not before the promote started"
    )
    assert sess.files[dest] == old
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    assert [k for k in sess.files if ".mrc-bak-" in k] == []
    # The promote really was the replace of the uploaded temp, and the native
    # copy left exactly one temp for it to consume.
    assert "[IO.File]::Replace(" in _promote_script(sess)
    assert sess.copy_calls and ".mrc-tmp-" in sess.copy_calls[-1][1]


def test_winrm_write_file_failure_after_target_parked_keeps_prior_target() -> None:
    """The whole-file write path keeps the prior target across the same failure.

    ``write_file`` promotes its temp with the same branch, so a failure after
    the prior target was parked leaves the prior bytes at the destination there
    too, with no temp and no backup left behind.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\write-kept.bin"
    old = b"old-content-must-survive"
    sess.files[dest] = old
    sess.fail_write_at = "promote"
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(dest, b"new-payload-must-not-land")

    assert excinfo.value.code == "FS_ERROR"
    assert "promote" in excinfo.value.msg
    assert sess.promote_removed_target
    assert sess.files[dest] == old
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    assert [k for k in sess.files if ".mrc-bak-" in k] == []


def test_winrm_promote_refuses_file_that_appeared_after_the_probe(
    tmp_path: Path,
) -> None:
    """A file that appears at a new destination is refused, not overwritten.

    The branch is decided on the host, so a destination created between the
    probe and the move must fail the unforced move (the real cmdlet refuses an
    existing target) instead of being replaced by it. The hook fires at exactly
    that point, so the interleaving is pinned instead of raced.
    """
    sess = FakePypsrpSession(has_copy=True)
    dest = rf"{TEMP}\raced-target.bin"
    raced = b"appeared-after-the-probe"
    sess.before_promote_move = lambda: sess.files.__setitem__(dest, raced)
    backend = WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-replace")

    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), dest)

    assert excinfo.value.code == "FS_ERROR"
    assert sess.files[dest] == raced
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    assert [k for k in sess.files if ".mrc-bak-" in k] == []


def test_winrm_promote_refuses_directory_that_appeared_after_the_probe(
    tmp_path: Path,
) -> None:
    """A directory at the destination is refused, not entered by the move.

    A directory destination is a container for ``Move-Item``: the temp would
    land inside it under its own name and the put would report success while
    the requested path was never created. The emitted script probes for that
    case itself, so the directory the probe did not see as a file is refused
    and the catch cleans the temp. The hook fires after the emitted probe, so
    the interleaving is pinned instead of raced; the gap that remains between
    the container probe and the move is not pinned here.
    """
    sess = FakePypsrpSession(has_copy=True)
    dest = rf"{TEMP}\raced-dir"
    sess.before_promote_move = lambda: sess.dirs.add(dest)
    backend = WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-must-not-land")

    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), dest)

    assert excinfo.value.code == "FS_ERROR"
    assert "dir-refused" in excinfo.value.msg
    assert dest in sess.dirs
    assert not any(k.startswith(dest + "\\") for k in sess.files), (
        "the payload must not land inside the directory"
    )
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    assert "-PathType Container" in _promote_script(sess)


def test_winrm_write_failure_before_the_promote_keeps_a_stale_backup_parked() -> None:
    """A failure before the promote must not move a stale backup to the target.

    The restore step of the emitted catch is guarded by ``$parked``, which the
    replace branch sets and nothing else does. A whole-file write that fails
    before the promote therefore leaves an absent destination absent even when
    the backup name from an earlier interrupted replace is still on the host:
    an unguarded restore would put that older generation at a destination this
    run never touched and report the failure while the destination held
    content from neither this write nor its own history.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\stale-restore.bin"
    # The backup name this destination's promote uses, read off one emitted
    # script: it is derived from the temp name, which is derived from the
    # destination plus the writing process and thread, so it is stable here.
    warm = FakePypsrpSession()
    PypsrpFileClient(warm).write_file(dest, b"first")
    m = re.search(
        r"\[IO\.File\]::Replace\('[^']*', '[^']*', '([^']*)'\)", warm.ps_calls[-1]
    )
    assert m is not None, warm.ps_calls[-1]
    backup = m.group(1)
    sess.files[backup] = b"ancient-generation"
    sess.fail_write_at = "write"
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(dest, b"new-payload")

    assert excinfo.value.code == "FS_ERROR"
    assert dest not in sess.files, (
        "a failure before the promote restored a stale backup onto the target"
    )
    assert sess.files[backup] == b"ancient-generation", (
        "the recovery copy belongs where it was left"
    )
    script = next(s for s in sess.ps_calls if "WriteAllBytes" in s)
    assert "if ($parked -and (-not (Test-Path -LiteralPath" in script


# ---------------------------------------------------------------------------
# WinRM write_file atomicity (same-dir temp + promote; no half target)
# ---------------------------------------------------------------------------


def test_winrm_write_file_atomic_success_replaces_existing() -> None:
    """Successful atomic write replaces dest; no .mrc-tmp-* leftovers."""
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\atom-write.bin"
    sess.files[dest] = b"old-content"
    client = PypsrpFileClient(sess)

    client.write_file(dest, b"new-content")

    assert sess.files[dest] == b"new-content"
    assert not any(".mrc-tmp-" in k for k in sess.files)
    script = sess.ps_calls[-1]
    # Write lands on temp path first, then Move-Item promotes to dest.
    assert ".mrc-tmp-" in script
    assert "Move-Item -LiteralPath" in script
    assert "try {" in script
    assert "catch {" in script
    assert "Remove-Item -LiteralPath" in script


def test_winrm_write_file_atomic_failure_preserves_dest() -> None:
    """A promote failure after a fully written temp must not touch dest.

    The fake reports the script's own control flow: ``WriteAllBytes`` creates
    the temp, the promote then fails, and the ``catch`` block removes whatever
    path it names (parsed from the emitted script). So the two assertions
    below have content - a temp really existed, and only the script's cleanup
    can account for its absence.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\keep.bin"
    old = b"original-content-must-survive"
    sess.files[dest] = old
    sess.fail_write_at = "promote"
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(dest, b"new-payload-must-not-land")
    err = excinfo.value
    assert err.code == "FS_ERROR"
    assert "promote" in err.msg
    assert sess.files[dest] == old
    # The temp was created (the failure stage says so) and is gone: only the
    # script's catch/Test-Path cleanup can have removed it.
    assert not any(".mrc-tmp-" in k for k in sess.files)
    assert "remote write failed at promote" in err.msg
    script = sess.ps_calls[-1]
    assert "[IO.File]::WriteAllBytes(" in script
    assert ".mrc-tmp-" in script
    assert "Move-Item -LiteralPath" in script
    # Failure path must clean temp on the remote (catch + Test-Path + remove).
    assert "Remove-Item -LiteralPath" in script
    assert "Test-Path -LiteralPath" in script


def test_winrm_write_file_atomic_write_failure_never_creates_temp() -> None:
    """A ``WriteAllBytes`` failure leaves no temp and no dest change.

    Same interpreter, earlier stage: nothing was written at all, so the catch
    block's ``Test-Path`` finds nothing and dest keeps its prior bytes.
    """
    sess = FakePypsrpSession()
    dest = rf"{TEMP}\keep2.bin"
    sess.files[dest] = b"original"
    sess.fail_write_at = "write"
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(dest, b"new-must-not-land")
    assert "remote write failed at write" in excinfo.value.msg
    assert sess.files[dest] == b"original"
    assert not any(".mrc-tmp-" in k for k in sess.files)


def test_winrm_put_promote_failure_keeps_prior_dest(tmp_path: Path) -> None:
    """A promote failure after the native copy must keep the prior destination.

    Native copy writes a same-dir temp and promotes it with the remote rename;
    the rename fails there (dest locked / read-only on a real host), so the
    backend must surface the error and leave neither a replaced destination
    nor an orphan temp behind. The temp really existed (the native copy wrote
    it) and is gone afterwards: only the promote script's own catch / the
    backend cleanup can account for that.
    """
    sess = FakePypsrpSession(has_copy=True, has_fetch=True)
    dest = rf"{TEMP}\kept.bin"
    sess.files[dest] = b"prior-good-copy"
    sess.fail_write_at = "promote"
    backend = WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"new-payload")

    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), dest)

    # The refused promote reaches the caller instead of a silent success.
    assert excinfo.value.code == "FS_ERROR"
    assert "promote" in excinfo.value.msg
    assert sess.files[dest] == b"prior-good-copy"
    assert [k for k in sess.files if ".mrc-tmp-" in k] == []
    # The failed step was the remote rename of the uploaded temp, and the temp
    # really existed before it (the native copy wrote it).
    assert sess.copy_calls and ".mrc-tmp-" in sess.copy_calls[-1][1]
    assert any(
        "Move-Item -LiteralPath" in s and "WriteAllBytes" not in s
        for s in sess.ps_calls
    )


def test_winrm_put_promote_holds_lock_and_respects_op_budget(tmp_path: Path) -> None:
    """The remote promote shares the serial lock and the whole-op budget.

    The promote is one more session call on the shared pypsrp wsman object, so
    it runs inside the same serial-ops scope as the rest of the put (a long
    exec / ps holding the lock must not interleave with the rename), and it
    must not start once the whole-op deadline is spent.
    """
    import time

    from test_fs_winrm_lock import _RecordingSerialOps

    ops = _RecordingSerialOps()

    class _DepthProbeSession(FakePypsrpSession):
        """Record the serial-ops depth observed by every session call."""

        def __init__(self) -> None:
            super().__init__(has_copy=True)
            self.observed_depth: list[int] = []

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> Any:
            self.observed_depth.append(ops.depth)
            return super().execute_ps(script, environment=environment)

    sess = _DepthProbeSession()
    dest = rf"{TEMP}\lock-promote.bin"
    client = PypsrpFileClient(sess, serial_ops=ops)
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload")

    backend.put(str(src), dest)

    assert sess.files[dest] == b"payload"
    assert sess.observed_depth, "no session call was recorded"
    assert min(sess.observed_depth) >= 1, "every session call must hold the lock"
    assert ops.depth == 0, "the scope leaked past the put"
    assert any(
        "Move-Item -LiteralPath" in s and "WriteAllBytes" not in s
        for s in sess.ps_calls
    ), "the promote ran as a session call under the same lock"

    # Whole-op budget already spent: the promote must not start a session call.
    client._op_timeout_s = 0.5  # noqa: SLF001
    client._op_deadline = time.monotonic() - 0.5  # noqa: SLF001
    calls_before = len(sess.ps_calls)
    with pytest.raises(FsError) as excinfo:
        client.rename(rf"{TEMP}\.x.bin.mrc-tmp-1", rf"{TEMP}\x.bin")

    assert excinfo.value.code == "TIMEOUT"
    assert excinfo.value.details.get("op_timeout_s") == 0.5
    assert len(sess.ps_calls) == calls_before, (
        "no session call may start after the whole-op budget is spent"
    )


def test_winrm_write_file_atomic_temp_path_is_sibling() -> None:
    """Temp path is in the same directory as dest (same-volume Move-Item)."""
    from mcp_remote_control.fs.backends.winrm import _win_temp_path

    dest = rf"{TEMP}\nested\out.bin"
    tmp = _win_temp_path(dest)
    assert tmp.startswith(rf"{TEMP}\nested\.")
    assert ".out.bin.mrc-tmp-" in tmp
    assert tmp != dest


def test_winrm_write_file_atomic_copy_fallback_uses_temp_promote(
    tmp_path: Path,
) -> None:
    """copy without session.copy goes through atomic write_file."""
    sess = FakePypsrpSession()  # has_copy=False
    client = PypsrpFileClient(sess)
    src = tmp_path / "local.bin"
    src.write_bytes(b"via-copy-fallback")
    remote = rf"{TEMP}\from-copy.bin"
    sess.files[remote] = b"prior"

    client.copy(str(src), remote)

    assert sess.files[remote] == b"via-copy-fallback"
    assert not any(".mrc-tmp-" in k for k in sess.files)
    write_scripts = [s for s in sess.ps_calls if "WriteAllBytes" in s]
    assert write_scripts
    assert "Move-Item -LiteralPath" in write_scripts[-1]


def test_pypsrp_resolve_final_link_stat_timeout_does_not_replace() -> None:
    """Stat TIMEOUT during resolve must not Move-Item onto the original path."""
    dest = rf"{TEMP}\link.bin"
    old = b"must-keep-link-body"

    class _TimeoutStat(PypsrpFileClient):
        def stat(self, path: str) -> object:
            if path.replace("/", "\\") == dest:
                raise FsError(
                    "TIMEOUT",
                    "winrm fs stat timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().stat(path)

    sess = FakePypsrpSession()
    sess.files[dest] = old
    client = _TimeoutStat(sess)
    with pytest.raises(FsError) as ei:
        client.write_file(dest, b"new-must-not-land")
    assert ei.value.code == "TIMEOUT"
    assert sess.files[dest] == old
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)
    assert not any("Move-Item" in s for s in sess.ps_calls)


def test_pypsrp_resolve_final_link_readlink_timeout_does_not_replace() -> None:
    """Dest is a reparse/symlink; readlink TIMEOUT keeps the link entry."""
    dest = rf"{TEMP}\link.bin"
    target = rf"{TEMP}\target.bin"

    class _TimeoutReadlink(PypsrpFileClient):
        def stat(self, path: str) -> object:
            if path.replace("/", "\\") == dest:
                return {
                    "kind": "link",
                    "size": 0,
                    "mode": "ReparsePoint",
                    "target": target,
                }
            return super().stat(path)

        def readlink(self, path: str) -> str:
            if path.replace("/", "\\") == dest:
                raise FsError(
                    "TIMEOUT",
                    "winrm fs readlink timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            return super().readlink(path)

    sess = FakePypsrpSession()
    sess.files[target] = b"old-referent"
    client = _TimeoutReadlink(sess)
    with pytest.raises(FsError) as ei:
        client.write_file(dest, b"new-must-not-land")
    assert ei.value.code == "TIMEOUT"
    assert dest not in sess.files
    assert sess.files[target] == b"old-referent"
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)
    assert not any("Move-Item" in s for s in sess.ps_calls)

    resolved = None
    with pytest.raises(FsError) as ei2:
        resolved = client._resolve_final_link(dest)
    assert ei2.value.code == "TIMEOUT"
    assert resolved is None


# ---------------------------------------------------------------------------
# Directory destination and reparse resolution at the adapter level
# ---------------------------------------------------------------------------


def test_pypsrp_write_file_directory_destination_rejected() -> None:
    """``write_file`` onto a directory raises IS_A_DIR before any script.

    A move onto a directory destination is a container move: the temp would
    land inside the directory and the write would report success. The adapter
    refuses the destination instead of emitting that script, and the promote's
    own container branch is the backstop for when it appears later.
    """
    sess = FakePypsrpSession()
    drop = rf"{TEMP}\drop"
    sess.dirs.add(drop)
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(drop, b"payload-must-not-land")

    assert excinfo.value.code == "IS_A_DIR"
    assert drop in excinfo.value.msg
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)
    assert sess.files == {}


def test_pypsrp_write_file_resolves_reparse_chain_to_referent() -> None:
    """The production resolve lands the promote on the referent, not the link.

    The fake session stores a ``Move-Item`` literally (it does not follow
    links), so only the adapter's own resolve can account for the referent
    receiving the bytes while the link entry survives.
    """
    sess = FakePypsrpSession()
    link = rf"{TEMP}\link.bin"
    target = rf"{TEMP}\target.bin"
    sess.files[target] = b"old-referent"
    sess.links[link] = target
    client = PypsrpFileClient(sess)

    client.write_file(link, b"new-referent")

    assert sess.files[target] == b"new-referent"
    assert link not in sess.files
    assert sess.links[link] == target


def test_pypsrp_write_file_dir_reparse_unreadable_target_rejected() -> None:
    """A directory reparse point with an unreadable target is IS_A_DIR.

    Windows reports a junction / symlink-to-directory as ``Directory,
    ReparsePoint``. When ``Target``/``LinkTarget`` comes back empty the
    resolve has nothing to follow, and promoting onto the reparse entry
    itself would hit ``Move-Item``'s container rule - the temp lands inside
    the referent and the write reports success while *path* is never created
    (the fake now models the container for directory reparse points, so the
    stray temp is visible if the guard is gone). The entry's own attributes
    are the only signal left, and they decide the verdict.
    """
    sess = FakePypsrpSession()
    link = rf"{TEMP}\dirlink"
    real = rf"{TEMP}\realdir"
    sess.dirs.add(real)
    sess.links[link] = ""  # production readlink -> OSError("empty reparse target")
    sess.dir_links.add(link)
    client = PypsrpFileClient(sess)

    with pytest.raises(FsError) as excinfo:
        client.write_file(link, b"payload-must-not-land")

    assert excinfo.value.code == "IS_A_DIR"
    assert link in excinfo.value.msg
    assert sess.files == {}, "nothing may land in the referent or as a temp"
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)
    assert not any("Move-Item" in s for s in sess.ps_calls)


def test_pypsrp_write_file_file_reparse_unreadable_target_still_promotes() -> None:
    """Control: a *file* reparse point with an unreadable target is not a
    container, so the promote-onto-the-entry fallback is unchanged."""
    sess = FakePypsrpSession()
    link = rf"{TEMP}\filelink.bin"
    sess.links[link] = ""  # not a directory reparse point
    client = PypsrpFileClient(sess)

    client.write_file(link, b"payload")

    assert sess.files[link] == b"payload"
    assert sess.links[link] == ""


def test_pypsrp_resolve_final_link_link_failure_propagates() -> None:
    """A link failure during the resolve probe fails the write.

    The resolve's probe failures fall back to the original path so minimal
    clients still reach the promote - but a *link* failure is not an unknown
    target: the transport has already retired the session, so promoting onto
    the unresolved path would report success on a dead endpoint (a one-shot
    rejection at this exact round trip used to return ``status=ok`` while
    the transport was marked dead).
    """
    store: dict[str, object] = {
        "files": {},
        "dirs": {"C:\\", TEMP, HOME},
    }
    sess = _LinkFailingSession(store=store, fail_on="PSIsContainer")
    client = PypsrpFileClient(sess)

    with pytest.raises(pypsrp.exceptions.WinRMTransportError):
        client.write_file(rf"{TEMP}\out.bin", b"payload")

    assert not any("WriteAllBytes" in s for s in sess.ps_calls), (
        "the write must not be attempted after its link was reported dead"
    )
    assert sess.files == {}


def test_pypsrp_resolve_final_link_readlink_link_failure_propagates() -> None:
    """A link failure while *reading* the target is not "no target".

    The readlink probe's other failures (``not a reparse point``, empty
    target) mean "cannot resolve - keep the old promote behavior", but a link
    failure means the session is gone: promoting onto the unresolved entry
    would clobber the link entry and report success on a dead endpoint.
    """
    link = rf"{TEMP}\link.bin"
    store: dict[str, object] = {
        "files": {},
        "dirs": {"C:\\", TEMP, HOME},
        "links": {link: rf"{TEMP}\target.bin"},
    }
    sess = _LinkFailingSession(store=store, fail_on="NOT_A_LINK")
    client = PypsrpFileClient(sess)

    with pytest.raises(pypsrp.exceptions.WinRMTransportError):
        client.write_file(link, b"payload")

    assert not any("WriteAllBytes" in s for s in sess.ps_calls)
    assert sess.files == {}
    assert sess.links[link] == rf"{TEMP}\target.bin"


# ---------------------------------------------------------------------------
# Real fs composition: fs_ops -> registry -> WinRMTransport -> PypsrpFileClient
# -> transport link callback.
# ---------------------------------------------------------------------------


class _LinkFailingSession(FakePypsrpSession):
    """Pypsrp-shaped session that fails one round trip with a link rejection.

    *fail_on* names a fragment of the script that triggers the failure (e.g.
    ``"PSIsContainer"`` for the first stat, ``"WriteAllBytes"`` for the write
    script, ``"Move-Item"`` for the remote promote of a native put); the
    failure fires once, so the retry after the endpoint reconnects can
    succeed. *fail_at_match* selects which matching round trip fails (1 = the
    first), which is how a failure can be aimed at a later probe - e.g. the
    resolve stat inside ``PypsrpFileClient.write_file``. State is shared
    across sessions via *store* so a reconnected session sees the same remote
    filesystem.
    """

    def __init__(
        self,
        *,
        store: dict[str, object],
        fail_on: str | None,
        has_copy: bool = False,
        fail_at_match: int = 1,
    ) -> None:
        super().__init__(has_copy=has_copy, has_fetch=has_copy)
        self.cwd = HOME
        self.home = HOME
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.files = store["files"]  # type: ignore[assignment]
        self.dirs = store["dirs"]  # type: ignore[assignment]
        if store.get("links") is not None:
            self.links = store["links"]  # type: ignore[assignment]
        self._fail_on = fail_on
        self._fail_at_match = fail_at_match
        self.matches = 0

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        if self._fail_on is not None and self._fail_on in script:
            self.matches += 1
            if self.matches == self._fail_at_match:
                self._fail_on = None
                self.ps_calls.append(script)
                raise pypsrp.exceptions.WinRMTransportError("http", 400, "")
        return super().execute_ps(script, environment=environment)


@pytest.mark.parametrize(
    "has_copy, fail_on",
    [
        (False, "PSIsContainer"),  # parent-dir stat inside mkdir_p
        (False, "WriteAllBytes"),  # the write script itself, no native copy
        (True, "PSIsContainer"),  # native-copy put: first stat, same shape
        (True, "Move-Item"),  # native-copy put: the remote promote
    ],
)
def test_fs_composition_link_failure_reports_and_reconnects(
    tmp_path: Path,
    has_copy: bool,
    fail_on: str,
) -> None:
    """A link-class failure through the real fs composition is honest.

    The whole production route runs: ``fs_ops.run`` -> registry ->
    ``WinRMTransport.open_fs`` -> ``PypsrpFileClient`` -> the transport's link
    callback. The row must be an error (never ``ok`` with the endpoint dead),
    the prior destination must survive without an orphan temp, the endpoint
    must be marked dead with the machine-readable tokens, and the next call
    must reconnect on a fresh session and land the payload.
    """
    dest = rf"{TEMP}\work\out.bin"
    work = rf"{TEMP}\work"
    store: dict[str, object] = {
        "files": {dest: b"old-bytes"},
        "dirs": {"C:\\", TEMP, HOME, work},
    }
    sessions: list[_LinkFailingSession] = []
    src = tmp_path / "payload.bin"
    src.write_bytes(b"new-payload")

    def connector(**_kwargs: object) -> _LinkFailingSession:
        # Only the first generation is poisoned; the reconnect is healthy.
        sess = _LinkFailingSession(
            store=store,
            fail_on=fail_on if not sessions else None,
            has_copy=has_copy,
        )
        sessions.append(sess)
        return sess

    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        connector=connector,
    )

    assert r.status == "error", (r.code, r.fields)
    assert r.code == "FS_ERROR", (r.code, r.fields)
    # The PypsrpFileClient adapter really ran (its PS scripts reached the
    # session), so this is the production route and not a mocked-open_fs path.
    assert any(
        "PSIsContainer" in s or "WriteAllBytes" in s for s in sessions[0].ps_calls
    )
    files = store["files"]
    assert isinstance(files, dict)
    assert files.get(dest) == b"old-bytes", "prior destination must survive"
    assert [k for k in files if ".mrc-tmp-" in k] == [], "no orphan temp"

    ep = reg.get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.is_connected() is False
    meta = ep.transport.meta
    assert meta.get("marked_dead") is True
    assert meta.get("link_lost") is True
    assert meta.get("reopen_hint") == "endpoint close then open"

    r2 = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok", (r2.code, r2.fields)
    assert len(sessions) == 2, "the dead endpoint must rebuild its session"
    assert any(".mrc-tmp-" in s and "Move-Item" in s for s in sessions[1].ps_calls)
    assert files.get(dest) == b"new-payload"


@pytest.mark.parametrize("fail_at_match", [1, 2, 3, 4])
def test_fs_composition_link_failure_at_each_resolve_stat_is_error(
    tmp_path: Path,
    fail_at_match: int,
) -> None:
    """A link rejection at *any* stat of a put is never a success row.

    A put issues four stat round trips: (1) the parent probe inside
    ``_mkdir_p``, (2) the put-level ``_resolve_final_link``, (3)
    ``_write_bytes``' resolve, (4) ``PypsrpFileClient.write_file``'s own
    resolve. (2) and (3) reach ``WinrmFs``, which propagates a link failure;
    (4) used to swallow it into the promote-onto-the-original-path fallback -
    the write reported ``ok`` while the transport had already retired the
    session, and the row carried no link token at all. A link failure is not
    "unknown target": every one of them must fail the op, keep the prior
    destination, and leave the machine-readable link tokens on the row.
    """
    dest = rf"{TEMP}\work\out.bin"
    work = rf"{TEMP}\work"
    store: dict[str, object] = {
        "files": {dest: b"old-bytes"},
        "dirs": {"C:\\", TEMP, HOME, work},
    }
    sessions: list[_LinkFailingSession] = []
    src = tmp_path / "payload.bin"
    src.write_bytes(b"new-payload")

    def connector(**_kwargs: object) -> _LinkFailingSession:
        sess = _LinkFailingSession(
            store=store,
            fail_on="PSIsContainer",
            fail_at_match=fail_at_match,
        )
        sessions.append(sess)
        return sess

    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        connector=connector,
    )

    # The rejection really fired at the intended stat: if the round-trip
    # sequence changed, this fails instead of testing nothing.
    assert sessions[0].matches == fail_at_match, sessions[0].ps_calls
    assert r.status == "error", (r.code, r.fields)
    assert r.code == "FS_ERROR", (r.code, r.fields)
    assert r.fields.get("link_lost") == 1, r.fields
    assert r.fields.get("reopen_hint") == "endpoint close then open", r.fields

    files = store["files"]
    assert isinstance(files, dict)
    assert files.get(dest) == b"old-bytes", "prior destination must survive"
    assert [k for k in files if ".mrc-tmp-" in k] == [], "no orphan temp"

    ep = reg.get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.is_connected() is False
    meta = ep.transport.meta
    assert meta.get("marked_dead") is True
    assert meta.get("link_lost") is True
