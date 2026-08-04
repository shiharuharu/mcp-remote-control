"""MCP tool surface: host five + independent console (scheme A)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control import mcp_server as mcp_server_mod
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.mcp_server import TOOL_NAMES, create_server, tool_names
from mcp_remote_control.serial.registry import reset_serial_registry

EXPECTED = ["endpoint", "exec", "fs", "screen", "ps", "console", "config"]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


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
    assert names == EXPECTED
    assert list(TOOL_NAMES) == EXPECTED
    assert set(names) == set(EXPECTED)
    assert len(names) == 7


def test_tool_names_no_extras() -> None:
    assert "profiles" not in tool_names()
    assert "win_registry" not in tool_names()
    assert len(tool_names()) == len(set(tool_names()))


def test_fastmcp_list_tools_matches() -> None:
    async def _run() -> list[str]:
        mcp = create_server()
        tools = await mcp.list_tools()
        return [t.name for t in tools]

    listed = asyncio.run(_run())
    assert listed == EXPECTED
    assert set(listed) == set(EXPECTED)


def test_endpoint_tool_call_returns_agent_text() -> None:
    async def _run() -> str:
        mcp = create_server()
        out = await mcp.call_tool("endpoint", {"op": "list"})
        if isinstance(out, tuple):
            blocks = out[0]
        else:
            blocks = out
        texts = []
        for b in blocks:
            text = getattr(b, "text", None)
            if text is not None:
                texts.append(text)
        return "\n".join(texts)

    text = asyncio.run(_run())
    first = text.splitlines()[0] if text else ""
    assert first.startswith("@endpoint ")
    assert " ok" in first or " error" in first
    assert not text.lstrip().startswith("{")


def test_tools_unstructured_agent_text_no_result_json_shell() -> None:
    """MCP wire: plain content text, no FastMCP structuredContent {result:…}."""

    async def _run() -> None:
        mcp = create_server()
        tools = await mcp.list_tools()
        for t in tools:
            schema = getattr(t, "outputSchema", None)
            assert schema is None, f"{t.name} still has outputSchema={schema!r}"

        out = await mcp.call_tool("config", {"op": "home"})
        # Unstructured: list of TextContent only (not (blocks, structured_dict))
        if isinstance(out, tuple):
            blocks, structured = out[0], out[1] if len(out) > 1 else None
            assert structured is None or structured == {}, structured
        else:
            blocks = out
        texts = [getattr(b, "text", "") or "" for b in blocks]
        text = "\n".join(texts)
        assert text.lstrip().startswith("@config")
        assert not text.lstrip().startswith("{")
        assert '"result"' not in text.split("\n", 1)[0]

    asyncio.run(_run())


def test_console_tool_list_returns_console_kind() -> None:
    async def _run() -> str:
        mcp = create_server()
        out = await mcp.call_tool("console", {"op": "list"})
        blocks = out[0] if isinstance(out, tuple) else out
        texts = [getattr(b, "text", "") or "" for b in blocks]
        return "\n".join(texts)

    text = asyncio.run(_run())
    assert "@console" in text
    assert "ok" in text.splitlines()[0] or "error" in text.splitlines()[0]
    # Must not be disguised as endpoint
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
            out = await mcp.call_tool(name, args)
            blocks = out[0] if isinstance(out, tuple) else out
            texts = [getattr(b, "text", "") or "" for b in blocks]
            results[name] = "\n".join(texts)
        return results

    results = asyncio.run(_run())
    assert set(results) == set(EXPECTED)
    for name, text in results.items():
        assert f"@{name}" in text, (name, text)
        assert "ok" in text.splitlines()[0] or "error" in text.splitlines()[0]


# ---------------------------------------------------------------------------
# Param pass-through / description coverage on the MCP surface
# ---------------------------------------------------------------------------


def test_config_tool_description_lists_winrm_and_caps() -> None:
    """``config`` tool description must mention winrm/defaults/caps."""
    async def _run() -> str:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return tools["config"].description or ""

    desc = asyncio.run(_run())
    assert "winrm" in desc
    assert "caps" in desc
    assert "defaults" in desc


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


def test_console_tool_passes_path_or_device_only() -> None:
    """The console MCP handler must NOT pass a redundant ``device=`` to Core.

    Core's ``open_console`` reads ``path or device``; if MCP passes both
    ``path=path or device`` and ``device=device or path``, ``device=`` is
    always ignored (path always set). The fix drops the redundant line so
    a caller's distinct ``device=`` cannot silently be ignored. Assert via
    source inspection: ``path=path or device`` present, ``device=device or path``
    absent. (Secondary pin — the behavioral assertion lives in
    ``test_console_tool_routes_device_to_path_behavioral``.)
    """
    src = inspect.getsource(mcp_server_mod)
    assert "path=path or device" in src
    assert "device=device or path" not in src


def test_console_tool_routes_device_to_path_behavioral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral pin: the console MCP handler forwards ``device=`` to Core's
    ``path=`` (it does ``path=path or device`` and drops ``device=``).

    Source-inspection (``test_console_tool_passes_path_or_device_only``) is a
    brittle secondary guard; this drives the actual registered FastMCP
    ``console`` tool via ``mcp.call_tool`` with ``device=COM9`` (no ``path``)
    and asserts what reaches ``console_ops.run``: Core receives
    ``path="COM9"`` and NO ``device=`` kwarg (a caller's distinct ``device=``
    cannot silently be ignored). ``console_ops.run`` is monkeypatched so no
    real serial open is attempted (mcp_server.py is not edited).
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
        out = await mcp.call_tool("console", {"op": "open", "device": "COM9"})
        blocks = out[0] if isinstance(out, tuple) else out
        texts = [getattr(b, "text", "") or "" for b in blocks]
        return "\n".join(texts)

    text = asyncio.run(_run())

    # Core received path=COM9 (device routed to path).
    assert captured.get("path") == "COM9", captured
    # The MCP handler must NOT forward a redundant ``device=`` kwarg to Core
    # (Core would otherwise ignore it because path is always set).
    assert "device" not in captured, captured
    # And the rendered Agent track reflects the routed path back to the host.
    assert text.startswith("@console ok"), text
    assert "path=COM9" in text, text


# ---------------------------------------------------------------------------
# Per-tool param pass-through — one representative payload per tool that
# exercises the named kwargs the MCP handler forwards to Core ops.run. Guards
# against a future shell↔Core param rename (a mis-mapped kwarg would either
# crash the handler or silently drop a reflected value in the Agent text).
# ---------------------------------------------------------------------------


def test_mcp_tool_param_pass_through_representative_payloads() -> None:
    """Each tool callable with a representative multi-kwarg payload — no
    param mis-map crash, and the documented kwargs that the MCP handler
    forwards to Core surface in the Agent track where the Core renders them.

    The reflected-value assertions pin the actual shell↔Core forwarding
    (e.g. ``exec`` handler's ``cwd`` kwarg → Core ``cwd`` → Agent ``cwd=…``),
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
            out = await mcp.call_tool(name, args)
            blocks = out[0] if isinstance(out, tuple) else out
            texts = [getattr(b, "text", "") or "" for b in blocks]
            results[name] = "\n".join(texts)
        return results

    results = asyncio.run(_run())
    assert set(results) == set(EXPECTED)

    for name, text in results.items():
        # No param mis-map crash: every tool returns a well-formed Agent
        # track line for its kind (no TypeError / AttributeError surfaced).
        assert f"@{name}" in text, (name, text)
        assert "TypeError" not in text, (name, text)
        assert "AttributeError" not in text, (name, text)
        assert "takes" not in text.lower() or "argument" not in text.lower(), (
            name,
            text,
        )

    # Reflected forwarding assertions: the value the MCP handler receives
    # must be visible in the Agent track, proving it was forwarded to Core
    # (and Core rendered it back). Catches silent param-name drift.
    assert "cwd=" in results["exec"], "exec handler dropped cwd kwarg"
    assert "/tmp" in results["exec"], ("exec cwd=/tmp not reflected", results["exec"])
    assert "path=/tmp" in results["fs"], ("fs path=/tmp not reflected", results["fs"])
    # PS error path echoes the id we forwarded (PS_NOT_FOUND carries id=…).
    assert "id=no-such-session" in results["ps"], (
        "ps handler dropped id kwarg",
        results["ps"],
    )
    # screen list is the empty-registry ok path.
    assert results["screen"].startswith("@screen ok"), results["screen"]
    assert "n=0" in results["screen"], results["screen"]


def test_mcp_tool_handler_signatures_accept_documented_kwargs() -> None:
    """Static guard: every kwarg in the MCP handler signatures (per the
    FastMCP ``inputSchema``) must be a name the Python handler declares.
    A future rename of a Core param that forgets to update the MCP handler
    would surface here as an inputSchema mismatch (FastMCP derives properties
    from the handler signature).
    """

    async def _run() -> dict[str, set[str]]:
        mcp = create_server()
        tools = {t.name: t for t in await mcp.list_tools()}
        return {
            name: set((t.inputSchema or {}).get("properties", {}).keys())
            for name, t in tools.items()
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
        "fs": {"op", "ep", "path", "content", "local", "recursive"},
        "screen": {"op", "ep", "id", "actions", "wait", "shot"},
        "ps": {"op", "ep", "id", "script"},
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
            "with_seq",
        },
        "config": {
            "op",
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
            f"{name} inputSchema drifted: expected {props}, got {schema_props[name]}"
        )


# ---------------------------------------------------------------------------
# MCP-level behavioral pin for config put_profile winrm/defaults/caps
# forward path. ``test_mcp_tool_param_pass_through_representative_payloads``
# uses ``config: {op: "list_profiles"}`` (a no-arg op), so the winrm/defaults/caps
# forward path in the MCP config handler is not behaviorally exercised at the
# MCP level — a future refactor dropping ``winrm=winrm`` from the MCP forward
# would pass the inputSchema pin + the list_profiles test. This drives the
# real FastMCP config tool with op=put_profile + winrm/defaults/caps dicts,
# writes a real profile in a tmpdir MRC_HOME, and reads the TOML back (mirroring
# the CLI round-trip at test_cli_tools.py::test_config_put_profile_roundtrip_with_winrm_json
# but on the MCP↔Core surface).
# ---------------------------------------------------------------------------


def test_mcp_config_put_profile_winrm_defaults_caps_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ``config put_profile`` with winrm/defaults/caps → profile TOML round-trips all three blocks.

    Uses a real write+readback against a tmpdir ``MRC_HOME`` (no monkeypatch of
    ``config_ops.run``) for maximum behavioral strength: the call traverses
    FastMCP tool → ``mcp_server.config`` handler → ``config_ops.run`` →
    ``store.put_profile`` → TOML on disk, then ``get_profile`` reads it back.
    A future refactor that drops ``winrm=winrm`` / ``defaults=defaults`` /
    ``caps=caps`` from the MCP handler forward would silently lose the
    corresponding ``[winrm]`` / ``[defaults]`` / ``[caps]`` block and fail the
    TOML assertions below.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()

    async def _run() -> dict[str, str]:
        mcp = create_server()

        async def _call(args: dict[str, object]) -> str:
            out = await mcp.call_tool("config", args)
            blocks = out[0] if isinstance(out, tuple) else out
            return "\n".join(getattr(b, "text", "") or "" for b in blocks)

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

    # ensure_home set up the layout (no error); put_profile succeeded.
    assert "@config ok" in texts["ensure"], texts["ensure"]
    put_text = texts["put"]
    assert "@config ok" in put_text, put_text
    assert "name=win" in put_text, put_text

    # Real write+readback: the profile TOML round-trips all three blocks.
    assert (home / "profiles" / "win.toml").is_file()
    toml_text = (home / "profiles" / "win.toml").read_text()
    assert "[winrm]" in toml_text, toml_text
    assert 'scheme = "https"' in toml_text, toml_text
    assert "[defaults]" in toml_text, toml_text
    # Defaults block must round-trip the actual value, not just the header
    # (regression: a [defaults] header with a missing cwd would satisfy a
    # weak ``"[defaults]" in toml_text`` while dropping the caller's value).
    assert 'cwd = "C:/x"' in toml_text, toml_text
    assert "[caps]" in toml_text, toml_text
    assert "ps = true" in toml_text, toml_text

    # get_profile Agent track surfaces winrm/defaults/caps (forward + readback
    # on the observe side too). Mirrors the CLI get-profile assertions.
    get_text = texts["get"]
    assert get_text.startswith("@config ok"), get_text
    assert "winrm.scheme" in get_text, get_text
    assert "winrm.scheme=https" in get_text, get_text
    assert "defaults.cwd" in get_text, get_text
    assert "defaults.cwd=C:/x" in get_text, get_text
    assert "caps.ps" in get_text, get_text
