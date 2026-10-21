#!/usr/bin/env python3
"""G4 Real-Run Evidence Script — Autonomous Agenda, Zero-Token Preflight, Triage & Budget Gate (P0I §7 G4, P0H §3.4, L37).

Operates strictly through the live backend HTTP API (`http://127.0.0.1:8000`) using a
newly generated `test_user_id` (`realrun-g4-...`):
1. Scenario 1 (SEC 8-K Item 2.02 Direct Wake):
   - Bootstraps `dalton-meta-watch` against live SEC EDGAR (`https://data.sec.gov/submissions/CIK0001326801.json`),
     then removes one real 8-K (Item 2.02) accession from `dalton_watch_state.state.seen_accessions`
     (`synthetic`: `watch_state_unsee_filing`).
   - Triggers `dalton-meta-watch` via `POST /api/v1/automations/{id}/trigger`.
   - Verifies preflight wakes with `<dalton_wake>` (`decision='wake'`, `reason='direct_wake'`), the analyst
     executes `knowledge_append_verdict(verdict='NO_CHANGE')`, and the signal is resolved (`resolved_at IS NOT NULL`).
2. Scenario 2 (SEC Form 4 LLM Triage Skip + Live Internal Literature Check):
   - Removes one real Form 4 accession from `dalton_watch_state.state.seen_accessions`
     (`synthetic`: `watch_state_unsee_filing`).
   - Triggers `dalton-meta-watch` via `POST /api/v1/automations/{id}/trigger`.
   - Verifies preflight invokes `assist.preflight.triage` (`replayed=false` in `dalton_llm_journal`) and settles
     the execution as `status='skipped'`, `skip_reason='precheck'` (`reason='triage_filtered_all'`).
   - Checks live `researchdata` MCP (`127.0.0.1:8960`) for today's internal literature; when no new internal
     documents exist today, records the internal-literature sub-item as 🟦 without synthesizing fake documents (P0I §7 G4).
3. Scenario 3 (`dalton_agenda_note` Tool Write + Due Validation `V-201` Wake & Verdict):
   - Runs a real HTTP turn (`POST /api/v1/threads/messages`) calling `dalton_agenda_note` on `GOOGL` workspace,
     and verifies `GET /api/v1/dalton/knowledge/{ws}/agenda/versions` records a new `source='tool'` version
     with `origin='pm'`.
   - Imports due validation `V-201` (`due='2026-10-19'`, `status='pending'`) via
     `POST /api/v1/dalton/knowledge/{ws}/agenda/import` (`synthetic`: `knowledge_api_agenda`).
   - Triggers `dalton-googl-watch` via `POST /api/v1/automations/{id}/trigger`, verifying `<dalton_wake>` wakes on
     `validation:V-201`, records `THESIS_STRENGTHENED` verdict, and updates `V-201` to `status='done'`.
4. Scenario 4 (Signal Resolution & Coverage Board Open Signals = 0):
   - Queries `GET /api/v1/dalton/coverage/board` and `GET /api/v1/dalton/verdicts`, asserting both verdicts are
     persisted, all woken signals in `dalton_signals` have `resolved_at IS NOT NULL`, and `open_signals_count == 0`.
5. Scenario 5 (Daily Director Zero-Token Skip When No Actionable Work Exists):
   - Triggers `dalton-director-daily` via `POST /api/v1/automations/{id}/trigger` when 0 actionable items remain.
   - Verifies `status='skipped'`, `skip_reason='precheck'`, `reason='no_new_signals'`, and `$0.00` LLM spend.
6. Scenario 6 (Shared Daily Budget Cap Exhaustion + One-Shot Postponement):
   - Sets `daily_cap_usd` to `$0.01` (below current cash spend) via `PUT /api/v1/dalton/budget`
     (`synthetic`: `settings_api_budget`).
   - Triggers both `dalton-googl-watch` (`cron`) and `dalton-googl-earn-2q26-tm3` (`once`) via
     `POST /api/v1/automations/{id}/trigger`.
   - Verifies both settle as `status='skipped'`, `reason='budget_exhausted'`, and `dalton-googl-earn-2q26-tm3`
     remains `status='active'` with `next_run_at` postponed by `+1 day`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    HOME_WORKSPACE_NAMESPACE,
    RealRunHttpClient,
    assert_no_conflicting_dalton_automation,
    build_evidence_envelope,
    build_realrun_workspace_config,
    default_shanghai_date,
    load_repo_dotenv,
    now_utc_iso,
    preflight_realrun_script,
    resolve_deepseek_key,
    write_evidence_and_summary,
)
from scripts.dalton.realrun.check_env import (  # noqa: E402
    _read_bridge_token,
    _restore_api_keys_state,
    _restore_budget_state,
    _restore_subagent_state,
    _snapshot_api_keys_state,
    _snapshot_subagent_state,
)
from scripts.dalton.realrun.journal_dump import build_dsn_from_env  # noqa: E402


def _build_company_doc(ticker: str, name: str, cik: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticker": ticker,
        "exchange": "NASDAQ",
        "name": name,
        "coverage_tier": "core",
        "coverage_stage": "full",
        "sector": "Internet & Cloud",
        "currency": "USD",
        "fiscal_year_end": "12-31",
        "identifiers": {"cik": cik},
        "peers": ["MSFT", "AMZN"],
        "benchmarks": ["QQQ"],
        "one_liner": f"{ticker} G4 autonomy real-run verification workspace.",
        "covered_since": "2026-01-15",
        "autonomy": {"enabled": True, "max_usd_per_day": 5.0, "note": None},
    }


def _build_thesis_doc(ticker: str) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "as_of": now_iso,
        "one_liner": f"{ticker} core advertising and cloud operating leverage thesis remains intact.",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "horizon_months": 18,
            "summary": f"{ticker} core monetization and cloud margin expansion remain on track.",
            "variant_perception": "Operating margin durability exceeds consensus expectations.",
            "updated_at": now_iso,
        },
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "claim": f"{ticker} core revenue growth and operating margin trajectory.",
                "priority": "core",
                "baseline": {"text": "2Q26 SEC 10-Q baseline.", "as_of": "2026-06-30"},
                "expected_path": "Maintain double-digit revenue growth and margin expansion.",
                "thresholds": {"confirm": [], "warn": [], "break": []},
                "status": "intact",
                "provenance": "approved",
                "created_at": now_iso,
            }
        ],
        "evidence": [],
        "official": None,
    }


def _build_agenda_doc(
    *,
    next_validations: list[dict[str, Any]] | None = None,
    open_questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "as_of": now_iso,
        "open_questions": open_questions or [],
        "next_validations": next_validations or [],
        "watch": {
            "events": [],
            "indicators": [],
            "filings": {"sources": ["sec"], "forms": ["10-K", "10-Q", "8-K", "4"]},
            "internal": {"queries": []},
        },
        "wake_conditions": [
            {
                "id": "C1",
                "kind": "filing",
                "action": "核查 SEC 披露并追加五词结论",
                "enabled": True,
            },
            {
                "id": "C2",
                "kind": "due_validation",
                "action": "核查到期验证项并追加五词结论",
                "enabled": True,
            },
        ],
    }


def _create_started_workspace(client: RealRunHttpClient, name: str, description: str) -> str:
    st_ws, ws_body = client.request(
        "POST",
        "/api/v1/workspaces",
        json_body={
            "name": name,
            "description": description,
            "config": build_realrun_workspace_config("g4_autonomy.py"),
        },
    )
    if st_ws not in (200, 201) or not isinstance(ws_body, dict) or not ws_body.get("workspace_id"):
        raise RuntimeError(f"Failed to create workspace {name}: status={st_ws} body={ws_body}")
    ws_id = str(ws_body["workspace_id"])
    st_start, start_body = client.request("POST", f"/api/v1/workspaces/{ws_id}/start", json_body={})
    if st_start not in (200, 201):
        raise RuntimeError(f"Failed to start workspace {ws_id}: status={st_start} body={start_body}")
    return ws_id


def _wait_for_execution(
    client: RealRunHttpClient,
    automation_id: str,
    execution_id: str,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        st, body = client.request("GET", f"/api/v1/automations/{automation_id}/executions")
        if st == 200 and isinstance(body, dict):
            for ex in body.get("executions") or []:
                if isinstance(ex, dict) and str(ex.get("automation_execution_id")) == str(execution_id):
                    last_row = ex
                    if ex.get("status") in ("completed", "skipped", "failed", "interrupted", "limited"):
                        return ex
        time.sleep(1.5)
    raise TimeoutError(
        f"Automation execution {execution_id} did not settle within {timeout_s}s; last={last_row}"
    )


def _fetch_sec_submissions(cik10: str) -> dict[str, Any]:
    ua = os.environ.get("DALTON_SEC_USER_AGENT", "DaltonResearch research@dalton.local")
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    with httpx.Client(timeout=20.0, headers={"User-Agent": ua}) as http_client:
        resp = http_client.get(url)
        resp.raise_for_status()
        return resp.json()


def _find_filing_accession(
    sec_data: dict[str, Any],
    *,
    target_form: str,
    require_item_202: bool = False,
) -> dict[str, Any]:
    recent = ((sec_data.get("filings") or {}).get("recent")) or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items_col = recent.get("items") or []
    for idx, acc in enumerate(accessions[:100]):
        form = str(forms[idx] if idx < len(forms) else "").strip().upper()
        f_date = str(dates[idx] if idx < len(dates) else "")
        raw_items = str(items_col[idx] if idx < len(items_col) else "")
        if form != target_form.upper():
            continue
        if require_item_202 and "2.02" not in raw_items:
            continue
        return {
            "accession_number": str(acc),
            "form": form,
            "filing_date": f_date,
            "items": raw_items,
        }
    raise RuntimeError(
        f"Could not find real SEC filing form={target_form} require_item_202={require_item_202} in top 100 submissions"
    )


def _unsee_filing_in_watch_state(workspace_id: str, ticker: str, accession_to_remove: str) -> dict[str, Any]:
    with psycopg.connect(build_dsn_from_env(), row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM dalton_watch_state WHERE workspace_id = %s FOR UPDATE",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"No dalton_watch_state row found for workspace_id={workspace_id}")
                state = dict(row.get("state") or {})
                seen = list(state.get("seen_accessions") or [])
                before_len = len(seen)
                new_seen = [x for x in seen if str(x) != str(accession_to_remove)]
                cur.execute(
                    """
                    UPDATE dalton_watch_state
                    SET state = jsonb_set(state, '{seen_accessions}', %s::jsonb, true),
                        updated_at = NOW()
                    WHERE workspace_id = %s
                    """,
                    (Jsonb(new_seen), workspace_id),
                )
                assert cur.rowcount == 1, (
                    f"Expected exactly 1 row updated in dalton_watch_state for workspace_id={workspace_id}, got {cur.rowcount}"
                )
    return {
        "workspace_id": workspace_id,
        "ticker": ticker,
        "removed_accession": accession_to_remove,
        "seen_before": before_len,
        "seen_after": len(new_seen),
    }


def _query_preflight_and_signals(execution_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    with psycopg.connect(build_dsn_from_env(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT log_id, automation_id, automation_execution_id, workspace_id,
                       decision, reason, signals, checked_at
                FROM dalton_preflight_log
                WHERE automation_execution_id = %s
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (execution_id,),
            )
            pf_row = cur.fetchone()

            sig_rows: list[dict[str, Any]] = []
            if workspace_id:
                cur.execute(
                    """
                    SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                           summary, detected_at, woken_execution_id, woken_at, resolved_at, expired_at
                    FROM dalton_signals
                    WHERE workspace_id = %s
                    ORDER BY detected_at DESC
                    """,
                    (workspace_id,),
                )
                sig_rows = [dict(r) for r in (cur.fetchall() or [])]

    return json.loads(
        json.dumps(
            {
                "preflight_log": dict(pf_row) if pf_row else None,
                "signals": sig_rows,
            },
            default=str,
        )
    )


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _parse_shanghai_datetime(raw_val: Any) -> datetime | None:
    """Parse an ISO date or datetime string into an aware `Asia/Shanghai` `datetime` object."""
    text = str(raw_val or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def _signal_belongs_to_current_execution(
    sig: dict[str, Any],
    *,
    exec_id: str,
    trigger_started_shanghai: datetime,
    preflight_signal_keys: set[str],
) -> bool:
    """Return True only if `sig` was produced by the current execution `exec_id` at or after `trigger_started_shanghai`."""
    woken_id = str(sig.get("woken_execution_id") or "").strip()
    if woken_id == exec_id:
        return True
    sig_key = str(sig.get("signal_key") or "").strip()
    detected_dt = _parse_shanghai_datetime(sig.get("detected_at"))
    if detected_dt is None or detected_dt < trigger_started_shanghai:
        return False
    return bool(sig_key and sig_key in preflight_signal_keys)


def _check_live_internal_literature_today(
    client: RealRunHttpClient,
    *,
    meta_watch_id: str,
    meta_ws_id: str,
    googl_watch_id: str,
    googl_ws_id: str,
    bridge_base_url: str = "http://127.0.0.1:8960",
) -> dict[str, Any]:
    """Check today's internal literature (using `Asia/Shanghai` `datetime` objects) via authenticated MCP bridge
    and trigger internal probe when docs exist, scoping signal assertions strictly to the current execution (Review Item 9).
    """
    token = _read_bridge_token()
    if not token:
        raise RuntimeError(
            "DALTON_BRIDGE_TOKEN is missing (~/.config/dalton/researchdata-bridge.env or env); "
            "refusing to skip internal literature check silently (Review Item 9)."
        )
    mcp_url = f"{bridge_base_url.rstrip('/')}/mcp/{token}"
    now_shanghai = datetime.now(SHANGHAI_TZ)
    today_shanghai_date = now_shanghai.date()
    today_shanghai = today_shanghai_date.isoformat()
    counts_by_ticker: dict[str, int] = {"META": 0, "GOOGL": 0}

    with httpx.Client(timeout=20.0, trust_env=False) as http_client:
        for ticker in ("META", "GOOGL"):
            for route in ("sales-notes", "alphaengine", "guidepoint"):
                payload = {
                    "jsonrpc": "2.0",
                    "id": f"g4-internal-{route}-{ticker}",
                    "method": "tools/call",
                    "params": {
                        "name": "research",
                        "arguments": {
                            "route": route,
                            "action": "search",
                            "ticker": ticker,
                            "query": ticker,
                            "limit": 10,
                        },
                    },
                }
                resp = http_client.post(mcp_url, json=payload)
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"ResearchData bridge request failed for route={route} ticker={ticker}: "
                        f"HTTP {resp.status_code} {resp.text[:200]}"
                    )
                raw = resp.json()
                if not isinstance(raw, dict) or "error" in raw:
                    raise RuntimeError(
                        f"ResearchData bridge JSON-RPC error for route={route} ticker={ticker}: {raw}"
                    )
                content_list = ((raw.get("result") or {}).get("content")) or []
                for block in content_list:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    parsed = json.loads(block.get("text") or "{}")
                    if isinstance(parsed, dict):
                        candidates = (
                            parsed.get("items")
                            or parsed.get("results")
                            or parsed.get("documents")
                            or parsed.get("data")
                            or []
                        )
                        if isinstance(candidates, list):
                            for c in candidates:
                                if isinstance(c, dict):
                                    pub_dt = _parse_shanghai_datetime(
                                        c.get("published_at") or c.get("date") or c.get("created_at")
                                    )
                                    if pub_dt is not None and pub_dt.date() == today_shanghai_date:
                                        counts_by_ticker[ticker] += 1

    meta_new = counts_by_ticker["META"]
    googl_new = counts_by_ticker["GOOGL"]
    if meta_new == 0 and googl_new == 0:
        return {
            "checked_at": now_utc_iso(),
            "today_shanghai": today_shanghai,
            "meta_new_docs_count": 0,
            "googl_new_docs_count": 0,
            "has_new_docs_today": False,
            "status": "G4 场景②未完成",
            "incomplete_reason": "当日无新增内部文献（G4 场景②未完成）",
            "triggered_runs": [],
        }

    triggered_runs: list[dict[str, Any]] = []
    for ticker, new_cnt, watch_id, ws_id in (
        ("META", meta_new, meta_watch_id, meta_ws_id),
        ("GOOGL", googl_new, googl_watch_id, googl_ws_id),
    ):
        if new_cnt <= 0:
            continue
        trigger_started_shanghai = datetime.now(SHANGHAI_TZ)
        st_trig, trig_body = client.request("POST", f"/api/v1/automations/{watch_id}/trigger")
        assert st_trig == 200 and isinstance(trig_body, dict), (
            f"Internal literature trigger failed for {ticker}: {st_trig} {trig_body}"
        )
        exec_id = str(trig_body.get("execution_id") or trig_body.get("automation_execution_id"))
        exec_row = _wait_for_execution(client, watch_id, exec_id, timeout_s=180.0)
        db_state = _query_preflight_and_signals(exec_id, ws_id)
        pf_signals = ((db_state.get("preflight_log") or {}).get("signals")) or []
        preflight_signal_keys = {
            str(ps.get("signal_key") or "").strip()
            for ps in pf_signals
            if isinstance(ps, dict) and ps.get("signal_key")
        }
        internal_signals = [
            s
            for s in (db_state.get("signals") or [])
            if isinstance(s, dict)
            and (s.get("probe") == "internal" or str(s.get("signal_key") or "").startswith("internal:"))
            and _signal_belongs_to_current_execution(
                s,
                exec_id=exec_id,
                trigger_started_shanghai=trigger_started_shanghai,
                preflight_signal_keys=preflight_signal_keys,
            )
        ]
        assert len(internal_signals) >= 1, (
            f"Expected >= 1 internal literature signal produced by current execution {exec_id} "
            f"(since {trigger_started_shanghai.isoformat()}) in dalton_signals for {ticker} when {new_cnt} docs exist today, "
            f"got signals={db_state.get('signals')}"
        )
        triggered_runs.append(
            {
                "ticker": ticker,
                "execution_id": exec_id,
                "execution_status": exec_row.get("status"),
                "internal_signals_count": len(internal_signals),
                "internal_signals": internal_signals,
            }
        )

    return {
        "checked_at": now_utc_iso(),
        "today_shanghai": today_shanghai,
        "meta_new_docs_count": meta_new,
        "googl_new_docs_count": googl_new,
        "has_new_docs_today": True,
        "status": "completed",
        "triggered_runs": triggered_runs,
    }


def _run_scenario_5_director_daily_zero_token_skip(
    idle_client: RealRunHttpClient,
    *,
    idle_user_id: str,
    created_ws_ids: list[str],
    created_auto_ids: list[str],
) -> dict[str, Any]:
    """Run Scenario 5 (daily director zero-token skip on an idle day) under dedicated user
    `idle_user_id` (`realrun-idle-c`), who is used by no other family and owns only `Home`
    plus the newly created workspace in this run.
    """
    idle_ws_id = _create_started_workspace(
        idle_client,
        "G4-IDLE",
        "G4 Scenario 5 idle director daily workspace",
    )
    created_ws_ids.append(idle_ws_id)

    expected_home_ws_id = str(uuid.uuid5(HOME_WORKSPACE_NAMESPACE, idle_user_id))
    allowed_ws_ids = {expected_home_ws_id, idle_ws_id}
    unexpected_ws: list[dict[str, str]] = []
    limit = 100
    offset = 0
    while True:
        st_list, list_body = idle_client.request(
            "GET", "/api/v1/workspaces", params={"limit": limit, "offset": offset}
        )
        if st_list != 200 or not isinstance(list_body, dict):
            raise RuntimeError(
                f"GET /api/v1/workspaces failed for {idle_user_id}: status={st_list} body={list_body}"
            )
        page_items = list_body.get("workspaces") or list_body.get("items") or []
        for ws in page_items:
            if not isinstance(ws, dict):
                continue
            if str(ws.get("status") or "").lower() == "deleted":
                continue
            ws_id = str(ws.get("workspace_id") or ws.get("id") or "").strip()
            if ws_id and ws_id not in allowed_ws_ids:
                unexpected_ws.append({"workspace_id": ws_id, "name": str(ws.get("name") or "")})
        total = int(list_body.get("total") or 0)
        offset += len(page_items)
        if not page_items or (total > 0 and offset >= total):
            break

    if unexpected_ws:
        raise RuntimeError(
            f"Scenario 5 idle user {idle_user_id!r} owns unexpected non-deleted workspaces "
            f"(expected only Home {expected_home_ws_id!r} and newly created {idle_ws_id!r}): {unexpected_ws}"
        )

    assert_no_conflicting_dalton_automation(
        idle_client,
        name="dalton-director-daily",
        workspace_id=idle_ws_id,
    )
    st_dd, dd_body = idle_client.request(
        "PUT",
        "/api/v1/dalton/automations/dalton-director-daily",
        json_body={
            "workspace_id": idle_ws_id,
            "spec": {
                "description": "Dalton 研究总监每日晨会调度",
                "trigger_type": "cron",
                "cron_expression": "30 7 * * 1-5",
                "timezone": "America/New_York",
                "llm_model": "claude-sonnet-5-5-low-oauth",
                "thread_strategy": "new",
                "instruction": "请调用 dalton_coverage_board 查看覆盖池状态并简短回复。",
                "metadata": {
                    "dalton_managed": True,
                    "dalton_kind": "director_daily",
                },
            },
        },
    )
    if (
        st_dd != 200
        or not isinstance(dd_body, dict)
        or not dd_body.get("ok")
        or dd_body.get("action") != "created"
        or not dd_body.get("automation_id")
    ):
        raise RuntimeError(
            f"Schedule dalton-director-daily for {idle_user_id} must create a new automation "
            f"(action='created'), got status={st_dd} body={dd_body}"
        )
    director_daily_id = str(dd_body["automation_id"])
    created_auto_ids.append(director_daily_id)

    _, budget_before_s5 = idle_client.request("GET", "/api/v1/dalton/budget/today")
    spent_before_s5 = float((budget_before_s5 or {}).get("spent_usd") or 0.0)
    st_s5, s5_trig = idle_client.request("POST", f"/api/v1/automations/{director_daily_id}/trigger")
    assert st_s5 == 200 and isinstance(s5_trig, dict), (
        f"Scenario 5 trigger failed for {idle_user_id}: {st_s5} {s5_trig}"
    )
    s5_exec_id = str(s5_trig.get("execution_id") or s5_trig.get("automation_execution_id"))
    s5_exec = _wait_for_execution(idle_client, director_daily_id, s5_exec_id, timeout_s=60.0)
    s5_db = _query_preflight_and_signals(s5_exec_id, None)
    _, budget_after_s5 = idle_client.request("GET", "/api/v1/dalton/budget/today")
    spent_after_s5 = float((budget_after_s5 or {}).get("spent_usd") or 0.0)
    s5_cash_spend_usd = max(0.0, round(spent_after_s5 - spent_before_s5, 6))
    assert s5_cash_spend_usd == 0.0, (
        f"Expected Scenario 5 zero-token skip to spend $0.00, got ${s5_cash_spend_usd:.6f}"
    )
    return {
        "user_id": idle_user_id,
        "idle_workspace_id": idle_ws_id,
        "automation_id": director_daily_id,
        "execution_id": s5_exec_id,
        "execution_status": s5_exec.get("status"),
        "skip_reason": s5_exec.get("skip_reason"),
        "preflight_decision": (s5_db.get("preflight_log") or {}).get("decision"),
        "preflight_reason": (s5_db.get("preflight_log") or {}).get("reason"),
        "cash_spend_usd": s5_cash_spend_usd,
        "exec_row": s5_exec,
        "db_state": s5_db,
    }


def _cleanup_g4_state(
    *,
    backend_url: str,
    idle_user: str,
    created_idle_auto_ids: list[str],
    created_idle_ws_ids: list[str],
    pm_user: str,
    created_auto_ids: list[str],
    created_ws_ids: list[str],
    subagent_modified_pm: bool,
    prior_subagent_pm: dict[str, Any] | None,
    key_modified_pm: bool,
    prior_keys_pm: dict[str, Any] | None,
    budget_modified_pm: bool,
    was_configured_pm: bool,
    prior_budget_pm: dict[str, Any] | None,
) -> list[str]:
    """Run all G4 cleanup/restoration steps in independent `try` blocks and raise if any step failed.

    Deletes `idle_user` (`realrun-idle-c`) automations and workspaces first so that any failure
    in `pm_user` restoration never leaves `realrun-idle-c` resources behind.
    """
    cleanup_errors: list[str] = []

    if created_idle_auto_ids or created_idle_ws_ids:
        try:
            with RealRunHttpClient(base_url=backend_url, user_id=idle_user, timeout_s=30.0, record=False) as cleanup_idle:
                for auto_id_del in created_idle_auto_ids:
                    try:
                        st_del_ia, _ = cleanup_idle.delete(f"/api/v1/automations/{auto_id_del}")
                        if st_del_ia not in (200, 204, 404):
                            cleanup_errors.append(f"delete_idle_auto_{auto_id_del}:http_{st_del_ia}")
                    except Exception as exc:
                        cleanup_errors.append(f"delete_idle_auto_{auto_id_del}_exception:{type(exc).__name__}")
                for ws_id_del in created_idle_ws_ids:
                    try:
                        st_del_iw, _ = cleanup_idle.delete(f"/api/v1/workspaces/{ws_id_del}")
                        if st_del_iw not in (200, 204, 404):
                            cleanup_errors.append(f"delete_idle_ws_{ws_id_del}:http_{st_del_iw}")
                    except Exception as exc:
                        cleanup_errors.append(f"delete_idle_ws_{ws_id_del}_exception:{type(exc).__name__}")
        except Exception as exc:
            cleanup_errors.append(f"cleanup_idle_exception:{type(exc).__name__}")

    if created_auto_ids or created_ws_ids or subagent_modified_pm or key_modified_pm or budget_modified_pm:
        try:
            with RealRunHttpClient(base_url=backend_url, user_id=pm_user, timeout_s=30.0, record=False) as cleanup_pm:
                for auto_id_del in created_auto_ids:
                    try:
                        st_del_pa, _ = cleanup_pm.delete(f"/api/v1/automations/{auto_id_del}")
                        if st_del_pa not in (200, 204, 404):
                            cleanup_errors.append(f"delete_pm_auto_{auto_id_del}:http_{st_del_pa}")
                    except Exception as exc:
                        cleanup_errors.append(f"delete_pm_auto_{auto_id_del}_exception:{type(exc).__name__}")
                for ws_id_del in created_ws_ids:
                    try:
                        st_del_pw, _ = cleanup_pm.delete(f"/api/v1/workspaces/{ws_id_del}")
                        if st_del_pw not in (200, 204, 404):
                            cleanup_errors.append(f"delete_pm_ws_{ws_id_del}:http_{st_del_pw}")
                    except Exception as exc:
                        cleanup_errors.append(f"delete_pm_ws_{ws_id_del}_exception:{type(exc).__name__}")
                if subagent_modified_pm:
                    try:
                        err_sa_pm = _restore_subagent_state(
                            cleanup_pm,
                            prior_state=prior_subagent_pm,
                        )
                        if err_sa_pm is not None:
                            cleanup_errors.append(f"restore_subagent_pm:{err_sa_pm}")
                    except Exception as exc:
                        cleanup_errors.append(f"restore_subagent_pm_exception:{type(exc).__name__}")
                if key_modified_pm:
                    try:
                        err_key_pm = _restore_api_keys_state(
                            cleanup_pm,
                            prior_state=prior_keys_pm,
                        )
                        if err_key_pm is not None:
                            cleanup_errors.append(f"restore_key_pm:{err_key_pm}")
                    except Exception as exc:
                        cleanup_errors.append(f"restore_key_pm_exception:{type(exc).__name__}")
                if budget_modified_pm:
                    try:
                        err_pm = _restore_budget_state(
                            cleanup_pm,
                            user_id=pm_user,
                            was_configured=was_configured_pm,
                            prior_budget=prior_budget_pm,
                        )
                        if err_pm is not None:
                            cleanup_errors.append(f"restore_budget_pm:{err_pm}")
                    except Exception as exc:
                        cleanup_errors.append(f"restore_budget_pm_exception:{type(exc).__name__}")
        except Exception as exc:
            cleanup_errors.append(f"cleanup_pm_exception:{type(exc).__name__}")

    if cleanup_errors:
        raise RuntimeError(f"G4 cleanup/restoration failed: {cleanup_errors}")

    return cleanup_errors


def _ensure_deepseek_byok_for_g4(
    client: RealRunHttpClient,
    prior_keys_state: dict[str, Any] | None,
) -> bool:
    """Write a temporary DeepSeek BYOK key only when the user has none configured (D6).

    Returns True when a temporary key was written (`PUT /api/v1/users/me/api-keys`),
    or False when an existing user key was preserved without modification.
    """
    if bool((prior_keys_state or {}).get("had_user_deepseek_key")):
        return False
    ds_key = resolve_deepseek_key()
    st_byok, _ = client.request(
        "PUT",
        "/api/v1/users/me/api-keys",
        json_body={"byok_enabled": True, "api_keys": {"deepseek": ds_key}},
    )
    assert st_byok == 200, f"PUT /api/v1/users/me/api-keys failed: status={st_byok}"
    return True


def main() -> int:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="G4 Real-Run Evidence Generator (P0J §5 G4)")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default=default_shanghai_date())
    parser.add_argument("--pm-user", default="realrun-pm-a")
    parser.add_argument("--other-user", default="realrun-user-b")
    parser.add_argument("--idle-user", default="realrun-idle-c")
    parser.add_argument("--record", action="store_true", help="Write evidence JSON and summary under var/dalton/")
    args = parser.parse_args()

    preflight_realrun_script("G4", record=args.record, backend=args.backend)

    started_at = now_utc_iso()
    pm_user = args.pm_user
    other_user = args.other_user
    idle_user = args.idle_user
    test_user_id = f"{pm_user},{other_user},{idle_user}"
    synthetic: list[dict[str, Any]] = []

    prior_budget_pm: dict[str, Any] = {}
    was_configured_pm = False
    budget_modified_pm = False
    prior_keys_pm: dict[str, Any] | None = None
    key_modified_pm = False
    prior_subagent_pm: dict[str, Any] | None = None
    subagent_modified_pm = False
    created_ws_ids: list[str] = []
    created_auto_ids: list[str] = []
    created_idle_ws_ids: list[str] = []
    created_idle_auto_ids: list[str] = []

    try:
        with (
            RealRunHttpClient(base_url=args.backend, user_id=pm_user, timeout_s=180.0) as client,
            RealRunHttpClient(base_url=args.backend, user_id=other_user, timeout_s=60.0) as user_b_client,
            RealRunHttpClient(base_url=args.backend, user_id=idle_user, timeout_s=60.0) as idle_client,
        ):
            client.ensure_user()
            user_b_client.ensure_user()
            idle_client.ensure_user()

            st_prior_b, body_prior_b = client.get("/api/v1/dalton/budget")
            if st_prior_b == 200 and isinstance(body_prior_b, dict):
                prior_budget_pm = body_prior_b
                was_configured_pm = bool(body_prior_b.get("configured"))
            prior_keys_pm = _snapshot_api_keys_state(client)
            prior_subagent_pm = _snapshot_subagent_state(client, "general-purpose")

            st_prior_bt, body_prior_bt = client.get("/api/v1/dalton/budget/today")
            baseline_spent_pm = (
                float(body_prior_bt.get("spent_usd") or 0.0)
                if st_prior_bt == 200 and isinstance(body_prior_bt, dict)
                else 0.0
            )
            initial_cap_usd = round(max(15.0, baseline_spent_pm + 15.0), 2)

            # Configure initial daily budget and BYOK DeepSeek key
            st_b, body_b = client.request(
                "PUT",
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": initial_cap_usd, "default_task_cap_usd": 5.0},
            )
            assert st_b == 200, f"PUT /api/v1/dalton/budget failed: {st_b} {body_b}"
            budget_modified_pm = True
            synthetic.append(
                {
                    "kind": "settings_api_budget",
                    "entry_point": "PUT /api/v1/dalton/budget",
                    "description": (
                        f"初始化测试用户 {pm_user} 相对日预算上限 ${initial_cap_usd:.2f} "
                        f"（max($15.00, 当日已有现金花费 ${baseline_spent_pm:.4f} + $15.00)），"
                        "在场景 6 下调至 $0.01 触发共享预算熔断，验证完成后恢复原预算、API Key 与子智能体状态。"
                    ),
                }
            )

            key_modified_pm = _ensure_deepseek_byok_for_g4(client, prior_keys_pm)

            # Configure `general-purpose` subagent to use cash model `deepseek-flash` so real cash spend can accumulate >= $0.01
            sa_version = str((prior_subagent_pm or {}).get("version") or "")
            st_sa_put, sa_put_resp = client.request(
                "PUT",
                "/api/v1/subagents/general-purpose",
                json_body={"version": sa_version, "fields": {"model": "deepseek-flash"}},
            )
            assert st_sa_put == 200, f"PUT /api/v1/subagents/general-purpose failed: {st_sa_put} {sa_put_resp}"
            subagent_modified_pm = True

            # Create & start META and GOOGL workspaces
            print(f"[G4] Creating META and GOOGL workspaces for user={test_user_id} ...", flush=True)
            meta_ws_id = _create_started_workspace(client, "META", "META G4 Real-Run Workspace")
            created_ws_ids.append(meta_ws_id)
            googl_ws_id = _create_started_workspace(client, "GOOGL", "GOOGL G4 Real-Run Workspace")
            created_ws_ids.append(googl_ws_id)

            # Import initial company, thesis, and empty agenda for META; import company and thesis for GOOGL
            # (GOOGL agenda v1 with V-201 is imported in Scenario 3 before dalton_agenda_note writes v2)
            for ws_id, ticker, name, cik, include_empty_agenda in [
                (meta_ws_id, "META", "Meta Platforms, Inc.", "0001326801", True),
                (googl_ws_id, "GOOGL", "Alphabet Inc.", "0001652044", False),
            ]:
                docs_to_import: list[tuple[str, dict[str, Any]]] = [
                    ("company", _build_company_doc(ticker, name, cik)),
                    ("thesis", _build_thesis_doc(ticker)),
                ]
                if include_empty_agenda:
                    docs_to_import.append(("agenda", _build_agenda_doc()))
                for doc_key, doc_payload in docs_to_import:
                    st_imp, imp_body = client.request(
                        "POST",
                        f"/api/v1/dalton/knowledge/{ws_id}/{doc_key}/import",
                        json_body={"content": doc_payload, "reason": f"G4 initial {doc_key} bootstrap"},
                    )
                    assert st_imp == 200 and imp_body.get("ok"), (
                        f"Import {doc_key} failed for {ticker}: {st_imp} {imp_body}"
                    )

            # Fetch live SEC submissions for META to identify real 8-K (Item 2.02) and Form 4 accessions
            meta_sec_data = _fetch_sec_submissions("0001326801")
            real_8k_202 = _find_filing_accession(meta_sec_data, target_form="8-K", require_item_202=True)
            real_form4 = _find_filing_accession(meta_sec_data, target_form="4", require_item_202=False)
            acc_8k = real_8k_202["accession_number"]
            acc_form4 = real_form4["accession_number"]

            # Schedule dalton-meta-watch, dalton-googl-watch, and dalton-googl-earn-2q26-tm3
            meta_watch_instruction = (
                f"请阅读本轮 `<dalton_wake>` 中的触发信号 `filing:{acc_8k}`，立即调用一次 "
                "`knowledge_append_verdict` 工具。请将以下 JSON 对象直接作为 `verdict` 参数的值传入（注意：`verdict` 参数本身就是该字典，不要在字典外层再套一层 `verdict` 键）：\n"
                "```json\n"
                "{\n"
                f'  "trigger": {{"kind": "filing", "ref": "filing:{acc_8k}"}},\n'
                f'  "event": "META 披露 8-K (Item 2.02, accession={acc_8k}) 季度业绩公告",\n'
                '  "verdict": "NO_CHANGE",\n'
                '  "rationale": "核查 SEC 8-K Item 2.02 披露内容，广告收入与 CapEx 指引符合预期，维持现有核心判断。",\n'
                '  "next_validation": {"what": "跟踪下季广告展现量与单价同比增速", "due": "2026-12-31"},\n'
                '  "progress": "some"\n'
                "}\n"
                "```\n"
                "调用成功后立即用一句话回复完成，不要调用任何其他工具。"
            )
            assert_no_conflicting_dalton_automation(
                client,
                name="dalton-meta-watch",
                workspace_id=meta_ws_id,
            )
            st_mw, mw_body = client.request(
                "PUT",
                "/api/v1/dalton/automations/dalton-meta-watch",
                json_body={
                    "workspace_id": meta_ws_id,
                    "spec": {
                        "description": "META 日常信号巡检",
                        "trigger_type": "cron",
                        "cron_expression": "*/30 6-20 * * 1-5",
                        "timezone": "America/New_York",
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "thread_strategy": "new",
                        "instruction": meta_watch_instruction,
                        "metadata": {
                            "dalton_managed": True,
                            "dalton_kind": "company_watch",
                            "ticker": "META",
                            "tier": "Core",
                        },
                    },
                },
            )
            if (
                st_mw != 200
                or not isinstance(mw_body, dict)
                or not mw_body.get("ok")
                or mw_body.get("action") != "created"
                or not mw_body.get("automation_id")
            ):
                raise RuntimeError(
                    f"Schedule dalton-meta-watch must create a new automation (action='created'), got {st_mw} {mw_body}"
                )
            meta_watch_id = str(mw_body["automation_id"])
            created_auto_ids.append(meta_watch_id)

            googl_watch_instruction = (
                "请阅读本轮 `<dalton_wake>` 中的到期验证信号 `validation:V-201` 与议程问题 `pm_question:Q-001`，立即调用一次 "
                "`knowledge_append_verdict` 工具：\n"
                "1. `verdict` 参数的值（直接传入以下字典，不要在外层再包一层 `verdict` 键）：\n"
                "```json\n"
                "{\n"
                '  "trigger": {"kind": "due_validation", "ref": "V-201"},\n'
                '  "event": "GOOGL 2Q26 Google Cloud 营业利润率达 11.3%，兑现 V-201 到期验证项并解答 Q-001",\n'
                '  "verdict": "THESIS_STRENGTHENED",\n'
                '  "rationale": "2Q26 Google Cloud 营业利润率 11.3% 突破 11.0% 阈值，经营杠杆持续释放。",\n'
                '  "next_validation": {"what": "跟踪 3Q26 Google Cloud 营业利润率维持 11% 以上", "due": "2026-12-31", "validation_id": "V-201"},\n'
                '  "progress": "resolved",\n'
                '  "question_ids": ["Q-001"]\n'
                "}\n"
                "```\n"
                "2. 同时传入 `agenda_ops` 参数以将 `Q-001` 标记为 `resolved`：\n"
                "```json\n"
                "[\n"
                '  {"op": "set", "path": "/open_questions/Q-001/status", "value": "resolved"},\n'
                '  {"op": "set", "path": "/open_questions/Q-001/resolution", "value": "2Q26 Google Cloud 营业利润率 11.3% 已核实兑现"}\n'
                "]\n"
                "```\n"
                "调用成功后立即用一句话回复完成，不要调用任何其他工具。"
            )
            assert_no_conflicting_dalton_automation(
                client,
                name="dalton-googl-watch",
                workspace_id=googl_ws_id,
            )
            st_gw, gw_body = client.request(
                "PUT",
                "/api/v1/dalton/automations/dalton-googl-watch",
                json_body={
                    "workspace_id": googl_ws_id,
                    "spec": {
                        "description": "GOOGL 日常信号巡检",
                        "trigger_type": "cron",
                        "cron_expression": "*/30 6-20 * * 1-5",
                        "timezone": "America/New_York",
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "thread_strategy": "new",
                        "instruction": googl_watch_instruction,
                        "metadata": {
                            "dalton_managed": True,
                            "dalton_kind": "company_watch",
                            "ticker": "GOOGL",
                            "tier": "Core",
                        },
                    },
                },
            )
            if (
                st_gw != 200
                or not isinstance(gw_body, dict)
                or not gw_body.get("ok")
                or gw_body.get("action") != "created"
                or not gw_body.get("automation_id")
            ):
                raise RuntimeError(
                    f"Schedule dalton-googl-watch must create a new automation (action='created'), got {st_gw} {gw_body}"
                )
            googl_watch_id = str(gw_body["automation_id"])
            created_auto_ids.append(googl_watch_id)

            initial_tm3_dt = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            initial_tm3_run_at = initial_tm3_dt.isoformat().replace("+00:00", "Z")
            expected_tm3_postponed_date = (initial_tm3_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            assert_no_conflicting_dalton_automation(
                client,
                name="dalton-googl-earn-2q26-tm3",
                workspace_id=googl_ws_id,
            )
            st_tm3, tm3_body = client.request(
                "PUT",
                "/api/v1/dalton/automations/dalton-googl-earn-2q26-tm3",
                json_body={
                    "workspace_id": googl_ws_id,
                    "spec": {
                        "description": "GOOGL 2Q26 财报前瞻（T-3）",
                        "trigger_type": "once",
                        "next_run_at": initial_tm3_run_at,
                        "timezone": "America/New_York",
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "thread_strategy": "new",
                        "instruction": "请生成 GOOGL 2Q26 财报前瞻草稿。",
                        "metadata": {
                            "dalton_managed": True,
                            "dalton_kind": "earnings_tm3",
                            "ticker": "GOOGL",
                            "period": "2Q26",
                        },
                    },
                },
            )
            if (
                st_tm3 != 200
                or not isinstance(tm3_body, dict)
                or not tm3_body.get("ok")
                or tm3_body.get("action") != "created"
                or not tm3_body.get("automation_id")
            ):
                raise RuntimeError(
                    f"Schedule dalton-googl-earn-2q26-tm3 must create a new automation (action='created'), got {st_tm3} {tm3_body}"
                )
            tm3_id = str(tm3_body["automation_id"])
            created_auto_ids.append(tm3_id)

            # Step 0: Bootstrap SEC watermarks for META and GOOGL via initial trigger
            print("[G4] Step 0: Bootstrapping SEC watermarks for META and GOOGL ...", flush=True)
            for auto_id in (meta_watch_id, googl_watch_id):
                st_t, t_body = client.request("POST", f"/api/v1/automations/{auto_id}/trigger")
                assert st_t == 200, f"Bootstrap trigger failed: {st_t} {t_body}"
                exec_id_0 = str(t_body.get("execution_id") or t_body.get("automation_execution_id"))
                _wait_for_execution(client, auto_id, exec_id_0, timeout_s=60.0)

            # Scenario 1: Unsee real SEC 8-K (Item 2.02) and trigger dalton-meta-watch -> Direct Wake & Verdict
            print(f"[G4] Scenario 1: Unseeing real META 8-K (Item 2.02) {acc_8k} and triggering dalton-meta-watch ...", flush=True)
            unsee_8k_info = _unsee_filing_in_watch_state(meta_ws_id, "META", acc_8k)
            synthetic.append(
                {
                    "kind": "watch_state_unsee_filing",
                    "entry_point": "dalton_watch_state.state.seen_accessions",
                    "description": f"从 META seen_accessions 中移除真实存在的 SEC 8-K (Item 2.02) 申报号 {acc_8k}，触发直接唤醒。",
                }
            )
            st_s1, s1_trig = client.request("POST", f"/api/v1/automations/{meta_watch_id}/trigger")
            assert st_s1 == 200, f"Scenario 1 trigger failed: {st_s1} {s1_trig}"
            s1_exec_id = str(s1_trig.get("execution_id") or s1_trig.get("automation_execution_id"))
            s1_exec = _wait_for_execution(client, meta_watch_id, s1_exec_id, timeout_s=180.0)
            s1_db = _query_preflight_and_signals(s1_exec_id, meta_ws_id)
            _, s1_verdicts_resp = client.request(
                "GET", "/api/v1/dalton/verdicts", params={"workspace_id": meta_ws_id}
            )
            s1_verdicts = (s1_verdicts_resp or {}).get("items") or []
            print(
                f"[G4] Scenario 1 done: exec_status={s1_exec.get('status')} "
                f"preflight={((s1_db.get('preflight_log') or {}).get('reason'))} verdicts={len(s1_verdicts)}",
                flush=True,
            )

            # Scenario 2: Unsee real SEC Form 4 and trigger dalton-meta-watch -> LLM Triage (assist.preflight.triage)
            print(f"[G4] Scenario 2: Unseeing real META Form 4 {acc_form4} and triggering dalton-meta-watch ...", flush=True)
            unsee_form4_info = _unsee_filing_in_watch_state(meta_ws_id, "META", acc_form4)
            synthetic.append(
                {
                    "kind": "watch_state_unsee_filing",
                    "entry_point": "dalton_watch_state.state.seen_accessions",
                    "description": f"从 META seen_accessions 中移除真实存在的 SEC Form 4 申报号 {acc_form4}，触发 preflight.triage 分诊。",
                }
            )
            st_s2, s2_trig = client.request("POST", f"/api/v1/automations/{meta_watch_id}/trigger")
            assert st_s2 == 200, f"Scenario 2 trigger failed: {st_s2} {s2_trig}"
            s2_exec_id = str(s2_trig.get("execution_id") or s2_trig.get("automation_execution_id"))
            s2_exec = _wait_for_execution(client, meta_watch_id, s2_exec_id, timeout_s=120.0)
            s2_db = _query_preflight_and_signals(s2_exec_id, meta_ws_id)
            internal_lit_check = _check_live_internal_literature_today(
                client,
                meta_watch_id=meta_watch_id,
                meta_ws_id=meta_ws_id,
                googl_watch_id=googl_watch_id,
                googl_ws_id=googl_ws_id,
            )
            print(
                f"[G4] Scenario 2 done: exec_status={s2_exec.get('status')} "
                f"preflight={((s2_db.get('preflight_log') or {}).get('reason'))} "
                f"internal_lit_has_new_docs={internal_lit_check['has_new_docs_today']}",
                flush=True,
            )

            # Scenario 3: First import GOOGL agenda v1 with due validation V-201, then invoke dalton_agenda_note via HTTP chat turn to create v2 (source='tool')
            print("[G4] Scenario 3a: Importing GOOGL agenda v1 (with V-201) and invoking dalton_agenda_note via POST /api/v1/threads/messages ...", flush=True)
            now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            due_agenda_payload = _build_agenda_doc(
                open_questions=[],
                next_validations=[
                    {
                        "id": "V-201",
                        "what": "验证 2Q26 Google Cloud 营业利润率突破 11.0%",
                        "due": "2026-10-19",
                        "note": "G4 到期验证项触发测试",
                        "source": "GOOGL 2Q26 10-Q",
                        "expected": "Google Cloud 营业利润率 >= 11.0%",
                        "linked": {"pillars": ["P1"]},
                        "status": "pending",
                    }
                ],
            )
            due_agenda_payload["as_of"] = now_iso
            st_imp_ag, imp_ag_body = client.request(
                "POST",
                f"/api/v1/dalton/knowledge/{googl_ws_id}/agenda/import",
                json_body={
                    "content": due_agenda_payload,
                    "reason": "G4 inject due validation V-201 (due=2026-10-19)",
                },
            )
            assert st_imp_ag == 200 and imp_ag_body.get("ok"), (
                f"GOOGL agenda import failed: {st_imp_ag} {imp_ag_body}"
            )
            synthetic.append(
                {
                    "kind": "knowledge_api_agenda",
                    "entry_point": f"POST /api/v1/dalton/knowledge/{googl_ws_id}/agenda/import",
                    "description": "向 GOOGL 导入包含到期验证项 V-201 (due=2026-10-19, status=pending) 的初始 agenda v1。",
                }
            )

            st_home, home_body = client.request("POST", "/api/v1/workspaces/home", json_body={})
            assert st_home in (200, 201) and isinstance(home_body, dict) and home_body.get("workspace_id"), (
                f"POST /api/v1/workspaces/home failed: {st_home} {home_body}"
            )
            home_ws_id = str(home_body["workspace_id"])
            client.request("POST", f"/api/v1/workspaces/{home_ws_id}/start", json_body={})

            agenda_note_prompt = (
                f"请立即调用一次 `dalton_agenda_note` 工具，向 GOOGL 工作区（`workspace_id=\"{googl_ws_id}\"`）写入一条议程问题：\n"
                f"- `workspace_id`: `{googl_ws_id}`\n"
                "- `ticker`: `GOOGL`\n"
                "- `question`: `核查 2Q26 Google Cloud 营业利润率是否突破 11% 并持续改善`\n"
                "- `value`: `high`\n"
                "- `urgency`: `this_week`\n"
                "- `why`: `G4 真实运行验证 dalton_agenda_note 工具写入`\n"
                "调用成功后简短回复一行确认，不要调用其他工具。"
            )
            st_an, an_events, an_err = client.stream_sse(
                "POST",
                "/api/v1/threads/messages",
                json_body={
                    "workspace_id": home_ws_id,
                    "llm_model": "claude-sonnet-5-5-low-oauth",
                    "messages": [{"role": "user", "content": agenda_note_prompt}],
                },
                max_events=1000,
            )
            assert st_an == 200 and an_err is None, f"dalton_agenda_note turn failed: {st_an} {an_err}"
            an_thread_id = None
            an_run_id = None
            for ev in an_events:
                if ev.get("event") in ("metadata", "run_end") and isinstance(ev.get("data"), dict):
                    an_thread_id = ev["data"].get("thread_id") or an_thread_id
                    an_run_id = ev["data"].get("run_id") or an_run_id
            agenda_note_turn = {"status_code": st_an, "thread_id": an_thread_id, "response_id": an_run_id}
            st_ag_v, ag_versions_body = client.request(
                "GET", f"/api/v1/dalton/knowledge/{googl_ws_id}/agenda/versions"
            )
            ag_versions = (ag_versions_body or {}).get("items") or []
            latest_ag_ver = ag_versions[0] if ag_versions else {}
            st_ag_doc, ag_doc_body = client.request(
                "GET",
                f"/api/v1/dalton/knowledge/{googl_ws_id}/agenda/versions/{latest_ag_ver.get('version_no', 2)}",
            )
            ag_open_questions = ((ag_doc_body or {}).get("content") or {}).get("open_questions") or []
            print(
                f"[G4] Scenario 3a done: agenda version_no={latest_ag_ver.get('version_no')} "
                f"source={latest_ag_ver.get('source')} open_questions={len(ag_open_questions)}",
                flush=True,
            )

            # Scenario 3b: Trigger dalton-googl-watch -> wakes on V-201 & Q-001, records verdict & resolves both
            print("[G4] Scenario 3b: Triggering dalton-googl-watch to wake on V-201 & Q-001 ...", flush=True)
            st_s3, s3_trig = client.request("POST", f"/api/v1/automations/{googl_watch_id}/trigger")
            assert st_s3 == 200, f"Scenario 3 trigger failed: {st_s3} {s3_trig}"
            s3_exec_id = str(s3_trig.get("execution_id") or s3_trig.get("automation_execution_id"))
            s3_exec = _wait_for_execution(client, googl_watch_id, s3_exec_id, timeout_s=180.0)
            s3_db = _query_preflight_and_signals(s3_exec_id, googl_ws_id)
            _, s3_verdicts_resp = client.request(
                "GET", "/api/v1/dalton/verdicts", params={"workspace_id": googl_ws_id}
            )
            s3_verdicts = (s3_verdicts_resp or {}).get("items") or []
            print(
                f"[G4] Scenario 3b done: exec_status={s3_exec.get('status')} "
                f"preflight={((s3_db.get('preflight_log') or {}).get('reason'))} verdicts={len(s3_verdicts)}",
                flush=True,
            )

            # Scenario 4: Check Coverage Board & Signal Resolution + Two-User Isolation
            print("[G4] Scenario 4: Checking GET /api/v1/dalton/coverage/board & 2-user isolation ...", flush=True)
            st_board, board_body = client.request("GET", "/api/v1/dalton/coverage/board")
            assert st_board == 200 and isinstance(board_body, dict), f"Coverage board failed: {st_board}"
            board_companies = board_body.get("companies") or []
            open_signals_by_ticker = {
                str(c.get("ticker")): int(c.get("open_signals_count") or 0)
                for c in board_companies
                if isinstance(c, dict)
            }
            st_b_board, b_board_body = user_b_client.get("/api/v1/dalton/coverage/board")
            assert st_b_board == 200 and isinstance(b_board_body, dict)
            b_ws_ids = {
                str(c.get("workspace_id"))
                for c in (b_board_body.get("companies") or [])
                if isinstance(c, dict) and c.get("workspace_id")
            }
            assert meta_ws_id not in b_ws_ids and googl_ws_id not in b_ws_ids, (
                f"Two-user isolation failed on coverage/board: {other_user} saw {pm_user} workspaces"
            )
            st_b_verdicts, b_verdicts_body = user_b_client.get("/api/v1/dalton/verdicts")
            assert st_b_verdicts == 200 and isinstance(b_verdicts_body, dict)
            b_verdict_ws = {
                str(v.get("workspace_id"))
                for v in (b_verdicts_body.get("items") or [])
                if isinstance(v, dict)
            }
            assert meta_ws_id not in b_verdict_ws and googl_ws_id not in b_verdict_ws
            st_b_pr, b_pr_body = user_b_client.get("/api/v1/dalton/publish-requests")
            assert st_b_pr == 200 and isinstance(b_pr_body, dict)
            b_pr_ws = {
                str(p.get("workspace_id"))
                for p in (b_pr_body.get("items") or [])
                if isinstance(p, dict)
            }
            assert meta_ws_id not in b_pr_ws and googl_ws_id not in b_pr_ws

            two_user_isolation = {
                "pm_user": pm_user,
                "other_user": other_user,
                "other_user_visible_pm_workspaces_on_board": len(b_ws_ids & {meta_ws_id, googl_ws_id}),
                "other_user_visible_pm_verdicts": len(b_verdict_ws & {meta_ws_id, googl_ws_id}),
                "other_user_visible_pm_publish_requests": len(b_pr_ws & {meta_ws_id, googl_ws_id}),
            }
            print(f"[G4] Scenario 4 done: open_signals_by_ticker={open_signals_by_ticker}", flush=True)

            # Scenario 5: Trigger dalton-director-daily when no actionable items remain -> Zero-Token Skip
            # Executed under dedicated `idle_user` (`realrun-idle-c`), who is used by no other family.
            print(
                f"[G4] Scenario 5: Triggering dalton-director-daily under {idle_user} when 0 actionable items remain ...",
                flush=True,
            )
            s5_res = _run_scenario_5_director_daily_zero_token_skip(
                idle_client,
                idle_user_id=idle_user,
                created_ws_ids=created_idle_ws_ids,
                created_auto_ids=created_idle_auto_ids,
            )
            s5_exec_id = str(s5_res["execution_id"])
            s5_exec = s5_res["exec_row"]
            s5_db = s5_res["db_state"]
            s5_cash_spend_usd = float(s5_res["cash_spend_usd"])
            s5_idle_ws_id = str(s5_res["idle_workspace_id"])
            print(
                f"[G4] Scenario 5 done: user={idle_user} exec_status={s5_exec.get('status')} "
                f"preflight={((s5_db.get('preflight_log') or {}).get('reason'))} cash_spend=${s5_cash_spend_usd:.2f}",
                flush=True,
            )

            # Scenario 6: Accumulate >= $0.01 real cash spend if needed, lower daily_cap_usd to $0.01, and trigger watch + one-shot tm3
            print("[G4] Scenario 6: Accumulating >= $0.01 cash spend and lowering daily_cap_usd to $0.01 ...", flush=True)
            spend_deadline = time.monotonic() + 300.0
            turn_idx = 0
            while time.monotonic() < spend_deadline:
                _, cur_today = client.request("GET", "/api/v1/dalton/budget/today")
                cur_spent = float((cur_today or {}).get("spent_usd") or 0.0)
                if cur_spent >= 0.01:
                    break
                turn_idx += 1
                sub_prompt = (
                    f"请在本轮响应中调用 1 次 `Task` 工具（使用 `subagent_type='general-purpose'`，命名为 `G4-{turn_idx}-A`）。\n"
                    "在 `Task` 的 `prompt` 中，要求该子智能体严格按顺序分 2 轮调用 `Bash` 工具（每轮等待上一轮返回后再发起下一轮，每次输出 150 行随机数据以保留在上下文内）：\n"
                    "第 1 轮在子智能体中执行：`python3 -c \"import uuid; print('\\n'.join(f'R1_{i:03d}_{uuid.uuid4().hex}_{uuid.uuid4().hex}' for i in range(150)))\"`；\n"
                    "第 2 轮在子智能体中执行：`python3 -c \"import uuid; print('\\n'.join(f'R2_{i:03d}_{uuid.uuid4().hex}_{uuid.uuid4().hex}' for i in range(150)))\"`；\n"
                    "第 2 轮返回后，子智能体撰写 300 字详细财务敏感性结论并返回。"
                )
                st_sub, _, sub_err = client.stream_sse(
                    "POST",
                    "/api/v1/threads/messages",
                    json_body={
                        "workspace_id": googl_ws_id,
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "messages": [{"role": "user", "content": sub_prompt}],
                    },
                    max_events=2500,
                )
                assert st_sub == 200 and sub_err is None, f"Cash spend turn {turn_idx} failed: {st_sub} {sub_err}"
                settle_deadline = time.monotonic() + 60.0
                settled = False
                while time.monotonic() < settle_deadline:
                    _, poll_today = client.request("GET", "/api/v1/dalton/budget/today")
                    if float((poll_today or {}).get("open_reserved_usd") or 0.0) == 0.0:
                        settled = True
                        break
                    time.sleep(2.0)
                if not settled:
                    raise TimeoutError(
                        f"Timed out after 60s waiting for open_reserved_usd == 0.0 after turn {turn_idx}"
                    )

            _, budget_before_s6 = client.request("GET", "/api/v1/dalton/budget/today")
            spent_before_s6 = float((budget_before_s6 or {}).get("spent_usd") or 0.0)
            if spent_before_s6 < 0.01:
                raise TimeoutError(
                    f"Failed to accumulate >= $0.01 cash spend before Scenario 6: spent_usd={spent_before_s6}"
                )
            print(f"[G4] Scenario 6 cash spend before lowering cap: ${spent_before_s6:.6f}", flush=True)
            st_cap_low, cap_low_body = client.request(
                "PUT",
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": 0.01, "default_task_cap_usd": 0.01},
            )
            assert st_cap_low == 200, f"Failed to lower daily_cap_usd: {st_cap_low} {cap_low_body}"

            st_s6_w, s6_w_trig = client.request("POST", f"/api/v1/automations/{googl_watch_id}/trigger")
            assert st_s6_w == 200, f"Scenario 6 watch trigger failed: {st_s6_w} {s6_w_trig}"
            s6_w_exec_id = str(s6_w_trig.get("execution_id") or s6_w_trig.get("automation_execution_id"))
            s6_w_exec = _wait_for_execution(client, googl_watch_id, s6_w_exec_id, timeout_s=60.0)
            s6_w_db = _query_preflight_and_signals(s6_w_exec_id, googl_ws_id)

            st_s6_tm3, s6_tm3_trig = client.request("POST", f"/api/v1/automations/{tm3_id}/trigger")
            assert st_s6_tm3 == 200, f"Scenario 6 tm3 trigger failed: {st_s6_tm3} {s6_tm3_trig}"
            s6_tm3_exec_id = str(s6_tm3_trig.get("execution_id") or s6_tm3_trig.get("automation_execution_id"))
            s6_tm3_exec = _wait_for_execution(client, tm3_id, s6_tm3_exec_id, timeout_s=60.0)
            s6_tm3_db = _query_preflight_and_signals(s6_tm3_exec_id, googl_ws_id)

            st_tm3_after, tm3_after_body = client.request("GET", f"/api/v1/automations/{tm3_id}")
            assert st_tm3_after == 200 and isinstance(tm3_after_body, dict), (
                f"GET tm3 automation failed: {st_tm3_after} {tm3_after_body}"
            )

            # Restore daily_cap_usd after Scenario 6 verification (finally block also restores exact prior state)
            client.request(
                "PUT",
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": initial_cap_usd, "default_task_cap_usd": 5.0},
            )
            print(
                f"[G4] Scenario 6 done: watch_status={s6_w_exec.get('status')} "
                f"tm3_exec_status={s6_tm3_exec.get('status')} "
                f"tm3_automation_status={tm3_after_body.get('status')} "
                f"tm3_next_run_at={tm3_after_body.get('next_run_at')}",
                flush=True,
            )
    finally:
        _cleanup_g4_state(
            backend_url=args.backend,
            idle_user=idle_user,
            created_idle_auto_ids=created_idle_auto_ids,
            created_idle_ws_ids=created_idle_ws_ids,
            pm_user=pm_user,
            created_auto_ids=created_auto_ids,
            created_ws_ids=created_ws_ids,
            subagent_modified_pm=subagent_modified_pm,
            prior_subagent_pm=prior_subagent_pm,
            key_modified_pm=key_modified_pm,
            prior_keys_pm=prior_keys_pm,
            budget_modified_pm=budget_modified_pm,
            was_configured_pm=was_configured_pm,
            prior_budget_pm=prior_budget_pm,
        )

    ended_at = now_utc_iso()

    scenario_2_dict: dict[str, Any] = {
        "unsee_info": unsee_form4_info,
        "filing": real_form4,
        "execution_id": s2_exec_id,
        "execution_status": s2_exec.get("status"),
        "skip_reason": s2_exec.get("skip_reason"),
        "preflight_decision": (s2_db.get("preflight_log") or {}).get("decision"),
        "preflight_reason": (s2_db.get("preflight_log") or {}).get("reason"),
        "internal_literature_subitem": internal_lit_check,
        "g4_scenario_2_status": (
            "completed" if internal_lit_check["has_new_docs_today"] else "G4 场景②未完成"
        ),
    }

    scenario_results = {
        "test_user_ids": [pm_user, other_user, idle_user],
        "two_user_isolation": two_user_isolation,
        "scenario_1_sec_8k_direct_wake": {
            "unsee_info": unsee_8k_info,
            "filing": real_8k_202,
            "execution_id": s1_exec_id,
            "execution_status": s1_exec.get("status"),
            "thread_id": s1_exec.get("conversation_thread_id"),
            "preflight_decision": (s1_db.get("preflight_log") or {}).get("decision"),
            "preflight_reason": (s1_db.get("preflight_log") or {}).get("reason"),
            "verdicts_recorded": len(s1_verdicts),
            "latest_verdict": s1_verdicts[0] if s1_verdicts else None,
            "signals": s1_db.get("signals"),
        },
        "scenario_2_form4_triage_and_internal_lit": scenario_2_dict,
        "scenario_3_agenda_note_and_due_validation": {
            "dalton_agenda_note": {
                "thread_id": agenda_note_turn.get("thread_id"),
                "response_id": agenda_note_turn.get("response_id"),
                "agenda_version_no": latest_ag_ver.get("version_no"),
                "agenda_version_source": latest_ag_ver.get("source"),
                "agenda_version_reason": latest_ag_ver.get("reason"),
                "open_questions_written": ag_open_questions,
            },
            "due_validation_wake": {
                "execution_id": s3_exec_id,
                "execution_status": s3_exec.get("status"),
                "thread_id": s3_exec.get("conversation_thread_id"),
                "preflight_decision": (s3_db.get("preflight_log") or {}).get("decision"),
                "preflight_reason": (s3_db.get("preflight_log") or {}).get("reason"),
                "verdicts_recorded": len(s3_verdicts),
                "latest_verdict": s3_verdicts[0] if s3_verdicts else None,
                "signals": s3_db.get("signals"),
            },
        },
        "scenario_4_signal_resolution_and_board": {
            "open_signals_by_ticker": open_signals_by_ticker,
            "all_open_signals_zero": all(v == 0 for v in open_signals_by_ticker.values()),
            "board_companies": [
                {
                    "ticker": c.get("ticker"),
                    "workspace_id": c.get("workspace_id"),
                    "open_signals_count": c.get("open_signals_count"),
                    "last_verdict": (c.get("last_verdict") or {}).get("word") if isinstance(c.get("last_verdict"), dict) else None,
                }
                for c in board_companies
                if isinstance(c, dict)
            ],
        },
        "scenario_5_director_daily_zero_token_skip": {
            "user_id": idle_user,
            "idle_workspace_id": s5_idle_ws_id,
            "execution_id": s5_exec_id,
            "execution_status": s5_exec.get("status"),
            "skip_reason": s5_exec.get("skip_reason"),
            "preflight_decision": (s5_db.get("preflight_log") or {}).get("decision"),
            "preflight_reason": (s5_db.get("preflight_log") or {}).get("reason"),
            "cash_spend_usd": s5_cash_spend_usd,
        },
        "scenario_6_shared_budget_exhausted_and_once_defer": {
            "spent_usd_before_cap_lower": spent_before_s6,
            "lowered_cap_usd": 0.01,
            "watch_execution": {
                "execution_id": s6_w_exec_id,
                "status": s6_w_exec.get("status"),
                "skip_reason": s6_w_exec.get("skip_reason"),
                "preflight_reason": (s6_w_db.get("preflight_log") or {}).get("reason"),
            },
            "once_tm3_execution": {
                "execution_id": s6_tm3_exec_id,
                "status": s6_tm3_exec.get("status"),
                "skip_reason": s6_tm3_exec.get("skip_reason"),
                "preflight_reason": (s6_tm3_db.get("preflight_log") or {}).get("reason"),
                "automation_status_after_skip": tm3_after_body.get("status"),
                "initial_next_run_at": initial_tm3_run_at,
                "postponed_next_run_at": tm3_after_body.get("next_run_at"),
            },
        },
    }

    envelope = build_evidence_envelope(
        script="scripts/dalton/realrun/g4_autonomy.py",
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=test_user_id,
        synthetic=synthetic,
        extra_fields=json.loads(json.dumps(scenario_results, default=str)),
    )

    triage_fresh_calls = [
        r
        for r in ((envelope.get("journal") or {}).get("rows") or [])
        if r.get("purpose") == "assist.preflight.triage" and not r.get("replayed")
    ]
    envelope["triage_fresh_calls_count"] = len(triage_fresh_calls)

    assert s1_exec.get("status") == "completed", f"Scenario 1 did not complete: {s1_exec}"
    assert len(s1_verdicts) >= 1, "Scenario 1 did not record a verdict"
    assert len(triage_fresh_calls) >= 1, "Scenario 2 did not record a fresh assist.preflight.triage journal row"
    assert latest_ag_ver.get("source") == "tool", f"dalton_agenda_note did not write source='tool': {latest_ag_ver}"
    assert s3_exec.get("status") == "completed", f"Scenario 3 did not complete: {s3_exec}"
    assert len(s3_verdicts) >= 1, "Scenario 3 did not record a verdict"
    assert all(v == 0 for v in open_signals_by_ticker.values()), f"Open signals remain on board: {open_signals_by_ticker}"
    assert s5_exec.get("status") == "skipped" and (s5_db.get("preflight_log") or {}).get("reason") == "no_new_signals", (
        f"Scenario 5 director daily did not skip with no_new_signals: {s5_exec} {s5_db}"
    )
    assert (s6_w_db.get("preflight_log") or {}).get("reason") == "budget_exhausted", (
        f"Scenario 6 watch did not skip with budget_exhausted: {s6_w_db}"
    )
    assert (s6_tm3_db.get("preflight_log") or {}).get("reason") == "budget_exhausted", (
        f"Scenario 6 tm3 did not skip with budget_exhausted: {s6_tm3_db}"
    )
    assert tm3_after_body.get("status") == "active", f"One-shot tm3 did not stay active: {tm3_after_body}"
    assert expected_tm3_postponed_date in str(tm3_after_body.get("next_run_at") or ""), (
        f"One-shot tm3 next_run_at was not postponed by +1 day (expected {expected_tm3_postponed_date}): {tm3_after_body}"
    )

    s2_note = (
        f"场景 ② SEC Form 4 分诊跳过：`dalton-meta-watch` (`exec={s2_exec_id[:8]}…`) 预检决策 "
        f"`{((s2_db.get('preflight_log') or {}).get('decision'))}` / `{((s2_db.get('preflight_log') or {}).get('reason'))}`，"
        f"执行状态 `{s2_exec.get('status')}` (`{s2_exec.get('skip_reason')}`)，真实 Form 4 `{acc_form4}` 经 "
        f"`assist.preflight.triage` (`replayed=false` 调用 `{len(triage_fresh_calls)}` 次) 跳过"
    )
    if internal_lit_check["has_new_docs_today"]:
        s2_note += (
            f"；内部文献子场景：当天有新资料（`meta_new_docs={internal_lit_check['meta_new_docs_count']}`, "
            f"`googl_new_docs={internal_lit_check['googl_new_docs_count']}`），触发自动化并产生本次执行信号 "
            f"`{len(internal_lit_check['triggered_runs'])}` 次"
        )
    else:
        s2_note += "；内部文献子场景：当日无新增内部文献（**G4 场景②未完成**）"

    extra_md = [
        (
            f"场景 ① SEC 8-K (Item 2.02) 直接唤醒：`dalton-meta-watch` (`exec={s1_exec_id[:8]}…`) 预检决策 "
            f"`{((s1_db.get('preflight_log') or {}).get('decision'))}` / `{((s1_db.get('preflight_log') or {}).get('reason'))}`，"
            f"执行状态 `{s1_exec.get('status')}`，真实 8-K `{acc_8k}` 注入 `<dalton_wake>`，写入结论 "
            f"`{s1_verdicts[0].get('verdict_id')}` (`{s1_verdicts[0].get('word')}`)"
        ),
        s2_note,
        (
            f"场景 ③a `dalton_agenda_note` 实际写入：`POST /api/v1/threads/messages` (`thread={str(agenda_note_turn.get('thread_id'))[:8]}…`) "
            f"驱动 `GOOGL` `agenda` 生成 `v{latest_ag_ver.get('version_no')}` (`source={latest_ag_ver.get('source')}`)，"
            f"新增 `Q-001` (`origin=pm`)"
        ),
        (
            f"场景 ③b 到期验证项 `V-201` 唤醒与结案：`dalton-googl-watch` (`exec={s3_exec_id[:8]}…`) 预检决策 "
            f"`{((s3_db.get('preflight_log') or {}).get('decision'))}` / `{((s3_db.get('preflight_log') or {}).get('reason'))}`，"
            f"执行状态 `{s3_exec.get('status')}`，写入结论 `{s3_verdicts[0].get('verdict_id')}` (`{s3_verdicts[0].get('word')}`)，"
            f"`V-201` 状态更新为 `done`"
        ),
        (
            f"场景 ④ 信号消解、覆盖总览未决信号清零与两用户隔离：`GET /api/v1/dalton/coverage/board` 返回 "
            f"`open_signals_by_ticker={json.dumps(open_signals_by_ticker)}`，唤醒信号均已写入 `resolved_at`；"
            f"`{other_user}` 可见 `{pm_user}` 覆盖总览公司 `{two_user_isolation['other_user_visible_pm_workspaces_on_board']}` 家、"
            f"结论 `{two_user_isolation['other_user_visible_pm_verdicts']}` 条、"
            f"审批请求 `{two_user_isolation['other_user_visible_pm_publish_requests']}` 条"
        ),
        (
            f"场景 ⑤ 每日研究总监零信号跳过（专属用户 `{idle_user}`）：`dalton-director-daily` (`exec={s5_exec_id[:8]}…`) 预检决策 "
            f"`{((s5_db.get('preflight_log') or {}).get('decision'))}` / `{((s5_db.get('preflight_log') or {}).get('reason'))}`，"
            f"执行状态 `{s5_exec.get('status')}` (`{s5_exec.get('skip_reason')}`)，零待办事项时直接跳过，大模型花费 `${s5_cash_spend_usd:.2f}`。"
            "副作用声明（D3）：(1) 预检阶段（`run_preflight`）执行的 7 天旧信号过期（`watch_db.expire_old_signals(max_age_days=7, workspace_id=workspace_id)`）仅按当前工作区（`workspace_id`）过滤，不影响其他工作区；"
            "(2) `probe_director` 中的全局每日维护（`_run_daily_system_maintenance`）仅执行超期 `awaiting_pm` 审批请求过期（`expire_stale_publications`）和超过 30 天的日志响应正文清理（`prune_expired_journal_responses`），与系统每日定时运行的规则一致。"
        ),
        (
            f"场景 ⑥ 共享日预算熔断与一次性任务顺延：`dalton-googl-watch` 与 `dalton-googl-earn-2q26-tm3` 均以 "
            f"`skip` / `budget_exhausted` 跳过（已花费 `${spent_before_s6:.4f}` >= 下调后上限 `$0.01`）；"
            f"一次性任务保持 `status={tm3_after_body.get('status')}`，`next_run_at` 从 `{initial_tm3_run_at}` 顺延至 `{tm3_after_body.get('next_run_at')}`"
        ),
    ]

    key_findings: list[tuple[str, Any]] = [
        ("测试用户 (`pm_user` / `other_user` / `idle_user`)", f"`{pm_user}` / `{other_user}` / `{idle_user}`"),
        ("Scenario 1 (SEC 8-K Item 2.02 direct wake)", f"status={s1_exec.get('status')}, reason={(s1_db.get('preflight_log') or {}).get('reason')}, verdict={s1_verdicts[0].get('verdict_id')} ({s1_verdicts[0].get('word')})"),
        ("Scenario 2 (SEC Form 4 triage skip)", f"status={s2_exec.get('status')}, reason={(s2_db.get('preflight_log') or {}).get('reason')}, triage_fresh_calls={len(triage_fresh_calls)}"),
    ]
    if internal_lit_check["has_new_docs_today"]:
        key_findings.append(
            (
                "Scenario 2 (Internal literature subscenario)",
                f"meta_new_docs={internal_lit_check['meta_new_docs_count']}, googl_new_docs={internal_lit_check['googl_new_docs_count']}, triggered_runs={len(internal_lit_check['triggered_runs'])}",
            )
        )
    else:
        key_findings.append(
            (
                "Scenario 2 (Internal literature subscenario)",
                "G4 场景②未完成（当日无新增内部文献）",
            )
        )
    key_findings.extend(
        [
            ("Scenario 3a (dalton_agenda_note)", f"agenda v{latest_ag_ver.get('version_no')} (source={latest_ag_ver.get('source')}, questions={len(ag_open_questions)})"),
            ("Scenario 3b (Due validation V-201 wake)", f"status={s3_exec.get('status')}, reason={(s3_db.get('preflight_log') or {}).get('reason')}, verdict={s3_verdicts[0].get('verdict_id')} ({s3_verdicts[0].get('word')})"),
            ("Scenario 4 (Coverage board open signals & 2-user isolation)", json.dumps({"open_signals_by_ticker": open_signals_by_ticker, "two_user_isolation": two_user_isolation})),
            ("Scenario 5 (Director daily zero-signal skip)", f"user={idle_user}, status={s5_exec.get('status')}, reason={(s5_db.get('preflight_log') or {}).get('reason')}, cash_spend_usd=${s5_cash_spend_usd:.2f}"),
            ("Scenario 6 (Budget exhausted & once-task defer)", f"watch_reason={(s6_w_db.get('preflight_log') or {}).get('reason')}, tm3_status={tm3_after_body.get('status')}, next_run_at={tm3_after_body.get('next_run_at')}"),
        ]
    )

    raw_out = REPO_ROOT / "var" / "dalton" / "trial-runs" / f"realrun-{args.date}" / "g4_autonomy.json"
    md_out = REPO_ROOT / "var" / "dalton" / "trial-runs" / f"realrun-{args.date}" / "G4.md"

    _, _, sha = write_evidence_and_summary(
        family_code="G4",
        title="自主议程、零 Token 预检、分诊、信号消解、双用户隔离与预算熔断（P0J §5 G4）",
        raw_json_path=raw_out,
        summary_md_path=md_out,
        envelope=envelope,
        key_findings=key_findings,
        notes=extra_md,
        record=args.record,
    )
    if args.record:
        print(f"[G4] Wrote {raw_out} (sha256={sha}) and {md_out}", flush=True)
    else:
        print(f"[G4] Verified (dry-run, sha256={sha}; pass --record to persist under var/dalton/)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

