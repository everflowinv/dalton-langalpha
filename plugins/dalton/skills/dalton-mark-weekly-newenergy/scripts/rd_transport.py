#!/usr/bin/env python3
"""researchdata transport for the Mark weekly skill.

Copied from plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:
the sandbox wrapper resolver and callers, and the route item extraction
(about lines 1000-1060). This directory does not import that file.

The HTTP bridge path has been removed. There is no DALTON_RESEARCHDATA_URL
override and no _call_bridge_http. The only call path is the sandbox wrapper
module tools.researchdata plus mcp_client.

Bash exports PYTHONPATH and PTC_WORKSPACE_CONFIG before it runs this skill's
script, so `from tools import researchdata` works the same way it does inside
an ExecuteCode cell.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime
from typing import Any

RESEARCHDATA_SERVER = "researchdata"
# The only call path is the sandbox wrapper module tools.researchdata (the same
# module ExecuteCode uses). If it is missing, collect reports
# researchdata_transport_unconfigured once at startup.
RESEARCHDATA_TRANSPORT_UNCONFIGURED = "researchdata_transport_unconfigured"
_ITEM_KEYS = ("results", "items", "documents", "rows")


def clean_val(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_val(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_val(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:  # noqa: BLE001 - numpy scalars without .item() stay as-is
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def resolve_researchdata_transport() -> dict[str, Any]:
    """Pick the researchdata call path.

    Uses the sandbox wrapper only. Returns {"mode": "wrapper"|None, "call":
    callable or None, "error": reason when mode is None}.
    """
    try:
        from tools import researchdata as wrapper  # type: ignore
    except Exception as exc:  # noqa: BLE001 - any import failure means the wrapper is unavailable
        return {
            "mode": None,
            "call": None,
            "error": f"无法导入沙箱包装模块 tools.{RESEARCHDATA_SERVER}（{str(exc)[:200]}）",
        }
    try:
        import mcp_client as mcp_client_module  # type: ignore
    except Exception as exc:  # noqa: BLE001 - a missing runtime cannot call MCP
        return {
            "mode": None,
            "call": None,
            "error": f"沙箱缺少 mcp_client 运行时（{str(exc)[:200]}），包装模块无法调用 MCP",
        }
    config_path = os.environ.get("PTC_WORKSPACE_CONFIG", ".agents/tools/mcp_client_config.json")
    try:
        with open(config_path, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except OSError as exc:
        return {
            "mode": None,
            "call": None,
            "error": f"无法读取工作区 MCP 配置（{config_path}）：{str(exc)[:160]}",
        }
    except ValueError as exc:
        return {
            "mode": None,
            "call": None,
            "error": f"工作区 MCP 配置不可解析（{config_path}）：{str(exc)[:160]}",
        }
    if not isinstance(cfg, dict) or ("servers" in cfg and not isinstance(cfg["servers"], dict)):
        return {
            "mode": None,
            "call": None,
            "error": (
                f"工作区 MCP 配置结构不对（{config_path}）："
                "顶层须为 JSON 对象、servers 须为对象映射"
            ),
        }
    enabled = set((cfg.get("servers") or {}).keys())
    if RESEARCHDATA_SERVER not in enabled:
        return {
            "mode": None,
            "call": None,
            "error": f"工作区未启用 {RESEARCHDATA_SERVER} MCP 服务器（{config_path}）",
        }
    flagged = getattr(mcp_client_module, "call_mcp_tool_with_error_flag", None)
    if callable(flagged):
        return {"mode": "wrapper", "call": flagged, "error": ""}
    call_fn = getattr(wrapper, "_call_mcp_tool", None)
    if not callable(call_fn):
        return {
            "mode": None,
            "call": None,
            "error": f"tools.{RESEARCHDATA_SERVER} 模块缺少 _call_mcp_tool",
        }
    return {"mode": "wrapper", "call": call_fn, "error": ""}


def _extract_non_empty_error(obj: dict[str, Any]) -> str | None:
    raw_err = obj.get("error")
    if raw_err is None or raw_err is False:
        return None
    if isinstance(raw_err, str):
        stripped = raw_err.strip()
        return stripped or None
    if isinstance(raw_err, (dict, list)):
        return json.dumps(raw_err, ensure_ascii=False) if raw_err else None
    text = str(raw_err).strip()
    return text or None


def _extract_mcp_content_error_text(obj: dict[str, Any]) -> str | None:
    content = obj.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                return text
    return None


def _normalize_dict_result(
    value: dict[str, Any],
    *,
    outer_is_error: bool = False,
    default_error: str = "wrapper_call_failed",
) -> dict[str, Any]:
    structured = value.get("structuredContent") if isinstance(value.get("structuredContent"), dict) else None
    err_from_value = _extract_non_empty_error(value) or (
        _extract_non_empty_error(structured) if structured else None
    )
    mcp_error = bool(
        outer_is_error
        or value.get("isError") is True
        or value.get("is_error") is True
        or (
            structured
            and (structured.get("isError") is True or structured.get("is_error") is True)
        )
    )
    payload_ok = value.get("ok") if "ok" in value else (structured.get("ok") if structured else None)
    raw_status = value.get("status") or (structured.get("status") if structured else None)
    status_str = str(raw_status or "").strip()
    is_ok = (
        not mcp_error
        and err_from_value is None
        and payload_ok is not False
        and status_str not in ("error", "failed")
    )
    if is_ok:
        err_msg = None
        final_status = status_str or "ok"
    else:
        reason = value.get("reason") or (structured.get("reason") if structured else None)
        message = value.get("message") or (structured.get("message") if structured else None)
        content_err = _extract_mcp_content_error_text(value)
        err_msg = str(
            err_from_value
            or reason
            or message
            or content_err
            or (status_str if status_str and status_str != "ok" else "")
            or default_error
        )
        final_status = status_str if status_str and status_str != "ok" else "error"
    return {"ok": is_ok, "status": final_status, "error": err_msg, "data": value}


def _call_wrapper(call_fn: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one researchdata tool through the sandbox wrapper.

    call_mcp_tool_with_error_flag returns (is_error, value). is_error is the
    only signal when the unpacked value has no error field.
    """
    try:
        result = call_fn(RESEARCHDATA_SERVER, tool_name, arguments)
    except Exception as exc:  # noqa: BLE001 - one MCP failure is recorded, the script keeps going
        return {"ok": False, "status": "exception", "error": str(exc)[:300]}
    is_error = False
    value = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_error, value = result
    if isinstance(value, str):
        raw_text = value.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "status": "empty_wrapper_content",
                "error": raw_text[:300] if raw_text else "wrapper returned non-JSON text",
            }
    if isinstance(value, dict):
        return _normalize_dict_result(value, outer_is_error=is_error, default_error="wrapper_call_failed")
    return {
        "ok": False,
        "status": "invalid_format",
        "error": f"wrapper returned non-dict result ({type(value).__name__}): {str(value)[:200]}",
        "data": clean_val(value),
    }


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """Call one researchdata tool through the sandbox wrapper.

    `timeout` stays on the signature so existing callers match. The wrapper
    does not apply it. The HTTP bridge override has been removed.
    """
    del timeout
    transport = resolve_researchdata_transport()
    if transport["mode"] == "wrapper":
        return _call_wrapper(transport["call"], tool_name, arguments)
    return {
        "ok": False,
        "status": RESEARCHDATA_TRANSPORT_UNCONFIGURED,
        "error": transport["error"] or RESEARCHDATA_TRANSPORT_UNCONFIGURED,
    }


def find_result_items(data: Any, _seen: set[int] | None = None) -> list[dict[str, Any]] | None:
    """Return the result list, or None when the payload has no result container.

    An empty list is a container (the call succeeded and matched nothing).
    `_declared_sources` and `_sources` are source labels, not result rows.
    Walks `results` / `items` / `documents` / `rows`, `structuredContent`, and
    nested `data`, including JSON packed into MCP content text.
    """
    if _seen is None:
        _seen = set()
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return None
    marker = id(data)
    if marker in _seen:
        return None
    _seen.add(marker)
    for key in _ITEM_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    structured = data.get("structuredContent")
    if isinstance(structured, dict):
        for key in ("results", "data", "items", "documents"):
            value = structured.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = find_result_items(structured, _seen)
        if nested is not None:
            return nested
    inner = data.get("data")
    if isinstance(inner, list):
        return [item for item in inner if isinstance(item, dict)]
    if isinstance(inner, dict):
        nested = find_result_items(inner, _seen)
        if nested is not None:
            return nested
        found = _items_from_content_blocks(inner.get("content"), _seen)
        if found is not None:
            return found
    return _items_from_content_blocks(data.get("content"), _seen)


def _items_from_content_blocks(content: Any, seen: set[int]) -> list[dict[str, Any]] | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            parsed = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        nested = find_result_items(parsed, seen)
        if nested is not None:
            return nested
    return None


def extract_route_items(data: Any) -> list[dict[str, Any]]:
    """List form of find_result_items. Missing containers become an empty list."""
    found = find_result_items(data)
    return found if found is not None else []
