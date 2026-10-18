"""Unit tests for `src/dalton/provenance/declared_sources.py` and `scripts/dalton/source_identity.py`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

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
