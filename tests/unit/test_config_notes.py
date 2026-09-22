"""Host notes store: independent files, atomic replace, append/prepend."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mcp_remote_control.config import (
    ConfigError,
    ConfigInvalid,
    NotesNotFound,
    ProfileInvalid,
    notes_dir,
    notes_path,
    notes_present,
)
from mcp_remote_control.config.store import (
    append_notes,
    delete_notes,
    ensure_home_layout,
    prepend_notes,
    read_notes,
    stat_notes,
    write_notes,
)
from mcp_remote_control.config import store as store_mod

_PEM = "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "mrc"
    ensure_home_layout(home)
    return home


def _set_max_body_chars(home: Path, n: int) -> None:
    cfg = home / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    if "max_body_chars" in text:
        lines = []
        for line in text.splitlines(keepends=True):
            if line.startswith("max_body_chars"):
                lines.append(f"max_body_chars = {n}\n")
            else:
                lines.append(line)
        cfg.write_text("".join(lines), encoding="utf-8")
        return
    cfg.write_text(
        text.replace("[defaults]\n", f"[defaults]\nmax_body_chars = {n}\n"),
        encoding="utf-8",
    )


def test_ensure_home_creates_notes_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)
    nd = notes_dir(home)
    assert nd.is_dir()
    assert nd == home / "notes"
    paths = ensure_home_layout(home)
    assert Path(paths["notes"]) == nd


def test_write_replaces_not_concatenates(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "first")
    write_notes(home, "box", "second")
    assert read_notes(home, "box") == "second"
    assert (home / "notes" / "box.md").read_bytes() == b"second"


def test_notes_preserve_cr_bytes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "a\r\nb")
    path = home / "notes" / "box.md"
    assert read_notes(home, "box") == "a\r\nb"
    assert path.read_bytes() == b"a\r\nb"
    assert stat_notes(home, "box")["bytes"] == 4

    append_notes(home, "box", "c")
    assert read_notes(home, "box") == "a\r\nbc"
    assert path.read_bytes() == b"a\r\nbc"
    assert stat_notes(home, "box")["bytes"] == 5

    write_notes(home, "other", "a\r\nb")
    prepend_notes(home, "other", "c")
    other = home / "notes" / "other.md"
    assert read_notes(home, "other") == "ca\r\nb"
    assert other.read_bytes() == b"ca\r\nb"
    assert stat_notes(home, "other")["bytes"] == 5


def test_write_empty_truncates_and_not_present(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "hello")
    assert notes_present(home, "box") is True
    write_notes(home, "box", "")
    path = home / "notes" / "box.md"
    assert path.is_file()
    assert path.stat().st_size == 0
    assert read_notes(home, "box") == ""
    assert notes_present(home, "box") is False


def test_append_raw_concat_no_newline(tmp_path: Path) -> None:
    home = _home(tmp_path)
    append_notes(home, "box", "A")
    append_notes(home, "box", "B")
    assert read_notes(home, "box") == "AB"


def test_prepend_inserts_at_front(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "AB")
    prepend_notes(home, "box", "X")
    assert read_notes(home, "box") == "XAB"


def test_empty_append_rejected_old_unchanged(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "keep")
    with pytest.raises(ConfigInvalid, match="empty"):
        append_notes(home, "box", "")
    assert read_notes(home, "box") == "keep"


def test_empty_prepend_rejected_old_unchanged(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "keep")
    with pytest.raises(ConfigInvalid, match="empty"):
        prepend_notes(home, "box", "")
    assert read_notes(home, "box") == "keep"


def test_empty_append_missing_file_does_not_create(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(ConfigInvalid, match="empty"):
        append_notes(home, "ghost", "")
    assert not (home / "notes" / "ghost.md").exists()
    with pytest.raises(ConfigInvalid, match="empty"):
        prepend_notes(home, "ghost", "")
    assert not (home / "notes" / "ghost.md").exists()


def test_oversize_write_rejected_old_unchanged(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _set_max_body_chars(home, 4)
    write_notes(home, "box", "abcd")
    with pytest.raises(ConfigInvalid, match="max_body_chars"):
        write_notes(home, "box", "abcde")
    assert read_notes(home, "box") == "abcd"


def test_oversize_append_rejected_old_unchanged(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _set_max_body_chars(home, 4)
    write_notes(home, "box", "ab")
    with pytest.raises(ConfigInvalid, match="max_body_chars"):
        append_notes(home, "box", "xyz")
    assert read_notes(home, "box") == "ab"
    with pytest.raises(ConfigInvalid, match="max_body_chars"):
        prepend_notes(home, "box", "xyz")
    assert read_notes(home, "box") == "ab"


def test_pem_armor_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "ok")
    with pytest.raises(ConfigInvalid, match="PEM"):
        write_notes(home, "box", _PEM)
    assert read_notes(home, "box") == "ok"
    with pytest.raises(ConfigInvalid, match="PEM"):
        append_notes(home, "box", _PEM)
    with pytest.raises(ConfigInvalid, match="PEM"):
        prepend_notes(home, "box", _PEM)
    assert read_notes(home, "box") == "ok"


def test_append_does_not_scan_historical_pem(tmp_path: Path) -> None:
    home = _home(tmp_path)
    path = home / "notes" / "box.md"
    path.write_text(_PEM, encoding="utf-8")
    append_notes(home, "box", "tail")
    assert read_notes(home, "box") == _PEM + "tail"


def test_illegal_name_rejected_before_path_join(tmp_path: Path) -> None:
    home = _home(tmp_path)
    sentinel = home / "outside.txt"
    sentinel.write_text("safe", encoding="utf-8")
    for bad in ("../x", "", "..", "a/b", "../outside"):
        with pytest.raises(ProfileInvalid):
            notes_path(home, bad)
        with pytest.raises(ProfileInvalid):
            write_notes(home, bad, "x")
        with pytest.raises(ProfileInvalid):
            read_notes(home, bad)
        with pytest.raises(ProfileInvalid):
            stat_notes(home, bad)
        with pytest.raises(ProfileInvalid):
            delete_notes(home, bad)
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert not (home / "x.md").exists()
    assert not (home / "x").exists()
    assert list((home / "notes").iterdir()) == []


def test_missing_read_stat_rm_raise_notes_not_found(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(NotesNotFound) as ei:
        read_notes(home, "gone")
    msg = str(ei.value)
    assert "notes" in msg.lower()
    assert "gone" in msg
    assert str(home.resolve()) not in msg
    assert isinstance(ei.value, ConfigError)
    with pytest.raises(NotesNotFound):
        stat_notes(home, "gone")
    with pytest.raises(NotesNotFound):
        delete_notes(home, "gone")
    assert notes_present(home, "gone") is False


def test_stat_bytes_no_body(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "abcd")
    info = stat_notes(home, "box")
    assert info["bytes"] == 4
    assert "mtime" in info
    assert isinstance(info["mtime"], float)
    assert "body" not in info
    payload = repr(info)
    assert "abcd" not in payload
    extra = set(info) - {"bytes", "mtime"}
    for key in extra:
        val = info[key]
        assert isinstance(val, (int, float, bool)) or val is None
        assert "abcd" not in repr(val)


def test_delete_notes_removes_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "x")
    delete_notes(home, "box")
    assert not (home / "notes" / "box.md").exists()
    assert notes_present(home, "box") is False


def test_append_creates_when_missing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    append_notes(home, "box", "new")
    assert read_notes(home, "box") == "new"
    prepend_notes(home, "other", "head")
    assert read_notes(home, "other") == "head"


@pytest.mark.parametrize(
    "mutate",
    [write_notes, append_notes, prepend_notes],
    ids=["write_notes", "append_notes", "prepend_notes"],
)
def test_write_atomic_failure_leaves_old(
    tmp_path: Path, monkeypatch, mutate
) -> None:
    home = _home(tmp_path)
    seed = "v1"
    write_notes(home, "box", seed)
    path = home / "notes" / "box.md"
    seed_bytes = path.read_bytes()
    seed_size = stat_notes(home, "box")["bytes"]

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        mutate(home, "box", "v2")
    assert path.read_bytes() == seed_bytes
    assert stat_notes(home, "box")["bytes"] == seed_size
    leftovers = [q for q in (home / "notes").iterdir() if ".tmp" in q.name]
    assert leftovers == []


@pytest.mark.parametrize(
    "mutate",
    [write_notes, append_notes, prepend_notes],
    ids=["write_notes", "append_notes", "prepend_notes"],
)
def test_write_fsync_before_replace(
    tmp_path: Path, monkeypatch, mutate
) -> None:
    home = _home(tmp_path)
    if mutate is not write_notes:
        write_notes(home, "box", "seed")
    order: list[str] = []
    real_fsync = store_mod.os.fsync
    real_replace = store_mod.os.replace

    def spy_fsync(fd: int) -> None:
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(
        src: str | os.PathLike[str], dst: str | os.PathLike[str]
    ) -> None:
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "fsync", spy_fsync)
    monkeypatch.setattr(store_mod.os, "replace", spy_replace)
    mutate(home, "box", "durable")
    assert "fsync" in order
    assert "replace" in order
    assert order.index("fsync") < order.index("replace")
    leftovers = [q for q in (home / "notes").iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_same_name_concurrent_append_keeps_both(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_notes(home, "box", "")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def go(frag: str) -> None:
        try:
            barrier.wait()
            append_notes(home, "box", frag)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=go, args=("A",))
    t2 = threading.Thread(target=go, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    body = read_notes(home, "box")
    assert body in ("AB", "BA")


def test_notes_name_lock_is_reentrant(tmp_path: Path) -> None:
    """Holding the per-name lock does not deadlock a notes write for that name.

    A caller holds this lock across a profile-existence check plus the notes
    mutation, and the mutation takes the same lock again; re-entry from the
    holding thread must be allowed. The write runs on a helper thread so a
    non-reentrant lock fails this test instead of hanging it.
    """
    home = _home(tmp_path)
    lock = store_mod.notes_name_lock(home, "box")
    done = threading.Event()
    errors: list[BaseException] = []

    def nested_write() -> None:
        try:
            with lock:
                write_notes(home, "box", "held")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    writer = threading.Thread(target=nested_write)
    writer.start()
    writer.join(5.0)
    assert not writer.is_alive(), "nested same-lock notes write deadlocked"
    assert errors == []
    assert read_notes(home, "box") == "held"


def test_notes_name_lock_is_per_name(tmp_path: Path) -> None:
    """Names do not share one lock: holding one name must not block another.

    Unrelated profiles stay independent; a single global lock would serialize
    every notes mutation against every other name.
    """
    home = _home(tmp_path)
    lock_a = store_mod.notes_name_lock(home, "alpha")
    lock_b = store_mod.notes_name_lock(home, "beta")
    assert lock_a is store_mod.notes_name_lock(home, "alpha")
    assert lock_a is not lock_b

    done = threading.Event()
    errors: list[BaseException] = []

    def write_beta() -> None:
        try:
            write_notes(home, "beta", "independent")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    with lock_a:
        writer = threading.Thread(target=write_beta)
        writer.start()
        writer.join(5.0)
        assert not writer.is_alive(), "beta was blocked by the lock held for alpha"
    assert errors == []
    assert read_notes(home, "beta") == "independent"
