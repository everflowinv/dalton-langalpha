"""Director probe: zero-token check for Research Director daily/weekly runs (PK.16 / PK.19)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.dalton.autonomy.due import validation_is_due, workspace_market_timezone
from src.dalton.autonomy.probes import ProbeSignal
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)


def _parse_backoff_future(backoff_val: Any, now_dt: datetime, now_date: date) -> bool:
    if not backoff_val:
        return False
    raw = str(backoff_val).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now_dt.tzinfo or timezone.utc)
        return dt > now_dt
    except ValueError:
        try:
            return date.fromisoformat(raw[:10]) > now_date
        except ValueError:
            return False


def _count_agenda_actionable(
    agenda_docs: list[dict[str, Any]],
    *,
    now_dt: datetime,
    now_date: date,
    doc_markets: Optional[list[tuple[str, ZoneInfo]]] = None,
) -> dict[str, int]:
    due_validations = 0
    actionable_questions = 0
    unconfirmed_earnings_14d = 0

    for idx, doc in enumerate(agenda_docs):
        if not isinstance(doc, dict):
            continue
        ticker, market_tz = (
            doc_markets[idx] if doc_markets and idx < len(doc_markets) else ("", None)
        )
        for v in (doc.get("next_validations") or []) + (doc.get("validation_queue") or []):
            if not isinstance(v, dict):
                continue
            if v.get("status") not in (None, "open", "pending"):
                continue
            raw_due = v.get("due") or v.get("target_date") or v.get("due_date")
            if not raw_due:
                continue
            # 与议程探针、看板同一到期口径：保留原始时刻/时区，缺时区按标的
            # 交易所时区（与看板同源的 company.ticker/exchange 解析）。
            if validation_is_due(raw_due, ticker, now_dt, tz=market_tz):
                due_validations += 1

        for q in doc.get("open_questions") or []:
            if not isinstance(q, dict):
                continue
            if q.get("status") not in (None, "open", "investigating"):
                continue
            if bool(q.get("waiting_pm")) or q.get("status") == "waiting_pm":
                continue
            backoff_raw = q.get("next_attempt_after") or q.get("backoff_until")
            if _parse_backoff_future(backoff_raw, now_dt, now_date):
                continue
            val = str(q.get("value") or q.get("priority") or "medium").lower()
            origin = str(q.get("origin") or "").lower()
            if val in ("high", "p0") or origin == "pm":
                actionable_questions += 1

        events = ((doc.get("watch") or {}).get("events") or []) + (doc.get("catalysts") or [])
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type") or ev.get("kind") or "").lower()
            eplan = str(ev.get("plan") or ev.get("title") or ev.get("event") or "").lower()
            is_earnings = etype == "earnings" or "财报" in eplan or "earnings" in eplan
            date_status = str(ev.get("date_status") or "").lower()
            if not is_earnings or date_status != "estimated":
                continue
            raw_d = ev.get("date") or ev.get("expected_date")
            if not raw_d:
                continue
            try:
                days = (date.fromisoformat(str(raw_d)[:10]) - now_date).days
                if 0 <= days <= 14:
                    unconfirmed_earnings_14d += 1
            except ValueError:
                continue

    return {
        "due_validations": due_validations,
        "actionable_questions": actionable_questions,
        "unconfirmed_earnings_14d": unconfirmed_earnings_14d,
    }


async def _run_daily_system_maintenance(
    *,
    ttl_hours: int,
    user_id: Optional[str] = None,
) -> int:
    """Run system-context daily maintenance (`expire_stale_publications` and 30-day journal pruning) when `user_id is None` (P0J §3.1, L39)."""
    if user_id is not None:
        return 0
    expired_count = 0
    try:
        from src.dalton.database.publications import expire_stale_publications

        # Returns the number of rows it expired, not the rows.
        expired_count = int(await expire_stale_publications(ttl_hours=ttl_hours) or 0)
    except Exception as e:
        logger.warning("[DALTON_DIRECTOR_PROBE] expire_stale_publications failed: %s", e)

    try:
        from src.dalton.database.llm_journal import prune_expired_journal_responses

        pruned_count = await prune_expired_journal_responses(retention_days=30)
        if pruned_count:
            logger.info(
                "[DALTON_DIRECTOR_PROBE] pruned %d expired journal response payloads",
                pruned_count,
            )
    except Exception as e:
        logger.warning("[DALTON_DIRECTOR_PROBE] prune_expired_journal_responses failed: %s", e)
    return expired_count


async def run_daily_system_maintenance() -> dict[str, int]:
    """System-context daily maintenance entrypoint; runs with `user_id=None` and accepts no external IDs."""
    settings = get_dalton_settings()
    ttl_hours = int(getattr(settings.publish, "approval_ttl_hours", 72) or 72)
    expired_publications = await _run_daily_system_maintenance(
        ttl_hours=ttl_hours,
        user_id=None,
    )
    expired_signals = 0
    try:
        from src.dalton.database import watch as watch_db

        sig_rows = await watch_db.expire_old_signals(
            max_age_days=7,
            workspace_id=None,
            system_maintenance=True,
        )
        expired_signals = len(sig_rows or [])
    except Exception as e:
        logger.warning("[DALTON_DIRECTOR_PROBE] expire_old_signals failed: %s", e)
    return {
        "expired_publications": expired_publications,
        "expired_signals": expired_signals,
    }


async def _check_db_actionable_work(
    *,
    now_dt: datetime,
    now_date: date,
    ttl_hours: int,
    user_id: Optional[str] = None,
) -> dict[str, int]:
    """Query Postgres for the 5 daily director actionable work conditions (A7, N2-M3, L39)."""
    counts = {
        "due_validations": 0,
        "actionable_questions": 0,
        "stale_signals_24h": 0,
        "publish_near_or_past_ttl": 0,
        "unconfirmed_earnings_14d": 0,
    }
    try:
        from psycopg.rows import dict_row
        from src.server.database.pool import get_db_connection

        if user_id is None:
            counts["publish_near_or_past_ttl"] += await _run_daily_system_maintenance(
                ttl_hours=ttl_hours,
                user_id=None,
            )

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if user_id is not None:
                    await cur.execute(
                        """
                        SELECT DISTINCT ON (dkv.workspace_id, dkv.doc_key)
                               dkv.workspace_id, dkv.doc_key, dkv.content, ws.name AS ws_name
                        FROM dalton_knowledge_versions dkv
                        JOIN (
                            SELECT workspace_id, name FROM workspaces
                            WHERE user_id = %s AND status <> 'deleted'
                        ) ws ON ws.workspace_id = dkv.workspace_id
                        WHERE dkv.doc_key IN ('agenda', 'company', 'thesis')
                          AND dkv.schema_valid = TRUE
                        ORDER BY dkv.workspace_id, dkv.doc_key, dkv.version_no DESC
                        """,
                        (user_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT DISTINCT ON (dkv.workspace_id, dkv.doc_key)
                               dkv.workspace_id, dkv.doc_key, dkv.content, ws.name AS ws_name
                        FROM dalton_knowledge_versions dkv
                        JOIN (
                            SELECT workspace_id, name FROM workspaces
                            WHERE status <> 'deleted'
                        ) ws ON ws.workspace_id = dkv.workspace_id
                        WHERE dkv.doc_key IN ('agenda', 'company', 'thesis')
                          AND dkv.schema_valid = TRUE
                        ORDER BY dkv.workspace_id, dkv.doc_key, dkv.version_no DESC
                        """
                    )
                agenda_rows = await cur.fetchall()
                # 按工作区分组：agenda 文档 + 同工作区的 company/thesis 内容一起
                # 喂给共享解析，标的与交易所时区和看板同源（工作区名常是中文
                # 公司名，直接拿它推断会把港股 / A 股当美股）。
                ws_docs: dict[str, dict[str, Any]] = {}
                for r in agenda_rows or []:
                    ws_id = str(r.get("workspace_id") or "")
                    entry = ws_docs.setdefault(
                        ws_id, {"ws_name": str(r.get("ws_name") or ""), "agenda": None, "company": {}, "thesis": {}}
                    )
                    content = r.get("content") if isinstance(r.get("content"), dict) else {}
                    doc_key = str(r.get("doc_key") or "")
                    if doc_key in ("agenda", "company", "thesis"):
                        entry[doc_key] = content
                agenda_docs = []
                doc_markets: list[tuple[str, ZoneInfo]] = []
                for ws_id, entry in ws_docs.items():
                    if not isinstance(entry["agenda"], dict):
                        continue
                    agenda_docs.append(entry["agenda"])
                    doc_markets.append(
                        workspace_market_timezone(entry["company"], entry["thesis"], entry["ws_name"])
                    )
                ag_counts = _count_agenda_actionable(
                    agenda_docs, now_dt=now_dt, now_date=now_date, doc_markets=doc_markets
                )
                counts["due_validations"] = ag_counts["due_validations"]
                counts["actionable_questions"] = ag_counts["actionable_questions"]
                counts["unconfirmed_earnings_14d"] = ag_counts["unconfirmed_earnings_14d"]

                if user_id is not None:
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM dalton_signals
                        WHERE resolved_at IS NULL
                          AND expired_at IS NULL
                          AND detected_at <= NOW() - INTERVAL '24 hours'
                          AND workspace_id IN (
                              SELECT workspace_id FROM workspaces
                              WHERE user_id = %s AND status <> 'deleted'
                          )
                        """,
                        (user_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM dalton_signals
                        WHERE resolved_at IS NULL
                          AND expired_at IS NULL
                          AND detected_at <= NOW() - INTERVAL '24 hours'
                        """
                    )
                sig_row = await cur.fetchone()
                counts["stale_signals_24h"] = int((sig_row or {}).get("cnt") or 0)

                warn_hours = max(1, ttl_hours - 24)
                if user_id is not None:
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM dalton_publish_requests
                        WHERE status = 'awaiting_pm'
                          AND created_at <= NOW() - make_interval(hours => %s)
                          AND workspace_id IN (
                              SELECT workspace_id FROM workspaces
                              WHERE user_id = %s AND status <> 'deleted'
                          )
                        """,
                        (warn_hours, user_id),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM dalton_publish_requests
                        WHERE status = 'awaiting_pm'
                          AND created_at <= NOW() - make_interval(hours => %s)
                        """,
                        (warn_hours,),
                    )
                pub_row = await cur.fetchone()
                counts["publish_near_or_past_ttl"] += int((pub_row or {}).get("cnt") or 0)
    except Exception as e:
        logger.warning("[DALTON_DIRECTOR_PROBE] DB actionable work check failed: %s", e)

    return counts


async def probe_director(
    *,
    kind: str,
    watch_state: dict[str, Any],
    today: Optional[date] = None,
    persist: bool = True,
    agenda_docs_override: Optional[list[dict[str, Any]]] = None,
    user_id: Optional[str] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Emit a daily or weekly director wake signal when actionable work exists (PK.16 / PK.19 / A7)."""
    settings = get_dalton_settings()
    try:
        tz = ZoneInfo(settings.autonomy.timezone)
    except Exception:
        tz = timezone.utc
    now_dt = datetime.now(tz)
    now_date = today or now_dt.date()
    state = dict(watch_state or {})

    if kind == "director_weekly":
        iso_year, iso_week, _ = now_date.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        return [
            ProbeSignal(
                probe="director",
                signal_key=f"weekly:{week_key}",
                priority="P1",
                summary=f"研究总监周度全域复盘（{week_key}）",
                payload={"kind": "director_weekly", "iso_week": week_key},
            )
        ], state

    # Daily director: check the 5 actionable conditions before waking (A7)
    ttl_hours = int(getattr(settings.publish, "approval_ttl_hours", 72) or 72)
    counts: dict[str, int]
    if isinstance(state.get("actionable_counts"), dict):
        raw_c = state["actionable_counts"]
        counts = {
            "due_validations": int(raw_c.get("due_validations") or 0),
            "actionable_questions": int(raw_c.get("actionable_questions") or 0),
            "stale_signals_24h": int(raw_c.get("stale_signals_24h") or 0),
            "publish_near_or_past_ttl": int(raw_c.get("publish_near_or_past_ttl") or 0),
            "unconfirmed_earnings_14d": int(raw_c.get("unconfirmed_earnings_14d") or 0),
        }
    elif agenda_docs_override is not None or isinstance(state.get("agenda_docs"), list):
        docs = agenda_docs_override if agenda_docs_override is not None else state.get("agenda_docs") or []
        ag_c = _count_agenda_actionable(docs, now_dt=now_dt, now_date=now_date)
        counts = {
            "due_validations": ag_c["due_validations"],
            "actionable_questions": ag_c["actionable_questions"],
            "stale_signals_24h": int(state.get("stale_signals_24h") or 0),
            "publish_near_or_past_ttl": int(state.get("publish_near_or_past_ttl") or 0),
            "unconfirmed_earnings_14d": ag_c["unconfirmed_earnings_14d"],
        }
    elif "has_actionable_work" in state:
        has_work = bool(state["has_actionable_work"])
        counts = {
            "due_validations": 1 if has_work else 0,
            "actionable_questions": 0,
            "stale_signals_24h": 0,
            "publish_near_or_past_ttl": 0,
            "unconfirmed_earnings_14d": 0,
        }
    elif persist:
        counts = await _check_db_actionable_work(
            now_dt=now_dt,
            now_date=now_date,
            ttl_hours=ttl_hours,
            user_id=user_id,
        )
    else:
        counts = {
            "due_validations": 0,
            "actionable_questions": 0,
            "stale_signals_24h": 0,
            "publish_near_or_past_ttl": 0,
            "unconfirmed_earnings_14d": 0,
        }

    state["last_director_check_counts"] = counts
    total_actionable = sum(counts.values())
    if total_actionable <= 0:
        return [], state

    return [
        ProbeSignal(
            probe="director",
            signal_key=f"daily:{now_date.isoformat()}",
            priority="P1",
            summary=f"研究总监每日晨会调度与覆盖总览刷新（{now_date.isoformat()}，待办 {total_actionable} 项）",
            payload={
                "kind": "director_daily",
                "date": now_date.isoformat(),
                "actionable_counts": counts,
            },
        )
    ], state
