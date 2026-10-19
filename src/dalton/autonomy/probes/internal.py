"""Internal library probe: checks for new AlphaEngine / GuidePoint / Transcript / Sales Notes docs (PK.16)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Optional
import urllib.request

from src.dalton.autonomy.probes import ProbeSignal

logger = logging.getLogger(__name__)

INTERNAL_BRIDGE_ROUTES: tuple[str, ...] = ("sales-notes", "alphaengine", "guidepoint")
INTERNAL_PROBE_STEP_TIMEOUT_S: float = 10.0
_LAST_UNCONFIGURED_WARN_AT: float = 0.0


def _fetch_bridge_route_sync(
    *,
    bridge_url: str,
    route: str,
    ticker: str,
    since_ts: Optional[str] = None,
    timeout_s: float = 8.0,
) -> list[dict[str, Any]]:
    """Call local researchdata bridge for recent internal research docs on ``route``."""
    payload = {
        "jsonrpc": "2.0",
        "id": f"probe-internal-{route}-{ticker}",
        "method": "tools/call",
        "params": {
            "name": "research",
            "arguments": {
                "route": route,
                "action": "search",
                "ticker": ticker,
                "query": ticker,
                "limit": 10,
            },
        },
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        bridge_url,
        data=data_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    result = raw.get("result") or {}
    content_list = result.get("content") or []
    items: list[dict[str, Any]] = []
    for block in content_list:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            parsed = json.loads(block.get("text") or "{}")
        except Exception:
            continue
        if isinstance(parsed, dict):
            candidates = (
                parsed.get("items")
                or parsed.get("results")
                or parsed.get("documents")
                or parsed.get("data")
                or []
            )
            if isinstance(candidates, list):
                for c in candidates:
                    if isinstance(c, dict):
                        pub_at = str(c.get("published_at") or c.get("date") or c.get("created_at") or "")
                        if since_ts and pub_at and pub_at <= str(since_ts):
                            continue
                        items.append(
                            {
                                "doc_id": str(c.get("doc_id") or c.get("id") or c.get("uri") or ""),
                                "source": str(c.get("source") or route),
                                "doc_type": str(c.get("doc_type") or c.get("type") or route),
                                "title": str(c.get("title") or c.get("headline") or ""),
                                "snippet": str(c.get("snippet") or c.get("summary") or c.get("abstract") or "")[:500],
                                "published_at": pub_at or None,
                            }
                        )
    return items


async def _query_bridge_internal_docs(
    *,
    ticker: str,
    workspace_id: Optional[str] = None,
    since_ts: Optional[str] = None,
    allow_network: bool = True,
) -> list[dict[str, Any]]:
    """Query internal research documents via researchdata bridge (sales-notes / alphaengine / guidepoint, A6)."""
    global _LAST_UNCONFIGURED_WARN_AT
    if not allow_network:
        return []
    sym = (ticker or "").strip().upper()
    if not sym or sym == "UNKNOWN":
        return []

    bridge_url = os.environ.get("DALTON_BRIDGE_MCP_URL") or ""
    if not bridge_url:
        token = os.environ.get("DALTON_BRIDGE_TOKEN") or ""
        port = os.environ.get("DALTON_BRIDGE_PORT") or "8960"
        if token:
            bridge_url = f"http://127.0.0.1:{port}/mcp/{token}"
        else:
            now_mono = time.monotonic()
            if _LAST_UNCONFIGURED_WARN_AT == 0.0 or (now_mono - _LAST_UNCONFIGURED_WARN_AT) >= 3600.0:
                _LAST_UNCONFIGURED_WARN_AT = now_mono
                logger.warning(
                    "[DALTON_INTERNAL_PROBE] neither DALTON_BRIDGE_MCP_URL nor DALTON_BRIDGE_TOKEN "
                    "is configured; internal probe returning empty results (ticker=%s).",
                    sym,
                )
            return []

    async def _fetch_one_route(route: str) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                _fetch_bridge_route_sync,
                bridge_url=bridge_url,
                route=route,
                ticker=sym,
                since_ts=since_ts,
            )
        except Exception as e:
            logger.warning(
                "[DALTON_INTERNAL_PROBE] bridge query failed for ticker=%s route=%s: %s",
                sym,
                route,
                e,
            )
            return []

    tasks = [asyncio.create_task(_fetch_one_route(route)) for route in INTERNAL_BRIDGE_ROUTES]
    try:
        route_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=INTERNAL_PROBE_STEP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        logger.warning(
            "[DALTON_INTERNAL_PROBE] bridge parallel query timed out after %.1fs for ticker=%s",
            INTERNAL_PROBE_STEP_TIMEOUT_S,
            sym,
        )
        return []

    docs: list[dict[str, Any]] = []
    for res in route_results:
        if isinstance(res, list):
            docs.extend(res)
    return docs


async def probe_internal(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    workspace_id: Optional[str] = None,
    docs_override: Optional[list[dict[str, Any]]] = None,
    allow_network: bool = True,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Detect new internal research documents newer than `last_internal_doc_id` / `last_internal_doc_ts`."""
    state = dict(watch_state or {})
    since_watermark = state.get("last_internal_doc_ts") or state.get("last_internal_check_at")
    docs: Optional[list[dict[str, Any]]] = docs_override
    if docs is None:
        pending = state.get("pending_internal_docs")
        if isinstance(pending, list) and pending:
            docs = pending
        elif isinstance(state.get("internal_docs_feed"), list):
            docs = state.get("internal_docs_feed")
        elif workspace_id and allow_network:
            docs = await _query_bridge_internal_docs(
                ticker=ticker,
                workspace_id=workspace_id,
                since_ts=str(since_watermark) if since_watermark else None,
                allow_network=allow_network,
            )
    state["last_internal_check_at"] = datetime.now(timezone.utc).isoformat()

    if not isinstance(docs, list) or not docs:
        return [], state

    seen_ids = set(state.get("seen_internal_doc_ids") or [])
    if state.get("last_internal_doc_id"):
        seen_ids.add(str(state["last_internal_doc_id"]))

    signals: list[ProbeSignal] = []
    newest_ts: Optional[str] = state.get("last_internal_doc_ts")
    newest_id: Optional[str] = state.get("last_internal_doc_id")

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("doc_id") or doc.get("id") or "").strip()
        if not doc_id or doc_id in seen_ids:
            continue
        source = str(doc.get("source") or "internal")
        doc_type = str(doc.get("doc_type") or doc.get("type") or source)
        title = str(doc.get("title") or doc_id)
        snippet = str(doc.get("snippet") or doc.get("summary") or doc.get("abstract") or "")[:500]
        pub_at = doc.get("published_at") or doc.get("created_at")
        pub_str = str(pub_at) if pub_at is not None else None

        signals.append(
            ProbeSignal(
                probe="internal",
                signal_key=f"doc:{source}:{doc_id}",
                priority="P1",
                summary=f"{ticker} 新增内部研究文献 [{source}]：{title}",
                payload={
                    "source": source,
                    "doc_type": doc_type,
                    "doc_id": doc_id,
                    "title": title,
                    "snippet": snippet,
                    "published_at": pub_str,
                },
            )
        )
        seen_ids.add(doc_id)
        newest_id = doc_id
        if pub_str and (newest_ts is None or pub_str > str(newest_ts)):
            newest_ts = pub_str

    state["seen_internal_doc_ids"] = sorted(seen_ids)[-200:]
    if newest_id:
        state["last_internal_doc_id"] = newest_id
    if newest_ts:
        state["last_internal_doc_ts"] = newest_ts
    if "pending_internal_docs" in state:
        state["pending_internal_docs"] = []
    return signals, state
