"""Unit tests for AlphaEngine host-side exact search cache, ledger/report, and per-request caller identity."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import stat as stat_mod
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import alphaengine_search_cache as ae_cache  # noqa: E402
import alphaengine_usage_report as usage_report  # noqa: E402
import researchdata_bridge as rb  # noqa: E402
from ptc_agent.agent.backends.sandbox import SandboxBackend  # noqa: E402
from ptc_agent.agent.tools.bash import create_execute_bash_tool  # noqa: E402
from ptc_agent.core.sandbox import PTCSandbox, mcp_client_runtime as mcr  # noqa: E402
from ptc_agent.core.sandbox.jobs import Ended, JobHandle, JobOutcome  # noqa: E402
from ptc_agent.core.sandbox.supervisor_runtime import daemon as sup_daemon  # noqa: E402
from ptc_agent.core.sandbox.supervisor_runtime import protocol as sup_proto  # noqa: E402
from src.dalton.sandbox_caller_identity import caller_env  # noqa: E402


def _make_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_inflight: int = 2,
) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "test-token")
    monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "DALTON_ALPHAENGINE_CACHE_DB",
        str(tmp_path / "alphaengine_search_cache.sqlite3"),
    )
    monkeypatch.setenv(
        "DALTON_ALPHAENGINE_USAGE_FILE",
        str(tmp_path / "alphaengine_usage.jsonl"),
    )
    monkeypatch.setenv(
        "DALTON_BRIDGE_USAGE_FILE",
        str(tmp_path / "bridge-usage.json"),
    )
    monkeypatch.setenv("DALTON_BRIDGE_MAX_INFLIGHT", str(max_inflight))
    cfg = rb.BridgeConfig()
    cfg.data_sources_json = tmp_path / "missing_data_sources.json"
    return rb.ResearchDataBridge(cfg)


def _mcp_reply(req_id: Any, payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": dict(payload),
            "isError": is_error,
        },
    }


def _ae_results_payload(
    count: int,
    *,
    has_more: bool = False,
    cursor: str | None = None,
    total_upstream: int | None = None,
    prefix: str = "9900",
) -> dict[str, Any]:
    items = [
        {
            "doc_id": f"{prefix}{i:04d}",
            "title": f"Report {i}",
            "publish_time": "2026-11-10",
            "source_name": "Broker",
        }
        for i in range(1, count + 1)
    ]
    return {
        "ok": True,
        "data": {
            "structuredContent": {
                "results": items,
                "returned": len(items),
                "has_more": has_more,
                "cursor": cursor,
                "total_upstream": total_upstream if total_upstream is not None else len(items),
                "filters": {},
            }
        },
    }


def _transcript_payload(count: int, *, complete: bool = True, pages_scanned: int = 2) -> dict[str, Any]:
    items = [
        {
            "doc_id": f"tr_{i}",
            "title": f"Earnings Call {i}",
            "publish_time": "2026-11-01",
            "kind": "company_call",
            "company_name": "Meta",
        }
        for i in range(1, count + 1)
    ]
    return {
        "ok": True,
        "status": "ok" if complete else "partial",
        "data": {
            "source": "alphaengine",
            "transcripts": items,
            "coverage": {
                "complete": complete,
                "pages_scanned": pages_scanned,
                "records_scanned": len(items),
                "warnings": [],
            },
        },
    }


# ------------------------------------------------------------------ 1. Key normalization


def test_key_normalization_case_nfkc_whitespace_order_filters_fresh_timeout() -> None:
    p1 = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "  ＮＶＤＡ\tＱ４   CapEx  ",
            "start": "2026-10-01",
            "end": "2026-11-01",
            "limit": 10,
            "timeout": 30,
            "params": {"type_ids": [2, 1, 2], "fresh": True},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p2 = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA Q4 CapEx",
            "start": "2026-10-01",
            "end": "2026-11-01",
            "limit": 10,
            "timeout": 120,
            "params": {"type_ids": [1, 2], "sort": "relevance"},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p1.cache_key == p2.cache_key
    assert p1.base_key == p2.base_key
    assert p1.fresh is True
    assert p2.fresh is False
    # `fresh` is stripped from upstream arguments
    assert "fresh" not in (p1.upstream_arguments.get("params") or {})

    # v4 lowercases query in cache_key while preserving caller case in upstream_arguments and v3 legacy key
    p_lower = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "nvda q4 capex",
            "start": "2026-10-01",
            "end": "2026-11-01",
            "limit": 10,
            "params": {"type_ids": [1, 2], "sort": "relevance"},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_lower.cache_key == p1.cache_key
    assert p_lower.legacy_cache_key != p1.legacy_cache_key
    assert p1.upstream_arguments["query"] == "NVDA Q4 CapEx"
    assert p_lower.upstream_arguments["query"] == "nvda q4 capex"

    # Word order matters -> different key
    p_reordered = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "CapEx NVDA Q4",
            "start": "2026-10-01",
            "end": "2026-11-01",
            "limit": 10,
            "params": {"type_ids": [1, 2]},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_reordered.cache_key != p1.cache_key

    # Filter params differ -> different key; v4 sorts & dedups keyword/name filter arrays too
    p_filter_diff = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA Q4 CapEx",
            "start": "2026-10-01",
            "end": "2026-11-01",
            "limit": 10,
            "params": {"type_ids": [1, 3]},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_filter_diff.cache_key != p1.cache_key

    p_order_a = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "query": "nvda",
            "params": {"company_names": ["Alpha", "Beta"]},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_order_b = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "query": "nvda",
            "params": {"company_names": ["Beta", "Alpha"]},
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_order_a.cache_key == p_order_b.cache_key
    assert p_order_a.legacy_cache_key != p_order_b.legacy_cache_key


# ------------------------------------------------------------------ 2. Cross-hit & limit slicing


@pytest.mark.asyncio
async def test_direct_and_fanout_cross_hit_and_limit_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    upstream_calls: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        if args["route"] == "all":
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        if args["route"] == "guidepoint":
            return [_mcp_reply(req["id"], {"ok": True, "data": {"structuredContent": {"data": []}}})]
        if args["route"] == "alphaengine":
            upstream_calls.append(dict(args))
            return [
                _mcp_reply(
                    req["id"],
                    _ae_results_payload(50, has_more=True, cursor="cur_p2", total_upstream=120),
                )
            ]
        raise AssertionError(f"unexpected route {args['route']}")

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 1. route='all' with limit=100 (clamped to 50) populates cache with req_limit=50
    resp_all = await bridge.handle_tools_call(
        "r-all-1",
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "NVDA Blackwell",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 100,
            },
        },
    )
    body_all = json.loads(resp_all["result"]["content"][0]["text"])
    assert body_all["alphaengine"]["ok"] is True
    assert body_all["alphaengine"]["count"] == 50
    assert body_all["alphaengine"]["_bridge_cache"]["status"] == "miss"
    assert len(upstream_calls) == 1
    assert upstream_calls[0]["limit"] == 50

    # 2. Direct route='alphaengine' limit=10 hits cache via slicing (50 -> 10)
    resp_direct = await bridge.handle_tools_call(
        "r-dir-10",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "  ＮＶＤＡ   Blackwell ",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 10,
            },
        },
    )
    body_dir = json.loads(resp_direct["result"]["content"][0]["text"])
    assert body_dir["ok"] is True
    sc = body_dir["data"]["structuredContent"]
    assert len(sc["results"]) == 10
    assert sc["returned"] == 10
    assert sc["has_more"] is True
    assert sc["cursor"] is None
    assert body_dir["_bridge_cache"]["status"] == "hit"
    assert body_dir["_bridge_cache"]["sliced_from_limit"] == 50
    assert len(body_dir["_sources"]) == 10
    assert len(upstream_calls) == 1  # no second upstream call!

    # 3. Direct write with has_more=False (12 items, limit=50) -> fanout limit=10 slices & sets has_more=True
    upstream_calls.clear()

    async def _fake_rpc_12(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        if args["route"] == "alphaengine":
            upstream_calls.append(dict(args))
            return [_mcp_reply(req["id"], _ae_results_payload(12, has_more=False))]
        if args["route"] == "all":
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        return [_mcp_reply(req["id"], {"ok": True, "data": {"structuredContent": {"data": []}}})]

    rpc_double.side_effect = _fake_rpc_12
    await bridge.handle_tools_call(
        "r-dir-12",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "TSMC CoWoS",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 50,
            },
        },
    )
    assert len(upstream_calls) == 1

    # Fan-out limit=10 hits direct 50->10 slice with has_more=True (cut 12 -> 10)
    resp_all_10 = await bridge.handle_tools_call(
        "r-all-10",
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "TSMC CoWoS",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 10,
            },
        },
    )
    body_all_10 = json.loads(resp_all_10["result"]["content"][0]["text"])
    assert body_all_10["alphaengine"]["count"] == 10
    assert body_all_10["alphaengine"]["has_more"] is True
    assert body_all_10["alphaengine"]["_bridge_cache"]["status"] == "hit"
    assert body_all_10["alphaengine"]["_bridge_cache"]["sliced_from_limit"] == 50
    assert len(upstream_calls) == 1

    # Direct limit=20 hits 12-item has_more=False -> keeps all 12 and has_more=False!
    resp_dir_20 = await bridge.handle_tools_call(
        "r-dir-20",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "TSMC CoWoS",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 20,
            },
        },
    )
    body_dir_20 = json.loads(resp_dir_20["result"]["content"][0]["text"])
    assert len(body_dir_20["data"]["structuredContent"]["results"]) == 12
    assert body_dir_20["data"]["structuredContent"]["has_more"] is False
    assert body_dir_20["_bridge_cache"]["status"] == "hit"
    assert len(upstream_calls) == 1


# ------------------------------------------------------------------ 3. TTL & Beijing midnight


def test_ttl_tiers_empty_and_beijing_midnight_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-11-15T15:59:00+00:00 -> 2026-11-15 23:59 BJT (today = 2026-11-15)
    # 2026-11-15T16:01:00+00:00 -> 2026-11-16 00:01 BJT (today = 2026-11-16)
    today_box = ["2026-11-15"]
    now_box = [1000.0]

    assert ae_cache.compute_ttl_seconds("", result_count=5, today_str="2026-11-16") == 2 * 3600
    assert ae_cache.compute_ttl_seconds("", result_count=0, today_str="2026-11-16") == 30 * 60
    assert ae_cache.compute_ttl_seconds("2026-11-16", result_count=5, today_str="2026-11-16") == 2 * 3600
    assert ae_cache.compute_ttl_seconds("2026-11-16", result_count=0, today_str="2026-11-16") == 30 * 60
    assert ae_cache.compute_ttl_seconds("2026-11-15", result_count=0, today_str="2026-11-16") == 12 * 3600
    assert ae_cache.compute_ttl_seconds("2026-11-09", result_count=1, today_str="2026-11-16") == 12 * 3600
    assert ae_cache.compute_ttl_seconds("2026-11-08", result_count=1, today_str="2026-11-16") == 7 * 86400

    # Beijing midnight flip changes 2026-11-15 from today (2h/30m) to recent-7d (12h)
    assert ae_cache.compute_ttl_seconds("2026-11-15", result_count=3, today_str="2026-11-15") == 2 * 3600
    assert ae_cache.compute_ttl_seconds("2026-11-15", result_count=3, today_str="2026-11-16") == 12 * 3600

    # Hit does NOT extend expires_at
    db_path = tmp_path / "ttl.sqlite3"
    cache = ae_cache.AlphaEngineSearchCache(
        db_path,
        today_fn=lambda: today_box[0],
        now_fn=lambda: now_box[0],
    )
    fetched_at, expires_at = cache.put_sync(
        cache_key="k1",
        base_key="b1",
        op=ae_cache.OP_SEARCH_LIBRARY,
        user_scope="team",
        req_limit=10,
        result_count=0,
        has_more=False,
        payload=_ae_results_payload(0),
        date_to="2026-11-15",
    )
    assert expires_at - fetched_at == 30 * 60

    now_box[0] = 1000.0 + 1000.0
    hit, degraded = cache.get_sync("k1", "b1", 10, op=ae_cache.OP_SEARCH_LIBRARY)
    assert not degraded and hit is not None
    assert hit.expires_at == expires_at  # hit did NOT extend expires_at!

    now_box[0] = expires_at + 1.0
    expired_hit, _ = cache.get_sync("k1", "b1", 10, op=ae_cache.OP_SEARCH_LIBRARY)
    assert expired_hit is None


# ------------------------------------------------------------------ 4. Singleflight concurrency


@pytest.mark.asyncio
async def test_singleflight_coalescing_no_deadlock_cancel_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=2)
    upstream_started = asyncio.Event()
    upstream_release = asyncio.Event()
    upstream_count = 0

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_count
        req = requests[0]
        upstream_count += 1
        upstream_started.set()
        await upstream_release.wait()
        return [_mcp_reply(req["id"], _ae_results_payload(3))]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 5 concurrent identical requests with max_inflight=2 -> 1 upstream call, no deadlock!
    req_args = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "Meta Reels",
            "limit": 3,
        },
    }
    t0 = asyncio.create_task(bridge.handle_tools_call("sf-0", req_args))
    await upstream_started.wait()
    waiters = [asyncio.create_task(bridge.handle_tools_call(f"sf-{i}", req_args)) for i in range(1, 5)]
    await asyncio.sleep(0.02)

    # Cancel one waiter: shared upstream call continues for the other 4!
    waiters[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiters[1]

    upstream_release.set()
    results = await asyncio.gather(t0, waiters[0], waiters[2], waiters[3])
    assert upstream_count == 1
    statuses = [json.loads(r["result"]["content"][0]["text"])["_bridge_cache"]["status"] for r in results]
    assert statuses.count("miss") == 1
    assert statuses.count("coalesced") == 3

    # Upstream failure is shared with all concurrent waiters and NOT written to cache
    fail_started = asyncio.Event()
    fail_release = asyncio.Event()
    fail_calls = 0

    async def _failing_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal fail_calls
        req = requests[0]
        fail_calls += 1
        fail_started.set()
        await fail_release.wait()
        return [_mcp_reply(req["id"], {"ok": False, "status": "error", "error": "upstream down"}, is_error=True)]

    rpc_double.side_effect = _failing_rpc
    fail_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"fail-{i}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": "Failing Query",
                        "limit": 3,
                    },
                },
            )
        )
        for i in range(3)
    ]
    await fail_started.wait()
    await asyncio.sleep(0.02)
    fail_release.set()
    fail_resps = await asyncio.gather(*fail_tasks)
    assert fail_calls == 1
    for r in fail_resps:
        body = json.loads(r["result"]["content"][0]["text"])
        assert body["ok"] is False

    # Because failure was not cached, a subsequent call hits upstream again
    await bridge.handle_tools_call(
        "fail-retry",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "Failing Query",
                "limit": 3,
            },
        },
    )
    assert fail_calls == 2

    # 5. Also verify 5 concurrent route='all' calls with max_inflight=2 coalesce to 1 AlphaEngine upstream call
    all_ae_calls = 0
    all_ae_started = asyncio.Event()
    all_ae_release = asyncio.Event()

    async def _all_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal all_ae_calls
        req = requests[0]
        args = req["params"]["arguments"]
        if args["route"] == "all":
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        if args["route"] == "guidepoint":
            return [_mcp_reply(req["id"], {"ok": True, "data": {"structuredContent": {"data": []}}})]
        all_ae_calls += 1
        all_ae_started.set()
        await all_ae_release.wait()
        return [_mcp_reply(req["id"], _ae_results_payload(5))]

    rpc_double.side_effect = _all_rpc
    all_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"all-sf-{i}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "all",
                        "operation": "search",
                        "query": "Concurrent Fanout Query",
                        "limit": 5,
                    },
                },
                caller_identity={"workspace": f"ws-all-sf-{i}"},
            )
        )
        for i in range(5)
    ]
    await all_ae_started.wait()
    await asyncio.sleep(0.02)
    all_ae_release.set()
    all_resps = await asyncio.gather(*all_tasks)
    assert all_ae_calls == 1
    all_statuses = [
        json.loads(r["result"]["content"][0]["text"])["alphaengine"]["_bridge_cache"]["status"]
        for r in all_resps
    ]
    assert all_statuses.count("miss") == 1
    assert all_statuses.count("coalesced") == 4

    await bridge.close()


# ------------------------------------------------------------------ 5. Fresh & cursor


@pytest.mark.asyncio
async def test_fresh_refresh_strips_param_and_cursor_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    seen_args: list[dict[str, Any]] = []
    call_idx = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal call_idx
        call_idx += 1
        req = requests[0]
        seen_args.append(dict(req["params"]["arguments"]))
        return [_mcp_reply(req["id"], _ae_results_payload(call_idx))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    # Initial miss -> 1 result
    r1 = await bridge.handle_tools_call(
        "f1",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA"}},
    )
    assert json.loads(r1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"

    # Fresh=true -> skips cache, strips `fresh`, calls upstream, overwrites cache with 2 results
    r2 = await bridge.handle_tools_call(
        "f2",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "NVDA",
                "params": {"fresh": True},
            },
        },
    )
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b2["_bridge_cache"]["status"] == "fresh"
    assert len(b2["data"]["structuredContent"]["results"]) == 2
    assert "fresh" not in seen_args[-1].get("params", {})

    # Subsequent non-fresh call gets the overwritten 2-result payload from cache
    r3 = await bridge.handle_tools_call(
        "f3",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA"}},
    )
    b3 = json.loads(r3["result"]["content"][0]["text"])
    assert b3["_bridge_cache"]["status"] == "hit"
    assert len(b3["data"]["structuredContent"]["results"]) == 2
    assert call_idx == 2

    # Cursor request always bypasses cache
    r4 = await bridge.handle_tools_call(
        "c1",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "NVDA",
                "params": {"cursor": "page_2"},
            },
        },
    )
    b4 = json.loads(r4["result"]["content"][0]["text"])
    assert b4["_bridge_cache"]["status"] == "bypass"
    assert call_idx == 3


# ------------------------------------------------------------------ 6. Personal scope


@pytest.mark.asyncio
async def test_personal_meeting_variants_always_bypass_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    upstream_calls = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_calls
        upstream_calls += 1
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(upstream_calls))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    personal_variants = [
        {"meeting_scope": "personal"},
        {"own_reservations_only": True},
        {"document_categories": ["personal_meetings"]},
        {"document_codes": ["personalMeeting"]},
        {"type_ids": ["personalMeeting"]},
    ]
    expected_calls = 0
    for params_item in personal_variants:
        args_personal = {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "1on1 notes",
                "params": dict(params_item),
            },
        }
        # Authenticated Alice x2 + Bob + Anonymous -> all bypass, zero shared/persisted cache!
        r_a1 = await bridge.handle_tools_call("p-a1", args_personal, caller_identity={"user": "alice", "workspace": "w1", "thread": "t1"})
        r_a2 = await bridge.handle_tools_call("p-a2", args_personal, caller_identity={"user": "alice", "workspace": "w1", "thread": "t1"})
        r_b1 = await bridge.handle_tools_call("p-b1", args_personal, caller_identity={"user": "bob", "workspace": "w2", "thread": "t2"})
        r_anon = await bridge.handle_tools_call("p-anon", args_personal, caller_identity={"user": "", "workspace": "", "thread": ""})
        expected_calls += 4
        for resp in (r_a1, r_a2, r_b1, r_anon):
            assert json.loads(resp["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "bypass"

    assert upstream_calls == expected_calls


# ------------------------------------------------------------------ 7. Quota fallback (stale)


@pytest.mark.asyncio
async def test_quota_error_returns_stale_within_7d_and_errors_without_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    now_box = [10_000.0]
    cache = bridge._get_alphaengine_search_cache()
    cache._now_fn = lambda: now_box[0]
    cache._today_fn = lambda: "2026-11-16"

    async def _ok_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(2))]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_ok_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)
    await bridge.handle_tools_call(
        "q1",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "TSLA"}},
    )

    # Advance past 2h TTL (within 7d retention), upstream now returns quota error
    now_box[0] = 10_000.0 + 3 * 3600

    async def _quota_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        return [_mcp_reply(req["id"], {"ok": False, "status": "error", "error": "额度已达上限，请明天再试"}, is_error=True)]

    rpc_double.side_effect = _quota_rpc
    r_stale = await bridge.handle_tools_call(
        "q2",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "TSLA"}},
    )
    b_stale = json.loads(r_stale["result"]["content"][0]["text"])
    assert b_stale["ok"] is True
    assert b_stale["stale"] is True
    assert b_stale["stale_reason"] == "quota"
    assert b_stale["_bridge_cache"]["status"] == "stale"
    assert len(b_stale["data"]["structuredContent"]["results"]) == 2

    # Advance past 7d retention -> stale expired -> returns quota error
    now_box[0] = 10_000.0 + 8 * 86400
    r_err = await bridge.handle_tools_call(
        "q3",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "TSLA"}},
    )
    b_err = json.loads(r_err["result"]["content"][0]["text"])
    assert b_err["ok"] is False
    assert b_err["error_category"] == "权限或额度"


# ------------------------------------------------------------------ 8. Nested error not cached


@pytest.mark.asyncio
async def test_nested_errors_are_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = 0

    async def _nested_err_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        req = requests[0]
        if calls == 1:
            return [
                _mcp_reply(
                    req["id"],
                    {"ok": True, "data": {"structuredContent": {"isError": True, "error": "nested upstream boom"}}},
                )
            ]
        return [_mcp_reply(req["id"], _ae_results_payload(1))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_nested_err_rpc))
    r1 = await bridge.handle_tools_call(
        "ne1",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "BABA"}},
    )
    assert json.loads(r1["result"]["content"][0]["text"])["ok"] is False

    r2 = await bridge.handle_tools_call(
        "ne2",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "BABA"}},
    )
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b2["ok"] is True
    assert b2["_bridge_cache"]["status"] == "miss"
    assert calls == 2


# ------------------------------------------------------------------ 9 & 10. Corrupt DB & restart persistence


@pytest.mark.asyncio
async def test_corrupt_db_degrades_to_passthrough_and_persists_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Part A: corrupt DB file -> logs warning, degrades to passthrough, retrieval succeeds
    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_db.write_bytes(b"not a valid sqlite file at all")
    monkeypatch.setenv("DALTON_ALPHAENGINE_CACHE_DB", str(corrupt_db))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "test-token")
    monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DALTON_ALPHAENGINE_USAGE_FILE", str(tmp_path / "usage.jsonl"))
    cfg = rb.BridgeConfig()
    cfg.data_sources_json = tmp_path / "missing.json"
    bridge_corrupt = rb.ResearchDataBridge(cfg)

    async def _ok_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(2))]

    monkeypatch.setattr(bridge_corrupt, "_run_stdio_rpc", create_autospec(bridge_corrupt._run_stdio_rpc, side_effect=_ok_rpc))
    with caplog.at_level("WARNING", logger="researchdata_bridge"):
        r_deg = await bridge_corrupt.handle_tools_call(
            "deg1",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "PDD"}},
        )
    b_deg = json.loads(r_deg["result"]["content"][0]["text"])
    assert b_deg["ok"] is True
    assert b_deg["_bridge_cache"]["status"] == "bypass"
    assert any("degraded to passthrough" in rec.message for rec in caplog.records)

    # Part B: healthy DB -> file perms 0600 -> new bridge instance hits cache!
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    bridge1 = _make_bridge(clean_dir, monkeypatch)
    calls = 0

    async def _count_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(4))]

    monkeypatch.setattr(bridge1, "_run_stdio_rpc", create_autospec(bridge1._run_stdio_rpc, side_effect=_count_rpc))
    await bridge1.handle_tools_call(
        "p1",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "JD"}},
    )
    assert calls == 1
    db_file = clean_dir / "alphaengine_search_cache.sqlite3"
    assert db_file.is_file()
    assert stat_mod.S_IMODE(db_file.stat().st_mode) == 0o600

    # New ResearchDataBridge instance reads persisted SQLite cache without calling upstream
    bridge2 = _make_bridge(clean_dir, monkeypatch)
    monkeypatch.setattr(bridge2, "_run_stdio_rpc", create_autospec(bridge2._run_stdio_rpc, side_effect=_count_rpc))
    r_hit = await bridge2.handle_tools_call(
        "p2",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "JD"}},
    )
    b_hit = json.loads(r_hit["result"]["content"][0]["text"])
    assert b_hit["_bridge_cache"]["status"] == "hit"
    assert calls == 1


# ------------------------------------------------------------------ 11 & 12. Transcript + ledger/report


@pytest.mark.asyncio
async def test_transcript_caching_and_usage_ledger_and_beijing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = 0

    async def _tr_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        req = requests[0]
        return [_mcp_reply(req["id"], _transcript_payload(3, complete=True, pages_scanned=4))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_tr_rpc))

    r1 = await bridge.handle_tools_call(
        "tr1",
        {
            "name": "research",
            "arguments": {
                "route": "transcript",
                "ticker": "META",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 3,
            },
        },
        caller_identity={"user": "u1", "workspace": "w1", "thread": "t1"},
    )
    r2 = await bridge.handle_tools_call(
        "tr2",
        {
            "name": "research",
            "arguments": {
                "route": "transcript",
                "ticker": "META",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "limit": 3,
            },
        },
        caller_identity={"user": "u2", "workspace": "w2", "thread": "t2"},
    )
    assert calls == 1
    assert json.loads(r1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"
    assert json.loads(r2["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "hit"

    entries = usage_report.load_entries([tmp_path / "alphaengine_usage.jsonl"])
    assert len(entries) == 2
    assert entries[0]["operation"] == "transcript"
    assert entries[0]["cache"] == "miss"
    assert entries[0]["upstream"] is True
    assert entries[0]["date_from"] == "2026-10-01"
    assert entries[0]["date_to"] == "2026-11-10"
    assert entries[0]["pages_scanned"] == 4
    assert entries[1]["cache"] == "hit"
    assert entries[1]["upstream"] is False

    # Beijing timezone rollover in report: 2026-11-15T17:00:00+00:00 is 2026-11-16 01:00 BJT
    summary = usage_report.summarize(
        [
            {"ts": "2026-11-15T17:00:00+00:00", "op": "search", "ok": True, "cache": "miss", "upstream": True, "user": "u1", "thread": "t1"},
            {"ts": "2026-11-15T17:05:00+00:00", "op": "search", "ok": True, "cache": "hit", "upstream": False, "user": "u2", "thread": "t2"},
            {"ts": "2026-11-15T17:10:00+00:00", "op": "search", "ok": True, "cache": "coalesced", "upstream": False, "user": "u3", "thread": "t3"},
            {"ts": "2026-11-15T17:15:00+00:00", "op": "search", "ok": True, "cache": "stale", "upstream": True, "user": "u1", "thread": "t1"},
            {"ts": "2026-11-15T17:20:00+00:00", "op": "search", "ok": True, "cache": "fresh", "upstream": True, "user": "u1", "thread": "t1"},
            {"ts": "2026-11-15T17:25:00+00:00", "op": "search", "ok": False, "cache": "miss", "upstream": True, "user": "u1", "thread": "t1"},
        ]
    )
    assert summary["total_calls"] == 6
    assert summary["upstream_calls"] == 4
    assert summary["cache_hits"] == 1
    assert summary["coalesced"] == 1
    assert summary["stale"] == 1
    assert summary["fresh"] == 1
    assert summary["failures"] == 1
    assert "2026-11-16" in summary["by_day"]
    assert "2026-11-15" not in summary["by_day"]


# ------------------------------------------------------------------ 13. Caller identity (supervisor + bash)


def test_supervisor_strips_caller_env_and_forwards_per_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. _start_supervisor strips DALTON_CALLER_* from the daemon's environment
    spawned_envs: list[dict[str, str]] = []

    orig_popen_cls = subprocess.Popen

    def _fake_popen(*args: Any, **kwargs: Any) -> Any:
        spawned_envs.append(dict(kwargs["env"]))
        proc = create_autospec(orig_popen_cls, instance=True)
        proc.pid = 12345
        return proc

    monkeypatch.setattr(mcr.subprocess, "Popen", create_autospec(orig_popen_cls, side_effect=_fake_popen))
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    sup_dir = tmp_path / mcr._LAYOUT["INTERNAL_SRC_DIR"] / mcr._SUPERVISOR_PACKAGE
    sup_dir.mkdir(parents=True)
    (sup_dir / "daemon.py").write_text("# stub\n")
    alive_checks = iter([False, True])
    monkeypatch.setattr(mcr, "_socket_alive", create_autospec(mcr._socket_alive, side_effect=lambda _p: next(alive_checks, True)))
    monkeypatch.setenv("DALTON_CALLER_USER", "leaked_user")
    monkeypatch.setenv("DALTON_CALLER_WORKSPACE", "leaked_ws")
    monkeypatch.setenv("DALTON_CALLER_THREAD", "leaked_thread")

    assert mcr._start_supervisor(str(tmp_path / "s.sock")) is True
    assert len(spawned_envs) == 1
    for k in ("DALTON_CALLER_USER", "DALTON_CALLER_WORKSPACE", "DALTON_CALLER_THREAD"):
        assert k not in spawned_envs[0]

    # 2. Same running Supervisor serves two callers -> HTTP requests carry each caller's own X-Dalton-* headers
    #    even if supervisor process env happens to have stale DALTON_CALLER_*!
    captured_http_headers: list[dict[str, str]] = []

    def _make_fake_resp(body_bytes: bytes) -> Any:
        resp_mock = create_autospec(orig_resp_cls, instance=True)
        resp_mock.status_code = 200
        resp_mock.headers = {"content-type": "application/json"}
        resp_mock.iter_bytes.side_effect = lambda: iter([body_bytes])
        resp_mock.raise_for_status.return_value = None
        return contextlib.nullcontext(resp_mock)

    def _fake_stream(
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        import json as _json

        if (json or {}).get("method") == "tools/call":
            captured_http_headers.append(dict(headers or {}))
            body = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": {"content": [{"type": "text", "text": '{"ok": true}'}], "isError": False},
            }
        else:
            body = {
                "jsonrpc": "2.0",
                "id": (json or {}).get("id", 1),
                "result": {"supportedVersions": [mcr._MODERN_VERSIONS[0]]},
            }
        return _make_fake_resp(_json.dumps(body).encode("utf-8"))

    orig_client_cls = httpx.Client
    orig_resp_cls = httpx.Response
    client_factory = create_autospec(orig_client_cls)

    def _new_client(*args: Any, **kwargs: Any) -> Any:
        c = create_autospec(orig_client_cls, instance=True)
        c.__enter__.return_value = c
        c.__exit__.return_value = False
        c.stream.side_effect = _fake_stream
        return c

    client_factory.side_effect = _new_client
    monkeypatch.setattr(
        mcr,
        "_SERVER_CONFIGS",
        {"researchdata": mcr._normalize("researchdata", {"transport": "http", "untrusted": False, "url": "http://127.0.0.1:8960/mcp"})},
    )
    monkeypatch.setattr(mcr, "_PROTO", {})
    monkeypatch.setattr(mcr.httpx, "Client", client_factory)

    # Build Supervisor backed by ClientBackend(mcr) and drive two requests
    # from different callers over AF_UNIX socketpairs (works inside Seatbelt sandbox).
    cfg_dir = tmp_path / ".agents" / "tools"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = str(cfg_dir / "mcp_client_config.json")
    Path(cfg_path).write_text(
        json.dumps({"workspace_id": "ws-shared", "servers": {"researchdata": {}}}),
        encoding="utf-8",
    )

    supervisor = sup_daemon.Supervisor(
        str(tmp_path / "unused.sock"),
        sup_daemon.ClientBackend(mcr),
        root=str(tmp_path),
        idle_window_s=1e12,
        heartbeat_s=3600.0,
        log=lambda _m: None,
    )
    try:
        # Leave stale DALTON_CALLER_* in supervisor process env to prove raw_tool_reply ignores it!
        monkeypatch.setenv("DALTON_CALLER_USER", "stale-daemon-env")
        for req_id, who in (
            (1, {"user": "alice", "workspace": "ws1", "thread": "th1"}),
            (2, {"user": "bob", "workspace": "ws2", "thread": "th2"}),
        ):
            srv_sock, cli_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            srv_sock.settimeout(5.0)
            cli_sock.settimeout(5.0)
            srv_thread = threading.Thread(
                target=supervisor._serve_conn, args=(srv_sock,), daemon=True
            )
            srv_thread.start()
            try:
                req = sup_proto.call_request(
                    req_id,
                    server="researchdata",
                    tool="research",
                    args={"route": "alphaengine"},
                    workspace_id="ws-shared",
                    config_path=cfg_path,
                    caller=who,
                )
                cli_sock.sendall(sup_proto.encode(req))
                buf = b""
                while True:
                    chunk = cli_sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b'"type": "reply"' in buf or b'"type":"reply"' in buf:
                        break
            finally:
                cli_sock.close()
                srv_thread.join(timeout=5.0)

        assert len(captured_http_headers) == 2
        assert captured_http_headers[0]["X-Dalton-User"] == "alice"
        assert captured_http_headers[0]["X-Dalton-Workspace"] == "ws1"
        assert captured_http_headers[0]["X-Dalton-Thread"] == "th1"
        assert captured_http_headers[0]["X-Dalton-Caller-Scope"] == "per-request"
        assert captured_http_headers[1]["X-Dalton-User"] == "bob"
        assert captured_http_headers[1]["X-Dalton-Workspace"] == "ws2"
        assert captured_http_headers[1]["X-Dalton-Thread"] == "th2"
        assert captured_http_headers[1]["X-Dalton-Caller-Scope"] == "per-request"
    finally:
        supervisor.close()


@pytest.mark.asyncio
async def test_bash_tool_binds_caller_identity(tmp_path: Path) -> None:
    captured_caller_env: dict[str, str] = {}

    async def _fake_start_bash(
        command: str, *, working_dir: str | None = None, wait_s: float = 0.0, context: Any = None
    ) -> JobOutcome:
        captured_caller_env.update(caller_env())
        return Ended(JobHandle("cmd-1", 0), 0, "ok", [])

    backend = create_autospec(SandboxBackend, instance=True)
    backend.provider = "daytona"
    backend.channel = "default"
    backend.working_dir = str(tmp_path)
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.call_filer = None
    backend.sandbox = sandbox
    backend.config_folder.return_value = None
    backend.settled_livefs.return_value = None
    backend.start_bash_job.side_effect = _fake_start_bash

    tool = create_execute_bash_tool(
        backend,
        thread_id="thread-fallback",
        hand_off=None,
    )
    await tool.ainvoke(
        {"command": "echo hi"},
        config={"configurable": {"user_id": "u-bash", "workspace_id": "ws-bash", "thread_id": "th-bash"}},
    )
    assert captured_caller_env == {
        "DALTON_CALLER_USER": "u-bash",
        "DALTON_CALLER_WORKSPACE": "ws-bash",
        "DALTON_CALLER_THREAD": "th-bash",
    }


# ---------------------------------------------------------------------------
# Round 2 regression tests (1–12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_1_singleflight_keeps_entry_until_put_done_and_uses_put_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    orig_put = cache.put
    put_started = asyncio.Event()
    allow_put = asyncio.Event()
    events: list[str] = []

    async def _paused_put(
        *,
        cache_key: str,
        base_key: str,
        op: str,
        user_scope: str,
        req_limit: int | None,
        result_count: int,
        has_more: bool,
        payload: dict[str, Any],
        date_to: str,
    ) -> tuple[float, float] | None:
        events.append("put_start")
        put_started.set()
        await allow_put.wait()
        res = await orig_put(
            cache_key=cache_key,
            base_key=base_key,
            op=op,
            user_scope=user_scope,
            req_limit=req_limit,
            result_count=result_count,
            has_more=has_more,
            payload=payload,
            date_to=date_to,
        )
        events.append("put_done")
        return res

    monkeypatch.setattr(cache, "put", create_autospec(orig_put, side_effect=_paused_put))

    upstream_count = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_count
        upstream_count += 1
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(3, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    args = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "TSMC",
            "start": "2026-10-01",
            "end": "2026-11-10",
            "limit": 3,
        },
    }
    t1 = asyncio.create_task(bridge.handle_tools_call("r2-1-a", args))
    await asyncio.wait_for(put_started.wait(), timeout=5.0)
    # Waiter arrives AFTER upstream returned, BEFORE SQLite put completes!
    t2 = asyncio.create_task(bridge.handle_tools_call("r2-1-b", args))
    await asyncio.sleep(0.02)
    assert not t2.done(), "waiter must wait for cache.put before returning"
    allow_put.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert upstream_count == 1
    assert events == ["put_start", "put_done"]
    b1 = json.loads(r1["result"]["content"][0]["text"])
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b1["_bridge_cache"]["status"] == "miss"
    assert b2["_bridge_cache"]["status"] == "coalesced"
    assert b1["_bridge_cache"]["expires_at"] == b2["_bridge_cache"]["expires_at"]
    assert b1["_bridge_cache"]["fetched_at"] == b2["_bridge_cache"]["fetched_at"]


@pytest.mark.asyncio
async def test_r2_2_fresh_refresh_coalesces_concurrent_requests_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=2)
    cache = bridge._get_alphaengine_search_cache()
    await cache.initialize()

    base_args = {
        "route": "alphaengine",
        "operation": "search_library",
        "query": "Apple",
        "start": "2026-10-01",
        "end": "2026-11-10",
        "limit": 3,
    }
    plan = ae_cache.prepare_alphaengine_search_request(base_args, op=ae_cache.OP_SEARCH_LIBRARY)
    await cache.put(
        cache_key=plan.cache_key,
        base_key=plan.base_key,
        op=plan.op,
        user_scope=plan.user_scope,
        req_limit=plan.req_limit,
        result_count=1,
        has_more=False,
        payload=_ae_results_payload(1, has_more=False, prefix="old_"),
        date_to=plan.date_to,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    upstream_fresh_calls = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_fresh_calls
        upstream_fresh_calls += 1
        entered.set()
        await release.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False, prefix="fresh_"))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    fresh_req = {"name": "research", "arguments": {**base_args, "params": {"fresh": True}}}
    fresh_tasks = [asyncio.create_task(bridge.handle_tools_call(f"f-{i}", fresh_req)) for i in range(5)]
    await asyncio.wait_for(entered.wait(), timeout=5.0)

    # Normal request uses cache directly without waiting on #fresh flight.
    normal_res = await bridge.handle_tools_call("n-0", {"name": "research", "arguments": base_args})
    normal_body = json.loads(normal_res["result"]["content"][0]["text"])
    assert normal_body["_bridge_cache"]["status"] == "hit"
    assert normal_body["data"]["structuredContent"]["results"][0]["doc_id"].startswith("old_")

    release.set()
    fresh_results = await asyncio.gather(*fresh_tasks)
    assert upstream_fresh_calls == 1
    statuses = [json.loads(r["result"]["content"][0]["text"])["_bridge_cache"]["status"] for r in fresh_results]
    assert statuses.count("fresh") == 1
    assert statuses.count("coalesced") == 4


@pytest.mark.asyncio
async def test_r2_3_limit_and_date_conflicts_rejected_and_nested_canonical_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    # 1. Conflicting top vs nested limit/date_from/date_to must be rejected as parameter_conflict
    for bad_args in (
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA",
            "limit": 10,
            "start": "2026-10-01",
            "end": "2026-10-10",
            "params": {"limit": 3},
        },
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA",
            "start": "2026-10-01",
            "end": "2026-10-10",
            "params": {"date_from": "2026-09-01"},
        },
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA",
            "start": "2026-10-01",
            "end": "2026-10-10",
            "params": {"date_to": "2026-10-05"},
        },
    ):
        resp = await bridge.handle_tools_call("conflict", {"name": "research", "arguments": bad_args})
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["error_code"] == "parameter_conflict"

    # 2. Nested-only params.limit / date_from / date_to shares same cache key and sends canonical args upstream
    sent_args: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        sent_args.append(dict(requests[0]["params"]["arguments"]))
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    r1 = await bridge.handle_tools_call(
        "nested-only",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "AMD",
                "params": {"limit": 7, "date_from": "2026-10-01", "date_to": "2026-10-10"},
            },
        },
    )
    r2 = await bridge.handle_tools_call(
        "top-only",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "AMD",
                "limit": 7,
                "start": "2026-10-01",
                "end": "2026-10-10",
            },
        },
    )
    assert len(sent_args) == 1
    assert sent_args[0]["limit"] == 7
    assert sent_args[0]["start"] == "2026-10-01"
    assert sent_args[0]["end"] == "2026-10-10"
    assert "limit" not in (sent_args[0].get("params") or {})
    assert json.loads(r1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"
    assert json.loads(r2["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "hit"


@pytest.mark.asyncio
async def test_r2_4_mode_plan_bypasses_and_mode_in_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        args = requests[0]["params"]["arguments"]
        calls.append(dict(args))
        mode = args.get("mode") or "fetch"
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False, prefix=mode))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # Two identical mode="plan" calls -> both bypass and neither poisons cache
    for i in range(2):
        res_plan = await bridge.handle_tools_call(
            f"plan-{i}",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "ASML",
                    "start": "2026-10-01",
                    "end": "2026-11-10",
                    "mode": "plan",
                },
            },
        )
        b_plan = json.loads(res_plan["result"]["content"][0]["text"])
        assert b_plan["_bridge_cache"]["status"] == "bypass"

    # Subsequent mode="fetch" (or omitted) must miss and write fetch result
    res_fetch = await bridge.handle_tools_call(
        "fetch-1",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "ASML",
                "start": "2026-10-01",
                "end": "2026-11-10",
                "mode": "fetch",
            },
        },
    )
    res_default = await bridge.handle_tools_call(
        "fetch-2",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "ASML",
                "start": "2026-10-01",
                "end": "2026-11-10",
            },
        },
    )
    assert len(calls) == 3
    assert json.loads(res_fetch["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"
    assert json.loads(res_default["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "hit"


@pytest.mark.asyncio
async def test_r2_5_transcript_slicing_and_has_more_false_null_limit_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    upstream_calls: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        args = requests[0]["params"]["arguments"]
        upstream_calls.append(dict(args))
        route = args["route"]
        lim = args.get("limit")
        if route == "transcript":
            n = int(lim) if lim is not None else 8
            return [_mcp_reply(requests[0]["id"], _transcript_payload(n, complete=True))]
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(8, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # Part A: transcript with limit=3, complete=True MUST NOT satisfy limit=10!
    await bridge.handle_tools_call(
        "tr-3",
        {"name": "research", "arguments": {"route": "transcript", "ticker": "META", "start": "2026-10-01", "end": "2026-10-31", "limit": 3}},
    )
    res_tr10 = await bridge.handle_tools_call(
        "tr-10",
        {"name": "research", "arguments": {"route": "transcript", "ticker": "META", "start": "2026-10-01", "end": "2026-10-31", "limit": 10}},
    )
    assert len(upstream_calls) == 2
    assert json.loads(res_tr10["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"

    # Part B: transcript stored WITHOUT limit (req_limit=None, 8 actual items):
    # - limit=5 CAN slice down to 5 items
    # - limit=12 CANNOT confirm -> must miss!
    upstream_calls.clear()
    await bridge.handle_tools_call(
        "tr-null",
        {"name": "research", "arguments": {"route": "transcript", "ticker": "GOOG", "start": "2026-10-01", "end": "2026-10-31"}},
    )
    res_tr5 = await bridge.handle_tools_call(
        "tr-5",
        {"name": "research", "arguments": {"route": "transcript", "ticker": "GOOG", "start": "2026-10-01", "end": "2026-10-31", "limit": 5}},
    )
    b_tr5 = json.loads(res_tr5["result"]["content"][0]["text"])
    assert b_tr5["_bridge_cache"]["status"] == "hit"
    assert len(b_tr5["data"]["transcripts"]) == 5
    assert b_tr5["_bridge_cache"]["sliced_from_limit"] == 8
    assert len(upstream_calls) == 1

    res_tr12 = await bridge.handle_tools_call(
        "tr-12",
        {"name": "research", "arguments": {"route": "transcript", "ticker": "GOOG", "start": "2026-10-01", "end": "2026-10-31", "limit": 12}},
    )
    assert len(upstream_calls) == 2
    assert json.loads(res_tr12["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"

    # Part C (GLM #1): search_library row stored with req_limit=None, has_more=False, 8 items
    # MUST still slice down to limit=3 when matched!
    cache = bridge._get_alphaengine_search_cache()
    plan_null = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "CRM", "start": "2026-10-01", "end": "2026-10-31", "limit": 8},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    await cache.put(
        cache_key=plan_null.base_key + ":null",
        base_key=plan_null.base_key,
        op=ae_cache.OP_SEARCH_LIBRARY,
        user_scope="team",
        req_limit=None,
        result_count=8,
        has_more=False,
        payload=_ae_results_payload(8, has_more=False),
        date_to="2026-10-31",
    )
    res_crm3 = await bridge.handle_tools_call(
        "crm-3",
        {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "CRM", "start": "2026-10-01", "end": "2026-10-31", "limit": 3}},
    )
    b_crm3 = json.loads(res_crm3["result"]["content"][0]["text"])
    assert b_crm3["_bridge_cache"]["status"] == "hit"
    sc = b_crm3["data"]["structuredContent"]
    assert len(sc["results"]) == 3
    assert sc["returned"] == 3
    assert sc["has_more"] is True
    assert b_crm3["_bridge_cache"]["sliced_from_limit"] == 8


@pytest.mark.asyncio
async def test_r2_6_guidepoint_semaphore_cap_and_max_inflight_1_no_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=1)
    active = 0
    max_active = 0
    routes_seen: list[str] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal active, max_active
        req = requests[0]
        route = req["params"]["arguments"]["route"]
        routes_seen.append(route)
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            if route == "alphaengine":
                return [_mcp_reply(req["id"], _ae_results_payload(2, has_more=False))]
            if route == "guidepoint":
                return [_mcp_reply(req["id"], {"ok": True, "data": {"structuredContent": {"data": []}}})]
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        finally:
            active -= 1

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    r1, r2 = await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call("all-1", {"name": "research", "arguments": {"route": "all", "operation": "search", "query": "TSLA"}}),
            bridge.handle_tools_call("all-2", {"name": "research", "arguments": {"route": "all", "operation": "search", "query": "MSFT"}}),
        ),
        timeout=5.0,
    )
    assert json.loads(r1["result"]["content"][0]["text"])["ok"] is True
    assert json.loads(r2["result"]["content"][0]["text"])["ok"] is True
    assert "guidepoint" in routes_seen
    assert max_active == 1


@pytest.mark.asyncio
async def test_r2_7_path_permission_error_degrades_to_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    orig_exists = Path.exists

    def _guarded_exists(self_path: Path) -> bool:
        if self_path == cache.db_path or self_path == cache.db_path.parent:
            raise PermissionError("simulated permission denied")
        return orig_exists(self_path)

    monkeypatch.setattr(Path, "exists", _guarded_exists)

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await bridge.handle_tools_call(
        "perm-err",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "INTC",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 3,
            },
        },
    )
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["_bridge_cache"]["status"] == "bypass"


@pytest.mark.asyncio
async def test_r2_8_sliced_hit_note_and_honest_has_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(4, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    await bridge.handle_tools_call(
        "w-10",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "AVGO",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 10,
            },
        },
    )

    # Not truncated (4 <= 6, has_more=False) -> honest has_more=False, sliced_from_limit=10, note present
    r_not_trunc = await bridge.handle_tools_call(
        "s-6",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "AVGO",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 6,
            },
        },
    )
    b6 = json.loads(r_not_trunc["result"]["content"][0]["text"])
    assert b6["data"]["structuredContent"]["has_more"] is False
    assert b6["_bridge_cache"]["sliced_from_limit"] == 10
    assert "limit" in b6["_bridge_cache"]["note"]

    # Truncated (4 > 2) -> has_more=True, cursor=None, sliced_from_limit=10, note present
    r_trunc = await bridge.handle_tools_call(
        "s-2",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "AVGO",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 2,
            },
        },
    )
    b2 = json.loads(r_trunc["result"]["content"][0]["text"])
    assert b2["data"]["structuredContent"]["has_more"] is True
    assert b2["data"]["structuredContent"]["cursor"] is None
    assert b2["_bridge_cache"]["sliced_from_limit"] == 10
    assert "limit" in b2["_bridge_cache"]["note"]


@pytest.mark.asyncio
async def test_r2_9_timeout_and_jsonrpc_error_accounting_and_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=2)
    leader_entered = asyncio.Event()
    release_leader = asyncio.Event()

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        args = requests[0]["params"]["arguments"]
        if str(args.get("query") or "").upper() == "RPCERR":
            return [{"jsonrpc": "2.0", "id": requests[0]["id"], "error": {"code": -32000, "message": "upstream err"}}]
        if (args.get("params") or {}).get("cursor") == "cur-1":
            raise asyncio.TimeoutError()
        leader_entered.set()
        await release_leader.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _slow_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    args = {
        "route": "alphaengine",
        "operation": "search_library",
        "query": "SLOWCO",
        "start": "2026-10-01",
        "end": "2026-10-31",
        "limit": 3,
        "timeout": 10,
    }
    t_leader = asyncio.create_task(bridge.handle_tools_call("leader", {"name": "research", "arguments": args}))
    await asyncio.wait_for(leader_entered.wait(), timeout=5.0)
    # Force waiter timeout while leader stays open
    orig_wait_for = asyncio.wait_for

    async def _fast_timeout(awaitable: Any, timeout: float | None = None) -> Any:
        # Cancel wait_for immediately for the waiter coroutine.
        return await orig_wait_for(awaitable, timeout=0.02)

    monkeypatch.setattr(asyncio, "wait_for", _fast_timeout)
    r_waiter = await bridge.handle_tools_call("waiter", {"name": "research", "arguments": args})
    monkeypatch.setattr(asyncio, "wait_for", orig_wait_for)
    release_leader.set()
    await t_leader

    w_body = json.loads(r_waiter["result"]["content"][0]["text"])
    assert w_body["_bridge_cache"]["status"] == "coalesced"

    # Cursor bypass timeout
    r_cur = await bridge.handle_tools_call(
        "cur-to",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "CURCO",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "params": {"cursor": "cur-1"},
            },
        },
    )
    assert json.loads(r_cur["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "bypass"

    # JSON-RPC error response must carry _bridge_cache
    r_rpc_err = await bridge.handle_tools_call(
        "rpc-err",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "RPCERR",
                "start": "2026-10-01",
                "end": "2026-10-31",
            },
        },
    )
    assert "error" in r_rpc_err
    assert r_rpc_err["error"]["data"]["_bridge_cache"]["status"] == "miss"

    ledger_rows = [
        json.loads(line)
        for line in Path(os.environ["DALTON_ALPHAENGINE_USAGE_FILE"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    coalesced_rows = [r for r in ledger_rows if r.get("cache") == "coalesced"]
    assert len(coalesced_rows) == 1 and coalesced_rows[0]["upstream"] is False
    bypass_rows = [r for r in ledger_rows if r.get("cache") == "bypass"]
    assert len(bypass_rows) == 1 and bypass_rows[0]["upstream"] is True


@pytest.mark.asyncio
async def test_r2_10_sigterm_cancels_main_and_awaits_bridge_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "tok")
    monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DALTON_ALPHAENGINE_CACHE_DB", str(tmp_path / "cache.sqlite3"))
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))

    registered_handlers: dict[int, Any] = {}
    loop = asyncio.get_running_loop()
    orig_add = loop.add_signal_handler
    orig_rem = loop.remove_signal_handler

    def _capture_add(sig: int, cb: Any, *args: Any) -> None:
        registered_handlers[sig] = (cb, args)
        orig_add(sig, cb, *args)

    def _capture_rem(sig: int) -> bool:
        return orig_rem(sig)

    monkeypatch.setattr(loop, "add_signal_handler", _capture_add)
    monkeypatch.setattr(loop, "remove_signal_handler", _capture_rem)

    close_awaited = asyncio.Event()
    serving_started = asyncio.Event()
    server_cls = create_autospec(asyncio.Server, instance=True)

    async def _serve_forever() -> None:
        serving_started.set()
        await asyncio.Future()

    server_cls.__aenter__.return_value = server_cls
    server_cls.__aexit__.return_value = False
    server_cls.serve_forever.side_effect = _serve_forever

    async def _fake_start_server(*args: Any, **kwargs: Any) -> Any:
        return server_cls

    start_server_mock = create_autospec(asyncio.start_server)
    start_server_mock.side_effect = _fake_start_server
    monkeypatch.setattr(asyncio, "start_server", start_server_mock)

    orig_close = rb.ResearchDataBridge.close

    async def _spy_close(self: rb.ResearchDataBridge) -> None:
        await orig_close(self)
        close_awaited.set()

    monkeypatch.setattr(rb.ResearchDataBridge, "close", _spy_close)
    ens_init = create_autospec(rb.ResearchDataBridge.ensure_initialized)
    monkeypatch.setattr(rb.ResearchDataBridge, "ensure_initialized", ens_init)

    main_task = asyncio.create_task(rb.main())
    await asyncio.wait_for(serving_started.wait(), timeout=5.0)
    assert signal.SIGTERM in registered_handlers
    assert signal.SIGINT in registered_handlers

    cb, cb_args = registered_handlers[signal.SIGTERM]
    cb(*cb_args)
    await asyncio.wait_for(main_task, timeout=5.0)
    assert close_awaited.is_set()


def test_r2_11_old_supervisor_replaced_or_fallback_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcr, "_supervisor_unavailable", False)
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(mcr, "_workspace_view", create_autospec(mcr._workspace_view, return_value={}))
    monkeypatch.delenv("MCP_SUPERVISOR", raising=False)
    monkeypatch.setenv("DALTON_CALLER_USER", "alice")

    # Part A: Live daemon supports caller_identity -> forwards caller without invoking --retire
    seen_call_requests: list[dict[str, Any]] = []

    def _exchange_supported(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req.get("op") == "health":
            return "result", {"type": "result", "data": {"ok": True, "caller_identity": True}}
        seen_call_requests.append(dict(req))
        return "reply", {"type": "reply", "reply": {"result": {"ok": True}}}

    sub_run = create_autospec(subprocess.run)
    orig_exchange = mcr._supervisor_exchange
    exchange_mock = create_autospec(orig_exchange, side_effect=_exchange_supported)
    monkeypatch.setattr(mcr, "_socket_alive", create_autospec(mcr._socket_alive, return_value=True))
    monkeypatch.setattr(mcr, "_supervisor_exchange", exchange_mock)
    monkeypatch.setattr(mcr.subprocess, "run", sub_run)

    reply = mcr._supervisor_reply("researchdata", "research", {"route": "alphaengine"})
    assert reply == {"result": {"ok": True}}
    assert sub_run.call_count == 0
    assert seen_call_requests[0]["caller"]["user"] == "alice"

    # Part B: Legacy daemon without caller_identity -> strips caller and calls without failing
    seen_call_requests.clear()

    def _exchange_old_forever(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req.get("op") == "health":
            return "result", {"type": "result", "data": {"ok": True}}
        seen_call_requests.append(dict(req))
        return "reply", {"type": "reply", "reply": {"result": {"ok": "old-still-works"}}}

    exchange_mock.side_effect = _exchange_old_forever

    reply_old = mcr._supervisor_reply("researchdata", "research", {"route": "alphaengine"})
    assert reply_old == {"result": {"ok": "old-still-works"}}
    assert len(seen_call_requests) == 1
    assert "caller" not in seen_call_requests[0]

    # Supervisor.health() itself advertises caller_identity: True
    backend = sup_daemon.ClientBackend(mcr)
    sup = sup_daemon.Supervisor(str(tmp_path / "s.sock"), backend, log=lambda _m: None)
    try:
        assert sup.health()["caller_identity"] is True
    finally:
        sup.close()


def test_r2_12_no_dead_pending_cache_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    assert not hasattr(bridge, "_pending_cache_writes")


# ---------------------------------------------------------------------------
# Round 3 regression tests (1–10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_1_read_and_register_atomic_inside_shared_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    orig_get = cache.get
    first_get_paused = asyncio.Event()
    release_first_get = asyncio.Event()
    get_count = 0

    async def _get_race(
        cache_key: str, base_key: str, req_limit: int | None, *, op: str, **kwargs: Any
    ) -> tuple[ae_cache.CacheHit | None, bool]:
        nonlocal get_count
        get_count += 1
        call_no = get_count
        res = await orig_get(cache_key, base_key, req_limit, op=op, **kwargs)
        if call_no == 1:
            first_get_paused.set()
            await release_first_get.wait()
        return res

    monkeypatch.setattr(cache, "get", create_autospec(orig_get, side_effect=_get_race))

    upstream_count = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_count
        upstream_count += 1
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(3, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    req = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "SNOW",
            "start": "2026-10-01",
            "end": "2026-11-10",
            "limit": 3,
        },
    }
    t_slow = asyncio.create_task(bridge.handle_tools_call("r3-slow", req))
    try:
        await asyncio.wait_for(first_get_paused.wait(), timeout=5.0)
        # Concurrent arrival while first shared task's cache.get is mid-flight
        t_fast = asyncio.create_task(bridge.handle_tools_call("r3-fast", req))
        await asyncio.sleep(0.02)
    finally:
        release_first_get.set()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t_slow, t_fast), timeout=5.0)

    assert upstream_count == 1
    assert get_count == 1
    assert json.loads(r1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"
    assert json.loads(r2["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "coalesced"


@pytest.mark.asyncio
async def test_r3_2_route_all_mode_plan_preserves_mode_and_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    ae_sent_modes: list[Any] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        args = requests[0]["params"]["arguments"]
        route = args["route"]
        if route == "alphaengine":
            ae_sent_modes.append(args.get("mode"))
            return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]
        if route == "all":
            return [_mcp_reply(requests[0]["id"], {"ok": True, "status": "ok", "results": []})]
        return [_mcp_reply(requests[0]["id"], {"ok": True, "data": {"structuredContent": {"data": []}}})]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    plan_req = {
        "name": "research",
        "arguments": {"route": "all", "operation": "search", "query": "ASML", "mode": "plan"},
    }
    p1 = await bridge.handle_tools_call("all-plan-1", plan_req)
    p2 = await bridge.handle_tools_call("all-plan-2", plan_req)
    b1 = json.loads(p1["result"]["content"][0]["text"])
    b2 = json.loads(p2["result"]["content"][0]["text"])
    assert b1["alphaengine"]["_bridge_cache"]["status"] == "bypass"
    assert b2["alphaengine"]["_bridge_cache"]["status"] == "bypass"
    assert ae_sent_modes == ["plan", "plan"]

    f1 = await bridge.handle_tools_call(
        "all-fetch-1",
        {"name": "research", "arguments": {"route": "all", "operation": "search", "query": "ASML"}},
    )
    bf1 = json.loads(f1["result"]["content"][0]["text"])
    assert bf1["alphaengine"]["_bridge_cache"]["status"] == "miss"
    assert len(ae_sent_modes) == 3


@pytest.mark.asyncio
async def test_r3_3_content_text_json_shape_metrics_and_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    items = [
        {
            "doc_id": f"txt_{i}",
            "title": f"Text JSON Report {i}",
            "publish_time": "2026-11-10",
            "sources": ["Morgan Stanley"],
        }
        for i in range(1, 11)
    ]
    text_json_payload = {
        "ok": True,
        "status": "ok",
        "data": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"results": items, "returned": 10, "has_more": True, "cursor": "cur-x"},
                        ensure_ascii=False,
                    ),
                }
            ],
            "isError": False,
        },
    }
    upstream_calls = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_calls
        upstream_calls += 1
        return [_mcp_reply(requests[0]["id"], copy_deep(text_json_payload))]

    def copy_deep(obj: Any) -> Any:
        return json.loads(json.dumps(obj, ensure_ascii=False))

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    await bridge.handle_tools_call(
        "txt-10",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "TSMC CoWoS",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 10,
            },
        },
    )
    res_3 = await bridge.handle_tools_call(
        "txt-3",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "TSMC CoWoS",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 3,
            },
        },
    )
    assert upstream_calls == 1
    b3 = json.loads(res_3["result"]["content"][0]["text"])
    assert b3["_bridge_cache"]["status"] == "hit"
    assert b3["_bridge_cache"]["sliced_from_limit"] == 10
    inner_parsed = json.loads(b3["data"]["content"][0]["text"])
    assert len(inner_parsed["results"]) == 3
    assert inner_parsed["returned"] == 3
    assert inner_parsed["has_more"] is True
    assert inner_parsed["cursor"] is None


@pytest.mark.asyncio
async def test_r3_4_normalized_params_written_back_to_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    sent: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        sent.append(dict(requests[0]["params"]["arguments"]))
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    await bridge.handle_tools_call(
        "norm-a",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "  ＡＰＰＬ   ｉＰｈｏｎｅ  ",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "params": {"type_ids": ["B", "A", "B"], "fresh": True},
            },
        },
    )
    await bridge.handle_tools_call(
        "norm-b",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "APPL iPhone",
                "start": "2026-10-01",
                "end": "2026-10-31",
                "limit": 3,
                "params": {"type_ids": ["A", "B"], "sort": "relevance", "fresh": True},
            },
        },
    )
    assert len(sent) == 2
    # Ignore timeout if one had default vs omitted; query/limit/params must match identically
    for key in ("query", "start", "end", "limit", "params"):
        assert sent[0].get(key) == sent[1].get(key), f"mismatch on {key}: {sent[0]} vs {sent[1]}"
    assert sent[0]["query"] == "APPL iPhone"
    assert sent[0]["limit"] == 3
    assert sent[0]["params"] == {"type_ids": ["A", "B"], "sort": "relevance"}


def test_r3_5_old_supervisor_no_auto_replace_strips_caller_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcr, "_supervisor_unavailable", False)
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(mcr, "_workspace_view", create_autospec(mcr._workspace_view, return_value={}))
    monkeypatch.delenv("MCP_SUPERVISOR", raising=False)
    monkeypatch.setenv("DALTON_CALLER_USER", "alice")

    sup_dir = tmp_path / mcr._LAYOUT["INTERNAL_SRC_DIR"] / mcr._SUPERVISOR_PACKAGE
    sup_dir.mkdir(parents=True, exist_ok=True)
    (sup_dir / "daemon.py").write_text("# daemon\n", encoding="utf-8")

    # Never invoke subprocess.run(--retire) for auto-replacement
    sub_run = create_autospec(subprocess.run)
    monkeypatch.setattr(mcr.subprocess, "run", sub_run)
    monkeypatch.setattr(mcr, "_socket_alive", create_autospec(mcr._socket_alive, return_value=True))

    upgraded = {"ok": False}
    seen_calls: list[dict[str, Any]] = []

    def _exchange(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req.get("op") == "health":
            return "result", {
                "type": "result",
                "data": {"ok": True, **({"caller_identity": True} if upgraded["ok"] else {})},
            }
        seen_calls.append(dict(req))
        return "reply", {"type": "reply", "reply": {"result": {"ok": True}}}

    monkeypatch.setattr(
        mcr, "_supervisor_exchange", create_autospec(mcr._supervisor_exchange, side_effect=_exchange)
    )

    # First two calls against legacy daemon: BOTH must strip caller, neither spawns replacement
    r1 = mcr._supervisor_reply("researchdata", "research", {"route": "alphaengine"})
    r2 = mcr._supervisor_reply("researchdata", "research", {"route": "alphaengine"})
    assert r1 == {"result": {"ok": True}}
    assert r2 == {"result": {"ok": True}}
    assert sub_run.call_count == 0
    assert "caller" not in seen_calls[0]
    assert "caller" not in seen_calls[1]

    # Once daemon restarts with caller_identity capability, subsequent call forwards caller immediately
    upgraded["ok"] = True
    r3 = mcr._supervisor_reply("researchdata", "research", {"route": "alphaengine"})
    assert r3 == {"result": {"ok": True}}
    assert seen_calls[2]["caller"]["user"] == "alice"


@pytest.mark.asyncio
async def test_r3_6_semaphore_wait_timeout_records_upstream_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=1)
    hold_started = asyncio.Event()
    release_hold = asyncio.Event()

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        args = requests[0]["params"]["arguments"]
        if str(args.get("query") or "").upper() == "HOLD":
            hold_started.set()
            await release_hold.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    t_hold = asyncio.create_task(
        bridge.handle_tools_call(
            "hold",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "HOLD", "start": "2026-10-01", "end": "2026-10-31"}},
        )
    )
    orig_wait_for = asyncio.wait_for
    try:
        await orig_wait_for(hold_started.wait(), timeout=5.0)

        async def _short_wait(aw: Any, timeout: float | None = None) -> Any:
            return await orig_wait_for(aw, timeout=0.02)

        monkeypatch.setattr(asyncio, "wait_for", _short_wait)
        await orig_wait_for(
            bridge.handle_tools_call(
                "q-cache",
                {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "QUEUECO", "start": "2026-10-01", "end": "2026-10-31"}},
            ),
            timeout=1.0,
        )
        await orig_wait_for(
            bridge.handle_tools_call(
                "q-plan",
                {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "QUEUEPLAN", "start": "2026-10-01", "end": "2026-10-31", "mode": "plan"}},
            ),
            timeout=1.0,
        )
    finally:
        monkeypatch.setattr(asyncio, "wait_for", orig_wait_for)
        release_hold.set()
        await orig_wait_for(t_hold, timeout=5.0)

    rows = [
        json.loads(line)
        for line in Path(os.environ["DALTON_ALPHAENGINE_USAGE_FILE"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed_timeouts = [r for r in rows if r.get("ok") is False]
    assert len(failed_timeouts) == 2
    assert all(r["upstream"] is False for r in failed_timeouts)


@pytest.mark.asyncio
async def test_r3_7_invalid_limit_rejected_and_top_ticker_in_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    for bad_args in (
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA", "limit": 0},
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA", "limit": -3},
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA", "limit": "abc"},
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA", "params": {"limit": 0}},
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA", "params": {"limit": "bad"}},
    ):
        resp = await bridge.handle_tools_call("bad-lim", {"name": "research", "arguments": bad_args})
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["status"] == "invalid_arguments"

    p_meta = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "x", "ticker": "META"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_aapl = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "x", "ticker": "AAPL"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_meta.cache_key != p_aapl.cache_key
    assert p_meta.base_key != p_aapl.base_key


@pytest.mark.asyncio
async def test_r3_8_degraded_db_still_coalesces_concurrent_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=2)
    cache_db = bridge._get_alphaengine_search_cache().db_path
    cache_db.write_text("not sqlite bytes", encoding="utf-8")

    entered = asyncio.Event()
    release = asyncio.Event()
    upstream_count = 0

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_count
        upstream_count += 1
        entered.set()
        await release.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc))

    args = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "DEGRADE",
            "start": "2026-10-01",
            "end": "2026-10-31",
            "limit": 3,
        },
    }
    t0 = asyncio.create_task(bridge.handle_tools_call("d-0", args))
    waiters: list[asyncio.Task[Any]] = []
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        waiters = [asyncio.create_task(bridge.handle_tools_call(f"d-{i}", args)) for i in range(1, 4)]
        await asyncio.sleep(0.02)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(t0, *waiters), timeout=5.0)

    assert upstream_count == 1
    statuses = [json.loads(r["result"]["content"][0]["text"])["_bridge_cache"]["status"] for r in results]
    assert statuses.count("bypass") == 1
    assert statuses.count("coalesced") == 3


# ---------------------------------------------------------------------------
# Round 4 regression tests (1–7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r4_1_personal_meeting_variants_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    upstream_calls = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_calls
        upstream_calls += 1
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(upstream_calls))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    for params in (
        {"meeting_scope": "personal"},
        {"own_reservations_only": True},
        {"document_categories": ["personal_meetings"]},
        {"document_codes": ["personalMeeting"]},
        {"type_ids": ["personalMeeting"]},
    ):
        args = {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "private summary",
                "params": dict(params),
            },
        }
        r1 = await bridge.handle_tools_call("p1", args, caller_identity={"user": "alice", "workspace": "w1", "thread": "t1"})
        r2 = await bridge.handle_tools_call("p2", args, caller_identity={"user": "alice", "workspace": "w1", "thread": "t1"})
        r3 = await bridge.handle_tools_call("p3", args, caller_identity={"user": "bob", "workspace": "w2", "thread": "t2"})
        for r in (r1, r2, r3):
            assert json.loads(r["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "bypass"

    assert upstream_calls == 15
    assert "user:" not in json.dumps(ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "params": {"document_codes": ["personalMeeting"]}},
        op=ae_cache.OP_SEARCH_LIBRARY,
        caller_user="alice",
    ).__dict__)


@pytest.mark.asyncio
async def test_r4_2_legacy_supervisor_http_bypasses_daemon_env_and_bridge_clears_unscoped_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Part A: legacy supervisor daemon (no `caller_identity` capability) on HTTP server
    # is bypassed via in-process `_call_mcp_tool_http`, never leaking stale daemon env.
    monkeypatch.setattr(mcr, "_supervisor_unavailable", False)
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(mcr, "_WORKSPACE_VIEW_CACHE", {"view": {"workspace_id": "ws1", "servers": {"researchdata": {}}}})
    monkeypatch.setattr(
        mcr,
        "_SERVER_CONFIGS",
        {"researchdata": mcr._normalize("researchdata", {"transport": "http", "untrusted": False, "url": "http://127.0.0.1:8960/mcp"})},
    )
    monkeypatch.delenv("MCP_SUPERVISOR", raising=False)
    monkeypatch.setenv("DALTON_CALLER_USER", "stale-daemon-alice")
    monkeypatch.setattr(mcr, "_socket_alive", create_autospec(mcr._socket_alive, return_value=True))

    legacy_calls: list[dict[str, Any]] = []

    def _legacy_exchange(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req.get("op") == "health":
            return "result", {"type": "result", "data": {"ok": True}}
        legacy_calls.append(dict(req))
        return "reply", {"type": "reply", "reply": {"result": {"ok": True}}}

    monkeypatch.setattr(
        mcr, "_supervisor_exchange", create_autospec(mcr._supervisor_exchange, side_effect=_legacy_exchange)
    )
    inproc_callers: list[dict[str, str] | None] = []

    def _fake_http_exchange(
        server_name: str, tool_name: str, arguments: dict[str, Any], *, caller: dict[str, str] | None = None
    ) -> dict[str, Any]:
        inproc_callers.append(dict(caller or {}))
        return {"result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}

    monkeypatch.setattr(
        mcr, "_http_exchange", create_autospec(mcr._http_exchange, side_effect=_fake_http_exchange)
    )

    # Caller with no DALTON_CALLER_* env -> supervisor NOT used for HTTP, in-process caller is empty
    monkeypatch.delenv("DALTON_CALLER_USER", raising=False)
    res_anon = mcr._call_mcp_tool_dispatch("researchdata", "research", {"route": "alphaengine"})
    assert res_anon == {"ok": True}
    assert legacy_calls == []
    assert inproc_callers == [{}]

    # Part B: bridge HTTP handler treats unsigned/unscoped X-Dalton-User headers as empty identity
    # (even when X-Dalton-Caller-Scope: per-request is sent without a signed token), and only
    # accepts identity from a verified X-Dalton-Caller-Token.
    from src.dalton.sandbox_caller_identity import sign_caller_token
    import time as _time

    bridge = _make_bridge(tmp_path / "b", monkeypatch)

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        return [_mcp_reply(req["id"], _ae_results_payload(1, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    signed_tok = sign_caller_token(
        {"user": "alice", "workspace": "ws-a", "thread": "th-a"},
        exp=int(_time.time()) + 1800,
        admin_token="r4-2-admin-secret",
    )
    assert signed_tok

    for idx, extra_hdr in enumerate((
        {},  # legacy daemon/client sending X-Dalton-User without X-Dalton-Caller-Scope
        {"X-Dalton-Caller-Scope": "per-request"},  # unsigned per-request headers also stripped
        {"X-Dalton-Caller-Scope": "per-request", "X-Dalton-Caller-Token": signed_tok},
    )):
        if idx == 2:
            bridge.cfg.admin_token = "r4-2-admin-secret"
        body_bytes = json.dumps({
            "jsonrpc": "2.0",
            "id": f"http-{idx}",
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {"route": "alphaengine", "operation": "search_library", "query": f"Q{idx}"},
            },
        }).encode("utf-8")
        raw_req = (
            "POST /mcp HTTP/1.1\r\n"
            "Host: 127.0.0.1:8960\r\n"
            "Authorization: Bearer test-token\r\n"
            "X-Dalton-User: alice\r\n"
            "X-Dalton-Workspace: ws-a\r\n"
            "X-Dalton-Thread: th-a\r\n"
            + "".join(f"{hk}: {hv}\r\n" for hk, hv in extra_hdr.items())
            + f"Content-Length: {len(body_bytes)}\r\n\r\n"
        ).encode("utf-8") + body_bytes
        reader = asyncio.StreamReader()
        reader.feed_data(raw_req)
        reader.feed_eof()
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        await asyncio.wait_for(rb._handle_client(bridge, reader, writer), timeout=5.0)

    entries = [json.loads(line) for line in bridge.cfg.alphaengine_usage_file.read_text("utf-8").splitlines() if line.strip()]
    assert len(entries) == 3
    assert (entries[0]["user"], entries[0]["workspace"], entries[0]["thread"]) == ("", "", "")
    assert (entries[1]["user"], entries[1]["workspace"], entries[1]["thread"]) == ("", "", "")
    assert (entries[2]["user"], entries[2]["workspace"], entries[2]["thread"]) == ("alice", "ws-a", "th-a")


def test_r4_3_query_normalization_preserves_case_in_upstream_and_v3_lowercases_v4() -> None:
    p_full = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "  ＮＶＤＡ\tＡＩ  "},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_ascii = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA AI"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_lower = ae_cache.prepare_alphaengine_search_request(
        {"route": "alphaengine", "operation": "search_library", "query": "nvda ai"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_full.upstream_arguments["query"] == "NVDA AI"
    assert p_full.cache_key == p_ascii.cache_key
    assert p_lower.cache_key == p_ascii.cache_key
    assert p_lower.legacy_cache_key != p_ascii.legacy_cache_key
    assert p_lower.upstream_arguments["query"] == "nvda ai"


@pytest.mark.asyncio
async def test_r4_4_cursor_continuation_normalizes_params_identically_to_first_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    sent: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        sent.append(dict(requests[0]["params"]["arguments"]))
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=True, cursor="cur-2"))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc))

    r1 = await bridge.handle_tools_call(
        "p-first",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "  ＮＶＤＡ  Blackwell ",
                "params": {"type_ids": ["B", "A", "B"]},
            },
        },
    )
    r2 = await bridge.handle_tools_call(
        "p-next",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "NVDA\tBlackwell",
                "params": {"cursor": "cur-2", "type_ids": ["A", "B"]},
            },
        },
    )
    assert len(sent) == 2
    assert json.loads(r1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"
    assert json.loads(r2["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "bypass"
    assert sent[0]["query"] == sent[1]["query"] == "NVDA Blackwell"
    assert sent[0]["limit"] == sent[1]["limit"] == 3
    p0 = dict(sent[0]["params"])
    p1 = dict(sent[1]["params"])
    assert p1.pop("cursor") == "cur-2"
    assert p0 == p1 == {"type_ids": ["A", "B"], "sort": "relevance"}


def test_r4_5_slice_updates_both_structured_content_and_content_text_json() -> None:
    items = [
        {"doc_id": f"doc_{i}", "title": f"Item {i}", "publish_time": "2026-11-15"}
        for i in range(10)
    ]
    inner_body = {
        "ok": True,
        "results": items,
        "returned": 10,
        "has_more": False,
        "cursor": "cur-10",
    }
    dual_payload = {
        "ok": True,
        "data": {
            "structuredContent": json.loads(json.dumps(inner_body)),
            "content": [{"type": "text", "text": json.dumps(inner_body, ensure_ascii=False)}],
        },
    }
    sliced = ae_cache.slice_cached_payload(dual_payload, op=ae_cache.OP_SEARCH_LIBRARY, target_limit=3)
    sc = sliced["data"]["structuredContent"]
    tb = json.loads(sliced["data"]["content"][0]["text"])
    for rep in (sc, tb):
        assert len(rep["results"]) == 3
        assert rep["returned"] == 3
        assert rep["has_more"] is True
        assert rep["cursor"] is None


@pytest.mark.asyncio
async def test_r4_6_cancelled_shared_waiter_writes_ledger_while_shared_call_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        entered.set()
        await release.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc))

    args = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "CANCEL LEDGER",
            "limit": 3,
        },
    }
    leader_task = asyncio.create_task(
        bridge.handle_tools_call("c-leader", args, caller_identity={"user": "alice", "workspace": "w1", "thread": "t1"})
    )
    waiter_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        waiter_task = asyncio.create_task(
            bridge.handle_tools_call("c-waiter", args, caller_identity={"user": "bob", "workspace": "w2", "thread": "t2"})
        )
        await asyncio.sleep(0.02)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter_task, timeout=5.0)
    finally:
        release.set()

    leader_resp = await asyncio.wait_for(leader_task, timeout=5.0)
    assert json.loads(leader_resp["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "miss"

    entries = [json.loads(line) for line in bridge.cfg.alphaengine_usage_file.read_text("utf-8").splitlines() if line.strip()]
    assert len(entries) == 2
    by_user = {e["user"]: e for e in entries}
    assert by_user["bob"]["ok"] is False
    assert by_user["bob"]["error_category"] == "canceled"
    assert by_user["bob"]["cache"] == "coalesced"
    assert by_user["bob"]["upstream"] is False
    assert by_user["alice"]["ok"] is True
    assert by_user["alice"]["cache"] == "miss"
    assert by_user["alice"]["upstream"] is True


def test_r4_7_capability_probe_cached_per_instance_and_reprobes_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcr, "_supervisor_unavailable", False)
    monkeypatch.setattr(mcr, "_supervisor_caller_cap_cache", {})
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(mcr, "_workspace_view", create_autospec(mcr._workspace_view, return_value={}))
    monkeypatch.setattr(mcr, "_SERVER_CONFIGS", {})
    monkeypatch.delenv("MCP_SUPERVISOR", raising=False)
    monkeypatch.setenv("DALTON_CALLER_USER", "alice")

    sock_path = Path(mcr._supervisor_socket_path())
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.write_text("sock-v1", encoding="utf-8")

    monkeypatch.setattr(mcr, "_socket_alive", create_autospec(mcr._socket_alive, return_value=True))
    monkeypatch.setattr(mcr.time, "sleep", create_autospec(mcr.time.sleep, return_value=None))

    health_probes = 0
    call_ids: list[int] = []
    transient_health_fail = {"fail": False}
    retry_first_call = {"pending": True}

    def _exchange(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        nonlocal health_probes
        if req.get("op") == "health":
            health_probes += 1
            if transient_health_fail["fail"]:
                raise mcr._SupervisorBroke(OSError("transient"), acked=False)
            return "result", {"type": "result", "data": {"ok": True, "pid": 1001, "caller_identity": True}}
        call_ids.append(int(req["id"]))
        if retry_first_call["pending"]:
            retry_first_call["pending"] = False
            return "error", {"type": "error", "code": "queue_timeout", "message": "busy"}
        return "reply", {"type": "reply", "reply": {"ok": True, "caller": req.get("caller")}}

    monkeypatch.setattr(
        mcr, "_supervisor_exchange", create_autospec(mcr._supervisor_exchange, side_effect=_exchange)
    )

    # 1. Call 1 (with 1 pre-ack retry) + Call 2 against same daemon instance -> only 1 health probe total,
    #    and call IDs remain strictly consecutive (+1) without being consumed by health probes.
    r1 = mcr._supervisor_reply("alpha", "one", {})
    r2 = mcr._supervisor_reply("alpha", "two", {})
    assert r1 == {"ok": True, "caller": {"user": "alice"}}
    assert r2 == {"ok": True, "caller": {"user": "alice"}}
    assert health_probes == 1
    assert call_ids[1] == call_ids[0] + 1
    assert call_ids[2] == call_ids[1] + 1

    # 2. Daemon restarts/replaces socket (new inode + mtime via atomic replace): re-probes ONCE,
    #    and if that probe fails transiently, does NOT cache the failure (next call re-probes).
    tmp_sock = sock_path.with_suffix(".new")
    tmp_sock.write_text("sock-v2", encoding="utf-8")
    os.replace(tmp_sock, sock_path)
    transient_health_fail["fail"] = True
    r3 = mcr._supervisor_reply("alpha", "three", {})
    assert r3 == {"ok": True, "caller": None}
    assert health_probes == 2

    transient_health_fail["fail"] = False
    r4 = mcr._supervisor_reply("alpha", "four", {})
    assert r4 == {"ok": True, "caller": {"user": "alice"}}
    assert health_probes == 3
    r5 = mcr._supervisor_reply("alpha", "five", {})
    assert r5 == {"ok": True, "caller": {"user": "alice"}}
    assert health_probes == 3






def test_r4c_own_reservations_any_nonempty_value_is_personal() -> None:
    """Upstream reads own_reservations_only with Boolean(), so even "false" is personal."""
    from scripts.dalton.alphaengine_search_cache import _is_personal_scope_request

    for val in (True, "true", "false", "0", 1, "yes"):
        assert _is_personal_scope_request({"own_reservations_only": val}), val
    for val in (None, False, "", 0):
        assert not _is_personal_scope_request({"own_reservations_only": val}), val


def test_r4c_array_cursor_is_forwarded_unchanged() -> None:
    """An array cursor must reach upstream as an array, not its Python repr."""
    from scripts.dalton.alphaengine_search_cache import OP_SEARCH_LIBRARY, prepare_alphaengine_search_request

    cursor = ["2026-11-01", "doc-1"]
    plan = prepare_alphaengine_search_request(
        {"query": "NVDA", "params": {"cursor": cursor, "limit": 10}},
        op=OP_SEARCH_LIBRARY,
    )
    assert plan.cacheable is False
    assert plan.upstream_arguments["params"]["cursor"] == cursor


# ---------------------------------------------------------------------------
# Phase 2 tests: circuit breaker, upstream_unavailable stale, v4 + v3 migration, report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_circuit_breaker_stale_fallback_fast_fail_backoff_reset_read_and_healthz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [10_000.0]
    mono_box = [1_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"
    breaker = ae_cache.AlphaEngineSearchCircuitBreaker(now_fn=lambda: mono_box[0])
    bridge._ae_search_breaker = breaker

    upstream_calls: list[dict[str, Any]] = []

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        upstream_calls.append(dict(args))
        op = args.get("operation")
        if op == "get_document":
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "data": {
                            "structuredContent": {
                                "metadata": {"doc_id": args["params"]["doc_id"]},
                                "content_field": "auto",
                                "returned_chars": 10,
                                "text": "doc body",
                                "complete": True,
                            }
                        },
                    },
                )
            ]
        return [_mcp_reply(req["id"], _ae_results_payload(2, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 1. Populate cache for query "NVDA"
    r_seed = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-seed",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA"}},
        ),
        timeout=5.0,
    )
    assert json.loads(r_seed["result"]["content"][0]["text"])["ok"] is True
    assert len(upstream_calls) == 1

    # Advance wall clock past 2h TTL (still within 7d retention)
    wall_box[0] += 3 * 3600

    # 2. Upstream returns quota/rate-limit error on uncached query "AAPL" -> trips breaker (60s)
    async def _quota_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        upstream_calls.append(dict(args))
        if args.get("operation") == "get_document":
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "data": {
                            "structuredContent": {
                                "metadata": {"doc_id": args["params"]["doc_id"]},
                                "content_field": "auto",
                                "returned_chars": 8,
                                "text": "ok read!",
                                "complete": True,
                            }
                        },
                    },
                )
            ]
        return [
            _mcp_reply(
                req["id"],
                {"ok": False, "status": "error", "error": "E_UPSTREAM_API: 无查看权限或额度已达上限"},
                is_error=True,
            )
        ]

    rpc_double.side_effect = _quota_rpc
    r_trip1 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-trip-1",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "AAPL"}},
        ),
        timeout=5.0,
    )
    b_trip1 = json.loads(r_trip1["result"]["content"][0]["text"])
    assert b_trip1["ok"] is False
    assert b_trip1["error_category"] == "权限或额度"
    assert breaker.is_open() is True
    assert breaker.remaining_seconds() == pytest.approx(60.0)
    assert breaker.snapshot()["trip_count"] == 1
    assert len(upstream_calls) == 2

    # 3. During open breaker:
    #    (a) "NVDA" (expired cache exists) -> returns stale cache WITHOUT calling upstream!
    r_stale = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-stale",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA"}},
        ),
        timeout=5.0,
    )
    b_stale = json.loads(r_stale["result"]["content"][0]["text"])
    assert b_stale["ok"] is True
    assert b_stale["stale"] is True
    assert b_stale["stale_reason"] == "circuit_open"
    assert b_stale["cached_at"] == ae_cache.iso_utc(10_000.0)
    assert b_stale["_bridge_cache"]["status"] == "stale"
    assert len(upstream_calls) == 2

    #    (b) "MSFT" (no cache) -> fast-fails immediately WITHOUT calling upstream!
    mono_box[0] += 15.0  # 45s remaining
    r_fast = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-fast",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "MSFT"}},
        ),
        timeout=5.0,
    )
    b_fast = json.loads(r_fast["result"]["content"][0]["text"])
    assert b_fast["ok"] is False
    assert b_fast["error_category"] == "权限或额度"
    assert "AlphaEngine 检索暂停约 45 秒（上次错误：权限或额度：" in b_fast["error"]
    assert "无查看权限或额度已达上限" in b_fast["error"]
    assert len(upstream_calls) == 2

    #    (c) get_document (read) is UNAFFECTED by search breaker and calls upstream normally!
    r_read = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-read",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "get_document", "params": {"doc_id": "99001122"}}},
        ),
        timeout=5.0,
    )
    b_read = json.loads(r_read["result"]["content"][0]["text"])
    assert b_read["ok"] is True
    assert len(upstream_calls) == 3

    #    (d) /healthz outputs remaining_seconds and trip_count
    ens_init = create_autospec(bridge.ensure_initialized, return_value={"protocolVersion": "2024-11-05"})
    monkeypatch.setattr(bridge, "ensure_initialized", ens_init)
    monkeypatch.setattr(bridge, "probe_xreach_version", create_autospec(bridge.probe_xreach_version, return_value={"ok": True}))
    monkeypatch.setattr(bridge, "probe_tmeet_version", create_autospec(bridge.probe_tmeet_version, return_value={"ok": True}))
    bridge.server_version = "1.5.0"

    raw_healthz = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:8960\r\n\r\n"
    reader = asyncio.StreamReader()
    reader.feed_data(raw_healthz)
    reader.feed_eof()
    written_chunks: list[bytes] = []
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    writer.write.side_effect = lambda data: written_chunks.append(data)

    async def _noop_drain() -> None:
        return None

    writer.drain.side_effect = _noop_drain
    await asyncio.wait_for(rb._handle_client(bridge, reader, writer), timeout=5.0)
    http_body = b"".join(written_chunks).split(b"\r\n\r\n", 1)[1]
    health_json = json.loads(http_body.decode("utf-8"))
    cb_snap = health_json["alphaengine_search_circuit_breaker"]
    assert cb_snap["open"] is True
    assert cb_snap["remaining_seconds"] == pytest.approx(45.0)
    assert cb_snap["trip_count"] == 1

    # 4. Consecutive trips double duration (60 -> 120 -> 240 -> 480 -> 600 cap)
    expected_durations = [120.0, 240.0, 480.0, 600.0, 600.0]
    for idx, exp_dur in enumerate(expected_durations, start=2):
        mono_box[0] += breaker.remaining_seconds() + 1.0
        assert breaker.is_open() is False
        await asyncio.wait_for(
            bridge.handle_tools_call(
                f"cb-trip-{idx}",
                {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": f"Q{idx}"}},
            ),
            timeout=5.0,
        )
        assert breaker.remaining_seconds() == pytest.approx(exp_dur)
        assert breaker.snapshot()["trip_count"] == idx

    # 5. One upstream search success resets consecutive backoff back to 60s
    mono_box[0] += breaker.remaining_seconds() + 1.0
    rpc_double.side_effect = _fake_rpc
    r_ok = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-recover",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "RECOVER"}},
        ),
        timeout=5.0,
    )
    assert json.loads(r_ok["result"]["content"][0]["text"])["ok"] is True
    assert breaker.is_open() is False
    assert breaker.snapshot()["consecutive_trips"] == 0

    # Next quota failure starts at 60s again
    rpc_double.side_effect = _quota_rpc
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "cb-re-trip",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "RETRIP"}},
        ),
        timeout=5.0,
    )
    assert breaker.remaining_seconds() == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_p2_stale_fallback_on_upstream_unavailable_and_personal_scope_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [20_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"

    calls = 0

    async def _ok_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(3, has_more=False))]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_ok_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # Seed normal query + personal meeting query
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "s-norm",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "TSM"}},
        ),
        timeout=5.0,
    )
    r_pers_1 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "s-pers-1",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "My 1on1",
                    "params": {"meeting_scope": "personal"},
                },
            },
        ),
        timeout=5.0,
    )
    assert json.loads(r_pers_1["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "bypass"
    assert calls == 2

    # Advance past 2h TTL (within 7d retention); upstream fails with 上游不可用
    wall_box[0] += 4 * 3600

    async def _unavail_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return [
            _mcp_reply(
                requests[0]["id"],
                {"ok": False, "status": "error", "error": "E_UPSTREAM_RESPONSE: upstream HTTP 503"},
                is_error=True,
            )
        ]

    rpc_double.side_effect = _unavail_rpc
    r_stale = await asyncio.wait_for(
        bridge.handle_tools_call(
            "s-stale-unavail",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "TSM"}},
        ),
        timeout=5.0,
    )
    b_stale = json.loads(r_stale["result"]["content"][0]["text"])
    assert b_stale["ok"] is True
    assert b_stale["stale"] is True
    assert b_stale["stale_reason"] == "upstream_unavailable"
    assert b_stale["cached_at"] == ae_cache.iso_utc(20_000.0)
    assert b_stale["_bridge_cache"]["status"] == "stale"
    assert len(b_stale["data"]["structuredContent"]["results"]) == 3
    # 上游不可用 does NOT open the quota circuit breaker
    assert bridge._get_ae_search_breaker().is_open() is False

    # Personal meeting never cached -> fails upstream (never returns stale!)
    r_pers_2 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "s-pers-2",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "My 1on1",
                    "params": {"meeting_scope": "personal"},
                },
            },
        ),
        timeout=5.0,
    )
    b_pers_2 = json.loads(r_pers_2["result"]["content"][0]["text"])
    assert b_pers_2["ok"] is False
    assert b_pers_2["_bridge_cache"]["status"] == "bypass"


@pytest.mark.asyncio
async def test_p2_v4_key_normalization_and_v3_dual_read_write_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Case, fullwidth, whitespace, keyword list order/dups, and pure-date normalization
    p_a = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "  ＮＶＤＡ\tＢｌａｃｋｗｅｌｌ   CapEx  ",
            "start": "2026-11-01",
            "end": "2026-11-17",
            "limit": 10,
            "params": {
                "keywords": ["CoWoS", "  ＡＩ ", "cowos"],
                "company_names": ["Beta", "Alpha", "Beta"],
                "type_ids": [2, 1, 2],
            },
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_b = ae_cache.prepare_alphaengine_search_request(
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "nvda blackwell capex",
            "start": "2026/11/01",
            "end": "20261117",
            "limit": 10,
            "params": {
                "keywords": ["ai", "CoWoS"],
                "company_names": ["Alpha", "Beta"],
                "type_ids": [1, 2],
            },
        },
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_a.cache_key == p_b.cache_key
    assert p_a.base_key == p_b.base_key
    assert p_a.date_from == "2026-11-01"
    assert p_a.date_to == "2026-11-17"

    # 2. v3 fallback read + automatic v4 write-back preserving original timestamps
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [50_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"
    await cache.initialize()

    v3_args = {
        "route": "alphaengine",
        "operation": "search_library",
        "query": "TSMC CoWoS",
        "start": "2026-10-01",
        "end": "2026-11-10",
        "limit": 5,
    }
    plan = ae_cache.prepare_alphaengine_search_request(v3_args, op=ae_cache.OP_SEARCH_LIBRARY)
    assert plan.cache_key != plan.legacy_cache_key

    # Seed ONLY the v3 entry in SQLite
    put_res = await cache.put(
        cache_key=plan.legacy_cache_key,
        base_key=plan.legacy_base_key,
        op=plan.op,
        user_scope="team",
        req_limit=plan.req_limit,
        result_count=2,
        has_more=False,
        payload=_ae_results_payload(2, has_more=False, prefix="v3_"),
        date_to=plan.date_to,
    )
    assert put_res is not None
    orig_fetched_at, orig_expires_at = put_res

    upstream_mock = create_autospec(bridge._run_stdio_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", upstream_mock)

    # Query with original v3 query ("TSMC CoWoS") under v4:
    # v4 key misses, falls back to v3 key, hits without calling upstream, and writes v4 copy;
    # subsequent lowercase query ("tsmc cowos") then hits the migrated v4 entry directly!
    wall_box[0] += 100.0
    r_hit = await asyncio.wait_for(
        bridge.handle_tools_call(
            "v3-mig",
            {
                "name": "research",
                "arguments": dict(v3_args),
            },
        ),
        timeout=5.0,
    )
    assert upstream_mock.call_count == 0
    b_hit = json.loads(r_hit["result"]["content"][0]["text"])
    assert b_hit["ok"] is True
    assert b_hit["_bridge_cache"]["status"] == "hit"
    assert b_hit["data"]["structuredContent"]["results"][0]["doc_id"].startswith("v3_")

    # Verify a v4 row was written into SQLite with the original fetched_at / expires_at
    import sqlite3

    with sqlite3.connect(str(cache.db_path)) as conn:
        v4_row = conn.execute(
            "SELECT fetched_at, expires_at FROM ae_search_cache WHERE cache_key = ?",
            (plan.cache_key,),
        ).fetchone()
    assert v4_row is not None
    assert v4_row[0] == pytest.approx(orig_fetched_at)
    assert v4_row[1] == pytest.approx(orig_expires_at)

    # Now lowercase query hits the migrated v4 entry directly without touching v3 or upstream
    r_hit_lower = await asyncio.wait_for(
        bridge.handle_tools_call(
            "v4-after-mig",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "tsmc cowos",
                    "start": "2026-10-01",
                    "end": "2026-11-10",
                    "limit": 5,
                },
            },
        ),
        timeout=5.0,
    )
    assert upstream_mock.call_count == 0
    assert json.loads(r_hit_lower["result"]["content"][0]["text"])["_bridge_cache"]["status"] == "hit"


def test_p2_usage_report_failures_by_day_caller_ranking_hit_rate_and_breaker_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        # 2026-11-17 (BJT): ws-heavy has 2 fails (1 quota + breaker trip, 1 upstream), 1 miss ok
        {
            "ts": "2026-11-17T06:00:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": False,
            "error_category": "权限或额度",
            "cache": "miss",
            "upstream": True,
            "workspace": "ws-heavy",
            "user": "u1",
            "breaker_tripped": True,
        },
        {
            "ts": "2026-11-17T06:01:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": False,
            "error_category": "上游不可用",
            "cache": "miss",
            "upstream": True,
            "workspace": "ws-heavy",
            "user": "u1",
        },
        {
            "ts": "2026-11-17T06:02:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "error_category": "",
            "cache": "hit",
            "upstream": False,
            "workspace": "ws-heavy",
            "user": "u1",
        },
        # ws-light has 1 coalesced ok + 1 bypass (excluded from cacheable hit rate) + 1 personal_scope
        {
            "ts": "2026-11-17T07:00:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "error_category": "",
            "cache": "coalesced",
            "upstream": False,
            "workspace": "",
            "user": "u2",
        },
        {
            "ts": "2026-11-17T07:05:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "error_category": "",
            "cache": "bypass",
            "upstream": True,
            "workspace": "",
            "user": "u2",
        },
        {
            "ts": "2026-11-17T07:06:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "error_category": "",
            "cache": "stale",
            "upstream": False,
            "workspace": "",
            "user": "u2",
            "personal_scope": True,
        },
    ]
    summary = usage_report.summarize(entries)
    assert summary["breaker_trips"] == 1
    assert summary["failures_by_day"]["2026-11-17"] == {"权限或额度": 1, "上游不可用": 1}
    # Cacheable searches: ws-heavy (miss, miss, hit) + u2 (coalesced) = 4; hits = hit + coalesced = 2 -> 0.5
    assert summary["cacheable_search_stats"]["total"] == 4
    assert summary["cacheable_search_stats"]["hits"] == 2
    assert summary["cacheable_search_hit_rate"] == pytest.approx(0.5)

    ranking = summary["caller_search_ranking"]
    assert ranking[0]["caller"] == "ws-heavy"
    assert ranking[0]["searches"] == 3
    assert ranking[0]["failures"] == 2
    assert ranking[0]["fail_rate"] == pytest.approx(round(2 / 3, 4))
    assert ranking[1]["caller"] == "u2"
    assert ranking[1]["searches"] == 3
    assert ranking[1]["failures"] == 0

    usage_report._print_report(summary)
    out = capsys.readouterr().out
    assert "可缓存检索命中率 50.0%" in out
    assert "熔断触发 1 次" in out
    assert "失败分布：上游不可用=1，权限或额度=1" in out
    assert "ws-heavy [workspace]: 检索=3 失败=2 失败率=66.7%" in out


# ---------------------------------------------------------------------------
# Phase 2 Round 2 tests: soft advice, first-open vs repeat read, query logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_r2_company_search_soft_advice_and_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mono_box = [1000.0]
    bridge._ae_company_tracker = ae_cache.AlphaEngineCompanySearchTracker(
        now_fn=lambda: mono_box[0]
    )

    upstream_calls = 0

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_calls
        upstream_calls += 1
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    )

    caller = {"user": "u1", "workspace": "ws1", "thread": "th-adv"}

    # 1 & 2 for NVDA -> no advice yet
    r1 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-1",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert "advice" not in json.loads(r1["result"]["content"][0]["text"])

    r2 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-2",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "英伟达 算力"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert "advice" not in json.loads(r2["result"]["content"][0]["text"])

    # Exact repeat of #2 (same keyword) -> hits cache, no advice because keyword is NOT different
    r2_repeat = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-2-rep",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "英伟达 算力"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert "advice" not in json.loads(r2_repeat["result"]["content"][0]["text"])

    # 3rd distinct keyword for NVDA within 30m -> executes normally AND returns advice!
    r3 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-3",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "Nvidia Blackwell"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    b3 = json.loads(r3["result"]["content"][0]["text"])
    assert b3["ok"] is True
    assert len(b3["data"]["structuredContent"]["results"]) == 2
    assert "请先从现有结果中挑选（本会话已检索这家公司 3 次）；需要新主题时再检索" == b3["advice"]

    # Different company (TSM / 台积电) -> independent counter, no advice!
    r_tsm = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-tsm",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "台积电 CoWoS"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert "advice" not in json.loads(r_tsm["result"]["content"][0]["text"])

    # Unrecognized topic queries (e.g., AI Agent / 光模块) -> never counted, never trigger advice!
    for idx, topic_q in enumerate(("AI Agent 商业化", "光模块 800G 需求", "算力网络 CAPEX", "液冷机柜方案")):
        r_topic = await asyncio.wait_for(
            bridge.handle_tools_call(
                f"topic-{idx}",
                {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": topic_q}},
                caller_identity=caller,
            ),
            timeout=5.0,
        )
        assert "advice" not in json.loads(r_topic["result"]["content"][0]["text"])

    # Workspace fallback when thread is empty, and 30m TTL expiration resets count
    ws_caller = {"user": "u1", "workspace": "ws-only", "thread": ""}
    for idx, q in enumerate(("宁德时代", "300750 电池", "CATL 储能")):
        r_ws = await asyncio.wait_for(
            bridge.handle_tools_call(
                f"ws-{idx}",
                {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": q}},
                caller_identity=ws_caller,
            ),
            timeout=5.0,
        )
        body_ws = json.loads(r_ws["result"]["content"][0]["text"])
        if idx < 2:
            assert "advice" not in body_ws
        else:
            assert "已检索这家公司 3 次" in body_ws["advice"]

    # Advance past 30m TTL -> expired entries pruned, next distinct NVDA search starts fresh (count 1)
    mono_box[0] += 31 * 60
    r_after_ttl = await asyncio.wait_for(
        bridge.handle_tools_call(
            "adv-expired",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "NVDA Rubin"}},
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert "advice" not in json.loads(r_after_ttl["result"]["content"][0]["text"])
    assert upstream_calls >= 10


@pytest.mark.asyncio
async def test_p2_r2_get_document_first_open_vs_repeat_open_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _doc_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        doc_id = args["params"]["doc_id"]
        offset = int(args["params"].get("offset") or 0)
        return [
            _mcp_reply(
                req["id"],
                {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "structuredContent": {
                            "metadata": {"doc_id": doc_id},
                            "content_field": "auto",
                            "offset": offset,
                            "returned_chars": 100,
                            "text": "x" * 100,
                            "complete": offset > 0,
                        }
                    },
                },
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_doc_rpc)
    )

    # 1. First open of doc A (offset=0) -> first_open=True (counts toward quota)
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "doc-a-0",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "get_document", "params": {"doc_id": "doc_A", "offset": 0}}},
        ),
        timeout=5.0,
    )
    # 2. Pagination read of doc A (offset=30000) -> repeat (does NOT count toward quota)
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "doc-a-1",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "get_document", "params": {"doc_id": "doc_A", "offset": 30000}}},
        ),
        timeout=5.0,
    )
    # 3. Another open of doc A -> repeat (does NOT count toward quota)
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "doc-a-2",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "get_document", "params": {"doc_id": "alphaengine:doc_A"}}},
        ),
        timeout=5.0,
    )
    # 4. First open of doc B -> counts toward quota (calls=2)
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "doc-b-0",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "get_document", "params": {"doc_id": "doc_B"}}},
        ),
        timeout=5.0,
    )

    today_stats = bridge._read_usage_today()
    assert today_stats["calls"] == 2
    assert today_stats["first_open_calls"] == 2
    assert today_stats["repeat_calls"] == 2
    assert today_stats["total_calls"] == 4
    assert today_stats["chars"] == 400

    ledger_entries = usage_report.load_entries([bridge.cfg.alphaengine_usage_file])
    assert [e["first_open"] for e in ledger_entries] == [True, False, False, True]
    summary = usage_report.summarize(ledger_entries)
    assert summary["read_first_open"] == 2
    assert summary["read_repeat"] == 2


@pytest.mark.asyncio
async def test_p2_r2_team_query_log_excludes_personal_rotates_7d_0600_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    q_dir = tmp_path / "alphaengine_search_queries"
    today_box = ["2026-11-18"]
    bridge._ae_query_logger = ae_cache.AlphaEngineTeamQueryLogger(
        q_dir, today_fn=lambda: today_box[0]
    )

    # Seed an 8-day-old file and a 5-day-old file to verify automatic 7-day cleanup
    q_dir.mkdir(parents=True, exist_ok=True)
    old_file = q_dir / "2026-11-10.jsonl"
    recent_file = q_dir / "2026-11-13.jsonl"
    old_file.write_text('{"query": "ancient"}\n', encoding="utf-8")
    recent_file.write_text('{"query": "recent keep"}\n', encoding="utf-8")

    async def _fake_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(2, has_more=False))]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    )

    # 1. Personal-scope queries MUST NOT be written to query log
    for idx, p_params in enumerate((
        {"meeting_scope": "personal"},
        {"own_reservations_only": True},
        {"document_categories": ["personal_meetings"]},
    )):
        await asyncio.wait_for(
            bridge.handle_tools_call(
                f"pers-q-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"SECRET_PERSONAL_QUERY_{idx}",
                        "params": p_params,
                    },
                },
                caller_identity={"user": "alice", "workspace": "ws1", "thread": "th-p"},
            ),
            timeout=5.0,
        )

    # 2. Team-scope queries ARE logged with 0600 permissions, and 8-day-old file is cleaned up
    caller_1 = {"user": "alice", "workspace": "ws1", "thread": "th-team-1"}
    for idx, q in enumerate(("NVDA Blackwell", "英伟达 CoWoS", "NVDA Blackwell")):
        await asyncio.wait_for(
            bridge.handle_tools_call(
                f"team-q-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": q,
                        "params": {"fresh": True} if idx == 2 else {},
                    },
                },
                caller_identity=caller_1,
            ),
            timeout=5.0,
        )

    today_file = q_dir / "2026-11-18.jsonl"
    assert today_file.is_file()
    assert stat_mod.S_IMODE(today_file.stat().st_mode) == 0o600
    assert stat_mod.S_IMODE(q_dir.stat().st_mode) == 0o700
    assert not old_file.exists()
    assert recent_file.exists()

    written_text = today_file.read_text(encoding="utf-8")
    assert "SECRET_PERSONAL_QUERY" not in written_text
    assert "NVDA Blackwell" in written_text
    assert "英伟达 CoWoS" in written_text

    # 3. Usage report summarizes multi-search by session+company, repeated queries, and quota breakdown
    ledger_entries = usage_report.load_entries([bridge.cfg.alphaengine_usage_file])
    q_entries = usage_report.load_query_entries(q_dir)
    summary = usage_report.summarize(ledger_entries, query_entries=q_entries)

    assert summary["search_upstream_calls"] == 6
    assert summary["multi_search_by_session_company"]
    top_mc = summary["multi_search_by_session_company"][0]
    assert top_mc["session"] == "th-team-1"
    assert top_mc["company"] == "NVDA"
    assert top_mc["count"] == 3
    assert "NVDA Blackwell" in top_mc["queries"]
    assert "英伟达 CoWoS" in top_mc["queries"]
    assert summary["top_repeated_queries"][0] == {"query": "NVDA Blackwell", "count": 2}

    usage_report._print_report(summary)
    out = capsys.readouterr().out
    assert "同一公司的多次检索排行（按会话和公司，前 15）：" in out
    assert "th-team-1 / NVDA: 3 次" in out
    assert "重复最多的查询文本（>1 次，前 15）：" in out
    assert "2 次: NVDA Blackwell" in out


@pytest.mark.asyncio
async def test_p2_r2_single_company_search_rule_in_docs_plugin_and_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    ds_json = json.loads(
        (repo_root / "scripts/dalton/research_runtime/config/data_sources.json").read_text(
            encoding="utf-8"
        )
    )
    ae_entry = next(r for r in ds_json["routes"] if r["id"] == "alphaengine")
    assert rb.ALPHAENGINE_SINGLE_COMPANY_SEARCH_RULE in ae_entry["rules"]

    plugin_json = json.loads((repo_root / "plugins/dalton_data/plugin.json").read_text(encoding="utf-8"))
    srv_inst = plugin_json["extensions"]["ai.langalpha"]["servers"]["researchdata"]["instruction"]
    assert srv_inst == rb.build_researchdata_plugin_instruction()
    assert rb.ALPHAENGINE_SINGLE_COMPANY_SEARCH_RULE in srv_inst

    # Verify catalog() response carries the rule on alphaengine route too
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _cat_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_reply(
                requests[0]["id"],
                {"ok": True, "routes": [{"id": "alphaengine", "rules": []}]},
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_cat_rpc)
    )
    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "cat-ae", {"name": "catalog", "arguments": {"route": "alphaengine"}}
        ),
        timeout=5.0,
    )
    cat_body = json.loads(resp["result"]["content"][0]["text"])
    assert rb.ALPHAENGINE_SINGLE_COMPANY_SEARCH_RULE in cat_body["routes"][0]["rules"]


# ---------------------------------------------------------------------------
# Phase 2 Round 3 tests: review fixes (breaker signals, post-slot check,
# caller-deadline stale fallback, pipe rule, pages_scanned, strict company,
# date precision, per-route forwarded query logs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_r3_breaker_trigger_signals_reason_and_uncacheable_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mono_box = [1000.0]
    breaker = ae_cache.AlphaEngineSearchCircuitBreaker(now_fn=lambda: mono_box[0])
    bridge._ae_search_breaker = breaker

    responses: list[dict[str, Any]] = []
    upstream_calls: list[dict[str, Any]] = []

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        upstream_calls.append(dict(req["params"]["arguments"]))
        reply_payload = responses.pop(0)
        return [_mcp_reply(req["id"], reply_payload, is_error=not bool(reply_payload.get("ok")))]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    )

    # 1. Parameter errors (even containing "403") & non-quota auth errors NEVER trip breaker
    non_tripping = [
        {"ok": False, "status": "error", "error": "E_BAD_DATE: invalid id 403"},
        {"ok": False, "status": "auth_expired", "error": "auth_expired: please re-login"},
        {"ok": False, "status": "paid_source_not_allowed", "error": "paid source restricted"},
        {"ok": False, "status": "forbidden", "error": "HTTP 403 Forbidden"},
        {"ok": False, "status": "unauthorized", "error": "HTTP 401 Unauthorized"},
    ]
    for idx, err_payload in enumerate(non_tripping):
        responses.append(err_payload)
        resp = await asyncio.wait_for(
            bridge.handle_tools_call(
                f"nontrip-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"NT{idx}",
                    },
                },
            ),
            timeout=5.0,
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert breaker.is_open() is False
        if "E_BAD_DATE" in err_payload["error"]:
            assert body["error_category"] == "参数错误"

    # 2. Uncacheable requests (`mode=plan`, `cursor`, `meeting_scope=personal`) NEVER trip breaker on 429
    for idx, extra_args in enumerate(
        (
            {"mode": "plan"},
            {"params": {"cursor": "ae1:tok123"}},
            {"params": {"meeting_scope": "personal"}},
        )
    ):
        responses.append({"ok": False, "status": "error", "error": "HTTP 429 Too Many Requests"})
        await asyncio.wait_for(
            bridge.handle_tools_call(
                f"uncacheable-429-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"UC{idx}",
                        **extra_args,
                    },
                },
            ),
            timeout=5.0,
        )
        assert breaker.is_open() is False

    # 3. Quota / rate-limit signals trip breaker AND fast-fail carries error category + excerpt
    tripping_errors = [
        {"ok": False, "status": "error", "error": "E_UPSTREAM_API: 无查看权限或额度已达上限"},
        {"ok": False, "status": "error", "error": "HTTP 429 Too Many Requests"},
        {"ok": False, "status": "quota_exceeded", "error": "quota_exceeded on backend"},
        {"ok": False, "status": "rate_limited", "error": "rate_limited"},
        {"ok": False, "status": "error", "error": "触发频率限制，请稍后再试"},
    ]
    for idx, err_payload in enumerate(tripping_errors):
        responses.append(err_payload)
        await asyncio.wait_for(
            bridge.handle_tools_call(
                f"trip-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"TR{idx}",
                    },
                },
            ),
            timeout=5.0,
        )
        assert breaker.is_open() is True

        # Fast-fail on uncached cacheable query carries category + original error excerpt
        r_ff = await asyncio.wait_for(
            bridge.handle_tools_call(
                f"ff-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"FF{idx}",
                    },
                },
            ),
            timeout=5.0,
        )
        b_ff = json.loads(r_ff["result"]["content"][0]["text"])
        assert b_ff["ok"] is False
        assert "AlphaEngine 检索暂停约" in b_ff["error"]
        assert "上次错误：权限或额度：" in b_ff["error"]

        # Uncacheable requests STILL reach upstream normally even while breaker is open!
        before_cnt = len(upstream_calls)
        responses.append(_ae_results_payload(1, has_more=False))
        r_pers = await asyncio.wait_for(
            bridge.handle_tools_call(
                f"pers-open-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": f"PERS{idx}",
                        "params": {"meeting_scope": "personal"},
                    },
                },
            ),
            timeout=5.0,
        )
        assert json.loads(r_pers["result"]["content"][0]["text"])["ok"] is True
        assert len(upstream_calls) == before_cnt + 1
        assert breaker.is_open() is True  # personal success did not reset breaker either

        # Expire current breaker window cleanly before testing next signal
        mono_box[0] += breaker.remaining_seconds() + 1.0
        breaker.record_success()


@pytest.mark.asyncio
async def test_p2_r3_breaker_rechecked_after_slot_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, max_inflight=1)
    mono_box = [2000.0]
    breaker = ae_cache.AlphaEngineSearchCircuitBreaker(now_fn=lambda: mono_box[0])
    bridge._ae_search_breaker = breaker

    rpc_calls: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _slot_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        q = requests[0]["params"]["arguments"]["query"]
        rpc_calls.append(q)
        if q == "Q1":
            first_entered.set()
            await asyncio.wait_for(release_first.wait(), timeout=2.0)
            return [
                _mcp_reply(
                    requests[0]["id"],
                    {"ok": False, "status": "error", "error": "E_UPSTREAM_API: 无查看权限或额度已达上限"},
                    is_error=True,
                )
            ]
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(1, has_more=False))]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slot_rpc)
    )

    # Start Q1 (holds the single concurrency slot)
    t1 = asyncio.create_task(
        bridge.handle_tools_call(
            "slot-1",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "Q1"}},
        )
    )
    await asyncio.wait_for(first_entered.wait(), timeout=2.0)

    # Start Q2 while breaker is STILL closed -> passes outer check and waits on slot!
    t2 = asyncio.create_task(
        bridge.handle_tools_call(
            "slot-2",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "Q2"}},
        )
    )
    await asyncio.sleep(0.05)

    # Release Q1 -> fails with quota limit, trips breaker, releases slot
    release_first.set()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=3.0)
    b1 = json.loads(r1["result"]["content"][0]["text"])
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b1["ok"] is False
    assert b2["ok"] is False
    assert "AlphaEngine 检索暂停约" in b2["error"]
    # Post-slot check stopped Q2 from hitting upstream!
    assert rpc_calls == ["Q1"]

    # Confirm concurrency slot was cleanly released (uncacheable call acquires slot immediately)
    r3 = await asyncio.wait_for(
        bridge.handle_tools_call(
            "slot-3",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "Q3",
                    "params": {"meeting_scope": "personal"},
                },
            },
        ),
        timeout=2.0,
    )
    assert json.loads(r3["result"]["content"][0]["text"])["ok"] is True
    assert rpc_calls == ["Q1", "Q3"]


@pytest.mark.asyncio
async def test_p2_r3_caller_deadline_returns_stale_while_upstream_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [30_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"
    await cache.initialize()

    args = {"route": "alphaengine", "operation": "search_library", "query": "SlowCoCapex"}
    plan = ae_cache.prepare_alphaengine_search_request(args, op=ae_cache.OP_SEARCH_LIBRARY)
    await cache.put(
        cache_key=plan.cache_key,
        base_key=plan.base_key,
        op=plan.op,
        user_scope="team",
        req_limit=plan.req_limit,
        result_count=1,
        has_more=False,
        payload={"ok": True, "status": "ok", "results": [{"doc_id": "stale-doc-1"}]},
        date_to=plan.date_to,
    )
    # Expire TTL (remain inside 7d retention window)
    wall_box[0] += 3 * 3600

    upstream_started = asyncio.Event()
    allow_upstream_finish = asyncio.Event()
    upstream_finished = asyncio.Event()

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        upstream_started.set()
        await asyncio.wait_for(allow_upstream_finish.wait(), timeout=3.0)
        upstream_finished.set()
        return [
            _mcp_reply(
                requests[0]["id"],
                {"ok": True, "status": "ok", "results": [{"doc_id": "fresh-doc-99"}]},
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc)
    )

    loop = asyncio.get_running_loop()
    t_start = loop.time()
    early_res = await asyncio.wait_for(
        bridge._execute_alphaengine_cached_search(
            req_id="dl-1",
            op=ae_cache.OP_SEARCH_LIBRARY,
            arguments=args,
            caller_identity=None,
            caller_timeout_s=0.32,
            shared_timeout_s=5.0,
            fanout=False,
        ),
        timeout=1.5,
    )
    elapsed = loop.time() - t_start
    assert upstream_started.is_set()
    assert elapsed < 0.45
    assert early_res["ok"] is True
    assert early_res["stale"] is True
    assert early_res["stale_reason"] == "upstream_timeout"
    assert early_res["cached_at"] == ae_cache.iso_utc(30_000.0)
    assert early_res["payload"]["results"][0]["doc_id"] == "stale-doc-1"

    # Background shared upstream task is still alive and finishes writing fresh cache
    runners = list(bridge._get_singleflight_runners().values())
    assert len(runners) == 1
    allow_upstream_finish.set()
    await asyncio.wait_for(upstream_finished.wait(), timeout=2.0)
    await asyncio.wait_for(runners[0]["runner_task"], timeout=2.0)

    # Next call immediately hits the fresh cache written by the background upstream task
    hit_res = await asyncio.wait_for(
        bridge._execute_alphaengine_cached_search(
            req_id="dl-2",
            op=ae_cache.OP_SEARCH_LIBRARY,
            arguments=args,
            caller_identity=None,
            caller_timeout_s=5.0,
            shared_timeout_s=5.0,
            fanout=False,
        ),
        timeout=2.0,
    )
    assert hit_res["ok"] is True
    assert hit_res["stale"] is False
    assert hit_res["bridge_cache"]["status"] == "hit"
    assert hit_res["payload"]["results"][0]["doc_id"] == "fresh-doc-99"

    # Without a stale cache entry, deadline still raises _CachedSearchTimeoutError
    allow_upstream_finish.clear()
    with pytest.raises(rb._CachedSearchTimeoutError):
        await asyncio.wait_for(
            bridge._execute_alphaengine_cached_search(
                req_id="dl-miss",
                op=ae_cache.OP_SEARCH_LIBRARY,
                arguments={"route": "alphaengine", "operation": "search_library", "query": "NoStaleKey"},
                caller_identity=None,
                caller_timeout_s=0.2,
                shared_timeout_s=5.0,
                fanout=False,
            ),
            timeout=1.5,
        )
    allow_upstream_finish.set()
    for r in list(bridge._get_singleflight_runners().values()):
        await asyncio.wait_for(r["runner_task"], timeout=2.0)


def test_p2_r3_space_rule_no_keywords_and_description_length() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    assert "用空格隔开写在同一个查询里" in rb.ALPHAENGINE_SINGLE_COMPANY_SEARCH_RULE
    assert "keywords" not in rb.ALPHAENGINE_SINGLE_COMPANY_SEARCH_RULE

    ds_json = json.loads(
        (repo_root / "scripts/dalton/research_runtime/config/data_sources.json").read_text(
            encoding="utf-8"
        )
    )
    ae_entry = next(r for r in ds_json["routes"] if r["id"] == "alphaengine")
    ae_rules_text = "\n".join(ae_entry["rules"])
    assert "用空格隔开写在同一个查询里" in ae_rules_text
    assert "keywords" not in ae_rules_text

    plugin_json = json.loads((repo_root / "plugins/dalton_data/plugin.json").read_text(encoding="utf-8"))
    srv_inst = plugin_json["extensions"]["ai.langalpha"]["servers"]["researchdata"]["instruction"]
    assert srv_inst == rb.build_researchdata_plugin_instruction()
    # Guidepoint section mentions params.keywords, but AlphaEngine section before Guidepoint does not
    ae_segment = srv_inst.split("Guidepoint 仅返回问答摘录")[0]
    assert "keywords" not in ae_segment


def test_p2_r3_usage_report_pages_scanned_and_definitions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        # Transcript multi-page scan: counts as 5 search quota units
        {
            "ts": "2026-11-18T02:00:00+00:00",
            "op": "search",
            "operation": "transcript",
            "ok": True,
            "cache": "miss",
            "upstream": True,
            "pages_scanned": 5,
            "workspace": "ws1",
        },
        # Regular upstream search without pages_scanned: counts as 1
        {
            "ts": "2026-11-18T02:01:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "cache": "miss",
            "upstream": True,
            "workspace": "ws1",
        },
        # Cached hit: 0 quota units consumed, 1 saved
        {
            "ts": "2026-11-18T02:02:00+00:00",
            "op": "search",
            "operation": "search_library",
            "ok": True,
            "cache": "hit",
            "upstream": False,
            "pages_scanned": 5,
            "workspace": "ws1",
        },
    ]
    summary = usage_report.summarize(entries)
    assert summary["search_calls"] == 3
    assert summary["search_upstream_calls"] == 6
    assert summary["search_cache_saved"] == 1
    assert summary["by_day"]["2026-11-18"]["search.upstream"] == 6

    usage_report._print_report(summary)
    out = capsys.readouterr().out
    assert "检索次数=3" in out
    assert "检索消耗额度=6" in out
    assert "pages_scanned" in out


def test_p2_r3_strict_company_recognition() -> None:
    # Lowercase pure-ASCII ticker must NOT match; ambiguous bare words must NOT match
    assert ae_cache.extract_companies_from_search("AI APP 商业化") == []
    assert ae_cache.extract_companies_from_search("nvda Blackwell capex") == []
    assert ae_cache.extract_companies_from_search("meta-analysis on ad spend") == []
    assert ae_cache.extract_companies_from_search("META NOW UBER SNOW PATH") == []
    assert ae_cache.extract_companies_from_search("ON ALL CAN CARS FAST REAL LOVE") == []
    assert ae_cache.extract_companies_from_search("OPEN PLAY TEAM BOX LIFE ARM FUN") == []

    # Uppercase non-ambiguous tickers and explicit ticker forms MUST match
    assert ae_cache.extract_companies_from_search("NVDA Blackwell capex") == ["NVDA"]
    assert ae_cache.extract_companies_from_search("$APP 财报") == ["APP"]
    assert ae_cache.extract_companies_from_search("APP US 营收") == ["APP"]
    assert ae_cache.extract_companies_from_search("APP.O 指引") == ["APP"]
    assert ae_cache.extract_companies_from_search("AppLovin APP 业绩") == ["APP"]
    assert ae_cache.extract_companies_from_search("Meta Platforms META") == ["META"]
    assert ae_cache.extract_companies_from_search("$META 业绩") == ["META"]
    assert ae_cache.extract_companies_from_search("Uber Technologies 业绩") == ["UBER"]
    assert ae_cache.extract_companies_from_search("$NOW Q3") == ["NOW"]
    assert ae_cache.extract_companies_from_search("APP", ticker="APP") == ["APP"]


def test_p2_r3_date_precision_day_vs_utc_iso() -> None:
    # Pure dates -> day precision
    assert ae_cache.normalize_date_param("2026-11-01") == "2026-11-01"
    assert ae_cache.normalize_date_param("2026/11/1") == "2026-11-01"
    assert ae_cache.normalize_date_param("20261101") == "2026-11-01"

    # Datetimes with time/tz -> canonical UTC ISO
    assert ae_cache.normalize_date_param("2026-11-01T00:00:00Z") == "2026-11-01T00:00:00Z"
    assert ae_cache.normalize_date_param("2026-11-01T08:00:00+08:00") == "2026-11-01T00:00:00Z"
    assert ae_cache.normalize_date_param("2026-11-01 23:59:59") == "2026-11-01T23:59:59Z"

    p_utc = ae_cache.prepare_alphaengine_search_request(
        {"query": "TSMC", "start": "2026-11-01T00:00:00Z", "end": "2026-11-10T00:00:00Z"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_bjt_equiv = ae_cache.prepare_alphaengine_search_request(
        {"query": "TSMC", "start": "2026-11-01T08:00:00+08:00", "end": "2026-11-10T08:00:00+08:00"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_different_time = ae_cache.prepare_alphaengine_search_request(
        {"query": "TSMC", "start": "2026-11-01T23:59:59Z", "end": "2026-11-10T00:00:00Z"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    p_day_only = ae_cache.prepare_alphaengine_search_request(
        {"query": "TSMC", "start": "2026-11-01", "end": "2026-11-10"},
        op=ae_cache.OP_SEARCH_LIBRARY,
    )
    assert p_utc.cache_key == p_bjt_equiv.cache_key
    assert p_utc.cache_key != p_different_time.cache_key
    assert p_utc.cache_key != p_day_only.cache_key


@pytest.mark.asyncio
async def test_p2_r3_query_log_records_actual_forwarded_route_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    q_dir = bridge.cfg.alphaengine_query_log_dir
    bridge._ae_query_logger = ae_cache.AlphaEngineTeamQueryLogger(
        q_dir, today_fn=lambda: "2026-11-18"
    )
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-11-18")

    async def _fanout_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route == "alphaengine":
            return [_mcp_reply(req["id"], _ae_results_payload(1, has_more=False))]
        if route == "guidepoint":
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        return [
            _mcp_reply(
                req["id"],
                {"ok": True, "status": "ok", "results": [], "source_status": {"sales-notes": {"ok": True, "count": 0}}},
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_fanout_rpc)
    )

    caller = {"user": "alice", "workspace": "ws1", "thread": "th-fan"}
    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "fanout-qlog",
            {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "search",
                    "ticker": "NVDA",
                    "query": "HBM",
                    "limit": 80,
                    "params": {"keywords": ["CoWoS", "Blackwell"], "recency_bias": 40},
                },
            },
            caller_identity=caller,
        ),
        timeout=5.0,
    )
    assert json.loads(resp["result"]["content"][0]["text"])["ok"] is True

    log_file = q_dir / "2026-11-18.jsonl"
    assert log_file.is_file()
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["query"] == "NVDA HBM"
    assert entry["keywords"] == []
    assert entry["params"]["limit"] == 50
    assert "recency_bias" not in entry["params"]
    assert entry["companies"] == ["NVDA"]


# ---------------------------------------------------------------------------
# Phase 2 Round 4 tests: transcript partial breaker, no-cancel after upstream,
# bounded get_stale, fresh deadline behavior, upstream_pages_fetched, strict 429/text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_r4_transcript_partial_page_quota_error_trips_breaker_and_skips_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import importlib.util

    gw_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "research_runtime"
        / "scripts"
        / "data_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("test_dg_r4", gw_path)
    assert spec is not None and spec.loader is not None
    dg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dg)

    # 1. data_gateway.fetch_transcripts preserves page_errors when page 2 fails
    page_calls = 0

    def _fake_native(_route: dict[str, Any], _args: Any, _unwrap: bool) -> dict[str, Any]:
        nonlocal page_calls
        page_calls += 1
        if page_calls == 1:
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "results": [
                            {
                                "doc_id": "tr_1",
                                "title": "NVIDIA Q3 2026 Earnings Call",
                                "publish_time": "2026-11-18 10:00:00",
                            }
                        ],
                        "has_more": True,
                        "cursor": "cur-p2",
                    }
                },
            }
        return {
            "ok": False,
            "status": "error",
            "error": "E_UPSTREAM_API: 无查看权限或额度已达上限",
        }

    monkeypatch.setattr(dg, "native_request", _fake_native)
    monkeypatch.setattr(dg, "_ticker_company_name", lambda _t: "NVIDIA")
    reg = {"routes": [{"id": "alphaengine"}]}
    ns = argparse.Namespace(ticker="NVDA", query="", start="2026-01-01", end="2026-11-18", limit=10, timeout=30)
    gw_out = dg.fetch_transcripts(reg, ns, "python3 data_gateway.py")
    assert gw_out["ok"] is True
    assert gw_out["status"] == "partial"
    assert gw_out["page_errors"] == [{"page": 2, "error": "E_UPSTREAM_API: 无查看权限或额度已达上限"}]
    assert gw_out["data"]["coverage"]["page_errors"] == gw_out["page_errors"]

    # 2. Bridge receives this partial response: opens closed breaker, returns partial, does NOT write cache
    bridge = _make_bridge(tmp_path, monkeypatch)
    mono_box = [1000.0]
    breaker = ae_cache.AlphaEngineSearchCircuitBreaker(now_fn=lambda: mono_box[0])
    bridge._ae_search_breaker = breaker

    async def _tr_partial_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [_mcp_reply(requests[0]["id"], gw_out)]

    monkeypatch.setattr(
        bridge,
        "_run_stdio_rpc",
        create_autospec(bridge._run_stdio_rpc, side_effect=_tr_partial_rpc),
    )

    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "tr-partial-1",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "NVDA",
                    "start": "2026-01-01",
                    "end": "2026-11-18",
                },
            },
        ),
        timeout=5.0,
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert body["status"] == "partial"
    assert len(body["data"]["transcripts"]) == 1
    assert breaker.is_open() is True

    # Nothing written to cache for this key
    plan = ae_cache.prepare_alphaengine_search_request(
        {"route": "transcript", "ticker": "NVDA", "start": "2026-01-01", "end": "2026-11-18"},
        op=ae_cache.OP_TRANSCRIPT,
    )
    hit, _ = await bridge._get_alphaengine_search_cache().get(
        plan.cache_key, plan.base_key, plan.req_limit, op=plan.op
    )
    assert hit is None

    # 3. An already-open breaker (consecutive_trips=2) is NOT reset by an arriving partial result
    bridge2 = _make_bridge(tmp_path / "b2", monkeypatch)
    mono2 = [5000.0]
    breaker2 = ae_cache.AlphaEngineSearchCircuitBreaker(now_fn=lambda: mono2[0])
    breaker2.record_quota_failure("权限或额度：init-1")
    mono2[0] += 61.0
    breaker2.record_quota_failure("权限或额度：init-2")
    assert breaker2.is_open() is True
    assert breaker2.snapshot()["consecutive_trips"] == 2
    bridge2._ae_search_breaker = breaker2

    async def _tr_partial_while_open(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        return [_mcp_reply(requests[0]["id"], gw_out)]

    # Simulate breaker opening between enqueue and upstream completion (via post-slot race bypass)
    monkeypatch.setattr(
        bridge2,
        "_run_stdio_rpc",
        create_autospec(bridge2._run_stdio_rpc, side_effect=_tr_partial_while_open),
    )
    orig_is_open = breaker2.is_open
    calls_open = [0]

    def _open_after_dispatch() -> bool:
        calls_open[0] += 1
        if calls_open[0] <= 2:
            return False
        return orig_is_open()

    monkeypatch.setattr(breaker2, "is_open", _open_after_dispatch)
    resp2 = await asyncio.wait_for(
        bridge2.handle_tools_call(
            "tr-partial-open",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "NVDA",
                    "start": "2026-01-01",
                    "end": "2026-11-18",
                },
            },
        ),
        timeout=5.0,
    )
    assert json.loads(resp2["result"]["content"][0]["text"])["status"] == "partial"
    assert orig_is_open() is True
    assert breaker2.snapshot()["consecutive_trips"] == 2


@pytest.mark.asyncio
async def test_p2_r4_shared_task_not_cancelled_after_upstream_started_when_a_gets_stale_and_b_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [40_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"
    await cache.initialize()

    args = {"route": "alphaengine", "operation": "search_library", "query": "CancelKeepAlive"}
    plan = ae_cache.prepare_alphaengine_search_request(args, op=ae_cache.OP_SEARCH_LIBRARY)
    await cache.put(
        cache_key=plan.cache_key,
        base_key=plan.base_key,
        op=plan.op,
        user_scope="team",
        req_limit=plan.req_limit,
        result_count=1,
        has_more=False,
        payload={"ok": True, "status": "ok", "results": [{"doc_id": "stale-ab"}]},
        date_to=plan.date_to,
    )
    wall_box[0] += 3 * 3600

    upstream_started = asyncio.Event()
    allow_finish = asyncio.Event()
    upstream_cancelled = False

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        nonlocal upstream_cancelled
        upstream_started.set()
        try:
            await asyncio.wait_for(allow_finish.wait(), timeout=3.0)
        except asyncio.CancelledError:
            upstream_cancelled = True
            raise
        return [
            _mcp_reply(
                requests[0]["id"],
                {"ok": True, "status": "ok", "results": [{"doc_id": "fresh-ab"}]},
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    )

    task_a = asyncio.create_task(
        bridge._execute_alphaengine_cached_search(
            req_id="ab-a",
            op=ae_cache.OP_SEARCH_LIBRARY,
            arguments=args,
            caller_identity=None,
            caller_timeout_s=0.3,
            shared_timeout_s=5.0,
            fanout=False,
        )
    )
    await asyncio.wait_for(upstream_started.wait(), timeout=2.0)

    task_b = asyncio.create_task(
        bridge._execute_alphaengine_cached_search(
            req_id="ab-b",
            op=ae_cache.OP_SEARCH_LIBRARY,
            arguments=args,
            caller_identity=None,
            caller_timeout_s=5.0,
            shared_timeout_s=5.0,
            fanout=False,
        )
    )
    await asyncio.sleep(0.02)

    # A gets stale cache before its tight deadline
    res_a = await asyncio.wait_for(task_a, timeout=1.5)
    assert res_a["ok"] is True
    assert res_a["stale"] is True
    assert res_a["payload"]["results"][0]["doc_id"] == "stale-ab"

    # Now B (the last remaining waiter) cancels: runner must NOT be cancelled because upstream already started
    task_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task_b, timeout=1.5)
    assert upstream_cancelled is False

    runners = list(bridge._get_singleflight_runners().values())
    assert len(runners) == 1
    assert not runners[0]["runner_task"].done()

    # Let shared upstream complete and persist fresh result into cache
    allow_finish.set()
    await asyncio.wait_for(runners[0]["runner_task"], timeout=2.0)
    assert upstream_cancelled is False

    hit_after = await asyncio.wait_for(
        bridge._execute_alphaengine_cached_search(
            req_id="ab-c",
            op=ae_cache.OP_SEARCH_LIBRARY,
            arguments=args,
            caller_identity=None,
            caller_timeout_s=5.0,
            shared_timeout_s=5.0,
            fanout=False,
        ),
        timeout=2.0,
    )
    assert hit_after["bridge_cache"]["status"] == "hit"
    assert hit_after["payload"]["results"][0]["doc_id"] == "fresh-ab"


@pytest.mark.asyncio
async def test_p2_r4_get_stale_bounded_by_caller_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    await cache.initialize()

    allow_finish = asyncio.Event()

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        await asyncio.wait_for(allow_finish.wait(), timeout=3.0)
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(1, has_more=False))]

    async def _blocked_get_stale(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(5.0)
        return None

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc)
    )
    monkeypatch.setattr(cache, "get_stale", _blocked_get_stale)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(rb._CachedSearchTimeoutError):
        await asyncio.wait_for(
            bridge._execute_alphaengine_cached_search(
                req_id="stale-hang",
                op=ae_cache.OP_SEARCH_LIBRARY,
                arguments={"route": "alphaengine", "operation": "search_library", "query": "HangStale"},
                caller_identity=None,
                caller_timeout_s=0.3,
                shared_timeout_s=5.0,
                fanout=False,
            ),
            timeout=1.2,
        )
    elapsed = loop.time() - t0
    assert elapsed < 0.8

    allow_finish.set()
    for r in list(bridge._get_singleflight_runners().values()):
        await asyncio.wait_for(r["runner_task"], timeout=2.0)


@pytest.mark.asyncio
async def test_p2_r4_fresh_true_skips_pre_deadline_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    cache = bridge._get_alphaengine_search_cache()
    wall_box = [50_000.0]
    cache._now_fn = lambda: wall_box[0]
    cache._today_fn = lambda: "2026-11-18"
    await cache.initialize()

    args_fresh = {
        "route": "alphaengine",
        "operation": "search_library",
        "query": "FreshSkipsDeadlineStale",
        "params": {"fresh": True},
    }
    plan = ae_cache.prepare_alphaengine_search_request(args_fresh, op=ae_cache.OP_SEARCH_LIBRARY)
    await cache.put(
        cache_key=plan.cache_key,
        base_key=plan.base_key,
        op=plan.op,
        user_scope="team",
        req_limit=plan.req_limit,
        result_count=1,
        has_more=False,
        payload={"ok": True, "status": "ok", "results": [{"doc_id": "old-doc"}]},
        date_to=plan.date_to,
    )
    wall_box[0] += 3 * 3600

    allow_finish = asyncio.Event()

    async def _slow_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        await asyncio.wait_for(allow_finish.wait(), timeout=3.0)
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(1, has_more=False))]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc)
    )

    with pytest.raises(rb._CachedSearchTimeoutError):
        await asyncio.wait_for(
            bridge._execute_alphaengine_cached_search(
                req_id="fresh-deadline",
                op=ae_cache.OP_SEARCH_LIBRARY,
                arguments=args_fresh,
                caller_identity=None,
                caller_timeout_s=0.25,
                shared_timeout_s=5.0,
                fanout=False,
            ),
            timeout=1.2,
        )

    allow_finish.set()
    for r in list(bridge._get_singleflight_runners().values()):
        await asyncio.wait_for(r["runner_task"], timeout=2.0)


@pytest.mark.asyncio
async def test_p2_r4_upstream_pages_fetched_recorded_and_reported_for_direct_and_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _paged_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route in ("alphaengine",):
            payload = _ae_results_payload(2, has_more=False)
            payload["data"]["structuredContent"]["upstream_pages_fetched"] = 3
            return [_mcp_reply(req["id"], payload)]
        if route == "guidepoint":
            return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]
        return [
            _mcp_reply(
                req["id"],
                {"ok": True, "status": "ok", "results": [], "source_status": {"sales-notes": {"ok": True, "count": 0}}},
            )
        ]

    monkeypatch.setattr(
        bridge, "_run_stdio_rpc", create_autospec(bridge._run_stdio_rpc, side_effect=_paged_rpc)
    )

    # 1. Direct search_library
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "pages-direct",
            {"name": "research", "arguments": {"route": "alphaengine", "operation": "search_library", "query": "DirectPages"}},
        ),
        timeout=5.0,
    )
    # 2. route="all" fanout
    await asyncio.wait_for(
        bridge.handle_tools_call(
            "pages-fanout",
            {"name": "research", "arguments": {"route": "all", "operation": "search", "query": "FanoutPages"}},
        ),
        timeout=5.0,
    )

    entries = usage_report.load_entries([bridge.cfg.alphaengine_usage_file])
    assert len(entries) == 2
    assert all(e.get("pages_scanned") == 3 for e in entries)
    summary = usage_report.summarize(entries)
    assert summary["search_upstream_calls"] == 6


def test_p2_r4_strict_breaker_text_signals() -> None:
    # Ambiguous or unrelated mentions of 429 / 额度 MUST NOT trigger breaker
    for err_text in ("doc 429 not valid", "套餐额度不含此源", "HTTP 403 Forbidden", "auth_expired"):
        cat, _ = rb._classify_error_category_and_hint(
            "research", "alphaengine", "search_library", {"error": err_text}
        )
        should_trip, _ = rb._classify_ae_search_breaker_signal(
            cat, {"error": err_text}, {"error_message": err_text}
        )
        assert should_trip is False, f"Unexpected trip on {err_text!r}"

    # Explicit HTTP 429 / quota exhaustion / rate-limit signals MUST trigger breaker
    trigger_cases: list[dict[str, Any]] = [
        {"error": "HTTP 429 Too Many Requests"},
        {"error": "status 429"},
        {"error": "status_code=429"},
        {"status_code": 429, "error": "upstream throttled"},
        {"error": "E_UPSTREAM_API: 无查看权限或额度已达上限"},
        {"error": "当月检索额度已达上限"},
        {"error": "触发频率限制，请稍后重试"},
        {"status": "quota_exceeded", "error": "quota_exceeded"},
        {"status": "rate_limited", "error": "rate_limited"},
    ]
    for probe in trigger_cases:
        cat, _ = rb._classify_error_category_and_hint(
            "research", "alphaengine", "search_library", dict(probe)
        )
        should_trip, reason = rb._classify_ae_search_breaker_signal(
            cat, probe, {"error_message": str(probe.get("error") or "")}
        )
        assert should_trip is True, f"Expected trip on {probe!r}"
        assert reason




def test_p2_r4_gateway_page_error_json_is_flattened_for_breaker() -> None:
    import importlib.util

    gw_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "research_runtime"
        / "scripts"
        / "data_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("test_dg_r4_flatten", gw_path)
    assert spec is not None and spec.loader is not None
    dg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dg)

    cases = [
        json.dumps({"ok": False, "status_code": 429, "error": "upstream busy"}),
        json.dumps({"ok": False, "error": "E_UPSTREAM_API: 无查看权限或额度已达上限"}),
    ]
    for raw in cases:
        flat = dg._page_error_text(raw)
        cat, _ = rb._classify_error_category_and_hint(
            "research", "alphaengine", "search_library", {"error": flat}
        )
        should_trip, _ = rb._classify_ae_search_breaker_signal(
            cat, {"error": flat}, {"error_message": flat}
        )
        assert should_trip is True, f"expected trip for {raw!r} -> {flat!r}"
    assert dg._page_error_text("plain text error") == "plain text error"


def test_p2_r5_gateway_long_escaped_json_error_parsed_before_truncation() -> None:
    import importlib.util

    gw_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "research_runtime"
        / "scripts"
        / "data_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("test_dg_r5_long", gw_path)
    assert spec is not None and spec.loader is not None
    dg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dg)

    raw = json.dumps(
        {"ok": False, "error": "E_UPSTREAM_API: 无查看权限或额度已达上限", "detail": "x" * 400}
    )
    assert len(raw) > 300
    flat = dg._page_error_text(raw)
    assert "无查看权限或额度已达上限" in flat


def test_a_page_with_a_cursor_older_than_the_mcp_keeps_it_is_a_miss(tmp_path: Path) -> None:
    """Regression (2026-11-20): a cached first page handed back a cursor the MCP had
    already dropped (15-minute TTL), so page 2 failed with E_CURSOR_EXPIRED."""
    now_box = [1000.0]
    cache = ae_cache.AlphaEngineSearchCache(
        tmp_path / "cursor.sqlite3", today_fn=lambda: "2026-11-20", now_fn=lambda: now_box[0]
    )
    common = dict(op=ae_cache.OP_SEARCH_LIBRARY, user_scope="team", req_limit=50, date_to="2026-11-20")
    cache.put_sync(cache_key="more", base_key="b-more", result_count=50, has_more=True,
                   payload=_ae_results_payload(50, has_more=True, cursor="ae1:abc"), **common)
    cache.put_sync(cache_key="last", base_key="b-last", result_count=5, has_more=False,
                   payload=_ae_results_payload(5), **common)

    now_box[0] += ae_cache.CURSOR_SAFE_AGE_S - 1
    assert cache.get_sync("more", "b-more", 50, op=ae_cache.OP_SEARCH_LIBRARY)[0] is not None

    now_box[0] += 2
    miss, degraded = cache.get_sync("more", "b-more", 50, op=ae_cache.OP_SEARCH_LIBRARY)
    assert miss is None and not degraded
    # A complete page (no more results, no cursor) still serves from cache.
    assert cache.get_sync("last", "b-last", 50, op=ae_cache.OP_SEARCH_LIBRARY)[0] is not None


def test_a_stale_copy_never_carries_a_cursor() -> None:
    payload = _ae_results_payload(3, has_more=True, cursor="ae1:abc")
    assert ae_cache.payload_has_live_cursor(payload, op=ae_cache.OP_SEARCH_LIBRARY)
    stripped = ae_cache.strip_cursor(payload, op=ae_cache.OP_SEARCH_LIBRARY)
    assert not ae_cache.payload_has_live_cursor(stripped, op=ae_cache.OP_SEARCH_LIBRARY)
    assert ae_cache.payload_has_live_cursor(payload, op=ae_cache.OP_SEARCH_LIBRARY)  # original untouched
