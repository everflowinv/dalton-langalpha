"""Unit tests for Dalton autonomous agenda, 4 probes, LLM triage, signal lifecycle, and CIK initialization (PK.14–PK.20, G4.1–G4.5, H3 A1–A14)."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

from psycopg import AsyncCursor
import pytest

from src.dalton.autonomy.backoff import (
    clear_probe_backoff,
    compute_failure_backoff_seconds,
    is_in_backoff,
    next_backoff_minutes,
    record_probe_backoff,
)
from src.dalton.autonomy.board import build_coverage_board
from src.dalton.autonomy.preflight import (
    _check_budget_available,
    format_wake_directive,
    is_dalton_automation,
    is_direct_wake_signal,
    preflight_check,
)
from src.dalton.autonomy.probes import COMPANY_PROBES
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.director import probe_director
from src.dalton.autonomy.probes.earnings import probe_earnings
from src.dalton.autonomy.probes.internal import probe_internal
from src.dalton.autonomy.probes.sec import probe_sec, resolve_company_cik
from src.dalton.autonomy.schedule import (
    build_company_watch_specs,
    build_earnings_window_specs,
    schedule_dalton_automation_helper,
    validate_automation_name,
)
from src.dalton.budget import service as budget_service
from src.dalton.database import watch as watch_db
from src.dalton.knowledge.ops import apply_ops, repair_knowledge_ops
from src.dalton.knowledge.validate import validate_update
from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.tools.knowledge_tools import (
    extract_verdict_signal_refs,
    resolve_verdict_signals,
)
from src.server.services.automation_settlement import _POLICIES, Outcome, _schedule_action


def test_four_company_probes_and_deleted_price_consensus():
    """Verify price.py and consensus.py are removed and COMPANY_PROBES has exact 4 probes (G4.1)."""
    assert COMPANY_PROBES == ("sec", "internal", "agenda", "earnings")
    assert importlib.util.find_spec("src.dalton.autonomy.probes.price") is None
    assert importlib.util.find_spec("src.dalton.autonomy.probes.consensus") is None


def test_backoff_is_purely_time_based():
    assert next_backoff_minutes(1) == 2
    assert next_backoff_minutes(2) == 10
    assert next_backoff_minutes(3) == 30
    assert next_backoff_minutes(4) == 120
    assert next_backoff_minutes(99) == 120

    assert compute_failure_backoff_seconds(1) == 3600
    assert compute_failure_backoff_seconds(2) == 7200
    assert compute_failure_backoff_seconds(99) == 604800

    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    state: dict = {}
    state = record_probe_backoff(state, "sec", "HTTP 503", now=now)
    in_bo, nb = is_in_backoff(state, "sec", now=now + timedelta(minutes=1))
    assert in_bo is True
    assert nb is not None

    in_bo_after, _ = is_in_backoff(state, "sec", now=now + timedelta(minutes=3))
    assert in_bo_after is False

    state = clear_probe_backoff(state, "sec")
    in_bo_cleared, _ = is_in_backoff(state, "sec", now=now)
    assert in_bo_cleared is False


@pytest.mark.asyncio
async def test_automation_naming_templates_and_schedule_helper(monkeypatch):
    ok, err = validate_automation_name("dalton-meta-watch", ticker="META")
    assert ok is True
    assert err is None

    ok_bad, err_bad = validate_automation_name("dalton-amzn-watch", ticker="META")
    assert ok_bad is False
    assert "dalton-meta-" in (err_bad or "")

    specs = build_company_watch_specs(ticker="META", workspace_id="ws-meta", tier="Core")
    names = [s["name"] for s in specs]
    assert names == [
        "dalton-meta-watch",
        "dalton-meta-delta",
        "dalton-meta-move-up",
        "dalton-meta-move-down",
    ]
    # A13: verify cron/threshold alignment with docs/dalton/dev-guide/automation-templates/
    assert specs[0]["cron_expression"] == "*/30 6-20 * * 1-5"
    assert specs[1]["cron_expression"] == "30 21 * * 1-5"
    assert specs[2]["trigger_config"]["conditions"][0]["value"] == 4.0
    assert specs[3]["trigger_config"]["conditions"][0]["value"] == 4.0

    earn_specs = build_earnings_window_specs(
        ticker="META",
        workspace_id="ws-meta",
        period="2026Q3",
        report_at="2026-10-29T20:00:00",  # A8: naive ISO string gets timezone attached
    )
    assert [s["name"] for s in earn_specs] == [
        "dalton-meta-earn-2026q3-tm3",
        "dalton-meta-earn-2026q3-t0",
        "dalton-meta-earn-2026q3-tp1",
    ]
    assert earn_specs[0]["next_run_at"].tzinfo == ZoneInfo("Asia/Shanghai")

    # Test idempotent schedule_dalton_automation_helper (G4.5, A8) with create_autospec (L33)
    from src.server.database import automation as auto_db
    from src.server.database import workspace as ws_db

    mock_get_workspace = create_autospec(
        ws_db.get_workspace,
        return_value={"workspace_id": "ws-meta", "user_id": "user-1"},
    )
    monkeypatch.setattr(ws_db, "get_workspace", mock_get_workspace)

    store: list[dict[str, Any]] = []

    mock_list = create_autospec(auto_db.list_automations)
    mock_create = create_autospec(auto_db.create_automation)
    mock_update = create_autospec(auto_db.update_automation)

    async def _list_impl(
        user_id: str,
        status: str | None = None,
        thread_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        return [r for r in store if not workspace_id or r.get("workspace_id") == workspace_id], len(store)

    async def _create_impl(
        user_id: str,
        name: str,
        trigger_type: str,
        instruction: str,
        *,
        file_name: str | None = None,
        description: str | None = None,
        cron_expression: str | None = None,
        timezone: str = "UTC",
        trigger_config: dict[str, Any] | None = None,
        next_run_at: datetime | None = None,
        workspace_id: str | None = None,
        llm_model: str | None = None,
        additional_context: list[dict[str, Any]] | None = None,
        thread_strategy: str = "new",
        conversation_thread_id: str | None = None,
        max_failures: int = 3,
        delivery_config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        conn=None,
    ):
        row = {
            "automation_id": f"auto-{len(store) + 1}",
            "user_id": user_id,
            "name": name,
            "trigger_type": trigger_type,
            "instruction": instruction,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "trigger_config": trigger_config or {},
            "next_run_at": next_run_at,
            "workspace_id": workspace_id,
            "description": description,
            "metadata": metadata or {},
        }
        store.append(row)
        return row

    async def _update_impl(
        automation_id: str,
        user_id: str,
        *,
        conn=None,
        instruction: str | None = None,
        description: str | None = None,
        cron_expression: str | None = None,
        timezone: str | None = None,
        trigger_config: dict[str, Any] | None = None,
        next_run_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
    ):
        for r in store:
            if r["automation_id"] == automation_id:
                if instruction is not None:
                    r["instruction"] = instruction
                if description is not None:
                    r["description"] = description
                if cron_expression is not None:
                    r["cron_expression"] = cron_expression
                if timezone is not None:
                    r["timezone"] = timezone
                if trigger_config is not None:
                    r["trigger_config"] = trigger_config
                if next_run_at is not None:
                    r["next_run_at"] = next_run_at
                if metadata is not None:
                    r["metadata"] = metadata
                if status is not None:
                    r["status"] = status
                return r
        return None

    mock_list.side_effect = _list_impl
    mock_create.side_effect = _create_impl
    mock_update.side_effect = _update_impl

    monkeypatch.setattr(auto_db, "list_automations", mock_list)
    monkeypatch.setattr(auto_db, "create_automation", mock_create)
    monkeypatch.setattr(auto_db, "update_automation", mock_update)

    res_create = await schedule_dalton_automation_helper(
        "dalton-meta-watch",
        specs[0],
        user_id="user-1",
        workspace_id="ws-meta",
    )
    assert res_create["ok"] is True
    assert res_create["action"] == "created"

    # A8: naive next_run_at gets ZoneInfo("Asia/Shanghai") attached
    res_once = await schedule_dalton_automation_helper(
        "dalton-meta-earn-2026q3-t0",
        {
            "name": "dalton-meta-earn-2026q3-t0",
            "trigger_type": "once",
            "next_run_at": "2026-10-29T20:00:00",
            "timezone": "Asia/Shanghai",
            "instruction": "T0 flash",
        },
        user_id="user-1",
        workspace_id="ws-meta",
    )
    assert res_once["ok"] is True
    assert store[-1]["next_run_at"].tzinfo == ZoneInfo("Asia/Shanghai")

    res_update = await schedule_dalton_automation_helper(
        user_id="user-1",
        workspace_id="ws-meta",
        name="dalton-meta-watch",
        entry={**specs[0], "description": "updated description"},
    )
    assert res_update["ok"] is True
    assert res_update["action"] == "updated"
    assert len(store) == 2


@pytest.mark.asyncio
async def test_probes_detect_real_signals_and_resolve_cik():
    today = date(2026, 10, 18)
    now_utc = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    agenda_doc = {
        "next_validations": [
            {
                "id": "V1",
                "check": "核对 3Q26 广告加载率环比变化",
                "due": "2026-10-18",
            }
        ],
        "watch": {
            "events": [
                {
                    "id": "W1",
                    "type": "earnings",
                    "date": "2026-10-20",
                    "date_confidence": "confirmed",
                    "plan": "Q3 2026 财报披露",
                }
            ]
        },
        "open_questions": [
            {
                "id": "Q1",
                "question": "Reels 广告加载率上限",
                "status": "open",
                "priority": "P0",
                "due_date": "2026-10-18",
            },
            {
                "id": "Q2",
                "question": "处于退避期的问题不应唤醒 (A11)",
                "status": "open",
                "priority": "P0",
                "due_date": "2026-10-18",
                "next_attempt_after": "2026-10-25T00:00:00Z",
            },
        ],
        "wake_conditions": ["10-Q 披露后更新收入拆分"],
    }
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=agenda_doc,
        today=today,
        now=now_utc,
    )
    assert {s.signal_key for s in sigs} == {
        "validation:V1:2026-10-18",
        "catalyst:W1:2026-10-20",
        "question:Q1:2026-10-18",
    }

    # A4: Also test against real migrated GOOGL agenda.json on disk if present
    googl_agenda_path = Path("var/dalton/migration/Home/dalton/companies/GOOGL/data/agenda.json")
    if googl_agenda_path.exists():
        real_googl_agenda = json.loads(googl_agenda_path.read_text(encoding="utf-8"))
        real_googl_agenda = copy.deepcopy(real_googl_agenda)
        real_googl_agenda.setdefault("next_validations", []).append(
            {"id": "V99", "check": "验证 Search AI Overview 货币化率", "due": "2026-10-18"}
        )
        googl_sigs, _ = await probe_agenda(
            workspace_id=None,
            ticker="GOOGL",
            watch_state={},
            agenda_doc=real_googl_agenda,
            today=today,
            now=now_utc,
        )
        assert any(s.signal_key == "validation:V99:2026-10-18" for s in googl_sigs)

    # SEC probe: resolves CIK automatically and records watermark on first run; second run detects new 10-Q & 8-K items
    cik = await resolve_company_cik("NVDA", watch_state={}, allow_network=False)
    assert cik == "0001045810"

    submissions_v1 = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001326801-26-000010"],
                "form": ["10-Q"],
                "filingDate": ["2026-07-30"],
                "primaryDocument": ["meta-20260630.htm"],
            }
        }
    }
    sec_sigs_1, st = await probe_sec(
        ticker="META", watch_state={}, submissions_override=submissions_v1
    )
    assert sec_sigs_1 == []
    assert st["last_sec_accession"] == "0001326801-26-000010"
    assert st["cik"] == "0001326801"

    submissions_v2 = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001326801-26-000025", "0001326801-26-000010"],
                "form": ["8-K", "10-Q"],
                "filingDate": ["2026-10-29", "2026-07-30"],
                "primaryDocument": ["meta-8k.htm", "meta-20260630.htm"],
                "items": ["2.02,9.01", ""],
            }
        }
    }
    sec_sigs_2, st2 = await probe_sec(
        ticker="META", watch_state=st, submissions_override=submissions_v2
    )
    assert len(sec_sigs_2) == 1
    assert sec_sigs_2[0].signal_key == "filing:0001326801-26-000025"
    assert sec_sigs_2[0].payload["items"] == ["2.02", "9.01"]
    assert st2["last_sec_accession"] == "0001326801-26-000025"

    # Internal probe: detects new docs and populates snippet & watermark (A6)
    int_sigs, ist = await probe_internal(
        ticker="META",
        watch_state={},
        docs_override=[
            {
                "doc_id": "gp-101",
                "source": "guidepoint",
                "title": "Meta Reels Ad Load Expert Call",
                "snippet": "Expert noted +15% CPM lift in Q3.",
                "published_at": "2026-10-18T10:00:00Z",
            }
        ],
    )
    assert len(int_sigs) == 1
    assert int_sigs[0].signal_key == "doc:guidepoint:gp-101"
    assert int_sigs[0].payload["snippet"] == "Expert noted +15% CPM lift in Q3."
    assert ist["last_internal_doc_id"] == "gp-101"
    assert "last_internal_check_at" in ist

    # Earnings probe T-3 reading watch.events
    earn_sigs, _ = await probe_earnings(
        ticker="META",
        watch_state={},
        agenda_doc={
            "watch": {
                "events": [
                    {
                        "id": "W1",
                        "type": "earnings",
                        "date": "2026-10-21",
                        "date_confidence": "confirmed",
                        "plan": "2026Q3 财报",
                    }
                ]
            }
        },
        today=today,
    )
    assert len(earn_sigs) == 1
    assert "tm3" in earn_sigs[0].signal_key


@pytest.mark.asyncio
async def test_preflight_direct_wake_vs_llm_triage_and_fallback(monkeypatch):
    """Test G4.3 + A2 + L33: 10-Q/10-K/20-F/8-K(2.02)/agenda/earnings wake directly; Form 4/internal go to assist_json(preflight.triage) verified via create_autospec."""
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "10-Q"}}) is True
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "10-K"}}) is True
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "20-F"}}) is True
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "8-K", "items": ["2.02", "9.01"]}}) is True
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "8-K", "items": ["5.02"]}}) is False
    assert is_direct_wake_signal({"probe": "sec", "payload": {"form": "4"}}) is False
    assert is_direct_wake_signal({"probe": "internal", "payload": {"doc_id": "1"}}) is False

    auto = {
        "automation_id": "11111111-1111-1111-1111-111111111111",
        "name": "dalton-meta-watch",
        "trigger_type": "cron",
        "user_id": "local-dev-user",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    assert is_dalton_automation(auto) is True
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "user_id": "local-dev-user",
            },
        ),
    )

    # Case 1: Routine Form 4 -> assist_json (autospec!) returns wake=False -> preflight skips
    mock_assist_skip = create_autospec(assist_json)
    mock_assist_skip.return_value = AssistResult(
        ok=True,
        data={
            "wake": False,
            "priority": "P2",
            "reason": "routine_form4_tax_withholding",
            "relevant_pillar_or_question_id": None,
        },
        model="gemini-3.8-flash-low-oauth",
    )
    monkeypatch.setattr("src.dalton.autonomy.preflight.assist_json", mock_assist_skip)

    ws_form4 = {
        "cik": "0001326801",
        "last_sec_accession": "0001326801-26-000001",
        "sec_submissions_mock": {
            "filings": {
                "recent": {
                    "accessionNumber": ["0001326801-26-000002", "0001326801-26-000001"],
                    "form": ["4", "10-Q"],
                    "filingDate": ["2026-10-18", "2026-07-30"],
                    "primaryDocument": ["form4.xml", "10q.htm"],
                }
            }
        },
    }
    res_form4 = await preflight_check(
        auto,
        "exec-form4",
        watch_state_override=ws_form4,
        agenda_doc_override={},
        persist=False,
    )
    assert mock_assist_skip.await_count == 1
    call_kwargs = mock_assist_skip.call_args.kwargs
    assert call_kwargs["task"] == "preflight.triage"
    assert isinstance(call_kwargs["instructions"], str) and call_kwargs["instructions"]
    assert isinstance(call_kwargs["payload"], dict) and call_kwargs["payload"]["ticker"] == "META"
    assert isinstance(call_kwargs["schema"], dict)
    assert call_kwargs["user_id"] == "local-dev-user"
    assert call_kwargs["workspace_id"] == "22222222-2222-2222-2222-222222222222"
    assert res_form4.should_wake is False
    assert res_form4.decision == "skip"
    assert res_form4.reason == "routine_form4_tax_withholding"

    # Case 2: Internal doc where assist_json returns ok=False -> conservative fallback MUST wake!
    mock_assist_fail = create_autospec(assist_json)
    mock_assist_fail.return_value = AssistResult(
        ok=False,
        data=None,
        error="timeout",
        status="failed",
    )
    monkeypatch.setattr("src.dalton.autonomy.preflight.assist_json", mock_assist_fail)

    ws_internal = {
        "pending_internal_docs": [
            {
                "doc_id": "ae-900",
                "source": "alphaengine",
                "title": "Meta AI CapEx Supply Chain Check",
                "snippet": "H200 server orders revised up 20%.",
            }
        ]
    }
    res_fallback = await preflight_check(
        auto,
        "exec-fallback",
        watch_state_override=ws_internal,
        agenda_doc_override={},
        persist=False,
    )
    assert res_fallback.should_wake is True
    assert res_fallback.decision == "wake"
    assert len(res_fallback.signals) == 1


@pytest.mark.asyncio
async def test_a1_budget_keys_a3_timeout_a5_agenda_note_a7_director_a9_once_defer_a10_concurrent_claim(monkeypatch):
    """Test A1 (budget keys), A3 (45s timeout), A5 (agenda_note schema), A7 (director idle skip), A9 (once defer), and A10 (concurrent claim)."""
    # A1: Lock _check_budget_available and build_coverage_board against real get_budget_today return shape
    real_budget_summary = {
        "date": "2026-10-18",
        "cap_usd": 45.0,
        "spent_usd": 40.0,
        "open_reserved_usd": 5.0,
        "remaining_usd": 0.0,
        "exhausted": True,
    }
    mock_get_budget = create_autospec(budget_service.get_budget_today)
    mock_get_budget.return_value = real_budget_summary
    monkeypatch.setattr(budget_service, "get_budget_today", mock_get_budget)
    monkeypatch.setattr("src.dalton.autonomy.board.get_budget_today", mock_get_budget)

    budget_ok, budget_reason = await _check_budget_available(user_id="local-dev-user")
    assert budget_ok is False
    assert budget_reason == "budget_exhausted"
    mock_get_budget.assert_awaited_with(user_id="local-dev-user")

    board = await build_coverage_board(user_id="local-dev-user")
    assert board["budget"]["cap_usd"] == 45.0
    assert board["budget"]["spent_usd"] == 40.0
    assert board["budget"]["open_reserved_usd"] == 5.0
    assert board["budget"]["allocatable_usd"] == 0.0

    # A9: When budget is exhausted on a 'once' automation, preflight postpones next_run_at by +1d and settlement uses defer_once_1d
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "user_id": "local-dev-user",
            },
        ),
    )
    mock_log_budget = create_autospec(watch_db.insert_preflight_log)
    mock_log_budget.return_value = {}
    monkeypatch.setattr(watch_db, "insert_preflight_log", mock_log_budget)

    orig_next_run = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)
    once_auto = {
        "automation_id": "11111111-1111-1111-1111-111111111111",
        "name": "dalton-meta-earn-2026q3-t0",
        "trigger_type": "once",
        "user_id": "local-dev-user",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
        "next_run_at": orig_next_run,
    }
    res_budget_once = await preflight_check(
        once_auto,
        "exec-once-budget",
        persist=True,
    )
    assert res_budget_once.should_wake is False
    assert res_budget_once.reason == "budget_exhausted"
    assert once_auto["status"] == "active"
    assert once_auto["next_run_at"] == orig_next_run + timedelta(days=1)
    assert _schedule_action(_POLICIES[Outcome.PRECHECK_SKIPPED], once_auto, outcome=Outcome.PRECHECK_SKIPPED, excerpt="budget_exhausted ($0.00 remaining)") == "defer_once_1d"

    # Restore ample budget for remaining tests
    mock_get_budget.return_value = {
        "date": "2026-10-18",
        "cap_usd": 45.0,
        "spent_usd": 5.0,
        "open_reserved_usd": 0.0,
        "remaining_usd": 40.0,
        "exhausted": False,
    }

    # A3: 45s timeout (tested with timeout_s=0.05) wakes conservatively with reason="preflight_timeout"
    async def slow_probe_sec(workspace_id=None, ticker="", watch_state=None, company_doc=None, submissions_override=None, allow_network=True, now=None):
        await asyncio.sleep(0.3)
        return [], watch_state or {}

    monkeypatch.setattr("src.dalton.autonomy.preflight.probe_sec", slow_probe_sec)
    res_timeout = await preflight_check(
        {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "trigger_type": "cron",
            "user_id": "local-dev-user",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
        },
        "exec-timeout",
        persist=False,
        timeout_s=0.05,
    )
    assert res_timeout.should_wake is True
    assert res_timeout.reason == "preflight_timeout"
    monkeypatch.setattr("src.dalton.autonomy.preflight.probe_sec", probe_sec)

    # A5: dalton_agenda_note op passes apply_ops + validate_update("agenda", ...) against real agenda.schema.json
    base_agenda = {
        "schema_version": 1,
        "as_of": "2026-10-18T00:00:00Z",
        "open_questions": [],
        "next_validations": [],
        "watch": {"events": [], "indicators": []},
        "wake_conditions": [],
    }
    note_ops = [
        {
            "op": "append_item",
            "path": "/open_questions",
            "value": {
                "question": "核实 Q4 指引隐含的 CapEx 增速",
                "value": "high",
                "urgency": "now",
                "origin": "pm",
                "owner": "pm",
                "status": "open",
                "why": "PM 经研究总监追加",
            },
        }
    ]
    repaired_ops, _ = repair_knowledge_ops("agenda", base_agenda, note_ops)
    updated_agenda, _ = apply_ops("agenda", base_agenda, repaired_ops, reason="director note")
    val_agenda = validate_update("agenda", base_agenda, updated_agenda, source="tool", reason="director note")
    assert val_agenda.ok is True, val_agenda.errors
    assert updated_agenda["open_questions"][0]["owner"] == "pm"
    assert updated_agenda["open_questions"][0]["urgency"] == "now"

    # A7: probe_director skips on idle day and wakes when actionable work exists
    idle_sigs, _ = await probe_director(
        kind="director_daily",
        watch_state={
            "actionable_counts": {
                "due_validations": 0,
                "actionable_questions": 0,
                "stale_signals_24h": 0,
                "publish_near_or_past_ttl": 0,
                "unconfirmed_earnings_14d": 0,
            }
        },
        persist=False,
    )
    assert idle_sigs == []

    busy_sigs, _ = await probe_director(
        kind="director_daily",
        watch_state={
            "actionable_counts": {
                "due_validations": 2,
                "actionable_questions": 0,
                "stale_signals_24h": 1,
                "publish_near_or_past_ttl": 0,
                "unconfirmed_earnings_14d": 0,
            }
        },
        persist=False,
    )
    assert len(busy_sigs) == 1
    assert busy_sigs[0].probe == "director"

    # A10: concurrent claim returning empty list causes preflight_check to skip with already_claimed_by_concurrent_run
    mock_upsert = create_autospec(watch_db.upsert_signal)
    mock_upsert.return_value = (
        {"signal_id": "sig-1", "signal_key": "question:Q9:2026-10-18", "status": "pending"},
        True,
    )
    mock_claim = create_autospec(watch_db.claim_signals_woken)
    mock_claim.return_value = []
    mock_get_ws = create_autospec(watch_db.get_watch_state)
    mock_get_ws.return_value = {}
    mock_upsert_ws = create_autospec(watch_db.upsert_watch_state)
    mock_upsert_ws.return_value = {}
    mock_expire = create_autospec(watch_db.expire_old_signals)
    mock_expire.return_value = []

    mock_probe_sec = create_autospec(probe_sec)
    mock_probe_sec.return_value = ([], {})
    mock_probe_internal = create_autospec(probe_internal)
    mock_probe_internal.return_value = ([], {})

    monkeypatch.setattr(watch_db, "upsert_signal", mock_upsert)
    monkeypatch.setattr(watch_db, "claim_signals_woken", mock_claim)
    monkeypatch.setattr(watch_db, "get_watch_state", mock_get_ws)
    monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
    monkeypatch.setattr(watch_db, "expire_old_signals", mock_expire)
    monkeypatch.setattr("src.dalton.autonomy.preflight.probe_sec", mock_probe_sec)
    monkeypatch.setattr("src.dalton.autonomy.preflight.probe_internal", mock_probe_internal)

    today_iso = datetime.now(timezone.utc).date().isoformat()
    res_concurrent = await preflight_check(
        {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "trigger_type": "cron",
            "user_id": "local-dev-user",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
        },
        "33333333-3333-3333-3333-333333333333",
        watch_state_override={},
        agenda_doc_override={
            "open_questions": [
                {"id": "Q9", "question": "Concurrent test", "status": "open", "priority": "P0", "due_date": today_iso}
            ]
        },
        persist=True,
    )
    assert res_concurrent.should_wake is False
    assert res_concurrent.reason == "already_claimed_by_concurrent_run"


@pytest.mark.asyncio
async def test_preflight_check_skips_when_no_signals_and_wakes_once(monkeypatch):
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "user_id": "local-dev-user",
            },
        ),
    )
    auto = {
        "automation_id": "11111111-1111-1111-1111-111111111111",
        "name": "dalton-meta-watch",
        "trigger_type": "cron",
        "user_id": "local-dev-user",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    watch_state: dict = {}

    # 1. Empty agenda & no signals -> skip with 0 tokens
    res_skip = await preflight_check(
        auto,
        "exec-1",
        watch_state_override=watch_state,
        agenda_doc_override={"next_validations": [], "open_questions": []},
        persist=False,
    )
    assert res_skip.should_wake is False
    assert res_skip.skip is True
    assert res_skip.decision == "skip"
    assert res_skip.reason == "no_new_signals"

    # 2. Due open question -> wake with <dalton_wake> directive
    today_iso = datetime.now(timezone.utc).date().isoformat()
    agenda_with_due = {
        "open_questions": [
            {
                "id": "Q9",
                "question": "AI CapEx ROI 拐点验证",
                "status": "open",
                "priority": "P0",
                "due_date": today_iso,
            }
        ]
    }
    res_wake = await preflight_check(
        auto,
        "exec-2",
        watch_state_override=watch_state,
        agenda_doc_override=agenda_with_due,
        persist=False,
    )
    assert res_wake.should_wake is True
    assert res_wake.skip is False
    assert res_wake.decision == "wake"
    assert res_wake.wake_context is not None
    assert "<dalton_wake" in res_wake.wake_context
    assert "Q9" in res_wake.wake_context

    # 3. Second check with the exact same signal -> deduplicated -> skip!
    res_dup = await preflight_check(
        auto,
        "exec-3",
        watch_state_override=watch_state,
        agenda_doc_override=agenda_with_due,
        persist=False,
    )
    assert res_dup.should_wake is False
    assert res_dup.reason == "no_new_signals"


@pytest.mark.asyncio
async def test_signal_lifecycle_resolve_and_expire(monkeypatch):
    """Test G4.2: signal resolution from verdict trigger_signal and 7-day signal expiration."""
    refs = extract_verdict_signal_refs(
        {
            "word": "THESIS_STRENGTHENED",
            "trigger_signal": "filing:0001045810-26-000012",
            "trigger_signals": ["validation:V1:2026-10-18", {"signal_key": "question:Q9:2026-10-18"}],
        }
    )
    assert refs == [
        "filing:0001045810-26-000012",
        "validation:V1:2026-10-18",
        "question:Q9:2026-10-18",
    ]

    mock_resolve = create_autospec(watch_db.resolve_signals)
    mock_resolve.side_effect = (
        lambda signal_ids=None, *, signal_refs=None, workspace_id=None, ticker=None, resolved_by=None, execution_id=None: len(signal_ids or [])
    )
    monkeypatch.setattr(watch_db, "resolve_signals", mock_resolve)

    count = await resolve_verdict_signals(
        workspace_id="22222222-2222-2222-2222-222222222222",
        verdict={"word": "NO_CHANGE", "trigger_signal": "filing:0001045810-26-000012"},
        verdict_id="VD-20261018-01",
    )
    assert count == 1
    assert mock_resolve.call_args.args[0] == ["filing:0001045810-26-000012"]
    assert mock_resolve.call_args.kwargs["resolved_by"] == "VD-20261018-01"

    # Test expire_old_signals with mock cursor
    mock_cur = create_autospec(AsyncCursor, instance=True)
    mock_cur.fetchall.return_value = [
        {"signal_id": "sig-old-1", "signal_key": "filing:old", "status": "pending"}
    ]
    expired = await watch_db.expire_old_signals(
        mock_cur,
        max_age_days=7,
        workspace_id="22222222-2222-2222-2222-222222222222",
    )
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"
    sql_executed = mock_cur.execute.call_args[0][0]
    assert "expired_at = NOW()" in sql_executed


def test_format_wake_directive():
    xml = format_wake_directive(
        automation_name="dalton-meta-watch",
        ticker="META",
        signals=[
            {
                "probe": "sec",
                "priority": "P0",
                "signal_key": "filing:0001",
                "summary": "META 新 SEC 披露：10-Q",
            }
        ],
    )
    assert '<dalton_wake automation="dalton-meta-watch" ticker="META"' in xml
    assert "knowledge_append_verdict" in xml
    assert "trigger_signals: [filing:0001]" in xml
    assert "</dalton_wake>" in xml


@pytest.mark.asyncio
async def test_p0i_i5_autonomy_low_risk_fixes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify P0I §5 #1-#7 low-risk autonomy fixes."""
    import importlib.util
    from pathlib import Path
    from src.dalton.autonomy import preflight as preflight_mod
    from src.dalton.autonomy.probes import internal as internal_mod
    from src.dalton.autonomy.probes import sec as sec_mod
    from src.dalton.budget import service as budget_svc
    from src.dalton.tools import director_tools as dt_mod

    # 1. director_tools passes user_id from config["configurable"]["user_id"] to build_coverage_board
    mock_board = create_autospec(
        dt_mod.build_coverage_board,
        return_value={
            "date": "2026-10-19",
            "budget": {
                "cap_usd": 20.0,
                "spent_usd": 2.0,
                "remaining_usd": 18.0,
                "reserve_for_events_usd": 3.0,
                "allocatable_usd": 15.0,
            },
            "companies": [],
            "candidates": [],
        },
    )
    monkeypatch.setattr(dt_mod, "build_coverage_board", mock_board)
    tools = {t.name: t for t in dt_mod.build_director_tools(role="chief_of_staff")}
    await tools["dalton_coverage_board"].ainvoke(
        {}, config={"configurable": {"user_id": "pm-user-42"}}
    )
    mock_board.assert_awaited_once_with(user_id="pm-user-42")

    # 2. internal.py logs warning once per hour when bridge is unconfigured
    monkeypatch.delenv("DALTON_BRIDGE_MCP_URL", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(internal_mod, "_LAST_UNCONFIGURED_WARN_AT", 0.0)
    with caplog.at_level("WARNING"):
        res1 = await internal_mod._query_bridge_internal_docs(
            ticker="META", workspace_id="ws-1", allow_network=True
        )
        res2 = await internal_mod._query_bridge_internal_docs(
            ticker="META", workspace_id="ws-1", allow_network=True
        )
    assert res1 == [] and res2 == []
    unconf_warns = [
        r for r in caplog.records if "neither DALTON_BRIDGE_MCP_URL nor DALTON_BRIDGE_TOKEN" in r.message
    ]
    assert len(unconf_warns) == 1

    # 3. internal.py runs 3 routes in parallel and enforces 10s step timeout
    monkeypatch.setenv("DALTON_BRIDGE_MCP_URL", "http://127.0.0.1:8960/mcp/test")
    monkeypatch.setattr(internal_mod, "INTERNAL_PROBE_STEP_TIMEOUT_S", 0.05)

    def _slow_bridge(**kwargs: Any) -> list[dict[str, Any]]:
        import time as _t

        _t.sleep(0.2)
        return []

    monkeypatch.setattr(internal_mod, "_fetch_bridge_route_sync", _slow_bridge)
    with caplog.at_level("WARNING"):
        timed_out_docs = await internal_mod._query_bridge_internal_docs(
            ticker="META", workspace_id="ws-1", allow_network=True
        )
    assert timed_out_docs == []
    assert any("bridge parallel query timed out" in r.message for r in caplog.records)

    # 7. _check_budget_available fails open on DB error and logs at ERROR level
    monkeypatch.setattr(
        budget_svc,
        "get_budget_today",
        create_autospec(
            budget_svc.get_budget_today,
            side_effect=RuntimeError("db down during preflight budget check"),
        ),
    )
    with caplog.at_level("ERROR"):
        ok_b, reason_b = await preflight_mod._check_budget_available(user_id="u-1")
    assert ok_b is True and reason_b is None
    assert any(
        r.levelname == "ERROR" and "failing open at preflight" in r.message
        for r in caplog.records
    )

    # 4. preflight.py returns skip with reason="claim_failed" when claim_signals_woken raises
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "user_id": "u-1",
            },
        ),
    )
    monkeypatch.setattr(
        preflight_mod,
        "_check_budget_available",
        create_autospec(preflight_mod._check_budget_available, return_value=(True, None)),
    )
    monkeypatch.setattr(
        watch_db,
        "get_watch_state",
        create_autospec(watch_db.get_watch_state, return_value={}),
    )
    monkeypatch.setattr(
        watch_db,
        "upsert_watch_state",
        create_autospec(watch_db.upsert_watch_state, return_value={}),
    )
    monkeypatch.setattr(
        watch_db,
        "upsert_signal",
        create_autospec(
            watch_db.upsert_signal,
            return_value=({"signal_id": "sig-claim-err"}, True),
        ),
    )
    monkeypatch.setattr(
        watch_db,
        "claim_signals_woken",
        create_autospec(
            watch_db.claim_signals_woken,
            side_effect=RuntimeError("db connection reset during claim"),
        ),
    )
    monkeypatch.setattr(
        watch_db,
        "insert_preflight_log",
        create_autospec(watch_db.insert_preflight_log, return_value=1),
    )

    pf_res = await preflight_check(
        {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
            "user_id": "u-1",
            "metadata": {"dalton_managed": True, "ticker": "META", "allow_network": False},
        },
        "33333333-3333-3333-3333-333333333333",
        agenda_doc_override={
            "next_validations": [
                {"id": "V-1", "what": "Check Q3", "due": "2020-01-01", "status": "open"}
            ]
        },
        persist=True,
    )
    assert pf_res.should_wake is False
    assert pf_res.decision == "skip"
    assert pf_res.reason == "claim_failed"

    # 5. sec.py logs warning and marks sec_probe_degraded=True when CIK resolution fails
    monkeypatch.setattr(
        sec_mod,
        "resolve_company_cik",
        create_autospec(sec_mod.resolve_company_cik, return_value=None),
    )
    with caplog.at_level("WARNING"):
        sec_sigs, sec_state = await sec_mod.probe_sec(
            ticker="UNKNOWN_TICKER_XYZ",
            watch_state={},
            allow_network=False,
        )
    assert sec_sigs == []
    assert sec_state.get("sec_probe_degraded") is True
    assert any("marking sec_probe_degraded=True" in r.message for r in caplog.records)

    # 6. schedule.py uses trading days & fixed hours (08:00 / 20:00), and make_price_triggers.py uses cooldown_seconds=86400
    wed_specs = build_earnings_window_specs(
        ticker="META",
        workspace_id="ws-meta",
        period="FY26Q3",
        report_at="2026-10-28T16:05:00-04:00",  # Wednesday
        timezone_name="America/New_York",
    )
    # 3 trading days before Wednesday Oct 28 -> Friday Oct 23 at 08:00 ET
    assert wed_specs[0]["next_run_at"].isoformat() == "2026-10-23T08:00:00-04:00"
    # 1 trading day after Wednesday Oct 28 -> Thursday Oct 29 at 20:00 ET
    assert wed_specs[2]["next_run_at"].isoformat() == "2026-10-29T20:00:00-04:00"

    script_path = (
        Path(__file__).resolve().parents[3]
        / "plugins/dalton/skills/dalton-stock-move/scripts/make_price_triggers.py"
    )
    spec = importlib.util.spec_from_file_location("make_price_triggers", script_path)
    assert spec and spec.loader
    mpt_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mpt_mod)
    pair = mpt_mod.build_trigger_pair("META")
    for obj in pair.values():
        assert obj["trigger_config"]["retrigger"]["cooldown_seconds"] == 86400


