"""Price probe: zero-token check for large 1d/5d price moves (PK.16)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_price(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    quote_override: Optional[dict[str, Any]] = None,
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Emit signal when 1d move >= ±5% or 5d move >= ±10%."""
    now_date = today or datetime.now(timezone.utc).date()
    state = dict(watch_state or {})
    quote = quote_override or state.get("latest_quote") or {}
    if not isinstance(quote, dict):
        return [], state

    signals: list[ProbeSignal] = []
    q_date = str(quote.get("as_of") or now_date.isoformat())[:10]
    pct_1d = quote.get("pct_change_1d")
    pct_5d = quote.get("pct_change_5d")

    if isinstance(pct_1d, (int, float)) and abs(float(pct_1d)) >= 5.0:
        direction = "up" if pct_1d > 0 else "down"
        prio = "P0" if abs(float(pct_1d)) >= 8.0 else "P1"
        signals.append(
            ProbeSignal(
                probe="price",
                signal_key=f"1d:{q_date}:{direction}",
                priority=prio,
                summary=f"{ticker} 单日股价异动 {pct_1d:+.2f}%（{q_date}）",
                payload={"pct_change_1d": float(pct_1d), "as_of": q_date, "price": quote.get("price")},
            )
        )

    if isinstance(pct_5d, (int, float)) and abs(float(pct_5d)) >= 10.0:
        direction = "up" if pct_5d > 0 else "down"
        signals.append(
            ProbeSignal(
                probe="price",
                signal_key=f"5d:{q_date}:{direction}",
                priority="P1",
                summary=f"{ticker} 5 日累计股价异动 {pct_5d:+.2f}%（截至 {q_date}）",
                payload={"pct_change_5d": float(pct_5d), "as_of": q_date, "price": quote.get("price")},
            )
        )

    return signals, state
