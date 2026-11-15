"""Resolve and invoke Dalton sandbox ``tools.researchdata`` / ``mcp_client`` transport."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def resolve_researchdata_transport() -> tuple[str, Any]:
    """Return ``(kind, module)`` for ``tools.researchdata`` or ``mcp_client``, or raise RuntimeError."""
    candidates: list[str] = []
    env_dir = os.environ.get("MCP_CLIENT_DIR")
    if env_dir:
        candidates.append(env_dir)
    for static in ("/app/mcp_client", "/workspace/mcp_client"):
        if static not in candidates:
            candidates.append(static)
    cwd_mcp = str(Path.cwd() / "mcp_client")
    if cwd_mcp not in candidates:
        candidates.append(cwd_mcp)
    for item in candidates:
        if Path(item).is_dir() and item not in sys.path:
            sys.path.insert(0, item)

    try:
        import tools.researchdata as mod  # type: ignore[import-not-found]

        return "tools.researchdata", mod
    except ImportError:
        pass
    try:
        import mcp_client as mod  # type: ignore[import-not-found]

        return "mcp_client", mod
    except ImportError:
        pass
    raise RuntimeError(
        "researchdata sandbox transport is not available; set MCP_CLIENT_DIR or run inside Dalton sandbox"
    )


_ITEM_KEYS = ("results", "items", "documents", "rows", "posts")
_CONTAINER_KEYS = ("structuredContent", "data", "payload", "result")
_OUTER_META_KEYS = ("ok", "status", "error", "error_category")
_QUOTA_CATEGORY = "权限或额度"
_QUOTA_MESSAGES = ("额度已达上限", "无查看权限")


def _merge_outer_meta(parsed: dict[str, Any], outer: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parsed)
    for key in _OUTER_META_KEYS:
        if key in outer and outer[key] is not None and key not in merged:
            merged[key] = outer[key]
    return merged


def unwrap_bridge_response(raw: Any) -> dict[str, Any]:
    """Unwrap JSON-RPC / wrapper strings/dicts returned by ``tools.researchdata``."""
    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return _merge_outer_meta(parsed, raw)
                except json.JSONDecodeError:
                    pass
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_json", "raw": raw[:500]}
    return {"ok": False, "error": f"unexpected_response_type:{type(raw).__name__}"}


def _items_from_content_blocks(
    blocks: list[Any],
    keys: tuple[str, ...],
    depth: int,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        found = find_result_items(parsed, keys=keys, _depth=depth + 1)
        if found:
            collected.extend(found)
    return collected


def find_result_items(
    payload: Any,
    *,
    keys: tuple[str, ...] = _ITEM_KEYS,
    _depth: int = 0,
) -> list[dict[str, Any]]:
    """Extract row dicts from flat or nested bridge payloads (results/items/posts/structuredContent/data)."""
    if _depth > 8 or payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    for key in _CONTAINER_KEYS:
        nested = payload.get(key)
        if isinstance(nested, (dict, list)):
            found = find_result_items(nested, keys=keys, _depth=_depth + 1)
            if found:
                return found
    content = payload.get("content")
    if isinstance(content, list):
        return _items_from_content_blocks(content, keys, _depth)
    return []


def extract_route_items(
    payload: Any,
    *,
    keys: tuple[str, ...] = _ITEM_KEYS,
) -> list[dict[str, Any]]:
    """Unwrap JSON-RPC / MCP content wrappers and return extracted item dicts."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    unwrapped = unwrap_bridge_response(payload)
    return find_result_items(unwrapped, keys=keys)


def extract_nested_value(payload: Any, key: str, _depth: int = 0) -> Any:
    """Look up ``key`` at top level or inside ``structuredContent`` / ``data`` / ``payload`` / ``result``."""
    if _depth > 8 or not isinstance(payload, dict):
        return None
    if key in payload and payload[key] is not None:
        return payload[key]
    for container_key in _CONTAINER_KEYS:
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            found = extract_nested_value(nested, key, _depth=_depth + 1)
            if found is not None:
                return found
    content = payload.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                return extract_nested_value(parsed, key, _depth=_depth + 1)
            except json.JSONDecodeError:
                pass
    return None


def is_quota_or_auth_error(resp: Any) -> bool:
    """Return True when bridge response indicates quota exhaustion or missing permission."""
    if extract_nested_value(resp, "error_category") == _QUOTA_CATEGORY:
        return True
    blob = json.dumps(resp, ensure_ascii=False, default=str) if not isinstance(resp, str) else resp
    return any(msg in blob for msg in _QUOTA_MESSAGES)


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a ``researchdata`` tool (``research`` / ``community`` / ``finance`` / ...) synchronously."""
    kind, mod = resolve_researchdata_transport()
    if kind == "tools.researchdata":
        fn = getattr(mod, tool_name, None)
        if fn is None:
            raise RuntimeError(f"tools.researchdata has no attribute {tool_name!r}")
        raw = fn(**arguments)
        return unwrap_bridge_response(raw)

    flagged = getattr(mod, "call_mcp_tool_with_error_flag", None)
    call_fn = flagged if callable(flagged) else getattr(mod, "_call_mcp_tool", None)
    if not callable(call_fn):
        raise RuntimeError("mcp_client module exposes no call_mcp_tool_with_error_flag or _call_mcp_tool")
    result = call_fn("researchdata", tool_name, arguments)
    is_error = False
    value = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_error, value = result
    parsed = unwrap_bridge_response(value)
    if is_error and isinstance(parsed, dict) and parsed.get("ok") is not False:
        err_msg = parsed.get("error") or parsed.get("message") or str(value)[:300] or "mcp_tool_error"
        return {**parsed, "ok": False, "error": err_msg}
    return parsed
