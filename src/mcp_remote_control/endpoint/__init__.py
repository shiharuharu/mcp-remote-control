"""Endpoint lifecycle package: capability matrix, registry, lazy connect."""

from __future__ import annotations

from mcp_remote_control.endpoint.caps import caps_for_transport, format_caps, merge_caps
from mcp_remote_control.endpoint.registry import (
    Endpoint,
    EndpointRegistry,
    ensure_endpoint,
    get_registry,
    list_known_profiles,
    reset_registry,
)

__all__ = [
    "Endpoint",
    "EndpointRegistry",
    "caps_for_transport",
    "ensure_endpoint",
    "format_caps",
    "get_registry",
    "list_known_profiles",
    "merge_caps",
    "reset_registry",
]
