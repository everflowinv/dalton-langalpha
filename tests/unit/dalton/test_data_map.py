"""Unit tests for the Dalton Data Map, Data Source Health Check, and prompt/tool contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

from psycopg import AsyncConnection
import pytest

from src.dalton import settings as dalton_settings
from src.dalton.data_map import builder as map_builder
from src.dalton.data_map import health as health_mod
from src.dalton.database import data_map as db_data_map
from src.dalton.tools import build_dalton_tools
from src.dalton.web_search import provider as web_provider
from src.ptc_agent.agent.prompts.loader import PromptLoader
import src.tools.market_data.implementations as market_impl

REPO_ROOT = Path(__file__).resolve().parents[3]

FIXED_HANDOFF_ROUTES: frozenset[str] = frozenset(
    {
        "web-news",
        "web-page",
        "filings",
        "agent-reach",
        "x-timeline",
        "x-search",
    }
)


def _sample_catalog_routes() -> list[dict[str, Any]]:
    return [
        {"id": "all", "category": "research", "title": "跨库初筛", "rules": ["四库+AE+GP"]},
        {"id": "alphaengine", "category": "research", "title": "卖方研报", "rules": ["search_library后get_document"]},
        {"id": "guidepoint", "category": "research", "title": "专家访谈", "rules": ["匿名引职务"]},
        {"id": "transcript", "category": "research", "title": "业绩会转写", "rules": ["all不含,须单调"]},
        {"id": "company-wiki", "category": "research", "title": "公司资料库", "rules": ["只读;params.company=TICKER"]},
        {"id": "sales-notes", "category": "research", "title": "销售邮件", "rules": ["raw独立源/digest线索"]},
        {"id": "tmtb", "category": "research", "title": "TMT总结", "rules": ["转述与原件计同源"]},
        {"id": "funda-ai", "category": "research", "title": "FUNDA AI", "rules": ["先search再read"]},
        {"id": "sec", "category": "finance", "title": "美股SEC", "rules": ["10-K/10-Q/8-K/20-F/6-K"]},
        {"id": "estimates", "category": "finance", "title": "一致预期", "rules": ["operation=consensus"]},
        {"id": "cn-hk", "category": "finance", "title": "港A股财务公告", "rules": ["operation=ask|filings"]},
        {"id": "fred", "category": "macro", "title": "FRED宏观", "rules": ["按series查"]},
        {"id": "treasury", "category": "macro", "title": "美债与发债", "rules": ["财政部官方"]},
        {"id": "xueqiu", "category": "community", "title": "雪球讨论", "kind": "skill"},
        {"id": "employee", "category": "community", "title": "员工评论", "kind": "skill"},
        {"id": "last30days", "category": "community", "title": "跨平台社区情绪", "kind": "skill"},
        # Undocumented route present in catalog but not in sources.yaml:
        {"id": "new-alt-route", "category": "research", "title": "实验研究源", "rules": ["按ticker查询"]},
        # Filtered routes that handoff_routes excludes:
        {"id": "agent-reach", "category": "community", "title": "Agent Reach", "kind": "skill"},
        {"id": "web-news", "category": "news", "title": "网页新闻", "kind": "agent_tool"},
    ]


def _sample_health_map() -> dict[str, dict[str, Any]]:
    return {
        "conversation_search": {"status": "available", "latency_ms": 12, "latest_date": "2026-10-22"},
        "web_search": {"status": "available", "latency_ms": 185, "latest_date": "2026-10-22"},
        "x_search": {"status": "available", "latency_ms": 420, "latest_date": "2026-10-22"},
        "market_quotes": {"status": "available", "latency_ms": 95, "latest_date": "2026-10-22"},
        "sec_filings": {"status": "available", "latency_ms": 140, "latest_date": "2026-10-18"},
        "all": {"status": "available", "latency_ms": 210, "latest_date": "2026-10-22"},
        "alphaengine": {"status": "available", "latency_ms": 340, "latest_date": "2026-10-22"},
        "guidepoint": {"status": "unavailable", "latency_ms": 1200, "error_type": "DesktopLoginRequired"},
        "transcript": {"status": "available", "latency_ms": 260, "latest_date": "2026-10-21"},
        "company-wiki": {"status": "available", "latency_ms": 80, "latest_date": "2026-10-20"},
        "sales-notes": {"status": "available", "latency_ms": 90, "latest_date": "2026-10-22"},
        "tmtb": {"status": "available", "latency_ms": 75, "latest_date": "2026-10-22"},
        "funda-ai": {"status": "available", "latency_ms": 88, "latest_date": "2026-10-21"},
        "sec": {"status": "available", "latency_ms": 150, "latest_date": "2026-10-18"},
        "estimates": {"status": "available", "latency_ms": 130, "latest_date": "2026-10-22"},
        "cn-hk": {"status": "available", "latency_ms": 175, "latest_date": "2026-10-22"},
        "fred": {"status": "available", "latency_ms": 60, "latest_date": "2026-10-01"},
        "treasury": {"status": "available", "latency_ms": 70, "latest_date": "2026-10-21"},
        "xueqiu": {"status": "available", "latency_ms": 310, "latest_date": "2026-10-22"},
        "employee": {"status": "available", "latency_ms": 480, "latest_date": "2026-10-20"},
        "last30days": {
            "status": "unprobed",
            "latency_ms": 0,
            "error_type": "成本高跳过探测",
            "detail": {"reason": "成本高跳过探测"},
        },
    }


def _sample_conversation_stats() -> dict[str, Any]:
    return {
        "total_cards": 1542,
        "min_date": "2026-06-01",
        "max_date": "2026-10-22",
        "by_type": {
            "fact": 412,
            "estimate": 238,
            "model_assumption": 185,
            "opinion": 364,
            "question": 129,
            "decision": 118,
            "todo": 96,
        },
        "top_subjects": [
            {"label": "NVDA", "count": 218},
            {"label": "GOOG", "count": 176},
            {"label": "META", "count": 154},
            {"label": "TSM", "count": 139},
            {"label": "PDD", "count": 112},
            {"label": "MSFT", "count": 98},
            {"label": "BABA", "count": 87},
            {"label": "0700.HK", "count": 79},
            {"label": "AI算力资本开支", "count": 68},
            {"label": "AMZN", "count": 61},
        ],
    }


def test_merge_catalog_routes_filters_handoff_and_marks_undocumented() -> None:
    """Routes in `handoff_routes` are excluded; catalog routes missing in `sources.yaml` are marked `未写说明`."""
    merged = map_builder.merge_catalog_routes(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
    )
    ids = [r["id"] for r in merged]

    assert "xueqiu" in ids
    assert "employee" in ids
    assert "last30days" in ids
    assert "agent-reach" not in ids
    assert "web-news" not in ids
    assert "cn-hk" in ids
    assert "tmtb" in ids
    assert "new-alt-route" in ids

    undoc = next(r for r in merged if r["id"] == "new-alt-route")
    assert undoc["description"] == "未写说明"
    assert undoc["title"] == "实验研究源"


def test_render_data_map_markdown_contents_date_free_title_and_search_api() -> None:
    """Rendered Data Map has date-free title, accurate cn-hk/HKEX notes, no `get_market_movers`, and respects `search_api`."""
    md = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        search_api="openclaw",
    )

    # 1. Title is date-free
    first_line = md.splitlines()[0]
    assert first_line == "## Dalton 数据地图"

    # 2. Operating principle & health statuses
    assert "下结论前先对照地图" in md
    assert "`tmtb`[research/TMT总结|可用]" in md
    assert "`guidepoint`[research/专家访谈|不可用(DesktopLoginRequired)]" in md
    assert "`xueqiu`[community/雪球讨论|可用]" in md
    assert "`employee`[community/员工评论|可用]" in md
    assert "`last30days`[community/跨平台社区情绪|未体检(成本高跳过探测)]" in md
    assert "`company_options_screener`(公司与期权选股|未体检|" in md
    assert "`new-alt-route`[research/实验研究源|未体检]:未写说明" in md
    assert "雪球(xueqiu):handoff过滤" not in md
    assert "员工评论(employee/Glassdoor/Blind):handoff过滤" not in md

    # 3. Direct tools: no get_market_movers; x_search present when search_api=="openclaw"
    assert "get_market_movers" not in md
    assert "- `x_search`" in md

    # 4. cn-hk description and HKEXnews removed from unavailable_sources
    spec = map_builder.load_sources_spec()
    unavail_ids = [item.get("id") for item in spec.get("unavailable_sources", [])]
    assert "hkexnews" not in unavail_ids
    assert (
        "`filings` 覆盖 A 股和港股的法定公告（巨潮转载），港股约有 5% 的缺口，"
        "主要是仅英文版公告和股东大会投票结果，这类需要原文时，用 `web_search` 查 `site:hkexnews.hk`"
    ) in md
    assert "港交所披露易公告" not in md
    assert "期货与大宗商品实时(非延迟)行情" in md

    # 5. Token budget < 1500 tokens
    tokens = map_builder.estimate_data_map_tokens(md)
    assert tokens < map_builder.MAX_DATA_MAP_TOKENS

    # 6. When search_api != "openclaw", x_search is excluded
    md_no_x = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        search_api="tavily",
    )
    assert "- `x_search`" not in md_no_x


def test_render_data_map_token_limit_trimming() -> None:
    """When rendered map exceeds `max_tokens`, drops Top10 subjects first, then parenthetical notes until <= `max_tokens`."""
    full_md = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
    )
    full_tokens = map_builder.estimate_data_map_tokens(full_md)
    assert "Top10：" in full_md
    assert "(search_library后get_document)" in full_md

    # Budget just below full_tokens -> drops Top10 first while keeping route parenthetical rules
    step1_budget = full_tokens - 10
    step1_md = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        max_tokens=step1_budget,
    )
    assert map_builder.estimate_data_map_tokens(step1_md) <= step1_budget
    assert "Top10：" not in step1_md
    assert "(search_library后get_document)" in step1_md

    # Tighter budget -> also drops parenthetical remarks from lines until within budget
    step2_budget = map_builder.estimate_data_map_tokens(step1_md) - 80
    step2_md = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        max_tokens=step2_budget,
    )
    assert map_builder.estimate_data_map_tokens(step2_md) <= step2_budget
    assert "Top10：" not in step2_md
    assert "(search_library后get_document)" not in step2_md

    # Even tighter budget (still exceeding after all parenthetical remarks are stripped)
    # -> truncates Section 2 tail route lines and appends overflow notice
    step3_budget = 800
    step3_md = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        max_tokens=step3_budget,
    )
    assert map_builder.estimate_data_map_tokens(step3_md) <= step3_budget
    assert '…其余路由见 `research(route="all")` / `catalog()`' in step3_md
    assert "- `all`[" in step3_md
    assert "- `new-alt-route`[" not in step3_md
    assert "### 3. 明确没有的数据（切勿以为查过）" in step3_md


@pytest.mark.asyncio
async def test_turn_cache_miss_timeout_and_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn cache read:
    1) Cache hit filters `x_search` when `search_api != 'openclaw'`;
    2) Cache miss returns static `未体检` map and does NOT write DB or query stats;
    3) Read timeout falls back to static `未体检` map;
    4) `asyncio.CancelledError` propagates upwards.
    """
    cached_text = map_builder.render_data_map_markdown(
        catalog_routes=_sample_catalog_routes(),
        handoff_routes=FIXED_HANDOFF_ROUTES,
        health_map=_sample_health_map(),
        conversation_stats=_sample_conversation_stats(),
        search_api="openclaw",
    )
    mock_get_cached = create_autospec(
        db_data_map.get_cached_data_map,
        return_value={
            "cache_date": "2026-10-22",
            "rendered_markdown": cached_text,
            "payload": {},
        },
    )
    mock_fetch_health = create_autospec(db_data_map.fetch_all_source_health)
    mock_fetch_stats = create_autospec(db_data_map.fetch_conversation_archive_stats)
    mock_upsert_cache = create_autospec(db_data_map.upsert_cached_data_map)
    monkeypatch.setattr(db_data_map, "get_cached_data_map", mock_get_cached)
    monkeypatch.setattr(db_data_map, "fetch_all_source_health", mock_fetch_health)
    monkeypatch.setattr(db_data_map, "fetch_conversation_archive_stats", mock_fetch_stats)
    monkeypatch.setattr(db_data_map, "upsert_cached_data_map", mock_upsert_cache)

    # 1) Cache hit with search_api="tavily" -> strips x_search without touching other DB functions
    hit_no_x = await map_builder.warm_turn_data_map(
        search_api="tavily",
        cache_date=date(2026, 10, 22),
    )
    assert "- `x_search`" not in hit_no_x
    assert "**1542**" in hit_no_x
    assert mock_upsert_cache.await_count == 0
    assert mock_fetch_stats.await_count == 0

    # 2) Cache miss -> returns static `未体检` map and never writes to DB or queries stats
    mock_get_cached.reset_mock()
    mock_get_cached.return_value = None
    miss_md = await map_builder.get_or_build_data_map_markdown(
        cache_date=date(2026, 10, 22),
        search_api="openclaw",
    )
    assert "未体检" in miss_md
    assert mock_fetch_health.await_count == 0
    assert mock_fetch_stats.await_count == 0
    assert mock_upsert_cache.await_count == 0

    # 3) Read timeout -> falls back to static `未体检` map
    async def _slow_get_cached(cache_date: date, *, conn: Any = None) -> dict[str, Any] | None:
        await asyncio.sleep(0.2)
        return None

    mock_get_cached.side_effect = _slow_get_cached
    timeout_md = await map_builder.get_or_build_data_map_markdown(
        cache_date=date(2026, 10, 22),
        timeout_s=0.02,
    )
    assert "未体检" in timeout_md
    assert mock_upsert_cache.await_count == 0

    # 4) CancelledError -> must propagate, never swallowed
    async def _cancelled_get_cached(cache_date: date, *, conn: Any = None) -> dict[str, Any] | None:
        raise asyncio.CancelledError()

    mock_get_cached.side_effect = _cancelled_get_cached
    with pytest.raises(asyncio.CancelledError):
        await map_builder.get_or_build_data_map_markdown(cache_date=date(2026, 10, 22))


def test_prompt_loader_injects_data_map_and_rules_into_both_roles() -> None:
    """Both `analyst` and `chief_of_staff` system prompts contain `<data_map>` and the operating principles."""
    loader = PromptLoader()
    map_builder.set_turn_data_map(None)

    analyst_prompt = loader.get_system_prompt(role="analyst")
    cos_prompt = loader.get_system_prompt(role="chief_of_staff")

    for prompt in (analyst_prompt, cos_prompt):
        assert "<data_map>" in prompt
        assert "## Dalton 数据地图" in prompt
        assert "说“证据不足/无法确定”" in prompt
        assert "conversation_search" in prompt
        assert "说明与之前看法相比有什么变化" in prompt


def test_resolve_endpoint_and_cn_hk_probe_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health check uses `get_dalton_researchdata_url` + `get_dalton_bridge_token`, and probes `cn-hk` with `300750` and `end <= today`."""
    mock_url = create_autospec(
        dalton_settings.get_dalton_researchdata_url,
        return_value="http://127.0.0.1:8960/mcp/tok-abc",
    )
    mock_tok = create_autospec(
        dalton_settings.get_dalton_bridge_token,
        return_value="tok-abc",
    )
    monkeypatch.setattr(health_mod, "get_dalton_researchdata_url", mock_url)
    monkeypatch.setattr(health_mod, "get_dalton_bridge_token", mock_tok)

    url, token, handoff = health_mod.resolve_researchdata_endpoint_and_token()
    assert url == "http://127.0.0.1:8960/mcp/tok-abc"
    assert token == "tok-abc"
    assert "agent-reach" in handoff
    assert "xueqiu" not in handoff
    assert "employee" not in handoff
    assert "last30days" not in handoff

    tool_name, args = health_mod.build_route_probe_spec("cn-hk", "finance")
    assert tool_name == "finance"
    assert args["route"] == "cn-hk"
    assert args["operation"] == "filings"
    assert args["ticker"] == "300750"
    assert args["end"] <= map_builder.today_cache_date().isoformat()

    xq_tool, xq_args = health_mod.build_route_probe_spec("xueqiu", "community")
    assert xq_tool == "community"
    assert xq_args == {"route": "xueqiu", "operation": "posts", "ticker": "00700", "pages": 1}

    emp_tool, emp_args = health_mod.build_route_probe_spec("employee", "community")
    assert emp_tool == "community"
    assert emp_args == {"route": "employee", "operation": "blind", "company": "NVIDIA", "pages": 1}


@pytest.mark.asyncio
async def test_market_quote_probe_and_latest_date_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`default_market_quote_probe` rejects `No quote data available` or missing `price`; `extract_latest_date` ignores `fetched_at`/`retrieved_at`."""
    # 1) extract_latest_date ignores fetched_at / retrieved_at / as_of and returns None when no data date exists
    assert (
        health_mod.extract_latest_date(
            {
                "fetched_at": "2026-10-22T12:00:00Z",
                "retrieved_at": "2026-10-22T12:00:00Z",
                "as_of": "2026-10-22",
            }
        )
        is None
    )
    assert health_mod.extract_latest_date({"filing_date": "2026-10-18"}) == "2026-10-18"

    # 2) "No quote data available" -> raises RuntimeError
    mock_fetch_quote = create_autospec(
        market_impl.fetch_quote,
        return_value=("No quote data available for NVDA.", {"type": "quote", "quotes": []}),
    )
    monkeypatch.setattr(market_impl, "fetch_quote", mock_fetch_quote)
    with pytest.raises(RuntimeError, match="No quote data available"):
        await health_mod.default_market_quote_probe(timeout_s=5.0)

    # 3) Non-empty quotes without price -> raises RuntimeError
    mock_fetch_quote.return_value = ("NVDA", {"type": "quote", "quotes": [{"symbol": "NVDA", "price": None}]})
    with pytest.raises(RuntimeError, match="missing price"):
        await health_mod.default_market_quote_probe(timeout_s=5.0)

    # 4) Valid quote with price but no data-intrinsic date -> succeeds with latest_date=None
    mock_fetch_quote.return_value = (
        "NVDA $140.50",
        {"type": "quote", "quotes": [{"symbol": "NVDA", "price": 140.5}], "as_of": "2026-10-22"},
    )
    probe_ok = await health_mod.default_market_quote_probe(timeout_s=5.0)
    assert probe_ok["latest_date"] is None


@pytest.mark.asyncio
async def test_bridge_inner_proxy_error_and_web_search_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1) Outer `ok=True` with inner `data.isError=True` is marked `unavailable`;
    2) `default_web_search_probe` validates via `map_broker_response()`, marks empty results
       as `unavailable` with reason `空结果`, and extracts `latest_date` from cleaned results.
    """
    # 1) Inner `isError=True` inside `{"ok": True, "data": {"isError": True, ...}}` -> unavailable
    inner_err_payload = {
        "ok": True,
        "status": "ok",
        "data": {
            "isError": True,
            "content": [{"type": "text", "text": "Guidepoint session expired"}],
        },
    }
    with pytest.raises(RuntimeError, match="Guidepoint session expired"):
        health_mod._validate_bridge_payload(inner_err_payload)

    mock_resolve_endpoint = create_autospec(
        health_mod.resolve_researchdata_endpoint_and_token,
        return_value=("http://127.0.0.1:8960/mcp", "fixed-test-token", set(FIXED_HANDOFF_ROUTES)),
    )
    monkeypatch.setattr(
        health_mod,
        "resolve_researchdata_endpoint_and_token",
        mock_resolve_endpoint,
    )

    async def _bridge_with_inner_err(
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        if tool_name == "catalog":
            return {"ok": True, "routes": [{"id": "guidepoint", "category": "research", "title": "专家访谈"}]}
        return inner_err_payload

    mock_bridge = create_autospec(health_mod.default_bridge_rpc_call, side_effect=_bridge_with_inner_err)
    mock_web = create_autospec(health_mod.default_web_search_probe, return_value={"latest_date": "2026-10-22"})
    mock_x = create_autospec(health_mod.default_x_search_probe, return_value={"latest_date": "2026-10-22"})
    mock_mkt = create_autospec(health_mod.default_market_quote_probe, return_value={"latest_date": None})
    mock_sec = create_autospec(health_mod.default_sec_filings_probe, return_value={"latest_date": "2026-10-18"})
    mock_conv = create_autospec(health_mod.default_conversation_probe, return_value={"latest_date": "2026-10-22"})

    summary = await health_mod.run_all_health_checks(
        item_timeout_s=5.0,
        bridge_caller=mock_bridge,
        web_search_prober=mock_web,
        x_search_prober=mock_x,
        market_quote_prober=mock_mkt,
        sec_filings_prober=mock_sec,
        conversation_prober=mock_conv,
        persist=False,
    )
    by_id = {r["source_id"]: r for r in summary["results"]}
    assert by_id["guidepoint"]["status"] == "unavailable"
    assert "Guidepoint session expired" in str(by_id["guidepoint"]["detail"]["reason"])

    # 2) Web search empty result via `map_broker_response` -> unavailable with reason `空结果`
    mock_wrapper_instance = create_autospec(web_provider.OpenClawSearchWrapper, instance=True)
    mock_wrapper_instance.raw_results.return_value = {"ok": True, "result": {"results": []}}
    mock_wrapper_cls = create_autospec(
        web_provider.OpenClawSearchWrapper,
        return_value=mock_wrapper_instance,
    )
    monkeypatch.setattr(web_provider, "OpenClawSearchWrapper", mock_wrapper_cls)

    with pytest.raises(web_provider.OpenClawSearchEmptyResultError, match="空结果"):
        await health_mod.default_web_search_probe(timeout_s=5.0)

    sem = asyncio.Semaphore(2)
    empty_web_row = await health_mod._run_single_probe(
        "web_search",
        "web",
        lambda: health_mod.default_web_search_probe(timeout_s=5.0),
        sem=sem,
        timeout_s=5.0,
    )
    assert empty_web_row["status"] == "unavailable"
    assert empty_web_row["error_type"] == "空结果"
    assert empty_web_row["detail"]["reason"] == "空结果"
    assert map_builder.format_health_status(empty_web_row) == "不可用(空结果)"

    # Non-empty web search result -> extracts latest_date from cleaned results (not raw envelope)
    mock_wrapper_instance.raw_results.return_value = {
        "ok": True,
        "date": "2026-10-99",
        "result": {
            "results": [
                {
                    "title": "NVDA earnings",
                    "url": "https://example.com/nvda",
                    "content": "Q3 update",
                    "published_date": "2026-10-20",
                }
            ]
        },
    }
    ok_web = await health_mod.default_web_search_probe(timeout_s=5.0)
    assert ok_web["latest_date"] == "2026-10-20"
    assert ok_web["count"] == 1


@pytest.mark.asyncio
async def test_health_check_isolation_separate_backends_and_dedicated_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_all_health_checks` uses a dedicated lock connection, isolates failures, scrubs secrets, and never copies status to unprobed tools."""
    assert health_mod.clamp_timeout_s(180.0) == 60.0

    dirty = (
        "Error calling https://api.x.ai/v1 with Authorization: Bearer xai-secretToken1234567890 "
        "and api_key=sk-proj-abcdef1234567890 on postgresql://dalton:superSecretPw@127.0.0.1:5432/db"
    )
    cleaned = health_mod.sanitize_health_text(dirty, max_len=300)
    assert "xai-secretToken" not in cleaned
    assert "sk-proj-abcdef" not in cleaned
    assert "superSecretPw" not in cleaned
    assert "[REDACTED]" in cleaned

    mock_resolve_endpoint = create_autospec(
        health_mod.resolve_researchdata_endpoint_and_token,
        return_value=("http://127.0.0.1:8960/mcp", "fixed-test-token", set(FIXED_HANDOFF_ROUTES)),
    )
    monkeypatch.setattr(
        health_mod,
        "resolve_researchdata_endpoint_and_token",
        mock_resolve_endpoint,
    )

    async def _fake_bridge_rpc(
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        if tool_name == "catalog":
            return {"ok": True, "routes": _sample_catalog_routes()}
        rid = str(arguments.get("route") or "")
        if rid == "last30days":
            raise AssertionError("last30days must not execute a real probe")
        if rid == "guidepoint":
            raise asyncio.TimeoutError("timed out")
        if rid == "alphaengine":
            raise RuntimeError("Bearer sk-secret999999999999 failed")
        if rid == "xueqiu":
            return {
                "ok": True,
                "status": "ok",
                "data": [{"id": "101", "created_at": "2026-10-22 10:00", "url": "https://xueqiu.com/1/101"}],
            }
        if rid == "employee":
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "ok": True,
                    "source": "blind",
                    "count": 1,
                    "reviews": [{"id": "r1", "created_at": "2026-10-20"}],
                },
            }
        return {"ok": True, "date": "2026-10-21", "route": rid}

    mock_bridge_caller = create_autospec(health_mod.default_bridge_rpc_call, side_effect=_fake_bridge_rpc)
    mock_web_prober = create_autospec(
        health_mod.default_web_search_probe,
        return_value={"latest_date": None},
    )
    mock_x_prober = create_autospec(
        health_mod.default_x_search_probe,
        return_value={"latest_date": "2026-10-22", "count": 2},
    )
    mock_mkt_prober = create_autospec(
        health_mod.default_market_quote_probe,
        return_value={"latest_date": None},
    )
    mock_sec_prober = create_autospec(
        health_mod.default_sec_filings_probe,
        return_value={"latest_date": "2026-10-18"},
    )
    mock_conv_prober = create_autospec(
        health_mod.default_conversation_probe,
        return_value={"latest_date": "2026-10-22", "total_cards": 1542, "min_date": "2026-06-01"},
    )

    mock_lock_conn = create_autospec(AsyncConnection, instance=True)
    mock_open_lock = create_autospec(health_mod.db_pool.open_session_connection, return_value=mock_lock_conn)
    mock_try_lock = create_autospec(db_data_map.try_acquire_health_run_lock, return_value=True)
    mock_rel_lock = create_autospec(db_data_map.release_health_run_lock, return_value=True)
    mock_get_cached = create_autospec(db_data_map.get_cached_data_map, return_value=None)
    mock_upsert_batch = create_autospec(db_data_map.upsert_source_health_batch, return_value=19)
    mock_refresh_map = create_autospec(
        health_mod.refresh_daily_data_map,
        return_value={"rendered_markdown": "## Dalton 数据地图", "token_estimate": 1150},
    )
    monkeypatch.setattr(db_data_map, "try_acquire_health_run_lock", mock_try_lock)
    monkeypatch.setattr(db_data_map, "release_health_run_lock", mock_rel_lock)
    monkeypatch.setattr(db_data_map, "get_cached_data_map", mock_get_cached)
    monkeypatch.setattr(db_data_map, "upsert_source_health_batch", mock_upsert_batch)
    monkeypatch.setattr(health_mod, "refresh_daily_data_map", mock_refresh_map)

    summary = await health_mod.run_all_health_checks(
        item_timeout_s=5.0,
        max_concurrency=3,
        bridge_caller=mock_bridge_caller,
        web_search_prober=mock_web_prober,
        x_search_prober=mock_x_prober,
        market_quote_prober=mock_mkt_prober,
        sec_filings_prober=mock_sec_prober,
        conversation_prober=mock_conv_prober,
        persist=True,
        skip_if_cached=True,
        use_advisory_lock=True,
        open_lock_connection=mock_open_lock,
    )
    by_id = {r["source_id"]: r for r in summary["results"]}

    # company_options_screener is NOT copied from market_quotes (remains unprobed)
    assert "company_options_screener" not in by_id
    assert by_id["market_quotes"]["status"] == "available"
    assert by_id["market_quotes"]["latest_date"] is None
    assert by_id["sec_filings"]["status"] == "available"
    assert by_id["sec_filings"]["latest_date"] == "2026-10-18"
    assert by_id["tmtb"]["status"] == "available"
    assert by_id["tmtb"]["latest_date"] == "2026-10-21"
    assert by_id["xueqiu"]["status"] == "available"
    assert by_id["xueqiu"]["latest_date"] == "2026-10-22"
    assert by_id["employee"]["status"] == "available"
    assert by_id["employee"]["latest_date"] == "2026-10-20"
    assert by_id["last30days"]["status"] == "unprobed"
    assert "成本高跳过探测" in str(by_id["last30days"]["detail"]["reason"])

    assert by_id["guidepoint"]["status"] == "unavailable"
    assert by_id["guidepoint"]["error_type"] == "TimeoutError"
    assert by_id["alphaengine"]["status"] == "unavailable"
    assert "sk-secret999999999999" not in str(by_id["alphaengine"]["error_type"])

    mock_resolve_endpoint.assert_called_once()
    mock_open_lock.assert_awaited_once()
    mock_try_lock.assert_awaited_once_with(conn=mock_lock_conn)
    mock_rel_lock.assert_awaited_once_with(conn=mock_lock_conn)
    mock_lock_conn.close.assert_awaited_once()

    # Startup check skips immediately when today's cache already exists
    from src.server.database import automation as auto_db

    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[]),
    )
    mock_open_lock.reset_mock()
    mock_get_cached.return_value = {"cache_date": "2026-10-22", "rendered_markdown": "## Dalton 数据地图"}
    skipped_summary = await health_mod.run_startup_health_check_once(open_lock_connection=mock_open_lock)
    assert skipped_summary is not None
    assert skipped_summary["skipped"] == "already_cached"
    assert mock_open_lock.await_count == 0


@pytest.mark.asyncio
async def test_startup_health_check_loop_cross_day_exception_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`run_startup_health_check_loop` exercises real `_is_data_health_paused_by_user` (mocking only DB queries):
    1) Skips when `dalton-data-health` row is paused / `paused_by_user=True`;
    2) Skips and logs warning when pause-state DB query fails (never treats error as unpaused);
    3) Skips when a health check record exists within the last 24 hours;
    4) Runs once when neither condition holds, without entering a periodic sleep loop.
    """
    from src.server.database import automation as auto_db

    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-admin")
    current_day = [date(2026, 10, 22)]
    mock_today = create_autospec(
        health_mod.today_cache_date,
        side_effect=lambda: current_day[0],
    )
    monkeypatch.setattr(health_mod, "today_cache_date", mock_today)

    mock_resolve_endpoint = create_autospec(
        health_mod.resolve_researchdata_endpoint_and_token,
        return_value=("http://127.0.0.1:8960/mcp", "fixed-test-token", set(FIXED_HANDOFF_ROUTES)),
    )
    monkeypatch.setattr(
        health_mod,
        "resolve_researchdata_endpoint_and_token",
        mock_resolve_endpoint,
    )

    cached_by_date: dict[date, dict[str, Any]] = {
        date(2026, 10, 22): {"cache_date": "2026-10-22", "rendered_markdown": "## Dalton 数据地图"},
    }
    automation_rows: list[dict[str, Any]] = [
        {
            "automation_id": "auto-dh-1",
            "user_id": "pm-admin",
            "name": "dalton-data-health",
            "status": "paused",
            "metadata": {"dalton_managed": True, "dalton_kind": "data_health", "paused_by_user": True},
        }
    ]
    raise_db_error = [False]

    async def _fake_list_all_automations(user_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        if raise_db_error[0]:
            raise RuntimeError("db query failure")
        return [dict(r) for r in automation_rows if r.get("user_id") == user_id]

    async def _fake_get_cached(cache_date: date, *, conn: Any = None) -> dict[str, Any] | None:
        return cached_by_date.get(cache_date)

    mock_list_all = create_autospec(auto_db.list_all_automations, side_effect=_fake_list_all_automations)
    mock_get_cached = create_autospec(db_data_map.get_cached_data_map, side_effect=_fake_get_cached)
    mock_fetch_health = create_autospec(db_data_map.fetch_all_source_health, return_value={})
    mock_lock_conn = create_autospec(AsyncConnection, instance=True)
    mock_open_lock = create_autospec(
        health_mod.db_pool.open_session_connection,
        return_value=mock_lock_conn,
    )
    mock_try_lock = create_autospec(db_data_map.try_acquire_health_run_lock, return_value=True)
    mock_rel_lock = create_autospec(db_data_map.release_health_run_lock, return_value=True)
    mock_upsert_batch = create_autospec(db_data_map.upsert_source_health_batch, return_value=5)

    refreshed_dates: list[date] = []

    async def _fake_refresh_map(
        *,
        cache_date: date | None = None,
        catalog_routes: Any = None,
        handoff_routes: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        assert cache_date is not None
        refreshed_dates.append(cache_date)
        snap = {"cache_date": cache_date.isoformat(), "rendered_markdown": "## Dalton 数据地图"}
        cached_by_date[cache_date] = snap
        return snap

    mock_refresh_map = create_autospec(health_mod.refresh_daily_data_map, side_effect=_fake_refresh_map)
    mock_execute_probes = create_autospec(
        health_mod._execute_probes,
        return_value=([{"source_id": "tmtb", "status": "available"}], [], set(FIXED_HANDOFF_ROUTES)),
    )

    monkeypatch.setattr(auto_db, "list_all_automations", mock_list_all)
    monkeypatch.setattr(db_data_map, "get_cached_data_map", mock_get_cached)
    monkeypatch.setattr(db_data_map, "fetch_all_source_health", mock_fetch_health)
    monkeypatch.setattr(db_data_map, "try_acquire_health_run_lock", mock_try_lock)
    monkeypatch.setattr(db_data_map, "release_health_run_lock", mock_rel_lock)
    monkeypatch.setattr(db_data_map, "upsert_source_health_batch", mock_upsert_batch)
    monkeypatch.setattr(health_mod, "refresh_daily_data_map", mock_refresh_map)
    monkeypatch.setattr(health_mod, "_execute_probes", mock_execute_probes)

    mock_sleep = create_autospec(asyncio.sleep)

    # 1) Skipped when dalton-data-health is paused by user (via real _is_data_health_paused_by_user)
    res_paused = await health_mod.run_startup_health_check_loop(
        open_lock_connection=mock_open_lock,
        sleep_fn=mock_sleep,
    )
    assert res_paused is not None
    assert res_paused["skipped"] == "paused_by_user"
    assert mock_open_lock.await_count == 0
    assert mock_sleep.await_count == 0

    # 2) Skipped and logged when pause-state DB query fails (never treated as unpaused)
    raise_db_error[0] = True
    with caplog.at_level("WARNING"):
        res_err = await health_mod.run_startup_health_check_loop(
            open_lock_connection=mock_open_lock,
            sleep_fn=mock_sleep,
        )
    assert res_err is not None
    assert res_err["skipped"] == "pause_query_failed"
    assert mock_open_lock.await_count == 0
    assert mock_execute_probes.await_count == 0
    assert "pause query failed" in caplog.text

    # 3) Skipped when a recent health check record exists within 24h
    raise_db_error[0] = False
    automation_rows.clear()
    res_cached = await health_mod.run_startup_health_check_loop(
        open_lock_connection=mock_open_lock,
        sleep_fn=mock_sleep,
    )
    assert res_cached is not None
    assert res_cached["skipped"] == "already_cached"
    assert mock_open_lock.await_count == 0
    assert mock_sleep.await_count == 0

    # 4) Runs once on a fresh day with no 24h record and returns immediately (no sleep loop)
    current_day[0] = date(2026, 10, 23)
    res_ran = await health_mod.run_startup_health_check_loop(
        open_lock_connection=mock_open_lock,
        sleep_fn=mock_sleep,
    )
    assert res_ran is not None
    assert res_ran.get("skipped") is None
    assert refreshed_dates == [date(2026, 10, 23)]
    mock_open_lock.assert_awaited_once()
    mock_execute_probes.assert_awaited_once()
    assert mock_sleep.await_count == 0


def test_conversation_search_docstring_contract() -> None:
    """`conversation_search` docstring states when to use it in the first sentence and has a single-line `Returns:`."""
    tools = {t.name: t for t in build_dalton_tools("analyst")}
    desc = (tools["conversation_search"].description or "").strip()
    lines = [ln.strip() for ln in desc.splitlines() if ln.strip()]

    first_sentence = lines[0]
    assert "Use when asked about any company, topic, or sector" in first_sentence
    assert "prior views" in first_sentence
    assert "model assumptions" in first_sentence
    assert "decisions" in first_sentence
    assert "wiki" not in desc.lower()

    returns_idx = lines.index("Returns:")
    returns_lines = lines[returns_idx + 1 :]
    assert len(returns_lines) == 1
    assert "openclaw://" in returns_lines[0]
