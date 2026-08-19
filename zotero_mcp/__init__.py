"""Zotero MCP server — read access to a Zotero library for AI agents.

Importing this package has no side effects: no config is read, no client is built,
and nothing touches the network. Call :func:`create_server` to build a server, either
from the environment or from a :class:`ZoteroSettings` you construct yourself.

    from zotero_mcp import ZoteroSettings, create_server

    server = create_server(ZoteroSettings(api_key=..., library_id="123456"))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .settings import ConfigurationError, ZoteroSettings

if TYPE_CHECKING:  # pragma: no cover
    from fastmcp import FastMCP

__all__ = [
    "ConfigurationError",
    "ZoteroGateway",
    "ZoteroSettings",
    "create_server",
]

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """Defer imports that pull in fastmcp/pyzotero until actually needed."""
    if name == "create_server":
        from .server import create_server

        return create_server
    if name == "ZoteroGateway":
        from .gateway import ZoteroGateway

        return ZoteroGateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_server(settings: ZoteroSettings | None = None) -> FastMCP:
    """Convenience wrapper around :func:`create_server`."""
    from .server import create_server

    return create_server(settings)
