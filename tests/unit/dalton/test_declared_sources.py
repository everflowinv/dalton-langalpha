"""Unit tests for `src/dalton/provenance/declared_sources.py`, `scripts/dalton/source_identity.py`, and `scripts/dalton/researchdata_bridge.py`."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
import source_identity  # noqa: E402
from ptc_agent.agent.middleware.provenance.middleware import ProvenanceMiddleware  # noqa: E402
from src.dalton.provenance.declared_sources import expand_declared_sources  # noqa: E402


class TestExpandDeclaredSources:
    def test_whitelist_enforced(self) -> None:
        body = json.dumps(
            {
                "_declared_sources": [
                    {
                        "document_id": "wiki:1632",
                        "route": "company-wiki",
                        "identity": "expert",
                        "publisher": "专家网络访谈",
                        "title": "2026-10-08-专家访谈.md",
                        "as_of": "2026-10-08",
                    }
                ]
            }
        )
        assert expand_declared_sources("untrusted_mcp", "research", body, "tc-1") == []
        assert expand_declared_sources("price_data", "get_stock_data", body, "tc-1") == []
        assert expand_declared_sources("", "research", body, "tc-1") == []

        res_rd = expand_declared_sources("researchdata", "research", body, "tc-1")
        assert len(res_rd) == 1
        assert res_rd[0].source_type == "research_doc"
        assert res_rd[0].identifier == "wiki:1632"
        assert res_rd[0].detail == "expert"
        assert res_rd[0].provider == "company-wiki:专家网络访谈"
        assert res_rd[0].title == "2026-10-08-专家访谈.md · 2026-10-08"
        assert res_rd[0].tool_call_id == "tc-1"

        res_sec = expand_declared_sources("dalton_sec", "get_company_facts", body, "tc-2")
        assert len(res_sec) == 1

    def test_invalid_identity_and_source_type_discarded(self) -> None:
        body = json.dumps(
            {
                "_sources": [
                    {
                        "document_id": "bad:1",
                        "identity": "invalid_identity_value",
                        "title": "Should be dropped",
                    },
                    {
                        "document_id": "bad:2",
                        "identity": "filing",
                        "source_type": "arbitrary_table_injection",
                        "title": "Should also be dropped",
                    },
                    {
                        "document_id": "good:1",
                        "identity": "filing",
                        "source_type": "sec_filing",
                        "route": "sec",
                        "publisher": "SEC EDGAR",
                        "title": "META 10-Q",
                        "as_of": "2026-07-30",
                    },
                ]
            }
        )
        out = expand_declared_sources("researchdata", "finance", body, "tc-3")
        assert len(out) == 1
        assert out[0].identifier == "good:1"
        assert out[0].source_type == "sec_filing"
        assert out[0].detail == "filing"

    def test_field_truncation_and_max_50_cap(self) -> None:
        long_id = "wiki:" + ("x" * 400)
        long_title = "T" * 500
        long_pub = "P" * 300
        long_snippet = "S" * 1000
        entries = [
            {
                "document_id": f"{long_id}_{i}",
                "route": "company-wiki",
                "identity": "internal_note",
                "publisher": long_pub,
                "title": long_title,
                "as_of": "2026-10-08",
                "snippet": long_snippet,
            }
            for i in range(70)
        ]
        body = json.dumps({"_declared_sources": entries})
        out = expand_declared_sources("researchdata", "research", body, "tc-cap")
        assert len(out) == 50
        first = out[0]
        assert len(first.identifier) <= 200
        assert first.title is not None and len(first.title) <= 300
        assert first.provider is not None and len(first.provider) <= 128
        assert first.result_snippet is not None and len(first.result_snippet) <= 500

    def test_truncated_or_malformed_body_never_raises(self) -> None:
        assert expand_declared_sources("researchdata", "research", None, "tc") == []
        assert expand_declared_sources("researchdata", "research", "", "tc") == []
        assert expand_declared_sources("researchdata", "research", "not json", "tc") == []
        assert expand_declared_sources("researchdata", "research", '{"_declared_sources": [123', "tc") == []

        # Body truncated AFTER _declared_sources array closes (simulating 64 KiB cap)
        full = json.dumps(
            {
                "_declared_sources": [
                    {
                        "document_id": "alphaengine:123",
                        "route": "alphaengine",
                        "identity": "broker_report",
                        "publisher": "Morgan Stanley",
                        "title": "Meta Note",
                        "as_of": "2026-10-14",
                    }
                ],
                "huge_text": "A" * 5000,
            }
        )
        truncated_after_sources = full[:-2000]
        recovered = expand_declared_sources("researchdata", "research", truncated_after_sources, "tc-trunc")
        assert len(recovered) == 1
        assert recovered[0].identifier == "alphaengine:123"

    def test_middleware_extract_execute_code_integration(self) -> None:
        mw = ProvenanceMiddleware()
        body = json.dumps(
            {
                "_declared_sources": [
                    {
                        "document_id": "wiki:1632",
                        "route": "company-wiki",
                        "identity": "expert",
                        "publisher": "专家网络访谈",
                        "title": "2026-10-08-专家访谈.md",
                        "as_of": "2026-10-08",
                    }
                ],
                "ok": True,
            }
        )
        req = SimpleNamespace(tool_call={"id": "call-99"})
        res = SimpleNamespace(
            artifact={
                "mcp_trace": [
                    {
                        "server": "researchdata",
                        "tool": "research",
                        "args": {"route": "company-wiki", "operation": "search"},
                        "result_sha256": "abc123",
                        "result_size": len(body),
                        "result_snippet": body[:200],
                        "result_body": body,
                        "timestamp": "2026-10-18T12:00:00+00:00",
                    }
                ]
            }
        )
        emitted = list(mw._extract_execute_code(req, res))
        assert len(emitted) == 2
        assert emitted[0].source_type == "mcp_tool"
        assert emitted[0].identifier == "researchdata:research"
        assert emitted[1].source_type == "research_doc"
        assert emitted[1].identifier == "wiki:1632"
        assert emitted[1].detail == "expert"


class TestSourceIdentityRoutes:
    def test_company_wiki_identities(self) -> None:
        payload = {
            "ok": True,
            "status": "ok",
            "results": [
                {
                    "document_id": "wiki:1632",
                    "source": "company-wiki",
                    "original_source": "专家网络访谈",
                    "title": "2026-10-08-专家访谈.md",
                    "date": "2026-10-08",
                    "tags": ["数字广告"],
                },
                {
                    "document_id": "wiki:1532",
                    "source": "company-wiki",
                    "original_source": "Wolfe Research",
                    "title": "2026-04-16-券商研报.md",
                    "date": "2026-04-16",
                    "tags": ["AI算力"],
                },
                {
                    "document_id": "wiki:1440",
                    "source": "company-wiki",
                    "original_source": "Flomo",
                    "title": "2024-08-30-Flomo笔记.md",
                    "date": "2024-08-30",
                    "tags": ["Meta"],
                },
            ],
        }
        declared = source_identity.declare(
            "research", {"route": "company-wiki", "operation": "search"}, payload
        )
        assert [d["identity"] for d in declared] == ["expert", "broker_report", "internal_note"]
        assert [d["tier"] for d in declared] == [5, 3, None]

    def test_guidepoint_strips_expert_name(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "structuredContent": {
                    "data": [
                        {
                            "id": "qapair:library_ts:640F2FFC-173A-4028-9651-AB8B5383C138_8",
                            "transcript_name": "Meta: Entering the AI Cloud",
                            "date": "2026-07-02",
                            "reference_url": "https://platform.guidepoint.com/transcript?id=640F2FFC",
                            "respondent": {
                                "full_name": "Danish Faruqui",
                                "title": "Founding Partner and CEO",
                                "company": "Fab Economics",
                            },
                        }
                    ]
                }
            },
        }
        declared = source_identity.declare(
            "research", {"route": "guidepoint", "operation": "search_library"}, payload
        )
        assert len(declared) == 1
        assert declared[0]["document_id"] == "guidepoint:640F2FFC-173A-4028-9651-AB8B5383C138"
        assert declared[0]["identity"] == "expert"
        assert "Danish Faruqui" not in declared[0]["publisher"]
        assert "Founding Partner and CEO, Fab Economics" in declared[0]["publisher"]

    def test_sales_notes_digest_vs_raw_and_all_route(self) -> None:
        payload = {
            "ok": True,
            "results": [
                {
                    "document_id": "mail:111154",
                    "source": "sales-notes",
                    "original_source": "BofA",
                    "title": "BofA US 10",
                    "date": "2026-08-04",
                    "layer": "raw",
                },
                {
                    "document_id": "digest:2026-08-04",
                    "source": "sales-notes",
                    "original_source": "market-digest",
                    "title": "Daily Digest",
                    "date": "2026-08-04",
                    "layer": "digest",
                },
                {
                    "document_id": "tmtb:190000041634994",
                    "canonical_id": "alphaengine:190000041634994",
                    "source": "tmtb",
                    "title": "TMTB EOD Wrap",
                    "date": "2026-10-12",
                },
                {
                    "document_id": "funda:26",
                    "source": "funda-ai",
                    "title": "Deep|META",
                    "published_at": "2026-09-21T12:31:45+00:00",
                    "url": "https://fundaai.substack.com/p/deepmeta",
                },
            ],
        }
        declared = source_identity.declare("research", {"route": "all", "operation": "search"}, payload)
        assert len(declared) == 4
        assert declared[0]["identity"] == "broker_report" and declared[0]["independent"] is True
        assert declared[1]["identity"] == "derived" and declared[1]["independent"] is False
        assert declared[2]["document_id"] == "alphaengine:190000041634994"
        assert declared[3]["publisher"] == "FUNDA AI" and declared[3]["as_of_basis"] == "publish_time"

    def test_transcript_sec_estimates_macro_cnhk(self) -> None:
        # transcript
        tr_decl = source_identity.declare(
            "research",
            {"route": "transcript", "ticker": "META"},
            {
                "ok": True,
                "transcripts": [
                    {"doc_id": "130000101576482", "title": "Meta Q2 2026", "publish_time": "2026-07-30", "kind": "company_call"},
                    {"doc_id": "290001790207175", "title": "Meta Callback", "publish_time": "2026-07-30", "kind": "broker_callback"},
                ],
            },
        )
        assert [d["identity"] for d in tr_decl] == ["company", "broker_report"]

        # sec
        sec_decl = source_identity.declare(
            "finance",
            {"route": "sec", "operation": "list-filings", "ticker": "META"},
            {
                "ok": True,
                "ticker": "META",
                "rows": [{"form": "10-Q", "filing_date": "2026-07-30", "report_date": "2026-06-30", "accession_no": "0001628280-26-050705"}],
            },
        )
        assert sec_decl[0]["identity"] == "filing" and sec_decl[0]["source_type"] == "sec_filing"

        # estimates
        est_decl = source_identity.declare(
            "finance",
            {"route": "estimates", "operation": "consensus", "ticker": "META"},
            {"ok": True, "ticker": "META", "fetched_at": "2026-10-18T08:56:41-04:00"},
        )
        assert est_decl[0]["identity"] == "street_estimate" and est_decl[0]["tier"] is None

        # fred
        fred_decl = source_identity.declare(
            "macro",
            {"route": "fred", "series": "DGS10"},
            {"ok": True, "series": "DGS10", "vintage": "2026-10-18", "rows": [{"observation_date": "2026-10-16", "value": 4.08}]},
        )
        assert fred_decl[0]["identity"] == "official_data" and fred_decl[0]["as_of"] == "2026-10-16"

        # cn-hk
        cnhk_decl = source_identity.declare(
            "finance",
            {"route": "cn-hk", "operation": "filings", "ticker": "00700"},
            {"ok": True, "ticker": "00700", "rows": [{"date": "2026-09-15", "title": "Next Day Disclosure", "pdf_link": "https://hkexnews.hk/1.pdf", "index": 1}]},
        )
        assert cnhk_decl[0]["identity"] == "filing" and cnhk_decl[0]["url"] == "https://hkexnews.hk/1.pdf"


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    # Never read the host's bridge env file (it carries the real proxy and token).
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    dummy_xq = tmp_path / "xueqiu_posts.py"
    dummy_xq.write_text("# dummy\n", encoding="utf-8")
    dummy_emp = tmp_path / "employee_reviews_cli.py"
    dummy_emp.write_text("# dummy\n", encoding="utf-8")
    dummy_l30 = tmp_path / "last30days.py"
    dummy_l30.write_text("# dummy\n", encoding="utf-8")
    dummy_xreach = tmp_path / "xreach"
    dummy_xreach.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_XUEQIU_SCRIPT", str(dummy_xq))
    monkeypatch.setenv("DALTON_EMPLOYEE_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_EMPLOYEE_CLI_SCRIPT", str(dummy_emp))
    monkeypatch.setenv("DALTON_LAST30DAYS_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_LAST30DAYS_SCRIPT", str(dummy_l30))
    monkeypatch.setenv("DALTON_XREACH_BIN", str(dummy_xreach))
    cfg = rb.BridgeConfig()
    return rb.ResearchDataBridge(cfg)


def _make_subprocess_double(
    responder: Any,
) -> Any:
    """Create a `create_autospec(asyncio.create_subprocess_exec)` double."""
    exec_mock = create_autospec(asyncio.create_subprocess_exec)

    async def _fake_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0

        async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            lines = (input or b"").decode("utf-8").splitlines()
            reqs = [json.loads(line) for line in lines if line.strip()]
            out_lines, rc = await responder(reqs)
            proc.returncode = rc
            stdout_bytes = (
                "\n".join(json.dumps(obj, ensure_ascii=False) for obj in out_lines) + "\n"
            ).encode("utf-8") if out_lines else b""
            return stdout_bytes, b""

        proc.communicate.side_effect = _communicate
        return proc

    exec_mock.side_effect = _fake_exec
    return exec_mock


def _local_all_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "results": [
            {
                "document_id": "mail:8801",
                "canonical_id": "mail:8801",
                "source": "sales-notes",
                "original_source": "Goldman Sachs",
                "title": "CATL Sales Note",
                "date": "2026-04-16",
                "layer": "raw",
                "snippet": "GS comment on CATL move.",
            }
        ],
        "ranking": "priority_tier_desc,published_at_desc_with_90d_decay,fts_score_desc",
        "source_status": {
            "company-wiki": {"ok": True, "status": "ok", "count": 0},
            "sales-notes": {"ok": True, "status": "ok", "count": 1},
            "tmtb": {"ok": True, "status": "ok", "count": 0},
            "funda-ai": {"ok": True, "status": "ok", "count": 0},
        },
        "retrieved_at": "2026-04-17T00:00:00+00:00",
        "local_only": True,
        "result_count": 1,
    }


def _alphaengine_ok_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "executed": True,
        "operation": "search_library",
        "data": {
            "content": [{"type": "text", "text": "Found 1 document"}],
            "structuredContent": {
                "query": "CATL 宁德时代",
                "total_upstream": 1,
                "returned": 1,
                "results": [
                    {
                        "doc_id": "99001122",
                        "title": "CATL: Volume Drop Assessment",
                        "publish_time": "2026-04-16",
                        "sources": ["Morgan Stanley"],
                        "snippet": "MS assessment on CATL.",
                        "source_attribution": {
                            "markdown": "[Morgan Stanley — CATL (16 Apr 2026)](https://www.alphaengine.io/document/99001122)"
                        },
                    }
                ],
                "has_more": False,
            },
            "isError": False,
        },
    }


def _guidepoint_ok_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "route": "guidepoint",
        "operation": "search_library",
        "data": {
            "content": [{"type": "text", "text": "Found 1 transcript"}],
            "structuredContent": {
                "data": [
                    {
                        "id": "library_ts:gp-778899",
                        "transcript_name": "China EV Battery Supply Chain & Pricing",
                        "date": "2026-04-15",
                        "reference_url": "https://new.guidepoint.com/transcripts/gp-778899",
                        "respondent": {
                            "full_name": "Zhang San",
                            "title": "Former VP of Procurement",
                            "company": "Leading NEV OEM",
                        },
                        "highlights": ["LFP cell pricing pressure easing in Q2"],
                    }
                ]
            },
            "isError": False,
        },
    }


def _mcp_tool_response(req_id: Any, payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        },
    }


class TestResearchAllAlphaEngineAndGuidepointFanout:
    def test_fanout_timeout_constants(self) -> None:
        assert rb.FANOUT_ALPHAENGINE_TIMEOUT_S == 60
        assert rb.FANOUT_GUIDEPOINT_TIMEOUT_S == 90

    @pytest.mark.asyncio
    async def test_fanout_triggered_on_all_search_and_merges_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        recorded_calls: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            params = req["params"]
            args = params["arguments"]
            recorded_calls.append(args)
            if args["route"] == "all":
                return [_mcp_tool_response(req["id"], _local_all_payload())], 0
            if args["route"] == "alphaengine":
                return [_mcp_tool_response(req["id"], _alphaengine_ok_payload())], 0
            if args["route"] == "guidepoint":
                return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())], 0
            raise AssertionError(f"Unexpected route: {args['route']}")

        exec_double = _make_subprocess_double(_responder)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_double)

        resp = await bridge.handle_tools_call(
            "req-1",
            {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "search",
                    "query": "CATL 宁德时代",
                    "ticker": "300750.SZ",
                    "start": "2026-03-01",
                    "end": "2026-04-17",
                    "limit": 5,
                    "params": {
                        "layer": "raw",
                        "sources": ["sales-notes"],
                        "keywords": ["LFP"],
                        "recency_bias": "30",
                    },
                    "document": "ignored-doc",
                },
            },
        )

        routes_called = [c["route"] for c in recorded_calls]
        assert sorted(routes_called) == ["all", "alphaengine", "guidepoint"]

        all_call = next(c for c in recorded_calls if c["route"] == "all")
        assert all_call["params"] == {"layer": "raw", "sources": ["sales-notes"]}

        ae_call = next(c for c in recorded_calls if c["route"] == "alphaengine")
        assert ae_call["operation"] == "search_library"
        assert ae_call["query"] == "300750.SZ CATL 宁德时代"
        assert ae_call["start"] == "2026-03-01"
        assert ae_call["end"] == "2026-04-17"
        assert ae_call["limit"] == 5
        assert ae_call["timeout"] == rb.FANOUT_ALPHAENGINE_TIMEOUT_S == 60
        assert "ticker" not in ae_call
        assert "params" not in ae_call
        assert "document" not in ae_call

        gp_call = next(c for c in recorded_calls if c["route"] == "guidepoint")
        assert gp_call["operation"] == "search_library"
        assert gp_call["query"] == "300750.SZ CATL 宁德时代"
        assert gp_call["start"] == "2026-03-01"
        assert gp_call["end"] == "2026-04-17"
        assert gp_call["limit"] == 5
        assert gp_call["timeout"] == rb.FANOUT_GUIDEPOINT_TIMEOUT_S == 90
        assert gp_call["params"] == {"keywords": ["LFP"], "recency_bias": 30}
        assert "ticker" not in gp_call
        assert "document" not in gp_call

        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["alphaengine"]["ok"] is True
        assert body["alphaengine"]["status"] == "ok"
        assert body["alphaengine"]["route"] == "alphaengine"
        assert body["alphaengine"]["count"] == 1
        assert body["alphaengine"]["results"][0]["route"] == "alphaengine"
        assert body["alphaengine"]["results"][0]["document_id"] == "alphaengine:99001122"
        assert body["source_status"]["alphaengine"]["status"] == "ok"

        assert body["guidepoint"]["ok"] is True
        assert body["guidepoint"]["status"] == "ok"
        assert body["guidepoint"]["route"] == "guidepoint"
        assert body["guidepoint"]["count"] == 1
        gp_item = body["guidepoint"]["results"][0]
        assert gp_item["route"] == "guidepoint"
        assert gp_item["document_id"] == "guidepoint:gp-778899"
        assert "full_name" not in gp_item["respondent"]
        assert gp_item["respondent"]["title"] == "Former VP of Procurement"
        assert body["source_status"]["guidepoint"]["status"] == "ok"

        ae_in_results = [r for r in body["results"] if r.get("route") == "alphaengine"]
        gp_in_results = [r for r in body["results"] if r.get("route") == "guidepoint"]
        assert len(ae_in_results) == 1
        assert len(gp_in_results) == 1
        assert body["result_count"] == 3

        declared = body["_declared_sources"]
        assert len(declared) == 3
        by_route = {d["route"]: d for d in declared}
        assert "sales-notes" in by_route
        assert "alphaengine" in by_route
        assert "guidepoint" in by_route
        assert by_route["alphaengine"]["document_id"] == "alphaengine:99001122"
        assert by_route["alphaengine"]["publisher"] == "Morgan Stanley"
        assert by_route["guidepoint"]["document_id"] == "guidepoint:gp-778899"
        assert by_route["guidepoint"]["title"] == "China EV Battery Supply Chain & Pricing"
        assert by_route["guidepoint"]["publisher"] == "Guidepoint (Former VP of Procurement, Leading NEV OEM)"
        assert by_route["guidepoint"]["identity"] == "expert"

    @pytest.mark.asyncio
    async def test_fanout_triggered_when_operation_omitted_with_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        recorded_calls: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"]["arguments"]
            recorded_calls.append(args)
            if args["route"] == "all":
                return [_mcp_tool_response(req["id"], _local_all_payload())], 0
            if args["route"] == "guidepoint":
                return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())], 0
            return [_mcp_tool_response(req["id"], _alphaengine_ok_payload())], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        await bridge.handle_tools_call(
            "req-2",
            {"name": "research", "arguments": {"route": "all", "query": "CATL"}},
        )
        assert sorted(c["route"] for c in recorded_calls) == ["all", "alphaengine", "guidepoint"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,arguments",
        [
            ("research", {"route": "all", "operation": "read", "document": "mail:8801"}),
            ("research", {"route": "all", "operation": "get_document", "document": "wiki:1"}),
            ("research", {"route": "all", "operation": "list"}),
            ("research", {"route": "all"}),
            ("research", {"route": "sales-notes", "operation": "search", "query": "CATL"}),
            ("finance", {"route": "sec", "operation": "search", "ticker": "META", "query": "CATL"}),
        ],
    )
    async def test_fanout_not_triggered_for_non_all_search(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        recorded_calls: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            recorded_calls.append(req["params"]["arguments"])
            return [_mcp_tool_response(req["id"], {"ok": True, "status": "ok", "results": []})], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        await bridge.handle_tools_call(
            "req-non-fanout", {"name": tool_name, "arguments": arguments}
        )
        assert len(recorded_calls) == 1
        assert recorded_calls[0]["route"] == arguments["route"]

    @pytest.mark.asyncio
    async def test_alphaengine_failure_preserves_local_and_guidepoint_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"]["arguments"]
            if args["route"] == "all":
                return [_mcp_tool_response(req["id"], _local_all_payload())], 0
            if args["route"] == "guidepoint":
                return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())], 0
            return [], 2

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        resp = await bridge.handle_tools_call(
            "req-fail",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "partial_error"
        assert set(body["partial"]) == {
            "company-wiki",
            "sales-notes",
            "tmtb",
            "funda-ai",
            "guidepoint",
        }
        assert body["error"]
        assert len(body["results"]) == 2
        assert body["results"][0]["document_id"] == "mail:8801"
        assert body["results"][1]["document_id"] == "guidepoint:gp-778899"
        assert len(body["_declared_sources"]) == 2
        assert body["alphaengine"]["ok"] is False
        assert body["alphaengine"]["status"] == "failed"
        assert "SubprocessExitError" in body["alphaengine"]["reason"]
        assert body["source_status"]["alphaengine"]["status"] == "failed"
        assert body["guidepoint"]["ok"] is True
        assert body["source_status"]["guidepoint"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_alphaengine_timeout_preserves_local_and_guidepoint_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        orig_rpc = bridge._run_stdio_rpc

        async def _rpc_side_effect(
            requests: list[dict[str, Any]], timeout_s: float
        ) -> list[dict[str, Any]]:
            req = requests[0]
            args = req["params"]["arguments"]
            if args["route"] == "alphaengine":
                raise asyncio.TimeoutError()
            if args["route"] == "guidepoint":
                return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())]
            return [_mcp_tool_response(req["id"], _local_all_payload())]

        rpc_double = create_autospec(orig_rpc, side_effect=_rpc_side_effect)
        monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

        resp = await bridge.handle_tools_call(
            "req-timeout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert set(body["partial"]) == {
            "company-wiki",
            "sales-notes",
            "tmtb",
            "funda-ai",
            "guidepoint",
        }
        assert len(body["results"]) == 2
        assert body["alphaengine"]["ok"] is False
        assert body["alphaengine"]["status"] == "timeout"
        assert "timed out" in body["alphaengine"]["reason"]
        assert body["source_status"]["alphaengine"]["status"] == "timeout"
        assert body["guidepoint"]["ok"] is True
        assert body["source_status"]["guidepoint"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_guidepoint_failure_preserves_local_and_alphaengine_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"]["arguments"]
            if args["route"] == "all":
                return [_mcp_tool_response(req["id"], _local_all_payload())], 0
            if args["route"] == "alphaengine":
                return [_mcp_tool_response(req["id"], _alphaengine_ok_payload())], 0
            return [], 2

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        resp = await bridge.handle_tools_call(
            "req-gp-fail",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "partial_error"
        assert set(body["partial"]) == {
            "company-wiki",
            "sales-notes",
            "tmtb",
            "funda-ai",
            "alphaengine",
        }
        assert len(body["results"]) == 2
        assert {r["document_id"] for r in body["results"]} == {
            "mail:8801",
            "alphaengine:99001122",
        }
        assert body["alphaengine"]["ok"] is True
        assert body["guidepoint"]["ok"] is False
        assert body["guidepoint"]["status"] == "failed"
        assert "SubprocessExitError" in body["guidepoint"]["reason"]
        assert body["source_status"]["guidepoint"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_guidepoint_timeout_preserves_local_and_alphaengine_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        orig_rpc = bridge._run_stdio_rpc

        async def _rpc_side_effect(
            requests: list[dict[str, Any]], timeout_s: float
        ) -> list[dict[str, Any]]:
            req = requests[0]
            args = req["params"]["arguments"]
            if args["route"] == "guidepoint":
                raise asyncio.TimeoutError()
            if args["route"] == "alphaengine":
                return [_mcp_tool_response(req["id"], _alphaengine_ok_payload())]
            return [_mcp_tool_response(req["id"], _local_all_payload())]

        rpc_double = create_autospec(orig_rpc, side_effect=_rpc_side_effect)
        monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

        resp = await bridge.handle_tools_call(
            "req-gp-timeout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert set(body["partial"]) == {
            "company-wiki",
            "sales-notes",
            "tmtb",
            "funda-ai",
            "alphaengine",
        }
        assert len(body["results"]) == 2
        assert body["alphaengine"]["ok"] is True
        assert body["guidepoint"]["ok"] is False
        assert body["guidepoint"]["status"] == "timeout"
        assert "timed out" in body["guidepoint"]["reason"]
        assert body["source_status"]["guidepoint"]["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_stdio_rpc_cancellation_kills_and_waits_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        started = asyncio.Event()
        block = asyncio.Event()
        spawned_procs: list[asyncio.subprocess.Process] = []

        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = None

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                started.set()
                await block.wait()
                return b"", b""

            proc.communicate.side_effect = _communicate
            spawned_procs.append(proc)
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        task = asyncio.create_task(
            bridge._run_stdio_rpc([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], timeout_s=30.0)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(spawned_procs) == 1
        spawned_procs[0].kill.assert_called_once()
        spawned_procs[0].wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_local_all_fast_failure_cleans_up_pending_fanout_and_keeps_completed_online_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        gp_blocked = asyncio.Event()
        procs_by_route: dict[str, asyncio.subprocess.Process] = {}

        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                lines = (input or b"").decode("utf-8").splitlines()
                req = json.loads(lines[0])
                route = req["params"]["arguments"]["route"]
                procs_by_route[route] = proc
                if route == "alphaengine":
                    out = _mcp_tool_response(req["id"], _alphaengine_ok_payload())
                    return (json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"), b""
                if route == "guidepoint":
                    await gp_blocked.wait()
                    return b"", b""
                proc.returncode = 2
                return b"", b""

            proc.communicate.side_effect = _communicate
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            "req-local-fast-fail",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["structuredContent"] == body
        assert body["ok"] is False
        assert body["partial"] == ["alphaengine"]
        assert body["error"]
        assert [r["document_id"] for r in body["results"]] == ["alphaengine:99001122"]
        assert body["alphaengine"]["ok"] is True
        assert body["alphaengine"]["status"] == "ok"
        assert body["guidepoint"]["ok"] is False
        assert body["guidepoint"]["status"] == "canceled"
        assert body["source_status"]["local_all"]["status"] == "failed"
        assert body["source_status"]["alphaengine"]["status"] == "ok"
        assert body["source_status"]["guidepoint"]["status"] == "canceled"
        assert [d["document_id"] for d in body["_declared_sources"]] == ["alphaengine:99001122"]

        procs_by_route["guidepoint"].kill.assert_called_once()
        procs_by_route["guidepoint"].wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_local_all_timeout_preserves_completed_alphaengine_and_guidepoint_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        orig_rpc = bridge._run_stdio_rpc

        async def _rpc_side_effect(
            requests: list[dict[str, Any]], timeout_s: float
        ) -> list[dict[str, Any]]:
            req = requests[0]
            route = req["params"]["arguments"]["route"]
            if route == "all":
                await asyncio.sleep(0)
                raise asyncio.TimeoutError()
            if route == "alphaengine":
                return [_mcp_tool_response(req["id"], _alphaengine_ok_payload())]
            return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())]

        rpc_double = create_autospec(orig_rpc, side_effect=_rpc_side_effect)
        monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

        resp = await bridge.handle_tools_call(
            "req-local-timeout",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["structuredContent"] == body
        assert body["ok"] is False
        assert body["partial"] == ["alphaengine", "guidepoint"]
        assert body["error"]
        assert {r["document_id"] for r in body["results"]} == {
            "alphaengine:99001122",
            "guidepoint:gp-778899",
        }
        assert body["source_status"]["local_all"]["status"] == "timeout"
        assert body["source_status"]["alphaengine"]["status"] == "ok"
        assert body["source_status"]["guidepoint"]["status"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", ["content_text_json", "data_results"])
    async def test_alphaengine_unwraps_proxy_text_json_and_data_results_and_syncs_structured_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        item = {
            "doc_id": "99001122",
            "title": "CATL: Volume Drop Assessment",
            "publish_time": "2026-04-16",
            "sources": ["Morgan Stanley"],
            "snippet": "MS assessment on CATL.",
        }
        if shape == "content_text_json":
            ae_payload: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"results": [item]}, ensure_ascii=False),
                        }
                    ],
                    "isError": False,
                },
            }
        else:
            ae_payload = {
                "ok": True,
                "status": "ok",
                "data": {"results": [item]},
            }

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            route = req["params"]["arguments"]["route"]
            if route == "all":
                local_p = _local_all_payload()
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(local_p, ensure_ascii=False),
                                }
                            ],
                            "structuredContent": dict(local_p),
                            "isError": False,
                        },
                    }
                ], 0
            if route == "alphaengine":
                return [_mcp_tool_response(req["id"], ae_payload)], 0
            return [_mcp_tool_response(req["id"], _guidepoint_ok_payload())], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        resp = await bridge.handle_tools_call(
            f"req-{shape}",
            {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "CATL"},
            },
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        sc = resp["result"]["structuredContent"]
        assert sc == body
        assert body["alphaengine"]["ok"] is True
        assert body["alphaengine"]["count"] == 1
        assert body["alphaengine"]["results"][0]["document_id"] == "alphaengine:99001122"
        assert sc["alphaengine"]["count"] == 1
        assert sc["guidepoint"]["count"] == 1
        assert sc["result_count"] == 3
        assert {d["route"] for d in body["_declared_sources"]} == {
            "sales-notes",
            "alphaengine",
            "guidepoint",
        }


class TestToolsListResearchDescription:
    @pytest.mark.asyncio
    async def test_research_description_appends_situational_route_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            if req["method"] == "initialize":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "serverInfo": {"name": "researchdata", "version": "1.0.0"},
                        },
                    }
                ], 0
            if req["method"] == "tools/list":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "research",
                                    "description": "Internal research library, transcripts, broker notes, and expert interviews.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {
                                                "type": "string",
                                                "enum": [
                                                    "all",
                                                    "alphaengine",
                                                    "guidepoint",
                                                    "transcript",
                                                    "company-wiki",
                                                    "sales-notes",
                                                    "tmtb",
                                                    "funda-ai",
                                                    "employee",
                                                ],
                                            }
                                        },
                                    },
                                },
                                {
                                    "name": "catalog",
                                    "description": "List routes.",
                                    "inputSchema": {"type": "object", "properties": {}},
                                },
                            ]
                        },
                    }
                ], 0
            raise AssertionError(f"Unexpected method: {req['method']}")

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        tools = await bridge.get_filtered_tools()
        research_tool = next(t for t in tools if t["name"] == "research")
        catalog_tool = next(t for t in tools if t["name"] == "catalog")

        desc = research_tool["description"]
        assert "Internal research library" in desc
        assert "各来源提供什么证据：" in desc
        assert "何时选用哪个路由（按研究情境，基于 catalog）：" in desc
        assert "→ alphaengine（先 search_library 再 get_document）" in desc
        assert "→ guidepoint；" in desc
        assert "→ transcript（AlphaEngine 业绩会转写；无匹配时用 | 分隔中英文公司名重试或扩大 start/end）" in desc
        assert "roic" not in desc.lower()
        assert "→ company-wiki（只读，params.company=TICKER）" in desc
        assert "→ sales-notes；" in desc
        assert "→ tmtb；" in desc
        assert "→ funda-ai；" in desc
        assert "→ all（company-wiki + sales-notes + tmtb + funda-ai，并行检索 AlphaEngine 与 Guidepoint；不含 transcript）" in desc
        assert "ok: false` 不等于“没有数据”" in desc

        assert "employee" not in research_tool["inputSchema"]["properties"]["route"]["enum"]
        assert catalog_tool["description"].startswith("List routes.")
        assert "route" in catalog_tool["inputSchema"]["properties"]

    def test_plugin_json_instruction_shares_single_source_with_bridge(self) -> None:
        plugin_path = (
            Path(__file__).resolve().parents[3] / "plugins" / "dalton_data" / "plugin.json"
        )
        plugin_data = json.loads(plugin_path.read_text(encoding="utf-8"))
        srv = plugin_data["extensions"]["ai.langalpha"]["servers"]["researchdata"]
        assert srv["instruction"] == rb.build_researchdata_plugin_instruction()
        assert rb.RESEARCH_EVIDENCE_AND_ROUTE_GUIDE.replace("\n", " ") in srv["instruction"]
        assert "Keep date windows narrow" not in srv["instruction"]
        assert "AlphaEngine full text is quota-limited, so read only what you need" not in srv["instruction"]
        assert "检索可以放开，全文阅读（get_document）计配额，读最相关的几篇" in srv["instruction"]


class TestDaltonSkillRegistrySingleSource:
    def test_all_24_dalton_skills_match_skill_md_frontmatter(self) -> None:
        import yaml

        from ptc_agent.agent.middleware.skills.dalton_registry import (
            DALTON_SKILL_DEFINITIONS,
            DALTON_SKILLS_DIR,
            _DALTON_SKILL_SPECS,
        )
        from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

        assert len(_DALTON_SKILL_SPECS) == 24  # + dalton-transcript-polish (A1d)
        assert len(DALTON_SKILL_DEFINITIONS) == 24

        for name, skill_def in DALTON_SKILL_DEFINITIONS.items():
            skill_md = DALTON_SKILLS_DIR / name / "SKILL.md"
            assert skill_md.is_file(), f"Missing SKILL.md for {name}"
            text = skill_md.read_text(encoding="utf-8")
            assert text.startswith("---\n")
            end_idx = text.find("\n---\n", 4)
            frontmatter = yaml.safe_load(text[4:end_idx])
            expected_desc = frontmatter["description"].strip()

            assert expected_desc, f"Empty description in {skill_md}"
            assert skill_def.description == expected_desc
            assert SKILL_REGISTRY[name].description == expected_desc
            assert "触发词" not in expected_desc, f"{name} still has trigger keyword list"
            assert len(expected_desc) <= 120, f"{name} description exceeds 120 chars: {len(expected_desc)}"

    def test_dalton_onboarding_skill_and_interview_entry(self) -> None:
        from ptc_agent.agent.middleware.skills.content import (
            SkillRequest,
            build_skill_content,
            load_skill_content,
        )
        from ptc_agent.agent.middleware.skills.registry import (
            SKILL_REGISTRY,
            get_command_to_skill_map,
            list_skills,
            skills_outside_sandbox,
        )

        skill = SKILL_REGISTRY["dalton-onboarding"]
        assert skill.hidden is True
        assert skill.command is None
        assert skill.requires == ()
        assert "dalton-onboarding" not in {s["name"] for s in list_skills()}
        assert "dalton-onboarding" not in get_command_to_skill_map().values()
        assert "dalton-onboarding" in skills_outside_sandbox()

        body = load_skill_content("dalton-onboarding")
        assert body is not None
        assert ".agents/user/profile/README.md" in body
        assert ".agents/user/profile/portfolio.json" in body
        assert ".agents/user/profile/watchlist.json" in body
        assert ".agents/user/profile/preference.json" in body
        assert ".agents/user/profile/user.json" in body
        assert '"onboarding_completed": true' in body
        assert "get_brokerage_status" not in body

        injected = build_skill_content(
            [SkillRequest(name="dalton-onboarding", instruction="Start the Dalton onboarding interview")]
        )
        assert injected is not None
        assert injected.loaded_skill_names == ["dalton-onboarding"]
        assert '<loaded-skill name="dalton-onboarding">' in injected.content

        interview_tsx = (
            Path(__file__).resolve().parents[3]
            / "web"
            / "src"
            / "pages"
            / "Onboarding"
            / "dalton"
            / "interview.tsx"
        ).read_text(encoding="utf-8")
        assert "name: 'dalton-onboarding'" in interview_tsx
        assert "name: 'onboarding'" not in interview_tsx

    def test_composer_drafts_thread_send_result_chain(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        chat_view = (repo_root / "web/src/pages/ChatAgent/components/ChatView.tsx").read_text(
            encoding="utf-8"
        )
        use_chat_messages = (
            repo_root / "web/src/pages/ChatAgent/hooks/useChatMessages.ts"
        ).read_text(encoding="utf-8")
        chat_drafts = (repo_root / "web/src/lib/dalton/chatDrafts.ts").read_text(
            encoding="utf-8"
        )

        assert "return stableSendMessage(" in chat_view
        assert "): Promise<boolean> => {" in use_chat_messages
        assert "(resolved) => pending.finalizeSend(resolved === true)" in chat_drafts
        assert "pending.finalizeSend(result === true);" in chat_drafts

    def test_missing_skill_md_raises_at_load(self, tmp_path: Path) -> None:
        from ptc_agent.agent.middleware.skills.dalton_registry import (
            build_dalton_skill_definitions,
            read_skill_description,
        )

        with pytest.raises(FileNotFoundError, match="Missing Dalton SKILL.md"):
            read_skill_description("dalton-nonexistent", skills_dir=tmp_path)

        with pytest.raises(FileNotFoundError, match="Missing Dalton SKILL.md"):
            build_dalton_skill_definitions(skills_dir=tmp_path)

    def test_missing_or_empty_description_raises_at_load(self, tmp_path: Path) -> None:
        from ptc_agent.agent.middleware.skills.dalton_registry import read_skill_description

        skill_dir = tmp_path / "dalton-broken"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"

        skill_md.write_text("# No frontmatter\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing YAML frontmatter"):
            read_skill_description("dalton-broken", skills_dir=tmp_path)

        skill_md.write_text("---\nname: dalton-broken\n---\nbody\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing or empty 'description'"):
            read_skill_description("dalton-broken", skills_dir=tmp_path)

        skill_md.write_text("---\nname: dalton-broken\ndescription: '   '\n---\nbody\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing or empty 'description'"):
            read_skill_description("dalton-broken", skills_dir=tmp_path)




class _SlowWaitProcess:
    """A stand-in for an asyncio subprocess whose wait() takes a little while."""

    def __init__(self) -> None:
        self.waited = False

    async def wait(self) -> int:
        await asyncio.sleep(0.05)
        self.waited = True
        return -9


@pytest.mark.asyncio
async def test_reap_process_finishes_waiting_when_cancelled_mid_wait() -> None:
    proc = _SlowWaitProcess()
    task = asyncio.create_task(rb._reap_process(proc))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.waited is True


@pytest.mark.asyncio
async def test_cancel_and_reap_cancels_pending_tasks_even_if_caller_is_cancelled() -> None:
    finished: list[str] = []

    async def online(name: str) -> None:
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.02)
            finished.append(name)

    ae = asyncio.create_task(online("alphaengine"))
    gp = asyncio.create_task(online("guidepoint"))
    await asyncio.sleep(0)
    reaper = asyncio.create_task(rb._cancel_and_reap((ae, gp)))
    await asyncio.sleep(0.005)
    reaper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reaper
    assert ae.cancelled() and gp.cancelled()
    assert sorted(finished) == ["alphaengine", "guidepoint"]


async def _send_raw_bridge_request(
    bridge: rb.ResearchDataBridge,
    path: str,
    body_obj: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body_bytes = json.dumps(body_obj).encode("utf-8")
    hdr_lines = [
        f"POST {path} HTTP/1.1",
        "Host: 127.0.0.1:8960",
        "Content-Type: application/json",
        f"Content-Length: {len(body_bytes)}",
    ]
    for hk, hv in (headers or {}).items():
        hdr_lines.append(f"{hk}: {hv}")
    raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8") + body_bytes

    reader = asyncio.StreamReader()
    reader.feed_data(raw_req)
    reader.feed_eof()

    writer = create_autospec(asyncio.StreamWriter, instance=True)
    await rb._handle_client(bridge, reader, writer)

    written: bytes = b"".join(call.args[0] for call in writer.write.call_args_list)
    head_bytes, payload_bytes = written.split(b"\r\n\r\n", 1)
    status_line = head_bytes.splitlines()[0].decode("utf-8")
    status_code = int(status_line.split(" ", 2)[1])
    return status_code, json.loads(payload_bytes.decode("utf-8"))


@pytest.mark.asyncio
async def test_bridge_accepts_header_token_rejects_bad_token_401_and_keeps_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "secret-bridge-token")
    bridge = _make_bridge(tmp_path, monkeypatch)
    ping_rpc = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    # 1. Header token (Authorization: Bearer <token> and X-Dalton-Bridge-Token) on /mcp -> 200
    status_bearer, body_bearer = await _send_raw_bridge_request(
        bridge,
        "/mcp",
        ping_rpc,
        headers={"Authorization": "Bearer secret-bridge-token"},
    )
    assert status_bearer == 200
    assert body_bearer == {"jsonrpc": "2.0", "id": 1, "result": {}}

    status_xhdr, body_xhdr = await _send_raw_bridge_request(
        bridge,
        "/mcp",
        ping_rpc,
        headers={"X-Dalton-Bridge-Token": "secret-bridge-token"},
    )
    assert status_xhdr == 200
    assert body_xhdr == {"jsonrpc": "2.0", "id": 1, "result": {}}

    # 2. Wrong or missing token -> 401
    status_bad_hdr, _ = await _send_raw_bridge_request(
        bridge,
        "/mcp",
        ping_rpc,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert status_bad_hdr == 401

    status_missing_hdr, _ = await _send_raw_bridge_request(
        bridge,
        "/mcp",
        ping_rpc,
    )
    assert status_missing_hdr == 401

    status_bad_path, _ = await _send_raw_bridge_request(
        bridge,
        "/mcp/wrong-token",
        ping_rpc,
    )
    assert status_bad_path == 401

    # 3. Legacy path /mcp/<token> still works -> 200
    status_legacy, body_legacy = await _send_raw_bridge_request(
        bridge,
        "/mcp/secret-bridge-token",
        ping_rpc,
    )
    assert status_legacy == 200
    assert body_legacy == {"jsonrpc": "2.0", "id": 1, "result": {}}


class TestCommunityExecutors:
    @pytest.mark.asyncio
    async def test_handoff_only_releases_four_community_routes_and_keeps_others_filtered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ds_file = tmp_path / "data_sources.json"
        ds_file.write_text(
            json.dumps(
                {
                    "routes": [
                        {"id": "xueqiu", "kind": "skill"},
                        {"id": "employee", "kind": "skill"},
                        {"id": "last30days", "kind": "skill"},
                        {"id": "x-timeline", "kind": "skill"},
                        {"id": "agent-reach", "kind": "skill"},
                        {"id": "web-news", "kind": "agent_tool"},
                        {"id": "web-page", "kind": "agent_tool"},
                        {"id": "filings", "kind": "agent_tool"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        bridge = _make_bridge(tmp_path, monkeypatch)
        bridge.cfg.data_sources_json = ds_file

        handoff = bridge.handoff_routes()
        for released in ("xueqiu", "employee", "last30days", "x-timeline"):
            assert released not in handoff
        assert {"agent-reach", "web-news", "web-page", "filings"} <= handoff

        for blocked in ("agent-reach", "web-news", "web-page", "filings"):
            resp = await bridge.handle_tools_call(
                10,
                {"name": "community", "arguments": {"route": blocked}},
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False
            assert body["status"] == "handoff_filtered"

        # x-timeline is bridge-executed now: an invalid call is rejected as invalid_arguments,
        # never as handoff_filtered.
        resp_xtl = await bridge.handle_tools_call(
            11,
            {"name": "community", "arguments": {"route": "x-timeline", "operation": "auth"}},
        )
        body_xtl = json.loads(resp_xtl["result"]["content"][0]["text"])
        assert body_xtl["ok"] is False
        assert body_xtl["status"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_tools_list_and_catalog_expose_four_community_routes_and_schemas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(
            reqs: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            if req["method"] == "initialize":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "serverInfo": {"name": "researchdata", "version": "1.0.0"},
                        },
                    }
                ], 0
            if req["method"] == "tools/list":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "community",
                                    "description": "Community sources.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {
                                                "type": "string",
                                                "enum": [
                                                    "xueqiu",
                                                    "employee",
                                                    "last30days",
                                                    "agent-reach",
                                                    "x-timeline",
                                                ],
                                            }
                                        },
                                    },
                                }
                            ]
                        },
                    }
                ], 0
            if req["method"] == "tools/call" and req["params"]["name"] == "catalog":
                cat_payload = {
                    "ok": True,
                    "status": "ok",
                    "routes": [
                        {"id": "xueqiu", "kind": "skill", "category": "community", "operations": []},
                        {"id": "employee", "kind": "skill", "category": "community", "operations": []},
                        {"id": "last30days", "kind": "skill", "category": "community", "operations": []},
                        {"id": "agent-reach", "kind": "skill", "category": "community", "operations": []},
                    ],
                }
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(cat_payload)}]
                        },
                    }
                ], 0
            raise AssertionError(f"Unexpected method: {req}")

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        tools = await bridge.get_filtered_tools()
        comm_tool = next(t for t in tools if t["name"] == "community")
        enum_vals = comm_tool["inputSchema"]["properties"]["route"]["enum"]
        assert enum_vals == ["xueqiu", "employee", "last30days", "x-timeline"]
        assert "allow_paid" in comm_tool["inputSchema"]["properties"]
        assert "plan" in comm_tool["inputSchema"]["properties"]
        assert "handle" in comm_tool["inputSchema"]["properties"]
        assert "include_replies" in comm_tool["inputSchema"]["properties"]
        assert "xueqiu" in comm_tool["description"]
        assert "employee" in comm_tool["description"]
        assert "last30days" in comm_tool["description"]
        assert "x-timeline" in comm_tool["description"]

        cat_resp = await bridge.handle_tools_call(20, {"name": "catalog", "arguments": {}})
        cat_body = json.loads(cat_resp["result"]["content"][0]["text"])
        by_id = {r["id"]: r for r in cat_body["routes"]}
        assert "agent-reach" not in by_id
        assert by_id["xueqiu"]["operations"] == ["posts", "post", "comments", "search", "user"]
        assert "ticker" in by_id["xueqiu"]["parameters"]
        assert by_id["employee"]["operations"] == ["blind", "summary", "indeed", "glassdoor"]
        assert "allow_paid" in by_id["employee"]["parameters"]
        assert by_id["last30days"]["operations"] == ["search"]
        assert "plan" in by_id["last30days"]["parameters"]
        assert by_id["x-timeline"]["operations"] == ["tweets", "tweet", "thread", "search"]
        assert "handle" in by_id["x-timeline"]["parameters"]
        xtl_rules = " ".join(by_id["x-timeline"]["rules"])
        assert "x_search" in xtl_rules
        assert "去重" in xtl_rules

    @pytest.mark.asyncio
    async def test_parameter_whitelist_and_injection_rejection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        bad_calls = [
            # Shell injection in ticker
            {"route": "xueqiu", "operation": "posts", "ticker": "00700; rm -rf /"},
            # Disallowed operation (self-test)
            {"route": "xueqiu", "operation": "self-test"},
            # Pages out of range (1..3 for xueqiu)
            {"route": "xueqiu", "operation": "posts", "ticker": "00700", "pages": 4},
            # Non-whitelisted URL domain for xueqiu
            {"route": "xueqiu", "operation": "post", "url": "https://evil.com/xueqiu.com/123"},
            # Glassdoor URL passed to xueqiu
            {"route": "xueqiu", "operation": "post", "url": "https://www.glassdoor.com/Reviews/1"},
            # Non-numeric post_id
            {"route": "xueqiu", "operation": "comments", "post_id": "123;id"},
            # Disallowed self-test on employee
            {"route": "employee", "operation": "self-test"},
            # Shell injection in company
            {"route": "employee", "operation": "blind", "company": "NVIDIA && curl evil.com"},
            # Pages out of range for blind (max 3) and summary (max 40)
            {"route": "employee", "operation": "blind", "company": "NVIDIA", "pages": 5},
            {"route": "employee", "operation": "summary", "company": "NVIDIA", "pages": 41},
            # Xueqiu URL passed to employee glassdoor
            {
                "route": "employee",
                "operation": "glassdoor",
                "url": "https://xueqiu.com/S/NVDA",
                "allow_paid": True,
            },
            # Extra parameter on last30days (e.g. search or non-search operation)
            {"route": "last30days", "query": "NVDA", "search": "reddit"},
            {"route": "last30days", "query": "NVDA", "operation": "self-test"},
            # Days out of range (1..30)
            {"route": "last30days", "query": "NVDA", "days": 31},
            # Invalid plan missing ranking_query
            {
                "route": "last30days",
                "query": "NVDA",
                "plan": {"subqueries": [{"search_query": "NVDA"}]},
            },
        ]

        for idx, args in enumerate(bad_calls):
            resp = await bridge.handle_tools_call(
                100 + idx,
                {"name": "community", "arguments": args},
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Expected failure for {args}"
            assert body["status"] == "invalid_arguments"

        # Subprocess must never be spawned for any rejected input
        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_employee_paid_source_switch_and_free_operations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "ok": True,
                "source": "indeed" if "indeed" in cmd else "glassdoor",
                "company": "Nvidia",
                "count": 1,
                "reviews": [
                    {
                        "id": "rev-1",
                        "source": "indeed" if "indeed" in cmd else "glassdoor",
                        "url": "https://www.glassdoor.com/Reviews/Employee-Review-1.htm",
                        "created_at": "2026-10-19",
                        "jobgroup": "Hardware Engineer",
                        "summary": "Great GPU culture",
                    }
                ],
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # 1. indeed and glassdoor without allow_paid=True -> paid_source_not_allowed
        for paid_args in (
            {"route": "employee", "operation": "indeed", "company": "Nvidia", "pages": 2},
            {
                "route": "employee",
                "operation": "glassdoor",
                "url": "https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm",
            },
            {
                "route": "employee",
                "operation": "indeed",
                "company": "Nvidia",
                "allow_paid": False,
            },
        ):
            resp = await bridge.handle_tools_call(
                200, {"name": "community", "arguments": paid_args}
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False
            assert body["status"] == "paid_source_not_allowed"
            assert body["error_code"] == "paid_source_not_allowed"

        assert exec_mock.await_count == 0

        # 2. With allow_paid=True -> executes subprocess and returns unified envelope + _sources
        resp_ok = await bridge.handle_tools_call(
            201,
            {
                "name": "community",
                "arguments": {
                    "route": "employee",
                    "operation": "glassdoor",
                    "url": "https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm",
                    "allow_paid": True,
                },
            },
        )
        body_ok = json.loads(resp_ok["result"]["content"][0]["text"])
        assert exec_mock.await_count == 1
        assert body_ok["ok"] is True
        assert body_ok["status"] == "ok"
        assert len(body_ok["_sources"]) == 1
        src = body_ok["_sources"][0]
        assert src["platform"] == "glassdoor"
        assert src["url"] == "https://www.glassdoor.com/Reviews/Employee-Review-1.htm"
        assert src["date"] == "2026-10-19"
        assert src["author"] == "Hardware Engineer"

    @pytest.mark.asyncio
    async def test_three_failure_statuses_and_stderr_sanitization_last_500_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        mode = ["script_error"]

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            long_prefix = "P" * 600
            dirty_stderr = (
                f"{long_prefix} Authorization: Bearer sk-secretToken9876543210 "
                "Cookie: xq_a_token=secretCookieVal123\nfinal error tail"
            ).encode("utf-8")
            if mode[0] == "script_error":
                proc.returncode = 2
                proc.communicate.return_value = (b"", dirty_stderr)
            elif mode[0] == "invalid_output":
                proc.returncode = 0
                proc.communicate.return_value = (b"not-a-json-document", dirty_stderr)
            else:
                proc.returncode = 0
                proc.communicate.return_value = (b"[]", dirty_stderr)
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for expected_status in ("script_error", "invalid_output", "empty"):
            mode[0] = expected_status
            resp = await bridge.handle_tools_call(
                300,
                {
                    "name": "community",
                    "arguments": {
                        "route": "xueqiu",
                        "operation": "posts",
                        "ticker": "00700",
                        "pages": 1,
                    },
                },
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            if expected_status == "empty":
                assert body["ok"] is True
                assert body["empty_reason"] == "no_matching_items"
                assert resp["result"]["isError"] is False
            else:
                assert body["ok"] is False
                assert resp["result"]["isError"] is True
            assert body["status"] == expected_status
            assert body["_sources"] == []
            assert body["_declared_sources"] == []
            assert "sk-secretToken" not in body["stderr"]
            assert "secretCookieVal" not in body["stderr"]
            assert "[REDACTED]" in body["stderr"]
            assert body["stderr"].endswith("final error tail")
            assert len(body["stderr"]) <= 500

    @pytest.mark.asyncio
    async def test_timeout_reaps_subprocess_and_last30days_plan_tempfile_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "bridge-cache"
        monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(cache_dir))
        bridge = _make_bridge(tmp_path, monkeypatch)

        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        killed = [False]
        waited = [False]
        observed_plan_path: list[Path] = []
        should_timeout = [False]

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            cmd_list = list(cmd)
            if "--plan" in cmd_list:
                idx = cmd_list.index("--plan")
                p_path = Path(cmd_list[idx + 1])
                observed_plan_path.append(p_path)
                assert p_path.is_file()
                assert p_path.parent == cache_dir.resolve()
                assert bridge.cfg.ocw_root.resolve() not in p_path.parents

            def _kill() -> None:
                killed[0] = True

            async def _wait() -> int:
                waited[0] = True
                return -9

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                if should_timeout[0]:
                    await asyncio.sleep(30.0)
                out = {
                    "schema_version": "1.4",
                    "query": "NVDA",
                    "generated_at": "2026-10-22T12:00:00Z",
                    "results": [
                        {
                            "candidate_id": "c1",
                            "source": "stocktwits",
                            "url": "https://stocktwits.com/symbol/NVDA/1",
                            "published_at": "2026-10-22",
                            "author": "chip_bull",
                            "title": "NVDA Blackwell demand strong",
                        }
                    ],
                }
                proc.returncode = 0
                return json.dumps(out).encode("utf-8"), b""

            proc.terminate.side_effect = _kill
            proc.kill.side_effect = _kill
            proc.wait.side_effect = _wait
            proc.communicate.side_effect = _communicate
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        plan_payload = {
            "subqueries": [
                {
                    "label": "primary",
                    "search_query": "NVDA Blackwell",
                    "ranking_query": "How is NVDA Blackwell sentiment?",
                    "sources": ["reddit", "x", "stocktwits", "polymarket"],
                    "weight": 1.0,
                }
            ]
        }

        # 1. Successful call with plan -> plan file created in cache_dir and deleted afterwards
        resp_ok = await bridge.handle_tools_call(
            401,
            {
                "name": "community",
                "arguments": {
                    "route": "last30days",
                    "query": "NVDA",
                    "days": 14,
                    "quick": True,
                    "plan": plan_payload,
                },
            },
        )
        body_ok = json.loads(resp_ok["result"]["content"][0]["text"])
        assert body_ok["ok"] is True
        assert body_ok["status"] == "ok"
        assert len(observed_plan_path) == 1
        assert not observed_plan_path[0].exists()
        assert list(cache_dir.glob("last30days-plan-*.json")) == []
        assert body_ok["_sources"][0]["platform"] == "stocktwits"
        assert body_ok["_sources"][0]["url"] == "https://stocktwits.com/symbol/NVDA/1"
        assert body_ok["_sources"][0]["date"] == "2026-10-22"
        assert body_ok["_sources"][0]["author"] == "chip_bull"

        # 2. Timeout -> kills and reaps subprocess AND deletes plan temp file
        should_timeout[0] = True
        monkeypatch.setattr(bridge, "compute_timeout", lambda *_a, **_kw: 1)
        resp_to = await bridge.handle_tools_call(
            402,
            {
                "name": "community",
                "arguments": {
                    "route": "last30days",
                    "query": "NVDA",
                    "days": 7,
                    "plan": plan_payload,
                },
            },
        )
        body_to = json.loads(resp_to["result"]["content"][0]["text"])
        assert body_to["ok"] is False
        assert body_to["status"] == "timeout"
        assert killed[0] is True
        assert waited[0] is True
        assert len(observed_plan_path) == 2
        assert not observed_plan_path[1].exists()
        assert list(cache_dir.glob("last30days-plan-*.json")) == []

    @pytest.mark.asyncio
    async def test_ptc_wrapper_default_params_ignored_and_route_timeouts_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "ok": True,
                "posts": [
                    {
                        "id": "101",
                        "url": "https://xueqiu.com/100/101",
                        "created_at": "2026-10-22 14:30",
                        "created_at_ms": 1761114600000,
                        "author": "value_investor",
                        "title": "600519 analysis",
                    }
                ],
                "reviews": [
                    {
                        "id": "r1",
                        "source": "blind",
                        "url": "https://www.teamblind.com/company/NVIDIA/reviews",
                        "created_at": "2026-10-21",
                        "jobgroup": "Engineering",
                        "summary": "Solid team",
                    }
                ],
                "results": [
                    {
                        "candidate_id": "c1",
                        "source": "stocktwits",
                        "url": "https://stocktwits.com/symbol/NVDA/1",
                        "published_at": "2026-10-22",
                        "author": "trader1",
                        "title": "Bullish",
                    }
                ],
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        upstream_defaults = {
            "mode": "fetch",
            "timeout": 180,
        }

        # Verify timeout is determined solely by executor route config, ignoring timeout=180
        assert (
            bridge.compute_timeout(
                "community", {"route": "xueqiu", **upstream_defaults}
            )
            == 60
        )
        assert (
            bridge.compute_timeout(
                "community", {"route": "employee", **upstream_defaults}
            )
            == 120
        )
        assert (
            bridge.compute_timeout(
                "community", {"route": "last30days", **upstream_defaults}
            )
            == 600
        )

        # All 3 routes accept the full wrapper argument payload
        for req_id, route_args in (
            (
                501,
                {
                    "route": "xueqiu",
                    "operation": "posts",
                    "ticker": "06181",
                    "pages": 1,
                    **upstream_defaults,
                },
            ),
            (
                502,
                {
                    "route": "employee",
                    "operation": "blind",
                    "company": "NVIDIA",
                    "pages": 1,
                    **upstream_defaults,
                },
            ),
            (
                503,
                {
                    "route": "last30days",
                    "operation": "search",
                    "query": "NVDA Blackwell",
                    "days": 30,
                    "quick": True,
                    **upstream_defaults,
                },
            ),
        ):
            resp = await bridge.handle_tools_call(
                req_id, {"name": "community", "arguments": route_args}
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is True, f"Failed for {route_args}: {body}"
            assert body["status"] == "ok"

        assert exec_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_redaction_of_json_dict_keys_and_sanitized_error_data_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_log = (
            '{"token": "secretJsonToken12345", "xq_a_token": "xqJsonSecret999"} '
            "{'cookie': 'xq_a_token=dictCookieVal888; u=123'} "
            "xq_a_token=inlineXqToken777; Authorization: Bearer bearerSecret666 "
            "api_key='singleQuoteKey555'"
        )
        cleaned = rb.sanitize_stderr_text(raw_log, max_len=500)
        for leaked in (
            "secretJsonToken12345",
            "xqJsonSecret999",
            "dictCookieVal888",
            "inlineXqToken777",
            "bearerSecret666",
            "singleQuoteKey555",
        ):
            assert leaked not in cleaned
        assert "[REDACTED]" in cleaned

        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            dirty_json = {
                "ok": False,
                "error": 'Auth failed with {"token": "leaked_err_token_123"} and xq_a_token=leaked_xq_456',
                "cookie": "xq_a_token=raw_uncleaned_cookie_value",
                "raw_headers": {"Authorization": "Bearer raw_uncleaned_bearer_token"},
            }
            proc.communicate.return_value = (
                json.dumps(dirty_json).encode("utf-8"),
                b"'cookie': 'stderr_cookie_secret_999'",
            )
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            510,
            {
                "name": "community",
                "arguments": {"route": "xueqiu", "operation": "posts", "ticker": "00700"},
            },
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "script_error"
        serialized = json.dumps(body)
        for secret in (
            "leaked_err_token_123",
            "leaked_xq_456",
            "raw_uncleaned_cookie_value",
            "raw_uncleaned_bearer_token",
            "stderr_cookie_secret_999",
        ):
            assert secret not in serialized
        assert "cookie" not in body["data"]
        assert "raw_headers" not in body["data"]
        assert body["data"]["status"] == "script_error"
        assert body["data"]["route"] == "xueqiu"

    @pytest.mark.asyncio
    async def test_sigterm_process_group_then_sigkill_order_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        monkeypatch.setattr(rb, "COMMUNITY_SIGTERM_GRACE_S", 0.02)
        monkeypatch.setattr(bridge, "compute_timeout", lambda *_a, **_kw: 1)

        events: list[str] = []
        killpg_mock = create_autospec(rb.os.killpg)

        def _record_killpg(pgid: int, sig: int) -> None:
            if sig == rb.signal.SIGTERM:
                events.append(f"killpg:SIGTERM:{pgid}")
            elif sig == rb.signal.SIGKILL:
                events.append(f"killpg:SIGKILL:{pgid}")
            else:
                events.append(f"killpg:{sig}:{pgid}")

        killpg_mock.side_effect = _record_killpg
        monkeypatch.setattr(rb.os, "killpg", killpg_mock)

        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            assert kwargs.get("start_new_session") is True
            events.append("spawn:start_new_session=True")
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.pid = 43210
            proc.returncode = None

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.sleep(30.0)
                return b"", b""

            wait_calls = [0]

            async def _wait() -> int:
                wait_calls[0] += 1
                if wait_calls[0] == 1:
                    events.append("wait:grace_after_sigterm")
                    await asyncio.sleep(10.0)
                events.append("wait:reap_after_sigkill")
                proc.returncode = -9
                return -9

            proc.communicate.side_effect = _communicate
            proc.wait.side_effect = _wait
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            520,
            {
                "name": "community",
                "arguments": {"route": "last30days", "query": "NVDA"},
            },
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "timeout"
        assert events == [
            "spawn:start_new_session=True",
            "killpg:SIGTERM:43210",
            "wait:grace_after_sigterm",
            "killpg:SIGKILL:43210",
            "wait:reap_after_sigkill",
        ]

    @pytest.mark.asyncio
    async def test_employee_invokes_venv_python_directly_and_missing_files_return_source_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        observed_cmds: list[list[str]] = []
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            observed_cmds.append([str(c) for c in cmd])
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "ok": True,
                "source": "blind",
                "company": "NVIDIA",
                "count": 1,
                "reviews": [
                    {
                        "id": "b1",
                        "source": "blind",
                        "url": "https://www.teamblind.com/company/NVIDIA/reviews",
                        "created_at": "2026-10-20",
                        "jobgroup": "SWE",
                        "summary": "Great",
                    }
                ],
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp_ok = await bridge.handle_tools_call(
            530,
            {
                "name": "community",
                "arguments": {"route": "employee", "operation": "blind", "company": "NVIDIA"},
            },
        )
        body_ok = json.loads(resp_ok["result"]["content"][0]["text"])
        assert body_ok["ok"] is True
        assert len(observed_cmds) == 1
        assert observed_cmds[0][0] == str(bridge.cfg.employee_python)
        assert observed_cmds[0][1] == str(bridge.cfg.employee_cli_script)
        assert "bash" not in observed_cmds[0]
        assert not observed_cmds[0][1].endswith("run.sh")

        # When interpreter or CLI file is missing -> source_missing, no subprocess spawned
        bridge.cfg.employee_python = tmp_path / "nonexistent_venv_python"
        resp_missing = await bridge.handle_tools_call(
            531,
            {
                "name": "community",
                "arguments": {"route": "employee", "operation": "blind", "company": "NVIDIA"},
            },
        )
        body_missing = json.loads(resp_missing["result"]["content"][0]["text"])
        assert body_missing["ok"] is False
        assert body_missing["status"] == "source_missing"
        assert body_missing["error_code"] == "source_missing"
        assert len(observed_cmds) == 1  # No additional subprocess spawned

    @pytest.mark.asyncio
    async def test_temp_dir_rejects_any_path_inside_real_openclaw_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_ocw_tree = tmp_path / ".openclaw"
        fake_workspace = fake_ocw_tree / "workspace"
        fake_workspace.mkdir(parents=True)
        outside_workspace_in_ocw = fake_ocw_tree / "other-subdir" / "cache"
        monkeypatch.setenv("DALTON_OCW_ROOT", str(fake_workspace))
        monkeypatch.setenv("DALTON_BRIDGE_CACHE_DIR", str(outside_workspace_in_ocw))
        bridge = _make_bridge(tmp_path, monkeypatch)

        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            540,
            {
                "name": "community",
                "arguments": {
                    "route": "last30days",
                    "query": "NVDA",
                    "plan": {
                        "subqueries": [
                            {"search_query": "NVDA", "ranking_query": "NVDA sentiment"}
                        ]
                    },
                },
            },
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "invalid_arguments"
        assert "~/.openclaw" in body["error"]
        assert exec_mock.await_count == 0

    def test_subprocess_env_proxy_and_claudecode_stripping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "12345")
        monkeypatch.setenv("DALTON_BRIDGE_SUBPROCESS_PROXY", "http://127.0.0.1:7890")

        bridge = _make_bridge(tmp_path, monkeypatch)
        env = bridge._build_community_subprocess_env()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert "CLAUDE_CODE_SSE_PORT" not in env
        for pk in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            assert env[pk] == "http://127.0.0.1:7890"

        # When DALTON_BRIDGE_SUBPROCESS_PROXY is not configured, do not inject proxy vars
        monkeypatch.delenv("DALTON_BRIDGE_SUBPROCESS_PROXY", raising=False)
        bridge_no_proxy = _make_bridge(tmp_path, monkeypatch)
        env_no_proxy = bridge_no_proxy._build_community_subprocess_env()
        for pk in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            assert pk not in env_no_proxy

    @pytest.mark.asyncio
    async def test_last30days_degraded_vs_empty_vs_partial_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        payload_queue: list[dict[str, Any]] = []

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = payload_queue.pop(0)
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # 1. Degraded: failure states in source_status and 0 items -> ok=False, status="degraded"
        payload_queue.append(
            {
                "query": "NVDA",
                "results": [],
                "source_status": {
                    "x": {"state": "auth-failed", "reason": "token expired"},
                    "stocktwits": {"state": "timeout", "reason": "timed out"},
                    "polymarket": "no-results",
                },
            }
        )
        resp_deg = await bridge.handle_tools_call(
            550, {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}}
        )
        body_deg = json.loads(resp_deg["result"]["content"][0]["text"])
        assert body_deg["ok"] is False
        assert body_deg["status"] == "degraded"
        assert "source_status" in body_deg
        assert body_deg["source_status"]["x"]["state"] == "auth-failed"
        assert body_deg["source_status"]["stocktwits"]["state"] == "timeout"

        # 2. Empty: all sources are no-results and 0 items -> ok=True, status="empty", empty_reason="no_matching_items"
        payload_queue.append(
            {
                "query": "ObscureTopicXYZ",
                "results": [],
                "source_status": {
                    "x": {"state": "no-results"},
                    "stocktwits": {"state": "no-results"},
                    "polymarket": {"state": "no-results"},
                },
            }
        )
        resp_emp = await bridge.handle_tools_call(
            551,
            {
                "name": "community",
                "arguments": {"route": "last30days", "query": "ObscureTopicXYZ"},
            },
        )
        body_emp = json.loads(resp_emp["result"]["content"][0]["text"])
        assert body_emp["ok"] is True
        assert body_emp["status"] == "empty"
        assert body_emp["empty_reason"] == "no_matching_items"
        assert resp_emp["result"]["isError"] is False

        # 3. Partial items + failed sources -> ok=True, status="ok", failed sources in warnings
        payload_queue.append(
            {
                "query": "NVDA",
                "results": [
                    {
                        "candidate_id": "c1",
                        "source": "stocktwits",
                        "url": "https://stocktwits.com/symbol/NVDA/1",
                        "published_at": "2026-10-22",
                        "author": "bull_trader",
                        "title": "NVDA strong",
                    }
                ],
                "source_status": {
                    "stocktwits": {"state": "ok", "count": 1},
                    "x": {"state": "rate-limited"},
                    "polymarket": {"state": "unreachable"},
                },
            }
        )
        resp_part = await bridge.handle_tools_call(
            552, {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}}
        )
        body_part = json.loads(resp_part["result"]["content"][0]["text"])
        assert body_part["ok"] is True
        assert body_part["status"] == "ok"
        warnings_joined = " ".join(body_part.get("warnings") or [])
        assert "x" in warnings_joined and "rate-limited" in warnings_joined
        assert "polymarket" in warnings_joined and "unreachable" in warnings_joined

    @pytest.mark.asyncio
    async def test_queue_wait_counts_toward_deadline_and_skips_spawn_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        monkeypatch.setattr(bridge, "compute_timeout", lambda *_a, **_kw: 1)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # Hold the single last30days semaphore slot so the queued request times out before spawning
        sem = bridge.community_sems["last30days"]
        await sem.acquire()
        try:
            # Make compute_timeout return a tiny budget via _handle_community_call
            resp = await bridge._handle_community_call(
                560,
                "last30days",
                {"route": "last30days", "query": "NVDA"},
                eff_timeout=0,
            )
        finally:
            sem.release()

        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "timeout"
        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_catalog_category_filter_and_xueqiu_beijing_timezone_and_blind_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(
            reqs: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            cat_payload = {
                "ok": True,
                "status": "ok",
                "routes": [
                    {"id": "sec", "kind": "cli", "category": "finance", "operations": ["list-filings"]},
                ],
            }
            return [_mcp_tool_response(req["id"], cat_payload)], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. catalog(category="finance") must NOT append community routes
        resp_fin = await bridge.handle_tools_call(
            570, {"name": "catalog", "arguments": {"category": "finance"}}
        )
        body_fin = json.loads(resp_fin["result"]["content"][0]["text"])
        fin_ids = {r["id"] for r in body_fin["routes"]}
        assert fin_ids == {"sec"}

        # 2. catalog(category="community") appends the 3 community routes
        resp_comm = await bridge.handle_tools_call(
            571, {"name": "catalog", "arguments": {"category": "community"}}
        )
        body_comm = json.loads(resp_comm["result"]["content"][0]["text"])
        comm_ids = {r["id"] for r in body_comm["routes"]}
        assert {"xueqiu", "employee", "last30days"} <= comm_ids

        # 3. Xueqiu _sources date converts created_at_ms and YYYY-MM-DD HH:MM to +08:00 ISO
        xq_decl = source_identity.declare(
            "community",
            {"route": "xueqiu", "operation": "posts", "ticker": "06181"},
            {
                "ok": True,
                "status": "ok",
                "data": [
                    {
                        "id": "9001",
                        "url": "https://xueqiu.com/111/9001",
                        "created_at": "2026-10-22 14:30",
                        "created_at_ms": 1761114600000,
                        "author": "kol_a",
                        "title": "Post with ms",
                    },
                    {
                        "id": "9002",
                        "url": "https://xueqiu.com/111/9002",
                        "created_at": "2026-10-22 09:15",
                        "author": "kol_b",
                        "title": "Post without ms",
                    },
                ],
            },
        )
        assert len(xq_decl) == 2
        assert xq_decl[0]["date"].endswith("+08:00")
        assert "T" in xq_decl[0]["date"]
        assert xq_decl[1]["date"] == "2026-10-22T09:15:00+08:00"

        # 4. Blind evidence rule in evidence-and-sources.md
        ev_md = (
            REPO_ROOT
            / "plugins"
            / "dalton"
            / "skills"
            / "dalton-conventions"
            / "references"
            / "evidence-and-sources.md"
        ).read_text(encoding="utf-8")
        assert "仅可用于统计评分等元数据" in ev_md
        assert "不能**用于做正文词频统计" in ev_md

    @pytest.mark.asyncio
    async def test_last30days_maintenance_commands_and_dash_prefix_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        forbidden_queries = [
            "setup",
            "SETUP",
            "  setup  ",
            "self-test",
            "doctor",
            "diagnose",
            "preflight",
            "welcome",
            "install",
            "upgrade",
            "library",
            "library feed",
            "library search",
            "library search NVDA",
            "queue",
            "queue list",
            "queue cover",
            "queue cover 12",
            "-h",
            "--help",
            "--diagnose",
            "--welcome",
            "-setup",
        ]
        for idx, q in enumerate(forbidden_queries):
            resp = await bridge.handle_tools_call(
                600 + idx,
                {
                    "name": "community",
                    "arguments": {"route": "last30days", "query": q},
                },
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Expected rejection for query={q!r}"
            assert body["status"] == "invalid_arguments"
            assert body["error_code"] == "invalid_arguments"

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_partial_success_sanitizes_source_status_and_errors_by_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "schema_version": "1.4",
                "query": "NVDA",
                "results": [
                    {
                        "candidate_id": "c1",
                        "source": "stocktwits",
                        "url": "https://stocktwits.com/symbol/NVDA/1",
                        "published_at": "2026-10-22",
                        "author": "bull_1",
                        "title": "NVDA demand solid",
                    }
                ],
                "source_status": {
                    "stocktwits": {"state": "ok", "count": 1},
                    "x": {
                        "state": "error",
                        "error": "Auth failed with token=secretSourceStatusToken111",
                        "detail": "Authorization: Bearer secretBearerInDetail222",
                        "api_key": "rawApiKeyField333",
                    },
                },
                "errors_by_source": {
                    "x": "Request failed: token=secretErrorsBySourceToken444; cookie=xq_a_token=secretCookie555",
                    "youtube": {"reason": "api_key='secretYoutubeKey666'"},
                },
                "x_error": "x_degraded: token=secretTopLevelXError777",
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            620,
            {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}},
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["status"] == "ok"
        serialized = json.dumps(body)
        for secret in (
            "secretSourceStatusToken111",
            "secretBearerInDetail222",
            "rawApiKeyField333",
            "secretErrorsBySourceToken444",
            "secretCookie555",
            "secretYoutubeKey666",
            "secretTopLevelXError777",
        ):
            assert secret not in serialized
        assert "[REDACTED]" in json.dumps(body["data"]["source_status"])
        assert "[REDACTED]" in json.dumps(body["data"]["errors_by_source"])
        assert "[REDACTED]" in body["data"]["x_error"]

    @pytest.mark.asyncio
    async def test_url_validation_rejects_backslashes_and_forwards_canonical_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        observed_cmds: list[list[str]] = []
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            observed_cmds.append([str(c) for c in cmd])
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "ok": True,
                "reviews": [
                    {
                        "id": "gd1",
                        "source": "glassdoor",
                        "url": "https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm",
                        "created_at": "2026-10-20",
                        "jobgroup": "Engineer",
                        "summary": "Good",
                    }
                ],
                "id": "99",
                "url": "https://xueqiu.com/12345/67890",
                "created_at": "2026-10-22 10:00",
                "author": "user1",
                "title": "Title",
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        malicious_urls = [
            (
                "employee",
                "glassdoor",
                r"https://attacker.example\.glassdoor.com/Reviews",
            ),
            (
                "xueqiu",
                "post",
                r"https://attacker.example\.xueqiu.com/123/456",
            ),
            (
                "employee",
                "glassdoor",
                "https://glassdoor.com@attacker.example/Reviews",
            ),
            (
                "employee",
                "glassdoor",
                "https://%67lassdoor.com/Reviews",
            ),
            (
                "xueqiu",
                "post",
                "https://www.%78ueqiu.com/123/456",
            ),
            (
                "employee",
                "glassdoor",
                "https://www.glassdoor.com/Reviews\nInjected",
            ),
        ]
        for idx, (route, op, bad_url) in enumerate(malicious_urls):
            args: dict[str, Any] = {
                "route": route,
                "operation": op,
                "url": bad_url,
            }
            if route == "employee":
                args["allow_paid"] = True
            resp = await bridge.handle_tools_call(
                630 + idx, {"name": "community", "arguments": args}
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Expected failure for URL {bad_url!r}"
            assert body["status"] == "invalid_arguments"

        assert len(observed_cmds) == 0

        # Canonical URL must be passed to the script, not the raw input string
        raw_gd_url = "HTTPS://WWW.Glassdoor.COM./Reviews/NVIDIA-Reviews-E7633.htm?sort=date"
        resp_ok = await bridge.handle_tools_call(
            640,
            {
                "name": "community",
                "arguments": {
                    "route": "employee",
                    "operation": "glassdoor",
                    "url": raw_gd_url,
                    "allow_paid": True,
                },
            },
        )
        body_ok = json.loads(resp_ok["result"]["content"][0]["text"])
        assert body_ok["ok"] is True
        assert len(observed_cmds) == 1
        url_arg_idx = observed_cmds[0].index("--url") + 1
        assert (
            observed_cmds[0][url_arg_idx]
            == "https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm?sort=date"
        )

    @pytest.mark.asyncio
    async def test_last30days_invokes_engine_venv_python_directly_with_env_and_source_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCLUDE_SOURCES", "hn")
        bridge = _make_bridge(tmp_path, monkeypatch)
        observed_cmds: list[list[str]] = []
        observed_envs: list[dict[str, str]] = []
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            observed_cmds.append([str(c) for c in cmd])
            observed_envs.append(dict(kwargs.get("env") or {}))
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "query": "NVDA",
                "results": [
                    {
                        "candidate_id": "c1",
                        "source": "stocktwits",
                        "url": "https://stocktwits.com/symbol/NVDA/1",
                        "published_at": "2026-10-22",
                        "author": "u1",
                        "title": "NVDA",
                    }
                ],
                "source_status": {"stocktwits": {"state": "ok", "count": 1}},
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp_ok = await bridge.handle_tools_call(
            650,
            {
                "name": "community",
                "arguments": {"route": "last30days", "query": "NVDA", "days": 14, "quick": True},
            },
        )
        body_ok = json.loads(resp_ok["result"]["content"][0]["text"])
        assert body_ok["ok"] is True
        assert len(observed_cmds) == 1
        assert observed_cmds[0] == [
            str(bridge.cfg.last30days_python),
            str(bridge.cfg.last30days_script),
            "NVDA",
            "--days",
            "14",
            "--quick",
            "--save-dir",
            str(bridge.cfg.cache_dir / "last30days-memory"),
            "--emit=json",
        ]
        assert "bash" not in observed_cmds[0]
        # Never the engine default ~/Documents/Last30Days (TCC-protected under launchd).
        assert "Documents" not in " ".join(observed_cmds[0])
        assert observed_envs[0]["FROM_BROWSER"] == "off"
        assert observed_envs[0]["LAST30DAYS_ALLOW_ENGINE_PLAN"] == "1"
        assert observed_envs[0]["EXCLUDE_SOURCES"] == "hn,reddit"

        # Missing python interpreter -> source_missing
        bridge.cfg.last30days_python = tmp_path / "missing_l30_python"
        resp_missing_py = await bridge.handle_tools_call(
            651,
            {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}},
        )
        body_missing_py = json.loads(resp_missing_py["result"]["content"][0]["text"])
        assert body_missing_py["ok"] is False
        assert body_missing_py["status"] == "source_missing"
        assert len(observed_cmds) == 1

    @pytest.mark.asyncio
    async def test_last30days_strict_empty_vs_degraded_and_partial_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        payload_queue: list[dict[str, Any]] = []

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = payload_queue.pop(0)
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # 1. Non-no-results states with 0 items (payment-required, missing, broken, skipped-unconfigured, schema-drift, or empty source_status) -> degraded
        for idx, src_status in enumerate(
            [
                {"x": {"state": "payment-required"}, "stocktwits": {"state": "no-results"}},
                {"x": {"state": "missing"}},
                {"x": {"state": "broken"}},
                {"x": {"state": "skipped-unconfigured"}},
                {"x": {"state": "schema-drift"}},
                {},
            ]
        ):
            payload_queue.append(
                {"query": "NVDA", "results": [], "source_status": src_status}
            )
            resp = await bridge.handle_tools_call(
                660 + idx,
                {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}},
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Expected degraded for {src_status}"
            assert body["status"] == "degraded"
            assert "source_status" in body

        # 2. Has items + partial / schema-drift / payment-required -> ok=True, all non-(ok/no-results) in warnings
        payload_queue.append(
            {
                "query": "NVDA",
                "results": [
                    {
                        "candidate_id": "c1",
                        "source": "stocktwits",
                        "url": "https://stocktwits.com/symbol/NVDA/1",
                        "published_at": "2026-10-22",
                        "author": "u1",
                        "title": "NVDA",
                    }
                ],
                "source_status": {
                    "stocktwits": {"state": "ok", "count": 1},
                    "polymarket": {"state": "no-results"},
                    "x": {"state": "partial"},
                    "youtube": {"state": "schema-drift"},
                },
            }
        )
        resp_warn = await bridge.handle_tools_call(
            670,
            {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}},
        )
        body_warn = json.loads(resp_warn["result"]["content"][0]["text"])
        assert body_warn["ok"] is True
        assert body_warn["status"] == "ok"
        warnings_text = " ".join(body_warn.get("warnings") or [])
        assert "x" in warnings_text and "partial" in warnings_text
        assert "youtube" in warnings_text and "schema-drift" in warnings_text
        assert "polymarket" not in warnings_text

    @pytest.mark.asyncio
    async def test_process_group_reap_when_leader_exits_early_but_child_remains_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        monkeypatch.setattr(rb, "COMMUNITY_SIGTERM_GRACE_S", 0.03)
        monkeypatch.setattr(bridge, "compute_timeout", lambda *_a, **_kw: 1)

        events: list[str] = []
        child_alive = [True]
        killpg_mock = create_autospec(rb.os.killpg)

        def _fake_killpg(pgid: int, sig: int) -> None:
            if not child_alive[0]:
                raise ProcessLookupError(3, "No such process")
            if sig == rb.signal.SIGTERM:
                events.append(f"killpg:SIGTERM:{pgid}")
            elif sig == 0:
                if f"killpg:0:{pgid}" not in events:
                    events.append(f"killpg:0:{pgid}")
            elif sig == rb.signal.SIGKILL:
                events.append(f"killpg:SIGKILL:{pgid}")
                child_alive[0] = False

        killpg_mock.side_effect = _fake_killpg
        monkeypatch.setattr(rb.os, "killpg", killpg_mock)

        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.pid = 54321
            # Simulate group leader already exited (returncode=0), while child in process group holds pipe open
            proc.returncode = 0

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.sleep(30.0)
                return b"", b""

            async def _wait() -> int:
                events.append("wait:leader_reaped")
                return 0

            proc.communicate.side_effect = _communicate
            proc.wait.side_effect = _wait
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            680,
            {"name": "community", "arguments": {"route": "last30days", "query": "NVDA"}},
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "timeout"
        assert events == [
            "killpg:SIGTERM:54321",
            "killpg:0:54321",
            "killpg:SIGKILL:54321",
            "wait:leader_reaped",
        ]






def test_secret_key_regex_keeps_author_fields_and_token_counts() -> None:
    """Author attribution and token-count fields are evidence, not secrets."""
    import researchdata_bridge as rb

    for kept in ("author", "author_id", "totalTokens", "promptTokens"):
        assert not rb._SECRET_KEY_NAME_RE.search(kept), kept
    for secret in ("auth", "auth_token", "Authorization", "api_key", "xq_a_token", "token", "cookie"):
        assert rb._SECRET_KEY_NAME_RE.search(secret), secret
    payload = {"author": "西门居士", "author_id": 7295972385, "token": "abc123secret"}
    out = rb._sanitize_community_payload_metadata(dict(payload))
    assert out["author"] == "西门居士" and out["author_id"] == 7295972385
    assert out["token"] == "[REDACTED]"


def test_community_url_rejects_surrounding_whitespace() -> None:
    import pytest
    import researchdata_bridge as rb

    for bad in (" https://xueqiu.com/1/2", "https://xueqiu.com/1/2\n"):
        with pytest.raises(ValueError):
            rb._validate_community_url(bad, required_domain="xueqiu.com")


class TestBridgeResearchParamsAndTranscriptGuards:
    @pytest.mark.asyncio
    async def test_max_chars_and_offset_clamping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"]["arguments"]
            forwarded_args.append(args)
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "document": {"document_id": "tmtb:12345", "content": "text"},
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        resp = await bridge.handle_tools_call(
            "clamp-1",
            {
                "name": "research",
                "arguments": {
                    "route": "tmtb",
                    "operation": "read",
                    "document": "tmtb:12345",
                    "params": {"max_chars": 50000, "offset": -10},
                },
            },
        )
        assert len(forwarded_args) == 1
        assert forwarded_args[0]["params"]["max_chars"] == 30000
        assert forwarded_args[0]["params"]["offset"] == 0

        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["params_clamped"] == {"max_chars": [50000, 30000], "offset": [-10, 0]}

        # String integer values and max_chars < 1 are also clamped and recorded
        resp_str = await bridge.handle_tools_call(
            "clamp-2",
            {
                "name": "research",
                "arguments": {
                    "route": "tmtb",
                    "operation": "read",
                    "document": "tmtb:12345",
                    "params": {"max_chars": "0", "offset": "-5"},
                },
            },
        )
        assert forwarded_args[-1]["params"]["max_chars"] == 1
        assert forwarded_args[-1]["params"]["offset"] == 0
        body_str = json.loads(resp_str["result"]["content"][0]["text"])
        assert body_str["params_clamped"] == {"max_chars": ["0", 1], "offset": ["-5", 0]}

    @pytest.mark.asyncio
    async def test_numeric_alphaengine_doc_id_on_internal_library_returns_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        resp = await bridge.handle_tools_call(
            "ae-id-1",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "operation": "read",
                    "document": "190000041634994",
                },
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["document_id"] == "190000041634994"
        expected_hint = (
            '这是 AlphaEngine 文档，用 research(route="alphaengine", '
            'operation="get_document", params={"doc_id": "190000041634994"}) 读取'
        )
        assert expected_hint in body["error"]
        assert expected_hint in body["hint"]

    @pytest.mark.asyncio
    async def test_internal_library_maps_params_document_to_top_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"]["arguments"]
            forwarded_args.append(args)
            if not args.get("operation"):
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": False,
                            "status": "error",
                            "error": "未知内部资料参数: document",
                        },
                        is_error=True,
                    )
                ], 0
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "document": {"document_id": "mail:888", "content": "hello"},
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # Explicit operation="read" maps params.document to top-level document
        resp = await bridge.handle_tools_call(
            "map-doc-1",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "operation": "read",
                    "params": {"document": "mail:888", "max_chars": 60000},
                },
            },
        )
        assert len(forwarded_args) == 1
        assert forwarded_args[0]["document"] == "mail:888"
        assert "document" not in forwarded_args[0]["params"]
        assert forwarded_args[0]["params"]["max_chars"] == 30000
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["params_clamped"] == {"max_chars": [60000, 30000]}
        assert "顶层参数" in body["hint"]

        # route="all" with an explicit read is a legal library read and maps the same way
        await bridge.handle_tools_call(
            "map-doc-all",
            {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "read",
                    "params": {"document": "mail:888"},
                },
            },
        )
        assert forwarded_args[-1]["document"] == "mail:888"
        assert "document" not in forwarded_args[-1]["params"]

        # Empty operation keeps call_args unchanged and attaches guidance hint in the result
        resp_empty_op = await bridge.handle_tools_call(
            "map-doc-empty-op",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "params": {"document": "mail:888"},
                },
            },
        )
        assert "document" not in forwarded_args[-1]
        assert forwarded_args[-1]["params"]["document"] == "mail:888"
        assert not forwarded_args[-1].get("operation")
        body_empty_op = json.loads(resp_empty_op["result"]["content"][0]["text"])
        assert 'operation="read"' in body_empty_op["hint"]
        assert 'document="mail:888"' in body_empty_op["hint"]

    @pytest.mark.asyncio
    async def test_transcript_drops_topic_query_and_no_match_returns_empty_reason_without_roic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            name = req["params"]["name"]
            args = req["params"].get("arguments") or {}
            if name == "catalog":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "routes": [
                                {
                                    "id": "transcript",
                                    "category": "research",
                                    "rules": ["默认近 400 天"],
                                },
                                {
                                    "id": "company-wiki",
                                    "category": "research",
                                    "rules": ["只读本地 wiki"],
                                },
                            ],
                        },
                    )
                ], 0
            forwarded_args.append(args)
            if args.get("ticker") == "GOOGL":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "data": {"ok": True, "source": "alphaengine", "transcripts": []},
                        },
                    )
                ], 0
            if args.get("ticker") == "AE_NON_ROIC_ERR":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": False,
                            "status": "error",
                            "source": "alphaengine",
                            "error": "upstream timeout discussing roic metric 403bps",
                            "hint": "keep original hint",
                        },
                    )
                ], 0
            # Simulate AlphaEngine no-match (even if legacy upstream payload still had fallback metadata)
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": False,
                        "status": "no_match",
                        "data": {"ok": False, "transcripts": []},
                        "fallback": {"source": "roic", "reason": "alphaengine_no_match"},
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. Topic query with ticker is dropped before forwarding
        resp_topic = await bridge.handle_tools_call(
            "tr-1",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "GOOGL",
                    "query": "Q2 2026 earnings call capital expenditures",
                },
            },
        )
        assert "query" not in forwarded_args[-1]
        body_topic = json.loads(resp_topic["result"]["content"][0]["text"])
        assert body_topic["ok"] is True
        assert body_topic["empty_reason"] == "no_matching_transcripts"
        assert body_topic["query_ignored"] == "Q2 2026 earnings call capital expenditures"
        assert "transcript 的 query 只用来传公司名，主题检索请在拿到 doc_id 后读正文" in body_topic["note"]

        # Multi-word company names without topic signals are ALL preserved
        kept_company_queries = [
            "Alphabet|谷歌|Google",
            "Alphabet Inc",
            "Meta Platforms",
            "Berkshire Hathaway",
            "Ping An",
            "Taiwan Semiconductor Manufacturing",
            "腾讯 控股",
        ]
        for idx, company_q in enumerate(kept_company_queries):
            await bridge.handle_tools_call(
                f"tr-keep-{idx}",
                {
                    "name": "research",
                    "arguments": {
                        "route": "transcript",
                        "ticker": "GOOGL",
                        "query": company_q,
                    },
                },
            )
            assert forwarded_args[-1].get("query") == company_q

        # 2. No-match on transcript returns ok=True with empty_reason and AlphaEngine-only hint (no roic mention)
        resp_no_match = await bridge.handle_tools_call(
            "tr-3",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "UNKNOWN",
                },
            },
        )
        body_no_match = json.loads(resp_no_match["result"]["content"][0]["text"])
        assert body_no_match["ok"] is True
        assert body_no_match["empty_reason"] == "no_matching_transcripts"
        assert "fallback" not in body_no_match
        assert "roic" not in json.dumps(body_no_match, ensure_ascii=False).lower()
        assert "|" in body_no_match["hint"]
        assert "start/end" in body_no_match["hint"]

        resp_non_roic = await bridge.handle_tools_call(
            "tr-non-roic",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "AE_NON_ROIC_ERR",
                },
            },
        )
        body_non_roic = json.loads(resp_non_roic["result"]["content"][0]["text"])
        assert body_non_roic["ok"] is False
        assert body_non_roic["error_category"] == "上游不可用"
        assert "upstream timeout discussing roic metric 403bps" in body_non_roic["error"]
        assert body_non_roic["hint"] == "keep original hint"

        # 3. catalog includes transcript and pagination rules (and no roic mention)
        resp_cat = await bridge.handle_tools_call(
            "cat-1", {"name": "catalog", "arguments": {}}
        )
        cat_body = json.loads(resp_cat["result"]["content"][0]["text"])
        tr_route = next(r for r in cat_body["routes"] if r["id"] == "transcript")
        wiki_route = next(r for r in cat_body["routes"] if r["id"] == "company-wiki")
        assert any("只用来传公司名" in rl for rl in tr_route["rules"])
        assert not any("roic" in rl.lower() for rl in tr_route["rules"])
        assert any("1–30000" in rl for rl in wiki_route["rules"])

    @pytest.mark.asyncio
    async def test_parameter_alias_mapping_and_normalized_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"].get("arguments") or {}
            forwarded_args.append(args)
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "rows": [{"date": "2026-10-01", "form": "6-K"}],
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # symbol -> ticker (with 0700.HK -> 00700 normalization on cn-hk), from/to -> start/end, op -> operation
        resp = await bridge.handle_tools_call(
            "alias-1",
            {
                "name": "finance",
                "arguments": {
                    "source": "cn-hk",
                    "op": "list_filings",
                    "symbol": "0700.HK",
                    "from": "2026-01-01",
                    "to": "2026-10-23",
                    "max_results": 5,
                },
            },
        )
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert forwarded_args[-1]["route"] == "cn-hk"
        assert forwarded_args[-1]["operation"] == "filings"
        assert forwarded_args[-1]["ticker"] == "00700"
        assert forwarded_args[-1]["start"] == "2026-01-01"
        assert forwarded_args[-1]["end"] == "2026-10-23"
        assert forwarded_args[-1]["limit"] == 5
        assert body["normalized_params"] == {
            "route": "cn-hk",
            "operation": "filings",
            "ticker": "00700",
            "start": "2026-01-01",
            "end": "2026-10-23",
            "limit": 5,
        }

        # q/keyword -> query and start_date/end_date -> start/end on research
        resp_res = await bridge.handle_tools_call(
            "alias-2",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "action": "query",
                    "q": "META Reels",
                    "start_date": "2026-05-01",
                    "end_date": "2026-10-23",
                },
            },
        )
        body_res = json.loads(resp_res["result"]["content"][0]["text"])
        assert body_res["ok"] is True
        assert forwarded_args[-1]["operation"] == "search"
        assert forwarded_args[-1]["query"] == "META Reels"
        assert forwarded_args[-1]["start"] == "2026-05-01"
        assert forwarded_args[-1]["end"] == "2026-10-23"
        assert body_res["normalized_params"]["query"] == "META Reels"
        assert body_res["normalized_params"]["operation"] == "search"

    @pytest.mark.asyncio
    async def test_parameter_alias_conflict_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        conflict_cases = [
            (
                "finance",
                {"route": "sec", "operation": "financials", "ticker": "META", "symbol": "GOOGL"},
                "ticker",
            ),
            (
                "research",
                {
                    "route": "sales-notes",
                    "operation": "search",
                    "query": "META",
                    "start": "2026-01-01",
                    "from": "2026-06-01",
                },
                "start",
            ),
            (
                "research",
                {"route": "tmtb", "operation": "search", "query": "META", "keyword": "NVDA"},
                "query",
            ),
        ]
        for idx, (tool_name, args, canon_key) in enumerate(conflict_cases):
            resp = await bridge.handle_tools_call(
                f"conflict-{idx}",
                {"name": tool_name, "arguments": args},
            )
            assert resp["result"]["isError"] is True
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False
            assert body["error_code"] == "parameter_conflict"
            assert body["error_category"] == "参数错误"
            assert "参数错误" in body["error"]
            assert canon_key in body["error"]
            assert body["hint"]
            assert "normalized_params" in body

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_unknown_parameter_and_invalid_operation_return_error_with_accepted_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # 1. Unknown parameter on finance(route="sec")
        resp_unk = await bridge.handle_tools_call(
            "unk-1",
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "financials",
                    "ticker": "META",
                    "unknown_knob": "foo",
                },
            },
        )
        assert resp_unk["result"]["isError"] is True
        body_unk = json.loads(resp_unk["result"]["content"][0]["text"])
        assert body_unk["ok"] is False
        assert body_unk["error_code"] == "unknown_parameters"
        assert body_unk["error_category"] == "参数错误"
        assert "unknown_knob" in body_unk["error"]
        assert "ticker" in body_unk["accepted_parameters"]
        assert "form" in body_unk["accepted_parameters"]
        assert "catalog" in body_unk["hint"]

        # 2. Invalid/misspelled operation on research(route="alphaengine")
        resp_op = await bridge.handle_tools_call(
            "unk-op",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_libary_typo",
                    "query": "META",
                },
            },
        )
        assert resp_op["result"]["isError"] is True
        body_op = json.loads(resp_op["result"]["content"][0]["text"])
        assert body_op["ok"] is False
        assert body_op["error_code"] == "invalid_operation"
        assert body_op["error_category"] == "参数错误"
        assert "search_library" in body_op["accepted_operations"]
        assert "get_document" in body_op["accepted_operations"]
        assert "hint" in body_op and body_op["hint"]

        # 3. Unknown parameter on community(route="xueqiu")
        resp_comm = await bridge.handle_tools_call(
            "unk-comm",
            {
                "name": "community",
                "arguments": {
                    "route": "xueqiu",
                    "operation": "posts",
                    "ticker": "00700",
                    "bad_extra": 1,
                },
            },
        )
        body_comm = json.loads(resp_comm["result"]["content"][0]["text"])
        assert body_comm["ok"] is False
        assert body_comm["error_category"] == "参数错误"
        assert "Accepted parameters" in body_comm["error"]
        assert body_comm["hint"]

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_unknown_route_is_parameter_error_and_never_forwarded_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Upstream answers an unknown route with "route 不在当前分类的可选值中",
        # which used to be classified as 上游不可用.
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            "bad-route",
            {
                "name": "research",
                "arguments": {"route": "nonexistent-route", "query": "x"},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == "invalid_arguments"
        assert body["error_code"] == "invalid_route"
        assert body["error_category"] == "参数错误"
        assert "nonexistent-route" in body["error"]
        assert "catalog()" in body["hint"]
        assert "transcript" in body["accepted_routes"]
        assert "sec" not in body["accepted_routes"]
        assert "normalized_params" in body

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_route_under_wrong_tool_is_parameter_error_and_community_combo_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for call_id, args in (
            ("wrong-tool-no-ticker", {"route": "sec"}),
            ("wrong-tool-with-ticker", {"route": "sec", "ticker": "META"}),
        ):
            resp = await bridge.handle_tools_call(
                call_id,
                {"name": "research", "arguments": args},
            )
            assert resp["result"]["isError"] is True
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False
            assert body["status"] == "invalid_arguments"
            assert body["error_code"] == "invalid_route"
            assert body["error_category"] == "参数错误"
            assert "sec" in body["error"]
            assert "finance" in body["hint"]
            assert "finance(route='sec'" in body["hint"]
            assert "transcript" in body["accepted_routes"]
            assert "sec" not in body["accepted_routes"]

        # Existing community tool/route combination check stays unchanged for research(route="xueqiu")
        resp_comm = await bridge.handle_tools_call(
            "comm-combo",
            {"name": "research", "arguments": {"route": "xueqiu"}},
        )
        assert resp_comm["result"]["isError"] is True
        body_comm = json.loads(resp_comm["result"]["content"][0]["text"])
        assert body_comm["ok"] is False
        assert body_comm["status"] == "invalid_arguments"
        assert body_comm["error_code"] == "invalid_arguments"
        assert (
            "Invalid community tool/route combination: tool='research', route='xueqiu'."
            in body_comm["error"]
        )
        assert body_comm["data"] is None

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_ok_false_error_categories_with_hint_and_ok_true_empty_reason_and_catalog_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            name = req["params"]["name"]
            args = req["params"].get("arguments") or {}
            if name == "catalog":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "routes": [{"id": "sec", "category": "finance", "operations": ["financials"]}],
                        },
                    )
                ], 0
            if args.get("query") == "QUOTA_ERR":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": False,
                            "status": "quota_exceeded",
                            "error": "AlphaEngine daily quota exceeded (429)",
                        },
                        is_error=True,
                    )
                ], 0
            if args.get("query") == "UPSTREAM_ERR":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": False,
                            "status": "error",
                            "error": "Connection reset by upstream gateway",
                        },
                        is_error=True,
                    )
                ], 0
            # True no-match: 0 results
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "results": [],
                        "result_count": 0,
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. Quota / permission error -> error_category == "权限或额度" + actionable hint
        resp_quota = await bridge.handle_tools_call(
            "err-quota",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "QUOTA_ERR",
                },
            },
        )
        body_quota = json.loads(resp_quota["result"]["content"][0]["text"])
        assert body_quota["ok"] is False
        assert body_quota["error_category"] == "权限或额度"
        assert "[权限或额度]" in body_quota["error"]
        assert "禁止写成“没有相关信息”" in body_quota["hint"]
        assert body_quota["normalized_params"]["route"] == "alphaengine"

        # 2. Upstream failure -> error_category == "上游不可用" + actionable hint
        resp_up = await bridge.handle_tools_call(
            "err-up",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "operation": "search",
                    "query": "UPSTREAM_ERR",
                },
            },
        )
        body_up = json.loads(resp_up["result"]["content"][0]["text"])
        assert body_up["ok"] is False
        assert body_up["error_category"] == "上游不可用"
        assert "[上游不可用]" in body_up["error"]
        assert "不得当成“没有数据”" in body_up["hint"]

        # 3. True no-match -> ok=True + empty results + empty_reason
        resp_empty = await bridge.handle_tools_call(
            "ok-empty",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "operation": "search",
                    "query": "NONEXISTENT_TOPIC_XYZ",
                },
            },
        )
        body_empty = json.loads(resp_empty["result"]["content"][0]["text"])
        assert body_empty["ok"] is True
        assert body_empty["results"] == []
        assert body_empty["empty_reason"] == "no_matching_records"
        assert body_empty["hint"]

        # 4. catalog(route="cn-hk") returns the route's contract (required_params, param_formats, examples)
        resp_cat_route = await bridge.handle_tools_call(
            "cat-route",
            {"name": "catalog", "arguments": {"route": "cn-hk"}},
        )
        body_cat_route = json.loads(resp_cat_route["result"]["content"][0]["text"])
        assert body_cat_route["ok"] is True
        assert body_cat_route["route_count"] == 1
        cnhk_entry = body_cat_route["routes"][0]
        assert cnhk_entry["id"] == "cn-hk"
        assert "filings" in cnhk_entry["required_params"]
        assert "00700" in cnhk_entry["param_formats"]["ticker"]
        assert cnhk_entry["examples"]
        assert body_cat_route["normalized_params"] == {"route": "cn-hk"}

    @pytest.mark.asyncio
    async def test_empty_result_contract_strict_disqualifiers_and_never_deletes_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        payload_queue: list[tuple[dict[str, Any], bool]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            payload, is_err = payload_queue.pop(0)
            return [_mcp_tool_response(req["id"], payload, is_error=is_err)], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # Disqualifying payloads: must ALL stay ok=False, never delete error, and carry error_category + hint
        disqualifying_cases = [
            # 1. ok: False with empty results
            (
                "research",
                {"route": "sales-notes", "operation": "search", "query": "META"},
                {"ok": False, "status": "error", "results": [], "error": "Database query failed"},
                "上游不可用",
                "Database query failed",
            ),
            # 2. Top-level error even if ok was True and results was []
            (
                "research",
                {"route": "tmtb", "operation": "search", "query": "META"},
                {"ok": True, "status": "ok", "results": [], "error": "Index file not found on disk"},
                "上游不可用",
                "Index file not found on disk",
            ),
            # 3. Failed source in source_status
            (
                "research",
                {"route": "company-wiki", "operation": "search", "query": "META"},
                {
                    "ok": True,
                    "status": "ok",
                    "results": [],
                    "source_status": {"company-wiki": {"ok": False, "status": "failed", "error": "sqlite locked"}},
                },
                "上游不可用",
                "sqlite locked",
            ),
            # 4. 401/403/404/429/5xx HTTP errors and 'not found' error text (not business no-match)
            (
                "finance",
                {"route": "sec", "operation": "financials", "ticker": "META"},
                {"ok": False, "status": "error", "rows": [], "error": "HTTP 404 Not Found: SEC upstream endpoint not found"},
                "上游不可用",
                "404 Not Found",
            ),
            (
                "research",
                {"route": "alphaengine", "operation": "search_library", "query": "META"},
                {"ok": False, "status": "error", "results": [], "error": "HTTP 403 Forbidden"},
                "权限或额度",
                "403 Forbidden",
            ),
            # 5. Transcript with ONLY fallback.source == "roic" (without alphaengine_no_match)
            (
                "research",
                {"route": "transcript", "ticker": "META"},
                {"ok": False, "status": "error", "fallback": {"source": "roic"}},
                "上游不可用",
                "roic",
            ),
            # 6. Transcript with error text containing "not found" and transcripts=[]
            (
                "research",
                {"route": "transcript", "ticker": "META"},
                {
                    "ok": False,
                    "status": "error",
                    "error": "Transcript index file not found",
                    "data": {"transcripts": []},
                },
                "上游不可用",
                "Transcript index file not found",
            ),
            # 7. Transcript with ok=False, status="error", data={"transcripts": []} and NO business no-match reason
            (
                "research",
                {"route": "transcript", "ticker": "META"},
                {
                    "ok": False,
                    "status": "error",
                    "data": {"transcripts": []},
                },
                "上游不可用",
                "调用失败",
            ),
        ]

        for idx, (tool_name, args, upstream_payload, expected_cat, expected_err_substr) in enumerate(
            disqualifying_cases
        ):
            payload_queue.append((upstream_payload, True))
            resp = await bridge.handle_tools_call(
                f"disq-{idx}", {"name": tool_name, "arguments": args}
            )
            assert resp["result"]["isError"] is True, f"Case {idx} should have isError=True"
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Case {idx} must stay ok=False: {body}"
            assert "empty_reason" not in body, f"Case {idx} must not be marked empty_reason: {body}"
            assert body["error_category"] == expected_cat
            assert expected_err_substr in body["error"]
            assert body["hint"]

    @pytest.mark.asyncio
    async def test_unwrap_nested_mcp_and_alphaengine_errors_and_override_conflicting_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        payload_queue: list[tuple[dict[str, Any], bool]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            payload, is_err = payload_queue.pop(0)
            return [_mcp_tool_response(req["id"], payload, is_error=is_err)], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. Real data_gateway.py transcript error shape: data.alphaengine_error="403 Forbidden" + conflicting "无匹配" hint
        payload_queue.append(
            (
                {
                    "ok": False,
                    "status": "error",
                    "route": "transcript",
                    "operation": "list",
                    "data": {
                        "ok": False,
                        "ticker": "META",
                        "transcripts": [],
                        "reason": "alphaengine_error",
                        "alphaengine_error": "HTTP 403 Forbidden: AlphaEngine session expired",
                    },
                    "fallback": {
                        "source": "roic",
                        "reason": "alphaengine_error",
                        "ticker": "META",
                    },
                    "hint": "AlphaEngine 无匹配：尝试用公司名（query='腾讯|Tencent'）重新检索，或检查 start/end 窗口。",
                },
                True,
            )
        )
        resp_tr = await bridge.handle_tools_call(
            "unwrap-tr",
            {"name": "research", "arguments": {"route": "transcript", "ticker": "META"}},
        )
        body_tr = json.loads(resp_tr["result"]["content"][0]["text"])
        assert body_tr["ok"] is False
        assert body_tr["error_category"] == "权限或额度"
        assert "403 Forbidden" in body_tr["error"]
        assert "无匹配" not in body_tr["hint"]
        assert "禁止写成“没有相关信息”" in body_tr["hint"]

        # 2. Real data_gateway.py MCP proxy error shape: error inside data.content[0].text & data.structuredContent
        payload_queue.append(
            (
                {
                    "ok": False,
                    "status": "error",
                    "route": "alphaengine",
                    "operation": "search_library",
                    "data": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": "429 Too Many Requests: rate limit exceeded"}
                                ),
                            }
                        ],
                        "structuredContent": {
                            "error": "429 Too Many Requests: rate limit exceeded",
                            "status": "rate_limited",
                        },
                    },
                },
                True,
            )
        )
        resp_mcp = await bridge.handle_tools_call(
            "unwrap-mcp",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        body_mcp = json.loads(resp_mcp["result"]["content"][0]["text"])
        assert body_mcp["ok"] is False
        assert body_mcp["error_category"] == "权限或额度"
        assert "429 Too Many Requests" in body_mcp["error"]
        assert "禁止写成“没有相关信息”" in body_mcp["hint"]

        # 3. Real data_gateway.py:268 business no-match shape (status="no_match", data.reason="alphaengine_no_match",
        # data.alphaengine_error=None, names_tried=[...], without transcripts)
        # must return ok=True and empty_reason="no_matching_transcripts", NOT be treated as an error.
        nomatch_upstream = {
            "ok": False,
            "status": "no_match",
            "route": "transcript",
            "operation": "list",
            "data": {
                "ok": False,
                "ticker": "00700",
                "reason": "alphaengine_no_match",
                "alphaengine_error": None,
                "names_tried": ["00700", "腾讯", "Tencent"],
            },
            "fallback": {
                "source": "roic",
                "reason": "alphaengine_no_match",
                "ticker": "00700",
            },
            "hint": "AlphaEngine 无匹配：尝试用公司名（query='腾讯|Tencent'）重新检索，或检查 start/end 窗口。",
        }
        assert rb._unwrap_upstream_error_info(nomatch_upstream)["failed"] is False
        payload_queue.append((nomatch_upstream, True))
        resp_nomatch = await bridge.handle_tools_call(
            "unwrap-nomatch",
            {"name": "research", "arguments": {"route": "transcript", "ticker": "0700.HK"}},
        )
        assert resp_nomatch["result"]["isError"] is False
        body_nomatch = json.loads(resp_nomatch["result"]["content"][0]["text"])
        assert body_nomatch["ok"] is True
        assert body_nomatch["empty_reason"] == "no_matching_transcripts"
        assert "error" not in body_nomatch
        assert "error_category" not in body_nomatch

        # 4. Top-level ok=True with nested data.structuredContent.error="HTTP 403 Forbidden"
        # must force ok=False, isError=True, error_category="权限或额度", and populate error/hint.
        payload_queue.append(
            (
                {
                    "ok": True,
                    "status": "ok",
                    "route": "alphaengine",
                    "operation": "search_library",
                    "data": {
                        "structuredContent": {
                            "error": "HTTP 403 Forbidden",
                        },
                    },
                },
                False,
            )
        )
        resp_nested_err = await bridge.handle_tools_call(
            "unwrap-nested-err",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        assert resp_nested_err["result"]["isError"] is True
        body_nested_err = json.loads(resp_nested_err["result"]["content"][0]["text"])
        assert body_nested_err["ok"] is False
        assert "empty_reason" not in body_nested_err
        assert body_nested_err["error_category"] == "权限或额度"
        assert "HTTP 403 Forbidden" in body_nested_err["error"]
        assert body_nested_err["hint"]
        assert "禁止写成“没有相关信息”" in body_nested_err["hint"]

        # 5. Top-level ok=True with data.structuredContent={"ok": False, "results": []}
        # must return failed=True, ok=False, isError=True, and never set empty_reason.
        sc_ok_false = {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "operation": "search_library",
            "data": {
                "structuredContent": {
                    "ok": False,
                    "results": [],
                },
            },
        }
        assert rb._unwrap_upstream_error_info(sc_ok_false)["failed"] is True
        payload_queue.append((sc_ok_false, False))
        resp_sc_ok_false = await bridge.handle_tools_call(
            "unwrap-sc-ok-false",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        assert resp_sc_ok_false["result"]["isError"] is True
        body_sc_ok_false = json.loads(resp_sc_ok_false["result"]["content"][0]["text"])
        assert body_sc_ok_false["ok"] is False
        assert body_sc_ok_false["status"] == "error"
        assert "empty_reason" not in body_sc_ok_false
        assert body_sc_ok_false["error_category"] == "上游不可用"
        assert body_sc_ok_false["hint"]

        # 6. Top-level ok=True, status="empty" with data.structuredContent={"isError": True, "status": "ok"}
        # must return failed=True, ok=False, status="error" (never "ok"/"empty"), isError=True, and never set empty_reason.
        sc_is_error = {
            "ok": True,
            "status": "empty",
            "route": "alphaengine",
            "operation": "search_library",
            "data": {
                "structuredContent": {
                    "isError": True,
                    "status": "ok",
                    "results": [],
                },
            },
        }
        assert rb._unwrap_upstream_error_info(sc_is_error)["failed"] is True
        assert rb._unwrap_upstream_error_info(sc_is_error)["status"] == "error"
        payload_queue.append((sc_is_error, False))
        resp_sc_is_error = await bridge.handle_tools_call(
            "unwrap-sc-is-error",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        assert resp_sc_is_error["result"]["isError"] is True
        body_sc_is_error = json.loads(resp_sc_is_error["result"]["content"][0]["text"])
        assert body_sc_is_error["ok"] is False
        assert body_sc_is_error["status"] == "error"
        assert "empty_reason" not in body_sc_is_error
        assert body_sc_is_error["error_category"] == "上游不可用"
        assert body_sc_is_error["hint"]

        # 7. Top-level ok=True with data={"status": "forbidden", "message": "HTTP 403 Forbidden", "results": []}
        # must return failed=True, ok=False, status="forbidden", isError=True, error_category="权限或额度", and never set empty_reason.
        data_forbidden = {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "operation": "search_library",
            "data": {
                "status": "forbidden",
                "message": "HTTP 403 Forbidden",
                "results": [],
            },
        }
        assert rb._unwrap_upstream_error_info(data_forbidden)["failed"] is True
        assert rb._unwrap_upstream_error_info(data_forbidden)["status"] == "forbidden"
        payload_queue.append((data_forbidden, False))
        resp_data_forbidden = await bridge.handle_tools_call(
            "unwrap-data-forbidden",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        assert resp_data_forbidden["result"]["isError"] is True
        body_data_forbidden = json.loads(resp_data_forbidden["result"]["content"][0]["text"])
        assert body_data_forbidden["ok"] is False
        assert body_data_forbidden["status"] == "forbidden"
        assert "empty_reason" not in body_data_forbidden
        assert body_data_forbidden["error_category"] == "权限或额度"
        assert "HTTP 403 Forbidden" in body_data_forbidden["error"]
        assert body_data_forbidden["hint"]

        # 8. Specific failure status (`forbidden`) in `data.structuredContent.status` beats generic `data.status="error"`
        data_err_sc_forbidden = {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "operation": "search_library",
            "data": {
                "status": "error",
                "structuredContent": {
                    "status": "forbidden",
                    "results": [],
                },
            },
        }
        assert rb._unwrap_upstream_error_info(data_err_sc_forbidden)["failed"] is True
        assert rb._unwrap_upstream_error_info(data_err_sc_forbidden)["status"] == "forbidden"
        payload_queue.append((data_err_sc_forbidden, False))
        resp_sc_forb = await bridge.handle_tools_call(
            "unwrap-sc-forb",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "search_library",
                    "query": "META",
                },
            },
        )
        assert resp_sc_forb["result"]["isError"] is True
        body_sc_forb = json.loads(resp_sc_forb["result"]["content"][0]["text"])
        assert body_sc_forb["ok"] is False
        assert body_sc_forb["status"] == "forbidden"
        assert "empty_reason" not in body_sc_forb
        assert body_sc_forb["error_category"] == "权限或额度"
        assert body_sc_forb["hint"]

    @pytest.mark.asyncio
    async def test_ptc_tool_function_generator_wrapper_accepts_aliases_and_returns_normalized_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ptc_agent.core.tool_generator import ToolFunctionGenerator

        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            if req["method"] == "initialize":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "serverInfo": {"name": "researchdata", "version": "1.0.0"},
                        },
                    }
                ], 0
            if req["method"] == "tools/list":
                return [
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "finance",
                                    "description": "Financial filings and statements.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {"type": "string", "enum": ["sec", "cn-hk"]},
                                            "operation": {"type": "string"},
                                            "ticker": {"type": "string"},
                                            "start": {"type": "string"},
                                            "end": {"type": "string"},
                                            "limit": {"type": "integer", "default": 3},
                                            "timeout": {"type": "integer", "default": 180},
                                        },
                                        "required": ["route"],
                                    },
                                },
                                {
                                    "name": "research",
                                    "description": "Internal research library.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {
                                                "type": "string",
                                                "enum": [
                                                    "all",
                                                    "alphaengine",
                                                    "guidepoint",
                                                    "transcript",
                                                    "company-wiki",
                                                    "sales-notes",
                                                    "tmtb",
                                                    "funda-ai",
                                                ],
                                            },
                                            "operation": {"type": "string"},
                                            "query": {"type": "string"},
                                            "limit": {"type": "integer", "default": 3},
                                            "timeout": {"type": "integer", "default": 180},
                                        },
                                        "required": ["route"],
                                    },
                                },
                                {
                                    "name": "community",
                                    "description": "Community sources.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {
                                                "type": "string",
                                                "enum": ["xueqiu", "employee", "last30days", "x-timeline"],
                                            },
                                            "mode": {"type": "string", "default": "fetch"},
                                            "limit": {"type": "integer", "default": 3},
                                            "timeout": {"type": "integer", "default": 180},
                                        },
                                        "required": ["route"],
                                    },
                                },
                                {
                                    "name": "macro",
                                    "description": "Macro series.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "route": {"type": "string", "enum": ["fred", "treasury", "market-regime"]},
                                            "series": {"type": "string"},
                                            "start": {"type": "string"},
                                            "end": {"type": "string"},
                                        },
                                        "required": ["route"],
                                    },
                                },
                                {
                                    "name": "catalog",
                                    "description": "List routes.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"category": {"type": "string"}},
                                    },
                                },
                                {
                                    "name": "news",
                                    "description": "News sources.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"route": {"type": "string"}},
                                    },
                                },
                            ]
                        },
                    }
                ], 0
            args = req["params"].get("arguments") or {}
            forwarded_args.append(args)
            return [
                _mcp_tool_response(
                    req["id"],
                    {"ok": True, "status": "ok", "rows": [{"form": "10-K"}]},
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        filtered_tools = await bridge.get_filtered_tools()
        gen = ToolFunctionGenerator()

        # Verify all 6 tool descriptions are concise (< 800 chars), appended after upstream text,
        # contain the 3 required points, and are not swallowed into `Returns:` by _extract_return_info.
        for t in filtered_tools:
            desc = t["description"]
            assert len(desc) < 800, f"{t['name']} description too long ({len(desc)} chars): {desc}"
            assert "catalog(" in desc, f"{t['name']} missing catalog pointer"
            assert "ok: false" in desc and "hint" in desc, f"{t['name']} missing ok:false hint rule"
            assert gen._extract_return_info(desc) == (
                "Any",
                "Tool execution result",
            ), f"{t['name']} suffix swallowed into Returns"

        # Generate Python wrapper module via ToolFunctionGenerator and execute it with aliases
        from ptc_agent.core.mcp_registry import MCPToolInfo

        tool_infos = [
            MCPToolInfo(
                name=t["name"],
                description=t["description"],
                input_schema=t["inputSchema"],
                server_name="researchdata",
            )
            for t in filtered_tools
        ]

        sent_to_bridge: list[dict[str, Any]] = []

        def _call_mcp_tool_proto(server_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
            return None

        call_mcp_mock = create_autospec(_call_mcp_tool_proto)

        def _sync_bridge_caller(
            server_name: str, tool_name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            assert server_name == "researchdata"
            sent_to_bridge.append({"name": tool_name, "arguments": dict(arguments)})
            return {"ok": True, "captured": dict(arguments)}

        call_mcp_mock.side_effect = _sync_bridge_caller

        module_code = gen.generate_tool_module("researchdata", tool_infos)
        ns: dict[str, Any] = {}
        exec(module_code, ns)
        ns["_call_mcp_tool"] = call_mcp_mock

        # Call generated `finance` wrapper using aliases: symbol, op, from_date, to_date
        ns["finance"](
            route="cn-hk",
            op="list-filings",
            symbol="0700.HK",
            from_date="2025-01-01",
            to_date="2026-10-23",
        )
        # Verify unset optional parameters (like limit/timeout) are NOT injected by wrapper
        assert sent_to_bridge[-1]["arguments"] == {
            "route": "cn-hk",
            "op": "list-filings",
            "symbol": "0700.HK",
            "from_date": "2025-01-01",
            "to_date": "2026-10-23",
        }
        resp_fin = await bridge.handle_tools_call("ptc-fin", sent_to_bridge[-1])
        body_fin = json.loads(resp_fin["result"]["content"][0]["text"])
        assert body_fin["ok"] is True
        assert body_fin["normalized_params"] == {
            "route": "cn-hk",
            "operation": "filings",
            "ticker": "00700",
            "start": "2025-01-01",
            "end": "2026-10-23",
        }

        # Call generated `research` wrapper using aliases: q, max_results
        ns["research"](route="sales-notes", operation="search", q="META Reels", max_results=5)
        resp_res = await bridge.handle_tools_call("ptc-res", sent_to_bridge[-1])
        body_res = json.loads(resp_res["result"]["content"][0]["text"])
        assert body_res["ok"] is True
        assert body_res["normalized_params"] == {
            "route": "sales-notes",
            "operation": "search",
            "query": "META Reels",
            "limit": 5,
        }

        # Call generated `community` wrapper on xueqiu without unset defaults leaking in
        ns["community"](route="xueqiu", operation="posts", symbol="00700")
        assert sent_to_bridge[-1]["arguments"] == {
            "route": "xueqiu",
            "operation": "posts",
            "symbol": "00700",
        }

        # Call generated `finance` wrapper passing ONLY route alias `source="sec"` (without `route=`)
        ns["finance"](source="sec", symbol="META")
        assert sent_to_bridge[-1]["arguments"] == {
            "source": "sec",
            "symbol": "META",
        }
        resp_route_alias = await bridge.handle_tools_call("ptc-route-alias", sent_to_bridge[-1])
        body_route_alias = json.loads(resp_route_alias["result"]["content"][0]["text"])
        assert body_route_alias["ok"] is True
        assert body_route_alias["normalized_params"] == {
            "route": "sec",
            "ticker": "META",
        }

        # Omitting both `route` and all route aliases (`source`/`channel`/`provider`) is rejected by bridge
        resp_no_route = await bridge.handle_tools_call(
            "ptc-no-route",
            {"name": "finance", "arguments": {"symbol": "META"}},
        )
        assert resp_no_route["result"]["isError"] is True
        body_no_route = json.loads(resp_no_route["result"]["content"][0]["text"])
        assert body_no_route["ok"] is False
        assert body_no_route["error_code"] == "missing_required_parameters"
        assert body_no_route["error_category"] == "参数错误"
        assert "route" in body_no_route["missing_parameters"]

    @pytest.mark.asyncio
    async def test_alias_mapped_to_unaccepted_route_param_rejected_and_nested_params_validated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        forwarded_args: list[dict[str, Any]] = []

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            args = req["params"].get("arguments") or {}
            forwarded_args.append(args)
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "data": {"structuredContent": {"returned_chars": 120, "text": "doc body"}},
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. Alias mapped to a canonical parameter NOT accepted by that route/operation -> rejected
        unaccepted_alias_cases = [
            # series_id -> series, which is not accepted on finance(route="sec")
            ("finance", {"route": "sec", "operation": "financials", "ticker": "META", "series_id": "SOFR"}),
            # symbol -> ticker, which is not accepted on macro(route="treasury")
            ("macro", {"route": "treasury", "dataset": "tga", "start": "2026-01-01", "end": "2026-10-23", "symbol": "AAPL"}),
            # from -> start, which is not accepted on community(route="xueqiu")
            ("community", {"route": "xueqiu", "operation": "posts", "ticker": "00700", "from": "2026-01-01"}),
        ]
        for idx, (tool_name, args) in enumerate(unaccepted_alias_cases):
            resp = await bridge.handle_tools_call(
                f"unacc-alias-{idx}", {"name": tool_name, "arguments": args}
            )
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is False, f"Expected rejection for {args}: {body}"
            assert body["error_code"] == "unknown_parameters"
            assert body["error_category"] == "参数错误"
            assert body["accepted_parameters"]

        # 2. Nested `params` conflict (params.document vs params.doc_id; top-level document vs params.doc_id)
        for idx, bad_params_args in enumerate(
            [
                {
                    "route": "alphaengine",
                    "operation": "get_document",
                    "params": {"document": "111", "doc_id": "222"},
                },
                {
                    "route": "sales-notes",
                    "operation": "read",
                    "document": "mail:111",
                    "params": {"doc_id": "mail:222"},
                },
                {
                    "route": "sales-notes",
                    "operation": "read",
                    "document": "mail:111",
                    "params": {"offset": 0, "start_offset": 500},
                },
            ]
        ):
            resp_c = await bridge.handle_tools_call(
                f"nested-conflict-{idx}",
                {"name": "research", "arguments": bad_params_args},
            )
            body_c = json.loads(resp_c["result"]["content"][0]["text"])
            assert body_c["ok"] is False
            assert body_c["error_code"] == "parameter_conflict"
            assert body_c["error_category"] == "参数错误"

        # 3. Unknown key inside `params` -> rejected with accepted_nested_parameters
        resp_unk_p = await bridge.handle_tools_call(
            "nested-unk",
            {
                "name": "research",
                "arguments": {
                    "route": "sales-notes",
                    "operation": "search",
                    "query": "META",
                    "params": {"layer": "raw", "unexpected_nested_key": True},
                },
            },
        )
        body_unk_p = json.loads(resp_unk_p["result"]["content"][0]["text"])
        assert body_unk_p["ok"] is False
        assert body_unk_p["error_code"] == "unknown_parameters"
        assert "unexpected_nested_key" in body_unk_p["error"]
        assert "layer" in body_unk_p["accepted_nested_parameters"]

        assert len(forwarded_args) == 0

        # 4. AlphaEngine top-level `document` (or alias `doc_id`) maps to `params.doc_id` and deletes top-level `document`
        resp_ae_doc = await bridge.handle_tools_call(
            "ae-top-doc",
            {
                "name": "research",
                "arguments": {
                    "route": "alphaengine",
                    "operation": "get_document",
                    "document": "190000041634994",
                },
            },
        )
        body_ae_doc = json.loads(resp_ae_doc["result"]["content"][0]["text"])
        assert body_ae_doc["ok"] is True
        assert len(forwarded_args) == 1
        assert "document" not in forwarded_args[-1]
        assert forwarded_args[-1]["params"] == {"doc_id": "190000041634994"}
        assert body_ae_doc["normalized_params"] == {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": "190000041634994"},
        }

    @pytest.mark.asyncio
    async def test_catalog_first_routing_all_routes_and_required_params_and_examples_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ast

        bridge = _make_bridge(tmp_path, monkeypatch)

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            assert req["params"]["name"] == "catalog"
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": True,
                        "status": "ok",
                        "routes": [
                            {"id": rid, "category": spec["category"], "operations": list(spec["operations"])}
                            for rid, spec in rb.ROUTE_CONTRACT_SPECS.items()
                            if rid not in rb.BRIDGE_COMMUNITY_ROUTES and rid != "market-regime"
                        ],
                    },
                )
            ], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # 1. Every route in ROUTE_CONTRACT_SPECS (including xueqiu, employee, last30days, x-timeline, market-regime)
        # must succeed via catalog(route=rid) without triggering community or market-regime execution.
        for rid, spec in rb.ROUTE_CONTRACT_SPECS.items():
            resp = await bridge.handle_tools_call(
                f"cat-{rid}",
                {"name": "catalog", "arguments": {"route": rid}},
            )
            assert not resp["result"].get("isError"), f"catalog(route={rid!r}) failed: {resp}"
            body = json.loads(resp["result"]["content"][0]["text"])
            assert body["ok"] is True
            assert body["route_count"] == 1
            entry = body["routes"][0]
            assert entry["id"] == rid
            assert entry["required_params"] == spec["required_params"]
            assert entry["accepted_params"] == spec["accepted_params"]

        # 2. Required parameters aligned with data_gateway.py (fred/treasury/cn-hk filings start+end, sec smart-facts query, transcript ticker)
        missing_required_cases = [
            ("macro", {"route": "fred", "series": "SOFR"}, "start"),
            ("macro", {"route": "treasury", "dataset": "tga", "start": "2026-01-01"}, "end"),
            ("finance", {"route": "cn-hk", "operation": "filings", "ticker": "00700"}, "start"),
            ("finance", {"route": "sec", "operation": "smart-facts", "ticker": "META"}, "query"),
            ("research", {"route": "transcript", "query": "Meta Platforms"}, "ticker"),
        ]
        for idx, (tool_name, bad_args, missing_field) in enumerate(missing_required_cases):
            _norm, _view, val_err = rb._normalize_and_validate_call_args(tool_name, bad_args)
            assert val_err is not None, f"Expected missing_required_parameters for {bad_args}"
            assert val_err["error_code"] == "missing_required_parameters"
            assert missing_field in val_err["missing_parameters"]

        # treasury does not accept vintage
        _norm_tr, _view_tr, err_tr = rb._normalize_and_validate_call_args(
            "macro",
            {"route": "treasury", "dataset": "tga", "start": "2026-01-01", "end": "2026-10-23", "vintage": "2026-10-23"},
        )
        assert err_tr is not None and err_tr["error_code"] == "unknown_parameters"

        # 3. Every example in ROUTE_CONTRACT_SPECS passes bridge parameter validation (and sales-mail example uses route="sales-mail")
        assert any('route="sales-mail"' in ex for ex in rb.ROUTE_CONTRACT_SPECS["sales-mail"]["examples"])
        for rid, spec in rb.ROUTE_CONTRACT_SPECS.items():
            for ex in spec["examples"]:
                tree = ast.parse(ex, mode="eval")
                assert isinstance(tree.body, ast.Call)
                fn_name = tree.body.func.id if isinstance(tree.body.func, ast.Name) else ""
                kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in tree.body.keywords if kw.arg}
                assert kwargs.get("route") == rid, f"Example {ex!r} route mismatch for {rid}"
                _norm, _view, val_err = rb._normalize_and_validate_call_args(fn_name, kwargs)
                assert val_err is None, f"Example {ex!r} failed validation: {val_err}"

    @pytest.mark.asyncio
    async def test_route_all_fanout_unwraps_nested_failures_and_sets_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        scenario = ["ae_sc_ok_false"]

        async def _responder(reqs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            req = reqs[0]
            r_arg = (req["params"].get("arguments") or {}).get("route")
            if r_arg == "all":
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "results": [
                                {
                                    "route": "company-wiki",
                                    "document_id": "wiki:meta",
                                    "title": "META Wiki",
                                }
                            ],
                        },
                    )
                ], 0
            if r_arg == "alphaengine":
                if scenario[0] == "ae_sc_ok_false":
                    return [
                        _mcp_tool_response(
                            req["id"],
                            {
                                "ok": True,
                                "status": "ok",
                                "data": {
                                    "structuredContent": {
                                        "ok": False,
                                        "results": [],
                                    }
                                },
                            },
                        )
                    ], 0
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "data": {
                                "structuredContent": {
                                    "results": [{"doc_id": "12345", "title": "AE Doc"}]
                                }
                            },
                        },
                    )
                ], 0
            if r_arg == "guidepoint":
                if scenario[0] == "gp_data_forbidden":
                    return [
                        _mcp_tool_response(
                            req["id"],
                            {
                                "ok": True,
                                "status": "ok",
                                "data": {
                                    "status": "forbidden",
                                    "message": "HTTP 403 Forbidden",
                                    "results": [],
                                },
                            },
                        )
                    ], 0
                return [
                    _mcp_tool_response(
                        req["id"],
                        {
                            "ok": True,
                            "status": "ok",
                            "data": {
                                "structuredContent": {
                                    "results": [{"id": "gp_1", "title": "GP Transcript"}]
                                }
                            },
                        },
                    )
                ], 0
            return [_mcp_tool_response(req["id"], {"ok": True, "status": "ok", "results": []})], 0

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _make_subprocess_double(_responder)
        )

        # Case 1: AlphaEngine returns {ok: True, data: {structuredContent: {ok: False, results: []}}}
        resp1 = await bridge.handle_tools_call(
            "fanout-nested-1",
            {"name": "research", "arguments": {"route": "all", "query": "META"}},
        )
        assert resp1["result"]["isError"] is True
        body1 = json.loads(resp1["result"]["content"][0]["text"])
        assert body1["ok"] is False
        assert body1["status"] == "partial_error"
        assert "empty_reason" not in body1
        assert body1["source_status"]["alphaengine"]["ok"] is False
        assert body1["source_status"]["alphaengine"]["status"] == "failed"
        assert body1["partial"] == ["local_all", "guidepoint"]

        # Case 2: Guidepoint returns {ok: True, data: {status: "forbidden", message: "HTTP 403 Forbidden", results: []}}
        scenario[0] = "gp_data_forbidden"
        resp2 = await bridge.handle_tools_call(
            "fanout-nested-2",
            {"name": "research", "arguments": {"route": "all", "query": "META"}},
        )
        assert resp2["result"]["isError"] is True
        body2 = json.loads(resp2["result"]["content"][0]["text"])
        assert body2["ok"] is False
        assert body2["status"] == "partial_error"
        assert "empty_reason" not in body2
        assert body2["source_status"]["guidepoint"]["ok"] is False
        assert body2["source_status"]["guidepoint"]["status"] == "forbidden"
        assert body2["partial"] == ["local_all", "alphaengine"]
        assert body2["error_category"] == "权限或额度"

    @pytest.mark.asyncio
    async def test_last30days_and_mcp_is_error_recomputed_from_final_payload_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock = create_autospec(asyncio.create_subprocess_exec)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            out = {
                "query": "META",
                "results": [],
                "source_status": {
                    "x": {"state": "no-results", "isError": True},
                },
            }
            proc.communicate.return_value = (json.dumps(out).encode("utf-8"), b"")
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            "l30-iserror-sync",
            {"name": "community", "arguments": {"route": "last30days", "query": "META"}},
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert resp["result"]["isError"] is True
        assert body["status"] in rb._FAILURE_STATUSES
        assert "empty_reason" not in body
        assert body["error_category"] == "上游不可用"
        assert body["hint"]


