"""Open-source research connector host (`python -m src.dalton.research_host`).

Provides Streamable HTTP MCP endpoints (`/mcp/<connector>`) and backend-only
admin ingest endpoints (`/ingest/*`) with strict 3-class credential isolation
and per-source capability enforcement.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
