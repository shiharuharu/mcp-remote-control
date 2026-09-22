"""Unit tests for mcp_remote_control.render."""

from __future__ import annotations

import json

import pytest

from mcp_remote_control.render import (
    REDACTED,
    render_agent_text,
    render_json,
)
from mcp_remote_control.render.redact import is_sensitive_key, redact_string

# ---------------------------------------------------------------------------
# Agent track - shape & kinds
# ---------------------------------------------------------------------------


def test_exec_ok_includes_kind_status_cwd_and_body():
    text = render_agent_text(
        "exec",
        "ok",
        cwd="/home/deploy/app",
        fields={"ep": "local", "exit": 0, "ms": 12},
        body="$ echo hello\nhello",
    )
    first = text.splitlines()[0]
    assert first.startswith("@exec ok")
    assert "ep=local" in first
    assert "exit=0" in first
    assert "cwd=/home/deploy/app" in first
    assert "$ echo hello" in text
    assert "hello" in text
    # Agent track is not a JSON shell
    assert not text.lstrip().startswith("{")


def test_exec_fail_and_timeout_statuses():
    fail = render_agent_text(
        "exec", "fail", cwd="/tmp", fields={"ep": "ep_1", "exit": 1}
    )
    assert fail.startswith("@exec fail")
    assert "exit=1" in fail
    assert "cwd=/tmp" in fail

    timeout = render_agent_text(
        "exec",
        "timeout",
        cwd="/tmp",
        fields={"ep": "ep_1", "exit": "?", "partial": 1},
        body="partial out",
    )
    assert timeout.startswith("@exec timeout")
    assert "partial" in timeout.splitlines()[0]
    assert "partial out" in timeout


def test_fs_put_ok_with_absolute_path_and_cwd():
    text = render_agent_text(
        "fs",
        "ok",
        cwd="/home/deploy",
        fields={
            "op": "put",
            "ep": "ep_7a",
            "path": "/home/deploy/app/x.conf",
            "bytes": 420,
            "ms": 88,
            "sha256": "e3b0c44298fc",
        },
    )
    first = text.splitlines()[0]
    assert first.startswith("@fs put ok")
    assert "path=/home/deploy/app/x.conf" in first
    assert "cwd=/home/deploy" in first
    assert "bytes=420" in first
    # put success must not echo file body when body omitted
    assert "\n\n" not in text


def test_fs_read_with_body_and_meta():
    text = render_agent_text(
        "fs",
        "ok",
        cwd="/var/log",
        fields={
            "op": "read",
            "ep": "local",
            "path": "/var/log/syslog",
            "type": "text",
            "bytes": 12,
            "encoding": "utf-8",
        },
        body="hello syslog",
    )
    assert text.startswith("@fs read ok")
    assert "path=/var/log/syslog" in text
    assert "| encoding=utf-8" in text
    assert "hello syslog" in text


def test_endpoint_ok():
    text = render_agent_text(
        "endpoint",
        "ok",
        cwd="/home/deploy",
        fields={
            "id": "ep_7a",
            "transport": "ssh",
            "host": "10.0.0.5",
            "caps": "exec,screen,fs",
        },
    )
    first = text.splitlines()[0]
    assert first.startswith("@endpoint ok")
    assert "id=ep_7a" in first
    assert "transport=ssh" in first
    assert "cwd=/home/deploy" in first


def test_ps_ok_with_cwd():
    text = render_agent_text(
        "ps",
        "ok",
        cwd=r"C:\Users\Administrator\project",
        fields={"id": "ps_01", "ep": "win-lab"},
        body="Hello from PS",
    )
    first = text.splitlines()[0]
    assert first.startswith("@ps ok")
    assert "id=ps_01" in first
    assert r"cwd=C:\Users\Administrator\project" in first
    assert "Hello from PS" in text


def test_screen_ok_with_cur_and_frame():
    text = render_agent_text(
        "screen",
        "ok",
        cwd="/var/log",
        fields={
            "id": "scr_01",
            "ep": "prod-web",
            "cols": 200,
            "rows": 52,
            "cur": (47, 2),
            "gen": 3,
            "idle": True,
            "surface": "shell",
            "cursor_line": "deploy@host:/var/log$ ",
        },
        body="line1\nline2",
    )
    first = text.splitlines()[0]
    assert first.startswith("@screen ok")
    assert "id=scr_01" in first
    assert "200x52" in first
    assert "cur=47,2" in first
    assert "cwd=/var/log" in first
    assert "idle" in first.split()
    assert "| cursor_line=" in text
    assert "line1\nline2" in text


def test_screen_unchanged_omits_frame():
    text = render_agent_text(
        "screen",
        "unchanged",
        cwd="/home/deploy",
        fields={
            "id": "scr_01",
            "gen": 12,
            "hash": "a1b2c3d4",
            "idle": True,
            "surface": "shell",
        },
        # no body - unchanged path
    )
    assert text.startswith("@screen unchanged")
    assert "hash=a1b2c3d4" in text
    assert "cwd=/home/deploy" in text
    # no blank-line body section
    assert "\n\n" not in text
    assert "@hint" not in text


def test_screen_cur_as_string():
    text = render_agent_text(
        "screen",
        "ok",
        fields={"id": "s1", "cur": "10,0", "cols": 80, "rows": 24},
    )
    assert "cur=10,0" in text.splitlines()[0]
    assert "80x24" in text.splitlines()[0]


def test_error_kind_with_code():
    text = render_agent_text(
        "error",
        "error",
        code="ENDPOINT_NOT_FOUND",
        fields={"msg": "no such endpoint"},
    )
    first = text.splitlines()[0]
    assert first.startswith("@error error")
    assert "code=ENDPOINT_NOT_FOUND" in first
    # Free-text msg lives on meta line with spaces preserved (not header underscores).
    assert "msg=" not in first or "msg=no_such_endpoint" not in first
    assert "| msg=no such endpoint" in text


def test_msg_with_spaces_on_meta_not_header():
    text = render_agent_text(
        "fs",
        "error",
        code="PERMISSION_DENIED",
        fields={
            "op": "put",
            "path": "/tmp/x",
            "msg": "permission denied for write",
        },
    )
    first = text.splitlines()[0]
    assert first.startswith("@fs put error")
    assert "code=PERMISSION_DENIED" in first
    assert "path=/tmp/x" in first
    # Spaces kept on meta; not collapsed to underscores on the status header.
    assert "msg=permission_denied" not in first
    assert "| msg=permission denied for write" in text


def test_agent_cwd_and_path_with_spaces_stay_verbatim():
    """Absolute cwd/path with spaces must appear unchanged, not underscore-rewritten."""
    cwd = "/home/user/My Documents"
    path = "/tmp/My Files/a.txt"
    text = render_agent_text(
        "fs",
        "ok",
        cwd=cwd,
        fields={"op": "stat", "ep": "local", "path": path},
    )
    assert f"cwd={cwd}" in text
    assert f"path={path}" in text
    assert "My_Documents" not in text
    assert "My_Files" not in text
    # Header stays one token stream; spaced paths live on | meta.
    first = text.splitlines()[0]
    assert first.startswith("@fs stat ok")
    assert "My Documents" not in first
    assert "My Files" not in first
    assert f"| cwd={cwd}" in text
    assert f"| path={path}" in text


def test_agent_windows_cwd_with_spaces_stays_verbatim():
    """Windows cwd with a space must stay verbatim (not My_Documents)."""
    cwd = r"C:\Users\My Documents"
    text = render_agent_text(
        "exec",
        "ok",
        cwd=cwd,
        fields={"ep": "win-lab", "exit": 0},
    )
    assert f"cwd={cwd}" in text
    assert "My_Documents" not in text
    first = text.splitlines()[0]
    assert first.startswith("@exec ok")
    assert "cwd=" not in first
    assert f"| cwd={cwd}" in text


def test_exec_error_with_code():
    text = render_agent_text(
        "exec",
        "error",
        code="ENDPOINT_NOT_FOUND",
        fields={"msg": "missing"},
    )
    assert text.startswith("@exec error")
    assert "code=ENDPOINT_NOT_FOUND" in text
    assert "| msg=missing" in text


def test_geometry_meta_omitted_when_fit_ok_steps_zero():
    text = render_agent_text(
        "screen",
        "ok",
        fields={
            "id": "scr_01",
            "cols": 160,
            "rows": 48,
            "fit": "ok",
            "steps": 0,
            "seed": "160x48",
            "class": "shell",
        },
    )
    assert text.startswith("@screen ok")
    assert "160x48" in text.splitlines()[0]
    assert "| fit=" not in text
    assert "| steps=" not in text
    assert "| seed=" not in text
    assert "| class=" not in text
    assert "| geom=" not in text


def test_geometry_meta_cmd_kept_when_fit_ok_steps_zero():
    """Healthy fit must still surface opened command."""
    body = "line1\nline2"
    text = render_agent_text(
        "screen",
        "ok",
        fields={
            "id": "scr_01",
            "cols": 160,
            "rows": 48,
            "fit": "ok",
            "steps": 0,
            "seed": "160x48",
            "class": "shell",
            "cmd": "htop",
        },
        body=body,
    )
    assert text.startswith("@screen ok")
    assert "cmd=htop" in text
    assert "| geom=" in text
    geom_line = next(ln for ln in text.splitlines() if ln.startswith("| geom="))
    assert "cmd=htop" in geom_line
    # Still quiet for seed/class noise on healthy fit.
    assert "seed=" not in geom_line
    assert "class=" not in geom_line
    assert "| seed=" not in text
    assert "| class=" not in text
    assert "| fit=" not in text
    assert "| steps=" not in text
    # Frame/body content unchanged.
    assert body in text


def test_geometry_meta_merged_when_nontrivial():
    text = render_agent_text(
        "screen",
        "ok",
        fields={
            "id": "scr_01",
            "cols": 180,
            "rows": 50,
            "fit": "ok",
            "steps": 2,
            "seed": "100x30",
            "class": "tui",
            "cmd": "htop",
        },
    )
    assert "| geom=" in text
    geom_line = next(ln for ln in text.splitlines() if ln.startswith("| geom="))
    assert "ok" in geom_line
    assert "steps=2" in geom_line
    assert "seed=100x30" in geom_line
    assert "class=tui" in geom_line
    assert "cmd=htop" in geom_line
    # Not one meta line per geometry field
    assert "| fit=" not in text
    assert "| steps=" not in text


def test_geometry_meta_forced_shown():
    text = render_agent_text(
        "screen",
        "ok",
        fields={
            "id": "scr_01",
            "fit": "forced",
            "steps": 0,
            "seed": "120x40",
            "class": "shell",
        },
    )
    assert "| geom=forced" in text or "| geom=forced " in text
    geom_line = next(ln for ln in text.splitlines() if ln.startswith("| geom="))
    assert "seed=120x40" in geom_line


def test_screen_id_deduped_when_equal_to_id():
    text = render_agent_text(
        "screen",
        "ok",
        fields={
            "id": "scr_01",
            "screen_id": "scr_01",
            "cols": 80,
            "rows": 24,
        },
    )
    first = text.splitlines()[0]
    assert "id=scr_01" in first
    assert "screen_id=" not in first


def test_probe_summary_one_meta_line():
    text = render_agent_text(
        "endpoint",
        "ok",
        cwd="/home/u",
        fields={
            "ep": "local",
            "transport": "local",
            "caps": "exec,fs,screen",
            "shell": "zsh",
            "uname": "Linux-x86_64",
            "locale": "UTF-8",
        },
    )
    assert text.startswith("@endpoint ok")
    # Grouped onto one meta line (not three).
    probe_lines = [
        ln
        for ln in text.splitlines()
        if ln.startswith("| ") and ("shell=" in ln or "uname=" in ln or "locale=" in ln)
    ]
    assert len(probe_lines) == 1
    line = probe_lines[0]
    assert "shell=zsh" in line
    assert "uname=Linux-x86_64" in line
    assert "locale=UTF-8" in line


def test_probe_summary_winrm_ps_tokens_on_meta_line():
    """WinRM PS Agent tokens collapse onto the probe meta line, not the header.

    endpoint_ops emits ps_version / lang_mode / ps_fs / ps_edition / ps_probe;
    they must share the ``| shell=... dialect=...`` line so agents skim one row.
    """
    text = render_agent_text(
        "endpoint",
        "ok",
        cwd=r"C:\Windows\System32",
        fields={
            "ep": "win-lab",
            "transport": "winrm",
            "caps": "exec,fs,ps",
            "shell": "powershell",
            "dialect": "powershell",
            "ps_version": "5.1.19041",
            "lang_mode": "FullLanguage",
            "ps_fs": 1,
            "ps_edition": "Desktop",
        },
    )
    first = text.splitlines()[0]
    assert first.startswith("@endpoint ok")
    # Must not leak onto the status header as free tokens.
    assert "ps_version=" not in first
    assert "lang_mode=" not in first
    assert "ps_fs=" not in first
    assert "ps_edition=" not in first

    probe_lines = [
        ln
        for ln in text.splitlines()
        if ln.startswith("| ")
        and ("shell=" in ln or "ps_version=" in ln or "lang_mode=" in ln)
    ]
    assert len(probe_lines) == 1, text
    line = probe_lines[0]
    assert "shell=powershell" in line
    assert "dialect=powershell" in line
    assert "ps_version=5.1.19041" in line
    assert "lang_mode=FullLanguage" in line
    assert "ps_fs=1" in line
    assert "ps_edition=Desktop" in line
    # Token order follows _PROBE_META_KEYS (shell/dialect before ps_*).
    assert line.index("shell=") < line.index("dialect=") < line.index("ps_version=")
    assert line.index("ps_version=") < line.index("lang_mode=") < line.index("ps_fs=")
    assert line.index("ps_fs=") < line.index("ps_edition=")


def test_probe_summary_ps_probe_status_on_meta_line():
    """ps_probe=skipped/failed joins the collapse line when present alone."""
    text = render_agent_text(
        "endpoint",
        "ok",
        fields={
            "ep": "win-lab",
            "transport": "winrm",
            "ps_probe": "skipped",
        },
    )
    first = text.splitlines()[0]
    assert "ps_probe=" not in first
    assert "| ps_probe=skipped" in text
    # Single meta line, no separate status-header token.
    meta = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert len(meta) == 1
    assert meta[0] == "| ps_probe=skipped"


def test_hint_appended():
    text = render_agent_text(
        "screen",
        "ok",
        fields={"id": "scr_01", "cur": (0, 0), "cols": 80, "rows": 24},
        body="vim",
        hint="TUI/alt-screen; prefer keys over shell commands",
    )
    assert text.splitlines()[-1].startswith("@hint ")
    assert "prefer keys" in text.splitlines()[-1]


def test_cwd_omitted_when_not_provided():
    text = render_agent_text("exec", "ok", fields={"ep": "local", "exit": 0})
    assert "cwd=" not in text


# ---------------------------------------------------------------------------
# Machine track - compact JSON
# ---------------------------------------------------------------------------


def test_json_loads_with_status_and_kind():
    raw = render_json(
        "exec",
        "ok",
        cwd="/home/deploy/app",
        fields={"ep": "local", "exit": 0},
        body="hello",
    )
    # compact: no indent spaces after separators
    assert "\n" not in raw
    assert ": " not in raw
    data = json.loads(raw)
    assert data["kind"] == "exec"
    assert data["status"] == "ok"
    assert data["cwd"] == "/home/deploy/app"
    assert data["exit"] == 0
    assert data["body"] == "hello"


def test_json_omits_nulls():
    raw = render_json("fs", "ok", fields={"op": "stat", "path": "/tmp/x", "ep": None})
    data = json.loads(raw)
    assert "ep" not in data
    assert data["path"] == "/tmp/x"
    assert "body" not in data
    assert "code" not in data


def test_json_screen_cur_and_extra():
    raw = render_json(
        "screen",
        "ok",
        cwd="/tmp",
        fields={"id": "scr_01", "cur": (1, 2), "cols": 80, "rows": 24},
        generation=9,
    )
    data = json.loads(raw)
    assert data["cur"] == "1,2"
    assert data["size"] == "80x24"
    assert data["generation"] == 9


def test_json_error_code():
    data = json.loads(
        render_json("error", "error", code="PERMISSION_DENIED", fields={"msg": "nope"})
    )
    assert data["status"] == "error"
    assert data["code"] == "PERMISSION_DENIED"


def test_json_cwd_and_path_with_spaces_stay_verbatim():
    """JSON track keeps spaced cwd/path as raw strings; compact separators stay."""
    cwd = "/home/user/My Documents"
    path = "/tmp/My Files/a.txt"
    raw = render_json(
        "fs",
        "ok",
        cwd=cwd,
        fields={"op": "stat", "path": path},
    )
    assert "\n" not in raw
    assert ": " not in raw
    data = json.loads(raw)
    assert data["cwd"] == cwd
    assert data["path"] == path
    assert "My_Documents" not in raw
    assert "My_Files" not in raw


# ---------------------------------------------------------------------------
# Redaction - both tracks
# ---------------------------------------------------------------------------


def test_password_field_not_redacted_token_is():
    """Product rule: plain passwords are visible; tokens still redacted."""
    fields = {
        "ep": "lab",
        "password": "s3cr3t-value",
        "token": "abcd1234",
        "path": "/tmp/x",
    }
    agent = render_agent_text("endpoint", "ok", fields=fields, cwd="/home/u")
    assert "s3cr3t-value" in agent
    assert "abcd1234" not in agent
    assert f"token={REDACTED}" in agent

    data = json.loads(render_json("endpoint", "ok", fields=fields, cwd="/home/u"))
    assert data["password"] == "s3cr3t-value"
    assert data["token"] == REDACTED
    assert data["path"] == "/tmp/x"


def test_redact_private_key_material_in_body():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF7P\n"
        "-----END RSA PRIVATE KEY-----"
    )
    body = f"got key:\n{pem}\ndone"
    agent = render_agent_text("exec", "ok", fields={"ep": "local", "exit": 0}, body=body)
    assert "BEGIN RSA PRIVATE KEY" not in agent
    assert REDACTED in agent

    data = json.loads(
        render_json("exec", "ok", fields={"ep": "local", "exit": 0}, body=body)
    )
    assert "BEGIN RSA PRIVATE KEY" not in data["body"]
    assert REDACTED in data["body"]


def test_inline_password_assignment_not_redacted():
    body = "export password=hunter2 && run"
    agent = render_agent_text("exec", "ok", body=body)
    assert "hunter2" in agent


def test_redact_secret_key_name_variants():
    fields = {"private_key": "KEYDATA", "api_key": "xyz", "host": "h"}
    data = json.loads(render_json("endpoint", "ok", fields=fields))
    assert data["private_key"] == REDACTED
    assert data["api_key"] == REDACTED
    assert data["host"] == "h"


@pytest.mark.parametrize(
    "kind",
    ["endpoint", "exec", "fs", "screen", "ps", "error"],
)
def test_all_kinds_render(kind: str):
    status = "error" if kind == "error" else "ok"
    kwargs: dict = {"fields": {"id": "x1"}}
    if kind == "error":
        kwargs["code"] = "X"
    if kind == "fs":
        kwargs["fields"] = {"op": "stat", "path": "/tmp"}
    text = render_agent_text(kind, status, cwd="/tmp", **kwargs)
    assert text.startswith(f"@{kind}")
    data = json.loads(render_json(kind, status, cwd="/tmp", **kwargs))
    assert data["kind"] == kind
    assert data["status"] == status


# ---------------------------------------------------------------------------
# Redaction - quoted secrets with spaces + sensitive-key heuristics
# ---------------------------------------------------------------------------


def test_redact_inline_quoted_secret_with_spaces_agent_and_json():
    """Quoted secret= with spaces must redact the full value on both tracks.

    Uses ``secret=`` (not password=) - passwords are product-visible.
    """
    body = 'export secret="my secret value" && run'
    red = redact_string(body)
    assert "my secret value" not in red
    assert 'secret="***"' in red

    agent = render_agent_text("exec", "ok", fields={"ep": "local", "exit": 0}, body=body)
    assert "my secret value" not in agent
    assert 'secret="***"' in agent

    data = json.loads(render_json("exec", "ok", fields={"ep": "local", "exit": 0}, body=body))
    assert "my secret value" not in data["body"]
    assert 'secret="***"' in data["body"]


def test_redact_inline_quoted_single_word_still_redacts():
    """Quoted value without spaces still redacts (regression guard)."""
    assert redact_string('secret="abc"') == 'secret="***"'
    assert redact_string("token='xyz'") == "token='***'"


def test_redact_inline_quoted_secret_with_opposite_quote_inside():
    """Mixed-quote values on secret= (password= is not redacted)."""
    body_dq = 'export secret="o\'reilly" && run'
    red_dq = redact_string(body_dq)
    assert "o'reilly" not in red_dq
    assert 'secret="***"' in red_dq
    agent_dq = render_agent_text("exec", "ok", fields={"ep": "local", "exit": 0}, body=body_dq)
    assert "o'reilly" not in agent_dq
    assert 'secret="***"' in agent_dq
    data_dq = json.loads(render_json("exec", "ok", fields={"ep": "local", "exit": 0}, body=body_dq))
    assert "o'reilly" not in data_dq["body"]
    assert 'secret="***"' in data_dq["body"]

    body_sq = 'export secret=\'he said "hi"\' && run'
    red_sq = redact_string(body_sq)
    assert 'he said "hi"' not in red_sq
    assert "secret='***'" in red_sq


def test_redact_inline_escaped_same_quote_redacts_up_to_first_escape():
    """Escaped same-quote inside secret= redacts head of value."""
    body = 'export secret="my \\"token\\"" && run'
    red = redact_string(body)
    assert 'secret="***"' in red
    agent = render_agent_text("exec", "ok", fields={"ep": "local", "exit": 0}, body=body)
    assert 'secret="***"' in agent


def test_is_sensitive_key_concatenated_no_underscore_not_redacted():
    """password is not a sensitive field name (product rule); tokens still are."""
    assert is_sensitive_key("dbpassword") is False
    assert is_sensitive_key("usertoken") is False
    assert is_sensitive_key("password") is False
    assert is_sensitive_key("db_password") is False
    assert is_sensitive_key("user_token") is True
    assert is_sensitive_key("my-api-key") is True
    assert is_sensitive_key("API_KEY") is True


def test_is_sensitive_key_private_key_pem():
    """private_key_pem is a known secret field name (not only private_key)."""
    assert is_sensitive_key("private_key_pem") is True
    assert is_sensitive_key("PRIVATE_KEY_PEM") is True
    assert is_sensitive_key("ssh_private_key_pem") is True


def test_redact_private_key_pem_field_name_agent_and_json():
    """Field-name redaction covers private_key_pem on both tracks."""
    fields = {
        "ep": "lab",
        "private_key_pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
        "host": "h",
    }
    agent = render_agent_text("endpoint", "ok", fields=fields)
    assert "BEGIN OPENSSH" not in agent
    assert "fake" not in agent
    assert f"private_key_pem={REDACTED}" in agent

    data = json.loads(render_json("endpoint", "ok", fields=fields))
    assert data["private_key_pem"] == REDACTED
    assert data["host"] == "h"


def test_redact_inline_private_key_pem_assignment():
    """Inline assign pattern redacts private_key_pem=... values."""
    body = 'private_key_pem="-----BEGIN FAKE-----\nabc\n-----END FAKE-----"'
    red = redact_string(body)
    assert "BEGIN FAKE" not in red
    assert f'private_key_pem="{REDACTED}"' in red


# ---------------------------------------------------------------------------
# render `dead` flag on Agent track
# ---------------------------------------------------------------------------


def test_dead_flag_emitted_on_agent_track():
    """``fields={"dead": True}`` emits a bare ``dead`` token on the Agent track.

    Flag membership and emission order both come from ``_FLAG_ORDER``. A
    true flag omitted from that tuple is skipped in the header loops and
    dropped from Agent output while JSON still carries the field.
    """
    text = render_agent_text(
        "screen",
        "ok",
        fields={"id": "scr_01", "dead": True, "cols": 80, "rows": 24},
    )
    first = text.splitlines()[0]
    assert first.startswith("@screen ok")
    assert "dead" in first.split()
    assert "dead=True" not in first  # bare token, not k=v
    # JSON track still carries the field.
    data = json.loads(
        render_json("screen", "ok", fields={"id": "scr_01", "dead": True, "cols": 80, "rows": 24})
    )
    assert data["dead"] is True


def test_dead_status_with_dead_field_combo_renders():
    """``status="dead"`` + ``fields={"dead": True}`` combo must render without
    error and produce a sensible Agent track (status token + bare ``dead``
    flag).

    The two ``dead`` tokens are independent (``status`` is the OpResult
    outcome; the ``dead`` field is a boolean-ish flag the renderer emits as a
    bare token). They can co-occur (a dead endpoint surfaces ``@screen dead``
    plus a ``dead`` flag from fields).
    """
    text = render_agent_text(
        "screen",
        "dead",
        fields={"id": "scr_01", "dead": True, "cols": 80, "rows": 24},
    )
    first = text.splitlines()[0]
    # Status token comes from the status arg; the bare ``dead`` flag from fields.
    assert first.startswith("@screen dead")
    tokens = first.split()
    assert tokens.count("dead") == 2, (tokens, text)
    assert "dead=True" not in first  # bare flag, not k=v
    # No crash, no malformed tokens.
    assert "True" not in first
    # JSON track: status carries ``dead`` and the field carries ``dead`` too.
    data = json.loads(
        render_json(
            "screen",
            "dead",
            fields={"id": "scr_01", "dead": True, "cols": 80, "rows": 24},
        )
    )
    assert data["status"] == "dead"
    assert data["dead"] is True
