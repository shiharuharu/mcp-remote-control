"""MCP server surface — thin shells over Core ops.

Handler path only: parse arguments → call Core → render Agent text.
No business logic lives here.

MCP tools/call result shape used here:

- ``content``: ``[{type: "text", text: "<Agent track>"}]``
- ``isError`` / ``is_error``: false|true
- ``structuredContent`` / ``structured_content``: omitted for Agent-track tools

``MCPServer`` (SDK v2; was FastMCP in v1) defaults for ``-> str`` invent
``output_schema {result: string}`` and wrap as structured content. We force
unstructured output (``structured_output=False``) so Agent text is the sole
payload and hosts do not show a JSON ``{"result":…}`` shell.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_remote_control.core import (
    config_ops,
    console_ops,
    endpoint_ops,
    exec_ops,
    fs_ops,
    ps_ops,
    screen_ops,
)
from mcp_remote_control.core.result import render_result

# Host tools + serial console + self-config (agent manages MRC_HOME).
TOOL_NAMES: tuple[str, ...] = (
    "endpoint",
    "exec",
    "fs",
    "screen",
    "ps",
    "console",
    "config",
)

# Agent-track text only — no ``{"result": ...}`` structured wrap.
# Pass as an explicit kwarg (not ``**dict``) so type checkers bind
# ``structured_output: bool`` correctly on MCPServer.tool.
_STRUCTURED_OUTPUT_AGENT = False


def tool_names() -> list[str]:
    """Return the exact registered MCP tool names (pure; no stdio server)."""
    return list(TOOL_NAMES)


def create_server(*, name: str = "mcp-remote-control") -> MCPServer[Any]:
    """Build MCPServer with host tools, console, and config."""
    mcp = MCPServer(
        name,
        instructions=(
            "mcp-remote-control tools: "
            "endpoint|exec|fs|screen|ps (hosts); "
            "console (serial, not PTY); "
            "config (ONLY way to manage profiles/secrets — do NOT shell cat/ls/edit "
            "~/.config or MRC_HOME files). "
            "Bootstrap: config ensure_home → put_secret → put_profile → "
            "endpoint open profile=. If unsure about auth JSON, config op=help. "
            "Hosts: endpoint open → exec/fs/screen/ps with ep=. "
            "Console: list → open path=<device> → send/views → close. "
            "Secret bodies only via put_secret; never returned by reads."
        ),
    )
    register_tools(mcp)
    return mcp


def register_tools(mcp: MCPServer[Any]) -> None:
    """Register MCP tools (idempotent per instance)."""

    @mcp.tool(
        name="endpoint",
        description=(
            "Host endpoint lifecycle only: op=list|open|close. "
            "open uses profile=; close uses ep=. "
            "Not for serial ports — use the console tool."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def endpoint(
        op: str = "list",
        profile: str | None = None,
        ep: str | None = None,
    ) -> str:
        result = endpoint_ops.run(op=op, profile=profile, ep=ep)
        return render_result(result)

    @mcp.tool(
        name="exec",
        description=(
            "Remote non-interactive exec on ep=. "
            "Provide command= or argv= or script=/script_path=; "
            "optional runtime=, script_args=, cwd=, timeout=."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def exec_tool(
        ep: str | None = None,
        command: str | None = None,
        argv: list[str] | None = None,
        script: str | None = None,
        script_path: str | None = None,
        runtime: str | None = None,
        script_args: list[str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> str:
        result = exec_ops.run(
            ep=ep,
            command=command,
            argv=argv,
            script=script,
            script_path=script_path,
            runtime=runtime,
            script_args=script_args,
            cwd=cwd,
            timeout=timeout,
        )
        return render_result(result)

    @mcp.tool(
        name="fs",
        description=(
            "Filesystem on ep=: op=list|stat|read|write|put|get|mkdir|rm; "
            "path= absolute preferred."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def fs(
        op: str,
        ep: str | None = None,
        path: str | None = None,
        content: str | None = None,
        local: str | None = None,
        recursive: bool | None = None,
    ) -> str:
        result = fs_ops.run(
            op=op,
            ep=ep,
            path=path,
            content=content,
            local=local,
            recursive=recursive,
        )
        return render_result(result)

    @mcp.tool(
        name="screen",
        description=(
            "Interactive PTY screen on host ep=: op=open|send|close|list. "
            "Loop: open → frame → send(actions) → frame → close. "
            "Not for serial hardware — use the console tool."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def screen(
        op: str,
        ep: str | None = None,
        id: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        wait: dict[str, Any] | None = None,
        shot: bool | None = None,
    ) -> str:
        result = screen_ops.run(
            op=op,
            ep=ep,
            id=id,
            actions=actions,
            wait=wait,
            shot=shot,
        )
        return render_result(result)

    @mcp.tool(
        name="ps",
        description=(
            "Persistent PowerShell runspace (WinRM): op=open|invoke|close. "
            "open needs ep=; invoke/close need id=; invoke uses script=."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def ps(
        op: str,
        ep: str | None = None,
        id: str | None = None,
        script: str | None = None,
    ) -> str:
        result = ps_ops.run(op=op, ep=ep, id=id, script=script)
        return render_result(result)

    @mcp.tool(
        name="console",
        description=(
            "Serial Console (embedded device link on this host; not PTY/SSH). "
            "op=list|open|send|views|close|sessions. "
            "list: system device= names (do not scan /dev or drivers). "
            "open: path= or device= from list, optional baud=, max_lines= "
            "(default huge buffer; background capture starts immediately). "
            "send: id= + data= or data_b64=, optional newline=. "
            "views: id= + mode=tail|since|contains; n=; since=; contains=; "
            "context=; settle_ms=; with_seq=; queries capture buffer (not driver recv). "
            "close/sessions: id= / list open sessions."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def console(
        op: str = "list",
        path: str | None = None,
        device: str | None = None,
        baud: int | None = None,
        id: str | None = None,
        data: str | None = None,
        data_b64: str | None = None,
        newline: bool = False,
        mode: str | None = None,
        n: int | None = None,
        since: int | None = None,
        contains: str | None = None,
        context: int | None = None,
        settle_ms: int | None = None,
        max_lines: int | None = None,
        with_seq: bool = False,
    ) -> str:
        result = console_ops.run(
            op=op,
            path=path or device,
            baud=baud,
            id=id,
            data=data,
            data_b64=data_b64,
            newline=newline,
            mode=mode,
            n=n,
            since=since,
            contains=contains,
            context=context,
            settle_ms=settle_ms,
            max_lines=max_lines,
            with_seq=with_seq,
        )
        return render_result(result)

    @mcp.tool(
        name="config",
        description=(
            "Complete self-config for profiles. Do NOT shell-edit config files. "
            "ops: help|home|ensure_home|get|list_profiles|get_profile|put_profile|"
            "delete_profile|put_secret|list_secrets. "
            "Password is NOT private — write it inline: "
            'put_profile name=lab transport=ssh host=… username=… '
            'auth={"method":"password","password":"<plain>"} '
            'ssh={"known_hosts":"none"} then endpoint open profile=lab. '
            "SSH key still uses put_secret + key_path=secrets/…. "
            "WinRM: auth password=plain + winrm={scheme,auth}; optional defaults/caps. "
            "op=help for recipes."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def config(
        op: str = "home",
        name: str | None = None,
        transport: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        label: str | None = None,
        auth: dict[str, Any] | str | None = None,
        ssh: dict[str, Any] | str | None = None,
        winrm: dict[str, Any] | str | None = None,
        defaults: dict[str, Any] | str | None = None,
        caps: dict[str, Any] | str | None = None,
        body: str | None = None,
        content: str | None = None,
    ) -> str:
        result = config_ops.run(
            op=op,
            name=name,
            transport=transport,
            host=host,
            port=port,
            username=username,
            label=label,
            auth=auth,
            ssh=ssh,
            winrm=winrm,
            defaults=defaults,
            caps=caps,
            body=body,
            content=content,
        )
        return render_result(result)


def main() -> None:
    """stdio MCP entry (optional; hosts may import create_server)."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
