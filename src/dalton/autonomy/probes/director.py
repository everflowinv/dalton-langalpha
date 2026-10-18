"""Director probe: zero-token check for Research Director daily/weekly runs (PK.16 / PK.19)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_director(
    *,
    kind: str,
    watch_state: dict[str, Any],
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Emit a daily or weekly director wake signal."""
    now_date = today or datetime.now(timezone.utc).date()
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

    return [
        ProbeSignal(
            probe="director",
            signal_key=f"daily:{now_date.isoformat()}",
            priority="P1",
            summary=f"研究总监每日晨会调度与覆盖总览刷新（{now_date.isoformat()}）",
            payload={"kind": "director_daily", "date": now_date.isoformat()},
        )
    ], state
