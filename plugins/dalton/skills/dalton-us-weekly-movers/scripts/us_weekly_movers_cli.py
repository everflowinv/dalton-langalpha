#!/usr/bin/env python3
"""美股周涨跌幅榜算术层。不含模型判断，也不联网以外的研究。

命令:
  universe       拉取股票池并按市值下限过滤
  scan           计算周涨跌幅、量比、榜单和行业聚类
  self-test      依赖与数据通路检查，周报流程不把它当门槛
  render-card    把已写好的 Markdown 收成一张卡片
  append-themes  投递确认后追加主题行，重复键跳过
  recent-themes  读最近一个 week_end 的主题行
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

TEMPLATE_VERSION = "us-weekly-movers-v1.0"
SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
EXCLUDE_NAME_RE = re.compile(
    r"\b(Warrant|Warrants|Right|Rights|Unit|Units|Preferred|Preference|Notes?|Debentures?|Trust Preferred|Subordinated)\b",
    re.IGNORECASE,
)
THEME_FIELDS = (
    "week_end",
    "side",
    "label",
    "symbols",
    "catalyst_type",
    "median_ret_week",
    "verdict",
)
STATE_REL = "state/us-weekly-movers"
TEMP_REL = "temp/us-weekly-movers"


def _card_io() -> Any:
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_us_weekly_movers"
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


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def error_hint(err: Exception) -> str:
    msg = str(err).lower()
    if "ssl" in msg or "tls" in msg:
        return "TLS/SSL failure. Check proxy/network settings."
    if "screener" in msg or "nasdaq" in msg:
        return "NASDAQ screener 拉不到。若 state/us-weekly-movers/universe.json 已在，继续 scan，并在周报注明池子沿用日期。"
    if "universe" in msg and "not found" in msg:
        return "先跑 universe，或确认 state/us-weekly-movers/universe.json 已导入。"
    if "not found" in msg:
        return "Resource not found. Verify parameters and retry."
    if "permission" in msg or "forbidden" in msg or "401" in msg or "403" in msg:
        return "Authentication/permission issue. Verify credentials and scopes."
    return "Check arguments and environment, then retry."


def _log(msg: str, args: argparse.Namespace) -> None:
    if not getattr(args, "quiet", False):
        print(msg, file=sys.stderr)


def _num(value: Any) -> float | None:
    try:
        text = str(value).replace(",", "").replace("$", "").replace("%", "").strip()
        return float(text) if text and text.upper() != "NA" else None
    except (TypeError, ValueError):
        return None


def _write_text(workspace: Path, relative: str, text: str) -> None:
    """Write text only after the resolved parent is inside the workspace."""
    path = _card_io().workspace_file(workspace, relative)
    _card_io().write_bytes_exclusive(path, text.encode("utf-8"))


def fetch_screener(timeout: int = 60) -> list[dict[str, Any]]:
    import requests

    params = {"tableonly": "true", "limit": "10000", "offset": "0", "download": "true"}
    response = requests.get(SCREENER_URL, params=params, headers=SCREENER_HEADERS, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"NASDAQ screener HTTP {response.status_code}")
    rows = response.json().get("data", {}).get("rows", [])
    if len(rows) < 3000:
        raise RuntimeError(f"NASDAQ screener 只返回 {len(rows)} 行，疑似截断")
    return rows


def build_universe(rows: list[dict[str, Any]], min_cap: float) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        name = row.get("name") or ""
        cap = _num(row.get("marketCap"))
        if not symbol or cap is None or cap < min_cap:
            continue
        if "^" in symbol or EXCLUDE_NAME_RE.search(name):
            continue
        if not re.fullmatch(r"[A-Z]{1,5}(/[A-Z])?", symbol):
            continue
        out.append(
            {
                "symbol": symbol,
                "yf_symbol": symbol.replace("/", "-"),
                "name": name.replace(" Common Stock", "").replace(" Class A", " Cl A").strip(),
                "market_cap": cap,
                "sector": row.get("sector") or "Unclassified",
                "industry": row.get("industry") or "Unclassified",
                "country": row.get("country") or "",
                "ipo_year": row.get("ipoyear") or "",
            }
        )
    out.sort(key=lambda item: -item["market_cap"])
    return out


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def load_universe(workspace: Path, path: str | None = None) -> dict[str, Any]:
    if path:
        source = _card_io().workspace_file(workspace, path)
    else:
        source = workspace / STATE_REL / "universe.json"
        if source.is_symlink():
            raise RuntimeError("refusing symlink")
    if not source.is_file():
        raise RuntimeError("universe not found: state/us-weekly-movers/universe.json")
    loaded = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("rows"), list):
        raise TypeError("universe file is missing rows")
    return loaded


def cmd_universe(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    rows = fetch_screener()
    universe = build_universe(rows, args.min_cap)
    payload = {
        "ok": True,
        "as_of": date.today().isoformat(),
        "min_cap": args.min_cap,
        "screener_rows": len(rows),
        "universe_n": len(universe),
        "by_sector": _count_by(universe, "sector"),
        "path": f"{STATE_REL}/universe.json",
    }
    _write_text(
        workspace,
        f"{STATE_REL}/universe.json",
        json.dumps({"as_of": payload["as_of"], "min_cap": args.min_cap, "rows": universe}, ensure_ascii=False),
    )
    if not args.json:
        _log(
            f"universe: {len(universe)} names from {len(rows)} screener rows -> {payload['path']}",
            args,
        )
    return payload


def download_prices(symbols: list[str], chunk: int = 400, period: str = "3mo", args: argparse.Namespace | None = None) -> Any:
    import pandas as pd
    import yfinance as yf

    frames = []
    for start in range(0, len(symbols), chunk):
        batch = symbols[start : start + chunk]
        if args:
            _log(f"  download {start + 1}-{start + len(batch)} / {len(symbols)}", args)
        frame = yf.download(
            batch,
            period=period,
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="ticker",
        )
        if frame is None or frame.empty:
            continue
        if not isinstance(frame.columns, pd.MultiIndex):
            frame.columns = pd.MultiIndex.from_product([[batch[0]], frame.columns])
        frames.append(frame)
    if not frames:
        raise RuntimeError("yfinance 未返回任何行情")
    return pd.concat(frames, axis=1)


def week_bounds(index: Any) -> tuple[Any, Any]:
    """返回上一 ISO 周最后交易日，以及本周最后交易日。"""
    dates = list(index)
    end = dates[-1]
    end_week = (end.isocalendar()[0], end.isocalendar()[1])
    prev = None
    for item in reversed(dates):
        if (item.isocalendar()[0], item.isocalendar()[1]) != end_week:
            prev = item
            break
    if prev is None:
        raise RuntimeError("行情窗口不足两周")
    return prev, end


def compute_metrics(prices: Any, universe: list[dict[str, Any]], vol_lookback: int = 20) -> tuple[list[dict[str, Any]], str, str]:
    import numpy as np

    prev, end = week_bounds(prices.index)
    idx = list(prices.index)
    i_prev = idx.index(prev)
    i_end = idx.index(end)
    prev_week = (prev.isocalendar()[0], prev.isocalendar()[1])
    prev2 = None
    for item in reversed(idx[: i_prev + 1]):
        if (item.isocalendar()[0], item.isocalendar()[1]) != prev_week:
            prev2 = item
            break
    rows = []
    meta = {item["yf_symbol"]: item for item in universe}
    tickers = sorted({column[0] for column in prices.columns})
    for ticker in tickers:
        if ticker not in meta:
            continue
        try:
            close = prices[ticker]["Close"]
            volume = prices[ticker]["Volume"]
        except KeyError:
            continue
        close_end = close.iloc[i_end]
        close_prev = close.iloc[i_prev]
        if not (np.isfinite(close_end) and np.isfinite(close_prev)) or close_prev <= 0:
            continue
        week_volume = volume.iloc[i_prev + 1 : i_end + 1]
        base_volume = volume.iloc[max(0, i_prev - vol_lookback + 1) : i_prev + 1]
        if week_volume.count() < 3 or base_volume.count() < 10 or base_volume.mean() <= 0:
            continue
        ret_prev_week = None
        if prev2 is not None:
            close_prev2 = close.iloc[idx.index(prev2)]
            if np.isfinite(close_prev2) and close_prev2 > 0:
                ret_prev_week = float(close_prev / close_prev2 - 1)
        high_63 = close.iloc[: i_end + 1].tail(63).max()
        info = meta[ticker]
        rows.append(
            {
                "symbol": info["symbol"],
                "name": info["name"],
                "sector": info["sector"],
                "industry": info["industry"],
                "market_cap": info["market_cap"],
                "close": float(close_end),
                "ret_week": float(close_end / close_prev - 1),
                "ret_prev_week": ret_prev_week,
                "vol_ratio": float(week_volume.mean() / base_volume.mean()),
                "drawdown_from_63d_high": float(close_end / high_63 - 1) if np.isfinite(high_63) and high_63 > 0 else None,
                "days_in_week": int(week_volume.count()),
            }
        )
    return rows, prev.date().isoformat(), end.date().isoformat()


def earnings_flags(symbols: list[str], start: str, end: str, workers: int = 8) -> dict[str, dict[str, Any]]:
    """标记窗口 (start, end] 内有财报的票。失败视为未知。"""
    import yfinance as yf

    start_day = datetime.fromisoformat(start).date()
    end_day = datetime.fromisoformat(end).date()

    def one(symbol: str) -> tuple[str, bool | None, str | None]:
        try:
            dates = yf.Ticker(symbol.replace("/", "-")).earnings_dates
            if dates is None or dates.empty:
                return symbol, False, None
            hits = [item for item in dates.index if start_day < item.date() <= end_day]
            return symbol, bool(hits), (hits[0].date().isoformat() if hits else None)
        except Exception:  # noqa: BLE001 - one ticker's earnings failure is unknown, not a scan failure
            return symbol, None, None

    out: dict[str, dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, flag, when in pool.map(one, symbols):
            out[symbol] = {"earnings_in_week": flag, "earnings_date": when}
    return out


def cluster(rows: list[dict[str, Any]], key: str, min_n: int) -> list[dict[str, Any]]:
    import statistics

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    out = []
    for label, group in groups.items():
        if label == "Unclassified" or len(group) < min_n:
            continue
        out.append(
            {
                "level": key,
                "label": label,
                "sector": group[0]["sector"],
                "count": len(group),
                "median_ret_week": statistics.median(item["ret_week"] for item in group),
                "median_vol_ratio": statistics.median(item["vol_ratio"] for item in group),
                "symbols": [item["symbol"] for item in sorted(group, key=lambda item: -abs(item["ret_week"]))],
            }
        )
    out.sort(key=lambda item: (-item["count"], -abs(item["median_ret_week"])))
    return out


def load_last_scan(workspace: Path) -> dict[str, Any] | None:
    path = workspace / STATE_REL / "last_scan.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _market_context(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    import statistics

    if not metrics:
        return {}
    returns = [item["ret_week"] for item in metrics]
    return {
        "median_ret_week": statistics.median(returns),
        "pct_up": sum(1 for item in returns if item > 0) / len(returns),
        "n": len(returns),
    }


def _fmt_cap(cap: float) -> str:
    return f"{cap / 1e9:.1f}B"


def render_candidates(result: dict[str, Any]) -> str:
    lines = [f"# 美股周涨跌幅榜候选 {result['week_start']} → {result['week_end']}"]
    market = result.get("market", {})
    volume_note = (
        f"量比 ≥ {result['params']['min_vol_ratio']} 的 {result['eligible_n']}"
        if result["params"]["min_vol_ratio"] > 0
        else f"可进榜 {result['eligible_n']}（量比不过滤）"
    )
    lines.append(
        f"池子 {result['universe_n']} 只（市值 ≥ {result['params']['min_cap'] / 1e9:.0f}B），有行情 {result['priced_n']}，"
        + volume_note
        + f"；进榜门槛周涨跌 ≥ {result['params']['min_move'] * 100:.0f}%。"
        + f"全池周涨幅中位数 {market.get('median_ret_week', 0) * 100:+.1f}%，上涨占比 {market.get('pct_up', 0) * 100:.0f}%。"
    )
    for side, title in (("gainers", "涨幅榜"), ("losers", "跌幅榜")):
        lines.append(f"\n## {title} 行业聚类（同一行业 ≥ {result['params']['cluster_min_industry']} 只）")
        groups = result["clusters"][side]["industry"]
        if not groups:
            lines.append("（无）")
        for item in groups:
            continued = " ｜上周同名聚类已出现" if item["label"] in result["last_week_clusters"][side] else ""
            lines.append(
                f"- **{item['label']}**（{item['sector']}）{item['count']} 只，中位周涨幅 {item['median_ret_week'] * 100:+.1f}%，"
                f"中位量比 {item['median_vol_ratio']:.1f}x：{', '.join(item['symbols'])}{continued}"
            )
        sectors = result["clusters"][side]["sector"]
        if sectors:
            bits = []
            for item in sectors:
                shown = ", ".join(item["symbols"][:6])
                if item["count"] > 6:
                    shown += "…"
                bits.append(f"{item['label']} {item['count']} 只（{shown}）")
            lines.append(f"板块级聚类（≥ {result['params']['cluster_min_sector']} 只）：" + "；".join(bits))
        lines.append(f"\n## {title} 前 {result['params']['top_n']}")
        lines.append("| # | 代码 | 公司 | 行业 | 市值 | 本周 | 上周 | 量比 | 距63日高 | 财报周 | 上周在榜 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for item in result[side]:
            earnings = item.get("earnings_in_week")
            earnings_text = "是" if earnings else ("?" if earnings is None else "")
            prev_text = f"{item['ret_prev_week'] * 100:+.0f}%" if item.get("ret_prev_week") is not None else ""
            drawdown = item.get("drawdown_from_63d_high")
            drawdown_text = f"{drawdown * 100:.0f}%" if drawdown is not None else ""
            lines.append(
                f"| {item['rank']} | {item['symbol']} | {item['name'][:28]} | {item['industry'][:30]} | {_fmt_cap(item['market_cap'])} | "
                f"{item['ret_week'] * 100:+.1f}% | {prev_text} | {item['vol_ratio']:.1f}x | {drawdown_text} | {earnings_text} | "
                f"{'是' if item.get('on_last_week_list') else ''} |"
            )
    return "\n".join(lines) + "\n"


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "rank",
        "symbol",
        "name",
        "industry",
        "sector",
        "market_cap",
        "ret_week",
        "ret_prev_week",
        "vol_ratio",
        "drawdown_from_63d_high",
        "earnings_in_week",
        "earnings_date",
        "on_last_week_list",
    )
    return {key: item.get(key) for key in keys}


def cmd_scan(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    universe = load_universe(workspace, args.universe_file)
    rows = [item for item in universe["rows"] if item["market_cap"] >= args.min_cap]
    if args.limit:
        rows = rows[: args.limit]
    _log(f"scan: {len(rows)} names, downloading prices…", args)
    prices = download_prices([item["yf_symbol"] for item in rows], args=args)
    metrics, week_start, week_end = compute_metrics(prices, rows)
    _log(f"metrics for {len(metrics)} names, window {week_start} -> {week_end}", args)
    eligible = [
        item
        for item in metrics
        if item["days_in_week"] >= 3 and (args.min_vol_ratio <= 0 or item["vol_ratio"] >= args.min_vol_ratio)
    ]
    gainers = sorted([item for item in eligible if item["ret_week"] >= args.min_move], key=lambda item: -item["ret_week"])[: args.top_n]
    losers = sorted([item for item in eligible if item["ret_week"] <= -args.min_move], key=lambda item: item["ret_week"])[: args.top_n]
    for index, item in enumerate(gainers, 1):
        item["rank"] = index
    for index, item in enumerate(losers, 1):
        item["rank"] = index
    if not args.no_earnings:
        _log("earnings flags…", args)
        flags = earnings_flags([item["symbol"] for item in gainers + losers], week_start, week_end)
        for item in gainers + losers:
            item.update(flags.get(item["symbol"], {}))
    last = load_last_scan(workspace)
    last_gainers = {item["symbol"] for item in (last or {}).get("gainers", [])}
    last_losers = {item["symbol"] for item in (last or {}).get("losers", [])}
    for item in gainers:
        item["on_last_week_list"] = item["symbol"] in last_gainers
    for item in losers:
        item["on_last_week_list"] = item["symbol"] in last_losers
    result: dict[str, Any] = {
        "ok": True,
        "week_start": week_start,
        "week_end": week_end,
        "universe_n": len(rows),
        "priced_n": len(metrics),
        "eligible_n": len(eligible),
        "params": {
            "min_cap": args.min_cap,
            "top_n": args.top_n,
            "min_vol_ratio": args.min_vol_ratio,
            "min_move": args.min_move,
            "cluster_min_industry": args.cluster_min,
            "cluster_min_sector": args.cluster_min + 1,
        },
        "market": _market_context(metrics),
        "gainers": gainers,
        "losers": losers,
        "clusters": {
            "gainers": {
                "industry": cluster(gainers, "industry", args.cluster_min),
                "sector": cluster(gainers, "sector", args.cluster_min + 1),
            },
            "losers": {
                "industry": cluster(losers, "industry", args.cluster_min),
                "sector": cluster(losers, "sector", args.cluster_min + 1),
            },
        },
        "last_week_clusters": {
            "gainers": [item["label"] for item in ((last or {}).get("clusters", {}).get("gainers", {}).get("industry", []))],
            "losers": [item["label"] for item in ((last or {}).get("clusters", {}).get("losers", {}).get("industry", []))],
        },
        "meta": {"template": TEMPLATE_VERSION, "ts": datetime.now().isoformat(timespec="seconds")},
        "paths": {
            "json": f"{STATE_REL}/scan_{week_end}.json",
            "last_scan": f"{STATE_REL}/last_scan.json",
            "candidates_md": f"{TEMP_REL}/{week_end}-candidates.md",
        },
    }
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    _write_text(workspace, f"{STATE_REL}/scan_{week_end}.json", text)
    _write_text(workspace, f"{STATE_REL}/last_scan.json", text)
    _write_text(workspace, f"{TEMP_REL}/{week_end}-candidates.md", render_candidates(result))
    if not args.json:
        print((workspace / TEMP_REL / f"{week_end}-candidates.md").read_text(encoding="utf-8"))
        return {"ok": True}
    slim = dict(result)
    slim["gainers"] = [_slim(item) for item in gainers]
    slim["losers"] = [_slim(item) for item in losers]
    return slim


def probe_screener() -> None:
    import requests

    response = requests.get(
        SCREENER_URL,
        params={"tableonly": "true", "limit": "5", "offset": "0", "download": "true"},
        headers=SCREENER_HEADERS,
        timeout=30,
    )
    rows = response.json().get("data", {}).get("rows") if response.status_code == 200 else None
    if not rows:
        raise RuntimeError(f"NASDAQ screener HTTP {response.status_code}, rows={0 if not rows else len(rows)}")


def probe_yfinance() -> None:
    import yfinance as yf

    frame = yf.download(["SPY"], period="1mo", auto_adjust=False, progress=False)
    if frame is None or frame.empty:
        raise RuntimeError("yfinance empty")


def self_test(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    checks: list[dict[str, Any]] = []

    def run_check(name: str, fn: Any) -> None:
        try:
            fn()
            checks.append({"name": name, "ok": True})
        except Exception as exc:  # noqa: BLE001 - a probe failure is recorded, not fatal
            checks.append({"name": name, "ok": False, "error": str(exc), "hint": error_hint(exc)})

    run_check("python-runtime", lambda: None)
    for module_name in ("requests", "pandas", "numpy", "yfinance"):
        run_check(f"import.{module_name}", lambda name=module_name: importlib.import_module(name))

    def writable() -> None:
        probe = _card_io().workspace_file(workspace, f"{STATE_REL}/.write-probe")
        _card_io().write_bytes_exclusive(probe, b"1")
        probe.unlink()

    run_check("workspace.state_dir_writable", writable)
    run_check("nasdaq.screener", probe_screener)
    run_check("yfinance.download", probe_yfinance)
    extra = os.environ.get("SKILL_SELFTEST_IMPORTS", "").strip()
    if extra:
        for module_name in [item.strip() for item in extra.split(",") if item.strip()]:
            run_check(f"import.{module_name}", lambda name=module_name: importlib.import_module(name))
    ok = all(item.get("ok") for item in checks)
    payload = {
        "ok": ok,
        "checks": checks,
        "meta": {"template": TEMPLATE_VERSION, "ts": datetime.now().isoformat(timespec="seconds")},
    }
    if not args.json:
        _log(f"Self-test: {'PASS' if ok else 'FAIL'}", args)
        for item in checks:
            print(f"  [{'PASS' if item['ok'] else 'FAIL'}] {item['name']}")
    return payload


def _validate_theme(theme: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(theme, dict):
        raise TypeError("theme must be an object")
    kept = {key: theme[key] for key in THEME_FIELDS if key in theme}
    week_end = str(kept.get("week_end") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", week_end):
        raise RuntimeError("theme week_end must be YYYY-MM-DD")
    if kept.get("side") not in {"gainers", "losers"}:
        raise RuntimeError("theme side must be gainers or losers")
    if not isinstance(kept.get("label"), str) or not kept["label"].strip():
        raise TypeError("theme label is empty")
    symbols = kept.get("symbols")
    if not isinstance(symbols, list) or len(symbols) < 3 or not all(isinstance(item, str) and item for item in symbols):
        raise TypeError("theme symbols need at least 3 tickers")
    if not isinstance(kept.get("catalyst_type"), str) or not kept["catalyst_type"].strip():
        raise TypeError("theme catalyst_type is empty")
    if "median_ret_week" in kept and not isinstance(kept["median_ret_week"], (int, float)):
        raise TypeError("theme median_ret_week must be a number")
    if "verdict" in kept and (not isinstance(kept["verdict"], str) or not kept["verdict"].strip()):
        raise TypeError("theme verdict is empty")
    return kept


def _theme_key(theme: dict[str, Any]) -> tuple[str, str, str]:
    return (str(theme.get("week_end")), str(theme.get("side")), str(theme.get("label")))


def _read_theme_lines(path: Path) -> tuple[list[str], set[tuple[str, str, str]]]:
    if path.is_symlink():
        raise RuntimeError("refusing symlink")
    if not path.is_file():
        return [], set()
    lines = []
    seen: set[tuple[str, str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        lines.append(line)
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            seen.add(_theme_key(parsed))
    return lines, seen


def cmd_append_themes(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    source = _card_io().workspace_file(workspace, args.file)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("themes input not found")
    payload = json.loads(source.read_text(encoding="utf-8"))
    themes = payload.get("themes") if isinstance(payload, dict) else payload
    if not isinstance(themes, list):
        raise TypeError("themes input must be a list or an object with themes")
    target = workspace / STATE_REL / "themes.jsonl"
    existing, seen = _read_theme_lines(target)
    added = 0
    skipped = 0
    fresh: list[str] = []
    for theme in themes:
        kept = _validate_theme(theme)
        key = _theme_key(kept)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        fresh.append(json.dumps(kept, ensure_ascii=False, sort_keys=True))
        added += 1
    if fresh:
        _write_text(workspace, f"{STATE_REL}/themes.jsonl", "\n".join([*existing, *fresh]) + "\n")
    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "path": f"{STATE_REL}/themes.jsonl",
    }


def cmd_recent_themes(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    path = workspace / STATE_REL / "themes.jsonl"
    lines, _seen = _read_theme_lines(path)
    parsed = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("week_end"):
            parsed.append(item)
    if not parsed:
        return {"ok": True, "week_end": None, "themes": []}
    latest = max(str(item["week_end"]) for item in parsed)
    return {
        "ok": True,
        "week_end": latest,
        "themes": [item for item in parsed if str(item["week_end"]) == latest],
    }


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    return _card_io().render_markdown(
        workspace=workspace,
        markdown_rel=args.file,
        output_rel=args.output,
        title=args.title,
    )


def _build_parser() -> argparse.ArgumentParser:
    # Separate action objects. parents= reuses one object, and set_defaults
    # would replace its SUPPRESS default; the subparser would then write None
    # over a --workspace-dir already parsed before the subcommand.
    parser = argparse.ArgumentParser(description="us-weekly-movers CLI")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", action="version", version=TEMPLATE_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("self-test", parents=[shared])
    check.set_defaults(func=self_test)

    universe = sub.add_parser("universe", parents=[shared])
    universe.add_argument("--min-cap", type=float, default=1e9)
    universe.set_defaults(func=cmd_universe)

    scan = sub.add_parser("scan", parents=[shared])
    scan.add_argument("--min-cap", type=float, default=1e9)
    scan.add_argument("--top-n", type=int, default=40)
    scan.add_argument("--min-vol-ratio", type=float, default=0.0)
    scan.add_argument("--min-move", type=float, default=0.05)
    scan.add_argument("--cluster-min", type=int, default=3)
    scan.add_argument("--universe-file", default=None)
    scan.add_argument("--limit", type=int, default=0)
    scan.add_argument("--no-earnings", action="store_true")
    scan.set_defaults(func=cmd_scan)

    render = sub.add_parser("render-card", parents=[shared])
    render.add_argument("--file", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--title", required=True)
    render.set_defaults(func=cmd_render)

    themes = sub.add_parser("append-themes", parents=[shared])
    themes.add_argument("--file", required=True)
    themes.set_defaults(func=cmd_append_themes)

    recent = sub.add_parser("recent-themes", parents=[shared])
    recent.set_defaults(func=cmd_recent_themes)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        payload = {
            "ok": False,
            "error": str(exc),
            "hint": error_hint(exc),
            "meta": {"template": TEMPLATE_VERSION, "ts": datetime.now().isoformat(timespec="seconds")},
        }
        if getattr(args, "json", False):
            emit_json(payload)
        else:
            print(f"Unhandled error: {payload['error']}")
            print(f"Hint: {payload['hint']}")
        return 1
    if getattr(args, "json", False) and isinstance(result, dict):
        emit_json(result)
    return 0 if not isinstance(result, dict) or result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
