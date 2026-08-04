"""WinRM filesystem backend over the ``WinRMFileClient`` Protocol.

Agent-facing API remains ``fs_*``; ``via=winrm`` is optional meta only.
Recursive list guards against junction/reparse cycles (visited set + depth
cap). Reads always request bounded transfer via ``read_file(..., max_bytes=)``
on the Protocol surface (``PypsrpFileClient`` and mocks implement it).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mcp_remote_control.fs.types import (
    DEFAULT_READ_MAX_BYTES,
    DEFAULT_TRANSFER_CHUNK,
    FsError,
    ListEntry,
    ListResult,
    ProgressCallback,
    ReadResult,
    StatInfo,
    TransferResult,
    WriteResult,
    detect_text,
    report_progress,
)
from mcp_remote_control.transport.protocols import (
    SupportsCopyFetch,
    SupportsFileOpen,
    SupportsListWithAttrs,
    SupportsRmtree,
    WinRMFileClient,
)

# Factory: () -> file client (sync object).
WinrmFsFactory = Callable[[], WinRMFileClient | Any]


def _coerce_bytes(data: object) -> bytes:
    """Normalize duck-typed client read results to ``bytes``."""
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    return bytes(cast(Any, data))


def _coerce_str_list(raw: object) -> list[str]:
    """Normalize duck-typed client listdir results to ``list[str]``."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return [str(x) for x in cast(Any, raw)]


def _mtime_iso(ts: float | str | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        text = ts.strip()
        if not text:
            return None
        # Already ISO-ish from PowerShell.
        if "T" in text or text.endswith("Z"):
            return text.replace("+00:00", "Z")
        try:
            ts = float(text)
        except ValueError:
            return text
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _is_abs_win(path: str) -> bool:
    text = path.strip()
    if not text:
        return False
    # Drive-absolute: C:\… or C:/…
    if len(text) >= 2 and text[1] == ":":
        return True
    # UNC
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return False


def _win_sep_join(base: str, name: str) -> str:
    base = base.rstrip("\\/")
    name = name.lstrip("\\/")
    if not base:
        return name
    return base + "\\" + name


def _norm_win_path(path: str) -> str:
    """Normalize Windows path separators; keep drive/UNC shape."""
    text = str(path).strip()
    if not text:
        return text
    # Preserve UNC prefix.
    unc = text.startswith("\\\\") or text.startswith("//")
    text = text.replace("/", "\\")
    while "\\\\" in text[2:] if unc else "\\\\" in text:
        if unc:
            text = "\\\\" + text[2:].replace("\\\\", "\\")
        else:
            text = text.replace("\\\\", "\\")
    # Strip trailing slash except roots: C:\ or \\server\share
    if len(text) > 3 and text.endswith("\\"):
        # C:\ stays; C:\foo\ → C:\foo
        if not (len(text) == 3 and text[1] == ":"):
            text = text.rstrip("\\")
    # Drive root without slash: C: → C:\
    if len(text) == 2 and text[1] == ":":
        text = text + "\\"
    return text


def _entry_kind(kind: str) -> str:
    return {"dir": "d", "file": "f", "link": "l"}.get(kind, "o")


def _kind_from_attrs(attrs: Any) -> str:
    if attrs is None:
        return "file"
    if isinstance(attrs, dict):
        k = attrs.get("kind") or attrs.get("type") or attrs.get("Type")
        if k is not None:
            return _normalize_kind(str(k))
    k = getattr(attrs, "kind", None)
    if k is None:
        k = getattr(attrs, "type", None)
    if k is not None:
        return _normalize_kind(str(k))
    # POSIX-ish mode bits if present
    mode = getattr(attrs, "mode", None)
    if mode is None:
        mode = getattr(attrs, "st_mode", None)
    if mode is not None and isinstance(mode, int):
        import stat as statmod

        if statmod.S_ISDIR(mode):
            return "dir"
        if statmod.S_ISLNK(mode):
            return "link"
        if statmod.S_ISREG(mode):
            return "file"
    if getattr(attrs, "is_dir", None) is True or getattr(attrs, "isdir", None) is True:
        return "dir"
    return "file"


def _normalize_kind(raw: str) -> str:
    low = raw.strip().lower()
    if low in {"d", "dir", "directory", "container"}:
        return "dir"
    if low in {"f", "file", "reg", "regular"}:
        return "file"
    if low in {"l", "link", "symlink", "junction"}:
        return "link"
    return "other"


def _size_of(attrs: Any) -> int:
    if isinstance(attrs, dict):
        size = attrs.get("size", attrs.get("Length", 0))
        return int(size) if size is not None else 0
    size = getattr(attrs, "size", None)
    if size is None:
        size = getattr(attrs, "st_size", None)
    if size is None:
        size = getattr(attrs, "Length", None)
    return int(size) if size is not None else 0


def _mode_of(attrs: Any) -> str | None:
    if isinstance(attrs, dict):
        mode = attrs.get("mode") or attrs.get("Attributes")
        return str(mode) if mode is not None else None
    mode = getattr(attrs, "mode", None)
    if mode is None:
        mode = getattr(attrs, "attributes", None)
    if mode is None:
        return None
    if isinstance(mode, int):
        return format(mode & 0o7777, "04o")
    return str(mode)


def _mtime_of(attrs: Any) -> str | None:
    if isinstance(attrs, dict):
        return _mtime_iso(attrs.get("mtime") or attrs.get("LastWriteTimeUtc"))
    mtime = getattr(attrs, "mtime", None)
    if mtime is None:
        mtime = getattr(attrs, "st_mtime", None)
    return _mtime_iso(mtime)


def _map_fs_error(exc: BaseException, path: str) -> FsError:
    if isinstance(exc, FsError):
        return exc
    name = type(exc).__name__
    text = str(exc).strip() or name
    text = " ".join(text.split())
    low = text.lower()
    code = "FS_ERROR"
    if (
        "no such file" in low
        or "not found" in low
        or "cannot find path" in low
        or "does not exist" in low
        or name in {"FileNotFoundError", "ItemNotFoundException"}
    ):
        code = "NOT_FOUND"
        text = f"path not found: {path}"
    elif "permission" in low or "access is denied" in low or name in {
        "PermissionError",
        "UnauthorizedAccessException",
    }:
        code = "PERMISSION_DENIED"
        text = f"permission denied: {path}"
    elif "not a directory" in low:
        code = "NOT_A_DIR"
    elif "is a directory" in low or "is a container" in low:
        code = "IS_A_DIR"
    if len(text) > 200:
        text = text[:197] + "..."
    return FsError(code, text, details={"path": path})


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class WinrmFs:
    """Filesystem ops over a :class:`WinRMFileClient` (or ``PypsrpFileClient``).

    Parameters
    ----------
    client:
        Connected file client, or ``None`` when using *factory*.
    factory:
        Callable returning a client when *client* is ``None``. Enables lazy
        open without requiring a live session at construction time.
    cwd / home:
        Used to absolutize relative remote paths in results.
    ps_caps:
        Optional ``transport.meta["winrm_ps"]`` capability dict. When present
        and ``ps_script_fs`` is ``False``, script-based FS ops raise
        ``FsError("UNSUPPORTED")``. Missing dict or ``probe_skipped`` keeps
        legacy allow (lab / ``probe=False`` compatibility).
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        factory: WinrmFsFactory | None = None,
        cwd: str | None = None,
        home: str | None = None,
        ps_caps: dict[str, Any] | None = None,
    ) -> None:
        if client is None and factory is None:
            raise ValueError("WinrmFs requires client= or factory=")
        self._client = client
        self._factory = factory
        self._cwd = cwd
        self._home = home
        self._ps_caps = ps_caps

    @property
    def via(self) -> str:
        return "winrm"

    def _fs(self) -> Any:
        if self._client is not None:
            return self._client
        assert self._factory is not None
        self._client = self._factory()
        return self._client

    def _ps_script_fs_allowed(self) -> bool:
        """Return True when script FS may run (or probe was skipped / absent)."""
        caps = self._ps_caps
        if caps is None:
            # No winrm_ps meta → legacy allow (probe=False / older transports).
            return True
        if caps.get("probe_skipped") is True:
            return True
        if caps.get("ps_probe") == "skipped":
            return True
        # Only block when the probe explicitly derived False.
        return caps.get("ps_script_fs") is not False

    def _require_ps_script_fs(self) -> None:
        """Raise UNSUPPORTED when probe says PowerShell script FS is unavailable.

        Call at the start of public ops that depend on Get-Item / File IO /
        ConvertTo-Json (list, stat, read, write, mkdir, rm, and script put/get).
        """
        if self._ps_script_fs_allowed():
            return
        caps = self._ps_caps or {}
        lang = caps.get("language_mode") or caps.get("lang_mode") or "unknown"
        raise FsError(
            "UNSUPPORTED",
            (
                "WinRM script filesystem requires FullLanguage "
                f"(language_mode={lang}); host reports ps_script_fs=false"
            ),
            details={
                "language_mode": lang,
                "ps_script_fs": False,
            },
        )

    def _reraise_gated_native_failure(self, exc: BaseException, path: str) -> None:
        """Prefer UNSUPPORTED when native transfer fails and script FS is gated.

        Specific path errors (NOT_FOUND, PERMISSION_DENIED, …) are kept.
        Opaque ``FS_ERROR`` is replaced by the capability gate message so the
        Agent sees language_mode / ``ps_script_fs`` rather than a bare native
        exception — script write/read is not available as a fallback.
        """
        if isinstance(exc, FsError):
            if exc.code != "FS_ERROR":
                raise exc
            self._require_ps_script_fs()
        mapped = _map_fs_error(exc, path)
        if mapped.code != "FS_ERROR":
            raise mapped from exc
        self._require_ps_script_fs()

    def resolve_path(self, path: str) -> str:
        """Return an absolute-looking remote path for Windows output semantics."""
        text = str(path).strip()
        if not text:
            raise FsError("INVALID_ARG", "path is empty")
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            home = self._home or ""
            if home:
                rest = text[1:].lstrip("\\/")
                text = home if not rest else _win_sep_join(home, rest)
            elif text == "~":
                text = "C:\\"
            else:
                text = text[2:]
        if _is_abs_win(text):
            return _norm_win_path(text)
        base = self._cwd or self._home or "C:\\"
        return _norm_win_path(_win_sep_join(base, text))

    def list(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> ListResult:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            dir_attrs = self._stat(client, abs_path)
            if _kind_from_attrs(dir_attrs) != "dir":
                raise FsError(
                    "NOT_A_DIR",
                    f"not a directory: {abs_path}",
                    details={"path": abs_path},
                )
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc

        entries: list[ListEntry] = []
        # "." reuses the dir attrs already fetched above (no second stat RT).
        entries.append(
            ListEntry(
                name=".",
                kind="d",
                size=0,
                mode=_mode_of(dir_attrs),
                mtime=_mtime_of(dir_attrs),
                path=abs_path,
            )
        )

        # ".." requires statting the parent (1 RT, unavoidable).
        parent = _parent_win(abs_path)
        if parent and parent != abs_path:
            try:
                st_par = self._stat(client, parent)
                entries.append(
                    ListEntry(
                        name="..",
                        kind="d",
                        size=0,
                        mode=_mode_of(st_par),
                        mtime=_mtime_of(st_par),
                        path=parent,
                    )
                )
            except Exception:  # noqa: BLE001
                entries.append(ListEntry(name="..", kind="d", size=0, path=parent))

        # Children: prefer a single batched attrs round-trip when the client
        # supports it; fall back to per-name stat (N RTs) otherwise.
        if isinstance(client, SupportsListWithAttrs):
            try:
                child_attrs = client.list_with_attrs(abs_path)
            except FsError:
                raise
            except Exception:  # noqa: BLE001 — best-effort; fall back to listdir
                child_attrs = None
            if child_attrs is not None:
                child_entries: list[ListEntry] = []
                for c in child_attrs:
                    if not isinstance(c, dict):
                        continue
                    name = str(c.get("name") or c.get("Name") or "")
                    if not name or name in {".", ".."}:
                        continue
                    kind = _kind_from_attrs(c)
                    child_entries.append(
                        ListEntry(
                            name=name,
                            kind=_entry_kind(kind),
                            size=0 if kind == "dir" else _size_of(c),
                            mode=_mode_of(c),
                            mtime=_mtime_of(c),
                            path=_win_sep_join(abs_path, name),
                        )
                    )
                # Stable sorted-by-name ordering (matches the per-stat path).
                child_entries.sort(key=lambda e: e.name)
                entries.extend(child_entries)
            else:
                self._list_children_by_stat(client, abs_path, entries)
        else:
            try:
                names = self._listdir(client, abs_path)
            except FsError:
                raise
            except Exception as exc:
                raise _map_fs_error(exc, abs_path) from exc
            self._append_children_from_names(client, abs_path, names, entries)

        if recursive:
            subdirs = [e for e in entries if e.kind == "d" and e.name not in {".", ".."}]
            # Cycle protection: a junction/reparse-point reported by
            # list_with_attrs as kind="dir" and pointing to an ancestor (or
            # reappearing one level deeper each time) would recurse forever.
            # *visited* catches same-path cycles immediately; *depth* is a
            # backstop for the ever-growing-path shape (a junction back to an
            # ancestor produces a new, longer path at each level, so a
            # visited-set alone never matches). Cap at 40 levels — enough for
            # any realistic tree, still terminates a pathological cycle.
            self._collect_recursive(
                abs_path,
                subdirs,
                entries,
                visited={abs_path},
                depth=0,
            )

        return ListResult(path=abs_path, entries=entries)

    def _collect_recursive(
        self,
        top: str,
        subdirs: list[ListEntry],
        entries: list[ListEntry],
        *,
        visited: set[str],
        depth: int,
        max_depth: int = 40,
    ) -> None:
        """Append descendants of *subdirs* to *entries*, named relative to *top*.

        Reuses single-level ``list`` per subdir. Descendant names come from
        absolute ``se.path`` relative to *top* via ``_rel_name_under`` so
        depth ≥3 and prefix-overlapping basenames stay correct. DFS — each
        subdir's full subtree before the next sibling. Cycle protection is
        documented on ``list``.
        """
        for e in subdirs:
            sub_path = e.path or _win_sep_join(top, e.name)
            if sub_path in visited or depth >= max_depth:
                continue
            visited.add(sub_path)
            sub = self.list(sub_path, recursive=False)
            child_dirs: list[ListEntry] = []
            for se in sub.entries:
                if se.name in {".", ".."}:
                    continue
                # ListEntry.path is optional; fall back like *sub_path* above so
                # _rel_name_under always receives a concrete Windows path.
                se_abs = se.path or _win_sep_join(sub_path, se.name)
                se_name = _rel_name_under(top, se_abs)
                if not se_name:
                    se_name = se.name
                rel = ListEntry(
                    name=se_name,
                    kind=se.kind,
                    size=se.size,
                    mode=se.mode,
                    mtime=se.mtime,
                    path=se_abs,
                )
                entries.append(rel)
                if se.kind == "d":
                    child_dirs.append(rel)
            if child_dirs:
                self._collect_recursive(
                    top,
                    child_dirs,
                    entries,
                    visited=visited,
                    depth=depth + 1,
                    max_depth=max_depth,
                )

    def _list_children_by_stat(
        self,
        client: Any,
        abs_path: str,
        entries: list[ListEntry],
    ) -> None:
        try:
            names = self._listdir(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        self._append_children_from_names(client, abs_path, names, entries)

    def _append_children_from_names(
        self,
        client: Any,
        abs_path: str,
        names: list[str],
        entries: list[ListEntry],
    ) -> None:
        for name in sorted(names):
            if name in {".", ".."}:
                continue
            child = _win_sep_join(abs_path, name)
            try:
                st = self._stat(client, child)
                kind = _kind_from_attrs(st)
                entries.append(
                    ListEntry(
                        name=name,
                        kind=_entry_kind(kind),
                        size=0 if kind == "dir" else _size_of(st),
                        mode=_mode_of(st),
                        mtime=_mtime_of(st),
                        path=child,
                    )
                )
            except Exception:  # noqa: BLE001
                entries.append(ListEntry(name=name, kind="o", path=child))

    def stat(self, path: str) -> StatInfo:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            attrs = self._stat(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        kind = _kind_from_attrs(attrs)
        return StatInfo(
            path=abs_path,
            kind=kind,
            size=0 if kind == "dir" else _size_of(attrs),
            mode=_mode_of(attrs),
            mtime=_mtime_of(attrs),
        )

    def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> ReadResult:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        limit = DEFAULT_READ_MAX_BYTES if max_bytes is None else int(max_bytes)
        if limit < 0:
            limit = DEFAULT_READ_MAX_BYTES
        try:
            info = self.stat(abs_path)
            if info.kind == "dir":
                raise FsError(
                    "IS_A_DIR",
                    f"is a directory: {abs_path}",
                    details={"path": abs_path},
                )
            data = self._read_bytes(client, abs_path, limit + 1)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        truncated = len(data) > limit
        if truncated:
            data = data[:limit]
        is_text, encoding = detect_text(data)
        return ReadResult(
            path=abs_path,
            data=data,
            truncated=truncated,
            encoding=encoding if is_text else None,
            is_text=is_text,
        )

    def write(
        self,
        path: str,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
    ) -> WriteResult:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        raw = content.encode(encoding) if isinstance(content, str) else content
        created = True
        try:
            try:
                st = self._stat(client, abs_path)
                if _kind_from_attrs(st) == "dir":
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory: {abs_path}",
                        details={"path": abs_path},
                    )
                created = False
            except FsError as exc:
                if exc.code == "IS_A_DIR":
                    raise
                created = True
            except Exception:  # noqa: BLE001
                created = True
            parent = _parent_win(abs_path)
            if parent:
                self._mkdir_p(client, parent)
            self._write_bytes(client, abs_path, raw)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        return WriteResult(path=abs_path, bytes_written=len(raw), created=created)

    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        src = Path(str(local_path)).expanduser()
        if not src.is_absolute():
            src = (Path.cwd() / src).resolve()
        else:
            src = src.resolve()
        if not src.is_file():
            raise FsError(
                "NOT_FOUND",
                f"local path not found or not a file: {src}",
                details={"path": str(src)},
            )
        abs_remote = self.resolve_path(remote_path)
        client = self._fs()
        # When script FS is blocked, still attempt native copy (no PS mkdir/write).
        # Progress and write_file paths need script FS → UNSUPPORTED with hint.
        if not self._ps_script_fs_allowed():
            if progress is not None:
                self._require_ps_script_fs()
            try:
                if self._try_copy(client, str(src), abs_remote):
                    size = int(src.stat().st_size)
                    return TransferResult(
                        path=abs_remote,
                        local=str(src),
                        bytes_transferred=int(size),
                        direction="put",
                    )
            except Exception as exc:  # noqa: BLE001 — native client surface
                self._reraise_gated_native_failure(exc, abs_remote)
            self._require_ps_script_fs()
        try:
            parent = _parent_win(abs_remote)
            if parent:
                self._mkdir_p(client, parent)
            size = int(src.stat().st_size)
            if progress is not None:
                size = self._put_with_progress(client, src, abs_remote, size, progress)
            elif self._try_copy(client, str(src), abs_remote):
                pass
            else:
                data = src.read_bytes()
                self._write_bytes(client, abs_remote, data)
                size = len(data)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_remote) from exc
        return TransferResult(
            path=abs_remote,
            local=str(src),
            bytes_transferred=int(size),
            direction="put",
        )

    def get(
        self,
        remote_path: str,
        local_path: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        abs_remote = self.resolve_path(remote_path)
        dst = Path(str(local_path)).expanduser()
        if not dst.is_absolute():
            dst = (Path.cwd() / dst).resolve()
        client = self._fs()
        # When script FS is blocked, still attempt native fetch (no PS stat/read).
        # Progress and read_file paths need script FS → UNSUPPORTED with hint.
        if not self._ps_script_fs_allowed():
            if progress is not None:
                self._require_ps_script_fs()
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = (
                dst.parent
                / f".{dst.name}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                if self._try_fetch(client, abs_remote, str(tmp)):
                    size = tmp.stat().st_size if tmp.is_file() else 0
                    os.replace(tmp, dst)
                    return TransferResult(
                        path=abs_remote,
                        local=str(dst),
                        bytes_transferred=int(size),
                        direction="get",
                    )
            except Exception as exc:  # noqa: BLE001 — native client surface
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._reraise_gated_native_failure(exc, abs_remote)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._require_ps_script_fs()
        try:
            info = self.stat(abs_remote)
            if info.kind == "dir":
                raise FsError(
                    "IS_A_DIR",
                    f"is a directory: {abs_remote}",
                    details={"path": abs_remote},
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            total = int(info.size)
            # Download to a local temp in dst.parent, then os.replace over dst.
            # Mid-get failure removes the temp and leaves any prior dst intact.
            tmp = (
                dst.parent
                / f".{dst.name}.mrc-tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                if progress is not None:
                    size = self._get_with_progress(
                        client, abs_remote, tmp, total, progress
                    )
                elif self._try_fetch(client, abs_remote, str(tmp)):
                    size = tmp.stat().st_size if tmp.is_file() else info.size
                else:
                    data = self._read_bytes(client, abs_remote, None)
                    tmp.write_bytes(data)
                    size = len(data)
                os.replace(tmp, dst)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_remote) from exc
        return TransferResult(
            path=abs_remote,
            local=str(dst),
            bytes_transferred=int(size),
            direction="get",
        )

    def mkdir(self, path: str, *, parents: bool = True) -> StatInfo:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            if parents:
                self._mkdir_p(client, abs_path)
            else:
                self._mkdir(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            try:
                info = self.stat(abs_path)
                if info.kind == "dir":
                    return info
            except FsError:
                pass
            raise _map_fs_error(exc, abs_path) from exc
        return self.stat(abs_path)

    def rm(self, path: str, *, recursive: bool = False) -> str:
        self._require_ps_script_fs()
        abs_path = self.resolve_path(path)
        client = self._fs()
        try:
            info = self.stat(abs_path)
            if info.kind == "dir":
                if not recursive:
                    raise FsError(
                        "IS_A_DIR",
                        f"is a directory (use recursive): {abs_path}",
                        details={"path": abs_path},
                    )
                self._rmtree(client, abs_path)
            else:
                self._remove(client, abs_path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, abs_path) from exc
        return abs_path

    # ------------------------------------------------------------------
    # progress-aware transfer helpers
    # ------------------------------------------------------------------

    def _put_with_progress(
        self,
        client: Any,
        src: Path,
        abs_remote: str,
        total: int,
        progress: ProgressCallback,
    ) -> int:
        """Upload with progress; prefer chunked open/write, else whole-file."""
        report_progress(progress, 0, total)
        if isinstance(client, SupportsFileOpen):
            fh = client.open(abs_remote, "wb")
            done = 0
            closed = False
            try:
                write = getattr(fh, "write", None)
                if not callable(write):
                    raise FsError("UNSUPPORTED", "winrm file has no write")
                with src.open("rb") as fsrc:
                    while True:
                        chunk = fsrc.read(DEFAULT_TRANSFER_CHUNK)
                        if not chunk:
                            break
                        write(chunk)
                        done += len(chunk)
                        report_progress(progress, done, total)
            except Exception:
                # Best-effort cleanup of the partial remote file. The first
                # write already overwrote any prior good file, so leaving a
                # truncated artifact on failure would be misleading. Close
                # the handle BEFORE remove: on Windows deleting an open file
                # fails ("file in use"). Cleanup may still fail (e.g. broken
                # connection); swallow so the original error surfaces.
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                closed = True
                try:
                    self._remove(client, abs_remote)
                except Exception:  # noqa: BLE001
                    pass
                raise
            finally:
                # Success path: close after the write loop. Error path already
                # closed-then-removed and sets closed to avoid double-close.
                if not closed:
                    close = getattr(fh, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            pass
            if done != total:
                report_progress(progress, done, total if total else done)
            return done

        # Whole-file path (copy/write_file): report start + complete.
        # Prefer copy so the source path is streamed without loading the whole
        # file into memory; only read bytes when write_file is needed.
        if self._try_copy(client, str(src), abs_remote):
            report_progress(progress, total, total)
            return total
        data = src.read_bytes()
        self._write_bytes(client, abs_remote, data)
        report_progress(progress, len(data), total if total else len(data))
        return len(data)

    def _get_with_progress(
        self,
        client: Any,
        abs_remote: str,
        dst: Path,
        total: int,
        progress: ProgressCallback,
    ) -> int:
        report_progress(progress, 0, total if total else None)
        if isinstance(client, SupportsFileOpen):
            fh = client.open(abs_remote, "rb")
            done = 0
            try:
                read = getattr(fh, "read", None)
                if not callable(read):
                    raise FsError("UNSUPPORTED", "winrm file has no read")
                with dst.open("wb") as fdst:
                    while True:
                        chunk = _coerce_bytes(read(DEFAULT_TRANSFER_CHUNK))
                        if not chunk:
                            break
                        fdst.write(chunk)
                        done += len(chunk)
                        report_progress(
                            progress, done, total if total else None
                        )
            finally:
                close = getattr(fh, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            if total and done != total:
                report_progress(progress, done, total)
            elif not total:
                report_progress(progress, done, done)
            return done

        if self._try_fetch(client, abs_remote, str(dst)):
            size = dst.stat().st_size if dst.is_file() else total
        else:
            data = self._read_bytes(client, abs_remote, None)
            dst.write_bytes(data)
            size = len(data)
        report_progress(progress, int(size), total if total else int(size))
        return int(size)

    # ------------------------------------------------------------------
    # low-level client adapters (Protocol surface)
    # ------------------------------------------------------------------

    def _stat(self, client: Any, path: str) -> Any:
        try:
            return client.stat(path)
        except FsError:
            raise
        except Exception as exc:
            raise _map_fs_error(exc, path) from exc

    def _listdir(self, client: Any, path: str) -> list[str]:
        return _coerce_str_list(client.listdir(path))

    def _read_bytes(self, client: Any, path: str, max_n: int | None) -> bytes:
        # Protocol: read_file always accepts max_bytes (None = unbounded).
        return _coerce_bytes(client.read_file(path, max_bytes=max_n))

    def _write_bytes(self, client: Any, path: str, data: bytes) -> None:
        client.write_file(path, data)

    def _try_copy(self, client: Any, local: str, remote: str) -> bool:
        if isinstance(client, SupportsCopyFetch):
            # Opt-in only: missing has_native_copy defaults to False (custom
            # SupportsCopyFetch clients must set True). A PS-script fallback
            # (e.g. PypsrpFileClient without session.copy) is not native and
            # must not bypass the ps_script_fs gate.
            if getattr(client, "has_native_copy", False) is False:
                return False
            client.copy(local, remote)
            return True
        # Optional single-method copy without fetch (mock convenience).
        copy_fn = getattr(client, "copy", None)
        if callable(copy_fn):
            copy_fn(local, remote)
            return True
        return False

    def _try_fetch(self, client: Any, remote: str, local: str) -> bool:
        if isinstance(client, SupportsCopyFetch):
            if getattr(client, "has_native_fetch", False) is False:
                return False
            client.fetch(remote, local)
            return True
        fetch_fn = getattr(client, "fetch", None)
        if callable(fetch_fn):
            fetch_fn(remote, local)
            return True
        return False

    def _mkdir(self, client: Any, path: str) -> None:
        client.mkdir(path)

    def _mkdir_p(self, client: Any, path: str) -> None:
        if not path:
            return
        norm = _norm_win_path(path)
        # Drive root always "exists".
        if len(norm) == 3 and norm[1] == ":" and norm[2] == "\\":
            return
        try:
            attrs = self._stat(client, norm)
            kind = _kind_from_attrs(attrs)
            if kind == "dir":
                return
            raise FsError(
                "ALREADY_EXISTS",
                f"path exists and is not a directory: {norm}",
                details={"path": norm},
            )
        except FsError as exc:
            if exc.code != "NOT_FOUND":
                raise
        parent = _parent_win(norm)
        if parent and parent != norm:
            self._mkdir_p(client, parent)
        try:
            self._mkdir(client, norm)
        except Exception as exc:
            try:
                attrs = self._stat(client, norm)
                if _kind_from_attrs(attrs) == "dir":
                    return
            except Exception:  # noqa: BLE001
                pass
            raise _map_fs_error(exc, norm) from exc

    def _remove(self, client: Any, path: str) -> None:
        client.remove(path)

    def _rmdir(self, client: Any, path: str) -> None:
        client.rmdir(path)

    def _rmtree(self, client: Any, path: str) -> None:
        if isinstance(client, SupportsRmtree):
            client.rmtree(path)
            return
        names = self._listdir(client, path)
        for name in names:
            if name in {".", ".."}:
                continue
            child = _win_sep_join(path, name)
            try:
                st = self._stat(client, child)
                if _kind_from_attrs(st) == "dir":
                    self._rmtree(client, child)
                else:
                    self._remove(client, child)
            except FsError:
                try:
                    self._remove(client, child)
                except Exception:  # noqa: BLE001
                    self._rmdir(client, child)
        self._rmdir(client, path)



def _rel_name_under(base: str, path: str) -> str:
    """Return *path* relative to *base* using Windows separators.

    Used by recursive ``list`` so descendant names come from absolute paths
    rather than per-level basename heuristics (which break on prefix overlap).
    When *path* is not under *base*, returns the normalized path (defensive;
    normal recursion always stays under *base*).
    """
    nb = _norm_win_path(base).rstrip("\\")
    np_ = _norm_win_path(path)
    if not nb or not np_:
        return ""
    prefix = nb + "\\"
    if np_.startswith(prefix):
        return np_[len(prefix):]
    if np_ == nb:
        return ""
    return np_


def _parent_win(path: str) -> str | None:
    norm = _norm_win_path(path)
    if not norm:
        return None
    # Drive root
    if len(norm) == 3 and norm[1] == ":" and norm[2] == "\\":
        return norm
    if norm.startswith("\\\\"):
        parts = [p for p in norm.split("\\") if p]
        if len(parts) <= 2:
            return norm
        return "\\\\" + "\\".join(parts[:-1])
    parent = norm.rsplit("\\", 1)[0]
    if len(parent) == 2 and parent[1] == ":":
        return parent + "\\"
    return parent or None


class PypsrpFileClient:
    """Adapter: pypsrp-compatible session → :class:`WinRMFileClient` for ``WinrmFs``.

    Uses ``copy``/``fetch`` for put/get when present and PowerShell oneshots
    for the rest. Implements the stable file Protocol so ``WinrmFs`` needs
    no multi-name method soup.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def _execute_ps(self, script: str) -> str:
        execute_ps = getattr(self._session, "execute_ps", None)
        if not callable(execute_ps):
            raise FsError("UNSUPPORTED", "winrm session has no execute_ps")
        # Protocol-stable call: always pass environment= (None here).
        try:
            raw = execute_ps(script, environment=None)
        except TypeError:
            # Last-resort for third-party shapes that reject the kwarg.
            raw = execute_ps(script)
        if isinstance(raw, tuple) and raw:
            return str(raw[0] or "")
        if isinstance(raw, str):
            return raw
        stdout = getattr(raw, "stdout", None)
        if stdout is not None:
            return str(stdout or "")
        return str(raw or "")

    def _run_json(self, script: str, path: str) -> Any:
        out = self._execute_ps(script).strip()
        # Strip BOM / noise lines; take last JSON object/array.
        if not out:
            raise FsError("FS_ERROR", f"empty ps output for path: {path}", details={"path": path})
        # Prefer last line that looks like JSON.
        candidate = out
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                candidate = line
                break
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            low = out.lower()
            if "not found" in low or "cannot find" in low or "does not exist" in low:
                raise FsError("NOT_FOUND", f"path not found: {path}", details={"path": path}) from exc
            raise FsError(
                "FS_ERROR",
                f"invalid ps json for path: {path}",
                details={"path": path},
            ) from exc

    def stat(self, path: str) -> dict[str, Any]:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ $p = Get-Item -LiteralPath {q} -Force; "
            f"$kind = if ($p.PSIsContainer) {{ 'dir' }} else {{ 'file' }}; "
            f"$size = if ($p.PSIsContainer) {{ 0 }} else {{ [int64]$p.Length }}; "
            f"$mtime = $p.LastWriteTimeUtc.ToString('o'); "
            f"@{{ kind=$kind; size=$size; mtime=$mtime; mode=$p.Attributes.ToString() }} "
            f"| ConvertTo-Json -Compress "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        return data if isinstance(data, dict) else {"kind": "file", "size": 0}

    def listdir(self, path: str) -> list[str]:
        q = _ps_single_quote(path)
        # Always emit a JSON array: for an empty existing dir the pipeline is
        # empty and ConvertTo-Json would emit nothing, which _run_json treats
        # as FS_ERROR. Emit '[]' explicitly when Count is 0.
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ "
            f"$names = @(Get-ChildItem -LiteralPath {q} -Force | ForEach-Object {{ $_.Name }}); "
            f"if ($names.Count -eq 0) {{ Write-Output '[]' }} "
            f"else {{ $names | ConvertTo-Json -Compress }} "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        if data is None:
            return []
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, str):
            return [data]
        return []

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        q = _ps_single_quote(path)
        if max_bytes is not None:
            # Bounded read: open a FileStream and read at most max_bytes so a
            # large file only transfers ~K bytes instead of base64-ing the
            # whole file and truncating locally.
            n = int(max_bytes)
            n = max(n, 0)
            # Stream Read up to max_bytes. Do not cast $fs.Length to Int32 —
            # files larger than 2 GiB would fail before any bytes are returned.
            # FileStream.Read stops at EOF, so Length is unnecessary.
            script = (
                f"$ErrorActionPreference='Stop'; "
                f"$maxN = {n}; "
                f"try {{ "
                f"$fs = [IO.File]::Open({q}, [IO.FileMode]::Open, "
                f"[IO.FileAccess]::Read, [IO.FileShare]::Read); "
                f"try {{ "
                f"$buf = New-Object byte[] $maxN; "
                f"$offset = 0; "
                f"while ($offset -lt $maxN) {{ "
                f"$r = $fs.Read($buf, $offset, $maxN - $offset); "
                f"if ($r -le 0) {{ break }}; "
                f"$offset += $r "
                f"}}; "
                f"if ($offset -lt $maxN) {{ "
                f"$final = New-Object byte[] $offset; "
                f"[Array]::Copy($buf, $final, $offset); "
                f"$buf = $final "
                f"}}; "
                f"[Convert]::ToBase64String($buf) "
                f"}} finally {{ $fs.Close() }} "
                f"}} catch {{ "
                f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
                f"Write-Output 'NOT_FOUND' }} else {{ throw }} }}"
            )
        else:
            script = (
                f"$ErrorActionPreference='Stop'; "
                f"try {{ "
                f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({q})) "
                f"}} catch {{ "
                f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
                f"Write-Output 'NOT_FOUND' }} else {{ throw }} }}"
            )
        out = self._execute_ps(script).strip()
        if out == "NOT_FOUND" or out.endswith("\nNOT_FOUND"):
            raise FileNotFoundError(path)
        # Take last non-empty line as base64 payload.
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            return b""
        b64 = lines[-1]
        if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", b64):
            low = out.lower()
            if "not found" in low or "cannot find" in low:
                raise FileNotFoundError(path)
            raise FsError("FS_ERROR", f"invalid base64 read for: {path}", details={"path": path})
        return base64.b64decode(b64)

    def list_with_attrs(self, path: str) -> list[dict[str, Any]]:
        """Return name/kind/size/mtime/mode per child in one PowerShell round-trip.

        Lets ``WinrmFs.list`` avoid N per-child stat calls. Empty existing dir
        → ``[]``. Callers fall back to listdir + per-name stat when absent.
        """
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"try {{ "
            f"$items = @(Get-ChildItem -LiteralPath {q} -Force | ForEach-Object {{ "
            f"$kind = if ($_.PSIsContainer) {{ 'dir' }} else {{ 'file' }}; "
            f"$size = if ($_.PSIsContainer) {{ 0 }} else {{ [int64]$_.Length }}; "
            f"@{{ name=$_.Name; kind=$kind; size=$size; "
            f"mtime=$_.LastWriteTimeUtc.ToString('o'); "
            f"mode=$_.Attributes.ToString() }} "
            f"}}); "
            f"if ($items.Count -eq 0) {{ Write-Output '[]' }} "
            f"else {{ $items | ConvertTo-Json -Compress }} "
            f"}} catch {{ "
            f"if ($_.Exception.Message -match 'not find|does not exist|NotFound') {{ "
            f"Write-Output '{{\"error\":\"NOT_FOUND\"}}' }} else {{ throw }} }}"
        )
        data = self._run_json(script, path)
        if isinstance(data, dict) and data.get("error") == "NOT_FOUND":
            raise FileNotFoundError(path)
        if data is None:
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def write_file(self, path: str, data: bytes) -> None:
        q = _ps_single_quote(path)
        b64 = base64.b64encode(data).decode("ascii")
        # Embed base64 in a single-quoted PS string (no quotes inside b64).
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"$bytes = [Convert]::FromBase64String('{b64}'); "
            f"[IO.File]::WriteAllBytes({q}, $bytes)"
        )
        self._execute_ps(script)

    def mkdir(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"New-Item -ItemType Directory -Path {q} -Force | Out-Null"
        )
        self._execute_ps(script)

    def remove(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Force"
        )
        self._execute_ps(script)

    def rmdir(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Force"
        )
        self._execute_ps(script)

    def rmtree(self, path: str) -> None:
        q = _ps_single_quote(path)
        script = (
            f"$ErrorActionPreference='Stop'; "
            f"Remove-Item -LiteralPath {q} -Recurse -Force"
        )
        self._execute_ps(script)

    def copy(self, local: str, remote: str) -> None:
        fn = getattr(self._session, "copy", None)
        if not callable(fn):
            data = Path(local).read_bytes()
            self.write_file(remote, data)
            return
        fn(local, remote)

    def fetch(self, remote: str, local: str) -> None:
        fn = getattr(self._session, "fetch", None)
        if not callable(fn):
            data = self.read_file(remote)
            Path(local).write_bytes(data)
            return
        fn(remote, local)

    @property
    def has_native_copy(self) -> bool:
        """True only when the wrapped session provides a real ``copy`` callable.

        The PS ``write_file`` fallback is NOT native — running it under
        ``ps_script_fs=false`` would execute the gated business scripts, so
        put/get's native exemption must not apply when only the fallback exists.
        """
        return callable(getattr(self._session, "copy", None))

    @property
    def has_native_fetch(self) -> bool:
        """True only when the wrapped session provides a real ``fetch`` callable."""
        return callable(getattr(self._session, "fetch", None))
