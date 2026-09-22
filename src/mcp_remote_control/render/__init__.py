"""Output rendering: Agent semantic text track + compact JSON machine track.

Pure helpers with no MCP dependency. Core builds :class:`OpResult` values;
this package formats them for hosts and the CLI.
"""

from mcp_remote_control.render.core import (
    render_agent_text,
    render_json,
)
from mcp_remote_control.render.redact import REDACTED, redact_mapping, redact_string

__all__ = [
    "REDACTED",
    "redact_mapping",
    "redact_string",
    "render_agent_text",
    "render_json",
]
