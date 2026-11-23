"""Bootstrap helper so plugin MCP servers find ``mcp_servers.*`` modules.

Plugin MCP servers live in ``plugins/<name>/`` rather than ``mcp_servers/``,
so Python's default sys.path (the script's own directory) doesn't include
the repo root. Importing this module before ``mcp_servers._envelope`` etc.
adds the repo root to ``sys.path`` and re-exports ``MCPServer`` from the
canonical ``mcp_servers._bootstrap``.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp_servers._bootstrap import MCPServer  # noqa: E402,F401

__all__ = ["MCPServer"]
