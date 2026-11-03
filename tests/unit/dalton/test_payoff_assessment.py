"""Payoff assessment on verdicts (P-1a, FINAL_SPEC §3.4/§7/§12.11).

Three layers share the field: the verdict schema + validate combo rules,
the ``verdicts.md`` render (old rows predate the field), and the two
consumers — the price_move push filter (NO_CHANGE with moved odds passes
with a dedicated headline) and the completion contract (stock-move and
thesis-delta verdicts must carry it; a payoff change owes a push draft).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.database.verdicts import get_verdict_for_execution
from src.dalton.knowledge.render import render_verdicts_md
from src.dalton.knowledge.validate import validate_verdict
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_targets,
)
from src.dalton.notify.dispatch import (
    build_feishu_notify_card,
    claim_target_delivery,
    classify_automation_for_notify,
    format_notify_message,
    mark_target_delivery,
    on_automation_settled,
    post_bridge_notify,
)
from src.dalton.settings import get_dalton_frontend_url
from src.dalton.tools import check_done_tools
from src.dalton.tools.check_done_tools import run_check_done
from tests.unit.dalton.test_check_done import (
    MOVE_FILES,
    MOVE_PARAMS,
    MOVE_PUSH,
    WS,
    _delta_files,
    _language_json,
    _make_backend,
    _patch_knowledge,
    _patch_verdicts,
    _verdict_row,
)


def _patch_run_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name a readable start for the conversation run, as the stock-move
    fixtures in test_check_done do (the push-draft freshness check reads it)."""
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(
            check_done_tools._run_start_epoch,
            return_value=int(time.time()) - 3600,
        ),
    )


def _verdict(**overrides: Any) -> dict[str, Any]:
    base = {
        "at": "2026-11-02T00:00:00Z",
        "id": "VD-20261102-01",
        "trigger": {"kind": "price_move"},
        "event": "单日 +6% 异动归因",
        "verdict": "NO_CHANGE",
        "rationale": "板块 beta 带动，无个股层面事件",
        "next_validation": {"what": "次日开盘确认无个股公告", "due": "2026-11-03"},
        "progress": "none",
    }
    base.update(overrides)
    return base


def test_payoff_combo_rules_accept_valid_pairs() -> None:
    for payoff in (
        {"changed": False, "reason": "none"},
        {"changed": True, "reason": "price_crossed_band"},
        {"changed": True, "reason": "scenario_revised"},
        {"changed": True, "reason": "expectations_migrated"},
        {"changed": True, "reason": "catalyst_expired"},
        {"changed": True, "reason": "protection_failed"},
    ):
        result = validate_verdict(_verdict(payoff_assessment=payoff))
        assert result.ok, payoff
    # Absent stays legal: only stock-move / thesis-delta owe the field, and
    # the completion contract (not the schema) enforces that per skill.
    assert validate_verdict(_verdict()).ok


def test_payoff_combo_rules_reject_mismatched_pairs() -> None:
    mismatched = validate_verdict(
        _verdict(
            payoff_assessment={"changed": False, "reason": "price_crossed_band"}
        )
    )
    assert not mismatched.ok
    assert any(e["code"] == "payoff_reason_mismatch" for e in mismatched.errors)

    none_with_change = validate_verdict(
        _verdict(payoff_assessment={"changed": True, "reason": "none"})
    )
    assert not none_with_change.ok
    assert any(e["code"] == "payoff_reason_mismatch" for e in none_with_change.errors)


def test_payoff_shape_is_schema_checked() -> None:
    # reason missing, non-boolean changed, unknown keys inside the object and
    # out-of-enum reasons are all rejected; the enum cases carry a schema
    # error (the combo rule may fire alongside, which is harmless).
    for payoff in (
        {"changed": True},
        {"changed": "yes", "reason": "none"},
        {"changed": False, "reason": "none", "note": "手写备注"},
        {"changed": False, "reason": "made_up_reason"},
    ):
        result = validate_verdict(_verdict(payoff_assessment=payoff))
        assert not result.ok, payoff
        assert any(e["code"] == "schema_error" for e in result.errors), payoff


def _render_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict_id": "VD-20261102-01",
        "word": payload.get("verdict", "NO_CHANGE"),
        "verdict_at": "2026-11-02T10:00:00+00:00",
        "payload": payload,
    }


def test_verdicts_md_renders_payoff_and_tolerates_legacy_rows() -> None:
    legacy = _render_row(_verdict())  # written before the field existed
    unchanged = _render_row(
        _verdict(payoff_assessment={"changed": False, "reason": "none"})
    )
    moved = _render_row(
        _verdict(payoff_assessment={"changed": True, "reason": "price_crossed_band"})
    )
    out = render_verdicts_md([moved, unchanged, legacy], ticker="META")
    assert "赔率变了：股价越过三情景或买点价带" in out
    assert "赔率未变" in out
    assert "—（未评估）" in out
    assert out.count("| 结论 ID |") == 1  # one table, all rows in it


def _dispatch_stubs() -> dict[str, Any]:
    return {
        "enabled_fn": create_autospec(
            resolve_dalton_notify_enabled, return_value=True
        ),
        "overrides_fn": create_autospec(
            resolve_dalton_notify_overrides, return_value={}
        ),
        "targets_fn": create_autospec(
            resolve_dalton_notify_targets, return_value=["feishu:user:ou_analyst_a"]
        ),
        "claim_fn": create_autospec(claim_target_delivery, return_value=True),
        "mark_fn": create_autospec(mark_target_delivery, return_value=None),
        "post_fn": create_autospec(
            post_bridge_notify,
            return_value={
                "ok": True,
                "results": [{"target": "feishu:user:ou_analyst_a", "ok": True}],
            },
        ),
        "url_fn": create_autospec(
            get_dalton_frontend_url, return_value="https://dalton.example.com"
        ),
    }


async def _run_price_move_notify(
    verdict_row: dict[str, Any] | None, *, final_text: str
) -> tuple[bool, Any]:
    stubs = _dispatch_stubs()
    verdict_fn = create_autospec(get_verdict_for_execution, return_value=verdict_row)
    pushed = await on_automation_settled(
        automation={"name": "dalton-meta-move-down", "user_id": "u-1"},
        execution_id="exec-move-payoff",
        thread_id="th-payoff",
        final_text=final_text,
        resolve_enabled_fn=stubs["enabled_fn"],
        resolve_overrides_fn=stubs["overrides_fn"],
        resolve_targets_fn=stubs["targets_fn"],
        get_verdict_fn=verdict_fn,
        claim_target_fn=stubs["claim_fn"],
        mark_target_fn=stubs["mark_fn"],
        post_notify_fn=stubs["post_fn"],
        frontend_url_fn=stubs["url_fn"],
    )
    return pushed, stubs["post_fn"]


@pytest.mark.asyncio
async def test_price_move_no_change_with_moved_odds_is_pushed_with_payoff_title() -> None:
    row = {
        "word": "NO_CHANGE",
        "payload": {
            "payoff_assessment": {"changed": True, "reason": "price_crossed_band"}
        },
    }
    pushed, post_fn = await _run_price_move_notify(
        row, final_text="板块普跌带动下跌。经营与预期均无变化，但价格已跌破三情景价带下沿。"
    )
    assert pushed is True
    post_fn.assert_called_once()
    message_text = post_fn.call_args.args[1]
    card = post_fn.call_args.kwargs["card"]
    expected = "META · 论点未变、赔率变了：股价越过三情景或买点价带"
    assert message_text.splitlines()[0] == expected
    assert card["header"]["title"]["content"] == expected


@pytest.mark.asyncio
async def test_price_move_no_change_without_odds_move_stays_muted() -> None:
    final_text = "板块普跌带动下跌，价带内正常波动。"

    unchanged, post_unchanged = await _run_price_move_notify(
        {
            "word": "NO_CHANGE",
            "payload": {
                "payoff_assessment": {"changed": False, "reason": "none"}
            },
        },
        final_text=final_text,
    )
    assert unchanged is False
    post_unchanged.assert_not_called()

    legacy, post_legacy = await _run_price_move_notify(
        {"word": "NO_CHANGE", "payload": {}}, final_text=final_text
    )
    assert legacy is False
    post_legacy.assert_not_called()

    no_payload, post_none = await _run_price_move_notify(
        {"word": "NO_CHANGE"}, final_text=final_text
    )
    assert no_payload is False
    post_none.assert_not_called()


def test_title_override_only_replaces_the_headline() -> None:
    classification_move = classify_automation_for_notify(
        {"name": "dalton-meta-move-down"}
    )
    assert classification_move is not None
    message = format_notify_message(
        classification_move,
        verdict_word="NO_CHANGE",
        final_text="## 结论\n价格跌破价带下沿。\n",
        thread_id="th-1",
        frontend_base_url="https://dalton.example.com",
        title_override="论点未变、赔率变了：股价越过三情景或买点价带",
    )
    assert (
        message.splitlines()[0] == "META · 论点未变、赔率变了：股价越过三情景或买点价带"
    )
    assert "结论：NO_CHANGE" in message
    card = build_feishu_notify_card(
        classification_move,
        verdict_word="NO_CHANGE",
        final_text=None,
        thread_id="th-1",
        frontend_base_url="https://dalton.example.com",
        title_override="论点未变、赔率变了：股价越过三情景或买点价带",
    )
    assert (
        card["header"]["title"]["content"]
        == "META · 论点未变、赔率变了：股价越过三情景或买点价带"
    )
    # Without the override the default headline stays exactly as before.
    default_card = build_feishu_notify_card(
        classification_move, verdict_word="THESIS_WEAKENED"
    )
    assert default_card["header"]["title"]["content"] == "META · 重大股价异动"


@pytest.mark.asyncio
async def test_stock_move_payoff_gap_heals_by_appending_not_replacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict table is append-only: a fieldless first write is healed by
    APPENDING a payoff-carrying verdict, and the contract reads only this
    run's LATEST verdict — the older fieldless row stays in the window and
    must not block the re-check."""
    _patch_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, [])

    fieldless = _verdict_row(
        "NO_CHANGE", trigger_kind="price_move", run_id="run-pay"
    )
    fieldless["verdict_id"] = "VD-20261023-01"
    fieldless["verdict_at"] = "2026-10-23T10:00:00+00:00"
    _patch_verdicts(monkeypatch, [fieldless])
    result = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-pay",
    )
    assert result["done"] is False
    gap = [m for m in result["missing"] if "缺少 `payoff_assessment`" in m]
    assert gap and "VD-20261023-01" in gap[0]
    assert "再追加一条" in gap[0]

    # The heal: a SECOND verdict appended (the first row is kept — append-only).
    healed_row = _verdict_row(
        "NO_CHANGE",
        trigger_kind="price_move",
        run_id="run-pay",
        payoff={"changed": False, "reason": "none"},
    )
    healed_row["verdict_id"] = "VD-20261023-02"
    healed_row["verdict_at"] = "2026-10-23T11:00:00+00:00"
    _patch_verdicts(monkeypatch, [healed_row, fieldless])  # newest first
    healed = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-pay",
    )
    assert healed["done"] is True, healed["missing"]


@pytest.mark.asyncio
async def test_stock_move_payoff_check_reads_only_this_run_latest_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same window, same thread: another skill's fieldless verdict (earlier
    turn, different run_id) never blocks this run; what counts is the LATEST
    verdict THIS run wrote."""
    _patch_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, [])

    other_skill_row = _verdict_row(
        "NO_CHANGE", trigger_kind="agenda", run_id="run-agenda-earlier"
    )
    other_skill_row["verdict_id"] = "VD-20261023-01"
    other_skill_row["verdict_at"] = "2026-10-23T09:00:00+00:00"
    own_row = _verdict_row(
        "NO_CHANGE",
        trigger_kind="price_move",
        run_id="run-pay",
        payoff={"changed": False, "reason": "none"},
    )
    own_row["verdict_id"] = "VD-20261023-02"
    own_row["verdict_at"] = "2026-10-23T10:00:00+00:00"
    _patch_verdicts(monkeypatch, [own_row, other_skill_row])  # newest first
    result = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-pay",
    )
    assert result["done"] is True, result["missing"]

    # This run's own latest verdict lacks the field -> the gap names it, and
    # the other skill's row is not the excuse.
    own_fieldless = _verdict_row(
        "NO_CHANGE", trigger_kind="price_move", run_id="run-pay-2"
    )
    own_fieldless["verdict_id"] = "VD-20261023-03"
    own_fieldless["verdict_at"] = "2026-10-23T10:30:00+00:00"
    _patch_verdicts(monkeypatch, [own_fieldless, other_skill_row])
    blocked = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-pay-2",
    )
    assert blocked["done"] is False
    gap = [m for m in blocked["missing"] if "缺少 `payoff_assessment`" in m]
    assert gap and "VD-20261023-03" in gap[0]


@pytest.mark.asyncio
async def test_stock_move_no_change_with_moved_odds_owes_a_push_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, [])

    def moved_row(run_id: str) -> dict[str, Any]:
        return _verdict_row(
            "NO_CHANGE",
            trigger_kind="price_move",
            run_id=run_id,
            payoff={"changed": True, "reason": "price_crossed_band"},
        )

    # No push draft -> the skip does not apply, and the gap says why.
    _patch_verdicts(monkeypatch, [moved_row("run-odds")])
    missing_push = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-odds",
    )
    assert missing_push["done"] is False
    push_gap = [m for m in missing_push["missing"] if "缺少推送稿" in m]
    assert push_gap and MOVE_PUSH in push_gap[0]
    assert "payoff_assessment.changed=true" in push_gap[0]
    # The thesis did not change, so no knowledge update is owed.
    assert not any("非 NO_CHANGE" in m for m in missing_push["missing"])

    # Push draft rewritten this round + two language rounds -> done.
    push_text = "## 结论\n论点未变、赔率变了：价格跌破三情景价带下沿。\n"
    files = {**MOVE_FILES, MOVE_PUSH: push_text, f"{MOVE_PUSH}.language.json": _language_json(push_text)}
    done = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-odds",
    )
    assert done["done"] is True, done["missing"]

    # Same verdict shape but odds NOT moved -> no push draft needed again.
    _patch_verdicts(
        monkeypatch,
        [
            _verdict_row(
                "NO_CHANGE",
                trigger_kind="price_move",
                run_id="run-flat",
                payoff={"changed": False, "reason": "none"},
            )
        ],
    )
    flat = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-flat",
    )
    assert flat["done"] is True, flat["missing"]


@pytest.mark.asyncio
async def test_thesis_delta_contract_requires_payoff_on_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, [])
    fieldless = _verdict_row("NO_CHANGE", trigger_kind="agenda")
    fieldless["verdict_id"] = "VD-20261023-01"
    fieldless["verdict_at"] = "2026-10-23T10:00:00+00:00"
    _patch_verdicts(monkeypatch, [fieldless])
    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-23"},
        backend=_make_backend(_delta_files("no", with_note=False)),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"] is False
    assert any("缺少 `payoff_assessment`" in m for m in result["missing"])

    # Heal by appending: the fieldless row stays, the newer one carries the field.
    healed = _verdict_row(
        "NO_CHANGE",
        trigger_kind="agenda",
        payoff={"changed": False, "reason": "none"},
    )
    healed["verdict_id"] = "VD-20261023-02"
    healed["verdict_at"] = "2026-10-23T11:00:00+00:00"
    _patch_verdicts(monkeypatch, [healed, fieldless])
    ok = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-23"},
        backend=_make_backend(_delta_files("no", with_note=False)),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert ok["done"] is True, ok["missing"]


# --- Decision 2: the tool refuses to save a fieldless verdict it can attribute ---

import src.dalton.tools.knowledge_tools as _kt  # noqa: E402

# Specs captured at import: _patch_tool_env may run twice in one test, and a
# second create_autospec must not spec the first patch's Mock.
_REAL_VERIFY_ACCESS = _kt.verify_workspace_access
_REAL_RESOLVE_ORIGIN = _kt._resolve_thread_origin
_REAL_AUTOMATION_NAME_BY_ID = _kt._automation_name_by_id
_REAL_RESOLVE_SIGNALS = _kt.resolve_verdict_signals
_REAL_APPEND_VERDICT = _kt.append_verdict


def _fieldless_verdict(trigger_kind: str) -> dict[str, Any]:
    return {
        "verdict": "NO_CHANGE",
        "event": "e",
        "rationale": "r",
        "progress": "none",
        "trigger": {"kind": trigger_kind, "ref": "sig-1"},
        "next_validation": {"what": "w", "due": "2026-11-05"},
    }


def _payoff_skill_automation_names() -> None:
    import src.dalton.tools.knowledge_tools as kt

    for name in ("dalton-meta-move-up", "dalton-meta-move-down", "dalton-googl-delta"):
        assert kt._PAYOFF_SKILL_AUTO_RE.match(name), name
    for name in (
        "dalton-meta-watch",
        "dalton-director-weekly",
        "my-custom-delta-scan",  # not the standard naming: check_done covers it
    ):
        assert not kt._PAYOFF_SKILL_AUTO_RE.match(name), name


def _patch_tool_env(monkeypatch: pytest.MonkeyPatch, automation_name: str | None) -> Any:
    monkeypatch.setattr(
        _kt,
        "verify_workspace_access",
        create_autospec(
            _REAL_VERIFY_ACCESS, return_value={"workspace_id": "ws-1"}
        ),
    )
    monkeypatch.setattr(
        _kt,
        "_resolve_thread_origin",
        create_autospec(_REAL_RESOLVE_ORIGIN, return_value=("automation", "exec-1")),
    )
    monkeypatch.setattr(
        _kt,
        "_automation_name_by_id",
        create_autospec(_REAL_AUTOMATION_NAME_BY_ID, return_value=automation_name),
    )
    monkeypatch.setattr(
        _kt,
        "resolve_verdict_signals",
        create_autospec(_REAL_RESOLVE_SIGNALS, return_value=0),
    )
    append_stub = create_autospec(
        _REAL_APPEND_VERDICT,
        return_value={"ok": True, "verdict_id": "VD-20261102-01", "message": "saved"},
    )
    monkeypatch.setattr(_kt, "append_verdict", append_stub)
    return append_stub


async def _invoke_verdict_tool(
    verdict: dict[str, Any],
    *,
    thread_id: str | None = None,
    turn_automation_id: str | None = None,
) -> str:
    from src.dalton.tools.knowledge_tools import create_knowledge_tools

    tools = create_knowledge_tools(None)
    verdict_tool = {t.name: t for t in tools}["knowledge_append_verdict"]
    configurable: dict[str, Any] = {"user_id": "u-1", "workspace_id": "ws-1"}
    if thread_id:
        configurable["thread_id"] = thread_id
    if turn_automation_id:
        # What request_prep stamps for a turn the automation itself sent; a
        # manual follow-up in the same thread carries no such key.
        configurable["turn_origin_automation_id"] = turn_automation_id
    res = await verdict_tool.ainvoke(
        {
            "name": "knowledge_append_verdict",
            "id": "tc-payoff",
            "type": "tool_call",
            "args": {"verdict": verdict, "workspace_id": "ws-1"},
        },
        config={"configurable": configurable},
    )
    return str(res.content) if hasattr(res, "content") else str(res)


@pytest.mark.asyncio
async def test_tool_rejects_fieldless_price_move_verdict_at_save_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _payoff_skill_automation_names()
    append_stub = _patch_tool_env(monkeypatch, automation_name=None)

    res = await _invoke_verdict_tool(_fieldless_verdict("price_move"))
    assert res.startswith("ERROR:")
    assert "NOT SAVED" in res
    assert "`payoff_assessment`" in res and "`price_move`" in res
    assert '`{"changed": false, "reason": "none"}`' in res
    append_stub.assert_not_called()

    # Same call with the field present saves as before.
    with_field = {
        **_fieldless_verdict("price_move"),
        "payoff_assessment": {"changed": False, "reason": "none"},
    }
    res_ok = await _invoke_verdict_tool(with_field)
    assert "ERROR" not in res_ok
    append_stub.assert_called_once()


@pytest.mark.asyncio
async def test_tool_rejects_fieldless_verdict_from_payoff_skill_automation_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thesis-delta run woken by a non-price probe (trigger.kind=filing):
    the TURN's automation stamp names the skill, so the save-time gate still
    applies; a watch automation's turn, or a turn with no stamp at all, falls
    through to the completion contract."""
    append_stub = _patch_tool_env(monkeypatch, automation_name="dalton-meta-delta")
    res = await _invoke_verdict_tool(
        _fieldless_verdict("filing"),
        thread_id="th-delta",
        turn_automation_id="auto-delta-1",
    )
    assert res.startswith("ERROR:")
    assert "NOT SAVED" in res and "dalton-meta-delta" in res
    append_stub.assert_not_called()

    # Not one of the two skills: saved; the contract layer is the backstop.
    append_watch = _patch_tool_env(monkeypatch, automation_name="dalton-meta-watch")
    res_watch = await _invoke_verdict_tool(
        _fieldless_verdict("indicator"),
        thread_id="th-watch",
        turn_automation_id="auto-watch-1",
    )
    assert "ERROR" not in res_watch
    append_watch.assert_called_once()

    # An id that resolves to nothing (deleted automation): saved.
    append_unresolved = _patch_tool_env(monkeypatch, automation_name=None)
    res_unresolved = await _invoke_verdict_tool(
        _fieldless_verdict("indicator"),
        thread_id="th-gone",
        turn_automation_id="auto-deleted",
    )
    assert "ERROR" not in res_unresolved
    append_unresolved.assert_called_once()


@pytest.mark.asyncio
async def test_tool_passes_manual_followup_turn_inside_automation_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r3 #1: after a delta automation settles, the user keeps working
    in the SAME thread (filing-review, say). That turn carries no
    ``turn_origin_automation_id`` — the thread's stored origin must not
    answer for it, and a legal fieldless verdict passes untouched."""
    append_stub = _patch_tool_env(monkeypatch, automation_name="dalton-meta-delta")
    res = await _invoke_verdict_tool(
        _fieldless_verdict("filing"), thread_id="th-delta"
    )  # same thread, no per-turn stamp
    assert "ERROR" not in res
    append_stub.assert_called_once()


# --- Decision 3: the unlock and the push-skip stay pinned to THIS workspace ---


@pytest.mark.asyncio
async def test_get_verdict_for_execution_pins_the_named_workspace() -> None:
    """The DB query itself carries the workspace predicate — that is what
    keeps another workspace's verdict from unlocking this one's push."""
    import psycopg
    from src.dalton.database import verdicts as verdicts_db

    captured: dict[str, Any] = {}

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _execute(query: Any, params: Any = None, **_kw: Any) -> None:
        captured["query"] = query
        captured["params"] = params

    async def _fetchone() -> None:
        return None

    cursor.execute.side_effect = _execute
    cursor.fetchone.side_effect = _fetchone
    cursor.__aenter__.return_value = cursor
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection() -> Any:
        yield conn

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdicts_db, "get_db_connection", _get_db_connection)
        await verdicts_db.get_verdict_for_execution(run_id="run-1", workspace_id="ws-meta")
    assert "AND workspace_id = %s" in captured["query"]
    assert captured["params"] == ("run-1", "ws-meta")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdicts_db, "get_db_connection", _get_db_connection)
        await verdicts_db.get_verdict_for_execution(run_id="run-1")
    assert "AND workspace_id" not in captured["query"]
    assert captured["params"] == ("run-1",)

    # codex r3 #3: the trigger pin keeps the price_move gate on the
    # price_move row even when a later other-trigger verdict shares the run.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdicts_db, "get_db_connection", _get_db_connection)
        await verdicts_db.get_verdict_for_execution(
            run_id="run-1", workspace_id="ws-meta", trigger_kind="price_move"
        )
    assert "AND workspace_id = %s" in captured["query"]
    assert "AND trigger_kind = %s" in captured["query"]
    assert captured["params"] == ("run-1", "ws-meta", "price_move")


@pytest.mark.asyncio
async def test_price_move_unlock_reads_only_this_workspaces_verdict() -> None:
    """codex r2 #1: a run that last wrote a NO_CHANGE+changed=true verdict
    into ANOTHER workspace must not see META's price_move push unlocked —
    the dispatch lookup is workspace-pinned, mirroring check_done's scope."""
    stubs = _dispatch_stubs()
    rows_by_workspace: dict[str, dict[str, Any]] = {
        # The odds do move — but for another company's workspace.
        "ws-other": {
            "word": "NO_CHANGE",
            "trigger_kind": "price_move",
            "payload": {
                "payoff_assessment": {"changed": True, "reason": "price_crossed_band"}
            },
        },
        "ws-meta": {
            "word": "NO_CHANGE",
            "trigger_kind": "price_move",
            "payload": {},  # legacy row: not assessed
        },
    }
    verdict_fn = create_autospec(get_verdict_for_execution)

    async def _workspace_pinned_lookup(
        *,
        automation_execution_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
        trigger_kind: str | None = None,
        **_kw: Any,
    ) -> dict[str, Any] | None:
        row = rows_by_workspace.get(workspace_id or "")
        if row is not None and trigger_kind and row.get("trigger_kind") != trigger_kind:
            return None
        return row

    verdict_fn.side_effect = _workspace_pinned_lookup
    pushed = await on_automation_settled(
        automation={"name": "dalton-meta-move-down", "user_id": "u-1"},
        execution_id="exec-move-x",
        thread_id="th-x",
        final_text="板块普跌。本工作区无赔率变化记录。",
        workspace_id="ws-meta",
        resolve_enabled_fn=stubs["enabled_fn"],
        resolve_overrides_fn=stubs["overrides_fn"],
        resolve_targets_fn=stubs["targets_fn"],
        get_verdict_fn=verdict_fn,
        claim_target_fn=stubs["claim_fn"],
        mark_target_fn=stubs["mark_fn"],
        post_notify_fn=stubs["post_fn"],
        frontend_url_fn=stubs["url_fn"],
    )
    assert pushed is False
    stubs["post_fn"].assert_not_called()
    # The lookup really ran with this automation's workspace pinned.
    assert verdict_fn.call_args.kwargs.get("workspace_id") == "ws-meta"

    # Same run, but the odds genuinely moved in THIS workspace: unlocked.
    rows_by_workspace["ws-meta"] = {
        "word": "NO_CHANGE",
        "trigger_kind": "price_move",
        "payload": {
            "payoff_assessment": {"changed": True, "reason": "price_crossed_band"}
        },
    }
    pushed_own = await on_automation_settled(
        automation={"name": "dalton-meta-move-down", "user_id": "u-1"},
        execution_id="exec-move-y",
        thread_id="th-y",
        final_text="板块普跌。股价跌破三情景价带下沿，赔率变化已复核。",
        workspace_id="ws-meta",
        resolve_enabled_fn=stubs["enabled_fn"],
        resolve_overrides_fn=stubs["overrides_fn"],
        resolve_targets_fn=stubs["targets_fn"],
        get_verdict_fn=verdict_fn,
        claim_target_fn=stubs["claim_fn"],
        mark_target_fn=stubs["mark_fn"],
        post_notify_fn=stubs["post_fn"],
        frontend_url_fn=stubs["url_fn"],
    )
    assert pushed_own is True
    stubs["post_fn"].assert_called_once()
    assert (
        "论点未变、赔率变了" in stubs["post_fn"].call_args.args[1].splitlines()[0]
    )
    # The unlock scoped the lookup to the price_move trigger, same as the gate.
    assert verdict_fn.call_args.kwargs.get("trigger_kind") == "price_move"


@pytest.mark.asyncio
async def test_price_move_unlock_ignores_later_other_trigger_verdict_of_same_run() -> None:
    """codex r3 #3: the run appended its price_move NO_CHANGE+changed=true
    verdict, then a fieldless agenda NO_CHANGE one for an unrelated item.
    The unlock reads the price_move row and pushes; without the trigger pin
    the newer agenda row would win and mute the notification."""
    stubs = _dispatch_stubs()
    same_run_rows: list[dict[str, Any]] = [
        {  # newer: written second, LIMIT 1 would pick it unfiltered
            "word": "NO_CHANGE",
            "trigger_kind": "agenda",
            "payload": {},
        },
        {  # the price_move verdict the push gate exists for
            "word": "NO_CHANGE",
            "trigger_kind": "price_move",
            "payload": {
                "payoff_assessment": {"changed": True, "reason": "price_crossed_band"}
            },
        },
    ]
    verdict_fn = create_autospec(get_verdict_for_execution)

    async def _trigger_scoped_lookup(
        *,
        automation_execution_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
        trigger_kind: str | None = None,
        **_kw: Any,
    ) -> dict[str, Any] | None:
        for row in same_run_rows:  # newest first, like the real query
            if trigger_kind and row.get("trigger_kind") != trigger_kind:
                continue
            return row
        return None

    verdict_fn.side_effect = _trigger_scoped_lookup
    pushed = await on_automation_settled(
        automation={"name": "dalton-meta-move-down", "user_id": "u-1"},
        execution_id="exec-move-mixed",
        thread_id="th-mixed",
        final_text="异动归因完成；另处理了一条议程事项。",
        workspace_id="ws-meta",
        resolve_enabled_fn=stubs["enabled_fn"],
        resolve_overrides_fn=stubs["overrides_fn"],
        resolve_targets_fn=stubs["targets_fn"],
        get_verdict_fn=verdict_fn,
        claim_target_fn=stubs["claim_fn"],
        mark_target_fn=stubs["mark_fn"],
        post_notify_fn=stubs["post_fn"],
        frontend_url_fn=stubs["url_fn"],
    )
    assert pushed is True
    stubs["post_fn"].assert_called_once()
    assert (
        "论点未变、赔率变了" in stubs["post_fn"].call_args.args[1].splitlines()[0]
    )


@pytest.mark.asyncio
async def test_push_skip_state_ignores_other_workspace_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_done's push-skip lookup is workspace-pinned too: a changed=true
    verdict written into another workspace must not make THIS workspace's
    stock-move run owe a push draft."""
    from tests.unit.dalton.test_check_done import _patch_verdicts as _patch

    _patch_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, [])

    def _row(run_id: str, verdict_id: str, at: str, **over: Any) -> dict[str, Any]:
        row = _verdict_row("NO_CHANGE", trigger_kind="price_move", run_id=run_id, **over)
        row["verdict_id"] = verdict_id
        row["verdict_at"] = at
        return row

    other_ws_row = _row("run-odds", "VD-20261023-01", "2026-10-23T10:00:00+00:00")
    other_ws_row["workspace_id"] = "ws-other"
    other_ws_row["payload"] = {
        "payoff_assessment": {"changed": True, "reason": "price_crossed_band"}
    }
    _patch(monkeypatch, [other_ws_row])
    result = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-odds",
    )
    # The other workspace's changed=true cannot skip this run's push draft,
    # so the draft is owed — and the payoff note explains nothing (the row
    # this run owns was never read as a payoff change).
    assert result["done"] is False
    gaps = [m for m in result["missing"] if "缺少推送稿" in m and MOVE_PUSH in m]
    assert gaps and "changed=true" not in gaps[0]


@pytest.mark.asyncio
async def test_push_skip_state_reads_only_price_move_verdict_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r3 #3 at the contract layer: this run wrote its price_move
    NO_CHANGE+changed=true verdict, then a fieldless agenda NO_CHANGE one for
    an unrelated item. The push-draft skip reads the price_move row (the
    contract's verdict_trigger), so the draft is still owed and the gap
    carries the payoff note; the newer agenda row must not mute it."""
    _patch_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, [])

    def _row(
        run_id: str, verdict_id: str, at: str, trigger: str, **over: Any
    ) -> dict[str, Any]:
        row = _verdict_row("NO_CHANGE", trigger_kind=trigger, run_id=run_id, **over)
        row["verdict_id"] = verdict_id
        row["verdict_at"] = at
        return row

    agenda_row = _row("run-mixed", "VD-20261023-02", "2026-10-23T10:30:00+00:00", "agenda")
    price_move_row = _row(
        "run-mixed",
        "VD-20261023-01",
        "2026-10-23T10:00:00+00:00",
        "price_move",
        payoff={"changed": True, "reason": "price_crossed_band"},
    )
    _patch_verdicts(monkeypatch, [agenda_row, price_move_row])  # newest first
    result = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-mixed",
    )
    assert result["done"] is False
    gaps = [m for m in result["missing"] if "缺少推送稿" in m and MOVE_PUSH in m]
    assert gaps and "payoff_assessment.changed=true" in gaps[0]
    # The closing payoff check is trigger-scoped too (verdict_trigger in the
    # contract): the agenda row never reaches it, so no payoff gap reports.
    assert not any("缺少 `payoff_assessment`" in m for m in result["missing"])
