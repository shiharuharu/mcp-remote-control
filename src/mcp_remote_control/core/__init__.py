"""Core service APIs shared by CLI and MCP (no MCP dependency).

Submodules (``endpoint_ops``, ``exec_ops``, ``fs_ops``, ``screen_ops``,
``ps_ops``, ``console_ops``, ``config_ops``) implement tool ops and return
structured :class:`OpResult` values for dual-track render.
"""

from mcp_remote_control.core.result import OpResult, render_result

__all__ = [
    "OpResult",
    "render_result",
]
