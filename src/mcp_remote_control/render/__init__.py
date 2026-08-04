"""Output rendering: Agent semantic text track + compact JSON machine track.

Pure helpers with no MCP dependency. Core builds :class:`OpResult` values;
this package formats them for hosts and the CLI.
"""

from mcp_remote_control.render.core import (
    VALID_KINDS,
    VALID_STATUSES,
    render_agent_text,
    render_json,
)
from mcp_remote_control.render.redact import REDACTED, redact_mapping, redact_string

__all__ = [
    "REDACTED",
    "VALID_KINDS",
    "VALID_STATUSES",
    "redact_mapping",
    "redact_string",
    "render_agent_text",
    "render_json",
]
