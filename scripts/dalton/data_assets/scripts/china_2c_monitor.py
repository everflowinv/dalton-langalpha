#!/usr/bin/env python3
"""
中国2C消费/互联网 止跌信号 + 买卖点监控器
每日运行，检测：
1. 止跌信号（RSI底背离、MACD金叉、均线突破等）
2. 买点提醒（中间确认条件三选二：RSI回升到35+、MACD日线金叉、RSI底背离）
3. 卖点提醒（趋势反转、超买衰减、均线破位）

2026-08-27 改成「只报事件、不报状态」：
- 每个信号有一个 key，只在它从「不成立」翻到「成立」的那一天报一次；持续成立不再重复。
  状态型 key（周线金叉/死叉、背离、超买/超卖）另加冷却期。
- 盘中 cron 只用前一个已完整收盘的日线；周线只用最近一个完整周，刚确认上穿/下穿才报。
- 一只票只出现一行：买点 > 止跌 > 转弱，另一侧的信号写在同一行末尾。
- 每日硬上限 8 只：买点 <=3、止跌 <=2、转弱 <=3；超出的只报数量。
- 首次见到的标的只静默建基线，不把存量状态当新事件。
- 「仍在50MA下方」的周度回顾只在周一附，最多列 10 只（--weekly 可强制）。
- --asof YYYY-MM-DD 用历史截面回放；--state-file 指定状态文件，回放时不污染线上状态。
"""

import warnings as _w; _w.filterwarnings('ignore')  # suppress NotOpenSSLWarning etc. before urllib3 import
import yfinance as yf
import pandas as pd
import numpy as np
import json, sys, os
from datetime import datetime, date
from zoneinfo import ZoneInfo
import warnings
warnings.filterwarnings('ignore')

import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--market', choices=['hk', 'us', 'all'], default='all')
parser.add_argument('--asof', default=None, help='YYYY-MM-DD，只用这一天及之前的收盘做回放')
parser.add_argument('--state-file', default=None, help='状态文件路径（默认 <workspace>/state/china_2c_monitor_state.json）')
parser.add_argument('--universe-dir', default=None, help='股票池目录（默认读取 DALTON_DATA_TASK_CHINA_2C_UNIVERSE_DIR 或 <workspace>/state）')
parser.add_argument('--output-file', default=None, help='最新报告输出路径（默认 <workspace>/temp/china_2c_monitor_latest.txt）')
parser.add_argument('--weekly', action='store_true', help='强制附上周度回顾与口径（默认只在周一附）')
parser.add_argument('--initialize-only', action='store_true',
                    help='静默重建当前信号基线，清掉对应标的旧事件冷却，不生成告警')
parser.add_argument('--cache-dir', default=os.environ.get('CHINA_2C_CACHE_DIR'),
                    help='行情缓存目录（回放用；有缓存就不重新下载）')
args, _ = parser.parse_known_args()


def _resolve_workspace() -> str:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return os.getcwd()


WS = _resolve_workspace()
UNIVERSE_DIR = (
    args.universe_dir
    or (os.environ.get("DALTON_DATA_TASK_CHINA_2C_UNIVERSE_DIR") or "").strip()
    or os.path.join(WS, "state")
)
if args.market == 'hk':
    uf = os.path.join(UNIVERSE_DIR, "china_2c_universe_hk.json")
elif args.market == 'us':
    uf = os.path.join(UNIVERSE_DIR, "china_2c_universe_us.json")
else:
    uf = os.path.join(UNIVERSE_DIR, "china_2c_universe.json")

if os.path.exists(uf):
    with open(uf) as f:
        UNIVERSE = json.load(f)
else:
    UNIVERSE = {}

STATE_FILE = args.state_file or os.path.join(WS, "state", "china_2c_monitor_state.json")
OUT_FILE = args.output_file or os.path.join(WS, "temp", "china_2c_monitor_latest.txt")
ASOF = date.fromisoformat(args.asof) if args.asof else None
MARKET_TZ = ZoneInfo("Asia/Hong_Kong" if args.market == "hk" else "America/New_York")
TODAY = ASOF or datetime.now(MARKET_TZ).date()

BUY_CAP = 3
STOP_CAP = 2
SELL_CAP = 3
WEEKLY_LINGERING_CAP = 10

STOP_PRIORITY = {
    "break200": 100, "engulf": 90, "hammer": 85, "break50": 80,
    "macd_golden": 70, "weekly_golden": 65, "rsi_recover": 55,
    "divergence": 50, "oversold": 45, "break200_lowvol": 35,
    "break50_lowvol": 25,
}
SELL_PRIORITY = {
    "ma200_break": 100, "weekly_dead": 90, "three_day_low": 85,
    "below50_5d": 80, "upper_shadow": 70, "rsi_overbought": 60,
    "top_div": 50, "ma20_break": 40,
}

# 状态型信号的冷却期（日历日）。翻转到成立当天报一次，冷却期内即使中途失效再成立也不重报。
# 未列出的 key（单日 K 线形态、突破/跌破当日）只做翻转去重，不设冷却。
COOLDOWN_DAYS = {
    "rsi_recover": 10,
    "divergence": 20,
    "macd_golden": 5,
    "weekly_golden": 21,
    "weekly_dead": 21,
    "rsi_overbought": 10,
    "top_div": 20,
    "oversold": 10,
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
    else:
        s = {}
    s.setdefault("buy_signals_sent", {})
    s.setdefault("events_sent", {})     # ticker -> {key: 上次报出的日期}
    s.setdefault("events_active", {})   # ticker -> [上次运行时成立的 key]
    # 旧状态已有 events_active 的标的视为已建基线；新标的首次扫描静默初始化。
    s.setdefault("initialized_tickers", sorted(s["events_active"].keys()))
    return s


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_history(ticker):
    """yfinance 2y 日线；--cache-dir 有缓存时直接读，回放不重复下载。"""
    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)
        p = os.path.join(args.cache_dir, f"{ticker}.pkl")
        if os.path.exists(p):
            return pd.read_pickle(p)
        df = yf.Ticker(ticker).history(period="2y")
        df.to_pickle(p)
        return df
    return yf.Ticker(ticker).history(period="2y")


def calc_rsi_series(close, period=14):
    """Wilder 递归平滑 RSI（SMA 种子），与 TradingView / 雪球 / Bloomberg 同口径。

    2026-08-17 由简单移动平均（Cutler）口径改为 Wilder，与 sentiment-dashboard /
    kweb-dashboard 统一；超买超卖阈值 70/80/85/90 不变。
    """
    values = pd.to_numeric(close, errors="coerce")
    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= period:
        return out
    avg_gain = float(gain.iloc[1:period + 1].mean())
    avg_loss = float(loss.iloc[1:period + 1].mean())

    def _rsi(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out.iloc[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + float(gain.iloc[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(loss.iloc[i])) / period
        out.iloc[i] = _rsi(avg_gain, avg_loss)
    # 停牌/无成交：整个窗口价格不动时，Wilder 会拖着停牌前的动能给出假信号，置空
    out[delta.abs().rolling(period).sum() == 0] = np.nan
    return out


def fmt_rsi(meta):
    """停牌标的 RSI 置空，报告里写「停牌」而不是 nan。"""
    v = meta.get("rsi")
    return "停牌" if v is None else v


def check_stock(ticker, name):
    """分析单只股票。

    返回 (stop_events, buy_conditions, sell_events, meta)
    stop_events / sell_events 是 [(key, text)]，key 用于跨日去重。
    """
    stop_events = []
    sell_events = []
    buy_conditions = []

    try:
        df = load_history(ticker)
        if ASOF is not None and not df.empty:
            df = df[df.index.date <= ASOF]
        elif not df.empty:
            # HK/US cron 都在当地市场盘中运行；丢掉当日 partial bar。
            df = df[df.index.date < TODAY]
        if df.empty or len(df) < 60:
            return stop_events, buy_conditions, sell_events, {}

        c = df['Close']; v = df['Volume']
        cur = c.iloc[-1]
        prev_close = c.iloc[-2]
        ma50_series = c.rolling(50).mean()
        ma50 = ma50_series.iloc[-1]
        ma200 = c.rolling(200).mean().iloc[-1] if len(c) >= 200 else np.nan
        last_bar = df.index[-1].date()

        # RSI
        rsi_series = calc_rsi_series(c)
        rsi_now = rsi_series.iloc[-1]

        # MACD
        e12 = c.ewm(span=12, adjust=False).mean()
        e26 = c.ewm(span=26, adjust=False).mean()
        ml = e12 - e26
        sig = ml.ewm(span=9, adjust=False).mean()
        macd_golden = ml.iloc[-1] > sig.iloc[-1]
        macd_prev = ml.iloc[-2] <= sig.iloc[-2] if len(ml) > 2 else False

        # Weekly MACD：W-FRI 统一周期，只保留已到周五标签的完整周。
        c_weekly = c.resample('W-FRI').last().dropna()
        c_weekly = c_weekly[c_weekly.index.date <= last_bar]
        weekly_macd_golden = False
        weekly_golden_cross = False
        weekly_dead_cross = False
        if len(c_weekly) >= 30:
            we12 = c_weekly.ewm(span=12, adjust=False).mean()
            we26 = c_weekly.ewm(span=26, adjust=False).mean()
            wml = we12 - we26
            wsig = wml.ewm(span=9, adjust=False).mean()
            weekly_macd_golden = wml.iloc[-1] > wsig.iloc[-1]
            weekly_golden_cross = weekly_macd_golden and wml.iloc[-2] <= wsig.iloc[-2]
            weekly_dead_cross = (not weekly_macd_golden) and wml.iloc[-2] >= wsig.iloc[-2]

        # Volume
        vol_20avg = v.iloc[-20:].mean()
        vol_ratio = v.iloc[-1] / vol_20avg if vol_20avg > 0 else 1

        # Price action
        today_open = df['Open'].iloc[-1]
        today_high = df['High'].iloc[-1]
        today_low = df['Low'].iloc[-1]
        lower_shadow = min(today_open, cur) - today_low
        total_range = today_high - today_low

        chg_30d = (cur / c.iloc[-23] - 1) * 100 if len(c) > 23 else 0

        # ============ 底背离检测 ============
        divergence = False
        if len(c) > 40:
            price_min_20d = c.iloc[-20:].min()
            price_min_40d = c.iloc[-40:-20].min()
            rsi_min_20d = rsi_series.iloc[-20:].min()
            rsi_min_40d = rsi_series.iloc[-40:-20].min()
            if price_min_20d < price_min_40d and rsi_min_20d > rsi_min_40d:
                divergence = True

        # ============ RSI超卖回升检测 ============
        rsi_min_recent = rsi_series.iloc[-20:].min() if len(rsi_series) >= 20 else rsi_now

        # ============ 买点条件（中间确认，三选二）============
        # 条件1: RSI从<20回升到35+，且伴随放量（量比 > 1.1x），过滤缩量诱多
        cond_rsi = rsi_min_recent < 20 and rsi_now >= 35
        if cond_rsi and vol_ratio > 1.1:
            buy_conditions.append("RSI放量回升35+")
        # 条件2: MACD日线金叉
        if macd_golden:
            buy_conditions.append("MACD日线金叉")
        # 条件3: RSI底背离
        if divergence:
            buy_conditions.append("RSI底背离")

        # ============ 止跌信号（事件）============

        if rsi_min_recent < 25 and rsi_now > 30:
            stop_events.append(("rsi_recover", f"📈 RSI超卖回升: 近20日最低{rsi_min_recent:.0f} → 现{rsi_now:.0f}"))

        if divergence:
            stop_events.append(("divergence", "🔄 RSI底背离"))

        if macd_golden and macd_prev:
            stop_events.append(("macd_golden", "⚡ MACD日线金叉 (今日)"))
        elif macd_golden:
            recent_cross = any(ml.iloc[-i] <= sig.iloc[-i] for i in range(2, min(6, len(ml))))
            if recent_cross:
                stop_events.append(("macd_golden", "⚡ MACD日线金叉 (近5日)"))

        if weekly_golden_cross:
            stop_events.append(("weekly_golden", "🌟 周线MACD金叉（最近完整周确认）"))

        # 均线突破 (区分真假突破)
        if cur > ma50 and prev_close <= ma50:
            if vol_ratio > 1.2:
                stop_events.append(("break50", f"🚀 放量突破50MA! ({ma50:.2f}, 量比{vol_ratio:.1f}x)"))
            else:
                stop_events.append(("break50_lowvol", f"⚠️ 缩量突破50MA(诱多嫌疑) ({ma50:.2f}, 量比{vol_ratio:.1f}x)"))
        if not np.isnan(ma200) and cur > ma200 and prev_close <= ma200:
            if vol_ratio > 1.2:
                stop_events.append(("break200", f"🚀 放量突破200MA! ({ma200:.2f}, 量比{vol_ratio:.1f}x)"))
            else:
                stop_events.append(("break200_lowvol", f"⚠️ 缩量突破200MA(诱多嫌疑) ({ma200:.2f}, 量比{vol_ratio:.1f}x)"))

        if total_range > 0 and lower_shadow / total_range > 0.6 and vol_ratio > 1.5 and cur > today_open:
            stop_events.append(("hammer", f"🔨 放量长下影线 (量比={vol_ratio:.1f}x)"))

        if len(df) >= 2:
            prev_o = df['Open'].iloc[-2]; prev_c = df['Close'].iloc[-2]
            if prev_c < prev_o and cur > today_open and cur > prev_o and today_open < prev_c and vol_ratio > 1.3:
                stop_events.append(("engulf", f"🔥 放量阳包阴! (量比={vol_ratio:.1f}x)"))

        # ============ 转弱信号（事件）============

        # 1. 连续第5个交易日收在当日50MA之下 → 趋势降级。
        below50_since = None
        below50_streak = 0
        if len(c) >= 55:
            for i in range(1, len(c) + 1):
                if pd.notna(ma50_series.iloc[-i]) and c.iloc[-i] < ma50_series.iloc[-i]:
                    below50_streak += 1
                else:
                    break
            if below50_streak >= 5:
                sell_events.append(("below50_5d", "⚠️ 连续第5日收于50MA下（趋势降级）"))
        if cur < ma50:
            above = c[c >= ma50_series]
            if len(above):
                pos = c.index.get_loc(above.index[-1])
                if pos + 1 < len(c):
                    below50_since = c.index[pos + 1].date()

        # 2. 周线MACD死叉
        if weekly_dead_cross:
            sell_events.append(("weekly_dead", "💀 周线MACD死叉（最近完整周确认）"))

        # 3. 放量长上影线（冲高回落>5%）
        if total_range > 0:
            upper_shadow = today_high - max(today_open, cur)
            if upper_shadow / total_range > 0.6 and vol_ratio > 1.5:
                intraday_drop = (today_high - cur) / today_high * 100
                if intraday_drop > 5:
                    sell_events.append(("upper_shadow", f"🔻 放量长上影线 (冲高回落{intraday_drop:.1f}%, 量比={vol_ratio:.1f}x)"))

        # 4. RSI>70 + MACD死叉/空头
        if rsi_now > 70 and not macd_golden:
            sell_events.append(("rsi_overbought", f"🔴 RSI超买({rsi_now:.0f}) + MACD死叉/空头"))

        # 5. 跌破20日均线 + 缩量
        ma20 = c.rolling(20).mean().iloc[-1]
        if cur < ma20 and prev_close >= ma20 and vol_ratio < 0.8:
            sell_events.append(("ma20_break", f"📉 跌破20MA({ma20:.2f}) + 缩量(量比={vol_ratio:.1f}x)"))

        # 6. 连续3日收盘低于前低
        if len(c) >= 5:
            recent_low = c.iloc[-5:-2].min()
            if all(c.iloc[-i] < recent_low for i in range(1, 4)):
                sell_events.append(("three_day_low", f"📉 连续3日破前低({recent_low:.2f})"))

        # 7. 顶背离
        top_divergence = False
        if len(c) > 40:
            price_max_20d = c.iloc[-20:].max()
            price_max_40d = c.iloc[-40:-20].max()
            rsi_max_20d = rsi_series.iloc[-20:].max()
            rsi_max_40d = rsi_series.iloc[-40:-20].max()
            if price_max_20d > price_max_40d and rsi_max_20d < rsi_max_40d:
                top_divergence = True
                sell_events.append(("top_div", "🔄 RSI顶背离（价格新高但动量衰减）"))

        # 8. 跌破200MA
        if not np.isnan(ma200) and cur < ma200 and prev_close >= ma200:
            sell_events.append(("ma200_break", f"💀 跌破200MA!({ma200:.2f})"))

        meta = {
            "price": round(cur, 2),
            "rsi": None if pd.isna(rsi_now) else round(float(rsi_now), 1),
            "ma50": round(ma50, 2),
            "ma200": round(ma200, 2) if not np.isnan(ma200) else None,
            "macd_d": macd_golden,
            "macd_w": weekly_macd_golden,
            "divergence": divergence,
            "chg_30d": round(chg_30d, 1),
            "above_50ma": cur > ma50,
            "above_200ma": (not np.isnan(ma200)) and cur > ma200,
            "below50_since": below50_since.isoformat() if below50_since else None,
            "below50_streak": below50_streak,
            "buy_conds": len(buy_conditions),
            "buy_conds_list": buy_conditions,
            "top_divergence": top_divergence,
            "last_bar": last_bar.isoformat(),
        }
        return stop_events, buy_conditions, sell_events, meta

    except Exception as e:
        return [], [], [], {"error": str(e)}


def is_new_event(state, ticker, key):
    """只报翻转：上次运行不成立、这次成立；状态型 key 再看冷却期。"""
    if key in state["events_active"].get(ticker, []):
        return False
    last = state["events_sent"].get(ticker, {}).get(key)
    if last:
        try:
            if (TODAY - date.fromisoformat(last)).days < COOLDOWN_DAYS.get(key, 0):
                return False
        except ValueError:
            pass
    return True


def fmt_row(name, ticker, meta, main_str, other_label=None, other_str=None):
    price = meta.get('price', '?')
    rsi = fmt_rsi(meta)
    chg = meta.get('chg_30d', '?')
    line = f"  **{name}({ticker})** 价格={price} RSI={rsi} 30D={chg}% → {main_str}"
    if other_str:
        line += f" ｜ {other_label}: {other_str}"
    return line


def event_priority(keys, weights):
    return max((weights.get(key, 0) for key in keys), default=0)


def main():
    now = TODAY.isoformat() if ASOF else datetime.now(MARKET_TZ).strftime("%Y-%m-%d %H:%M")
    state = load_state()
    prev_buy_sent = dict(state.get("buy_signals_sent", {}))
    new_buy_sent = dict(prev_buy_sent)
    initialized_tickers = set(state.get("initialized_tickers", []))
    weekly_block = args.weekly or TODAY.weekday() == 0

    buy_candidates = []  # (ticker, name, conditions, meta)
    new_stop = {}        # ticker -> [text]
    new_sell = {}        # ticker -> [text]
    new_stop_keys = {}   # ticker -> [key]
    new_sell_keys = {}   # ticker -> [key]
    scan_results = {}
    new_active = {}
    reported_keys = {}   # 包含因上限未展开的新事件，避免次日重报
    n_active_stop = n_active_sell = 0
    baseline_count = 0

    for ticker, name in UNIVERSE.items():
        stop_ev, buy_conds, sell_ev, meta = check_stock(ticker, name)
        scan_results[ticker] = {"name": name, "buy_conds": buy_conds, "meta": meta,
                                "stop_keys": [k for k, _ in stop_ev], "sell_keys": [k for k, _ in sell_ev]}
        if not meta.get("last_bar"):
            continue
        if stop_ev:
            n_active_stop += 1
        if sell_ev:
            n_active_sell += 1

        active_keys = [k for k, _ in stop_ev] + [k for k, _ in sell_ev]
        rsi = meta.get("rsi")
        if rsi is not None and rsi < 25:
            active_keys.append("oversold")
        active_keys = list(dict.fromkeys(active_keys))
        new_active[ticker] = active_keys

        baseline_only = args.initialize_only or ticker not in initialized_tickers
        if baseline_only:
            initialized_tickers.add(ticker)
            baseline_count += 1
            if len(buy_conds) >= 2:
                new_buy_sent[ticker] = buy_conds
            else:
                new_buy_sent.pop(ticker, None)
            continue

        for key, text in stop_ev:
            if is_new_event(state, ticker, key):
                new_stop.setdefault(ticker, []).append(text)
                new_stop_keys.setdefault(ticker, []).append(key)
                reported_keys.setdefault(ticker, []).append(key)
        for key, text in sell_ev:
            if is_new_event(state, ticker, key):
                new_sell.setdefault(ticker, []).append(text)
                new_sell_keys.setdefault(ticker, []).append(key)
                reported_keys.setdefault(ticker, []).append(key)
        if rsi is not None and rsi < 25 and is_new_event(state, ticker, "oversold"):
            new_stop.setdefault(ticker, []).append(f"🟢 RSI极度超卖({rsi:.0f})")
            new_stop_keys.setdefault(ticker, []).append("oversold")
            reported_keys.setdefault(ticker, []).append("oversold")

        # 买点也按「不成立 -> 成立/条件组合变化」触发。
        if len(buy_conds) >= 2:
            prev_conds = prev_buy_sent.get(ticker, [])
            if set(buy_conds) != set(prev_conds):
                buy_candidates.append((ticker, name, buy_conds, meta))
            new_buy_sent[ticker] = buy_conds
        else:
            new_buy_sent.pop(ticker, None)

    # 更新每个标的的当前状态，但不动本次市场之外的 ticker。
    events_active = state["events_active"]
    for ticker, keys in new_active.items():
        if keys:
            events_active[ticker] = keys
        else:
            events_active.pop(ticker, None)

    if args.initialize_only:
        # 显式基线重建用于修复/迁移：当前 active 状态足以防重，
        # 清掉本市场旧冷却，避免错误历史压住后续真正的新翻转。
        events_sent = state["events_sent"]
        for ticker in new_active:
            events_sent.pop(ticker, None)
        state["last_run"] = now
        state["buy_signals_sent"] = new_buy_sent
        state["events_sent"] = events_sent
        state["events_active"] = events_active
        state["initialized_tickers"] = sorted(initialized_tickers)
        save_state(state)
        print(f"基线初始化完成：{args.market} {baseline_count} 只，未生成告警。")
        return 0

    # 一只票只进一个栏目：买点 > 止跌 > 转弱。各栏再按信号硬度排序。
    buy_candidates.sort(key=lambda x: (-len(x[2]), not x[3].get("above_50ma", False), x[0]))
    buy_candidate_tickers = {t for t, *_ in buy_candidates}
    buy_alerts = buy_candidates[:BUY_CAP]

    stop_all = [(t, scan_results[t]["name"], texts, scan_results[t]["meta"])
                for t, texts in new_stop.items() if t not in buy_candidate_tickers]
    stop_all.sort(key=lambda x: (-event_priority(new_stop_keys.get(x[0], []), STOP_PRIORITY),
                                 -len(x[2]), x[0]))
    stop_candidate_tickers = {t for t, *_ in stop_all}
    stop_rows = stop_all[:STOP_CAP]

    sell_all = [(t, scan_results[t]["name"], texts, scan_results[t]["meta"])
                for t, texts in new_sell.items()
                if t not in buy_candidate_tickers and t not in stop_candidate_tickers]
    sell_all.sort(key=lambda x: (-event_priority(new_sell_keys.get(x[0], []), SELL_PRIORITY),
                                 -len(x[2]), x[0]))
    sell_rows = sell_all[:SELL_CAP]

    omitted_buy = max(0, len(buy_candidates) - len(buy_alerts))
    omitted_stop = max(0, len(stop_all) - len(stop_rows))
    omitted_sell = max(0, len(sell_all) - len(sell_rows))
    omitted_total = omitted_buy + omitted_stop + omitted_sell

    # ============ 输出 ============
    L = []
    L.append(f"中国消费与互联网技术观察｜{now}")
    bar_dates = sorted({info["meta"].get("last_bar") for info in scan_results.values()
                        if info["meta"].get("last_bar")})
    bar_note = bar_dates[-1] if bar_dates else "N/A"
    L.append(f"覆盖 {len(UNIVERSE)} 只｜数据截至 {bar_note} 收盘｜展示/新触发："
             f"买点 {len(buy_alerts)}/{len(buy_candidates)}、"
             f"止跌 {len(stop_rows)}/{len(stop_all)}、转弱 {len(sell_rows)}/{len(sell_all)}")
    if baseline_count:
        L.append(f"新纳入 {baseline_count} 只已静默建基线，未把存量信号当成新事件。")
    L.append("")

    if not (buy_alerts or stop_rows or sell_rows):
        L.append("今天没有需要推送的新事件。")
        L.append("")

    # === 1. 买点提醒（最多3只，每只一行）===
    if buy_alerts:
        L.append(f"**买点观察：{len(buy_alerts)} 只**")
        L.append("")
        for ticker, name, conds, meta in buy_alerts:
            ma50 = meta.get('ma50', '?')
            above_50 = "✅站上50MA" if meta.get('above_50ma') else f"❌未到50MA({ma50})"
            extra = []
            if ticker in new_stop:
                extra.append("止跌: " + " | ".join(new_stop[ticker]))
            if ticker in new_sell:
                extra.append("转弱: " + " | ".join(new_sell[ticker]))
            L.append(fmt_row(name, ticker, meta,
                             f"🎯 {' + '.join(conds)} | {above_50}",
                             "同日信号", " ｜ ".join(extra) if extra else None))
        L.append("")

    # === 2. 止跌信号（最多2只）===
    if stop_rows:
        L.append(f"📈 **止跌信号（当日新触发）：{len(stop_rows)} 只**")
        L.append("")
        for ticker, name, texts, meta in stop_rows:
            other = " | ".join(new_sell[ticker]) if ticker in new_sell else None
            L.append(fmt_row(name, ticker, meta, " | ".join(texts), "同日转弱", other))
        L.append("")

    # === 3. 转弱观察（最多3只）===
    if sell_rows:
        L.append(f"**转弱观察（当日新触发）：{len(sell_rows)} 只**")
        L.append("")
        for ticker, name, texts, meta in sell_rows:
            L.append(fmt_row(name, ticker, meta, " | ".join(texts)))
        L.append("")

    if omitted_total:
        L.append(f"另有 {omitted_total} 只普通技术变化未展开（买点 {omitted_buy}、"
                 f"止跌 {omitted_stop}、转弱 {omitted_sell}）。")
        L.append("")

    # === 4. 周度回顾（周一，最多10只）===
    if weekly_block:
        L.append("---")
        L.append("**周度回顾**")
        L.append("")
        lingering = []
        for t, info in scan_results.items():
            m = info["meta"]
            since = m.get("below50_since")
            if since and not m.get("above_50ma"):
                days = (TODAY - date.fromisoformat(since)).days
                if days >= 7:
                    ma50 = m.get("ma50")
                    price = m.get("price")
                    deviation = ((price / ma50) - 1) * 100 if ma50 and price is not None else 0
                    lingering.append((deviation, days, info["name"], t))
        if lingering:
            lingering.sort(key=lambda x: (x[0], -x[1]))
            L.append(f"仍在50MA下方超过一周：共 {len(lingering)} 只，"
                     f"只列偏离最大的 {min(len(lingering), WEEKLY_LINGERING_CAP)} 只。")
            for deviation, days, name, ticker in lingering[:WEEKLY_LINGERING_CAP]:
                L.append(f"  **{name}({ticker})** 低于50MA {abs(deviation):.1f}% | 已持续 {days} 天")
        else:
            L.append("没有标的在50MA下方持续超过一周。")
        L.append("")
    L.append("---")
    L.append("只报收盘后首次成立的事件；存量状态不重复。以上为规则筛选，不是独立买卖建议。")

    output = "\n".join(L)
    print(output)

    # ============ 保存状态 ============
    events_sent = state["events_sent"]
    for t, keys in reported_keys.items():
        d = events_sent.setdefault(t, {})
        for k in keys:
            d[k] = TODAY.isoformat()

    state["last_run"] = now
    state["alerts_count"] = len(stop_rows)
    state["active_stop_count"] = n_active_stop
    state["active_sell_count"] = n_active_sell
    state["buy_alerts_count"] = len(buy_alerts)
    state["sell_alerts_count"] = len(sell_rows)
    state["buy_signals_sent"] = new_buy_sent
    state["events_sent"] = events_sent
    state["events_active"] = events_active
    state["initialized_tickers"] = sorted(initialized_tickers)
    state["omitted_count"] = omitted_total
    state.pop("alert_tickers", None)
    save_state(state)

    os.makedirs(os.path.dirname(os.path.abspath(OUT_FILE)), exist_ok=True)
    with open(OUT_FILE, 'w') as f:
        f.write(output)

    return 0 if (buy_alerts or stop_rows or sell_rows) else 1


if __name__ == "__main__":
    sys.exit(main())
