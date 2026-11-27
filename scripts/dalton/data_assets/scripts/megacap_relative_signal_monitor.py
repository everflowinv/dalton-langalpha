#!/usr/bin/env python3
"""Daily relative-RSI/200DMA monitor for six US mega-cap stocks.

The signal rules are the frozen two-stage confirmation framework from
research-outputs/megacap_relative_signals_2023_2026.md:

* Top combination: RSI(30) of stock/QQQ > 60, price-vs-200DMA is above its
  prior 504-trading-day 80th percentile, and price is at least 20% above 200DMA.
* Bottom combination: RSI(30) of stock/QQQ < 40, price-vs-200DMA is below its
  prior 504-trading-day 20th percentile, and price is at least 5% below 200DMA.

Prices use Yahoo Finance adjusted closes.  The latest common daily bar must be
fresh and complete for every stock plus QQQ; otherwise the run fails instead of
publishing a mixed-date table.

配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
* DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET  飞书接收群 chat_id（兼容 DALTON_MEGACAP_FEISHU_TARGET，必填，未配置时非零退出）
* DALTON_DATA_TASK_WORKSPACE              数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR，默认当前运行目录）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")
import yfinance as yf


def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


WORKSPACE = _resolve_workspace()
DEFAULT_OUTPUT_DIR = WORKSPACE / "temp" / "megacap-relative-signal-monitor"


def _read_data_task_config_file(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def resolve_feishu_target() -> str:
    keys = ("DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET", "DALTON_MEGACAP_FEISHU_TARGET")
    for key in keys:
        env_val = (os.environ.get(key) or "").strip()
        if env_val:
            return env_val
    cfg_override = (os.environ.get("DALTON_DATA_TASKS_CONFIG") or "").strip()
    cfg_path = Path(cfg_override) if cfg_override else (_resolve_workspace() / "config" / "data_tasks.env")
    cfg = _read_data_task_config_file(cfg_path)
    for key in keys:
        val = cfg.get(key, "").strip()
        if val:
            return val
    return ""


FEISHU_TARGET = resolve_feishu_target()
TICKERS = ["AMZN", "GOOGL", "META", "MSFT", "AAPL", "NVDA"]
BENCHMARK = "QQQ"
DOWNLOAD_START = "2022-01-01"
RSI_PERIOD = 30
DMA_PERIOD = 200
LOOKBACK = 504
HOT_RSI = 60.0
COLD_RSI = 40.0
TOP_ABS_FLOOR = 0.20
BOTTOM_ABS_FLOOR = -0.05
MAX_STALE_CALENDAR_DAYS = 5
CJK_FONT_PATH = Path("/System/Library/Fonts/STHeiti Medium.ttc")


def wilder_rsi(series: pd.Series, period: int) -> pd.Series:
    """Exact Wilder RSI using an SMA seed and recursive Wilder smoothing."""
    values = series.astype(float)
    delta = values.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= period:
        return result

    avg_gain = gains.iloc[1 : period + 1].mean()
    avg_loss = losses.iloc[1 : period + 1].mean()

    def calculate(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result.iloc[period] = calculate(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains.iloc[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses.iloc[index]) / period
        result.iloc[index] = calculate(avg_gain, avg_loss)
    return result


def extract_field(frame: pd.DataFrame, field: str, symbols: list[str]) -> pd.DataFrame:
    if not isinstance(frame.columns, pd.MultiIndex):
        raise RuntimeError("Yahoo Finance returned an unexpected non-MultiIndex table")
    if field in frame.columns.get_level_values(0):
        output = frame[field]
    elif field in frame.columns.get_level_values(1):
        output = frame.xs(field, axis=1, level=1)
    else:
        raise RuntimeError(f"Yahoo Finance response is missing {field}")
    return output.reindex(columns=symbols)


def download_adjusted_close() -> tuple[pd.DataFrame, pd.DataFrame]:
    symbols = TICKERS + [BENCHMARK]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = yf.download(
                symbols,
                start=DOWNLOAD_START,
                auto_adjust=False,
                actions=True,
                group_by="column",
                progress=False,
                threads=True,
                timeout=30,
            )
            if raw.empty:
                raise RuntimeError("Yahoo Finance returned no rows")
            close = extract_field(raw, "Close", symbols)
            adjusted = extract_field(raw, "Adj Close", symbols)
            for table in (close, adjusted):
                table.index = pd.to_datetime(table.index).tz_localize(None)
                table.sort_index(inplace=True)
                # This job is designed for 08:30 ET.  Excluding the current ET
                # date also makes manual/intraday runs safe: Yahoo can expose a
                # still-forming daily candle after the opening bell.
                today_et = datetime.now(ZoneInfo("America/New_York")).date()
                table.drop(index=table.index[table.index.date >= today_et], inplace=True)
            return close, adjusted
        except Exception as exc:  # pragma: no cover - exercised on provider failures
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f"Yahoo Finance download failed after retry: {last_error}")


def validate_prices(close: pd.DataFrame, adjusted: pd.DataFrame) -> pd.Timestamp:
    symbols = TICKERS + [BENCHMARK]
    common = adjusted[symbols].dropna(how="any")
    if len(common) < LOOKBACK + DMA_PERIOD:
        raise RuntimeError(
            f"Only {len(common)} complete observations; need at least {LOOKBACK + DMA_PERIOD}"
        )
    latest = pd.Timestamp(common.index.max())

    latest_by_symbol = {symbol: adjusted[symbol].dropna().index.max() for symbol in symbols}
    mismatched = {
        symbol: str(pd.Timestamp(date).date())
        for symbol, date in latest_by_symbol.items()
        if pd.Timestamp(date) != latest
    }
    if mismatched:
        raise RuntimeError(
            f"Latest dates are not aligned with common date {latest.date()}: {mismatched}"
        )

    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    stale_days = (today_et - latest.date()).days
    if stale_days < 0 or stale_days > MAX_STALE_CALENDAR_DAYS:
        raise RuntimeError(
            f"Latest common daily bar is stale or future-dated: {latest.date()} (ET today {today_et})"
        )

    if (common <= 0).any().any():
        raise RuntimeError("Adjusted-close history contains non-positive values")
    latest_close = close.loc[latest, symbols]
    latest_adjusted = adjusted.loc[latest, symbols]
    if latest_close.isna().any() or latest_adjusted.isna().any():
        raise RuntimeError("Latest common bar contains missing raw or adjusted prices")

    adjusted_returns = adjusted[symbols].pct_change(fill_method=None).loc["2022-01-01":]
    maximum_move = float(adjusted_returns.abs().max().max())
    if maximum_move > 0.60:
        raise RuntimeError(
            f"Adjusted price integrity check failed: maximum one-day move is {maximum_move:.1%}"
        )
    return latest


def calculate_rows(adjusted: pd.DataFrame, latest: pd.Timestamp) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for ticker in TICKERS:
        pair = adjusted[[ticker, BENCHMARK]].dropna()
        ratio = pair[ticker] / pair[BENCHMARK]
        relative_rsi = wilder_rsi(ratio, RSI_PERIOD)
        dma200 = pair[ticker].rolling(DMA_PERIOD, min_periods=DMA_PERIOD).mean()
        deviation = pair[ticker] / dma200 - 1.0
        low_line = deviation.rolling(LOOKBACK, min_periods=LOOKBACK).quantile(0.20).shift(1)
        high_line = deviation.rolling(LOOKBACK, min_periods=LOOKBACK).quantile(0.80).shift(1)

        rsi_now = float(relative_rsi.loc[latest])
        deviation_now = float(deviation.loc[latest])
        low_now = float(low_line.loc[latest])
        high_now = float(high_line.loc[latest])
        if not np.isfinite([rsi_now, deviation_now, low_now, high_now]).all():
            raise RuntimeError(f"{ticker} has a non-finite latest indicator")

        is_high = deviation_now > high_now
        is_low = deviation_now < low_now
        top_combo = rsi_now > HOT_RSI and is_high and deviation_now >= TOP_ABS_FLOOR
        bottom_combo = rsi_now < COLD_RSI and is_low and deviation_now <= BOTTOM_ABS_FLOOR

        if top_combo:
            state = "★ 顶部组合｜分批减仓"
            tone = "top"
        elif bottom_combo:
            state = "★ 底部组合｜可先加一部分"
            tone = "bottom"
        elif rsi_now > HOT_RSI and is_high:
            state = "偏热｜高位确认，未达+20%"
            tone = "watch"
        elif rsi_now > HOT_RSI:
            state = "偏热｜绝对位置未确认"
            tone = "watch"
        elif rsi_now < COLD_RSI and is_low:
            state = "偏冷｜低位确认，未达-5%"
            tone = "watch"
        elif rsi_now < COLD_RSI:
            state = "偏冷｜绝对位置未确认"
            tone = "watch"
        elif is_high:
            state = "绝对位置偏高｜RSI未进热区"
            tone = "watch"
        elif is_low:
            state = "绝对位置偏低｜RSI未进冷区"
            tone = "watch"
        else:
            state = "中性"
            tone = "neutral"

        if is_high:
            position = f"高20%（线 {high_now:+.1%}）"
        elif is_low:
            position = f"低20%（线 {low_now:+.1%}）"
        else:
            position = "中间60%"

        rows.append(
            {
                "ticker": ticker,
                "relative_rsi_30": round(rsi_now, 1),
                "price_vs_200dma": round(deviation_now, 6),
                "two_year_low_line": round(low_now, 6),
                "two_year_high_line": round(high_now, 6),
                "two_year_position": position,
                "top_combo": top_combo,
                "bottom_combo": bottom_combo,
                "state": state,
                "tone": tone,
            }
        )
    return rows


def font_properties(size: float | None = None, weight: str = "normal") -> FontProperties:
    if CJK_FONT_PATH.exists():
        return FontProperties(fname=str(CJK_FONT_PATH), size=size, weight=weight)
    return FontProperties(size=size, weight=weight)


def render_table(rows: list[dict[str, object]], as_of: pd.Timestamp, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    chart = output_dir / f"megacap_relative_signal_{as_of:%Y%m%d}.png"
    columns = ["股票", "相对 RSI(30)", "距 200DMA", "两年区间位置", "当前状态"]
    cell_text = []
    for row in rows:
        ticker = f"★ {row['ticker']}" if row["top_combo"] or row["bottom_combo"] else row["ticker"]
        cell_text.append(
            [
                ticker,
                f"{row['relative_rsi_30']:.1f}",
                f"{row['price_vs_200dma']:+.1%}",
                row["two_year_position"],
                row["state"],
            ]
        )

    fig, axis = plt.subplots(figsize=(15.6, 6.3), dpi=170)
    fig.patch.set_facecolor("#F7F9FC")
    axis.axis("off")
    axis.set_title(
        f"六大科技股相对强弱信号｜{as_of.date()} 收盘",
        loc="left",
        pad=20,
        fontproperties=font_properties(19, "bold"),
        color="#17324D",
    )
    table = axis.table(
        cellText=cell_text,
        colLabels=columns,
        cellLoc="center",
        colLoc="center",
        colWidths=[0.11, 0.15, 0.14, 0.22, 0.38],
        bbox=[0.0, 0.18, 1.0, 0.72],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1.0, 1.65)

    tone_colors = {
        "top": "#FDE8E7",
        "bottom": "#E5F5EA",
        "watch": "#FFF4D6",
        "neutral": "#FFFFFF",
    }
    for (row_index, column_index), cell in table.get_celld().items():
        cell.set_edgecolor("#D8E0E8")
        cell.set_linewidth(0.8)
        cell.get_text().set_fontproperties(font_properties(10.8, "normal"))
        if row_index == 0:
            cell.set_facecolor("#274C6B")
            cell.get_text().set_color("white")
            cell.get_text().set_fontproperties(font_properties(11.2, "bold"))
        else:
            cell.set_facecolor(tone_colors[str(rows[row_index - 1]["tone"])])
            if column_index == 0:
                cell.get_text().set_fontproperties(font_properties(11.2, "bold"))
            if column_index == 4 and rows[row_index - 1]["tone"] in {"top", "bottom"}:
                cell.get_text().set_fontproperties(font_properties(10.8, "bold"))

    fig.text(
        0.02,
        0.105,
        "★ 顶部：RSI>60 + 200DMA乖离进入过去504交易日最高20% + 乖离≥+20%；"
        "底部：RSI<40 + 进入最低20% + 乖离≤-5%。",
        fontproperties=font_properties(9.5),
        color="#334E68",
    )
    fig.text(
        0.02,
        0.058,
        "META、NVDA 的反转规则低置信；即使出现★，也需结合基本面、期权拥挤度或相对趋势破位确认。",
        fontproperties=font_properties(9.5),
        color="#7A4E00",
    )
    fig.text(
        0.98,
        0.02,
        "Source: Yahoo Finance adjusted close",
        ha="right",
        fontsize=8.2,
        color="#6B7C93",
    )
    fig.savefig(chart, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return chart.resolve()


def build_message(rows: list[dict[str, object]], as_of: pd.Timestamp) -> str:
    tops = [str(row["ticker"]) for row in rows if row["top_combo"]]
    bottoms = [str(row["ticker"]) for row in rows if row["bottom_combo"]]
    if tops or bottoms:
        parts = []
        if tops:
            parts.append("顶部组合：" + "、".join(tops))
        if bottoms:
            parts.append("底部组合：" + "、".join(bottoms))
        summary = "★ " + "；".join(parts)
    else:
        summary = "本期没有股票触发顶部或底部组合。"
    return (
        f"六大科技股相对信号｜{as_of.date()} 收盘\n"
        f"{summary}\n"
        "★ 代表相对RSI、两年极端位置和200DMA绝对门槛同时成立。"
    )


def run(output_dir: Path) -> dict[str, object]:
    feishu_target = resolve_feishu_target()
    if not feishu_target:
        raise RuntimeError(
            "未配置 DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET（兼容 DALTON_MEGACAP_FEISHU_TARGET；请设置环境变量或在 config/data_tasks.env 中配置）"
        )
    close, adjusted = download_adjusted_close()
    latest = validate_prices(close, adjusted)
    rows = calculate_rows(adjusted, latest)
    chart = render_table(rows, latest, output_dir)
    result = {
        "ok": True,
        "date": str(latest.date()),
        "source": "Yahoo Finance adjusted close via yfinance",
        "price_checks": {
            "latest_common_date": str(latest.date()),
            "symbols": TICKERS + [BENCHMARK],
            "series_basis": "Adjusted Close",
        },
        "rules": {
            "top": "relative RSI(30)>60; 200DMA deviation above prior 504-day 80th percentile; deviation>=+20%",
            "bottom": "relative RSI(30)<40; 200DMA deviation below prior 504-day 20th percentile; deviation<=-5%",
        },
        "rows": rows,
        "chart": str(chart),
        "feishu_target": feishu_target,
        "feishu_message": build_message(rows, latest),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def self_test() -> dict[str, object]:
    series = pd.Series([100.0 + index for index in range(40)])
    rsi = wilder_rsi(series, RSI_PERIOD)
    checks = {
        "wilder_rsi_monotonic_gain_is_100": float(rsi.dropna().iloc[-1]) == 100.0,
        "output_directory_under_workspace": str(DEFAULT_OUTPUT_DIR).startswith(str(WORKSPACE)),
        "cjk_font_available": CJK_FONT_PATH.exists(),
    }
    return {"ok": all(checks.values()), "checks": checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--self-test", action="store_true", help="Run dependency-free checks")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = self_test() if args.self_test else run(args.output_dir.resolve())
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "hint": "Retry once; if the latest common bar is stale, check Yahoo daily-price availability before sending.",
        }
        print(json.dumps(result, ensure_ascii=False) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
