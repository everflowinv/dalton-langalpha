#!/usr/bin/env python3
"""美国与日本消费周报行情与事实库。

窗口对齐到当周周五。相对强弱用本币对本地基准，美元收益单独列出。
中位数先按市场取。写入只走工作区相对路径，并拒绝符号链接。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL_DIR = Path(__file__).resolve().parent.parent
REF_DIR = SKILL_DIR / "references"
DB_REL = "state/global-consumer"
PRICE_REL = DB_REL + "/prices"
EARN_REL = DB_REL + "/earnings"
NOTES_REL = DB_REL + "/notes.jsonl"
SNAP_REL = DB_REL + "/snapshots"
CHECK_REL = DB_REL + "/checkpoints.jsonl"
ARCHIVE_REL = "research-outputs/global-consumer-weekly"
HK = ZoneInfo("Asia/Hong_Kong")
WORKSPACE: Path | None = None

BENCH = {"US": "^GSPC", "JP": "^N225"}
FX_SYMS = {"USDJPY": "JPY=X", "EURUSD": "EURUSD=X", "USDCNY": "CNY=X", "DXY": "DX-Y.NYB"}
TIERS = ("A", "B", "C", "D")
WINDOWS = {"w1": 7, "w4": 28, "w13": 91, "w52": 364}
MIN_DIVERGENCE_SIDE = 4

COMMODITIES = [
    {"code": "SB=F", "name_cn": "原糖", "unit": "美分/磅",
     "downstream": ["soft_drinks", "snacks_confection", "packaged_food", "coffee_tea"]},
    {"code": "KC=F", "name_cn": "咖啡", "unit": "美分/磅",
     "downstream": ["coffee_tea", "restaurants_qsr"]},
    {"code": "CC=F", "name_cn": "可可", "unit": "美元/吨",
     "downstream": ["snacks_confection", "packaged_food"]},
    {"code": "CT=F", "name_cn": "棉花", "unit": "美分/磅",
     "downstream": ["apparel_footwear", "sportswear"]},
    {"code": "ZC=F", "name_cn": "玉米", "unit": "美分/蒲式耳",
     "downstream": ["packaged_food", "pet", "dairy"]},
    {"code": "ZW=F", "name_cn": "小麦", "unit": "美分/蒲式耳",
     "downstream": ["packaged_food", "restaurants_qsr", "snacks_confection"]},
    {"code": "ZS=F", "name_cn": "大豆", "unit": "美分/蒲式耳", "downstream": ["packaged_food"]},
    {"code": "ZM=F", "name_cn": "豆粕", "unit": "美元/短吨", "downstream": ["packaged_food", "pet"]},
    {"code": "ZL=F", "name_cn": "豆油", "unit": "美分/磅",
     "downstream": ["packaged_food", "restaurants_qsr"]},
    {"code": "HE=F", "name_cn": "瘦肉猪", "unit": "美分/磅",
     "downstream": ["packaged_food", "restaurants_qsr"]},
    {"code": "LE=F", "name_cn": "活牛", "unit": "美分/磅",
     "downstream": ["restaurants_qsr", "restaurants_casual", "packaged_food"]},
    {"code": "DC=F", "name_cn": "III类牛奶", "unit": "美元/英担",
     "downstream": ["dairy", "coffee_tea", "restaurants_qsr"]},
    {"code": "OJ=F", "name_cn": "橙汁", "unit": "美分/磅", "downstream": ["soft_drinks"]},
    {"code": "GC=F", "name_cn": "黄金", "unit": "美元/盎司", "downstream": ["apparel_footwear"]},
    {"code": "ALI=F", "name_cn": "铝", "unit": "美元/吨",
     "downstream": ["soft_drinks", "beer_spirits"]},
    {"code": "CL=F", "name_cn": "原油", "unit": "美元/桶",
     "downstream": ["consumer_freight", "drugstore_conv", "travel_lodging"]},
]
COMMODITY_GAPS = [
    "PET与树脂：无公开连续合约，用原油方向做粗代理，引用必须写明是代理指标",
    "瓦楞纸与包装纸：现货报价需订阅，未接自动源",
    "海运与空运运价：SCFI/BDI 未接，服饰与家居的到岸成本按缺口处理",
]

TDNET_BASE = "https://www.release.tdnet.info/inbs/"
TDNET_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TDNET_KINDS = [
    ("guidance", ["業績予想の修正", "業績予想値と実績値との差異", "予想の修正"]),
    ("monthly_sales", ["月次"]),
    ("earnings", ["決算短信", "決算説明", "決算補足"]),
    ("shareholder", ["自己株式", "配当", "株式分割", "株主優待"]),
    ("corporate", ["資本業務提携", "子会社", "組織再編", "代表取締役", "M&A", "譲渡"]),
]

VAGUE = ["大幅增长", "显著改善", "明显好转", "大幅提升", "爆发式", "史无前例",
         "最强", "最好", "空前", "彻底反转", "全面复苏"]
CALIBER_GROUPS = [
    {"name": "口径_收入范围", "triggers": ["收入", "营收", "销售额", "同店"],
     "labels": ["同店", "系统销售", "总收入", "净收入", "有机增长", "可比口径", "报表口径",
                "自有销售额", "自有销量", "该店口径", "单店口径", "渠道口径"]},
    {"name": "口径_汇率", "triggers": ["增长", "下滑", "同比"],
     "labels": ["固定汇率", "本币", "美元口径", "报表口径", "有机增长", "同店", "可比口径",
                "销量", "客流", "价格", "毛利", "利润", "份额", "指引", "开店", "关店",
                "净增", "库存", "订单", "产能", "成本", "费用", "股价", "估值", "回报"]},
    {"name": "口径_指引与实际", "triggers": ["指引", "展望", "全年目标"],
     "labels": ["指引", "管理层预期", "尚未兑现", "实际"]},
]
REQUIRED = {
    "full": ["本周结论", "跨市场信号", "细分指数", "技术面", "变化归因", "观察项",
             "上游成本与汇率", "专家与纪要", "待验证"],
    "card": ["本周结论", "跨市场信号", "技术面", "变化归因", "专家与纪要",
             "上游成本与汇率", "观察项", "待验证"],
}
ENG_SUFFIX = re.compile(
    r"[,\s]*\(?(The|Inc|Incorporated|Corp|Corporation|Company|Companies|Co|Ltd|Limited|"
    r"Holdings|Holding|Group|plc|PLC|AG|SA|NV|LLC|International|Brands|Stores)\.?\)?\s*$")


class CliExit(SystemExit):
    """Exit the CLI after a JSON error has already been printed."""


def _card_io():
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_global_consumer_weekly"
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
    raise CliExit(2)


def emit(payload, as_json=True, text_fn=None):
    if as_json or text_fn is None:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(text_fn(payload))


def hk_today() -> date:
    return datetime.now(HK).date()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def anchor_friday(as_of=None) -> date:
    """Most recent Friday on or before the Hong Kong calendar date."""
    day = parse_date(as_of) if as_of else hk_today()
    return day - timedelta(days=(day.weekday() - 4) % 7)


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
    write_text_rel(relative, json.dumps(payload, ensure_ascii=False, indent=indent))


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


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def med(xs):
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def r2(x, nd=2):
    return None if x is None else round(x, nd)


def _yf():
    try:
        import yfinance
    except ImportError:
        fail("yfinance 未安装", "沙箱镜像应已包含 yfinance")
    return yfinance


def price_rel(sym: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.=^-]", "_", sym)
    return f"{PRICE_REL}/{safe}.json"


def load_prices(sym):
    return read_json_rel(price_rel(sym))


def earn_rel(ticker: str) -> str:
    return f"{EARN_REL}/{ticker.replace('/', '_')}.json"


def file_fresh(relative: str, max_age_hours: float) -> bool:
    path = rel_file(relative)
    if not path.is_file():
        return False
    return time.time() - path.stat().st_mtime < max_age_hours * 3600


def fetch_one(yf, sym, period="2y"):
    df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
    df = df.dropna(subset=["Close"])
    if not len(df):
        return None
    rows = []
    for ts, row in df.iterrows():
        volume = row["Volume"]
        rows.append({"d": str(ts.date()), "c": float(row["Close"]),
                     "v": float(volume) if not math.isnan(volume) else 0.0})
    return rows


def cmd_sync_prices(args):
    yf = _yf()
    uni = universe()
    syms = [c["ticker"] for c in uni["companies"]] + list(BENCH.values())
    if args.with_macro:
        syms += list(FX_SYMS.values()) + [c["code"] for c in COMMODITIES]
    t0 = time.time()
    ok, skipped, failed = 0, 0, []
    todo = []
    for sym in syms:
        rel = price_rel(sym)
        if not args.force and file_fresh(rel, args.max_age_hours):
            skipped += 1
        else:
            todo.append(sym)

    def pull(sym):
        try:
            rows = fetch_one(yf, sym)
            if not rows:
                return sym, None, "empty"
            return sym, rows, None
        except Exception as exc:  # noqa: BLE001  # 单只行情源失败只回传错误，线程池继续其余标的
            return sym, None, str(exc)[:160]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, rows, err in pool.map(pull, todo):
            if err:
                failed.append({"symbol": sym, "error": err})
                continue
            write_text_rel(price_rel(sym), json.dumps(rows))
            ok += 1
    emit({"ok": not failed, "fetched": ok, "cached_skipped": skipped,
          "failed": failed, "total": len(syms), "elapsed_sec": round(time.time() - t0, 1),
          "hint": "failed 非空时先重跑 sync-prices --force，仍失败则在报告口径里写明缺口" if failed else None})


def market_as_of(sym, anchor):
    rows = load_prices(sym)
    if not rows:
        return None
    limit = str(anchor)
    for row in reversed(rows):
        if row["d"] <= limit:
            return row["d"]
    return None


def series_upto(rows, as_of):
    return [row for row in rows if row["d"] <= as_of]


def window_return(rows, as_of, cal_days):
    """Calendar window, not a session count. Tokyo and New York holidays differ."""
    series = series_upto(rows, as_of)
    if len(series) < 2:
        return None
    start = str(parse_date(as_of) - timedelta(days=cal_days))
    base = None
    for row in series:
        if row["d"] <= start:
            base = row
        else:
            break
    if base is None:
        return None
    return pct(series[-1]["c"], base["c"])


def build_perf(as_of=None):
    anchor = anchor_friday(as_of)
    uni, subs = universe(), subsectors()
    submeta = {item["key"]: item for item in subs["subsectors"]}
    bench_asof, bench_ret = {}, {}
    for market, symbol in BENCH.items():
        rows = load_prices(symbol)
        if not rows:
            fail(f"缺少基准行情 {symbol}", "先运行 sync-prices")
        ao = market_as_of(symbol, anchor)
        bench_asof[market] = ao
        bench_ret[market] = {key: window_return(rows, ao, n) for key, n in WINDOWS.items()}

    fx_rows = load_prices(FX_SYMS["USDJPY"])
    fx_asof = market_as_of(FX_SYMS["USDJPY"], anchor) if fx_rows else None
    fx_ret = ({key: window_return(fx_rows, fx_asof, n) for key, n in WINDOWS.items()}
              if fx_rows else {key: None for key in WINDOWS})

    stocks, missing = [], []
    for company in uni["companies"]:
        rows = load_prices(company["ticker"])
        if not rows:
            missing.append(company["ticker"])
            continue
        market = company["market"]
        ao = market_as_of(company["ticker"], anchor)
        if ao is None or ao != bench_asof[market]:
            missing.append(company["ticker"])
            continue
        rec = {"ticker": company["ticker"], "name": company["name"], "market": market,
               "subsector": company["subsector"], "as_of": ao}
        for key, n in WINDOWS.items():
            ret = window_return(rows, ao, n)
            rec[f"ret_{key}"] = r2(ret)
            bench = bench_ret[market][key]
            rec[f"exc_{key}"] = r2(None if (ret is None or bench is None) else ret - bench)
            if market == "JP" and ret is not None and fx_ret[key] is not None:
                rec[f"usd_{key}"] = r2(((1 + ret / 100) / (1 + fx_ret[key] / 100) - 1) * 100)
            else:
                rec[f"usd_{key}"] = rec[f"ret_{key}"]
        stocks.append(rec)

    pool = {}
    for market in BENCH:
        sel = [row for row in stocks if row["market"] == market]
        pool[market] = {"count": len(sel),
                        "median_return_w1": r2(med([row["ret_w1"] for row in sel])),
                        "median_excess_w1": r2(med([row["exc_w1"] for row in sel])),
                        "median_usd_w1": r2(med([row["usd_w1"] for row in sel]))}

    subrows = []
    for key, meta in sorted(submeta.items()):
        entry = {"key": key, "name_cn": meta["name_cn"],
                 "cross_market": meta.get("cross_market", True), "by_market": {}}
        for market in BENCH:
            sel = [row for row in stocks if row["subsector"] == key and row["market"] == market]
            if not sel:
                continue
            exc = med([row["exc_w1"] for row in sel])
            pool_exc = pool[market]["median_excess_w1"]
            entry["by_market"][market] = {
                "count": len(sel),
                **{f"median_excess_{key}": r2(med([row[f"exc_{key}"] for row in sel])) for key in WINDOWS},
                "median_usd_w1": r2(med([row["usd_w1"] for row in sel])),
                "alpha_vs_pool_w1": r2(None if exc is None or pool_exc is None else exc - pool_exc),
            }
        subrows.append(entry)
    anchor_s = str(anchor)
    late = {
        market: bench_asof.get(market)
        for market in BENCH
        if bench_asof.get(market) != anchor_s
    }
    holiday_note = None
    if late:
        detail = "，".join(
            f"{market} {late[market] or '缺失'}" for market in ("US", "JP") if market in late
        )
        holiday_note = (
            f"请求周五 {anchor_s} 与实际行情日期不一致：{detail}。"
            "休市或缓存未到周五时用实际日期，不用请求日冒充行情日期。"
        )
    return {"requested_as_of": as_of or anchor_s, "price_as_of": dict(bench_asof),
            "missing_count": len(missing), "holiday_note": holiday_note,
            "as_of_anchor": anchor_s, "market_as_of": bench_asof,
            "benchmarks": {market: {"symbol": BENCH[market],
                                    **{key: r2(value) for key, value in bench_ret[market].items()}}
                           for market in BENCH},
            "fx_usdjpy": {key: r2(value) for key, value in fx_ret.items()},
            "pool": pool, "subsectors": subrows, "stocks": stocks,
            "missing_or_stale": missing}


def cmd_perf(args):
    payload = build_perf(args.as_of)
    if not args.full:
        payload.pop("stocks", None)
    payload["ok"] = True
    emit(payload)


def build_index(as_of=None):
    perf = build_perf(as_of)
    stocks = perf["stocks"]
    pool_exc = {market: perf["pool"][market]["median_excess_w1"] for market in BENCH}
    subs = {item["key"]: item for item in subsectors()["subsectors"]}
    out = []
    for key, meta in sorted(subs.items()):
        row = {"key": key, "name_cn": meta["name_cn"],
               "cross_market": meta.get("cross_market", True), "by_market": {}}
        for market in BENCH:
            sel = [item for item in stocks if item["subsector"] == key and item["market"] == market]
            if not sel:
                continue
            exc1 = [item["exc_w1"] for item in sel if item["exc_w1"] is not None]
            m1 = med(exc1)
            top = max(sel, key=lambda item: item["exc_w1"] if item["exc_w1"] is not None else -1e9)
            ex_top = [item["exc_w1"] for item in sel
                      if item["ticker"] != top["ticker"] and item["exc_w1"] is not None]
            row["by_market"][market] = {
                "count": len(sel),
                **{f"idx_{wkey}": r2(med([item[f"exc_{wkey}"] for item in sel])) for wkey in WINDOWS},
                "breadth_w1": f"{sum(1 for x in exc1 if x > 0)}/{len(exc1)}",
                "breadth_pct": r2(100.0 * sum(1 for x in exc1 if x > 0) / len(exc1)) if exc1 else None,
                "top_name": top["name"], "top_excess_w1": top["exc_w1"],
                "median_ex_top_w1": r2(med(ex_top)),
                "single_name_driven": bool(m1 is not None and med(ex_top) is not None and m1 > 0 >= med(ex_top)),
                "persistent": all((med([item[f"exc_{wkey}"] for item in sel]) or 0) > 0
                                  for wkey in ("w1", "w4", "w13")),
                "sufficient": len(sel) >= 3,
                "thin": len(sel) < MIN_DIVERGENCE_SIDE,
                "alpha_w1": r2(None if m1 is None or pool_exc[market] is None else m1 - pool_exc[market]),
            }
        us, jp = row["by_market"].get("US"), row["by_market"].get("JP")
        if (row["cross_market"] and us and jp and not us["thin"] and not jp["thin"]
                and us["alpha_w1"] is not None and jp["alpha_w1"] is not None):
            row["divergence_w1"] = r2(us["alpha_w1"] - jp["alpha_w1"])
            row["divergence_raw_w1"] = r2(us["idx_w1"] - jp["idx_w1"])
            row["same_direction_w1"] = bool((us["alpha_w1"] > 0) == (jp["alpha_w1"] > 0))
        else:
            row["divergence_w1"] = row["divergence_raw_w1"] = None
            row["same_direction_w1"] = None
            if row["cross_market"] and us and jp:
                thin_side = [market for market, side in (("US", us), ("JP", jp)) if side["thin"]]
                row["divergence_blocked"] = (
                    f"{'/'.join(thin_side)} 侧成分不足 {MIN_DIVERGENCE_SIDE} 家，背离数不出，只作观察")
        out.append(row)
    thin_pairs = [{"key": row["key"], "name_cn": row["name_cn"], "reason": row["divergence_blocked"]}
                  for row in out if row.get("divergence_blocked")]
    return {"as_of_anchor": perf["as_of_anchor"], "market_as_of": perf["market_as_of"],
            "requested_as_of": perf["requested_as_of"], "price_as_of": perf["price_as_of"],
            "missing_count": perf["missing_count"], "holiday_note": perf["holiday_note"],
            "pool": perf["pool"], "divergence_min_side": MIN_DIVERGENCE_SIDE,
            "divergence_blocked_thin": thin_pairs, "subsectors": out}


def cmd_index(args):
    payload = build_index(args.as_of)
    payload["ok"] = True
    emit(payload)


def ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def build_tech(as_of=None):
    anchor = anchor_friday(as_of)
    uni = universe()
    subs = {item["key"]: item["name_cn"] for item in subsectors()["subsectors"]}
    up, down = [], []
    for company in uni["companies"]:
        rows = load_prices(company["ticker"])
        if not rows:
            continue
        ao = market_as_of(company["ticker"], anchor)
        series = series_upto(rows, ao) if ao else []
        if len(series) < 260:
            continue
        bench = load_prices(BENCH[company["market"]])
        bench_ao = market_as_of(BENCH[company["market"]], anchor)
        bench_series = series_upto(bench, bench_ao) if bench else []
        bmap = {row["d"]: row["c"] for row in bench_series}
        closes = [row["c"] for row in series]
        vols = [row["v"] for row in series]
        rs = [row["c"] / bmap[row["d"]] for row in series if row["d"] in bmap]
        last = closes[-1]
        sig = []
        for n, tag in ((60, "60日"), (120, "120日"), (250, "250日")):
            if last >= max(closes[-n:]):
                sig.append(f"创{tag}新高")
            if last <= min(closes[-n:]):
                sig.append(f"创{tag}新低")
        ma200, ma50 = ma(closes, 200), ma(closes, 50)
        prev200 = ma(closes[:-1], 200)
        if ma200 and prev200 and closes[-2] < prev200 <= last / 1 and last > ma200:
            sig.append("上穿200日均线")
        if ma200 and prev200 and closes[-2] > prev200 and last < ma200:
            sig.append("跌破200日均线")
        prev50 = ma(closes[:-1], 50)
        prev_ma200 = ma(closes[:-1], 200)
        if ma50 and ma200 and prev50 and prev_ma200 and prev50 <= prev_ma200 and ma50 > ma200:
            sig.append("50日线上穿200日线")
        vol_ratio = (vols[-1] / (sum(vols[-21:-1]) / 20)) if len(vols) >= 21 and sum(vols[-21:-1]) else None
        if "创60日新高" in sig and vol_ratio and vol_ratio >= 2.0:
            sig.append("放量突破")
        if len(rs) >= 60:
            if rs[-1] >= max(rs[-60:]):
                sig.append("相对强弱创60日新高")
            if rs[-1] <= min(rs[-60:]):
                sig.append("相对强弱创60日新低")
        if not sig:
            continue
        rec = {"ticker": company["ticker"], "name": company["name"], "market": company["market"],
               "subsector": company["subsector"], "subsector_cn": subs.get(company["subsector"]),
               "as_of": ao, "signals": sig, "vol_ratio": r2(vol_ratio),
               "pct_from_52w_high": r2(pct(last, max(closes[-250:]))),
               "ret_w1": r2(window_return(rows, ao, WINDOWS["w1"]))}
        if any("新高" in item for item in sig):
            up.append(rec)
        if any("新低" in item or "跌破" in item for item in sig):
            down.append(rec)
    dedup = {row["ticker"] for row in up} & {row["ticker"] for row in down}
    up = [row for row in up if row["ticker"] not in dedup]
    down = [row for row in down if row["ticker"] not in dedup]
    return {"ok": True, "as_of_anchor": str(anchor), "strong_count": len(up),
            "weak_count": len(down), "strong": up, "weak": down,
            "conflicting_excluded": sorted(dedup)}


def cmd_tech(args):
    emit(build_tech(args.as_of))


def _snapshot_before(anchor: str, require_tech: bool):
    names = [name for name in dir_names(SNAP_REL, ".json") if name[:-5] < anchor]
    while names:
        payload = read_json_rel(f"{SNAP_REL}/{names.pop()}")
        if not payload:
            continue
        if require_tech and not payload.get("tech"):
            continue
        return payload
    return None


def _fwd_excess(ticker, market, d0, d1):
    rows = load_prices(ticker)
    bench = load_prices(BENCH[market])
    if not rows or not bench:
        return None

    def leg(series):
        left = [row for row in series if row["d"] <= d0]
        right = [row for row in series if row["d"] <= d1]
        return pct(right[-1]["c"], left[-1]["c"]) if left and right else None

    stock, index = leg(rows), leg(bench)
    return None if stock is None or index is None else stock - index


def cmd_tech_survival(args):
    current = build_tech(args.as_of)
    anchor = current["as_of_anchor"]
    prior = _snapshot_before(anchor, require_tech=True)
    if not prior:
        emit({"ok": True, "has_baseline": False, "anchor": anchor,
              "note": "上期快照里没有 tech 名单，本期不写存活率"})
        return
    now_strong = {row["ticker"]: row for row in current["strong"]}
    now_weak = {row["ticker"]: row for row in current["weak"]}

    def follow(prev_rows, same, opposite, label):
        rows = []
        for row in prev_rows:
            ticker = row["ticker"]
            if ticker in same:
                state = "仍在同侧"
            elif ticker in opposite:
                state = "翻到另一侧"
            else:
                state = "信号消失"
            rows.append({"ticker": ticker, "name": row.get("name_zh") or row.get("name"),
                         "prev_signals": row.get("signals", []), "state": state,
                         "now_signals": (same.get(ticker) or opposite.get(ticker) or {}).get("signals", [])})
        kept = sum(1 for row in rows if row["state"] == "仍在同侧")
        return {"side": label, "prior_count": len(rows), "still_same_side": kept,
                "flipped": sum(1 for row in rows if row["state"] == "翻到另一侧"),
                "faded": sum(1 for row in rows if row["state"] == "信号消失"),
                "survival_pct": r2(100.0 * kept / len(rows)) if rows else None,
                "detail": rows}

    def fwd_block(prev_rows):
        vals = {"US": [], "JP": []}
        for row in prev_rows:
            value = _fwd_excess(row["ticker"], row["market"], prior["anchor"], anchor)
            if value is not None:
                vals[row["market"]].append(value)
        return {market: {"n": len(xs), "median_excess_next_week": r2(med(xs)),
                         "positive_share_pct": r2(100.0 * sum(1 for v in xs if v > 0) / len(xs)) if xs else None}
                for market, xs in vals.items()}

    pool = {"US": [], "JP": []}
    for company in universe()["companies"]:
        value = _fwd_excess(company["ticker"], company["market"], prior["anchor"], anchor)
        if value is not None:
            pool[company["market"]].append(value)
    tech = prior["tech"]
    emit({"ok": True, "has_baseline": True, "anchor": anchor, "baseline": prior["anchor"],
          "strong": follow(tech.get("strong", []), now_strong, now_weak, "走强"),
          "weak": follow(tech.get("weak", []), now_weak, now_strong, "走弱"),
          "forward": {
              "window": f"{prior['anchor']} → {anchor}",
              "strong_named": fwd_block(tech.get("strong", [])),
              "weak_named": fwd_block(tech.get("weak", [])),
              "pool": {market: {"n": len(xs), "median_excess_next_week": r2(med(xs))}
                       for market, xs in pool.items()},
              "read": "survival_pct 只描述信号是否延续。对错看 forward："
                      "走强名单的次周超额中位数要稳定高于 pool，走弱名单要稳定低于 pool",
          },
          "note": "单期没有解释力，连续六到八期才够判断"})


def cmd_fx(args):
    anchor = anchor_friday(args.as_of)
    out = []
    for label, sym in FX_SYMS.items():
        rows = load_prices(sym)
        if not rows:
            out.append({"pair": label, "symbol": sym, "error": "no cache"})
            continue
        ao = market_as_of(sym, anchor)
        series = series_upto(rows, ao)
        out.append({"pair": label, "symbol": sym, "as_of": ao, "last": r2(series[-1]["c"], 4),
                    **{key: r2(window_return(rows, ao, n)) for key, n in WINDOWS.items()}})
    emit({"ok": True, "as_of_anchor": str(anchor), "fx": out,
          "note": "USDJPY 上行=日元贬值，日股本币回报会高于美元口径回报"})


def cmd_commodities(args):
    anchor = anchor_friday(args.as_of)
    out = []
    for spec in COMMODITIES:
        rows = load_prices(spec["code"])
        if not rows:
            out.append({**spec, "error": "no cache"})
            continue
        ao = market_as_of(spec["code"], anchor)
        series = series_upto(rows, ao)
        w1 = window_return(rows, ao, WINDOWS["w1"])
        w4 = window_return(rows, ao, WINDOWS["w4"])
        out.append({"code": spec["code"], "name_cn": spec["name_cn"], "unit": spec["unit"],
                    "as_of": ao, "last": r2(series[-1]["c"], 3),
                    **{key: r2(window_return(rows, ao, n)) for key, n in WINDOWS.items()},
                    "cost_direction_4w": ("上行" if (w4 or 0) > 2 else "下行" if (w4 or 0) < -2 else "持平"),
                    "downstream": spec["downstream"], "material_w1": bool(abs(w1 or 0) >= 2.0)})
    emit({"ok": True, "as_of_anchor": str(anchor), "commodities": out,
          "material_movers": [item["name_cn"] for item in out if item.get("material_w1")],
          "unavailable": COMMODITY_GAPS,
          "constraint": "只作成本方向输入。锁价、套保和传导滞后各家不同，毛利率另看公司披露。"})


def eng_short(name):
    text = re.sub(r"\s*\([^)]*$", "", name).strip()
    for _ in range(4):
        nxt = ENG_SUFFIX.sub("", text).strip(" ,.&")
        if nxt == text or not nxt:
            break
        text = nxt
    return text or name


def ae_query_names(company):
    out = []
    if company.get("name_zh"):
        out.append(company["name_zh"])
    if company.get("name_ja"):
        out.append(company["name_ja"])
    short = eng_short(company["name"])
    if short not in out:
        out.append(short)
    return out


def cmd_names(args):
    uni = universe()
    subs = {item["key"]: item["name_cn"] for item in subsectors()["subsectors"]}
    rows = uni["companies"]
    if args.market:
        rows = [row for row in rows if row["market"] == args.market]
    if args.subsector:
        rows = [row for row in rows if row["subsector"] == args.subsector]
    if args.tickers:
        want = {token.strip() for token in args.tickers.split(",") if token.strip()}
        rows = [row for row in rows if row["ticker"] in want]
    if args.missing:
        rows = [row for row in rows if not row.get("name_zh")]
    out = [{"ticker": row["ticker"], "market": row["market"],
            "subsector_cn": subs.get(row["subsector"]),
            "name": row["name"], "name_zh": row.get("name_zh"), "name_ja": row.get("name_ja"),
            "ae_query": ae_query_names(row)} for row in rows]
    all_rows = uni["companies"]
    n_zh = sum(1 for row in all_rows if row.get("name_zh"))
    n_ja = sum(1 for row in all_rows if row.get("name_ja"))
    emit({"ok": True, "returned": len(out), "coverage": {
              "total": len(all_rows), "name_zh": n_zh, "name_ja": n_ja,
              "missing_name_zh": len(all_rows) - n_zh},
          "rule": "检索中文库时逐个用 ae_query 里的词，按顺序试。没有 name_zh 的公司用英文名检索。"
                  "查不到写「该库未检索到」。未检索到和没有覆盖是两件不同的事。",
          "rows": out})


def _earn_fetch(yf, ticker):
    buf = io.StringIO()
    tk = yf.Ticker(ticker)
    rows = []
    with contextlib.redirect_stdout(buf):
        frame = tk.get_earnings_dates(limit=12)
    if frame is not None and len(frame):
        cols = {col.lower(): col for col in frame.columns}
        rep_col = cols.get("reported eps")
        for idx, row in frame.iterrows():
            try:  # 财报索引不是日期时跳过这一行，其余日期继续
                day = idx.date().isoformat()
            except Exception:  # noqa: BLE001, S112  # 日期下标异常时跳过这一行，不中断其余财报日
                continue
            reported = False
            if rep_col is not None:
                value = row.get(rep_col)
                reported = value is not None and not (isinstance(value, float) and math.isnan(value))
            rows.append({"date": day, "reported": bool(reported)})
    unique = sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])
    return unique, buf.getvalue().strip()[:120]


def cmd_earnings(args):
    anchor = anchor_friday(args.as_of)
    uni = universe()
    subs = {item["key"]: item["name_cn"] for item in subsectors()["subsectors"]}
    rows = uni["companies"]
    if args.subsector:
        rows = [row for row in rows if row["subsector"] == args.subsector]
    if args.tickers:
        want = {token.strip() for token in args.tickers.split(",") if token.strip()}
        rows = [row for row in rows if row["ticker"] in want]
    yf = None
    fetched = cached = failed = 0
    errors, events = [], []
    for company in rows:
        rel = earn_rel(company["ticker"])
        blob = None
        if not args.refresh:
            try:
                blob = read_json_rel(rel)
            except json.JSONDecodeError:
                blob = None
            if blob:
                try:
                    age = (datetime.now() - datetime.fromisoformat(blob["fetched_at"])).total_seconds() / 3600.0
                    if age > args.max_age_hours:
                        blob = None
                except (KeyError, ValueError):
                    blob = None
        if blob is None:
            if yf is None:
                yf = _yf()
            try:
                dates, warn = _earn_fetch(yf, company["ticker"])
                blob = {"ticker": company["ticker"],
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "dates": dates, "warning": warn}
                write_json_rel(rel, blob)
                fetched += 1
                time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001  # 单家财报日期拉取失败只记这一家，继续其余公司
                failed += 1
                errors.append({"ticker": company["ticker"], "error": str(exc)[:100]})
                continue
        else:
            cached += 1
        for item in blob["dates"]:
            delta = (parse_date(item["date"]) - anchor).days
            if -args.lookback <= delta <= args.days:
                events.append({"date": item["date"], "days_from_anchor": delta,
                               "ticker": company["ticker"], "market": company["market"],
                               "name": company["name"], "name_zh": company.get("name_zh"),
                               "subsector": company["subsector"],
                               "subsector_cn": subs.get(company["subsector"]),
                               "status": "已披露" if item["reported"] else "待披露",
                               "confidence": "指示性" if company["market"] == "JP" else "较可靠"})
    events.sort(key=lambda item: (item["date"], item["ticker"]))
    no_data = []
    for company in rows:
        try:
            blob = read_json_rel(earn_rel(company["ticker"]))
        except json.JSONDecodeError:
            blob = None
        if not blob or not blob.get("dates"):
            no_data.append(company["ticker"])
    emit({"ok": True, "as_of_anchor": str(anchor),
          "window": {"lookback_days": args.lookback, "ahead_days": args.days},
          "universe_scanned": len(rows), "fetched": fetched, "from_cache": cached,
          "fetch_failed": failed, "errors": errors[:10],
          "upcoming_count": sum(1 for item in events if item["days_from_anchor"] > 0),
          "just_reported_count": sum(1 for item in events if item["days_from_anchor"] <= 0),
          "upcoming": [item for item in events if item["days_from_anchor"] > 0],
          "just_reported": [item for item in events if item["days_from_anchor"] <= 0],
          "no_data_tickers": no_data,
          "caveat": "日股排期来自 Yahoo，实测有缺失（如 8136.T 缺 2026 年 8 月那次），"
                    "只作指示性；写进「待验证」的日期用 TDnet 或公司 IR 核对",
          "usage": "先看 just_reported 解释本周为什么动，再用 upcoming 给「待验证」写到期日"})


def read_checkpoints():
    text = read_text_rel(CHECK_REL)
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def write_checkpoints(rows):
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    write_text_rel(CHECK_REL, body)


def cmd_add_checkpoint(args):
    anchor = anchor_friday(args.as_of)
    rows = read_checkpoints()
    due = parse_date(args.due)
    if due <= anchor:
        fail(f"到期日 {args.due} 不晚于本期锚定 {anchor}",
             "待验证条目的到期日必须在未来，否则本期就该直接下结论而不是挂账")
    seq = sum(1 for row in rows if row["anchor"] == str(anchor)) + 1
    rec = {"id": f"{anchor}-{seq:02d}", "anchor": str(anchor), "status": "open",
           "subsector": args.subsector, "company": args.company, "market": args.market,
           "claim": args.claim, "metric": args.metric, "threshold": args.threshold,
           "due": args.due, "source": args.source,
           "created_at": datetime.now().isoformat(timespec="seconds")}
    rows.append(rec)
    write_checkpoints(rows)
    emit({"ok": True, "added": rec, "open_total": sum(1 for row in rows if row["status"] == "open")})


def cmd_checkpoints(args):
    anchor = anchor_friday(args.as_of)
    rows = read_checkpoints()
    selected = rows if args.status == "all" else [row for row in rows if row["status"] == args.status]
    due_now = [row for row in rows if row["status"] == "open" and parse_date(row["due"]) <= anchor]
    later = [row for row in rows if row["status"] == "open" and parse_date(row["due"]) > anchor]
    resolved = [row for row in rows if row["status"] != "open"]
    tally = {}
    for row in resolved:
        tally[row["status"]] = tally.get(row["status"], 0) + 1
    emit({"ok": True, "anchor": str(anchor),
          "due_now_count": len(due_now), "open_later_count": len(later),
          "due_now": due_now, "open_later": later if args.full else later[:10],
          "resolved_tally": tally,
          "resolved": resolved if args.full else resolved[-10:],
          "shown_status": args.status, "shown_count": len(selected),
          "rule": "due_now 里的每一条本期都给结果：resolve-checkpoint 三选一"
                  "（confirmed / falsified / unresolved）。unresolved 写明为什么拿不到数据"})


def cmd_resolve_checkpoint(args):
    rows = read_checkpoints()
    hit = [row for row in rows if row["id"] == args.id]
    if not hit:
        fail(f"没有这条待验证记录: {args.id}", "先跑 checkpoints 看 id；id 形如 2026-08-14-01")
    rec = hit[0]
    if rec["status"] != "open":
        fail(f"{args.id} 已经是 {rec['status']}", "同一条不重复结案")
    if args.result in ("confirmed", "falsified") and not args.evidence.strip():
        fail("confirmed / falsified 必须给 --evidence",
             "证实或证伪都要落到可核对的数字与来源；拿不到数据用 --result unresolved 并写 --note")
    if args.result == "unresolved" and not args.note.strip():
        fail("unresolved 必须给 --note 说明为什么拿不到数据",
             "例如：该指标只在季报披露、下次披露日期在 X；或渠道数据本周未更新")
    rec["status"] = args.result
    rec["resolved_anchor"] = str(anchor_friday(args.as_of))
    rec["resolved_at"] = datetime.now().isoformat(timespec="seconds")
    rec["evidence"] = args.evidence
    rec["note"] = args.note
    write_checkpoints(rows)
    emit({"ok": True, "resolved": rec,
          "open_remaining": sum(1 for row in rows if row["status"] == "open")})


def cmd_add_note(args):
    if args.tier == "D":
        fail("D 级证据不入库", "补一个可核对的一手来源并升到 C 级以上，或直接丢弃")
    words = len(re.findall(r"[A-Za-z']+|[一-鿿]", args.verbatim or ""))
    if args.verbatim and words > 20 and not args.source_attribution:
        fail(f"逐字引用 {words} 词超过 20 词上限且缺 source attribution",
             "压到 20 词以内，或补 --source-attribution")
    if args.type == "expert_call" and not args.source_attribution:
        fail("专家访谈类必须带 source attribution", "补 --source-attribution")
    if args.tier in ("B", "C") and not args.cross_check:
        fail(f"{args.tier} 级证据必须声明交叉验证情况",
             "补 --cross-check '下游/渠道/上游印证来源'；本周确实找不到就传 none")
    keys = {item["key"] for item in subsectors()["subsectors"]}
    if args.subsector not in keys:
        fail(f"未知细分 {args.subsector}", f"用 subsectors 命令查可用 key；共 {len(keys)} 个")
    cross = None if args.cross_check in (None, "none") else args.cross_check
    rec = {"id": f"{int(time.time() * 1000)}", "logged_at": datetime.now().isoformat(timespec="seconds"),
           "week_anchor": str(anchor_friday(args.as_of)), "subsector": args.subsector,
           "market": args.market, "company": args.company, "tier": args.tier,
           "type": args.type, "claim": args.claim, "numbers": args.numbers,
           "caliber": args.caliber, "source": args.source, "source_date": args.source_date,
           "url": args.url, "verbatim": args.verbatim,
           "source_attribution": args.source_attribution,
           "cross_check": cross,
           "cross_checked": bool(cross) or args.tier == "A"}
    existing = read_text_rel(NOTES_REL) or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_text_rel(NOTES_REL, existing + json.dumps(rec, ensure_ascii=False) + "\n")
    emit({"ok": True, "id": rec["id"], "cross_checked": rec["cross_checked"],
          "note": "cross_checked=false 且非 A 级的记录只进观察项。变化归因只收 A 级，或已经交叉验证的 B/C 级。"})


def read_notes():
    text = read_text_rel(NOTES_REL)
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def cmd_notes(args):
    notes = read_notes()
    if args.week:
        notes = [note for note in notes if note["week_anchor"] == args.week]
    if args.subsector:
        notes = [note for note in notes if note["subsector"] == args.subsector]
    single = [note for note in notes if not note["cross_checked"] and note["tier"] != "A"]
    emit({"ok": True, "count": len(notes),
          "by_tier": {tier: sum(1 for note in notes if note["tier"] == tier) for tier in TIERS},
          "cross_checked_count": sum(1 for note in notes if note["cross_checked"]),
          "single_sided_count": len(single),
          "single_sided_ids": [note["id"] for note in single],
          "notes": notes if args.full else None})


def _tech_keep(rows):
    keep = ("ticker", "name", "name_zh", "market", "subsector", "signals", "vol_ratio", "ret_w1")
    return [{key: row.get(key) for key in keep if key in row} for row in rows]


def cmd_snapshot(args):
    if args.write:
        require_delivery(args, "snapshot")
    idx = build_index(args.as_of)
    anchor = idx["as_of_anchor"]
    tech = build_tech(args.as_of)
    snap = {"anchor": anchor, "created_at": datetime.now().isoformat(timespec="seconds"),
            "pool": idx["pool"],
            "subsectors": [{"key": row["key"], "name_cn": row["name_cn"],
                            "divergence_w1": row.get("divergence_w1"),
                            "alpha_w1": {market: side.get("alpha_w1") for market, side in row["by_market"].items()},
                            "by_market": {market: {"idx_w1": side["idx_w1"], "idx_w4": side["idx_w4"],
                                                  "breadth_pct": side["breadth_pct"]}
                                          for market, side in row["by_market"].items()}}
                           for row in idx["subsectors"]],
            "tech": {"strong": _tech_keep(tech["strong"]), "weak": _tech_keep(tech["weak"])},
            "tech_counts": {"strong": tech["strong_count"], "weak": tech["weak_count"]},
            "open_checkpoints": [{key: row[key] for key in ("id", "subsector", "company", "claim",
                                                           "metric", "threshold", "due")}
                                for row in read_checkpoints() if row["status"] == "open"]}
    rel = f"{SNAP_REL}/{anchor}.json"
    if args.write:
        write_json_rel(rel, snap, indent=1)
    emit({"ok": True, "written": bool(args.write), "path": rel if args.write else None,
          "anchor": anchor, "subsector_count": len(snap["subsectors"]),
          "tech_counts": snap["tech_counts"],
          "open_checkpoints": len(snap["open_checkpoints"])})


def cmd_diff(args):
    idx = build_index(args.as_of)
    anchor = idx["as_of_anchor"]
    prior = _snapshot_before(anchor, require_tech=False)
    if not prior:
        emit({"ok": True, "has_baseline": False, "anchor": anchor,
              "note": "无可比历史快照，本期不写「对比上期」整节"})
        return
    pmap = {row["key"]: row for row in prior["subsectors"]}
    flips, moves = [], []
    for row in idx["subsectors"]:
        prev = pmap.get(row["key"])
        if not prev:
            continue
        for market, side in row["by_market"].items():
            old = prev["by_market"].get(market)
            if not old or side["idx_w1"] is None or old["idx_w1"] is None:
                continue
            if (side["idx_w1"] > 0) != (old["idx_w1"] > 0):
                flips.append({"key": row["key"], "name_cn": row["name_cn"], "market": market,
                              "prev": old["idx_w1"], "now": side["idx_w1"]})
        if row.get("divergence_w1") is not None and prev.get("divergence_w1") is not None:
            moves.append({"key": row["key"], "name_cn": row["name_cn"],
                          "prev_divergence": prev["divergence_w1"],
                          "now_divergence": row["divergence_w1"],
                          "change": r2(row["divergence_w1"] - prev["divergence_w1"])})
    moves.sort(key=lambda item: -abs(item["change"] or 0))
    emit({"ok": True, "has_baseline": True, "anchor": anchor, "baseline": prior["anchor"],
          "direction_flips": flips, "largest_divergence_moves": moves[:6]})


def cmd_plan(args):
    idx = build_index(args.as_of)
    scored = []
    for row in idx["subsectors"]:
        score, why = 0.0, []
        if row.get("divergence_w1") is not None and abs(row["divergence_w1"]) >= 2.0:
            score += abs(row["divergence_w1"])
            why.append(f"跨市场背离 {row['divergence_w1']:+.2f} 个百分点")
        for market, side in row["by_market"].items():
            if not side.get("sufficient"):
                continue
            if side.get("persistent"):
                score += 3.0
                why.append(f"{market} 三档窗口同向为正")
            if side.get("single_name_driven"):
                score -= 2.0
                why.append(f"{market} 中位数由 {side['top_name']} 单点抬起，已降权")
            if side["idx_w1"] is not None and abs(side["idx_w1"]) >= 3.0:
                score += abs(side["idx_w1"]) / 2
                why.append(f"{market} 本周 {side['idx_w1']:+.2f}")
        if score > 0:
            scored.append({"key": row["key"], "name_cn": row["name_cn"], "score": r2(score),
                           "reasons": why, "cross_market": row["cross_market"]})
    scored.sort(key=lambda item: -item["score"])
    focus = scored[:args.focus]
    anchor = parse_date(idx["as_of_anchor"])
    friday = anchor.isoformat()
    now = datetime.now(HK)
    due = [{key: row[key] for key in ("id", "subsector", "company", "claim", "metric", "threshold", "due")}
           for row in read_checkpoints()
           if row["status"] == "open" and parse_date(row["due"]) <= anchor]
    emit({"ok": True, "as_of_anchor": friday, "friday": friday,
          "requested_as_of": idx["requested_as_of"], "price_as_of": idx["price_as_of"],
          "holiday_note": idx["holiday_note"], "missing_count": idx["missing_count"],
          "report_run_id": f"global-consumer-weekly-{friday}",
          "market_as_of": idx["market_as_of"],
          "price_window": {
              "start": (anchor - timedelta(days=7)).isoformat(),
              "end": idx["price_as_of"],
              "friday": friday,
              "note": "行情窗口对齐当周周五，各市场终点是不晚于周五的最后交易日。",
          },
          "source_window": {
              "start": (anchor - timedelta(days=5)).isoformat(),
              "end": now.date().isoformat(),
              "end_at": now.isoformat(timespec="seconds"),
              "note": "信源窗口从上周日到本次采集时刻（香港时间），覆盖周五盘后与周末。",
          },
          "focus_subsectors": focus,
          "not_selected_count": max(0, len(scored) - len(focus)),
          "checkpoints_due_this_week": due,
          "rule": f"只对入选的 {args.focus} 个细分做深度归因；其余只出指数表，不查信源。"
                  "上期待验证到期的条目不受选题限制，无论所属细分是否入选都给结果",
          "next": [
              "跑 earnings --lookback 10 --days 21，先用已披露的财报解释本周，再给待验证写到期日",
              "跑 names --subsector <key> 取 ae_query，按顺序试，中文名只来自这份输出",
              "web_search(query, start_date, end_date)；点名公司的卖方用 research(route=\"alphaengine\", operation=\"search_library\", query=中文名, start, end, limit, params={\"sort\": \"relevance\", \"document_categories\": [\"foreign_report\"]})，再 get_document。主题检索才传 industry_names。带 query 时 sort 只用 relevance",
              "research(route=\"guidepoint\", operation=\"search_library\", query, start, end, limit=30, params={\"keywords\": [...], \"recency_bias\": 50})",
              "community(route=\"last30days\", operation=\"search\", query, days=7, plan={\"subqueries\": [{\"search_query\": ..., \"ranking_query\": ...}]})",
              "日股披露跑 jp-disclosures；标题没有数字的 guidance 与 monthly_sales 用 WebFetch 打开 PDF",
              "美股公告用 finance(route=\"sec\", operation=\"list-filings\", ticker, form)",
              "每条结论进正文前要有可核对数字、口径与期间、基数或同比基准",
              "品牌方自述要有下游、渠道、上游或平台侧印证，否则只进观察项",
          ]})


def cmd_validate(args):
    try:
        path = rel_file(args.file)
    except ValueError as exc:
        fail(str(exc), "报告路径必须是工作区内的相对路径")
    if not path.is_file():
        fail(f"文件不存在: {args.file}", "确认路径，或先生成稿件")
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocking, advisory = [], []
    for sec in REQUIRED[args.profile]:
        if sec not in text:
            blocking.append({"rule": "missing_section", "detail": sec})
    for phrase in VAGUE:
        for index, line in enumerate(lines, 1):
            if phrase in line and not re.search(r"\d", line):
                blocking.append({"rule": "vague_phrase", "line": index, "detail": phrase})
    for index, line in enumerate(lines, 1):
        if re.search(r"\$[^$\n]{1,60}\$", line):
            blocking.append({"rule": "dollar_sign", "line": index,
                             "detail": "成对 $ 会被当成公式，金额写「美元」"})
    for index, line in enumerate(lines, 1):
        masked = re.sub(r"\d{4}-\d{2}-\d{2}|\d{4}年|\d+月\d+日|Q[1-4]|\d+FY", "", line)
        if not re.search(r"\d", masked):
            continue
        for group in CALIBER_GROUPS:
            if any(token in masked for token in group["triggers"]) and not any(label in line for label in group["labels"]):
                blocking.append({"rule": "caliber_label", "line": index, "detail": group["name"],
                                 "text": line.strip()[:70]})
                break
    period = ["年", "季", "同比", "环比", "截至", "周", "月", "上半年", "全年", "FY", "Q1", "Q2",
              "Q3", "Q4", "本期", "上期", "近", "过去"]
    for index, line in enumerate(lines, 1):
        if not re.search(r"\d+(\.\d+)?%", line):
            continue
        ctx = line + (lines[index - 2] if index >= 2 else "")
        if not any(token in ctx for token in period):
            advisory.append({"rule": "number_without_period", "line": index, "text": line.strip()[:70]})
    in_check = False
    for index, line in enumerate(lines, 1):
        if line.startswith("#") or re.match(r"^\*\*[^*]+\*\*\s*$", line.strip()):
            in_check = "待验证" in line
            continue
        if not in_check:
            continue
        stripped = line.strip()
        if not stripped.startswith(("- ", "* ")):
            continue
        if re.search(r"\d{4}-\d{2}-\d{2}|\d+月\d+日|财报|季报|中报|年报|决算|月次|下周|次周"
                     r"|到期日|披露节点|下次", stripped):
            continue
        advisory.append({"rule": "checkpoint_without_due", "line": index,
                         "detail": "待验证条目缺到期日或披露节点，下期无法回收",
                         "text": stripped[:70]})
    if args.profile == "card":
        for index, line in enumerate(lines, 1):
            stripped = line.strip()
            if len(stripped) <= 160:
                continue
            if stripped.startswith(("- ", "* ", "> ", "#", "**", "|")):
                continue
            if any(stripped.startswith(prefix) for prefix in ("事实：", "交叉：", "卖方：", "来源：")):
                continue
            advisory.append({"rule": "card_long_paragraph", "line": index,
                             "detail": f"{len(stripped)} 字，拆成列表或分句",
                             "text": stripped[:70]})
    emit({"ok": not blocking, "profile": args.profile, "file": args.file,
          "blocking_count": len(blocking), "advisory_count": len(advisory),
          "blocking": blocking, "advisory": advisory,
          "hint": "blocking 非空不得投递；逐条改写后重跑" if blocking else None})


def cmd_universe(args):
    uni = universe()
    emit({"ok": True, "meta": uni["meta"],
          "companies": uni["companies"] if args.full else None})


def cmd_subsectors(args):
    emit({"ok": True, **subsectors()})


def cmd_anchor(args):
    friday = anchor_friday(args.as_of)
    emit({"ok": True, "hk_today": hk_today().isoformat(),
          "friday": friday.isoformat(),
          "report_run_id": f"global-consumer-weekly-{friday.isoformat()}"})


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
    emit({"ok": True, "written": rel, "source": args.file})


def cmd_self_test(args):
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    uni = universe()
    subs = subsectors()
    keys = {item["key"] for item in subs["subsectors"]}
    bad = [company["ticker"] for company in uni["companies"] if company["subsector"] not in keys]
    add("taxonomy_consistent", not bad, f"{len(bad)} 个标的的细分不在 subsectors.json 中")
    add("universe_loaded", len(uni["companies"]) > 150,
        f"{len(uni['companies'])} 家；US {uni['meta']['market_counts']['US']} / JP {uni['meta']['market_counts']['JP']}")
    dup = len(uni["companies"]) - len({company["ticker"] for company in uni["companies"]})
    add("no_duplicate_ticker", dup == 0, f"{dup} 个重复 ticker")
    try:
        cached = len(dir_names(PRICE_REL, ".json"))
        if cached:
            add("price_cache", True, f"{cached} 个缓存文件")
        else:
            add("price_cache", True, "缓存为空，运行 sync-prices 后才有行情")
    except ValueError as exc:
        add("price_cache", False, str(exc))
    add("templates_present",
        all((SKILL_DIR / "templates" / name).is_file() for name in ("card.md", "full-report.md")),
        "card.md / full-report.md")
    try:
        notes = read_notes()
        add("notes_readable", isinstance(notes, list), f"{len(notes)} 条事实记录")
    except ValueError as exc:
        add("notes_readable", False, str(exc))
    n_zh = sum(1 for company in uni["companies"] if company.get("name_zh"))
    n_ja = sum(1 for company in uni["companies"] if company.get("name_ja"))
    jp_total = sum(1 for company in uni["companies"] if company["market"] == "JP")
    add("query_names_present", n_zh > 0 and n_ja == jp_total,
        f"name_zh {n_zh}/{len(uni['companies'])}，name_ja {n_ja}/{jp_total}（日股应全覆盖，来自 JPX 上場銘柄一覧）")
    try:
        cps = read_checkpoints()
        add("checkpoints_readable", isinstance(cps, list),
            f"{len(cps)} 条待验证记录，其中未结 {sum(1 for row in cps if row['status'] == 'open')} 条")
    except ValueError as exc:
        add("checkpoints_readable", False, str(exc))
    try:
        probe = rel_file(f"{DB_REL}/.write-probe")
        probe.parent.mkdir(parents=True, exist_ok=True)
        add("db_writable", os.access(probe.parent, os.W_OK), DB_REL)
    except (OSError, ValueError) as exc:
        add("db_writable", False, str(exc))
    anchor = anchor_friday(None)
    add("anchor_is_friday", anchor.weekday() == 4, f"锚定 {anchor}（周五）")
    if args.network:
        yf = _yf()
        try:
            rows = fetch_one(yf, "^GSPC", period="1mo")
            add("yfinance_reachable", bool(rows) and len(rows) > 5, f"^GSPC {len(rows or [])} bars")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("yfinance_reachable", False, str(exc)[:120])
        try:
            rows = fetch_one(yf, "7203.T", period="1mo")
            add("jp_quotes_reachable", bool(rows) and len(rows) > 5, f"7203.T {len(rows or [])} bars")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("jp_quotes_reachable", False, str(exc)[:120])
        try:
            rows = fetch_one(yf, "SB=F", period="1mo")
            add("commodity_source_reachable", bool(rows) and len(rows) > 5, f"SB=F {len(rows or [])} bars")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("commodity_source_reachable", False, str(exc)[:120])
        try:
            rows = fetch_one(yf, "JPY=X", period="1mo")
            add("fx_source_reachable", bool(rows) and len(rows) > 5, f"JPY=X {len(rows or [])} bars")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("fx_source_reachable", False, str(exc)[:120])
        try:
            day = anchor_friday(None)
            code_map = {company["ticker"][:-2] + "0": company for company in uni["companies"]
                        if company["market"] == "JP" and company["ticker"].endswith(".T")}
            _hits, meta = _tdnet_day(day, code_map)
            add("jp_disclosure_source_reachable",
                bool(meta.get("ok")) and meta.get("disclosures_that_day", 0) > 0,
                f"TDnet {day} 当日全市场 {meta.get('disclosures_that_day', 0)} 条，命中本池 {len(_hits)} 条")
        except Exception as exc:  # noqa: BLE001  # 网络自检失败只记检查项，不让异常冒出命令
            add("jp_disclosure_source_reachable", False, str(exc)[:120])
    passed = sum(1 for item in checks if item["ok"])
    emit({"ok": passed == len(checks), "passed": passed, "total": len(checks),
          "checks": checks,
          "hint": None if passed == len(checks) else "先看失败项 detail；行情为空时运行 sync-prices"})
    raise CliExit(0 if passed == len(checks) else 2)


def _tdnet_classify(title):
    for kind, keys in TDNET_KINDS:
        for key in keys:
            if key in title:
                return kind
    return "other"


def _tdnet_fetch(url, timeout=25):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": TDNET_UA})
    with urllib.request.urlopen(req, timeout=timeout) as handle:
        return handle.read().decode("utf-8", "replace")


def _tdnet_day(day, code_map, max_pages=15):
    import html as html_lib
    hits, page, total = [], 1, None
    while True:
        url = "{}I_list_{:03d}_{}.html".format(TDNET_BASE, page, day.strftime("%Y%m%d"))
        try:
            text = _tdnet_fetch(url)
        except Exception as exc:  # noqa: BLE001  # 披露页拉取失败：首页记错误，后续页停止翻页
            if page == 1:
                return [], {"date": day.isoformat(), "ok": False, "error": str(exc)[:120]}
            break
        if total is None:
            matched = re.search(r"全(\d+)件", text)
            total = int(matched.group(1)) if matched else 0
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.DOTALL)
        seen = 0
        for row in rows:
            cells = [html_lib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
                     for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
            if len(cells) < 4 or not re.match(r"^\d{2}:\d{2}$", cells[0]):
                continue
            seen += 1
            code = cells[1]
            if code not in code_map:
                continue
            company = code_map[code]
            pdf = re.findall(r'href="([^"]+\.pdf)"', row)
            title = cells[3]
            hits.append({"date": day.isoformat(), "time": cells[0], "code": code,
                         "ticker": company["ticker"], "name": company["name"],
                         "subsector": company["subsector"], "title": title,
                         "kind": _tdnet_classify(title),
                         "url": (TDNET_BASE + pdf[0]) if pdf else ""})
        if seen == 0:
            break
        if total and page * 100 >= total:
            break
        page += 1
        if page > max_pages:
            break
        time.sleep(0.3)
    return hits, {"date": day.isoformat(), "ok": True, "disclosures_that_day": total or 0}


def cmd_jp_disclosures(args):
    # 披露截止日是采集或成稿当天。省略 --as-of 时用香港当天，不折成周五。friday 只标行情周。
    raw = getattr(args, "as_of", None)
    queried = parse_date(raw) if raw else hk_today()
    friday = anchor_friday(queried.isoformat())
    days = max(1, int(args.days))
    uni = universe()
    code_map = {}
    for company in uni["companies"]:
        if company["market"] != "JP":
            continue
        ticker = company["ticker"]
        if ticker.endswith(".T") and ticker[:-2].isdigit():
            code_map[ticker[:-2] + "0"] = company
    hits, meta, errors = [], [], []
    for offset in range(days):
        day = queried - timedelta(days=offset)
        found, info = _tdnet_day(day, code_map)
        hits += found
        meta.append(info)
        if not info.get("ok"):
            errors.append(info)
    hits.sort(key=lambda item: (item["date"], item["time"]), reverse=True)
    by_kind, by_sub = {}, {}
    for item in hits:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_sub[item["subsector"]] = by_sub.get(item["subsector"], 0) + 1
    material = [item for item in hits if item["kind"] in ("guidance", "earnings", "monthly_sales")]
    out = {"ok": True, "as_of": queried.isoformat(), "friday": friday.isoformat(),
           "window_days": days,
           "jp_universe_covered": len(code_map),
           "hits": len(hits), "material_hits": len(material),
           "by_kind": by_kind, "by_subsector": by_sub,
           "days": meta, "fetch_errors": errors,
           "note": "TDnet 仅保留约 31 天历史；超出后改查公司 IR。业绩预想修正与月次数据是本层最有信息量的两类。"}
    out["items"] = hits if getattr(args, "full", False) else material[:40]
    if not getattr(args, "full", False):
        out["items_note"] = "默认只返回 guidance/earnings/monthly_sales 前 40 条；--full 返回全部"
    emit(out)


def build_parser():
    parser = argparse.ArgumentParser(prog="global-consumer-weekly")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    delivery = argparse.ArgumentParser(add_help=False)
    delivery.add_argument("--content-kind", default=None)
    delivery.add_argument("--delivery-confirmed", action="store_true")
    delivery.add_argument("--status", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    item = sub.add_parser("self-test", parents=[shared])
    item.add_argument("--network", action="store_true")
    item.set_defaults(func=cmd_self_test)

    item = sub.add_parser("anchor", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_anchor)

    item = sub.add_parser("universe", parents=[shared])
    item.add_argument("--full", action="store_true")
    item.set_defaults(func=cmd_universe)

    item = sub.add_parser("subsectors", parents=[shared])
    item.set_defaults(func=cmd_subsectors)

    item = sub.add_parser("sync-prices", parents=[shared])
    item.add_argument("--force", action="store_true")
    item.add_argument("--with-macro", action="store_true", default=True)
    item.add_argument("--max-age-hours", type=float, default=12.0)
    item.set_defaults(func=cmd_sync_prices)

    item = sub.add_parser("perf", parents=[shared])
    item.add_argument("--as-of")
    item.add_argument("--full", action="store_true")
    item.set_defaults(func=cmd_perf)

    item = sub.add_parser("index", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_index)

    item = sub.add_parser("tech", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_tech)

    item = sub.add_parser("tech-survival", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_tech_survival)

    item = sub.add_parser("fx", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_fx)

    item = sub.add_parser("commodities", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_commodities)

    item = sub.add_parser("jp-disclosures", parents=[shared])
    item.add_argument("--as-of")
    item.add_argument("--days", type=int, default=7)
    item.add_argument("--full", action="store_true")
    item.set_defaults(func=cmd_jp_disclosures)

    item = sub.add_parser("names", parents=[shared])
    item.add_argument("--market", choices=["US", "JP"])
    item.add_argument("--subsector")
    item.add_argument("--tickers")
    item.add_argument("--missing", action="store_true")
    item.set_defaults(func=cmd_names)

    item = sub.add_parser("earnings", parents=[shared])
    item.add_argument("--as-of")
    item.add_argument("--subsector")
    item.add_argument("--tickers")
    item.add_argument("--days", type=int, default=21)
    item.add_argument("--lookback", type=int, default=10)
    item.add_argument("--refresh", action="store_true")
    item.add_argument("--max-age-hours", type=float, default=168.0)
    item.set_defaults(func=cmd_earnings)

    item = sub.add_parser("add-checkpoint", parents=[shared])
    item.add_argument("--subsector", required=True)
    item.add_argument("--company", default="")
    item.add_argument("--market", default="US")
    item.add_argument("--claim", required=True)
    item.add_argument("--metric", required=True)
    item.add_argument("--threshold", required=True)
    item.add_argument("--due", required=True)
    item.add_argument("--source", required=True)
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_add_checkpoint)

    item = sub.add_parser("checkpoints", parents=[shared])
    item.add_argument("--as-of")
    item.add_argument("--status", choices=["open", "confirmed", "falsified", "unresolved", "all"], default="open")
    item.add_argument("--full", action="store_true")
    item.set_defaults(func=cmd_checkpoints)

    item = sub.add_parser("resolve-checkpoint", parents=[shared])
    item.add_argument("--id", required=True)
    item.add_argument("--result", required=True, choices=["confirmed", "falsified", "unresolved"])
    item.add_argument("--evidence", default="")
    item.add_argument("--note", default="")
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_resolve_checkpoint)

    item = sub.add_parser("plan", parents=[shared])
    item.add_argument("--as-of")
    item.add_argument("--focus", type=int, default=5)
    item.set_defaults(func=cmd_plan)

    item = sub.add_parser("add-note", parents=[shared])
    item.add_argument("--subsector", required=True)
    item.add_argument("--market", default="US")
    item.add_argument("--company", default="")
    item.add_argument("--tier", required=True, choices=list(TIERS))
    item.add_argument("--type", required=True)
    item.add_argument("--claim", required=True)
    item.add_argument("--numbers", default="")
    item.add_argument("--caliber", default="")
    item.add_argument("--source", required=True)
    item.add_argument("--source-date", required=True)
    item.add_argument("--url", default="")
    item.add_argument("--verbatim", default="")
    item.add_argument("--source-attribution", default="")
    item.add_argument("--cross-check", default=None)
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_add_note)

    item = sub.add_parser("notes", parents=[shared])
    item.add_argument("--week")
    item.add_argument("--subsector")
    item.add_argument("--full", action="store_true")
    item.set_defaults(func=cmd_notes)

    item = sub.add_parser("snapshot", parents=[shared, delivery])
    item.add_argument("--as-of")
    item.add_argument("--write", action="store_true")
    item.set_defaults(func=cmd_snapshot)

    item = sub.add_parser("diff", parents=[shared])
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_diff)

    item = sub.add_parser("validate", parents=[shared])
    item.add_argument("--file", required=True)
    item.add_argument("--profile", choices=["full", "card"], default="full")
    item.set_defaults(func=cmd_validate)

    item = sub.add_parser("archive", parents=[shared, delivery])
    item.add_argument("--file", required=True)
    item.add_argument("--as-of")
    item.set_defaults(func=cmd_archive)
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else (code if isinstance(code, int) else 2)
    try:
        bind_workspace(getattr(args, "workspace_dir", None))
        args.func(args)
    except CliExit as exc:
        code = exc.code
        return 2 if code is None else (code if isinstance(code, int) else 2)
    except Exception as exc:  # noqa: BLE001  # 命令行边界把未分类错误收成 JSON，避免栈信息直接吐出
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "hint": "检查输入参数；路径必须留在工作区内"}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
