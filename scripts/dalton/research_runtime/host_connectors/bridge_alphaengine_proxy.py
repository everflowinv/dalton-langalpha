"""Batch 3 AlphaEngine federated leg forwarding proxy (Everflow host-only, excluded from Core).

Keeps the legacy bridge (`:8960/mcp`) as the single authoritative handler for AlphaEngine
(SQLite v4 cache, usage_file, circuit breaker) throughout Batch 3 (§6.4b (2) #1).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.dalton.capabilities import CapabilityId
from src.dalton.research_host.auth import CURRENT_CALLER_HEADERS
from src.dalton.research_host.connector_registry import ConnectorRegistry

ALPHAENGINE_ALLOWED_PARAMS: frozenset[str] = frozenset(
    {"search_in", "fresh", "sort", "meeting_scope"}
)


def read_alphaengine_usage_today(usage_file: Path | None) -> dict[str, Any]:
    """Read today's AlphaEngine document-read usage counter from JSON ledger."""
    if usage_file is None or not usage_file.is_file():
        return {"configured": usage_file is not None, "today_reads": 0}
    try:
        data = json.loads(usage_file.read_text(encoding="utf-8"))
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if isinstance(data, dict):
            if isinstance(data.get("days"), dict) and today_str in data["days"]:
                entry = data["days"][today_str]
                reads = int(entry.get("reads", 0)) if isinstance(entry, dict) else int(entry or 0)
                return {"configured": True, "today_reads": reads, "date": today_str}
            if data.get("date") == today_str:
                return {"configured": True, "today_reads": int(data.get("reads", 0)), "date": today_str}
        return {"configured": True, "today_reads": 0, "date": today_str}
    except Exception as exc:
        return {"configured": True, "error": type(exc).__name__}


class LegacyBridgeAlphaEngineProxy:
    """Forwards Batch 3 `all.search` AlphaEngine leg to the legacy bridge (`:8960/mcp`)."""

    def __init__(
        self,
        *,
        bridge_mcp_url: str,
        bridge_token: str = "",
        timeout_s: float = 45.0,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.bridge_mcp_url = str(bridge_mcp_url or "").strip()
        self.bridge_token = str(bridge_token or "").strip()
        self.timeout_s = float(timeout_s)
        self._client_factory = client_factory or httpx.AsyncClient

    async def search(
        self,
        query: str,
        start: str | None,
        end: str | None,
        limit: int,
        params: dict[str, Any],
        caller_key: str,
    ) -> dict[str, Any]:
        if not self.bridge_mcp_url:
            return {
                "ok": False,
                "status": "error",
                "reason": "Legacy bridge MCP URL not configured for AlphaEngine forwarding",
                "results": [],
                "count": 0,
            }
        arguments: dict[str, Any] = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
            "limit": min(max(1, int(limit)), 50),
        }
        if start:
            arguments["start"] = start
        if end:
            arguments["end"] = end
        ae_params: dict[str, Any] = {}
        for k in ("search_in", "fresh", "sort", "meeting_scope"):
            if k in params and params[k] is not None:
                ae_params[k] = params[k]
        if ae_params:
            arguments["params"] = ae_params

        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "research", "arguments": arguments},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.bridge_token:
            headers["Authorization"] = f"Bearer {self.bridge_token}"
        caller_hdrs = CURRENT_CALLER_HEADERS.get()
        if caller_hdrs:
            for hdr_name in ("x-dalton-caller-token", "x-dalton-caller-scope"):
                if caller_hdrs.get(hdr_name):
                    headers[hdr_name] = caller_hdrs[hdr_name]
        if caller_key and caller_key != "anonymous":
            headers["X-Dalton-Caller-Key"] = caller_key
        async with self._client_factory(timeout=self.timeout_s, trust_env=False) as client:
            resp = await client.post(self.bridge_mcp_url, json=rpc_body, headers=headers)
        if resp.status_code != 200:
            return {
                "ok": False,
                "status": "error",
                "reason": f"Legacy bridge HTTP {resp.status_code}",
                "results": [],
                "count": 0,
            }
        body = resp.json()
        if "error" in body:
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return {
                "ok": False,
                "status": "error",
                "reason": str(msg or "legacy bridge error"),
                "results": [],
                "count": 0,
            }
        res_obj = body.get("result") or {}
        payload = res_obj.get("structuredContent")
        if not isinstance(payload, dict):
            for block in res_obj.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        cand = json.loads(block["text"])
                        if isinstance(cand, dict):
                            payload = cand
                            break
                    except ValueError:
                        continue
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "status": "error",
                "reason": "Unrecognized AlphaEngine response from legacy bridge",
                "results": [],
                "count": 0,
            }
        inner_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        inner_sc = inner_data.get("structuredContent") if isinstance(inner_data.get("structuredContent"), dict) else {}
        rows = (
            inner_sc.get("results")
            if isinstance(inner_sc.get("results"), list)
            else (payload.get("results") if isinstance(payload.get("results"), list) else [])
        )
        ok = bool(payload.get("ok", not res_obj.get("isError", False)))
        return {
            "ok": ok,
            "status": str(payload.get("status") or ("ok" if ok else "error")),
            "results": rows,
            "count": len(rows),
            "cursor": inner_sc.get("cursor"),
            "has_more": bool(inner_sc.get("has_more", False)),
            "total_upstream": inner_sc.get("total_upstream"),
            "returned": len(rows),
            "_sources": payload.get("_sources") or [],
            **({"reason": str(payload.get("error") or payload.get("reason") or "")} if not ok else {}),
        }


def register_alphaengine_bridge_proxy(
    registry: ConnectorRegistry,
    *,
    bridge_mcp_url: str,
    bridge_token: str = "",
    usage_file: Path | None = None,
    proxy: LegacyBridgeAlphaEngineProxy | None = None,
) -> LegacyBridgeAlphaEngineProxy | None:
    """Register Batch 3 AlphaEngine federated forwarding leg and usage health field."""
    eff_proxy = proxy
    if eff_proxy is None and bridge_mcp_url:
        eff_proxy = LegacyBridgeAlphaEngineProxy(
            bridge_mcp_url=bridge_mcp_url,
            bridge_token=bridge_token,
        )
    if eff_proxy is not None:
        registry.register_federated_source(
            "alphaengine",
            CapabilityId.ALPHAENGINE,
            search_fn=eff_proxy.search,
            allowed_params=ALPHAENGINE_ALLOWED_PARAMS,
            top_level_block=True,
            fuse_local=False,
            provenance_identity="internal_note",
        )
    registry.register_health_field(
        "alphaengine_usage",
        lambda _caps: read_alphaengine_usage_today(usage_file),
        capability=CapabilityId.ALPHAENGINE,
    )
    registry.register_health_probe(
        "alphaengine_usage",
        lambda: read_alphaengine_usage_today(usage_file),
    )
    return eff_proxy
