"""Calendar probe: zero-token check for weekly earnings calendar refresh (PK.16)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_calendar(
    *,
    watch_state: dict[str, Any],
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Emit signal once per ISO week for `dalton-earnings-calendar`."""
    now_date = today or datetime.now(timezone.utc).date()
    iso_year, iso_week, _ = now_date.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    state = dict(watch_state or {})
    if state.get("last_calendar_week") == week_key:
        return [], state

    state["last_calendar_week"] = week_key
    return [
        ProbeSignal(
            probe="calendar",
            signal_key=f"weekly:{week_key}",
            priority="P1",
            summary=f"覆盖池财报日历周度刷新（{week_key}）",
            payload={"iso_week": week_key, "date": now_date.isoformat()},
        )
    ], state
