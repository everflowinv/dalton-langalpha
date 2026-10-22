"""Unit tests for the read-only `market-regime` route on the researchdata bridge, data map, skills, and prompts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

from src.dalton.data_map import builder as map_builder
from src.dalton.data_map import health as health_mod
from src.ptc_agent.agent.prompts.loader import PromptLoader

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SCRIPTS_DIR = REPO_ROOT / "scripts" / "dalton"


def _load_bridge_module() -> Any:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "researchdata_bridge_regime_test", SCRIPTS_DIR / "researchdata_bridge.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load_bridge_module()


def _make_fixture_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    ref_date: date = date(2026, 10, 22),
) -> tuple[Any, dict[str, Path]]:
    """Build a `ResearchDataBridge` wired strictly to static test fixtures copied into `tmp_path`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "data_sources.json").write_text(
        json.dumps(
            {
                "routes": [
                    {"id": "web-news", "kind": "agent_tool"},
                    {"id": "xueqiu", "kind": "skill"},
                ]
            }
        ),
        encoding="utf-8",
    )

    signals_path = tmp_path / "signals.json"
    crowding_path = tmp_path / "official-all.json"
    status_path = tmp_path / "pipeline_status.json"
    unwind_path = tmp_path / "quant_unwind_signals.csv"

    shutil.copyfile(FIXTURES_DIR / "signals.json", signals_path)
    shutil.copyfile(FIXTURES_DIR / "official-all.json", crowding_path)
    shutil.copyfile(FIXTURES_DIR / "pipeline_status.json", status_path)
    shutil.copyfile(FIXTURES_DIR / "quant_unwind_signals.csv", unwind_path)

    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(tmp_path / "nonexistent.env"))
    monkeypatch.setenv("DALTON_OCW_ROOT", str(tmp_path))
    monkeypatch.setenv("DALTON_SENTIMENT_SIGNALS_JSON", str(signals_path))
    monkeypatch.setenv("DALTON_INDEX_CROWDING_JSON", str(crowding_path))
    monkeypatch.setenv("DALTON_CN_LEVERAGE_STATUS_JSON", str(status_path))
    monkeypatch.setenv("DALTON_CN_LEVERAGE_UNWIND_CSV", str(unwind_path))

    cfg = rb.BridgeConfig()
    bridge = rb.ResearchDataBridge(cfg)

    mock_ref_date = create_autospec(bridge._reference_date, return_value=ref_date)
    monkeypatch.setattr(bridge, "_reference_date", mock_ref_date)

    # Guard against any accidental subprocess execution during market-regime calls
    mock_stdio = create_autospec(bridge._run_stdio_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_stdio)

    return bridge, {
        "signals": signals_path,
        "crowding": crowding_path,
        "status": status_path,
        "unwind": unwind_path,
    }


def _unwrap_rpc_result(rpc_reply: Mapping[str, Any]) -> dict[str, Any]:
    result_obj = rpc_reply["result"]
    text = result_obj["content"][0]["text"]
    return json.loads(text)


@pytest.mark.asyncio
async def test_market_regime_snapshot_and_us_topbottom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`snapshot` (default) returns <2KB digest with `momentum.gscbhmom`, `ndx_spw.rsi14`, stages, put_hedge, CN/HK/Asia readings, `as_of`, and `stale=False`; `us_topbottom` strips large arrays."""
    bridge, paths = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 22))
    assert bridge.cfg.sentiment_signals_json == paths["signals"]

    # 1. Default operation is `snapshot`
    reply = await bridge.handle_tools_call(
        1,
        {"name": "macro", "arguments": {"route": "market-regime"}},
    )
    assert reply["result"]["isError"] is False
    raw_text = reply["result"]["content"][0]["text"]
    assert len(raw_text.encode("utf-8")) < 2048

    payload = _unwrap_rpc_result(reply)
    assert payload["ok"] is True
    assert payload["operation"] == "snapshot"
    assert payload["date"] == "2026-10-22"
    assert payload["stale"] is False
    assert payload["as_of"]["us"] == "2026-10-21"
    assert payload["as_of"]["cn_crowding"] == "2026-10-21"
    assert payload["us_topbottom"]["top_framework"]["stage"] == "无信号"
    assert payload["us_topbottom"]["bottom_framework"]["stage"] == "预警"
    assert "RSI14≤32" in payload["us_topbottom"]["bottom_framework"]["summary"]
    assert payload["put_hedge"]["vvix"] == 100.32
    assert payload["momentum"]["reading"] == "GSCBHMOM RSI14 36.4"
    assert payload["momentum"]["gscbhmom"] == 36.43
    assert "rsi14" not in payload["momentum"]
    assert payload["ndx_spw"]["rsi14"] == 51.2
    assert "两融余额 RSI14 13.3" in payload["cn_a"]["reading"]
    assert payload["cn_a"]["stale"] is False
    assert "AH溢价 RSI30 -71.5" in payload["hk"]["reading"]
    assert payload["asia"]["reading"] == "韩国 20 · 日本 54 · 台湾 82"
    assert payload["warnings"] == []
    assert payload["_sources"][0]["route"] == "market-regime"
    assert payload["_sources"][0]["document_id"] == "regime:snapshot:2026-10-22"
    assert payload["_sources"][0]["publisher"] == "OpenClaw"
    assert payload["_sources"][0]["as_of"] == "2026-10-22"
    assert bridge._run_stdio_rpc.await_count == 0

    # 2. `us_topbottom` returns full frameworks without large arrays (`cycles`, `signals`)
    tb_reply = await bridge.handle_tools_call(
        2,
        {"name": "macro", "arguments": {"route": "market-regime", "operation": "us_topbottom"}},
    )
    tb = _unwrap_rpc_result(tb_reply)
    assert tb["ok"] is True
    assert tb["operation"] == "us_topbottom"
    assert "cycles" not in tb["top_framework"]
    assert "cycles" not in tb["bottom_framework"]
    assert "signals" not in tb["bottom_framework"]
    # Nested dict `environment.signals` is preserved
    assert tb["bottom_framework"]["environment"]["signals"] == {"vix_spike": False}
    assert tb["bottom_framework"]["latest_transition"]["date"] == "2026-10-14"
    assert tb["top_framework"]["put_hedge"]["vix"] == 18.23


@pytest.mark.asyncio
async def test_market_regime_crowding_and_cn_leverage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`crowding` supports `top` (clamped <=30), `order` (`high`/`low`), `category`, `query`, and omits `history_1y`; `cn_leverage` (<3KB) returns compact `pipeline_status` + 5-row `latest_unwind` and scrubs `/Users/` paths."""
    bridge, _ = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 22))

    # 1. Crowding high order, top clamped to 30 when 99 is passed
    high_reply = await bridge.handle_tools_call(
        10,
        {
            "name": "macro",
            "arguments": {
                "route": "market-regime",
                "operation": "crowding",
                "top": 99,
                "order": "high",
            },
        },
    )
    high_data = _unwrap_rpc_result(high_reply)
    assert high_data["ok"] is True
    assert high_data["stale"] is False
    assert high_data["top"] == 30
    assert high_data["trade_date"] == "2026-10-21"
    rows_high = high_data["rows"]
    assert len(rows_high) == 6
    assert [r["index_name"] for r in rows_high[:2]] == ["人工智能", "中证半导体"]
    assert all("history_1y" not in r for r in rows_high)

    # 2. Crowding low order with `category` and `query` filters
    low_reply = await bridge.handle_tools_call(
        11,
        {
            "name": "macro",
            "arguments": {
                "route": "market-regime",
                "operation": "crowding",
                "top": 2,
                "order": "low",
                "category": "宽基",
            },
        },
    )
    low_data = _unwrap_rpc_result(low_reply)
    assert [r["index_name"] for r in low_data["rows"]] == ["国证2000", "中证500"]

    q_reply = await bridge.handle_tools_call(
        12,
        {
            "name": "macro",
            "arguments": {
                "route": "market-regime",
                "operation": "crowding",
                "query": "半导体",
            },
        },
    )
    q_data = _unwrap_rpc_result(q_reply)
    assert len(q_data["rows"]) == 1
    assert q_data["rows"][0]["index_code"] == "H30184.CSI"
    assert "history_1y" not in q_data["rows"][0]

    # 3. `cn_leverage`: compact pipeline_status + 5-row `latest_unwind` (<3KB), `completed` is success, `/Users/` scrubbed
    lev_reply = await bridge.handle_tools_call(
        13,
        {"name": "macro", "arguments": {"route": "market-regime", "operation": "cn_leverage"}},
    )
    lev_raw_text = lev_reply["result"]["content"][0]["text"]
    assert len(lev_raw_text.encode("utf-8")) < 3072
    assert "/Users/" not in lev_raw_text

    lev_data = _unwrap_rpc_result(lev_reply)
    assert lev_data["ok"] is True
    assert lev_data["stale"] is False
    assert lev_data["as_of"] == "2026-10-21"
    assert "unwind_signals" not in lev_data
    assert "rows" not in lev_data

    # Pipeline status is 1-line summary per pipeline (name/status/updated)
    pipe_by_name = {p["name"]: p for p in lev_data["pipeline_status"]}
    assert pipe_by_name["两融核心"]["status"] == "completed"
    assert "detail" not in pipe_by_name["两融核心"]
    assert pipe_by_name["北向资金"]["status"] == "failed"

    # Only non-completed/ok/success pipelines enter warnings, with `/Users/...` replaced by `<local>`
    assert len(lev_data["warnings"]) == 1
    assert "北向资金" in lev_data["warnings"][0]
    assert "<local>" in lev_data["warnings"][0]
    assert "/Users/" not in lev_data["warnings"][0]
    assert not any("两融核心" in w for w in lev_data["warnings"])

    unwind_rows = lev_data["latest_unwind"]
    assert len(unwind_rows) == 5
    assert [r["date"] for r in unwind_rows] == [
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
    ]
    latest_row = unwind_rows[-1]
    assert latest_row["date"] == "2026-10-21"
    assert latest_row["unwind_pressure_score"] == 52.14
    assert latest_row["unwind_direction"] == "balanced"
    assert latest_row["regime"] == "normal_or_Expanding"
    assert "extra_ignored_col" not in latest_row


@pytest.mark.asyncio
async def test_market_regime_holidays_and_us_cn_staleness_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifies NYSE holiday (Thanksgiving), CN fixed holiday (Oct 1), and that A-share staleness does not drag down US `snapshot`/`us_topbottom`."""
    bridge, paths = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 22))

    # 1. A-share `cn_crowding` is stale (2026-10-15), while US `signals.json` `date` is fresh (2026-10-22):
    #    - `us_topbottom` has `stale=False` and no CN warning
    #    - `snapshot` has top-level `stale=False`, while `cn_a.stale=True`
    sig_obj = json.loads(paths["signals"].read_text(encoding="utf-8"))
    sig_obj["date"] = "2026-10-22"
    sig_obj["as_of"]["cn_crowding"] = "2026-10-15"
    paths["signals"].write_text(json.dumps(sig_obj), encoding="utf-8")
    os.utime(paths["signals"], ns=(paths["signals"].stat().st_mtime_ns + 1_000_000,) * 2)

    tb_res = _unwrap_rpc_result(
        await bridge.handle_tools_call(
            15,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "us_topbottom"}},
        )
    )
    assert tb_res["stale"] is False
    assert tb_res["warnings"] == []

    snap_res = _unwrap_rpc_result(
        await bridge.handle_tools_call(
            16,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
        )
    )
    assert snap_res["stale"] is False
    assert snap_res["cn_a"]["stale"] is True
    assert any("cn_crowding" in w for w in snap_res["warnings"])

    # 2. US NYSE Holiday — Thanksgiving (Thursday 2026-11-26):
    #    Latest US trading day is Wednesday 2026-11-25 -> `date="2026-11-25"` is NOT stale on 2026-11-26!
    bridge_tg, paths_tg = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 11, 26))
    sig_tg = json.loads(paths_tg["signals"].read_text(encoding="utf-8"))
    sig_tg["date"] = "2026-11-25"
    sig_tg["as_of"]["cn_crowding"] = "2026-11-25"
    sig_tg["as_of"]["cn_margin"] = "2026-11-25"
    paths_tg["signals"].write_text(json.dumps(sig_tg), encoding="utf-8")
    os.utime(paths_tg["signals"], ns=(paths_tg["signals"].stat().st_mtime_ns + 2_000_000,) * 2)

    snap_tg = _unwrap_rpc_result(
        await bridge_tg.handle_tools_call(
            17,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
        )
    )
    assert snap_tg["stale"] is False
    assert snap_tg["cn_a"]["stale"] is False

    # 3. CN National Day Holiday — October 1 (Thursday 2026-10-01) and October 5 (Monday 2026-10-05):
    #    Oct 1-3 are in `_cn_fixed_holidays(2026)`.
    #    On 2026-10-01, latest CN trading day is 2026-09-30 and minus 1 is 2026-09-29.
    #    On 2026-10-05, latest CN trading day is 2026-10-05 and minus 1 skips Oct 1-3 to 2026-09-30.
    bridge_oct1, paths_oct1 = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 1))
    crowd_oct1 = json.loads(paths_oct1["crowding"].read_text(encoding="utf-8"))
    crowd_oct1["trade_date"] = "2026-09-29"
    paths_oct1["crowding"].write_text(json.dumps(crowd_oct1), encoding="utf-8")
    os.utime(paths_oct1["crowding"], ns=(paths_oct1["crowding"].stat().st_mtime_ns + 3_000_000,) * 2)

    res_oct1 = _unwrap_rpc_result(
        await bridge_oct1.handle_tools_call(
            18,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "crowding"}},
        )
    )
    assert res_oct1["stale"] is False

    bridge_oct5, paths_oct5 = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 5))
    crowd_oct5 = json.loads(paths_oct5["crowding"].read_text(encoding="utf-8"))
    crowd_oct5["trade_date"] = "2026-09-30"
    paths_oct5["crowding"].write_text(json.dumps(crowd_oct5), encoding="utf-8")
    os.utime(paths_oct5["crowding"], ns=(paths_oct5["crowding"].stat().st_mtime_ns + 4_000_000,) * 2)

    res_oct5 = _unwrap_rpc_result(
        await bridge_oct5.handle_tools_call(
            19,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "crowding"}},
        )
    )
    assert res_oct5["stale"] is False


@pytest.mark.asyncio
async def test_market_regime_staleness_thread_cache_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifies staleness detection, `asyncio.to_thread` + mtime cache, and `source_missing` / `invalid_source` errors."""
    # Advance reference date to Friday 2026-10-23:
    # - Latest US trading day = 2026-10-23 (signals.json date 2026-10-22 -> stale=True)
    # - Latest A-share trading day minus 1 = 2026-10-22 (crowding trade_date 2026-10-21 -> stale=True)
    bridge, paths = _make_fixture_bridge(monkeypatch, tmp_path, ref_date=date(2026, 10, 23))

    with patch.object(asyncio, "to_thread", wraps=asyncio.to_thread, autospec=True) as spy_to_thread:
        snap1 = _unwrap_rpc_result(
            await bridge.handle_tools_call(
                20,
                {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
            )
        )
        assert spy_to_thread.call_count == 1
        assert snap1["stale"] is True
        assert snap1["cn_a"]["stale"] is True
        assert any("2026-10-22" in w and "2026-10-23" in w for w in snap1["warnings"])

        crowd1 = _unwrap_rpc_result(
            await bridge.handle_tools_call(
                21,
                {"name": "macro", "arguments": {"route": "market-regime", "operation": "crowding"}},
            )
        )
        assert spy_to_thread.call_count == 2
        assert crowd1["stale"] is True
        assert any("2026-10-21" in w and "2026-10-22" in w for w in crowd1["warnings"])

    # Second call with unchanged mtime hits in-process cache without reading `signals.json` again
    orig_read_text = Path.read_text
    with patch.object(Path, "read_text", wraps=orig_read_text, autospec=True) as spy_read_text:
        snap2 = _unwrap_rpc_result(
            await bridge.handle_tools_call(
                22,
                {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
            )
        )
        assert snap2["ok"] is True
        read_paths = [c.args[0] for c in spy_read_text.call_args_list]
        assert paths["signals"] not in read_paths

    # Updating file mtime/content invalidates cache
    updated_signals = json.loads(paths["signals"].read_text(encoding="utf-8"))
    updated_signals["date"] = "2026-10-23"
    updated_signals["as_of"]["cn_crowding"] = "2026-10-22"
    updated_signals["as_of"]["cn_margin"] = "2026-10-22"
    paths["signals"].write_text(json.dumps(updated_signals), encoding="utf-8")
    os.utime(paths["signals"], ns=(paths["signals"].stat().st_mtime_ns + 1_000_000, paths["signals"].stat().st_mtime_ns + 1_000_000))

    snap3 = _unwrap_rpc_result(
        await bridge.handle_tools_call(
            23,
            {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
        )
    )
    assert snap3["date"] == "2026-10-23"
    assert snap3["stale"] is False
    assert snap3["cn_a"]["stale"] is False

    # Corrupted JSON -> ok=False, status="invalid_source"
    paths["crowding"].write_text("{not valid json", encoding="utf-8")
    os.utime(paths["crowding"], ns=(paths["crowding"].stat().st_mtime_ns + 2_000_000, paths["crowding"].stat().st_mtime_ns + 2_000_000))
    bad_crowd_reply = await bridge.handle_tools_call(
        24,
        {"name": "macro", "arguments": {"route": "market-regime", "operation": "crowding"}},
    )
    assert bad_crowd_reply["result"]["isError"] is True
    bad_crowd = _unwrap_rpc_result(bad_crowd_reply)
    assert bad_crowd["ok"] is False
    assert bad_crowd["status"] == "invalid_source"

    # Missing file -> ok=False, status="source_missing"
    paths["signals"].unlink()
    missing_reply = await bridge.handle_tools_call(
        25,
        {"name": "macro", "arguments": {"route": "market-regime", "operation": "snapshot"}},
    )
    assert missing_reply["result"]["isError"] is True
    missing_payload = _unwrap_rpc_result(missing_reply)
    assert missing_payload["ok"] is False
    assert missing_payload["status"] == "source_missing"


@pytest.mark.asyncio
async def test_tools_list_catalog_and_data_map_integration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`tools/list`, `catalog`, `sources.yaml`, `health.py`, skills, and prompt template all include `market-regime`."""
    bridge, _ = _make_fixture_bridge(monkeypatch, tmp_path)

    async def _fake_stdio_rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        method = requests[0].get("method")
        if method == "initialize":
            return [{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"version": "1.0.0"}}}]
        if method == "tools/list":
            return [
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {
                                "name": "macro",
                                "description": "Macro data.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "route": {"type": "string", "enum": ["fred", "treasury"]},
                                        "operation": {"type": "string", "description": "Operation."},
                                    },
                                },
                            }
                        ]
                    },
                }
            ]
        # catalog tool call
        return [
            {
                "jsonrpc": "2.0",
                "id": requests[0].get("id", 1),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": True,
                                    "status": "ok",
                                    "routes": [{"id": "fred", "category": "macro"}],
                                    "route_count": 1,
                                }
                            ),
                        }
                    ]
                },
            }
        ]

    bridge._run_stdio_rpc.side_effect = _fake_stdio_rpc

    # 1. tools/list includes `market-regime` and its parameters on `macro`
    tools = await bridge.get_filtered_tools()
    macro_tool = next(t for t in tools if t["name"] == "macro")
    props = macro_tool["inputSchema"]["properties"]
    assert "market-regime" in props["route"]["enum"]
    for param_key in ("operation", "top", "order", "category", "query"):
        assert param_key in props

    # 2. catalog includes `market-regime` entry and parameters
    cat_reply = await bridge.handle_tools_call(30, {"name": "catalog", "arguments": {}})
    cat_payload = _unwrap_rpc_result(cat_reply)
    mr_entry = next(r for r in cat_payload["routes"] if r["id"] == "market-regime")
    assert mr_entry["category"] == "macro"
    assert mr_entry["operations"] == ["snapshot", "us_topbottom", "crowding", "cn_leverage"]
    assert set(mr_entry["parameters"].keys()) >= {"operation", "top", "order", "category", "query"}

    # 3. Data map sources.yaml and health probe spec + latest_date extraction
    spec = map_builder.load_sources_spec()
    mr_yaml = spec["catalog_routes"]["market-regime"]
    assert mr_yaml["category"] == "macro"
    assert "大盘过热/超卖、顶部底部阶段、拥挤度、两融杠杆；回答大盘和仓位问题时先读" in mr_yaml["description"]

    probe_tool, probe_args = health_mod.build_route_probe_spec("market-regime", "macro")
    assert probe_tool == "macro"
    assert probe_args == {"route": "market-regime", "operation": "snapshot"}

    snap_payload = await bridge._handle_market_regime({"operation": "snapshot"})
    assert health_mod.extract_latest_date(snap_payload) == "2026-10-22"

    # 4. Prompt routing rule and skill/evidence docs
    loader = PromptLoader()
    map_builder.set_turn_data_map(None)
    analyst_prompt = loader.get_system_prompt(role="analyst")
    assert "问到大盘、仓位、过热或超卖时，先读 `market-regime`" in analyst_prompt

    stock_move_md = (REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/SKILL.md").read_text(encoding="utf-8")
    assert 'macro(route="market-regime", operation="snapshot")' in stock_move_md

    weekly_md = (REPO_ROOT / "plugins/dalton/skills/dalton-weekly-review/SKILL.md").read_text(encoding="utf-8")
    assert "市场状态" in weekly_md
    assert 'operation="crowding", top=5' in weekly_md

    evidence_md = (
        REPO_ROOT / "plugins/dalton/skills/dalton-conventions/references/evidence-and-sources.md"
    ).read_text(encoding="utf-8")
    assert "regime:<op>:<date>" in evidence_md
    assert 'publisher="OpenClaw"' in evidence_md
    assert "上涨概率" in evidence_md
    assert "probability_pct" in evidence_md
