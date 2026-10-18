"""Earnings probe: zero-token check for earnings window and new earnings releases (PK.16)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_earnings(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    workspace_id: Optional[str] = None,
    agenda_doc: Optional[dict[str, Any]] = None,
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Check if `next_earnings_date` is within T-3..T+1 or if a new earnings release arrived."""
    now_date = today or datetime.now(timezone.utc).date()
    state = dict(watch_state or {})
    signals: list[ProbeSignal] = []

    next_earn_raw = state.get("next_earnings_date")
    period = state.get("next_earnings_period") or "next"

    if not next_earn_raw and isinstance(agenda_doc, dict):
        for cat in (agenda_doc.get("catalysts") or []) + ((agenda_doc.get("watch") or {}).get("events") or []):
            if not isinstance(cat, dict):
                continue
            ctype = str(cat.get("type") or "").lower()
            ctitle = str(cat.get("title") or cat.get("event") or "").lower()
            if ctype == "earnings" or "财报" in ctitle or "earnings" in ctitle:
                cand_date = cat.get("expected_date") or cat.get("date")
                if cand_date:
                    next_earn_raw = cand_date
                    if cat.get("period"):
                        period = str(cat["period"])
                    break

    if next_earn_raw:
        try:
            earn_date = date.fromisoformat(str(next_earn_raw)[:10])
            days_until = (earn_date - now_date).days
            if 1 <= days_until <= 3:
                signals.append(
                    ProbeSignal(
                        probe="earnings",
                        signal_key=f"window:{period}:tm3:{earn_date.isoformat()}",
                        priority="P0",
                        summary=f"{ticker} {period} 财报 T-3 前瞻窗口开启（披露日 {earn_date.isoformat()}）",
                        payload={"period": period, "earnings_date": earn_date.isoformat(), "stage": "tm3"},
                    )
                )
            elif days_until == 0:
                signals.append(
                    ProbeSignal(
                        probe="earnings",
                        signal_key=f"window:{period}:t0:{earn_date.isoformat()}",
                        priority="P0",
                        summary=f"{ticker} {period} 财报今日披露（T+0 快评窗口）",
                        payload={"period": period, "earnings_date": earn_date.isoformat(), "stage": "t0"},
                    )
                )
            elif days_until == -1:
                signals.append(
                    ProbeSignal(
                        probe="earnings",
                        signal_key=f"window:{period}:tp1:{earn_date.isoformat()}",
                        priority="P0",
                        summary=f"{ticker} {period} 财报 T+1 深度复盘与模型更新窗口",
                        payload={"period": period, "earnings_date": earn_date.isoformat(), "stage": "tp1"},
                    )
                )
        except ValueError:
            pass

    latest_release_id = state.get("latest_earnings_release_id")
    last_seen_release_id = state.get("last_seen_earnings_release_id")
    if latest_release_id and latest_release_id != last_seen_release_id:
        signals.append(
            ProbeSignal(
                probe="earnings",
                signal_key=f"release:{latest_release_id}",
                priority="P0",
                summary=f"{ticker} 检测到新财报/业绩电话会材料（id={latest_release_id}）",
                payload={"release_id": latest_release_id},
            )
        )
        state["last_seen_earnings_release_id"] = latest_release_id

    return signals, state
