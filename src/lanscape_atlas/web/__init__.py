"""FastAPI application: JSON API now; web UI, topology and MCP mount onto it later."""

from lanscape_atlas.web.app import create_app

__all__ = ["create_app"]
