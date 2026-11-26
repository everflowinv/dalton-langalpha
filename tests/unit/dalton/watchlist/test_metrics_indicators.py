"""Unit tests for watchlist Wilder RSI, split-adjusted period returns, 52W range, and metrics service."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist import indicators as ind_mod
from src.dalton.watchlist import metrics as metrics_mod


def test_wilder_rsi_and_symbol_metrics_from_daily_bars() -> None:
    # Insufficient bars -> RSI is None
    assert ind_mod.wilder_rsi([100.0] * 14, 14) is None
    # Strictly rising closes -> RSI = 100.0
    rising = [100.0 + i for i in range(40)]
    assert ind_mod.wilder_rsi(rising, 14) == 100.0
    assert ind_mod.wilder_rsi(rising, 30) == 100.0

    # Build 260 chronological daily bars spanning last year into current year
    start = date(2025, 11, 1)
    bars: list[dict[str, Any]] = []
    price = 100.0
    for i in range(260):
        d = start + timedelta(days=i)
        price += 0.5 if i % 3 != 0 else -0.2
        bars.append(
            {
                "date": d.isoformat(),
                "close": round(price, 4),
                "high": round(price + 1.0, 4),
                "low": round(price - 1.0, 4),
                "volume": 1_000_000 if i < 255 else 2_000_000,
            }
        )

    out = ind_mod.compute_symbol_metrics(
        bars,
        latest_price=bars[-1]["close"],
        currency="USD",
        market_cap=2_500_000_000_000,
        next_earnings="2026-12-01",
    )
    assert out["price"] == round(bars[-1]["close"], 4)
    assert out["chg_1d"] is not None
    assert out["chg_1w"] is not None
    assert out["chg_1m"] is not None
    assert out["chg_3m"] is not None
    assert out["chg_ytd"] is not None
    assert out["chg_1y"] is not None
    assert out["position_52w"] is not None
    assert 0.0 <= out["position_52w"] <= 100.0
    assert out["rsi_14d"] is not None
    assert out["rsi_30d"] is not None
    assert out["vol_ratio_5_20"] is not None
    assert out["vol_ratio_5_20"] > 1.0
    assert out["market_cap"] == 2_500_000_000_000
    assert out["next_earnings"] == "2026-12-01"


@pytest.mark.asyncio
async def test_metrics_service_skips_private_and_enriches_earnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": "NVDA", "tier": "core", "is_private": False},
                {"user_id": "u1", "ticker": "OPENAI", "tier": "core", "is_private": True},
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(
            coverage_db.get_earnings_calendar_mirror,
            return_value={"ledger": {"entries": [{"ticker": "NVDA", "earnings_date": "2026-12-15"}]}},
        ),
    )
    snap_calls: list[list[str]] = []

    async def _fake_snaps(symbols: Any, *, user_id: str) -> dict[str, dict[str, Any]]:
        _ = user_id
        snap_calls.append(list(symbols))
        return {"NVDA": {"symbol": "NVDA", "price": 140.0, "previous_close": 135.0, "currency": "USD", "market_cap": 3.4e12}}

    async def _fake_bars(symbol: str, *, user_id: str) -> list[dict[str, Any]]:
        _ = (symbol, user_id)
        return [
            {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000}
            for i in range(40)
        ]

    monkeypatch.setattr(metrics_mod, "_fetch_snapshots_chunked", _fake_snaps)
    monkeypatch.setattr(metrics_mod, "_fetch_daily_bars_singleflight", _fake_bars)

    res = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1")
    assert res["ok"] is True
    assert snap_calls == [["NVDA"]]  # Private OPENAI skipped!
    assert res["items"]["OPENAI"]["is_private"] is True
    assert res["items"]["OPENAI"]["status"] == "unlisted"
    assert res["items"]["NVDA"]["price"] == 140.0
    assert res["items"]["NVDA"]["next_earnings"] == "2026-12-15"
    assert res["items"]["NVDA"]["rsi_14d"] is not None
