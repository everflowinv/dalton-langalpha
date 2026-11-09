"""Observation dates on the US monitor cards, and the missing A-share volume note."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import pandas as pd

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
    "gs_prime_positioning",
)


def _install_import_stubs() -> None:
    workspace = types.ModuleType("openclaw_workspace")

    def workspace_root(source: object) -> Path:
        del source
        return REPO_ROOT

    workspace.workspace_root = workspace_root
    sys.modules["openclaw_workspace"] = workspace

    class _Config:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    def _module(name: str, **attrs: object) -> None:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    _module(
        "us_bottom_framework",
        BottomFrameworkConfig=_Config,
        alert_projection_digest_lines=lambda *args, **kwargs: [],
        alert_projection_lead=lambda *args, **kwargs: "",
        bottom_environment_digest_line=lambda *args, **kwargs: "",
        build_bottom_environment=lambda *args, **kwargs: {},
        evaluate_bottom_framework=lambda *args, **kwargs: {},
        projection_digest_lines=lambda *args, **kwargs: [],
    )
    _module("us_bottom_environment_selftest", bottom_environment_checks=lambda: [])
    _module(
        "us_top_framework",
        TopFrameworkConfig=_Config,
        evaluate_top_framework=lambda *args, **kwargs: {},
        top_digest_lines=lambda *args, **kwargs: [],
    )
    _module("us_top_framework_selftest", top_framework_checks=lambda: [])
    _module(
        "gs_prime_positioning",
        evaluate_gs_prime_signals=lambda *args, **kwargs: {},
        render_gs_prime_section=lambda *args, **kwargs: "",
        update_gs_prime_store=lambda *args, **kwargs: {},
    )


def _load_cli():
    saved = {name: sys.modules.get(name) for name in _STUB_MODULES}
    sys.path.insert(0, str(SCRIPTS))
    try:
        _install_import_stubs()
        spec = importlib.util.spec_from_file_location("sentiment_dashboard_cli_obs_test", CLI_PATH)
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


CLI = _load_cli()


class _BottomCfg:
    deep_index_rsi = 20
    pressure_index_rsi = 30
    gsp_alert_rsi = 30
    qqq_alert_drawdown = -0.1
    pressure_vix = 25


class _TopCfg:
    window_td = 40
    excess_timeout_td = 60
    gsp_warn_rsi30 = 70
    quantum_warn_gain_pct = 100
    excess_qqq_rsi14 = 75
    excess_qqq_200dma_pct = 15
    excess_momentum_rsi = 75
    confirm_drawdown_pct = 5
    put_hedge_vvix_vix = 6.0
    put_hedge_cheap_vix = 20
    cool_drawdown_15_pct = -15
    weak_trend_premium_pct = 0


def _series(points: list[tuple[object, float]]) -> pd.Series:
    return pd.Series(
        [value for _, value in points],
        index=pd.DatetimeIndex([pd.Timestamp(stamp) for stamp, _ in points]),
        dtype=float,
    )


def _span(last_day: str, last_value: float, earlier_day: str, earlier_value: float) -> pd.Series:
    return _series([
        ("2026-10-01", earlier_value),
        (earlier_day, earlier_value),
        (last_day, last_value),
    ])


def _card(page: str, title: str) -> str:
    marker = f'<div class="metric-title">{title}</div>'
    start = page.index(marker)
    return page[start:page.index("</article>", start)]


def _bottom_page(framework: dict, environment: dict, **series: object) -> str:
    empty = pd.Series(dtype=float)
    return CLI.bottom_monitor_section(
        chart=CLI.chart,
        series_payload=CLI.series_payload,
        hline=CLI.hline,
        metric_card=CLI.metric_card,
        fmt=CLI.fmt,
        finite=CLI.finite,
        bottom_framework=framework,
        bottom_environment=environment,
        bottom_cfg=_BottomCfg(),
        cyc_rsi14=series.get("cyc", empty),
        last_cyc14=series.get("last_cyc14"),
        qqq_drawdown_63=series.get("dd", empty),
        spx_rsi14=series.get("spx", empty),
        qqq_rsi14=series.get("qqq", empty),
        vix_price=series.get("vix", empty),
        vvix_price=series.get("vvix", empty),
    )


def _bottom_framework(as_of: str, scenario_as_of: str | None = None) -> dict:
    return {
        "metrics": {"as_of": as_of},
        "stage": "无信号",
        "summary": "无活动信号",
        "window_remaining_td": None,
        "projection": {"scenario": {"available": scenario_as_of is not None, "as_of": scenario_as_of}},
        "alert_projection": {"scenario": {"available": False}},
    }


def _bottom_environment(as_of: str) -> dict:
    return {
        "as_of": as_of,
        "summary": "中性",
        "tone": "neutral",
        "breadth": {},
        "metrics": {},
    }


def _top_page(framework: dict, **series: object) -> str:
    empty = pd.Series(dtype=float)
    return CLI.top_monitor_section(
        chart=CLI.chart,
        series_payload=CLI.series_payload,
        hline=CLI.hline,
        metric_card=CLI.metric_card,
        fmt=CLI.fmt,
        finite=CLI.finite,
        top_framework=framework,
        top_cfg=_TopCfg(),
        cyc_rsi30=series.get("cyc", empty),
        last_cyc30=series.get("last_cyc30"),
        quantum_from_low_log=series.get("quantum", empty),
        qqq_price=series.get("qqq_price", empty),
        qqq_rsi14=series.get("qqq_rsi", empty),
        qqq_vs_200dma=series.get("qqq_200", empty),
        vvix_vix_series=series.get("vvix_vix", empty),
        momentum_rsi=series.get("momentum", empty),
        momentum_period=series.get("momentum_period", 14),
        spx_vs_20dma=series.get("spx_20", empty),
        qqq_vs_20dma=series.get("qqq_20", empty),
        vol_ratio_series=series.get("vol", empty),
        vol_ratio=series.get("vol_ratio"),
        vol_crush=0.85,
        momentum_chart="<article>momentum</article>",
    )


def test_intraday_cards_use_each_series_date() -> None:
    page = _bottom_page(
        _bottom_framework("2026-11-06", "2026-11-09"),
        _bottom_environment("2026-11-06"),
        spx=_span("2026-11-09", 43.1, "2026-11-06", 35.1),
        qqq=_span("2026-11-09", 39.0, "2026-11-06", 31.4),
        dd=_span("2026-11-09", -11.4, "2026-11-06", -13.6),
        vix=_span("2026-11-05", 18.2, "2026-10-15", 16.0),
        vvix=_span("2026-11-04", 92.0, "2026-10-15", 88.0),
        cyc=_span("2026-11-03", 28.0, "2026-10-15", 40.0),
    )
    spx = _card(page, "SPX RSI14")
    qqq = _card(page, "QQQ RSI14")
    drawdown = _card(page, "QQQ 63日回撤")
    assert ">43.1</div>" in spx
    assert "数据截至 2026-11-09（盘中）" in spx
    assert "35.1" not in spx
    assert ">39.0</div>" in qqq
    assert "数据截至 2026-11-09（盘中）" in qqq
    assert "31.4" not in qqq
    assert ">-11.4%</div>" in drawdown
    assert "数据截至 2026-11-09（盘中）" in drawdown
    assert "-13.6" not in drawdown
    assert "正式状态截至 2026-11-06" in _card(page, "当前阶段")
    vix = _card(page, "VIX")
    vvix = _card(page, "VVIX")
    cycle = _card(page, "周期股/防御股相对指数 RSI14")
    assert "数据截至 2026-11-05" in vix
    assert "盘中" not in vix
    assert "2026-11-09" not in vix
    assert "数据截至 2026-11-04" in vvix
    assert "盘中" not in vvix
    assert "数据截至 2026-11-03" in cycle
    assert "盘中" not in cycle
    assert "（2026-11-09 盘中）" in page


def test_after_close_same_date_is_not_intraday() -> None:
    page = _bottom_page(
        _bottom_framework("2026-11-09"),
        _bottom_environment("2026-11-09"),
        spx=_span("2026-11-09", 43.1, "2026-11-06", 35.1),
        qqq=_span("2026-11-09", 39.0, "2026-11-06", 31.4),
        dd=_span("2026-11-09", -11.4, "2026-11-06", -13.6),
    )
    assert "盘中" not in page
    assert "数据截至 2026-11-09" in _card(page, "SPX RSI14")
    assert "正式状态截至 2026-11-09" in _card(page, "当前阶段")


def test_empty_series_does_not_borrow_a_date() -> None:
    page = _bottom_page(
        _bottom_framework("2026-11-06", "2026-11-09"),
        _bottom_environment("2026-11-06"),
    )
    for title in ("SPX RSI14", "QQQ RSI14", "QQQ 63日回撤", "VIX", "VVIX"):
        card = _card(page, title)
        assert ">暂无</div>" in card
        assert "数据截至 —" in card
        assert "2026-11-06" not in card
        assert "2026-11-09" not in card
    assert "正式状态截至 2026-11-06" in _card(page, "当前阶段")


def test_missing_last_value_keeps_the_previous_observation_date() -> None:
    page = _bottom_page(
        _bottom_framework("2026-11-09"),
        _bottom_environment("2026-11-09"),
        spx=_series([
            ("2026-10-01", 30.0),
            ("2026-11-06", 41.5),
            ("2026-11-09", float("nan")),
        ]),
    )
    card = _card(page, "SPX RSI14")
    assert ">41.5</div>" in card
    assert "数据截至 2026-11-06" in card
    assert "盘中" not in card
    assert "2026-11-09" not in card


def test_clocked_timestamp_converts_to_new_york_and_date_only_keeps_wall_date() -> None:
    page = _bottom_page(
        _bottom_framework("2026-11-06"),
        _bottom_environment("2026-11-06"),
        vix=_series([
            (pd.Timestamp("2026-10-01", tz="UTC"), 16.0),
            (pd.Timestamp("2026-11-06 18:30", tz="UTC"), 18.2),
        ]),
        qqq=_series([
            (pd.Timestamp("2026-10-01", tz="UTC"), 30.0),
            (pd.Timestamp("2026-11-06", tz="UTC"), 31.4),
        ]),
    )
    vix = _card(page, "VIX")
    qqq = _card(page, "QQQ RSI14")
    assert ">18.2</div>" in vix
    assert "数据截至 2026-11-06（盘中）" in vix
    assert ">31.4</div>" in qqq
    assert "数据截至 2026-11-06" in qqq
    assert "盘中" not in qqq


def test_top_cards_keep_separate_dates_and_empty_series_use_official_dates() -> None:
    trading = _top_page(
        {
            "metrics": {"as_of": "2026-11-06"},
            "stage": "无信号",
            "summary": "无活动信号",
            "strength": "",
            "environment": {},
            "projection": {"scenario": {"available": True, "as_of": "2026-11-09"}},
            "put_hedge": {},
        },
        qqq_rsi=_span("2026-11-09", 39.0, "2026-11-06", 31.4),
        momentum=_span("2026-11-04", 62.0, "2026-10-15", 55.0),
        spx_20=_span("2026-11-08", 1.2, "2026-10-15", 0.4),
        qqq_20=_span("2026-11-09", -0.5, "2026-10-15", 0.2),
    )
    qqq = _card(trading, "QQQ RSI14")
    momentum = _card(trading, "Momentum RSI14")
    assert ">39.0</div>" in qqq
    assert "数据截至 2026-11-09（盘中）" in qqq
    assert "31.4" not in qqq
    assert ">62.0</div>" in momentum
    assert "数据截至 2026-11-04" in momentum
    assert "2026-11-09" not in momentum
    assert "盘中" not in momentum
    assert "（2026-11-08 盘中）" in trading
    assert "（2026-11-09 盘中）" in trading
    assert "正式状态截至 2026-11-06" in _card(trading, "顶部阶段")

    official = _top_page(
        {
            "metrics": {
                "as_of": "2026-11-06",
                "gsp_as_of": "2026-11-05",
                "gsp_rsi30": 70.2,
                "quantum_from_low_pct": 12,
                "qqq_rsi14": 31.4,
                "momentum_rsi": 60,
                "momentum_as_of": "2026-11-04",
            },
            "stage": "无信号",
            "summary": "无活动信号",
            "strength": "",
            "environment": {},
            "projection": {"scenario": {"available": False}},
            "put_hedge": {},
        },
        last_cyc30=70.2,
        vol_ratio=0.91,
    )
    assert "盘中" not in official
    qqq = _card(official, "QQQ RSI14")
    assert ">31.4</div>" in qqq
    assert "数据截至 2026-11-06" in qqq
    momentum = _card(official, "Momentum RSI14")
    assert ">60.0</div>" in momentum
    assert "数据截至 2026-11-04" in momentum
    assert "2026-11-06" not in momentum
    assert "数据截至 2026-11-05" in _card(official, "周期股/防御股相对指数 RSI30")
    assert ">12%</div>" in _card(official, "量子等权较60日低点")
    assert "数据截至 2026-11-06" in _card(official, "量子等权较60日低点")
    vol = _card(official, "QQQ 隐波 30D/60D")
    assert ">0.91</div>" in vol
    assert "数据截至 2026-11-06" in vol


_TURNOVER_CFG = {
    "cn_turnover": {
        "components": [
            {"name": "上证综指", "em_secid": "1.000001"},
            {"name": "深证综指", "em_secid": "0.399106"},
        ]
    }
}


def _note(page: str) -> str:
    marker = '<p class="note">'
    start = page.index(marker) + len(marker)
    return page[start:page.index("</p>", start)]


def _embedded_snapshot(page: str) -> dict:
    import html

    marker = 'data-vol="'
    start = page.index(marker) + len(marker)
    encoded = page[start:page.index('"', start)]
    payload = json.loads(html.unescape(encoded))
    return payload["snapshot"]


def _refresh() -> str:
    return f"盘中每 {CLI.FLOW_REFRESH_MIN} 分钟刷新。"


def test_nonempty_volume_baseline_keeps_the_original_sentence() -> None:
    days = [f"2026-09-{day:02d}" for day in range(1, 21)]
    snapshot = {
        "ok": True,
        "error": "",
        "days": days,
        "today": [["09:35", 1.25]],
        "prev": [["09:35", 1.1]],
        "median": [["09:35", 1.0]],
    }
    page = CLI.turnover_panel(snapshot, _TURNOVER_CFG)
    expected = (
        "上证综指＋深证综指合计成交额，09:30 起累计，单位万亿元。"
        "实线今天，灰线上一交易日，虚线前 20 个交易日同一时刻的中位数。"
        + _refresh()
    )
    assert _note(page) == expected
    assert _embedded_snapshot(page)["days"] == days
    assert _embedded_snapshot(page)["today"] == [["09:35", 1.25]]
    assert snapshot["today"] == [["09:35", 1.25]]


def test_missing_volume_note_does_not_invent_a_zero_baseline() -> None:
    failed = {
        "ok": False,
        "error": "接口失败",
        "days": [],
        "today": [],
        "prev": [],
        "median": [],
    }
    page = CLI.turnover_panel(failed, _TURNOVER_CFG)
    assert _note(page) == (
        "上证综指＋深证综指合计成交额，09:30 起累计，单位万亿元。"
        "量能数据缺失：接口失败。"
        + _refresh()
    )
    assert "data-vol=" in page
    assert "前 0 个交易日" not in page
    embedded = _embedded_snapshot(page)
    assert embedded["ok"] is False
    assert embedded["error"] == "接口失败"
    assert embedded["days"] == []
    assert embedded["today"] == []
    assert failed["today"] == []

    timed_out = {"ok": False, "error": "TimeoutError: timed out", "days": None}
    timeout_page = CLI.turnover_panel(timed_out, _TURNOVER_CFG)
    assert "量能数据缺失：TimeoutError: timed out。" in _note(timeout_page)
    assert "前 0" not in timeout_page
    assert _embedded_snapshot(timeout_page)["days"] is None

    empty_baseline = {"ok": True, "error": "", "days": [], "today": [["10:00", 0.8]], "median": []}
    baseline_page = CLI.turnover_panel(empty_baseline, _TURNOVER_CFG)
    assert _note(baseline_page) == (
        "上证综指＋深证综指合计成交额，09:30 起累计，单位万亿元。"
        "完整交易日参考数据缺失，没有可计算的前序中位数。"
        + _refresh()
    )
    assert "前 0" not in baseline_page
    assert _embedded_snapshot(baseline_page)["today"] == [["10:00", 0.8]]


def test_freshness_detail_and_page_script_do_not_say_zero_days() -> None:
    refresh = f"页面每{CLI.FLOW_REFRESH_MIN}分钟自刷新"
    assert CLI.turnover_freshness_detail({"ok": True, "days": ["2026-11-06"] * 20}) == (
        f"沪深两市累计成交额；基线取前20个完整交易日，{refresh}"
    )
    missing = CLI.turnover_freshness_detail({"ok": False, "error": "接口失败", "days": []})
    assert missing == f"沪深两市累计成交额；量能数据缺失，{refresh}"
    assert "前0" not in missing
    assert CLI.turnover_freshness_detail({"ok": True, "days": []}) == (
        f"沪深两市累计成交额；完整交易日参考数据缺失，{refresh}"
    )

    source = CLI_PATH.read_text(encoding="utf-8")
    assert "turnover_freshness_detail(turnover_snapshot)" in source
    assert "基线取前{len(turnover_snapshot.get('days') or [])}" not in source
    assert "历史参考数据缺失" in source
    assert "较前'+((s.days||[]).length)" not in source
    assert "function volFetch" in source
    assert "st.live&&st.live.length?st.live:(s.today||[])" in source
    vol_init = source.index("function volInit")
    assert "cnOpen(st.cfg)" in source[vol_init:vol_init + 600]
