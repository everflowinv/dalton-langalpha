"""Unit tests for Publication Governance, Figure Audit, Cross-Family Review, and Immutable Files (PK.8–PK.12)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from src.dalton.publish.audit import (
    audit_draft,
    render_annotated_draft,
    render_audit_md,
)
from src.dalton.publish.files import (
    _pick_non_colliding_path,
    compute_bundle_sha256,
    prepend_publication_banner,
)
from src.dalton.publish.match import match_figures, values_within_tolerance
from src.dalton.publish.numbers import (
    ExtractedFigure,
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


def test_normalize_period() -> None:
    assert normalize_period("FY26") == "FY2026"
    assert normalize_period("FY2027E") == "FY2027"
    assert normalize_period("Q3 2026") == "3Q26"
    assert normalize_period("3Q26E") == "3Q26"
    assert normalize_period("1H2026") == "1H26"


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

    # Excluded items (2026-10-15, 10-Q, 8-K, 3 个支柱, P1, D2, K1.1, disclaimer 100) must NOT be in non_exempt
    raw_tokens = [f.raw for f in non_exempt]
    assert "$59.4bn" in raw_tokens
    assert "27%" in raw_tokens
    assert "$850" in raw_tokens
    assert "$34.05" in raw_tokens
    assert "60.8" in raw_tokens
    assert "63.4" in raw_tokens
    assert "24.3" in raw_tokens
    assert "25.0" in raw_tokens
    assert len(non_exempt) == 8


def test_match_figures_all_orders_and_discrepancies() -> None:
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
    audit_ok = audit_draft(
        clean_draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        provenance_records=provenance_records,
    )
    assert audit_ok.passed, f"Unmatched: {audit_ok.unmatched}"
    assert audit_ok.matched == 8
    # Verify Order 5 derived figure (40.0% = 24.3 / 60.8) was matched as `derived`
    assert any(m["kind"] == "derived" and m["raw"] == "40.0%" for m in audit_ok.matches)

    # 2. Draft with specific discrepancies:
    # - unit_mismatch: 2Q26 广告收入 $59.4mn (million instead of billion)
    # - period_mismatch: 3Q26 广告收入 $59.4bn pointing to 2Q26 evidence
    # - source_conflict: 2Q26 营业收入 $68.9bn pointing to E-20261015-01 (which has 60.8)
    # - target price without model cell: 目标价 $900 pointing to evidence:E-20261015-01
    bad_draft = """# META 错误测试草稿

2Q26 广告收入为 $59.4mn[^e1]。
3Q26 广告收入为 $59.4bn[^e1]。
2Q26 营业收入为 $68.9bn[^e1]。
我们给予 FY26 目标价 $900[^e1]。

[^e1]: evidence:E-20261015-01
"""
    audit_bad = audit_draft(
        bad_draft,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        provenance_records=provenance_records,
    )
    assert not audit_bad.passed
    reasons_by_raw = {u["raw"]: u["reason"] for u in audit_bad.unmatched}
    assert reasons_by_raw["$59.4mn"] == "unit_mismatch"
    assert reasons_by_raw["$59.4bn"] == "period_mismatch"
    assert reasons_by_raw["$68.9bn"] == "source_conflict"
    assert reasons_by_raw["$900"] == "unverified"

    audit_md = render_audit_md(audit_bad, draft_path="drafts/meta.md")
    assert "unit_mismatch" in audit_md
    assert "period_mismatch" in audit_md
    assert "source_conflict" in audit_md

    annotated = render_annotated_draft(bad_draft, audit_bad)
    assert "**$59.4mn**⟨未核实⟩" in annotated
    assert "**$68.9bn**⟨未核实⟩" in annotated


def test_facts_ledger_order_0_matching() -> None:
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
    audit = audit_draft(draft, thesis_doc=thesis_doc, facts_markdown=facts_md)
    assert audit.passed
    assert audit.matched == 1


def test_cross_family_reviewer_selection_and_verdict() -> None:
    assert family_of("claude-opus-4-7") == "anthropic-claude"
    assert family_of("gpt-5.4") == "openai-gpt"
    assert family_of("gemini-3.1-pro") == "google-gemini"

    candidates = ["claude-opus-4-7", "gpt-5.4", "gemini-3.1-pro"]
    # Author is Claude -> picks GPT-5.4
    assert pick_reviewer_model("claude-sonnet-4-6", candidates=candidates) == "gpt-5.4"
    # Author is GPT -> picks Claude
    assert pick_reviewer_model("gpt-5.4", candidates=candidates) == "claude-opus-4-7"
    # Only same-family model available -> returns None (refuses to publish without cross-family reviewer)
    assert (
        pick_reviewer_model(
            "claude-opus-4-7",
            candidates=candidates,
            available_models={"claude-opus-4-7"},
        )
        is None
    )

    # ReviewVerdict with non-empty must_fix forces verdict='revise' in ReviewResult.to_dict()
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

    # First write takes base_path
    p1 = await _pick_non_colliding_path(base_path, sha_v1, backend)
    assert p1 == base_path
    backend.files[p1] = content_v1

    # Idempotent retry with identical SHA-256 reuses base_path
    p1_retry = await _pick_non_colliding_path(base_path, sha_v1, backend)
    assert p1_retry == base_path

    # Different content never overwrites; gets `-2` suffix
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
    from unittest.mock import create_autospec
    from src.dalton.llm.assist import AssistResult, assist_json
    from src.dalton.publish import numbers as numbers_mod
    from src.dalton.publish.audit import aaudit_draft

    # 1. SEC accession (0001326801-26-000042), doc code (10-K-2026-03), slash date (10/12),
    #    clock time (05:22), Discord ID (1485122334455667788), batch code (20261018-0412) masked (N2-M6)
    # 2. Heading `# 2Q26 复盘` keeps period `2Q26` (N2-H3)
    # 3. Cross-scale unit normalization: 512 亿美元 == $51.2B (51.2 USD_bn)
    # 4. Table header unit inheritance: 广告收入（亿美元） -> unit USD_bn, metric "广告收入"
    # 5. Colloquial Chinese numbers ("三成", "两成", "百分之三十", "腰斩", "过半") extracted via Step B (L28)
    draft = """# 2Q26 复盘（Filing 10-K-2026-03, Accession 0001326801-26-000042, 10/12 05:22, msg 1485122334455667788, batch 20261018-0412）

广告收入 512 亿美元，同比 +21%[^E0001]；核心 AI 广告转化率提升约三成[^E0001]，退订率下降两成[^E0001]，渗透率达百分之三十[^E0001]，硬件亏损腰斩[^E0001]，视频时长占比过半[^E0001]。

| 期间 | 广告收入（亿美元） | 同比增速 |
| :--- | :---: | :---: |
| 2Q26 | 512[^E0001] | +21%[^E0001] |
"""
    figs = extract_figures(draft)
    raws = [f.raw for f in figs]
    # Accession, doc codes, slash dates, clock times, Discord IDs, batch codes excluded
    assert raws == ["512 亿美元", "+21%", "512", "+21%"]
    # Heading `# 2Q26 复盘` propagates period 2Q26 to subsequent prose line
    prose_512 = next(f for f in figs if f.raw == "512 亿美元")
    assert prose_512.period == "2Q26"

    # Table cell 512 inherits unit USD_bn (scale 1e8) from column header 广告收入（亿美元）
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
    ) -> AssistResult:
        if task == "audit.label_figures":
            return AssistResult(
                ok=True,
                data={
                    "labels": [],
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
    res = await aaudit_draft(draft, thesis_doc=thesis_doc)
    assert res.passed, f"Unexpected failures: {res.unmatched}"
    assert res.labeling_incomplete is False
    # 4 code-extracted figures + 5 anchored colloquial Chinese figures = 9 verified (hallucinated extra_figure dropped)
    assert len(res.verified) == 9


def test_g31_provenance_rejects_wrong_metric_or_period_r4_3() -> None:
    provenance_records = {
        "prov-sec-001": {
            "provider": "sec_edgar",
            "identifier": "meta_10q",
            "source_type": "filing",
            "result_sha256": "deadbeef",
            "body": "In 1Q25, Reality Labs hardware revenue grew 21.0% YoY to $0.4B.",
        }
    }
    # Draft claims 2Q26 广告收入同比 +21.0% citing prov-sec-001 (which is 1Q25 Reality Labs!)
    draft = "2Q26 广告收入同比 +21.0%[^p1]。\n\n[^p1]: prov:prov-sec-001\n"
    res = audit_draft(draft, provenance_records=provenance_records)
    assert not res.passed
    assert len(res.unverified) + len(res.contradicted) == 1


@pytest.mark.asyncio
async def test_g31_llm_judge_reverification_and_uncertain_bucket_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec
    from src.dalton.llm.assist import AssistResult, assist_json
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import numbers as numbers_mod
    from src.dalton.publish.audit import aaudit_draft, verify_llm_judge_verdict

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

    # 1. Hallucinated quote not in candidate text -> rejected by verify_llm_judge_verdict
    bucket_1, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote": "This quote does not exist in source",
        },
        cand,
    )
    assert bucket_1 == "unverified"

    # 2. Quote exists verbatim in source ("Family of Apps ad revenue"), but contains NO number -> downgraded to uncertain (L29 / P0H §1.4 #1)
    bucket_2, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote": "Family of Apps ad revenue",
        },
        cand,
    )
    assert bucket_2 == "uncertain"

    # 3. Verbatim quote containing "$51.2B" -> code extracts $51.2B and confirms it matches 512 亿美元!
    bucket_3, _ = verify_llm_judge_verdict(
        fig,
        {
            "verdict": "supported",
            "source_id": "evidence:E0001",
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_3 == "verified"

    # 4. Contradicted verdict where quote is missing or unanchored -> downgraded to "unverified" (L29!)
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
    #     but quote number ($51.2B) differs from draft figure (600 亿美元 = $60.0B) -> MUST NOT be "verified" (P0H §1.7 #1)!
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
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_3b != "verified"
    assert bucket_3b == "uncertain"

    # 5b. Contradicted verdict where quote is anchored AND has a differing number -> accepted as "contradicted"
    bucket_4b, detail_4b = verify_llm_judge_verdict(
        fig_wrong,
        {
            "verdict": "contradicted",
            "source_id": "evidence:E0001",
            "quote": "ad revenue was $51.2B",
        },
        cand,
    )
    assert bucket_4b == "contradicted"
    assert detail_4b["reason"] == "source_conflict"

    # 5c. Single line with both consensus (with consensus footnote) and our own estimate/reported (with evidence footnote)
    #     must verify BOTH figures without whole-line consensus_confusion false positive (P0H §1.7 #1)!
    mixed_line_draft = (
        "2Q26 广告收入市场一致预期为 $50.0B[^p_cons]，而我们测算/实际录得 2Q26 广告收入为 $51.2B[^E0001]。\n\n"
        "[^p_cons]: prov:prov-yf-001\n"
    )
    mixed_res = audit_draft(
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
    )
    assert mixed_res.passed, f"Unexpected failures on mixed consensus+evidence line: {mixed_res.unmatched}"
    assert len(mixed_res.verified) == 2

    # 6. Exempt verdict without [估计]/[假设] marker in draft sentence -> downgraded to "unverified" (L29!)
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
    )
    assert res.labeling_incomplete is True
    assert len(res.uncertain) == 1
    assert len(res.contradicted) == 0
    assert len(res.unverified) == 0
    assert res.passed is True


def test_g31_realistic_report_false_positive_rate_at_most_2() -> None:
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
        "market_view": {
            "figures": [
                {"metric": "营业收入", "value": 63.4, "unit": "USD_bn", "period": "3Q26", "label": "consensus"},
                {"metric": "EPS", "value": 34.05, "unit": "per_share", "period": "FY2027", "label": "consensus"},
            ]
        },
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
    report_md = """# META 2026Q3 财报前瞻深度报告（Accession 0001326801-26-000042, Form 10-Q-2026-02）

我们在 2Q26 看到公司总收入达 608 亿美元[^E-20261015-01]，其中广告收入 594 亿美元，同比 +27%[^E-20261015-01]。
2Q26 营业利润为 $24.3B[^E-20261015-01]，对应营业利润率 40.0%，单季 Capex 为 135 亿美元[^E-20261015-01]。
市场一致预期 3Q26E 营业收入为 $63.4B，一致预期 FY2027 EPS 为 $34.05。
我们维持 FY26 目标价 $850[^m1]。

> "We expect AI infrastructure investment to remain significant in 2027, exceeding $100B." —— 管理层原话引用

| 指标 | 2Q26 |
| :--- | :---: |
| 总收入（亿美元） | 608[^E-20261015-01] |
| 广告收入（亿美元） | 594[^E-20261015-01] |
| 营业利润（亿美元） | 243[^E-20261015-01] |

[^m1]: model:model/meta_v1.xlsx#Summary!C10
"""
    res = audit_draft(report_md, thesis_doc=thesis_doc, models_doc=models_doc)
    false_positives = len(res.contradicted) + len(res.unverified)
    assert false_positives <= 2, f"Too many false positives ({false_positives}): {res.unmatched}"
    assert false_positives == 0


@pytest.mark.asyncio
async def test_g32_reviewer_refuses_none_or_unknown_author_model() -> None:
    from src.dalton.publish.review import run_review

    assert pick_reviewer_model(None) is None
    assert pick_reviewer_model("") is None
    assert pick_reviewer_model("unknown-custom-model-v1") is None

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


