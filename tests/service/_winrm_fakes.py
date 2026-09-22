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
from collections.abc import Callable
from pathlib import Path

# Match the test_fs_winrm.py constants so the extracted fake behaves identically
# to the previous local ``_FakePypsrpSession``.
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


# Guarded ``catch`` statements the emitted write/promote scripts can contain,
# as one alternation so each is matched at the position it was emitted and the
# compound patterns consume the plain temp-cleanup shape nested inside them.
_CATCH_STATEMENT_RE = re.compile(
    # Restore the parked prior content at a destination that is gone. The
    # emitted guard names ``$parked``; a script without that prefix restores
    # whenever the filesystem says so, which is the shape that resurrects a
    # stale backup on a failure that never touched the destination.
    r"if \((\$parked -and )?\(-not \(Test-Path -LiteralPath '([^']*)'\)\) -and "
    r"\(Test-Path -LiteralPath '([^']*)'\)\) \{\s*"
    r"Move-Item -LiteralPath '([^']*)' -Destination '([^']*)'"
    # Drop a backup the destination no longer needs.
    r"|if \(\(Test-Path -LiteralPath '([^']*)'\) -and "
    r"\(Test-Path -LiteralPath '([^']*)'\)\) \{\s*"
    r"Remove-Item -LiteralPath '([^']*)'"
    # Temp cleanup (also the shape scripts without a replace primitive emit).
    r"|Test-Path -LiteralPath '([^']*)'\)\s*\{\s*"
    r"Remove-Item -LiteralPath '([^']*)'"
)


class _ErrorStreams:
    """Minimal pypsrp ``PSDataStreams`` stand-in (``error`` list)."""

    def __init__(self, errors: list[str]) -> None:
        self.error = list(errors)


class FakePypsrpSession:
    """Focused pypsrp session mock: interprets PS scripts against an in-memory FS.

    Paired with ``PypsrpFileClient`` to exercise the production adapter's
    PowerShell scripts (stat, listdir, list_with_attrs, bounded + unbounded
    read, write_file, mkdir, remove / rmdir / rmtree) without a real Windows
    host, recording every ``execute_ps`` call in ``ps_calls``.

    The interpreter models the Windows semantics those scripts rely on rather
    than assuming they succeed: ``Move-Item -Destination <existing dir>`` moves
    the item *inside* that directory (a directory container), the write
    script's ``catch`` block runs against the paths it actually names, so a
    cleanup aimed at the wrong path or missing its ``Test-Path`` guard shows up
    in the FS model, and reparse points (``links``) report ``kind=link`` with a
    target. ``fail_write_at`` injects a remote failure at a named stage of the
    atomic write, reported the way pypsrp does:
    ``(output, streams, had_errors=True)``; :meth:`_run_promote_script` models
    the emitted promote's phases.

    The optional ``copy`` / ``fetch`` methods are bound as INSTANCE attributes
    in ``__init__`` only when ``has_copy`` / ``has_fetch`` is set; the class
    itself does NOT define them, so ``getattr(sess, "copy", None)`` returns
    ``None`` when the flag is False - which is exactly what
    ``PypsrpFileClient.copy`` probes to decide fallback. Calls land in
    ``copy_calls`` / ``fetch_calls``.
    """

    MTIME = "2024-01-01T00:00:00Z"

    def __init__(
        self,
        *,
        has_copy: bool = False,
        has_fetch: bool = False,
        fail_write_at: str | None = None,
    ) -> None:
        self.dirs: set[str] = {"C:\\", TEMP, HOME}
        self.files: dict[str, bytes] = {}
        # Final-component reparse points: link path -> target path. Move/stat
        # treat a link as its own entry; resolving it is the caller's job.
        self.links: dict[str, str] = {}
        # Subset of ``links`` that are directory reparse points (junctions /
        # symlinks to a directory): Windows reports them ``Directory,
        # ReparsePoint`` and ``Move-Item -Destination`` treats them as
        # containers, so a promote onto one lands inside its referent.
        self.dir_links: set[str] = set()
        self.ps_calls: list[str] = []
        self.copy_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        # Atomic-write failure injection: None | "write" | "promote".
        self.fail_write_at = fail_write_at
        # Optional hook fired between the emitted promote's destination probe
        # and its move, so a test can pin "a file appeared at the destination
        # after the probe" deterministically instead of racing the host.
        self.before_promote_move: Callable[[], None] | None = None
        # Set when an injected promote failure fired *after* the destination's
        # entry was already removed (parked at the replace backup, or deleted
        # by the force-overwrite branch): the boundary the emitted command
        # really has, rather than a failure before the promote started.
        self.promote_removed_target = False
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
    ) -> str | tuple[str, _ErrorStreams, bool]:
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
        # promote: standalone replace/move script (PypsrpFileClient.rename) -
        # the temp is already on the host, so no payload travels with this
        # script. This must be tested before the Remove-Item branch below: the
        # promote script's catch block names Remove-Item too.
        if "Move-Item -LiteralPath" in script and "WriteAllBytes" not in script:
            return self._run_promote_script(script)
        # write_file: [IO.File]::WriteAllBytes - same-dir temp + promote
        if "[IO.File]::WriteAllBytes(" in script:
            path = _path_after(script, "[IO.File]::WriteAllBytes(")
            m = re.search(r"FromBase64String\('([^']*)'\)", script)
            data = base64.b64decode(m.group(1)) if m else b""
            if self.fail_write_at == "write":
                # WriteAllBytes itself failed: no temp was ever created, then
                # the script's catch ran (Test-Path finds nothing).
                self._run_script_catch(script)
                return self._write_failure("write", path)
            self.files[path] = data
            return self._run_promote_script(script)
        # readlink: Get-Item + ReparsePoint attribute + target
        if "NOT_A_LINK" in script:
            return self._readlink_json(_path_after(script, "Get-Item -LiteralPath "))
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
                for lnk in list(self.links):
                    if lnk == path or lnk.startswith(prefix):
                        self.links.pop(lnk, None)
                        self.dir_links.discard(lnk)
            else:
                # remove / rmdir: just the exact path (file, link, or dir).
                self.dirs.discard(path)
                self.files.pop(path, None)
                self.links.pop(path, None)
                self.dir_links.discard(path)
            return ""
        return ""

    # ------------------------------------------------------------------
    # Write-script semantics: Move-Item container rule + catch cleanup
    # ------------------------------------------------------------------

    def _move_item(self, src: str, dest: str) -> None:
        """Apply ``Move-Item -LiteralPath <src> -Destination <dest>``.

        Real semantics: an existing *directory* destination - including a
        directory reparse point, which Windows sees through for the container
        test - is a container, so the item lands inside it under its source
        name; otherwise the destination path itself is replaced. Links are not
        followed here - callers resolve file reparse points before promoting.
        """
        data = self.files.pop(src, None)
        if data is None:
            return
        container = self._container_dir(dest)
        if container is not None:
            self.files[container.rstrip("\\") + "\\" + src.rsplit("\\", 1)[-1]] = data
        else:
            self.files[dest] = data

    def _container_dir(self, dest: str) -> str | None:
        """The directory *dest* is a container for, or ``None``.

        A plain directory is itself; a directory reparse point stands for its
        referent, which the model resolves one hop (the depth the write-path
        tests need - chained directory links are the resolve's business, not
        the promote's).
        """
        if dest in self.dirs:
            return dest
        if dest in self.dir_links:
            return self.links.get(dest) or dest
        return None

    def _run_promote_script(
        self, script: str
    ) -> str | tuple[str, _ErrorStreams, bool]:
        """Model the emitted promote against the FS model, phases and all.

        The emitted script branches on what the host sees at the destination,
        so the model does too:

        * An existing destination file goes to the runtime replace primitive
          (``[IO.File]::Replace``), which parks the replaced content at the
          backup name before the replacement takes the destination name. The
          script's own catch runs on a failure, so a promote that restores the
          parked content and one that only cleans its temp are told apart by
          the resulting FS state.
        * A directory at the destination is refused by the script's own
          container branch when the script carries one; without it the move
          treats the directory as a container and lands the temp inside.
        * Anything else is created by a ``Move-Item``; unforced it refuses a
          destination that appeared after the probe (the real cmdlet's
          "cannot create a file when that file already exists"), while
          ``-Force`` deletes an existing destination before moving the source
          - that delete is why a later move failure leaves no destination.

        ``fail_write_at == "promote"`` fires after the destination's entry is
        already gone (parked at the backup, or deleted by the force branch) and
        reports the pypsrp ``had_errors`` shape.
        """
        m_move = re.search(
            r"Move-Item -LiteralPath '([^']*)' -Destination '([^']*)'( -Force)?",
            script,
        )
        m_replace = re.search(
            r"\[IO\.File\]::Replace\('([^']*)', '([^']*)', '([^']*)'\)", script
        )
        if m_move is None:
            return ""
        tmp, dest, force = m_move.group(1), m_move.group(2), bool(m_move.group(3))
        if m_replace is not None and dest in self.files:
            src, dst, bak = m_replace.groups()
            # Success path drops a stale backup first, then the primitive
            # parks the replaced content and moves the replacement in.
            self.files.pop(bak, None)
            self.files[bak] = self.files.pop(dst)
            self.promote_removed_target = True
            self._run_before_move_hook()
            if self.fail_write_at == "promote":
                self._run_script_catch(script, parked=True)
                return self._write_failure("promote", dst)
            self.files[dst] = self.files.pop(src)
            self.files.pop(bak, None)
            return ""
        if force and dest in self.files:
            # Force overwrite deletes the destination before the move.
            self.files.pop(dest, None)
            self.promote_removed_target = True
        self._run_before_move_hook()
        if "PathType Container" in script and self._container_dir(dest) is not None:
            # The script's own container branch refuses a directory the probe
            # did not see as a file, instead of moving the temp inside it.
            self._run_script_catch(script)
            return self._write_failure("dir-refused", dest)
        if self.fail_write_at == "promote":
            self._run_script_catch(script)
            return self._write_failure("promote", tmp)
        if not force and dest in self.files and self._container_dir(dest) is None:
            # Unforced move onto a file that is already there: refused, and the
            # script's catch (temp cleanup) runs.
            self._run_script_catch(script)
            return self._write_failure("move-refused", dest)
        self._move_item(tmp, dest)
        return ""

    def _run_before_move_hook(self) -> None:
        """Fire the promote interleave hook, if a test installed one.

        The hook runs after the emitted script's destination probe and before
        its terminal step (the move, or the container refusal), so a test can
        pin "the destination changed after the probe" deterministically.
        """
        hook = self.before_promote_move
        if hook is not None:
            hook()

    def _run_script_catch(self, script: str, *, parked: bool = False) -> None:
        """Execute an emitted script's ``catch`` body against the FS model.

        Runs the guarded statements in the order they were emitted, so a script
        is reported exactly as the host would run it:

        * ``if ($parked -and (not Test-Path X) -and (Test-Path Y)) { ... }`` -
          put the parked prior content back at a destination that is gone.
          *parked* is what the emitted ``$parked`` variable holds for this
          execution: ``True`` only where the run reached the replace primitive.
          A script without the guard restores whenever the filesystem matches,
          so dropping it is modelled as resurrecting a stale backup.
        * ``if ((Test-Path X) and (Test-Path Y)) { Remove-Item Z }`` - drop a
          backup the destination no longer needs.
        * ``if (Test-Path X) { Remove-Item Y }`` - the temp cleanup: *Y* goes
          only when *X* exists, so a script that cleans the wrong path (or
          names no guard at all) leaves its temp behind for the test to see.
        """
        idx = script.find("catch {")
        if idx < 0:
            return
        body = script[idx:]
        for m in _CATCH_STATEMENT_RE.finditer(body):
            if m.group(2) is not None:
                gated, missing, park_name, src, dst = m.group(1, 2, 3, 4, 5)
                if gated is not None and not parked:
                    continue
                if missing not in self.files and park_name in self.files:
                    data = self.files.pop(src, None)
                    if data is not None:
                        self.files[dst] = data
            elif m.group(6) is not None:
                present, park_name, target = m.group(6, 7, 8)
                if present in self.files and park_name in self.files:
                    self.files.pop(target, None)
            else:
                guard, target = m.group(9, 10)
                if guard in self.files or guard in self.dirs or guard in self.links:
                    self.files.pop(target, None)
                    self.dirs.discard(target)
                    self.links.pop(target, None)
                    self.dir_links.discard(target)

    def _write_failure(self, stage: str, path: str) -> tuple[str, _ErrorStreams, bool]:
        """The pypsrp shape for a remote script failure (``had_errors``)."""
        return ("", _ErrorStreams([f"remote write failed at {stage}: {path}"]), True)

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
        if path in self.links:
            return json.dumps(
                {
                    "kind": "link",
                    "size": 0,
                    "mtime": self.MTIME,
                    "mode": (
                        "Directory, ReparsePoint"
                        if path in self.dir_links
                        else "ReparsePoint"
                    ),
                    "target": self.links[path],
                }
            )
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

    def _readlink_json(self, path: str) -> str:
        """PS ``readlink`` script reply: NOT_A_LINK, NOT_FOUND, or ``{target}``."""
        if path not in self.links:
            if path in self.dirs or path in self.files:
                return '{"error":"NOT_A_LINK"}'
            return '{"error":"NOT_FOUND"}'
        return json.dumps({"target": self.links[path]})

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