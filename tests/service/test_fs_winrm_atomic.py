"""Service tests: WinRM open-promote, reparse/symlink, and atomic get."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from _winrm_fakes import HOME, TEMP
from test_fs_winrm import MockWinrmFileClient, _MockAttrs, _backend

from mcp_remote_control.core import fs_ops
from mcp_remote_control.fs.backends.winrm import WinrmFs
from mcp_remote_control.fs.types import FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def test_winrm_fs_partial_upload_cleans_up_remote(tmp_path: Path) -> None:
    """Failed chunked upload streams to a sibling temp only.

    Cleanup removes the temp (never the final dest). Handle is closed BEFORE
    remove so Windows does not reject delete of an open file.
    """

    class _FailingHandle:
        def __init__(self, path: str, log: list[str]) -> None:
            self._path = path
            self._log = log
            self.writes = 0

        def write(self, chunk: bytes) -> None:
            self.writes += 1
            if self.writes >= 2:
                raise OSError("simulated mid-stream transport failure")

        def close(self) -> None:
            self._log.append(f"close:{self._path}")

    class _OpenClient:
        def __init__(self) -> None:
            self.opened: list[str] = []
            self.remove_calls: list[str] = []
            # Ordered event log so we can assert close precedes remove.
            self.events: list[str] = []
            self._dirs: set[str] = {"C:\\", TEMP, rf"{TEMP}\dst"}
            self.files: dict[str, bytes] = {
                rf"{TEMP}\dst\partial.bin": b"prior-good-content",
            }

        def stat(self, path: str) -> _MockAttrs:
            if path in self._dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                return _MockAttrs("file", len(self.files[path]), 1.0, "Archive")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self._dirs.add(path)

        def open(self, path: str, mode: str) -> _FailingHandle:
            self.opened.append(path)
            return _FailingHandle(path, self.events)

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.events.append(f"remove:{path}")
            self.files.pop(path, None)

        def rename(self, src: str, dst: str) -> None:
            raise AssertionError("rename must not run after mid-stream write fail")

    src = tmp_path / "payload.bin"
    # Larger than one DEFAULT_TRANSFER_CHUNK so the loop reaches write #2.
    src.write_bytes(b"Y" * (256 * 1024 + 10))
    remote = rf"{TEMP}\dst\partial.bin"

    client = _OpenClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    with pytest.raises(Exception):  # noqa: B017
        backend.put(str(src), remote, progress=lambda d, t: None)

    # Stream lands on sibling temp, not the final dest.
    assert client.opened, "open must have been called"
    assert client.opened[0] != remote
    assert ".mrc-tmp-" in client.opened[0]
    # Cleanup removes temp only - prior dest content is preserved.
    assert client.remove_calls, "partial temp must be best-effort removed"
    assert all(p != remote for p in client.remove_calls), (
        "failure must not remove the final dest (prior good copy)"
    )
    assert all(".mrc-tmp-" in p for p in client.remove_calls)
    assert client.files.get(remote) == b"prior-good-content"
    # Handle closed before the remove: the close event must precede the remove
    # event so Windows doesn't reject the delete of an open file.
    closes = [i for i, e in enumerate(client.events) if e.startswith("close:")]
    removes = [i for i, e in enumerate(client.events) if e.startswith("remove:")]
    assert closes and removes, "both close and remove must have fired"
    assert closes[0] < removes[0], (
        "handle must be closed BEFORE remove (Windows rejects deleting an "
        "open file); events=" + repr(client.events)
    )


# ---------------------------------------------------------------------------
# SupportsFileOpen progress put - close fail-closed + temp+promote
# ---------------------------------------------------------------------------


def test_winrm_put_open_close_failure_not_success(tmp_path: Path) -> None:
    """Close raises on open/progress put -> error, not done-success;
    dest is not reported half-ok and prior content is preserved."""

    class _CloseFailHandle:
        def __init__(self, store: dict[str, bytes], path: str) -> None:
            self._store = store
            self._path = path
            self._buf = bytearray()
            self.close_calls = 0

        def write(self, chunk: bytes) -> None:
            self._buf.extend(chunk)

        def close(self) -> None:
            self.close_calls += 1
            # Commit temp so cleanup can observe it, then fail close.
            self._store[self._path] = bytes(self._buf)
            raise OSError("simulated winrm write close failure")

    class _CloseFailClient:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {
                rf"{TEMP}\dst\out.bin": b"original-remote",
            }
            self.dirs: set[str] = {"C:\\", TEMP, rf"{TEMP}\dst"}
            self.opened: list[str] = []
            self.rename_calls: list[tuple[str, str]] = []
            self.remove_calls: list[str] = []
            self.handles: list[_CloseFailHandle] = []

        def stat(self, path: str) -> _MockAttrs:
            if path in self.dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                return _MockAttrs("file", len(self.files[path]), 1.0, "Archive")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self.dirs.add(path)

        def open(self, path: str, mode: str = "rb") -> _CloseFailHandle:
            self.opened.append(path)
            fh = _CloseFailHandle(self.files, path)
            self.handles.append(fh)
            return fh

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            if src not in self.files:
                raise FileNotFoundError(src)
            self.files[dst] = self.files.pop(src)

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.files.pop(path, None)

    src = tmp_path / "up.bin"
    payload = b"N" * (256 * 1024 + 64)
    src.write_bytes(payload)
    remote = rf"{TEMP}\dst\out.bin"
    client = _CloseFailClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=remote,
        local=str(src),
        home=FIXTURES,
        backend=backend,
        progress=lambda d, t: None,
    )
    assert r.status == "error"
    assert r.code == "FS_ERROR"
    msg = str(r.fields.get("msg") or "")
    assert "close" in msg.lower()
    # Dest not replaced; no rename after close fail.
    assert client.files.get(remote) == b"original-remote"
    assert client.rename_calls == []
    assert client.opened and ".mrc-tmp-" in client.opened[0]
    assert all(".mrc-tmp-" in p for p in client.remove_calls)
    assert remote not in client.remove_calls
    assert client.handles and all(h.close_calls >= 1 for h in client.handles)
    # No leftover half-ok temp claiming success.
    assert [k for k in client.files if ".mrc-tmp-" in k] == []


def test_winrm_put_open_midstream_failure_preserves_dest(tmp_path: Path) -> None:
    """Mid-stream write fail must not silently destroy the only prior
    remote copy; error is raised and dest content remains recoverable."""

    class _FailMidHandle:
        def __init__(self) -> None:
            self.writes = 0
            self.closed = False

        def write(self, chunk: bytes) -> None:
            self.writes += 1
            if self.writes >= 2:
                raise OSError("simulated mid-stream write failure")

        def close(self) -> None:
            self.closed = True

    class _OpenStoreClient:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {
                rf"{TEMP}\keep.bin": b"only-prior-copy",
            }
            self.dirs: set[str] = {"C:\\", TEMP}
            self.opened: list[str] = []
            self.remove_calls: list[str] = []
            self.rename_calls: list[tuple[str, str]] = []

        def stat(self, path: str) -> _MockAttrs:
            if path in self.dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                return _MockAttrs("file", len(self.files[path]), 1.0, "Archive")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self.dirs.add(path)

        def open(self, path: str, mode: str = "rb") -> _FailMidHandle:
            self.opened.append(path)
            return _FailMidHandle()

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            raise AssertionError("must not promote after mid-stream failure")

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.files.pop(path, None)

    src = tmp_path / "payload.bin"
    src.write_bytes(b"Z" * (256 * 1024 + 10))
    remote = rf"{TEMP}\keep.bin"
    client = _OpenStoreClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    with pytest.raises(Exception) as ei:  # noqa: B017
        backend.put(str(src), remote, progress=lambda d, t: None)
    assert "mid-stream" in str(ei.value).lower() or "simulated" in str(ei.value).lower()

    assert client.files.get(remote) == b"only-prior-copy"
    assert client.rename_calls == []
    assert client.opened and ".mrc-tmp-" in client.opened[0]
    assert remote not in client.opened
    assert remote not in client.remove_calls
    assert all(".mrc-tmp-" in p for p in client.remove_calls)


def test_winrm_put_open_success_temp_promote(tmp_path: Path) -> None:
    """Successful open/progress put promotes temp onto dest; no leftovers."""

    class _OkHandle:
        def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
            self._store = store
            self._path = path
            self._mode = mode
            self._buf = bytearray()

        def write(self, chunk: bytes) -> None:
            self._buf.extend(chunk)

        def read(self, n: int) -> bytes:
            data = self._store.get(self._path, b"")
            # Simple full-read once for promote fallback tests.
            out = data[:n] if data else b""
            if out:
                self._store[self._path] = data[n:]
            return out

        def close(self) -> None:
            if "w" in self._mode:
                self._store[self._path] = bytes(self._buf)

    class _OpenRenameClient:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {
                rf"{TEMP}\final.bin": b"old",
            }
            self.dirs: set[str] = {"C:\\", TEMP}
            self.opened: list[str] = []
            self.rename_calls: list[tuple[str, str]] = []

        def stat(self, path: str) -> _MockAttrs:
            if path in self.dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                return _MockAttrs("file", len(self.files[path]), 1.0, "Archive")
            raise FileNotFoundError(path)

        def mkdir(self, path: str) -> None:
            self.dirs.add(path)

        def open(self, path: str, mode: str = "rb") -> _OkHandle:
            self.opened.append(path)
            return _OkHandle(self.files, path, mode)

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            if src not in self.files:
                raise FileNotFoundError(src)
            self.files[dst] = self.files.pop(src)

        def remove(self, path: str) -> None:
            self.files.pop(path, None)

    src = tmp_path / "ok.bin"
    payload = b"fresh-payload-bytes"
    src.write_bytes(payload)
    remote = rf"{TEMP}\final.bin"
    client = _OpenRenameClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    result = backend.put(str(src), remote, progress=lambda d, t: None)
    assert result.bytes_transferred == len(payload)
    assert client.files.get(remote) == payload
    assert client.rename_calls and client.rename_calls[0][1] == remote
    assert ".mrc-tmp-" in client.rename_calls[0][0]
    assert client.opened and ".mrc-tmp-" in client.opened[0]
    assert [k for k in client.files if ".mrc-tmp-" in k] == []


# ---------------------------------------------------------------------------
# Final-component reparse/symlink resolve on write/put (not replace link)
# ---------------------------------------------------------------------------


def test_winrm_write_symlink_preserves_link() -> None:
    """Write through a reparse/symlink updates the referent; link remains.

    Matches SFTP/local policy: atomic promote must land on the final referent,
    not replace the reparse directory entry with a regular file.
    """
    store, backend = _backend()
    target = rf"{TEMP}\target.txt"
    link = rf"{TEMP}\link.txt"
    store.files[target] = b"old-content"
    store.links[link] = target

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=link,
        content="new-content",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Link entry preserved (not converted into a regular file key).
    assert link in store.links
    assert store.links[link] == target
    assert link not in store.files
    # Referent content updated.
    assert store.files[target] == b"new-content"
    # Public stat still reports kind=link.
    r_stat = fs_ops.run(
        "stat", ep="lab-win", path=link, home=FIXTURES, backend=backend
    )
    assert r_stat.status == "ok"
    assert r_stat.fields.get("type") == "link"


def test_winrm_put_symlink_preserves_link(tmp_path: Path) -> None:
    """Put through a reparse/symlink updates the referent; link remains."""
    store, backend = _backend()
    target = rf"{TEMP}\target.bin"
    link = rf"{TEMP}\link.bin"
    store.files[target] = b"old"
    store.links[link] = target
    src = tmp_path / "payload.bin"
    src.write_bytes(b"payload-via-link")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=link,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert link in store.links
    assert store.links[link] == target
    assert link not in store.files
    assert store.files[target] == b"payload-via-link"


def test_winrm_write_symlink_chain_preserves_links() -> None:
    """Write through a reparse chain resolves to the final target."""
    store, backend = _backend()
    real = rf"{TEMP}\real.txt"
    link1 = rf"{TEMP}\link1"
    link2 = rf"{TEMP}\link2"
    store.files[real] = b"old"
    store.links[link1] = real
    store.links[link2] = link1

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=link2,
        content="via-chain",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.links[link1] == real
    assert store.links[link2] == link1
    assert link1 not in store.files
    assert link2 not in store.files
    assert store.files[real] == b"via-chain"


def test_winrm_put_open_promote_resolves_symlink(tmp_path: Path) -> None:
    """SupportsFileOpen temp+rename promote lands on referent, not link."""

    class _OpenRenameLinkClient:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {
                rf"{TEMP}\target.bin": b"old",
            }
            self.links: dict[str, str] = {
                rf"{TEMP}\link.bin": rf"{TEMP}\target.bin",
            }
            self.dirs: set[str] = {"C:\\", TEMP}
            self.rename_calls: list[tuple[str, str]] = []
            self.opened: list[str] = []

        def stat(self, path: str) -> _MockAttrs:
            if path in self.links:
                return _MockAttrs("link", 0, 1.0, "ReparsePoint")
            if path in self.dirs:
                return _MockAttrs("dir", 0, 1.0, "Directory")
            if path in self.files:
                return _MockAttrs("file", len(self.files[path]), 1.0, "Archive")
            raise FileNotFoundError(path)

        def readlink(self, path: str) -> str:
            if path not in self.links:
                raise OSError(f"not a link: {path}")
            return self.links[path]

        def mkdir(self, path: str) -> None:
            self.dirs.add(path)

        def open(self, path: str, mode: str = "rb") -> object:
            self.opened.append(path)

            class _H:
                def __init__(self, store: dict[str, bytes], p: str, m: str) -> None:
                    self._store = store
                    self._path = p
                    self._mode = m
                    self._buf = bytearray()

                def write(self, chunk: bytes) -> None:
                    self._buf.extend(chunk)

                def close(self) -> None:
                    if "w" in self._mode:
                        self._store[self._path] = bytes(self._buf)

            return _H(self.files, path, mode)

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            if src not in self.files:
                raise FileNotFoundError(src)
            self.files[dst] = self.files.pop(src)

        def remove(self, path: str) -> None:
            self.files.pop(path, None)

    src = tmp_path / "via-open.bin"
    payload = b"open-promote-payload"
    src.write_bytes(payload)
    link = rf"{TEMP}\link.bin"
    target = rf"{TEMP}\target.bin"
    client = _OpenRenameLinkClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)

    result = backend.put(str(src), link, progress=lambda d, t: None)
    assert result.bytes_transferred == len(payload)
    # Promote destination is the referent, not the link path.
    assert client.rename_calls
    assert client.rename_calls[0][1] == target
    assert ".mrc-tmp-" in client.rename_calls[0][0]
    # Temp sibling of referent (same volume as target).
    assert client.rename_calls[0][0].startswith(rf"{TEMP}\.")
    assert client.files.get(target) == payload
    assert link in client.links
    assert link not in client.files


def test_winrm_write_ordinary_file_still_atomic() -> None:
    """Non-link write still succeeds (temp+promote path)."""
    store, backend = _backend()
    dest = rf"{TEMP}\ordinary.txt"
    store.files[dest] = b"before"

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=dest,
        content="after-ordinary",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.files[dest] == b"after-ordinary"
    assert dest not in store.links
    # No temp leftovers.
    assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_resolve_final_link_dangling_writes_at_link_path() -> None:
    """Dangling reparse -> write at current path (create referent path)."""
    store, backend = _backend()
    link = rf"{TEMP}\dangling"
    missing = rf"{TEMP}\no-such-target.txt"
    store.links[link] = missing

    # Resolve returns the missing target path (write will create it).
    dest = backend._resolve_final_link(store, link)
    assert dest == missing

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=link,
        content="created-via-dangling",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Link entry still present; content at resolved (previously missing) path.
    assert link in store.links
    assert store.files[missing] == b"created-via-dangling"
    assert link not in store.files


def test_winrm_resolve_final_link_cycle_raises() -> None:
    """Reparse cycle exceeds _MAX_SYMLINK_FOLLOW -> clear FsError."""
    from mcp_remote_control.fs.backends.winrm import _MAX_SYMLINK_FOLLOW

    store, backend = _backend()
    a = rf"{TEMP}\cycle-a"
    b = rf"{TEMP}\cycle-b"
    store.links[a] = b
    store.links[b] = a

    with pytest.raises(FsError) as ei:
        backend._resolve_final_link(store, a)
    err = ei.value
    assert err.code == "FS_ERROR"
    assert "too many symbolic links" in err.msg
    assert err.details.get("path") == a
    # Sanity: cap is the same constant SFTP uses (32).
    assert _MAX_SYMLINK_FOLLOW == 32


def test_winrm_kind_from_attrs_reparsepoint_mode() -> None:
    """Attributes containing ReparsePoint promote kind to link."""
    from mcp_remote_control.fs.backends.winrm import _kind_from_attrs

    assert _kind_from_attrs({"kind": "file", "mode": "Archive, ReparsePoint"}) == "link"
    assert _kind_from_attrs({"kind": "dir", "mode": "Directory, ReparsePoint"}) == "link"
    assert _kind_from_attrs({"kind": "file", "mode": "Archive"}) == "file"
    assert _kind_from_attrs(_MockAttrs("file", 1, 1.0, "Archive, ReparsePoint")) == "link"


# ---------------------------------------------------------------------------
# mkdir_p treats reparse/symlink-to-directory parents as existing dirs
# ---------------------------------------------------------------------------


def test_winrm_write_under_symlink_to_dir_parent() -> None:
    """Write under a reparse-to-directory parent succeeds.

    Production failure mode: ``C:\\srv\\www`` -> ``C:\\var\\www`` reports
    kind=link via reparse promotion; ``_mkdir_p`` must treat the link as an
    existing dir, not ALREADY_EXISTS. Mirrors SFTP symlink-to-dir parents.
    """
    store, backend = _backend()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=r"C:\srv\www\app.conf",
        content="listen=80\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.files.get(r"C:\srv\www\app.conf") == b"listen=80\n"
    # Parent reparse entry preserved (not replaced by a real directory).
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"
    assert not store._exists_dir(r"C:\srv\www")


def test_winrm_mkdir_under_symlink_to_dir_parent() -> None:
    """mkdir under a reparse-to-directory parent succeeds."""
    store, backend = _backend()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"

    r = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=r"C:\srv\www\releases",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store._exists_dir(r"C:\srv\www\releases")
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"
    assert not store._exists_dir(r"C:\srv\www")


def test_winrm_mkdir_symlink_to_dir_itself_is_ok() -> None:
    """mkdir parents=True on an existing reparse-to-dir is a no-op success."""
    store, backend = _backend()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"

    r = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=r"C:\srv\www",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    # Still a reparse entry, not materialised into dirs.
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"
    assert not store._exists_dir(r"C:\srv\www")


def test_winrm_write_under_symlink_to_file_parent_rejected() -> None:
    """Reparse-to-file as a parent path still raises ALREADY_EXISTS."""
    store, backend = _backend()
    store.dirs.add(rf"{TEMP}\d")
    real = rf"{TEMP}\d\real.txt"
    notdir = rf"{TEMP}\d\notdir"
    store.files[real] = b"body"
    store.links[notdir] = real

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=rf"{TEMP}\d\notdir\child.txt",
        content="nope",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"
    assert "not a directory" in (r.fields.get("msg") or "").lower()
    assert rf"{TEMP}\d\notdir\child.txt" not in store.files


def test_winrm_mkdir_symlink_to_file_rejected() -> None:
    """mkdir on a reparse-to-file path is rejected (not treated as dir)."""
    store, backend = _backend()
    store.dirs.add(rf"{TEMP}\d")
    real = rf"{TEMP}\d\real.txt"
    filelink = rf"{TEMP}\d\filelink"
    store.files[real] = b"body"
    store.links[filelink] = real

    r = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=filelink,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"
    assert "not a directory" in (r.fields.get("msg") or "").lower()


def test_winrm_write_under_dangling_symlink_parent_rejected() -> None:
    """Dangling reparse parent is not mis-allowed as a directory."""
    store, backend = _backend()
    store.dirs.add(rf"{TEMP}\d")
    broken = rf"{TEMP}\d\broken"
    store.links[broken] = rf"{TEMP}\d\missing-target"

    r = fs_ops.run(
        "write",
        ep="lab-win",
        path=rf"{TEMP}\d\broken\child.txt",
        content="nope",
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "ALREADY_EXISTS"
    assert "not a directory" in (r.fields.get("msg") or "").lower()


def test_winrm_write_under_ordinary_dir_parent_unchanged() -> None:
    """Ordinary directory parents still work for write/mkdir."""
    store, backend = _backend()
    work = rf"{TEMP}\x13-ord"

    r_mkdir = fs_ops.run(
        "mkdir",
        ep="lab-win",
        path=rf"{work}\sub",
        home=FIXTURES,
        backend=backend,
    )
    assert r_mkdir.status == "ok"
    assert store._exists_dir(rf"{work}\sub")

    r_write = fs_ops.run(
        "write",
        ep="lab-win",
        path=rf"{work}\sub\f.txt",
        content="ok\n",
        home=FIXTURES,
        backend=backend,
    )
    assert r_write.status == "ok"
    assert store.files.get(rf"{work}\sub\f.txt") == b"ok\n"


def test_winrm_put_under_symlink_to_dir_parent(tmp_path: Path) -> None:
    """Put under a reparse-to-directory parent succeeds; link preserved."""
    store, backend = _backend()
    store.dirs.update({"C:\\srv", "C:\\var", "C:\\var\\www"})
    store.links[r"C:\srv\www"] = r"C:\var\www"
    src = tmp_path / "payload.bin"
    src.write_bytes(b"put-under-link")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=r"C:\srv\www\payload.bin",
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.files.get(r"C:\srv\www\payload.bin") == b"put-under-link"
    assert store.links.get(r"C:\srv\www") == r"C:\var\www"
    assert not store._exists_dir(r"C:\srv\www")


def test_winrm_native_put_uses_temp_then_promote(tmp_path: Path) -> None:
    """ps_script_fs allowed: native copy lands on a sibling temp, then promote.

    Dest is not in the copy target list; prior dest is replaced only by
    promote. Mid-copy never opens dest.
    """

    class _AtomicCopyClient(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.copy_targets: list[str] = []
            self.rename_calls: list[tuple[str, str]] = []

        def copy(self, local: str, remote: str) -> None:
            self.copy_targets.append(remote)
            super().copy(local, remote)

        def rename(self, src: str, dst: str) -> None:
            self.rename_calls.append((src, dst))
            src_n = self._norm(src)
            dst_n = self._norm(dst)
            if src_n not in self.files:
                raise FileNotFoundError(src)
            self.files[dst_n] = self.files.pop(src_n)

    dest = rf"{TEMP}\atomic-put.bin"
    store = _AtomicCopyClient()
    store.files[dest] = b"prior-good"
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"fresh-bytes")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert store.copy_targets
    assert all(t != dest for t in store.copy_targets)
    assert all(".mrc-tmp-" in t for t in store.copy_targets)
    assert store.rename_calls and store.rename_calls[0][1] == dest
    assert store.files.get(dest) == b"fresh-bytes"
    assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_native_put_mid_copy_failure_preserves_dest(tmp_path: Path) -> None:
    """ps_script_fs allowed: mid-copy failure leaves prior dest intact."""

    class _FailMidCopy(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.copy_targets: list[str] = []
            self.remove_calls: list[str] = []

        def copy(self, local: str, remote: str) -> None:
            self.copy_targets.append(remote)
            # Partial bytes on the temp only - dest must stay untouched.
            self.files[self._norm(remote)] = b"partial-corrupt"
            raise OSError("simulated native copy failure")

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            super().remove(path)

    dest = rf"{TEMP}\keep-put.bin"
    store = _FailMidCopy()
    store.files[dest] = b"only-prior-copy"
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"new-payload")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert store.files.get(dest) == b"only-prior-copy"
    assert store.copy_targets
    assert dest not in store.copy_targets
    assert all(".mrc-tmp-" in t for t in store.copy_targets)
    # Temp cleaned; dest never removed.
    assert dest not in store.remove_calls
    assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_native_put_short_copy_fails_or_reports_real_size(
    tmp_path: Path,
) -> None:
    """Native copy that writes fewer bytes than src must not report a lie.

    Promote is gated on dest size: a short remote file fails (or, if a
    backend reports success, ``bytes_transferred`` cannot exceed dest
    length). Prior dest is not replaced by the short copy.
    """

    class _ShortCopy(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.copy_targets: list[str] = []

        def copy(self, local: str, remote: str) -> None:
            del local
            self.copy_targets.append(remote)
            self.files[self._norm(remote)] = b"short"

    dest = rf"{TEMP}\short-put.bin"
    store = _ShortCopy()
    store.files[dest] = b"prior-good-copy"
    backend = WinrmFs(store, cwd=HOME, home=HOME)
    src = tmp_path / "payload.bin"
    src.write_bytes(b"full-source-payload")

    r = fs_ops.run(
        "put",
        ep="lab-win",
        path=dest,
        local=str(src),
        home=FIXTURES,
        backend=backend,
    )
    dest_len = len(store.files.get(dest, b""))
    if r.status == "ok":
        transferred = int(r.fields.get("bytes") or r.fields.get("bytes_transferred") or 0)
        assert transferred <= dest_len
        assert dest_len == len(b"short")
    else:
        assert r.status == "error"
        assert store.files.get(dest) == b"prior-good-copy"
        assert dest not in store.copy_targets
        assert not any(".mrc-tmp-" in k for k in store.files)


def test_winrm_put_late_copy_temp_is_swept_after_timeout(
    tmp_path: Path,
) -> None:
    """A temp an abandoned copy lands after the TIMEOUT is erased too.

    The copy is still streaming when the put's budget expires, so the payload
    appears on the host only after the caller was told the put failed. The
    cleanup's bounded sweep retries the remove inside its own budget and erases
    it; the prior destination stays intact. The event barrier pins the
    interleaving: the copy is released once the cleanup's first remove attempt
    has been issued.
    """

    class _LateCopyClient(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.copy_calls: list[str] = []
            self.remove_calls: list[str] = []
            self.copy_entered = threading.Event()
            self.copy_landed = threading.Event()
            self.copy_release = threading.Event()
            self.remove_entered = threading.Event()

        def copy(self, local: str, remote: str) -> None:
            self.copy_calls.append(remote)
            self.copy_entered.set()
            self.copy_release.wait(timeout=10.0)
            self.files[self._norm(remote)] = Path(local).read_bytes()
            self.copy_landed.set()

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)
            self.remove_entered.set()
            if not self.copy_landed.is_set():
                # The abandoned copy has not landed the temp yet, so this
                # delete finds nothing (Remove-Item fails on a missing path).
                # The payload appears only afterwards - a sweep is what
                # erases a temp nobody could see when the put gave up.
                raise FileNotFoundError(path)
            super().remove(path)

    store = _LateCopyClient()
    dest = rf"{TEMP}\late-put.bin"
    store.files[dest] = b"prior-good"
    op_budget = 0.3
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        timeout_s=2.0,
        op_timeout_s=op_budget,
    )
    backend._cleanup_timeout_s = 1.0  # noqa: SLF001
    src = tmp_path / "payload.bin"
    src.write_bytes(b"late-payload")
    errors: list[BaseException] = []

    def _put() -> None:
        try:
            backend.put(str(src), dest)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    worker = threading.Thread(target=_put, daemon=True)
    worker.start()
    assert store.copy_entered.wait(timeout=5.0), "native copy never started"
    # The barrier: the spent budget abandoned the copy and the cleanup is
    # already running, so release the copy and let its payload land now.
    cleanup_called = store.remove_entered.wait(timeout=op_budget + 2.0)
    store.copy_release.set()
    assert store.copy_landed.wait(timeout=5.0), "the late copy never landed"
    worker.join(timeout=10.0)

    assert not worker.is_alive(), "put did not return"
    assert errors and isinstance(errors[0], FsError)
    assert errors[0].code == "TIMEOUT"
    assert store.files.get(dest) == b"prior-good", "prior destination changed"
    assert not any(".mrc-tmp-" in k for k in store.files), (
        "a temp landed after the put reported TIMEOUT survived the cleanup"
    )
    assert cleanup_called, "the error-path cleanup issued no remote remove"
    assert store.copy_landed.is_set(), "the late copy never landed"
    assert len(store.remove_calls) >= 2, (
        "the cleanup must retry after the late temp landed"
    )


def test_winrm_fs_get_atomic_failure_preserves_local(tmp_path: Path) -> None:
    """get uses same-dir temp + os.replace; mid-fetch failure preserves dst."""

    class _FailingFetchClient(MockWinrmFileClient):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_targets: list[str] = []
            self.files[rf"{TEMP}\remote.bin"] = b"new-payload"

        def fetch(self, remote: str, local: str) -> None:
            self.fetch_targets.append(local)
            Path(local).write_bytes(b"partial-corrupt")
            raise OSError("simulated winrm fetch failure")

    client = _FailingFetchClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    dst = tmp_path / "downloaded.bin"
    dst.write_bytes(b"pre-existing-local-content")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=rf"{TEMP}\remote.bin",
        local=str(dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"pre-existing-local-content"
    assert client.fetch_targets
    assert client.fetch_targets[0] != str(dst)
    assert ".mrc-tmp-" in Path(client.fetch_targets[0]).name
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


def test_winrm_fs_get_atomic_failure_with_progress_preserves_local(
    tmp_path: Path,
) -> None:
    """Progress/chunked get branch is also atomic (temp + replace)."""

    class _FailingHandle:
        def read(self, n: int) -> bytes:
            raise OSError("simulated mid-stream read failure")

        def close(self) -> None:
            return None

    class _OpenFailClient(MockWinrmFileClient):
        def open(self, path: str, mode: str = "rb") -> _FailingHandle:
            return _FailingHandle()

    client = _OpenFailClient()
    remote = rf"{TEMP}\stream.bin"
    client.files[remote] = b"X" * 100
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    dst = tmp_path / "streamed.bin"
    dst.write_bytes(b"keep-me")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(dst),
        home=FIXTURES,
        backend=backend,
        progress=lambda d, t: None,
    )
    assert r.status == "error"
    assert dst.read_bytes() == b"keep-me"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


def test_winrm_fs_get_atomic_success_replaces_destination(tmp_path: Path) -> None:
    """Successful get replaces the local destination with remote bytes."""
    store, backend = _backend()
    remote = rf"{TEMP}\atom-get.bin"
    store.files[remote] = b"from-remote"
    dst = tmp_path / "atom-get.bin"
    dst.write_bytes(b"old-local")

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(dst),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert dst.read_bytes() == b"from-remote"
    temps = [f for f in os.listdir(tmp_path) if ".mrc-tmp-" in f]
    assert temps == []


def test_winrm_get_symlink_dst_follows_to_target(tmp_path: Path) -> None:
    """WinRM get to a local symlink dst updates the referent; link preserved.

    Covers the default (ps_script_fs allowed) path that uses temp + os.replace
    after native fetch / read. Mirrors local/SFTP symlink-dst policy.
    """
    store, backend = _backend()
    remote = rf"{TEMP}\symlink-get.bin"
    store.files[remote] = b"payload-data"
    target = tmp_path / "target.bin"
    target.write_bytes(b"old-target")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(link),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert os.path.islink(link), "local symlink inode must not be replaced"
    assert target.read_bytes() == b"payload-data"
    # Normal file dst unchanged behavior.
    plain = tmp_path / "plain.bin"
    r2 = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(plain),
        home=FIXTURES,
        backend=backend,
    )
    assert r2.status == "ok"
    assert plain.is_file() and not os.path.islink(plain)
    assert plain.read_bytes() == b"payload-data"


def test_winrm_get_symlink_dst_native_fetch_branch(tmp_path: Path) -> None:
    """Gated native-fetch branch also resolves local symlink before replace.

    When ``ps_script_fs=false``, get still uses temp + os.replace via
    ``_try_fetch``; that path must not replace the local link inode.
    """
    store = MockWinrmFileClient()
    remote = rf"{TEMP}\native-symlink-get.bin"
    store.files[remote] = b"native-payload"
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps={
            "ps_version": "5.1.19041",
            "language_mode": "ConstrainedLanguage",
            "ps_script_fs": False,
            "ps_oneshot": True,
            "ps_runspace": False,
        },
    )
    target = tmp_path / "native-target.bin"
    target.write_bytes(b"old")
    link = tmp_path / "native-link.bin"
    link.symlink_to(target)

    r = fs_ops.run(
        "get",
        ep="lab-win",
        path=remote,
        local=str(link),
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert os.path.islink(link)
    assert target.read_bytes() == b"native-payload"
