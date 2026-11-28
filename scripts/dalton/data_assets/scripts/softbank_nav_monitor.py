#!/usr/bin/env python3
"""
SoftBank Group (9984.T) — Daily NAV Discount Monitor
Calculates live NAV using Arm + SoftBank Corp daily mark-to-market
+ OpenAI & ByteDance estimated stakes + quarterly residual.
Outputs chart to temp/ and optional text summary.

Usage:
  python3 scripts/softbank_nav_monitor.py              # generate chart + print summary
  python3 scripts/softbank_nav_monitor.py --json        # JSON output
  python3 scripts/softbank_nav_monitor.py --feishu CHAT_ID  # send to feishu group
"""

import warnings as _w; _w.filterwarnings('ignore')  # suppress NotOpenSSLWarning etc. before urllib3 import
import sys
import os
import json
import argparse
from datetime import datetime, timedelta

def main():
    parser = argparse.ArgumentParser(description='SoftBank NAV Discount Monitor')
    parser.add_argument('--json', action='store_true', help='Output JSON')
    parser.add_argument('--feishu', type=str, help='Send to Feishu chat_id')
    parser.add_argument('--output', type=str, default=None, help='Output chart path')
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
    output_path = args.output or os.path.join(workspace, 'temp', 'softbank_nav_daily.png')
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    ##############################################
    # 1. Fetch prices
    ##############################################
    lookback_days = 500
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    end_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        arm_hist = yf.Ticker('ARM').history(start=start_date, end=end_date)['Close']
        sbkk_hist = yf.Ticker('9434.T').history(start=start_date, end=end_date)['Close']
        sbg_hist = yf.Ticker('9984.T').history(start=start_date, end=end_date)['Close']
        fx_hist = yf.Ticker('JPY=X').history(start=start_date, end=end_date)['Close']
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Data fetch failed: {e}"}))
        sys.exit(1)

    for s in [arm_hist, sbkk_hist, sbg_hist, fx_hist]:
        s.index = s.index.tz_localize(None)

    ##############################################
    # 2. SoTP model parameters
    ##############################################
    ARM_SHARES_HELD = 922_734_000
    SBKK_OWNERSHIP = 0.4068
    SBG_SHARES = 5_698_763_936
    # Haircuts = adjusted / gross, per official SoTP page (group.softbank/en/ir/stock/sotp)
    # 2026-03-31: Arm 19.15 / 22.32 = 0.858; SBKK 2.85 / 4.04 = 0.706
    ARM_HAIRCUT = 0.858
    SBKK_HAIRCUT = 0.706

    SBKK_TOTAL_SHARES = 47_763_467_941
    SBKK_SHARES_HELD = int(SBKK_TOTAL_SHARES * SBKK_OWNERSHIP)

    # =============================================
    # OpenAI: ~13% committed stake (post-$64.6B total commitment), in SVF2
    # ByteDance: ~3% stake (SVF1), private
    # These are SBG's pro-rata share value (USD B), not total company valuation
    #
    # OpenAI ownership timeline:
    #   - By Dec 2025: ~11% at $300B ($34.6B cumulative deployed)
    #   - Mar 2026: $64.6B total committed ($34.6B deployed), FV=$79.6B
    #     Per official Q4 FY2025 filing (SoTP page). ~13% committed stake.
    #   - $30B follow-on in 3 tranches ($10B each: Apr/Jul/Oct 2026)
    #     Once fully deployed → 13% economic stake
    # Sources: group.softbank/en/ir/stock/sotp (Q4 FY2025, reported 2026-05-13)
    # =============================================
    quarterly_openai_usd_b = {
        '2024-03-31': 2.0,    # Pre major investment, small SVF2 position
        '2024-06-30': 3.5,    # Early SVF2 buildup
        '2024-09-30': 5.0,    # $150B valuation round, ~3% stake
        '2024-12-31': 8.0,    # Increased to ~7-8% after additional investment
        '2025-03-31': 15.0,   # $300B valuation, ~5% growing stake
        '2025-06-30': 33.0,   # Post $34.6B investment, ~11% at $300B
        '2025-09-30': 33.0,   # ~11% at $300B (stable)
        '2025-12-31': 33.0,   # ~11% at $300B (pre new round)
        '2026-03-31': 79.6,   # Official FV per Q4 FY2025 filing ($34.6B deployed, SVF2 portion)
    }

    # Quarterly ByteDance stake value (USD B) — SBG's ~3% share (SVF1 primary holding)
    # SVF1 total at Mar 2026 = ¥3.38T = ~$21.1B; ByteDance is largest holding
    # Secondary market: $480-600B by Mar 2026 (SCMP, TechInAsia)
    quarterly_bytedance_usd_b = {
        '2024-03-31': 6.0,    # ~$200B valuation × 3%
        '2024-06-30': 6.5,
        '2024-09-30': 7.0,
        '2024-12-31': 7.5,
        '2025-03-31': 9.5,    # $315B valuation × 3%
        '2025-06-30': 9.9,    # $330B
        '2025-09-30': 12.0,   # Rising toward $400B
        '2025-12-31': 14.4,   # $480B secondary × 3%
        '2026-03-31': 18.0,   # $600B secondary × 3%; SVF1 total = ¥3.38T (official)
    }

    # Quarterly residual: NAV - Arm - SBKK - OpenAI - ByteDance
    # This captures: SVF1 (ex-ByteDance) + SVF2 (ex-OpenAI) + LatAm + T-Mobile + Others - Net Debt
    # We back-calculate from official SoTP quarterly data
    # Sources: group.softbank/en/ir/stock/sotp (Q4 FY2025 filing, 2026-05-13)
    quarterly_sotp = {
        # date: (total_nav_tn_jpy, arm_official_tn, sbkk_official_tn)
        '2024-03-31': (27.8, 14.5, 3.9),
        '2024-06-30': (35.3, 21.0, 3.8),
        '2024-09-30': (29.0, 14.5, 3.5),
        '2024-12-31': (29.3, 14.8, 3.4),
        '2025-03-31': (25.7, 10.5, 3.9),
        '2025-06-30': (32.4, 17.5, 3.5),
        '2025-09-30': (33.3, 14.4, 3.4),
        '2025-12-31': (30.9, 12.69, 2.92),
        # Q4 FY2025 (official): Arm(adj)=19.15, SBKK(adj)=2.85, Net Debt=8.21
        # Equity: SVF2=17.19 SVF1=3.38 LatAm=1.04 T-Mobile=0.05 Others=4.61
        '2026-03-31': (40.06, 19.15, 2.85),
    }

    # =============================================
    # Cash outflow / debt adjustments (USD B)
    # For quarters WITHOUT official NAV, we must manually adjust the
    # forward-filled residual for known large cash deployments.
    #
    # IMPORTANT: Only include cash flows AFTER the last official NAV date.
    # The Dec 2025 NAV (¥30.9T) already reflects all $34.6B deployed
    # into OpenAI through 2024-2025. Do NOT double-count.
    #
    # Post-Dec-2025 known cash flows:
    #   - 2026-04-01: -$10B (1st tranche of $30B follow-on)
    #   - 2026-07-01: -$10B (2nd tranche, pending)
    #   - 2026-10-01: -$10B (3rd tranche, pending)
    #   - SBG also sold Nvidia ($5.8B) + partial T-Mobile to fund;
    #     these reduce residual assets but add cash → net ~0 on NAV.
    #   - SBG took $40B bridge loan: +debt +cash → net 0 until deployed.
    # =============================================
    quarterly_cash_adjustments_usd_b = {
        # 2026-04-01: First $10B tranche of $30B follow-on deployed
        '2026-04-01': -10.0,
    }

    ##############################################
    # 3. Calculate daily NAV
    ##############################################
    all_dates = pd.date_range(start=start_date, end=datetime.now().strftime('%Y-%m-%d'), freq='B')

    arm_daily = arm_hist.reindex(all_dates).ffill()
    sbkk_daily = sbkk_hist.reindex(all_dates).ffill()
    sbg_daily = sbg_hist.reindex(all_dates).ffill()
    fx_daily = fx_hist.reindex(all_dates).ffill()

    # Arm value (trillion JPY) with haircut
    arm_value_tn = (ARM_SHARES_HELD * arm_daily * fx_daily / 1e12) * ARM_HAIRCUT

    # SBKK value (trillion JPY) with haircut
    sbkk_value_tn = (SBKK_SHARES_HELD * sbkk_daily / 1e12) * SBKK_HAIRCUT

    # OpenAI value (trillion JPY) — convert USD B to JPY T using daily FX
    q_dates_oai = pd.to_datetime(list(quarterly_openai_usd_b.keys()))
    q_vals_oai = list(quarterly_openai_usd_b.values())
    openai_usd_b_daily = pd.Series(q_vals_oai, index=q_dates_oai).reindex(all_dates).ffill().bfill()
    openai_value_tn = openai_usd_b_daily * fx_daily / 1000  # USD B × JPY/USD / 1000 = T JPY

    # ByteDance value (trillion JPY)
    q_dates_bd = pd.to_datetime(list(quarterly_bytedance_usd_b.keys()))
    q_vals_bd = list(quarterly_bytedance_usd_b.values())
    bytedance_usd_b_daily = pd.Series(q_vals_bd, index=q_dates_bd).reindex(all_dates).ffill().bfill()
    bytedance_value_tn = bytedance_usd_b_daily * fx_daily / 1000

    # Residual: Total NAV - Arm - SBKK - OpenAI - ByteDance
    # Back-calculate from quarterly official NAV using actual FX at each quarter-end
    # FX rates at quarter-ends (USD/JPY); 2026-03-31 official from SoTP page
    quarterly_fx = {
        '2024-03-31': 151.3, '2024-06-30': 160.9, '2024-09-30': 143.6,
        '2024-12-31': 157.3, '2025-03-31': 149.8, '2025-06-30': 146.5,
        '2025-09-30': 142.2, '2025-12-31': 156.4, '2026-03-31': 159.88,
    }
    residual_data = {}
    for d_str, (nav, arm_off, sbkk_off) in quarterly_sotp.items():
        fx_q = quarterly_fx.get(d_str, 155)
        oai_tn = quarterly_openai_usd_b.get(d_str, 0) * fx_q / 1000
        bd_tn = quarterly_bytedance_usd_b.get(d_str, 0) * fx_q / 1000
        residual = nav - arm_off - sbkk_off - oai_tn - bd_tn
        residual_data[d_str] = residual

    q_dates_r = pd.to_datetime(list(residual_data.keys()))
    residual_vals = list(residual_data.values())
    residual_daily = pd.Series(residual_vals, index=q_dates_r).reindex(all_dates).ffill().bfill()

    # Apply cash outflow / debt adjustments to residual
    # These account for known large cash deployments in quarters
    # where we don't yet have an official NAV to calibrate against.
    if quarterly_cash_adjustments_usd_b:
        adj_dates = pd.to_datetime(list(quarterly_cash_adjustments_usd_b.keys()))
        adj_vals = list(quarterly_cash_adjustments_usd_b.values())
        # Build sparse series, then ffill so the adjustment persists forward
        adj_series_usd = pd.Series(adj_vals, index=adj_dates).reindex(all_dates).ffill().fillna(0)
        # Convert USD B to T JPY using daily FX
        cash_adj_tn = adj_series_usd * fx_daily / 1000
        residual_daily = residual_daily + cash_adj_tn

    # Total NAV
    total_nav_tn = arm_value_tn + sbkk_value_tn + openai_value_tn + bytedance_value_tn + residual_daily
    nav_per_share = (total_nav_tn * 1e12) / SBG_SHARES

    # Discount
    discount = (1 - sbg_daily / nav_per_share) * 100

    # Clean NaN
    mask = nav_per_share.notna() & sbg_daily.notna() & discount.notna()
    nav_per_share = nav_per_share[mask]
    sbg_daily = sbg_daily[mask]
    discount = discount[mask]
    arm_value_tn = arm_value_tn[mask]
    sbkk_value_tn = sbkk_value_tn[mask]
    openai_value_tn = openai_value_tn[mask]
    bytedance_value_tn = bytedance_value_tn[mask]
    total_nav_tn = total_nav_tn[mask]
    residual_daily = residual_daily[mask]

    if len(nav_per_share) == 0:
        print(json.dumps({"ok": False, "error": "No data after alignment"}))
        sys.exit(1)

    # Current values
    curr_nav = nav_per_share.iloc[-1]
    curr_price = sbg_daily.iloc[-1]
    curr_discount = discount.iloc[-1]
    curr_arm = arm_value_tn.iloc[-1]
    curr_sbkk = sbkk_value_tn.iloc[-1]
    curr_openai = openai_value_tn.iloc[-1]
    curr_bytedance = bytedance_value_tn.iloc[-1]
    curr_residual = residual_daily.iloc[-1]
    curr_total_nav = total_nav_tn.iloc[-1]
    curr_mktcap = curr_price * SBG_SHARES / 1e12

    # OpenAI/ByteDance in USD B for display
    curr_openai_usd_b = openai_usd_b_daily.iloc[-1] if len(openai_usd_b_daily[mask]) > 0 else 0
    curr_bytedance_usd_b = bytedance_usd_b_daily.iloc[-1] if len(bytedance_usd_b_daily[mask]) > 0 else 0

    if len(discount) >= 2:
        prev_discount = discount.iloc[-2]
        disc_change = curr_discount - prev_discount
    else:
        disc_change = 0

    avg_discount = discount.mean()

    ##############################################
    # 4. Generate chart
    ##############################################
    fig, axes = plt.subplots(3, 1, figsize=(14, 11), gridspec_kw={'height_ratios': [1.3, 0.7, 1]})
    today_str = datetime.now().strftime('%Y-%m-%d')
    fig.suptitle(f'软银集团（9984.T）NAV 折价｜{today_str}\n'
                 f'Arm、SBKK 每日盯市｜OpenAI、字节跳动按季度估值｜其余资产按季度更新',
                 fontsize=11, fontweight='bold', y=0.98)

    # Panel 1: Price vs NAV
    ax1 = axes[0]
    ax1.plot(sbg_daily.index, sbg_daily.values, color='#2196F3', linewidth=1.3, label='股价', alpha=0.9)
    ax1.plot(nav_per_share.index, nav_per_share.values, color='#FF5722', linewidth=1.5, label='估算 NAV/股')
    ax1.fill_between(sbg_daily.index, sbg_daily.values, nav_per_share.values,
                      where=(sbg_daily.values < nav_per_share.values), alpha=0.1, color='red')
    ax1.fill_between(sbg_daily.index, sbg_daily.values, nav_per_share.values,
                      where=(sbg_daily.values >= nav_per_share.values), alpha=0.1, color='green')
    ax1.set_ylabel('日元/股（拆股后）', fontsize=10)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)

    # Panel 2: NAV composition stacked area (5 components)
    ax2 = axes[1]
    arm_pct = (arm_value_tn / total_nav_tn * 100).clip(0, 100)
    openai_pct = (openai_value_tn / total_nav_tn * 100).clip(0, 100)
    bytedance_pct = (bytedance_value_tn / total_nav_tn * 100).clip(0, 100)
    sbkk_pct = (sbkk_value_tn / total_nav_tn * 100).clip(0, 100)
    # residual is whatever's left

    y1 = arm_pct.values
    y2 = y1 + openai_pct.values
    y3 = y2 + bytedance_pct.values
    y4 = y3 + sbkk_pct.values

    ax2.fill_between(arm_pct.index, 0, y1, alpha=0.5, color='#9C27B0', label=f'Arm ({arm_pct.iloc[-1]:.0f}%)')
    ax2.fill_between(arm_pct.index, y1, y2, alpha=0.5, color='#00BCD4', label=f'OpenAI ({openai_pct.iloc[-1]:.0f}%)')
    ax2.fill_between(arm_pct.index, y2, y3, alpha=0.5, color='#FF5722', label=f'ByteDance ({bytedance_pct.iloc[-1]:.0f}%)')
    ax2.fill_between(arm_pct.index, y3, y4, alpha=0.4, color='#FF9800', label=f'SBKK ({sbkk_pct.iloc[-1]:.0f}%)')
    ax2.fill_between(arm_pct.index, y4, 100, alpha=0.3, color='#607D8B', label=f'其他资产−债务（{100-y4[-1]:.0f}%）')
    ax2.set_ylabel('NAV 构成（%）', fontsize=9)
    ax2.set_ylim(0, 100)
    ax2.legend(loc='upper left', fontsize=7, ncol=5, framealpha=0.8)
    ax2.grid(True, alpha=0.3)

    # Panel 3: Discount
    ax3 = axes[2]
    ax3.fill_between(discount.index, 0, discount.values,
                      where=(discount.values >= 0), alpha=0.2, color='#E53935')
    ax3.fill_between(discount.index, 0, discount.values,
                      where=(discount.values < 0), alpha=0.2, color='#4CAF50')
    ax3.plot(discount.index, discount.values, color='#E53935', linewidth=1.3)
    ax3.axhline(y=0, color='black', linewidth=0.8)
    ax3.axhline(y=avg_discount, color='orange', linestyle='--', alpha=0.5)
    ax3.text(discount.index[5], avg_discount + 2, f'2 年均值：{avg_discount:.0f}%', fontsize=8, color='orange')

    curr_d = curr_discount
    ax3.annotate(f'当前：{curr_d:.0f}%',
                 xy=(discount.index[-1], curr_d),
                 xytext=(-130, 20), textcoords='offset points',
                 fontsize=11, fontweight='bold', color='#E53935',
                 arrowprops=dict(arrowstyle='->', color='#E53935'))

    min_idx = discount.idxmin()
    min_val = discount.min()
    ax3.annotate(f'{min_val:.0f}% ({min_idx.strftime("%b %y")})',
                 xy=(min_idx, min_val),
                 xytext=(40, -25), textcoords='offset points',
                 fontsize=9, color='#4CAF50', fontweight='bold',
                 arrowprops=dict(arrowstyle='->', color='#4CAF50'))

    ax3.set_ylabel('NAV 折价（%）', fontsize=10)
    ax3.set_ylim(-15, 70)
    ax3.grid(True, alpha=0.3)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.set_xlim(sbg_daily.index[0], sbg_daily.index[-1])
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    fig.text(0.5, 0.005,
             f'Arm：每日盯市（×{ARM_HAIRCUT:.3f}）｜SBKK：每日盯市（×{SBKK_HAIRCUT:.3f}）｜'
             f'OpenAI：约 13% committed stake，2026-03 官方 FV USD 79.6B｜字节跳动：SVF1 约 3% 持股｜其余：季度 SoTP\n'
             f'NAV：Arm ¥{curr_arm:.1f}T + OpenAI ¥{curr_openai:.1f}T（USD {curr_openai_usd_b:.0f}B）+ '
             f'字节跳动 ¥{curr_bytedance:.1f}T（USD {curr_bytedance_usd_b:.0f}B）+ '
             f'SBKK ¥{curr_sbkk:.1f}T + 其他 ¥{curr_residual:.1f}T = ¥{curr_total_nav:.1f}T｜'
             f'市值 ¥{curr_mktcap:.1f}T｜折价 {curr_d:.0f}%',
             ha='center', fontsize=6.5, color='gray')

    plt.tight_layout(rect=[0, 0.05, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()

    ##############################################
    # 5. Generate text summary
    ##############################################
    disc_arrow = "↑" if disc_change > 0 else "↓" if disc_change < 0 else "→"
    summary_lines = [
        f"📊 软银集团 NAV 折价｜{today_str}",
        f"",
        f"股价：¥{curr_price:,.0f}｜NAV/股：¥{curr_nav:,.0f}",
        f"折价：{curr_discount:.1f}%（较昨日 {disc_arrow}{abs(disc_change):.1f} 个百分点）",
        f"2 年均值：{avg_discount:.0f}%",
        f"",
        f"NAV 构成（¥{curr_total_nav:.1f}T）：",
        f"  Arm：¥{curr_arm:.1f}T（{curr_arm/curr_total_nav*100:.0f}%）",
        f"  OpenAI：¥{curr_openai:.1f}T / USD {curr_openai_usd_b:.0f}B（{curr_openai/curr_total_nav*100:.0f}%；约 13% committed stake，2026-03 官方 FV USD 79.6B）",
        f"  字节跳动：¥{curr_bytedance:.1f}T / USD {curr_bytedance_usd_b:.0f}B（{curr_bytedance/curr_total_nav*100:.0f}%；SVF1 约 3% 持股，二级市场估值约 USD 480B—600B）",
        f"  SBKK：¥{curr_sbkk:.1f}T（{curr_sbkk/curr_total_nav*100:.0f}%）",
        f"  其他资产−债务：¥{curr_residual:.1f}T（{curr_residual/curr_total_nav*100:.0f}%）",
        f"",
        f"市值：¥{curr_mktcap:.1f}T",
        f"",
        "口径限制：OpenAI 和字节跳动是非上市资产，估值参考最近融资或二级市场交易价格。",
    ]
    summary_text = "\n".join(summary_lines)

    ##############################################
    # 6. Output
    ##############################################
    if args.json:
        result = {
            "ok": True,
            "date": today_str,
            "price_jpy": round(curr_price, 0),
            "nav_per_share_jpy": round(curr_nav, 0),
            "discount_pct": round(curr_discount, 1),
            "discount_change_pp": round(disc_change, 1),
            "avg_discount_pct": round(avg_discount, 1),
            "arm_value_tn_jpy": round(curr_arm, 2),
            "openai_value_tn_jpy": round(curr_openai, 2),
            "openai_value_usd_b": round(curr_openai_usd_b, 1),
            "bytedance_value_tn_jpy": round(curr_bytedance, 2),
            "bytedance_value_usd_b": round(curr_bytedance_usd_b, 1),
            "sbkk_value_tn_jpy": round(curr_sbkk, 2),
            "residual_tn_jpy": round(curr_residual, 2),
            "total_nav_tn_jpy": round(curr_total_nav, 2),
            "mktcap_tn_jpy": round(curr_mktcap, 2),
            "chart_path": output_path,
            "summary": summary_text,
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(summary_text)
        print(f"\nChart saved: {output_path}")

    ##############################################
    # 7. Feishu delivery (Card Schema 2.0)
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
        card_file = os.path.join(workspace, 'temp', 'softbank_nav_card.md')
        os.makedirs(os.path.dirname(os.path.abspath(card_file)), exist_ok=True)
        card_markdown = (
            f"## 核心估值指标\n\n"
            f"· **当前股价**: ¥{curr_price:,.0f} ｜ **NAV/股**: ¥{curr_nav:,.0f}\n"
            f"· **当前折价**: **{curr_discount:.1f}%**（较昨日 {disc_arrow}{abs(disc_change):.1f} 个百分点）\n"
            f"· **2 年折价均值**: {avg_discount:.0f}% ｜ **当前市值**: ¥{curr_mktcap:.1f}T\n\n"
            f"## NAV 构成细分（总计 ¥{curr_total_nav:.1f}T）\n\n"
            f"· **Arm**: ¥{curr_arm:.1f}T ({curr_arm/curr_total_nav*100:.0f}%)\n"
            f"· **OpenAI**: ¥{curr_openai:.1f}T / USD {curr_openai_usd_b:.0f}B ({curr_openai/curr_total_nav*100:.0f}%；约 13% committed stake)\n"
            f"· **字节跳动**: ¥{curr_bytedance:.1f}T / USD {curr_bytedance_usd_b:.0f}B ({curr_bytedance/curr_total_nav*100:.0f}%；SVF1 约 3% 持股)\n"
            f"· **软银电信 (SBKK)**: ¥{curr_sbkk:.1f}T ({curr_sbkk/curr_total_nav*100:.0f}%)\n"
            f"· **其他资产 − 债务**: ¥{curr_residual:.1f}T ({curr_residual/curr_total_nav*100:.0f}%)\n\n"
            f"## 折价走势图\n\n"
            f"IMG:{output_path}\n\n"
            f"## 口径限制\n\n"
            f"OpenAI 和字节跳动是非上市资产，估值参考最近融资或二级市场交易价格。"
        )
        with open(card_file, 'w', encoding='utf-8') as f:
            f.write(card_markdown)

        ret = os.system(
            f'python3 "{card_script}" --chat-id {feishu_chat_id} '
            f'--title "📊 软银集团 NAV 折价监控" '
            f'--subtitle "截至 {today_str} · 日度追踪" '
            f'--header-template indigo '
            f'--file "{card_file}"'
        )
        if ret != 0:
            print(f"Warning: feishu card send returned {ret}", file=sys.stderr)
            sys.exit(1)

if __name__ == '__main__':
    main()
