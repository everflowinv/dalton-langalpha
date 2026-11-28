"""Unit tests for researchdata_bridge per-caller fair concurrency, automation priority, fanout grace, and observability."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from ptc_agent.agent.tools.code_execution import _bound_caller_identity  # noqa: E402
from ptc_agent.core.sandbox import mcp_client_runtime as mcr  # noqa: E402
from ptc_agent.core.sandbox.supervisor_runtime import daemon as sup_daemon  # noqa: E402
from src.dalton.sandbox_caller_identity import caller_env  # noqa: E402


def _make_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_inflight: int | None = None,
    max_inflight_per_caller: int | None = None,
    max_inflight_unsigned: int | None = None,
    automation_reserved: int | None = None,
    fanout_grace_s: float | None = None,
    admin_token: str | None = None,
) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "test-token")
    if admin_token is not None:
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", admin_token)
    else:
        monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "DALTON_ALPHAENGINE_CACHE_DB",
        str(tmp_path / "alphaengine_search_cache.sqlite3"),
    )
    monkeypatch.setenv(
        "DALTON_ALPHAENGINE_USAGE_FILE",
        str(tmp_path / "alphaengine_usage.jsonl"),
    )
    if max_inflight is not None:
        monkeypatch.setenv("DALTON_BRIDGE_MAX_INFLIGHT", str(max_inflight))
    else:
        monkeypatch.delenv("DALTON_BRIDGE_MAX_INFLIGHT", raising=False)
    if max_inflight_per_caller is not None:
        monkeypatch.setenv(
            "DALTON_BRIDGE_MAX_INFLIGHT_PER_CALLER", str(max_inflight_per_caller)
        )
    else:
        monkeypatch.delenv("DALTON_BRIDGE_MAX_INFLIGHT_PER_CALLER", raising=False)
    if max_inflight_unsigned is not None:
        monkeypatch.setenv(
            "DALTON_BRIDGE_MAX_INFLIGHT_UNSIGNED", str(max_inflight_unsigned)
        )
    else:
        monkeypatch.delenv("DALTON_BRIDGE_MAX_INFLIGHT_UNSIGNED", raising=False)
    if automation_reserved is not None:
        monkeypatch.setenv("DALTON_BRIDGE_AUTOMATION_RESERVED", str(automation_reserved))
    else:
        monkeypatch.delenv("DALTON_BRIDGE_AUTOMATION_RESERVED", raising=False)
    if fanout_grace_s is not None:
        monkeypatch.setenv("DALTON_BRIDGE_FANOUT_GRACE_S", str(fanout_grace_s))
    else:
        monkeypatch.delenv("DALTON_BRIDGE_FANOUT_GRACE_S", raising=False)

    cfg = rb.BridgeConfig()
    cfg.data_sources_json = tmp_path / "missing_data_sources.json"
    return rb.ResearchDataBridge(cfg)


def _mcp_reply(
    req_id: Any, payload: dict[str, Any], *, is_error: bool = False
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
            ],
            "structuredContent": dict(payload),
            "isError": is_error,
        },
    }


def _ae_results_payload(count: int = 2, prefix: str = "9900") -> dict[str, Any]:
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
                "has_more": False,
                "cursor": None,
                "total_upstream": len(items),
                "filters": {},
            }
        },
    }


def _get_doc_call(doc_id: str) -> dict[str, Any]:
    return {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": doc_id},
        },
    }


def test_default_bridge_config_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    assert bridge.cfg.max_inflight == 6
    assert bridge.cfg.max_inflight_per_caller == 2
    assert bridge.cfg.max_inflight_unsigned == 1
    assert bridge.cfg.automation_reserved == 2
    assert bridge.cfg.fanout_grace_s == 20.0


@pytest.mark.asyncio
async def test_single_caller_flood_does_not_block_other_caller_or_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    flood_entered = 0
    flood_two_entered = asyncio.Event()
    release_flood = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal flood_entered
        req = requests[0]
        doc_id = str(
            req["params"]["arguments"].get("params", {}).get("doc_id") or "doc"
        )
        if doc_id.startswith("flood-"):
            flood_entered += 1
            if flood_entered >= 2:
                flood_two_entered.set()
            await release_flood.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": f"body:{doc_id}"})]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    flood_caller = {"user": "u-flood", "workspace": "ws-flood-11112222", "thread": "t1"}
    other_caller = {"user": "u-other", "workspace": "ws-other-33334444", "thread": "t2"}
    auto_caller = {
        "user": "u-auto",
        "workspace": "ws-auto-55556666",
        "thread": "t3",
        "run_origin": "automation",
    }

    flood_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"f-{i}", _get_doc_call(f"flood-{i}"), caller_identity=flood_caller
            )
        )
        for i in range(5)
    ]
    await asyncio.wait_for(flood_two_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    # Flood caller is capped at 2 inflight and queues the remaining 3 in its own queue.
    snap = bridge._get_concurrency().status_snapshot()
    assert flood_entered == 2
    assert snap["global_inflight"] == 2
    assert snap["global_queued"] == 0
    assert snap["callers"] == {"ws:ws-flood": {"inflight": 2, "queued": 3}}

    # Both another interactive caller and an automation caller proceed immediately.
    other_res, auto_res = await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call(
                "other-1", _get_doc_call("other-doc"), caller_identity=other_caller
            ),
            bridge.handle_tools_call(
                "auto-1", _get_doc_call("auto-doc"), caller_identity=auto_caller
            ),
        ),
        timeout=2.0,
    )
    assert json.loads(other_res["result"]["content"][0]["text"])["ok"] is True
    assert json.loads(auto_res["result"]["content"][0]["text"])["ok"] is True

    release_flood.set()
    flood_resps = await asyncio.wait_for(asyncio.gather(*flood_tasks), timeout=2.0)
    assert all(
        json.loads(r["result"]["content"][0]["text"])["ok"] is True for r in flood_resps
    )
    assert bridge._get_concurrency().status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }


@pytest.mark.asyncio
async def test_automation_reserved_slots_and_priority_over_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        automation_reserved=2,
    )
    inter_active = 0
    inter_four_entered = asyncio.Event()
    release_inter = asyncio.Event()
    auto_two_entered = asyncio.Event()
    release_auto = asyncio.Event()
    auto_entered = 0
    execution_order: list[str] = []

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal inter_active, auto_entered
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        execution_order.append(doc_id)
        if doc_id.startswith("inter-"):
            inter_active += 1
            if inter_active >= 4:
                inter_four_entered.set()
            await release_inter.wait()
        elif doc_id.startswith("auto-"):
            auto_entered += 1
            if auto_entered >= 2:
                auto_two_entered.set()
            await release_auto.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 5 interactive calls across 3 workspaces -> max 4 run (2 slots reserved for automation).
    inter_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                "i-1", _get_doc_call("inter-1"), caller_identity={"workspace": "ws-i1"}
            )
        ),
        asyncio.create_task(
            bridge.handle_tools_call(
                "i-2", _get_doc_call("inter-2"), caller_identity={"workspace": "ws-i1"}
            )
        ),
        asyncio.create_task(
            bridge.handle_tools_call(
                "i-3", _get_doc_call("inter-3"), caller_identity={"workspace": "ws-i2"}
            )
        ),
        asyncio.create_task(
            bridge.handle_tools_call(
                "i-4", _get_doc_call("inter-4"), caller_identity={"workspace": "ws-i2"}
            )
        ),
        asyncio.create_task(
            bridge.handle_tools_call(
                "i-5", _get_doc_call("inter-5"), caller_identity={"workspace": "ws-i3"}
            )
        ),
    ]
    await asyncio.wait_for(inter_four_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    snap = bridge._get_concurrency().status_snapshot()
    assert inter_active == 4
    assert snap["global_inflight"] == 4
    assert snap["global_queued"] == 1
    assert "inter-5" not in execution_order

    # 2 automation calls enter the 2 reserved slots immediately while i-5 remains queued.
    auto_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"a-{k}",
                _get_doc_call(f"auto-{k}"),
                caller_identity={"workspace": f"ws-a{k}", "run_origin": "automation"},
            )
        )
        for k in (1, 2)
    ]
    await asyncio.wait_for(auto_two_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    assert bridge._get_concurrency().status_snapshot()["global_inflight"] == 6

    # Queue a 3rd automation call; release auto-1/2 and verify auto-3 runs before inter-5.
    auto3_task = asyncio.create_task(
        bridge.handle_tools_call(
            "a-3",
            _get_doc_call("auto-3"),
            caller_identity={"workspace": "ws-a3", "run_origin": "automation"},
        )
    )
    await asyncio.sleep(0.02)
    release_auto.set()
    await asyncio.wait_for(asyncio.gather(*auto_tasks, auto3_task), timeout=2.0)
    assert "auto-3" in execution_order
    assert "inter-5" not in execution_order

    release_inter.set()
    inter_resps = await asyncio.wait_for(asyncio.gather(*inter_tasks), timeout=2.0)
    assert all(
        json.loads(r["result"]["content"][0]["text"])["ok"] is True for r in inter_resps
    )


@pytest.mark.asyncio
async def test_caller_key_precedence_workspace_then_user_then_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=1,
        automation_reserved=0,
    )
    active_by_doc: dict[str, asyncio.Event] = {}
    in_flight_max = 0
    in_flight_cur = 0

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal in_flight_max, in_flight_cur
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        in_flight_cur += 1
        in_flight_max = max(in_flight_max, in_flight_cur)
        try:
            ev = active_by_doc.get(doc_id)
            if ev is not None:
                ev.set()
            await asyncio.sleep(0.03)
            return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]
        finally:
            in_flight_cur -= 1

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 1) Same user, different workspaces -> independent caller keys -> run concurrently (max=2).
    in_flight_max = 0
    await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call(
                "w1",
                _get_doc_call("d1"),
                caller_identity={"user": "same-u", "workspace": "ws-one"},
            ),
            bridge.handle_tools_call(
                "w2",
                _get_doc_call("d2"),
                caller_identity={"user": "same-u", "workspace": "ws-two"},
            ),
        ),
        timeout=2.0,
    )
    assert in_flight_max == 2

    # 2) No workspace, same user -> serialized at per_caller=1 (max=1).
    in_flight_max = 0
    await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call(
                "u1", _get_doc_call("u1"), caller_identity={"user": "u-shared"}
            ),
            bridge.handle_tools_call(
                "u2", _get_doc_call("u2"), caller_identity={"user": "u-shared"}
            ),
        ),
        timeout=2.0,
    )
    assert in_flight_max == 1

    # 3) No workspace, no user -> all unsigned calls share the single `unsigned` bucket (cap=1) regardless of conn_id.
    in_flight_max = 0
    await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call("c1", _get_doc_call("c1"), conn_id="10.0.0.5"),
            bridge.handle_tools_call("c2", _get_doc_call("c2"), conn_id="10.0.0.5"),
        ),
        timeout=2.0,
    )
    assert in_flight_max == 1

    in_flight_max = 0
    await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call("c3", _get_doc_call("c3"), conn_id="10.0.0.5"),
            bridge.handle_tools_call("c4", _get_doc_call("c4"), conn_id="10.0.0.6"),
        ),
        timeout=2.0,
    )
    assert in_flight_max == 1


@pytest.mark.asyncio
async def test_fanout_grace_cancels_slow_guidepoint_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        fanout_grace_s=0.05,
    )
    gp_cancelled = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        route = args.get("route")
        if route == "all":
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "results": [{"document_id": "wiki:doc-1", "title": "Local"}],
                        "result_count": 1,
                    },
                )
            ]
        if route == "alphaengine":
            return [_mcp_reply(req["id"], _ae_results_payload(2))]
        if route == "guidepoint":
            if req["id"] == "direct-gp":
                await asyncio.sleep(0.08)
                return [
                    _mcp_reply(
                        req["id"],
                        {
                            "ok": True,
                            "data": {"structuredContent": {"data": [{"id": "gp-9"}]}},
                        },
                    )
                ]
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                gp_cancelled.set()
                raise
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "fanout-grace",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "NVDA"},
            },
            caller_identity={"workspace": "ws-fanout-1"},
        ),
        timeout=2.0,
    )
    assert gp_cancelled.is_set()
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["status"] == "partial_error"
    assert payload["partial"] == ["local_all", "alphaengine"]
    assert payload["source_status"]["guidepoint"]["status"] == "timeout"
    assert payload["guidepoint"]["status"] == "timeout"
    assert payload["source_status"]["local_all"]["ok"] is True
    assert payload["source_status"]["alphaengine"]["ok"] is True
    assert any(r.get("document_id") == "wiki:doc-1" for r in payload["results"])
    assert any(
        r.get("document_id") == "alphaengine:99000001" for r in payload["results"]
    )
    # Slot released immediately after grace cancel.
    assert bridge._get_concurrency().status_snapshot()["global_inflight"] == 0

    # Standalone route='guidepoint' keeps its own timeout (not cut off by fanout_grace_s=0.05).
    gp_direct = await asyncio.wait_for(
        bridge.handle_tools_call(
            "direct-gp",
            {
                "name": "research",
                "arguments": {
                    "route": "guidepoint",
                    "operation": "search_library",
                    "query": "NVDA",
                },
            },
            caller_identity={"workspace": "ws-fanout-1"},
        ),
        timeout=2.0,
    )
    gp_direct_payload = json.loads(gp_direct["result"]["content"][0]["text"])
    assert gp_direct_payload["ok"] is True


@pytest.mark.asyncio
async def test_fanout_skipped_guidepoint_does_not_cut_short_local_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        fanout_grace_s=0.04,
    )

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route == "all":
            await asyncio.sleep(0.08)
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "results": [{"document_id": "wiki:doc-long"}],
                        "result_count": 1,
                    },
                )
            ]
        return [_mcp_reply(req["id"], _ae_results_payload(1))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    long_query = "NVDA " * 250  # >1000 chars -> guidepoint skipped
    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "fanout-skip",
            {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "search",
                    "query": long_query,
                },
            },
            caller_identity={"workspace": "ws-skip-1"},
        ),
        timeout=2.0,
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["guidepoint"]["status"] == "skipped"
    assert payload["source_status"]["local_all"]["ok"] is True


@pytest.mark.asyncio
async def test_alphaengine_cache_hit_does_not_consume_caller_or_global_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=1,
        max_inflight_per_caller=1,
        automation_reserved=0,
    )
    search_calls = 0
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal search_calls
        req = requests[0]
        args = req["params"]["arguments"]
        if args.get("operation") == "get_document":
            blocker_entered.set()
            await release_blocker.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": "doc"})]
        search_calls += 1
        return [_mcp_reply(req["id"], _ae_results_payload(2))]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    ws_caller = {"user": "u1", "workspace": "ws-cache-test"}
    search_req = {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "TSLA robotics",
        },
    }

    # 1. Populate exact cache.
    r1 = await asyncio.wait_for(
        bridge.handle_tools_call("seed", search_req, caller_identity=ws_caller),
        timeout=2.0,
    )
    b1 = json.loads(r1["result"]["content"][0]["text"])
    assert b1["_bridge_cache"]["status"] == "miss"
    assert search_calls == 1

    # 2. Same caller fills both its 1/1 per-caller slot AND the 1/1 global slot with a slow get_document.
    blocker_task = asyncio.create_task(
        bridge.handle_tools_call(
            "block-1", _get_doc_call("slow-doc"), caller_identity=ws_caller
        )
    )
    await asyncio.wait_for(blocker_entered.wait(), timeout=2.0)

    # 3. Cache hit for the SAME caller succeeds immediately without waiting on caller or global slot!
    r2 = await asyncio.wait_for(
        bridge.handle_tools_call("hit-1", search_req, caller_identity=ws_caller),
        timeout=1.0,
    )
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b2["_bridge_cache"]["status"] == "hit"
    assert search_calls == 1

    release_blocker.set()
    await asyncio.wait_for(blocker_task, timeout=2.0)


@pytest.mark.asyncio
async def test_observability_logs_queued_ms_redacted_caller_and_healthz_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=1,
        automation_reserved=2,
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        if doc_id == "d-first":
            first_entered.set()
            await release_first.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)
    monkeypatch.setattr(
        bridge,
        "probe_xreach_version",
        create_autospec(bridge.probe_xreach_version, return_value={"ok": True}),
    )
    monkeypatch.setattr(
        bridge,
        "probe_tmeet_version",
        create_autospec(bridge.probe_tmeet_version, return_value={"ok": True}),
    )

    full_ws = "12345678-secret-suffix-9999"
    caller = {"user": "u-obs", "workspace": full_ws}

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        t1 = asyncio.create_task(
            bridge.handle_tools_call(
                "obs-1", _get_doc_call("d-first"), caller_identity=caller
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=2.0)
        t2 = asyncio.create_task(
            bridge.handle_tools_call(
                "obs-2", _get_doc_call("d-second"), caller_identity=caller
            )
        )
        await asyncio.sleep(0.03)

        # Query /healthz via _handle_client while t1 is inflight and t2 is queued.
        raw_req = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:8960\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw_req)
        reader.feed_eof()
        written_chunks: list[bytes] = []
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        writer.write.side_effect = lambda b: written_chunks.append(b)
        await asyncio.wait_for(rb._handle_client(bridge, reader, writer), timeout=2.0)

        raw_http = b"".join(written_chunks).decode("utf-8")
        health_body = json.loads(raw_http.split("\r\n\r\n", 1)[1])
        assert health_body["max_inflight"] == 6
        assert health_body["max_inflight_per_caller"] == 1
        assert health_body["automation_reserved"] == 2
        assert health_body["fanout_grace_s"] == 20.0
        assert health_body["inflight"] == 1
        assert health_body["queued"] == 1
        assert health_body["orphan_upstream"] == 0
        assert health_body["callers"] == {"ws:12345678": {"inflight": 1, "queued": 1}}
        assert full_ws not in raw_http

        release_first.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)

    tool_logs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("tools/call ")
    ]
    assert len(tool_logs) == 2
    assert all("caller=ws:12345678" in m for m in tool_logs)
    assert all(full_ws not in m for m in tool_logs)
    queued_vals = [
        int(m.split("queued_ms=", 1)[1].split(" ", 1)[0]) for m in tool_logs
    ]
    assert min(queued_vals) >= 0
    assert max(queued_vals) >= 20


@pytest.mark.asyncio
async def test_caller_identity_propagates_run_origin_automation_to_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    import hashlib
    import hmac
    import time as _time

    shared_secret = "bridge-admin-shared-secret-xyz"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", shared_secret)

    # 1. ExecuteCode/Bash binding signs {user, workspace, thread, run_origin, exp}
    #    using HKDF(info=dalton-bridge-caller-v1), and ignores raw cfg["run_origin"].
    deadline_ts = int(_time.time()) + 1800
    with _bound_caller_identity(
        {
            "configurable": {
                "user_id": "u-auto-12345",
                "workspace_id": "ws-auto-99999",
                "thread_id": "th-auto-1",
                "turn_origin_automation_id": "auto-uuid-1",
                "turn_deadline": deadline_ts,
            }
        },
        "fallback",
    ):
        env_auto = caller_env()
    assert "DALTON_CALLER_RUN_ORIGIN" not in env_auto
    token_auto = env_auto["DALTON_CALLER_TOKEN"]
    payload_b64, sig_b64 = token_auto.split(".")
    decoded_claims = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
    )
    assert decoded_claims == {
        "user": "u-auto-12345",
        "workspace": "ws-auto-99999",
        "thread": "th-auto-1",
        "run_origin": "automation",
        "run_id": "",
        "turn_origin_automation_id": "auto-uuid-1",
        "automation_execution_id": "",
        "exp": deadline_ts,
    }
    # Must NOT be signed directly with raw admin token (must use HKDF subkey).
    raw_token_sig = base64.urlsafe_b64encode(
        hmac.new(shared_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert sig_b64 != raw_token_sig

    # Raw configurable["run_origin"]="automation" without turn_origin_automation_id is ignored.
    with _bound_caller_identity(
        {
            "configurable": {
                "user_id": "u-inter-12345",
                "workspace_id": "ws-inter-99999",
                "thread_id": "th-inter-1",
                "run_origin": "automation",
            }
        },
        "fallback",
    ):
        env_inter = caller_env()
    inter_p = env_inter["DALTON_CALLER_TOKEN"].split(".")[0]
    inter_claims = json.loads(
        base64.urlsafe_b64decode(inter_p + "=" * (-len(inter_p) % 4)).decode("utf-8")
    )
    assert inter_claims["run_origin"] == ""

    # 2. mcp_client_runtime reads DALTON_CALLER_TOKEN and emits X-Dalton-Caller-Token.
    for k, v in env_auto.items():
        monkeypatch.setenv(k, v)
    parsed = mcr._caller_identity_from_env()
    assert parsed["token"] == token_auto
    cfg = mcr._normalize(
        "researchdata",
        {"transport": "http", "untrusted": False, "url": "http://127.0.0.1:8960/mcp"},
    )
    hdrs = mcr._apply_caller_headers({}, cfg, parsed)
    assert hdrs["X-Dalton-Caller-Token"] == token_auto
    assert hdrs["X-Dalton-Caller-Scope"] == "per-request"

    # 3. Supervisor daemon forwards signed token from request["caller"] and advertises v2.
    captured: list[dict[str, str]] = []
    backend = create_autospec(sup_daemon.ClientBackend, instance=True)
    backend.config_version.return_value = 0
    backend.names.return_value = ["researchdata"]
    backend.is_running.return_value = True
    backend.secret_fingerprint.return_value = []
    backend.secret_dependencies.return_value = set()
    backend.serializes_calls.return_value = False
    backend.workspace_config_relpath.return_value = ".agents/tools/mcp_client_config.json"

    def _capture_call(server, tool, args, timeout, *, caller=None):
        captured.append(dict(caller or {}))
        return {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}}

    backend.call_with_caller.side_effect = _capture_call
    cfg_dir = tmp_path / ".agents" / "tools"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = str(cfg_dir / "mcp_client_config.json")
    Path(cfg_path).write_text(
        json.dumps({"workspace_id": "ws-auto-99999", "servers": {"researchdata": {}}}),
        encoding="utf-8",
    )
    sup = sup_daemon.Supervisor(
        str(tmp_path / "s.sock"),
        backend,
        root=str(tmp_path),
        idle_window_s=1e9,
        heartbeat_s=3600.0,
        log=lambda _m: None,
    )
    try:
        assert sup.health()["caller_identity_v2"] is True
        conn = create_autospec(sup_daemon._Conn, instance=True)
        conn.closed = False
        conn.send.return_value = True
        sup._run_call(
            conn,
            7,
            {
                "v": 1,
                "id": 7,
                "op": "call",
                "server": "researchdata",
                "tool": "research",
                "args": {"route": "alphaengine"},
                "workspace_id": "ws-auto-99999",
                "config_path": cfg_path,
                "caller": parsed,
            },
            _time.monotonic(),
        )
    finally:
        sup.close()
    assert len(captured) == 1
    assert captured[0]["token"] == token_auto

    # 4. Bridge HTTP handler verifies X-Dalton-Caller-Token; forged/expired/missing tokens degrade cleanly.
    bridge = _make_bridge(tmp_path / "b", monkeypatch, admin_token=shared_secret)
    seen_routing: list[tuple[str, bool]] = []
    orig_handle = bridge.handle_tools_call

    async def _spy_handle(
        req_id, params, *, caller_identity=None, conn_id=None, raw_caller_token=None
    ):
        ckey, _, is_auto = rb._resolve_caller_routing(caller_identity, conn_id=conn_id or "")
        seen_routing.append((ckey, is_auto))
        return await orig_handle(
            req_id,
            params,
            caller_identity=caller_identity,
            conn_id=conn_id,
            raw_caller_token=raw_caller_token,
        )

    monkeypatch.setattr(bridge, "handle_tools_call", _spy_handle)
    monkeypatch.setattr(
        bridge,
        "_run_stdio_rpc",
        create_autospec(
            bridge._run_stdio_rpc,
            side_effect=lambda reqs, timeout_s: [
                _mcp_reply(reqs[0]["id"], {"ok": True, "text": "ok"})
            ],
        ),
    )

    async def _send_mcp(extra_headers: str) -> None:
        body_bytes = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": _get_doc_call("d1")}
        ).encode("utf-8")
        raw_http = (
            "POST /mcp HTTP/1.1\r\n"
            "Host: 127.0.0.1:8960\r\n"
            "Authorization: Bearer test-token\r\n"
            + extra_headers
            + f"Content-Length: {len(body_bytes)}\r\n\r\n"
        ).encode("utf-8") + body_bytes
        reader = asyncio.StreamReader()
        reader.feed_data(raw_http)
        reader.feed_eof()
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        await asyncio.wait_for(rb._handle_client(bridge, reader, writer), timeout=2.0)

    # Valid signed automation token -> ws:ws-auto-99999 and is_automation=True
    await _send_mcp(
        f"X-Dalton-Caller-Token: {token_auto}\r\n"
        "X-Dalton-Caller-Scope: per-request\r\n"
    )
    # Forged unsigned headers without token -> degrades to conn: and interactive (no error)
    await _send_mcp(
        "X-Dalton-User: u-victim\r\n"
        "X-Dalton-Workspace: ws-victim\r\n"
        "X-Dalton-Run-Origin: automation\r\n"
        "X-Dalton-Automation-Id: fake-auto\r\n"
        "X-Dalton-Caller-Scope: per-request\r\n"
    )
    # Tampered signature -> degrades to conn: and interactive
    await _send_mcp(
        f"X-Dalton-Caller-Token: {payload_b64}.AAAAtampered\r\n"
        "X-Dalton-Caller-Scope: per-request\r\n"
    )
    # Expired token -> degrades to conn: and interactive
    from src.dalton.sandbox_caller_identity import sign_caller_token

    expired_tok = sign_caller_token(
        {"user": "u-auto", "workspace": "ws-auto", "run_origin": "automation"},
        exp=int(_time.time()) - 60,
        admin_token=shared_secret,
    )
    await _send_mcp(
        f"X-Dalton-Caller-Token: {expired_tok}\r\n"
        "X-Dalton-Caller-Scope: per-request\r\n"
    )

    assert seen_routing[0] == ("ws:ws-auto-99999", True)
    for ckey, is_auto in seen_routing[1:]:
        assert ckey == "unsigned"
        assert is_auto is False


@pytest.mark.asyncio
async def test_two_automation_and_four_interactive_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        automation_reserved=2,
    )
    six_entered = asyncio.Event()
    release_all = asyncio.Event()
    entered_docs: list[str] = []

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        entered_docs.append(doc_id)
        if len(entered_docs) >= 6:
            six_entered.set()
        await release_all.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # 2 automation first, then 4 interactive -> all 6 must run simultaneously.
    tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"a-{k}",
                _get_doc_call(f"auto-{k}"),
                caller_identity={"workspace": f"ws-auto-{k}", "run_origin": "automation"},
            )
        )
        for k in range(2)
    ] + [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"i-{k}",
                _get_doc_call(f"inter-{k}"),
                caller_identity={"workspace": f"ws-inter-{k}"},
            )
        )
        for k in range(4)
    ]
    await asyncio.wait_for(six_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    limiter = bridge._get_concurrency()
    assert limiter._global_inflight == 6
    assert limiter._interactive_inflight == 4

    # A 5th interactive call must queue rather than exceeding interactive_cap=4.
    extra_inter = asyncio.create_task(
        bridge.handle_tools_call(
            "i-extra",
            _get_doc_call("inter-extra"),
            caller_identity={"workspace": "ws-inter-extra"},
        )
    )
    await asyncio.sleep(0.02)
    assert "inter-extra" not in entered_docs
    assert limiter.status_snapshot()["global_queued"] == 1

    release_all.set()
    all_resps = await asyncio.wait_for(asyncio.gather(*tasks, extra_inter), timeout=2.0)
    assert all(
        json.loads(r["result"]["content"][0]["text"])["ok"] is True for r in all_resps
    )
    assert limiter._global_inflight == 0
    assert limiter._interactive_inflight == 0


@pytest.mark.asyncio
async def test_fanout_grace_cancels_slow_alphaengine_singleflight_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        fanout_grace_s=0.05,
    )
    ae_started = asyncio.Event()
    ae_cancelled = asyncio.Event()
    ae_cleaned_up = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route == "all":
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "results": [{"document_id": "wiki:doc-1", "title": "Local"}],
                        "result_count": 1,
                    },
                )
            ]
        if route == "guidepoint":
            return [
                _mcp_reply(
                    req["id"],
                    {"ok": True, "data": {"structuredContent": {"data": [{"id": "gp-1"}]}}},
                )
            ]
        if route == "alphaengine":
            ae_started.set()
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                ae_cancelled.set()
                raise
            finally:
                ae_cleaned_up.set()
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "fanout-ae-grace",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "AAPL"},
            },
            caller_identity={"workspace": "ws-ae-grace"},
        ),
        timeout=2.0,
    )
    assert ae_started.is_set()
    assert ae_cancelled.is_set()
    assert ae_cleaned_up.is_set()
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["status"] == "partial_error"
    assert payload["partial"] == ["local_all", "guidepoint"]
    assert payload["source_status"]["alphaengine"]["status"] == "timeout"
    # No orphan runner or leaked global slot remains.
    assert bridge._get_concurrency().status_snapshot()["global_inflight"] == 0
    assert bridge._count_orphan_upstream() == 0


@pytest.mark.asyncio
async def test_fanout_grace_keeps_alphaengine_singleflight_when_other_waiter_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        fanout_grace_s=0.05,
    )
    ae_started = asyncio.Event()
    release_ae = asyncio.Event()
    ae_calls = 0

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal ae_calls
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route == "all":
            return [
                _mcp_reply(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "results": [{"document_id": "wiki:doc-2", "title": "Local"}],
                        "result_count": 1,
                    },
                )
            ]
        if route == "guidepoint":
            return [
                _mcp_reply(
                    req["id"],
                    {"ok": True, "data": {"structuredContent": {"data": [{"id": "gp-2"}]}}},
                )
            ]
        if route == "alphaengine":
            ae_calls += 1
            ae_started.set()
            await release_ae.wait()
            return [_mcp_reply(req["id"], _ae_results_payload(2))]
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "fanout-leader",
            {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "search",
                    "query": "MSFT",
                    "limit": 10,
                },
            },
            caller_identity={"workspace": "ws-fanout-a"},
        )
    )
    await asyncio.wait_for(ae_started.wait(), timeout=2.0)

    # Second caller joins the same singleflight (limit=10 matches fanout normalized args).
    joiner_task = asyncio.create_task(
        bridge.handle_tools_call(
            "direct-joiner",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "MSFT",
                    "limit": 10,
                },
            },
            caller_identity={"workspace": "ws-direct-b"},
        )
    )
    # Wait for fanout grace to cancel the fanout's AE leg while joiner still waits.
    fanout_resp = await asyncio.wait_for(fanout_task, timeout=2.0)
    fanout_payload = json.loads(fanout_resp["result"]["content"][0]["text"])
    assert fanout_payload["source_status"]["alphaengine"]["status"] == "timeout"
    assert not joiner_task.done()
    assert bridge._count_orphan_upstream() == 0

    # Release upstream -> joiner gets shared result with only 1 AE upstream call.
    release_ae.set()
    joiner_resp = await asyncio.wait_for(joiner_task, timeout=2.0)
    joiner_payload = json.loads(joiner_resp["result"]["content"][0]["text"])
    assert joiner_payload["ok"] is True
    assert ae_calls == 1
    assert bridge._get_concurrency().status_snapshot()["global_inflight"] == 0
    assert bridge._count_orphan_upstream() == 0


def test_old_supervisor_without_v2_falls_back_to_direct_http_with_signed_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcr, "_supervisor_unavailable", False)
    monkeypatch.setattr(mcr, "_supervisor_caller_cap_cache", {})
    monkeypatch.setattr(mcr, "_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(mcr, "_INTERNAL_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(
        mcr,
        "_WORKSPACE_VIEW_CACHE",
        {"view": {"workspace_id": "ws1", "servers": {"researchdata": {}}}},
    )
    monkeypatch.setattr(
        mcr,
        "_SERVER_CONFIGS",
        {
            "researchdata": mcr._normalize(
                "researchdata",
                {"transport": "http", "untrusted": False, "url": "http://127.0.0.1:8960/mcp"},
            )
        },
    )
    monkeypatch.delenv("MCP_SUPERVISOR", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "admin-secret-for-v2-test")
    with _bound_caller_identity(
        {
            "configurable": {
                "user_id": "u-v2",
                "workspace_id": "ws-v2",
                "thread_id": "th-v2",
                "turn_origin_automation_id": "auto-v2",
            }
        },
        "fallback",
    ):
        env_auto = caller_env()
    monkeypatch.setenv("DALTON_CALLER_TOKEN", env_auto["DALTON_CALLER_TOKEN"])
    monkeypatch.setattr(
        mcr, "_socket_alive", create_autospec(mcr._socket_alive, return_value=True)
    )

    sup_calls: list[dict[str, Any]] = []
    v2_enabled = {"value": False}

    def _exchange(path: str, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req.get("op") == "health":
            data: dict[str, Any] = {"ok": True, "caller_identity": True}
            if v2_enabled["value"]:
                data["caller_identity_v2"] = True
            return "result", {"type": "result", "data": data}
        sup_calls.append(dict(req))
        return "reply", {"type": "reply", "reply": {"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}}}

    monkeypatch.setattr(
        mcr, "_supervisor_exchange", create_autospec(mcr._supervisor_exchange, side_effect=_exchange)
    )
    http_callers: list[dict[str, str]] = []

    def _fake_http_exchange(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        caller: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        http_callers.append(dict(caller or {}))
        return {"result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}

    monkeypatch.setattr(
        mcr, "_http_exchange", create_autospec(mcr._http_exchange, side_effect=_fake_http_exchange)
    )

    # 1. Old supervisor (only caller_identity=True, no caller_identity_v2): falls back to direct HTTP with token.
    res_v1 = mcr._call_mcp_tool_dispatch("researchdata", "research", {"route": "alphaengine"})
    assert res_v1 == {"ok": True}
    assert sup_calls == []
    assert len(http_callers) == 1
    assert http_callers[0]["token"] == env_auto["DALTON_CALLER_TOKEN"]

    # 2. Upgraded supervisor (caller_identity_v2=True): forwards token through supervisor.
    mcr._supervisor_caller_cap_cache.clear()
    v2_enabled["value"] = True
    res_v2 = mcr._call_mcp_tool_dispatch("researchdata", "research", {"route": "alphaengine"})
    assert res_v2 == {"ok": True}
    assert len(sup_calls) == 1
    assert sup_calls[0]["caller"]["token"] == env_auto["DALTON_CALLER_TOKEN"]


@pytest.mark.asyncio
async def test_caller_queue_timeout_records_nonzero_queued_ms_and_healthz_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=1,
        automation_reserved=2,
    )
    assert not hasattr(bridge, "sem")

    hold_entered = asyncio.Event()
    release_hold = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        if doc_id == "hold":
            hold_entered.set()
            await release_hold.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    caller = {"workspace": "ws-q-timeout"}
    hold_task = asyncio.create_task(
        bridge.handle_tools_call("q-hold", _get_doc_call("hold"), caller_identity=caller)
    )
    await asyncio.wait_for(hold_entered.wait(), timeout=2.0)

    # 1. Direct limiter caller-slot timeout populates queued_ms > 0.
    limiter = bridge._get_concurrency()
    stats: dict[str, int] = {}
    with pytest.raises(asyncio.TimeoutError):
        async with limiter.acquire_call(
            "ws:ws-q-timeout",
            "ws:ws-q-ti",
            is_automation=False,
            timeout_s=0.04,
            stats=stats,
        ):
            pass
    assert stats["queued_ms"] >= 20

    # 2. handle_tools_call timeout on caller queue logs nonzero queued_ms.
    orig_compute_timeout = bridge.compute_timeout
    monkeypatch.setattr(
        bridge,
        "compute_timeout",
        create_autospec(bridge.compute_timeout, return_value=0.04),
    )
    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        timeout_resp = await asyncio.wait_for(
            bridge.handle_tools_call(
                "q-to", _get_doc_call("second"), caller_identity=caller
            ),
            timeout=2.0,
        )
    assert json.loads(timeout_resp["result"]["content"][0]["text"])["status"] == "timeout"
    timeout_logs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("tools/call timeout ")
    ]
    assert len(timeout_logs) == 1
    queued_ms_val = int(timeout_logs[0].split("queued_ms=", 1)[1].split(" ", 1)[0])
    assert queued_ms_val >= 20

    monkeypatch.setattr(bridge, "compute_timeout", orig_compute_timeout)
    release_hold.set()
    await asyncio.wait_for(hold_task, timeout=2.0)

    # 3. healthz reports orphan_upstream > 0 when a singleflight runner outlives a timed-out waiter.
    sf_started = asyncio.Event()
    sf_release = asyncio.Event()

    async def _sf_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        if requests[0].get("method") == "initialize":
            return [{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"version": "1.0.0"}}}]
        sf_started.set()
        await sf_release.wait()
        return [_mcp_reply(requests[0]["id"], _ae_results_payload(1))]

    rpc_double.side_effect = _sf_rpc
    monkeypatch.setattr(
        bridge,
        "probe_xreach_version",
        create_autospec(bridge.probe_xreach_version, return_value={"ok": True}),
    )
    monkeypatch.setattr(
        bridge,
        "probe_tmeet_version",
        create_autospec(bridge.probe_tmeet_version, return_value={"ok": True}),
    )
    orig_wait_for = asyncio.wait_for

    async def _short_wait(aw: Any, timeout: float | None = None) -> Any:
        return await orig_wait_for(aw, timeout=0.03)

    monkeypatch.setattr(asyncio, "wait_for", _short_wait)
    res_to = await orig_wait_for(
        bridge.handle_tools_call(
            "sf-to",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "ORPHAN",
                },
            },
        ),
        timeout=2.0,
    )
    monkeypatch.setattr(asyncio, "wait_for", orig_wait_for)
    assert json.loads(res_to["result"]["content"][0]["text"])["status"] == "timeout"
    await orig_wait_for(sf_started.wait(), timeout=2.0)

    raw_req = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:8960\r\n\r\n"
    reader = asyncio.StreamReader()
    reader.feed_data(raw_req)
    reader.feed_eof()
    written_chunks: list[bytes] = []
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    writer.write.side_effect = lambda b: written_chunks.append(b)
    await orig_wait_for(rb._handle_client(bridge, reader, writer), timeout=2.0)
    health_body = json.loads(b"".join(written_chunks).decode("utf-8").split("\r\n\r\n", 1)[1])
    assert health_body["orphan_upstream"] == 1

    sf_release.set()
    await bridge.close()
    assert bridge._count_orphan_upstream() == 0


@pytest.mark.asyncio
async def test_queued_cancellation_and_timeout_never_leak_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=1,
        automation_reserved=2,
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        if doc_id == "hold":
            first_entered.set()
            await release_first.wait()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc)
    rpc_double.side_effect = _fake_rpc
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    caller = {"workspace": "ws-cancel-1"}
    t_hold = asyncio.create_task(
        bridge.handle_tools_call("h", _get_doc_call("hold"), caller_identity=caller)
    )
    await asyncio.wait_for(first_entered.wait(), timeout=2.0)

    t_cancel = asyncio.create_task(
        bridge.handle_tools_call("c", _get_doc_call("cancel-me"), caller_identity=caller)
    )
    await asyncio.sleep(0.02)
    t_cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t_cancel, timeout=1.0)

    # Timed-out acquire on queued caller slot also cleans up cleanly.
    limiter = bridge._get_concurrency()
    with pytest.raises(asyncio.TimeoutError):
        async with limiter.acquire_call(
            "ws:ws-cancel-1", "ws:ws-cance", is_automation=False, timeout_s=0.02
        ):
            pass

    release_first.set()
    await asyncio.wait_for(t_hold, timeout=2.0)

    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }
    res = await asyncio.wait_for(
        bridge.handle_tools_call("after", _get_doc_call("ok"), caller_identity=caller),
        timeout=2.0,
    )
    assert json.loads(res["result"]["content"][0]["text"])["ok"] is True


@pytest.mark.asyncio
async def test_unsigned_callers_share_single_bucket_and_leave_room_for_other_signed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import time as _time
    from src.dalton.sandbox_caller_identity import sign_caller_token

    shared_secret = "unsigned-cap-test-secret"
    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        bridge = _make_bridge(
            tmp_path,
            monkeypatch,
            max_inflight=6,
            max_inflight_per_caller=2,
            automation_reserved=2,
            admin_token=shared_secret,
        )
    assert any(
        "unsigned" in rec.getMessage() and "automation_reserved=2" in rec.getMessage()
        for rec in caplog.records
    )

    signed_a_entered = 0
    signed_a_two_entered = asyncio.Event()
    unsigned_one_entered = asyncio.Event()
    release_first_wave = asyncio.Event()
    signed_b_entered = asyncio.Event()
    auto_entered = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal signed_a_entered
        req = requests[0]
        doc_id = str(req["params"]["arguments"].get("params", {}).get("doc_id") or "")
        if doc_id.startswith("signed-a-"):
            signed_a_entered += 1
            if signed_a_entered >= 2:
                signed_a_two_entered.set()
            await release_first_wave.wait()
        elif doc_id.startswith("unsigned-"):
            unsigned_one_entered.set()
            await release_first_wave.wait()
        elif doc_id == "signed-b-1":
            signed_b_entered.set()
        elif doc_id == "auto-1":
            auto_entered.set()
        return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    exp_ts = int(_time.time()) + 1800
    tok_ws_a = sign_caller_token(
        {"user": "u-a", "workspace": "ws-signed-a", "thread": "t-a"},
        exp=exp_ts,
        admin_token=shared_secret,
    )
    tok_ws_b = sign_caller_token(
        {"user": "u-b", "workspace": "ws-signed-b", "thread": "t-b"},
        exp=exp_ts,
        admin_token=shared_secret,
    )
    tok_auto = sign_caller_token(
        {
            "user": "u-auto",
            "workspace": "ws-auto",
            "thread": "t-auto",
            "run_origin": "automation",
        },
        exp=exp_ts,
        admin_token=shared_secret,
    )
    assert tok_ws_a and tok_ws_b and tok_auto

    # 2 signed requests from ws-signed-a + 4 unsigned requests across distinct peer IPs arrive together.
    first_wave = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"sa-{i}",
                _get_doc_call(f"signed-a-{i}"),
                caller_identity={"token": tok_ws_a},
                conn_id="10.0.0.10",
            )
        )
        for i in range(2)
    ] + [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"un-{i}",
                _get_doc_call(f"unsigned-{i}"),
                caller_identity=(
                    {"user": "spoof", "workspace": "ws-signed-a", "run_origin": "automation", "_caller_verified": False}
                    if i == 0
                    else None
                ),
                conn_id=f"10.0.0.{20 + i}",
            )
        )
        for i in range(4)
    ]

    await asyncio.wait_for(
        asyncio.gather(signed_a_two_entered.wait(), unsigned_one_entered.wait()),
        timeout=2.0,
    )
    await asyncio.sleep(0.02)

    limiter = bridge._get_concurrency()
    snap = limiter.status_snapshot()
    # 2 from ws-signed-a + 1 from shared unsigned bucket = 3 interactive inflight (<= 3); 3 unsigned queued.
    assert limiter._interactive_inflight <= 3
    assert limiter._interactive_inflight == 3
    assert snap["global_inflight"] == 3
    assert snap["callers"]["ws:ws-signe"] == {"inflight": 2, "queued": 0}
    assert snap["callers"]["unsigned"] == {"inflight": 1, "queued": 3}

    # Another signed workspace (ws-signed-b) and an automation call both still obtain slots immediately!
    res_b, res_auto = await asyncio.wait_for(
        asyncio.gather(
            bridge.handle_tools_call(
                "sb-1",
                _get_doc_call("signed-b-1"),
                caller_identity={"token": tok_ws_b},
                conn_id="10.0.0.10",
            ),
            bridge.handle_tools_call(
                "auto-1",
                _get_doc_call("auto-1"),
                caller_identity={"token": tok_auto},
                conn_id="10.0.0.10",
            ),
        ),
        timeout=2.0,
    )
    assert signed_b_entered.is_set()
    assert auto_entered.is_set()
    assert json.loads(res_b["result"]["content"][0]["text"])["ok"] is True
    assert json.loads(res_auto["result"]["content"][0]["text"])["ok"] is True

    release_first_wave.set()
    wave_resps = await asyncio.wait_for(asyncio.gather(*first_wave), timeout=2.0)
    assert all(
        json.loads(r["result"]["content"][0]["text"])["ok"] is True for r in wave_resps
    )
    assert limiter._global_inflight == 0
    assert limiter._interactive_inflight == 0


@pytest.mark.asyncio
async def test_malformed_caller_token_in_http_handler_degrades_without_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch, admin_token="secret-for-malformed-test")
    monkeypatch.setattr(
        bridge,
        "_run_stdio_rpc",
        create_autospec(
            bridge._run_stdio_rpc,
            side_effect=lambda reqs, timeout_s: [
                _mcp_reply(reqs[0]["id"], {"ok": True, "text": "ok"})
            ],
        ),
    )
    seen_routing: list[tuple[str, bool]] = []
    orig_handle = bridge.handle_tools_call

    async def _spy_handle(
        req_id, params, *, caller_identity=None, conn_id=None, raw_caller_token=None
    ):
        ckey, _, is_auto = rb._resolve_caller_routing(caller_identity, conn_id=conn_id or "")
        seen_routing.append((ckey, is_auto))
        return await orig_handle(
            req_id,
            params,
            caller_identity=caller_identity,
            conn_id=conn_id,
            raw_caller_token=raw_caller_token,
        )

    monkeypatch.setattr(bridge, "handle_tools_call", _spy_handle)

    malformed_tokens = [
        "中文payload.AAAA",
        "e30.签名非ascii",
        "not-a-dot-token",
        ".",
        "bad!@#$.bad!@#$",
        "===.===",
    ]
    for idx, bad_tok in enumerate(malformed_tokens):
        assert rb.verify_caller_token(bad_tok, "secret-for-malformed-test") is None
        body_bytes = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": f"mal-{idx}",
                "method": "tools/call",
                "params": _get_doc_call(f"d-{idx}"),
            }
        ).encode("utf-8")
        raw_http = (
            "POST /mcp HTTP/1.1\r\n"
            "Host: 127.0.0.1:8960\r\n"
            "Authorization: Bearer test-token\r\n"
            "X-Dalton-Caller-Scope: per-request\r\n"
            f"X-Dalton-Caller-Token: {bad_tok}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n\r\n"
        ).encode("utf-8") + body_bytes
        reader = asyncio.StreamReader()
        reader.feed_data(raw_http)
        reader.feed_eof()
        written_chunks: list[bytes] = []
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        writer.write.side_effect = lambda b: written_chunks.append(b)
        await asyncio.wait_for(rb._handle_client(bridge, reader, writer), timeout=2.0)

        raw_resp = b"".join(written_chunks).decode("utf-8")
        status_line, resp_body = raw_resp.split("\r\n\r\n", 1)
        assert status_line.startswith("HTTP/1.1 200 OK")
        rpc_resp = json.loads(resp_body)
        assert json.loads(rpc_resp["result"]["content"][0]["text"])["ok"] is True

    assert seen_routing == [("unsigned", False)] * len(malformed_tokens)


@pytest.mark.asyncio
async def test_acquire_global_timeout_records_nonzero_queued_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=1,
        max_inflight_per_caller=1,
        automation_reserved=0,
    )
    limiter = bridge._get_concurrency()

    # 1. Direct acquire_global() timeout populates stats["queued_ms"] > 0 and leaks no slots.
    stats: dict[str, int] = {}
    async with limiter.acquire_global(is_automation=False, timeout_s=1.0):
        with pytest.raises(asyncio.TimeoutError):
            async with limiter.acquire_global(
                is_automation=False, timeout_s=0.04, stats=stats
            ):
                pass
    assert stats["queued_ms"] >= 20
    assert limiter._global_inflight == 0
    assert limiter._interactive_inflight == 0
    assert limiter._global_waiting == 0

    # 2. End-to-end route='all' fan-out hitting global queue timeout logs nonzero queued_ms.
    hold_entered = asyncio.Event()
    release_hold = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        if args.get("operation") == "get_document":
            hold_entered.set()
            await release_hold.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": "held"})]
        return [_mcp_reply(req["id"], {"ok": True, "status": "ok", "results": []})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    hold_task = asyncio.create_task(
        bridge.handle_tools_call(
            "g-hold",
            _get_doc_call("hold"),
            caller_identity={"workspace": "ws-global-holder"},
        )
    )
    await asyncio.wait_for(hold_entered.wait(), timeout=2.0)

    monkeypatch.setattr(
        bridge,
        "compute_timeout",
        create_autospec(bridge.compute_timeout, return_value=0.04),
    )
    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        timeout_resp = await asyncio.wait_for(
            bridge.handle_tools_call(
                "g-to",
                {
                    "name": "research",
                    "arguments": {"route": "all", "operation": "search", "query": "NVDA"},
                },
                caller_identity={"workspace": "ws-global-waiter"},
            ),
            timeout=2.0,
        )
    assert json.loads(timeout_resp["result"]["content"][0]["text"])["status"] == "timeout"
    timeout_logs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("tools/call ") and "status=timeout" in r.getMessage()
    ]
    assert len(timeout_logs) == 1
    queued_ms_val = int(timeout_logs[0].split("queued_ms=", 1)[1].split(" ", 1)[0])
    assert queued_ms_val >= 20

    release_hold.set()
    await asyncio.wait_for(hold_task, timeout=2.0)
    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 0,
        "callers": {},
    }


@pytest.mark.asyncio
async def test_unsigned_fanout_does_not_fill_interactive_cap_and_workspace_b_runs_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time
    from src.dalton.sandbox_caller_identity import sign_caller_token

    shared_secret = "unsigned-fanout-repro-secret"
    # Default knobs: max_inflight=6, max_inflight_per_caller=2, max_inflight_unsigned=1, automation_reserved=2 -> interactive_cap=4
    bridge = _make_bridge(tmp_path, monkeypatch, admin_token=shared_secret)

    ws_a_entered = 0
    ws_a_two_entered = asyncio.Event()
    release_ws_a = asyncio.Event()
    unsigned_first_entered = asyncio.Event()
    release_unsigned_legs = asyncio.Event()
    unsigned_inflight_cur = 0
    unsigned_inflight_max = 0
    ws_b_entered = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal ws_a_entered, unsigned_inflight_cur, unsigned_inflight_max
        req = requests[0]
        args = req["params"]["arguments"]
        route = args.get("route")
        op = args.get("operation")
        doc_id = str(args.get("params", {}).get("doc_id") or "")
        if op == "get_document" and doc_id.startswith("ws-a-"):
            ws_a_entered += 1
            if ws_a_entered >= 2:
                ws_a_two_entered.set()
            await release_ws_a.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]
        if op == "get_document" and doc_id == "ws-b-1":
            ws_b_entered.set()
            return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]
        if route in ("all", "alphaengine", "guidepoint"):
            unsigned_inflight_cur += 1
            unsigned_inflight_max = max(unsigned_inflight_max, unsigned_inflight_cur)
            try:
                unsigned_first_entered.set()
                await release_unsigned_legs.wait()
                if route == "all":
                    return [
                        _mcp_reply(
                            req["id"],
                            {
                                "ok": True,
                                "status": "ok",
                                "results": [{"document_id": "wiki:u-1", "title": "Local"}],
                                "result_count": 1,
                            },
                        )
                    ]
                if route == "alphaengine":
                    return [_mcp_reply(req["id"], _ae_results_payload(1, prefix="7700"))]
                return [
                    _mcp_reply(
                        req["id"],
                        {"ok": True, "data": {"structuredContent": {"data": [{"id": "gp-u1"}]}}},
                    )
                ]
            finally:
                unsigned_inflight_cur -= 1
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    exp_ts = int(_time.time()) + 1800
    tok_ws_a = sign_caller_token(
        {"user": "u-a", "workspace": "ws-alpha-a", "thread": "t-a"},
        exp=exp_ts,
        admin_token=shared_secret,
    )
    tok_ws_b = sign_caller_token(
        {"user": "u-b", "workspace": "ws-beta-b", "thread": "t-b"},
        exp=exp_ts,
        admin_token=shared_secret,
    )
    assert tok_ws_a and tok_ws_b

    # Workspace A holds 2 signed requests + 1 unsigned route='all' request runs concurrently.
    ws_a_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"a-{i}",
                _get_doc_call(f"ws-a-{i}"),
                caller_identity={"token": tok_ws_a},
            )
        )
        for i in range(2)
    ]
    unsigned_fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "un-fanout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "NVDA"},
            },
            caller_identity=None,
            conn_id="10.0.0.88",
        )
    )
    await asyncio.wait_for(
        asyncio.gather(ws_a_two_entered.wait(), unsigned_first_entered.wait()),
        timeout=2.0,
    )
    await asyncio.sleep(0.02)

    limiter = bridge._get_concurrency()
    snap = limiter.status_snapshot()
    # 2 from ws-a + 1 from unsigned (capped at 1) = 3 interactive slots (< 4); excess legs queue in unsigned's own queue.
    assert unsigned_inflight_max == 1
    assert limiter._interactive_inflight == 3
    assert snap["global_inflight"] == 3
    assert snap["global_queued"] == 0
    assert snap["callers"]["ws:ws-alpha"] == {"inflight": 2, "queued": 0}
    assert snap["callers"]["unsigned"] == {"inflight": 1, "queued": 2}

    # Workspace B must obtain the 4th interactive slot and finish immediately!
    ws_b_resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "b-1",
            _get_doc_call("ws-b-1"),
            caller_identity={"token": tok_ws_b},
        ),
        timeout=1.0,
    )
    assert ws_b_entered.is_set()
    assert json.loads(ws_b_resp["result"]["content"][0]["text"])["ok"] is True

    release_unsigned_legs.set()
    release_ws_a.set()
    fanout_resp, *a_resps = await asyncio.wait_for(
        asyncio.gather(unsigned_fanout_task, *ws_a_tasks),
        timeout=2.0,
    )
    assert unsigned_inflight_max == 1
    fanout_payload = json.loads(fanout_resp["result"]["content"][0]["text"])
    assert fanout_payload["ok"] is True
    assert fanout_payload["source_status"]["local_all"]["ok"] is True
    assert fanout_payload["source_status"]["alphaengine"]["ok"] is True
    assert fanout_payload["source_status"]["guidepoint"]["ok"] is True
    assert all(
        json.loads(r["result"]["content"][0]["text"])["ok"] is True for r in a_resps
    )
    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }


@pytest.mark.asyncio
async def test_signed_workspace_fanout_respects_per_caller_cap_cache_hit_and_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time
    from src.dalton.sandbox_caller_identity import sign_caller_token

    shared_secret = "signed-fanout-cap-secret"
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        automation_reserved=2,
        admin_token=shared_secret,
    )

    two_entered = asyncio.Event()
    release_first_two = asyncio.Event()
    cur_inflight = 0
    max_inflight_seen = 0
    entered_routes: list[str] = []
    ae_rpc_calls = 0

    hold_inter_entered = 0
    four_inter_entered = asyncio.Event()
    release_four_inter = asyncio.Event()
    auto_two_entered = asyncio.Event()
    release_auto = asyncio.Event()
    auto_cur = 0
    auto_max = 0

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal cur_inflight, max_inflight_seen, ae_rpc_calls
        nonlocal hold_inter_entered, auto_cur, auto_max
        req = requests[0]
        args = req["params"]["arguments"]
        route = args.get("route")
        op = args.get("operation")
        doc_id = str(args.get("params", {}).get("doc_id") or "")
        query = str(args.get("query") or "")

        if op == "get_document" and doc_id.startswith("fill-inter-"):
            hold_inter_entered += 1
            if hold_inter_entered >= 4:
                four_inter_entered.set()
            await release_four_inter.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

        if op == "get_document" and doc_id == "hold-for-cache":
            await release_first_two.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": doc_id})]

        if query == "AUTO-FANOUT":
            auto_cur += 1
            auto_max = max(auto_max, auto_cur)
            try:
                if auto_cur >= 2:
                    auto_two_entered.set()
                await release_auto.wait()
                if route == "all":
                    return [
                        _mcp_reply(
                            req["id"],
                            {"ok": True, "status": "ok", "results": [{"document_id": "wiki:a1"}], "result_count": 1},
                        )
                    ]
                if route == "alphaengine":
                    return [_mcp_reply(req["id"], _ae_results_payload(1, prefix="6600"))]
                return [
                    _mcp_reply(
                        req["id"],
                        {"ok": True, "data": {"structuredContent": {"data": [{"id": "gp-a1"}]}}},
                    )
                ]
            finally:
                auto_cur -= 1

        cur_inflight += 1
        max_inflight_seen = max(max_inflight_seen, cur_inflight)
        entered_routes.append(str(route))
        try:
            if query == "AAPL-CAP":
                if cur_inflight >= 2:
                    two_entered.set()
                if not release_first_two.is_set():
                    await release_first_two.wait()
            if route == "all":
                return [
                    _mcp_reply(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "results": [{"document_id": "wiki:s1", "title": "Local"}],
                            "result_count": 1,
                        },
                    )
                ]
            if route == "alphaengine":
                ae_rpc_calls += 1
                return [_mcp_reply(req["id"], _ae_results_payload(1, prefix="8800"))]
            return [
                _mcp_reply(
                    req["id"],
                    {"ok": True, "data": {"structuredContent": {"data": [{"id": "gp-s1"}]}}},
                )
            ]
        finally:
            cur_inflight -= 1

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    exp_ts = int(_time.time()) + 1800
    tok_ws = sign_caller_token(
        {"user": "u-s", "workspace": "ws-cap-signed", "thread": "t-s"},
        exp=exp_ts,
        admin_token=shared_secret,
    )
    tok_auto = sign_caller_token(
        {
            "user": "u-auto",
            "workspace": "ws-auto-fanout",
            "thread": "t-auto",
            "run_origin": "automation",
        },
        exp=exp_ts,
        admin_token=shared_secret,
    )
    assert tok_ws and tok_auto

    # 1. Single route='all' from signed workspace with cap=2: max 2 concurrent global slots, 3rd waits then completes.
    fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "s-fanout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "AAPL-CAP"},
            },
            caller_identity={"token": tok_ws},
        )
    )
    await asyncio.wait_for(two_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    limiter = bridge._get_concurrency()
    snap = limiter.status_snapshot()
    assert max_inflight_seen == 2
    assert snap["global_inflight"] == 2
    assert snap["global_queued"] == 0
    assert snap["callers"]["ws:ws-cap-s"] == {"inflight": 2, "queued": 1}

    release_first_two.set()
    fanout_resp = await asyncio.wait_for(fanout_task, timeout=2.0)
    assert max_inflight_seen == 2
    fanout_payload = json.loads(fanout_resp["result"]["content"][0]["text"])
    assert fanout_payload["ok"] is True
    assert fanout_payload["source_status"]["local_all"]["ok"] is True
    assert fanout_payload["source_status"]["alphaengine"]["ok"] is True
    assert fanout_payload["source_status"]["guidepoint"]["ok"] is True
    assert ae_rpc_calls == 1

    # 2. Repeat same query: AE leg hits cache -> occupies 0 slots! Even with 1 get_document holding a slot,
    #    both local and guidepoint fit in the 2-slot cap? Better: with cap=2, 1 get_document + local + gp
    #    still keeps max concurrent <= 2 and AE hits cache without an upstream call.
    max_inflight_seen = 0
    cur_inflight = 0
    release_first_two.clear()
    hold_doc_task = asyncio.create_task(
        bridge.handle_tools_call(
            "hold-doc",
            _get_doc_call("hold-for-cache"),
            caller_identity={"token": tok_ws},
        )
    )
    await asyncio.sleep(0.02)
    cached_fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "s-fanout-hit",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "AAPL-CAP"},
            },
            caller_identity={"token": tok_ws},
        )
    )
    await asyncio.sleep(0.03)
    release_first_two.set()
    _, cached_resp = await asyncio.wait_for(
        asyncio.gather(hold_doc_task, cached_fanout_task), timeout=2.0
    )
    cached_payload = json.loads(cached_resp["result"]["content"][0]["text"])
    assert cached_payload["ok"] is True
    assert cached_payload["alphaengine"]["_bridge_cache"]["status"] == "hit"
    assert ae_rpc_calls == 1  # No extra upstream AE call on cache hit.

    # 3. Automation reservation preserved: 4 interactive slots full, automation route='all' runs in 2 reserved slots (max 2 concurrent).
    inter_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"fi-{i}",
                _get_doc_call(f"fill-inter-{i}"),
                caller_identity={"workspace": f"ws-fill-{i}"},
            )
        )
        for i in range(4)
    ]
    await asyncio.wait_for(four_inter_entered.wait(), timeout=2.0)

    auto_fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "auto-fanout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "AUTO-FANOUT"},
            },
            caller_identity={"token": tok_auto},
        )
    )
    await asyncio.wait_for(auto_two_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    assert auto_max == 2
    assert limiter._global_inflight == 6
    assert limiter._interactive_inflight == 4
    assert limiter.status_snapshot()["global_queued"] == 0
    assert limiter.status_snapshot()["callers"]["ws:ws-auto-"] == {"inflight": 2, "queued": 1}

    release_auto.set()
    auto_resp = await asyncio.wait_for(auto_fanout_task, timeout=2.0)
    assert json.loads(auto_resp["result"]["content"][0]["text"])["ok"] is True
    assert auto_max == 2

    release_four_inter.set()
    await asyncio.wait_for(asyncio.gather(*inter_tasks), timeout=2.0)
    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }


@pytest.mark.asyncio
async def test_fanout_branch_cancellation_and_timeout_do_not_leak_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        max_inflight=6,
        max_inflight_per_caller=2,
        automation_reserved=2,
    )
    two_entered = asyncio.Event()
    entered_count = 0
    release_slow = asyncio.Event()

    async def _fake_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        nonlocal entered_count
        req = requests[0]
        args = req["params"]["arguments"]
        op = args.get("operation")
        if op == "get_document":
            entered_count += 1
            if entered_count >= 2:
                two_entered.set()
            await release_slow.wait()
            return [_mcp_reply(req["id"], {"ok": True, "text": "doc"})]
        entered_count += 1
        if entered_count >= 2:
            two_entered.set()
        await asyncio.sleep(5.0)
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    limiter = bridge._get_concurrency()
    caller = {"workspace": "ws-branch-leak"}

    # A. Cancelling route='all' while 2 branches hold global slots and 1 branch waits in caller queue.
    fanout_task = asyncio.create_task(
        bridge.handle_tools_call(
            "cancel-fanout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "TSLA"},
            },
            caller_identity=caller,
        )
    )
    await asyncio.wait_for(two_entered.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    snap = limiter.status_snapshot()
    assert snap["global_inflight"] == 2
    assert snap["global_queued"] == 0
    assert snap["callers"]["ws:ws-branc"] == {"inflight": 2, "queued": 1}

    fanout_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(fanout_task, timeout=1.0)

    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }
    assert limiter._interactive_inflight == 0
    assert bridge._count_orphan_upstream() == 0

    # B. Timeout of a queued fan-out branch in caller's own queue does not leak slots.
    entered_count = 0
    two_entered.clear()
    hold_tasks = [
        asyncio.create_task(
            bridge.handle_tools_call(
                f"h-{i}", _get_doc_call(f"hold-{i}"), caller_identity=caller
            )
        )
        for i in range(2)
    ]
    await asyncio.wait_for(two_entered.wait(), timeout=2.0)

    gp_to_res = await asyncio.wait_for(
        bridge._run_guidepoint_fanout(
            "gp-branch-to",
            {"route": "guidepoint", "operation": "search_library", "query": "TSLA", "timeout": 1},
            0.04,
            caller_key="ws:ws-branch-leak",
            caller_display="ws:ws-branc",
            is_automation=False,
        ),
        timeout=1.0,
    )
    assert gp_to_res["status"] == "timeout"

    release_slow.set()
    await asyncio.wait_for(asyncio.gather(*hold_tasks), timeout=2.0)
    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }

    # C. End-to-end local branch timeout in route='all' cancels remaining queued/running branches cleanly.
    async def _fast_local_fail_rpc(
        requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        req = requests[0]
        route = req["params"]["arguments"].get("route")
        if route == "all":
            await asyncio.sleep(0.03)
            raise asyncio.TimeoutError()
        await asyncio.sleep(5.0)
        return [_mcp_reply(req["id"], {"ok": True})]

    rpc_double.side_effect = _fast_local_fail_rpc
    monkeypatch.setattr(
        bridge,
        "compute_timeout",
        create_autospec(bridge.compute_timeout, return_value=0.04),
    )
    to_resp = await asyncio.wait_for(
        bridge.handle_tools_call(
            "all-local-to",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "META"},
            },
            caller_identity=caller,
        ),
        timeout=2.0,
    )
    assert json.loads(to_resp["result"]["content"][0]["text"])["status"] == "timeout"
    assert limiter.status_snapshot() == {
        "global_inflight": 0,
        "global_queued": 0,
        "total_queued": 0,
        "effective_automation_reserved": 2,
        "callers": {},
    }
    assert limiter._interactive_inflight == 0
    assert bridge._count_orphan_upstream() == 0
