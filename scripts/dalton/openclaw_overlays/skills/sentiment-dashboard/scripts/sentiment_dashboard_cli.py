#!/usr/bin/env python3
"""Build a self-contained multi-tab market sentiment dashboard."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import io
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from openclaw_workspace import workspace_root as resolve_workspace

from typing import Any, Iterable

import numpy as np
import pandas as pd

from us_bottom_framework import BottomFrameworkConfig, alert_projection_digest_lines, alert_projection_lead, bottom_environment_digest_line, build_bottom_environment, evaluate_bottom_framework, projection_digest_lines
from us_bottom_environment_selftest import bottom_environment_checks as bottom_environment_checks
from us_top_framework import TopFrameworkConfig, evaluate_top_framework, top_digest_lines
from us_top_framework_selftest import top_framework_checks as top_framework_checks
from us_market_sections import MONITOR_CSS, bottom_monitor_section, state_monitor_section, top_monitor_section
from gs_prime_positioning import evaluate_gs_prime_signals, render_gs_prime_section, update_gs_prime_store


def find_workspace(start: Path) -> Path:
    return resolve_workspace(start / "SKILL.md")


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
WORKSPACE = find_workspace(SKILL_DIR)
CONFIG_PATH = SKILL_DIR / "references" / "dashboard_config.json"
SEED_PATH = SKILL_DIR / "references" / "spx_model_seed.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def finite(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI with the original SMA seed and recursive smoothing."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= period:
        return result
    avg_gain = float(gain.iloc[1 : period + 1].mean())
    avg_loss = float(loss.iloc[1 : period + 1].mean())

    def calculate(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    result.iloc[period] = calculate(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + float(gain.iloc[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(loss.iloc[i])) / period
        result.iloc[i] = calculate(avg_gain, avg_loss)
    return result


def equal_weight_index(prices: dict[str, pd.Series], members: list[str]) -> pd.Series:
    """Daily-rebalanced equal-weight index (base 100) from split/dividend-adjusted closes.

    Returns an empty series unless every member has data, so a missing download
    fails the chart instead of silently reweighting the basket. The series starts
    on the first date all members quote, which is the latest IPO date.
    """
    if not members or any(m not in prices or prices[m].empty for m in members):
        return pd.Series(dtype=float)
    frame = pd.concat({m: prices[m] for m in members}, axis=1).dropna()
    if len(frame) < 2:
        return pd.Series(dtype=float)
    daily = frame.pct_change().mean(axis=1)
    daily.iloc[0] = 0.0
    return 100 * (1 + daily).cumprod()


def pct_change(series: pd.Series, periods: int) -> float | None:
    clean = series.dropna()
    if len(clean) <= periods:
        return None
    return finite((clean.iloc[-1] / clean.iloc[-periods - 1] - 1) * 100)


def fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    x = finite(value)
    return "暂无" if x is None else f"{x:,.{digits}f}{suffix}"


def signed_fmt(value: Any, digits: int = 1, suffix: str = "%") -> str:
    """带正负号的纯文本，给飞书卡片用；direction_fmt 带 HTML 标签，进不了卡片。"""
    x = finite(value)
    return "暂无" if x is None else f"{x:+,.{digits}f}{suffix}"


def direction_fmt(value: Any, digits: int = 1, suffix: str = "%") -> str:
    """Format market direction using overseas convention: green up, red down."""
    x = finite(value)
    if x is None:
        return "暂无"
    tone = "up" if x > 0 else "down" if x < 0 else ""
    rendered = f"{x:,.{digits}f}{suffix}"
    return f'<span class="{tone}">{rendered}</span>' if tone else rendered


def iso_date(value: Any) -> str:
    if value is None or pd.isna(value):
        return "未知"
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"—", "未知", "暂无"}:
            return "未知"
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return "未知"
        if ts.tzinfo is not None:
            has_clock = not (
                ts.hour == 0
                and ts.minute == 0
                and ts.second == 0
                and ts.microsecond == 0
                and ts.nanosecond == 0
            )
            if has_clock:
                ts = ts.tz_convert("America/New_York")
            ts = ts.tz_localize(None)
        return ts.date().isoformat()
    except Exception:
        return "未知"


def series_last_obs(series: pd.Series | None) -> tuple[float | None, str | None]:
    """Extract the latest finite observation value and its YYYY-MM-DD date from a series."""
    if not isinstance(series, pd.Series) or series.empty:
        return None, None
    numeric = pd.to_numeric(series, errors="coerce")
    records: list[tuple[pd.Timestamp, int, float, str]] = []
    for pos, (raw_idx, raw_val) in enumerate(numeric.items()):
        val = finite(raw_val)
        dt_str = iso_date(raw_idx)
        if val is None or dt_str == "未知":
            continue
        records.append((pd.Timestamp(dt_str), pos, val, dt_str))
    if not records:
        return None, None
    records.sort(key=lambda item: (item[0], item[1]))
    _, _, last_val, last_dt = records[-1]
    return last_val, last_dt


def status_for_rsi(value: float | None) -> tuple[str, str]:
    if value is None:
        return "暂无", "muted"
    if value >= 70:
        return "过热", "hot"
    if value <= 30:
        return "超卖", "cold"
    if value >= 60:
        return "偏热", "warm"
    if value <= 40:
        return "偏冷", "cool"
    return "中性", "neutral"


def status_for_ah_rsi(value: float | None) -> tuple[str, str]:
    """Match the ah-premium-monitor thresholds on its -100..0 Bloomberg scale."""
    if value is None:
        return "暂无", "muted"
    if value >= -36:
        return "超买（溢价快速收窄）", "hot"
    if value <= -60:
        return "超卖（溢价快速扩大）", "cold"
    if value >= -44:
        return "偏强", "warm"
    if value <= -52:
        return "偏弱", "cool"
    return "中性", "neutral"


STRIP_WINDOW = 252
STRIP_TRAIL = 20
STRIP_WEEK = 5
STRIP_SMOOTH = 5
STRIP_Z_FULL = 2.5
# 温度带上画出来的冷区/热区边界，冷热 chip 用的就是这两条线，说明和阴影必须一致。
ZONE_COLD = 20.0
ZONE_HOT = 80.0
# 上游拥挤度分位贴边是常态，用 20/80 判定会让 A股 带天天挂满 chip，所以单独收紧。
CROWD_CHIP_COLD = 5.0
CROWD_CHIP_HOT = 90.0

# ── 每日速览：阈值与页面同源，底部用 us_bottom_framework 状态机 ──
SIGNALS_NAME = "signals.json"
DIGEST_NEAR_RSI = 3.0        # 顶底与 Momentum：离阈值几个 RSI 点算接近
DIGEST_NEAR_AH = 3.0         # AH 溢价 RSI：同上
DIGEST_NEAR_MA_PCT = 3.0     # TSM 距均线：几个百分点以内算接近
DIGEST_NEAR_TREND_PCT = 5.0  # AFRM 距支撑线：同上
# 科技内部轮动分五档。过热/过冷就是温度带的热区/冷区（±1.5σ，等于 0—100 轴上的 80/20），
# 偏热/偏冷是中间那一段，免得读者以为「不在热区」就等于毫无倾斜。
DIGEST_TECH_LEAN_Z = 0.75
DIGEST_TECH_ZONE_Z = STRIP_Z_FULL * (ZONE_HOT - 50.0) / 50.0
# QQQ put IV 30D/60D ≤ 0.85：顶部监控的环境线（用户 2026-09-03 从 0.80 改到 0.85）。只作环境，不进顶部状态机。
US_VOL_CRUSH = 0.85
# signals.json 比这个天数还旧就拒发卡片：旧读数发出去看不出来，比不发更糟。
DIGEST_STALE_DAYS = 4

# A-share intraday sector fund flow (Eastmoney).
# push2.eastmoney.com returns 502 for this host's IP; push2delay serves the same
# current-minute data and is the only host allowed here.
FLOW_HOST = "push2delay.eastmoney.com"
# 本机 IP 被东财封禁时的统一说法：构建日志和页面兜底提示共用，卡片不报。
FLOW_BLOCKED_NOTE = "服务器 IP 被东财封禁（HTTP 502），兜底快照为空；资金流由页面打开时实时拉取"
FLOW_REFRESH_MIN = 15
FLOW_SIDE_N = 8
FLOW_YI = 1e8
FLOW_SESSIONS = [["09:30", "11:30"], ["13:00", "15:00"]]
FLOW_TZ = "Asia/Shanghai"

# A股全市场量能：累计成交额以万亿元计，完整交易日是 48 根 5 分钟 K 线。
TURNOVER_UNIT = 1e12
TURNOVER_FULL_BARS = 48
TURNOVER_TIMEOUT = 25

# Vendor crowding board embedded in the China tab.
CROWD_SPARK_STEP = 2
# 图注只写口径和当前读数；来源评价、选样理由、方法论细节归 methodology.md 和总览的“口径与限制”面板。
# 超过这个字数说明又把解释写回脚注了，build 会进 warnings。
NOTE_MAX_CHARS = 100
# 指数长历史（收盘价 + 拥挤度）。拥挤度总表只带 1 年，A股与港股看板要画到 2023 年。
HISTORY_URL = "https://fund.wacaijijin.com/extension/api/index/zone/queryCrowdedList"
HISTORY_TIMEOUT = 25
HISTORY_START = "2023-01-01"
CROWD_BOARD_FIELDS = (
    "index_code", "index_name", "category", "crowding_percentile", "crowding_level",
    "daily_change_pct", "probability_direction", "probability_pct",
    "probability_horizon_days", "detail_url",
)


def rolling_pct_rank(series: pd.Series, window: int = STRIP_WINDOW) -> pd.Series:
    """Percentile rank (0-100) of each observation inside its trailing window."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return pd.Series(dtype=float)
    ranked = values.rolling(window, min_periods=60).rank(pct=True) * 100
    return ranked.dropna()


def robust_z(series: pd.Series, window: int = STRIP_WINDOW, locator: pd.Series | None = None) -> pd.Series:
    """Median/MAD z-score inside a trailing window.

    Percentile rank spends axis space according to local sample density, so the same
    raw move is worth wildly different distance depending on where it lands. A z-score
    keeps one constant ruler (1 sigma per unit). Median/MAD instead of mean/sd because
    the outliers we are measuring would otherwise inflate their own yardstick.

    The ruler always comes from the raw series. `locator` only says which value gets
    scored, so a smoothed anchor and a raw current point are read off the same yardstick
    instead of each carrying its own dispersion.
    """
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return pd.Series(dtype=float)

    def mad(window_values: np.ndarray) -> float:
        return float(np.median(np.abs(window_values - np.median(window_values))))

    center = values.rolling(window, min_periods=60).median()
    scale = values.rolling(window, min_periods=60).apply(mad, raw=True) * 1.4826
    located = values if locator is None else pd.to_numeric(locator, errors="coerce").reindex(values.index)
    z = (located - center) / scale.replace(0.0, np.nan)
    return z.replace([np.inf, -np.inf], np.nan).dropna()


def anchor_smooth(series: pd.Series, window: int = STRIP_SMOOTH) -> pd.Series:
    """Centred median (T-2..T+2) for the 1W/1M anchors.

    Anchors sit far enough back that both halves of the window already exist, so the
    estimate can be centred on the anchor date instead of dragging it days late.
    """
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return values
    return values.rolling(window, center=True, min_periods=1).median()


def locator_series(series: pd.Series, window: int = STRIP_SMOOTH) -> pd.Series:
    """Marker positions: smoothed history, raw current point.

    The last observation keeps its own value because that is the reading being acted on
    today; smoothing it would park the current marker on a two-day-old number.
    """
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return values
    located = anchor_smooth(values, window)
    located.iloc[-1] = float(values.iloc[-1])
    return located


def z_to_x(z: Any) -> float | None:
    """Map a robust z onto the shared 0-100 axis; +/-2.5 sigma fills the strip."""
    value = finite(z)
    if value is None:
        return None
    return round(max(0.0, min(100.0, 50.0 + 50.0 * value / STRIP_Z_FULL)), 1)


def path_marks(trail: list[float]) -> tuple[float | None, float | None]:
    """Percentile one week and one month back, read off the trailing path."""
    week = trail[-(STRIP_WEEK + 1)] if len(trail) >= STRIP_WEEK + 1 else None
    month = trail[-(STRIP_TRAIL + 1)] if len(trail) >= STRIP_TRAIL + 1 else None
    return week, month


def strip_item(
    name: str,
    series: pd.Series,
    *,
    invert: bool = False,
    digits: int = 1,
    suffix: str = "",
    hint: str = "",
    window: int = STRIP_WINDOW,
) -> dict[str, Any] | None:
    """One row on a temperature strip, positioned by robust z off one raw-built ruler.

    Anchors use a centred 5-day median so a single print cannot own them; the current
    marker uses today's raw value. The percentile survives in the payload because it
    reads more naturally in hover; it just no longer decides where the marker sits.
    """
    raw_values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if raw_values.empty:
        return None
    raw_values = raw_values[~pd.isna(raw_values.index)]
    if raw_values.empty:
        return None
    z = robust_z(raw_values, window, locator=locator_series(raw_values))
    ranked = rolling_pct_rank(raw_values, window)
    if z.empty:
        return None
    if invert:
        z = -z
        ranked = 100 - ranked

    def back(offset: int) -> float | None:
        return float(z.iloc[-(offset + 1)]) if len(z) > offset else None

    z_now, z_week, z_month = back(0), back(STRIP_WEEK), back(STRIP_TRAIL)
    if z_now is None:
        return None
    recent = raw_values.iloc[-window:]
    _, last_dt = series_last_obs(raw_values)
    return {
        "name": name,
        "scale": "z",
        "x": z_to_x(z_now),
        "x1w": z_to_x(z_week),
        "x1m": z_to_x(z_month),
        "z": round(z_now, 2),
        "z1w": round(z_week, 2) if z_week is not None else None,
        "z1m": round(z_month, 2) if z_month is not None else None,
        "pct": round(float(ranked.iloc[-1]), 1) if not ranked.empty else None,
        "raw": f"{float(raw_values.iloc[-1]):,.{digits}f}{suffix}",
        "range": f"{float(recent.min()):,.{digits}f}–{float(recent.max()):,.{digits}f}{suffix}",
        "invert": bool(invert),
        "asof": last_dt or iso_date(raw_values.index.max()),
        "hint": hint,
    }


def strip_point(
    name: str,
    x: float | None,
    *,
    trail: list[float] | None = None,
    raw: str = "",
    asof: str = "",
    hint: str = "",
    invert: bool = False,
    scale: str = "pct",
) -> dict[str, Any] | None:
    """One row from an already-normalised 0-100 score (crowding, leverage composites).

    These arrive pre-normalised from upstream monitors, so they keep their own ruler
    and are grouped apart from the z rows instead of being silently mixed with them.
    """
    value = finite(x)
    if value is None:
        return None
    if invert:
        value = 100 - value
    path = [round(float(v), 1) for v in (trail or [])]
    if invert:
        path = [round(100 - v, 1) for v in path]
    week, month = path_marks(path)
    return {
        "name": name,
        "scale": scale,
        "x": round(value, 1),
        "trail": path,
        "x1w": week,
        "x1m": month,
        "pct": round(value, 1) if scale == "pct" else None,
        "raw": raw,
        "invert": bool(invert),
        "asof": asof,
        "hint": hint,
    }


def chip(text: str, tone: str) -> dict[str, str]:
    """One 冷/热 chip above a strip. tone 只有 cold / hot 两种，和图上冷区热区同色。"""
    return {"text": text, "tone": "cold" if tone == "cold" else "hot"}


def zone_chips(items: list[dict[str, Any]], skip: Iterable[str] = ()) -> list[dict[str, str]]:
    """把落进冷区/热区的行提成提示，四条温度带同一套判定。

    z 行和综合分行直接用图上那两条分区线（0—100 轴的 20 / 80），提示词和阴影说的是同一件事。
    上游拥挤度分位天然贴边——七个观察指数里常年有三四个待在 20 以下——所以那类行收紧到
    ≥90 / ≤5 才提示，否则整条带天天挂五个 chip，等于没提示。
    """
    skip_names = set(skip)
    out: list[dict[str, str]] = []
    for item in items:
        if not item or item["name"] in skip_names:
            continue
        x = finite(item.get("x"))
        if x is None:
            continue
        scale = item.get("scale")
        if scale == "pct":
            hot, cold = CROWD_CHIP_HOT, CROWD_CHIP_COLD
            label = f"{item['name']} 拥挤度 {x:.1f}%"
        elif scale == "z":
            hot, cold = ZONE_HOT, ZONE_COLD
            z = finite(item.get("z"))
            label = f"{item['name']} 稳健z {z:+.2f}σ" if z is not None else item["name"]
        else:
            hot, cold = ZONE_HOT, ZONE_COLD
            label = f"{item['name']} 综合分 {x:.0f}/100"
        if x >= hot:
            out.append(chip(f"{label}（热区）", "hot"))
        elif x <= cold:
            out.append(chip(f"{label}（冷区）", "cold"))
    return out


def strip_panel(title: str, items: list[dict[str, Any]], alerts: list[dict[str, str]], note: str = "", fixed: bool = False) -> str:
    """Render one market temperature strip; position carries the state, no numbers to read.

    fixed=True keeps the declared row order instead of ranking rows by today's dot, so a
    block of rows that share one 口径 stays together and a row does not jump position
    between two builds for a 0.05 sigma move.
    """
    live = [i for i in items if i]
    if not live:
        return f'<article class="panel"><h3>{html.escape(title)}</h3><p class="empty">本带数据本次不可用。</p></article>'
    payload = {"title": title, "items": live, "fixed": bool(fixed)}
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=True)
    alert_html = (
        '<div class="alerts on">'
        + "".join(f'<span class="{a["tone"]}">⚠ {html.escape(a["text"])}</span>' for a in alerts)
        + "</div>"
        if alerts else ""
    )
    return (
        f'<article class="panel strip-panel"><div class="panel-head"><h3>{html.escape(title)}</h3></div>'
        f'{alert_html}<div class="strip" data-strip="{encoded}"><canvas></canvas><div class="tooltip"></div></div>'
        + (f'<p class="note">{html.escape(note)}</p>' if note else "")
        + "</article>"
    )


def series_payload(series: pd.Series, name: str, color: str, axis: str = "left", fill: bool = False, max_points: int = 520, max_gap_days: int | None = None) -> dict[str, Any]:
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if max_points > 0 and len(clean) > max_points:
        clean = clean.iloc[-max_points:]
    return {
        "name": name,
        "color": color,
        "axis": axis,
        "fill": fill,
        "maxGapDays": max_gap_days,
        "data": [[iso_date(idx), round(float(val), 5)] for idx, val in clean.items() if iso_date(idx) != "未知"],
    }


def chart(
    title: str,
    series: list[dict[str, Any]],
    note: str = "",
    right_domain: list[float] | None = None,
    height: int = 320,
    left_domain: list[float] | None = None,
    reverse_left: bool = False,
    hlines: list[dict[str, Any]] | None = None,
    align: tuple[float, float] | None = None,
) -> str:
    payload = {
        "title": title,
        "series": series,
        "rightDomain": right_domain,
        "leftDomain": left_domain,
        "reverseLeft": reverse_left,
        "hlines": hlines or [],
        "align": None if align is None else {"left": float(align[0]), "right": float(align[1])},
    }
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=True)
    legend = "".join(
        f'<span><i style="background:{html.escape(s["color"])}"></i>{html.escape(s["name"])}</span>' for s in series
    )
    return (
        f'<article class="panel chart-panel"><div class="panel-head"><h3>{html.escape(title)}</h3>'
        f'<div class="legend">{legend}</div></div><div class="chart" style="height:{height}px" data-chart="{encoded}">'
        '<canvas></canvas><div class="tooltip"></div></div>'
        f'<p class="note">{html.escape(note)}</p></article>'
    )


def append_note(panel: str, text: str) -> str:
    """在已生成的图表面板里再挂一行图注。text 为空时原样返回。"""
    if not text or "</article>" not in panel:
        return panel
    cut = panel.rindex("</article>")
    return panel[:cut] + f'<p class="note">{html.escape(text)}</p>' + panel[cut:]


def trend_note(stats: dict[str, Any] | None) -> str:
    """趋势线读数，压在 NOTE_MAX_CHARS 以内；拟合口径留在 config 的 note 和 methodology.md。"""
    if not stats:
        return ""
    span = f"{stats['start'][:4]}H{1 if int(stats['start'][5:7]) <= 6 else 2}起"
    if stats.get("mode") == "channel" and stats.get("top") is not None:
        return (
            f"{span}平行通道：当前趋势顶 ${stats['top']:.1f}（{stats['top_gap']:+.1f}%），"
            f"当前趋势底 ${stats['bottom']:.1f}（{stats['bottom_gap']:+.1f}%）；"
            f"趋势线未来6M抬升 ${stats['rise_6m']:.1f}、12M ${stats['rise_12m']:.1f}。"
        )
    return (
        f"{span}上行支撑线：当前趋势底 ${stats['bottom']:.1f}（{stats['bottom_gap']:+.1f}%）；"
        f"趋势线未来6M抬升 ${stats['rise_6m']:.1f}、12M ${stats['rise_12m']:.1f}。"
    )


def trend_fit_note(stats: dict[str, Any] | None) -> str:
    """第二行图注：这条线是哪次拟合冻结下来的，以及至今的上下破比例。"""
    if not stats:
        return ""
    fitted = stats.get("fitted_on") or "首次"
    parts = [f"线固定于{fitted}那次拟合，斜率 ${stats['slope']:.3f}/根K线，往后只随K线数平移。"]
    if stats.get("break_pct") is not None:
        parts.append(f"期间{stats['break_pct']:.1f}%的交易日上破顶、{stats['pierce_pct']:.1f}%下破底。")
    else:
        parts.append(f"期间{stats['pierce_pct']:.1f}%的交易日下破。")
    return "".join(parts)


def trend_panel(stats: dict[str, Any] | None, ticker: str) -> str:
    """只报趋势底的独立面板：当前位置、6M 与 12M 抬升幅度，各占一张卡。"""
    if not stats:
        return (
            f'<article class="panel"><h3>{html.escape(ticker)} 上行支撑线</h3>'
            f'<p class="empty">{html.escape(ticker)}的复权日线本次取不到，趋势底算不出来，不用旧值顶替。</p></article>'
        )
    gap = stats["bottom_gap"]
    tone = "cold" if gap > -5 else "neutral"
    cards = (
        metric_card("当前趋势底", f"${stats['bottom']:.1f}", f"距现价 {gap:+.1f}%", tone,
                    f"现价 ${stats['close']:.2f} · {stats['as_of']}")
        + metric_card("未来6M抬升", f"+${stats['rise_6m']:.1f}", "126个交易日", "neutral",
                      f"半年后趋势底 ${stats['bottom'] + stats['rise_6m']:.1f}")
        + metric_card("未来12M抬升", f"+${stats['rise_12m']:.1f}", "252个交易日", "neutral",
                      f"一年后趋势底 ${stats['bottom'] + stats['rise_12m']:.1f}")
    )
    note = (
        f"{stats['start']}起，等距K线上对最低价做下包络，线固定于{stats.get('fitted_on') or '首次'}那次拟合，"
        f"斜率 ${stats['slope']:.3f}/根，贴线低点{stats.get('bottom_pts', '—')}个，"
        f"期间{stats['pierce_pct']:.1f}%的交易日曾下破。"
    )
    return (
        f'<article class="panel"><h3>{html.escape(ticker)} 上行支撑线</h3>'
        f'<p class="note">{html.escape(note)}</p><section class="cards">{cards}</section></article>'
    )


def long_notes(doc: str, cap: int = NOTE_MAX_CHARS) -> list[str]:
    """脚注体检：返回超过 cap 字的图注前 40 字，供 build 报 warning。"""
    out: list[str] = []
    for raw in re.findall(r'<p class="note[^"]*">(.*?)</p>', doc, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if len(text) > cap:
            out.append(f"{text[:40]}…（{len(text)}字）")
    return out


def hline(value: float, axis: str = "left", label: str = "", tone: str = "zero") -> dict[str, Any]:
    """Reference line drawn inside a chart so thresholds stop living in footnotes."""
    return {"value": float(value), "axis": axis, "label": label, "tone": tone}


# 200DMA 乖离的中性位是 0，相对强弱 RSI 的中性位是 50。两张轴各自 auto-fit 时这两条
# 中性线会落在不同高度，读者得先在脑子里做一次配准才能判断“价格在均线上方、相对强弱却
# 已经转弱”。把左轴 0 钉在右轴 50 上，中性线合成一条，跨图比较也不用重新校准。
ALIGN_ZERO_TO_50: tuple[float, float] = (0.0, 50.0)


def metric_card(title: str, value: str, subtitle: str, tone: str = "neutral", meta: str = "") -> str:
    return (
        f'<article class="metric {tone}"><div class="metric-title">{html.escape(title)}</div>'
        f'<div class="metric-value">{html.escape(value)}</div><div class="metric-sub">{html.escape(subtitle)}</div>'
        f'<div class="metric-meta">{html.escape(meta)}</div></article>'
    )


def table(headers: list[str], rows: list[list[str]], classes: str = "") -> str:
    th = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap {classes}"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


BBG_STALE_DAYS = 4


def stale_days(as_of: str) -> int | None:
    """外部投喂数据落后今天几个日历日；日期解析不出来返回 None。"""
    try:
        return int((pd.Timestamp.today().normalize() - pd.Timestamp(as_of).normalize()).days)
    except Exception:
        return None


def parse_bbg(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "BBG CSV不存在"}
    df = pd.read_csv(path)

    def paired(date_col: str, value_col: str) -> pd.Series:
        dates = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
        values = pd.to_numeric(df[value_col], errors="coerce")
        out = pd.Series(values.to_numpy(), index=dates).dropna()
        return out[~out.index.isna()].sort_index()

    mom_value_col = next((c for c in df.columns if re.fullmatch(r"RSI \d+D", str(c))), None)
    # The first exact RSI column belongs to GSPUCYDE; momentum is the next un-suffixed RSI column.
    if "GSCBHMOM Index" in df.columns:
        pos = list(df.columns).index("GSCBHMOM Index")
        mom_value_col = str(df.columns[pos + 1])
    ah_rsi_col = "RSI 30D.1"
    if "HSAHP Index.1" in df.columns:
        pos = list(df.columns).index("HSAHP Index.1")
        ah_rsi_col = str(df.columns[pos + 1])
    period_match = re.search(r"(\d+)D", mom_value_col or "")
    mom_period = int(period_match.group(1)) if period_match else None
    out: dict[str, Any] = {"ok": True, "path": str(path), "mom_period": mom_period}
    specs = {
        "cyc_price": ("GSPUCYDE Index", "Last Price"),
        "cyc_rsi": ("GSPUCYDE Index.1", "RSI 14D"),
        "momentum_rsi": ("GSCBHMOM Index", mom_value_col),
        "qqq_vol_1m": ("QQQ US Equity", "1M vol"),
        "qqq_vol_3m": ("QQQ US Equity.1", "3M vol"),
        "ah_price": ("HSAHP Index", "Last Price.1"),
        "ah_rsi": ("HSAHP Index.1", ah_rsi_col),
    }
    for key, pair in specs.items():
        try:
            out[key] = paired(*pair)
        except Exception:
            out[key] = pd.Series(dtype=float)
    us_keys = ("cyc_price", "cyc_rsi", "momentum_rsi", "qqq_vol_1m", "qqq_vol_3m")
    us_dates = [d for k in us_keys if (d := series_last_obs(out.get(k))[1])]
    ah_dates = [d for k in ("ah_price", "ah_rsi") if (d := series_last_obs(out.get(k))[1])]
    all_dates = us_dates + ah_dates
    out["as_of"] = max(all_dates) if all_dates else "未知"
    out["us_as_of"] = max(us_dates) if us_dates else out["as_of"]
    out["ah_as_of"] = max(ah_dates) if ah_dates else out["as_of"]
    return out


TREND_ITERS = 4
TREND_MIN_PTS = 8


def envelope_line(t: np.ndarray, y: np.ndarray, upper: bool, slope: float | None = None) -> tuple[float, float, int]:
    """一侧削减回归：拟合一条贴着上沿或下沿的直线，返回 (斜率, 截距, 最后一轮用到的点数)。

    每轮先做 OLS，再只保留落在线这一侧的点重拟合，重复 TREND_ITERS 轮。点数大致逐轮减半，
    最后收敛到最外侧那几个百分点的样本上，等价于人手画趋势线时"只连高点"或"只连低点"。

    不直接取 max/min 残差是因为那样整条线由单根 K 线的插针决定；一侧削减仍是多点拟合，
    图表软件里那条线也是多点接触的。轮数定在 4 是照着用户给的图标定的：AMZN 上沿按这个
    口径算出 287.11，交易软件在同一根 K 线上标 287.200。改轮数等于改线，要重新标定。

    slope 不为 None 时冻结斜率、只迭代截距，用来把通道的两条边强制画成平行。
    """
    mask = np.ones(len(t), dtype=bool)
    b, c = (float(slope) if slope is not None else 0.0), 0.0
    for _ in range(TREND_ITERS):
        if slope is None:
            fit = np.polyfit(t[mask], y[mask], 1)
            b, c = float(fit[0]), float(fit[1])
        else:
            c = float(np.mean(y[mask] - b * t[mask]))
        side = (y - (b * t + c)) > 0 if upper else (y - (b * t + c)) < 0
        if int(side.sum()) < TREND_MIN_PTS:
            break
        mask = side
    return b, c, int(mask.sum())


def fit_trend_line(frame: pd.DataFrame, start: str, mode: str) -> dict[str, Any] | None:
    """等距 K 线上拟合一次上行通道 / 支撑线，产出可写进 config 的冻结参数。

    只有 `refit-trend` 会调用它，日常 build 不重拟合。返回的 anchor 是拟合窗口最后一根 K 线的
    日期，top/bottom 是这条线在该 K 线上的取值；后续 build 用 anchor 往后数 bar 平移，
    所以哪怕更早的历史被数据源改写，线本身也不动。

    frame 需要 High/Low/Close 三列复权日线。
    """
    window = frame.loc[frame.index >= start].dropna()
    if len(window) < 250:
        return None
    n = len(window)
    t = np.arange(n, dtype=float)
    high = window["High"].to_numpy(dtype=float)
    low = window["Low"].to_numpy(dtype=float)
    b_dn, c_dn, pts_dn = envelope_line(t, low, upper=False)
    out: dict[str, Any] = {
        "anchor": iso_date(window.index.max()),
        "fitted_on": date.today().isoformat(),
        "bars": n,
    }
    if mode == "channel":
        b_up, _, _ = envelope_line(t, high, upper=True)
        slope = (b_up + b_dn) / 2
        _, c_up, pts_up = envelope_line(t, high, upper=True, slope=slope)
        _, c_dn, pts_dn = envelope_line(t, low, upper=False, slope=slope)
        out["top"] = round(slope * (n - 1) + c_up, 4)
        out["top_pts"] = pts_up
    else:
        slope = b_dn
    out["slope"] = round(slope, 6)
    out["bottom"] = round(slope * (n - 1) + c_dn, 4)
    out["bottom_pts"] = pts_dn
    return out


def trend_line_stats(frame: pd.DataFrame, start: str, mode: str, fit: dict[str, Any] | None) -> dict[str, Any] | None:
    """按 config 里冻结的那条线读当前位置。线不重拟合，随行情变的只有距离和上下破比例。

    fit 给的是 anchor 那根 K 线上的线值和每根 K 线的斜率，这里只做平移：
    当前线值 = anchor 线值 + 斜率 × (今天的 bar 号 − anchor 的 bar 号)。
    6M/12M 抬升按 126 / 252 个交易日折算，不按日历日——自变量是 bar 序号，
    用日历日换算会把休市天数也算成抬升。
    """
    if not fit:
        return None
    window = frame.loc[frame.index >= start].dropna()
    if len(window) < 250:
        return None
    pos = int(window.index.searchsorted(pd.Timestamp(fit["anchor"]), side="right")) - 1
    if pos < 0:
        return None
    n = len(window)
    t = np.arange(n, dtype=float) - pos  # anchor 那根 K 线为 0，之后为正
    high = window["High"].to_numpy(dtype=float)
    low = window["Low"].to_numpy(dtype=float)
    close = float(window["Close"].to_numpy(dtype=float)[-1])
    slope = float(fit["slope"])
    line_dn = slope * t + float(fit["bottom"])
    out: dict[str, Any] = {
        "start": iso_date(window.index.min()),
        "as_of": iso_date(window.index.max()),
        "close": close,
        "bars": n,
        "anchor": iso_date(window.index[pos]),
        "fitted_on": fit.get("fitted_on", ""),
    }
    if mode == "channel" and fit.get("top") is not None:
        line_up = slope * t + float(fit["top"])
        out["top"] = float(line_up[-1])
        out["top_gap"] = (out["top"] / close - 1) * 100
        out["top_pts"] = fit.get("top_pts")
        out["break_pct"] = float((high > line_up).sum()) / n * 100
    out["slope"] = slope
    out["bottom"] = float(line_dn[-1])
    out["bottom_gap"] = (out["bottom"] / close - 1) * 100
    out["bottom_pts"] = fit.get("bottom_pts")
    out["rise_6m"] = slope * 126
    out["rise_12m"] = slope * 252
    out["pierce_pct"] = float((low < line_dn).sum()) / n * 100
    return out


def download_ohlc(tickers: Iterable[str], start: str, cache_path: Path | None = None) -> dict[str, pd.DataFrame]:
    """趋势线要接触影线，所以另取一份复权 High/Low/Close，不复用只有收盘价的 download_prices。

    只拉趋势线用到的那一两个标的。永远先向 yfinance 取一次，所以缓存不会挡住新数据；只有这次
    取不到才退回缓存，并给退回的标的打上 from_cache，由调用方写进 warnings。缓存也没有时返回空，
    由调用方标记不可用，不用收盘价假装成最高最低价。
    """
    unique = list(dict.fromkeys(tickers))
    if not unique:
        return {}
    import yfinance as yf

    result: dict[str, pd.DataFrame] = {}
    for ticker in unique:
        try:
            raw = yf.download(ticker, start=start, auto_adjust=True, progress=False, threads=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            frame = raw[["High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce").dropna()
            frame.index = pd.to_datetime(frame.index).tz_localize(None)
            if len(frame) > 250:
                frame.attrs["from_cache"] = False
                result[ticker] = frame
        except Exception:
            continue
    if cache_path:
        cached: dict[str, pd.DataFrame] = {}
        if cache_path.exists():
            try:
                loaded = pd.read_pickle(cache_path)
                if isinstance(loaded, dict):
                    cached = loaded
            except Exception:
                cached = {}
        fresh = dict(result)
        for ticker in unique:
            if ticker not in result and ticker in cached:
                fallback = cached[ticker].copy()
                fallback.attrs["from_cache"] = True
                result[ticker] = fallback
        if fresh:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            pd.to_pickle({**cached, **fresh}, cache_path)
    return result


def last_completed_us_session(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """Last US cash session that should already have a daily bar.

    Before 16:00 America/New_York we do not expect today's bar. Weekends walk
    back to Friday. This is not a holiday calendar; it only stops a Tuesday
    cache from looking fresh on Friday.
    """
    current = pd.Timestamp.now(tz="America/New_York") if now is None else pd.Timestamp(now)
    if current.tzinfo is None:
        current = current.tz_localize("America/New_York")
    else:
        current = current.tz_convert("America/New_York")
    day = current.normalize()
    if current.hour < 16:
        day = day - pd.offsets.BDay(1)
    while day.weekday() >= 5:
        day = day - pd.Timedelta(days=1)
    return pd.Timestamp(day.tz_localize(None).normalize())


def price_cache_fresh(latest: Any, now: pd.Timestamp | None = None) -> bool:
    if latest is None or (isinstance(latest, float) and pd.isna(latest)):
        return False
    latest_ts = pd.Timestamp(latest)
    if latest_ts.tzinfo is not None:
        latest_ts = latest_ts.tz_convert("America/New_York").tz_localize(None)
    return latest_ts.normalize() >= last_completed_us_session(now)


def us_cash_session_today(now: pd.Timestamp | None = None) -> bool:
    """Calendar-day check: Mon-Fri counts as a session day. Not a holiday calendar."""
    current = pd.Timestamp.now(tz="America/New_York") if now is None else pd.Timestamp(now)
    if current.tzinfo is None:
        current = current.tz_localize("America/New_York")
    else:
        current = current.tz_convert("America/New_York")
    return int(current.weekday()) < 5


def should_reuse_price_cache(latest: Any, now: pd.Timestamp | None = None) -> bool:
    """Reuse cache only when US cash is not in session today and we already have the last bar."""
    return (not us_cash_session_today(now)) and price_cache_fresh(latest, now)


def download_prices(tickers: Iterable[str], start: str, cache_path: Path | None = None) -> dict[str, pd.Series]:
    unique = list(dict.fromkeys(tickers))
    if cache_path and cache_path.exists():
        try:
            cached = pd.read_pickle(cache_path)
            if isinstance(cached, dict) and all(t in cached for t in unique):
                latest = max((s.index.max() for s in cached.values() if isinstance(s, pd.Series) and not s.empty), default=None)
                spx_cached = cached.get("^GSPC", pd.Series(dtype=float))
                requested_start = (pd.Timestamp(start) + timedelta(days=10)).date()
                covers_history = not spx_cached.empty and pd.Timestamp(spx_cached.index.min()).date() <= requested_start
                if latest is not None and covers_history and should_reuse_price_cache(latest):
                    return cached
        except Exception:
            pass
    import yfinance as yf

    result: dict[str, pd.Series] = {}
    for i in range(0, len(unique), 12):
        chunk = unique[i : i + 12]
        try:
            raw = yf.download(chunk, start=start, auto_adjust=True, progress=False, group_by="ticker", threads=True)
        except Exception:
            continue
        for ticker in chunk:
            try:
                if len(chunk) == 1:
                    close = raw["Close"]
                else:
                    close = raw[(ticker, "Close")]
                close = pd.to_numeric(close, errors="coerce").dropna()
                close.index = pd.to_datetime(close.index).tz_localize(None)
                if not close.empty:
                    result[ticker] = close
            except Exception:
                continue
    # 批量下载偶发整块失败（本次就掉过 MU），漏掉的标的会让等权篮子和半导体广度少一只成分
    # 却照样画出来。这里逐个补一次，补不回来的留给调用方进 warnings，不做静默降级。
    for ticker in [t for t in unique if t not in result]:
        try:
            raw = yf.download(ticker, start=start, auto_adjust=True, progress=False, threads=False)
            close = raw["Close"] if "Close" in raw else pd.Series(dtype=float)
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = pd.to_numeric(close, errors="coerce").dropna()
            if not close.empty:
                close.index = pd.to_datetime(close.index).tz_localize(None)
                result[ticker] = close
        except Exception:
            continue
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pd.to_pickle(result, cache_path)
        except Exception:
            pass
    return result


def fetch_fred(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd=2022-12-01"
    if shutil.which("curl"):
        proc = subprocess.run(["curl", "-L", "--max-time", "20", "-fsS", url], capture_output=True, timeout=25, check=True)
        frame = pd.read_csv(io.BytesIO(proc.stdout))
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw sentiment-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=25) as response:
            frame = pd.read_csv(response)
    date_col = "DATE" if "DATE" in frame.columns else frame.columns[0]
    value_col = series_id if series_id in frame.columns else frame.columns[1]
    out = pd.Series(pd.to_numeric(frame[value_col], errors="coerce").to_numpy(), index=pd.to_datetime(frame[date_col], errors="coerce"))
    return out.dropna().sort_index()


def spx_trend(seed: dict[str, Any], end: pd.Timestamp) -> pd.Series:
    anchor_date = pd.Timestamp(seed["anchor"]["date"])
    anchor_value = float(seed["anchor"]["spx"])
    annual = {int(k): float(v) for k, v in seed["annual_nominal_gdp_pct"].items()}
    c = seed["normal_formula"]

    def eps_growth(year: int) -> float:
        nominal = annual.get(year, annual[max(annual)])
        if year == 2018:
            return c["nominal_gdp_beta"] * nominal + c["intercept"] + seed["tax_reform_2018_growth"]
        return c["nominal_gdp_beta"] * nominal + c["intercept"] + c["earnings_share_growth"] + c["buyback_growth"]

    dates = pd.date_range("2015-01-02", end.normalize(), freq="W-FRI")
    anchor_idx = int(np.argmin(np.abs(dates - anchor_date)))
    values = np.empty(len(dates), dtype=float)
    values[anchor_idx] = anchor_value
    for i in range(anchor_idx + 1, len(dates)):
        weekly = (1 + eps_growth(dates[i].year) / 100) ** (1 / 52) - 1
        values[i] = values[i - 1] * (1 + weekly)
    for i in range(anchor_idx - 1, -1, -1):
        weekly = (1 + eps_growth(dates[i].year) / 100) ** (1 / 52) - 1
        values[i] = values[i + 1] / (1 + weekly)
    return pd.Series(values, index=dates, name="SPX长期趋势")


def load_cn_margin(workspace: Path) -> dict[str, Any]:
    margin_path = workspace / "state/cn-leverage/margin_core.csv"
    index_path = workspace / "state/cn-leverage/index_prices.csv"
    margin = pd.read_csv(margin_path, parse_dates=["date"]).set_index("date")["margin_balance_cny100m"]
    indices = pd.read_csv(index_path, parse_dates=["date"])
    csi1000 = indices.loc[indices["code"].eq("IM")].set_index("date")["close"]
    frame = pd.concat({"margin": margin, "csi1000": csi1000}, axis=1).sort_index()
    frame["csi1000"] = frame["csi1000"].ffill(limit=3)
    frame["margin_rsi14"] = wilder_rsi(frame["margin"], 14)
    clean_frame = frame.dropna(subset=["margin_rsi14", "csi1000"])
    _, margin_as_of = series_last_obs(clean_frame["margin_rsi14"] if not clean_frame.empty else margin)
    return {"frame": clean_frame, "as_of": margin_as_of or iso_date(margin.index.max()), "source": str(margin_path)}


def load_crowding(workspace: Path) -> dict[str, Any]:
    path = workspace / "research-outputs/index-crowding/official-all.json"
    if not path.exists():
        return {"ok": False, "rows": [], "trade_date": "未知"}
    payload = load_json(path)
    payload["ok"] = True
    return payload


def index_history_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """供应商单个指数的长历史，收盘价与拥挤度同源同交易日。"""
    rows = payload.get("data") or []
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if not {"tradeDt", "value", "close"} <= set(frame.columns):
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["tradeDt"], format="%Y%m%d", errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["date", "close", "value"]).drop_duplicates("date", keep="last")
    return frame.set_index("date")[["close", "value"]].sort_index()


def index_history_fetch(code: str, timeout: int = HISTORY_TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{HISTORY_URL}?indexCode={urllib.parse.quote(code)}&timeInterval=3",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://fund.wacaijijin.com/extension/indexZoneDetail",
            "User-Agent": "Mozilla/5.0 (compatible; EveSentimentDashboard/1.0; research)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("error") or f"vendor code={payload.get('code')}")
    if not (payload.get("data") or []):
        raise RuntimeError("vendor returned an empty series")
    return payload


def load_index_history(codes: Iterable[str], workspace: Path, refresh: bool = True) -> dict[str, Any]:
    """本板块用到的指数长历史：先刷新，失败就退回缓存，绝不用空图冒充。

    拥挤度总表只带 1 年历史，画不了 2023 年以来的走势，所以这里单独取供应商的长历史。
    只拉本页真正用到的十几个代码，不重跑 index-crowding-research 那份 211 个的全量抓取。
    缓存写本 skill 自己的 state 目录；index-crowding-research 的缓存只读作种子，不回写别人的 state。
    """
    cache_dir = workspace / "state/sentiment-dashboard/index_history"
    seed_dir = workspace / "skills/index-crowding-research/state/cache/history"
    wanted = list(dict.fromkeys(codes))
    frames: dict[str, pd.DataFrame] = {}
    refreshed: list[str] = []
    stale: list[str] = []
    missing: list[str] = []

    fetched: dict[str, dict[str, Any]] = {}
    if refresh and wanted:
        with ThreadPoolExecutor(max_workers=min(6, len(wanted))) as pool:
            futures = {pool.submit(index_history_fetch, code): code for code in wanted}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    fetched[code] = future.result()
                except Exception:
                    pass

    for code in wanted:
        stem = code.replace(".", "_")
        payload = fetched.get(code)
        if payload is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp_path = cache_dir / f"{stem}.json.tmp"
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(cache_dir / f"{stem}.json")
            refreshed.append(code)
        else:
            path = cache_dir / f"{stem}.json"
            if not path.exists():
                path = seed_dir / f"{stem}.json"
            if not path.exists():
                missing.append(code)
                continue
            try:
                payload = load_json(path)
            except Exception:
                missing.append(code)
                continue
            stale.append(code)
        frame = index_history_frame(payload)
        if frame.empty:
            missing.append(code)
            continue
        frames[code] = frame

    as_of = max((f.index.max() for f in frames.values()), default=None)
    return {
        "frames": frames,
        "as_of": iso_date(as_of),
        "refreshed": refreshed,
        "stale": stale,
        "missing": missing,
    }


MSCI_LEVEL_URL = (
    "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
    "?currency_symbol={currency}&index_variant=STRD&start_date={start}&end_date={end}"
    "&data_frequency=DAILY&index_codes={code}"
)


def fetch_msci_levels(code: str, currency: str, start: str, workspace: Path, refresh: bool = True) -> tuple[pd.Series, str]:
    """MSCI 官方 indexmaster 端点的日度指数点位；拉不到就退回本地缓存，并把状态报出去。

    这三条国别指数没有别的免费一手来源：拥挤度供应商只覆盖中国指数，Yahoo 上只有跟踪
    ETF（EWJ/EWY/EWT），而 EWT 跟的是 MSCI Taiwan 25/50 封顶版，台积电权重被压过之后
    2023 年以来累计回报比未封顶的母指数低约 40 个百分点，拿它冒充 MSCI 台湾会读错幅度。
    """
    cache_dir = workspace / "state/sentiment-dashboard/msci"
    path = cache_dir / f"{code}_{currency}.json"
    payload: dict[str, Any] | None = None
    if refresh:
        url = MSCI_LEVEL_URL.format(
            currency=currency,
            code=code,
            start=pd.Timestamp(start).strftime("%Y%m%d"),
            end=pd.Timestamp.today().strftime("%Y%m%d"),
        )
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not (payload.get("indexes", {}).get("INDEX_LEVELS") or []):
                payload = None
        except Exception:
            payload = None
    state = "fresh"
    if payload is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path = cache_dir / f"{code}_{currency}.json.tmp"
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
    else:
        state = "stale"
        if not path.exists():
            return pd.Series(dtype=float), "missing"
        try:
            payload = load_json(path)
        except Exception:
            return pd.Series(dtype=float), "missing"
    levels = payload.get("indexes", {}).get("INDEX_LEVELS") or []
    series = pd.Series(
        [finite(x.get("level_eod")) for x in levels],
        index=pd.to_datetime([str(x.get("calc_date")) for x in levels], errors="coerce"),
        dtype=float,
    )
    series = series[~series.index.isna()].dropna().sort_index()
    if series.empty:
        return series, "missing"
    return series, state


def asia_leverage_csv(workspace: Path, market: str, name: str) -> pd.DataFrame | None:
    """读各地杠杆监控自己的历史库，不复制它们的抓取逻辑，也不回写它们的目录。"""
    path = workspace / "research-outputs" / f"{market}-leverage/data/{name}"
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if "date" not in frame.columns:
        return None
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")
    return frame.set_index("date")


def asia_leverage_column(frame: pd.DataFrame | None, column: str) -> pd.Series:
    if frame is None or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def asia_leverage_series(workspace: Path) -> dict[str, pd.Series]:
    """日韩台三地的一级杠杆指标，单位换算与 scripts/asia_leverage_crossview.py 逐条对齐。

    口径只有一处允许分歧：这里不做分位和去杠杆事件判定，只出原始水平序列，图上读的是
    水平和拐点，不是分数。
    """
    out: dict[str, pd.Series] = {}

    jp = asia_leverage_csv(workspace, "jp", "margin_weekly.csv")
    long_value = asia_leverage_column(jp, "long_value_mjpy")
    short_value = asia_leverage_column(jp, "short_value_mjpy")
    out["jp_margin_long"] = long_value / 1e6 if not long_value.empty else long_value  # 百万円 → 兆円
    if not long_value.empty and not short_value.empty:
        out["jp_margin_ratio"] = (long_value / short_value.reindex(long_value.index)).dropna()
    else:
        out["jp_margin_ratio"] = pd.Series(dtype=float)

    credit = asia_leverage_column(asia_leverage_csv(workspace, "kr", "kofia_credit.csv"), "credit_loan_total")
    out["kr_credit_total"] = credit / 1e6 if not credit.empty else credit  # 百万₩ → 兆₩
    kospi_mcap = asia_leverage_column(asia_leverage_csv(workspace, "kr", "kofia_kospi.csv"), "mcap")
    kosdaq_mcap = asia_leverage_column(asia_leverage_csv(workspace, "kr", "kofia_kosdaq.csv"), "mcap")
    if not credit.empty and not kospi_mcap.empty and not kosdaq_mcap.empty:
        mcap = kospi_mcap.add(kosdaq_mcap.reindex(kospi_mcap.index), fill_value=None).dropna()
        common = credit.index.intersection(mcap.index)
        out["kr_credit_ratio"] = (credit.loc[common] / mcap.loc[common] * 100).dropna()
    else:
        out["kr_credit_ratio"] = pd.Series(dtype=float)

    fin_value = asia_leverage_column(asia_leverage_csv(workspace, "tw", "margin_market.csv"), "fin_value_kntd")
    out["tw_margin_balance"] = fin_value / 1e5 if not fin_value.empty else fin_value  # 千NT$ → 亿NT$
    out["tw_usage_p90"] = asia_leverage_column(
        asia_leverage_csv(workspace, "tw", "margin_issues_derived.csv"), "usage_p90"
    )
    return out


def asia_country_charts(workspace: Path, config: dict[str, Any], refresh: bool = True) -> dict[str, Any]:
    """每个国家的一级杠杆指标各配一张图，右轴统一叠该国 MSCI 指数。

    左右两轴量纲完全不同（兆円 / 倍数 / % 对指数点位），所以不做零线对齐：
    MSCI 指数没有有意义的零点，硬对齐会把点位轴一路拉到 0，指数被压成一条平线。
    这两条线只能比时点和方向，不能比幅度，图注里写死了这句。
    """
    spec = config.get("asia_leverage_charts") or {}
    countries = spec.get("countries") or []
    start = spec.get("start", "2023-01-01")
    series_map = asia_leverage_series(workspace)
    html_parts: list[str] = []
    charts = 0
    missing: list[str] = []
    stale: list[str] = []
    for country in countries:
        index_series, state = fetch_msci_levels(
            country["msci_code"], country["msci_currency"], start, workspace, refresh=refresh
        )
        index_series = index_series.loc[index_series.index >= start] if not index_series.empty else index_series
        if state == "missing":
            missing.append(f"{country['name']}MSCI")
        elif state == "stale":
            stale.append(f"{country['name']}MSCI")
        blocks: list[str] = []
        for indicator in country.get("indicators", []):
            raw = series_map.get(indicator["id"], pd.Series(dtype=float))
            raw = raw.loc[raw.index >= start] if not raw.empty else raw
            if raw.empty:
                missing.append(f"{country['name']}·{indicator['label']}")
                continue
            payload = [series_payload(raw, f"{indicator['label']}（{indicator['unit']}）", indicator["color"], "left", True, 1400)]
            if not index_series.empty:
                payload.append(series_payload(index_series, country["msci_name"], country.get("msci_color", "#8fb7ff"), "right", False, 1400))
            span = f"{iso_date(raw.index.min())} 起"
            # 脚注只留口径一行；指标来源、回测评价等长说明留在 dashboard_config.json 的 note 和 methodology.md。
            note = (
                f"左轴 {indicator['label']}（{indicator['unit']}），右轴 {country['msci_name']}，{span}。两轴量纲不同，只比拐点和方向。"
            )
            if index_series.empty:
                note = f"⚠️ {country['msci_name']}取数失败，本图只有左轴。" + note
            blocks.append(chart(f"{country['name']}：{indicator['label']} vs {country['msci_name']}", payload, note, None, 340))
            charts += 1
        if blocks:
            html_parts.append(f'<h2>{html.escape(country["name"])}</h2>' + "".join(blocks))
    return {
        "html": "".join(html_parts),
        "charts": charts,
        "missing": missing,
        "stale": stale,
        "note": spec.get("note", ""),
    }


def crowding_board_rows(rows: list[dict[str, Any]], step: int = CROWD_SPARK_STEP) -> list[dict[str, Any]]:
    """Compact every vendor row into what the in-page board needs.

    The 1-year history only feeds a 174px sparkline, so keeping all ~241 daily points
    would spend ~700KB of page weight below one point per pixel. Every step-th point is
    kept and the newest day is always appended, so the last dot and the table number
    always come from the same day.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        history = [p for p in (row.get("history_1y") or []) if len(p) >= 3]
        thinned = history[::step]
        if history and (not thinned or thinned[-1] is not history[-1]):
            thinned = thinned + [history[-1]]
        item = {k: row.get(k) for k in CROWD_BOARD_FIELDS}
        item["hp"] = [round(float(p[1]), 2) for p in thinned if finite(p[1]) is not None]
        item["hc"] = [round(float(p[2]), 2) for p in thinned if finite(p[2]) is not None]
        item["hs"] = str(history[0][0]) if history else ""
        item["he"] = str(history[-1][0]) if history else ""
        if len(item["hp"]) != len(item["hc"]):
            item["hp"], item["hc"] = [], []
        out.append(item)
    return out


def crowding_board(crowding: dict[str, Any]) -> str:
    """Full vendor crowding board, ported into the China tab.

    Replaces the fixed 11-name table. Rows carry the vendor's own 0-100 score, so no
    local re-scaling happens here; sorting, filtering and the sparkline are presentation.
    """
    rows = crowding.get("rows", [])
    if not rows:
        return '<article class="panel"><h3>指数与板块拥挤度</h3><p class="empty">拥挤度镜像本次不可用；看板不用旧值冒充当日值。</p></article>'
    board = crowding_board_rows(rows)
    overseas = sum(1 for r in board if r.get("category") == "海外")
    domestic = [r for r in board if r.get("category") != "海外"]
    # Two fixed ranks, both low-to-high crowding. Vendor rank counts overseas indices, so
    # a China-only view built on it would show gaps; the checkbox switches which one shows.
    for key, subset in (("rk_cn", domestic), ("rk_all", board)):
        for i, r in enumerate(sorted(subset, key=lambda x: (finite(x.get("crowding_percentile")) is None, finite(x.get("crowding_percentile")) or 0.0)), 1):
            r[key] = i
    levels = ["低", "较低", "中等", "偏高", "高"]
    counts = {lv: sum(1 for r in domestic if str(r.get("crowding_level")) == lv) for lv in levels}
    no_history = sum(1 for r in domestic if len(r.get("hp") or []) < 2)
    stats = "".join(
        f'<div class="cb-stat"><span>{html.escape(label)}</span><b>{value}</b></div>'
        for label, value in [("中国指数", len(domestic)), *[(lv, counts[lv]) for lv in levels], ("暂无一年历史", no_history)]
    )
    cats = [c for c in ["宽基", "行业", "主题", "风格", "其他"] if any(r.get("category") == c for r in domestic)]
    cat_options = '<option value="">全部类型</option>' + "".join(f"<option>{html.escape(c)}</option>" for c in cats)
    level_options = '<option value="">全部档位</option>' + "".join(f"<option>{lv}</option>" for lv in levels)
    encoded = json.dumps(board, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    trade_date = html.escape(str(crowding.get("trade_date", "未知")))
    return (
        '<article class="panel cb-panel"><div class="panel-head"><h3>指数与板块拥挤度</h3>'
        f'<div class="shape">截止 {trade_date} · 中国 {len(domestic)} 个指数</div></div>'
        f'<div class="cb-stats">{stats}</div>'
        '<div class="cb-controls">'
        '<input id="cbSearch" type="search" placeholder="搜索指数名称或代码" autocomplete="off">'
        f'<select id="cbCat">{cat_options}</select>'
        f'<select id="cbLevel">{level_options}</select>'
        '<select id="cbSort"><option value="asc">拥挤度：低到高</option><option value="desc">拥挤度：高到低</option>'
        '<option value="changeDesc">当日涨幅：高到低</option><option value="probDesc">历史概率：高到低</option></select>'
        f'<label class="cb-toggle"><input id="cbOverseas" type="checkbox">含海外 {overseas} 个</label>'
        '</div>'
        '<div class="cb-shell"><table><thead><tr><th>排名</th><th>指数</th><th>类型</th><th>拥挤度</th>'
        '<th><i class="cb-dot cb-dot-price"></i>价格 <i class="cb-dot cb-dot-crowd"></i>拥挤度（1年）</th>'
        '<th>原版历史概率</th><th>回测周期</th><th>当日涨跌</th></tr></thead><tbody id="cbRows"></tbody></table>'
        '<div id="cbEmpty" class="empty" hidden>没有符合条件的指数</div></div>'
        f'<script id="cbData" type="application/json">{encoded}</script>'
        '<p class="note">镜像原版公开排名字段，中国指数内部按拥挤度从低到高排序。'
        f'小图每 {CROWD_SPARK_STEP} 个交易日取一点，只看形状：蓝线价格、橙线拥挤度，两线高度不可互比，读数以表格为准。悬停或聚焦可放大。</p>'
        '</article><div id="cbZoom" class="cb-zoom" hidden></div>'
    )


def match_crowding(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    # 主匹配走 index_code；这里只是兜底，供应商偶尔改展示名时不至于整行消失。
    aliases = {
        "创业板指": ["创业板指"], "科创50": ["科创50"], "国证2000": ["国证2000"], "中证2000": ["中证2000"],
        "通信设备": ["通信设备", "5G通信"], "中证全指半导体": ["中证全指半导体", "全指半导体"],
        "红利低波": ["红利低波"], "CS电池": ["CS电池"], "恒生科技": ["恒生科技"],
        "恒生消费": ["恒生消费"], "恒生医疗": ["恒生医疗保健", "恒生医疗"],
        "港股通高股息低波": ["港股通高股息低波动", "港股通高股息低波"], "上证指数": ["上证指数"], "沪深300": ["沪深300"],
    }
    names = aliases.get(wanted, [wanted])
    for row in rows:
        if any(name == row.get("index_name") for name in names):
            return row
    for row in rows:
        if any(name in str(row.get("index_name", "")) for name in names):
            return row
    return None


def parse_asia_card(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    countries = {"中国A股": "中国A股", "韩国": "韩国", "日本": "日本", "台湾": "台湾"}
    result = []
    for label, name in countries.items():
        pattern = rf"## .*?{re.escape(label)} — 拥挤度 (\d+)/100\(([^)]+)\) · ([^\n]+)(.*?)(?=\n## |\Z)"
        m = re.search(pattern, text, re.S)
        if not m:
            continue
        block = m.group(4)
        core = re.search(r"\*\*核心指标\*\*: ([^\n]+)", block)
        result.append({"name": name, "score": int(m.group(1)), "level": m.group(2), "state": m.group(3), "core": core.group(1) if core else ""})
    return result


ASIA_COMPOSITE_KEYS = {"中国A股": "cn", "韩国": "kr", "日本": "jp", "台湾": "tw"}


def asia_composite_history(path: Path) -> dict[str, pd.Series]:
    """日韩台（含中国A股）拥挤度综合分的历史序列。

    由 scripts/asia_leverage_crossview.py 每次运行时按全量历史重算后落盘，所以它一次成型、
    不靠逐日快照累积。看板只读不写，也不在这里复制那套加权分位逻辑——同一个分数只能有一个定义。
    """
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    if "date" not in frame.columns:
        return {}
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date").set_index("date")
    return {col: pd.to_numeric(frame[col], errors="coerce").dropna() for col in frame.columns}


def stock_metrics(prices: dict[str, pd.Series], config: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in config["single_stocks"]:
        ticker, benchmark = item["ticker"], item["benchmark"]
        price = prices.get(ticker, pd.Series(dtype=float)).dropna()
        bench = prices.get(benchmark, pd.Series(dtype=float)).dropna()
        if len(price) < 201:
            result.append({**item, "available": False})
            continue
        ma200 = price.rolling(200).mean()
        gate = bool(price.iloc[-1] > ma200.iloc[-1] and ma200.iloc[-1] > ma200.iloc[-21])
        rsi = finite(wilder_rsi(price, 14).iloc[-1])
        rel20 = None
        if len(bench) > 21:
            joined = pd.concat([price.rename("s"), bench.rename("b")], axis=1).dropna()
            if len(joined) > 21:
                rel20 = finite(((joined["s"].iloc[-1] / joined["s"].iloc[-21]) / (joined["b"].iloc[-1] / joined["b"].iloc[-21]) - 1) * 100)
        result.append({
            **item, "available": True, "as_of": iso_date(price.index.max()), "close": finite(price.iloc[-1]),
            "gate": gate, "rsi14": rsi, "ret20": pct_change(price, 20), "ret60": pct_change(price, 60),
            "ma200_dev": finite((price.iloc[-1] / ma200.iloc[-1] - 1) * 100), "rel20": rel20,
        })
    return result


def china_stock_indicators(stock: pd.Series, benchmark: pd.Series, period: int = 30) -> tuple[pd.Series, pd.Series]:
    """Keep the stock's full calendar; never treat a long gap as one daily move."""
    stock = pd.to_numeric(stock, errors="coerce").dropna().sort_index()
    benchmark = pd.to_numeric(benchmark, errors="coerce").dropna().sort_index()

    def blocks(values: pd.Series, compute) -> pd.Series:
        if values.empty:
            return pd.Series(dtype=float)
        groups = values.index.to_series().diff().dt.days.fillna(0).gt(14).cumsum()
        return pd.concat([compute(part) for _, part in values.groupby(groups)]).dropna()

    deviation = blocks(stock, lambda p: (p / p.rolling(200).mean() - 1) * 100)
    pair = pd.concat([stock.rename("stock"), benchmark.rename("bench")], axis=1).dropna()
    relative = (pair["stock"] / pair["bench"]).replace([np.inf, -np.inf], np.nan).dropna()
    return deviation, blocks(relative, lambda p: wilder_rsi(p, period))


def flow_endpoints(host: str = FLOW_HOST) -> dict[str, str]:
    """Both the build-time snapshot and the browser refresh read these two URLs.

    They are generated once here and handed to the page, so the embedded snapshot
    and the live refresh can never drift onto different endpoints.
    """
    # s:8 是申万三级行业（337 个）。t:2 会把一级、二级、三级混在一张榜里，
    # 医药生物 与 医疗研发外包 会同时出现在图上，等于把同一批钱画两遍。
    # clist 单页最多返回 100 行，所以流入用 po=1、流出用 po=0 各取一次，
    # 不能只拉一次再取尾部——尾部只是第 100 名，不是流出最多的板块。
    rank = (
        f"https://{host}/api/qt/clist/get?fid=f62&pz=100&pn=1&np=1&fltt=2&invt=2"
        "&fs=m%3A90%2Bs%3A8&fields=f12%2Cf14%2Cf62&po="
    )
    return {
        "rankTop": rank + "1",
        "rankBottom": rank + "0",
        "kline": (
            f"https://{host}/api/qt/stock/fflow/kline/get?lmt=0&klt=1"
            "&fields1=f1%2Cf2%2Cf3%2Cf7&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56&secid=90."
        ),
    }


def flow_http_json(url: str, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def parse_flow_kline(raw: dict[str, Any]) -> tuple[list[list[Any]], str]:
    """Minute rows are `time,主力,小单,中单,大单,超大单` in yuan; keep 主力 only."""
    lines = ((raw.get("data") or {}) or {}).get("klines") or []
    points: list[list[Any]] = []
    stamp = ""
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        value = finite(pd.to_numeric(parts[1], errors="coerce"))
        if value is None:
            continue
        stamp = parts[0]
        points.append([parts[0][-5:], round(value / FLOW_YI, 3)])
    return points, stamp


def fetch_fund_flow(side_n: int = FLOW_SIDE_N, host: str = FLOW_HOST) -> dict[str, Any]:
    """Server-side snapshot of the same data the browser refreshes.

    Never raises: a failed snapshot leaves the panel empty and the page says so
    instead of stalling the whole build.
    """
    urls = flow_endpoints(host)
    snapshot: dict[str, Any] = {"ok": False, "error": "", "asof": "", "date": "", "boards": [], "rank": {"top": [], "bottom": []}}

    def rank_side(url: str) -> list[dict[str, Any]]:
        rows = ((flow_http_json(url).get("data") or {}) or {}).get("diff") or []
        out = []
        for row in rows:
            net = finite(pd.to_numeric(row.get("f62"), errors="coerce"))
            if net is None or not row.get("f12"):
                continue
            out.append({"code": str(row["f12"]), "name": str(row.get("f14", row["f12"])), "net": round(net / FLOW_YI, 2)})
        return out

    try:
        top_rows, bottom_rows = rank_side(urls["rankTop"]), rank_side(urls["rankBottom"])
    except Exception as exc:
        # 2026-10 起东财把本机 IP 在 push2 和 push2delay 上都封了，整个域名回 nginx 502。
        # 页面打开时浏览器从读者自己的网络直连东财，面板照常出数，缺的只是内嵌兜底快照。
        # 所以 502 记为 blocked：只进构建日志的 notices，不再天天进卡片 warnings。
        # 其他错误（超时、解析失败、别的状态码）仍按故障进 warnings。
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 502:
            snapshot["blocked"] = True
            snapshot["error"] = FLOW_BLOCKED_NOTE
        else:
            snapshot["error"] = f"板块排名接口失败：{exc}"
        return snapshot
    if not top_rows and not bottom_rows:
        snapshot["error"] = "板块排名接口返回空列表"
        return snapshot
    snapshot["rank"] = {"top": top_rows[:10], "bottom": bottom_rows[:10]}
    picks = top_rows[:side_n] + bottom_rows[:side_n]
    seen: set[str] = set()
    picks = [p for p in picks if not (p["code"] in seen or seen.add(p["code"]))]

    def one(board: dict[str, Any]) -> dict[str, Any]:
        try:
            points, stamp = parse_flow_kline(flow_http_json(urls["kline"] + board["code"]))
        except Exception:
            points, stamp = [], ""
        return {**board, "points": points, "stamp": stamp}

    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(one, picks))
    boards = [b for b in done if b["points"]]
    if not boards:
        snapshot["error"] = "分钟资金流接口没有返回任何曲线"
        return snapshot
    stamp = max((b["stamp"] for b in boards if b["stamp"]), default="")
    order = {b["code"]: i for i, b in enumerate(picks)}
    boards.sort(key=lambda b: order.get(b["code"], 99))
    snapshot.update({
        "ok": True,
        "asof": stamp,
        "date": stamp.split(" ")[0] if stamp else "",
        "boards": [{k: b[k] for k in ("code", "name", "net", "points")} for b in boards],
    })
    return snapshot


def fund_flow_panel(snapshot: dict[str, Any], side_n: int = FLOW_SIDE_N, host: str = FLOW_HOST) -> str:
    payload = {
        "cfg": {
            **flow_endpoints(host),
            "sideN": side_n,
            "refreshMs": FLOW_REFRESH_MIN * 60_000,
            "sessions": FLOW_SESSIONS,
            "tz": FLOW_TZ,
        },
        "snapshot": snapshot,
    }
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=True)
    note = (
        f"东方财富申万三级板块主力净流入当日累计，单位亿元；画净流入、净流出各 {side_n} 个。"
        f"绿=净流入、红=净流出。盘中每 {FLOW_REFRESH_MIN} 分钟刷新。"
    )
    return (
        '<article class="panel flow-panel"><div class="panel-head"><h3>A股盘中资金流向</h3>'
        '<div class="flow-status" data-flow-status>加载中…</div></div>'
        f'<div class="flow" data-flow="{encoded}"><canvas></canvas><div class="tooltip"></div></div>'
        '<div class="flow-rank" data-flow-rank></div>'
        f'<p class="note">{html.escape(note)}</p></article>'
    )


def turnover_minute(label: str) -> int:
    """把 09:30–15:00 的时钟折成 0–240，午休 11:30–13:00 不占横轴（和资金流面板同一映射）。"""
    hh, mm = label.split(":")[:2]
    v = int(hh) * 60 + int(mm)
    return v - 570 if v <= 690 else v - 660


def sina_five_minute(symbol: str, bars: int, timeout: int = TURNOVER_TIMEOUT) -> list[dict[str, Any]]:
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=5&ma=no&datalen={int(bars)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    return payload if isinstance(payload, list) else []


def fetch_market_turnover(config: dict[str, Any]) -> dict[str, Any]:
    """A股全市场累计成交额：今日曲线 + 昨日 + 前 N 个完整交易日的同一时刻中位数。

    两市合计只认交易所全口径的两个指数（上证综指、深证综指）。深证成指只有 500 只成分，
    新浪按成分汇总的成交额比深市实际少四成，拿来当“深市”会把两市合计系统性画低。
    历史用新浪 5 分钟 K 线（带 amount，可回溯约 21 个交易日）；盘中的今日曲线由浏览器直连
    东方财富分钟线自行刷新，服务端这份是收盘/断网时的兜底，不是唯一来源。
    """
    spec = config.get("cn_turnover") or {}
    if not spec.get("enabled", True):
        return {"ok": False, "error": "配置里已关闭该面板", "days": [], "today": [], "prev": [], "median": [], "asof": ""}
    components = spec.get("components") or []
    bars = int(spec.get("sina_bars", 1023))
    baseline_days = int(spec.get("baseline_days", 20))
    try:
        buckets: dict[str, dict[str, float]] = {}
        for comp in components:
            rows = sina_five_minute(comp["sina_symbol"], bars)
            if not rows:
                raise RuntimeError(f"{comp['name']}（{comp['sina_symbol']}）5分钟K线返回空")
            for row in rows:
                stamp = str(row.get("day", ""))
                amount = finite(row.get("amount"))
                if len(stamp) < 16 or amount is None:
                    continue
                buckets.setdefault(stamp[:10], {}).setdefault(stamp[11:16], 0.0)
                buckets[stamp[:10]][stamp[11:16]] += amount
    except Exception as exc:  # noqa: BLE001 - surfaced in warnings, never silently swallowed
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "days": [], "today": [], "prev": [], "median": [], "asof": ""}

    def curve(day: str) -> list[list[Any]]:
        total = 0.0
        out: list[list[Any]] = []
        for label in sorted(buckets[day]):
            total += buckets[day][label]
            out.append([label, round(total / TURNOVER_UNIT, 4)])
        return out

    days = sorted(buckets)
    if not days:
        return {"ok": False, "error": "5分钟K线里没有可用交易日", "days": [], "today": [], "prev": [], "median": [], "asof": ""}
    today = days[-1]
    complete = [d for d in days[:-1] if len(buckets[d]) >= TURNOVER_FULL_BARS][-baseline_days:]
    grid: dict[str, list[float]] = {}
    for day in complete:
        for label, value in curve(day):
            grid.setdefault(label, []).append(float(value))
    median = [[label, round(float(np.median(vals)), 4)] for label, vals in sorted(grid.items()) if vals]
    today_curve = curve(today)
    return {
        "ok": True,
        "error": "",
        "date": today,
        "asof": f"{today} {today_curve[-1][0]}" if today_curve else today,
        "today": today_curve,
        "prev": curve(complete[-1]) if complete else [],
        "prev_date": complete[-1] if complete else "",
        "median": median,
        "days": complete,
        "full_bars": TURNOVER_FULL_BARS,
    }


def turnover_baseline_note(names: str, snapshot: dict[str, Any]) -> str:
    """量能脚注。有完整交易日基数时沿用原句；缺失时不把空基数写成 0。"""
    days = snapshot.get("days")
    day_count = len(days) if isinstance(days, list) else None
    head = f"{names}合计成交额，09:30 起累计，单位万亿元。"
    refresh = f"盘中每 {FLOW_REFRESH_MIN} 分钟刷新。"
    if day_count:
        return (
            f"{head}实线今天，灰线上一交易日，虚线前 {day_count} 个交易日同一时刻的中位数。{refresh}"
        )
    if not snapshot.get("ok"):
        error = str(snapshot.get("error") or "未返回可用数据").strip() or "未返回可用数据"
        return f"{head}量能数据缺失：{error}。{refresh}"
    return f"{head}完整交易日参考数据缺失，没有可计算的前序中位数。{refresh}"


def turnover_freshness_detail(snapshot: dict[str, Any]) -> str:
    """新鲜度表的量能说明。有完整交易日基数时沿用原句；没有基数时不写成前 0 个。"""
    days = snapshot.get("days")
    day_count = len(days) if isinstance(days, list) else None
    refresh = f"页面每{FLOW_REFRESH_MIN}分钟自刷新"
    if day_count:
        return f"沪深两市累计成交额；基线取前{day_count}个完整交易日，{refresh}"
    if not snapshot.get("ok"):
        return f"沪深两市累计成交额；量能数据缺失，{refresh}"
    return f"沪深两市累计成交额；完整交易日参考数据缺失，{refresh}"


def turnover_panel(snapshot: dict[str, Any], config: dict[str, Any]) -> str:
    spec = config.get("cn_turnover") or {}
    components = spec.get("components") or []
    payload = {
        "cfg": {
            "trend": f"https://{FLOW_HOST}/api/qt/stock/trends2/get?fields1=f1,f2,f3,f7&fields2=f51,f53,f56,f57&iscr=0&ndays=1&secid=",
            "secids": [c["em_secid"] for c in components],
            "unit": TURNOVER_UNIT,
            "refreshMs": FLOW_REFRESH_MIN * 60_000,
            "sessions": FLOW_SESSIONS,
            "tz": FLOW_TZ,
        },
        "snapshot": snapshot,
    }
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=True)
    names = "＋".join(c["name"] for c in components) or "两市"
    note = turnover_baseline_note(names, snapshot)
    return (
        '<article class="panel flow-panel"><div class="panel-head"><h3>A股全市场量能（累计成交额）</h3>'
        '<div class="flow-status" data-vol-status>加载中…</div></div>'
        f'<div class="vol" data-vol="{encoded}"><canvas></canvas><div class="tooltip"></div></div>'
        '<div class="vol-stat" data-vol-stat></div>'
        f'<p class="note">{html.escape(note)}</p></article>'
    )


def level_up(value: Any, trigger: float, near: float) -> str:
    """上行阈值：到线算「触发」，差 near 以内算「接近」，其余空字符串。"""
    x = finite(value)
    if x is None:
        return ""
    if x >= trigger:
        return "触发"
    return "接近" if x >= trigger - near else ""


def level_down(value: Any, trigger: float, near: float) -> str:
    """下行阈值：跌到线算「触发」，差 near 以内算「接近」。"""
    x = finite(value)
    if x is None:
        return ""
    if x <= trigger:
        return "触发"
    return "接近" if x <= trigger + near else ""


def tech_state(z: Any) -> str:
    """科技内部轮动的五档状态，热区/冷区边界与总览温度带完全一致。"""
    x = finite(z)
    if x is None:
        return "无数据"
    if x >= DIGEST_TECH_ZONE_Z:
        return "过热"
    if x <= -DIGEST_TECH_ZONE_Z:
        return "过冷"
    if x >= DIGEST_TECH_LEAN_Z:
        return "偏热"
    if x <= -DIGEST_TECH_LEAN_Z:
        return "偏冷"
    return "中性"


def digest_lines(signals: dict[str, Any]) -> list[str]:
    """把 signals.json 渲染成飞书卡片的 markdown。

    只读 signals.json，不碰行情：卡片里的每个数字都必须能在同一份 JSON 里找到，
    这样卡片和当天那版页面读的是同一批值，对不上就是 build 出了问题，而不是文案漂移。
    """
    out = [f"# 📈 情绪看板速览 — {signals.get('date', '未知日期')}", ""]

    us = signals.get("us_market", {})
    out += ["## 美股市场", ""]
    topbottom = us.get("topbottom", {})
    # 顶部一行 + 顶部环境一行都由 us_top_framework 渲染，读的是 signals.json 里同一份 top_framework。
    out.extend(top_digest_lines(topbottom, fmt))
    out.append("- 美股底部：" + (signals_join(topbottom.get("bottom_hits", []))
                             or f"无活动信号（{topbottom.get('bottom_reading', '读数缺失')}）"))
    environment_line = bottom_environment_digest_line(topbottom, fmt)
    if environment_line:
        out.append(environment_line)
    out.extend(projection_digest_lines(topbottom.get("bottom_framework") or {}))
    out.extend(alert_projection_digest_lines(topbottom.get("bottom_framework") or {}))
    out.append("- Momentum：" + (signals_join(us.get("momentum", {}).get("hits", []))
                             or f"无信号（{us.get('momentum', {}).get('reading', '读数缺失')}）"))
    # 纳指/等权 2026-09-30 从 Momentum 行拆出；旧版 signals.json 没有这一块，就不写这一行。
    if "ndx_spw" in us:
        out.append("- 纳指/等权：" + (signals_join(us["ndx_spw"].get("hits", []))
                                  or f"无信号（{us['ndx_spw'].get('reading', '读数缺失')}）"))
    rot = us.get("tech_rotation", [])
    out.append("- 科技内部轮动：" + (" · ".join(f"{x['name']} {x['state']}" for x in rot) if rot else "读数缺失"))
    out.append("- 行业：" + (signals_join(us.get("sectors", {}).get("hits", []))
                          or f"无过热过冷（{us.get('sectors', {}).get('reading', '11 个一级行业相对SPX RSI30 全在中间区')}）"))
    if "gs_prime" in us:
        gp = us["gs_prime"]
        out.append("- GS Prime：" + (signals_join(gp.get("hits", []))
                                    or f"无极端信号（{gp.get('reading', '读数缺失')}）"))
    out.append("")

    st = signals.get("us_stocks", {})
    out += ["## 美股个股", ""]
    out.append("- 六大科技：" + (signals_join(st.get("megacap", {}).get("hits", []))
                            or f"无过热过冷（{st.get('megacap', {}).get('reading', '读数缺失')}）"))
    # TSM 与 AFRM 只在触发或接近时出现。没信号时整行不写——用户 2026-08-12 的口径：
    # 「未接近」那种行天天一样，看多了会连带把真出信号的那天一起略过去。
    for key, label in (("TSM", "TSM"), ("AFRM", "AFRM")):
        hit = st.get(key, {}).get("hit")
        if hit:
            out.append(f"- {label}：{hit}")
    out.append("")

    cn = signals.get("cn_a", {})
    out += ["## A股市场", ""]
    hits = cn.get("hits", [])
    out.append("- " + (signals_join(hits) if hits else f"无信号（{cn.get('reading', '读数缺失')}）"))
    out.append("")

    hk = signals.get("hk", {})
    out += ["## 港股市场", ""]
    out.append("- " + (signals_join(hk.get("hits", [])) if hk.get("hits") else f"无信号（{hk.get('reading', '读数缺失')}）"))
    out.append("")

    asia = signals.get("asia", {})
    out += ["## 日韩台市场", ""]
    out.append("- " + (signals_join(asia.get("hits", [])) if asia.get("hits") else f"无信号（{asia.get('reading', '读数缺失')}）"))
    out.append("")

    # 「数据与口径」整栏已按用户 2026-08-12 的要求去掉：截止日、来源与阈值说明都在页面上，卡片不复述。
    # warnings 留着且只在非空时出现——这是卡片上唯一能看出「某块数据今天没取到」的地方，
    # 一起删掉的话，缺数据的卡片和正常的卡片长得一模一样（BBG 卡三天没人发现就是这么来的）。
    for line in signals.get("warnings", []):
        out.append(f"- ⚠️ {line}")
    return out


def signals_join(hits: list[str]) -> str:
    return "；".join(hits)


# ── 卡片 2.0：顶部、底部各成一节；有信号的行着色，无信号的读数缩成灰色小字（用户 2026-09-07） ──
# 文字全部复用 digest_lines 用的同一批函数和同一份 signals.json，只是分组和着色不同：
# digest.md 仍是五个栏目的纯文本版，digest.card.json 是发到飞书的版式。两边数字同源，不会各算一遍。
DIGEST_CARD_HOT = "red"
DIGEST_CARD_COLD = "green"
DIGEST_CARD_WARN = "orange"
DIGEST_CARD_QUIET = "grey"
DIGEST_CARD_HOT_WORDS = ("过热", "过高", "超买", "偏热", "过激", "高位", "极度拥挤")
DIGEST_CARD_COLD_WORDS = ("过冷", "过低", "超卖", "偏冷", "极度出清")
DIGEST_CARD_TITLE_TOP = "🔺 美股顶部"
DIGEST_CARD_TITLE_BOTTOM = "🔻 美股底部"
DIGEST_CARD_TITLE_US_OTHER = "美股其他"
DIGEST_CARD_TITLE_ASIA = "A股 · 港股 · 日韩台"


def digest_tone(text: str) -> str:
    """按信号方向给颜色：热/高/超买用红，冷/低/超卖用绿，分不清方向的用橙。"""
    if any(word in text for word in DIGEST_CARD_HOT_WORDS):
        return DIGEST_CARD_HOT
    if any(word in text for word in DIGEST_CARD_COLD_WORDS):
        return DIGEST_CARD_COLD
    return DIGEST_CARD_WARN


def paint(text: str, tone: str | None) -> str:
    return f"<font color='{tone}'>{text}</font>" if tone else text


def paint_hits(hits: list[str]) -> str:
    return signals_join([paint(hit, digest_tone(hit)) for hit in hits])


def strip_bullet(line: str) -> str:
    return line[2:] if line.startswith("- ") else line


def digest_card_sections(signals: dict[str, Any]) -> list[dict[str, Any]]:
    """卡片 2.0 的分节模型：title / lead（已着色）/ lines（已着色，正常字号）/ quiet（灰色小字）。"""
    us = signals.get("us_market", {})
    topbottom = us.get("topbottom", {})
    sections: list[dict[str, Any]] = []

    # 顶部：状态机摘要做本节第一行，读数、环境、put 保护没在亮时都缩成小字；门槛和盘中情景保持正常字号。
    top_fw = topbottom.get("top_framework") or {}
    top_active = bool(top_fw.get("active"))
    top_lines = top_digest_lines(topbottom, fmt)
    head = top_fw.get("summary") or "无活动信号"
    top: dict[str, Any] = {"title": DIGEST_CARD_TITLE_TOP, "lead": "", "lines": [], "quiet": []}
    if not top_fw:
        top["lead"] = paint("读数缺失", DIGEST_CARD_WARN)
    else:
        top["lead"] = paint(head, DIGEST_CARD_HOT if top_active else None)
        prefix = f"- 美股顶部：{head}；"
        first = top_lines[0] if top_lines else ""
        if first.startswith(prefix) and first[len(prefix):]:
            top["quiet"].append(first[len(prefix):])
        environment = top_fw.get("environment") or {}
        hedge = top_fw.get("put_hedge") or {}
        for line in top_lines[1:]:
            text = strip_bullet(line)
            if text.startswith("顶部环境："):
                (top["lines"] if environment.get("weak_now") else top["quiet"]).append(
                    paint(text, DIGEST_CARD_WARN) if environment.get("weak_now") else text)
            elif text.startswith("指数 put 保护："):
                (top["lines"] if hedge.get("on") else top["quiet"]).append(
                    paint(text, DIGEST_CARD_WARN) if hedge.get("on") else text)
            else:
                top["lines"].append(text)
    sections.append(top)

    # 底部：活动信号做第一行并标绿；没信号时第一行写「无活动信号」，当前读数缩成小字。
    bottom_fw = topbottom.get("bottom_framework") or {}
    bottom_active = bool(bottom_fw.get("active"))
    bottom_hits = topbottom.get("bottom_hits", [])
    bottom: dict[str, Any] = {"title": DIGEST_CARD_TITLE_BOTTOM, "lead": "", "lines": [], "quiet": []}
    if bottom_hits:
        bottom["lead"] = paint(signals_join(bottom_hits), DIGEST_CARD_COLD if bottom_active else None)
    else:
        # 无信号但盘中已破预警线（用户 2026-10-14）：标题行补一句绿字，情景和门槛按正常字号列在下面。
        alert_lead = alert_projection_lead(bottom_fw)
        bottom["lead"] = "无活动信号" + (f" · {paint(alert_lead, DIGEST_CARD_COLD)}" if alert_lead else "")
        bottom["quiet"].append(topbottom.get("bottom_reading", "读数缺失"))
    environment_line = bottom_environment_digest_line(topbottom, fmt)
    if environment_line:
        env = topbottom.get("bottom_environment") or bottom_fw.get("environment") or {}
        text = strip_bullet(environment_line)
        if env.get("tone") not in (None, "", "neutral"):
            bottom["lines"].append(paint(text, DIGEST_CARD_WARN))
        else:
            bottom["quiet"].append(text)
    bottom["lines"].extend(strip_bullet(x) for x in projection_digest_lines(bottom_fw))
    bottom["lines"].extend(strip_bullet(x) for x in alert_projection_digest_lines(bottom_fw))
    sections.append(bottom)

    # 美股其他：Momentum、纳指/等权、科技轮动、行业、六大科技，TSM/AFRM 只在触发或接近时出现（同 digest_lines）。
    other: dict[str, Any] = {"title": DIGEST_CARD_TITLE_US_OTHER, "lead": "", "lines": [], "quiet": []}
    momentum = us.get("momentum", {})
    if momentum.get("hits"):
        other["lines"].append("Momentum：" + paint_hits(momentum["hits"]))
    else:
        other["quiet"].append(f"Momentum：无信号（{momentum.get('reading', '读数缺失')}）")
    ndx_spw = us.get("ndx_spw")
    if ndx_spw is not None:
        if ndx_spw.get("hits"):
            other["lines"].append("纳指/等权：" + paint_hits(ndx_spw["hits"]))
        else:
            other["quiet"].append(f"纳指/等权：无信号（{ndx_spw.get('reading', '读数缺失')}）")
    rotation = us.get("tech_rotation", [])
    if rotation:
        painted = []
        for item in rotation:
            state = str(item.get("state", ""))
            tone = DIGEST_CARD_HOT if state == "过热" else DIGEST_CARD_COLD if state == "过冷" else DIGEST_CARD_WARN if state in ("偏热", "偏冷") else None
            painted.append(f"{item.get('name', '')} {paint(state, tone)}")
        line = "科技内部轮动：" + " · ".join(painted)
        (other["lines"] if any(x.get("state") not in ("中性", "无数据") for x in rotation) else other["quiet"]).append(line)
    else:
        other["quiet"].append("科技内部轮动：读数缺失")
    sectors = us.get("sectors", {})
    if sectors.get("hits"):
        other["lines"].append("行业：" + paint_hits(sectors["hits"]))
    else:
        other["quiet"].append(f"行业：无过热过冷（{sectors.get('reading', '11 个一级行业相对SPX RSI30 全在中间区')}）")
    gs_prime = us.get("gs_prime")
    if gs_prime is not None:
        if gs_prime.get("hits"):
            other["lines"].append("GS Prime：" + paint_hits(gs_prime["hits"]))
            if gs_prime.get("mag7_quiet"):
                other["quiet"].append(gs_prime["mag7_quiet"])
        else:
            other["quiet"].append(f"GS Prime：无极端信号（{gs_prime.get('reading', '读数缺失')}）")
    st = signals.get("us_stocks", {})
    megacap = st.get("megacap", {})
    if megacap.get("hits"):
        other["lines"].append("六大科技：" + paint_hits(megacap["hits"]))
    else:
        other["quiet"].append(f"六大科技：无过热过冷（{megacap.get('reading', '读数缺失')}）")
    for key in ("TSM", "AFRM"):
        stock_hit = st.get(key, {}).get("hit")
        if stock_hit:
            other["lines"].append(f"{key}：" + paint(stock_hit, digest_tone(stock_hit)))
    sections.append(other)

    # 亚洲三市场合成一节，各占一行：有信号标色，没信号写当前读数。
    asia_section: dict[str, Any] = {"title": DIGEST_CARD_TITLE_ASIA, "lead": "", "lines": [], "quiet": []}
    for label, block in (("A股", signals.get("cn_a", {})), ("港股", signals.get("hk", {})), ("日韩台", signals.get("asia", {}))):
        if block.get("hits"):
            asia_section["lines"].append(f"**{label}** " + paint_hits(block["hits"]))
        else:
            asia_section["quiet"].append(f"{label}：无信号（{block.get('reading', '读数缺失')}）")
    sections.append(asia_section)
    return sections


def digest_card(signals: dict[str, Any]) -> dict[str, Any]:
    """把 signals.json 渲染成飞书 Card Schema 2.0。

    版式：标题栏颜色跟顶部/底部状态机走（顶部活动=红，底部活动=绿，都没有=indigo），
    右上角两枚标签写顶部、底部当前阶段；正文四节，节与节之间用分隔线。
    warnings 仍在最后，橙色正常字号——这是卡片上唯一能看出「某块数据今天没取到」的地方。
    只用 markdown / hr 两种元素：schema 2.0 不支持 note，小字走 text_size=notation。
    """
    topbottom = signals.get("us_market", {}).get("topbottom", {})
    top_fw = topbottom.get("top_framework") or {}
    bottom_fw = topbottom.get("bottom_framework") or {}
    top_active = bool(top_fw.get("active"))
    bottom_active = bool(bottom_fw.get("active"))
    elements: list[dict[str, Any]] = []
    for section in digest_card_sections(signals):
        block = [f"**{section['title']}**" + (f"　{section['lead']}" if section["lead"] else "")]
        block += [f"- {line}" for line in section["lines"]]
        elements.append({"tag": "markdown", "content": "\n".join(block)})
        if section["quiet"]:
            elements.append({
                "tag": "markdown",
                "text_size": "notation",
                "content": "\n".join(f"- {paint(line, DIGEST_CARD_QUIET)}" for line in section["quiet"]),
            })
        elements.append({"tag": "hr"})
    warnings = list(signals.get("warnings", []))
    if warnings:
        elements.append({"tag": "markdown", "content": "\n".join(paint(f"⚠️ {line}", DIGEST_CARD_WARN) for line in warnings)})
    while elements and elements[-1].get("tag") == "hr":
        elements.pop()

    def text_tag(text: str, color: str) -> dict[str, Any]:
        return {"tag": "text_tag", "text": {"tag": "plain_text", "content": text}, "color": color}

    template = DIGEST_CARD_HOT if top_active else DIGEST_CARD_COLD if bottom_active else "indigo"
    header = {
        "title": {"tag": "plain_text", "content": f"📈 情绪看板速览 — {signals.get('date', '未知日期')}"},
        "template": template,
        "text_tag_list": [
            text_tag(f"顶部 {top_fw.get('stage') or '读数缺失'}", DIGEST_CARD_HOT if top_active else DIGEST_CARD_QUIET),
            text_tag("底部 盘中破预警线", DIGEST_CARD_COLD) if not bottom_active and alert_projection_lead(bottom_fw)
            else text_tag(f"底部 {bottom_fw.get('stage') or '读数缺失'}", DIGEST_CARD_COLD if bottom_active else DIGEST_CARD_QUIET),
        ],
    }
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {"direction": "vertical", "vertical_spacing": "8px", "elements": elements},
    }


def build_dashboard(output: Path, use_cache: bool = True) -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    rel_signal = config["relative_signal"]
    seed = load_json(SEED_PATH)
    bbg = parse_bbg(WORKSPACE / "temp/bbg_data_latest.csv")
    # BBG CSV 是外部投喂的（Windows 机 SSH 拉回），看板这边只读不取，链路断了页面照画不误：
    # 2026-08-09 改用工作簿导出后拉取一直失败，CSV 卡在 08-07 三天没人发现。落后就进 warnings。
    bbg_stale_days = stale_days(bbg.get("as_of", ""))
    crowding = load_crowding(WORKSPACE)
    cn = load_cn_margin(WORKSPACE)
    asia = parse_asia_card(WORKSPACE / "temp/asia-leverage-crossview/card_latest.md")
    asia_charts = asia_country_charts(WORKSPACE, config)
    asia_charts_start = (config.get("asia_leverage_charts") or {}).get("start", "2023-01-01")
    cn_a_cfg = config.get("cn_a_indices", [])
    hk_cfg = config.get("hk_relative", {})
    hk_bench_cfg = hk_cfg.get("benchmark", {})
    hk_members = hk_cfg.get("members", [])
    cn_broad_cfg = config.get("cn_broad_rsi", {})
    cn_broad_members = cn_broad_cfg.get("members", [])
    history = load_index_history(
        [x["code"] for x in cn_a_cfg]
        + [x["code"] for x in cn_broad_members]
        + [x["code"] for x in hk_members]
        + ([hk_bench_cfg["code"]] if hk_bench_cfg.get("code") else []),
        WORKSPACE,
    )
    hist_frames = history["frames"]

    tickers = list(config["market_tickers"].values()) + config["mags"] + config["quantum"] + config["semiconductor_breadth"]
    tickers += [item["ticker"] for item in config.get("sp_sector_etfs", [])]
    tickers += [x["ticker"] for x in config["single_stocks"]] + [x["benchmark"] for x in config["single_stocks"]]
    tickers += [x["ticker"] for x in config.get("relative_extras", [])]
    for basket in config.get("equal_weight_baskets", []):
        tickers += list(basket["members"]) + [basket["benchmark"]]
    cache = WORKSPACE / "state/sentiment-dashboard/yfinance_prices.pkl" if use_cache else None
    # SPX/trend needs a complete pre-anchor history; other tickers simply begin
    # on their own listing dates.
    prices = download_prices(tickers, "2014-01-01", cache)
    if not prices:
        raise RuntimeError("price download returned no usable series")
    # Yahoo 000300.SS can omit weeks of daily bars while still returning today's
    # quote. Use the same continuous CSI300 close history as the A-share tab.
    csi300_history = hist_frames.get("000300.SH", pd.DataFrame())
    prices["000300.SS"] = csi300_history.get("close", pd.Series(dtype=float)).dropna()
    price_missing = [t for t in dict.fromkeys(tickers) if t not in prices]
    trend_cfg = config.get("trend_lines", [])
    # 趋势线的 OHLC 缓存不受 --no-cache 影响：download_ohlc 每次都先向 yfinance 取一遍，缓存只在
    # 取数失败时兜底。cron 走 --no-cache，没有兜底时 yfinance 抽一次风，AMZN 通道和 AFRM 支撑线
    # 会整块从页面消失而 build 仍退出 0。退回缓存的标的写进 warnings，不假装是当天数据。
    ohlc = download_ohlc(
        [x["ticker"] for x in trend_cfg],
        "2014-01-01",
        WORKSPACE / "state/sentiment-dashboard/yfinance_ohlc.pkl",
    )
    trend_stale = [t for t, f in ohlc.items() if f.attrs.get("from_cache")]
    trend_stats: dict[str, dict[str, Any]] = {}
    for item in trend_cfg:
        frame = ohlc.get(item["ticker"], pd.DataFrame())
        stats = trend_line_stats(frame, item["start"], item["mode"], item.get("fit")) if not frame.empty else None
        if stats:
            trend_stats[item["ticker"]] = {**stats, **{k: item[k] for k in ("mode", "label")}}
    trend_missing = [x["ticker"] for x in trend_cfg if x["ticker"] not in trend_stats]
    # 线只认 config 里的冻结参数。少了就报出来，不临时拟合一条顶上——那样图注会天天变。
    trend_unfrozen = [x["ticker"] for x in trend_cfg if not x.get("fit")]
    spx = prices.get("^GSPC", pd.Series(dtype=float))
    ndx = prices.get("^NDX", pd.Series(dtype=float))
    spw = prices.get("^SPXEW", pd.Series(dtype=float))
    ratio_proxy = False
    if spw.empty:
        spw = prices.get("RSP", pd.Series(dtype=float))
        ratio_proxy = True
    sox_series = prices.get("^SOX", prices.get("SOXX", pd.Series(dtype=float)))
    ratio_frame = pd.concat([ndx.rename("ndx"), spw.rename("spw")], axis=1).dropna()
    ndx_spw_rsi = wilder_rsi(ratio_frame["ndx"] / ratio_frame["spw"], 14) if not ratio_frame.empty else pd.Series(dtype=float)

    cyc_rsi14 = bbg.get("cyc_rsi", pd.Series(dtype=float))
    cyc_price = bbg.get("cyc_price", pd.Series(dtype=float))
    cyc_rsi30 = wilder_rsi(cyc_price, 30) if isinstance(cyc_price, pd.Series) else pd.Series(dtype=float)
    mom = bbg.get("momentum_rsi", pd.Series(dtype=float))
    mom_period = bbg.get("mom_period")
    q1 = bbg.get("qqq_vol_1m", pd.Series(dtype=float))
    q3 = bbg.get("qqq_vol_3m", pd.Series(dtype=float))
    ah_rsi_raw = bbg.get("ah_rsi", pd.Series(dtype=float))
    ah_rsi = -ah_rsi_raw if isinstance(ah_rsi_raw, pd.Series) else pd.Series(dtype=float)

    trend = spx_trend(seed, spx.index.max()) if not spx.empty else pd.Series(dtype=float)
    aligned_trend = trend.reindex(spx.index, method="ffill") if not spx.empty else pd.Series(dtype=float)
    trend_premium = ((spx / aligned_trend) - 1) * 100 if not aligned_trend.empty else pd.Series(dtype=float)
    trend_premium = trend_premium.replace([np.inf, -np.inf], np.nan).dropna()
    trend_dev = finite(trend_premium.iloc[-1]) if not trend_premium.empty else None

    # Composite baskets.
    mags_frame = pd.concat({t: prices[t] for t in config["mags"] if t in prices}, axis=1).dropna()
    mags_index = (mags_frame / mags_frame.iloc[0] * 100).mean(axis=1) if not mags_frame.empty else pd.Series(dtype=float)
    quantum_frame = pd.concat({t: prices[t] for t in config["quantum"] if t in prices}, axis=1).dropna()
    quantum_returns = quantum_frame.pct_change(fill_method=None).dropna()
    quantum_index = (1 + quantum_returns.mean(axis=1)).cumprod() * 100 if not quantum_returns.empty else pd.Series(dtype=float)

    qqq = prices.get("QQQ", pd.Series(dtype=float))
    qqq_price = pd.to_numeric(qqq, errors="coerce").dropna() if not qqq.empty else pd.Series(dtype=float)
    vix_price = pd.to_numeric(prices.get("^VIX", pd.Series(dtype=float)), errors="coerce").dropna()
    vvix_price = pd.to_numeric(prices.get("^VVIX", pd.Series(dtype=float)), errors="coerce").dropna()
    spx_rsi14 = wilder_rsi(spx, 14) if not spx.empty else pd.Series(dtype=float)
    qqq_rsi14 = wilder_rsi(qqq_price, 14) if not qqq_price.empty else pd.Series(dtype=float)
    qqq_drawdown_63 = (qqq_price / qqq_price.rolling(63).max() - 1.0) * 100 if not qqq_price.empty else pd.Series(dtype=float)

    stocks = stock_metrics(prices, config)
    fred_ids = ["BAMLH0A0HYM2", "DFII10", "DGS10", "DGS2"]
    fred: dict[str, pd.Series] = {sid: pd.Series(dtype=float) for sid in fred_ids}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_fred, sid): sid for sid in fred_ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                fred[sid] = future.result()
            except Exception:
                pass
    curve = pd.concat([fred["DGS10"].rename("10y"), fred["DGS2"].rename("2y")], axis=1).dropna()
    curve_series = curve["10y"] - curve["2y"] if not curve.empty else pd.Series(dtype=float)

    last_cyc14 = finite(cyc_rsi14.iloc[-1]) if isinstance(cyc_rsi14, pd.Series) and not cyc_rsi14.empty else None
    last_cyc30 = finite(cyc_rsi30.iloc[-1]) if not cyc_rsi30.empty else None
    last_mom = finite(mom.iloc[-1]) if isinstance(mom, pd.Series) and not mom.empty else None
    last_ratio = finite(ndx_spw_rsi.iloc[-1]) if not ndx_spw_rsi.empty else None
    last_margin = finite(cn["frame"]["margin_rsi14"].iloc[-1]) if not cn["frame"].empty else None
    last_ah = finite(ah_rsi.iloc[-1]) if isinstance(ah_rsi, pd.Series) and not ah_rsi.empty else None
    cyc_state, cyc_tone = status_for_rsi(last_cyc14)
    mom_state, mom_tone = status_for_rsi(last_mom)
    margin_state, margin_tone = status_for_rsi(last_margin)
    ah_state, ah_tone = status_for_ah_rsi(last_ah)

    vol_inverted = finite(q1.iloc[-1]) is not None and finite(q3.iloc[-1]) is not None and float(q1.iloc[-1]) > float(q3.iloc[-1])
    vol_pair = pd.concat([q1.rename("m1"), q3.rename("m3")], axis=1).dropna() if isinstance(q1, pd.Series) and isinstance(q3, pd.Series) and not q1.empty and not q3.empty else pd.DataFrame()
    vol_ratio_series = (vol_pair["m1"] / vol_pair["m3"]).replace([np.inf, -np.inf], np.nan).dropna() if not vol_pair.empty else pd.Series(dtype=float)
    vol_ratio = finite(vol_ratio_series.iloc[-1]) if not vol_ratio_series.empty else None

    bottom_cfg = BottomFrameworkConfig(**config["us_bottom_framework"])
    bottom_framework = evaluate_bottom_framework(
        cyc_rsi14,
        spx,
        qqq,
        vix_price,
        bottom_cfg,
        gsp_close=cyc_price,
    )
    bottom_environment, semi_breadth = build_bottom_environment(prices, config, spx, qqq, vix_price, vol_ratio_series)
    bottom_framework["environment"] = bottom_environment

    bottom_monitor_html = bottom_monitor_section(
        chart=chart, series_payload=series_payload, hline=hline, metric_card=metric_card,
        fmt=fmt, finite=finite, bottom_framework=bottom_framework, bottom_environment=bottom_environment, bottom_cfg=bottom_cfg,
        cyc_rsi14=cyc_rsi14, last_cyc14=last_cyc14, qqq_drawdown_63=qqq_drawdown_63,
        spx_rsi14=spx_rsi14, qqq_rsi14=qqq_rsi14, vix_price=vix_price, vvix_price=vvix_price,
    )

    spx_trend_chart = chart("SPX 相对GDP/CPI长期趋势的折溢价", [series_payload(trend_premium.loc[trend_premium.index >= "2015-01-01"], "SPX折溢价", "#4da3ff", "left", True, 4000)], f"折溢价=(SPX实际值÷长期趋势−1)，当前{fmt(trend_dev,1,'%')}，更新到{iso_date(spx.index.max())}。", None, 390, None, False, [hline(0, "left", "趋势线", "zero")])
    # ── 顶部状态机（CY 2026-09-03）：预警后并行检查过激与确认；过激非前置，弱预警只挂标签 ──
    top_cfg = TopFrameworkConfig(**config["us_top_framework"])
    top_framework = evaluate_top_framework(
        cyc_rsi30,
        quantum_index,
        mom,
        spx,
        qqq,
        vix_price,
        vvix_price,
        trend_premium,
        top_cfg,
    )
    vvix_vix_series = (vvix_price / vix_price.reindex(vvix_price.index)).replace([np.inf, -np.inf], np.nan).dropna() if not vvix_price.empty and not vix_price.empty else pd.Series(dtype=float)
    # 预警图画对数：线性口径下这个篮子较 60 日低点能到 +1775%，翻倍线会被压到底部。ln(2)×100=69.3 就是翻倍。
    quantum_from_low_log = (np.log(quantum_index / quantum_index.rolling(top_cfg.quantum_low_td).min()) * 100).replace([np.inf, -np.inf], np.nan).dropna() if not quantum_index.empty else pd.Series(dtype=float)
    qqq_vs_200dma = ((qqq_price / qqq_price.rolling(200).mean() - 1.0) * 100).dropna() if not qqq_price.empty else pd.Series(dtype=float)
    spx_vs_20dma = ((spx / spx.rolling(top_cfg.confirm_ma_td).mean() - 1.0) * 100).dropna() if not spx.empty else pd.Series(dtype=float)
    qqq_vs_20dma = ((qqq_price / qqq_price.rolling(top_cfg.confirm_ma_td).mean() - 1.0) * 100).dropna() if not qqq_price.empty else pd.Series(dtype=float)
    # Momentum 图 2026-09-04 从状态监控搬到顶部监控过激段（用户要求）：线画过激 75、超买 70、超卖 30，
    # 70/30 是速览 Momentum 那行用的 thresholds.rsi_hot/rsi_cold，75 是顶部状态机的过激门槛。
    momentum_chart = chart(
        "过激：Momentum RSI 与 NDX/SPW RSI14",
        [
            series_payload(mom.loc[mom.index >= "2023-01-01"], f"GSCBHMOM RSI{mom_period or '?'}", "#f4f5f7", "left", False, 1000),
            series_payload(ndx_spw_rsi.loc[ndx_spw_rsi.index >= "2023-01-01"], "NDX/SPW RSI14" + ("（RSP代理）" if ratio_proxy else ""), "#ffd447", "left", False, 1000),
        ],
        f"左轴 GSCBHMOM RSI{mom_period or '?'}（过激≥{top_cfg.excess_momentum_rsi:.0f}）与 NDX/SPW RSI14；当前{fmt(last_mom,1)}、{fmt(last_ratio,1)}。",
        None,
        390,
        [0, 100],
        False,
        [hline(top_cfg.excess_momentum_rsi, "left", f"过激{top_cfg.excess_momentum_rsi:.0f}", "hot"), hline(70, "left", "超买70", "hot"), hline(30, "left", "超卖30", "cold")],
    )
    top_monitor_html = top_monitor_section(
        chart=chart, series_payload=series_payload, hline=hline, metric_card=metric_card, fmt=fmt, finite=finite,
        top_framework=top_framework, top_cfg=top_cfg,
        cyc_rsi30=cyc_rsi30, last_cyc30=last_cyc30,
        quantum_from_low_log=quantum_from_low_log,
        qqq_price=qqq_price, qqq_rsi14=qqq_rsi14, qqq_vs_200dma=qqq_vs_200dma,
        vvix_vix_series=vvix_vix_series, momentum_rsi=mom if isinstance(mom, pd.Series) else pd.Series(dtype=float), momentum_period=mom_period,
        spx_vs_20dma=spx_vs_20dma, qqq_vs_20dma=qqq_vs_20dma,
        vol_ratio_series=vol_ratio_series, vol_ratio=vol_ratio, vol_crush=US_VOL_CRUSH,
        momentum_chart=momentum_chart,
    )

    macro_series = [
        ("美国高收益债OAS", fred["BAMLH0A0HYM2"], "百分点评价信用压力", "#ff6b6b"),
        ("美国10年实际利率", fred["DFII10"], "FRED DFII10", "#ffd447"),
        ("10Y-2Y曲线", curve_series, "DGS10-DGS2", "#4da3ff"),
    ]
    macro_html = "".join(chart(t, [series_payload(s.iloc[-756:], t, c, "left")], n, None, 240) for t, s, n, c in macro_series if not s.empty)
    if not macro_html:
        macro_html = '<article class="panel"><h3>MacroCharts</h3><p class="empty">FRED数据本次不可用；看板未用旧值冒充实时值。</p></article>'

    # 「左轴距200DMA、右轴相对基准RSI30」这一族图共用同一组参考线：美股行业、美股风格篮子、港股都在用。
    style_hlines = [hline(0, "left", "200DMA", "zero"), hline(rel_signal["top_rsi"], "right", "", "hot"), hline(rel_signal["bottom_rsi"], "right", "", "cold")]

    cn_frame = cn["frame"].loc["2023-01-01":]
    cn_margin_chart = chart("两融余额 RSI14 vs 中证1000", [series_payload(cn_frame["csi1000"], "中证1000", "#4da3ff", "left", True, 1200), series_payload(cn_frame["margin_rsi14"], "两融余额 RSI14", "#ffbf3c", "right", False, 1200)], "两融余额为沪深合计、单位亿元；RSI使用Wilder 14日口径。", [0, 100], 380, None, False, [hline(70, "right", "超买70", "hot"), hline(30, "right", "超卖30", "cold")])
    ah_charts = ""
    ah_price = bbg.get("ah_price", pd.Series(dtype=float))
    if isinstance(ah_price, pd.Series) and isinstance(ah_rsi, pd.Series):
        ah_price_plot = ah_price.loc[ah_price.index >= "2020-01-01"]
        ah_rsi_plot = ah_rsi.loc[ah_rsi.index >= "2020-01-01"]
        ah_charts += '<div class="grid two">'
        ah_charts += chart("AH溢价指数 HSAHP（倒序）", [series_payload(ah_price_plot, "HSAHP", "#ff7a3d", "left", False, 2000)], "与每日cron一致：纵轴倒序，向上代表AH溢价收窄，向下代表溢价扩大。", None, 330, None, True)
        ah_charts += chart("AH溢价 RSI30（Bloomberg口径）", [series_payload(ah_rsi_plot, "RSI30", "#4da3ff", "left", True, 2000)], f"与每日cron一致：BBG HT006直接取负，范围-100~0；超买-36、超卖-60，当前{fmt(last_ah,2)}。", None, 330, [-80, -20], False, [hline(-36, "left", "超买-36", "hot"), hline(-60, "left", "超卖-60", "cold")])
        ah_charts += '</div>'

    # ── A股：指数点位 + 拥挤度，一格一个指数，顺序写死 ──
    # 标了 featured 的那格单独提到盘中资金流上方，剩下的仍按配置顺序排成两列网格。
    cn_index_charts: list[str] = []
    cn_featured_charts: list[str] = []
    for item in cn_a_cfg:
        name, code = item["name"], item["code"]
        bucket = cn_featured_charts if item.get("featured") else cn_index_charts
        frame = hist_frames.get(code, pd.DataFrame())
        plot = frame.loc[HISTORY_START:] if not frame.empty else frame
        if plot.empty or len(plot) < 20:
            bucket.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">拥挤度长历史本次不可用（{html.escape(code)}），不用空图或旧值顶替。</p></article>')
            continue
        crowd_now = finite(plot["value"].iloc[-1])
        bucket.append(chart(
            f"{name}：指数点位 + 拥挤度",
            [
                series_payload(plot["close"], f"{name}点位", item["color"], "left", True, 1000),
                series_payload(plot["value"], "拥挤度（右轴0–100）", "#ffbf3c", "right", False, 1000),
            ],
            # 脚注只留口径；选样理由（国证2000 vs 中证2000）留在 config 的 note 和上方 cn_index_rule。
            f"{code}；左轴指数点位、右轴 0–100 拥挤度，两轴不可互比。"
            f"当前拥挤度{fmt(crowd_now,1)}，图面 {iso_date(plot.index.min())}—{iso_date(plot.index.max())}。",
            [0, 100],
            330,
            None,
            False,
            [hline(80, "right", "拥挤80", "hot"), hline(50, "right", "中位50", "zero"), hline(20, "right", "低位20", "cold")],
        ))
    cn_index_charts_html = '<div class="grid two">' + "".join(cn_index_charts) + "</div>" if cn_index_charts else ""
    cn_featured_html = "".join(cn_featured_charts)
    cn_index_rule = (
        f"彩色线是指数点位（左轴），橙线是拥挤度（右轴 0–100，≥80 拥挤、≤20 冷），{HISTORY_START} 起。"
        f"小微盘用国证2000（中证2000 的拥挤度只有 2025-09 以来）。"
    )

    # ── A股宽基：指数点位 + 自身 RSI30，一只票一张图，排在国证2000 那张拥挤度图上方 ──
    # 左轴是该指数自己的点位，右轴是它自身的 Wilder RSI。左轴没有有意义的零点，不做 0=50 对齐——
    # 硬对齐会把点位轴一路拉到 0，指数线压成平线（同 A股拥挤度那七张图）。
    # 2026-08-12 用户去掉了原来那两张「除以中证1000」的相对 RSI，只留自身动能。
    rsi_period = rel_signal["rsi_period"]
    cn_broad_hlines = [
        hline(rel_signal["top_rsi"], "right", f"强{rel_signal['top_rsi']}", "hot"),
        hline(50, "right", "中位50", "zero"),
        hline(rel_signal["bottom_rsi"], "right", f"弱{rel_signal['bottom_rsi']}", "cold"),
    ]
    cn_broad_charts: list[str] = []
    for item in cn_broad_members:
        name, code, color = item["name"], item["code"], item["color"]
        frame = hist_frames.get(code, pd.DataFrame())
        if frame.empty or len(frame) < rsi_period * 4:
            cn_broad_charts.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">指数长历史本次不可用（{html.escape(code)}），不用空图或旧值顶替。</p></article>')
            continue
        close_plot = frame["close"].loc[frame.index >= HISTORY_START]
        own_rsi = wilder_rsi(frame["close"], rsi_period)
        own_plot = own_rsi.loc[own_rsi.index >= HISTORY_START].dropna()
        cn_broad_charts.append(chart(
            f"{name}：指数点位 + RSI{rsi_period}",
            [
                series_payload(close_plot, f"{name}点位", color, "left", True, 1000),
                series_payload(own_plot, f"{name} RSI{rsi_period}", "#ffbf3c", "right", False, 1000),
            ],
            f"{code}；左轴点位、右轴自身 Wilder RSI{rsi_period}，两轴不可互比。"
            f"{iso_date(close_plot.index.max()) if not close_plot.empty else '—'} 点位"
            f"{fmt(finite(close_plot.iloc[-1]) if not close_plot.empty else None,1)}、"
            f"RSI{fmt(finite(own_plot.iloc[-1]) if not own_plot.empty else None,1)}。",
            [0, 100],
            330,
            None,
            False,
            cn_broad_hlines,
        ))
    # 一张图独占一行（用户 2026-08-12 的要求）：不套 grid two，直接把 article 交给外层 .stack，
    # 和国证2000 那张 featured 图同样的排法。并排时每张只有半个页宽，2023 年以来 600 多根点位
    # 挤在 700px 里，RSI 那根线的拐点基本看不出来。
    cn_broad_html = "".join(cn_broad_charts)
    cn_broad_rule = (
        f"彩色线是指数点位（左轴），橙线是该指数自身的 Wilder RSI{rsi_period}（右轴，参考线"
        f"{rel_signal['top_rsi']}/50/{rel_signal['bottom_rsi']}），两轴不可互比，{HISTORY_START} 起。"
    )

    # ── 港股：完全套用美股行业那套（左轴距200DMA、右轴相对基准RSI30、左0右50同高）──
    hk_bench_frame = hist_frames.get(hk_bench_cfg.get("code", ""), pd.DataFrame())
    hk_bench_name = hk_bench_cfg.get("name", "基准")
    hk_rel_rsi: dict[str, pd.Series] = {}
    hk_charts: list[str] = []
    for item in hk_members:
        name, code = item["name"], item["code"]
        frame = hist_frames.get(code, pd.DataFrame())
        if frame.empty or hk_bench_frame.empty:
            hk_charts.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">{html.escape(name)}或基准的长历史本次不可用（{html.escape(code)}），不用空图顶替。</p></article>')
            continue
        pair = pd.concat([frame["close"].rename("idx"), hk_bench_frame["close"].rename("bench")], axis=1).dropna()
        if len(pair) < 220:
            hk_charts.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">与基准重叠的交易日不足220天，算不出200DMA乖离，本次不画图。</p></article>')
            continue
        dev200 = (pair["idx"] / pair["idx"].rolling(200).mean() - 1) * 100
        rel_rsi = wilder_rsi(pair["idx"] / pair["bench"], rel_signal["rsi_period"])
        hk_rel_rsi[code] = rel_rsi
        dev_plot = dev200.loc[dev200.index >= HISTORY_START].dropna()
        rsi_plot = rel_rsi.loc[rel_rsi.index >= HISTORY_START].dropna()
        hk_charts.append(chart(
            f"{name}：200DMA乖离 + {name}/{hk_bench_name} RSI{rel_signal['rsi_period']}",
            [
                series_payload(dev_plot, f"{name}距200DMA", item["color"], "left", True, 1000),
                series_payload(rsi_plot, f"{name}/{hk_bench_name} RSI{rel_signal['rsi_period']}", "#ffbf3c", "right", False, 1000),
            ],
            # 脚注只留口径和读数；替代标的的理由留在 config 的 note 和上方 hk_rule。
            f"左轴距200DMA，右轴相对{hk_bench_name}的 Wilder RSI{rel_signal['rsi_period']}，左轴0与右轴50同高。"
            f"当前乖离{fmt(finite(dev_plot.iloc[-1]) if not dev_plot.empty else None,1,'%')}，相对RSI{fmt(finite(rsi_plot.iloc[-1]) if not rsi_plot.empty else None,1)}；"
            f"乖离线自{iso_date(dev_plot.index.min()) if not dev_plot.empty else '—'}起。",
            [0, 100],
            330,
            None,
            False,
            style_hlines,
            ALIGN_ZERO_TO_50,
        ))
    hk_charts_html = '<div class="grid two">' + "".join(hk_charts) + "</div>" if hk_charts else ""
    hk_rule = (
        f"左轴距 200 日均线，右轴与基准之比的 Wilder RSI{rel_signal['rsi_period']}，左轴 0 与右轴 50 同高。"
        f"基准用{hk_bench_name}，红利那格用恒生港股通高股息低波动。"
    )

    crowd_rows = crowding.get("rows", [])
    crowd_board_html = crowding_board(crowding)

    # 六大科技这张表不再列 BF P/E：本页读的是相对强弱和均线位置，估值倍数是另一套判断，
    # 混在同一行会让人拿 P/E 去解释 RSI。指数与板块的 BF P/E 仍留在美股市场页的估值快照里。
    mags_rows = []
    megacap_charts = []
    megacap_alerts: list[dict[str, str]] = []
    megacap_signals: list[dict[str, Any]] = []  # 每日速览用：只留读数和组合状态，不留序列
    stock_lookup = {x["ticker"]: x for x in stocks}
    for ticker in config["mags"]:
        x = stock_lookup.get(ticker, {})
        price = prices.get(ticker, pd.Series(dtype=float))
        pair = pd.concat([price.rename("stock"), qqq.rename("qqq")], axis=1).dropna()
        if len(pair) < rel_signal["position_lookback_days"] + 201:
            mags_rows.append([ticker, "暂无", "暂无", "暂无", '<span class="pill off">数据不足</span>'])
            continue
        dev200 = (pair["stock"] / pair["stock"].rolling(200).mean() - 1) * 100
        rel_rsi30 = wilder_rsi(pair["stock"] / pair["qqq"], rel_signal["rsi_period"])
        lookback = rel_signal["position_lookback_days"]
        low_line = dev200.rolling(lookback, min_periods=lookback).quantile(0.20).shift(1)
        high_line = dev200.rolling(lookback, min_periods=lookback).quantile(0.80).shift(1)
        dev_now = finite(dev200.iloc[-1])
        rsi_now = finite(rel_rsi30.iloc[-1])
        low_now = finite(low_line.iloc[-1])
        high_now = finite(high_line.iloc[-1])
        if dev_now is not None and high_now is not None and dev_now > high_now:
            position = f"高20%（线{high_now:+.1f}%）"
        elif dev_now is not None and low_now is not None and dev_now < low_now:
            position = f"低20%（线{low_now:+.1f}%）"
        else:
            position = "中间60%"
        top_combo = bool(rsi_now is not None and dev_now is not None and high_now is not None and rsi_now > rel_signal["top_rsi"] and dev_now > high_now and dev_now >= rel_signal["top_ma200_dev_pct"])
        bottom_combo = bool(rsi_now is not None and dev_now is not None and low_now is not None and rsi_now < rel_signal["bottom_rsi"] and dev_now < low_now and dev_now <= rel_signal["bottom_ma200_dev_pct"])
        if top_combo:
            state = '<span class="pill off">顶部组合</span>'
            megacap_alerts.append(chip(f"{ticker} 顶部组合触发", "hot"))
        elif bottom_combo:
            state = '<span class="pill on">底部组合</span>'
            megacap_alerts.append(chip(f"{ticker} 底部组合触发", "cold"))
        elif rsi_now is not None and rsi_now > rel_signal["top_rsi"]:
            state = "相对偏热，绝对门未齐"
        elif rsi_now is not None and rsi_now < rel_signal["bottom_rsi"]:
            state = "相对偏冷，绝对门未齐"
        else:
            state = "中性"
        mags_rows.append([ticker, fmt(rsi_now, 1), fmt(dev_now, 1, "%"), html.escape(position), state])
        megacap_signals.append({"ticker": ticker, "rel_rsi": rsi_now, "dev200": dev_now, "top_combo": top_combo, "bottom_combo": bottom_combo})
        panel = chart(
            f"{ticker}：200DMA乖离 + 相对QQQ RSI30",
            [
                series_payload(dev200.loc[dev200.index >= "2023-01-01"], "距200DMA", "#4da3ff", "left", True, 1000),
                series_payload(rel_rsi30.loc[rel_rsi30.index >= "2023-01-01"], f"相对QQQ RSI{rel_signal['rsi_period']}", "#ffbf3c", "right", False, 1000),
            ],
            f"当前乖离{fmt(dev_now,1,'%')}，相对RSI{fmt(rsi_now,1)}，两年区间{position}。左轴0与右轴50同高。",
            [0, 100],
            315,
            None,
            False,
            [hline(0, "left", "200DMA", "zero"), hline(rel_signal["top_rsi"], "right", "", "hot"), hline(rel_signal["bottom_rsi"], "right", "", "cold")],
            ALIGN_ZERO_TO_50,
        )
        # 通道读数挂在该股自己那张图下面，不另开面板：读者看的是同一只票，翻两处只会对不上。
        panel = append_note(panel, trend_note(trend_stats.get(ticker)))
        panel = append_note(panel, trend_fit_note(trend_stats.get(ticker)))
        megacap_charts.append(panel)
    mags_table = table(["股票", "相对QQQ RSI30", "距MA200", "两年区间", "信号"], mags_rows)
    megacap_rule = (
        f"六张图同一口径：顶部需相对RSI>{rel_signal['top_rsi']}、乖离进两年最高20%且≥{rel_signal['top_ma200_dev_pct']:+.0f}%；"
        f"底部需相对RSI<{rel_signal['bottom_rsi']}、进最低20%且≤{rel_signal['bottom_ma200_dev_pct']:+.0f}%。"
    )
    megacap_charts_html = f'<p class="note rule">{html.escape(megacap_rule)}</p><div class="grid two">' + "".join(megacap_charts) + '</div>'

    # ── TSM / INTC / BRKB：口径同上，但均线周期和基准各按自己的行业换过 ──
    extra_benchmarks = {"SOX": sox_series, "SPW": spw, "QQQ": qqq}
    extra_charts: list[str] = []
    extra_signals: dict[str, dict[str, Any]] = {}  # 每日速览用：TSM 距 100DMA 那条提醒读这里
    for item in config.get("relative_extras", []):
        name, ticker, ma = item["name"], item["ticker"], int(item.get("ma", 200))
        bench_key = item["benchmark"]
        bench = extra_benchmarks.get(bench_key, prices.get(bench_key, pd.Series(dtype=float)))
        price = prices.get(ticker, pd.Series(dtype=float))
        if price.empty or bench.empty:
            extra_charts.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">{html.escape(ticker)}或基准{html.escape(bench_key)}本次取不到行情，不用空图顶替。</p></article>')
            continue
        pair = pd.concat([price.rename("stock"), bench.rename("bench")], axis=1).dropna()
        if len(pair) < ma + 60:
            extra_charts.append(f'<article class="panel"><h3>{html.escape(name)}</h3><p class="empty">与基准重叠的交易日不足{ma + 60}天，算不出{ma}DMA乖离，本次不画图。</p></article>')
            continue
        dev = (pair["stock"] / pair["stock"].rolling(ma).mean() - 1) * 100
        rel = wilder_rsi(pair["stock"] / pair["bench"], rel_signal["rsi_period"])
        dev_plot = dev.loc[dev.index >= "2023-01-01"].dropna()
        rel_plot = rel.loc[rel.index >= "2023-01-01"].dropna()
        extra_signals[name] = {
            "ticker": ticker, "ma": ma, "benchmark": bench_key,
            "dev": finite(dev_plot.iloc[-1]) if not dev_plot.empty else None,
            "rel_rsi": finite(rel_plot.iloc[-1]) if not rel_plot.empty else None,
            "as_of": iso_date(dev_plot.index.max()) if not dev_plot.empty else "",
        }
        extra_charts.append(chart(
            f"{name}：{ma}DMA乖离 + 相对{bench_key} RSI{rel_signal['rsi_period']}",
            [
                series_payload(dev_plot, f"距{ma}DMA", item["color"], "left", True, 1000),
                series_payload(rel_plot, f"相对{bench_key} RSI{rel_signal['rsi_period']}", "#ffbf3c", "right", False, 1000),
            ],
            f"左轴距{ma}DMA，右轴相对{bench_key}的 Wilder RSI{rel_signal['rsi_period']}，左轴0与右轴50同高。"
            f"当前乖离{fmt(finite(dev_plot.iloc[-1]) if not dev_plot.empty else None,1,'%')}，相对RSI{fmt(finite(rel_plot.iloc[-1]) if not rel_plot.empty else None,1)}。",
            [0, 100],
            315,
            None,
            False,
            [hline(0, "left", f"{ma}DMA", "zero"), hline(rel_signal["top_rsi"], "right", "", "hot"), hline(rel_signal["bottom_rsi"], "right", "", "cold")],
            ALIGN_ZERO_TO_50,
        ))
    extra_rule = "半导体两只对 SOX、BRKB 对标普等权：基准换成各自的同类池子，才读得出个股相对同行的强弱。TSM、INTC 用100DMA，波动快一档。"
    extra_charts_html = (
        f'<p class="note rule">{html.escape(extra_rule)}</p><div class="grid two">' + "".join(extra_charts) + "</div>"
        if extra_charts else ""
    )

    afrm_html = trend_panel(trend_stats.get("AFRM"), "AFRM")

    us_style_charts = ""
    sox_rel_rsi = pd.Series(dtype=float)
    sox = sox_series
    sox_label = "SOX" if "^SOX" in prices else "SOXX代理"
    if not sox.empty and not qqq.empty:
        sox_pair = pd.concat([sox.rename("sox"), qqq.rename("qqq")], axis=1).dropna()
        sox_dev200 = (sox_pair["sox"] / sox_pair["sox"].rolling(200).mean() - 1) * 100
        sox_rel_rsi = wilder_rsi(sox_pair["sox"] / sox_pair["qqq"], rel_signal["rsi_period"])
        us_style_charts += chart("SOX：200DMA乖离 + SOX/QQQ RSI30", [series_payload(sox_dev200.loc[sox_dev200.index >= "2023-01-01"], f"{sox_label}距200DMA", "#a377ff", "left", True, 1000), series_payload(sox_rel_rsi.loc[sox_rel_rsi.index >= "2023-01-01"], "SOX/QQQ RSI30", "#ffbf3c", "right", False, 1000)], f"左轴距200DMA，右轴相对QQQ的Wilder RSI30，左轴0与右轴50同高。半导体在200DMA上方比例{fmt(semi_breadth,1,'%')}。", [0, 100], 360, None, False, style_hlines, ALIGN_ZERO_TO_50)
    xsw_rel_rsi = pd.Series(dtype=float)
    xsw = prices.get("XSW", pd.Series(dtype=float))
    if not xsw.empty and not qqq.empty:
        xsw_pair = pd.concat([xsw.rename("xsw"), qqq.rename("qqq")], axis=1).dropna()
        xsw_dev200 = (xsw_pair["xsw"] / xsw_pair["xsw"].rolling(200).mean() - 1) * 100
        xsw_rel_rsi = wilder_rsi(xsw_pair["xsw"] / xsw_pair["qqq"], rel_signal["rsi_period"])
        us_style_charts += chart("XSW：200DMA乖离 + XSW/QQQ RSI30", [series_payload(xsw_dev200.loc[xsw_dev200.index >= "2023-01-01"], "XSW距200DMA", "#4dd2ff", "left", True, 1000), series_payload(xsw_rel_rsi.loc[xsw_rel_rsi.index >= "2023-01-01"], "XSW/QQQ RSI30", "#ffbf3c", "right", False, 1000)], "口径同SOX图，起点2023-01-01；XSW 是等权软件ETF。左轴0与右轴50同高。", [0, 100], 360, None, False, style_hlines, ALIGN_ZERO_TO_50)
    basket_rel_rsi: dict[str, pd.Series] = {}
    for basket in config.get("equal_weight_baskets", []):
        members = list(basket["members"])
        index = equal_weight_index(prices, members)
        bench = prices.get(basket["benchmark"], pd.Series(dtype=float))
        if index.empty or bench.empty:
            continue
        pair = pd.concat([index.rename("idx"), bench.rename("bench")], axis=1).dropna()
        dev200 = (pair["idx"] / pair["idx"].rolling(200).mean() - 1) * 100
        rel_rsi = wilder_rsi(pair["idx"] / pair["bench"], rel_signal["rsi_period"])
        basket_rel_rsi[basket["key"]] = rel_rsi
        label, bench_name = basket["label"], basket["benchmark"]
        note = (
            f"口径同SOX图，起点2023-01-01。成分{len(members)}只（{'、'.join(members)}），日频等权、复权价。左轴0与右轴50同高。"
        )
        us_style_charts += chart(
            f"{label}：200DMA乖离 + {label}/{bench_name} RSI30",
            [
                series_payload(dev200.loc[dev200.index >= "2023-01-01"], f"{label}距200DMA", basket["color"], "left", True, 1000),
                series_payload(rel_rsi.loc[rel_rsi.index >= "2023-01-01"], f"{label}/{bench_name} RSI30", "#ffbf3c", "right", False, 1000),
            ],
            note,
            [0, 100],
            360,
            None,
            False,
            style_hlines,
            ALIGN_ZERO_TO_50,
        )
    splv = prices.get("SPLV", pd.Series(dtype=float))
    spy = prices.get("SPY", pd.Series(dtype=float))
    if not splv.empty and not spy.empty:
        splv_pair = pd.concat([splv.rename("splv"), spy.rename("spy")], axis=1).dropna()
        splv_dev200 = (splv_pair["splv"] / splv_pair["splv"].rolling(200).mean() - 1) * 100
        splv_rel_rsi = wilder_rsi(splv_pair["splv"] / splv_pair["spy"], rel_signal["rsi_period"])
        us_style_charts += chart("SPLV：200DMA乖离 + SPLV/SPX RSI30", [series_payload(splv_dev200.loc[splv_dev200.index >= "2023-01-01"], "SPLV距200DMA", "#38d39f", "left", True, 1000), series_payload(splv_rel_rsi.loc[splv_rel_rsi.index >= "2023-01-01"], "SPLV/SPX RSI30", "#ffbf3c", "right", False, 1000)], "口径同SOX图，SPX 用 SPY 复权总回报代理。SPLV 跑赢=防御占优，方向与本页其他图相反，不进温度带。左轴0与右轴50同高。", [0, 100], 360, None, False, style_hlines, ALIGN_ZERO_TO_50)

    sector_charts: list[str] = []
    sector_signals: list[dict[str, Any]] = []  # 每日速览用：只在相对 RSI 越过 60/40 时才进卡片
    if not spy.empty:
        for item in config.get("sp_sector_etfs", []):
            ticker = item["ticker"]
            sector = prices.get(ticker, pd.Series(dtype=float))
            if sector.empty:
                continue
            pair = pd.concat([sector.rename("sector"), spy.rename("spy")], axis=1).dropna()
            if len(pair) < 200:
                continue
            dev200 = (pair["sector"] / pair["sector"].rolling(200).mean() - 1) * 100
            rel_rsi = wilder_rsi(pair["sector"] / pair["spy"], rel_signal["rsi_period"])
            sector_signals.append({"name": item["name"], "ticker": ticker, "rel_rsi": finite(rel_rsi.iloc[-1]), "dev200": finite(dev200.iloc[-1])})
            sector_charts.append(chart(
                f"{item['name']}（{ticker}）：200DMA乖离 + {ticker}/SPX RSI30",
                [
                    series_payload(dev200.loc[dev200.index >= "2023-01-01"], f"{ticker}距200DMA", item["color"], "left", True, 1000),
                    series_payload(rel_rsi.loc[rel_rsi.index >= "2023-01-01"], f"{ticker}/SPX RSI30", "#ffbf3c", "right", False, 1000),
                ],
                f"{item['name']}一级行业ETF；左轴距200DMA，右轴为相对SPX的Wilder RSI30，左轴0与右轴50同高。",
                [0, 100],
                315,
                None,
                False,
                style_hlines,
                ALIGN_ZERO_TO_50,
            ))
    if sector_charts:
        us_style_charts += (
            '<h2>S&amp;P 一级行业ETF</h2>'
            '<p class="note rule">覆盖11个GICS一级行业。SPX 用 SPY 复权总回报代理，行业ETF 同样用复权价；图面从 2023-01-01 起。</p>'
            '<div class="grid two">' + "".join(sector_charts) + "</div>"
        )

    # 美股这边的非六大科技只有 AFRM，它已经改成只报趋势底那张面板，不再进 RSI/趋势门表。
    china_stock_rows: list[list[str]] = []
    for x in stocks:
        if x["ticker"] in config["mags"] or not x["ticker"].endswith((".SS", ".SZ")):
            continue
        if not x.get("available"):
            china_stock_rows.append([html.escape(x["name"]), html.escape(x["ticker"]), "暂无", "暂无", "暂无", "暂无", "<span class=\"pill off\">数据不足</span>"])
            continue
        gate_html = '<span class="pill on">有效</span>' if x["gate"] else '<span class="pill off">关闭</span>'
        china_stock_rows.append([html.escape(x["name"]), html.escape(x["ticker"]), fmt(x["rsi14"], 1), direction_fmt(x["ret20"], 1, "%"), direction_fmt(x["ret60"], 1, "%"), direction_fmt(x["ma200_dev"], 1, "%"), gate_html])
    china_stock_table = table(["公司", "Ticker", "RSI14", "20日", "60日", "距MA200", "趋势门"], china_stock_rows)

    bench_names = {"000300.SS": "沪深300", "QQQ": "QQQ", "SPY": "SPX"}
    china_stock_charts: list[str] = []
    china_stock_warnings: list[str] = []
    for item in config["single_stocks"]:
        ticker = item["ticker"]
        if not ticker.endswith((".SS", ".SZ")):
            continue
        stock = prices.get(ticker, pd.Series(dtype=float)).dropna()
        benchmark = prices.get(item["benchmark"], pd.Series(dtype=float)).dropna()
        if len(stock) < 260:
            china_stock_charts.append(f'<article class="panel"><h3>{html.escape(item["name"])}</h3><p class="empty">历史不足260个交易日，本次不画图。</p></article>')
            continue
        cn_dev200, cn_rel_rsi = china_stock_indicators(stock, benchmark, rel_signal["rsi_period"])
        if cn_rel_rsi.empty:
            china_stock_warnings.append(f"{item['name']}相对RSI缺少连续基准历史")
        if "000300.SH" in history["stale"]:
            china_stock_warnings.append("中国个股沪深300基准使用未刷新缓存")
        bench_label = bench_names.get(item["benchmark"], item["benchmark"])
        china_stock_charts.append(chart(
            f"{item['name']}：200DMA乖离 + 相对{bench_label} RSI{rel_signal['rsi_period']}",
            [
                series_payload(cn_dev200.loc[cn_dev200.index >= "2023-01-01"], "距200DMA", "#4da3ff", "left", True, 1000, max_gap_days=14),
                series_payload(cn_rel_rsi.loc[cn_rel_rsi.index >= "2023-01-01"], f"相对{bench_label} RSI{rel_signal['rsi_period']}", "#ffbf3c", "right", False, 1000, max_gap_days=14),
            ],
            f"乖离{fmt((cn_dev200.iloc[-1] if not cn_dev200.empty else None),1,'%')}（{iso_date(cn_dev200.index.max()) if not cn_dev200.empty else '暂无'}）；相对RSI{fmt((cn_rel_rsi.iloc[-1] if not cn_rel_rsi.empty else None),1)}（{iso_date(cn_rel_rsi.index.max()) if not cn_rel_rsi.empty else '暂无'}）。沪深300：挖财指数日线。",
            [0, 100],
            330,
            None,
            False,
            style_hlines,
        ))
    china_stock_charts_html = '<div class="grid two">' + "".join(china_stock_charts) + "</div>" if china_stock_charts else ""

    def leverage_card(x: dict[str, Any]) -> str:
        tone = "hot" if x["score"] >= 75 else "warm" if x["score"] >= 55 else "neutral" if x["score"] >= 35 else "cold"
        return metric_card(x["name"], f"{x['score']}/100", f"{x['level']} · {x['state']}", tone, x["core"])

    # 中国A股 的杠杆卡归 A股市场页，不再留在日韩台页：它和两融是同一件事的两种读法，
    # 放在一起才能对着看，散在两页只会让人两边翻。
    asia_by_name = {x["name"]: x for x in asia}
    asia_cards = "".join(leverage_card(x) for x in asia if x["name"] != "中国A股")
    if not asia_cards:
        asia_cards = metric_card("日韩台", "暂无", "跨市场杠杆卡未找到", "muted")
    cn_leverage = asia_by_name.get("中国A股")
    cn_leverage_card = (
        '<section class="cards">' + leverage_card(cn_leverage) + '</section>'
        if cn_leverage else
        '<section class="cards">' + metric_card("中国A股杠杆", "暂无", "cn-leverage 跨市场卡未找到", "muted") + '</section>'
    )

    valuation_rows = [[html.escape(k), fmt(v, 1, "x")] for k, v in seed["valuation_snapshot"]["forward_pe"].items()]
    valuation_table = table(["指数/板块", "BF P/E"], valuation_rows)
    consensus_rows = [[year, fmt(v["real_gdp_pct"], 2, "%"), fmt(v["cpi_pct"], 2, "%"), fmt(v["real_gdp_pct"] + v["cpi_pct"], 2, "%")] for year, v in seed["consensus"].items()]
    consensus_table = table(["年度", "GDP共识", "CPI共识", "名义GDP"], consensus_rows)

    gs_prime_store = update_gs_prime_store(WORKSPACE)
    gs_prime_signal = evaluate_gs_prime_signals(gs_prime_store, config)
    gs_prime_html = render_gs_prime_section(
        store=gs_prime_store, signals_block=gs_prime_signal, qqq_price=qqq_price,
        chart=chart, series_payload=series_payload, hline=hline, metric_card=metric_card, fmt=fmt,
    )

    freshness = [
        ["Bloomberg CSV", bbg.get("as_of", "未知"), "动态识别GSCBHMOM RSI周期"],
        ["公开行情", iso_date(max((s.index.max() for s in prices.values() if not s.empty), default=None)), "Yahoo复权日线"],
        ["高盛Prime仓位", f"杠杆 {gs_prime_store.get('leverage_as_of') or '未知'} / Mag7 {gs_prime_store.get('mag7_as_of') or '未知'}", f"周报自动提取美股L/S杠杆（{gs_prime_store.get('leverage_count', 0)}期）与Mag7敞口（{gs_prime_store.get('mag7_count', 0)}点）"],
        ["A股两融", cn["as_of"], "沪深交易所聚合历史库"],
        ["指数拥挤度", crowding.get("trade_date", "未知"), f"原版镜像 {crowding.get('index_count',0)}项"],
        ["指数长历史", history["as_of"], f"A股与港股看板用；本次刷新{len(history['refreshed'])}个、退回缓存{len(history['stale'])}个、缺失{len(history['missing'])}个"],
        ["GDP/CPI与估值", seed["source_as_of"], "用户附件快照；不自动冒充最新"],
        ["趋势线复权OHLC", max((v["as_of"] for v in trend_stats.values()), default="未知"), f"AMZN通道与AFRM支撑线；线用config冻结参数不重拟合，本次读出{len(trend_stats)}条、缺{len(trend_missing)}条"],
    ]

    flow_cfg = config.get("cn_fund_flow", {})
    flow_side_n = int(flow_cfg.get("side_n", FLOW_SIDE_N))
    flow_snapshot = fetch_fund_flow(flow_side_n) if flow_cfg.get("enabled", True) else {"ok": False, "error": "配置里已关闭该面板", "asof": "", "date": "", "boards": [], "rank": {"top": [], "bottom": []}}
    freshness.append(["A股盘中资金流", flow_snapshot.get("asof") or ("服务器被封，页面实时拉取" if flow_snapshot.get("blocked") else "未取到"), f"东方财富板块主力净流入；页面每{FLOW_REFRESH_MIN}分钟自刷新"])
    turnover_snapshot = fetch_market_turnover(config)
    freshness.append([
        "A股全市场量能",
        turnover_snapshot.get("asof") or "未取到",
        turnover_freshness_detail(turnover_snapshot),
    ])

    mags_pair = pd.concat([mags_index.rename("m"), qqq.rename("q")], axis=1).dropna() if not mags_index.empty and not qqq.empty else pd.DataFrame()
    mags_rel_rsi = wilder_rsi(mags_pair["m"] / mags_pair["q"], rel_signal["rsi_period"]) if not mags_pair.empty else pd.Series(dtype=float)

    us_items = [
        strip_item("周期/防御", cyc_rsi14, hint="周期股/防御股相对指数 RSI14（GSPUCYDE）"),
        strip_item("Momentum", mom, hint=f"GSCBHMOM RSI{mom_period or '?'}"),
        strip_item("纳指/等权", ndx_spw_rsi, hint="NDX/SPW RSI14"),
        strip_item("六大科技", mags_rel_rsi, hint="六股等权/QQQ RSI30"),
        strip_item("半导体", sox_rel_rsi, hint="SOX/QQQ RSI30"),
        strip_item("XSW", xsw_rel_rsi, hint="XSW/QQQ RSI30，等权软件ETF"),
        strip_item("互联网", basket_rel_rsi.get("INTERNET", pd.Series(dtype=float)), hint="Internet等权/QQQ RSI30：NFLX、BKNG、ABNB、DASH"),
        strip_item("Staples", basket_rel_rsi.get("STAPLES", pd.Series(dtype=float)), hint="Staples等权/QQQ RSI30：WMT、COST、TMUS、PEP、CMCSA"),
    ]
    # 冷热 chip 分两类：先是落进冷区/热区的行，然后是本来就有绝对阈值的那几条专用监控。
    # 专用监控的行不再重复出 zone chip，否则同一行会挂两个说法不一样的提示。
    us_alerts = zone_chips(us_items, skip={"周期/防御"})
    if top_framework.get("active"):
        us_alerts.append(chip(top_framework["summary"], "hot"))
    # 指数 put 保护提醒（用户 2026-09-04）：VVIX/VIX 过线的每一天都在美股温度带上挂 chip，和状态机分离。
    if (top_framework.get("put_hedge") or {}).get("on"):
        us_alerts.append(chip(f"指数 put 保护提醒：VVIX/VIX {fmt(top_framework['metrics'].get('vvix_vix'),2)}", "hot"))
    if bottom_framework.get("active"):
        us_alerts.append(chip(bottom_framework["summary"], "cold"))
    elif alert_projection_lead(bottom_framework):
        us_alerts.append(chip(f"美股底部：{alert_projection_lead(bottom_framework)}", "cold"))
    if vol_inverted:
        us_alerts.append(chip("QQQ 近月隐波倒挂", "hot"))
    us_alerts += megacap_alerts
    for gp_chip in gs_prime_signal.get("chips", []):
        us_alerts.append(chip(gp_chip["text"], gp_chip["tone"]))

    crowd_by_code = {str(r.get("index_code")): r for r in crowd_rows}

    # A股带：第一行是自带历史序列的两融，其余七行是供应商 0-100 拥挤度。顺序写死，
    # 不按当日冷热重排——这七个是刻意选的观察名单，位置换来换去就没法逐日对照。
    cn_a_items = [strip_item("两融余额", cn["frame"]["margin_rsi14"], hint="沪深两融余额 RSI14")]
    for item in cn_a_cfg:
        row = crowd_by_code.get(item["code"]) or match_crowding(crowd_rows, item["name"])
        if not row:
            continue
        score = finite(row.get("crowding_percentile"))
        trail = [finite(p[2]) for p in (row.get("history_1y") or []) if finite(p[2]) is not None]
        cn_a_items.append(strip_point(
            item["name"],
            score,
            trail=trail[-(STRIP_TRAIL + 1) :],
            raw=fmt(score, 1, "%"),
            asof=str(row.get("trade_date", "")),
            hint=f"{item['code']} 拥挤度1年分位 · {row.get('crowding_level','')}",
        ))
    cn_a_alerts = zone_chips(cn_a_items, skip={"两融余额"})
    if last_margin is not None and last_margin <= 30:
        cn_a_alerts.append(chip(f"两融余额 RSI14={fmt(last_margin,1)} ≤ 30（超卖）", "cold"))
    if last_margin is not None and last_margin >= 70:
        cn_a_alerts.append(chip(f"两融余额 RSI14={fmt(last_margin,1)} ≥ 70（超买）", "hot"))

    # 港股带：AH 溢价动能 + 四条「板块/离岸中国基准」的相对强弱，全部走稳健 z，同一把尺子。
    cn_hk_items = [strip_item("AH溢价动能", ah_rsi, digits=2, hint="BBG HT006取负，右=溢价收窄")]
    for item in hk_members:
        series = hk_rel_rsi.get(item["code"], pd.Series(dtype=float))
        if series.empty:
            continue
        cn_hk_items.append(strip_item(
            item["name"],
            series,
            hint=f"{item['name']}/{hk_bench_name} RSI{rel_signal['rsi_period']}（{item['code']}）",
        ))
    cn_hk_alerts = zone_chips(cn_hk_items, skip={"AH溢价动能"})
    if last_ah is not None and last_ah >= -36:
        cn_hk_alerts.append(chip(f"AH溢价 RSI30={fmt(last_ah,1)} ≥ -36（超买）", "hot"))
    if last_ah is not None and last_ah <= -60:
        cn_hk_alerts.append(chip(f"AH溢价 RSI30={fmt(last_ah,1)} ≤ -60（超卖）", "cold"))

    # 综合分的历史序列由 crossview 落盘，这里只读；有历史的行才画得出 1W/1M 位移。
    asia_comp_hist = asia_composite_history(WORKSPACE / "temp/asia-leverage-crossview/composite_history.csv")

    def asia_row(x: dict[str, Any]) -> dict[str, Any] | None:
        series = asia_comp_hist.get(ASIA_COMPOSITE_KEYS.get(x["name"], ""), pd.Series(dtype=float))
        trail = [round(float(v), 1) for v in series.iloc[-(STRIP_TRAIL + 1):]] if not series.empty else []
        # 日本是周频，往回数 20 个观测就是 20 周，1M 标记会跑到五个月前。周频行改按日历日
        # 回溯：第 k 格对应 k×28/20 天前的最后一个已发布值，这样 -6 格正好是 7 天前、
        # -21 格正好是 28 天前，和另外两行的 1W/1M 含义一致。
        if not series.empty and x["name"] == "日本":
            picks = []
            for k in range(STRIP_TRAIL, -1, -1):
                window = series.loc[: series.index.max() - timedelta(days=int(round(k * 28 / STRIP_TRAIL)))]
                picks.append(round(float(window.iloc[-1]), 1) if not window.empty else None)
            trail = [v for v in picks if v is not None] if picks[0] is not None else []
        if trail:
            trail = trail[:-1] + [float(x["score"])]
        return strip_point(
            x["name"], x["score"], trail=trail, raw=f"{x['score']}/100",
            asof=iso_date(series.index.max()) if not series.empty else "",
            scale="score", hint=f"{x['level']} · {x['state']}" + ("" if trail else " · 综合分历史未落盘，本行只有当前值"),
        )

    asia_items = [asia_row(x) for x in asia if x["name"] in {"日本", "韩国", "台湾"}]
    asia_alerts = zone_chips(asia_items)
    asia_trail_rows = sum(1 for x in asia_items if x and x.get("trail"))

    strips_html = (
        # 四条带的脚注各一行口径；详细规则统一放在下方“口径与限制”面板，不在脚注里重复解释。
        strip_panel("美股", [x for x in us_items if x], us_alerts, "○ 1个月前，● 1周前，⬤ 当前；原值和日期见悬停。行序固定。", fixed=True)
        + strip_panel("A股", [x for x in cn_a_items if x], cn_a_alerts, "第一行两融余额是自身历史的标准化位置，分隔线下七行是 0—100 拥挤度，两把尺子不可互比。", fixed=True)
        + strip_panel("港股", [x for x in cn_hk_items if x], cn_hk_alerts, f"同一把尺子：AH 溢价动能，以及四个板块相对{hk_bench_name}的 Wilder RSI{rel_signal['rsi_period']}。", fixed=True)
        + strip_panel(
            "日韩台", [x for x in asia_items if x], asia_alerts,
            "各地杠杆监控的 0—100 综合分，高分=可出清杠杆多，不等于卖出信号。"
            + (
                f"{asia_trail_rows} 行带 1W/1M 位移，日本为周频。"
                if asia_trail_rows else "综合分历史文件尚未生成，本次只显示当前值。"
            ),
        )
    )

    sections = {
        "overview": '<div class="stack">' + strips_html + '<div class="grid two"><article class="panel"><h3>数据新鲜度</h3>' + table(["数据", "日期", "说明"], freshness) + '</article><article class="panel"><h3>口径与限制</h3><ul><li>点的位置只表示该指标相对自身历史的高低，不代表上涨概率。</li><li>同一市场里各行越分散，内部差异越大；越聚拢则越同向。</li><li>“稳健 z”行以过去1年中位数为 0，每格为 1 个 MAD 标准化单位，横轴覆盖 ±2.5，超出后钳到边界；1个月前和1周前取目标日前后各2天的中位数，当前点使用当日值。</li><li>分位和综合分只表示所处区域，点之间的距离不能和稳健 z 行相比。</li><li>冷端只表示相对自身历史偏低，不等于绝对超卖。原值、1年区间和日期见悬停。</li><li>标题下方的蓝色/黄色提示对应图上的冷区和热区：稳健 z 行和综合分行按 0—100 轴的 20 / 80 判定；上游拥挤度分位天然贴边，收紧到 ≥90 / ≤5 才提示。带自己绝对阈值的行（两融 RSI、AH 溢价 RSI、周期防御、个股顶底组合）走各自的阈值，不重复出提示。</li><li>个股信号要结合趋势门和相对强弱，不能用单一指标直接下结论。</li><li>缺失或过期数据会明确标记，不以前值冒充实时值。</li></ul></article></div></div>',
        "us": (
            '<div class="stack">'
            + bottom_monitor_html
            + top_monitor_html
            + state_monitor_section(
                spx_trend_chart=spx_trend_chart,
                gs_prime_html=gs_prime_html,
                us_style_charts=us_style_charts, macro_html=macro_html,
                consensus_table=consensus_table, valuation_table=valuation_table,
                model_note=f'公式：1.219×名义GDP−0.8269+2.2775+3.8761；2018年采用税改项13.8633；锚点2019-06-28 SPX 2941.76。共识截至{seed["source_as_of"]}。',
                valuation_note=f'截至{seed["source_as_of"]}；来自附件，不自动滚动。',
            )
            + '</div>'
        ),
        "cn_a": (
            '<div class="stack">'
            + cn_leverage_card
            + cn_margin_chart
            + '<h2>宽基动能：指数点位与自身 RSI</h2>'
            + f'<p class="note rule">{html.escape(cn_broad_rule)}</p>'
            + cn_broad_html
            + cn_featured_html
            + fund_flow_panel(flow_snapshot, flow_side_n)
            + turnover_panel(turnover_snapshot, config)
            + '<h2>观察名单：拥挤度与指数走势</h2>'
            + f'<p class="note rule">{html.escape(cn_index_rule)}</p>'
            + cn_index_charts_html
            + crowd_board_html
            + '</div>'
        ),
        "cn_hk": (
            '<div class="stack">'
            + ah_charts
            + '<h2>板块相对强弱</h2>'
            + f'<p class="note rule">{html.escape(hk_rule)}</p>'
            + hk_charts_html
            + '</div>'
        ),
        "asia": (
            '<div class="stack">'
            + '<section class="cards">' + asia_cards + '</section>'
            + '<article class="panel"><h3>口径</h3><p>上方三张卡直接复用现有日、韩、台杠杆监控的存量拥挤度和去杠杆状态；高分表示可出清燃料多，不等于当日卖出信号。</p>'
            + f'<p class="note">下方每国两张图：左轴该国杠杆指标（同卡片“核心指标”口径），右轴该国 MSCI 指数（本币），起点 {html.escape(asia_charts_start)}。</p></article>'
            + asia_charts["html"]
            + '</div>'
        ),
        "us_stocks": '<div class="stack"><article class="panel"><h3>六大科技股相对信号</h3><p class="note">完全沿用每日cron跟踪名单：AMZN、GOOGL、META、MSFT、AAPL、NVDA；不用MAGS ETF。</p>' + mags_table + '</article>' + megacap_charts_html + extra_charts_html + afrm_html + '</div>',
        "china_stocks": '<div class="stack"><article class="panel"><h3>覆盖名单</h3><p class="note">只跟宁德时代与长江电力两只（刻意选择，不是数据缺失），基准沪深300。趋势门：Close&gt;MA200 且 MA200 高于20个交易日前。</p>' + china_stock_table + '</article>' + china_stock_charts_html + '</div>',
    }

    # ── 每日速览的结构化读数 ──
    us_top_line = float(config["thresholds"]["us_top_rsi30"])
    rsi_hot, rsi_cold = float(config["thresholds"]["rsi_hot"]), float(config["thresholds"]["rsi_cold"])
    top_rsi, bottom_rsi = float(rel_signal["top_rsi"]), float(rel_signal["bottom_rsi"])

    # 顶部只认状态机：预警/过激/确认/失效/到期才算信号，隐波比只作环境不进 hits。
    top_hits: list[str] = []
    if top_framework.get("active"):
        top_hits.append(top_framework["summary"])
    top_metrics = top_framework.get("metrics", {})
    top_reading = (
        f"顶部框架：{top_framework['summary']}；周期/防御 RSI30 {fmt(top_metrics.get('gsp_rsi30'),1)}（预警线 {us_top_line:.0f}），"
        f"量子等权较60日低点 {fmt(top_metrics.get('quantum_from_low_pct'),0,'%')}，"
        f"QQQ RSI14 {fmt(top_metrics.get('qqq_rsi14'),1)}，距200DMA {fmt(top_metrics.get('qqq_vs_200dma_pct'),1,'%')}，"
        f"Momentum RSI {fmt(top_metrics.get('momentum_rsi'),1)}，"
        f"QQQ 隐波 30D/60D {fmt(vol_ratio,2)}；指数put保护 VVIX/VIX {fmt(top_metrics.get('vvix_vix'),2)}"
        f"（提醒线 ≥{top_cfg.put_hedge_vvix_vix:.1f}，{'提醒' if (top_framework.get('put_hedge') or {}).get('on') else '无'}）"
    )
    bottom_hits: list[str] = []
    if bottom_framework.get("active"):
        bottom_hits.append(bottom_framework["summary"])
    topbottom_hits = top_hits + bottom_hits
    bottom_metrics = bottom_framework.get("metrics", {})
    bottom_reading = (
        f"底部框架：{bottom_framework['summary']}；周期股/防御股相对指数 RSI14 {fmt(bottom_metrics.get('gsp_rsi14'),1)}，"
        f"QQQ 63日回撤 {fmt(bottom_metrics.get('qqq_drawdown_63_pct'),1,'%')}，"
        f"SPX/QQQ RSI14 {fmt(bottom_metrics.get('spx_rsi14'),1)}/{fmt(bottom_metrics.get('qqq_rsi14'),1)}，"
        f"VIX {fmt(bottom_metrics.get('vix'),1)}"
    )

    # 各市场的截止日分开报。取所有行情序列的最大值会把 A股/港股当天的收盘当成美股的截止日
    # （美东盘前跑时两者差一天），卡片上就成了「美股读数截至今天」而实际是昨收。
    us_as_of = iso_date(max((s.index.max() for t, s in prices.items() if t in {"QQQ", "SPY", "^GSPC"} and not s.empty), default=None))

    # Momentum 行只报 GSCBHMOM，纳指/等权单独一行：它量的是纳指相对等权的强弱（上涨集中度），
    # 和动量因子放一行写「过热」，会让 RSP 大跌那个月读成「动量过热」（CY 2026-09-30）。
    # GSCBHMOM 来自 Bloomberg CSV，CSV 没跟上美股收盘时把截止日写进这一行，免得旧值冒充当天读数。
    mom_label = f"GSCBHMOM RSI{mom_period or '?'}"
    bbg_as_of = bbg.get("as_of", "")
    mom_lag = f"，Bloomberg 数据截至 {bbg_as_of}" if bbg_as_of and us_as_of and bbg_as_of < us_as_of else ""
    momentum_hits: list[str] = []
    hot, cold = level_up(last_mom, rsi_hot, DIGEST_NEAR_RSI), level_down(last_mom, rsi_cold, DIGEST_NEAR_RSI)
    if hot:
        momentum_hits.append(f"{hot}过热 · {mom_label} {fmt(last_mom,1)}（线 {rsi_hot:.0f}）{mom_lag}")
    elif cold:
        momentum_hits.append(f"{cold}过冷 · {mom_label} {fmt(last_mom,1)}（线 {rsi_cold:.0f}）{mom_lag}")
    # 纳指/等权两端都不用冷热字眼：热端是上涨变窄、冷端是上涨扩散，卡片按「分不清方向」标橙。
    ndx_spw_hits: list[str] = []
    hot, cold = level_up(last_ratio, rsi_hot, DIGEST_NEAR_RSI), level_down(last_ratio, rsi_cold, DIGEST_NEAR_RSI)
    if hot:
        ndx_spw_hits.append(f"{hot}偏窄 · 纳指/等权 RSI14 {fmt(last_ratio,1)}（线 {rsi_hot:.0f}），纳指跑赢等权过快，涨幅集中在大权重股")
    elif cold:
        ndx_spw_hits.append(f"{cold}扩散 · 纳指/等权 RSI14 {fmt(last_ratio,1)}（线 {rsi_cold:.0f}），等权跑赢纳指过快，资金从大权重股往外扩")

    # 科技内部轮动直接读总览温度带那五行，热区/冷区边界与页面完全一致。
    rotation_names = {"六大科技": "Mega tech", "半导体": "半导体", "XSW": "软件(XSW)", "互联网": "互联网", "Staples": "Staples"}
    tech_rotation = [
        {"name": rotation_names[x["name"]], "z": x["z"], "state": tech_state(x["z"]), "raw": x["raw"], "as_of": x["asof"]}
        for x in us_items if x and x["name"] in rotation_names
    ]

    sector_hits = [
        f"{x['name']}({x['ticker']}) 相对SPX RSI{rel_signal['rsi_period']} {fmt(x['rel_rsi'],1)}"
        + ("（过热）" if finite(x["rel_rsi"]) is not None and x["rel_rsi"] >= top_rsi else "（过冷）")
        for x in sector_signals
        if finite(x["rel_rsi"]) is not None and (x["rel_rsi"] >= top_rsi or x["rel_rsi"] <= bottom_rsi)
    ]

    megacap_hits: list[str] = []
    for x in megacap_signals:
        if x["top_combo"] or x["bottom_combo"]:
            megacap_hits.append(f"{x['ticker']} {'顶部' if x['top_combo'] else '底部'}组合触发（相对RSI {fmt(x['rel_rsi'],1)}、距MA200 {fmt(x['dev200'],1,'%')}）")
        elif finite(x["rel_rsi"]) is not None and x["rel_rsi"] >= top_rsi:
            megacap_hits.append(f"{x['ticker']} 相对QQQ RSI{rel_signal['rsi_period']} {fmt(x['rel_rsi'],1)} 过热（线 {top_rsi:.0f}）")
        elif finite(x["rel_rsi"]) is not None and x["rel_rsi"] <= bottom_rsi:
            megacap_hits.append(f"{x['ticker']} 相对QQQ RSI{rel_signal['rsi_period']} {fmt(x['rel_rsi'],1)} 过冷（线 {bottom_rsi:.0f}）")

    # 读数句按实际命中数写，没命中才说「全在 40—60 之间」。这句原来写死，2026-09-29 的 signals.json
    # 里六个行业标着过冷，旁边的 reading 却说 11 个行业全在 40—60 之间。
    rel_band = f"{bottom_rsi:.0f}—{top_rsi:.0f}"
    sector_valid = sum(finite(x["rel_rsi"]) is not None for x in sector_signals)
    sector_reading = (
        f"{sector_valid} 个一级行业相对SPX RSI{rel_signal['rsi_period']} 全在 {rel_band} 之间" if not sector_hits
        else f"{len(sector_hits)} 个一级行业相对SPX RSI{rel_signal['rsi_period']} 越线，其余 {sector_valid - len(sector_hits)} 个在 {rel_band} 之间"
    ) + (f"，{len(sector_signals) - sector_valid} 个缺数" if sector_valid < len(sector_signals) else "")
    megacap_valid = sum(finite(x["rel_rsi"]) is not None for x in megacap_signals)
    megacap_reading = (
        f"{megacap_valid} 只相对QQQ RSI{rel_signal['rsi_period']} 全在 {rel_band} 之间" if not megacap_hits
        else f"{len(megacap_hits)} 只有信号，其余 {len(megacap_signals) - len(megacap_hits)} 只无信号"
    ) + (f"，{len(megacap_signals) - megacap_valid} 只缺数" if megacap_valid < len(megacap_signals) else "")

    tsm = extra_signals.get("TSM", {})
    tsm_dev = finite(tsm.get("dev"))
    tsm_state = level_down(tsm_dev, 0.0, DIGEST_NEAR_MA_PCT)
    tsm_signal = {
        "dev": tsm_dev, "ma": tsm.get("ma"), "as_of": tsm.get("as_of", ""),
        "reading": f"距{tsm.get('ma', 100)}DMA {signed_fmt(tsm_dev,1)}，未接近",
        "hit": (f"{'跌破' if tsm_state == '触发' else '接近'}{tsm.get('ma', 100)}DMA · 当前乖离 {signed_fmt(tsm_dev,1)}" if tsm_state else ""),
    }

    afrm = trend_stats.get("AFRM", {})
    afrm_gap = finite(afrm.get("bottom_gap"))  # (趋势底 ÷ 现价 − 1)×100，负数=线还在现价下方
    afrm_state = level_up(afrm_gap, 0.0, DIGEST_NEAR_TREND_PCT)
    afrm_signal = {
        "close": finite(afrm.get("close")), "bottom": finite(afrm.get("bottom")), "gap": afrm_gap, "as_of": afrm.get("as_of", ""),
        "reading": (f"现价 {fmt(afrm.get('close'),2)}，趋势底 {fmt(afrm.get('bottom'),2)}（低于现价 {fmt(abs(afrm_gap) if afrm_gap is not None else None,1,'%')}），未接近"
                    if afrm_gap is not None else "趋势线本次算不出"),
        "hit": (f"{'跌破' if afrm_state == '触发' else '接近'}上行支撑线 · 现价 {fmt(afrm.get('close'),2)}、趋势底 {fmt(afrm.get('bottom'),2)}" if afrm_state else ""),
    }

    # A股：两融 RSI14 走 70/30，中小盘（国证2000）与其余观察指数走温度带的拥挤度冷热区。
    cn_hits: list[str] = []
    if level_up(last_margin, rsi_hot, 0.0):
        cn_hits.append(f"两融余额 RSI14 {fmt(last_margin,1)} 超买（线 {rsi_hot:.0f}）")
    elif level_down(last_margin, rsi_cold, 0.0):
        cn_hits.append(f"两融余额 RSI14 {fmt(last_margin,1)} 超卖（线 {rsi_cold:.0f}）")
    cn_small_name = next((x["name"] for x in cn_a_cfg if x.get("featured")), "国证2000")
    cn_small_score = None
    for item in cn_a_items[1:]:
        if not item:
            continue
        score = finite(item.get("x"))
        if item["name"] == cn_small_name:
            cn_small_score = score
        if score is None:
            continue
        if score >= CROWD_CHIP_HOT:
            cn_hits.append(f"{item['name']} 拥挤度 {score:.1f}% 过高（线 {CROWD_CHIP_HOT:.0f}）")
        elif score <= CROWD_CHIP_COLD:
            cn_hits.append(f"{item['name']} 拥挤度 {score:.1f}% 过低（线 {CROWD_CHIP_COLD:.0f}）")

    hk_hits: list[str] = []
    ah_hot, ah_cold = level_up(last_ah, -36.0, DIGEST_NEAR_AH), level_down(last_ah, -60.0, DIGEST_NEAR_AH)
    if ah_hot:
        hk_hits.append(f"{ah_hot}超买 · AH溢价 RSI30 {fmt(last_ah,1)}（超买线 -36）")
    elif ah_cold:
        hk_hits.append(f"{ah_cold}超卖 · AH溢价 RSI30 {fmt(last_ah,1)}（超卖线 -60）")

    asia_scores = [(x["name"], finite(x.get("x")), x.get("hint", "")) for x in asia_items if x]
    asia_hits = [f"{n} 杠杆综合分 {s:.0f}/100 过热（线 {ZONE_HOT:.0f}）" for n, s, _ in asia_scores if s is not None and s >= ZONE_HOT]

    signals = {
        "ok": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # 卡片标题用发卡当天的日期（同市场晨报口径）；各市场的实际截止日在 as_of 和脚注里。
        "date": datetime.now().astimezone().date().isoformat(),
        "as_of": {
            "us": us_as_of, "bbg": bbg.get("as_of", ""), "gs_prime": gs_prime_store.get("leverage_as_of", ""), "cn_margin": cn["as_of"],
            "cn_crowding": crowding.get("trade_date", ""), "index_history": history["as_of"],
        },
        "us_market": {
            "topbottom": {
                "hits": topbottom_hits,
                "top_hits": top_hits,
                "bottom_hits": bottom_hits,
                "reading": f"{top_reading}；{bottom_reading}",
                "top_reading": top_reading,
                "bottom_reading": bottom_reading,
                "cyc_rsi30": last_cyc30, "vol_ratio": vol_ratio,
                "top_framework": top_framework,
                "bottom_framework": bottom_framework,
                "bottom_environment": bottom_environment,
                "lines": {"top_rsi30": us_top_line, "vol_crush": US_VOL_CRUSH, "gsp_alert_rsi": bottom_cfg.gsp_alert_rsi,
                          "top_confirm_drawdown_pct": top_cfg.confirm_drawdown_pct, "top_window_td": top_cfg.window_td, "top_excess_timeout_td": top_cfg.excess_timeout_td,
                          "put_hedge_vvix_vix": top_cfg.put_hedge_vvix_vix},
            },
            "momentum": {
                "hits": momentum_hits,
                "reading": f"{mom_label} {fmt(last_mom,1)}{mom_lag}",
                "gscbhmom": last_mom, "bbg_as_of": bbg_as_of, "lines": {"hot": rsi_hot, "cold": rsi_cold},
            },
            "ndx_spw": {
                "hits": ndx_spw_hits,
                "reading": f"纳指/等权 RSI14 {fmt(last_ratio,1)}" + ("（RSP代理）" if ratio_proxy else ""),
                "rsi14": last_ratio, "proxy": ratio_proxy, "lines": {"hot": rsi_hot, "cold": rsi_cold},
            },
            "tech_rotation": tech_rotation,
            "sectors": {
                "hits": sector_hits,
                "reading": sector_reading,
                "rows": sector_signals,
            },
            "gs_prime": gs_prime_signal,
        },
        "us_stocks": {
            "megacap": {
                "hits": megacap_hits,
                "reading": megacap_reading,
                "rows": megacap_signals,
            },
            "TSM": tsm_signal,
            "AFRM": afrm_signal,
        },
        "cn_a": {
            "hits": cn_hits,
            "reading": f"两融余额 RSI14 {fmt(last_margin,1)}，{cn_small_name} 拥挤度 {fmt(cn_small_score,1,'%')}",
            "margin_rsi14": last_margin, "smallcap": {"name": cn_small_name, "crowding": cn_small_score},
        },
        "hk": {
            "hits": hk_hits,
            "reading": f"AH溢价 RSI30 {fmt(last_ah,1)}，超买线 -36 / 超卖线 -60",
            "ah_rsi30": last_ah,
        },
        "asia": {
            "hits": asia_hits,
            "reading": " · ".join(f"{n} {s:.0f}" for n, s, _ in asia_scores if s is not None) or "杠杆综合分本次读不到",
            "rows": [{"name": n, "score": s, "hint": h} for n, s, h in asia_scores],
        },
        "footnotes": [
            f"美股与个股读数截至 {us_as_of}（Yahoo 复权收盘）；"
            f"周期/防御、Momentum、AH 来自 Bloomberg CSV，截至 {bbg.get('as_of', '未知')}。",
            f"A股拥挤度截至 {crowding.get('trade_date', '未知')}，两融截至 {cn['as_of']}；日韩台杠杆综合分来自各地监控当日卡片。",
            "阈值与页面上画的参考线同源；「接近」是卡片自己的一档，页面上没有画。完整口径见 eve.lumos.space。",
        ],
        "warnings": [],
    }

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    html_doc = render_html(sections, generated_at, freshness)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_doc, encoding="utf-8")
    overlong_notes = long_notes(html_doc)
    warnings = [x for x in ["NDX/SPW使用RSP代理" if ratio_proxy else None, "FRED部分数据不可用" if any(v.empty for v in fred.values()) else None, f"A股盘中资金流快照缺失：{flow_snapshot.get('error','')}" if not flow_snapshot.get("ok") and not flow_snapshot.get("blocked") else None, f"指数长历史缺失：{'、'.join(history['missing'])}" if history["missing"] else None, f"日韩台图缺数据：{'、'.join(asia_charts['missing'])}" if asia_charts["missing"] else None, f"MSCI指数用缓存未刷新：{'、'.join(asia_charts['stale'])}" if asia_charts["stale"] else None, f"A股量能取数失败：{turnover_snapshot.get('error','')}" if not turnover_snapshot.get("ok") else None, "日韩台综合分历史未落盘，1W/1M位移缺失" if not asia_trail_rows else None, f"行情缺失标的：{'、'.join(price_missing)}" if price_missing else None, f"趋势线算不出：{'、'.join(trend_missing)}" if trend_missing else None, f"config缺趋势线冻结参数：{'、'.join(trend_unfrozen)}（跑 refit-trend 生成）" if trend_unfrozen else None, f"趋势线OHLC取数失败，退回缓存：{'、'.join(trend_stale)}" if trend_stale else None, ("Bloomberg CSV未更新" if not bbg.get("ok") else f"Bloomberg CSV落后{bbg_stale_days}天（{bbg.get('as_of')}），跑 scripts/bbg_remote_refresh.sh" if bbg_stale_days is not None and bbg_stale_days > BBG_STALE_DAYS else None), *[f"美股底部框架：{x}" for x in bottom_framework.get("warnings", [])], *[f"美股底部环境：{x}" for x in bottom_environment.get("warnings", [])], *[f"美股顶部框架：{x}" for x in top_framework.get("warnings", [])], f"图注超过{NOTE_MAX_CHARS}字：{'；'.join(overlong_notes)}" if overlong_notes else None] if x]
    warnings.extend(dict.fromkeys(china_stock_warnings))
    signals["warnings"] = list(warnings)
    # notices 只进构建日志和 --json 结果，不上卡片：已知、已定性、不需要每天处理的取数缺口。
    notices = [f"A股盘中资金流：{flow_snapshot.get('error','')}"] if flow_snapshot.get("blocked") else []
    # signals.json 和 HTML 一起落盘：飞书速览卡片只读这份 JSON，页面和卡片因此同源。
    (output.parent / SIGNALS_NAME).write_text(json.dumps(signals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "overlong_notes": overlong_notes, "output": str(output), "bytes": output.stat().st_size, "generated_at": generated_at, "price_series": len(prices), "price_missing": price_missing, "trend_lines": {k: {m: round(float(v[m]), 3) for m in ("slope", "bottom", "bottom_gap", "rise_6m", "rise_12m", "pierce_pct", "break_pct") if v.get(m) is not None} | ({"top": round(float(v["top"]), 3), "top_gap": round(float(v["top_gap"]), 3)} if "top" in v else {}) | {"fitted_on": v.get("fitted_on", ""), "anchor": v.get("anchor", "")} for k, v in trend_stats.items()}, "trend_missing": trend_missing, "trend_unfrozen": trend_unfrozen, "trend_stale": trend_stale, "bbg_as_of": bbg.get("as_of", ""), "bbg_stale_days": bbg_stale_days, "stock_rows": len(stocks), "crowding_rows": len(crowd_rows), "bbg_momentum_period": mom_period, "fund_flow_boards": len(flow_snapshot.get("boards", [])), "fund_flow_asof": flow_snapshot.get("asof", ""), "cn_a_charts": len(cn_index_charts) + len(cn_featured_charts), "cn_a_featured": len(cn_featured_charts), "cn_broad_charts": len(cn_broad_charts), "hk_charts": len(hk_charts), "asia_charts": asia_charts["charts"], "asia_trail_rows": asia_trail_rows, "turnover": {"ok": turnover_snapshot.get("ok", False), "asof": turnover_snapshot.get("asof", ""), "baseline_days": len(turnover_snapshot.get("days") or []), "today_points": len(turnover_snapshot.get("today") or [])}, "index_history": {"refreshed": len(history["refreshed"]), "stale": history["stale"], "missing": history["missing"], "as_of": history["as_of"]}, "signals": str(output.parent / SIGNALS_NAME), "notices": notices, "signal_hits": sum(len(v) for v in (signals["us_market"]["topbottom"]["hits"], signals["us_market"]["momentum"]["hits"], signals["us_market"]["ndx_spw"]["hits"], signals["us_market"]["sectors"]["hits"], signals["us_market"]["gs_prime"]["hits"], signals["us_stocks"]["megacap"]["hits"], signals["cn_a"]["hits"], signals["hk"]["hits"], signals["asia"]["hits"])), "warnings": warnings}


def render_html(sections: dict[str, str], generated_at: str, freshness: list[list[str]]) -> str:
    nav = [
        ("overview", "总览"), ("us", "美股市场"), ("cn_a", "A股市场"), ("cn_hk", "港股市场"),
        ("asia", "日韩台市场"), ("us_stocks", "美股个股"), ("china_stocks", "中国个股"),
    ]
    buttons = "".join(f'<button data-tab="{k}" class="tab-btn{" active" if i == 0 else ""}">{v}</button>' for i, (k, v) in enumerate(nav))
    bodies = "".join(f'<main id="tab-{k}" class="tab-page{" active" if i == 0 else ""}"><div class="section-title"><h1>{v}</h1></div>{sections[k]}</main>' for i, (k, v) in enumerate(nav))
    css = r"""
:root{--bg:#07101f;--panel:#0f1b2d;--panel2:#13243b;--line:#253a57;--text:#edf4ff;--muted:#91a3bd;--blue:#4da3ff;--yellow:#ffbf3c;--green:#38d39f;--red:#ff6b6b;--purple:#a377ff;--orange:#ff9d3c;--cold:#4da3ff;--tepid:#aebdd2}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#17335d 0,transparent 35%),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}.top{padding:28px 32px 12px;max-width:1600px;margin:auto}.top h1{margin:0;font-size:28px}.top p{color:var(--muted);margin:8px 0}.tabs{position:sticky;top:0;z-index:20;display:flex;gap:8px;padding:10px 32px;background:rgba(7,16,31,.92);backdrop-filter:blur(16px);border-bottom:1px solid var(--line);overflow:auto}.tab-btn{border:1px solid var(--line);background:#0c1728;color:var(--muted);padding:9px 15px;border-radius:10px;cursor:pointer;white-space:nowrap}.tab-btn.active{background:var(--blue);border-color:var(--blue);color:#04101f;font-weight:700}.tab-page{display:none;max-width:1600px;margin:auto;padding:20px 32px 60px}.tab-page.active{display:block}.section-title h1{font-size:22px;margin:0 0 16px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:18px}.metric,.panel{background:linear-gradient(145deg,rgba(19,36,59,.96),rgba(12,25,43,.96));border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 32px rgba(0,0,0,.18)}.metric{padding:17px;border-top:3px solid var(--tepid)}.metric.hot{border-top-color:var(--orange)}.metric.warm{border-top-color:#ffc266}.metric.cold{border-top-color:var(--cold)}.metric.cool{border-top-color:#7fc0ff}.metric.neutral{border-top-color:var(--tepid)}.metric.muted{opacity:.7}.up{color:var(--green)}.down{color:var(--red)}.metric-title{font-size:13px;color:var(--muted)}.metric-value{font-size:30px;font-weight:750;margin:7px 0}.metric-sub{font-size:14px}.metric-meta{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.35}.panel{padding:18px;min-width:0}.panel h3{margin:0 0 12px;font-size:17px}.panel p,.panel li{color:#c5d2e5;line-height:1.55}.panel-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.legend{display:flex;flex-wrap:wrap;gap:12px;color:var(--muted);font-size:12px}.legend span{white-space:nowrap}.legend i{display:inline-block;width:18px;height:3px;margin-right:5px;vertical-align:middle;border-radius:3px}.chart{position:relative;width:100%;margin-top:8px}.chart canvas{width:100%;height:100%;display:block}.tooltip{display:none;position:absolute;pointer-events:none;background:#050b15e8;border:1px solid #405777;border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.45;box-shadow:0 8px 24px #0008;white-space:nowrap}.note{font-size:12px!important;color:var(--muted)!important;margin:8px 0 0}.note.rule{background:#101d31;border:1px solid var(--line);border-radius:10px;padding:9px 12px;margin:0 0 4px}.strip{position:relative;width:100%;margin-top:6px}.strip canvas{width:100%;display:block}.shape{font-size:13px;color:#e4edfa;background:#16273f;border:1px solid var(--line);padding:5px 11px;border-radius:999px;white-space:nowrap}.alerts{font-size:12px;color:var(--muted);margin-top:2px}.alerts.on span{display:inline-block;border-radius:999px;padding:3px 10px;margin:4px 6px 0 0;font-size:12px;border:1px solid transparent}.alerts.on span.hot{background:#3a2a15;border-color:#8a5f24;color:#ffc98a}.alerts.on span.cold{background:#122a45;border-color:#2f5f8a;color:#9ccbff}.strip-panel .panel-head{align-items:center}.stack{display:grid;gap:18px}.grid{display:grid;gap:18px}.grid.two{grid-template-columns:repeat(2,minmax(0,1fr))}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600;background:#0b1728;position:sticky;top:0}.flow{position:relative;width:100%;height:470px;margin-top:8px}.flow canvas{width:100%;height:100%;display:block}.flow-status{font-size:12px;color:var(--muted);background:#16273f;border:1px solid var(--line);padding:5px 11px;border-radius:999px;white-space:nowrap}.flow-status.live{color:#8fe3c4;border-color:#2f6b57}.flow-status.stale{color:#ffc98a;border-color:#8a5f24}.flow-rank{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:12px}.flow-rank h4{margin:0 0 6px;font-size:13px;color:var(--muted);font-weight:600}.flow-rank ol{margin:0;padding-left:20px;font-size:12.5px;color:#c5d2e5;line-height:1.75}.flow-rank b{font-variant-numeric:tabular-nums}.vol{position:relative;width:100%;height:330px;margin-top:8px}.vol canvas{width:100%;height:100%;display:block}.vol-stat{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:12px}.vol-stat div{background:#101d31;border:1px solid var(--line);border-radius:10px;padding:9px 11px}.vol-stat span{display:block;font-size:11px;color:var(--muted)}.vol-stat b{font-size:19px;font-variant-numeric:tabular-nums}@media(max-width:900px){.flow-rank{grid-template-columns:1fr}.flow{height:520px}.vol-stat{grid-template-columns:1fr}}.cb-stats{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin:2px 0 12px}.cb-stat{background:#101d31;border:1px solid var(--line);border-radius:10px;padding:9px 11px}.cb-stat span{display:block;font-size:11px;color:var(--muted)}.cb-stat b{font-size:19px;font-variant-numeric:tabular-nums}.cb-controls{display:grid;grid-template-columns:minmax(180px,1.6fr) repeat(3,minmax(0,1fr)) auto;gap:10px;margin-bottom:10px;align-items:center}.cb-controls input[type=search],.cb-controls select{background:#0c1728;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:8px 11px;font-size:13px;font-family:inherit;width:100%}.cb-controls input[type=search]::placeholder{color:var(--muted)}.cb-toggle{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);white-space:nowrap;cursor:pointer}.cb-shell{max-height:620px;overflow:auto;border:1px solid var(--line);border-radius:12px}.cb-shell table{font-size:12.5px}.cb-shell td{vertical-align:middle}.cb-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}.cb-dot-price{background:var(--blue)}.cb-dot-crowd{background:var(--yellow);margin-left:8px}.cb-rank{color:var(--muted);font-variant-numeric:tabular-nums}.cb-name a{color:var(--text);text-decoration:none;border-bottom:1px solid transparent}.cb-name a:hover{border-bottom-color:var(--blue)}.cb-code{font-size:11px;color:var(--muted)}.cb-tag{background:#16273f;border:1px solid var(--line);color:var(--muted);padding:2px 8px;border-radius:999px;font-size:11px}.cb-score-top{display:flex;gap:8px;justify-content:flex-end;align-items:baseline}.cb-score{font-variant-numeric:tabular-nums;font-weight:650}.cb-level{font-size:11px;color:var(--muted)}.cb-bar{height:4px;border-radius:3px;background:#1a2b45;margin-top:5px;min-width:84px}.cb-bar i{display:block;height:100%;border-radius:3px}.cb-spark{cursor:crosshair;outline:none;width:174px;margin-left:auto}.cb-spark svg{display:block;width:174px;height:56px}.cb-spark:focus-visible svg{outline:1px solid var(--blue)}.cb-grid{stroke:#22354f;stroke-width:1}.cb-price{fill:none;stroke:var(--blue);stroke-width:1.4}.cb-crowd{fill:none;stroke:var(--yellow);stroke-width:1.4}.cb-price-dot{fill:var(--blue)}.cb-crowd-dot{fill:var(--yellow)}.cb-spark-meta{display:flex;gap:10px;justify-content:flex-end;font-size:10.5px;color:var(--muted);margin-top:2px}.cb-spark-empty{font-size:11px;color:var(--muted)}.cb-zoom{position:fixed;z-index:60;background:#050b15f2;border:1px solid #405777;border-radius:12px;padding:12px 14px;box-shadow:0 14px 36px #000a;pointer-events:none}.cb-zoom svg{width:420px;height:136px}.cb-zoom .cb-spark-meta{font-size:12px}.cb-zoom-title{display:flex;gap:12px;justify-content:space-between;font-size:13px;color:var(--text);margin-bottom:6px}.cb-zoom-title span{color:var(--muted);font-size:11px}.cb-zoom-note{font-size:11px;color:var(--muted);margin-top:6px}@media(max-width:1100px){.cb-stats{grid-template-columns:repeat(4,minmax(0,1fr))}.cb-controls{grid-template-columns:1fr 1fr}}.pill{padding:3px 8px;border-radius:999px;font-size:11px}.pill.on{background:#143e35;color:#67e1b7}.pill.off{background:#3c2430;color:#ff9bac}.empty{padding:30px;text-align:center;color:var(--muted)}footer{max-width:1600px;margin:auto;padding:0 32px 30px;color:var(--muted);font-size:11px}@media(max-width:900px){.grid.two{grid-template-columns:1fr}.top,.tabs,.tab-page{padding-left:16px;padding-right:16px}.panel-head{display:block}.legend{margin-bottom:8px}}
"""
    css += r"""
.top-link{display:inline-block;margin-top:7px;color:var(--blue);font-size:14px;font-weight:650;text-decoration:none}.top-link:hover{text-decoration:underline}
"""
    css += MONITOR_CSS
    js = r"""
const $=(s,r=document)=>r.querySelector(s);const $$=(s,r=document)=>[...r.querySelectorAll(s)];
$$('.tab-btn').forEach(b=>b.addEventListener('click',()=>{$$('.tab-btn').forEach(x=>x.classList.remove('active'));$$('.tab-page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#tab-'+b.dataset.tab).classList.add('active');setTimeout(()=>redraw($('#tab-'+b.dataset.tab)),20)}));
function niceStep(span,n){const raw=(Math.abs(span)||1)/n,p=Math.pow(10,Math.floor(Math.log10(Math.abs(raw)||1))),u=raw/p,s=u<=1?1:u<=2?2:u<=2.5?2.5:u<=5?5:10;return s*p}
function chartSegments(data,maxGapDays){const groups=[];data.forEach(p=>{const group=groups.at(-1);if(!group||(maxGapDays&&(+new Date(p[0])-+new Date(group.at(-1)[0]))>maxGapDays*864e5))groups.push([p]);else group.push(p)});return groups;}
function drawChart(el){const cv=$('canvas',el),ctx=cv.getContext('2d'),d=JSON.parse(el.dataset.chart),D=devicePixelRatio||1,w=el.clientWidth,h=el.clientHeight;if(w<20)return;cv.width=w*D;cv.height=h*D;ctx.setTransform(D,0,0,D,0,0);ctx.clearRect(0,0,w,h);const pad={l:58,r:58,t:14,b:28},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;const all=d.series.flatMap(s=>s.data.map(p=>({t:+new Date(p[0]),v:+p[1],a:s.axis})));if(!all.length)return;const xmin=Math.min(...all.map(p=>p.t)),xmax=Math.max(...all.map(p=>p.t)),hasRight=d.series.some(s=>s.axis==='right'),hl=d.hlines||[];
function range(axis){const fixed=axis==='right'?d.rightDomain:d.leftDomain;if(fixed)return fixed.slice();const vals=all.filter(p=>p.a===axis).map(p=>p.v);hl.filter(l=>(l.axis||'left')===axis).forEach(l=>vals.push(l.value));if(!vals.length)return[0,1];const lo=Math.min(...vals),hi=Math.max(...vals),step=niceStep((hi-lo)||Math.abs(hi)||1,4);return[Math.floor(lo/step)*step,Math.ceil(hi/step)*step]}
// Pin one left-axis value onto one right-axis value: keep the right domain untouched and
// restretch the left one so the two anchors land on the same pixel row. Only the anchor is
// aligned; the other gridlines stay whatever each axis needs.
function alignLeft(L,R,a){const rs=(R[1]-R[0]);if(!rs)return L;const p=(R[1]-a.right)/rs;if(!(p>1e-6&&p<1-1e-6))return L;const k=p/(1-p);let top=Math.max(L[1]-a.left,0),bot=Math.max(a.left-L[0],0);if(top<=0&&bot<=0)return L;bot=Math.max(bot,top/k);const step=niceStep(bot*(1+k),4);const span=Math.max(Math.ceil(bot*(1+k)/step)*step,step);const nb=span/(1+k);return[a.left-nb,a.left+k*nb]}
let L=range('left');const R=range('right');
if(d.align&&hasRight&&!d.leftDomain&&!d.reverseLeft)L=alignLeft(L,R,d.align);const xp=t=>pad.l+(t-xmin)/(xmax-xmin||1)*pw,yp=(v,a)=>{const q=a==='right'?R:L;if(a==='left'&&d.reverseLeft)return pad.t+(v-q[0])/(q[1]-q[0]||1)*ph;return pad.t+(q[1]-v)/(q[1]-q[0]||1)*ph};const dec=r=>{const s=Math.abs(r[1]-r[0])/4;return s<1?2:s<10?1:0},dl=dec(L),dr=dec(R);ctx.lineWidth=1;ctx.font='11px -apple-system,sans-serif';
for(let i=0;i<=4;i++){const y=pad.t+ph*i/4;ctx.strokeStyle='#1f3251';ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();const lv=d.reverseLeft?L[0]+(L[1]-L[0])*i/4:L[1]-(L[1]-L[0])*i/4,rv=R[1]-(R[1]-R[0])*i/4;ctx.fillStyle='#8294ae';ctx.textAlign='right';ctx.fillText(lv.toFixed(dl),pad.l-7,y+4);if(hasRight){ctx.textAlign='left';ctx.fillText(rv.toFixed(dr),w-pad.r+7,y+4)}}
hl.forEach(l=>{const a=l.axis||'left',q=a==='right'?R:L;if(l.value<Math.min(q[0],q[1])||l.value>Math.max(q[0],q[1]))return;const y=yp(l.value,a),c=l.tone==='hot'?'#ff9d3c':l.tone==='cold'?'#4da3ff':'#93a7c2';ctx.save();ctx.strokeStyle=c+'99';ctx.setLineDash(l.tone==='zero'?[2,3]:[6,4]);ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.restore();if(l.label){ctx.font='10px -apple-system,sans-serif';ctx.fillStyle=c+'cc';ctx.textAlign='left';ctx.fillText(l.label,pad.l+5,y-4);ctx.font='11px -apple-system,sans-serif'}});
const spanDays=(xmax-xmin)/864e5;ctx.textAlign='center';ctx.fillStyle='#8294ae';for(let i=0;i<5;i++){const t=xmin+(xmax-xmin)*i/4,dt=new Date(t),y=dt.getFullYear(),m=String(dt.getMonth()+1).padStart(2,'0');ctx.fillText(spanDays>150?y+'/'+m:m+'/'+String(dt.getDate()).padStart(2,'0'),xp(t),h-8)}
d.series.forEach(s=>{const groups=chartSegments(s.data,s.maxGapDays).map(group=>group.map(p=>[xp(+new Date(p[0])),yp(+p[1],s.axis)]));if(!groups.length)return;groups.forEach(pts=>{if(s.fill){const g=ctx.createLinearGradient(0,pad.t,0,pad.t+ph);g.addColorStop(0,s.color+'55');g.addColorStop(1,s.color+'05');ctx.fillStyle=g;ctx.beginPath();ctx.moveTo(pts[0][0],pad.t+ph);pts.forEach(p=>ctx.lineTo(...p));ctx.lineTo(pts.at(-1)[0],pad.t+ph);ctx.closePath();ctx.fill()}ctx.strokeStyle=s.color;ctx.lineWidth=1.8;ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.stroke()});const last=groups.at(-1).at(-1);ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(last[0],last[1],3.2,0,6.284);ctx.fill()});el._chart={d,xmin,xmax,xp,yp,pad,pw,ph,w,h,ctx};}
function hover(e){const el=e.currentTarget,c=el._chart;if(!c)return;const r=el.getBoundingClientRect(),rawX=e.clientX-r.left,t=Math.min(c.xmax,Math.max(c.xmin,c.xmin+(rawX-c.pad.l)/c.pw*(c.xmax-c.xmin)));const lines=[];let nearest=null;c.d.series.forEach(s=>{if(!s.data.length)return;let best=s.data.reduce((a,b)=>Math.abs(+new Date(b[0])-t)<Math.abs(+new Date(a[0])-t)?b:a);const bt=+new Date(best[0]);if(s.maxGapDays&&Math.abs(bt-t)>s.maxGapDays*864e5/2){lines.push(`<span style="color:${s.color}">●</span> ${s.name}: 暂无数据`);return}if(nearest==null||Math.abs(bt-t)<Math.abs(nearest-t))nearest=bt;lines.push(`<span style="color:${s.color}">●</span> ${s.name}: <b>${(+best[1]).toFixed(2)}</b>`)});const hx=c.pad.l+((nearest==null?t:nearest)-c.xmin)/(c.xmax-c.xmin||1)*c.pw;c.ctx.save();c.ctx.strokeStyle='#8ea5c4aa';c.ctx.setLineDash([4,4]);c.ctx.beginPath();c.ctx.moveTo(hx,c.pad.t);c.ctx.lineTo(hx,c.pad.t+c.ph);c.ctx.stroke();c.ctx.restore();const dt=new Date(nearest==null?t:nearest).toISOString().slice(0,10),tip=$('.tooltip',el);tip.innerHTML=`<b>${dt}</b><br>${lines.join('<br>')}`;tip.style.display='block';tip.style.left=Math.min(Math.max(hx+12,8),c.w-tip.offsetWidth-8)+'px';tip.style.top='20px';}
function leave(e){$('.tooltip',e.currentTarget).style.display='none';drawChart(e.currentTarget)}
function zoneColor(x){return x>=80?'#ff9d3c':x<=20?'#4da3ff':'#aebdd2'}
const SCALE_ORDER={z:0,pct:1,score:2},SCALE_TAG={z:'稳健z · 每格1σ',pct:'上游拥挤度1年分位',score:'杠杆综合分 0–100'},Z_FULL=2.5;
function drawStrip(el){const cv=$('canvas',el),ctx=cv.getContext('2d'),d=JSON.parse(el.dataset.strip),D=devicePixelRatio||1,w=el.clientWidth;if(w<20)return;ctx.setTransform(1,0,0,1,0,0);ctx.font='12px -apple-system,"PingFang SC",sans-serif';
const rk=s=>SCALE_ORDER[s]!==undefined?SCALE_ORDER[s]:9,items=d.items.slice().sort((a,b)=>d.fixed?rk(a.scale)-rk(b.scale):rk(a.scale)-rk(b.scale)||b.x-a.x);
const hasZ=items.some(it=>it.scale==='z'),hasRaw=items.some(it=>it.scale!=='z'),mixed=new Set(items.map(it=>it.scale)).size>1;let g=0;items.forEach(it=>{g=Math.max(g,ctx.measureText(it.name).width)});
const pad={l:Math.min(Math.max(g+16,90),210),r:24,t:hasZ?44:28,b:hasRaw?46:30},rowH=26,dotR=5,pw=w-pad.l-pad.r,X=p=>pad.l+Math.max(0,Math.min(100,p))/100*pw;
const h=pad.t+items.length*rowH+pad.b;el.style.height=h+'px';cv.width=w*D;cv.height=h*D;ctx.setTransform(D,0,0,D,0,0);ctx.clearRect(0,0,w,h);ctx.font='12px -apple-system,"PingFang SC",sans-serif';
const top=pad.t-9,bot=pad.t+items.length*rowH+5;
ctx.fillStyle='rgba(77,163,255,.10)';ctx.fillRect(X(0),top,X(20)-X(0),bot-top);
ctx.fillStyle='rgba(255,255,255,.026)';ctx.fillRect(X(20),top,X(80)-X(20),bot-top);
ctx.fillStyle='rgba(255,157,60,.13)';ctx.fillRect(X(80),top,X(100)-X(80),bot-top);
ctx.save();ctx.setLineDash([3,4]);ctx.strokeStyle='#2c4265';[20,50,80].forEach(v=>{ctx.beginPath();ctx.moveTo(X(v),top);ctx.lineTo(X(v),bot);ctx.stroke()});ctx.restore();
ctx.font='10px -apple-system,"PingFang SC",sans-serif';ctx.fillStyle='#6f84a1';ctx.textAlign='center';const yZone=hasZ?pad.t-31:pad.t-15;ctx.fillText('冷区',(X(0)+X(20))/2,yZone);ctx.fillText('中性',(X(20)+X(80))/2,yZone);ctx.fillText('热区',(X(80)+X(100))/2,yZone);
if(hasZ){[[-2,'−2σ'],[-1,'−1σ'],[0,'0'],[1,'+1σ'],[2,'+2σ']].forEach(p=>ctx.fillText(p[1],X(50+50*p[0]/Z_FULL),pad.t-14))}
if(mixed&&items.length){ctx.textAlign='right';ctx.fillStyle='#8fa4c0';ctx.fillText(SCALE_TAG[items[0].scale]||'',pad.l-11,hasZ?pad.t-14:pad.t-15)}
ctx.font='12px -apple-system,"PingFang SC",sans-serif';
items.forEach((it,i)=>{const y=pad.t+i*rowH+rowH/2,c=zoneColor(it.x),xn=X(it.x);
if(i%2){ctx.fillStyle='rgba(255,255,255,.016)';ctx.fillRect(pad.l,y-rowH/2,pw,rowH)}
if(mixed&&i&&items[i-1].scale!==it.scale){const yd=y-rowH/2,tag=SCALE_TAG[it.scale]||'';ctx.save();ctx.setLineDash([4,4]);ctx.strokeStyle='#3a527a';ctx.beginPath();ctx.moveTo(pad.l,yd);ctx.lineTo(w-pad.r,yd);ctx.stroke();ctx.setLineDash([]);if(tag){ctx.font='10px -apple-system,"PingFang SC",sans-serif';const tw=ctx.measureText(tag).width;ctx.fillStyle='#101d31';ctx.fillRect(pad.l+10,yd-7,tw+12,14);ctx.fillStyle='#8fa4c0';ctx.textAlign='left';ctx.fillText(tag,pad.l+16,yd+3.5);ctx.font='12px -apple-system,"PingFang SC",sans-serif'}ctx.restore()}
const path=[];if(it.x1m!=null)path.push(X(it.x1m));if(it.x1w!=null)path.push(X(it.x1w));path.push(xn);
if(path.length>1){ctx.save();ctx.strokeStyle=c+'4d';ctx.lineWidth=2.5;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(path[0],y);path.forEach(v=>ctx.lineTo(v,y));ctx.stroke();ctx.restore()}
if(it.x1m!=null){ctx.save();ctx.strokeStyle=c+'8c';ctx.lineWidth=1.3;ctx.beginPath();ctx.arc(X(it.x1m),y,3.1,0,6.284);ctx.stroke();ctx.restore()}
if(it.x1w!=null){ctx.fillStyle=c+'aa';ctx.beginPath();ctx.arc(X(it.x1w),y,3.1,0,6.284);ctx.fill()}
ctx.fillStyle=c;ctx.beginPath();ctx.arc(xn,y,dotR,0,6.284);ctx.fill();
ctx.fillStyle='#c9d7ea';ctx.textAlign='right';ctx.fillText(it.name,pad.l-11,y+4);it._x=xn;it._y=y});
if(hasRaw){ctx.font='10px -apple-system,"PingFang SC",sans-serif';ctx.fillStyle='#6f84a1';ctx.textAlign='center';[0,20,50,80,100].forEach(v=>ctx.fillText(String(v),X(v),h-26))}
ctx.font='11px -apple-system,"PingFang SC",sans-serif';ctx.fillStyle='#7f93ae';ctx.textAlign='left';ctx.fillText('← 冷',pad.l,h-9);ctx.textAlign='center';ctx.fillText('○1M前  ●1W前  ⬤当前',X(50),h-9);ctx.textAlign='right';ctx.fillText('热 · 拥挤 →',w-pad.r,h-9);
el._strip={items,w,h,rowH,top:pad.t};}
function stripHover(e){const el=e.currentTarget,st=el._strip,tip=$('.tooltip',el);if(!st)return;const r=el.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;let hit=null;st.items.forEach(it=>{if(Math.abs(my-it._y)<=st.rowH/2)hit=it});if(!hit){tip.style.display='none';return}
const s0=v=>(v>=0?'+':'')+v.toFixed(0),s2=v=>(v>=0?'+':'')+v.toFixed(2),rows=[];
if(hit.scale==='z'){rows.push(`稳健z <b>${s2(hit.z)}σ</b>`+(hit.z1w!=null?` · 1W ${s2(hit.z1w)}σ`:'')+(hit.z1m!=null?` · 1M ${s2(hit.z1m)}σ`:''));if(hit.pct!=null)rows.push(`1年分位 ${hit.pct.toFixed(0)}`)}
else{const d1w=hit.x1w!=null?hit.x-hit.x1w:null,d1m=hit.x1m!=null?hit.x-hit.x1m:null;rows.push((hit.scale==='score'?'综合分 <b>':'1年分位 <b>')+hit.x.toFixed(0)+'</b>'+(d1w!=null?` · 1W ${s0(d1w)}`:'')+(d1m!=null?` · 1M ${s0(d1m)}`:'')+(d1w==null&&d1m==null?' · 无历史路径':''))}
if(hit.raw)rows.push(`原值 ${hit.raw}`+(hit.range?`<br>1年原值区间 ${hit.range}`:''));
if(hit.scale==='z')rows.push('<span style="color:#8fa4c0">当前=当日原值 · 1W/1M=该日前后共5日中位</span>');
if(hit.hint)rows.push(hit.hint);if(hit.asof)rows.push(hit.asof);
tip.innerHTML=`<b>${hit.name}</b><br>`+rows.join('<br>');
tip.style.display='block';tip.style.left=Math.min(Math.max(mx+14,8),st.w-tip.offsetWidth-8)+'px';tip.style.top=Math.max(hit._y-14,4)+'px';}
/* ── A股盘中资金流向：浏览器用 JSONP 直连东方财富，交易时段内自行刷新 ── */
const FLOW_UP='#38d39f',FLOW_DOWN='#ff6b6b',FLOW_SPAN=240;
function jsonp(url,ms=15000){return new Promise((ok,bad)=>{const cb='ocjp'+Math.random().toString(36).slice(2)+Date.now().toString(36),s=document.createElement('script');let t;const done=(e,v)=>{clearTimeout(t);try{delete window[cb]}catch(_){window[cb]=void 0}s.remove();e?bad(e):ok(v)};window[cb]=v=>done(null,v);t=setTimeout(()=>done(new Error('请求超时')),ms);s.onerror=()=>done(new Error('网络请求失败'));s.src=url+(url.includes('?')?'&':'?')+'cb='+cb+'&_='+Date.now();document.head.appendChild(s)})}
async function pooled(items,n,fn){const out=[];for(let i=0;i<items.length;i+=n)out.push(...await Promise.all(items.slice(i,i+n).map(fn)));return out}
function cnClock(tz){const p=new Intl.DateTimeFormat('en-GB',{timeZone:tz,weekday:'short',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(new Date()),g=k=>(p.find(x=>x.type===k)||{}).value||'';return{wd:g('weekday'),hm:g('hour')+':'+g('minute')}}
function cnOpen(cfg){const n=cnClock(cfg.tz);if(n.wd==='Sat'||n.wd==='Sun')return false;return cfg.sessions.some(s=>n.hm>=s[0]&&n.hm<=s[1])}
/* 午休 11:30-13:00 不占横轴：上午 09:30-11:30 映射到 0-120，下午 13:00-15:00 接着走 120-240 */
function flowMin(t){const p=String(t).split(':'),v=+p[0]*60+ +p[1];return v<=690?v-570:v-660}
function drawFlow(el){const cv=$('canvas',el),ctx=cv.getContext('2d'),st=el._flow;if(!st)return;const snap=st.snapshot||{},boards=(snap.boards||[]).filter(b=>b.points&&b.points.length),D=devicePixelRatio||1,w=el.clientWidth,h=el.clientHeight;if(w<20)return;cv.width=w*D;cv.height=h*D;ctx.setTransform(D,0,0,D,0,0);ctx.clearRect(0,0,w,h);ctx.font='12px -apple-system,"PingFang SC",sans-serif';
if(!boards.length){ctx.fillStyle='#91a3bd';ctx.textAlign='center';ctx.fillText(snap.error||'暂无盘中数据',w/2,h/2);el._flowHit=null;return}
let gut=0;boards.forEach(b=>{gut=Math.max(gut,ctx.measureText(b.name+'  +00.00').width)});
const pad={l:56,r:Math.min(gut+24,236),t:14,b:30},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;
let lo=0,hi=0;boards.forEach(b=>b.points.forEach(p=>{lo=Math.min(lo,p[1]);hi=Math.max(hi,p[1])}));
const step=niceStep((hi-lo)||1,5);lo=Math.floor(lo/step)*step;hi=Math.ceil(hi/step)*step;
const X=m=>pad.l+Math.max(0,Math.min(FLOW_SPAN,m))/FLOW_SPAN*pw,Y=v=>pad.t+(hi-v)/((hi-lo)||1)*ph;
ctx.fillStyle='rgba(56,211,159,.05)';ctx.fillRect(pad.l,pad.t,pw,Math.max(0,Y(0)-pad.t));
ctx.fillStyle='rgba(255,107,107,.05)';ctx.fillRect(pad.l,Y(0),pw,Math.max(0,pad.t+ph-Y(0)));
ctx.font='11px -apple-system,sans-serif';ctx.textAlign='right';ctx.lineWidth=1;
for(let v=lo;v<=hi+1e-9;v+=step){const y=Y(v);ctx.strokeStyle=Math.abs(v)<1e-9?'#3d5b83':'#1f3251';ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+pw,y);ctx.stroke();ctx.fillStyle='#8294ae';ctx.fillText(v.toFixed(step<1?1:0)+'亿',pad.l-7,y+4)}
ctx.save();ctx.setLineDash([3,4]);ctx.strokeStyle='#2c4265';ctx.beginPath();ctx.moveTo(X(120),pad.t);ctx.lineTo(X(120),pad.t+ph);ctx.stroke();ctx.restore();
ctx.textAlign='center';ctx.fillStyle='#8294ae';[[0,'09:30'],[60,'10:30'],[120,'11:30/13:00'],[180,'14:00'],[FLOW_SPAN,'15:00']].forEach(t=>ctx.fillText(t[1],X(t[0]),h-9));
const ends=[];boards.forEach(b=>{const c=b.net>=0?FLOW_UP:FLOW_DOWN,pts=b.points.map(p=>[X(flowMin(p[0])),Y(p[1])]);ctx.strokeStyle=c+'d0';ctx.lineWidth=1.7;ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.stroke();const e=pts[pts.length-1];ctx.fillStyle=c;ctx.beginPath();ctx.arc(e[0],e[1],2.8,0,6.284);ctx.fill();ends.push({b:b,c:c,x:e[0],y:e[1]})});
ends.sort((a,z)=>a.y-z.y);const lh=18;let cur=pad.t+6-lh;ends.forEach(e=>{e.ly=Math.max(e.y,cur+lh);cur=e.ly});cur=pad.t+ph-6+lh;for(let i=ends.length-1;i>=0;i--){ends[i].ly=Math.min(ends[i].ly,cur-lh);cur=ends[i].ly}
ctx.font='12px -apple-system,"PingFang SC",sans-serif';ctx.textAlign='left';
ends.forEach(e=>{ctx.save();ctx.strokeStyle=e.c+'66';ctx.setLineDash([2,3]);ctx.beginPath();ctx.moveTo(e.x+3,e.y);ctx.lineTo(pad.l+pw+9,e.ly);ctx.stroke();ctx.restore();ctx.fillStyle=e.c;ctx.fillText(e.b.name+'  '+(e.b.net>=0?'+':'')+e.b.net.toFixed(2),pad.l+pw+13,e.ly+4)});
el._flowHit={boards:boards,X:X,pad:pad,pw:pw,ph:ph,w:w,ctx:ctx};}
function flowHover(e){const el=e.currentTarget,tip=$('.tooltip',el);if(!el._flowHit){tip.style.display='none';return}
const r=el.getBoundingClientRect(),mx=e.clientX-r.left;drawFlow(el);const c=el._flowHit;if(!c){tip.style.display='none';return}
const m=Math.round((mx-c.pad.l)/c.pw*FLOW_SPAN);if(m<0||m>FLOW_SPAN){tip.style.display='none';return}
c.ctx.save();c.ctx.strokeStyle='#8ea5c4aa';c.ctx.setLineDash([4,4]);c.ctx.beginPath();c.ctx.moveTo(c.X(m),c.pad.t);c.ctx.lineTo(c.X(m),c.pad.t+c.ph);c.ctx.stroke();c.ctx.restore();
let stamp='';const rows=c.boards.map(b=>{let best=b.points[0];b.points.forEach(p=>{if(Math.abs(flowMin(p[0])-m)<Math.abs(flowMin(best[0])-m))best=p});if(!stamp||flowMin(best[0])<=m)stamp=best[0];return{n:b.name,v:best[1],c:b.net>=0?FLOW_UP:FLOW_DOWN}}).sort((a,z)=>z.v-a.v);
tip.innerHTML='<b>'+stamp+'</b><br>'+rows.map(x=>'<span style="color:'+x.c+'">●</span> '+x.n+': <b>'+(x.v>=0?'+':'')+x.v.toFixed(2)+'</b>亿').join('<br>');
tip.style.display='block';tip.style.left=Math.min(Math.max(mx+14,8),c.w-tip.offsetWidth-8)+'px';tip.style.top='14px';}
function flowRender(el){drawFlow(el);const st=el._flow,snap=st.snapshot||{},art=el.parentElement,box=$('[data-flow-rank]',art),sb=$('[data-flow-status]',art),rk=snap.rank||{};
const list=rows=>'<ol>'+rows.map(r=>r.name+' <b>'+(r.net>=0?'+':'')+r.net.toFixed(2)+'</b>').map(x=>'<li>'+x+'</li>').join('')+'</ol>';
box.innerHTML=(rk.top&&rk.top.length)?'<div><h4>主力净流入前10（亿元）</h4>'+list(rk.top)+'</div><div><h4>主力净流出前10（亿元）</h4>'+list(rk.bottom||[])+'</div>':'';
const open=cnOpen(st.cfg),mins=Math.round(st.cfg.refreshMs/60000);
if(st.err)sb.className='flow-status stale',sb.textContent='刷新失败：'+st.err+(snap.asof?'（仍显示 '+snap.asof+'）':'');
else if(!snap.ok)sb.className='flow-status stale',sb.textContent='取数失败：'+(snap.error||'未知');
else sb.className='flow-status'+(open?' live':''),sb.textContent='更新于 '+(snap.asof||'—')+(open?' · 交易中，每'+mins+'分钟刷新':' · 非交易时段，停在最新一笔');}
async function flowFetch(el){const st=el._flow,cfg=st.cfg;if(st.busy)return;st.busy=true;
try{const side=async u=>{const r=await jsonp(u);return (((r||{}).data||{}).diff||[]).filter(x=>x&&x.f12&&isFinite(+x.f62)).map(x=>({code:String(x.f12),name:String(x.f14||x.f12),net:+(+x.f62/1e8).toFixed(2)}))};
const pair=await Promise.all([side(cfg.rankTop),side(cfg.rankBottom)]),tops=pair[0],bots=pair[1];
if(!tops.length&&!bots.length)throw new Error('板块排名接口返回空列表');
const n=cfg.sideN,seen={},picks=tops.slice(0,n).concat(bots.slice(0,n)).filter(b=>seen[b.code]?false:(seen[b.code]=1));
const got=await pooled(picks,4,async b=>{try{const k=await jsonp(cfg.kline+b.code),ln=(k&&k.data&&k.data.klines)||[],pts=[];let stamp='';ln.forEach(s=>{const p=String(s).split(',');if(p.length<2)return;const v=+p[1];if(!isFinite(v))return;stamp=p[0];pts.push([p[0].slice(-5),+(v/1e8).toFixed(3)])});return{code:b.code,name:b.name,net:b.net,points:pts,stamp:stamp}}catch(_){return{code:b.code,name:b.name,net:b.net,points:[],stamp:''}}});
const boards=got.filter(b=>b.points.length);if(!boards.length)throw new Error('分钟资金流接口没有返回任何曲线');
st.snapshot={ok:true,error:'',asof:boards.map(b=>b.stamp).filter(Boolean).sort().pop()||'',boards:boards,rank:{top:tops.slice(0,10),bottom:bots.slice(0,10)}};st.err='';}
catch(e){st.err=String((e&&e.message)||e)}
st.busy=false;st.last=Date.now();flowRender(el)}
function flowInit(el){try{el._flow=JSON.parse(el.dataset.flow)}catch(_){return}el._flow.last=0;el._flow.err='';el._flow.busy=false;flowRender(el);
const tick=()=>{const st=el._flow;if(st.busy)return;if(!st.last||(cnOpen(st.cfg)&&Date.now()-st.last>=st.cfg.refreshMs))flowFetch(el)};
tick();setInterval(tick,30000);
el.addEventListener('mousemove',flowHover);el.addEventListener('mouseleave',()=>{$('.tooltip',el).style.display='none';drawFlow(el)});}
/* ── A股全市场量能：历史基线由构建时写入，今日曲线交易时段内自行刷新 ── */
const VOL_TODAY='#4da3ff',VOL_PREV='#8294ae',VOL_MED='#ffbf3c';
function volLines(el){const st=el._vol;if(!st)return[];const s=st.snapshot||{};return[
{k:'med',name:((s.days||[]).length?'前'+s.days.length+'日同一时刻中位':'历史参考数据缺失'),color:VOL_MED,dash:[5,4],pts:s.median||[]},
{k:'prev',name:'上一交易日'+(s.prev_date?'（'+s.prev_date+'）':''),color:VOL_PREV,dash:[],pts:s.prev||[]},
{k:'today',name:'今日'+(s.date?'（'+s.date+'）':''),color:VOL_TODAY,dash:[],pts:st.live&&st.live.length?st.live:(s.today||[])}].filter(l=>l.pts.length>1)}
function drawVol(el){const cv=$('canvas',el),ctx=cv.getContext('2d'),D=devicePixelRatio||1,w=el.clientWidth,h=el.clientHeight;if(w<20)return;cv.width=w*D;cv.height=h*D;ctx.setTransform(D,0,0,D,0,0);ctx.clearRect(0,0,w,h);ctx.font='12px -apple-system,"PingFang SC",sans-serif';
const lines=volLines(el);if(!lines.length){ctx.fillStyle='#91a3bd';ctx.textAlign='center';ctx.fillText(((el._vol||{}).snapshot||{}).error||'暂无量能数据',w/2,h/2);el._volHit=null;return}
const pad={l:60,r:18,t:14,b:30},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;
let hi=0;lines.forEach(l=>l.pts.forEach(p=>{hi=Math.max(hi,p[1])}));
const step=niceStep(hi||1,5);hi=Math.ceil(hi/step)*step;
const X=m=>pad.l+Math.max(0,Math.min(FLOW_SPAN,m))/FLOW_SPAN*pw,Y=v=>pad.t+(hi-v)/(hi||1)*ph;
ctx.font='11px -apple-system,sans-serif';ctx.textAlign='right';ctx.lineWidth=1;
for(let v=0;v<=hi+1e-9;v+=step){const y=Y(v);ctx.strokeStyle='#1f3251';ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+pw,y);ctx.stroke();ctx.fillStyle='#8294ae';ctx.fillText(v.toFixed(step<0.5?2:1)+'万亿',pad.l-7,y+4)}
ctx.save();ctx.setLineDash([3,4]);ctx.strokeStyle='#2c4265';ctx.beginPath();ctx.moveTo(X(120),pad.t);ctx.lineTo(X(120),pad.t+ph);ctx.stroke();ctx.restore();
ctx.textAlign='center';ctx.fillStyle='#8294ae';[[0,'09:30'],[60,'10:30'],[120,'11:30/13:00'],[180,'14:00'],[FLOW_SPAN,'15:00']].forEach(t=>ctx.fillText(t[1],X(t[0]),h-9));
lines.forEach(l=>{ctx.save();ctx.setLineDash(l.dash);ctx.strokeStyle=l.color;ctx.lineWidth=l.k==='today'?2.2:1.5;ctx.beginPath();l.pts.forEach((p,i)=>{const x=X(flowMin(p[0])),y=Y(p[1]);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.restore();
const e=l.pts[l.pts.length-1];ctx.fillStyle=l.color;ctx.beginPath();ctx.arc(X(flowMin(e[0])),Y(e[1]),l.k==='today'?3.2:2.4,0,6.284);ctx.fill()});
el._volHit={lines:lines,X:X,pad:pad,pw:pw,ph:ph,w:w,ctx:ctx};}
function volAt(pts,m){if(!pts.length)return null;let best=null;pts.forEach(p=>{const d=flowMin(p[0]);if(d<=m+1e-9&&(!best||d>flowMin(best[0])))best=p});return best}
function volHover(e){const el=e.currentTarget,tip=$('.tooltip',el);if(!el._volHit){tip.style.display='none';return}
const r=el.getBoundingClientRect(),mx=e.clientX-r.left;drawVol(el);const c=el._volHit;if(!c)return;
const m=Math.round((mx-c.pad.l)/c.pw*FLOW_SPAN);if(m<0||m>FLOW_SPAN){tip.style.display='none';return}
c.ctx.save();c.ctx.strokeStyle='#8ea5c4aa';c.ctx.setLineDash([4,4]);c.ctx.beginPath();c.ctx.moveTo(c.X(m),c.pad.t);c.ctx.lineTo(c.X(m),c.pad.t+c.ph);c.ctx.stroke();c.ctx.restore();
const rows=c.lines.map(l=>{const p=volAt(l.pts,m);return p?'<span style="color:'+l.color+'">●</span> '+l.name+': <b>'+p[1].toFixed(2)+'</b>万亿':null}).filter(Boolean);
const t=volAt((c.lines.find(l=>l.k==='today')||{pts:[]}).pts,m),md=volAt((c.lines.find(l=>l.k==='med')||{pts:[]}).pts,m);
if(t&&md&&md[1]>0)rows.push('今日/中位 <b>'+((t[1]/md[1]-1)*100>=0?'+':'')+((t[1]/md[1]-1)*100).toFixed(1)+'%</b>');
tip.innerHTML='<b>'+(t?t[0]:'')+'</b><br>'+rows.join('<br>');
tip.style.display='block';tip.style.left=Math.min(Math.max(mx+14,8),c.w-tip.offsetWidth-8)+'px';tip.style.top='14px';}
function volRender(el){drawVol(el);const st=el._vol,s=st.snapshot||{},art=el.parentElement,box=$('[data-vol-stat]',art),sb=$('[data-vol-status]',art);
const today=(st.live&&st.live.length?st.live:(s.today||[])),last=today[today.length-1];
const cmp=(pts)=>{if(!last||!pts||!pts.length)return null;const p=volAt(pts,flowMin(last[0]));return p&&p[1]>0?(last[1]/p[1]-1)*100:null};
const dm=cmp(s.median),dp=cmp(s.prev),fx=v=>v===null?'--':(v>=0?'+':'')+v.toFixed(1)+'%';
const cls=v=>v===null?'':v>=0?'up':'down';
box.innerHTML=last?'<div><span>当前累计成交额（'+last[0]+'）</span><b>'+last[1].toFixed(2)+' 万亿</b></div>'
+'<div><span>'+((s.days||[]).length?'较前'+s.days.length+'日同一时刻中位':'历史参考数据缺失')+'</span><b class="'+cls(dm)+'">'+fx(dm)+'</b></div>'
+'<div><span>较上一交易日同一时刻</span><b class="'+cls(dp)+'">'+fx(dp)+'</b></div>':'';
const open=cnOpen(st.cfg),mins=Math.round(st.cfg.refreshMs/60000);
if(!s.ok)sb.className='flow-status stale',sb.textContent='取数失败：'+(s.error||'未知');
else if(st.err)sb.className='flow-status stale',sb.textContent='盘中刷新失败：'+st.err+'，显示构建时曲线（'+(s.asof||'—')+'）';
else sb.className='flow-status'+(open?' live':''),sb.textContent='更新于 '+(st.liveAsof||s.asof||'—')+(open?' · 交易中，每'+mins+'分钟刷新':' · 非交易时段，停在当日最后一笔');}
async function volFetch(el){const st=el._vol,cfg=st.cfg;if(st.busy)return;st.busy=true;
try{const parts=await Promise.all(cfg.secids.map(async id=>{const r=await jsonp(cfg.trend+id),tr=(((r||{}).data||{}).trends)||[];const out={};tr.forEach(s=>{const p=String(s).split(',');const v=+p[3];if(p.length>3&&isFinite(v))out[p[0].slice(11,16)]=v});return out}));
if(!parts.length||!Object.keys(parts[0]).length)throw new Error('分钟线接口返回空');
const labels=Object.keys(parts[0]).sort();let run=0;const pts=[];
labels.forEach(t=>{let v=0,ok=true;parts.forEach(p=>{if(p[t]===undefined)ok=false;else v+=p[t]});if(!ok)return;run+=v;pts.push([t,+(run/cfg.unit).toFixed(4)])});
if(pts.length<2)throw new Error('可用分钟数不足');
st.live=pts;st.liveAsof=((st.snapshot||{}).date||'')+' '+pts[pts.length-1][0];st.err='';}
catch(e){st.err=String((e&&e.message)||e)}
st.busy=false;st.last=Date.now();volRender(el)}
function volInit(el){try{el._vol=JSON.parse(el.dataset.vol)}catch(_){return}el._vol.last=0;el._vol.err='';el._vol.busy=false;el._vol.live=null;volRender(el);
const tick=()=>{const st=el._vol;if(st.busy||!(st.snapshot||{}).ok)return;if(!st.last||(cnOpen(st.cfg)&&Date.now()-st.last>=st.cfg.refreshMs))volFetch(el)};
tick();setInterval(tick,30000);
el.addEventListener('mousemove',volHover);el.addEventListener('mouseleave',()=>{$('.tooltip',el).style.display='none';drawVol(el)});}
$$('.vol').forEach(volInit);
function redraw(root){$$('.chart',root||document).forEach(drawChart);$$('.strip',root||document).forEach(drawStrip);$$('.flow',root||document).forEach(drawFlow);$$('.vol',root||document).forEach(drawVol)}
$$('.chart').forEach(el=>{drawChart(el);el.addEventListener('mousemove',e=>{drawChart(el);hover(e)});el.addEventListener('mouseleave',leave)});
$$('.strip').forEach(el=>{drawStrip(el);el.addEventListener('mousemove',stripHover);el.addEventListener('mouseleave',()=>{$('.tooltip',el).style.display='none'})});
$$('.flow').forEach(flowInit);
/* --- crowding board (China tab) --- */
(()=>{const node=document.getElementById('cbData');if(!node)return;let DATA=[];try{DATA=JSON.parse(node.textContent)}catch(e){return}
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const num=v=>(v===null||v===undefined||!isFinite(v))?null:Number(v);
const pct=v=>num(v)===null?'--':Number(v).toFixed(2)+'%';
const cbColor=v=>num(v)===null?'#7f93ae':v<15?'#38d39f':v<40?'#7fd6b4':v<60?'#ffbf3c':v<85?'#ff9d3c':'#ff6b6b';
const path=(vals,min,max,w,h,pad)=>{const span=(max-min)||1;return vals.map((v,i)=>{const x=pad+i*(w-2*pad)/Math.max(1,vals.length-1);const y=h-pad-(v-min)*(h-2*pad)/span;return (i?'L':'M')+x.toFixed(2)+','+y.toFixed(2)}).join(' ')};
function svgFor(r,w,h){const pad=3,hp=r.hp||[],hc=r.hc||[];if(hp.length<2)return'';
const lo=Math.min.apply(null,hp),hi=Math.max.apply(null,hp),grow=Math.max((hi-lo)*0.06,0.8),pMin=lo-grow,pMax=hi+grow;
const lastP=hp[hp.length-1],lastC=hc[hc.length-1];
const py=h-pad-(lastP-pMin)*(h-2*pad)/((pMax-pMin)||1),cy=h-pad-lastC*(h-2*pad)/100;
return '<svg viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+esc(r.index_name)+'过去一年价格与拥挤度">'
+'<line class="cb-grid" x1="'+pad+'" y1="'+(h/2)+'" x2="'+(w-pad)+'" y2="'+(h/2)+'"></line>'
+'<path class="cb-price" d="'+path(hp,pMin,pMax,w,h,pad)+'"></path><path class="cb-crowd" d="'+path(hc,0,100,w,h,pad)+'"></path>'
+'<circle class="cb-price-dot" cx="'+(w-pad)+'" cy="'+py.toFixed(2)+'" r="2.1"></circle>'
+'<circle class="cb-crowd-dot" cx="'+(w-pad)+'" cy="'+cy.toFixed(2)+'" r="2.1"></circle></svg>'}
function meta(r){const hp=r.hp||[],hc=r.hc||[];const ret=hp[hp.length-1]-100;return '<div class="cb-spark-meta"><span>价格 '+(ret>=0?'+':'')+ret.toFixed(1)+'%</span><span>拥挤 '+hc[hc.length-1].toFixed(1)+'</span></div>'}
function spark(r){if((r.hp||[]).length<2)return '<div class="cb-spark-empty">暂无一年历史</div>';
return '<div class="cb-spark" tabindex="0" data-code="'+esc(r.index_code)+'">'+svgFor(r,174,56)+meta(r)+'</div>'}
const el=id=>document.getElementById(id),body=el('cbRows'),empty=el('cbEmpty'),zoom=el('cbZoom');
const byCode={};DATA.forEach(r=>{byCode[r.index_code]=r});
function render(){const q=el('cbSearch').value.trim().toLowerCase(),cat=el('cbCat').value,lv=el('cbLevel').value,sort=el('cbSort').value,ov=el('cbOverseas').checked;
let rows=DATA.filter(r=>(ov||r.category!=='海外')&&(!q||(r.index_name+' '+r.index_code).toLowerCase().includes(q))&&(!cat||r.category===cat)&&(!lv||r.crowding_level===lv));
const key=r=>num(r.crowding_percentile);
rows.sort((a,b)=>sort==='desc'?(key(b)??-1)-(key(a)??-1):sort==='changeDesc'?(num(b.daily_change_pct)??-999)-(num(a.daily_change_pct)??-999):sort==='probDesc'?(num(b.probability_pct)??-1)-(num(a.probability_pct)??-1):(key(a)??999)-(key(b)??999));
body.innerHTML=rows.map(r=>{const ch=num(r.daily_change_pct),cls=ch===null?'':ch>0?'up':ch<0?'down':'';
const pdir=r.probability_direction,pcls=pdir==='上涨'?'up':pdir==='下跌'?'down':'';
const rank=ov?r.rk_all:r.rk_cn;const score=key(r);
return '<tr><td class="cb-rank">'+(rank??'--')+'</td>'
+'<td class="cb-name"><a target="_blank" rel="noreferrer" href="'+esc(r.detail_url)+'">'+esc(r.index_name)+'</a><div class="cb-code">'+esc(r.index_code)+'</div></td>'
+'<td><span class="cb-tag">'+esc(r.category)+'</span></td>'
+'<td><div class="cb-score-top"><span class="cb-score">'+pct(score)+'</span><span class="cb-level">'+esc(r.crowding_level)+'</span></div>'
+'<div class="cb-bar"><i style="width:'+Math.max(1,score??1)+'%;background:'+cbColor(score)+'"></i></div></td>'
+'<td>'+spark(r)+'</td>'
+'<td><span class="'+pcls+'">'+(pdir?esc(pdir)+' '+pct(r.probability_pct):'--')+'</span></td>'
+'<td>'+(r.probability_horizon_days?r.probability_horizon_days+'日':'--')+'</td>'
+'<td class="'+cls+'">'+(ch===null?'--':(ch>0?'+':'')+pct(ch))+'</td></tr>'}).join('');
empty.hidden=rows.length>0}
function place(x,y){const m=12,w=zoom.offsetWidth,h=zoom.offsetHeight;let left=x+18,top=y-h/2;if(left+w+m>innerWidth)left=x-w-18;zoom.style.left=Math.max(m,left)+'px';zoom.style.top=Math.max(m,Math.min(innerHeight-h-m,top))+'px'}
function show(s,x,y){const r=byCode[s.dataset.code];if(!r)return;
zoom.innerHTML='<div class="cb-zoom-title">'+esc(r.index_name)+'<span>'+esc(r.hs)+' → '+esc(r.he)+'</span></div>'+svgFor(r,420,136)+meta(r)+'<div class="cb-zoom-note"><i class="cb-dot cb-dot-price"></i>蓝线：价格，窗口起点=100、独立纵轴　<i class="cb-dot cb-dot-crowd"></i>橙线：拥挤度0–100、独立纵轴</div>';
zoom.hidden=false;place(x,y)}
body.addEventListener('pointerover',e=>{const s=e.target.closest('.cb-spark');if(s&&!s.contains(e.relatedTarget))show(s,e.clientX,e.clientY)});
body.addEventListener('pointermove',e=>{if(!zoom.hidden)place(e.clientX,e.clientY)});
body.addEventListener('pointerout',e=>{const s=e.target.closest('.cb-spark');if(s&&!s.contains(e.relatedTarget))zoom.hidden=true});
body.addEventListener('focusin',e=>{const s=e.target.closest('.cb-spark');if(s)show(s,innerWidth/2,innerHeight/2)});
body.addEventListener('focusout',e=>{if(e.target.closest('.cb-spark'))zoom.hidden=true});
['cbSearch','cbCat','cbLevel','cbSort','cbOverseas'].forEach(id=>el(id).addEventListener(id==='cbSearch'?'input':'change',render));
render();})();
addEventListener('resize',()=>redraw());
"""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>市场情绪追踪看板</title><style>{css}</style></head><body><header class="top"><h1>市场情绪追踪看板</h1><a class="top-link" href="https://eve.lumos.space/kweb.html">Richard 相对看板 →</a><p>每行是一个指标，依次标出1个月前、1周前和当前的位置。同行的点距表示变化幅度；同一市场的行越分散，内部差异越大。</p></header><nav class="tabs">{buttons}</nav>{bodies}<footer>生成时间 {html.escape(generated_at)}｜单文件离线 HTML｜RSI 和拥挤度只描述状态，不代表上涨概率</footer><script>{js}</script></body></html>'''


def refit_trend(tickers: list[str], write: bool) -> dict[str, Any]:
    """重新拟合趋势线并写回 config 的 fit 块。只有人手跑这条命令线才会动，build 永远不重拟合。

    不带 --write 时只打印新旧参数对比，便于先看一眼线挪了多少再决定要不要落盘。
    """
    config = load_json(CONFIG_PATH)
    items = [x for x in config.get("trend_lines", []) if not tickers or x["ticker"] in tickers]
    if not items:
        raise RuntimeError(f"config.trend_lines 里没有匹配的标的：{'、'.join(tickers) or '(全部)'}")
    ohlc = download_ohlc([x["ticker"] for x in items], "2014-01-01", None)
    changes: list[dict[str, Any]] = []
    for item in items:
        frame = ohlc.get(item["ticker"], pd.DataFrame())
        fit = fit_trend_line(frame, item["start"], item["mode"]) if not frame.empty else None
        if not fit:
            changes.append({"ticker": item["ticker"], "ok": False, "error": "复权OHLC取不到或样本不足250根"})
            continue
        changes.append({"ticker": item["ticker"], "ok": True, "old": item.get("fit"), "new": fit})
        if write:
            item["fit"] = fit
    if write and any(x["ok"] for x in changes):
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": all(x["ok"] for x in changes), "written": bool(write), "config": str(CONFIG_PATH), "changes": changes}


def run_digest(signals_path: Path, out: Path) -> dict[str, Any]:
    """把 build 落下的 signals.json 渲染成飞书速览卡片。"""
    if not signals_path.exists():
        return {"ok": False, "error": f"{signals_path} 不存在", "hint": "先跑 scripts/run.sh build --json 生成 signals.json"}
    signals = json.loads(signals_path.read_text(encoding="utf-8"))
    stale = stale_days(signals.get("date", ""))
    if stale is not None and stale > DIGEST_STALE_DAYS:
        return {"ok": False, "error": f"signals.json 停在 {signals.get('date')}，落后 {stale} 天",
                "hint": "先跑 scripts/run.sh build --no-cache --json 刷新，再发卡片"}
    markdown = "\n".join(digest_lines(signals)).rstrip() + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    # 卡片 2.0 和 digest.md 出自同一份 signals：digest.md 留作纯文本版和排障对照，飞书发 card.json。
    card = digest_card(signals)
    card_path = out.with_suffix(".card.json")
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True, "date": signals.get("date", ""), "stale_days": stale, "markdown_path": str(out),
        "card_path": str(card_path), "card_template": card["header"]["template"],
        "title": f"📈 情绪看板速览 — {signals.get('date', '')}",
        "chars": len(markdown), "warnings": signals.get("warnings", []),
        "hits": {
            "美股顶底": signals["us_market"]["topbottom"]["hits"], "Momentum": signals["us_market"]["momentum"]["hits"],
            "纳指/等权": signals["us_market"].get("ndx_spw", {}).get("hits", []),
            "行业": signals["us_market"]["sectors"]["hits"], "GS Prime": signals["us_market"].get("gs_prime", {}).get("hits", []),
            "六大科技": signals["us_stocks"]["megacap"]["hits"],
            "TSM": [signals["us_stocks"]["TSM"]["hit"]] if signals["us_stocks"]["TSM"].get("hit") else [],
            "AFRM": [signals["us_stocks"]["AFRM"]["hit"]] if signals["us_stocks"]["AFRM"].get("hit") else [],
            "A股": signals["cn_a"]["hits"], "港股": signals["hk"]["hits"], "日韩台": signals["asia"]["hits"],
        },
    }


def run_bottom_status(signals_path: Path) -> dict[str, Any]:
    """读取 build 同批次的美股底部状态，不另取行情。"""
    if not signals_path.exists():
        return {"ok": False, "error": f"{signals_path} 不存在", "hint": "先跑 scripts/run.sh build --json 生成 signals.json"}
    signals = json.loads(signals_path.read_text(encoding="utf-8"))
    stale = stale_days(signals.get("date", ""))
    if stale is not None and stale > DIGEST_STALE_DAYS:
        return {"ok": False, "error": f"signals.json 停在 {signals.get('date')}，落后 {stale} 天",
                "hint": "先跑 scripts/run.sh build --no-cache --json 刷新"}
    framework = signals.get("us_market", {}).get("topbottom", {}).get("bottom_framework")
    if not framework:
        return {"ok": False, "error": "signals.json 没有美股底部框架字段", "hint": "先用含底部状态机的版本重新 build"}
    return {
        "ok": True,
        "date": signals.get("date", ""),
        "stale_days": stale,
        **framework,
    }


def run_top_status(signals_path: Path) -> dict[str, Any]:
    """读取 build 同批次的美股顶部状态，不另取行情。"""
    if not signals_path.exists():
        return {"ok": False, "error": f"{signals_path} 不存在", "hint": "先跑 scripts/run.sh build --json 生成 signals.json"}
    signals = json.loads(signals_path.read_text(encoding="utf-8"))
    stale = stale_days(signals.get("date", ""))
    if stale is not None and stale > DIGEST_STALE_DAYS:
        return {"ok": False, "error": f"signals.json 停在 {signals.get('date')}，落后 {stale} 天",
                "hint": "先跑 scripts/run.sh build --no-cache --json 刷新"}
    topbottom = signals.get("us_market", {}).get("topbottom", {})
    framework = topbottom.get("top_framework")
    if not framework:
        return {"ok": False, "error": "signals.json 没有美股顶部框架字段", "hint": "先用含顶部状态机的版本重新 build"}
    return {
        "ok": True,
        "date": signals.get("date", ""),
        "stale_days": stale,
        "vol_ratio": topbottom.get("vol_ratio"),
        "vol_crush": (topbottom.get("lines") or {}).get("vol_crush"),
        **framework,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the market sentiment dashboard.")
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser("build", help="Refresh data and write the HTML dashboard")
    build.add_argument("--output", type=Path, default=WORKSPACE / "research-outputs/sentiment-dashboard/index.html")
    build.add_argument("--no-cache", action="store_true", help="Ignore the yfinance price cache")
    build.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    test = sub.add_parser("self-test", help="Run dependency-free source and formula checks")
    test.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    dig = sub.add_parser("digest", help="Render the daily Feishu digest card from signals.json")
    dig.add_argument("--signals", type=Path, default=WORKSPACE / "research-outputs/sentiment-dashboard" / SIGNALS_NAME)
    dig.add_argument("--out", type=Path, default=WORKSPACE / "temp/sentiment-dashboard/digest.md")
    dig.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    bottom = sub.add_parser("bottom-status", help="Read the US bottom-framework state from signals.json")
    bottom.add_argument("--signals", type=Path, default=WORKSPACE / "research-outputs/sentiment-dashboard" / SIGNALS_NAME)
    bottom.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    top = sub.add_parser("top-status", help="Read the US top-framework state from signals.json")
    top.add_argument("--signals", type=Path, default=WORKSPACE / "research-outputs/sentiment-dashboard" / SIGNALS_NAME)
    top.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    refit = sub.add_parser("refit-trend", help="Re-fit the frozen AMZN/AFRM trend lines and update config")
    refit.add_argument("--ticker", action="append", default=[], help="Limit to these tickers; default all")
    refit.add_argument("--write", action="store_true", help="Write the new fit back into dashboard_config.json")
    refit.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()
    command = args.command or "build"
    try:
        if command == "self-test":
            from sentiment_dashboard_selftest import self_test  # 拆出去是为了让 CLI 文件留在 Workshop 的 256KB 上限之内
            return self_test(args.json)
        if command == "digest":
            result = run_digest(args.signals, args.out)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif result["ok"]:
                print(args.out.read_text(encoding="utf-8"))
            else:
                print(f"ERROR: {result['error']}\nHINT: {result['hint']}", file=sys.stderr)
            return 0 if result["ok"] else 2
        if command == "bottom-status":
            result = run_bottom_status(args.signals)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif result["ok"]:
                metrics = result.get("metrics", {})
                print(result["summary"])
                print(
                    f"截至 {metrics.get('as_of')}：周期股/防御股相对指数 RSI14 {fmt(metrics.get('gsp_rsi14'),1)}；"
                    f"QQQ 63日回撤 {fmt(metrics.get('qqq_drawdown_63_pct'),1,'%')}；"
                    f"SPX/QQQ RSI14 {fmt(metrics.get('spx_rsi14'),1)}/{fmt(metrics.get('qqq_rsi14'),1)}；"
                    f"VIX {fmt(metrics.get('vix'),1)}"
                )
                print(result["macro_policy_rule"])
            else:
                print(f"ERROR: {result['error']}\nHINT: {result['hint']}", file=sys.stderr)
            return 0 if result["ok"] else 2
        if command == "top-status":
            result = run_top_status(args.signals)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif result["ok"]:
                metrics = result.get("metrics", {})
                environment = result.get("environment", {})
                print(result["summary"])
                print(environment.get("summary", ""))
                print(
                    f"截至 {metrics.get('as_of')}：周期股/防御股相对指数 RSI30 {fmt(metrics.get('gsp_rsi30'),1)}；"
                    f"量子等权较60日低点 {fmt(metrics.get('quantum_from_low_pct'),0,'%')}；"
                    f"QQQ RSI14 {fmt(metrics.get('qqq_rsi14'),1)}，距200DMA {fmt(metrics.get('qqq_vs_200dma_pct'),1,'%')}；"
                    f"Momentum RSI {fmt(metrics.get('momentum_rsi'),1)}；"
                    f"QQQ 隐波 30D/60D {fmt(result.get('vol_ratio'),2)}（环境线 ≤{fmt(result.get('vol_crush'),2)}）"
                )
                print((result.get("put_hedge") or {}).get("summary", "指数 put 保护提醒：读数缺失"))
                print(result["regime_rule"])
            else:
                print(f"ERROR: {result['error']}\nHINT: {result['hint']}", file=sys.stderr)
            return 0 if result["ok"] else 2
        if command == "refit-trend":
            result = refit_trend(args.ticker, args.write)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                for item in result["changes"]:
                    print(f"{item['ticker']}: " + (json.dumps(item["new"], ensure_ascii=False) if item["ok"] else f"FAIL {item['error']}"))
                    if item.get("ok") and item.get("old"):
                        print(f"  旧: {json.dumps(item['old'], ensure_ascii=False)}")
                print(("已写入 " + result["config"]) if result["written"] else "未写入 config（加 --write 才落盘）")
            return 0 if result["ok"] else 2
        result = build_dashboard(args.output, use_cache=not args.no_cache)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Dashboard written: {result['output']} ({result['bytes']:,} bytes)")
            if result["warnings"]:
                print("Warnings: " + "; ".join(result["warnings"]))
            if result.get("notices"):
                print("Notices: " + "; ".join(result["notices"]))
        return 0
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "hint": "Run scripts/run.sh self-test --json; then verify BBG CSV, local monitor outputs, and network access."}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}\nHINT: {payload['hint']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
