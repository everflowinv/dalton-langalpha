#!/usr/bin/env python3
"""
attribution.py — EPS 预期修正 vs 估值倍数归因脚本（PS.13 新设计）

核心公式（PS.13 lines 1612–1618）：
1. 对数分解：
     ln(P_end / P_base) = ln(EPS_end / EPS_base) + ln(PE_end / PE_base)
   其中 EPS 为远期 12 个月一致预期（NTM EPS），PE = P / NTM_EPS。
2. NTM EPS 时间加权插值：
     NTM_EPS = w * EPS_0y + (1 - w) * EPS_+1y
     w = 本财年剩余天数 / 365
   窗口起点的 EPS 优先取工作区 data/consensus/ 快照历史里离 base 最近的一份；
   没有快照时用一致预期接口的 7daysAgo 值，并标注“近似（7daysAgo）”。
3. 主导驱动判定（driver）：
   - 两项贡献同号时，绝对值大的为主导：占比 >= 70% 写“盈利驱动”或“估值驱动”，否则写“两者”；
   - 两项贡献异号时，写“相抵”（详情“盈利 Xpp、倍数 Ypp 相抵”）。
4. 负 EPS 或 EPS 接近 0（<= 0.05）的公司不做分解，写“NM，改用 EV/Sales 定性”。
5. 币种契约（对齐 researchdata 1.5）：EPS 提取保留一致预期币种，价格带行情币种
   （拿不到按交易所推断并标注）；两者相同才直接算 P/E，不同则用快照/行情汇率
   换算并注明汇率与日期，拿不到汇率写“币种不一致，未计算”。跨期归因要求两端
   EPS 币种一致，不一致时不计算并说明。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

EPS_NEAR_ZERO_THRESHOLD = 0.05
DOMINANCE_SHARE_THRESHOLD = 0.70


def _norm_currency(value: Any) -> str | None:
    """币种规范化：缺失/非字符串返回 None（未知），其余去空格大写。"""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def infer_price_currency(ticker: str) -> str:
    """按交易所后缀推断价格币种：`.HK`→HKD，沪深 A 股（.SS/.SZ）→CNY，其余→USD。"""
    t = (ticker or "").upper()
    if t.endswith(".HK"):
        return "HKD"
    if t.endswith((".SS", ".SZ")):
        return "CNY"
    return "USD"


def get_price_currency(ticker: str) -> tuple[str, str]:
    """价格币种：(currency, source)。行情接口返回的 currency 优先；拿不到按交易所推断（source='推断'）。"""
    try:
        import yfinance as yf  # type: ignore

        cur = _norm_currency(yf.Ticker(ticker).fast_info["currency"])
        if cur:
            return cur, "quote"
    except Exception:
        pass
    return infer_price_currency(ticker), "推断"


def _parse_as_date(value: Any) -> str | None:
    """把 as_of 解析成 YYYY-MM-DD：接受日期或带时间的 ISO 时间戳（按其日期部分比较）；
    非法日期（如 2026-02-30）返回 None，视为无日期、不用于换算。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _all_fx_records(fx: Any) -> list[dict[str, Any]]:
    """任意形状的快照 fx 规范化为 [{"pair","rate","as_of"}] 记录（pair 记法 A/B 表示 1 A = rate B）。

    只保留日期可解析的记录；接受原始快照形状（顶层 {"pair","rate","as_of"}、
    {"CNY/USD": {"rate":..,"as_of":..}}、{"CNY/USD": {"2026-10-01": 0.14, ...}}）
    以及 `_merge_snapshot_fx` 合并出的记录列表。
    """
    records: list[dict[str, Any]] = []

    def add(pair: Any, rate: Any, as_of: Any) -> None:
        if not isinstance(rate, (int, float)) or isinstance(rate, bool):
            return
        p = str(pair or "").replace(" ", "").upper()
        day = _parse_as_date(as_of)
        if not p or "/" not in p or day is None:
            return
        records.append({"pair": p, "rate": float(rate), "as_of": day})

    if isinstance(fx, list):
        for item in fx:
            if isinstance(item, dict):
                add(item.get("pair"), item.get("rate"), item.get("as_of"))
        return records
    if not isinstance(fx, dict):
        return records
    add(fx.get("pair"), fx.get("rate"), fx.get("as_of"))
    for key, val in fx.items():
        if not isinstance(val, dict):
            continue
        if val.get("rate") is not None or val.get("as_of") is not None:
            add(key, val.get("rate"), val.get("as_of"))
        else:
            for day, rv in val.items():
                add(key, rv, day)
    return records


def fx_from_snapshot(snapshot_fx: Any, from_cur: str, to_cur: str, on_date: str) -> dict[str, Any] | None:
    """快照带日期的汇率里取 on_date 当日（或之前最近一条，日期按解析后的值比较）；
    无日期/非法日期记录不可靠，不用于换算。"""
    fwd, inv = f"{from_cur}/{to_cur}", f"{to_cur}/{from_cur}"
    usable: list[tuple[float, str]] = []
    for rec in _all_fx_records(snapshot_fx):
        rate = rec["rate"]
        if rec["pair"] == fwd:
            hit_rate = rate
        elif rec["pair"] == inv and rate != 0:
            hit_rate = 1.0 / rate
        else:
            continue
        if rec["as_of"] <= on_date[:10]:
            usable.append((hit_rate, rec["as_of"]))
    if not usable:
        return None
    rate, as_of = max(usable, key=lambda item: item[1])
    return {"rate": rate, "as_of": as_of, "source": "snapshot"}


def fx_from_quote(from_cur: str, to_cur: str, on_date: str) -> dict[str, Any] | None:
    """行情接口按 on_date 当日（或之前最近一个交易日）取货币对历史收盘，并记录该 K 线的实际日期。

    历史里取不到时不用 `fast_info` 最新价顶替——它没有可靠日期，不能冒充按日汇率。
    """
    if not from_cur or not to_cur or from_cur == to_cur:
        return None
    import yfinance as yf  # type: ignore

    target = date.fromisoformat(on_date[:10])
    start = (target - timedelta(days=10)).isoformat()
    end = (target + timedelta(days=1)).isoformat()

    def last_on(pair: str) -> dict[str, Any] | None:
        try:
            hist = yf.Ticker(pair).history(start=start, end=end)
            if hist is None or getattr(hist, "empty", True):
                return None
            close = hist["Close"].dropna()
            close = close[[d <= target for d in close.index.date]]
            if len(close) == 0:
                return None
            return {
                "rate": float(close.iloc[-1]),
                "as_of": str(close.index[-1].date()),
                "source": f"yfinance {pair}",
            }
        except Exception:
            return None

    hit = last_on(f"{from_cur}{to_cur}=X")
    if hit:
        return hit
    hit = last_on(f"{to_cur}{from_cur}=X")
    if hit and hit["rate"] != 0:
        return {"rate": 1.0 / hit["rate"], "as_of": hit["as_of"], "source": hit["source"]}
    return None


def resolve_fx_rate(
    from_cur: str | None,
    to_cur: str | None,
    on_date: str,
    snapshot_fx: Any = None,
) -> dict[str, Any] | None:
    """把 EPS 币种换算到价格币种、按 on_date 当日（或之前最近）的汇率：
    先快照带日期记录，再行情历史收盘；拿不到返回 None（不硬算）。"""
    if not from_cur or not to_cur or from_cur == to_cur:
        return None
    return fx_from_snapshot(snapshot_fx, from_cur, to_cur, on_date) or fx_from_quote(from_cur, to_cur, on_date)


def remaining_fiscal_year_weight(ref_date: date, fiscal_year_end_mmdd: str = "12-31") -> float:
    """
    计算参考日期 ref_date 距离本财年结束日的剩余天数占 365 天的权重 w = 本财年剩余天数 / 365。
    """
    parts = [int(x) for x in fiscal_year_end_mmdd.strip().split("-")]
    m, d = parts[0], parts[1]
    try:
        fye = date(ref_date.year, m, d)
    except ValueError:
        fye = date(ref_date.year, m, 28)
    if fye < ref_date:
        try:
            fye = date(ref_date.year + 1, m, d)
        except ValueError:
            fye = date(ref_date.year + 1, m, 28)
    rem_days = (fye - ref_date).days
    w = rem_days / 365.0
    return max(0.0, min(1.0, w))


def compute_ntm_eps(
    eps_0y: float | None,
    eps_1y: float | None,
    ref_date: date | str,
    fiscal_year_end: str = "12-31",
) -> tuple[float | None, float]:
    """
    按 w * EPS_0y + (1 - w) * EPS_+1y 插值计算 NTM EPS。
    返回 (ntm_eps, w)。
    """
    d = date.fromisoformat(ref_date[:10]) if isinstance(ref_date, str) else ref_date
    w = remaining_fiscal_year_weight(d, fiscal_year_end)
    if eps_0y is None and eps_1y is None:
        return None, round(w, 4)
    if eps_0y is not None and eps_1y is not None:
        return float(w * float(eps_0y) + (1.0 - w) * float(eps_1y)), round(w, 4)
    # 若仅有一个财年有值则退化使用该值
    val = float(eps_0y) if eps_0y is not None else float(eps_1y)  # type: ignore[arg-type]
    return val, round(w, 4)


def classify_driver(eps_contrib_pp: float, pe_contrib_pp: float) -> tuple[str, str]:
    """
    主导驱动判定（PS.13 line 1616）：
    - 两项贡献同号时，绝对值大的为主导，占比 >= 70% 写“盈利驱动”或“估值驱动”，否则写“两者”；
    - 异号时写“相抵”（附“盈利 X、倍数 Y 相抵”）。
    返回 (driver, driver_display)
    """
    abs_e = abs(eps_contrib_pp)
    abs_m = abs(pe_contrib_pp)
    total_abs = abs_e + abs_m
    if total_abs < 1e-9:
        return "两者", "无明显变动"

    if eps_contrib_pp * pe_contrib_pp >= -1e-9:
        e_share = abs_e / total_abs
        m_share = abs_m / total_abs
        if e_share >= DOMINANCE_SHARE_THRESHOLD:
            return "盈利驱动", "盈利驱动"
        if m_share >= DOMINANCE_SHARE_THRESHOLD:
            return "估值驱动", "估值驱动"
        return "两者", "两者"
    else:
        detail = f"盈利 {eps_contrib_pp:+.1f}pp、倍数 {pe_contrib_pp:+.1f}pp 相抵"
        return "相抵", detail


def _end_currency_state(
    eps_0y: float | None,
    eps_1y: float | None,
    curs: Any,
) -> tuple[str | None, bool, str | None]:
    """(端币种, 是否有币种未知的参与值, 冲突描述)。

    参与计算的每个 EPS 值（值非 None）都必须有自己的币种，不从另一期间、另一端借用：
    有未知参与值时端币种为 None；所有参与值已知且相同才给出端币种；已知币种不同为冲突。
    单字符串 curs 视为两期同币种。
    """
    if isinstance(curs, str):
        curs = (curs, curs)
    if not isinstance(curs, (tuple, list)):
        curs = (None, None)
    pairs = [
        (eps_0y, _norm_currency(curs[0]) if len(curs) > 0 else None),
        (eps_1y, _norm_currency(curs[1]) if len(curs) > 1 else None),
    ]
    has_unknown = any(v is not None and c is None for v, c in pairs)
    known = {c for v, c in pairs if v is not None and c}
    if len(known) > 1:
        detail = "、".join(f"{per} 为 {c}" for per, (v, c) in zip(("0y", "+1y"), pairs) if v is not None and c)
        return None, has_unknown, detail
    resolved = next(iter(known)) if len(known) == 1 and not has_unknown else None
    return resolved, has_unknown, None


def _valid_fx(fx: Any) -> bool:
    rate = fx.get("rate") if isinstance(fx, dict) else None
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0


def decompose_price_move(
    ticker: str,
    p_base: float | None,
    p_end: float | None,
    eps_0y_base: float | None,
    eps_1y_base: float | None,
    eps_0y_end: float | None,
    eps_1y_end: float | None,
    base_date: str,
    end_date: str,
    fiscal_year_end: str = "12-31",
    base_eps_source: str = "snapshot",
    name: str | None = None,
    eps_currencies_base: Any = None,
    eps_currencies_end: Any = None,
    price_currency: str | None = None,
    fx_rate_provider: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """
    对单只股票执行 NTM EPS 插值与 ln(P_end/P_base) = ln(EPS_end/EPS_base) + ln(PE_end/PE_base) 归因分解。

    币种规则：`eps_currencies_base/end` 传 (0y, +1y) 币种元组（或单一字符串）；参与计算的
    每个 EPS 值都必须有自己的币种（不从另一期间、另一端借用），任一未知不计算 P/E；
    跨期两端 EPS 币种须一致。`price_currency` 已知时直接使用，否则按交易所推断并标注。
    EPS 币种与价格币种相同时直接算 P/E；不同时经 `fx_rate_provider(eps_cur, price_cur,
    on_date)` 取**各自端日期**的汇率换算（汇率变动计入盈利贡献，输出注明两端汇率与日期），
    只拿到一端或拿不到汇率不硬算，倍数归因写“币种不一致，未计算”。
    """
    ntm_base, w_base = compute_ntm_eps(eps_0y_base, eps_1y_base, base_date, fiscal_year_end)
    ntm_end, w_end = compute_ntm_eps(eps_0y_end, eps_1y_end, end_date, fiscal_year_end)

    eps_cur_b, unknown_b, conflict_b = _end_currency_state(eps_0y_base, eps_1y_base, eps_currencies_base)
    eps_cur_e, unknown_e, conflict_e = _end_currency_state(eps_0y_end, eps_1y_end, eps_currencies_end)
    known_price_cur = _norm_currency(price_currency)
    price_cur = known_price_cur or infer_price_currency(ticker)
    price_cur_inferred = known_price_cur is None

    base = {
        "ticker": ticker,
        "name": name or ticker,
        "price_currency": price_cur,
        "price_currency_inferred": price_cur_inferred,
        "eps_currency_base": eps_cur_b,
        "eps_currency_end": eps_cur_e,
    }

    if p_base is None or p_end is None or p_base <= 0 or p_end <= 0:
        return {
            **base,
            "status": "missing_price",
            "driver": "数据缺失",
            "driver_display": "价格数据缺失",
        }

    simple_ret_pct = (p_end / p_base - 1.0) * 100.0
    log_ret_pp = math.log(p_end / p_base) * 100.0

    def _mismatch_row(explanation: str, display: str, eps_contrib_pp: float | None = None) -> dict[str, Any]:
        return {
            **base,
            "status": "currency_mismatch",
            "p_base": round(p_base, 2),
            "p_end": round(p_end, 2),
            "price_change_pct": round(simple_ret_pct, 2),
            "log_return_pp": round(log_ret_pp, 2),
            "ntm_eps_base": round(ntm_base, 3) if ntm_base is not None else None,
            "ntm_eps_end": round(ntm_end, 3) if ntm_end is not None else None,
            "w_base": w_base,
            "w_end": w_end,
            "pe_base": None,
            "pe_end": None,
            "eps_contrib_pp": round(eps_contrib_pp, 2) if eps_contrib_pp is not None else None,
            "pe_contrib_pp": None,
            "closure_error_pp": None,
            "driver": "币种不一致",
            "driver_display": display,
            "base_eps_source": base_eps_source,
            "eps_revision_source": "暂缺",
            "multiple_explanation": explanation,
        }

    if conflict_b or conflict_e:
        msg = conflict_b or conflict_e
        return _mismatch_row(f"EPS 期间币种不一致（{msg}），未计算", "EPS 币种不一致，未计算")
    if unknown_b or unknown_e:
        # 参与计算的每个 EPS 值都必须有自己的币种，不能从另一期间、另一端借用
        where = "与".join(side for side, u in (("起点", unknown_b), ("终点", unknown_e)) if u)
        return _mismatch_row(
            f"EPS 币种未知（{where} EPS 未标注币种），未计算 P/E",
            "EPS 币种未知，未计算",
        )
    if eps_cur_b and eps_cur_e and eps_cur_b != eps_cur_e:
        return _mismatch_row(
            f"跨期 EPS 币种不一致（起点 {eps_cur_b}、终点 {eps_cur_e}），未做跨期归因",
            "跨期 EPS 币种不一致，未计算",
        )

    # 负 EPS 或 EPS 接近 0 的公司不做分解，写“NM，改用 EV/Sales 定性”
    if (
        ntm_base is None
        or ntm_end is None
        or ntm_base <= EPS_NEAR_ZERO_THRESHOLD
        or ntm_end <= EPS_NEAR_ZERO_THRESHOLD
    ):
        return {
            **base,
            "status": "NM",
            "p_base": round(p_base, 2),
            "p_end": round(p_end, 2),
            "price_change_pct": round(simple_ret_pct, 2),
            "log_return_pp": round(log_ret_pp, 2),
            "ntm_eps_base": round(ntm_base, 3) if ntm_base is not None else None,
            "ntm_eps_end": round(ntm_end, 3) if ntm_end is not None else None,
            "w_base": w_base,
            "w_end": w_end,
            "pe_base": None,
            "pe_end": None,
            "eps_contrib_pp": None,
            "pe_contrib_pp": None,
            "closure_error_pp": 0.0,
            "driver": "NM，改用 EV/Sales 定性",
            "driver_display": "NM，改用 EV/Sales 定性",
            "base_eps_source": base_eps_source,
            "eps_revision_source": "暂缺",
            "multiple_explanation": "NM，改用 EV/Sales 定性",
        }

    eps_cur = eps_cur_b or eps_cur_e or None
    fx_used: dict[str, Any] | None = None
    # 两端 EPS 同币种，原币种比值（跨期汇率不变时与价格币种口径一致）
    raw_eps_contrib_pp = math.log(ntm_end / ntm_base) * 100.0
    if eps_cur == price_cur:
        pe_base = p_base / ntm_base
        pe_end = p_end / ntm_end
        eps_contrib_pp = raw_eps_contrib_pp
    elif fx_rate_provider is None or not eps_cur:
        price_text = f"{price_cur}（推断）" if price_cur_inferred else price_cur
        return _mismatch_row(
            f"币种不一致（EPS {eps_cur or '未知'}、价格 {price_text}），未取得汇率，未计算倍数；"
            f"EPS 贡献为 {eps_cur} 原币种口径",
            "币种不一致，倍数未计算",
            eps_contrib_pp=raw_eps_contrib_pp,
        )
    else:
        # 每一端用自己日期的汇率换算：汇率变动计入盈利贡献，两因子恒等式仍闭合
        fx_base = fx_rate_provider(eps_cur, price_cur, base_date)
        fx_end = fx_rate_provider(eps_cur, price_cur, end_date)
        missing = [side for side, fx in (("起点", fx_base), ("终点", fx_end)) if not _valid_fx(fx)]
        if missing:
            price_text = f"{price_cur}（推断）" if price_cur_inferred else price_cur
            return _mismatch_row(
                f"币种不一致（EPS {eps_cur}、价格 {price_text}），缺{'与'.join(missing)}汇率，未计算倍数；"
                f"EPS 贡献为 {eps_cur} 原币种口径",
                "币种不一致，倍数未计算",
                eps_contrib_pp=raw_eps_contrib_pp,
            )
        r_base, r_end = float(fx_base["rate"]), float(fx_end["rate"])
        pe_base = p_base / (ntm_base * r_base)
        pe_end = p_end / (ntm_end * r_end)
        eps_contrib_pp = math.log((ntm_end * r_end) / (ntm_base * r_base)) * 100.0
        fx_used = {"base": fx_base, "end": fx_end}

    pe_contrib_pp = math.log(pe_end / pe_base) * 100.0
    closure_err = abs((eps_contrib_pp + pe_contrib_pp) - log_ret_pp)

    driver_code, driver_display = classify_driver(eps_contrib_pp, pe_contrib_pp)

    return {
        **base,
        "status": "ok",
        "p_base": round(p_base, 2),
        "p_end": round(p_end, 2),
        "price_change_pct": round(simple_ret_pct, 2),
        "log_return_pp": round(log_ret_pp, 2),
        "ntm_eps_base": round(ntm_base, 3),
        "ntm_eps_end": round(ntm_end, 3),
        "w_base": w_base,
        "w_end": w_end,
        "pe_base": round(pe_base, 2),
        "pe_end": round(pe_end, 2),
        "eps_contrib_pp": round(eps_contrib_pp, 2),
        "pe_contrib_pp": round(pe_contrib_pp, 2),
        "closure_error_pp": round(closure_err, 6),
        "driver": driver_code,
        "driver_display": driver_display,
        "base_eps_source": base_eps_source,
        "eps_revision_source": "待子智能体从卖方研报补充（无则写暂缺）",
        "fx": fx_used,
        "multiple_explanation": (
            (
                f"EPS {eps_cur}→{price_cur}：起点汇率 {fx_used['base']['rate']}"
                f"（{fx_used['base'].get('as_of')}，{fx_used['base'].get('source')}）、"
                f"终点汇率 {fx_used['end']['rate']}"
                f"（{fx_used['end'].get('as_of')}，{fx_used['end'].get('source')}）；汇率变动计入盈利贡献"
            )
            if fx_used
            else "原因暂缺"
        ),
    }


def extract_eps_from_snapshot(snap: dict[str, Any]) -> tuple[float | None, float | None, str | None, str | None]:
    """(eps_0y, eps_+1y, cur_0y, cur_+1y)。优先 earnings_estimate（带 currency），
    缺项回退 eps_trend 的 current（同样读行内 currency，缺失为 None/未知）。"""
    ee = snap.get("earnings_estimate") if isinstance(snap.get("earnings_estimate"), dict) else {}
    row0 = ee.get("0y") if isinstance(ee.get("0y"), dict) else {}
    row1 = ee.get("+1y") if isinstance(ee.get("+1y"), dict) else {}
    e0, e1 = row0.get("avg"), row1.get("avg")
    cur0, cur1 = _norm_currency(row0.get("currency")), _norm_currency(row1.get("currency"))
    if e0 is None or e1 is None:
        et = snap.get("eps_trend") if isinstance(snap.get("eps_trend"), dict) else {}
        t0 = et.get("0y") if isinstance(et.get("0y"), dict) else {}
        t1 = et.get("+1y") if isinstance(et.get("+1y"), dict) else {}
        if e0 is None:
            e0, cur0 = t0.get("current"), _norm_currency(t0.get("currency"))
        if e1 is None:
            e1, cur1 = t1.get("current"), _norm_currency(t1.get("currency"))
    return (float(e0) if e0 is not None else None), (float(e1) if e1 is not None else None), cur0, cur1


def extract_7d_ago_eps(snap: dict[str, Any]) -> tuple[float | None, float | None, str | None, str | None]:
    """(eps_0y, eps_+1y, cur_0y, cur_+1y)，取 eps_trend 的 7daysAgo；币种缺失为 None（未知）。"""
    et = snap.get("eps_trend") if isinstance(snap.get("eps_trend"), dict) else {}
    t0 = et.get("0y") if isinstance(et.get("0y"), dict) else {}
    t1 = et.get("+1y") if isinstance(et.get("+1y"), dict) else {}
    e0, e1 = t0.get("7daysAgo"), t1.get("7daysAgo")
    return (
        float(e0) if e0 is not None else None,
        float(e1) if e1 is not None else None,
        _norm_currency(t0.get("currency")),
        _norm_currency(t1.get("currency")),
    )


def _merge_snapshot_fx(snaps: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """合并多份快照的 fx：规范化成 [{"pair","rate","as_of"}] 记录，按（货币对 + 日期）保留。

    字典按 key 合并会让起点快照的记录被终点快照覆盖；记录列表让起点、终点各自
    日期的汇率都留下，解析时（fx_from_snapshot）各取 <= 自己日期的最近一条。
    同（货币对 + 日期）的记录后见的快照覆盖先见的。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for s in snaps:
        for rec in _all_fx_records(s.get("fx")):
            merged[(rec["pair"], rec["as_of"])] = rec
    return list(merged.values()) or None


def _missing_window() -> dict[str, Any]:
    return {
        "e0_base": None, "e1_base": None, "cur0_base": None, "cur1_base": None,
        "e0_end": None, "e1_end": None, "cur0_end": None, "cur1_end": None,
        "source": "missing", "fx": None,
    }


def load_snapshots_for_window(
    ticker: str,
    workspace_dir: Path | None,
    base_date: str,
    end_date: str,
) -> dict[str, Any]:
    """
    优先从公司工作区 data/consensus/*.json 读取离 base 最近的快照和离 end 最新的快照；
    若无历史快照，回退调用 yfinance 获取 current 与 7daysAgo（标注“近似”）。

    返回 {"e0_base","e1_base","cur0_base","cur1_base","e0_end","e1_end","cur0_end","cur1_end",
    "source","fx"}；EPS 币种随值一起带出（缺失为 None/未知），fx 为快照里的汇率记录（若有）。
    """
    if workspace_dir:
        cdir = workspace_dir / "data" / "consensus"
        if cdir.exists():
            files = sorted(cdir.glob("*.json"))
            snaps: list[tuple[str, dict[str, Any]]] = []
            for f in files:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        ts_str = str(d.get("fetched_at") or f.stem)[:10]
                        snaps.append((ts_str, d))
                except Exception:
                    continue
            if len(snaps) >= 2:
                # 找离 base_date 最近的一份快照
                base_dt = date.fromisoformat(base_date[:10])
                base_snap = min(snaps, key=lambda item: abs((date.fromisoformat(item[0][:10]) - base_dt).days))[1]
                end_snap = snaps[-1][1]
                e0_b, e1_b, cb0, cb1 = extract_eps_from_snapshot(base_snap)
                e0_e, e1_e, ce0, ce1 = extract_eps_from_snapshot(end_snap)
                if (e0_b is not None or e1_b is not None) and (e0_e is not None or e1_e is not None):
                    return {
                        "e0_base": e0_b, "e1_base": e1_b, "cur0_base": cb0, "cur1_base": cb1,
                        "e0_end": e0_e, "e1_end": e1_e, "cur0_end": ce0, "cur1_end": ce1,
                        "source": "snapshot", "fx": _merge_snapshot_fx([base_snap, end_snap]),
                    }
            elif len(snaps) == 1:
                only_snap = snaps[0][1]
                e0_e, e1_e, ce0, ce1 = extract_eps_from_snapshot(only_snap)
                e0_b, e1_b, cb0, cb1 = extract_7d_ago_eps(only_snap)
                if (e0_b is not None or e1_b is not None) and (e0_e is not None or e1_e is not None):
                    return {
                        "e0_base": e0_b, "e1_base": e1_b, "cur0_base": cb0, "cur1_base": cb1,
                        "e0_end": e0_e, "e1_end": e1_e, "cur0_end": ce0, "cur1_end": ce1,
                        "source": "近似（7daysAgo）", "fx": _merge_snapshot_fx([only_snap]),
                    }

    # 回退：直接通过 yfinance 读取 eps_trend 的 current 与 7daysAgo（行内 currency 一并带出）
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        et_df = getattr(t, "eps_trend", None)
        if et_df is not None and not getattr(et_df, "empty", True):
            rec = et_df.to_dict(orient="index")
            r0 = rec.get("0y") or {}
            r1 = rec.get("+1y") or {}

            def _num(x: Any) -> float | None:
                return float(x) if x is not None and not math.isnan(float(x)) else None

            return {
                "e0_base": _num(r0.get("7daysAgo")), "e1_base": _num(r1.get("7daysAgo")),
                "cur0_base": _norm_currency(r0.get("currency")), "cur1_base": _norm_currency(r1.get("currency")),
                "e0_end": _num(r0.get("current")), "e1_end": _num(r1.get("current")),
                "cur0_end": _norm_currency(r0.get("currency")), "cur1_end": _norm_currency(r1.get("currency")),
                "source": "近似（7daysAgo）", "fx": None,
            }
    except Exception:
        pass
    return _missing_window()


def _currency_cell(r: dict[str, Any]) -> str:
    """表格用“价格币种（推断时标注）/EPS 币种”单元格；无币种信息时为 -。"""
    pc = r.get("price_currency")
    if not pc:
        return "-"
    price = f"{pc}（推断）" if r.get("price_currency_inferred") else pc
    eps_cur = r.get("eps_currency_end") or r.get("eps_currency_base") or "未知"
    return f"{price}/{eps_cur}"


def render_markdown_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| 公司 | 股价变化（对数变化） | EPS 预期贡献（pp） | 倍数贡献（pp） | 主导驱动 | 币种（价格/EPS） | EPS 口径来源 | 盈利预测变化来源 | 倍数变化解释 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        t_label = f"{r.get('name', r['ticker'])} ({r['ticker']})"
        if r.get("status") == "NM":
            p_str = f"{r.get('price_change_pct', 0):+.2f}% ({r.get('log_return_pp', 0):+.2f}pp)"
            lines.append(f"| {t_label} | {p_str} | NM | NM | {r['driver_display']} | {_currency_cell(r)} | {r.get('base_eps_source', '')} | 暂缺 | NM，改用 EV/Sales 定性 |")
        elif r.get("status") == "ok":
            p_str = f"{r['price_change_pct']:+.2f}% ({r['log_return_pp']:+.2f}pp)"
            e_str = f"{r['eps_contrib_pp']:+.2f}pp"
            m_str = f"{r['pe_contrib_pp']:+.2f}pp"
            lines.append(
                f"| {t_label} | {p_str} | {e_str} | {m_str} | {r['driver_display']} | {_currency_cell(r)} | "
                f"{r.get('base_eps_source', '')} | {r.get('eps_revision_source', '暂缺')} | {r.get('multiple_explanation', '原因暂缺')} |"
            )
        elif r.get("status") == "currency_mismatch":
            p_str = f"{r['price_change_pct']:+.2f}% ({r['log_return_pp']:+.2f}pp)"
            e_str = f"{r['eps_contrib_pp']:+.2f}pp" if r.get("eps_contrib_pp") is not None else "未计算"
            lines.append(
                f"| {t_label} | {p_str} | {e_str} | 币种不一致，未计算 | {r['driver_display']} | {_currency_cell(r)} | "
                f"{r.get('base_eps_source', '')} | 暂缺 | {r.get('multiple_explanation', '币种不一致，未计算')} |"
            )
        else:
            lines.append(f"| {t_label} | 数据缺失 | - | - | 数据缺失 | - | - | - | - |")
    return "\n".join(lines)


def run_self_test() -> dict[str, Any]:
    """
    自检：验证 NTM EPS 加权插值、对数分解闭合性（误差 < 0.1pp）、四类主导判定（盈利驱动 / 估值驱动 / 两者 / 相抵）、
    负 EPS 的 NM 处理，以及币种契约（同币种直算、经汇率换算、币种不一致不硬算、跨期币种不一致不归因）。
    """
    # 1. 盈利驱动样例：股价从 100 涨到 110 (+9.531% log)，EPS 从 10 涨到 10.9 (+8.618% log)，倍数微扩 (+0.913% log)
    r_earn = decompose_price_move(
        ticker="META",
        p_base=100.0,
        p_end=110.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.9,
        eps_1y_end=10.9,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="USD",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    # 2. 估值驱动样例：股价从 100 涨到 110，EPS 从 10 涨到 10.1，主要由 PE 倍数驱动
    r_mult = decompose_price_move(
        ticker="AMZN",
        p_base=100.0,
        p_end=110.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.1,
        eps_1y_end=10.1,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="USD",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    # 3. 两者共同驱动样例：EPS +4% log，PE +4% log
    r_both = decompose_price_move(
        ticker="GOOGL",
        p_base=100.0,
        p_end=108.3287,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.4081,
        eps_1y_end=10.4081,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="USD",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    # 4. 相抵样例：EPS 上修 +5%，但股价跌 -3%（倍数收缩约 -8%）
    r_offset = decompose_price_move(
        ticker="MSFT",
        p_base=100.0,
        p_end=97.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.5,
        eps_1y_end=10.5,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="USD",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    # 5. 负 EPS / 近零 EPS -> NM
    r_nm = decompose_price_move(
        ticker="RIVN",
        p_base=15.0,
        p_end=14.0,
        eps_0y_base=-2.5,
        eps_1y_base=-1.8,
        eps_0y_end=-2.4,
        eps_1y_end=-1.7,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="USD",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    # 6. NTM EPS 跨期插值检验：年中 2026-07-02（距 12-31 剩 182 天，w = 182/365 ≈ 0.4986）
    ntm_val, w_val = compute_ntm_eps(10.0, 20.0, "2026-07-02", "12-31")
    expected_ntm = (182 / 365.0) * 10.0 + (1 - 182 / 365.0) * 20.0

    # 7. 币种契约：价格 USD、EPS CNY，两端各按自己日期的汇率换算 -> 汇率变动计入盈利贡献
    def _dated_fx(from_cur: str, to_cur: str, on_date: str) -> dict[str, Any]:
        if on_date.startswith("2026-10-01"):
            return {"rate": 0.14, "as_of": "2026-10-01", "source": f"snapshot {from_cur}/{to_cur}"}
        return {"rate": 0.15, "as_of": "2026-10-08", "source": f"snapshot {from_cur}/{to_cur}"}

    r_fx = decompose_price_move(
        ticker="BABA",
        p_base=100.0,
        p_end=103.0,
        eps_0y_base=60.0,
        eps_1y_base=66.0,
        eps_0y_end=61.8,
        eps_1y_end=67.8,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="CNY",
        eps_currencies_end="CNY",
        price_currency="USD",
        fx_rate_provider=_dated_fx,
    )

    # 8. 币种契约：拿不到汇率 -> 倍数不硬算；9. 跨期 EPS 币种不一致 -> 不做跨期归因
    r_no_fx = decompose_price_move(
        ticker="BABA",
        p_base=100.0,
        p_end=103.0,
        eps_0y_base=60.0,
        eps_1y_base=66.0,
        eps_0y_end=61.8,
        eps_1y_end=67.8,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="CNY",
        eps_currencies_end="CNY",
        price_currency="USD",
    )
    r_cross = decompose_price_move(
        ticker="BABA",
        p_base=100.0,
        p_end=103.0,
        eps_0y_base=60.0,
        eps_1y_base=66.0,
        eps_0y_end=8.6,
        eps_1y_end=9.4,
        base_date="2026-10-01",
        end_date="2026-10-08",
        eps_currencies_base="CNY",
        eps_currencies_end="USD",
        price_currency="USD",
    )

    checks = {
        "earnings_driven": r_earn["driver"] == "盈利驱动",
        "multiple_driven": r_mult["driver"] == "估值驱动",
        "both_driven": r_both["driver"] == "两者",
        "offset_driven": r_offset["driver"] == "相抵" and "相抵" in r_offset["driver_display"],
        "nm_negative_eps": r_nm["status"] == "NM" and r_nm["driver"] == "NM，改用 EV/Sales 定性",
        "math_closure_under_0_1pp": all(
            abs((r["eps_contrib_pp"] + r["pe_contrib_pp"]) - r["log_return_pp"]) < 0.1
            for r in (r_earn, r_mult, r_both, r_offset)
        ),
        "ntm_interpolation_accurate": ntm_val is not None and abs(ntm_val - expected_ntm) < 1e-6,
        "fx_converted_pe_and_noted": (
            r_fx["status"] == "ok"
            and r_fx["fx"]["base"]["rate"] == 0.14
            and r_fx["fx"]["end"]["rate"] == 0.15
            and "0.14" in r_fx["multiple_explanation"]
            and "0.15" in r_fx["multiple_explanation"]
            and "汇率变动计入盈利贡献" in r_fx["multiple_explanation"]
            and abs((r_fx["eps_contrib_pp"] + r_fx["pe_contrib_pp"]) - r_fx["log_return_pp"]) < 0.1
        ),
        "no_fx_not_computed": (
            r_no_fx["status"] == "currency_mismatch"
            and r_no_fx["pe_end"] is None
            and "币种不一致" in r_no_fx["multiple_explanation"]
        ),
        "cross_period_currency_refused": (
            r_cross["status"] == "currency_mismatch"
            and r_cross["pe_end"] is None
            and "跨期 EPS 币种不一致" in r_cross["multiple_explanation"]
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "samples": [r_earn, r_mult, r_both, r_offset, r_nm, r_fx, r_no_fx, r_cross],
        "markdown_preview": render_markdown_table([r_earn, r_mult, r_both, r_offset, r_nm, r_fx, r_no_fx, r_cross]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dalton EPS 预期修正 vs 估值倍数归因工具 (PS.13)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例（美元标的，需声明 EPS 币种，否则 P/E 不计算）：\n"
            "  attribution.py single --ticker META --p-base 100 --p-end 110 --eps0-base 10 "
            "--eps1-base 10 --eps0-end 10.9 --eps1-end 10.9 --eps-currency USD\n"
            "示例（跨币种：EPS 为 CNY、价格为 USD，两端各按自己日期的汇率换算）：\n"
            "  attribution.py single --ticker BABA --p-base 100 --p-end 103 --eps0-base 60 "
            "--eps1-base 66 --eps0-end 61.8 --eps1-end 67.8 --eps-currency CNY --price-currency USD\n"
            "\n"
            "--eps-currency：EPS 币种（如 USD/CNY）；不传视为未知，P/E 不计算。\n"
            "--price-currency：价格币种；不传按交易所推断（.HK→HKD、沪深→CNY、其余→USD）并标注“推断”。"
        ),
    )
    ap.add_argument("command", nargs="?", default="run", choices=["run", "single", "self-test"])
    ap.add_argument("--screen", help="screen_movers.py 输出的 screen.json 路径")
    ap.add_argument("--ticker", help="单股计算模式下的股票代码")
    ap.add_argument("--p-base", type=float, help="单股模式：窗口起点收盘价")
    ap.add_argument("--p-end", type=float, help="单股模式：窗口终点收盘价")
    ap.add_argument("--eps0-base", type=float, help="单股模式：起点本财年(0y) EPS")
    ap.add_argument("--eps1-base", type=float, help="单股模式：起点下一财年(+1y) EPS")
    ap.add_argument("--eps0-end", type=float, help="单股模式：终点本财年(0y) EPS")
    ap.add_argument("--eps1-end", type=float, help="单股模式：终点下一财年(+1y) EPS")
    ap.add_argument("--eps-currency", help="单股模式：EPS 币种（如 USD/CNY）；不传视为未知，P/E 不计算")
    ap.add_argument("--price-currency", help="单股模式：价格币种；不传按交易所推断并标注")
    ap.add_argument("--base", default="2026-10-01", help="窗口起点日期 YYYY-MM-DD")
    ap.add_argument("--end", default="2026-10-08", help="窗口终点日期 YYYY-MM-DD")
    ap.add_argument("--fye", default="12-31", help="财年结束月日 MM-DD（默认 12-31）")
    ap.add_argument("--out", help="将归因 JSON 写入指定文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    if args.command == "self-test":
        res = run_self_test()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.command == "single" or (args.ticker and args.p_base is not None and args.p_end is not None):
        row = decompose_price_move(
            ticker=args.ticker or "SAMPLE",
            p_base=args.p_base,
            p_end=args.p_end,
            eps_0y_base=args.eps0_base,
            eps_1y_base=args.eps1_base,
            eps_0y_end=args.eps0_end,
            eps_1y_end=args.eps1_end,
            base_date=args.base,
            end_date=args.end,
            fiscal_year_end=args.fye,
            base_eps_source="manual",
            eps_currencies_base=args.eps_currency,
            eps_currencies_end=args.eps_currency,
            price_currency=args.price_currency,
        )
        if not args.eps_currency and row.get("status") == "currency_mismatch":
            row["multiple_explanation"] = "未提供 EPS 币种，未计算 P/E；美元标的请加 `--eps-currency USD`"
        out_obj = {"ok": True, "rows": [row], "markdown_table": render_markdown_table([row])}
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
        return 0

    if not args.screen:
        ap.print_help()
        return 0

    scr = json.loads(Path(args.screen).read_text(encoding="utf-8"))
    base_str = scr.get("base") or args.base
    end_str = scr.get("end") or args.end
    targets = list(scr.get("core") or []) + list(scr.get("flagged") or [])

    import pandas as pd
    import yfinance as yf  # type: ignore

    tickers = sorted({r["ticker"] for r in targets if r.get("ticker")})
    raw_prices = yf.download(
        tickers,
        start=(date.fromisoformat(base_str) - pd.Timedelta(days=10)).isoformat(),
        end=(date.fromisoformat(end_str) + pd.Timedelta(days=2)).isoformat(),
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )

    base_ts, end_ts = pd.Timestamp(base_str), pd.Timestamp(end_str)
    rows_out: list[dict[str, Any]] = []
    for item in targets:
        t = item["ticker"]
        ws_dir = Path(item["workspace_dir"]) if item.get("workspace_dir") else None
        p_base = p_end = None
        try:
            s = raw_prices[t]["Adj Close"].dropna() if isinstance(raw_prices.columns, pd.MultiIndex) else raw_prices["Adj Close"].dropna()
            sb = s[s.index <= base_ts]
            se = s[s.index <= end_ts]
            if len(sb) and len(se):
                p_base, p_end = float(sb.iloc[-1]), float(se.iloc[-1])
        except Exception:
            pass

        win = load_snapshots_for_window(t, ws_dir, base_str, end_str)
        # 价格币种：行情接口返回优先；推断出的币种交给 decompose 自行推断并标注
        p_cur, p_cur_src = get_price_currency(t)
        snap_fx = win.get("fx")
        row = decompose_price_move(
            ticker=t,
            p_base=p_base,
            p_end=p_end,
            eps_0y_base=win["e0_base"],
            eps_1y_base=win["e1_base"],
            eps_0y_end=win["e0_end"],
            eps_1y_end=win["e1_end"],
            base_date=base_str,
            end_date=end_str,
            base_eps_source=win["source"],
            name=item.get("name"),
            eps_currencies_base=(win["cur0_base"], win["cur1_base"]),
            eps_currencies_end=(win["cur0_end"], win["cur1_end"]),
            price_currency=p_cur if p_cur_src == "quote" else None,
            fx_rate_provider=lambda f, to, on, snap_fx=snap_fx: resolve_fx_rate(f, to, on, snapshot_fx=snap_fx),
        )
        rows_out.append(row)

    payload = {
        "ok": True,
        "base": base_str,
        "end": end_str,
        "count": len(rows_out),
        "rows": rows_out,
        "markdown_table": render_markdown_table(rows_out),
    }
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
