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

    # Query case IS preserved -> lowercase differs from mixed case
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
    assert p_lower.cache_key != p1.cache_key
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

    # Filter params differ -> different key; non-code arrays preserve order
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
    assert p_order_a.cache_key != p_order_b.cache_key


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
        cache_key: str, base_key: str, req_limit: int | None, *, op: str
    ) -> tuple[ae_cache.CacheHit | None, bool]:
        nonlocal get_count
        get_count += 1
        call_no = get_count
        res = await orig_get(cache_key, base_key, req_limit, op=op)
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


def test_r4_3_query_normalization_preserves_case_in_key_and_upstream() -> None:
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
    assert p_lower.cache_key != p_ascii.cache_key
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
