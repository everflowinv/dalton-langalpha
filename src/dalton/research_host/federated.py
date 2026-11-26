"""Federated research connector (`dalton_research_all` at `/mcp/federated`).

Implements §0.2 #4/#6, §2.2 #2, §2.3 #1/#2, and §6.4 #3:
- Mounts whenever ANY internal research source capability is enabled.
- Enforces **per-source `effective_capabilities()`** checks on every `search`,
  `list`, `read` (`get_document`), and `sales_mail` call:
  - Disabled explicit sources or prefixes immediately return `capability_disabled`.
  - Unspecified `sources` fans out strictly across currently enabled sources.
- Batch 3 single-authority rule (§0.2 #6): when `alphaengine` is enabled, the
  federated `all` AlphaEngine leg forwards through the legacy bridge (`:8960/mcp`)
  rather than accessing `:8950` or its SQLite cache directly.
- Partial-failure contract (§2.3 #2): returns `"ok": true, "status": "partial"`,
  per-source `"source_status"`, and preserves `"alphaengine"` / `"guidepoint"`
  top-level summary blocks whenever any enabled leg times out (> `fanout_grace_s`)
  or fails while another enabled leg succeeds.
- Prefix `read` dispatch (§2.3 #1): `wiki:`, `mail:`, `digest:`, `tmtb:`,
  `funda:`, `image:` (verifying the image owner's capability!).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from src.dalton.capabilities import CapabilityId
from src.dalton.research_host.auth import CURRENT_CALLER_HEADERS
from src.dalton.research_host.connector_registry import ConnectorRegistry
from src.dalton.research_host.wiki_store import OpenWikiStore

logger = logging.getLogger("dalton-research-host.federated")

ALL_INTERNAL_RESEARCH_CAPABILITIES: tuple[CapabilityId, ...] = (
    CapabilityId.COMPANY_WIKI,
    CapabilityId.SALES_NOTES,
    CapabilityId.TMTB,
    CapabilityId.FUNDA_AI,
    CapabilityId.GUIDEPOINT,
    CapabilityId.ALPHAENGINE,
)

SOURCE_TO_CAPABILITY: dict[str, CapabilityId] = {
    "company-wiki": CapabilityId.COMPANY_WIKI,
    "sales-notes": CapabilityId.SALES_NOTES,
    "tmtb": CapabilityId.TMTB,
    "funda-ai": CapabilityId.FUNDA_AI,
    "guidepoint": CapabilityId.GUIDEPOINT,
    "alphaengine": CapabilityId.ALPHAENGINE,
}

PREFIX_TO_SOURCE: dict[str, str] = {
    "wiki": "company-wiki",
    "mail": "sales-notes",
    "digest": "sales-notes",
    "tmtb": "tmtb",
    "funda": "funda-ai",
}

SOURCE_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "company-wiki": frozenset({"company", "sector", "tag", "search_mode", "no_decay", "media", "indexed_since"}),
    "sales-notes": frozenset({"sender", "layer", "search_mode", "media"}),
    "tmtb": frozenset({"sender", "search_mode", "media"}),
    "funda-ai": frozenset({"sender", "search_mode", "media"}),
    "guidepoint": frozenset({"size", "keywords", "recency_bias", "start_date", "end_date"}),
    "alphaengine": frozenset({"search_in", "fresh", "sort", "meeting_scope"}),
}

SOURCE_PREFIXES: dict[str, tuple[str, ...]] = {
    "company-wiki": ("wiki",),
    "sales-notes": ("mail", "digest"),
    "tmtb": ("tmtb",),
    "funda-ai": ("funda",),
}

DEFAULT_FANOUT_GRACE_S: float = 20.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_sources(results: list[dict[str, Any]], extra_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for blk in extra_blocks:
        for src in blk.get("_sources") or []:
            if isinstance(src, dict):
                ident = str(src.get("identifier") or src.get("document_id") or "")
                if ident and ident not in seen:
                    seen.add(ident)
                    sources.append(src)
                    if len(sources) >= 50:
                        return sources
    for item in results:
        doc_id = str(item.get("document_id") or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        src_name = str(item.get("source") or "research")
        sources.append(
            {
                "document_id": doc_id[:200],
                "identifier": doc_id[:200],
                "route": src_name,
                "source_type": "research_doc",
                "identity": "internal_note" if src_name != "guidepoint" else "expert",
                "detail": "internal_note" if src_name != "guidepoint" else "expert",
                "publisher": str(item.get("original_source") or src_name)[:120],
                "provider": f"dalton_research_all:{src_name}",
                "title": str(item.get("title") or doc_id)[:300],
                "as_of": str(item.get("date") or "")[:10],
                "url": item.get("url") if isinstance(item.get("url"), str) else None,
                "private": True,
            }
        )
        if len(sources) >= 50:
            break
    return sources


class LegacyBridgeAlphaEngineProxy:
    """Forwards Batch 3 `all.search` AlphaEngine leg to the legacy bridge (`:8960/mcp`).

    Keeps the legacy bridge as the single authoritative handler for AlphaEngine
    (SQLite v4 cache, usage_file, circuit breaker) throughout Batch 3 (§0.2 #6, §6.4 #3).
    """

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


class FederatedResearchConnector:
    """MCP connector mounted at ``/mcp/federated`` (`dalton_research_all`)."""

    route_id = "federated"
    server_name = "dalton_research_all"
    required_capabilities = ALL_INTERNAL_RESEARCH_CAPABILITIES
    require_all_capabilities = False  # Any single enabled internal source mounts the endpoint

    def __init__(
        self,
        registry: ConnectorRegistry,
        wiki_store: OpenWikiStore,
        *,
        fanout_grace_s: float = DEFAULT_FANOUT_GRACE_S,
        alphaengine_proxy: LegacyBridgeAlphaEngineProxy | None = None,
    ) -> None:
        self.registry = registry
        self.wiki_store = wiki_store
        self.fanout_grace_s = float(fanout_grace_s)
        self.alphaengine_proxy = alphaengine_proxy

    def _enabled_prefixes_desc(
        self,
        active_caps: frozenset[CapabilityId] | None = None,
    ) -> str:
        if active_caps is None:
            enabled_sources = list(self.registry.federated_search_legs.keys())
            if self.alphaengine_proxy is not None and "alphaengine" not in enabled_sources:
                enabled_sources.append("alphaengine")
        else:
            enabled_sources = self._enabled_sources(active_caps)
        enabled_prefixes: list[str] = []
        for src in enabled_sources:
            for pref in SOURCE_PREFIXES.get(src, ()):
                enabled_prefixes.append(f"'{pref}:'")
        if any(src in SOURCE_PREFIXES for src in enabled_sources):
            enabled_prefixes.append("'image:'")
        return ", ".join(enabled_prefixes) if enabled_prefixes else "'wiki:', 'image:'"

    def list_tools(
        self,
        *,
        active_caps: frozenset[CapabilityId] | None = None,
    ) -> list[dict[str, Any]]:
        if active_caps is None:
            enabled_sources = list(self.registry.federated_search_legs.keys())
            if self.alphaengine_proxy is not None and "alphaengine" not in enabled_sources:
                enabled_sources.append("alphaengine")
        else:
            enabled_sources = self._enabled_sources(active_caps)

        prefix_desc = self._enabled_prefixes_desc(active_caps)
        sources_desc = ", ".join(enabled_sources) if enabled_sources else "enabled research sources"

        param_hints = ["sources", "offset", "max_chars"]
        seen_hints = set(param_hints)
        for src in enabled_sources:
            for key in sorted(SOURCE_ALLOWED_PARAMS.get(src, ())):
                if key not in seen_hints:
                    seen_hints.add(key)
                    param_hints.append(key)

        shared_props = {
            "query": {"type": "string", "description": "Search keywords"},
            "ticker": {"type": "string", "description": "Optional ticker merged into query"},
            "document": {
                "type": "string",
                "description": f"Prefixed document ID ({prefix_desc})",
            },
            "start": {"type": "string", "description": "Start date YYYY-MM-DD"},
            "end": {"type": "string", "description": "End date YYYY-MM-DD"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            "params": {
                "type": "object",
                "description": f"Optional route filters ({', '.join(param_hints)})",
            },
        }
        tools: list[dict[str, Any]] = [
            {
                "name": "search",
                "description": f"Federated search across enabled internal research sources ({sources_desc}).",
                "inputSchema": {"type": "object", "properties": shared_props},
            },
            {
                "name": "read",
                "description": f"Read a document by prefix ({prefix_desc}) with per-source capability check.",
                "inputSchema": {"type": "object", "properties": shared_props, "required": ["document"]},
            },
            {
                "name": "get_document",
                "description": f"Alias of `read` for prefix-dispatched document retrieval ({prefix_desc}).",
                "inputSchema": {"type": "object", "properties": shared_props, "required": ["document"]},
            },
            {
                "name": "list",
                "description": f"List recent documents across enabled local research libraries ({sources_desc}).",
                "inputSchema": {"type": "object", "properties": shared_props},
            },
        ]
        # Advertise `sales_mail` ONLY when at least one of its backing capabilities is enabled
        sales_mail_sources = [s for s in ("sales-notes", "tmtb") if s in enabled_sources]
        if sales_mail_sources:
            tools.append(
                {
                    "name": "sales_mail",
                    "description": f"Compatibility alias querying enabled sources among {', '.join(sales_mail_sources)}.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["search", "read", "list"], "default": "search"},
                            **shared_props,
                        },
                    },
                }
            )
        return tools

    def _enabled_sources(self, active_caps: frozenset[CapabilityId]) -> list[str]:
        enabled: list[str] = []
        for src_id, cap in SOURCE_TO_CAPABILITY.items():
            if cap not in active_caps:
                continue
            if src_id in self.registry.federated_search_legs:
                enabled.append(src_id)
            elif src_id == "alphaengine" and self.alphaengine_proxy is not None:
                enabled.append(src_id)
        return enabled

    async def _handle_read(
        self,
        document: str,
        params: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        allowed_sources: set[str] | None = None,
    ) -> dict[str, Any]:
        doc_str = str(document or "").strip()
        if not doc_str or ":" not in doc_str:
            allowed_prefixes = self._enabled_prefixes_desc(active_caps)
            return {
                "ok": False,
                "status": "bad_request",
                "error": f"read requires prefixed document ID ({allowed_prefixes})",
            }
        prefix = doc_str.split(":", 1)[0]
        if prefix == "image":
            _status_code, res = self.wiki_store.read_image(
                doc_str,
                active_caps=active_caps,
                allowed_sources=allowed_sources,
            )
            return res

        target_source = PREFIX_TO_SOURCE.get(prefix)
        if target_source is None:
            return {
                "ok": False,
                "status": "bad_request",
                "error": f"Unsupported document prefix '{prefix}:'",
            }
        if allowed_sources is not None and target_source not in allowed_sources:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "error": f"Document '{doc_str}' belongs to '{target_source}', which is not permitted on this route.",
            }
        req_cap = SOURCE_TO_CAPABILITY[target_source]
        if req_cap not in active_caps:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "http_status": 403,
                "error": f"Capability '{req_cap.value}' is disabled.",
            }
        registered = self.registry.prefix_read_handlers.get(prefix)
        if registered is None:
            return {
                "ok": False,
                "status": "error",
                "error": f"No reader registered for prefix '{prefix}:'",
            }
        cap_guard, reader_fn = registered
        if cap_guard is not None and cap_guard not in active_caps:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "http_status": 403,
                "error": f"Capability '{cap_guard.value}' is disabled.",
            }
        # Strip federated-only `sources` before passing read params to target reader
        read_params = {k: v for k, v in params.items() if k != "sources"}
        return await reader_fn(doc_str, read_params)

    async def _run_fanout_search(
        self,
        *,
        query: str,
        start: str | None,
        end: str | None,
        limit: int,
        params: dict[str, Any],
        target_sources: list[str],
        active_caps: frozenset[CapabilityId],
        caller_key: str,
        operation: str = "search",
    ) -> dict[str, Any]:
        # Strip federated-only parameters and keep only each source's supported filter keys
        base_leg_params = {k: v for k, v in params.items() if k != "sources"}
        tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        for src in target_sources:
            allowed_keys = SOURCE_ALLOWED_PARAMS.get(src)
            src_params = (
                {k: v for k, v in base_leg_params.items() if k in allowed_keys}
                if allowed_keys is not None
                else dict(base_leg_params)
            )
            if src == "alphaengine":
                if self.alphaengine_proxy is not None and operation == "search":
                    tasks[src] = asyncio.create_task(
                        self.alphaengine_proxy.search(query, start, end, limit, src_params, caller_key)
                    )
                continue
            legs = self.registry.federated_list_legs if operation == "list" else self.registry.federated_search_legs
            entry = legs.get(src)
            if entry is None:
                continue
            cap, fn = entry
            if cap not in active_caps:
                continue
            tasks[src] = asyncio.create_task(fn(query, start, end, limit, src_params, caller_key))

        if not tasks:
            return {
                "ok": True,
                "status": "empty",
                "results": [],
                "result_count": 0,
                "source_status": {},
                "retrieved_at": _now_iso(),
            }

        pending: set[asyncio.Task[Any]] = set(tasks.values())
        grace_deadline: float | None = None
        overall_deadline: float = time.monotonic() + max(self.fanout_grace_s * 2.0, self.fanout_grace_s)
        timed_out: set[asyncio.Task[Any]] = set()

        while pending:
            any_succeeded = any(
                t.done() and not t.cancelled() and t.exception() is None
                for t in tasks.values()
            )
            if grace_deadline is None and any_succeeded and len(pending) > 0:
                grace_deadline = time.monotonic() + self.fanout_grace_s
            active_deadline = (
                min(grace_deadline, overall_deadline)
                if grace_deadline is not None
                else overall_deadline
            )
            wait_s = max(0.0, active_deadline - time.monotonic())
            done_now, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=wait_s,
            )
            if not done_now and pending:
                timed_out = set(pending)
                for t in timed_out:
                    t.cancel()
                await asyncio.gather(*timed_out, return_exceptions=True)
                break

        outcomes: dict[str, dict[str, Any]] = {}
        for src, task in tasks.items():
            if task in timed_out:
                outcomes[src] = {
                    "ok": False,
                    "status": "timeout",
                    "reason": f"{src} timed out after fanout grace {self.fanout_grace_s:.1f}s",
                    "count": 0,
                    "results": [],
                }
            else:
                try:
                    res = task.result()
                    if not isinstance(res, dict):
                        res = {"ok": False, "status": "error", "reason": "invalid_payload", "results": [], "count": 0}
                    outcomes[src] = res
                except Exception as exc:
                    outcomes[src] = {
                        "ok": False,
                        "status": "error",
                        "reason": str(exc) or type(exc).__name__,
                        "count": 0,
                        "results": [],
                    }

        # Fuse local four-library results via RRF; keep alphaengine and guidepoint top-level blocks
        local_sources = ("company-wiki", "sales-notes", "tmtb", "funda-ai")
        fused: dict[str, dict[str, Any]] = {}
        for src in local_sources:
            batch = outcomes.get(src)
            if not batch or not batch.get("ok"):
                continue
            for rank, item in enumerate(batch.get("results") or [], 1):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("canonical_id") or item.get("document_id") or f"{src}:{rank}")
                entry = fused.setdefault(
                    key,
                    {**item, "rrf_score": 0.0, "matched_sources": [], "alternate_document_ids": []},
                )
                entry["rrf_score"] += 1.0 / (60 + rank)
                if src not in entry["matched_sources"]:
                    entry["matched_sources"].append(src)
                did = item.get("document_id")
                if did and did != entry.get("document_id") and did not in entry["alternate_document_ids"]:
                    entry["alternate_document_ids"].append(did)

        hits = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)[:limit]
        source_status_map: dict[str, dict[str, Any]] = {}
        for src, batch in outcomes.items():
            rows = batch.get("results") if isinstance(batch.get("results"), list) else []
            st_entry: dict[str, Any] = {
                "ok": bool(batch.get("ok")),
                "status": str(batch.get("status") or ("ok" if batch.get("ok") else "error")),
                "count": int(batch.get("count") if batch.get("count") is not None else len(rows)),
            }
            if batch.get("reason") or batch.get("error"):
                st_entry["reason"] = str(batch.get("reason") or batch.get("error"))
            if batch.get("warnings"):
                st_entry["warnings"] = list(batch["warnings"])
            source_status_map[src] = st_entry

        any_ok = any(bool(b.get("ok")) for b in outcomes.values())
        any_partial_or_fail = any(
            (not bool(b.get("ok"))) or str(b.get("status") or "") == "partial"
            for b in outcomes.values()
        )
        has_any_hits = bool(hits) or any(
            bool(outcomes.get(s, {}).get("results")) for s in ("alphaengine", "guidepoint")
        )
        if not any_ok:
            top_status = "error"
        elif any_partial_or_fail:
            top_status = "partial"
        elif has_any_hits:
            top_status = "ok"
        else:
            top_status = "empty"

        out: dict[str, Any] = {
            "ok": any_ok,
            "status": top_status,
            "results": hits,
            "result_count": len(hits),
            "cursor": None,
            "has_more": False,
            "total_upstream": len(hits),
            "returned": len(hits),
            "source_status": source_status_map,
            "ranking": "RRF of per-source document ranks; raw scores are not mixed",
            "retrieved_at": _now_iso(),
        }
        extra_blocks: list[dict[str, Any]] = []
        if "alphaengine" in outcomes:
            ae = outcomes["alphaengine"]
            out["alphaengine"] = {
                "ok": bool(ae.get("ok")),
                "status": str(ae.get("status") or ("ok" if ae.get("ok") else "error")),
                "count": int(ae.get("count") if ae.get("count") is not None else len(ae.get("results") or [])),
                "results": list(ae.get("results") or []),
                **({"reason": str(ae.get("reason"))} if ae.get("reason") else {}),
            }
            extra_blocks.append(ae)
        if "guidepoint" in outcomes:
            gp = outcomes["guidepoint"]
            out["guidepoint"] = {
                "ok": bool(gp.get("ok")),
                "status": str(gp.get("status") or ("ok" if gp.get("ok") else "error")),
                "count": int(gp.get("count") if gp.get("count") is not None else len(gp.get("results") or [])),
                "results": list(gp.get("results") or []),
                **({"reason": str(gp.get("reason"))} if gp.get("reason") else {}),
            }
            extra_blocks.append(gp)

        sources_list = _extract_sources(hits, extra_blocks)
        if sources_list:
            out["_sources"] = sources_list
        return out

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        if not any(cap in active_caps for cap in ALL_INTERNAL_RESEARCH_CAPABILITIES):
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "http_status": 403,
                "error": "No internal research source capability is enabled.",
            }

        params = dict(arguments.get("params") or {})
        for k in ("sources", "media", "offset", "max_chars", "layer", "sender", "company", "sector", "tag", "indexed_since", "search_mode", "no_decay"):
            if k in arguments and arguments[k] is not None and k not in params:
                params[k] = arguments[k]

        limit = max(1, min(100, int(arguments.get("limit") or 10)))
        start = str(arguments["start"]) if arguments.get("start") is not None else None
        end = str(arguments["end"]) if arguments.get("end") is not None else None

        # Compatibility route: `sales_mail` strictly scopes to enabled among (sales-notes, tmtb)
        if tool_name == "sales_mail":
            op = str(arguments.get("operation") or "search").strip().lower()
            allowed_sm = [
                src
                for src in ("sales-notes", "tmtb")
                if SOURCE_TO_CAPABILITY[src] in active_caps and src in self.registry.federated_search_legs
            ]
            if not allowed_sm:
                return {
                    "ok": False,
                    "status": "capability_disabled",
                    "error_code": "capability_disabled",
                    "http_status": 403,
                    "error": "Neither sales_notes nor tmtb capability is enabled.",
                }
            if op in ("read", "get_document"):
                return await self._handle_read(
                    str(arguments.get("document") or ""),
                    params,
                    active_caps=active_caps,
                    allowed_sources=set(allowed_sm),
                )
            query = str(arguments.get("query") or arguments.get("ticker") or "").strip()
            if op == "search" and not query:
                return {"ok": False, "status": "bad_request", "error": "sales_mail search requires query"}
            return await self._run_fanout_search(
                query=query,
                start=start,
                end=end,
                limit=limit,
                params={"layer": "all", "media": "text", **params},
                target_sources=allowed_sm,
                active_caps=active_caps,
                caller_key=caller_key,
                operation=op if op in ("search", "list") else "search",
            )

        if tool_name in ("read", "get_document", "read_document"):
            req_sources_raw = params.get("sources")
            allowed_override: set[str] | None = None
            if isinstance(req_sources_raw, list) and req_sources_raw:
                allowed_override = {str(s) for s in req_sources_raw}
                for s in allowed_override:
                    cap = SOURCE_TO_CAPABILITY.get(s)
                    if cap is not None and cap not in active_caps:
                        return {
                            "ok": False,
                            "status": "capability_disabled",
                            "error_code": "capability_disabled",
                            "http_status": 403,
                            "error": f"Capability '{cap.value}' for source '{s}' is disabled.",
                        }
            return await self._handle_read(
                str(arguments.get("document") or ""),
                params,
                active_caps=active_caps,
                allowed_sources=allowed_override,
            )

        if tool_name in ("search", "list"):
            query_parts = [
                str(arguments.get("query") or "").strip(),
                str(arguments.get("ticker") or "").strip(),
            ]
            query = " ".join(p for p in dict.fromkeys(query_parts) if p)
            if tool_name == "search" and not query:
                return {"ok": False, "status": "bad_request", "error": "search requires query or ticker"}

            req_sources = params.get("sources")
            if isinstance(req_sources, list) and req_sources:
                target_sources: list[str] = []
                for s in req_sources:
                    s_clean = str(s).strip()
                    if s_clean not in SOURCE_TO_CAPABILITY:
                        return {"ok": False, "status": "bad_request", "error": f"Unknown source: {s_clean}"}
                    cap = SOURCE_TO_CAPABILITY[s_clean]
                    if cap not in active_caps:
                        return {
                            "ok": False,
                            "status": "capability_disabled",
                            "error_code": "capability_disabled",
                            "http_status": 403,
                            "error": f"Capability '{cap.value}' for requested source '{s_clean}' is disabled.",
                        }
                    target_sources.append(s_clean)
            else:
                target_sources = self._enabled_sources(active_caps)

            return await self._run_fanout_search(
                query=query,
                start=start,
                end=end,
                limit=limit,
                params=params,
                target_sources=target_sources,
                active_caps=active_caps,
                caller_key=caller_key,
                operation=tool_name,
            )

        return {"ok": False, "status": "error", "error": f"Unknown federated tool: {tool_name}"}
