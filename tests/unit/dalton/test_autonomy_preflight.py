"""Unit tests for Dalton autonomous agenda, zero-token preflight probes, time-based backoff, and coverage HTML renderer (PK.14–PK.20)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.dalton.autonomy.backoff import (
    clear_probe_backoff,
    is_in_backoff,
    next_backoff_minutes,
    record_probe_backoff,
)
from src.dalton.autonomy.preflight import (
    format_wake_directive,
    is_dalton_automation,
    preflight_check,
)
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.consensus import probe_consensus
from src.dalton.autonomy.probes.earnings import probe_earnings
from src.dalton.autonomy.probes.price import probe_price
from src.dalton.autonomy.probes.sec import probe_sec
from src.dalton.autonomy.schedule import (
    build_company_watch_specs,
    build_earnings_window_specs,
    validate_automation_name,
)


def test_backoff_is_purely_time_based():
    assert next_backoff_minutes(1) == 2
    assert next_backoff_minutes(2) == 10
    assert next_backoff_minutes(3) == 30
    assert next_backoff_minutes(4) == 120
    assert next_backoff_minutes(99) == 120

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


def test_automation_naming_and_templates():
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


@pytest.mark.asyncio
async def test_probes_detect_real_signals_and_deduplicate():
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

    # SEC probe: first run records watermark without flooding; second run detects new 10-Q
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

    submissions_v2 = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001326801-26-000025", "0001326801-26-000010"],
                "form": ["10-Q", "10-Q"],
                "filingDate": ["2026-10-29", "2026-07-30"],
                "primaryDocument": ["meta-20260930.htm", "meta-20260630.htm"],
            }
        }
    }
    sec_sigs_2, st2 = await probe_sec(
        ticker="META", watch_state=st, submissions_override=submissions_v2
    )
    assert len(sec_sigs_2) == 1
    assert sec_sigs_2[0].signal_key == "filing:0001326801-26-000025"
    assert st2["last_sec_accession"] == "0001326801-26-000025"

    # Price probe
    price_sigs, _ = await probe_price(
        ticker="META",
        watch_state={},
        quote_override={"pct_change_1d": -6.4, "as_of": "2026-10-18", "price": 550.0},
        today=today,
    )
    assert len(price_sigs) == 1
    assert price_sigs[0].signal_key == "1d:2026-10-18:down"

    # Consensus probe
    cons_sigs_1, cst = await probe_consensus(
        ticker="META",
        watch_state={},
        consensus_override={"fy26_eps": 25.0},
        today=today,
    )
    assert cons_sigs_1 == []
    cons_sigs_2, _ = await probe_consensus(
        ticker="META",
        watch_state=cst,
        consensus_override={"fy26_eps": 26.2},
        today=today,
    )
    assert len(cons_sigs_2) == 1
    assert "rev:fy26_eps" in cons_sigs_2[0].signal_key

    # Earnings probe T-3
    earn_sigs, _ = await probe_earnings(
        ticker="META",
        watch_state={"next_earnings_date": "2026-10-21", "next_earnings_period": "2026Q3"},
        today=today,
    )
    assert len(earn_sigs) == 1
    assert "tm3" in earn_sigs[0].signal_key


@pytest.mark.asyncio
async def test_preflight_check_skips_when_no_signals_and_wakes_once():
    auto = {
        "automation_id": "11111111-1111-1111-1111-111111111111",
        "name": "dalton-meta-watch",
        "trigger_type": "cron",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    assert is_dalton_automation(auto) is True
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
    assert "</dalton_wake>" in xml
