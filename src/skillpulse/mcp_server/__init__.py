"""MCP server package (M5): the shared read-only tools over stdio and HTTP."""

from __future__ import annotations

from skillpulse.mcp_server.server import (
    SERVER_NAME,
    SERVER_VERSION,
    TAXONOMY_URI,
    AppContext,
    build_server,
    main,
)

__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "TAXONOMY_URI",
    "AppContext",
    "build_server",
    "main",
]
