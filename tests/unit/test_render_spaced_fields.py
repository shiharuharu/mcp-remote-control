"""Agent/JSON track agreement for fields that carry whitespace.

Every path-valued field (``path``, ``cwd``, ``local``, ``target``, ``cwd_arg``)
and every free-text / machine-token field (``warning``, ``reopen_hint``, ...)
must reach the caller with its bytes intact. The Agent track folds whitespace
to underscores only for space-free machine tokens; the JSON track never folds.
These tests pin both tracks against the same input.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_remote_control.render import render_agent_text, render_json


def _json(payload: str) -> dict:
    return json.loads(payload)


def test_agent_local_path_with_spaces_stays_verbatim():
    """fs put/get: the local-side path must not be underscore-rewritten."""
    fields = {
        "op": "get",
        "ep": "local",
        "path": "/remote/My Files/a.txt",
        "local": "/Users/me/My Files/a.txt",
        "bytes": 3,
    }
    text = render_agent_text("fs", "ok", cwd="/home/u", fields=dict(fields))

    assert "| local=/Users/me/My Files/a.txt" in text
    assert "My_Files" not in text
    # Both spaced paths leave the single-token status header.
    first = text.splitlines()[0]
    assert first.startswith("@fs get ok")
    assert "My Files" not in first
    assert "| path=/remote/My Files/a.txt" in text

    assert _json(render_json("fs", "ok", cwd="/home/u", fields=dict(fields)))[
        "local"
    ] == "/Users/me/My Files/a.txt"


def test_agent_symlink_target_with_spaces_stays_verbatim():
    """fs stat on a symlink: the target path is a real path, not a token."""
    fields = {
        "op": "stat",
        "path": "/opt/link",
        "type": "symlink",
        "target": "/opt/real dir/b",
    }
    text = render_agent_text("fs", "ok", fields=dict(fields))

    assert "| target=/opt/real dir/b" in text
    assert "real_dir" not in text
    # A space-free path keeps its cheaper header-token form.
    assert "path=/opt/link" in text.splitlines()[0]

    assert _json(render_json("fs", "ok", fields=dict(fields)))[
        "target"
    ] == "/opt/real dir/b"


def test_agent_path_keeps_internal_space_runs():
    """Path meta preserves runs of spaces; only CR/LF are flattened."""
    fields = {"op": "stat", "ep": "local", "path": "/tmp/a  b/c"}
    text = render_agent_text("fs", "ok", fields=dict(fields))

    assert "| path=/tmp/a  b/c" in text


def test_agent_reopen_hint_token_stays_verbatim():
    """exec_ops documents reopen_hint as a literal token; it must match."""
    fields = {"ep": "lab", "reopen_hint": "endpoint close then open"}
    text = render_agent_text("exec", "timeout", fields=dict(fields))

    assert "| reopen_hint=endpoint close then open" in text
    assert "endpoint_close_then_open" not in text

    assert _json(render_json("exec", "timeout", fields=dict(fields)))[
        "reopen_hint"
    ] == "endpoint close then open"


def test_agent_partial_write_warning_stays_verbatim():
    """console partial write: the warning prose keeps its spaces."""
    fields = {"ep": "local", "warning": "only 1/12 bytes written"}
    text = render_agent_text("console", "ok", fields=dict(fields))

    assert "| warning=only 1/12 bytes written" in text
    assert "only_1/12_bytes_written" not in text

    assert _json(render_json("console", "ok", fields=dict(fields)))[
        "warning"
    ] == "only 1/12 bytes written"


def test_space_free_path_fields_stay_on_status_header():
    """Routing is whitespace-triggered: no extra meta line when avoidable."""
    fields = {
        "op": "put",
        "ep": "local",
        "path": "/remote/a.txt",
        "local": "/Users/me/a.txt",
        "bytes": 3,
    }
    text = render_agent_text("fs", "ok", fields=dict(fields))

    assert text.count("\n") == 0
    first = text.splitlines()[0]
    assert "path=/remote/a.txt" in first
    assert "local=/Users/me/a.txt" in first


def test_agent_cwd_arg_with_spaces_stays_verbatim():
    """exec INVALID_CWD echoes the caller's cwd; a retry reads that value."""
    fields = {
        "ep": "local",
        "form": "command",
        "msg": "cwd does not exist or is not a directory: /tmp/no such dir/xyz",
        "cwd_arg": "/tmp/no such dir/xyz",
    }
    text = render_agent_text("exec", "error", code="INVALID_CWD", fields=dict(fields))

    assert "| cwd_arg=/tmp/no such dir/xyz" in text
    assert "no_such_dir" not in text
    # The rounded-back path leaves the single-token status header.
    assert "cwd_arg" not in text.splitlines()[0]

    assert _json(
        render_json("exec", "error", code="INVALID_CWD", fields=dict(fields))
    )["cwd_arg"] == "/tmp/no such dir/xyz"


def test_space_free_cwd_arg_stays_on_status_header():
    """Routing is whitespace-triggered; the common case costs no extra line."""
    fields = {"ep": "local", "form": "command", "cwd_arg": "/tmp/missing"}
    text = render_agent_text("exec", "error", code="INVALID_CWD", fields=dict(fields))

    assert text.count("\n") == 0
    assert "cwd_arg=/tmp/missing" in text.splitlines()[0]


def test_exec_invalid_cwd_render_matches_json(tmp_path, monkeypatch):
    """End-to-end: the real exec error path names a path, not an identifier."""
    from mcp_remote_control.core import exec_ops
    from mcp_remote_control.endpoint import reset_registry

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "config"
    monkeypatch.setenv("MRC_HOME", str(fixtures))
    reset_registry()
    missing = tmp_path / "no such dir" / "xyz"
    res = exec_ops.run(ep="local", command="true", cwd=str(missing), home=fixtures)
    reset_registry()

    assert res.code == "INVALID_CWD"
    assert str(missing) in str(res.fields.get("cwd_arg", ""))

    text = res.render_text()
    rendered = str(res.fields["cwd_arg"])
    assert f"| cwd_arg={rendered}" in text
    assert "no_such_dir" not in text
    assert _json(res.render_json())["cwd_arg"] == rendered
