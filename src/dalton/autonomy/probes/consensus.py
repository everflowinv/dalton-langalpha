"""Consensus probe: zero-token check for street EPS / revenue estimate revisions (PK.16)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_consensus(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    consensus_override: Optional[dict[str, Any]] = None,
    today: Optional[date] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Compare current consensus snapshot against `consensus_baseline` (threshold >= 2.0%)."""
    now_date = today or datetime.now(timezone.utc).date()
    state = dict(watch_state or {})
    current = consensus_override or state.get("consensus_current") or {}
    baseline = state.get("consensus_baseline") or {}
    if not isinstance(current, dict) or not current:
        return [], state

    signals: list[ProbeSignal] = []
    if not isinstance(baseline, dict) or not baseline:
        state["consensus_baseline"] = dict(current)
        return [], state

    for metric_key, cur_val in current.items():
        base_val = baseline.get(metric_key)
        if not isinstance(cur_val, (int, float)) or not isinstance(base_val, (int, float)):
            continue
        if abs(float(base_val)) < 1e-6:
            continue
        delta_pct = ((float(cur_val) - float(base_val)) / abs(float(base_val))) * 100.0
        if abs(delta_pct) >= 2.0:
            prio = "P0" if abs(delta_pct) >= 5.0 else "P1"
            signals.append(
                ProbeSignal(
                    probe="consensus",
                    signal_key=f"rev:{metric_key}:{now_date.isoformat()}",
                    priority=prio,
                    summary=f"{ticker} 卖方一致预期 {metric_key} 修正 {delta_pct:+.2f}%（{base_val} -> {cur_val}）",
                    payload={
                        "metric": metric_key,
                        "baseline": float(base_val),
                        "current": float(cur_val),
                        "delta_pct": round(delta_pct, 2),
                    },
                )
            )
            baseline[metric_key] = float(cur_val)

    state["consensus_baseline"] = baseline
    return signals, state
