#!/usr/bin/env python3
"""
e-watch - v23 自选池技术观察

两种运行模式：
  daily    每天 08:00 HKT 全量报告，输出 JSON（messages 数组 + 附件全文），由 cron agent 转发到 Discord。
           同时重置并写入当日信号 state，作为盘中增量的基准。
  intraday 盘中增量扫描，只输出「相对 daily 基准新出现」的信号。纯文本输出，
           无新信号时打印 NO_REPLY（cron command payload 会静默）。
           2026-11-12 用户要求停掉盘中提醒（股票池扩到 362 只），只发每天的早报；代码保留。

信号口径与 scripts/pt_watchlist_monitor.py 保持一致：
MA20/50/200、RSI14、MACD 日线与周线、底背离、量比，强势股动态分层。
在此基础上增加「正股机会」分档，与 Sell Put 分档相互独立。
股票池从 state/e_watch_universe.json 读取，加减票改 JSON 不改代码。
"""

import warnings as _w; _w.filterwarnings('ignore')
import yfinance as yf
import logging
# yfinance 把单只下载失败写到 stderr；cron 见到 stderr 会连同 NO_REPLY 一起推送。失败的票下面会逐只重试。
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
import pandas as pd
import numpy as np
import json, os, re, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


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


def resolve_discord_target() -> str:
    key = "DALTON_DATA_TASK_DISCORD_DM_TARGET"
    env_val = (os.environ.get(key) or "").strip()
    if env_val:
        return env_val
    cfg_override = (os.environ.get("DALTON_DATA_TASKS_CONFIG") or "").strip()
    cfg_path = Path(cfg_override) if cfg_override else (_resolve_workspace() / "config" / "data_tasks.env")
    return _read_data_task_config_file(cfg_path).get(key, "").strip()


WS = str(_resolve_workspace())
UNIVERSE_FILE = os.path.join(WS, "state/e_watch_universe.json")
OUT_FILE = os.path.join(WS, "temp/e_watch_latest.txt")
STATE_FILE = os.path.join(WS, "state/e_watch_signal_state.json")
HIST_FILE = os.path.join(WS, "state/e_watch_daily_history.json")   # 每天早报的档位快照，给「跟昨天比」用
MAX_CHARS = 1800   # Discord 单条 2000 字符上限，留余量
MAX_LINES = 17     # OpenClaw 的 Discord 通道超过 17 行会自动拆条，可能把一家公司拆成两半
HKT = ZoneInfo("Asia/Hong_Kong")

EQ_ORDER = ["🚀突破启动", "🟢趋势进场", "🔄回调买点", "⚠️左侧观察",
            "🟡趋势好但偏高", "🔴追高风险", "🔴趋势走坏", "⚪暂无机会"]


def load_universe():
    with open(UNIVERSE_FILE) as f:
        return json.load(f)["universe"]


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


def fetch_all(tickers):
    """批量下载，返回 {ticker: DataFrame}。失败的 ticker 不进结果。"""
    data = yf.download(tickers, period="2y", progress=False, auto_adjust=True,
                       group_by="ticker", threads=True)
    out = {}
    missing = []
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"])
        except Exception:
            df = None
        if df is None or df.empty or len(df) < 50:
            missing.append(t)
        else:
            out[t] = df
    # 批量下载偶发单只失败，逐只重试一次
    for t in missing:
        try:
            df = yf.Ticker(t).history(period="2y").dropna(subset=["Close"])
            if not df.empty:
                out[t] = df
        except Exception:
            pass
    return out


# 各市场收盘时间（当地）。晚间 yfinance 的当日日线常常还是 NaN，用小时线补成日线前，先确认那天已经收盘。
SESSION_CLOSE = {"US": ("America/New_York", 16, 0), "EU": ("Europe/Amsterdam", 17, 30),
                 "JP": ("Asia/Tokyo", 15, 30), "KR": ("Asia/Seoul", 15, 30),
                 "HK": ("Asia/Hong_Kong", 16, 10), "CN": ("Asia/Shanghai", 15, 0)}


def exchange_of(ticker):
    if ticker.endswith(".HK"): return "HK"
    if ticker.endswith((".SS", ".SZ")): return "CN"
    if ticker.endswith(".T"): return "JP"
    if ticker.endswith(".KS"): return "KR"
    if ticker.endswith(".AS"): return "EU"
    return "US"


def fill_recent_sessions(out, tickers, now=None, allow_partial=False):
    """日线缺最近交易日时，用小时线补一根。

    2026-10 起查到：08:00 HKT（美东前一晚 20:00）拉日线，美股和 ASML 当天那一行的 Close 是 NaN，
    dropna 之后整份早报的美股都晚了一个交易日。这里把小时线里比日线更新、且已经收盘的交易日
    聚合成日线补上；盘中模式允许补未收完的当天。返回 {ticker: [补上的日期]}。"""
    now = now or datetime.now(HKT)
    try:
        hr = yf.download(tickers, period="5d", interval="60m", progress=False, auto_adjust=True,
                         group_by="ticker", threads=True, prepost=False)
    except Exception:
        return {}
    filled = {}
    for t in tickers:
        if t not in out:
            continue
        try:
            h = hr[t].dropna(subset=["Close"])
        except Exception:
            continue
        if h.empty:
            continue
        tz, ch, cm = SESSION_CLOSE[exchange_of(t)]
        idx = h.index.tz_convert(tz) if h.index.tz is not None else h.index.tz_localize("UTC").tz_convert(tz)
        h = h.set_axis(idx)
        df = out[t]
        last = df.index[-1].date()
        local_now = now.astimezone(ZoneInfo(tz))
        rows = []
        for d in sorted({x.date() for x in h.index}):
            if d <= last:
                continue
            closed = local_now >= datetime(d.year, d.month, d.day, ch, cm, tzinfo=ZoneInfo(tz)) + timedelta(minutes=20)
            if not closed and not allow_partial:
                continue
            day = h[[x.date() == d for x in h.index]]
            stamp = pd.Timestamp(d)
            if df.index.tz is not None:
                stamp = stamp.tz_localize(df.index.tz)
            rows.append(pd.DataFrame({"Open": [float(day["Open"].iloc[0])], "High": [float(day["High"].max())],
                                      "Low": [float(day["Low"].min())], "Close": [float(day["Close"].iloc[-1])],
                                      "Volume": [float(day["Volume"].sum())]}, index=[stamp]))
        if rows:
            out[t] = pd.concat([df[["Open", "High", "Low", "Close", "Volume"]]] + rows)
            filled[t] = [str(r.index[0].date()) for r in rows]
    return filled


def analyze(ticker, name, group, df):
    if df is None or len(df) < 50:
        return {"name": name, "ticker": ticker, "group": group,
                "error": f"历史数据不足（{0 if df is None else len(df)} 根日线）"}
    try:
        c = df["Close"]; v = df["Volume"]
        cur = float(c.iloc[-1]); prev = float(c.iloc[-2])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma50 = float(c.rolling(50).mean().iloc[-1])
        ma200_s = c.rolling(200).mean()
        ma200 = float(ma200_s.iloc[-1]) if len(c) >= 200 else np.nan

        rsi_s = calc_rsi_series(c)
        rsi = float(rsi_s.iloc[-1])

        # 强势股判定（动态切换）：过去 6 个月 RSI>70 超过 30 天
        rsi_6m = rsi_s.iloc[-130:] if len(rsi_s) >= 130 else rsi_s
        rsi70_days = int((rsi_6m > 70).sum())
        was_strong = rsi70_days > 30
        if was_strong and cur < ma50:
            days_below_50 = 0
            for j in range(1, min(21, len(c))):
                if float(c.iloc[-j]) < ma50 - (ma50 * 0.001):
                    days_below_50 += 1
                else:
                    break
            is_strong = days_below_50 < 5
        else:
            is_strong = was_strong
        downgraded = was_strong and not is_strong

        # MACD 日线
        e12 = c.ewm(span=12, adjust=False).mean()
        e26 = c.ewm(span=26, adjust=False).mean()
        ml = e12 - e26; sig = ml.ewm(span=9, adjust=False).mean()
        macd_g = float(ml.iloc[-1]) > float(sig.iloc[-1])
        macd_prev = float(ml.iloc[-2]) <= float(sig.iloc[-2]) if len(ml) > 2 else False
        macd_just_crossed = macd_g and macd_prev

        # MACD 周线
        cw = c.resample("W").last().dropna()
        wk_golden = False
        if len(cw) >= 30:
            we12 = cw.ewm(span=12, adjust=False).mean()
            we26 = cw.ewm(span=26, adjust=False).mean()
            wml = we12 - we26; wsig = wml.ewm(span=9, adjust=False).mean()
            wk_golden = float(wml.iloc[-1]) > float(wsig.iloc[-1])

        v20 = float(v.iloc[-20:].mean())
        vol_ratio = float(v.iloc[-1]) / v20 if v20 > 0 else 1.0

        chg_1d = (cur / prev - 1) * 100
        chg_30d = (cur / float(c.iloc[-23]) - 1) * 100 if len(c) > 23 else 0.0

        lo52 = float(c.iloc[-252:].min()) if len(c) >= 252 else float(c.min())
        hi52 = float(c.iloc[-252:].max()) if len(c) >= 252 else float(c.max())

        divergence = False
        if len(c) > 40:
            p20 = float(c.iloc[-20:].min()); p40 = float(c.iloc[-40:-20].min())
            r20 = float(rsi_s.iloc[-20:].min()); r40 = float(rsi_s.iloc[-40:-20].min())
            if p20 < p40 and r20 > r40:
                divergence = True

        has200 = not np.isnan(ma200)
        above_50 = cur > ma50
        above_200 = has200 and cur > ma200

        # === 信号 ===
        signals = []
        if macd_just_crossed:
            signals.append("⚡MACD金叉(今日)")
        elif macd_g:
            recent = any(float(ml.iloc[-i]) <= float(sig.iloc[-i]) for i in range(2, min(6, len(ml))))
            if recent:
                signals.append("⚡MACD金叉(近5日)")
        if not macd_g:
            prev_golden = float(ml.iloc[-2]) > float(sig.iloc[-2]) if len(ml) > 2 else True
            if prev_golden:
                signals.append("💀MACD死叉(今日)")

        brk50 = cur > ma50 and prev <= ma50
        bd50 = cur < ma50 and prev >= ma50
        brk200 = has200 and cur > ma200 and prev <= ma200
        bd200 = has200 and cur < ma200 and prev >= ma200
        if brk50: signals.append("🚀突破50MA")
        if bd50: signals.append("📉跌破50MA")
        if brk200: signals.append("🚀突破200MA")
        if bd200: signals.append("📉跌破200MA")

        extreme_ob = rsi > (90 if is_strong else 80)
        ob = rsi > (85 if is_strong else 70)
        if is_strong:
            if rsi > 90: signals.append("🔴强势股极度超买(>90)")
            elif rsi > 85: signals.append("🔴强势股超买(>85)")
        else:
            if rsi > 80: signals.append("🔴极度超买")
            elif rsi > 70: signals.append("🔴超买")
        if rsi < 20: signals.append("🟢极度超卖")
        elif rsi < 30: signals.append("🟢超卖")

        if divergence: signals.append("🔄底背离")
        if vol_ratio > 2: signals.append(f"📊放量{vol_ratio:.1f}x")

        # === 无信号时在等什么 ===
        waiting = []
        if not signals:
            if is_strong:
                if cur < ma50:
                    waiting.append(f"强势股回调中，关注50MA({ma50:.1f})支撑")
                elif not macd_g:
                    waiting.append(f"强势股MACD死叉，等金叉回来；跌破50MA({ma50:.1f})才是真卖点")
                elif rsi > 65:
                    waiting.append(f"强势运行，RSI>85才需警惕（当前{rsi:.0f}）")
                else:
                    waiting.append("强势股正常波动，持有观察")
            else:
                if rsi < 35 and not macd_g:
                    waiting.append("等MACD金叉")
                elif rsi > 60 and macd_g and cur < ma50:
                    waiting.append(f"等突破50MA({ma50:.1f})")
                elif 35 <= rsi <= 65 and not macd_g:
                    waiting.append("中性震荡，等方向选择")
                elif 35 <= rsi <= 65 and macd_g and cur > ma50:
                    waiting.append("趋势偏多，持有观察")
                elif rsi > 65 and cur > ma50:
                    waiting.append("强势运行，关注RSI>70减仓信号")
            if not waiting:
                waiting.append("无明确信号，观望")

        if above_50 and (not has200 or above_200):
            trend = "📈多头"
        elif not above_50 and (not has200 or not above_200):
            trend = "📉空头"
        else:
            trend = "↔️震荡"

        stock_type = "💪强势" if is_strong else ("⚠️降级" if downgraded else "普通")

        r = {
            "name": name, "ticker": ticker, "group": group,
            "price": round(cur, 2), "chg_1d": round(chg_1d, 1), "chg_30d": round(chg_30d, 1),
            "rsi": round(rsi, 1), "macd": "金叉" if macd_g else "死叉",
            "macd_w": "金叉" if wk_golden else "死叉",
            "ma20": round(ma20, 2), "ma50": round(ma50, 2),
            "ma200": round(ma200, 2) if has200 else None,
            "above_50": above_50, "above_200": above_200, "has_ma200": has200,
            "trend": trend, "signals": signals, "waiting": waiting,
            "from_52h": round((cur / hi52 - 1) * 100, 1),
            "from_52l": round((cur / lo52 - 1) * 100, 1),
            "stock_type": stock_type, "is_strong": is_strong, "downgraded": downgraded,
            "macd_g": macd_g, "wk_golden": wk_golden,
            "vol_ratio": round(vol_ratio, 1), "divergence": divergence,
            "asof": str(df.index[-1].date()),
        }

        # === 正股机会分档（与 Sell Put 独立）===
        # 三块料：趋势结构（50MA/200MA）、动能（日线周线 MACD）、位置（RSI 是否已经透支）
        score = 0
        if above_50: score += 1
        if above_200: score += 1
        if macd_g: score += 1
        if wk_golden: score += 1
        if 40 <= rsi <= 65: score += 1
        if brk50 or brk200: score += 1
        if extreme_ob: score -= 1

        struct_txt = f"50MA{'✅' if above_50 else '❌'}/200MA{'✅' if above_200 else ('n/a' if not has200 else '❌')}"
        momo_txt = f"日线{'金叉' if macd_g else '死叉'}·周线{'金叉' if wk_golden else '死叉'}"

        if extreme_ob:
            eq_verdict = "🔴追高风险"
            eq_reason = [f"RSI={rsi:.0f} 已到极度超买（{'强势股>90' if is_strong else '普通股>80'}）",
                         f"等回落到 20MA({ma20:.1f}) 附近再谈进场"]
        elif bd50 or bd200:
            eq_verdict = "🔴趋势走坏"
            eq_reason = ([f"今日跌破50MA({ma50:.1f})"] if bd50 else []) + \
                        ([f"今日跌破200MA({ma200:.1f})"] if bd200 else [])
            eq_reason.append("结构破位优先减仓，不是抄底位")
        elif (brk50 or brk200) and macd_g and not ob:
            eq_verdict = "🚀突破启动"
            crossed = "、".join(x for x in [("今日站上50MA" if brk50 else None),
                                            ("今日站上200MA" if brk200 else None)] if x)
            eq_reason = [crossed,
                         f"{momo_txt}，RSI={rsi:.0f} 未触{'85' if is_strong else '70'}超买线",
                         f"跌回{(ma50 if brk50 else ma200):.1f}以下即证伪"]
            if vol_ratio > 1.5:
                eq_reason.insert(1, f"放量{vol_ratio:.1f}x 配合")
        elif above_50 and (above_200 or not has200) and macd_g and wk_golden and not ob:
            eq_verdict = "🟢趋势进场"
            eq_reason = [f"{struct_txt} 结构完整", f"{momo_txt} 同向",
                         f"RSI={rsi:.0f}，未触{'85（强势股口径）' if is_strong else '70'}超买线",
                         f"止损参考50MA({ma50:.1f})"]
        elif above_200 and ma50 * 0.94 <= cur <= ma50 * 1.02 and rsi < 50 and (divergence or macd_just_crossed):
            eq_verdict = "🔄回调买点"
            eq_reason = [f"长期趋势未破（200MA={ma200:.1f}）", f"回调到50MA({ma50:.1f})附近",
                         "底背离" if divergence else "日线MACD今日金叉",
                         f"RSI={rsi:.0f} 已释放"]
        elif is_strong and not downgraded and cur < ma50 and rsi < 55:
            eq_verdict = "🔄回调买点"
            eq_reason = ["强势股回调，尚未跌破50MA满5天", f"RSI={rsi:.0f}",
                         f"连续5天收在{ma50:.1f}下方就降级，届时离场"]
        elif not above_50 and divergence and (macd_g or rsi < 35):
            eq_verdict = "⚠️左侧观察"
            eq_reason = ["下跌趋势中出现底背离", f"RSI={rsi:.0f}，{momo_txt}",
                         f"左侧仓位，要等站上50MA({ma50:.1f})才算右侧确认"]
        elif ob and above_50 and macd_g:
            eq_verdict = "🟡趋势好但偏高"
            eq_reason = [f"{struct_txt}、{momo_txt} 都对", f"但 RSI={rsi:.0f} 已超买",
                         "追要减半仓，或等回踩20MA"]
        else:
            eq_verdict = "⚪暂无机会"
            eq_reason = [waiting[0] if waiting else f"{struct_txt}、{momo_txt}，条件不成组"]

        r["eq_verdict"], r["eq_reason"], r["eq_score"] = eq_verdict, [x for x in eq_reason if x], score

        # === Sell Put 条件检查（口径同 pt-watchlist）===
        dangers, cautions = [], []
        if is_strong:
            if rsi > 90: dangers.append("RSI>90极度超买")
        else:
            if rsi > 80: dangers.append("RSI>80极度超买")
        if downgraded:
            dangers.append("前强势股已降级，刚跌破50MA")
        if cur < ma50:
            days_below = 0
            for j in range(1, min(10, len(c))):
                if float(c.iloc[-j]) < ma50:
                    days_below += 1
                else:
                    break
            if 0 < days_below <= 5:
                dangers.append(f"刚跌破50MA({days_below}天)")
            elif days_below > 5:
                dangers.append("50MA下方，趋势向下")
        if not macd_g and rsi < 50:
            if len(rsi_s) > 5 and float(rsi_s.iloc[-1]) < float(rsi_s.iloc[-5]):
                dangers.append("MACD死叉+RSI下行通道")
        if rsi < 30:
            dangers.append(f"RSI超卖({rsi:.0f})")
        if vol_ratio > 2 and chg_1d < -2:
            dangers.append(f"放量{vol_ratio:.1f}x下跌{chg_1d:.1f}%")
        if not wk_golden and not macd_g:
            cautions.append("日线周线均死叉")

        ok = []
        if cur > ma50: ok.append(">50MA")
        if macd_g: ok.append("MACD金叉")
        if wk_golden: ok.append("周线金叉")
        if 40 <= rsi <= 65: ok.append(f"RSI={rsi:.0f}甜蜜区")

        if dangers:
            verdict, reason = "🔴暂不满足", " | ".join(dangers)
        elif cautions or len(ok) < 2:
            verdict, reason = "⚠️需要谨慎", (" | ".join(cautions) if cautions else "正面条件不足")
        else:
            verdict, reason = "✅条件较好", " | ".join(ok)

        if verdict == "✅条件较好":
            if is_strong:
                strike = f"strike参考<50MA({ma50:.1f})"
            elif has200 and cur > ma200:
                strike = f"strike参考<200MA({ma200:.1f})"
            else:
                strike = f"strike参考<{lo52:.1f}(52周低)"
        else:
            strike = ""

        r["put_verdict"], r["put_reason"], r["put_strike_ref"] = verdict, reason, strike
        return r
    except Exception as e:
        return {"name": name, "ticker": ticker, "group": group, "error": str(e)}


def pack(blocks, header):
    """把 block（行列表）打包成若干条消息，不切断单只标的。"""
    msgs, cur, cur_len = [], [], 0
    head_len = len(header) + 1
    for b in blocks:
        text = "\n".join(b)
        if cur and cur_len + len(text) + 1 > MAX_CHARS - head_len:
            msgs.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(text); cur_len += len(text) + 1
    if cur:
        msgs.append("\n".join(cur))
    if len(msgs) == 1:
        return [f"{header}\n{msgs[0]}"]
    return [f"{header}（{i+1}/{len(msgs)}）\n{m}" for i, m in enumerate(msgs)]


def one_line(r):
    ma200 = f"{r['ma200']}{'✅' if r['above_200'] else '❌'}" if r["has_ma200"] else "n/a"
    return (f"**{r['name']}({r['ticker']})** {r['trend']}[{r['stock_type']}] {r['price']} "
            f"今日{r['chg_1d']:+.1f}% 30日{r['chg_30d']:+.1f}%｜RSI={r['rsi']}｜"
            f"MACD日{r['macd']}周{r['macd_w']}｜50MA={r['ma50']}{'✅' if r['above_50'] else '❌'} 200MA={ma200}")


# ---------- 推送排版（2026-10-16 改版：每家公司第一行先写结论，第二行放数字，公司之间空一行）----------
# 用户 10-16 确认定稿；11-12 用户要求把有变化的公司（变好、变差）放到早报开头。
# 改排版先问用户，改完跑 scripts/test_e_watch_format.py。

BUY = ("🚀突破启动", "🟢趋势进场", "🔄回调买点")
WEEK = "一二三四五六日"


def px(x):
    if x >= 1000: return f"{x:,.0f}"
    if x >= 100: return f"{x:.1f}"
    return f"{x:.2f}"


def pct(x):
    return f"{'+' if x > 0 else '−' if x < 0 else '±'}{abs(x):.1f}%"


def nm(r):
    """美股名字后面带代码，其余市场只写名字。"""
    t = r["ticker"]
    if "." not in t and r["name"].upper() != t:
        return f"{r['name']} {t}"
    return r["name"]


def stop_of(r):
    """突破启动按刚站上的那条均线止损，其余用 50MA。"""
    if r["eq_verdict"] == "🚀突破启动" and "🚀突破50MA" not in r["signals"] and r["ma200"]:
        return r["ma200"], "200MA"
    return r["ma50"], "50MA"


def room_txt(r):
    lv, lab = stop_of(r)
    room = (1 - lv / r["price"]) * 100
    return f"止损 {lab} {px(lv)}，还有 {room:.1f}%{' ⚠️' if room < 3 else ''}"


def broke_what(r):
    return "、".join(x for x, k in (("50MA", "📉跌破50MA"), ("200MA", "📉跌破200MA")) if k in r["signals"]) or "均线"


def company_block(r, new=False):
    """一家公司两行：结论在前，数字在后。"""
    v, tag = r["eq_verdict"], " 🆕" if new else ""
    base = f"{px(r['price'])}（{pct(r['chg_1d'])}）"
    rsi = f"RSI {r['rsi']:.0f}"
    if v == "🚀突破启动":
        crossed = "和".join(x for x, k in (("50MA", "🚀突破50MA"), ("200MA", "🚀突破200MA")) if k in r["signals"])
        return [f"🚀 **{nm(r)}｜突破启动**{tag}　今天站上 {crossed}",
                f"{base}· {room_txt(r)} · {rsi}"]
    if v == "🟢趋势进场":
        return [f"🟢 **{nm(r)}｜趋势进场**{tag}", f"{base}· {room_txt(r)} · {rsi}"]
    if v == "🔄回调买点":
        why = "底背离" if r["divergence"] else ("日线今天金叉" if "⚡MACD金叉(今日)" in r["signals"] else "强势股回调")
        return [f"🔄 **{nm(r)}｜回调买点**{tag}　回到 50MA 附近，{why}",
                f"{base}· 50MA {px(r['ma50'])} · {rsi}"]
    if v == "🔴趋势走坏":
        vol = f" · 放量 {r['vol_ratio']:.1f} 倍" if r["vol_ratio"] > 2 else ""
        return [f"🔴 **{nm(r)}｜跌破 {broke_what(r)}**", f"{base}· {rsi}{vol}"]
    if v == "⚠️左侧观察":
        d = (r["ma50"] / r["price"] - 1) * 100
        return [f"⚠️ **{nm(r)}｜左侧观察**　出现底背离",
                f"{base}· 要涨 {d:.1f}% 才站上 50MA · {rsi}"]
    if v == "🟡趋势好但偏高":
        return [f"🟡 **{nm(r)}｜趋势好但偏高**　RSI {r['rsi']:.0f} 超买",
                f"{base}· 等回到 20MA {px(r['ma20'])} 附近再看"]
    if v == "🔴追高风险":
        return [f"🔴 **{nm(r)}｜追高风险**　RSI {r['rsi']:.0f} 极度超买",
                f"{base}· 等回到 20MA {px(r['ma20'])} 附近再看"]
    return [f"⚪ **{nm(r)}**", base]


TIERS = [
    ("🚀突破启动", "🚀 突破启动", "刚站上均线、日线金叉、没超买；跌回均线算失败"),
    ("🟢趋势进场", "🟢 趋势进场", "站上 50MA 和 200MA，日线周线都金叉、没超买；跌回 50MA 离场"),
    ("🔄回调买点", "🔄 回调买点", "长期趋势没破，回到 50MA 附近出现底背离或金叉"),
    ("🔴趋势走坏", "🔴 今天跌破均线", "结构破位，有仓位先减"),
    ("⚠️左侧观察", "⚠️ 左侧观察", "还在 50MA 下方，出现底背离；站上 50MA 才算转好"),
    ("🟡趋势好但偏高", "🟡 趋势好但偏高", "趋势对但已超买，追就减半，或等回踩"),
    ("🔴追高风险", "🔴 追高风险", "RSI 极度超买"),
]


def pack_units(units):
    """units 是若干段（行列表）。按 Discord 的字数和行数上限装条，一段不拆开；段与段之间空一行。"""
    msgs, cur = [], []
    def size(lines):
        return len(lines), len("\n".join(lines))
    for u in units:
        trial = cur + ([""] if cur else []) + u
        n, c = size(trial)
        if cur and (n > MAX_LINES or c > MAX_CHARS):
            msgs.append("\n".join(cur))
            cur = list(u)
        else:
            cur = trial
    if cur:
        msgs.append("\n".join(cur))
    return msgs


def split_unit(lines):
    """单段超过行数上限时（如分组多于 16 个的分组表）均分成几段，各段仍按原顺序发。"""
    if len(lines) <= MAX_LINES:
        return [lines]
    k = -(-len(lines) // MAX_LINES)
    step = -(-len(lines) // k)
    return [lines[i:i + step] for i in range(0, len(lines), step)]


def load_hist():
    try:
        with open(HIST_FILE) as f:
            return json.load(f).get("days", [])
    except Exception:
        return []


def save_hist(days, results, data_date):
    snap = {"data_date": data_date, "saved_at": datetime.now(HKT).isoformat(),
            "verdict": {r["ticker"]: r["eq_verdict"] for r in results},
            "above_50": {r["ticker"]: bool(r["above_50"]) for r in results}}
    days = [d for d in days if d.get("data_date") != data_date] + [snap]
    days = sorted(days, key=lambda d: d["data_date"])[-10:]
    os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
    with open(HIST_FILE, "w") as f:
        json.dump({"days": days}, f, ensure_ascii=False)


REGION = {"US": "美股", "EU": "欧股", "JP": "亚洲", "KR": "亚洲", "HK": "亚洲", "CN": "亚洲"}


def session_closed(ticker, day, now=None):
    tz, ch, cm = SESSION_CLOSE[exchange_of(ticker)]
    d = datetime.strptime(day, "%Y-%m-%d")
    return (now or datetime.now(HKT)) >= datetime(d.year, d.month, d.day, ch, cm, tzinfo=ZoneInfo(tz))


def data_line(results):
    """每个市场取最常见的最后交易日；还没收盘的写「盘中」，个别票更旧时点名。"""
    from collections import Counter
    by = {}
    for r in results:
        by.setdefault(REGION[exchange_of(r["ticker"])], []).append(r)
    main, lag = {}, []
    for reg in ("美股", "欧股", "亚洲"):
        rs = by.get(reg)
        if not rs:
            continue
        d = Counter(r["asof"] for r in rs).most_common(1)[0][0]
        live = any(r["asof"] == d and not session_closed(r["ticker"], d) for r in rs)
        main[reg] = f"{int(d[5:7])}/{int(d[8:10])} {'盘中' if live else '收盘'}"
        lag += [f"{nm(r)}（{int(r['asof'][5:7])}/{int(r['asof'][8:10])}）" for r in rs if r["asof"] < d]
    vals = set(main.values())
    if len(vals) == 1:
        txt = f"数据：{'、'.join(main)}都是 {vals.pop()}"
    else:
        txt = "数据：" + "；".join(f"{reg} {v}" for reg, v in main.items())
    if lag:
        txt += "\n⚠️ 这几只数据停在更早的日期：" + "、".join(lag)
    return txt


# ---------- 信号 state（盘中增量去重）----------

def sig_key(s):
    """去掉数字，避免『放量2.1x → 放量2.4x』被当成新信号。"""
    return re.sub(r"[\d.]+", "", s)


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        ts = datetime.fromisoformat(st.get("seeded_at"))
        if datetime.now(HKT) - ts > timedelta(hours=36):
            return None
        return st
    except Exception:
        return None


def save_state(results, mode):
    st = load_state() if mode == "intraday" else None
    seen = st.get("seen", {}) if st else {}
    for r in results:
        keys = set(seen.get(r["ticker"], []))
        keys |= {sig_key(s) for s in r["signals"]}
        keys.add("EQ:" + r["eq_verdict"])
        seen[r["ticker"]] = sorted(keys)
    payload = {"seeded_at": (st or {}).get("seeded_at") or datetime.now(HKT).isoformat(),
               "updated_at": datetime.now(HKT).isoformat(), "seen": seen}
    if mode == "daily":
        payload["seeded_at"] = datetime.now(HKT).isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False)


def market_of(ticker):
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SS", ".SZ")):
        return "CN"
    return "US"


def in_own_session(ticker, n):
    """标的是否处在自己市场的交易时段（含收盘后的结算余量，HKT）。
    盘中增量只推自己有行情的标的：美股时段不推 A 股/港股的昨收。"""
    wd, h = n.weekday(), n.hour
    m = market_of(ticker)
    if m == "CN":
        return wd <= 4 and 9 <= h <= 17
    if m == "HK":
        return wd <= 4 and 9 <= h <= 18
    return (wd <= 4 and h >= 21) or (1 <= wd <= 5 and h <= 6)


def market_open_now():
    """粗粒度时段闸门，只为省掉明显无行情的运行。宽 1 小时余量，跨夏令时也覆盖。"""
    n = datetime.now(HKT)
    wd, h = n.weekday(), n.hour          # 周一=0
    if wd <= 4 and 8 <= h <= 23:         # 东京/首尔/港股/A股/欧洲/美股盘前与前半场
        return True
    if 1 <= wd <= 5 and h <= 5:          # 美股后半场落在港时次日凌晨
        return True
    return False


# ---------- daily ----------

def run_daily(results, failed, universe, now):
    has_signal = [r for r in results if r["signals"]]
    no_signal = [r for r in results if not r["signals"]]

    put_ok = [r for r in results if r.get("put_verdict") == "✅条件较好"]
    put_warn = [r for r in results if r.get("put_verdict") == "⚠️需要谨慎"]
    put_ban = [r for r in results if r.get("put_verdict") == "🔴暂不满足"]

    eq = {k: [r for r in results if r["eq_verdict"] == k] for k in EQ_ORDER}

    # —— 跟前一个交易日比：取早报快照里数据日期更早的最近一份 ——
    data_date = max(r["asof"] for r in results)
    hist = load_hist()
    prev = next((d for d in reversed(hist) if d["data_date"] < data_date), None)
    pv = prev["verdict"] if prev else {}
    p50 = prev["above_50"] if prev else {}
    # 股票池刚加过票时，新票在前一天快照里没有记录，只拿两天都有的票做比较，避免把新票算成"新进"
    common = [r for r in results if r["ticker"] in pv]

    n = len(results)
    up = sum(1 for r in results if r["chg_1d"] > 0)
    down = sum(1 for r in results if r["chg_1d"] < 0)
    a50 = sum(1 for r in results if r["above_50"])
    rev = up >= down

    groups = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    gstat = []
    for g, rs in groups.items():
        avg = sum(r["chg_1d"] for r in rs) / len(rs)
        ab = sum(1 for r in rs if r["above_50"])
        abp = sum(1 for r in rs if p50.get(r["ticker"])) \
            if prev and all(r["ticker"] in p50 for r in rs) else None
        gstat.append((g, len(rs), avg, ab, abp))
    gstat.sort(key=lambda x: x[2], reverse=rev)

    # 结论句：先看各地区是否分化，再说领涨/领跌分组
    regions = {}
    for r in results:
        regions.setdefault(REGION[exchange_of(r["ticker"])], []).append(r["chg_1d"])
    ra = {k: sum(v) / len(v) for k, v in regions.items() if len(v) >= 5}
    if "美股" in ra and "亚洲" in ra and ra["美股"] * ra["亚洲"] < 0 and abs(ra["美股"] - ra["亚洲"]) >= 3:
        mood = f"美股{'涨' if ra['美股'] > 0 else '跌'}、亚洲{'涨' if ra['亚洲'] > 0 else '跌'}"
        tail = f"美股 {len(regions['美股'])} 只平均 {pct(ra['美股'])}，亚洲 {len(regions['亚洲'])} 只平均 {pct(ra['亚洲'])}"
    else:
        mood = "普跌" if down >= 0.7 * n else ("普涨" if up >= 0.7 * n else "涨跌互现")
        tail = f"{n} 只里 {up} 涨 {down} 跌"
    lead = [x for x in gstat if x[1] >= 3][:2]
    lead_txt = "、".join(f"{g}（{pct(a)}）" for g, _, a, _, _ in lead)
    new_buy = [r for r in common if r["eq_verdict"] in BUY and pv.get(r["ticker"]) not in BUY] if prev else []
    out_buy = [r for r in common if r["eq_verdict"] not in BUY and pv.get(r["ticker"]) in BUY] if prev else []
    broke = eq["🔴趋势走坏"]
    line2 = (f"新出现 {len(new_buy)} 个可买信号" if new_buy else ("没有新的可买信号" if prev else "可买档共 "
             f"{sum(len(eq[k]) for k in BUY)} 只")) + f"，{len(broke)} 只跌破均线"
    if prev:
        if len(common) == len(results) == len(p50):
            line2 += f"；站上 50MA 的从 {sum(1 for v in p50.values() if v)} 只变成 {a50} 只"
        else:
            line2 += (f"；两天都在名单里的 {len(common)} 只中，站上 50MA 的从 "
                      f"{sum(1 for r in common if p50.get(r['ticker']))} 只变成 "
                      f"{sum(1 for r in common if r['above_50'])} 只")
    nowd = datetime.now(HKT)
    dl = data_line(results).split("\n")
    title = [f"**e-watch 早报｜{nowd.month}/{nowd.day} 周{WEEK[nowd.weekday()]} {nowd:%H:%M}**　{dl[0]}"] + dl[1:]
    if failed:
        title.append("⚠️ 没取到数据：" + "、".join(f"{f['name']}({f['ticker']})" for f in failed))
    title += ["", f"**{mood}：{tail}；{'领涨' if rev else '领跌'}：{lead_txt}。**", line2 + "。"]

    gtab = ["**分组**（当天平均涨跌｜站上 50MA 的家数）"]
    for g, k, avg, ab, abp in gstat:
        gtab.append(f"- {g}：{pct(avg)}｜{ab}/{k}" + (f"（前一天 {abp}）" if abp is not None and abp != ab else ""))

    # —— 今天的变化（2026-11-12 用户要求：有变化的放在提醒最开头，好的坏的都列）——
    # 变好＝新进可买档；变差＝今天跌破均线（前一个快照里不是），或从可买档掉到左侧观察／暂无机会。
    # 涨到超买而掉出可买档的不算变差，只列名字，明细在后面「趋势好但偏高」「追高风险」。
    # 在这里展开过的公司，后面的档位明细不再重复。
    hot = ("🟡趋势好但偏高", "🔴追高风险")
    order = {k: i for i, k in enumerate(EQ_ORDER)}
    better = sorted(new_buy, key=lambda r: (order[r["eq_verdict"]], 1 - stop_of(r)[0] / r["price"]))
    # 前一个快照里已经是「跌破均线」的不再算变差：休市或没取到新数据时，同一根 K 线会重复出现
    worse = sorted([r for r in broke if pv.get(r["ticker"]) != "🔴趋势走坏"], key=lambda r: r["chg_1d"]) + sorted(
        [r for r in out_buy if r["eq_verdict"] not in hot and r["eq_verdict"] != "🔴趋势走坏"],
        key=lambda r: r["chg_1d"])
    overheated = [r for r in out_buy if r["eq_verdict"] in hot]
    flagged = {r["ticker"] for r in better + worse}
    crossed_up = [r for r in common if r["above_50"] and p50.get(r["ticker"]) is False
                  and r["ticker"] not in flagged] if prev else []
    crossed_down = [r for r in common if not r["above_50"] and p50.get(r["ticker"]) is True
                    and r["ticker"] not in flagged] if prev else []

    def plain(v):
        return re.sub(r"^[^\u4e00-\u9fff]+", "", v or "")

    def names(rs, k=20):
        txt = "、".join(nm(r) for r in rs[:k])
        return txt + (f" 等 {len(rs)} 只" if len(rs) > k else "")

    def change_block(r):
        v, was = r["eq_verdict"], pv.get(r["ticker"])
        if v == "⚪暂无机会":
            blk = [f"⚪ **{nm(r)}｜掉出可买档**　条件散了",
                   f"{px(r['price'])}（{pct(r['chg_1d'])}）· 50MA {px(r['ma50'])} · RSI {r['rsi']:.0f}"]
        else:
            blk = company_block(r, new=v in BUY)
        if was and was != v:
            blk = [blk[0] + f"　前一天：{plain(was)}"] + blk[1:]
        return blk

    vs = f"和 {prev['data_date'][5:].replace('-', '/')} 比" if prev else "没有前一天的快照，只列今天跌破均线的"
    change_units = []
    for sign, rs, rule in (("🟢 变好", better, "新进可买档"), ("🔴 变差", worse, "今天跌破均线，或掉出可买档")):
        if not rs:
            continue
        head = [f"**{sign} {len(rs)} 只**　_{vs}；{rule}_"]
        for j, r in enumerate(rs):
            change_units.append(head + [""] + change_block(r) if j == 0 else change_block(r))
    extra = []
    if crossed_up:
        extra.append(f"- 另有 {len(crossed_up)} 只站上 50MA，还没进可买档：" + names(crossed_up))
    if crossed_down:
        extra.append(f"- 另有 {len(crossed_down)} 只回到 50MA 下方：" + names(crossed_down))
    if overheated:
        extra.append(f"- 🟡 涨到超买、掉出可买档 {len(overheated)} 只：" + "、".join(
            f"{nm(r)}（{'偏高' if r['eq_verdict'] == hot[0] else '极度超买'}）" for r in overheated))
    oversold = sorted([r for r in results if r["rsi"] < 30], key=lambda r: r["rsi"])
    if oversold:
        extra.append(f"- RSI 低于 30 的 {len(oversold)} 只：" + names(oversold, 30))
    if not change_units:
        extra.insert(0, f"**今天没有档位变化**（{vs}）" if prev else f"**今天没有跌破均线的**（{vs}）")
    if extra:
        change_units.append(extra)
    overview = "\n\n".join("\n".join(u) for u in [title] + change_units + [gtab])

    # —— 明细：按档位分组，每家公司两行，公司之间空一行；每家只出现一次 ——
    units = []
    for key, label, rule in TIERS:
        rs = [r for r in eq[key] if r["ticker"] not in flagged]
        if not rs:
            continue
        if key in BUY:   # 新进的排前面，其余按离止损由近到远
            rs.sort(key=lambda r: (pv.get(r["ticker"]) in BUY, 1 - stop_of(r)[0] / r["price"]))
        elif key == "⚠️左侧观察":
            rs.sort(key=lambda r: r["ma50"] / r["price"])
        else:
            rs.sort(key=lambda r: r["chg_1d"])
        above = len(eq[key]) - len(rs)
        head = [f"**{label} {len(rs)} 只**" + (f"（开头还有 {above} 只）" if above else "") + f"　_{rule}_"]
        first = True
        for r in rs:
            blk = company_block(r, new=bool(prev) and key in BUY and r["ticker"] in pv
                                and pv.get(r["ticker"]) not in BUY)
            units.append(head + [""] + blk if first else blk)
            first = False
    rest = len([r for r in eq["⚪暂无机会"] if r["ticker"] not in flagged])
    units.append([f"其余 {rest} 只没有成组的条件。全部明细、Sell Put 检查和规则口径见附件。"])
    # 顺序：标题和结论 → 今天的变化 → 分组 → 其余档位明细
    head_units = split_unit(title) + [p for u in change_units for p in split_unit(u)] + split_unit(gtab)
    messages = pack_units(head_units + units)

    # —— 附件全文 ——
    full = [overview, "", "【正股机会分档】"]
    for k in EQ_ORDER:
        group = sorted(eq[k], key=lambda x: -x["eq_score"])
        full.append(f"\n— {k}（{len(group)} 只）—")
        for r in group:
            full.append(f"{r['name']}({r['ticker']}) {r['price']} RSI={r['rsi']} "
                        f"日{r['macd']}周{r['macd_w']} {'>50MA' if r['above_50'] else '<50MA'}"
                        f" → {' ｜ '.join(r['eq_reason'])}")
    if has_signal:
        full.append(f"\n【今日触发规则：{len(has_signal)} 只】")
        cur_group = None
        for r in sorted(has_signal, key=lambda x: (x["group"], x["name"])):
            if r["group"] != cur_group:
                cur_group = r["group"]; full.append(f"\n— {cur_group} —")
            ma200 = f"{r['ma200']}{'✅' if r['above_200'] else '❌'}" if r["has_ma200"] else "n/a"
            full.append(f"{r['name']}({r['ticker']}) {r['trend']}[{r['stock_type']}] {r['price']} 今日{r['chg_1d']:+.1f}% 30日{r['chg_30d']:+.1f}%｜RSI={r['rsi']}｜MACD日{r['macd']}周{r['macd_w']}｜50MA={r['ma50']}{'✅' if r['above_50'] else '❌'} 200MA={ma200}｜距52周高{r['from_52h']:+.1f}%")
            full.append(f"  ➡️ {' | '.join(r['signals'])}")
    if no_signal:
        full.append(f"\n【今日未触发：{len(no_signal)} 只】")
        cur_group = None
        for r in sorted(no_signal, key=lambda x: (x["group"], x["name"])):
            if r["group"] != cur_group:
                cur_group = r["group"]; full.append(f"\n— {cur_group} —")
            full.append(f"{r['name']}({r['ticker']}) {r['price']} {r['chg_1d']:+.1f}% {r['trend']} RSI={r['rsi']} {r['macd']} {'>50MA' if r['above_50'] else '<50MA'} → {r['waiting'][0]}")
    full.append("\n【Sell Put 条件检查】")
    for label, group in (("条件较好", put_ok), ("需要谨慎", put_warn), ("暂不满足", put_ban)):
        full.append(f"\n— {label}（{len(group)} 只）—")
        for r in group:
            tail = f"｜{r['put_strike_ref']}" if r["put_strike_ref"] else ""
            full.append(f"{r['name']}({r['ticker']}) {r['price']}｜{r['put_reason']}{tail}")
    full += [
        "\n【口径】",
        "触发任一条件即列入观察：MACD 日线金叉/死叉（今日或近 5 日）、突破或跌破 50MA/200MA、",
        "RSI 超买（普通股 >70/80，强势股 >85/90）、RSI 超卖（<30/<20）、底背离、量比 >2x。",
        "强势股判定：过去 6 个月 RSI>70 的天数超过 30 天，改用更高超买阈值；跌破 50MA 超过 5 天降级回普通标准。",
        "",
        "正股机会分档（看的是买正股的时机，与 Sell Put 各自独立）：",
        "🚀突破启动＝今日站上 50MA 或 200MA + 日线金叉 + 未超买，趋势刚起、位置最好，跌回均线即证伪；",
        "🟢趋势进场＝站上 50MA 与 200MA + 日线周线双金叉 + 未超买，趋势已成、可顺势进，止损放 50MA；",
        "🔄回调买点＝长期趋势未破（站上 200MA 或仍属强势股）但已回调到 50MA 附近、RSI 释放且出现底背离或日线金叉；",
        "⚠️左侧观察＝还在 50MA 下方，只有底背离或超卖反弹的苗头，属左侧仓位，站上 50MA 才算右侧确认；",
        "🟡趋势好但偏高＝结构和动能都对但 RSI 已超买，追要减半仓或等回踩 20MA；",
        "🔴追高风险＝RSI 极度超买；🔴趋势走坏＝今日跌破 50MA 或 200MA；⚪暂无机会＝条件不成组。",
        "",
        "",
        "Sell Put 分组：暂不满足＝RSI 极端超买、刚跌破 50MA、MACD 死叉且 RSI 下行、超卖或放量下跌；",
        "需要谨慎＝日线周线均死叉，或正面条件不足；条件较好＝站上 50MA、日线金叉、周线金叉、RSI 40—65 中至少满足两项。",
        "RSI 用 Wilder 递归平滑口径，与 TradingView / 雪球 / Bloomberg 的默认算法一致。",
        "数据源 yfinance 日线（后复权），上市不足 200 个交易日的标的没有 200MA。",
        "以上均为规则筛选结果，不是买卖建议。",
        f"股票池：{UNIVERSE_FILE}（加减票改这个文件，不改代码）。",
    ]
    full_text = "\n".join(full)
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        f.write(full_text)

    save_state(results, "daily")
    save_hist(hist, results, data_date)

    discord_target = resolve_discord_target()
    if not discord_target:
        print(
            "ERROR: 未配置 DALTON_DATA_TASK_DISCORD_DM_TARGET（请设置环境变量或在 config/data_tasks.env 中配置）",
            file=sys.stderr,
        )
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "discord_target": discord_target,
        "messages": messages,
        "report_file": OUT_FILE,
        "stats": {"universe": len(universe), "analyzed": len(results),
                  "failed": [f["ticker"] for f in failed],
                  "signals": len(has_signal), "message_count": len(messages),
                  "eq": {k: len(v) for k, v in eq.items()}},
    }, ensure_ascii=False))


# ---------- intraday ----------

def run_intraday(results, now):
    st = load_state()
    if st is None:
        print("NO_REPLY")   # 还没有当日基准，等 08:00 全量报告先跑
        return
    seen = st.get("seen", {})

    # 只处理此刻自己市场有行情的标的；其余保持基准不变，等它们开盘再比
    n = datetime.now(HKT)
    results = [r for r in results if in_own_session(r["ticker"], n)]
    if not results:
        print("NO_REPLY")
        return

    MA_EVENTS = {"🚀突破50MA": "站上 50MA", "🚀突破200MA": "站上 200MA",
                 "📉跌破50MA": "跌破 50MA", "📉跌破200MA": "跌破 200MA"}

    def minor_word(sig, r):
        if "金叉" in sig: return "日线金叉"
        if "死叉" in sig: return "日线死叉"
        if "超买" in sig: return f"RSI {r['rsi']:.0f} 超买"
        if "超卖" in sig: return f"RSI {r['rsi']:.0f} 超卖"
        if "底背离" in sig: return "底背离"
        if "放量" in sig: return f"放量 {r['vol_ratio']:.1f} 倍"
        return sig

    main, minor = [], []
    for r in results:
        old = set(seen.get(r["ticker"], []))
        new_sigs = [s for s in r["signals"]
                    if "(近5日)" not in s and sig_key(s) not in old]
        eq_new = r["eq_verdict"] in ("🚀突破启动", "🟢趋势进场", "🔄回调买点", "🔴趋势走坏") \
            and ("EQ:" + r["eq_verdict"]) not in old
        if not new_sigs and not eq_new:
            continue
        ma = [MA_EVENTS[s] for s in new_sigs if s in MA_EVENTS]
        rest = [minor_word(s, r) for s in new_sigs if s not in MA_EVENTS]
        if eq_new or ma:
            what = "、".join(ma)
            if eq_new:
                to = r["eq_verdict"][1:] if r["eq_verdict"][0] != "⚠" else r["eq_verdict"]
                what = (what + "，" if what else "") + f"转为{to}"
            emoji = "🔴" if (r["eq_verdict"] == "🔴趋势走坏" or (ma and all("跌破" in m for m in ma))) else \
                    ("🟢" if eq_new else "🔵")
            line2 = f"{px(r['price'])}（{pct(r['chg_1d'])}）"
            if r["eq_verdict"] in BUY:
                line2 += f"· {room_txt(r)}"
            elif ma and all("200MA" in x for x in ma) and r["ma200"]:
                line2 += f"· 200MA {px(r['ma200'])}"
            else:
                line2 += f"· 50MA {px(r['ma50'])}"
            line2 += f" · RSI {r['rsi']:.0f}" + (f" · 另有{'、'.join(rest)}" if rest else "")
            prio = 0 if emoji == "🔴" else 1
            main.append((prio, -abs(r["chg_1d"]), [f"{emoji} **{nm(r)}｜{what}**", line2]))
        else:
            minor.append(f"{nm(r)} {'、'.join(rest)}")

    save_state(results, "intraday")

    if not main and not minor:
        print("NO_REPLY")
        return

    main.sort(key=lambda x: (x[0], x[1]))
    shown, extra = main[:4], main[4:]
    body = [f"⚡ **e-watch 盘中｜{now[5:].replace('-', '/')} HKT**　用的是没收盘的日线，收盘前可能反复"]
    for _, _, blk in shown:
        body += [""] + blk
    tail = []
    if extra:
        tail.append("另有：" + "；".join(b[0].split("**")[1] for _, _, b in extra))
    if minor:
        tail.append("其他：" + "；".join(minor))
    if tail:
        body += [""] + tail
    text = "\n".join(body)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 30] + "…（明早全量报告里有全部）"
    print(text)


def main():
    mode = "daily"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
    if mode == "intraday" and "--force" not in sys.argv and not market_open_now():
        print("NO_REPLY")
        return
    if mode != "intraday" and not resolve_discord_target():
        print(
            "ERROR: 未配置 DALTON_DATA_TASK_DISCORD_DM_TARGET（请设置环境变量或在 config/data_tasks.env 中配置）",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now(HKT).strftime("%Y-%m-%d %H:%M")
    universe = load_universe()
    tickers = [u["ticker"] for u in universe]
    data = fetch_all(tickers)
    fill_recent_sessions(data, tickers, allow_partial=(mode == "intraday"))

    results, failed = [], []
    for u in universe:
        r = analyze(u["ticker"], u["name"], u["group"], data.get(u["ticker"]))
        if "error" in r:
            failed.append(r)
        else:
            results.append(r)

    if mode == "intraday":
        run_intraday(results, now)
    else:
        run_daily(results, failed, universe, now)


if __name__ == "__main__":
    main()
