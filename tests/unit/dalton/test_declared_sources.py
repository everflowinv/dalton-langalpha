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
                        "recency_bias": "1y",
                    },
                    "document": "ignored-doc",
                },
            },
        )

        routes_called = [c["route"] for c in recorded_calls]
        assert sorted(routes_called) == ["all", "alphaengine", "guidepoint"]

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
        assert gp_call["params"] == {"keywords": ["LFP"], "recency_bias": "1y"}
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
            ("finance", {"route": "sec", "operation": "search", "query": "CATL"}),
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["status"] == "ok"
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
        assert body["status"] == "ok"
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is True
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["structuredContent"] == body
        assert body["ok"] is True
        assert body["status"] == "ok"
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
        assert not resp["result"].get("isError")
        body = json.loads(resp["result"]["content"][0]["text"])
        assert resp["result"]["structuredContent"] == body
        assert body["ok"] is True
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
        assert "→ alphaengine：卖方研报与会议纪要（来源：AlphaEngine，先 search_library 再 get_document）" in desc
        assert "→ guidepoint：专家访谈与一线经营反馈（来源：Guidepoint）" in desc
        assert "→ transcript：业绩电话会转写稿（来源：AlphaEngine 业绩会 ASR 稿（主）/ roic.ai（兜底））" in desc
        assert "→ company-wiki：已入库公司与行业资料（来源：本地 company-wiki，只读，params.company=TICKER）" in desc
        assert "→ sales-notes：卖方销售邮件与市场摘要（来源：本地 market-digest 私有库）" in desc
        assert "→ tmtb：TMT Breakout 本地原文缓存（来源：AlphaEngine TMTB 已入库副本）" in desc
        assert "→ funda-ai：FUNDA AI 原邮件与图表（来源：fundaai@substack.com" in desc
        assert "→ all：内部研究资料跨库检索（本地库 company-wiki + sales-notes + tmtb + funda-ai，外加并行的 AlphaEngine 与 Guidepoint 检索；不含 transcript 电话会转写稿" in desc

        assert "employee" not in research_tool["inputSchema"]["properties"]["route"]["enum"]
        assert catalog_tool["description"] == "List routes."

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
    def test_all_22_dalton_skills_match_skill_md_frontmatter(self) -> None:
        import yaml

        from ptc_agent.agent.middleware.skills.dalton_registry import (
            DALTON_SKILL_DEFINITIONS,
            DALTON_SKILLS_DIR,
            _DALTON_SKILL_SPECS,
        )
        from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

        assert len(_DALTON_SKILL_SPECS) == 22
        assert len(DALTON_SKILL_DEFINITIONS) == 22

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
            assert body["ok"] is False
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
            "limit": 3,
            "timeout": 180,
            "series": "DGS10",
            "concept": "Revenues",
            "item": "Item 7",
            "document": "doc-1",
            "form": "10-K",
            "dataset": "edgar",
            "start": "2026-01-01",
            "end": "2026-10-22",
            "as_of": "2026-10-22",
            "vintage": "2026-10-22",
            "params": {"offset": 0},
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

        # 2. Empty: all sources are no-results and 0 items -> ok=False, status="empty"
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
        assert body_emp["ok"] is False
        assert body_emp["status"] == "empty"

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
    async def test_transcript_drops_topic_query_and_rewrites_roic_403(
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
            # Simulate roic fallback 403
            return [
                _mcp_tool_response(
                    req["id"],
                    {
                        "ok": False,
                        "status": "error",
                        "data": {"ok": False, "error": "HTTP Error 403: Forbidden"},
                        "fallback": {"source": "roic", "reason": "alphaengine_no_match"},
                        "hint": "old hint",
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

        # 2. roic 403 fallback rewrites error and hint ONLY on structured roic + 403/403 Forbidden
        resp_403 = await bridge.handle_tools_call(
            "tr-3",
            {
                "name": "research",
                "arguments": {
                    "route": "transcript",
                    "ticker": "UNKNOWN",
                },
            },
        )
        body_403 = json.loads(resp_403["result"]["content"][0]["text"])
        assert body_403["error"] == "AlphaEngine 无匹配，roic 回退已不可用。"
        assert body_403["hint"] == "用公司名（| 分隔中英文）重试，或者扩大 start/end。"

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
        assert body_non_roic["error"] == "upstream timeout discussing roic metric 403bps"
        assert body_non_roic["hint"] == "keep original hint"

        # 3. catalog includes transcript and pagination rules
        resp_cat = await bridge.handle_tools_call(
            "cat-1", {"name": "catalog", "arguments": {}}
        )
        cat_body = json.loads(resp_cat["result"]["content"][0]["text"])
        tr_route = next(r for r in cat_body["routes"] if r["id"] == "transcript")
        wiki_route = next(r for r in cat_body["routes"] if r["id"] == "company-wiki")
        assert any("只用来传公司名" in rl for rl in tr_route["rules"])
        assert any("1–30000" in rl for rl in wiki_route["rules"])

