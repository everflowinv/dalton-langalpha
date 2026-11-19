#!/usr/bin/env python3
"""A 股底部放量盘中监控：池子里的票当日换手超过阈值、且昨收在一年区间低位时提醒。

由 cron command payload 每 2 分钟调用一次；没有新提醒时只打印 NO_REPLY（cron 静默）。
同一只票一天只提醒一次。行情的日期不是今天（周末、节假日）时直接静默。

用法：
  python3 scripts/a_share_volume_watch.py              # cron 模式（只在 9:25-11:35、12:59-15:10 工作）
  python3 scripts/a_share_volume_watch.py --dry-run    # 不看时段、不写状态，打印现在会提醒什么
  python3 scripts/a_share_volume_watch.py --backtest 120   # 用日线回看过去 N 个交易日会触发几次
  python3 scripts/a_share_volume_watch.py --self-test

配置：config/a_share_volume_watch.json（turnover_min 换手阈值 %，pos_max 昨收在一年高低区间的
位置上限，0 为最低；r5_max 前 5 个交易日涨幅上限，超过算已经拉起来；peer_ratio 同组"跟着放量"的判定倍数；pools 分组 → 带 sh/sz 前缀的代码）。
状态：state/a-share-volume-watch/（当日基准、已提醒名单、连续失败计数、日志）。
测试时可用环境变量 A_SHARE_VOLUME_WATCH_CONFIG / A_SHARE_VOLUME_WATCH_STATE 指向临时配置和状态目录。
行情：腾讯 qt.gtimg.cn 快照（字段 38 换手率）+ proxy.finance.qq.com 前复权日线。东财主机封了本机 IP，别换回去。
兼容 Python 3.9（cron 走 /usr/bin/python3）。
"""
import argparse, fcntl, json, os, statistics as st, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.environ.get("A_SHARE_VOLUME_WATCH_CONFIG") or os.path.join(ROOT, "config", "a_share_volume_watch.json")
STATE = os.environ.get("A_SHARE_VOLUME_WATCH_STATE") or os.path.join(ROOT, "state", "a-share-volume-watch")
# In a data-task container 127.0.0.1 is the container itself; reach the host proxy instead.
PROXY = os.environ.get("DALTON_LOCAL_PROXY") or (
    "http://host.docker.internal:7897" if os.path.exists("/.dockerenv") else "http://127.0.0.1:7897"
)
TZ = ZoneInfo("Asia/Shanghai")
FAIL_ANNOUNCE = 8          # 交易时段连续失败这么多次（约 16 分钟）才报错
KEEP_DAYS = 10


def log(msg):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "run.log"), "a") as f:
        f.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}\n")


def curl(url, timeout=12):
    for extra in (["-x", PROXY], []):
        r = subprocess.run(["curl", "-s", "--max-time", str(timeout)] + extra + [url], capture_output=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    raise RuntimeError(f"curl failed: {url[:80]}")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG, None)
    if not cfg or not cfg.get("pools"):
        raise SystemExit(f"配置缺失或为空：{CONFIG}")
    group = {}
    for g, s in cfg["pools"].items():
        for c in s.split():
            group.setdefault(c, g)
    return cfg, group


def snapshot(codes):
    """腾讯快照。返回 code -> 行情；ts 是行情时间 YYYYMMDDhhmmss。"""
    out = {}
    for i in range(0, len(codes), 50):
        raw = curl("http://qt.gtimg.cn/q=" + ",".join(codes[i:i + 50])).decode("gbk", "replace")
        for line in raw.strip().split(";"):
            if "~" not in line:
                continue
            f = line.split("~")
            code = line.split("=")[0].strip().replace("v_", "")
            try:
                out[code] = {"name": f[1].replace(" ", ""), "price": float(f[3] or 0), "prev": float(f[4] or 0),
                             "chg": float(f[32] or 0), "ts": f[30], "turnover": float(f[38] or 0),
                             "amount_yi": float(f[37] or 0) / 1e4, "mcap": float(f[45] or 0),
                             "limit_up": float(f[47] or 0) if f[47] else 0.0}
            except (IndexError, ValueError):
                continue
    if not out:
        raise RuntimeError("snapshot empty")
    return out


def kline(code, n):
    url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           f"?param={code},day,2000-01-01,2099-12-31,{n},qfq")
    d = json.loads(curl(url, 20))["data"][code]
    return d.get("qfqday") or d.get("day") or []


def base_from_rows(rows):
    """rows 只含今天之前的日线。返回昨收位置、近 60 日均换手、前 5 日涨幅。"""
    if len(rows) < 60:
        return None
    rows = rows[-250:]
    c = [float(r[2]) for r in rows]; hi = [float(r[3]) for r in rows]; lo = [float(r[4]) for r in rows]
    to = [float(r[7] or 0) for r in rows if len(r) > 7]
    h, l = max(hi), min(lo)
    return {"date": rows[-1][0], "prev_close": c[-1], "h250": h, "l250": l, "bars": len(rows),
            "pos": (c[-1] - l) / (h - l) if h > l else None,
            "to60": st.mean(to[-60:]) if to else None,
            "r5": c[-1] / c[-6] - 1 if len(c) > 5 else None}


def build_base(codes, today):
    def one(code):
        try:
            rows = [r for r in kline(code, 300) if r[0] < today]
            return code, base_from_rows(rows)
        except Exception as e:  # noqa: BLE001 — 单只失败不影响其他，下一轮重试
            log(f"base {code} failed: {e}")
            return code, None
    with ThreadPoolExecutor(8) as ex:
        return {c: b for c, b in ex.map(one, codes) if b}


def is_bottom(b, cfg):
    """昨收在一年区间低位，且前 5 天还没拉起来（汉威科技这类低位已经连涨的不算）。"""
    if b.get("pos") is None or b["pos"] > cfg["pos_max"]:
        return False
    return b.get("r5") is None or b["r5"] <= cfg.get("r5_max", 0.15)


def in_session(now):
    hm = now.hour * 100 + now.minute
    return now.weekday() < 5 and (925 <= hm <= 1135 or 1259 <= hm <= 1510)


def cleanup(today):
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for fn in os.listdir(STATE):
        parts = fn.rsplit("-", 1)
        if len(parts) == 2 and parts[1].endswith(".json") and parts[1][:8].isdigit() and parts[1][:8] < cutoff:
            os.remove(os.path.join(STATE, fn))


def recent_hits(code, tag):
    """之前几天（状态里最多保留 KEEP_DAYS 天）这只票真正提醒过几次。"""
    n = 0
    for fn in os.listdir(STATE):
        if fn.startswith("alerted-") and fn < f"alerted-{tag}.json":
            rec = load_json(os.path.join(STATE, fn), {}).get(code)
            n += bool(rec and not rec.get("skipped"))
    return n


def pct(x):
    return f"{x * 100:+.1f}%"


def render(hits, quotes, base, group, cfg, now, tag):
    lines = [f"底部放量提醒 · {now.month}月{now.day}日 {now:%H:%M}", ""]
    ratio = cfg.get("peer_ratio", 1.5)
    for code in hits:
        q, b, g = quotes[code], base[code], group[code]
        to60 = b.get("to60") or 0
        if not to60:
            vol = f"换手 {q['turnover']:.1f}%"
        elif q["turnover"] / to60 < 1.5:
            vol = f"换手 {q['turnover']:.1f}%，近 60 日平均 {to60:.1f}%，只有平时的 {q['turnover'] / to60:.1f} 倍，它平时换手就高"
        else:
            vol = f"换手 {q['turnover']:.1f}%，是近 60 日平均 {to60:.1f}% 的 {q['turnover'] / to60:.1f} 倍"
        if q["chg"] < 0:
            move = f"跌 {-q['chg']:.2f}%" + ("，是放量下跌" if to60 and q["turnover"] >= 1.5 * to60 else "")
        elif q["chg"] == 0:
            move = "平盘"
        else:
            move = f"涨 {q['chg']:.2f}%" + ("，涨停" if q["limit_up"] and q["price"] >= q["limit_up"] - 1e-6 else "")
        peers = []
        for c2, g2 in group.items():
            if g2 != g or c2 == code or c2 not in quotes or c2 not in base or "ST" in quotes[c2]["name"]:
                continue
            t2, a2 = quotes[c2]["turnover"], base[c2].get("to60") or 0
            if a2 and t2 >= ratio * a2:
                peers.append((t2 / a2, f"{quotes[c2]['name']} {t2:.1f}%"))
        peers = [x[1] for x in sorted(peers, reverse=True)[:5]]
        n_group = sum(1 for g2 in group.values() if g2 == g)
        before = recent_hits(code, tag)
        r5 = f"，之前 5 天{'涨' if b['r5'] >= 0 else '跌'} {abs(b['r5']) * 100:.1f}%" if b.get("r5") is not None else ""
        lines += [f"{q['name']} {code[2:]}（{g}），近 {KEEP_DAYS} 天" + (f"第 {before + 1} 次触发" if before else "首次触发"),
                  f"{vol}；{move}，现价 {q['price']:.2f}",
                  f"昨收在一年区间的 {b['pos'] * 100:.0f}% 位置{r5}；成交 {q['amount_yi']:.1f} 亿，市值 {q['mcap']:.0f} 亿",
                  (f"同组 {n_group} 只里换手超过平时 {ratio:g} 倍的：" + "、".join(peers)) if peers
                  else f"同组 {n_group} 只里没有别的票跟着放量", ""]
    lines.append(f"规则：换手 ≥ {cfg['turnover_min']:g}%，昨收在一年区间 {cfg['pos_max'] * 100:.0f}% 以下，"
                 f"之前 5 天涨幅不超过 {cfg.get('r5_max', 0.15) * 100:.0f}%，不含 ST；同一只票一天只提醒一次。放量原因没核，要看公告。")
    return "\n".join(lines)


def run(dry):
    os.makedirs(STATE, exist_ok=True)
    now = datetime.now(TZ)
    if not dry and not in_session(now):
        print("NO_REPLY"); return 0
    lock = open(os.path.join(STATE, "run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("NO_REPLY"); return 0
    cfg, group = load_config()
    codes = list(group)
    health_path = os.path.join(STATE, "health.json")
    health = load_json(health_path, {"fails": 0})
    if health.get("day") != now.strftime("%Y%m%d"):
        health["fails"] = 0
    try:
        quotes = snapshot(codes)
    except Exception as e:  # noqa: BLE001
        health["fails"] = health.get("fails", 0) + 1
        health["day"] = now.strftime("%Y%m%d")
        health["last_error"] = f"{now:%m-%d %H:%M} {e}"
        if not dry:
            save_json(health_path, health)
        log(f"snapshot failed ({health['fails']}): {e}")
        if health["fails"] == FAIL_ANNOUNCE:
            print(f"底部放量监控：腾讯行情已连续约 {FAIL_ANNOUNCE * 2} 分钟取不到数据，这段时间的放量没法提醒，恢复后自动继续。")
            return 1
        print("NO_REPLY"); return 1 if health["fails"] > FAIL_ANNOUNCE else 0
    if health.get("fails"):
        log(f"recovered after {health['fails']} fails")
    health = {"fails": 0, "day": now.strftime("%Y%m%d"), "last_ok": f"{now:%Y-%m-%d %H:%M}"}
    if not dry:
        save_json(health_path, health)

    today, tag = now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d")
    live = {c: q for c, q in quotes.items() if q["ts"][:8] == tag and q["price"] > 0}
    if not live and not dry:
        print("NO_REPLY"); return 0          # 休市日
    if dry and not live:                     # 周末 dry-run：用最近一个交易日的行情演示
        live = {c: q for c, q in quotes.items() if q["price"] > 0}
        today = max(q["ts"][:8] for q in live.values())
        today = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        tag = today.replace("-", "")

    base_path = os.path.join(STATE, f"base-{tag}.json")
    base = load_json(base_path, {})
    missing = [c for c in codes if c not in base]
    if missing:
        base.update(build_base(missing, today))
        if not dry:
            save_json(base_path, base)
    alerted_path = os.path.join(STATE, f"alerted-{tag}.json")
    alerted = load_json(alerted_path, {})

    hits, high_pos = [], []
    for code in codes:
        q, b = live.get(code), base.get(code)
        if not q or not b or code in alerted or q["turnover"] < cfg["turnover_min"]:
            continue
        if not is_bottom(b, cfg) or "ST" in q["name"]:
            high_pos.append(code); continue
        hits.append(code)
    for code in high_pos:
        alerted[code] = {"time": f"{now:%H:%M}", "turnover": live[code]["turnover"], "skipped": "not_bottom",
                         "pos": round(base[code]["pos"], 3) if base[code].get("pos") is not None else None}
    for code in hits:
        alerted[code] = {"time": f"{now:%H:%M}", "turnover": live[code]["turnover"]}
    if not dry and (hits or high_pos):
        save_json(alerted_path, alerted)
        log("hits " + ",".join(hits) + (" | not_bottom " + ",".join(high_pos) if high_pos else ""))
        cleanup(today)
    if dry:
        skipped = ", ".join("%s(%.1f%%, pos %.2f)" % (live[c]["name"], live[c]["turnover"], base[c]["pos"])
                            for c in high_pos) or "无"
        print(f"[dry-run] 行情日 {today}，有效行情 {len(live)}/{len(codes)}，基准 {len(base)}/{len(codes)}；"
              f"换手达标但不在底部：{skipped}")
    if not hits:
        print("NO_REPLY"); return 0
    hits.sort(key=lambda c: -live[c]["turnover"])
    print(render(hits, live, base, group, cfg, now, tag))
    return 0


def backtest(n):
    cfg, group = load_config()
    codes = list(group)
    def one(code):
        try:
            return code, kline(code, 250 + n + 5)
        except Exception:  # noqa: BLE001
            return code, []
    with ThreadPoolExecutor(8) as ex:
        data = dict(ex.map(one, codes))
    per_day, per_code, raw_day = {}, {}, {}
    for code, rows in data.items():
        if len(rows) < 120:
            continue
        for i in range(max(60, len(rows) - n), len(rows)):
            to = float(rows[i][7] or 0)
            if to < cfg["turnover_min"]:
                continue
            raw_day.setdefault(rows[i][0], []).append(code)
            b = base_from_rows(rows[:i])
            if b and is_bottom(b, cfg):
                per_day.setdefault(rows[i][0], []).append(code)
                per_code[code] = per_code.get(code, 0) + 1
    days = sorted({r[0] for rows in data.values() for r in rows[-n:]})
    hit_days = [d for d in days if d in per_day]
    print(f"回看 {len(days)} 个交易日（{days[0]} 至 {days[-1]}），{len(codes)} 只")
    print(f"只看换手 ≥ {cfg['turnover_min']:g}%：{sum(len(v) for v in raw_day.values())} 次；"
          f"再加底部条件（昨收位置 ≤ {cfg['pos_max']:g}、前 5 天涨幅 ≤ {cfg.get('r5_max', 0.15):.0%}）：{sum(per_code.values())} 次，落在 {len(hit_days)} 天，"
          f"单日最多 {max((len(v) for v in per_day.values()), default=0)} 只")
    names = snapshot(codes)
    st_codes = {c for c, v in names.items() if "ST" in v["name"]}
    per_code = {c: k for c, k in per_code.items() if c not in st_codes}
    per_day = {d: [c for c in v if c not in st_codes] for d, v in per_day.items()}
    per_day = {d: v for d, v in per_day.items() if v}
    hit_days = [d for d in days if d in per_day]
    print(f"去掉 ST 后：{sum(per_code.values())} 次，落在 {len(hit_days)} 天")
    for code, k in sorted(per_code.items(), key=lambda x: -x[1])[:15]:
        print(f"  {code} {names.get(code, {}).get('name', '')}: {k} 次")
    for d in hit_days[-15:]:
        print(f"  {d}: " + ", ".join(names.get(c, {}).get("name", c) for c in per_day[d]))
    return 0


def self_test():
    cfg, group = load_config()
    assert cfg["turnover_min"] > 0 and 0 < cfg["pos_max"] <= 1, "阈值不合理"
    q = snapshot(list(group)[:3])
    assert q and all("turnover" in v for v in q.values()), "快照解析失败"
    code = next(iter(q))
    b = base_from_rows(kline(code, 300)[:-1])
    assert b and b["pos"] is not None and b["to60"] is not None, "日线基准计算失败"
    t = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
    assert in_session(t) and not in_session(t.replace(hour=12)) and not in_session(t + timedelta(days=5))
    print(json.dumps({"ok": True, "codes": len(group), "sample": {code: q[code]["turnover"]}, "base": b},
                     ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backtest", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.backtest:
        return backtest(a.backtest)
    try:
        return run(a.dry_run)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — 记日志后按失败退出，交给 cron failure alert
        log(f"unexpected: {e!r}")
        print("NO_REPLY")
        return 1


if __name__ == "__main__":
    sys.exit(main())
