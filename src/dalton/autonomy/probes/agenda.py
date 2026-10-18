"""Agenda probe: checks due catalysts, validations, open questions, and next actions (PK.16)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    s = str(val).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


async def probe_agenda(
    *,
    workspace_id: Optional[str],
    ticker: str,
    watch_state: dict[str, Any],
    agenda_doc: Optional[dict[str, Any]] = None,
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Inspect the company's `agenda` document for due catalysts, validations, open questions, and actions."""
    now_date = today or datetime.now(timezone.utc).date()
    doc = agenda_doc
    if doc is None and workspace_id:
        try:
            from src.dalton.database.knowledge import latest_valid_version

            row = await latest_valid_version(str(workspace_id), "agenda")
            if row and isinstance(row.get("content"), dict):
                doc = row["content"]
        except Exception:
            doc = None

    if not isinstance(doc, dict):
        return [], watch_state

    signals: list[ProbeSignal] = []

    # 1. Catalysts / watch events within [today - 2d, today + 3d]
    events = (doc.get("catalysts") or []) + ((doc.get("watch") or {}).get("events") or [])
    for item in events:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "upcoming", "watching", "active", "confirmed"):
            continue
        if item.get("date_status") == "estimated":
            continue
        exp = _parse_date(item.get("expected_date") or item.get("date"))
        if exp is None:
            continue
        if (now_date - timedelta(days=2)) <= exp <= (now_date + timedelta(days=3)):
            cid = item.get("id") or item.get("title") or item.get("type") or "cat"
            prio = str(item.get("importance") or item.get("priority") or "P1").upper()
            if prio not in ("P0", "P1", "P2"):
                prio = "P1"
            title = item.get("title") or item.get("event") or item.get("type") or cid
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"catalyst:{cid}:{exp.isoformat()}",
                    priority=prio,
                    summary=f"{ticker} 催化剂窗口临近：{title}（预计 {exp.isoformat()}）",
                    payload={"kind": "catalyst", "id": cid, "expected_date": exp.isoformat(), "title": title},
                )
            )

    # 2. Validation queue items due <= today
    for val_item in doc.get("validation_queue") or []:
        if not isinstance(val_item, dict):
            continue
        if val_item.get("status") not in (None, "pending", "watching", "open"):
            continue
        due = _parse_date(val_item.get("due") or val_item.get("due_date"))
        if due and due <= now_date:
            vid = val_item.get("id") or "V"
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

    # 3. Open questions due <= today or origin=='pm' with no attempts
    for q in doc.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        if q.get("status") not in (None, "open", "investigating"):
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
        due = _parse_date(q.get("due_date") or q.get("review_by"))
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

    # 4. Next actions due <= today
    for act in doc.get("next_actions") or []:
        if not isinstance(act, dict):
            continue
        if act.get("status") not in (None, "pending", "in_progress", "open"):
            continue
        due = _parse_date(act.get("due_date") or act.get("due"))
        prio = str(act.get("priority") or "P1").upper()
        if due and due <= now_date:
            aid = act.get("id") or "A"
            desc = act.get("action") or act.get("title") or aid
            signals.append(
                ProbeSignal(
                    probe="agenda",
                    signal_key=f"action:{aid}:{due.isoformat()}",
                    priority=prio if prio in ("P0", "P1", "P2") else "P1",
                    summary=f"{ticker} 议程动作到期（{aid}）：{desc}",
                    payload={"kind": "next_action", "id": aid, "due_date": due.isoformat(), "action": desc},
                )
            )

    return signals, watch_state
