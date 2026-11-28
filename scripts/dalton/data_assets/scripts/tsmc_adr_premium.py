#!/usr/bin/env python3
"""
TSMC ADR vs Taiwan Stock Premium/Discount Monitor
Compares TSM (NYSE ADR) vs 2330.TW using daily FX (USD/TWD).
1 ADR = 5 ordinary shares.

Usage:
  python3 scripts/tsmc_adr_premium.py              # chart + summary
  python3 scripts/tsmc_adr_premium.py --json        # JSON output
  python3 scripts/tsmc_adr_premium.py --feishu CHAT_ID  # send to feishu
"""

import warnings as _w; _w.filterwarnings('ignore')  # suppress NotOpenSSLWarning etc. before urllib3 import
import sys
import os
import json
import argparse
from datetime import datetime, timedelta

def main():
    parser = argparse.ArgumentParser(description='TSMC ADR Premium Monitor')
    parser.add_argument('--json', action='store_true', help='Output JSON')
    parser.add_argument('--feishu', type=str, help='Send to Feishu chat_id')
    parser.add_argument('--output', type=str, default=None, help='Output chart path')
    parser.add_argument('--years', type=int, default=5, help='Lookback years (default 5)')
    args = parser.parse_args()

    if args.feishu is not None and not args.feishu.strip():
        print(
            "ERROR: --feishu 目标为空，未配置飞书群 chat_id（请传入有效 chat_id 或配置 DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID）",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import yfinance as yf
        import pandas as pd
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError as e:
        print(json.dumps({"ok": False, "error": f"Missing dependency: {e}"}))
        sys.exit(1)

    plt.rcParams["font.sans-serif"] = ["Hiragino Sans GB", "PingFang SC", "STHeiti", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    workspace = (
        os.environ.get("DALTON_DATA_TASK_WORKSPACE")
        or os.environ.get("DALTON_WORKSPACE")
        or os.environ.get("OPENCLAW_WORKSPACE")
        or os.environ.get("WORKDIR")
        or os.getcwd()
    ).strip()
    output_path = args.output or os.path.join(workspace, 'temp', 'tsmc_adr_premium.png')
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    ADR_RATIO = 5  # 1 ADR = 5 ordinary shares

    ##############################################
    # 1. Fetch data
    ##############################################
    start_date = (datetime.now() - timedelta(days=args.years * 365 + 30)).strftime('%Y-%m-%d')
    end_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        tsm_hist = yf.Ticker('TSM').history(start=start_date, end=end_date)['Close']
        tw_hist = yf.Ticker('2330.TW').history(start=start_date, end=end_date)['Close']
        fx_hist = yf.Ticker('TWD=X').history(start=start_date, end=end_date)['Close']  # USD/TWD
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Data fetch failed: {e}"}))
        sys.exit(1)

    # Strip timezone
    for s in [tsm_hist, tw_hist, fx_hist]:
        s.index = s.index.tz_localize(None)

    ##############################################
    # 2. Align and calculate premium
    ##############################################
    all_dates = pd.date_range(start=start_date, end=datetime.now().strftime('%Y-%m-%d'), freq='B')

    tsm_daily = tsm_hist.reindex(all_dates).ffill()
    tw_daily = tw_hist.reindex(all_dates).ffill()
    fx_daily = fx_hist.reindex(all_dates).ffill()

    # ADR price per ordinary share in TWD
    adr_twd_per_share = tsm_daily / ADR_RATIO * fx_daily

    # Premium: (ADR_TWD - TW_TWD) / TW_TWD * 100
    premium = (adr_twd_per_share - tw_daily) / tw_daily * 100

    # Clean
    mask = premium.notna() & tsm_daily.notna() & tw_daily.notna()
    premium = premium[mask]
    tsm_daily = tsm_daily[mask]
    tw_daily = tw_daily[mask]
    fx_daily_clean = fx_daily[mask]
    adr_twd = adr_twd_per_share[mask]

    if len(premium) == 0:
        print(json.dumps({"ok": False, "error": "No data after alignment"}))
        sys.exit(1)

    # Current values
    curr_premium = premium.iloc[-1]
    curr_adr_usd = tsm_daily.iloc[-1]
    curr_tw_twd = tw_daily.iloc[-1]
    curr_fx = fx_daily_clean.iloc[-1]
    curr_adr_twd = adr_twd.iloc[-1]

    # Stats
    avg_premium = premium.mean()
    std_premium = premium.std()
    max_premium = premium.max()
    min_premium = premium.min()
    median_premium = premium.median()

    # Percentile of current
    pct_rank = (premium < curr_premium).sum() / len(premium) * 100

    # Yesterday comparison
    if len(premium) >= 2:
        prev_premium = premium.iloc[-2]
        prem_change = curr_premium - prev_premium
    else:
        prem_change = 0

    # 30-day moving average
    ma30 = premium.rolling(30).mean()

    # Z-score
    zscore = (curr_premium - avg_premium) / std_premium if std_premium > 0 else 0

    ##############################################
    # 3. Chart
    ##############################################
    fig, axes = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={'height_ratios': [1.5, 1]})
    today_str = datetime.now().strftime('%Y-%m-%d')
    fig.suptitle(f'台积电 ADR（TSM）相对台股（2330.TW）溢价｜{today_str}\n'
                 f'1 份 ADR = {ADR_RATIO} 股普通股｜USD/TWD：{curr_fx:.2f}',
                 fontsize=12, fontweight='bold', y=0.98)

    # Panel 1: Premium time series
    ax1 = axes[0]

    # Color zones
    ax1.axhspan(-5, 5, alpha=0.05, color='gray')
    ax1.axhline(y=0, color='black', linewidth=0.8)
    ax1.axhline(y=avg_premium, color='orange', linestyle='--', alpha=0.6, linewidth=1)
    ax1.text(premium.index[5], avg_premium + 0.5, f'{args.years} 年均值：{avg_premium:.1f}%',
             fontsize=8, color='orange')

    # +/- 1 std band
    ax1.axhline(y=avg_premium + std_premium, color='orange', linestyle=':', alpha=0.3)
    ax1.axhline(y=avg_premium - std_premium, color='orange', linestyle=':', alpha=0.3)
    ax1.fill_between(premium.index,
                      avg_premium - std_premium, avg_premium + std_premium,
                      alpha=0.05, color='orange')

    # Premium line
    ax1.fill_between(premium.index, 0, premium.values,
                      where=(premium.values >= 0), alpha=0.15, color='#E53935')
    ax1.fill_between(premium.index, 0, premium.values,
                      where=(premium.values < 0), alpha=0.15, color='#4CAF50')
    ax1.plot(premium.index, premium.values, color='#1565C0', linewidth=0.8, alpha=0.7)
    ax1.plot(ma30.index, ma30.values, color='#FF5722', linewidth=1.5, label='30 日均线', alpha=0.8)

    # Current annotation
    prem_arrow = "↑" if prem_change > 0 else "↓" if prem_change < 0 else "→"
    ax1.annotate(f'当前：{curr_premium:.1f}%（{prem_arrow}{abs(prem_change):.1f} 个百分点）\n'
                 f'Z={zscore:.1f}｜百分位={pct_rank:.0f}%',
                 xy=(premium.index[-1], curr_premium),
                 xytext=(-180, 30 if curr_premium < avg_premium else -40),
                 textcoords='offset points',
                 fontsize=10, fontweight='bold', color='#1565C0',
                 arrowprops=dict(arrowstyle='->', color='#1565C0'),
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='#1565C0', alpha=0.8))

    ax1.set_ylabel('ADR 溢价（%）', fontsize=10)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(min(min_premium - 3, -5), max(max_premium + 3, 25))

    # Panel 2: Premium histogram
    ax2 = axes[1]
    n_bins = 80
    counts, bin_edges, patches = ax2.hist(premium.values, bins=n_bins, alpha=0.6,
                                           color='#1565C0', edgecolor='white', linewidth=0.3)

    # Color current bin
    for patch, left_edge, right_edge in zip(patches, bin_edges[:-1], bin_edges[1:]):
        if left_edge <= curr_premium <= right_edge:
            patch.set_facecolor('#E53935')
            patch.set_alpha(0.9)

    ax2.axvline(x=curr_premium, color='#E53935', linewidth=2, linestyle='-',
                label=f'当前：{curr_premium:.1f}%')
    ax2.axvline(x=avg_premium, color='orange', linewidth=1.5, linestyle='--',
                label=f'均值：{avg_premium:.1f}%')
    ax2.axvline(x=median_premium, color='green', linewidth=1, linestyle=':',
                label=f'中位数：{median_premium:.1f}%')

    ax2.set_xlabel('ADR 溢价（%）', fontsize=10)
    ax2.set_ylabel('交易日数', fontsize=10)
    ax2.legend(fontsize=8, loc='upper right')
    ax2.grid(True, alpha=0.3)

    for ax in axes:
        if ax == axes[0]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
            ax.set_xlim(premium.index[0], premium.index[-1])
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    fig.text(0.5, 0.005,
             f'溢价 =（ADR÷5×USD/TWD − 2330.TW）/ 2330.TW｜'
             f'ADR：USD {curr_adr_usd:.2f} → TWD {curr_adr_twd:.0f}/股｜台股：TWD {curr_tw_twd:.0f}｜'
             f'历史区间：{min_premium:.1f}%—{max_premium:.1f}%｜σ：{std_premium:.1f}%',
             ha='center', fontsize=7, color='gray')

    plt.tight_layout(rect=[0, 0.03, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()

    ##############################################
    # 4. Text summary
    ##############################################
    disc_arrow = "↑" if prem_change > 0 else "↓" if prem_change < 0 else "→"
    summary_lines = [
        f"📊 台积电 ADR 溢价｜{today_str}",
        f"",
        f"ADR（TSM）：USD {curr_adr_usd:.2f} → TWD {curr_adr_twd:.0f}/股",
        f"台股（2330.TW）：TWD {curr_tw_twd:.0f}",
        f"USD/TWD：{curr_fx:.2f}",
        f"",
        f"溢价：{curr_premium:.1f}%（较昨日 {disc_arrow}{abs(prem_change):.1f} 个百分点）",
        f"{args.years} 年均值：{avg_premium:.1f}%｜中位数：{median_premium:.1f}%",
        f"Z-score：{zscore:.1f}｜百分位：{pct_rank:.0f}%",
        f"历史区间：{min_premium:.1f}%—{max_premium:.1f}%",
    ]

    # Signal
    if zscore > 2:
        summary_lines.append(f"⚠️ 溢价显著偏高 (>2σ)，ADR 相对台股偏贵")
    elif zscore > 1:
        summary_lines.append(f"📈 溢价偏高 (>1σ)")
    elif zscore < -2:
        summary_lines.append(f"⚠️ 溢价显著偏低 (<-2σ)，ADR 相对台股便宜")
    elif zscore < -1:
        summary_lines.append(f"📉 溢价偏低 (<-1σ)")
    else:
        summary_lines.append(f"→ 溢价在正常范围内 (±1σ)")

    summary_text = "\n".join(summary_lines)

    ##############################################
    # 5. Output
    ##############################################
    if args.json:
        result = {
            "ok": True,
            "date": today_str,
            "adr_price_usd": round(curr_adr_usd, 2),
            "tw_price_twd": round(curr_tw_twd, 0),
            "usd_twd": round(curr_fx, 2),
            "adr_twd_per_share": round(curr_adr_twd, 0),
            "premium_pct": round(curr_premium, 2),
            "premium_change_pp": round(prem_change, 2),
            "avg_premium_pct": round(avg_premium, 2),
            "median_premium_pct": round(median_premium, 2),
            "std_premium_pct": round(std_premium, 2),
            "zscore": round(zscore, 2),
            "percentile": round(pct_rank, 1),
            "min_premium_pct": round(min_premium, 2),
            "max_premium_pct": round(max_premium, 2),
            "chart_path": output_path,
            "summary": summary_text,
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(summary_text)
        print(f"\nChart saved: {output_path}")

    ##############################################
    # 6. Feishu delivery (Card Schema 2.0)
    ##############################################
    if args.feishu is not None:
        feishu_chat_id = args.feishu.strip()
        if not feishu_chat_id:
            print(
                "ERROR: --feishu 目标为空，未配置飞书群 chat_id（请传入有效 chat_id 或配置 DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID）",
                file=sys.stderr,
            )
            sys.exit(1)
        card_script = os.path.join(workspace, 'scripts', 'feishu_send_card.py')
        card_file = os.path.join(workspace, 'temp', 'tsmc_adr_premium_card.md')
        os.makedirs(os.path.dirname(os.path.abspath(card_file)), exist_ok=True)
        prem_arrow = "↑" if prem_change > 0 else "↓" if prem_change < 0 else "→"
        status_eval = (
            "📈 溢价偏高 (高于均值 +1σ，溢价回落风险增加)" if zscore > 1
            else ("📉 溢价偏低 (低于均值 -1σ，折价修复机会)" if zscore < -1
                  else "→ 溢价处于正常历史波动区间 (±1σ 内)")
        )
        card_markdown = (
            f"## 核心价差与溢价指标\n\n"
            f"· **ADR 收盘价**: ${curr_adr_usd:.2f} (折合 NT${curr_adr_twd:,.0f})\n"
            f"· **台股收盘价**: NT${curr_tw_twd:,.0f} ｜ **USD/TWD 汇率**: {curr_fx:.2f}\n"
            f"· **当前溢价率**: **{curr_premium:+.2f}%**（较昨日 {prem_arrow}{abs(prem_change):.2f} 个百分点）\n"
            f"· **历史统计 (1年)**: 均值 {avg_premium:.2f}% ｜ 中位数 {median_premium:.2f}% ｜ Z-Score **{zscore:+.2f}** ({pct_rank:.1f}%分位)\n\n"
            f"## 溢价走势图\n\n"
            f"IMG:{output_path}\n\n"
            f"## 统计状态判定\n\n"
            f"{status_eval}"
        )
        with open(card_file, 'w', encoding='utf-8') as f:
            f.write(card_markdown)

        ret = os.system(
            f'python3 "{card_script}" --chat-id {feishu_chat_id} '
            f'--title "📊 台积电 ADR 溢价监控" '
            f'--subtitle "截至 {today_str} · 日度追踪" '
            f'--header-template indigo '
            f'--file "{card_file}"'
        )
        if ret != 0:
            print(f"Warning: feishu card send returned {ret}", file=sys.stderr)
            sys.exit(1)

if __name__ == '__main__':
    main()
