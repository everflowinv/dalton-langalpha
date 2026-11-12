#!/usr/bin/env python3
"""中国消费周报行情与事实库。

结论只来自缓存行情和已入库记录。脚本不推算未披露数字。
写入只走工作区相对路径，并拒绝符号链接。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover
    print(json.dumps({"ok": False, "error": "requests 未安装",
                      "hint": "沙箱镜像应已包含 requests"}, ensure_ascii=False))
    raise SystemExit(1)

VERSION = "1.2.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
REF_DIR = SKILL_DIR / "references"
DB_REL = "state/china-consumer-db"
PRICE_REL = DB_REL + "/prices"
SNAP_REL = DB_REL + "/snapshots"
NOTES_REL = DB_REL + "/notes.jsonl"
COMMODITY_REL = DB_REL + "/commodities"
RUNS_REL = DB_REL + "/collection-runs"
ARCHIVE_REL = "research-outputs/china-consumer-weekly"
HK = ZoneInfo("Asia/Hong_Kong")
WORKSPACE: Path | None = None

BENCH = {
    "A": {"symbol": "sh000300", "name": "沪深300"},
    "HK": {"symbol": "hkHSI", "name": "恒生指数"},
}
EXTRA_BENCH = [
    {"symbol": "sh000932", "name": "中证主要消费", "market": "A"},
    {"symbol": "hkHSCEI", "name": "恒生中国企业指数", "market": "HK"},
]

SOURCE_TYPES = ["sellside_note", "roadshow_minutes", "expert_call", "company_disclosure",
                "channel_data", "news", "community"]
EVIDENCE_GRADES = ["A", "B", "C"]
DIRECTIONS = ["positive", "negative", "neutral"]
DRIVERS = ["volume_price", "channel", "cost_margin", "new_product", "overseas",
           "penetration", "policy", "competition", "inventory", "capital_return", "other"]

COMMODITIES = [
    {"code": "TA0", "name": "PTA", "unit": "元/吨",
     "downstream": ["softdrink_water", "dairy", "snack_bakery"],
     "use": "PET 瓶片上游，包装水与软饮的包材成本"},
    {"code": "EG0", "name": "乙二醇", "unit": "元/吨",
     "downstream": ["softdrink_water"], "use": "PET 另一上游，与 PTA 一起看"},
    {"code": "P0", "name": "棕榈油", "unit": "元/吨",
     "downstream": ["snack_bakery", "instant_frozen", "restaurant_chain"],
     "use": "方便面、烘焙、连锁餐饮用油"},
    {"code": "Y0", "name": "豆油", "unit": "元/吨",
     "downstream": ["restaurant_chain", "instant_frozen"], "use": "餐饮与预制菜用油"},
    {"code": "SR0", "name": "白糖", "unit": "元/吨",
     "downstream": ["softdrink_water", "snack_bakery", "tea_coffee"],
     "use": "软饮、零食、茶饮的糖成本"},
    {"code": "CF0", "name": "棉花", "unit": "元/吨",
     "downstream": ["mass_apparel", "sportswear", "home_furnishing"],
     "use": "大众服饰、运动服饰与代工的原料"},
    {"code": "AU0", "name": "黄金", "unit": "元/克",
     "downstream": ["gold_jewelry"], "use": "黄金珠宝的原料与终端定价锚"},
    {"code": "M0", "name": "豆粕", "unit": "元/吨",
     "downstream": ["dairy", "pet"], "use": "饲料成本，传导到生鲜乳与宠物食品"},
    {"code": "C0", "name": "玉米", "unit": "元/吨",
     "downstream": ["dairy", "pet"], "use": "饲料另一主料，与豆粕一起看"},
    {"code": "JD0", "name": "鸡蛋", "unit": "元/500千克",
     "downstream": ["snack_bakery", "instant_frozen"], "use": "烘焙与预制菜原料"},
    {"code": "LH0", "name": "生猪", "unit": "元/吨",
     "downstream": ["restaurant_chain", "instant_frozen"], "use": "餐饮与肉制品原料"},
    {"code": "SP0", "name": "纸浆", "unit": "元/吨", "proxy_for": "包装用纸",
     "downstream": ["softdrink_water", "snack_bakery"],
     "use": "包装纸上游代理；不是瓦楞纸现货价，写报告时必须标明是代理指标"},
]
COMMODITY_GAPS = [
    {"item": "生鲜乳收购价", "reason": "国内无对应期货合约，统计局接口对本机 403",
     "fallback": "web_search 农业农村部畜牧兽医局周度主产省生鲜乳收购价，人工标日期与来源，C 级"},
    {"item": "瓦楞纸现货价", "reason": "无期货合约，卓创与纸业网需订阅",
     "fallback": "用纸浆期货 SP0 作方向代理并标明，或人工查证现货报价"},
    {"item": "白酒批价与渠道库存", "reason": "不是大宗品，属渠道数据",
     "fallback": "今日酒价 / 酒说，见 references/source-universe.yaml 的 channel_data.baijiu"},
]

BANNED_PHRASES = ["大幅增长", "显著提升", "爆发式增长", "明显改善", "持续向好", "大幅改善",
                  "强劲复苏", "全面提升", "遥遥领先"]

CALIBER_RULES = [
    {"key": "price_caliber",
     "trigger": r"(提价|涨价|降价|调价|价格|价盘|价格体系|价升)",
     "require": r"(批价|出厂价|终端零售价|零售价|成交价|挂牌价|开票价|吨价|均价|建议零售价|中标价|目标价)",
     "fix": "价格要写明是批价、出厂价还是终端零售价，三者的含义完全不同"},
    {"key": "shipment_caliber",
     "trigger": r"(销量|销售|走货|卖出|卖了)",
     "require": r"(动销|回款|发货|出货|终端动销|终端零售|渠道库存|收入确认|营业收入|营收|收入|"
                r"同店|系统销售|可比店|GMV|零售额|销售额)",
     "fix": "出货口径要写明是动销、回款还是发货，厂家发货不等于终端动销"},
    {"key": "guidance_caliber",
     "trigger": r"(指引|目标|展望|规划|有望|力争|冲刺)",
     "require": r"(指引|预测|测算|假设|管理层预期|尚未实现|非实际|规划目标|目标价)",
     "fix": "前瞻数字要标成指引或预测，与已发生的实际数分开写"},
]
CALIBER_NUM_WINDOW = 25
CALIBER_CTX_WINDOW = 40
DATE_PATTERNS = [r"20\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?",
                 r"\d{1,2}\s*月\s*\d{1,2}\s*日"]
PERIOD_MARKERS = (r"(20\d{2}|截至|口径|同比|环比|年初至今|至今|Q[1-4]|H[12]|YoY|QoQ|TTM|"
                  r"本周|上周|本月|上月|本季|上季|全年|上半年|下半年|一季|二季|三季|四季|"
                  r"周(涨|跌|收益|超额|回报|度)|月度|季度|年度|中枢|"
                  r"\d+\s*(周|日|天|个月|月|年|季)|近\d+|过去\d+|前\d+)")
PERIOD_WINDOW = 30
WINDOWS = {"1w": 7, "4w": 28, "13w": 91, "52w": 364}


class CliExit(SystemExit):
    """Exit the CLI after a JSON error has already been printed."""


def _card_io():
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_china_consumer_weekly"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("card helper is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fail(msg, hint, extra=None):
    out = {"ok": False, "error": msg, "hint": hint}
    if extra:
        out.update(extra)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    raise CliExit(1)


def emit(payload, as_json=True, text_fn=None):
    if as_json or text_fn is None:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(text_fn(payload))


def hk_today() -> date:
    return datetime.now(HK).date()


def anchor_friday(as_of=None) -> date:
    """Most recent Friday on or before the Hong Kong calendar date."""
    d = parse_date(as_of) if as_of else hk_today()
    return d - timedelta(days=(d.weekday() - 4) % 7)


def bind_workspace(raw: str | None) -> Path:
    global WORKSPACE
    if not raw:
        fail("missing --workspace-dir", "每个命令都要带 --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        fail("workspace is a symlink", "工作区不能是符号链接")
    if not path.is_dir():
        fail("workspace not found", "先创建工作区目录")
    WORKSPACE = path.resolve()
    return WORKSPACE


def rel_file(relative: str) -> Path:
    if WORKSPACE is None:
        fail("missing --workspace-dir", "每个命令都要带 --workspace-dir")
    return _card_io().workspace_file(WORKSPACE, relative)


def write_text_rel(relative: str, text: str) -> None:
    path = rel_file(relative)
    _card_io().write_bytes_exclusive(path, text.encode("utf-8"))


def write_json_rel(relative: str, payload, indent=None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    write_text_rel(relative, text)


def read_text_rel(relative: str) -> str | None:
    path = rel_file(relative)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_json_rel(relative: str):
    text = read_text_rel(relative)
    if text is None:
        return None
    return json.loads(text)


def dir_names(relative_dir: str, suffix: str) -> list[str]:
    """List regular files. A symlink directory or entry is refused."""
    folder = rel_file(relative_dir + "/.dir").parent
    if folder.is_symlink():
        raise ValueError("refusing symlink")
    if not folder.is_dir():
        return []
    names = []
    for child in folder.iterdir():
        if child.is_symlink():
            raise ValueError("refusing symlink")
        if child.name.endswith(suffix):
            names.append(child.name)
    return sorted(names)


def delivery_ok(args) -> bool:
    return (getattr(args, "content_kind", None) == "text"
            and bool(getattr(args, "delivery_confirmed", False))
            and getattr(args, "status", None) == "sent")


def require_delivery(args, action: str) -> None:
    if delivery_ok(args):
        return
    fail("未确认发送",
         f"{action} 需要 --content-kind text --delivery-confirmed --status sent",
         {"written": None})


def mask_dates(s):
    for p in DATE_PATTERNS:
        s = re.sub(p, lambda m: "〇" * len(m.group(0)), s)
    return s


def load_ref(name: str):
    if REF_DIR.is_symlink():
        raise ValueError("refusing symlink")
    path = REF_DIR / name
    if path.is_symlink():
        raise ValueError("refusing symlink")
    if not path.is_file():
        fail(f"缺少 {name}", "确认技能 references 目录完整")
    return json.loads(path.read_text(encoding="utf-8"))


def universe():
    return load_ref("universe.json")


def subsectors():
    return load_ref("subsectors.json")


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def pct(a, b):
    if b in (None, 0):
        return None
    return round((a / b - 1) * 100, 2)


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def _session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def fetch_kline(sess, symbol, bars=420):
    hk = symbol.startswith("hk")
    # A 股 fqkline 在 web.ifzq 会被 WAF 回 501；proxy.finance.qq.com 同一后端。
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/{}fqkline/get?param={},day,,,{},qfq"
        .format("hk" if hk else "", symbol, bars)
    )
    r = sess.get(url, timeout=25)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if not data:
        return []
    node = data.get(symbol) or next(iter(data.values()))
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for row in rows:
        try:
            out.append({"d": row[0], "o": float(row[1]), "c": float(row[2]),
                        "h": float(row[3]), "l": float(row[4]), "v": float(row[5])})
        except (ValueError, IndexError, TypeError):
            continue
    return out


def fetch_quote(sess, symbol):
    r = sess.get("https://qt.gtimg.cn/q=" + symbol, timeout=15)
    r.encoding = "gbk"
    if '="' not in r.text:
        return None
    p = r.text.split('"')[1].split("~")
    if len(p) < 46:
        return None
    try:
        cap = float(p[45])
    except ValueError:
        cap = None
    return {"name": re.sub(r"\s+", "", p[1]), "close": p[3], "mktcap_yi": cap}


def price_rel(symbol: str) -> str:
    return f"{PRICE_REL}/{symbol}.json"


def load_prices(symbol):
    return read_json_rel(price_rel(symbol))


def cmd_sync_prices(args):
    sess = _session()
    uni = universe()["companies"]
    targets = [(c["symbol"], c["name"], c["market"]) for c in uni]
    targets += [(b["symbol"], b["name"], m) for m, b in BENCH.items()]
    targets += [(b["symbol"], b["name"], b["market"]) for b in EXTRA_BENCH]

    ok, failed, skipped = [], [], []
    for symbol, name, market in targets:
        cached = load_prices(symbol)
        if cached and not args.force and cached.get("fetched_at") == date.today().isoformat():
            skipped.append(symbol)
            continue
        try:
            rows = fetch_kline(sess, symbol, args.bars)
        except Exception as exc:  # noqa: BLE001  # 单只行情源失败只记这一只，继续其余标的
            failed.append({"symbol": symbol, "name": name, "error": type(exc).__name__})
            continue
        if len(rows) < 60:
            failed.append({"symbol": symbol, "name": name, "error": "bars<60",
                           "bars": len(rows)})
            continue
        cap = None
        try:
            q = fetch_quote(sess, symbol)
            cap = q["mktcap_yi"] if q else None
        except Exception:  # noqa: BLE001  # 市值报价失败不中断行情入库，这一只的市值留空
            cap = None
        payload = {"symbol": symbol, "name": name, "market": market,
                   "fetched_at": date.today().isoformat(), "bars": len(rows),
                   "mktcap_yi": cap, "last_date": rows[-1]["d"], "rows": rows}
        write_json_rel(price_rel(symbol), payload)
        ok.append(symbol)
        time.sleep(0.03)

    res = {"ok": len(failed) == 0, "fetched": len(ok), "skipped_fresh": len(skipped),
           "failed": failed, "price_dir": PRICE_REL}
    if failed:
        res["error"] = "部分标的行情拉取失败"
        res["hint"] = "先重跑 sync-prices --force；仍失败的在报告里按「行情缺失」列出，不要静默跳过"
    emit(res, args.json)


def window_return(rows, start, end):
    """区间收益：用 < start 的最后一根收盘做基准，<= end 的最后一根收盘做终点。"""
    base = None
    last = None
    last_d = None
    for r in rows:
        d = r["d"]
        if d < start:
            base = r["c"]
        elif d <= end:
            last = r["c"]
            last_d = d
    if base is None or last is None:
        return None, None
    return pct(last, base), last_d


def latest_common_date(symbols):
    dates = []
    for s in symbols:
        p = load_prices(s)
        if p:
            dates.append(p["last_date"])
    return max(dates) if dates else None


def common_benchmark_date(limit):
    """Latest session on or before limit present on both A-share and HK benchmarks."""
    sets = []
    for spec in BENCH.values():
        payload = load_prices(spec["symbol"])
        if not payload:
            return None
        dates = {row["d"] for row in payload["rows"] if row["d"] <= limit}
        if not dates:
            return None
        sets.append(dates)
    common = set.intersection(*sets)
    return max(common) if common else None


def collection_windows(as_of, price_as_of=None):
    """Price window ends on the common session. Source window ends at this HK run."""
    friday = anchor_friday(as_of or price_as_of)
    now = datetime.now(HK)
    return {
        "price_window": {
            "start": (friday - timedelta(days=7)).isoformat(),
            "end": price_as_of or friday.isoformat(),
            "friday": friday.isoformat(),
            "price_as_of": price_as_of,
            "note": "行情窗口对齐当周周五，终点是两市共同交易日 price_as_of。",
        },
        "source_window": {
            "start": (friday - timedelta(days=5)).isoformat(),
            "end": now.date().isoformat(),
            "end_at": now.isoformat(timespec="seconds"),
            "note": "信源窗口从上周日到本次采集时刻（香港时间），覆盖周五盘后与周末。",
        },
    }


def build_perf(as_of=None):
    uni = universe()["companies"]
    subs = {s["key"]: s for s in subsectors()["subsectors"]}
    requested_as_of = as_of
    ceiling = as_of or latest_common_date([c["symbol"] for c in uni])
    if not ceiling:
        fail("本地没有行情缓存", "先运行 sync-prices")
    price_as_of = common_benchmark_date(ceiling)
    if not price_as_of:
        fail("基准没有不晚于请求日的共同交易日", "运行 sync-prices --force，再看 price_as_of")
    end = price_as_of

    bench_ret = {}
    bench_meta = []
    for market, b in BENCH.items():
        p = load_prices(b["symbol"])
        if not p:
            fail(f"缺少基准行情 {b['symbol']}", "运行 sync-prices --force")
        rets = {}
        for wk, days in WINDOWS.items():
            start = (parse_date(end) - timedelta(days=days)).isoformat()
            rets[wk] = window_return(p["rows"], start, end)[0]
        bench_ret[market] = rets
        bench_meta.append({"market": market, "name": b["name"], "symbol": b["symbol"], **rets})
    for b in EXTRA_BENCH:
        p = load_prices(b["symbol"])
        if not p:
            continue
        rets = {}
        for wk, days in WINDOWS.items():
            start = (parse_date(end) - timedelta(days=days)).isoformat()
            rets[wk] = window_return(p["rows"], start, end)[0]
        bench_meta.append({"market": b["market"], "name": b["name"], "symbol": b["symbol"],
                           "role": "参考", **rets})

    stocks, missing = [], []
    for c in uni:
        p = load_prices(c["symbol"])
        if not p:
            missing.append({"ticker": c["ticker"], "name": c["name"], "reason": "无行情缓存"})
            continue
        rec = {"ticker": c["ticker"], "name": c["name"], "market": c["market"],
               "subsector": c["subsector"], "mktcap_yi": p.get("mktcap_yi"),
               "last_date": p["last_date"], "stale": p["last_date"] < end}
        if c.get("dual_listed_with"):
            rec["dual_listed_with"] = c["dual_listed_with"]
        for wk, days in WINDOWS.items():
            start = (parse_date(end) - timedelta(days=days)).isoformat()
            r, _ = window_return(p["rows"], start, end)
            rec["ret_" + wk] = r
            b = bench_ret[c["market"]].get(wk)
            rec["excess_" + wk] = round(r - b, 2) if (r is not None and b is not None) else None
        stocks.append(rec)

    uni_median_excess = {wk: med([s["excess_" + wk] for s in stocks]) for wk in WINDOWS}
    uni_median_ret = {wk: med([s["ret_" + wk] for s in stocks]) for wk in WINDOWS}

    buckets = []
    for key, meta in subs.items():
        members = [s for s in stocks if s["subsector"] == key]
        if not members:
            continue
        row = {"key": key, "name_cn": meta["name_cn"], "n": len(members),
               "sample_ok": len(members) >= 4}
        for wk in WINDOWS:
            row["median_ret_" + wk] = med([m["ret_" + wk] for m in members])
            row["median_excess_" + wk] = med([m["excess_" + wk] for m in members])
            ume = uni_median_excess[wk]
            mex = row["median_excess_" + wk]
            row["alpha_vs_consumer_" + wk] = (round(mex - ume, 2)
                                              if (mex is not None and ume is not None) else None)
        markets = {m["market"] for m in members}
        if len(markets) == 1 and all(m.get("mktcap_yi") for m in members):
            tot = sum(m["mktcap_yi"] for m in members)
            row["cap_weighted_ret_1w"] = round(
                sum(m["ret_1w"] * m["mktcap_yi"] for m in members if m["ret_1w"] is not None) / tot, 2)
        else:
            row["cap_weighted_ret_1w"] = None
            row["cap_weight_note"] = "跨市场成分，未做汇率折算，只用中位数"
        ranked = sorted([m for m in members if m["excess_1w"] is not None],
                        key=lambda x: x["excess_1w"], reverse=True)
        row["top"] = [{"ticker": m["ticker"], "name": m["name"], "ret_1w": m["ret_1w"],
                       "excess_1w": m["excess_1w"]} for m in ranked[:3]]
        row["bottom"] = [{"ticker": m["ticker"], "name": m["name"], "ret_1w": m["ret_1w"],
                          "excess_1w": m["excess_1w"]} for m in ranked[-3:]][::-1]
        buckets.append(row)

    buckets.sort(key=lambda x: (x["median_excess_1w"] is None, -(x["median_excess_1w"] or 0)))
    for i, b in enumerate(buckets, 1):
        b["rank_1w"] = i

    for s in stocks:
        bucket = next((b for b in buckets if b["key"] == s["subsector"]), None)
        bm = bucket["median_excess_1w"] if bucket else None
        s["alpha_vs_subsector_1w"] = (round(s["excess_1w"] - bm, 2)
                                      if (s["excess_1w"] is not None and bm is not None) else None)

    holiday_note = None
    if requested_as_of and price_as_of < requested_as_of:
        holiday_note = (
            f"请求日期 {requested_as_of} 晚于两市共同交易日 {price_as_of}。"
            "缓存未到请求日或该日休市时，行情用这个共同收盘，不用请求日冒充行情日期。"
        )
    return {"requested_as_of": requested_as_of, "price_as_of": price_as_of, "as_of": price_as_of,
            "missing_count": len(missing), "holiday_note": holiday_note,
            "benchmarks": bench_meta,
            "consumer_pool": {"count": len(stocks), "median_ret": uni_median_ret,
                              "median_excess": uni_median_excess},
            "subsectors": buckets, "stocks": stocks, "missing": missing}


def weekly_excess_series(rows, bench_rows, as_of, weeks=12):
    out = []
    end_d = parse_date(as_of)
    for i in range(weeks):
        e = (end_d - timedelta(days=7 * i)).isoformat()
        s = (end_d - timedelta(days=7 * (i + 1))).isoformat()
        r, _ = window_return(rows, s, e)
        b, _ = window_return(bench_rows, s, e)
        out.append(round(r - b, 2) if (r is not None and b is not None) else None)
    return out[::-1]


def cmd_perf(args):
    data = build_perf(args.as_of)
    if args.subsector:
        data["stocks"] = [s for s in data["stocks"] if s["subsector"] == args.subsector]
        data["subsectors"] = [b for b in data["subsectors"] if b["key"] == args.subsector]
    data["stocks"].sort(key=lambda x: (x["excess_1w"] is None, -(x["excess_1w"] or 0)))
    if args.limit:
        data["stocks"] = data["stocks"][:args.limit] + data["stocks"][-args.limit:]
    data["ok"] = True
    emit(data, args.json)


def _rs_series(rows, bench_rows):
    bmap = {r["d"]: r["c"] for r in bench_rows}
    return [(r["d"], r["c"] / bmap[r["d"]]) for r in rows if r["d"] in bmap and bmap[r["d"]]]


def _series_return(pairs, start, end):
    base = last = None
    for d, v in pairs:
        if d < start:
            base = v
        elif d <= end:
            last = v
    return pct(last, base) if (base and last) else None


def build_index(as_of=None, base_days=420):
    """细分相对指数：成员先各自除以本市场基准，再按等权中位数合成，基期归 100。"""
    uni = universe()["companies"]
    subs = {s["key"]: s for s in subsectors()["subsectors"]}
    as_of = as_of or latest_common_date([c["symbol"] for c in uni])
    if not as_of:
        fail("本地没有行情缓存", "先运行 sync-prices")
    bench_rows = {}
    for market, b in BENCH.items():
        p = load_prices(b["symbol"])
        if not p:
            fail(f"缺少基准行情 {b['symbol']}", "运行 sync-prices --force")
        bench_rows[market] = [r for r in p["rows"] if r["d"] <= as_of]
    base_floor = (parse_date(as_of) - timedelta(days=base_days)).isoformat()

    out = []
    for key, meta in subs.items():
        members = [c for c in uni if c["subsector"] == key]
        member_rs = []
        for c in members:
            p = load_prices(c["symbol"])
            if not p:
                continue
            rows = [r for r in p["rows"] if r["d"] <= as_of and r["d"] >= base_floor]
            rs = _rs_series(rows, bench_rows[c["market"]])
            if len(rs) < 60:
                continue
            member_rs.append((c, rs))
        if not member_rs:
            continue
        row = {"key": key, "name_cn": meta["name_cn"], "n_members": len(member_rs)}
        for wk, days in WINDOWS.items():
            start = (parse_date(as_of) - timedelta(days=days)).isoformat()
            vals = [_series_return(rs, start, as_of) for _, rs in member_rs]
            row["idx_" + wk] = med([v for v in vals if v is not None])
        win_start = (parse_date(as_of) - timedelta(days=400)).isoformat()
        eligible = [rs for _, rs in member_rs if rs[0][0] <= win_start]
        level = []
        if len(eligible) >= 3:
            base = {}
            dates = sorted({d for rs in eligible for d, _ in rs if d >= win_start})
            for i, rs in enumerate(eligible):
                b = next((v for d, v in rs if d >= win_start), None)
                if b:
                    base[i] = (b, dict(rs))
            carry = {i: None for i in base}
            for d in dates:
                vals = []
                for i, (b, series) in base.items():
                    if d in series:
                        carry[i] = series[d] / b * 100
                    if carry[i] is not None:
                        vals.append(carry[i])
                m = med(vals)
                if m is not None:
                    level.append((d, round(m, 3)))
        vals = [v for _, v in level]
        row["index_members_in_level"] = len(eligible)
        row["index_level"] = vals[-1] if vals else None
        row["index_base_date"] = level[0][0] if level else None
        row["idx_60d_high"] = bool(len(vals) >= 60 and vals[-1] >= max(vals[-60:]))
        row["idx_60d_low"] = bool(len(vals) >= 60 and vals[-1] <= min(vals[-60:]))
        wk_vals = [row.get("idx_1w"), row.get("idx_4w"), row.get("idx_13w")]
        row["persist_positive"] = all(v is not None and v > 0 for v in wk_vals)
        row["persist_negative"] = all(v is not None and v < 0 for v in wk_vals)
        start_1w = (parse_date(as_of) - timedelta(days=7)).isoformat()
        per_member = []
        for c, rs in member_rs:
            r = _series_return(rs, start_1w, as_of)
            if r is not None:
                per_member.append({"ticker": c["ticker"], "name": c["name"], "rel_1w": round(r, 2)})
        per_member.sort(key=lambda x: -x["rel_1w"])
        pos = [m for m in per_member if m["rel_1w"] > 0]
        row["breadth_1w"] = (f"{len(pos)}/{len(per_member)}" if per_member else None)
        row["breadth_ratio_1w"] = (round(len(pos) / len(per_member), 2) if per_member else None)
        row["leader_1w"] = per_member[0] if per_member else None
        row["laggard_1w"] = per_member[-1] if per_member else None
        if len(per_member) >= 3:
            trimmed = med([m["rel_1w"] for m in per_member[1:]])
            row["median_ex_leader_1w"] = trimmed
            row["single_driver"] = bool(row.get("idx_1w") and row["idx_1w"] > 0
                                        and trimmed is not None and trimmed <= 0)
        row["sample_ok"] = len(member_rs) >= 4
        out.append(row)

    out.sort(key=lambda x: (x.get("idx_1w") is None, -(x.get("idx_1w") or 0)))
    for i, r in enumerate(out, 1):
        r["rank_1w"] = i

    pool_rs = []
    for c in uni:
        p = load_prices(c["symbol"])
        if not p:
            continue
        rows = [r for r in p["rows"] if base_floor <= r["d"] <= as_of]
        rs = _rs_series(rows, bench_rows[c["market"]])
        if len(rs) >= 60:
            pool_rs.append(rs)
    pool = {"n": len(pool_rs)}
    for wk, days in WINDOWS.items():
        start = (parse_date(as_of) - timedelta(days=days)).isoformat()
        vals = [_series_return(rs, start, as_of) for rs in pool_rs]
        pool["idx_" + wk] = med([v for v in vals if v is not None])
    for r in out:
        for wk in WINDOWS:
            a, b = r.get("idx_" + wk), pool.get("idx_" + wk)
            r["vs_pool_" + wk] = round(a - b, 2) if (a is not None and b is not None) else None

    return {"as_of": as_of, "consumer_pool_index": pool,
            "method": "成员先除以本市场基准（沪深300 / 恒生指数）再取等权中位数，"
                      "基期归 100；停牌按前值填充；跨市场不做汇率折算",
            "subsector_index": out}


def cmd_index(args):
    data = build_index(args.as_of)
    rows = [r for r in data["subsector_index"] if r["sample_ok"] or not args.sample_only]
    lead = rows[:args.top]
    lag = rows[-args.top:][::-1]
    data["headline"] = {
        "leaders": [{k: r[k] for k in ("name_cn", "idx_1w", "idx_4w", "idx_13w", "breadth_1w",
                                       "idx_60d_high", "single_driver", "n_members")
                     if k in r} for r in lead],
        "laggards": [{k: r[k] for k in ("name_cn", "idx_1w", "idx_4w", "idx_13w", "breadth_1w",
                                        "idx_60d_low", "n_members") if k in r} for r in lag],
        "persist_positive": [r["name_cn"] for r in rows if r.get("persist_positive")],
        "persist_negative": [r["name_cn"] for r in rows if r.get("persist_negative")],
        "index_60d_high": [r["name_cn"] for r in rows if r.get("idx_60d_high")],
        "index_60d_low": [r["name_cn"] for r in rows if r.get("idx_60d_low")],
    }
    data["ok"] = True
    emit(data, args.json)


def ma(closes, n):
    return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None


def cmd_tech(args):
    uni = universe()["companies"]
    subs = {s["key"]: s["name_cn"] for s in subsectors()["subsectors"]}
    as_of = args.as_of or latest_common_date([c["symbol"] for c in uni])
    bench_rows = {m: (load_prices(b["symbol"]) or {}).get("rows", []) for m, b in BENCH.items()}

    hits = []
    skipped = []
    for c in uni:
        p = load_prices(c["symbol"])
        if not p:
            skipped.append({"ticker": c["ticker"], "reason": "无行情缓存"})
            continue
        rows = [r for r in p["rows"] if r["d"] <= as_of]
        if len(rows) < 130:
            skipped.append({"ticker": c["ticker"], "reason": "历史不足130个交易日",
                            "bars": len(rows)})
            continue
        closes = [r["c"] for r in rows]
        vols = [r["v"] for r in rows]
        last, prev = closes[-1], closes[-2]
        sig = []

        for n in (60, 120, 250):
            if len(closes) >= n and last >= max(closes[-n:]):
                sig.append(f"收盘创{n}日新高")
        for n in (60, 250):
            if len(closes) >= n and last <= min(closes[-n:]):
                sig.append(f"收盘创{n}日新低")

        ma60, ma120, ma200 = ma(closes, 60), ma(closes, 120), ma(closes, 200)
        prev_ma200 = ma(closes[:-1], 200)
        if ma200 and prev_ma200 and last > ma200 and prev <= prev_ma200:
            sig.append("上穿200日均线")
        if ma200 and prev_ma200 and last < ma200 and prev >= prev_ma200:
            sig.append("跌破200日均线")
        ma50, prev_ma50 = ma(closes, 50), ma(closes[:-1], 50)
        if ma50 and ma200 and prev_ma50 and prev_ma200 and ma50 > ma200 and prev_ma50 <= prev_ma200:
            sig.append("50日线金叉200日线")

        vol20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
        vol_x = round(vols[-1] / vol20, 2) if vol20 else None
        if vol_x and vol_x >= 2 and len(closes) >= 60 and last >= max(closes[-60:]):
            sig.append(f"放量突破（量比{vol_x}×）")

        br = bench_rows.get(c["market"], [])
        bmap = {r["d"]: r["c"] for r in br}
        rs = [closes[i] / bmap[rows[i]["d"]] for i in range(len(rows)) if rows[i]["d"] in bmap]
        if len(rs) >= 120:
            if rs[-1] >= max(rs[-60:]):
                sig.append("相对强弱创60日新高")
            elif rs[-1] <= min(rs[-60:]):
                sig.append("相对强弱创60日新低")

        if not sig:
            continue
        hi52 = max(closes[-250:]) if len(closes) >= 250 else max(closes)
        lo52 = min(closes[-250:]) if len(closes) >= 250 else min(closes)
        wk_excess = weekly_excess_series(rows, br, as_of, weeks=8) if br else []
        streak = 0
        for x in reversed(wk_excess):
            if x is not None and x > 0:
                streak += 1
            else:
                break
        hits.append({
            "ticker": c["ticker"], "name": c["name"], "market": c["market"],
            "subsector": c["subsector"], "subsector_cn": subs.get(c["subsector"], c["subsector"]),
            "close": last, "last_date": rows[-1]["d"], "signals": sig,
            "vol_x_20d": vol_x,
            "pct_from_52w_high": pct(last, hi52), "pct_above_52w_low": pct(last, lo52),
            "ma60": ma60, "ma120": ma120, "ma200": ma200,
            "weekly_excess_8w": wk_excess,
            "consecutive_outperform_weeks": streak,
            "direction": "upside" if any(k in " ".join(sig) for k in ("新高", "上穿", "金叉", "放量突破")) else "downside",
        })

    up = [h for h in hits if h["direction"] == "upside"]
    down = [h for h in hits if h["direction"] == "downside"]
    up.sort(key=lambda x: (-len(x["signals"]), -(x["consecutive_outperform_weeks"] or 0)))
    down.sort(key=lambda x: len(x["signals"]), reverse=True)
    emit({"ok": True, "as_of": as_of, "upside_count": len(up), "downside_count": len(down),
          "upside": up, "downside": down, "skipped": skipped}, args.json)


def fetch_commodity(sess, code):
    url = (
        f"https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_{code}=/"
        f"InnerFuturesNewService.getDailyKLine?symbol={code}"
    )
    r = sess.get(url, timeout=25, headers={"Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    m = re.search(r"\((.*)\)", r.text, re.DOTALL)
    raw = json.loads(m.group(1)) if m else json.loads(r.text[r.text.index("["):r.text.rindex("]") + 1])
    out = []
    for row in raw:
        try:
            out.append({"d": row["d"], "c": float(row["c"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def commodity_rel(code: str) -> str:
    return f"{COMMODITY_REL}/{code}.json"


def load_commodity(code):
    return read_json_rel(commodity_rel(code))


def _back_pct(rows, n):
    if len(rows) <= n:
        return None
    return pct(rows[-1]["c"], rows[-1 - n]["c"])


def cmd_commodities(args):
    sess = _session()
    today = date.today().isoformat()
    items, failed = [], []
    for spec in COMMODITIES:
        code = spec["code"]
        cached = load_commodity(code)
        stale = (not cached) or cached.get("fetched_at", "") < today
        rows = cached.get("rows") if cached else None
        if args.refresh or stale:
            try:
                rows = fetch_commodity(sess, code)
                write_json_rel(commodity_rel(code), {"code": code, "fetched_at": today, "rows": rows[-400:]})
                rows = rows[-400:]
            except Exception as exc:  # noqa: BLE001  # 单个合约拉取失败时保留已有缓存，没有缓存才记入 failed
                if not rows:
                    failed.append({"code": code, "name": spec["name"], "error": type(exc).__name__})
                    continue
        if not rows:
            failed.append({"code": code, "name": spec["name"], "error": "no_rows"})
            continue
        as_of = rows[-1]["d"]
        if args.as_of:
            rows = [r for r in rows if r["d"] <= args.as_of]
            if not rows:
                failed.append({"code": code, "name": spec["name"], "error": "as_of 之前无数据"})
                continue
            as_of = rows[-1]["d"]
        w1 = _back_pct(rows, 5)
        item = {"code": code, "name": spec["name"], "unit": spec["unit"],
                "as_of": as_of, "close": rows[-1]["c"],
                "chg_1w_pct": w1, "chg_4w_pct": _back_pct(rows, 20),
                "chg_13w_pct": _back_pct(rows, 65), "chg_52w_pct": _back_pct(rows, 250),
                "downstream": spec["downstream"], "use": spec["use"]}
        if spec.get("proxy_for"):
            item["proxy_for"] = spec["proxy_for"]
            item["caveat"] = "代理指标，不是 {} 现货价".format(spec["proxy_for"])
        m4 = item["chg_4w_pct"]
        item["cost_pressure_4w"] = ("上行" if m4 is not None and m4 >= 2
                                    else "下行" if m4 is not None and m4 <= -2
                                    else "持平")
        items.append(item)

    moved = [i for i in items if i["chg_1w_pct"] is not None and abs(i["chg_1w_pct"]) >= 2]
    moved.sort(key=lambda x: -abs(x["chg_1w_pct"]))
    dates = {i["as_of"] for i in items}
    payload = {"ok": len(items) > 0, "count": len(items),
               "as_of_range": {"min": min(dates), "max": max(dates)} if dates else None,
               "source": "新浪财经期货连续合约日线（交易所结算口径，A 级）",
               "caliber": "涨跌按交易日回溯：1w=5 日、4w=20 日、13w=65 日、52w=250 日；连续合约会含移仓换月跳空",
               "usage_rule": "只作成本方向的输入，不得直接推导毛利率或利润弹性；传导有滞后且各家锁价方式不同",
               "items": items, "moved_over_2pct_1w": moved,
               "unavailable": COMMODITY_GAPS, "failed": failed}
    if failed:
        payload["hint"] = "失败品种可加 --refresh 重试；仍失败就在报告里写明该品种本期缺失，不要用旧值"
    if not items:
        payload["error"] = "全部品种取数失败"
        payload["hint"] = "新浪期货接口不可达；检查网络后重跑 commodities --refresh"
    emit(payload, args.json)
    raise CliExit(0 if items else 1)


def cmd_add_note(args):
    if args.evidence_grade not in EVIDENCE_GRADES:
        fail("证据等级不合法", "只接受 A/B/C；D 级（无口径转述、无链接爆料）一律不入库")
    if args.source_type not in SOURCE_TYPES:
        fail("source-type 不合法", "可选: " + ", ".join(SOURCE_TYPES))
    if args.direction not in DIRECTIONS:
        fail("direction 不合法", "可选: " + ", ".join(DIRECTIONS))
    if args.driver not in DRIVERS:
        fail("driver 不合法", "可选: " + ", ".join(DRIVERS))
    subs = {s["key"] for s in subsectors()["subsectors"]}
    if args.subsector not in subs:
        fail(f"未知细分行业 {args.subsector}", "运行 subsectors 查看合法 key")
    if args.verbatim:
        words = len(re.findall(r"[A-Za-z0-9']+", args.verbatim)) + len(re.findall(r"[一-鿿]", args.verbatim))
        if words > 20:
            fail(f"verbatim 超过 20 词（当前 {words}）",
                 "专家访谈逐字引用上限 20 词，主张请用 --claim 自己的话写")
        if not args.source_attribution:
            fail("verbatim 缺 source-attribution",
                 "带逐字引用必须附提供方给的 source_attribution 原文")
    if args.source_type == "expert_call" and not args.source_attribution:
        fail("专家访谈缺 source-attribution", "Guidepoint 内容必须附 source_attribution.markdown 原文")
    if args.evidence_grade in ("B", "C") and not args.cross_check:
        fail("B/C 级证据缺 cross-check",
             "品牌方或单边口径必须写下游、渠道、上游或平台侧的印证；"
             "本周确实找不到印证就显式传 --cross-check none，该条只能进「观察项」")
    cross_checked = bool(args.cross_check) and args.cross_check.strip().lower() != "none"

    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "week_of": args.week_of or date.today().isoformat(),
           "subsector": args.subsector, "ticker": args.ticker, "company": args.company,
           "source_type": args.source_type, "evidence_grade": args.evidence_grade,
           "direction": args.direction, "driver": args.driver,
           "claim": args.claim, "verbatim": args.verbatim,
           "source_id": args.source_id, "source_url": args.source_url,
           "source_date": args.source_date, "source_attribution": args.source_attribution,
           "numbers": args.numbers,
           "cross_check": args.cross_check, "cross_check_url": args.cross_check_url,
           "cross_checked": cross_checked}
    existing = read_text_rel(NOTES_REL) or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_text_rel(NOTES_REL, existing + json.dumps(rec, ensure_ascii=False) + "\n")
    emit({"ok": True, "written": NOTES_REL, "record": rec}, args.json)


def read_notes():
    text = read_text_rel(NOTES_REL)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def note_in_source_window(rec, start):
    """Keep a note when its source date or its collect week falls in the window.

    A source_date on Monday–Thursday is still this week's collect fact when
    week_of is the report Friday, or when --since is the source-window Sunday.
    """
    for stamp in (rec.get("source_date"), rec.get("week_of")):
        if stamp and stamp >= start:
            return True
    return False


def cmd_notes(args):
    rows = read_notes()
    if args.subsector:
        rows = [r for r in rows if r["subsector"] == args.subsector]
    if args.ticker:
        rows = [r for r in rows if r.get("ticker") == args.ticker]
    if args.since:
        rows = [r for r in rows if note_in_source_window(r, args.since)]
    if args.source_type:
        rows = [r for r in rows if r["source_type"] == args.source_type]
    by_sub = {}
    for r in rows:
        by_sub.setdefault(r["subsector"], []).append(r)
    crossed = [r for r in rows if r.get("cross_checked")]
    single = [r for r in rows if r.get("evidence_grade") in ("B", "C") and not r.get("cross_checked")]
    # --limit 0 returns every matching note. The default 50 keeps only the tail.
    shown = rows if args.limit == 0 else rows[-args.limit:]
    emit({"ok": True, "count": len(rows),
          "cross_checked_count": len(crossed),
          "single_sided_count": len(single),
          "by_subsector": {k: len(v) for k, v in sorted(by_sub.items())},
          "notes": shown}, args.json)


def cmd_snapshot(args):
    if args.write:
        require_delivery(args, "snapshot --write")
    perf = build_perf(args.as_of)
    label = anchor_friday(args.as_of or perf["price_as_of"]).isoformat()
    snap = {
        "as_of": label, "requested_as_of": perf["requested_as_of"],
        "price_as_of": perf["price_as_of"],
        "created_at": datetime.now(HK).isoformat(timespec="seconds"),
        "subsector_rank": {b["key"]: {"rank": b["rank_1w"], "median_excess_1w": b["median_excess_1w"],
                                      "median_excess_4w": b["median_excess_4w"]}
                           for b in perf["subsectors"]},
        "stock_excess_1w": {s["ticker"]: s["excess_1w"] for s in perf["stocks"]},
        "stock_excess_4w": {s["ticker"]: s["excess_4w"] for s in perf["stocks"]},
        "notes_count": len(read_notes()),
    }
    if args.write:
        rel = f"{SNAP_REL}/{label}.json"
        existing = read_text_rel(rel)
        if existing is not None and not args.overwrite:
            fail("快照已存在", "加 --overwrite 覆盖，或换 --as-of", {"path": rel})
        write_json_rel(rel, snap, indent=1)
        snap["written"] = rel
    else:
        snap["written"] = None
        snap["note"] = "未加 --write，本次不落盘"
    snap["ok"] = True
    emit(snap, args.json)


def cmd_diff(args):
    perf = build_perf(args.as_of)
    label = anchor_friday(args.as_of or perf["price_as_of"]).isoformat()
    files = [f for f in dir_names(SNAP_REL, ".json") if f[:-5] < label]
    if args.against:
        files = [f for f in files if f[:-5] == args.against]
    if not files:
        emit({"ok": True, "baseline": None, "note": "无可比历史快照，本期为基期"}, args.json)
        return
    base = read_json_rel(f"{SNAP_REL}/{files[-1]}")

    rank_moves = []
    for b in perf["subsectors"]:
        old = base["subsector_rank"].get(b["key"])
        if not old:
            rank_moves.append({"key": b["key"], "name_cn": b["name_cn"], "change": "新增细分"})
            continue
        d = old["rank"] - b["rank_1w"]
        if abs(d) >= args.min_rank_move:
            rank_moves.append({"key": b["key"], "name_cn": b["name_cn"],
                               "rank_from": old["rank"], "rank_to": b["rank_1w"], "rank_delta": d,
                               "median_excess_1w": b["median_excess_1w"],
                               "prev_median_excess_1w": old["median_excess_1w"]})
    rank_moves.sort(key=lambda x: -(x.get("rank_delta") or 0))

    flips = []
    for s in perf["stocks"]:
        prev = base["stock_excess_1w"].get(s["ticker"])
        if prev is None or s["excess_1w"] is None:
            continue
        if prev < 0 <= s["excess_1w"] or s["excess_1w"] < 0 <= prev:
            flips.append({"ticker": s["ticker"], "name": s["name"], "subsector": s["subsector"],
                          "excess_1w_prev": prev, "excess_1w": s["excess_1w"]})
    flips.sort(key=lambda x: -abs(x["excess_1w"] - x["excess_1w_prev"]))

    emit({"ok": True, "baseline": base["as_of"], "as_of": label,
          "price_as_of": perf["price_as_of"],
          "subsector_rank_moves": rank_moves,
          "relative_direction_flips": flips[:args.limit or 20],
          "notes_added": len(read_notes()) - base.get("notes_count", 0)}, args.json)


def cmd_universe(args):
    u = universe()
    rows = u["companies"]
    if args.subsector:
        rows = [c for c in rows if c["subsector"] == args.subsector]
    if args.market:
        rows = [c for c in rows if c["market"] == args.market]
    emit({"ok": True, "meta": u["meta"], "count": len(rows), "companies": rows}, args.json)


def cmd_subsectors(args):
    s = subsectors()
    emit({"ok": True, "count": len(s["subsectors"]), "subsectors": s["subsectors"]}, args.json)


def cmd_plan(args):
    u = universe()
    subs = subsectors()["subsectors"]
    as_of = args.as_of or latest_common_date([c["symbol"] for c in u["companies"]]) or hk_today().isoformat()
    friday = anchor_friday(as_of)
    price_as_of = None
    if all(load_prices(spec["symbol"]) for spec in BENCH.values()):
        price_as_of = common_benchmark_date(as_of)
    windows = collection_windows(as_of, price_as_of)
    emit({"ok": True, "as_of": as_of, "friday": friday.isoformat(),
          "requested_as_of": args.as_of, "price_as_of": price_as_of,
          "report_run_id": f"china-consumer-weekly-{friday.isoformat()}",
          **windows,
          "universe_count": len(u["companies"]),
          "subsector_count": len(subs),
          "lanes": [
              {"lane": "行情与技术面", "cmd": ["sync-prices", "perf", "index", "tech", "diff"]},
              {"lane": "大宗成本", "cmd": ["commodities"]},
              {"lane": "AlphaEngine 卖方点评与路演纪要",
               "tool": "research",
               "call": "research(route=\"alphaengine\", operation=\"search_library\", query=..., start=..., end=..., limit=20, params={\"sort\": \"relevance\", \"industry_names\": [...]})，再 operation=\"get_document\""},
              {"lane": "Guidepoint 专家访谈",
               "tool": "research",
               "call": "research(route=\"guidepoint\", operation=\"search_library\", query=..., start=..., end=..., limit=30, params={\"keywords\": [...], \"recency_bias\": 50})"},
              {"lane": "公司公告与业绩",
               "tool": "finance",
               "call": "finance(route=\"cn-hk\", operation=\"filings\" 或 \"ask\", ticker=..., start=..., end=...)"},
              {"lane": "全网实时检索",
               "tool": "web_search",
               "call": "web_search(query=..., start_date=..., end_date=..., time_range=\"week\")"},
              {"lane": "社区与渠道",
               "tool": "community",
               "call": "community(route=\"xueqiu\" 或 \"last30days\", operation=\"search\", query=..., days=7)"},
          ],
          "queries": [{"key": s["key"], "name_cn": s["name_cn"],
                       "alphaengine": s.get("alphaengine_queries", []),
                       "guidepoint": s.get("guidepoint_queries", []),
                       "web": s.get("web_queries", [])} for s in subs]}, args.json)


def cmd_validate(args):
    try:
        path = rel_file(args.file)
    except ValueError as exc:
        fail(str(exc), "报告路径必须是工作区内的相对路径")
    if not path.is_file():
        fail("报告文件不存在", f"确认路径：{args.file}")
    text = path.read_text(encoding="utf-8")
    problems = []
    for p in BANNED_PHRASES:
        if p in text:
            problems.append({"type": "vague_phrase", "detail": p,
                             "fix": "换成带口径的数字，或直接删掉"})
    if getattr(args, "profile", "report") == "card":
        required = [["本周结论"], ["细分行情", "细分指数"], ["技术面"], ["变化归因"],
                    ["观察项"], ["待验证"]]
        tpl = "templates/weekly-card.md"
    else:
        required = [["本周结论"], ["细分行情", "细分指数"], ["技术面"], ["上游成本"],
                    ["变化归因"], ["观察项"], ["待验证"], ["信源"], ["口径"]]
        tpl = "templates/weekly-report.md"
    for alts in required:
        if not any(a in text for a in alts):
            problems.append({"type": "missing_section", "detail": " / ".join(alts),
                             "fix": f"按 {tpl} 补齐该章节"})
    if "$" in text:
        problems.append({"type": "dollar_sign", "detail": "正文含 $",
                         "fix": "成对 $ 会被当成公式，金额写「美元」「港元」「元」"})

    for line in text.splitlines():
        if not re.search(r"\d", line):
            continue
        masked = mask_dates(line)
        for rule in CALIBER_RULES:
            hit = None
            for m in re.finditer(rule["trigger"], line):
                near = masked[max(0, m.start() - CALIBER_NUM_WINDOW):m.end() + CALIBER_NUM_WINDOW]
                if not re.search(r"\d", near):
                    continue
                ctx = line[max(0, m.start() - CALIBER_CTX_WINDOW):m.end() + CALIBER_CTX_WINDOW]
                if not re.search(rule["require"], ctx):
                    hit = m.group(0)
                    break
            if hit:
                problems.append({"type": "caliber_label",
                                 "detail": f'{rule["key"]}（触发词 {hit}）: {line.strip()[:60]}',
                                 "fix": rule["fix"]})

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"\d+(\.\d+)?%", line):
            continue
        if re.search(PERIOD_MARKERS, line):
            continue
        exempt = False
        for j in range(i - 1, -1, -1):
            prev = lines[j]
            if not prev.strip():
                continue
            if re.search(PERIOD_MARKERS, prev):
                exempt = True
                break
            if not re.match(r"\s*([-*•]|\d+[.、)])\s", prev):
                break
        if exempt:
            continue
        problems.append({"type": "number_without_period", "detail": line.strip()[:60],
                         "fix": "百分比要带时间窗或口径，或在本段小标题里写清窗口"})
    ok = not [p for p in problems if p["type"] != "number_without_period"]
    emit({"ok": ok, "file": args.file, "problem_count": len(problems), "problems": problems,
          "hint": "number_without_period 是提醒项；vague_phrase / missing_section / "
                  "dollar_sign / caliber_label 必须清零才能发送"}, args.json)


def cmd_self_test(args):
    checks = []

    def add(name, ok, detail="", hint=""):
        checks.append({"check": name, "ok": ok, "detail": detail, "hint": hint})

    add("python_version", sys.version_info >= (3, 10), sys.version.split()[0],
        "需要 Python 3.10+")
    try:
        u = universe()
        add("universe_loaded", len(u["companies"]) > 0, f"{len(u['companies'])} 家")
    except CliExit:
        raise
    except Exception as exc:  # noqa: BLE001  # 自检要接住名单文件的任意读取错误并写成检查项
        add("universe_loaded", False, str(exc), "检查 references/universe.json")
    try:
        s = subsectors()
        keys = {x["key"] for x in s["subsectors"]}
        ukeys = {c["subsector"] for c in universe()["companies"]}
        add("subsectors_loaded", True, f"{len(keys)} 个细分")
        add("taxonomy_consistent", ukeys <= keys, f"universe 独有: {sorted(ukeys - keys)}",
            "universe.json 里出现了 subsectors.json 未定义的 key")
    except Exception as exc:  # noqa: BLE001  # 自检要接住细分文件的任意读取错误并写成检查项
        add("subsectors_loaded", False, str(exc), "检查 references/subsectors.json")
    try:
        probe = rel_file(f"{DB_REL}/.write-probe")
        probe.parent.mkdir(parents=True, exist_ok=True)
        add("db_writable", os.access(probe.parent, os.W_OK), DB_REL, "确认该目录可写")
    except (OSError, ValueError) as exc:
        add("db_writable", False, str(exc), "确认 state/ 在工作区内且可写")
    if args.network:
        try:
            sess = _session()
            rows = fetch_kline(sess, "sh000300", 10)
            add("price_source_reachable", len(rows) > 0, f"{len(rows)} bars",
                "腾讯行情不可达；检查网络或本机代理")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("price_source_reachable", False, type(exc).__name__,
                "腾讯行情不可达；检查网络或本机代理")
    try:
        cached = len(dir_names(PRICE_REL, ".json"))
        if cached:
            add("price_cache", True, f"{cached} 个缓存文件")
        else:
            add("price_cache", True, "缓存为空，运行 sync-prices 后才有行情",
                "首次使用先跑 sync-prices")
    except ValueError as exc:
        add("price_cache", False, str(exc), "行情目录不能是符号链接")
    if args.network:
        try:
            rows = fetch_commodity(_session(), "TA0")
            add("commodity_source_reachable", len(rows) > 0, f"{len(rows)} bars",
                "新浪期货接口不可达；大宗成本这一节本期写缺失，不要用旧值")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("commodity_source_reachable", False, type(exc).__name__,
                "新浪期货接口不可达；大宗成本这一节本期写缺失，不要用旧值")

    ok = all(c["ok"] for c in checks)
    payload = {"ok": ok, "version": VERSION, "checks": checks}
    if not ok:
        payload["error"] = "self-test 未全部通过"
        payload["hint"] = "; ".join(c["hint"] for c in checks if not c["ok"] and c["hint"])
    emit(payload, args.json)
    raise CliExit(0 if ok else 1)


def cmd_anchor(args):
    friday = anchor_friday(args.as_of)
    emit({"ok": True, "hk_today": hk_today().isoformat(),
          "friday": friday.isoformat(),
          "report_run_id": f"china-consumer-weekly-{friday.isoformat()}"}, True)


def cmd_record_collection(args):
    day = args.as_of or anchor_friday(None).isoformat()
    payload = {"as_of": day, "recorded_at": datetime.now().isoformat(timespec="seconds"),
               "mode": "collect", "note": args.note or ""}
    rel = f"{RUNS_REL}/{day}.json"
    write_json_rel(rel, payload, indent=1)
    payload["ok"] = True
    payload["written"] = rel
    emit(payload, args.json)


def cmd_archive(args):
    require_delivery(args, "archive")
    try:
        src = rel_file(args.file)
    except ValueError as exc:
        fail(str(exc), "归档来源必须是工作区内的相对路径")
    if not src.is_file():
        fail("报告文件不存在", f"确认路径：{args.file}")
    day = args.as_of or anchor_friday(None).isoformat()
    rel = f"{ARCHIVE_REL}/{day}.md"
    write_text_rel(rel, src.read_text(encoding="utf-8"))
    emit({"ok": True, "written": rel, "source": args.file}, args.json)


def build_parser():
    p = argparse.ArgumentParser(prog="china-consumer-weekly",
                                description="中国消费周度监控：行情与事实库")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--workspace-dir", default=None)
    p.add_argument("--version", action="version", version=VERSION)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    delivery = argparse.ArgumentParser(add_help=False)
    delivery.add_argument("--content-kind", default=None)
    delivery.add_argument("--delivery-confirmed", action="store_true")
    delivery.add_argument("--status", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("self-test", parents=[shared])
    s.add_argument("--network", action="store_true")
    s.set_defaults(func=cmd_self_test)

    s = sub.add_parser("plan", parents=[shared])
    s.add_argument("--as-of")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("anchor", parents=[shared])
    s.add_argument("--as-of")
    s.set_defaults(func=cmd_anchor)

    s = sub.add_parser("universe", parents=[shared])
    s.add_argument("--subsector")
    s.add_argument("--market", choices=["A", "HK"])
    s.set_defaults(func=cmd_universe)

    s = sub.add_parser("subsectors", parents=[shared])
    s.set_defaults(func=cmd_subsectors)

    s = sub.add_parser("sync-prices", parents=[shared])
    s.add_argument("--bars", type=int, default=420)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_sync_prices)

    s = sub.add_parser("perf", parents=[shared])
    s.add_argument("--as-of")
    s.add_argument("--subsector")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_perf)

    s = sub.add_parser("index", parents=[shared])
    s.add_argument("--as-of")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--sample-only", action="store_true")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("tech", parents=[shared])
    s.add_argument("--as-of")
    s.set_defaults(func=cmd_tech)

    s = sub.add_parser("commodities", parents=[shared])
    s.add_argument("--as-of")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_commodities)

    s = sub.add_parser("add-note", parents=[shared])
    s.add_argument("--subsector", required=True)
    s.add_argument("--ticker")
    s.add_argument("--company")
    s.add_argument("--source-type", required=True)
    s.add_argument("--evidence-grade", required=True)
    s.add_argument("--direction", default="neutral")
    s.add_argument("--driver", default="other")
    s.add_argument("--claim", required=True)
    s.add_argument("--verbatim")
    s.add_argument("--source-id")
    s.add_argument("--source-url")
    s.add_argument("--source-date")
    s.add_argument("--source-attribution")
    s.add_argument("--numbers")
    s.add_argument("--week-of")
    s.add_argument("--cross-check")
    s.add_argument("--cross-check-url")
    s.set_defaults(func=cmd_add_note)

    s = sub.add_parser("notes", parents=[shared])
    s.add_argument("--subsector")
    s.add_argument("--ticker")
    s.add_argument("--since")
    s.add_argument("--source-type")
    s.add_argument("--limit", type=int, default=50, help="返回条数，0 表示全部")
    s.set_defaults(func=cmd_notes)

    s = sub.add_parser("snapshot", parents=[shared, delivery])
    s.add_argument("--as-of")
    s.add_argument("--write", action="store_true")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("diff", parents=[shared])
    s.add_argument("--as-of")
    s.add_argument("--against")
    s.add_argument("--min-rank-move", type=int, default=3)
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("validate", parents=[shared])
    s.add_argument("--file", required=True)
    s.add_argument("--profile", choices=["report", "card"], default="report")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("record-collection", parents=[shared])
    s.add_argument("--as-of")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_record_collection)

    s = sub.add_parser("archive", parents=[shared, delivery])
    s.add_argument("--file", required=True)
    s.add_argument("--as-of")
    s.set_defaults(func=cmd_archive)
    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else (code if isinstance(code, int) else 1)
    try:
        bind_workspace(getattr(args, "workspace_dir", None))
        args.func(args)
    except CliExit as exc:
        code = exc.code
        return 1 if code is None else (code if isinstance(code, int) else 1)
    except Exception as exc:  # noqa: BLE001  # 命令行边界把未分类错误收成 JSON，避免栈信息直接吐出
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "hint": "检查输入参数；路径必须留在工作区内"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
