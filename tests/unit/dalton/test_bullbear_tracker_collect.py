"""MSFT 采集器的离线测试。只使用临时目录和 create_autospec，不访问真实 API。"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_PATH = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker" / "scripts" / "collect.py"
ET = ZoneInfo("America/New_York")
FROZEN = datetime(2026, 11, 10, 22, 50, tzinfo=ET)
WIKI_WARNING = "列表有更多文档，请缩小筛选或提高 limit"


def _load():
    spec = importlib.util.spec_from_file_location("dalton_msft_tracker_collect_under_test", COLLECT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect_mod = _load()
_ORIG_BRIDGE = collect_mod.call_bridge_jsonrpc
_ORIG_NOW = collect_mod.now_et
_ORIG_TRANSPORT = collect_mod.resolve_researchdata_transport
_ORIG_CONSENSUS = collect_mod.fetch_consensus_snapshot


def _run(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    ticker: str | None = "MSFT",
) -> tuple[int, dict[str, Any]]:
    if ticker is not None and "--ticker" not in argv:
        argv = [argv[0], "--ticker", ticker, *argv[1:]]
    code = collect_mod.main(argv)
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def _patch_bridge(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(
        collect_mod,
        "call_bridge_jsonrpc",
        create_autospec(_ORIG_BRIDGE, side_effect=fn),
    )


def _patch_clock(monkeypatch: pytest.MonkeyPatch, clock: dict[str, datetime]) -> None:
    monkeypatch.setattr(
        collect_mod,
        "now_et",
        create_autospec(_ORIG_NOW, side_effect=lambda: clock["now"]),
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    def _resolve() -> dict[str, Any]:
        if state["mode"] is None:
            return {"mode": None, "call": None, "error": "unconfigured"}
        return {"mode": state["mode"], "call": "local", "error": ""}

    monkeypatch.setattr(
        collect_mod,
        "resolve_researchdata_transport",
        create_autospec(_ORIG_TRANSPORT, side_effect=_resolve),
    )


def _patch_consensus(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(
        collect_mod,
        "fetch_consensus_snapshot",
        create_autospec(_ORIG_CONSENSUS, side_effect=fn),
    )


def _consensus_ok(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
    assert ticker == "MSFT"
    return {
        "ok": True,
        "status": "ok",
        "fetched_at": "2026-11-10T22:50:00-05:00",
        "currency": "USD",
        "field_status": {"earnings_estimate": "ok"},
        "earnings_estimate": {},
        "revenue_estimate": {},
        "eps_trend": {},
        "eps_revisions": {},
        "price": {"close": 400, "chg_1d_pct": 0.1, "chg_5d_pct": 0.2, "as_of": "2026-11-10"},
        "price_targets": {"mean": 450},
    }


def _workspace(path: Path, ticker: str = "MSFT") -> Path:
    ws = path / "ws"
    (ws / "data" / "consensus").mkdir(parents=True)
    (ws / "research" / "tracker").mkdir(parents=True)
    (ws / "data" / "company.json").write_text(
        json.dumps({"ticker": ticker, "benchmarks": ["QQQ"]}),
        encoding="utf-8",
    )
    return ws


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_origin_packet_with_ae_fresh(expected_json: str) -> str:
    """原 origin/dalton/main 基准未带 AE 首页 params.fresh=True 与配置项 search_in；补入后应逐字节一致。"""
    obj = json.loads(expected_json)
    ticker = str(obj.get("ticker") or "").strip().upper()
    search_in_by_query: dict[str, str] = {}
    if ticker:
        cfg_path = collect_mod.research_config_path(ticker)
        if cfg_path.is_file():
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for q_text, q_search_in in collect_mod._parse_ae_queries(cfg_data):
                if q_search_in:
                    search_in_by_query[q_text] = q_search_in
    for row in (obj.get("alphaengine") or {}).get("status") or []:
        req = row.get("request")
        if isinstance(req, dict) and isinstance(req.get("params"), dict):
            q_name = str(row.get("query") or "")
            if q_name in search_in_by_query:
                req["params"]["search_in"] = search_in_by_query[q_name]
            req["params"]["fresh"] = True
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _ae_state(until: str, marker: str) -> dict[str, Any]:
    return {
        "queries": [{
            "query": marker,
            "window": {"start": "2026-11-01", "end": until},
            "complete": True,
            "covered_until": until,
            "ok": True,
            "cursor": None,
        }]
    }


def _write_packet(ws: Path, run_id: str, until: str, **extra: Any) -> bytes:
    internal_new = {doc_id: {"analyzed": True} for doc_id in extra.get("new_ids", [])}
    internal_new.update(extra.get("extra_new") or {})
    packet = {
        "ticker": "MSFT",
        "run_id": run_id,
        "collection_complete": True,
        "since": extra.get("since", "2026-11-09"),
        "until": until,
        "snapshot_file": "data/consensus/snap.json",
        "msft_ae_state": extra.get("ae") or _ae_state(until, run_id),
        "internal": {
            "new": internal_new,
            "pending": extra.get("pending") or {},
            "withheld": {},
            "irrelevant": {doc_id: {"analyzed": True} for doc_id in extra.get("irrelevant", [])},
        },
        "alphaengine": {"new": {}},
        "sec": {"new": {}},
        "x": {
            "new": {doc_id: {} for doc_id in extra.get("x_new", [])},
            "noise_ids": list(extra.get("noise") or []),
        },
    }
    if "collected_at" in extra:
        packet["collected_at"] = extra["collected_at"]
    if "retry" in extra:
        packet["internal_retry_since"] = extra["retry"]
    folder = ws / "research" / "tracker" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "packet.json"
    target.write_text(json.dumps(packet), encoding="utf-8")
    return target.read_bytes()


def test_normalized_envelopes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = collect_mod.load_research_config()
    query = " ".join(cfg["alias_terms"])
    calls: list[tuple[str, dict[str, Any]]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append((tool_name, arguments))
        if arguments.get("operation") == "read":
            return {"ok": True, "status": "ok", "data": {"content": "Microsoft Azure grew.\n", "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            cursor = (arguments.get("params") or {}).get("cursor")
            if not cursor:
                body = {
                    "results": [
                        {"document_id": "tmtb:1", "title": "One", "date": "2026-11-09", "keyword_hits": 2},
                        {"document_id": "shared", "title": "Shared", "date": "2026-11-08", "keyword_hits": 2},
                    ],
                    "has_more": True,
                    "cursor": "c1",
                }
                return {"ok": True, "data": {"content": [{"type": "text", "text": json.dumps(body)}]}}
            return {
                "ok": True,
                "data": {"results": [{"document_id": "tmtb:2", "title": "Two", "date": "2026-11-07", "keyword_hits": 1}], "has_more": False},
            }
        if route == "sales-notes":
            return {
                "ok": True,
                "data": {"structuredContent": {"results": [
                    {"document_id": "shared", "title": "Shared later", "date": "2026-11-08"},
                    {"document_id": "sales:1", "title": "Sales", "date": "2026-11-08", "keyword_hits": 1},
                ], "has_more": False}},
            }
        if route == "funda-ai":
            return {
                "ok": True,
                "data": {"data": {"results": [{"document_id": "funda:1", "title": "Funda", "date": "2026-11-08", "keyword_hits": 1}]}},
            }
        if route == "company-wiki":
            return {
                "ok": True,
                "data": {"results": [{
                    "document_id": "wiki:ms",
                    "title": "Owned",
                    "date": "2026-11-08",
                    "company": "MSFT",
                    "location": str(tmp_path / "missing-wiki.txt"),
                }]},
            }
        if route == "alphaengine":
            if arguments.get("query") == cfg["ae_queries"][0]:
                body = {"results": [
                    {"doc_id": "alphaengine:_inst_99", "title": "Inst", "companies": ["Microsoft"], "publish_time": "2026-11-08"},
                    {"doc_id": "_inst_77", "title": "Bare", "companies": ["Microsoft Corp."], "publish_time": "2026-11-08"},
                    {"doc_id": "alphaengine:seen-me", "title": "Seen", "companies": ["Microsoft"], "publish_time": "2026-11-08"},
                    {"doc_id": "alphaengine:apple", "title": "Snack", "companies": ["Apple"], "publish_time": "2026-11-08"},
                ], "has_more": False}
                return {"ok": True, "data": {"content": [{"type": "text", "text": json.dumps(body)}]}}
            return {
                "ok": True,
                "data": {"data": {"results": [
                    {"doc_id": "alphaengine:nested", "title": "Azure note", "companies": [], "publish_time": "2026-11-08"},
                ]}},
            }
        if arguments.get("route") == "sec":
            if arguments.get("form") != "8-K":
                return {"ok": True, "data": {"rows": []}}
            return {"ok": True, "data": {"data": {"rows": [
                {"accession_no": "0001193125-26-323632", "form": "8-K", "filing_date": "2026-11-08"},
                {"accession_no": "0001193125-26-000002", "form": "8-K", "filing_date": "2026-11-06"},
                {"accession_no": "0001193125-26-000003", "form": "8-K", "filing_date": "2026-11-08"},
            ]}}}
        raise AssertionError(arguments)

    _patch_bridge(monkeypatch, bridge)
    ws = _workspace(tmp_path)
    run_dir = ws / "research" / "tracker" / "env"
    run_dir.mkdir()
    seen = {"alphaengine:seen-me", "sec:0001193125-26-000003"}
    internal = collect_mod.collect_internal(cfg, "2026-11-09", "2026-11-10", seen, ws, run_dir)
    ae, _state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", seen, {})
    sec = collect_mod.collect_sec(cfg, "2026-11-09", seen)

    assert not (tmp_path / "missing-wiki.txt").exists()
    assert internal["new"]["shared"]["route"] == "tmtb"
    assert {"tmtb:1", "tmtb:2", "shared", "sales:1", "funda:1", "wiki:ms"} <= set(internal["new"])
    assert "alphaengine:_inst_99" in ae["new"]
    assert "alphaengine:_inst_77" in ae["new"]
    assert "alphaengine:nested" in ae["new"]
    assert "alphaengine:apple" not in ae["new"]
    assert "alphaengine:seen-me" not in ae["new"]
    kept = sec["new"]["sec:0001193125-26-323632"]
    assert kept["accession"] == "0001193125-26-323632"
    assert "sec:0001193125-26-000002" not in sec["new"]
    assert "sec:0001193125-26-000003" not in sec["new"]

    keyword = [args for _tool, args in calls if args.get("route") in {"tmtb", "sales-notes", "funda-ai"}]
    assert keyword[0]["query"] == query
    assert keyword[0]["limit"] == 60
    assert keyword[0]["start"] == "2026-11-09" and keyword[0]["end"] == "2026-11-10"
    assert keyword[0]["params"]["media"] == "text" and keyword[0]["params"]["search_mode"] == "keyword"
    page2 = [args for args in keyword if args.get("route") == "tmtb" and (args.get("params") or {}).get("cursor")]
    assert page2[0]["query"] == query and page2[0]["params"]["cursor"] == "c1"
    wiki_calls = [args for _tool, args in calls if args.get("route") == "company-wiki"]
    assert wiki_calls[0]["operation"] == "list"
    assert wiki_calls[0]["params"]["indexed_since"] == "2026-11-09"
    assert wiki_calls[0]["limit"] == 100
    ae_calls = [args for _tool, args in calls if args.get("route") == "alphaengine"]
    assert [args["query"] for args in ae_calls] == list(cfg["ae_queries"])
    assert {args["limit"] for args in ae_calls} == {20}
    sec_calls = [(tool, args) for tool, args in calls if args.get("route") == "sec"]
    assert {tool for tool, _args in sec_calls} == {"finance"}
    assert [args["form"] for _tool, args in sec_calls] == list(cfg["sources"]["sec_forms"])
    assert {args["limit"] for _tool, args in sec_calls} == {3}
    assert all(args.get("ticker") == "MSFT" for _tool, args in sec_calls)
    assert not any(args.get("route") in {"news", "gsearch"} or "params" in args and args.get("route") == "x-timeline" for _tool, args in calls)

    def capped(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append((tool_name, arguments))
        return {"ok": True, "data": {"results": [{"document_id": "p"}], "has_more": True, "cursor": "next"}}

    before = len(calls)
    _patch_bridge(monkeypatch, capped)
    fetched = collect_mod._fetch_pages("research", lambda cursor: {"route": "tmtb", "cursor": cursor}, 1)
    assert len(calls) == before + 1
    assert fetched["incomplete"] is True
    assert "翻页上限" in fetched["error"]
    assert fetched["items"][0]["document_id"] == "p"


def test_read_cap_pending_commit_and_internal_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skipped = collect_mod._commit_ids({
        "internal": {
            "new": {"bad": {"analyzed": False}},
            "irrelevant": {"irr-ok": {"analyzed": True}, "irr-skip": {"analyzed": True, "skipped": "x"}},
            "pending": {"pending-id": {}},
            "withheld": {"withheld-id": {"analyzed": True}},
        },
        "alphaengine": {"new": {}},
        "sec": {"new": {}},
        "x": {"new": {"x:1": {}}, "noise_ids": ["x:n"]},
    })
    assert skipped == {"irr-ok", "x:1", "x:n"}

    docs = [
        {
            "document_id": f"tmtb:{i:02d}",
            "title": f"Note {i}",
            "date": f"2026-11-{14 - i:02d}",
            "keyword_hits": 1,
            "original_source": "TMTB",
        }
        for i in range(1, 14)
    ]
    calls: list[dict[str, Any]] = []
    phase = {"truncate": False}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        if arguments.get("operation") == "read":
            if phase["truncate"]:
                return {"ok": True, "data": {"content": "Microsoft\n", "next_offset": 20}}
            return {"ok": True, "data": {"content": "Microsoft Azure\n", "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            return {"ok": True, "data": {"results": docs, "has_more": False}}
        if route in {"sales-notes", "funda-ai", "company-wiki", "alphaengine"}:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        raise AssertionError(arguments)

    clock = {"now": FROZEN}
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, clock)
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    seen_path = tracker / "seen.json"
    last_path = tracker / "last_run.json"
    log_path = tracker / "updates.log"
    seen_path.write_text('["old-seen"]\n', encoding="utf-8")
    last_path.write_text('{"until": "2026-11-09", "run_id": "seed"}\n', encoding="utf-8")
    log_path.write_text("seed line\n", encoding="utf-8")
    before = (seen_path.read_text(encoding="utf-8"), last_path.read_text(encoding="utf-8"), log_path.read_text(encoding="utf-8"))

    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2200"])
    assert code == 0 and payload["short_circuit_eligible"] is False
    assert (seen_path.read_text(encoding="utf-8"), last_path.read_text(encoding="utf-8"), log_path.read_text(encoding="utf-8")) == before
    packet = _read_json(tracker / "20261110-2200" / "packet.json")
    assert packet["since"] == "2026-11-08"
    assert packet["internal_retry_since"] == "2026-11-08"
    assert "tmtb:13" not in packet["internal"]["new"]
    assert packet["internal"]["pending"]["tmtb:13"]["analyzed"] is False
    assert packet["internal"]["pending"]["tmtb:13"]["pending_reason"] == "read_cap"
    assert "tmtb:13" in (tracker / "20261110-2200" / "packet.md").read_text(encoding="utf-8")
    read_ids = {args["document"] for args in calls if args.get("operation") == "read"}
    assert read_ids == {f"tmtb:{i:02d}" for i in range(1, 13)}

    code, _payload = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2200", "--material", "no", "--summary", "cap"])
    assert code == 0
    seen = set(_read_json(seen_path))
    assert "old-seen" in seen
    assert seen >= {f"tmtb:{i:02d}" for i in range(1, 13)}
    assert "tmtb:13" not in seen
    last = _read_json(last_path)
    assert last["internal_retry_since"] == "2026-11-08"
    assert "ae_resume" not in last and "ae_covered_until" not in last

    calls.clear()
    phase["truncate"] = True
    clock["now"] = datetime(2026, 11, 11, 10, 0, tzinfo=ET)
    last_after_commit = last_path.read_text(encoding="utf-8")
    code, _payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261111-1000"])
    assert code == 0
    assert last_path.read_text(encoding="utf-8") == last_after_commit
    second = _read_json(tracker / "20261111-1000" / "packet.json")
    assert second["since"] == "2026-11-09"
    assert second["internal_since"] == "2026-11-08"
    tmtb_starts = [args["start"] for args in calls if args.get("route") == "tmtb"]
    ae_starts = [args["start"] for args in calls if args.get("route") == "alphaengine"]
    assert tmtb_starts == ["2026-11-08"]
    assert ae_starts and set(ae_starts) == {"2026-11-08"}

    calls.clear()
    clock["now"] = datetime(2026, 11, 11, 15, 0, tzinfo=ET)
    code, _payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261111-1500", "--since", "2026-11-01"])
    assert code == 0
    assert [args["start"] for args in calls if args.get("route") == "tmtb"] == ["2026-11-01"]
    assert {args["start"] for args in calls if args.get("route") == "alphaengine"} == {"2026-11-08"}


def test_wiki_alias_threshold_and_truncated_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = collect_mod.load_research_config()
    cfg["excluded_documents"] = ["wiki:excluded"]
    secret = tmp_path / "secret-location.txt"
    texts = {
        "wiki:owned": "no alias words",
        "wiki:one": "Azure is mentioned once",
        "wiki:two": "Azure and Azure again",
        "wiki:zero": "azure only",
        "wiki:cut": "Microsoft Azure",
    }

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            doc_id = arguments["document"]
            if doc_id == "wiki:cut":
                return {"ok": True, "data": {"content": texts[doc_id], "next_offset": 30}}
            return {"ok": True, "data": {"content": texts[doc_id], "next_offset": None}}
        if arguments.get("route") != "company-wiki":
            return {"ok": True, "data": {"results": [], "has_more": False}}
        return {"ok": True, "data": {"results": [
            {"document_id": "image:1", "title": "Image", "company": "MSFT"},
            {"document_id": "wiki:excluded", "title": "Excluded", "company": "MSFT"},
            {"document_id": "wiki:owned", "title": "Owned", "company": "MSFT", "date": "2026-11-02", "location": str(secret)},
            {"document_id": "wiki:one", "title": "One hit", "company": "OTHER", "date": "2026-11-03"},
            {"document_id": "wiki:two", "title": "Two hits", "company": "OTHER", "date": "2026-11-04"},
            {"document_id": "wiki:zero", "title": "Zero", "company": "OTHER", "date": "2026-11-01"},
            {"document_id": "wiki:cut", "title": "Cut", "company": "OTHER", "date": "2026-11-05"},
        ]}}

    _patch_bridge(monkeypatch, bridge)
    monkeypatch.setattr(collect_mod, "MAX_READ_PAGES", 1)
    ws = _workspace(tmp_path)
    run_dir = ws / "research" / "tracker" / "wiki"
    run_dir.mkdir()
    internal = collect_mod.collect_internal(cfg, "2026-11-08", "2026-11-10", set(), ws, run_dir)
    assert not secret.exists()
    assert "wiki:owned" in internal["new"]
    assert internal["withheld"]["wiki:one"]["alias_hits"] == 1
    assert "wiki:two" in internal["new"] and internal["new"]["wiki:two"]["alias_hits"] == 2
    assert "wiki:zero" in internal["irrelevant"]
    assert internal["pending"]["wiki:cut"]["pending_reason"] == "read_truncated"
    assert "wiki:cut" not in internal["irrelevant"]
    assert "image:1" not in internal["new"] and "wiki:excluded" not in internal["new"]
    ids = collect_mod._commit_ids({"internal": internal, "alphaengine": {}, "sec": {}, "x": {}})
    assert {"wiki:owned", "wiki:two", "wiki:zero"} <= ids
    assert "wiki:one" not in ids and "wiki:cut" not in ids


def test_ae_two_query_window_resume_and_completed_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    calls: list[dict[str, Any]] = []
    mode = {"kind": "resume"}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        if mode["kind"] == "partial":
            return {
                "ok": True,
                "status": "partial",
                "data": {
                    "ok": True,
                    "status": "partial",
                    "results": [{"doc_id": "alphaengine:partial", "title": "Azure partial", "companies": ["Microsoft"]}],
                    "warnings": [WIKI_WARNING],
                },
            }
        if arguments["query"] == first:
            return {"ok": True, "data": {"results": [
                {"doc_id": "alphaengine:_inst_99", "title": "Kept", "companies": ["Microsoft"], "publish_time": arguments["end"]},
            ], "has_more": True, "cursor": "c-new"}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    last_run = {"msft_ae_state": {"queries": [
        {"query": first, "complete": True, "covered_until": "2026-11-06", "ok": True, "cursor": None, "window": {"start": "2026-11-03", "end": "2026-11-06"}},
        {"query": second, "complete": False, "covered_until": "2026-11-05", "ok": True, "cursor": "cur-2", "window": {"start": "2026-11-02", "end": "2026-11-06"}},
    ]}}
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", {"alphaengine:seen-me"}, last_run)
    by_query = {row["query"]: row for row in state["queries"]}
    q1_calls = [args for args in calls if args["query"] == first]
    q2_calls = [args for args in calls if args["query"] == second]
    assert q1_calls[0]["start"] == "2026-11-03" and q1_calls[0]["end"] == "2026-11-10"
    assert q1_calls[0]["params"] == {"sort": "relevance", "fresh": True}
    # 整段 4 页 + 首子窗口 [11-04, 11-07] 5 页 + 单日重扫 [11-07, 11-07] 5 页 + 11-08/09/10 各 5 页 = 29 页
    assert len(q1_calls) == (collect_mod.MAX_AE_PAGES - 1) + collect_mod.MAX_AE_PAGES * 5
    assert q1_calls[collect_mod.MAX_AE_PAGES - 1]["start"] == "2026-11-04"
    assert q1_calls[collect_mod.MAX_AE_PAGES - 1]["end"] == "2026-11-07"
    assert q1_calls[(collect_mod.MAX_AE_PAGES - 1) + collect_mod.MAX_AE_PAGES]["start"] == "2026-11-07"
    assert q1_calls[(collect_mod.MAX_AE_PAGES - 1) + collect_mod.MAX_AE_PAGES]["end"] == "2026-11-07"
    assert q2_calls[0]["start"] == "2026-11-02" and q2_calls[0]["end"] == "2026-11-10"
    assert q2_calls[0]["params"] == {"sort": "relevance", "fresh": True}
    assert all("cur-2" not in json.dumps(args) for args in calls)
    assert by_query[first]["complete"] is False
    assert by_query[first]["covered_until"] == "2026-11-10"
    assert by_query[first]["cursor"] is None
    assert by_query[first]["partial_days"] == ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"]
    assert "2026-11-07 当天超过翻页上限，只取了前 1 条" in by_query[first]["error"]
    assert "2026-11-10 当天超过翻页上限，只取了前 1 条" in by_query[first]["error"]
    assert by_query[second]["complete"] is True
    assert by_query[second]["covered_until"] == "2026-11-10"
    assert by_query[second]["cursor"] is None
    assert "alphaengine:_inst_99" in _ae["new"]

    calls.clear()
    _ae2, state2 = collect_mod.collect_alphaengine(
        cfg, "2026-11-09", "2026-11-12", set(), {"msft_ae_state": state}
    )
    by_query2 = {row["query"]: row for row in state2["queries"]}
    replay = [args for args in calls if args["query"] == first]
    opened = [args for args in calls if args["query"] == second]
    assert replay[0]["start"] == "2026-11-06" and replay[0]["end"] == "2026-11-12"
    assert replay[0]["params"] == {"sort": "relevance", "fresh": True}
    assert replay[collect_mod.MAX_AE_PAGES - 1]["start"] == "2026-11-08"
    assert replay[collect_mod.MAX_AE_PAGES - 1]["end"] == "2026-11-11"
    assert by_query2[first]["covered_until"] == "2026-11-12"
    assert opened[0]["end"] == "2026-11-12" and opened[0]["start"] <= "2026-11-10"
    assert opened[0]["params"] == {"sort": "relevance", "fresh": True}
    assert len(opened) == 1

    calls.clear()
    mode["kind"] = "partial"
    previous = {"msft_ae_state": {"queries": [
        {"query": query, "complete": True, "covered_until": "2026-11-06", "ok": True, "window": {"start": "2026-11-03", "end": "2026-11-06"}}
        for query in cfg["ae_queries"]
    ]}}
    ae_partial, partial_state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), previous)
    assert "alphaengine:partial" in ae_partial["new"]
    assert {row["covered_until"] for row in partial_state["queries"]} == {"2026-11-06"}
    assert all(row["complete"] is False and WIKI_WARNING in row["error"] for row in partial_state["queries"])
    assert all(args["start"] <= "2026-11-06" for args in calls)


def test_x_same_day_until_dedupe_and_null_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = collect_mod.load_research_config()
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    pool = {
        "inactive_user": {"active": False, "tickers": ["MSFT"]},
        "otherco": {"active": True, "tickers": ["AAPL"]},
        "keeper": {"active": True, "tickers": ["MSFT"]},
        "allhands": {"active": True, "tickers": ["ALL"]},
    }
    authors = {"someone": {"followers": 10, "bio": "cached", "fetched_at": "2026-11-01T00:00:00Z", "handle": "someone"}}
    pool_path = tracker / "x_pool.json"
    author_path = tracker / "x_authors.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    author_path.write_text(json.dumps(authors), encoding="utf-8")
    pool_bytes, author_bytes = pool_path.read_bytes(), author_path.read_bytes()
    calls: list[dict[str, Any]] = []

    def post(post_id: str, text: str, created_at: str, likes: int, **extra: Any) -> dict[str, Any]:
        item = {
            "id": post_id,
            "text": text,
            "created_at": created_at,
            "author_handle": extra.get("author_handle", "newperson"),
            "metrics": {"likes": likes, "reposts": extra.get("reposts", 0), "replies": 1, "views": 2},
        }
        item.update({key: value for key, value in extra.items() if key not in {"author_handle", "reposts"}})
        return item

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        assert tool_name == "community" and "params" not in arguments
        calls.append(arguments)
        if arguments.get("operation") == "search":
            return {"ok": True, "data": {"data": {"has_more": True, "items": [
                post("42", "search EPS", "2026-11-11T02:30:00Z", 1),
                post("77", "repost EPS", "2026-11-11T02:30:00Z", 9, is_repost=True),
                post("99", "seen EPS", "2026-11-11T02:30:00Z", 9),
                post("7", "zero EPS", "2026-11-11T02:30:00Z", 0),
                post("8", "late EPS", "2026-11-11T12:00:00Z", 9),
                post("9", "undated EPS", "n/a", 0),
                post("weather", "hello weather", "2026-11-11T02:30:00Z", 0),
            ]}}}
        if arguments.get("handle") == "keeper":
            return {"ok": True, "data": {"results": [
                post("42", "pool EPS", "2026-11-11T02:30:00Z", 5, author_handle="keeper"),
            ]}}
        return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}

    _patch_bridge(monkeypatch, bridge)
    data = collect_mod.collect_x(cfg, "2026-11-09", "2026-11-10", {"x:99"}, ws)
    assert data["new"]["x:42"]["via"] == "pool"
    assert data["new"]["x:42"]["text"].startswith("pool EPS")
    assert "x-timeline:42" not in data["new"]
    assert "x:77" not in data["new"] and "x:77" not in data["noise_ids"]
    assert "x:99" not in data["new"] and "x:99" not in data["noise_ids"]
    assert "x:8" not in data["new"]
    zero = data["new"]["x:7"]
    assert zero["likes"] == 0 and zero["followers"] is None and zero["bio"] is None and zero["bookmarks"] is None
    assert "x:9" in data["new"]
    assert "x:weather" in data["noise_ids"]
    assert data["unprocessed_upstream"] is True and data["failures"] == []
    searches = [args for args in calls if args.get("operation") == "search"]
    timelines = [args for args in calls if args.get("operation") == "tweets"]
    assert len(searches) == len(cfg["x_queries"])
    assert {args["until"] for args in searches} == {"2026-11-12"}
    assert {args["since"] for args in searches} == {"2026-11-09"}
    assert {args["count"] for args in searches} == {40}
    assert {args["handle"] for args in timelines} == {"keeper", "allhands"}
    assert {args["until"] for args in timelines} == {"2026-11-11"}
    assert {args["count"] for args in timelines} == {20}
    assert pool_path.read_bytes() == pool_bytes and author_path.read_bytes() == author_bytes

    calls.clear()

    def overflow(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        if arguments.get("operation") == "tweets" and arguments.get("handle") == "keeper":
            return {"ok": True, "data": {"data": {"items": [
                post("high", "pool high", "2026-11-10T12:00:00-05:00", 9, author_handle="keeper"),
                post("low", "pool low", "2026-11-10T12:00:00-05:00", 1, author_handle="keeper"),
            ], "has_more": True}}}
        return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}

    _patch_bridge(monkeypatch, overflow)
    narrow = dict(cfg)
    narrow["x_keep_max"] = 1
    overflow_data = collect_mod.collect_x(narrow, "2026-11-09", "2026-11-10", set(), ws)
    assert "x:high" in overflow_data["new"] and "x:low" in overflow_data["noise_ids"]
    assert overflow_data["failures"] == [] and overflow_data["unprocessed_upstream"] is True
    failures = collect_mod.failed_sources({"status": {}}, {"status": []}, {"status": []}, overflow_data, {"ok": True, "status": "ok"})
    assert failures == []
    assert author_path.read_bytes() == author_bytes


def _normalize_x_timeline():
    scripts = REPO_ROOT / "scripts" / "dalton"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import researchdata_bridge as bridge

    return bridge._normalize_x_timeline_output


def _xreach(post_id: str, created_at: str, text: str) -> dict[str, Any]:
    return {
        "id": post_id,
        "createdAt": created_at,
        "text": text,
        "user": {"screenName": "keeper", "name": "Keeper"},
        "likeCount": 4,
        "retweetCount": 0,
        "replyCount": 0,
        "viewCount": 1,
    }


def test_x_upstream_window_keeps_et_evening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    winter_timeline, winter_search = collect_mod._x_upstream_until("2026-11-10")
    summer_timeline, summer_search = collect_mod._x_upstream_until("2026-07-15")
    same_timeline, same_search = collect_mod._x_upstream_until("2026-11-10")
    assert (winter_timeline, winter_search) == ("2026-11-11", "2026-11-12")
    assert (summer_timeline, summer_search) == ("2026-07-16", "2026-07-17")
    assert (same_timeline, same_search) == (winter_timeline, winter_search)

    normalize = _normalize_x_timeline()
    evening = _xreach("evening", "2026-11-11T02:30:00Z", "Azure EPS late")
    nxt = _xreach("nextday", "2026-11-11T12:00:00Z", "Azure EPS next")
    dropped = normalize({"items": [evening, nxt]}, operation="tweets", count=20, since="2026-11-10", until="2026-11-10")
    assert dropped["items"] == []
    kept = normalize({"items": [evening, nxt]}, operation="tweets", count=20, since="2026-11-10", until=winter_timeline)
    assert {item["id"] for item in kept["items"]} == {"evening", "nextday"}
    summer_evening = _xreach("summer", "2026-07-16T02:30:00Z", "Azure EPS summer")
    summer_old = normalize(
        {"items": [summer_evening]}, operation="tweets", count=20, since="2026-07-15", until="2026-07-15"
    )
    summer_new = normalize(
        {"items": [summer_evening]}, operation="tweets", count=20, since="2026-07-15", until=summer_timeline
    )
    assert summer_old["items"] == []
    assert [item["id"] for item in summer_new["items"]] == ["summer"]

    cfg = collect_mod.load_research_config()
    ws = _workspace(tmp_path)
    (ws / "research" / "tracker" / "x_pool.json").write_text(
        json.dumps({"keeper": {"active": True, "tickers": ["MSFT"]}}), encoding="utf-8"
    )
    calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        assert tool_name == "community" and "params" not in arguments
        calls.append(arguments)
        if arguments.get("operation") == "search":
            items = []
            for item in (evening, nxt):
                stamp = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")).astimezone(ZoneInfo("UTC"))
                if stamp.date().isoformat() < arguments["until"]:
                    items.append(item)
            return {"ok": True, "data": {"data": {"items": items, "has_more": False}}}
        raw = {"items": [evening, nxt]}
        normalized = normalize(
            raw,
            operation="tweets",
            count=int(arguments["count"]),
            since=str(arguments.get("since") or ""),
            until=str(arguments.get("until") or ""),
        )
        return {"ok": True, "data": {"data": {"items": normalized["items"], "has_more": False}}}

    _patch_bridge(monkeypatch, bridge)
    data = collect_mod.collect_x(cfg, "2026-11-10", "2026-11-10", set(), ws)
    assert {args["until"] for args in calls if args.get("operation") == "search"} == {"2026-11-12"}
    assert {args["until"] for args in calls if args.get("operation") == "tweets"} == {"2026-11-11"}
    assert "x:evening" in data["new"]
    assert data["new"]["x:evening"]["created_at"].startswith("2026-11-11T02:30:00")
    assert "x:nextday" not in data["new"] and "x:nextday" not in data["noise_ids"]


def test_legacy_ae_resume_then_new_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    covered_only = {"ae_covered_until": "2026-11-05"}
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), covered_only)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-11-02"}
    assert {args["end"] for args in calls} == {"2026-11-10"}
    assert all("cursor" not in args["params"] for args in calls)

    calls.clear()
    imported = {
        "ticker": "MSFT",
        "run_id": "20261105-1800",
        "committed_at": "2026-11-05T18:00:00-05:00",
        "since": "2026-11-02",
        "until": "2026-11-05",
        "snapshot_file": "data/consensus/old.json",
        "material": "no",
        "summary": "imported",
        "ae_covered_until": "2026-11-05",
        "ae_resume": {
            "window": {"since": "2026-11-02", "until": "2026-11-05"},
            "cursor": "legacy-cursor-token",
            "query": "Microsoft",
        },
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), imported)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-11-02"}
    assert {args["end"] for args in calls} == {"2026-11-10"}
    assert all(args["params"] == {"sort": "relevance", "fresh": True} for args in calls)
    assert all("legacy-cursor-token" not in json.dumps(args) for args in calls)
    assert {row["complete"] for row in state["queries"]} == {True}
    assert {row["covered_until"] for row in state["queries"]} == {"2026-11-10"}

    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    _write_packet(ws, "20261110-1000", "2026-11-10", since="2026-11-09", ae=state)
    code, _payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-1000", "--material", "no", "--summary", "legacy"],
    )
    assert code == 0
    last = _read_json(ws / "research" / "tracker" / "last_run.json")
    assert last["run_id"] == "20261110-1000"
    assert "ae_resume" not in last and "ae_covered_until" not in last
    assert "legacy-cursor-token" not in json.dumps(last)
    assert all("cursor" not in row and "resumed_cursor" not in row for row in last["msft_ae_state"]["queries"])
    assert {row["window"]["end"] for row in last["msft_ae_state"]["queries"]} == {"2026-11-10"}

    calls.clear()
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-11-06"}
    assert {args["end"] for args in calls} == {"2026-11-10"}
    assert all(args["params"] == {"sort": "relevance", "fresh": True} for args in calls)
    assert _ae["status"]


def test_retained_ae_coverage_commits_and_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    mode = {"kind": "has_more"}
    calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        kind = mode["kind"]
        if kind == "error":
            return {"ok": False, "error": "upstream down", "data": {}}
        if kind == "partial":
            return {
                "ok": True,
                "status": "partial",
                "data": {"ok": True, "status": "partial", "results": [], "warnings": [WIKI_WARNING]},
            }
        if kind == "resume":
            return {"ok": True, "data": {"results": [], "has_more": False}}
        token = "cursor-kept" if arguments["query"] == first else "cursor-kept-2"
        return {"ok": True, "data": {"results": [], "has_more": True, "cursor": token}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    seed_last = {
        "ticker": "MSFT",
        "run_id": "20261110-0900",
        "committed_at": "2026-11-10T09:00:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/snap.json",
        "material": "no",
        "summary": "covered",
        "msft_ae_state": {"queries": [
            {
                "query": query,
                "complete": True,
                "ok": True,
                "cursor": None,
                "covered_until": "2026-11-10",
                "window": {"start": "2026-11-06", "end": "2026-11-10"},
            }
            for query in (first, second)
        ]},
    }
    states: dict[str, dict[str, Any]] = {}
    for kind in ("has_more", "error", "partial"):
        mode["kind"] = kind
        calls.clear()
        _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), seed_last)
        rows = state["queries"]
        assert [row["query"] for row in rows] == [first, second]
        assert all(row["complete"] is False and row["covered_until"] == "2026-11-10" for row in rows)
        assert all(row["window"] == {"start": "2026-11-06", "end": "2026-11-10"} for row in rows)
        first_calls = [
            next(args for args in calls if args["query"] == q)
            for q in (first, second)
        ]
        assert all(args["params"] == {"sort": "relevance", "fresh": True} for args in first_calls)
        assert _ae["status"]
        states[kind] = state
    assert [row["cursor"] for row in states["has_more"]["queries"]] == [None, None]
    assert all(row["ok"] is True and "翻页上限" in row["error"] for row in states["has_more"]["queries"])
    assert all(
        row["ok"] is False and row["cursor"] is None and row["error"] == "upstream down"
        for row in states["error"]["queries"]
    )
    assert all(
        row["ok"] is True and row["cursor"] is None and WIKI_WARNING in row["error"]
        for row in states["partial"]["queries"]
    )

    def commit_kind(kind: str) -> tuple[Path, dict[str, Any]]:
        ws = _workspace(tmp_path / kind)
        (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
        (ws / "research" / "tracker" / "last_run.json").write_text(json.dumps(seed_last), encoding="utf-8")
        _write_packet(
            ws, "20261110-2200", "2026-11-10",
            ae=states[kind], collected_at="2026-11-10T22:00:00-05:00",
        )
        code, payload = _run(
            capsys,
            ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2200", "--material", "no", "--summary", kind],
        )
        assert code == 0 and payload["ok"] is True
        last = _read_json(ws / "research" / "tracker" / "last_run.json")
        saved = {row["query"]: row for row in last["msft_ae_state"]["queries"]}
        for row in states[kind]["queries"]:
            kept = saved[row["query"]]
            assert kept["complete"] is False
            assert kept["covered_until"] == "2026-11-10"
            assert "cursor" not in kept and "resumed_cursor" not in kept
            assert kept["ok"] is row["ok"]
            assert kept["window"] == {"start": "2026-11-06", "end": "2026-11-10"}
        return ws, last

    kept_ws, kept_last = commit_kind("has_more")
    commit_kind("error")
    commit_kind("partial")

    mode["kind"] = "resume"
    calls.clear()
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-12", set(), kept_last)
    replayed = {args["query"]: args for args in calls}
    assert set(replayed) == {first, second}
    assert replayed[first]["start"] == "2026-11-06" and replayed[first]["end"] == "2026-11-12"
    assert replayed[first]["params"] == {"sort": "relevance", "fresh": True}
    assert replayed[second]["start"] == "2026-11-06" and replayed[second]["end"] == "2026-11-12"
    assert replayed[second]["params"] == {"sort": "relevance", "fresh": True}

    blocked = _workspace(tmp_path / "earlier")
    (blocked / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    earlier = json.loads(json.dumps(seed_last))
    for row in earlier["msft_ae_state"]["queries"]:
        row["covered_until"] = "2026-11-09"
        row["window"]["end"] = "2026-11-09"
    earlier_path = blocked / "research" / "tracker" / "last_run.json"
    earlier_path.write_text(json.dumps(earlier), encoding="utf-8")
    _write_packet(
        blocked, "20261110-2200", "2026-11-10",
        ae=states["has_more"], collected_at="2026-11-10T22:00:00-05:00",
    )
    before = earlier_path.read_bytes()
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(blocked), "--run-id", "20261110-2200", "--material", "no", "--summary", "advance"],
    )
    assert code == 2 and payload["status"] == "ae_incomplete_advanced"
    assert earlier_path.read_bytes() == before

    kept_path = kept_ws / "research" / "tracker" / "last_run.json"
    past = json.loads(json.dumps(states["has_more"]))
    for row in past["queries"]:
        row["complete"] = False
        row["covered_until"] = "2026-11-11"
    _write_packet(
        kept_ws, "20261110-2300", "2026-11-10",
        ae=past, collected_at="2026-11-10T23:00:00-05:00",
    )
    before = kept_path.read_bytes()
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(kept_ws), "--run-id", "20261110-2300", "--material", "no", "--summary", "past-end"],
    )
    assert code == 2 and payload["status"] == "ae_incomplete_advanced"
    assert kept_path.read_bytes() == before

    moved = json.loads(json.dumps(states["has_more"]))
    for row in moved["queries"]:
        row["complete"] = False
        row["covered_until"] = "2026-11-11"
        row["window"]["end"] = "2026-11-11"
    _write_packet(
        kept_ws, "20261110-2310", "2026-11-11",
        ae=moved, collected_at="2026-11-10T23:10:00-05:00",
    )
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(kept_ws), "--run-id", "20261110-2310", "--material", "no", "--summary", "new-end"],
    )
    assert code == 2 and payload["status"] == "ae_incomplete_advanced"
    assert kept_path.read_bytes() == before
    assert "cursor" not in _read_json(kept_path)["msft_ae_state"]["queries"][0]


def test_offline_collect_leaves_seen_and_recovers_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = {"n": 0, "closed": False}
    transport = {"mode": None}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if calls["closed"]:
            raise AssertionError("recovered collect called the bridge")
        calls["n"] += 1
        if arguments.get("route") == "x-timeline":
            assert "params" not in arguments
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        if arguments.get("route") == "sec":
            return {"ok": True, "data": {"rows": []}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, transport)
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    seen_path, last_path, log_path = tracker / "seen.json", tracker / "last_run.json", tracker / "updates.log"
    seen_path.write_text('["keep-me"]\n', encoding="utf-8")
    last_path.write_text('{"until": "2026-11-09", "run_id": "seed"}\n', encoding="utf-8")
    log_path.write_text("seed line\n", encoding="utf-8")
    before = (seen_path.read_bytes(), last_path.read_bytes(), log_path.read_bytes())

    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "run-offline"])
    assert code == 2 and payload["status"] == "researchdata_transport_unconfigured"
    assert not (tracker / "run-offline" / "packet.json").exists()
    assert (seen_path.read_bytes(), last_path.read_bytes(), log_path.read_bytes()) == before

    transport["mode"] = "bridge"
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "run-offline"])
    assert code == 0 and payload["ok"] is True and payload["recovered"] is False
    assert payload["short_circuit_eligible"] is False
    assert payload["snapshot_file"] == "data/consensus/2026-11-10_2250.json"
    packet_path = tracker / "run-offline" / "packet.json"
    packet_bytes = packet_path.read_bytes()
    packet = json.loads(packet_bytes)
    assert packet["collection_complete"] is True
    assert packet["collected_at"] == FROZEN.isoformat()
    assert packet["internal_retry_since"] is None
    assert packet["news"]["status"] == "pending_agent_search"
    assert packet["news"]["requires_original_fetch"] is True
    assert (seen_path.read_bytes(), last_path.read_bytes(), log_path.read_bytes()) == before
    assert calls["n"] > 0
    taken = calls["n"]
    calls["closed"] = True

    def _boom(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
        raise AssertionError("recovered collect fetched consensus")

    _patch_consensus(monkeypatch, _boom)
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "run-offline"])
    assert code == 0 and payload["recovered"] is True
    assert calls["n"] == taken
    assert packet_path.read_bytes() == packet_bytes
    assert (seen_path.read_bytes(), last_path.read_bytes(), log_path.read_bytes()) == before


def test_collect_rejects_non_msft_and_bad_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = _workspace(tmp_path / "bad-company", ticker="AAPL")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "ok-id"])
    assert code == 2 and payload["status"] == "company_mismatch"
    assert not (ws / "research" / "tracker" / "ok-id").exists()

    ws = _workspace(tmp_path / "bad-run")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "../x"])
    assert code == 2 and payload["status"] == "bad_run_id"
    assert not (ws / "x").exists() and not (ws.parent / "x").exists()


def _log_count(text: str, run_id: str) -> int:
    needle = f" | {run_id} | material="
    return sum(1 for line in text.splitlines() if needle in line)


def test_commit_same_day_order_stale_and_partial_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_clock(monkeypatch, {"now": FROZEN})
    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    (ws / "research" / "tracker" / "seen.json").write_text('["keep-me"]\n', encoding="utf-8")
    packets = {
        "20261110-1000": _write_packet(
            ws, "20261110-1000", "2026-11-10",
            collected_at="not-a-date", new_ids=["doc-1000"], ae=_ae_state("2026-11-10", "q-1000"),
        ),
        "20261110-2200": _write_packet(
            ws, "20261110-2200", "2026-11-10",
            collected_at="2026-11-10T22:00:00",
            new_ids=["good-2200"],
            extra_new={"bad-unread": {"analyzed": False, "incomplete": True, "skipped": "unread"}},
            pending={"pending-1": {"analyzed": False}},
            irrelevant=["irr-2200"],
            x_new=["x:keep"],
            noise=["x:noise"],
            retry="2026-11-08",
            ae=_ae_state("2026-11-10", "q-2200"),
        ),
    }
    _write_packet(ws, "20261110-0900", "2026-11-10", new_ids=["doc-0900"], ae=_ae_state("2026-11-10", "q-0900"))
    _write_packet(
        ws, "20261109-1800", "2026-11-09", new_ids=["doc-stale"],
        retry="2026-11-01", ae=_ae_state("2026-11-09", "q-stale"),
    )

    def commit(run_id: str) -> tuple[int, dict[str, Any]]:
        return _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "no", "--summary", run_id])

    code, _payload = commit("20261110-1000")
    assert code == 0
    last_path = ws / "research" / "tracker" / "last_run.json"
    log_path = ws / "research" / "tracker" / "updates.log"
    seen_path = ws / "research" / "tracker" / "seen.json"
    first = _read_json(last_path)
    assert first["run_id"] == "20261110-1000"
    assert datetime.fromisoformat(first["collected_at"]) == datetime(2026, 11, 10, 10, 0, tzinfo=ET)
    assert "not-a-date" not in last_path.read_text(encoding="utf-8")

    code, _payload = commit("20261110-2200")
    assert code == 0
    last = _read_json(last_path)
    saved = last_path.read_text(encoding="utf-8")
    assert last["run_id"] == "20261110-2200"
    assert datetime.fromisoformat(last["collected_at"]) == datetime(2026, 11, 10, 22, 0, tzinfo=ET)
    assert last["msft_ae_state"]["queries"][0]["query"] == "q-2200"
    assert last["internal_retry_since"] == "2026-11-08"
    assert "ae_resume" not in last and "ae_covered_until" not in last
    log = log_path.read_text(encoding="utf-8")
    assert _log_count(log, "20261110-1000") == 1 and _log_count(log, "20261110-2200") == 1
    seen = set(_read_json(seen_path))
    assert {"keep-me", "doc-1000", "good-2200", "irr-2200", "x:keep", "x:noise"} <= seen
    assert "bad-unread" not in seen and "pending-1" not in seen

    code, _payload = commit("20261110-1000")
    assert code == 0
    assert last_path.read_text(encoding="utf-8") == saved
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261110-1000") == 1
    assert (ws / "research" / "tracker" / "20261110-1000" / "packet.json").read_bytes() == packets["20261110-1000"]

    kept = "".join(
        line for line in log_path.read_text(encoding="utf-8").splitlines(True)
        if " | 20261110-2200 | material=" not in line
    )
    log_path.write_text(kept, encoding="utf-8")
    code, payload = commit("20261110-2200")
    assert code == 0 and payload["log_repaired"] is True
    assert last_path.read_text(encoding="utf-8") == saved
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261110-2200") == 1
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261110-1000") == 1
    code, payload = commit("20261110-2200")
    assert code == 0 and payload["idempotent"] is True
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261110-2200") == 1

    code, _payload = commit("20261110-0900")
    assert code == 0
    assert "doc-0900" in set(_read_json(seen_path))
    assert last_path.read_text(encoding="utf-8") == saved
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261110-0900") == 1

    code, payload = commit("20261109-1800")
    assert code == 0 and payload["stale"] is True
    assert "doc-stale" in set(_read_json(seen_path))
    assert last_path.read_text(encoding="utf-8") == saved
    assert _read_json(last_path)["internal_retry_since"] == "2026-11-08"
    assert _log_count(log_path.read_text(encoding="utf-8"), "20261109-1800") == 1
    assert (ws / "research" / "tracker" / "20261110-2200" / "packet.json").read_bytes() == packets["20261110-2200"]

    code, payload = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2200", "--material", "maybe", "--summary", "nope"])
    assert code == 2 and payload["status"] == "bad_material"
    code, payload = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2200", "--material", "no", "--summary", "   "])
    assert code == 2 and payload["status"] == "bad_summary"
    assert last_path.read_text(encoding="utf-8") == saved

    imported = _workspace(tmp_path / "imported")
    (imported / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    imported_last = {
        "ticker": "MSFT",
        "run_id": "20261110-1000",
        "committed_at": "2026-11-10T23:30:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/snap.json",
        "material": "no",
        "summary": "imported",
        "msft_ae_state": _ae_state("2026-11-10", "q-import"),
    }
    (imported / "research" / "tracker" / "last_run.json").write_text(json.dumps(imported_last), encoding="utf-8")
    (imported / "research" / "tracker" / "updates.log").write_text(
        "2026-11-10 23:30 | 20261110-1000 | material=no | imported\n", encoding="utf-8"
    )
    _write_packet(imported, "20261110-2200", "2026-11-10", new_ids=["doc-import"], ae=_ae_state("2026-11-10", "q-2200"))
    code, _payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(imported), "--run-id", "20261110-2200", "--material", "yes", "--summary", "later"],
    )
    assert code == 0
    imported_now = _read_json(imported / "research" / "tracker" / "last_run.json")
    assert imported_now["run_id"] == "20261110-2200"
    assert imported_now["msft_ae_state"]["queries"][0]["query"] == "q-2200"
    imported_log = (imported / "research" / "tracker" / "updates.log").read_text(encoding="utf-8")
    assert _log_count(imported_log, "20261110-1000") == 1
    assert _log_count(imported_log, "20261110-2200") == 1


def test_commit_older_first_logs_once_and_keeps_newer_cursors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_clock(monkeypatch, {"now": FROZEN})
    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    (ws / "research" / "tracker" / "seen.json").write_text('["keep-me"]\n', encoding="utf-8")
    newer_ae = {
        "queries": [{
            "query": "q-2200",
            "window": {"start": "2026-10-25", "end": "2026-11-10"},
            "complete": False,
            "covered_until": "2026-10-28",
            "ok": True,
            "cursor": "cursor-2200",
        }]
    }
    older_ae = {
        "queries": [{
            "query": "q-1000",
            "window": {"start": "2026-10-17", "end": "2026-10-28"},
            "complete": False,
            "covered_until": "2026-10-20",
            "ok": True,
            "cursor": "cursor-1000",
        }]
    }
    _write_packet(
        ws, "20261110-2200", "2026-11-10",
        new_ids=["doc-2200"], retry="2026-11-08", ae=newer_ae,
    )
    older_bytes = _write_packet(
        ws, "20261110-1000", "2026-11-10",
        new_ids=["doc-1000"], retry="2026-10-01", ae=older_ae,
    )

    def commit(run_id: str) -> tuple[int, dict[str, Any]]:
        return _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "no", "--summary", run_id])

    code, payload = commit("20261110-2200")
    assert code == 0 and payload["last_run_unchanged"] is False
    last_path = ws / "research" / "tracker" / "last_run.json"
    log_path = ws / "research" / "tracker" / "updates.log"
    saved = last_path.read_text(encoding="utf-8")
    last = _read_json(last_path)
    assert last["run_id"] == "20261110-2200"
    assert last["internal_retry_since"] == "2026-11-08"
    assert last["msft_ae_state"]["queries"][0]["covered_until"] == "2026-10-28"
    assert "cursor" not in last["msft_ae_state"]["queries"][0]
    assert "cursor-2200" not in saved

    code, payload = commit("20261110-1000")
    assert code == 0 and payload["stale"] is True and payload["last_run_unchanged"] is True
    assert last_path.read_text(encoding="utf-8") == saved
    log = log_path.read_text(encoding="utf-8")
    assert _log_count(log, "20261110-2200") == 1
    assert _log_count(log, "20261110-1000") == 1
    assert sum(1 for line in log.splitlines() if "| material=" in line) == 2
    seen = set(_read_json(ws / "research" / "tracker" / "seen.json"))
    assert {"keep-me", "doc-2200", "doc-1000"} <= seen

    code, payload = commit("20261110-1000")
    assert code == 0 and payload["last_run_unchanged"] is True
    assert last_path.read_text(encoding="utf-8") == saved
    log = log_path.read_text(encoding="utf-8")
    assert _log_count(log, "20261110-1000") == 1
    assert _log_count(log, "20261110-2200") == 1
    assert sum(1 for line in log.splitlines() if "| material=" in line) == 2
    assert _read_json(last_path)["msft_ae_state"]["queries"][0]["covered_until"] == "2026-10-28"
    assert "cursor" not in _read_json(last_path)["msft_ae_state"]["queries"][0]
    assert "cursor-1000" not in saved
    assert (ws / "research" / "tracker" / "20261110-1000" / "packet.json").read_bytes() == older_bytes


def test_commit_rejects_bad_snapshot_and_identity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = ws / "data" / "consensus" / "link.json"
    cases = {
        "abs-run": {"snapshot_file": "/tmp/snap.json"},
        "trav-run": {"snapshot_file": "data/consensus/../../data/company.json"},
        "miss-run": {"snapshot_file": "data/consensus/missing.json"},
        "link-run": {"snapshot_file": "data/consensus/link.json"},
    }
    for run_id, overrides in cases.items():
        packet = {
            "ticker": "MSFT",
            "run_id": run_id,
            "collection_complete": True,
            "since": "2026-11-09",
            "until": "2026-11-10",
            "snapshot_file": overrides["snapshot_file"],
            "msft_ae_state": _ae_state("2026-11-10", run_id),
            "internal": {},
            "alphaengine": {},
            "sec": {},
            "x": {},
        }
        folder = ws / "research" / "tracker" / run_id
        folder.mkdir()
        target = folder / "packet.json"
        target.write_text(json.dumps(packet), encoding="utf-8")
        before = target.read_bytes()
        if run_id == "link-run":
            link.symlink_to(outside)
        code, payload = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "no", "--summary", "bad"])
        assert code == 2 and payload["ok"] is False
        if run_id == "link-run":
            assert payload["status"] == "symlink_escape"
        assert target.read_bytes() == before
    assert not (ws / "research" / "tracker" / "last_run.json").exists()
    link.unlink()

    identity = _workspace(tmp_path / "identity")
    folder = identity / "research" / "tracker" / "runA"
    folder.mkdir()
    mismatched = {
        "ticker": "MSFT", "run_id": "runB", "collection_complete": True,
        "since": "2026-11-09", "until": "2026-11-10", "snapshot_file": "data/consensus/missing.json",
        "msft_ae_state": _ae_state("2026-11-10", "runB"), "internal": {}, "alphaengine": {}, "sec": {}, "x": {},
    }
    (folder / "packet.json").write_text(json.dumps(mismatched), encoding="utf-8")
    code, payload = _run(capsys, ["commit", "--workspace-dir", str(identity), "--run-id", "runA", "--material", "no", "--summary", "bad"])
    assert code == 2 and payload["status"] == "packet_run_mismatch"

    code, payload = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "../x", "--material", "no", "--summary", "bad"])
    assert code == 2 and payload["status"] == "bad_run_id"
    assert not (ws / "x").exists() and not (ws.parent / "x").exists()


def test_implied_model_period_rollover_and_consensus_flags() -> None:
    model = json.loads(json.dumps(collect_mod.load_research_config()["model"]))
    model["other_income_bn"]["+1y"] = -1.5
    assert "0001193125-26-323632" in model["source"]

    def row(avg: float, end: str | None, currency: str = "USD") -> dict[str, Any]:
        body: dict[str, Any] = {"avg": avg, "currency": currency}
        if end is not None:
            body["period_end"] = end
        return body

    def consensus(spec: dict[str, tuple[float, float, str | None, str]]) -> dict[str, Any]:
        return {
            "earnings_estimate": {key: row(eps, end, currency) for key, (eps, _rev, end, currency) in spec.items()},
            "revenue_estimate": {key: row(rev, end, currency) for key, (_eps, rev, end, currency) in spec.items()},
        }

    built = collect_mod.build_implied_model(consensus({
        "0y": (10, 100e9, "2027-06-30", "USD"),
        "+1y": (10, 100e9, "2028-06-30", "USD"),
    }), model)
    assert built["assumptions"]["source"] == model["source"]
    current = built["periods"]["0y"]
    assert current["other_income_key"] == "0y"
    assert current["implied_pretax_bn"] == 93.1
    assert current["implied_total_expenses_bn"] == 6.5
    assert current["implied_op_margin_pct"] == 93.5
    assert current["eps_per_10bn_expense"] == -1.07
    assert current["eps_per_1pct_revenue_no_cost"] == 0.11
    nxt = built["periods"]["+1y"]
    assert nxt["other_income_key"] == "+1y" and nxt["other_income_bn"] == -1.5
    assert nxt["implied_total_expenses_bn"] == 5.4

    missing = collect_mod.build_implied_model(consensus({"0y": (10, 100e9, None, "USD")}), model)
    assert missing["periods"]["0y"]["status"] == "missing_period"
    assert "implied_total_expenses_bn" not in missing["periods"]["0y"]

    foreign = collect_mod.build_implied_model(consensus({"0y": (10, 100e9, "2027-06-30", "EUR")}), model)
    assert foreign["periods"]["0y"]["status"] == "currency_incomparable"
    assert "implied_total_expenses_bn" not in foreign["periods"]["0y"]

    rolled = collect_mod.build_implied_model(consensus({"0y": (10, 100e9, "2028-06-30", "USD")}), model)
    assert rolled["periods"]["0y"]["other_income_key"] == "+1y"
    assert rolled["periods"]["0y"]["other_income_bn"] == -1.5

    incompatible = collect_mod.build_implied_model(consensus({"0y": (10, 100e9, "2029-06-30", "USD")}), model)
    reason = incompatible["periods"]["0y"]["reason"]
    assert incompatible["periods"]["0y"]["status"] == "period_incompatible"
    assert "FY27" in reason and "未把" in reason
    assert "implied_total_expenses_bn" not in incompatible["periods"]["0y"]

    def snap(fetched: str, estimates: dict[str, tuple[float, str]], trend: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "fetched_at": fetched,
            "currency": "USD",
            "earnings_estimate": {key: {"avg": eps, "period_end": end, "currency": "USD"} for key, (eps, end) in estimates.items()},
            "revenue_estimate": {key: {"avg": 100e9, "period_end": end, "currency": "USD"} for key, (_eps, end) in estimates.items()},
            "eps_trend": trend or {},
            "eps_revisions": {"+1y": {"upLast7days": 0, "downLast7Days": 0}},
            "price": {"chg_1d_pct": 0.1, "chg_5d_pct": 0.2, "benchmark": "QQQ"},
            "price_targets": {"mean": 450},
        }

    periods = {"0q": (3, "2026-12-31"), "+1q": (3, "2027-03-31"), "0y": (10.0, "2027-06-30"), "+1y": (11, "2028-06-30")}
    current_periods = dict(periods)
    current_periods["0y"] = (10.2, "2027-06-30")
    thr = collect_mod.map_thresholds(collect_mod.load_research_config()["thresholds"])
    _deltas, flags = collect_mod.consensus_deltas(
        snap("2026-11-10T22:50:00-05:00", current_periods, {"0y": {"current": 10.3, "7daysAgo": 10.0}}),
        snap("2026-11-09T22:50:00-05:00", periods),
        thr,
    )
    last_flags = [flag for flag in flags if "本财年(0y) EPS 较上次运行" in flag]
    week_flags = [flag for flag in flags if "本财年(0y) EPS 较 7 天前" in flag]
    assert len(last_flags) == 1 and len(week_flags) == 1
    assert last_flags[0] != week_flags[0] and "较上次运行" not in week_flags[0]

    rolled_cur = dict(periods)
    rolled_cur["0y"] = (10.2, "2029-06-30")
    rolled_deltas, rolled_flags = collect_mod.consensus_deltas(
        snap("2026-11-10T22:50:00-05:00", rolled_cur),
        snap("2026-11-09T22:50:00-05:00", periods),
        thr,
    )
    assert rolled_deltas["eps_0y_vs_last_pct"] is None
    assert not any("本财年(0y) EPS 较上次运行" in flag for flag in rolled_flags)


def test_partial_wiki_consensus_and_sec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = {
        "ok": True,
        "status": "partial",
        "data": {
            "ok": True,
            "status": "partial",
            "results": [{"document_id": "wiki:1", "company": "META", "title": "Old doc"}],
            "warnings": [WIKI_WARNING],
        },
    }

    def once(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        return fixture

    _patch_bridge(monkeypatch, once)
    fetched = collect_mod._fetch_pages("research", lambda cursor: {"route": "company-wiki"}, 5)
    assert fetched["request_ok"] is True and fetched["incomplete"] is True and fetched["status"] == "incomplete"
    assert fetched["cursor"] is None and fetched["items"][0]["document_id"] == "wiki:1"
    assert WIKI_WARNING in fetched["error"]

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            return {"ok": True, "data": {"content": "hello", "next_offset": None}}
        if arguments.get("route") == "company-wiki":
            return fixture
        if arguments.get("route") == "sec" and arguments.get("form") == "8-K":
            return {
                "ok": True,
                "status": "partial",
                "data": {
                    "status": "partial",
                    "rows": [{"accession_no": "0001193125-26-323632", "form": "8-K", "filing_date": "2026-11-08"}],
                    "warnings": [WIKI_WARNING],
                },
            }
        if arguments.get("route") == "sec":
            return {"ok": True, "data": {"rows": []}}
        if arguments.get("route") == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    def consensus(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
        body = _consensus_ok(ticker, benchmark)
        body["ok"] = True
        body["status"] = "partial"
        body["field_status"] = {"earnings_estimate": "ok", "price": "empty"}
        return body

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, consensus)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    seen_path = tracker / "seen.json"
    seen_path.write_text('["keep-me"]\n', encoding="utf-8")
    before = seen_path.read_bytes()
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "partial-1", "--since", "2026-11-08"])
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"]
    assert seen_path.read_bytes() == before
    packet = _read_json(tracker / "partial-1" / "packet.json")
    assert packet["consensus"]["status"] == "partial"
    assert packet["consensus"]["field_status"]["price"] == "empty"
    assert packet["internal_retry_since"] == "2026-11-08"
    assert packet["internal"]["status"]["company-wiki"]["incomplete"] is True
    assert WIKI_WARNING in packet["internal"]["status"]["company-wiki"]["error"]
    assert packet["internal"]["status"]["company-wiki"]["returned"] == 1
    assert "wiki:1" in packet["internal"]["irrelevant"]
    assert "sec:0001193125-26-323632" in packet["sec"]["new"]
    sources = {item["source"]: item["error"] for item in packet["failed_sources"]}
    assert WIKI_WARNING in sources["company-wiki"]
    assert WIKI_WARNING in sources["sec"]
    assert "partial" in sources["estimates"] and "field_status" in sources["estimates"]
    assert "price" in sources["estimates"]


def test_incomplete_ae_page_keeps_packet_ok_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    calls = {"n": 0, "closed": False}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if calls["closed"]:
            raise AssertionError("recovered collect called the bridge")
        calls["n"] += 1
        if arguments.get("route") == "alphaengine":
            assert arguments["limit"] == 20
            token = "a" if arguments["query"] == first else "b"
            items = [
                {
                    "doc_id": f"alphaengine:{token}-{i}",
                    "title": "Azure note",
                    "companies": ["Microsoft"],
                    "publish_time": "2026-11-10",
                }
                for i in range(20)
            ]
            return {"ok": True, "data": {"results": items, "has_more": True, "cursor": f"cursor-{token}"}}
        if arguments.get("route") == "x-timeline":
            assert "params" not in arguments
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        if arguments.get("route") == "sec":
            return {"ok": True, "data": {"rows": []}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    last_run = {
        "ticker": "MSFT",
        "run_id": "20261109-1800",
        "until": "2026-11-09",
        "msft_ae_state": {"queries": [
            {
                "query": query,
                "complete": True,
                "ok": True,
                "cursor": None,
                "covered_until": "2026-11-09",
                "window": {"start": "2026-11-06", "end": "2026-11-09"},
            }
            for query in (first, second)
        ]},
    }
    last_path = tracker / "last_run.json"
    last_path.write_text(json.dumps(last_run), encoding="utf-8")
    before_last = last_path.read_bytes()
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "ae-page", "--since", "2026-11-09"])
    assert code == 0 and payload["ok"] is True and payload["recovered"] is False
    assert payload["short_circuit_eligible"] is False
    assert payload["counts"]["alphaengine"] == 40
    errors = [item["error"] for item in payload["failed_sources"] if item["source"] == "alphaengine"]
    assert len(errors) == 2
    for query, err in zip((first, second), errors, strict=True):
        assert err.startswith(f"{query}: ")
        assert "2026-11-10 当天超过翻页上限，只取了前 20 条" in err
    packet_path = tracker / "ae-page" / "packet.json"
    packet_bytes = packet_path.read_bytes()
    packet = json.loads(packet_bytes)
    assert packet["collection_complete"] is True
    assert packet["failed_sources"] == payload["failed_sources"]
    by_query = {row["query"]: row for row in packet["msft_ae_state"]["queries"]}
    for query in (first, second):
        row = by_query[query]
        assert row["ok"] is True and row["returned"] == 20 and row["has_more"] is True
        assert row["complete"] is False and row["cursor"] is None
        assert row["covered_until"] == "2026-11-10"
        assert row["window"]["end"] == "2026-11-10"
    assert last_path.read_bytes() == before_last
    taken = calls["n"]
    calls["closed"] = True

    def _boom(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
        raise AssertionError("recovered collect fetched consensus")

    _patch_consensus(monkeypatch, _boom)
    code, again = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "ae-page", "--since", "2026-11-09"])
    assert code == 0 and again["ok"] is True and again["recovered"] is True
    assert again["failed_sources"] == payload["failed_sources"]
    assert again["counts"] == payload["counts"]
    assert calls["n"] == taken
    assert packet_path.read_bytes() == packet_bytes
    assert last_path.read_bytes() == before_last

    bad = tracker / "bad-packet" / "packet.json"
    bad.parent.mkdir()
    bad.write_text("{", encoding="utf-8")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "bad-packet"])
    assert code == 2 and payload["ok"] is False and payload["status"] == "packet_unreadable"
    assert calls["n"] == taken
    half = tracker / "half-packet" / "packet.json"
    half.parent.mkdir()
    half.write_text(
        json.dumps({"ticker": "MSFT", "run_id": "half-packet", "collection_complete": False}),
        encoding="utf-8",
    )
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "half-packet"])
    assert code == 2 and payload["ok"] is False and payload["status"] == "packet_exists_not_complete"
    assert calls["n"] == taken
    assert packet_path.read_bytes() == packet_bytes


@pytest.mark.parametrize("mode", ["has_more", "partial", "error"])
def test_legacy_incomplete_window_can_commit_and_resume(monkeypatch, tmp_path, capsys, mode):
    cfg = collect_mod.load_research_config()
    calls = []
    def bridge(tool_name, arguments, timeout=120):
        calls.append(arguments)
        if mode == "error":
            return {"ok": False, "error": "unavailable", "data": {}}
        if mode == "partial":
            return {"ok": True, "status": "partial", "data": {"results": [], "status": "partial", "warnings": [WIKI_WARNING]}}
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:legacy-{arguments['end']}",
                        "title": "Microsoft",
                        "companies": ["Microsoft"],
                        "publish_time": arguments["end"],
                    }
                ],
                "has_more": True,
                "cursor": "new-query-cursor",
            },
        }
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    imported = {"ae_covered_until": "2026-11-06", "ae_resume": {"window": {"since": "2026-11-04", "until": "2026-11-06"}, "cursor": "legacy-cursor"}}
    _, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), imported)
    if mode == "has_more":
        assert all(not row["complete"] and row["covered_until"] == "2026-11-10" for row in state["queries"])
    else:
        assert all(not row["complete"] and row["covered_until"] is None for row in state["queries"])
    ws = _workspace(tmp_path)
    (ws / "data/consensus/snap.json").write_text("{}", encoding="utf-8")
    _write_packet(ws, "20261110-1000", "2026-11-10", since="2026-11-09", ae=state)
    code, _ = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-1000", "--material", "no", "--summary", "resume legacy window"])
    assert code == 0
    last = _read_json(ws / "research/tracker/last_run.json")
    assert all("cursor" not in row for row in last["msft_ae_state"]["queries"])
    calls.clear()
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-11", set(), last)
    first_per_query = [
        next(row for row in calls if row["query"] == q)
        for q in cfg["ae_queries"]
    ]
    expected_start = "2026-11-08" if mode == "has_more" else "2026-11-04"
    assert all(row["start"] == expected_start and row["end"] == "2026-11-11" for row in first_per_query)
    assert all(row["params"] == {"sort": "relevance", "fresh": True} for row in first_per_query)
    assert all("new-query-cursor" not in json.dumps(row) for row in first_per_query)


def test_old_incomplete_packet_commits_after_newer_coverage(monkeypatch, tmp_path, capsys):
    _patch_clock(monkeypatch, {"now": FROZEN})
    ws = _workspace(tmp_path)
    (ws / "data/consensus/snap.json").write_text("{}", encoding="utf-8")
    old_state = _ae_state("2026-11-10", "query")
    old_state["queries"][0]["complete"] = False
    old_state["queries"][0]["cursor"] = "old-cursor"
    _write_packet(ws, "20261110-2200", "2026-11-10", new_ids=["old-doc"], ae=old_state)
    _write_packet(ws, "20261111-2200", "2026-11-11", new_ids=["new-doc"], ae=_ae_state("2026-11-11", "query"))
    def commit(run):
        return _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", run, "--material", "no", "--summary", run])
    assert commit("20261111-2200")[0] == 0
    latest = (ws / "research/tracker/last_run.json").read_bytes()
    assert commit("20261110-2200")[0] == 0
    assert commit("20261110-2200")[0] == 0
    assert (ws / "research/tracker/last_run.json").read_bytes() == latest
    assert _log_count((ws / "research/tracker/updates.log").read_text(), "20261110-2200") == 1
    assert {"old-doc", "new-doc"} <= set(_read_json(ws / "research/tracker/seen.json"))


@pytest.mark.parametrize("raw", ["NVDA", "msftt", ""])
def test_collect_rejects_unknown_ticker(capsys: pytest.CaptureFixture[str], raw: str) -> None:
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", "/tmp/unused", "--run-id", "ok-id"],
        ticker=raw,
    )
    assert code == 2
    assert payload["status"] == "unsupported_ticker"
    assert payload["ticker"] == raw.strip().upper()
    assert collect_mod.TICKER == "MSFT"


@pytest.mark.parametrize(
    ("ticker", "lock_name", "state_key"),
    [
        ("MSFT", ".msft-collect.lock", "msft_ae_state"),
        ("META", ".meta-collect.lock", "meta_ae_state"),
        ("AMZN", ".amzn-collect.lock", "amzn_ae_state"),
        ("GOOGL", ".googl-collect.lock", "googl_ae_state"),
    ],
)
def test_lock_and_ae_state_key_follow_ticker(tmp_path: Path, ticker: str, lock_name: str, state_key: str) -> None:
    assert collect_mod.ae_state_key(ticker) == state_key
    assert collect_mod.collect_lock_name(ticker) == lock_name
    assert ".msft-collect.lock" in COLLECT_PATH.read_text(encoding="utf-8")
    workspace = tmp_path / ticker
    tracker = workspace / "research" / "tracker"
    previous = collect_mod.TICKER
    collect_mod.TICKER = ticker
    try:
        with collect_mod.workspace_lock(workspace, tracker):
            assert (tracker / lock_name).is_file()
    finally:
        collect_mod.TICKER = previous


@pytest.mark.parametrize("ticker", ["MSFT", "META", "AMZN", "GOOGL"])
def test_x_pool_scope_is_company_or_all(tmp_path: Path, ticker: str) -> None:
    workspace = tmp_path / ticker
    tracker = workspace / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "x_pool.json").write_text(
        json.dumps(
            {
                "meta_only": {"active": True, "tickers": ["META"]},
                "shared": {"active": True, "tickers": ["ALL"]},
                "msft_only": {"active": True, "tickers": ["MSFT"]},
                "amzn_only": {"active": True, "tickers": ["AMZN"]},
                "googl_only": {"active": True, "tickers": ["GOOGL"]},
                "idle": {"active": False, "tickers": [ticker, "ALL"]},
            }
        ),
        encoding="utf-8",
    )
    previous = collect_mod.TICKER
    collect_mod.TICKER = ticker
    try:
        handles, _pool = collect_mod._load_pool(workspace, {"x_pool_max": 25})
    finally:
        collect_mod.TICKER = previous
    expected = {
        "MSFT": ["shared", "msft_only"],
        "META": ["meta_only", "shared"],
        "AMZN": ["shared", "amzn_only"],
        "GOOGL": ["shared", "googl_only"],
    }
    assert handles == expected[ticker]


def test_applicable_years_follow_fiscal_month_when_source_has_no_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect_mod, "now_et", lambda: datetime(2026, 11, 11, tzinfo=ET))
    for ticker in ("META", "AMZN", "GOOGL"):
        model = json.loads(collect_mod.research_config_path(ticker).read_text(encoding="utf-8"))["model"]
        assert model["fiscal_year_end_month"] == 12
        years = collect_mod.applicable_years(model)
        assert [item["key"] for item in years] == ["0y", "+1y"]
        assert years[0]["fiscal_year"] == "FY2026"
        assert years[0]["period_end_prefix"] == "2026-12"
        assert years[1]["fiscal_year"] == "FY2027"
        assert years[1]["period_end_prefix"] == "2027-12"
        assert years[0]["other_income_bn"] == model["other_income_bn"]["0y"]
        assert years[1]["other_income_bn"] == model["other_income_bn"]["+1y"]
        built = collect_mod.build_implied_model(
            {
                "earnings_estimate": {
                    "0y": {"avg": 10, "period_end": "2026-12-31", "currency": "USD"},
                },
                "revenue_estimate": {
                    "0y": {"avg": 100e9, "period_end": "2026-12-31", "currency": "USD"},
                },
            },
            model,
        )
        assert built["periods"]["0y"]["status"] == "ok"
        assert built["periods"]["0y"]["fiscal_year"] == "FY2026"
        assert built["periods"]["+1y"]["status"] == "missing_period"

    monkeypatch.setattr(collect_mod, "now_et", lambda: datetime(2026, 3, 1, tzinfo=ET))
    msft = json.loads(collect_mod.research_config_path("MSFT").read_text(encoding="utf-8"))["model"]
    assert msft["fiscal_year_end_month"] == 6
    assert "0001193125-26-323632" in msft["source"]
    assert "0y=FY27（2027-06 止）" in msft["source"]
    parsed = collect_mod.applicable_years(msft)
    assert parsed[0]["fiscal_year"] == "FY27"
    assert parsed[0]["period_end_prefix"] == "2027-06"
    assert parsed[0]["other_income_bn"] == -0.4
    assert parsed[1]["fiscal_year"] == "FY28"
    assert parsed[1]["period_end_prefix"] == "2028-06"
    assert parsed[1]["other_income_bn"] == -0.4


@pytest.mark.parametrize(
    "today",
    [datetime(2026, 12, 15, tzinfo=ET), datetime(2027, 1, 15, tzinfo=ET)],
)
def test_other_income_stays_on_basis_fiscal_year(monkeypatch: pytest.MonkeyPatch, today: datetime) -> None:
    monkeypatch.setattr(collect_mod, "now_et", lambda: today)
    for ticker in ("META", "AMZN", "GOOGL"):
        configured = json.loads(collect_mod.research_config_path(ticker).read_text(encoding="utf-8"))["model"]
        assert configured["basis_fiscal_year"] == 2026

    model = json.loads(collect_mod.research_config_path("AMZN").read_text(encoding="utf-8"))["model"]
    assert collect_mod.other_income_for_fiscal_year(model, 2026) == 69.1
    assert collect_mod.other_income_for_fiscal_year(model, 2027) == 0
    assert collect_mod.other_income_for_fiscal_year(model, 2028) is None

    years = collect_mod.applicable_years(model)
    assert [item["key"] for item in years] == ["0y", "+1y"]
    if today.year == 2026:
        expected = (("FY2026", "2026-12", 69.1), ("FY2027", "2027-12", 0))
        periods = (("0y", "2026-12-31", "ok"), ("+1y", "2027-12-31", "ok"))
    else:
        expected = (("FY2027", "2027-12", 0), ("FY2028", "2028-12", None))
        periods = (("0y", "2027-12-31", "ok"), ("+1y", "2028-12-31", "other_income_missing"))
    for item, (fiscal_year, prefix, income) in zip(years, expected, strict=True):
        assert item["fiscal_year"] == fiscal_year
        assert item["period_end_prefix"] == prefix
        assert item["other_income_bn"] == income
        if income is None:
            assert item["other_income_reason"] == f"{fiscal_year} 没有其他收入假设，未计算隐含费用"
        else:
            assert "other_income_reason" not in item

    built = collect_mod.build_implied_model(
        {
            "earnings_estimate": {
                key: {"avg": 10, "period_end": end, "currency": "USD"} for key, end, _status in periods
            },
            "revenue_estimate": {
                key: {"avg": 100e9, "period_end": end, "currency": "USD"} for key, end, _status in periods
            },
        },
        model,
    )
    for key, _end, status in periods:
        entry = built["periods"][key]
        assert entry["status"] == status
        if status == "ok":
            assert entry["other_income_bn"] == next(item["other_income_bn"] for item in years if item["key"] == key)
            assert "implied_total_expenses_bn" in entry
        else:
            assert entry["fiscal_year"] == "FY2028"
            assert entry["reason"] == "FY2028 没有其他收入假设，未计算隐含费用"
            assert "implied_total_expenses_bn" not in entry

    msft = json.loads(collect_mod.research_config_path("MSFT").read_text(encoding="utf-8"))["model"]
    assert "basis_fiscal_year" not in msft
    parsed = collect_mod.applicable_years(msft)
    assert parsed[0]["fiscal_year"] == "FY27"
    assert parsed[0]["period_end_prefix"] == "2027-06"
    assert parsed[0]["other_income_bn"] == -0.4
    assert parsed[1]["fiscal_year"] == "FY28"
    assert parsed[1]["period_end_prefix"] == "2028-06"
    assert parsed[1]["other_income_bn"] == -0.4
    assert "other_income_reason" not in parsed[0]
    assert "other_income_reason" not in parsed[1]


def test_fetch_consensus_snapshot_handles_empty_and_single_day_history(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd
    import yfinance as yf

    empty_df = pd.DataFrame()
    orig_ticker = yf.Ticker
    orig_download = yf.download
    fake_ticker = create_autospec(orig_ticker, instance=True)
    fake_ticker.earnings_estimate = empty_df
    fake_ticker.revenue_estimate = empty_df
    fake_ticker.eps_trend = empty_df
    fake_ticker.eps_revisions = empty_df
    fake_ticker.analyst_price_targets = {}
    fake_ticker.earnings_dates = empty_df

    # 1) Benchmark has rows, newly listed ticker has 0 rows
    zero_close = pd.DataFrame(
        {"ANT": [float("nan"), float("nan")], "QQQ": [500.0, 505.0]},
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    zero_download = pd.concat({"Close": zero_close}, axis=1)

    # 1) Benchmark has rows, timed-out company has 0 valid close rows -> price error
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=fake_ticker))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=zero_download))
    snap_empty = collect_mod.fetch_consensus_snapshot("ANT", benchmark="QQQ")
    assert snap_empty["ok"] is False
    assert snap_empty["status"] == "error"
    assert "error" in snap_empty["price"]
    assert snap_empty["field_status"]["price"] == "error"
    assert snap_empty["_meta"]["price_symbols"] == {"ANT": "no_rows", "QQQ": "ok"}
    assert snap_empty["earnings_dates"] == []

    # 2) Newly listed ticker has 1 trading day
    one_close = pd.DataFrame(
        {"ANT": [float("nan"), 120.5], "QQQ": [500.0, 505.0]},
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    one_download = pd.concat({"Close": one_close}, axis=1)
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=one_download))
    snap_one = collect_mod.fetch_consensus_snapshot("ANT", benchmark="QQQ")
    assert snap_one["ok"] is True
    assert snap_one["status"] == "empty"
    assert snap_one["price"]["close"] == 120.5
    assert snap_one["price"]["as_of"] == "2026-11-10"
    assert snap_one["price"]["chg_1d_pct"] is None
    assert snap_one["price"]["chg_5d_pct"] is None


def test_price_snapshot_two_and_five_days_marks_5d_unavailable_and_aligns_benchmark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pandas as pd
    import yfinance as yf

    empty_df = pd.DataFrame()
    orig_ticker = yf.Ticker
    orig_download = yf.download
    fake_ticker = create_autospec(orig_ticker, instance=True)
    fake_ticker.earnings_estimate = empty_df
    fake_ticker.revenue_estimate = empty_df
    fake_ticker.eps_trend = empty_df
    fake_ticker.eps_revisions = empty_df
    fake_ticker.analyst_price_targets = {}
    fake_ticker.earnings_dates = empty_df
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=fake_ticker))

    # 2 trading days: +10% 1d change must NOT compute 5d change or trigger 5d threshold;
    # benchmark also has an extra prior day that must be ignored by date alignment.
    two_close = pd.DataFrame(
        {
            "ANT": [float("nan"), 100.0, 110.0],
            "QQQ": [490.0, 500.0, 505.0],
        },
        index=pd.to_datetime(["2026-11-06", "2026-11-09", "2026-11-10"]),
    )
    two_download = pd.concat({"Close": two_close}, axis=1)
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=two_download))
    snap_two = collect_mod.fetch_consensus_snapshot("ANT", benchmark="QQQ")
    assert snap_two["price"]["chg_1d_pct"] == 10.0
    assert snap_two["price"]["bench_chg_1d_pct"] == 1.0
    assert snap_two["price"]["chg_5d_pct"] is None
    assert snap_two["price"]["bench_chg_5d_pct"] is None
    thr = collect_mod.map_thresholds({})
    _deltas, flags = collect_mod.consensus_deltas(snap_two, None, thr)
    assert any("1 日涨跌" in f for f in flags)
    assert not any("5 日涨跌" in f for f in flags)

    # 5 trading days: still < 6, so 5d change stays None (unavailable)
    five_close = pd.DataFrame(
        {
            "ANT": [float("nan"), 100.0, 102.0, 104.0, 106.0, 110.0],
            "QQQ": [490.0, 500.0, 501.0, 502.0, 503.0, 505.0],
        },
        index=pd.to_datetime(["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]),
    )
    five_download = pd.concat({"Close": five_close}, axis=1)
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=five_download))
    snap_five = collect_mod.fetch_consensus_snapshot("ANT", benchmark="QQQ")
    assert snap_five["price"]["chg_1d_pct"] == round((110.0 / 106.0 - 1.0) * 100.0, 2)
    assert snap_five["price"]["chg_5d_pct"] is None
    assert snap_five["price"]["bench_chg_5d_pct"] is None


def test_has_estimates_requires_valid_forecast_values() -> None:
    placeholder = {
        "earnings_estimate": {"0y": {"numberOfAnalysts": 0, "avg": None, "low": None, "high": None}},
        "revenue_estimate": {"0y": {"numberOfAnalysts": 0, "avg": None}},
        "eps_trend": {"0y": {"current": None, "7daysAgo": None}},
        "eps_revisions": {"0y": {"upLast7days": 0, "downLast7Days": 0}},
        "price_targets": {"current": 120.0, "mean": None, "median": None, "low": None, "high": None},
    }
    assert collect_mod._has_estimates(placeholder) is False
    placeholder["earnings_estimate"]["0y"] = {"numberOfAnalysts": 3, "avg": 2.5}
    assert collect_mod._has_estimates(placeholder) is True


@pytest.mark.parametrize("ticker", ["MSFT", "META", "AMZN", "GOOGL"])
def test_legacy_four_tickers_packet_matches_origin_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
) -> None:
    fix_dir = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "bullbear_regression"
    expected_json = (fix_dir / f"{ticker}.packet.json").read_text(encoding="utf-8")
    expected_md = (fix_dir / f"{ticker}.packet.md").read_text(encoding="utf-8")
    cfg = json.loads(collect_mod.research_config_path(ticker).read_text(encoding="utf-8"))
    kw = cfg["alias_terms"][0]
    ae_co = cfg["ae_company_names"][0]
    period_ends = {
        "MSFT": ("2027-06-30", "2028-06-30"),
        "META": ("2026-12-31", "2027-12-31"),
        "AMZN": ("2026-12-31", "2027-12-31"),
        "GOOGL": ("2026-12-31", "2027-12-31"),
    }
    p0, p1 = period_ends[ticker]

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            return {"ok": True, "status": "ok", "data": {"content": f"{kw} revenue growth update.\n", "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            return {"ok": True, "data": {"results": [{"document_id": f"tmtb:{ticker.lower()}-1", "title": f"{ticker} Note", "date": "2026-11-10", "keyword_hits": 2}], "has_more": False}}
        if route in {"sales-notes", "funda-ai", "company-wiki"}:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "alphaengine":
            return {"ok": True, "data": {"results": [{"doc_id": f"alphaengine:ae-{ticker.lower()}-1", "title": f"{ticker} AE Report", "companies": [ae_co], "publish_time": "2026-11-10"}], "has_more": False}}
        if route == "sec":
            if arguments.get("form") == "8-K":
                return {"ok": True, "data": {"rows": [{"accession_no": "0000000001-26-000001", "form": "8-K", "filing_date": "2026-11-10"}]}}
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            if arguments.get("operation") == "search":
                return {"ok": True, "data": {"data": {"items": [{"id": f"x-{ticker.lower()}-1", "text": f"${ticker} EPS and revenue beat", "created_at": "2026-11-10T12:00:00-05:00", "author_handle": "analyst1", "metrics": {"likes": 50, "reposts": 5, "replies": 1, "views": 100}}], "has_more": False}}}
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        raise AssertionError(arguments)

    def cons(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        assert sym == ticker
        return {
            "ok": True,
            "status": "ok",
            "fetched_at": FROZEN.isoformat(),
            "currency": "USD",
            "field_status": {"earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "ok", "price": "ok"},
            "earnings_estimate": {
                "0y": {"avg": 10.0, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30},
                "+1y": {"avg": 12.0, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28},
            },
            "revenue_estimate": {
                "0y": {"avg": 100e9, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30},
                "+1y": {"avg": 120e9, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28},
            },
            "eps_trend": {"+1y": {"current": 12.0, "7daysAgo": 12.0}},
            "eps_revisions": {"+1y": {"upLast7days": 1, "downLast7Days": 0}},
            "price_targets": {"mean": 450.0},
            "price": {"as_of": "2026-11-10", "close": 400.0, "chg_1d_pct": 1.2, "chg_5d_pct": 2.5, "benchmark": benchmark, "bench_chg_1d_pct": 0.4, "bench_chg_5d_pct": 1.0},
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, cons)

    ws = _workspace(tmp_path / ticker, ticker=ticker)
    code, _payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker=ticker,
    )
    assert code == 0
    actual_json = (ws / "research" / "tracker" / "20261110-2250" / "packet.json").read_text(encoding="utf-8")
    actual_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert actual_json == _expected_origin_packet_with_ae_fresh(expected_json)
    assert actual_md == expected_md
    assert "data_status" not in json.loads(actual_json)
    assert "财报日历" not in actual_md


@pytest.mark.parametrize(
    ("ticker", "expected_routes", "expected_forms", "expected_accounts"),
    [
        (
            "ANT",
            ["sales-notes", "tmtb", "funda-ai"],
            ["424B4", "8-K", "S-1", "S-1/A", "10-Q", "4"],
            ["AnthropicAI", "DarioAmodei"],
        ),
        (
            "SPCX",
            ["sales-notes", "tmtb"],
            ["8-K"],
            ["elonmusk", "xai", "SpaceX", "Starlink"],
        ),
    ],
)
def test_collect_ant_and_spcx_sparse_data_and_optional_config_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
    expected_routes: list[str],
    expected_forms: list[str],
    expected_accounts: list[str],
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append((tool_name, arguments))
        route = arguments.get("route")
        if route == "x-timeline":
            if arguments.get("operation") == "tweets":
                handle = arguments.get("handle")
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": f"post-{handle}",
                                    "text": f"{ticker} official update from {handle}: revenue and capex",
                                    "created_at": "2026-11-10T12:00:00-05:00",
                                    "author_handle": handle,
                                    "metrics": {"likes": 50, "reposts": 5, "replies": 1, "views": 100},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    def empty_consensus(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        assert sym == ticker
        return {
            "ok": True,
            "status": "empty",
            "fetched_at": FROZEN.isoformat(),
            "currency": None,
            "field_status": {
                "earnings_estimate": "empty",
                "revenue_estimate": "empty",
                "eps_trend": "empty",
                "eps_revisions": "empty",
                "price_targets": "empty",
                "price": "empty",
            },
            "earnings_estimate": {},
            "revenue_estimate": {},
            "eps_trend": {},
            "eps_revisions": {},
            "price_targets": {},
            "price": {},
            "earnings_dates": [],
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, empty_consensus)

    ws = _workspace(tmp_path / ticker, ticker=ticker)
    run_id = "20261110-2250"
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", run_id],
        ticker=ticker,
    )
    assert code == 0
    assert payload["ok"] is True
    assert payload["ticker"] == ticker
    assert payload["failed_sources"] == []

    packet = _read_json(ws / "research" / "tracker" / run_id / "packet.json")
    assert packet["data_status"] == {
        "price": "暂无",
        "consensus": "暂无",
        "earnings_calendar": "暂无",
    }
    assert packet["implied_model"]["status"] == "empty"
    assert packet["implied_model"]["summary"] == "暂无"

    packet_md = (ws / "research" / "tracker" / run_id / "packet.md").read_text(encoding="utf-8")
    assert "历史行情：暂无" in packet_md
    assert "一致预期：暂无" in packet_md
    assert "财报日历：暂无" in packet_md
    assert "隐含费用：暂无" in packet_md

    cfg = json.loads(collect_mod.research_config_path(ticker).read_text(encoding="utf-8"))
    expected_kw_query = " ".join(cfg["research_keywords"])
    internal_calls = [args for _tool, args in calls if args.get("route") in {"sales-notes", "tmtb", "funda-ai", "company-wiki"}]
    assert [args["route"] for args in internal_calls] == expected_routes
    assert all(args["query"] == expected_kw_query for args in internal_calls)

    sec_calls = [args for _tool, args in calls if args.get("route") == "sec"]
    assert [args["form"] for args in sec_calls] == expected_forms

    tweet_handles = [
        args["handle"]
        for _tool, args in calls
        if args.get("route") == "x-timeline" and args.get("operation") == "tweets"
    ]
    assert tweet_handles == expected_accounts
    for handle in expected_accounts:
        assert packet["x"]["new"][f"x:post-{handle}"]["via"] == "config"


def test_sources_toggle_can_skip_optional_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "mini-rules.md").write_text("# MINI\n", encoding="utf-8")
    (fake_skill / "config" / "MINI.json").write_text(
        json.dumps(
            {
                "ticker": "MINI",
                "name": "MiniCo",
                "research_keywords": ["MiniCo", "MINI"],
                "sources": {
                    "internal": False,
                    "alphaengine": False,
                    "sec": False,
                    "consensus": False,
                    "price": False,
                    "earnings_calendar": False,
                    "news": False,
                    "x": False,
                },
                "card": {
                    "title_prefix": "📈 MINI 跟踪｜",
                    "subtitle": "MiniCo · 多空观点、盈利预测修正与催化剂",
                    "tag": "MINI",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    def no_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        raise AssertionError(f"disabled source called bridge: {tool_name} {arguments}")

    def no_consensus(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
        raise AssertionError("disabled consensus called fetch_consensus_snapshot")

    _patch_bridge(monkeypatch, no_bridge)
    _patch_consensus(monkeypatch, no_consensus)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws", ticker="MINI")
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker="MINI",
    )
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"] == []
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert packet["news"]["status"] == "skipped"
    assert packet["data_status"] == {"price": "暂无", "consensus": "暂无", "earnings_calendar": "已关闭"}


@pytest.mark.parametrize(
    ("enabled_key", "expected_status"),
    [
        ("price", {"price": "ok", "consensus": "暂无", "earnings_calendar": "已关闭"}),
        ("earnings_calendar", {"price": "暂无", "consensus": "暂无", "earnings_calendar": "ok"}),
        ("consensus", {"price": "暂无", "consensus": "ok", "earnings_calendar": "已关闭"}),
    ],
)
def test_independent_source_toggles_price_calendar_consensus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enabled_key: str,
    expected_status: dict[str, str],
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "solo-rules.md").write_text("# SOLO\n", encoding="utf-8")
    (fake_skill / "config" / "SOLO.json").write_text(
        json.dumps(
            {
                "ticker": "SOLO",
                "name": "SoloCo",
                "research_keywords": ["SoloCo", "SOLO"],
                "sources": {
                    "internal": False,
                    "alphaengine": False,
                    "sec": False,
                    "consensus": enabled_key == "consensus",
                    "price": enabled_key == "price",
                    "earnings_calendar": enabled_key == "earnings_calendar",
                    "news": False,
                    "x": False,
                },
                "card": {
                    "title_prefix": "📈 SOLO 跟踪｜",
                    "subtitle": "SoloCo · 多空观点、盈利预测修正与催化剂",
                    "tag": "SOLO",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    orig_download = yf.download
    called = {"ticker": False, "download": False, "consensus": False, "calendar": False}

    class _TickerStub:
        @property
        def earnings_estimate(self):
            called["consensus"] = True
            return pd.DataFrame(
                {"numberOfAnalysts": [5], "avg": [3.0]},
                index=["0y"],
            )

        @property
        def revenue_estimate(self):
            called["consensus"] = True
            return pd.DataFrame()

        @property
        def eps_trend(self):
            called["consensus"] = True
            return pd.DataFrame()

        @property
        def eps_revisions(self):
            called["consensus"] = True
            return pd.DataFrame()

        @property
        def analyst_price_targets(self):
            called["consensus"] = True
            return {"mean": 150.0}

        @property
        def earnings_dates(self):
            called["calendar"] = True
            return pd.DataFrame({"EPS Estimate": [1.2]}, index=pd.to_datetime(["2026-12-01"]))

    def make_ticker(sym: str):
        called["ticker"] = True
        return _TickerStub()

    def make_download(*args, **kwargs):
        called["download"] = True
        close = pd.DataFrame(
            {"SOLO": [100.0, 102.0], "QQQ": [500.0, 505.0]},
            index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
        )
        return pd.concat({"Close": close}, axis=1)

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=make_ticker))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, side_effect=make_download))
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / f"ws-{enabled_key}", ticker="SOLO")
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker="SOLO",
    )
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"] == []
    assert called["download"] is (enabled_key == "price")
    assert called["consensus"] is (enabled_key == "consensus")
    assert called["calendar"] is (enabled_key == "earnings_calendar")
    assert called["ticker"] is (enabled_key in {"consensus", "earnings_calendar"})
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert packet["data_status"] == expected_status

    # Verify undocumented "estimates" alias is NOT honored
    assert collect_mod._source_enabled({"sources": {"estimates": False}}, "consensus") is True


def test_config_x_accounts_require_relevance_and_skip_seen_when_unrelated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "x-timeline":
            if arguments.get("operation") == "tweets" and arguments.get("handle") == "elonmusk":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "meme-offtopic",
                                    "text": "Good morning everyone! Nice weather today.",
                                    "created_at": "2026-11-10T12:00:00-05:00",
                                    "author_handle": "elonmusk",
                                    "metrics": {"likes": 99999, "reposts": 5000, "replies": 100, "views": 1000000},
                                },
                                {
                                    "id": "spcx-ontopic",
                                    "text": "SpaceX Starlink annual revenue continues scaling fast.",
                                    "created_at": "2026-11-10T12:05:00-05:00",
                                    "author_handle": "elonmusk",
                                    "metrics": {"likes": 100, "reposts": 10, "replies": 5, "views": 5000},
                                },
                            ],
                            "has_more": False,
                        }
                    },
                }
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    def empty_cons(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        return {
            "ok": True,
            "status": "empty",
            "fetched_at": FROZEN.isoformat(),
            "earnings_estimate": {},
            "revenue_estimate": {},
            "eps_trend": {},
            "eps_revisions": {},
            "price_targets": {},
            "price": {},
            "earnings_dates": [],
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, empty_cons)

    ws = _workspace(tmp_path / "ws", ticker="SPCX")
    run_id = "20261110-2250"
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", run_id],
        ticker="SPCX",
    )
    assert code == 0 and payload["ok"] is True
    packet = _read_json(ws / "research" / "tracker" / run_id / "packet.json")
    assert "x:spcx-ontopic" in packet["x"]["new"]
    assert packet["x"]["new"]["x:spcx-ontopic"]["via"] == "config"
    assert "x:meme-offtopic" not in packet["x"]["new"]
    assert "x:meme-offtopic" not in packet["x"]["noise_ids"]

    code, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "no", "--summary", "check x"],
        ticker="SPCX",
    )
    assert code == 0
    seen = set(_read_json(ws / "research" / "tracker" / "seen.json"))
    assert "x:spcx-ontopic" in seen
    assert "x:meme-offtopic" not in seen


def test_new_company_without_sources_uses_5d_unavailable_and_missing_benchmark_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "fresh-rules.md").write_text("# FRESH\n", encoding="utf-8")
    (fake_skill / "config" / "FRESH.json").write_text(
        json.dumps(
            {
                "ticker": "FRESH",
                "name": "FreshCo",
                "research_keywords": ["FreshCo", "FRESH"],
                "card": {
                    "title_prefix": "📈 FRESH 跟踪｜",
                    "subtitle": "FreshCo · 多空观点、盈利预测修正与催化剂",
                    "tag": "FRESH",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    orig_download = yf.download
    cal_accessed = False

    class _FreshStub:
        @property
        def earnings_estimate(self):
            return pd.DataFrame()

        @property
        def revenue_estimate(self):
            return pd.DataFrame()

        @property
        def eps_trend(self):
            return pd.DataFrame()

        @property
        def eps_revisions(self):
            return pd.DataFrame()

        @property
        def analyst_price_targets(self):
            return {}

        @property
        def earnings_dates(self):
            nonlocal cal_accessed
            cal_accessed = True
            return pd.DataFrame()

    # 5 trading days: 100,103,106,109,112 and QQQ column missing -> 5d is None, benchmark is None
    close_no_bench = pd.DataFrame(
        {"FRESH": [100.0, 103.0, 106.0, 109.0, 112.0]},
        index=pd.to_datetime(["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]),
    )
    dl_no_bench = pd.concat({"Close": close_no_bench}, axis=1)

    def ok_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        return {"ok": True, "data": {"data": {"items": [], "has_more": False}, "results": [], "rows": []}}

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _FreshStub()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=dl_no_bench))
    _patch_bridge(monkeypatch, ok_bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws", ticker="FRESH")
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker="FRESH",
    )
    assert code == 0 and payload["ok"] is True
    assert cal_accessed is False
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    packet_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "data_status" not in packet
    assert packet["consensus"]["price"]["chg_5d_pct"] is None
    assert packet["consensus"]["price"]["bench_chg_1d_pct"] is None
    assert packet["consensus"]["price"]["bench_chg_5d_pct"] is None
    assert not any("5 日涨跌" in f for f in packet["auto_flags"])
    assert "5 日 None%" in packet_md
    assert "财报日历" not in packet_md


def test_zero_analysts_only_eps_trend_triggers_no_alert_and_empty_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    orig_ticker = yf.Ticker
    orig_download = yf.download

    class _TrendOnlyStub:
        @property
        def earnings_estimate(self):
            return pd.DataFrame(
                {"numberOfAnalysts": [0], "avg": [float("nan")]},
                index=["+1y"],
            )

        @property
        def revenue_estimate(self):
            return pd.DataFrame()

        @property
        def eps_trend(self):
            return pd.DataFrame(
                {"current": [1.25], "7daysAgo": [1.0], "30daysAgo": [1.0]},
                index=["+1y"],
            )

        @property
        def eps_revisions(self):
            return pd.DataFrame(
                {"upLast7days": [0], "downLast7Days": [8]},
                index=["+1y"],
            )

        @property
        def analyst_price_targets(self):
            return {"mean": 500.0}

        @property
        def earnings_dates(self):
            return pd.DataFrame()

    def ok_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        return {"ok": True, "data": {"data": {"items": [], "has_more": False}, "results": [], "rows": []}}

    one_close = pd.DataFrame(
        {"ANT": [100.0, 101.0], "QQQ": [500.0, 505.0]},
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    ok_dl = pd.concat({"Close": one_close}, axis=1)
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _TrendOnlyStub()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=ok_dl))
    _patch_bridge(monkeypatch, ok_bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws-ant", ticker="ANT")
    code, payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker="ANT",
    )
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"] == []
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert packet["consensus"]["status"] == "empty"
    assert packet["data_status"]["consensus"] == "暂无"
    assert packet["deltas"]["eps_+1y_vs_7d_pct"] is None
    assert packet["deltas"]["fy1_revisions_7d"] == {"up": None, "down": None}
    assert packet["auto_flags"] == []


@pytest.mark.parametrize("ticker", ["MSFT", "META", "AMZN", "GOOGL"])
def test_legacy_four_tickers_at_pool_matches_origin_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
) -> None:
    """Fixtures generated directly from origin/dalton/main collect.py with x_pool=@Analyst1."""
    fix_dir = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "bullbear_regression"
    expected_json = (fix_dir / f"{ticker}.pool.packet.json").read_text(encoding="utf-8")
    expected_md = (fix_dir / f"{ticker}.pool.packet.md").read_text(encoding="utf-8")
    cfg = json.loads(collect_mod.research_config_path(ticker).read_text(encoding="utf-8"))
    kw = cfg["alias_terms"][0]
    ae_co = cfg["ae_company_names"][0]
    p0, p1 = {
        "MSFT": ("2027-06-30", "2028-06-30"),
        "META": ("2026-12-31", "2027-12-31"),
        "AMZN": ("2026-12-31", "2027-12-31"),
        "GOOGL": ("2026-12-31", "2027-12-31"),
    }[ticker]

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            return {"ok": True, "status": "ok", "data": {"content": f"{kw} revenue growth update.\n", "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            return {"ok": True, "data": {"results": [{"document_id": f"tmtb:{ticker.lower()}-1", "title": f"{ticker} Note", "date": "2026-11-10", "keyword_hits": 2}], "has_more": False}}
        if route in {"sales-notes", "funda-ai", "company-wiki"}:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "alphaengine":
            return {"ok": True, "data": {"results": [{"doc_id": f"alphaengine:ae-{ticker.lower()}-1", "title": f"{ticker} AE Report", "companies": [ae_co], "publish_time": "2026-11-10"}], "has_more": False}}
        if route == "sec":
            if arguments.get("form") == "8-K":
                return {"ok": True, "data": {"rows": [{"accession_no": "0000000001-26-000001", "form": "8-K", "filing_date": "2026-11-10"}]}}
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            if arguments.get("operation") == "search":
                return {"ok": True, "data": {"data": {"items": [{"id": f"x-{ticker.lower()}-1", "text": f"${ticker} EPS and revenue beat", "created_at": "2026-11-10T12:00:00-05:00", "author_handle": "analyst1", "metrics": {"likes": 50, "reposts": 5, "replies": 1, "views": 100}}], "has_more": False}}}
            if arguments.get("operation") == "tweets" and arguments.get("handle") == "@Analyst1":
                return {"ok": True, "data": {"data": {"items": [{"id": f"x-{ticker.lower()}-pool", "text": f"${ticker} cloud channel check", "created_at": "2026-11-10T13:00:00-05:00", "metrics": {"likes": 80, "reposts": 8, "replies": 2, "views": 300}}], "has_more": False}}}
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        raise AssertionError(arguments)

    def cons(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        return {
            "ok": True, "status": "ok", "fetched_at": FROZEN.isoformat(), "currency": "USD",
            "field_status": {"earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "ok", "price": "ok"},
            "earnings_estimate": {"0y": {"avg": 10.0, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30}, "+1y": {"avg": 12.0, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28}},
            "revenue_estimate": {"0y": {"avg": 100e9, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30}, "+1y": {"avg": 120e9, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28}},
            "eps_trend": {"+1y": {"current": 12.0, "7daysAgo": 12.0}},
            "eps_revisions": {"+1y": {"upLast7days": 1, "downLast7Days": 0}},
            "price_targets": {"mean": 450.0},
            "price": {"as_of": "2026-11-10", "close": 400.0, "chg_1d_pct": 1.2, "chg_5d_pct": 2.5, "benchmark": benchmark, "bench_chg_1d_pct": 0.4, "bench_chg_5d_pct": 1.0},
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, cons)

    ws = _workspace(tmp_path / ticker, ticker=ticker)
    (ws / "research" / "tracker" / "x_pool.json").write_text(
        json.dumps({"@Analyst1": {"active": True, "tickers": [ticker]}}), encoding="utf-8"
    )
    code, _payload = _run(
        capsys,
        ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"],
        ticker=ticker,
    )
    assert code == 0
    actual_json = (ws / "research" / "tracker" / "20261110-2250" / "packet.json").read_text(encoding="utf-8")
    actual_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert actual_json == _expected_origin_packet_with_ae_fresh(expected_json)
    assert actual_md == expected_md


def test_meta_news_off_preserves_legacy_packet_and_top_level_routes_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    orig_meta = json.loads(collect_mod.research_config_path("META").read_text(encoding="utf-8"))
    orig_meta["sources"] = dict(orig_meta["sources"])
    orig_meta["sources"]["news"] = False
    (fake_skill / "config" / "META.json").write_text(json.dumps(orig_meta), encoding="utf-8")
    (fake_skill / "references" / "meta-rules.md").write_text("# META\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    orig_download = yf.download
    cal_called = False

    class _MetaStub:
        @property
        def earnings_estimate(self):
            return pd.DataFrame()

        @property
        def revenue_estimate(self):
            return pd.DataFrame()

        @property
        def eps_trend(self):
            return pd.DataFrame()

        @property
        def eps_revisions(self):
            return pd.DataFrame()

        @property
        def analyst_price_targets(self):
            return {}

        @property
        def earnings_dates(self):
            nonlocal cal_called
            cal_called = True
            return pd.DataFrame({"EPS Estimate": [2.0]}, index=pd.to_datetime(["2026-12-01"]))

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _MetaStub()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=pd.DataFrame()))
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": [], "data": {"items": [], "has_more": False}}})
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws", ticker="META")
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="META")
    assert code == 0
    assert cal_called is False
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    packet_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    # 旧四家过滤掉 sources 下新增开关（含 news=false），始终走主分支默认新闻与配置口径
    assert packet["news"]["status"] == "pending_agent_search"
    assert "data_status" not in packet
    assert "财报日历" not in packet_md
    assert "历史行情：暂无" not in packet_md

    # A main-era config keeps these at the top level; they are read when `sources` has none.
    legacy = collect_mod._filter_legacy_config({"ticker": "META", "internal_routes": ["tmtb"], "sec_forms": ["8-K"]})
    assert collect_mod._effective_internal_routes(legacy) == ["tmtb"]
    assert collect_mod._effective_sec_forms(legacy) == ["8-K"]


@pytest.mark.parametrize("cal_mode", ["disabled", "empty", "timeout"])
def test_earnings_calendar_states_disabled_empty_and_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cal_mode: str,
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "cal-rules.md").write_text("# CAL\n", encoding="utf-8")
    (fake_skill / "config" / "CAL.json").write_text(
        json.dumps(
            {
                "ticker": "CAL",
                "name": "CalCo",
                "sources": {
                    "internal": False,
                    "alphaengine": False,
                    "sec": False,
                    "consensus": False,
                    "price": False,
                    "earnings_calendar": cal_mode != "disabled",
                    "news": False,
                    "x": False,
                },
                "card": {
                    "title_prefix": "📈 CAL 跟踪｜",
                    "subtitle": "CalCo · 多空观点、盈利预测修正与催化剂",
                    "tag": "CAL",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    cal_hits = 0

    class _CalStub:
        @property
        def earnings_dates(self):
            nonlocal cal_hits
            cal_hits += 1
            if cal_mode == "timeout":
                raise TimeoutError("yfinance calendar timed out")
            return pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _CalStub()))
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / f"ws-{cal_mode}", ticker="CAL")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="CAL")
    assert code == 0 and payload["ok"] is True
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    fs = packet["consensus"]["field_status"]
    assert packet["consensus"]["_meta"]["field_status"] == fs
    if cal_mode == "disabled":
        assert cal_hits == 0
        assert fs["earnings_dates"] == "skipped"
        assert packet["data_status"]["earnings_calendar"] == "已关闭"
        assert "财报日历：已关闭" in (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
        assert packet["failed_sources"] == []
    elif cal_mode == "empty":
        assert cal_hits == 1
        assert fs["earnings_dates"] == "empty"
        assert packet["data_status"]["earnings_calendar"] == "暂无"
        assert packet["failed_sources"] == []
    else:
        assert cal_hits == 1
        assert fs["earnings_dates"] == "error"
        assert packet["data_status"]["earnings_calendar"] == "error"
        assert any(item["source"] == "earnings_calendar" for item in packet["failed_sources"])


def test_x_pool_max_keeps_config_accounts_and_filters_search_and_pool_config_authors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = json.loads(collect_mod.research_config_path("SPCX").read_text(encoding="utf-8"))
    timeline_calls: list[str] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "x-timeline":
            op = arguments.get("operation")
            if op == "search":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "cfg-search-offtopic",
                                    "text": "Just saying hello to everyone",
                                    "created_at": "2026-11-10T12:00:00-05:00",
                                    "author_handle": "@SpaceX",
                                    "metrics": {"likes": 5000, "reposts": 200, "replies": 50, "views": 100000},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            handle = str(arguments.get("handle"))
            timeline_calls.append(handle)
            if handle == "elonmusk":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "cfg-pool-offtopic",
                                    "text": "What a lovely dog!",
                                    "created_at": "2026-11-10T12:01:00-05:00",
                                    "author_handle": "elonmusk",
                                    "metrics": {"likes": 8000, "reposts": 300, "replies": 40, "views": 200000},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            if handle == "xai":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "cfg-xai-ontopic",
                                    "text": "xAI Colossus GPU capex expansion",
                                    "created_at": "2026-11-10T12:02:00-05:00",
                                    "author_handle": "xai",
                                    "metrics": {"likes": 200, "reposts": 20, "replies": 5, "views": 5000},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "rows": []}}

    _patch_bridge(monkeypatch, bridge)
    ws = _workspace(tmp_path / "ws", ticker="SPCX")
    # Pool has elonmusk (also in config.x_accounts) + extra_user; with x_pool_max=1 only elonmusk stays in pool,
    # while config.x_accounts (xai, SpaceX, Starlink) must still be appended AFTER pool truncation.
    (ws / "research" / "tracker" / "x_pool.json").write_text(
        json.dumps(
            {
                "elonmusk": {"active": True, "tickers": ["SPCX"]},
                "extra_user": {"active": True, "tickers": ["SPCX"]},
            }
        ),
        encoding="utf-8",
    )
    narrow_cfg = dict(cfg)
    narrow_cfg["x_pool_max"] = 1
    prev_ticker = collect_mod.TICKER
    collect_mod.TICKER = "SPCX"
    try:
        xdata = collect_mod.collect_x(narrow_cfg, "2026-11-09", "2026-11-10", set(), ws)
    finally:
        collect_mod.TICKER = prev_ticker

    assert "extra_user" not in timeline_calls
    assert set(timeline_calls) == {"elonmusk", "xai", "SpaceX", "Starlink"}
    assert xdata["pool_size"] == 1
    pool_rows = [s for s in xdata["status"] if "pool" in s]
    cfg_rows = [s for s in xdata["status"] if "config" in s]
    assert [s["pool"] for s in pool_rows] == ["elonmusk"]
    assert {s["config"] for s in cfg_rows} == {"xai", "SpaceX", "Starlink"}
    assert "x:cfg-xai-ontopic" in xdata["new"]
    assert "x:cfg-pool-offtopic" not in xdata["new"] and "x:cfg-pool-offtopic" not in xdata["noise_ids"]
    assert "x:cfg-search-offtopic" not in xdata["new"] and "x:cfg-search-offtopic" not in xdata["noise_ids"]

    # Also test full collect -> commit flow leaves off-topic config posts out of seen.json
    import pandas as pd
    import yfinance as yf

    orig_ticker = yf.Ticker
    orig_download = yf.download
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: object()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=pd.DataFrame()))
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="SPCX")
    assert code == 0
    code, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "x"],
        ticker="SPCX",
    )
    assert code == 0
    seen = set(_read_json(ws / "research" / "tracker" / "seen.json"))
    assert "x:cfg-xai-ontopic" in seen
    assert "x:cfg-pool-offtopic" not in seen
    assert "x:cfg-search-offtopic" not in seen


def test_yfinance_no_data_exception_maps_to_empty_and_syncs_meta_field_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf
    from yfinance.exceptions import YFTickerMissingError

    orig_ticker = yf.Ticker
    orig_download = yf.download

    class _NoDataStub:
        @property
        def earnings_estimate(self):
            raise YFTickerMissingError("ANT", "Quote not found for symbol: ANT")

        @property
        def revenue_estimate(self):
            raise RuntimeError("HTTP Error 404: Quote not found")

        @property
        def eps_trend(self):
            return None

        @property
        def eps_revisions(self):
            return None

        @property
        def analyst_price_targets(self):
            return {}

        @property
        def earnings_dates(self):
            return None

    one_close = pd.DataFrame(
        {"ANT": [float("nan"), 100.0], "QQQ": [500.0, 505.0]},
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    one_dl = pd.concat({"Close": one_close}, axis=1)

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _NoDataStub()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=one_dl))
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": [], "data": {"items": [], "has_more": False}}})
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws", ticker="ANT")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="ANT")
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"] == []
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert packet["consensus"]["status"] == "empty"
    assert packet["consensus"]["_meta"]["field_status"] == packet["consensus"]["field_status"]
    assert packet["data_status"] == {"price": "ok", "consensus": "暂无", "earnings_calendar": "暂无"}


@pytest.mark.parametrize("ticker", ["MSFT", "META", "AMZN", "GOOGL"])
@pytest.mark.parametrize("scenario", ["degenerate", "target_only", "price_fail"])
def test_legacy_four_tickers_degraded_and_price_fail_match_origin_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
    scenario: str,
) -> None:
    """Fixtures generated directly from origin/dalton/main collect.py for degraded consensus, target-only, and price failure."""
    fix_dir = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "bullbear_regression"
    expected_json = (fix_dir / f"{ticker}.{scenario}.packet.json").read_text(encoding="utf-8")
    expected_md = (fix_dir / f"{ticker}.{scenario}.packet.md").read_text(encoding="utf-8")

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route in {"tmtb", "sales-notes", "funda-ai", "company-wiki", "alphaengine"}:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        raise AssertionError(arguments)

    def cons_deg(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        return {
            "ok": True, "status": "partial", "fetched_at": FROZEN.isoformat(), "currency": "USD",
            "field_status": {"earnings_estimate": "empty", "revenue_estimate": "empty", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "empty", "price": "ok"},
            "earnings_estimate": {"+1y": {"avg": None, "low": None, "high": None, "numberOfAnalysts": 0}},
            "revenue_estimate": {},
            "eps_trend": {"+1y": {"current": 12.5, "7daysAgo": 10.0}},
            "eps_revisions": {"+1y": {"upLast7days": 0, "downLast7Days": 7}},
            "price_targets": {},
            "price": {"as_of": "2026-11-10", "close": 400.0, "chg_1d_pct": 0.5, "chg_5d_pct": 1.0, "benchmark": benchmark, "bench_chg_1d_pct": 0.2, "bench_chg_5d_pct": 0.5},
        }

    def cons_tgt(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        return {
            "ok": False, "status": "error", "fetched_at": FROZEN.isoformat(), "currency": "USD",
            "field_status": {"earnings_estimate": "empty", "revenue_estimate": "empty", "eps_trend": "empty", "eps_revisions": "empty", "price_targets": "ok", "price": "empty"},
            "earnings_estimate": {}, "revenue_estimate": {}, "eps_trend": {}, "eps_revisions": {},
            "price_targets": {"mean": 500.0}, "price": {},
        }

    def cons_pfail(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        return {
            "ok": True, "status": "partial", "fetched_at": FROZEN.isoformat(), "currency": "USD",
            "field_status": {"earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "ok", "price": "error"},
            "earnings_estimate": {"0y": {"avg": 10.0, "period_end": "2026-12-31", "currency": "USD", "numberOfAnalysts": 30}},
            "revenue_estimate": {"0y": {"avg": 100e9, "period_end": "2026-12-31", "currency": "USD", "numberOfAnalysts": 30}},
            "eps_trend": {}, "eps_revisions": {}, "price_targets": {"mean": 450.0},
            "price": {"error": "download timeout"},
        }

    cons_fn = {"degenerate": cons_deg, "target_only": cons_tgt, "price_fail": cons_pfail}[scenario]
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, cons_fn)

    ws = _workspace(tmp_path / f"{ticker}-{scenario}", ticker=ticker)
    if scenario == "target_only":
        prev_time = datetime(2026, 11, 9, 22, 50, tzinfo=ET)
        (ws / "data" / "consensus" / "2026-11-09_2250.json").write_text(
            json.dumps({
                "ok": True, "status": "error", "fetched_at": prev_time.isoformat(), "currency": "USD",
                "earnings_estimate": {}, "revenue_estimate": {}, "eps_trend": {}, "eps_revisions": {},
                "price_targets": {"mean": 400.0}, "price": {},
            }),
            encoding="utf-8",
        )
    code, _payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker=ticker)
    assert code == 0
    actual_json = (ws / "research" / "tracker" / "20261110-2250" / "packet.json").read_text(encoding="utf-8")
    actual_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert actual_json == _expected_origin_packet_with_ae_fresh(expected_json)
    assert actual_md == expected_md


def test_commit_with_delivered_card_and_escaping_rules_symlink_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path / "ws", ticker="ACME" if False else "ANT")
    run_id = "20261110-2250"
    run_dir = ws / "research" / "tracker" / run_id
    run_dir.mkdir(parents=True)
    snap_rel = "data/consensus/2026-11-10_2250.json"
    (ws / snap_rel).write_text(json.dumps({"ok": True, "status": "empty"}), encoding="utf-8")
    (run_dir / "packet.json").write_text(
        json.dumps(
            {
                "ticker": "ANT",
                "run_id": run_id,
                "since": "2026-11-09",
                "until": "2026-11-10",
                "collected_at": FROZEN.isoformat(),
                "snapshot_file": snap_rel,
                "ant_ae_state": _ae_state("2026-11-10", run_id),
                "internal": {},
                "alphaengine": {},
                "sec": {},
                "x": {},
                "collection_complete": True,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
    outside = tmp_path / "outside-rules.md"
    outside.write_text("# outside\n", encoding="utf-8")
    (ws / "research" / "tracker" / "rules.md").symlink_to(outside)

    _patch_clock(monkeypatch, {"now": FROZEN})
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "yes", "--summary", "sent card"],
        ticker="ANT",
    )
    assert code == 0 and payload["ok"] is True


def test_yf_data_exception_and_ticker_init_failure_attribute_only_enabled_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import yfinance as yf
    from yfinance.exceptions import YFDataException

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "calonly-rules.md").write_text("# CALONLY\n", encoding="utf-8")
    (fake_skill / "config" / "CALONLY.json").write_text(
        json.dumps(
            {
                "ticker": "CALONLY",
                "name": "CalOnly",
                "sources": {
                    "internal": False,
                    "alphaengine": False,
                    "sec": False,
                    "consensus": False,
                    "price": False,
                    "earnings_calendar": True,
                    "news": False,
                    "x": False,
                },
                "card": {
                    "title_prefix": "📈 CALONLY 跟踪｜",
                    "subtitle": "CalOnly",
                    "tag": "CALONLY",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)
    orig_ticker = yf.Ticker
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    # 1) YFDataException on earnings_dates -> failure recorded as earnings_calendar (not 暂无)
    class _ParseFailStub:
        @property
        def earnings_dates(self):
            raise YFDataException("Failed to parse earnings table")

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _ParseFailStub()))
    ws1 = _workspace(tmp_path / "ws1", ticker="CALONLY")
    code1, payload1 = _run(capsys, ["collect", "--workspace-dir", str(ws1), "--run-id", "20261110-2250"], ticker="CALONLY")
    assert code1 == 0
    assert payload1["failed_sources"] == [{"source": "earnings_calendar", "error": "Failed to parse earnings table"}]

    # 2) yf.Ticker() init TimeoutError -> attributes to earnings_calendar ONLY, never estimates
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=TimeoutError("init timed out")))
    ws2 = _workspace(tmp_path / "ws2", ticker="CALONLY")
    code2, payload2 = _run(capsys, ["collect", "--workspace-dir", str(ws2), "--run-id", "20261110-2250"], ticker="CALONLY")
    assert code2 == 0
    assert [f["source"] for f in payload2["failed_sources"]] == ["earnings_calendar"]


def test_spcx_x_relevance_filters_all_three_sources_uniformly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "x-timeline":
            op = arguments.get("operation")
            if op == "search":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "search-offtopic",
                                    "text": "Good morning beautiful weekend!",
                                    "created_at": "2026-11-10T12:00:00-05:00",
                                    "author_handle": "random_user",
                                    "metrics": {"likes": 500, "reposts": 20, "replies": 5, "views": 1000},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            handle = str(arguments.get("handle"))
            if handle == "plain_pool":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "pool-offtopic",
                                    "text": "Having coffee at the beach.",
                                    "created_at": "2026-11-10T12:01:00-05:00",
                                    "author_handle": "plain_pool",
                                    "metrics": {"likes": 900, "reposts": 50, "replies": 5, "views": 2000},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            if handle == "elonmusk":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "config-offtopic",
                                    "text": "Happy Friday everyone!",
                                    "created_at": "2026-11-10T12:02:00-05:00",
                                    "author_handle": "elonmusk",
                                    "metrics": {"likes": 9999, "reposts": 900, "replies": 50, "views": 90000},
                                },
                                {
                                    "id": "config-ontopic",
                                    "text": "SpaceX revenue growth update.",
                                    "created_at": "2026-11-10T12:03:00-05:00",
                                    "author_handle": "elonmusk",
                                    "metrics": {"likes": 100, "reposts": 10, "replies": 2, "views": 1000},
                                },
                            ],
                            "has_more": False,
                        }
                    },
                }
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "rows": []}}

    orig_ticker = yf.Ticker
    orig_download = yf.download
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: object()))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=pd.DataFrame()))
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws-spcx", ticker="SPCX")
    (ws / "research" / "tracker" / "x_pool.json").write_text(
        json.dumps({"plain_pool": {"active": True, "tickers": ["SPCX"]}}), encoding="utf-8"
    )
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="SPCX")
    assert code == 0
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert list(packet["x"]["new"]) == ["x:config-ontopic"]
    assert packet["x"]["noise_ids"] == []
    code, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "x"],
        ticker="SPCX",
    )
    assert code == 0
    seen = set(_read_json(ws / "research" / "tracker" / "seen.json"))
    assert "x:config-ontopic" in seen
    for off in ("x:pool-offtopic", "x:config-offtopic", "x:search-offtopic"):
        assert off not in seen


def test_earnings_calendar_exceptions_and_empty_timeout_classified_as_calendar_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    orig_ticker = yf.Ticker
    orig_download = yf.download
    valid_close = pd.DataFrame(
        {"ANT": [100.0, 102.0], "QQQ": [500.0, 505.0]},
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    ok_dl = pd.concat({"Close": valid_close}, axis=1)

    class _CalValueErrStub:
        earnings_estimate = pd.DataFrame()
        revenue_estimate = pd.DataFrame()
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}

        @property
        def earnings_dates(self):
            raise ValueError("Earnings Date column not found in response")

    class _CalEmptyTimeoutStub(_CalValueErrStub):
        @property
        def earnings_dates(self):
            raise TimeoutError()

    class _CalEmptyDfStub(_CalValueErrStub):
        @property
        def earnings_dates(self):
            return pd.DataFrame()

    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": [], "data": {"items": [], "has_more": False}}})
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=ok_dl))

    # 1) ValueError containing "not found" MUST record earnings_calendar failure, not "暂无"
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _CalValueErrStub()))
    ws1 = _workspace(tmp_path / "ws1", ticker="ANT")
    code1, p1 = _run(capsys, ["collect", "--workspace-dir", str(ws1), "--run-id", "20261110-2250"], ticker="ANT")
    assert code1 == 0
    assert p1["failed_sources"] == [
        {"source": "earnings_calendar", "error": "Earnings Date column not found in response"}
    ]
    pk1 = _read_json(ws1 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk1["data_status"]["earnings_calendar"] == "error"

    # 2) TimeoutError() with empty str(exc) uses exception type name
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _CalEmptyTimeoutStub()))
    ws2 = _workspace(tmp_path / "ws2", ticker="ANT")
    code2, p2 = _run(capsys, ["collect", "--workspace-dir", str(ws2), "--run-id", "20261110-2250"], ticker="ANT")
    assert code2 == 0
    assert p2["failed_sources"] == [{"source": "earnings_calendar", "error": "TimeoutError"}]
    pk2 = _read_json(ws2 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk2["consensus"]["earnings_dates_error"] == "TimeoutError"

    # 3) Normal empty DataFrame -> "暂无" and no failed_sources
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=lambda sym: _CalEmptyDfStub()))
    ws3 = _workspace(tmp_path / "ws3", ticker="ANT")
    code3, p3 = _run(capsys, ["collect", "--workspace-dir", str(ws3), "--run-id", "20261110-2250"], ticker="ANT")
    assert code3 == 0
    assert p3["failed_sources"] == []
    pk3 = _read_json(ws3 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk3["data_status"]["earnings_calendar"] == "暂无"


def test_non_legacy_price_failures_record_price_source_only_and_symbol_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    orig_ticker = yf.Ticker
    orig_download = yf.download
    empty_stub = create_autospec(orig_ticker, instance=True)
    empty_stub.earnings_estimate = pd.DataFrame()
    empty_stub.revenue_estimate = pd.DataFrame()
    empty_stub.eps_trend = pd.DataFrame()
    empty_stub.eps_revisions = pd.DataFrame()
    empty_stub.analyst_price_targets = {}
    empty_stub.earnings_dates = pd.DataFrame()
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=empty_stub))
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": [], "data": {"items": [], "has_more": False}}})
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    # 1) Single-symbol timeout returning empty DataFrame -> price failure, NO estimates failure
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=pd.DataFrame()))
    ws1 = _workspace(tmp_path / "ws1", ticker="ANT")
    code1, p1 = _run(capsys, ["collect", "--workspace-dir", str(ws1), "--run-id", "20261110-2250"], ticker="ANT")
    assert code1 == 0
    assert [f["source"] for f in p1["failed_sources"]] == ["price"]
    pk1 = _read_json(ws1 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk1["consensus"]["_meta"]["price_symbols"] == {"ANT": "empty", "QQQ": "empty"}
    assert pk1["data_status"]["price"] == "error"

    # 2) download() raising exception -> price failure, NO estimates failure
    def raise_dl(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError()

    monkeypatch.setattr(yf, "download", create_autospec(orig_download, side_effect=raise_dl))
    ws2 = _workspace(tmp_path / "ws2", ticker="ANT")
    code2, p2 = _run(capsys, ["collect", "--workspace-dir", str(ws2), "--run-id", "20261110-2250"], ticker="ANT")
    assert code2 == 0
    assert p2["failed_sources"] == [{"source": "price", "error": "TimeoutError"}]
    pk2 = _read_json(ws2 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk2["consensus"]["_meta"]["price_symbols"] == {"ANT": "error: TimeoutError", "QQQ": "error: TimeoutError"}

    # 3) Company column missing while benchmark present -> price failure with per-symbol outcomes
    bench_only = pd.concat(
        {"Close": pd.DataFrame({"QQQ": [500.0, 505.0]}, index=pd.to_datetime(["2026-11-09", "2026-11-10"]))},
        axis=1,
    )
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=bench_only))
    ws3 = _workspace(tmp_path / "ws3", ticker="ANT")
    code3, p3 = _run(capsys, ["collect", "--workspace-dir", str(ws3), "--run-id", "20261110-2250"], ticker="ANT")
    assert code3 == 0
    assert [f["source"] for f in p3["failed_sources"]] == ["price"]
    pk3 = _read_json(ws3 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk3["consensus"]["_meta"]["price_symbols"] == {"ANT": "missing_column", "QQQ": "ok"}


def test_non_legacy_implied_model_sanitizes_periods_and_marks_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    for sub in ("config", "references"):
        (fake_skill / sub).mkdir(parents=True)
    cfg = {
        "ticker": "ON",
        "card": {
            "title_prefix": "ON 跟踪",
            "subtitle": "ON Semiconductor",
            "tag": "ON",
            "header_template": "blue",
            "tag_color": "blue",
        },
        "sources": {
            "internal_routes": [],
            "sec_forms": [],
            "alphaengine": False,
            "consensus": True,
            "price": False,
            "earnings_calendar": False,
            "x": False,
            "news": False,
        },
        "model": {
            "diluted_shares_bn": 1.0,
            "tax_rate": 0.2,
            "other_income_bn": {"0y": 0.0, "+1y": 0.0},
            "source": "Yahoo 0y=FY26（2026-12 止），+1y=FY27",
        },
    }
    (fake_skill / "config" / "ON.json").write_text(json.dumps(cfg), encoding="utf-8")
    (fake_skill / "references" / "on-rules.md").write_text("# ON\n", encoding="utf-8")
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": []}})

    class _InvalidConsensusStub:
        earnings_estimate = pd.DataFrame(
            {"avg": [2.0], "numberOfAnalysts": [0], "currency": ["USD"], "period_end": ["2026-12-31"]},
            index=["0y"],
        )
        revenue_estimate = pd.DataFrame(
            {"avg": [8e9], "numberOfAnalysts": [0], "currency": ["USD"], "period_end": ["2026-12-31"]},
            index=["0y"],
        )
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}
        earnings_dates = pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=_InvalidConsensusStub()))
    ws1 = _workspace(tmp_path / "ws1", ticker="ON")
    code1, _ = _run(capsys, ["collect", "--workspace-dir", str(ws1), "--run-id", "20261110-2250"], ticker="ON")
    assert code1 == 0
    pk1 = _read_json(ws1 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk1["implied_model"]["status"] == "empty"
    assert pk1["implied_model"]["summary"] == "暂无"
    assert pk1["implied_model"]["periods"] == {}
    md1 = (ws1 / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "- 隐含费用：暂无" in md1

    # Partial valid: only 0y valid and +1y 0 analysts -> summary "暂无" (both years required)
    class _MixedConsensusStub:
        earnings_estimate = pd.DataFrame(
            {
                "avg": [2.0, 3.0],
                "numberOfAnalysts": [15, 0],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2027-12-31"],
            },
            index=["0y", "+1y"],
        )
        revenue_estimate = pd.DataFrame(
            {
                "avg": [8e9, 10e9],
                "numberOfAnalysts": [15, 0],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2027-12-31"],
            },
            index=["0y", "+1y"],
        )
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}
        earnings_dates = pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=_MixedConsensusStub()))
    ws2 = _workspace(tmp_path / "ws2", ticker="ON")
    code2, _ = _run(capsys, ["collect", "--workspace-dir", str(ws2), "--run-id", "20261110-2250"], ticker="ON")
    assert code2 == 0
    pk2 = _read_json(ws2 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk2["implied_model"]["status"] == "empty"
    assert pk2["implied_model"]["summary"] == "暂无"
    assert pk2["implied_model"]["periods"] == {}
    md2 = (ws2 / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "- 隐含费用：暂无" in md2

    # Both 0y and +1y valid -> computes both years
    class _BothValidStub:
        earnings_estimate = pd.DataFrame(
            {
                "avg": [2.0, 3.0],
                "numberOfAnalysts": [15, 12],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2027-12-31"],
            },
            index=["0y", "+1y"],
        )
        revenue_estimate = pd.DataFrame(
            {
                "avg": [8e9, 10e9],
                "numberOfAnalysts": [15, 12],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2027-12-31"],
            },
            index=["0y", "+1y"],
        )
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}
        earnings_dates = pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=_BothValidStub()))
    ws_both = _workspace(tmp_path / "ws-both", ticker="ON")
    code_b, _ = _run(capsys, ["collect", "--workspace-dir", str(ws_both), "--run-id", "20261110-2250"], ticker="ON")
    assert code_b == 0
    pk_b = _read_json(ws_both / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk_b["implied_model"]["periods"]["0y"]["status"] == "ok"
    assert pk_b["implied_model"]["periods"]["+1y"]["status"] == "ok"

    # +1y period mismatch between EPS (2027-12) and revenue (2028-04) -> whole model marks "暂无"
    class _PeriodMismatchStub:
        earnings_estimate = pd.DataFrame(
            {
                "avg": [2.0, 3.0],
                "numberOfAnalysts": [15, 12],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2027-12-31"],
            },
            index=["0y", "+1y"],
        )
        revenue_estimate = pd.DataFrame(
            {
                "avg": [8e9, 10e9],
                "numberOfAnalysts": [15, 12],
                "currency": ["USD", "USD"],
                "period_end": ["2026-12-31", "2028-04-30"],
            },
            index=["0y", "+1y"],
        )
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}
        earnings_dates = pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=_PeriodMismatchStub()))
    ws_mm = _workspace(tmp_path / "ws-mm", ticker="ON")
    code_mm, _ = _run(capsys, ["collect", "--workspace-dir", str(ws_mm), "--run-id", "20261110-2250"], ticker="ON")
    assert code_mm == 0
    pk_mm = _read_json(ws_mm / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk_mm["implied_model"]["status"] == "empty"
    assert pk_mm["implied_model"]["summary"] == "暂无"
    assert pk_mm["implied_model"]["periods"] == {}
    md_mm = (ws_mm / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "- 隐含费用：暂无" in md_mm

    # Quarter-only valid (0q analysts > 0, no valid 0y/+1y EPS+revenue pair) -> summary "暂无"
    class _QuarterOnlyStub:
        earnings_estimate = pd.DataFrame(
            {"avg": [0.5], "numberOfAnalysts": [15], "currency": ["USD"], "period_end": ["2026-03-31"]},
            index=["0q"],
        )
        revenue_estimate = pd.DataFrame(
            {"avg": [2e9], "numberOfAnalysts": [15], "currency": ["USD"], "period_end": ["2026-03-31"]},
            index=["0q"],
        )
        eps_trend = pd.DataFrame()
        eps_revisions = pd.DataFrame()
        analyst_price_targets: dict[str, Any] = {}
        earnings_dates = pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=_QuarterOnlyStub()))
    ws3 = _workspace(tmp_path / "ws3", ticker="ON")
    code3, _ = _run(capsys, ["collect", "--workspace-dir", str(ws3), "--run-id", "20261110-2250"], ticker="ON")
    assert code3 == 0
    pk3 = _read_json(ws3 / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk3["implied_model"]["status"] == "empty"
    assert pk3["implied_model"]["summary"] == "暂无"
    assert pk3["implied_model"]["periods"] == {}
    md3 = (ws3 / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "- 隐含费用：暂无" in md3


def test_minimal_config_without_aliases_regex_avoids_common_english_false_positives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_skill = tmp_path / "skill"
    for sub in ("config", "references"):
        (fake_skill / sub).mkdir(parents=True)
    # Minimal ON config WITHOUT aliases_regex
    cfg = {
        "ticker": "ON",
        "card": {
            "title_prefix": "ON 跟踪",
            "subtitle": "ON Semiconductor",
            "tag": "ON",
            "header_template": "blue",
            "tag_color": "blue",
        },
        "sources": {
            "internal_routes": ["tmtb"],
            "sec_forms": [],
            "alphaengine": False,
            "consensus": False,
            "price": False,
            "earnings_calendar": False,
            "x": True,
            "news": True,
        },
        "x_accounts": [],
        "x_queries": ["ON Semiconductor"],
        "internal_read_cap_per_route": 10,
    }
    (fake_skill / "config" / "ON.json").write_text(json.dumps(cfg), encoding="utf-8")
    (fake_skill / "references" / "on-rules.md").write_text("# ON\n", encoding="utf-8")
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            doc_id = arguments.get("document")
            text = (
                "Congratulations on another great weekend to everyone on our team."
                if doc_id == "doc-noise"
                else "Update on $ON SiC margin outlook."
            )
            return {"ok": True, "status": "ok", "data": {"content": text, "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"document_id": "doc-noise", "title": "Congratulations on another great weekend", "date": "2026-11-10"},
                        {"document_id": "doc-hit", "title": "$ON guidance review", "date": "2026-11-10"},
                    ],
                    "has_more": False,
                },
            }
        if route == "x-timeline":
            return {
                "ok": True,
                "data": {
                    "data": {
                        "items": [
                            {
                                "id": "x-noise-on",
                                "text": "Congratulations on another great weekend!",
                                "created_at": "2026-11-10T12:00:00-05:00",
                                "author_handle": "random_user",
                                "metrics": {"likes": 500, "reposts": 50},
                            },
                            {
                                "id": "x-oncall-noise",
                                "text": "ON_CALL rotation starts Friday evening.",
                                "created_at": "2026-11-10T12:02:00-05:00",
                                "author_handle": "ops_user",
                                "metrics": {"likes": 100, "reposts": 10},
                            },
                            {
                                "id": "x-dollar-lower",
                                "text": "Looking closely at $on production ramp.",
                                "created_at": "2026-11-10T12:03:00-05:00",
                                "author_handle": "analyst_y",
                                "metrics": {"likes": 40, "reposts": 4},
                            },
                            {
                                "id": "x-hit-on",
                                "text": "Strong quarter from ON today.",
                                "created_at": "2026-11-10T12:05:00-05:00",
                                "author_handle": "analyst_x",
                                "metrics": {"likes": 20, "reposts": 2},
                            },
                        ],
                        "has_more": False,
                    }
                },
            }
        return {"ok": True, "data": {"results": [], "rows": []}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws-on", ticker="ON")
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="ON")
    assert code == 0
    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert list(packet["internal"]["new"]) == ["doc-hit"]
    assert "doc-noise" in packet["internal"]["irrelevant"]
    assert set(packet["x"]["new"]) == {"x:x-hit-on", "x:x-dollar-lower"}
    assert "x:x-oncall-noise" not in packet["x"]["new"]
    assert packet["x"]["noise_ids"] == []

    code_c, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "s"],
        ticker="ON",
    )
    assert code_c == 0
    seen = set(_read_json(ws / "research" / "tracker" / "seen.json"))
    assert {"x:x-hit-on", "x:x-dollar-lower"} <= seen
    assert "x:x-noise-on" not in seen
    assert "x:x-oncall-noise" not in seen
    assert "doc-hit" in seen
    assert "doc-noise" in seen


def test_yfinance_import_error_and_company_timeout_with_benchmark_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins
    import pandas as pd
    import yfinance as yf

    orig_import = builtins.__import__
    orig_ticker = yf.Ticker
    orig_download = yf.download
    _patch_bridge(
        monkeypatch,
        lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": [], "data": {"items": [], "has_more": False}}},
    )
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    def fail_yf_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yfinance":
            raise ImportError("dlopen failed: symbol not found")
        return orig_import(name, *args, **kwargs)

    # 1) import yfinance error containing "not found": price=True -> price failure (no UnboundLocalError); calendar=True -> earnings_calendar failure
    monkeypatch.setattr(builtins, "__import__", fail_yf_import)
    price_only = collect_mod._fetch_configured_market_data(
        "ANT", "QQQ", include_consensus=False, include_price=True, include_earnings_calendar=False
    )
    assert price_only["field_status"]["price"] == "error"
    assert price_only["price"] == {"error": "dlopen failed: symbol not found"}
    assert price_only["_meta"]["price_symbols"] == {
        "ANT": "error: dlopen failed: symbol not found",
        "QQQ": "error: dlopen failed: symbol not found",
    }
    assert collect_mod.failed_sources({}, {}, {}, {}, price_only, legacy_verbatim=False) == [
        {"source": "price", "error": "dlopen failed: symbol not found"}
    ]

    cal_only = collect_mod._fetch_configured_market_data(
        "ANT", "QQQ", include_consensus=False, include_price=False, include_earnings_calendar=True
    )
    assert cal_only["field_status"]["earnings_dates"] == "error"
    assert cal_only["earnings_dates_error"] == "dlopen failed: symbol not found"
    assert collect_mod.failed_sources({}, {}, {}, {}, cal_only, legacy_verbatim=False) == [
        {"source": "earnings_calendar", "error": "dlopen failed: symbol not found"}
    ]
    monkeypatch.setattr(builtins, "__import__", orig_import)

    # 2) Batch download where ANT times out (all NaN) while QQQ succeeds -> price failure, NO estimates failure
    empty_stub = create_autospec(orig_ticker, instance=True)
    empty_stub.earnings_estimate = pd.DataFrame()
    empty_stub.revenue_estimate = pd.DataFrame()
    empty_stub.eps_trend = pd.DataFrame()
    empty_stub.eps_revisions = pd.DataFrame()
    empty_stub.analyst_price_targets = {}
    empty_stub.earnings_dates = pd.DataFrame()
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=empty_stub))

    ant_timeout_df = pd.concat(
        {
            "Close": pd.DataFrame(
                {"ANT": [float("nan"), float("nan")], "QQQ": [500.0, 505.0]},
                index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
            )
        },
        axis=1,
    )
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=ant_timeout_df))
    ws_ant = _workspace(tmp_path / "ws-ant-to", ticker="ANT")
    code_a, pay_a = _run(capsys, ["collect", "--workspace-dir", str(ws_ant), "--run-id", "20261110-2250"], ticker="ANT")
    assert code_a == 0
    assert [f["source"] for f in pay_a["failed_sources"]] == ["price"]
    pk_a = _read_json(ws_ant / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert pk_a["consensus"]["_meta"]["price_symbols"] == {"ANT": "no_rows", "QQQ": "ok"}
    assert pk_a["data_status"]["price"] == "error"

    # 3) Company OK, benchmark missing -> succeeds with bench_chg_* = None
    bench_missing_df = pd.concat(
        {
            "Close": pd.DataFrame(
                {"ANT": [100.0, 105.0]},
                index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
            )
        },
        axis=1,
    )
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=bench_missing_df))
    snap_ok = collect_mod._fetch_configured_market_data("ANT", "QQQ", include_consensus=False, include_price=True)
    assert snap_ok["field_status"]["price"] == "ok"
    assert snap_ok["price"]["chg_1d_pct"] == 5.0
    assert snap_ok["price"]["bench_chg_1d_pct"] is None
    assert snap_ok["_meta"]["price_symbols"] == {"ANT": "ok", "QQQ": "missing_column"}


def test_non_legacy_minimal_config_price_error_renders_request_failed_in_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pandas as pd
    import yfinance as yf

    fake_skill = tmp_path / "skill"
    for sub in ("config", "references"):
        (fake_skill / sub).mkdir(parents=True)
    # Non-legacy config WITHOUT explicit market switches in sources
    cfg = {
        "ticker": "ON",
        "card": {
            "title_prefix": "ON 跟踪",
            "subtitle": "ON Semiconductor",
            "tag": "ON",
            "header_template": "blue",
            "tag_color": "blue",
        },
    }
    (fake_skill / "config" / "ON.json").write_text(json.dumps(cfg), encoding="utf-8")
    (fake_skill / "references" / "on-rules.md").write_text("# ON\n", encoding="utf-8")
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    orig_ticker = yf.Ticker
    orig_download = yf.download
    stub = create_autospec(orig_ticker, instance=True)
    stub.earnings_estimate = pd.DataFrame()
    stub.revenue_estimate = pd.DataFrame()
    stub.eps_trend = pd.DataFrame()
    stub.eps_revisions = pd.DataFrame()
    stub.analyst_price_targets = {}
    stub.earnings_dates = pd.DataFrame()
    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, return_value=stub))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, return_value=pd.DataFrame()))
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": True, "data": {"results": [], "rows": []}})
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws-on-min", ticker="ON")
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="ON")
    assert code == 0
    md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert "- 历史行情：请求失败（" in md
    assert "收盘 None" not in md


@pytest.mark.parametrize("ticker", ["MSFT", "META", "AMZN", "GOOGL"])
def test_legacy_four_tickers_ignore_explicit_sources_price_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
) -> None:
    import pandas as pd
    import yfinance as yf

    fix_dir = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "bullbear_regression"
    expected_json = (fix_dir / f"{ticker}.packet.json").read_text(encoding="utf-8")
    expected_md = (fix_dir / f"{ticker}.packet.md").read_text(encoding="utf-8")

    # Copy real config/references into tmp skill dir and inject sources.price=True
    real_skill = collect_mod.SKILL_DIR
    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    cfg = json.loads((real_skill / "config" / f"{ticker}.json").read_text(encoding="utf-8"))
    cfg["x_accounts"] = ["ReviewAccount"]
    cfg["research_keywords"] = ["ExtraIgnoreKw"]
    cfg.setdefault("sources", {}).update(
        {"price": True, "consensus": False, "earnings_calendar": False, "alphaengine": False}
    )
    (fake_skill / "config" / f"{ticker}.json").write_text(json.dumps(cfg), encoding="utf-8")
    (fake_skill / "references" / "common-rules.md").write_text("# common\n", encoding="utf-8")
    (fake_skill / "references" / f"{ticker.lower()}-rules.md").write_text(f"# {ticker}\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    kw = cfg["alias_terms"][0]
    ae_co = cfg["ae_company_names"][0]
    period_ends = {
        "MSFT": ("2027-06-30", "2028-06-30"),
        "META": ("2026-12-31", "2027-12-31"),
        "AMZN": ("2026-12-31", "2027-12-31"),
        "GOOGL": ("2026-12-31", "2027-12-31"),
    }
    p0, p1 = period_ends[ticker]

    seen_ae_queries: list[str] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("operation") == "read":
            return {"ok": True, "status": "ok", "data": {"content": f"{kw} revenue growth update.\n", "next_offset": None}}
        route = arguments.get("route")
        if route == "tmtb":
            return {"ok": True, "data": {"results": [{"document_id": f"tmtb:{ticker.lower()}-1", "title": f"{ticker} Note", "date": "2026-11-10", "keyword_hits": 2}], "has_more": False}}
        if route in {"sales-notes", "funda-ai", "company-wiki"}:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "alphaengine":
            seen_ae_queries.append(str(arguments.get("query") or ""))
            return {"ok": True, "data": {"results": [{"doc_id": f"alphaengine:ae-{ticker.lower()}-1", "title": f"{ticker} AE Report", "companies": [ae_co], "publish_time": "2026-11-10"}], "has_more": False}}
        if route == "sec":
            if arguments.get("form") == "8-K":
                return {"ok": True, "data": {"rows": [{"accession_no": "0000000001-26-000001", "form": "8-K", "filing_date": "2026-11-10"}]}}
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            assert arguments.get("handle") != "reviewaccount"
            if arguments.get("operation") == "search":
                return {"ok": True, "data": {"data": {"items": [{"id": f"x-{ticker.lower()}-1", "text": f"${ticker} EPS and revenue beat", "created_at": "2026-11-10T12:00:00-05:00", "author_handle": "analyst1", "metrics": {"likes": 50, "reposts": 5, "replies": 1, "views": 100}}], "has_more": False}}}
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        raise AssertionError(arguments)

    def cons(sym: str, benchmark: str = "QQQ") -> dict[str, Any]:
        assert sym == ticker
        return {
            "ok": True,
            "status": "ok",
            "fetched_at": FROZEN.isoformat(),
            "currency": "USD",
            "field_status": {"earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "ok", "price": "ok"},
            "earnings_estimate": {
                "0y": {"avg": 10.0, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30},
                "+1y": {"avg": 12.0, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28},
            },
            "revenue_estimate": {
                "0y": {"avg": 100e9, "period_end": p0, "currency": "USD", "numberOfAnalysts": 30},
                "+1y": {"avg": 120e9, "period_end": p1, "currency": "USD", "numberOfAnalysts": 28},
            },
            "eps_trend": {"+1y": {"current": 12.0, "7daysAgo": 12.0}},
            "eps_revisions": {"+1y": {"upLast7days": 1, "downLast7Days": 0}},
            "price_targets": {"mean": 450.0},
            "price": {"as_of": "2026-11-10", "close": 400.0, "chg_1d_pct": 1.2, "chg_5d_pct": 2.5, "benchmark": benchmark, "bench_chg_1d_pct": 0.4, "bench_chg_5d_pct": 1.0},
        }

    orig_configured = collect_mod._fetch_configured_market_data
    forbid_configured = create_autospec(
        orig_configured,
        side_effect=AssertionError("legacy four must not route through _fetch_configured_market_data"),
    )
    monkeypatch.setattr(collect_mod, "_fetch_configured_market_data", forbid_configured)
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, cons)

    ws = _workspace(tmp_path / f"ws-{ticker}", ticker=ticker)
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker=ticker)
    assert code == 0
    assert len(seen_ae_queries) == 2
    assert forbid_configured.call_count == 0
    actual_json = (ws / "research" / "tracker" / "20261110-2250" / "packet.json").read_text(encoding="utf-8")
    actual_md = (ws / "research" / "tracker" / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    assert actual_json == _expected_origin_packet_with_ae_fresh(expected_json)
    assert actual_md == expected_md

    # Verify request sequence with real _g.fetch_consensus_snapshot: Ticker consensus runs BEFORE download()
    monkeypatch.setattr(collect_mod, "fetch_consensus_snapshot", collect_mod._DEFAULT_FETCH_CONSENSUS)
    call_order: list[str] = []
    orig_ticker = yf.Ticker
    orig_download = yf.download

    def track_ticker(sym: str) -> Any:
        call_order.append("ticker")
        stub = create_autospec(orig_ticker, instance=True)
        stub.earnings_estimate = pd.DataFrame()
        stub.revenue_estimate = pd.DataFrame()
        stub.eps_trend = pd.DataFrame()
        stub.eps_revisions = pd.DataFrame()
        stub.analyst_price_targets = {}
        return stub

    def track_download(*args: Any, **kwargs: Any) -> Any:
        call_order.append("download")
        raise TimeoutError("download timeout")

    monkeypatch.setattr(yf, "Ticker", create_autospec(orig_ticker, side_effect=track_ticker))
    monkeypatch.setattr(yf, "download", create_autospec(orig_download, side_effect=track_download))
    ws_seq = _workspace(tmp_path / f"ws-seq-{ticker}", ticker=ticker)
    code_seq, pay_seq = _run(capsys, ["collect", "--workspace-dir", str(ws_seq), "--run-id", "20261110-2250"], ticker=ticker)
    assert code_seq == 0
    assert call_order[:2] == ["ticker", "download"]
    assert [f["source"] for f in pay_seq["failed_sources"]] == ["estimates"]


def test_short_dotted_ticker_pattern_requires_uppercase_bare_and_ignores_case_with_dollar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_skill = tmp_path / "skill"
    for sub in ("config", "references"):
        (fake_skill / sub).mkdir(parents=True)
    cfg = {
        "ticker": "BF.B",
        "card": {
            "title_prefix": "BF.B 跟踪",
            "subtitle": "Brown-Forman Class B",
            "tag": "BF.B",
            "header_template": "blue",
            "tag_color": "blue",
        },
        "sources": {
            "internal_routes": [],
            "sec_forms": [],
            "alphaengine": False,
            "consensus": False,
            "price": False,
            "earnings_calendar": False,
            "x": True,
            "news": False,
        },
        "x_accounts": [],
        "x_queries": ["BF.B"],
        "research_keywords": ["Brown-Forman"],
    }
    (fake_skill / "config" / "BF.B.json").write_text(json.dumps(cfg), encoding="utf-8")
    (fake_skill / "references" / "bf.b-rules.md").write_text("# BF.B\n", encoding="utf-8")
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("route") == "x-timeline":
            return {
                "ok": True,
                "data": {
                    "data": {
                        "items": [
                            {"id": "bare-lower", "text": "bf.b slipped slightly today", "created_at": "2026-11-10T12:00:00-05:00", "author_handle": "a1", "metrics": {"likes": 10}},
                            {"id": "underscore-hit", "text": "BF.B_CALL alert only", "created_at": "2026-11-10T12:01:00-05:00", "author_handle": "a2", "metrics": {"likes": 10}},
                            {"id": "bare-upper", "text": "BF.B steady on volume", "created_at": "2026-11-10T12:02:00-05:00", "author_handle": "a3", "metrics": {"likes": 10}},
                            {"id": "dollar-lower", "text": "Just added $bf.b today", "created_at": "2026-11-10T12:03:00-05:00", "author_handle": "a4", "metrics": {"likes": 10}},
                            {"id": "dollar-upper", "text": "Watching $BF.B closely", "created_at": "2026-11-10T12:04:00-05:00", "author_handle": "a5", "metrics": {"likes": 10}},
                            {"id": "alias-name", "text": "Brown-Forman raises outlook", "created_at": "2026-11-10T12:05:00-05:00", "author_handle": "a6", "metrics": {"likes": 10}},
                        ],
                        "has_more": False,
                    }
                },
            }
        return {"ok": True, "data": {"results": [], "rows": []}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})

    ws = _workspace(tmp_path / "ws-bfb", ticker="BF.B")
    code, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"], ticker="BF.B")
    assert code == 0
    pk = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert set(pk["x"]["new"]) == {"x:bare-upper", "x:dollar-lower", "x:dollar-upper", "x:alias-name"}
    assert "x:bare-lower" not in pk["x"]["new"]
    assert "x:underscore-hit" not in pk["x"]["new"]
    assert pk["x"]["noise_ids"] == []


def test_top_level_routes_and_forms_from_a_main_era_config_are_still_read():
    """A config written for origin/dalton/main keeps internal_routes / sec_forms at the top level."""
    # Through the same filter the collector applies to the legacy four.
    cfg = collect_mod._filter_legacy_config(
        {"ticker": "MSFT", "internal_routes": ["tmtb", "wiki"], "sec_forms": ["8-K", "10-Q"]}
    )
    assert collect_mod._effective_internal_routes(cfg) == ["tmtb", "wiki"]
    assert collect_mod._effective_sec_forms(cfg) == ["8-K", "10-Q"]
    nested = collect_mod._filter_legacy_config(
        {"ticker": "MSFT", "internal_routes": ["old"], "sources": {"internal_routes": ["new"], "sec_forms": ["4"]}}
    )
    assert collect_mod._effective_internal_routes(nested) == ["new"]
    assert collect_mod._effective_sec_forms(nested) == ["4"]
    # A new company never falls back to top-level lists: they are read from `sources` only.
    ant = {"ticker": "ANT", "internal_routes": ["tmtb"], "sec_forms": ["8-K"], "sources": {"news": True}}
    assert collect_mod._effective_internal_routes(ant) == []
    assert collect_mod._effective_sec_forms(ant) == []


def test_annual_period_pair_needs_the_same_end_date_not_just_the_same_month():
    eps = {"+1y": {"period_end": "2027-12-31"}}
    same_month = {"+1y": {"period_end": "2027-12-30"}}
    same_day = {"+1y": {"period_end": "2027-12-31"}}
    assert collect_mod._valid_annual_period_pair(eps, same_month, "+1y") is False
    assert collect_mod._valid_annual_period_pair(eps, same_day, "+1y") is True


def test_aliases_that_differ_only_in_case_from_each_other_are_both_kept():
    pattern = collect_mod._aliases_pattern(
        {"ticker": "BF.B", "research_keywords": ["Bf"], "alias_terms": ["BF"]}
    )
    assert pattern.search("Watching BF today")
    assert pattern.search("$bf.b ramp")
    assert not pattern.search("bf.b lowercase without dollar")


def test_ae_same_run_multipage_and_no_cursor_in_last_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """一轮内翻多页直到 has_more=false，首页带 fresh=True，last_run.json 里没有 cursor。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    ae_calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "alphaengine":
            ae_calls.append(arguments)
            cur = (arguments.get("params") or {}).get("cursor")
            if arguments["query"] == first:
                if cur is None:
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:q1-p1", "title": "Azure p1", "companies": ["Microsoft"], "publish_time": "2026-11-09"}],
                            "has_more": True,
                            "cursor": "ae1:q1-c1",
                        },
                    }
                if cur == "ae1:q1-c1":
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:q1-p2", "title": "Azure p2", "companies": ["Microsoft"], "publish_time": "2026-11-10"}],
                            "has_more": True,
                            "cursor": "ae1:q1-c2",
                        },
                    }
                if cur == "ae1:q1-c2":
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:q1-p3", "title": "Azure p3", "companies": ["Microsoft"], "publish_time": "2026-11-10"}],
                            "has_more": False,
                        },
                    }
                raise AssertionError(cur)
            if cur is None:
                return {
                    "ok": True,
                    "data": {
                        "results": [{"doc_id": "alphaengine:q2-p1", "title": "Copilot p1", "companies": ["Microsoft"], "publish_time": "2026-11-10"}],
                        "has_more": True,
                        "cursor": "ae1:q2-c1",
                    },
                }
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:q2-p2", "title": "Copilot p2", "companies": ["Microsoft"], "publish_time": "2026-11-10"}],
                    "has_more": False,
                },
            }
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)

    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"])
    assert code == 0 and payload["ok"] is True
    assert payload["failed_sources"] == []
    assert payload["counts"]["alphaengine"] == 5

    q1_calls = [c for c in ae_calls if c["query"] == first]
    q2_calls = [c for c in ae_calls if c["query"] == second]
    assert len(q1_calls) == 3 and len(q2_calls) == 2
    assert q1_calls[0]["params"] == {"sort": "relevance", "fresh": True}
    assert q1_calls[1]["params"] == {"sort": "relevance", "cursor": "ae1:q1-c1"}
    assert q1_calls[2]["params"] == {"sort": "relevance", "cursor": "ae1:q1-c2"}
    assert q2_calls[0]["params"] == {"sort": "relevance", "fresh": True}
    assert q2_calls[1]["params"] == {"sort": "relevance", "cursor": "ae1:q2-c1"}

    packet = _read_json(ws / "research" / "tracker" / "20261110-2250" / "packet.json")
    assert set(packet["alphaengine"]["new"]) == {
        "alphaengine:q1-p1", "alphaengine:q1-p2", "alphaengine:q1-p3",
        "alphaengine:q2-p1", "alphaengine:q2-p2",
    }
    assert all(row["complete"] is True and row["covered_until"] == "2026-11-10" for row in packet["msft_ae_state"]["queries"])

    code, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "multipage"],
    )
    assert code == 0
    last_text = (ws / "research" / "tracker" / "last_run.json").read_text(encoding="utf-8")
    last = json.loads(last_text)
    for row in last["msft_ae_state"]["queries"]:
        assert "cursor" not in row
        assert "resumed_cursor" not in row
        assert row["covered_until"] == "2026-11-10"
    assert "ae1:" not in last_text


def test_ae_ignores_saved_cursor_in_old_state_and_starts_page_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧状态文件里带 cursor 与锁死的旧 window.end 时，本轮忽略 cursor、从第一页带 fresh=True 开始并推进至 until。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    old_last_run = {
        "msft_ae_state": {
            "queries": [
                {
                    "query": first,
                    "window": {"start": "2026-11-01", "end": "2026-11-05"},
                    "cursor": "ae1:expired-from-yesterday-1",
                    "has_more": True,
                    "complete": False,
                    "covered_until": "2026-11-04",
                    "ok": True,
                },
                {
                    "query": second,
                    "window": {"start": "2026-11-01", "end": "2026-11-05"},
                    "cursor": "ae1:expired-from-yesterday-2",
                    "has_more": True,
                    "complete": False,
                    "covered_until": "2026-11-04",
                    "ok": True,
                },
            ]
        }
    }

    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), old_last_run)
    assert len(calls) == 2
    for req in calls:
        assert req["start"] == "2026-11-01"
        assert req["end"] == "2026-11-10"
        assert req["params"] == {"sort": "relevance", "fresh": True}
        assert "cursor" not in req["params"]
    assert "expired-from-yesterday" not in json.dumps(calls)
    assert all(row["complete"] is True and row["covered_until"] == "2026-11-10" for row in state["queries"])
    assert collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"}) == []


def test_ae_same_run_cursor_expired_restarts_once_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """同轮第 2 页遇到 E_CURSOR_EXPIRED / E_CURSOR_FILTER_MISMATCH → 带 fresh=True 从首页重扫一次 → 成功且无脚注。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    calls: list[dict[str, Any]] = []
    counts = {first: 0, second: 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls.append(arguments)
        q = arguments["query"]
        counts[q] += 1
        step = counts[q]
        if q == first:
            if step == 1:
                assert arguments["params"] == {"sort": "relevance", "fresh": True}
                return {
                    "ok": True,
                    "data": {
                        "results": [{"doc_id": "alphaengine:d1", "title": "Microsoft d1", "companies": ["Microsoft"]}],
                        "has_more": True,
                        "cursor": "ae1:old-c1",
                    },
                }
            if step == 2:
                assert arguments["params"] == {"sort": "relevance", "cursor": "ae1:old-c1"}
                return {
                    "ok": False,
                    "error": "E_CURSOR_EXPIRED: 搜索游标不存在或已超过 15 分钟，请重新检索",
                    "data": {"error_code": "E_CURSOR_EXPIRED"},
                }
            if step == 3:
                assert arguments["params"] == {"sort": "relevance", "fresh": True}
                return {
                    "ok": True,
                    "data": {
                        "results": [
                            {"doc_id": "alphaengine:d1", "title": "Microsoft d1", "companies": ["Microsoft"]},
                            {"doc_id": "alphaengine:d2", "title": "Microsoft d2", "companies": ["Microsoft"]},
                        ],
                        "has_more": True,
                        "cursor": "ae1:new-c2",
                    },
                }
            if step == 4:
                assert arguments["params"] == {"sort": "relevance", "cursor": "ae1:new-c2"}
                return {
                    "ok": True,
                    "data": {
                        "results": [{"doc_id": "alphaengine:d3", "title": "Microsoft d3", "companies": ["Microsoft"]}],
                        "has_more": False,
                    },
                }
            raise AssertionError(step)

        if step == 1:
            assert arguments["params"] == {"sort": "relevance", "fresh": True}
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:d4", "title": "Azure d4", "companies": ["Microsoft"]}],
                    "has_more": True,
                    "cursor": "ae1:mismatch-c1",
                },
            }
        if step == 2:
            assert arguments["params"] == {"sort": "relevance", "cursor": "ae1:mismatch-c1"}
            return {
                "ok": False,
                "error": "E_CURSOR_FILTER_MISMATCH: 翻页时 query 与筛选条件必须保持不变",
                "data": {"error_code": "E_CURSOR_FILTER_MISMATCH"},
            }
        if step == 3:
            assert arguments["params"] == {"sort": "relevance", "fresh": True}
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": "alphaengine:d4", "title": "Azure d4", "companies": ["Microsoft"]},
                        {"doc_id": "alphaengine:d5", "title": "Azure d5", "companies": ["Microsoft"]},
                    ],
                    "has_more": False,
                },
            }
        raise AssertionError(step)

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_run)
    assert counts == {first: 4, second: 3}
    assert set(ae["new"]) == {
        "alphaengine:d1", "alphaengine:d2", "alphaengine:d3", "alphaengine:d4", "alphaengine:d5"
    }
    assert all(row["ok"] is True and row["complete"] is True and row["covered_until"] == "2026-11-10" for row in state["queries"])
    assert collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"}) == []


def test_ae_same_run_cursor_expired_twice_keeps_items_without_marking_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同轮重扫后再次游标失效：保留已取条目，ok=True（不记整段失败），记录未扫完原因与未覆盖日期范围。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    counts = {first: 0, second: 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        q = arguments["query"]
        counts[q] += 1
        if q == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        step = counts[q]
        if step == 1:
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:keep-1", "title": "Microsoft keep 1", "companies": ["Microsoft"]}],
                    "has_more": True,
                    "cursor": "ae1:c1",
                },
            }
        if step == 2:
            return {"ok": False, "error": "E_CURSOR_EXPIRED: expired 1", "data": {"error_code": "E_CURSOR_EXPIRED"}}
        if step == 3:
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": "alphaengine:keep-1", "title": "Microsoft keep 1", "companies": ["Microsoft"]},
                        {"doc_id": "alphaengine:keep-2", "title": "Microsoft keep 2", "companies": ["Microsoft"]},
                    ],
                    "has_more": True,
                    "cursor": "ae1:c2",
                },
            }
        if step == 4:
            return {"ok": False, "error": "E_CURSOR_EXPIRED: expired 2", "data": {"error_code": "E_CURSOR_EXPIRED"}}
        raise AssertionError(step)

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_run)
    assert counts[first] == 4
    assert {"alphaengine:keep-1", "alphaengine:keep-2"} == set(ae["new"])
    by_q = {row["query"]: row for row in state["queries"]}
    row1 = by_q[first]
    assert row1["ok"] is True
    assert row1["complete"] is False
    assert row1["covered_until"] == "2026-11-09"
    assert row1["cursor"] is None
    assert "游标失效重扫后仍未扫完" in row1["error"]
    assert "未覆盖日期：2026-11-10 至 2026-11-10" in row1["error"]
    failures = collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"})
    assert len(failures) == 1
    assert failures[0]["source"] == "alphaengine"
    assert "游标失效重扫后仍未扫完" in failures[0]["error"]
    assert "2026-11-10 至 2026-11-10" in failures[0]["error"]


def test_ae_page_cap_advances_only_completed_days_and_next_run_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """达到翻页上限：covered_until 只推进到已扫完的日期，脚注写明未覆盖范围，下一轮从那里继续。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    phase = {"round": 1}
    ae_calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "alphaengine":
            ae_calls.append(arguments)
            q = arguments["query"]
            if q == second:
                return {"ok": True, "data": {"results": [], "has_more": False}}
            if phase["round"] == 1:
                # 整段 2026-11-03..2026-11-10 返回 total_upstream=200，立即转按日切分（消耗 1 页，剩余 4 页）
                if arguments["start"] == "2026-11-03" and arguments["end"] == "2026-11-10":
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:whole-1", "title": "Azure whole", "companies": ["Microsoft"]}],
                            "has_more": True,
                            "cursor": "ae1:whole",
                            "total_upstream": 200,
                        },
                    }
                # 2026-11-07 子窗口（含回看 11-03..11-07）：1 页扫完 -> covered_until 推进至 2026-11-07
                if arguments["end"] == "2026-11-07":
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:d07", "title": "Azure 07", "companies": ["Microsoft"], "publish_time": "2026-11-07"}],
                            "has_more": False,
                        },
                    }
                # 2026-11-08 单日：1 页扫完 -> covered_until 推进至 2026-11-08
                if arguments["end"] == "2026-11-08":
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": "alphaengine:d08", "title": "Azure 08", "companies": ["Microsoft"], "publish_time": "2026-11-08"}],
                            "has_more": False,
                        },
                    }
                # 2026-11-09 单日：连续返回 has_more=True，用完剩余 2 页（总计 5 页上限），未扫完
                if arguments["end"] == "2026-11-09":
                    cur = (arguments.get("params") or {}).get("cursor")
                    return {
                        "ok": True,
                        "data": {
                            "results": [{"doc_id": f"alphaengine:d09-{cur or 'p1'}", "title": "Azure 09", "companies": ["Microsoft"], "publish_time": "2026-11-09"}],
                            "has_more": True,
                            "cursor": "ae1:d09-next",
                        },
                    }
                raise AssertionError(arguments)
            # 第 2 轮：从 covered_until(2026-11-08) - 3 = 2026-11-05 开始到 2026-11-11，全部扫完
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:r2-done", "title": "Azure round 2", "companies": ["Microsoft"]}],
                    "has_more": False,
                },
            }
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    clock = {"now": FROZEN}
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", 5)
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, clock)
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"
    (tracker / "last_run.json").write_text(
        json.dumps({
            "ticker": "MSFT",
            "run_id": "20261106-2200",
            "until": "2026-11-06",
            "msft_ae_state": {
                "queries": [
                    {"query": q, "complete": True, "covered_until": "2026-11-06", "ok": True, "window": {"start": "2026-11-03", "end": "2026-11-06"}}
                    for q in (first, second)
                ]
            },
        }),
        encoding="utf-8",
    )

    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--since", "2026-11-09"])
    assert code == 0 and payload["ok"] is True
    q1_calls = [c for c in ae_calls if c["query"] == first]
    assert len(q1_calls) == 1 + 1 + 1 + 2

    packet = _read_json(tracker / "20261110-2250" / "packet.json")
    packet_md = (tracker / "20261110-2250" / "packet.md").read_text(encoding="utf-8")
    by_q = {row["query"]: row for row in packet["msft_ae_state"]["queries"]}
    assert by_q[first]["complete"] is False
    assert by_q[first]["ok"] is True
    assert by_q[first]["covered_until"] == "2026-11-08"
    assert by_q[first]["cursor"] is None
    assert by_q[second]["complete"] is True
    assert by_q[second]["covered_until"] == "2026-11-10"
    assert {"alphaengine:whole-1", "alphaengine:d07", "alphaengine:d08", "alphaengine:d09-p1", "alphaengine:d09-ae1:d09-next"} <= set(packet["alphaengine"]["new"])
    assert len(payload["failed_sources"]) == 1
    err = payload["failed_sources"][0]["error"]
    assert "本轮请求次数已用完，2026-11-09 起留待下一轮" in err
    assert "覆盖边界已推进至 2026-11-08" in err
    assert "未覆盖日期：2026-11-09 至 2026-11-10" in err
    assert "本轮请求次数已用完，2026-11-09 起留待下一轮" in packet_md and "未覆盖日期：2026-11-09 至 2026-11-10" in packet_md

    code, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "cap-partial"],
    )
    assert code == 0
    last = _read_json(tracker / "last_run.json")
    saved_by_q = {row["query"]: row for row in last["msft_ae_state"]["queries"]}
    assert saved_by_q[first]["covered_until"] == "2026-11-08"
    assert "cursor" not in saved_by_q[first]

    # 下一轮从 covered_until(2026-11-08) - AE_LOOKBACK_DAYS(3) = 2026-11-05 继续到新 until(2026-11-11)
    ae_calls.clear()
    phase["round"] = 2
    clock["now"] = datetime(2026, 11, 11, 22, 50, tzinfo=ET)
    code, payload2 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261111-2250"])
    assert code == 0 and payload2["failed_sources"] == []
    q1_r2 = [c for c in ae_calls if c["query"] == first]
    assert len(q1_r2) == 1
    assert q1_r2[0]["start"] == "2026-11-05"
    assert q1_r2[0]["end"] == "2026-11-11"
    assert q1_r2[0]["params"] == {"sort": "relevance", "fresh": True}
    packet2 = _read_json(tracker / "20261111-2250" / "packet.json")
    assert all(row["complete"] is True and row["covered_until"] == "2026-11-11" for row in packet2["msft_ae_state"]["queries"])


def test_ae_window_advances_to_until_after_normal_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """正常完成后窗口 covered_until 推进到本轮 until，下一轮基于新 covered_until - AE_LOOKBACK_DAYS 推进到新 until。"""
    ae_calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "alphaengine":
            ae_calls.append(arguments)
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    clock = {"now": FROZEN}
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, clock)
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)
    ws = _workspace(tmp_path)
    tracker = ws / "research" / "tracker"

    code, payload1 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-2250"])
    assert code == 0 and payload1["failed_sources"] == []
    code, _ = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "r1"])
    assert code == 0
    last1 = _read_json(tracker / "last_run.json")
    assert all(row["covered_until"] == "2026-11-10" and "cursor" not in row for row in last1["msft_ae_state"]["queries"])

    ae_calls.clear()
    clock["now"] = datetime(2026, 11, 13, 22, 50, tzinfo=ET)
    code, payload2 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261113-2250", "--since", "2026-11-11"])
    assert code == 0 and payload2["failed_sources"] == []
    assert len(ae_calls) == 2
    assert {c["start"] for c in ae_calls} == {"2026-11-07"}
    assert {c["end"] for c in ae_calls} == {"2026-11-13"}
    assert all(c["params"] == {"sort": "relevance", "fresh": True} for c in ae_calls)
    code, _ = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261113-2250", "--material", "no", "--summary", "r2"])
    assert code == 0
    last2 = _read_json(tracker / "last_run.json")
    assert all(row["covered_until"] == "2026-11-13" and "cursor" not in row for row in last2["msft_ae_state"]["queries"])


@pytest.mark.parametrize(
    "trigger",
    ["hit_page_cap", "early_exit", "partial_status", "truncation_warning", "has_more_no_cursor"],
)
def test_ae_all_incomplete_full_scans_enter_day_split(
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    """整段扫不完的所有情况（页上限、early_exit、status=partial、截断警告、has_more 无 cursor）都进入按日续采。

    复现 codex 高 1：11-05 已覆盖，11-06 有 21 条（2 页）、11-07 有 69 条（4 页）、11-08 有 5 条（1 页），
    每未覆盖日有自己的页预算，首个子窗口只含 11-06（加 3 天回看），一轮即可全部扫完并推进到 11-08。
    """
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    ae_calls: list[dict[str, Any]] = []
    full_pages = {"n": 0}
    day_pages: dict[str, int] = {"2026-11-06": 0, "2026-11-07": 0, "2026-11-08": 0}

    def _make_docs(prefix: str, count: int, day: str) -> list[dict[str, Any]]:
        return [
            {
                "doc_id": f"alphaengine:{prefix}-{i}",
                "title": f"Microsoft {prefix} {i}",
                "companies": ["Microsoft"],
                "publish_time": day,
            }
            for i in range(count)
        ]

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        ae_calls.append(arguments)
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            full_pages["n"] += 1
            if trigger == "hit_page_cap":
                return {
                    "ok": True,
                    "data": {
                        "results": _make_docs(f"full-p{full_pages['n']}", 20, "2026-11-07"),
                        "has_more": True,
                        "cursor": f"ae1:full-c{full_pages['n']}",
                    },
                }
            if trigger == "early_exit":
                return {
                    "ok": True,
                    "data": {
                        "results": _make_docs("full-early", 20, "2026-11-07"),
                        "has_more": True,
                        "cursor": "ae1:full-early",
                        "total_upstream": 150,
                    },
                }
            if trigger == "partial_status":
                return {
                    "ok": True,
                    "status": "partial",
                    "data": {
                        "ok": True,
                        "status": "partial",
                        "results": _make_docs("full-partial", 5, "2026-11-07"),
                        "has_more": False,
                    },
                }
            if trigger == "truncation_warning":
                return {
                    "ok": True,
                    "data": {
                        "results": _make_docs("full-warn", 5, "2026-11-07"),
                        "warnings": [WIKI_WARNING],
                        "has_more": False,
                    },
                }
            return {
                "ok": True,
                "data": {
                    "results": _make_docs("full-nocursor", 20, "2026-11-07"),
                    "has_more": True,
                    "cursor": None,
                },
            }

        sub_end = arguments["end"]
        day_pages[sub_end] += 1
        p = day_pages[sub_end]
        if sub_end == "2026-11-06":
            assert arguments["start"] == "2026-11-03"
            if p == 1:
                return {"ok": True, "data": {"results": _make_docs("d06-p1", 20, "2026-11-06"), "has_more": True, "cursor": "ae1:d06-c1"}}
            return {"ok": True, "data": {"results": _make_docs("d06-p2", 1, "2026-11-06"), "has_more": False}}
        if sub_end == "2026-11-07":
            assert arguments["start"] == "2026-11-07"
            if p < 4:
                return {"ok": True, "data": {"results": _make_docs(f"d07-p{p}", 20, "2026-11-07"), "has_more": True, "cursor": f"ae1:d07-c{p}"}}
            return {"ok": True, "data": {"results": _make_docs("d07-p4", 9, "2026-11-07"), "has_more": False}}
        if sub_end == "2026-11-08":
            assert arguments["start"] == "2026-11-08"
            return {"ok": True, "data": {"results": _make_docs("d08-p1", 5, "2026-11-08"), "has_more": False}}
        raise AssertionError(arguments)

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_run)
    by_q = {row["query"]: row for row in state["queries"]}
    assert by_q[first]["ok"] is True
    assert by_q[first]["complete"] is True
    assert by_q[first]["covered_until"] == "2026-11-08"
    assert by_q[first]["error"] is None
    assert day_pages == {"2026-11-06": 2, "2026-11-07": 4, "2026-11-08": 1}
    assert "alphaengine:d06-p2-0" in ae["new"]
    assert "alphaengine:d07-p4-8" in ae["new"]
    assert "alphaengine:d08-p1-4" in ae["new"]
    assert collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"}) == []


def test_ae_day_split_respects_per_query_round_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """按日续采受每条查询每轮最多 40 次请求上限约束，已完成日期照常推进并写出未覆盖范围。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    q1_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(collect_mod, "AE_MAX_BACKLOG_DAYS", 60)

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        q1_calls.append(arguments)
        if arguments["start"] == "2026-09-22" and arguments["end"] == "2026-11-10":
            return {
                "ok": True,
                "data": {"results": [], "has_more": True, "cursor": "ae1:big", "total_upstream": 500},
            }
        return {
            "ok": True,
            "data": {
                "results": [{"doc_id": f"alphaengine:day-{arguments['end']}", "title": "Microsoft day", "companies": ["Microsoft"]}],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-09-25", "ok": True, "window": {"start": "2026-09-22", "end": "2026-09-25"}}
                for q in (first, second)
            ]
        }
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_run)
    by_q = {row["query"]: row for row in state["queries"]}
    assert len(q1_calls) == collect_mod.MAX_AE_REQUESTS_PER_QUERY == 40
    # 1 次整段早退 + 39 个单日（2026-09-26 .. 2026-11-03）
    assert by_q[first]["complete"] is False
    assert by_q[first]["ok"] is True
    assert by_q[first]["covered_until"] == "2026-11-03"
    assert "本轮请求次数已用完，2026-11-04 起留待下一轮" in by_q[first]["error"]
    assert "翻页上限" not in by_q[first]["error"]
    assert "未覆盖日期：2026-11-04 至 2026-11-10" in by_q[first]["error"]


def test_ae_single_day_over_page_cap_advances_boundary_with_footnote_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """连续多个超限日在一轮内依次推进并写出「<日期> 当天超过翻页上限，只取了前 N 条」，且可正常 commit。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    page_counters: dict[str, int] = {}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        key = f"{arguments['start']}..{arguments['end']}"
        page_counters[key] = page_counters.get(key, 0) + 1
        p = page_counters[key]
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-09":
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:full-{p}", "title": "Microsoft full", "companies": ["Microsoft"]}],
                    "has_more": True,
                    "cursor": f"ae1:full-{p}",
                    "total_upstream": 300,
                },
            }
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:d-{arguments['start']}-{arguments['end']}-p{p}-{i}",
                        "title": "Microsoft note",
                        "companies": ["Microsoft"],
                        "publish_time": arguments["end"],
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:d-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})

    # 场景 1：多日积压（11-06..11-09），首子窗口 [11-03, 11-06] 用满后重扫 [11-06, 11-06]，随后连续推进 11-07/08/09
    last_multi = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae1, state1 = collect_mod.collect_alphaengine(cfg, "2026-11-08", "2026-11-09", set(), last_multi)
    row1 = {row["query"]: row for row in state1["queries"]}[first]
    assert row1["ok"] is True and row1["complete"] is False
    assert row1["covered_until"] == "2026-11-09"
    assert row1["partial_days"] == ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"]
    assert "2026-11-06..2026-11-06" in page_counters
    assert "2026-11-06 当天超过翻页上限，只取了前 100 条" in row1["error"]
    assert "2026-11-09 当天超过翻页上限，只取了前 100 条" in row1["error"]
    assert len([k for k in ae1["new"] if k.startswith("alphaengine:d-2026-11-06-2026-11-06-")]) == 100

    # 场景 2：仅 1 个待扫日 2026-11-10：[11-07, 11-10] 用满后重扫 [11-10, 11-10] -> 越过 2026-11-10 且 commit 放行
    page_counters.clear()
    last_single = {
        "ticker": "MSFT",
        "run_id": "20261109-2200",
        "until": "2026-11-09",
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        },
    }
    _ae2, state2 = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_single)
    row2 = {row["query"]: row for row in state2["queries"]}[first]
    assert row2["ok"] is True and row2["complete"] is False
    assert row2["covered_until"] == "2026-11-10"
    assert "2026-11-10..2026-11-10" in page_counters
    assert "2026-11-10 当天超过翻页上限，只取了前 100 条" in row2["error"]

    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    (ws / "research" / "tracker" / "last_run.json").write_text(json.dumps(last_single), encoding="utf-8")
    _write_packet(ws, "20261110-2250", "2026-11-10", ae=state2, collected_at="2026-11-10T22:50:00-05:00")
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-2250", "--material", "no", "--summary", "single-day-cap"],
    )
    assert code == 0 and payload["ok"] is True
    saved = {row["query"]: row for row in _read_json(ws / "research" / "tracker" / "last_run.json")["msft_ae_state"]["queries"]}
    assert saved[first]["covered_until"] == "2026-11-10"


@pytest.mark.parametrize("stale_kind", ["stale_flag", "cursor_unavailable", "day2_bridge_cache_stale"])
def test_ae_stale_and_cursor_unavailable_keep_items_without_advancing_boundary(
    monkeypatch: pytest.MonkeyPatch,
    stale_kind: str,
) -> None:
    """响应带 stale=true 或 cursor_unavailable 时，已取条目照常入包，该窗口不推进 covered_until，脚注写缓存提示。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if stale_kind == "stale_flag":
            # 复现 codex 高 2：仅 1 页、has_more=False 但 stale=True
            return {
                "ok": True,
                "data": {
                    "stale": True,
                    "results": [{"doc_id": "alphaengine:stale-1", "title": "Microsoft cached", "companies": ["Microsoft"]}],
                    "has_more": False,
                },
            }
        if stale_kind == "cursor_unavailable":
            return {
                "ok": True,
                "data": {
                    "cursor_unavailable": "upstream_down",
                    "results": [{"doc_id": "alphaengine:cu-1", "title": "Microsoft cu", "companies": ["Microsoft"]}],
                    "has_more": False,
                },
            }
        # day2_bridge_cache_stale：整段早退 -> 11-06 正常完成 -> 11-07 返回 _bridge_cache.stale=True
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:shared", "title": "Microsoft fresh", "companies": ["Microsoft"], "snippet": "fresh snippet"}],
                    "has_more": True,
                    "cursor": "ae1:w",
                    "total_upstream": 200,
                },
            }
        if arguments["end"] == "2026-11-06":
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:d06", "title": "Microsoft 06", "companies": ["Microsoft"]}],
                    "has_more": False,
                },
            }
        if arguments["end"] == "2026-11-07":
            return {
                "ok": True,
                "data": {
                    "_bridge_cache": {"stale": True, "status": "stale"},
                    "results": [
                        {"doc_id": "alphaengine:shared", "title": "Microsoft stale dup", "companies": ["Microsoft"], "snippet": "stale snippet"},
                        {"doc_id": "alphaengine:d07-stale", "title": "Microsoft 07 stale", "companies": ["Microsoft"]},
                    ],
                    "has_more": False,
                },
            }
        raise AssertionError(arguments)

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    end_day = "2026-11-08" if stale_kind == "day2_bridge_cache_stale" else "2026-11-10"
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-07", end_day, set(), last_run)
    row1 = {row["query"]: row for row in state["queries"]}[first]
    assert row1["complete"] is False
    assert "AlphaEngine 暂时不可用，返回的是缓存结果" in row1["error"]

    if stale_kind == "stale_flag":
        assert "alphaengine:stale-1" in ae["new"]
        assert row1["covered_until"] == "2026-11-05"
        assert "未覆盖日期：2026-11-06 至 2026-11-10" in row1["error"]
    elif stale_kind == "cursor_unavailable":
        assert "alphaengine:cu-1" in ae["new"]
        assert row1["covered_until"] == "2026-11-05"
        assert "未覆盖日期：2026-11-06 至 2026-11-10" in row1["error"]
    else:
        assert {"alphaengine:shared", "alphaengine:d06", "alphaengine:d07-stale"} <= set(ae["new"])
        # 正常取得的版本优先于过期缓存版本
        assert ae["new"]["alphaengine:shared"]["snippet"] == "fresh snippet"
        assert row1["covered_until"] == "2026-11-06"
        assert "覆盖边界已推进至 2026-11-06" in row1["error"]
        assert "未覆盖日期：2026-11-07 至 2026-11-08" in row1["error"]

    failures = collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"})
    assert len(failures) == 1
    assert "AlphaEngine 暂时不可用，返回的是缓存结果" in failures[0]["error"]


def test_ae_cursor_expired_restarts_within_query_budget_and_never_blames_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只要本条查询本轮还有剩余请求次数，第一次游标失效就重扫；游标失效导致停止时脚注绝不写「已达翻页上限」。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]

    # 场景 1（复现 codex 中 3）：多日整段第 4 次请求首次 E_CURSOR_EXPIRED -> 整段 4 页已满但本查询还有剩余请求 -> 第 5 次带 fresh=True 重扫并成功
    calls_1: list[dict[str, Any]] = []

    def bridge_restart_on_4th(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        calls_1.append(arguments)
        step = len(calls_1)
        if step in (1, 2, 3):
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:p{step}", "title": "Microsoft", "companies": ["Microsoft"]}],
                    "has_more": True,
                    "cursor": f"ae1:c{step}",
                },
            }
        if step == 4:
            assert arguments["params"] == {"sort": "relevance", "cursor": "ae1:c3"}
            return {
                "ok": False,
                "error": "E_CURSOR_EXPIRED: expired on 4th request",
                "data": {"error_code": "E_CURSOR_EXPIRED"},
            }
        if step == 5:
            assert arguments["params"] == {"sort": "relevance", "fresh": True}
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:p5-fresh", "title": "Microsoft fresh", "companies": ["Microsoft"]}],
                    "has_more": False,
                },
            }
        raise AssertionError(step)

    _patch_bridge(monkeypatch, bridge_restart_on_4th)
    last_multi = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae1, state1 = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-10", set(), last_multi)
    row1 = {row["query"]: row for row in state1["queries"]}[first]
    assert len(calls_1) == 5
    assert row1["ok"] is True and row1["complete"] is True and row1["covered_until"] == "2026-11-10"
    assert "alphaengine:p5-fresh" in ae1["new"]
    assert collect_mod.failed_sources({}, ae1, {}, {}, {"ok": True, "status": "ok"}) == []

    # 场景 2：在查询本轮最后一次请求（第 5/5 次）才首次游标失效 -> 无剩余预算可重扫，脚注不得写「已达翻页上限」
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", 5)
    calls_2: list[dict[str, Any]] = []

    def bridge_expire_on_last_request(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        calls_2.append(arguments)
        step = len(calls_2)
        if step < 5:
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:last-{step}", "title": "Microsoft", "companies": ["Microsoft"]}],
                    "has_more": True,
                    "cursor": f"ae1:last-{step}",
                },
            }
        return {
            "ok": False,
            "error": "E_CURSOR_EXPIRED: expired on last request",
            "data": {"error_code": "E_CURSOR_EXPIRED"},
        }

    _patch_bridge(monkeypatch, bridge_expire_on_last_request)
    last_single = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    _ae2, state2 = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_single)
    row2 = {row["query"]: row for row in state2["queries"]}[first]
    assert len(calls_2) == 5
    assert row2["ok"] is True and row2["complete"] is False and row2["covered_until"] == "2026-11-09"
    assert "游标失效" in row2["error"]
    assert "已达翻页上限" not in row2["error"]
    assert "达到翻页上限" not in row2["error"]


def test_ae_capability_disabled_skips_alphaengine_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """alphaengine 能力关闭时，采集直接跳过 AlphaEngine，不发请求、不写失败、不写脚注，其他来源照常。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    # The sandbox learns the capability only from the config the backend assembles,
    # so the test drives the real path: assemble with alphaengine off, then collect.
    called_routes: list[str] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = str(arguments.get("route") or "")
        called_routes.append(route)
        assert route != "alphaengine", "alphaengine capability is disabled; must not call alphaengine"
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)

    # 3) 沙箱上传装配路径：assemble_json_text 在 Everflow 全开时原样返回，在 alphaengine 关闭时注入 sources.alphaengine=false
    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    everflow_caps = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
    no_ae_caps = everflow_caps - {CapabilityId.ALPHAENGINE}

    fake_skill = tmp_path / "assembled-skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")

    for ticker_name in ("MSFT", "ANT"):
        src_cfg = skill_dir / "config" / f"{ticker_name}.json"
        raw = src_cfg.read_text(encoding="utf-8")
        assert assemble_json_text(raw, path=src_cfg, capabilities=everflow_caps) == raw
        assembled = assemble_json_text(raw, path=src_cfg, capabilities=no_ae_caps)
        parsed_assembled = json.loads(assembled)
        assert parsed_assembled["sources"]["alphaengine"] is False
        assert parsed_assembled["_dalton_cap_alphaengine"] is False
        (fake_skill / "config" / f"{ticker_name}.json").write_text(assembled, encoding="utf-8")
        (fake_skill / "references" / f"{ticker_name.lower()}-rules.md").write_text(f"# {ticker_name}\n", encoding="utf-8")

    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)
    called_routes.clear()
    ws_msft = _workspace(tmp_path / "assembled-msft", ticker="MSFT")
    code_m, payload_m = _run(capsys, ["collect", "--workspace-dir", str(ws_msft), "--run-id", "20261110-asm"])
    assert code_m == 0 and payload_m["failed_sources"] == []
    assert "alphaengine" not in called_routes
    assert "tmtb" in called_routes

    called_routes.clear()
    ws_ant = _workspace(tmp_path / "assembled-ant", ticker="ANT")
    _patch_consensus(monkeypatch, lambda ticker, benchmark="QQQ": {**_consensus_ok("MSFT", benchmark), "ticker": ticker})
    code_a, payload_a = _run(capsys, ["collect", "--workspace-dir", str(ws_ant), "--run-id", "20261110-asm-ant"], ticker="ANT")
    assert code_a == 0 and payload_a["failed_sources"] == []
    assert "alphaengine" not in called_routes
    assert "tmtb" in called_routes


@pytest.mark.parametrize("partial_page", [1, 2])
def test_ae_partial_status_with_cursor_enters_day_split(
    monkeypatch: pytest.MonkeyPatch,
    partial_page: int,
) -> None:
    """首页或中间页返回 status=partial 且带有效 cursor 时，进入按日续采，不能把覆盖推进到整段末尾。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    full_pages = {"n": 0}
    day_pages: dict[str, int] = {"2026-11-06": 0, "2026-11-07": 0, "2026-11-08": 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            full_pages["n"] += 1
            p = full_pages["n"]
            if p == partial_page:
                return {
                    "ok": True,
                    "status": "partial",
                    "data": {
                        "ok": True,
                        "status": "partial",
                        "results": [
                            {
                                "doc_id": f"alphaengine:full-p{p}",
                                "title": "Microsoft partial",
                                "companies": ["Microsoft"],
                                "publish_time": "2026-11-07",
                            }
                        ],
                        "has_more": True,
                        "cursor": f"ae1:full-partial-{p}",
                    },
                }
            if p < partial_page:
                return {
                    "ok": True,
                    "data": {
                        "results": [
                            {
                                "doc_id": f"alphaengine:full-ok-{p}",
                                "title": "Microsoft ok",
                                "companies": ["Microsoft"],
                                "publish_time": "2026-11-07",
                            }
                        ],
                        "has_more": True,
                        "cursor": f"ae1:full-ok-{p}",
                    },
                }
            # 若误继续翻页就会在末页正常结束并把覆盖推进到 11-08
            return {"ok": True, "data": {"results": [], "has_more": False}}
        sub_end = arguments["end"]
        day_pages[sub_end] += 1
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:day-{sub_end}",
                        "title": f"Microsoft {sub_end}",
                        "companies": ["Microsoft"],
                        "publish_time": sub_end,
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_run)
    by_q = {row["query"]: row for row in state["queries"]}
    assert full_pages["n"] == partial_page
    assert day_pages == {"2026-11-06": 1, "2026-11-07": 1, "2026-11-08": 1}
    assert by_q[first]["ok"] is True and by_q[first]["complete"] is True
    assert by_q[first]["covered_until"] == "2026-11-08"
    assert {"alphaengine:day-2026-11-06", "alphaengine:day-2026-11-07", "alphaengine:day-2026-11-08"} <= set(ae["new"])

    # 单日窗口整段首页返回 status=partial 且带 cursor 时，先用剩余预算按日单独查 [待采日, 待采日]；
    # 若子窗口也返回 status=partial 且带 cursor，则不再继续翻页且不推进覆盖
    single_windows: list[tuple[str, str]] = []

    def single_bridge_both_partial(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        single_windows.append((arguments["start"], arguments["end"]))
        return {
            "ok": True,
            "status": "partial",
            "data": {
                "ok": True,
                "status": "partial",
                "results": [{"doc_id": f"alphaengine:single-p{len(single_windows)}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": "2026-11-10"}],
                "has_more": True,
                "cursor": f"ae1:single-c{len(single_windows)}",
            },
        }

    _patch_bridge(monkeypatch, single_bridge_both_partial)
    last_single = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    _ae_s, state_s = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_single)
    row_s = {row["query"]: row for row in state_s["queries"]}[first]
    assert single_windows == [("2026-11-07", "2026-11-10"), ("2026-11-10", "2026-11-10")]
    assert row_s["ok"] is True and row_s["complete"] is False
    assert row_s["covered_until"] == "2026-11-09"
    assert "未覆盖日期：2026-11-10 至 2026-11-10" in row_s["error"]


@pytest.mark.parametrize("fail_page", [1, 2])
def test_ae_upstream_cursor_error_enters_day_split(
    monkeypatch: pytest.MonkeyPatch,
    fail_page: int,
) -> None:
    """MCP 返回 E_UPSTREAM_CURSOR 时，只要还有请求预算，按整段扫不完进入按日续采，不当成普通失败直接结束。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    full_pages = {"n": 0}
    day_pages: dict[str, int] = {"2026-11-06": 0, "2026-11-07": 0, "2026-11-08": 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            full_pages["n"] += 1
            p = full_pages["n"]
            if p == fail_page:
                return {
                    "ok": False,
                    "error": "E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标；不能把部分结果当作全量",
                    "data": {"error": "E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标；不能把部分结果当作全量"},
                }
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:u-p{p}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": "2026-11-07"}],
                    "has_more": True,
                    "cursor": f"ae1:u-{p}",
                },
            }
        sub_end = arguments["end"]
        day_pages[sub_end] += 1
        return {
            "ok": True,
            "data": {
                "results": [{"doc_id": f"alphaengine:u-day-{sub_end}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": sub_end}],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_run)
    by_q = {row["query"]: row for row in state["queries"]}
    assert full_pages["n"] == fail_page
    assert day_pages == {"2026-11-06": 1, "2026-11-07": 1, "2026-11-08": 1}
    assert by_q[first]["ok"] is True and by_q[first]["complete"] is True
    assert by_q[first]["covered_until"] == "2026-11-08"
    assert collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"}) == []

    # 单日子窗口也遇到 E_UPSTREAM_CURSOR 时，该日不推进覆盖，前面已完成的日期保留
    def bridge_day2_fail(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return {"ok": False, "error": "E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标", "data": {}}
        if arguments["end"] == "2026-11-06":
            return {"ok": True, "data": {"results": [{"doc_id": "alphaengine:d06", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": "2026-11-06"}], "has_more": False}}
        if arguments["end"] == "2026-11-07":
            return {"ok": False, "error": "E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标", "data": {}}
        raise AssertionError(arguments)

    _patch_bridge(monkeypatch, bridge_day2_fail)
    ae2, state2 = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_run)
    row2 = {row["query"]: row for row in state2["queries"]}[first]
    assert row2["complete"] is False and row2["covered_until"] == "2026-11-06"
    assert "E_UPSTREAM_CURSOR" in row2["error"]
    assert "未覆盖日期：2026-11-07 至 2026-11-08" in row2["error"]
    assert collect_mod.failed_sources({}, ae2, {}, {}, {"ok": True, "status": "ok"})


def test_ae_single_pending_day_start_and_lookback_crowded_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个待采日窗口从 [待采日 - AE_LOOKBACK_DAYS, until] 开始；回看日占满页数时不越过该日。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]

    # 1) 已覆盖到 11-09、until 11-10 时，请求从 11-07 开始（不论 since 是 11-09 还是 11-08）
    seen_windows: list[tuple[str, str]] = []

    def bridge_capture(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == first:
            seen_windows.append((arguments["start"], arguments["end"]))
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_capture)
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    for since_day in ("2026-11-09", "2026-11-08", "2026-11-01"):
        seen_windows.clear()
        _ae, state = collect_mod.collect_alphaengine(cfg, since_day, "2026-11-10", set(), last_1109)
        assert seen_windows == [("2026-11-07", "2026-11-10")]
        row = {r["query"]: r for r in state["queries"]}[first]
        assert row["window"] == {"start": "2026-11-07", "end": "2026-11-10"}
        assert row["complete"] is True and row["covered_until"] == "2026-11-10"

    # 2) [P-3, P] 前 5 页全部是回看日文档（2026-11-03）-> 改为对 [2026-11-06, 2026-11-06] 单日重扫，不再卡死
    page_no = {"lookback": 0, "single": 0}

    def bridge_all_lookback(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-03" and arguments["end"] == "2026-11-06":
            page_no["lookback"] += 1
            p = page_no["lookback"]
            return {
                "ok": True,
                "data": {
                    "results": [
                        {
                            "doc_id": f"alphaengine:old-p{p}-{i}",
                            "title": "Microsoft old",
                            "companies": ["Microsoft"],
                            "publish_time": "2026-11-03 10:00:00",
                        }
                        for i in range(20)
                    ],
                    "has_more": True,
                    "cursor": f"ae1:old-{p}",
                },
            }
        assert arguments["start"] == "2026-11-06" and arguments["end"] == "2026-11-06"
        page_no["single"] += 1
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": "alphaengine:sat-1106",
                        "title": "Microsoft saturday",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-06 09:00:00",
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_all_lookback)
    last_1105 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    _ae_lb, state_lb = collect_mod.collect_alphaengine(cfg, "2026-11-05", "2026-11-06", set(), last_1105)
    row_lb = {r["query"]: r for r in state_lb["queries"]}[first]
    assert page_no == {"lookback": collect_mod.MAX_AE_PAGES, "single": 1}
    assert row_lb["ok"] is True and row_lb["complete"] is True
    assert row_lb["covered_until"] == "2026-11-06"
    assert "alphaengine:sat-1106" in _ae_lb["new"]

    # 3) 单日重扫 [2026-11-06, 2026-11-06] 也用完 5 页预算时，覆盖越过 2026-11-06 并写「2026-11-06 当天超过翻页上限」
    page_no = {"lookback": 0, "single": 0}

    def bridge_with_target_day(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        bucket = "single" if arguments["start"] == arguments["end"] else "lookback"
        page_no[bucket] += 1
        p = page_no[bucket]
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:mix-{bucket}-p{p}-{i}",
                        "title": "Microsoft mix",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-06" if bucket == "single" else "2026-11-03",
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:mix-{bucket}-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge_with_target_day)
    _ae_mix, state_mix = collect_mod.collect_alphaengine(cfg, "2026-11-05", "2026-11-06", set(), last_1105)
    row_mix = {r["query"]: r for r in state_mix["queries"]}[first]
    assert page_no == {"lookback": collect_mod.MAX_AE_PAGES, "single": collect_mod.MAX_AE_PAGES}
    assert row_mix["ok"] is True and row_mix["complete"] is False
    assert row_mix["covered_until"] == "2026-11-06"
    assert "2026-11-06 当天超过翻页上限，只取了前 100 条" in row_mix["error"]

    # 4) 多日按日切分的首子窗口（11-03..11-06）全是回看日文档 -> 单日重扫 11-06 后继续推进 11-07/11-08
    sub_windows_seen: list[tuple[str, str]] = []

    def bridge_multi_all_lookback(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "ae1:m", "total_upstream": 300}}
        win = (arguments["start"], arguments["end"])
        sub_windows_seen.append(win)
        if win == ("2026-11-03", "2026-11-06"):
            p = sub_windows_seen.count(win)
            return {
                "ok": True,
                "data": {
                    "results": [
                        {
                            "doc_id": f"alphaengine:mlb-p{p}-{i}",
                            "title": "Microsoft old",
                            "companies": ["Microsoft"],
                            "publish_time": "2026-11-04",
                        }
                        for i in range(20)
                    ],
                    "has_more": True,
                    "cursor": f"ae1:mlb-{p}",
                },
            }
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:day-{arguments['end']}",
                        "title": f"Microsoft {arguments['end']}",
                        "companies": ["Microsoft"],
                        "publish_time": arguments["end"],
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_multi_all_lookback)
    _ae_mlb, state_mlb = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_1105)
    row_mlb = {r["query"]: r for r in state_mlb["queries"]}[first]
    assert ("2026-11-06", "2026-11-06") in sub_windows_seen
    assert row_mlb["ok"] is True and row_mlb["complete"] is True
    assert row_mlb["covered_until"] == "2026-11-08"
    assert {"alphaengine:day-2026-11-06", "alphaengine:day-2026-11-07", "alphaengine:day-2026-11-08"} <= set(_ae_mlb["new"])


def test_researchdata_bridge_disabled_skips_bridge_sources_and_keeps_market_and_news(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """researchdata_bridge 能力关闭时，跳过依赖桥的来源，行情和网页新闻照常进入数据包且不写失败。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    def _forbid_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        raise AssertionError(f"bridge should not be called when researchdata_bridge is disabled: {arguments}")

    _patch_bridge(monkeypatch, _forbid_bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": None, "error": "researchdata_transport_unconfigured"})
    _patch_consensus(monkeypatch, _consensus_ok)

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    everflow_caps = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
    no_bridge_caps = frozenset({CapabilityId.FUND_AUTOMATIONS})

    fake_skill = tmp_path / "no-bridge-skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")

    for ticker_name in ("MSFT", "ANT"):
        src_cfg = skill_dir / "config" / f"{ticker_name}.json"
        raw = src_cfg.read_text(encoding="utf-8")
        assert assemble_json_text(raw, path=src_cfg, capabilities=everflow_caps) == raw
        assembled = assemble_json_text(raw, path=src_cfg, capabilities=no_bridge_caps)
        parsed = json.loads(assembled)
        assert parsed["_dalton_cap_researchdata_bridge"] is False
        assert parsed["_dalton_cap_alphaengine"] is False
        assert parsed["sources"]["internal"] is False
        assert parsed["sources"]["alphaengine"] is False
        assert parsed["sources"]["sec"] is False
        assert parsed["sources"]["x"] is False
        (fake_skill / "config" / f"{ticker_name}.json").write_text(assembled, encoding="utf-8")
        (fake_skill / "references" / f"{ticker_name.lower()}-rules.md").write_text(f"# {ticker_name}\n", encoding="utf-8")

    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    ws_msft = _workspace(tmp_path / "ws-msft", ticker="MSFT")
    code_m, payload_m = _run(capsys, ["collect", "--workspace-dir", str(ws_msft), "--run-id", "20261110-nobridge"])
    assert code_m == 0 and payload_m["ok"] is True and payload_m["failed_sources"] == []
    packet_m = _read_json(ws_msft / "research" / "tracker" / "20261110-nobridge" / "packet.json")
    assert packet_m["consensus"]["price"]["close"] == 400
    assert packet_m["news"]["status"] == "pending_agent_search"
    assert packet_m["news"]["queries"]
    assert packet_m["internal"]["status"] == {}
    assert packet_m["alphaengine"] == {"status": [], "new": {}}
    assert packet_m["sec"] == {"status": [], "new": {}}
    assert packet_m["x"]["status"] == []

    ws_ant = _workspace(tmp_path / "ws-ant", ticker="ANT")
    _patch_consensus(monkeypatch, lambda ticker, benchmark="QQQ": {**_consensus_ok("MSFT", benchmark), "ticker": ticker})
    code_a, payload_a = _run(capsys, ["collect", "--workspace-dir", str(ws_ant), "--run-id", "20261110-nobridge-ant"], ticker="ANT")
    assert code_a == 0 and payload_a["ok"] is True and payload_a["failed_sources"] == []
    packet_a = _read_json(ws_ant / "research" / "tracker" / "20261110-nobridge-ant" / "packet.json")
    assert packet_a["consensus"]["price"]["close"] == 400
    assert packet_a["news"]["status"] == "pending_agent_search"
    assert packet_a["news"]["queries"]


@pytest.mark.parametrize("disabled_mode", ["alphaengine_off", "bridge_off"])
def test_ae_disabled_commit_preserves_covered_until_and_resumes_when_reenabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    disabled_mode: str,
) -> None:
    """AlphaEngine 被跳过的轮次提交时保留 last_run.json 原有的 AE 查询状态；重新开启后从原 covered_until 继续。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    src_cfg = skill_dir / "config" / "MSFT.json"
    raw_cfg = src_cfg.read_text(encoding="utf-8")
    parsed_raw = json.loads(raw_cfg)
    first, second = parsed_raw["ae_queries"]

    caps_off = (
        frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION, CapabilityId.ALPHAENGINE}
        if disabled_mode == "alphaengine_off"
        else frozenset({CapabilityId.FUND_AUTOMATIONS})
    )
    assembled_off = assemble_json_text(raw_cfg, path=src_cfg, capabilities=caps_off)

    fake_skill = tmp_path / "skill-toggle"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    (fake_skill / "config" / "MSFT.json").write_text(assembled_off, encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    ws = _workspace(tmp_path / "ws-toggle")
    tracker = ws / "research" / "tracker"
    initial_queries = [
        {
            "query": q,
            "window": {"start": "2026-11-02", "end": "2026-11-05"},
            "has_more": False,
            "complete": True,
            "covered_until": "2026-11-05",
            "ok": True,
            "returned": 3,
            "error": None,
        }
        for q in (first, second)
    ]
    seed_last = {
        "ticker": "MSFT",
        "run_id": "20261105-2200",
        "committed_at": "2026-11-05T22:00:00-05:00",
        "collected_at": "2026-11-05T22:00:00-05:00",
        "since": "2026-11-04",
        "until": "2026-11-05",
        "snapshot_file": "data/consensus/2026-11-05_2200.json",
        "material": "no",
        "summary": "seed",
        "msft_ae_state": {"queries": initial_queries},
    }
    (tracker / "last_run.json").write_text(json.dumps(seed_last), encoding="utf-8")

    ae_calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments.get("route") == "alphaengine":
            ae_calls.append(arguments)
        if arguments.get("route") == "sec":
            return {"ok": True, "data": {"rows": []}}
        if arguments.get("route") == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    clock = {"now": datetime(2026, 11, 8, 22, 50, tzinfo=ET)}
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, clock)
    _patch_transport(
        monkeypatch,
        {"mode": None, "error": "researchdata_transport_unconfigured"} if disabled_mode == "bridge_off" else {"mode": "bridge"},
    )
    _patch_consensus(monkeypatch, _consensus_ok)

    code_c, payload_c = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261108-2250"])
    assert code_c == 0 and payload_c["failed_sources"] == []
    assert ae_calls == []
    code_m, payload_m = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261108-2250", "--material", "no", "--summary", "ae-off"],
    )
    assert code_m == 0 and payload_m["ok"] is True

    last_after_off = _read_json(tracker / "last_run.json")
    assert last_after_off["until"] == "2026-11-08"
    assert last_after_off["msft_ae_state"]["queries"] == initial_queries

    # 9 日重新开启能力后，从原 covered_until（11-05）继续，首个请求起点为 11-02，而非 11-05
    (fake_skill / "config" / "MSFT.json").write_text(raw_cfg, encoding="utf-8")
    _patch_transport(monkeypatch, {"mode": "bridge"})
    clock["now"] = datetime(2026, 11, 9, 22, 50, tzinfo=ET)
    code_on, payload_on = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261109-2250"])
    assert code_on == 0 and payload_on["failed_sources"] == []
    assert len(ae_calls) == 2
    assert {c["start"] for c in ae_calls} == {"2026-11-02"}
    assert {c["end"] for c in ae_calls} == {"2026-11-09"}


def test_ae_round_cap_footnote_distinguished_from_single_day_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复现 codex 低 6：整段 4 次 + 前三天各 5 次 + 第 4 天仅剩 1 次仍 has_more，脚注写本轮请求次数已用完而非翻页上限。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", 20)
    day_pages: dict[str, int] = {}
    q1_total = {"n": 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        q1_total["n"] += 1
        key = f"{arguments['start']}..{arguments['end']}"
        day_pages[key] = day_pages.get(key, 0) + 1
        p = day_pages[key]
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-10":
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:f-{p}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": "2026-11-06"}],
                    "has_more": True,
                    "cursor": f"ae1:f-{p}",
                },
            }
        sub_end = arguments["end"]
        if sub_end in ("2026-11-06", "2026-11-07", "2026-11-08"):
            # 前三天各恰好在第 5 页扫完
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": f"alphaengine:{sub_end}-p{p}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": sub_end}],
                    "has_more": p < 5,
                    "cursor": f"ae1:{sub_end}-c{p}" if p < 5 else None,
                },
            }
        if sub_end == "2026-11-09":
            # 第 4 天只剩 1 次请求，且仍有下一页
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:2026-11-09-p1", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": "2026-11-09"}],
                    "has_more": True,
                    "cursor": "ae1:2026-11-09-c1",
                },
            }
        raise AssertionError(arguments)

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_run)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert q1_total["n"] == collect_mod.MAX_AE_REQUESTS_PER_QUERY == 20
    assert row["ok"] is True and row["complete"] is False
    assert row["covered_until"] == "2026-11-08"
    assert "本轮请求次数已用完，2026-11-09 起留待下一轮" in row["error"]
    assert "翻页上限" not in row["error"]
    assert "未覆盖日期：2026-11-09 至 2026-11-10" in row["error"]


def test_ae_page_cap_without_target_day_docs_never_advances_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P-3, P] 前 5 页无待采日文档（无日期或全回看日）时改为单日 [P, P] 重扫，推进边界不再卡死。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }

    # 1) [11-07, 11-10] 前 5 页缺少日期 -> 重扫单日 [11-10, 11-10]；单日也满 5 页时按单日超限推进到 11-10
    windows_seen: list[tuple[str, str]] = []

    def bridge_all_undated(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        windows_seen.append(win)
        p = windows_seen.count(win)
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:nodate-{win[0]}-{win[1]}-p{p}-{i}",
                        "title": "Microsoft undated",
                        "companies": ["Microsoft"],
                        "publish_time": "" if i % 2 == 0 else "not-a-date",
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:nodate-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge_all_undated)
    _ae_nd, state_nd = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row_nd = {r["query"]: r for r in state_nd["queries"]}[first]
    assert windows_seen.count(("2026-11-07", "2026-11-10")) == collect_mod.MAX_AE_PAGES
    assert windows_seen.count(("2026-11-10", "2026-11-10")) == collect_mod.MAX_AE_PAGES
    assert row_nd["ok"] is True and row_nd["complete"] is False
    assert row_nd["covered_until"] == "2026-11-10"
    assert "2026-11-10 当天超过翻页上限，只取了前 100 条" in row_nd["error"]

    # 2) 回看日 + 无日期混合占满 [11-07, 11-10] -> 重扫 [11-10, 11-10] 完成并推进覆盖至 11-10
    windows_seen.clear()

    def bridge_mixed_no_target(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        windows_seen.append(win)
        if win == ("2026-11-10", "2026-11-10"):
            return {"ok": True, "data": {"results": [], "has_more": False}}
        p = windows_seen.count(win)
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:mixnd-p{p}-{i}",
                        "title": "Microsoft old or undated",
                        "companies": ["Microsoft"],
                        **({"publish_time": "2026-11-07"} if i % 2 == 0 else {}),
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:mixnd-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge_mixed_no_target)
    _ae_mx, state_mx = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row_mx = {r["query"]: r for r in state_mx["queries"]}[first]
    assert ("2026-11-10", "2026-11-10") in windows_seen
    assert row_mx["ok"] is True and row_mx["complete"] is True
    assert row_mx["covered_until"] == "2026-11-10"

    # 3) 多日按日子窗口首日 [11-03, 11-06] 满 5 页无日期 -> 单日重扫 11-06 并继续推进 11-07/11-08
    last_1105 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    windows_seen.clear()

    def bridge_multi_undated(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "ae1:m", "total_upstream": 300}}
        win = (arguments["start"], arguments["end"])
        windows_seen.append(win)
        p = windows_seen.count(win)
        return {
            "ok": True,
            "data": {
                "results": [
                    {"doc_id": f"alphaengine:mnd-{win[0]}-{win[1]}-p{p}-{i}", "title": "Microsoft undated", "companies": ["Microsoft"]}
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:mnd-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge_multi_undated)
    _ae_mnd, state_mnd = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_1105)
    row_mnd = {r["query"]: r for r in state_mnd["queries"]}[first]
    assert windows_seen.count(("2026-11-03", "2026-11-06")) == collect_mod.MAX_AE_PAGES
    assert windows_seen.count(("2026-11-06", "2026-11-06")) == collect_mod.MAX_AE_PAGES
    assert row_mnd["ok"] is True and row_mnd["complete"] is False
    assert row_mnd["covered_until"] == "2026-11-08"
    assert row_mnd["partial_days"] == ["2026-11-06", "2026-11-07", "2026-11-08"]
    assert "2026-11-06 当天超过翻页上限，只取了前 100 条" in row_mnd["error"]


@pytest.mark.parametrize(
    ("fault_kind", "fault_page"),
    [
        ("partial", 1),
        ("partial", 2),
        ("upstream_cursor", 1),
        ("upstream_cursor", 2),
    ],
)
def test_ae_single_pending_day_partial_and_upstream_cursor_retry_target_day(
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
    fault_page: int,
) -> None:
    """单待采日窗口 [待采日-3, until] 返回 partial 或 E_UPSTREAM_CURSOR 时，用剩余预算单独查 [待采日, 待采日]。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }

    calls_seen: list[tuple[str, str]] = []
    full_pages = {"n": 0}

    def bridge_recover(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        calls_seen.append((arguments["start"], arguments["end"]))
        if arguments["start"] == "2026-11-07" and arguments["end"] == "2026-11-10":
            full_pages["n"] += 1
            p = full_pages["n"]
            if p == fault_page:
                if fault_kind == "partial":
                    return {
                        "ok": True,
                        "status": "partial",
                        "data": {
                            "ok": True,
                            "status": "partial",
                            "results": [
                                {"doc_id": "alphaengine:full-partial-item", "title": "Microsoft partial", "companies": ["Microsoft"], "publish_time": "2026-11-08"}
                            ],
                            "has_more": True,
                            "cursor": "ae1:full-partial",
                        },
                    }
                return {
                    "ok": False,
                    "error": "E_UPSTREAM_CURSOR: 上游未推进游标",
                    "data": {"error": "E_UPSTREAM_CURSOR: 上游未推进游标"},
                }
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": f"alphaengine:full-ok-{p}", "title": "Microsoft ok", "companies": ["Microsoft"], "publish_time": "2026-11-08"}
                    ],
                    "has_more": True,
                    "cursor": f"ae1:full-ok-{p}",
                },
            }
        assert arguments["start"] == "2026-11-10" and arguments["end"] == "2026-11-10"
        return {
            "ok": True,
            "data": {
                "results": [
                    {"doc_id": "alphaengine:day-1110", "title": "Microsoft day", "companies": ["Microsoft"], "publish_time": "2026-11-10"}
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_recover)
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert full_pages["n"] == fault_page
    assert calls_seen[-1] == ("2026-11-10", "2026-11-10")
    assert row["ok"] is True and row["complete"] is True and row["covered_until"] == "2026-11-10"
    assert "alphaengine:day-1110" in ae["new"]
    if fault_kind == "partial":
        assert "alphaengine:full-partial-item" in ae["new"]
    assert collect_mod.failed_sources({}, ae, {}, {}, {"ok": True, "status": "ok"}) == []


def test_ae_partial_status_with_non_empty_error_keeps_items_and_enters_day_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一页同时带 status=partial 和非空 error 时，按部分响应保留该页条目并进入按日续采，不走普通失败分支。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]

    # 用真实的 _normalize_dict_result 构造响应（与 HTTP/wrapper 桥接层输出完全同构，此时 ok=False）
    normalized_partial_err = collect_mod._g._normalize_dict_result(
        {
            "status": "partial",
            "error": "upstream partial timeout",
            "results": [
                {
                    "doc_id": "alphaengine:partial-with-err",
                    "title": "Microsoft partial with error",
                    "companies": ["Microsoft"],
                    "publish_time": "2026-11-07",
                }
            ],
            "has_more": True,
            "cursor": "ae1:pe-1",
        }
    )
    assert normalized_partial_err["ok"] is False
    assert normalized_partial_err["status"] == "partial"
    assert normalized_partial_err["error"] == "upstream partial timeout"

    # 1) 多日窗口首页返回 partial + non-empty error -> 保留该页条目，进入按日续采并扫完
    day_calls: list[tuple[str, str]] = []

    def bridge_multi(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return normalized_partial_err
        day_calls.append((arguments["start"], arguments["end"]))
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:pe-day-{arguments['end']}",
                        "title": f"Microsoft {arguments['end']}",
                        "companies": ["Microsoft"],
                        "publish_time": arguments["end"],
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_multi)
    last_1105 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae_m, state_m = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_1105)
    row_m = {r["query"]: r for r in state_m["queries"]}[first]
    assert day_calls == [
        ("2026-11-03", "2026-11-06"),
        ("2026-11-07", "2026-11-07"),
        ("2026-11-08", "2026-11-08"),
    ]
    assert row_m["ok"] is True and row_m["complete"] is True and row_m["covered_until"] == "2026-11-08"
    assert "alphaengine:partial-with-err" in ae_m["new"]
    assert {"alphaengine:pe-day-2026-11-06", "alphaengine:pe-day-2026-11-07", "alphaengine:pe-day-2026-11-08"} <= set(ae_m["new"])
    assert collect_mod.failed_sources({}, ae_m, {}, {}, {"ok": True, "status": "ok"}) == []

    # 2) 单日窗口首页返回 partial + non-empty error -> 保留该页条目，单独查 [待采日, 待采日]
    single_calls: list[tuple[str, str]] = []

    def bridge_single(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        single_calls.append((arguments["start"], arguments["end"]))
        if arguments["start"] == "2026-11-07" and arguments["end"] == "2026-11-10":
            return normalized_partial_err
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": "alphaengine:pe-single-1110",
                        "title": "Microsoft 11-10",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-10",
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_single)
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    ae_s, state_s = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row_s = {r["query"]: r for r in state_s["queries"]}[first]
    assert single_calls == [("2026-11-07", "2026-11-10"), ("2026-11-10", "2026-11-10")]
    assert row_s["ok"] is True and row_s["complete"] is True and row_s["covered_until"] == "2026-11-10"
    assert {"alphaengine:partial-with-err", "alphaengine:pe-single-1110"} <= set(ae_s["new"])


def test_content_assembly_filters_disabled_bridge_routes_preserving_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """研究桥和 AlphaEngine 开启、tmtb 关闭时，装配过滤掉 tmtb 且其余路线保持顺序；采集不请求 tmtb。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    all_on_caps = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
    no_tmtb_caps = all_on_caps - {CapabilityId.TMTB}

    msft_path = skill_dir / "config" / "MSFT.json"
    msft_raw = msft_path.read_text(encoding="utf-8")
    assert assemble_json_text(msft_raw, path=msft_path, capabilities=all_on_caps) == msft_raw
    msft_assembled = assemble_json_text(msft_raw, path=msft_path, capabilities=no_tmtb_caps)
    msft_doc = json.loads(msft_assembled)
    assert msft_doc["sources"]["internal_routes"] == ["sales-notes", "funda-ai", "company-wiki"]
    assert "_dalton_cap_researchdata_bridge" not in msft_doc
    assert "_dalton_cap_alphaengine" not in msft_doc

    ant_path = skill_dir / "config" / "ANT.json"
    ant_raw = ant_path.read_text(encoding="utf-8")
    assert assemble_json_text(ant_raw, path=ant_path, capabilities=all_on_caps) == ant_raw
    ant_assembled = assemble_json_text(ant_raw, path=ant_path, capabilities=no_tmtb_caps)
    ant_doc = json.loads(ant_assembled)
    assert ant_doc["sources"]["internal_routes"] == ["sales-notes", "funda-ai"]

    # 采集脚本直接读装配后的配置，不再请求 tmtb，其余内部路线保持顺序
    fake_skill = tmp_path / "no-tmtb-skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    (fake_skill / "config" / "MSFT.json").write_text(msft_assembled, encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    internal_routes_called: list[str] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = str(arguments.get("route") or "")
        assert route != "tmtb", "tmtb capability is disabled; collector must not request tmtb"
        if route in {"sales-notes", "funda-ai", "company-wiki"}:
            internal_routes_called.append(route)
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)

    ws = _workspace(tmp_path / "ws-no-tmtb")
    code, payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261110-notmtb"])
    assert code == 0 and payload["failed_sources"] == []
    assert internal_routes_called == ["sales-notes", "funda-ai", "company-wiki"]


def test_bridge_disabled_two_days_preserves_internal_sec_x_and_ae_resume_positions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """研究桥关闭两天再打开：until 照常前进，内部库从原重试日起查，SEC 和 X 从关闭前的位置起查。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    src_cfg = skill_dir / "config" / "MSFT.json"
    raw_cfg = src_cfg.read_text(encoding="utf-8")
    parsed_raw = json.loads(raw_cfg)
    first, second = parsed_raw["ae_queries"]

    assembled_off = assemble_json_text(
        raw_cfg,
        path=src_cfg,
        capabilities=frozenset({CapabilityId.FUND_AUTOMATIONS}),
    )

    fake_skill = tmp_path / "skill-bridge-gap"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    (fake_skill / "config" / "MSFT.json").write_text(assembled_off, encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    ws = _workspace(tmp_path / "ws-bridge-gap")
    tracker = ws / "research" / "tracker"
    (tracker / "x_pool.json").write_text(
        json.dumps({"keeper": {"active": True, "tickers": ["MSFT"]}}),
        encoding="utf-8",
    )
    seed_queries = [
        {
            "query": q,
            "window": {"start": "2026-11-07", "end": "2026-11-10"},
            "has_more": False,
            "complete": True,
            "covered_until": "2026-11-10",
            "ok": True,
            "returned": 2,
            "error": None,
        }
        for q in (first, second)
    ]
    seed_last = {
        "ticker": "MSFT",
        "run_id": "20261110-2200",
        "committed_at": "2026-11-10T22:00:00-05:00",
        "collected_at": "2026-11-10T22:00:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/2026-11-10_2200.json",
        "material": "no",
        "summary": "seed",
        "internal_retry_since": "2026-11-08",
        "msft_ae_state": {"queries": seed_queries},
    }
    (tracker / "last_run.json").write_text(json.dumps(seed_last), encoding="utf-8")

    def _forbid_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        raise AssertionError(f"bridge must not be called while disabled: {arguments}")

    clock = {"now": datetime(2026, 11, 11, 22, 50, tzinfo=ET)}
    _patch_bridge(monkeypatch, _forbid_bridge)
    _patch_clock(monkeypatch, clock)
    _patch_transport(monkeypatch, {"mode": None, "error": "researchdata_transport_unconfigured"})
    _patch_consensus(monkeypatch, _consensus_ok)

    # 第 1 天关桥（11-11）和第 2 天关桥（11-12）均正常采集并提交
    for day_num in (11, 12):
        clock["now"] = datetime(2026, 11, day_num, 22, 50, tzinfo=ET)
        run_id = f"202611{day_num:02d}-2250"
        code_c, payload_c = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", run_id])
        assert code_c == 0 and payload_c["failed_sources"] == []
        code_m, payload_m = _run(
            capsys,
            ["commit", "--workspace-dir", str(ws), "--run-id", run_id, "--material", "no", "--summary", f"off-{day_num}"],
        )
        assert code_m == 0 and payload_m["ok"] is True
        saved = _read_json(tracker / "last_run.json")
        assert saved["until"] == f"2026-11-{day_num:02d}"
        assert saved["internal_retry_since"] == "2026-11-08"
        assert saved["sec_last_collected_until"] == "2026-11-10"
        assert saved["x_last_collected_until"] == "2026-11-10"
        assert saved["msft_ae_state"]["queries"] == seed_queries

    # 第 3 天重新打开（11-13）：内部库从 11-08 起查，SEC 从 11-10 起查（floor=11-08），X 从 11-10 起查
    (fake_skill / "config" / "MSFT.json").write_text(raw_cfg, encoding="utf-8")
    _patch_transport(monkeypatch, {"mode": "bridge"})
    clock["now"] = datetime(2026, 11, 13, 22, 50, tzinfo=ET)
    reopen_calls: list[dict[str, Any]] = []

    def bridge_reopen(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        reopen_calls.append(arguments)
        route = arguments.get("route")
        if route == "sec":
            if arguments.get("form") == "8-K":
                return {
                    "ok": True,
                    "data": {
                        "rows": [
                            {"accession_no": "0001193125-26-999001", "form": "8-K", "filing_date": "2026-11-08"},
                        ]
                    },
                }
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            if arguments.get("operation") == "tweets":
                return {
                    "ok": True,
                    "data": {
                        "data": {
                            "items": [
                                {
                                    "id": "gap-post-1",
                                    "text": "MSFT Azure capex update",
                                    "created_at": "2026-11-10T23:30:00-05:00",
                                    "author_handle": "keeper",
                                    "metrics": {"likes": 5, "reposts": 1, "replies": 0, "views": 10},
                                }
                            ],
                            "has_more": False,
                        }
                    },
                }
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_reopen)
    code_on, payload_on = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261113-2250"])
    assert code_on == 0 and payload_on["failed_sources"] == []

    packet_on = _read_json(tracker / "20261113-2250" / "packet.json")
    assert packet_on["since"] == "2026-11-11"
    assert packet_on["internal_since"] == "2026-11-08"

    keyword_starts = {c["start"] for c in reopen_calls if c.get("route") in {"tmtb", "sales-notes", "funda-ai"}}
    wiki_starts = {c["params"]["indexed_since"] for c in reopen_calls if c.get("route") == "company-wiki"}
    assert keyword_starts == {"2026-11-08"}
    assert wiki_starts == {"2026-11-08"}

    sec_floors = {row["floor"] for row in packet_on["sec"]["status"]}
    assert sec_floors == {"2026-11-08"}
    assert "sec:0001193125-26-999001" in packet_on["sec"]["new"]

    x_sinces = {c["since"] for c in reopen_calls if c.get("route") == "x-timeline"}
    assert x_sinces == {"2026-11-10"}
    assert "x:gap-post-1" in packet_on["x"]["new"]

    ae_starts = {c["start"] for c in reopen_calls if c.get("route") == "alphaengine"}
    assert ae_starts == {"2026-11-07"}

    # 重新开启后提交成功：仅跳过轮次才写的上次实际采集日字段不再保留
    code_cm, payload_cm = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261113-2250", "--material", "no", "--summary", "reopen"],
    )
    assert code_cm == 0 and payload_cm["ok"] is True
    last_final = _read_json(tracker / "last_run.json")
    assert last_final["until"] == "2026-11-13"
    assert last_final["internal_retry_since"] is None
    assert "sec_last_collected_until" not in last_final
    assert "x_last_collected_until" not in last_final
    assert "internal_last_collected_until" not in last_final


def test_ae_target_day_ignores_rank_date_and_generic_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ae_item_day 只读 publish_date/publish_time；[P-3, P] 满 5 页时自动改为单日 [P, P] 重扫并推进。"""
    assert collect_mod._ae_item_day({"rank_date": "2026-11-10", "date": "2026-11-10"}) is None
    assert collect_mod._ae_item_day({"publish_date": "2026-11-10"}) == "2026-11-10"
    assert collect_mod._ae_item_day({"publish_time": "2026-11-10T09:30:00Z"}) == "2026-11-10"

    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    windows_seen: list[tuple[str, str]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        windows_seen.append(win)
        p = windows_seen.count(win)
        rows = [
            {
                "doc_id": f"alphaengine:rankonly-{win[0]}-{win[1]}-p{p}-{i}",
                "title": "Microsoft rank date only",
                "companies": ["Microsoft"],
                **({"rank_date": "2026-11-10"} if p == 1 and i == 0 else {}),
                **({"date": "2026-11-10"} if p == 1 and i == 1 else {}),
            }
            for i in range(20)
        ]
        return {
            "ok": True,
            "data": {"results": rows, "has_more": True, "cursor": f"ae1:rankonly-{p}"},
        }

    _patch_bridge(monkeypatch, bridge)
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert windows_seen.count(("2026-11-07", "2026-11-10")) == collect_mod.MAX_AE_PAGES
    assert windows_seen.count(("2026-11-10", "2026-11-10")) == collect_mod.MAX_AE_PAGES
    assert row["ok"] is True and row["complete"] is False
    assert row["covered_until"] == "2026-11-10"
    assert "2026-11-10 当天超过翻页上限，只取了前 100 条" in row["error"]


def test_ae_outer_partial_not_overridden_by_inner_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex 中 2a：外层 status=partial 带 error、内层 structuredContent 为 status=error 且有条目时，按部分响应处理。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    outer_partial_inner_error = {
        "ok": False,
        "status": "partial",
        "error": "outer partial timeout",
        "data": {
            "structuredContent": {
                "status": "error",
                "error": "inner timeout",
                "results": [
                    {
                        "doc_id": "alphaengine:outer-partial-item",
                        "title": "Microsoft outer partial",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-08",
                    }
                ],
                "has_more": True,
                "cursor": "ae1:opie-1",
            }
        },
    }
    assert collect_mod._is_partial_status(outer_partial_inner_error, outer_partial_inner_error["data"]) is True
    assert collect_mod._known_truncation(outer_partial_inner_error, outer_partial_inner_error["data"]) is not None

    # 单待采日整段查询：保留条目并单查 [2026-11-10, 2026-11-10]
    single_calls: list[tuple[str, str]] = []

    def bridge_single(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        single_calls.append((arguments["start"], arguments["end"]))
        if arguments["start"] == "2026-11-07" and arguments["end"] == "2026-11-10":
            return outer_partial_inner_error
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": "alphaengine:opie-day-1110",
                        "title": "Microsoft 11-10",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-10",
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge_single)
    last_1109 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}}
                for q in (first, second)
            ]
        }
    }
    ae_s, state_s = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last_1109)
    row_s = {r["query"]: r for r in state_s["queries"]}[first]
    assert single_calls == [("2026-11-07", "2026-11-10"), ("2026-11-10", "2026-11-10")]
    assert row_s["ok"] is True and row_s["complete"] is True and row_s["covered_until"] == "2026-11-10"
    assert {"alphaengine:outer-partial-item", "alphaengine:opie-day-1110"} <= set(ae_s["new"])


@pytest.mark.parametrize("with_partial_status", [True, False])
def test_ae_no_match_with_items_keeps_items_and_enters_day_split(
    monkeypatch: pytest.MonkeyPatch,
    with_partial_status: bool,
) -> None:
    """codex 中 2b：带「没有匹配」标记但已返回条目的响应，不清空条目，保留条目再进入按日续采。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    resp: dict[str, Any] = {
        "ok": False,
        "error": "[没有匹配] 上游部分分段返回空",
        "data": {
            "error_category": "没有匹配",
            "results": [
                {
                    "doc_id": "alphaengine:nomatch-kept-item",
                    "title": "Microsoft kept despite no_match",
                    "companies": ["Microsoft"],
                    "publish_time": "2026-11-06",
                }
            ],
            "has_more": False,
        },
    }
    if with_partial_status:
        resp["status"] = "partial"

    day_calls: list[tuple[str, str]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-07":
            return resp
        day_calls.append((arguments["start"], arguments["end"]))
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:nm-day-{arguments['end']}",
                        "title": f"Microsoft {arguments['end']}",
                        "companies": ["Microsoft"],
                        "publish_time": arguments["end"],
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_1105 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-06", "2026-11-07", set(), last_1105)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert day_calls == [("2026-11-03", "2026-11-06"), ("2026-11-07", "2026-11-07")]
    assert row["ok"] is True and row["complete"] is True and row["covered_until"] == "2026-11-07"
    assert {"alphaengine:nomatch-kept-item", "alphaengine:nm-day-2026-11-06", "alphaengine:nm-day-2026-11-07"} <= set(ae["new"])


def test_bridge_reopen_sec_x_timeout_preserves_resume_and_fresh_bridge_off_no_fake_until(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex 中 3：桥恢复首轮 SEC/X 超时不清除续采日期，下一轮仍从原日期起采；新工作区首轮关桥不伪造实际采集日。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    src_cfg = skill_dir / "config" / "MSFT.json"
    raw_cfg = src_cfg.read_text(encoding="utf-8")
    assembled_off = assemble_json_text(
        raw_cfg,
        path=src_cfg,
        capabilities=frozenset({CapabilityId.FUND_AUTOMATIONS}),
    )

    fake_skill = tmp_path / "skill-r5-resume"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    # 1) 已有采集记录 -> 11-11..11-16 关桥 -> 11-17 恢复但 SEC 与 X 超时 -> 11-18 仍从 11-10 起采
    (fake_skill / "config" / "MSFT.json").write_text(assembled_off, encoding="utf-8")
    ws = _workspace(tmp_path / "ws-reopen-timeout")
    tracker = ws / "research" / "tracker"
    (tracker / "x_pool.json").write_text(
        json.dumps({"keeper": {"active": True, "tickers": ["MSFT"]}}),
        encoding="utf-8",
    )
    seed_last = {
        "ticker": "MSFT",
        "run_id": "20261110-2200",
        "committed_at": "2026-11-10T22:00:00-05:00",
        "collected_at": "2026-11-10T22:00:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/2026-11-10_2200.json",
        "material": "no",
        "summary": "seed",
        "msft_ae_state": {"queries": []},
    }
    (tracker / "last_run.json").write_text(json.dumps(seed_last), encoding="utf-8")

    clock = {"now": datetime(2026, 11, 16, 22, 50, tzinfo=ET)}
    _patch_clock(monkeypatch, clock)
    _patch_consensus(monkeypatch, _consensus_ok)
    _patch_transport(monkeypatch, {"mode": None, "error": "researchdata_transport_unconfigured"})
    _patch_bridge(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(AssertionError("bridge off")))

    code_c, _ = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261116-2250"])
    assert code_c == 0
    code_m, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261116-2250", "--material", "no", "--summary", "off"],
    )
    assert code_m == 0
    saved_16 = _read_json(tracker / "last_run.json")
    assert saved_16["sec_last_collected_until"] == "2026-11-10"
    assert saved_16["x_last_collected_until"] == "2026-11-10"
    assert saved_16["internal_last_collected_until"] == "2026-11-10"

    # 11-17 恢复桥接，但 SEC 与 X 均超时
    (fake_skill / "config" / "MSFT.json").write_text(raw_cfg, encoding="utf-8")
    _patch_transport(monkeypatch, {"mode": "bridge"})
    clock["now"] = datetime(2026, 11, 17, 22, 50, tzinfo=ET)

    def bridge_timeout(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route in ("sec", "x-timeline"):
            return {"ok": False, "error": "upstream timeout", "data": {"error": "upstream timeout"}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_timeout)
    code_c17, payload_17 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261117-2250"])
    assert code_c17 == 0
    assert {f["source"] for f in payload_17["failed_sources"]} >= {"sec", "x-timeline"}
    code_m17, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261117-2250", "--material", "no", "--summary", "timeout"],
    )
    assert code_m17 == 0
    saved_17 = _read_json(tracker / "last_run.json")
    assert saved_17["until"] == "2026-11-17"
    assert saved_17["sec_last_collected_until"] == "2026-11-10"
    assert saved_17["x_last_collected_until"] == "2026-11-10"
    assert "internal_last_collected_until" not in saved_17

    # 11-18 重跑恢复成功：SEC 与 X 仍从 11-10 起采，提交后清除续采日期
    clock["now"] = datetime(2026, 11, 18, 22, 50, tzinfo=ET)
    calls_18: list[dict[str, Any]] = []

    def bridge_ok_18(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        calls_18.append(arguments)
        route = arguments.get("route")
        if route == "sec":
            return {
                "ok": True,
                "data": {
                    "rows": [
                        {"accession_no": "0001193125-26-888012", "form": "8-K", "filing_date": "2026-11-12"},
                    ]
                    if arguments.get("form") == "8-K"
                    else []
                },
            }
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_ok_18)
    code_c18, payload_18 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261118-2250"])
    assert code_c18 == 0 and payload_18["failed_sources"] == []
    packet_18 = _read_json(tracker / "20261118-2250" / "packet.json")
    assert {row["floor"] for row in packet_18["sec"]["status"]} == {"2026-11-08"}
    assert "sec:0001193125-26-888012" in packet_18["sec"]["new"]
    assert {c["since"] for c in calls_18 if c.get("route") == "x-timeline"} == {"2026-11-10"}

    code_m18, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261118-2250", "--material", "no", "--summary", "recovered"],
    )
    assert code_m18 == 0
    saved_18 = _read_json(tracker / "last_run.json")
    assert "sec_last_collected_until" not in saved_18
    assert "x_last_collected_until" not in saved_18

    # 2) 新工作区首轮即桥关闭，连续两轮关桥提交：不把未采集的 until 写成实际采集日，
    #    而是记下首轮的起点，重新开启后从那一天补采（codex r5 中 2 / grok r5 高）
    (fake_skill / "config" / "MSFT.json").write_text(assembled_off, encoding="utf-8")
    _patch_transport(monkeypatch, {"mode": None, "error": "researchdata_transport_unconfigured"})
    _patch_bridge(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(AssertionError("bridge off")))
    fresh_ws = _workspace(tmp_path / "ws-fresh-bridge-off")
    fresh_tracker = fresh_ws / "research" / "tracker"
    first_since: str | None = None
    for day_num in (10, 11):
        clock["now"] = datetime(2026, 11, day_num, 22, 50, tzinfo=ET)
        run_id = f"202611{day_num:02d}-2250"
        c_code, c_payload = _run(capsys, ["collect", "--workspace-dir", str(fresh_ws), "--run-id", run_id])
        if first_since is None:
            first_since = _read_json(fresh_tracker / run_id / "packet.json")["since"]
        assert c_code == 0 and c_payload["failed_sources"] == []
        m_code, _ = _run(
            capsys,
            ["commit", "--workspace-dir", str(fresh_ws), "--run-id", run_id, "--material", "no", "--summary", f"fresh-{day_num}"],
        )
        assert m_code == 0
        saved_fresh = _read_json(fresh_tracker / "last_run.json")
        assert saved_fresh["until"] == f"2026-11-{day_num:02d}"
        assert first_since is not None and first_since < "2026-11-10"
        assert saved_fresh["internal_last_collected_until"] == first_since
        assert saved_fresh["sec_last_collected_until"] == first_since
        assert saved_fresh["x_last_collected_until"] == first_since


def test_single_disabled_internal_route_preserves_retry_since_until_reopened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """codex 中 4：只关 tmtb 一轮后重新打开，保留原 internal_retry_since 并从原重试日起查。"""
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import assemble_json_text

    skill_dir = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
    src_cfg = skill_dir / "config" / "MSFT.json"
    raw_cfg = src_cfg.read_text(encoding="utf-8")
    all_on_caps = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
    no_tmtb_caps = all_on_caps - {CapabilityId.TMTB}
    assembled_no_tmtb = assemble_json_text(raw_cfg, path=src_cfg, capabilities=no_tmtb_caps)
    parsed_no_tmtb = json.loads(assembled_no_tmtb)
    assert parsed_no_tmtb["sources"]["internal_routes"] == ["sales-notes", "funda-ai", "company-wiki"]
    assert parsed_no_tmtb["_dalton_cap_disabled_internal_routes"] == ["tmtb"]

    fake_skill = tmp_path / "skill-tmtb-toggle"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "common-rules.md").write_text("# Common\n", encoding="utf-8")
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    (fake_skill / "config" / "MSFT.json").write_text(assembled_no_tmtb, encoding="utf-8")
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)

    ws = _workspace(tmp_path / "ws-tmtb-toggle")
    tracker = ws / "research" / "tracker"
    seed_last = {
        "ticker": "MSFT",
        "run_id": "20261110-2200",
        "committed_at": "2026-11-10T22:00:00-05:00",
        "collected_at": "2026-11-10T22:00:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/2026-11-10_2200.json",
        "material": "no",
        "summary": "seed",
        "internal_retry_since": "2026-11-08",
        "msft_ae_state": {"queries": []},
    }
    (tracker / "last_run.json").write_text(json.dumps(seed_last), encoding="utf-8")

    def bridge_no_tmtb(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        assert route != "tmtb"
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    clock = {"now": datetime(2026, 11, 12, 22, 50, tzinfo=ET)}
    _patch_bridge(monkeypatch, bridge_no_tmtb)
    _patch_clock(monkeypatch, clock)
    _patch_transport(monkeypatch, {"mode": "bridge"})
    _patch_consensus(monkeypatch, _consensus_ok)

    code_c1, payload_c1 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261112-2250"])
    assert code_c1 == 0 and payload_c1["failed_sources"] == []
    packet_12 = _read_json(tracker / "20261112-2250" / "packet.json")
    assert packet_12["internal_retry_since"] == "2026-11-08"

    code_m1, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261112-2250", "--material", "no", "--summary", "tmtb-off"],
    )
    assert code_m1 == 0
    saved_12 = _read_json(tracker / "last_run.json")
    assert saved_12["until"] == "2026-11-12"
    assert saved_12["internal_retry_since"] == "2026-11-08"

    # 重新打开 tmtb：从原重试日 2026-11-08 起查，全部成功后清除 internal_retry_since
    (fake_skill / "config" / "MSFT.json").write_text(raw_cfg, encoding="utf-8")
    clock["now"] = datetime(2026, 11, 13, 22, 50, tzinfo=ET)
    tmtb_starts: list[str] = []

    def bridge_all_on(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "tmtb":
            tmtb_starts.append(str(arguments.get("start")))
        if route == "sec":
            return {"ok": True, "data": {"rows": []}}
        if route == "x-timeline":
            return {"ok": True, "data": {"data": {"items": [], "has_more": False}}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_all_on)
    code_c2, payload_c2 = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261113-2250"])
    assert code_c2 == 0 and payload_c2["failed_sources"] == []
    assert tmtb_starts == ["2026-11-08"]

    code_m2, _ = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261113-2250", "--material", "no", "--summary", "tmtb-on"],
    )
    assert code_m2 == 0
    saved_13 = _read_json(tracker / "last_run.json")
    assert saved_13["internal_retry_since"] is None


def test_ae_later_subwindow_over_cap_advances_and_continues_to_until(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按日续采先完成 11-06，随后单日 11-07 超过翻页上限时推进 11-07 并继续完成 11-08。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    last_1105 = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-05", "ok": True, "window": {"start": "2026-11-02", "end": "2026-11-05"}}
                for q in (first, second)
            ]
        }
    }
    sub_1107_pages = {"n": 0}
    sub_1108_called = {"n": 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        if arguments["start"] == "2026-11-02" and arguments["end"] == "2026-11-08":
            return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "ae1:early", "total_upstream": 300}}
        if arguments["end"] == "2026-11-06":
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": "alphaengine:d1106", "title": "Microsoft 1106", "companies": ["Microsoft"], "publish_time": "2026-11-06"}
                    ],
                    "has_more": False,
                },
            }
        if arguments["start"] == "2026-11-08" and arguments["end"] == "2026-11-08":
            sub_1108_called["n"] += 1
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": "alphaengine:d1108", "title": "Microsoft 1108", "companies": ["Microsoft"], "publish_time": "2026-11-08"}
                    ],
                    "has_more": False,
                },
            }
        assert arguments["start"] == "2026-11-07" and arguments["end"] == "2026-11-07"
        sub_1107_pages["n"] += 1
        p = sub_1107_pages["n"]
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:d1107-p{p}-{i}",
                        "title": "Microsoft later subwindow",
                        "companies": ["Microsoft"],
                        "publish_time": "2026-11-07",
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:d1107-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge)
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-07", "2026-11-08", set(), last_1105)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert sub_1107_pages["n"] == collect_mod.MAX_AE_PAGES
    assert sub_1108_called["n"] == 1
    assert row["ok"] is True and row["complete"] is False
    assert row["covered_until"] == "2026-11-08"
    assert row["partial_days"] == ["2026-11-07"]
    assert "2026-11-07 当天超过翻页上限，只取了前 100 条" in row["error"]
    assert "覆盖边界已推进至 2026-11-08" in row["error"]
    assert "未覆盖日期：" not in row["error"]


def test_ae_consecutive_over_cap_days_advance_in_single_round_and_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """连续多个超限日在一轮内依次推进（不再一轮一天），并能通过 _validate_ae_state 提交。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    page_counts: dict[tuple[str, str], int] = {}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        page_counts[win] = page_counts.get(win, 0) + 1
        p = page_counts[win]
        if win == ("2026-11-08", "2026-11-15"):
            return {
                "ok": True,
                "data": {"results": [], "has_more": True, "cursor": "ae1:w", "total_upstream": 400},
            }
        # 11-12、11-13、11-14 均超过翻页上限，11-15 正常扫完
        if win == ("2026-11-15", "2026-11-15"):
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": "alphaengine:d15-1", "title": "Microsoft 15", "companies": ["Microsoft"], "publish_time": "2026-11-15"}
                    ],
                    "has_more": False,
                },
            }
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:{win[0]}-{win[1]}-p{p}-{i}",
                        "title": "Microsoft note",
                        "companies": ["Microsoft"],
                        "publish_time": win[1],
                    }
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:{win[0]}-{win[1]}-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": datetime(2026, 11, 15, 22, 50, tzinfo=ET)})
    last_run = {
        "ticker": "MSFT",
        "run_id": "20261111-2200",
        "until": "2026-11-11",
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-11", "ok": True, "window": {"start": "2026-11-08", "end": "2026-11-11"}}
                for q in (first, second)
            ]
        },
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-14", "2026-11-15", set(), last_run)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert page_counts[("2026-11-09", "2026-11-12")] == 5
    assert page_counts[("2026-11-12", "2026-11-12")] == 5
    assert page_counts[("2026-11-13", "2026-11-13")] == 5
    assert page_counts[("2026-11-14", "2026-11-14")] == 5
    assert page_counts[("2026-11-15", "2026-11-15")] == 1
    assert row["ok"] is True and row["complete"] is False
    assert row["covered_until"] == "2026-11-15"
    assert row["partial_days"] == ["2026-11-12", "2026-11-13", "2026-11-14"]
    for day in ("2026-11-12", "2026-11-13", "2026-11-14"):
        assert f"{day} 当天超过翻页上限，只取了前 100 条" in row["error"]
    assert "覆盖边界已推进至 2026-11-15" in row["error"]

    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    (ws / "research" / "tracker" / "last_run.json").write_text(json.dumps(last_run), encoding="utf-8")
    _write_packet(ws, "20261115-2250", "2026-11-15", ae=state, collected_at="2026-11-15T22:50:00-05:00")
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261115-2250", "--material", "no", "--summary", "multi-day"],
    )
    assert code == 0 and payload["ok"] is True
    saved = {r["query"]: r for r in _read_json(ws / "research" / "tracker" / "last_run.json")["msft_ae_state"]["queries"]}
    assert saved[first]["covered_until"] == "2026-11-15"


def test_ae_lookback_crowded_first_subwindow_rescans_single_day_and_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P-3, P] 前 100 条全为回看日文档（无 P 日文档）时，改为单日 [P, P] 重扫并越过 P，不再卡死。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    windows_seen: list[tuple[str, str]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        windows_seen.append(win)
        if win == ("2026-11-10", "2026-11-15"):
            return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "ae1:w", "total_upstream": 300}}
        if win == ("2026-11-11", "2026-11-14"):
            p = windows_seen.count(win)
            return {
                "ok": True,
                "data": {
                    "results": [
                        {
                            "doc_id": f"alphaengine:lb-p{p}-{i}",
                            "title": "Microsoft lookback",
                            "companies": ["Microsoft"],
                            "publish_time": "2026-11-12",
                        }
                        for i in range(20)
                    ],
                    "has_more": True,
                    "cursor": f"ae1:lb-{p}",
                },
            }
        # 周六 2026-11-14 单日只有 1 篇文档，2026-11-15 只有 1 篇文档
        return {
            "ok": True,
            "data": {
                "results": [
                    {
                        "doc_id": f"alphaengine:single-{win[1]}",
                        "title": f"Microsoft {win[1]}",
                        "companies": ["Microsoft"],
                        "publish_time": win[1],
                    }
                ],
                "has_more": False,
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-13", "ok": True, "window": {"start": "2026-11-10", "end": "2026-11-13"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-14", "2026-11-15", set(), last_run)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert windows_seen.count(("2026-11-11", "2026-11-14")) == 5
    assert ("2026-11-14", "2026-11-14") in windows_seen
    assert ("2026-11-15", "2026-11-15") in windows_seen
    assert row["ok"] is True and row["complete"] is True
    assert row["covered_until"] == "2026-11-15"
    assert row["error"] is None
    assert {"alphaengine:single-2026-11-14", "alphaengine:single-2026-11-15"} <= set(ae["new"])


def test_ae_13_day_backlog_jumps_to_until_minus_3_with_note_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """积压 13 天（covered_until=11-11, until=11-24）跳到 until-3（11-21），备注写明跳过段并放行提交。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    seen_windows: list[tuple[str, str]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == first:
            seen_windows.append((arguments["start"], arguments["end"]))
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": datetime(2026, 11, 24, 22, 17, tzinfo=ET)})
    last_run = {
        "ticker": "MSFT",
        "run_id": "20261111-2200",
        "until": "2026-11-11",
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-11", "ok": True, "window": {"start": "2026-11-08", "end": "2026-11-11"}}
                for q in (first, second)
            ]
        },
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-23", "2026-11-24", set(), last_run)
    row = {r["query"]: r for r in state["queries"]}[first]
    expected_note = "AlphaEngine 积压超过 5 天，已跳过 2026-11-12 至 2026-11-20，未逐日采集；需要时可按标题检索补看"
    # 新待采起点 2026-11-21，含 3 天回看窗口 [2026-11-18, 2026-11-24]
    assert seen_windows == [("2026-11-18", "2026-11-24")]
    assert row["ok"] is True and row["complete"] is True
    assert row["covered_until"] == "2026-11-24"
    assert row["skipped_backlog"] == {"since": "2026-11-12", "until": "2026-11-20"}
    assert row["note"] == expected_note

    md = collect_mod.render_packet_md({
        "ticker": "MSFT",
        "run_id": "20261124-2217",
        "since": "2026-11-23",
        "until": "2026-11-24",
        "msft_ae_state": state,
        "alphaengine": _ae,
    })
    assert expected_note in md

    ws = _workspace(tmp_path)
    (ws / "data" / "consensus" / "snap.json").write_text("{}", encoding="utf-8")
    (ws / "research" / "tracker" / "last_run.json").write_text(json.dumps(last_run), encoding="utf-8")
    _write_packet(ws, "20261124-2217", "2026-11-24", ae=state, collected_at="2026-11-24T22:17:00-05:00")
    code, payload = _run(
        capsys,
        ["commit", "--workspace-dir", str(ws), "--run-id", "20261124-2217", "--material", "no", "--summary", "backlog-jump"],
    )
    assert code == 0 and payload["ok"] is True
    saved = {r["query"]: r for r in _read_json(ws / "research" / "tracker" / "last_run.json")["msft_ae_state"]["queries"]}
    assert saved[first]["covered_until"] == "2026-11-24"

    # 失败轮不跳积压、边界留在 2026-11-11
    _patch_bridge(monkeypatch, lambda tool, args, timeout=120: {"ok": False, "error": "upstream timeout", "data": {}})
    _ae_fail, state_fail = collect_mod.collect_alphaengine(cfg, "2026-11-23", "2026-11-24", set(), last_run)
    row_fail = {r["query"]: r for r in state_fail["queries"]}[first]
    assert row_fail["covered_until"] == "2026-11-11"
    assert "skipped_backlog" not in row_fail


def test_ae_round_budget_exhausted_midway_resumes_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮请求预算在中间某天用完时如实记录停在哪一天，下一轮从那天继续并扫完。"""
    assert collect_mod.MAX_AE_REQUESTS_PER_QUERY == 40
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", 16)
    page_counts: dict[tuple[str, str], int] = {}

    def bridge_r1(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        win = (arguments["start"], arguments["end"])
        page_counts[win] = page_counts.get(win, 0) + 1
        p = page_counts[win]
        if win == ("2026-11-08", "2026-11-15"):
            return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "ae1:w", "total_upstream": 500}}
        return {
            "ok": True,
            "data": {
                "results": [
                    {"doc_id": f"alphaengine:{win[0]}-{win[1]}-p{p}-{i}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": win[1]}
                    for i in range(20)
                ],
                "has_more": True,
                "cursor": f"ae1:{win[1]}-{p}",
            },
        }

    _patch_bridge(monkeypatch, bridge_r1)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-11", "ok": True, "window": {"start": "2026-11-08", "end": "2026-11-11"}}
                for q in (first, second)
            ]
        }
    }
    # 16 次预算：整段早退 1 + [11-09,11-12] 5 + [11-12,11-12] 5 + [11-13,11-13] 5 = 16 -> 推进至 11-13，停在 11-14
    _ae1, state1 = collect_mod.collect_alphaengine(cfg, "2026-11-14", "2026-11-15", set(), last_run)
    row1 = {r["query"]: r for r in state1["queries"]}[first]
    assert sum(page_counts.values()) == 16
    assert row1["ok"] is True and row1["complete"] is False
    assert row1["covered_until"] == "2026-11-13"
    assert "本轮请求次数已用完，2026-11-14 起留待下一轮" in row1["error"]
    assert "未覆盖日期：2026-11-14 至 2026-11-15" in row1["error"]

    # 下一轮从 2026-11-14 继续（含 3 天回看 2026-11-11..2026-11-15），正常扫完
    r2_calls: list[dict[str, Any]] = []

    def bridge_r2(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == first:
            r2_calls.append(arguments)
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge_r2)
    _ae2, state2 = collect_mod.collect_alphaengine(cfg, "2026-11-14", "2026-11-15", set(), {"msft_ae_state": state1})
    row2 = {r["query"]: r for r in state2["queries"]}[first]
    assert len(r2_calls) == 1
    assert (r2_calls[0]["start"], r2_calls[0]["end"]) == ("2026-11-10", "2026-11-15")
    assert row2["complete"] is True and row2["covered_until"] == "2026-11-15"


def test_ae_search_in_config_forwarded_to_request_params_and_state_keyed_by_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config 中的 search_in 透传到 params.search_in（含翻页续页），且状态仍以查询词文本为键。"""
    meta_cfg = json.loads(collect_mod.research_config_path("META").read_text(encoding="utf-8"))
    meta_cfg = collect_mod._filter_legacy_config(meta_cfg)
    googl_cfg = json.loads(collect_mod.research_config_path("GOOGL").read_text(encoding="utf-8"))
    googl_cfg = collect_mod._filter_legacy_config(googl_cfg)

    ae_calls: list[dict[str, Any]] = []

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        ae_calls.append(arguments)
        if arguments["query"] == "Meta Platforms" and "cursor" not in (arguments.get("params") or {}):
            return {
                "ok": True,
                "data": {
                    "results": [{"doc_id": "alphaengine:m1", "title": "Meta Platforms Q3", "companies": ["Meta Platforms"], "publish_time": "2026-11-10"}],
                    "has_more": True,
                    "cursor": "ae1:meta-c1",
                },
            }
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    # 模拟线上既有 last_run：状态键仍是纯查询词 "Meta Platforms"
    old_meta_last = {
        "meta_ae_state": {
            "queries": [
                {"query": "Meta Platforms", "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}},
                {"query": "Meta 脸书 Muse", "complete": True, "covered_until": "2026-11-09", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-09"}},
            ]
        }
    }
    monkeypatch.setattr(collect_mod, "TICKER", "META")
    _ae, state = collect_mod.collect_alphaengine(meta_cfg, "2026-11-09", "2026-11-10", set(), old_meta_last)

    meta_p_calls = [c for c in ae_calls if c["query"] == "Meta Platforms"]
    muse_calls = [c for c in ae_calls if c["query"] == "Meta 脸书 Muse"]
    assert len(meta_p_calls) == 2
    assert meta_p_calls[0]["params"] == {"sort": "relevance", "search_in": "title", "fresh": True}
    assert meta_p_calls[1]["params"] == {"sort": "relevance", "search_in": "title", "cursor": "ae1:meta-c1"}
    assert meta_p_calls[0]["start"] == "2026-11-07"
    assert len(muse_calls) == 1
    assert muse_calls[0]["params"] == {"sort": "relevance", "fresh": True}
    assert [r["query"] for r in state["queries"]] == ["Meta Platforms", "Meta 脸书 Muse"]

    ae_calls.clear()
    monkeypatch.setattr(collect_mod, "TICKER", "GOOGL")
    _ae_g, state_g = collect_mod.collect_alphaengine(googl_cfg, "2026-11-09", "2026-11-10", set(), {})
    googl_title = [c for c in ae_calls if c["query"] == "Alphabet Google"]
    googl_full = [c for c in ae_calls if c["query"] == "谷歌 Gemini 云 TPU"]
    assert googl_title[0]["params"] == {"sort": "relevance", "search_in": "title", "fresh": True}
    assert googl_full[0]["params"] == {"sort": "relevance", "fresh": True}
    assert [r["query"] for r in state_g["queries"]] == ["Alphabet Google", "谷歌 Gemini 云 TPU"]


def test_ae_window_overflow_wording_distinguishes_multiday_from_single_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多日回看窗口超限如实写「{起}–{止} 窗口超过 100 条」，单日超限才写「{P} 当天超过翻页上限」。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]

    def bridge_cap(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        return {
            "ok": True,
            "data": {
                "results": [{"doc_id": f"alphaengine:{arguments['start']}-{arguments['end']}", "title": "Microsoft", "companies": ["Microsoft"], "publish_time": arguments["end"]}],
                "has_more": True,
                "cursor": "ae1:next",
            },
        }

    _patch_bridge(monkeypatch, bridge_cap)
    same_day_last = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-10", "ok": True, "window": {"start": "2026-11-06", "end": "2026-11-10"}}
                for q in (first, second)
            ]
        }
    }
    _ae_same, state_same = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), same_day_last)
    row_same = {r["query"]: r for r in state_same["queries"]}[first]
    assert "2026-11-06–2026-11-10 窗口超过 100 条" in row_same["error"]
    assert "当天超过翻页上限" not in row_same["error"]


@pytest.mark.parametrize("budget", [1, 2])
def test_ae_tight_budget_1_or_2_requests_advances_every_round_without_loss(
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
) -> None:
    """bullbear 预算只够 1–2 次请求时，整段与回看让位给逐日扫描，连续多轮每轮都推进且无漏文档。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", budget)

    days = [f"2026-11-{d:02d}" for d in range(12, 17)]
    docs_by_day = {d: [f"{d}-doc"] for d in days}
    expected = {f"alphaengine:{d}-doc" for d in days}
    until = "2026-11-16"
    seen: set[str] = set()
    collected: set[str] = set()
    covered = "2026-11-11"
    last_run: dict[str, Any] = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": covered, "ok": True, "window": {"start": "2026-11-08", "end": covered}}
                for q in (first, second)
            ]
        }
    }
    rounds = 0

    while True:
        rounds += 1
        assert rounds <= len(days)
        q1_calls: list[dict[str, Any]] = []
        cursors: dict[str, tuple[str, str, int]] = {}
        seq = {"n": 0}

        def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
            if arguments["query"] == second:
                return {"ok": True, "data": {"results": [], "has_more": False}}
            q1_calls.append(arguments)
            s = str(arguments["start"])
            e = str(arguments["end"])
            cur = (arguments.get("params") or {}).get("cursor")
            offset = cursors[str(cur)][2] if cur is not None else 0
            matched = [(d, did) for d in sorted(docs_by_day) if s <= d <= e for did in docs_by_day[d]]
            chunk = matched[offset : offset + 1]
            nxt = offset + len(chunk)
            has_more = nxt < len(matched)
            next_cur = None
            if has_more:
                seq["n"] += 1
                next_cur = f"ae1:c{seq['n']}"
                cursors[next_cur] = (s, e, nxt)
            return {
                "ok": True,
                "data": {
                    "results": [
                        {"doc_id": f"alphaengine:{did}", "title": f"Microsoft {did}", "companies": ["Microsoft"], "publish_time": d}
                        for d, did in chunk
                    ],
                    "has_more": has_more,
                    "cursor": next_cur,
                    "total_upstream": len(matched) * collect_mod.AE_LIMIT,
                },
            }

        _patch_bridge(monkeypatch, bridge)
        ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-12", until, seen, last_run)
        row = {r["query"]: r for r in state["queries"]}[first]
        assert row["ok"] is True
        assert 1 <= len(q1_calls) <= budget
        new_cov = row["covered_until"]
        assert new_cov is not None and new_cov > covered
        assert not (set(ae["new"]) & seen)
        seen |= set(ae["new"])
        collected |= set(ae["new"])
        covered = new_cov
        last_run = {"msft_ae_state": state}
        if row["complete"]:
            break

    assert covered == until
    assert collected == expected
    assert rounds == (5 if budget == 1 else 3)


@pytest.mark.parametrize("budget", [3, 5, 6])
@pytest.mark.parametrize("multi_day", [False, True])
def test_ae_low_budget_3_5_6_with_crowded_lookback_never_stalls_or_misattributes_partial_day(
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    multi_day: bool,
) -> None:
    """预算 3/5/6 次，回看日 101 篇排在前面、待采日仅 1 篇：跳过回看直接扫单日 [P, P]，
    既不卡住也不把回看日文档记为当天 partial_days。"""
    cfg = collect_mod.load_research_config()
    first, second = cfg["ae_queries"]
    monkeypatch.setattr(collect_mod, "MAX_AE_REQUESTS_PER_QUERY", budget)

    docs_by_day: dict[str, list[str]] = {
        "2026-11-10": [f"lookback-{i}" for i in range(1, 102)],
        "2026-11-12": ["target-12"],
    }
    until = "2026-11-12"
    if multi_day:
        docs_by_day["2026-11-13"] = ["target-13"]
        until = "2026-11-13"

    q1_windows: list[tuple[str, str]] = []
    cursors: dict[str, int] = {}
    seq = {"n": 0}

    def bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        if arguments["query"] == second:
            return {"ok": True, "data": {"results": [], "has_more": False}}
        s = str(arguments["start"])
        e = str(arguments["end"])
        q1_windows.append((s, e))
        cur = (arguments.get("params") or {}).get("cursor")
        offset = cursors[str(cur)] if cur is not None else 0
        matched = [(d, did) for d in sorted(docs_by_day) if s <= d <= e for did in docs_by_day[d]]
        chunk = matched[offset : offset + 20]
        nxt = offset + len(chunk)
        has_more = nxt < len(matched)
        next_cur = None
        if has_more:
            seq["n"] += 1
            next_cur = f"ae1:c{seq['n']}"
            cursors[next_cur] = nxt
        return {
            "ok": True,
            "data": {
                "results": [
                    {"doc_id": f"alphaengine:{did}", "title": f"Microsoft {did}", "companies": ["Microsoft"], "publish_time": d}
                    for d, did in chunk
                ],
                "has_more": has_more,
                "cursor": next_cur,
                "total_upstream": len(matched),
            },
        }

    _patch_bridge(monkeypatch, bridge)
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": q, "complete": True, "covered_until": "2026-11-11", "ok": True, "window": {"start": "2026-11-08", "end": "2026-11-11"}}
                for q in (first, second)
            ]
        }
    }
    ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-12", until, set(), last_run)
    row = {r["query"]: r for r in state["queries"]}[first]
    assert 1 <= len(q1_windows) <= budget
    assert ("2026-11-12", "2026-11-12") in q1_windows
    assert row["ok"] is True and row["complete"] is True
    assert row["covered_until"] == until
    assert "partial_days" not in row
    assert "alphaengine:target-12" in ae["new"]
    if multi_day:
        assert "alphaengine:target-13" in ae["new"]


def test_ae_validate_state_accepts_valid_and_legacy_rejects_contradictions() -> None:
    """_validate_ae_state 放行合法多日推进/积压跳跃/旧格式数据包，拒绝矛盾 partial_days/skipped_backlog 与伪造 has_more=false。"""
    last_run = {
        "msft_ae_state": {
            "queries": [
                {"query": "Microsoft", "complete": True, "covered_until": "2026-11-11", "ok": True, "window": {"start": "2026-11-08", "end": "2026-11-11"}}
            ]
        }
    }

    # (1) 合法：多日推进 + partial_days + skipped_backlog 均合法
    valid_row = {
        "query": "Microsoft",
        "window": {"start": "2026-11-18", "end": "2026-11-24"},
        "cursor": None,
        "has_more": False,
        "complete": False,
        "covered_until": "2026-11-24",
        "ok": True,
        "partial_days": ["2026-11-21"],
        "skipped_backlog": {"since": "2026-11-12", "until": "2026-11-20"},
        "error": "2026-11-21 当天超过翻页上限，只取了前 100 条，覆盖边界已推进至 2026-11-24",
    }
    collect_mod._validate_ae_state({"queries": [valid_row]}, last_run)

    # (2) 合法：旧格式数据包（无 partial_days 字段）保留兼容
    legacy_row = {
        "query": "Microsoft",
        "window": {"start": "2026-11-08", "end": "2026-11-12"},
        "cursor": None,
        "has_more": True,
        "complete": False,
        "covered_until": "2026-11-12",
        "ok": True,
        "error": "2026-11-12 超过翻页上限，只取了前 100 条，覆盖边界已推进至 2026-11-12",
    }
    collect_mod._validate_ae_state({"queries": [legacy_row]}, last_run)

    # (3) 拒绝：partial_days 早于窗口起点
    early_partial = {**valid_row, "partial_days": ["2026-11-17"]}
    early_partial.pop("skipped_backlog")
    with pytest.raises(collect_mod.CollectError):
        collect_mod._validate_ae_state({"queries": [early_partial]}, last_run)

    # (4) 拒绝：partial_days 晚于 covered_until
    late_partial = {**valid_row, "covered_until": "2026-11-22", "partial_days": ["2026-11-23"]}
    with pytest.raises(collect_mod.CollectError):
        collect_mod._validate_ae_state({"queries": [late_partial]}, last_run)

    # (5) 拒绝：skipped_backlog 晚于或等于 covered_until
    late_skip = {**valid_row, "covered_until": "2026-11-20", "partial_days": []}
    with pytest.raises(collect_mod.CollectError):
        collect_mod._validate_ae_state({"queries": [late_skip]}, last_run)

    # (6) 拒绝：凭 has_more=false 把未完成窗口推进到终点（含停止/未覆盖提示或无单日超限说明）
    forged_has_more = {
        "query": "Microsoft",
        "window": {"start": "2026-11-08", "end": "2026-11-15"},
        "cursor": None,
        "has_more": False,
        "complete": False,
        "covered_until": "2026-11-15",
        "ok": True,
        "partial_days": ["2026-11-12"],
        "error": "2026-11-12 当天超过翻页上限，只取了前 100 条；本轮请求次数已用完，2026-11-13 起留待下一轮，未覆盖日期：2026-11-13 至 2026-11-15",
    }
    with pytest.raises(collect_mod.CollectError) as exc_info:
        collect_mod._validate_ae_state({"queries": [forged_has_more]}, last_run)
    assert exc_info.value.status == "ae_incomplete_advanced"

    forged_no_err = {**forged_has_more, "error": "本轮未扫完"}
    with pytest.raises(collect_mod.CollectError) as exc_info2:
        collect_mod._validate_ae_state({"queries": [forged_no_err]}, last_run)
    assert exc_info2.value.status == "ae_incomplete_advanced"


def test_duplicate_ae_queries_rejected_on_config_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置中同一查询词出现两次（如一条全文、一条标题）在配置加载与解析时均报错。"""
    dup_cfg = {
        "ticker": "META",
        "ae_max_searches": 2,
        "ae_queries": [
            "Meta Platforms",
            {"query": "Meta Platforms", "search_in": "title"},
        ],
    }
    with pytest.raises(collect_mod.CollectError) as exc_info:
        collect_mod._parse_ae_queries(dup_cfg)
    assert exc_info.value.status == "duplicate_ae_query"

    cfg_mod = collect_mod._load_tracker_config()
    monkeypatch.setattr(
        cfg_mod,
        "load_validated_config",
        create_autospec(cfg_mod.load_validated_config, return_value=dup_cfg),
    )
    with pytest.raises(collect_mod.CollectError) as exc_load:
        collect_mod.load_research_config()
    assert exc_load.value.status == "duplicate_ae_query"
