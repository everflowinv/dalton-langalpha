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
