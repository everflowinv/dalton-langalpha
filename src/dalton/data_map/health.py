"""Dalton data source health check service and startup runner.

Probes each connected data source with a minimal real call:
- `researchdata` bridge catalog + every non-handoff route (`research` / `finance` / `macro`)
- `web_search` (1 minimal search)
- `x_search` (1 minimal search)
- Market data (`get_quote` snapshot for 1 stock, verifying `quotes` is non-empty and has `price`)
- SEC filings (`dalton_sec.list_filings` minimal call for 1 company)
- `conversation_search` (1 count/date-range query on `dalton_conversation_cards`)

Guarantees:
- Per-item timeout clamped to <= 60s (`MAX_ITEM_TIMEOUT_S = 60.0`)
- Bounded concurrency via `asyncio.Semaphore`
- Fault isolation: a failure on any item never affects other items
- Secret redaction: never logs, prints, or stores tokens/API keys
- Dedicated advisory lock connection (`open_session_connection`) so pool connections are never
  held during external network probes
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import json
import logging
import os
import re
import time
from typing import Any

import httpx

from src.dalton.data_map.builder import (
    load_sources_spec,
    refresh_daily_data_map,
    today_cache_date,
)
from src.dalton.database import data_map as db_data_map
from src.dalton.settings import (
    HOST_IP,
    get_dalton_bridge_token,
    get_dalton_researchdata_url,
)
from src.server.database import pool as db_pool

logger = logging.getLogger(__name__)

MAX_ITEM_TIMEOUT_S: float = 60.0
DEFAULT_ITEM_TIMEOUT_S: float = 45.0
DEFAULT_MAX_CONCURRENCY: int = 4
HEALTH_CHECK_LOOP_INTERVAL_S: float = 3600.0
HEALTH_ADVISORY_LOCK_KEY = db_data_map.HEALTH_ADVISORY_LOCK_KEY

_DATE_PREFIX_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

# Data-intrinsic date keys only (never `fetched_at`, `retrieved_at`, or envelope `as_of`).
_DATA_DATE_KEYS: tuple[str, ...] = (
    "date",
    "created_at",
    "filing_date",
    "report_date",
    "observation_date",
    "published_at",
    "publish_time",
    "max_date",
    "latest_date",
    "period_end",
    "trade_date",
    "datetime",
    "timestamp",
)

BRIDGE_COMMUNITY_ROUTES: frozenset[str] = frozenset(
    {"xueqiu", "employee", "last30days"}
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(
        r"(?i)\b(api[-_]?key|x-api-key|token|secret|password|authorization)\s*[:=]\s*['\"]?[^\s'\",;}{]{4,}"
    ),
    re.compile(
        r"\b(sk-[A-Za-z0-9_-]{10,}|xai-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{15,}|ghp_[0-9A-Za-z]{15,})\b"
    ),
    re.compile(r"(?i)(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+(@)"),
    re.compile(r"(/mcp/)[A-Za-z0-9._~-]{8,}"),
)


def clamp_timeout_s(timeout_s: float) -> float:
    """Clamp per-item probe timeout to `(0.05, MAX_ITEM_TIMEOUT_S]` (<= 60s)."""
    return min(MAX_ITEM_TIMEOUT_S, max(0.05, float(timeout_s)))


def sanitize_health_text(value: Any, *, max_len: int = 120) -> str:
    """Redact credentials/tokens from error messages or metadata before logging or persistence."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text[:max_len]


def extract_latest_date(payload: Any) -> str | None:
    """Extract the most recent data-intrinsic `YYYY-MM-DD` date from a response payload, or None."""
    candidates: list[str] = []

    def _visit(obj: Any, depth: int = 0) -> None:
        if depth > 6 or obj is None:
            return
        if isinstance(obj, Mapping):
            for key in _DATA_DATE_KEYS:
                val = obj.get(key)
                if isinstance(val, str):
                    m = _DATE_PREFIX_RE.search(val)
                    if m:
                        candidates.append(m.group(1))
            for k in (
                "_declared_sources",
                "_sources",
                "results",
                "reviews",
                "posts",
                "items",
                "rows",
                "data",
                "quotes",
                "transcripts",
                "structuredContent",
                "content",
            ):
                if k in obj:
                    _visit(obj[k], depth + 1)
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            for item in list(obj)[:15]:
                _visit(item, depth + 1)

    _visit(payload, 0)
    if not candidates:
        return None
    valid = [d for d in candidates if "2000-01-01" <= d <= "2099-12-31"]
    return max(valid) if valid else None


def resolve_researchdata_endpoint_and_token() -> tuple[str, str, set[str]]:
    """Resolve the researchdata MCP HTTP endpoint, auth token, and handoff routes via `src/dalton/settings.py`."""
    url = get_dalton_researchdata_url()
    if not url:
        port = os.getenv("DALTON_BRIDGE_PORT", "8960").strip() or "8960"
        url = f"http://{HOST_IP}:{port}/mcp"
    token = get_dalton_bridge_token()
    spec = load_sources_spec()
    handoff = {
        str(r).strip()
        for r in (spec.get("handoff_filtered_routes") or [])
        if str(r).strip()
    }
    return url, token, handoff


def _unwrap_mcp_proxy_payload(inner: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap AlphaEngine / Guidepoint MCP proxy response (`structuredContent` or `content[0].text`)."""
    sc = inner.get("structuredContent")
    if isinstance(sc, Mapping):
        return dict(sc)
    content = inner.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, Mapping) and isinstance(first.get("text"), str):
            try:
                parsed = json.loads(first["text"])
                if isinstance(parsed, Mapping):
                    return dict(parsed)
            except Exception:
                pass
    return dict(inner)


def _validate_bridge_payload(
    parsed_payload: Mapping[str, Any],
    *,
    result_obj: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate outer and inner proxy fields (`isError`, `ok: false`, `error`) of a bridge payload."""
    inner = (
        parsed_payload.get("data")
        if isinstance(parsed_payload.get("data"), Mapping)
        else parsed_payload
    )
    unwrapped = (
        _unwrap_mcp_proxy_payload(inner)
        if isinstance(inner, Mapping)
        else dict(parsed_payload)
    )
    is_err = (
        bool(isinstance(result_obj, Mapping) and result_obj.get("isError"))
        or parsed_payload.get("ok") is False
        or bool(parsed_payload.get("isError"))
        or bool(parsed_payload.get("error"))
        or (
            isinstance(inner, Mapping)
            and (
                bool(inner.get("isError"))
                or inner.get("ok") is False
                or bool(inner.get("error"))
            )
        )
        or (
            isinstance(unwrapped, Mapping)
            and (
                bool(unwrapped.get("isError"))
                or unwrapped.get("ok") is False
                or bool(unwrapped.get("error"))
            )
        )
    )
    if is_err:
        err_text = str(
            parsed_payload.get("error")
            or parsed_payload.get("reason")
            or (inner.get("error") if isinstance(inner, Mapping) else "")
            or (inner.get("reason") if isinstance(inner, Mapping) else "")
            or (unwrapped.get("error") if isinstance(unwrapped, Mapping) else "")
            or (unwrapped.get("reason") if isinstance(unwrapped, Mapping) else "")
            or ""
        ).strip()
        if not err_text and isinstance(inner, Mapping) and isinstance(inner.get("content"), list):
            c_blocks = inner["content"]
            if c_blocks and isinstance(c_blocks[0], Mapping):
                err_text = str(c_blocks[0].get("text") or "").strip()
        err_reason = sanitize_health_text(
            err_text
            or parsed_payload.get("status")
            or (inner.get("status") if isinstance(inner, Mapping) else "")
            or "ToolError"
        )
        raise RuntimeError(err_reason or "ToolError")
    return dict(parsed_payload)


async def default_bridge_rpc_call(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    timeout_s: float = DEFAULT_ITEM_TIMEOUT_S,
) -> dict[str, Any]:
    """Call `researchdata` MCP bridge over HTTP JSON-RPC and return the parsed tool payload."""
    clamped_timeout = clamp_timeout_s(timeout_s)
    url, token, _ = resolve_researchdata_endpoint_and_token()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    rpc_body = {
        "jsonrpc": "2.0",
        "id": f"health:{tool_name}:{arguments.get('route', 'catalog')}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": dict(arguments),
        },
    }
    async with httpx.AsyncClient(timeout=clamped_timeout) as client:
        resp = await client.post(url, headers=headers, json=rpc_body)
        resp.raise_for_status()
        rpc_resp = resp.json()

    if not isinstance(rpc_resp, Mapping):
        raise RuntimeError("Invalid JSON-RPC response from researchdata bridge")
    if isinstance(rpc_resp.get("error"), Mapping):
        err_msg = sanitize_health_text(rpc_resp["error"].get("message") or "RPCError")
        raise RuntimeError(f"RPCError:{err_msg}")

    result_obj = rpc_resp.get("result")
    if not isinstance(result_obj, Mapping):
        raise RuntimeError("Missing result object in researchdata bridge response")

    blocks = result_obj.get("content")
    parsed_payload: dict[str, Any] = {}
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], Mapping):
        text = blocks[0].get("text")
        if isinstance(text, str) and text.strip():
            loaded = json.loads(text)
            if isinstance(loaded, Mapping):
                parsed_payload = dict(loaded)
    if not parsed_payload and isinstance(result_obj.get("structuredContent"), Mapping):
        parsed_payload = dict(result_obj["structuredContent"])

    return _validate_bridge_payload(parsed_payload, result_obj=result_obj)


def _probe_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def build_route_probe_spec(route_id: str, category: str) -> tuple[str, dict[str, Any]]:
    """Return `(tool_name, arguments)` for a minimal real probe of a `researchdata` route."""
    # Upstream routes reject an end date past *their* today (US); the Shanghai cache date runs a
    # day ahead every evening ET, which failed alphaengine/cn-hk probes.
    today = _probe_today()
    start_30d = (today - timedelta(days=30)).isoformat()
    end_today = today.isoformat()

    rid = route_id.strip()
    cat = (category or "research").strip()
    if rid == "all":
        return "research", {"route": "all", "operation": "search", "query": "NVDA", "limit": 1}
    if rid == "alphaengine":
        return (
            "research",
            {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "NVDA",
                "start": start_30d,
                "end": end_today,
                "limit": 1,
            },
        )
    if rid == "guidepoint":
        return (
            "research",
            {"route": "guidepoint", "operation": "search_library", "query": "NVDA", "limit": 1},
        )
    if rid == "transcript":
        return "research", {"route": "transcript", "ticker": "NVDA", "limit": 1}
    if rid == "company-wiki":
        return (
            "research",
            {
                "route": "company-wiki",
                "operation": "search",
                "query": "NVDA",
                "params": {"company": "NVDA"},
                "limit": 1,
            },
        )
    if rid in ("sales-notes", "tmtb", "funda-ai"):
        return "research", {"route": rid, "operation": "search", "query": "NVDA", "limit": 1}
    if rid == "sec":
        return "finance", {"route": "sec", "operation": "list-filings", "ticker": "NVDA", "limit": 1}
    if rid == "estimates":
        return "finance", {"route": "estimates", "operation": "consensus", "ticker": "NVDA"}
    if rid == "cn-hk":
        return (
            "finance",
            {
                "route": "cn-hk",
                "operation": "filings",
                "ticker": "300750",
                "start": start_30d,
                "end": end_today,
                "limit": 1,
            },
        )
    if rid == "fred":
        return "macro", {"route": "fred", "series": "DGS10", "start": start_30d, "end": end_today, "limit": 1}
    if rid == "treasury":
        return (
            "macro",
            {
                "route": "treasury",
                "operation": "treasury",
                "dataset": "tga",
                "start": start_30d,
                "end": end_today,
                "limit": 1,
            },
        )
    if rid == "market-regime":
        return "macro", {"route": "market-regime", "operation": "snapshot"}
    if rid == "xueqiu":
        return (
            "community",
            {"route": "xueqiu", "operation": "posts", "ticker": "00700", "pages": 1},
        )
    if rid == "employee":
        return (
            "community",
            {"route": "employee", "operation": "blind", "company": "NVIDIA", "pages": 1},
        )
    if rid == "last30days":
        return (
            "community",
            {"route": "last30days", "query": "NVDA", "days": 7, "quick": True},
        )

    tool_name = cat if cat in ("research", "finance", "macro", "news", "community") else "research"
    return tool_name, {"route": rid, "operation": "search", "query": "NVDA", "limit": 1}


async def default_web_search_probe(*, timeout_s: float = DEFAULT_ITEM_TIMEOUT_S) -> dict[str, Any]:
    """Minimal real probe for `web_search`, validating non-empty results via `map_broker_response`."""
    from src.dalton.web_search.provider import (
        OpenClawSearchEmptyResultError,
        OpenClawSearchWrapper,
        map_broker_response,
    )

    wrapper = OpenClawSearchWrapper(timeout_seconds=clamp_timeout_s(timeout_s))
    raw = await wrapper.raw_results(query="NVDA stock", max_results=1)
    try:
        cleaned, _artifact = map_broker_response(raw, query="NVDA stock", include_favicon=False)
    except OpenClawSearchEmptyResultError as exc:
        raise OpenClawSearchEmptyResultError("空结果") from exc
    latest = extract_latest_date(cleaned)
    return {"latest_date": latest, "count": len(cleaned)}


async def default_x_search_probe(*, timeout_s: float = DEFAULT_ITEM_TIMEOUT_S) -> dict[str, Any]:
    """Minimal real probe for `x_search`."""
    from src.dalton.web_search.x_search import OpenClawXSearchWrapper, map_x_search_response

    today = _probe_today()
    from_d = (today - timedelta(days=7)).isoformat()
    to_d = today.isoformat()
    wrapper = OpenClawXSearchWrapper(timeout_seconds=clamp_timeout_s(timeout_s))
    raw = await wrapper.raw_results(query="NVDA", from_date=from_d, to_date=to_d)
    cleaned, _artifact = map_x_search_response(raw, query="NVDA", include_favicon=False)
    latest = extract_latest_date(cleaned) or extract_latest_date(raw)
    return {"latest_date": latest, "count": len(cleaned)}


async def default_market_quote_probe(*, timeout_s: float = DEFAULT_ITEM_TIMEOUT_S) -> dict[str, Any]:
    """Minimal real probe for stock quotes (`get_quote`), verifying `quotes` is non-empty and has `price`."""
    from src.tools.market_data.implementations import fetch_quote

    content, artifact = await asyncio.wait_for(
        fetch_quote(["NVDA"], asset_type="stocks", config={}),
        timeout=clamp_timeout_s(timeout_s),
    )
    if isinstance(artifact, Mapping) and artifact.get("error"):
        raise RuntimeError(sanitize_health_text(artifact.get("error")))
    if isinstance(content, str):
        lowered = content.strip().lower()
        if lowered.startswith("error") or "no quote data available" in lowered:
            raise RuntimeError(sanitize_health_text(content))
    quotes = artifact.get("quotes") if isinstance(artifact, Mapping) else None
    if not isinstance(quotes, list) or not quotes:
        raise RuntimeError("No quote data available")
    has_price = any(
        isinstance(q, Mapping) and q.get("price") is not None
        for q in quotes
    )
    if not has_price:
        raise RuntimeError("Quote response missing price")
    latest = extract_latest_date(quotes)
    return {"latest_date": latest}


async def default_sec_filings_probe(*, timeout_s: float = DEFAULT_ITEM_TIMEOUT_S) -> dict[str, Any]:
    """Minimal real probe for `sec_filings` (`dalton_sec.list_filings`)."""
    from plugins.dalton_data.dalton_sec_mcp_server import list_filings

    res = await asyncio.wait_for(
        asyncio.to_thread(list_filings, "NVDA", "10-K,10-Q", 1),
        timeout=clamp_timeout_s(timeout_s),
    )
    if not isinstance(res, Mapping):
        raise RuntimeError("Invalid response from dalton_sec.list_filings")
    if res.get("error") or res.get("ok") is False:
        raise RuntimeError(sanitize_health_text(res.get("error") or res.get("message") or "SecFilingsError"))
    rows = res.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Empty filing list from dalton_sec.list_filings")
    latest = extract_latest_date(rows)
    return {"latest_date": latest}


async def default_conversation_probe(*, conn: Any = None) -> dict[str, Any]:
    """Minimal real probe for `conversation_search` (`dalton_conversation_cards` count & max_date)."""
    stats = await db_data_map.fetch_conversation_archive_stats(conn=conn)
    return {
        "latest_date": stats.get("max_date"),
        "total_cards": int(stats.get("total_cards") or 0),
        "min_date": stats.get("min_date"),
    }


async def _run_single_probe(
    source_id: str,
    category: str,
    coro_factory: Callable[[], Any],
    *,
    sem: asyncio.Semaphore,
    timeout_s: float,
) -> dict[str, Any]:
    """Run one source probe under `sem` and `timeout_s` (<= 60s), isolating any failure."""
    clamped_timeout = clamp_timeout_s(timeout_s)
    async with sem:
        t0 = time.monotonic()
        try:
            outcome = await asyncio.wait_for(coro_factory(), timeout=clamped_timeout)
            latency_ms = int((time.monotonic() - t0) * 1000)
            latest_date: str | None = None
            detail: dict[str, Any] = {}
            if isinstance(outcome, Mapping):
                latest_raw = outcome.get("latest_date")
                if latest_raw is None:
                    latest_raw = extract_latest_date(outcome)
                latest_date = str(latest_raw).strip()[:32] if latest_raw else None
                for k in ("total_cards", "min_date", "count"):
                    if k in outcome and outcome[k] is not None:
                        detail[k] = outcome[k]
            return {
                "source_id": source_id,
                "category": category,
                "status": "available",
                "latency_ms": latency_ms,
                "latest_date": latest_date,
                "error_type": None,
                "detail": detail,
            }
        except asyncio.TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "source_id": source_id,
                "category": category,
                "status": "unavailable",
                "latency_ms": latency_ms,
                "latest_date": None,
                "error_type": "TimeoutError",
                "detail": {"reason": f"timed out after {clamped_timeout:.1f}s"},
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            err_cls = type(exc).__name__
            sanitized_msg = sanitize_health_text(str(exc), max_len=80)
            if sanitized_msg == "空结果":
                err_type = "空结果"
            else:
                err_type = (
                    f"{err_cls}:{sanitized_msg}"
                    if sanitized_msg and sanitized_msg != err_cls
                    else err_cls
                )
            return {
                "source_id": source_id,
                "category": category,
                "status": "unavailable",
                "latency_ms": latency_ms,
                "latest_date": None,
                "error_type": sanitize_health_text(err_type, max_len=64),
                "detail": {"reason": sanitized_msg or err_cls},
            }


async def _execute_probes(
    *,
    clamped_timeout: float,
    clamped_concurrency: int,
    call_bridge: Callable[..., Any],
    probe_web: Callable[..., Any],
    probe_x: Callable[..., Any],
    probe_market: Callable[..., Any],
    probe_sec_filings: Callable[..., Any] | None,
    probe_conv: Callable[..., Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, set[str]]:
    sem = asyncio.Semaphore(clamped_concurrency)
    spec = load_sources_spec()
    _, _, handoff_routes = resolve_researchdata_endpoint_and_token()

    # 1. Discover routes via catalog() (with fallback to sources.yaml routes if catalog fails)
    catalog_routes: list[dict[str, Any]] | None = None
    try:
        cat_payload = await asyncio.wait_for(
            call_bridge("catalog", {}, timeout_s=min(15.0, clamped_timeout)),
            timeout=min(15.0, clamped_timeout),
        )
        raw_routes = cat_payload.get("routes") if isinstance(cat_payload, Mapping) else None
        if isinstance(raw_routes, list):
            catalog_routes = [
                dict(r)
                for r in raw_routes
                if isinstance(r, Mapping)
                and str(r.get("id") or "").strip()
                and str(r.get("id") or "").strip() not in handoff_routes
                and (
                    str(r.get("kind") or "").strip().lower() not in ("agent_tool", "skill")
                    or str(r.get("id") or "").strip() in BRIDGE_COMMUNITY_ROUTES
                )
            ]
    except Exception as exc:
        logger.warning(
            "catalog() discovery failed during health check (%s); falling back to sources.yaml routes",
            type(exc).__name__,
        )
        catalog_routes = None

    yaml_routes = (
        dict(spec.get("catalog_routes") or {})
        if isinstance(spec.get("catalog_routes"), Mapping)
        else {}
    )
    routes_to_probe: list[tuple[str, str]] = []
    if catalog_routes is not None:
        for r in catalog_routes:
            rid = str(r.get("id") or "").strip()
            cat = str(
                r.get("category")
                or (yaml_routes.get(rid) or {}).get("category")
                or "research"
            ).strip()
            if rid and rid not in handoff_routes:
                routes_to_probe.append((rid, cat))
    else:
        for rid, entry in yaml_routes.items():
            rid_s = str(rid).strip()
            if rid_s and rid_s not in handoff_routes and isinstance(entry, Mapping):
                routes_to_probe.append((rid_s, str(entry.get("category") or "research").strip()))

    # 2. Schedule all probes with bounded concurrency and per-item timeout <= 60s
    tasks: list[asyncio.Task[dict[str, Any]]] = []

    for rid, cat in routes_to_probe:
        if rid == "last30days":
            async def _unprobed_last30days(
                r_id: str = rid, r_cat: str = cat
            ) -> dict[str, Any]:
                return {
                    "source_id": r_id,
                    "category": r_cat,
                    "status": "unprobed",
                    "latency_ms": 0,
                    "latest_date": None,
                    "error_type": "成本高跳过探测",
                    "detail": {"reason": "成本高跳过探测"},
                }

            tasks.append(asyncio.create_task(_unprobed_last30days()))
            continue

        tool_name, tool_args = build_route_probe_spec(rid, cat)

        async def _probe_route(
            r_id: str = rid,
            t_name: str = tool_name,
            t_args: dict[str, Any] = tool_args,
        ) -> dict[str, Any]:
            res = await call_bridge(t_name, t_args, timeout_s=clamped_timeout)
            if isinstance(res, Mapping):
                _validate_bridge_payload(res)
                if r_id == "market-regime" and isinstance(res.get("date"), str):
                    m = _DATE_PREFIX_RE.search(res["date"])
                    if m:
                        return {"latest_date": m.group(1)}
                elif r_id == "xueqiu":
                    raw_data = res.get("data")
                    has_posts = (
                        (isinstance(raw_data, list) and len(raw_data) > 0)
                        or (
                            isinstance(raw_data, Mapping)
                            and bool(
                                raw_data.get("posts")
                                or raw_data.get("results")
                                or raw_data.get("items")
                            )
                        )
                        or bool(
                            res.get("posts")
                            or res.get("results")
                            or res.get("_sources")
                        )
                    )
                    if not has_posts:
                        raise RuntimeError("空结果")
                elif r_id == "employee":
                    raw_data = res.get("data")
                    has_reviews = (
                        (isinstance(raw_data, list) and len(raw_data) > 0)
                        or (
                            isinstance(raw_data, Mapping)
                            and bool(
                                raw_data.get("reviews")
                                or raw_data.get("results")
                                or raw_data.get("count")
                            )
                        )
                        or bool(
                            res.get("reviews")
                            or res.get("results")
                            or res.get("_sources")
                        )
                    )
                    if not has_reviews:
                        raise RuntimeError("空结果")
            return {"latest_date": extract_latest_date(res)}

        tasks.append(
            asyncio.create_task(
                _run_single_probe(rid, cat, _probe_route, sem=sem, timeout_s=clamped_timeout)
            )
        )

    tasks.append(
        asyncio.create_task(
            _run_single_probe(
                "web_search",
                "web",
                lambda: probe_web(timeout_s=clamped_timeout),
                sem=sem,
                timeout_s=clamped_timeout,
            )
        )
    )
    tasks.append(
        asyncio.create_task(
            _run_single_probe(
                "x_search",
                "social",
                lambda: probe_x(timeout_s=clamped_timeout),
                sem=sem,
                timeout_s=clamped_timeout,
            )
        )
    )
    tasks.append(
        asyncio.create_task(
            _run_single_probe(
                "market_quotes",
                "market",
                lambda: probe_market(timeout_s=clamped_timeout),
                sem=sem,
                timeout_s=clamped_timeout,
            )
        )
    )
    if probe_sec_filings is not None:
        tasks.append(
            asyncio.create_task(
                _run_single_probe(
                    "sec_filings",
                    "filing",
                    lambda: probe_sec_filings(timeout_s=clamped_timeout),
                    sem=sem,
                    timeout_s=clamped_timeout,
                )
            )
        )
    tasks.append(
        asyncio.create_task(
            _run_single_probe(
                "conversation_search",
                "memory",
                probe_conv,
                sem=sem,
                timeout_s=clamped_timeout,
            )
        )
    )

    probe_results: list[dict[str, Any]] = list(await asyncio.gather(*tasks))
    return probe_results, catalog_routes, handoff_routes


async def run_all_health_checks(
    *,
    item_timeout_s: float = DEFAULT_ITEM_TIMEOUT_S,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    bridge_caller: Callable[..., Any] | None = None,
    web_search_prober: Callable[..., Any] | None = None,
    x_search_prober: Callable[..., Any] | None = None,
    market_quote_prober: Callable[..., Any] | None = None,
    sec_filings_prober: Callable[..., Any] | None = default_sec_filings_probe,
    conversation_prober: Callable[..., Any] | None = None,
    persist: bool = True,
    skip_if_cached: bool = False,
    use_advisory_lock: bool = False,
    open_lock_connection: Callable[[], Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Probe connected Dalton data sources, persist results to Postgres, and refresh the daily data map.

    When `use_advisory_lock=True`, acquires `HEALTH_ADVISORY_LOCK_KEY` on a dedicated
    `open_session_connection()` so no pool connection is held while probing external sources.
    """
    clamped_timeout = clamp_timeout_s(item_timeout_s)
    clamped_concurrency = max(1, min(8, int(max_concurrency)))
    target_date = today_cache_date()

    if skip_if_cached:
        existing = await db_data_map.get_cached_data_map(target_date, conn=conn)
        if existing and existing.get("rendered_markdown"):
            return {
                "ok": True,
                "skipped": "already_cached",
                "checked_count": 0,
                "available_count": 0,
                "unavailable_count": 0,
                "results": [],
                "data_map": existing,
            }

    lock_conn: Any = None
    lock_acquired = False
    try:
        if use_advisory_lock:
            open_sess_fn = open_lock_connection or db_pool.open_session_connection
            lock_conn = await open_sess_fn()
            lock_acquired = await db_data_map.try_acquire_health_run_lock(conn=lock_conn)
            if not lock_acquired:
                return {
                    "ok": True,
                    "skipped": "lock_held",
                    "checked_count": 0,
                    "available_count": 0,
                    "unavailable_count": 0,
                    "results": [],
                    "data_map": None,
                }
            if skip_if_cached:
                existing = await db_data_map.get_cached_data_map(target_date, conn=conn)
                if existing and existing.get("rendered_markdown"):
                    return {
                        "ok": True,
                        "skipped": "already_cached",
                        "checked_count": 0,
                        "available_count": 0,
                        "unavailable_count": 0,
                        "results": [],
                        "data_map": existing,
                    }

        call_bridge = bridge_caller or default_bridge_rpc_call
        probe_web = web_search_prober or default_web_search_probe
        probe_x = x_search_prober or default_x_search_probe
        probe_market = market_quote_prober or default_market_quote_probe
        probe_conv = conversation_prober or (lambda: default_conversation_probe(conn=conn))

        probe_results, catalog_routes, handoff_routes = await _execute_probes(
            clamped_timeout=clamped_timeout,
            clamped_concurrency=clamped_concurrency,
            call_bridge=call_bridge,
            probe_web=probe_web,
            probe_x=probe_x,
            probe_market=probe_market,
            probe_sec_filings=sec_filings_prober,
            probe_conv=probe_conv,
        )

        refreshed_map: dict[str, Any] | None = None
        if persist:
            await db_data_map.upsert_source_health_batch(probe_results, conn=conn)
            refreshed_map = await refresh_daily_data_map(
                cache_date=target_date,
                catalog_routes=catalog_routes,
                handoff_routes=handoff_routes,
                conn=conn,
            )

        available_count = sum(1 for r in probe_results if r.get("status") == "available")
        unavailable_count = sum(1 for r in probe_results if r.get("status") == "unavailable")

        return {
            "ok": True,
            "checked_count": len(probe_results),
            "available_count": available_count,
            "unavailable_count": unavailable_count,
            "results": probe_results,
            "data_map": refreshed_map,
        }
    finally:
        if lock_conn is not None:
            try:
                if lock_acquired:
                    await db_data_map.release_health_run_lock(conn=lock_conn)
            except Exception:
                pass
            try:
                await lock_conn.close()
            except Exception:
                pass


async def run_startup_health_check_once(
    *,
    open_lock_connection: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """Run one background health check after backend startup.

    First checks if today's (`Asia/Shanghai`) snapshot already exists in `dalton_data_map_cache`;
    if not, acquires the advisory lock on a dedicated `open_session_connection()` (never holding a
    pool connection during probes) and writes the health rows + daily data map snapshot.
    """
    try:
        summary = await run_all_health_checks(
            persist=True,
            skip_if_cached=True,
            use_advisory_lock=True,
            open_lock_connection=open_lock_connection,
        )
        if summary.get("skipped"):
            logger.info(
                "[DaltonDataMap] Startup data source health check skipped (%s)",
                summary["skipped"],
            )
        else:
            logger.info(
                "[DaltonDataMap] Startup data source health check completed: %d/%d available",
                summary["available_count"],
                summary["checked_count"],
            )
        return summary
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[DaltonDataMap] Startup data source health check failed: %s",
            type(exc).__name__,
        )
        return None


async def run_startup_health_check_loop(
    *,
    interval_s: float = HEALTH_CHECK_LOOP_INTERVAL_S,
    open_lock_connection: Callable[[], Any] | None = None,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Run the health check on startup and re-check every `interval_s` (60 min) across day rollovers.

    Skips whenever today's (`Asia/Shanghai`) snapshot already exists in `dalton_data_map_cache`.
    When `Asia/Shanghai` crosses midnight, the new day has no snapshot and a fresh health check
    runs under the dedicated advisory lock. Any non-cancellation exception is logged as a warning
    so the loop never exits prematurely; `asyncio.CancelledError` propagates immediately.
    """
    while True:
        try:
            await run_startup_health_check_once(
                open_lock_connection=open_lock_connection,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[DaltonDataMap] Periodic data source health check loop error: %s",
                type(exc).__name__,
            )
        await sleep_fn(interval_s)
