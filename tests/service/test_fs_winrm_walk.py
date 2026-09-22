"""Service tests: WinRM recursive list and rmtree fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from _winrm_fakes import HOME, TEMP, FakePypsrpSession, _path_after
from test_fs_winrm import _MockAttrs, _backend

from mcp_remote_control.fs.backends.winrm import (
    _MAX_RECURSE_DEPTH,
    PypsrpFileClient,
    WinrmFs,
)
from mcp_remote_control.fs.types import FsError, ListEntry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def test_winrm_fs_recursive_list_depth_and_prefix_overlap() -> None:
    """Recursive list names are correct at depth >=3 and when a child's
    basename starts with its parent's basename."""
    store, backend = _backend()
    # Depth-4 chain under TEMP\foo: foo\bar\baz\qux.txt
    for d in (rf"{TEMP}\foo", rf"{TEMP}\foo\bar", rf"{TEMP}\foo\bar\baz"):
        store.dirs.add(d)
    deep = rf"{TEMP}\foo\bar\baz\qux.txt"
    store.files[deep] = b"leaf"
    # Prefix-overlap direct child: foobar.txt under foo (starts with "foo").
    overlap = rf"{TEMP}\foo\foobar.txt"
    store.files[overlap] = b"ovr"

    res = backend.list(TEMP, recursive=True)
    by_path = {e.path: e for e in res.entries}
    # Depth-4 entry: name is the full relative path from TEMP, not truncated.
    e_deep = by_path[deep]
    assert e_deep.name == r"foo\bar\baz\qux.txt"
    assert e_deep.path == deep
    # Prefix-overlap entry: keeps the "foo\" prefix.
    e_ovr = by_path[overlap]
    assert e_ovr.name == r"foo\foobar.txt"
    assert e_ovr.path == overlap


def test_winrm_fs_list_uses_batched_attrs_no_per_child_stat() -> None:
    """List with N entries makes O(1) child round-trips; `.` reuses dir attrs."""
    sess = FakePypsrpSession()
    parent = rf"{TEMP}\batch"
    sess.dirs.add(parent)
    # N children (mix of files and dirs).
    for i in range(5):
        sess.files[rf"{parent}\f{i}.txt"] = b"x" * i
    sess.dirs.add(rf"{parent}\sub")

    client = PypsrpFileClient(sess)
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    sess.ps_calls.clear()
    res = backend.list(parent)

    # Expect exactly 3 PS round-trips: stat(dir), list_with_attrs(dir),
    # stat(parent-of-dir for `..`). No per-child stat.
    assert len(sess.ps_calls) == 3
    child_scripts = [s for s in sess.ps_calls if "name=$_.Name" in s]
    assert len(child_scripts) == 1, "list_with_attrs should fire exactly once"
    # `.` reuses the dir stat: the stat script targeting `parent` runs once.
    dir_stat_scripts = [
        s
        for s in sess.ps_calls
        if "Get-Item -LiteralPath" in s
        and _path_after(s, "Get-Item -LiteralPath ") == parent
    ]
    assert len(dir_stat_scripts) == 1, "dir stat must not be repeated for `.`"
    # All children present with correct kinds/sizes.
    by_name = {e.name: e for e in res.entries}
    assert by_name["sub"].kind == "d"
    assert by_name["f3.txt"].kind == "f"
    assert by_name["f3.txt"].size == 3


def test_winrm_fs_recursive_list_terminates_on_junction_cycle() -> None:
    """A junction reported by list_with_attrs as kind='dir' and
    pointing back through itself (each level reappears one path deeper) must
    terminate with DEPTH_EXCEEDED - not hang, RecursionError, or silent ok
    partial tree. Aligned with SFTP recursive-list depth policy."""

    class _CycleClient:
        """Every dir contains a 'loop' child dir (kind='dir'), modeling a
        junction that reappears through itself: listing <path> yields a child
        at <path>\\loop, so the recursion produces an ever-growing path that
        a visited-set alone never matches - only the depth backstop catches
        it. Without protection the recursion is unbounded (RecursionError)."""

        def stat(self, path: str) -> _MockAttrs:
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def list_with_attrs(self, path: str) -> list[dict[str, object]]:
            return [
                {
                    "name": "loop",
                    "kind": "dir",
                    "size": 0,
                    "mtime": "2024-01-01T00:00:00Z",
                    "mode": "Directory",
                }
            ]

    client = _CycleClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    # Must raise DEPTH_EXCEEDED at the depth cap rather than hang,
    # RecursionError, or return success with an incomplete tree.
    with pytest.raises(FsError) as ei:
        backend.list(TEMP, recursive=True)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth" in err.msg
    assert str(_MAX_RECURSE_DEPTH) in err.msg
    assert err.details.get("max_depth") == _MAX_RECURSE_DEPTH
    assert err.details.get("path")  # path of the overflowing node


def test_winrm_fs_recursive_list_depth_cap_raises_not_silent_ok() -> None:
    """Depth cap is an explicit incompleteness signal (DEPTH_EXCEEDED),
    never silent ListResult(truncated=False) with a partial tree. Uses a small
    max_depth so the test stays fast."""
    store, backend = _backend()
    # Chain deep enough to exceed max_depth=2: TEMP\a\b\c
    for d in (rf"{TEMP}\a", rf"{TEMP}\a\b", rf"{TEMP}\a\b\c"):
        store.dirs.add(d)
    store.files[rf"{TEMP}\a\b\c\leaf.txt"] = b"x"

    top = TEMP
    # Seed like list() recursive branch: top-level subdirs already collected.
    entries: list[ListEntry] = [
        ListEntry(name="a", kind="d", size=0, path=rf"{TEMP}\a"),
    ]
    with pytest.raises(FsError) as ei:
        backend._collect_recursive(
            top,
            list(entries),
            entries,
            visited={top},
            depth=0,
            max_depth=2,
        )
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth (2)" in err.msg
    assert err.details.get("max_depth") == 2
    assert err.details.get("path")
    # Partial progress may have been collected before the raise - that is
    # fine; the signal is the error, not a false-success ListResult.


# ---------------------------------------------------------------------------
# rmtree fallback cycle / depth guards (non-SupportsRmtree clients)
# ---------------------------------------------------------------------------


def test_winrm_rmtree_fallback_depth_exceeded_raises() -> None:
    """Fallback _rmtree on an ever-growing dir cycle raises DEPTH_EXCEEDED
    instead of RecursionError / hang. Client has no SupportsRmtree."""

    class _CycleNoRmtreeClient:
        """Every dir contains a 'loop' child dir - models a junction that
        reappears one path deeper each level (visited-set alone never matches).
        No rmtree method -> WinrmFs uses listdir/stat fallback."""

        def listdir(self, path: str) -> list[str]:
            return ["loop"]

        def stat(self, path: str) -> _MockAttrs:
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            return None

        def rmdir(self, path: str) -> None:
            return None

    client = _CycleNoRmtreeClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.rm(TEMP, recursive=True)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth" in err.msg
    assert err.details.get("max_depth") == _MAX_RECURSE_DEPTH
    assert err.details.get("path")


def test_winrm_rmtree_fallback_max_depth_fast_path() -> None:
    """Depth cap is explicit (DEPTH_EXCEEDED) with path/max_depth details;
    use max_depth=2 so the test stays fast."""

    class _CycleNoRmtreeClient:
        def listdir(self, path: str) -> list[str]:
            return ["loop"]

        def stat(self, path: str) -> _MockAttrs:
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            return None

        def rmdir(self, path: str) -> None:
            return None

    client = _CycleNoRmtreeClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend._rmtree(client, TEMP, max_depth=2)
    err = ei.value
    assert err.code == "DEPTH_EXCEEDED"
    assert "maximum recursion depth (2)" in err.msg
    assert err.details.get("max_depth") == 2
    assert err.details.get("path")


def test_winrm_rmtree_fallback_same_path_cycle_terminates() -> None:
    """Same-path re-entry is skipped via *visited* (no hang/loop).

    Models a junction child already present in *visited* (re-entry of an
    ancestor path). Fallback must skip that child and finish the rest of
    the tree.
    """

    class _SelfRefJunctionClient:
        def __init__(self) -> None:
            self.rmdir_calls: list[str] = []
            self.remove_calls: list[str] = []
            self.listdir_on: list[str] = []

        def listdir(self, path: str) -> list[str]:
            self.listdir_on.append(path)
            p = path.replace("/", "\\").rstrip("\\")
            if p == TEMP.rstrip("\\"):
                return ["self", "leaf.txt"]
            return []

        def stat(self, path: str) -> _MockAttrs:
            p = path.replace("/", "\\")
            if p.endswith("leaf.txt"):
                return _MockAttrs("file", 1, 1.0, "Archive")
            # Junction reported as dir (common WinRM list/stat shape).
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            self.remove_calls.append(path)

        def rmdir(self, path: str) -> None:
            self.rmdir_calls.append(path)

    client = _SelfRefJunctionClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    junc = rf"{TEMP}\self"
    # Pre-seed visited with the junction path -> re-entry is a no-op skip.
    backend._rmtree(client, TEMP, visited={junc})
    assert any(c.endswith("leaf.txt") for c in client.remove_calls)
    assert any(c.rstrip("\\") == TEMP.rstrip("\\") for c in client.rmdir_calls)
    # Junction was already visited: never listed / rmdir'd under fallback.
    assert junc not in client.listdir_on
    assert junc not in client.rmdir_calls


def test_winrm_rmtree_fallback_self_ref_link_removed_as_leaf() -> None:
    """Reparse/symlink children reported as non-dir are unlinked as leaves
    (not followed), so a self-ref link cannot loop the fallback."""

    class _LinkLeafClient:
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.rmdired: list[str] = []

        def listdir(self, path: str) -> list[str]:
            p = path.replace("/", "\\").rstrip("\\")
            if p == TEMP.rstrip("\\"):
                return ["self", "sub", "a.txt"]
            if p == rf"{TEMP}\sub".rstrip("\\"):
                return ["up", "b.txt"]
            return []

        def stat(self, path: str) -> _MockAttrs:
            p = path.replace("/", "\\")
            if p.endswith(".txt"):
                return _MockAttrs("file", 1, 1.0, "Archive")
            if p.endswith("\\self") or p.endswith("\\up"):
                return _MockAttrs("link", 0, 1.0, "ReparsePoint")
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            self.removed.append(path)

        def rmdir(self, path: str) -> None:
            self.rmdired.append(path)

    client = _LinkLeafClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    result = backend.rm(TEMP, recursive=True)
    assert result == TEMP
    # Links removed as leaves, not descended.
    assert any(c.endswith("\\self") for c in client.removed)
    assert any(c.endswith("\\up") for c in client.removed)
    assert any(c.endswith("a.txt") for c in client.removed)
    assert any(c.endswith("b.txt") for c in client.removed)
    assert any(c.replace("/", "\\").rstrip("\\") == rf"{TEMP}\sub" for c in client.rmdired)
    assert any(c.replace("/", "\\").rstrip("\\") == TEMP.rstrip("\\") for c in client.rmdired)


def test_winrm_rmtree_supports_rmtree_uses_native() -> None:
    """SupportsRmtree clients still use native client.rmtree(path);
    fallback listdir/stat is not used."""

    class _NativeRmtreeClient:
        def __init__(self) -> None:
            self.rmtree_calls: list[str] = []
            self.listdir_calls: list[str] = []

        def rmtree(self, path: str) -> None:
            self.rmtree_calls.append(path)

        def listdir(self, path: str) -> list[str]:
            self.listdir_calls.append(path)
            raise AssertionError("fallback listdir must not run for SupportsRmtree")

        def stat(self, path: str) -> _MockAttrs:
            # rm() stats the target first to decide file vs dir.
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            raise AssertionError("fallback remove must not run for SupportsRmtree")

        def rmdir(self, path: str) -> None:
            raise AssertionError("fallback rmdir must not run for SupportsRmtree")

    client = _NativeRmtreeClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    result = backend.rm(TEMP, recursive=True)
    assert result == TEMP
    assert client.rmtree_calls == [TEMP]
    assert client.listdir_calls == []


def test_winrm_rmtree_fallback_child_stat_timeout_raises() -> None:
    """Fallback _rmtree re-raises child _stat TIMEOUT (partial delete is ok)."""

    class _TimeoutChild:
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.rmdired: list[str] = []

        def listdir(self, path: str) -> list[str]:
            p = path.replace("/", "\\").rstrip("\\")
            if p == TEMP.rstrip("\\"):
                return ["a.txt", "hang-dir"]
            return []

        def stat(self, path: str) -> _MockAttrs:
            p = path.replace("/", "\\")
            if p.endswith("hang-dir"):
                raise FsError(
                    "TIMEOUT",
                    "winrm fs operation timed out after 0.2s",
                    details={"path": path, "timeout_s": 0.2},
                )
            if p.endswith(".txt"):
                return _MockAttrs("file", 1, 1.0, "Archive")
            return _MockAttrs("dir", 0, 1.0, "Directory")

        def remove(self, path: str) -> None:
            self.removed.append(path)

        def rmdir(self, path: str) -> None:
            self.rmdired.append(path)

    client = _TimeoutChild()
    backend = WinrmFs(client, cwd=HOME, home=HOME)
    with pytest.raises(FsError) as ei:
        backend.rm(TEMP, recursive=True)
    assert ei.value.code == "TIMEOUT"
    assert any(c.endswith("a.txt") for c in client.removed)
    assert not any(
        c.replace("/", "\\").rstrip("\\") == TEMP.rstrip("\\") for c in client.rmdired
    )


def test_winrm_rmtree_fallback_shallow_tree_unchanged() -> None:
    """Normal shallow trees on non-SupportsRmtree clients still fully
    delete via the guarded fallback (regression)."""
    store, backend = _backend()
    # MockWinrmFileClient has no rmtree -> fallback path.
    assert not hasattr(store, "rmtree")
    root = rf"{TEMP}\tree"
    store.dirs.add(root)
    store.dirs.add(rf"{root}\sub")
    store.files[rf"{root}\a.txt"] = b"1"
    store.files[rf"{root}\sub\b.txt"] = b"22"
    result = backend.rm(root, recursive=True)
    assert result == root
    assert not store._exists_dir(root)
    assert not store._exists_dir(rf"{root}\sub")
    assert rf"{root}\a.txt" not in store.files
    assert rf"{root}\sub\b.txt" not in store.files
