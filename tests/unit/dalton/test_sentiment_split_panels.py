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
    / "openclaw_overlays"
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
        REPO_ROOT.parent.parent / "dalton-agy/design/dashboards/sentiment-dashboard/scripts",
        Path("/Users/everflow/Projects/dalton-agy/design/dashboards/sentiment-dashboard/scripts"),
        Path.home() / ".openclaw/workspace/skills/sentiment-dashboard/scripts",
    ]
    for candidate in candidates:
        if (
            (candidate / "openclaw_workspace.py").is_file()
            and (candidate / "us_bottom_framework.py").is_file()
            and (candidate / "us_top_framework.py").is_file()
        ):
            return candidate
    raise FileNotFoundError(
        "Real upstream sentiment-dashboard scripts directory not found in dalton-agy/design or ~/.openclaw"
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
    for mod_name in ("dashboard_copy", "gs_prime_positioning", "us_market_sections"):
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
