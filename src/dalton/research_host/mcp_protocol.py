"""Streamable HTTP JSON-RPC 2.0 MCP protocol helpers for ``dalton-research-host``."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = "2025-03-26"


def jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def sanitize_declared_sources(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce §2.3 #3 provenance contract: keep non-empty `_sources`, omit empty `_declared_sources`."""
    out = dict(payload)
    ds = out.get("_declared_sources")
    if isinstance(ds, list) and len(ds) == 0:
        out.pop("_declared_sources", None)
    sources = out.get("_sources")
    if isinstance(sources, list) and len(sources) == 0:
        out.pop("_sources", None)
    return out


def mcp_tool_result(payload: dict[str, Any], *, is_error: bool | None = None) -> dict[str, Any]:
    """Wrap a connector payload in standard MCP `tools/call` result shape."""
    cleaned = sanitize_declared_sources(payload)
    err_flag = (not bool(cleaned.get("ok", True))) if is_error is None else bool(is_error)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(cleaned, ensure_ascii=False),
            }
        ],
        "structuredContent": cleaned,
        "isError": err_flag,
    }


def make_initialize_result(server_name: str, version: str = "0.1.0") -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": server_name, "version": version},
    }
