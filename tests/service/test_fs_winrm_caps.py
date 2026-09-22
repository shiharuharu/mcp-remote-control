"""Service tests: WinRM ps_script_fs capability gate vs native copy/fetch."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from _winrm_fakes import HOME, TEMP, FakePypsrpSession
from test_fs_winrm import MockWinrmFileClient, _MockAttrs, _connector_for

from mcp_remote_control.core import fs_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.fs.backends.winrm import PypsrpFileClient, WinrmFs
from mcp_remote_control.fs.types import FsError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# WinRM PS capability gate
# ---------------------------------------------------------------------------


def _constrained_ps_caps() -> dict:
    return {
        "ps_version": "5.1.19041",
        "language_mode": "ConstrainedLanguage",
        "ps_script_fs": False,
        "ps_oneshot": True,
        "ps_runspace": False,
    }


def _full_ps_caps() -> dict:
    return {
        "ps_version": "5.1.19041",
        "language_mode": "FullLanguage",
        "ps_script_fs": True,
        "ps_oneshot": True,
        "ps_runspace": True,
        "ps_edition": "Desktop",
    }


def test_winrm_fs_list_unsupported_when_ps_script_fs_false() -> None:
    """ConstrainedLanguage / ps_script_fs=false -> list UNSUPPORTED; no listdir."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\gate.txt"] = b"should-not-list"
    listdir_calls: list[str] = []
    orig = store.listdir

    def tracking_listdir(path: str) -> list[str]:
        listdir_calls.append(path)
        return orig(path)

    store.listdir = tracking_listdir  # type: ignore[method-assign]

    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps=_constrained_ps_caps(),
    )
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert "FullLanguage" in (r.fields.get("msg") or "") or "ps_script_fs" in (
        r.fields.get("msg") or ""
    )
    assert listdir_calls == [], "business Get-ChildItem path must not run"
    assert r.hint and "FullLanguage" in r.hint, "UNSUPPORTED must carry a hint"


def test_winrm_fs_list_ok_when_ps_script_fs_true() -> None:
    """FullLanguage + caps true -> fs list still works."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\ok.txt"] = b"x"
    backend = WinrmFs(
        store,
        cwd=HOME,
        home=HOME,
        ps_caps=_full_ps_caps(),
    )
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "ok.txt" in (r.body or "")


def test_winrm_fs_list_ok_when_winrm_ps_absent() -> None:
    """Absent winrm_ps (probe=False / legacy) -> fs not blocked."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\compat.txt"] = b"x"
    backend = WinrmFs(store, cwd=HOME, home=HOME, ps_caps=None)
    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        backend=backend,
    )
    assert r.status == "ok"
    assert "compat.txt" in (r.body or "")


def test_winrm_fs_list_ok_probe_false_skipped_marker() -> None:
    """Service path: open probe=False records ps_probe=skipped -> list not blocked."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\probe-skip.txt"] = b"x"
    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)
    assert ep.transport is not None
    meta = getattr(ep.transport, "meta", {}) or {}
    # probe=False marks the skip (gates stay permissive: no ps_oneshot/ps_script_fs).
    assert meta.get("winrm_ps") == {"ps_probe": "skipped"}
    assert ep.probe == {"ps_probe": "skipped"}

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert "probe-skip.txt" in (r.body or "")


def test_winrm_fs_list_unsupported_via_transport_meta() -> None:
    """Service path: transport.meta winrm_ps.ps_script_fs=false blocks list."""
    store = MockWinrmFileClient()
    store.files[rf"{TEMP}\blocked.txt"] = b"x"
    listdir_calls: list[str] = []
    orig = store.listdir

    def tracking_listdir(path: str) -> list[str]:
        listdir_calls.append(path)
        return orig(path)

    store.listdir = tracking_listdir  # type: ignore[method-assign]

    connector = _connector_for(store)
    reg = get_registry()
    reg.winrm_connector = connector  # type: ignore[assignment]
    ep = reg.open("lab-win", home=FIXTURES, connector=connector, probe=False)
    assert ep.transport is not None
    ep.transport.meta["winrm_ps"] = _constrained_ps_caps()

    r = fs_ops.run(
        "list",
        ep="lab-win",
        path=TEMP,
        home=FIXTURES,
        connector=connector,
        file_client=store,
    )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert listdir_calls == []


# ---------------------------------------------------------------------------
# Fix C: put/get native copy/fetch exemption must not run gated business PS
# when the client only has a PS-script fallback (no native copy/fetch).
# ---------------------------------------------------------------------------


def _winrm_fs_with_caps(sess: FakePypsrpSession, caps: dict) -> WinrmFs:
    return WinrmFs(PypsrpFileClient(sess), cwd=HOME, home=HOME, ps_caps=caps)


def test_winrm_fs_put_unsupported_when_script_only_no_native_copy(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + session without native copy -> put UNSUPPORTED;
    the write_file PS fallback must NOT run (zero business execute_ps)."""
    sess = FakePypsrpSession()  # has_copy=False -> copy falls back to write_file
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "UNSUPPORTED"
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_put_ok_native_copy_when_script_fs_blocked(tmp_path: Path) -> None:
    """ps_script_fs=false + native copy -> put succeeds via native copy; no PS."""
    sess = FakePypsrpSession(has_copy=True)
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"native-payload")
    remote = rf"{TEMP}\out.bin"
    r = backend.put(str(src), remote)
    assert r.bytes_transferred == len(b"native-payload")
    assert sess.files[remote] == b"native-payload"
    assert sess.copy_calls == [(str(src), remote)]
    assert not any("WriteAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_get_unsupported_when_script_only_no_native_fetch(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + session without native fetch -> get UNSUPPORTED;
    zero business execute_ps (no read_file PS fallback)."""
    sess = FakePypsrpSession()  # has_fetch=False
    sess.files[rf"{TEMP}\src.bin"] = b"remote-data"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    assert excinfo.value.code == "UNSUPPORTED"
    assert not any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_get_ok_native_fetch_when_script_fs_blocked(tmp_path: Path) -> None:
    """ps_script_fs=false + native fetch -> get succeeds via native fetch; no PS."""
    sess = FakePypsrpSession(has_fetch=True)
    sess.files[rf"{TEMP}\src.bin"] = b"remote-data"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    dst = tmp_path / "out.bin"
    r = backend.get(rf"{TEMP}\src.bin", str(dst))
    assert r.bytes_transferred == len(b"remote-data")
    assert dst.read_bytes() == b"remote-data"
    assert not any("ReadAllBytes" in s for s in sess.ps_calls)


def test_winrm_fs_put_progress_unsupported_when_script_fs_blocked(
    tmp_path: Path,
) -> None:
    """ps_script_fs=false + progress -> put UNSUPPORTED (progress path gated)."""
    sess = FakePypsrpSession(has_copy=True)
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin", progress=lambda _c, _t: None)
    assert excinfo.value.code == "UNSUPPORTED"


@pytest.mark.parametrize("op", ["read", "stat", "mkdir", "rm", "list"])
def test_winrm_fs_script_ops_unsupported_when_ps_script_fs_false(
    op: str, tmp_path: Path
) -> None:
    """All script FS ops gated under ps_script_fs=false -> UNSUPPORTED,
    zero business execute_ps (gate fires before any PS)."""
    sess = FakePypsrpSession()
    sess.files[rf"{TEMP}\gate.txt"] = b"x"
    backend = _winrm_fs_with_caps(sess, _constrained_ps_caps())
    n_before = len(sess.ps_calls)
    with pytest.raises(FsError) as excinfo:
        if op == "read":
            backend.read(rf"{TEMP}\gate.txt")
        elif op == "stat":
            backend.stat(rf"{TEMP}\gate.txt")
        elif op == "mkdir":
            backend.mkdir(rf"{TEMP}\sub")
        elif op == "rm":
            backend.rm(rf"{TEMP}\gate.txt")
        else:  # list
            backend.list(TEMP)
    assert excinfo.value.code == "UNSUPPORTED"
    assert len(sess.ps_calls) == n_before


# ---------------------------------------------------------------------------
# has_native_copy/fetch default False; gated native failure -> UNSUPPORTED
# ---------------------------------------------------------------------------


class _CopyFetchClientNoNativeFlag:
    """SupportsCopyFetch shape without has_native_* attributes.

    Used to prove missing flags default to False (not treated as native).
    """

    def __init__(self) -> None:
        self.copy_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {}
        self._dirs: set[str] = {"C:\\", TEMP, HOME}

    def stat(self, path: str) -> _MockAttrs:
        if path in self._dirs or path.rstrip("\\") in self._dirs:
            return _MockAttrs("dir", 0, 1.0, "Directory")
        if path in self.files:
            data = self.files[path]
            return _MockAttrs("file", len(data), 1.0, "Archive")
        raise FileNotFoundError(path)

    def mkdir(self, path: str) -> None:
        self._dirs.add(path)

    def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if max_bytes is not None:
            return data[: max(0, int(max_bytes))]
        return data

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))
        self.files[remote] = Path(local).read_bytes()

    def fetch(self, remote: str, local: str) -> None:
        self.fetch_calls.append((remote, local))
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])


class _NativeCopyFailsClient:
    """Claims native copy/fetch, but the native methods raise opaque errors."""

    has_native_copy = True
    has_native_fetch = True

    def __init__(
        self,
        *,
        copy_exc: BaseException | None = None,
        fetch_exc: BaseException | None = None,
    ) -> None:
        self._copy_exc = copy_exc or RuntimeError("native copy transport blip")
        self._fetch_exc = fetch_exc or RuntimeError("native fetch transport blip")

    def copy(self, local: str, remote: str) -> None:
        raise self._copy_exc

    def fetch(self, remote: str, local: str) -> None:
        raise self._fetch_exc


def test_try_copy_defaults_has_native_copy_false_when_missing(tmp_path: Path) -> None:
    """Missing has_native_copy must not be treated as native (default False)."""
    client = _CopyFetchClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    # Under the gate, no-native -> UNSUPPORTED; copy must not have been called.
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.copy_calls == []
    msg = excinfo.value.msg or ""
    assert "ConstrainedLanguage" in msg or "ps_script_fs" in msg


def test_try_fetch_defaults_has_native_fetch_false_when_missing(
    tmp_path: Path,
) -> None:
    """Missing has_native_fetch must not be treated as native (default False)."""
    client = _CopyFetchClientNoNativeFlag()
    client.files[rf"{TEMP}\src.bin"] = b"remote"
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.fetch_calls == []


def test_put_uses_write_file_when_native_flag_missing_and_scripts_ok(
    tmp_path: Path,
) -> None:
    """Default-false native flag still allows put via write_file when scripts ok."""
    client = _CopyFetchClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_full_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"via-write")
    remote = rf"{TEMP}\via-write.bin"
    r = backend.put(str(src), remote)
    assert r.bytes_transferred == len(b"via-write")
    assert client.copy_calls == [], "must not call unflagged copy"
    assert client.files[remote] == b"via-write"


def test_gated_native_copy_opaque_failure_is_unsupported(tmp_path: Path) -> None:
    """ps_script_fs=false + native copy raises opaque error -> UNSUPPORTED,
    not FS_ERROR (scripts cannot fall back; surface language_mode hint)."""
    client = _NativeCopyFailsClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"x")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    err = excinfo.value
    assert err.code == "UNSUPPORTED"
    assert err.details.get("ps_script_fs") is False
    assert err.details.get("language_mode") == "ConstrainedLanguage"
    assert "FullLanguage" in err.msg or "ps_script_fs" in err.msg


def test_gated_native_fetch_opaque_failure_is_unsupported(tmp_path: Path) -> None:
    """ps_script_fs=false + native fetch raises opaque error -> UNSUPPORTED."""
    client = _NativeCopyFailsClient()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    err = excinfo.value
    assert err.code == "UNSUPPORTED"
    assert err.details.get("language_mode") == "ConstrainedLanguage"


def test_gated_native_copy_not_found_stays_not_found(tmp_path: Path) -> None:
    """Specific path errors from native still surface (not remapped to gate)."""
    client = _NativeCopyFailsClient(copy_exc=FileNotFoundError("no such file"))
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"x")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# One-sided copy/fetch without has_native_* is not native
# ---------------------------------------------------------------------------


class _CopyOnlyClientNoNativeFlag:
    """Copy-only (misses SupportsCopyFetch). No has_native_* - not native."""

    def __init__(self) -> None:
        self.copy_calls: list[tuple[str, str]] = []

    def copy(self, local: str, remote: str) -> None:
        self.copy_calls.append((local, remote))


class _FetchOnlyClientNoNativeFlag:
    """Fetch-only (misses SupportsCopyFetch). No has_native_* - not native."""

    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, str]] = []

    def fetch(self, remote: str, local: str) -> None:
        self.fetch_calls.append((remote, local))


def test_copy_only_client_without_flag_is_unsupported(tmp_path: Path) -> None:
    """Bare copy() without has_native_copy must not be treated as native."""
    client = _CopyOnlyClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload")
    with pytest.raises(FsError) as excinfo:
        backend.put(str(src), rf"{TEMP}\out.bin")
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.copy_calls == []


def test_fetch_only_client_without_flag_is_unsupported(tmp_path: Path) -> None:
    """Bare fetch() without has_native_fetch must not be treated as native."""
    client = _FetchOnlyClientNoNativeFlag()
    backend = WinrmFs(client, cwd=HOME, home=HOME, ps_caps=_constrained_ps_caps())
    with pytest.raises(FsError) as excinfo:
        backend.get(rf"{TEMP}\src.bin", str(tmp_path / "out.bin"))
    assert excinfo.value.code == "UNSUPPORTED"
    assert client.fetch_calls == []
