"""Unit tests for Dalton autonomous agenda, 4 probes, LLM triage, signal lifecycle, and CIK initialization (PK.14–PK.20, G4.1–G4.5)."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.dalton.autonomy.backoff import (
    clear_probe_backoff,
    compute_failure_backoff_seconds,
    is_in_backoff,
    next_backoff_minutes,
    record_probe_backoff,
)
from src.dalton.autonomy.preflight import (
    format_wake_directive,
    is_dalton_automation,
    is_direct_wake_signal,
    preflight_check,
)
from src.dalton.autonomy.probes import COMPANY_PROBES
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.earnings import probe_earnings
from src.dalton.autonomy.probes.internal import probe_internal
from src.dalton.autonomy.probes.sec import probe_sec, resolve_company_cik
from src.dalton.autonomy.schedule import (
    build_company_watch_specs,
    build_earnings_window_specs,
    schedule_dalton_automation_helper,
    validate_automation_name,
)
from src.dalton.database import watch as watch_db
from src.dalton.tools.knowledge_tools import (
    extract_verdict_signal_refs,
    resolve_verdict_signals,
)


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

    earn_specs = build_earnings_window_specs(
        ticker="META",
        workspace_id="ws-meta",
        period="2026Q3",
        report_at=datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc),
    )
    assert [s["name"] for s in earn_specs] == [
        "dalton-meta-earn-2026q3-tm3",
        "dalton-meta-earn-2026q3-t0",
        "dalton-meta-earn-2026q3-tp1",
    ]

    # Test idempotent schedule_dalton_automation_helper (G4.5)
    from src.server.database import automation as auto_db

    store: list[dict[str, Any]] = []

    async def fake_list(user_id: str, workspace_id: str | None = None, limit: int = 200):
        return [r for r in store if r.get("workspace_id") == workspace_id], len(store)

    async def fake_create(**kwargs):
        row = {"automation_id": f"auto-{len(store) + 1}", **kwargs}
        store.append(row)
        return row

    async def fake_update(automation_id: str, user_id: str, **patch):
        for r in store:
            if r["automation_id"] == automation_id:
                r.update(patch)
                return r
        return None

    monkeypatch.setattr(auto_db, "list_automations", fake_list)
    monkeypatch.setattr(auto_db, "create_automation", fake_create)
    monkeypatch.setattr(auto_db, "update_automation", fake_update)

    res_create = await schedule_dalton_automation_helper(
        "user-1",
        "dalton-meta-watch",
        specs[0],
        workspace_id="ws-meta",
    )
    assert res_create["ok"] is True
    assert res_create["action"] == "created"

    res_update = await schedule_dalton_automation_helper(
        user_id="user-1",
        workspace_id="ws-meta",
        name="dalton-meta-watch",
        entry={**specs[0], "description": "updated description"},
    )
    assert res_update["ok"] is True
    assert res_update["action"] == "updated"
    assert len(store) == 1


@pytest.mark.asyncio
async def test_probes_detect_real_signals_and_resolve_cik():
    today = date(2026, 10, 18)
    agenda_doc = {
        "catalysts": [
            {
                "id": "C1",
                "title": "Q3 2026 财报披露",
                "expected_date": "2026-10-20",
                "status": "upcoming",
                "importance": "P0",
            }
        ],
        "open_questions": [
            {
                "id": "Q1",
                "question": "Reels 广告加载率上限",
                "status": "open",
                "priority": "P0",
                "due_date": "2026-10-18",
            }
        ],
    }
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=agenda_doc,
        today=today,
    )
    assert len(sigs) == 2
    assert {s.signal_key for s in sigs} == {"catalyst:C1:2026-10-20", "question:Q1:2026-10-18"}

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

    # Internal probe: detects new docs and populates snippet & watermark
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

    # Earnings probe T-3
    earn_sigs, _ = await probe_earnings(
        ticker="META",
        watch_state={"next_earnings_date": "2026-10-21", "next_earnings_period": "2026Q3"},
        today=today,
    )
    assert len(earn_sigs) == 1
    assert "tm3" in earn_sigs[0].signal_key


@pytest.mark.asyncio
async def test_preflight_direct_wake_vs_llm_triage_and_fallback(monkeypatch):
    """Test G4.3: 10-Q/10-K/20-F/8-K(2.02)/agenda/earnings wake directly; Form 4/internal go to assist_json(preflight.triage)."""
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
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    assert is_dalton_automation(auto) is True

    # Case 1: Routine Form 4 -> assist_json returns wake=False -> preflight skips
    triage_calls: list[dict[str, Any]] = []

    async def mock_triage_skip(**kwargs):
        triage_calls.append(kwargs)
        return {
            "ok": True,
            "data": {
                "wake": False,
                "priority": "P2",
                "reason": "routine_form4_tax_withholding",
                "relevant_pillar_or_question_id": None,
            },
        }

    monkeypatch.setattr("src.dalton.autonomy.preflight.assist_json", mock_triage_skip)

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
    assert len(triage_calls) == 1
    assert triage_calls[0]["task"] == "preflight.triage"
    assert res_form4.should_wake is False
    assert res_form4.decision == "skip"
    assert res_form4.reason == "routine_form4_tax_withholding"

    # Case 2: Internal doc where assist_json raises -> conservative fallback MUST wake!
    async def mock_triage_error(**kwargs):
        raise RuntimeError("LLM provider timeout")

    monkeypatch.setattr("src.dalton.autonomy.preflight.assist_json", mock_triage_error)

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
async def test_preflight_check_skips_when_no_signals_and_wakes_once():
    auto = {
        "automation_id": "11111111-1111-1111-1111-111111111111",
        "name": "dalton-meta-watch",
        "trigger_type": "cron",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    watch_state: dict = {}

    # 1. Empty agenda & no signals -> skip with 0 tokens
    res_skip = await preflight_check(
        auto,
        "exec-1",
        watch_state_override=watch_state,
        agenda_doc_override={"catalysts": [], "open_questions": []},
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

    called_args: list[Any] = []

    async def fake_resolve(signal_ids=None, **kwargs):
        called_args.append((signal_ids, kwargs))
        return len(signal_ids or [])

    monkeypatch.setattr(watch_db, "resolve_signals", fake_resolve)

    count = await resolve_verdict_signals(
        workspace_id="22222222-2222-2222-2222-222222222222",
        verdict={"word": "NO_CHANGE", "trigger_signal": "filing:0001045810-26-000012"},
        verdict_id="VD-20261018-01",
    )
    assert count == 1
    assert called_args[0][0] == ["filing:0001045810-26-000012"]
    assert called_args[0][1]["resolved_by"] == "VD-20261018-01"

    # Test expire_old_signals with mock cursor
    mock_cur = AsyncMock()
    mock_cur.fetchall.return_value = [
        {"signal_id": "sig-old-1", "signal_key": "filing:old", "status": "pending"}
    ]
    expired = await watch_db.expire_old_signals(mock_cur, max_age_days=7)
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
