"""Local transfers must refuse a special-file source before opening it.

``LocalFs.read`` refuses a FIFO/socket/device by stat before opening, because
such a file has no end-of-file and no fs operation carries a wall-clock
budget: the read would block in the kernel forever and strand the caller.
``LocalFs.get`` feeds a caller-named path into the same kind of open, so it
must refuse on the same terms. Without the guard the progress branch blocks
in ``open()``/``read()``, and ``shutil.copyfile`` (the no-progress branch) only
rejects FIFOs, so a device source would copy without end.

Every call that can block runs on a worker thread with a join deadline, so a
regression fails the test instead of hanging the suite.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mcp_remote_control.fs.backends.local import LocalFs, _copy_file
from mcp_remote_control.fs.types import FsError, TransferResult

# /dev/zero is the portable POSIX "readable forever, never EOF" device.
_ENDLESS_DEVICE = "/dev/zero"

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO")
device_only = pytest.mark.skipif(
    not Path(_ENDLESS_DEVICE).exists(),
    reason=f"{_ENDLESS_DEVICE} not present",
)


def _call_in_thread(fn, timeout: float = 10.0) -> object:
    """Run *fn* on a daemon thread; assert it returns within *timeout*."""
    box: list[object] = []

    def worker() -> None:
        try:
            box.append(fn())
        except BaseException as exc:  # noqa: BLE001 - any outcome is the result
            box.append(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout)
    assert not th.is_alive(), f"{fn} blocked indefinitely"
    assert len(box) == 1, box
    return box[0]


def _make_fifo(path: Path) -> None:
    os.mkfifo(path)


@posix_only
def test_get_fifo_with_progress_is_refused_without_blocking(tmp_path: Path) -> None:
    """A writer-less FIFO source must fail fast on the progress branch."""
    fifo = tmp_path / "pipe"
    _make_fifo(fifo)
    outcome = _call_in_thread(
        lambda: LocalFs().get(
            str(fifo), str(tmp_path / "out"), progress=lambda done, total: None
        )
    )
    assert isinstance(outcome, FsError), outcome
    assert outcome.code == "NOT_A_FILE"


@posix_only
def test_get_fifo_without_progress_is_refused(tmp_path: Path) -> None:
    """Both copy branches refuse: shutil's FIFO check is not the guard."""
    fifo = tmp_path / "pipe"
    _make_fifo(fifo)
    outcome = _call_in_thread(lambda: LocalFs().get(str(fifo), str(tmp_path / "out")))
    assert isinstance(outcome, FsError), outcome
    assert outcome.code == "NOT_A_FILE"


@posix_only
def test_copy_helper_refuses_fifo_source_for_either_branch(tmp_path: Path) -> None:
    """The shared copy helper is the choke point every caller passes through."""
    fifo = tmp_path / "pipe"
    _make_fifo(fifo)
    for progress in (None, lambda done, total: None):
        outcome = _call_in_thread(
            lambda progress=progress: _copy_file(
                fifo, tmp_path / "copy-out", progress=progress
            )
        )
        assert isinstance(outcome, FsError), outcome
        assert outcome.code == "NOT_A_FILE"
        assert not (tmp_path / "copy-out").exists()


@device_only
def test_get_endless_device_is_refused_before_the_copy_starts(tmp_path: Path) -> None:
    """A device source has no EOF either; refusal must precede any read.

    The progress callback aborts the copy if it is ever reached, so a
    regression fails here instead of filling the disk with zeros.
    """
    copied = False

    def abort_if_copied(done: int, total: int) -> None:
        nonlocal copied
        copied = True
        raise RuntimeError("copy of a special file started")

    outcome = _call_in_thread(
        lambda: LocalFs().get(_ENDLESS_DEVICE, str(tmp_path / "out"), progress=abort_if_copied)
    )
    assert isinstance(outcome, FsError), outcome
    assert outcome.code == "NOT_A_FILE"
    assert copied is False
    assert not (tmp_path / "out").exists()


@posix_only
def test_put_fifo_source_is_refused_without_blocking(tmp_path: Path) -> None:
    """The sibling op takes a different guard and must stay fast as well."""
    fifo = tmp_path / "pipe"
    _make_fifo(fifo)
    outcome = _call_in_thread(
        lambda: LocalFs().put(
            str(fifo), str(tmp_path / "out"), progress=lambda done, total: None
        )
    )
    assert isinstance(outcome, FsError), outcome
    # put guards with is_file() before the copy helper; either refusal is a
    # fast, explicit error, which is what the invariant requires.
    assert outcome.code in ("NOT_FOUND", "NOT_A_FILE")


def test_get_regular_file_still_copies(tmp_path: Path) -> None:
    src = tmp_path / "plain.txt"
    src.write_text("hello-local", encoding="utf-8")
    dst = tmp_path / "copied.txt"
    result = LocalFs().get(str(src), str(dst), progress=lambda done, total: None)
    assert isinstance(result, TransferResult)
    assert result.bytes_transferred == len("hello-local")
    assert dst.read_text(encoding="utf-8") == "hello-local"


def test_get_follows_symlink_to_regular_file(tmp_path: Path) -> None:
    """The guard stats (not lstat): a symlinked regular file must still copy."""
    target = tmp_path / "target.txt"
    target.write_text("through-link", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    dst = tmp_path / "copied.txt"
    LocalFs().get(str(link), str(dst))
    assert dst.read_text(encoding="utf-8") == "through-link"


def test_get_directory_and_missing_still_rejected(tmp_path: Path) -> None:
    """Neighbouring path checks keep their codes after the regular-file gate."""
    with pytest.raises(FsError) as dir_exc:
        LocalFs().get(str(tmp_path), str(tmp_path / "out"))
    assert dir_exc.value.code == "IS_A_DIR"

    with pytest.raises(FsError) as missing_exc:
        LocalFs().get(str(tmp_path / "nope"), str(tmp_path / "out"))
    assert missing_exc.value.code == "NOT_FOUND"

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "absent")
    with pytest.raises(FsError) as dangling_exc:
        LocalFs().get(str(dangling), str(tmp_path / "out"))
    assert dangling_exc.value.code == "NOT_FOUND"
