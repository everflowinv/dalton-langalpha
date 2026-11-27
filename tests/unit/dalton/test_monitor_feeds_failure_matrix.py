"""Unified parametrized failure-matrix & end-to-end freeze tests for all 3 monitor dashboards."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import signal
import threading
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
from scripts.dalton.monitor_feeds import run_feeds
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


@pytest.fixture(autouse=True)
def _per_test_timeout():
    """Enforce an explicit 20s timeout on every matrix/E2E test."""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _on_alarm(_signum, _frame):
        raise TimeoutError("monitor_feeds test exceeded 20s timeout")

    prev_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, 20.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prev_handler)


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
            if "STAT_DATE=2026-11-" in url and "STAT_DATE=2026-11-13" not in url:
                return 'jsonpCallback(' + json.dumps(
                    {
                        "result": [
                            {"SEC_CODE": "512480", "STAT_DATE": "2026-11-12", "TOT_VOL": "95000"},
                            {"SEC_CODE": "510300", "STAT_DATE": "2026-11-12", "TOT_VOL": "490000"},
                        ]
                    }
                ) + ')'
            return 'jsonpCallback(' + json.dumps(
                {
                    "result": [
                        {"SEC_CODE": "512480", "STAT_DATE": "2026-11-13", "TOT_VOL": "100000"},
                        {"SEC_CODE": "510300", "STAT_DATE": "2026-11-13", "TOT_VOL": "500000"},
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
                    return _resp(json_data=[{"metadata": {"pagecount": 2, "recordcount": len(ggt_sorted)}, "data": p1_items if not is_p2 else []}])
                if mode_inject == "partial":
                    bad_p2 = [{"zqdm": "", "zqjc": "坏"}]
                    return _resp(json_data=[{"metadata": {"pagecount": 2, "recordcount": len(ggt_sorted)}, "data": p1_items if not is_p2 else bad_p2}])
                if mode_inject == "duplicate_or_meta":
                    if not is_p2:
                        return _resp(json_data=[{"metadata": {"pagecount": 2, "recordcount": len(ggt_sorted) + 5}, "data": p1_items}])
            meta = {"pagecount": 2 if p2_items else 1, "recordcount": len(ggt_sorted)}
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
                if mode_inject == "partial" and code == codes[0]:
                    return _resp(json_data={"data": {f"hk{code}": {"qfqday": [[sessions[-1], "bad"]]}}})
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
                if mode_inject == "duplicate_or_meta":
                    return _resp(json_data={"data": {"hkHSI": {"day": hsi_rows[:-1] + [hsi_rows[-2]]}}})
            return _resp(json_data={"data": {"hkHSI": {"day": hsi_rows}}})

        # 5. GTImg HSI spot
        if "r_hkHSI" in url:
            if src_inject == "hsi_spot":
                if mode_inject == "exception":
                    raise RuntimeError("HSI spot timeout")
                if mode_inject == "empty":
                    return _resp(text="")
                if mode_inject == "partial":
                    return _resp(text='v_r_hkHSI="bad~fields";')
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
            px_str = "-1.0" if (src_inject == "hk_spot" and mode_inject == "duplicate_or_meta") else "15.0"
            for c in req_codes:
                idx = int(c) - 1
                mktcap = 20.0 if idx < 55 else 150.0  # first 55 stay smallcap < 50e8 HKD after drift
                fields = ["0"] * 47
                fields[1], fields[3], fields[4] = f"HK{c}", px_str, "14.8"
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
        # Under per_panel publishing, isolated US source failures exit 0 while CN succeeds
        # so the healthy CN panel still publishes; US panel is blocked via status.json.
        ("us", "exception", 0, False),
        ("us", "empty", 0, False),
        ("us", "partial", 0, False),
        ("us", "duplicate_or_meta", 0, False),
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
        ("indices", "partial", 1, False),
        ("indices", "duplicate_or_meta", 1, False),
        ("indices", "success", 0, True),
        ("breadth", "exception", 1, False),
        ("breadth", "empty", 1, False),
        ("breadth", "partial", 1, False),
        ("breadth", "duplicate_or_meta", 1, False),
        ("breadth", "success", 0, True),
        ("turnover", "exception", 1, False),
        ("turnover", "empty", 1, False),
        ("turnover", "partial", 1, False),
        ("turnover", "duplicate_or_meta", 1, False),
        ("turnover", "success", 0, True),
        ("etf_radar", "exception", 0, False),
        ("etf_radar", "empty", 0, False),
        ("etf_radar", "partial", 0, False),
        ("etf_radar", "duplicate_or_meta", 0, False),
        ("etf_radar", "success", 0, True),
        ("etf_vratio", "exception", 0, False),
        ("etf_vratio", "empty", 0, False),
        ("etf_vratio", "partial", 0, False),
        ("etf_vratio", "duplicate_or_meta", 0, False),
        ("etf_vratio", "success", 0, True),
        ("industry", "exception", 0, False),
        ("industry", "empty", 0, False),
        ("industry", "partial", 0, False),
        ("industry", "duplicate_or_meta", 0, False),
        ("industry", "success", 0, True),
        ("daily_history", "exception", 0, False),
        ("daily_history", "empty", 0, False),
        ("daily_history", "partial", 0, False),
        ("daily_history", "duplicate_or_meta", 0, False),
        ("daily_history", "success", 0, True),
        ("etf_official_shares", "exception", 0, False),
        ("etf_official_shares", "empty", 0, False),
        ("etf_official_shares", "partial", 0, False),
        ("etf_official_shares", "duplicate_or_meta", 0, False),
        ("etf_official_shares", "success", 0, True),
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
    prior_snap = json.loads((st_dir / "last_snapshot.json").read_text(encoding="utf-8"))

    # 2. Advance clock +1 min and inject fault cleanly
    now_holder["dt"] = t0 + timedelta(minutes=1)

    def _fault_get(url: str, **kw) -> str:
        if mode == "success":
            return base_get(url, **kw)
        if source == "indices" and "hq.sinajs.cn" in url:
            if mode == "exception":
                raise RuntimeError("Sina 502")
            if mode == "empty":
                return ""
            if mode == "partial":
                # Omit ONLY s_sh000688 (keeps s_sh000001 & s_sz399106!) -> 7/8 incomplete!
                return "\n".join(line for line in base_get(url, **kw).splitlines() if "s_sh000688" not in line)
            if mode == "duplicate_or_meta":
                # Zero/invalid price for s_sh000300
                return base_get(url, **kw).replace('hq_str_s_sh000300="', 'hq_str_s_sh000300="沪深300,0,0,0,0,0"; // ')
        if source == "turnover" and "hq.sinajs.cn" in url:
            if mode == "exception":
                raise RuntimeError("Sina turnover timeout")
            if mode == "empty":
                return "\n".join(
                    'var hq_str_s_sz399106="";' if "s_sz399106" in line else line
                    for line in base_get(url, **kw).splitlines()
                )
            if mode == "partial":
                return "\n".join(line for line in base_get(url, **kw).splitlines() if "s_sz399106" not in line)
            if mode == "duplicate_or_meta":
                return "\n".join(
                    'var hq_str_s_sz399106="深证综指,2100.0,10.0,0.48,1000,-1";' if "s_sz399106" in line else line
                    for line in base_get(url, **kw).splitlines()
                )
        if source == "daily_history" and "q.stock.sohu.com" in url:
            if mode == "exception":
                raise RuntimeError("Sohu timeout")
            if mode == "empty":
                return "<html><body></body></html>"
            if mode == "partial":
                # Only header/non-data rows (fewer/missing numeric cells)
                return "<html><body><table><tr><td>日期</td><td>涨停</td></tr></table></body></html>"
            if mode == "duplicate_or_meta":
                return "<html><body><table><tr><td>invalid-date</td><td>bad</td></tr></table></body></html>"
        if source == "etf_official_shares" and "query.sse.com.cn" in url:
            if mode == "exception":
                raise RuntimeError("SSE 503")
            if mode == "empty":
                return 'jsonpCallback({"result":[]})'
            if mode == "partial":
                # Omit 510300 while 512480 present -> incomplete representative set!
                return 'jsonpCallback({"result":[{"SEC_CODE":"512480","STAT_DATE":"2026-11-13","TOT_VOL":"100000"}]})'
            if mode == "duplicate_or_meta":
                # Only 2026-11-13 returned even for prior date queries -> lacks previous trading day!
                return 'jsonpCallback(' + json.dumps({"result": [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-13", "TOT_VOL": "100000"}, {"SEC_CODE": "510300", "STAT_DATE": "2026-11-13", "TOT_VOL": "500000"}]}) + ')'
        return base_get(url, **kw)

    def _fault_json(url: str, **kw) -> Any:
        if mode == "success":
            return base_json(url, **kw)
        if source == "breadth" and "ulist.np/get" in url:
            base_b = base_json(url, **kw)
            if mode == "exception":
                raise RuntimeError("Breadth 502")
            if mode == "empty":
                return {"data": {"diff": []}}
            if mode == "partial":
                # Only 2/3 markets returned!
                return {"data": {"diff": base_b["data"]["diff"][:2]}}
            if mode == "duplicate_or_meta":
                # 3 items with duplicate f12 market code!
                return {"data": {"diff": [{**d, "f12": "000001"} for d in base_b["data"]["diff"]]}}
        if source == "etf_radar" and "MK0021" in url:
            base_e = base_json(url, **kw)
            if mode == "exception":
                raise RuntimeError("ETF 502")
            if mode == "empty":
                return {"data": {"total": 2, "diff": []}}
            if mode == "partial":
                return {"data": {"total": 100, "diff": [base_e["data"]["diff"][0]]}}
            if mode == "duplicate_or_meta":
                # len(diff) == total == 2, but duplicate 512480 code!
                dup_item = base_e["data"]["diff"][0]
                return {"data": {"total": 2, "diff": [dup_item, dup_item]}}
        if source == "etf_vratio" and "qt/stock/get" in url:
            if mode == "exception":
                raise RuntimeError("vratio timeout")
            if mode == "empty":
                return {"data": {"f50": None}}
            if mode == "partial" and "510300" in url:
                return {"data": {"f50": None}}
            if mode == "duplicate_or_meta":
                return {"data": {"f50": -1.5}}
        if source == "industry" and ("m%3A0" in url or "m:0" in url):
            base_s = base_json(url, **kw)
            if mode == "exception":
                raise RuntimeError("Stock scan 502")
            if mode == "empty":
                return {"data": {"total": 3, "diff": []}}
            if mode == "partial":
                if "pn=2" in url:
                    return {"data": {"diff": []}}
                return {"data": {"total": 150, "diff": [{"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8}]}}
            if mode == "duplicate_or_meta":
                # len(diff) == total == 3, but duplicate 000001 code!
                d0, _, d2 = base_s["data"]["diff"]
                return {"data": {"total": 3, "diff": [d0, d0, d2]}}
        return base_json(url, **kw)

    orig_write = ashare_render.write_ashare_panel
    rendered_snapshots: list[dict[str, Any]] = []

    def _wrapped_write(**kw):
        # status.json MUST NOT have been updated yet before render runs (Req #8)!
        on_disk = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
        assert on_disk["overall"]["last_success_at"] == prior_ok
        rendered_snapshots.append(kw["snapshot"])
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
            assert json.loads((st_dir / "last_snapshot.json").read_text(encoding="utf-8")) == prior_snap
            snap_out = rendered_snapshots[-1]
            if source == "indices":
                assert snap_out["indices"] == prior_snap["indices"]
            elif source == "breadth":
                assert snap_out["breadth"] == prior_snap["breadth"]
            elif source == "turnover":
                assert snap_out["two_market_yi"] == prior_snap["two_market_yi"]
            elif source == "etf_radar":
                assert snap_out["etf_radar"] == prior_snap["etf_radar"]
            elif source == "industry":
                assert snap_out["industry"] == prior_snap["industry"]
            elif source == "daily_history":
                assert snap_out["daily_history"] == prior_snap["daily_history"]
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
        ("hkex_universe", "partial", True, 1, False),
        ("hkex_universe", "duplicate_or_meta", True, 1, False),
        ("ggt_list", "exception", True, 1, False),
        ("ggt_list", "empty", True, 1, False),
        ("ggt_list", "partial", True, 1, False),
        ("ggt_list", "duplicate_or_meta", True, 1, False),
        ("hk_spot", "exception", False, 1, False),
        ("hk_spot", "empty", False, 1, False),
        ("hk_spot", "partial", False, 1, False),
        ("hk_spot", "duplicate_or_meta", False, 1, False),
        ("hk_history", "exception", True, 1, False),
        ("hk_history", "empty", True, 1, False),
        ("hk_history", "partial", True, 1, False),
        ("hk_history", "duplicate_or_meta", True, 1, False),
        ("hsi_history", "exception", False, 1, False),
        ("hsi_history", "empty", True, 1, False),
        ("hsi_history", "partial", True, 1, False),
        ("hsi_history", "duplicate_or_meta", False, 1, False),
        ("hk_intraday_spot", "exception", False, 0, True),
        ("hk_intraday_spot", "empty", False, 0, True),
        ("hk_intraday_spot", "partial", False, 0, True),
        ("hk_intraday_spot", "duplicate_or_meta", False, 0, True),
        ("hsi_spot", "exception", False, 0, True),
        ("hsi_spot", "empty", False, 0, True),
        ("hsi_spot", "partial", False, 0, True),
        ("hsi_spot", "duplicate_or_meta", False, 0, True),
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
            if mode == "duplicate_or_meta":
                return pd.DataFrame({"bad_col": ["00001"]})
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
    if source == "all":
        assert sdoc["overall"]["ok"] is True
        assert sdoc["overall"]["last_success_at"] != prior_ok
        assert saved_frozen["frozen_series"]["dates"][-1] == target_str
    elif source in ("hk_intraday_spot", "hsi_spot"):
        assert expect_freeze is True
        assert sdoc["overall"]["ok"] is False
        assert sdoc["overall"]["last_success_at"] == prior_ok
        assert sdoc["sources"][source]["last_success_at"] == prior_ok
        assert saved_frozen["frozen_series"]["dates"][-1] == target_str
        meta_doc = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
        assert meta_doc["last_date"] == target_str
        assert prior_ok in (out_dir / "index.html").read_text(encoding="utf-8")
    else:
        assert sdoc["overall"]["ok"] is False
        assert sdoc["overall"]["last_success_at"] == prior_ok
        if source in sdoc["sources"]:
            assert sdoc["sources"][source]["last_success_at"] == prior_ok
        assert saved_frozen == seed_frozen, "Frozen history must NOT be overwritten when any daily source failed!"
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


# ---------------------------------------------------------------------------
# 5. Pre-Enable Hardening Tests (Items 1, 2, 5, 6)
# ---------------------------------------------------------------------------


def test_hkmom_daily_ok_intraday_fail_then_feed_daemon_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hardening #1: clean HK daily persists & freezes even when live spots fail; FeedDaemon recovery clears stale and completes close sync."""
    daily_dir = tmp_path / "hk_daily_state"
    out_dir = tmp_path / "hk_daily_out"
    daemon_root = tmp_path / "daemon_state"
    (daily_dir / "hist").mkdir(parents=True)

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-02")]
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

    prior_ok = "2026-10-30 16:35:00 北京时间"
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
    (daily_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")
    (daily_dir / "index_series.json").write_text(json.dumps(seed_frozen["frozen_series"]), encoding="utf-8")
    (daily_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c, "name": c} for c in codes[:50]], "nonggt": [{"code": c, "name": c} for c in codes[:50]]}),
        encoding="utf-8",
    )
    (daily_dir / "index_meta.json").write_text(
        json.dumps({"last_date": sessions[-2], "launch_date": "2026-09-30", "last_success_at": prior_ok}),
        encoding="utf-8",
    )

    daily_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/11/02"
    )
    failing_intra_sess = _build_hk_substitute_session(
        sessions=sessions,
        codes=codes,
        ggt_codes=ggt_codes,
        quote_date="2026/11/02",
        inject=("hk_intraday_spot", "exception"),
    )
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    orig_uni = hkmom_fetch.build_universe
    orig_dl = hkmom_fetch.download_history
    orig_comp = hkmom_compute.compute_all
    orig_intra = hkmom_compute.compute_intraday_snapshot
    dt_close = datetime(2026, 11, 2, 16, 35, 0, tzinfo=feeds_common.HK_TZ)

    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": daily_sess}))
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": daily_sess}))
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 11, 2), "hsi_session": daily_sess}))
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_close, "session": failing_intra_sess}))
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_close)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_close)

    # 1) Daily job runs cleanly on daily sources, while intraday spot fails -> daily results MUST persist!
    res = hkmom_run.run_daily_job(state_dir=daily_dir, output_dir=out_dir, as_of_date=date(2026, 11, 2))
    assert res["ok"] is False
    assert res["publish_ok"] is True
    assert res["froze"] is True
    assert res["last_date"] == "2026-11-02"
    assert json.loads((daily_dir / "index_series.json").read_text(encoding="utf-8"))["dates"][-1] == "2026-11-02"
    assert json.loads((daily_dir / "frozen_history.json").read_text(encoding="utf-8"))["frozen_series"]["dates"][-1] == "2026-11-02"
    daily_meta = json.loads((daily_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert daily_meta["last_date"] == "2026-11-02"
    assert daily_meta["stale"] is False
    assert "hk_intraday_spot" not in daily_meta.get("sources", {})
    assert json.loads((daily_dir / "status.json").read_text(encoding="utf-8"))["sources"]["hk_intraday_spot"]["ok"] is False

    # 2) Host FeedDaemon picks up daily results with recovered intraday spots -> clears stale & completes close sync!
    recovered_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/11/02"
    )
    pushed: list[dict[str, Any]] = []
    dt_live = datetime(2026, 11, 2, 16, 40, 0, tzinfo=feeds_common.HK_TZ)
    daemon = run_feeds.FeedDaemon(
        state_dir=daemon_root,
        hk_daily_dir=daily_dir,
        feeds=("hk",),
        hk_collect_fn=lambda cur: orig_intra(
            state_dir=daemon_root / "hk_momentum",
            daily_state_dir=daily_dir,
            now=cur,
            session=recovered_sess,
            persist_status=True,
        ),
        push_fn=lambda slug, data_key, payload, **_kw: pushed.append(
            {"slug": slug, "data_key": data_key, "payload": payload}
        ),
        now_fn=lambda: dt_live,
    )
    results = daemon.run_tick(now=dt_live)
    assert len(results) == 1 and results[0]["action"] == "post_close"
    assert len(pushed) == 1
    live_payload = pushed[0]["payload"]
    assert live_payload["stale"] is False
    assert live_payload["daily_close_date"] == "2026-11-02"
    assert live_payload["smallcap"]["source"] == "daily_close"
    assert live_payload["nonggt"]["source"] == "daily_close"
    assert live_payload["last_success_at"] == "2026-11-02 16:40:00"
    assert daemon.state["hk"]["post_close_daily_synced_date"] == "2026-11-02"


def test_pm_sw_code_date_level_completeness_hardening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hardening #2: PM checks completeness across industry code x date x level without advancing or sealing partial state."""
    monkeypatch.setattr(pm_collect.time, "sleep", lambda _s: None)
    cfg = pm_collect.load_config()
    raw_dir = tmp_path / "pm_raw"
    raw_dir.mkdir(parents=True)

    # Seed clean baseline CN meta & valuation first
    _, fake_ak_ok = _make_pm_sdks(cfg)
    cn_ok = pm_collect.fetch_cn(cfg, raw_dir=raw_dir, ak_module=fake_ak_ok)
    assert cn_ok["source_status"]["ok"] is True
    seed_meta = json.loads((raw_dir / "cn_sw_meta.json").read_text(encoding="utf-8"))
    val_ok = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_ok)
    assert val_ok["source_status"]["ok"] is True

    # 1) fetch_cn: omit an expected L1 code present in prior cn_sw_meta.json -> MUST fail & preserve cn_sw_meta.json
    _, fake_ak_missing_l1 = _make_pm_sdks(cfg)
    orig_l1 = fake_ak_missing_l1.sw_index_first_info()
    dropped_l1 = orig_l1.iloc[1:].copy()
    fake_ak_missing_l1.sw_index_first_info = lambda: dropped_l1
    cn_bad = pm_collect.fetch_cn(cfg, raw_dir=raw_dir, ak_module=fake_ak_missing_l1)
    assert cn_bad["source_status"]["ok"] is False
    assert "缺失" in (cn_bad["source_status"]["reason"] or "")
    assert json.loads((raw_dir / "cn_sw_meta.json").read_text(encoding="utf-8")) == seed_meta

    # 2) fetch_cn_val_latest: omit one expected L2 code in daily valuation -> MUST fail val_latest
    _, fake_ak_missing_val = _make_pm_sdks(cfg)
    orig_daily_sw = fake_ak_missing_val.index_analysis_daily_sw.side_effect
    l1_codes = sorted(k for k, v in seed_meta["industries"].items() if v.get("level") == 1)
    l2_codes = sorted(k for k, v in seed_meta["industries"].items() if v.get("level") == 2)
    assert l1_codes and l2_codes

    def _drop_l2_daily(symbol: str, start_date: str, end_date: str):
        df = orig_daily_sw(symbol, start_date, end_date)
        if symbol == "二级行业":
            return df[df["指数代码"].astype(str).str.split(".").str[0] != l2_codes[0]].copy()
        return df

    fake_ak_missing_val.index_analysis_daily_sw.side_effect = _drop_l2_daily
    with pytest.raises(RuntimeError, match="二级行业"):
        pm_collect.fetch_cn_val_latest(raw_dir=raw_dir, ak_module=fake_ak_missing_val)

    # 3) fetch_cn_valuation: weekly valuation missing an expected L1 code retroactively un-marks done_map and keeps (date, level) pending
    prog_path = raw_dir / "cn_sw_val_progress.json"
    prog = json.loads(prog_path.read_text(encoding="utf-8"))
    assert "一级行业" in prog["done"].get("2025-12-26", [])
    val_hist_path = raw_dir / "cn_sw_val_weekly.csv"
    drop_target_code = l1_codes[0]
    vh_df = pd.read_csv(val_hist_path, dtype={"code": str})
    vh_df = vh_df[~((vh_df["date"].astype(str) == "2025-12-26") & (vh_df["code"].astype(str) == drop_target_code))].copy()
    vh_df.to_csv(val_hist_path, index=False)

    _, fake_ak_bad_week = _make_pm_sdks(cfg)
    orig_weekly = fake_ak_bad_week.index_analysis_weekly_sw.side_effect

    def _drop_one_code_week(symbol: str, date: str):
        df = orig_weekly(symbol, date)
        if date == "20251226" and symbol == "一级行业":
            return df[df["指数代码"].astype(str).str.split(".").str[0] != drop_target_code].copy()
        return df

    fake_ak_bad_week.index_analysis_weekly_sw.side_effect = _drop_one_code_week
    fake_ak_bad_week.index_analysis_week_month_sw.side_effect = None
    fake_ak_bad_week.index_analysis_week_month_sw.return_value = pd.DataFrame(
        {"date": ["2025-12-19", "2025-12-26", "2026-01-02"]}
    )

    val_partial = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_bad_week)
    assert val_partial["source_status"]["ok"] is False
    prog_after = json.loads(prog_path.read_text(encoding="utf-8"))
    assert "一级行业" not in prog_after["done"].get("2025-12-26", [])
    assert "二级行业" in prog_after["done"].get("2025-12-26", [])

    # When missing code recovers, re-run completes cleanly!
    _, fake_ak_recovered = _make_pm_sdks(cfg)
    fake_ak_recovered.index_analysis_week_month_sw.side_effect = None
    fake_ak_recovered.index_analysis_week_month_sw.return_value = pd.DataFrame(
        {"date": ["2025-12-19", "2025-12-26", "2026-01-02"]}
    )
    val_recovered = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_recovered)
    assert val_recovered["source_status"]["ok"] is True
    prog_recovered = json.loads(prog_path.read_text(encoding="utf-8"))
    assert set(prog_recovered["done"].get("2025-12-26", [])) == {"一级行业", "二级行业"}
    assert set(prog_recovered["done"].get("2026-01-02", [])) == {"一级行业", "二级行业"}


def test_ashare_official_shares_date_and_szse_hardening(tmp_path: Path) -> None:
    """Hardening #5: SSE and SZSE official ETF share pairs reject future, stale, and non-consecutive trading dates."""
    now_dt = datetime(2026, 11, 16, 14, 30, 0, tzinfo=feeds_common.CN_TZ)

    def _run_sse(rows_latest: list[dict[str, Any]], rows_prev: list[dict[str, Any]]) -> dict[str, Any]:
        def _fake_get(url: str, **_kw) -> str:
            if "STAT_DATE=2026-" in url:
                return "jsonpCallback(" + json.dumps({"result": rows_prev}) + ")"
            return "jsonpCallback(" + json.dumps({"result": rows_latest}) + ")"

        col = ashare_collect.AShareCollector(
            state_dir=tmp_path / "sse",
            http_get=_fake_get,
            http_get_json=lambda *_a, **_kw: {},
            now_fn=lambda: now_dt,
        )
        return col._fetch_sse_pairs(["512480"])

    # Valid consecutive trading days (Fri 2026-11-13 & Thu 2026-11-12 for Mon 2026-11-16) -> succeeds
    ok_pairs = _run_sse(
        [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-13", "TOT_VOL": "100000"}],
        [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-12", "TOT_VOL": "95000"}],
    )
    assert ok_pairs["512480"] == (("2026-11-13", 100000.0), ("2026-11-12", 95000.0))

    # Future date rejected
    with pytest.raises(RuntimeError, match="未来日期"):
        _run_sse(
            [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-20", "TOT_VOL": "100000"}],
            [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-13", "TOT_VOL": "95000"}],
        )

    # Stale latest date rejected (2026-11-12 < expected previous trading day 2026-11-13)
    with pytest.raises(RuntimeError, match="最新日期过期"):
        _run_sse(
            [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-12", "TOT_VOL": "100000"}],
            [{"SEC_CODE": "512480", "STAT_DATE": "2026-11-11", "TOT_VOL": "95000"}],
        )

    # SZSE non-adjacent / future date checks
    def _run_szse(data_rows: list[dict[str, Any]]) -> Any:
        col = ashare_collect.AShareCollector(
            state_dir=tmp_path / "szse",
            http_get=lambda _u, **_kw: json.dumps([{"data": data_rows}]),
            http_get_json=lambda *_a, **_kw: {},
            now_fn=lambda: now_dt,
        )
        return col._fetch_szse_pair("159915")

    assert _run_szse(
        [
            {"fund_code": "159915", "size_date": "2026-11-13", "current_size": "200000"},
            {"fund_code": "159915", "size_date": "2026-11-12", "current_size": "195000"},
        ]
    ) == (("2026-11-13", 200000.0), ("2026-11-12", 195000.0))

    with pytest.raises(RuntimeError, match="未来日期"):
        _run_szse(
            [
                {"fund_code": "159915", "size_date": "2026-11-18", "current_size": "200000"},
                {"fund_code": "159915", "size_date": "2026-11-13", "current_size": "195000"},
            ]
        )

    with pytest.raises(RuntimeError, match="非相邻交易日"):
        _run_szse(
            [
                {"fund_code": "159915", "size_date": "2026-11-13", "current_size": "200000"},
                {"fund_code": "159915", "size_date": "2026-11-11", "current_size": "195000"},
            ]
        )


def test_hkmom_ggt_recordcount_and_duplicate_codes_hardening() -> None:
    """Hardening #6: fetch_ggt_list strictly checks metadata.recordcount presence, consistency, total match, and uniqueness."""

    def _run(pages: dict[int, dict[str, Any]]) -> dict[str, str]:
        sess = create_autospec(requests.Session, instance=True)

        def _get(url: str, **_kw):
            pg = 2 if "PAGENO=2" in url else 1
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [pages[pg]])

        sess.get.side_effect = _get
        return hkmom_fetch.fetch_ggt_list(session=sess)

    assert _run(
        {
            1: {"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00001", "zqjc": "长和"}]},
            2: {"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00002", "zqjc": "中电控股"}]},
        }
    ) == {"00001": "长和", "00002": "中电控股"}

    with pytest.raises(RuntimeError, match="recordcount"):
        _run({1: {"metadata": {"pagecount": 1}, "data": [{"zqdm": "00001", "zqjc": "长和"}]}})

    with pytest.raises(RuntimeError, match="recordcount.*不符"):
        _run({1: {"metadata": {"pagecount": 1, "recordcount": 3}, "data": [{"zqdm": "00001", "zqjc": "长和"}]}})

    with pytest.raises(RuntimeError, match="不一致"):
        _run(
            {
                1: {"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00001", "zqjc": "长和"}]},
                2: {"metadata": {"pagecount": 2, "recordcount": 5}, "data": [{"zqdm": "00002", "zqjc": "中电控股"}]},
            }
        )

    with pytest.raises(RuntimeError, match="重复"):
        _run(
            {
                1: {"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00001", "zqjc": "长和"}]},
                2: {"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00001", "zqjc": "长和"}]},
            }
        )


# ---------------------------------------------------------------------------
# 6. Final Pre-Enable Fixes (HK Spot Date/Dup/Coverage, HSI Raw Gap, Daemon Timestamps, PM CSV Missing)
# ---------------------------------------------------------------------------


def test_hkmom_spot_date_duplicate_and_candidate_coverage_blocks_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final #1: hk_spot validates target trade date, code uniqueness, 50% candidate coverage, and held coverage, blocking month-end freeze."""
    st_dir = tmp_path / "hk_spot_guard"
    out_dir = tmp_path / "hk_spot_out"
    (st_dir / "hist").mkdir(parents=True)

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
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

    base_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    orig_base_get = base_sess.get.side_effect
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    orig_uni = hkmom_fetch.build_universe
    orig_dl = hkmom_fetch.download_history
    orig_comp = hkmom_compute.compute_all
    orig_intra = hkmom_compute.compute_intraday_snapshot
    dt_me = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_me)

    def _run_with_spot_mutator(mutator) -> dict[str, Any]:
        sess = create_autospec(requests.Session, instance=True)

        def _get(url: str, **kw):
            resp = orig_base_get(url, **kw)
            if "qt.gtimg.cn" in url and "r_hkHSI" not in url:
                resp.text = mutator(url, resp.text)
            return resp

        sess.get.side_effect = _get
        monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sess}))
        monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sess}))
        monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 10, 30), "hsi_session": sess}))
        monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_me, "session": sess}))
        return hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))

    # (a) Duplicate code in batch spot response -> hk_spot fails, month-end freeze blocked
    def _dup_spot(_url: str, txt: str) -> str:
        segs = [s for s in txt.split(";") if s.strip()]
        dup_target = next((s for s in segs if "r_hk00056=" in s), None)
        return ";".join(segs + ([dup_target] if dup_target else []))

    res_dup = _run_with_spot_mutator(_dup_spot)
    assert res_dup["ok"] is False and res_dup["month_end_failed"] is True
    assert res_dup["sources"]["hk_spot"]["ok"] is False
    assert res_dup["last_success_at"] == prior_ok
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == seed_frozen

    # (b) Stale date on ONE candidate non-held stock (2026/10/29 vs target 2026-10-30) -> hk_spot fails, freeze blocked
    def _stale_one(_url: str, txt: str) -> str:
        segs = []
        for s in txt.split(";"):
            if s.startswith("v_r_hk00060="):
                s = s.replace("2026/10/30", "2026/10/29")
            segs.append(s)
        return ";".join(segs)

    res_stale = _run_with_spot_mutator(_stale_one)
    assert res_stale["ok"] is False and res_stale["month_end_failed"] is True
    assert res_stale["sources"]["hk_spot"]["ok"] is False
    assert res_stale["last_success_at"] == prior_ok
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == seed_frozen

    # (c) Missing >50% candidate quotes (even with ALL 50 held stocks present: 50/120 < 50%) -> hk_spot fails, freeze blocked
    held_set = set(codes[:50])

    def _drop_candidates(_url: str, txt: str) -> str:
        kept = []
        for s in txt.split(";"):
            if not s.strip():
                continue
            code = s.split("_hk")[-1].split("=")[0]
            if code in held_set:
                kept.append(s)
        return ";".join(kept)

    res_cov = _run_with_spot_mutator(_drop_candidates)
    assert res_cov["ok"] is False and res_cov["month_end_failed"] is True
    assert res_cov["sources"]["hk_spot"]["ok"] is False
    assert res_cov["last_success_at"] == prior_ok
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == seed_frozen

    # (d) Clean quotes on all 120 stocks dated 2026/10/30 -> freezes cleanly
    res_ok = _run_with_spot_mutator(lambda _u, txt: txt)
    assert res_ok["ok"] is True
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))["frozen_series"]["dates"][-1] == "2026-10-30"


def test_hkmom_hsi_intermediate_gap_not_ffilled_blocks_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final #2: missing an intermediate trading day in HSI raw data (tail_dates) cannot be ffill'd and blocks freeze."""
    st_dir = tmp_path / "hk_hsi_gap"
    out_dir = tmp_path / "hk_hsi_out"
    (st_dir / "hist").mkdir(parents=True)

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    assert sessions[-3:] == ["2026-10-28", "2026-10-29", "2026-10-30"]
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

    # Seed frozen history ending 2026-10-28 so tail_dates = ["2026-10-29", "2026-10-30"]
    prior_ok = "2026-10-28 16:35:00 北京时间"
    seed_frozen = {
        "launch_date": "2026-09-30",
        "rebalance_snapshots": {"2026-09-30": {"smallcap": codes[:50], "nonggt": codes[:50]}},
        "frozen_series": {
            "dates": sessions[:-2],
            "smallcap": [1000.0 + i for i in range(len(sessions) - 2)],
            "nonggt": [1000.0 + i for i in range(len(sessions) - 2)],
            "hsi": [1000.0 + i for i in range(len(sessions) - 2)],
        },
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(seed_frozen), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(seed_frozen["frozen_series"]), encoding="utf-8")
    (st_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": c, "name": c} for c in codes[:50]], "nonggt": [{"code": c, "name": c} for c in codes[:50]]}),
        encoding="utf-8",
    )
    (st_dir / "index_meta.json").write_text(
        json.dumps({"last_date": "2026-10-28", "launch_date": "2026-09-30", "last_success_at": prior_ok}),
        encoding="utf-8",
    )

    full_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    orig_get = full_sess.get.side_effect

    def _gap_hsi_get(url: str, **kw):
        resp = orig_get(url, **kw)
        if "kline/kline" in url and "hkHSI" in url:
            payload = resp.json()
            day_rows = payload["data"]["hkHSI"]["day"]
            # Drop intermediate 2026-10-29 while KEEPING 2026-10-28 and target 2026-10-30!
            filtered = [r for r in day_rows if str(r[0]) != "2026-10-29"]
            return SimpleNamespace(
                status_code=200,
                text="",
                content=b"",
                encoding="utf-8",
                raise_for_status=lambda: None,
                json=lambda: {"data": {"hkHSI": {"day": filtered}}},
            )
        return resp

    gap_sess = create_autospec(requests.Session, instance=True)
    gap_sess.get.side_effect = _gap_hsi_get
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    dt_me = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    orig_uni = hkmom_fetch.build_universe
    orig_dl = hkmom_fetch.download_history
    orig_comp = hkmom_compute.compute_all
    orig_intra = hkmom_compute.compute_intraday_snapshot
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": gap_sess}))
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": gap_sess}))
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 10, 30), "hsi_session": gap_sess}))
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_me, "session": gap_sess}))

    res_gap = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))
    assert res_gap["ok"] is False
    assert res_gap["froze"] is False
    assert res_gap["month_end_failed"] is True
    assert res_gap["sources"]["hsi_history"]["ok"] is False
    assert "2026-10-29" in (res_gap["sources"]["hsi_history"]["reason"] or "")
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == seed_frozen

    # When 2026-10-29 recovers -> appends ["2026-10-29", "2026-10-30"] and freezes!
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "as_of_date": date(2026, 10, 30), "hsi_session": full_sess}))
    res_rec = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))
    assert res_rec["ok"] is True and res_rec["froze"] is True
    saved_frozen = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert saved_frozen["frozen_series"]["dates"][-2:] == ["2026-10-29", "2026-10-30"]


def test_hkmom_daemon_preserves_daily_timestamps_and_prefers_meta_daily_ok(
    tmp_path: Path,
) -> None:
    """Final #3 + GLM low #1: FeedDaemon preserves original daily last_success_at untouched and overlay prefers meta.daily_last_success_at."""
    daily_dir = tmp_path / "daily_hk"
    daemon_dir = tmp_path / "daemon_state"
    daily_hk_dir = daemon_dir / "hk_momentum"
    daily_dir.mkdir(parents=True)

    prior_combined_ok = "2026-10-30 16:35:00 北京时间"
    daily_clean_ok = "2026-11-02 16:35:00 北京时间"

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2026-10-20", "2026-11-02")]
    (daily_dir / "index_series.json").write_text(
        json.dumps(
            {
                "dates": sessions,
                "smallcap": [1000.0 + i for i in range(len(sessions))],
                "nonggt": [1000.0 + i for i in range(len(sessions))],
                "hsi": [1000.0 + i for i in range(len(sessions))],
            }
        ),
        encoding="utf-8",
    )
    (daily_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": "00001", "name": "CKH"}], "nonggt": [{"code": "00001", "name": "CKH"}]}),
        encoding="utf-8",
    )
    (daily_dir / "index_meta.json").write_text(
        json.dumps(
            {
                "last_date": "2026-11-02",
                "launch_date": "2026-09-30",
                "stale": False,
                "last_success_at": prior_combined_ok,
                "daily_last_success_at": daily_clean_ok,
                "sources": {
                    "hsi_history": {
                        "name": "hsi_history",
                        "label": "恒生指数历史",
                        "ok": True,
                        "stale": False,
                        "reason": None,
                        "last_success_at": daily_clean_ok,
                        "as_of": "2026-11-02",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    feeds_common.SourceStatusStore(daily_dir).update_sources(
        [
            feeds_common.make_source_result(n, ok=True, stale=False, as_of="2026-11-02", label=n)
            for n in ("hkex_universe", "ggt_list", "hk_spot", "hk_history", "hsi_history", "index_compute", "html_render")
        ]
        + [feeds_common.make_source_result("hk_intraday_spot", ok=False, stale=True, reason="spot down", label="港股成分实时报价")],
        now_str=daily_clean_ok,
    )
    # Force status.json overall.last_success_at to yesterday's combined ok (simulating daily clean + intraday fail)
    sdoc = json.loads((daily_dir / "status.json").read_text(encoding="utf-8"))
    sdoc["overall"]["last_success_at"] = prior_combined_ok
    (daily_dir / "status.json").write_text(json.dumps(sdoc, ensure_ascii=False), encoding="utf-8")

    # 1) FeedDaemon overlay prefers meta.daily_last_success_at over stale status.json overall.last_success_at
    daemon = run_feeds.FeedDaemon(
        state_dir=daemon_dir,
        hk_daily_dir=daily_dir,
        feeds=("hk",),
        push_fn=lambda *_a, **_kw: None,
    )
    overlay = daemon._hk_daily_status_overlay()
    assert overlay["daily_last_success_at"] == daily_clean_ok

    # 2) compute_intraday_snapshot at 16:45 MUST NOT overwrite daily source timestamps in daemon status.json!
    sess_live = _build_hk_substitute_session(
        sessions=sessions, codes=["00001"], ggt_codes=set(), quote_date="2026/11/02"
    )
    dt_live = datetime(2026, 11, 2, 16, 45, 0, tzinfo=feeds_common.HK_TZ)
    snap = hkmom_compute.compute_intraday_snapshot(
        state_dir=daily_hk_dir,
        daily_state_dir=daily_dir,
        now=dt_live,
        session=sess_live,
        persist_status=True,
    )
    assert snap["stale"] is False
    daemon_status = json.loads((daily_hk_dir / "status.json").read_text(encoding="utf-8"))
    for daily_key in ("hkex_universe", "ggt_list", "hk_spot", "hk_history", "hsi_history", "index_compute", "html_render"):
        assert daemon_status["sources"][daily_key]["last_success_at"] == daily_clean_ok
        assert snap["sources"][daily_key]["last_success_at"] == daily_clean_ok
    assert daemon_status["sources"]["hk_intraday_spot"]["last_success_at"] == "2026-11-02 16:45:00"
    assert daemon_status["sources"]["hsi_spot"]["last_success_at"] == "2026-11-02 16:45:00"
    assert snap["sources"] == daemon_status["sources"]


def test_pm_weekly_valuation_missing_csv_recollects_even_with_done_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final #4: missing or empty cn_sw_val_weekly.csv invalidates done_map and forces re-collection."""
    monkeypatch.setattr(pm_collect.time, "sleep", lambda _s: None)
    cfg = pm_collect.load_config()
    raw_dir = tmp_path / "pm_missing_csv"
    raw_dir.mkdir(parents=True)

    _, fake_ak = _make_pm_sdks(cfg)
    assert pm_collect.fetch_cn(cfg, raw_dir=raw_dir, ak_module=fake_ak)["source_status"]["ok"] is True
    seed_val = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak)
    assert seed_val["source_status"]["ok"] is True
    csv_path = raw_dir / "cn_sw_val_weekly.csv"
    prog_path = raw_dir / "cn_sw_val_progress.json"
    assert csv_path.is_file()
    assert json.loads(prog_path.read_text(encoding="utf-8"))["done"]

    # Delete CSV while leaving done_map intact on disk!
    csv_path.unlink()

    # 1) When weekly endpoint fails after CSV loss: MUST clear done_map, keep ok=False, n_rows=0
    _, fake_ak_fail = _make_pm_sdks(cfg)
    fake_ak_fail.index_analysis_weekly_sw.side_effect = RuntimeError("weekly down")
    res_fail = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_fail)
    assert res_fail["ok"] is False and res_fail["stale"] is True
    assert res_fail["n_rows"] == 0
    assert json.loads(prog_path.read_text(encoding="utf-8"))["done"] == {}

    # 2) When weekly endpoint recovers: MUST actually re-fetch & rebuild cn_sw_val_weekly.csv!
    _, fake_ak_rec = _make_pm_sdks(cfg)
    res_rec = pm_collect.fetch_cn_valuation(cfg, raw_dir=raw_dir, ak_module=fake_ak_rec)
    assert res_rec["ok"] is True
    assert res_rec["weeks_fetched_pairs"] > 0
    assert res_rec["n_rows"] > 0
    assert csv_path.is_file()


# ---------------------------------------------------------------------------
# 7. HK Momentum First-Run Prod Fixes (Empty CN Name, Pre-Open Target Date, Missing universe.csv)
# ---------------------------------------------------------------------------


def test_hkmom_ggt_list_empty_cn_name_and_skip_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty zqjc falls back to zqywjc with count; invalid code/both-empty rows skip; all-invalid raises."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    recorded_pages = {
        1: {
            "metadata": {"pagecount": 2, "recordcount": 4},
            "data": [
                {"zqdm": "00700", "zqjc": "腾讯控股", "zqywjc": "TENCENT"},
                {"zqdm": "00068", "zqjc": "", "zqywjc": "MANYCORE TECH"},
            ],
        },
        2: {
            "metadata": {"pagecount": 2, "recordcount": 4},
            "data": [
                {"zqdm": "BAD", "zqjc": "坏代码", "zqywjc": "BADCODE"},
                {"zqdm": "00069", "zqjc": None, "zqywjc": ""},
            ],
        },
    }

    def _get(url: str, **_kw):
        pg = 2 if "PAGENO=2" in url else 1
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [recorded_pages[pg]])

    sess = create_autospec(requests.Session, instance=True)
    sess.get.side_effect = _get

    ggt = hkmom_fetch.fetch_ggt_list(session=sess)
    assert ggt == {"00700": "腾讯控股", "00068": "MANYCORE TECH"}
    assert ggt.missing_cn_name == 1
    assert ggt.skipped_invalid == 2

    # All-invalid payload must fail
    all_bad_sess = create_autospec(requests.Session, instance=True)
    all_bad_sess.get.return_value = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: [
            {
                "metadata": {"pagecount": 1, "recordcount": 2},
                "data": [
                    {"zqdm": "", "zqjc": "无代码", "zqywjc": "NOCODE"},
                    {"zqdm": "00068", "zqjc": "", "zqywjc": ""},
                ],
            }
        ],
    )
    with pytest.raises(RuntimeError, match="全部记录无效"):
        hkmom_fetch.fetch_ggt_list(session=all_bad_sess)

    # build_universe records count in ggt_list source_status & universe_meta
    st_dir = tmp_path / "hk_ggt_fallback"
    hkex_df = pd.DataFrame(
        {
            "code": ["00700", "00068"],
            "name": ["TENCENT", "MANYCORE TECH"],
            "subcat": ["Equity Securities (Main Board)", "Equity Securities (Main Board)"],
            "ccy": ["HKD", "HKD"],
        }
    )
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    monkeypatch.setattr(
        hkmom_fetch,
        "now_hk",
        lambda: datetime(2026, 11, 17, 16, 30, 0, tzinfo=feeds_common.HK_TZ),
    )

    def _spot_get(url: str, **_kw):
        if "szse.cn" in url:
            return _get(url)
        lines = []
        for c in ("00700", "00068"):
            f = ["0"] * 47
            f[1], f[3], f[4], f[30], f[32], f[37], f[45] = f"HK{c}", "15.0", "14.8", "2026/11/17 16:08:00", "1.0", "1000", "40.0"
            lines.append(f'v_r_hk{c}="{"~".join(f)}"')
        return SimpleNamespace(raise_for_status=lambda: None, text=";".join(lines), encoding="gbk")

    uni_sess = create_autospec(requests.Session, instance=True)
    uni_sess.get.side_effect = _spot_get

    uni, uni_meta = hkmom_fetch.build_universe(state_dir=st_dir, session=uni_sess)
    assert uni_meta["stale"] is False
    assert uni_meta["ggt_missing_cn_name"] == 1
    assert uni_meta["ggt_skipped_invalid"] == 2
    ggt_st = uni_meta["source_statuses"]["ggt_list"]
    assert ggt_st["ok"] is True
    assert "缺中文简称 1 只" in (ggt_st.get("note") or "")
    assert ggt_st["counts"] == {"valid": 2, "missing_cn_name": 1, "skipped_invalid": 2}
    assert set(uni.loc[uni["is_ggt"], "code"]) == {"00700", "00068"}


def test_hkmom_preopen_early_morning_target_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running at 00:25 Beijing time (16:25 UTC) targets previous closed HK trading day; intraday targets today."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)

    # 2026-11-18 00:25 BJT (Wed before 09:30 open) -> 2026-11-17
    dt_0025 = datetime(2026, 11, 18, 0, 25, 0, tzinfo=feeds_common.HK_TZ)
    assert hkmom_fetch._target_hk_trade_date(now=dt_0025) == "2026-11-17"
    # 2026-11-18 09:29 BJT -> 2026-11-17; 10:30 intraday -> 2026-11-18; 16:25 post-close -> 2026-11-18
    assert hkmom_fetch._target_hk_trade_date(now=datetime(2026, 11, 18, 9, 29, 0, tzinfo=feeds_common.HK_TZ)) == "2026-11-17"
    assert hkmom_fetch._target_hk_trade_date(now=datetime(2026, 11, 18, 10, 30, 0, tzinfo=feeds_common.HK_TZ)) == "2026-11-18"
    assert hkmom_fetch._target_hk_trade_date(now=datetime(2026, 11, 18, 16, 25, 0, tzinfo=feeds_common.HK_TZ)) == "2026-11-18"
    # Weekend / Monday 00:25 -> previous Friday 2026-11-13 / 2026-11-20
    assert hkmom_fetch._target_hk_trade_date(now=datetime(2026, 11, 21, 14, 0, 0, tzinfo=feeds_common.HK_TZ)) == "2026-11-20"
    assert hkmom_fetch._target_hk_trade_date(now=datetime(2026, 11, 16, 0, 25, 0, tzinfo=feeds_common.HK_TZ)) == "2026-11-13"

    # Full first-run E2E at 2026-11-18 00:25 BJT without universe.csv and with empty-CN-name GGT entry
    st_dir = tmp_path / "hk_first_0025"
    out_dir = tmp_path / "hk_first_0025_out"
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-17")]
    assert sessions[-1] == "2026-11-17"
    codes = ["00068"] + [f"{i:05d}" for i in range(1, 121) if i != 68]
    ggt_codes = set(codes[55:]) | {"00068"}

    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/11/17"
    )
    orig_get = sub_sess.get.side_effect

    def _patched_get(url: str, **kw):
        if "szse.cn" in url:
            resp = orig_get(url, **kw)
            payload = resp.json()
            for item in payload[0]["data"]:
                if item.get("zqdm") == "00068":
                    item["zqjc"] = ""
                    item["zqywjc"] = "MANYCORE TECH"
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
        return orig_get(url, **kw)

    sub_sess.get.side_effect = _patched_get
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: sub_sess)
    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_0025)
    monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_0025)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_0025)
    orig_uni = hkmom_fetch.build_universe
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sub_sess}))
    orig_dl = hkmom_fetch.download_history
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sub_sess}))
    orig_comp = hkmom_compute.compute_all
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "hsi_session": sub_sess}))
    orig_intra = hkmom_compute.compute_intraday_snapshot
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_0025, "session": sub_sess}))

    res = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir)
    assert res["publish_ok"] is True
    assert res["froze"] is True
    assert res["last_date"] == "2026-11-17"
    assert (st_dir / "universe.csv").is_file()
    status_doc = json.loads((st_dir / "status.json").read_text(encoding="utf-8"))
    assert status_doc["sources"]["hk_spot"]["ok"] is True
    assert status_doc["sources"]["ggt_list"]["ok"] is True
    assert "缺中文简称 1 只" in (status_doc["sources"]["ggt_list"].get("note") or "")


def test_hkmom_first_run_without_universe_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First run without universe.csv uses in-memory list if available; gives clear stale reason if unavailable."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    dt_close = datetime(2026, 11, 17, 16, 35, 0, tzinfo=feeds_common.HK_TZ)

    # Case A: No universe.csv on disk, GGT list fails (so universe.csv isn't written), HKEX + spot succeed ->
    # run_daily_job computes constituents from in-memory fetched list without FileNotFoundError!
    st_mem = tmp_path / "hk_first_mem"
    out_mem = tmp_path / "hk_first_mem_out"
    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-17")]
    codes = [f"{i:05d}" for i in range(1, 121)]
    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=set(), quote_date="2026/11/17", inject=("ggt_list", "exception")
    )
    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: sub_sess)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_close)
    monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_close)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_close)
    orig_uni = hkmom_fetch.build_universe
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sub_sess}))
    orig_dl = hkmom_fetch.download_history
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sub_sess}))
    orig_comp = hkmom_compute.compute_all
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "hsi_session": sub_sess}))
    orig_intra = hkmom_compute.compute_intraday_snapshot
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_close, "session": sub_sess}))

    res_mem = hkmom_run.run_daily_job(state_dir=st_mem, output_dir=out_mem)
    assert res_mem["publish_ok"] is True
    assert res_mem["froze"] is False
    assert not (st_mem / "universe.csv").is_file()
    assert not (st_mem / "frozen_history.json").is_file()
    assert res_mem["sources"]["index_compute"]["ok"] is True
    assert res_mem["sources"]["ggt_list"]["stale"] is True
    assert "No such file" not in (res_mem.get("stale_reason") or "")

    # Case B: No universe.csv and HKEX universe fails -> clear stale reason, NO FileNotFoundError
    st_fail = tmp_path / "hk_first_fail"
    out_fail = tmp_path / "hk_first_fail_out"
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", create_autospec(hkmom_fetch.fetch_hkex_universe, side_effect=RuntimeError("HKEX 503")))
    res_fail = hkmom_run.run_daily_job(state_dir=st_fail, output_dir=out_fail)
    assert res_fail["ok"] is False
    assert res_fail["publish_ok"] is False
    assert res_fail["stale"] is True
    comp_reason = res_fail["sources"]["index_compute"]["reason"] or ""
    assert "No such file" not in comp_reason
    assert "universe.csv" in comp_reason
    assert res_fail["sources"]["hkex_universe"]["stale"] is True

    # Case C: Direct compute_all without universe.csv raises clear RuntimeError (not FileNotFoundError)
    empty_dir = tmp_path / "hk_empty_direct"
    empty_dir.mkdir()
    with pytest.raises(RuntimeError, match="缺少成分文件 universe.csv"):
        hkmom_compute.compute_all(state_dir=empty_dir)


# ---------------------------------------------------------------------------
# 8. Round 2 Review Fixes (GGT Skipped Exclusion, HK Holiday Snapshot, A-share Target/Date Checks)
# ---------------------------------------------------------------------------


def test_hkmom_ggt_skipped_rows_excluded_and_displayed_at_month_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipped GGT rows report '跳过 N 只（原因）' on page, stay out of ALL candidate pools, and record at month-end."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    st_dir = tmp_path / "hk_skip_excl"
    out_dir = tmp_path / "hk_skip_excl_out"

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    assert sessions[-1] == "2026-10-30"

    # Put 00069 in HKEX universe with smallcap (<50e8) and highest momentum return so it WOULD rank #1
    # in BOTH smallcap and nonggt if not excluded when SZSE skips it for empty CN+EN names!
    skipped_target = "00069"
    codes = [skipped_target] + [f"{i:05d}" for i in range(1, 130) if i != 69]
    ggt_codes = set(codes[65:])  # first 65 have is_ggt=False (including 00069), rest are GGT

    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    orig_get = sub_sess.get.side_effect

    def _patched_get(url: str, **kw):
        if "szse.cn" in url:
            resp = orig_get(url, **kw)
            payload = resp.json()
            meta = payload[0]["metadata"]
            # Append 00069 (valid code, empty CN+EN names) on page 1 and BAD (invalid code) on page 2
            if "PAGENO=2" in url:
                payload[0]["data"].append({"zqdm": "BAD", "zqjc": "坏代码", "zqywjc": "BAD"})
            else:
                payload[0]["data"].append({"zqdm": skipped_target, "zqjc": "", "zqywjc": ""})
            meta["recordcount"] = int(meta["recordcount"]) + 2
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
        if "hkfqkline" in url and f"param=hk{skipped_target}," in url:
            # Give 00069 huge upward momentum so it would dominate both pools if ever eligible
            bars = [
                [ds, f"{10.0 * (1.01 ** t):.3f}", f"{10.0 * (1.01 ** t):.3f}", f"{10.0 * (1.01 ** t) * 1.01:.3f}", f"{10.0 * (1.01 ** t) * 0.99:.3f}", "500000", {}, "0", "0", "500.0"]
                for t, ds in enumerate(sessions)
            ]
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": {f"hk{skipped_target}": {"qfqday": bars}}},
            )
        return orig_get(url, **kw)

    sub_sess.get.side_effect = _patched_get
    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )
    dt_me = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: sub_sess)
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    monkeypatch.setattr(hkmom_run, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_me)
    orig_uni = hkmom_fetch.build_universe
    monkeypatch.setattr(hkmom_run, "build_universe", lambda **kw: orig_uni(**{**kw, "session": sub_sess}))
    orig_dl = hkmom_fetch.download_history
    monkeypatch.setattr(hkmom_run, "download_history", lambda cl, **kw: orig_dl(cl, **{**kw, "session": sub_sess}))
    orig_comp = hkmom_compute.compute_all
    monkeypatch.setattr(hkmom_run, "compute_all", lambda **kw: orig_comp(**{**kw, "hsi_session": sub_sess}))
    orig_intra = hkmom_compute.compute_intraday_snapshot
    monkeypatch.setattr(hkmom_run, "compute_intraday_snapshot", lambda **kw: orig_intra(**{**kw, "now": dt_me, "session": sub_sess}))

    res = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))
    assert res["publish_ok"] is True
    assert res["froze"] is True

    ggt_st = res["sources"]["ggt_list"]
    assert ggt_st["ok"] is True
    assert ggt_st["stale"] is False
    assert "跳过 2 只（" in (ggt_st.get("note") or "")
    assert "代码无效" in (ggt_st.get("note") or "")
    assert "中英文名称均为空" in (ggt_st.get("note") or "")

    uni_df = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    row_skip = uni_df[uni_df["code"] == skipped_target].iloc[0]
    assert bool(row_skip["excluded"]) is True

    # 00069 must NOT appear in EITHER smallcap or nonggt month-end selection or constituents!
    frozen_doc = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    rebal_snap = frozen_doc["rebalance_snapshots"]["2026-10-30"]
    assert skipped_target not in rebal_snap["smallcap"]
    assert skipped_target not in rebal_snap["nonggt"]
    assert skipped_target in frozen_doc["rebalance_exclusions"]["2026-10-30"]["codes"]
    assert "跳过 2 只（" in (frozen_doc["rebalance_exclusions"]["2026-10-30"]["reason"] or "")

    cons_doc = json.loads((st_dir / "constituents.json").read_text(encoding="utf-8"))
    assert all(item["code"] != skipped_target for item in cons_doc["smallcap"])
    assert all(item["code"] != skipped_target for item in cons_doc["nonggt"])

    meta_doc = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert skipped_target in meta_doc["excluded_codes"]
    assert "跳过 2 只（" in (meta_doc.get("ggt_skipped_note") or "")

    html_txt = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "跳过 2 只（" in html_txt
    assert "info-banner show" in html_txt


def test_hkmom_preserved_previous_universe_merges_new_excluded_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When universe.csv is preserved (degraded run), newly identified unconfirmed codes in universe_meta.json are STILL excluded."""
    monkeypatch.setattr(hkmom_fetch.time, "sleep", lambda _s: None)
    st_dir = tmp_path / "hk_preserved_excl"

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    assert sessions[-1] == "2026-10-30"

    old_excl = "00001"
    new_skipped = "00069"
    codes = [old_excl, new_skipped] + [f"{i:05d}" for i in range(2, 130) if i != 69]
    ggt_codes = set(codes[65:])  # new_skipped is non-GGT so it affects BOTH smallcap & nonggt

    sub_sess = _build_hk_substitute_session(
        sessions=sessions, codes=codes, ggt_codes=ggt_codes, quote_date="2026/10/30"
    )
    orig_get = sub_sess.get.side_effect
    inject_skip = {"on": False}

    def _patched_get(url: str, **kw):
        if "szse.cn" in url and inject_skip["on"]:
            resp = orig_get(url, **kw)
            payload = resp.json()
            meta = payload[0]["metadata"]
            if "PAGENO=2" not in url:
                payload[0]["data"].append({"zqdm": new_skipped, "zqjc": "", "zqywjc": ""})
            meta["recordcount"] = int(meta["recordcount"]) + 1
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
        if "hkfqkline" in url and f"param=hk{new_skipped}," in url:
            bars = [
                [
                    ds,
                    f"{10.0 * (1.01 ** t):.3f}",
                    f"{10.0 * (1.01 ** t):.3f}",
                    f"{10.0 * (1.01 ** t) * 1.01:.3f}",
                    f"{10.0 * (1.01 ** t) * 0.99:.3f}",
                    "500000",
                    {},
                    "0",
                    "0",
                    "500.0",
                ]
                for t, ds in enumerate(sessions)
            ]
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": {f"hk{new_skipped}": {"qfqday": bars}}},
            )
        return orig_get(url, **kw)

    sub_sess.get.side_effect = _patched_get
    hkex_df = pd.DataFrame(
        {
            "code": codes,
            "name": [f"HK Co {i}" for i in range(len(codes))],
            "subcat": ["Equity Securities (Main Board)"] * len(codes),
            "ccy": ["HKD"] * len(codes),
        }
    )
    dt_me = datetime(2026, 10, 30, 16, 35, 0, tzinfo=feeds_common.HK_TZ)
    monkeypatch.setattr(hkmom_fetch, "_sess", lambda proxy=None: sub_sess)
    monkeypatch.setattr(hkmom_fetch, "now_hk", lambda: dt_me)
    monkeypatch.setattr(hkmom_compute, "now_hk", lambda: dt_me)

    # 1) Initial clean run: universe.csv has new_skipped with excluded=False (and seed old_excl=True).
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: hkex_df.copy())
    uni_clean, meta_clean = hkmom_fetch.build_universe(
        state_dir=st_dir, session=sub_sess, as_of_date=date(2026, 10, 30)
    )
    assert meta_clean["preserved_previous_universe"] is False
    uni_clean.loc[uni_clean["code"] == old_excl, "excluded"] = True
    uni_clean.to_csv(st_dir / "universe.csv", index=False)
    hkmom_fetch.download_history(list(uni_clean["code"]), state_dir=st_dir, session=sub_sess, target_date="2026-10-30")

    # 2) Degraded run: hkex_universe fails so old universe.csv is preserved (preserved_previous_universe=True),
    #    while ggt_list succeeds and newly identifies 00069 as unconfirmed identity.
    inject_skip["on"] = True
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", lambda **_kw: (_ for _ in ()).throw(RuntimeError("HKEX down")))
    _uni_deg, meta_deg = hkmom_fetch.build_universe(
        state_dir=st_dir, session=sub_sess, as_of_date=date(2026, 10, 30)
    )
    assert meta_deg["preserved_previous_universe"] is True
    assert new_skipped in meta_deg["excluded_codes"]

    # Confirm old universe.csv on disk still marks 00069 as excluded=False
    disk_uni = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    assert bool(disk_uni.loc[disk_uni["code"] == new_skipped, "excluded"].iloc[0]) is False

    # 3) compute_all MUST merge new_skipped from universe_meta.json (alongside old_excl from universe.csv) and exclude both!
    computed = hkmom_compute.compute_all(
        state_dir=st_dir,
        as_of_date=date(2026, 10, 30),
        hsi_session=sub_sess,
        persist=False,
    )
    assert old_excl in computed["meta"]["excluded_codes"]
    assert new_skipped in computed["meta"]["excluded_codes"]
    rebal_snap = computed["frozen_payload"]["rebalance_snapshots"]["2026-10-30"]
    assert new_skipped not in rebal_snap["smallcap"]
    assert new_skipped not in rebal_snap["nonggt"]
    assert all(item["code"] != new_skipped for item in computed["constituents"]["smallcap"])
    assert all(item["code"] != new_skipped for item in computed["constituents"]["nonggt"])


def test_hkmom_weekend_holiday_snapshot_uses_target_trade_date_close(tmp_path: Path) -> None:
    """Friday close (1100, +10%) evaluated on Saturday MUST stay 1100 (NOT 1210) and display '最近交易日（日期）收盘'."""
    st_dir = tmp_path / "hk_weekend"
    st_dir.mkdir(parents=True)
    (st_dir / "constituents.json").write_text(
        json.dumps(
            {
                "smallcap": [{"code": "00001", "name": "长江和记"}],
                "nonggt": [{"code": "00002", "name": "中电控股"}],
            }
        ),
        encoding="utf-8",
    )
    # Thursday 2026-11-12 = 1000.0; Friday 2026-11-13 = 1100.0 (+10.0%)
    (st_dir / "index_series.json").write_text(
        json.dumps(
            {
                "dates": ["2026-11-12", "2026-11-13"],
                "smallcap": [1000.0, 1100.0],
                "nonggt": [1000.0, 1100.0],
                "hsi": [1000.0, 1015.0],
            }
        ),
        encoding="utf-8",
    )
    meta_doc = {
        "base_date": "2026-11-12",
        "last_date": "2026-11-13",
        "launch_date": "2026-11-12",
        "last_rebalance_date": "2026-10-30",
        "params": {},
    }
    (st_dir / "index_meta.json").write_text(json.dumps(meta_doc), encoding="utf-8")

    # Tencent still serves Friday 2026/11/13 quotes (+10.0% on Friday) over the weekend!
    def _weekend_quote(url: str, **_kw):
        if "r_hkHSI" in url:
            f = ["0"] * 47
            f[1], f[3], f[4], f[30], f[32] = "恒生指数", "20500.0", "20200.0", "2026/11/13 16:08:00", "1.49"
            return SimpleNamespace(text=f'v_r_hkHSI="{"~".join(f)}";', encoding="gbk")
        lines = []
        for c in ("00001", "00002"):
            f = ["0"] * 47
            f[1], f[3], f[4], f[30], f[32], f[37], f[45] = f"HK{c}", "11.0", "10.0", "2026/11/13 16:08:00", "10.0", "1000", "20.0"
            lines.append(f'v_r_hk{c}="{"~".join(f)}"')
        return SimpleNamespace(text=";".join(lines), encoding="gbk")

    sess = create_autospec(requests.Session, instance=True)
    sess.get.side_effect = _weekend_quote

    # 1) Friday post-close (2026-11-13 16:35) -> daily_close=1100.0, +10.0%, is_trading_day=True
    snap_fri = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 13, 16, 35, 0, tzinfo=feeds_common.HK_TZ),
        session=sess,
    )
    assert snap_fri["stale"] is False
    assert snap_fri["is_trading_day"] is True
    assert snap_fri["smallcap"]["level"] == 1100.0
    assert snap_fri["smallcap"]["pct"] == 10.0
    assert snap_fri["smallcap"]["source"] == "daily_close"
    assert snap_fri["smallcap"]["close_date"] == "2026-11-13"

    # 2) Saturday run (2026-11-14 10:00) -> MUST NOT compound +10% into 1210.0!
    snap_sat = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 14, 10, 0, 0, tzinfo=feeds_common.HK_TZ),
        session=sess,
    )
    assert snap_sat["stale"] is False
    assert snap_sat["is_trading_day"] is False
    assert snap_sat["target_trade_date"] == "2026-11-13"
    assert snap_sat["daily_close_date"] == "2026-11-13"
    assert snap_sat["smallcap"]["level"] == 1100.0
    assert snap_sat["smallcap"]["pct"] == 10.0
    assert snap_sat["smallcap"]["source"] == "daily_close"
    assert snap_sat["smallcap"]["close_date"] == "2026-11-13"
    assert snap_sat["nonggt"]["level"] == 1100.0
    assert snap_sat["nonggt"]["source"] == "daily_close"
    assert snap_sat["nonggt"]["close_date"] == "2026-11-13"

    html_sat = hkmom_render.render_hkmom_html(
        series=json.loads((st_dir / "index_series.json").read_text(encoding="utf-8")),
        meta=meta_doc,
        constituents=json.loads((st_dir / "constituents.json").read_text(encoding="utf-8")),
        intraday=snap_sat,
    )
    assert "最近交易日（2026-11-13）收盘" in html_sat
    assert "formatCloseTag" in html_sat


def test_ashare_target_trade_date_0025_and_history_staleness(tmp_path: Path) -> None:
    """At 00:25 BJT, A-share trade_date is previous trading day (2026-11-17) and stale history blocks last_success_at."""
    base_get, base_json = _make_ashare_handlers()
    tick = [datetime(2026, 11, 17, 14, 30, 0, tzinfo=feeds_common.CN_TZ)]
    sohu_mode = {"dates": [("11/16", "2026-11-16")]}
    sse_mode = {"latest": "2026-11-16", "prev": "2026-11-13"}

    def _sohu_html(items: list[tuple[str, str]]) -> str:
        rows_html = "".join(
            f"<tr><td>{mmdd}</td><td>50</td><td>5</td><td>6</td><td>18000.0</td>"
            "<td>1200</td><td>90</td><td>900</td><td>1400</td><td>100</td><td>1100</td><td>120</td><td>15</td><td>90</td></tr>"
            for mmdd, _ in items
        )
        return (
            "<html><body><table>"
            "<tr><th>日期</th><th>涨停</th><th>跌停</th><th>停牌</th><th>成交额</th>"
            '<th colspan="3">沪</th><th colspan="3">深</th><th colspan="3">京</th></tr>'
            f"{rows_html}</table></body></html>"
        )

    def _custom_get(url: str, **kw) -> str:
        if "q.stock.sohu.com" in url:
            return _sohu_html(sohu_mode["dates"])
        if "query.sse.com.cn" in url:
            if "STAT_DATE=2026-" in url:
                return "jsonpCallback(" + json.dumps(
                    {
                        "result": [
                            {"SEC_CODE": "512480", "STAT_DATE": sse_mode["prev"], "TOT_VOL": "95000"},
                            {"SEC_CODE": "510300", "STAT_DATE": sse_mode["prev"], "TOT_VOL": "490000"},
                        ]
                    }
                ) + ")"
            return "jsonpCallback(" + json.dumps(
                {
                    "result": [
                        {"SEC_CODE": "512480", "STAT_DATE": sse_mode["latest"], "TOT_VOL": "100000"},
                        {"SEC_CODE": "510300", "STAT_DATE": sse_mode["latest"], "TOT_VOL": "500000"},
                    ]
                }
            ) + ")"
        return base_get(url, **kw)

    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare_0025",
        http_get=create_autospec(ashare_collect.default_http_get, side_effect=_custom_get),
        http_get_json=create_autospec(ashare_collect.default_http_get_json, side_effect=base_json),
        now_fn=lambda: tick[0],
    )

    # 1) Clean intraday baseline at 2026-11-17 14:30 BJT -> trade_date=2026-11-17, last_success_at=2026-11-17 14:30:00
    snap_base = collector.collect_snapshot()
    assert snap_base["stale"] is False
    assert snap_base["trade_date"] == "2026-11-17"
    prior_ok = "2026-11-17 14:30:00"
    assert snap_base["last_success_at"] == prior_ok

    # 2) Run at 2026-11-18 00:25 BJT with history stuck at earlier date 2026-11-13 ->
    #    trade_date MUST be 2026-11-17 (NOT 2026-11-18!), daily_history MUST be stale=True, last_success_at unchanged!
    tick[0] = datetime(2026, 11, 18, 0, 25, 0, tzinfo=feeds_common.CN_TZ)
    sohu_mode["dates"] = [("11/12", "2026-11-12"), ("11/13", "2026-11-13")]
    collector._history_cache["ts"] = 0.0
    collector._official_cache["ts"] = 0.0
    snap_stale_hist = collector.collect_snapshot()
    assert snap_stale_hist["trade_date"] == "2026-11-17"
    assert snap_stale_hist["stale"] is True
    assert "daily_history" in snap_stale_hist["stale_sources"]
    assert snap_stale_hist["sources"]["daily_history"]["ok"] is False
    assert snap_stale_hist["sources"]["daily_history"]["stale"] is True
    assert snap_stale_hist["last_success_at"] == prior_ok
    assert snap_stale_hist["sources"]["daily_history"]["last_success_at"] == prior_ok

    # 3) History updated to 2026-11-17 (with SSE official shares at 2026-11-16 before morning release) -> clean!
    sohu_mode["dates"] = [("11/13", "2026-11-13"), ("11/17", "2026-11-17")]
    collector._history_cache["ts"] = 0.0
    collector._official_cache["ts"] = 0.0
    snap_ok_0025 = collector.collect_snapshot()
    assert snap_ok_0025["trade_date"] == "2026-11-17"
    assert snap_ok_0025["stale"] is False
    assert snap_ok_0025["last_success_at"] == "2026-11-18 00:25:00"

    # 4) At 2026-11-18 00:25 BJT, if SSE official shares lag back at 2026-11-13 (< 2026-11-16) -> stale!
    sse_mode["latest"] = "2026-11-13"
    sse_mode["prev"] = "2026-11-12"
    collector._official_cache["ts"] = 0.0
    snap_stale_sse = collector.collect_snapshot()
    assert snap_stale_sse["stale"] is True
    assert "etf_official_shares" in snap_stale_sse["stale_sources"]
    assert snap_stale_sse["last_success_at"] == "2026-11-18 00:25:00"


def test_ashare_official_shares_0900_cutoff_and_cache_staleness(tmp_path: Path) -> None:
    """Official ETF shares expected date switches at 09:00 BJT; at 09:25 data only through 11-16 (live or cached) is stale."""
    base_get, base_json = _make_ashare_handlers()
    tick = [datetime(2026, 11, 18, 8, 55, 0, tzinfo=feeds_common.CN_TZ)]
    sse_mode = {"latest": "2026-11-16", "prev": "2026-11-13"}
    szse_mode = {"latest": "2026-11-16", "prev": "2026-11-13"}

    def _sohu_1117() -> str:
        return (
            "<html><body><table>"
            "<tr><th>日期</th><th>涨停</th><th>跌停</th><th>停牌</th><th>成交额</th>"
            '<th colspan="3">沪</th><th colspan="3">深</th><th colspan="3">京</th></tr>'
            "<tr><td>11/17</td><td>50</td><td>5</td><td>6</td><td>18000.0</td>"
            "<td>1200</td><td>90</td><td>900</td><td>1400</td><td>100</td><td>1100</td><td>120</td><td>15</td><td>90</td></tr>"
            "</table></body></html>"
        )

    def _custom_get(url: str, **kw) -> str:
        if "q.stock.sohu.com" in url:
            return _sohu_1117()
        if "query.sse.com.cn" in url:
            if "STAT_DATE=2026-" in url:
                return "jsonpCallback(" + json.dumps(
                    {
                        "result": [
                            {"SEC_CODE": "512480", "STAT_DATE": sse_mode["prev"], "TOT_VOL": "95000"},
                            {"SEC_CODE": "510300", "STAT_DATE": sse_mode["prev"], "TOT_VOL": "490000"},
                        ]
                    }
                ) + ")"
            return "jsonpCallback(" + json.dumps(
                {
                    "result": [
                        {"SEC_CODE": "512480", "STAT_DATE": sse_mode["latest"], "TOT_VOL": "100000"},
                        {"SEC_CODE": "510300", "STAT_DATE": sse_mode["latest"], "TOT_VOL": "500000"},
                    ]
                }
            ) + ")"
        if "szse.cn" in url:
            return json.dumps(
                [
                    {
                        "data": [
                            {"fund_code": "159915", "size_date": szse_mode["latest"], "current_size": "200000"},
                            {"fund_code": "159915", "size_date": szse_mode["prev"], "current_size": "195000"},
                        ]
                    }
                ]
            )
        return base_get(url, **kw)

    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare_0900",
        http_get=create_autospec(ashare_collect.default_http_get, side_effect=_custom_get),
        http_get_json=create_autospec(ashare_collect.default_http_get_json, side_effect=base_json),
        now_fn=lambda: tick[0],
    )

    # Boundary checks for _expected_official_min_date
    assert collector._expected_official_min_date(
        datetime(2026, 11, 18, 8, 59, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 16)
    assert collector._expected_official_min_date(
        datetime(2026, 11, 18, 9, 0, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 17)
    assert collector._expected_official_min_date(
        datetime(2026, 11, 18, 9, 25, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 17)
    assert collector._expected_official_min_date(
        datetime(2026, 11, 16, 8, 55, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 12)
    assert collector._expected_official_min_date(
        datetime(2026, 11, 16, 9, 25, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 13)
    assert collector._expected_official_min_date(
        datetime(2026, 11, 14, 10, 0, 0, tzinfo=feeds_common.CN_TZ)
    ) == date(2026, 11, 12)

    # 1) At 2026-11-18 08:55 BJT (< 09:00), 11-16 data is valid and populates _official_cache cleanly.
    snap_0855 = collector.collect_snapshot()
    assert snap_0855["stale"] is False
    assert snap_0855["last_success_at"] == "2026-11-18 08:55:00"
    assert collector._official_cache["error"] is None

    # 2) Advance to 2026-11-18 09:25 BJT WITHOUT clearing _official_cache["ts"] (still within 30m TTL):
    #    cached 11-16 data MUST be invalidated against 11-17 expectation; upstream still at 11-16 -> stale!
    tick[0] = datetime(2026, 11, 18, 9, 25, 0, tzinfo=feeds_common.CN_TZ)
    snap_0925_stale = collector.collect_snapshot()
    assert snap_0925_stale["stale"] is True
    assert "etf_official_shares" in snap_0925_stale["stale_sources"]
    assert snap_0925_stale["last_success_at"] == "2026-11-18 08:55:00"
    with pytest.raises(RuntimeError, match="最新日期过期"):
        collector._fetch_szse_pair("159915")

    # 3) Upstream updates to 2026-11-17 -> recovers cleanly at 09:25 BJT.
    sse_mode["latest"], sse_mode["prev"] = "2026-11-17", "2026-11-16"
    szse_mode["latest"], szse_mode["prev"] = "2026-11-17", "2026-11-16"
    collector._official_cache["ts"] = 0.0
    snap_0925_ok = collector.collect_snapshot()
    assert snap_0925_ok["stale"] is False
    assert snap_0925_ok["last_success_at"] == "2026-11-18 09:25:00"


