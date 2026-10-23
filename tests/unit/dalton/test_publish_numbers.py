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
            "body": "META consensus estimates: 3Q26 revenue 63.4 bn, FY2027 EPS 34.05.",
        },
        "prov-news-002": {
            "provider": "reuters",
            "identifier": "news_article",
            "source_type": "news",
            "result_sha256": "111222",
            "body": "Analysts see FY2027 EPS around 34.05.",
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
    assert table_512[0].unit == "USD_bn"
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
                "body": "2Q26 ad revenue consensus 50.0 bn.",
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
            "body": "META consensus estimates: 3Q26 revenue 63.4 bn, FY2027 EPS 34.05.",
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
    assert values_within_tolerance(fig_eps, 19.11, "per_share")[0] is True

    # Dimension check: pct (19.1%) vs decimal ratio (0.191, unit="other") is True, but vs USD_bn (19.1) is False
    fig_pct = extract_figures("营业利润率为 19.1%。")[0]
    assert fig_pct.unit == "pct"
    assert values_within_tolerance(fig_pct, 0.191, "other")[0] is True
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

    assert len(EXEMPT_MARKERS) == 12
    skill_md = (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-publish" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for marker in EXEMPT_MARKERS:
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
    monkeypatch.setattr(audit_mod, "AUDIT_PROMPT_VERSION", "audit_v2")
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
    assert res4["audit"]["prompt_version"] == "audit_v2"
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
                "fact": "2Q26 revenue was 100B USD and operating profit was 20B USD (operating margin 20%).",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 100.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 20.0,
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
                "fact": "2Q26 revenue was 100B USD and operating profit was 20B USD (operating margin 20%).",
                "figures": [
                    {
                        "metric": "营业收入",
                        "value": 100.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                    {
                        "metric": "营业利润",
                        "value": 20.0,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    },
                ],
            },
            {
                "id": "E-20261023-02",
                "fact": "2Q26 capex was 15B USD.",
                "figures": [
                    {
                        "metric": "资本开支",
                        "value": 15.0,
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
    # Case 1: Prose uses [引用1], but definition is traditional footnote [^引用1]:
    draft_trad_def = (
        "# META 报告\n\n"
        "2Q26 广告收入为 594 亿美元 [引用1]。\n\n"
        "## 引用\n"
        "[^引用1]: evidence:E-20261023-01\n"
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
