"""Agenda probe: checks due watch.events, next_validations, open_questions, and wake_conditions (PK.16 / P0H A4)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.dalton.autonomy.due import validation_is_due
from src.dalton.autonomy.probes import ProbeSignal
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    s = str(val).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _is_question_in_backoff(q: dict[str, Any], now_dt: datetime, now_date: date) -> bool:
    """Return True if ``q`` has a ``next_attempt_after`` / ``backoff_until`` timestamp in the future (A11)."""
    raw = q.get("next_attempt_after") or q.get("backoff_until")
    if not raw:
        return False
    s = str(raw).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now_dt.tzinfo or timezone.utc)
        return dt > now_dt
    except ValueError:
        d = _parse_date(s)
        return bool(d and d > now_date)


async def probe_agenda(
    *,
    workspace_id: Optional[str],
    ticker: str,
    watch_state: dict[str, Any],
    agenda_doc: Optional[dict[str, Any]] = None,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    due_tz: Optional[ZoneInfo] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Inspect the company's ``agenda`` document for due watch.events, next_validations, open_questions, and wake_conditions.

    ``ticker`` is the subject label used in summaries; for a company it is the
    ticker and also picks the due-moment fallback timezone via the exchange
    rules. A sector caller (P3) passes the sector's display name as the label
    together with ``due_tz`` — the sector's resolved research timezone — so
    date-only and naive moments are read on the sector's own clock instead of
    a guessed exchange; offset-carrying moments keep their offset either way
    (``parse_due_moment``). ``due_tz`` also moves the "today" the event window
    and question due-dates count from into the sector's calendar: a New-York
    sector's 27th noon is already Shanghai's 28th, and its three-day catalyst
    window must not ride the deployment date (codex r2 #7). Company callers
    omit ``due_tz`` and behave exactly as before.
    """
    tz = ZoneInfo(get_dalton_settings().autonomy.timezone)
    if due_tz is not None:
        tz = due_tz
    now_dt = now or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    if due_tz is not None:
        # 行业分支："今天"取研究时区日历上的日期（传入的时刻可能是别的
        # 时区的）。公司分支保持原式：传入时刻自身的日期。
        now_date = today or now_dt.astimezone(due_tz).date()
    else:
        now_date = today or now_dt.date()
    doc = agenda_doc
    if doc is None and workspace_id:
        try:
            from src.dalton.database.knowledge import latest_valid_version

            row = await latest_valid_version(str(workspace_id), "agenda")
            if row and isinstance(row.get("content"), dict):
                doc = row["content"]
        except Exception as exc:
            logger.warning("[DALTON_AGENDA_PROBE] failed to load agenda for %s: %s", ticker, exc)
            doc = None

    if not isinstance(doc, dict):
        return [], watch_state

    signals: list[ProbeSignal] = []

    # 1. Watch events (schema field `watch.events`, plus legacy `catalysts` fallback) within [today - 2d, today + 3d]
    watch_obj = doc.get("watch") if isinstance(doc.get("watch"), dict) else {}
    events = list(watch_obj.get("events") or []) + list(doc.get("catalysts") or [])
    for item in events:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "upcoming", "watching", "active", "confirmed"):
            continue
        if (item.get("date_confidence") or item.get("date_status")) in ("estimated", "unknown"):
            continue
        exp = _parse_date(item.get("date") or item.get("expected_date"))
        if exp is None:
            continue
        if (now_date - timedelta(days=2)) <= exp <= (now_date + timedelta(days=3)):
            cid = item.get("id") or item.get("title") or item.get("type") or "cat"
            prio = str(item.get("importance") or item.get("priority") or "P1").upper()
            if prio not in ("P0", "P1", "P2"):
                prio = "P1"
            title = item.get("title") or item.get("event") or item.get("plan") or item.get("type") or cid
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"catalyst:{cid}:{exp.isoformat()}",
                    priority=prio,
                    summary=f"{ticker} 催化剂窗口临近：{title}（预计 {exp.isoformat()}）",
                    payload={"kind": "catalyst", "id": cid, "expected_date": exp.isoformat(), "title": title},
                )
            )

    # 2. Next validations past their due moment (schema field `next_validations`, plus legacy
    #    `validation_queue` fallback). Due-ness keeps the item's own moment and timezone — naive
    #    values read in the ticker's exchange timezone — instead of the local (Shanghai) date:
    #    a US item dated for an after-hours call must not fall due while its ET day is young.
    validations = list(doc.get("next_validations") or []) + list(doc.get("validation_queue") or [])
    seen_val_ids: set[str] = set()
    for val_item in validations:
        if not isinstance(val_item, dict):
            continue
        if val_item.get("status") not in (None, "pending", "watching", "open"):
            continue
        raw_due = val_item.get("due") or val_item.get("due_date") or val_item.get("target_date")
        due = _parse_date(raw_due)
        if validation_is_due(raw_due, ticker, now_dt, tz=due_tz):
            vid = str(val_item.get("id") or "V")
            if vid in seen_val_ids:
                continue
            seen_val_ids.add(vid)
            what = val_item.get("what") or val_item.get("metric") or vid
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"validation:{vid}:{due.isoformat()}",
                    priority="P0",
                    summary=f"{ticker} 支柱验证项到期（{vid}）：{what}",
                    payload={"kind": "validation", "id": vid, "due": due.isoformat(), "what": what},
                )
            )

    # 3. Open questions due <= today or origin=='pm' with no attempts (skipping questions in backoff per A11)
    for q in doc.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        if q.get("status") not in (None, "open", "investigating"):
            continue
        if _is_question_in_backoff(q, now_dt, now_date):
            continue
        qid = q.get("id") or "Q"
        question = q.get("question") or q.get("title") or qid
        if q.get("origin") == "pm" and not (q.get("attempts") or []):
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"pm_question:{qid}",
                    priority="P0",
                    summary=f"{ticker} PM 交办问题待处理（{qid}）：{question}",
                    payload={"kind": "pm_question", "id": qid, "question": question},
                )
            )
            continue
        due = _parse_date(q.get("due_date") or q.get("review_by") or q.get("due"))
        prio = str(q.get("priority") or q.get("value") or "P1").upper()
        if prio == "HIGH":
            prio = "P0"
        elif prio == "MEDIUM":
            prio = "P1"
        if due and due <= now_date and prio in ("P0", "P1"):
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"question:{qid}:{due.isoformat()}",
                    priority=prio,
                    summary=f"{ticker} 待查问题到期（{qid}）：{question}",
                    payload={"kind": "open_question", "id": qid, "due_date": due.isoformat(), "question": question},
                )
            )

    # 4. Wake conditions / next actions due <= today (schema field `wake_conditions`, plus legacy `next_actions` fallback)
    wake_items = list(doc.get("wake_conditions") or []) + list(doc.get("next_actions") or [])
    for act in wake_items:
        if not isinstance(act, dict):
            continue
        if act.get("enabled") is False:
            continue
        if act.get("status") not in (None, "pending", "in_progress", "open", "active"):
            continue
        due = _parse_date(act.get("due_date") or act.get("due") or act.get("target_date"))
        prio = str(act.get("priority") or "P1").upper()
        if due and due <= now_date:
            aid = act.get("id") or "W"
            desc = act.get("action") or act.get("condition") or act.get("title") or aid
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"action:{aid}:{due.isoformat()}",
                    priority=prio if prio in ("P0", "P1", "P2") else "P1",
                    summary=f"{ticker} 唤醒条件/议程动作到期（{aid}）：{desc}",
                    payload={"kind": "wake_condition", "id": aid, "due_date": due.isoformat(), "action": desc},
                )
            )

    return signals, watch_state
