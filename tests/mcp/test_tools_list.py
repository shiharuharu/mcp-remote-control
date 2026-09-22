"""MCP tool surface: host five + extra console|config."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.mcp_server import TOOL_NAMES, create_server, tool_names
from mcp_remote_control.serial.registry import reset_serial_registry

HOST_FIVE = ["endpoint", "exec", "fs", "screen", "ps"]
EXTRA = ["console", "config"]
REGISTERED = HOST_FIVE + EXTRA
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

def _tool_text(out: object) -> str:
    """Extract Agent-track text from MCPServer.call_tool result (SDK v1 or v2).

    v2 returns ``CallToolResult`` with ``.content`` blocks.
    v1 returned a list of blocks or ``(blocks, structured)``.
    """
    if hasattr(out, "content") and not isinstance(out, (list, tuple)):
        blocks = getattr(out, "content") or []
        structured = getattr(out, "structured_content", None)
        if structured:
            raise AssertionError(f"unexpected structured_content={structured!r}")
        return "\n".join(getattr(b, "text", "") or "" for b in blocks)
    if isinstance(out, tuple):
        blocks = out[0]
        if len(out) > 1 and out[1]:
            raise AssertionError(f"unexpected structured={out[1]!r}")
        return "\n".join(getattr(b, "text", "") or "" for b in blocks)
    blocks = out  # type: ignore[assignment]
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)  # type: ignore[arg-type]


def _tool_is_error(out: object) -> bool:
    """MCP protocol error flag: SDK ``is_error`` / wire ``isError``.

    A missing flag is success (protocol default).
    """
    if hasattr(out, "is_error"):
        return bool(getattr(out, "is_error"))
    if isinstance(out, dict):
        return bool(out.get("isError") or out.get("is_error"))
    return False


def _tool_blocks(out: object) -> list[object]:
    """Return content blocks from MCPServer.call_tool (SDK v1 or v2)."""
    if hasattr(out, "content") and not isinstance(out, (list, tuple)):
        return list(getattr(out, "content") or [])
    if isinstance(out, tuple):
        return list(out[0])
    if isinstance(out, list):
        return out
    return []


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture(autouse=True)
def _mrc_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_serial_registry()
    yield
    reset_registry()
    reset_serial_registry()


def test_tool_names_pure() -> None:
    names = tool_names()
    assert names == REGISTERED
    assert list(TOOL_NAMES) == REGISTERED
    assert names[:5] == HOST_FIVE
    assert names[5:] == EXTRA


def test_mcpserver_list_tools_matches() -> None:
    async def _run() -> list[str]:
        mcp = create_server()
        tools = await mcp.list_tools()
        return [t.name for t in tools]

    listed = asyncio.run(_run())
    assert listed == REGISTERED


def test_endpoint_tool_call_returns_agent_text() -> None:
    async def _run() -> str:
        mcp = create_server()
        return _tool_text(await mcp.call_tool("endpoint", {"op": "list"}))

    text = asyncio.run(_run())
    first = text.splitlines()[0] if text else ""
    assert first.split()[:2] == ["@endpoint", "ok"]
    assert not text.lstrip().startswith("{")


def test_tools_unstructured_agent_text_no_result_json_shell() -> None:
    """MCP wire: plain content text, no structured_content {result:...} shell."""

    async def _run() -> None:
        mcp = create_server()
        tools = await mcp.list_tools()
        for tool in tools:
            schema = getattr(tool, "output_schema", None) or getattr(
                tool, "outputSchema", None
            )
            assert schema is None, f"{tool.name} still has output_schema={schema!r}"

        out = await mcp.call_tool("config", {"op": "home"})
        if hasattr(out, "structured_content"):
            assert not out.structured_content, out.structured_content
        text = _tool_text(out)
        assert text.lstrip().startswith("@config ok")
        assert "MRC_HOME" not in text
        assert str(FIXTURES) not in text
        assert not text.lstrip().startswith("{")
        assert '"result"' not in text.split("\n", 1)[0]

    asyncio.run(_run())


def test_fs_invalid_op_sets_is_error() -> None:
    """Core INVALID_OP must surface as MCP isError, not a successful call."""

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool("fs", {"op": "invalid"})

    out = asyncio.run(_run())
    assert _tool_is_error(out) is True
    dump = getattr(out, "model_dump", None)
    if callable(dump):
        wire = dump(by_alias=True)
        assert wire.get("isError") is True
        assert not wire.get("structuredContent")
    text = _tool_text(out)
    assert "code=INVALID_OP" in text
    assert text.lstrip().startswith("@fs")
    if hasattr(out, "structured_content"):
        assert not out.structured_content


def test_endpoint_list_success_is_not_error() -> None:
    """Successful endpoint list stays isError=false with Agent-track text."""

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool("endpoint", {"op": "list"})

    out = asyncio.run(_run())
    assert _tool_is_error(out) is False
    dump = getattr(out, "model_dump", None)
    if callable(dump):
        wire = dump(by_alias=True)
        assert wire.get("isError") in (False, None)
        assert not wire.get("structuredContent")
    text = _tool_text(out)
    first = text.splitlines()[0] if text else ""
    assert first.split()[:2] == ["@endpoint", "ok"]
    if hasattr(out, "structured_content"):
        assert not out.structured_content


def test_unchanged_status_is_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core status=unchanged is success; MCP is_error stays false."""
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.core.result import OpResult

    def _unchanged(**_kwargs: object) -> OpResult:
        return OpResult(
            kind="screen",
            status="unchanged",
            fields={"op": "send", "id": "s1"},
        )

    monkeypatch.setattr(screen_ops, "run", _unchanged)

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool(
            "screen",
            {"op": "send", "id": "s1", "actions": [{"type": "nop"}]},
        )

    out = asyncio.run(_run())
    assert _tool_is_error(out) is False
    text = _tool_text(out)
    first = text.splitlines()[0] if text else ""
    assert first.split()[:2] == ["@screen", "unchanged"]
    if hasattr(out, "structured_content"):
        assert not out.structured_content


def test_console_tool_list_returns_console_kind() -> None:
    async def _run() -> str:
        mcp = create_server()
        return _tool_text(await mcp.call_tool("console", {"op": "list"}))

    text = asyncio.run(_run())
    first = text.splitlines()[0] if text else ""
    assert first.split()[:2] in (["@console", "ok"], ["@console", "error"])
    assert not text.lstrip().startswith("@endpoint")


def test_all_tools_callable() -> None:
    async def _run() -> dict[str, str]:
        mcp = create_server()
        payloads = {
            "endpoint": {"op": "list"},
            "exec": {"ep": "local", "command": "echo hi"},
            "fs": {"op": "list", "ep": "local", "path": "/"},
            "screen": {"op": "list"},
            "ps": {"op": "open", "ep": "win"},
            "console": {"op": "list"},
            "config": {"op": "home"},
        }
        results: dict[str, str] = {}
        for name, args in payloads.items():
            results[name] = _tool_text(await mcp.call_tool(name, args))
        return results

    results = asyncio.run(_run())
    assert set(results) == set(REGISTERED)
    firsts = {name: (text.splitlines()[0] if text else "") for name, text in results.items()}
    for name, text in results.items():
        assert f"@{name}" in text, (name, text)
    assert firsts["endpoint"].split()[:2] == ["@endpoint", "ok"]
    assert firsts["exec"].split()[:2] == ["@exec", "ok"]
    assert firsts["fs"].startswith("@fs list ok")
    assert firsts["screen"].split()[:2] == ["@screen", "ok"]
    assert firsts["config"].split()[:2] == ["@config", "ok"]
    assert firsts["ps"].startswith("@ps error")
    assert firsts["console"].split()[:2] in (["@console", "ok"], ["@console", "error"])


# ---------------------------------------------------------------------------
# Param pass-through / description coverage on the MCP surface
# ---------------------------------------------------------------------------


def test_config_tool_description_lists_winrm_and_caps() -> None:
    """``config`` tool description must mention winrm={scheme,auth} and optional defaults/caps."""
    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["config"].description or ""

    desc = asyncio.run(_run())
    assert "winrm={scheme,auth}" in desc
    assert "optional defaults/caps" in desc


def test_config_description_and_instructions_password_first() -> None:
    """MCP config description + server instructions prefer plain password.

    put_secret is optional (keys/compat); shell-edit of TOML is forbidden;
    lab known_hosts=none is present.
    description/instructions point at op=help for ssh_agent / password_env /
    certificate / CredSSP coverage.
    """
    async def _run() -> tuple[str, str]:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        desc = tools["config"].description or ""
        # MCPServer stores instructions on the instance (not always via list_tools).
        instructions = getattr(mcp, "instructions", None) or ""
        return desc, instructions

    desc, instructions = asyncio.run(_run())
    for text in (desc, instructions):
        assert 'auth={"method":"password","password":' in text
        assert "known_hosts" in text and "none" in text
        assert "ssh_agent" in text or "password_env" in text
    assert "Do NOT shell-edit" in desc
    assert "do NOT shell cat/ls/edit" in instructions
    assert "op=help" in desc.lower()
    assert "put_secret" in desc.lower()
    assert "optional" in desc.lower()
    # put_secret is optional; the inline password recipe is the first bootstrap.
    put_profile_pos = desc.find("put_profile")
    put_secret_pos = desc.find("put_secret")
    password_pos = desc.find("password")
    assert password_pos >= 0
    assert put_profile_pos >= 0
    assert password_pos < put_secret_pos or "optional" in desc[put_secret_pos : put_secret_pos + 40].lower()
    assert "optional" in instructions.lower() or "put_secret is optional" in instructions
    desc_l = desc.lower()
    assert "ssh_agent" in desc
    assert "password_env" in desc
    assert "certificate" in desc_l
    assert "credssp" in desc_l
    # Host notes hang on config (not a separate tool); body only via action=read.
    assert "op=notes" in desc_l
    assert "action=read|write|append|prepend|stat|rm" in desc_l
    assert "notes" in instructions.lower()
    assert "op=notes" in instructions.lower()
    assert "action=read|write|append|prepend|stat|rm" in instructions.lower()
    assert "do NOT shell cat/ls/edit" in instructions
    assert "~/.config" in instructions


def test_console_tool_description_lists_view_knobs() -> None:
    """``console`` views description must list context/settle_ms/with_seq."""
    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["console"].description or ""

    desc = asyncio.run(_run())
    assert "context" in desc
    assert "settle_ms" in desc
    assert "with_seq" in desc


def test_screen_tool_description_lists_open_fields_and_actions() -> None:
    """``screen`` description lists open knobs + send action mini-table.

    Shipping catalog is pinned as the type= string agents must use on send.
    """

    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["screen"].description or ""

    desc = asyncio.run(_run())
    for field in ("cwd", "cols", "rows", "shell"):
        assert field in desc, f"open field {field!r} missing from screen desc: {desc}"
    assert "text|key|keys|go|click|move|to_text|submit|" in desc


def test_screen_mcp_open_forwards_cwd_cols_to_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP screen open with cwd=/tmp cols=120 reaches Core screen_ops.run.

    Monkeypatch Core so no real PTY is opened; assert kwargs the handler
    forwards (cwd/cols/rows/shell) match the call_tool payload.
    """
    from mcp_remote_control.core import screen_ops
    from mcp_remote_control.core.result import OpResult

    captured: dict[str, object] = {}

    def _capture_run(**kwargs: object) -> OpResult:
        captured.update(kwargs)
        return OpResult(
            kind="screen",
            status="ok",
            fields={
                "op": "open",
                "id": "s1",
                "ep": kwargs.get("ep") or "local",
                "cols": kwargs.get("cols") or 80,
                "rows": kwargs.get("rows") or 24,
            },
            cwd=str(kwargs.get("cwd") or ""),
        )

    monkeypatch.setattr(screen_ops, "run", _capture_run)

    async def _run() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool(
                "screen",
                {
                    "op": "open",
                    "ep": "local",
                    "cwd": "/tmp",
                    "cols": 120,
                    "rows": 40,
                    "shell": "/bin/bash",
                },
            )
        )

    text = asyncio.run(_run())

    assert captured.get("op") == "open", captured
    assert captured.get("ep") == "local", captured
    assert captured.get("cwd") == "/tmp", captured
    assert captured.get("cols") == 120, captured
    assert captured.get("rows") == 40, captured
    assert captured.get("shell") == "/bin/bash", captured
    assert text.startswith("@screen ok"), text


def test_fs_mcp_read_forwards_max_bytes_to_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP fs read with max_bytes reaches Core fs_ops.run.

    Monkeypatch Core so no real fs backend is used; the handler must
    forward max_bytes and still pass max_bytes=None when omitted.
    """
    from mcp_remote_control.core import fs_ops
    from mcp_remote_control.core.result import OpResult

    captured: dict[str, object] = {}

    def _capture_run(**kwargs: object) -> OpResult:
        captured.update(kwargs)
        return OpResult(
            kind="fs",
            status="ok",
            fields={
                "op": kwargs.get("op") or "read",
                "ep": kwargs.get("ep") or "local",
                "path": kwargs.get("path") or "",
            },
            body="x",
        )

    monkeypatch.setattr(fs_ops, "run", _capture_run)

    async def _run_with() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool(
                "fs",
                {
                    "op": "read",
                    "ep": "local",
                    "path": "/tmp/a",
                    "max_bytes": 64,
                },
            )
        )

    text = asyncio.run(_run_with())

    assert captured.get("op") == "read", captured
    assert captured.get("ep") == "local", captured
    assert captured.get("path") == "/tmp/a", captured
    assert captured.get("max_bytes") == 64, captured
    assert text.startswith("@fs"), text
    assert " ok" in text.splitlines()[0], text

    captured.clear()

    async def _run_omit() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool(
                "fs",
                {"op": "read", "ep": "local", "path": "/tmp/b"},
            )
        )

    text_omit = asyncio.run(_run_omit())

    # Default when omitted: handler still passes max_bytes=None (Core default).
    assert captured.get("op") == "read", captured
    assert captured.get("path") == "/tmp/b", captured
    assert "max_bytes" in captured, captured
    assert captured.get("max_bytes") is None, captured
    assert text_omit.startswith("@fs"), text_omit
    assert " ok" in text_omit.splitlines()[0], text_omit


def test_fs_tool_description_lists_max_bytes() -> None:
    """``fs`` tool description mentions optional max_bytes for read budget."""

    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["fs"].description or ""

    desc = asyncio.run(_run())
    assert "max_bytes" in desc
    assert "1 MiB" in desc
    lower = desc.lower()
    assert "image" in lower
    assert "png" in lower
    assert "read" in lower


def test_fs_read_png_returns_text_and_image_blocks(tmp_path: Path) -> None:
    """Complete PNG read is text + image; bytes round-trip; no structured wrap."""
    target = tmp_path / "pic.png"
    target.write_bytes(_PNG)

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool(
            "fs",
            {"op": "read", "ep": "local", "path": str(target)},
        )

    out = asyncio.run(_run())
    assert _tool_is_error(out) is False
    blocks = _tool_blocks(out)
    assert len(blocks) == 2, blocks
    types = [getattr(b, "type", None) for b in blocks]
    assert types == ["text", "image"]
    text = getattr(blocks[0], "text", "") or ""
    b64 = getattr(blocks[1], "data", "") or ""
    assert base64.b64decode(b64) == _PNG
    assert b64 not in text
    assert "type=image" in text
    assert "mime_type=image/png" in text
    mime = getattr(blocks[1], "mime_type", None)
    assert mime == "image/png"
    if hasattr(out, "structured_content"):
        assert not out.structured_content
    dump = getattr(out, "model_dump", None)
    if callable(dump):
        wire = dump(by_alias=True)
        assert wire.get("isError") in (False, None)
        assert not wire.get("structuredContent")
        img = wire["content"][1]
        assert img["type"] == "image"
        assert img.get("mimeType") == "image/png"
        assert base64.b64decode(img["data"]) == _PNG
        assert img["data"] not in wire["content"][0].get("text", "")


def test_fs_read_truncated_image_is_error_without_image(tmp_path: Path) -> None:
    """Truncated image read is isError with no image content block."""
    target = tmp_path / "big.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    target.write_bytes(payload)

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool(
            "fs",
            {
                "op": "read",
                "ep": "local",
                "path": str(target),
                "max_bytes": 20,
            },
        )

    out = asyncio.run(_run())
    assert _tool_is_error(out) is True
    blocks = _tool_blocks(out)
    types = [getattr(b, "type", None) for b in blocks]
    assert "image" not in types
    assert all(t == "text" for t in types)
    text = _tool_text(out)
    b64 = base64.b64encode(payload).decode("ascii")
    assert b64 not in text
    assert "code=READ_LIMIT_EXCEEDED" in text
    if hasattr(out, "structured_content"):
        assert not out.structured_content


def test_fs_read_text_is_text_only(tmp_path: Path) -> None:
    """Ordinary text read stays a single text content block."""
    target = tmp_path / "note.txt"
    target.write_text("hello world\n", encoding="utf-8")

    async def _run() -> object:
        mcp = create_server()
        return await mcp.call_tool(
            "fs",
            {"op": "read", "ep": "local", "path": str(target)},
        )

    out = asyncio.run(_run())
    assert _tool_is_error(out) is False
    blocks = _tool_blocks(out)
    assert len(blocks) == 1, blocks
    assert getattr(blocks[0], "type", None) == "text"
    text = _tool_text(out)
    assert "hello world" in text
    if hasattr(out, "structured_content"):
        assert not out.structured_content


def test_exec_fs_ps_timeout_semantics_in_tool_descriptions() -> None:
    """list_tools descriptions state wall-clock != remote cancel + close+reopen.

    Agents treat timeout as SSH-style remote kill; the MCP surface must
    say (1) timeout = local wait wall-clock, (2) WinRM cannot guarantee
    remote cancel, (3) repeated timeouts -> endpoint close then open.
    """

    async def _run() -> dict[str, str]:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return {
            name: (tools[name].description or "")
            for name in ("exec", "fs", "ps")
        }

    descs = asyncio.run(_run())
    for name, desc in descs.items():
        lower = desc.lower()
        assert "wall-clock" in lower, (name, desc)
        # WinRM cannot cancel the remote command; timeout is local wait only.
        assert (
            "remote cancel" in lower
            or "cannot guarantee" in lower
            or "not a remote kill" in lower
        ), (name, desc)
        assert (
            "close+reopen" in lower
            or "close then open" in lower
        ), (name, desc)
        assert "hardeni" not in lower, (name, desc)
        assert "redact" not in lower, (name, desc)
        # timeout=0 is invalid; only omit/None is unlimited.
        assert "0/omit" not in desc, (name, desc)
        assert "0 = unlimited" not in lower, (name, desc)
        assert "0=unlimited" not in lower, (name, desc)
        if name in ("exec", "ps"):
            assert "INVALID_ARG" in desc, (name, desc)
            assert "unlimited" in lower, (name, desc)
            assert "omit" in lower or "none" in lower, (name, desc)


def test_ps_mcp_invoke_forwards_timeout_to_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ps invoke with timeout reaches Core ps_ops.run.

    Monkeypatch Core so no real runspace is used; assert the handler
    forwards timeout and still passes timeout=None when omitted.
    """
    from mcp_remote_control.core import ps_ops
    from mcp_remote_control.core.result import OpResult

    captured: dict[str, object] = {}

    def _capture_run(**kwargs: object) -> OpResult:
        captured.update(kwargs)
        return OpResult(
            kind="ps",
            status="ok",
            fields={
                "op": kwargs.get("op") or "invoke",
                "id": kwargs.get("id") or "ps_01",
                "exit": 0,
            },
            body="ok",
        )

    monkeypatch.setattr(ps_ops, "run", _capture_run)

    async def _run_with() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool(
                "ps",
                {
                    "op": "invoke",
                    "id": "ps_01",
                    "script": "$x=1",
                    "timeout": 12.5,
                },
            )
        )

    text = asyncio.run(_run_with())

    assert captured.get("op") == "invoke", captured
    assert captured.get("id") == "ps_01", captured
    assert captured.get("script") == "$x=1", captured
    assert captured.get("timeout") == 12.5, captured
    assert text.startswith("@ps ok"), text

    captured.clear()

    async def _run_omit() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool(
                "ps",
                {"op": "invoke", "id": "ps_02", "script": "$y=2"},
            )
        )

    text_omit = asyncio.run(_run_omit())

    assert captured.get("op") == "invoke", captured
    assert captured.get("id") == "ps_02", captured
    assert "timeout" in captured, captured
    assert captured.get("timeout") is None, captured
    assert text_omit.startswith("@ps ok"), text_omit


def test_ps_tool_description_lists_timeout() -> None:
    """``ps`` tool description mentions optional timeout for invoke."""

    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["ps"].description or ""

    desc = asyncio.run(_run())
    lower = desc.lower()
    assert "timeout" in desc
    assert "INVALID_ARG" in desc
    assert "0/omit" not in desc
    assert "0 = unlimited" not in lower
    assert "0=unlimited" not in lower
    assert "unlimited" in lower
    assert "omit" in lower or "none" in lower


def test_console_tool_routes_device_to_path_behavioral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP console open with ``device=`` (no ``path``) forwards ``path=`` to Core.

    The handler does ``path=path or device`` and must not also pass ``device=``.
    ``console_ops.run`` is monkeypatched so no real serial open is attempted.
    """
    from mcp_remote_control.core import console_ops
    from mcp_remote_control.core.result import OpResult

    captured: dict[str, object] = {}

    def _capture_run(**kwargs: object) -> OpResult:
        captured.update(kwargs)
        return OpResult(
            kind="console",
            status="ok",
            fields={
                "op": kwargs.get("op") or "open",
                "id": "con_1",
                "path": kwargs.get("path"),
                "baud": kwargs.get("baud") or 115200,
            },
        )

    monkeypatch.setattr(console_ops, "run", _capture_run)

    async def _run() -> str:
        mcp = create_server()
        return _tool_text(
            await mcp.call_tool("console", {"op": "open", "device": "COM9"})
        )

    text = asyncio.run(_run())

    assert captured.get("path") == "COM9", captured
    # Do not also forward ``device=``: Core would ignore it once path is set.
    assert "device" not in captured, captured
    assert text.startswith("@console ok"), text
    assert "path=COM9" in text, text


# ---------------------------------------------------------------------------
# Per-tool param pass-through: MCP kwargs must reach Core and Agent text.
# ---------------------------------------------------------------------------


def test_mcp_tool_param_pass_through_representative_payloads() -> None:
    """Each tool callable with a representative multi-kwarg payload - no
    param mis-map crash, and the documented kwargs that the MCP handler
    forwards to Core surface in the Agent track where the Core renders them.

    The reflected-value assertions pin the actual shell<->Core forwarding
    (e.g. ``exec`` handler's ``cwd`` kwarg -> Core ``cwd`` -> Agent ``cwd=...``),
    not just that the handler accepts the kwarg without erroring.
    """

    async def _run() -> dict[str, str]:
        mcp = create_server()
        # One representative payload per tool. Each uses named kwargs from the
        # documented MCP handler signature; values are no-side-effect (read
        # / list / no-such-id error path) so the test stays offline.
        payloads: dict[str, dict[str, object]] = {
            "endpoint": {"op": "list", "profile": None, "ep": None},
            "exec": {
                "ep": "local",
                "command": "echo hi",
                "argv": None,
                "script": None,
                "script_path": None,
                "runtime": None,
                "script_args": None,
                "cwd": "/tmp",
                "timeout": 5.0,
            },
            "fs": {
                "op": "list",
                "ep": "local",
                "path": "/tmp",
                "content": None,
                "local": None,
                "recursive": False,
                "max_bytes": None,
            },
            "screen": {
                "op": "list",
                "ep": None,
                "id": None,
                "actions": None,
                "wait": None,
                "shot": None,
            },
            "ps": {
                "op": "invoke",
                "ep": "win",
                "id": "no-such-session",
                "script": "echo hi",
                "timeout": 5.0,
            },
            "console": {
                "op": "list",
                "path": None,
                "device": None,
                "baud": None,
                "id": None,
                "data": None,
                "data_b64": None,
                "newline": False,
                "mode": None,
                "n": None,
                "since": None,
                "contains": None,
                "context": None,
                "settle_ms": None,
                "max_lines": None,
                "with_seq": False,
            },
            "config": {"op": "list_profiles"},
        }
        results: dict[str, str] = {}
        for name, args in payloads.items():
            results[name] = _tool_text(await mcp.call_tool(name, args))
        return results

    results = asyncio.run(_run())
    assert set(results) == set(REGISTERED)

    for name, text in results.items():
        assert f"@{name}" in text, (name, text)

    # MCP-forwarded values must appear in the Agent text (not silently dropped).
    assert "cwd=" in results["exec"], "exec handler dropped cwd kwarg"
    assert "/tmp" in results["exec"], ("exec cwd=/tmp not reflected", results["exec"])
    assert "path=/tmp" in results["fs"], ("fs path=/tmp not reflected", results["fs"])
    # PS error path echoes the id we forwarded (PS_NOT_FOUND carries id=...).
    assert "id=no-such-session" in results["ps"], (
        "ps handler dropped id kwarg",
        results["ps"],
    )
    # screen list is the empty-registry ok path.
    assert results["screen"].startswith("@screen ok"), results["screen"]
    assert "n=0" in results["screen"], results["screen"]


def test_mcp_tool_handler_signatures_accept_documented_kwargs() -> None:
    """Static guard: every kwarg in the MCP handler signatures (per the
    MCPServer ``input_schema``) must be a name the Python handler declares.
    A future rename of a Core param that forgets to update the MCP handler
    would surface here as an input_schema mismatch (MCPServer derives properties
    from the handler signature).
    """

    async def _run() -> dict[str, set[str]]:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return {
            name: set(
                (
                    getattr(tool, "input_schema", None)
                    or getattr(tool, "inputSchema", None)
                    or {}
                ).get("properties", {}).keys()
            )
            for name, tool in tools.items()
        }

    schema_props = asyncio.run(_run())
    expected_props = {
        "endpoint": {"op", "profile", "ep"},
        "exec": {
            "ep",
            "command",
            "argv",
            "script",
            "script_path",
            "runtime",
            "script_args",
            "cwd",
            "timeout",
        },
        "fs": {"op", "ep", "path", "content", "local", "recursive", "max_bytes"},
        "screen": {
            "op",
            "ep",
            "id",
            "actions",
            "wait",
            "shot",
            "cwd",
            "cols",
            "rows",
            "shell",
        },
        "ps": {"op", "ep", "id", "script", "timeout"},
        "console": {
            "op",
            "path",
            "device",
            "baud",
            "id",
            "data",
            "data_b64",
            "newline",
            "mode",
            "n",
            "since",
            "contains",
            "context",
            "settle_ms",
            "max_lines",
            # Peer console code page for the byte->text boundary; a serial
            # device has no profile to probe, so the operator supplies it.
            "encoding",
            "with_seq",
        },
        "config": {
            "op",
            "action",
            "name",
            "transport",
            "host",
            "port",
            "username",
            "label",
            "auth",
            "ssh",
            "winrm",
            "defaults",
            "caps",
            "body",
            "content",
        },
    }
    assert set(schema_props) == set(expected_props)
    for name, props in expected_props.items():
        assert schema_props[name] == props, (
            f"{name} input_schema drifted: expected {props}, got {schema_props[name]}"
        )


# ---------------------------------------------------------------------------
# config put_profile: winrm/defaults/caps must reach Core, not only input_schema.
# ---------------------------------------------------------------------------


def test_mcp_config_put_profile_winrm_defaults_caps_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ``config put_profile`` with winrm/defaults/caps round-trips into profile TOML.

    Write+readback against a tmpdir ``MRC_HOME`` (no monkeypatch of
    ``config_ops.run``). Dropping ``winrm=`` / ``defaults=`` / ``caps=``
    from the handler would omit the matching TOML tables.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()

    async def _run() -> dict[str, str]:
        mcp = create_server()

        async def _call(args: dict[str, object]) -> str:
            return _tool_text(await mcp.call_tool("config", args))

        texts: dict[str, str] = {}
        texts["ensure"] = await _call({"op": "ensure_home"})
        texts["put"] = await _call(
            {
                "op": "put_profile",
                "name": "win",
                "transport": "winrm",
                "host": "h",
                "username": "u",
                "winrm": {"scheme": "https"},
                "defaults": {"cwd": "C:/x"},
                "caps": {"ps": True},
            }
        )
        texts["get"] = await _call({"op": "get_profile", "name": "win"})
        return texts

    texts = asyncio.run(_run())

    assert "@config ok" in texts["ensure"], texts["ensure"]
    put_text = texts["put"]
    assert "@config ok" in put_text, put_text
    assert "name=win" in put_text, put_text

    assert (home / "profiles" / "win.toml").is_file()
    toml_text = (home / "profiles" / "win.toml").read_text()
    assert "[winrm]" in toml_text, toml_text
    assert 'scheme = "https"' in toml_text, toml_text
    assert "[defaults]" in toml_text, toml_text
    # Header alone is not enough: [defaults] with a missing cwd would still
    # match while dropping the caller's value.
    assert 'cwd = "C:/x"' in toml_text, toml_text
    assert "[caps]" in toml_text, toml_text
    assert "ps = true" in toml_text, toml_text

    # get_profile Agent text must also surface the forwarded winrm/defaults/caps.
    get_text = texts["get"]
    assert get_text.startswith("@config ok"), get_text
    assert "winrm.scheme" in get_text, get_text
    assert "winrm.scheme=https" in get_text, get_text
    assert "defaults.cwd" in get_text, get_text
    assert "defaults.cwd=C:/x" in get_text, get_text
    assert "caps.ps" in get_text, get_text


def test_mcp_config_notes_write_read_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ``config op=notes`` write/stat/read round-trip on a tmp MRC_HOME.

    Notes is a config op, not a separate tool. Body is returned only on read;
    stat carries ``bytes=`` without the notes text. write/append/prepend
    require an existing profile (same contract as Core).
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    body = "## dirs\n/opt/app\n"

    async def _run() -> dict[str, str]:
        mcp = create_server()
        tools = await mcp.list_tools()
        names = [t.name for t in tools]

        async def _call(args: dict[str, object]) -> str:
            return _tool_text(await mcp.call_tool("config", args))

        texts: dict[str, str] = {"listed": ",".join(names)}
        texts["ensure"] = await _call({"op": "ensure_home"})
        texts["put"] = await _call(
            {"op": "put_profile", "name": "box", "transport": "local"}
        )
        texts["write"] = await _call(
            {
                "op": "notes",
                "action": "write",
                "name": "box",
                "content": body,
            }
        )
        texts["stat"] = await _call(
            {"op": "notes", "action": "stat", "name": "box"}
        )
        texts["read"] = await _call(
            {"op": "notes", "action": "read", "name": "box"}
        )
        return texts

    texts = asyncio.run(_run())
    assert texts["listed"].split(",") == REGISTERED
    assert "@config ok" in texts["ensure"], texts["ensure"]
    assert "@config ok" in texts["put"], texts["put"]

    write_text = texts["write"]
    assert write_text.startswith("@config ok"), write_text
    assert "action=write" in write_text, write_text
    assert "name=box" in write_text, write_text
    assert "bytes=" in write_text, write_text
    assert (home / "notes" / "box.md").read_text(encoding="utf-8") == body
    # write response is status fields, not the notes body.
    assert "/opt/app" not in write_text
    assert "## dirs" not in write_text

    stat_text = texts["stat"]
    assert stat_text.startswith("@config ok"), stat_text
    assert "action=stat" in stat_text, stat_text
    assert "bytes=" in stat_text, stat_text
    assert "/opt/app" not in stat_text, stat_text
    assert "## dirs" not in stat_text, stat_text

    read_text = texts["read"]
    assert read_text.startswith("@config ok"), read_text
    assert "action=read" in read_text, read_text
    assert "/opt/app" in read_text, read_text
    assert "## dirs" in read_text, read_text


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate unlink only for a non-root POSIX user",
)
def test_mcp_config_delete_profile_notes_cleanup_failure_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed notes cleanup must reach the wire as ``isError``, not a crash.

    The profile was deleted before the notes unlink failed, so the tool text
    reports that and keeps the absolute config-home path out of the message.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()

    async def _run() -> tuple[bool, str]:
        mcp = create_server()

        async def _call(args: dict[str, object]) -> object:
            return await mcp.call_tool("config", args)

        await _call({"op": "ensure_home"})
        await _call({"op": "put_profile", "name": "box", "transport": "local"})
        await _call(
            {"op": "notes", "action": "write", "name": "box", "content": "keep-me"}
        )
        os.chmod(home / "notes", 0o500)
        try:
            out = await _call({"op": "delete_profile", "name": "box"})
        finally:
            os.chmod(home / "notes", 0o700)
        return _tool_is_error(out), _tool_text(out)

    is_error, text = asyncio.run(_run())
    assert is_error, text
    assert "CONFIG_WRITE_FAILED" in text, text
    assert "notes/box.md" in text, text
    assert str(home.resolve()) not in text, text
    # The profile was deleted; only the notes cleanup failed.
    assert not (home / "profiles" / "box.toml").exists()
    assert (home / "notes" / "box.md").is_file()
