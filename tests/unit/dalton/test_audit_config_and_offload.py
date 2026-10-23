"""Number audit: configured model chain, per-model retry policy, one deadline, work off the loop."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
from src.dalton.publish import audit as audit_mod
from src.dalton.publish import match as match_mod
from src.dalton.publish import numbers as numbers_mod
from src.dalton.publish.numbers import extract_figures
from src.dalton.settings import get_dalton_config

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}
SUB = "sub-primary-oauth"  # "-oauth" suffix: subscription-billed
SUB_FB = "sub-fallback-oauth"
CASH = "cash-primary"


def _timeout(model: str) -> DaltonCompleteResult:
    return DaltonCompleteResult(
        ok=False, status="indeterminate", error="TimeoutError: ", error_kind="timeout", model=model
    )


def _ok(model: str) -> DaltonCompleteResult:
    return DaltonCompleteResult(ok=True, status="done", data={"items": []}, text='{"items": []}', model=model)


def _patch_complete(monkeypatch, behaviour) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _complete(**kwargs: Any) -> DaltonCompleteResult:
        calls.append(kwargs)
        return behaviour(str(kwargs.get("request_model")))

    monkeypatch.setattr(assist_mod, "dalton_complete", create_autospec(dalton_complete, side_effect=_complete))
    return calls


@pytest.mark.asyncio
async def test_subscription_chain_falls_back_after_timeout_and_resends_every_candidate(monkeypatch):
    calls = _patch_complete(monkeypatch, lambda m: _timeout(m) if m == SUB else _ok(m))
    res = await assist_json("audit.label_figures", "x", {}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB])
    assert res.ok and [c["request_model"] for c in calls] == [SUB, SUB_FB]
    # Both the primary and the fallback may be re-sent after an indeterminate call.
    assert [c["resend_indeterminate"] for c in calls] == [True, True]


@pytest.mark.asyncio
async def test_cash_model_keeps_the_double_charge_guard(monkeypatch):
    calls = _patch_complete(monkeypatch, _timeout)
    res = await assist_json("audit.judge", "x", {}, SCHEMA, user_id="u1", model=CASH, fallback_models=[SUB_FB])
    assert not res.ok and [c["request_model"] for c in calls] == [CASH]
    assert calls[0]["resend_indeterminate"] is False


@pytest.mark.asyncio
async def test_second_run_resends_the_fallback_too(monkeypatch):
    calls = _patch_complete(monkeypatch, lambda m: _timeout(m) if m == SUB else _ok(m))
    for _ in range(2):
        await assist_json("audit.judge", "x", {"same": 1}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB])
    fb_calls = [c for c in calls if c["request_model"] == SUB_FB]
    assert len(fb_calls) == 2 and all(c["resend_indeterminate"] for c in fb_calls)


@pytest.mark.asyncio
async def test_deadline_reaches_every_call(monkeypatch):
    calls = _patch_complete(monkeypatch, lambda m: _timeout(m) if m == SUB else _ok(m))
    deadline = time.monotonic() + 30.0
    await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB], deadline=deadline
    )
    assert [c["deadline"] for c in calls] == [deadline, deadline]


@pytest.mark.asyncio
async def test_past_deadline_a_journalled_result_still_replays(monkeypatch):
    """A re-run after the deadline must not turn a finished verdict into 'uncertain'."""
    replay = DaltonCompleteResult(
        ok=True, status="done", data={"items": [{"id": "J-001"}]}, text="{}", model=SUB,
        replayed=True, cached=True,
    )
    _patch_complete(monkeypatch, lambda m: replay)
    res = await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB],
        deadline=time.monotonic() - 60,
    )
    assert res.ok and res.data == {"items": [{"id": "J-001"}]}


@pytest.mark.asyncio
async def test_dalton_complete_replays_after_deadline_but_sends_nothing_new(monkeypatch):
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    row = {"state": "done", "response": {"data": {"ok": 1}, "text": "{}"}, "model_key": SUB}
    monkeypatch.setattr(complete_mod, "inspect_journal_state",
                        create_autospec(complete_mod.inspect_journal_state, return_value=("done", row)))
    monkeypatch.setattr(complete_mod, "is_journal_response_replayable",
                        create_autospec(complete_mod.is_journal_response_replayable, return_value=True))
    monkeypatch.setattr(complete_mod, "record_journal_replay",
                        create_autospec(complete_mod.record_journal_replay))
    past = time.monotonic() - 60
    replayed = await dalton_complete(service, user_id="u1", user_prompt="p", request_model=SUB, deadline=past)
    assert replayed.ok and replayed.replayed
    service.complete.assert_not_called()

    fresh = await dalton_complete(
        service, user_id="u1", user_prompt="p2", request_model=SUB, deadline=past, skip_journal=True
    )
    assert not fresh.ok and fresh.status == "failed" and fresh.error_kind == "deadline"
    service.complete.assert_not_called()


@pytest.mark.asyncio
async def test_send_timeout_is_clamped_to_time_left_at_send(monkeypatch):
    from src.server.services.llm.service import LLMService

    seen: dict[str, float] = {}
    real_wait_for = asyncio.wait_for

    async def _wait_for(aw, timeout):
        seen["timeout"] = timeout
        return await real_wait_for(aw, timeout)

    service = create_autospec(LLMService, instance=True)
    service.complete.return_value = ({"ok": 1}, {})
    monkeypatch.setattr(complete_mod.asyncio, "wait_for", _wait_for)
    await dalton_complete(
        service, user_id="u1", user_prompt="p", request_model=SUB, timeout_s=300.0,
        deadline=time.monotonic() + 5.0, skip_journal=True,
    )
    assert 0 < seen["timeout"] <= 5.0


@pytest.mark.asyncio
async def test_forbidden_fallback_fails_before_anything_is_sent(monkeypatch):
    calls = _patch_complete(monkeypatch, _ok)
    res = await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=SUB,
        fallback_models=["muse-spark-1.3-contributor-oauth"],
    )
    assert not res.ok and "contributor" in (res.error or "")
    assert calls == []  # no successful primary result was thrown away


DRAFT = "## 业绩\n公司2Q26广告收入为512亿美元，同比增长21%。\n经营利润率为41%。\n"
PROV = {"P1": {"body": "2Q26 advertising revenue was $51.2 billion, up 21% year over year.", "kind": "sec"}}


def _stub_assist(monkeypatch, seen: list[dict[str, Any]]):
    async def _assist(*args: Any, **kwargs: Any) -> AssistResult:
        seen.append({"task": args[0] if args else kwargs.get("task"), **kwargs})
        return AssistResult(ok=False, data=None, error="stub", model=str(kwargs.get("model")))

    stub = create_autospec(assist_json, side_effect=_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", stub)
    monkeypatch.setattr(audit_mod, "assist_json", stub)


@pytest.mark.asyncio
async def test_audit_uses_the_configured_chain_for_both_tasks(monkeypatch):
    seen: list[dict[str, Any]] = []
    _stub_assist(monkeypatch, seen)
    await audit_mod.aaudit_draft(DRAFT, provenance_records=PROV, use_llm_judge=True, user_id="u1")
    pub = get_dalton_config().publish
    assert {c["task"] for c in seen} == {"audit.label_figures", "audit.judge"}
    for c in seen:
        assert c["model"] == pub.audit_model
        assert c["fallback_models"] == pub.audit_fallback_models
        assert c["timeout_s"] == pub.audit_timeout_s
        assert c["deadline"] is not None
        assert "resend_indeterminate" not in c  # decided per model inside assist_json


@pytest.mark.asyncio
async def test_missing_audit_model_is_a_clear_error(monkeypatch):
    pub = get_dalton_config().publish
    monkeypatch.setattr(pub, "audit_model", "")
    with pytest.raises(ValueError, match="publish.audit_model"):
        await audit_mod.aaudit_draft(DRAFT, provenance_records=PROV, use_llm_judge=True, user_id="u1")


@pytest.mark.asyncio
async def test_hotspots_run_off_the_event_loop_thread(monkeypatch):
    _stub_assist(monkeypatch, [])
    loop_thread = threading.get_ident()
    ran_on: dict[str, int] = {}
    for name, mod in (("match_figures", audit_mod), ("_collect_judge_candidates", audit_mod)):
        real = getattr(mod, name)

        def _spy(*a, _real=real, _name=name, **k):
            ran_on[_name] = threading.get_ident()
            return _real(*a, **k)

        monkeypatch.setattr(mod, name, create_autospec(real, side_effect=_spy))
    await audit_mod.aaudit_draft(DRAFT, provenance_records=PROV, use_llm_judge=True, user_id="u1")
    assert set(ran_on) == {"match_figures", "_collect_judge_candidates"}
    assert all(t != loop_thread for t in ran_on.values())


@pytest.mark.asyncio
async def test_offloaded_audit_matches_inline_result(monkeypatch):
    _stub_assist(monkeypatch, [])
    threaded = await audit_mod.aaudit_draft(DRAFT, provenance_records=PROV, use_llm_judge=True, user_id="u1")

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline)
    inline = await audit_mod.aaudit_draft(DRAFT, provenance_records=PROV, use_llm_judge=True, user_id="u1")
    assert threaded == inline


def test_cached_parse_equals_direct_parse_and_is_per_instance():
    body = PROV["P1"]["body"]
    cache = match_mod.SourceParseCache()
    first = cache.figures(body)
    assert list(first) == extract_figures(body)
    assert cache.figures(body) is first
    assert match_mod.SourceParseCache().figures(body) is not first
    assert not hasattr(match_mod, "_source_figures")


@pytest.mark.asyncio
async def test_label_batches_still_running_at_the_deadline_count_as_failed(monkeypatch):
    async def _hang(*args: Any, **kwargs: Any) -> AssistResult:
        await asyncio.sleep(30)
        return AssistResult(ok=True, data={"items": []}, model="x")

    monkeypatch.setattr(numbers_mod, "assist_json", create_autospec(assist_json, side_effect=_hang))
    monkeypatch.setattr(numbers_mod, "_LABEL_DEADLINE_GRACE_S", 0.05)
    figs = extract_figures(DRAFT)
    stats: dict[str, Any] = {}
    t0 = time.monotonic()
    _, ok = await numbers_mod.label_figures_with_status(
        DRAFT, figs, user_id="u1", stats_out=stats, deadline=time.monotonic() + 0.2
    )
    assert time.monotonic() - t0 < 5
    assert not ok and stats["failed_batches"] == stats["batches"] >= 1


def _successful_assist():
    async def _assist(*args: Any, **kwargs: Any) -> AssistResult:
        task = args[0] if args else kwargs.get("task")
        payload = args[2] if len(args) > 2 else kwargs.get("payload")
        if task == "audit.label_figures":
            items = [
                {"id": f["id"], "metric": f.get("metric") or "广告收入", "period": "2Q26", "kind": "reported"}
                for f in payload["extracted_figures"]
            ]
            return AssistResult(ok=True, data={"items": items}, model="stub")
        verdicts = []
        for f in payload["figures"]:
            cand = next((c for c in f["candidate_sources"] if f["raw"].rstrip("%") in str(c.get("text"))), None)
            if cand is None:
                verdicts.append({"id": f["id"], "verdict": "unverified"})
                continue
            text = str(cand["text"])
            num = f["raw"].rstrip("%").replace("亿美元", "")
            verdicts.append({
                "id": f["id"], "verdict": "supported", "matched_source_id": cand["source_id"],
                "quote": text, "quote_number_raw": num if num in text else f["raw"],
                "quote_metric_raw": "advertising revenue",
            })
        return AssistResult(ok=True, data={"items": verdicts}, model="stub")

    return create_autospec(assist_json, side_effect=_assist)


@pytest.mark.asyncio
async def test_threaded_audit_equals_inline_reference_with_successful_labels_and_verdicts(monkeypatch):
    stub = _successful_assist()
    monkeypatch.setattr(numbers_mod, "assist_json", stub)
    monkeypatch.setattr(audit_mod, "assist_json", stub)
    prov = {"P1": {"body": "2Q26 advertising revenue was 512 billion, up 21% year over year; margin 41%.",
                   "kind": "sec"}}
    threaded = await audit_mod.aaudit_draft(DRAFT, provenance_records=prov, use_llm_judge=True, user_id="u1")
    tasks = {c.args[0] if c.args else c.kwargs.get("task") for c in stub.call_args_list}
    assert tasks == {"audit.label_figures", "audit.judge"}
    assert not threaded.labeling_incomplete
    assert threaded.matched >= 1, "judge verdicts should verify at least one figure"

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline)
    reference = await audit_mod.aaudit_draft(DRAFT, provenance_records=prov, use_llm_judge=True, user_id="u1")
    assert threaded == reference


@pytest.mark.asyncio
async def test_cancelling_the_audit_leaves_no_label_task_sending(monkeypatch):
    started: list[int] = []
    finished: list[int] = []

    async def _slow(*args: Any, **kwargs: Any) -> AssistResult:
        started.append(1)
        await asyncio.sleep(0.5)
        finished.append(1)
        return AssistResult(ok=True, data={"items": []}, model="x")

    monkeypatch.setattr(numbers_mod, "assist_json", create_autospec(assist_json, side_effect=_slow))
    draft = "\n\n".join(f"## 第{i}节\n公司2Q26收入为{i}00亿美元。" for i in range(1, 12))
    parent = asyncio.create_task(
        numbers_mod.label_figures_with_status(
            draft, extract_figures(draft), user_id="u1", deadline=time.monotonic() + 60
        )
    )
    await asyncio.sleep(0.05)
    assert started
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    sends_at_cancel = len(started)
    await asyncio.sleep(0.8)
    assert finished == [] and len(started) == sends_at_cancel
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not [t for t in others if "_call_label_batch" in repr(t.get_coro())]


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", ["", "   \n\t  \n"])
async def test_empty_drafts_label_without_error(monkeypatch, draft):
    stub = create_autospec(assist_json)
    monkeypatch.setattr(numbers_mod, "assist_json", stub)
    figs, ok = await numbers_mod.label_figures_with_status(
        draft, extract_figures(draft), user_id="u1", deadline=time.monotonic() + 60
    )
    assert figs == [] and ok
    stub.assert_not_called()


@pytest.mark.asyncio
async def test_past_deadline_the_chain_still_reads_fallback_journals(monkeypatch):
    indeterminate = DaltonCompleteResult(
        ok=False, status="indeterminate", error="Journal state is indeterminate",
        error_kind="indeterminate", model=SUB,
    )
    replay = DaltonCompleteResult(
        ok=True, status="done", data={"items": [{"id": "J-001"}]}, text="{}", model=SUB_FB,
        replayed=True, cached=True,
    )
    _patch_complete(monkeypatch, lambda m: indeterminate if m == SUB else replay)
    res = await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB],
        deadline=time.monotonic() - 60,
    )
    assert res.ok and res.model == SUB_FB and res.data == {"items": [{"id": "J-001"}]}


@pytest.mark.asyncio
async def test_past_deadline_a_successful_primary_is_kept(monkeypatch):
    """A primary that succeeded (e.g. a replay) past the deadline is not replaced by the fallback."""
    primary_ok = DaltonCompleteResult(
        ok=True, status="done", data={"items": [{"id": "P-001"}]}, text="{}", model=SUB,
        replayed=True, cached=True,
    )
    calls = []

    def _pick(m):
        calls.append(m)
        return primary_ok

    _patch_complete(monkeypatch, _pick)
    res = await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=SUB, fallback_models=[SUB_FB],
        deadline=time.monotonic() - 60,
    )
    assert res.ok and res.model == SUB and res.data == {"items": [{"id": "P-001"}]}
    assert calls == [SUB]
