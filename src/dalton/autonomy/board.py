"""Coverage board aggregator for the Dalton Research Director and UI (PK.19)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from psycopg.rows import dict_row

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.budget.service import get_budget_today
from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_EST_COST_BY_KIND: dict[str, float] = {
    "earnings_preview": 3.5,
    "earnings_flash": 1.5,
    "earnings_deep": 5.0,
    "event_note": 1.2,
    "validation_check": 1.8,
    "open_question": 2.0,
    "topic": 6.0,
}

VALUE_WEIGHT = {"high": 3.0, "P0": 3.0, "medium": 2.0, "P1": 2.0, "low": 1.0, "P2": 1.0}
URGENCY_WEIGHT = {
    "overdue": 3.0,
    "now": 3.0,
    "today": 2.5,
    "tm3": 2.5,
    "this_week": 2.0,
    "this_quarter": 1.0,
    "someday": 0.5,
    "later": 0.5,
}


def _load_coverage_yaml() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[3] / "config" / "dalton" / "coverage.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = raw.get("companies") or raw.get("coverage") or []
        out: dict[str, dict[str, Any]] = {}
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and item.get("ticker"):
                    out[str(item["ticker"]).upper()] = item
        return out
    except Exception as e:
        logger.warning("[DALTON_BOARD] failed to load coverage.yaml: %s", e)
        return {}


def _compute_staleness(last_research_iso: str | None, today: date) -> float:
    if not last_research_iso:
        return 1.5
    try:
        d = date.fromisoformat(str(last_research_iso)[:10])
        days = max(0, (today - d).days)
        return min(2.0, 1.0 + (days / 14.0))
    except ValueError:
        return 1.2


def _is_backoff_active(backoff_until: Any, now_dt: datetime, today: date) -> bool:
    if not backoff_until:
        return False
    raw = str(backoff_until).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now_dt.tzinfo or timezone.utc)
        return dt > now_dt
    except ValueError:
        try:
            return date.fromisoformat(raw[:10]) > today
        except ValueError:
            return False


async def build_coverage_board(
    user_id: str | None = None,
    tier: str = "all",
    stale_only: bool = False,
) -> dict[str, Any]:
    """Assemble the cross-company coverage board and candidate ranking (PK.19)."""
    settings = get_dalton_settings()
    tz_name = settings.autonomy.timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now_dt = datetime.now(tz)
    today = now_dt.date()
    cov_map = _load_coverage_yaml()

    budget_raw: dict[str, Any] = {}
    try:
        budget_raw = await get_budget_today(user_id=user_id or LOCAL_DEV_USER_ID)
    except Exception as e:
        logger.warning("[DALTON_BOARD] get_budget_today failed: %s", e)

    cap_usd = float(
        budget_raw["cap_usd"]
        if "cap_usd" in budget_raw and budget_raw["cap_usd"] is not None
        else (budget_raw.get("daily_cap_usd") or 0.0)
    )
    spent_usd = float(
        budget_raw["spent_usd"]
        if "spent_usd" in budget_raw and budget_raw["spent_usd"] is not None
        else (budget_raw.get("spent_today_usd") or 0.0)
    )
    reserved_usd = float(
        budget_raw["open_reserved_usd"]
        if "open_reserved_usd" in budget_raw and budget_raw["open_reserved_usd"] is not None
        else (budget_raw.get("reserved_usd") or 0.0)
    )
    remaining_usd = max(0.0, round(cap_usd - spent_usd - reserved_usd, 2)) if cap_usd > 0 else 50.0
    reserve_for_events_usd = round(min(remaining_usd * 0.2, 6.0), 2)
    allocatable_usd = max(0.0, round(remaining_usd - reserve_for_events_usd, 2))

    budget_summary = {
        **budget_raw,
        "cap_usd": cap_usd,
        "spent_usd": spent_usd,
        "open_reserved_usd": reserved_usd,
        "remaining_usd": remaining_usd,
        "reserve_for_events_usd": reserve_for_events_usd,
        "allocatable_usd": allocatable_usd,
    }

    companies: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    pm_handoffs: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if user_id is not None:
                    await cur.execute(
                        """
                        SELECT workspace_id, name, description, status, updated_at
                        FROM workspaces
                        WHERE status <> 'deleted' AND user_id = %s
                        ORDER BY name ASC
                        """,
                        (user_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT workspace_id, name, description, status, updated_at
                        FROM workspaces
                        WHERE status <> 'deleted'
                        ORDER BY name ASC
                        """
                    )
                ws_rows = await cur.fetchall()
                user_ws_ids = {str(r["workspace_id"]) for r in ws_rows}

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id, doc_key)
                           workspace_id, doc_key, version_no, content, created_at
                    FROM dalton_knowledge_versions
                    WHERE schema_valid = TRUE
                      AND doc_key IN ('thesis', 'agenda', 'company')
                    ORDER BY workspace_id, doc_key, version_no DESC
                    """
                )
                kv_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id)
                           workspace_id, verdict_id, verdict_at, word,
                           trigger_kind, trigger_ref, progress, next_due, payload,
                           conversation_thread_id, created_at
                    FROM dalton_verdicts
                    ORDER BY workspace_id, verdict_at DESC
                    """
                )
                verdict_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id)
                           publication_id, workspace_id, ticker, kind, title,
                           published_date, thesis_version_no, created_at
                    FROM dalton_publications
                    ORDER BY workspace_id, published_date DESC, created_at DESC
                    """
                )
                pub_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT publish_request_id, workspace_id, conversation_thread_id,
                           kind, title, slug, draft_path, review, created_at
                    FROM dalton_publish_requests
                    WHERE status = 'awaiting_pm'
                    ORDER BY created_at DESC
                    """
                )
                req_rows = await cur.fetchall()
                if user_id is not None:
                    req_rows = [r for r in req_rows if str(r.get("workspace_id") or "") in user_ws_ids]
                pending_approvals = [
                    {
                        "request_id": str(r["publish_request_id"]),
                        "publish_request_id": str(r["publish_request_id"]),
                        "workspace_id": str(r["workspace_id"]),
                        "thread_id": str(r["conversation_thread_id"]),
                        "kind": r.get("kind"),
                        "title": r.get("title"),
                        "draft_path": r.get("draft_path"),
                        "review": r.get("review"),
                        "created_at": str(r.get("created_at") or ""),
                    }
                    for r in req_rows
                ]

                await cur.execute(
                    """
                    SELECT signal_id, workspace_id, ticker, probe, signal_key, priority, summary, detected_at
                    FROM dalton_signals
                    WHERE resolved_at IS NULL
                      AND expired_at IS NULL
                    ORDER BY detected_at DESC
                    """
                )
                sig_rows = await cur.fetchall()
                if user_id is not None:
                    sig_rows = [s for s in sig_rows if str(s.get("workspace_id") or "") in user_ws_ids]

                watch_state_by_ws: dict[str, dict[str, Any]] = {}
                try:
                    await cur.execute(
                        """
                        SELECT workspace_id, state
                        FROM dalton_watch_state
                        """
                    )
                    for wr in await cur.fetchall() or []:
                        if isinstance(wr, dict) and wr.get("workspace_id") and isinstance(wr.get("state"), dict):
                            watch_state_by_ws[str(wr["workspace_id"])] = dict(wr["state"])
                except Exception:
                    watch_state_by_ws = {}

        kv_by_ws: dict[str, dict[str, dict[str, Any]]] = {}
        for r in kv_rows:
            ws_id = str(r["workspace_id"])
            kv_by_ws.setdefault(ws_id, {})[str(r["doc_key"])] = dict(r)

        verdict_by_ws = {str(r["workspace_id"]): dict(r) for r in verdict_rows}
        pub_by_ws = {str(r["workspace_id"]): dict(r) for r in pub_rows}
        sigs_by_ws: dict[str, list[dict[str, Any]]] = {}
        for s in sig_rows:
            if s.get("workspace_id"):
                sigs_by_ws.setdefault(str(s["workspace_id"]), []).append(dict(s))

        reqs_by_ws: dict[str, list[dict[str, Any]]] = {}
        for req in pending_approvals:
            if req.get("workspace_id"):
                reqs_by_ws.setdefault(str(req["workspace_id"]), []).append(req)

        for ws in ws_rows:
            ws_id = str(ws["workspace_id"])
            ws_name = str(ws.get("name") or "").strip().upper()
            docs = kv_by_ws.get(ws_id) or {}
            thesis_content = (docs.get("thesis") or {}).get("content") or {}
            agenda_content = (docs.get("agenda") or {}).get("content") or {}
            company_content = (docs.get("company") or {}).get("content") or {}

            ticker = (
                str(company_content.get("ticker") or thesis_content.get("ticker") or ws_name)
                .strip()
                .upper()
            )
            if not ticker:
                continue

            cov_meta = cov_map.get(ticker) or {}
            house_view = thesis_content.get("house_view") or {}
            one_liner = (
                house_view.get("one_liner")
                or thesis_content.get("one_liner")
                or company_content.get("one_liner")
                or ws.get("description")
                or ""
            )
            conviction = house_view.get("conviction") or thesis_content.get("conviction") or "medium"
            stance = house_view.get("stance") or thesis_content.get("stance") or "neutral"

            last_verdict = verdict_by_ws.get(ws_id)
            last_pub = pub_by_ws.get(ws_id)
            last_research_at = (
                last_verdict["verdict_at"].isoformat()[:10]
                if last_verdict and hasattr(last_verdict.get("verdict_at"), "isoformat")
                else (
                    str(last_pub.get("published_date") or "")[:10]
                    if last_pub and last_pub.get("published_date")
                    else None
                )
            )
            staleness = _compute_staleness(last_research_at, today)

            open_questions_list: list[dict[str, Any]] = []
            for q in agenda_content.get("open_questions") or []:
                if not isinstance(q, dict) or q.get("status") not in (None, "open", "investigating"):
                    continue
                qid = str(q.get("id") or "Q-001")
                val = str(q.get("value") or q.get("priority") or "medium").lower()
                urg = str(q.get("urgency") or "this_week").lower()
                backoff_until = q.get("next_attempt_after") or q.get("backoff_until")
                in_backoff = _is_backoff_active(backoff_until, now_dt, today)
                waiting_pm = bool(q.get("waiting_pm")) or q.get("status") == "waiting_pm"
                open_questions_list.append(
                    {
                        "id": qid,
                        "question": q.get("question") or q.get("title") or qid,
                        "value": val,
                        "urgency": urg,
                        "backoff_until": backoff_until,
                        "waiting_pm": waiting_pm,
                    }
                )
                if not waiting_pm and not in_backoff:
                    v_w = VALUE_WEIGHT.get(val, 2.0)
                    u_w = URGENCY_WEIGHT.get(urg, 1.5)
                    score = round(v_w * u_w * staleness, 2)
                    est_usd = DEFAULT_EST_COST_BY_KIND["open_question"]
                    raw_candidates.append(
                        {
                            "workspace_id": ws_id,
                            "ticker": ticker,
                            "task": f"跟进问题 {qid}：{q.get('question') or q.get('title') or qid}",
                            "kind": "open_question",
                            "score": score,
                            "est_usd": est_usd,
                            "why": f"价值={val} · 紧迫度={urg} · 距上次研究权重={staleness:.2f}",
                        }
                    )

            for v in (agenda_content.get("next_validations") or []) + (
                agenda_content.get("validation_queue") or []
            ):
                if not isinstance(v, dict) or v.get("status") not in (None, "open", "pending"):
                    continue
                vid = str(v.get("id") or "V1")
                v_what = str(v.get("what") or v.get("metric") or v.get("title") or vid)
                due_raw = v.get("due") or v.get("target_date") or v.get("due_date")
                if not due_raw:
                    continue
                try:
                    due_date = date.fromisoformat(str(due_raw)[:10])
                    if due_date <= today:
                        score = round(3.0 * 3.0 * staleness, 2)
                        raw_candidates.append(
                            {
                                "workspace_id": ws_id,
                                "ticker": ticker,
                                "task": f"验证节点到期 {vid}：{v_what}",
                                "kind": "validation_check",
                                "score": score,
                                "est_usd": DEFAULT_EST_COST_BY_KIND["validation_check"],
                                "why": f"验证节点到期（{due_date.isoformat()}）· 距上次研究权重={staleness:.2f}",
                            }
                        )
                except ValueError:
                    continue

            catalysts = ((agenda_content.get("watch") or {}).get("events") or []) + (
                agenda_content.get("catalysts") or []
            )
            next_events: list[dict[str, Any]] = []
            for cat in catalysts:
                if isinstance(cat, dict) and cat.get("status") in (None, "upcoming", "watching", "active", "confirmed"):
                    date_status = str(cat.get("date_status") or "confirmed")
                    next_events.append(
                        {
                            "type": cat.get("type") or cat.get("kind") or "catalyst",
                            "title": cat.get("plan") or cat.get("title") or cat.get("event") or "催化剂",
                            "date": cat.get("date") or cat.get("expected_date"),
                            "date_status": "待确认" if date_status == "estimated" else date_status,
                        }
                    )

            waiting_pm_list: list[dict[str, Any]] = []
            for ho in agenda_content.get("pm_handoffs") or []:
                if isinstance(ho, dict) and ho.get("status") in (None, "open", "pending", "waiting"):
                    item = {
                        "id": ho.get("id") or "H1",
                        "what": ho.get("summary") or ho.get("what") or ho.get("question") or "",
                        "since": ho.get("created_at") or ho.get("since") or "",
                    }
                    waiting_pm_list.append(item)
                    pm_handoffs.append({"ticker": ticker, "workspace_id": ws_id, **item})

            ws_sigs = sigs_by_ws.get(ws_id) or []
            for s in ws_sigs[:3]:
                prio = str(s.get("priority") or "P1")
                v_w = VALUE_WEIGHT.get(prio, 2.0)
                score = round(v_w * 2.5 * staleness, 2)
                raw_candidates.append(
                    {
                        "workspace_id": ws_id,
                        "ticker": ticker,
                        "task": f"处理未消费信号 [{s.get('probe')}]：{s.get('summary')}",
                        "kind": "event_note",
                        "score": score,
                        "est_usd": DEFAULT_EST_COST_BY_KIND["event_note"],
                        "why": f"未消费信号（{prio}）",
                    }
                )

            v_payload = (last_verdict or {}).get("payload") or {}
            companies.append(
                {
                    "workspace_id": ws_id,
                    "ticker": ticker,
                    "company_name": company_content.get("name") or cov_meta.get("name") or ticker,
                    "tier": str(cov_meta.get("tier") or company_content.get("tier") or "core").lower(),
                    "sector": cov_meta.get("sector") or company_content.get("sector") or "",
                    "aggregate_status": thesis_content.get("aggregate_status") or "intact",
                    "house_view": {"stance": stance, "conviction": conviction},
                    "stance": stance,
                    "conviction": conviction,
                    "one_liner": one_liner,
                    "thesis_version": (docs.get("thesis") or {}).get("version_no"),
                    "official_version": house_view.get("official_version"),
                    "last_verdict": (
                        {
                            "id": last_verdict.get("verdict_id"),
                            "verdict_id": last_verdict.get("verdict_id"),
                            "word": last_verdict.get("word"),
                            "verdict": last_verdict.get("word"),
                            "at": (
                                last_verdict["verdict_at"].isoformat()
                                if hasattr(last_verdict.get("verdict_at"), "isoformat")
                                else str(last_verdict.get("verdict_at") or "")
                            ),
                            "event_date": (
                                last_verdict["verdict_at"].isoformat()[:10]
                                if hasattr(last_verdict.get("verdict_at"), "isoformat")
                                else str(last_verdict.get("verdict_at") or "")[:10]
                            ),
                            "summary": v_payload.get("summary") or v_payload.get("rationale") or "",
                            "thread_id": (
                                str(last_verdict["conversation_thread_id"])
                                if last_verdict.get("conversation_thread_id")
                                else None
                            ),
                        }
                        if last_verdict
                        else None
                    ),
                    "open_questions": open_questions_list,
                    "open_signals": [
                        {
                            "id": str(s.get("signal_id")),
                            "kind": s.get("probe"),
                            "summary": s.get("summary"),
                        }
                        for s in ws_sigs
                    ],
                    "next_events": next_events,
                    "next_catalyst": next_events[0] if next_events else None,
                    "waiting_pm": waiting_pm_list,
                    "publish_awaiting_pm": [
                        {
                            "request_id": str(r.get("request_id")),
                            "title": r.get("title"),
                        }
                        for r in (reqs_by_ws.get(ws_id) or [])
                    ],
                    "last_publication": (
                        {
                            "publication_id": str(last_pub.get("publication_id")),
                            "title": last_pub.get("title"),
                            "report_type": last_pub.get("kind"),
                            "published_at": str(last_pub.get("published_date") or ""),
                        }
                        if last_pub
                        else None
                    ),
                    "last_research_at": last_research_at,
                    "pending_signals": len(ws_sigs),
                    "sec_probe_degraded": bool(
                        (watch_state_by_ws.get(ws_id) or {}).get("sec_probe_degraded")
                    ),
                }
            )
    except Exception as e:
        logger.warning("[DALTON_BOARD] DB query fallback: %s", e)

    raw_candidates.sort(key=lambda c: (-float(c["score"]), str(c["ticker"])))
    selected_candidates: list[dict[str, Any]] = []
    running_est = 0.0
    for item in raw_candidates:
        est = float(item.get("est_usd") or 1.5)
        if cap_usd <= 0 or (running_est + est <= allocatable_usd + 1e-6):
            running_est = round(running_est + est, 2)
            selected_candidates.append(
                {
                    "rank": len(selected_candidates) + 1,
                    **item,
                }
            )

    return {
        "date": today.isoformat(),
        "tz": tz_name,
        "generated_at": now_dt.isoformat(),
        "budget": budget_summary,
        "companies": companies,
        "candidates": selected_candidates,
        "est_cost_by_kind": DEFAULT_EST_COST_BY_KIND,
        "pending_approvals": pending_approvals,
        "pm_handoffs": pm_handoffs,
    }
