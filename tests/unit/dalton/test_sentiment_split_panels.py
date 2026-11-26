"""Offline unit tests for the split sentiment dashboard panels and UI/data-staleness fixes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import importlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
from typing import Any
from unittest.mock import create_autospec

import numpy as np
import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "data_assets"
    / "skills"
    / "sentiment-dashboard"
    / "scripts"
)
CLI_PATH = SCRIPTS / "sentiment_dashboard_cli.py"

_STUB_MODULES = (
    "openclaw_workspace",
    "us_bottom_framework",
    "us_bottom_environment_selftest",
    "us_top_framework",
    "us_top_framework_selftest",
)


class _VisibleTextExtractor(HTMLParser):
    """Extract only visible text nodes, skipping <style> and <script> blocks."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"style", "script"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"style", "script"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.chunks.append(data)

    def text(self) -> str:
        return " ".join(self.chunks)


def _visible_text(html_doc: str) -> str:
    parser = _VisibleTextExtractor()
    parser.feed(html_doc)
    parser.close()
    return parser.text()


def _find_upstream_scripts() -> Path:
    candidates = [
        REPO_ROOT / "tests/fixtures/dalton/sentiment_dashboard_upstream",
        REPO_ROOT.parent.parent / "dalton-agy/design/dashboards/sentiment-dashboard/scripts",
        Path("/Users/everflow/Projects/dalton-agy/design/dashboards/sentiment-dashboard/scripts"),
    ]
    for candidate in candidates:
        if (
            (candidate / "openclaw_workspace.py").is_file()
            and (candidate / "us_bottom_framework.py").is_file()
            and (candidate / "us_top_framework.py").is_file()
        ):
            return candidate
    raise FileNotFoundError(
        "Upstream sentiment-dashboard fixture scripts directory not found in tests/fixtures/dalton or dalton-agy/design"
    )


def _install_stubs(workspace_path: Path) -> None:
    """Load the real upstream framework modules and wrap their callables via create_autospec."""
    upstream_dir = _find_upstream_scripts()
    saved_upstream = {name: sys.modules.pop(name, None) for name in _STUB_MODULES}
    sys.path.insert(0, str(upstream_dir))
    try:
        real_ws = importlib.import_module("openclaw_workspace")
        real_bottom = importlib.import_module("us_bottom_framework")
        real_btest = importlib.import_module("us_bottom_environment_selftest")
        real_top = importlib.import_module("us_top_framework")
        real_ttest = importlib.import_module("us_top_framework_selftest")
    finally:
        sys.path.remove(str(upstream_dir))
        for name in _STUB_MODULES:
            old = saved_upstream.get(name)
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    workspace = types.ModuleType("openclaw_workspace")
    workspace.workspace_root = create_autospec(
        real_ws.workspace_root, return_value=workspace_path
    )
    sys.modules["openclaw_workspace"] = workspace

    bottom_mod = types.ModuleType("us_bottom_framework")
    bottom_mod.BottomFrameworkConfig = real_bottom.BottomFrameworkConfig
    bottom_mod.alert_projection_digest_lines = create_autospec(
        real_bottom.alert_projection_digest_lines,
        wraps=real_bottom.alert_projection_digest_lines,
    )
    bottom_mod.alert_projection_lead = create_autospec(
        real_bottom.alert_projection_lead,
        wraps=real_bottom.alert_projection_lead,
    )
    bottom_mod.bottom_environment_digest_line = create_autospec(
        real_bottom.bottom_environment_digest_line,
        wraps=real_bottom.bottom_environment_digest_line,
    )
    bottom_mod.build_bottom_environment = create_autospec(
        real_bottom.build_bottom_environment,
        wraps=real_bottom.build_bottom_environment,
    )
    bottom_mod.evaluate_bottom_framework = create_autospec(
        real_bottom.evaluate_bottom_framework,
        wraps=real_bottom.evaluate_bottom_framework,
    )
    bottom_mod.projection_digest_lines = create_autospec(
        real_bottom.projection_digest_lines,
        wraps=real_bottom.projection_digest_lines,
    )
    sys.modules["us_bottom_framework"] = bottom_mod

    b_test = types.ModuleType("us_bottom_environment_selftest")
    b_test.bottom_environment_checks = create_autospec(
        real_btest.bottom_environment_checks, return_value=[]
    )
    sys.modules["us_bottom_environment_selftest"] = b_test

    top_mod = types.ModuleType("us_top_framework")
    top_mod.TopFrameworkConfig = real_top.TopFrameworkConfig
    top_mod.evaluate_top_framework = create_autospec(
        real_top.evaluate_top_framework,
        wraps=real_top.evaluate_top_framework,
    )
    top_mod.top_digest_lines = create_autospec(
        real_top.top_digest_lines,
        wraps=real_top.top_digest_lines,
    )
    sys.modules["us_top_framework"] = top_mod

    t_test = types.ModuleType("us_top_framework_selftest")
    t_test.top_framework_checks = create_autospec(
        real_ttest.top_framework_checks, return_value=[]
    )
    sys.modules["us_top_framework_selftest"] = t_test


def _load_cli(workspace_path: Path):
    saved = {name: sys.modules.get(name) for name in _STUB_MODULES}
    sys.path.insert(0, str(SCRIPTS))
    for mod_name in ("dashboard_copy", "gs_prime_positioning", "jepq_signal", "us_market_sections"):
        sys.modules.pop(mod_name, None)
    try:
        _install_stubs(workspace_path)
        spec = importlib.util.spec_from_file_location("sentiment_dashboard_cli_split_test", CLI_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _populate_fake_workspace(ws: Path) -> tuple[Path, Path]:
    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    # 1. BBG CSV (intentionally stale to verify stale warning & banner)
    stale_bbg_dates = pd.bdate_range("2026-04-01", "2026-06-01")
    n_bbg = len(stale_bbg_dates)
    bbg_df = pd.DataFrame({
        "GSPUCYDE Index": stale_bbg_dates.strftime("%Y-%m-%d"),
        "Last Price": np.linspace(1.0, 1.15, n_bbg),
        "GSPUCYDE Index.1": stale_bbg_dates.strftime("%Y-%m-%d"),
        "RSI 14D": np.linspace(45.0, 52.0, n_bbg),
        "GSCBHMOM Index": stale_bbg_dates.strftime("%Y-%m-%d"),
        "RSI 30D": np.linspace(48.0, 62.0, n_bbg),
        "QQQ US Equity": stale_bbg_dates.strftime("%Y-%m-%d"),
        "1M vol": np.full(n_bbg, 18.0),
        "QQQ US Equity.1": stale_bbg_dates.strftime("%Y-%m-%d"),
        "3M vol": np.full(n_bbg, 19.5),
        "HSAHP Index": stale_bbg_dates.strftime("%Y-%m-%d"),
        "Last Price.1": np.linspace(135.0, 142.0, n_bbg),
        "HSAHP Index.1": stale_bbg_dates.strftime("%Y-%m-%d"),
        "RSI 30D.1": np.linspace(40.0, 52.0, n_bbg),
    })
    (ws / "temp").mkdir(parents=True, exist_ok=True)
    bbg_df.to_csv(ws / "temp/bbg_data_latest.csv", index=False)

    # 2. CN margin & CSI1000
    cn_dates = dates[-120:]
    (ws / "state/cn-leverage").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": cn_dates.strftime("%Y-%m-%d"),
        "margin_balance_cny100m": np.linspace(15000.0, 18200.0, len(cn_dates)),
    }).to_csv(ws / "state/cn-leverage/margin_core.csv", index=False)
    pd.DataFrame({
        "date": cn_dates.strftime("%Y-%m-%d"),
        "code": ["IM"] * len(cn_dates),
        "close": np.linspace(5400.0, 6100.0, len(cn_dates)),
    }).to_csv(ws / "state/cn-leverage/index_prices.csv", index=False)

    # 3. Crowding JSON
    (ws / "research-outputs/index-crowding").mkdir(parents=True, exist_ok=True)
    crowding_rows = [
        {
            "index_code": "399303.SZ",
            "index_name": "国证2000",
            "category": "宽基",
            "crowding_percentile": 83.4,
            "crowding_level": "偏热",
            "probability_pct": 58.2,
            "probability_direction": "上涨",
            "probability_horizon_days": 20,
            "daily_change_pct": 1.25,
            "detail_url": "https://example.test/399303",
            "history_1y": [
                ["2025-10-16", 100.0, 40.0],
                ["2026-04-16", 112.0, 62.0],
                ["2026-10-16", 118.5, 83.4],
            ],
        },
        {
            "index_code": "000300.SH",
            "index_name": "沪深300",
            "category": "宽基",
            "crowding_percentile": 45.0,
            "crowding_level": "中性",
            "probability_pct": 51.0,
            "probability_direction": "上涨",
            "probability_horizon_days": 20,
            "daily_change_pct": -0.42,
            "detail_url": "https://example.test/000300",
            "history_1y": [
                ["2025-10-16", 100.0, 42.0],
                ["2026-10-16", 104.2, 45.0],
            ],
        },
    ]
    (ws / "research-outputs/index-crowding/official-all.json").write_text(
        json.dumps({
            "trade_date": "2026-10-16",
            "index_count": len(crowding_rows),
            "rows": crowding_rows,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # 4. Asia leverage card & CSVs
    (ws / "temp/asia-leverage-crossview").mkdir(parents=True, exist_ok=True)
    (ws / "temp/asia-leverage-crossview/card_latest.md").write_text(
        "\n".join([
            "## 🇰🇷 韩国 — 拥挤度 78/100(高位) · 融资扩表",
            "- **核心指标**: 融资余额 21.4 兆₩",
            "",
            "## 🇯🇵 日本 — 拥挤度 46/100(中性) · 温和回升",
            "- **核心指标**: 信用买残 4.2 兆円",
            "",
            "## 🇹🇼 台湾 — 拥挤度 62/100(偏热) · 融资增温",
            "- **核心指标**: 融资余额 3,180 亿NT$",
            "",
            "## 🇨🇳 中国A股 — 拥挤度 54/100(中性) · 两融平稳",
            "- **核心指标**: 两融 1.82 万亿元",
        ]),
        encoding="utf-8",
    )
    comp_dates = pd.bdate_range("2026-08-01", "2026-10-16")
    pd.DataFrame({
        "date": comp_dates.strftime("%Y-%m-%d"),
        "kr_composite_score": np.linspace(65, 78, len(comp_dates)),
        "jp_composite_score": np.linspace(40, 46, len(comp_dates)),
        "tw_composite_score": np.linspace(55, 62, len(comp_dates)),
    }).to_csv(ws / "temp/asia-leverage-crossview/composite_history.csv", index=False)

    for rel_csv, cols in (
        (
            "research-outputs/jp-leverage/data/margin_weekly.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "long_value_mjpy": np.linspace(4.0e6, 4.2e6, len(comp_dates)), "short_value_mjpy": np.full(len(comp_dates), 8.0e5)},
        ),
        (
            "research-outputs/kr-leverage/data/kofia_credit.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "credit_loan_total": np.linspace(2.0e7, 2.14e7, len(comp_dates))},
        ),
        (
            "research-outputs/kr-leverage/data/kofia_kospi.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "mcap": np.full(len(comp_dates), 2.1e9)},
        ),
        (
            "research-outputs/kr-leverage/data/kofia_kosdaq.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "mcap": np.full(len(comp_dates), 4.0e8)},
        ),
        (
            "research-outputs/tw-leverage/data/margin_market.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "fin_value_kntd": np.linspace(3.0e8, 3.18e8, len(comp_dates))},
        ),
        (
            "research-outputs/tw-leverage/data/margin_issues_derived.csv",
            {"date": comp_dates.strftime("%Y-%m-%d"), "usage_p90": np.linspace(35.0, 42.0, len(comp_dates))},
        ),
    ):
        p = ws / rel_csv
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(cols).to_csv(p, index=False)

    # 5. GS Prime JSON (intentionally stale so weekly stale banner also renders cleanly)
    (ws / "state/sentiment-dashboard").mkdir(parents=True, exist_ok=True)
    old_gp_date = (date.today() - timedelta(days=20)).isoformat()
    prev_gp_date = (date.today() - timedelta(days=27)).isoformat()
    (ws / "state/sentiment-dashboard/gs_prime_positioning.json").write_text(
        json.dumps({
            "leverage_history": [
                {"date": prev_gp_date, "doc_id": "mail:1", "gross_leverage": 212.0, "net_leverage": 50.0, "gross_pctile_1y": 55, "net_pctile_5y": 28},
                {"date": old_gp_date, "doc_id": "mail:2", "gross_leverage": 216.1, "net_leverage": 43.4, "gross_pctile_1y": 64, "net_pctile_5y": 15},
            ],
            "mag7_history": [
                {"date": prev_gp_date, "net_exp": 16.5, "gross_exp": 13.8},
                {"date": old_gp_date, "net_exp": 18.2, "gross_exp": 15.1},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # 6. Config & seed
    config_path = ws / "dashboard_config.json"
    seed_path = ws / "spx_model_seed.json"
    config = {
        "market_tickers": {
            "SPX": "^GSPC",
            "NDX": "^NDX",
            "SPW": "^SPXEW",
            "QQQ": "QQQ",
            "SPY": "SPY",
            "SOX": "^SOX",
            "VIX": "^VIX",
            "VVIX": "^VVIX",
        },
        "mags": ["AMZN", "GOOGL", "META", "MSFT", "AAPL", "NVDA"],
        "quantum": ["IONQ", "RGTI", "QBTS", "QUBT"],
        "semiconductor_breadth": ["NVDA", "AVGO"],
        "relative_signal": {
            "rsi_period": 30,
            "top_rsi": 60,
            "bottom_rsi": 40,
            "top_ma200_dev_pct": 20,
            "bottom_ma200_dev_pct": -5,
            "position_lookback_days": 504,
        },
        "thresholds": {"us_top_rsi30": 68, "rsi_hot": 70, "rsi_cold": 30},
        "us_bottom_framework": {
            "gsp_alert_rsi": 35,
            "qqq_alert_drawdown": -0.10,
            "pressure_vix": 25,
            "pressure_index_rsi": 30,
            "deep_index_rsi": 20,
        },
        "us_top_framework": {
            "window_td": 20,
            "excess_timeout_td": 40,
            "gsp_warn_rsi30": 68,
            "quantum_warn_gain_pct": 100,
            "excess_qqq_rsi14": 75,
            "excess_qqq_200dma_pct": 20,
            "excess_momentum_rsi": 75,
            "confirm_drawdown_pct": 5,
            "confirm_ma_td": 20,
            "put_hedge_vvix_vix": 6.5,
            "put_hedge_cheap_vix": 15,
            "cool_drawdown_15_pct": -15,
            "weak_trend_premium_pct": 0,
            "quantum_low_td": 60,
        },
        "gs_prime": {
            "net_cold_pctile": 10,
            "net_hot_pctile": 90,
            "gross_cold_pctile": 10,
            "gross_hot_pctile": 90,
            "mag7_net_hot": 20.0,
            "mag7_net_cold": 10.0,
            "fresh_lag_days": 7,
        },
        "sp_sector_etfs": [
            {"ticker": "XLP", "name": "Staples", "color": "#4da3ff"},
            {"ticker": "XLK", "name": "科技", "color": "#ff9d3c"},
        ],
        "equal_weight_baskets": [
            {
                "key": "INTERNET",
                "label": "Internet等权",
                "members": ["NFLX", "BKNG"],
                "benchmark": "QQQ",
                "color": "#a377ff",
            },
            {
                "key": "STAPLES",
                "label": "Staples等权",
                "members": ["WMT", "COST"],
                "benchmark": "QQQ",
                "color": "#38d39f",
            },
        ],
        "relative_extras": [
            {"name": "TSM", "ticker": "TSM", "benchmark": "SOX", "ma": 100, "color": "#4da3ff"},
            {"name": "BRKB", "ticker": "BRK-B", "benchmark": "SPW", "ma": 200, "color": "#ffbf3c"},
        ],
        "single_stocks": [
            {"name": "宁德时代", "ticker": "300750.SZ", "benchmark": "000300.SS"},
            {"name": "长江电力", "ticker": "600900.SS", "benchmark": "000300.SS"},
        ],
        "trend_lines": [
            {
                "ticker": "AFRM",
                "label": "AFRM",
                "start": "2023-07-01",
                "mode": "support",
                "fit": {
                    "anchor": "2025-10-16",
                    "fitted_on": "2025-10-16",
                    "slope": 0.05,
                    "bottom": 40.0,
                    "bottom_pts": 8,
                },
            }
        ],
        "cn_a_indices": [
            {"name": "国证2000", "code": "399303.SZ", "color": "#ff9d3c", "featured": True},
            {"name": "沪深300", "code": "000300.SH", "color": "#4da3ff", "featured": False},
        ],
        "cn_broad_rsi": {
            "members": [
                {"name": "沪深300", "code": "000300.SH", "color": "#4da3ff"},
            ]
        },
        "hk_relative": {
            "benchmark": {"name": "恒生国企", "code": "HSCE.HK"},
            "members": [
                {"name": "恒生科技", "code": "HSTECH.HK", "color": "#4da3ff"},
            ],
        },
        "cn_fund_flow": {"enabled": True, "side_n": 10},
        "cn_turnover": {
            "components": [
                {"name": "上证综指", "sina_symbol": "sh000001", "em_secid": "1.000001"},
                {"name": "深证综指", "sina_symbol": "sz399106", "em_secid": "0.399106"},
            ]
        },
        "us_bottom_environment": {
            "trend_below_200_td": 5,
            "spx_short_ma_td": 50,
            "long_ma_td": 200,
            "acute_vix": 30,
            "acute_vol_ratio": 1.0,
        },
        "asia_leverage_charts": {
            "start": "2026-08-01",
            "countries": [
                {
                    "name": "韩国",
                    "msci_code": "KR",
                    "msci_currency": "KRW",
                    "msci_name": "MSCI韩国指数",
                    "msci_color": "#8fb7ff",
                    "indicators": [
                        {"id": "kr_credit_total", "label": "信用融资余额", "unit": "兆₩", "color": "#4da3ff"},
                    ],
                },
                {
                    "name": "日本",
                    "msci_code": "JP",
                    "msci_currency": "JPY",
                    "msci_name": "MSCI日本指数",
                    "msci_color": "#8fb7ff",
                    "indicators": [
                        {"id": "jp_margin_long", "label": "信用买入余额", "unit": "兆円", "color": "#ff9d3c"},
                    ],
                },
            ],
        },
    }
    seed = {
        "source_as_of": "2026-07-25",
        "anchor": {"date": "2019-06-28", "spx": 2941.76},
        "annual_nominal_gdp_pct": {"2024": 5.0, "2025": 4.8, "2026": 4.7},
        "normal_formula": {
            "nominal_gdp_beta": 1.219,
            "intercept": -0.8269,
            "earnings_share_growth": 2.2775,
            "buyback_growth": 3.8761,
        },
        "tax_reform_2018_growth": 13.8633,
        "valuation_snapshot": {"forward_pe": {"标普500": 21.5, "纳指100": 26.8}},
        "consensus": {"2026": {"real_gdp_pct": 2.1, "cpi_pct": 2.6}},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path, seed_path


def test_build_dashboard_writes_six_standalone_clean_panels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)
    cli = _load_cli(ws)

    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    base_wave = 100.0 + np.linspace(0, 60, len(dates)) + 5.0 * np.sin(np.arange(len(dates)) / 12.0)

    def _fake_prices(tickers, start, cache_path=None):
        out: dict[str, pd.Series] = {}
        for idx, t in enumerate(dict.fromkeys(tickers)):
            if t == "^VIX":
                out[t] = pd.Series(np.full(len(dates), 16.4), index=dates)
            elif t == "^VVIX":
                out[t] = pd.Series(np.full(len(dates), 84.0), index=dates)
            else:
                out[t] = pd.Series(base_wave + idx * 3.0, index=dates)
        return out

    def _fake_hist(codes, workspace, refresh=True):
        frames = {}
        for i, c in enumerate(dict.fromkeys(codes)):
            frames[c] = pd.DataFrame(
                {"close": base_wave + i * 20.0, "value": np.linspace(35.0, 82.0, len(dates))},
                index=dates,
            )
        return {"frames": frames, "refreshed": ["399303.SZ"], "stale": ["000300.SH"], "missing": [], "as_of": "2026-10-16"}

    def _fake_msci(code, currency, start, workspace, refresh=True):
        return pd.Series(np.linspace(1200.0, 1500.0, 50), index=dates[-50:]), "2026-10-16"

    def _fake_ohlc(tickers, start, cache_path=None):
        del start, cache_path
        out: dict[str, pd.DataFrame] = {}
        for t in dict.fromkeys(tickers):
            out[t] = pd.DataFrame(
                {
                    "High": base_wave + 2.0,
                    "Low": base_wave - 2.0,
                    "Close": base_wave,
                },
                index=dates,
            )
        return out

    monkeypatch.setattr(cli, "download_prices", create_autospec(cli.download_prices, side_effect=_fake_prices))
    monkeypatch.setattr(cli, "download_ohlc", create_autospec(cli.download_ohlc, side_effect=_fake_ohlc))
    monkeypatch.setattr(cli, "fetch_fred", create_autospec(cli.fetch_fred, return_value=pd.Series(np.linspace(3.1, 3.6, len(dates)), index=dates)))
    monkeypatch.setattr(cli, "load_index_history", create_autospec(cli.load_index_history, side_effect=_fake_hist))
    monkeypatch.setattr(cli, "fetch_msci_levels", create_autospec(cli.fetch_msci_levels, side_effect=_fake_msci))
    monkeypatch.setattr(
        cli,
        "fetch_fund_flow",
        create_autospec(
            cli.fetch_fund_flow,
            return_value={"ok": True, "asof": "2026-10-16 15:00", "date": "2026-10-16", "boards": [], "rank": {"top": [], "bottom": []}},
        ),
    )
    monkeypatch.setattr(
        cli,
        "fetch_market_turnover",
        create_autospec(
            cli.fetch_market_turnover,
            return_value={"ok": True, "asof": "2026-10-16 15:00", "date": "2026-10-16", "days": ["2026-10-15"], "today": [["09:35", 0.12]], "prev": [["09:35", 0.11]], "median": [["09:35", 0.10]]},
        ),
    )

    out_dir = ws / "research-outputs/sentiment-dashboard"
    result = cli.build_dashboard(out_dir / "index.html", use_cache=False)
    assert result["ok"] is True

    single_html = (out_dir / "index.html").read_text(encoding="utf-8")
    panels_dir = out_dir / "panels"
    panel_css = (panels_dir / "_shared/panel.css").read_text(encoding="utf-8")
    panel_js = (panels_dir / "_shared/panel.js").read_text(encoding="utf-8")

    # Unified A-share CSS variables & color conventions (JS reads via cssVar)
    assert "--dir-up:#ff6b6b" in panel_css
    assert "--dir-down:#38d39f" in panel_css
    assert "--temp-cold:#4da3ff" in panel_css
    assert "--temp-hot:#ff9d3c" in panel_css
    assert "const FLOW_UP=cssVar('--dir-up','#ff6b6b'),FLOW_DOWN=cssVar('--dir-down','#38d39f')" in panel_js
    assert (
        "const cbColor=v=>num(v)===null?'#7f93ae':v<15?TEMP_COLD:v<40?TEMP_COOL:"
        "v<60?TEMP_NEUTRAL:v<85?TEMP_WARM:TEMP_HOT;"
    ) in panel_js

    expected_slugs = [
        "market-overview",
        "us-market",
        "cn-market",
        "hk-market",
        "asia-leverage",
        "stocks",
    ]
    assert [p["slug"] for p in result["panels"]] == expected_slugs

    forbidden_visible = [
        "cold",
        "hot",
        "状态机",
        "cron",
        "config",
        "CSV",
        "Richard",
        "百分点评价信用压力",
        "单文件离线 HTML",
        "兆₩",
        "兆円",
        "亿NT$",
        "Staples",
    ]

    docs_to_audit: list[tuple[str, str]] = [("single", single_html)]
    panel_docs: dict[str, str] = {}
    for slug in expected_slugs:
        pfile = panels_dir / slug / "index.html"
        assert pfile.exists()
        doc = pfile.read_text(encoding="utf-8")
        panel_docs[slug] = doc
        docs_to_audit.append((slug, doc))

        # Standalone HTML + relative _shared assets + smaller than monolith
        assert doc.startswith("<!doctype html>")
        assert '<link rel="stylesheet" href="../_shared/panel.css">' in doc
        assert '<script src="../_shared/panel.js"></script>' in doc
        assert pfile.stat().st_size < (out_dir / "index.html").stat().st_size

        # Cross-panel navigation links
        for target_slug in expected_slugs:
            assert f'href="/monitors/{target_slug}"' in doc

        # Top color legend present everywhere; panels must not link out to eve.lumos.space
        assert "涨 / 净流入：红" in doc
        assert "冷清 / 低位：蓝" in doc
        assert "中概互联网相对强弱看板 →" not in doc
        assert "lumos" not in doc.lower()

    assert "lumos" not in single_html.lower()
    assert 'href="/monitors/relative-strength"' in single_html

    # Visible text check across single-page HTML & all 6 panels
    for label, doc in docs_to_audit:
        vis = _visible_text(doc)
        for bad in forbidden_visible:
            assert not re.search(rf"\b{re.escape(bad)}\b" if bad.isascii() else re.escape(bad), vis, re.IGNORECASE), (
                f"Forbidden term {bad!r} appeared in visible text of {label}"
            )

    # Panel scoping checks: each page carries only its own section data
    assert 'id="cbData"' in panel_docs["cn-market"]
    for other in ("market-overview", "us-market", "hk-market", "asia-leverage", "stocks"):
        assert 'id="cbData"' not in panel_docs[other]

    # GS Prime card fix, timezone labels & staleness checks on us-market
    us_vis = _visible_text(panel_docs["us-market"])
    assert "高盛 Prime 账本对冲基金仓位监控" in us_vis
    assert "来源：高盛 Prime 周报" in us_vis
    assert "周频 · 截至" in us_vis
    assert "（美东时间）" in us_vis
    assert "数据未更新（上次成功：" in us_vis
    assert "非自动更新，截至 2026-07-25（手工导入快照）" in us_vis

    # CN panel checks: probability header, margin dual-axis note, timezone, no "自刷新"
    cn_vis = _visible_text(panel_docs["cn-market"])
    assert "历史同向概率（参考）" in cn_vis
    assert "上涨频率" not in cn_vis
    assert "历史同向概率：高到低" in cn_vis
    assert "左轴：中证1000 点位；右轴：两融余额 RSI" in cn_vis
    assert "（北京时间）" in cn_vis
    assert "自刷新" not in cn_vis

    # Overview staleness, turnover freshness copy & manual snapshot notices
    ov_vis = _visible_text(panel_docs["market-overview"])
    assert "非自动更新，截至 2026-07-25" in ov_vis
    assert "数据未更新（上次成功：" in ov_vis
    assert "自刷新" not in ov_vis
    assert "交易时段每15分钟更新" in ov_vis

    # Asia currency normalization & weekly notice
    asia_vis = _visible_text(panel_docs["asia-leverage"])
    assert "万亿韩元" in asia_vis
    assert "万亿日元" in asia_vis
    assert "亿新台币" in asia_vis
    assert "周频更新" in asia_vis

    # Verify real upstream config classes were bound (not synthetic partial stubs)
    assert hasattr(cli.TopFrameworkConfig, "put_hedge_stats_start")
    assert hasattr(cli.BottomFrameworkConfig, "deep_index_rsi")

    # Stocks panel combines US + CN stocks cleanly, uses A-share bull/bear pills,
    # and renders AFRM trend card with 美东时间 timezone
    stocks_doc = panel_docs["stocks"]
    stocks_vis = _visible_text(stocks_doc)
    assert "美股个股监控" in stocks_vis
    assert "中国个股监控" in stocks_vis
    assert "宁德时代" in stocks_vis
    assert "伯克希尔（BRK-B）" in stocks_vis
    assert "当前趋势底" in stocks_vis
    assert "截至 2026-10-16（美东时间）" in stocks_vis
    assert ('class="pill bull"' in stocks_doc) or ('class="pill bear"' in stocks_doc)

    # Item 4: enlarged crowding sparkline legend injected from UI_COPY
    assert 'esc("蓝线：价格，窗口起点=100、独立纵轴")' in panel_js
    assert 'esc("橙线：拥挤度0–100、独立纵轴")' in panel_js

    # Item 5: every metric card displaying a YYYY-MM-DD date across all 6 panels carries a timezone
    metric_cards = re.findall(r'<article class="metric [^"]*">.*?</article>', "".join(panel_docs.values()), re.S)
    assert metric_cards
    for card_html in metric_cards:
        card_vis = _visible_text(card_html)
        if re.search(r"\d{4}-\d{2}-\d{2}", card_vis):
            assert ("美东时间" in card_vis) or ("北京时间" in card_vis), (
                f"Metric card date missing timezone: {card_vis!r}"
            )


def _patch_cli_fetchers(cli: Any, monkeypatch: pytest.MonkeyPatch, dates: pd.DatetimeIndex) -> None:
    base_wave = 100.0 + np.linspace(0, 60, len(dates)) + 5.0 * np.sin(np.arange(len(dates)) / 12.0)

    def _fake_prices(tickers, start, cache_path=None):
        del start, cache_path
        out: dict[str, pd.Series] = {}
        for idx, t in enumerate(dict.fromkeys(tickers)):
            if t == "^VIX":
                out[t] = pd.Series(np.full(len(dates), 16.4), index=dates)
            elif t == "^VVIX":
                out[t] = pd.Series(np.full(len(dates), 84.0), index=dates)
            else:
                out[t] = pd.Series(base_wave + idx * 3.0, index=dates)
        return out

    def _fake_hist(codes, workspace, refresh=True):
        del workspace, refresh
        frames = {}
        for i, c in enumerate(dict.fromkeys(codes)):
            frames[c] = pd.DataFrame(
                {"close": base_wave + i * 20.0, "value": np.linspace(35.0, 82.0, len(dates))},
                index=dates,
            )
        return {"frames": frames, "refreshed": ["399303.SZ"], "stale": ["000300.SH"], "missing": [], "as_of": "2026-10-16"}

    def _fake_msci(code, currency, start, workspace, refresh=True):
        del code, currency, start, workspace, refresh
        return pd.Series(np.linspace(1200.0, 1500.0, 50), index=dates[-50:]), "2026-10-16"

    def _fake_ohlc(tickers, start, cache_path=None):
        del start, cache_path
        out: dict[str, pd.DataFrame] = {}
        for t in dict.fromkeys(tickers):
            out[t] = pd.DataFrame(
                {
                    "High": base_wave + 2.0,
                    "Low": base_wave - 2.0,
                    "Close": base_wave,
                },
                index=dates,
            )
        return out

    monkeypatch.setattr(cli, "download_prices", create_autospec(cli.download_prices, side_effect=_fake_prices))
    monkeypatch.setattr(cli, "download_ohlc", create_autospec(cli.download_ohlc, side_effect=_fake_ohlc))
    monkeypatch.setattr(
        cli,
        "fetch_fred",
        create_autospec(cli.fetch_fred, return_value=pd.Series(np.linspace(3.1, 3.6, len(dates)), index=dates)),
    )
    monkeypatch.setattr(cli, "load_index_history", create_autospec(cli.load_index_history, side_effect=_fake_hist))
    monkeypatch.setattr(cli, "fetch_msci_levels", create_autospec(cli.fetch_msci_levels, side_effect=_fake_msci))
    monkeypatch.setattr(
        cli,
        "fetch_fund_flow",
        create_autospec(
            cli.fetch_fund_flow,
            return_value={"ok": True, "asof": "2026-10-16 15:00", "date": "2026-10-16", "boards": [], "rank": {"top": [], "bottom": []}},
        ),
    )
    monkeypatch.setattr(
        cli,
        "fetch_market_turnover",
        create_autospec(
            cli.fetch_market_turnover,
            return_value={"ok": True, "asof": "2026-10-16 15:00", "date": "2026-10-16", "days": ["2026-10-15"], "today": [["09:35", 0.12]], "prev": [["09:35", 0.11]], "median": [["09:35", 0.10]]},
        ),
    )


def test_split_panels_sha256_identical_across_repeated_builds_on_unchanged_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical inputs built at two different wall-clock timestamps produce identical panel hashes."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)
    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    _patch_cli_fetchers(cli, monkeypatch, dates)

    class _ClockDatetime(datetime):
        current = datetime(2026, 10, 16, 22, 20, 0, tzinfo=timezone(timedelta(hours=8)))

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", _ClockDatetime)
    out1 = ws / "out1/index.html"
    res1 = cli.build_dashboard(out1, use_cache=False)
    hashes1 = {
        str(p.relative_to(ws / "out1/panels")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ws / "out1/panels").rglob("*"))
        if p.is_file()
    }

    _ClockDatetime.current = datetime(2026, 10, 16, 23, 55, 17, tzinfo=timezone(timedelta(hours=8)))
    out2 = ws / "out2/index.html"
    res2 = cli.build_dashboard(out2, use_cache=False)
    hashes2 = {
        str(p.relative_to(ws / "out2/panels")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ws / "out2/panels").rglob("*"))
        if p.is_file()
    }

    assert res1["generated_at"] != res2["generated_at"]
    assert hashes1 == hashes2
    assert len(hashes1) == 8  # _shared/panel.css, _shared/panel.js, and 6 panels
    ov_html = (ws / "out1/panels/market-overview/index.html").read_text(encoding="utf-8")
    assert "数据截至 " in ov_html
    assert "22:20:00" not in ov_html
    assert "23:55:17" not in ov_html


def test_per_series_bbg_staleness_flags_us_when_ah_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When HK AH BBG series is fresh but US BBG series stopped updating, US & overview warn."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)

    # Override BBG CSV so AH series reaches today, while US series stalled 20 days ago.
    today = date.today()
    fresh_dates = pd.bdate_range(today - timedelta(days=45), today)
    stale_end = today - timedelta(days=20)
    stale_us_dates = pd.bdate_range(stale_end - timedelta(days=45), stale_end)
    n = min(len(fresh_dates), len(stale_us_dates))
    stale_us_str = stale_us_dates[-n:].strftime("%Y-%m-%d")
    fresh_ah_str = fresh_dates[-n:].strftime("%Y-%m-%d")
    pd.DataFrame({
        "GSPUCYDE Index": stale_us_str,
        "Last Price": np.linspace(1.0, 1.15, n),
        "GSPUCYDE Index.1": stale_us_str,
        "RSI 14D": np.linspace(45.0, 52.0, n),
        "GSCBHMOM Index": stale_us_str,
        "RSI 30D": np.linspace(48.0, 62.0, n),
        "QQQ US Equity": stale_us_str,
        "1M vol": np.full(n, 18.0),
        "QQQ US Equity.1": stale_us_str,
        "3M vol": np.full(n, 19.5),
        "HSAHP Index": fresh_ah_str,
        "Last Price.1": np.linspace(135.0, 142.0, n),
        "HSAHP Index.1": fresh_ah_str,
        "RSI 30D.1": np.linspace(40.0, 52.0, n),
    }).to_csv(ws / "temp/bbg_data_latest.csv", index=False)

    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    _patch_cli_fetchers(cli, monkeypatch, dates)

    out_dir = ws / "out_bbg"
    cli.build_dashboard(out_dir / "index.html", use_cache=False)

    us_vis = _visible_text((out_dir / "panels/us-market/index.html").read_text(encoding="utf-8"))
    ov_vis = _visible_text((out_dir / "panels/market-overview/index.html").read_text(encoding="utf-8"))
    hk_vis = _visible_text((out_dir / "panels/hk-market/index.html").read_text(encoding="utf-8"))

    assert "彭博美股序列" in us_vis
    assert "数据未更新（上次成功：" in us_vis
    assert "彭博" in ov_vis and "数据未更新（上次成功：" in ov_vis
    assert "AH溢价" not in hk_vis or "数据未更新（上次成功：" not in hk_vis


def test_bbg_intra_group_partial_stale_or_missing_triggers_banners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within paired BBG series (cyc, qqq vol, ah), one fresh series must not hide a stale/missing sibling."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)

    today = date.today()
    fresh_dates = pd.bdate_range(today - timedelta(days=45), today)
    stale_end = today - timedelta(days=20)
    stale_dates = pd.bdate_range(stale_end - timedelta(days=45), stale_end)
    n = min(len(fresh_dates), len(stale_dates))
    fresh_str = fresh_dates[-n:].strftime("%Y-%m-%d")
    stale_str = stale_dates[-n:].strftime("%Y-%m-%d")

    # Within each pair, one series is fresh and the sibling is stale (cyc_rsi, qqq_vol_3m) or missing (ah_rsi):
    pd.DataFrame({
        "GSPUCYDE Index": fresh_str,
        "Last Price": np.linspace(1.0, 1.15, n),
        "GSPUCYDE Index.1": stale_str,
        "RSI 14D": np.linspace(45.0, 52.0, n),
        "GSCBHMOM Index": fresh_str,
        "RSI 30D": np.linspace(48.0, 62.0, n),
        "QQQ US Equity": fresh_str,
        "1M vol": np.full(n, 18.0),
        "QQQ US Equity.1": stale_str,
        "3M vol": np.full(n, 19.5),
        "HSAHP Index": fresh_str,
        "Last Price.1": np.linspace(135.0, 142.0, n),
        "HSAHP Index.1": fresh_str,
        "RSI 30D.1": [np.nan] * n,
    }).to_csv(ws / "temp/bbg_data_latest.csv", index=False)

    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    _patch_cli_fetchers(cli, monkeypatch, dates)

    out_dir = ws / "out_partial_bbg"
    cli.build_dashboard(out_dir / "index.html", use_cache=False)

    us_vis = _visible_text((out_dir / "panels/us-market/index.html").read_text(encoding="utf-8"))
    hk_vis = _visible_text((out_dir / "panels/hk-market/index.html").read_text(encoding="utf-8"))
    ov_vis = _visible_text((out_dir / "panels/market-overview/index.html").read_text(encoding="utf-8"))

    assert "周期/防御RSI14截至" in us_vis
    assert "QQQ 3个月隐含波动率截至" in us_vis
    assert "AH溢价RSI30缺失" in hk_vis
    assert "AH溢价RSI30缺失" in ov_vis
    assert "周期/防御RSI14截至" in ov_vis


def test_selftest_us_monitor_contents_catches_card_removed_from_bottom_only(
    tmp_path: Path,
) -> None:
    """Deleting copy['card_env'] from bottom_monitor_section must fail us_monitor_contents."""
    import os
    import subprocess

    upstream_dir = _find_upstream_scripts()
    skill_dir = tmp_path / "skills/sentiment-dashboard"
    ws = tmp_path / "workspace"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir(parents=True)

    for p in upstream_dir.glob("*.py"):
        (skill_dir / "scripts" / p.name).write_bytes(p.read_bytes())
    for p in (upstream_dir.parent / "references").glob("*"):
        if p.is_file():
            (skill_dir / "references" / p.name).write_bytes(p.read_bytes())
    for p in SCRIPTS.glob("*.py"):
        (skill_dir / "scripts" / p.name).write_bytes(p.read_bytes())

    # Remove only bottom_monitor_section's copy["card_env"] card while keeping top_monitor_section's
    sec_path = skill_dir / "scripts/us_market_sections.py"
    sec_src = sec_path.read_text(encoding="utf-8")
    target = 'metric_card(copy["card_env"], env_summary'
    assert target in sec_src
    sec_path.write_text(sec_src.replace(target, 'metric_card("环境占位", env_summary'), encoding="utf-8")

    _populate_fake_workspace(ws)
    (ws / "temp/bbg_data_latest.csv").parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OPENCLAW_WORKSPACE=str(ws))
    proc = subprocess.run(
        [sys.executable, str(skill_dir / "scripts/sentiment_dashboard_cli.py"), "self-test", "--json"],
        cwd=skill_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    failed = {c["name"]: c for c in payload["checks"] if not c["ok"]}
    assert "us_monitor_contents" in failed


def _synthetic_jepq_pair(
    annual_rvol: float,
    d8_gap_target: float,
    *,
    n: int = 30,
    end_date: str = "2026-10-16",
) -> tuple[pd.Series, pd.Series]:
    """Construct synthetic QQQ and JEPQ close series with exact target 20D RVol and D-8 gap."""
    dates = pd.bdate_range(end=end_date, periods=n)
    alt = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(n - 1)], dtype=float)
    daily_std = annual_rvol / np.sqrt(252.0)
    rets = alt * (daily_std / float(pd.Series(alt[-20:]).std())) if annual_rvol > 0 else np.zeros(n - 1)
    qqq_vals = [500.0]
    for r in rets:
        qqq_vals.append(qqq_vals[-1] * (1.0 + float(r)))
    qqq_s = pd.Series(qqq_vals, index=dates, dtype=float)
    jepq_vals = [55.0] * n
    qqq_8d_ret = qqq_vals[-1] / qqq_vals[-9] - 1.0
    jepq_8d_ret = qqq_8d_ret - d8_gap_target
    jepq_vals[-1] = jepq_vals[-9] * (1.0 + jepq_8d_ret)
    jepq_s = pd.Series(jepq_vals, index=dates, dtype=float)
    return qqq_s, jepq_s


def test_build_dashboard_fetches_jepq_once_writes_signals_and_us_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_dashboard reuses the single download_prices call for QQQ+JEPQ, writes signals['jepq'], and renders the US panel block."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)
    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    _patch_cli_fetchers(cli, monkeypatch, dates)

    out_dir = ws / "out_jepq"
    result = cli.build_dashboard(out_dir / "index.html", use_cache=False)
    assert result["ok"] is True

    # download_prices was called exactly once with QQQ and JEPQ included
    assert cli.download_prices.call_count == 1
    requested_tickers = list(cli.download_prices.call_args.args[0])
    assert "QQQ" in requested_tickers
    assert "JEPQ" in requested_tickers

    signals = json.loads((out_dir / "signals.json").read_text(encoding="utf-8"))
    jepq = signals["jepq"]
    assert jepq["ok"] is True
    assert set(jepq) >= {"ok", "light", "reason", "d8_gap", "d7_gap", "rvol20", "qqq", "jepq", "asof"}
    assert jepq["light"] in {"🟢", "🟡", "🔴"}
    assert jepq["asof"] == "2026-10-16"

    us_vis = _visible_text((out_dir / "panels/us-market/index.html").read_text(encoding="utf-8"))
    assert "JEPQ—QQQ Put 价差观察" in us_vis
    assert "这只是规则筛选结果，不是独立的仓位指令。" in us_vis


def test_jepq_signal_levels_boundaries_and_missing_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify 🟢/🟡/🔴 thresholds on both sides, dual-breach reason, staleness, precision, and missing warning."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)
    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    # Both sides of D-8 gap (+0.3%) and RVol (20%, 25%) thresholds
    q_g, j_g = _synthetic_jepq_pair(0.199, 0.00299)
    res_g = cli.evaluate_jepq_signal(q_g, j_g)
    assert res_g["ok"] is True
    assert res_g["light"] == "🟢"
    assert res_g["reason"] == "条件较好 — D-8 gap 未超阈值，且波动率低于 20%"

    # Exact D-8 gap == 0.003 using clean binary fractions (QQQ flat 100, JEPQ 1000 -> 997 => gap == 0.003)
    dates_eq = pd.bdate_range(end="2026-10-16", periods=25)
    q_eq = pd.Series([100.0] * 25, index=dates_eq, dtype=float)
    j_eq = pd.Series([1000.0] * 24 + [997.0], index=dates_eq, dtype=float)
    # 0.0 - (997/1000 - 1) in IEEE-754 is 0.0030000000000000027 (> 0.003); verify strict comparison matches reference
    ref_gap = (100.0 / 100.0 - 1.0) - (997.0 / 1000.0 - 1.0)
    res_eq = cli.evaluate_jepq_signal(q_eq, j_eq)
    assert res_eq["d8_gap"] == ref_gap
    assert res_eq["light"] == ("🟢" if ref_gap <= 0.003 else "🔴")

    # Yellow band: both near lower (20.1%) and upper (24.9%) bounds
    q_y1, j_y1 = _synthetic_jepq_pair(0.201, 0.00299)
    res_y1 = cli.evaluate_jepq_signal(q_y1, j_y1)
    assert res_y1["ok"] is True and res_y1["light"] == "🟡"
    assert res_y1["reason"] == "条件一般 — D-8 gap 未超阈值，波动率为 20%—25%"

    q_y2, j_y2 = _synthetic_jepq_pair(0.249, 0.001)
    res_y2 = cli.evaluate_jepq_signal(q_y2, j_y2)
    assert res_y2["ok"] is True and res_y2["light"] == "🟡"

    # Red via D-8 gap > +0.3% (just above threshold)
    q_rg, j_rg = _synthetic_jepq_pair(0.16, 0.00301)
    res_rg = cli.evaluate_jepq_signal(q_rg, j_rg)
    assert res_rg["ok"] is True and res_rg["light"] == "🔴"
    assert res_rg["reason"] == "暂停观察 — D-8 gap 超过阈值"

    # Red via RVol >= 25% (just above threshold)
    q_rv, j_rv = _synthetic_jepq_pair(0.251, 0.001)
    res_rv = cli.evaluate_jepq_signal(q_rv, j_rv)
    assert res_rv["ok"] is True and res_rv["light"] == "🔴"
    assert res_rv["reason"] == "暂停观察 — 波动率不低于 25%"

    # Dual breach (D-8 gap > +0.3% AND RVol >= 25%): matches reference priority -> D-8 gap reason only
    q_rb, j_rb = _synthetic_jepq_pair(0.28, 0.005)
    res_rb = cli.evaluate_jepq_signal(q_rb, j_rb)
    assert res_rb["ok"] is True and res_rb["light"] == "🔴"
    assert res_rb["reason"] == "暂停观察 — D-8 gap 超过阈值"

    # Single-rounding precision check: unrounded d8_gap 0.0012504 renders +0.13% (not double-rounded +0.12%)
    q_pr, j_pr = _synthetic_jepq_pair(0.15, 0.0012504)
    res_pr = cli.evaluate_jepq_signal(q_pr, j_pr)
    assert res_pr["ok"] is True
    assert abs(res_pr["d8_gap"] - 0.0012504) < 1e-12
    assert res_pr["d8_gap"] != round(res_pr["d8_gap"], 6)
    pr_md = cli.format_jepq_digest_line(res_pr)
    pr_sec = cli.build_jepq_card_section(res_pr, paint=cli.paint)
    assert "+0.13%" in pr_md
    assert any("+0.13%" in line for line in pr_sec["lines"])

    # Stale JEPQ bar (missing latest session) or both lagging behind SPY reference -> treated as missing
    res_stale_jepq = cli.evaluate_jepq_signal(q_g, j_g.iloc[:-1], reference_series=q_g)
    assert res_stale_jepq["ok"] is False and res_stale_jepq["light"] is None
    assert "不一致" in res_stale_jepq["error"]

    res_stale_both = cli.evaluate_jepq_signal(q_g.iloc[:-1], j_g.iloc[:-1], reference_series=q_g)
    assert res_stale_both["ok"] is False and res_stale_both["light"] is None
    assert "早于" in res_stale_both["error"]

    # Missing JEPQ in build_dashboard -> degrades cleanly, warning on card bottom, card still succeeds
    orig_download_fn = cli.download_prices
    dates = pd.bdate_range("2023-01-02", "2026-10-16")
    _patch_cli_fetchers(cli, monkeypatch, dates)
    orig_download = cli.download_prices.side_effect

    def _download_without_jepq(tickers, start, cache_path=None):
        prices = orig_download(tickers, start, cache_path)
        prices.pop("JEPQ", None)
        return prices

    monkeypatch.setattr(
        cli,
        "download_prices",
        create_autospec(orig_download_fn, side_effect=_download_without_jepq),
    )
    out_dir = ws / "out_missing_jepq"
    res = cli.build_dashboard(out_dir / "index.html", use_cache=False)
    assert res["ok"] is True
    signals = json.loads((out_dir / "signals.json").read_text(encoding="utf-8"))
    assert signals["jepq"]["ok"] is False
    assert signals["jepq"]["light"] is None
    assert "缺少 JEPQ 行情" in signals["jepq"]["error"]
    assert any("JEPQ—QQQ 信号缺失" in w for w in signals["warnings"])

    state_file = ws / "state/sentiment-dashboard/jepq_light.json"
    cli.save_jepq_light(state_file, "🟢", "2026-10-15")
    digest_res = cli.run_digest(out_dir / "signals.json", ws / "temp/digest_missing.md", jepq_state_path=state_file)
    assert digest_res["ok"] is True
    card = json.loads(Path(digest_res["card_path"]).read_text(encoding="utf-8"))
    elements = card["body"]["elements"]
    assert "⚠️ JEPQ—QQQ 信号缺失" in elements[-1]["content"]
    # Existing saved light & asof are preserved when current run has insufficient data
    assert cli.load_jepq_state(state_file) == {"light": "🟢", "asof": "2026-10-15"}

    # Stale JEPQ (missing latest bar) during build_dashboard -> warning on card & preserved state
    def _download_stale_jepq(tickers, start, cache_path=None):
        prices = orig_download(tickers, start, cache_path)
        prices["JEPQ"] = prices["JEPQ"].iloc[:-1]
        return prices

    monkeypatch.setattr(
        cli,
        "download_prices",
        create_autospec(orig_download_fn, side_effect=_download_stale_jepq),
    )
    out_stale_dir = ws / "out_stale_jepq"
    res_s = cli.build_dashboard(out_stale_dir / "index.html", use_cache=False)
    assert res_s["ok"] is True
    sig_stale = json.loads((out_stale_dir / "signals.json").read_text(encoding="utf-8"))
    assert sig_stale["jepq"]["ok"] is False
    assert "不一致" in sig_stale["jepq"]["error"]
    assert any("JEPQ—QQQ 信号缺失" in w for w in sig_stale["warnings"])
    digest_stale = cli.run_digest(
        out_stale_dir / "signals.json", ws / "temp/digest_stale_bar.md", jepq_state_path=state_file
    )
    assert digest_stale["ok"] is True
    assert cli.load_jepq_state(state_file) == {"light": "🟢", "asof": "2026-10-15"}

    # Existing-ticker download gate: returning only JEPQ when all existing tickers fail must raise
    def _download_only_jepq(tickers, start, cache_path=None):
        del tickers, start, cache_path
        return {"JEPQ": pd.Series([55.0, 56.0], index=dates[-2:])}

    monkeypatch.setattr(
        cli,
        "download_prices",
        create_autospec(orig_download_fn, side_effect=_download_only_jepq),
    )
    with pytest.raises(RuntimeError, match="price download returned no usable series"):
        cli.build_dashboard(ws / "out_only_jepq/index.html", use_cache=False)


def test_jepq_digest_card_first_run_unchanged_changed_and_failure_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test first run, same-day/older-day asof guard, transition highlight, invalid payloads, and failure safety."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_path, seed_path = _populate_fake_workspace(ws)
    cli = _load_cli(ws)
    monkeypatch.setattr(cli, "WORKSPACE", ws)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "SEED_PATH", seed_path)

    today = date.today()
    # Three consecutive trading days ending on the latest weekday, so the test also holds on weekends.
    trading_days = pd.bdate_range(end=pd.Timestamp(today), periods=3)
    day1_str, day2_str, today_str = (d.date().isoformat() for d in trading_days)
    q_y, j_y = _synthetic_jepq_pair(0.22, 0.001, end_date=day2_str)
    q_r_same, j_r_same = _synthetic_jepq_pair(0.16, 0.0045, end_date=day2_str)
    q_r_old, j_r_old = _synthetic_jepq_pair(0.16, 0.0045, end_date=day1_str)
    q_r_new, j_r_new = _synthetic_jepq_pair(0.16, 0.0045, end_date=today_str)
    jepq_yellow = cli.evaluate_jepq_signal(q_y, j_y)
    jepq_red_same_day = cli.evaluate_jepq_signal(q_r_same, j_r_same)
    jepq_red_old_day = cli.evaluate_jepq_signal(q_r_old, j_r_old)
    jepq_red_new_day = cli.evaluate_jepq_signal(q_r_new, j_r_new)
    assert jepq_yellow["light"] == "🟡"
    assert jepq_red_new_day["light"] == "🔴"

    def _base_signals(jepq_block: Any, sig_date: str = today_str) -> dict[str, Any]:
        return {
            "ok": True,
            "date": sig_date,
            "us_market": {
                "topbottom": {
                    "hits": [],
                    "top_hits": [],
                    "bottom_hits": [],
                    "bottom_reading": "底部框架：无活动信号",
                    "top_framework": {"active": False, "stage": "无信号", "summary": "无活动信号", "environment": {}, "put_hedge": {}},
                    "bottom_framework": {"active": False, "stage": "无信号", "summary": "无活动信号", "environment": {}},
                },
                "momentum": {"hits": [], "reading": "GSCBHMOM RSI14 50.0"},
                "ndx_spw": {"hits": [], "reading": "纳指/等权 RSI14 52.0"},
                "tech_rotation": [{"name": "六大科技", "state": "中性"}],
                "sectors": {"hits": [], "reading": "11 个一级行业全在中间区"},
            },
            "us_stocks": {
                "megacap": {"hits": [], "reading": "6 只全在 40—60 之间"},
                "TSM": {"hit": ""},
                "AFRM": {"hit": ""},
            },
            "cn_a": {"hits": [], "reading": "两融余额 RSI14 48.0"},
            "hk": {"hits": [], "reading": "AH溢价 RSI30 -48.0"},
            "asia": {"hits": [], "reading": "韩国 50 · 日本 50 · 台湾 50"},
            "jepq": jepq_block,
            "warnings": [],
        }

    signals_path = ws / "signals.json"
    state_path = ws / "state/sentiment-dashboard/jepq_light.json"

    # 1. First run (no state file): treated as unchanged, writes {light, asof} after card generation
    signals_path.write_text(json.dumps(_base_signals(jepq_yellow), ensure_ascii=False), encoding="utf-8")
    res1 = cli.run_digest(signals_path, ws / "out/digest1.md", jepq_state_path=state_path)
    assert res1["ok"] is True
    assert res1["jepq"] == {"light": "🟡", "prev_light": None, "changed": False}
    card1 = json.loads(Path(res1["card_path"]).read_text(encoding="utf-8"))
    md1 = Path(res1["markdown_path"]).read_text(encoding="utf-8")
    dump1 = json.dumps(card1, ensure_ascii=False)
    assert "**JEPQ—QQQ Put 价差**" in dump1
    assert "信号灯由" not in dump1
    assert f"QQQ {jepq_yellow['qqq']:.2f} ｜ JEPQ {jepq_yellow['jepq']:.2f}" in dump1
    assert "阈值 ≤+0.3%" in dump1
    assert "D-7 gap" in dump1
    assert "QQQ 20D RVol" in dump1
    assert "这只是规则筛选结果，不是独立的仓位指令。" in dump1
    assert "- JEPQ—QQQ：🟡 " in md1
    assert cli.load_jepq_state(state_path) == {"light": "🟡", "asof": jepq_yellow["asof"]}

    # 2a. Same asof repeated (even with different light): treated as unchanged, state NOT overwritten
    signals_path.write_text(json.dumps(_base_signals(jepq_red_same_day), ensure_ascii=False), encoding="utf-8")
    res2a = cli.run_digest(signals_path, ws / "out/digest2a.md", jepq_state_path=state_path)
    assert res2a["ok"] is True
    assert res2a["jepq"] == {"light": "🔴", "prev_light": "🟡", "changed": False}
    dump2a = Path(res2a["card_path"]).read_text(encoding="utf-8")
    assert "**JEPQ—QQQ Put 价差**" in dump2a
    assert "信号灯由" not in dump2a
    assert cli.load_jepq_state(state_path) == {"light": "🟡", "asof": jepq_yellow["asof"]}

    # 2b. Older asof (< saved asof): treated as unchanged, state NOT overwritten
    signals_path.write_text(json.dumps(_base_signals(jepq_red_old_day), ensure_ascii=False), encoding="utf-8")
    res2b = cli.run_digest(signals_path, ws / "out/digest2b.md", jepq_state_path=state_path)
    assert res2b["ok"] is True
    assert res2b["jepq"] == {"light": "🔴", "prev_light": "🟡", "changed": False}
    assert "信号灯由" not in Path(res2b["card_path"]).read_text(encoding="utf-8")
    assert cli.load_jepq_state(state_path) == {"light": "🟡", "asof": jepq_yellow["asof"]}

    # 3. Newer asof with light change 🟡 -> 🔴: prominent colored transition title + state updated
    signals_path.write_text(json.dumps(_base_signals(jepq_red_new_day), ensure_ascii=False), encoding="utf-8")
    res3 = cli.run_digest(signals_path, ws / "out/digest3.md", jepq_state_path=state_path)
    assert res3["ok"] is True
    assert res3["jepq"] == {"light": "🔴", "prev_light": "🟡", "changed": True}
    card3 = json.loads(Path(res3["card_path"]).read_text(encoding="utf-8"))
    md3 = Path(res3["markdown_path"]).read_text(encoding="utf-8")
    dump3 = json.dumps(card3, ensure_ascii=False)
    assert "**<font color='red'>JEPQ—QQQ 信号灯由 🟡 转为 🔴</font>**" in dump3
    assert "当前判断：<font color='red'>🔴 暂停观察 — D-8 gap 超过阈值</font>" in dump3
    assert f"QQQ {jepq_red_new_day['qqq']:.2f} ｜ JEPQ {jepq_red_new_day['jepq']:.2f}" in dump3
    assert "这只是规则筛选结果，不是独立的仓位指令。" in dump3
    assert "- JEPQ—QQQ 信号灯由 🟡 转为 🔴：🔴 暂停观察 — D-8 gap 超过阈值" in md3
    assert cli.load_jepq_state(state_path) == {"light": "🔴", "asof": jepq_red_new_day["asof"]}

    # 4. Invalid / partial / non-numeric JEPQ payloads must NOT crash or update state
    for bad_payload in (
        {"ok": True, "light": "🟡"},
        {"ok": True, "light": "🟡", "reason": "ok", "asof": today_str, "d8_gap": "bad", "d7_gap": 0.0, "rvol20": 0.1, "qqq": 500, "jepq": 55},
        {"ok": True, "light": "🟡", "reason": "ok", "asof": today_str, "d8_gap": True, "d7_gap": 0.0, "rvol20": 0.1, "qqq": 500, "jepq": 55},
        "corrupted",
    ):
        assert cli.validate_jepq(bad_payload) is None
        signals_path.write_text(json.dumps(_base_signals(bad_payload), ensure_ascii=False), encoding="utf-8")
        res_bad = cli.run_digest(signals_path, ws / "out/digest_bad.md", jepq_state_path=state_path)
        assert res_bad["ok"] is True
        assert res_bad["jepq"]["changed"] is False
        assert res_bad["jepq"]["light"] is None
        bad_card = json.loads(Path(res_bad["card_path"]).read_text(encoding="utf-8"))
        assert "数据不足" in json.dumps(bad_card, ensure_ascii=False)
        assert "⚠️ JEPQ—QQQ 信号缺失" in bad_card["body"]["elements"][-1]["content"]
        assert "数据不足" in Path(res_bad["markdown_path"]).read_text(encoding="utf-8")
        assert cli.load_jepq_state(state_path) == {"light": "🔴", "asof": jepq_red_new_day["asof"]}

    # 5. Card generation failure must NOT overwrite state
    q_g_next, j_g_next = _synthetic_jepq_pair(0.15, 0.001, end_date=(today + timedelta(days=1)).isoformat())
    jepq_green_next = cli.evaluate_jepq_signal(q_g_next, j_g_next)
    signals_path.write_text(json.dumps(_base_signals(jepq_green_next), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "digest_card", create_autospec(cli.digest_card, side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        cli.run_digest(signals_path, ws / "out/digest_fail.md", jepq_state_path=state_path)
    assert cli.load_jepq_state(state_path) == {"light": "🔴", "asof": jepq_red_new_day["asof"]}

    # 6. Stale signals.json fails cleanly and does NOT overwrite state
    stale_date = (today - timedelta(days=10)).isoformat()
    signals_path.write_text(json.dumps(_base_signals(jepq_green_next, sig_date=stale_date), ensure_ascii=False), encoding="utf-8")
    res_stale = cli.run_digest(signals_path, ws / "out/digest_stale.md", jepq_state_path=state_path)
    assert res_stale["ok"] is False
    assert cli.load_jepq_state(state_path) == {"light": "🔴", "asof": jepq_red_new_day["asof"]}


def _write_full_selftest_references(references_dir: Path) -> None:
    """Write complete dashboard_config.json and spx_model_seed.json matching selftest invariants."""
    references_dir.mkdir(parents=True, exist_ok=True)
    full_config = {
        "market_tickers": {
            "SPX": "^GSPC",
            "NDX": "^NDX",
            "SPW": "^SPXEW",
            "QQQ": "QQQ",
            "SPY": "SPY",
            "SOX": "^SOX",
            "XSW": "XSW",
            "VIX": "^VIX",
            "VVIX": "^VVIX",
        },
        "mags": ["AMZN", "GOOGL", "META", "MSFT", "AAPL", "NVDA"],
        "quantum": ["IONQ", "RGTI", "QBTS", "QUBT"],
        "semiconductor_breadth": ["NVDA", "AVGO"],
        "relative_signal": {
            "rsi_period": 30,
            "top_rsi": 60,
            "bottom_rsi": 40,
            "top_ma200_dev_pct": 20,
            "bottom_ma200_dev_pct": -5,
            "position_lookback_days": 504,
        },
        "thresholds": {"us_top_rsi30": 68, "rsi_hot": 70, "rsi_cold": 30},
        "us_bottom_framework": {
            "gsp_alert_rsi": 35,
            "qqq_alert_drawdown": -0.10,
            "pressure_vix": 20,
            "pressure_index_rsi": 38,
            "deep_index_rsi": 30,
        },
        "us_top_framework": {
            "window_td": 20,
            "excess_timeout_td": 40,
            "gsp_warn_rsi30": 68,
            "quantum_warn_gain_pct": 100,
            "excess_qqq_rsi14": 75,
            "excess_qqq_200dma_pct": 20,
            "excess_momentum_rsi": 75,
            "confirm_drawdown_pct": 5,
            "confirm_ma_td": 20,
            "put_hedge_vvix_vix": 6.5,
            "put_hedge_cheap_vix": 15,
            "cool_drawdown_15_pct": -15,
            "weak_trend_premium_pct": 0,
            "quantum_low_td": 60,
        },
        "gs_prime": {
            "net_cold_pctile": 10,
            "net_hot_pctile": 90,
            "gross_cold_pctile": 10,
            "gross_hot_pctile": 90,
            "mag7_net_hot": 20.0,
            "mag7_net_cold": 10.0,
            "fresh_lag_days": 7,
        },
        "sp_sector_etfs": [
            {"ticker": t, "name": t, "color": "#4da3ff"}
            for t in ("XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU")
        ],
        "equal_weight_baskets": [
            {"key": "INTERNET", "label": "Internet等权", "members": ["NFLX", "BKNG", "ABNB", "DASH"], "benchmark": "QQQ", "color": "#a377ff"},
            {"key": "STAPLES", "label": "Staples等权", "members": ["WMT", "COST", "TMUS", "PEP", "CMCSA"], "benchmark": "QQQ", "color": "#38d39f"},
        ],
        "relative_extras": [
            {"name": "TSM", "ticker": "TSM", "benchmark": "SOX", "ma": 100, "color": "#4da3ff"},
            {"name": "INTC", "ticker": "INTC", "benchmark": "SOX", "ma": 100, "color": "#a377ff"},
            {"name": "BRKB", "ticker": "BRK-B", "benchmark": "SPW", "ma": 200, "color": "#ffbf3c"},
        ],
        "single_stocks": [
            {"name": "宁德时代", "ticker": "300750.SZ", "benchmark": "000300.SS"},
        ],
        "trend_lines": [
            {
                "ticker": "AMZN",
                "label": "AMZN",
                "start": "2022-07-01",
                "mode": "channel",
                "note": "软件标 287.200 校验",
                "fit": {"anchor": "2025-10-16", "fitted_on": "2025-10-16", "slope": 0.12, "bottom": 180.0, "top": 240.0, "bottom_pts": 8, "top_pts": 8},
            },
            {
                "ticker": "AFRM",
                "label": "AFRM",
                "start": "2023-07-01",
                "mode": "support",
                "fit": {"anchor": "2025-10-16", "fitted_on": "2025-10-16", "slope": 0.05, "bottom": 40.0, "bottom_pts": 8},
            },
        ],
        "cn_a_indices": [
            {"name": "国证2000", "code": "399303.SZ", "color": "#4da3ff", "featured": True, "note": "替代中证2000"},
            {"name": "创业板指", "code": "399006.SZ", "color": "#a377ff", "featured": False},
            {"name": "科创50", "code": "000688.SH", "color": "#38d39f", "featured": False},
            {"name": "通信设备", "code": "931160.CSI", "color": "#4dd2ff", "featured": False},
            {"name": "中证全指半导体", "code": "H30184.CSI", "color": "#ff6b6b", "featured": False},
            {"name": "红利低波", "code": "H30269.CSI", "color": "#91a3bd", "featured": False},
            {"name": "CS电池", "code": "931719.CSI", "color": "#c5d2e5", "featured": False},
        ],
        "cn_broad_rsi": {
            "members": [
                {"name": "创业板指", "code": "399006.SZ", "color": "#a377ff"},
                {"name": "沪深300", "code": "000300.SH", "color": "#4da3ff"},
            ]
        },
        "hk_relative": {
            "benchmark": {"name": "恒生国企", "code": "HSCE.HK"},
            "benchmark_note": "替代 MSCI China ex A",
            "members": [
                {"name": "恒生科技", "code": "HSTECH.HK", "color": "#4da3ff"},
                {"name": "港股通高股息低波", "code": "HSHYLV.HK", "color": "#38d39f", "note": "替代 930914 / 932596；未选 930915 因其非港元口径"},
                {"name": "恒生消费", "code": "HSCGSI.HK", "color": "#a377ff"},
                {"name": "恒生医疗", "code": "HSHCI.HK", "color": "#ff6b6b"},
            ],
        },
        "cn_fund_flow": {
            "enabled": True,
            "refresh_minutes": 15,
            "side_n": 8,
            "host": "push2delay.eastmoney.com",
            "board_scope": "m:90 s:8",
        },
        "cn_turnover": {
            "components": [
                {"name": "上证综指", "sina_symbol": "sh000001", "em_secid": "1.000001"},
                {"name": "深证综指", "sina_symbol": "sz399106", "em_secid": "0.399106"},
            ]
        },
        "us_bottom_environment": {
            "trend_below_200_td": 5,
            "spx_short_ma_td": 50,
            "long_ma_td": 200,
            "acute_vix": 30,
            "acute_vol_ratio": 1.0,
        },
        "asia_leverage_charts": {
            "start": "2023-01-01",
            "countries": [
                {
                    "key": "kr",
                    "name": "韩国",
                    "msci_code": "941000",
                    "msci_currency": "KRW",
                    "msci_name": "MSCI韩国指数",
                    "msci_color": "#8fb7ff",
                    "indicators": [
                        {"id": "kr_credit_total", "label": "信用融资余额", "unit": "兆₩", "color": "#4da3ff"},
                        {"id": "kr_credit_ratio", "label": "信用融资比率", "unit": "%", "color": "#ff9d3c"},
                    ],
                },
                {
                    "key": "jp",
                    "name": "日本",
                    "msci_code": "939200",
                    "msci_currency": "JPY",
                    "msci_name": "MSCI日本指数",
                    "msci_color": "#8fb7ff",
                    "indicators": [
                        {"id": "jp_margin_long", "label": "信用买入余额", "unit": "兆円", "color": "#ff9d3c"},
                        {"id": "jp_margin_ratio", "label": "信用倍率", "unit": "x", "color": "#4da3ff"},
                    ],
                },
                {
                    "key": "tw",
                    "name": "台湾",
                    "msci_code": "915800",
                    "msci_currency": "TWD",
                    "msci_name": "MSCI台湾指数",
                    "msci_color": "#8fb7ff",
                    "indicators": [
                        {"id": "tw_margin_balance", "label": "融资余额", "unit": "亿NT$", "color": "#38d39f"},
                        {"id": "tw_usage_p90", "label": "融资使用率P90", "unit": "%", "color": "#a377ff"},
                    ],
                },
            ],
        },
    }
    full_seed = {
        "source_as_of": "2026-07-25",
        "anchor": {"date": "2019-06-28", "spx": 2941.76},
        "annual_nominal_gdp_pct": {"2015": 4.0, "2024": 5.0, "2025": 4.8, "2026": 4.7},
        "normal_formula": {
            "nominal_gdp_beta": 1.219,
            "intercept": -0.8269,
            "earnings_share_growth": 2.2775,
            "buyback_growth": 3.8761,
        },
        "tax_reform_2018_growth": 13.8633,
        "valuation_snapshot": {"forward_pe": {"标普500": 21.5, "纳指100": 26.8}},
        "consensus": {"2026": {"real_gdp_pct": 2.1, "cpi_pct": 2.6}},
    }
    (references_dir / "dashboard_config.json").write_text(
        json.dumps(full_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (references_dir / "spx_model_seed.json").write_text(
        json.dumps(full_seed, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_full_selftest_upstream_modules(scripts_dir: Path) -> None:
    """Write minimal complete upstream framework modules so `cli.py self-test --json` runs end-to-end."""
    (scripts_dir / "openclaw_workspace.py").write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "from pathlib import Path\n"
        "def workspace_root(source: object = None) -> Path:\n"
        "    del source\n"
        "    return Path(os.environ['OPENCLAW_WORKSPACE']).expanduser()\n",
        encoding="utf-8",
    )
    (scripts_dir / "us_bottom_environment_selftest.py").write_text(
        "def bottom_environment_checks(*args, **kwargs):\n    return []\n",
        encoding="utf-8",
    )
    (scripts_dir / "us_top_framework_selftest.py").write_text(
        "def top_framework_checks(*args, **kwargs):\n    return []\n",
        encoding="utf-8",
    )
    (scripts_dir / "us_top_framework.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "import pandas as pd\n"
        "@dataclass\n"
        "class TopFrameworkConfig:\n"
        "    window_td: int = 20\n"
        "    excess_timeout_td: int = 40\n"
        "    gsp_warn_rsi30: float = 68.0\n"
        "    quantum_warn_gain_pct: float = 100.0\n"
        "    excess_qqq_rsi14: float = 75.0\n"
        "    excess_qqq_200dma_pct: float = 20.0\n"
        "    excess_momentum_rsi: float = 75.0\n"
        "    confirm_drawdown_pct: float = 5.0\n"
        "    confirm_ma_td: int = 20\n"
        "    put_hedge_vvix_vix: float = 6.5\n"
        "    put_hedge_cheap_vix: float = 15.0\n"
        "    cool_drawdown_15_pct: float = -15.0\n"
        "    weak_trend_premium_pct: float = 0.0\n"
        "    quantum_low_td: int = 60\n"
        "    put_hedge_stats_start: str = '2010-01-01'\n"
        "def _completed_us_daily_frame(df: pd.DataFrame) -> pd.DataFrame:\n"
        "    return df\n"
        "def evaluate_top_framework(*args, **kwargs) -> dict[str, Any]:\n"
        '    frame = pd.DataFrame({"exc_qqq_rsi": [False], "exc_qqq_200": [False], "exc_mom": [False]})\n'
        '    frame["excess"] = frame["exc_qqq_rsi"] | frame["exc_qqq_200"] | frame["exc_mom"]\n'
        '    put_hedge = {"on": False, "summary": "无put保护提醒"}\n'
        '    return {"ok": True, "stage": "无信号", "active": False, "summary": "无活动信号", "environment": {"weak_now": False}, "metrics": {}, "put_hedge": put_hedge}\n'
        "def top_digest_lines(topbottom: dict[str, Any], fmt: Any) -> list[str]:\n"
        "    fw = topbottom.get('top_framework') or {}\n"
        "    if not fw:\n"
        "        return []\n"
        "    m = fw.get('metrics') or {}\n"
        "    env = fw.get('environment') or {}\n"
        "    hedge = fw.get('put_hedge') or {}\n"
        "    gsp = fmt(m.get('gsp_rsi30'), 1)\n"
        "    return [\n"
        "        f\"- 美股顶部：{fw.get('summary') or '无活动信号'}；周期股/防御股相对指数 RSI30 {gsp}\",\n"
        "        f\"- 顶部环境：{env.get('summary') or '强预警环境'}\",\n"
        "        f\"- 指数 put 保护：{hedge.get('summary') or '无put保护提醒'}\",\n"
        "    ]\n",
        encoding="utf-8",
    )
    (scripts_dir / "us_bottom_framework.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from datetime import datetime, time\n"
        "from typing import Any\n"
        "import numpy as np\n"
        "import pandas as pd\n"
        "@dataclass\n"
        "class BottomFrameworkConfig:\n"
        "    gsp_alert_rsi: float = 35.0\n"
        "    qqq_alert_drawdown: float = -0.10\n"
        "    pressure_vix: float = 20.0\n"
        "    pressure_index_rsi: float = 38.0\n"
        "    deep_index_rsi: float = 30.0\n"
        "def _wilder(series: pd.Series, period: int = 14) -> pd.Series:\n"
        "    vals = pd.to_numeric(series, errors='coerce').dropna()\n"
        "    delta = vals.diff()\n"
        "    gain = delta.clip(lower=0)\n"
        "    loss = -delta.clip(upper=0)\n"
        "    out = pd.Series(np.nan, index=vals.index, dtype=float)\n"
        "    if len(vals) <= period:\n"
        "        return out\n"
        "    ag = float(gain.iloc[1:period + 1].mean())\n"
        "    al = float(loss.iloc[1:period + 1].mean())\n"
        "    out.iloc[period] = 100.0 if al == 0 and ag > 0 else (50.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))\n"
        "    for i in range(period + 1, len(vals)):\n"
        "        ag = (ag * (period - 1) + float(gain.iloc[i])) / period\n"
        "        al = (al * (period - 1) + float(loss.iloc[i])) / period\n"
        "        out.iloc[i] = 100.0 if al == 0 and ag > 0 else (50.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))\n"
        "    return out\n"
        "def _completed_us_daily_frame(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:\n"
        "    if now is not None and now.time() < time(16, 15):\n"
        "        return df.iloc[:-1] if len(df) > 1 else df\n"
        "    return df\n"
        "def evaluate_bottom_framework(cyc_rsi14: pd.Series, spx: pd.Series, qqq: pd.Series, vix_price: pd.Series, cfg: BottomFrameworkConfig, *, now: datetime | None = None, gsp_close: pd.Series | None = None) -> dict[str, Any]:\n"
        "    # Keep selftest invariant markers: row.spx_rsi14) < cfg.deep_index_rsi and row.qqq_rsi14) < cfg.deep_index_rsi\n"
        "    _ = (spx, qqq, vix_price)\n"
        "    is_intraday = now is not None and now.time() < time(16, 15)\n"
        "    if is_intraday and isinstance(gsp_close, pd.Series) and len(gsp_close) > 16:\n"
        "        hist = gsp_close.iloc[:-1]\n"
        "        lo, hi = float(hist.iloc[-1]) * 0.5, float(hist.iloc[-1]) * 1.5\n"
        "        idx_last = gsp_close.index[-1]\n"
        "        for _ in range(60):\n"
        "            mid = (lo + hi) / 2.0\n"
        "            r = float(_wilder(pd.concat([hist, pd.Series([mid], index=[idx_last])])).iloc[-1])\n"
        "            if r < cfg.gsp_alert_rsi:\n"
        "                lo = mid\n"
        "            else:\n"
        "                hi = mid\n"
        "        gate = (lo + hi) / 2.0\n"
        "        would = float(cyc_rsi14.iloc[-1]) <= cfg.gsp_alert_rsi\n"
        "        return {'ok': True, 'stage': '无信号', 'active': False, 'summary': '无活动信号', 'projection': {'active': False}, 'alert_projection': {'active': would, 'thresholds': {'gsp_close': gate}, 'scenario': {'alert_would_trigger': would}}}\n"
        "    settled_alert = (not cyc_rsi14.empty) and float(cyc_rsi14.iloc[-1]) <= cfg.gsp_alert_rsi\n"
        "    return {'ok': True, 'stage': '预警' if settled_alert else '无信号', 'active': settled_alert, 'summary': '预警' if settled_alert else '无活动信号', 'projection': {'active': settled_alert}, 'alert_projection': {'active': False, 'thresholds': {}, 'scenario': {'alert_would_trigger': False}}}\n"
        "def build_bottom_environment(*args, **kwargs):\n"
        "    return {'summary': '正常趋势候选', 'tone': 'neutral', 'breadth': {}, 'metrics': {}}, None\n"
        "def projection_digest_lines(bottom_framework: dict[str, Any]) -> list[str]:\n"
        "    proj = bottom_framework.get('projection') or {}\n"
        "    if proj.get('active'):\n"
        "        return ['- 底部收盘门槛：SPX/QQQ 关注压力阈值']\n"
        "    return []\n"
        "def alert_projection_lead(bottom_framework: dict[str, Any]) -> str:\n"
        "    ap = bottom_framework.get('alert_projection') or {}\n"
        "    return '盘中已破预警线，待收盘确认' if (ap.get('scenario') or {}).get('alert_would_trigger') else ''\n"
        "def alert_projection_digest_lines(bottom_framework: dict[str, Any]) -> list[str]:\n"
        "    ap = bottom_framework.get('alert_projection') or {}\n"
        "    if (ap.get('scenario') or {}).get('alert_would_trigger'):\n"
        "        gate = (ap.get('thresholds') or {}).get('gsp_close')\n"
        "        return [\n"
        "            '- 底部盘中情景：若按当前价格收盘会触发底部预警',\n"
        "            f'- 底部预警收盘门槛：周期股/防御股相对指数≤{gate:.2f}',\n"
        "        ]\n"
        "    return []\n"
        "def bottom_environment_digest_line(topbottom: dict[str, Any], fmt: Any = None) -> str:\n"
        "    _ = (topbottom, fmt)\n"
        "    return ''\n",
        encoding="utf-8",
    )


def test_selftest_jepq_offline_checks_all_pass(tmp_path: Path) -> None:
    """Full `sentiment_dashboard_cli.py self-test --json` exits 0 with ok=true when references/ is populated."""
    import os
    import subprocess

    skill_dir = tmp_path / "skills/sentiment-dashboard"
    ws = tmp_path / "workspace"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir(parents=True)

    _write_full_selftest_upstream_modules(skill_dir / "scripts")
    for p in SCRIPTS.glob("*.py"):
        (skill_dir / "scripts" / p.name).write_bytes(p.read_bytes())

    _populate_fake_workspace(ws)
    _write_full_selftest_references(skill_dir / "references")

    env = dict(os.environ, OPENCLAW_WORKSPACE=str(ws))
    proc = subprocess.run(
        [sys.executable, str(skill_dir / "scripts/sentiment_dashboard_cli.py"), "self-test", "--json"],
        cwd=skill_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    payload = json.loads(proc.stdout)
    failed = [c for c in payload["checks"] if not c["ok"]]
    assert proc.returncode == 0, f"self-test failed ({len(failed)}): {failed[:5]}"
    assert payload["ok"] is True
    by_name = {c["name"]: c for c in payload["checks"]}
    for check_name in (
        "jepq_wired_in_build_and_signals",
        "jepq_signal_three_lights_and_boundaries",
        "jepq_light_change_detection",
        "digest_writes_card_json",
        "digest_reads_signals_only",
        "us_monitor_section_order",
    ):
        assert check_name in by_name
        assert by_name[check_name]["ok"] is True


def test_jepq_signal_uses_the_latest_reference_day_and_strict_validation(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    _populate_fake_workspace(ws)
    cli = _load_cli(ws)
    q, j = _synthetic_jepq_pair(0.16, 0.0045, end_date="2026-11-24")
    spy_stale = q.copy()
    gspc_fresh = pd.concat([q, pd.Series([q.iloc[-1]], index=[pd.Timestamp("2026-11-25")])])
    stale = cli.evaluate_jepq_signal(q, j, reference_series=[spy_stale, gspc_fresh])
    assert stale["ok"] is False and "早于美股参考标的(2026-11-25)" in stale["error"]
    none_ref = cli.evaluate_jepq_signal(q, j, reference_series=[None, pd.Series(dtype=float)])
    assert none_ref["ok"] is False and "缺少美股参考标的行情" in none_ref["error"]
    fresh = cli.evaluate_jepq_signal(q, j, reference_series=[spy_stale, None])
    assert fresh["ok"] is True

    import jepq_signal as js

    assert js._valid_iso_date("2026-99-99") == ""
    assert js._valid_iso_date("2026-11-25") == "2026-11-25"
    assert js._parse_finite_float(10**400) is None
