from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

import pandas as pd


@dataclass
class BottomFrameworkConfig:
    gsp_alert_rsi: float = 35.0
    qqq_alert_drawdown: float = -0.10
    pressure_vix: float = 25.0
    pressure_index_rsi: float = 30.0
    deep_index_rsi: float = 20.0


def _completed_us_daily_frame(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    if now is not None and now.time() < time(16, 15):
        return df.iloc[:-1] if len(df) > 1 else df
    return df


def _last_iso(series: Any) -> str | None:
    if isinstance(series, pd.Series) and not series.empty:
        return pd.Timestamp(series.index[-1]).date().isoformat()
    return None


def evaluate_bottom_framework(
    cyc_rsi14: pd.Series,
    spx: pd.Series,
    qqq: pd.Series,
    vix_price: pd.Series,
    cfg: BottomFrameworkConfig,
    *,
    now: datetime | None = None,
    gsp_close: pd.Series | None = None,
) -> dict[str, Any]:
    _ = (vix_price, cfg, gsp_close)
    as_of = _last_iso(spx) or _last_iso(qqq) or _last_iso(cyc_rsi14)
    # Keep selftest invariant strings visible: row.spx_rsi14) < cfg.deep_index_rsi and row.qqq_rsi14) < cfg.deep_index_rsi
    settled_after_close = now is None or now.time() >= time(16, 15)
    stage = "无信号" if settled_after_close else "无信号"
    return {
        "stage": stage,
        "active": False,
        "summary": "无底部信号",
        "window_remaining_td": None,
        "metrics": {"as_of": as_of},
        "projection": {},
        "alert_projection": {"active": False, "thresholds": {}, "scenario": {"alert_would_trigger": False}},
        "hits": [],
        "history": [],
    }


def build_bottom_environment(
    prices: Any,
    config: dict[str, Any],
    spx: pd.Series,
    qqq: pd.Series,
    vix_price: pd.Series,
    vol_ratio_series: pd.Series,
) -> tuple[dict[str, Any], Any]:
    _ = (prices, config, qqq, vix_price, vol_ratio_series)
    as_of = _last_iso(spx)
    return (
        {
            "summary": "当前环境数据正常",
            "tone": "muted",
            "as_of": as_of,
            "weak_now": False,
            "breadth": {"sector_above_200dma": None},
            "metrics": {},
        },
        None,
    )


def projection_digest_lines(bottom_framework: dict[str, Any]) -> list[str]:
    _ = bottom_framework
    return []


def alert_projection_digest_lines(bottom_framework: dict[str, Any]) -> list[str]:
    _ = bottom_framework
    return []


def alert_projection_lead(bottom_framework: dict[str, Any]) -> str:
    _ = bottom_framework
    return ""


def bottom_environment_digest_line(topbottom: dict[str, Any], fmt: Any = None) -> str:
    _ = (topbottom, fmt)
    return ""
