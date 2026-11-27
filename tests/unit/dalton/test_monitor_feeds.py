"""Offline unit tests for migrated monitor feeds (PM L1/L2, A-share intraday, HK momentum)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
import math
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import numpy as np
import pandas as pd
import pytest
import requests
import yfinance as yf

from scripts.dalton.monitor_feeds import common as feeds_common
from scripts.dalton.monitor_feeds.ashare_monitor import collect as ashare_collect
from scripts.dalton.monitor_feeds.ashare_monitor import render as ashare_render
from scripts.dalton.monitor_feeds.hk_momentum import compute as hkmom_compute
from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkmom_fetch
from scripts.dalton.monitor_feeds.hk_momentum import render as hkmom_render
from scripts.dalton.monitor_feeds.hk_momentum import run as hkmom_run
from scripts.dalton.monitor_feeds.pm_cockpit import collect as pm_collect
from scripts.dalton.monitor_feeds.pm_cockpit import compute as pm_compute
from scripts.dalton.monitor_feeds.pm_cockpit import render as pm_render
from scripts.dalton.monitor_feeds.run_feeds import FeedDaemon
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.monitors import data_job_registry

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "monitor_feeds"


# ---------------------------------------------------------------------------
# 1. Registry, Manifest & Path Isolation
# ---------------------------------------------------------------------------


def test_manifest_and_monitor_registry_entries() -> None:
    manifest = load_data_task_manifest()
    by_key = manifest.by_job_key()
    for job_key, expected_code in (
        ("pm-market-state", "T84"),
        ("cn-intraday", "T85"),
        ("hk-momentum", "T86"),
    ):
        assert job_key in by_key
        entry = by_key[job_key]
        assert entry.task_code == expected_code
        assert entry.runner == "container"
        assert entry.enabled is False
        assert entry.notify_enabled is False
        assert "~/.openclaw" not in entry.command
    assert by_key["pm-market-state"].no_output_timeout_seconds == 1800

    pm_bindings = data_job_registry.get_registered_monitor_bindings("pm-market-state")
    assert [b.slug for b in pm_bindings] == ["pm-market-state", "cn-sector-rotation"]
    assert [b.category for b in pm_bindings] == ["us", "cn"]
    assert all(b.visibility == "company" for b in pm_bindings)

    cn_binding = data_job_registry.get_registered_monitor_job("cn-intraday")
    assert cn_binding is not None
    assert cn_binding.slug == "cn-intraday"
    assert cn_binding.category == "cn"
    assert cn_binding.data_keys == ("snapshot",)

    hk_binding = data_job_registry.get_registered_monitor_job("hk-momentum")
    assert hk_binding is not None
    assert hk_binding.slug == "hk-momentum"
    assert hk_binding.category == "hk"
    assert hk_binding.data_keys == ("intraday",)


def test_state_dir_defaults_to_var_dalton_monitor_feeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DALTON_MONITOR_FEEDS_STATE_DIR", raising=False)
    default_path = feeds_common.resolve_state_dir("ashare_monitor")
    assert ".openclaw" not in str(default_path)
    assert default_path.parts[-3:] == ("var", "dalton", "monitor-feeds") or default_path.parts[-2:] == ("monitor-feeds", "ashare_monitor")

    monkeypatch.setenv("DALTON_MONITOR_FEEDS_STATE_DIR", str(tmp_path / "custom-feeds"))
    custom = feeds_common.resolve_state_dir("hk_momentum")
    assert custom == tmp_path / "custom-feeds" / "hk_momentum"
    assert custom.is_dir()


# ---------------------------------------------------------------------------
# 2. PM Cockpit L1 & L2 (offline yfinance + akshare doubles via create_autospec)
# ---------------------------------------------------------------------------


def _build_pm_fixtures(cfg: dict[str, Any]):
    dates = pd.bdate_range("2023-01-02", periods=600)
    rng = np.random.default_rng(42)

    # US tickers MultiIndex DataFrame mimicking yfinance auto_adjust=False
    us_tk = pm_collect.us_tickers(cfg)
    cols = pd.MultiIndex.from_product([["Adj Close", "Close"], us_tk])
    data = {}
    for idx, tk in enumerate(us_tk):
        drift = 0.0003 * (1 + (idx % 5) * 0.1)
        series = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, len(dates))))
        if tk == "^VIX":
            series = np.full(len(dates), 16.5)
        elif tk == "^VIX3M":
            series = np.full(len(dates), 19.2)
        data[("Adj Close", tk)] = series
        data[("Close", tk)] = series * 0.99
    us_df = pd.DataFrame(data, index=dates, columns=cols)

    def _yf_download(*_args, **_kwargs):
        return us_df

    # Fake akshare module with create_autospec methods
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
                    {"行业代码": "801999.SI", "行业名称": "微小行业", "上级行业": "农林牧渔", "成份个数": 2, "TTM(滚动)市盈率": 15.0, "市净率": 1.5, "静态股息率": 2.0},
                ]
            )

        def index_hist_sw(self, symbol: str, period: str = "day") -> pd.DataFrame:
            mult = 1.15 if symbol in ("801080", "801081") else 0.95
            closes = np.linspace(1000.0, 1000.0 * mult, len(dates))
            return pd.DataFrame(
                {"日期": dates.strftime("%Y-%m-%d"), "收盘": closes, "成交额": np.full(len(dates), 120.0)}
            )

        def index_analysis_week_month_sw(self, symbol: str = "week") -> pd.DataFrame:
            fridays = pd.date_range("2022-01-07", "2025-12-26", freq="W-FRI")
            return pd.DataFrame({"date": fridays.strftime("%Y-%m-%d")})

        def index_analysis_weekly_sw(self, symbol: str, date: str) -> pd.DataFrame:
            codes = ["801010", "801080"] if symbol == "一级行业" else ["801081", "801012"]
            return pd.DataFrame(
                [
                    {"指数代码": f"{c}.SI", "市盈率": 20.0 + (hash(date + c) % 15), "市净率": 2.0 + (hash(date) % 10) * 0.1, "股息率": 1.5}
                    for c in codes
                ]
            )

        def index_analysis_daily_sw(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            if symbol == "一级行业":
                return pd.DataFrame(
                    [
                        {"指数代码": "801010.SI", "发布日期": "2026-11-16", "市盈率": 22.0, "市净率": 2.4, "股息率": 1.5},
                        {"指数代码": "801080.SI", "发布日期": "2026-11-16", "市盈率": 48.0, "市净率": 3.8, "股息率": 0.8},
                    ]
                )
            return pd.DataFrame(
                [
                    {"指数代码": "801081.SI", "发布日期": "2026-11-16", "市盈率": 65.0, "市净率": 4.2, "股息率": 0.4},
                    {"指数代码": "801012.SI", "发布日期": "2026-11-16", "市盈率": 950.0, "市净率": 1.9, "股息率": 1.2},
                ]
            )

    fake_yf = SimpleNamespace(download=create_autospec(yf.download, side_effect=_yf_download))
    real_ak = _FakeAk()
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


def test_pm_cockpit_l1_l2_collect_compute_and_render(tmp_path: Path) -> None:
    cfg = pm_collect.load_config()
    assert "risk_budget" not in cfg

    fake_yf, fake_ak = _build_pm_fixtures(cfg)
    state_dir = tmp_path / "state" / "pm_cockpit"
    out_dir = tmp_path / "out"

    progress_msgs: list[str] = []
    meta = pm_collect.collect_all(
        which="all",
        state_dir=state_dir,
        yf_module=fake_yf,
        ak_module=fake_ak,
        progress_fn=progress_msgs.append,
    )
    assert any("stage cn" in m for m in progress_msgs)
    assert any("fetch_cn:" in m for m in progress_msgs)
    assert meta["us"]["ok"] is True
    assert meta["cn"]["ok"] is True
    assert meta["cn_valuation"]["ok"] is True

    panels = pm_compute.compute_panels(state_dir=state_dir)
    assert "l1" in panels and "l2_us" in panels and "l2_cn" in panels
    for forbidden in ("l3", "l4", "l5", "watchlist", "positions", "journal"):
        assert forbidden not in panels

    # Small constituent count (<5) industry 801999 must be filtered out; 801012 PE=950 flagged excluded
    l2_codes = {r["ticker"]: r for r in panels["l2_cn"]["rows_l2"]}
    assert "801999" not in l2_codes
    assert "801081" in l2_codes
    assert l2_codes["801012"]["pe_excluded"] is not None

    outputs = pm_render.render_pm_panels(state_dir=state_dir, output_dir=out_dir, panels=panels)
    us_html = outputs["pm-market-state"].read_text(encoding="utf-8")
    cn_html = outputs["cn-sector-rotation"].read_text(encoding="utf-8")

    # Verify language & visual standards
    for doc in (us_html, cn_html):
        assert "color-legend" in doc
        assert "STEP 0" not in doc
        assert "PM Cockpit" not in doc
        assert "panels.json" not in doc
        assert "journal.py" not in doc
    assert "上涨 / 正超额 / 领先：红" in us_html
    assert "美东时间" in us_html
    assert "北京时间" in cn_html
    assert "当日涨跌%" in cn_html and "近1周超额%" in cn_html
    assert "估值历史分位(越低越便宜)" in cn_html
    assert "估值性价比(1–5分，5分最便宜)" in cn_html
    assert "盈利失真(剔除)" in cn_html
    assert "2021-12-31 起算" in cn_html
    # Verify fix #1 (renderL1Table appends wrap into card) & fix #9 ("改善" is top-left)
    l1_fn = re.search(r"function renderL1Table\(cn\)\s*\{.*?\n\}", cn_html, re.DOTALL)
    assert l1_fn is not None
    assert "wrap.appendChild(t);" in l1_fn.group(0)
    assert "c.appendChild(wrap);" in l1_fn.group(0)
    assert "左上「改善」" in cn_html and "左下「改善」" not in cn_html

    # Verify stale banner renders when collection fails
    stale_panels = dict(panels)
    stale_panels["us_status"] = {
        "stale": True,
        "reason": "yfinance 超时",
        "last_success_at": "2026-11-15 17:10:00 美东时间",
    }
    stale_out = pm_render.render_pm_panels(
        state_dir=state_dir, output_dir=tmp_path / "stale_out", panels=stale_panels
    )
    stale_us = stale_out["pm-market-state"].read_text(encoding="utf-8")
    assert "stale-banner show" in stale_us
    assert "数据未更新（上次成功：2026-11-15 17:10:00 美东时间）" in stale_us


# ---------------------------------------------------------------------------
# 3. A-share Intraday Monitor (unified turnover + stale handling + HTML)
# ---------------------------------------------------------------------------


def _fixture_http_handlers(
    *,
    sina_override: str | None = None,
    fail_sina: bool = False,
    fail_breadth: bool = False,
    fail_etf: bool = False,
):
    sina_txt = (
        sina_override
        if sina_override is not None
        else (FIXTURES_DIR / "sina_indices_sample.txt").read_text(encoding="utf-8")
    )
    breadth_json = json.loads(
        (FIXTURES_DIR / "eastmoney_breadth_sample.json").read_text(encoding="utf-8")
    )
    sohu_html = (FIXTURES_DIR / "sohu_zdt_sample.html").read_text(encoding="utf-8")

    def fake_get(url: str, **kwargs) -> str:
        if "hq.sinajs.cn" in url:
            if fail_sina:
                raise RuntimeError("Sina timeout")
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

    def fake_get_json(url: str, **kwargs) -> Any:
        if "ulist.np/get" in url:
            if fail_breadth:
                raise RuntimeError("Breadth 502")
            return breadth_json
        if "MK0021" in url:
            if fail_etf:
                raise RuntimeError("ETF 502")
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

    return (
        create_autospec(ashare_collect.default_http_get, side_effect=fake_get),
        create_autospec(ashare_collect.default_http_get_json, side_effect=fake_get_json),
    )


def test_ashare_turnover_uses_sz_composite_and_marks_stale_on_failure(tmp_path: Path) -> None:
    trading_dt = datetime(2026, 11, 16, 10, 30, 0, tzinfo=feeds_common.CN_TZ)
    get_mock, json_mock = _fixture_http_handlers()

    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare",
        http_get=get_mock,
        http_get_json=json_mock,
        now_fn=lambda: trading_dt,
    )
    snap = collector.collect_snapshot()
    assert snap["stale"] is False
    # s_sh000001 amount_wan = 51200000 (5120亿) + s_sz399106 amount_wan = 68800000 (6880亿) = 12000亿
    # (NOT s_sz399001 = 35000000 which would give 8620亿!)
    assert snap["two_market_yi"] == 12000.0
    assert "深证综指" in snap["turnover_basis"]
    assert snap["industry"]["mapping_asof"] == "2026-07-31"
    assert len(snap["daily_history"]) == 2
    assert len(snap["indices"]) == 7

    # Simulate partial source failure (breadth fails & sz399106 missing) -> stale=True, retains 12000 & breadth
    bad_sina = (
        (FIXTURES_DIR / "sina_indices_sample.txt")
        .read_text(encoding="utf-8")
        .splitlines()[:-1]
    )
    get_fail, json_fail = _fixture_http_handlers(
        sina_override="\n".join(bad_sina),
        fail_breadth=True,
    )
    collector_fail = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare",
        http_get=get_fail,
        http_get_json=json_fail,
        now_fn=lambda: datetime(2026, 11, 16, 10, 31, 0, tzinfo=feeds_common.CN_TZ),
    )
    snap_stale = collector_fail.collect_snapshot()
    assert snap_stale["stale"] is True
    assert snap_stale["two_market_yi"] == 12000.0
    assert snap_stale["breadth"] == snap["breadth"]
    assert snap_stale["last_success_at"] == "2026-11-16 10:30:00"
    assert "涨跌家数" in (snap_stale["stale_reason"] or "")

    # Verify rendered HTML copy, legend, grant poller, right axis & typo fixes
    html_out = ashare_render.render_ashare_html(snap)
    assert "dalton:monitor-grant" in html_out
    assert "DATA_KEY = \"snapshot\"" in html_out
    assert "2026-07-31" in html_out
    assert "交时段" not in html_out
    assert "交易时段" in html_out
    assert "V3.0" not in html_out
    assert "proxy" not in html_out
    assert "搜狐" not in html_out
    assert "上期官方申赎(亿元)" in html_out
    assert "每分钟速率（右轴，亿元/分）" in html_out


# ---------------------------------------------------------------------------
# 4. HK Momentum (month_ends fix, frozen history, quote date & missing quotes)
# ---------------------------------------------------------------------------


def test_hkmom_month_ends_excludes_ongoing_month() -> None:
    # Nov 16 is NOT the end of November; Oct 30 IS the last HKEX trading day of Oct 2026.
    dates = ["2026-09-30", "2026-10-15", "2026-10-30", "2026-11-02", "2026-11-16"]
    mes = hkmom_compute.month_ends(dates, as_of_date=date(2026, 11, 16))
    assert mes == ["2026-09-30", "2026-10-30"]

    # When Nov 30 arrives (actual last trading day of Nov 2026), Nov 30 IS included.
    dates_full = dates + ["2026-11-30"]
    mes_full = hkmom_compute.month_ends(dates_full, as_of_date=date(2026, 11, 30))
    assert mes_full == ["2026-09-30", "2026-10-30", "2026-11-30"]


def test_hkmom_frozen_history_does_not_rewrite_past_rebalances(tmp_path: Path) -> None:
    st_dir = tmp_path / "hk_momentum"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True)

    # Create 310 HKEX trading days ending 2026-11-16 (crosses Oct 30 month-end, Nov ongoing)
    import exchange_calendars as xcals

    xhkg = xcals.get_calendar("XHKG")
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-11-16")]
    assert len(sessions) >= 300

    codes = [f"{i:05d}" for i in range(1, 56)]
    uni_rows = []
    for idx, c in enumerate(codes):
        uni_rows.append(
            {
                "code": c,
                "name": f"HK{c}",
                "subcat": "Ordinary Shares",
                "ccy": "HKD",
                "is_ggt": idx >= 45,  # 45 non-GGT smallcap + 10 GGT
                "price": 10.0 + idx * 0.1,
                "mktcap_e8": 20.0,  # < 50亿港元
                "name_cn": f"港股{c}",
            }
        )
        # Create upward trending prices so scores vary across stocks
        p0 = 10.0
        rows = []
        for t_i, dt_str in enumerate(sessions):
            px = p0 * (1.0 + 0.0004 * t_i * (1 + (idx % 7) * 0.2)) * (1.0 + 0.015 * math.sin(t_i * 0.7 + idx))
            rows.append([dt_str, px, px, px * 1.01, px * 0.99, 100000, 200.0])  # median turnover 200万 >= 100万
        pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)

    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股{c}" for c in codes[45:]}), encoding="utf-8"
    )

    class _FakeHsiResp:
        def json(self):
            return {"data": {"hkHSI": {"day": [[d, 20000, 20000 + i] for i, d in enumerate(sessions)]}}}

    fake_sess = create_autospec(requests.Session, instance=True)
    fake_sess.get.return_value = _FakeHsiResp()

    res1 = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 16), hsi_session=fake_sess
    )
    frozen_before = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert res1["meta"]["last_rebalance_date"] == "2026-10-30"
    assert res1["meta"]["launch_date"] == "2026-11-16"

    # Now mutate GGT list AND drop stocks from universe -> re-running compute_all MUST preserve frozen past rebalance & levels!
    (st_dir / "ggt_list.json").write_text(
        json.dumps({c: f"港股{c}" for c in codes[0:20]}), encoding="utf-8"
    )
    uni_trimmed = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    uni_trimmed = uni_trimmed[~uni_trimmed["code"].isin([codes[0], codes[1], codes[2]])]
    uni_trimmed.to_csv(st_dir / "universe.csv", index=False)
    for dropped in codes[:3]:
        (hist_dir / f"{dropped}.csv").unlink()

    res2 = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 16), hsi_session=fake_sess
    )
    assert res2["series"]["smallcap"] == res1["series"]["smallcap"]
    assert res2["series"]["nonggt"] == res1["series"]["nonggt"]
    frozen_after = json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8"))
    assert frozen_after["rebalance_snapshots"] == frozen_before["rebalance_snapshots"]

    # Restore deleted held constituents with extended 2-day history so clean cross-day append succeeds;
    # first verify that extending ONLY codes[3:] while held codes[:3] are absent blocks append & marks stale!
    extra_sessions = ["2026-11-17", "2026-11-18"]
    all_sessions = sessions + extra_sessions
    for idx, c in enumerate(codes[3:], start=3):
        p = hist_dir / f"{c}.csv"
        df_c = pd.read_csv(p)
        last_px = float(df_c["close"].iloc[-1])
        ext = pd.DataFrame(
            [
                [d_s, last_px * (1 + 0.01 * (j + 1)), last_px * (1 + 0.01 * (j + 1)), last_px * 1.02, last_px * 0.99, 100000, 200.0]
                for j, d_s in enumerate(extra_sessions)
            ],
            columns=df_c.columns,
        )
        pd.concat([df_c, ext], ignore_index=True).to_csv(p, index=False)

    class _FakeHsiExtendedResp:
        def json(self):
            return {"data": {"hkHSI": {"day": [[d, 20000, 20000 + i] for i, d in enumerate(all_sessions)]}}}

    fake_sess_ext = create_autospec(requests.Session, instance=True)
    fake_sess_ext.get.return_value = _FakeHsiExtendedResp()

    res_missing_held = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 18), hsi_session=fake_sess_ext
    )
    assert res_missing_held["meta"]["series_stale"] is True
    assert res_missing_held["series"]["dates"][-1] == res1["series"]["dates"][-1]

    # Restore deleted held constituents with full history + universe entries -> now cross-day append proceeds!
    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    for idx, c in enumerate(codes[:3]):
        rows = []
        for t_i, dt_str in enumerate(all_sessions):
            px = 10.0 * (1.0 + 0.0004 * t_i * (1 + (idx % 7) * 0.2)) * (1.0 + 0.015 * math.sin(t_i * 0.7 + idx))
            rows.append([dt_str, px, px, px * 1.01, px * 0.99, 100000, 200.0])
        pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"],
        ).to_csv(hist_dir / f"{c}.csv", index=False)

    # 1) HSI fetch failure during cross-day append MUST NOT overwrite frozen hsi with None!
    n_prev = len(res1["series"]["dates"])
    fake_sess_broken = create_autospec(requests.Session, instance=True)
    fake_sess_broken.get.side_effect = RuntimeError("HSI kline timeout")
    res_hsi_fail = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 18), hsi_session=fake_sess_broken
    )
    assert res_hsi_fail["meta"]["series_stale"] is True
    assert res_hsi_fail["series"]["hsi"] is not None
    assert res_hsi_fail["series"]["hsi"][:n_prev] == res1["series"]["hsi"]

    # 2) When HSI recovers, cross-day append heals new days while preserving frozen prefix identically!
    res3 = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 18), hsi_session=fake_sess_ext
    )
    assert res3["series"]["dates"][:n_prev] == res1["series"]["dates"]
    assert res3["series"]["dates"][n_prev:] == extra_sessions
    assert res3["series"]["smallcap"][:n_prev] == res1["series"]["smallcap"]
    assert res3["series"]["nonggt"][:n_prev] == res1["series"]["nonggt"]
    assert res3["series"]["hsi"][:n_prev] == res1["series"]["hsi"]
    assert res3["meta"]["series_stale"] is False

    # Same-day recompute (no tail_dates) with failing HSI fetch -> keeps frozen HSI AND marks series_stale=True!
    res_same_day_hsi_fail = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 18), hsi_session=fake_sess_broken
    )
    assert res_same_day_hsi_fail["meta"]["series_stale"] is True
    assert "恒生指数" in (res_same_day_hsi_fail["meta"]["series_stale_reason"] or "")
    assert res_same_day_hsi_fail["series"]["hsi"] == res3["series"]["hsi"]

    # New data has 2026-11-19 but is MISSING last_f_date anchor (2026-11-18) -> MUST preserve frozen series!
    for c in codes[3:]:
        p = hist_dir / f"{c}.csv"
        df_c = pd.read_csv(p)
        df_c.loc[df_c["date"] == "2026-11-18", "date"] = "2026-11-19"
        df_c.to_csv(p, index=False)
    res_missing_anchor = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 19), hsi_session=fake_sess_ext
    )
    assert res_missing_anchor["meta"]["series_stale"] is True
    assert res_missing_anchor["series"]["smallcap"] == res3["series"]["smallcap"]

    # Check rendered HTML: no external CDN, no raw placeholders, median turnover wording, launch_date shown
    out_html = hkmom_render.render_hkmom_html(
        series=res3["series"],
        meta=res3["meta"],
        constituents=res3["constituents"],
        intraday={"as_of": "2026-11-18 15:30:00", "trading": True, "stale": False},
    )
    assert "echarts.min.js" not in out_html
    assert not re.search(r"\bEve\b", out_html)
    assert "OpenClaw" not in out_html
    assert "{min_turn_wan:" not in out_html
    assert "{min_price:" not in out_html
    seed_m = re.search(r'<script id="hkmom-seed"[^>]*>(.*?)</script>', out_html, re.DOTALL)
    assert seed_m is not None
    visible_html = re.sub(r"<script\b.*?</script>", "", out_html, flags=re.DOTALL)
    ph_re = r"\{(?:min_turn_wan|min_price|smallcap_limit|top_n|launch_date|base_date|last_date)(?::[^}]*)?\}"
    assert not re.search(ph_re, visible_html)
    assert not re.search(ph_re, seed_m.group(1))
    assert "近 3 个月日成交额中位数 ≥ 100 万港元" in out_html
    assert "股价 ≥ 0.10 港元" in out_html
    assert "2026-11-16" in out_html
    assert "12-1月涨幅(剔除近1月)" in out_html
    assert "DATA_KEY = \"intraday\"" in out_html


def test_hkmom_intraday_quote_date_and_missing_quote_handling(tmp_path: Path) -> None:
    st_dir = tmp_path / "hk_live"
    st_dir.mkdir(parents=True)
    (st_dir / "constituents.json").write_text(
        json.dumps(
            {
                "smallcap": [
                    {"code": "00001", "name": "长江和记"},
                    {"code": "00002", "name": "中电控股"},
                    {"code": "00003", "name": "旧日期测试"},
                    {"code": "00004", "name": "缺失测试"},
                ],
                "nonggt": [
                    {"code": "00001", "name": "长江和记"},
                    {"code": "00002", "name": "中电控股"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (st_dir / "index_series.json").write_text(
        json.dumps({"dates": ["2026-11-13"], "smallcap": [1000.0], "nonggt": [1000.0]}),
        encoding="utf-8",
    )

    spot_txt = (FIXTURES_DIR / "tencent_hk_spot_sample.txt").read_text(encoding="utf-8")

    class _FakeResp:
        def __init__(self, text: str) -> None:
            self.text = text
            self.encoding = "gbk"

    def _get_quote(url: str, **_kwargs):
        if "r_hkHSI" in url:
            return _FakeResp('v_r_hkHSI="50~恒生指数~HSI~20500.0~20300.0~' + "~" * 25 + '2026/11/16 15:30:00~200~0.99";')
        return _FakeResp(spot_txt)

    fake_sess = create_autospec(requests.Session, instance=True)
    fake_sess.get.side_effect = _get_quote

    # smallcap has 2/4 valid today (2026-11-16) quotes (00003 has old date 2026-11-13, 00004 missing).
    # Coverage = 2/4 = 50% -> partial_quotes=True, missing stocks held at 0% (NOT averaged over just 2!).
    # (0.98 + 0.77) / 4 = 0.4375% -> 0.44% (if wrongly averaged over 2, would be 0.88%!)
    snap = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 16, 15, 30, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess,
    )
    assert snap["stale"] is False
    assert snap["smallcap"]["n_quoted"] == 2
    assert snap["smallcap"]["n_expected"] == 4
    assert snap["smallcap"]["partial_quotes"] is True
    assert snap["smallcap"]["missing_codes"] == ["00003", "00004"]
    assert snap["smallcap"]["pct"] == 0.44
    assert snap["nonggt"]["partial_quotes"] is False
    assert snap["nonggt"]["pct"] == 0.88

    # Now simulate API failure -> marks stale=True and retains previous valid level!
    fake_sess_err = create_autospec(requests.Session, instance=True)
    fake_sess_err.get.side_effect = RuntimeError("Tencent 503")
    snap_err = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 16, 15, 31, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess_err,
    )
    assert snap_err["stale"] is True
    assert snap_err["smallcap"]["level"] == snap["smallcap"]["level"]
    assert snap_err["last_success_at"] == "2026-11-16 15:30:00"


# ---------------------------------------------------------------------------
# 5. Exchange Calendar & Host Daemon (run_feeds.py)
# ---------------------------------------------------------------------------


def test_trading_calendar_skips_holidays_and_lunch_breaks() -> None:
    # 2026-10-01 (Thursday) is China National Day & HK National Day holiday
    assert feeds_common.is_trading_day("cn", date(2026, 10, 1)) is False
    assert feeds_common.is_trading_day("hk", date(2026, 10, 1)) is False
    assert feeds_common.market_session_state("cn", datetime(2026, 10, 1, 10, 0, tzinfo=feeds_common.CN_TZ))["status"] == "holiday"

    # 2026-11-16 (Monday) is a trading day; lunch breaks are skipped
    assert feeds_common.market_session_state("cn", datetime(2026, 11, 16, 10, 0, tzinfo=feeds_common.CN_TZ))["in_trading_window"] is True
    assert feeds_common.market_session_state("cn", datetime(2026, 11, 16, 12, 0, tzinfo=feeds_common.CN_TZ))["status"] == "lunch_break"
    assert feeds_common.market_session_state("cn", datetime(2026, 11, 16, 12, 0, tzinfo=feeds_common.CN_TZ))["in_trading_window"] is False
    assert feeds_common.market_session_state("hk", datetime(2026, 11, 16, 12, 30, tzinfo=feeds_common.HK_TZ))["status"] == "lunch_break"
    assert feeds_common.market_session_state("hk", datetime(2026, 11, 16, 15, 30, tzinfo=feeds_common.HK_TZ))["in_trading_window"] is True


def test_feed_daemon_trading_post_close_stale_and_token_safety(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_tok = "super-secret-dalton-token-999"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", secret_tok)

    pushed: list[tuple[str, str, dict[str, Any]]] = []

    def _push(slug: str, data_key: str, payload: dict[str, Any], **_kw) -> dict[str, Any]:
        pushed.append((slug, data_key, payload))
        return {"ok": True}

    cn_good = {"as_of": "2026-11-16 14:30:00", "stale": False, "two_market_yi": 15000}
    hk_good = {"as_of": "2026-11-16 14:30:00", "stale": False, "smallcap": {"level": 1100.0}}

    cn_fn = create_autospec(lambda: cn_good, return_value=cn_good)
    hk_fn = create_autospec(lambda _dt: hk_good, return_value=hk_good)
    push_mock = create_autospec(feeds_common.push_monitor_data, side_effect=_push)

    # 1. Trading session tick -> pushes both cn & hk
    t_trade = datetime(2026, 11, 16, 14, 30, 0, tzinfo=feeds_common.CN_TZ)
    daemon = FeedDaemon(
        state_dir=tmp_path / "feeds",
        cn_collect_fn=cn_fn,
        hk_collect_fn=hk_fn,
        push_fn=push_mock,
        now_fn=lambda: t_trade,
    )
    res1 = daemon.run_tick()
    assert [r["action"] for r in res1] == ["pushed", "pushed"]
    assert len(pushed) == 2

    # 2. Collector failure during trading -> retains last_good_payload with stale=True
    cn_fn.side_effect = RuntimeError("upstream 502")
    res2 = daemon.run_tick(now=datetime(2026, 11, 16, 14, 31, 0, tzinfo=feeds_common.CN_TZ))
    assert res2[0]["stale"] is True
    cn_stale_payload = [p for s, _, p in pushed[-2:] if s == "cn-intraday"][-1]
    assert cn_stale_payload["stale"] is True
    assert cn_stale_payload["two_market_yi"] == 15000
    assert cn_stale_payload["last_success_at"] == "2026-11-16 14:30:00"

    # 3. Post-close: writes final value ONCE, then skips subsequent closed ticks & sleeps until next session
    cn_fn.side_effect = None
    pushed.clear()
    t_closed = datetime(2026, 11, 16, 16, 30, 0, tzinfo=feeds_common.CN_TZ)
    res3 = daemon.run_tick(now=t_closed)
    assert [r["action"] for r in res3] == ["post_close", "post_close"]
    assert len(pushed) == 2

    res4 = daemon.run_tick(now=datetime(2026, 11, 16, 16, 35, 0, tzinfo=feeds_common.CN_TZ))
    assert [r["action"] for r in res4] == ["skip", "skip"]
    assert len(pushed) == 2  # No extra push!

    # At 16:35 stays at 60s grid awaiting T86 daily close sync; past 17:30 (or once synced) enters idle sleep
    assert daemon.compute_sleep_seconds(now=datetime(2026, 11, 16, 16, 35, 0, tzinfo=feeds_common.CN_TZ)) == 60.0
    sleep_sec = daemon.compute_sleep_seconds(now=datetime(2026, 11, 16, 17, 35, 0, tzinfo=feeds_common.CN_TZ))
    assert sleep_sec == daemon.max_idle_sleep_seconds

    # 4. Bounded run_loop with stop_event and hard thread timeout
    with caplog.at_level(logging.INFO):
        stop = threading.Event()
        loop_thr = threading.Thread(
            target=daemon.run_loop,
            kwargs={"stop_event": stop, "max_iterations": 1},
            daemon=True,
        )
        loop_thr.start()
        loop_thr.join(timeout=5.0)
        assert not loop_thr.is_alive(), "FeedDaemon.run_loop did not exit within 5s timeout"
        assert secret_tok not in caplog.text


@pytest.mark.asyncio
async def test_write_monitor_data_internal_allows_registered_feed_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timezone
    from fastapi import HTTPException
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
    from src.dalton.monitors import db as monitor_db, service as monitor_service
    from src.dalton.monitors.models import MonitorDataEntry, MonitorRecord

    now = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)
    cn_rec = MonitorRecord(
        monitor_id="70000000-0000-4000-8000-000000000001",
        slug="cn-intraday",
        artifact_id="71000000-0000-4000-8000-000000000001",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        title="A股盘中监测",
        category="cn",
        sort_order=32,
        visibility="company",
        published_seq=1,
        keep_updated=True,
        source_kind="data_job",
        source_ref="cn-intraday",
        acl_revision=1,
        created_at=now,
        updated_at=now,
    )
    hk_rec = MonitorRecord(
        monitor_id="70000000-0000-4000-8000-000000000002",
        slug="hk-momentum",
        artifact_id="71000000-0000-4000-8000-000000000002",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        title="港股动量指数",
        category="hk",
        sort_order=45,
        visibility="company",
        published_seq=1,
        keep_updated=True,
        source_kind="data_job",
        source_ref="hk-momentum",
        acl_revision=1,
        created_at=now,
        updated_at=now,
    )
    by_slug = {"cn-intraday": cn_rec, "hk-momentum": hk_rec}

    async def _get_by_slug(slug: str):
        return by_slug.get(slug)

    async def _upsert(
        monitor_id: str,
        data_key: str,
        payload: Any,
        *,
        as_of=None,
        attempt_seq=None,
        is_stale=False,
        error=None,
        last_attempt_at=None,
    ):
        del attempt_seq, is_stale, error, last_attempt_at
        return MonitorDataEntry(
            monitor_id=monitor_id,
            data_key=data_key,
            payload=payload,
            as_of=as_of or now,
            sha256="b" * 64,
            updated_at=now,
        )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, side_effect=_get_by_slug),
    )
    monkeypatch.setattr(
        monitor_db,
        "upsert_monitor_data",
        create_autospec(monitor_db.upsert_monitor_data, side_effect=_upsert),
    )

    res_cn = await monitor_service.write_monitor_data_internal("cn-intraday", "snapshot", {"two_market_yi": 12000})
    assert res_cn["slug"] == "cn-intraday" and res_cn["key"] == "snapshot"

    res_hk = await monitor_service.write_monitor_data_internal("hk-momentum", "intraday", {"smallcap": {"level": 1000}})
    assert res_hk["slug"] == "hk-momentum" and res_hk["key"] == "intraday"

    with pytest.raises(HTTPException) as exc:
        await monitor_service.write_monitor_data_internal("cn-intraday", "unregistered", {})
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 6. Round-2 Hardening Regression Tests (Fixes 1–10)
# ---------------------------------------------------------------------------


def test_frontend_poller_has_fetch_timeout_and_3min_stale_guard() -> None:
    poller_cn = feeds_common.build_grant_poller_js("snapshot", market="cn")
    assert "AbortController" in poller_cn
    assert "STALE_AFTER_MS = 180000" in poller_cn
    assert "resp.status === 304" in poller_cn
    assert "checkDataFreshness()" in poller_cn

    cn_html = ashare_render.render_ashare_html({"as_of": "2026-11-16 14:00:00", "trading": True})
    assert "window.onMonitorStale = onMonitorStale;" in cn_html
    assert "if (clientStaleReason)" in cn_html
    assert "数据未更新（上次成功：" in cn_html

    hk_html = hkmom_render.render_hkmom_html(intraday={"as_of": "2026-11-16 14:00:00", "trading": True})
    assert "window.onMonitorStale = onMonitorStale;" in hk_html
    assert "if (clientStaleReason)" in hk_html
    assert "数据未更新（上次成功：" in hk_html


def test_ashare_subsource_failures_cached_with_original_time_and_no_sw3_fallback(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 11, 16, 10, 30, 0, tzinfo=feeds_common.CN_TZ)
    now_holder = {"dt": t0}

    # First run succeeds cleanly to seed SQLite, caches & last_success_at
    g_ok, j_ok = _fixture_http_handlers()
    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare_sub",
        http_get=g_ok,
        http_get_json=j_ok,
        now_fn=lambda: now_holder["dt"],
    )
    snap_ok = collector.collect_snapshot()
    assert snap_ok["stale"] is False

    # Case A: official share fetch + vratio + stock page 2 partial failure + history failure -> NEVER swallowed, even on cache hit
    call_flags = {"fail_stock_p2": True, "fail_vratio": True, "fail_sse": True, "fail_sohu": True}

    def _custom_get(url: str, **kwargs):
        if "query.sse.com.cn" in url and call_flags["fail_sse"]:
            raise RuntimeError("SSE 503")
        if "q.stock.sohu.com" in url and call_flags["fail_sohu"]:
            raise RuntimeError("Sohu timeout")
        return g_ok(url, **kwargs)

    def _custom_json(url: str, **kwargs):
        if "qt/stock/get" in url and call_flags["fail_vratio"]:
            raise RuntimeError("Vratio 502")
        if "m%3A0" in url or "m:0" in url:
            if "pn=2" in url and call_flags["fail_stock_p2"]:
                raise RuntimeError("Stock page 2 timeout")
            return {
                "data": {
                    "total": 150,  # 2 pages -> triggers page 2!
                    "diff": [
                        {"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8},
                        {"f12": "600036", "f14": "招商银行", "f3": 1.4, "f6": 2.8e9, "f62": 1.5e8},
                    ],
                }
            }
        return j_ok(url, **kwargs)

    collector._get = create_autospec(ashare_collect.default_http_get, side_effect=_custom_get)
    collector._get_json = create_autospec(ashare_collect.default_http_get_json, side_effect=_custom_json)
    # Expire caches so this tick attempts refresh
    collector._stock_scan_cache["ts"] = 0.0
    collector._history_cache["ts"] = 0.0
    collector._official_cache["ts"] = 0.0
    collector._vratio_cache.clear()

    now_holder["dt"] = datetime(2026, 11, 16, 10, 31, 0, tzinfo=feeds_common.CN_TZ)
    snap_fail = collector.collect_snapshot()
    assert snap_fail["stale"] is True
    for expected_src in ("industry", "daily_history", "etf_vratio", "etf_official_shares"):
        assert expected_src in snap_fail["stale_sources"]
    assert snap_fail["last_success_at"] == "2026-11-16 10:30:00"
    # Partial 1-page industry data was NOT cached over complete 3-stock cache
    assert collector._stock_scan_cache["error"] is not None
    assert len(collector._stock_scan_cache["rows"]) == 3

    # Case B: Missing sw3_mapping.json -> NO Eastmoney industry fallback; leaves area empty & marks stale
    missing_map_collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare_nomap",
        mapping_path=tmp_path / "nonexistent_sw3.json",
        http_get=g_ok,
        http_get_json=j_ok,
        now_fn=lambda: t0,
    )
    snap_nomap = missing_map_collector.collect_snapshot()
    assert snap_nomap["stale"] is True
    assert "industry" in snap_nomap["stale_sources"]
    assert snap_nomap["industry"]["rows"] == []
    nomap_html = ashare_render.render_ashare_html(snap_nomap)
    assert "行业映射不可用时本区暂停显示" in nomap_html


def test_hkmom_run_daily_job_propagation_and_month_end_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    st_dir = tmp_path / "hk_daily"
    st_dir.mkdir(parents=True)
    out_dir = tmp_path / "hk_out"
    prior_ok = "2026-10-29 16:35:00 北京时间"
    frozen_seed = {"launch_date": "2026-10-01", "rebalance_snapshots": {"2026-09-30": {"smallcap": ["00001"]}}, "frozen_series": {"dates": ["2026-10-29"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": [1000.0]}}
    (st_dir / "frozen_history.json").write_text(json.dumps(frozen_seed), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(frozen_seed["frozen_series"]), encoding="utf-8")
    (st_dir / "constituents.json").write_text(json.dumps({"smallcap": [{"code": "00001", "name": "HK1"}], "nonggt": [{"code": "00001", "name": "HK1"}]}), encoding="utf-8")
    (st_dir / "index_meta.json").write_text(
        json.dumps({"base_date": "2026-10-01", "last_date": "2026-10-29", "launch_date": "2026-10-01", "last_success_at": prior_ok, "params": {}}),
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

    degraded_uni = pd.DataFrame([{"code": "00001", "name": "HK1", "price": 10.0, "mktcap_e8": 20.0, "is_ggt": False}])
    degraded_meta = {"stale": True, "errors": ["spot batch 502"]}
    prog_err = {"done": 0, "total": 1, "errors": ["00001"], "n_errors": 1, "stale": True}

    uni_mock = create_autospec(hkmom_fetch.build_universe, return_value=(degraded_uni, degraded_meta))
    dl_mock = create_autospec(hkmom_fetch.download_history, return_value=prog_err)
    monkeypatch.setattr(hkmom_run, "build_universe", uni_mock)
    monkeypatch.setattr(hkmom_run, "download_history", dl_mock)

    # 1. Container daily job runs WITHOUT INTERNAL_SERVICE_TOKEN on month-end (2026-10-30):
    # keeps frozen files untouched, records "本月调仓未完成：..." in status.json / index_meta.json / live.json / HTML,
    # and host FeedDaemon picks up status.json change and pushes month-end failure even after provisional close!
    res_me_fetch = hkmom_run.run_daily_job(
        state_dir=st_dir,
        output_dir=out_dir,
        as_of_date=date(2026, 10, 30),
    )
    assert res_me_fetch["ok"] is False
    assert res_me_fetch["month_end_failed"] is True
    assert "本月调仓未完成：" in (res_me_fetch["stale_reason"] or "")
    assert res_me_fetch["last_success_at"] == prior_ok
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_seed
    assert (out_dir / "index.html").is_file()
    assert "本月调仓未完成：" in (out_dir / "index.html").read_text(encoding="utf-8")
    meta_me = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert meta_me["month_end_failed"] is True and meta_me["last_success_at"] == prior_ok

    # Host daemon (has token, already posted 16:15 close earlier) sees daily status.json change and pushes failure!
    pushed_host: list[tuple[str, str, dict[str, Any]]] = []
    push_host_mock = create_autospec(
        feeds_common.push_monitor_data,
        side_effect=lambda slug, key, payload, **_kw: pushed_host.append((slug, key, payload)) or {"ok": True},
    )
    daemon = FeedDaemon(
        state_dir=tmp_path / "daemon_state",
        hk_daily_dir=st_dir,
        feeds=("hk",),
        hk_collect_fn=create_autospec(
            lambda _dt: {},
            return_value={"as_of": "2026-10-30 16:40:00", "stale": False, "smallcap": {"level": 1001.0, "source": "intraday"}},
        ),
        push_fn=push_host_mock,
    )
    daemon.state["hk"]["post_close_written_date"] = "2026-10-30"
    r_host = daemon.run_tick(now=datetime(2026, 10, 30, 16, 40, 0, tzinfo=feeds_common.HK_TZ))
    assert r_host[0]["action"] == "post_close_daily_sync"
    assert pushed_host and pushed_host[-1][2]["month_end_failed"] is True
    assert "本月调仓未完成：" in (pushed_host[-1][2].get("stale_reason") or "")

    # Subsequent intraday snapshot MUST keep surfacing "本月调仓未完成：" until next clean daily run!
    fake_sess_ok = create_autospec(requests.Session, instance=True)
    snap_after_me = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 10, 30, 16, 40, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess_ok,
    )
    assert snap_after_me["stale"] is True
    assert "本月调仓未完成：" in (snap_after_me["stale_reason"] or "")

    # 1b. Month-end fetch succeeds, but HSI history fails inside compute_all -> MUST fail ok=False, NOT overwrite frozen!
    uni_mock.return_value = (degraded_uni, {"stale": False, "errors": []})
    dl_mock.return_value = {"done": 1, "total": 1, "errors": [], "n_errors": 0, "stale": False}
    real_compute_all = hkmom_compute.compute_all
    compute_mock = create_autospec(
        real_compute_all,
        return_value={
            "series": {"dates": ["2026-10-30"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": None},
            "constituents": {"smallcap": [], "nonggt": []},
            "meta": {
                "base_date": "2026-10-30",
                "last_date": "2026-10-30",
                "launch_date": "2026-10-01",
                "series_stale": True,
                "completeness_ok": False,
                "series_stale_reason": "恒生指数历史行情拉取失败",
                "params": {},
            },
        },
    )
    monkeypatch.setattr(hkmom_run, "compute_all", compute_mock)
    res_me_hsi = hkmom_run.run_daily_job(
        state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30)
    )
    assert res_me_hsi["ok"] is False
    assert "本月调仓未完成：" in (res_me_hsi["stale_reason"] or "")
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_seed

    # Verify CLI main() returns exit code 1 when month-end check fails (run_daily_job ok=False)
    monkeypatch.setattr(hkmom_run, "run_daily_job", create_autospec(hkmom_run.run_daily_job, return_value=res_me_hsi))
    monkeypatch.setattr("sys.argv", ["run.py", "--state-dir", str(st_dir), "--output-dir", str(out_dir)])
    assert hkmom_run.main() == 1
    monkeypatch.undo()

    # 2. Item 5 + Codex #2: If downstream HTML render raises on clean run, error is captured in status.json & last_success_at preserves prior_ok!
    (st_dir / "index_meta.json").write_text(
        json.dumps({"base_date": "2026-10-01", "last_date": "2026-11-16", "launch_date": "2026-10-01", "last_success_at": prior_ok, "params": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(hkmom_run, "build_universe", create_autospec(hkmom_fetch.build_universe, return_value=(degraded_uni, {"stale": False, "errors": []})))
    monkeypatch.setattr(hkmom_run, "download_history", create_autospec(hkmom_fetch.download_history, return_value={"done": 1, "total": 1, "errors": [], "n_errors": 0, "stale": False}))
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        create_autospec(
            real_compute_all,
            return_value={
                "series": {"dates": ["2026-11-16"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": [1000.0]},
                "constituents": {"smallcap": [], "nonggt": []},
                "meta": {
                    "base_date": "2026-11-16",
                    "last_date": "2026-11-16",
                    "launch_date": "2026-10-01",
                    "completeness_ok": True,
                    "params": {},
                },
            },
        ),
    )
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        create_autospec(hkmom_compute.compute_intraday_snapshot, return_value={"as_of": "2026-11-16 16:35:00", "stale": False}),
    )
    render_fail_mock = create_autospec(hkmom_render.write_hkmom_panel, side_effect=RuntimeError("disk full"))
    monkeypatch.setattr(hkmom_run, "write_hkmom_panel", render_fail_mock)
    res_render_err = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 11, 16))
    assert res_render_err["ok"] is False
    assert res_render_err["last_success_at"] == prior_ok
    saved_after_err = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert saved_after_err["last_success_at"] == prior_ok
    assert saved_after_err["sources"]["html_render"]["ok"] is False

    # 3. Non-rebalance day with degraded fetch marks overall ok=False & preserves PRIOR last_success_at!
    monkeypatch.undo()
    monkeypatch.setattr(hkmom_run, "build_universe", create_autospec(hkmom_fetch.build_universe, return_value=(degraded_uni, degraded_meta)))
    monkeypatch.setattr(hkmom_run, "download_history", create_autospec(hkmom_fetch.download_history, return_value=prog_err))
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        create_autospec(
            real_compute_all,
            return_value={
                "series": {"dates": ["2026-11-16"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": [1000.0]},
                "constituents": {"smallcap": [], "nonggt": []},
                "meta": {
                    "base_date": "2026-11-16",
                    "last_date": "2026-11-16",
                    "launch_date": "2026-10-01",
                    "completeness_ok": True,
                    "params": {},
                },
            },
        ),
    )
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        create_autospec(hkmom_compute.compute_intraday_snapshot, return_value={"as_of": "2026-11-16 16:35:00", "stale": False}),
    )
    res = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 11, 16))
    assert res["ok"] is False
    assert res["month_end_failed"] is False
    assert res["stale"] is True
    assert res["last_success_at"] == prior_ok
    saved_meta = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert saved_meta["last_success_at"] == prior_ok


def test_hkmom_intraday_pre_open_lagging_daily_and_hsi_date_strict(tmp_path: Path) -> None:
    st_dir = tmp_path / "hk_checks"
    st_dir.mkdir(parents=True)
    (st_dir / "constituents.json").write_text(
        json.dumps({"smallcap": [{"code": "00001", "name": "长江和记"}], "nonggt": [{"code": "00001", "name": "长江和记"}]}),
        encoding="utf-8",
    )
    spot_txt = (FIXTURES_DIR / "tencent_hk_spot_sample.txt").read_text(encoding="utf-8")

    class _FakeResp:
        def __init__(self, text: str) -> None:
            self.text = text
            self.encoding = "gbk"

    # HSI quote WITHOUT date timestamp -> must be treated same as stocks (stale!)
    def _get_dateless_hsi(url: str, **_kw):
        if "r_hkHSI" in url:
            return _FakeResp('v_r_hkHSI="50~恒生指数~HSI~20500.0~20300.0~' + "~" * 26 + '200~0.99~";')
        return _FakeResp(spot_txt)

    fake_sess = create_autospec(requests.Session, instance=True)
    fake_sess.get.side_effect = _get_dateless_hsi

    # Case 1: Pre-open (< 09:30 HK time) -> ALWAYS stale, no HTTP intraday calculation
    (st_dir / "index_series.json").write_text(
        json.dumps({"dates": ["2026-11-13"], "smallcap": [1000.0], "nonggt": [1000.0]}),
        encoding="utf-8",
    )
    snap_pre = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 16, 9, 15, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess,
    )
    assert snap_pre["stale"] is True
    assert "盘前" in (snap_pre["stale_reason"] or "")

    # Case 2: Lagging daily close (today=2026-11-17 Tue, expected prev=2026-11-16, but series only up to 2026-11-13)
    snap_lag = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 17, 10, 30, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess,
    )
    assert snap_lag["stale"] is True
    assert "日频收盘数据滞后" in (snap_lag["stale_reason"] or "")
    assert snap_lag["smallcap"]["pct"] is None
    assert snap_lag["smallcap"]["source"] == "stale_close"

    # Case 3: Current daily close (2026-11-13 for Mon 2026-11-16), but HSI missing date -> stale!
    snap_hsi = hkmom_compute.compute_intraday_snapshot(
        state_dir=st_dir,
        now=datetime(2026, 11, 16, 15, 30, 0, tzinfo=feeds_common.HK_TZ),
        session=fake_sess,
    )
    assert snap_hsi["stale"] is True
    assert "恒指报价缺少有效日期" in (snap_hsi["stale_reason"] or "")


def test_calendar_half_day_default_fallback_warning_and_lookup_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. XHKG 2026-12-24 is a half-day (closes 12:00, no lunch break)
    sched_half = feeds_common.get_session_schedule("hk", date(2026, 12, 24))
    assert sched_half["is_trading_day"] is True
    assert sched_half["has_break"] is False
    assert sched_half["am_end"] == 12 * 60
    assert sched_half["using_default_schedule"] is False
    sess_after_half = feeds_common.market_session_state(
        "hk", datetime(2026, 12, 24, 14, 0, 0, tzinfo=feeds_common.HK_TZ)
    )
    assert sess_after_half["status"] == "closed"
    assert sess_after_half["in_trading_window"] is False

    # 2. If open/close lookup fails on a valid trading day, warn and label "交易时段为默认值"
    orig_get_cal = feeds_common._get_exchange_calendar
    real_cal = orig_get_cal("XSHG")
    bad_hours_cal = SimpleNamespace(
        is_session=create_autospec(real_cal.is_session, side_effect=real_cal.is_session),
        session_open=create_autospec(real_cal.session_open, side_effect=RuntimeError("open unavailable")),
    )
    cal_mock = create_autospec(orig_get_cal, return_value=bad_hours_cal)
    monkeypatch.setattr(feeds_common, "_get_exchange_calendar", cal_mock)
    with caplog.at_level(logging.WARNING):
        sess_default = feeds_common.market_session_state(
            "cn", datetime(2026, 11, 16, 10, 0, 0, tzinfo=feeds_common.CN_TZ)
        )
    assert sess_default["using_default_schedule"] is True
    assert sess_default["session_note"] == "交易时段为默认值"
    assert "使用默认交易时段" in caplog.text

    # 3. If is_session itself fails, DO NOT silently return weekday<5 — raise & report calendar_error!
    broken_cal = SimpleNamespace(
        is_session=create_autospec(real_cal.is_session, side_effect=RuntimeError("cal corrupt")),
    )
    cal_mock.return_value = broken_cal
    with pytest.raises(feeds_common.CalendarLookupError):
        feeds_common.is_trading_day("cn", date(2026, 11, 16))
    sess_err = feeds_common.market_session_state(
        "cn", datetime(2026, 11, 16, 10, 0, 0, tzinfo=feeds_common.CN_TZ)
    )
    assert sess_err["status"] == "calendar_error"
    assert sess_err["calendar_error"] is not None


def test_feed_daemon_post_close_retry_hk_daily_sync_parallel_and_fixed_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "env-only-token")
    assert not hasattr(feeds_common, "load_env_file_if_set")
    plist_text = (
        Path(__file__).resolve().parents[3] / "scripts" / "dalton" / "monitor_feeds" / "com.dalton.monitor-feeds.plist"
    ).read_text(encoding="utf-8")
    assert "__INTERNAL_SERVICE_TOKEN__" in plist_text
    assert "DALTON_MONITOR_FEEDS_ENV_FILE" not in plist_text

    pushed: list[tuple[str, str, dict[str, Any]]] = []
    hk_daily = tmp_path / "hk_daily_repo"
    hk_daily.mkdir()

    def _push(slug: str, data_key: str, payload: dict[str, Any], **_kw):
        pushed.append((slug, data_key, payload))
        return {"ok": True}

    push_mock = create_autospec(feeds_common.push_monitor_data, side_effect=_push)

    # 1. Parallel non-blocking run_tick: CN waits until HK starts inside barrier (with 3s timeout!)
    hk_entered = threading.Event()
    cn_proceed = threading.Event()

    def _slow_cn():
        assert hk_entered.wait(timeout=3.0), "HK feed was blocked waiting for CN feed"
        cn_proceed.set()
        return {"as_of": "2026-11-16 14:30:00", "stale": False, "two_market_yi": 13000}

    def _fast_hk(_cur):
        hk_entered.set()
        assert cn_proceed.wait(timeout=3.0), "CN did not finish after signaling"
        return {"as_of": "2026-11-16 14:30:00", "stale": False, "smallcap": {"level": 1010.0}}

    daemon = FeedDaemon(
        state_dir=tmp_path / "daemon_state",
        hk_daily_dir=hk_daily,
        interval_seconds=60.0,
        cn_collect_fn=_slow_cn,
        hk_collect_fn=_fast_hk,
        push_fn=push_mock,
    )
    res_par = daemon.run_tick(now=datetime(2026, 11, 16, 14, 30, 12, tzinfo=feeds_common.CN_TZ))
    assert [r["action"] for r in res_par] == ["pushed", "pushed"]

    # 2. Fixed-minute grid subtracts elapsed execution time (at 14:30:18 -> wait 42s until 14:31:00)
    wait_sec = daemon.compute_sleep_seconds(now=datetime(2026, 11, 16, 14, 30, 18, tzinfo=feeds_common.CN_TZ))
    assert math.isclose(wait_sec, 42.0, abs_tol=0.2)

    # 3. Stale close MUST NOT mark post_close_written_date; non-stale retry marks it; HK syncs again after daily T86 finishes
    def _hk_stub(cur: datetime) -> dict[str, Any]:
        return hkmom_compute.compute_intraday_snapshot(now=cur)

    hk_collect_mock = create_autospec(
        _hk_stub,
        return_value={"as_of": "2026-11-16 16:10:00", "stale": True, "smallcap": {"level": 1005.0, "source": "intraday"}},
    )
    hk_daemon = FeedDaemon(
        state_dir=tmp_path / "hk_close_state",
        hk_daily_dir=hk_daily,
        feeds=("hk",),
        hk_collect_fn=hk_collect_mock,
        push_fn=push_mock,
    )
    t_close = datetime(2026, 11, 16, 16, 10, 0, tzinfo=feeds_common.HK_TZ)
    r_stale = hk_daemon.run_tick(now=t_close)
    assert r_stale[0]["stale"] is True
    assert hk_daemon.state["hk"].get("post_close_written_date") is None

    # Now HK collect succeeds cleanly at 16:11 (before T86 16:35 rebuild) -> sets post_close_written_date, not daily_synced
    hk_collect_mock.return_value = {
        "as_of": "2026-11-16 16:11:00",
        "stale": False,
        "smallcap": {"level": 1012.0, "source": "intraday"},
    }
    r_ok_close = hk_daemon.run_tick(now=t_close + timedelta(minutes=1))
    assert r_ok_close[0]["stale"] is False
    assert r_ok_close[0]["action"] == "post_close"
    assert hk_daemon.state["hk"].get("post_close_written_date") == "2026-11-16"
    assert hk_daemon.state["hk"].get("post_close_daily_synced_date") is None

    # Before T86 finishes -> skips
    r_skip = hk_daemon.run_tick(now=datetime(2026, 11, 16, 16, 20, 0, tzinfo=feeds_common.HK_TZ))
    assert r_skip[0]["action"] == "skip"

    # At 16:36 T86 writes index_series.json containing 2026-11-16.
    # Item 4 guard: if the actual pushed payload still only had 1 intraday index, MUST NOT mark daily_synced!
    (hk_daily / "index_series.json").write_text(
        json.dumps({"dates": ["2026-11-13", "2026-11-16"], "smallcap": [1000.0, 1015.5]}),
        encoding="utf-8",
    )
    hk_collect_mock.return_value = {
        "as_of": "2026-11-16 16:36:00",
        "trade_date": "2026-11-16",
        "stale": False,
        "smallcap": {"level": 1015.5, "source": "daily_close"},
        "nonggt": {"level": 1020.0, "source": "intraday"},  # race: nonggt not daily_close yet!
    }
    r_race = hk_daemon.run_tick(now=datetime(2026, 11, 16, 16, 36, 0, tzinfo=feeds_common.HK_TZ))
    assert r_race[0]["action"] == "post_close_daily_sync"
    assert hk_daemon.state["hk"].get("post_close_daily_synced_date") is None

    # Next tick pushes BOTH smallcap & nonggt with daily_close -> NOW marks post_close_daily_synced_date!
    hk_collect_mock.return_value = {
        "as_of": "2026-11-16 16:37:00",
        "trade_date": "2026-11-16",
        "daily_close_date": "2026-11-16",
        "stale": False,
        "smallcap": {"level": 1015.5, "source": "daily_close"},
        "nonggt": {"level": 1020.0, "source": "daily_close"},
    }
    r_sync = hk_daemon.run_tick(now=datetime(2026, 11, 16, 16, 37, 0, tzinfo=feeds_common.HK_TZ))
    assert r_sync[0]["action"] == "post_close_daily_sync"
    assert hk_daemon.state["hk"].get("post_close_daily_synced_date") == "2026-11-16"

    # Subsequent tick skips cleanly
    r_done = hk_daemon.run_tick(now=datetime(2026, 11, 16, 16, 40, 0, tzinfo=feeds_common.HK_TZ))
    assert r_done[0]["action"] == "skip"


def test_hkmom_download_history_rejects_empty_and_incomplete_without_overwriting(
    tmp_path: Path,
) -> None:
    st_dir = tmp_path / "hk_hist_guard"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True)
    csv_path = hist_dir / "00001.csv"
    original_csv = (
        "date,open,close,high,low,volume,turnover_wan\n"
        "2026-11-12,10.0,10.1,10.2,9.9,1000,100.0\n"
        "2026-11-13,10.1,10.2,10.3,10.0,1000,100.0\n"
    )
    csv_path.write_text(original_csv, encoding="utf-8")

    # Empty list returned by upstream -> keeps original CSV untouched & records error!
    class _EmptyResp:
        def json(self):
            return {"data": {"hk00001": {"qfqday": []}}}

    sess_empty = create_autospec(requests.Session, instance=True)
    sess_empty.get.return_value = _EmptyResp()
    prog_empty = hkmom_fetch.download_history(["00001"], state_dir=st_dir, session=sess_empty)
    assert prog_empty["n_errors"] == 1
    assert prog_empty["errors"] == ["00001"]
    assert csv_path.read_text(encoding="utf-8") == original_csv

    # Truncated response (only 1 bar when prior CSV already had 2 bars) or corrupt bar -> keeps original CSV!
    class _ShortResp:
        def json(self):
            return {"data": {"hk00001": {"qfqday": [["2026-11-16", "10.2", "10.3", "10.4", "10.1", "1000", "100"]]}}}

    sess_short = create_autospec(requests.Session, instance=True)
    sess_short.get.return_value = _ShortResp()
    prog_short = hkmom_fetch.download_history(["00001"], state_dir=st_dir, session=sess_short)
    assert prog_short["n_errors"] == 1
    assert csv_path.read_text(encoding="utf-8") == original_csv

    # Same-length responses that contain duplicate dates, drop an existing date, or have invalid date -> MUST keep original!
    for bad_rows in [
        # Duplicate date 2026-11-13
        [
            ["2026-11-13", "10.0", "10.1", "10.2", "9.9", "1000", "100"],
            ["2026-11-13", "10.1", "10.2", "10.3", "10.0", "1000", "100"],
        ],
        # Same length (2 rows) but dropped 2026-11-12!
        [
            ["2026-11-13", "10.1", "10.2", "10.3", "10.0", "1000", "100"],
            ["2026-11-16", "10.2", "10.3", "10.4", "10.1", "1000", "100"],
        ],
        # Invalid/unparseable date
        [
            ["2026-99-99", "10.0", "10.1", "10.2", "9.9", "1000", "100"],
            ["2026-11-13", "10.1", "10.2", "10.3", "10.0", "1000", "100"],
        ],
    ]:
        class _BadDatesResp:
            def __init__(self, payload_rows):
                self._rows = payload_rows

            def json(self):
                return {"data": {"hk00001": {"qfqday": self._rows}}}

        sess_bad = create_autospec(requests.Session, instance=True)
        sess_bad.get.return_value = _BadDatesResp(bad_rows)
        prog_bad = hkmom_fetch.download_history(["00001"], state_dir=st_dir, session=sess_bad)
        assert prog_bad["n_errors"] == 1
        assert csv_path.read_text(encoding="utf-8") == original_csv


def test_hkmom_compute_completeness_checks_block_freeze(tmp_path: Path) -> None:
    st_dir = tmp_path / "hk_complete"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True)

    import exchange_calendars as xcals

    xhkg = xcals.get_calendar("XHKG")
    # Ends 2026-10-30 (month-end Friday)
    sessions = [d.strftime("%Y-%m-%d") for d in xhkg.sessions_in_range("2025-07-01", "2026-10-30")]
    assert sessions[-1] == "2026-10-30"
    codes = [f"{i:05d}" for i in range(1, 56)]
    uni_rows = []
    for idx, c in enumerate(codes):
        uni_rows.append(
            {
                "code": c,
                "name": f"HK{c}",
                "is_ggt": idx >= 45,
                "price": 10.0,
                "mktcap_e8": 20.0,
                "name_cn": f"港股{c}",
            }
        )
        rows = [
            [d_s, 10.0 + 0.01 * i, 10.0 * (1 + 0.0005 * i) * (1 + 0.02 * math.sin(i * 0.6 + idx)), 12.0, 9.0, 100000, 200.0]
            for i, d_s in enumerate(sessions)
        ]
        pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "turnover_wan"]).to_csv(
            hist_dir / f"{c}.csv", index=False
        )
    pd.DataFrame(uni_rows).to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(json.dumps({c: f"港股{c}" for c in codes[45:]}), encoding="utf-8")

    # Case 1: Raw HSI stops at 2026-10-29 (missing target 2026-10-30 even though ffill would fill it) -> NO freeze!
    class _HsiLagResp:
        def json(self):
            return {"data": {"hkHSI": {"day": [[d, 20000, 20000 + i] for i, d in enumerate(sessions[:-1])]}}}

    sess_lag_hsi = create_autospec(requests.Session, instance=True)
    sess_lag_hsi.get.return_value = _HsiLagResp()
    res_lag_hsi = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 10, 30), hsi_session=sess_lag_hsi
    )
    assert res_lag_hsi["meta"]["completeness_ok"] is False
    assert res_lag_hsi["meta"]["froze"] is False
    assert "恒生指数原始记录缺少目标日期 2026-10-30" in (res_lag_hsi["meta"]["stale_reason"] or "")
    assert not (st_dir / "frozen_history.json").exists()

    # Case 2: Raw HSI has 2026-10-30, but target date is 2026-11-02 (panel still stuck at 2026-10-30) -> NO freeze!
    class _HsiUpToNov2:
        def json(self):
            ext = sessions + ["2026-11-02"]
            return {"data": {"hkHSI": {"day": [[d, 20000, 20000 + i] for i, d in enumerate(ext)]}}}

    sess_nov2 = create_autospec(requests.Session, instance=True)
    sess_nov2.get.return_value = _HsiUpToNov2()
    res_lag_px = hkmom_compute.compute_all(
        state_dir=st_dir, as_of_date=date(2026, 11, 2), hsi_session=sess_nov2
    )
    assert res_lag_px["meta"]["completeness_ok"] is False
    assert res_lag_px["meta"]["froze"] is False
    assert "未达目标日期 2026-11-02" in (res_lag_px["meta"]["stale_reason"] or "")
    assert not (st_dir / "frozen_history.json").exists()


def test_ashare_pagination_rejects_null_empty_diff_and_count_mismatch(tmp_path: Path) -> None:
    t0 = datetime(2026, 11, 16, 10, 30, 0, tzinfo=feeds_common.CN_TZ)
    g_ok, j_ok = _fixture_http_handlers()
    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "ashare_page",
        http_get=g_ok,
        http_get_json=j_ok,
        now_fn=lambda: t0,
    )
    rows_init, err_init = collector.fetch_stock_market()
    assert err_init is None and len(rows_init) == 3

    for bad_case, bad_p2 in [
        ("data_null", {"data": None}),
        ("empty_diff", {"data": {"diff": []}}),
        ("count_mismatch", {"data": {"diff": [{"f12": "000002", "f14": "万科A", "f3": 0.5, "f6": 5e8, "f62": 1e7}]}}),
    ]:
        def _bad_json(url: str, *, _p2=bad_p2, **kw):
            if "m%3A0" in url or "m:0" in url:
                if "pn=2" in url:
                    return _p2
                return {
                    "data": {
                        "total": 103,  # forces 2 pages; 1 item on p1 + 1 on p2 != 103
                        "diff": [{"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8}],
                    }
                }
            return j_ok(url, **kw)

        collector._get_json = create_autospec(ashare_collect.default_http_get_json, side_effect=_bad_json)
        collector._stock_scan_cache["ts"] = 0.0
        rows_res, err_res = collector.fetch_stock_market()
        assert err_res is not None, f"Failed to detect {bad_case}"
        assert len(rows_res) == 3  # original 3-row complete cache preserved!
        assert collector._stock_scan_cache["error"] is not None

    # Item 4: total missing / invalid / zero with non-empty diff MUST fail on BOTH stock & ETF!
    for bad_total in (None, 0, -5, "NaN", True):
        def _bad_total_stock(url: str, *, _tot=bad_total, **kw):
            if "m%3A0" in url or "m:0" in url:
                return {"data": {"total": _tot, "diff": [{"f12": "000001", "f14": "平安银行", "f3": 1.1, "f6": 1.5e9, "f62": 0.8e8}]}}
            if "MK0021" in url:
                return {"data": {"total": _tot, "diff": [{"f12": "512480", "f13": 1, "f14": "半导体ETF", "f2": 1.05, "f3": 2.4, "f6": 2.5e9, "f20": 3.0e10, "f62": 1.8e8}]}}
            return j_ok(url, **kw)

        collector._get_json = create_autospec(ashare_collect.default_http_get_json, side_effect=_bad_total_stock)
        collector._stock_scan_cache["ts"] = 0.0
        collector._etf_scan_cache["ts"] = 0.0
        rows_s, err_s = collector.fetch_stock_market()
        assert err_s is not None and len(rows_s) == 3
        _, _, err_e = collector.fetch_etf_market()
        assert err_e is not None


def test_feed_daemon_independent_cadence_and_calendar_error_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[tuple[str, str, dict[str, Any]]] = []

    def _push(slug: str, data_key: str, payload: dict[str, Any], **_kw):
        pushed.append((slug, data_key, payload))
        return {"ok": True}

    push_mock = create_autospec(feeds_common.push_monitor_data, side_effect=_push)

    # 1. Item 5: CN blocks inside tick 1 while HK completes 2 independent iterations of run_loop (with 5s timeout!)
    hk_two_done = threading.Event()
    hk_count = {"n": 0}
    cn_calls = {"n": 0}

    def _blocking_cn():
        cn_calls["n"] += 1
        assert hk_two_done.wait(timeout=5.0), "HK loop was delayed by slow CN feed!"
        return {"as_of": "2026-11-16 10:00:00", "stale": False, "two_market_yi": 11000}

    def _fast_hk(_cur):
        hk_count["n"] += 1
        if hk_count["n"] >= 2:
            hk_two_done.set()
        return {"as_of": "2026-11-16 10:00:00", "stale": False, "smallcap": {"level": 1005.0, "source": "intraday"}}

    def _hk_spec(cur: datetime) -> dict[str, Any]:
        return hkmom_compute.compute_intraday_snapshot(now=cur)

    daemon = FeedDaemon(
        state_dir=tmp_path / "indep_daemon",
        interval_seconds=0.05,  # small alignment unit for fast offline loop test (<1s, capped min 0.05s)
        cn_collect_fn=create_autospec(lambda: {}, side_effect=_blocking_cn),
        hk_collect_fn=create_autospec(_hk_spec, side_effect=_fast_hk),
        push_fn=push_mock,
        now_fn=lambda: datetime(2026, 11, 16, 10, 0, 0, tzinfo=feeds_common.CN_TZ),
    )
    stop = threading.Event()
    t = threading.Thread(
        target=daemon.run_loop,
        kwargs={"stop_event": stop, "max_iterations": 2},
        daemon=True,
    )
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "Independent per-market run_loop timed out"
    assert hk_count["n"] == 2
    assert cn_calls["n"] == 2

    # 2. Item 6: Calendar failure in normal run_tick pushes stale "交易日历不可用" before skipping collection
    pushed.clear()
    orig_get_cal = feeds_common._get_exchange_calendar
    broken_cal = SimpleNamespace(is_session=create_autospec(orig_get_cal("XSHG").is_session, side_effect=RuntimeError("XSHG down")))
    monkeypatch.setattr(feeds_common, "_get_exchange_calendar", create_autospec(orig_get_cal, return_value=broken_cal))

    cn_cal_mock = create_autospec(lambda: {}, return_value={})
    daemon_cal = FeedDaemon(
        state_dir=tmp_path / "cal_err_daemon",
        feeds=("cn",),
        cn_collect_fn=cn_cal_mock,
        push_fn=push_mock,
    )
    res_cal = daemon_cal.run_tick(now=datetime(2026, 11, 16, 10, 0, 0, tzinfo=feeds_common.CN_TZ))
    assert res_cal[0]["action"] == "calendar_error"
    assert cn_cal_mock.call_count == 0
    assert pushed[-1][2]["stale"] is True
    assert pushed[-1][2]["market_status_label"] == "交易日历不可用"
    assert "交易日历不可用" in pushed[-1][2]["stale_reason"]


# ---------------------------------------------------------------------------
# 5. Round 5: Unified SourceStatus Matrix & Codex #1-#7 Coverage
# ---------------------------------------------------------------------------


def test_unified_source_status_store_and_pm_partial_valuation_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. SourceStatusStore contract: ONLY clean (ok=True, stale=False) advances last_success_at
    store = feeds_common.SourceStatusStore(tmp_path / "unit_status")
    t1 = "2026-11-16 10:00:00 北京时间"
    t2 = "2026-11-16 10:05:00 北京时间"
    doc1 = store.update_sources(
        [
            feeds_common.make_source_result("s_a", ok=True, stale=False, label="A源"),
            feeds_common.make_source_result("s_b", ok=True, stale=False, label="B源"),
        ],
        required=["s_a", "s_b"],
        now_str=t1,
    )
    assert doc1["overall"]["ok"] is True
    assert doc1["overall"]["last_success_at"] == t1

    doc2 = store.update_sources(
        [
            feeds_common.make_source_result("s_a", ok=True, stale=False, label="A源"),
            feeds_common.make_source_result("s_b", ok=False, stale=True, reason="B超时", label="B源"),
        ],
        required=["s_a", "s_b"],
        now_str=t2,
    )
    assert doc2["overall"]["ok"] is False
    assert doc2["overall"]["last_success_at"] == t1
    assert doc2["sources"]["s_a"]["last_success_at"] == t2
    assert doc2["sources"]["s_b"]["last_success_at"] == t1
    banner = feeds_common.format_source_statuses(doc2["sources"])
    assert f"B源：数据未更新（上次成功：{t1}，原因：B超时）" in banner

    # 2. PM Cockpit Codex #6 & #7: exception / partial / empty MUST preserve status.json last_success_at,
    # and cn_valuation tracks per (date, level) without skipping failed (date, level) pairs!
    monkeypatch.setattr(pm_collect.time, "sleep", lambda _s: None)
    cfg = pm_collect.load_config()
    fake_yf, fake_ak = _build_pm_fixtures(cfg)
    # Restrict valuation weeks to 2 dates so full + partial runs stay <0.2s
    short_weeks = pd.DataFrame({"date": ["2025-12-19", "2025-12-26"]})
    fake_ak.index_analysis_week_month_sw.side_effect = None
    fake_ak.index_analysis_week_month_sw.return_value = short_weeks
    pm_state = tmp_path / "pm_matrix"

    meta_ok = pm_collect.collect_all(
        which="all", state_dir=pm_state, yf_module=fake_yf, ak_module=fake_ak
    )
    assert meta_ok["overall"]["ok"] is True
    assert pm_state.joinpath("status.json").is_file()
    # Pin distinct prior_ok timestamps in status.json so same-second test runs prove non-advancement on failure
    prior_us = "2026-11-15 16:00:00 北京时间"
    prior_cn = "2026-11-15 16:05:00 北京时间"
    prior_val = "2026-11-15 16:10:00 北京时间"
    sdoc_seed = json.loads((pm_state / "status.json").read_text(encoding="utf-8"))
    sdoc_seed["sources"]["us"]["last_success_at"] = prior_us
    sdoc_seed["sources"]["cn"]["last_success_at"] = prior_cn
    sdoc_seed["sources"]["cn_valuation"]["last_success_at"] = prior_val
    sdoc_seed["overall"]["last_success_at"] = prior_us
    (pm_state / "status.json").write_text(json.dumps(sdoc_seed, ensure_ascii=False), encoding="utf-8")

    # (a) US partial failure: one ticker has NaN / exception -> ok=False, stale=True, preserves prior last_success_at
    orig_yf = fake_yf.download

    def _partial_yf(tickers, *args, **kwargs):
        df = orig_yf(tickers, *args, **kwargs)
        bad = df.copy()
        for col in bad.columns:
            if col[1] == "XLK":
                bad[col] = float("nan")
        return bad

    bad_yf = SimpleNamespace(download=create_autospec(yf.download, side_effect=_partial_yf))
    meta_us_partial = pm_collect.collect_all(
        which="us", state_dir=pm_state, yf_module=bad_yf, ak_module=fake_ak
    )
    assert meta_us_partial["us"]["ok"] is False
    assert meta_us_partial["sources"]["us"]["last_success_at"] == prior_us

    # (b) US total exception (caught cleanly at collect_all level!) -> preserves prior_us
    exc_yf = SimpleNamespace(download=create_autospec(yf.download, side_effect=RuntimeError("yf down")))
    meta_us_exc = pm_collect.collect_all(
        which="us", state_dir=pm_state, yf_module=exc_yf, ak_module=fake_ak
    )
    assert meta_us_exc["us"]["ok"] is False
    assert meta_us_exc["sources"]["us"]["last_success_at"] == prior_us

    # (c) CN partial failure: one SW index raises exception -> preserves prior_cn
    _, fake_ak_cn_part = _build_pm_fixtures(cfg)
    orig_hist_sw = fake_ak_cn_part.index_hist_sw.side_effect
    fake_ak_cn_part.index_hist_sw.side_effect = lambda symbol, period="day": (
        (_ for _ in ()).throw(RuntimeError("sw 801081 timeout"))
        if symbol == "801081"
        else orig_hist_sw(symbol, period=period)
    )
    meta_cn_part = pm_collect.collect_all(
        which="cn", state_dir=pm_state, yf_module=fake_yf, ak_module=fake_ak_cn_part
    )
    assert meta_cn_part["cn"]["ok"] is False
    assert meta_cn_part["sources"]["cn"]["last_success_at"] == prior_cn

    # (d) CN valuation per (date, level) tracking (Codex #7): add a new week "2026-01-02" where
    # "二级行业" succeeds, while "一级行业" raises transient error -> MUST NOT skip "一级行业", MUST mark stale & preserve prior_val!
    sdoc_val_seed = json.loads((pm_state / "status.json").read_text(encoding="utf-8"))
    sdoc_val_seed["sources"]["cn_valuation"]["last_success_at"] = prior_val
    (pm_state / "status.json").write_text(json.dumps(sdoc_val_seed, ensure_ascii=False), encoding="utf-8")

    three_weeks = pd.DataFrame({"date": ["2025-12-19", "2025-12-26", "2026-01-02"]})
    _, fake_ak_val = _build_pm_fixtures(cfg)
    orig_weekly = fake_ak_val.index_analysis_weekly_sw.side_effect
    fake_ak_val.index_analysis_week_month_sw.side_effect = None
    fake_ak_val.index_analysis_week_month_sw.return_value = three_weeks

    def _fail_l1_2026(symbol: str, date: str):
        if date == "20260102" and symbol == "一级行业":
            raise RuntimeError("akshare weekly 502")
        return orig_weekly(symbol, date)

    fake_ak_val.index_analysis_weekly_sw.side_effect = _fail_l1_2026
    meta_val_fail = pm_collect.collect_all(
        which="cnval", state_dir=pm_state, yf_module=fake_yf, ak_module=fake_ak_val
    )
    assert meta_val_fail["cn_valuation"]["ok"] is False
    assert meta_val_fail["cn_valuation"]["stale"] is True
    assert meta_val_fail["sources"]["cn_valuation"]["last_success_at"] == prior_val
    prog = json.loads((pm_state / "raw" / "cn_sw_val_progress.json").read_text(encoding="utf-8"))
    assert "二级行业" in prog["done"]["2026-01-02"]
    assert "一级行业" not in prog["done"].get("2026-01-02", [])

    # Recovery: fix "一级行业" for 2026-01-02 -> ONLY retries missing (2026-01-02, 一级行业) and succeeds cleanly!
    called_pairs: list[tuple[str, str]] = []

    def _recover_l1(symbol: str, date: str):
        called_pairs.append((date, symbol))
        return orig_weekly(symbol, date)

    fake_ak_val.index_analysis_weekly_sw.side_effect = _recover_l1
    meta_val_rec = pm_collect.collect_all(
        which="cnval", state_dir=pm_state, yf_module=fake_yf, ak_module=fake_ak_val
    )
    assert called_pairs == [("20260102", "一级行业")]
    assert meta_val_rec["cn_valuation"]["ok"] is True
    assert meta_val_rec["cn_valuation"]["stale"] is False
    assert meta_val_rec["sources"]["cn_valuation"]["last_success_at"] != prior_val


def test_ashare_official_shares_empty_and_all_sources_status_matrix(
    tmp_path: Path,
) -> None:
    base_txt, base_json = _fixture_http_handlers()
    tick = [datetime(2026, 11, 16, 14, 30, 0, tzinfo=feeds_common.CN_TZ)]
    collector = ashare_collect.AShareCollector(
        state_dir=tmp_path / "cn_matrix",
        http_get=create_autospec(ashare_collect.default_http_get, side_effect=base_txt),
        http_get_json=create_autospec(ashare_collect.default_http_get_json, side_effect=base_json),
        now_fn=lambda: tick[0],
    )
    snap_ok = collector.collect_snapshot()
    assert snap_ok["stale"] is False
    prior_ok = snap_ok["last_success_at"]
    assert prior_ok == "2026-11-16 14:30:00"
    prior_official_cache = dict(collector._official_cache["data"])
    assert "512480" in prior_official_cache

    # Helper matching all empty/exception modes per A-share subsource
    def _run_degraded(case_name: str, *, txt_override=None, json_override=None):
        tick[0] = tick[0] + timedelta(minutes=1)
        collector._get = create_autospec(
            ashare_collect.default_http_get, side_effect=txt_override or base_txt
        )
        collector._get_json = create_autospec(
            ashare_collect.default_http_get_json, side_effect=json_override or base_json
        )
        # Expire in-memory caches so every collector actually hits the mock upstream
        collector._official_cache["ts"] = 0.0
        collector._stock_scan_cache["ts"] = 0.0
        collector._etf_scan_cache["ts"] = 0.0
        collector._history_cache["ts"] = 0.0
        snap = collector.collect_snapshot()
        assert snap["stale"] is True, f"{case_name} did not mark snapshot stale"
        assert snap["overall"]["ok"] is False
        assert snap["last_success_at"] == prior_ok, f"{case_name} wrongly advanced last_success_at"
        return snap

    # Codex #5: Official ETF share empty SSE list -> MUST mark etf_official_shares failed & preserve prior cache/timestamp!
    def _empty_sse_shares(url: str, **kw):
        if "query.sse.com.cn" in url:
            return 'jsonpCallback({"result":[]})'
        return base_txt(url, **kw)

    snap_sse_empty = _run_degraded("sse_official_empty", txt_override=_empty_sse_shares)
    assert "etf_official_shares" in snap_sse_empty["stale_sources"]
    assert collector._official_cache["data"] == prior_official_cache
    assert snap_sse_empty["sources"]["etf_official_shares"]["last_success_at"] == prior_ok

    # Empty indices (Sina blank response): indices keeps its own last clean timestamp (14:31:00) while overall stays 14:30:00
    prev_idx_ok = snap_sse_empty["sources"]["indices"]["last_success_at"]
    snap_idx_empty = _run_degraded(
        "indices_empty",
        txt_override=lambda url, **kw: "" if "hq.sinajs.cn" in url else base_txt(url, **kw),
    )
    assert "indices" in snap_idx_empty["stale_sources"]
    assert snap_idx_empty["sources"]["indices"]["last_success_at"] == prev_idx_ok

    prev_ind_ok = snap_idx_empty["sources"]["industry"]["last_success_at"]
    snap_ind_empty = _run_degraded(
        "industry_empty",
        json_override=lambda url, **kw: (
            {"data": {"total": 0, "diff": []}}
            if "cl=2" in url or "fs=m%3A0" in url or "fs=m:0" in url
            else base_json(url, **kw)
        ),
    )
    assert "industry" in snap_ind_empty["stale_sources"]
    assert snap_ind_empty["sources"]["industry"]["last_success_at"] == prev_ind_ok

    html_out = ashare_render.write_ashare_panel(output_dir=tmp_path / "cn_out", snapshot=snap_sse_empty)
    html_text = html_out.read_text(encoding="utf-8")
    assert "上交所/深交所官方份额" in html_text or "etf_official_shares" in html_text
    assert prior_ok in html_text

    # Clean recovery advances last_success_at cleanly!
    tick[0] = tick[0] + timedelta(minutes=1)
    collector._get = create_autospec(ashare_collect.default_http_get, side_effect=base_txt)
    collector._get_json = create_autospec(ashare_collect.default_http_get_json, side_effect=base_json)
    collector._official_cache["ts"] = 0.0
    collector._stock_scan_cache["ts"] = 0.0
    collector._etf_scan_cache["ts"] = 0.0
    collector._history_cache["ts"] = 0.0
    snap_recovered = collector.collect_snapshot()
    assert snap_recovered["stale"] is False
    assert snap_recovered["overall"]["ok"] is True
    assert snap_recovered["last_success_at"] == "2026-11-16 14:34:00"


def test_hkmom_ggt_empty_page_intraday_stale_and_unhandled_exception_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st_dir = tmp_path / "hk_matrix"
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True)
    out_dir = tmp_path / "hk_out"
    prior_ok = "2026-10-29 16:35:00 北京时间"
    frozen_seed = {
        "launch_date": "2026-10-01",
        "rebalance_snapshots": {"2026-09-30": {"smallcap": ["00001"]}},
        "frozen_series": {"dates": ["2026-10-29"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": [1000.0]},
    }
    (st_dir / "frozen_history.json").write_text(json.dumps(frozen_seed), encoding="utf-8")
    (st_dir / "index_series.json").write_text(json.dumps(frozen_seed["frozen_series"]), encoding="utf-8")
    (st_dir / "index_meta.json").write_text(
        json.dumps({"base_date": "2026-10-01", "last_date": "2026-10-29", "launch_date": "2026-10-01", "last_success_at": prior_ok, "params": {}}),
        encoding="utf-8",
    )
    seed_uni = pd.DataFrame([{"code": "00001", "name": "HK1", "price": 10.0, "mktcap_e8": 20.0, "is_ggt": True}])
    seed_uni.to_csv(st_dir / "universe.csv", index=False)
    (st_dir / "ggt_list.json").write_text(json.dumps({"00001": "长和"}), encoding="utf-8")
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

    # 1. Codex #3: fetch_ggt_list MUST reject empty mid-page (pagecount=2, page 2 data=[]) & invalid codes!
    def _szse_page2_empty(url, params=None, **_kw):
        if "PAGENO=2" in url:
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: [{"metadata": {"pagecount": 2, "recordcount": 2}, "data": []}],
            )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [{"metadata": {"pagecount": 2, "recordcount": 2}, "data": [{"zqdm": "00001", "zqjc": "长和"}]}],
        )

    bad_ggt_sess = create_autospec(requests.Session, instance=True)
    bad_ggt_sess.get.side_effect = _szse_page2_empty
    with pytest.raises(RuntimeError, match="港股通名单第 2 页返回空列表"):
        hkmom_fetch.fetch_ggt_list(session=bad_ggt_sess)

    # Verify build_universe preserves prior ggt_list.json & universe.csv when ggt fails!
    monkeypatch.setattr(hkmom_fetch, "fetch_hkex_universe", create_autospec(hkmom_fetch.fetch_hkex_universe, return_value=seed_uni))
    monkeypatch.setattr(hkmom_fetch, "fetch_ggt_list", create_autospec(hkmom_fetch.fetch_ggt_list, side_effect=RuntimeError("GGT page 2 empty")))
    monkeypatch.setattr(
        hkmom_fetch,
        "fetch_spot_batch",
        create_autospec(hkmom_fetch.fetch_spot_batch, return_value=({"00001": {"price": 10.0, "mktcap_e8": 20.0}}, [])),
    )
    uni_res, uni_meta = hkmom_fetch.build_universe(state_dir=st_dir)
    assert uni_meta["stale"] is True
    assert uni_meta["source_statuses"]["ggt_list"]["ok"] is False
    assert json.loads((st_dir / "ggt_list.json").read_text(encoding="utf-8")) == {"00001": "长和"}
    assert len(uni_res) == 1
    monkeypatch.undo()

    # 2. Codex #2: Unhandled exceptions in compute_all / build_universe NEVER escape unhandled;
    # frozen history & prior_ok preserved, month-end flagged when applicable, status.json updated!
    monkeypatch.setattr(hkmom_run, "build_universe", create_autospec(hkmom_fetch.build_universe, side_effect=RuntimeError("HKEX TLS crash")))
    res_exc_me = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 10, 30))
    assert res_exc_me["ok"] is False
    assert res_exc_me["month_end_failed"] is True
    assert res_exc_me["last_success_at"] == prior_ok
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_seed
    assert (st_dir / "status.json").is_file()
    monkeypatch.undo()

    # 3. Codex #4: Clean fetch + compute, while compute_intraday_snapshot returns stale=True (HSI spot failed)
    # -> MUST NOT advance overall last_success_at or index_meta.last_success_at!
    monkeypatch.setattr(hkmom_run, "build_universe", create_autospec(hkmom_fetch.build_universe, return_value=(seed_uni, {"stale": False, "errors": []})))
    monkeypatch.setattr(hkmom_run, "download_history", create_autospec(hkmom_fetch.download_history, return_value={"done": 1, "total": 1, "errors": [], "n_errors": 0, "stale": False}))
    monkeypatch.setattr(
        hkmom_run,
        "compute_all",
        create_autospec(
            hkmom_compute.compute_all,
            return_value={
                "series": {"dates": ["2026-11-16"], "smallcap": [1000.0], "nonggt": [1000.0], "hsi": [1000.0]},
                "constituents": {"smallcap": [], "nonggt": []},
                "meta": {"base_date": "2026-10-01", "last_date": "2026-11-16", "launch_date": "2026-10-01", "completeness_ok": True, "params": {}},
            },
        ),
    )
    monkeypatch.setattr(
        hkmom_run,
        "compute_intraday_snapshot",
        create_autospec(
            hkmom_compute.compute_intraday_snapshot,
            return_value={
                "as_of": "2026-11-16 16:35:00",
                "stale": True,
                "stale_reason": "恒生指数现货获取失败",
                "intraday_source_statuses": {
                    "hk_intraday_spot": feeds_common.make_source_result("hk_intraday_spot", ok=True, stale=False, label="港股盘中现货").to_dict(),
                    "hsi_spot": feeds_common.make_source_result("hsi_spot", ok=False, stale=True, reason="HSI timeout", label="恒生指数现货").to_dict(),
                },
            },
        ),
    )
    res_intra_stale = hkmom_run.run_daily_job(state_dir=st_dir, output_dir=out_dir, as_of_date=date(2026, 11, 16))
    assert res_intra_stale["ok"] is False
    assert res_intra_stale["stale"] is True
    assert res_intra_stale["last_success_at"] == prior_ok
    assert json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))["last_success_at"] == prior_ok
    monkeypatch.undo()

    # 4. GLM #2: Corrupted frozen_history.json MUST raise explicit RuntimeError (never silently replaced with {})!
    corrupt_dir = tmp_path / "hk_corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "frozen_history.json").write_text("{corrupt-json", encoding="utf-8")
    (corrupt_dir / "universe.csv").write_text("code,name,is_ggt,mktcap_e8,price\n00001,HK1,False,20.0,10.0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="frozen_history.json 文件损坏"):
        hkmom_compute.compute_all(state_dir=corrupt_dir)


def test_hk_history_accepts_a_rolled_window_and_rewrites_from_one_response(tmp_path, monkeypatch):
    """Tencent returns the latest N bars: when two new sessions arrive, the two
    oldest stored dates roll off. That is not data loss; the update must succeed
    and the file must hold exactly the new response (one adjustment basis)."""
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    dates = [f"2026-01-{d:02d}" for d in range(1, 31)]
    stored, fresh = dates[:28], dates[2:]
    hist = tmp_path / "hist"
    hist.mkdir(parents=True)
    header = "date,open,close,high,low,volume,turnover_wan\n"
    (hist / "00700.csv").write_text(
        header + "".join(f"{d},1,1,1,1,1,0\n" for d in stored), encoding="utf-8"
    )

    def fake_fetch(sess, code, **kw):
        return [[d, 2.0, 2.0, 2.0, 2.0, 1.0, 0.0] for d in fresh]

    monkeypatch.setattr(hkf, "fetch_hist_one", create_autospec(hkf.fetch_hist_one, side_effect=fake_fetch))
    monkeypatch.setattr(hkf, "HIST_BARS", len(fresh))  # a full window may roll dates off
    res = hkf.download_history(["00700"], state_dir=tmp_path, target_date="2026-01-30", session=object())
    assert res["n_errors"] == 0
    lines = (hist / "00700.csv").read_text(encoding="utf-8").splitlines()
    assert [ln.split(",")[0] for ln in lines[1:]] == fresh


def test_hk_history_still_rejects_a_gap_inside_the_window(tmp_path, monkeypatch):
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    dates = [f"2026-01-{d:02d}" for d in range(1, 31)]
    hist = tmp_path / "hist"
    hist.mkdir(parents=True)
    (hist / "00700.csv").write_text(
        "date,open,close,high,low,volume,turnover_wan\n" + "".join(f"{d},1,1,1,1,1,0\n" for d in dates[:28]),
        encoding="utf-8",
    )
    gapped = [d for d in dates if d != "2026-01-15"]

    monkeypatch.setattr(
        hkf,
        "fetch_hist_one",
        create_autospec(hkf.fetch_hist_one, side_effect=lambda s, c, **kw: [[d, 2.0, 2.0, 2.0, 2.0, 1.0, 0.0] for d in gapped]),
    )
    monkeypatch.setattr(hkf.time, "sleep", lambda *_: None)
    res = hkf.download_history(["00700"], state_dir=tmp_path, target_date="2026-01-30", session=object())
    assert res["n_errors"] == 1


def test_hk_history_never_falls_back_to_unadjusted_bars():
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    class _Resp:
        def json(self):
            return {"data": {"hk00700": {"day": [["2026-11-19", "1", "1", "1", "1", "1"]]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    with pytest.raises(RuntimeError):
        hkf.fetch_hist_one(sess, "00700")


def test_hk_history_keeps_the_stored_file_when_only_unadjusted_bars_exist(tmp_path):
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    hist = tmp_path / "hist"
    hist.mkdir(parents=True)
    original = "date,open,close,high,low,volume,turnover_wan\n2025-11-27,1,1,1,1,1,0\n2025-11-28,1,1,1,1,1,0\n"
    (hist / "09986.csv").write_text(original, encoding="utf-8")

    class _Resp:
        def json(self):
            return {"data": {"hk09986": {"day": [["2025-11-28", "1", "1", "1", "1", "1"]]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    res = hkf.download_history(["09986"], state_dir=tmp_path, target_date="2026-11-19", session=sess)
    assert res["n_errors"] == 0
    assert (hist / "09986.csv").read_text(encoding="utf-8") == original


def test_hk_history_ignores_bad_bars_before_hist_start():
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    class _Resp:
        def json(self):
            return {"data": {"hk00700": {"qfqday": [
                ["2022-01-04", "", None, "", "", "1"],  # before HIST_START, not numeric
                ["2022-01-05", "-1", "-1", "-1", "-1", "1"],  # before HIST_START, negative
                ["2026-11-18", "2", "2", "2", "2", "1"],
                ["2026-11-19", "2", "2", "2", "2", "1"],
            ]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    rows = hkf.fetch_hist_one(sess, "00700")
    assert [r[0] for r in rows] == ["2026-11-18", "2026-11-19"]
    assert rows.raw_count == 4
    assert rows.first_date == "2022-01-04"


def test_hk_history_full_window_with_bad_pre_start_bars_still_owes_kept_dates(tmp_path, monkeypatch):
    """The window start comes from the whole response, so dropping bad bars
    before HIST_START cannot hide a stored date missing at the window's edge."""
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    hist = tmp_path / "hist"
    hist.mkdir(parents=True)
    (hist / "00700.csv").write_text(
        "date,open,close,high,low,volume,turnover_wan\n2023-06-01,1,1,1,1,1,0\n2023-06-02,1,1,1,1,1,0\n",
        encoding="utf-8",
    )

    class _Resp:
        def json(self):
            return {"data": {"hk00700": {"qfqday": [
                ["2023-05-30", "-1", "-1", "-1", "-1", "1"],
                ["2023-05-31", "-1", "-1", "-1", "-1", "1"],
                ["2023-06-02", "2", "2", "2", "2", "1"],
                ["2023-06-05", "2", "2", "2", "2", "1"],
            ]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    monkeypatch.setattr(hkf, "HIST_BARS", 4)  # the response is a full window
    monkeypatch.setattr(hkf.time, "sleep", lambda *_: None)
    res = hkf.download_history(["00700"], state_dir=tmp_path, target_date="2023-06-05", session=sess)
    assert res["n_errors"] == 1
    assert "2023-06-01" in (hist / "00700.csv").read_text(encoding="utf-8")


def test_hk_history_listing_entirely_before_hist_start_is_not_an_error(tmp_path):
    """A code whose valid bars all predate HIST_START has nothing in range: the
    download succeeds with an empty panel file, as before the window change."""
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    class _Resp:
        def json(self):
            return {"data": {"hk08888": {"qfqday": [
                ["2023-05-30", "2", "2", "2", "2", "1"],
                ["2023-05-31", "2", "2", "2", "2", "1"],
            ]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    res = hkf.download_history(["08888"], state_dir=tmp_path, target_date="2026-11-19", session=sess)
    assert res["n_errors"] == 0
    lines = (tmp_path / "hist" / "08888.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["date,open,close,high,low,volume,turnover_wan"]


def test_hk_history_rejects_a_response_with_only_bad_bars_before_hist_start(tmp_path, monkeypatch):
    from scripts.dalton.monitor_feeds.hk_momentum import fetch_data as hkf

    class _Resp:
        def json(self):
            return {"data": {"hk08888": {"qfqday": [
                ["2023-05-30", "", "", "", "", "1"],
                ["2023-05-31", "-1", "-1", "-1", "-1", "1"],
                ["2023-05-29", "inf", "inf", "inf", "inf", "1"],
            ]}}}

    sess = create_autospec(requests.Session, instance=True)
    sess.get.return_value = _Resp()
    monkeypatch.setattr(hkf.time, "sleep", lambda *_: None)
    res = hkf.download_history(["08888"], state_dir=tmp_path, target_date="2026-11-19", session=sess)
    assert res["n_errors"] == 1
