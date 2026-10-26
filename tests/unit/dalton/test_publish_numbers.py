"""Unit tests for Publication Governance, Figure Audit, Cross-Family Review, and Immutable Files (PK.8–PK.12, P0H, P0I)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.publish import audit as audit_mod
from src.dalton.publish import numbers as numbers_mod
from src.dalton.publish.audit import (
    CONTRADICTED_REASONS,
    aaudit_draft,
    render_annotated_draft,
    render_audit_md,
    verify_llm_judge_verdict,
)
from src.dalton.publish.files import (
    _pick_non_colliding_path,
    compute_bundle_sha256,
    prepend_publication_banner,
)
from src.dalton.publish.match import (
    match_figures,
    retrieve_candidate_sources,
    values_within_tolerance,
)
from src.dalton.publish.numbers import (
    ExtractedFigure,
    _can_be_incidental,
    extract_figures,
    normalize_period,
    parse_facts_ledger,
    parse_footnotes,
)
from src.dalton.publish.review import (
    ReviewBundle,
    ReviewCheckItem,
    ReviewResult,
    ReviewVerdict,
    family_of,
    pick_reviewer_model,
    render_review_input,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
G3_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "dalton" / "g3"


def test_normalize_period() -> None:
    assert normalize_period("FY26") == "FY2026"
    assert normalize_period("FY2027E") == "FY2027"
    assert normalize_period("Q3 2026") == "3Q26"
    assert normalize_period("3Q26E") == "3Q26"
    assert normalize_period("1H2026") == "1H26"
    assert normalize_period("26Q4") == "4Q26"
    assert normalize_period("26H1") == "1H26"
    assert normalize_period("2026 上半年") == "1H26"
    assert normalize_period("2026 下半年") == "2H26"
    assert normalize_period("2026 全年") == "FY2026"
    assert normalize_period("2026 财年") == "FY2026"


def test_extract_figures_exclusions_and_exemptions() -> None:
    md = """# 1. META 3Q26 财报前瞻

我们在 2026-10-15（2026年10月15日）查阅了 10-Q 与 8-K，围绕 3 个支柱和 2 条分歧（P1、D2、K1.1、E-20261015-01）展开分析。
管理层在 2Q26 披露广告收入达到 $59.4bn[^e1]，同比增长 27%[^e1]。
我们预计 FY26 目标价为 $850[^m1]。
一致预期 FY2027 EPS 为 $34.05[^p1]。

> "We are investing $15B per GW cluster." —— 电话会引语豁免

假设某客户算力翻倍至 500 亿美元〔示意〕，仅供参考。

| 指标 | 2Q26 | 3Q26E |
| :--- | :---: | :---: |
| 营业收入 (USD bn)[^e1] | 60.8 | 63.4[^p1] |
| 营业利润 (USD bn) | 24.3[^e1] | 25.0[^m1] |

## 免责声明
本报告由 100 名研究员耗时 2025-2027 年整理，不构成投资建议。

[^e1]: evidence:E-20261015-01 (2Q26 10-Q)
[^m1]: model:model/meta_v1.xlsx#Summary!C10
[^p1]: prov:prov-yf-001
"""
    footnotes = parse_footnotes(md)
    assert footnotes == {
        "e1": "evidence:E-20261015-01",
        "m1": "model:model/meta_v1.xlsx#Summary!C10",
        "p1": "prov:prov-yf-001",
    }

    figs = extract_figures(md)
    non_exempt = [f for f in figs if not f.exempt]
    exempt = [f for f in figs if f.exempt]

    # Quote ($15B) and illustrative (500 亿美元〔示意〕) must be marked exempt
    assert len(exempt) == 2
    assert any("15B" in f.raw for f in exempt)
    assert any("500" in f.raw for f in exempt)

    # Structural / format tokens (2026-10-15, 10-Q, 8-K, P1, D2, K1.1) must NOT be in non_exempt;
    # numbers inside ## 免责声明 (100) are extracted and sent to Step B (P0J.J2b §8.1 #16).
    raw_tokens = [f.raw for f in non_exempt]
    assert "$59.4bn" in raw_tokens
    assert "27%" in raw_tokens
    assert "$850" in raw_tokens
    assert "$34.05" in raw_tokens
    assert "60.8" in raw_tokens
    assert "63.4" in raw_tokens
    assert "24.3" in raw_tokens
    assert "25.0" in raw_tokens
    assert "100" in raw_tokens
    assert len(non_exempt) == 9


@pytest.mark.asyncio
async def test_match_figures_all_orders_and_discrepancies() -> None:
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261015-01",
                "fact": "2Q26 revenue $60.8B, ad revenue $59.4B (+27% YoY), operating income $24.3B.",
                "source": {"type": "filing", "identity": "2Q26 10-Q", "tier": 1, "ref": {"kind": "file", "path": "10q.md"}},
                "figures": [
                    {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "广告收入同比增速", "value": 27.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "营业收入", "value": 60.8, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "营业利润", "value": 24.3, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                ],
            }
        ]
    }
    models_doc = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/meta_v1.xlsx",
                "sha256": "abc12345",
                "created_at": "2026-10-18T12:00:00Z",
                "reason": "v1",
                "recalculated": True,
                "key_outputs": [
                    {"name": "目标价", "cell": "Summary!C10", "value": 850.0, "unit": "USD", "period": "FY2026"},
                    {"name": "营业利润", "cell": "Summary!D12", "value": 25.0, "unit": "USD_bn", "period": "3Q26"},
                ],
            }
        ],
    }
    provenance_records = {
        "prov-yf-001": {
            "provider": "estimates:yfinance",
            "identifier": "yf_analysis",
            "source_type": "street_estimate",
            "result_sha256": "def67890",
            "body": "META consensus estimates: 3Q26 revenue $63.4 bn, FY2027 EPS $34.05.",
        },
        "prov-news-002": {
            "provider": "reuters",
            "identifier": "news_article",
            "source_type": "news",
            "result_sha256": "111222",
            "body": "Analysts see FY2027 EPS around $34.05.",
        },
    }

    # 1. Clean draft where all figures match (including derived营业利润率 40.0% = 24.3 / 60.8)
    clean_draft = """# META 3Q26 深度报告

2Q26 广告收入为 $59.4bn[^e1]，营业收入为 $60.8bn[^e1]，营业利润为 $24.3bn[^e1]，对应营业利润率 40.0%。
一致预期 3Q26E 营业收入为 $63.4bn[^p1]，一致预期 FY2027 EPS 为 $34.05[^p1]。
我们预计 FY26 目标价为 $850[^m1]，3Q26E 营业利润为 $25.0bn[^m2]。

[^e1]: evidence:E-20261015-01
[^p1]: prov:prov-yf-001
[^m1]: model:model/meta_v1.xlsx#Summary!C10
[^m2]: model:model/meta_v1.xlsx#Summary!D12
"""
    audit_ok = await aaudit_draft(
        clean_draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        provenance_records=provenance_records,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_ok.passed, f"Unmatched: {audit_ok.unmatched}"
    assert audit_ok.matched == 8
    # Verify Order 5 derived figure (40.0% = 24.3 / 60.8) was matched as `derived`
    assert any(m["kind"] == "derived" and m["raw"] == "40.0%" for m in audit_ok.matches)

    # 2. Draft with specific discrepancies (L36: only source_conflict is contradicted;
    #    unit mismatch and period mismatch emit reason="unverified" with descriptive Chinese hints):
    bad_draft = """# META 错误测试草稿

2Q26 广告收入为 $59.4mn[^e1]。
3Q26 广告收入为 $59.4bn[^e1]。
2Q26 营业收入为 $68.9bn[^e1]。
我们给予 FY26 目标价 $900[^e1]。

[^e1]: evidence:E-20261015-01
"""
    audit_bad = await aaudit_draft(
        bad_draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        provenance_records=provenance_records,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert not audit_bad.passed
    reasons_by_raw = {u["raw"]: u["reason"] for u in audit_bad.unmatched}
    hints_by_raw = {u["raw"]: u.get("hint", "") for u in audit_bad.unmatched}
    assert reasons_by_raw["$59.4mn"] == "unverified"
    assert "量纲" in hints_by_raw["$59.4mn"]
    assert reasons_by_raw["$59.4bn"] == "unverified"
    assert "期间" in hints_by_raw["$59.4bn"]
    assert reasons_by_raw["$68.9bn"] == "source_conflict"
    assert reasons_by_raw["$900"] == "unverified"
    assert len(audit_bad.contradicted) == 1
    assert audit_bad.contradicted[0]["raw"] == "$68.9bn"

    audit_md = render_audit_md(audit_bad, draft_path="drafts/meta.md")
    assert "source_conflict" in audit_md
    assert "脚注指向的证据量纲与正文不一致" in audit_md
    assert "脚注指向的证据期间与正文不一致" in audit_md

    annotated = render_annotated_draft(bad_draft, audit_bad)
    assert "**$59.4mn**⟨未核实⟩" in annotated
    assert "**$68.9bn**⟨未核实⟩" in annotated


@pytest.mark.asyncio
async def test_facts_ledger_order_0_matching() -> None:
    facts_md = """<!-- FACTS-LEDGER -->
| id | metric | period | value | unit | ref |
|---|---|---|---|---|---|
| F1 | 广告收入 | 2Q26 | 59.4 | USD_bn | evidence:E-20261015-01 |
<!-- END -->
"""
    ledger = parse_facts_ledger(facts_md)
    assert "F1" in ledger
    assert ledger["F1"]["value"] == 59.4

    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261015-01",
                "fact": "2Q26 ad revenue $59.4B",
                "figures": [
                    {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "2Q26", "label": "reported"}
                ],
            }
        ]
    }
    draft = "2Q26 广告收入达到 $59.4bn[^f1]。\n\n[^f1]: f:F1\n"
    audit = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        facts_markdown=facts_md,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit.passed
    assert audit.matched == 1


@pytest.mark.asyncio
async def test_cross_family_reviewer_selection_and_verdict() -> None:
    assert family_of("claude-opus-4-7") == "anthropic-claude"
    assert family_of("gpt-5.4") == "openai-gpt"
    assert family_of("gemini-3.1-pro") == "google-gemini"

    candidates = ["claude-opus-4-7", "gpt-5.4", "gemini-3.1-pro"]
    assert await pick_reviewer_model("claude-sonnet-4-6", candidates=candidates) == "gpt-5.4"
    assert await pick_reviewer_model("gpt-5.4", candidates=candidates) == "claude-opus-4-7"
    assert (
        await pick_reviewer_model(
            "claude-opus-4-7",
            candidates=candidates,
            available_models={"claude-opus-4-7"},
        )
        is None
    )

    verdict = ReviewVerdict(
        verdict="pass",
        summary="Missing bear rebuttal on D1",
        items=[ReviewCheckItem(check=2, result="revise", reason="Add rebuttal", lines=[12])],
        must_fix=["Add strongest rebuttal to D1 on line 12"],
        nice_to_have=[],
    )
    res = ReviewResult(status="done", model="gpt-5.4", family="openai-gpt", verdict=verdict)
    d = res.to_dict()
    assert d["verdict"] == "revise"

    bundle = ReviewBundle(
        title="META 3Q26 Preview",
        kind="earnings",
        draft_markdown="Line 1\nLine 2",
        audit_summary={"figures": 2, "matched": 2, "exempt": 0, "unmatched": []},
        thesis_md="# Thesis",
        debate_md="# Debate",
        management_md="# Management",
    )
    rendered_input = render_review_input(bundle, max_chars=50)
    assert "   1: Line 1" in rendered_input


@pytest.mark.asyncio
async def test_published_files_immutability_and_collision_suffix() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        async def adownload_file_bytes(self, path: str) -> bytes | None:
            return self.files.get(path)

        async def awrite_text(self, path: str, content: str) -> None:
            self.files[path] = content.encode("utf-8")

    backend = FakeBackend()
    base_path = "published/2026-10-18-meta-3q26.md"
    content_v1 = b"# Report v1"
    sha_v1 = hashlib.sha256(content_v1).hexdigest()

    p1 = await _pick_non_colliding_path(base_path, sha_v1, backend)
    assert p1 == base_path
    backend.files[p1] = content_v1

    p1_retry = await _pick_non_colliding_path(base_path, sha_v1, backend)
    assert p1_retry == base_path

    content_v2 = b"# Report v2 (updated)"
    sha_v2 = hashlib.sha256(content_v2).hexdigest()
    p2 = await _pick_non_colliding_path(base_path, sha_v2, backend)
    assert p2 == "published/2026-10-18-meta-3q26-2.md"

    banner_md = prepend_publication_banner(
        "# Body",
        title="META 3Q26 Preview",
        kind="earnings",
        published_date="2026-10-18",
        approver_user_id="pm_user",
        approved_at="2026-10-18T16:00:00Z",
        reviewer_model="gpt-5.4",
        reviewer_family="openai-gpt",
        publication_id="pub-123",
        thesis_version_no=4,
    )
    assert "dalton:publication id=pub-123" in banner_md
    assert "Thesis 正式版本**：v4" in banner_md

    b_sha1 = compute_bundle_sha256(b"draft", [("model", "model/a.xlsx", b"123")])
    b_sha2 = compute_bundle_sha256(b"draft", [("model", "model/a.xlsx", b"123")])
    assert b_sha1 == b_sha2


@pytest.mark.asyncio
async def test_g31_counterexamples_units_footnotes_tables_and_sec_accession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = """# 2Q26 复盘（Filing 10-K-2026-03, Accession 0001326801-26-000042, 10/12 05:22, msg 1485122334455667788, batch 20261018-0412）

广告收入 512 亿美元，同比 +21%[^E0001]；核心 AI 广告转化率提升约三成[^E0001]，退订率下降两成[^E0001]，渗透率达百分之三十[^E0001]，硬件亏损腰斩[^E0001]，视频时长占比过半[^E0001]。

| 期间 | 广告收入（亿美元） | 同比增速 |
| :--- | :---: | :---: |
| 2Q26 | 512[^E0001] | +21%[^E0001] |
"""
    figs = extract_figures(draft)
    raws = [f.raw for f in figs]
    assert raws == ["512 亿美元", "+21%", "512", "+21%"]
    prose_512 = next(f for f in figs if f.raw == "512 亿美元")
    assert prose_512.period == "2Q26"

    table_512 = [f for f in figs if f.raw == "512" and f.location.get("line", 0) > 5]
    assert len(table_512) == 1
    assert table_512[0].unit == "USD_100mn"
    assert table_512[0].scale == 1e8
    assert table_512[0].metric == "广告收入"

    mock_assist = create_autospec(assist_json)

    async def _side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        *,
        user_id: str = "local-dev-user",
        role: str = "assist",
        scope_ref: str | None = None,
        timeout_s: float = 60.0,
        model: str | None = None,
        llm_service: Any = None,
        workspace_id: str | None = None,
        thread_id: str | None = None,
        resend_indeterminate: bool = False,
        persist_raw: bool = True,
        skip_journal: bool = False,
        fallback_models: list[str] | None = None,
        deadline: float | None = None,
    ) -> AssistResult:
        if task == "audit.label_figures":
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {"id": "B-001", "metric": "广告收入", "period": "2Q26", "kind": "reported"},
                        {"id": "B-002", "metric": "广告收入同比增速", "period": "2Q26", "kind": "reported"},
                        {"id": "B-003", "metric": "广告收入", "period": "2Q26", "kind": "reported"},
                        {"id": "B-004", "metric": "广告收入同比增速", "period": "2Q26", "kind": "reported"},
                    ],
                    "extra_figures": [
                        {"raw": "三成", "value": 30.0, "unit": "pct", "metric": "AI 广告转化率提升", "period": "2Q26", "kind": "reported", "line": 3},
                        {"raw": "两成", "value": 20.0, "unit": "pct", "metric": "退订率下降", "period": "2Q26", "kind": "reported", "line": 3},
                        {"raw": "百分之三十", "value": 30.0, "unit": "pct", "metric": "渗透率", "period": "2Q26", "kind": "reported", "line": 3},
                        {"raw": "腰斩", "value": 50.0, "unit": "pct", "metric": "硬件亏损降幅", "period": "2Q26", "kind": "reported", "line": 3},
                        {"raw": "过半", "value": 50.0, "unit": "pct", "metric": "视频时长占比", "period": "2Q26", "kind": "reported", "line": 3},
                        {"raw": "并不存在于草稿中的幻觉词", "value": 99.0, "unit": "pct", "metric": "幻觉", "period": "2Q26", "kind": "reported", "line": 3},
                    ],
                },
            )
        return AssistResult(ok=False, data=None, error="unexpected_task")

    mock_assist.side_effect = _side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)

    thesis_doc = {
        "evidence": [
            {
                "id": "E0001",
                "fact": "2Q26 ad revenue reached $51.2B (+21% YoY), AI conversion up 30%, churn down 20%, penetration 30%, RL loss down 50%, video share >50%.",
                "figures": [
                    {"metric": "广告收入", "value": 51.2, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "广告收入同比增速", "value": 21.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "AI 广告转化率提升", "value": 30.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "退订率下降", "value": 20.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "渗透率", "value": 30.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "硬件亏损降幅", "value": 50.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "视频时长占比", "value": 50.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                ],
            }
        ]
    }
    res = await aaudit_draft(draft, thesis_doc=thesis_doc, user_id="local-dev-user")
    assert res.passed, f"Unexpected failures: {res.unmatched}"
    assert res.labeling_incomplete is False
    assert len(res.verified) == 9


@pytest.mark.asyncio
async def test_g31_provenance_rejects_wrong_metric_or_period_r4_3() -> None:
    provenance_records = {
        "prov-sec-001": {
            "provider": "sec_edgar",
            "identifier": "meta_10q",
            "source_type": "filing",
            "result_sha256": "deadbeef",
            "body": "In 1Q25, Reality Labs hardware revenue grew 21.0% YoY to $0.4B.",
        }
    }
    draft = "2Q26 广告收入同比 +21.0%[^p1]。\n\n[^p1]: prov:prov-sec-001\n"
    res = await aaudit_draft(draft, provenance_records=provenance_records, use_llm_judge=False, user_id="local-dev-user")
    assert not res.passed
    assert len(res.unverified) == 1
    assert len(res.contradicted) == 0


@pytest.mark.asyncio
async def test_g31_llm_judge_reverification_and_uncertain_bucket_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fig = ExtractedFigure(
        id="F-001",
        raw="512 亿美元",
        value=512.0,
        decimals=0,
        unit="USD_bn",
        scale=1e8,
        currency="USD",
        metric="广告收入",
        period="2Q26",
        label_hint="reported",
        location={"line": 3, "sentence": "2Q26 广告收入 512 亿美元。"},
    )
    cand = [
        {
            "source_id": "evidence:E0001",
            "text": "Meta 2Q26 Family of Apps ad revenue was $51.2B (+21% YoY).",
            "figures": [{"metric": "Ad Revenue", "value": 51.2, "unit": "USD_bn", "period": "2Q26"}],
        }
    ]

    # 1. S1: Hallucinated quote on `supported` -> downgraded to "uncertain" (P0I §1.5 / §1.8 S1)
    bucket_1, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote_metric_raw": "ad revenue",
            "quote": "This quote does not exist in source",
        },
        cand,
    )
    assert bucket_1 == "uncertain"

    # 2. Quote exists verbatim in source ("Family of Apps ad revenue"), but contains NO number -> downgraded to uncertain
    bucket_2, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote_metric_raw": "ad revenue",
            "quote": "Family of Apps ad revenue",
        },
        cand,
    )
    assert bucket_2 == "uncertain"

    # 3. Verbatim quote containing "$51.2B" and verbatim quote_metric_raw "ad revenue" -> verified!
    bucket_3, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote_metric_raw": "ad revenue",
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_3 == "verified"

    # 4. Contradicted verdict where quote is missing or unanchored -> downgraded to "unverified"
    bucket_4a, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "contradicted",
            "source_id": "evidence:E0001",
            "quote": "nonexistent quote",
        },
        cand,
    )
    assert bucket_4a == "unverified"

    # 5a. Supported verdict where quote is verbatim in source ("ad revenue was $51.2B"),
    #     but quote number ($51.2B) differs from draft figure (600 亿美元 = $60.0B) -> MUST be "uncertain"
    fig_wrong = ExtractedFigure(
        id="F-002",
        raw="600 亿美元",
        value=600.0,
        decimals=0,
        unit="USD_bn",
        scale=1e8,
        currency="USD",
        metric="广告收入",
        period="2Q26",
        label_hint="reported",
        location={"line": 3, "sentence": "2Q26 广告收入 600 亿美元。"},
    )
    bucket_3b, _ = verify_llm_judge_verdict(
        fig_wrong,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote_metric_raw": "ad revenue",
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_3b == "uncertain"

    # 5b. Contradicted verdict where quote is anchored AND has a differing number -> accepted as "contradicted"
    bucket_4b, detail_4b = verify_llm_judge_verdict(
        fig_wrong,
        {
            "verdict": "contradicted",
            "source_id": "evidence:E0001",
            "quote_metric_raw": "ad revenue",
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_4b == "contradicted"
    assert detail_4b["reason"] == "source_conflict"

    # 5c. Single line with both consensus (with consensus footnote) and our own estimate/reported (with evidence footnote)
    mixed_line_draft = (
        "2Q26 广告收入市场一致预期为 $50.0B[^p_cons]，而我们测算/实际录得 2Q26 广告收入为 $51.2B[^E0001]。\n\n"
        "[^p_cons]: prov:prov-yf-001\n"
    )
    mixed_res = await aaudit_draft(
        mixed_line_draft,
        thesis_doc={
            "evidence": [
                {
                    "id": "E0001",
                    "fact": "2Q26 ad revenue $51.2B",
                    "figures": [{"metric": "广告收入", "value": 51.2, "unit": "USD_bn", "period": "2Q26", "label": "reported"}],
                }
            ]
        },
        provenance_records={
            "prov-yf-001": {
                "provider": "estimates:yfinance",
                "identifier": "yf",
                "source_type": "street_estimate",
                "result_sha256": "abc",
                "body": "2Q26 ad revenue consensus $50.0 bn.",
            }
        },
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert mixed_res.passed, f"Unexpected failures on mixed consensus+evidence line: {mixed_res.unmatched}"
    assert len(mixed_res.verified) == 2

    # 6. Exempt verdict without [估计]/[假设] marker in draft sentence -> downgraded to "unverified"
    bucket_5a, _ = verify_llm_judge_verdict(
        fig,
        {"verdict": "exempt", "reason": "analyst assumption"},
        cand,
    )
    assert bucket_5a == "unverified"

    fig_exempt = ExtractedFigure(
        id="F-003",
        raw="512 亿美元",
        value=512.0,
        decimals=0,
        unit="USD_bn",
        scale=1e8,
        currency="USD",
        metric="广告收入",
        period="2Q26",
        label_hint="assumption",
        location={"line": 3, "sentence": "[假设] 2Q26 广告收入 512 亿美元。"},
    )
    bucket_5b, _ = verify_llm_judge_verdict(
        fig_exempt,
        {"verdict": "exempt", "reason": "explicit assumption marker"},
        cand,
    )
    assert bucket_5b == "exempt"

    # 7. When assist_json fails (ok=False), figure goes to `uncertain`, `labeling_incomplete=True`, and does NOT block `passed`
    mock_assist_fail = create_autospec(assist_json, return_value=AssistResult(ok=False, data=None, error="llm_timeout"))
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist_fail)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist_fail)
    draft = "2Q26 某细分指标达到 88.8%。\n"
    res = await aaudit_draft(
        draft,
        thesis_doc={
            "evidence": [{"id": "E0001", "fact": "2Q26 某细分指标在北美市场表现良好。"}]
        },
        user_id="local-dev-user",
    )
    assert res.labeling_incomplete is True
    assert len(res.uncertain) == 1
    assert len(res.contradicted) == 0
    assert len(res.unverified) == 0
    assert res.passed is True


@pytest.mark.asyncio
async def test_g31_realistic_report_false_positive_rate_at_most_2() -> None:
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261015-01",
                "fact": "2Q26 总收入 608 亿美元（$60.8B），广告收入 594 亿美元（$59.4B，同比 +27%），营业利润 243 亿美元（$24.3B），capex 135 亿美元。",
                "figures": [
                    {"metric": "总收入", "value": 60.8, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "广告收入同比增速", "value": 27.0, "unit": "pct", "period": "2Q26", "label": "reported"},
                    {"metric": "营业利润", "value": 24.3, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "Capex", "value": 13.5, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                ],
            }
        ],
    }
    models_doc = {
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/meta_v1.xlsx",
                "key_outputs": [
                    {"name": "目标价", "cell": "Summary!C10", "value": 850.0, "unit": "USD", "period": "FY2026"},
                ],
            }
        ],
    }
    provenance_records = {
        "prov-yf-001": {
            "provider": "estimates:yfinance",
            "identifier": "yf_analysis",
            "source_type": "street_estimate",
            "result_sha256": "def67890",
            "body": "META consensus estimates: 3Q26 revenue $63.4 bn, FY2027 EPS $34.05.",
        }
    }
    report_md = """# META 2026Q3 财报前瞻深度报告（Accession 0001326801-26-000042, Form 10-Q-2026-02）

我们在 2Q26 看到公司总收入达 608 亿美元[^E-20261015-01]，其中广告收入 594 亿美元，同比 +27%[^E-20261015-01]。
2Q26 营业利润为 $24.3B[^E-20261015-01]，对应营业利润率 40.0%，单季 Capex 为 135 亿美元[^E-20261015-01]。
市场一致预期 3Q26E 营业收入为 $63.4B[^p1]，一致预期 FY2027 EPS 为 $34.05[^p1]。
我们维持 FY26 目标价 $850[^m1]。

> "We expect AI infrastructure investment to remain significant in 2027, exceeding $100B." —— 管理层原话引用

| 指标 | 2Q26 |
| :--- | :---: |
| 总收入（亿美元） | 608[^E-20261015-01] |
| 广告收入（亿美元） | 594[^E-20261015-01] |
| 营业利润（亿美元） | 243[^E-20261015-01] |

[^m1]: model:model/meta_v1.xlsx#Summary!C10
[^p1]: prov:prov-yf-001
"""
    res = await aaudit_draft(
        report_md,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        provenance_records=provenance_records,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    false_positives = len(res.contradicted) + len(res.unverified)
    assert false_positives == 0, f"Unexpected false positives ({false_positives}): {res.unmatched}"


@pytest.mark.asyncio
async def test_g32_reviewer_refuses_none_or_unknown_author_model() -> None:
    from src.dalton.publish.review import run_review

    assert await pick_reviewer_model(None) is None
    assert await pick_reviewer_model("") is None
    assert await pick_reviewer_model("unknown-custom-model-v1") is None

    bundle = ReviewBundle(
        title="META 3Q26",
        kind="earnings",
        draft_markdown="# Draft",
        audit_summary={},
    )
    res_none = await run_review(bundle=bundle, author_model=None, user_id="u1")
    assert res_none.status == "unavailable"
    res_unknown = await run_review(
        bundle=bundle, author_model="unknown-custom-model-v1", user_id="u1"
    )
    assert res_unknown.status == "unavailable"


@pytest.mark.asyncio
async def test_g32_reviewer_falls_back_to_next_cross_family_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.llm import complete as complete_mod
    from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
    from src.dalton.publish.review import run_review

    bundle = ReviewBundle(
        title="META 3Q26",
        kind="earnings",
        draft_markdown="# Draft",
        audit_summary={},
    )
    candidates = [
        "gpt-6.1-sol-oauth",
        "claude-sonnet-5-5-oauth",
        "gemini-4-argon-high-oauth",
        "ocb-xai-grok-4.7",
    ]
    called_models: list[str] = []

    async def _side_effect(
        llm_service: Any = None,
        *,
        user_id: str | None = None,
        system_prompt: str | None = None,
        user_prompt: str = "",
        scope_ref: str | None = None,
        purpose: str = "dalton.complete",
        model: str | None = None,
        request_model: str | None = None,
        response_schema: Any = None,
        slot: str = "primary",
        reasoning_effort: str | None = None,
        timeout_s: float = 60.0,
        is_byok: bool = False,
        disable_tracing: bool = False,
        workspace_id: str | None = None,
        thread_id: str | None = None,
        stale_after_s: float = 360.0,
        resend_indeterminate: bool = False,
        raise_on_error: bool = False,
    ) -> DaltonCompleteResult:
        eff = str(request_model or model or "")
        called_models.append(eff)
        if eff == "gpt-6.1-sol-oauth":
            return DaltonCompleteResult(
                ok=False,
                status="failed",
                model=eff,
                error="http_4xx: OAuth token expired",
            )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            model=eff,
            data=ReviewVerdict(verdict="pass", summary="All good"),
            usage={"input_tokens": 100, "output_tokens": 20},
            cash_usd=0.0,
            notional_usd=0.01,
            invocation_id="lj1:rev-ok",
        )

    mock_complete = create_autospec(dalton_complete, side_effect=_side_effect)
    monkeypatch.setattr(complete_mod, "dalton_complete", mock_complete)

    res = await run_review(
        user_id="u1",
        author_model="claude-opus-5-5-oauth",
        bundle=bundle,
        candidates=candidates,
    )
    assert res.status == "done"
    assert res.model == "gemini-4-argon-high-oauth"
    assert res.family == "google-gemini"
    assert called_models == ["gpt-6.1-sol-oauth", "gemini-4-argon-high-oauth"]

    # When all cross-family candidates fail -> returns unavailable
    called_models.clear()
    mock_all_fail = create_autospec(
        dalton_complete,
        return_value=DaltonCompleteResult(
            ok=False,
            status="failed",
            model="ocb-xai-grok-4.7",
            error="upstream_timeout",
        ),
    )
    monkeypatch.setattr(complete_mod, "dalton_complete", mock_all_fail)
    res_fail = await run_review(
        user_id="u1",
        author_model="claude-opus-5-5-oauth",
        bundle=bundle,
        candidates=candidates,
    )
    assert res_fail.status == "unavailable"
    assert mock_all_fail.await_count == 3


@pytest.mark.asyncio
async def test_p0i_ten_r3_false_verified_counterexamples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify all 10 Round-3 false-verified counterexamples (P0I §1.8 #4)."""
    draft = """# 1. GOOGL 3Q25 季报复盘（参考 `GOOGL_3Q25_10-Q.pdf` 与 10-Q 披露）

1. 社区帖子获得 600 赞，接口偶发返回 HTTP 500，评测使用 Claude Sonnet 5.5，附件编号 Doc-42。
2. 截至 10 月底（自 7 月以来），管理层展望 26Q4 与 26H1（2026 上半年）云业务表现。
3. 3Q25 合并总收入为 882.68 亿美元；此外 4Q25 资本开支预计继续爬坡。
"""
    figs = extract_figures(draft)
    raws = [f.raw for f in figs]

    # Counterexamples #3, #4, #5:
    # - Filename `GOOGL_3Q25_10-Q.pdf`, `Doc-42`, and `10-Q` are masked, never extracted or polluting period to `4Q10`
    # - Chinese months `10 月底` and `7 月` are masked by _CN_DATE_RE, never extracted as 10 or 7
    # - `26Q4` and `26H1` are recognized as period tokens (`4Q26`, `1H26`), never extracted as bare numbers 26 or 4
    assert "10" not in raws
    assert "42" not in raws
    assert "7" not in raws
    assert "26" not in raws
    assert "4" not in raws
    assert all(f.period != "4Q10" for f in figs)

    # Counterexample #7 (3 period mis-assignments):
    # - `882.68 亿美元` in clause 1 of line 5 has period `3Q25`, NOT polluted by `4Q25` in the second clause after `；`
    fig_rev = next(f for f in figs if f.raw == "882.68 亿美元")
    assert fig_rev.period == "3Q25"

    # Counterexamples #1 (600 赞), #2 (HTTP 500), #6 (Sonnet 5.5):
    # Even when thesis_doc contains matching numeric values (600, 500, 42, 5.5), unfootnoted figures
    # cannot be auto-verified by Order 4 (L34), and Step B labels them `incidental`!
    thesis_doc = {
        "evidence": [
            {
                "id": "E-TRAP",
                "fact": "Trap numbers 600, 500, 42, 5.5 in 3Q25.",
                "source": {"type": "filing", "identity": "10-Q", "tier": 1, "ref": {"kind": "sec_filing", "accession": "0001652044-25-000088"}},
                "figures": [
                    {"metric": "陷阱600", "value": 600.0, "unit": "other", "period": "3Q25", "label": "reported"},
                    {"metric": "陷阱500", "value": 500.0, "unit": "other", "period": "3Q25", "label": "reported"},
                    {"metric": "陷阱42", "value": 42.0, "unit": "other", "period": "3Q25", "label": "reported"},
                    {"metric": "陷阱5.5", "value": 5.5, "unit": "other", "period": "3Q25", "label": "reported"},
                ],
            }
        ]
    }
    det_res = await aaudit_draft(draft, thesis_doc=thesis_doc, use_llm_judge=False, user_id="local-dev-user")
    assert det_res.matched == 0, f"Order 4 must never auto-verify unfootnoted figures: {det_res.matches}"

    mock_assist = create_autospec(assist_json)

    async def _side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            items = []
            for item in payload.get("extracted_figures") or payload.get("figures") or []:
                raw = str(item.get("raw") or "")
                if raw == "600":
                    items.append(
                        {
                            "id": item["id"],
                            "metric": "非财务数字",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "点赞数、转发数等社交互动计数",
                        }
                    )
                elif raw == "500":
                    items.append(
                        {
                            "id": item["id"],
                            "metric": "非财务数字",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "HTTP 状态码",
                        }
                    )
                elif raw == "5.5":
                    items.append(
                        {
                            "id": item["id"],
                            "metric": "模型版本",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "软件和模型的版本号、硬件型号",
                        }
                    )
                else:
                    items.append({"id": item["id"], "metric": "合并总收入", "period": "3Q25", "kind": "reported"})
            return AssistResult(ok=True, data={"items": items, "extra_figures": []})
        if task == "audit.judge":
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {"id": f["id"], "verdict": "unverified", "reason": "no matching source"}
                        for f in payload.get("figures", [])
                    ]
                },
            )
        return AssistResult(ok=False, data=None, error="unexpected_task")

    mock_assist.side_effect = _side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    full_res = await aaudit_draft(draft, thesis_doc=thesis_doc, use_llm_judge=True, user_id="local-dev-user")
    assert full_res.matched == 0
    incidental_raws = {i["raw"] for i in full_res.incidental}
    assert {"600", "500"}.issubset(incidental_raws)
    assert "5.5" not in incidental_raws
    uncertain_by_raw = {u["raw"]: u for u in full_res.uncertain}
    assert "5.5" in uncertain_by_raw
    assert uncertain_by_raw["5.5"]["hint"] == "LLM 判断为版本号或名称；带小数的数字不能自动排除，请 PM 确认"
    assert "5.5" not in {u["raw"] for u in full_res.unverified}


def test_p0i_two_anchoring_blind_spots_and_s1() -> None:
    """Verify the two anchoring blind spots and S1 fabricated quote handling (P0I §1.5 & §1.8 #4)."""
    # Blind spot 1: "400 亿 ATM 对资本开支余量"
    fig_capex = ExtractedFigure(
        id="F-CAPEX",
        raw="400 亿美元",
        value=400.0,
        decimals=0,
        unit="USD_bn",
        scale=1e8,
        currency="USD",
        metric="资本开支",
        period="FY2026",
        label_hint="reported",
        location={"line": 10, "sentence": "FY2026 资本开支预计为 400 亿美元。"},
    )
    cand_atm = [
        {
            "source_id": "evidence:E-ATM",
            "text": "As of Q3 2025, $40.0 billion remained available under the ATM share repurchase program.",
            "figures": [],
        }
    ]
    # If LLM claims supported but quote_metric_raw is missing or not verbatim in quote -> uncertain
    b_missing_metric, d_missing = verify_llm_judge_verdict(
        fig_capex,
        {
            "verdict": "supported",
            "source_id": "evidence:E-ATM",
            "quote_metric_raw": "Capital expenditures",
            "quote": "As of Q3 2025, $40.0 billion remained available under the ATM share repurchase program.",
        },
        cand_atm,
    )
    assert b_missing_metric == "uncertain"
    assert "quote_metric_raw" in d_missing["hint"]

    # Even if quote_metric_raw is in quote, quote has explicit period Q3 2025 (3Q25) != FY2026 -> uncertain!
    b_period_mismatch, d_period = verify_llm_judge_verdict(
        fig_capex,
        {
            "verdict": "supported",
            "source_id": "evidence:E-ATM",
            "quote_metric_raw": "ATM share repurchase program",
            "quote": "As of Q3 2025, $40.0 billion remained available under the ATM share repurchase program.",
        },
        cand_atm,
    )
    assert b_period_mismatch == "uncertain"
    assert "引文期间" in d_period["hint"]

    # Blind spot 2: "19.11 美元/股对 19.1%"
    figs_eps = extract_figures("FY2025 稀释后 EPS 为 19.11 美元/股。")
    assert len(figs_eps) == 1
    fig_eps = figs_eps[0]
    assert fig_eps.unit == "per_share"

    # Dimension check: per_share (19.11) vs pct (19.1%) MUST be False!
    assert values_within_tolerance(fig_eps, 19.1, "pct")[0] is False
    assert values_within_tolerance(fig_eps, 19.11, "USD_per_share")[0] is True
    assert values_within_tolerance(fig_eps, 19.11, "HKD_per_share")[0] is False
    assert values_within_tolerance(fig_eps, 19.11, "per_share")[0] is False

    # Dimension check: pct (19.1%) vs decimal ratio (0.191) is False when unit="other" or plain "pct",
    # and True only when pct_from_number_format=True
    fig_pct = extract_figures("营业利润率为 19.1%。")[0]
    assert fig_pct.unit == "pct"
    assert values_within_tolerance(fig_pct, 0.191, "other")[0] is False
    assert values_within_tolerance(fig_pct, 0.191, "pct", pct_from_number_format=True)[0] is True
    assert values_within_tolerance(fig_pct, 19.1, "USD_bn")[0] is False
    assert values_within_tolerance(fig_pct, 19.1, "per_share")[0] is False

    cand_margin = [
        {
            "source_id": "evidence:E-MARGIN",
            "text": "In FY2025, consolidated operating margin was 19.1%.",
            "figures": [],
        }
    ]
    b_dim, d_dim = verify_llm_judge_verdict(
        fig_eps,
        {
            "verdict": "supported",
            "source_id": "evidence:E-MARGIN",
            "quote_metric_raw": "operating margin",
            "quote": "In FY2025, consolidated operating margin was 19.1%.",
        },
        cand_margin,
    )
    assert b_dim == "uncertain"
    assert "量纲不符" in d_dim["hint"]


@pytest.mark.asyncio
async def test_p0i_legacy_thesis_excluded_from_verification_and_step_d() -> None:
    """Verify L35: migrated legacy_thesis evidence cannot verify figures in Step C or Step D."""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-LEGACY-001",
                "fact": "2Q26 广告收入为 594 亿美元。",
                "source": {
                    "type": "legacy_thesis",
                    "identity": "Migrated from thesis.md",
                    "tier": 3,
                    "ref": {"kind": "url", "path": "sources/legacy/meta_thesis.md"},
                },
                "figures": [
                    {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "2Q26", "label": "reported"}
                ],
            }
        ]
    }
    draft = "2Q26 广告收入为 594 亿美元[^E-LEGACY-001]。\n\n[^E-LEGACY-001]: evidence:E-LEGACY-001\n"
    res = await aaudit_draft(draft, thesis_doc=thesis_doc, use_llm_judge=False, user_id="local-dev-user")
    assert res.matched == 0
    assert len(res.unverified) == 1
    assert "旧认知中的说法，尚未用原始来源核实" in res.unverified[0]["hint"]
    assert res.unverified[0]["candidates"][0]["why"] == "旧认知中的说法，尚未用原始来源核实"

    # Step D candidate retrieval also excludes legacy_thesis
    fig = extract_figures(draft)[0]
    cands = retrieve_candidate_sources(fig, thesis_doc=thesis_doc)
    assert cands == []


def test_p0i_incidental_guard_rejects_currency_and_pct() -> None:
    """Verify _can_be_incidental guard: currency, pct, bps, and per_share figures can never be incidental."""
    figs = extract_figures(
        "帖子获 600 赞；收入 $500M；毛利率 45.2%；利差 50bps；EPS 为 19.11 美元/股。"
    )
    by_raw = {f.raw: f for f in figs}
    assert _can_be_incidental(by_raw["600"]) is True
    assert _can_be_incidental(by_raw["$500M"]) is False
    assert _can_be_incidental(by_raw["45.2%"]) is False
    assert _can_be_incidental(by_raw["50bps"]) is False
    fig_eps = next(f for f in figs if f.unit == "per_share")
    assert _can_be_incidental(fig_eps) is False


@pytest.mark.asyncio
async def test_p0i_section_batch_cache_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify modifying Section 1 leaves Section 2's Step B and Step D LLM payloads byte-for-byte identical."""
    draft_v1 = """## 第一节：收入概览
2Q26 搜索收入达到 540 亿美元。

## 第二节：云业务与利润率
2Q26 云业务收入达到 142.7 亿美元。
"""
    draft_v2 = """## 第一节：收入概览
2Q26 搜索收入达到 545 亿美元，YouTube 广告收入达到 98 亿美元。
此外订阅收入达到 110 亿美元。

## 第二节：云业务与利润率
2Q26 云业务收入达到 142.7 亿美元。
"""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-CLOUD",
                "fact": "Q2 2026 Google Cloud revenue was $14.27B.",
                "source": {"type": "filing", "identity": "10-Q", "tier": 1, "ref": {"kind": "sec_filing", "accession": "0001652044-26-000010"}},
                "figures": [
                    {"metric": "云业务收入", "value": 14.27, "unit": "USD_bn", "period": "2Q26", "label": "reported"}
                ],
            }
        ]
    }

    captured_label_sec2: list[str] = []
    captured_judge_sec2: list[str] = []

    mock_assist = create_autospec(assist_json)

    async def _side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            if "第二节" in str(payload.get("section_title") or ""):
                captured_label_sec2.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {"id": f["id"], "metric": "云业务收入", "period": "2Q26", "kind": "reported"}
                        for f in payload.get("extracted_figures") or payload.get("figures") or []
                    ],
                    "extra_figures": [],
                },
            )
        if task == "audit.judge":
            if "第二节" in str(payload.get("section_title") or ""):
                captured_judge_sec2.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "verdict": "supported",
                            "source_id": "evidence:E-CLOUD",
                            "quote_metric_raw": "Google Cloud revenue",
                            "quote": "Q2 2026 Google Cloud revenue was $14.27B.",
                        }
                        for f in payload.get("figures", [])
                    ]
                },
            )
        return AssistResult(ok=False, data=None, error="unexpected")

    mock_assist.side_effect = _side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    await aaudit_draft(draft_v1, thesis_doc=thesis_doc, use_llm_judge=True, user_id="local-dev-user")
    await aaudit_draft(draft_v2, thesis_doc=thesis_doc, use_llm_judge=True, user_id="local-dev-user")

    assert len(captured_label_sec2) == 2
    assert captured_label_sec2[0] == captured_label_sec2[1]
    assert len(captured_judge_sec2) == 2
    assert captured_judge_sec2[0] == captured_judge_sec2[1]


def test_p0i_contradicted_reasons_and_zero_unit_or_period_mismatch_assignments() -> None:
    """Verify L36: CONTRADICTED_REASONS == {'source_conflict'} and zero unit_mismatch / period_mismatch reason assignments."""
    assert CONTRADICTED_REASONS == frozenset({"source_conflict"})

    match_src = (REPO_ROOT / "src" / "dalton" / "publish" / "match.py").read_text(encoding="utf-8")
    audit_src = (REPO_ROOT / "src" / "dalton" / "publish" / "audit.py").read_text(encoding="utf-8")
    for src_name, src_text in [("match.py", match_src), ("audit.py", audit_src)]:
        assert not re.search(r'reason\s*[:=]\s*["\']unit_mismatch["\']', src_text), f"Found unit_mismatch in {src_name}"
        assert not re.search(r'reason\s*[:=]\s*["\']period_mismatch["\']', src_text), f"Found period_mismatch in {src_name}"
        assert "SF-CN" not in src_text, f"Found SF-CN in {src_name}"
        assert "_match_in_provenance_record" not in src_text, f"Found _match_in_provenance_record in {src_name}"
        assert "maybe_label_prose_figures_with_llm" not in src_text, f"Found maybe_label_prose_figures_with_llm in {src_name}"


@pytest.mark.asyncio
async def test_p0i_g3_gold_standard_and_cross_control_zero_false_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the committed G3 gold-standard dataset (159 figures across GOOGL + AMZN) and GOOGL-vs-META cross-control."""
    gold_doc = json.loads((G3_FIXTURES_DIR / "gold_standard.json").read_text(encoding="utf-8"))
    googl_draft = (G3_FIXTURES_DIR / "googl_draft.md").read_text(encoding="utf-8")
    amzn_draft = (G3_FIXTURES_DIR / "amzn_draft.md").read_text(encoding="utf-8")
    googl_ev = json.loads((G3_FIXTURES_DIR / "googl_sec_evidence.json").read_text(encoding="utf-8"))
    amzn_ev = json.loads((G3_FIXTURES_DIR / "amzn_sec_evidence.json").read_text(encoding="utf-8"))
    meta_ev = json.loads((G3_FIXTURES_DIR / "meta_cross_control_evidence.json").read_text(encoding="utf-8"))

    gold_items = gold_doc["items"]
    assert len(gold_items) >= 150

    # Verify all evidence items in GOOGL and AMZN fixtures have real SEC filing refs and zero legacy_thesis
    for ev_bundle in (googl_ev, amzn_ev):
        for ev in ev_bundle["evidence"]:
            src = ev["source"]
            assert src["type"] != "legacy_thesis"
            assert src["ref"].get("accession") or src["ref"].get("url")

    # 1. Cross-control: GOOGL draft audited against META evidence MUST produce 0 verified figures!
    cross_det = await aaudit_draft(googl_draft, thesis_doc=meta_ev, use_llm_judge=False, user_id="local-dev-user")
    assert cross_det.matched == 0, f"Cross-control (GOOGL draft vs META evidence) false verified: {cross_det.matches}"

    # 2. Audit GOOGL and AMZN drafts using gold-standard-informed Step B & Step D autospec double
    #    to verify deterministic + anchored pipeline achieves 0 false-verified and 0 financial incidental misclassifications.
    gold_by_report_line_raw: dict[tuple[str, int, str], dict[str, Any]] = {
        (it["report"], int(it["line"]), it["raw"]): it for it in gold_items
    }
    gold_by_report_sent_raw: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rep_name, draft_txt in [("GOOGL", googl_draft), ("AMZN", amzn_draft)]:
        d_lines = draft_txt.splitlines()
        for it in gold_items:
            if it["report"] == rep_name:
                lno = int(it["line"])
                sent = d_lines[lno - 1].strip() if 1 <= lno <= len(d_lines) else ""
                gold_by_report_sent_raw[(rep_name, sent, it["raw"])] = it

    ev_by_id: dict[str, dict[str, Any]] = {}
    for ev in googl_ev["evidence"] + amzn_ev["evidence"]:
        ev_by_id[ev["id"]] = ev

    current_report = {"name": "GOOGL"}
    mock_assist = create_autospec(assist_json)

    async def _gold_assist_side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        sec_title = str(payload.get("section_title") or "")
        draft_text = googl_draft if current_report["name"] == "GOOGL" else amzn_draft
        draft_lines = draft_text.splitlines()
        sec_start = 1
        for idx_l, l_str in enumerate(draft_lines, start=1):
            if sec_title and sec_title in l_str:
                sec_start = idx_l
                break

        if task == "audit.label_figures":
            items = []
            for f_in in payload.get("extracted_figures") or payload.get("figures") or []:
                rel_line = int(f_in.get("line") or 1)
                abs_line = sec_start + rel_line - 1
                raw = str(f_in.get("raw") or "")
                g = gold_by_report_line_raw.get((current_report["name"], abs_line, raw))
                if g and not g["is_verification_target"]:
                    items.append({"id": f_in["id"], "metric": "非财务数字", "period": "", "kind": "incidental"})
                elif g:
                    items.append({"id": f_in["id"], "metric": str(f_in.get("metric") or "财务指标"), "period": g.get("expected_period") or "", "kind": "reported"})
                else:
                    items.append({"id": f_in["id"], "metric": str(f_in.get("metric") or "指标"), "period": str(f_in.get("period") or ""), "kind": "reported"})
            return AssistResult(ok=True, data={"items": items, "extra_figures": []})

        if task == "audit.judge":
            out_items = []
            for f_in in payload.get("figures", []):
                raw = str(f_in.get("raw") or "")
                sent = str(f_in.get("sentence") or "").strip()
                g = gold_by_report_sent_raw.get((current_report["name"], sent, raw))
                src_id = g.get("expected_source_id") if g else None
                if g and g["has_source"] and src_id in ev_by_id:
                    ev_obj = ev_by_id[src_id]
                    fact_str = ev_obj["fact"]
                    m_words = [w for w in re.split(r"[,:;.]", fact_str) if w.strip()]
                    metric_phrase = m_words[0].strip()[:30] if m_words else fact_str[:20]
                    out_items.append(
                        {
                            "id": f_in["id"],
                            "verdict": "supported",
                            "source_id": f"evidence:{src_id}",
                            "quote_metric_raw": metric_phrase,
                            "quote": fact_str,
                        }
                    )
                else:
                    out_items.append(
                        {
                            "id": f_in["id"],
                            "verdict": "unverified",
                            "reason": "no matching source in evidence",
                        }
                    )
            return AssistResult(ok=True, data={"items": out_items})

        return AssistResult(ok=False, data=None, error="unexpected")

    mock_assist.side_effect = _gold_assist_side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    current_report["name"] = "GOOGL"
    googl_res = await aaudit_draft(googl_draft, thesis_doc=googl_ev, use_llm_judge=True, user_id="local-dev-user")

    current_report["name"] = "AMZN"
    amzn_res = await aaudit_draft(amzn_draft, thesis_doc=amzn_ev, use_llm_judge=True, user_id="local-dev-user")

    for rep_name, res in [("GOOGL", googl_res), ("AMZN", amzn_res)]:
        assert res.labeling_incomplete is False
        assert res.labeling["total"] == res.labeling["labeled"]
        assert res.labeling["failed_batches"] == 0
        # 0 false-verified: every verified figure must have has_source=True in gold standard
        for m in res.verified:
            g = gold_by_report_line_raw.get((rep_name, int(m["line"]), m["raw"]))
            if g is not None:
                assert g["has_source"] is True, f"False-verified figure in {rep_name}: {m} (gold={g})"
        # 0 financial figures misclassified as incidental
        for inc in res.incidental:
            g = gold_by_report_line_raw.get((rep_name, int(inc["line"]), inc["raw"]))
            if g is not None:
                assert g["is_verification_target"] is False, f"Financial figure marked incidental in {rep_name}: {inc}"


@pytest.mark.asyncio
async def test_p0j_j2_failed_batch_never_excludes_figures_and_colloquial_always_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0J.J2 (§2.2, §2.3, §2.5; replaces _COLLOQUIAL_HINT_RE and _EXEMPT_MARKER_RE):
    1. Every non-empty section (including colloquial-only sections with 0 Arabic digits) is sent to Step B.
    2. STEP_B_LABEL_INSTRUCTIONS and FigureLabels schema do not leak G3 fixture examples.
    3. When a labeling batch fails, all figures in that batch fall back to normal verification targets and
       cannot be marked incidental by another batch (no cross-batch by_id pollution).
    """
    colloquial_draft = (G3_FIXTURES_DIR / "colloquial_only_draft.md").read_text(encoding="utf-8")
    colloquial_gold = json.loads((G3_FIXTURES_DIR / "colloquial_only_gold.json").read_text(encoding="utf-8"))
    expected_raws = {it["raw"] for it in colloquial_gold["items"]}

    # Verify Step B prompt & schema do not hardcode G3 fixture raw examples
    schema_dump = json.dumps(numbers_mod.FigureLabels.model_json_schema(), ensure_ascii=False)
    for forbidden_tok in ("702MW", "6.4 年", "20 年 PPA", "18 笔", "三成", "百分之三十", "翻倍", *expected_raws):
        assert forbidden_tok not in numbers_mod.STEP_B_LABEL_INSTRUCTIONS
        assert forbidden_tok not in schema_dump

    seen_sections: list[str] = []
    mock_assist = create_autospec(assist_json)

    async def _colloquial_side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            sec_title = str(payload.get("section_title") or "")
            seen_sections.append(sec_title)
            extra = (
                [
                    {
                        "raw": it["raw"],
                        "value": 50.0,
                        "unit": "pct",
                        "metric": "渠道比例",
                        "period": "FY2026",
                        "kind": "reported",
                        "line": it["line"],
                        "sentence": "管理层在电话会中提到上述口语化比例。",
                    }
                    for it in colloquial_gold["items"]
                ]
                if "渠道观察" in sec_title
                else []
            )
            return AssistResult(ok=True, data={"items": [], "extra_figures": extra})
        return AssistResult(ok=True, data={"items": []})

    mock_assist.side_effect = _colloquial_side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    col_res = await aaudit_draft(colloquial_draft, thesis_doc={"evidence": []}, use_llm_judge=True, user_id="local-dev-user")
    assert any("渠道观察" in s for s in seen_sections)
    extracted_raws = {u["raw"] for u in col_res.unverified}
    assert expected_raws.issubset(extracted_raws)

    # Test failed batch fallback AND cross-batch isolation (empty-figure batch B cannot overwrite failed batch A's F-001)
    draft_with_figs = (
        "## 合同期限与装机\n\n"
        "项目签署了 20 年 PPA 协议，加权剩余合同期限 6.4 年，装机容量 702MW。\n\n"
        "## 渠道观察\n\n"
        "本节没有阿拉伯数字。\n"
    )
    figs = extract_figures(draft_with_figs)
    assert len(figs) == 3
    figs[0].label_hint = "incidental"
    figs[0].incidental_category = "期间、日期和时间窗口表达里的数字"

    mock_mixed = create_autospec(assist_json)

    async def _mixed_side_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            sec = str(payload.get("section_title") or "")
            if "合同期限与装机" in sec:
                return AssistResult(ok=False, data=None, error="llm_unavailable", status="failed")
            # Empty-figure batch attempts to pollute F-001 from failed batch 1
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": "F-001",
                            "metric": "非财务",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "期间、日期和时间窗口表达里的数字",
                        }
                    ],
                    "extra_figures": [],
                },
            )
        return AssistResult(ok=False, data=None, error="llm_unavailable", status="failed")

    mock_mixed.side_effect = _mixed_side_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_mixed)
    monkeypatch.setattr(audit_mod, "assist_json", mock_mixed)

    stats: dict[str, Any] = {}
    labeled_figs, ok = await numbers_mod.label_figures_with_status(
        draft_with_figs,
        figs,
        user_id="local-dev-user",
        stats_out=stats,
    )
    assert ok is False
    assert stats["failed_batches"] == 1
    assert len(stats["failed_batch_details"]) == 1
    for lf in labeled_figs:
        assert lf.label_hint == "reported"
        assert lf.incidental_category == ""

    audit_fail_res = await aaudit_draft(draft_with_figs, thesis_doc={"evidence": []}, use_llm_judge=True, user_id="local-dev-user")
    assert audit_fail_res.labeling_incomplete is True
    assert len(audit_fail_res.incidental) == 0
    assert audit_fail_res.figures == 3


def test_p0j_j2_exempt_markers_single_source_and_ltm_period_normalization() -> None:
    """P0J.J2 (§2.4 #1 & #2):
    - EXEMPT_MARKERS is the single source of truth across numbers.py, audit.py judge prompt, and dalton-publish/SKILL.md.
    - normalize_period maps standalone '过去 12 个月', '近 12 个月', 'TTM', 'LTM' to 'LTM' without matching metric phrases.
    - FigureLabels schema constrains incidental_category to the 9 categories (plus empty string).
    """
    from src.dalton.publish.numbers import (
        EXEMPT_MARKERS,
        INCIDENTAL_CATEGORIES,
        FigureLabelItem,
        has_exempt_marker,
        normalize_period,
    )
    from src.dalton.settings import REPO_ROOT

    assert len(EXEMPT_MARKERS) == 15
    skill_md = (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-publish" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for marker in EXEMPT_MARKERS:
        if "阈值" not in marker or marker in skill_md:
            assert marker in skill_md, f"Marker {marker} missing from dalton-publish/SKILL.md"
        assert marker in audit_mod._EXEMPT_MARKERS_PROMPT
        assert has_exempt_marker(f"假设推演 {marker} 收入增长 15%")

    for ltm_expr in ("过去 12 个月", "过去12个月", "近 12 个月", "近12个月", "TTM", "LTM"):
        assert normalize_period(ltm_expr) == "LTM", f"Expected LTM for {ltm_expr!r}"
    assert normalize_period("过去 12 个月营业利润率") != "LTM"
    assert normalize_period("ULTIMATE") != "LTM"

    ltm_figs = extract_figures("过去 12 个月营业利润率达到 35.2%，TTM收入达 120 亿美元。")
    fig_margin = next(f for f in ltm_figs if f.raw == "35.2%")
    assert fig_margin.period == "LTM"
    fig_ttm_rev = next(f for f in ltm_figs if "120" in f.raw)
    assert fig_ttm_rev.period == "LTM"

    assert len(INCIDENTAL_CATEGORIES) == 9
    item_unknown = FigureLabelItem.model_validate(
        {"id": "F-001", "kind": "incidental", "incidental_category": "随便写的类别"}
    )
    assert item_unknown.incidental_category == ""


@pytest.mark.asyncio
async def test_p0j_j2_decimal_version_number_two_counterexamples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0J §8.1 #8 counterexamples:
    1) '45.5 亿美元' mislabeled as Category 5 ('软件和模型的版本号、硬件型号') with no source -> unverified (blocks publish).
    2) 'Sonnet 5.5' labeled as Category 5 with source present in evidence -> verified ('已核实') via Step D judge.
    """
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import numbers as numbers_mod

    # Counterexample 1: "45.5 亿美元" mislabeled as Category 5 -> must remain unverified (never uncertain)
    draft_fin = "## 财务摘要\n本季度公司实现收入 45.5 亿美元。\n"
    thesis_empty = {
        "schema_version": 2,
        "evidence": [
            {
                "id": "EV-FIN-001",
                "fact": "本季度公司实现收入稳步提升，版本号持续迭代。",
                "source": {"type": "filing", "identity": "10-Q", "tier": 1},
                "figures": [],
            }
        ],
    }
    mock_assist_fin = create_autospec(assist_json)
    judge_called_fin = False

    async def _fin_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        nonlocal judge_called_fin
        if task == "audit.label_figures":
            figs = payload.get("extracted_figures") or payload.get("figures") or []
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "metric": "版本号",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "软件和模型的版本号、硬件型号",
                        }
                        for f in figs
                    ],
                    "extra_figures": [],
                },
            )
        if task == "audit.judge":
            judge_called_fin = True
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "verdict": "unverified",
                            "quote_metric_raw": "",
                            "explanation": "no source for 45.5 bn revenue",
                        }
                        for f in payload.get("figures", [])
                    ]
                },
            )
        return AssistResult(ok=False, data=None, error="unexpected_task")

    mock_assist_fin.side_effect = _fin_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist_fin)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist_fin)

    res_fin = await aaudit_draft(draft_fin, thesis_doc=thesis_empty, use_llm_judge=True, user_id="local-dev-user")
    assert judge_called_fin is True
    assert len(res_fin.incidental) == 0
    assert len(res_fin.uncertain) == 0
    assert len(res_fin.unverified) == 1
    assert "45.5" in res_fin.unverified[0]["raw"]
    assert len(res_fin.unmatched) == 1

    # Counterexample 2: "Sonnet 5.5" labeled as Category 5 with source in evidence -> verified ("已核实")
    draft_ver = "## 产品进展\n本季度新发布的模型版本为 Sonnet 5.5。\n"
    thesis_with_source = {
        "schema_version": 2,
        "evidence": [
            {
                "id": "EV-MOD-001",
                "fact": "本季度新发布的模型版本为 Sonnet 5.5。",
                "source": {"type": "filing", "identity": "10-Q", "tier": 1},
                "figures": [],
            }
        ],
    }
    mock_assist_ver = create_autospec(assist_json)

    async def _ver_effect(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            figs = payload.get("extracted_figures") or payload.get("figures") or []
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "metric": "模型版本",
                            "period": "",
                            "kind": "incidental",
                            "incidental_category": "软件和模型的版本号、硬件型号",
                        }
                        for f in figs
                    ],
                    "extra_figures": [],
                },
            )
        if task == "audit.judge":
            return AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "verdict": "supported",
                            "matched_source_id": "evidence:EV-MOD-001",
                            "quote": "本季度新发布的模型版本为 Sonnet 5.5。",
                            "quote_number_raw": "5.5",
                            "quote_metric_raw": "模型版本",
                            "explanation": "evidence EV-MOD-001 states Sonnet 5.5",
                        }
                        for f in payload.get("figures", [])
                    ]
                },
            )
        return AssistResult(ok=False, data=None, error="unexpected_task")

    mock_assist_ver.side_effect = _ver_effect
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist_ver)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist_ver)

    res_ver = await aaudit_draft(draft_ver, thesis_doc=thesis_with_source, use_llm_judge=True, user_id="local-dev-user")
    assert res_ver.matched == 1
    assert res_ver.matches[0]["raw"] == "5.5"
    assert len(res_ver.uncertain) == 0
    assert len(res_ver.unverified) == 0


@pytest.mark.asyncio
async def test_p0j_j2_assist_fallback_unified_and_role_policy_family_check(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """P0J.J2 (§1 & §2.2):
    - assist_json only falls back on status=='failed' with error_kind in ('pre_send', 'http_4xx' excluding 429).
    - Never falls back on indeterminate/timeout, in_flight, conflict, or budget_exhausted.
    - Records the last attempted fallback model when all fallbacks fail.
    - role_policy checks assist_fallback against ALL reviewer_models and raises DaltonStartupConfigError on invalid YAML.
    """
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
    from src.dalton.llm.role_policy import (
        DaltonStartupConfigError,
        RolePolicyError,
        check_roles,
        enforce_role_policy_on_startup,
    )

    mock_complete = create_autospec(dalton_complete)
    calls: list[str] = []

    async def _complete_side_effect(**kwargs: Any) -> DaltonCompleteResult:
        req_model = str(kwargs.get("request_model") or "")
        calls.append(req_model)
        # The configured assist model (whatever agent_config.dalton.yaml names) fails pre-send;
        # only the fallback succeeds.
        if req_model != "deepseek-flash":
            return DaltonCompleteResult(
                ok=False,
                status="failed",
                error="primary_oauth_expired",
                error_kind="pre_send",
                model=req_model,
            )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data={"items": []},
            text='{"items": []}',
            model=req_model,
            cash_usd=0.001,
        )

    mock_complete.side_effect = _complete_side_effect
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete)

    schema = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}
    with caplog.at_level("INFO"):
        res = await assist_mod.assist_json("audit.label_figures", "Label figures", {"figures": []}, schema, user_id="local-dev-user")
    assert res.ok is True
    assert res.model == "deepseek-flash"
    # Every configured fallback before deepseek-flash fails pre-send too, so it is tried last.
    assert calls[0] != "deepseek-flash" and calls[-1] == "deepseek-flash"
    assert len(calls) == 1 + len(await assist_mod.resolve_assist_fallback_models())
    assert any("switching to assist_fallback=deepseek-flash" in r.message for r in caplog.records)

    # Non-fallbackable statuses/error_kinds MUST NOT trigger fallback
    for non_fb_status, non_fb_kind in [
        ("indeterminate", "timeout"),
        ("in_flight", "in_flight"),
        ("conflict", "conflict"),
        ("budget_exhausted", "budget_exhausted"),
        ("failed", "http_429"),
    ]:
        calls.clear()

        async def _no_fb_effect(
            _st: str = non_fb_status,
            _ek: str = non_fb_kind,
            **kwargs: Any,
        ) -> DaltonCompleteResult:
            req_m = str(kwargs.get("request_model") or "")
            calls.append(req_m)
            return DaltonCompleteResult(
                ok=False,
                status=_st,
                error=f"err_{_ek}",
                error_kind=_ek,
                model=req_m,
            )

        mock_complete.side_effect = _no_fb_effect
        res_no_fb = await assist_mod.assist_json("audit.label_figures", "Label figures", {"figures": []}, schema, user_id="local-dev-user")
        assert res_no_fb.ok is False
        assert len(calls) == 1 and calls[0] not in (await assist_mod.resolve_assist_fallback_models()), (
            f"Unexpected fallback for {non_fb_status}/{non_fb_kind}: {calls}"
        )

    # When primary and all fallbacks fail at pre_send, res.model records the last attempted fallback model
    calls.clear()

    async def _all_fail_effect(**kwargs: Any) -> DaltonCompleteResult:
        req_m = str(kwargs.get("request_model") or "")
        calls.append(req_m)
        return DaltonCompleteResult(
            ok=False,
            status="failed",
            error=f"pre_send_fail_{req_m}",
            error_kind="pre_send",
            model=req_m,
        )

    mock_complete.side_effect = _all_fail_effect
    res_all_fail = await assist_mod.assist_json("audit.label_figures", "Label figures", {"figures": []}, schema, user_id="local-dev-user")
    assert res_all_fail.ok is False
    fbs = await assist_mod.resolve_assist_fallback_models()
    assert calls[1:] == fbs and calls[0] not in fbs
    assert res_all_fail.model == fbs[-1]
    assert fbs[-1] in (res_all_fail.error or "")

    # Role policy: assist_fallback (from DALTON_ASSIST_FALLBACK_MODEL / dalton.yaml) compared against ALL reviewer_models
    base_role_cfg = {
        "llm": {
            "name": "claude-opus-5-5-oauth",
            "assist": "gemini-3.8-flash-low-oauth",
        }
    }
    monkeypatch.setenv("DALTON_ASSIST_FALLBACK_MODEL", "deepseek-flash")
    warnings = check_roles(
        base_role_cfg,
        reviewer_models=["gpt-6.1-sol-oauth", "deepseek-reasoner"],
    )
    assert any(w["code"] == "assist_fallback_same_family" for w in warnings)

    monkeypatch.setenv("DALTON_ASSIST_FALLBACK_MODEL", "gpt-6.1-sol-oauth")
    with pytest.raises(RolePolicyError, match="assist_fallback"):
        enforce_role_policy_on_startup(base_role_cfg)

    # Corrupt YAML in PTC_CONFIG_FILE must raise DaltonStartupConfigError (never silently return {})
    monkeypatch.delenv("DALTON_ASSIST_FALLBACK_MODEL", raising=False)
    bad_yaml = tmp_path / "bad_agent_config.yaml"
    bad_yaml.write_text(":::invalid_yaml\n  - [unclosed", encoding="utf-8")
    monkeypatch.setenv("PTC_CONFIG_FILE", str(bad_yaml))
    with pytest.raises(DaltonStartupConfigError):
        enforce_role_policy_on_startup()


@pytest.mark.asyncio
async def test_p0j_j2b_risk_and_disclaimer_section_sent_to_step_b_and_labeled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0J.J2b (§8.1 #16): Section titled '风险提示与免责' containing numbers must not be skipped."""
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish.numbers import (
        _split_markdown_sections_and_chunks,
        extract_figures,
        label_figures_with_status,
    )

    md = (
        "# META 研究报告\n\n"
        "## 1. 核心财务表现\n"
        "2Q26 广告营收达到 $59.4bn。\n\n"
        "## 风险提示与免责\n"
        "若欧盟监管罚款达到 $8.5bn，FY26 营业利润率将下降 12%。\n"
    )

    outline, chunks = _split_markdown_sections_and_chunks(md)
    section_titles = [sec_title for sec_title, _, _, _ in chunks]
    assert "风险提示与免责" in section_titles

    figs = extract_figures(md)
    risk_figs = [f for f in figs if f.location.get("section") == "风险提示与免责"]
    assert [f.raw for f in risk_figs] == ["$8.5bn", "12%"]

    seen_sections: list[str] = []

    async def fake_assist_json(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        sec = str(payload.get("section_title") or "")
        seen_sections.append(sec)
        items = []
        for pf in payload.get("extracted_figures") or []:
            raw = str(pf.get("raw") or "")
            metric_name = (
                "欧盟监管罚款"
                if "8.5" in raw
                else ("营业利润率变动" if "12%" in raw else "广告营收")
            )
            items.append(
                {
                    "id": pf["id"],
                    "kind": "estimate" if sec == "风险提示与免责" else "reported",
                    "incidental_category": "",
                    "metric": metric_name,
                    "period": "FY26" if sec == "风险提示与免责" else "2Q26",
                    "unit": str(pf.get("unit") or ""),
                }
            )
        return AssistResult(
            ok=True,
            status="ok",
            data={"items": items, "extra_figures": []},
            model="gemini-3.8-flash-low-oauth",
        )

    mock_assist = create_autospec(assist_json, side_effect=fake_assist_json)
    monkeypatch.setattr("src.dalton.publish.numbers.assist_json", mock_assist)
    stats: dict[str, Any] = {}
    labeled, ok = await label_figures_with_status(
        md,
        figs,
        user_id="local-dev-user",
        stats_out=stats,
    )
    assert ok is True
    assert "风险提示与免责" in seen_sections
    assert stats["total"] == 3
    assert stats["labeled"] == 3
    labeled_risk = [f for f in labeled if f.location.get("section") == "风险提示与免责"]
    assert len(labeled_risk) == 2
    assert all(f.label_hint == "estimate" for f in labeled_risk)
    assert {f.metric for f in labeled_risk} == {"欧盟监管罚款", "营业利润率变动"}


@pytest.mark.parametrize(
    ("line_text", "raw_sub", "expected_raw", "expected_pos"),
    [
        # "近一半" -> "近一半" (both when LLM extracted "一半" and when LLM already extracted "近一半")
        ("渠道调研显示，近一半的企业客户已把推理负载迁到自研芯片。", "一半", "近一半", 7),
        ("渠道调研显示，近一半的企业客户已把推理负载迁到自研芯片。", "近一半", "近一半", 7),
        # "超过三成" -> "超过三成"
        ("调查显示，超过三成的企业计划在明年扩大 AI 预算。", "三成", "超过三成", 5),
        ("调查显示，超过三成的企业计划在明年扩大 AI 预算。", "超过三成", "超过三成", 5),
        # "将近一半" longest match (must match "将近" rather than "近")
        ("渠道调研显示，将近一半的企业客户已把推理负载迁到自研芯片。", "一半", "将近一半", 7),
        ("渠道调研显示，将近一半的企业客户已把推理负载迁到自研芯片。", "近一半", "将近一半", 7),
        # Negated qualifiers matched as complete longest-first qualifiers (do not strip negation)
        ("调查显示，不超过三成的客户选择缩减预算。", "三成", "不超过三成", 5),
        ("调查显示，未超过三成的客户选择缩减预算。", "三成", "未超过三成", 5),
        ("调查显示，没超过三成的客户选择缩减预算。", "三成", "没超过三成", 5),
        ("调查显示，不超三成的客户选择缩减预算。", "三成", "不超三成", 5),
        ("调查显示，未超三成的客户选择缩减预算。", "三成", "未超三成", 5),
        ("调查显示，不能超过三成的客户选择缩减预算。", "三成", "不能超过三成", 5),
        ("调查显示，没有超过三成的客户选择缩减预算。", "三成", "没有超过三成", 5),
        ("调查显示，不逾三成的客户选择缩减预算。", "三成", "不逾三成", 5),
        ("调查显示，未逾三成的客户选择缩减预算。", "三成", "未逾三成", 5),
        # Compound words ending in single-char '近' / '约' / '超' must NOT expand
        ("最近一半的企业客户完成了迁移。", "一半", "一半", 2),
        ("附近一半的园区已接入新电网。", "一半", "一半", 2),
        ("新近一半的订单来自海外。", "一半", "一半", 2),
        ("靠近一半的节点处于满载状态。", "一半", "一半", 2),
        ("合约一半的金额将在今年确认。", "一半", "一半", 2),
        ("签约三成的客户选择了三年期合同。", "三成", "三成", 2),
        ("高超三成的对手仍未盈利。", "三成", "三成", 2),
        ("赶超三成的历史峰值。", "三成", "三成", 2),
        # No preceding qualifier -> unchanged
        ("大半的新签合同带有产能预留条款。", "大半", "大半", 0),
        ("一家头部客户把明年的 GPU 预算砍掉一半，转投自研加速器。", "砍掉一半", "砍掉一半", 17),
        ("渠道商预计明年 Trainium 出货量倍增。", "倍增", "倍增", 20),
        # Qualifier not immediately adjacent (separated by other characters) -> unchanged
        ("近两年有一半的企业客户已把推理负载迁到自研芯片。", "一半", "一半", 4),
        ("超过市场预期，三成客户选择续约。", "三成", "三成", 7),
        ("大约在明年年中，四成订单将交付。", "四成", "四成", 8),
    ],
)
def test_p0j_j5h_expand_colloquial_raw_with_qualifier(
    line_text: str, raw_sub: str, expected_raw: str, expected_pos: int
) -> None:
    """P0J.J5h (Part B): Direct unit tests for `_expand_colloquial_raw_with_qualifier`."""
    from src.dalton.publish.numbers import (
        COLLOQUIAL_APPROX_QUALIFIERS,
        SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING,
        _expand_colloquial_raw_with_qualifier,
    )

    assert callable(match_figures)
    required_qualifiers = {
        "近",
        "将近",
        "接近",
        "约",
        "大约",
        "约为",
        "超",
        "超过",
        "逾",
        "不到",
        "不足",
        "近乎",
        "几乎",
        "多达",
        "不超过",
        "未超过",
        "没超过",
        "不超",
        "未超",
        "不逾",
        "未逾",
    }
    assert required_qualifiers.issubset(set(COLLOQUIAL_APPROX_QUALIFIERS))
    assert list(COLLOQUIAL_APPROX_QUALIFIERS) == sorted(
        COLLOQUIAL_APPROX_QUALIFIERS, key=len, reverse=True
    )
    assert {"最", "附", "新", "靠", "邻", "贴", "逼", "亲", "就"}.issubset(
        SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING["近"]
    )
    assert {
        "合",
        "签",
        "制",
        "预",
        "公",
        "条",
        "节",
        "婚",
        "违",
        "践",
        "履",
        "契",
        "相",
        "邀",
        "隐",
    }.issubset(SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING["约"])
    assert {"高", "赶"}.issubset(SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING["超"])
    assert _expand_colloquial_raw_with_qualifier(line_text, raw_sub) == (
        expected_raw,
        expected_pos,
    )

    # Same line with two occurrences of '一半', where only the second has '近' immediately before it
    same_line = "上半年完成一半，下半年完成近一半。"
    first_raw, first_pos = _expand_colloquial_raw_with_qualifier(same_line, "一半", 0)
    assert (first_raw, first_pos) == ("一半", 5)
    second_raw, second_pos = _expand_colloquial_raw_with_qualifier(
        same_line, "一半", first_pos + len("一半")
    )
    assert (second_raw, second_pos) == ("近一半", 13)


@pytest.mark.asyncio
async def test_p0j_j5h_extra_figures_loop_expands_qualifier_preserving_value_and_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0J.J5h (Part B): `extra_figures` merge loop expands `raw` with adjacent qualifier while keeping `value` and `unit` unchanged, including multiple occurrences on the same line."""
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish.numbers import extract_figures, label_figures_with_status

    md = (
        "# 渠道观察\n\n"
        "- 渠道调研显示，近一半的企业客户已把推理负载迁到自研芯片。\n"
        "- 调查显示，超过三成的企业计划扩大预算，不超过三成的企业维持原状。\n"
        "- 上半年完成一半[^1]，下半年完成近一半[^2]。\n\n"
        "[^1]: https://example.com/h1\n"
        "[^2]: https://example.com/h2\n"
    )
    figs = extract_figures(md)
    assert figs == []

    async def fake_assist_json(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        return AssistResult(
            ok=True,
            status="ok",
            data={
                "items": [],
                "extra_figures": [
                    {
                        "raw": "一半",
                        "line": 3,
                        "value": 50.0,
                        "unit": "pct",
                        "metric": "",
                        "period": "",
                        "kind": "reported",
                    },
                    {
                        "raw": "三成",
                        "line": 4,
                        "value": 30.0,
                        "unit": "pct",
                        "metric": "",
                        "period": "",
                        "kind": "reported",
                    },
                    {
                        "raw": "三成",
                        "line": 4,
                        "value": 30.0,
                        "unit": "pct",
                        "metric": "",
                        "period": "",
                        "kind": "reported",
                    },
                    {
                        "raw": "一半",
                        "line": 5,
                        "value": 50.0,
                        "unit": "pct",
                        "metric": "",
                        "period": "",
                        "kind": "reported",
                    },
                    {
                        "raw": "一半",
                        "line": 5,
                        "value": 50.0,
                        "unit": "pct",
                        "metric": "",
                        "period": "",
                        "kind": "reported",
                    },
                ],
            },
            model="gemini-3.8-flash-low-oauth",
        )

    mock_assist = create_autospec(assist_json, side_effect=fake_assist_json)
    monkeypatch.setattr("src.dalton.publish.numbers.assist_json", mock_assist)
    labeled, ok = await label_figures_with_status(md, figs, user_id="local-dev-user")
    assert ok is True
    assert [(f.raw, f.value, f.unit, f.footnote_id) for f in labeled] == [
        ("近一半", 50.0, "pct", None),
        ("超过三成", 30.0, "pct", None),
        ("不超过三成", 30.0, "pct", None),
        ("一半", 50.0, "pct", "1"),
        ("近一半", 50.0, "pct", "2"),
    ]


@pytest.mark.asyncio
async def test_citation_table_lookup_missing_hint_and_multi_citations() -> None:
    """Verify [引用N] mapping via ## 引用 table, multi-citation [引用3][引用5], and missing/unrecognized citation hint."""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue reached 59.4 bn USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            },
            {
                "id": "E-20261023-02",
                "fact": "2Q26 Family of Apps operating income was 32.1 bn USD.",
                "figures": [
                    {
                        "metric": "营业利润",
                        "value": 32.1,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            },
        ]
    }
    models_doc = {
        "versions": [
            {
                "path": "model/META-model-v01.xlsx",
                "recalculated": True,
                "sha256": "abc123",
                "key_outputs": [
                    {
                        "name": "自由现金流",
                        "cell": "DCF!C12",
                        "value": 15.5,
                        "unit": "USD_bn",
                        "period": "2Q26",
                    }
                ],
            }
        ]
    }
    draft = (
        "# META 初次覆盖\n\n"
        "2Q26 广告收入为 594 亿美元（10-Q [引用1]）。\n"
        "2Q26 自由现金流为 155 亿美元 [引用2][引用3]。\n"
        "卖方预计 2Q26 资本开支为 180 亿美元（UBS [引用9]）。\n"
        "另有渠道调研预测 2Q26 收入为 610 亿美元（某券商 [引用10]）。\n\n"
        "## 引用清单\n"
        "[引用1] SEC 10-Q｜2Q26 季报\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
        "[引用2]: evidence:E-20261023-02\n"
        "[引用3]: model:model/META-model-v01.xlsx!DCF!C12\n"
        "[引用10]: 某未识别格式来源说明\n"
    )
    footnotes = parse_footnotes(draft)
    assert footnotes["引用1"] == "evidence:E-20261023-01"
    assert footnotes["引用3"] == "model:model/META-model-v01.xlsx!DCF!C12"

    res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res.matched == 2
    verified_raws = {m["raw"] for m in res.matches}
    assert "594 亿美元" in verified_raws
    assert "155 亿美元" in verified_raws

    unverified_by_raw = {u["raw"]: u for u in res.unverified}
    assert "180 亿美元" in unverified_by_raw
    assert unverified_by_raw["180 亿美元"]["reason"] == "unverified"
    assert unverified_by_raw["180 亿美元"]["hint"] == "[引用9] 在引用表中没有对应项"

    assert "610 亿美元" in unverified_by_raw
    assert unverified_by_raw["610 亿美元"]["reason"] == "unverified"
    assert unverified_by_raw["610 亿美元"]["hint"] == "[引用10] 在引用表中没有对应项"


@pytest.mark.asyncio
async def test_audit_summaries_separate_contradicted_and_lead_with_conflict_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify check_draft, publish_report, sidecar .audit.md, and review input separate 冲突 and lead with conflict IDs."""
    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc

    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD; operating income 24.3B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 24.3,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                ],
            }
        ]
    }
    draft = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n"
        "2Q26 营业利润为 290 亿美元 [引用1]。\n"
        "2Q26 资本开支为 180 亿美元（UBS [引用9]）。\n"
        "假设算力投入翻倍至 500 亿美元〔示意〕。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    audit = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit.matched == 1
    assert len(audit.contradicted) == 1
    assert len(audit.unverified) == 1
    assert audit.not_required_count == 1
    conflict_id = audit.contradicted[0]["id"]

    summary = audit.summary_line("drafts/meta.audit.md")
    assert summary.startswith(f"发现 1 处冲突（{conflict_id}）。")
    assert "已核实 1 / 冲突 1 / 未核实 1 / 不需核实 1" in summary

    audit_md = render_audit_md(audit, draft_path="drafts/meta.md")
    assert f"发现 1 处冲突（{conflict_id}）。" in audit_md.splitlines()[2]
    assert "已核实 1 / 冲突 1 / 未核实 1 / 不需核实 1" in audit_md

    bundle = ReviewBundle(
        title="META",
        kind="initial_coverage",
        draft_markdown=draft,
        audit_summary=audit.to_dict(),
    )
    rev_input = render_review_input(bundle)
    assert f"发现 1 处冲突（{conflict_id}）。" in rev_input
    assert "已核实 1 / 冲突 1 / 未核实 1 / 不需核实 1" in rev_input

    class _BackendProto:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...

    files_store: dict[str, str] = {"drafts/meta.md": draft}
    backend = create_autospec(_BackendProto, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={"thesis": {"content": thesis_doc, "version_no": 1}},
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(db_publications.resolve_thread_author_model, return_value="claude-opus-5-5-oauth"),
    )
    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(
            db_publications.get_or_create_request,
            return_value=({"publish_request_id": "req-1", "status": "auditing"}, True),
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(
            db_publications.transition,
            return_value={"publish_request_id": "req-1", "status": "audit_failed"},
        ),
    )
    monkeypatch.setattr(
        publish_svc,
        "aaudit_draft",
        create_autospec(publish_svc.aaudit_draft, return_value=audit),
    )

    pub_fail = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
    )
    assert pub_fail["status"] == "audit_failed"
    assert pub_fail["message"].startswith(f"发现 1 处冲突（{conflict_id}）。")
    assert "已核实 1 / 冲突 1 / 未核实 1 / 不需核实 1" in pub_fail["message"]


@pytest.mark.asyncio
async def test_audit_sidecar_reuse_and_invalidation_on_draft_or_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify check_draft reuses .audit.md when unchanged, and re-runs when draft or config changes."""
    from src.dalton import auth as dalton_auth
    from src.dalton.llm.assist import AssistResult, assist_json
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import service as publish_svc

    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "unit-test-dedicated-audit-cache-key")

    class _BackendProto:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...

    draft_v1 = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    files_store: dict[str, str] = {"drafts/meta.md": draft_v1}
    backend = create_autospec(_BackendProto, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )

    async def _fake_assist(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        return AssistResult(
            ok=True,
            status="ok",
            data={
                "items": [
                    {
                        "id": "B-001",
                        "kind": "reported",
                        "metric": "广告收入",
                        "period": "2Q26",
                        "unit": "USD_bn",
                    }
                ],
                "extra_figures": [],
            },
            model="gemini-3.8-flash-low-oauth",
        )

    mock_assist = create_autospec(assist_json, side_effect=_fake_assist)
    monkeypatch.setattr("src.dalton.publish.numbers.assist_json", mock_assist)
    monkeypatch.setattr("src.dalton.publish.audit.assist_json", mock_assist)

    # 1. Initial run: executes LLM and writes sidecar
    res1 = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res1["ok"] is True
    assert res1["reused"] is False
    assert res1["audit"]["reused"] is False
    assert mock_assist.call_count == 1

    # 2. Second run with identical draft, inputs, and config: reuses sidecar without calling LLM
    res2 = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res2["ok"] is True
    assert res2["reused"] is True
    assert res2["audit"]["reused"] is True
    assert mock_assist.call_count == 1

    # 3. Change 1 character in draft -> re-runs LLM
    files_store["drafts/meta.md"] = draft_v1.replace("META 报告", "META 报告！")
    res3 = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res3["reused"] is False
    assert res3["audit"]["reused"] is False
    assert mock_assist.call_count == 2

    # Same modified draft again -> reuses
    res3_reuse = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res3_reuse["reused"] is True
    assert mock_assist.call_count == 2

    # 4. Change config (prompt_version or audit_model) -> re-runs LLM
    assert res3["audit"]["prompt_version"] == "audit_v8"
    monkeypatch.setattr(audit_mod, "AUDIT_PROMPT_VERSION", "audit_v9")
    res4 = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res4["reused"] is False
    assert res4["audit"]["reused"] is False
    assert res4["audit"]["prompt_version"] == "audit_v9"
    assert mock_assist.call_count == 3



    # 5. The user switches to their own audit model (per-user preference, not
    # yaml): the sidecar written under the old effective model must not be
    # reused by run_check_draft.
    before = mock_assist.call_count
    real_resolve = audit_mod.resolve_dalton_audit_model

    async def _user_model(user_id: str | None = None) -> str:
        return "claude-opus-5-5-oauth"

    monkeypatch.setattr(
        audit_mod,
        "resolve_dalton_audit_model",
        create_autospec(real_resolve, side_effect=_user_model),
    )
    res_user = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/meta.md",
        backend=backend,
        thesis_doc=thesis_doc,
        models_doc={},
        provenance_records={},
        use_llm_judge=True,
    )
    assert res_user["reused"] is False
    assert mock_assist.call_count > before

@pytest.mark.asyncio
async def test_audit_cache_hmac_tamper_wrong_type_and_multiple_blocks_rerun(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify missing DALTON_AUDIT_CACHE_KEY disables reuse with warning, and with key set, tampered/invalid/multi-block caches rerun."""
    import logging
    from src.dalton.publish import audit as audit_mod
    from src.dalton.settings import get_dalton_audit_cache_hmac_key

    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "unit-test-dedicated-audit-cache-key")

    draft = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n"
        "2Q26 资本开支为 180 亿美元 [引用9]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    initial = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert initial.reused is False
    assert initial.matched == 1
    assert len(initial.unverified) == 1

    valid_md = render_audit_md(initial, draft_path="drafts/meta.md")
    reused = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=valid_md,
    )
    assert reused.reused is True

    # 0. When DALTON_AUDIT_CACHE_KEY is unset (even if DALTON_BRIDGE_TOKEN / INTERNAL_SERVICE_TOKEN exist),
    # cache is never reused and a warning is logged without leaking any secret value.
    monkeypatch.delenv("DALTON_AUDIT_CACHE_KEY", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "bridge-token-must-not-be-used")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-token-must-not-be-used")
    assert get_dalton_audit_cache_hmac_key() is None
    with caplog.at_level(logging.WARNING, logger="src.dalton.publish.audit"):
        caplog.clear()
        no_key_res = await aaudit_draft(
            draft,
            thesis_doc=thesis_doc,
            use_llm_judge=False,
            user_id="local-dev-user",
            previous_audit_md=valid_md,
        )
        assert no_key_res.reused is False
        assert any("DALTON_AUDIT_CACHE_KEY" in rec.message for rec in caplog.records)
        assert all("bridge-token-must-not-be-used" not in rec.message for rec in caplog.records)
        no_key_md = render_audit_md(no_key_res, draft_path="drafts/meta.md")
        assert "<!-- dalton:audit-cache" not in no_key_md

    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "unit-test-dedicated-audit-cache-key")

    # 1. Tamper a conclusion field (change unverified/unmatched to empty, matched to 2) -> HMAC fails & re-runs
    m = audit_mod._AUDIT_CACHE_RE.search(valid_md)
    assert m is not None
    tampered_obj = json.loads(m.group(1))
    tampered_obj["unverified"] = []
    tampered_obj["unmatched"] = []
    tampered_md = valid_md.replace(m.group(1), json.dumps(tampered_obj, ensure_ascii=False, sort_keys=True))
    res_tampered = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=tampered_md,
    )
    assert res_tampered.reused is False
    assert len(res_tampered.unverified) == 1

    # 2. Wrong field type (e.g. matched is a string or bool, or contradicted is not a list) -> re-runs without raising
    wrong_type_obj = json.loads(m.group(1))
    wrong_type_obj["matched"] = "1"
    wrong_type_md = valid_md.replace(m.group(1), json.dumps(wrong_type_obj, ensure_ascii=False))
    res_wrong_type = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=wrong_type_md,
    )
    assert res_wrong_type.reused is False

    bool_int_obj = json.loads(m.group(1))
    bool_int_obj["figures"] = True
    bool_int_md = valid_md.replace(m.group(1), json.dumps(bool_int_obj, ensure_ascii=False))
    res_bool_int = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=bool_int_md,
    )
    assert res_bool_int.reused is False

    # 3. Two cache blocks in the same sidecar -> treated as cache miss & re-runs
    two_blocks_md = valid_md + "\n" + valid_md[valid_md.index("<!-- dalton:audit-cache") :]
    res_two_blocks = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=two_blocks_md,
    )
    assert res_two_blocks.reused is False


@pytest.mark.asyncio
async def test_parse_footnotes_citation_table_scope_code_block_and_duplicate_definition() -> None:
    """Verify parse_footnotes only reads ## 引用 outside code/comments, and marks conflicting duplicate [引用N] as unverified."""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            },
            {
                "id": "E-20261023-02",
                "fact": "2Q26 ad revenue 59.4B USD (alt).",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            },
        ]
    }
    draft = (
        "# META 报告\n\n"
        "[引用8]: evidence:E-20261023-01\n\n"
        "```markdown\n"
        "## 引用\n"
        "[引用7]: evidence:E-20261023-01\n"
        "```\n\n"
        "<!--\n"
        "## 引用\n"
        "[引用6]: evidence:E-20261023-01\n"
        "-->\n\n"
        "2Q26 广告收入为 594 亿美元 [引用7]。\n"
        "2Q26 广告收入为 594 亿美元 [引用8]。\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "```markdown\n"
        "[引用7]: evidence:E-20261023-01\n"
        "```\n"
        "[引用1]: evidence:E-20261023-01\n"
        "[引用1]: evidence:E-20261023-02\n"
    )
    footnotes = parse_footnotes(draft)
    assert "引用7" not in footnotes
    assert "引用8" not in footnotes
    assert "引用6" not in footnotes

    res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res.matched == 0
    assert len(res.unverified) == 3
    hints = [u["hint"] for u in res.unverified]
    assert any("[引用7] 在引用表中没有对应项" in h for h in hints)
    assert any("[引用8] 在引用表中没有对应项" in h for h in hints)
    assert any("重复定义" in h and "[引用1]" in h for h in hints)


@pytest.mark.asyncio
async def test_missing_or_nonexistent_citation_locked_unverified_against_derived_and_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify figures with missing [引用N] or non-existent evidence/model target cannot be rescued by derived or LLM judge."""
    from src.dalton.llm.assist import AssistResult, assist_json

    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 revenue was 10B USD and operating profit was 2B USD (operating margin 20%).",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 10.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 2.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                ],
            }
        ]
    }
    draft = (
        "# META 报告\n\n"
        "2Q26 营业收入为 100 亿美元 [引用1]，2Q26 营业利润为 20 亿美元 [引用1]，2Q26 营业利润率为 20% [引用9]。\n"
        "此外 2Q26 营业利润率为 20% [引用2]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
        "[引用2]: evidence:E-DOES-NOT-EXIST\n"
    )

    async def _fake_assist(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            return AssistResult(
                ok=True,
                status="ok",
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "kind": "derived" if "%" in f["raw"] else "reported",
                            "metric": f["metric"],
                            "period": "2Q26",
                            "unit": f["unit"],
                        }
                        for f in payload.get("extracted_figures", [])
                    ],
                    "extra_figures": [],
                },
                model="gemini-3.8-flash-low-oauth",
            )
        return AssistResult(
            ok=True,
            status="ok",
            data={
                "items": [
                    {
                        "id": f["id"],
                        "verdict": "supported",
                        "matched_source_id": "evidence:E-20261023-01",
                        "quote": "2Q26 revenue was 100B USD and operating profit was 20B USD (operating margin 20%).",
                        "quote_number_raw": "20%",
                        "quote_metric_raw": "operating margin",
                        "explanation": "Matched",
                    }
                    for f in payload.get("figures", [])
                ]
            },
            model="gemini-3.8-flash-low-oauth",
        )

    mock_assist = create_autospec(assist_json, side_effect=_fake_assist)
    monkeypatch.setattr("src.dalton.publish.numbers.assist_json", mock_assist)
    monkeypatch.setattr("src.dalton.publish.audit.assist_json", mock_assist)

    res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=True,
        user_id="local-dev-user",
    )
    assert res.matched == 2
    assert len(res.unverified) == 2
    unverified_raws = [u["raw"] for u in res.unverified]
    assert unverified_raws == ["20%", "20%"]
    hints = [u["hint"] for u in res.unverified]
    assert any("[引用9] 在引用表中没有对应项" in h for h in hints)
    assert any("Evidence E-DOES-NOT-EXIST not found" in h for h in hints)


@pytest.mark.asyncio
async def test_audit_config_and_inputs_fingerprints_rerun_on_each_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify changing audit_timeout_s, audit_total_timeout_s, audit_concurrency, or metric_aliases triggers a re-run."""
    from src.dalton.settings import get_dalton_config

    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "unit-test-dedicated-audit-cache-key")

    draft = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    pub_cfg = get_dalton_config().publish

    res0 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    sidecar = render_audit_md(res0, draft_path="drafts/meta.md")

    res_hit = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=sidecar,
    )
    assert res_hit.reused is True

    # 1. audit_timeout_s change
    monkeypatch.setattr(pub_cfg, "audit_timeout_s", pub_cfg.audit_timeout_s + 10.0)
    res_t1 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=sidecar,
    )
    assert res_t1.reused is False
    sidecar = render_audit_md(res_t1, draft_path="drafts/meta.md")

    # 2. audit_total_timeout_s change
    monkeypatch.setattr(pub_cfg, "audit_total_timeout_s", pub_cfg.audit_total_timeout_s + 60.0)
    res_t2 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=sidecar,
    )
    assert res_t2.reused is False
    sidecar = render_audit_md(res_t2, draft_path="drafts/meta.md")

    # 3. audit_concurrency change
    monkeypatch.setattr(pub_cfg, "audit_concurrency", max(1, pub_cfg.audit_concurrency - 1))
    res_t3 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=sidecar,
    )
    assert res_t3.reused is False
    sidecar = render_audit_md(res_t3, draft_path="drafts/meta.md")

    # 4. metric_aliases change
    res_t4 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        metric_aliases={"ad_revenue": ["核心广告营收"]},
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=sidecar,
    )
    assert res_t4.reused is False


@pytest.mark.asyncio
async def test_publish_retry_early_return_reenters_auditing_on_input_or_config_change_and_unifies_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify publish_report early-return checks input/config fingerprints and all exit points use unified summary."""
    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.files import compute_bundle_sha256

    draft = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n"
        "2Q26 营业利润为 290 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    thesis_v1 = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD; operating income 24.3B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 24.3,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                ],
            }
        ]
    }
    from src.dalton.publish.audit import compute_audit_config_fingerprint

    audit_fail = await aaudit_draft(
        draft,
        thesis_doc=thesis_v1,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    _, _, _, publish_cfg_sha = compute_audit_config_fingerprint(use_llm_judge=True)
    audit_fail.config_sha256 = publish_cfg_sha
    assert not audit_fail.passed
    bundle_sha = compute_bundle_sha256(draft.encode("utf-8"), [])

    class _BackendProto:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...

    files_store: dict[str, str] = {"drafts/meta.md": draft}
    backend = create_autospec(_BackendProto, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(db_publications.resolve_thread_author_model, return_value="claude-opus-5-5-oauth"),
    )

    # 1. Existing audit_failed request with identical bundle_sha256, inputs_sha256, config_sha256 -> early return with unified summary
    existing_row = {
        "publish_request_id": "req-1",
        "workspace_id": "ws-1",
        "user_id": "local-dev-user",
        "status": "audit_failed",
        "bundle_sha256": bundle_sha,
        "audit": audit_fail.to_dict(),
    }
    mock_list_latest = create_autospec(
        db_knowledge.list_all_latest_for_workspace,
        return_value={"thesis": {"content": thesis_v1, "version_no": 1}},
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        mock_list_latest,
    )
    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(db_publications.get_or_create_request, return_value=(existing_row, False)),
    )
    mock_transition = create_autospec(
        db_publications.transition,
        return_value={**existing_row, "status": "auditing"},
    )
    monkeypatch.setattr(db_publications, "transition", mock_transition)

    early_res = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
    )
    assert early_res["status"] == "audit_failed"
    assert mock_transition.call_count == 0
    conflict_id = audit_fail.contradicted[0]["id"]
    assert early_res["message"].startswith(f"发现 1 处冲突（{conflict_id}）。")
    assert "已核实 1 / 冲突 1 / 未核实 0 / 不需核实 0" in early_res["message"]

    # 2. Update thesis_doc (inputs_sha256 changes while draft bundle_sha256 is unchanged) -> transitions back to auditing
    import copy

    thesis_v2 = copy.deepcopy(thesis_v1)
    thesis_v2["evidence"][0]["figures"][1]["value"] = 29.0
    audit_pass = await aaudit_draft(
        draft,
        thesis_doc=thesis_v2,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_pass.passed
    mock_list_latest.return_value = {"thesis": {"content": thesis_v2, "version_no": 2}}
    monkeypatch.setattr(
        publish_svc,
        "aaudit_draft",
        create_autospec(publish_svc.aaudit_draft, return_value=audit_pass),
    )

    async def _transition_side_effect(
        publish_request_id: str,
        from_statuses: Any,
        to: str,
        **cols: Any,
    ) -> dict[str, Any]:
        existing_row["status"] = to
        existing_row.update(cols)
        return dict(existing_row)

    mock_transition.side_effect = _transition_side_effect
    monkeypatch.setattr(
        publish_svc,
        "_append_agenda_pm_handoff",
        create_autospec(publish_svc._append_agenda_pm_handoff, return_value=None),
    )
    from src.dalton.publish.review import ReviewResult, ReviewVerdict

    monkeypatch.setattr(
        publish_svc,
        "run_review",
        create_autospec(
            publish_svc.run_review,
            return_value=ReviewResult(
                status="done",
                model="gpt-6.1-sol-oauth",
                family="openai-gpt",
                verdict=ReviewVerdict(verdict="pass", summary="ok", items=[], must_fix=[], nice_to_have=[]),
            ),
        ),
    )

    unattended_res = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        origin_type="automation",
        backend=backend,
    )
    assert unattended_res["status"] == "awaiting_pm"
    assert mock_transition.call_count >= 1
    assert "已核实 2 / 冲突 0 / 未核实 0 / 不需核实 0" in unattended_res["message"]

    # 3. Verify denominator consistency when incidental and uncertain items exist
    from src.dalton.publish.audit import AuditResult, format_audit_summary_from_dict

    aud_mixed = AuditResult(
        draft_sha256="a" * 64,
        figures=7,
        matched=2,
        exempt=1,
        contradicted=[{"id": "F-003", "raw": "10%", "line": 3, "reason": "source_conflict"}],
        unverified=[{"id": "F-004", "raw": "20%", "line": 4, "reason": "unverified"}],
        uncertain=[{"id": "F-005", "raw": "3.5", "line": 5, "reason": "uncertain"}],
        incidental=[{"id": "F-006", "raw": "100", "line": 6, "incidental_category": "历史背景"}],
        unmatched=[
            {"id": "F-003", "raw": "10%", "line": 3, "reason": "source_conflict"},
            {"id": "F-004", "raw": "20%", "line": 4, "reason": "unverified"},
        ],
    )
    s_mixed = format_audit_summary_from_dict(aud_mixed.to_dict())
    assert s_mixed.startswith("发现 1 处冲突（F-003）。")
    assert "共 7 个数字：已核实 2 / 冲突 1 / 未核实 1 / 不需核实 2（另有未能判断 1，不计入以上四项）" in s_mixed


@pytest.mark.asyncio
async def test_multi_citation_with_missing_or_broken_target_locks_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a figure with multiple citations ([引用1][引用9] where 1 is valid and 9 is missing/broken) is locked unverified."""
    from src.dalton.llm.assist import AssistResult, assist_json

    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 revenue was 10B USD and operating profit was 2B USD (operating margin 20%).",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 10.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 2.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                ],
            },
            {
                "id": "E-20261023-02",
                "fact": "2Q26 capex was 1.5B USD.",
                "figures": [
                    {
                        "metric": "资本开支",
                        "value": 1.5,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            },
        ]
    }
    models_doc = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/meta_v1.xlsx",
                "sha256": "abc12345",
                "recalculated": True,
                "key_outputs": [
                    {"name": "目标价", "cell": "DCF!C12", "value": 850.0, "unit": "USD", "period": "FY2026"}
                ],
            }
        ],
    }
    draft = (
        "# META 报告\n\n"
        "2Q26 营业收入为 100 亿美元 [引用1]，2Q26 营业利润为 20 亿美元 [引用1]。\n"
        "2Q26 营业利润率为 20% [引用1][引用9]。\n"
        "2Q26 营业收入为 100 亿美元 [引用1][引用2]。\n"
        "FY26 目标价为 $850 [引用3][引用9]。\n"
        "3Q26 资本开支为 15 亿美元 [引用4][引用2]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
        "[引用2]: evidence:E-DOES-NOT-EXIST\n"
        "[引用3]: model:model/meta_v1.xlsx#DCF!C12\n"
        "[引用4]: evidence:E-20261023-02\n"
    )

    async def _fake_assist(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            return AssistResult(
                ok=True,
                status="ok",
                data={
                    "items": [
                        {
                            "id": f["id"],
                            "kind": "derived" if "%" in f["raw"] else "reported",
                            "metric": f["metric"],
                            "period": f["period"] or "2Q26",
                            "unit": f["unit"],
                        }
                        for f in payload.get("extracted_figures", [])
                    ],
                    "extra_figures": [],
                },
                model="gemini-3.8-flash-low-oauth",
            )
        return AssistResult(
            ok=True,
            status="ok",
            data={
                "items": [
                    {
                        "id": f["id"],
                        "verdict": "supported",
                        "matched_source_id": "evidence:E-20261023-01",
                        "quote": "2Q26 revenue was 100B USD and operating profit was 20B USD (operating margin 20%).",
                        "quote_number_raw": f["raw"],
                        "quote_metric_raw": "operating margin",
                        "explanation": "Matched",
                    }
                    for f in payload.get("figures", [])
                ]
            },
            model="gemini-3.8-flash-low-oauth",
        )

    mock_assist = create_autospec(assist_json, side_effect=_fake_assist)
    monkeypatch.setattr("src.dalton.publish.numbers.assist_json", mock_assist)
    monkeypatch.setattr("src.dalton.publish.audit.assist_json", mock_assist)

    res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        use_llm_judge=True,
        user_id="local-dev-user",
    )
    # Only the first two single-citation [引用1] figures match; all 4 multi-citation figures with a broken ref stay unverified
    assert res.matched == 2
    assert len(res.unverified) == 4
    unverified_raws = [u["raw"] for u in res.unverified]
    assert unverified_raws == ["20%", "100 亿美元", "$850", "15 亿美元"]
    assert "[引用9] 在引用表中没有对应项" in res.unverified[0]["hint"]
    assert "Evidence E-DOES-NOT-EXIST not found" in res.unverified[1]["hint"]
    assert "[引用9] 在引用表中没有对应项" in res.unverified[2]["hint"]
    assert "Evidence E-DOES-NOT-EXIST not found" in res.unverified[3]["hint"]


@pytest.mark.asyncio
async def test_citation_and_traditional_footnote_separate_namespaces() -> None:
    """Verify traditional footnote [^引用1]: cannot resolve [引用1] in prose, and [引用1]: cannot resolve [^引用1]."""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    # Case 1a: Prose uses [引用1], citation table has [引用2], and [^引用1] is a traditional footnote:
    # [^引用1] cannot resolve [引用1], and since [引用2] is present the table exists so [引用1] is reported missing.
    draft_trad_def = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[^引用1]: evidence:E-20261023-01\n"
        "[引用2]: evidence:E-20261023-01\n"
    )
    fn_map = parse_footnotes(draft_trad_def)
    assert "引用1" not in fn_map
    res1 = await aaudit_draft(
        draft_trad_def,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res1.matched == 0
    assert len(res1.unverified) == 1
    assert res1.unverified[0]["hint"] == "[引用1] 在引用表中没有对应项"

    # Case 1b: ## 引用 has ONLY [^引用1] and no [引用N] definitions -> treated as missing citation table
    draft_only_trad = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[^引用1]: evidence:E-20261023-01\n"
    )
    res1b = await aaudit_draft(
        draft_only_trad,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res1b.matched == 0
    assert res1b.missing_citation_table_count == 1
    assert len(res1b.unverified) == 1
    assert res1b.unverified[0]["hint"] == "无引用，未在来源中找到"

    # Case 2: Prose uses [^引用1], but definition is citation table [引用1]:
    draft_cit_def = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元[^引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    res2 = await aaudit_draft(
        draft_cit_def,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res2.matched == 0
    assert len(res2.unverified) == 1


@pytest.mark.asyncio
async def test_publish_remaining_exit_points_include_unified_audit_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify review_unavailable, review_revise, and PM rejected exit points all include the 4-bucket audit summary."""
    # The rejected branch is PM-only; pin the PM list so the host's
    # DALTON_PM_USER_IDS cannot turn it into awaiting_pm.
    monkeypatch.setenv("DALTON_PM_USER_IDS", "local-dev-user")
    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.files import compute_bundle_sha256
    from src.dalton.publish.review import ReviewResult, ReviewVerdict

    draft = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "2Q26 ad revenue 59.4B USD.",
                "figures": [
                    {
                        "metric": "广告收入",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    audit_ok = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_ok.passed
    bundle_sha = compute_bundle_sha256(draft.encode("utf-8"), [])

    class _BackendProto:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...

    files_store: dict[str, str] = {"drafts/meta.md": draft}
    backend = create_autospec(_BackendProto, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={"thesis": {"content": thesis_doc, "version_no": 1}},
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(db_publications.resolve_thread_author_model, return_value="claude-opus-5-5-oauth"),
    )
    monkeypatch.setattr(
        publish_svc,
        "_append_agenda_pm_handoff",
        create_autospec(publish_svc._append_agenda_pm_handoff, return_value=None),
    )

    state_row: dict[str, Any] = {
        "publish_request_id": "req-exit",
        "workspace_id": "ws-1",
        "user_id": "local-dev-user",
        "status": "reviewing",
        "bundle_sha256": bundle_sha,
        "audit": audit_ok.to_dict(),
    }

    async def _transition_side_effect(
        publish_request_id: str,
        from_statuses: Any,
        to: str,
        **cols: Any,
    ) -> dict[str, Any]:
        state_row["status"] = to
        state_row.update(cols)
        return dict(state_row)

    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(db_publications.get_or_create_request, side_effect=lambda **kw: (dict(state_row), False)),
    )
    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(db_publications.transition, side_effect=_transition_side_effect),
    )

    # 1. review_unavailable exit point
    mock_run_review = create_autospec(
        publish_svc.run_review,
        return_value=ReviewResult(status="unavailable", reason="no reviewer credentials"),
    )
    monkeypatch.setattr(publish_svc, "run_review", mock_run_review)
    res_unavail = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
    )
    assert res_unavail["status"] == "review_unavailable"
    assert "已核实 1 / 冲突 0 / 未核实 0 / 不需核实 0" in res_unavail["message"]

    # 2. review_revise exit point
    state_row["status"] = "reviewing"
    mock_run_review.return_value = ReviewResult(
        status="done",
        model="gpt-6.1-sol-oauth",
        family="openai-gpt",
        verdict=ReviewVerdict(
            verdict="revise",
            summary="need fix",
            items=[],
            must_fix=["补充风险章节"],
            nice_to_have=[],
        ),
    )
    res_revise = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        backend=backend,
    )
    assert res_revise["status"] == "review_revise"
    assert "已核实 1 / 冲突 0 / 未核实 0 / 不需核实 0" in res_revise["message"]

    # 3. PM rejected exit point
    state_row["status"] = "awaiting_pm"
    res_rejected = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/meta.md",
        title="META",
        kind="initial_coverage",
        slug="meta",
        origin_type="user",
        backend=backend,
        interrupt_fn=lambda _payload: {"decisions": [{"type": "reject", "message": "暂缓发布"}]},
    )
    assert res_rejected["status"] == "rejected"
    assert "已核实 1 / 冲突 0 / 未核实 0 / 不需核实 0" in res_rejected["message"]
    assert "PM 拒绝发布：暂缓发布" in res_rejected["message"]










@pytest.mark.asyncio
async def test_audit_config_fingerprint_follows_the_users_resolved_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache reuse keys on the per-user audit model/fallbacks/timeout, not the
    raw yaml: changing a user's audit model must invalidate the cache."""
    from src.dalton.publish import audit as audit_mod

    models = {"model": "gemini-4-argon-medium-oauth"}

    async def _model(user_id: str | None = None) -> str:
        return models["model"]

    async def _fallbacks(user_id: str | None = None) -> list[str]:
        return ["gpt-6.1-sol-oauth"]

    async def _timeout(user_id: str | None = None) -> int:
        return 300

    monkeypatch.setattr(audit_mod, "resolve_dalton_audit_model", create_autospec(audit_mod.resolve_dalton_audit_model, side_effect=_model))
    monkeypatch.setattr(
        audit_mod,
        "resolve_dalton_audit_fallback_models",
        create_autospec(audit_mod.resolve_dalton_audit_fallback_models, side_effect=_fallbacks),
    )
    monkeypatch.setattr(
        audit_mod,
        "resolve_dalton_audit_timeout_s",
        create_autospec(audit_mod.resolve_dalton_audit_timeout_s, side_effect=_timeout),
    )

    first = await audit_mod.resolve_audit_config_fingerprint("u1")
    models["model"] = "claude-opus-5-5-oauth"
    second = await audit_mod.resolve_audit_config_fingerprint("u1")

    assert first[0] == "gemini-4-argon-medium-oauth"
    assert second[0] == "claude-opus-5-5-oauth"
    assert first[3] != second[3]


@pytest.mark.asyncio
async def test_inspect_text_source_unstructured_fact_false_positives_and_true_conflict() -> None:
    """When evidence has no structured figures[] and falls back to unstructured fact text,
    missing metrics must be classified as 'unverified' (not 'source_conflict'); only same-metric
    same-unit value disagreements are 'source_conflict'."""
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-06",
                "period": "FY2027",
                "source": {
                    "type": "model",
                    "identity": "implied_model.py",
                    "tier": 2,
                    "ref": {"kind": "file", "path": "research/implied_model.py"},
                },
                "fact": (
                    "model-derived（implied_model.py；123.09 亿稀释股，税率 17.15%，其他收益 0）："
                    "FY2027 隐含税前利润 2,246 亿美元、隐含总费用 4,022 亿美元、隐含营业利润率 35.8%；"
                    "费用每多 100 亿美元 EPS −0.67 美元；收入每多 1% 且不增成本 EPS +0.42 美元"
                ),
            },
            {
                "id": "E-20261023-02",
                "period": "2Q26",
                "source": {
                    "type": "filing",
                    "identity": "Alphabet 2Q26 10-Q",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-Q", "period": "2Q26"},
                },
                "fact": (
                    "2Q26 收入 1,197.96 亿美元、同比 +24.2%；Services 收入 945.40 亿美元、利润率 41.8%；"
                    "Cloud 247.68 亿美元、同比 +81.8%、利润率 35.6%。"
                    "Cloud 剩余履约义务截至 2026-06-30 为 5,139 亿美元（总 RPO 5,195 亿美元）"
                ),
            },
            {
                "id": "E-20261023-01",
                "period": "FY2025",
                "source": {
                    "type": "filing",
                    "identity": "Alphabet FY2025 10-K",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-K", "period": "FY2025"},
                },
                "fact": (
                    "FY2025 收入 4,028.36 亿美元；Google 广告 2,946.91 亿美元，占收入 73.2%。"
                    "Google Services 收入 3,427.21 亿美元、分部营业利润率 40.7%；"
                    "Google Cloud 收入 587.05 亿美元、同比 +35.8%、分部营业利润率 23.7%"
                ),
            },
        ]
    }

    # False-positive example 1: FY2027 EPS 均值 15.12 美元 vs E-20261023-06 ("费用每多 100 亿美元 EPS −0.67 美元")
    # False-positive example 2: 2Q26 Cloud 营业利润 88.2 亿美元 vs E-20261023-02 (only has Cloud revenue 247.68 亿美元 and Cloud RPO 5,139 亿美元)
    # Plus positive matches for 2Q26 Cloud 剩余履约义务 5,139 亿美元 and FY2025 Google Cloud 分部营业利润率 23.7%
    draft = """# GOOGL 初筛数字核验测试

一致预期 FY2027 EPS 均值 15.12 美元[^e6]。
2Q26 Cloud 剩余履约义务截至期末达到 5,139 亿美元[^e2]，Cloud 营业利润为 88.2 亿美元[^e2]。
FY2025 Google Cloud 分部营业利润率为 23.7%[^e1]。

[^e6]: evidence:E-20261023-06
[^e2]: evidence:E-20261023-02
[^e1]: evidence:E-20261023-01
"""
    res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res.contradicted) == 0, f"Expected 0 false-positive conflicts, got: {res.contradicted}"
    reasons_by_raw = {u["raw"]: u["reason"] for u in res.unmatched}
    assert reasons_by_raw.get("15.12 美元") == "unverified"
    assert reasons_by_raw.get("88.2 亿美元") == "unverified"
    matched_raws = {m["raw"] for m in res.matches}
    assert "5,139 亿美元" in matched_raws
    assert "23.7%" in matched_raws

    # True conflict: when the exact same metric and unit ARE present in fact text and the value disagrees
    conflict_draft = """# GOOGL 真实冲突测试

2Q26 Cloud 剩余履约义务为 5,000 亿美元[^e2]。

[^e2]: evidence:E-20261023-02
"""
    res_conflict = await aaudit_draft(
        conflict_draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res_conflict.contradicted) == 1
    assert res_conflict.contradicted[0]["raw"] == "5,000 亿美元"
    assert res_conflict.contradicted[0]["reason"] == "source_conflict"

    # Case 1 (Round 2 repro): arbitrary English verbs ("was" vs "reached") must NOT be treated as segments;
    # FY2025 Revenue was 100.0 billion USD vs FY2025 Revenue reached 120.0 billion USD must report source_conflict.
    repro_thesis = {
        "evidence": [
            {
                "id": "E-20261023-10",
                "source": {
                    "identity": "FY2025 10-K",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-K", "period": "FY2025"},
                },
                "fact": "FY2025 Revenue reached 120.0 billion USD.",
            },
            {
                "id": "E-20261023-11",
                "source": {
                    "identity": "FY2025 10-K",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-K", "period": "FY2025"},
                },
                "fact": "FY2025 Google Cloud operating margin 23.7%.",
            },
        ]
    }
    repro_draft = """# Repro test

FY2025 Revenue was 100.0 billion USD[^e10].

[^e10]: evidence:E-20261023-10
"""
    res_repro = await aaudit_draft(
        repro_draft,
        thesis_doc=repro_thesis,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res_repro.contradicted) == 1, f"Expected source_conflict for Revenue was vs reached, got: {res_repro.unmatched}"
    assert res_repro.contradicted[0]["reason"] == "source_conflict"

    # Case 3 (Round 2): explicitly different segments ("Google Services operating margin 40.7%"
    # vs "Google Cloud operating margin 23.7%") must NOT report source_conflict.
    seg_draft = """# Segment test

FY2025 Google Services operating margin 40.7%[^e11].

[^e11]: evidence:E-20261023-11
"""
    res_seg = await aaudit_draft(
        seg_draft,
        thesis_doc=repro_thesis,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res_seg.contradicted) == 0, f"Expected 0 conflicts across different segments, got: {res_seg.contradicted}"
    assert res_seg.unmatched[0]["reason"] == "unverified"

    # Case 4 (Round 3): capitalized sentence starters ("Our" vs "The") must NOT be treated as segments;
    # "Our revenue was 100.0 billion USD" vs "The revenue reached 120.0 billion USD" must report source_conflict.
    r3_thesis = {
        "evidence": [
            {
                "id": "E-20261023-12",
                "source": {
                    "identity": "FY2025 10-K",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-K", "period": "FY2025"},
                },
                "fact": "FY2025 The revenue reached 120.0 billion USD.",
            },
            {
                "id": "E-20261023-13",
                "source": {
                    "identity": "FY2025 10-K",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-K", "period": "FY2025"},
                },
                "fact": "FY2025 Cloud revenue reached 58.7 billion USD.",
            },
        ]
    }
    our_vs_the_draft = """# Our vs The test

FY2025 Our revenue was 100.0 billion USD[^e12].

[^e12]: evidence:E-20261023-12
"""
    res_our_the = await aaudit_draft(
        our_vs_the_draft,
        thesis_doc=r3_thesis,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res_our_the.contradicted) == 1, f"Expected source_conflict for Our vs The revenue, got: {res_our_the.unmatched}"
    assert res_our_the.contradicted[0]["reason"] == "source_conflict"

    # Case 5 (Round 3): full name vs short name of the same segment ("Google Cloud revenue" vs "Cloud revenue")
    # map to the same canonical segment id ("cloud") and must report source_conflict when values differ.
    same_seg_draft = """# Same canonical segment test

FY2025 Google Cloud revenue was 50.0 billion USD[^e13].

[^e13]: evidence:E-20261023-13
"""
    res_same_seg = await aaudit_draft(
        same_seg_draft,
        thesis_doc=r3_thesis,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert len(res_same_seg.contradicted) == 1, f"Expected source_conflict for Google Cloud vs Cloud revenue, got: {res_same_seg.unmatched}"
    assert res_same_seg.contradicted[0]["reason"] == "source_conflict"


@pytest.mark.asyncio
async def test_audit_cache_miss_when_prompt_version_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached .audit.md written under audit_v1 is not reused when AUDIT_PROMPT_VERSION is audit_v2."""
    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "unit-test-dedicated-audit-cache-key")
    draft = (
        "# GOOGL 报告\n\n"
        "FY2025 Google Cloud 收入为 587 亿美元[^1]。\n\n"
        "[^1]: evidence:E-20261023-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "FY2025 Google Cloud revenue 58.7B USD.",
                "figures": [
                    {
                        "metric": "Google Cloud 收入",
                        "value": 58.7,
                        "unit": "USD_bn",
                        "period": "FY2025",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    res_v1 = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        prompt_version="audit_v1",
    )
    assert res_v1.reused is False
    assert res_v1.prompt_version == "audit_v1"
    v1_sidecar_md = render_audit_md(res_v1, draft_path="drafts/googl.md")

    # Same version ("audit_v1") hits cache
    res_v1_hit = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=v1_sidecar_md,
        prompt_version="audit_v1",
    )
    assert res_v1_hit.reused is True

    # Default version ("audit_v8") misses v1 cache and recomputes
    res_v2_miss = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=False,
        user_id="local-dev-user",
        previous_audit_md=v1_sidecar_md,
    )
    assert res_v2_miss.reused is False
    assert res_v2_miss.prompt_version == "audit_v8"


def test_thousands_separator_and_pause_comma_metric_labels() -> None:
    """Metric clause extraction splits on `、`, preserves thousands commas (`1,234`), and strips numbers."""
    md = "FY2025 收入 1,234 亿、利润 567 亿。\n"
    figs = extract_figures(md)
    by_raw = {f.raw: f for f in figs}
    assert "1,234 亿" in by_raw
    assert "567 亿" in by_raw

    rev_fig = by_raw["1,234 亿"]
    prof_fig = by_raw["567 亿"]
    assert "收入" in rev_fig.metric
    assert "利润" not in rev_fig.metric
    assert "234" not in rev_fig.metric
    assert "1,234" not in rev_fig.metric

    assert "利润" in prof_fig.metric
    assert "收入" not in prof_fig.metric
    assert "567" not in prof_fig.metric


def test_citation_binding_same_sentence_pre_and_post_without_crossing_sentences() -> None:
    """Citations bind within the same sentence (preferring post-number, falling back to pre-number) and never cross sentences."""
    md = (
        "# 1. 业务拆解\n"
        "[引用3] FY2025 Cloud 收入 247.68 亿美元。\n"
        "FY2025 YouTube 广告收入 361.47 亿美元[引用1]。FY2025 其他业务亏损 44.50 亿美元。\n"
        "前句有引用[引用2]。FY2025 研发费用 493.26 亿美元，后句没有引用。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261023-01\n"
        "[引用2]: evidence:E-20261023-02\n"
        "[引用3]: evidence:E-20261023-03\n"
    )
    figs = extract_figures(md)
    by_val = {round(f.value, 2): f for f in figs}

    # Pre-positioned citation in the same sentence binds when no post-number citation exists
    assert by_val[247.68].footnote_id == "引用3"
    assert by_val[247.68].footnote_target == "evidence:E-20261023-03"

    # Post-number citation in the same sentence binds normally
    assert by_val[361.47].footnote_id == "引用1"
    assert by_val[361.47].footnote_target == "evidence:E-20261023-01"

    # Next sentence on the same line does not inherit the previous sentence's citation
    assert by_val[44.50].footnote_id is None
    assert by_val[44.50].footnote_target is None

    # Sentence after `[引用2]。` does not cross the sentence boundary backwards
    assert by_val[493.26].footnote_id is None
    assert by_val[493.26].footnote_target is None


def test_short_english_alias_whole_word_matching_and_consensus_target_price() -> None:
    """Short English aliases (`tp`, `pe`, `irr`, `dap`, `fcf`) match whole words only; consensus target price does not require model cells."""
    from src.dalton.publish.match import canonical_metric_group, metrics_match

    assert canonical_metric_group("TPU v5p 算力规模") != "target_price"
    assert not metrics_match("TPU v5p 出货量", "目标价")
    assert not metrics_match("TPU", "tp")
    assert not metrics_match("supercomputer", "pe")
    assert canonical_metric_group("目标价 (TP)") == "target_price"
    assert canonical_metric_group("2026E PE 倍数") == "pe"
    assert canonical_metric_group("Custom Supercomputer (uper)") != "pe"

    # TPU figure with evidence footnote must NOT be forced into model-cell-only rule
    md = (
        "# 1. 算力与估值\n"
        "FY2025 TPU 部署量达到 200 万颗[引用1]。\n"
        "卖方一致预期目标价为 220 美元[引用2]。\n"
        "本报告目标价为 235 美元[引用3]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-TPU\n"
        "[引用2]: evidence:E-CONS\n"
        "[引用3]: evidence:E-HOUSE\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-TPU",
                "fact": "FY2025 TPU deployment reached 2.0 million units.",
                "figures": [
                    {
                        "metric": "TPU 部署量",
                        "value": 2.0,
                        "unit": "count_mn",
                        "period": "FY2025",
                        "label": "reported",
                    }
                ],
            },
            {
                "id": "E-CONS",
                "fact": "Street consensus target price is $220 per share.",
                "figures": [
                    {
                        "metric": "卖方一致预期目标价",
                        "value": 220.0,
                        "unit": "per_share",
                        "period": "",
                        "label": "consensus",
                    }
                ],
            },
            {
                "id": "E-HOUSE",
                "fact": "House target price $235.",
                "figures": [
                    {
                        "metric": "目标价",
                        "value": 235.0,
                        "unit": "per_share",
                        "period": "",
                        "label": "estimate",
                    }
                ],
            },
        ]
    }
    figs = extract_figures(md)
    outcomes = match_figures(figs, thesis_doc=thesis_doc)
    by_val = {round(o.figure.value, 1): o for o in outcomes}

    # TPU figure matches evidence E-TPU
    assert by_val[200.0].matched is True
    assert by_val[200.0].ref == "E-TPU"

    # Consensus target price matches evidence E-CONS without requiring a model cell
    assert by_val[220.0].matched is True
    assert by_val[220.0].ref == "E-CONS"

    # House target price with `evidence:` footnote still requires a `model:` cell
    assert by_val[235.0].matched is False
    assert by_val[235.0].lock_unverified is True
    assert "必须指向模型单元格" in by_val[235.0].hint


@pytest.mark.asyncio
async def test_missing_citation_table_falls_back_to_step_d_and_emits_top_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prose uses `[引用N]` but `## 引用` table is missing, figures enter Step D and top warning is rendered."""
    from src.dalton.llm import assist as assist_mod
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import numbers as numbers_mod

    draft = (
        "# 1. 财务表现\n"
        "FY2025 公司营业收入为 1,645.00 亿美元[引用1]，同比增长 22.0%[引用2]。\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261023-01",
                "fact": "FY2025 revenue was 164.50 billion USD, up 22.0% YoY.",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 164.5,
                        "unit": "USD_bn",
                        "period": "FY2025",
                        "label": "reported",
                    },
                    {
                        "metric": "收入同比增速",
                        "value": 22.0,
                        "unit": "pct",
                        "period": "FY2025",
                        "label": "reported",
                    },
                ],
            }
        ]
    }

    figs = extract_figures(draft)
    assert len(figs) == 2
    for f in figs:
        assert f.missing_citations == []
        assert f.footnote_target is None
        assert f.footnote_targets == []

    async def _fake_assist_json(*, task: str, payload: dict[str, Any], **kwargs: Any) -> assist_mod.AssistResult:
        if task in ("audit.figure_label", "audit.label_figures"):
            return assist_mod.AssistResult(
                ok=True,
                data={
                    "items": [
                        {
                            "id": item["id"],
                            "metric": item.get("regex_metric") or item.get("metric") or "营业收入",
                            "period": "FY2025",
                            "label_hint": "reported",
                            "incidental_category": None,
                        }
                        for item in payload.get("figures", [])
                    ],
                    "extra_figures": [],
                },
                model="test-audit-model",
            )
        if task == "audit.judge":
            items_out = []
            for item in payload.get("figures", []):
                if abs(float(item["value"]) - 1645.0) < 1e-3:
                    items_out.append(
                        {
                            "id": item["id"],
                            "verdict": "supported",
                            "matched_source_id": "E-20261023-01",
                            "quote": "FY2025 revenue was 164.50 billion USD, up 22.0% YoY.",
                            "quote_number_raw": "164.50",
                            "quote_metric_raw": "revenue",
                            "explanation": "Matched E-20261023-01",
                        }
                    )
                else:
                    items_out.append(
                        {
                            "id": item["id"],
                            "verdict": "supported",
                            "matched_source_id": "E-20261023-01",
                            "quote": "FY2025 revenue was 164.50 billion USD, up 22.0% YoY.",
                            "quote_number_raw": "22.0%",
                            "quote_metric_raw": "up",
                            "explanation": "Matched E-20261023-01",
                        }
                    )
            return assist_mod.AssistResult(
                ok=True,
                data={"items": items_out},
                model="test-audit-model",
            )
        return assist_mod.AssistResult(ok=False, error="unexpected_task")

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_fake_assist_json)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)

    audit_res = await aaudit_draft(
        draft,
        thesis_doc=thesis_doc,
        use_llm_judge=True,
        user_id="local-dev-user",
    )
    assert audit_res.missing_citation_table_count == 2
    assert audit_res.matched == 2
    assert len(audit_res.unverified) == 0

    rendered = render_audit_md(audit_res, draft_path="drafts/meta.md")
    assert "草稿缺少 `## 引用` 表，2 处 [引用] 无法解析" in rendered


def test_evidence_source_ref_primary_file_verifies_and_draft_self_ref_excluded() -> None:
    """Verifier reads primary files pointed to by `evidence[].source.ref` and excludes refs pointing to the draft itself."""
    draft = (
        "# 1. 云业务分析\n"
        "FY2025 Google Cloud 营业收入为 587.05 亿美元[引用1]。\n"
        "FY2025 资本开支为 914.47 亿美元[引用2]。\n"
        "FY2025 自由现金流为 732.66 亿美元。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-PRIMARY\n"
        "[引用2]: evidence:E-SELF-DRAFT\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-PRIMARY",
                "fact": "Alphabet 10-K 分部财务摘要（详见 data/googl_10k_segments.md）",
                "source": {
                    "type": "filing",
                    "identity": "Alphabet FY2025 10-K",
                    "ref": "data/googl_10k_segments.md",
                },
                "figures": [],
            },
            {
                "id": "E-SELF-DRAFT",
                "fact": "估值与财务完稿章节（详见草稿）",
                "source": {
                    "type": "other",
                    "identity": "Draft self reference",
                    "ref": "drafts/GOOGL_init_20261023.md",
                },
                "figures": [],
            },
        ]
    }
    evidence_ref_bodies = {
        "data/googl_10k_segments.md": (
            "Alphabet FY2025 10-K Segment Results: "
            "FY2025 Google Cloud 营业收入为 587.05 亿美元，FY2025 自由现金流为 732.66 亿美元。"
        ),
        "drafts/GOOGL_init_20261023.md": draft,
    }

    figs = extract_figures(draft)
    outcomes = match_figures(
        figs,
        thesis_doc=thesis_doc,
        evidence_ref_bodies=evidence_ref_bodies,
        draft_path="drafts/GOOGL_init_20261023.md",
    )
    by_val = {round(o.figure.value, 2): o for o in outcomes}

    # Order 1: E-PRIMARY verifies 587.05 via its primary file `data/googl_10k_segments.md`
    assert by_val[587.05].matched is True
    assert by_val[587.05].kind == "evidence"
    assert by_val[587.05].ref == "E-PRIMARY"

    # Order 1: E-SELF-DRAFT points to the draft itself and is excluded from verification
    assert by_val[914.47].matched is False
    assert "指向草稿自身" in by_val[914.47].hint

    # Step D: retrieve_candidate_sources includes primary file body for E-PRIMARY and excludes E-SELF-DRAFT
    fcf_fig = by_val[732.66].figure
    cands = retrieve_candidate_sources(
        fcf_fig,
        thesis_doc=thesis_doc,
        evidence_ref_bodies=evidence_ref_bodies,
        draft_path="drafts/GOOGL_init_20261023.md",
    )
    cand_ids = [c["source_id"] for c in cands]
    assert "E-PRIMARY" in cand_ids
    assert "E-SELF-DRAFT" not in cand_ids
    primary_cand = next(c for c in cands if c["source_id"] == "E-PRIMARY")
    assert "732.66" in primary_cand["text"]


@pytest.mark.asyncio
async def test_model_xlsx_cell_reading_and_prov_windowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service reads referenced `.xlsx` cells via `read_model_cell`, enforces reliable units, and windows long prov bodies."""
    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "A2": "营业利润率",
                "B2": Cell(0.241, num_fmt="0.0%"),
                "A3": "营业收入 ($B)",
                "C3": 18.55,  # 18.55 billion USD == 185.5 亿美元 (with row label ($B))
                "D4": 18.55,  # Unitless cell: no row/col header unit and General number_format
            }
        }
    )

    draft_md = (
        "# 1. 模型预测\n"
        "FY2026E 营业利润率预计达到 24.1%[引用1]，FY2026E 营业收入预计为 185.5 亿美元[引用2]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/val.xlsx#Summary!B2\n"
        "[引用2]: model:models/val.xlsx#Summary!C3\n"
    )
    files_store: dict[str, str] = {"drafts/val_report.md": draft_md}

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: xlsx_bytes if p == "models/val.xlsx" else None
    )

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )

    res = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/val_report.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": [{"path": "models/val.xlsx", "recalculated": True}]},
        provenance_records={},
        use_llm_judge=False,
    )
    assert res["ok"] is True
    assert res["audit"]["matched"] == 2
    assert res["audit"]["unverified"] == 0

    # Unitless cell D4 = 18.55 vs draft "185.5 亿美元" must remain unverified (never guess billion/million)
    draft_unitless_mismatch = (
        "# 1. 模型预测\n"
        "FY2026E 营业收入预计为 185.5 亿美元[引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/val.xlsx#Summary!D4\n"
    )
    files_store["drafts/unitless.md"] = draft_unitless_mismatch
    res_unitless = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/unitless.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": [{"path": "models/val.xlsx", "recalculated": True}]},
        provenance_records={},
        use_llm_judge=False,
    )
    assert res_unitless["ok"] is True
    assert res_unitless["audit"]["matched"] == 0
    assert res_unitless["audit"]["unverified"] == 1
    assert res_unitless["audit"]["contradicted"] == 0

    # Verify prov hit-centered windowing (>1000 chars offset) and draft self-read exclusion
    unfootnoted_fig = extract_figures("FY2025 核心广告收入达到 888.88 亿美元。\n")[0]
    long_prefix = "无关背景段落。" * 250  # > 1500 chars
    prov_records = {
        "prov-deep": {
            "record_id": "prov-deep",
            "source_type": "filing",
            "identifier": "10-K",
            "body_inline": f"{long_prefix} FY2025 核心广告收入达到 888.88 亿美元。后续段落。",
        },
        "prov-draft-self": {
            "record_id": "prov-draft-self",
            "source_type": "file_read",
            "identifier": "drafts/val_report.md",
            "body_inline": "FY2025 核心广告收入达到 888.88 亿美元。",
        },
        "prov-other-draft": {
            "record_id": "prov-other-draft",
            "source_type": "file_read",
            "identifier": "drafts/other_company_memo.md",
            "body_inline": "FY2025 核心广告收入达到 888.88 亿美元。",
        },
    }
    cands = retrieve_candidate_sources(
        unfootnoted_fig,
        provenance_records=prov_records,
        draft_path="drafts/val_report.md",
    )
    cand_ids = [c["source_id"] for c in cands]
    assert "prov-deep" in cand_ids
    assert "prov-draft-self" not in cand_ids
    assert "prov-other-draft" in cand_ids
    deep_cand = next(c for c in cands if c["source_id"] == "prov-deep")
    assert "888.88" in deep_cand["text"]


def test_threshold_exempt_marker_and_sentence_scoped_exemption() -> None:
    """`[阈值]` is recognized as an exemption marker, and exemption applies per sentence rather than the whole line."""
    from src.dalton.publish.numbers import has_exempt_marker

    assert has_exempt_marker("若营业利润率跌破 20% [阈值]，则触发改判条件。")
    assert has_exempt_marker("若营业利润率跌破 20% 〔阈值〕，则触发改判条件。")
    assert has_exempt_marker("若营业利润率跌破 20% （阈值），则触发改判条件。")

    # Same line: sentence 1 has no marker, sentence 2 has `[阈值]`; sentence 3 after `；` has no marker
    md = (
        "# 1. 核心判断与改判条件\n"
        "FY2025 Google Cloud 营业利润率达到 23.7%[引用1]。"
        "若后续季度营业利润率跌破 18.0% 或收入增速低于 20.0% [阈值]，则下调评级；"
        "FY2025 搜索广告收入同比增长 13.5%[引用1]。\n"
    )
    figs = extract_figures(md)
    by_val = {round(f.value, 1): f for f in figs}

    # Unmarked first sentence on the line is NOT exempted
    assert by_val[23.7].exempt is False
    assert by_val[23.7].exempt_reason is None

    # Second sentence with `[阈值]` IS exempted (both numbers in that sentence)
    assert by_val[18.0].exempt is True
    assert by_val[18.0].exempt_reason == "illustrative"
    assert by_val[20.0].exempt is True
    assert by_val[20.0].exempt_reason == "illustrative"

    # Third sentence after semicolon `；` on the same line is NOT exempted
    assert by_val[13.5].exempt is False
    assert by_val[13.5].exempt_reason is None


def test_ref_body_does_not_override_structured_figure_conflict_and_self_ref_keeps_structured_figures() -> None:
    """Structured `figures` conflict is not overridden by `source.ref` body; self-ref excludes only `ref` body, not structured `figures`."""
    draft = (
        "# 1. 收入核验\n"
        "FY2025 营业收入为 500.0 亿美元[引用1]。\n"
        "FY2025 净利润为 120.0 亿美元[引用2]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-CONFLICT\n"
        "[引用2]: evidence:E-SELF-WITH-FIGS\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-CONFLICT",
                "fact": "FY2025 营业收入为 600.0 亿美元。",
                "source": {"type": "filing", "identity": "10-K", "ref": "data/filing_note.md"},
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 60.0,
                        "unit": "USD_bn",
                        "period": "FY2025",
                        "label": "reported",
                    }
                ],
            },
            {
                "id": "E-SELF-WITH-FIGS",
                "fact": "FY2025 净利润为 120.0 亿美元。",
                "source": {"type": "other", "identity": "Draft", "ref": "drafts/current.md"},
                "figures": [
                    {
                        "metric": "净利润",
                        "value": 12.0,
                        "unit": "USD_bn",
                        "period": "FY2025",
                        "label": "reported",
                    }
                ],
            },
        ]
    }
    # Even though data/filing_note.md mentions "500.0 亿美元" elsewhere, structured figures[] already found a conflict (600 vs 500)
    evidence_ref_bodies = {
        "data/filing_note.md": "某段历史提及 FY2025 营业收入为 500.0 亿美元。",
        "drafts/current.md": draft,
    }
    figs = extract_figures(draft)
    outcomes = match_figures(
        figs,
        thesis_doc=thesis_doc,
        evidence_ref_bodies=evidence_ref_bodies,
        draft_path="drafts/current.md",
    )
    by_val = {round(o.figure.value, 1): o for o in outcomes}

    assert by_val[500.0].matched is False
    assert by_val[500.0].reason == "source_conflict"

    # E-SELF-WITH-FIGS has structured figures[] that still verify 120.0 even though source.ref points to drafts/current.md
    assert by_val[120.0].matched is True
    assert by_val[120.0].ref == "E-SELF-WITH-FIGS"


@pytest.mark.asyncio
async def test_model_cell_priority_missing_sheet_and_read_error_in_audit_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_xlsx` value takes priority over `models.json.key_outputs`; missing sheet and read failure are reported in `.audit.md`."""
    import builtins

    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import read_model_cell
    from tests.unit.dalton._xlsx_builder import build_xlsx

    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "C10": 200.0,  # Real xlsx value is 200, while stale key_outputs and draft say 100
            }
        }
    )

    draft_md = (
        "# 1. 估值与预测\n"
        "FY2026 目标价为 $100[引用1]。\n"
        "FY2026 营业利润为 $50bn[引用2]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/val.xlsx#Summary!C10\n"
        "[引用2]: model:models/val.xlsx#NoSuchSheet!B2\n"
    )
    files_store: dict[str, str] = {"drafts/val.md": draft_md}

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: xlsx_bytes if p == "models/val.xlsx" else None
    )

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )

    models_doc = {
        "versions": [
            {
                "path": "models/val.xlsx",
                "recalculated": True,
                "key_outputs": [
                    {
                        "name": "目标价",
                        "cell": "Summary!C10",
                        "value": 100.0,
                        "unit": "USD",
                        "period": "FY2026",
                    }
                ],
            }
        ]
    }
    res = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/val.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc=models_doc,
        provenance_records={},
        use_llm_judge=False,
    )
    assert res["ok"] is True
    assert res["audit"]["matched"] == 0
    # $100 vs real cell 200 USD -> source_conflict (never matched=True!)
    assert res["audit"]["contradicted"] == 1
    assert res["audit"]["unverified"] == 1
    audit_md_text = files_store["drafts/val.audit.md"]
    assert "模型文件中没有工作表 NoSuchSheet" in audit_md_text

    # Block openpyxl at runtime: stdlib XML reader still succeeds without openpyxl
    real_import = builtins.__import__

    def _no_openpyxl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("No module named 'openpyxl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_openpyxl)
    val_no_oxl, unit_no_oxl, err_no_oxl = read_model_cell(xlsx_bytes, "Summary", "C10")
    assert val_no_oxl == 200.0
    assert unit_no_oxl is None
    assert err_no_oxl is None

    # Corrupt xlsx bytes -> explicit read failure written to audit report
    backend.adownload_file_bytes.side_effect = (
        lambda p: b"not-a-valid-zip" if p == "models/val.xlsx" else None
    )
    files_store.pop("drafts/val.audit.md", None)
    res_err = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/val.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc=models_doc,
        provenance_records={},
        use_llm_judge=False,
    )
    assert res_err["audit"]["matched"] == 0
    assert "模型单元格读取失败" in files_store["drafts/val.audit.md"]


@pytest.mark.asyncio
async def test_provenance_tiered_loading_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_load_workspace_provenance` does not expand when batch 1 hits, and caps total records (including `prov:` refs) at `max_limit`."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor
    from src.dalton.publish import service as publish_svc

    executed_queries: list[tuple[str, Any]] = []

    cur_mock = create_autospec(AsyncCursor, instance=True)

    # Case 1: First batch hits the draft figure -> only 1 query executed, no expansion
    async def _exec_hit(
        sql: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Any:
        executed_queries.append((str(sql), params))
        return cur_mock

    async def _fetchall_hit() -> list[Any]:
        return [
            (
                "prov-001",
                "filing",
                "10-K",
                "Title",
                "Detail",
                "sec",
                "sha1",
                "FY2025 收入为 100 亿美元。",
                "FY2025 收入为 100 亿美元。",
            )
        ]

    cur_mock.execute.side_effect = _exec_hit
    cur_mock.fetchall.side_effect = _fetchall_hit

    @asynccontextmanager
    async def _fake_cursor(*args: Any, **kwargs: Any):
        yield cur_mock

    conn_mock = create_autospec(AsyncConnection, instance=True)
    conn_mock.cursor.side_effect = _fake_cursor

    @asynccontextmanager
    async def _fake_get_db(conn_arg: Any = None):
        yield conn_mock

    monkeypatch.setattr(
        publish_svc,
        "get_db_connection",
        create_autospec(publish_svc.get_db_connection, side_effect=_fake_get_db),
    )

    res_hit = await publish_svc._load_workspace_provenance(
        "ws-1",
        draft_path="drafts/d.md",
        draft_text="FY2025 收入为 100 亿美元。\n",
        initial_limit=300,
        max_limit=1500,
    )
    assert len(res_hit) == 1
    assert len(executed_queries) == 1
    assert executed_queries[0][1] == ("ws-1", 300)

    # Case 2: First batch misses and referenced_ids are present -> expands with LIMIT and never exceeds max_limit
    executed_queries.clear()
    call_idx = 0

    async def _fetchall_expand() -> list[Any]:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return [
                (f"prov-init-{i}", "filing", "10-K", "", "", "sec", f"s{i}", "无关内容", "无关内容")
                for i in range(3)
            ]
        if call_idx == 2:
            return [
                (f"prov-ref-{i}", "filing", "10-K", "", "", "sec", f"sr{i}", "无关内容", "无关内容")
                for i in range(5)
            ]
        return [
            (f"prov-exp-{i}", "filing", "10-K", "", "", "sec", f"se{i}", "无关内容", "无关内容")
            for i in range(10)
        ]

    cur_mock.fetchall.side_effect = _fetchall_expand

    res_capped = await publish_svc._load_workspace_provenance(
        "ws-1",
        draft_path="drafts/d.md",
        draft_text="FY2025 收入为 100 亿美元。\n",
        referenced_ids={"prov-ref-0", "prov-ref-1", "prov-ref-2"},
        initial_limit=3,
        max_limit=5,
    )
    assert len(res_capped) == 5
    assert all("LIMIT %s" in q[0] for q in executed_queries)


def test_english_period_after_number_sentence_boundary_and_footnote_only_citation_section() -> None:
    """English period after a number followed by space+uppercase is a sentence boundary; `## 引用` with only `[^note]` triggers missing-table fallback."""
    from src.dalton.publish.numbers import count_unparsed_citations_when_table_missing

    md = (
        "# 1. English Sentences\n"
        "Revenue 100. Profit 20 [引用2]. Growth 1.5% [引用1].\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-1\n"
        "[引用2]: evidence:E-2\n"
    )
    figs = extract_figures(md)
    by_val = {round(f.value, 1): f for f in figs}
    assert by_val[100.0].footnote_id is None
    assert by_val[20.0].footnote_id == "引用2"
    assert by_val[1.5].footnote_id == "引用1"

    # `## 引用` containing only `[^note]: ...` and no `[引用N]` definitions is treated as missing `## 引用` table
    md_only_trad_fn = (
        "# 1. 摘要\n"
        "FY2025 收入为 100 亿美元[引用1]，利润率 25%[^note]。\n\n"
        "## 引用\n"
        "[^note]: evidence:E-NOTE\n"
    )
    assert count_unparsed_citations_when_table_missing(md_only_trad_fn) == 1
    figs_fallback = extract_figures(md_only_trad_fn)
    by_val_fb = {round(f.value, 1): f for f in figs_fallback}
    assert by_val_fb[100.0].missing_citations == []
    assert by_val_fb[100.0].footnote_target is None
    assert by_val_fb[25.0].footnote_target == "evidence:E-NOTE"


@pytest.mark.asyncio
async def test_stdlib_xlsx_model_cell_reading_and_unrecalculated_formula_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stdlib XML `.xlsx` cell reading (with openpyxl blocked at runtime) and unrecalculated formula checks."""
    import builtins

    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import count_unrecalculated_formulas, read_model_cell
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    # 1. Build `.xlsx` workbooks via stdlib `_xlsx_builder` (with and without cached `<v>` on formulas)
    unrecalc_only_bytes = build_xlsx(
        {
            "Summary": {
                "A12": "营业收入 ($B)",
                "B10": 10.0,
                "B11": 20.0,
                "B12": Cell(None, formula="=B10+B11"),
                "B13": Cell(None, formula="=B12*2"),
                "C2": True,
            }
        }
    )
    recalc_bytes = build_xlsx(
        {
            "Summary": {
                "A12": "营业收入 ($B)",
                "B10": 10.0,
                "B11": 20.0,
                "B12": Cell(30.0, formula="=B10+B11"),
                "B13": Cell(60.0, formula="=B12*2"),
                "C2": True,
                # Shared string t="s" at A20 (index 1 in sharedStrings.xml -> "共享字符串文本")
                "A20": Cell("共享字符串文本", t="s"),
                # inlineStr at B20
                "B20": Cell("内联字符串($M)", t="inlineStr"),
                # Formula string result t="str" at C20
                "C20": Cell("FY2026", t="str", formula='CONCAT("FY","2026")'),
                # Boolean t="b" at D20
                "D20": Cell(True, t="b"),
                # Error cell t="e" at E20
                "E20": Cell("#DIV/0!", t="e"),
            }
        }
    )

    # 2. Block openpyxl at runtime for the remainder of the test to prove zero runtime dependency
    real_import = builtins.__import__

    def _forbid_openpyxl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("openpyxl is forbidden at runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _forbid_openpyxl)

    # 3. Direct `read_model_cell` assertions on recalculated workbook + special cell types
    assert count_unrecalculated_formulas(recalc_bytes) == 0
    val_b12, unit_b12, err_b12 = read_model_cell(recalc_bytes, "Summary", "B12")
    assert val_b12 == 30.0
    assert unit_b12 == "USD_bn"
    assert err_b12 is None

    # Coordinate normalization: `$B$12` and ` $b$13 `
    val_dollar, unit_dollar, err_dollar = read_model_cell(
        recalc_bytes, "Summary", " $B$12 "
    )
    assert val_dollar == 30.0
    assert unit_dollar == "USD_bn"
    assert err_dollar is None

    # Shared string t="s"
    val_s, _, err_s = read_model_cell(recalc_bytes, "Summary", "A20")
    assert err_s is None
    assert val_s == "共享字符串文本"

    # inlineStr t="inlineStr"
    val_in, _, err_in = read_model_cell(recalc_bytes, "Summary", "B20")
    assert val_in == "内联字符串($M)"
    assert err_in is None

    # Formula string t="str"
    val_fstr, _, err_fstr = read_model_cell(recalc_bytes, "Summary", "C20")
    assert val_fstr == "FY2026"
    assert err_fstr is None

    # Boolean t="b"
    val_b, _, err_b = read_model_cell(recalc_bytes, "Summary", "D20")
    assert val_b is True
    assert err_b is None

    # Error cell t="e"
    val_e, _, err_e = read_model_cell(recalc_bytes, "Summary", "E20")
    assert val_e is None
    assert err_e is not None and "#DIV/0!" in err_e

    # Non-existent worksheet and non-existent cell
    val_ns, _, err_ns = read_model_cell(recalc_bytes, "MissingSheet", "B12")
    assert val_ns is None
    assert err_ns == "模型文件中没有工作表 MissingSheet"

    val_nc, _, err_nc = read_model_cell(recalc_bytes, "Summary", "Z99")
    assert val_nc is None
    assert err_nc is not None and "不存在或为空" in err_nc

    # 4. Unrecalculated workbook without `<v>` on formulas
    assert count_unrecalculated_formulas(unrecalc_only_bytes) == 2
    val_un, _, err_un = read_model_cell(unrecalc_only_bytes, "Summary", "B12")
    assert val_un is None
    assert err_un == "公式没有缓存值（模型未重算，请先运行 recalc.py）"

    # 5. `check_draft` and `publish_report` on unrecalculated vs recalculated workbook
    draft_with_dollar_coord = (
        "# 1. 模型预测\n"
        "FY2026E 营业收入预计为 300 亿美元[引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/val.xlsx#Summary!$B$12\n"
    )
    files_store: dict[str, str] = {"drafts/val.md": draft_with_dollar_coord}
    current_xlsx: dict[str, bytes] = {"bytes": unrecalc_only_bytes}

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: current_xlsx["bytes"] if p == "models/val.xlsx" else None
    )

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )

    expected_recalc_cmd = (
        "模型未重算：2 个公式没有缓存值，请运行 "
        "`python .agents/skills/xlsx/scripts/recalc.py models/val.xlsx` 后再发布"
    )

    # 5a. `check_draft` on unrecalculated workbook warns at top of `.audit.md`
    chk_unrecalc = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/val.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": [{"path": "models/val.xlsx", "recalculated": True}]},
        provenance_records={},
        use_llm_judge=False,
    )
    assert chk_unrecalc["ok"] is True
    assert chk_unrecalc["audit"]["matched"] == 0
    assert chk_unrecalc["audit"]["unverified"] == 1
    audit_md_unrecalc = files_store["drafts/val.audit.md"]
    assert expected_recalc_cmd in audit_md_unrecalc
    assert "公式没有缓存值（模型未重算，请先运行 recalc.py）" in audit_md_unrecalc

    # 5b. `publish_report` (`run_publish`) is rejected with the recalc.py command (even when prose figures all pass via evidence)
    draft_only_attachment_model = (
        "# 1. 历史数据\n"
        "FY2025 营业收入为 400 亿美元[引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-1\n"
    )
    files_store["drafts/att_only.md"] = draft_only_attachment_model
    thesis_ok = {
        "evidence": [
            {
                "id": "E-1",
                "fact": "FY2025 营业收入为 400 亿美元。",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 40.0,
                        "unit": "USD_bn",
                        "period": "FY2025",
                        "label": "reported",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(
            db_publications.resolve_thread_author_model,
            return_value="claude-opus-5-5-oauth",
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={"thesis": {"content": thesis_ok, "version_no": 1}},
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(
            db_publications.get_or_create_request,
            return_value=(
                {
                    "publish_request_id": "req-unrecalc-1",
                    "workspace_id": "ws-1",
                    "user_id": "local-dev-user",
                    "status": "auditing",
                },
                True,
            ),
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(db_publications.transition, return_value=None),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )

    pub_unrecalc = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="th-1",
        tool_call_id="tc-1",
        draft_path="drafts/att_only.md",
        title="GOOGL",
        kind="initiation",
        slug="googl-init",
        attachments=[{"path": "models/val.xlsx", "role": "model"}],
        backend=backend,
    )
    assert pub_unrecalc["ok"] is False
    assert pub_unrecalc["status"] == "audit_failed"
    assert expected_recalc_cmd in pub_unrecalc["message"]
    assert expected_recalc_cmd in files_store["drafts/att_only.audit.md"]

    # 5c. Once recalculated (`recalc_bytes`), `check_draft` on `drafts/val.md` (`Summary!$B$12` = 30.0 USD_bn == 300 亿美元) verifies!
    current_xlsx["bytes"] = recalc_bytes
    files_store.pop("drafts/val.audit.md", None)
    chk_recalc = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/val.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": [{"path": "models/val.xlsx", "recalculated": True}]},
        provenance_records={},
        use_llm_judge=False,
    )
    assert chk_recalc["ok"] is True
    assert chk_recalc["audit"]["matched"] == 1
    assert chk_recalc["audit"]["unverified"] == 0
    assert "模型未重算" not in files_store["drafts/val.audit.md"]


@pytest.mark.asyncio
async def test_round5_pct_unit_strictness_and_row_col_unit_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 #1–#3:
    1. Percentage never guesses:
       - `1.0 pct` vs draft `100%` does not match;
       - `0.21 pct` (from `figures`, `models.json`, or row label) vs `21%` does not match;
       - Unitless `0.21` vs `21%` does not match;
       - Percentage-formatted Excel cell (`0.21` with `0.0%` number_format) vs `21%` matches.
    2. Unit hints only inspect same row, same column, or explicit `单位：` cell;
       when row 2 (`A2`) has `营业利润率 (%)`, unitless `18.55` in row 4 (`D4`) cannot verify `18.55%`.
    3. Magnitude and currency are separated:
       - `18.55 亿美元` (`USD_100mn`) does NOT match draft `185.5 亿美元`, and DOES match draft `18.55 亿美元`;
       - `HK$B` is recognized as `HKD_bn` (港元);
       - Scale-only label without currency (`in millions`) is `unknown_mn` (`unknown_currency`) and cannot verify `USD_mn`.
    """
    from unittest.mock import create_autospec

    import src.dalton.auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import (
        _parse_unit_from_label_text,
        match_figures,
        read_model_cell,
        values_within_tolerance,
    )
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    # --- Item 1: `1.0 pct` vs `100%` and `0.21 pct` (from figures) vs `21%` do NOT match ---
    fig_100pct = extract_figures("FY2025 收入增速为 100%。")[0]
    ok_100, _, _ = values_within_tolerance(fig_100pct, 1.0, "pct")
    assert ok_100 is False

    fig_1pct = extract_figures("FY2025 收入增速为 1%。")[0]
    ok_1, _, _ = values_within_tolerance(fig_1pct, 1.0, "pct")
    assert ok_1 is True

    fig_21pct = extract_figures("FY2025 营业利润率为 21%[引用1]。\n\n## 引用\n[引用1]: evidence:E-1\n")[0]
    ok_021_fig, _, _ = values_within_tolerance(fig_21pct, 0.21, "pct")
    assert ok_021_fig is False
    ok_021_other, _, _ = values_within_tolerance(fig_21pct, 0.21, "other")
    assert ok_021_other is False

    outcomes_ev = match_figures(
        [fig_21pct],
        thesis_doc={
            "evidence": [
                {
                    "id": "E-1",
                    "fact": "FY2025 营业利润率为 0.21%。",
                    "figures": [
                        {
                            "metric": "营业利润率",
                            "value": 0.21,
                            "unit": "pct",
                            "period": "FY2025",
                            "label": "reported",
                        }
                    ],
                }
            ]
        },
    )
    assert outcomes_ev[0].matched is False

    # --- Item 2 & 3: Workbook with percentage number_format, row-only units, 亿美元 (`USD_100mn`), and `HK$B` (`HKD_bn`) ---
    assert _parse_unit_from_label_text("收入 (亿美元)") == "USD_100mn"
    assert _parse_unit_from_label_text("收入 (十亿美元)") == "USD_bn"
    assert _parse_unit_from_label_text("收入 (HK$B)") == "HKD_bn"
    assert _parse_unit_from_label_text("收入 (HKD bn)") == "HKD_bn"
    assert _parse_unit_from_label_text("Revenue (in millions)") == "unknown_mn"
    assert _parse_unit_from_label_text("增长超 10% 的核心业务") is None

    fig_hkd = extract_figures("| 指标 | FY2025 |\n|---|---|\n| 收入 (HK$B) | 50.0 |\n")[0]
    assert fig_hkd.currency == "HKD"
    assert fig_hkd.unit == "HKD_bn"

    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "A2": "营业利润率 (%)",
                "B2": 0.21,  # Row label says (%), General number_format -> 0.21 pct (0.21%), NOT 21%!
                "C2": Cell(0.21, num_fmt="0.0%"),
                "A3": "净利率",
                "B3": Cell(0.21, num_fmt="0.0%"),  # No unit in row 3 label -> unit from cell percentage number_format!
                "D4": 18.55,  # Row 4 has no row label unit; must NOT inherit `(%)` from A2!
                "A5": "营业收入 (亿美元)",
                "B5": 18.55,  # 18.55 亿美元 (`USD_100mn`)
                "A6": "分部收入 (in millions)",
                "B6": 18.55,  # Scale-only without currency (`unknown_mn`)
            }
        }
    )

    # Check `read_model_cell` units directly
    val_b3, unit_b3, err_b3 = read_model_cell(xlsx_bytes, "Summary", "B3")
    assert err_b3 is None and val_b3 == 0.21
    assert unit_b3 == "pct" and getattr(unit_b3, "is_pct_number_format", False) is True

    val_d4, unit_d4, err_d4 = read_model_cell(xlsx_bytes, "Summary", "D4")
    assert err_d4 is None and val_d4 == 18.55
    assert unit_d4 is None

    val_b5, unit_b5, err_b5 = read_model_cell(xlsx_bytes, "Summary", "B5")
    assert err_b5 is None and val_b5 == 18.55
    assert unit_b5 == "USD_100mn"

    val_b6, unit_b6, err_b6 = read_model_cell(xlsx_bytes, "Summary", "B6")
    assert err_b6 is None and val_b6 == 18.55
    assert unit_b6 == "unknown_mn"

    draft_md = (
        "# 1. 测试\n"
        "FY2025 净利率为 21%[引用1]。\n"
        "FY2025 营业利润率为 21%[引用2]。\n"
        "FY2025 研发费率为 18.55%[引用3]。\n"
        "FY2025 营业收入为 185.5 亿美元[引用4]。\n"
        "FY2025 调整后营业收入为 18.55 亿美元[引用5]。\n"
        "FY2025 分部收入为 1,855 百万美元[引用6]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/r5.xlsx#Summary!B3\n"
        "[引用2]: model:models/r5.xlsx#Summary!B2\n"
        "[引用3]: model:models/r5.xlsx#Summary!D4\n"
        "[引用4]: model:models/r5.xlsx#Summary!B5\n"
        "[引用5]: model:models/r5.xlsx#Summary!B5\n"
        "[引用6]: model:models/r5.xlsx#Summary!B6\n"
    )
    files_store: dict[str, str] = {"drafts/r5.md": draft_md}

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: xlsx_bytes if p == "models/r5.xlsx" else None
    )
    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )

    res = await publish_svc.run_check_draft(
        user_id="local-dev-user",
        workspace_id="ws-1",
        draft_path="drafts/r5.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": [{"path": "models/r5.xlsx", "recalculated": True}]},
        provenance_records={},
        use_llm_judge=False,
    )
    assert res["ok"] is True
    # Only [引用1] (percentage-formatted 0.21 == 21%) and [引用5] (18.55 亿美元 == 18.55 亿美元) match!
    matched_raws = [m["raw"] for m in res["audit"]["matches"]]
    assert matched_raws == ["21%", "18.55 亿美元"]
    assert res["audit"]["matched"] == 2
    assert res["audit"]["contradicted"] + res["audit"]["unverified"] == 4


@pytest.mark.asyncio
async def test_round5_xlsx_read_failure_never_falls_back_to_models_json_key_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 #4: When `.xlsx` download fails (`None`), is empty (`b""`), or fails to parse,
    `_merge_model_cells_with_models_doc` must NOT fall back to `models.json` `key_outputs` values.
    """
    from unittest.mock import create_autospec
    import src.dalton.auth as dalton_auth
    from src.dalton.publish import service as publish_svc

    draft_md = (
        "# 1. 估值\n"
        "FY2026E 营业收入预计为 300 亿美元[引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/broken.xlsx#Summary!B12\n"
    )
    files_store: dict[str, str] = {"drafts/broken.md": draft_md}
    current_bytes: dict[str, bytes | None] = {"raw": None}

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: current_bytes["raw"] if p == "models/broken.xlsx" else None
    )
    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )

    models_doc_with_ko = {
        "versions": [
            {
                "path": "models/broken.xlsx",
                "recalculated": True,
                "key_outputs": [
                    {
                        "name": "营业收入",
                        "cell": "Summary!B12",
                        "value": 30.0,
                        "unit": "USD_bn",
                        "period": "FY2026E",
                    }
                ],
            }
        ]
    }

    for bad_payload in (None, b"", b"corrupt-not-a-zip"):
        current_bytes["raw"] = bad_payload
        files_store.pop("drafts/broken.audit.md", None)
        res = await publish_svc.run_check_draft(
            user_id="local-dev-user",
            workspace_id="ws-1",
            draft_path="drafts/broken.md",
            backend=backend,
            thesis_doc={"evidence": []},
            models_doc=models_doc_with_ko,
            provenance_records={},
            use_llm_judge=False,
        )
        assert res["ok"] is True
        assert res["audit"]["matched"] == 0
        assert res["audit"]["unverified"] == 1


@pytest.mark.asyncio
async def test_round5_publish_gates_all_statuses_corrupt_xml_and_referenced_model_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 #5 & #9:
    - `count_unrecalculated_formulas` raises `ValueError` on empty bytes, corrupt zip, or corrupt worksheet XML;
    - `<c t="str"><f>""</f><v/></c>` is a valid empty cached string and NOT counted as unrecalculated;
    - `run_publish` enforces unrecalculated/corrupt model checks and referenced `model:` `.xlsx` `sha256` checks
      across `reviewing`, `awaiting_pm` (including after `interrupt()`), `approved`, and `publishing`.
    """
    import hashlib
    from unittest.mock import create_autospec

    import src.dalton.auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import count_unrecalculated_formulas, read_model_cell
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    def _build_xlsx(
        *,
        b12_cached: str | None = "30.0",
        str_formula_mode: str = "empty_v",
        corrupt_sheet2: bool = False,
    ) -> bytes:
        c21_val: str | None = "" if str_formula_mode == "empty_v" else None
        extra = (
            {"xl/worksheets/sheet2.xml": b"<worksheet><broken"}
            if corrupt_sheet2
            else None
        )
        return build_xlsx(
            {
                "Summary": {
                    "A12": "营业收入 ($B)",
                    "B12": Cell(b12_cached, formula="=10+20"),
                    "C21": Cell(c21_val, t="str", formula='""'),
                }
            },
            extra_entries=extra,
        )

    # Item 9: `<c t="str"><f>""</f><v/></c>` is valid cached empty string (0 unrecalculated)
    valid_empty_str_bytes = _build_xlsx(b12_cached="30.0", str_formula_mode="empty_v")
    assert count_unrecalculated_formulas(valid_empty_str_bytes) == 0
    val_c21, _, err_c21 = read_model_cell(valid_empty_str_bytes, "Summary", "C21")
    assert err_c21 is None and val_c21 == ""

    # Missing `<v>` on `t="str"` formula IS unrecalculated
    missing_v_str_bytes = _build_xlsx(b12_cached="30.0", str_formula_mode="missing_v")
    assert count_unrecalculated_formulas(missing_v_str_bytes) == 1
    _, _, err_c21_missing = read_model_cell(missing_v_str_bytes, "Summary", "C21")
    assert err_c21_missing == "公式没有缓存值（模型未重算，请先运行 recalc.py）"

    # Item 5: Corrupt zip / empty bytes / corrupt worksheet XML raise ValueError in `count_unrecalculated_formulas`
    with pytest.raises(ValueError):
        count_unrecalculated_formulas(b"")
    with pytest.raises(ValueError):
        count_unrecalculated_formulas(b"not-a-zip")
    corrupt_ws_bytes = _build_xlsx(
        b12_cached="30.0", str_formula_mode="empty_v", corrupt_sheet2=True
    )
    with pytest.raises(ValueError):
        count_unrecalculated_formulas(corrupt_ws_bytes)

    # Item 5: `run_publish` gates on `awaiting_pm` (after interrupt) and `approved` when referenced `model:` `.xlsx`
    # (not in `attachments`) changes its `sha256` or has unrecalculated/corrupt XML after audit
    draft_md = (
        "# 1. 模型预测\n"
        "FY2026E 营业收入预计为 300 亿美元[引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/ref_only.xlsx#Summary!B12\n"
    )
    files_store: dict[str, str] = {"drafts/ref_gate.md": draft_md}
    current_xlsx: dict[str, bytes] = {"bytes": valid_empty_str_bytes}
    audit_time_sha = hashlib.sha256(valid_empty_str_bytes).hexdigest()

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BackendSpec, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)
    backend.adownload_file_bytes.side_effect = (
        lambda p: current_xlsx["bytes"] if p == "models/ref_only.xlsx" else None
    )
    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-1"}
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(
            db_publications.resolve_thread_author_model,
            return_value="claude-opus-5-5-oauth",
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={"thesis": {"content": {"evidence": []}, "version_no": 1}},
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(db_publications.transition, return_value=None),
    )

    from src.dalton.publish.files import compute_bundle_sha256

    bundle_sha = compute_bundle_sha256(draft_md.encode("utf-8"), [])
    audit_dict = {
        "figures": 1,
        "matched": 1,
        "verified": 1,
        "contradicted": 0,
        "unverified": 0,
        "uncertain": 0,
        "exempt": 0,
        "model_file_sha256s": {"models/ref_only.xlsx": audit_time_sha},
        "matches": [
            {
                "id": "F-001",
                "raw": "300 亿美元",
                "kind": "model",
                "ref": "models/ref_only.xlsx#Summary!B12",
                "source_sha256": audit_time_sha,
            }
        ],
    }

    # Case A: Request is in `awaiting_pm`; during `interrupt()`, the referenced `.xlsx` is modified to a different value
    from src.dalton.settings import get_dalton_config

    monkeypatch.setenv("DALTON_PM_USER_IDS", "local-dev-user")
    get_dalton_config.cache_clear()
    orig_get_request = db_publications.get_request
    modified_recalc_bytes = _build_xlsx(b12_cached="99.0", str_formula_mode="empty_v")
    monkeypatch.setattr(
        db_publications,
        "get_request",
        create_autospec(
            orig_get_request,
            return_value={
                "publish_request_id": "req-awaiting-1",
                "workspace_id": "ws-1",
                "user_id": "local-dev-user",
                "status": "awaiting_pm",
                "bundle_sha256": bundle_sha,
                "audit": audit_dict,
                "review": {"model": "gemini-3.1-pro", "family": "google", "verdict": "pass"},
            },
        ),
    )

    def _interrupt_mutates_xlsx(_payload: Any) -> dict[str, Any]:
        current_xlsx["bytes"] = modified_recalc_bytes
        return {"decisions": [{"type": "approve"}]}

    res_awaiting = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="th-1",
        tool_call_id="tc-1",
        draft_path="drafts/ref_gate.md",
        title="GOOGL",
        kind="initiation",
        slug="googl-init",
        request_id="req-awaiting-1",
        backend=backend,
        interrupt_fn=_interrupt_mutates_xlsx,
    )
    assert res_awaiting["ok"] is False
    assert res_awaiting["status"] == "superseded"
    assert "sha256 不一致" in res_awaiting["message"]

    # Case B: Request is in `approved`, and the referenced `.xlsx` has corrupt worksheet XML
    current_xlsx["bytes"] = corrupt_ws_bytes
    monkeypatch.setattr(
        db_publications,
        "get_request",
        create_autospec(
            orig_get_request,
            return_value={
                "publish_request_id": "req-approved-1",
                "workspace_id": "ws-1",
                "user_id": "local-dev-user",
                "status": "approved",
                "bundle_sha256": bundle_sha,
                "audit": audit_dict,
                "review": {"model": "gemini-3.1-pro", "family": "google", "verdict": "pass"},
            },
        ),
    )
    res_approved_corrupt = await publish_svc.run_publish(
        user_id="local-dev-user",
        workspace_id="ws-1",
        thread_id="th-1",
        tool_call_id="tc-1",
        draft_path="drafts/ref_gate.md",
        title="GOOGL",
        kind="initiation",
        slug="googl-init",
        request_id="req-approved-1",
        backend=backend,
    )
    assert res_approved_corrupt["ok"] is False
    assert res_approved_corrupt["status"] == "audit_failed"
    assert "损坏或解析失败" in res_approved_corrupt["message"]


@pytest.mark.asyncio
async def test_round5_table_sentence_exemption_prov_expansion_and_exact_self_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 #6, #7, #8:
    6. Table exemption is sentence-scoped: `[阈值]` in the first sentence of a table row header or cell
       only exempts that sentence; the following unmarked sentence with actual performance is audited.
    7. `_prov_hits_all_key_figures` / `_load_workspace_provenance` checks metric, period, value, and unit:
       `FY2024 利润 $100bn` in batch 1 does NOT count as a hit for `FY2025 收入 $100bn` in the draft, so expansion continues.
    8. `_is_draft_provenance_record` normalizes paths and only excludes `draft_path` or `drafts/<basename>`,
       never `data/current.md`, `drafts/current.md.bak`, or `other/current.md`.
    """
    from contextlib import asynccontextmanager
    from typing import AsyncIterator
    from psycopg import AsyncConnection, AsyncCursor
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import _is_draft_provenance_record

    # --- Item 6: Cross-sentence table exemption regression ---
    table_md = (
        "# 1. 业绩与阈值对比\n\n"
        "| 指标 | 数值说明 |\n"
        "|---|---|\n"
        "| 营业利润率目标[阈值]为 15%。实际 FY2025 营业利润率 | 25.0% |\n"
        "| 营收增速 | 目标线[阈值] 20.0%。实际 FY2025 营收增速为 30.0%。 |\n"
    )
    figs = extract_figures(table_md)
    by_raw = {f.raw: f for f in figs}
    assert "15%" in by_raw and by_raw["15%"].exempt is True
    assert "25.0%" in by_raw and by_raw["25.0%"].exempt is False
    assert by_raw["25.0%"].period == "FY2025"
    assert "20.0%" in by_raw and by_raw["20.0%"].exempt is True
    assert "30.0%" in by_raw and by_raw["30.0%"].exempt is False
    assert by_raw["30.0%"].period == "FY2025"

    # --- Item 7: Provenance expansion when batch 1 only has `FY2024 利润 $100bn` vs draft `FY2025 收入 $100bn` ---
    draft_prov = "FY2025 收入为 $100bn。\n"
    assert (
        publish_svc._prov_hits_all_key_figures(
            draft_prov,
            {"p1": {"body_inline": "FY2024 利润为 $100bn。"}},
            draft_path="drafts/current.md",
        )
        is False
    )
    assert (
        publish_svc._prov_hits_all_key_figures(
            draft_prov,
            {"p2": {"body_inline": "FY2025 收入为 $100bn。"}},
            draft_path="drafts/current.md",
        )
        is True
    )

    executed_queries: list[tuple[str, Any]] = []
    current_rows: list[Any] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _execute(
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Any:
        sql_str = str(query)
        executed_queries.append((sql_str, params))
        if "OFFSET" not in sql_str:
            current_rows[:] = [
                (
                    "prov-batch1",
                    "tool",
                    "sec_filing",
                    "10-K",
                    "detail",
                    "sec",
                    "sha-1",
                    "FY2024 利润为 $100bn。",
                    "FY2024 利润为 $100bn。",
                )
            ]
        else:
            current_rows[:] = [
                (
                    "prov-batch2",
                    "tool",
                    "sec_filing",
                    "10-K",
                    "detail",
                    "sec",
                    "sha-2",
                    "FY2025 收入为 $100bn。",
                    "FY2025 收入为 $100bn。",
                )
            ]
        return cur

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = lambda: list(current_rows)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield cur

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm

    @asynccontextmanager
    async def _fake_get_db_connection(conn_arg: Any = None) -> AsyncIterator[Any]:
        yield conn

    orig_get_db_conn = publish_svc.get_db_connection
    monkeypatch.setattr(
        publish_svc,
        "get_db_connection",
        create_autospec(orig_get_db_conn, side_effect=_fake_get_db_connection),
    )
    loaded_prov = await publish_svc._load_workspace_provenance(
        "ws-1",
        draft_path="drafts/current.md",
        draft_text=draft_prov,
        initial_limit=1,
        max_limit=5,
    )
    assert any("OFFSET" in sql for sql, _ in executed_queries)
    assert "prov-batch1" in loaded_prov and "prov-batch2" in loaded_prov

    # --- Item 8: `_is_draft_provenance_record` exact normalized self-ref only ---
    assert _is_draft_provenance_record({"identifier": "drafts/current.md"}, "drafts/current.md") is True
    assert _is_draft_provenance_record({"identifier": "./drafts/./current.md"}, "drafts/current.md") is True
    assert _is_draft_provenance_record({"identifier": "drafts/sub/../current.md"}, "drafts/current.md") is True
    assert _is_draft_provenance_record({"identifier": "data/current.md"}, "drafts/current.md") is False
    assert _is_draft_provenance_record({"identifier": "drafts/current.md.bak"}, "drafts/current.md") is False
    assert _is_draft_provenance_record({"identifier": "other/current.md"}, "drafts/current.md") is False


@pytest.mark.asyncio
async def test_round6_pct_stored_as_ratio_literal_pct_100mn_currency_and_table_header_sentences() -> None:
    """Round 6 #1-#5:
    1. `stored_as_ratio` is recorded independently from unit labels (`(%)` row label or `key_outputs.unit`):
       cached `0.21` with format `0.00%` and row label `(%)` (or `key_outputs.unit = "pct"`) does NOT verify `0.21%`,
       and DOES verify `21%`.
    2. Literal `%` in Excel `number_format` (`0.00"%"` or `0.00\\%`) is not a percentage format specifier (`stored_as_ratio=False`),
       so cached `0.21` does NOT multiply by 100 and does NOT match `21%`; only unquoted/unescaped `0.00%` does.
    3. `亿` (`1e8`, `USD_100mn`) converts to base units without raw-value fallback:
       `18.55 亿美元` does NOT match `18.55 USD_bn`, and DOES match `1.855 USD_bn`.
    4. All currency comparisons require explicit and matching currency:
       `$100bn` does NOT match currency-less `100 billion`;
       HKD EPS (`港元每股收益`) does NOT match USD values (`USD` or `USD/sh`).
    5. Column header narrative numbers are extracted per sentence (only the marked sentence is exempt),
       and row/col header markers never bleed into the first sentence of adjacent cells.
    """
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import (
        _is_pct_number_format,
        _parse_unit_from_number_format,
    )
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    # --- Item 2: `0.00"%"`, `0.00\%`, `0.00%` format strings ---
    assert _is_pct_number_format('0.00"%"') is False
    assert _is_pct_number_format(r"0.00\%") is False
    assert _is_pct_number_format("0.00%") is True
    assert _parse_unit_from_number_format('0.00"%"') is None
    assert _parse_unit_from_number_format(r"0.00\%") is None
    u_pct_fmt = _parse_unit_from_number_format("0.00%")
    assert u_pct_fmt == "pct"
    assert getattr(u_pct_fmt, "stored_as_ratio", False) is True

    # Build an .xlsx with 3 styles:
    # s="1" -> 0.00% (true percentage format, stored_as_ratio=True)
    # s="2" -> 0.00"%" (quoted literal "%", stored_as_ratio=False)
    # s="3" -> 0.00\% (escaped literal "\%", stored_as_ratio=False)
    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "A10": Cell("营业利润率 (%)", t="inlineStr"),
                "B10": Cell(0.21, num_fmt="0.00%"),
                "C10": Cell(0.21, num_fmt='0.00"%"'),
                "D10": Cell(0.21, num_fmt=r"0.00\%"),
            }
        }
    )

    class _BytesBackendProto:
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BytesBackendProto, instance=True)
    backend.adownload_file_bytes.return_value = xlsx_bytes

    # --- Item 1: cached `0.21`, format `0.00%`, row label `(%)` (and `key_outputs.unit = "pct"`) ---
    models_doc = {
        "versions": [
            {
                "id": "v1",
                "path": "models/m.xlsx",
                "recalculated": True,
                "key_outputs": [
                    {"name": "营业利润率", "cell": "Summary!B10", "value": 0.21, "unit": "pct", "period": "FY2025"}
                ],
            }
        ]
    }
    draft_wrong_021 = (
        "FY2025 营业利润率为 0.21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    cells_021 = await publish_svc._load_referenced_model_cells(
        backend, draft_wrong_021, models_doc=models_doc
    )
    assert cells_021["models/m.xlsx#Summary!B10"]["stored_as_ratio"] is True
    out_wrong_021 = match_figures(
        extract_figures(draft_wrong_021),
        models_doc=models_doc,
        model_cells=cells_021,
    )
    assert out_wrong_021[0].matched is False

    draft_right_21 = (
        "FY2025 营业利润率为 21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    out_right_21 = match_figures(
        extract_figures(draft_right_21),
        models_doc=models_doc,
        model_cells=cells_021,
    )
    assert out_right_21[0].matched is True

    # --- Item 2: cached `0.21` with literal `%` formats `0.00"%"` (C10) and `0.00\%` (D10) ---
    draft_lit_pct = (
        "FY2025 营业利润率为 21% [引用1]，毛利率为 21% [引用2]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!C10\n"
        "[引用2]: model:models/m.xlsx#Summary!D10\n"
    )
    cells_lit = await publish_svc._load_referenced_model_cells(backend, draft_lit_pct)
    assert cells_lit["models/m.xlsx#Summary!C10"]["stored_as_ratio"] is False
    assert cells_lit["models/m.xlsx#Summary!D10"]["stored_as_ratio"] is False
    out_lit = match_figures(extract_figures(draft_lit_pct), model_cells=cells_lit)
    assert out_lit[0].matched is False
    assert out_lit[1].matched is False

    # --- Item 3: `18.55 亿美元` (`USD_100mn`) vs `18.55 USD_bn` (False) and `1.855 USD_bn` (True) ---
    fig_1855_yi = extract_figures("FY2025 收入为 18.55 亿美元。")[0]
    assert fig_1855_yi.unit == "USD_100mn"
    assert fig_1855_yi.scale == 1e8
    assert values_within_tolerance(fig_1855_yi, 18.55, "USD_bn")[0] is False
    assert values_within_tolerance(fig_1855_yi, 1.855, "USD_bn")[0] is True

    # --- Item 4: Currency must be explicit and matching (`$100bn` vs `100 billion`; HKD EPS vs USD) ---
    draft_cur_vs_count = (
        "FY2025 收入为 $100bn [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: prov:p-count\n"
    )
    out_cur_vs_count = match_figures(
        extract_figures(draft_cur_vs_count),
        provenance_records={"p-count": {"body": "FY2025 revenue was 100 billion."}},
    )
    assert out_cur_vs_count[0].matched is False

    fig_hkd_eps = extract_figures("FY2025 港元每股收益为 5.20 港元/股。")[0]
    assert fig_hkd_eps.currency == "HKD"
    assert values_within_tolerance(fig_hkd_eps, 5.20, "USD")[0] is False
    assert values_within_tolerance(fig_hkd_eps, 5.20, "USD_per_share")[0] is False
    assert values_within_tolerance(fig_hkd_eps, 5.20, "per_share")[0] is False
    assert values_within_tolerance(fig_hkd_eps, 5.20, "HKD")[0] is True
    assert values_within_tolerance(fig_hkd_eps, 5.20, "HKD_per_share")[0] is True

    draft_hkd_eps = (
        "FY2025 港元每股收益为 5.20 港元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-USD-EPS\n"
    )
    out_hkd_vs_usd = match_figures(
        extract_figures(draft_hkd_eps),
        thesis_doc={
            "evidence": [
                {
                    "id": "E-USD-EPS",
                    "fact": "FY2025 diluted EPS was $5.20.",
                    "figures": [
                        {"metric": "每股收益", "value": 5.20, "unit": "USD", "period": "FY2025"}
                    ],
                }
            ]
        },
    )
    assert out_hkd_vs_usd[0].matched is False

    # --- Item 5: Column header narrative numbers extracted per sentence + row/col markers don't bleed ---
    col_hdr_table_md = (
        "# 1. 利润率核验\n\n"
        "| 指标 | 目标[阈值]为 15%。实际 FY2025 营业利润率为 25%。 |\n"
        "|---|---|\n"
        "| 营业利润率[阈值] | 22.0% |\n"
    )
    hdr_figs = extract_figures(col_hdr_table_md)
    hdr_by_raw = {f.raw: f for f in hdr_figs}
    assert "15%" in hdr_by_raw and hdr_by_raw["15%"].exempt is True
    assert "25%" in hdr_by_raw and hdr_by_raw["25%"].exempt is False
    assert hdr_by_raw["25%"].period == "FY2025"
    assert "22.0%" in hdr_by_raw and hdr_by_raw["22.0%"].exempt is False


@pytest.mark.asyncio
async def test_round7_pct_stored_as_ratio_overrides_unit_labels_and_per_share_bound_currency() -> None:
    """Round 7 #1 & #2:
    1. `read_model_cell` returns `stored_as_ratio` directly on its read result (determined solely by Excel number format),
       never indirectly recovered from unit objects, and no unit label (sheet-level `单位：USD mn`, row label `亿美元`,
       or `key_outputs.unit`) can overwrite it. When `stored_as_ratio=True`, the cell is compared as a percentage
       (`0.21 -> 21%`), conflicting unit labels are invalidated, and the audit report records `"格式为百分比，单位标签不一致"`.
    2. Currency for a per-share amount comes ONLY from a currency token/word directly bound to that number or inside
       that number's own phrase — never by scanning the whole `fact`/paragraph text and picking by priority.
    """
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import (
        UNIT_FORMAT_CONFLICT_WARNING,
        read_model_cell,
    )
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    # --- Item 1(a): Sheet-level unit note ("单位：USD mn") + percentage format ("0.00%") + key_outputs.unit="pct" ---
    # --- Item 1(b): Row label with monetary unit ("营业利润率（亿美元）") + percentage format ("0.00%") ---
    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "A1": Cell("单位：USD mn", t="inlineStr"),
                "A10": Cell("营业利润率", t="inlineStr"),
                "B10": Cell(0.21, num_fmt="0.00%"),
                "A11": Cell("营业利润率（亿美元）", t="inlineStr"),
                "B11": Cell(0.21, num_fmt="0.00%"),
            }
        }
    )

    # Direct read_model_cell checks for both 1(a) B10 (sheet-level "单位：USD mn") and 1(b) B11 (row label "亿美元")
    res_b10 = read_model_cell(xlsx_bytes, "Summary", "B10")
    val_b10, unit_b10, err_b10 = res_b10
    assert val_b10 == pytest.approx(0.21)
    assert unit_b10 == "pct"
    assert err_b10 is None
    assert res_b10.stored_as_ratio is True
    assert res_b10.unit_conflict_warning == UNIT_FORMAT_CONFLICT_WARNING

    res_b11 = read_model_cell(xlsx_bytes, "Summary", "B11")
    val_b11, unit_b11, err_b11 = res_b11
    assert val_b11 == pytest.approx(0.21)
    assert unit_b11 == "pct"
    assert err_b11 is None
    assert res_b11.stored_as_ratio is True
    assert res_b11.unit_conflict_warning == UNIT_FORMAT_CONFLICT_WARNING

    class _BytesBackendProto:
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BytesBackendProto, instance=True)
    backend.adownload_file_bytes.return_value = xlsx_bytes

    models_doc = {
        "versions": [
            {
                "id": "v1",
                "path": "models/m.xlsx",
                "recalculated": True,
                "key_outputs": [
                    {"name": "营业利润率", "cell": "Summary!B10", "value": 0.21, "unit": "pct", "period": "FY2025"},
                    {"name": "营业利润率", "cell": "Summary!B11", "value": 0.21, "unit": "USD_100mn", "period": "FY2025"},
                ],
            }
        ]
    }

    # 1(a): Sheet-level "单位：USD mn" + 0.00% + key_outputs.unit="pct"
    draft_b10_wrong = (
        "FY2025 营业利润率为 0.21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    cells_b10 = await publish_svc._load_referenced_model_cells(
        backend, draft_b10_wrong, models_doc=models_doc
    )
    assert cells_b10["models/m.xlsx#Summary!B10"]["stored_as_ratio"] is True
    assert cells_b10["models/m.xlsx#Summary!B10"]["unit"] == "pct"
    assert cells_b10["models/m.xlsx#Summary!B10"]["unit_conflict_warning"] == UNIT_FORMAT_CONFLICT_WARNING

    audit_b10_wrong = await aaudit_draft(
        draft_b10_wrong,
        models_doc=models_doc,
        model_cells=cells_b10,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_b10_wrong.passed is False
    assert audit_b10_wrong.unverified_count == 1
    assert UNIT_FORMAT_CONFLICT_WARNING in render_audit_md(audit_b10_wrong)

    draft_b10_right = (
        "FY2025 营业利润率为 21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    audit_b10_right = await aaudit_draft(
        draft_b10_right,
        models_doc=models_doc,
        model_cells=cells_b10,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_b10_right.passed is True
    assert audit_b10_right.verified_count == 1
    assert UNIT_FORMAT_CONFLICT_WARNING in render_audit_md(audit_b10_right)

    # 1(b): Row label with monetary unit ("营业利润率（亿美元）") + 0.00%
    draft_b11_wrong_pct = (
        "FY2025 营业利润率为 0.21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B11\n"
    )
    cells_b11 = await publish_svc._load_referenced_model_cells(
        backend, draft_b11_wrong_pct, models_doc=models_doc
    )
    assert cells_b11["models/m.xlsx#Summary!B11"]["stored_as_ratio"] is True
    assert cells_b11["models/m.xlsx#Summary!B11"]["unit"] == "pct"
    assert cells_b11["models/m.xlsx#Summary!B11"]["unit_conflict_warning"] == UNIT_FORMAT_CONFLICT_WARNING

    out_b11_wrong_pct = match_figures(
        extract_figures(draft_b11_wrong_pct),
        models_doc=models_doc,
        model_cells=cells_b11,
    )
    assert out_b11_wrong_pct[0].matched is False

    draft_b11_wrong_money = (
        "FY2025 营业利润率为 0.21 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B11\n"
    )
    out_b11_wrong_money = match_figures(
        extract_figures(draft_b11_wrong_money),
        models_doc=models_doc,
        model_cells=cells_b11,
    )
    assert out_b11_wrong_money[0].matched is False

    draft_b11_right = (
        "FY2025 营业利润率为 21% [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B11\n"
    )
    audit_b11_right = await aaudit_draft(
        draft_b11_right,
        models_doc=models_doc,
        model_cells=cells_b11,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_b11_right.passed is True
    assert audit_b11_right.verified_count == 1
    assert UNIT_FORMAT_CONFLICT_WARNING in render_audit_md(audit_b11_right)

    # --- Item 2(a): Exact reproduction — `fact` has USD 5.20 EPS + HKD 100mn cash, `figures` has `unit="per_share"` ---
    thesis_usd_eps_hkd_cash = {
        "evidence": [
            {
                "id": "E-EPS-USD-CASH-HKD",
                "fact": "FY2025 diluted EPS was USD 5.20. Cash balance was HKD 100 million.",
                "figures": [
                    {"metric": "diluted EPS", "value": 5.20, "unit": "per_share", "period": "FY2025"}
                ],
            }
        ]
    }
    draft_eps_hkd_wrong = (
        "FY2025 每股收益 5.20 港元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-EPS-USD-CASH-HKD\n"
    )
    out_eps_hkd_wrong = match_figures(
        extract_figures(draft_eps_hkd_wrong),
        thesis_doc=thesis_usd_eps_hkd_cash,
    )
    assert out_eps_hkd_wrong[0].matched is False

    draft_eps_usd_right = (
        "FY2025 每股收益 5.20 美元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-EPS-USD-CASH-HKD\n"
    )
    out_eps_usd_right = match_figures(
        extract_figures(draft_eps_usd_right),
        thesis_doc=thesis_usd_eps_hkd_cash,
    )
    assert out_eps_usd_right[0].matched is True

    # --- Item 2(b): Currency-less EPS in a paragraph containing HKD -> currency is None (unknown), stays unverified ---
    thesis_no_cur_eps_hkd_cash = {
        "evidence": [
            {
                "id": "E-EPS-NOCUR-CASH-HKD",
                "fact": "FY2025 diluted EPS was 5.20. Cash balance was HKD 100 million.",
                "figures": [
                    {"metric": "diluted EPS", "value": 5.20, "unit": "per_share", "period": "FY2025"}
                ],
            }
        ]
    }
    # Check extracted prose figures directly: 5.20 has no currency bound to it
    prose_figs = extract_figures("FY2025 diluted EPS was 5.20. Cash balance was HKD 100 million.")
    fig_520 = next(f for f in prose_figs if abs(f.value - 5.20) < 1e-6)
    assert fig_520.currency is None

    out_nocur_vs_hkd = match_figures(
        extract_figures(
            "FY2025 每股收益 5.20 港元/股 [引用1]。\n\n"
            "## 引用\n"
            "[引用1]: evidence:E-EPS-NOCUR-CASH-HKD\n"
        ),
        thesis_doc=thesis_no_cur_eps_hkd_cash,
    )
    assert out_nocur_vs_hkd[0].matched is False
    assert out_nocur_vs_hkd[0].reason == "unverified"

    out_nocur_vs_usd = match_figures(
        extract_figures(
            "FY2025 每股收益 5.20 美元/股 [引用1]。\n\n"
            "## 引用\n"
            "[引用1]: evidence:E-EPS-NOCUR-CASH-HKD\n"
        ),
        thesis_doc=thesis_no_cur_eps_hkd_cash,
    )
    assert out_nocur_vs_usd[0].matched is False
    assert out_nocur_vs_usd[0].reason == "unverified"

    # Also test provenance body with currency-less per-share amount in same sentence/paragraph as HKD cash
    out_prov_nocur_vs_hkd = match_figures(
        extract_figures(
            "FY2025 每股收益 5.20 港元/股 [引用1]。\n\n"
            "## 引用\n"
            "[引用1]: prov:p-eps-nocur\n"
        ),
        provenance_records={
            "p-eps-nocur": {
                "body": "FY2025 diluted EPS was 5.20 per share, while cash balance was HKD 100 million."
            }
        },
    )
    assert out_prov_nocur_vs_hkd[0].matched is False
    assert out_prov_nocur_vs_hkd[0].reason == "unverified"


@pytest.mark.asyncio
async def test_round8_tight_currency_binding_and_model_cell_per_share_currency_preserved() -> None:
    """Round 8 #1 & #2:
    1. Currency is bound ONLY when tightly adjacent to the number (`$5.20`, `HK$5.20`, `US$5.20`, `¥5.20`,
       `USD 5.20`, `5.20 USD`, `5.20 美元`, `5.20 港元`, `5.20 元`, `5.20 港元/股`, `USD 5.2 billion`, `52 亿港元`).
       Window-based currency inference is removed:
       - `FY2025 diluted EPS was 5.20 and HKD cash totaled 100 million.` -> `5.20` has `currency=None`,
         and `"每股收益 5.20 港元/股"` stays `unverified` on both `evidence` and `prov` paths.
       - `HKD cash 100 million; EPS 5.20` -> both `100 million` and `5.20` have `currency=None`.
    2. `_load_referenced_model_cells` preserves `_PerShareUnit.currency` (and explicit `"currency"`):
       model row label `EPS (USD per share)` with value `5.20` verifies `"5.20 美元/股"` through
       `read_model_cell -> _load_referenced_model_cells -> match_figures / aaudit_draft`.
    """
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.match import read_model_cell
    from tests.unit.dalton._xlsx_builder import Cell, build_xlsx

    # --- Item 1(a): `FY2025 diluted EPS was 5.20 and HKD cash totaled 100 million.` ---
    src_and_hkd = "FY2025 diluted EPS was 5.20 and HKD cash totaled 100 million."
    figs_and = extract_figures(src_and_hkd)
    fig_520_and = next(f for f in figs_and if abs(f.value - 5.20) < 1e-6)
    fig_100_and = next(f for f in figs_and if abs(f.value - 100.0) < 1e-6)
    assert fig_520_and.currency is None
    assert getattr(fig_520_and.unit, "currency", None) is None
    assert fig_100_and.currency is None

    draft_hkd_eps_ev = (
        "FY2025 每股收益 5.20 港元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-AND-HKD\n"
    )
    out_ev_structured = match_figures(
        extract_figures(draft_hkd_eps_ev),
        thesis_doc={
            "evidence": [
                {
                    "id": "E-AND-HKD",
                    "fact": src_and_hkd,
                    "figures": [
                        {"metric": "diluted EPS", "value": 5.20, "unit": "per_share", "period": "FY2025"}
                    ],
                }
            ]
        },
    )
    assert out_ev_structured[0].matched is False
    assert out_ev_structured[0].reason == "unverified"

    out_ev_unstructured = match_figures(
        extract_figures(draft_hkd_eps_ev),
        thesis_doc={
            "evidence": [
                {
                    "id": "E-AND-HKD",
                    "fact": src_and_hkd,
                }
            ]
        },
    )
    assert out_ev_unstructured[0].matched is False
    assert out_ev_unstructured[0].reason == "unverified"

    draft_hkd_eps_prov = (
        "FY2025 每股收益 5.20 港元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: prov:p-and-hkd\n"
    )
    out_prov_and = match_figures(
        extract_figures(draft_hkd_eps_prov),
        provenance_records={"p-and-hkd": {"body": src_and_hkd}},
    )
    assert out_prov_and[0].matched is False
    assert out_prov_and[0].reason == "unverified"

    # --- Item 1(b): `HKD cash 100 million; EPS 5.20` ---
    src_semi_hkd = "HKD cash 100 million; EPS 5.20"
    figs_semi = extract_figures(src_semi_hkd)
    fig_100_semi = next(f for f in figs_semi if abs(f.value - 100.0) < 1e-6)
    fig_520_semi = next(f for f in figs_semi if abs(f.value - 5.20) < 1e-6)
    assert fig_100_semi.currency is None
    assert fig_520_semi.currency is None
    assert getattr(fig_520_semi.unit, "currency", None) is None

    out_semi_prov = match_figures(
        extract_figures(
            "每股收益 5.20 港元/股 [引用1]，现金 100 百万港元 [引用1]。\n\n"
            "## 引用\n"
            "[引用1]: prov:p-semi-hkd\n"
        ),
        provenance_records={"p-semi-hkd": {"body": src_semi_hkd}},
    )
    assert out_semi_prov[0].matched is False
    assert out_semi_prov[0].reason == "unverified"
    assert out_semi_prov[1].matched is False
    assert out_semi_prov[1].reason == "unverified"

    # --- Item 1(c): Tightly adjacent currency forms bind currency properly ---
    for expr, exp_cur in (
        ("$5.20", "USD"),
        ("HK$5.20", "HKD"),
        ("US$5.20", "USD"),
        ("¥5.20", "CNY"),
        ("USD 5.20", "USD"),
        ("5.20 USD", "USD"),
        ("5.20 美元", "USD"),
        ("5.20 港元", "HKD"),
        ("5.20 元", "CNY"),
        ("5.20 港元/股", "HKD"),
        ("USD 5.2 billion", "USD"),
        ("52 亿港元", "HKD"),
    ):
        extracted = extract_figures(f"FY2025 指标为 {expr}。")
        assert len(extracted) == 1, expr
        assert extracted[0].currency == exp_cur, expr

    # --- Item 2: Model row label `EPS (USD per share)` + value `5.20` full pipeline (read -> load -> verify) ---
    xlsx_bytes = build_xlsx(
        {
            "Summary": {
                "A10": Cell("EPS (USD per share)", t="inlineStr"),
                "B10": 5.20,
            }
        }
    )

    # Step 1: read_model_cell
    res_b10 = read_model_cell(xlsx_bytes, "Summary", "B10")
    val_b10, unit_b10, err_b10 = res_b10
    assert val_b10 == pytest.approx(5.20)
    assert unit_b10 == "per_share"
    assert getattr(unit_b10, "currency", None) == "USD"
    assert err_b10 is None
    assert res_b10.stored_as_ratio is False

    # Step 2: _load_referenced_model_cells
    class _BytesBackendProto:
        async def adownload_file_bytes(self, path: str) -> bytes | None: ...

    backend = create_autospec(_BytesBackendProto, instance=True)
    backend.adownload_file_bytes.return_value = xlsx_bytes

    models_doc = {
        "versions": [
            {
                "id": "v1",
                "path": "models/m.xlsx",
                "recalculated": True,
            }
        ]
    }
    draft_usd_eps = (
        "FY2025 每股收益为 5.20 美元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    loaded_cells = await publish_svc._load_referenced_model_cells(
        backend, draft_usd_eps, models_doc=models_doc
    )
    loaded_entry = loaded_cells["models/m.xlsx#Summary!B10"]
    assert getattr(loaded_entry["unit"], "currency", None) == "USD"
    assert loaded_entry["currency"] == "USD"

    # Step 3: match_figures & aaudit_draft
    out_usd_eps = match_figures(
        extract_figures(draft_usd_eps),
        models_doc=models_doc,
        model_cells=loaded_cells,
    )
    assert out_usd_eps[0].matched is True

    audit_usd_eps = await aaudit_draft(
        draft_usd_eps,
        models_doc=models_doc,
        model_cells=loaded_cells,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert audit_usd_eps.passed is True
    assert audit_usd_eps.verified_count == 1

    draft_hkd_eps_wrong = (
        "FY2025 每股收益为 5.20 港元/股 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: model:models/m.xlsx#Summary!B10\n"
    )
    out_hkd_eps_wrong = match_figures(
        extract_figures(draft_hkd_eps_wrong),
        models_doc=models_doc,
        model_cells=loaded_cells,
    )
    assert out_hkd_eps_wrong[0].matched is False










def test_hit_window_falls_back_to_an_equal_value_written_differently() -> None:
    """The draft writes 9.14 亿美元, the source writes $914 million: the window must
    centre on the source figure (this path used to read a nonexistent `.span`)."""
    from src.dalton.publish.match import _extract_hit_window
    from src.dalton.publish.numbers import extract_figures

    fig = extract_figures("Adjusted EBITDA 为 9.14 亿美元。\n")[0]
    body = ("背景资料。" * 400) + " Adjusted EBITDA was $914 million in the quarter. " + ("其他内容。" * 400)
    assert fig.raw not in body

    window = _extract_hit_window(body, fig, window=200)

    assert len(window) == 200
    assert "$914 million" in window


def test_hit_window_picks_the_matched_occurrence_not_the_first() -> None:
    """The same raw appears first in an exempt quote and again much later: the
    window must centre on the occurrence the matcher chose."""
    from src.dalton.publish.match import _extract_hit_window
    from src.dalton.publish.numbers import extract_figures

    fig = extract_figures("Adjusted EBITDA 为 9.14 亿美元。\n")[0]
    body = (
        "> 管理层原话：we expect $914 million next year.\n"
        + ("背景资料。" * 400)
        + "\nSECOND-HIT Adjusted EBITDA was $914 million in the quarter.\n"
        + ("其他内容。" * 400)
    )

    window = _extract_hit_window(body, fig, window=200)

    assert "SECOND-HIT" in window


@pytest.mark.parametrize("newline", ["\n", "\r", " "])
def test_hit_window_line_starts_follow_the_extractors_line_breaks(newline: str) -> None:
    from src.dalton.publish.match import _extract_hit_window
    from src.dalton.publish.numbers import extract_figures

    fig = extract_figures("Adjusted EBITDA 为 9.14 亿美元。\n")[0]
    body = (
        "> 管理层原话：we expect $914 million next year." + newline
        + ("背景资料。" * 400) + newline
        + "SECOND-HIT Adjusted EBITDA was $914 million in the quarter." + newline
        + ("其他内容。" * 400)
    )

    assert "SECOND-HIT" in _extract_hit_window(body, fig, window=200)


def test_hit_window_skips_an_exempt_duplicate_earlier_on_the_same_line() -> None:
    from src.dalton.publish.match import _extract_hit_window
    from src.dalton.publish.numbers import extract_figures

    fig = extract_figures("Adjusted EBITDA 为 9.14 亿美元。\n")[0]
    table = (
        "| 指标 | 假设值 | 实际 |\n|---|---|---|\n"
        "| EBITDA | [假设] $914 million " + ("占位" * 300) + " | SECOND-HIT $914 million |\n"
    )
    body = ("背景资料。" * 300) + "\n" + table + ("其他内容。" * 300)

    assert "SECOND-HIT" in _extract_hit_window(body, fig, window=200)
