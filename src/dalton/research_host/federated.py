"""Federated research connector (`dalton_research_all` at `/mcp/federated`).

Implements generic multi-source federation over `ConnectorRegistry`:
- Mounts whenever ANY internal research source capability is enabled.
- Enforces **per-source `effective_capabilities()`** checks on every `search`,
  `list`, `read` (`get_document`), and registered alias call:
  - Disabled explicit sources or prefixes immediately return `capability_disabled`.
  - Unspecified `sources` fans out strictly across currently enabled registered sources.
- Partial-failure contract: returns `"ok": true, "status": "partial"`,
  per-source `"source_status"`, and preserves top-level summary blocks for any
  source registered with `top_level_block=True` whenever any enabled leg times
  out (> `fanout_grace_s`) or fails while another enabled leg succeeds.
- Prefix `read` dispatch: dynamically resolves registered prefixes and `image:`
  (verifying the image owner's capability).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from src.dalton.capabilities import (
    BRIDGE_ROUTE_CAPABILITY_MAP,
    INTERNAL_RESEARCH_CAPABILITIES,
    CapabilityId,
)
from src.dalton.research_host.connector_registry import ConnectorRegistry, FederatedSourceSpec
from src.dalton.research_host.wiki_store import OpenWikiStore

logger = logging.getLogger("dalton-research-host.federated")

ALL_INTERNAL_RESEARCH_CAPABILITIES: tuple[CapabilityId, ...] = INTERNAL_RESEARCH_CAPABILITIES

DEFAULT_FANOUT_GRACE_S: float = 20.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_sources(
    results: list[dict[str, Any]],
    extra_blocks: list[dict[str, Any]],
    source_specs: dict[str, FederatedSourceSpec] | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    specs = source_specs or {}
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
        spec = specs.get(src_name)
        identity = spec.provenance_identity if spec is not None else "internal_note"
        sources.append(
            {
                "document_id": doc_id[:200],
                "identifier": doc_id[:200],
                "route": src_name,
                "source_type": "research_doc",
                "identity": identity,
                "detail": identity,
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
    ) -> None:
        self.registry = registry
        self.wiki_store = wiki_store
        self.fanout_grace_s = float(fanout_grace_s)

    def _source_capability(self, source_id: str) -> CapabilityId | None:
        spec = self.registry.federated_sources.get(source_id)
        if spec is not None:
            return spec.capability
        leg = self.registry.federated_search_legs.get(source_id)
        if leg is not None:
            return leg[0]
        req = BRIDGE_ROUTE_CAPABILITY_MAP.get(source_id)
        if isinstance(req, CapabilityId):
            return req
        return None

    def _source_prefixes(self, source_id: str) -> tuple[str, ...]:
        spec = self.registry.federated_sources.get(source_id)
        if spec is not None and spec.prefixes:
            return spec.prefixes
        inferred = tuple(
            pref for pref, sid in self.registry.prefix_to_source.items() if sid == source_id
        )
        if inferred:
            return inferred
        if source_id == "company-wiki":
            return ("wiki",)
        return ()

    def _source_allowed_params(self, source_id: str) -> frozenset[str] | None:
        spec = self.registry.federated_sources.get(source_id)
        if spec is not None and spec.allowed_params:
            return spec.allowed_params
        if source_id == "company-wiki":
            return frozenset(
                {"company", "sector", "tag", "search_mode", "no_decay", "media", "indexed_since"}
            )
        return None

    def _enabled_prefixes_desc(
        self,
        active_caps: frozenset[CapabilityId] | None = None,
    ) -> str:
        if active_caps is None:
            enabled_sources = list(self.registry.federated_search_legs.keys())
        else:
            enabled_sources = self._enabled_sources(active_caps)
        enabled_prefixes: list[str] = []
        seen_prefs: set[str] = set()
        for src in enabled_sources:
            for pref in self._source_prefixes(src):
                if pref not in seen_prefs:
                    seen_prefs.add(pref)
                    enabled_prefixes.append(f"'{pref}:'")
        if enabled_prefixes:
            enabled_prefixes.append("'image:'")
        return ", ".join(enabled_prefixes) if enabled_prefixes else "'wiki:', 'image:'"

    def list_tools(
        self,
        *,
        active_caps: frozenset[CapabilityId] | None = None,
    ) -> list[dict[str, Any]]:
        if active_caps is None:
            enabled_sources = list(self.registry.federated_search_legs.keys())
        else:
            enabled_sources = self._enabled_sources(active_caps)

        prefix_desc = self._enabled_prefixes_desc(active_caps)
        sources_desc = ", ".join(enabled_sources) if enabled_sources else "enabled research sources"

        param_hints = ["sources", "offset", "max_chars"]
        seen_hints = set(param_hints)
        for src in enabled_sources:
            allowed = self._source_allowed_params(src) or ()
            for key in sorted(allowed):
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
        # Advertise registered alias tools ONLY when at least one backing source is enabled
        alias_groups = dict(self.registry.federated_aliases)
        for alias_name, alias_members in alias_groups.items():
            active_members = [s for s in alias_members if s in enabled_sources]
            if active_members:
                tools.append(
                    {
                        "name": alias_name,
                        "description": f"Compatibility alias querying enabled sources among {', '.join(active_members)}.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "operation": {
                                    "type": "string",
                                    "enum": ["search", "read", "list"],
                                    "default": "search",
                                },
                                **shared_props,
                            },
                        },
                    }
                )
        return tools

    def _enabled_sources(self, active_caps: frozenset[CapabilityId]) -> list[str]:
        enabled: list[str] = []
        for src_id, (cap, _fn) in self.registry.federated_search_legs.items():
            if cap in active_caps:
                enabled.append(src_id)
        return enabled

    def _resolve_prefix_source(self, prefix: str) -> str | None:
        if prefix in self.registry.prefix_to_source:
            return self.registry.prefix_to_source[prefix]
        for src_id, spec in self.registry.federated_sources.items():
            if prefix in spec.prefixes:
                return src_id
        if prefix == "wiki":
            return "company-wiki"
        return None

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

        target_source = self._resolve_prefix_source(prefix)
        registered = self.registry.prefix_read_handlers.get(prefix)
        if registered is None and target_source is None:
            return {
                "ok": False,
                "status": "bad_request",
                "error": f"Unsupported document prefix '{prefix}:'",
            }
        if target_source is not None and allowed_sources is not None and target_source not in allowed_sources:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "error": f"Document '{doc_str}' belongs to '{target_source}', which is not permitted on this route.",
            }
        if target_source is not None:
            req_cap = self._source_capability(target_source)
            if req_cap is not None and req_cap not in active_caps:
                return {
                    "ok": False,
                    "status": "capability_disabled",
                    "error_code": "capability_disabled",
                    "http_status": 403,
                    "error": f"Capability '{req_cap.value}' is disabled.",
                }
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
        legs = self.registry.federated_list_legs if operation == "list" else self.registry.federated_search_legs
        for src in target_sources:
            entry = legs.get(src)
            if entry is None:
                continue
            cap, fn = entry
            if cap not in active_caps:
                continue
            allowed_keys = self._source_allowed_params(src)
            src_params = (
                {k: v for k, v in base_leg_params.items() if k in allowed_keys}
                if allowed_keys is not None
                else dict(base_leg_params)
            )
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

        # Fuse local library sources via RRF; emit top-level blocks for sources registered with top_level_block=True
        top_level_sources: list[str] = []
        fused: dict[str, dict[str, Any]] = {}
        for src, batch in outcomes.items():
            spec = self.registry.federated_sources.get(src)
            if spec is not None and spec.top_level_block:
                top_level_sources.append(src)
            if spec is not None and not spec.fuse_local:
                continue
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
            bool(outcomes.get(s, {}).get("results")) for s in top_level_sources
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
        for src in top_level_sources:
            blk = outcomes[src]
            src_block: dict[str, Any] = {
                "ok": bool(blk.get("ok")),
                "status": str(blk.get("status") or ("ok" if blk.get("ok") else "error")),
                "count": int(blk.get("count") if blk.get("count") is not None else len(blk.get("results") or [])),
                "results": list(blk.get("results") or []),
                **({"reason": str(blk.get("reason"))} if blk.get("reason") else {}),
            }
            for page_key in ("cursor", "has_more", "total_upstream"):
                if page_key in blk:
                    src_block[page_key] = blk[page_key]
            out[src] = src_block
            extra_blocks.append(blk)

        sources_list = _extract_sources(hits, extra_blocks, self.registry.federated_sources)
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

        alias_groups = dict(self.registry.federated_aliases)

        if tool_name in alias_groups:
            op = str(arguments.get("operation") or "search").strip().lower()
            members = alias_groups[tool_name]
            allowed_members = [
                src
                for src in members
                if (cap := self._source_capability(src)) is not None
                and cap in active_caps
                and src in self.registry.federated_search_legs
            ]
            if not allowed_members:
                return {
                    "ok": False,
                    "status": "capability_disabled",
                    "error_code": "capability_disabled",
                    "http_status": 403,
                    "error": f"No backing capability is enabled for '{tool_name}'.",
                }
            if op in ("read", "get_document"):
                return await self._handle_read(
                    str(arguments.get("document") or ""),
                    params,
                    active_caps=active_caps,
                    allowed_sources=set(allowed_members),
                )
            query = str(arguments.get("query") or arguments.get("ticker") or "").strip()
            if op == "search" and not query:
                return {"ok": False, "status": "bad_request", "error": f"{tool_name} search requires query"}
            return await self._run_fanout_search(
                query=query,
                start=start,
                end=end,
                limit=limit,
                params={"layer": "all", "media": "text", **params},
                target_sources=allowed_members,
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
                    cap = self._source_capability(s)
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
                    cap = self._source_capability(s_clean)
                    if cap is None:
                        return {"ok": False, "status": "bad_request", "error": f"Unknown source: {s_clean}"}
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
