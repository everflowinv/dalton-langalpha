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

