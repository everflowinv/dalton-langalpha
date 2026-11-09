#!/usr/bin/env python3
"""US-market page groups for the sentiment dashboard."""

from __future__ import annotations

import math
from typing import Any, Callable

import pandas as pd


CYCLE_DEFENSIVE_LABEL = "周期股/防御股相对指数"
VVIX_WARNING = 160.0


MONITOR_CSS = (
    ".monitor-section{display:grid;gap:18px;padding:4px 0 26px;border-bottom:1px solid var(--line)}"
    ".monitor-section:last-child{border-bottom:0;padding-bottom:0}"
    ".monitor-head{border-left:4px solid var(--blue);padding:4px 0 4px 14px}"
    ".monitor-head-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}"
    ".monitor-head h2{margin:0;font-size:22px}"
    ".monitor-head p{margin:5px 0 0;color:var(--muted);font-size:13px}"
    ".monitor-env{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:5px 10px;font-size:12px;font-weight:650;background:#16273f;color:#cbd8ea}"
    ".monitor-env.hot{background:#3a2a15;border-color:#8a5f24;color:#ffc98a}"
    ".monitor-env.muted{opacity:.7}"
    ".monitor-top .monitor-head{border-left-color:var(--orange)}"
    ".monitor-state .monitor-head{border-left-color:var(--purple)}"
    ".monitor-group-title{margin:4px 0 -6px;color:#d9e4f5;font-size:17px}"
    ".bottom-cards{grid-template-columns:repeat(auto-fit,minmax(175px,1fr))}"
    ".monitor-chart-stack{display:grid;grid-template-columns:minmax(0,1fr);gap:14px}"
)


def _normalize_obs_timestamp(value: Any) -> tuple[pd.Timestamp | None, str | None, bool]:
    """Normalize an observation timestamp into a naive Timestamp, YYYY-MM-DD date string, and intraday flag."""
    if value is None or pd.isna(value):
        return None, None, False
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"—", "未知", "暂无"}:
            return None, None, False
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None, None, False
    if pd.isna(ts):
        return None, None, False
    has_clock = not (
        ts.hour == 0
        and ts.minute == 0
        and ts.second == 0
        and ts.microsecond == 0
        and ts.nanosecond == 0
    )
    if ts.tzinfo is not None:
        if has_clock:
            ts_et = ts.tz_convert("America/New_York")
            explicit_intraday = (ts_et.hour, ts_et.minute, ts_et.second) < (16, 15, 0)
            ts_naive = ts_et.tz_localize(None)
        else:
            explicit_intraday = False
            ts_naive = ts.tz_localize(None)
    else:
        explicit_intraday = has_clock and (ts.hour, ts.minute, ts.second) < (16, 15, 0)
        ts_naive = ts
    return ts_naive, ts_naive.date().isoformat(), explicit_intraday


def _obs_date_str(value: Any) -> str | None:
    _, date_str, _ = _normalize_obs_timestamp(value)
    return date_str


def _last_series_obs(
    series: pd.Series | None,
    finite: Callable[..., float | None],
) -> tuple[float | None, str | None, bool]:
    """Extract the latest finite value, its YYYY-MM-DD observation date, and explicit intraday flag."""
    if not isinstance(series, pd.Series) or series.empty:
        return None, None, False
    numeric = pd.to_numeric(series, errors="coerce")
    records: list[tuple[pd.Timestamp, int, float, str, bool]] = []
    for pos, (raw_idx, raw_val) in enumerate(numeric.items()):
        val = finite(raw_val)
        if val is None:
            continue
        sort_ts, date_str, explicit_intraday = _normalize_obs_timestamp(raw_idx)
        if sort_ts is None or date_str is None:
            continue
        records.append((sort_ts, pos, val, date_str, explicit_intraday))
    if not records:
        return None, None, False
    records.sort(key=lambda item: (item[0], item[1]))
    _, _, last_val, last_date, last_intraday = records[-1]
    return last_val, last_date, last_intraday


def _plot_series_since(
    series: pd.Series | None,
    finite: Callable[..., float | None],
    start: str = "2023-01-01",
) -> pd.Series:
    """Return a clean, tz-naive series sliced from ``start`` for chart rendering."""
    if not isinstance(series, pd.Series) or series.empty:
        return pd.Series(dtype=float)
    numeric = pd.to_numeric(series, errors="coerce")
    start_ts = pd.Timestamp(start)
    records: list[tuple[pd.Timestamp, int, float]] = []
    for pos, (raw_idx, raw_val) in enumerate(numeric.items()):
        val = finite(raw_val)
        if val is None:
            continue
        sort_ts, date_str, _ = _normalize_obs_timestamp(raw_idx)
        if sort_ts is None or date_str is None:
            continue
        day_ts = pd.Timestamp(date_str)
        if day_ts >= start_ts:
            records.append((day_ts, pos, val))
    if not records:
        return pd.Series(dtype=float)
    records.sort(key=lambda item: (item[0], item[1]))
    dedup: dict[pd.Timestamp, float] = {}
    for day_ts, _, val in records:
        dedup[day_ts] = val
    return pd.Series(list(dedup.values()), index=pd.DatetimeIndex(list(dedup.keys())), dtype=float)


def _is_intraday_obs(
    obs_date: str | None,
    formal_as_of: str | None,
    *,
    scenario_as_of: str | None = None,
    explicit_intraday: bool = False,
) -> bool:
    if not obs_date:
        return False
    if explicit_intraday:
        return True
    if scenario_as_of and obs_date == scenario_as_of and (not formal_as_of or obs_date > formal_as_of):
        return True
    if formal_as_of and obs_date > formal_as_of:
        return True
    return False


def _obs_meta(
    obs_date: str | None,
    formal_as_of: str | None,
    *,
    scenario_as_of: str | None = None,
    explicit_intraday: bool = False,
    prefix: str = "数据截至 ",
) -> str:
    if not obs_date:
        return f"{prefix}—"
    suffix = (
        "（盘中）"
        if _is_intraday_obs(
            obs_date,
            formal_as_of,
            scenario_as_of=scenario_as_of,
            explicit_intraday=explicit_intraday,
        )
        else ""
    )
    return f"{prefix}{obs_date}{suffix}"


def _obs_tag(
    obs_date: str | None,
    formal_as_of: str | None,
    *,
    scenario_as_of: str | None = None,
    explicit_intraday: bool = False,
) -> str:
    if not obs_date:
        return ""
    if _is_intraday_obs(
        obs_date,
        formal_as_of,
        scenario_as_of=scenario_as_of,
        explicit_intraday=explicit_intraday,
    ):
        return f"（{obs_date} 盘中）"
    return f"（{obs_date}）"


def bottom_monitor_section(
    *,
    chart: Callable[..., str],
    series_payload: Callable[..., dict[str, Any]],
    hline: Callable[..., dict[str, Any]],
    metric_card: Callable[..., str],
    fmt: Callable[..., str],
    finite: Callable[..., float | None],
    bottom_framework: dict[str, Any],
    bottom_environment: dict[str, Any],
    bottom_cfg: Any,
    cyc_rsi14: pd.Series,
    last_cyc14: float | None,
    qqq_drawdown_63: pd.Series,
    spx_rsi14: pd.Series,
    qqq_rsi14: pd.Series,
    vix_price: pd.Series,
    vvix_price: pd.Series,
) -> str:
    metrics = bottom_framework.get("metrics") or {}
    formal_as_of = _obs_date_str(metrics.get("as_of"))
    env_as_of = _obs_date_str(bottom_environment.get("as_of")) or formal_as_of
    proj_scenario = (bottom_framework.get("projection") or {}).get("scenario") or {}
    alert_scenario = (bottom_framework.get("alert_projection") or {}).get("scenario") or {}
    scenario_as_of = (
        _obs_date_str(proj_scenario.get("as_of"))
        if proj_scenario.get("available")
        else _obs_date_str(alert_scenario.get("as_of"))
        if alert_scenario.get("available")
        else None
    )

    cyc_val, cyc_date, cyc_intraday = _last_series_obs(cyc_rsi14, finite)
    if cyc_val is None:
        cyc_val = finite(last_cyc14)
        if cyc_val is not None:
            cyc_date = _obs_date_str(metrics.get("gsp_as_of"))
    qqq_dd, qqq_dd_date, qqq_dd_intraday = _last_series_obs(qqq_drawdown_63, finite)
    vix, vix_date, vix_intraday = _last_series_obs(vix_price, finite)
    vvix, vvix_date, vvix_intraday = _last_series_obs(vvix_price, finite)
    spx_rsi, spx_rsi_date, spx_rsi_intraday = _last_series_obs(spx_rsi14, finite)
    qqq_rsi, qqq_rsi_date, qqq_rsi_intraday = _last_series_obs(qqq_rsi14, finite)

    breadth = bottom_environment.get("breadth") or {}
    sector_above = breadth.get("sector_above_200dma")
    sector_expected = breadth.get("sector_expected") or 11
    sector_text = "暂无" if sector_above is None else f"{int(sector_above)}/{int(sector_expected)}"
    semi_pct = finite(breadth.get("semiconductor_above_200dma_pct"))
    breadth_text = f"中期广度：行业{sector_text}；半导体{fmt(semi_pct,1,'%')}"
    environment_metrics = bottom_environment.get("metrics") or {}

    def signed_pct(value: Any) -> str:
        number = finite(value)
        return "暂无" if number is None else f"{number:+.1f}%"

    below_days = environment_metrics.get("both_below_200dma_consecutive_td")
    below_days_text = "暂无" if below_days is None else f"{int(below_days)}日"
    dma_text = (
        f"均线：SPX距200DMA {signed_pct(environment_metrics.get('spx_vs_200dma_pct'))}；"
        f"QQQ距200DMA {signed_pct(environment_metrics.get('qqq_vs_200dma_pct'))}；"
        f"SPX 50DMA较200DMA {signed_pct(environment_metrics.get('spx_ma50_vs_ma200_pct'))}；"
        f"双指数连续低于200DMA {below_days_text}"
    )
    environment_meta = (
        f"{dma_text}；{breadth_text}；正式状态截至 {env_as_of}"
        if env_as_of
        else f"{dma_text}；{breadth_text}"
    )

    def rsi_tone(value: float | None) -> str:
        if value is None:
            return "muted"
        if value < bottom_cfg.deep_index_rsi:
            return "cold"
        if value <= bottom_cfg.pressure_index_rsi:
            return "cool"
        return "neutral"

    stage = str(bottom_framework.get("stage") or "无信号")
    stage_tone = "cold" if stage in {"预警", "压力", "深度超卖"} else "warm" if stage == "确认" else "neutral"
    remaining = bottom_framework.get("window_remaining_td")
    stage_window = (
        f"压力观察窗剩余：{remaining}个交易日"
        if remaining is not None
        else "等待5DMA确认，不受20日窗限制"
        if stage in {"压力", "深度超卖"}
        else "无活动压力观察窗"
    )
    stage_meta = f"{stage_window}；正式状态截至 {formal_as_of}" if formal_as_of else stage_window

    cyc_meta = _obs_meta(cyc_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=cyc_intraday)
    qqq_dd_meta = _obs_meta(qqq_dd_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_dd_intraday)
    vix_meta = f"进入压力条件；{_obs_meta(vix_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=vix_intraday)}"
    vvix_meta = (
        "历史样本中罕见，命中大底率100%；触发即提示抄底；"
        f"{_obs_meta(vvix_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=vvix_intraday)}"
    )
    spx_rsi_meta = _obs_meta(spx_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=spx_rsi_intraday)
    qqq_rsi_meta = _obs_meta(qqq_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_rsi_intraday)

    cyc_tag = _obs_tag(cyc_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=cyc_intraday)
    qqq_dd_tag = _obs_tag(qqq_dd_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_dd_intraday)
    spx_rsi_tag = _obs_tag(spx_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=spx_rsi_intraday)
    qqq_rsi_tag = _obs_tag(qqq_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_rsi_intraday)

    cards = "".join([
        metric_card("当前阶段", stage, str(bottom_framework.get("summary") or ""), stage_tone, stage_meta),
        metric_card("环境状态", str(bottom_environment.get("summary") or "环境数据不完整"), "", str(bottom_environment.get("tone") or "muted"), environment_meta),
        metric_card(f"{CYCLE_DEFENSIVE_LABEL} RSI14", fmt(cyc_val, 1), f"预警线 ≤{bottom_cfg.gsp_alert_rsi:.0f}", "cold" if cyc_val is not None and cyc_val <= bottom_cfg.gsp_alert_rsi else "neutral", cyc_meta),
        metric_card("QQQ 63日回撤", fmt(qqq_dd, 1, "%"), f"预警线 ≤{bottom_cfg.qqq_alert_drawdown * 100:.0f}%", "cold" if qqq_dd is not None and qqq_dd <= bottom_cfg.qqq_alert_drawdown * 100 else "neutral", qqq_dd_meta),
        metric_card("VIX", fmt(vix, 1), f"压力线 ≥{bottom_cfg.pressure_vix:.0f}", "cool" if vix is not None and vix >= bottom_cfg.pressure_vix else "neutral", vix_meta),
        metric_card("VVIX", fmt(vvix, 1), f"警戒线 ≥{VVIX_WARNING:.0f}", "cold" if vvix is not None and vvix >= VVIX_WARNING else "neutral", vvix_meta),
        metric_card("SPX RSI14", fmt(spx_rsi, 1), f"压力区 ≤{bottom_cfg.pressure_index_rsi:.0f}；深度超卖 <{bottom_cfg.deep_index_rsi:.0f}", rsi_tone(spx_rsi), spx_rsi_meta),
        metric_card("QQQ RSI14", fmt(qqq_rsi, 1), f"压力区 ≤{bottom_cfg.pressure_index_rsi:.0f}；深度超卖 <{bottom_cfg.deep_index_rsi:.0f}", rsi_tone(qqq_rsi), qqq_rsi_meta),
    ])
    charts = '<div class="monitor-chart-stack">' + "".join([
        chart(
            f"预警：{CYCLE_DEFENSIVE_LABEL} RSI14",
            [series_payload(_plot_series_since(cyc_rsi14, finite), f"{CYCLE_DEFENSIVE_LABEL} RSI14", "#ffbf3c", "left", True, 1000)],
            f"预警线≤{bottom_cfg.gsp_alert_rsi:.0f}；当前{fmt(cyc_val,1)}{cyc_tag}。",
            None, 300, [0, 100], False,
            [hline(bottom_cfg.gsp_alert_rsi, "left", f"预警{bottom_cfg.gsp_alert_rsi:.0f}", "cold")],
        ),
        chart(
            "预警：QQQ 63日回撤",
            [series_payload(_plot_series_since(qqq_drawdown_63, finite), "距63日高点", "#4da3ff", "left", True, 1000)],
            f"预警线≤{bottom_cfg.qqq_alert_drawdown * 100:.0f}%；当前{fmt(qqq_dd,1,'%')}{qqq_dd_tag}。",
            None, 300, None, False,
            [hline(bottom_cfg.qqq_alert_drawdown * 100, "left", f"预警{bottom_cfg.qqq_alert_drawdown * 100:.0f}%", "cold"), hline(0, "left", "高点", "zero")],
        ),
        chart(
            "压力参考：VIX 与 VVIX",
            [
                series_payload(_plot_series_since(vix_price, finite), "VIX", "#ff6b6b", "left", True, 1000),
                series_payload(_plot_series_since(vvix_price, finite), "VVIX（右轴）", "#a377ff", "right", False, 1000),
            ],
            f"VIX压力线≥{bottom_cfg.pressure_vix:.0f}；VVIX警戒线≥{VVIX_WARNING:.0f}。历史样本中VVIX触发罕见，命中大底率100%；触发即提示抄底，但不进入状态机。",
            None, 300, None, False,
            [
                hline(bottom_cfg.pressure_vix, "left", f"压力{bottom_cfg.pressure_vix:.0f}", "cold"),
                hline(VVIX_WARNING, "right", f"VVIX警戒{VVIX_WARNING:.0f}", "cold"),
            ],
        ),
        chart(
            "压力与深度超卖：SPX、QQQ RSI14",
            [
                series_payload(_plot_series_since(spx_rsi14, finite), "SPX RSI14", "#4da3ff", "left", False, 1000),
                series_payload(_plot_series_since(qqq_rsi14, finite), "QQQ RSI14", "#ffbf3c", "left", False, 1000),
            ],
            f"SPX、QQQ RSI14；压力区 ≤{bottom_cfg.pressure_index_rsi:.0f}，深度超卖 <{bottom_cfg.deep_index_rsi:.0f}；当前{fmt(spx_rsi,1)}{spx_rsi_tag}、{fmt(qqq_rsi,1)}{qqq_rsi_tag}。",
            None, 300, [0, 100], False,
            [hline(bottom_cfg.pressure_index_rsi, "left", f"压力{bottom_cfg.pressure_index_rsi:.0f}", "cold"), hline(bottom_cfg.deep_index_rsi, "left", f"深度超卖<{bottom_cfg.deep_index_rsi:.0f}", "hot")],
        ),
    ]) + "</div>"
    return (
        '<section class="monitor-section monitor-bottom"><div class="monitor-head"><h2>底部监控</h2>'
        '<p>先看环境状态，再看预警、压力、深度超卖和走出底部；环境标签与中期广度不改变状态机。</p></div>'
        f'<section class="cards bottom-cards">{cards}</section>{charts}</section>'
    )


def _env_reasons(environment: dict[str, Any], top_cfg: Any, signed: Callable[..., str]) -> str:
    """预警环境卡的小字：强弱都写两项原因（冷却期、趋势线），带当前读数。"""
    cooldown = environment.get("cooldown") or {}
    trough_date = cooldown.get("trough_date")
    trough_dd = cooldown.get("trough_drawdown_pct")
    trough_text = f"{trough_date} 最低点跌{abs(float(trough_dd)):.1f}%" if trough_date and trough_dd is not None else ""
    if cooldown.get("active"):
        cool_part = f"冷却期剩余{cooldown.get('remaining_td')}个交易日" + (f"（{trough_text}）" if trough_text else "")
    else:
        cool_part = "不在冷却期" + (f"（{trough_text}，未到{abs(top_cfg.cool_drawdown_15_pct):.0f}%）" if trough_text else "")
    trend = environment.get("trend_premium_pct")
    if trend is None:
        trend_part = "SPX 趋势线读数暂无"
    elif float(trend) < top_cfg.weak_trend_premium_pct:
        trend_part = f"SPX 低于趋势线 {signed(trend)}"
    else:
        trend_part = f"SPX 高于趋势线 {signed(trend)}"
    return "＋".join([cool_part, trend_part])


def top_monitor_section(
    *,
    chart: Callable[..., str],
    series_payload: Callable[..., dict[str, Any]],
    hline: Callable[..., dict[str, Any]],
    metric_card: Callable[..., str],
    fmt: Callable[..., str],
    finite: Callable[..., float | None],
    top_framework: dict[str, Any],
    top_cfg: Any,
    cyc_rsi30: pd.Series,
    last_cyc30: float | None,
    quantum_from_low_log: pd.Series,
    qqq_price: pd.Series,
    qqq_rsi14: pd.Series,
    qqq_vs_200dma: pd.Series,
    vvix_vix_series: pd.Series,
    momentum_rsi: pd.Series,
    momentum_period: int | None,
    spx_vs_20dma: pd.Series,
    qqq_vs_20dma: pd.Series,
    vol_ratio_series: pd.Series,
    vol_ratio: float | None,
    vol_crush: float,
    momentum_chart: str,
) -> str:
    metrics = top_framework.get("metrics") or {}
    formal_as_of = _obs_date_str(metrics.get("as_of"))
    proj_scenario = (top_framework.get("projection") or {}).get("scenario") or {}
    scenario_as_of = _obs_date_str(proj_scenario.get("as_of")) if proj_scenario.get("available") else None

    environment = top_framework.get("environment") or {}
    stage = str(top_framework.get("stage") or "无信号")
    strength = str(top_framework.get("strength") or "")
    stage_tone = "hot" if stage in {"过激", "确认"} else "warm" if stage == "预警" else "neutral"
    window_remaining = top_framework.get("window_remaining_td")
    timeout_remaining = top_framework.get("excess_timeout_remaining_td")
    stage_window = (
        f"活动窗剩余：{window_remaining}个交易日（T+{top_cfg.window_td}，同时检查过激与确认）"
        if window_remaining is not None
        else f"过激超时剩余：{timeout_remaining}个交易日（{top_cfg.excess_timeout_td}日）"
        if timeout_remaining is not None
        else "等待任一指数20日收盘新高失效"
        if stage == "确认"
        else "无活动观察窗"
    )
    stage_meta = f"{stage_window}；正式状态截至 {formal_as_of}" if formal_as_of else stage_window
    stage_value = f"{stage}（{strength}）" if strength and stage in {"预警", "过激", "确认"} else stage

    def signed(value: Any, digits: int = 1, suffix: str = "%") -> str:
        number = finite(value)
        return "暂无" if number is None else f"{number:+.{digits}f}{suffix}"

    env_tone = "cool" if environment.get("weak_now") else "neutral"
    env_value = "弱" if environment.get("weak_now") else "强"
    env_sub = _env_reasons(environment, top_cfg, signed)
    env_meta = f"正式状态截至 {formal_as_of}" if formal_as_of else ""

    gsp_now, gsp_date, gsp_intraday = _last_series_obs(cyc_rsi30, finite)
    if gsp_now is None:
        gsp_now = finite(last_cyc30) if finite(last_cyc30) is not None else finite(metrics.get("gsp_rsi30"))
        if gsp_now is not None:
            gsp_date = _obs_date_str(metrics.get("gsp_as_of"))

    q_log, q_date, q_intraday = _last_series_obs(quantum_from_low_log, finite)
    if q_log is not None:
        quantum_gain: float | None = (math.exp(q_log / 100.0) - 1.0) * 100.0
    else:
        quantum_gain = finite(metrics.get("quantum_from_low_pct"))
        q_date = formal_as_of if quantum_gain is not None else None
        q_intraday = False

    def official_if_missing(
        value: float | None,
        obs_date: str | None,
        intraday: bool,
        metric_value: Any,
        metric_date: Any,
    ) -> tuple[float | None, str | None, bool]:
        if value is not None:
            return value, obs_date, intraday
        fallback = finite(metric_value)
        if fallback is None:
            return None, None, False
        return fallback, _obs_date_str(metric_date), False

    qqq_rsi, qqq_rsi_date, qqq_rsi_intraday = official_if_missing(
        *_last_series_obs(qqq_rsi14, finite), metrics.get("qqq_rsi14"), formal_as_of,
    )
    qqq_200, qqq_200_date, qqq_200_intraday = official_if_missing(
        *_last_series_obs(qqq_vs_200dma, finite), metrics.get("qqq_vs_200dma_pct"), formal_as_of,
    )
    vvix_ratio, vvix_ratio_date, vvix_ratio_intraday = official_if_missing(
        *_last_series_obs(vvix_vix_series, finite), metrics.get("vvix_vix"), formal_as_of,
    )
    mom_now, mom_date, mom_intraday = official_if_missing(
        *_last_series_obs(momentum_rsi, finite), metrics.get("momentum_rsi"), metrics.get("momentum_as_of"),
    )
    spx_20, spx_20_date, spx_20_intraday = official_if_missing(
        *_last_series_obs(spx_vs_20dma, finite), metrics.get("spx_vs_20dma_pct"), formal_as_of,
    )
    qqq_20, qqq_20_date, qqq_20_intraday = official_if_missing(
        *_last_series_obs(qqq_vs_20dma, finite), metrics.get("qqq_vs_20dma_pct"), formal_as_of,
    )
    vol_val, vol_date, vol_intraday = _last_series_obs(vol_ratio_series, finite)
    if vol_val is None:
        vol_val = finite(vol_ratio)
        vol_date = formal_as_of if vol_val is not None else None
        vol_intraday = False

    gsp_meta = _obs_meta(gsp_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=gsp_intraday)
    q_meta = f"IONQ/RGTI/QBTS/QUBT 日等权再平衡；{_obs_meta(q_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=q_intraday)}"
    qqq_rsi_meta = _obs_meta(qqq_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_rsi_intraday)
    qqq_200_meta = _obs_meta(qqq_200_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_200_intraday)
    mom_meta = f"GSCBHMOM，{_obs_meta(mom_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=mom_intraday)}"
    vol_meta = (
        "只作环境：比值塌陷时把在亮的预警或过激看得更重，不进状态机；"
        f"{_obs_meta(vol_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=vol_intraday)}"
    )

    gsp_tag = _obs_tag(gsp_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=gsp_intraday)
    q_tag = _obs_tag(q_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=q_intraday)
    qqq_rsi_tag = _obs_tag(qqq_rsi_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_rsi_intraday)
    qqq_200_tag = _obs_tag(qqq_200_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_200_intraday)
    spx_20_tag = _obs_tag(spx_20_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=spx_20_intraday)
    qqq_20_tag = _obs_tag(qqq_20_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=qqq_20_intraday)
    vol_tag = _obs_tag(vol_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=vol_intraday)
    vvix_ratio_tag = _obs_tag(vvix_ratio_date, formal_as_of, scenario_as_of=scenario_as_of, explicit_intraday=vvix_ratio_intraday)

    round_high_pct = metrics.get("qqq_vs_round_high_pct")
    put_hedge = top_framework.get("put_hedge") or {}
    hedge_stats = put_hedge.get("stats") or {}
    hedge_on = bool(put_hedge.get("on"))
    # The formal reminder, VIX, and put cost share put_hedge.as_of.
    hedge_as_of = _obs_date_str(put_hedge.get("as_of"))
    official_note = f"正式提醒截至 {hedge_as_of or '—'}"
    hedge_sub = (
        f"VVIX/VIX {fmt(vvix_ratio, 2)}{vvix_ratio_tag}（提醒线 ≥{top_cfg.put_hedge_vvix_vix:.1f}）；"
        f"{official_note}：VIX {fmt(put_hedge.get('vix'), 1)}，1月平值put≈{fmt(put_hedge.get('put_cost_pct_1m_atm'), 1, '%')}名义"
    )
    hedge_eval = hedge_stats.get("segments_evaluated") or 0
    hedge_rate10 = hedge_stats.get("control_dd_ge_10pct_rate")
    hedge_meta = (
        f"{str(hedge_stats.get('start') or '')[:4]}年起{hedge_stats.get('segments')}段：起点后{hedge_stats.get('horizon_td')}日QQQ回撤≥10%的"
        f"{hedge_stats.get('segments_dd_ge_10pct')}/{hedge_eval}段（VIX<{top_cfg.put_hedge_cheap_vix:.0f}对照日{fmt(None if hedge_rate10 is None else hedge_rate10 * 100, 0, '%')}）；"
        f"买便宜尾部保险的窗口，不是顶部信号，不进状态机；{official_note}"
        if hedge_eval
        else f"历史样本不足；买便宜尾部保险的窗口，不是顶部信号，不进状态机；{official_note}"
    )
    confirm_value = signed(round_high_pct) if round_high_pct is not None else "无活动轮次"
    confirm_tone = "hot" if round_high_pct is not None and round_high_pct <= -top_cfg.confirm_drawdown_pct else "neutral"
    confirm_meta = (
        f"SPX距20日线 {signed(spx_20)}{spx_20_tag}；"
        f"QQQ距20日线 {signed(qqq_20)}{qqq_20_tag}；"
        f"本轮最高收盘 {metrics.get('round_high_date') or '—'}"
        + (f"；正式状态截至 {formal_as_of}" if formal_as_of else "")
    )

    def hot_if(value: Any, threshold: float) -> str:
        number = finite(value)
        return "hot" if number is not None and number >= threshold else "neutral"

    cards = "".join([
        metric_card("顶部阶段", stage_value, str(top_framework.get("summary") or ""), stage_tone, stage_meta),
        metric_card("预警环境", env_value, env_sub, env_tone, env_meta),
        metric_card(f"{CYCLE_DEFENSIVE_LABEL} RSI30", fmt(gsp_now, 1), f"预警线 ≥{top_cfg.gsp_warn_rsi30:.0f}", hot_if(gsp_now, top_cfg.gsp_warn_rsi30), gsp_meta),
        metric_card("量子等权较60日低点", fmt(quantum_gain, 0, "%"), f"预警线 ≥+{top_cfg.quantum_warn_gain_pct:.0f}%", hot_if(quantum_gain, top_cfg.quantum_warn_gain_pct), q_meta),
        metric_card("QQQ RSI14", fmt(qqq_rsi, 1), f"过激线 ≥{top_cfg.excess_qqq_rsi14:.0f}", hot_if(qqq_rsi, top_cfg.excess_qqq_rsi14), qqq_rsi_meta),
        metric_card("QQQ 距200DMA", signed(qqq_200), f"过激线 ≥+{top_cfg.excess_qqq_200dma_pct:.0f}%", hot_if(qqq_200, top_cfg.excess_qqq_200dma_pct), qqq_200_meta),
        metric_card(f"Momentum RSI{momentum_period or ''}", fmt(mom_now, 1), f"过激线 ≥{top_cfg.excess_momentum_rsi:.0f}", hot_if(mom_now, top_cfg.excess_momentum_rsi), mom_meta),
        metric_card("确认进度：QQQ较本轮最高收盘", confirm_value, f"确认需 ≤-{top_cfg.confirm_drawdown_pct:.0f}% 且双指数同日收在20日线下", confirm_tone, confirm_meta),
        metric_card("QQQ 隐波 30D/60D", fmt(vol_val, 2), f"环境线 ≤{vol_crush:.2f}", "hot" if vol_val is not None and vol_val <= vol_crush else "neutral", vol_meta),
        metric_card("指数 put 保护提醒（VVIX/VIX）", "提醒" if hedge_on else "无", hedge_sub, "warm" if hedge_on else "neutral", hedge_meta),
    ])
    doubling = 100.0 * math.log(1.0 + top_cfg.quantum_warn_gain_pct / 100.0)
    charts = '<div class="monitor-chart-stack">' + "".join([
        chart(
            f"预警：{CYCLE_DEFENSIVE_LABEL} RSI30",
            [series_payload(_plot_series_since(cyc_rsi30, finite), f"{CYCLE_DEFENSIVE_LABEL} RSI30", "#f6f7fb", "left", True, 1000)],
            f"预警线≥{top_cfg.gsp_warn_rsi30:g}；当前{fmt(gsp_now,1)}{gsp_tag}。",
            None, 300, [0, 100], False,
            [hline(top_cfg.gsp_warn_rsi30, "left", f"预警{top_cfg.gsp_warn_rsi30:g}", "hot")],
        ),
        chart(
            "预警：量子等权较60日低点涨幅（对数）",
            [series_payload(_plot_series_since(quantum_from_low_log, finite), "ln(等权指数÷60日最低)×100", "#a377ff", "left", True, 1000)],
            f"左轴ln(等权指数÷60个交易日最低)×100，翻倍线{doubling:.1f}；当前较60日低点{fmt(quantum_gain,0,'%')}{q_tag}。",
            None, 300, None, False,
            [hline(doubling, "left", f"翻倍+{top_cfg.quantum_warn_gain_pct:.0f}%", "hot"), hline(0, "left", "60日低点", "zero")],
        ),
        chart(
            "过激：QQQ RSI14 与距200DMA",
            [
                series_payload(_plot_series_since(qqq_rsi14, finite), "QQQ RSI14", "#ffbf3c", "left", False, 1000),
                series_payload(_plot_series_since(qqq_vs_200dma, finite), "QQQ 距200DMA %（右轴）", "#4da3ff", "right", False, 1000),
            ],
            f"左轴RSI14（过激≥{top_cfg.excess_qqq_rsi14:.0f}），右轴距200DMA（过激≥+{top_cfg.excess_qqq_200dma_pct:.0f}%）；当前{fmt(qqq_rsi,1)}{qqq_rsi_tag}、{signed(qqq_200)}{qqq_200_tag}。",
            [-25, 45], 320, [0, 100], False,
            [hline(top_cfg.excess_qqq_rsi14, "left", f"RSI{top_cfg.excess_qqq_rsi14:.0f}", "hot"), hline(top_cfg.excess_qqq_200dma_pct, "right", f"200DMA+{top_cfg.excess_qqq_200dma_pct:.0f}%", "hot")],
        ),
        momentum_chart,  # 过激：Momentum RSI 与 NDX/SPW RSI14，2026-09-04 从状态监控搬来（用户要求）
        chart(
            "确认与失效：SPX、QQQ 距20日线",
            [
                series_payload(_plot_series_since(spx_vs_20dma, finite), "SPX 距20DMA %", "#4da3ff", "left", False, 1000),
                series_payload(_plot_series_since(qqq_vs_20dma, finite), "QQQ 距20DMA %", "#ffbf3c", "left", False, 1000),
            ],
            f"两条都收在0以下且QQQ较本轮最高跌≥{top_cfg.confirm_drawdown_pct:.0f}%才确认；当前SPX {signed(spx_20)}{spx_20_tag}、QQQ {signed(qqq_20)}{qqq_20_tag}。",
            None, 300, None, False, [hline(0, "left", "20日线", "zero")],
        ),
        chart(
            "QQQ 隐波 30D/60D（叠加QQQ）",
            [
                series_payload(_plot_series_since(vol_ratio_series, finite), "30D/60D 比值", "#a377ff", "left", True, 1000),
                series_payload(_plot_series_since(qqq_price, finite), "QQQ 收盘价（右轴）", "#4da3ff", "right", False, 1000),
            ],
            f"左轴30D/60D比值，右轴QQQ收盘价；≤{vol_crush:.2f}为波动率塌陷（只作环境），当前{fmt(vol_val,2)}{vol_tag}。",
            None, 340, None, False,
            [hline(vol_crush, "left", f"塌陷{vol_crush:.2f}", "hot"), hline(1.0, "left", "倒挂1.00", "zero")],
        ),
        chart(
            "指数 put 保护提醒：VVIX/VIX（叠加QQQ）",
            [
                series_payload(_plot_series_since(vvix_vix_series, finite), "VVIX/VIX", "#a377ff", "left", False, 1000),
                series_payload(_plot_series_since(qqq_price, finite), "QQQ 收盘（右轴）", "#4da3ff", "right", False, 1000),
            ],
            f"左轴VVIX/VIX（提醒线≥{top_cfg.put_hedge_vvix_vix:.1f}，过线每天提醒），右轴QQQ收盘；和状态机分离，当前{fmt(vvix_ratio,2)}{vvix_ratio_tag}。",
            None, 340, [2, 11], False,
            [hline(top_cfg.put_hedge_vvix_vix, "left", f"提醒{top_cfg.put_hedge_vvix_vix:.1f}", "hot")],
        ),
    ]) + "</div>"
    stage_pill = f'<span class="monitor-env {"hot" if top_framework.get("active") else "muted"}">顶部阶段：{stage_value}</span>'
    env_pill = f'<span class="monitor-env {"hot" if environment.get("weak_now") else ""}">预警环境：{env_value}</span>'
    hedge_pill = f'<span class="monitor-env {"hot" if hedge_on else "muted"}">指数put保护：{"提醒" if hedge_on else "无"}</span>'
    return (
        '<section class="monitor-section monitor-top"><div class="monitor-head">'
        f'<div class="monitor-head-row"><h2>顶部监控</h2><span>{stage_pill} {env_pill} {hedge_pill}</span></div>'
        '<p>预警开 T+40 活动窗，期间每天同时检查过激与转跌确认；过激三条（QQQ RSI14、距 200DMA、Momentum RSI）不是确认的前置条件，成立后把确认等待期延长到 60 个交易日。确认后等任一指数 20 日收盘新高失效；弱预警只挂标签，隐波比只作环境。VVIX/VIX 不进状态机，单独做指数 put 保护提醒，过线每天提醒。</p></div>'
        f'<section class="cards bottom-cards">{cards}</section>{charts}</section>'
    )


def state_monitor_section(
    *,
    spx_trend_chart: str,
    gs_prime_html: str = "",
    us_style_charts: str,
    macro_html: str,
    consensus_table: str,
    valuation_table: str,
    model_note: str,
    valuation_note: str,
) -> str:
    # 风格走势图保持单列；S&P 一级行业 ETF 在 us_style_charts 内自带两列网格。
    style_charts = us_style_charts
    return (
        '<section class="monitor-section monitor-state"><div class="monitor-head"><h2>状态监控</h2>'
        '<p>趋势、高盛 Prime 对冲基金仓位、风格轮动、宏观和估值放在这里，用来判断市场所处环境；Momentum 图已并入顶部监控的过激段。</p></div>'
        + spx_trend_chart + gs_prime_html + style_charts
        + '<h3 class="monitor-group-title">宏观与估值</h3><div class="grid two">' + macro_html
        + '</div><div class="grid two"><article class="panel"><h3>SPX长期趋势模型输入</h3>'
        + consensus_table + f'<p class="note">{model_note}</p></article>'
        + f'<article class="panel"><h3>估值快照</h3>{valuation_table}<p class="note">{valuation_note}</p></article></div></section>'
    )
