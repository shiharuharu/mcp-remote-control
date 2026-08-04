"""Shared pypsrp fakes for WinRM/PSRP production-path tests.

Centralizes fakes used to exercise the real pypsrp code paths in
``fs/backends/winrm.py`` (``PypsrpFileClient``) and ``transport/winrm.py``
without a Windows host.

``FakePypsrpSession`` implements the oneshot ``execute_ps`` Protocol surface
(``environment=`` accepted) and interprets PS scripts against an in-memory FS.
Optional ``copy`` / ``fetch`` are bound as instance attributes so
``PypsrpFileClient`` can exercise both native-delegation and fallback paths.

Runspace fakes that carry test-coupled timeout / block logic remain in
``test_ps.py``. Transport ``open_runspace`` wraps pool-like handles in
``PypsrpPoolRunspaceAdapter`` (no ``invoke``) or ``InvokeRunspaceAdapter``
(has ``invoke``).
"""


from __future__ import annotations

import base64
import json
import re
from pathlib import Path

# Match the test_fs_winrm.py constants so the extracted fake behaves identically
# to O6's local ``_FakePypsrpSession``.
HOME = r"C:\Users\Administrator"
TEMP = r"C:\temp"


def _path_after(script: str, marker: str) -> str:
    """Extract the first single-quoted string occurring after *marker*.

    The PS scripts put ``$ErrorActionPreference='Stop'`` up front, so a naive
    first-quote scan grabs ``'Stop'``. Each path is preceded by a stable marker
    (``Get-Item -LiteralPath ``, ``[IO.File]::Open(``, ...) which we anchor on.
    """
    idx = script.find(marker)
    if idx < 0:
        return ""
    m = re.search(r"'([^']*)'", script[idx + len(marker):])
    return m.group(1) if m else ""


def _first_sq(script: str) -> str:
    """Extract the first PowerShell single-quoted string from *script*."""
    m = re.search(r"'([^']*)'", script)
    return m.group(1) if m else ""


class FakePypsrpSession:
    """Focused pypsrp session mock: interprets PS scripts against an in-memory FS.

    Paired with ``PypsrpFileClient`` to exercise the production adapter's
    PowerShell scripts (stat, listdir, list_with_attrs, bounded + unbounded read,
    write_file, mkdir, remove / rmdir / rmtree) without a real Windows host.
    Records every ``execute_ps`` call in ``ps_calls`` for round-trip counting.

    The optional ``copy`` / ``fetch`` methods (enabled via ``has_copy`` /
    ``has_fetch``) let ``PypsrpFileClient.copy`` / ``fetch`` be exercised both
    on the native-delegation path (session.copy / session.fetch present) and on
    the ``write_file`` / ``read_file`` fallback path (absent). When enabled,
    calls are recorded in ``copy_calls`` / ``fetch_calls``.

    The gate is implemented by binding ``copy`` / ``fetch`` as INSTANCE
    attributes in ``__init__`` only when the corresponding flag is set; the class
    itself does NOT define ``copy`` / ``fetch`` methods, so
    ``getattr(sess, "copy", None)`` returns ``None`` when the flag is False —
    which is exactly what ``PypsrpFileClient.copy`` probes to decide fallback.
    """

    MTIME = "2024-01-01T00:00:00Z"

    def __init__(self, *, has_copy: bool = False, has_fetch: bool = False) -> None:
        self.dirs: set[str] = {"C:\\", TEMP, HOME}
        self.files: dict[str, bytes] = {}
        self.ps_calls: list[str] = []
        self.copy_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        if has_copy:
            # Bind an instance attribute (a bound method is callable) so the
            # ``getattr(sess, "copy", None)`` probe in PypsrpFileClient.copy
            # finds a callable and delegates instead of falling back.
            self.copy = self._do_copy
        if has_fetch:
            self.fetch = self._do_fetch

    # ------------------------------------------------------------------
    # pypsrp Client surface
    # ------------------------------------------------------------------

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> str:
        del environment  # Protocol surface; FS path does not use env.
        self.ps_calls.append(script)
        # stat: Get-Item -LiteralPath + PSIsContainer
        if "Get-Item -LiteralPath" in script and "PSIsContainer" in script:
            return self._stat_json(_path_after(script, "Get-Item -LiteralPath "))
        # list_with_attrs: Get-ChildItem + name=$_.Name
        if "Get-ChildItem" in script and "name=$_.Name" in script:
            return self._list_attrs_json(
                _path_after(script, "Get-ChildItem -LiteralPath ")
            )
        # listdir: Get-ChildItem + ForEach-Object { $_.Name }
        if "Get-ChildItem" in script and "ForEach-Object { $_.Name }" in script:
            return self._listdir_json(
                _path_after(script, "Get-ChildItem -LiteralPath ")
            )
        # bounded read: [IO.File]::Open
        if "[IO.File]::Open(" in script:
            m = re.search(r"\$maxN = (\d+)", script)
            n = int(m.group(1)) if m else 0
            return self._read_b64(_path_after(script, "[IO.File]::Open("), n)
        # unbounded read: [IO.File]::ReadAllBytes
        if "[IO.File]::ReadAllBytes(" in script:
            return self._read_b64(
                _path_after(script, "[IO.File]::ReadAllBytes("), None
            )
        # write_file: [IO.File]::WriteAllBytes
        if "[IO.File]::WriteAllBytes(" in script:
            path = _path_after(script, "[IO.File]::WriteAllBytes(")
            m = re.search(r"FromBase64String\('([^']*)'\)", script)
            self.files[path] = base64.b64decode(m.group(1)) if m else b""
            return ""
        # mkdir: New-Item -ItemType Directory
        if "New-Item -ItemType Directory" in script:
            self.dirs.add(_path_after(script, "New-Item -ItemType Directory -Path "))
            return ""
        # remove / rmdir / rmtree: Remove-Item -LiteralPath
        if "Remove-Item -LiteralPath" in script:
            path = _path_after(script, "Remove-Item -LiteralPath ")
            if "-Recurse" in script:
                # rmtree: real ``Remove-Item -Recurse`` drops the dir AND all
                # descendants sharing the ``path\`` prefix (PS semantics).
                prefix = path.rstrip("\\") + "\\"
                for d in list(self.dirs):
                    if d == path or d.startswith(prefix):
                        self.dirs.discard(d)
                for f in list(self.files):
                    if f == path or f.startswith(prefix):
                        self.files.pop(f, None)
            else:
                # remove / rmdir: just the exact path.
                self.dirs.discard(path)
                self.files.pop(path, None)
            return ""
        return ""

    # ------------------------------------------------------------------
    # Optional native copy / fetch delegation (pypsrp Client.copy / .fetch)
    # ------------------------------------------------------------------

    def _do_copy(self, local: str, remote: str) -> None:
        """Simulate server-side streaming copy (pypsrp Client.copy)."""
        self.copy_calls.append((local, remote))
        self.files[remote] = Path(local).read_bytes()

    def _do_fetch(self, remote: str, local: str) -> None:
        """Simulate server-side streaming fetch (pypsrp Client.fetch)."""
        self.fetch_calls.append((remote, local))
        data = self.files.get(remote, b"")
        Path(local).write_bytes(data)

    # ------------------------------------------------------------------
    # PS interpreter helpers
    # ------------------------------------------------------------------

    def _stat_json(self, path: str) -> str:
        if path in self.dirs:
            return json.dumps(
                {"kind": "dir", "size": 0, "mtime": self.MTIME, "mode": "Directory"}
            )
        if path in self.files:
            data = self.files[path]
            return json.dumps(
                {
                    "kind": "file",
                    "size": len(data),
                    "mtime": self.MTIME,
                    "mode": "Archive",
                }
            )
        return '{"error":"NOT_FOUND"}'

    def _children(self, path: str) -> list[tuple[str, str, int]] | None:
        if path not in self.dirs:
            return None
        prefix = path.rstrip("\\") + "\\"
        out: list[tuple[str, str, int]] = []
        for d in self.dirs:
            if d == path:
                continue
            if d.startswith(prefix):
                rest = d[len(prefix):]
                if rest and "\\" not in rest:
                    out.append((rest, "dir", 0))
        for f, data in self.files.items():
            if f.startswith(prefix):
                rest = f[len(prefix):]
                if rest and "\\" not in rest:
                    out.append((rest, "file", len(data)))
        return out

    def _list_attrs_json(self, path: str) -> str:
        kids = self._children(path)
        if kids is None:
            return '{"error":"NOT_FOUND"}'
        if not kids:
            return "[]"
        items = [
            {
                "name": n,
                "kind": k,
                "size": s,
                "mtime": self.MTIME,
                "mode": "Directory" if k == "dir" else "Archive",
            }
            for (n, k, s) in kids
        ]
        # Mimic PS ConvertTo-Json: single-element array unwraps to an object.
        return json.dumps(items[0]) if len(items) == 1 else json.dumps(items)

    def _listdir_json(self, path: str) -> str:
        kids = self._children(path)
        if kids is None:
            return '{"error":"NOT_FOUND"}'
        if not kids:
            return "[]"
        names = [n for (n, _k, _s) in kids]
        # Mimic PS ConvertTo-Json: single name unwraps to a JSON string.
        return json.dumps(names[0]) if len(names) == 1 else json.dumps(names)

    def _read_b64(self, path: str, n: int | None) -> str:
        if path not in self.files:
            return "NOT_FOUND"
        data = self.files[path]
        if n is not None:
            data = data[:n]
        return base64.b64encode(data).decode("ascii")