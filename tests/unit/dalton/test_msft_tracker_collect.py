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
COLLECT_PATH = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-msft-tracker" / "scripts" / "collect.py"
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


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any]]:
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
    assert [args["form"] for _tool, args in sec_calls] == list(cfg["sec_forms"])
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
    assert ae_starts and set(ae_starts) == {"2026-11-06"}

    calls.clear()
    clock["now"] = datetime(2026, 11, 11, 15, 0, tzinfo=ET)
    code, _payload = _run(capsys, ["collect", "--workspace-dir", str(ws), "--run-id", "20261111-1500", "--since", "2026-11-01"])
    assert code == 0
    assert [args["start"] for args in calls if args.get("route") == "tmtb"] == ["2026-11-01"]
    assert {args["start"] for args in calls if args.get("route") == "alphaengine"} == {"2026-10-29"}


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
                {"doc_id": "alphaengine:_inst_99", "title": "Kept", "companies": ["Microsoft"]},
            ], "has_more": True, "cursor": "c-new"}}
        return {"ok": True, "data": {"results": [], "has_more": False}}

    _patch_bridge(monkeypatch, bridge)
    last_run = {"msft_ae_state": {"queries": [
        {"query": first, "complete": True, "covered_until": "2026-10-28", "ok": True, "cursor": None, "window": {"start": "2026-10-01", "end": "2026-10-28"}},
        {"query": second, "complete": False, "covered_until": "2026-10-20", "ok": True, "cursor": "cur-2", "window": {"start": "2026-10-17", "end": "2026-10-28"}},
    ]}}
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", {"alphaengine:seen-me"}, last_run)
    by_query = {row["query"]: row for row in state["queries"]}
    q1 = next(args for args in calls if args["query"] == first)
    q2 = next(args for args in calls if args["query"] == second)
    assert q1["start"] == "2026-10-25" and q1["start"] <= "2026-10-28" and q1["end"] == "2026-11-10"
    assert "cursor" not in q1["params"]
    assert q2["start"] == "2026-10-17" and q2["end"] == "2026-10-28" and q2["params"]["cursor"] == "cur-2"
    assert [args["query"] for args in calls].count(first) == 1
    assert by_query[first]["complete"] is False
    assert by_query[first]["covered_until"] == "2026-10-28"
    assert by_query[first]["cursor"] == "c-new"
    assert by_query[second]["complete"] is True
    assert by_query[second]["covered_until"] == "2026-10-28"
    assert "alphaengine:_inst_99" in _ae["new"]

    calls.clear()
    collect_mod.collect_alphaengine(
        cfg, "2026-11-09", "2026-11-12", set(), {"msft_ae_state": state}
    )
    replay = next(args for args in calls if args["query"] == first)
    opened = next(args for args in calls if args["query"] == second)
    assert replay["start"] == "2026-10-25" and replay["end"] == "2026-11-10" and replay["params"]["cursor"] == "c-new"
    assert opened["end"] == "2026-11-12" and opened["start"] <= "2026-10-28"
    assert [args["query"] for args in calls].count(first) == 1
    assert [args["query"] for args in calls].count(second) == 1

    calls.clear()
    mode["kind"] = "partial"
    previous = {"msft_ae_state": {"queries": [
        {"query": query, "complete": True, "covered_until": "2026-10-28", "ok": True, "window": {"start": "2026-10-25", "end": "2026-10-28"}}
        for query in cfg["ae_queries"]
    ]}}
    ae_partial, partial_state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), previous)
    assert "alphaengine:partial" in ae_partial["new"]
    assert {row["covered_until"] for row in partial_state["queries"]} == {"2026-10-28"}
    assert all(row["complete"] is False and WIKI_WARNING in row["error"] for row in partial_state["queries"])
    assert all(args["start"] <= "2026-10-28" for args in calls)


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
    covered_only = {"ae_covered_until": "2026-10-28"}
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), covered_only)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-10-25"}
    assert {args["end"] for args in calls} == {"2026-11-10"}
    assert all("cursor" not in args["params"] for args in calls)

    calls.clear()
    imported = {
        "ticker": "MSFT",
        "run_id": "20261028-1800",
        "committed_at": "2026-10-28T18:00:00-04:00",
        "since": "2026-10-25",
        "until": "2026-10-28",
        "snapshot_file": "data/consensus/old.json",
        "material": "no",
        "summary": "imported",
        "ae_covered_until": "2026-10-28",
        "ae_resume": {
            "window": {"since": "2026-10-25", "until": "2026-10-28"},
            "cursor": "legacy-cursor-token",
            "query": "Microsoft",
        },
    }
    _ae, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), imported)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-10-25"}
    assert {args["end"] for args in calls} == {"2026-10-28"}
    assert all("cursor" not in args["params"] for args in calls)
    assert all("legacy-cursor-token" not in json.dumps(args) for args in calls)
    assert {row["complete"] for row in state["queries"]} == {True}
    assert {row["covered_until"] for row in state["queries"]} == {"2026-10-28"}

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
    assert {row["window"]["end"] for row in last["msft_ae_state"]["queries"]} == {"2026-10-28"}

    calls.clear()
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), last)
    assert [args["query"] for args in calls] == [first, second]
    assert {args["start"] for args in calls} == {"2026-10-25"}
    assert {args["end"] for args in calls} == {"2026-11-10"}
    assert all("cursor" not in args["params"] for args in calls)
    assert _ae["status"]


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
    assert code == 2 and payload["status"] == "company_not_msft"
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
    assert last["msft_ae_state"]["queries"][0]["cursor"] == "cursor-2200"

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
    assert _read_json(last_path)["msft_ae_state"]["queries"][0]["cursor"] == "cursor-2200"
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
    assert code == 0 and payload["ok"] is False
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
