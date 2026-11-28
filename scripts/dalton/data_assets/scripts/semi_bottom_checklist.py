#!/usr/bin/env python3
"""半导体见底清单（参照 IGV 2026 年 2–4 月见底过程）。

每个美股交易日开盘一小时后跑一次，输出一条 Discord 文本：
  - 今日盘中：SMH/SOXX 价格、日 RSI、按时点折算的全天成交量倍数、成分股广度
  - 昨日收盘确认的投降条件（盘中成交量折算有偏差，投降以收盘数据记账）
  - 测试期特征（日线/周线背离）、普涨确认、路径一（V 形）条件
  - 仓位：读当天 08:00 ET 半导体环境追踪的 snapshot.json

用法：
  python3 -I scripts/semi_bottom_checklist.py run [--dry-run] [--force] [--json]
  python3 -I scripts/semi_bottom_checklist.py self-test

非交易日输出 NO_REPLY（cron 不投递）。说明见 scripts/semi_bottom_checklist.md。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import sys
import traceback

WS = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = WS / "state" / "semi-bottom-checklist"
SNAP_DIR = WS / "research-outputs" / "semi-rescue-alert"

ETFS = ["SMH", "SOXX"]
COMPS = ("NVDA AVGO TSM MU TXN AMD ADI ASML KLAC QCOM MRVL AMAT LRCX INTC NXPI MPWR "
         "ALAB TER MCHP ARM ON COHR CRDO ENTG LSCC TSEM GFS SITM MTSI STM").split()
IDX = ["QQQ", "^VIX", "^VXN"]
PEAK_FROM = "2026-01-01"          # 本轮高点在此之后找（6/22）
JUL_LOW = "2026-07-29"            # 7/29 低点，路径一的收复位
RANGE_DD = (0.35, 0.40)           # 路径二目标区间：较高点 -35% 到 -40%
CAP = {"rsi": 25.0, "vol": 4.0, "br50": 3.0, "br200": 10.0}
THRUST = {"low": 15.0, "high": 70.0, "days": 7}
ET = dt.timezone(dt.timedelta(hours=-5))  # 只用于取“今天”的日期，精确时区下面用 zoneinfo


def now_et():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York"))


# ---------------------------------------------------------------- data
def download_daily(tickers, start):
    import pandas as pd
    import yfinance as yf
    end = (now_et().date() + dt.timedelta(days=1)).isoformat()
    df = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False,
                     group_by="ticker", threads=True)
    C, V = {}, {}
    for t in tickers:
        try:
            sub = df[t]
            c = sub["Close"].dropna()
            if c.empty:
                raise KeyError
        except Exception:
            one = yf.download(t, start=start, end=end, auto_adjust=True, progress=False)
            if one.empty:
                continue
            if isinstance(one.columns, pd.MultiIndex):
                one.columns = one.columns.get_level_values(0)
            sub = one
            c = sub["Close"].dropna()
        C[t] = c
        V[t] = sub["Volume"].reindex(c.index)
    C = pd.DataFrame(C)
    V = pd.DataFrame(V)
    C.index = pd.to_datetime(C.index).tz_localize(None).normalize()
    V.index = C.index
    return C, V


def fix_splits(C, V):
    """yfinance 在拆股当天可能出现历史未调整、当天已调整的断层；按断层倍数把之前的数据换到拆股后口径。"""
    notes = []
    for t in C.columns:
        s = C[t].dropna()
        r = s / s.shift(1)
        for day, ratio in r.items():
            if ratio is None or not (ratio < 0.45 or ratio > 2.2):
                continue
            k = 1 / ratio if ratio < 1 else ratio
            for f in (2, 3, 4, 5, 10, 20):
                if abs(k - f) / f < 0.08:
                    if ratio < 1:
                        C.loc[C.index < day, t] /= f
                        V.loc[V.index < day, t] *= f
                    else:
                        C.loc[C.index < day, t] *= f
                        V.loc[V.index < day, t] /= f
                    notes.append(f"{t} {day.date()} 1 拆 {f}" if ratio < 1 else f"{t} {day.date()} {f} 并 1")
                    break
    return notes


def intraday(etfs):
    """返回 {etf: (last_price, last_ts, today_volume, frac_of_day_by_now)}。"""
    import numpy as np
    import yfinance as yf
    h = yf.download(etfs, period="60d", interval="5m", progress=False, group_by="ticker", prepost=False)
    out = {}
    for t in etfs:
        x = h[t].dropna(subset=["Close"])
        if x.empty:
            continue
        idx = x.index.tz_convert("America/New_York")
        x = x.set_axis(idx)
        last = idx[-1]
        today = x[x.index.date == last.date()]
        cut = last.time()
        days = sorted(set(x.index.date))[:-1][-20:]
        fr = []
        for d in days:
            dv = x["Volume"][x.index.date == d]
            if dv.sum() > 0:
                fr.append(dv[dv.index.time <= cut].sum() / dv.sum())
        out[t] = (float(today["Close"].iloc[-1]), last, float(today["Volume"].sum()),
                  float(np.median(fr)) if fr else float("nan"))
    return out


# ---------------------------------------------------------------- indicators
def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def breadth(C, comps, win):
    sub = C[comps]
    ma = sub.rolling(win, min_periods=int(win * 0.9)).mean()
    valid = ma.notna()
    return ((sub > ma) & valid).sum(axis=1) / valid.sum(axis=1) * 100


def new_low_share(C, comps, win=20):
    sub = C[comps]
    mn = sub.rolling(win).min()
    valid = mn.notna()
    return ((sub <= mn) & valid).sum(axis=1) / valid.sum(axis=1) * 100


def etf_frame(C, V, t):
    import numpy as np
    import pandas as pd
    c = C[t].dropna()
    v = V[t].reindex(c.index)
    f = pd.DataFrame(index=c.index)
    f["close"] = c
    f["rsi"] = rsi(c)
    f["ma20"] = c.rolling(20).mean()
    f["ma50"] = c.rolling(50).mean()
    f["ma200"] = c.rolling(200).mean()
    f["v50"] = v.rolling(50).mean().shift(1)
    f["volx"] = v / f["v50"]
    f["rv20"] = c.pct_change().rolling(20).std() * np.sqrt(252) * 100
    return f


def weekly_rsi(c):
    w = c.resample("W-FRI").last()
    return rsi(w)


# ---------------------------------------------------------------- positioning
def load_positioning():
    snaps = sorted(SNAP_DIR.glob("*/snapshot.json"))
    if not snaps:
        return None, {}
    p = snaps[-1]
    try:
        d = json.loads(p.read_text())
    except Exception:
        return p.parent.name, {}
    by = {x.get("id"): x for x in d.get("indicators", []) if isinstance(x, dict)}
    return p.parent.name, by


# ---------------------------------------------------------------- state
def load_state():
    f = STATE_DIR / "state.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def save_state(st, row):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))
    with open(STATE_DIR / "history.jsonl", "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- formatting
def pct(x, nd=1, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:+.{nd}f}%" if sign else f"{x:.{nd}f}%"


def num(x, nd=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def tick(ok):
    return "✅" if ok else "⬜"


def cap_count(rsi_v, volx, br50, br200):
    flags = [rsi_v < CAP["rsi"], volx >= CAP["vol"], br50 <= CAP["br50"], br200 <= CAP["br200"]]
    return sum(bool(x) for x in flags), flags


# ---------------------------------------------------------------- main run
def build(force=False):
    import numpy as np
    import pandas as pd

    now = now_et()
    today = pd.Timestamp(now.date())
    tickers = ETFS + COMPS + IDX
    C, V = download_daily(tickers, start="2025-06-01")
    split_notes = fix_splits(C, V)
    last_bar = C.index[-1]
    if last_bar != today and not force:
        return {"skip": True, "reason": f"今天没有美股交易数据（最近一根日线 {last_bar.date()}）"}

    intra = intraday(ETFS)
    live = last_bar == today
    # 今日价格和成交量用日线的盘中累计值（和历史同口径，拆股已调整）；
    # 5 分钟线只用来算“到这个时点通常已成交全天的几成”。SOXX 的 5 分钟成交量 2026-11 拆股后口径错乱，不能直接用。
    proj = {}
    for t in ETFS:
        if live and t in intra:
            frac = intra[t][3]
            vol_today = float(V.loc[today, t])
            proj[t] = (vol_today / frac) if frac and frac > 0 else float("nan")
    comps = [t for t in COMPS if t in C.columns and C[t].dropna().size > 220]

    b20 = breadth(C, comps, 20)
    b50 = breadth(C, comps, 50)
    b200 = breadth(C, comps, 200)
    nl = new_low_share(C, comps, 20)

    res = {"asof_et": now.strftime("%Y-%m-%d %H:%M"), "live": bool(live), "split_notes": split_notes,
           "n_comps": len(comps), "etf": {}}
    frames = {}
    for t in ETFS:
        f = etf_frame(C, V, t)
        frames[t] = f
        c = f["close"]
        pk = c.loc[PEAK_FROM:].max()
        pkd = c.loc[PEAK_FROM:].idxmax()
        jl = float(c.loc[JUL_LOW]) if pd.Timestamp(JUL_LOW) in c.index else float("nan")
        cur = f.iloc[-1]
        prev = f.iloc[-2]
        volx_today = (proj.get(t, float("nan")) / cur["v50"]) if live else cur["volx"]
        wr = weekly_rsi(c)
        hist = f.loc["2026-06-22":]
        lo_rng, hi_rng = pk * (1 - RANGE_DD[1]), pk * (1 - RANGE_DD[0])
        # 日线背离：今天收盘在 6/22 以来最低收盘的 2% 以内（或更低），RSI 高于那个低点的 RSI
        prior = hist.iloc[:-1]
        plo_d = prior["close"].idxmin()
        div_d = bool(cur["close"] <= prior.loc[plo_d, "close"] * 1.02 and cur["rsi"] > prior.loc[plo_d, "rsi"])
        # 周线：本周 RSI 与之前 12 周最低周 RSI
        wr_prior = wr.iloc[-13:-1]
        res["etf"][t] = {
            "price": float(cur["close"]), "chg": float(cur["close"] / prev["close"] - 1) * 100,
            "peak": float(pk), "peak_date": str(pkd.date()), "dd": float(cur["close"] / pk - 1) * 100,
            "range": [float(lo_rng), float(hi_rng)],
            "to_range_top": float(hi_rng / cur["close"] - 1) * 100,
            "jul_low": jl, "vs_jul": float(cur["close"] / jl - 1) * 100,
            "rsi": float(cur["rsi"]), "rsi_prev": float(prev["rsi"]),
            "rsi_min_since": float(hist["rsi"].iloc[:-1].min()), "rsi_min_date": str(hist["rsi"].iloc[:-1].idxmin().date()),
            "volx": float(volx_today), "volx_prev": float(prev["volx"]),
            "volx_max_since": float(hist["volx"].iloc[:-1].max()), "volx_max_date": str(hist["volx"].iloc[:-1].idxmax().date()),
            "ma20": float(cur["ma20"]), "ma50": float(cur["ma50"]), "ma200": float(cur["ma200"]),
            "vs50": float(cur["close"] / cur["ma50"] - 1) * 100, "vs200": float(cur["close"] / cur["ma200"] - 1) * 100,
            "rv20": float(cur["rv20"]),
            "wrsi": float(wr.iloc[-1]), "wrsi_prior_min": float(wr_prior.min()),
            "wrsi_last6": [round(float(x), 1) for x in wr.iloc[-6:]],
            "div_daily": div_d, "prior_low_date": str(plo_d.date()), "prior_low": float(prior.loc[plo_d, "close"]),
            "prior_low_rsi": float(prior.loc[plo_d, "rsi"]),
        }
    res["breadth"] = {
        "br20": float(b20.iloc[-1]), "br50": float(b50.iloc[-1]), "br200": float(b200.iloc[-1]), "nl20": float(nl.iloc[-1]),
        "br20_prev": float(b20.iloc[-2]), "br50_prev": float(b50.iloc[-2]), "br200_prev": float(b200.iloc[-2]),
        "br20_min7": float(b20.iloc[-THRUST["days"]:].min()),
        "br20_path": [round(float(x)) for x in b20.iloc[-8:]],
    }
    res["vol_idx"] = {k: float(C[k].dropna().iloc[-1]) for k in ["^VXN", "^VIX"] if k in C}
    res["qqq_chg"] = float(C["QQQ"].iloc[-1] / C["QQQ"].iloc[-2] - 1) * 100

    # 昨日收盘确认的投降条件（全天数据）
    yd = C.index[-2] if live else C.index[-1]
    smh_f = frames["SMH"]
    y_cnt, y_flags = cap_count(smh_f.loc[yd, "rsi"], smh_f.loc[yd, "volx"], b50.loc[yd], b200.loc[yd])
    res["yesterday"] = {"date": str(yd.date()), "rsi": float(smh_f.loc[yd, "rsi"]), "volx": float(smh_f.loc[yd, "volx"]),
                        "br50": float(b50.loc[yd]), "br200": float(b200.loc[yd]), "count": y_cnt}
    s = res["etf"]["SMH"]; b = res["breadth"]
    t_cnt, t_flags = cap_count(s["rsi"], s["volx"], b["br50"], b["br200"])
    res["today_cap"] = {"count": t_cnt, "flags": t_flags}
    x = res["etf"]["SOXX"]
    res["soxx_cap_count"] = cap_count(x["rsi"], x["volx"], b["br50"], b["br200"])[0]
    return res


def update_state(res, st):
    st = dict(st)
    y = res["yesterday"]
    # 投降以收盘数据记账：≥3/4 记为投降日
    if y["count"] >= 3:
        caps = st.get("capitulations", [])
        if y["date"] not in [c["date"] for c in caps]:
            caps.append({"date": y["date"], "count": y["count"], "rsi": round(y["rsi"], 1), "volx": round(y["volx"], 2),
                         "smh": None})
        st["capitulations"] = caps
    b = res["breadth"]
    if st.get("capitulations") and b["br20_min7"] <= THRUST["low"] and b["br20"] >= THRUST["high"] \
            and res["etf"]["SMH"]["vs50"] > 0 and not st.get("confirmed"):
        st["confirmed"] = res["asof_et"][:10]
    st["last"] = {"asof": res["asof_et"], "smh": res["etf"]["SMH"]["price"], "soxx": res["etf"]["SOXX"]["price"],
                  "rsi": res["etf"]["SMH"]["rsi"], "volx": res["etf"]["SMH"]["volx"], "br20": b["br20"],
                  "br50": b["br50"], "br200": b["br200"], "wrsi": res["etf"]["SMH"]["wrsi"], "today_cap": res["today_cap"]["count"]}
    return st


def stage_line(res, st):
    caps = st.get("capitulations", [])
    t = res["today_cap"]["count"]
    if st.get("confirmed"):
        return f"阶段：底部已确认（{st['confirmed']} 普涨并收回 50 日线），后面看回踩是否守住"
    if caps:
        c = caps[-1]
        return f"阶段：测试期。{c['date']} 收盘满足投降 {c['count']}/4，现在等成交和波动收敛、周 RSI 低点抬高，再出现普涨"
    if t >= 3:
        return f"阶段：盘中满足投降 {t}/4，按收盘数据确认（明天这条会写结果）"
    return f"阶段：下跌中，投降还没出现（盘中 {t}/4，昨日收盘 {res['yesterday']['count']}/4）"


def render(res, st, prev, pos_slot, pos):
    s, x, b = res["etf"]["SMH"], res["etf"]["SOXX"], res["breadth"]
    y = res["yesterday"]
    L = []
    L.append(f"**半导体见底清单｜{res['asof_et'][5:]} ET**（{'盘中' if res['live'] else '收盘后'}）")
    L.append(stage_line(res, st))
    L.append("")
    L.append(f"**价格**：SMH {num(s['price'],2)}（今日 {pct(s['chg'])}，距 {s['peak_date'][5:]} 高点 {pct(s['dd'])}）；"
             f"SOXX {num(x['price'],2)}（今日 {pct(x['chg'])}，距高点 {pct(x['dd'])}）；QQQ {pct(res['qqq_chg'])}")
    L.append(f"目标区间（较高点 -35% 到 -40%）：SMH {num(s['range'][0],0)}–{num(s['range'][1],0)}，"
             f"{'已在区间内' if s['price'] <= s['range'][1] else '再跌 ' + pct(-s['to_range_top'], sign=False) + ' 到上沿'}；"
             f"SOXX {num(x['range'][0],1)}–{num(x['range'][1],1)}，"
             f"{'已在区间内' if x['price'] <= x['range'][1] else '再跌 ' + pct(-x['to_range_top'], sign=False) + ' 到上沿'}")
    L.append("")
    tf = res["today_cap"]["flags"]
    L.append(f"**投降 {res['today_cap']['count']}/4（SMH 盘中）**")
    L.append(f"{tick(tf[0])} 日 RSI {num(s['rsi'])}（门槛 <25；SOXX {num(x['rsi'])}；6/22 以来最低 {num(s['rsi_min_since'])}，{s['rsi_min_date'][5:]}）")
    L.append(f"{tick(tf[1])} 成交 {num(s['volx'],1)} 倍 50 日均量（按时点折算全天，门槛 ≥4；SOXX {num(x['volx'],1)} 倍）")
    L.append(f"{tick(tf[2])} 站上 50 日线的成分股 {num(b['br50'],0)}%（门槛 ≤3%）")
    L.append(f"{tick(tf[3])} 站上 200 日线的成分股 {num(b['br200'],0)}%（门槛 ≤10%）")
    L.append(f"昨日收盘（{y['date'][5:]}）确认 {y['count']}/4：RSI {num(y['rsi'])}，成交 {num(y['volx'],1)} 倍，"
             f"50 日 {num(y['br50'],0)}%，200 日 {num(y['br200'],0)}%")
    L.append("")
    L.append("**测试和确认**")
    L.append(f"日线背离：{'有' if s['div_daily'] else '没有'}（6/22 以来收盘低点 {s['prior_low_date'][5:]} {num(s['prior_low'],2)}，"
             f"当时 RSI {num(s['prior_low_rsi'])}，现在 {num(s['rsi'])}）")
    wr = s["wrsi_last6"]
    wr_dir = "还在往下" if wr[-1] <= min(wr[:-1]) else "低点抬高"
    L.append(f"周 RSI：SMH 本周 {num(s['wrsi'])}，近 6 周 {' → '.join(f'{v:.0f}' for v in wr)}，{wr_dir}（IGV 见底时 26–30）")
    L.append(f"普涨：站上 20 日线 {num(b['br20'],0)}%（近 7 天最低 {num(b['br20_min7'],0)}%，门槛：7 天内从 ≤15% 到 ≥70%）；"
             f"SMH 对 50 日线 {pct(s['vs50'])}")
    L.append(f"波动：VXN {num(res['vol_idx'].get('^VXN'))}，SMH 20 日实际波动 {num(s['rv20'],0)}%（收敛才像最后一跌）；"
             f"创 20 日新低的成分股 {num(b['nl20'],0)}%")
    L.append("")
    p1 = [x["price"] > x["jul_low"], s["price"] > s["ma200"], b["br20"] >= THRUST["high"]]
    L.append(f"**路径一（V 形）{sum(p1)}/3**：{tick(p1[0])} SOXX 收回 7/29 低点 {num(x['jul_low'],2)}（{pct(x['vs_jul'])}）"
             f"｜{tick(p1[1])} SMH 收回 200 日线 {num(s['ma200'],1)}（{pct(s['vs200'])}）｜{tick(p1[2])} 站上 20 日线 ≥70%")
    if pos:
        def g(k, f="value"):
            it = pos.get(k) or {}
            return it.get(f) or "n/a"
        L.append("")
        L.append(f"**仓位**（{pos_slot} 环境追踪）：PB 净杠杆 {g('gs_pb')}（{g('gs_pb','asof')[5:]}）；"
                 f"AI 敞口 {g('ai_pb')}；纳指期货 {g('cftc_nq')}；AI 篮子空头 {g('ai_short')}（{g('ai_short','asof')[5:]}）")
    if prev:
        d_rsi = s["rsi"] - prev.get("rsi", float("nan"))
        L.append("")
        L.append(f"和上次（{prev.get('asof','')[5:]}）比：SMH {pct((s['price']/prev['smh']-1)*100)}，RSI {d_rsi:+.1f}，"
                 f"站上 50 日 {b['br50']-prev.get('br50',0):+.0f} 个点，站上 20 日 {b['br20']-prev.get('br20',0):+.0f} 个点")
    if res["split_notes"]:
        L.append(f"注：已按拆股调整历史数据（{'；'.join(res['split_notes'])}）")
    L.append("盘中数据，收盘会变；成交倍数按过去 20 天同一时点占全天比例折算，暴跌日开盘段会偏高。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="不写状态")
    r.add_argument("--force", action="store_true", help="非交易日也出结果")
    r.add_argument("--json", action="store_true")
    sub.add_parser("self-test")
    a = ap.parse_args()

    if a.cmd == "self-test":
        import pandas as pd
        s = pd.Series([1, 2, 3, 2, 1, 2, 3, 4, 5, 4, 3, 2, 3, 4, 5, 6], dtype=float)
        assert 0 < rsi(s).iloc[-1] < 100
        C = pd.DataFrame({"X": [30.0, 30.3, 10.2, 10.1]}, index=pd.date_range("2026-01-01", periods=4))
        V = pd.DataFrame({"X": [100.0, 100, 300, 300]}, index=C.index)
        n = fix_splits(C, V)
        assert n and abs(C["X"].iloc[0] - 10.0) < 1e-9 and V["X"].iloc[0] == 300, (n, C, V)
        print(json.dumps({"ok": True, "checks": ["rsi", "split_fix"]}))
        return 0

    try:
        res = build(force=a.force)
    except Exception as e:  # 让 cron 记失败
        traceback.print_exc()
        print(f"半导体见底清单取数失败：{e}", file=sys.stderr)
        return 1
    if res.get("skip"):
        print("NO_REPLY")
        print(res["reason"], file=sys.stderr)
        return 0
    st = load_state()
    prev = st.get("last")
    st2 = update_state(res, st)
    pos_slot, pos = load_positioning()
    text = render(res, st2, prev, pos_slot, pos)
    if not a.dry_run:
        save_state(st2, {"asof": res["asof_et"], "smh": res["etf"]["SMH"], "soxx": res["etf"]["SOXX"],
                         "breadth": res["breadth"], "today_cap": res["today_cap"], "yesterday": res["yesterday"]})
    if a.json:
        print(json.dumps({"ok": True, "text": text, "data": res}, ensure_ascii=False, default=str))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
