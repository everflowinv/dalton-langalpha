#!/usr/bin/env python3
"""
price_reaction.py — Post-earnings stock price reaction & true 52-week range calculator (PS.9).

Computes:
  1. Post-market (AMC) or Pre-market (BMO) move + next-session opening gap;
  2. Next-trading-day close-to-close move (absolute & relative to QQQ, SPY, and sector ETF);
  3. 1-week (5 trading days) close-to-close move (absolute & relative to QQQ, SPY, and sector ETF);
  4. True 252-trading-day 52-week high/low and current position (fixes the legacy bug where 6-month
     history was mistakenly labeled as 52-week high/low).

Data sources:
  - `--prices-file <json>` : Optional local/offline price series for deterministic testing or sandboxed runs.
  - `yfinance`             : Default live market data provider (`period="2y"`, `auto_adjust=False` for unadjusted levels/52W range).

Usage:
  python3 price_reaction.py --ticker META --release-date 2026-10-28 --timing AMC --benchmarks QQQ,SPY --sector-etf XLC --markdown
  python3 price_reaction.py --ticker NVDA --release-date 2026-08-26 --timing AMC --sector-etf SOXX --post-market-pct -2.1 --json
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path


TRADING_DAYS_52W = 252

_cn_price_fallback_mod = None


def _cn_price_fallback():
    """兄弟技能 dalton-stock-move 的 cn_price_fallback（港股/A 股日线坏数据
    识别与 cn-hk 备用源）；两个技能同在 plugins/dalton/skills 下，按路径加载
    与 move_quant 引用 earnings-calendar 的方式一致。"""
    global _cn_price_fallback_mod
    if _cn_price_fallback_mod is None:
        import importlib.util

        path = (
            Path(__file__).resolve().parents[2]
            / "dalton-stock-move"
            / "scripts"
            / "cn_price_fallback.py"
        )
        spec = importlib.util.spec_from_file_location("cn_price_fallback", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _cn_price_fallback_mod = mod
    return _cn_price_fallback_mod


def _pct(new_val: float | None, base_val: float | None) -> float | None:
    if new_val is None or base_val is None or base_val == 0:
        return None
    return round((new_val / base_val - 1.0) * 100.0, 2)


def _diff_pct(stock_pct: float | None, bench_pct: float | None) -> float | None:
    if stock_pct is None or bench_pct is None:
        return None
    return round(stock_pct - bench_pct, 2)


def _normalize_bars(raw_bars: list[dict]) -> list[dict]:
    """Return sorted list of {'date': 'YYYY-MM-DD', 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float|None}."""
    out: list[dict] = []
    for b in raw_bars:
        d_str = str(b.get("date") or b.get("Date") or "")[:10]
        if not d_str:
            continue
        close_v = float(b.get("close") if b.get("close") is not None else b.get("Close"))
        open_v = float(b.get("open") if b.get("open") is not None else b.get("Open", close_v))
        high_v = float(b.get("high") if b.get("high") is not None else b.get("High", max(open_v, close_v)))
        low_v = float(b.get("low") if b.get("low") is not None else b.get("Low", min(open_v, close_v)))
        raw_vol = b.get("volume", b.get("Volume"))
        try:
            vol_v = float(raw_vol) if raw_vol is not None else None
        except (TypeError, ValueError):
            vol_v = None
        out.append({"date": d_str, "open": open_v, "high": high_v, "low": low_v, "close": close_v, "volume": vol_v})
    out.sort(key=lambda x: x["date"])
    return out


def fetch_series_yfinance(symbol: str) -> tuple[list[dict], dict]:
    """Fetch 2 years of daily OHLC bars via yfinance to guarantee >= 252 trading days for 52W high/low."""
    import yfinance as yf  # type: ignore

    t = yf.Ticker(symbol)
    hist = t.history(period="2y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance returned empty history for {symbol}")

    bars: list[dict] = []
    for idx, row in hist.iterrows():
        d_str = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
        raw_vol = row["Volume"] if "Volume" in hist.columns else None
        try:
            vol_v = float(raw_vol) if raw_vol is not None else None
        except (TypeError, ValueError):
            vol_v = None
        bars.append(
            {
                "date": d_str,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": vol_v,
            }
        )

    ext_quote: dict = {}
    try:
        info = t.info or {}
        if info.get("postMarketPrice"):
            ext_quote["post_market_price"] = float(info["postMarketPrice"])
        if info.get("preMarketPrice"):
            ext_quote["pre_market_price"] = float(info["preMarketPrice"])
    except Exception:
        pass
    return _normalize_bars(bars), ext_quote


def load_series(
    symbols: list[str],
    prices_file: str | None = None,
    as_of: str | None = None,
) -> tuple[dict[str, list[dict]], dict, dict[str, str], list[str], dict[str, dict]]:
    """Returns (series_map, ext_meta, price_sources, source_notes, cn_flags).

    港股/A 股日线坏数据按请求截止日（as_of，缺省今天）判定并只做非破坏性
    标注：bar 原样保留（不删、不合成），判定（stagnant_from / valid_until /
    note 等）放进 cn_flags 供 analyze_reaction 把落在停滞段里的反应窗口标
    不完整；price_sources/source_notes 注明用了哪个源、为什么。as_of 早于
    今天时是历史回测，不调备用源。美股不受影响。
    """
    if prices_file:
        raw = json.loads(Path(prices_file).read_text(encoding="utf-8"))
        series_map: dict[str, list[dict]] = {}
        ext_meta = raw.get("_extended_quotes", {}) if isinstance(raw, dict) else {}
        if isinstance(raw, dict):
            for sym in symbols:
                bars = raw.get(sym) or raw.get(sym.upper()) or raw.get(sym.lower())
                if isinstance(bars, list):
                    series_map[sym.upper()] = _normalize_bars(bars)
        elif isinstance(raw, list) and symbols:
            series_map[symbols[0].upper()] = _normalize_bars(raw)
        return series_map, ext_meta, {}, [], {}

    guard = _cn_price_fallback()
    series_map = {}
    ext_meta = {}
    price_sources: dict[str, str] = {}
    source_notes: list[str] = []
    cn_flags: dict[str, dict] = {}
    end = date.fromisoformat(as_of) if as_of else None
    for sym in symbols:
        s_up = sym.upper()
        bars, ext = fetch_series_yfinance(s_up)
        if guard.is_cn_symbol(s_up):
            verdict = guard.evaluate_cn_daily(
                s_up,
                [b["date"] for b in bars],
                [b["close"] for b in bars],
                [b.get("volume") for b in bars],
                end=end,
            )
            if verdict is not None:
                source_notes.append(verdict["note"])
                if verdict.get("price_source"):
                    price_sources[s_up] = verdict["price_source"]
                cn_flags[s_up] = verdict
        series_map[s_up] = bars
        if ext:
            ext_meta[s_up] = ext
    return series_map, ext_meta, price_sources, source_notes, cn_flags


def resolve_event_indices(bars: list[dict], release_date_str: str, timing: str) -> tuple[int, int]:
    """
    Determine (base_idx, reaction_idx) in `bars`:
      - AMC (After Market Close): base close is on release_date (or latest session <= release_date);
        reaction session is the next trading day (base_idx + 1).
      - BMO (Before Market Open): reaction session is the first session >= release_date;
        base close is the prior trading session (reaction_idx - 1).
      - unknown: defaults to AMC convention.
    """
    if not bars:
        raise ValueError("Empty price series")
    timing_u = (timing or "AMC").upper()
    dates = [b["date"] for b in bars]

    if timing_u == "BMO":
        react_candidates = [i for i, d in enumerate(dates) if d >= release_date_str]
        if not react_candidates:
            raise ValueError(f"No trading session found on or after release_date {release_date_str}")
        reaction_idx = react_candidates[0]
        base_idx = reaction_idx - 1
        if base_idx < 0:
            raise ValueError(f"No prior trading session before {dates[reaction_idx]} for BMO baseline")
        return base_idx, reaction_idx
    else:
        # AMC or unknown
        base_candidates = [i for i, d in enumerate(dates) if d <= release_date_str]
        if not base_candidates:
            raise ValueError(f"No trading session found on or before release_date {release_date_str}")
        base_idx = base_candidates[-1]
        reaction_idx = base_idx + 1
        return base_idx, reaction_idx


def compute_window_move(bars: list[dict], base_idx: int, target_idx: int) -> dict:
    base_bar = bars[base_idx]
    if target_idx >= len(bars):
        # Partial window up to latest available bar
        last_bar = bars[-1]
        elapsed = len(bars) - 1 - base_idx
        return {
            "complete": False,
            "elapsed_trading_days": max(elapsed, 0),
            "base_date": base_bar["date"],
            "base_close": round(base_bar["close"], 4),
            "end_date": last_bar["date"],
            "end_close": round(last_bar["close"], 4),
            "change_pct": _pct(last_bar["close"], base_bar["close"]),
        }
    end_bar = bars[target_idx]
    return {
        "complete": True,
        "elapsed_trading_days": target_idx - base_idx,
        "base_date": base_bar["date"],
        "base_close": round(base_bar["close"], 4),
        "end_date": end_bar["date"],
        "end_close": round(end_bar["close"], 4),
        "change_pct": _pct(end_bar["close"], base_bar["close"]),
    }


def compute_true_52w(bars: list[dict], as_of_date: str | None = None) -> dict:
    """Compute true 52-week (252 trading days) high and low up to as_of_date."""
    filtered = [b for b in bars if not as_of_date or b["date"] <= as_of_date]
    if not filtered:
        raise ValueError("No bars available for 52-week calculation")
    window = filtered[-TRADING_DAYS_52W:]
    high_bar = max(window, key=lambda b: b["high"])
    low_bar = min(window, key=lambda b: b["low"])
    last_bar = window[-1]
    last_close = last_bar["close"]
    h52 = high_bar["high"]
    l52 = low_bar["low"]
    span = h52 - l52
    percentile = round(((last_close - l52) / span) * 100.0, 1) if span > 0 else None

    return {
        "window_trading_days": len(window),
        "is_full_252_days": len(window) >= TRADING_DAYS_52W,
        "window_start_date": window[0]["date"],
        "as_of_date": last_bar["date"],
        "last_close": round(last_close, 4),
        "high_52w": round(h52, 4),
        "high_52w_date": high_bar["date"],
        "low_52w": round(l52, 4),
        "low_52w_date": low_bar["date"],
        "pct_from_52w_high": _pct(last_close, h52),
        "pct_from_52w_low": _pct(last_close, l52),
        "range_52w_percentile": percentile,
    }


def _stagnant_window(reason: str) -> dict:
    """反应窗口落在停滞段时的占位：不完整、不计算涨跌，也不把"最后有效
    收盘 → 今日备用报价"的落差当成 T+1。"""
    return {
        "complete": False,
        "elapsed_trading_days": None,
        "base_date": None,
        "base_close": None,
        "end_date": None,
        "end_close": None,
        "change_pct": None,
        "incomplete_reason": reason,
    }


def analyze_reaction(
    ticker: str,
    release_date: str,
    timing: str,
    benchmarks: list[str],
    sector_etf: str | None,
    series_map: dict[str, list[dict]],
    ext_meta: dict,
    post_market_price: float | None = None,
    post_market_pct: float | None = None,
    as_of: str | None = None,
    daily_flags: dict[str, dict] | None = None,
) -> dict:
    t_sym = ticker.upper()
    stock_bars = series_map.get(t_sym)
    if not stock_bars:
        raise ValueError(f"Missing price series for ticker {t_sym}")

    if as_of:
        stock_bars = [b for b in stock_bars if b["date"] <= as_of]

    base_idx, react_idx = resolve_event_indices(stock_bars, release_date, timing)
    base_bar = stock_bars[base_idx]
    base_close = base_bar["close"]

    # 港股/A 股日线停滞段或疑似停牌段（cn_price_fallback 判定）：发布日或
    # T+1/T+5 落在停滞/停牌段里时，窗口标不完整、不计算涨跌。
    guard = _cn_price_fallback()

    def _resolve_symbol_flags(sym: str, bars: list[dict]) -> dict:
        explicit = (daily_flags or {}).get(sym)
        if explicit:
            return explicit
        if guard.is_cn_symbol(sym):
            stagnant = guard.find_stagnant_tail(
                [b["date"] for b in bars],
                [b["close"] for b in bars],
                [b.get("volume") for b in bars],
            )
            if stagnant is not None:
                valid_idx = len(bars) - stagnant["bars"] - 1
                return {
                    "kind": "stagnant",
                    "stagnant_from": stagnant["from_date"],
                    "valid_until": bars[valid_idx]["date"] if valid_idx >= 0 else None,
                }
        return {}

    def _window_stale_flags(
        bars: list[dict], b_idx: int, r_idx: int, sym_flags: dict, label_prefix: str = ""
    ) -> tuple[bool, bool, str | None]:
        s_from = sym_flags.get("stagnant_from")
        if not s_from:
            return False, False, None
        s_first = next(
            (i for i, b in enumerate(bars) if b["date"] >= s_from), None
        )
        if s_first is None:
            return False, False, None
        nd_stale = r_idx >= s_first or b_idx >= s_first
        ow_end = min(r_idx + 4, len(bars) - 1)
        ow_stale = nd_stale or ow_end >= s_first
        if not (nd_stale or ow_stale):
            return False, False, None
        v_until = sym_flags.get("valid_until") or s_from
        if sym_flags.get("kind") == "suspended":
            reason = (
                f"{label_prefix}反应窗口落在疑似停牌段（{s_from} 起，成交量为 0、价格不变，"
                f"截至 {v_until}（停牌前）），不计算涨跌"
            )
        else:
            reason = (
                f"{label_prefix}反应窗口落在停滞段（{s_from} 起，行情停在同一价、量为 0，"
                f"截至 {v_until}），不计算涨跌，也不把最后有效收盘到备用报价的落差当成 T+1"
            )
        return nd_stale, ow_stale, reason

    flags = _resolve_symbol_flags(t_sym, stock_bars)
    stagnant_from = flags.get("stagnant_from")
    next_day_stale, one_week_stale, stale_reason = _window_stale_flags(
        stock_bars, base_idx, react_idx, flags
    )

    # 1. Post-market / Pre-market + Next-day Opening Gap
    opening_gap_pct = None
    reaction_open = None
    if react_idx < len(stock_bars) and not next_day_stale:
        reaction_open = round(stock_bars[react_idx]["open"], 4)
        opening_gap_pct = _pct(reaction_open, base_close)

    ext_info = ext_meta.get(t_sym, {})
    pm_price = post_market_price or ext_info.get("post_market_price") or ext_info.get("pre_market_price")
    if post_market_pct is not None:
        pm_pct = round(float(post_market_pct), 2)
        if pm_price is None:
            pm_price = round(base_close * (1.0 + pm_pct / 100.0), 4)
    elif pm_price is not None:
        pm_pct = _pct(float(pm_price), base_close)
    else:
        pm_pct = None

    post_market_block = {
        "timing": timing.upper(),
        "base_date": base_bar["date"],
        "base_close": round(base_close, 4),
        "extended_hours_price": pm_price,
        "extended_hours_change_pct": pm_pct,
        "reaction_session_open": reaction_open,
        "reaction_open_gap_pct": opening_gap_pct,
        "effective_initial_move_pct": pm_pct if pm_pct is not None else opening_gap_pct,
        "effective_basis": "extended_hours_quote" if pm_pct is not None else (
            "reaction_session_open_gap" if opening_gap_pct is not None else "unavailable"
        ),
    }

    # 2. Next-day (T+1 session) and 1-Week (5 trading sessions from base)
    next_day = (
        _stagnant_window(stale_reason)
        if next_day_stale
        else compute_window_move(stock_bars, base_idx, react_idx)
    )
    one_week = (
        _stagnant_window(stale_reason)
        if one_week_stale
        else compute_window_move(stock_bars, base_idx, react_idx + 4)
    )

    all_benches = list(dict.fromkeys([b.upper() for b in benchmarks if b] + ([sector_etf.upper()] if sector_etf else [])))
    relative_next_day: dict[str, dict] = {}
    relative_one_week: dict[str, dict] = {}

    for b_sym in all_benches:
        b_bars = series_map.get(b_sym)
        if not b_bars:
            continue
        if as_of:
            b_bars = [b for b in b_bars if b["date"] <= as_of]
        try:
            b_base_idx, b_react_idx = resolve_event_indices(b_bars, release_date, timing)
            b_flags = _resolve_symbol_flags(b_sym, b_bars)
            b_nd_stale, b_ow_stale, b_stale_reason = _window_stale_flags(
                b_bars, b_base_idx, b_react_idx, b_flags, label_prefix=f"基准 {b_sym} "
            )
            if b_nd_stale:
                relative_next_day[b_sym] = {
                    "benchmark_change_pct": None,
                    "relative_pct": None,
                    "incomplete_reason": b_stale_reason,
                    "note": b_stale_reason,
                }
            else:
                b_next = compute_window_move(b_bars, b_base_idx, b_react_idx)
                relative_next_day[b_sym] = {
                    "benchmark_change_pct": b_next["change_pct"],
                    "relative_pct": _diff_pct(next_day["change_pct"], b_next["change_pct"]),
                }
            if b_ow_stale:
                relative_one_week[b_sym] = {
                    "benchmark_change_pct": None,
                    "relative_pct": None,
                    "incomplete_reason": b_stale_reason,
                    "note": b_stale_reason,
                }
            else:
                b_week = compute_window_move(b_bars, b_base_idx, b_react_idx + 4)
                relative_one_week[b_sym] = {
                    "benchmark_change_pct": b_week["change_pct"],
                    "relative_pct": _diff_pct(one_week["change_pct"], b_week["change_pct"]),
                }
        except Exception as exc:
            relative_next_day[b_sym] = {"error": str(exc)}
            relative_one_week[b_sym] = {"error": str(exc)}

    # 3. True 252-trading-day 52-week high/low — 有停滞/停牌段时只用其之前的
    # 有效数据算（输出注明"截至某日"或"截至某日（停牌前）"），不再标"真实 52 周"。
    range_52w: dict
    if stagnant_from:
        valid_until = flags.get("valid_until")
        range_bars = (
            [b for b in stock_bars if b["date"] <= valid_until] if valid_until else []
        )
        try:
            range_52w = compute_true_52w(range_bars, as_of_date=as_of)
        except ValueError:
            range_52w = {"error": "停滞/停牌段之前无有效日线，不计算 52 周区间"}
        else:
            range_52w["stagnant_truncated"] = True
            if flags.get("kind") == "suspended":
                range_52w["suspended_truncated"] = True
    else:
        range_52w = compute_true_52w(stock_bars, as_of_date=as_of)

    return {
        "ok": True,
        "ticker": t_sym,
        "release_date": release_date,
        "timing": timing.upper(),
        "sector_etf": sector_etf.upper() if sector_etf else None,
        "benchmarks": all_benches,
        "post_market": post_market_block,
        "next_day": {**next_day, "relative": relative_next_day},
        "one_week": {**one_week, "relative": relative_one_week},
        "range_52w": range_52w,
        **({"today_quote": {"ticker": t_sym, **flags["today"]}} if flags.get("today") else {}),
    }


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "未取得"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}%"


def render_markdown(res: dict) -> str:
    pm = res["post_market"]
    nd = res["next_day"]
    ow = res["one_week"]
    r52 = res["range_52w"]
    benches = res["benchmarks"]

    pm_label = "盘后（AMC）" if res["timing"] == "AMC" else ("盘前（BMO）" if res["timing"] == "BMO" else "盘后/初段")
    if pm["extended_hours_change_pct"] is not None:
        pm_str = f"{_fmt_pct(pm['extended_hours_change_pct'])}（盘后/盘前价 {pm['extended_hours_price']} vs 基准收盘 {pm['base_close']}；次日开盘跳空 {_fmt_pct(pm['reaction_open_gap_pct'])}）"
    elif pm["reaction_open_gap_pct"] is not None:
        pm_str = f"开盘跳空 {_fmt_pct(pm['reaction_open_gap_pct'])}（反应日开盘 {pm['reaction_session_open']} vs 基准收盘 {pm['base_close']}，盘后即时报价未单独归档）"
    else:
        pm_str = "尚未开盘 / 报价未取得"

    rel_headers = " | ".join(f"相对 {b}" for b in benches)
    rel_sep = " | ".join("---" for _ in benches)
    nd_rels = " | ".join(_fmt_pct((nd["relative"].get(b) or {}).get("relative_pct")) for b in benches)
    ow_rels = " | ".join(_fmt_pct((ow["relative"].get(b) or {}).get("relative_pct")) for b in benches)

    if ow.get("incomplete_reason"):
        ow_window_note = "一周（不完整）"
    elif ow["complete"]:
        ow_window_note = "一周（5 交易日）"
    else:
        ow_window_note = f"截至最新（{ow['elapsed_trading_days']} 交易日）"

    def _cell(val: object) -> str:
        return "—" if val is None else str(val)

    if r52.get("error"):
        range_line = f"- **52 周高低位置**：{r52['error']}"
    else:
        days_note = f"{r52['window_trading_days']} 交易日" + ("" if r52["is_full_252_days"] else "，样本不足 252 交易日")
        if r52.get("suspended_truncated"):
            range_header = f"52 周高低位置（截至 {r52['as_of_date']}（停牌前），疑似停牌段不计入）"
        elif r52.get("stagnant_truncated"):
            range_header = f"52 周高低位置（截至 {r52['as_of_date']}，停滞段不计入）"
        else:
            range_header = f"真实 52 周高低位置（{days_note}，{r52['window_start_date']} 至 {r52['as_of_date']}）"
        range_line = (
            f"- **{range_header}**："
            f"最新收盘 **{r52['last_close']}**｜52 周最高 **{r52['high_52w']}**（{r52['high_52w_date']}，距高点 {_fmt_pct(r52['pct_from_52w_high'])}）｜"
            f"52 周最低 **{r52['low_52w']}**（{r52['low_52w_date']}，距低点 {_fmt_pct(r52['pct_from_52w_low'])}）｜"
            f"所处 52 周区间分位 **{r52['range_52w_percentile']}%**"
        )

    lines = [
        f"#### {res['ticker']} 财报后股价表现（发布日：{res['release_date']} {res['timing']}）",
        "",
        f"- **{pm_label}即时反应**：{pm_str}",
        range_line,
        "",
        f"| 窗口 | 基准日（收盘） | 结束日（收盘） | 绝对涨跌 | {rel_headers} |",
        f"|---|---|---|---|{rel_sep}|",
        f"| **次日（T+1）** | {_cell(nd['base_date'])} ({_cell(nd['base_close'])}) | {_cell(nd['end_date'])} ({_cell(nd['end_close'])}) | **{_fmt_pct(nd['change_pct'])}** | {nd_rels} |",
        f"| **{ow_window_note}** | {_cell(ow['base_date'])} ({_cell(ow['base_close'])}) | {_cell(ow['end_date'])} ({_cell(ow['end_close'])}) | **{_fmt_pct(ow['change_pct'])}** | {ow_rels} |",
    ]
    stale_reasons: list[str] = []
    for r_str in (nd.get("incomplete_reason"), ow.get("incomplete_reason")):
        if r_str and r_str not in stale_reasons:
            stale_reasons.append(r_str)
    for rel_map in (nd.get("relative") or {}, ow.get("relative") or {}):
        for b_info in rel_map.values():
            if isinstance(b_info, dict):
                b_reason = b_info.get("incomplete_reason")
                if b_reason and b_reason not in stale_reasons:
                    stale_reasons.append(b_reason)
    if stale_reasons:
        lines.append("")
        for sr in stale_reasons:
            lines.append(f"> ⚠️ 次日/一周窗口不完整：{sr}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compute post-market, next-day, and 1-week price reaction (abs & rel to QQQ/SPY/sector ETF) + true 252-day 52W range (PS.9)."
    )
    p.add_argument("--ticker", "-t", required=True, help="Stock ticker (e.g. META, NVDA)")
    p.add_argument("--release-date", "-d", required=True, help="Earnings release date YYYY-MM-DD")
    p.add_argument("--timing", default="AMC", choices=["AMC", "BMO", "unknown", "amc", "bmo"], help="Release timing (default: AMC)")
    p.add_argument("--benchmarks", default="QQQ,SPY", help="Comma-separated benchmark ETFs (default: QQQ,SPY)")
    p.add_argument("--sector-etf", default=None, help="Sector ETF ticker (e.g. SOXX, SMH, IGV, XLC, XLY)")
    p.add_argument("--post-market-price", type=float, default=None, help="Optional explicit post/pre-market price")
    p.add_argument("--post-market-pct", type=float, default=None, help="Optional explicit post/pre-market move in percent")
    p.add_argument("--as-of", default=None, help="Optional cutoff date YYYY-MM-DD for historical backtests")
    p.add_argument("--prices-file", default=None, help="Optional local JSON file with daily OHLC bars for offline/test execution")
    p.add_argument("--markdown", "--md", action="store_true", help="Output Markdown summary and table")
    p.add_argument("--json", action="store_true", help="Output JSON (default if --markdown not set)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    benchmarks = [b.strip().upper() for b in args.benchmarks.split(",") if b.strip()]
    all_symbols = list(
        dict.fromkeys([args.ticker.upper()] + benchmarks + ([args.sector_etf.upper()] if args.sector_etf else []))
    )

    try:
        series_map, ext_meta, price_sources, source_notes, cn_flags = load_series(
            all_symbols, prices_file=args.prices_file, as_of=args.as_of
        )
        res = analyze_reaction(
            ticker=args.ticker,
            release_date=args.release_date,
            timing=args.timing,
            benchmarks=benchmarks,
            sector_etf=args.sector_etf,
            series_map=series_map,
            ext_meta=ext_meta,
            post_market_price=args.post_market_price,
            post_market_pct=args.post_market_pct,
            as_of=args.as_of,
            daily_flags=cn_flags,
        )
        if price_sources:
            res["price_sources"] = price_sources
        if source_notes:
            res["price_source_notes"] = source_notes
        if args.markdown and not args.json:
            md = render_markdown(res)
            if source_notes:
                md = "\n".join([f"> ⚠️ {n}" for n in source_notes]) + "\n\n" + md
            print(md, end="")
        else:
            res["markdown"] = render_markdown(res)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
