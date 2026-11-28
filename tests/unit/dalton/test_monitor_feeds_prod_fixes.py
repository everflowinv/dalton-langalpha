"""Offline regression tests for monitor_feeds production update-failure fixes (items 1-6)."""

from __future__ import annotations

from datetime import date, datetime
import json
import math
from pathlib import Path
import time
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import akshare as ak
import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest
import requests
import yfinance as yf

from scripts.dalton.monitor_feeds import common as feeds_common
from scripts.dalton.monitor_feeds.hk_momentum import compute as hkmom_compute
from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkmom_fetch
from scripts.dalton.monitor_feeds.hk_momentum import render as hkmom_render
from scripts.dalton.monitor_feeds.hk_momentum import run as hkmom_run
from scripts.dalton.monitor_feeds.pm_cockpit import collect as pm_collect
from scripts.dalton.monitor_feeds.pm_cockpit import run as pm_run

_REAL_SLEEP = time.sleep
_REAL_FETCH_HKEX = hkmom_fetch.fetch_hkex_universe
_REAL_BUILD_UNIVERSE = hkmom_fetch.build_universe
_REAL_DOWNLOAD_HISTORY = hkmom_fetch.download_history
_REAL_COMPUTE_ALL = hkmom_compute.compute_all
_REAL_COMPUTE_INTRADAY = hkmom_compute.compute_intraday_snapshot
_REAL_NOW_HK = feeds_common.now_hk
_REAL_NOW_US = feeds_common.now_us
_REAL_NOW_CN = feeds_common.now_cn


def _make_us_price_frame(
    cfg: dict[str, Any],
    dates: pd.DatetimeIndex,
    *,
    holiday_futures_only_date: str | None = None,
) -> pd.DataFrame:
    tickers = pm_collect.us_tickers(cfg)
    cols = pd.MultiIndex.from_product([["Adj Close", "Close"], tickers])
    rng = np.random.default_rng(102)
    data: dict[tuple[str, str], np.ndarray] = {}
    for idx, tk in enumerate(tickers):
        series = 100.0 * np.exp(np.cumsum(0.0003 + rng.normal(0.0, 0.005, len(dates))))
        if tk == "^VIX":
            series = np.full(len(dates), 15.5)
        elif tk == "^VIX3M":
            series = np.full(len(dates), 18.4)
        data[("Adj Close", tk)] = series
        data[("Close", tk)] = series * 0.99
    df = pd.DataFrame(data, index=dates, columns=cols)
    if holiday_futures_only_date is not None:
        ts = pd.Timestamp(holiday_futures_only_date)
        row_vals = {col: float("nan") for col in cols}
        for fut in ("HG=F", "GC=F"):
            if ("Adj Close", fut) in row_vals:
                row_vals[("Adj Close", fut)] = float(df[("Adj Close", fut)].iloc[-1]) * 1.001
                row_vals[("Close", fut)] = float(df[("Close", fut)].iloc[-1]) * 1.001
        extra = pd.DataFrame([row_vals], index=pd.DatetimeIndex([ts]), columns=cols)
        df = pd.concat([df, extra])
    return df


def _make_cn_fake_ak(
    dates: pd.DatetimeIndex,
    *,
    l2_rows: list[dict[str, Any]] | None = None,
    weekly_dates: list[str] | None = None,
    weekly_fn: Any = None,
    daily_val_fn: Any = None,
) -> SimpleNamespace:
    l2_default = l2_rows or [
        {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "静态市盈率": 62.0, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
        {"行业代码": "801012.SI", "行业名称": "种植业", "上级行业": "农林牧渔", "成份个数": 12, "静态市盈率": 24.0, "TTM(滚动)市盈率": 25.0, "市净率": 1.9, "静态股息率": 1.2},
    ]
    w_dates = (
        list(weekly_dates)
        if weekly_dates is not None
        else feeds_common.completed_trading_weeks("cn", "2021-12-31", dates[-1].date())
    )
    l2_codes = [str(r["行业代码"]).split(".")[0] for r in l2_default]

    class _FakeAk:
        def stock_zh_index_daily_tx(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(
                {"date": dates.strftime("%Y-%m-%d"), "close": np.linspace(3500.0, 4100.0, len(dates))}
            )

        def stock_zh_index_daily_em(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(
                {"date": dates.strftime("%Y-%m-%d"), "close": np.linspace(3505.0, 4105.0, len(dates))}
            )

        def sw_index_first_info(self) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {"行业代码": "801010.SI", "行业名称": "农林牧渔", "成份个数": 80, "静态市盈率": 21.0, "TTM(滚动)市盈率": 22.0, "市净率": 2.4, "静态股息率": 1.5},
                    {"行业代码": "801080.SI", "行业名称": "电子", "成份个数": 120, "静态市盈率": 46.0, "TTM(滚动)市盈率": 48.0, "市净率": 3.8, "静态股息率": 0.8},
                ]
            )

        def sw_index_second_info(self) -> pd.DataFrame:
            return pd.DataFrame(l2_default)

        def index_hist_sw(self, symbol: str, period: str = "day") -> pd.DataFrame:
            closes = np.linspace(1000.0, 1150.0, len(dates))
            return pd.DataFrame(
                {"日期": dates.strftime("%Y-%m-%d"), "收盘": closes, "成交额": np.full(len(dates), 120.0)}
            )

        def index_analysis_week_month_sw(self, symbol: str = "week") -> pd.DataFrame:
            return pd.DataFrame({"date": w_dates})

        def index_analysis_weekly_sw(self, symbol: str, date: str) -> pd.DataFrame:
            if weekly_fn is not None:
                return weekly_fn(symbol, date)
            codes = ["801010", "801080"] if symbol == "一级行业" else l2_codes
            pub_dt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            return pd.DataFrame(
                [
                    {"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.1, "股息率": 1.5}
                    for c in codes
                ]
            )

        def index_analysis_daily_sw(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            if daily_val_fn is not None:
                return daily_val_fn(symbol, start_date, end_date)
            codes = ["801010", "801080"] if symbol == "一级行业" else l2_codes
            return pd.DataFrame(
                [
                    {"指数代码": f"{c}.SI", "发布日期": str(dates[-1].date()), "市盈率": 21.0, "市净率": 2.2, "股息率": 1.4}
                    for c in codes
                ]
            )

    ak_impl = _FakeAk()
    return SimpleNamespace(
        stock_zh_index_daily_tx=create_autospec(
            ak.stock_zh_index_daily_tx, side_effect=ak_impl.stock_zh_index_daily_tx
        ),
        stock_zh_index_daily_em=create_autospec(
            ak_impl.stock_zh_index_daily_em, side_effect=ak_impl.stock_zh_index_daily_em
        ),
        sw_index_first_info=create_autospec(
            ak_impl.sw_index_first_info, side_effect=ak_impl.sw_index_first_info
        ),
        sw_index_second_info=create_autospec(
            ak_impl.sw_index_second_info, side_effect=ak_impl.sw_index_second_info
        ),
        index_hist_sw=create_autospec(ak_impl.index_hist_sw, side_effect=ak_impl.index_hist_sw),
        index_analysis_week_month_sw=create_autospec(
            ak_impl.index_analysis_week_month_sw, side_effect=ak_impl.index_analysis_week_month_sw
        ),
        index_analysis_weekly_sw=create_autospec(
            ak_impl.index_analysis_weekly_sw, side_effect=ak_impl.index_analysis_weekly_sw
        ),
        index_analysis_daily_sw=create_autospec(
            ak_impl.index_analysis_daily_sw, side_effect=ak_impl.index_analysis_daily_sw
        ),
    )


# ---------------------------------------------------------------------------
# Decision 1: Market Holidays (Thanksgiving US, Black Friday half-day, CN/HK)
# ---------------------------------------------------------------------------


def test_pm_us_thanksgiving_holiday_uses_prev_trading_day_and_half_day_normal(tmp_path: Path) -> None:
    """On US Thanksgiving (2026-11-26), futures-only rows do not trigger insufficient-sample failure;
    status is ok with as_of=2026-11-25. Half-day Black Friday (2026-11-27) runs normally."""
    cfg = pm_collect.load_config()
    xnys = xcals.get_calendar("XNYS")
    sessions_pre = pd.DatetimeIndex(xnys.sessions_in_range("2026-06-01", "2026-11-25"))
    us_df_thanksgiving = _make_us_price_frame(
        cfg, sessions_pre, holiday_futures_only_date="2026-11-26"
    )
    fake_yf = SimpleNamespace(
        download=create_autospec(yf.download, return_value=us_df_thanksgiving)
    )

    thanksgiving_dt = datetime(2026, 11, 26, 17, 15, 0, tzinfo=feeds_common.US_TZ)
    res_holiday = pm_collect.fetch_us(
        cfg,
        raw_dir=tmp_path / "raw_tg",
        yf_module=fake_yf,
        now=thanksgiving_dt,
    )
    assert res_holiday["ok"] is True
    assert res_holiday["stale"] is False
    assert res_holiday["is_trading_day"] is False
    assert res_holiday["last_date"] == "2026-11-25"
    assert res_holiday["source_status"]["ok"] is True
    assert res_holiday["source_status"]["as_of"] == "2026-11-25"
    assert res_holiday["insufficient"] == []

    # Half-day Black Friday (2026-11-27) is a trading day -> normal as_of=2026-11-27
    sessions_bf = pd.DatetimeIndex(xnys.sessions_in_range("2026-06-01", "2026-11-27"))
    us_df_bf = _make_us_price_frame(cfg, sessions_bf)
    fake_yf_bf = SimpleNamespace(download=create_autospec(yf.download, return_value=us_df_bf))
    bf_dt = datetime(2026, 11, 27, 17, 15, 0, tzinfo=feeds_common.US_TZ)
    assert feeds_common.is_trading_day("us", date(2026, 11, 27)) is True
    res_bf = pm_collect.fetch_us(
        cfg,
        raw_dir=tmp_path / "raw_bf",
        yf_module=fake_yf_bf,
        now=bf_dt,
    )
    assert res_bf["ok"] is True
    assert res_bf["is_trading_day"] is True
    assert res_bf["last_date"] == "2026-11-27"
    assert res_bf["source_status"]["as_of"] == "2026-11-27"

    # CN & HK holidays resolve to latest trading day
    assert feeds_common.is_trading_day("us", date(2026, 11, 26)) is False
    assert feeds_common.resolve_market_trade_date("us", as_of_date=date(2026, 11, 26)) == date(2026, 11, 25)
    assert feeds_common.resolve_market_trade_date("cn", as_of_date=date(2026, 10, 1)) == date(2026, 9, 30)
    assert feeds_common.resolve_market_trade_date("hk", as_of_date=date(2026, 10, 1)) == date(2026, 9, 30)


# ---------------------------------------------------------------------------
# Decision 2: CSI 300 Benchmark without py_mini_racer (bench_tx + bench_em fallback)
# ---------------------------------------------------------------------------


def test_pm_cn_csi300_uses_tx_primary_and_falls_back_to_em_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSI 300 primary uses stock_zh_index_daily_tx (no py_mini_racer); if tx fails,
    bench_em fallback succeeds with cn ok=True and stale=False."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()
    xshg = xcals.get_calendar("XSHG")
    dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-25"))

    # 1) Primary bench_tx succeeds (only passes symbol="sh000300", filters dates post-return,
    #    and never touches stock_zh_index_daily or bench_em)
    fake_ak_tx = _make_cn_fake_ak(dates)
    full_tx_df = pd.DataFrame(
        {
            "date": ["2019-01-02"] + list(dates.strftime("%Y-%m-%d")) + ["2026-11-26"],
            "close": [3000.0] + list(np.linspace(3500.0, 4100.0, len(dates))) + [4200.0],
        }
    )

    def _tx_symbol_only(symbol: str) -> pd.DataFrame:
        return full_tx_df.copy()

    fake_ak_tx.stock_zh_index_daily_tx.side_effect = _tx_symbol_only
    raw_tx = tmp_path / "raw_tx"
    res_tx = pm_collect.fetch_cn(
        cfg,
        raw_dir=raw_tx,
        ak_module=fake_ak_tx,
        as_of_date=date(2026, 11, 25),
    )
    assert res_tx["ok"] is True
    assert res_tx["stale"] is False
    assert res_tx["bench_source"] == "bench_tx"
    assert res_tx["source_status"]["ok"] is True
    assert res_tx["source_status"]["as_of"] == "2026-11-25"
    assert fake_ak_tx.stock_zh_index_daily_tx.call_count == 1
    tx_call = fake_ak_tx.stock_zh_index_daily_tx.call_args
    assert tx_call.args == ()
    assert tx_call.kwargs == {"symbol": "sh000300"}
    assert "start_date" not in tx_call.kwargs and "end_date" not in tx_call.kwargs
    bench_csv = pd.read_csv(raw_tx / "cn_sw_hist.csv", index_col=0)
    assert "沪深300" in bench_csv.columns
    assert "2019-01-02" not in set(bench_csv.index.astype(str))
    assert "2026-11-26" not in set(bench_csv.index.astype(str))
    assert "2026-11-25" in set(bench_csv.index.astype(str))
    assert fake_ak_tx.stock_zh_index_daily_em.call_count == 0
    assert not hasattr(fake_ak_tx, "stock_zh_index_daily")

    # 2) Primary bench_tx fails -> falls back to bench_em cleanly (cn still ok=True!)
    fake_ak_fb = _make_cn_fake_ak(dates)
    fake_ak_fb.stock_zh_index_daily_tx.side_effect = RuntimeError("tx 502")
    res_em = pm_collect.fetch_cn(
        cfg,
        raw_dir=tmp_path / "raw_em",
        ak_module=fake_ak_fb,
        as_of_date=date(2026, 11, 25),
    )
    assert res_em["ok"] is True
    assert res_em["stale"] is False
    assert res_em["bench_source"] == "bench_em"
    assert res_em["errors"] == []
    assert res_em["source_status"]["ok"] is True
    assert fake_ak_fb.stock_zh_index_daily_tx.call_count == 3
    assert fake_ak_fb.stock_zh_index_daily_em.call_count == 1


# ---------------------------------------------------------------------------
# Decision 3: Shenwan Valuation Early Missing Code (801193) vs Post-Inception Missing
# ---------------------------------------------------------------------------


def test_pm_cn_valuation_allows_801193_early_absence_but_fails_after_first_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """801193 missing before its first observed date (2022-01-14) passes cleanly;
    801193 missing AFTER 2022-01-14 fails validation."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()
    xshg = xcals.get_calendar("XSHG")
    dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-25"))
    l2_rows = [
        {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "静态市盈率": 62.0, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
        {"行业代码": "801193.SI", "行业名称": "证券Ⅱ", "上级行业": "非银金融", "成份个数": 50, "静态市盈率": 21.0, "TTM(滚动)市盈率": 22.0, "市净率": 1.6, "静态股息率": 1.8},
    ]

    # Case A: 801193 absent on 2021-12-31 & 2022-01-07, first appears on 2022-01-14 and stays present on 2022-01-21
    def _weekly_early_ok(symbol: str, date_str: str) -> pd.DataFrame:
        if symbol == "一级行业":
            codes = ["801010", "801080"]
        else:
            codes = ["801081"] if date_str in ("20211231", "20220107") else ["801081", "801193"]
        pub_dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.0, "股息率": 1.5} for c in codes]
        )

    raw_ok = tmp_path / "val_early_ok"
    fake_ak_ok = _make_cn_fake_ak(
        dates,
        l2_rows=l2_rows,
        weekly_fn=_weekly_early_ok,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_ok, ak_module=fake_ak_ok, as_of_date=date(2026, 11, 25))["ok"] is True
    res_ok = pm_collect.fetch_cn_valuation(
        cfg, raw_dir=raw_ok, ak_module=fake_ak_ok, as_of_date=date(2026, 11, 25)
    )
    assert res_ok["ok"] is True
    assert res_ok["stale"] is False
    assert res_ok["missing_pairs"] == 0
    prog_ok = json.loads((raw_ok / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    for wk in ("2021-12-31", "2022-01-07", "2022-01-14", "2022-01-21"):
        assert set(prog_ok["done"][wk]) == {"一级行业", "二级行业"}

    # Case B: 801193 exists on 2022-01-14, then goes MISSING on 2022-01-21 -> MUST fail!
    def _weekly_late_missing(symbol: str, date_str: str) -> pd.DataFrame:
        if symbol == "一级行业":
            codes = ["801010", "801080"]
        else:
            codes = ["801081"] if date_str in ("20211231", "20220107", "20220121") else ["801081", "801193"]
        pub_dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.0, "股息率": 1.5} for c in codes]
        )

    raw_fail = tmp_path / "val_late_fail"
    fake_ak_fail = _make_cn_fake_ak(
        dates,
        l2_rows=l2_rows,
        weekly_fn=_weekly_late_missing,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_fail, ak_module=fake_ak_fail, as_of_date=date(2026, 11, 25))["ok"] is True
    res_fail = pm_collect.fetch_cn_valuation(
        cfg, raw_dir=raw_fail, ak_module=fake_ak_fail, as_of_date=date(2026, 11, 25)
    )
    assert res_fail["ok"] is False
    assert res_fail["stale"] is True
    assert any("2022-01-21" in e and "801193" in e for e in res_fail["errors"])
    prog_fail = json.loads((raw_fail / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    # Early dates before first-seen are still marked done, while 2022-01-21 L2 stays pending!
    assert "二级行业" in prog_fail["done"].get("2021-12-31", [])
    assert "二级行业" in prog_fail["done"].get("2022-01-07", [])
    assert "二级行业" not in prog_fail["done"].get("2022-01-21", [])


# ---------------------------------------------------------------------------
# Decision 4: HK Suspended Constituent Carry-Forward (<=20 trading days ok, >20 fail)
# ---------------------------------------------------------------------------


def _seed_hk_panel_with_00626(
    st_dir: Path,
    sessions: list[str],
    *,
    suspend_days: int,
    credible_suspension: bool = True,
) -> tuple[list[str], requests.Session]:
    """Seed HK momentum state with 00626 held and suspended for the last ``suspend_days`` sessions."""
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    codes = ["00626"] + [f"{i:05d}" for i in range(1, 60)]
    uni_rows = []
    for idx, c in enumerate(codes):
        is_susp_00626 = c == "00626" and suspend_days > 0 and credible_suspension
        uni_rows.append(
            {
                "code": c,
                "name": "PUBLIC FIN HOLD" if c == "00626" else f"HK{c}",
                "subcat": "Ordinary Shares",
                "ccy": "HKD",
                "is_ggt": idx >= 52,
                "price": 2.49 if c == "00626" else (10.0 + idx * 0.1),
                "prev_close": 2.49 if is_susp_00626 else (2.40 if c == "00626" else (9.9 + idx * 0.1)),
                "volume": 0.0 if is_susp_00626 else 500000.0,
                "turnover": 0.0 if is_susp_00626 else 2000000.0,
                "trading_status": "S" if is_susp_00626 else "",
                "suspended": is_susp_00626,
                "quote_date": sessions[-1],
                "mktcap_e8": 25.0,
                "name_cn": "大众金融控股" if c == "00626" else f"港股{c}",
            }
        )
        c_sessions = sessions[:-suspend_days] if (c == "00626" and suspend_days > 0) else sessions
        rows = []
        for t_i, dt_str in enumerate(c_sessions):
            if c == "00626":
                px = 2.49
            else:
                px = 10.0 * (1.0 + 0.0005 * t_i * (1 + (idx % 5) * 0.15)) * (
                    1.0 + 0.015 * math.sin(t_i * 0.7 + idx)
                )
            rows.append([dt_str, px, px, px * 1.01, px * 0.99, 100000, 200.0])
        pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)

    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股{c}" for c in codes[52:]}), encoding="utf-8"
    )
    # Lock 00626 in both smallcap and nonggt across completed month-end rebalances up to sessions[-2]
    held = codes[:50]
    mes = hkmom_compute.month_ends(sessions[:-1], as_of_date=date.fromisoformat(sessions[-2]))
    rebal_dates = [d for d in mes if sessions.index(d) >= 273]
    rebal_date = rebal_dates[0]
    rebal_idx = sessions.index(rebal_date)
    frozen_dates = sessions[rebal_idx:-1]
    seed_frozen: dict[str, Any] = {
        "launch_date": rebal_date,
        "rebalance_snapshots": {d: {"smallcap": held, "nonggt": held} for d in rebal_dates},
        "frozen_series": {
            "dates": frozen_dates,
            "smallcap": [1000.0 + i for i in range(len(frozen_dates))],
            "nonggt": [1000.0 + i for i in range(len(frozen_dates))],
            "hsi": [1000.0 + i for i in range(len(frozen_dates))],
        },
    }
    if suspend_days >= 2 and credible_suspension:
        seed_frozen["suspended_on_last_date"] = ["00626"]
        seed_frozen["suspended_by_date"] = {
            d_s: ["00626"] for d_s in sessions[-suspend_days:-1]
        }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")

    class _FakeHsiResp:
        def json(self):
            return {"data": {"hkHSI": {"day": [[d, 20000.0, 20000.0 + i] for i, d in enumerate(sessions)]}}}

    hsi_sess = create_autospec(requests.Session, instance=True)
    hsi_sess.get.return_value = _FakeHsiResp()
    return codes, hsi_sess


def test_hkmom_suspended_00626_carries_close_up_to_20_days_and_removes_over_20_days(
    tmp_path: Path,
) -> None:
    """00626 suspended for <=20 trading days carries forward last close and annotates page;
    00626 suspended for 21 trading days (>20) is removed from calculation, remaining weights
    renormalize to 1, task succeeds, and page + status list the removed code."""
    xhkg = xcals.get_calendar("XHKG")
    # 1) Suspended 2 trading days (11-25, 11-26; last close 2026-11-24) -> succeeds & annotates page
    sessions_2d = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    assert sessions_2d[-3:] == ["2026-11-24", "2026-11-25", "2026-11-26"]
    st_2d = tmp_path / "hk_susp_2d"
    _, hsi_sess_2d = _seed_hk_panel_with_00626(st_2d, sessions_2d, suspend_days=2)

    res_2d = hkmom_compute.compute_all(
        state_dir=st_2d, as_of_date=date(2026, 11, 26), hsi_session=hsi_sess_2d
    )
    assert res_2d["meta"]["ok"] is True
    assert res_2d["meta"]["series_stale"] is False
    assert res_2d["can_freeze"] is True
    assert res_2d["series"]["dates"][-1] == "2026-11-26"
    assert res_2d["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}

    susp_sc = res_2d["meta"]["suspended_constituents"]["smallcap"]
    assert len(susp_sc) == 1
    assert susp_sc[0]["code"] == "00626"
    assert susp_sc[0]["carried_close_date"] == "2026-11-24"
    assert susp_sc[0]["carried_close"] == 2.49
    assert susp_sc[0]["suspended_trading_days"] == 2
    assert susp_sc[0]["note"] == "停牌，沿用 2026-11-24 日收盘价"

    c00626 = next(x for x in res_2d["constituents"]["smallcap"] if x["code"] == "00626")
    assert c00626["suspended"] is True
    assert c00626["suspended_note"] == "停牌，沿用 2026-11-24 日收盘价"

    page_html = hkmom_render.render_hkmom_html(
        series=res_2d["series"],
        meta=res_2d["meta"],
        constituents=res_2d["constituents"],
        intraday={"as_of": "2026-11-26 16:35:00", "trading": False, "stale": False},
    )
    assert "停牌，沿用 2026-11-24 日收盘价" in page_html

    # 2) Suspended 20 trading days (exact N=20 boundary) -> still carries forward & succeeds
    sessions_20d = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-03")]
    st_20d = tmp_path / "hk_susp_20d"
    _, hsi_sess_20d = _seed_hk_panel_with_00626(st_20d, sessions_20d, suspend_days=20)
    res_20d = hkmom_compute.compute_all(
        state_dir=st_20d, as_of_date=date(2026, 12, 3), hsi_session=hsi_sess_20d
    )
    assert res_20d["meta"]["ok"] is True
    assert res_20d["meta"]["suspended_constituents"]["smallcap"][0]["suspended_trading_days"] == 20
    assert res_20d["meta"]["removed_suspended_codes"] == []

    # 3) Suspended 21 trading days (> 20) -> removed from calculation, task succeeds, remaining weights sum to 1!
    sessions_21d = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-04")]
    st_21d = tmp_path / "hk_susp_21d"
    _, hsi_sess_21d = _seed_hk_panel_with_00626(st_21d, sessions_21d, suspend_days=21)

    res_21d = hkmom_compute.compute_all(
        state_dir=st_21d, as_of_date=date(2026, 12, 4), hsi_session=hsi_sess_21d
    )
    assert res_21d["meta"]["ok"] is True
    assert res_21d["meta"]["series_stale"] is False
    assert res_21d["can_freeze"] is True
    assert res_21d["series"]["dates"][-1] == "2026-12-04"
    assert res_21d["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}
    assert res_21d["meta"]["removed_suspended_codes"] == ["00626"]
    assert res_21d["meta"]["source_statuses"]["index_compute"]["removed_codes"] == ["00626"]
    assert "已停牌超过 20 个交易日，移出计算" in (
        res_21d["meta"]["source_statuses"]["index_compute"].get("note") or ""
    )

    for key in ("smallcap", "nonggt"):
        rem_items = res_21d["meta"]["removed_suspended_constituents"][key]
        assert len(rem_items) == 1
        assert rem_items[0]["code"] == "00626"
        assert rem_items[0]["suspended_trading_days"] == 21
        assert rem_items[0]["note"] == "已停牌超过 20 个交易日，移出计算"
        active_cons = res_21d["constituents"][key]
        assert len(active_cons) == 49
        assert all(c["code"] != "00626" for c in active_cons)
        assert sum(float(c["weight"]) for c in active_cons) == pytest.approx(1.0, abs=1e-9)

    sdoc_21d = json.loads((st_21d / "status.json").read_text(encoding="utf-8"))
    assert sdoc_21d["overall"]["ok"] is True
    assert sdoc_21d["overall"]["removed_suspended_codes"] == ["00626"]
    assert sdoc_21d["sources"]["index_compute"]["removed_codes"] == ["00626"]

    page_21d = hkmom_render.render_hkmom_html(
        series=res_21d["series"],
        meta=res_21d["meta"],
        constituents=res_21d["constituents"],
        intraday={"as_of": "2026-12-04 16:35:00", "trading": False, "stale": False},
    )
    assert "已停牌超过 20 个交易日，移出计算" in page_21d
    assert "00626" in page_21d


# ---------------------------------------------------------------------------
# Decision 5: HK History Partial Download Failure Tolerance (<=3% & non-held)
# ---------------------------------------------------------------------------


def test_hkmom_history_partial_failure_ratio_and_held_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.4% non-held history failure succeeds with failure count in status;
    any held failure or >3% failure fails hk_history."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", create_autospec(hkmom_fetch.time.sleep, return_value=None))
    codes = [f"{i:05d}" for i in range(1, 215)]  # 214 codes: 3 failures = 1.40%, 7 failures = 3.27%
    held_codes = set(codes[:50])

    def _build_session(fail_set: set[str]) -> requests.Session:
        def _fake_get(url: str, **_kw):
            code = url.split("param=hk")[-1].split(",")[0]
            resp = create_autospec(requests.Response, instance=True)
            if code in fail_set:
                resp.json.return_value = {"data": {f"hk{code}": {"qfqday": []}}}
            else:
                resp.json.return_value = {
                    "data": {
                        f"hk{code}": {
                            "qfqday": [
                                ["2026-11-25", "10.0", "10.1", "10.2", "9.9", "1000", "100.0"],
                                ["2026-11-26", "10.1", "10.2", "10.3", "10.0", "1000", "100.0"],
                            ]
                        }
                    }
                }
            return resp

        sess = create_autospec(requests.Session, instance=True)
        sess.get.side_effect = _fake_get
        return sess

    # 1) 3/214 (1.40% <= 3%), all outside held_codes -> ok=True, stale=False, note records 3/214
    st_ok = tmp_path / "hist_ok_14pct"
    st_ok.mkdir()
    (st_ok / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c} for c in sorted(held_codes)], "nonggt": []}),
        encoding="utf-8",
    )
    prog_ok = hkmom_fetch.download_history(
        codes,
        state_dir=st_ok,
        workers=4,
        session=_build_session({"00201", "00202", "00203"}),
        target_date="2026-11-26",
    )
    assert prog_ok["n_errors"] == 3
    assert prog_ok["stale"] is False
    assert prog_ok["source_status"]["ok"] is True
    assert prog_ok["source_status"]["stale"] is False
    assert "3/214" in (prog_ok["source_status"].get("note") or "")
    assert prog_ok["source_status"]["counts"]["failed"] == 3

    # 2) 1/214 (0.47% <= 3%) BUT failed code 00001 IS in held_codes -> MUST fail!
    st_held = tmp_path / "hist_fail_held"
    st_held.mkdir()
    (st_held / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c} for c in sorted(held_codes)], "nonggt": []}),
        encoding="utf-8",
    )
    prog_held = hkmom_fetch.download_history(
        codes,
        state_dir=st_held,
        workers=4,
        session=_build_session({"00001"}),
        target_date="2026-11-26",
    )
    assert prog_held["stale"] is True
    assert prog_held["source_status"]["ok"] is False
    assert prog_held["failed_held"] == ["00001"]
    assert "00001" in (prog_held["source_status"].get("reason") or "")

    # 3) 7/214 (3.27% > 3%), even outside held_codes -> MUST fail!
    st_over = tmp_path / "hist_fail_over3pct"
    st_over.mkdir()
    (st_over / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c} for c in sorted(held_codes)], "nonggt": []}),
        encoding="utf-8",
    )
    prog_over = hkmom_fetch.download_history(
        codes,
        state_dir=st_over,
        workers=4,
        session=_build_session({f"{i:05d}" for i in range(201, 208)}),
        target_date="2026-11-26",
    )
    assert prog_over["stale"] is True
    assert prog_over["source_status"]["ok"] is False
    assert "3%" in (prog_over["source_status"].get("reason") or "")


# ---------------------------------------------------------------------------
# Decision 6: pm-market-state Exit Code Matches required_sources
# ---------------------------------------------------------------------------


def test_pm_run_exits_nonzero_when_cn_or_cn_valuation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When US succeeds but CN fails in mode='all', pm_cockpit.run.main() MUST exit non-zero
    and emit structured failure_summary so monitor shows recent update failed."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(_REAL_SLEEP, return_value=None))
    monkeypatch.setattr(
        pm_collect,
        "now_us",
        create_autospec(_REAL_NOW_US, return_value=datetime(2026, 11, 25, 17, 15, tzinfo=feeds_common.US_TZ)),
    )
    monkeypatch.setattr(
        pm_collect,
        "now_cn",
        create_autospec(_REAL_NOW_CN, return_value=datetime(2026, 11, 25, 16, 0, tzinfo=feeds_common.CN_TZ)),
    )
    cfg = pm_collect.load_config()
    xnys = xcals.get_calendar("XNYS")
    us_dates = pd.DatetimeIndex(xnys.sessions_in_range("2025-01-02", "2026-11-25"))
    us_df = _make_us_price_frame(cfg, us_dates)

    xshg = xcals.get_calendar("XSHG")
    cn_dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-02", "2026-11-25"))
    fake_ak = _make_cn_fake_ak(cn_dates)
    fake_ak.stock_zh_index_daily_tx.side_effect = RuntimeError("tx down")
    fake_ak.stock_zh_index_daily_em.side_effect = RuntimeError("em down")

    mod_yf = ModuleType("yfinance")
    mod_yf.download = create_autospec(yf.download, return_value=us_df)  # type: ignore[attr-defined]
    mod_ak = ModuleType("akshare")
    for k, v in vars(fake_ak).items():
        setattr(mod_ak, k, v)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", mod_yf)
    monkeypatch.setitem(__import__("sys").modules, "akshare", mod_ak)

    st_dir = tmp_path / "pm_cn_fail_state"
    out_dir = tmp_path / "pm_cn_fail_out"
    monkeypatch.setattr(
        "sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir), "--json"]
    )
    rc = pm_run.main()
    stdout_text = capsys.readouterr().out
    payload = json.loads(stdout_text)

    # Per-panel publishing (lane 98): the healthy US panel still publishes (exit 0) while the
    # CN panel is blocked by check_binding_source_health and records 最近一次更新未生效.
    assert rc == 0
    assert payload["publish_ok"] is True
    assert payload["ok"] is False
    assert payload["required_sources"] == ["us", "cn", "cn_valuation"]
    assert payload["sources"]["us"]["ok"] is True
    assert payload["sources"]["cn"]["ok"] is False
    panels = payload.get("panels") or {}
    assert panels["cn-sector-rotation"]["ok"] is False
    assert panels["pm-market-state"]["ok"] is True


# ---------------------------------------------------------------------------
# Round 2 Fixes (Items 1-6 + zh-CN failure copy)
# ---------------------------------------------------------------------------


def _build_hk_daily_entry_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: list[str],
    codes: list[str],
    ggt_codes: set[str],
    hist_fail_codes: set[str] | None = None,
    hist_omit_last_bar_codes: set[str] | None = None,
    spot_overrides: dict[str, dict[str, Any]] | None = None,
    ggt_fail: bool = False,
    hkex_fail: bool = False,
    spot_fail: bool = False,
) -> tuple[Path, Path]:
    """Seed a realistic HK momentum state up to sessions[-2] and wire full run_daily_job/main()."""
    st_dir = tmp_path / "hk_r2_state"
    out_dir = tmp_path / "hk_r2_out"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    target_str = sessions[-1]
    target_day = date.fromisoformat(target_str)
    quote_date = target_str.replace("-", "/")
    fail_set = hist_fail_codes or set()
    omit_last_set = hist_omit_last_bar_codes or set()
    overrides = spot_overrides or {}

    bar_cache: dict[str, list[list[Any]]] = {}
    for idx, c in enumerate(codes):
        base_px = 10.0 + (idx % 12) * 0.5
        slope = 0.001 + (idx % 20) * 0.00025
        c_sessions = sessions[:-1] if c in omit_last_set else sessions
        bars: list[list[Any]] = []
        for t, ds in enumerate(c_sessions):
            px = round(base_px * (1.0 + slope * t) * (1.0 + 0.015 * math.sin(t * 0.9 + idx)), 3)
            bars.append([ds, f"{px}", f"{px}", f"{px * 1.01:.3f}", f"{px * 0.99:.3f}", "500000", {}, "0", "0", "300.0"])
        bar_cache[c] = bars
        # Seed prior CSV on disk ending sessions[-2] so non-held history failure leaves prior bars intact
        prior_bars = [
            [b[0], b[1], b[2], b[3], b[4], b[5], "300.0"]
            for b in bars
            if b[0] <= sessions[-2]
        ]
        pd.DataFrame(
            prior_bars,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)

    hsi_rows = [
        [ds, f"{20000.0 + t * 5.0:.2f}", f"{20000.0 + t * 5.0:.2f}", f"{20050.0 + t * 5.0:.2f}", f"{19950.0 + t * 5.0:.2f}", "1000000"]
        for t, ds in enumerate(sessions)
    ]

    held = codes[:50]
    mes = hkmom_compute.month_ends(sessions[:-1], as_of_date=date.fromisoformat(sessions[-2]))
    rebal_dates = [d for d in mes if sessions.index(d) >= 273]
    rebal_idx = sessions.index(rebal_dates[0])
    frozen_dates = sessions[rebal_idx:-1]
    prior_ok = f"{sessions[-2]} 16:35:00 北京时间"
    seed_frozen = {
        "launch_date": rebal_dates[0],
        "rebalance_snapshots": {d: {"smallcap": held, "nonggt": held} for d in rebal_dates},
        "frozen_series": {
            "dates": frozen_dates,
            "smallcap": [1000.0 + i for i in range(len(frozen_dates))],
            "nonggt": [1000.0 + i for i in range(len(frozen_dates))],
            "hsi": [1000.0 + i for i in range(len(frozen_dates))],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(seed_frozen["frozen_series"]), encoding="utf-8")
    (st_dir / "constituents.json").write_text(
        json.dumps(
            {
                "smallcap": [{"code": c, "name": f"HK{c}"} for c in held],
                "nonggt": [{"code": c, "name": f"HK{c}"} for c in held],
            }
        ),
        encoding="utf-8",
    )
    (st_dir / "index_meta.json").write_text(
        json.dumps({"last_date": sessions[-2], "launch_date": rebal_dates[0], "last_success_at": prior_ok}),
        encoding="utf-8",
    )
    # Seed prior clean universe.csv & ggt_list.json so degraded universe falls back to prior pool
    pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
            "is_ggt": [c in ggt_codes for c in codes],
            "excluded": [False] * len(codes),
            "price": [15.0] * len(codes),
            "mktcap_e8": [20.0 if i < 55 else 150.0 for i in range(len(codes))],
            "name_cn": [f"HK{c}" for c in codes],
        }
    ).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股通{c}" for c in sorted(ggt_codes)}, ensure_ascii=False),
        encoding="utf-8",
    )
    feeds_common.SourceStatusStore(st_dir).update_sources(
        [
            feeds_common.make_source_result(n, ok=True, stale=False, label=n)
            for n in (
                "hkex_universe", "ggt_list", "hk_spot", "hk_history",
                "hsi_history", "index_compute", "hk_intraday_spot", "hsi_spot", "html_render",
            )
        ],
        now_str=prior_ok,
    )

    def _resp(*, text: str = "", json_data: Any = None) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        resp.status_code = 200
        resp.text = text
        resp.encoding = "utf-8"
        resp.raise_for_status.return_value = None
        resp.json.return_value = json_data
        return resp

    def _fake_get(url: str, **_kw):
        if "szse.cn" in url:
            if ggt_fail:
                raise RuntimeError("SZSE GGT 502")
            ggt_sorted = sorted(ggt_codes)
            items = [{"zqdm": c, "zqjc": f"港股通{c}"} for c in ggt_sorted]
            return _resp(json_data=[{"metadata": {"pagecount": 1, "recordcount": len(items)}, "data": items}])
        if "hkfqkline" in url:
            code = url.split("param=hk")[-1].split(",")[0]
            if code in fail_set:
                return _resp(json_data={"data": {f"hk{code}": {"qfqday": []}}})
            return _resp(json_data={"data": {f"hk{code}": {"qfqday": bar_cache[code]}}})
        if "kline/kline" in url and "hkHSI" in url:
            return _resp(json_data={"data": {"hkHSI": {"day": hsi_rows}}})
        if "r_hkHSI" in url:
            fields = ["0"] * 47
            fields[1], fields[3], fields[4], fields[30], fields[32] = (
                "恒生指数", "20500.0", "20400.0", f"{quote_date} 16:08:00", "0.49"
            )
            return _resp(text=f'v_r_hkHSI="{"~".join(fields)}";')
        if "qt.gtimg.cn" in url:
            if spot_fail:
                raise RuntimeError("GTImg spot 502")
            q_part = url.split("q=")[-1]
            req_codes = [p.replace("r_hk", "") for p in q_part.split(",") if p]
            lines = []
            for c in req_codes:
                idx = int(c) - 1
                ov = overrides.get(c) or {}
                mktcap = 20.0 if idx < 55 else 150.0
                fields = ["0"] * 60
                fields[1] = f"HK{c}"
                fields[3] = str(ov.get("price", "15.0"))
                fields[4] = str(ov.get("prev_close", "14.8"))
                fields[6] = str(ov.get("vol_6", ov.get("volume", "500000")))
                fields[30] = str(ov.get("ts", f"{quote_date} 16:08:00"))
                fields[32] = str(ov.get("pct", "1.35"))
                fields[36] = str(ov.get("vol_36", ov.get("volume", "500000")))
                fields[37] = str(ov.get("turnover", "2000"))
                fields[40] = str(ov.get("status_40", "0"))
                fields[45] = f"{mktcap}"
                lines.append(f'v_r_hk{c}="{"~".join(fields)}"')
            return _resp(text=";".join(lines))
        raise RuntimeError(f"Unexpected URL: {url}")

    sub_sess = create_autospec(requests.Session, instance=True)
    sub_sess.get.side_effect = _fake_get

    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )

    def _fetch_hkex_stub(*, state_dir: Path, proxy: str = "", session: Any = None):
        if hkex_fail:
            raise RuntimeError("HKEX 503")
        return hkex_df.copy()

    fixed_dt = datetime(target_day.year, target_day.month, target_day.day, 16, 35, 0, tzinfo=feeds_common.HK_TZ)

    def _call_build_universe(**kw: Any):
        return _REAL_BUILD_UNIVERSE(**{**kw, "session": sub_sess})

    def _call_download_history(*args: Any, **kw: Any):
        return _REAL_DOWNLOAD_HISTORY(*args, **{**kw, "session": sub_sess})

    def _call_compute_all(**kw: Any):
        return _REAL_COMPUTE_ALL(**{**kw, "as_of_date": target_day, "hsi_session": sub_sess})

    def _call_compute_intraday(**kw: Any):
        return _REAL_COMPUTE_INTRADAY(**{**kw, "now": fixed_dt, "session": sub_sess})

    monkeypatch.setattr(hkmom_fetch.time, "sleep", create_autospec(_REAL_SLEEP, return_value=None))
    monkeypatch.setattr(
        hkmom_fetch,
        "fetch_hkex_universe",
        create_autospec(_REAL_FETCH_HKEX, side_effect=_fetch_hkex_stub),
    )
    monkeypatch.setattr(
        hkmom_run,
        "build_universe",
        create_autospec(_REAL_BUILD_UNIVERSE, side_effect=_call_build_universe),
    )
    monkeypatch.setattr(
        hkmom_run,
        "download_history",
        create_autospec(_REAL_DOWNLOAD_HISTORY, side_effect=_call_download_history),
    )
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        create_autospec(_REAL_COMPUTE_ALL, side_effect=_call_compute_all),
    )
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        create_autospec(_REAL_COMPUTE_INTRADAY, side_effect=_call_compute_intraday),
    )
    monkeypatch.setattr(hkmom_run, "now_hk", create_autospec(_REAL_NOW_HK, return_value=fixed_dt))
    monkeypatch.setattr(hkmom_compute, "now_hk", create_autospec(_REAL_NOW_HK, return_value=fixed_dt))
    return st_dir, out_dir


def test_hkmom_daily_job_and_main_exit_code_for_hk_history_tolerance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Item 2: On non-month-end, hk_history is required for publish:
    >3% or held failure exits 1 with failure_summary; <=3% non-held exits 0 and shows count on page."""
    xhkg = xcals.get_calendar("XHKG")
    # 2026-11-26 is a non-month-end HK trading day (Nov month-end is 2026-11-30)
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    codes = [f"{i:05d}" for i in range(1, 121)]  # 120 codes: 2 fail = 1.67% <= 3%, 5 fail = 4.17% > 3%
    ggt_codes = set(codes[55:])

    # Case A: 5/120 (4.17% > 3%) non-held history failure -> main() MUST exit 1 with failure_summary!
    st_over, out_over = _build_hk_daily_entry_harness(
        tmp_path / "over3",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_fail_codes={"00110", "00111", "00112", "00113", "00114"},
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_over), "--output-dir", str(out_over)])
    rc_over = hkmom_run.main()
    payload_over = json.loads(capsys.readouterr().out)
    assert rc_over == 1
    assert payload_over["publish_ok"] is False
    assert payload_over["sources"]["hk_history"]["ok"] is False
    assert payload_over["failure_summary"]
    assert "3%" in payload_over["failure_summary"]

    # Case B: 1/120 (0.83% <= 3%) BUT failed code 00001 is held -> main() MUST exit 1 with failure_summary!
    st_held, out_held = _build_hk_daily_entry_harness(
        tmp_path / "held_fail",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_fail_codes={"00001"},
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_held), "--output-dir", str(out_held)])
    rc_held = hkmom_run.main()
    payload_held = json.loads(capsys.readouterr().out)
    assert rc_held == 1
    assert payload_held["publish_ok"] is False
    assert payload_held["sources"]["hk_history"]["ok"] is False
    assert payload_held["failure_summary"]
    assert "00001" in payload_held["failure_summary"]

    # Case C: 2/120 (1.67% <= 3%), none held -> main() exits 0 and rendered page keeps failure count!
    st_ok, out_ok = _build_hk_daily_entry_harness(
        tmp_path / "tolerable",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_fail_codes={"00119", "00120"},
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_ok), "--output-dir", str(out_ok)])
    rc_ok = hkmom_run.main()
    payload_ok = json.loads(capsys.readouterr().out)
    assert rc_ok == 0
    assert payload_ok["publish_ok"] is True
    assert payload_ok["sources"]["hk_history"]["ok"] is True
    assert payload_ok["sources"]["hk_history"]["counts"]["failed"] == 2
    html_ok = (out_ok / "index.html").read_text(encoding="utf-8")
    assert "2/120" in html_ok


def test_hkmom_credible_suspension_vs_missing_bar_full_daily_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Item 3 + Round 3 Row 3:
    - Real suspension with same-day zero-vol flat spot OR explicit status -> succeeds & freezes.
    - Normal trading stock with dropped daily bar -> fails (exit 1).
    - Empty volume fields (not 0) with price==prev_close -> NOT suspended -> fails (exit 1).
    - Old-date zero-vol quote without explicit suspended status -> NOT suspended -> fails (exit 1).
    - Multi-day catch-up window: earlier missing bar CANNOT be exempted by today's suspended spot."""
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])

    # Subcase A1: Real suspension — same-day zero-vol flat quote (quote date == target 2026/11/26)
    st_susp, out_susp = _build_hk_daily_entry_harness(
        tmp_path / "real_susp_same_day_zero_vol",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.0",
                "prev_close": "15.0",
                "volume": "0",
                "turnover": "0",
                "pct": "0.00",
                "ts": "2026/11/26 16:08:00",
            }
        },
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_susp), "--output-dir", str(out_susp)])
    rc_susp = hkmom_run.main()
    payload_susp = json.loads(capsys.readouterr().out)
    assert rc_susp == 0
    assert payload_susp["ok"] is True
    assert payload_susp["publish_ok"] is True
    assert payload_susp["froze"] is True
    assert payload_susp["sources"]["hk_spot"]["ok"] is True
    assert payload_susp["sources"]["hk_history"]["ok"] is True
    assert payload_susp["sources"]["index_compute"]["ok"] is True
    assert "停牌，沿用 2026-11-25 日收盘价" in (out_susp / "index.html").read_text(encoding="utf-8")

    # Subcase A2: Real suspension — explicit status 'S' with quote_date == target day 2026/11/26
    st_explicit, out_explicit = _build_hk_daily_entry_harness(
        tmp_path / "real_susp_explicit_status",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.0",
                "prev_close": "15.0",
                "volume": "",
                "turnover": "",
                "status_40": "S",
                "pct": "0.00",
                "ts": "2026/11/26 16:08:00",
            }
        },
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_explicit), "--output-dir", str(out_explicit)])
    rc_explicit = hkmom_run.main()
    payload_explicit = json.loads(capsys.readouterr().out)
    assert rc_explicit == 0
    assert payload_explicit["publish_ok"] is True
    assert payload_explicit["froze"] is True

    # Subcase A3: Explicit status 'S' with OLD quote date (2026/11/25 != target 2026/11/26) -> MUST FAIL!
    st_old_explicit, out_old_explicit = _build_hk_daily_entry_harness(
        tmp_path / "old_quote_date_explicit_status_not_suspended",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.0",
                "prev_close": "15.0",
                "volume": "0",
                "turnover": "0",
                "status_40": "S",
                "pct": "0.00",
                "ts": "2026/11/25 16:08:00",
            }
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["run.py", "--state-dir", str(st_old_explicit), "--output-dir", str(out_old_explicit)]
    )
    rc_old_explicit = hkmom_run.main()
    payload_old_explicit = json.loads(capsys.readouterr().out)
    assert rc_old_explicit == 1
    assert payload_old_explicit["publish_ok"] is False
    assert payload_old_explicit["froze"] is False
    assert payload_old_explicit["sources"]["hk_spot"]["ok"] is False
    assert payload_old_explicit["sources"]["index_compute"]["ok"] is False

    # Subcase B: Missing daily bar for a normally trading held stock -> MUST fail!
    st_drop, out_drop = _build_hk_daily_entry_harness(
        tmp_path / "missing_bar",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.2",
                "prev_close": "14.8",
                "volume": "500000",
                "turnover": "7600000",
                "pct": "2.70",
                "ts": "2026/11/26 16:08:00",
            }
        },
    )
    frozen_before = json.loads((st_drop / "frozen_history.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_drop), "--output-dir", str(out_drop)])
    rc_drop = hkmom_run.main()
    payload_drop = json.loads(capsys.readouterr().out)
    assert rc_drop == 1
    assert payload_drop["publish_ok"] is False
    assert payload_drop["froze"] is False
    assert payload_drop["sources"]["index_compute"]["ok"] is False
    assert "00001" in (payload_drop["failure_summary"] or "")
    assert json.loads((st_drop / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before

    # Subcase C: Empty volume/turnover fields with price == prev_close -> volume stays None (NOT 0!) -> MUST FAIL!
    st_empty_vol, out_empty_vol = _build_hk_daily_entry_harness(
        tmp_path / "empty_volume_not_suspended",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.0",
                "prev_close": "15.0",
                "vol_6": "",
                "vol_36": "",
                "turnover": "",
                "pct": "0.00",
                "ts": "2026/11/26 16:08:00",
            }
        },
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_empty_vol), "--output-dir", str(out_empty_vol)])
    rc_empty_vol = hkmom_run.main()
    payload_empty_vol = json.loads(capsys.readouterr().out)
    assert rc_empty_vol == 1
    assert payload_empty_vol["publish_ok"] is False
    assert payload_empty_vol["froze"] is False
    assert payload_empty_vol["sources"]["hk_history"]["ok"] is False
    assert payload_empty_vol["sources"]["index_compute"]["ok"] is False

    # Subcase D: Old-date (2026/11/25) zero-vol flat quote WITHOUT explicit status -> MUST FAIL!
    st_old_q, out_old_q = _build_hk_daily_entry_harness(
        tmp_path / "old_quote_date_not_suspended",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_overrides={
            "00001": {
                "price": "15.0",
                "prev_close": "15.0",
                "volume": "0",
                "turnover": "0",
                "pct": "0.00",
                "ts": "2026/11/25 16:08:00",
            }
        },
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_old_q), "--output-dir", str(out_old_q)])
    rc_old_q = hkmom_run.main()
    payload_old_q = json.loads(capsys.readouterr().out)
    assert rc_old_q == 1
    assert payload_old_q["publish_ok"] is False
    assert payload_old_q["froze"] is False
    assert payload_old_q["sources"]["hk_spot"]["ok"] is False

    # Subcase E: Multi-day catch-up window (frozen at 11-24, target 11-26): missing bar on earlier day 11-25
    # CANNOT be exempted by today's (11-26) suspended spot status!
    st_catchup = tmp_path / "catchup_earlier_missing"
    _, hsi_sess_cu = _seed_hk_panel_with_00626(st_catchup, sessions, suspend_days=2)
    # Move frozen_history back one day to 2026-11-24 (before suspension) so 2026-11-25 is an earlier day in catch-up tail
    f_doc = json.loads((st_catchup / "frozen_history.json").read_text(encoding="utf-8"))
    f_doc.pop("suspended_on_last_date", None)
    f_doc.pop("suspended_by_date", None)
    f_doc["frozen_series"]["dates"] = f_doc["frozen_series"]["dates"][:-1]
    f_doc["frozen_series"]["smallcap"] = f_doc["frozen_series"]["smallcap"][:-1]
    f_doc["frozen_series"]["nonggt"] = f_doc["frozen_series"]["nonggt"][:-1]
    f_doc["frozen_series"]["hsi"] = f_doc["frozen_series"]["hsi"][:-1]
    (st_catchup / "frozen_history.json").write_text(json.dumps(f_doc), encoding="utf-8")
    assert f_doc["frozen_series"]["dates"][-1] == "2026-11-24"
    res_cu = hkmom_compute.compute_all(
        state_dir=st_catchup, as_of_date=date(2026, 11, 26), hsi_session=hsi_sess_cu
    )
    assert res_cu["meta"]["ok"] is False
    assert res_cu["can_freeze"] is False
    assert "00626" in res_cu["meta"]["missing_held_by_index"]["smallcap"]
    assert res_cu["series"]["dates"][-1] == "2026-11-24"


def test_pm_freshness_target_trade_date_strict_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4: Strict target-trade-date freshness for US, CSI 300 primary->fallback, SW hist, and SW daily val."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()

    # 1) US Thanksgiving (2026-11-26, target=2026-11-25) response ending only at 2026-11-24 -> MUST FAIL!
    xnys = xcals.get_calendar("XNYS")
    sessions_stale_us = pd.DatetimeIndex(xnys.sessions_in_range("2026-06-01", "2026-11-24"))
    us_df_stale = _make_us_price_frame(cfg, sessions_stale_us)
    fake_yf_stale = SimpleNamespace(download=create_autospec(yf.download, return_value=us_df_stale))
    res_us_stale = pm_collect.fetch_us(
        cfg,
        raw_dir=tmp_path / "us_stale_tg",
        yf_module=fake_yf_stale,
        now=datetime(2026, 11, 26, 17, 15, 0, tzinfo=feeds_common.US_TZ),
    )
    assert res_us_stale["ok"] is False
    assert res_us_stale["stale"] is True
    assert res_us_stale["target_trade_date"] == "2026-11-25"
    assert res_us_stale["last_date"] == "2026-11-24"
    assert len(res_us_stale["stale_tickers"]) == len(pm_collect.us_tickers(cfg))
    assert "2026-11-25" in (res_us_stale["error"] or "")

    # 2) CSI 300 primary (tx) returns non-empty data only up to 11-25 while target is 11-26 -> switches to bench_em!
    xshg = xcals.get_calendar("XSHG")
    dates_26 = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-26"))
    dates_25 = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-25"))
    fake_ak_fb = _make_cn_fake_ak(dates_26)
    lag_df = pd.DataFrame(
        {"date": dates_25.strftime("%Y-%m-%d"), "close": np.linspace(3500.0, 4050.0, len(dates_25))}
    )

    def _lag_tx(symbol: str) -> pd.DataFrame:
        return lag_df.copy()

    def _lag_em(symbol: str) -> pd.DataFrame:
        return lag_df.copy()

    fake_ak_fb.stock_zh_index_daily_tx.side_effect = _lag_tx
    res_fb = pm_collect.fetch_cn(
        cfg,
        raw_dir=tmp_path / "cn_bench_fb",
        ak_module=fake_ak_fb,
        as_of_date=date(2026, 11, 26),
    )
    assert res_fb["ok"] is True
    assert res_fb["stale"] is False
    assert res_fb["bench_source"] == "bench_em"
    assert res_fb["last_date"] == "2026-11-26"
    assert fake_ak_fb.stock_zh_index_daily_tx.call_count == 1
    assert fake_ak_fb.stock_zh_index_daily_em.call_count == 1

    # 3) Both bench_tx AND bench_em lag at 11-25 while industries reach 11-26 -> cn MUST FAIL!
    fake_ak_both_lag = _make_cn_fake_ak(dates_26)
    fake_ak_both_lag.stock_zh_index_daily_tx.side_effect = _lag_tx
    fake_ak_both_lag.stock_zh_index_daily_em.side_effect = _lag_em
    res_both_lag = pm_collect.fetch_cn(
        cfg,
        raw_dir=tmp_path / "cn_bench_both_lag",
        ak_module=fake_ak_both_lag,
        as_of_date=date(2026, 11, 26),
    )
    assert res_both_lag["ok"] is False
    assert res_both_lag["stale"] is True
    assert "滞后" in (res_both_lag["error"] or "")

    # 4) Single SW industry series (801081) lags at 11-25 while all others reach 11-26 -> cn MUST FAIL!
    fake_ak_ind_lag = _make_cn_fake_ak(dates_26)
    orig_hist = fake_ak_ind_lag.index_hist_sw.side_effect

    def _hist_lag_801081(symbol: str, period: str = "day") -> pd.DataFrame:
        df = orig_hist(symbol, period=period)
        if symbol == "801081":
            return df.iloc[:-1].copy()
        return df

    fake_ak_ind_lag.index_hist_sw.side_effect = _hist_lag_801081
    res_ind_lag = pm_collect.fetch_cn(
        cfg,
        raw_dir=tmp_path / "cn_ind_lag",
        ak_module=fake_ak_ind_lag,
        as_of_date=date(2026, 11, 26),
    )
    assert res_ind_lag["ok"] is False
    assert res_ind_lag["stale"] is True
    assert "801081" in res_ind_lag["stale_hist_codes"]
    assert "801081" in (res_ind_lag["error"] or "")

    # 5) Daily SW valuation missing 801081 on target 11-26 (present only on 11-25 inside 12d window) -> MUST FAIL!
    raw_val = tmp_path / "cn_val_lag"
    fake_ak_seed = _make_cn_fake_ak(dates_26)
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_val, ak_module=fake_ak_seed, as_of_date=date(2026, 11, 26))["ok"] is True

    def _daily_val_stale_801081(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        if symbol == "一级行业":
            return pd.DataFrame(
                [
                    {"指数代码": "801010.SI", "发布日期": "2026-11-26", "市盈率": 21.0, "市净率": 2.2, "股息率": 1.4},
                    {"指数代码": "801080.SI", "发布日期": "2026-11-26", "市盈率": 45.0, "市净率": 3.5, "股息率": 0.8},
                ]
            )
        return pd.DataFrame(
            [
                {"指数代码": "801081.SI", "发布日期": "2026-11-25", "市盈率": 62.0, "市净率": 4.1, "股息率": 0.4},
                {"指数代码": "801012.SI", "发布日期": "2026-11-26", "市盈率": 25.0, "市净率": 1.9, "股息率": 1.2},
            ]
        )

    fake_ak_val_lag = _make_cn_fake_ak(dates_26, daily_val_fn=_daily_val_stale_801081)
    with pytest.raises(RuntimeError, match="801081"):
        pm_collect.fetch_cn_val_latest(raw_dir=raw_val, ak_module=fake_ak_val_lag, as_of_date=date(2026, 11, 26))


def test_pm_cn_valuation_sw_start_date_metadata_and_4week_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5 + Round 3 Row 6: SW inception dates come from sw_index_inception.json using REAL
    AkShare 1.19.1 column structure (no fabricated '发布日期' column); codes absent from the
    table fall back to the 4-week grace window after valuation_start."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()
    xshg = xcals.get_calendar("XSHG")
    dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-25"))

    # Verify bundled sw_index_inception.json covers all 134 SW2021 L2 industries and documents source
    inc_doc = json.loads(pm_collect.SW_INCEPTION_PATH.read_text(encoding="utf-8"))
    assert inc_doc.get("_source") and inc_doc.get("_comment")
    sw3_doc = json.loads(
        (feeds_common.REPO_ROOT / "scripts" / "dalton" / "monitor_feeds" / "ashare_monitor" / "sw3_mapping.json").read_text(
            encoding="utf-8"
        )
    )
    sw3_l2_names = {v["parent"] for v in sw3_doc["industries"].values()}
    assert sw3_l2_names.issubset(set(inc_doc["by_name"].keys()))

    # Case 1: Real AkShare 1.19.1 columns (NO '发布日期' column!):
    # 801081 (半导体) is in sw_index_inception.json (1999-12-30) -> fetch_cn persists publish_date,
    # so missing 801081 on 2021-12-31 MUST FAIL even inside the first 4 weeks!
    l2_real_cols = [
        {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "静态市盈率": 62.0, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
        {"行业代码": "801193.SI", "行业名称": "证券Ⅱ", "上级行业": "非银金融", "成份个数": 50, "静态市盈率": 21.0, "TTM(滚动)市盈率": 22.0, "市净率": 1.6, "静态股息率": 1.8},
    ]

    def _weekly_drop_801081_first_week(symbol: str, date_str: str) -> pd.DataFrame:
        pub_dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if symbol == "一级行业":
            codes = ["801010", "801080"]
        else:
            codes = ["801193"] if date_str == "20211231" else ["801081", "801193"]
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.0, "股息率": 1.5} for c in codes]
        )

    raw_meta = tmp_path / "val_inception_table"
    fake_ak_meta = _make_cn_fake_ak(
        dates,
        l2_rows=l2_real_cols,
        weekly_fn=_weekly_drop_801081_first_week,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_meta, ak_module=fake_ak_meta, as_of_date=date(2026, 11, 25))["ok"] is True
    saved_meta = json.loads((raw_meta / "cn_sw_meta.json").read_text(encoding="utf-8"))
    assert saved_meta["industries"]["801081"]["publish_date"] == "1999-12-30"
    assert saved_meta["industries"]["801193"]["publish_date"] == "2022-01-14"

    res_meta_fail = pm_collect.fetch_cn_valuation(
        cfg, raw_dir=raw_meta, ak_module=fake_ak_meta, as_of_date=date(2026, 11, 25)
    )
    assert res_meta_fail["ok"] is False
    assert any("2021-12-31" in e and "801081" in e for e in res_meta_fail["errors"])

    # Case 2: Inception table specifies an industry created AFTER the 4-week grace window (2022-06-01):
    # absence before 2022-06-01 (e.g. 2022-05-27) passes cleanly, while presence from 2022-06-02 onward passes!
    custom_inc_path = tmp_path / "custom_inception.json"
    custom_inc_path.write_text(
        json.dumps(
            {
                "by_code": {
                    "801010": {"name": "农林牧渔", "level": 1, "inception_date": "1999-12-30"},
                    "801080": {"name": "电子", "level": 1, "inception_date": "1999-12-30"},
                    "801081": {"name": "半导体", "level": 2, "inception_date": "1999-12-30"},
                    "801995": {"name": "新设测试行业", "level": 2, "inception_date": "2022-06-01"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_SW_INCEPTION_PATH", str(custom_inc_path))
    l2_new_ind = [
        {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "静态市盈率": 62.0, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
        {"行业代码": "801995.SI", "行业名称": "新设测试行业", "上级行业": "电子", "成份个数": 12, "静态市盈率": 30.0, "TTM(滚动)市盈率": 32.0, "市净率": 2.5, "静态股息率": 0.9},
    ]

    def _weekly_post_grace_inception(symbol: str, date_str: str) -> pd.DataFrame:
        pub_dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if symbol == "一级行业":
            codes = ["801010", "801080"]
        else:
            codes = ["801081"] if date_str < "20220601" else ["801081", "801995"]
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.0, "股息率": 1.5} for c in codes]
        )

    raw_post_grace = tmp_path / "val_post_grace_inception"
    fake_ak_pg = _make_cn_fake_ak(
        dates,
        l2_rows=l2_new_ind,
        weekly_fn=_weekly_post_grace_inception,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_post_grace, ak_module=fake_ak_pg, as_of_date=date(2026, 11, 25))["ok"] is True
    res_pg_ok = pm_collect.fetch_cn_valuation(
        cfg, raw_dir=raw_post_grace, ak_module=fake_ak_pg, as_of_date=date(2026, 11, 25)
    )
    assert res_pg_ok["ok"] is True
    monkeypatch.delenv("DALTON_SW_INCEPTION_PATH", raising=False)

    # Case 3: Unknown code NOT in inception table -> missing after 4-week grace (2022-02-11 > 2022-01-28) MUST FAIL!
    l2_unknown = [
        {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "静态市盈率": 62.0, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
        {"行业代码": "801998.SI", "行业名称": "未建表行业", "上级行业": "电子", "成份个数": 10, "静态市盈率": 20.0, "TTM(滚动)市盈率": 21.0, "市净率": 1.6, "静态股息率": 1.2},
    ]

    def _weekly_late_first_seen(symbol: str, date_str: str) -> pd.DataFrame:
        pub_dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if symbol == "一级行业":
            codes = ["801010", "801080"]
        else:
            codes = ["801081"] if date_str in ("20211231", "20220211") else ["801081", "801998"]
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": pub_dt, "市盈率": 20.0, "市净率": 2.0, "股息率": 1.5} for c in codes]
        )

    raw_grace = tmp_path / "val_no_meta_grace"
    fake_ak_grace = _make_cn_fake_ak(
        dates,
        l2_rows=l2_unknown,
        weekly_fn=_weekly_late_first_seen,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_grace, ak_module=fake_ak_grace, as_of_date=date(2026, 11, 25))["ok"] is True
    res_grace_fail = pm_collect.fetch_cn_valuation(
        cfg, raw_dir=raw_grace, ak_module=fake_ak_grace, as_of_date=date(2026, 11, 25)
    )
    assert res_grace_fail["ok"] is False
    assert any("2022-02-11" in e and "801998" in e for e in res_grace_fail["errors"])


def test_calendar_oob_2026_new_years_eve_fallback_and_hk_typhoon_override_and_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 6 + Round 3 Row 7 + zh-CN copy:
    - Statutory holidays through end of 2027 (CN/HK/US) in market_holidays.json cover 2027-01-01, etc.
    - Adjacent cross-year schedules (US 2026-12-31 17:15 and US 2027-01-01 17:15) BOTH resolve
      CN & US as_of=2026-12-31 without mistakenly picking 2027-01-01.
    - Non-holiday OOB weekday (2027-01-04 Mon for XSHG) falls back to weekday with note.
    - Late-2027 OOB statutory holidays (US 2027-12-24, HK 2027-12-27) & HK Typhoon Saola 2023-09-01
      are marked holiday_override.
    - zh-CN monitor locale says '最近一次更新未生效'."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()

    # 1) 2027-01-01 is New Year's Day holiday for CN, HK, US via market_holidays.json
    for mk in ("cn", "hk", "us"):
        is_td_ny, src_ny, _note_ny = feeds_common.inspect_trading_day(mk, date(2027, 1, 1))
        assert is_td_ny is False
        assert src_ny == "holiday_override"

    # First 2027 trading weekday (2027-01-04 Mon) is OOB for XSHG -> weekday_fallback with note
    is_td_jan4, src_jan4, note_jan4 = feeds_common.inspect_trading_day("cn", date(2027, 1, 4))
    assert is_td_jan4 is True
    assert src_jan4 == "weekday_fallback"
    assert note_jan4 is not None and "2027-01-04" in note_jan4 and "兜底" in note_jan4
    assert feeds_common.is_trading_day("cn", date(2027, 1, 2)) is False  # Saturday OOB

    # Late-2027 OOB statutory holidays (beyond XHKG/XNYS 2027-11-26) & CN 2027 holidays
    assert feeds_common.inspect_trading_day("us", date(2027, 12, 24))[:2] == (False, "holiday_override")
    assert feeds_common.inspect_trading_day("hk", date(2027, 12, 27))[:2] == (False, "holiday_override")
    assert feeds_common.inspect_trading_day("cn", date(2027, 2, 5))[:2] == (False, "holiday_override")
    assert feeds_common.inspect_trading_day("cn", date(2027, 10, 1))[:2] == (False, "holiday_override")

    # 2) Adjacent cross-year schedules:
    # Schedule 1: US Eastern 2026-12-31 17:15 (Beijing 2027-01-01 06:15)
    # Schedule 2: US Eastern 2027-01-01 17:15 (Beijing 2027-01-02 06:15)
    ny_eve_us = datetime(2026, 12, 31, 17, 15, 0, tzinfo=feeds_common.US_TZ)
    ny_day_us = datetime(2027, 1, 1, 17, 15, 0, tzinfo=feeds_common.US_TZ)
    assert feeds_common.resolve_market_trade_date("us", now=ny_eve_us) == date(2026, 12, 31)
    assert feeds_common.resolve_market_trade_date("cn", now=ny_eve_us) == date(2026, 12, 31)
    assert feeds_common.resolve_market_trade_date("us", now=ny_day_us) == date(2026, 12, 31)
    assert feeds_common.resolve_market_trade_date("cn", now=ny_day_us) == date(2026, 12, 31)

    xshg = xcals.get_calendar("XSHG")
    cn_dates_eoy = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-12-31"))
    for tag, sched_dt in (("sched1_eoy", ny_eve_us), ("sched2_nyd", ny_day_us)):
        fake_ak_eoy = _make_cn_fake_ak(cn_dates_eoy)
        raw_eoy = tmp_path / f"cn_{tag}"
        res_cn_eoy = pm_collect.fetch_cn(cfg, raw_dir=raw_eoy, ak_module=fake_ak_eoy, now=sched_dt)
        assert res_cn_eoy["ok"] is True
        assert res_cn_eoy["target_trade_date"] == "2026-12-31"
        assert res_cn_eoy["last_date"] == "2026-12-31"
        assert res_cn_eoy["source_status"]["as_of"] == "2026-12-31"

        res_val_eoy = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_eoy, ak_module=fake_ak_eoy, now=sched_dt)
        assert res_val_eoy["ok"] is True
        assert res_val_eoy["latest"]["as_of"] == "2026-12-31"
        assert res_val_eoy["source_status"]["as_of"] == "2026-12-31"

    # Also verify OOB weekday_fallback note surfaces on 2027-01-04
    cn_dates_jan4 = cn_dates_eoy.append(pd.DatetimeIndex(["2027-01-04"]))
    fake_ak_jan4 = _make_cn_fake_ak(cn_dates_jan4)
    raw_jan4 = tmp_path / "cn_jan4_2027"
    jan4_cn = datetime(2027, 1, 4, 16, 0, 0, tzinfo=feeds_common.CN_TZ)
    res_cn_jan4 = pm_collect.fetch_cn(cfg, raw_dir=raw_jan4, ak_module=fake_ak_jan4, now=jan4_cn)
    assert res_cn_jan4["ok"] is True
    assert res_cn_jan4["target_trade_date"] == "2027-01-04"
    assert "兜底" in (res_cn_jan4["source_status"].get("note") or "")

    # 3) Temporary closure table: HK 2023-09-01 (Typhoon Saola) is full-day holiday
    assert feeds_common.is_trading_day("hk", date(2023, 9, 1)) is False
    assert feeds_common.resolve_market_trade_date("hk", as_of_date=date(2023, 9, 1)) == date(2023, 8, 31)
    hk_typhoon_sess = feeds_common.market_session_state(
        "hk", datetime(2023, 9, 1, 14, 0, 0, tzinfo=feeds_common.HK_TZ)
    )
    assert hk_typhoon_sess["status"] == "holiday"
    assert hk_typhoon_sess["is_trading_day"] is False
    assert hk_typhoon_sess["session_source"] == "holiday_override"
    assert feeds_common.trading_days_between("hk", "2023-08-31", "2023-09-04") == 1

    # 4) Monitor zh-CN copy says '最近一次更新未生效'
    zh_copy = json.loads(
        (feeds_common.REPO_ROOT / "web" / "src" / "locales" / "monitors" / "zh-CN.json").read_text(
            encoding="utf-8"
        )
    )
    assert "最近一次更新未生效" in zh_copy["lastPublishFailed"]
    assert "最近一次更新失败" not in zh_copy["lastPublishFailed"]


# ---------------------------------------------------------------------------
# Round 3 Fixes (Rows 1, 2, 4, 5 dedicated regression tests)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fail_kind", ["ggt_list", "hkex_universe", "hk_spot"])
def test_hkmom_non_month_end_daily_required_sources_align_persist_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_kind: str,
) -> None:
    """Round 3 Row 1: On a non-month-end trading day, failing ggt_list / hkex_universe / hk_spot
    blocks BOTH persistence AND exit code 0 (same required source list)."""
    xhkg = xcals.get_calendar("XHKG")
    # 2026-11-26 is non-month-end (Nov month-end is 2026-11-30)
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])

    st_dir, out_dir = _build_hk_daily_entry_harness(
        tmp_path / f"fail_{fail_kind}",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        ggt_fail=(fail_kind == "ggt_list"),
        hkex_fail=(fail_kind == "hkex_universe"),
        spot_fail=(fail_kind == "hk_spot"),
    )
    frozen_before = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    rc = hkmom_run.main()
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["publish_ok"] is False
    assert payload["froze"] is False
    assert payload["sources"][fail_kind]["ok"] is False
    assert payload["failure_summary"]
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before


def test_hkmom_history_lagging_bars_counted_in_fail_ratio_and_held_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round 3 Row 2: Per-stock history last bar must equal target trade date; lagging bars
    without credible suspension count toward failure ratio (>3% fails) and held check."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", create_autospec(_REAL_SLEEP, return_value=None))

    # Case 1: 100 stocks ALL return non-empty qfqday ending 2026-11-25 while target is 2026-11-26 -> 100/100 fail!
    codes_100 = [f"{i:05d}" for i in range(1, 101)]

    def _lag_all_get(url: str, **_kw):
        code = url.split("param=hk")[-1].split(",")[0]
        resp = create_autospec(requests.Response, instance=True)
        resp.json.return_value = {
            "data": {
                f"hk{code}": {
                    "qfqday": [
                        ["2026-11-24", "10.0", "10.1", "10.2", "9.9", "1000", "100.0"],
                        ["2026-11-25", "10.1", "10.2", "10.3", "10.0", "1000", "100.0"],
                    ]
                }
            }
        }
        return resp

    sess_lag_all = create_autospec(requests.Session, instance=True)
    sess_lag_all.get.side_effect = _lag_all_get
    prog_all = hkmom_fetch.download_history(
        codes_100,
        state_dir=tmp_path / "lag_all_100",
        workers=4,
        session=sess_lag_all,
        target_date="2026-11-26",
    )
    assert prog_all["n_errors"] == 100
    assert prog_all["stale"] is True
    assert prog_all["source_status"]["ok"] is False

    # Case 2: Full daily entry — >3% (5/120) non-held stocks return lagging bars (11-25)
    # while all 50 held stocks and HSI reach 11-26 -> MUST fail hk_history and exit 1!
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    codes_120 = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes_120[55:])
    st_over, out_over = _build_hk_daily_entry_harness(
        tmp_path / "lag_over_3pct_non_held",
        monkeypatch,
        sessions=sessions,
        codes=codes_120,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00110", "00111", "00112", "00113", "00114"},
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_over), "--output-dir", str(out_over)])
    rc_over = hkmom_run.main()
    payload_over = json.loads(capsys.readouterr().out)
    assert rc_over == 1
    assert payload_over["publish_ok"] is False
    assert payload_over["froze"] is False
    assert payload_over["sources"]["hk_history"]["ok"] is False
    assert payload_over["sources"]["hk_history"]["counts"]["failed"] == 5

    # Case 3: Held stock lagging without suspension -> fails held check; with credible suspension -> passes!
    st_held_lag = tmp_path / "lag_held_vs_susp"
    prog_held_fail = hkmom_fetch.download_history(
        ["00001"],
        state_dir=st_held_lag / "no_susp",
        workers=1,
        session=sess_lag_all,
        target_date="2026-11-26",
        held_codes={"00001"},
    )
    assert prog_held_fail["stale"] is True
    assert prog_held_fail["failed_held"] == ["00001"]

    prog_held_susp = hkmom_fetch.download_history(
        ["00001"],
        state_dir=st_held_lag / "with_susp",
        workers=1,
        session=sess_lag_all,
        target_date="2026-11-26",
        held_codes={"00001"},
        spot_info={
            "00001": {
                "price": 10.2,
                "prev_close": 10.2,
                "volume": 0.0,
                "turnover": 0.0,
                "quote_date": "2026-11-26",
            }
        },
    )
    assert prog_held_susp["n_errors"] == 0
    assert prog_held_susp["stale"] is False
    assert prog_held_susp["source_status"]["ok"] is True


def test_hkmom_long_suspension_removal_persists_across_daily_resume_and_month_end(
    tmp_path: Path,
) -> None:
    """Round 3 Row 4: Long-term suspension (>20d) persists per-index effective_date across:
    1) Removal day (21st suspended day) -> 2) Next continuous daily run (still suspended)
    -> 3) Resume day (valid bar returns; stays out of current basket, anchor does not block)
    -> 4) Month-end rebalance (outgoing basket return excludes removed stock; resumed stock
       is re-included into new rebalance basket under normal rules) -> 5) Post-rebalance day."""
    xhkg = xcals.get_calendar("XHKG")
    # 2026-12-03 = day 20, 2026-12-04 = day 21 (removal), 2026-12-07 = continuous daily,
    # 2026-12-08 = resume day, ..., 2026-12-31 = month-end rebalance, 2027-01-04 = next session.
    all_sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2027-01-04")]
    idx_d20 = all_sessions.index("2026-12-03")
    idx_d21 = all_sessions.index("2026-12-04")
    idx_d22 = all_sessions.index("2026-12-07")
    idx_resume = all_sessions.index("2026-12-08")
    idx_me = all_sessions.index("2026-12-31")
    idx_jan = all_sessions.index("2027-01-04")

    st_dir = tmp_path / "hk_long_susp_lifecycle"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    codes = ["00626"] + [f"{i:05d}" for i in range(1, 60)]
    last_bar_before_susp_idx = idx_d21 - 21  # exactly 21 trading days before 2026-12-04

    # Pre-generate deterministic price series for all codes across all_sessions.
    # Give 00626 strong low-volatility upward trend prior to halt + clear +12% on resume day + clear +15% on 12-31
    # so we can assert both: (a) 00626 has ZERO effect on outgoing basket returns on resume & month-end,
    # and (b) 00626 ranks high enough in risk-adjusted momentum to be re-selected into 2026-12-31!
    full_bars: dict[str, list[list[Any]]] = {}
    for idx, c in enumerate(codes):
        rows: list[list[Any]] = []
        for t_i, dt_str in enumerate(all_sessions):
            if c == "00626":
                if last_bar_before_susp_idx < t_i < idx_resume:
                    continue  # suspended from day 1 through day 22 (inclusive)
                if t_i <= last_bar_before_susp_idx:
                    px = round(10.0 * (1.0 + 0.005 * t_i) * (1.0 + 0.004 * math.sin(t_i * 0.7)), 4)
                elif t_i < idx_me:
                    # Resumes at +12% on 2026-12-08 and trends smoothly through 2026-12-30
                    px = round(rows[-1][2] * (1.12 if t_i == idx_resume else 1.003), 4)
                elif t_i == idx_me:
                    # Jumps +15% on month-end 2026-12-31 (if wrongly included in outgoing 50-stock basket,
                    # would add +0.28% to 12-31 return, far outside 5e-4 tolerance!)
                    px = round(rows[-1][2] * 1.15, 4)
                else:
                    # +10% on 2027-01-04 (first session of new rebalance)
                    px = round(rows[-1][2] * 1.10, 4)
            else:
                # Non-00626 stocks move cleanly (+1% on 12-31 so outgoing return is exactly +1%!)
                if t_i == idx_me:
                    px = round(rows[-1][2] * 1.01, 4)
                elif t_i == idx_jan:
                    px = round(rows[-1][2] * 1.00, 4)
                else:
                    px = round(
                        10.0 * (1.0 + 0.0004 * t_i * (1 + (idx % 5) * 0.1)) * (1.0 + 0.015 * math.sin(t_i * 0.7 + idx)),
                        4,
                    )
            rows.append([dt_str, px, px, round(px * 1.01, 4), round(px * 0.99, 4), 100000, 250.0])
        full_bars[c] = rows

    def _write_state_for_day(day_idx: int, *, susp_00626: bool) -> requests.Session:
        day_str = all_sessions[day_idx]
        uni_rows = []
        for idx, c in enumerate(codes):
            c_rows = [r for r in full_bars[c] if r[0] <= day_str]
            pd.DataFrame(
                c_rows,
                columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
            ).to_csv(hist_dir / f"{c}.csv", index=False)
            last_px = c_rows[-1][2]
            is_susp = c == "00626" and susp_00626
            uni_rows.append(
                {
                    "code": c,
                    "name": "PUBLIC FIN HOLD" if c == "00626" else f"HK{c}",
                    "subcat": "Ordinary Shares",
                    "ccy": "HKD",
                    "is_ggt": idx >= 52,
                    "price": last_px,
                    "prev_close": last_px if is_susp else round(last_px * 0.99, 4),
                    "volume": 0.0 if is_susp else 500000.0,
                    "turnover": 0.0 if is_susp else 2000000.0,
                    "trading_status": "S" if is_susp else "",
                    "quote_date": day_str,
                    "mktcap_e8": 25.0,
                    "name_cn": "大众金融控股" if c == "00626" else f"港股{c}",
                }
            )
        pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
        (st_dir / "ggt_list.json").write_text(
            json.dumps({c: f"港股{c}" for c in codes[52:]}), encoding="utf-8"
        )
        hsi_resp = create_autospec(requests.Response, instance=True)
        hsi_resp.json.return_value = {
            "data": {
                "hkHSI": {
                    "day": [[d, 20000.0, 20000.0 + i] for i, d in enumerate(all_sessions[: day_idx + 1])]
                }
            }
        }
        sess = create_autospec(requests.Session, instance=True)
        sess.get.return_value = hsi_resp
        return sess

    # Seed frozen state through 2026-12-03 (day 20 of 00626 suspension)
    held_init = codes[:50]
    mes_pre = hkmom_compute.month_ends(all_sessions[: idx_d20 + 1], as_of_date=date(2026, 12, 3))
    rebal_dates_pre = [d for d in mes_pre if all_sessions.index(d) >= 273]
    rebal_start_idx = all_sessions.index(rebal_dates_pre[0])
    frozen_dates_pre = all_sessions[rebal_start_idx : idx_d20 + 1]
    nov_rebal = rebal_dates_pre[-1]
    seed_frozen = {
        "launch_date": rebal_dates_pre[0],
        "rebalance_snapshots": {d: {"smallcap": held_init, "nonggt": held_init} for d in rebal_dates_pre},
        "suspended_on_last_date": ["00626"],
        "suspended_by_date": {
            d_s: ["00626"] for d_s in all_sessions[last_bar_before_susp_idx + 1 : idx_d20 + 1]
        },
        "frozen_series": {
            "dates": frozen_dates_pre,
            "smallcap": [1000.0 + i for i in range(len(frozen_dates_pre))],
            "nonggt": [1000.0 + i for i in range(len(frozen_dates_pre))],
            "hsi": [1000.0 + i for i in range(len(frozen_dates_pre))],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")

    # Step 1: Day 21 (2026-12-04) — 00626 removed effective 2026-12-04 and persisted per index
    sess_d21 = _write_state_for_day(idx_d21, susp_00626=True)
    res_d21 = hkmom_compute.compute_all(state_dir=st_dir, as_of_date=date(2026, 12, 4), hsi_session=sess_d21)
    assert res_d21["meta"]["ok"] is True
    assert res_d21["series"]["dates"][-1] == "2026-12-04"
    assert res_d21["meta"]["removed_suspended_codes"] == ["00626"]
    frozen_d21 = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    for key in ("smallcap", "nonggt"):
        assert frozen_d21["suspended_removals"][key][nov_rebal]["00626"]["effective_date"] == "2026-12-04"
        assert len(res_d21["constituents"][key]) == 49
        assert all(c["code"] != "00626" for c in res_d21["constituents"][key])

    # Step 2: Continuous daily update (2026-12-07, still suspended) — advances cleanly & preserves effective_date
    sess_d22 = _write_state_for_day(idx_d22, susp_00626=True)
    res_d22 = hkmom_compute.compute_all(state_dir=st_dir, as_of_date=date(2026, 12, 7), hsi_session=sess_d22)
    assert res_d22["meta"]["ok"] is True
    assert res_d22["series"]["dates"][-1] == "2026-12-07"
    assert res_d22["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}
    frozen_d22 = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_d22["suspended_removals"]["smallcap"][nov_rebal]["00626"]["effective_date"] == "2026-12-04"

    # Step 3: Resume day (2026-12-08, 00626 resumes with +50% jump & active spot!)
    # MUST NOT get stuck on 2026-12-07 anchor missing bar; 00626 stays out of current basket until next rebalance!
    sess_res = _write_state_for_day(idx_resume, susp_00626=False)
    res_resume = hkmom_compute.compute_all(state_dir=st_dir, as_of_date=date(2026, 12, 8), hsi_session=sess_res)
    assert res_resume["meta"]["ok"] is True
    assert res_resume["series"]["dates"][-1] == "2026-12-08"
    assert res_resume["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}
    for key in ("smallcap", "nonggt"):
        assert len(res_resume["constituents"][key]) == 49
        assert all(c["code"] != "00626" for c in res_resume["constituents"][key])

    # Step 4: Fast-forward daily runs through 2026-12-30, then run Month-End Rebalance 2026-12-31!
    for day_i in range(idx_resume + 1, idx_me):
        sess_mid = _write_state_for_day(day_i, susp_00626=False)
        r_mid = hkmom_compute.compute_all(
            state_dir=st_dir, as_of_date=date.fromisoformat(all_sessions[day_i]), hsi_session=sess_mid
        )
        assert r_mid["meta"]["ok"] is True
        assert r_mid["series"]["dates"][-1] == all_sessions[day_i]

    lvl_before_me = {
        key: json.loads((st_dir / "index_series.json").read_text(encoding="utf-8"))[key][-1]
        for key in ("smallcap", "nonggt")
    }
    sess_me = _write_state_for_day(idx_me, susp_00626=False)
    res_me = hkmom_compute.compute_all(state_dir=st_dir, as_of_date=date(2026, 12, 31), hsi_session=sess_me)
    assert res_me["meta"]["ok"] is True
    assert res_me["series"]["dates"][-1] == "2026-12-31"
    assert res_me["meta"]["last_rebalance_date"] == "2026-12-31"
    # Every non-00626 stock moved +1.00% on 12-31 while 00626 jumped +40%!
    # Because outgoing Nov basket excludes 00626, the index return on 12-31 MUST be +1.00% (NOT ~+1.8%)!
    for key in ("smallcap", "nonggt"):
        me_ret = res_me["series"][key][-1] / lvl_before_me[key] - 1.0
        assert me_ret == pytest.approx(0.01, abs=5e-4)
        # Resumed 00626 is re-included in the NEW 2026-12-31 rebalance basket!
        new_codes = [c["code"] for c in res_me["constituents"][key]]
        assert len(new_codes) == 50
        assert "00626" in new_codes
    assert res_me["meta"]["removed_suspended_codes"] == []
    frozen_me = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_me["suspended_removals"]["smallcap"][nov_rebal]["00626"]["effective_date"] == "2026-12-04"

    # Step 5: Post-rebalance session (2027-01-04): 00626 moves +10%, others 0% -> new basket earns +10%/50 = +0.2%!
    lvl_me = {key: res_me["series"][key][-1] for key in ("smallcap", "nonggt")}
    sess_jan = _write_state_for_day(idx_jan, susp_00626=False)
    res_jan = hkmom_compute.compute_all(state_dir=st_dir, as_of_date=date(2027, 1, 4), hsi_session=sess_jan)
    assert res_jan["meta"]["ok"] is True
    assert res_jan["series"]["dates"][-1] == "2027-01-04"
    for key in ("smallcap", "nonggt"):
        jan_ret = res_jan["series"][key][-1] / lvl_me[key] - 1.0
        assert jan_ret == pytest.approx(0.10 / 50.0, abs=5e-4)


def test_pm_cn_valuation_rejects_wrong_weekly_publish_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 Row 5: Weekly SW valuation verifies actual '发布日期' in response; if requested week
    2026-11-27 returns rows dated 2026-11-20 (while daily valuation is normal), keep pending & fail."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(pm_collect.time.sleep, return_value=None))
    cfg = pm_collect.load_config()
    xshg = xcals.get_calendar("XSHG")
    dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-27"))

    def _weekly_stale_date(symbol: str, date_str: str) -> pd.DataFrame:
        codes = ["801010", "801080"] if symbol == "一级行业" else ["801081", "801012"]
        # Request 20261127 mistakenly returns previous week's 2026-11-20 rows!
        actual_dt = date(2026, 11, 20) if date_str == "20261127" else date.fromisoformat(
            f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        )
        return pd.DataFrame(
            [{"指数代码": f"{c}.SI", "发布日期": actual_dt, "市盈率": 22.0, "市净率": 2.1, "股息率": 1.4} for c in codes]
        )

    raw_dir = tmp_path / "val_wrong_week_date"
    fake_ak = _make_cn_fake_ak(
        dates,
        weekly_fn=_weekly_stale_date,
    )
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_dir, ak_module=fake_ak, as_of_date=date(2026, 11, 27))["ok"] is True
    res_val = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak, as_of_date=date(2026, 11, 27))

    assert res_val["ok"] is False
    assert res_val["stale"] is True
    assert res_val["source_status"]["ok"] is False
    assert res_val["latest"] is not None and res_val["latest"]["ok"] is True
    assert any("2026-11-20" in e and "2026-11-27" in e for e in res_val["errors"])

    prog = json.loads((raw_dir / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    assert set(prog["done"].get("2026-11-20", [])) == {"一级行业", "二级行业"}
    assert prog["done"].get("2026-11-27", []) == []

    saved_weekly = pd.read_csv(raw_dir / "cn_sw_val_weekly.csv", dtype={"code": str})
    assert "2026-11-27" not in set(saved_weekly["date"].astype(str))
    assert "2026-11-20" in set(saved_weekly["date"].astype(str))


# ---------------------------------------------------------------------------
# Round 4 Fixes (Grok r2 items 1-3 regression tests)
# ---------------------------------------------------------------------------


def test_hkmom_degraded_spot_ignores_preserved_csv_suspension_and_fails_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round 4 Item 2:
    1) is_spot_suspended requires quote_date == target_date even for explicit status,
       and never trusts stored boolean `suspended` flags.
    2) When hk_spot degrades and preserves prior universe.csv (containing yesterday's
       suspended=True, trading_status='S', volume=0, price==prev_close), neither
       download_history nor compute_all treats the missing bar as suspended;
       non-month-end main() exits 1 with publish_ok=False and frozen_history intact.
    3) Even if preserved universe.csv happens to carry today's quote_date, preserved
       csv suspension/price/volume columns are ignored."""
    # 1) Direct unit assertions on is_spot_suspended
    assert hkmom_fetch.is_spot_suspended(
        {"suspended": True, "quote_date": "2026-11-26"}, check_date="2026-11-26"
    ) is False
    assert hkmom_fetch.is_spot_suspended(
        {"trading_status": "S", "quote_date": "2026-11-25"}, check_date="2026-11-26"
    ) is False
    assert hkmom_fetch.is_spot_suspended(
        {"trading_status": "S"}, check_date="2026-11-26"
    ) is False
    assert hkmom_fetch.is_spot_suspended(
        {"trading_status": "S", "quote_date": "2026-11-26"}, check_date="2026-11-26"
    ) is True
    assert hkmom_fetch.is_spot_suspended(
        {"price": 15.0, "prev_close": 15.0, "volume": 0.0, "turnover": 0.0, "quote_date": "2026-11-25"},
        check_date="2026-11-26",
    ) is False
    assert hkmom_fetch.is_spot_suspended(
        {"price": 15.0, "prev_close": 15.0, "volume": 0.0, "turnover": 0.0, "quote_date": "2026-11-26"},
        check_date="2026-11-26",
    ) is True

    # 2) Full non-month-end daily run: yesterday's universe.csv marked held stock 00001 suspended;
    #    today hk_spot fails (preserving old universe.csv) and 00001 history has no target-day bar.
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-26")]
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])
    st_dir, out_dir = _build_hk_daily_entry_harness(
        tmp_path / "degraded_spot_preserved_csv",
        monkeypatch,
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        hist_omit_last_bar_codes={"00001"},
        spot_fail=True,
    )
    # Seed yesterday's preserved universe.csv with 00001 marked suspended (bool + status + 0-vol flat)
    uni_df = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    uni_df["prev_close"] = uni_df["price"]
    uni_df["volume"] = 500000.0
    uni_df["turnover"] = 2000000.0
    uni_df["trading_status"] = ""
    uni_df["suspended"] = False
    uni_df["quote_date"] = "2026-11-25"
    mask_00001 = uni_df["code"] == "00001"
    uni_df.loc[mask_00001, "volume"] = 0.0
    uni_df.loc[mask_00001, "turnover"] = 0.0
    uni_df.loc[mask_00001, "trading_status"] = "S"
    uni_df.loc[mask_00001, "suspended"] = True
    uni_df.to_csv(st_dir / "universe.csv", index=False)

    frozen_before = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    rc = hkmom_run.main()
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["publish_ok"] is False
    assert payload["froze"] is False
    assert payload["sources"]["hk_spot"]["ok"] is False
    assert payload["sources"]["hk_history"]["ok"] is False
    assert payload["sources"]["index_compute"]["ok"] is False
    assert "00001" in payload["missing_held_by_index"]["smallcap"]
    assert payload["failure_summary"]
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before
    uni_meta = json.loads((st_dir / "universe_meta.json").read_text(encoding="utf-8"))
    assert uni_meta["preserved_previous_universe"] is True
    assert uni_meta["suspended_codes"] == []
    assert uni_meta["suspended_spot"] == {}

    # 3) Even if preserved universe.csv happens to carry today's quote_date (2026-11-26),
    #    preserved_previous_universe=True MUST prevent compute_all from trusting old csv columns.
    st_same_dt = tmp_path / "preserved_csv_same_date"
    _, hsi_sess = _seed_hk_panel_with_00626(st_same_dt, sessions, suspend_days=2)
    (st_same_dt / "universe_meta.json").write_text(
        json.dumps(
            {
                "target_trade_date": "2026-11-26",
                "preserved_previous_universe": True,
                "stale": True,
                "suspended_codes": [],
                "suspended_spot": {},
                "source_statuses": {
                    "hk_spot": {"name": "hk_spot", "ok": False, "stale": True, "reason": "现货超时"}
                },
            }
        ),
        encoding="utf-8",
    )
    res_same_dt = hkmom_compute.compute_all(
        state_dir=st_same_dt,
        as_of_date=date(2026, 11, 26),
        hsi_session=hsi_sess,
        persist=False,
    )
    assert res_same_dt["meta"]["ok"] is False
    assert res_same_dt["can_freeze"] is False
    assert "00626" in res_same_dt["meta"]["missing_held_by_index"]["smallcap"]


def test_hkmom_catchup_multi_day_unfrozen_removes_only_from_day_21(
    tmp_path: Path,
) -> None:
    """Round 4 Item 3: When multiple consecutive days go unfrozen and a subsequent catch-up run
    encounters a constituent suspended >20 trading days, it stays in the basket (carrying close)
    through day 20 and is removed ONLY from day 21 onward — never retroactively for the whole segment."""
    xhkg = xcals.get_calendar("XHKG")
    # 2026-12-01 = day 18 (last frozen), 12-02 = day 19, 12-03 = day 20,
    # 12-04 = day 21 (first day > 20 -> removal effective_date!), 12-07 = day 22 (catch-up target).
    all_sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-07")]
    idx_d18 = all_sessions.index("2026-12-01")
    idx_d21 = all_sessions.index("2026-12-04")
    last_bar_before_susp_idx = idx_d21 - 21  # exact 21 trading days before 2026-12-04

    st_dir = tmp_path / "hk_catchup_day21"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    codes = ["00626"] + [f"{i:05d}" for i in range(1, 60)]
    held_init = codes[:50]

    mes_pre = hkmom_compute.month_ends(all_sessions[: idx_d18 + 1], as_of_date=date(2026, 12, 1))
    rebal_dates_pre = [d for d in mes_pre if all_sessions.index(d) >= 273]
    nov_rebal = rebal_dates_pre[-1]
    idx_nov_rebal = all_sessions.index(nov_rebal)

    # Keep all 50 held stocks flat from nov_rebal through D18 so every stock has exact 1/50 weight at D18.
    # On D19, D20, D21, D22: all 49 active stocks rise exactly +2.00% each day while 00626 is suspended.
    # -> D19 & D20 (50 stocks incl. 00626 flat): exact daily return = 49/50 * 2.00% = +1.96%
    # -> D21 & D22 (49 stocks after 00626 removed on D21): exact daily return = +2.00%
    uni_rows = []
    for idx, c in enumerate(codes):
        rows: list[list[Any]] = []
        for t_i, dt_str in enumerate(all_sessions):
            if c == "00626" and t_i > last_bar_before_susp_idx:
                continue
            if t_i < idx_nov_rebal:
                px = round(
                    10.0 * (1.0 + 0.0004 * t_i * (1 + (idx % 5) * 0.1)) * (1.0 + 0.01 * math.sin(t_i * 0.7 + idx)),
                    4,
                )
            elif t_i <= idx_d18:
                px = 10.0
            else:
                step = t_i - idx_d18
                px = round(10.0 * (1.02 ** step), 6)
            rows.append([dt_str, px, px, round(px * 1.01, 6), round(px * 0.99, 6), 100000, 250.0])
        pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)
        last_px = rows[-1][2]
        is_susp = c == "00626"
        uni_rows.append(
            {
                "code": c,
                "name": "PUBLIC FIN HOLD" if is_susp else f"HK{c}",
                "subcat": "Ordinary Shares",
                "ccy": "HKD",
                "is_ggt": idx >= 52,
                "price": last_px,
                "prev_close": last_px if is_susp else round(last_px / 1.02, 6),
                "volume": 0.0 if is_susp else 500000.0,
                "turnover": 0.0 if is_susp else 2000000.0,
                "trading_status": "S" if is_susp else "",
                "quote_date": "2026-12-07",
                "mktcap_e8": 25.0,
                "name_cn": "大众金融控股" if is_susp else f"港股{c}",
            }
        )
    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股{c}" for c in codes[52:]}), encoding="utf-8"
    )

    hsi_resp = create_autospec(requests.Response, instance=True)
    hsi_resp.json.return_value = {
        "data": {
            "hkHSI": {
                "day": [[d, 20000.0, 20000.0 + i] for i, d in enumerate(all_sessions)]
            }
        }
    }
    hsi_sess = create_autospec(requests.Session, instance=True)
    hsi_sess.get.return_value = hsi_resp

    # Record per-trading-day confirmed suspension for 00626 across D1..D21 (D22 comes from current spot)
    for d_susp in all_sessions[last_bar_before_susp_idx + 1 : idx_d21 + 1]:
        hkmom_fetch.record_suspended_for_date(st_dir, d_susp, ["00626"])

    # Case 1: Multi-day unfrozen catch-up (frozen at D18=2026-12-01, catch-up at D22=2026-12-07)
    rebal_start_idx = all_sessions.index(rebal_dates_pre[0])
    frozen_dates_pre = all_sessions[rebal_start_idx : idx_d18 + 1]
    seed_frozen = {
        "launch_date": rebal_dates_pre[0],
        "rebalance_snapshots": {d: {"smallcap": held_init, "nonggt": held_init} for d in rebal_dates_pre},
        "suspended_on_last_date": ["00626"],
        "frozen_series": {
            "dates": frozen_dates_pre,
            "smallcap": [1000.0] * len(frozen_dates_pre),
            "nonggt": [1000.0] * len(frozen_dates_pre),
            "hsi": [1000.0 + i for i in range(len(frozen_dates_pre))],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")

    res = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 12, 7), hsi_session=hsi_sess
    )
    assert res["meta"]["ok"] is True
    assert res["can_freeze"] is True
    assert res["series"]["dates"][-1] == "2026-12-07"
    assert res["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}
    assert res["meta"]["removed_suspended_codes"] == ["00626"]

    frozen_after = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    dates_out = res["series"]["dates"]
    p18 = dates_out.index("2026-12-01")
    p19 = dates_out.index("2026-12-02")
    p20 = dates_out.index("2026-12-03")
    p21 = dates_out.index("2026-12-04")
    p22 = dates_out.index("2026-12-07")

    for key in ("smallcap", "nonggt"):
        assert frozen_after["suspended_removals"][key][nov_rebal]["00626"]["effective_date"] == "2026-12-04"
        ser = res["series"][key]
        # D19: 00626 still in basket (weight 1/50, 0% return), 49 active stocks +2% -> +1.96%
        ret_d19 = ser[p19] / ser[p18] - 1.0
        assert ret_d19 == pytest.approx(49.0 / 50.0 * 0.02, abs=2e-4)
        # D20: 00626 still in basket (day 20 <= 20): active stocks rose 1.02->1.0404, 00626 flat at 1.00
        expected_d20_rel = (1.0 + 49.0 * (1.02 ** 2)) / (1.0 + 49.0 * 1.02) - 1.0
        ret_d20 = ser[p20] / ser[p19] - 1.0
        assert ret_d20 == pytest.approx(expected_d20_rel, abs=2e-4)
        # D21 (day 21 > 20): 00626 removed effective 2026-12-04, remaining 49 equal-weight -> +2.00%
        ret_d21 = ser[p21] / ser[p20] - 1.0
        assert ret_d21 == pytest.approx(0.02, abs=2e-4)
        # D22: continues on remaining 49 -> +2.00%
        ret_d22 = ser[p22] / ser[p21] - 1.0
        assert ret_d22 == pytest.approx(0.02, abs=2e-4)
        assert len(res["constituents"][key]) == 49
        assert all(c["code"] != "00626" for c in res["constituents"][key])

    # Case 2: Unfrozen full run (no frozen_series) at D22 ALSO sets effective_date=2026-12-04 (day 21)
    # and does NOT retroactively remove 00626 before D21.
    seed_no_series = {
        "launch_date": rebal_dates_pre[0],
        "rebalance_snapshots": {d: {"smallcap": held_init, "nonggt": held_init} for d in rebal_dates_pre},
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_no_series), encoding="utf-8")
    res_full = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 12, 7), hsi_session=hsi_sess, persist=False
    )
    assert res_full["meta"]["ok"] is True
    for key in ("smallcap", "nonggt"):
        assert (
            res_full["frozen_payload"]["suspended_removals"][key][nov_rebal]["00626"]["effective_date"]
            == "2026-12-04"
        )
        ser_f = res_full["series"][key]
        assert ser_f[p19] / ser_f[p18] - 1.0 == pytest.approx(49.0 / 50.0 * 0.02, abs=2e-4)
        assert ser_f[p21] / ser_f[p20] - 1.0 == pytest.approx(0.02, abs=2e-4)
        assert ser_f[p22] / ser_f[p21] - 1.0 == pytest.approx(0.02, abs=2e-4)


# ---------------------------------------------------------------------------
# Round 5 Fixes (Narrowed 4 items from Codex r3)
# ---------------------------------------------------------------------------


def _seed_hk_1124_to_1127_base(
    st_dir: Path,
    all_sessions: list[str],
    *,
    drop_panel_day: str | None = None,
    drop_00626_after: str | None = "2026-11-24",
    drop_hsi_day: str | None = None,
    spot_suspended_00626: bool = True,
    target_day_str: str = "2026-11-27",
) -> tuple[list[str], requests.Session, dict[str, list[list[Any]]]]:
    """Build deterministic HK momentum state frozen at 2026-11-24 with sessions through target_day_str."""
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    codes = ["00626"] + [f"{i:05d}" for i in range(1, 60)]
    held = codes[:50]
    idx_1124 = all_sessions.index("2026-11-24")
    idx_target = all_sessions.index(target_day_str)
    sessions = all_sessions[: idx_target + 1]

    full_bars: dict[str, list[list[Any]]] = {}
    uni_rows = []
    for idx, c in enumerate(codes):
        rows: list[list[Any]] = []
        for t_i, dt_str in enumerate(sessions):
            if drop_panel_day and dt_str == drop_panel_day:
                continue
            if c == "00626" and drop_00626_after and dt_str > drop_00626_after:
                continue
            if c == "00626":
                px = 2.49
            else:
                px = round(
                    10.0 * (1.0 + 0.0006 * t_i * (1 + (idx % 5) * 0.15)) * (1.0 + 0.012 * math.sin(t_i * 0.7 + idx)),
                    4,
                )
            rows.append([dt_str, px, px, round(px * 1.01, 4), round(px * 0.99, 4), 100000, 220.0])
        full_bars[c] = rows
        pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)
        last_px = rows[-1][2]
        is_susp = c == "00626" and spot_suspended_00626
        uni_rows.append(
            {
                "code": c,
                "name": "PUBLIC FIN HOLD" if c == "00626" else f"HK{c}",
                "subcat": "Equity Securities (Main Board)",
                "ccy": "HKD",
                "is_ggt": idx >= 52,
                "price": last_px,
                "prev_close": last_px if is_susp else round(last_px * 0.99, 4),
                "volume": 0.0 if is_susp else 500000.0,
                "turnover": 0.0 if is_susp else 2000000.0,
                "trading_status": "S" if is_susp else "",
                "quote_date": target_day_str,
                "mktcap_e8": 25.0,
                "name_cn": "大众金融控股" if c == "00626" else f"港股{c}",
            }
        )
    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股{c}" for c in codes[52:]}), encoding="utf-8"
    )

    mes_1124 = hkmom_compute.month_ends(all_sessions[: idx_1124 + 1], as_of_date=date(2026, 11, 24))
    rebal_dates = [d for d in mes_1124 if all_sessions.index(d) >= 273]
    rebal_start_idx = all_sessions.index(rebal_dates[0])
    frozen_dates = all_sessions[rebal_start_idx : idx_1124 + 1]
    seed_frozen = {
        "launch_date": rebal_dates[0],
        "rebalance_snapshots": {d: {"smallcap": held, "nonggt": held} for d in rebal_dates},
        "frozen_series": {
            "dates": frozen_dates,
            "smallcap": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
            "nonggt": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
            "hsi": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")

    hsi_rows = [
        [d, 20000.0, 20000.0 + i * 10.0]
        for i, d in enumerate(sessions)
        if not (drop_hsi_day and d == drop_hsi_day)
    ]
    hsi_resp = create_autospec(requests.Response, instance=True)
    hsi_resp.json.return_value = {"data": {"hkHSI": {"day": hsi_rows}}}
    hsi_sess = create_autospec(requests.Session, instance=True)
    hsi_sess.get.return_value = hsi_resp
    return codes, hsi_sess, full_bars


def test_hkmom_per_day_suspension_catchup_1124_to_1127_matches_daily_and_fails_without_record(
    tmp_path: Path,
) -> None:
    """Round 5 Item 1:
    - Production scenario: frozen stops at 2026-11-24, 00626 suspended starting 2026-11-25 through 2026-11-27.
    - Per-trading-day suspension records allow catch-up from 11-24 to 11-27 to advance frozen_history,
      producing exact same series & constituents as day-by-day updates on 11-25, 11-26, 11-27.
    - Missing suspension record on an intermediate day (11-26) with missing bar -> MUST FAIL.
    - Catch-up crossing day 21 determines effective_date from per-day suspension records, and fails if
      any intermediate day lacks a suspension record."""
    xhkg = xcals.get_calendar("XHKG")
    all_sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-28")]
    idx_1124 = all_sessions.index("2026-11-24")
    idx_1127 = all_sessions.index("2026-11-27")
    assert all_sessions[idx_1124 : idx_1127 + 1] == ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]

    # 1) Reference: day-by-day updates on 11-25, 11-26, 11-27
    st_daily = tmp_path / "daily_run"
    _codes, _sess_unused, full_bars = _seed_hk_1124_to_1127_base(st_daily, all_sessions, target_day_str="2026-11-27")
    for day_str in ("2026-11-25", "2026-11-26", "2026-11-27"):
        # Write hist & universe as of day_str
        for c, rows in full_bars.items():
            sub_rows = [r for r in rows if r[0] <= day_str]
            pd.DataFrame(
                sub_rows,
                columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
            ).to_csv(st_daily / "hist" / f"{c}.csv", index=False)
        uni_df = pd.read_csv(st_daily / "universe.csv", dtype={"code": str})
        uni_df["quote_date"] = day_str
        uni_df.to_csv(st_daily / "universe.csv", index=False)
        hsi_sub = [
            [d, 20000.0, 20000.0 + i * 10.0]
            for i, d in enumerate(all_sessions[: all_sessions.index(day_str) + 1])
        ]
        hsi_resp = create_autospec(requests.Response, instance=True)
        hsi_resp.json.return_value = {"data": {"hkHSI": {"day": hsi_sub}}}
        hsi_sess_day = create_autospec(requests.Session, instance=True)
        hsi_sess_day.get.return_value = hsi_resp
        res_day = hkmom_compute.compute_all(
            state_dir=st_daily, as_of_date=date.fromisoformat(day_str), hsi_session=hsi_sess_day
        )
        assert res_day["meta"]["ok"] is True
        assert res_day["series"]["dates"][-1] == day_str
    daily_final_series = json.loads((st_daily / "index_series.json").read_text(encoding="utf-8"))
    daily_final_cons = json.loads((st_daily / "constituents.json").read_text(encoding="utf-8"))

    # 2) Catch-up from frozen 11-24 directly to 11-27 with per-day suspension records (11-25, 11-26 recorded; 11-27 from spot)
    st_catchup = tmp_path / "catchup_1124_to_1127"
    _, hsi_sess_cu, _ = _seed_hk_1124_to_1127_base(st_catchup, all_sessions, target_day_str="2026-11-27")
    hkmom_fetch.record_suspended_for_date(st_catchup, "2026-11-25", ["00626"])
    hkmom_fetch.record_suspended_for_date(st_catchup, "2026-11-26", ["00626"])

    res_cu = hkmom_compute.compute_all(
        state_dir=st_catchup, as_of_date=date(2026, 11, 27), hsi_session=hsi_sess_cu
    )
    assert res_cu["meta"]["ok"] is True
    assert res_cu["meta"]["series_stale"] is False
    assert res_cu["can_freeze"] is True
    assert res_cu["series"]["dates"][-1] == "2026-11-27"
    assert res_cu["meta"]["missing_held_by_index"] == {"smallcap": [], "nonggt": []}
    assert res_cu["series"] == daily_final_series
    assert res_cu["constituents"] == daily_final_cons
    susp_sc = res_cu["meta"]["suspended_constituents"]["smallcap"]
    assert len(susp_sc) == 1
    assert susp_sc[0]["code"] == "00626"
    assert susp_sc[0]["carried_close_date"] == "2026-11-24"
    assert susp_sc[0]["suspended_trading_days"] == 3
    frozen_cu = json.loads((st_catchup / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_cu["frozen_series"]["dates"][-1] == "2026-11-27"
    assert frozen_cu["suspended_on_last_date"] == ["00626"]
    for d_s in ("2026-11-25", "2026-11-26", "2026-11-27"):
        assert "00626" in frozen_cu["suspended_by_date"][d_s]

    # 3) Catch-up 11-24 -> 11-27 when 11-26 lacks a suspension record (only 11-25 & 11-27 confirmed) -> MUST FAIL!
    st_gap_rec = tmp_path / "catchup_missing_1126_susp_record"
    _, hsi_sess_gr, _ = _seed_hk_1124_to_1127_base(st_gap_rec, all_sessions, target_day_str="2026-11-27")
    hkmom_fetch.record_suspended_for_date(st_gap_rec, "2026-11-25", ["00626"])
    # Intentionally omit 2026-11-26 suspension record!
    frozen_before_gr = json.loads((st_gap_rec / "frozen_history.json").read_text(encoding="utf-8"))
    res_gr = hkmom_compute.compute_all(
        state_dir=st_gap_rec, as_of_date=date(2026, 11, 27), hsi_session=hsi_sess_gr
    )
    assert res_gr["meta"]["ok"] is False
    assert res_gr["can_freeze"] is False
    assert "00626" in res_gr["meta"]["missing_held_by_index"]["smallcap"]
    assert res_gr["series"]["dates"][-1] == "2026-11-24"
    assert json.loads((st_gap_rec / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before_gr

    # 4) Catch-up crossing day 21 starting AFTER frozen date (frozen at 2026-11-30 with 00626 active,
    #    00626 suspended starting 2026-12-01 through day 22):
    #    - With per-day records for all 21 days -> removes 00626 effective on exact 21st trading day.
    #    - Dropping one intermediate day's suspension record (day 10) -> MUST FAIL instead of removing!
    all_sessions_d22 = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2027-01-12")]
    idx_nov_me = all_sessions_d22.index("2026-11-30")
    day21_str = all_sessions_d22[idx_nov_me + 21]
    day22_str = all_sessions_d22[idx_nov_me + 22]
    st_d22 = tmp_path / "catchup_1130_to_d22"
    codes_d22, hsi_sess_d22, _ = _seed_hk_1124_to_1127_base(
        st_d22,
        all_sessions_d22,
        drop_00626_after="2026-11-30",
        spot_suspended_00626=True,
        target_day_str=day22_str,
    )
    held_d22 = codes_d22[:50]
    mes_nov = hkmom_compute.month_ends(all_sessions_d22[: idx_nov_me + 1], as_of_date=date(2026, 11, 30))
    rebals_nov = [d for d in mes_nov if all_sessions_d22.index(d) >= 273]
    rebal_start_nov = all_sessions_d22.index(rebals_nov[0])
    frozen_nov_dates = all_sessions_d22[rebal_start_nov : idx_nov_me + 1]
    seed_frozen_nov = {
        "launch_date": rebals_nov[0],
        "rebalance_snapshots": {d: {"smallcap": held_d22, "nonggt": held_d22} for d in rebals_nov},
        "frozen_series": {
            "dates": frozen_nov_dates,
            "smallcap": [1000.0 + i * 0.5 for i in range(len(frozen_nov_dates))],
            "nonggt": [1000.0 + i * 0.5 for i in range(len(frozen_nov_dates))],
            "hsi": [1000.0 + i * 0.5 for i in range(len(frozen_nov_dates))],
        },
    }
    (st_d22 / "frozen_history.json").write_text(json.dumps(seed_frozen_nov), encoding="utf-8")

    # 4a) Missing day 10 suspension record -> fails instead of removing 00626!
    day10_str = all_sessions_d22[idx_nov_me + 10]
    for d_s in all_sessions_d22[idx_nov_me + 1 : idx_nov_me + 22]:
        if d_s != day10_str:
            hkmom_fetch.record_suspended_for_date(st_d22, d_s, ["00626"])
    res_d22_gap = hkmom_compute.compute_all(
        state_dir=st_d22, as_of_date=date.fromisoformat(day22_str), hsi_session=hsi_sess_d22, persist=False
    )
    assert res_d22_gap["meta"]["ok"] is False
    assert res_d22_gap["can_freeze"] is False
    assert "00626" in res_d22_gap["meta"]["missing_held_by_index"]["smallcap"]

    # 4b) Restore day 10 suspension record -> removes 00626 effective on exact 21st trading day!
    hkmom_fetch.record_suspended_for_date(st_d22, day10_str, ["00626"])
    res_d22 = hkmom_compute.compute_all(
        state_dir=st_d22, as_of_date=date.fromisoformat(day22_str), hsi_session=hsi_sess_d22
    )
    assert res_d22["meta"]["ok"] is True
    assert res_d22["can_freeze"] is True
    assert res_d22["series"]["dates"][-1] == day22_str
    frozen_d22 = json.loads((st_d22 / "frozen_history.json").read_text(encoding="utf-8"))
    for key in ("smallcap", "nonggt"):
        assert frozen_d22["suspended_removals"][key]["2026-11-30"]["00626"]["effective_date"] == day21_str
        assert all(c["code"] != "00626" for c in res_d22["constituents"][key])


def test_hkmom_catchup_checks_every_hkex_trading_day_for_panel_held_and_hsi(
    tmp_path: Path,
) -> None:
    """Round 5 Item 2: Catch-up enumerates every HKEX trading day after frozen_last_date and
    fails if ANY trading day is completely absent from panel, held constituents, or HSI."""
    xhkg = xcals.get_calendar("XHKG")
    all_sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-27")]

    # Case A: 2026-11-25 missing across the ENTIRE stock panel, while 11-26, 11-27 & HSI are complete -> MUST FAIL!
    st_panel_gap = tmp_path / "panel_whole_day_missing"
    _, hsi_sess_a, _ = _seed_hk_1124_to_1127_base(
        st_panel_gap,
        all_sessions,
        drop_panel_day="2026-11-25",
        drop_00626_after=None,
        spot_suspended_00626=False,
        target_day_str="2026-11-27",
    )
    frozen_before_a = json.loads((st_panel_gap / "frozen_history.json").read_text(encoding="utf-8"))
    res_a = hkmom_compute.compute_all(
        state_dir=st_panel_gap, as_of_date=date(2026, 11, 27), hsi_session=hsi_sess_a
    )
    assert res_a["meta"]["ok"] is False
    assert res_a["meta"]["series_stale"] is True
    assert res_a["can_freeze"] is False
    assert "2026-11-25" in (res_a["meta"]["stale_reason"] or "")
    assert res_a["series"]["dates"][-1] == "2026-11-24"
    assert json.loads((st_panel_gap / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before_a

    # Case B: Panel has 11-25..11-27, held stock 00001 drops 11-25 without suspension record -> MUST FAIL!
    st_held_gap = tmp_path / "held_single_day_missing"
    _, hsi_sess_b, _ = _seed_hk_1124_to_1127_base(
        st_held_gap,
        all_sessions,
        drop_00626_after=None,
        spot_suspended_00626=False,
        target_day_str="2026-11-27",
    )
    p_00001 = st_held_gap / "hist" / "00001.csv"
    df_00001 = pd.read_csv(p_00001)
    df_00001[df_00001["date"] != "2026-11-25"].to_csv(p_00001, index=False)
    res_b = hkmom_compute.compute_all(
        state_dir=st_held_gap, as_of_date=date(2026, 11, 27), hsi_session=hsi_sess_b
    )
    assert res_b["meta"]["ok"] is False
    assert res_b["can_freeze"] is False
    assert "00001" in res_b["meta"]["missing_held_by_index"]["smallcap"]
    assert res_b["series"]["dates"][-1] == "2026-11-24"

    # Case C: Panel & held have 11-25..11-27, HSI drops 2026-11-25 -> MUST FAIL!
    st_hsi_gap = tmp_path / "hsi_whole_day_missing"
    _, hsi_sess_c, _ = _seed_hk_1124_to_1127_base(
        st_hsi_gap,
        all_sessions,
        drop_00626_after=None,
        drop_hsi_day="2026-11-25",
        spot_suspended_00626=False,
        target_day_str="2026-11-27",
    )
    frozen_before_c = json.loads((st_hsi_gap / "frozen_history.json").read_text(encoding="utf-8"))
    res_c = hkmom_compute.compute_all(
        state_dir=st_hsi_gap, as_of_date=date(2026, 11, 27), hsi_session=hsi_sess_c
    )
    assert res_c["meta"]["ok"] is False
    assert res_c["can_freeze"] is False
    assert res_c["meta"]["source_statuses"]["hsi_history"]["ok"] is False
    assert "2026-11-25" in (res_c["meta"]["source_statuses"]["hsi_history"]["reason"] or "")
    assert json.loads((st_hsi_gap / "frozen_history.json").read_text(encoding="utf-8")) == frozen_before_c


def test_hkmom_unadjusted_only_history_preserves_file_and_counts_failure_unless_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5 Item 3: When Tencent returns `day` without `qfqday`:
    - Stored adjusted CSV is always preserved verbatim.
    - Active (non-exempt) stocks count toward download failure (held guard AND 3% threshold).
    - Only independently confirmed delisted or target-day suspended stocks are exempted."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", create_autospec(_REAL_SLEEP, return_value=None))
    target_dt = "2026-11-27"
    prior_csv = (
        "date,open,close,high,low,volume,turnover_wan\n"
        "2026-11-25,10.0,10.0,10.1,9.9,1000,100.0\n"
        "2026-11-26,10.0,10.1,10.2,9.9,1000,100.0\n"
    )

    def _make_sess(unadj_codes: set[str]) -> requests.Session:
        def _fake_get(url: str, **_kw):
            code = url.split("param=hk")[-1].split(",")[0]
            resp = create_autospec(requests.Response, instance=True)
            if code in unadj_codes:
                resp.json.return_value = {
                    "data": {f"hk{code}": {"day": [["2026-11-26", "10.0", "10.1", "10.2", "9.9", "1000"]]}}
                }
            else:
                resp.json.return_value = {
                    "data": {
                        f"hk{code}": {
                            "qfqday": [
                                ["2026-11-25", "10.0", "10.0", "10.1", "9.9", "1000", "100.0"],
                                ["2026-11-26", "10.0", "10.1", "10.2", "9.9", "1000", "100.0"],
                                ["2026-11-27", "10.1", "10.2", "10.3", "10.0", "1000", "100.0"],
                            ]
                        }
                    }
                }
            return resp

        s = create_autospec(requests.Session, instance=True)
        s.get.side_effect = _fake_get
        return s

    codes = [f"{i:05d}" for i in range(1, 101)]

    # Case A: Held stock 00001 receives day-only -> preserves old CSV AND fails held guard!
    st_held = tmp_path / "unadj_held"
    (st_held / "hist").mkdir(parents=True)
    (st_held / "hist" / "00001.csv").write_text(prior_csv, encoding="utf-8")
    prog_held = hkmom_fetch.download_history(
        codes,
        state_dir=st_held,
        workers=4,
        session=_make_sess({"00001"}),
        target_date=target_dt,
        held_codes={"00001"},
    )
    assert (st_held / "hist" / "00001.csv").read_text(encoding="utf-8") == prior_csv
    assert prog_held["n_errors"] == 1
    assert prog_held["failed_held"] == ["00001"]
    assert prog_held["stale"] is True
    assert prog_held["source_status"]["ok"] is False

    # Case B: 4/100 (4% > 3%) non-held stocks receive day-only -> preserves old CSVs AND fails 3% threshold!
    st_ratio = tmp_path / "unadj_over3pct"
    (st_ratio / "hist").mkdir(parents=True)
    bad_four = {"00097", "00098", "00099", "00100"}
    for c in bad_four:
        (st_ratio / "hist" / f"{c}.csv").write_text(prior_csv, encoding="utf-8")
    prog_ratio = hkmom_fetch.download_history(
        codes,
        state_dir=st_ratio,
        workers=4,
        session=_make_sess(bad_four),
        target_date=target_dt,
        held_codes={"00001"},
    )
    for c in bad_four:
        assert (st_ratio / "hist" / f"{c}.csv").read_text(encoding="utf-8") == prior_csv
    assert prog_ratio["n_errors"] == 4
    assert prog_ratio["stale"] is True
    assert prog_ratio["source_status"]["ok"] is False
    assert "3%" in (prog_ratio["source_status"]["reason"] or "")

    # Case C: Target-day suspended stock receives day-only -> preserves old CSV AND exempted!
    st_susp = tmp_path / "unadj_suspended"
    (st_susp / "hist").mkdir(parents=True)
    (st_susp / "hist" / "00626.csv").write_text(prior_csv, encoding="utf-8")
    prog_susp = hkmom_fetch.download_history(
        ["00626"],
        state_dir=st_susp,
        workers=1,
        session=_make_sess({"00626"}),
        target_date=target_dt,
        held_codes={"00626"},
        spot_info={
            "00626": {
                "price": 10.1,
                "prev_close": 10.1,
                "volume": 0.0,
                "turnover": 0.0,
                "trading_status": "S",
                "quote_date": target_dt,
            }
        },
    )
    assert (st_susp / "hist" / "00626.csv").read_text(encoding="utf-8") == prior_csv
    assert prog_susp["n_errors"] == 0
    assert prog_susp["stale"] is False
    assert prog_susp["source_status"]["ok"] is True

    # Case D: Independently confirmed delisted (absent from clean today's hkex_universe) -> preserves CSV & exempt!
    st_delist = tmp_path / "unadj_delisted_hkex"
    (st_delist / "hist").mkdir(parents=True)
    (st_delist / "hist" / "09986.csv").write_text(prior_csv, encoding="utf-8")
    pd.DataFrame({"code": ["00001", "00002"], "name": ["A", "B"]}).to_csv(
        st_delist / "universe.csv", index=False
    )
    (st_delist / "universe_meta.json").write_text(
        json.dumps(
            {
                "target_trade_date": target_dt,
                "preserved_previous_universe": False,
                "source_statuses": {
                    "hkex_universe": {"name": "hkex_universe", "ok": True, "stale": False},
                    "hk_spot": {"name": "hk_spot", "ok": True, "stale": False},
                },
            }
        ),
        encoding="utf-8",
    )
    prog_delist = hkmom_fetch.download_history(
        ["09986"],
        state_dir=st_delist,
        workers=1,
        session=_make_sess({"09986"}),
        target_date=target_dt,
        held_codes={"09986"},
    )
    assert (st_delist / "hist" / "09986.csv").read_text(encoding="utf-8") == prior_csv
    assert prog_delist["n_errors"] == 0
    assert prog_delist["stale"] is False

    # Case E: Degraded/preserved hkex_universe CANNOT confirm delisting -> counts as failure!
    (st_delist / "universe_meta.json").write_text(
        json.dumps(
            {
                "target_trade_date": target_dt,
                "preserved_previous_universe": True,
                "source_statuses": {
                    "hkex_universe": {"name": "hkex_universe", "ok": False, "stale": True},
                },
            }
        ),
        encoding="utf-8",
    )
    prog_degraded = hkmom_fetch.download_history(
        ["09986"],
        state_dir=st_delist,
        workers=1,
        session=_make_sess({"09986"}),
        target_date=target_dt,
        held_codes={"09986"},
    )
    assert prog_degraded["n_errors"] == 1
    assert prog_degraded["failed_held"] == ["09986"]
    assert prog_degraded["stale"] is True


def test_pm_cn_valuation_fails_and_stays_pending_when_weekly_list_misses_completed_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5 Item 4: At US Eastern 2026-11-27 17:15 (Beijing 2026-11-28 06:15), daily valuation
    reaches 2026-11-27 while index_analysis_week_month_sw lags at 2026-11-20 (or drops an interior
    completed trading week). cn_valuation MUST fail and keep missing week(s) pending until recovery."""
    monkeypatch.setattr(pm_collect.time, "sleep", create_autospec(_REAL_SLEEP, return_value=None))
    cfg = pm_collect.load_config()
    xshg = xcals.get_calendar("XSHG")
    dates = pd.DatetimeIndex(xshg.sessions_in_range("2025-01-01", "2026-11-27"))
    us_fri_close = datetime(2026, 11, 27, 17, 15, 0, tzinfo=feeds_common.US_TZ)
    all_weeks_27 = feeds_common.completed_trading_weeks("cn", "2021-12-31", date(2026, 11, 27))
    assert all_weeks_27[-2:] == ["2026-11-20", "2026-11-27"]

    # Case 1: Weekly list lags at 2026-11-20 while daily valuation has 2026-11-27 -> MUST FAIL & stay pending!
    raw_dir = tmp_path / "val_lagging_week_list"
    fake_ak_lag = _make_cn_fake_ak(dates, weekly_dates=all_weeks_27[:-1])
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_dir, ak_module=fake_ak_lag, now=us_fri_close)["ok"] is True
    res_lag = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_lag, now=us_fri_close)
    assert res_lag["ok"] is False
    assert res_lag["stale"] is True
    assert res_lag["source_status"]["ok"] is False
    assert res_lag["latest"] is not None and res_lag["latest"]["ok"] is True
    assert res_lag["missing_weeks"] == ["2026-11-27"]
    assert res_lag["missing_pairs"] == 2
    assert any("2026-11-27" in e for e in res_lag["errors"])
    prog_lag = json.loads((raw_dir / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    assert "2026-11-27" not in prog_lag["done"]
    assert set(prog_lag["done"]["2026-11-20"]) == {"一级行业", "二级行业"}

    # Recovery: weekly list now includes 2026-11-27 -> fetches only 2026-11-27 and succeeds cleanly!
    called_weeks: list[tuple[str, str]] = []
    fake_ak_rec = _make_cn_fake_ak(dates, weekly_dates=all_weeks_27)
    orig_weekly = fake_ak_rec.index_analysis_weekly_sw.side_effect

    def _track_weekly(symbol: str, date: str) -> pd.DataFrame:
        called_weeks.append((date, symbol))
        return orig_weekly(symbol, date)

    fake_ak_rec.index_analysis_weekly_sw.side_effect = _track_weekly
    res_rec = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_rec, now=us_fri_close)
    assert res_rec["ok"] is True
    assert res_rec["stale"] is False
    assert res_rec["missing_weeks"] == []
    assert res_rec["missing_pairs"] == 0
    assert called_weeks == [("20261127", "一级行业"), ("20261127", "二级行业")]
    prog_rec = json.loads((raw_dir / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    assert set(prog_rec["done"]["2026-11-27"]) == {"一级行业", "二级行业"}

    # Case 2 (Round 6 Item 2): Shenwan catalog itself omits some historical weeks (e.g. pre-holiday
    # 2026-09-30 and make-up/interior weeks) while latest completed week (2026-11-27) is present
    # and all catalog-listed weeks return valid details -> MUST SUCCEED!
    raw_omit = tmp_path / "val_omitted_historical_weeks"
    omitted_hist = {"2026-09-30", "2026-11-13"}
    assert "2026-09-30" in all_weeks_27
    weeks_omitted = [w for w in all_weeks_27 if w not in omitted_hist]
    fake_ak_omit = _make_cn_fake_ak(dates, weekly_dates=weeks_omitted)
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_omit, ak_module=fake_ak_omit, now=us_fri_close)["ok"] is True
    res_omit = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_omit, ak_module=fake_ak_omit, now=us_fri_close)
    assert res_omit["ok"] is True
    assert res_omit["stale"] is False
    assert res_omit["missing_weeks"] == []
    assert res_omit["missing_pairs"] == 0
    assert res_omit["last_week"] == "2026-11-27"

    # Case 3 (Round 6 Item 2): Catalog lists an earlier week (2026-11-13) AND latest week (2026-11-27),
    # but detail payload for the listed historical week fails/returns empty -> MUST FAIL!
    raw_detail_fail = tmp_path / "val_listed_week_detail_fail"
    fake_ak_dfail = _make_cn_fake_ak(dates, weekly_dates=all_weeks_27)
    orig_dfail_weekly = fake_ak_dfail.index_analysis_weekly_sw.side_effect

    def _fail_1113(symbol: str, date: str) -> pd.DataFrame:
        if date == "20261113":
            return pd.DataFrame()
        return orig_dfail_weekly(symbol, date)

    fake_ak_dfail.index_analysis_weekly_sw.side_effect = _fail_1113
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_detail_fail, ak_module=fake_ak_dfail, now=us_fri_close)["ok"] is True
    res_dfail = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_detail_fail, ak_module=fake_ak_dfail, now=us_fri_close)
    assert res_dfail["ok"] is False
    assert res_dfail["stale"] is True
    assert res_dfail["missing_weeks"] == []
    assert res_dfail["missing_pairs"] == 2
    assert any("2026-11-13" in e for e in res_dfail["errors"])


# ---------------------------------------------------------------------------
# Round 6 Fixes (Yahoo daily backfill for missed suspensions & pre-freeze gap)
# ---------------------------------------------------------------------------


def test_hkmom_yahoo_daily_backfill_advances_1124_to_1127_and_rejects_nonzero_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 6/7 Item 1:
    - Production state frozen at 2026-11-24 with NO suspended_history.json; 00626 suspended
      starting 2026-11-25 through 2026-11-27.
    - Yahoo daily is called with keepna=True, repair=False. Real suspension shape
      (11-24 close=2.49 vol=29136200; 11-25 & 11-26 O=H=L=C=2.49 vol=0) backfills 11-25 and
      11-26 into suspended_history.json with source='yahoo_daily', allowing run_daily_job to
      advance frozen_history from 2026-11-24 to 2026-11-27.
    - Non-zero volume, close != prev_close, non-flat OHLC, raw Volume=NaN, OR Volume=0
      (NaN filled to 0 by yfinance) with any missing OHLC price -> NOT recorded and MUST FAIL."""
    xhkg = xcals.get_calendar("XHKG")
    all_sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-27")]
    assert all_sessions[-4:] == ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]

    def _make_fake_yf(
        rows: list[tuple[str, float, float, float, float, float]],
    ) -> tuple[SimpleNamespace, Any, Any]:
        yf_df = pd.DataFrame(
            [
                {"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v}
                for _, o, h, lo, c, v in rows
            ],
            index=pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Hong_Kong") for d, *_ in rows]),
        )
        fake_ticker = create_autospec(yf.Ticker, instance=True)
        fake_ticker.history.return_value = yf_df
        ticker_ctor = create_autospec(yf.Ticker, return_value=fake_ticker)
        return SimpleNamespace(Ticker=ticker_ctor), ticker_ctor, fake_ticker

    def _run_catchup_job(
        subdir: str, yf_rows: list[tuple[str, float, float, float, float, float]]
    ) -> tuple[dict[str, Any], Path, Any, Any]:
        st_dir = tmp_path / subdir
        out_dir = tmp_path / f"{subdir}_out"
        codes, hsi_sess, _ = _seed_hk_1124_to_1127_base(
            st_dir, all_sessions, target_day_str="2026-11-27"
        )
        uni_df = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
        uni_meta = {
            "target_trade_date": "2026-11-27",
            "preserved_previous_universe": False,
            "stale": False,
            "errors": [],
            "suspended_codes": ["00626"],
            "suspended_spot": {
                "00626": {
                    "price": 2.49,
                    "prev_close": 2.49,
                    "volume": 0.0,
                    "turnover": 0.0,
                    "trading_status": "S",
                    "quote_date": "2026-11-27",
                }
            },
            "source_statuses": {
                "hkex_universe": feeds_common.make_source_result(
                    "hkex_universe", ok=True, stale=False, as_of="2026-11-27", label="港交所证券列表"
                ).to_dict(),
                "ggt_list": feeds_common.make_source_result(
                    "ggt_list", ok=True, stale=False, as_of="2026-11-27", label="深交所港股通名单"
                ).to_dict(),
                "hk_spot": feeds_common.make_source_result(
                    "hk_spot", ok=True, stale=False, as_of="2026-11-27", label="港股现货行情"
                ).to_dict(),
            },
        }
        (st_dir / "universe_meta.json").write_text(json.dumps(uni_meta), encoding="utf-8")

        def _fake_build_universe(**kw: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
            hkmom_fetch.record_suspended_for_date(st_dir, "2026-11-27", ["00626"], source="hk_spot")
            return uni_df.copy(), uni_meta

        hist_ok = {
            "done": len(codes),
            "total": len(codes),
            "errors": [],
            "n_errors": 0,
            "stale": False,
            "source_status": feeds_common.make_source_result(
                "hk_history", ok=True, stale=False, as_of="2026-11-27", label="港股历史日线"
            ).to_dict(),
        }
        orig_compute_all = hkmom_compute.compute_all
        dt_27 = datetime(2026, 11, 27, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
        fake_yf, ticker_ctor, fake_ticker = _make_fake_yf(yf_rows)

        monkeypatch.setattr(
            hkmom_run,
            "build_universe",
            create_autospec(hkmom_fetch.build_universe, side_effect=_fake_build_universe),
        )
        monkeypatch.setattr(
            hkmom_run,
            "download_history",
            create_autospec(hkmom_fetch.download_history, return_value=hist_ok),
        )
        monkeypatch.setattr(
            hkmom_run,
            "compute_all",
            create_autospec(
                orig_compute_all,
                side_effect=lambda **kw: orig_compute_all(**{**kw, "hsi_session": hsi_sess}),
            ),
        )
        monkeypatch.setattr(
            hkmom_run,
            "compute_intraday_snapshot",
            create_autospec(
                hkmom_compute.compute_intraday_snapshot,
                return_value={
                    "as_of": "2026-11-27 16:35:00",
                    "stale": False,
                    "intraday_source_statuses": {
                        "hk_intraday_spot": feeds_common.make_source_result(
                            "hk_intraday_spot", ok=True, stale=False, as_of="2026-11-27", label="港股盘中现货"
                        ).to_dict(),
                        "hsi_spot": feeds_common.make_source_result(
                            "hsi_spot", ok=True, stale=False, as_of="2026-11-27", label="恒生指数现货"
                        ).to_dict(),
                    },
                },
            ),
        )
        monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_27)
        monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_27)
        monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_27)

        res = hkmom_run.run_daily_job(
            state_dir=st_dir,
            output_dir=out_dir,
            as_of_date=date(2026, 11, 27),
            yf_module=fake_yf,
        )
        return res, st_dir, ticker_ctor, fake_ticker

    # 1) Verified production Yahoo daily suspension shape -> backfills 11-25 & 11-26 and advances freeze to 11-27!
    res_ok, st_ok, ticker_ok, fake_tk_ok = _run_catchup_job(
        "yahoo_backfill_ok",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", 2.49, 2.49, 2.49, 2.49, 0.0),
            ("2026-11-26", 2.49, 2.49, 2.49, 2.49, 0.0),
        ],
    )
    ticker_ok.assert_called_once_with("0626.HK")
    fake_tk_ok.history.assert_called_once()
    hist_kwargs = fake_tk_ok.history.call_args.kwargs
    assert hist_kwargs["interval"] == "1d"
    assert hist_kwargs["auto_adjust"] is False
    assert hist_kwargs["keepna"] is True
    assert hist_kwargs["repair"] is False
    assert res_ok["ok"] is True
    assert res_ok["publish_ok"] is True
    assert res_ok["froze"] is True
    assert res_ok["last_date"] == "2026-11-27"
    assert res_ok["missing_held_by_index"] == {"smallcap": [], "nonggt": []}

    susp_doc = json.loads((st_ok / "suspended_history.json").read_text(encoding="utf-8"))
    assert susp_doc["by_date"]["2026-11-25"] == ["00626"]
    assert susp_doc["by_date"]["2026-11-26"] == ["00626"]
    assert susp_doc["by_date"]["2026-11-27"] == ["00626"]
    assert susp_doc["sources"]["2026-11-25"]["00626"] == "yahoo_daily"
    assert susp_doc["sources"]["2026-11-26"]["00626"] == "yahoo_daily"
    assert susp_doc["sources"]["2026-11-27"]["00626"] == "hk_spot"

    frozen_ok = json.loads((st_ok / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_ok["frozen_series"]["dates"][-1] == "2026-11-27"

    # 2) Non-zero volume on 2026-11-26 -> NOT recorded as suspended, run_daily_job MUST FAIL!
    res_vol, st_vol, ticker_vol, _ = _run_catchup_job(
        "yahoo_backfill_nonzero_vol",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", 2.49, 2.49, 2.49, 2.49, 0.0),
            ("2026-11-26", 2.49, 2.49, 2.49, 2.49, 1000.0),
        ],
    )
    ticker_vol.assert_called_once_with("0626.HK")
    assert res_vol["ok"] is False
    assert res_vol["publish_ok"] is False
    assert res_vol["froze"] is False
    assert "00626" in res_vol["missing_held_by_index"]["smallcap"]
    susp_vol = json.loads((st_vol / "suspended_history.json").read_text(encoding="utf-8"))
    assert "00626" not in susp_vol["by_date"].get("2026-11-26", [])
    frozen_vol = json.loads((st_vol / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_vol["frozen_series"]["dates"][-1] == "2026-11-24"

    # 3) Zero volume on 2026-11-26 but close differs from prev_close -> NOT recorded, MUST FAIL!
    res_px, st_px, _, _ = _run_catchup_job(
        "yahoo_backfill_diff_close",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", 2.49, 2.49, 2.49, 2.49, 0.0),
            ("2026-11-26", 2.50, 2.50, 2.50, 2.50, 0.0),
        ],
    )
    assert res_px["ok"] is False
    assert res_px["froze"] is False
    assert "00626" in res_px["missing_held_by_index"]["smallcap"]
    frozen_px = json.loads((st_px / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_px["frozen_series"]["dates"][-1] == "2026-11-24"

    # 4) Volume NaN filled to 0 by yfinance with missing OHLC price(s) -> NOT suspended, MUST FAIL!
    nan = float("nan")
    res_na_px, st_na_px, _, _ = _run_catchup_job(
        "yahoo_backfill_filled_zero_missing_ohlc",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", nan, nan, nan, nan, 0.0),
            ("2026-11-26", nan, nan, nan, 2.49, 0.0),
        ],
    )
    assert res_na_px["ok"] is False
    assert res_na_px["froze"] is False
    assert "00626" in res_na_px["missing_held_by_index"]["smallcap"]
    susp_na_px = json.loads((st_na_px / "suspended_history.json").read_text(encoding="utf-8"))
    assert "00626" not in susp_na_px["by_date"].get("2026-11-25", [])
    assert "00626" not in susp_na_px["by_date"].get("2026-11-26", [])
    frozen_na_px = json.loads((st_na_px / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_na_px["frozen_series"]["dates"][-1] == "2026-11-24"

    # 5) Raw Volume=NaN even with flat OHLC=2.49 -> NOT suspended, MUST FAIL!
    res_na_vol, st_na_vol, _, _ = _run_catchup_job(
        "yahoo_backfill_nan_volume",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", 2.49, 2.49, 2.49, 2.49, 0.0),
            ("2026-11-26", 2.49, 2.49, 2.49, 2.49, nan),
        ],
    )
    assert res_na_vol["ok"] is False
    assert res_na_vol["froze"] is False
    assert "00626" in res_na_vol["missing_held_by_index"]["smallcap"]
    susp_na_vol = json.loads((st_na_vol / "suspended_history.json").read_text(encoding="utf-8"))
    assert "00626" not in susp_na_vol["by_date"].get("2026-11-26", [])

    # 6) Volume=0 and Close == prev_close, but Open/High/Low not all equal to prev_close -> NOT suspended!
    res_nonflat, st_nonflat, _, _ = _run_catchup_job(
        "yahoo_backfill_nonflat_ohlc",
        [
            ("2026-11-24", 2.48, 2.50, 2.47, 2.49, 29136200.0),
            ("2026-11-25", 2.49, 2.49, 2.49, 2.49, 0.0),
            ("2026-11-26", 2.48, 2.49, 2.48, 2.49, 0.0),
        ],
    )
    assert res_nonflat["ok"] is False
    assert res_nonflat["froze"] is False
    assert "00626" in res_nonflat["missing_held_by_index"]["smallcap"]
    susp_nonflat = json.loads((st_nonflat / "suspended_history.json").read_text(encoding="utf-8"))
    assert "00626" not in susp_nonflat["by_date"].get("2026-11-26", [])


def test_hkmom_backfill_covers_newly_selected_constituent_after_month_end_rebalance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 7 Item 2:
    - State is frozen BEFORE month-end (2026-11-27), with saved rebalance_snapshots only up to 2026-10-30.
    - During catch-up to 2026-12-03, month-end rebalance 2026-11-30 newly adds constituent 00051
      (NOT in 2026-10-30 frozen holdings or constituents.json).
    - 00051 was suspended on 2026-12-01 (bar missing, no prior suspension record) and resumed on 2026-12-02/12-03.
    - Backfill MUST inspect actual per-day participating constituents (including 00051 from 2026-11-30
      onwards) BEFORE completeness check: valid Yahoo suspension shape backfills 2026-12-01 and
      advances freeze to 2026-12-03; missing OHLC price in Yahoo fails without freezing."""
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-03")]
    assert "2026-10-30" in sessions
    assert "2026-11-27" in sessions
    assert "2026-11-30" in sessions
    assert "2026-12-01" in sessions
    assert "2026-12-02" in sessions
    assert "2026-12-03" in sessions
    idx_1127 = sessions.index("2026-11-27")
    idx_1130 = sessions.index("2026-11-30")
    new_code = "00051"

    def _run_rebal_catchup(
        subdir: str,
        yf_rows_00051: list[tuple[str, float, float, float, float, float]],
    ) -> tuple[dict[str, Any], Path, Any, Any, float]:
        st_dir = tmp_path / subdir
        out_dir = tmp_path / f"{subdir}_out"
        hist_dir = st_dir / "hist"
        hist_dir.mkdir(parents=True, exist_ok=True)
        codes = [f"{i:05d}" for i in range(1, 61)]  # 00001..00060; 00051 is outside initial held[:50]
        old_held = codes[:50]
        assert new_code not in old_held

        uni_rows = []
        close_1130_new = 0.0
        for idx, c in enumerate(codes):
            rows = []
            for t_i, dt_str in enumerate(sessions):
                if c == new_code:
                    # Strong smooth momentum so 00051 ranks #1 at 2026-11-30 rebalance
                    px = round(10.0 * math.exp(0.0045 * t_i) * (1.0 + 0.004 * math.sin(t_i * 0.6)), 4)
                    if dt_str == "2026-11-30":
                        close_1130_new = px
                    if dt_str == "2026-12-01":
                        continue  # suspended on 2026-12-01, resumed 12-02 & 12-03
                else:
                    px = round(
                        10.0
                        * (1.0 + 0.0002 * t_i * (1 + (idx % 5) * 0.1))
                        * (1.0 + 0.015 * math.sin(t_i * 0.7 + idx)),
                        4,
                    )
                rows.append([dt_str, px, px, round(px * 1.01, 4), round(px * 0.99, 4), 100000, 300.0])
            pd.DataFrame(
                rows,
                columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
            ).to_csv(hist_dir / f"{c}.csv", index=False)
            last_px = rows[-1][2]
            uni_rows.append(
                {
                    "code": c,
                    "name": f"HK{c}",
                    "subcat": "Equity Securities (Main Board)",
                    "ccy": "HKD",
                    "is_ggt": idx >= 54,  # 00051 (idx=50) is non-GGT AND smallcap
                    "price": last_px,
                    "prev_close": round(last_px * 0.99, 4),
                    "volume": 500000.0,
                    "turnover": 2000000.0,
                    "trading_status": "",
                    "quote_date": "2026-12-03",
                    "mktcap_e8": 25.0,
                    "name_cn": f"港股{c}",
                }
            )
        uni_df = pd.DataFrame(uni_rows)
        uni_df.to_csv(st_dir / "universe.csv", index=False)
        (st_dir / "ggt_list.json").write_text(
            json.dumps({c: f"港股{c}" for c in codes[54:]}), encoding="utf-8"
        )
        # Seed saved constituents.json & frozen_history.json up to 2026-11-27 (rebalances only up to 2026-10-30)
        (st_dir / "constituents.json").write_text(
            json.dumps(
                {
                    "smallcap": [{"code": c, "weight": 0.02} for c in old_held],
                    "nonggt": [{"code": c, "weight": 0.02} for c in old_held],
                }
            ),
            encoding="utf-8",
        )
        mes_pre = hkmom_compute.month_ends(sessions[: idx_1127 + 1], as_of_date=date(2026, 11, 27))
        rebal_dates_pre = [d for d in mes_pre if sessions.index(d) >= 273]
        assert rebal_dates_pre[-1] == "2026-10-30"
        rebal_start = sessions.index(rebal_dates_pre[0])
        frozen_dates = sessions[rebal_start : idx_1127 + 1]
        seed_frozen = {
            "launch_date": rebal_dates_pre[0],
            "rebalance_snapshots": {
                d: {"smallcap": old_held, "nonggt": old_held} for d in rebal_dates_pre
            },
            "frozen_series": {
                "dates": frozen_dates,
                "smallcap": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
                "nonggt": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
                "hsi": [1000.0 + i * 0.5 for i in range(len(frozen_dates))],
            },
        }
        (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")

        uni_meta = {
            "target_trade_date": "2026-12-03",
            "preserved_previous_universe": False,
            "stale": False,
            "errors": [],
            "suspended_codes": [],
            "suspended_spot": {},
            "source_statuses": {
                "hkex_universe": feeds_common.make_source_result(
                    "hkex_universe", ok=True, stale=False, as_of="2026-12-03", label="港交所证券列表"
                ).to_dict(),
                "ggt_list": feeds_common.make_source_result(
                    "ggt_list", ok=True, stale=False, as_of="2026-12-03", label="深交所港股通名单"
                ).to_dict(),
                "hk_spot": feeds_common.make_source_result(
                    "hk_spot", ok=True, stale=False, as_of="2026-12-03", label="港股现货行情"
                ).to_dict(),
            },
        }
        (st_dir / "universe_meta.json").write_text(json.dumps(uni_meta), encoding="utf-8")

        hsi_rows = [[d, 20000.0, 20000.0 + i * 10.0] for i, d in enumerate(sessions)]
        hsi_resp = create_autospec(requests.Response, instance=True)
        hsi_resp.json.return_value = {"data": {"hkHSI": {"day": hsi_rows}}}
        hsi_sess = create_autospec(requests.Session, instance=True)
        hsi_sess.get.return_value = hsi_resp

        # Resolve sentinel -1.0 in yf_rows_00051 to actual close_1130_new
        resolved_rows = [
            (
                d,
                close_1130_new if o == -1.0 else o,
                close_1130_new if h == -1.0 else h,
                close_1130_new if lo == -1.0 else lo,
                close_1130_new if cl == -1.0 else cl,
                v,
            )
            for d, o, h, lo, cl, v in yf_rows_00051
        ]
        yf_df = pd.DataFrame(
            [
                {"Open": o, "High": h, "Low": lo, "Close": cl, "Volume": v}
                for _, o, h, lo, cl, v in resolved_rows
            ],
            index=pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Hong_Kong") for d, *_ in resolved_rows]),
        )
        fake_ticker = create_autospec(yf.Ticker, instance=True)
        fake_ticker.history.return_value = yf_df
        ticker_ctor = create_autospec(yf.Ticker, return_value=fake_ticker)
        fake_yf = SimpleNamespace(Ticker=ticker_ctor)

        def _fake_build_universe(**kw: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
            hkmom_fetch.record_suspended_for_date(st_dir, "2026-12-03", [], source="hk_spot")
            return uni_df.copy(), uni_meta

        hist_ok = {
            "done": len(codes),
            "total": len(codes),
            "errors": [],
            "n_errors": 0,
            "stale": False,
            "source_status": feeds_common.make_source_result(
                "hk_history", ok=True, stale=False, as_of="2026-12-03", label="港股历史日线"
            ).to_dict(),
        }
        orig_compute_all = hkmom_compute.compute_all
        dt_1203 = datetime(2026, 12, 3, 16, 35, 0, tzinfo=feeds_common.HK_TZ)

        monkeypatch.setattr(
            hkmom_run,
            "build_universe",
            create_autospec(hkmom_fetch.build_universe, side_effect=_fake_build_universe),
        )
        monkeypatch.setattr(
            hkmom_run,
            "download_history",
            create_autospec(hkmom_fetch.download_history, return_value=hist_ok),
        )
        monkeypatch.setattr(
            hkmom_run,
            "compute_all",
            create_autospec(
                orig_compute_all,
                side_effect=lambda **kw: orig_compute_all(**{**kw, "hsi_session": hsi_sess}),
            ),
        )
        monkeypatch.setattr(
            hkmom_run,
            "compute_intraday_snapshot",
            create_autospec(
                hkmom_compute.compute_intraday_snapshot,
                return_value={
                    "as_of": "2026-12-03 16:35:00",
                    "stale": False,
                    "intraday_source_statuses": {
                        "hk_intraday_spot": feeds_common.make_source_result(
                            "hk_intraday_spot", ok=True, stale=False, as_of="2026-12-03", label="港股盘中现货"
                        ).to_dict(),
                        "hsi_spot": feeds_common.make_source_result(
                            "hsi_spot", ok=True, stale=False, as_of="2026-12-03", label="恒生指数现货"
                        ).to_dict(),
                    },
                },
            ),
        )
        monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_1203)
        monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_1203)
        monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_1203)

        assert idx_1130 > idx_1127
        res = hkmom_run.run_daily_job(
            state_dir=st_dir,
            output_dir=out_dir,
            as_of_date=date(2026, 12, 3),
            yf_module=fake_yf,
        )
        return res, st_dir, ticker_ctor, fake_ticker, close_1130_new

    # Case A: Real Yahoo suspension shape on 2026-12-01 for newly added 00051 -> backfills & advances to 2026-12-03!
    res_ok, st_ok, ticker_ok, fake_tk_ok, _ = _run_rebal_catchup(
        "rebal_new_constituent_susp_ok",
        [
            ("2026-11-30", -1.0, -1.0, -1.0, -1.0, 500000.0),
            ("2026-12-01", -1.0, -1.0, -1.0, -1.0, 0.0),
            ("2026-12-02", 50.0, 51.0, 49.0, 50.5, 600000.0),
        ],
    )
    ticker_ok.assert_called_once_with("0051.HK")
    fake_tk_ok.history.assert_called_once()
    assert fake_tk_ok.history.call_args.kwargs["keepna"] is True
    assert fake_tk_ok.history.call_args.kwargs["repair"] is False
    assert res_ok["ok"] is True
    assert res_ok["publish_ok"] is True
    assert res_ok["froze"] is True
    assert res_ok["last_date"] == "2026-12-03"
    assert res_ok["missing_held_by_index"] == {"smallcap": [], "nonggt": []}

    frozen_ok = json.loads((st_ok / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_ok["frozen_series"]["dates"][-1] == "2026-12-03"
    assert new_code in frozen_ok["rebalance_snapshots"]["2026-11-30"]["smallcap"]
    assert new_code in frozen_ok["rebalance_snapshots"]["2026-11-30"]["nonggt"]
    susp_ok = json.loads((st_ok / "suspended_history.json").read_text(encoding="utf-8"))
    assert susp_ok["by_date"]["2026-12-01"] == [new_code]
    assert susp_ok["sources"]["2026-12-01"][new_code] == "yahoo_daily"

    # Case B: Newly added 00051 has missing OHLC (with Volume=0) in Yahoo on 2026-12-01 -> NOT recorded, MUST FAIL!
    nan = float("nan")
    res_fail, st_fail, ticker_fail, _, _ = _run_rebal_catchup(
        "rebal_new_constituent_susp_missing_px",
        [
            ("2026-11-30", -1.0, -1.0, -1.0, -1.0, 500000.0),
            ("2026-12-01", nan, nan, nan, -1.0, 0.0),
        ],
    )
    ticker_fail.assert_called_once_with("0051.HK")
    assert res_fail["ok"] is False
    assert res_fail["publish_ok"] is False
    assert res_fail["froze"] is False
    assert new_code in res_fail["missing_held_by_index"]["smallcap"]
    frozen_fail = json.loads((st_fail / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_fail["frozen_series"]["dates"][-1] == "2026-11-27"
    assert "2026-11-30" not in frozen_fail["rebalance_snapshots"]


def test_hkmom_missing_suspension_record_before_frozen_date_fails_and_not_counted_in_21_days(
    tmp_path: Path,
) -> None:
    """Round 6 Item 1 (Grok suspected item): Trading days between a held stock's last valid bar
    and frozen_last_date MUST also have per-day suspension records; missing any day before
    frozen_last_date fails as missing instead of counting toward 21-day removal."""
    xhkg = xcals.get_calendar("XHKG")
    sessions_21d = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-12-04")]
    st_dir = tmp_path / "hk_pre_freeze_gap"
    _, hsi_sess = _seed_hk_panel_with_00626(st_dir, sessions_21d, suspend_days=21)

    # Drop one pre-freeze day's suspension record (day 5 of the 20 pre-freeze days), while keeping
    # frozen_last_date (day 20) in suspended_on_last_date and target day (day 21) in current spot.
    f_path = st_dir / "frozen_history.json"
    f_doc = json.loads(f_path.read_text(encoding="utf-8"))
    pre_freeze_days = sorted(f_doc["suspended_by_date"].keys())
    assert len(pre_freeze_days) == 20
    dropped_day = pre_freeze_days[4]
    assert dropped_day < f_doc["frozen_series"]["dates"][-1]
    del f_doc["suspended_by_date"][dropped_day]
    f_path.write_text(json.dumps(f_doc), encoding="utf-8")

    res = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 12, 4), hsi_session=hsi_sess, persist=False
    )
    assert res["meta"]["ok"] is False
    assert res["can_freeze"] is False
    assert "00626" in res["meta"]["missing_held_by_index"]["smallcap"]
    assert res["meta"]["removed_suspended_codes"] == []
    assert res["series"]["dates"][-1] == sessions_21d[-2]
