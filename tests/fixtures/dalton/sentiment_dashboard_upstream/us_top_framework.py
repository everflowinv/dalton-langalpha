from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class TopFrameworkConfig:
    window_td: int = 20
    excess_timeout_td: int = 40
    gsp_warn_rsi30: float = 68.0
    quantum_warn_gain_pct: float = 100.0
    excess_qqq_rsi14: float = 75.0
    excess_qqq_200dma_pct: float = 20.0
    excess_momentum_rsi: float = 75.0
    confirm_drawdown_pct: float = 5.0
    confirm_ma_td: int = 20
    put_hedge_vvix_vix: float = 6.5
    put_hedge_cheap_vix: float = 15.0
    cool_drawdown_15_pct: float = -15.0
    weak_trend_premium_pct: float = 0.0
    quantum_low_td: int = 60
    put_hedge_stats_start: str = "2010-01-01"


def evaluate_top_framework(
    cyc_rsi30: pd.Series,
    quantum_index: pd.Series,
    mom: pd.Series,
    spx: pd.Series,
    qqq: pd.Series,
    vix_price: pd.Series,
    vvix_price: pd.Series,
    trend_premium: pd.Series,
    cfg: TopFrameworkConfig,
) -> dict[str, Any]:
    _ = (cyc_rsi30, quantum_index, mom, qqq, vix_price, vvix_price, trend_premium, cfg)
    as_of = pd.Timestamp(spx.index[-1]).date().isoformat() if isinstance(spx, pd.Series) and not spx.empty else None
    return {
        "stage": "无信号",
        "active": False,
        "strength": "",
        "summary": "无顶部信号",
        "window_remaining_td": None,
        "excess_timeout_remaining_td": None,
        "environment": {"weak_now": False},
        "metrics": {"as_of": as_of},
        "projection": {},
        "put_hedge": {"alert": False, "stats": {}},
        "hits": [],
        "history": [],
    }


def top_digest_lines(topbottom: dict[str, Any], fmt: Any = None) -> list[str]:
    _ = (topbottom, fmt)
    return []
