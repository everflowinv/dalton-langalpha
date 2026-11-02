"""Deadline threading for the publish audit (A5): partial results at budget,
no journal failure rows past the deadline, quieter assist fallback logs."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.publish import audit as audit_mod
from src.dalton.publish.audit import (
    _collect_judge_candidates,
    _deadline_marked_outcomes,
)
from src.dalton.publish.match import MatchOutcome, match_figures
from src.dalton.publish.numbers import extract_figures


def _figure_with_metric(metric: str = "Revenue") -> Any:
    figs = extract_figures(f"报告期内 {metric} 为 $1,234 million。")
    assert figs, "fixture draft must extract at least one figure"
    return figs[0]


def test_match_figures_deadline_expired_marks_figures_incomplete() -> None:
    figs = extract_figures(
        "2024 财年 Revenue 为 $1,234 million，净利润为 $456 million。"
    )
    assert len(figs) >= 2
    outcomes = match_figures(figs, deadline=time.monotonic() - 1.0)
    assert len(outcomes) == len(figs)
    for out in outcomes:
        assert not out.matched
        assert out.reason == "uncertain"
        assert "核验未完成" in out.hint
        # Locked out of Step D: no judge batch may pick them up.
        assert out.lock_unverified


def test_match_figures_without_deadline_still_matches_and_collects() -> None:
    figs = extract_figures("2024 财年 Revenue 为 $1,234 million。")
    prov = {
        "p1": {
            "record_id": "p1",
            "source_type": "web_fetch",
            "identifier": "https://example.com/_results",
            "body": "The company reported Revenue of $1,234 million in FY2024.",
        }
    }
    outcomes = match_figures(figs, provenance_records=prov)
    # No footnote → candidates only; but the run must complete and classify.
    assert len(outcomes) == len(figs)
    assert all(out.reason != "uncertain" or "核验未完成" not in (out.hint or "") for out in outcomes)


def test_exempt_and_incidental_keep_their_outcome_at_deadline() -> None:
    figs = extract_figures("Revenue 为 $1,234 million，毛利率 45.6%，净利率 12.3%。")
    assert len(figs) >= 2
    figs[0].exempt = True
    figs[0].exempt_reason = "manual-exempt"
    figs[1].label_hint = "incidental"
    marked = _deadline_marked_outcomes(figs)
    assert len(marked) == len(figs)
    assert marked[0].matched and marked[0].kind == "exempt"
    assert marked[1].kind == "incidental" and not marked[1].matched
    assert "核验未完成" in marked[-1].hint
    assert marked[-1].lock_unverified


def test_collect_judge_candidates_stops_at_deadline() -> None:
    figs = extract_figures("2024 财年 Revenue 为 $1,234 million。")
    outcomes = [MatchOutcome(figure=f, matched=False, reason="unverified") for f in figs]
    pending_by_section, pending_cands = _collect_judge_candidates(
        outcomes,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger={},
        metric_aliases=None,
        deadline=time.monotonic() - 1.0,
    )
    assert pending_by_section == {}
    assert pending_cands == {}
    # Everything that should have been judged but was not reached carries the
    # 核验未完成 mark, locked out of any later judging pass — not a silent
    # "no match found".
    for out in outcomes:
        assert not out.matched
        assert out.reason == "uncertain"
        assert "核验未完成" in out.hint
        assert out.lock_unverified


def test_collect_judge_candidates_without_deadline_marks_nothing() -> None:
    figs = extract_figures("2024 财年 Revenue 为 $1,234 million。")
    outcomes = [MatchOutcome(figure=f, matched=False, reason="unverified") for f in figs]
    _collect_judge_candidates(
        outcomes,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger={},
        metric_aliases=None,
    )
    for out in outcomes:
        assert out.reason == "unverified"
        assert not out.lock_unverified


def test_mark_unjudged_incomplete_keeps_candidates_and_skips_settled() -> None:
    """The Step-D-deadline marker: judged/exempt/matched figures keep their
    outcome; pending ones turn 核验未完成 with their collected candidates."""
    from src.dalton.publish.audit import mark_unjudged_incomplete
    from src.dalton.publish.match import MatchCandidate

    figs = extract_figures("Revenue 为 $1,234 million，毛利率 45.6%。")
    matched = MatchOutcome(figure=figs[0], matched=True, kind="footnote", ref="f:1")
    pending = MatchOutcome(figure=figs[1], matched=False, reason="unverified")
    pending.candidates = [
        MatchCandidate(kind="prov", id="p1", value=1.0, unit="USD_mn", period="FY2024", why="candidate")
    ]
    outcomes = [matched, pending]
    mark_unjudged_incomplete(outcomes, judged_indices=set())
    assert outcomes[0] is matched and outcomes[0].matched
    assert not outcomes[1].matched
    assert "核验未完成" in outcomes[1].hint
    assert outcomes[1].lock_unverified
    assert outcomes[1].candidates  # kept for the PM


@pytest.mark.asyncio
async def test_finalize_timeout_keeps_judged_outcomes_and_flags_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed finalize must NOT re-run in-thread: the judged outcomes come
    back unchanged and the PM-facing stats say the derived pass was skipped."""
    from src.dalton.heavy_compute import HeavyComputeTimeout, run_in_heavy_subprocess
    from src.dalton.publish.audit import _finalize_outcomes

    async def _always_timeout(*args: Any, **kwargs: Any) -> Any:
        raise HeavyComputeTimeout("audit.finalize", 0.0)

    # Spec the real runner, not the conftest double already installed on the module.
    monkeypatch.setattr(
        audit_mod,
        "run_in_heavy_subprocess",
        create_autospec(run_in_heavy_subprocess, side_effect=_always_timeout),
    )
    figs = extract_figures("Revenue 为 $1,234 million。")
    outcomes = [MatchOutcome(figure=figs[0], matched=True, kind="footnote", ref="f:1")]
    stats: dict[str, Any] = {}
    result = await _finalize_outcomes(
        outcomes, pending_cands_by_idx=None, with_derived=True, stats=stats
    )
    assert result is outcomes  # no re-run, no mutation
    assert stats.get("derived_matching_incomplete") is True


@pytest.mark.asyncio
async def test_build_result_timeout_fails_the_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed result build must not re-run in-thread either: the audit fails
    with the explicit Chinese message."""
    from src.dalton.heavy_compute import HeavyComputeTimeout, run_in_heavy_subprocess
    from src.dalton.publish.audit import _build_result_isolated

    async def _always_timeout(*args: Any, **kwargs: Any) -> Any:
        raise HeavyComputeTimeout("audit.build", 0.0)

    monkeypatch.setattr(
        audit_mod,
        "run_in_heavy_subprocess",
        create_autospec(run_in_heavy_subprocess, side_effect=_always_timeout),
    )
    figs = extract_figures("Revenue 为 $1,234 million。")
    outcomes = [MatchOutcome(figure=figs[0], matched=True, kind="footnote", ref="f:1")]
    with pytest.raises(RuntimeError, match="审计结果汇总超时"):
        await _build_result_isolated("sha", figs, outcomes, False, {})


@pytest.mark.asyncio
async def test_dalton_complete_past_deadline_never_claims_or_fails_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 87-batch incident: past-deadline calls claimed journal rows and wrote
    DeadlineBeforeSendError failures. Now a past deadline returns a failed
    result without claiming; a journalled answer still replays."""
    from src.dalton.llm import complete as complete_mod

    inspect = create_autospec(
        complete_mod.inspect_journal_state, return_value=("proceed", None)
    )
    claim = create_autospec(
        complete_mod.claim_journal_invocation,
        return_value=("proceed", {"attempt": 1}),
    )
    fail = create_autospec(complete_mod.complete_journal_failed, return_value=None)
    monkeypatch.setattr(complete_mod, "inspect_journal_state", inspect)
    monkeypatch.setattr(complete_mod, "claim_journal_invocation", claim)
    monkeypatch.setattr(complete_mod, "complete_journal_failed", fail)
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        create_autospec(
            complete_mod.resolve_model_billing_and_provider,
            return_value=("oauth", "claude-oauth"),
        ),
    )

    result = await complete_mod.dalton_complete(
        user_id="u1",
        user_prompt="ping",
        model="some-oauth-model",
        deadline=time.monotonic() - 1.0,
    )
    assert result.ok is False
    assert result.status == "failed"
    assert result.error_kind == "deadline"
    claim.assert_not_called()
    fail.assert_not_called()


@pytest.mark.asyncio
async def test_assist_past_deadline_walks_fallbacks_as_cache_probe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the deadline the fallback chain still looks for journalled answers,
    but logs a cache probe — not a three-model failure switch."""
    import logging

    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.complete import DaltonCompleteResult

    def _deadline_result(**_: Any) -> DaltonCompleteResult:
        return DaltonCompleteResult(
            ok=False,
            status="failed",
            model="m",
            error_kind="deadline",
            error="deadline reached before the request was sent",
        )

    dbl = create_autospec(assist_mod.dalton_complete, side_effect=_deadline_result)
    monkeypatch.setattr(assist_mod, "dalton_complete", dbl)
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_model",
        create_autospec(assist_mod.resolve_assist_model, return_value="primary-model"),
    )
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_fallback_models",
        create_autospec(
            assist_mod.resolve_assist_fallback_models, return_value=["fb-a", "fb-b"]
        ),
    )

    with caplog.at_level(logging.INFO, logger="src.dalton.llm.assist"):
        res = await assist_mod.assist_json(
            "audit.judge",
            "instructions",
            {"value": 1},
            {"type": "object"},
            user_id="u1",
            fallback_models=["fb-a", "fb-b"],
            deadline=time.monotonic() - 1.0,
        )
    assert res.ok is False
    # Every fallback was probed (journal replay), not skipped…
    assert dbl.await_count == 3
    # …and the logs say cache probe, not "switching to assist_fallback".
    switching = [r for r in caplog.records if "switching to assist_fallback" in r.message]
    probing = [r for r in caplog.records if "probing journal cache only" in r.message]
    assert not switching
    assert len(probing) == 2


def test_audit_extract_and_match_task_shapes() -> None:
    """The combined child payloads keep their plain-data contracts: extract
    parses both draft and facts; match+collect returns the three-part tuple
    match phases hand to Step D."""
    draft = "2024 财年 Revenue 为 $1,234 million。"
    facts = "| 指标 | 值 |\n| --- | --- |\n| Revenue | 1234 |\n"
    extracted, ledger = audit_mod._extract_task(draft, facts)
    assert extracted and extracted[0].metric
    assert isinstance(ledger, dict)

    outcomes, by_section, cands = audit_mod._match_and_collect_task(
        extracted,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger=ledger,
        metric_aliases=None,
        evidence_ref_bodies=None,
        draft_path="d.md",
        match_deadline=None,
        collect_candidates=True,
    )
    assert len(outcomes) == len(extracted)
    assert isinstance(by_section, dict) and isinstance(cands, dict)


def test_audit_md_shows_derived_matching_incomplete_warning() -> None:
    """The finalize-timeout flag must reach the PM-facing sidecar body."""
    from src.dalton.publish.audit import AuditResult, render_audit_md

    figs = extract_figures("Revenue 为 $1,234 million。")

    def _result(derived_flag: bool) -> AuditResult:
        return AuditResult(
            draft_sha256="sha",
            figures=1,
            matched=0,
            exempt=0,
            unmatched=[
                {"id": "F-001", "raw": figs[0].raw, "line": 1, "metric": "Revenue"}
            ],
            labeling={"derived_matching_incomplete": derived_flag},
        )

    flagged = render_audit_md(_result(True), draft_path="d.md")
    assert "派生匹配因超时未完成" in flagged
    assert "仍显示为未核实" in flagged

    clean = render_audit_md(_result(False), draft_path="d.md")
    assert "派生匹配因超时未完成" not in clean


def test_match_figures_expired_deadline_still_verifies_footnoted_and_derived_figures() -> None:
    """Even when `deadline` has already expired before `match_figures` starts,
    Pass 1 still deterministically matches footnoted figures (and Order 5 derived
    figures from them) and catches footnoted conflicts, while marking only
    unfootnoted figures as 核验未完成."""
    draft = (
        "# 阳光电源 3Q26 更新\n\n"
        "2Q26 广告收入为 $59.4bn [引用1]，2Q26 营业收入为 $60.8bn [引用1]，"
        "2Q26 营业利润为 $24.3bn [引用1]，对应营业利润率 40.0%。\n"
        "2Q26 净利润为 $99.9bn [引用1]。\n"
        "无脚注的 2Q26 研发费用为 $11.2bn，无脚注的 2Q26 资本开支为 $8.7bn。\n\n"
        "## 引用\n"
        "[引用1]: evidence:E-20261015-01\n"
    )
    thesis_doc = {
        "evidence": [
            {
                "id": "E-20261015-01",
                "fact": "2Q26 ad revenue $59.4B, total revenue $60.8B, operating income $24.3B, net income $15.0B",
                "figures": [
                    {"metric": "广告收入", "value": 59.4, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "营业收入", "value": 60.8, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "营业利润", "value": 24.3, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                    {"metric": "净利润", "value": 15.0, "unit": "USD_bn", "period": "2Q26", "label": "reported"},
                ],
            }
        ]
    }
    figs = extract_figures(draft)
    outcomes = match_figures(
        figs,
        thesis_doc=thesis_doc,
        deadline=time.monotonic() - 5.0,
    )
    by_raw = {o.figure.raw: o for o in outcomes}
    assert by_raw["$59.4bn"].matched is True
    assert by_raw["$60.8bn"].matched is True
    assert by_raw["$24.3bn"].matched is True
    assert by_raw["40.0%"].matched is True
    assert by_raw["40.0%"].kind == "derived"
    # Conflict is still caught — standards are never relaxed
    assert by_raw["$99.9bn"].matched is False
    assert by_raw["$99.9bn"].reason == "source_conflict"
    # Only the unfootnoted figures are marked 核验未完成 due to the expired deadline
    assert by_raw["$11.2bn"].matched is False
    assert by_raw["$11.2bn"].reason == "uncertain"
    assert "截止时间已到" in (by_raw["$11.2bn"].hint or "")
    assert by_raw["$8.7bn"].matched is False
    assert by_raw["$8.7bn"].reason == "uncertain"


@pytest.mark.asyncio
async def test_long_draft_footnoted_figures_verified_and_conflicts_caught_when_llm_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long report with many footnoted figures (`f:`, `model:`, `evidence:`, `prov:`)
    and a slow LLM stub:
    1) Deterministically matchable footnoted figures are verified before Step B and
       never sent to Step B LLM labeling;
    2) Footnoted conflicts are still caught as `source_conflict`;
    3) Unfootnoted figures that time out during Step B/C/D are reported with an
       explicit `timed_out` count and `因为超时，N 个数字未判断` message + advice."""
    import asyncio
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish import numbers as numbers_mod
    from src.dalton.publish.audit import aaudit_draft, render_audit_md

    monkeypatch.setattr(numbers_mod, "_LABEL_DEADLINE_GRACE_S", 0.02)
    monkeypatch.setattr(audit_mod, "_DEADLINE_GRACE_S", 0.02)

    facts_lines = [
        "<!-- FACTS-LEDGER -->",
        "| id | metric | period | value | unit | ref |",
        "|---|---|---|---|---|---|",
    ]
    draft_sections: list[str] = ["# 阳光电源 3Q26 深度更新报告\n"]
    citation_lines: list[str] = ["## 引用"]

    def _alpha_tag(n: int) -> str:
        a = chr(ord("A") + ((n - 1) // 26))
        b = chr(ord("A") + ((n - 1) % 26))
        return f"{a}{b}"

    # 80 footnoted figures backed by facts.md (f:F001 .. f:F080)
    for i in range(1, 81):
        val = round(100.0 + i * 1.25, 2)
        fid = f"F{i:03d}"
        cit_idx = i
        m_name = f"分部{_alpha_tag(i)}营收"
        facts_lines.append(
            f"| {fid} | {m_name} | 3Q26 | {val} | USD_mn | SEC 10-Q |"
        )
        draft_sections.append(
            f"3Q26 {m_name}达到 ${val}mn [引用{cit_idx}]。"
        )
        citation_lines.append(f"[引用{cit_idx}]: f:{fid}")

    # 1 footnoted conflict against facts.md
    facts_lines.append("| F_BAD | 核心逆变器毛利率 | 3Q26 | 31.5 | pct | SEC 10-Q |")
    facts_lines.append("<!-- END -->")
    draft_sections.append("3Q26 核心逆变器毛利率达到 45.0% [引用81]。")
    citation_lines.append("[引用81]: f:F_BAD")

    # 20 footnoted figures backed by model cells + 1 footnoted model conflict
    model_key_outputs: list[dict[str, Any]] = []
    for j in range(1, 21):
        mval = round(50.0 + j * 2.5, 1)
        cell_ref = f"Summary!C{j + 1}"
        cit_idx = 81 + j
        m_name = f"区域{_alpha_tag(j)}预测营业利润"
        model_key_outputs.append(
            {
                "name": m_name,
                "cell": cell_ref,
                "value": mval,
                "unit": "USD_bn",
                "period": "FY2026",
            }
        )
        draft_sections.append(
            f"FY2026 {m_name}为 ${mval}bn [引用{cit_idx}]。"
        )
        citation_lines.append(
            f"[引用{cit_idx}]: model:model/sungrow_v1.xlsx#{cell_ref}"
        )

    model_key_outputs.append(
        {
            "name": "目标价",
            "cell": "Summary!Z99",
            "value": 120.0,
            "unit": "USD",
            "period": "FY2026",
        }
    )
    draft_sections.append("我们给予 FY2026 目标价 $180 [引用102]。")
    citation_lines.append("[引用102]: model:model/sungrow_v1.xlsx#Summary!Z99")

    # 12 unfootnoted figures in a separate section that require LLM Step B / Step D
    draft_sections.append("\n## 行业背景与未引注讨论\n")
    for k in range(1, 13):
        uval = round(300.0 + k * 3.0, 1)
        m_name = f"行业样本{_alpha_tag(k)}营收"
        draft_sections.append(f"2025 年{m_name}约为 ${uval}mn。")

    draft_md = "\n".join(draft_sections) + "\n\n" + "\n".join(citation_lines) + "\n"
    facts_md = "\n".join(facts_lines) + "\n"
    models_doc = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/sungrow_v1.xlsx",
                "sha256": "abc12345",
                "recalculated": True,
                "key_outputs": model_key_outputs,
            }
        ],
    }

    seen_step_b_fig_ids: list[str] = []

    async def _slow_llm_assist(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> AssistResult:
        if task == "audit.label_figures":
            for f_item in payload.get("extracted_figures", []):
                seen_step_b_fig_ids.append(str(f_item.get("raw") or f_item.get("id")))
        await asyncio.sleep(0.5)
        return AssistResult(ok=False, status="error", error="Step B/D timed out")

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_slow_llm_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    res = await aaudit_draft(
        draft_md,
        models_doc=models_doc,
        facts_markdown=facts_md,
        use_llm_judge=True,
        user_id="local-dev-user",
        deadline=time.monotonic() + 0.08,
    )

    # All 100 valid footnoted figures (80 facts.md + 20 model cells) are verified
    assert res.matched == 100
    # Both footnoted conflicts (45.0% vs 31.5% in facts.md, $180 vs $120 in model) are caught
    assert len(res.contradicted) == 2
    conflict_raws = {c["raw"] for c in res.contradicted}
    assert conflict_raws == {"45.0%", "$180"}

    # Pre-matched footnoted figures were never sent to Step B LLM labeling
    assert len(seen_step_b_fig_ids) == 12
    assert res.labeling.get("pre_matched") == 102

    # Only the 12 unfootnoted figures timed out and were marked uncertain due to deadline
    assert len(res.uncertain) == 12
    assert res.timed_out_count == 12
    audit_dict = res.to_dict()
    assert audit_dict["timed_out"] == 12
    assert len(audit_dict["timed_out_items"]) == 12

    summary = res.summary_line("drafts/sungrow.audit.md")
    assert "因超时未能判断 12" in summary
    assert "超时未能判断的数字" in summary
    assert "拆分报告后重试" in summary

    audit_md = render_audit_md(res, draft_path="drafts/sungrow.md")
    assert "有 12 个数字因超时未能判断" in audit_md
    assert "拆分报告后重试" in audit_md


@pytest.mark.asyncio
async def test_run_check_draft_auto_loads_facts_md_and_reports_timeout_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_check_draft` auto-discovers `facts.md` (including the 5-column / JSON
    format produced by `scripts/facts.py`) and preserves deterministic matches when
    the LLM phase times out."""
    import asyncio
    from src.dalton import auth as dalton_auth
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish import numbers as numbers_mod
    from src.dalton.publish import service as publish_svc

    monkeypatch.setattr(numbers_mod, "_LABEL_DEADLINE_GRACE_S", 0.02)
    monkeypatch.setattr(audit_mod, "_DEADLINE_GRACE_S", 0.02)

    facts_md = (
        "# Apple Inc. (AAPL) — 权威事实台账\n\n"
        '<!-- FACTS-LEDGER {"ledger": [{"key": "rev_3q26", "label": "营业收入", "value": "94.9 亿美元", "period": "3Q26", "source": "10-Q"}, {"key": "gm_3q26", "label": "毛利率", "value": "46.2%", "period": "3Q26", "source": "10-Q"}]} -->\n\n'
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_3q26 | 营业收入 | 94.9 亿美元 | 3Q26 | 10-Q |\n"
        "| gm_3q26 | 毛利率 | 46.2% | 3Q26 | 10-Q |\n"
    )
    draft_md = (
        "# AAPL 3Q26 点评\n\n"
        "3Q26 营业收入为 94.9 亿美元 [引用1]，3Q26 毛利率为 46.2% [引用2]。\n"
        "管理层电话会提到 3Q26 渠道库存周转天数为 18.5 天。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_3q26\n"
        "[引用2]: f:gm_3q26\n"
    )

    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    files_store: dict[str, str] = {
        "drafts/aapl.md": draft_md,
        "drafts/aapl.facts.md": facts_md,
    }
    backend = create_autospec(SandboxBackend, instance=True)
    backend.aread_text.side_effect = lambda p: files_store.get(p)
    backend.awrite_text.side_effect = lambda p, c: files_store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access, return_value={"workspace_id": "ws-aapl"}
        ),
    )

    async def _slow_assist(*args: Any, **kwargs: Any) -> AssistResult:
        await asyncio.sleep(0.3)
        return AssistResult(ok=False, status="error", error="timeout")

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_slow_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    cfg = publish_svc.get_dalton_config()
    monkeypatch.setattr(cfg.publish, "audit_total_timeout_s", 0.05)

    res = await publish_svc.run_check_draft(
        workspace_id="ws-aapl",
        draft_path="drafts/aapl.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={
            "p1": {
                "record_id": "p1",
                "source_type": "transcript",
                "identifier": "call",
                "body": "3Q26 渠道库存周转天数为 18.5 天。",
            }
        },
        use_llm_judge=True,
        user_id="local-dev-user",
    )
    assert res["ok"] is True
    assert res["audit"]["matched"] == 2
    assert res["audit"]["timed_out"] == 1
    assert "超时未能判断的数字" in res["message"]
    assert "因超时未能判断 1" in res["message"]


def test_parse_facts_ledger_preserves_scales_and_currencies() -> None:
    """Item 1: `_parse_facts_value_and_unit` and `parse_facts_ledger` must preserve scale
    and currency ('5 万美元' -> 50,000 USD) across 万, 亿, 千亿, 万亿, mn, bn, and compare
    via `values_within_tolerance`."""
    from src.dalton.publish.match import match_figures, values_within_tolerance
    from src.dalton.publish.numbers import extract_figures, parse_facts_ledger

    facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| k_wan_usd | 研发补贴 | 5 万美元 | FY2025 | 10-K |\n"
        "| k_yi_cny | 营业收入 | 12.5 亿元 | FY2025 | 年报 |\n"
        "| k_qianyi_cny | 总资产 | 3 千亿元 | FY2025 | 年报 |\n"
        "| k_wanyi_cny | 平台GMV | 1.8 万亿元 | FY2025 | 年报 |\n"
        "| k_mn_usd | 海外收入 | $250mn | FY2025 | 10-K |\n"
        "| k_bn_usd | 总市值 | $4.5bn | FY2025 | 10-K |\n"
        "<!-- END -->\n"
    )
    ledger = parse_facts_ledger(facts_md)

    assert ledger["k_wan_usd"]["value"] == pytest.approx(50_000.0)
    assert ledger["k_wan_usd"]["unit"] == "USD"
    assert ledger["k_wan_usd"]["currency"] == "USD"

    assert ledger["k_yi_cny"]["value"] == pytest.approx(12.5)
    assert ledger["k_yi_cny"]["unit"] == "CNY_100mn"
    assert ledger["k_yi_cny"]["currency"] == "CNY"

    assert ledger["k_qianyi_cny"]["value"] == pytest.approx(3e11)
    assert ledger["k_qianyi_cny"]["unit"] == "CNY"
    assert ledger["k_qianyi_cny"]["currency"] == "CNY"

    assert ledger["k_wanyi_cny"]["value"] == pytest.approx(1.8e12)
    assert ledger["k_wanyi_cny"]["unit"] == "CNY"
    assert ledger["k_wanyi_cny"]["currency"] == "CNY"

    assert ledger["k_mn_usd"]["value"] == pytest.approx(250.0)
    assert ledger["k_mn_usd"]["unit"] == "USD_mn"
    assert ledger["k_mn_usd"]["currency"] == "USD"

    assert ledger["k_bn_usd"]["value"] == pytest.approx(4.5)
    assert ledger["k_bn_usd"]["unit"] == "USD_bn"
    assert ledger["k_bn_usd"]["currency"] == "USD"

    draft = (
        "FY2025 研发补贴为 5 万美元[^1]，FY2025 营业收入为 12.5 亿元[^2]，"
        "FY2025 总资产为 3 千亿元[^3]，FY2025 平台GMV为 1.8 万亿元[^4]，"
        "FY2025 海外收入为 $250mn[^5]，FY2025 总市值为 $4.5bn[^6]。\n\n"
        "[^1]: f:k_wan_usd\n"
        "[^2]: f:k_yi_cny\n"
        "[^3]: f:k_qianyi_cny\n"
        "[^4]: f:k_wanyi_cny\n"
        "[^5]: f:k_mn_usd\n"
        "[^6]: f:k_bn_usd\n"
    )
    figs = extract_figures(draft)
    assert len(figs) == 6
    # Verify "5 万美元" vs "$500" does NOT false-match, and "5 万美元" vs 50,000 USD matches
    fig_wan = figs[0]
    ok_exact, _, _ = values_within_tolerance(
        fig_wan,
        ledger["k_wan_usd"]["value"],
        ledger["k_wan_usd"]["unit"],
        src_currency=ledger["k_wan_usd"]["currency"],
    )
    assert ok_exact is True
    ok_wrong, _, _ = values_within_tolerance(fig_wan, 5.0, "USD", src_currency="USD")
    assert ok_wrong is False

    outcomes = match_figures(figs, facts_ledger=ledger)
    assert all(o.matched for o in outcomes)


@pytest.mark.asyncio
async def test_facts_ledger_broken_delegated_ref_and_cycle_locked_unverified() -> None:
    """Items 2 & 7: When a facts row has an explicit `evidence:` / `model:` / `prov:` ref
    and the target collection is empty or the ref is missing, it must remain `lock_unverified`
    (never falling back to comparing the ledger's own value). Cyclic `f:` delegation chains
    must also terminate as `lock_unverified`."""
    facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| id | metric | period | value | unit | ref |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| F_MISS_EV | 营业收入 | 3Q26 | 94.9 | USD_bn | evidence:MISSING |\n"
        "| F_MISS_MOD | 目标价 | 3Q26 | 120.0 | USD | model:valuation.xlsx#DCF!B10 |\n"
        "| F_MISS_PROV | 毛利率 | 3Q26 | 46.2 | % | prov:missing-prov-id |\n"
        "| F_CYC_A | 净利润 | 3Q26 | 15.0 | USD_bn | f:F_CYC_B |\n"
        "| F_CYC_B | 净利润 | 3Q26 | 15.0 | USD_bn | f:F_CYC_A |\n"
        "<!-- END -->\n"
    )
    draft = (
        "3Q26 营业收入为 $94.9bn[^1]，3Q26 目标价为 $120.0[^2]，"
        "3Q26 毛利率为 46.2%[^3]，3Q26 净利润为 $15.0bn[^4]。\n\n"
        "[^1]: f:F_MISS_EV\n"
        "[^2]: f:F_MISS_MOD\n"
        "[^3]: f:F_MISS_PROV\n"
        "[^4]: f:F_CYC_A\n"
    )
    res = await audit_mod.aaudit_draft(
        draft,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        model_cells={},
        provenance_records={},
        facts_markdown=facts_md,
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res.matched == 0
    assert len(res.unverified) == 4
    assert all(out.lock_unverified for out in res.outcomes)
    assert any("MISSING" in (out.hint or "") for out in res.outcomes)
    assert any("循环引用" in (out.hint or "") for out in res.outcomes)


@pytest.mark.asyncio
async def test_deterministic_facts_resolution_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 3:
    1. Explicit footnote paths (`research/2026-11-01-coA-fc/facts.md#rev_25`, `research/2026-11-01-coB-fc/facts.md#rev_25`)
       bind each citation to its own file (dual-company comparison works without ambiguity);
       missing file or missing key in that file is locked unverified and NEVER falls back to another ledger.
    2. Bare `f:<key>` uses the single default ledger in exact priority order:
       (a) explicit `facts_path` / `role='facts'` attachment ->
       (b) convention (`drafts/<D>-memo.md` -> `research/<D>-fc/facts.md`, `drafts/<slug>.md` -> `drafts/<slug>.facts.md`) ->
       (c) `<draft_dir>/facts.md`.
    3. Workspace root `facts.md` is NEVER auto-loaded, and missing default ledger prompts
       `没有找到 facts 台账，请传入 facts_path`."""
    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-test"},
        ),
    )

    facts_co_a = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 100 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    facts_co_b = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 250 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    facts_explicit_override = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 380 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )

    # 1. Dual-company explicit footnote paths bind to their respective files; missing path/key does not fall back
    draft_dual = (
        "# 双公司对比报告\n\n"
        "甲公司 FY2025 营业收入为 100 亿元 [引用1]，乙公司 FY2025 营业收入为 250 亿元 [引用2]。\n"
        "丙公司 FY2025 营业收入为 100 亿元 [引用3]，甲公司另一指标 FY2025 营业收入为 100 亿元 [引用4]。\n\n"
        "## 引用\n"
        "[引用1]: research/2026-11-01-coA-fc/facts.md#rev_25\n"
        "[引用2]: research/2026-11-01-coB-fc/facts.md#rev_25\n"
        "[引用3]: research/2026-11-01-missing-fc/facts.md#rev_25\n"
        "[引用4]: research/2026-11-01-coA-fc/facts.md#missing_key\n"
    )
    store_dual: dict[str, str] = {
        "research/2026-11-01-coA-fc/facts.md": facts_co_a,
        "research/2026-11-01-coB-fc/facts.md": facts_co_b,
        "drafts/dual.facts.md": facts_co_a,
        "drafts/dual.md": draft_dual,
    }
    backend_dual = create_autospec(SandboxBackend, instance=True)
    backend_dual.aread_text.side_effect = lambda p: store_dual.get(p)
    backend_dual.awrite_text.side_effect = lambda p, c: store_dual.__setitem__(p, c)

    res_dual = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/dual.md",
        backend=backend_dual,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_dual["ok"] is True
    assert res_dual["audit"]["matched"] == 2
    assert res_dual["audit"]["unverified"] == 2
    assert res_dual["audit"]["contradicted"] == 0

    # 2. Convention path: `drafts/<D>-memo.md` -> `research/<D>-fc/facts.md`
    draft_memo = (
        "# 深度覆盖报告\n\n"
        "FY2025 营业收入为 250 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    store_conv: dict[str, str] = {
        "facts.md": facts_co_a,
        "research/2026-11-01-fc/facts.md": facts_co_b,
        "drafts/2026-11-01-memo.md": draft_memo,
    }
    backend_conv = create_autospec(SandboxBackend, instance=True)
    backend_conv.aread_text.side_effect = lambda p: store_conv.get(p)
    backend_conv.awrite_text.side_effect = lambda p, c: store_conv.__setitem__(p, c)

    res_conv = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/2026-11-01-memo.md",
        backend=backend_conv,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_conv["audit"]["matched"] == 1
    assert "默认事实台账：research/2026-11-01-fc/facts.md" in res_conv["message"]

    # 3. Explicit `facts_path` takes priority over convention path and `<draft_dir>/facts.md`
    draft_exp = (
        "# 深度覆盖报告\n\n"
        "FY2025 营业收入为 380 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    store_exp: dict[str, str] = {
        "custom/override_facts.md": facts_explicit_override,
        "research/2026-11-01-fc/facts.md": facts_co_b,
        "drafts/facts.md": facts_co_a,
        "drafts/2026-11-01-memo.md": draft_exp,
    }
    backend_exp = create_autospec(SandboxBackend, instance=True)
    backend_exp.aread_text.side_effect = lambda p: store_exp.get(p)
    backend_exp.awrite_text.side_effect = lambda p, c: store_exp.__setitem__(p, c)

    res_exp = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/2026-11-01-memo.md",
        facts_path="custom/override_facts.md",
        backend=backend_exp,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_exp["audit"]["matched"] == 1
    assert "默认事实台账：custom/override_facts.md" in res_exp["message"]

    # 4. Workspace root `facts.md` is NEVER auto-loaded when convention / draft_dir files do not exist
    store_root_only: dict[str, str] = {
        "facts.md": facts_co_b,
        "drafts/2026-11-01-memo.md": draft_memo,
    }
    backend_root = create_autospec(SandboxBackend, instance=True)
    backend_root.aread_text.side_effect = lambda p: store_root_only.get(p)
    backend_root.awrite_text.side_effect = lambda p, c: store_root_only.__setitem__(p, c)

    res_root = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/2026-11-01-memo.md",
        backend=backend_root,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_root["audit"]["matched"] == 0
    assert res_root["audit"]["unverified"] == 1
    assert "没有找到默认事实台账" in res_root["message"]


@pytest.mark.asyncio
async def test_pre_match_defers_inherited_or_relative_period_to_step_b_and_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4:
    1. Heading says `FY2026` while body prose says '上年营业收入为 80 亿元 [引用1]' — Step A inherits
       `FY2026` from the heading, which must NOT be pre-settled before Step B corrects the period to `FY2025`.
    2. Period can only be determined from the preceding sentence ('2025年公司经营稳健。期内营业收入为 80 亿元 [引用1]') —
       must NOT be pre-settled as verified before Step B confirms the period."""
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish import numbers as numbers_mod

    facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 80 亿元 | FY2025 | 年报 |\n"
        "| rev_26 | 营业收入 | 80 亿元 | FY2026 | 年报 |\n"
        "<!-- END -->\n"
    )

    # Case A: Heading has FY2026, prose says "上年营业收入为 80 亿元 [引用1]" pointing to f:rev_25 (or f:rev_26).
    # Before Step B, Step A extracts period="FY2026" (inherited from heading).
    # Pre-match must NOT settle it as verified against f:rev_26 or contradicted against f:rev_25;
    # Step B labels it as period="FY2025", and Step C then matches f:rev_25 and flags f:rev_26!
    draft_heading_relative = (
        "## FY2026 业绩展望\n\n"
        "上年营业收入为 80 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    extracted_a = numbers_mod.extract_figures(draft_heading_relative)
    assert len(extracted_a) == 1
    assert extracted_a[0].period == "FY2026"
    assert extracted_a[0].location.get("period_explicit") is False

    settled_a, cands_a = audit_mod._pre_label_match_task(
        extracted_a,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger=numbers_mod.parse_facts_ledger(facts_md),
        metric_aliases=None,
        evidence_ref_bodies=None,
        draft_path="draft.md",
    )
    # Not pre-settled before Step B because period came from heading + relative shift "上年"
    assert extracted_a[0].id not in settled_a

    # Also verify that if it pointed to f:rev_26 (which has period FY2026), it is still NOT pre-settled as matched!
    # And verify Item 5: MatchCandidate carries the ledger's true value (80.0) and unit ('CNY_100mn'), not fig's own value/unit.
    draft_heading_wrong_ref = (
        "## FY2026 业绩展望\n\n"
        "上年营业收入为 0.008 万亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_26\n"
    )
    extracted_a2 = numbers_mod.extract_figures(draft_heading_wrong_ref)
    settled_a2, cands_a2 = audit_mod._pre_label_match_task(
        extracted_a2,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger=numbers_mod.parse_facts_ledger(facts_md),
        metric_aliases=None,
        evidence_ref_bodies=None,
        draft_path="draft.md",
    )
    assert extracted_a2[0].id not in settled_a2
    assert extracted_a2[0].id in cands_a2
    assert cands_a2[extracted_a2[0].id][0].value == pytest.approx(80.0)
    assert cands_a2[extracted_a2[0].id][0].unit == "CNY_100mn"

    # Case B: Period only determinable from preceding sentence ("2025年公司经营稳健。期内营业收入为 80 亿元 [引用1]。")
    draft_prev_sent = (
        "2025年公司经营稳健。期内营业收入为 80 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    extracted_b = numbers_mod.extract_figures(draft_prev_sent)
    assert len(extracted_b) == 1
    assert extracted_b[0].location.get("period_explicit") is False
    settled_b, cands_b = audit_mod._pre_label_match_task(
        extracted_b,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger=numbers_mod.parse_facts_ledger(facts_md),
        metric_aliases=None,
        evidence_ref_bodies=None,
        draft_path="draft.md",
    )
    assert extracted_b[0].id not in settled_b
    assert extracted_b[0].id in cands_b

    # Now run full `aaudit_draft` where Step B LLM resolves the period to "FY2025",
    # and Step C settles it as matched!
    async def _fake_label_assist(*args: Any, **kwargs: Any) -> AssistResult:
        return AssistResult(
            ok=True,
            status="ok",
            data={
                "items": [
                    {
                        "id": "F-001",
                        "kind": "reported",
                        "metric": "营业收入",
                        "period": "FY2025",
                        "incidental_category": "",
                    }
                ],
                "extra_figures": [],
            },
        )

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_fake_label_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    res_a = await audit_mod.aaudit_draft(
        draft_heading_relative,
        facts_markdown=facts_md,
        use_llm_judge=True,
        user_id="local-dev-user",
    )
    assert res_a.matched == 1
    assert mock_assist.call_count == 1


def test_audit_cache_invalidated_by_algo_version_and_timeout_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: `AUDIT_ALGO_VERSION` is part of the config fingerprint, and cached audits
    containing timed-out unjudged items are never reused."""
    from src.dalton.publish.audit import (
        AuditResult,
        compute_audit_config_fingerprint,
        compute_audit_inputs_sha256,
        render_audit_md,
        try_load_reusable_audit_from_md,
    )

    monkeypatch.setenv("DALTON_AUDIT_CACHE_KEY", "test-hmac-secret-key")
    fp_v9 = compute_audit_config_fingerprint(use_llm_judge=True)
    monkeypatch.setattr(audit_mod, "AUDIT_ALGO_VERSION", "audit_v8_old")
    fp_old = compute_audit_config_fingerprint(use_llm_judge=True)
    assert fp_v9[3] != fp_old[3]
    monkeypatch.setattr(audit_mod, "AUDIT_ALGO_VERSION", "audit_v9")

    draft = "3Q26 营业收入为 $94.9bn。"
    inputs_sha = compute_audit_inputs_sha256(draft_markdown=draft)
    eff_model, _fallbacks, eff_prompt_ver, cfg_sha = fp_v9

    timed_out_item = {
        "id": "F-001",
        "line": 1,
        "raw": "$94.9bn",
        "value": 94.9,
        "unit": "USD_bn",
        "metric": "营业收入",
        "period": "3Q26",
        "reason": "uncertain",
        "hint": "核验未完成（截止时间已到）— 交由 PM 决定",
    }
    timed_out_res = AuditResult(
        draft_sha256=audit_mod.hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        figures=1,
        matched=0,
        exempt=0,
        unmatched=[timed_out_item],
        matches=[],
        contradicted=[],
        unverified=[],
        uncertain=[timed_out_item],
        incidental=[],
        outcomes=[],
        inputs_sha256=inputs_sha,
        config_sha256=cfg_sha,
        audit_model=eff_model,
        prompt_version=eff_prompt_ver,
    )
    audit_md = render_audit_md(timed_out_res, draft_path="draft.md")
    reused = try_load_reusable_audit_from_md(
        audit_md,
        draft_markdown=draft,
        use_llm_judge=True,
        config_fingerprint=fp_v9,
    )
    assert reused is None

    # Clean audit is reusable under v9, but invalidated if AUDIT_ALGO_VERSION changes
    clean_res = AuditResult(
        draft_sha256=audit_mod.hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        figures=1,
        matched=1,
        exempt=0,
        unmatched=[],
        matches=[
            {
                "id": "F-001",
                "line": 1,
                "raw": "$94.9bn",
                "value": 94.9,
                "unit": "USD_bn",
                "metric": "营业收入",
                "period": "3Q26",
                "kind": "facts",
                "ref": "rev_3q26",
                "excerpt": "营业收入 (3Q26): 94.9 USD_bn",
            }
        ],
        contradicted=[],
        unverified=[],
        uncertain=[],
        incidental=[],
        outcomes=[],
        inputs_sha256=inputs_sha,
        config_sha256=cfg_sha,
        audit_model=eff_model,
        prompt_version=eff_prompt_ver,
    )
    clean_audit_md = render_audit_md(clean_res, draft_path="draft.md")
    assert (
        try_load_reusable_audit_from_md(
            clean_audit_md,
            draft_markdown=draft,
            use_llm_judge=True,
            config_fingerprint=fp_v9,
        )
        is not None
    )
    assert (
        try_load_reusable_audit_from_md(
            clean_audit_md,
            draft_markdown=draft,
            use_llm_judge=True,
            config_fingerprint=fp_old,
        )
        is None
    )


def test_parse_facts_ledger_table_headers_na_skip_and_stop_boundary() -> None:
    """Item 6:
    - Generate a standard facts doc with `N/A` value via `scripts/facts.py` (`build_default_facts_doc`)
      and assert that the `N/A` key (`inv_days`) does not appear in `parse_facts_ledger`.
    - 5-column format with `period` column before `value` column (`| id | metric | period | value | ref |`)
      parses accurately.
    - Ledger table parsing stops at table end or next `#` heading and does not ingest free-form tables."""
    import importlib.util
    from pathlib import Path
    from src.dalton.publish.numbers import parse_facts_ledger

    facts_script_path = (
        Path(__file__).resolve().parents[3]
        / "plugins/dalton/skills/dalton-full-coverage/scripts/facts.py"
    )
    spec = importlib.util.spec_from_file_location("dalton_facts_script", facts_script_path)
    assert spec is not None and spec.loader is not None
    facts_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(facts_script)

    rows = [
        {
            "key": "rev_fy25",
            "label": "营业收入",
            "value": "$12.5B",
            "period": "FY2025",
            "source": "10-K",
        },
        {
            "key": "inv_days",
            "label": "存货周转天数",
            "value": "N/A（纯软件无存货）",
            "period": "FY2025",
            "source": "SaaS 行业惯例",
        },
    ]
    doc_with_na = (
        facts_script.build_default_facts_doc(rows, title="Acme Corp (ACME) — 事实与数字底表")
        + "\n## 二、业务与财务底稿\n"
        + "| 分部 | 收入 | 同比 | 占比 | 驱动与备注 |\n"
        + "| --- | --- | --- | --- | --- |\n"
        + "| 核心产品A | $8.0B | 25% | 64% | 企业客户 |\n"
    )
    assert "inv_days" in doc_with_na
    assert "N/A（纯软件无存货）" in doc_with_na
    parsed_default = parse_facts_ledger(doc_with_na)
    assert "inv_days" not in parsed_default
    assert "rev_fy25" in parsed_default
    assert parsed_default["rev_fy25"]["value"] == pytest.approx(12.5)
    assert parsed_default["rev_fy25"]["unit"] == "USD_bn"
    # Ensure free-form section tables under '## 二、业务与财务底稿' were NOT parsed into ledger
    assert "核心产品A" not in parsed_default

    # 5-column table with period before value, followed by a heading and another table
    five_col_period_first = (
        "<!-- FACTS-LEDGER -->\n"
        "| id | metric | period | value | ref |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| k_rev | 营业收入 | FY2025 | $12.5B | 10-K |\n"
        "| k_na | 存货周转天数 | FY2025 | N/A | 10-K |\n"
        "\n"
        "## 二、附表（非台账）\n"
        "| id | metric | period | value | ref |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| k_outside | 外部表格指标 | FY2025 | $99.9B | 10-K |\n"
    )
    parsed_five = parse_facts_ledger(five_col_period_first)
    assert "k_rev" in parsed_five
    assert parsed_five["k_rev"]["period"] == "FY2025"
    assert parsed_five["k_rev"]["value"] == pytest.approx(12.5)
    assert parsed_five["k_rev"]["unit"] == "USD_bn"
    assert "k_na" not in parsed_five
    assert "k_outside" not in parsed_five


@pytest.mark.asyncio
async def test_deferred_relative_period_not_upgraded_to_verified_when_step_b_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 1 (Phenomenon A):
    Heading says `FY2026`, body says `上年营业收入为 80 亿元 [引用1]` pointing to `f:rev_26` (period FY2026, 80 亿元).
    Pre-match defers settlement; when Step B times out, Step C must NOT upgrade the figure to `已核实` (`matched=True`),
    and must count it in `timed_out` (`因超时未判断`). Deterministic conflicts and broken refs still report as usual."""
    import asyncio
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish import numbers as numbers_mod

    monkeypatch.setattr(numbers_mod, "_LABEL_DEADLINE_GRACE_S", 0.02)
    monkeypatch.setattr(audit_mod, "_DEADLINE_GRACE_S", 0.02)

    facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_26 | 营业收入 | 80 亿元 | FY2026 | 年报 |\n"
        "| gm_26 | 毛利率 | 45.0% | FY2026 | 年报 |\n"
        "<!-- END -->\n"
    )
    draft = (
        "## FY2026 业绩展望\n\n"
        "上年营业收入为 80 亿元 [引用1]。\n"
        "毛利率为 30.0% [引用2]，净利润为 10 亿元 [引用3]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_26\n"
        "[引用2]: f:gm_26\n"
        "[引用3]: f:missing_key\n"
    )

    async def _slow_assist(*args: Any, **kwargs: Any) -> AssistResult:
        await asyncio.sleep(0.3)
        return AssistResult(ok=False, status="error", error="timeout")

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_slow_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    res = await audit_mod.aaudit_draft(
        draft,
        facts_markdown=facts_md,
        use_llm_judge=True,
        user_id="local-dev-user",
        deadline=time.monotonic() + 0.05,
    )
    # 80 亿元 [引用1] must NOT be verified; it is counted in timed_out
    assert res.matched == 0
    assert res.timed_out_count == 1
    assert any("80 亿元" in u["raw"] for u in res.timed_out_items)
    # Deterministic conflict (30.0% vs 45.0%) and broken ref (f:missing_key) still reported
    assert len(res.contradicted) == 1
    assert len(res.unverified) == 1


@pytest.mark.asyncio
async def test_comma_separated_following_period_not_treated_as_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 1 (Phenomenon B):
    In `营业收入为 80 亿元 [引用1]，FY2026 指引收入为 100 亿元`, `80 亿元` must NOT grab `FY2026`
    from after the comma as `period_explicit=True` and must NOT be pre-settled as verified before Step B."""
    import asyncio
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.assist import AssistResult
    from src.dalton.publish import numbers as numbers_mod

    monkeypatch.setattr(numbers_mod, "_LABEL_DEADLINE_GRACE_S", 0.02)
    monkeypatch.setattr(audit_mod, "_DEADLINE_GRACE_S", 0.02)

    facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_26 | 营业收入 | 80 亿元 | FY2026 | 年报 |\n"
        "<!-- END -->\n"
    )
    draft = (
        "营业收入为 80 亿元 [引用1]，FY2026 指引收入为 100 亿元。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_26\n"
    )
    figs = numbers_mod.extract_figures(draft)
    fig_80 = next(f for f in figs if "80" in f.raw)
    assert fig_80.location.get("period_explicit") is False
    assert fig_80.period == ""

    settled, _ = audit_mod._pre_label_match_task(
        figs,
        thesis_doc=None,
        models_doc=None,
        model_cells=None,
        provenance_records=None,
        facts_ledger=numbers_mod.parse_facts_ledger(facts_md),
        metric_aliases=None,
        evidence_ref_bodies=None,
        draft_path="draft.md",
    )
    assert fig_80.id not in settled

    async def _slow_assist(*args: Any, **kwargs: Any) -> AssistResult:
        await asyncio.sleep(0.3)
        return AssistResult(ok=False, status="error", error="timeout")

    mock_assist = create_autospec(assist_mod.assist_json, side_effect=_slow_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", mock_assist)
    monkeypatch.setattr(audit_mod, "assist_json", mock_assist)

    res = await audit_mod.aaudit_draft(
        draft,
        facts_markdown=facts_md,
        use_llm_judge=True,
        user_id="local-dev-user",
        deadline=time.monotonic() + 0.05,
    )
    assert res.matched == 0
    assert any("80 亿元" in u["raw"] for u in res.timed_out_items)


@pytest.mark.asyncio
async def test_publish_state_machine_retry_with_audit_to_dict_int_uncertain_reaudits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 2:
    When retrying a request in `audit_failed` or `review_revise` whose stored `audit` is a real
    `AuditResult(...).to_dict()` (where `audit['uncertain']` is an `int` and details are in `uncertain_items`),
    `_run_publish_state_machine` must not raise `TypeError` and must re-audit."""
    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.dalton.publish.audit import AuditResult
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    draft_md = "# AAPL 3Q26\n\n3Q26 营业收入为 $94.9bn。\n"
    store: dict[str, str] = {"drafts/aapl.md": draft_md}
    backend = create_autospec(SandboxBackend, instance=True)
    backend.aread_text.side_effect = lambda p: store.get(p)
    backend.awrite_text.side_effect = lambda p, c: store.__setitem__(p, c)

    cur_inputs_sha = publish_svc.compute_audit_inputs_sha256(
        draft_markdown=draft_md,
        facts_markdown=None,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        model_cells={},
        provenance_records={},
        evidence_ref_bodies={},
    )
    _, _, _, cur_cfg_sha = await publish_svc.resolve_audit_config_fingerprint(
        "local-dev-user", use_llm_judge=True
    )

    prev_audit_obj = AuditResult(
        draft_sha256="abc123",
        figures=2,
        matched=0,
        exempt=0,
        unmatched=[
            {
                "id": "F-001",
                "line": 3,
                "raw": "$94.9bn",
                "value": 94.9,
                "unit": "USD_bn",
                "metric": "营业收入",
                "period": "3Q26",
                "reason": "unverified",
                "hint": "未核实",
            }
        ],
        matches=[],
        contradicted=[],
        unverified=[
            {
                "id": "F-001",
                "line": 3,
                "raw": "$94.9bn",
                "value": 94.9,
                "unit": "USD_bn",
                "metric": "营业收入",
                "period": "3Q26",
                "reason": "unverified",
                "hint": "未核实",
            }
        ],
        uncertain=[
            {
                "id": "F-002",
                "line": 3,
                "raw": "$10.0bn",
                "value": 10.0,
                "unit": "USD_bn",
                "metric": "净利润",
                "period": "3Q26",
                "reason": "uncertain",
                "hint": "核验未完成（截止时间已到）— 交由 PM 决定",
            }
        ],
        incidental=[],
        outcomes=[],
        inputs_sha256=cur_inputs_sha,
        config_sha256=cur_cfg_sha,
    )
    real_audit_dict = prev_audit_obj.to_dict()
    assert isinstance(real_audit_dict["uncertain"], int)
    assert real_audit_dict["uncertain"] == 1

    existing_row = {
        "publish_request_id": "req-retry-1",
        "workspace_id": "ws-aapl",
        "thread_id": "t-1",
        "tool_call_id": "tc-1",
        "status": "audit_failed",
        "title": "AAPL 3Q26",
        "kind": "earnings_note",
        "slug": "aapl-3q26",
        "draft_path": "drafts/aapl.md",
        "updates_thesis": False,
        "thesis_version_no": None,
        "attachments": [],
        "bundle_sha256": publish_svc.compute_bundle_sha256(draft_md.encode("utf-8"), []),
        "audit": real_audit_dict,
    }

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-aapl", "user_id": "local-dev-user"},
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={
                "company": {"version_no": 1, "content": {"ticker": "AAPL", "name": "Apple Inc."}},
                "thesis": {"version_no": 1, "content": {"evidence": []}},
                "models": {"version_no": 1, "content": {"versions": []}},
            },
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(
            db_publications.get_or_create_request,
            return_value=(existing_row, False),
        ),
    )
    mock_transition = create_autospec(
        db_publications.transition,
        side_effect=lambda req_id, from_st, to_st, **kw: {
            **existing_row,
            "status": to_st,
            **{k: v for k, v in kw.items() if v is not None},
        },
    )
    monkeypatch.setattr(db_publications, "transition", mock_transition)
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )

    fresh_audit = AuditResult(
        draft_sha256="def456",
        figures=1,
        matched=0,
        exempt=0,
        unmatched=prev_audit_obj.unmatched,
        matches=[],
        contradicted=[],
        unverified=prev_audit_obj.unverified,
        uncertain=[],
        incidental=[],
        outcomes=[],
    )
    mock_aaudit = create_autospec(publish_svc.aaudit_draft, return_value=fresh_audit)
    monkeypatch.setattr(publish_svc, "aaudit_draft", mock_aaudit)

    for retry_status in ("audit_failed", "review_revise"):
        existing_row["status"] = retry_status
        res = await publish_svc._run_publish_state_machine(
            user_id="local-dev-user",
            workspace_id="ws-aapl",
            thread_id="t-1",
            tool_call_id="tc-1",
            draft_path="drafts/aapl.md",
            title="AAPL 3Q26",
            kind="earnings_note",
            slug="aapl-3q26",
            backend=backend,
        )
        assert res["status"] == "audit_failed"
    assert mock_aaudit.call_count == 2


def test_parse_facts_ledger_header_unit_parens_year_int_and_positional_fallback() -> None:
    """Round 3 Item 4:
    1. Table headers with parenthetical units (`数值（亿元）`, `value (USD mn)`) match the `value` column
       and apply the parenthetical unit to rows.
    2. A row with bare integer `2000` in the `value` column is parsed as `2000.0` (not dropped by the prose
       1900–2099 standalone-year filter).
    3. When table headers cannot be recognized by `_parse_facts_table_header`, `parse_facts_ledger` falls
       back to positional column parsing instead of dropping the table."""
    from src.dalton.publish.numbers import parse_facts_ledger

    md_parens_cny = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值（亿元） | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 80 | FY2025 | 年报 |\n"
        "| emp_25 | 研发人员数 | 2000 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    parsed_cny = parse_facts_ledger(md_parens_cny)
    assert "rev_25" in parsed_cny
    assert parsed_cny["rev_25"]["value"] == pytest.approx(80.0)
    assert parsed_cny["rev_25"]["unit"] == "CNY_100mn"
    assert parsed_cny["rev_25"]["currency"] == "CNY"
    assert "emp_25" in parsed_cny
    assert parsed_cny["emp_25"]["value"] == pytest.approx(2000.0)

    md_parens_usd = (
        "<!-- FACTS-LEDGER -->\n"
        "| id | metric | period | value (USD mn) | ref |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_us | Revenue | FY2025 | 250 | 10-K |\n"
        "| store_cnt | Store Count | FY2025 | 2000 | 10-K |\n"
        "<!-- END -->\n"
    )
    parsed_usd = parse_facts_ledger(md_parens_usd)
    assert "rev_us" in parsed_usd
    assert parsed_usd["rev_us"]["value"] == pytest.approx(250.0)
    assert parsed_usd["rev_us"]["unit"] == "USD_mn"
    assert parsed_usd["rev_us"]["currency"] == "USD"
    assert "store_cnt" in parsed_usd
    assert parsed_usd["store_cnt"]["value"] == pytest.approx(2000.0)

    # Positional fallback when headers are unrecognized
    md_unknown_headers = (
        "<!-- FACTS-LEDGER -->\n"
        "| col_a | col_b | col_c | col_d | col_e |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| k_pos_1 | 营业收入 | 95 亿元 | FY2025 | 年报 |\n"
        "| k_pos_2 | 毛利 | FY2025 | 40 亿元 | 年报 |\n"
        "<!-- END -->\n"
    )
    parsed_pos = parse_facts_ledger(md_unknown_headers)
    assert "k_pos_1" in parsed_pos
    assert parsed_pos["k_pos_1"]["value"] == pytest.approx(95.0)
    assert parsed_pos["k_pos_1"]["unit"] == "CNY_100mn"
    assert parsed_pos["k_pos_1"]["period"] == "FY2025"
    assert "k_pos_2" in parsed_pos
    assert parsed_pos["k_pos_2"]["value"] == pytest.approx(40.0)
    assert parsed_pos["k_pos_2"]["unit"] == "CNY_100mn"
    assert parsed_pos["k_pos_2"]["period"] == "FY2025"


@pytest.mark.asyncio
async def test_root_facts_md_hash_ref_and_scoped_delegation_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 1:
    - Top-level `facts.md#rev` binds strictly to workspace root `facts.md`; when root `facts.md`
      does not exist, it is marked as a broken reference (`lock_unverified=True`) even if the
      default ledger has `rev`.
    - Inside a scoped ledger (`research/A/facts.md`), `f:<key>` delegation stays bound to
      `research/A/facts.md`, whereas `facts.md#<key>` delegation resolves against workspace root `facts.md`."""
    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-test"},
        ),
    )

    default_ledger_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev | 营业收入 | 100 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    scoped_a_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| target_k | 营业收入 | 100 亿元 | FY2025 | 年报 |\n"
        "| del_local | 营业收入 | 100 亿元 | FY2025 | f:target_k |\n"
        "| del_root | 净利润 | 30 亿元 | FY2025 | facts.md#target_k |\n"
        "<!-- END -->\n"
    )
    root_facts_md = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| target_k | 净利润 | 30 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )

    # Part A: Top-level `facts.md#rev` when root `facts.md` does NOT exist -> broken ref even though default ledger has `rev`
    draft_top_root_ref = (
        "# 报告\n\n"
        "FY2025 营业收入为 100 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: facts.md#rev\n"
    )
    store_no_root: dict[str, str] = {
        "drafts/2026-11-01-memo.md": draft_top_root_ref,
        "research/2026-11-01-fc/facts.md": default_ledger_md,
    }
    backend_no_root = create_autospec(SandboxBackend, instance=True)
    backend_no_root.aread_text.side_effect = lambda p: store_no_root.get(p)
    backend_no_root.awrite_text.side_effect = lambda p, c: store_no_root.__setitem__(p, c)

    res_no_root = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/2026-11-01-memo.md",
        backend=backend_no_root,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_no_root["audit"]["matched"] == 0
    assert res_no_root["audit"]["unverified"] == 1
    assert "facts.md#rev" in res_no_root["audit"]["unverified_items"][0]["hint"]

    # Part B: Inside scoped ledger `research/A/facts.md`, contrast `f:target_k` vs `facts.md#target_k`
    draft_scoped_del = (
        "# 报告\n\n"
        "FY2025 营业收入为 100 亿元 [引用1]，FY2025 净利润为 30 亿元 [引用2]。\n\n"
        "## 引用\n"
        "[引用1]: research/A/facts.md#del_local\n"
        "[引用2]: research/A/facts.md#del_root\n"
    )
    # B1: When root `facts.md` exists, `del_local` (`f:target_k`) resolves in `research/A/facts.md` (100 亿元)
    #     and `del_root` (`facts.md#target_k`) resolves in root `facts.md` (30 亿元)
    store_with_root: dict[str, str] = {
        "drafts/report.md": draft_scoped_del,
        "research/A/facts.md": scoped_a_md,
        "facts.md": root_facts_md,
    }
    backend_with_root = create_autospec(SandboxBackend, instance=True)
    backend_with_root.aread_text.side_effect = lambda p: store_with_root.get(p)
    backend_with_root.awrite_text.side_effect = lambda p, c: store_with_root.__setitem__(p, c)

    res_with_root = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/report.md",
        backend=backend_with_root,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_with_root["audit"]["matched"] == 2
    assert res_with_root["audit"]["unverified"] == 0

    # B2: When root `facts.md` does NOT exist, `del_local` (`f:target_k`) still matches `research/A/facts.md#target_k`,
    #     while `del_root` (`facts.md#target_k`) is a broken reference (`unverified == 1`)
    store_without_root: dict[str, str] = {
        "drafts/report.md": draft_scoped_del,
        "research/A/facts.md": scoped_a_md,
    }
    backend_without_root = create_autospec(SandboxBackend, instance=True)
    backend_without_root.aread_text.side_effect = lambda p: store_without_root.get(p)
    backend_without_root.awrite_text.side_effect = lambda p, c: store_without_root.__setitem__(p, c)

    res_without_root = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/report.md",
        backend=backend_without_root,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_without_root["audit"]["matched"] == 1
    assert res_without_root["audit"]["unverified"] == 1
    assert "facts.md#target_k" in res_without_root["audit"]["unverified_items"][0]["hint"]


@pytest.mark.asyncio
async def test_explicit_facts_path_ref_never_falls_back_to_draft_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 2:
    Citing `research/A/facts.md#rev` resolves strictly relative to workspace root and is read once;
    when `research/A/facts.md` does not exist while `drafts/research/A/facts.md` exists, it must be
    marked as a broken reference (`unverified == 1`)."""
    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-test"},
        ),
    )

    draft_md = (
        "# 报告\n\n"
        "FY2025 营业收入为 100 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: research/A/facts.md#rev\n"
    )
    facts_in_draft_dir = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev | 营业收入 | 100 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    read_calls: list[str] = []
    store: dict[str, str] = {
        "drafts/report.md": draft_md,
        "drafts/research/A/facts.md": facts_in_draft_dir,
    }

    def _read_text(p: str) -> str | None:
        read_calls.append(p)
        return store.get(p)

    backend = create_autospec(SandboxBackend, instance=True)
    backend.aread_text.side_effect = _read_text
    backend.awrite_text.side_effect = lambda p, c: store.__setitem__(p, c)

    res = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="drafts/report.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert "drafts/research/A/facts.md" not in read_calls
    assert read_calls.count("research/A/facts.md") == 1
    assert res["audit"]["matched"] == 0
    assert res["audit"]["unverified"] == 1


@pytest.mark.asyncio
async def test_multiple_facts_attachments_keep_per_path_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 3:
    When multiple `role='facts'` attachments are provided with different values for the same key,
    the default ledger is the first existing attachment and uses its own content (not overwritten
    by the last attachment)."""
    from src.dalton import auth as dalton_auth
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import service as publish_svc
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    draft_md = (
        "# AAPL 3Q26\n\n"
        "FY2025 营业收入为 100 亿元 [引用1]，FY2025 毛利率为 30.0% [引用2]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
        "[引用2]: f:missing_gm\n"
    )
    facts_att_1 = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 100 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    facts_att_2 = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 999 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    store: dict[str, str] = {
        "drafts/aapl.md": draft_md,
        "attachments/facts_first.md": facts_att_1,
        "attachments/facts_second.md": facts_att_2,
    }
    backend = create_autospec(SandboxBackend, instance=True)
    backend.aread_text.side_effect = lambda p: store.get(p)
    backend.adownload_file_bytes.side_effect = (
        lambda p: store[p].encode("utf-8") if p in store else None
    )
    backend.awrite_text.side_effect = lambda p, c: store.__setitem__(p, c)

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-aapl", "user_id": "local-dev-user"},
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={
                "company": {"version_no": 1, "content": {"ticker": "AAPL", "name": "Apple Inc."}},
                "thesis": {"version_no": 1, "content": {"evidence": []}},
                "models": {"version_no": 1, "content": {"versions": []}},
            },
        ),
    )
    req_row = {
        "publish_request_id": "req-multi-att",
        "workspace_id": "ws-aapl",
        "thread_id": "t-1",
        "tool_call_id": "tc-1",
        "status": "auditing",
        "title": "AAPL 3Q26",
        "kind": "earnings_note",
        "slug": "aapl-3q26",
        "draft_path": "drafts/aapl.md",
        "updates_thesis": False,
        "thesis_version_no": None,
        "attachments": [
            {"path": "attachments/facts_first.md", "role": "facts"},
            {"path": "attachments/facts_second.md", "role": "facts"},
        ],
    }
    monkeypatch.setattr(
        db_publications,
        "get_or_create_request",
        create_autospec(
            db_publications.get_or_create_request,
            return_value=(req_row, True),
        ),
    )
    monkeypatch.setattr(
        db_publications,
        "transition",
        create_autospec(
            db_publications.transition,
            side_effect=lambda req_id, from_st, to_st, **kw: {
                **req_row,
                "status": to_st,
                **{k: v for k, v in kw.items() if v is not None},
            },
        ),
    )
    monkeypatch.setattr(
        publish_svc,
        "_load_workspace_provenance",
        create_autospec(publish_svc._load_workspace_provenance, return_value={}),
    )

    res = await publish_svc._run_publish_state_machine(
        user_id="local-dev-user",
        workspace_id="ws-aapl",
        thread_id="t-1",
        tool_call_id="tc-1",
        draft_path="drafts/aapl.md",
        title="AAPL 3Q26",
        kind="earnings_note",
        slug="aapl-3q26",
        attachments=[
            {"path": "attachments/facts_first.md", "role": "facts"},
            {"path": "attachments/facts_second.md", "role": "facts"},
        ],
        backend=backend,
    )
    assert res["facts_path"] == "attachments/facts_first.md"
    assert "默认事实台账：attachments/facts_first.md" in res["message"]
    # 100 亿元 matches facts_first.md (100 亿元) without conflict against facts_second.md (999 亿元)
    assert res["audit"]["matched"] == 1
    assert res["audit"]["contradicted"] == 0
    assert res["audit"]["unverified"] == 1


def test_table_cell_narrative_comma_following_period_not_explicit() -> None:
    """Round 4 Item 4:
    In a table cell containing `80 亿元 [引用1]，FY2026 指引收入为 100 亿元`, `80 亿元` must NOT
    grab `FY2026` across the comma and must NOT be marked `period_explicit=True`."""
    from src.dalton.publish.numbers import extract_figures

    draft = (
        "| 项目 | 内容 |\n"
        "| --- | --- |\n"
        "| 收入分析 | 80 亿元 [引用1]，FY2026 指引收入为 100 亿元 |\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    figs = extract_figures(draft)
    fig_80 = next(f for f in figs if "80" in f.raw)
    fig_100 = next(f for f in figs if "100" in f.raw)
    assert fig_80.location.get("table") is True
    assert fig_80.location.get("period_explicit") is False
    assert fig_80.period == ""
    assert fig_100.location.get("period_explicit") is True
    assert fig_100.period == "FY2026"


@pytest.mark.asyncio
async def test_in_workspace_absolute_draft_and_facts_paths_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 5:
    Passing in-workspace absolute paths for `draft_path` and `facts_path` (e.g. under `/home/workspace`
    or `backend.workspace_dir`) converts them to workspace-relative paths rather than calling `.lstrip('/')`."""
    from src.dalton import auth as dalton_auth
    from src.dalton.publish import service as publish_svc
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(
            dalton_auth.verify_workspace_access,
            return_value={"workspace_id": "ws-test"},
        ),
    )

    draft_md = (
        "# 深度覆盖报告\n\n"
        "FY2025 营业收入为 250 亿元 [引用1]。\n\n"
        "## 引用\n"
        "[引用1]: f:rev_25\n"
    )
    facts_conv = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 250 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    facts_custom = (
        "<!-- FACTS-LEDGER -->\n"
        "| key | 指标 | 数值 | 期间 | 来源 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| rev_25 | 营业收入 | 250 亿元 | FY2025 | 年报 |\n"
        "<!-- END -->\n"
    )
    store: dict[str, str] = {
        "drafts/2026-11-01-memo.md": draft_md,
        "research/2026-11-01-fc/facts.md": facts_conv,
        "custom/facts.md": facts_custom,
    }
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = "/home/workspace/acme"
    backend.aread_text.side_effect = lambda p: store.get(p)
    backend.awrite_text.side_effect = lambda p, c: store.__setitem__(p, c)

    # 1. In-workspace absolute `draft_path` resolves convention `research/2026-11-01-fc/facts.md`
    res_conv = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="/home/workspace/acme/drafts/2026-11-01-memo.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_conv["ok"] is True
    assert res_conv["facts_path"] == "research/2026-11-01-fc/facts.md"
    assert res_conv["audit"]["matched"] == 1

    # 2. In-workspace absolute `facts_path` resolves `custom/facts.md`
    res_exp = await publish_svc.run_check_draft(
        workspace_id="ws-test",
        draft_path="/home/workspace/acme/drafts/2026-11-01-memo.md",
        facts_path="/home/workspace/acme/custom/facts.md",
        backend=backend,
        thesis_doc={"evidence": []},
        models_doc={"versions": []},
        provenance_records={},
        use_llm_judge=False,
        user_id="local-dev-user",
    )
    assert res_exp["ok"] is True
    assert res_exp["facts_path"] == "custom/facts.md"
    assert res_exp["audit"]["matched"] == 1






def test_normalize_workspace_path_keeps_paths_outside_the_workspace_absolute():
    """Only the workspace folder is stripped: a sibling workspace's absolute path
    must reach the backend unchanged, not be re-anchored inside this workspace."""
    from src.dalton.publish.service import _normalize_workspace_path

    class _Backend:
        workspace_dir = "/home/daytona/ACME"
        root_dir = "/home/daytona"

    b = _Backend()
    assert _normalize_workspace_path("/home/daytona/ACME/drafts/x.md", b) == "drafts/x.md"
    assert _normalize_workspace_path("/home/daytona/OTHER/facts.md", b) == "/home/daytona/OTHER/facts.md"
    assert _normalize_workspace_path("research/A/../A/facts.md", b) == "research/A/facts.md"
    assert _normalize_workspace_path("/home/daytona/ACME/drafts/x.md", None) == "/home/daytona/ACME/drafts/x.md"


@pytest.mark.asyncio
async def test_absolute_footnote_path_is_refused_not_stripped():
    """An absolute footnote path must not collide with a relative ledger sharing
    its tail; it is locked unverified with a hint to use a workspace-relative path."""
    from src.dalton.publish.match import match_figures
    from src.dalton.publish.numbers import extract_figures, parse_facts_ledger

    draft = (
        "FY2025 营业收入为 250 亿元 [引用1]。\n\n## 引用\n"
        "[引用1]: /home/daytona/ACME/research/A/facts.md#rev\n"
    )
    ledger_text = (
        '<!-- DALTON-FACTS-FILE path="home/daytona/ACME/research/A/facts.md" -->\n'
        "<!-- FACTS-LEDGER -->\n| key | metric | period | value | unit | ref |\n"
        "|---|---|---|---|---|---|\n| rev | 营业收入 | FY2025 | 250 | 亿元 | 10-K |\n"
        "<!-- /DALTON-FACTS-FILE -->\n"
    )
    figs = extract_figures(draft)
    outcomes = match_figures(figs, facts_ledger=parse_facts_ledger(ledger_text))
    target = [o for o in outcomes if o.figure.value in (250, 250.0)]
    assert target, outcomes
    assert all(not o.matched and o.lock_unverified for o in target)
    assert any("绝对路径" in (o.hint or "") for o in target)
