"""Notes-read failures must report the real failure, not a write failure.

A notes file that is not valid UTF-8 must report an encoding error, and a
read denied by file permissions must report itself as unreadable - no write
happened in either case, and the bytes on disk stay untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_remote_control.config.store import ensure_home_layout, put_profile
from mcp_remote_control.core import config_ops

_LATIN1_NOTE = b"caf\xe9 latin-1 note\n"


@pytest.mark.parametrize("action", ["read", "append", "prepend"])
def test_notes_decode_failure_reports_encoding_not_write(
    tmp_path: Path, action: str
) -> None:
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    notes_file = tmp_path / "notes" / "lab.md"
    notes_file.write_bytes(_LATIN1_NOTE)

    kwargs: dict[str, object] = {
        "action": action,
        "name": "lab",
        "home": str(tmp_path),
    }
    if action in ("append", "prepend"):
        kwargs["content"] = "more\n"
    res = config_ops.run("notes", **kwargs)

    assert res.status == "error"
    assert res.code == "NOTES_ENCODING_INVALID"
    # The bytes on disk are untouched: nothing was written.
    assert notes_file.read_bytes() == _LATIN1_NOTE


def test_notes_decode_failure_message_names_the_file(tmp_path: Path) -> None:
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    (tmp_path / "notes" / "lab.md").write_bytes(_LATIN1_NOTE)

    res = config_ops.run("notes", action="read", name="lab", home=str(tmp_path))
    msg = str(res.fields.get("msg", ""))
    assert "notes/lab.md" in msg
    assert "UTF-8" in msg


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate reads only for a non-root POSIX user",
)
def test_unreadable_notes_reports_unreadable_not_write(tmp_path: Path) -> None:
    """A read that could not open the file must not report a write failure."""
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    notes_file = tmp_path / "notes" / "lab.md"
    notes_file.write_text("hello\n", encoding="utf-8")
    os.chmod(notes_file, 0)
    try:
        res = config_ops.run("notes", action="read", name="lab", home=str(tmp_path))
        # stat needs no read permission: the file is there, only unreadable.
        stat_res = config_ops.run(
            "notes", action="stat", name="lab", home=str(tmp_path)
        )
    finally:
        os.chmod(notes_file, 0o600)

    assert res.status == "error"
    assert res.code == "NOTES_UNREADABLE"
    assert stat_res.status == "ok"

    msg = str(res.fields.get("msg", ""))
    assert "notes/lab.md" in msg
    # The absolute config-home path must not be echoed back to the agent.
    assert str(tmp_path) not in msg
