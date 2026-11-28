#!/usr/bin/env python3
"""asia_leverage_crossview — 四地(CN/KR/JP/TW)杠杆·拥挤度跨市场日频监控 v3.

只读四个 leverage-monitor 的落盘 CSV(不发任何网络请求; JP指数长历史用一次性
回填文件 nikkei_history_backfill.csv)。

v3 变更(2026-08-04, 用户要求"卡片要 verbal、能快速产生交易判断"):
  - 新增 verbal 卡片输出 card_latest.md: 每市场 = 拥挤度定性判断(点名依据指标)
    + 去杠杆进度(对比历史浅度/深度出清事件完成几分之几、速度排名) + 极端信号,
    并内嵌历史图。原 v2 全指标报告仍写 report_latest.md 供追溯, 不再进卡片。
  - 新增 --charts: 每市场一张两栏历史图(指数+主杠杆余额 / 拥挤度综合分0-100),
    去杠杆事件区间红色阴影。综合分=composite指标滚动1y分位方向归一均值,
    与卡片标题里的拥挤度同一定义、同一序列。
  - 去杠杆事件库按深度分层: 浅度(回撤>-20%)/深度(<=-20%), 当前回撤给出
    对两类中位深度的完成度和10观测降速对比。
v3.1 变更(2026-08-04, 用户指出KR拥挤度与体感不符+2020-03假信号):
  综合分重构为主杠杆存量三视角加权 L40/T40/R20(见 crowding_history docstring),
  剔除活动量成分; 锚点验证结果见 crowding_history 上方注释（原验证脚本放在 temp/，已随清理丢失）。
v2 遗留设计不变: 指标 tier1/tier2 分层、滚动1y分位(刻意不拉长窗口)、
跨周期比较只用事件相对深度/速度。

用法:
  python3 scripts/asia_leverage_crossview.py            # 人读 v2 详细报告
  python3 scripts/asia_leverage_crossview.py --charts   # 生成PNG+verbal卡片(打印卡片md)
  python3 scripts/asia_leverage_crossview.py --json     # 机器读 JSON
输出文件:
  temp/asia-leverage-crossview/latest.json
  temp/asia-leverage-crossview/report_latest.md (+按日期存档)  # v2 详细版
  temp/asia-leverage-crossview/card_latest.md   (+按日期存档)  # v3 verbal卡片
  temp/asia-leverage-crossview/charts/{cn,kr,jp,tw}_latest.png

数据依赖(由各自 daily cron 维护):
  state/cn-leverage/*.csv
  research-outputs/{kr,jp,tw}-leverage/data/*.csv
"""
import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

WS = Path(__file__).resolve().parent.parent
OUT_DIR = WS / "temp" / "asia-leverage-crossview"
CHART_DIR = OUT_DIR / "charts"
DIRS = {
    "cn": WS / "state" / "cn-leverage",
    "kr": WS / "research-outputs" / "kr-leverage" / "data",
    "jp": WS / "research-outputs" / "jp-leverage" / "data",
    "tw": WS / "research-outputs" / "tw-leverage" / "data",
}
TODAY = dt.date.today()
# 日频序列超过5个自然日没更新算 stale; 周频给 12 天
STALE_DAILY, STALE_WEEKLY = 5, 12
MIN_OBS_DAILY, MIN_OBS_WEEKLY = 60, 26
DEEP_THR = -20.0  # 事件深度<=该值算深度出清, 否则浅度
# 图表起点: 用户指定2011为基准; TW数据2021起
CHART_START = {"cn": "2011-01-01", "kr": "2011-01-01",
               "jp": "2011-01-01", "tw": "2021-07-01"}
FLAGS = {"cn": "🇨🇳", "kr": "🇰🇷", "jp": "🇯🇵", "tw": "🇹🇼"}


def load(market, name, date_col="date"):
    p = DIRS[market] / name
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df[date_col] = df[date_col].astype(str)
    # 多代码/多品种文件按复合键去重, 否则每天只会留下文件里最后一个 code/variety
    keys = [date_col] + [c for c in ("code", "variety", "product") if c in df.columns]
    return df.sort_values(date_col).drop_duplicates(keys, keep="last")


def series_of(df, col, date_col="date"):
    if df is None or col not in df.columns:
        return None
    s = pd.to_numeric(df[col], errors="coerce")
    s.index = df[date_col].values
    return s.dropna()


def pct_rank(s, window, min_obs):
    if s is None or len(s) < min_obs:
        return None
    tail = s.tail(window)
    return round(float((tail <= tail.iloc[-1]).mean() * 100), 1)


def chg(s, n, mode="pct"):
    """mode=pct 百分比变化; mode=abs 差值(适合本身就是百分比/比率的序列)."""
    if s is None or len(s) < n + 1:
        return None
    last, prev = float(s.iloc[-1]), float(s.iloc[-1 - n])
    if mode == "abs":
        return round(last - prev, 2)
    if prev == 0 or not math.isfinite(prev):
        return None
    return round((last / prev - 1) * 100, 2)


def cum(s, n):
    if s is None or len(s) < n:
        return None
    return round(float(s.tail(n).sum()), 1)


def stale_flag(s, weekly=False):
    if s is None or not len(s):
        return True, None
    last = dt.date.fromisoformat(str(s.index[-1])[:10])
    limit = STALE_WEEKLY if weekly else STALE_DAILY
    return (TODAY - last).days > limit, last.isoformat()


def ind(label, s, unit="", direction=1, weekly=False, composite=False,
        chg_mode="pct", fmt="{:,.1f}", note="", tier=1):
    """构建一个指标读数 dict; s 为 None/过短时返回可展示的降级条目."""
    window = 52 if weekly else 252
    min_obs = MIN_OBS_WEEKLY if weekly else MIN_OBS_DAILY
    if s is None or not len(s):
        return {"label": label, "available": False, "note": note or "无数据",
                "tier": tier}
    stale, asof = stale_flag(s, weekly)
    d5 = chg(s, 1 if weekly else 5, chg_mode)
    d10 = chg(s, 4 if weekly else 10, chg_mode)
    p = pct_rank(s, window, min_obs)
    return {
        "label": label, "available": True, "unit": unit, "asof": asof,
        "stale": stale, "weekly": weekly,
        "value": round(float(s.iloc[-1]), 3),
        "value_fmt": fmt.format(float(s.iloc[-1])),
        "chg5": d5, "chg10": d10, "chg_mode": chg_mode,
        "pct_1y": p, "direction": direction, "composite": composite,
        "pct_thin": p is None and len(s) < min_obs, "note": note, "tier": tier,
    }


# ------------------------------------------------- 去杠杆事件库与追踪

def _dt_series(s):
    s2 = s.copy()
    s2.index = pd.to_datetime(pd.Index(s2.index).astype(str))
    return s2[~s2.index.duplicated(keep="last")].sort_index()


def detect_episodes(s, window, thr=-0.05, min_depth=-0.06, merge_gap=20):
    """主杠杆序列滚动峰值回撤事件; 返回按峰值日排序的事件列表."""
    if s is None or len(s) < window:
        return []
    roll_peak = s.rolling(window, min_periods=window // 2).max()
    dd = s / roll_peak - 1
    idx = np.where((dd <= thr).values)[0]
    if len(idx) == 0:
        return []
    groups, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev <= merge_gap:
            prev = i
        else:
            groups.append((start, prev))
            start = prev = i
    groups.append((start, prev))
    eps = []
    for g0, g1 in groups:
        seg = s.iloc[max(0, g0 - window):g1 + 1]
        peak_pos = seg.values.argmax()
        peak_date, peak_val = seg.index[peak_pos], float(seg.values[peak_pos])
        trough_seg = s.iloc[g0:g1 + 1]
        tr_pos = trough_seg.values.argmin()
        tr_date, tr_val = trough_seg.index[tr_pos], float(trough_seg.values[tr_pos])
        if peak_date >= tr_date or peak_val <= 0:
            continue
        depth = tr_val / peak_val - 1
        if depth > min_depth:
            continue
        span = s.loc[peak_date:tr_date]
        max10 = float(span.pct_change(10).min()) if len(span) > 10 else depth
        eps.append(dict(peak=str(peak_date.date()), trough=str(tr_date.date()),
                        depth_pct=round(depth * 100, 1), dur_obs=len(span) - 1,
                        max10_pct=round(max10 * 100, 1), peak_val=peak_val))
    best = {}
    for e in eps:
        if e["peak"] not in best or e["depth_pct"] < best[e["peak"]]["depth_pct"]:
            best[e["peak"]] = e
    return sorted(best.values(), key=lambda e: e["peak"])


def _med(xs):
    return round(float(np.median(xs)), 1) if xs else None


def delever_tracker(s_raw, weekly=False, since_label=""):
    """当前去杠杆状态 + 对标历史浅度/深度出清事件. 数据不足返回 None."""
    if s_raw is None or len(s_raw) < (52 if weekly else 252):
        return None
    s = _dt_series(s_raw)
    window = 52 if weekly else 252
    unit = "周" if weekly else "个交易日"
    spd_u = "周" if weekly else "日"
    eps = detect_episodes(s, window, merge_gap=6 if weekly else 20)
    cur = float(s.iloc[-1])
    # 剔除远古规模过小的数据段(如KR 1998-99接近零的余额), 市场结构不可比
    eps = [e for e in eps if e["peak_val"] > cur * 0.05]
    tail = s.tail(window)
    peak_date, peak_val = tail.idxmax(), float(tail.max())
    dd_now = (cur / peak_val - 1) * 100
    span = s.loc[peak_date:]
    n_since = len(span) - 1
    max10_cur = (float(span.pct_change(10).min()) * 100
                 if len(span) > 10 else dd_now)
    chg5 = chg(s, 2 if weekly else 5) or 0.0
    hist = [e for e in eps
            if pd.Timestamp(e["peak"]) < peak_date - pd.Timedelta(days=5)]
    shallow = [e for e in hist if e["depth_pct"] > DEEP_THR]
    deep = [e for e in hist if e["depth_pct"] <= DEEP_THR]
    sh_d, dp_d = _med([e["depth_pct"] for e in shallow]), _med([e["depth_pct"] for e in deep])
    sh_v, dp_v = _med([e["max10_pct"] for e in shallow]), _med([e["max10_pct"] for e in deep])
    frac_sh = round(dd_now / sh_d * 100) if sh_d else None
    frac_dp = round(dd_now / dp_d * 100) if dp_d else None
    faster = [e for e in hist if e["max10_pct"] < max10_cur]
    rank = len(faster) + 1 if hist else None
    # 状态判定
    if dd_now <= -5:
        state = "去杠杆进行中" if chg5 < -0.5 else "去杠杆暂停/企稳"
    elif dd_now <= -2:
        state = "小幅回落"
    else:
        state = "无活跃去杠杆"
    # verbal: 进度 + 速度
    line = (f"主杠杆距峰值({peak_date.date()}) {dd_now:+.1f}% · 第{n_since}{unit}")
    prog = []
    if dd_now <= -2:
        if sh_d is not None and frac_sh is not None:
            prog.append(f"浅度出清({len(shallow)}次·中位{sh_d}%)已走完"
                        if frac_sh >= 100 else
                        f"浅度出清({len(shallow)}次·中位{sh_d}%)完成约{frac_sh}%")
        if dp_d is not None and frac_dp is not None:
            prog.append(f"若演变为深度出清({len(deep)}次·中位{dp_d}%)则完成约{frac_dp}%")
        spd = f"10{spd_u}最快降速 {max10_cur:+.1f}%"
        cmp_parts = []
        if sh_v is not None:
            cmp_parts.append(f"浅度中位{sh_v}%")
        if dp_v is not None:
            cmp_parts.append(f"深度中位{dp_v}%")
        if cmp_parts:
            spd += f"(vs {' / '.join(cmp_parts)})"
        if rank is not None and dd_now <= -5:
            spd += f", {since_label}以来第{rank}快"
        prog.append(spd)
    verbal = line + ("; " + "; ".join(prog) if prog else "")
    return dict(state=state, dd_now_pct=round(dd_now, 1), peak_date=str(peak_date.date()),
                obs_since_peak=n_since, max10_cur_pct=round(max10_cur, 1),
                speed_rank=rank, n_hist_episodes=len(hist),
                shallow_med_depth=sh_d, deep_med_depth=dp_d,
                shallow_med_speed=sh_v, deep_med_speed=dp_v,
                frac_of_shallow_pct=frac_sh, frac_of_deep_pct=frac_dp,
                n_shallow=len(shallow), n_deep=len(deep),
                line=verbal, episodes=hist)


# ------------------------------------------------- 拥挤度综合分历史序列
# v3.1 (2026-08-04) 重构: 只用主杠杆"存量"三视角, 剔除换手率/融资买入占比/1570份额
# 等活动量成分(崩盘中被动飙升产生假信号)。四市场同构, 锚点验证(原脚本在 temp/ 已丢失)结果:
# 2015CN/2020-03KR·JP/2024-08JP/2025-04TW 崩盘中分数
# 全部塌陷, 2026-02KR=94、2015-06CN=88、2013-05JP=100 等体感高点全部读高。
CROWD_W = {"L": 0.40, "T": 0.40, "R": 0.20}  # 水平/趋势偏离/相对市值(冻结分母)


def crowding_history(bal, denom=None, weekly=False):
    """主杠杆存量三视角加权拥挤度 0-100 历史序列.

    L 水平    = 余额 滚动1y分位            (权40%)
    T 趋势偏离 = 余额/MA200(余额) 滚动3y分位 (周频MA40/窗156w, 权40%)
    R 相对规模 = 余额/MA120(市值) 滚动3y分位 (周频MA26, 权20%; 分母冻结均值,
               避免崩盘中市值先崩导致比率被动飙升的假信号)
    R 缺失时 L/T 权重按比例归一。返回 (comp_series, last_components_dict)。
    """
    if bal is None or len(bal) < (MIN_OBS_WEEKLY if weekly else MIN_OBS_DAILY):
        return None, None
    b = _dt_series(bal)
    if weekly:
        w1, m1, w3, m3, ma_t, ma_d = 52, 26, 156, 52, 40, 26
    else:
        w1, m1, w3, m3, ma_t, ma_d = 252, 60, 756, 252, 200, 120

    def rp(s, window, minp):
        return s.rolling(window, min_periods=minp).rank(pct=True) * 100

    cols = {"L": rp(b, w1, m1),
            "T": rp(b / b.rolling(ma_t, min_periods=ma_t // 2).mean(), w3, m3)}
    if denom is not None and len(denom):
        d = _dt_series(denom)
        d = d.reindex(b.index.union(d.index)).ffill(limit=10).reindex(b.index)
        dfroz = d.rolling(ma_d, min_periods=ma_d // 2).mean()
        cols["R"] = rp(b / dfroz, w3, m3)
    df = pd.DataFrame(cols)
    w = pd.Series({k: CROWD_W[k] for k in df.columns})
    avail = df.notna() * 1.0
    comp = (df.fillna(0) * w).sum(axis=1) / (avail * w).sum(axis=1)
    comp = comp.dropna()
    if not len(comp):
        return None, None
    last = df.loc[comp.index[-1]]
    last_d = {k: (round(float(last[k]), 1) if pd.notna(last[k]) else None)
              for k in df.columns}
    last_d["asof"] = str(comp.index[-1].date())
    return comp, last_d


# ---------------------------------------------------------------- per market
# 分层依据 2026-08-04 事件回测(indicator_study.md):
# tier=1 核心层 = 前置性高/同步性好/噪声低; tier=2 次要层压缩为一行。
# _chart/_crowd 为图表与综合分内部数据, JSON 输出前剥离。

def build_cn():
    core = load("cn", "margin_core.csv")
    bal = series_of(core, "financing_balance_cny100m")
    out = {"market": "中国A股", "indicators": [], "flows": [], "stress": []}
    # 回测: 融资余额1y分位噪声率53%无预警价值, 但其回撤深度/速度是最好的去杠杆标尺
    out["delever"] = delever_tracker(bal, since_label="2010")
    out["indicators"].append(ind(
        "融资余额", bal, "亿元", composite=True, fmt="{:,.0f}", tier=1))
    ratio = series_of(core, "financing_balance_to_float_mktcap_pct")
    out["indicators"].append(ind(
        "融资余额/流通市值", ratio,
        "%", composite=True, chg_mode="abs", fmt="{:.2f}", tier=2))
    buy_turn = series_of(core, "financing_buy_to_turnover_pct")
    out["indicators"].append(ind(
        "融资买入/成交额", buy_turn,
        "%", composite=True, chg_mode="abs", fmt="{:.2f}",
        note="历史高位11-12%(2015/2020)", tier=2))
    turn = load("cn", "market_turnover.csv")
    turn_s = series_of(turn, "market_turnover_rate_pct")
    out["indicators"].append(ind(
        "全市场换手率", turn_s,
        "%", composite=True, chg_mode="abs", fmt="{:.2f}", tier=2))
    fb = load("cn", "futures_basis.csv")
    im_s = None
    if fb is not None:
        for v, t in (("IM", 1), ("IC", 2)):
            sub = fb[fb["variety"] == v]
            s = series_of(sub, "annualized_basis_pct")
            if v == "IM":
                im_s = s
            out["indicators"].append(ind(
                f"{v}年化基差", s, "%", direction=-1, composite=(v == "IM"),
                chg_mode="abs", fmt="{:+.1f}",
                note="贴水越深=中性/雪球对冲盘越拥挤" if v == "IM" else "", tier=t))
    quant = load("cn", "quant_unwind_signals.csv")
    if quant is not None and len(quant):
        # 回测: Gross分前置85.7/同步82.9/噪声9.5%, 中性分79.8/74.2/5.5% — CN最优复合预警
        out["indicators"].append(ind(
            "A股Gross Leverage预警", series_of(quant, "gross_leverage_score"), "/100",
            chg_mode="abs", fmt="{:.1f}",
            note="存量燃料：绝对/相对贴水+原始OI存量+融资存量+小盘强弱", tier=1))
        out["indicators"].append(ind(
            "量化中性拥挤度", series_of(quant, "neutral_crowding_score"), "/100",
            chg_mode="abs", fmt="{:.1f}",
            note="中性特异：IC/IM相对IF贴水+OI存量+小盘风格", tier=1))
        # Unwind压力是同步流量指纹(峰值日分位中位仅53.6), 放状态行不单列
        out["indicators"].append(ind(
            "量化中性Unwind压力", series_of(quant, "unwind_pressure_score"), "/100",
            chg_mode="abs", fmt="{:.1f}",
            note="流量指纹：多头腿Unwind与期指空头挤压分开打分后取高者", tier=2))
        q = quant.dropna(subset=["gross_leverage_score", "unwind_pressure_score"])
        if len(q):
            r = q.iloc[-1]
            progress = (
                "n/a（未检测到活跃Unwind）"
                if pd.isna(r.get("unwind_progress_score"))
                else f"{float(r['unwind_progress_score']):.1f}%"
            )
            out["quant_line"] = (
                f"regime「{r['regime']}」：Gross {float(r['gross_leverage_score']):.1f}/100，"
                f"中性拥挤 {float(r['neutral_crowding_score']):.1f}/100，"
                f"Unwind压力 {float(r['unwind_pressure_score']):.1f}/100，"
                f"方向 {r.get('unwind_direction', 'none')}，出清进度 {progress} ({r['date']})"
            )
            out["stress"].append("A股量化状态 " + out["quant_line"])
    cb = load("cn", "convertible_bond_market.csv")
    out["indicators"].append(ind(
        "转债平均双低", series_of(cb, "average_double_low"), "", direction=-1,
        chg_mode="abs", fmt="{:.1f}", note="双低越低=转债越贵=投机越热(仅250日窗口)",
        tier=2))
    net = series_of(core, "financing_net_buy_cny100m")
    out["flows"].append({"label": "融资净买入累计", "unit": "亿元",
                         "cum5": cum(net, 5), "cum10": cum(net, 10)})
    lu = load("cn", "limit_up_breadth.csv")
    if lu is not None and len(lu):
        r = lu.iloc[-1]
        out["stress"].append(
            f"涨停 {int(r['limit_up_count'])} 家 / 连板高度 {int(r['max_consecutive_boards'])} "
            f"/ 炸板率 {r['blown_board_rate_pct']:.0f}% ({r['date']}, 历史仅~30日不算分位)")
    idxdf = load("cn", "index_prices.csv")
    idx_s = None
    if idxdf is not None:
        idx_s = series_of(idxdf[idxdf["code"] == "IC"], "close")
    out["_crowd"] = dict(bal=bal, denom=series_of(turn, "float_mktcap_cny100m"),
                         weekly=False, denom_label="流通市值")
    out["_chart"] = dict(idx_label="中证500", idx=idx_s,
                         main_label="融资余额", main_unit="亿元", main=bal,
                         weekly=False)
    return out


def build_kr():
    credit = load("kr", "kofia_credit.csv")
    funds = load("kr", "kofia_funds.csv")
    out = {"market": "韩国", "indicators": [], "flows": [], "stress": []}
    tot = series_of(credit, "credit_loan_total")
    tot_t = tot / 1e6 if tot is not None else None  # 百万₩ → 兆₩
    out["delever"] = delever_tracker(tot_t, since_label="2002")
    out["indicators"].append(ind(
        "信用融资余额", tot_t, "兆₩", composite=True, fmt="{:.2f}",
        note="2026-06-24历史峰值38.63兆", tier=1))
    kospi, kosdaq = load("kr", "kofia_kospi.csv"), load("kr", "kofia_kosdaq.csv")
    m1, m2 = series_of(kospi, "mcap"), series_of(kosdaq, "mcap")
    ratio, mc = None, None
    if tot is not None and m1 is not None and m2 is not None:
        mc = m1.add(m2.reindex(m1.index), fill_value=None).dropna()
        common = tot.index.intersection(mc.index)
        ratio = (tot.loc[common] / mc.loc[common] * 100).dropna()
    # 回测: 该比率2002-2024前置性99.2/噪声16.8%为KR最优, 但2026-06事件失效
    # (峰值日分位仅7.5, 市值涨速>信用) → 必须与绝对余额并读
    out["indicators"].append(ind(
        "信用融资/两市市值", ratio, "%", composite=True, chg_mode="abs", fmt="{:.3f}",
        note="2002-24回测最优预警, 但2026-06失效(指数涨速>信用), 与绝对余额并读", tier=1))
    dep = series_of(funds, "investor_deposit")
    out["indicators"].append(ind(
        "投资者预托金", dep / 1e6 if dep is not None else None, "兆₩", fmt="{:.1f}",
        note="待入场弹药, 高低本身中性", tier=2))
    out["stress"].append(ind(
        "반대매매(强平)占比", series_of(funds, "forced_sell_pct"), "%",
        chg_mode="abs", fmt="{:.1f}", note="去杠杆强度直读"))
    out["stress"].append(ind(
        "VKOSPI", series_of(load("kr", "vkospi.csv"), "close"),
        "", chg_mode="abs", fmt="{:.1f}"))
    fl = series_of(load("kr", "naver_flows_kospi.csv"), "individual")
    fl_t = fl / 10000 if fl is not None else None  # 억₩ → 兆₩
    out["flows"].append({"label": "KOSPI个人净买入累计", "unit": "兆₩",
                         "cum5": cum(fl_t, 5), "cum10": cum(fl_t, 10)})
    out["_crowd"] = dict(bal=tot_t, denom=mc, weekly=False, denom_label="两市市值")
    out["_chart"] = dict(idx_label="KOSPI", idx=series_of(kospi, "index_close"),
                         main_label="信用融资余额", main_unit="兆₩", main=tot_t,
                         weekly=False)
    return out


def build_jp():
    mw = load("jp", "margin_weekly.csv")
    out = {"market": "日本", "indicators": [], "flows": [], "stress": []}
    long_v = series_of(mw, "long_value_mjpy")
    long_t = long_v / 1e6 if long_v is not None else None  # 百万円 → 兆円
    out["delever"] = delever_tracker(long_t, weekly=True, since_label="2002")
    out["indicators"].append(ind(
        "信用买残(周频)", long_t, "兆円", weekly=True, composite=True, fmt="{:.2f}",
        tier=1))
    short_v = series_of(mw, "short_value_mjpy")
    mr = None
    if long_v is not None and short_v is not None:
        mr = (long_v / short_v.reindex(long_v.index)).dropna()
    # 回测: 信用倍率前置98.1/同步53.9/噪声6.2% — 四地单指标最优
    out["indicators"].append(ind(
        "信用倍率(买残/卖残)", mr, "x", weekly=True, composite=True,
        chg_mode="abs", fmt="{:.1f}", note="2002-26回测: 前置98/噪声6%, JP首选预警",
        tier=1))
    nav = load("jp", "etf_nav_units_daily.csv")
    nav1570 = None
    if nav is not None:
        nav = nav[nav["code"].astype(str) == "1570"]
        nav1570 = series_of(nav, "shares_os")
    # 回测: 1570份额峰值日分位中位仅68.7且事件中常升(散户抄底), 是反向温度计非预警
    out["indicators"].append(ind(
        "1570杠杆ETF份额", nav1570, "口", composite=True,
        fmt="{:,.0f}", note="散户抄底温度计(跌时申购), 反向解读", tier=2))
    fw = load("jp", "investor_flows_weekly.csv")
    out["indicators"].append(ind(
        "个人买入中信用占比(周频)", series_of(fw, "individual_margin_buy_share_pct"),
        "%", weekly=True, chg_mode="abs", fmt="{:.1f}",
        note="事件谷底常走高(逆势加杠杆), 反向解读", tier=2))
    out["stress"].append(ind(
        "日经VI", series_of(load("jp", "market_reference.csv"), "nikkei_vi_close"),
        "", chg_mode="abs", fmt="{:.1f}"))
    ind_flow = series_of(fw, "individual_net_bnjpy")
    out["flows"].append({"label": "个人净买入累计(周频)", "unit": "十亿円",
                         "cum5": cum(ind_flow, 1), "cum10": cum(ind_flow, 4),
                         "weekly": True})
    # 指数: 官方 market_reference 仅2023起, 长历史用一次性 yfinance 回填文件
    nk_new = series_of(load("jp", "market_reference.csv"), "nikkei_close")
    nk_old = series_of(load("jp", "nikkei_history_backfill.csv"), "close")
    nk = None
    if nk_old is not None:
        nk = nk_old.copy()
        if nk_new is not None:
            nk = pd.concat([nk[~nk.index.isin(nk_new.index)], nk_new]).sort_index()
    else:
        nk = nk_new
    out["_crowd"] = dict(bal=long_t, denom=nk, weekly=True,
                         denom_label="日经225(市值代理)")
    out["_chart"] = dict(idx_label="日经225", idx=nk,
                         main_label="信用买残", main_unit="兆円", main=long_t,
                         weekly=True)
    return out


def build_tw():
    mm = load("tw", "margin_market.csv")
    out = {"market": "台湾", "indicators": [], "flows": [], "stress": []}
    fv = series_of(mm, "fin_value_kntd")
    fv_yi = fv / 1e5 if fv is not None else None  # 千NT$ → 亿NT$
    out["delever"] = delever_tracker(fv_yi, since_label="2021(事件样本仅3次)")
    out["indicators"].append(ind(
        "融资余额(TWSE)", fv_yi, "亿NT$", composite=True, fmt="{:,.0f}", tier=1))
    dtp = series_of(load("tw", "daytrade_daily.csv"), "dt_value_pct")
    out["indicators"].append(ind(
        "当冲金额占比", dtp,
        "%", composite=True, chg_mode="abs", fmt="{:.1f}", tier=2))
    der = load("tw", "margin_issues_derived.csv")
    usage = series_of(der, "usage_p90")
    # 回测(样本1次): 2026-07事件中73→0.4分位, 同步性/噪声最好, 作TW出清深度标尺
    out["indicators"].append(ind(
        "个股融资使用率P90", usage, "%", composite=True,
        chg_mode="abs", fmt="{:.1f}", note="融资额度逼近上限的广度, TW出清深度标尺",
        tier=1))
    out["indicators"].append(ind(
        "融资集中度Top10", series_of(der, "top10_share_pct"), "%",
        chg_mode="abs", fmt="{:.1f}", tier=2))
    out["indicators"].append(ind(
        "TAIFEX P/C OI比", series_of(load("tw", "taifex_pc_ratio.csv"), "pc_oi_ratio"),
        "", direction=-1, chg_mode="abs", fmt="{:.1f}", note="越低=看涨拥挤/自满",
        tier=2))
    # inst_flows 列名带 _bn 但实际单位是亿NT$(用当日大盘成交额交叉验证过)
    fn = series_of(load("tw", "inst_flows_daily.csv"), "foreign_net_bn")
    out["flows"].append({"label": "外资净买卖累计", "unit": "亿NT$",
                         "cum5": cum(fn, 5), "cum10": cum(fn, 10)})
    vix = series_of(load("tw", "taifex_vix.csv"), "vix_close")
    if vix is not None and len(vix):
        out["stress"].append(
            f"TAIFEX VIX {float(vix.iloc[-1]):.1f} ({vix.index[-1]}, 历史仅{len(vix)}日不算分位)")
    taiex = series_of(load("tw", "taiex_daily.csv"), "close")
    out["_crowd"] = dict(bal=fv_yi, denom=taiex, weekly=False,
                         denom_label="加权指数(市值代理)")
    out["_chart"] = dict(idx_label="加权指数", idx=taiex,
                         main_label="融资余额", main_unit="亿NT$", main=fv_yi,
                         weekly=False)
    return out


# ---------------------------------------------------------------- aggregate

def crowd_evidence(block):
    """拥挤度综合分三成分读数一句话(与分数完全同源)."""
    c = block.get("crowd_components")
    if not c:
        return None
    weekly = (block.get("_crowd") or {}).get("weekly", False)
    ma_lab = "40周" if weekly else "200日"
    parts = []
    if c.get("L") is not None:
        parts.append(f"余额水平1y分位{c['L']:.0f}(权40%)")
    if c.get("T") is not None:
        parts.append(f"较{ma_lab}趋势偏离3y分位{c['T']:.0f}(权40%)")
    if c.get("R") is not None:
        dl = block.get("crowd_denom_label") or "市值"
        parts.append(f"余额/{dl}[冻结分母]3y分位{c['R']:.0f}(权20%)")
    return " · ".join(parts) if parts else None


def crowding_bucket(p):
    if p is None:
        return "n/a"
    if p >= 75:
        return "高位"
    if p >= 55:
        return "偏热"
    if p >= 35:
        return "中性"
    return "低位"


def fmt_chg(i):
    if not i.get("available"):
        return ""
    unit = "pt" if i["chg_mode"] == "abs" else "%"
    lab = ("1w", "4w") if i.get("weekly") else ("5d", "10d")
    parts = []
    for k, l in (("chg5", lab[0]), ("chg10", lab[1])):
        v = i.get(k)
        parts.append(f"{l} {v:+.2f}{unit}" if v is not None else f"{l} n/a")
    return ", ".join(parts)


def evidence_line(block, n=2):
    """挑方向化分位最极端的 composite/tier1 指标作为拥挤度判断依据."""
    cands = []
    for i in block["indicators"]:
        if not (i.get("available") and i.get("pct_1y") is not None):
            continue
        if i.get("tier") != 1:
            continue
        adj = i["pct_1y"] if i["direction"] > 0 else 100 - i["pct_1y"]
        cands.append((abs(adj - 50), adj, i))
    cands.sort(key=lambda t: -t[0])
    parts = []
    for _, adj, i in cands[:n]:
        tag = "反向" if i["direction"] < 0 else ""
        parts.append(f"{i['label']} {i['value_fmt']}{i['unit']}"
                     f"({tag}1y分位{adj:.0f})")
    return "、".join(parts)


# ---------------------------------------------------------------- charts

def _setup_cjk_font():
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    for p in ("/System/Library/Fonts/STHeiti Medium.ttc",
              "/System/Library/Fonts/STHeiti Light.ttc",
              "/System/Library/Fonts/Hiragino Sans GB.ttc"):
        if Path(p).exists():
            try:
                font_manager.fontManager.addfont(p)
                name = font_manager.FontProperties(fname=p).get_name()
                plt.rcParams["font.family"] = [name, "sans-serif"]
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False


def make_chart(mkey, block, comp_hist, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    meta = block.get("_chart") or {}
    idx_s, main_s = meta.get("idx"), meta.get("main")
    if idx_s is None or comp_hist is None or not len(comp_hist):
        return None
    start = pd.Timestamp(CHART_START[mkey])
    idx_d = _dt_series(idx_s)
    idx_d = idx_d[idx_d.index >= start]
    comp = comp_hist[comp_hist.index >= start]
    main_d = _dt_series(main_s) if main_s is not None else None
    if main_d is not None:
        main_d = main_d[main_d.index >= start]
    dl = block.get("delever") or {}
    eps = dl.get("episodes", [])
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.2), sharex=True, dpi=150,
        gridspec_kw={"height_ratios": [3, 2], "hspace": 0.08})
    # 上: 指数(log) + 主杠杆余额
    ax1.plot(idx_d.index, idx_d.values, color="#1f3b70", lw=1.1,
             label=meta["idx_label"])
    ax1.set_yscale("log")
    ax1.set_ylabel(meta["idx_label"], fontsize=9)
    if main_d is not None and len(main_d):
        ax1b = ax1.twinx()
        ax1b.fill_between(main_d.index, main_d.values, color="#4a90d9",
                          alpha=0.22, lw=0)
        ax1b.plot(main_d.index, main_d.values, color="#4a90d9", lw=0.8,
                  label=f"{meta['main_label']}({meta['main_unit']},右)")
        ax1b.set_ylabel(f"{meta['main_label']}({meta['main_unit']})", fontsize=9)
        ax1b.margins(y=0)
        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax1b.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, frameon=False)
    else:
        ax1.legend(loc="upper left", fontsize=8, frameon=False)
    # 下: 拥挤度综合分
    ax2.plot(comp.index, comp.values, color="#b03a2e", lw=1.0,
             label="拥挤度综合分(存量三视角加权)")
    ax2.axhspan(75, 100, color="#e74c3c", alpha=0.07)
    ax2.axhspan(55, 75, color="#e67e22", alpha=0.06)
    for y in (35, 55, 75):
        ax2.axhline(y, color="#999999", lw=0.5, ls=":")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("拥挤度(0-100)", fontsize=9)
    last_v, last_t = float(comp.iloc[-1]), comp.index[-1]
    ax2.scatter([last_t], [last_v], color="#b03a2e", s=18, zorder=5)
    ax2.annotate(f"{last_v:.0f}", (last_t, last_v), textcoords="offset points",
                 xytext=(6, 4), fontsize=9, color="#b03a2e", fontweight="bold")
    ax2.legend(loc="upper left", fontsize=8, frameon=False)
    # 去杠杆事件阴影(峰→谷)
    for e in eps + ([{"peak": dl["peak_date"], "trough": None}]
                    if dl and dl.get("dd_now_pct", 0) <= -5 else []):
        p0 = pd.Timestamp(e["peak"])
        p1 = pd.Timestamp(e["trough"]) if e.get("trough") else comp.index[-1]
        if p1 < start:
            continue
        for ax in (ax1, ax2):
            ax.axvspan(max(p0, start), p1, color="#c0392b", alpha=0.08, lw=0)
    ax1.set_title(
        f"{block['market']} — {meta['idx_label']} vs 拥挤度综合分 "
        f"({max(start, comp.index[0]).date()} 起, 红色阴影=去杠杆事件)",
        fontsize=11)
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    for ax in (ax1, ax2):
        ax.tick_params(labelsize=8)
        ax.grid(True, which="major", axis="x", lw=0.3, alpha=0.4)
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    return str(out_png)


# ---------------------------------------------------------------- render

def render_md(result):
    """v2 详细报告(全指标), 写 report_latest.md 供追溯, 不进卡片."""
    L = [f"# 亚洲四地杠杆/拥挤度监控 — {result['generated_date']}", ""]
    heads = []
    for m in ("cn", "kr", "jp", "tw"):
        b = result["markets"][m]
        state = b.get("delever", {}).get("state", "n/a") if b.get("delever") else "n/a"
        comp = b["composite"] if b["composite"] is not None else "n/a"
        heads.append(f"{b['market']} 拥挤{comp}·{state}")
    L.append("**总览(拥挤度=存量三视角加权):** " + " | ".join(heads))
    L.append("")
    for m in ("cn", "kr", "jp", "tw"):
        b = result["markets"][m]
        dl = b.get("delever")
        bucket = crowding_bucket(b["composite"])
        state = dl["state"] if dl else "n/a"
        L.append(f"## {b['market']} — 拥挤度{bucket}({b['composite']}) · {state}")
        if dl:
            L.append(f"- 去杠杆追踪(主杠杆存量): {dl['line']}")
        for i in b["indicators"]:
            if i.get("tier") != 1:
                continue
            if not i.get("available"):
                L.append(f"- {i['label']}: ⚠️ {i.get('note', '不可得')}")
                continue
            p = f"1y分位 {i['pct_1y']}%" if i["pct_1y"] is not None else "分位样本不足"
            stale = " ⚠️stale" if i["stale"] else ""
            note = f" — {i['note']}" if i["note"] else ""
            L.append(f"- {i['label']}: {i['value_fmt']}{i['unit']} "
                     f"({fmt_chg(i)}; {p}; 至{i['asof']}{stale}){note}")
        for f in b["flows"]:
            lab = ("1w", "4w") if f.get("weekly") else ("5d", "10d")
            c5 = f"{f['cum5']:+,.1f}" if f["cum5"] is not None else "n/a"
            c10 = f"{f['cum10']:+,.1f}" if f["cum10"] is not None else "n/a"
            L.append(f"- {f['label']}: {lab[0]} {c5} / {lab[1]} {c10} {f['unit']}")
        for s in b["stress"]:
            if isinstance(s, str):
                L.append(f"- 情绪/压力: {s}")
            elif s.get("available"):
                p = f"1y分位 {s['pct_1y']}%" if s["pct_1y"] is not None else "分位样本不足"
                L.append(f"- 情绪/压力 {s['label']}: {s['value_fmt']}{s['unit']} "
                         f"({fmt_chg(s)}; {p}; 至{s['asof']})")
        tier2 = []
        for i in b["indicators"]:
            if i.get("tier") != 2 or not i.get("available"):
                continue
            arrow = ""
            if i.get("chg10") is not None:
                arrow = "↑" if i["chg10"] > 0 else ("↓" if i["chg10"] < 0 else "→")
            p = f"分位{i['pct_1y']:.0f}" if i["pct_1y"] is not None else "分位n/a"
            stale = "⚠️" if i["stale"] else ""
            tier2.append(f"{i['label']} {i['value_fmt']}{i['unit']}{arrow}({p}){stale}")
        if tier2:
            L.append("- 次要指标: " + " | ".join(tier2))
        L.append("")
    L.append("说明: 拥挤度综合分=主杠杆存量三视角加权(水平1y分位40%+趋势偏离3y分位40%"
             "+冻结分母市值比3y分位20%), 详见卡片口径; 各指标分位=过去1年(日频252/周频52"
             "个观测)中的位置, 越高越拥挤(P/C比、期指贴水、转债双低已按方向反转), "
             "跨周期比较只用去杠杆事件的相对深度/速度。"
             "去杠杆追踪: 主杠杆存量(CN融资余额/KR信用融资/JP信用买残周频/TW融资余额)"
             "距滚动1y峰值的回撤; 浅度/深度出清=历史事件按回撤深度-20%分层。"
             "核心/次要分层依据2026-08-04事件回测(indicator_study.md)。"
             "CN Gross Leverage/量化中性拥挤度/Unwind压力本身已是0-100模型分。"
             "数据只读各 leverage-monitor 历史库, 不含盘中值。")
    return "\n".join(L)


def stress_extremes(block):
    """极端压力读数(1y分位>=90或<=10)一句话; 无极端返回None."""
    hits = []
    for s in block["stress"]:
        if isinstance(s, str) or not s.get("available") or s.get("pct_1y") is None:
            continue
        if s["pct_1y"] >= 90 or s["pct_1y"] <= 10:
            hits.append(f"{s['label']} {s['value_fmt']}{s['unit']}(1y分位{s['pct_1y']:.0f})")
    return "、".join(hits) if hits else None


def render_card_md(result, chart_paths):
    """v3 verbal 卡片: 每市场=定性判断+去杠杆进度+图, 供飞书卡片直接使用."""
    L = [f"# 亚洲四地杠杆/拥挤度监控 — {result['generated_date']}", ""]
    for m in ("cn", "kr", "jp", "tw"):
        b = result["markets"][m]
        dl = b.get("delever")
        bucket = crowding_bucket(b["composite"])
        state = dl["state"] if dl else "n/a"
        comp = f"{b['composite']:.0f}" if b["composite"] is not None else "n/a"
        L.append(f"## {FLAGS[m]} {b['market']} — 拥挤度 {comp}/100({bucket}) · {state}")
        ev = crowd_evidence(b)
        if ev:
            L.append(f"**拥挤度判断依据**: {ev}")
        core = evidence_line(b)
        if core:
            L.append(f"**核心指标**: {core}")
        if dl:
            L.append(f"**去杠杆进度**: {dl['line']}")
        if b.get("quant_line"):
            L.append(f"**A股量化状态**: {b['quant_line']}")
        ext = stress_extremes(b)
        if ext:
            L.append(f"**极端信号**: {ext}")
        # 数据滞后提示
        stale_labels = [i["label"] for i in b["indicators"]
                        if i.get("available") and i.get("stale") and i.get("tier") == 1]
        if stale_labels:
            L.append(f"⚠️ 数据滞后: {'、'.join(stale_labels)}")
        cp = chart_paths.get(m)
        if cp:
            L.append(f"IMG:{cp}")
        L.append("")
    L.append("## 口径说明")
    L.append("拥挤度=主杠杆存量三视角加权(0-100): 余额水平滚动1y分位40% + 余额较200日"
             "(周频40周)均线趋势偏离滚动3y分位40% + 余额/市值滚动3y分位20%(分母用120日"
             "冻结均值, 避免崩盘中市值先崩把比率被动推高)。只用存量成分, 换手率/融资买入"
             "占比/杠杆ETF份额等活动量指标因崩盘中被动飙升不进分数。≥75高位/55-75偏热"
             "/35-55中性/<35低位, 与图中红线同一序列。定位是燃料表: 高分=出清空间大, "
             "不是卖出时点; 时点看去杠杆追踪(主杠杆存量距滚动1y峰值回撤, 对标历史出清"
             "事件, 浅度=回撤<20%/深度=≥20%; 图中红色阴影为历史事件区间)。JP为周频; "
             "TW数据仅自2021。数据只读各监控历史库, 不含盘中值。")
    return "\n".join(L)


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--charts", action="store_true",
                    help="生成历史图PNG并输出verbal卡片markdown")
    args = ap.parse_args()
    result = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
              "generated_date": TODAY.isoformat(),
              "markets": {"cn": build_cn(), "kr": build_kr(),
                          "jp": build_jp(), "tw": build_tw()}}
    # 拥挤度综合分: 存量三视角加权历史序列末值(与图同源)
    comp_hists = {}
    for m, b in result["markets"].items():
        cw = b.get("_crowd") or {}
        ch, last_comp = crowding_history(cw.get("bal"), cw.get("denom"),
                                         weekly=cw.get("weekly", False))
        comp_hists[m] = ch
        b["composite"] = round(float(ch.iloc[-1]), 1) if ch is not None else None
        b["crowd_components"] = last_comp
        b["crowd_denom_label"] = cw.get("denom_label")
    # 拥挤度综合分的完整历史落盘: 每次运行都由全量历史重算, 所以这份 CSV 一次成型,
    # 不需要靠逐日快照攒。看板的 1W/1M 位移直接读它, 免得再复制一遍加权分位逻辑。
    hist_df = pd.DataFrame({m: ch for m, ch in comp_hists.items() if ch is not None})
    if not hist_df.empty:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        hist_df = hist_df.sort_index().round(2)
        hist_df.index.name = "date"
        hist_df.to_csv(OUT_DIR / "composite_history.csv")
    chart_paths = {}
    if args.charts:
        _setup_cjk_font()
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        for m, b in result["markets"].items():
            try:
                p = make_chart(m, b, comp_hists[m], CHART_DIR / f"{m}_latest.png")
                if p:
                    chart_paths[m] = p
            except Exception as e:
                print(f"[warn] {m} chart failed: {e}", file=sys.stderr)
    card_md = render_card_md(result, chart_paths)
    result_clean = _clean(result)
    md = render_md(result_clean)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(
        json.dumps(result_clean, ensure_ascii=False, indent=1, allow_nan=False))
    (OUT_DIR / "report_latest.md").write_text(md)
    (OUT_DIR / f"report_{TODAY.isoformat()}.md").write_text(md)
    (OUT_DIR / "card_latest.md").write_text(card_md)
    (OUT_DIR / f"card_{TODAY.isoformat()}.md").write_text(card_md)
    if args.json:
        print(json.dumps(result_clean, ensure_ascii=False, allow_nan=False))
    elif args.charts:
        print(card_md)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
