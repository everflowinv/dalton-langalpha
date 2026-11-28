#!/usr/bin/env python3
"""
PT 重点关注名单 - 增量信号监控

只报「相对上一次运行的变化」，不再每次全量输出：
- 状态转换：MACD 金叉/死叉、突破/跌破 50MA/200MA、RSI 进入/离开超买超卖区、
  出现底背离、强势股升降级
- 当日事件：放量 >2x（按 K 线日期去重，同一根 K 线只报一次）
- Sell Put：只报分组变动（进入/离开 ✅可卖 等）
- JEPQ—QQQ 三级灯：每次在主消息末尾附一行当前状态；变色时另发一条完整说明（jepq_message，平时为空）

状态存 state/pt_watchlist_signal_state.json；首次运行建基线，只报当日 bar 事件。
无任何变化时 main_message 为一行「无新信号」。
完整全量快照仍写 temp/pt_watchlist_latest.txt 供随时查看。
输出 JSON: {ok, main_message, jepq_message, feishu_target}

配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
- DALTON_DATA_TASK_FEISHU_ASIA_TARGET  飞书接收目标（兼容 DALTON_FEISHU_ASIA_TARGET，如 chat:oc_...，必填，未配置时非零退出）
- DALTON_DATA_TASK_WORKSPACE           数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR，默认当前运行目录）
"""

import warnings as _w; _w.filterwarnings('ignore')  # suppress NotOpenSSLWarning etc. before urllib3 import
import yfinance as yf
import pandas as pd
import numpy as np
import json, os, sys
from datetime import datetime
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')


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


def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


def resolve_feishu_target() -> str:
    keys = ("DALTON_DATA_TASK_FEISHU_ASIA_TARGET", "DALTON_FEISHU_ASIA_TARGET")
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


WS = str(_resolve_workspace())
STATE_FILE = os.path.join(WS, "state/pt_watchlist_signal_state.json")
OUT_FILE = os.path.join(WS, "temp/pt_watchlist_latest.txt")
FEISHU_TARGET = resolve_feishu_target()

WATCHLIST = {
    # 美股个股
    "GOOGL": "谷歌",
    "NVDA": "英伟达",
    "AVGO": "博通",
    "MSFT": "微软",
    "TSM": "台积电",
    "AFRM": "Affirm",
    "BABA": "阿里巴巴",
    # 港股
    "9992.HK": "泡泡玛特",
    "0700.HK": "腾讯",
    "0883.HK": "中海油",
    # 韩股
    "000660.KS": "SK海力士",
    "005930.KS": "三星电子",
    "005935.KS": "三星优先",
    # A股
    "600519.SS": "贵州茅台",
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    # ETF/指数
    "SOXX": "SOX半导体",
    "IGV": "软件ETF",
    "QQQ": "纳指100",
    "SPY": "标普500",
    "KWEB": "中概互联",
}

ZONE_NAME = {"ext_ob": "极度超买", "ob": "超买", "neutral": "中性",
             "os": "超卖", "ext_os": "极度超卖"}


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


def analyze(ticker, name):
    try:
        df = yf.Ticker(ticker).history(period="2y")
        # 2026-11-24：美股盘前 yfinance 可能带一行收盘价为空的当天数据，空值会让价格、
        # 均线、RSI 全部变成 nan 并写坏状态。先剔除空收盘行；剩余不足时按取数失败处理，保留旧状态。
        df = df.dropna(subset=['Close'])
        if df.empty or len(df) < 50:
            return {"name": name, "ticker": ticker, "error": "历史数据不足"}
        c = df['Close']; v = df['Volume']
        cur = float(c.iloc[-1]); prev = float(c.iloc[-2])
        if np.isnan(cur) or np.isnan(prev):
            return {"name": name, "ticker": ticker, "error": "最新收盘价缺失"}
        ma50 = float(c.rolling(50).mean().iloc[-1])
        ma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else np.nan
        has200 = not np.isnan(ma200)

        rsi_s = calc_rsi_series(c)
        rsi = float(rsi_s.iloc[-1])

        # 强势股判定（动态切换）：过去6个月RSI>70天数>30天
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
        macd_prev_g = float(ml.iloc[-2]) > float(sig.iloc[-2]) if len(ml) > 2 else macd_g

        # MACD 周线
        cw = c.resample('W').last().dropna()
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

        divergence = False
        if len(c) > 40:
            p20 = float(c.iloc[-20:].min()); p40 = float(c.iloc[-40:-20].min())
            r20 = float(rsi_s.iloc[-20:].min()); r40 = float(rsi_s.iloc[-40:-20].min())
            if p20 < p40 and r20 > r40:
                divergence = True

        above_50 = cur > ma50
        above_200 = has200 and cur > ma200

        # RSI 分区（强势股用更高超买阈值）
        ext_ob_th = 90 if is_strong else 80
        ob_th = 85 if is_strong else 70
        if rsi > ext_ob_th:
            zone = "ext_ob"
        elif rsi > ob_th:
            zone = "ob"
        elif rsi < 20:
            zone = "ext_os"
        elif rsi < 30:
            zone = "os"
        else:
            zone = "neutral"

        # === 当日 bar 事件（仅首次建基线时用）===
        today_events = []
        if macd_g and not macd_prev_g:
            today_events.append("⚡MACD金叉(今日)")
        if not macd_g and macd_prev_g:
            today_events.append("💀MACD死叉(今日)")
        if cur > ma50 and prev <= ma50:
            today_events.append("🚀突破50MA(今日)")
        if cur < ma50 and prev >= ma50:
            today_events.append("📉跌破50MA(今日)")
        if has200:
            if cur > ma200 and prev <= ma200:
                today_events.append("🚀突破200MA(今日)")
            if cur < ma200 and prev >= ma200:
                today_events.append("📉跌破200MA(今日)")

        # === Sell Put 评估（口径不变）===
        put_dangers, put_cautions = [], []
        if is_strong:
            if rsi > 90: put_dangers.append("RSI>90极度超买")
        else:
            if rsi > 80: put_dangers.append("RSI>80极度超买")
        if downgraded:
            put_dangers.append("前强势股已降级，刚跌破50MA")
        if cur < ma50:
            days_below = 0
            for j in range(1, min(10, len(c))):
                if float(c.iloc[-j]) < ma50:
                    days_below += 1
                else:
                    break
            if 0 < days_below <= 5:
                put_dangers.append(f"刚跌破50MA({days_below}天)")
            elif days_below > 5:
                put_dangers.append("50MA下方，趋势向下")
        if not macd_g and rsi < 50:
            if len(rsi_s) > 5 and float(rsi_s.iloc[-1]) < float(rsi_s.iloc[-5]):
                put_dangers.append("MACD死叉+RSI下行通道")
        if rsi < 30:
            put_dangers.append(f"RSI超卖({rsi:.0f})")
        if vol_ratio > 2 and chg_1d < -2:
            put_dangers.append(f"放量{vol_ratio:.1f}x下跌{chg_1d:.1f}%")
        if not wk_golden and not macd_g:
            put_cautions.append("日线周线均死叉")

        put_ok_reasons = []
        if cur > ma50: put_ok_reasons.append(">50MA")
        if macd_g: put_ok_reasons.append("MACD金叉")
        if wk_golden: put_ok_reasons.append("周线金叉")
        if 40 <= rsi <= 65: put_ok_reasons.append(f"RSI={rsi:.0f}甜蜜区")

        if put_dangers:
            put_verdict, put_reason = "🔴禁卖", " | ".join(put_dangers)
        elif put_cautions or len(put_ok_reasons) < 2:
            put_verdict, put_reason = "⚠️谨慎", (" | ".join(put_cautions) if put_cautions else "正面条件不足")
        else:
            put_verdict, put_reason = "✅可卖", " | ".join(put_ok_reasons)

        if put_verdict == "✅可卖":
            if is_strong:
                strike_ref = f"strike参考<50MA({ma50:.1f})"
            elif has200 and cur > ma200:
                strike_ref = f"strike参考<200MA({ma200:.1f})"
            else:
                strike_ref = f"strike参考<{lo52:.1f}(52周低)"
        else:
            strike_ref = ""

        if above_50 and (not has200 or above_200):
            trend = "📈多头"
        elif not above_50 and (not has200 or not above_200):
            trend = "📉空头"
        else:
            trend = "↔️震荡"

        return {
            "name": name, "ticker": ticker,
            "price": round(cur, 2), "chg_1d": round(chg_1d, 1), "chg_30d": round(chg_30d, 1),
            "rsi": round(rsi, 1),
            "macd": "金叉" if macd_g else "死叉",
            "macd_w": "金叉" if wk_golden else "死叉",
            "ma50": round(ma50, 2), "ma200": round(ma200, 2) if has200 else None,
            "trend": trend,
            "stock_type": "💪强势" if is_strong else ("⚠️降级" if downgraded else "普通"),
            "vol_ratio": round(vol_ratio, 1),
            "vol_bar": df.index[-1].strftime("%Y-%m-%d") if vol_ratio > 2 else None,
            "today_events": today_events,
            "put_reason": put_reason, "put_strike_ref": strike_ref,
            "status": {
                "macd_g": bool(macd_g),
                "above_50": bool(above_50),
                "above_200": bool(above_200) if has200 else None,
                "rsi_zone": zone,
                "divergence": bool(divergence),
                "is_strong": bool(is_strong),
                "put_verdict": put_verdict,
            },
        }
    except Exception as e:
        return {"name": name, "ticker": ticker, "error": str(e)}


def diff_signals(r, old):
    """与上次运行的状态比较，返回变化列表。"""
    s = r["status"]; lines = []
    if s["macd_g"] != old.get("macd_g"):
        lines.append("⚡MACD金叉" if s["macd_g"] else "💀MACD死叉")
    if s["above_50"] != old.get("above_50"):
        lines.append(f"🚀突破50MA({r['ma50']})" if s["above_50"] else f"📉跌破50MA({r['ma50']})")
    if s["above_200"] is not None and old.get("above_200") is not None \
            and s["above_200"] != old["above_200"]:
        lines.append(f"🚀突破200MA({r['ma200']})" if s["above_200"] else f"📉跌破200MA({r['ma200']})")
    oz = old.get("rsi_zone", "neutral"); nz = s["rsi_zone"]
    if nz != oz:
        emoji = "🔴" if nz in ("ob", "ext_ob") else ("🟢" if nz in ("os", "ext_os") else "⚪")
        lines.append(f"{emoji}RSI {ZONE_NAME[oz]}→{ZONE_NAME[nz]}（{r['rsi']:.0f}）")
    if s["divergence"] and not old.get("divergence"):
        lines.append("🔄出现底背离")
    if s["is_strong"] != old.get("is_strong", s["is_strong"]):
        lines.append("💪升级为强势股（超买阈值改用85/90）" if s["is_strong"]
                     else "⚠️强势股降级（超买阈值改回70/80）")
    if r["vol_bar"] and old.get("vol_bar_reported") != r["vol_bar"]:
        lines.append(f"📊放量{r['vol_ratio']}x")
    return lines


def jepq_qqq_arb_signal():
    """JEPQ-QQQ Put套利信号: D-8 Prior Gap + Realized Vol 三级灯模型
    返回 (light, block, line)；light 是 🟢/🟡/🔴，block 是完整文字，line 是一行当前状态。失败返回 None。

    基于 964 个交易日回测（2022.5-2026.3）：
    - 主信号：D-8 prior gap（QQQ 8日收益 - JEPQ 8日收益）≤ +0.3%
    - 辅助信号：QQQ 20日 Realized Volatility
    - 🟢 绿灯（全仓）：D-8 gap ≤ +0.3% AND vol < 20%  → 历史 0/85 breach -2.5%
    - 🟡 黄灯（半仓）：D-8 gap ≤ +0.3% AND 20% ≤ vol < 25%
    - 🔴 红灯（暂停）：D-8 gap > +0.3% OR vol ≥ 25%
    """
    try:
        qqq = yf.download('QQQ', period='2mo', progress=False)
        jepq = yf.download('JEPQ', period='2mo', progress=False)
        qqq.columns = [c[0] if isinstance(c, tuple) else c for c in qqq.columns]
        jepq.columns = [c[0] if isinstance(c, tuple) else c for c in jepq.columns]

        df = pd.DataFrame({
            'qqq_close': qqq['Close'], 'jepq_close': jepq['Close']
        }).dropna()
        if len(df) < 21:
            return None

        qqq_price = float(df['qqq_close'].iloc[-1])
        jepq_price = float(df['jepq_close'].iloc[-1])
        d8_gap = (qqq_price / float(df['qqq_close'].iloc[-9]) - 1) \
            - (jepq_price / float(df['jepq_close'].iloc[-9]) - 1)
        d7_gap = (qqq_price / float(df['qqq_close'].iloc[-8]) - 1) \
            - (jepq_price / float(df['jepq_close'].iloc[-8]) - 1)
        qqq_rets = df['qqq_close'].pct_change().dropna()
        rvol_20 = float(qqq_rets.tail(20).std() * np.sqrt(252))

        THRESHOLD = 0.003  # +0.3%
        if d8_gap <= THRESHOLD and rvol_20 < 0.20:
            light, signal = "🟢", "🟢 条件较好 — D-8 gap 未超阈值，且波动率低于 20%"
        elif d8_gap <= THRESHOLD and rvol_20 < 0.25:
            light, signal = "🟡", "🟡 条件一般 — D-8 gap 未超阈值，波动率为 20%—25%"
        elif d8_gap > THRESHOLD:
            light, signal = "🔴", "🔴 暂停观察 — D-8 gap 超过阈值"
        else:
            light, signal = "🔴", "🔴 暂停观察 — 波动率不低于 25%"

        block = "\n".join([
            "**JEPQ—QQQ Put 价差观察**",
            f"QQQ {qqq_price:.2f} | JEPQ {jepq_price:.2f}",
            f"D-8 gap：{d8_gap*100:+.2f}%（阈值 ≤+0.3%）｜D-7 gap：{d7_gap*100:+.2f}%",
            f"QQQ 20D RVol：{rvol_20*100:.1f}%",
            f"**当前判断：{signal}**",
            "这只是规则筛选结果，不是独立的仓位指令。",
        ])
        line = (f"JEPQ—QQQ：{signal}｜D-8 gap {d8_gap*100:+.2f}%（阈值 ≤+0.3%）"
                f"｜QQQ 20D RVol {rvol_20*100:.1f}%")
        return light, block, line
    except Exception:
        return None


def snapshot_line(r):
    s = r["status"]
    ma200 = f"{r['ma200']}{'✅' if s['above_200'] else '❌'}" if r["ma200"] else "n/a"
    return (f"{r['name']}({r['ticker']}) {r['trend']}[{r['stock_type']}] {r['price']} "
            f"今日{r['chg_1d']:+.1f}% 30日{r['chg_30d']:+.1f}%｜RSI={r['rsi']}({ZONE_NAME[s['rsi_zone']]})｜"
            f"MACD日{r['macd']}周{r['macd_w']}｜50MA={r['ma50']}{'✅' if s['above_50'] else '❌'} "
            f"200MA={ma200}｜Put:{s['put_verdict']}")


def main():
    feishu_target = resolve_feishu_target()
    if not feishu_target:
        print(
            "ERROR: 未配置 DALTON_DATA_TASK_FEISHU_ASIA_TARGET（兼容 DALTON_FEISHU_ASIA_TARGET；请设置环境变量或在 config/data_tasks.env 中配置）",
            file=sys.stderr,
        )
        sys.exit(1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = None
    prev = (state or {}).get("tickers", {})
    first_run = state is None

    results, failed = [], []
    for ticker, name in WATCHLIST.items():
        r = analyze(ticker, name)
        (failed if "error" in r else results).append(r)

    blocks, put_changes = [], []
    for r in sorted(results, key=lambda x: -abs(x["chg_1d"])):
        old = prev.get(r["ticker"])
        if old is None:
            lines = list(r["today_events"])
            if r["vol_bar"]:
                lines.append(f"📊放量{r['vol_ratio']}x")
        else:
            lines = diff_signals(r, old)
            if r["status"]["put_verdict"] != old.get("put_verdict"):
                tail = f"｜{r['put_strike_ref']}" if r["put_strike_ref"] else ""
                put_changes.append(f"{r['name']}({r['ticker']}) "
                                   f"{old.get('put_verdict')}→{r['status']['put_verdict']}"
                                   f"｜{r['put_reason']}{tail}")
        if lines:
            blocks.append(f"**{r['name']}({r['ticker']})** {r['price']} 今日{r['chg_1d']:+.1f}%"
                          f"｜RSI={r['rsi']}｜50MA{'✅' if r['status']['above_50'] else '❌'}"
                          f"\n　➡️ {'｜'.join(lines)}")

    # === JEPQ 三级灯：变色才报 ===
    jepq = jepq_qqq_arb_signal()
    old_light = (state or {}).get("jepq_light")
    jepq_message, jepq_block, new_light = "", "", old_light
    jepq_line = "JEPQ—QQQ：本次取数失败"
    if jepq:
        new_light, jepq_block, jepq_line = jepq
        if old_light is None:
            jepq_message = jepq_block
        elif new_light != old_light:
            jepq_message = f"JEPQ—QQQ 信号灯由 {old_light} 转为 {new_light}\n{jepq_block}"

    # === 组装主消息 ===
    if not blocks and not put_changes:
        fail_note = f"，{len(failed)}只取数失败" if failed else ""
        main_message = f"重点标的技术观察｜{now}｜无新信号（覆盖{len(results)}只{fail_note}）"
    else:
        parts = [f"**重点标的技术观察｜{now}**"]
        if first_run:
            parts.append("（信号基线已建立，此后只报相对上次运行的变化；以下为今日当日事件）")
        parts += blocks
        if put_changes:
            parts.append("**Sell Put 分组变动**")
            parts += ["　" + p for p in put_changes]
        if failed:
            parts.append("⚠️ 数据获取失败：" + "、".join(f"{f['name']}({f['ticker']})" for f in failed))
        main_message = "\n".join(parts)
    # 每次附一行 JEPQ 当前状态；变色时另发完整说明，这一行就不重复
    if not jepq_message:
        main_message = f"{main_message}\n{jepq_line}"

    # === 全量快照写文件（不发送，随时可查）===
    snap = [f"重点标的技术观察 全量快照｜{now}", ""]
    snap += [snapshot_line(r) for r in results]
    if failed:
        snap.append("")
        snap += [f"❌ {f['name']}({f['ticker']}): {f['error']}" for f in failed]
    if jepq_block:
        snap += ["", jepq_block]
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w') as f:
        f.write("\n".join(snap))

    # === 保存 state（缺数的 ticker 保留旧状态，避免下次误报转换）===
    tickers_state = dict(prev)
    for r in results:
        entry = dict(r["status"])
        old = prev.get(r["ticker"], {})
        if r["vol_bar"]:
            entry["vol_bar_reported"] = r["vol_bar"]
        elif old.get("vol_bar_reported"):
            entry["vol_bar_reported"] = old["vol_bar_reported"]
        tickers_state[r["ticker"]] = entry
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"updated_at": datetime.now().isoformat(),
                   "tickers": tickers_state, "jepq_light": new_light},
                  f, ensure_ascii=False)

    print(json.dumps({
        "ok": True,
        "main_message": main_message,
        "jepq_message": jepq_message,
        "feishu_target": feishu_target,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
