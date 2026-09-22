"""Service tests: ``fs get`` preserves the local destination's mode.

Both remote download paths write into a same-directory temp and then
``os.replace`` it over the destination. The temp is created by the transfer
client under the local umask (or carries the client's own temp mode), so
without re-applying the destination's prior mode an existing ``0o600`` file
such as ``~/.netrc`` is silently widened. These cover the download side of
the policy the local backend and the SFTP/WinRM put paths already enforce.

The mode of a *missing* destination is deliberately not this backend's
choice - there is no prior mode to preserve, so it keeps whatever the
transfer client created. That is a real divergence from ``LocalFs.get``,
which has the source inode to hand and copies the source mode onto a new
destination; a remote source offers no cheap, backend-independent
equivalent, so no local source mode is fabricated here.

The expected mode therefore has to come from the client actually in use,
not from a constant: asyncssh's ``SFTPClient.get`` creates the local file
through a plain ``open(..., "wb")`` when ``preserve`` is False (the default
used here), while pypsrp's ``Client.fetch`` stages into ``tempfile.mkstemp()``
and ``shutil.copy``s it, so a new WinRM destination lands at ``0o600``. The
in-repo ``MockWinrmFileClient`` writes with ``Path.write_bytes`` and cannot
show that, hence the faithful subclass below.
"""

from __future__ import annotations

import os
import shutil
import stat as statmod
import tempfile
from pathlib import Path

import pytest

from _sftp_fakes import MockSftp
from _winrm_fakes import HOME, TEMP
from test_fs_winrm import MockWinrmFileClient

from mcp_remote_control.fs.backends.sftp import SftpFs
from mcp_remote_control.fs.backends.winrm import WinrmFs
from mcp_remote_control.fs.types import FsError

SECRET = b"machine example.com login u password p\n"


def _mode(path: Path) -> int:
    return statmod.S_IMODE(path.stat().st_mode)


def _umask_default_file_mode() -> int:
    """Mode a plain ``open(path, "wb")`` creates under the current umask."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _no_temps(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if ".mrc-tmp-" in name]


def _constrained_ps_caps() -> dict:
    """Caps where PowerShell script FS is unavailable -> native-fetch-only get."""
    return {
        "ps_version": "5.1.19041",
        "language_mode": "ConstrainedLanguage",
        "ps_script_fs": False,
        "ps_oneshot": True,
        "ps_runspace": False,
    }


class _MkstempFetchClient(MockWinrmFileClient):
    """Native fetch staged through ``mkstemp`` + ``shutil.copy``.

    ``MockWinrmFileClient.fetch`` writes the temp with ``Path.write_bytes``
    (umask default), but production ``fetch`` stages through
    ``tempfile.mkstemp()`` and ``shutil.copy``s that onto the destination,
    which is the mode a caller actually observes for a new destination.
    """

    def fetch(self, remote: str, local: str) -> None:
        data = self.read_file(remote)
        fd, staged = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            shutil.copy(staged, local)
        finally:
            os.unlink(staged)



# ---------------------------------------------------------------------------
# SFTP get
# ---------------------------------------------------------------------------


def test_sftp_get_preserves_destination_mode(tmp_path: Path) -> None:
    """An existing 0600 destination keeps 0600 across the temp + replace."""
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    r = backend.get("/d/netrc", str(dst))

    assert r.bytes_transferred == len(SECRET)
    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"
    assert _no_temps(tmp_path) == []


def test_sftp_get_with_progress_preserves_destination_mode(tmp_path: Path) -> None:
    """The chunked progress branch preserves the destination mode too."""
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    seen: list[tuple[int, int | None]] = []
    backend.get("/d/netrc", str(dst), progress=lambda d, t: seen.append((d, t)))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"
    assert seen, "progress callback never fired"
    assert _no_temps(tmp_path) == []


def test_sftp_get_new_destination_keeps_default_mode(tmp_path: Path) -> None:
    """A missing destination has no prior mode - keep the client's temp mode.

    ``MockSftp.get`` opens the local path with ``Path.write_bytes``, matching
    asyncssh's default (``preserve=False``), so the umask-derived mode here is
    what production yields. Nothing is chmod-ed onto a destination the caller
    did not already have.
    """
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    dst = tmp_path / "fresh.netrc"
    backend.get("/d/netrc", str(dst))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == _umask_default_file_mode()


def test_sftp_get_symlink_destination_preserves_referent_mode(tmp_path: Path) -> None:
    """A symlink dst keeps the link and the referent's mode (not the temp's)."""
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    target = tmp_path / "real-netrc"
    target.write_bytes(b"old")
    os.chmod(target, 0o600)
    link = tmp_path / "link-netrc"
    link.symlink_to(target)

    backend.get("/d/netrc", str(link))

    assert link.is_symlink(), "get replaced the symlink with a regular file"
    assert target.read_bytes() == SECRET
    assert _mode(target) == 0o600, f"expected 0o600, got {oct(_mode(target))}"


def test_sftp_get_failure_removes_temp(tmp_path: Path) -> None:
    """A failing transfer still removes the temp and leaves the dst untouched."""

    class _FailGetSftp(MockSftp):
        def get(self, remote: str, local: str) -> None:
            Path(local).write_bytes(b"partial")
            raise OSError("simulated mid-download failure")

    mock = _FailGetSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    with pytest.raises(FsError):
        backend.get("/d/netrc", str(dst))

    assert dst.read_bytes() == b"old"
    assert _mode(dst) == 0o600
    assert _no_temps(tmp_path) == []


def test_sftp_get_survives_destination_without_chmod_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination filesystem that rejects chmod must not abort the get.

    The destination's prior mode simply cannot be re-applied there (exFAT,
    some FUSE mounts). The payload is already transferred, and letting the
    OSError escape would report a local capability gap as a permission error
    naming the readable remote path.
    """
    mock = MockSftp()
    mock.dirs.add("/d")
    mock.files["/d/netrc"] = SECRET
    backend = SftpFs(mock, cwd="/", home="/home/u")

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    def _reject_chmod(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", _reject_chmod)

    r = backend.get("/d/netrc", str(dst))

    assert r.bytes_transferred == len(SECRET)
    assert dst.read_bytes() == SECRET
    assert _no_temps(tmp_path) == []


# ---------------------------------------------------------------------------
# WinRM get
# ---------------------------------------------------------------------------


def _winrm_store(*, native_fetch: bool) -> MockWinrmFileClient:
    store = MockWinrmFileClient()
    store.has_native_fetch = native_fetch
    store.files[rf"{TEMP}\netrc"] = SECRET
    return store


def test_winrm_get_preserves_destination_mode_native_fetch(tmp_path: Path) -> None:
    """Native fetch branch: the destination keeps 0600 across the replace."""
    store = _winrm_store(native_fetch=True)
    backend = WinrmFs(store, cwd=HOME, home=HOME)

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    r = backend.get(rf"{TEMP}\netrc", str(dst))

    assert r.bytes_transferred == len(SECRET)
    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"
    assert _no_temps(tmp_path) == []


def test_winrm_get_preserves_destination_mode_read_file_fallback(
    tmp_path: Path,
) -> None:
    """read_file fallback branch: the destination keeps 0600 across the replace."""
    store = _winrm_store(native_fetch=False)
    backend = WinrmFs(store, cwd=HOME, home=HOME)

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    backend.get(rf"{TEMP}\netrc", str(dst))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"
    assert _no_temps(tmp_path) == []


def test_winrm_get_preserves_destination_mode_gated_native_branch(
    tmp_path: Path,
) -> None:
    """ConstrainedLanguage gate -> native-fetch-only get still preserves mode."""
    store = _winrm_store(native_fetch=True)
    backend = WinrmFs(store, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    backend.get(rf"{TEMP}\netrc", str(dst))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"
    assert _no_temps(tmp_path) == []


def test_winrm_get_new_destination_keeps_client_mode(tmp_path: Path) -> None:
    """Native fetch: a new destination keeps the mode pypsrp's fetch gave it.

    Production ``fetch`` stages into an ``mkstemp`` file (``0o600``) and
    ``shutil.copy``s it, so a brand-new destination is ``0o600`` and not the
    umask default. Any unconditional chmod on the missing-destination branch
    would widen it and fail here.
    """
    store = _MkstempFetchClient()
    store.files[rf"{TEMP}\netrc"] = SECRET
    backend = WinrmFs(store, cwd=HOME, home=HOME)

    dst = tmp_path / "fresh.netrc"
    backend.get(rf"{TEMP}\netrc", str(dst))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == 0o600, f"expected 0o600, got {oct(_mode(dst))}"


def test_winrm_get_new_destination_read_file_fallback_keeps_default_mode(
    tmp_path: Path,
) -> None:
    """read_file fallback: the temp is a plain write, so the umask mode stands."""
    store = _winrm_store(native_fetch=False)
    backend = WinrmFs(store, cwd=HOME, home=HOME)

    dst = tmp_path / "fresh.netrc"
    backend.get(rf"{TEMP}\netrc", str(dst))

    assert dst.read_bytes() == SECRET
    assert _mode(dst) == _umask_default_file_mode()


def test_winrm_get_symlink_destination_preserves_referent_mode(
    tmp_path: Path,
) -> None:
    """A symlink dst keeps the link and the referent's mode."""
    store = _winrm_store(native_fetch=True)
    backend = WinrmFs(store, cwd=HOME, home=HOME)

    target = tmp_path / "real-netrc"
    target.write_bytes(b"old")
    os.chmod(target, 0o600)
    link = tmp_path / "link-netrc"
    link.symlink_to(target)

    backend.get(rf"{TEMP}\netrc", str(link))

    assert link.is_symlink(), "get replaced the symlink with a regular file"
    assert target.read_bytes() == SECRET
    assert _mode(target) == 0o600, f"expected 0o600, got {oct(_mode(target))}"


@pytest.mark.parametrize(
    ("native_fetch", "constrained"),
    [(True, False), (False, False), (True, True)],
    ids=["native_fetch", "read_file_fallback", "gated_native_branch"],
)
def test_winrm_get_survives_destination_without_chmod_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_fetch: bool,
    constrained: bool,
) -> None:
    """A destination filesystem that rejects chmod must not abort the get.

    Same tolerance as the SFTP get: the destination's prior mode simply
    cannot be re-applied there (exFAT, some FUSE mounts). The payload is
    already transferred, and letting the OSError escape would report a local
    capability gap as a permission error naming the readable remote path -
    and on the read_file fallback would discard the finished download.
    """
    store = _winrm_store(native_fetch=native_fetch)
    caps = _constrained_ps_caps() if constrained else None
    backend = WinrmFs(store, cwd=HOME, home=HOME, ps_caps=caps)

    dst = tmp_path / ".netrc"
    dst.write_bytes(b"old")
    os.chmod(dst, 0o600)

    def _reject_chmod(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", _reject_chmod)

    r = backend.get(rf"{TEMP}\netrc", str(dst))

    assert r.bytes_transferred == len(SECRET)
    assert dst.read_bytes() == SECRET
    assert _no_temps(tmp_path) == []
