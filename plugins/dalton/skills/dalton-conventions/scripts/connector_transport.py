#!/usr/bin/env python3
"""Unified sandbox connector transport helper for Dalton skills.

Resolves dedicated Batch 3 MCP connectors (`dalton_sec`, `dalton_macro`,
`dalton_company_wiki`, `dalton_research_all`, `dalton_guidepoint`,
`dalton_sales_notes`, `dalton_tmtb`, `dalton_funda_ai`) when mounted in the
workspace MCP config **and** exposing the requested tool and parameters, with
transparent fallback to the legacy `researchdata` wrapper or explicit
`DALTON_RESEARCHDATA_URL` HTTP bridge.
"""

from __future__ import annotations

import inspect
import json
import os
import urllib.request
from typing import Any

ROUTE_TO_DEDICATED_SERVER: dict[str, str] = {
    "sec": "dalton_sec",
    "macro": "dalton_macro",
    "fred": "dalton_macro",
    "treasury": "dalton_macro",
    "company-wiki": "dalton_company_wiki",
    "guidepoint": "dalton_guidepoint",
    "sales-notes": "dalton_sales_notes",
    "tmtb": "dalton_tmtb",
    "funda-ai": "dalton_funda_ai",
    "all": "dalton_research_all",
    "sales-mail": "dalton_research_all",
}

SEC_OPERATION_TO_TOOL: dict[str, str] = {
    "list-filings": "list_filings",
    "list_filings": "list_filings",
    "facts": "get_concept_facts",
    "get_concept_facts": "get_concept_facts",
    "company-facts": "get_company_facts",
    "get_company_facts": "get_company_facts",
    "search-concepts": "search_concepts",
    "search_concepts": "search_concepts",
    "smart-facts": "search_smart_facts",
    "search_smart_facts": "search_smart_facts",
    "financials": "get_financials",
    "get_financials": "get_financials",
    "ratios": "get_ratios",
    "get_ratios": "get_ratios",
    "search-filing": "search_filing_text",
    "search-text": "search_filing_text",
    "search_filing_text": "search_filing_text",
    "read-item": "read_filing_item",
    "read_filing_item": "read_filing_item",
    "attachments": "get_filing_attachments",
    "get_filing_attachments": "get_filing_attachments",
    "8k-events": "list_eight_k_events",
    "eight-k": "list_eight_k_events",
    "list_eight_k_events": "list_eight_k_events",
    "insiders": "list_insider_transactions",
    "insider": "list_insider_transactions",
    "list_insider_transactions": "list_insider_transactions",
}

# Pre-cutover Everflow mounts baseline `dalton_sec` (only `get_company_facts`
# and `list_filings` without `form`). Do not route extended SEC ops or
# single-form `list_filings(form=...)` to `dalton_sec` unless the mounted
# server's tool list / wrapper confirms the tool and parameter exist.
_BASELINE_DALTON_SEC_TOOLS: dict[str, frozenset[str]] = {
    "get_company_facts": frozenset(
        {"ticker", "concepts", "period", "restatement", "as_of", "fiscal_year_end_month"}
    ),
    "list_filings": frozenset({"ticker", "forms", "limit", "as_of"}),
}

MACRO_OPERATION_TO_TOOL: dict[str, str] = {
    "fred": "get_fred_series",
    "get_fred_series": "get_fred_series",
    "tga": "get_treasury_data",
    "debt": "get_treasury_data",
    "treasury": "get_treasury_data",
    "get_treasury_data": "get_treasury_data",
    "catalog": "list_macro_catalog",
    "list_macro_catalog": "list_macro_catalog",
}

# Research connectors whose MCP tools take top-level `(query, document, start, end, limit, params)`
# rather than flattened filter kwargs.
_NESTED_PARAMS_SERVERS: frozenset[str] = frozenset(
    {
        "dalton_company_wiki",
        "dalton_sales_notes",
        "dalton_tmtb",
        "dalton_funda_ai",
        "dalton_research_all",
    }
)

_RESEARCH_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"query", "ticker", "document", "start", "end", "limit"}
)

LEGACY_SERVER = "researchdata"


def load_mcp_servers_config(config_path: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """Read `servers` mapping from workspace `.agents/tools/mcp_client_config.json`."""
    path = config_path or os.environ.get(
        "PTC_WORKSPACE_CONFIG", ".agents/tools/mcp_client_config.json"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except OSError as exc:
        return None, f"无法读取工作区 MCP 配置（{path}）：{str(exc)[:160]}"
    except ValueError as exc:
        return None, f"工作区 MCP 配置不可解析（{path}）：{str(exc)[:160]}"
    if not isinstance(cfg, dict) or ("servers" in cfg and not isinstance(cfg["servers"], dict)):
        return (
            None,
            f"工作区 MCP 配置结构不对（{path}）：顶层须为 JSON 对象、servers 须为对象映射",
        )
    return dict(cfg.get("servers") or {}), ""


def load_enabled_mcp_servers(config_path: str | None = None) -> tuple[set[str] | None, str]:
    """Read enabled MCP server names from workspace `.agents/tools/mcp_client_config.json`."""
    servers, err = load_mcp_servers_config(config_path=config_path)
    if servers is None:
        return None, err
    return set(servers.keys()), ""


def _tool_spec_supports_params(tool_spec: Any, required_params: set[str]) -> bool:
    if not required_params:
        return True
    if not isinstance(tool_spec, dict):
        return True
    schema = tool_spec.get("inputSchema") or tool_spec.get("input_schema") or tool_spec.get("parameters")
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        props = set(schema["properties"].keys())
        return required_params.issubset(props)
    return True


def _check_config_tools_metadata(
    server_entry: Any,
    tool_name: str,
    required_params: set[str],
) -> bool | None:
    """Inspect explicit `tools` metadata inside `mcp_client_config.json` when present.

    Returns True/False if `tools` metadata is present and decisive, or None when absent.
    """
    if not isinstance(server_entry, dict):
        return None
    tools_meta = server_entry.get("tools")
    if tools_meta is None:
        return None
    if isinstance(tools_meta, dict):
        if tool_name not in tools_meta:
            return False
        return _tool_spec_supports_params(tools_meta[tool_name], required_params)
    if isinstance(tools_meta, list):
        for item in tools_meta:
            if isinstance(item, str) and item == tool_name:
                return True
            if isinstance(item, dict) and item.get("name") == tool_name:
                return _tool_spec_supports_params(item, required_params)
        return False
    return None


def _check_wrapper_tool_signature(
    server_name: str,
    tool_name: str,
    required_params: set[str],
) -> bool | None:
    """Inspect generated sandbox wrapper `tools.<server_name>` for `tool_name` and parameters.

    Returns True/False when the wrapper module can be imported, or None when unavailable.
    """
    try:
        wrapper = __import__(f"tools.{server_name}", fromlist=[tool_name])
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(wrapper, tool_name, None)
    if not callable(fn):
        return False
    if not required_params:
        return True
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    accepted = set(sig.parameters.keys())
    return required_params.issubset(accepted)


def server_supports_tool(
    server_name: str,
    tool_name: str | None,
    *,
    required_params: set[str] | list[str] | tuple[str, ...] | None = None,
    servers_cfg: dict[str, Any] | None = None,
) -> bool:
    """Return True only when `server_name` is mounted AND exposes `tool_name` + `required_params`."""
    if not servers_cfg or server_name not in servers_cfg:
        return False
    if not tool_name:
        return True

    req_set = {str(k) for k in (required_params or ()) if k}
    server_entry = servers_cfg.get(server_name)

    # 1. Explicit tool manifest in config (if present)
    cfg_verdict = _check_config_tools_metadata(server_entry, tool_name, req_set)
    if cfg_verdict is not None:
        return cfg_verdict

    # 2. Generated sandbox wrapper module `tools.<server_name>` (ground truth in live sandboxes)
    wrapper_verdict = _check_wrapper_tool_signature(server_name, tool_name, req_set)
    if wrapper_verdict is not None:
        return wrapper_verdict

    # 3. Fail-closed when neither explicit `tools` metadata nor generated wrapper
    #    `tools.<server_name>` confirms the requested tool & parameters exist:
    #    - Whenever `researchdata` is also mounted (mixed / pre-cutover workspace),
    #      never guess that an unverified dedicated server exposes the tool.
    #    - For `dalton_sec`, also never assume extended tools/params without discovery.
    if LEGACY_SERVER in servers_cfg:
        return False

    if server_name == "dalton_sec":
        ext_env = os.environ.get("DALTON_SEC_EXTENDED_TOOLS", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not ext_env:
            base_params = _BASELINE_DALTON_SEC_TOOLS.get(tool_name)
            if base_params is None:
                return False
            if not req_set.issubset(base_params):
                return False

    return True


def _resolve_mcp_callable(server_name: str) -> tuple[Any, str]:
    try:
        import mcp_client as mcp_client_module  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"沙箱缺少 mcp_client 运行时（{str(exc)[:200]}）"
    flagged = getattr(mcp_client_module, "call_mcp_tool_with_error_flag", None)
    if callable(flagged):
        return flagged, ""
    try:
        wrapper = __import__(f"tools.{server_name}", fromlist=["_call_mcp_tool"])
    except Exception as exc:  # noqa: BLE001
        return None, f"无法导入沙箱包装模块 tools.{server_name}（{str(exc)[:200]}）"
    call_fn = getattr(wrapper, "_call_mcp_tool", None)
    if not callable(call_fn):
        return None, f"tools.{server_name} 模块缺少 _call_mcp_tool"
    return call_fn, ""


def resolve_connector_transport(
    route: str,
    *,
    prefer_dedicated: bool = True,
    config_path: str | None = None,
    tool_name: str | None = None,
    required_params: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Resolve the active transport for a logical research/finance route.

    Priority:
      1. Explicit `DALTON_RESEARCHDATA_URL` HTTP override -> `mode="bridge"`.
      2. When `prefer_dedicated=True` and the route's dedicated MCP server is mounted
         AND exposes `tool_name` + `required_params` -> `mode="dedicated"`.
      3. Legacy `researchdata` MCP server when mounted -> `mode="legacy"`, `server="researchdata"`.
      4. Dedicated server fallback if `prefer_dedicated=False` and `researchdata` is absent.
    """
    bridge_url = os.environ.get("DALTON_RESEARCHDATA_URL", "").strip()
    if bridge_url:
        return {
            "mode": "bridge",
            "server": LEGACY_SERVER,
            "call": bridge_url,
            "error": "",
        }

    servers_cfg, err = load_mcp_servers_config(config_path=config_path)
    if servers_cfg is None:
        return {"mode": None, "server": None, "call": None, "error": err}

    dedicated_server = ROUTE_TO_DEDICATED_SERVER.get(route)
    order: list[tuple[str, str]] = []
    if prefer_dedicated and dedicated_server:
        order.append(("dedicated", dedicated_server))
    order.append(("legacy", LEGACY_SERVER))
    if not prefer_dedicated and dedicated_server:
        order.append(("dedicated", dedicated_server))

    for mode, server_name in order:
        if server_name not in servers_cfg:
            continue
        if mode == "dedicated" and not server_supports_tool(
            server_name,
            tool_name,
            required_params=required_params,
            servers_cfg=servers_cfg,
        ):
            continue
        call_fn, call_err = _resolve_mcp_callable(server_name)
        if callable(call_fn):
            return {
                "mode": mode,
                "server": server_name,
                "call": call_fn,
                "error": "",
            }
        return {"mode": None, "server": server_name, "call": None, "error": call_err}

    wanted = dedicated_server or LEGACY_SERVER
    return {
        "mode": None,
        "server": None,
        "call": None,
        "error": f"工作区未启用 {wanted} 或 {LEGACY_SERVER} MCP 服务器",
    }


def _normalize_wrapper_output(result: Any) -> dict[str, Any]:
    is_error = False
    value = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_error, value = result
    if isinstance(value, str):
        raw = value.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "status": "invalid_format",
                "error": raw[:300] if raw else "empty_response",
            }
    if not isinstance(value, dict):
        return {
            "ok": False,
            "status": "invalid_format",
            "error": f"non-dict response ({type(value).__name__})",
            "data": value,
        }
    ok_val = value.get("ok")
    status_val = str(value.get("status") or "").strip()
    err_val = value.get("error")
    is_ok = (
        not is_error
        and not bool(value.get("isError"))
        and ok_val is not False
        and not (isinstance(err_val, str) and err_val.strip())
        and status_val not in ("error", "failed")
    )
    return {
        "ok": is_ok,
        "status": (status_val or "ok") if is_ok else (status_val or "error"),
        "error": None if is_ok else str(err_val or value.get("message") or "connector_call_failed"),
        "data": value,
    }


def _call_bridge_http(
    bridge_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: int = 120,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    req = urllib.request.Request(
        bridge_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "exception", "error": str(exc)[:300]}
    if isinstance(raw, dict) and raw.get("error"):
        return {"ok": False, "status": "rpc_error", "error": str(raw.get("error"))}
    res = raw.get("result") or {}
    content = res.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("text"):
        return _normalize_wrapper_output((bool(res.get("isError", False)), content[0]["text"]))
    return _normalize_wrapper_output((bool(res.get("isError", False)), res))


def call_connector_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call a specific MCP server tool via sandbox mcp_client wrapper."""
    call_fn, err = _resolve_mcp_callable(server_name)
    if not callable(call_fn):
        return {"ok": False, "status": "unconfigured", "error": err}
    try:
        raw = call_fn(server_name, tool_name, arguments)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "exception", "error": str(exc)[:300]}
    return _normalize_wrapper_output(raw)


def _prepare_sec_dedicated_args(operation: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool_name = SEC_OPERATION_TO_TOOL.get(operation, operation)
    args: dict[str, Any] = {}
    nested = arguments.get("params")
    if isinstance(nested, dict):
        for nk, nv in nested.items():
            if nv is not None:
                args[nk] = nv
    for k, v in arguments.items():
        if k in ("route", "operation", "params") or v is None:
            continue
        args[k] = v
    # Legacy `sec` `list-filings` defaulted omitted `form` to "10-Q" (data_gateway.py:173).
    if tool_name == "list_filings" and "form" not in args and "forms" not in args:
        args["form"] = "10-Q"
    return tool_name, args


def _prepare_research_dedicated_args(
    route: str,
    operation: str,
    arguments: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Build `(server_name, tool_name, call_args)` matching each dedicated server's real schema."""
    server_name = ROUTE_TO_DEDICATED_SERVER.get(route, "")
    tool_name = "sales_mail" if route == "sales-mail" else operation

    if server_name in _NESTED_PARAMS_SERVERS:
        nested: dict[str, Any] = {}
        raw_nested = arguments.get("params")
        if isinstance(raw_nested, dict):
            for nk, nv in raw_nested.items():
                if nv is not None:
                    nested[nk] = nv
        call_args: dict[str, Any] = {}
        if route == "sales-mail" and operation:
            call_args["operation"] = operation
        for key, value in arguments.items():
            if key in ("route", "operation", "params") or value is None:
                continue
            if key in _RESEARCH_TOP_LEVEL_KEYS:
                call_args[key] = value
            else:
                nested.setdefault(key, value)
        if nested:
            call_args["params"] = nested
        return server_name, tool_name, call_args

    # Guidepoint uses flat kwargs (`query`, `start_date`, `end_date`, `size`, etc.)
    flat_args: dict[str, Any] = {}
    raw_nested = arguments.get("params")
    if isinstance(raw_nested, dict):
        for nk, nv in raw_nested.items():
            if nv is not None:
                flat_args[nk] = nv
    for key, value in arguments.items():
        if key in ("route", "operation", "params") or value is None:
            continue
        flat_args[key] = value
    if route == "guidepoint":
        if "start" in flat_args and "start_date" not in flat_args:
            flat_args["start_date"] = flat_args.pop("start")
        if "end" in flat_args and "end_date" not in flat_args:
            flat_args["end_date"] = flat_args.pop("end")
        if "limit" in flat_args and "size" not in flat_args:
            flat_args["size"] = flat_args.pop("limit")
    return server_name, tool_name, flat_args


def resolve_dedicated_for_legacy_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    config_path: str | None = None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Map a legacy `(tool_name, arguments)` call to a dedicated Batch 3 MCP tool when supported.

    Checks that the target server is mounted in workspace MCP config AND exposes
    the mapped tool and required parameters. Returns `(None, None, {})` when the
    call should continue to the legacy `researchdata` wrapper.
    """
    route = str(arguments.get("route") or "").strip().lower()
    operation = str(arguments.get("operation") or "").strip()

    if tool_name == "finance" and route == "sec":
        if operation not in SEC_OPERATION_TO_TOOL:
            return None, None, {}
        mapped_tool, mapped_args = _prepare_sec_dedicated_args(operation, arguments)
        servers_cfg, _ = load_mcp_servers_config(config_path=config_path)
        if not server_supports_tool(
            "dalton_sec",
            mapped_tool,
            required_params=set(mapped_args.keys()),
            servers_cfg=servers_cfg,
        ):
            return None, None, {}
        return "dalton_sec", mapped_tool, mapped_args

    if tool_name == "macro" and route in ("fred", "treasury", "macro"):
        op_clean = (operation or route).strip().lower()
        mapped_tool = MACRO_OPERATION_TO_TOOL.get(op_clean)
        if not mapped_tool:
            return None, None, {}
        mapped_args = {
            k: v for k, v in arguments.items() if k not in ("route", "operation") and v is not None
        }
        if op_clean in ("tga", "debt") and "dataset" not in mapped_args:
            mapped_args["dataset"] = op_clean
        servers_cfg, _ = load_mcp_servers_config(config_path=config_path)
        if not server_supports_tool(
            "dalton_macro",
            mapped_tool,
            required_params=set(mapped_args.keys()),
            servers_cfg=servers_cfg,
        ):
            return None, None, {}
        return "dalton_macro", mapped_tool, mapped_args

    if tool_name == "research" and route in ROUTE_TO_DEDICATED_SERVER and operation:
        server, mapped_tool, mapped_args = _prepare_research_dedicated_args(
            route, operation, arguments
        )
        if not server or not mapped_tool:
            return None, None, {}
        servers_cfg, _ = load_mcp_servers_config(config_path=config_path)
        if not server_supports_tool(
            server,
            mapped_tool,
            required_params=set(mapped_args.keys()),
            servers_cfg=servers_cfg,
        ):
            return None, None, {}
        return server, mapped_tool, mapped_args

    return None, None, {}


def call_sec_operation(
    operation: str,
    arguments: dict[str, Any],
    *,
    prefer_dedicated: bool = True,
    timeout: int = 120,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Invoke an SEC operation on `dalton_sec` (when tool & params are supported) or legacy `researchdata.finance`."""
    tool_name, args = _prepare_sec_dedicated_args(operation, arguments)
    transport = resolve_connector_transport(
        "sec",
        prefer_dedicated=prefer_dedicated,
        config_path=config_path,
        tool_name=tool_name,
        required_params=set(args.keys()),
    )
    if transport["mode"] == "dedicated":
        try:
            raw = transport["call"](transport["server"], tool_name, args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    legacy_args = {"route": "sec", "operation": operation, **arguments}
    if transport["mode"] == "bridge":
        return _call_bridge_http(transport["call"], "finance", legacy_args, timeout=timeout)
    if transport["mode"] == "legacy":
        try:
            raw = transport["call"](LEGACY_SERVER, "finance", legacy_args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    return {"ok": False, "status": "unconfigured", "error": transport["error"]}


def call_macro_operation(
    operation: str,
    arguments: dict[str, Any],
    *,
    prefer_dedicated: bool = True,
    timeout: int = 120,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Invoke a macro operation on `dalton_macro` (when supported) or legacy `researchdata.macro`."""
    op_clean = str(operation or "").strip().lower()
    route_key = "treasury" if op_clean in ("tga", "debt", "treasury", "get_treasury_data") else "fred"
    tool_name = MACRO_OPERATION_TO_TOOL.get(operation, operation)
    ded_args = {k: v for k, v in arguments.items() if k not in ("route", "operation") and v is not None}
    if op_clean in ("tga", "debt") and "dataset" not in ded_args:
        ded_args["dataset"] = op_clean
    transport = resolve_connector_transport(
        route_key,
        prefer_dedicated=prefer_dedicated,
        config_path=config_path,
        tool_name=tool_name,
        required_params=set(ded_args.keys()),
    )
    if transport["mode"] == "dedicated":
        try:
            raw = transport["call"](transport["server"], tool_name, ded_args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    legacy_args: dict[str, Any] = {
        k: v for k, v in arguments.items() if k not in ("route", "operation") and v is not None
    }
    legacy_args["route"] = route_key
    if route_key == "treasury":
        legacy_args["operation"] = "treasury"
        if op_clean in ("tga", "debt") and "dataset" not in legacy_args:
            legacy_args["dataset"] = op_clean
    if transport["mode"] == "bridge":
        return _call_bridge_http(transport["call"], "macro", legacy_args, timeout=timeout)
    if transport["mode"] == "legacy":
        try:
            raw = transport["call"](LEGACY_SERVER, "macro", legacy_args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    return {"ok": False, "status": "unconfigured", "error": transport["error"]}


def call_research_route(
    route: str,
    operation: str,
    arguments: dict[str, Any] | None = None,
    *,
    prefer_dedicated: bool = True,
    timeout: int = 120,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Invoke a research connector operation on its dedicated MCP server or legacy `researchdata`."""
    args_in = dict(arguments or {})
    _srv, tool_name, ded_args = _prepare_research_dedicated_args(route, operation, args_in)
    transport = resolve_connector_transport(
        route,
        prefer_dedicated=prefer_dedicated,
        config_path=config_path,
        tool_name=tool_name,
        required_params=set(ded_args.keys()),
    )
    if transport["mode"] == "dedicated":
        try:
            raw = transport["call"](transport["server"], tool_name, ded_args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    legacy_args = {"route": route, "operation": operation, **args_in}
    if transport["mode"] == "bridge":
        return _call_bridge_http(transport["call"], "research", legacy_args, timeout=timeout)
    if transport["mode"] == "legacy":
        try:
            raw = transport["call"](LEGACY_SERVER, "research", legacy_args)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "exception", "error": str(exc)[:300]}
        return _normalize_wrapper_output(raw)
    return {"ok": False, "status": "unconfigured", "error": transport["error"]}
