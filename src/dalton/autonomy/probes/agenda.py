"""Agenda probe: checks due watch.events, next_validations, open_questions, and wake_conditions (PK.16 / P0H A4)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
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
    # One clock for every judgment below: an explicit `now` wins; a `today` with
    # no `now` builds the moment on `today` in the effective timezone, so the
    # date-window checks and the due-moment checks never ride two clocks
    # (prompt review batch 1 round 3 #12).
    if now is not None:
        now_dt = now
    elif today is not None:
        now_dt = datetime.combine(today, dtime(12, 0), tzinfo=tz)
    else:
        now_dt = datetime.now(tz)
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

    # 过期很久的历史项（>30 天）不再逐条唤醒，只汇总成一条低优先级清理信号
    # （提示词第 1 批第 3 轮 #11）；汇总范围＝本来会发信号的项（P0/P1 的待查
    # 问题、启用中的日期唤醒、验证项），不会把本来静默的低优先级项放大。
    stale_ids: list[str] = []
    stale_cutoff = now_date - timedelta(days=30)

    # 2. Next validations past their due moment (schema field `next_validations`, plus legacy
    #    `validation_queue` fallback). Due-ness keeps the item's own moment and timezone — naive
    #    values read in the ticker's exchange timezone — instead of the local (Shanghai) date:
    #    a US item dated for an after-hours call must not fall due while its ET day is young.
    #    Validations keep the 18:00-local rule for date-only dues (close of the local day).
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
            if due and due < stale_cutoff:
                stale_ids.append(vid)
                continue
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

    # 3. Open questions past their due moment (schema field `open_questions[].due_date`,
    #    plus legacy review_by/due fallbacks) or origin=='pm' with no attempts (skipping
    #    questions in backoff per A11). Due-ness shares the unified moment rule
    #    (`validation_is_due`) with the AGENDA reading of a date-only value: due at the
    #    START of the local date (date_hour=0, round 3 #8) — a full ISO moment keeps its
    #    own clock and offset. Only P0/P1 questions ever signal, so only they can go stale.
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
        raw_due = q.get("due_date") or q.get("review_by") or q.get("due")
        due = _parse_date(raw_due)
        prio = str(q.get("priority") or q.get("value") or "P1").upper()
        if prio == "HIGH":
            prio = "P0"
        elif prio == "MEDIUM":
            prio = "P1"
        if prio not in ("P0", "P1"):
            continue
        if not validation_is_due(raw_due, ticker, now_dt, tz=due_tz, date_hour=0):
            continue
        if due and due < stale_cutoff:
            stale_ids.append(str(qid))
            continue
        signals.append(
            ProbeSignal(
                probe="agenda",
                signal_key=f"question:{qid}:{due.isoformat() if due else now_date.isoformat()}",
                priority=prio,
                summary=f"{ticker} 待查问题到期（{qid}）：{question}",
                payload={"kind": "open_question", "id": qid, "due_date": due.isoformat() if due else None, "question": question},
            )
        )

    # 4. Wake conditions / next actions past their due moment (schema field
    #    `wake_conditions` with the date in `params`, plus legacy `next_actions`
    #    top-level spellings), on the same agenda moment rule as questions
    #    (date-only due at local midnight; full moments by their own clock).
    wake_items = list(doc.get("wake_conditions") or []) + list(doc.get("next_actions") or [])
    for act in wake_items:
        if not isinstance(act, dict):
            continue
        if act.get("enabled") is False:
            continue
        if act.get("kind") == "price_level":
            continue
        if act.get("status") not in (None, "pending", "in_progress", "open", "active"):
            continue
        # Schema-legal wake_conditions carry the date inside `params` (params.date
        # canonical; params.at / params.due tolerated). Top-level due_date/due/
        # target_date are pre-schema spellings that can still sit in stored rows,
        # so they stay as legacy fallbacks — reading params first is what makes a
        # schema-valid `kind=date` condition fire at all.
        params = act.get("params") if isinstance(act.get("params"), dict) else {}
        raw_due = (
            params.get("date")
            or params.get("at")
            or params.get("due")
            or act.get("due_date")
            or act.get("due")
            or act.get("target_date")
        )
        due = _parse_date(raw_due)
        prio = str(act.get("priority") or "P1").upper()
        if not validation_is_due(raw_due, ticker, now_dt, tz=due_tz, date_hour=0):
            continue
        aid = act.get("id") or "W"
        if due and due < stale_cutoff:
            stale_ids.append(str(aid))
            continue
        desc = act.get("action") or act.get("condition") or act.get("title") or aid
        signals.append(
            ProbeSignal(
                probe="agenda",
                signal_key=f"action:{aid}:{due.isoformat() if due else now_date.isoformat()}",
                priority=prio if prio in ("P0", "P1", "P2") else "P1",
                summary=f"{ticker} 唤醒条件/议程动作到期（{aid}）：{desc}",
                payload={"kind": "wake_condition", "id": aid, "due_date": due.isoformat() if due else None, "action": desc},
            )
        )

    if stale_ids:
        # One low-priority sweep-up per workspace per ISO WEEK (round 3 #11): the
        # key carries the ISO week so it does not repeat within the week. This
        # signal never directly wakes the sandbox (is_direct_wake_signal refuses
        # it); it rides along as a cleanup hint with whatever else woke.
        iso_year, iso_week, _ = now_date.isocalendar()
        owner = str(workspace_id) if workspace_id else ticker
        signals.append(
            ProbeSignal(
                probe="agenda",
                signal_key=f"stale_todos:{owner}:{iso_year}-W{iso_week:02d}",
                priority="P2",
                summary=f"{ticker} 的 agenda 里有 {len(stale_ids)} 个过期超过 30 天的待办（验证项/待查问题/日期唤醒），请用 knowledge_update 清理或改期：{'、'.join(stale_ids[:8])}{' 等' if len(stale_ids) > 8 else ''}",
                payload={"kind": "stale_todos", "count": len(stale_ids), "ids": stale_ids},
            )
        )

    return signals, watch_state
