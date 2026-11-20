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
        assert all("cursor" not in args.get("params", {}) for args in calls)
        assert _ae["status"]
        states[kind] = state
    assert [row["cursor"] for row in states["has_more"]["queries"]] == ["cursor-kept", "cursor-kept-2"]
    assert all(row["ok"] is True for row in states["has_more"]["queries"])
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
            assert kept["cursor"] == row["cursor"]
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
    assert replayed[first]["start"] == "2026-11-06" and replayed[first]["end"] == "2026-11-10"
    assert replayed[first]["params"]["cursor"] == "cursor-kept"
    assert replayed[second]["start"] == "2026-11-06" and replayed[second]["end"] == "2026-11-10"
    assert replayed[second]["params"]["cursor"] == "cursor-kept-2"

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
    assert _read_json(kept_path)["msft_ae_state"]["queries"][0]["cursor"] == "cursor-kept"


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
    assert errors == [f"{query}: 未完成，覆盖日期不推进" for query in (first, second)]
    packet_path = tracker / "ae-page" / "packet.json"
    packet_bytes = packet_path.read_bytes()
    packet = json.loads(packet_bytes)
    assert packet["collection_complete"] is True
    assert packet["failed_sources"] == payload["failed_sources"]
    by_query = {row["query"]: row for row in packet["msft_ae_state"]["queries"]}
    for query, token in ((first, "a"), (second, "b")):
        row = by_query[query]
        assert row["ok"] is True and row["returned"] == 20 and row["has_more"] is True
        assert row["complete"] is False and row["cursor"] == f"cursor-{token}"
        assert row["covered_until"] == "2026-11-09"
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
        return {"ok": True, "data": {"results": [], "has_more": True, "cursor": "new-query-cursor"}}
    _patch_bridge(monkeypatch, bridge)
    _patch_clock(monkeypatch, {"now": FROZEN})
    imported = {"ae_covered_until": "2026-10-28", "ae_resume": {"window": {"since": "2026-10-25", "until": "2026-10-28"}, "cursor": "legacy-cursor"}}
    _, state = collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-10", set(), imported)
    assert all(not row["complete"] and row["covered_until"] is None for row in state["queries"])
    ws = _workspace(tmp_path)
    (ws / "data/consensus/snap.json").write_text("{}", encoding="utf-8")
    _write_packet(ws, "20261110-1000", "2026-11-10", since="2026-11-09", ae=state)
    code, _ = _run(capsys, ["commit", "--workspace-dir", str(ws), "--run-id", "20261110-1000", "--material", "no", "--summary", "resume legacy window"])
    assert code == 0
    last = _read_json(ws / "research/tracker/last_run.json")
    calls.clear()
    collect_mod.collect_alphaengine(cfg, "2026-11-09", "2026-11-11", set(), last)
    assert len(calls) == 2
    assert all(row["start"] == "2026-10-25" and row["end"] == "2026-10-28" for row in calls)
    assert all(row["params"].get("cursor") == ("new-query-cursor" if mode == "has_more" else None) for row in calls)


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
    assert actual_json == expected_json
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
    assert actual_json == expected_json
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
    assert actual_json == expected_json
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
    assert actual_json == expected_json
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
