#!/usr/bin/env python3
"""量子计算 EW 指数日报 v4 (reconstructed 2026-07-07, original file was lost from temp/).
IONQ/RGTI/QBTS/QUBT 等权(25%每只,每日再平衡)组合, 用于监测 QQQ 阶段性顶部信号。
"""
import warnings as _w; _w.filterwarnings('ignore')  # suppress NotOpenSSLWarning etc. before urllib3 import
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd

TICKERS = ["IONQ", "RGTI", "QBTS", "QUBT"]

# 静态历史回测表 (阶段性顶部定义: QQQ在该高点后>=3个月未突破), 最近确认于 2026-06-29
BACKTEST = [
    ("2023.7.13", "+221%", "3只", "2023.7.18", "5天", "4.1月"),
    ("2025.1.3", "+1142%", "4只", "2025.2.19", "47天", "4.1月"),
    ("2025.6.20", "+118%", "2只", "未命中", "—", "—"),
    ("2025.10.14", "+124%", "2只", "2025.10.29", "15天", "5.5月"),
]


def fetch_prices():
    end = datetime.now()
    start = end - timedelta(days=220)  # comfortably covers 6-month window
    data = yf.download(TICKERS, start=start.strftime("%Y-%m-%d"),
                        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=True)["Close"]
    data = data.dropna(how="all").ffill().dropna()
    return data


def main():
    today = datetime.now()
    prices = fetch_prices()

    six_month_cutoff = today - timedelta(days=182)
    sixty_day_cutoff = today - timedelta(days=60)

    window_6m = prices[prices.index >= six_month_cutoff]
    window_60d = prices[prices.index >= sixty_day_cutoff]

    # EW index rebased to 100 at the start of the 6-month window
    base = window_6m.iloc[0]
    ew_series = (window_6m / base).mean(axis=1) * 100

    ew_now = ew_series.iloc[-1]
    ew_6m_low = ew_series.min()
    ew_6m_low_date = ew_series.idxmin()

    ew_60d_series = ew_series[ew_series.index >= sixty_day_cutoff]
    ew_60d_low = ew_60d_series.min()
    ew_60d_low_date = ew_60d_series.idxmin()

    gain_6m = (ew_now / ew_6m_low - 1) * 100
    gain_60d = (ew_now / ew_60d_low - 1) * 100

    # status level driven by the 60-day gain (main signal per v4 spec)
    if gain_60d >= 100:
        status = "近 60 天涨幅 ≥100%"
    elif gain_60d >= 80:
        status = "近 60 天涨幅 80%—100%"
    elif gain_60d >= 50:
        status = "近 60 天涨幅 50%—80%"
    elif gain_60d >= 30:
        status = "近 60 天涨幅 30%—50%"
    else:
        status = "近 60 天涨幅低于 30%"

    stock_lines = []
    doubled = 0
    for t in TICKERS:
        s60 = window_60d[t]
        low = s60.min()
        cur = s60.iloc[-1]
        pct = (cur / low - 1) * 100
        tag = ""
        if pct >= 100:
            doubled += 1
            tag = "（较 60 天低点翻倍）"
        stock_lines.append(f"  {t} ${cur:,.2f}  近60天 +{pct:.0f}%{tag}")

    lines = []
    lines.append(f"📈 量子股等权组合｜{today.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"当前区间：{status}；较近 60 天低点上涨 {gain_60d:.0f}%")
    lines.append("")
    lines.append(f"等权指数：{ew_now:.0f}")
    lines.append(f"较近 6 个月低点上涨：{gain_6m:.0f}%（低点 {ew_6m_low:.0f}，{ew_6m_low_date.strftime('%m-%d')}）")
    lines.append(f"较近 60 天低点上涨：{gain_60d:.0f}%（低点 {ew_60d_low:.0f}，{ew_60d_low_date.strftime('%m-%d')}）")
    lines.append("")
    lines.extend(stock_lines)
    lines.append(f"较 60 天低点翻倍：{doubled}/4")

    if doubled >= 2:
        lines.append("")
        lines.append("观察：至少 2 只股票较近 60 天低点翻倍。历史样本只有 4 次，其中 3 次 QQQ 在随后 3 个月内出现阶段性高点；样本很小，不能单独作为顶部判断。")

    lines.append("")
    lines.append("历史样本（4 次触发，其中 3 次 QQQ 在 3 个月内出现阶段性高点）")
    lines.append(f"{'信号日期':<12}{'EW涨幅':<10}{'翻倍':<8}{'QQQ顶部':<14}{'间隔':<8}{'未破时长'}")
    for row in BACKTEST:
        lines.append(f"{row[0]:<12}{row[1]:<10}{row[2]:<8}{row[3]:<14}{row[4]:<8}{row[5]}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
