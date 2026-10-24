"""Legacy market-data wire models: nullable-volume coercion and currency carry."""

from src.server.models.market_data import (
    CompanyOverviewResponse,
    DailyResponse,
    IntradayDataPoint,
    IntradayResponse,
    SnapshotData,
)


class TestIntradayDataPointVolume:
    def test_null_volume_becomes_zero(self):
        # The protocol allows a null volume (index bars, Tushare rows with no
        # `vol`); the legacy wire is a plain int, so it must not 500.
        point = IntradayDataPoint(
            time=1, open=1.0, high=2.0, low=0.5, close=1.5, volume=None
        )
        assert point.volume == 0

    def test_float_volume_truncates(self):
        point = IntradayDataPoint(
            time=1, open=1.0, high=2.0, low=0.5, close=1.5, volume=1500.9
        )
        assert point.volume == 1500


class TestCurrencyFields:
    def test_snapshot_currency_optional_and_carried(self):
        assert SnapshotData(symbol="AAPL").currency is None
        assert SnapshotData(symbol="600519.SS", currency="CNY").currency == "CNY"

    def test_overview_currency_optional_and_carried(self):
        assert CompanyOverviewResponse(symbol="AAPL").currency is None
        assert (
            CompanyOverviewResponse(symbol="600519.SS", currency="CNY").currency == "CNY"
        )

    def test_bar_responses_carry_currency_timezone_treatment(self):
        cache = {"cached": False}
        intraday = IntradayResponse(
            symbol="600519.SS",
            interval="5min",
            currency="CNY",
            timezone="Asia/Shanghai",
            price_treatment="raw",
            cache=cache,
        )
        assert (intraday.currency, intraday.timezone, intraday.price_treatment) == (
            "CNY",
            "Asia/Shanghai",
            "raw",
        )
        daily = DailyResponse(symbol="AAPL", cache=cache)
        assert (daily.currency, daily.timezone, daily.price_treatment) == (
            None,
            None,
            None,
        )


# DALTON-HOOK(extended-hours-quotes): Verify SnapshotData preserves last_trade_price and regular_trading_change_percent.
def test_snapshot_preserves_extended_hours_fields():
    snap = SnapshotData(
        symbol="GOOG",
        price=317.28,
        regular_close=317.28,
        previous_close=320.76,
        regular_trading_change=-3.48,
        regular_trading_change_percent=-1.0849,
        last_trade_price=318.10,
        late_trading_change=0.82,
        late_trading_change_percent=0.2584,
        market_status="late_trading",
    )
    assert snap.last_trade_price == 318.10
    assert snap.regular_trading_change_percent == -1.0849
    dumped = snap.model_dump()
    assert dumped["last_trade_price"] == 318.10
    assert dumped["regular_trading_change_percent"] == -1.0849
