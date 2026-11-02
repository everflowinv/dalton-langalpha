"""Reviewer dispatch contract at the driver/journal seam (prompt review batch
1 round 2 #1/#2).

The caller binds the review to the draft: the driver hashes the file named by
``opts.reviewDraft`` itself and stamps ``draft_sha256`` on the journal record
(the reviewer never echoes a hash); a reviewer reply that does not cover
checks 1–9 is an invalid output — recorded invalid_schema, never replayable —
and ``dalton_check_done`` reads the caller-bound hash back off the journal.
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from typing import Any

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.validation import (
    DispatchValidationError,
    validate_dispatch,
)
from src.dalton.publish.review import (
    effective_review_verdict,
    reviewer_reply_error,
)
from src.dalton.workflows.journal import WorkflowJournal, prompt_digest
from src.dalton.tools.check_done_tools import _find_run_review_pass

from .conftest import FakeBackend
from .test_driver import Immediate, _caps, _make_driver, _script

REVIEWER_TYPES = ["general-purpose", "dalton-reviewer"]

REVIEWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "revise", "block"]},
        "summary": {"type": "string", "minLength": 1},
        "must_fix": {"type": "array", "items": {"type": "string"}},
        "items": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["verdict", "summary", "items"],
}

# The reviewer prompt asks for checks 1..9 — the element shape a JSON Schema
# cannot demand under schema_max_depth, so the driver enforces it.
NINE_ITEMS = [{"check": n, "result": "pass", "reason": "ok"} for n in range(1, 10)]

REVIEW_DRAFT = "drafts/memo.md"


def _reviewer_dispatch() -> str:
    opts = (
        "{ agentType: 'dalton-reviewer', label: '审稿', schema: VERDICT, "
        f"reviewDraft: '{REVIEW_DRAFT}' }}"
    )
    return (
        "const VERDICT = "
        + json.dumps(REVIEWER_SCHEMA)
        + f";\nreturn await agent('审稿 {REVIEW_DRAFT}', {opts});"
    )


def _reply(
    *, verdict: str = "pass", items: list[dict[str, Any]] | None = None,
    must_fix: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": "ok",
            "items": NINE_ITEMS if items is None else items,
            "must_fix": must_fix or [],
        },
        ensure_ascii=False,
    )


def _journal(backend: FakeBackend, driver: Any) -> dict[str, Any]:
    return json.loads(backend.writes[f"{driver.base_rel}/journal/审稿.json"])


class _GlobBackend(FakeBackend):
    """FakeBackend plus the journal listing seam (aglob_paths) and reads that
    see the driver's writes as well as its files."""

    async def aglob_paths(self, pattern: str, path: str = ".") -> list[str]:
        keys = list(self.files) + list(self.writes)
        return [key for key in keys if fnmatch(key, pattern)]

    async def aread_text(self, path: str) -> str:
        if path in self.files:
            return self.files[path]
        if path in self.writes:
            return self.writes[path]
        raise FileNotFoundError(path)


# ------------------------------------------------- validate_dispatch: reviewDraft


def test_review_draft_opt_is_validated_and_normalized() -> None:
    caps = _caps()

    def _validate(review_draft: Any) -> dict[str, Any]:
        return validate_dispatch(
            prompt="审稿",
            opts={
                "agentType": "dalton-reviewer",
                "label": "审稿",
                "reviewDraft": review_draft,
            },
            known_subagent_types=REVIEWER_TYPES,
            default_subagent_type="general-purpose",
            caps=caps,
        )

    assert _validate(REVIEW_DRAFT)["review_draft"] == REVIEW_DRAFT
    assert _validate(None)["review_draft"] is None
    assert _validate("  drafts/x.md  ")["review_draft"] == "drafts/x.md"
    for bad in (123, "", "   ", "x" * 513):
        with pytest.raises(DispatchValidationError, match="reviewDraft"):
            _validate(bad)


# ------------------------------------------------- caller-bound hash on the journal


@pytest.mark.asyncio
async def test_reviewer_dispatch_binds_caller_computed_draft_hash() -> None:
    draft = "被审草稿正文"
    driver, _, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )
    backend.files[REVIEW_DRAFT] = draft

    await driver.run()

    assert len(dispatcher.calls) == 1
    entry = _journal(backend, driver)["dispatches"][-1]
    assert entry["status"] == "ok"
    # The binding is caller-side: the driver hashed the named draft itself.
    assert entry["draft_sha256"] == hashlib.sha256(draft.encode("utf-8")).hexdigest()
    # …and the reviewer's reply itself carries no hash to trust or echo.
    assert "draft_sha256" not in entry["result"]


@pytest.mark.asyncio
async def test_reviewer_dispatch_with_unreadable_draft_records_no_hash() -> None:
    """A reviewDraft that names nothing the backend can read records a null
    binding — the closing check then fails closed on the missing binding
    instead of trusting any child's word."""
    driver, _, _, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )

    await driver.run()

    entry = _journal(backend, driver)["dispatches"][-1]
    assert entry["status"] == "ok"
    assert entry["draft_sha256"] is None


@pytest.mark.asyncio
async def test_hash_binding_flows_through_check_done_and_fails_on_draft_change() -> None:
    """The full loop of round 2 #1: the reviewer answers the new format (no
    hash), the journal carries the caller's hash, dalton_check_done's run
    review passes — and stops passing once the draft changes under it."""
    draft = "# 备忘录\n被审正文"
    driver, _, _, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )
    backend.files[REVIEW_DRAFT] = draft

    await driver.run()

    flow = _GlobBackend(dict(backend.files))
    flow.writes.update(backend.writes)
    passed, reason = await _find_run_review_pass(
        flow, "driver-test", driver.wf_task_id, REVIEW_DRAFT, "short-1"
    )
    assert passed is True, reason

    flow.files[REVIEW_DRAFT] = draft + "\n事后改动。\n"
    stale, stale_reason = await _find_run_review_pass(
        flow, "driver-test", driver.wf_task_id, REVIEW_DRAFT, "short-1"
    )
    assert stale is False
    assert "旧草稿" in stale_reason


# ------------------------------------------------- missing items never replay


@pytest.mark.asyncio
async def test_reviewer_reply_missing_check_items_is_invalid_and_not_replayed() -> None:
    """A reply without items 1–9 is an invalid reviewer output: the retry
    fires, the script sees null, the journal outcome is invalid_schema (no
    result, not replayable) and find_replayable refuses it."""
    incomplete = _reply(items=NINE_ITEMS[:8])
    driver, registry, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[
            Immediate({"success": True, "result": incomplete}),
            Immediate({"success": True, "result": incomplete}),
        ],
        subagent_types=REVIEWER_TYPES,
    )
    backend.files[REVIEW_DRAFT] = "稿"

    await driver.run()

    # The corrective retry fired and still failed: the script got null.
    assert len(dispatcher.calls) == 2
    assert "1–9" in (dispatcher.calls[1]["prompt"])
    assert json.loads(backend.writes[f"{driver.base_rel}/result.json"]) is None
    done = [e for e in registry.events if e["phase"] == "child_done"]
    assert done[-1]["status"] == "invalid_schema"

    journal = _journal(backend, driver)
    reviewer_outcomes = [
        e for e in journal["dispatches"] if e.get("status") != "pending"
    ]
    assert all(e["status"] == "invalid_schema" for e in reviewer_outcomes)
    assert all("result" not in e for e in reviewer_outcomes)

    glob_backend = _GlobBackend(dict(backend.files))
    glob_backend.writes.update(backend.writes)
    resume = WorkflowJournal(
        glob_backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source=driver.base_rel,
        progress_path=None,
    )
    assert await resume.find_replayable("审稿", dispatcher.calls[0]["prompt"]) is None


@pytest.mark.asyncio
async def test_valid_reviewer_entry_replays_with_its_draft_binding() -> None:
    """The ok reviewer entry replays on resume and record_replay keeps the
    caller-bound draft hash on the new run's journal."""
    driver, _, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )
    draft = "稿"
    backend.files[REVIEW_DRAFT] = draft
    await driver.run()
    expected_sha = hashlib.sha256(draft.encode("utf-8")).hexdigest()

    glob_backend = _GlobBackend(dict(backend.files))
    glob_backend.writes.update(backend.writes)
    resume = WorkflowJournal(
        glob_backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source=driver.base_rel,
        progress_path=None,
    )
    # The prompt the script handed the driver — the replay key matches it.
    prompt = dispatcher.calls[0]["prompt"]
    entry = await resume.find_replayable("审稿", prompt)
    assert entry is not None
    assert entry["draft_sha256"] == expected_sha

    await resume.record_replay(entry, seq=1, label="审稿", phase="审稿")
    replayed = json.loads(glob_backend.writes["run-2/journal/审稿.json"])
    assert replayed["dispatches"][-1]["draft_sha256"] == expected_sha
    assert replayed["dispatches"][-1]["prompt_sha256"] == prompt_digest(prompt)


# ------------------------------------------------- the shared reply contract


class TestSharedReplyContract:
    def test_missing_items_is_an_invalid_review(self) -> None:
        reply = {"verdict": "pass", "summary": "ok", "items": NINE_ITEMS[:7]}
        assert reviewer_reply_error(reply) is not None
        assert effective_review_verdict(reply) is None

    def test_bad_item_shape_is_an_invalid_review(self) -> None:
        reply = {
            "verdict": "revise",
            "summary": "ok",
            "items": [{"check": n, "result": "maybe"} for n in range(1, 10)],
        }
        assert reviewer_reply_error(reply) is not None

    def test_stringly_check_numbers_are_an_invalid_review(self) -> None:
        """第 3 轮 #4：``check`` 是严格整数——字符串 "1" 不被 coerce，整条回复
        无效（driver 记 invalid、不可 replay），不作 pass。"""
        reply = {
            "verdict": "pass",
            "summary": "ok",
            "items": [{"check": str(n), "result": "pass", "reason": "ok"} for n in range(1, 10)],
        }
        assert reviewer_reply_error(reply) is not None
        assert effective_review_verdict(reply) is None

    def test_pass_with_must_fix_reads_as_revise(self) -> None:
        reply = {
            "verdict": "pass",
            "summary": "ok",
            "items": NINE_ITEMS,
            "must_fix": ["补一句风险"],
        }
        assert reviewer_reply_error(reply) is None
        assert effective_review_verdict(reply) == "revise"

    def test_block_survives_must_fix(self) -> None:
        reply = {
            "verdict": "block",
            "summary": "结论无证据",
            "items": NINE_ITEMS,
            "must_fix": ["§2 与证据脱节"],
        }
        assert effective_review_verdict(reply) == "block"

    def test_analyst_added_hash_field_does_not_break_the_contract(self) -> None:
        """The manual sidecar: the saving analyst appends the hash it computed
        itself; the reply itself stays valid with the extra field."""
        reply = {
            "verdict": "pass",
            "summary": "ok",
            "items": NINE_ITEMS,
            "must_fix": [],
            "draft_sha256": "a" * 64,
        }
        assert reviewer_reply_error(reply) is None
        assert effective_review_verdict(reply) == "pass"


# ------------------------------------------------- round 3 #4/#5/#7


@pytest.mark.asyncio
async def test_stringly_check_numbers_are_rejected_and_not_replayed() -> None:
    """第 3 轮 #7：字符串编号的审稿回复是无效输出——driver 记 invalid_schema、
    journal 无 result、resume 不复用。"""
    stringly = json.dumps(
        {
            "verdict": "pass",
            "summary": "编号写成了字符串",
            "must_fix": [],
            "items": [
                {"check": str(n), "result": "pass", "reason": "ok"}
                for n in range(1, 10)
            ],
        },
        ensure_ascii=False,
    )
    driver, registry, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[
            Immediate({"success": True, "result": stringly}),
            Immediate({"success": True, "result": stringly}),
        ],
        subagent_types=REVIEWER_TYPES,
    )
    backend.files[REVIEW_DRAFT] = "稿"

    await driver.run()

    assert len(dispatcher.calls) == 2
    assert json.loads(backend.writes[f"{driver.base_rel}/result.json"]) is None
    done = [e for e in registry.events if e["phase"] == "child_done"]
    assert done[-1]["status"] == "invalid_schema"

    journal = _journal(backend, driver)
    outcomes = [e for e in journal["dispatches"] if e.get("status") != "pending"]
    assert all(e["status"] == "invalid_schema" for e in outcomes)
    assert all("result" not in e for e in outcomes)

    glob_backend = _GlobBackend(dict(backend.files))
    glob_backend.writes.update(backend.writes)
    resume = WorkflowJournal(
        glob_backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source=driver.base_rel,
        progress_path=None,
    )
    assert await resume.find_replayable("审稿", dispatcher.calls[0]["prompt"]) is None


@pytest.mark.asyncio
async def test_changed_draft_forces_the_review_to_re_dispatch_on_resume() -> None:
    """第 3 轮 #5/#7：审稿 step 带了 reviewDraft——草稿没动时按记录 replay；
    草稿改动后（哈希不再一致）find_replayable 拒绝，resume 必须重新派发审稿。"""
    driver, _, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )
    draft = "原稿"
    backend.files[REVIEW_DRAFT] = draft
    await driver.run()
    prompt = dispatcher.calls[0]["prompt"]

    glob_backend = _GlobBackend(dict(backend.files))
    glob_backend.writes.update(backend.writes)
    resume = WorkflowJournal(
        glob_backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source=driver.base_rel,
        progress_path=None,
    )
    # 草稿未变：按记录复用。
    assert await resume.find_replayable("审稿", prompt) is not None

    # 草稿改动：绑定哈希不再等于当前文件，复用被拒（重新派发审稿）。
    glob_backend.files[REVIEW_DRAFT] = draft + "\n改稿。\n"
    assert await resume.find_replayable("审稿", prompt) is None


@pytest.mark.asyncio
async def test_reviewer_hash_failure_is_never_replayable() -> None:
    """第 3 轮 #5：reviewDraft 存在但哈希计算失败（草稿读不到）的 ok 记录，
    driver 写下 replayable=False——resume 不会复用它。"""
    driver, _, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[Immediate({"success": True, "result": _reply()})],
        subagent_types=REVIEWER_TYPES,
    )
    # 草稿文件不存在：digest 记 None，journal 记录 replayable=False。
    await driver.run()

    entry = _journal(backend, driver)["dispatches"][-1]
    assert entry["status"] == "ok"
    assert entry["draft_sha256"] is None
    assert entry["replayable"] is False

    glob_backend = _GlobBackend(dict(backend.files))
    glob_backend.writes.update(backend.writes)
    resume = WorkflowJournal(
        glob_backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source=driver.base_rel,
        progress_path=None,
    )
    assert await resume.find_replayable("审稿", dispatcher.calls[0]["prompt"]) is None


@pytest.mark.asyncio
async def test_pass_with_must_fix_journal_record_is_not_a_valid_pass() -> None:
    """第 3 轮 #6/#7：journal 里 verdict=pass 但 must_fix 非空的记录不是有效
    pass——`_find_run_review_pass` 报不是 pass；语言审查对照用的
    `_last_review_pass_sha256` 也不把它当 pass（返回 None 哈希）。"""
    from src.dalton.tools.check_done_tools import (
        _last_review_pass_sha256,
    )

    draft = "# 备忘录\n正文"
    driver, _, dispatcher, backend = await _make_driver(
        _script(_reviewer_dispatch()),
        behaviors=[
            Immediate({"success": True, "result": _reply(must_fix=["补风险"])})
        ],
        subagent_types=REVIEWER_TYPES,
    )
    backend.files[REVIEW_DRAFT] = draft
    await driver.run()

    flow = _GlobBackend(dict(backend.files))
    flow.writes.update(backend.writes)
    passed, reason = await _find_run_review_pass(
        flow, "driver-test", driver.wf_task_id, REVIEW_DRAFT, "short-1"
    )
    assert passed is False
    assert "不是 pass" in reason

    found_run, sha = await _last_review_pass_sha256(
        flow, "driver-test", driver.wf_task_id, "short-1"
    )
    assert found_run is True
    assert sha is None


# ------------------------------------------------- round 4 #2: pre-contract journals never replay


def _resume_journal(backend: _GlobBackend, entry: dict[str, Any]) -> WorkflowJournal:
    """A resume journal over one hand-written dispatch entry of a prior run."""
    path = "run-old/journal/审稿.json"
    backend.files[path] = json.dumps(
        {"label": "审稿", "dispatches": [entry]}, ensure_ascii=False
    )
    return WorkflowJournal(
        backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source="run-old",
        progress_path=None,
    )


def _ok_entry(result: Any, *, review_draft: str | None, draft_sha: str | None) -> dict[str, Any]:
    return {
        "seq": 1,
        "label": "审稿",
        "phase": "审稿",
        "prompt_sha256": prompt_digest(PROMPT),
        "status": "ok",
        "result": result,
        "result_kind": "parsed",
        "artifacts": [],
        "draft_sha256": draft_sha,
        "review_draft": review_draft,
        "replayable": True,
        "recorded_at": 1.0,
    }


PROMPT = "审稿 drafts/memo.md"


@pytest.mark.asyncio
async def test_pre_contract_reviewer_journal_is_revalidated_and_refused() -> None:
    """第 4 轮 #2：升级前保存的旧格式审稿记录（没有 items、result 里回填哈希）
    即使草稿绑定哈希仍然一致，也不得复用——resume 重新派发审稿。"""
    draft = "当前草稿"
    backend = _GlobBackend({REVIEW_DRAFT: draft})
    resume = _resume_journal(
        backend,
        _ok_entry(
            # The pre-contract shape: verdict + the model's echoed hash, no items.
            {"verdict": "pass", "summary": "旧格式", "draft_sha256": "a" * 64},
            review_draft=REVIEW_DRAFT,
            draft_sha=hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        ),
    )
    assert await resume.find_replayable("审稿", PROMPT) is None


@pytest.mark.asyncio
async def test_stringly_check_numbers_in_an_old_journal_are_refused() -> None:
    """第 4 轮 #2：旧记录里检查编号是字符串（"1".."9"）同样无效——按标签识别
    的审稿记录（无 reviewDraft 绑定）也会被重新校验拒绝。"""
    backend = _GlobBackend({REVIEW_DRAFT: "当前草稿"})
    resume = _resume_journal(
        backend,
        _ok_entry(
            {
                "verdict": "pass",
                "summary": "字符串编号",
                "must_fix": [],
                "items": [
                    {"check": str(n), "result": "pass", "reason": "ok"}
                    for n in range(1, 10)
                ],
            },
            # 升级前的记录没有 reviewDraft 字段——识别只能靠标签。
            review_draft=None,
            draft_sha=None,
        ),
    )
    assert await resume.find_replayable("审稿", PROMPT) is None


@pytest.mark.asyncio
async def test_valid_old_journal_entry_still_replays() -> None:
    """对照：满足现行契约的旧记录（items 齐全、编号是整数）仍可复用。"""
    backend = _GlobBackend({REVIEW_DRAFT: "当前草稿"})
    resume = _resume_journal(
        backend,
        _ok_entry(
            {
                "verdict": "pass",
                "summary": "合法旧记录",
                "must_fix": [],
                "items": NINE_ITEMS,
            },
            review_draft=None,
            draft_sha=None,
        ),
    )
    entry = await resume.find_replayable("审稿", PROMPT)
    assert entry is not None
    assert entry["result"]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_reviewer_type_is_revalidated_even_under_a_custom_label() -> None:
    """A custom workflow may label its reviewer "review" (no 审稿 pattern, no
    reviewDraft on an old record): the current dispatch's subagent type marks
    it reviewer-class, so an old reply missing the nine items is refused."""
    backend = _GlobBackend({REVIEW_DRAFT: "当前草稿"})
    entry = {
        **_ok_entry(
            {"verdict": "pass", "summary": "缺项旧记录", "must_fix": []},
            review_draft=None,
            draft_sha=None,
        ),
        "label": "review",
    }
    backend.files["run-old/journal/review.json"] = json.dumps(
        {"label": "review", "dispatches": [entry]}, ensure_ascii=False
    )
    resume = WorkflowJournal(
        backend,
        base_rel="run-2",
        run_id="task-2",
        workflow="driver-test",
        journal_source="run-old",
        progress_path=None,
    )
    # Without the type, the custom label alone does not mark it reviewer-class…
    assert await resume.find_replayable("review", PROMPT) is not None
    # …the current dispatch's type does, and the incomplete reply is refused.
    assert (
        await resume.find_replayable(
            "review", PROMPT, subagent_type="dalton-reviewer"
        )
        is None
    )
