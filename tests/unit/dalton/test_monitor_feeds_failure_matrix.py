"""Unified parametrized failure-matrix & end-to-end freeze tests for all 3 monitor dashboards."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest
import requests
import yfinance as yf

from scripts.dalton.monitor_feeds import common as feeds_common
from scripts.dalton.monitor_feeds.ashare_monitor import collect as ashare_collect
from scripts.dalton.monitor_feeds.ashare_monitor import render as ashare_render
from scripts.dalton.monitor_feeds.ashare_monitor import run as ashare_run
from scripts.dalton.monitor_feeds.hk_momentum import compute as hkmom_compute
from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkmom_fetch
from scripts.dalton.monitor_feeds.hk_momentum import render as hkmom_render
from scripts.dalton.monitor_feeds.hk_momentum import run as hkmom_run
from scripts.dalton.monitor_feeds.pm_cockpit import collect as pm_collect
from scripts.dalton.monitor_feeds.pm_cockpit import render as pm_render
from scripts.dalton.monitor_feeds.pm_cockpit import run as pm_run

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "monitor_feeds"


# ---------------------------------------------------------------------------
# Shared Substitute Network Builders
# ---------------------------------------------------------------------------


def _make_pm_sdks(cfg: dict[str, Any]):
    dates = pd.bdate_range("2023-01-02", "2026-11-16")
    tickers = pm_collect.us_tickers(cfg)
    cols = pd.MultiIndex.from_product([["Adj Close", "Close"], tickers])
    data: dict[tuple[str, str], np.ndarray] = {}
    rng = np.random.default_rng(7)
    for tk in tickers:
        drift = 0.0005 if tk in ("XLK", "SMH", "SPY") else 0.0002
        series = 100.0 * np.exp(np.cumsum(drift + rng.normal(0.0, 0.004, len(dates))))
        if tk == "^VIX":
            series = np.full(len(dates), 16.5)
        elif tk == "^VIX3M":
            series = np.full(len(dates), 19.2)
        data[("Adj Close", tk)] = series
        data[("Close", tk)] = series * 0.99
    us_df = pd.DataFrame(data, index=dates, columns=cols)

    class _FakeAk:
        def stock_zh_index_daily(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(
                {"date": dates.strftime("%Y-%m-%d"), "close": np.linspace(3500.0, 4050.0, len(dates))}
            )

        def stock_zh_index_daily_em(self, symbol: str) -> pd.DataFrame:
            return self.stock_zh_index_daily(symbol)

        def sw_index_first_info(self) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {"行业代码": "801010.SI", "行业名称": "农林牧渔", "成份个数": 80, "TTM(滚动)市盈率": 22.0, "市净率": 2.4, "静态股息率": 1.5},
                    {"行业代码": "801080.SI", "行业名称": "电子", "成份个数": 120, "TTM(滚动)市盈率": 48.0, "市净率": 3.8, "静态股息率": 0.8},
                ]
            )

        def sw_index_second_info(self) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {"行业代码": "801081.SI", "行业名称": "半导体", "上级行业": "电子", "成份个数": 45, "TTM(滚动)市盈率": 65.0, "市净率": 4.2, "静态股息率": 0.4},
                    {"行业代码": "801012.SI", "行业名称": "种植业", "上级行业": "农林牧渔", "成份个数": 12, "TTM(滚动)市盈率": 950.0, "市净率": 1.9, "静态股息率": 1.2},
                ]
            )

        def index_hist_sw(self, symbol: str, period: str = "day") -> pd.DataFrame:
            mult = 1.15 if symbol in ("801080", "801081") else 0.95
            closes = np.linspace(1000.0, 1000.0 * mult, len(dates))
            return pd.DataFrame(
                {"日期": dates.strftime("%Y-%m-%d"), "收盘": closes, "成交额": np.full(len(dates), 120.0)}
            )

        def index_analysis_week_month_sw(self, symbol: str = "week") -> pd.DataFrame:
            return pd.DataFrame({"date": ["2025-12-19", "2025-12-26"]})

        def index_analysis_weekly_sw(self, symbol: str, date: str) -> pd.DataFrame:
            codes = ["801010", "801080"] if symbol == "一级行业" else ["801081", "801012"]
            return pd.DataFrame(
                [
                    {"指数代码": f"{c}.SI", "市盈率": 24.0 + i, "市净率": 2.3, "股息率": 1.4}
                    for i, c in enumerate(codes)
                ]
            )

        def index_analysis_daily_sw(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            codes = ["801010", "801080"] if symbol == "一级行业" else ["801081", "801012"]
            return pd.DataFrame(
                [
                    {"指数代码": f"{c}.SI", "发布日期": "2026-11-16", "市盈率": 25.0 + i, "市净率": 2.5, "股息率": 1.3}
                    for i, c in enumerate(codes)
                ]
            )

    real_ak = _FakeAk()
    fake_yf = SimpleNamespace(download=create_autospec(yf.download, side_effect=lambda *_a, **_kw: us_df.copy()))
    fake_ak = SimpleNamespace(
        stock_zh_index_daily=create_autospec(real_ak.stock_zh_index_daily, side_effect=real_ak.stock_zh_index_daily),
        stock_zh_index_daily_em=create_autospec(real_ak.stock_zh_index_daily_em, side_effect=real_ak.stock_zh_index_daily_em),
        sw_index_first_info=create_autospec(real_ak.sw_index_first_info, side_effect=real_ak.sw_index_first_info),
        sw_index_second_info=create_autospec(real_ak.sw_index_second_info, side_effect=real_ak.sw_index_second_info),
        index_hist_sw=create_autospec(real_ak.index_hist_sw, side_effect=real_ak.index_hist_sw),
        index_analysis_week_month_sw=create_autospec(real_ak.index_analysis_week_month_sw, side_effect=real_ak.index_analysis_week_month_sw),
        index_analysis_weekly_sw=create_autospec(real_ak.index_analysis_weekly_sw, side_effect=real_ak.index_analysis_weekly_sw),
        index_analysis_daily_sw=create_autospec(real_ak.index_analysis_daily_sw, side_effect=real_ak.index_analysis_daily_sw),
    )
    return fake_yf, fake_ak


def _make_ashare_handlers():
    sina_txt = (FIXTURES_DIR / "sina_indices_sample.txt").read_text(encoding="utf-8")
    breadth_json = json.loads((FIXTURES_DIR / "eastmoney_breadth_sample.json").read_text(encoding="utf-8"))
    sohu_html = (FIXTURES_DIR / "sohu_zdt_sample.html").read_text(encoding="utf-8")

    def base_get(url: str, **_kw) -> str:
        if "hq.sinajs.cn" in url:
            return sina_txt
        if "q.stock.sohu.com" in url:
            return sohu_html
        if "query.sse.com.cn" in url:
            if "STAT_DATE=2026-11-" in url and "STAT_DATE=2026-11-15" not in url:
                return 'jsonpCallback(' + json.dumps(
                    {
                        "result": [
                            {"SEC_CODE": "512480", "STAT_DATE": "2026-11-14", "TOT_VOL": "95000"},
                            {"SEC_CODE": "510300", "STAT_DATE": "2026-11-14", "TOT_VOL": "490000"},
                        ]
                    }
                ) + ')'
            return 'jsonpCallback(' + json.dumps(
                {
                    "result": [
                        {"SEC_CODE": "512480", "STAT_DATE": "2026-11-15", "TOT_VOL": "100000"},
                        {"SEC_CODE": "510300", "STAT_DATE": "2026-11-15", "TOT_VOL": "500000"},
                    ]
                }
            ) + ')'
        if "szse.cn" in url:
            return json.dumps([{"data": []}])
        return ""

    def base_json(url: str, **_kw) -> Any:
        if "ulist.np/get" in url:
            return breadth_json
        if "MK0021" in url:
            return {
                "data": {
                    "total": 2,
                    "diff": [
                        {"f12": "512480", "f13": 1, "f14": "半导体ETF", "f2": 1.05, "f3": 2.4, "f6": 2.5e9, "f20": 3.0e10, "f62": 1.8e8, "f184": 7.2},
                        {"f12": "510300", "f13": 1, "f14": "沪深300ETF", "f2": 4.02, "f3": 0.4, "f6": 5.0e9, "f20": 3.0e11, "f62": 2.1e8, "f184": 4.2},
                    ],
                }
            }
        if "qt/stock/get" in url:
            return {"data": {"f50": 145}}
        return {
            "data": {
                "total": 3,
                "diff": [
                    {"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8},
                    {"f12": "600036", "f14": "招商银行", "f3": 1.4, "f6": 2.8e9, "f62": 1.5e8},
                    {"f12": "600000", "f14": "浦发银行", "f3": 0.8, "f6": 1.2e9, "f62": 0.5e8},
                ],
            }
        }

    return base_get, base_json


def _build_hk_substitute_session(
    *,
    sessions: list[str],
    codes: list[str],
    ggt_codes: set[str],
    quote_date: str = "2026/10/30",
    inject: tuple[str, str] | None = None,
) -> requests.Session:
    """Build a substitute requests.Session driving real HK fetch + compute + intraday snapshot."""
    # Pre-build HKEX Excel-style HTML/XLS table (real fetch_hkex_universe calls pd.read_html/xls or TSV/HTML)
    hkex_rows_html = ["<table><tr><th>Stock Code</th><th>Name of Securities</th><th>Sub-Category</th><th>Trading Currency</th></tr>"]
    for i, c in enumerate(codes):
        hkex_rows_html.append(f"<tr><td>{int(c)}</td><td>HK Co {i}</td><td>Equity Securities (Main Board)</td><td>HKD</td></tr>")
    hkex_rows_html.append("</table>")
    hkex_bytes = ("\n".join(hkex_rows_html)).encode("utf-8")

    # Deterministic history bars per code with realistic daily vol (> 0.01 annualized)
    bar_cache: dict[str, list[list[Any]]] = {}
    for idx, c in enumerate(codes):
        base_px = 10.0 + (idx % 12) * 0.5
        slope = 0.001 + (idx % 20) * 0.00025
        bars = []
        for t, ds in enumerate(sessions):
            px = round(base_px * (1.0 + slope * t) * (1.0 + 0.015 * math.sin(t * 0.9 + idx)), 3)
            # amount in HKD so turnover_wan >= MIN_TURNOVER_WAN (100 wan)
            bars.append([ds, f"{px}", f"{px}", f"{px * 1.01:.3f}", f"{px * 0.99:.3f}", "500000", {}, "0", "0", "300.0"])
        bar_cache[c] = bars

    hsi_rows = [
        [ds, f"{20000.0 + t * 5.0:.2f}", f"{20000.0 + t * 5.0:.2f}", f"{20050.0 + t * 5.0:.2f}", f"{19950.0 + t * 5.0:.2f}", "1000000"]
        for t, ds in enumerate(sessions)
    ]

    src_inject, mode_inject = inject or ("none", "success")

    def _resp(*, text: str = "", content: bytes = b"", json_data: Any = None, encoding: str = "utf-8"):
        ns = SimpleNamespace(
            status_code=200,
            text=text,
            content=content,
            encoding=encoding,
            raise_for_status=lambda: None,
            json=lambda: json_data,
        )
        return ns

    def _fake_get(url: str, **_kw):
        # 1. HKEX universe
        if "hkex.com.hk" in url:
            if src_inject == "hkex_universe":
                if mode_inject == "exception":
                    raise RuntimeError("HKEX 503")
                if mode_inject == "empty":
                    return _resp(content=b"<table><tr><th>Stock Code</th><th>Name of Securities</th><th>Sub-Category</th><th>Trading Currency</th></tr></table>")
                if mode_inject == "partial":
                    return _resp(content=b"<table><tr><th>Other</th></tr><tr><td>bad</td></tr></table>")
            return _resp(content=hkex_bytes)

        # 2. SZSE GGT list
        if "szse.cn" in url:
            ggt_sorted = sorted(ggt_codes)
            half = max(1, len(ggt_sorted) // 2)
            p1_items = [{"zqdm": c, "zqjc": f"港股通{c}"} for c in ggt_sorted[:half]]
            p2_items = [{"zqdm": c, "zqjc": f"港股通{c}"} for c in ggt_sorted[half:]]
            is_p2 = "PAGENO=2" in url
            if src_inject == "ggt_list":
                if mode_inject == "exception":
                    raise RuntimeError("SZSE GGT 502")
                if mode_inject == "empty":
                    return _resp(json_data=[{"metadata": {"pagecount": 2}, "data": p1_items if not is_p2 else []}])
                if mode_inject == "partial":
                    bad_p2 = [{"zqdm": "", "zqjc": "坏"}]
                    return _resp(json_data=[{"metadata": {"pagecount": 2}, "data": p1_items if not is_p2 else bad_p2}])
                if mode_inject == "duplicate_or_meta":
                    if not is_p2:
                        return _resp(json_data=[{"metadata": {}, "data": p1_items}])
            meta = {"pagecount": 2 if p2_items else 1}
            if is_p2:
                return _resp(json_data=[{"metadata": meta, "data": p2_items}])
            return _resp(json_data=[{"metadata": meta, "data": p1_items}])

        # 3. Tencent historical qfqday
        if "hkfqkline" in url:
            code = url.split("param=hk")[-1].split(",")[0]
            if src_inject == "hk_history":
                if mode_inject == "exception":
                    raise RuntimeError("QQ hist timeout")
                if mode_inject == "empty":
                    return _resp(json_data={"data": {f"hk{code}": {"qfqday": []}}})
                if mode_inject == "partial":
                    return _resp(json_data={"data": {f"hk{code}": {"qfqday": bar_cache[code][:-1]}}})
                if mode_inject == "duplicate_or_meta":
                    dup = bar_cache[code][:-1] + [bar_cache[code][-2]]
                    return _resp(json_data={"data": {f"hk{code}": {"qfqday": dup}}})
            return _resp(json_data={"data": {f"hk{code}": {"qfqday": bar_cache[code]}}})

        # 4. GTImg HSI daily history
        if "kline/kline" in url and "hkHSI" in url:
            if src_inject == "hsi_history":
                if mode_inject == "exception":
                    raise RuntimeError("GTImg HSI 429")
                if mode_inject == "empty":
                    return _resp(json_data={"data": {"hkHSI": {"day": []}}})
                if mode_inject == "partial":
                    return _resp(json_data={"data": {"hkHSI": {"day": hsi_rows[:-1]}}})
            return _resp(json_data={"data": {"hkHSI": {"day": hsi_rows}}})

        # 5. GTImg HSI spot
        if "r_hkHSI" in url:
            if src_inject == "hsi_spot":
                if mode_inject == "exception":
                    raise RuntimeError("HSI spot timeout")
                if mode_inject == "empty":
                    return _resp(text="")
                if mode_inject == "duplicate_or_meta":
                    bad_fields = ["0"] * 47
                    bad_fields[1], bad_fields[3], bad_fields[4], bad_fields[30], bad_fields[32] = (
                        "恒生指数", "20500.0", "20400.0", "1999/01/01 16:00:00", "0.49"
                    )
                    return _resp(text='v_r_hkHSI="' + "~".join(bad_fields) + '";')
            ts_str = f"{quote_date} 16:08:00"
            fields = ["0"] * 47
            fields[1], fields[3], fields[4], fields[30], fields[32] = "恒生指数", "20500.0", "20400.0", ts_str, "0.49"
            return _resp(text=f'v_r_hkHSI="{"~".join(fields)}";')

        # 6. GTImg batch stock quotes
        if "qt.gtimg.cn" in url:
            q_part = url.split("q=")[-1]
            req_codes = [p.replace("r_hk", "") for p in q_part.split(",") if p]
            if src_inject in ("hk_spot", "hk_intraday_spot"):
                if mode_inject == "exception":
                    raise RuntimeError("GTImg spot 502")
                if mode_inject == "empty":
                    return _resp(text="")
                if mode_inject == "partial":
                    req_codes = req_codes[: max(1, len(req_codes) // 5)]
            lines = []
            ts_str = f"{quote_date} 16:08:00"
            if src_inject in ("hk_spot", "hk_intraday_spot") and mode_inject == "duplicate_or_meta":
                ts_str = "2026/10/01 16:08:00"
            for c in req_codes:
                idx = int(c) - 1
                mktcap = 20.0 if idx < 55 else 150.0  # first 55 stay smallcap < 50e8 HKD after drift
                fields = ["0"] * 47
                fields[1], fields[3], fields[4] = f"HK{c}", "15.0", "14.8"
                fields[30], fields[32], fields[37], fields[45] = ts_str, "1.35", "2000", f"{mktcap}"
                lines.append(f'v_r_hk{c}="{"~".join(fields)}"')
            return _resp(text=";".join(lines))

        raise RuntimeError(f"Unexpected URL in substitute HK session: {url}")

    sess = create_autospec(requests.Session, instance=True)
    sess.get.side_effect = _fake_get
    return sess


# ---------------------------------------------------------------------------
# 1. Req #1 Regression Fix: Real Month-End Success Saves Series & Freezes
# ---------------------------------------------------------------------------


def test_hkmom_month_end_success_e2e_real_compute_and_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1: real compute_all + run_daily_job on month-end 2026-10-30 saves series & freezes."""
    st_dir = tmp_path / "hk_me_ok"
    out_dir = tmp_path / "hk_me_out"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True)

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    assert sessions[-1] == "2026-10-30"
    prior_session = sessions[-2]
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])  # first 55 are smallcap non-GGT; rest include nonggt & ggt

    # Seed prior state ending 2026-10-29 with September rebalance frozen
    prior_ok = "2026-10-29 16:35:00 北京时间"
    sep_me = "2026-09-30"
    seed_frozen = {
        "launch_date": "2026-09-30",
        "rebalance_snapshots": {
            sep_me: {"smallcap": codes[:50], "nonggt": codes[:50]},
        },
        "frozen_series": {
            "dates": sessions[:-1],
            "smallcap": [1000.0 + i for i in range(len(sessions) - 1)],
            "nonggt": [1000.0 + i for i in range(len(sessions) - 1)],
            "hsi": [1000.0 + i for i in range(len(sessions) - 1)],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(seed_frozen["frozen_series"]), encoding="utf-8")
    (st_dir / "index_meta.json").write_text(
        json.dumps({"last_date": prior_session, "launch_date": "2026-09-30", "last_success_at": prior_ok}),
        encoding="utf-8",
    )

    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: sub_sess)
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "compute_all", hkmom_compute.compute_all, raising=False)
    # Route compute_all & compute_intraday_snapshot HSI/spot sessions through the same substitute session
    orig_compute_all = hkmom_compute.compute_all
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        lambda **kw: orig_compute_all(**{**kw, "hsi_session": sub_sess}),
    )
    orig_intraday = hkmom_compute.compute_intraday_snapshot
    fixed_close_dt = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        lambda **kw: orig_intraday(**{**kw, "now": fixed_close_dt, "session": sub_sess}),
    )
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: fixed_close_dt)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: fixed_close_dt)

    # Provide DataFrame cleanly without requiring optional lxml html parser
    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )
    orig_build_uni = hkmom_fetch.build_universe
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_build_uni(**{**kw, "session": sub_sess}))
    orig_dl = hkmom_fetch.download_history
    monkeypatch.setattr(hkmom_run, "download_history", lambda c_list, **kw: orig_dl(c_list, **{**kw, "session": sub_sess}))

    res = hkmom_run.run_daily_job(
        state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30)
    )
    assert res["ok"] is True
    assert res["publish_ok"] is True
    assert res["froze"] is True
    assert res["last_date"] == "2026-10-30"
    assert res["last_success_at"] == "2026-10-30 16:35:00 北京时间"

    saved_series = json.loads((st_dir / "index_series.json").read_text(encoding="utf-8"))
    assert saved_series["dates"][-1] == "2026-10-30"
    assert len(saved_series["smallcap"]) == len(sessions)
    assert len(saved_series["hsi"]) == len(sessions)

    saved_frozen = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert "2026-10-30" in saved_frozen["rebalance_snapshots"]
    assert len(saved_frozen["rebalance_snapshots"]["2026-10-30"]["smallcap"]) == 50
    assert saved_frozen["frozen_series"]["dates"][-1] == "2026-10-30"

    saved_status = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    assert saved_status["overall"]["ok"] is True
    assert saved_status["overall"]["last_success_at"] == "2026-10-30 16:35:00 北京时间"
    assert (out_dir / "index.html").is_file()
    assert "本月调仓未完成" not in (out_dir / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. PM Cockpit Parametrized Failure Matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "mode", "expected_exit", "expect_advance"),
    [
        ("us", "exception", 1, False),
        ("us", "empty", 1, False),
        ("us", "partial", 1, False),
        ("us", "duplicate_or_meta", 1, False),
        ("us", "success", 0, True),
        ("cn", "exception", 0, False),
        ("cn", "empty", 0, False),
        ("cn", "partial", 0, False),
        ("cn", "duplicate_or_meta", 0, False),
        ("cn", "success", 0, True),
        ("cn_valuation", "exception", 0, False),
        ("cn_valuation", "empty", 0, False),
        ("cn_valuation", "partial", 0, False),
        ("cn_valuation", "duplicate_or_meta", 0, False),
        ("cn_valuation", "success", 0, True),
        ("pm_compute", "exception", 1, False),
        ("pm_render", "exception", 1, False),
    ],
)
def test_pm_cockpit_parametrized_failure_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    mode: str,
    expected_exit: int,
    expect_advance: bool,
) -> None:
    """Real pm_cockpit.run.main() across all sources & failure modes."""
    monkeypatch.setattr(pm_collect.time, "sleep", lambda _s: None)
    cfg = pm_collect.load_config()
    st_dir = tmp_path / "pm_state"
    out_dir = tmp_path / "pm_out"

    # Seed one clean baseline run first (using real collect_all + compute_panels + render)
    fake_yf_seed, fake_ak_seed = _make_pm_sdks(cfg)
    mod_yf = ModuleType("yfinance")
    mod_yf.download = fake_yf_seed.download  # type: ignore[attr-defined]
    mod_ak = ModuleType("akshare")
    for k, v in vars(fake_ak_seed).items():
        setattr(mod_ak, k, v)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", mod_yf)
    monkeypatch.setitem(__import__("sys").modules, "akshare", mod_ak)
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    assert pm_run.main() == 0

    # Pin distinguishable prior_ok timestamp across status.json
    prior_ok = "2026-11-15 16:00:00 北京时间"
    sdoc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    for ent in sdoc["sources"].values():
        ent["last_success_at"] = prior_ok
    sdoc["overall"]["last_success_at"] = prior_ok
    (st_dir / "status.json").write_text(json.dumps(sdoc, ensure_ascii=False), encoding="utf-8")

    # Configure fault injection for the parameterized case (force cn_valuation week so it runs)
    fake_yf, fake_ak = _make_pm_sdks(cfg)
    orig_yf_dl = fake_yf.download.side_effect

    if source == "us":
        if mode == "exception":
            fake_yf.download.side_effect = RuntimeError("yfinance 503")
        elif mode == "empty":
            fake_yf.download.side_effect = lambda *_a, **_kw: pd.DataFrame()
        elif mode == "partial":
            def _nan_xlk(*args, **kwargs):
                df = orig_yf_dl(*args, **kwargs)
                for c in df.columns:
                    if c[1] == "XLK":
                        df[c] = float("nan")
                return df
            fake_yf.download.side_effect = _nan_xlk
        elif mode == "duplicate_or_meta":
            def _drop_xlk(*args, **kwargs):
                df = orig_yf_dl(*args, **kwargs)
                return df[[c for c in df.columns if c[1] != "XLK"]]
            fake_yf.download.side_effect = _drop_xlk

    if source == "cn":
        orig_hist_sw = fake_ak.index_hist_sw.side_effect
        if mode == "exception":
            fake_ak.stock_zh_index_daily.side_effect = RuntimeError("sina down")
            fake_ak.stock_zh_index_daily_em.side_effect = RuntimeError("em down")
        elif mode == "empty":
            fake_ak.sw_index_first_info.side_effect = lambda: pd.DataFrame()
        elif mode == "partial":
            fake_ak.index_hist_sw.side_effect = (
                lambda symbol, period="day": pd.DataFrame() if symbol == "801081" else orig_hist_sw(symbol, period=period)
            )
        elif mode == "duplicate_or_meta":
            fake_ak.sw_index_second_info.side_effect = lambda: pd.DataFrame()

    if source == "cn_valuation":
        # Add an uncollected week 2026-01-02 so weekly valuation executes!
        fake_ak.index_analysis_week_month_sw.side_effect = None
        fake_ak.index_analysis_week_month_sw.return_value = pd.DataFrame({"date": ["2025-12-19", "2025-12-26", "2026-01-02"]})
        orig_weekly = fake_ak.index_analysis_weekly_sw.side_effect
        orig_daily_val = fake_ak.index_analysis_daily_sw.side_effect
        if mode == "exception":
            fake_ak.index_analysis_weekly_sw.side_effect = RuntimeError("weekly val 502")
        elif mode == "empty":
            fake_ak.index_analysis_weekly_sw.side_effect = lambda symbol, date: pd.DataFrame() if date == "20260102" else orig_weekly(symbol, date)
        elif mode == "partial":
            # Only L1 daily valuation returns empty while L2 succeeds -> must fail!
            fake_ak.index_analysis_daily_sw.side_effect = lambda symbol, start_date, end_date: (
                pd.DataFrame() if symbol == "一级行业" else orig_daily_val(symbol, start_date, end_date)
            )
        elif mode == "duplicate_or_meta":
            fake_ak.index_analysis_week_month_sw.return_value = pd.DataFrame({"bad_col": ["2026-01-02"]})

    mod_yf.download = fake_yf.download  # type: ignore[attr-defined]
    for k, v in vars(fake_ak).items():
        setattr(mod_ak, k, v)

    # Verify status.json does NOT advance prematurely before compute & render complete (Req #8)!
    orig_compute_panels = pm_run.compute_panels

    def _check_compute_panels(**kw):
        on_disk = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
        assert on_disk["overall"]["last_success_at"] == prior_ok, "status.json updated prematurely before compute!"
        if source == "pm_compute":
            raise RuntimeError("boom in compute")
        return orig_compute_panels(**kw)

    monkeypatch.setattr(pm_run, "compute_panels", _check_compute_panels)

    orig_render = pm_render.render_pm_panels

    def _check_render(**kw):
        on_disk = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
        assert on_disk["overall"]["last_success_at"] == prior_ok, "status.json updated prematurely before render!"
        if source == "pm_render":
            raise RuntimeError("disk full on PM render")
        return orig_render(**kw)

    monkeypatch.setattr(pm_run, "render_pm_panels", _check_render)
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])

    exit_code = pm_run.main()
    assert exit_code == expected_exit

    final_doc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    if expect_advance:
        assert final_doc["overall"]["ok"] is True
        assert final_doc["overall"]["last_success_at"] != prior_ok
    else:
        assert final_doc["overall"]["ok"] is False
        assert final_doc["overall"]["last_success_at"] == prior_ok
        if source in final_doc["sources"]:
            assert final_doc["sources"][source]["last_success_at"] == prior_ok

    if source == "cn_valuation" and mode == "empty":
        prog = json.loads((st_dir / "raw" / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
        assert "2026-01-02" not in prog.get("done", {}), "Empty weekly valuation must stay pending!"

    if source not in ("pm_compute", "pm_render") and not expect_advance:
        target_html = (out_dir / "pm-market-state" / "index.html") if source == "us" else (out_dir / "cn-sector-rotation" / "index.html")
        assert "stale-banner show" in target_html.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. A-share Intraday Parametrized Failure Matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "mode", "expected_exit", "expect_advance"),
    [
        ("indices", "exception", 1, False),
        ("indices", "empty", 1, False),
        ("indices", "success", 0, True),
        ("breadth", "exception", 1, False),
        ("breadth", "empty", 1, False),
        ("turnover", "partial", 1, False),
        ("etf_radar", "exception", 0, False),
        ("etf_radar", "empty", 0, False),
        ("etf_radar", "duplicate_or_meta", 0, False),
        ("etf_vratio", "exception", 0, False),
        ("etf_vratio", "empty", 0, False),
        ("industry", "exception", 0, False),
        ("industry", "partial", 0, False),
        ("daily_history", "exception", 0, False),
        ("daily_history", "empty", 0, False),
        ("etf_official_shares", "exception", 0, False),
        ("etf_official_shares", "empty", 0, False),
        ("etf_official_shares", "partial", 0, False),
        ("etf_official_shares", "duplicate_or_meta", 0, False),
        ("html_render", "exception", 1, False),
    ],
)
def test_ashare_parametrized_failure_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    mode: str,
    expected_exit: int,
    expect_advance: bool,
) -> None:
    """Real AShareCollector + ashare_monitor.run.main() across all sources & modes."""
    monkeypatch.setattr(ashare_collect.time, "sleep", lambda _s: None)
    orig_http_get = ashare_collect.default_http_get
    orig_http_get_json = ashare_collect.default_http_get_json
    base_get, base_json = _make_ashare_handlers()
    st_dir = tmp_path / "cn_state"
    out_dir = tmp_path / "cn_out"
    t0 = datetime(2026, 11, 16, 14, 30, 0, tzinfo=feeds_common.CN_TZ)
    now_holder = {"dt": t0}

    # 1. Clean baseline run via REAL ashare_monitor.run.main()
    monkeypatch.setattr(ashare_collect, "default_http_get", create_autospec(orig_http_get, side_effect=base_get))
    monkeypatch.setattr(ashare_collect, "default_http_get_json", create_autospec(orig_http_get_json, side_effect=base_json))
    monkeypatch.setattr(ashare_collect, "now_cn", lambda: now_holder["dt"])
    monkeypatch.setattr(ashare_run, "AShareCollector", lambda state_dir=None: ashare_collect.AShareCollector(state_dir=state_dir, http_get=ashare_collect.default_http_get, http_get_json=ashare_collect.default_http_get_json, now_fn=lambda: now_holder["dt"]))
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    assert ashare_run.main() == 0

    prior_ok = "2026-11-16 14:30:00"
    assert json.loads((st_dir / "status.json").read_text(encoding="utf-8"))["overall"]["last_success_at"] == prior_ok

    # 2. Advance clock +1 min and inject fault cleanly
    now_holder["dt"] = t0 + timedelta(minutes=1)

    def _fault_get(url: str, **kw) -> str:
        if source == "indices" and "hq.sinajs.cn" in url:
            if mode == "exception":
                raise RuntimeError("Sina 502")
            if mode == "empty":
                return ""
        if source == "turnover" and "hq.sinajs.cn" in url and mode == "partial":
            return "\n".join(base_get(url, **kw).splitlines()[:-1])  # drop s_sz399106
        if source == "daily_history" and "q.stock.sohu.com" in url:
            if mode == "exception":
                raise RuntimeError("Sohu timeout")
            if mode == "empty":
                return "<html><body></body></html>"
        if source == "etf_official_shares" and "query.sse.com.cn" in url:
            if mode == "exception":
                raise RuntimeError("SSE 503")
            if mode == "empty":
                return 'jsonpCallback({"result":[]})'
            if mode == "partial":
                # Omit 510300 while 512480 present -> incomplete representative set!
                return 'jsonpCallback({"result":[{"SEC_CODE":"512480","STAT_DATE":"2026-11-15","TOT_VOL":"100000"}]})'
            if mode == "duplicate_or_meta":
                # Only 2026-11-15 returned even for prior date queries -> lacks previous trading day!
                return 'jsonpCallback(' + json.dumps({"result": [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-15", "TOT_VOL": "100000"}, {"SEC_CODE": "510300", "STAT_DATE": "2026-11-15", "TOT_VOL": "500000"}]}) + ')'
        return base_get(url, **kw)

    def _fault_json(url: str, **kw) -> Any:
        if source == "breadth" and "ulist.np/get" in url:
            if mode == "exception":
                raise RuntimeError("Breadth 502")
            if mode == "empty":
                return {"data": {"diff": []}}
        if source == "etf_radar" and "MK0021" in url:
            if mode == "exception":
                raise RuntimeError("ETF 502")
            if mode == "empty":
                return {"data": {"total": 2, "diff": []}}
            if mode == "duplicate_or_meta":
                return {"data": {"total": None, "diff": [{"f12": "512480", "f13": 1, "f14": "半导体ETF", "f2": 1.05, "f3": 2.4, "f6": 2.5e9, "f20": 3.0e10, "f62": 1.8e8}]}}
        if source == "etf_vratio" and "qt/stock/get" in url:
            if mode == "exception":
                raise RuntimeError("vratio timeout")
            if mode == "empty":
                return {"data": {"f50": None}}
        if source == "industry" and ("m%3A0" in url or "m:0" in url):
            if mode == "exception":
                raise RuntimeError("Stock scan 502")
            if mode == "partial":
                if "pn=2" in url:
                    return {"data": {"diff": []}}
                return {"data": {"total": 150, "diff": [{"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8}]}}
        return base_json(url, **kw)

    orig_write = ashare_render.write_ashare_panel

    def _wrapped_write(**kw):
        # status.json MUST NOT have been updated yet before render runs (Req #8)!
        on_disk = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
        assert on_disk["overall"]["last_success_at"] == prior_ok
        if source == "html_render":
            raise RuntimeError("disk full")
        return orig_write(**kw)

    monkeypatch.setattr(ashare_run, "write_ashare_panel", _wrapped_write)
    monkeypatch.setattr(
        ashare_run,
        "AShareCollector",
        lambda state_dir=None: ashare_collect.AShareCollector(
            state_dir=state_dir,
            http_get=create_autospec(orig_http_get, side_effect=_fault_get),
            http_get_json=create_autospec(orig_http_get_json, side_effect=_fault_json),
            now_fn=lambda: now_holder["dt"],
        ),
    )
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    exit_code = ashare_run.main()
    assert exit_code == expected_exit

    sdoc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    if expect_advance:
        assert sdoc["overall"]["ok"] is True
        assert sdoc["overall"]["last_success_at"] == "2026-11-16 14:31:00"
    else:
        assert sdoc["overall"]["ok"] is False
        assert sdoc["overall"]["last_success_at"] == prior_ok
        assert sdoc["sources"][source]["last_success_at"] == prior_ok
        if source != "html_render":
            html_txt = (out_dir / "index.html").read_text(encoding="utf-8")
            assert prior_ok in html_txt


# ---------------------------------------------------------------------------
# 4. HK Momentum Parametrized Failure Matrix + Intraday Daily-Fail Guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "mode", "is_month_end", "expected_exit", "expect_freeze"),
    [
        ("hkex_universe", "exception", True, 1, False),
        ("hkex_universe", "empty", True, 1, False),
        ("ggt_list", "empty", True, 1, False),
        ("ggt_list", "duplicate_or_meta", True, 1, False),
        ("hk_history", "partial", True, 1, False),
        ("hk_history", "duplicate_or_meta", True, 1, False),
        ("hsi_history", "empty", True, 1, False),
        ("hsi_history", "exception", False, 1, False),
        ("hk_intraday_spot", "exception", False, 0, False),
        ("hsi_spot", "duplicate_or_meta", False, 0, False),
        ("html_render", "exception", False, 1, False),
        ("all", "success", False, 0, True),
    ],
)
def test_hkmom_parametrized_failure_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    mode: str,
    is_month_end: bool,
    expected_exit: int,
    expect_freeze: bool,
) -> None:
    """Real HK fetch + compute + intraday + render through hkmom_run.main()."""
    st_dir = tmp_path / "hk_matrix_state"
    out_dir = tmp_path / "hk_matrix_out"
    (st_dir / "hist").mkdir(parents=True)

    target_str = "2026-10-30" if is_month_end else "2026-11-02"
    target_day = date.fromisoformat(target_str)
    quote_date = target_str.replace("-", "/")

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", target_str)]
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])

    prior_ok = "2026-10-29 16:35:00 北京时间"
    seed_frozen = {
        "launch_date": "2026-09-30",
        "rebalance_snapshots": {"2026-09-30": {"smallcap": codes[:50], "nonggt": codes[:50]}},
        "frozen_series": {
            "dates": sessions[:-1],
            "smallcap": [1000.0 + i for i in range(len(sessions) - 1)],
            "nonggt": [1000.0 + i for i in range(len(sessions) - 1)],
            "hsi": [1000.0 + i for i in range(len(sessions) - 1)],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(seed_frozen["frozen_series"]), encoding="utf-8")
    (st_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c, "name": c} for c in codes[:50]], "nonggt": [{"code": c, "name": c} for c in codes[:50]]}),
        encoding="utf-8",
    )
    (st_dir / "index_meta.json").write_text(
        json.dumps({"last_date": sessions[-2], "launch_date": "2026-09-30", "last_success_at": prior_ok}),
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

    daily_inject = (source, mode) if source not in ("hk_intraday_spot", "hsi_spot") else None
    intra_inject = (source, mode) if source in ("hk_intraday_spot", "hsi_spot") else None
    daily_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date=quote_date, inject=daily_inject
    )
    intra_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date=quote_date, inject=intra_inject
    )
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: daily_sess)

    def _hkex_mock(**_kw):
        if source == "hkex_universe":
            if mode == "exception":
                raise RuntimeError("HKEX 503")
            if mode == "empty":
                return pd.DataFrame(columns=["code", "name", "subcat", "ccy"])
            if mode == "partial":
                return pd.DataFrame({"code": ["00001"], "name": ["HK1"], "subcat": ["Other"], "ccy": ["HKD"]})
        return pd.DataFrame(
            {
                "code": codes,
                "name": [f"HK Co {i}" for i in range(len(codes))],
                "subcat": ["Equity Securities (Main Board)"] * len(codes),
                "ccy": ["HKD"] * len(codes),
            }
        )

    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", _hkex_mock)
    orig_uni = hkmom_fetch.build_universe
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": daily_sess}))
    orig_dl = hkmom_fetch.download_history
    monkeypatch.setattr(hkmom_run, "download_history", lambda c_list, **kw: orig_dl(c_list, **{**kw, "session": daily_sess}))
    orig_comp = hkmom_compute.compute_all
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        lambda **kw: orig_comp(**{**kw, "as_of_date": target_day, "hsi_session": daily_sess}),
    )
    fixed_dt = datetime.combine(target_day, datetime.min.time(), tzinfo=feeds_common.HK_TZ).replace(hour=16, minute=35)
    orig_intra = hkmom_compute.compute_intraday_snapshot
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        lambda **kw: orig_intra(**{**kw, "now": fixed_dt, "session": intra_sess}),
    )
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: fixed_dt)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: fixed_dt)

    # Assert status.json still holds prior_ok at render time (Req #8), and inject html_render exception if parameterized!
    orig_write = hkmom_render.write_hkmom_panel

    def _guard_write(**kw):
        on_disk = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
        assert on_disk["overall"]["last_success_at"] == prior_ok, "Premature commit before HK HTML render!"
        if source == "html_render":
            raise RuntimeError("HK disk full")
        return orig_write(**kw)

    monkeypatch.setattr(hkmom_run, "write_hkmom_panel", _guard_write)
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])

    exit_code = hkmom_run.main()
    assert exit_code == expected_exit

    sdoc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    saved_frozen = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    if expect_freeze:
        assert sdoc["overall"]["ok"] is True
        assert sdoc["overall"]["last_success_at"] != prior_ok
        assert saved_frozen["frozen_series"]["dates"][-1] == target_str
    else:
        assert sdoc["overall"]["ok"] is False
        assert sdoc["overall"]["last_success_at"] == prior_ok
        assert saved_frozen == seed_frozen, "Frozen history must NOT be overwritten when any source failed!"
        if source != "html_render" and (out_dir / "index.html").is_file():
            assert prior_ok in (out_dir / "index.html").read_text(encoding="utf-8")


def test_hkmom_intraday_snapshot_does_not_advance_overall_when_daily_stale(
    tmp_path: Path,
) -> None:
    """Requirement 4: intraday snapshot with clean spots STILL preserves prior_ok if daily status.json is stale."""
    st_dir = tmp_path / "hk_daily_stale"
    st_dir.mkdir(parents=True)
    prior_ok = "2026-10-29 16:35:00 北京时间"
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2026-10-20", "2026-10-29")]
    (st_dir / "index_series.json").write_text(
        json.dumps({"dates": sessions, "smallcap": [1000.0 + i for i in range(len(sessions))], "nonggt": [1000.0 + i for i in range(len(sessions))], "hsi": [1000.0 + i for i in range(len(sessions))]}),
        encoding="utf-8",
    )
    (st_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": "00001", "name": "CKH"}], "nonggt": [{"code": "00001", "name": "CKH"}]}),
        encoding="utf-8",
    )
    feeds_common.SourceStatusStore(st_dir).update_sources(
        [
            feeds_common.make_source_result("hsi_history", ok=False, stale=True, reason="HSI历史失败", label="恒生指数历史"),
            feeds_common.make_source_result("index_compute", ok=True, stale=False, label="动量日度计算"),
        ],
        required=["hsi_history", "index_compute"],
        now_str=prior_ok,
    )
    # Seed prior overall last_success_at in status.json
    sdoc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    sdoc["overall"]["last_success_at"] = prior_ok
    (st_dir / "status.json").write_text(json.dumps(sdoc, ensure_ascii=False), encoding="utf-8")

    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=["00001"], ggt_codes=set(), quote_date="2026/10/30"
    )
    snap = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 10, 30, 14, 0, 0, tzinfo=feeds_common.HK_TZ),
        session=sub_sess,
        persist_status=True,
    )
    assert snap["stale"] is True
    assert snap["last_success_at"] == prior_ok
    after_doc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    assert after_doc["overall"]["ok"] is False
    assert after_doc["overall"]["last_success_at"] == prior_ok

    # Also test intraday partial coverage & wrong quote date when series is prior-day
    for bad_mode in ("partial", "duplicate_or_meta"):
        bad_intra = _build_hk_substitute_session(
            sessions=sessions,
            codes=["00001", "00002", "00003", "00004"],
            ggt_codes=set(),
            quote_date="2026/10/30",
            inject=("hk_intraday_spot", bad_mode),
        )
        (st_dir / "constituents.json").write_text(
            json.dumps({
                "smallcap": [{"code": f"{i:05d}", "name": f"H{i}"} for i in range(1, 5)],
                "nonggt": [{"code": f"{i:05d}", "name": f"H{i}"} for i in range(1, 5)],
            }),
            encoding="utf-8",
        )
        snap_bad = hkmom_compute.compute_intraday_snapshot(
            state_dir=st_dir,
            now=datetime(2026, 10, 30, 14, 0, 0, tzinfo=feeds_common.HK_TZ),
            session=bad_intra,
            persist_status=True,
        )
        assert snap_bad["stale"] is True
        assert snap_bad["last_success_at"] == prior_ok


def test_hkmom_held_constituent_missing_spot_or_history_blocks_append_and_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 7: omitting a single held constituent (>50% overall coverage) MUST NOT reweight/append/freeze."""
    st_dir = tmp_path / "hk_held_guard"
    out_dir = tmp_path / "hk_held_out"
    (st_dir / "hist").mkdir(parents=True)

    xhkg = xcals.get_calendar("XHKG")
    # First seed through 2026-10-30 cleanly (day 1)
    sessions_oct = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    sessions_nov = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-02")]
    assert sessions_oct[-1] == "2026-10-30"
    assert sessions_nov[-1] == "2026-11-02"
    codes = [f"{i:05d}" for i in range(1, 121)]
    ggt_codes = set(codes[55:])

    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )

    # Seed day 1 (2026-10-30) cleanly
    sess_oct = _build_hk_substitute_session(
        sessions=sessions_oct, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    orig_uni = hkmom_fetch.build_universe
    orig_dl = hkmom_fetch.download_history
    orig_comp = hkmom_compute.compute_all
    orig_intra = hkmom_compute.compute_intraday_snapshot

    dt_oct = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sess_oct}))
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sess_oct}))
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 10, 30), "hsi_session": sess_oct}))
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_oct, "session": sess_oct}))
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_oct)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_oct)

    res_day1 = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))
    assert res_day1["ok"] is True and res_day1["last_date"] == "2026-10-30"
    prior_ok = res_day1["last_success_at"]
    frozen_oct = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    series_oct = json.loads((st_dir / "index_series.json").read_text(encoding="utf-8"))
    # Pick ONE actively held constituent from the frozen rebalance snapshot (guarantee in portfolio!)
    held_target = frozen_oct["rebalance_snapshots"]["2026-10-30"]["smallcap"][0]

    # Day 2 (2026-11-02): substitute network omits ONLY held_target from spot quotes (119/120 > 50% coverage!)
    sess_nov_full = _build_hk_substitute_session(
        sessions=sessions_nov, codes=codes, ggt_codes=ggt_codes, quote_date="2026/11/02"
    )
    orig_get_nov = sess_nov_full.get.side_effect

    def _get_drop_one_spot(url: str, **kw):
        resp = orig_get_nov(url, **kw)
        if "qt.gtimg.cn" in url and f"r_hk{held_target}" in url:
            # Strip held_target quote from batch while keeping all 119 other quotes intact
            filtered = [seg for seg in resp.text.split(";") if not seg.startswith(f"v_r_hk{held_target}=")]
            resp.text = ";".join(filtered)
        return resp

    sess_nov_missing_spot = create_autospec(requests.Session, instance=True)
    sess_nov_missing_spot.get.side_effect = _get_drop_one_spot

    # 1) build_universe + run_daily_job MUST fail hk_spot (even with 119/120 > 50% coverage!), keep old series/frozen/timestamp
    dt_nov = datetime(2026, 11, 2, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sess_nov_missing_spot}))
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sess_nov_missing_spot}))
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 11, 2), "hsi_session": sess_nov_missing_spot}))
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_nov, "session": sess_nov_missing_spot}))
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_nov)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_nov)

    res_spot_drop = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 11, 2))
    assert res_spot_drop["ok"] is False
    assert res_spot_drop["froze"] is False
    assert res_spot_drop["last_success_at"] == prior_ok
    assert "1 只" in (res_spot_drop["sources"]["hk_spot"]["reason"] or "")
    assert held_target in (res_spot_drop["sources"]["hk_spot"]["reason"] or "")
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_oct
    assert json.loads((st_dir / "index_series.json").read_text(encoding="utf-8")) == series_oct

    # 2) Direct real compute_all when universe.csv has price=NaN (or history missing target date) for held_target ->
    # MUST NOT append 2026-11-02 to series, MUST NOT freeze, MUST fail with missing count & sample code!
    uni_df = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    uni_df.loc[uni_df["code"] == held_target, "price"] = float("nan")
    uni_df.to_csv(st_dir / "universe.csv", index=False)
    comp_missing_spot = orig_comp(
        state_dir=st_dir, as_of_date=date(2026, 11, 2), hsi_session=sess_nov_full, freeze_on_stale=True, update_last_success=True
    )
    assert comp_missing_spot["meta"]["ok"] is False
    assert comp_missing_spot["can_freeze"] is False
    assert comp_missing_spot["series"]["dates"][-1] == "2026-10-30"
    assert held_target in (comp_missing_spot["meta"]["series_stale_reason"] or "")
    assert "1 只" in (comp_missing_spot["meta"]["series_stale_reason"] or "")
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_oct
    assert json.loads((st_dir / "status.json").read_text(encoding="utf-8"))["overall"]["last_success_at"] == prior_ok

    # 3) Recovery when ALL 120 held + universe stocks have valid prices -> appends 2026-11-02 & freezes!
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sess_nov_full}))
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sess_nov_full}))
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 11, 2), "hsi_session": sess_nov_full}))
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_nov, "session": sess_nov_full}))

    res_ok = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 11, 2))
    assert res_ok["ok"] is True
    assert res_ok["froze"] is True
    assert res_ok["last_success_at"] == "2026-11-02 16:35:00 北京时间"
    assert json.loads((st_dir / "index_series.json").read_text(encoding="utf-8"))["dates"][-1] == "2026-11-02"
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))["frozen_series"]["dates"][-1] == "2026-11-02"
