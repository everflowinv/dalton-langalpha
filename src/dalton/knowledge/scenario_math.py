"""Pure scenario underwriting math and CLI entry point (§4, §12.1–§12.3, §12.8).

All calculation and verification helpers in this module are pure functions with no IO,
shared by `validate.py`, `render.py`, and skill scripts via CLI (`python -m src.dalton.knowledge.scenario_math`).
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

EPSILON = 1e-9
STANDARD_B_ROWS: tuple[float, ...] = (0.25, 0.50)
MAX_STATE_WIDTH_RATIO = 0.25


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    return float(val)


def compute_range_stats(band: dict[str, Any]) -> dict[str, float]:
    """Return `lo`, `hi`, `mid`, and `rel_width` for a range object `R = {lo, hi}`."""
    lo = float(band["lo"])
    hi = float(band["hi"])
    mid = (lo + hi) / 2.0
    rel_width = (hi - lo) / mid if abs(mid) > EPSILON else (0.0 if hi == lo else math.inf)
    return {
        "lo": lo,
        "hi": hi,
        "mid": mid,
        "rel_width": rel_width,
    }


def compute_state_band(state: dict[str, Any], *, check_width: bool = True) -> dict[str, Any]:
    """Compute price band and total-return terminal band for a single state (§4.1, §12.2).

    For `method == 'eps_pe'`, multiplies endpoints (`eps.lo * mult.lo`, `eps.hi * mult.hi`).
    For `method == 'reference'`, reads `calc.price_range`.
    Adds `cash_return_per_share` (`c_s`, default 0) to form `v_tr = {lo, mid, hi}`.
    """
    state_name = str(state.get("name") or "stress")
    calc = state.get("calc") if isinstance(state.get("calc"), dict) else {}
    method = str(calc.get("method") or "")
    errors: list[str] = []

    price_lo = 0.0
    price_hi = 0.0
    eps_stats: dict[str, float] | None = None
    mult_stats: dict[str, float] | None = None

    if method == "eps_pe":
        eps_raw = calc.get("eps")
        mult_raw = calc.get("multiple")
        if not isinstance(eps_raw, dict) or "lo" not in eps_raw or "hi" not in eps_raw:
            errors.append(
                f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `eps_pe` 时，必须提供 `calc.eps`（包含 `lo` 与 `hi`）"
            )
        if not isinstance(mult_raw, dict) or "lo" not in mult_raw or "hi" not in mult_raw:
            errors.append(
                f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `eps_pe` 时，必须提供 `calc.multiple`（包含 `lo` 与 `hi`）"
            )
        if not errors:
            eps_stats = compute_range_stats(eps_raw)
            mult_stats = compute_range_stats(mult_raw)
            if eps_stats["lo"] > eps_stats["hi"] + EPSILON:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.eps.lo`（{eps_stats['lo']}）不能大于 `calc.eps.hi`（{eps_stats['hi']}）"
                )
            if mult_stats["lo"] > mult_stats["hi"] + EPSILON:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.multiple.lo`（{mult_stats['lo']}）不能大于 `calc.multiple.hi`（{mult_stats['hi']}）"
                )
            if eps_stats["lo"] <= 0:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `eps_pe` 时，要求 `calc.eps.lo` > 0（当前为 {eps_stats['lo']}）；亏损或微利情景请改用 `reference` 方法"
                )
            if mult_stats["lo"] <= 0:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `eps_pe` 时，要求 `calc.multiple.lo` > 0（当前为 {mult_stats['lo']}）；请改用 `reference` 方法"
                )
            price_lo = eps_stats["lo"] * mult_stats["lo"]
            price_hi = eps_stats["hi"] * mult_stats["hi"]
            declared_pr = calc.get("price_range")
            if isinstance(declared_pr, dict) and "lo" in declared_pr and "hi" in declared_pr:
                decl_lo = float(declared_pr["lo"])
                decl_hi = float(declared_pr["hi"])
                if abs(decl_lo - price_lo) > max(1e-4, 1e-3 * abs(price_lo)) or abs(
                    decl_hi - price_hi
                ) > max(1e-4, 1e-3 * abs(price_hi)):
                    errors.append(
                        f"`underwriting.states` 中 `{state_name}` 情景填写的 `calc.price_range`（[{decl_lo}, {decl_hi}]）"
                        f"与 `eps * multiple` 算出的区间（[{price_lo:.4f}, {price_hi:.4f}]）不一致"
                    )
    elif method == "reference":
        pr_raw = calc.get("price_range")
        ref_raw = calc.get("ref")
        has_ref = False
        if isinstance(ref_raw, str) and ref_raw.strip() and not ref_raw.strip().lower().startswith("assumption:"):
            has_ref = True
        elif isinstance(ref_raw, dict) and any(
            str(ref_raw.get(k) or "").strip()
            for k in ("ref", "evidence_id", "path", "cell", "provenance_record_id", "url", "accession", "publication_id")
        ):
            has_ref = True
        if not has_ref:
            errors.append(
                f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `reference` 时，必须提供指向 `evidence` 计算记录或 `models` 已登记模型单元格的 `calc.ref`"
            )
        if not isinstance(pr_raw, dict) or "lo" not in pr_raw or "hi" not in pr_raw:
            errors.append(
                f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 为 `reference` 时，必须提供 `calc.price_range`（包含 `lo` 与 `hi`）"
            )
        else:
            pr_stats = compute_range_stats(pr_raw)
            price_lo = pr_stats["lo"]
            price_hi = pr_stats["hi"]
            if price_lo > price_hi + EPSILON:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.price_range.lo`（{price_lo}）不能大于 `calc.price_range.hi`（{price_hi}）"
                )
            if price_lo < 0:
                errors.append(
                    f"`underwriting.states` 中 `{state_name}` 情景的 `calc.price_range.lo`（{price_lo}）必须大于或等于 0"
                )
    else:
        errors.append(
            f"`underwriting.states` 中 `{state_name}` 情景的 `calc.method` 不支持 `{method}`；请使用 `eps_pe` 或 `reference`"
        )

    price_mid = (price_lo + price_hi) / 2.0
    rel_width = (
        (price_hi - price_lo) / price_mid
        if abs(price_mid) > EPSILON
        else (0.0 if abs(price_hi - price_lo) <= EPSILON else math.inf)
    )
    width_ok = rel_width <= MAX_STATE_WIDTH_RATIO + EPSILON
    if check_width and not errors and not width_ok:
        errors.append(
            f"`underwriting.states` 中 `{state_name}` 情景的价格区间相对宽度 `(hi - lo) / mid` 为 {(rel_width * 100):.2f}%，超过上限 25.00%"
        )

    cash_return = _to_float(state.get("cash_return_per_share"), 0.0)
    v_tr_lo = price_lo + cash_return
    v_tr_hi = price_hi + cash_return
    v_tr_mid = price_mid + cash_return

    eff_multiple: float | None = None
    if method == "eps_pe" and eps_stats is not None and eps_stats["mid"] > 0:
        eff_multiple = price_mid / eps_stats["mid"]

    return {
        "name": state_name,
        "method": method,
        "period": calc.get("period"),
        "basis": calc.get("basis"),
        "price": {
            "lo": price_lo,
            "mid": price_mid,
            "hi": price_hi,
            "rel_width": rel_width,
            "width_ok": width_ok,
        },
        "cash_return_per_share": cash_return,
        "v_tr": {
            "lo": v_tr_lo,
            "mid": v_tr_mid,
            "hi": v_tr_hi,
        },
        "eps": eps_stats,
        "multiple": mult_stats,
        "effective_multiple": eff_multiple,
        "errors": errors,
    }


def validate_states_order(states_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Check bear/base/bull price band ordering and total-return midpoint monotonicity (§4.1, §12.2)."""
    errors: list[str] = []
    warnings: list[str] = []
    for req_name in ("bear", "base", "bull"):
        if req_name not in states_by_name:
            errors.append(
                f"`underwriting.states` 缺少必填情景 `{req_name}`（必须完整包含 `bear`、`base`、`bull` 三情景）"
            )
    if errors:
        return {"ok": False, "v_tr_monotonic": False, "errors": errors, "warnings": warnings}

    bear = states_by_name["bear"]
    base = states_by_name["base"]
    bull = states_by_name["bull"]

    p_d, p_m, p_u = bear["price"], base["price"], bull["price"]
    if not (p_d["lo"] <= p_m["lo"] + EPSILON and p_m["lo"] <= p_u["lo"] + EPSILON):
        errors.append(
            f"三情景目标股价下限 `price.lo` 必须满足 `bear` <= `base` <= `bull` "
            f"（当前分别为 {p_d['lo']:.4f}、{p_m['lo']:.4f}、{p_u['lo']:.4f}）"
        )
    if not (p_d["hi"] <= p_m["hi"] + EPSILON and p_m["hi"] <= p_u["hi"] + EPSILON):
        errors.append(
            f"三情景目标股价上限 `price.hi` 必须满足 `bear` <= `base` <= `bull` "
            f"（当前分别为 {p_d['hi']:.4f}、{p_m['hi']:.4f}、{p_u['hi']:.4f}）"
        )
    if not (p_d["mid"] < p_m["mid"] - EPSILON and p_m["mid"] < p_u["mid"] - EPSILON):
        errors.append(
            f"三情景目标股价中点 `price.mid` 必须按 `bear` < `base` < `bull` 严格递增 "
            f"（当前分别为 {p_d['mid']:.4f}、{p_m['mid']:.4f}、{p_u['mid']:.4f}）"
        )

    v_d, v_m, v_u = bear["v_tr"], base["v_tr"], bull["v_tr"]
    v_tr_monotonic = bool(v_d["mid"] < v_m["mid"] - EPSILON and v_m["mid"] < v_u["mid"] - EPSILON)
    if not v_tr_monotonic:
        warnings.append(
            f"三情景含现金回报终值中点未按 `bear` < `base` < `bull` 严格递增 "
            f"（当前分别为 {v_d['mid']:.4f}、{v_m['mid']:.4f}、{v_u['mid']:.4f}），"
            "达标胜率反推降级为 `cannot_judge`"
        )

    return {
        "ok": len(errors) == 0,
        "v_tr_monotonic": v_tr_monotonic,
        "errors": errors,
        "warnings": warnings,
    }


def compute_long_hurdle(
    p0: float,
    horizon_months: int | float,
    long_hurdle_annual: float = 0.15,
    min_total_return: float = 0.10,
) -> float:
    """Compute Long terminal hurdle `H_L = P0 * max((1 + h_L)^T, 1 + min_total_return)` (§4.2, §12.1)."""
    t_years = float(horizon_months) / 12.0
    compounded = (1.0 + float(long_hurdle_annual)) ** t_years
    floor_mult = 1.0 + float(min_total_return)
    return float(p0) * max(compounded, floor_mult)


def compute_short_hurdle(
    p0: float,
    horizon_months: int | float,
    short_hurdle_annual: float = 0.20,
    borrow_cost_annual: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """Compute Short terminal hurdle `H_S` incorporating borrow cost and dividend drag (§12.1).

    `H_S = P0 * (1 - min(short_hurdle_annual * T, 0.5) - borrow_cost * T - dividend_yield * T)`
    """
    t_years = float(horizon_months) / 12.0
    drop_req = min(float(short_hurdle_annual) * t_years, 0.5)
    drag = (float(borrow_cost_annual) + float(dividend_yield)) * t_years
    return float(p0) * (1.0 - drop_req - drag)


def round_outward_5pct(lo_ratio: float, hi_ratio: float) -> tuple[int, int]:
    """Outward-round a ratio interval `[lo_ratio, hi_ratio]` to 5-percentage-point integers (§4.2)."""
    lo_clamped = max(0.0, min(1.0, min(lo_ratio, hi_ratio)))
    hi_clamped = max(0.0, min(1.0, max(lo_ratio, hi_ratio)))
    lo_pct = int(math.floor((lo_clamped * 100.0) / 5.0 + EPSILON) * 5)
    hi_pct = int(math.ceil((hi_clamped * 100.0) / 5.0 - EPSILON) * 5)
    return max(0, lo_pct), min(100, hi_pct)


def _solve_u_req_single(b: float, v_d: float, v_m: float, v_u: float, h_l: float) -> dict[str, Any]:
    """Solve single-point `u_req(b)` for Long given scalar terminal values `(v_d, v_m, v_u)` (§4.2, §12.1, §12.2)."""
    max_u = 1.0 - b
    ev_at_zero = b * v_m + (1.0 - b) * v_d
    ev_at_max_u = b * v_m + (1.0 - b) * v_u

    if abs(v_u - v_d) <= EPSILON and abs(v_m - v_d) <= EPSILON:
        if ev_at_zero >= h_l - EPSILON:
            return {
                "raw": 0.0,
                "clamped": 0.0,
                "d_implied": max_u,
                "status": "无需 bull 即达标",
                "reachable": True,
                "no_bull_needed": True,
                "formula_applicable": False,
                "ev_u_zero": ev_at_zero,
                "ev_u_max": ev_at_max_u,
            }
        return {
            "raw": math.inf,
            "clamped": None,
            "d_implied": None,
            "status": "该组合不可达",
            "reachable": False,
            "no_bull_needed": False,
            "formula_applicable": False,
            "ev_u_zero": ev_at_zero,
            "ev_u_max": ev_at_max_u,
        }

    # §12.2 degenerate case when cash return breaks strict monotonicity V_D < V_M < V_U
    if not (v_d < v_m - EPSILON and v_m < v_u - EPSILON):
        return {
            "raw": None,
            "clamped": None,
            "d_implied": None,
            "status": "无法判断",
            "verdict": "cannot_judge",
            "formula_applicable": False,
            "ev_u_zero": ev_at_zero,
            "ev_u_max": ev_at_max_u,
            "u_zero_passes": ev_at_zero >= h_l - EPSILON,
            "u_max_passes": ev_at_max_u >= h_l - EPSILON,
            "reachable": (ev_at_zero >= h_l - EPSILON) or (ev_at_max_u >= h_l - EPSILON),
            "no_bull_needed": (ev_at_zero >= h_l - EPSILON) and (ev_at_max_u >= h_l - EPSILON),
        }

    raw_u = (h_l - b * v_m - (1.0 - b) * v_d) / (v_u - v_d)
    no_bull_needed = raw_u <= EPSILON
    if raw_u < -EPSILON:
        return {
            "raw": raw_u,
            "clamped": 0.0,
            "d_implied": max_u,
            "status": "无需 bull 即达标",
            "reachable": True,
            "no_bull_needed": True,
            "formula_applicable": True,
            "ev_u_zero": ev_at_zero,
            "ev_u_max": ev_at_max_u,
        }
    if raw_u > max_u + EPSILON:
        return {
            "raw": raw_u,
            "clamped": None,
            "d_implied": None,
            "status": "该组合不可达",
            "reachable": False,
            "no_bull_needed": False,
            "formula_applicable": True,
            "ev_u_zero": ev_at_zero,
            "ev_u_max": ev_at_max_u,
        }
    clamped = max(0.0, min(max_u, raw_u))
    return {
        "raw": raw_u,
        "clamped": clamped,
        "d_implied": max(0.0, max_u - clamped),
        "status": "无需 bull 即达标" if abs(clamped) <= EPSILON else "可达",
        "reachable": True,
        "no_bull_needed": no_bull_needed,
        "formula_applicable": True,
        "ev_u_zero": ev_at_zero,
        "ev_u_max": ev_at_max_u,
    }


def _solve_d_req_single(b: float, v_d: float, v_m: float, v_u: float, h_s: float) -> dict[str, Any]:
    """Solve single-point `d_req(b)` for Short given scalar terminal price values `(v_d, v_m, v_u)` (§12.1, §12.2)."""
    max_d = 1.0 - b
    ev_at_zero = b * v_m + (1.0 - b) * v_u
    ev_at_max_d = b * v_m + (1.0 - b) * v_d

    if abs(v_u - v_d) <= EPSILON:
        if ev_at_zero <= h_s + EPSILON:
            return {
                "raw": 0.0,
                "clamped": 0.0,
                "u_implied": max_d,
                "status": "无需 bear 即达标",
                "reachable": True,
                "no_bear_needed": True,
                "ev_d_zero": ev_at_zero,
                "ev_d_max": ev_at_max_d,
            }
        return {
            "raw": math.inf,
            "clamped": None,
            "u_implied": None,
            "status": "该组合不可达",
            "reachable": False,
            "no_bear_needed": False,
            "ev_d_zero": ev_at_zero,
            "ev_d_max": ev_at_max_d,
        }

    raw_d = (b * v_m + (1.0 - b) * v_u - h_s) / (v_u - v_d)
    no_bear_needed = raw_d <= EPSILON
    if raw_d < -EPSILON:
        return {
            "raw": raw_d,
            "clamped": 0.0,
            "u_implied": max_d,
            "status": "无需 bear 即达标",
            "reachable": True,
            "no_bear_needed": True,
            "ev_d_zero": ev_at_zero,
            "ev_d_max": ev_at_max_d,
        }
    if raw_d > max_d + EPSILON:
        return {
            "raw": raw_d,
            "clamped": None,
            "u_implied": None,
            "status": "该组合不可达",
            "reachable": False,
            "no_bear_needed": False,
            "ev_d_zero": ev_at_zero,
            "ev_d_max": ev_at_max_d,
        }
    clamped = max(0.0, min(max_d, raw_d))
    return {
        "raw": raw_d,
        "clamped": clamped,
        "u_implied": max(0.0, max_d - clamped),
        "status": "无需 bear 即达标" if abs(clamped) <= EPSILON else "可达",
        "reachable": True,
        "no_bear_needed": no_bear_needed,
        "ev_d_zero": ev_at_zero,
        "ev_d_max": ev_at_max_d,
    }


def compute_u_req_row(
    b: float,
    v_tr_d: dict[str, float],
    v_tr_m: dict[str, float],
    v_tr_u: dict[str, float],
    h_l: float,
) -> dict[str, Any]:
    """Compute Long `u_req(b)` at midpoints and `(all_lo, all_hi)` endpoints with 5% outward rounding (§4.2, §12.2)."""
    mid_res = _solve_u_req_single(b, v_tr_d["mid"], v_tr_m["mid"], v_tr_u["mid"], h_l)
    lo_res = _solve_u_req_single(b, v_tr_d["lo"], v_tr_m["lo"], v_tr_u["lo"], h_l)
    hi_res = _solve_u_req_single(b, v_tr_d["hi"], v_tr_m["hi"], v_tr_u["hi"], h_l)

    if mid_res.get("status") == "无法判断":
        display = (
            f"无法判断（Bull 权重 0% 期望 {mid_res['ev_u_zero']:.2f}，"
            f"Bull 权重 {1.0 - b:.0%} 期望 {mid_res['ev_u_max']:.2f}）"
        )
        band_pct: tuple[int, int] | None = None
    elif not mid_res["reachable"] and not hi_res["reachable"]:
        display = "该组合不可达"
        band_pct = None
    elif (
        mid_res["status"] == "无需 bull 即达标"
        and (mid_res["raw"] is not None and mid_res["raw"] <= EPSILON)
        and lo_res.get("no_bull_needed")
    ):
        display = "无需 bull 即达标"
        band_pct = (0, 0)
    elif not lo_res["reachable"] or not mid_res["reachable"]:
        # At least one conservative endpoint (all_lo or mid) is unreachable: never clamp to 1 - b (§4, Codex 14)
        fav_val = hi_res.get("clamped")
        if fav_val is None:
            fav_val = mid_res.get("clamped") or 0.0
        lo_pct, _ = round_outward_5pct(float(fav_val), float(fav_val))
        display = f"Bull >= {lo_pct}%–该组合不可达"
        band_pct = None
    else:
        reachable_vals = [
            float(r["clamped"])
            for r in (mid_res, lo_res, hi_res)
            if r.get("clamped") is not None
        ]
        r_min = min(reachable_vals) if reachable_vals else 0.0
        r_max = max(reachable_vals) if reachable_vals else 0.0
        band_pct = round_outward_5pct(r_min, r_max)
        if band_pct[0] == band_pct[1]:
            display = f"Bull >= {band_pct[0]}%"
        else:
            display = f"Bull >= {band_pct[0]}%–{band_pct[1]}%"

    return {
        "side": "long",
        "b": b,
        "note": "测试权重，不是预测",
        "mid": mid_res,
        "all_lo": lo_res,
        "all_hi": hi_res,
        "band_pct": list(band_pct) if band_pct is not None else None,
        "display": display,
    }


def compute_d_req_row(
    b: float,
    price_d: dict[str, float],
    price_m: dict[str, float],
    price_u: dict[str, float],
    h_s: float,
) -> dict[str, Any]:
    """Compute Short `d_req(b)` at price midpoints and `(all_lo, all_hi)` endpoints with 5% outward rounding (§12.1, §12.2)."""
    mid_res = _solve_d_req_single(b, price_d["mid"], price_m["mid"], price_u["mid"], h_s)
    lo_res = _solve_d_req_single(b, price_d["lo"], price_m["lo"], price_u["lo"], h_s)
    hi_res = _solve_d_req_single(b, price_d["hi"], price_m["hi"], price_u["hi"], h_s)

    if not mid_res["reachable"] and not lo_res["reachable"]:
        display = "该组合不可达"
        band_pct: tuple[int, int] | None = None
    elif (
        mid_res["status"] == "无需 bear 即达标"
        and (mid_res["raw"] is not None and mid_res["raw"] <= EPSILON)
        and hi_res.get("no_bear_needed")
    ):
        display = "无需 bear 即达标"
        band_pct = (0, 0)
    elif not hi_res["reachable"] or not mid_res["reachable"]:
        # Upper price endpoint (all_hi or mid) is unreachable for Short: never clamp to 1 - b (§4, Codex 14)
        fav_val = lo_res.get("clamped")
        if fav_val is None:
            fav_val = mid_res.get("clamped") or 0.0
        lo_pct, _ = round_outward_5pct(float(fav_val), float(fav_val))
        display = f"Bear >= {lo_pct}%–该组合不可达"
        band_pct = None
    else:
        reachable_vals = [
            float(r["clamped"])
            for r in (mid_res, lo_res, hi_res)
            if r.get("clamped") is not None
        ]
        r_min = min(reachable_vals) if reachable_vals else 0.0
        r_max = max(reachable_vals) if reachable_vals else 0.0
        band_pct = round_outward_5pct(r_min, r_max)
        if band_pct[0] == band_pct[1]:
            display = f"Bear >= {band_pct[0]}%"
        else:
            display = f"Bear >= {band_pct[0]}%–{band_pct[1]}%"

    return {
        "side": "short",
        "b": b,
        "note": "测试权重，不是预测",
        "mid": mid_res,
        "all_lo": lo_res,
        "all_hi": hi_res,
        "band_pct": list(band_pct) if band_pct is not None else None,
        "display": display,
    }


def compute_breakeven_table(
    v_tr_d: dict[str, float],
    v_tr_m: dict[str, float],
    v_tr_u: dict[str, float],
    *,
    h_l: float,
    h_s: float,
    short_price_d: dict[str, float] | None = None,
    short_price_m: dict[str, float] | None = None,
    short_price_u: dict[str, float] | None = None,
    b_rows: tuple[float, ...] = STANDARD_B_ROWS,
) -> dict[str, Any]:
    """Compute both `long` (`u_req(b)` on `v_tr`) and `short` (`d_req(b)` on price-only) breakeven tables across `b_rows`."""
    sp_d = short_price_d if short_price_d is not None else v_tr_d
    sp_m = short_price_m if short_price_m is not None else v_tr_m
    sp_u = short_price_u if short_price_u is not None else v_tr_u
    return {
        "note": "测试权重，不是预测",
        "long": [compute_u_req_row(b, v_tr_d, v_tr_m, v_tr_u, h_l) for b in b_rows],
        "short": [compute_d_req_row(b, sp_d, sp_m, sp_u, h_s) for b in b_rows],
    }


def compute_weight_polytope_vertices(weight_bounds: dict[str, Any]) -> dict[str, Any]:
    """Enumerate vertices of `{(d, b, u) in [lo, hi]^3 : d + b + u == 1}` (§3.3, §12.4)."""
    errors: list[str] = []
    bounds: dict[str, tuple[float, float]] = {}
    for key in ("bear", "base", "bull"):
        raw = weight_bounds.get(key)
        if not isinstance(raw, dict) or "lo" not in raw or "hi" not in raw:
            errors.append(f"`underwriting.weight_bounds.{key}` 必须包含 `lo` 与 `hi`")
            continue
        lo = float(raw["lo"])
        hi = float(raw["hi"])
        if lo < -EPSILON or hi > 1.0 + EPSILON or lo > hi + EPSILON:
            errors.append(
                f"`underwriting.weight_bounds.{key}`（[{lo}, {hi}]）必须满足 `0 <= lo <= hi <= 1`"
            )
        bounds[key] = (lo, hi)

    if errors:
        return {"feasible": False, "vertices": [], "errors": errors}

    d_lo, d_hi = bounds["bear"]
    b_lo, b_hi = bounds["base"]
    u_lo, u_hi = bounds["bull"]

    sum_lo = d_lo + b_lo + u_lo
    sum_hi = d_hi + b_hi + u_hi
    if sum_lo > 1.0 + 1e-6 or sum_hi < 1.0 - 1e-6:
        errors.append(
            f"`underwriting.weight_bounds` 在 `bear + base + bull = 1` 约束下无可行解"
            f"（下限之和为 {sum_lo:.4f}，上限之和为 {sum_hi:.4f}），"
            "请调整各情景权重区间使下限之和 `<= 1` 且上限之和 `>= 1`"
        )
        return {"feasible": False, "vertices": [], "errors": errors}

    candidates: list[tuple[float, float, float]] = []
    for d in (d_lo, d_hi):
        for b in (b_lo, b_hi):
            u = 1.0 - d - b
            if u_lo - 1e-7 <= u <= u_hi + 1e-7:
                candidates.append((d, b, min(max(u, u_lo), u_hi)))
        for u in (u_lo, u_hi):
            b = 1.0 - d - u
            if b_lo - 1e-7 <= b <= b_hi + 1e-7:
                candidates.append((d, min(max(b, b_lo), b_hi), u))
    for b in (b_lo, b_hi):
        for u in (u_lo, u_hi):
            d = 1.0 - b - u
            if d_lo - 1e-7 <= d <= d_hi + 1e-7:
                candidates.append((min(max(d, d_lo), d_hi), b, u))

    unique: list[dict[str, float]] = []
    for d, b, u in candidates:
        if not any(
            abs(v["bear"] - d) <= 1e-6 and abs(v["base"] - b) <= 1e-6 and abs(v["bull"] - u) <= 1e-6
            for v in unique
        ):
            unique.append({"bear": d, "base": b, "bull": u})

    if not unique:
        errors.append("`underwriting.weight_bounds` 无法同时满足 `bear + base + bull = 1`：三个区间组合不出一组合计为 1 的权重")
        return {"feasible": False, "vertices": [], "errors": errors}

    return {"feasible": True, "vertices": unique, "errors": []}


def evaluate_weight_bounds(
    weight_bounds: dict[str, Any],
    v_tr_d: dict[str, float],
    v_tr_m: dict[str, float],
    v_tr_u: dict[str, float],
    *,
    h_l: float,
    h_s: float,
    short_price_d: dict[str, float] | None = None,
    short_price_m: dict[str, float] | None = None,
    short_price_u: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate terminal expected value across all vertices of `weight_bounds` for Long (`v_tr`) and Short (price-only) (§12.1, §12.2, §12.4)."""
    poly = compute_weight_polytope_vertices(weight_bounds)
    if not poly["feasible"]:
        return {
            "feasible": False,
            "vertices": [],
            "errors": poly["errors"],
            "long": {"all_pass": False, "any_pass": False, "min_ev": None, "max_ev": None},
            "short": {"all_pass": False, "any_pass": False, "min_ev": None, "max_ev": None},
        }

    sp_d = short_price_d if short_price_d is not None else v_tr_d
    sp_m = short_price_m if short_price_m is not None else v_tr_m
    sp_u = short_price_u if short_price_u is not None else v_tr_u

    evs_long: list[float] = []
    evs_short: list[float] = []
    vertex_details: list[dict[str, float]] = []
    for v in poly["vertices"]:
        ev_l = (
            v["bear"] * v_tr_d["mid"]
            + v["base"] * v_tr_m["mid"]
            + v["bull"] * v_tr_u["mid"]
        )
        ev_s = (
            v["bear"] * sp_d["mid"]
            + v["base"] * sp_m["mid"]
            + v["bull"] * sp_u["mid"]
        )
        evs_long.append(ev_l)
        evs_short.append(ev_s)
        vertex_details.append({**v, "ev_mid": ev_l, "ev_long": ev_l, "ev_short": ev_s})

    min_ev_l = min(evs_long)
    max_ev_l = max(evs_long)
    min_ev_s = min(evs_short)
    max_ev_s = max(evs_short)

    return {
        "feasible": True,
        "vertices": vertex_details,
        "errors": [],
        "long": {
            "all_pass": min_ev_l >= h_l - 1e-6,
            "any_pass": max_ev_l >= h_l - 1e-6,
            "min_ev": min_ev_l,
            "max_ev": max_ev_l,
            "hurdle": h_l,
        },
        "short": {
            "all_pass": max_ev_s <= h_s + 1e-6,
            "any_pass": min_ev_s <= h_s + 1e-6,
            "min_ev": min_ev_s,
            "max_ev": max_ev_s,
            "hurdle": h_s,
        },
    }


def compute_adverse_move(
    stance: str,
    p0: float,
    bear_v_tr_lo: float | None = None,
    bull_price_hi: float | None = None,
    max_adverse_move_act_now: float = 0.25,
    *,
    bear_price_lo: float | None = None,
    bear_cash_return: float = 0.0,
    bear_price_mid: float | None = None,
    bull_price_mid: float | None = None,
) -> dict[str, Any]:
    """Compute directional adverse move at unfavorable state band endpoint vs `P0` (§12.1).

    - Long checks bear lower endpoint including cash return: `(V_D.lo + cash_return - P0) / P0 >= -max_adverse_move_act_now`
    - Short checks bull upper price endpoint: `(V_U.hi - P0) / P0 <= max_adverse_move_act_now`
    """
    if p0 <= 0:
        return {"stance": stance, "adverse_move": math.inf, "ok_for_act_now": False}
    if stance == "long":
        if bear_v_tr_lo is not None:
            eff_bear_lo = float(bear_v_tr_lo) + float(bear_cash_return)
        elif bear_price_lo is not None:
            eff_bear_lo = float(bear_price_lo) + float(bear_cash_return)
        elif bear_price_mid is not None:
            eff_bear_lo = float(bear_price_mid) + float(bear_cash_return)
        else:
            eff_bear_lo = 0.0
        move = (p0 - eff_bear_lo) / p0
        return {
            "stance": "long",
            "adverse_move": move,
            "net_return_at_endpoint": (eff_bear_lo - p0) / p0,
            "max_adverse_move_act_now": max_adverse_move_act_now,
            "ok_for_act_now": move <= max_adverse_move_act_now + EPSILON,
        }
    if stance == "short":
        eff_bull_hi = (
            float(bull_price_hi)
            if bull_price_hi is not None
            else (float(bull_price_mid) if bull_price_mid is not None else math.inf)
        )
        move = (eff_bull_hi - p0) / p0
        return {
            "stance": "short",
            "adverse_move": move,
            "net_return_at_endpoint": (p0 - eff_bull_hi) / p0,
            "max_adverse_move_act_now": max_adverse_move_act_now,
            "ok_for_act_now": move <= max_adverse_move_act_now + EPSILON,
        }
    return {
        "stance": stance,
        "adverse_move": 0.0,
        "max_adverse_move_act_now": max_adverse_move_act_now,
        "ok_for_act_now": False,
    }


def check_entry_band(
    stance: str,
    entry_band: dict[str, Any] | None,
    p0: float,
    bear_price_lo: float,
    bull_price_hi: float,
    max_entry_distance: float = 0.25,
) -> dict[str, Any]:
    """Validate `entry_band` directional bounds and distance to anchor price `P0` (§2.3, §12.1, §12.4)."""
    if not isinstance(entry_band, dict) or "lo" not in entry_band or "hi" not in entry_band:
        return {
            "present": False,
            "ok": False,
            "errors": [
                "当 `house_view.actionability` 为 `wait_price` 时，必须提供包含 `lo` 与 `hi` 的 `house_view.entry_band`"
            ],
        }

    stats = compute_range_stats(entry_band)
    lo, hi, mid = stats["lo"], stats["hi"], stats["mid"]
    errors: list[str] = []
    if lo > hi + EPSILON:
        errors.append(f"`house_view.entry_band.lo`（{lo}）不能大于 `house_view.entry_band.hi`（{hi}）")
    if p0 <= 0:
        errors.append("`underwriting.anchor.price` 必须大于 0")
        distance = math.inf
    else:
        distance = abs(mid - p0) / p0
        if distance > max_entry_distance + EPSILON:
            errors.append(
                f"`house_view.entry_band` 中点与基准股价 `underwriting.anchor.price` 的偏离度为 {distance:.2%}，超过上限 {max_entry_distance:.0%}"
            )

    if stance == "long":
        if not (hi < p0 - EPSILON):
            errors.append(
                f"`house_view.stance` 为 `long` 时，`house_view.entry_band.hi`（{hi}）必须严格低于基准股价 `underwriting.anchor.price`（{p0}）"
            )
        if not (lo >= bear_price_lo - EPSILON):
            errors.append(
                f"`house_view.stance` 为 `long` 时，`house_view.entry_band.lo`（{lo}）不能低于 `bear` 情景目标股价下限（{bear_price_lo}）"
            )
    elif stance == "short":
        if not (lo > p0 + EPSILON):
            errors.append(
                f"`house_view.stance` 为 `short` 时，`house_view.entry_band.lo`（{lo}）必须严格高于基准股价 `underwriting.anchor.price`（{p0}）"
            )
        if not (hi <= bull_price_hi + EPSILON):
            errors.append(
                f"`house_view.stance` 为 `short` 时，`house_view.entry_band.hi`（{hi}）不能高于 `bull` 情景目标股价上限（{bull_price_hi}）"
            )
    elif stance == "neutral":
        if not (lo >= bear_price_lo - EPSILON):
            errors.append(
                f"`house_view.stance` 为 `neutral` 时，`house_view.entry_band.lo`（{lo}）不能低于 `bear` 情景目标股价下限（{bear_price_lo}）"
            )
        if not (hi <= bull_price_hi + EPSILON):
            errors.append(
                f"`house_view.stance` 为 `neutral` 时，`house_view.entry_band.hi`（{hi}）不能高于 `bull` 情景目标股价上限（{bull_price_hi}）"
            )

    return {
        "present": True,
        "lo": lo,
        "hi": hi,
        "mid": mid,
        "distance_to_p0": distance,
        "ok": len(errors) == 0,
        "errors": errors,
    }


def compute_multiple_share(
    p0: float,
    bull_band: dict[str, Any] | None = None,
    base_band: dict[str, Any] | None = None,
    bear_band: dict[str, Any] | None = None,
    *,
    stance: str = "long",
    anchor_eps_ref: float | None = None,
    anchor_multiple: float | None = None,
) -> dict[str, Any]:
    """Compute EPS vs valuation multiple log-return decomposition (§2.2, §12.3).

    Reference EPS (`EPS_ref`) is the base state's midpoint EPS (`base.eps.mid`),
    so `M0 = P0 / EPS_ref` and `M_s = P_s_mid / EPS_s_mid`.
    - Long (bull): `ln(P_bull / P0) = ln(EPS_bull / EPS_ref) + ln(M_bull / M0)` when `P_bull > P0`.
    - Short (bear mirror): `ln(P_bear / P0) = ln(EPS_bear / EPS_ref) + ln(M_bear / M0)` when `P_bear < P0`.
    """
    m0: float | None = None
    eps_ref: float | None = None
    if isinstance(base_band, dict) and base_band.get("method") == "eps_pe" and isinstance(base_band.get("eps"), dict):
        base_eps_mid = _to_float(base_band["eps"].get("mid"), 0.0)
        if base_eps_mid > 0 and p0 > 0:
            eps_ref = base_eps_mid
            m0 = p0 / eps_ref
    if (m0 is None or eps_ref is None) and anchor_eps_ref is not None and anchor_eps_ref > 0 and p0 > 0:
        eps_ref = float(anchor_eps_ref)
        m0 = p0 / eps_ref
    elif (m0 is None or eps_ref is None) and anchor_multiple is not None and anchor_multiple > 0 and p0 > 0:
        m0 = float(anchor_multiple)
        eps_ref = p0 / m0

    if m0 is None or eps_ref is None or m0 <= 0 or eps_ref <= 0 or p0 <= 0:
        return {
            "status": "skipped_missing_reference",
            "eps_ref": None,
            "m0": None,
            "multiple_share": None,
            "multiple_share_bull": None,
            "eps_share_bull": None,
            "multiple_share_bear": None,
            "eps_share_bear": None,
            "tag": None,
        }

    mult_share_bull: float | None = None
    eps_share_bull: float | None = None
    m_bull_eff: float | None = None
    eps_bull_mid: float | None = None
    bull_tag: str | None = None
    bull_status = "skipped_non_positive_or_reference"

    if isinstance(bull_band, dict) and bull_band.get("method") == "eps_pe" and isinstance(bull_band.get("eps"), dict):
        eps_bull_mid = _to_float(bull_band["eps"].get("mid"), 0.0)
        p_bull_mid = _to_float((bull_band.get("price") or {}).get("mid"), 0.0)
        if eps_bull_mid > 0 and p_bull_mid > 0:
            m_bull_eff = p_bull_mid / eps_bull_mid
            if p_bull_mid > p0 + EPSILON:
                ln_p = math.log(p_bull_mid / p0)
                ln_m = math.log(m_bull_eff / m0)
                ln_e = math.log(eps_bull_mid / eps_ref)
                mult_share_bull = ln_m / ln_p
                eps_share_bull = ln_e / ln_p
                bull_status = "ok"
                if mult_share_bull > 0.5 + EPSILON:
                    bull_tag = "bull 主要靠拔估值"
            else:
                bull_status = "skipped_non_positive_price_return"
                if m_bull_eff > m0 + EPSILON:
                    bull_tag = "EPS 下滑被拔估值抵消"

    mult_share_bear: float | None = None
    eps_share_bear: float | None = None
    m_bear_eff: float | None = None
    eps_bear_mid: float | None = None
    bear_tag: str | None = None
    bear_status = "skipped_non_positive_or_reference"

    if isinstance(bear_band, dict) and bear_band.get("method") == "eps_pe" and isinstance(bear_band.get("eps"), dict):
        eps_bear_mid = _to_float(bear_band["eps"].get("mid"), 0.0)
        p_bear_mid = _to_float((bear_band.get("price") or {}).get("mid"), 0.0)
        if eps_bear_mid > 0 and p_bear_mid > 0:
            m_bear_eff = p_bear_mid / eps_bear_mid
            if p_bear_mid < p0 - EPSILON:
                ln_p_b = math.log(p_bear_mid / p0)
                ln_m_b = math.log(m_bear_eff / m0)
                ln_e_b = math.log(eps_bear_mid / eps_ref)
                mult_share_bear = ln_m_b / ln_p_b
                eps_share_bear = ln_e_b / ln_p_b
                bear_status = "ok"
                if mult_share_bear > 0.5 + EPSILON:
                    bear_tag = "bear 主要靠杀估值"
            else:
                bear_status = "skipped_non_negative_price_return"

    if stance == "short":
        active_share = mult_share_bear
        active_tag = bear_tag
        active_status = bear_status if bear_band is not None else bull_status
    else:
        active_share = mult_share_bull
        active_tag = bull_tag
        active_status = bull_status

    return {
        "status": active_status,
        "m0": m0,
        "eps_ref": eps_ref,
        "m_bull_eff": m_bull_eff,
        "eps_bull_mid": eps_bull_mid,
        "multiple_share": active_share,
        "multiple_share_bull": mult_share_bull,
        "eps_share_bull": eps_share_bull,
        "m_bear_eff": m_bear_eff,
        "eps_bear_mid": eps_bear_mid,
        "multiple_share_bear": mult_share_bear,
        "eps_share_bear": eps_share_bear,
        "tag": active_tag,
    }


def _parse_iso_date(val: Any) -> date | None:
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip()
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_fy_end_month_day(fiscal_year_end: str | None) -> tuple[int, int]:
    """Parse `MM-DD` fiscal year end string (defaulting to `(12, 31)`)."""
    if isinstance(fiscal_year_end, str):
        m = re.match(r"^(\d{2})-(\d{2})$", fiscal_year_end.strip())
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return (mm, dd)
    return (12, 31)


def _parse_period_end_date(period_str: str, fiscal_year_end: str | None = None) -> date | None:
    """Resolve the end date of `period_str` (e.g. 'FY2027', '2027-09-30', '2027-09', '4Q26') (§12.2)."""
    s = period_str.strip().upper()
    if not s or s in ("NTM", "LTM", "TTM", "FWD", "SPOT"):
        return None

    exact = _parse_iso_date(s)
    if exact is not None:
        return exact

    # YYYY-MM or LTM-YYYY-MM or FY2027-09
    m_ym = re.match(r"^(?:LTM-|FY)?(\d{4})-(\d{2})$", s)
    if m_ym:
        yr, mm = int(m_ym.group(1)), int(m_ym.group(2))
        if 1 <= mm <= 12:
            return date(yr, mm, calendar.monthrange(yr, mm)[1])

    # Quarter pattern like 1Q27, 4Q2026, Q4 2026, FY27Q4
    m_q = re.match(r"^(?:([1-4])Q|Q([1-4]))[\s\-]*(?:FY)?(\d{2}|\d{4})E?$", s)
    if m_q:
        q = int(m_q.group(1) or m_q.group(2))
        yr = int(m_q.group(3))
        if yr < 100:
            yr += 2000
        end_month = q * 3
        return date(yr, end_month, calendar.monthrange(yr, end_month)[1])

    # Fiscal/calendar year pattern like FY2027, FY27, CY2027, 2027E, 2027
    m_yr = re.match(r"^(FY|CY)?(\d{2}|\d{4})E?$", s)
    if m_yr:
        prefix = m_yr.group(1) or "FY"
        yr = int(m_yr.group(2))
        if yr < 100:
            yr += 2000
        if prefix == "CY":
            return date(yr, 12, 31)
        mm, dd = _parse_fy_end_month_day(fiscal_year_end)
        max_d = calendar.monthrange(yr, mm)[1]
        return date(yr, mm, min(dd, max_d))

    return None


def _add_months(d: date, months: int) -> date:
    total_months = (d.year * 12 + (d.month - 1)) + int(months)
    ny = total_months // 12
    nm = (total_months % 12) + 1
    max_day = calendar.monthrange(ny, nm)[1]
    return date(ny, nm, min(d.day, max_day))


def check_period_alignment(
    states_list: list[dict[str, Any]],
    anchor_price_at: str | None,
    horizon_months: int | None,
    *,
    fiscal_year_end: str | None = None,
) -> dict[str, Any]:
    """Check `calc.period` alignment with `[anchor.price_at + T, anchor.price_at + T + 12m]` (§12.2).

    Period misalignment is a warning (`period_misaligned`), never a hard rejection.
    """
    eps_periods: list[tuple[str, str, str | None]] = []
    for st in states_list:
        if not isinstance(st, dict):
            continue
        calc = st.get("calc") if isinstance(st.get("calc"), dict) else {}
        if calc.get("method") == "eps_pe":
            p = str(calc.get("period") or "").strip()
            st_fye = str(calc.get("fiscal_year_end") or "").strip() or fiscal_year_end
            eps_periods.append((str(st.get("name") or ""), p, st_fye))

    warnings: list[str] = []
    distinct_periods = {p for _, p, _ in eps_periods if p}
    if len(distinct_periods) > 1:
        warnings.append(
            f"period_misaligned: eps_pe states have inconsistent calc.period values: {[(n, p) for n, p, _ in eps_periods]}"
        )

    if anchor_price_at and horizon_months is not None:
        anchor_d = _parse_iso_date(anchor_price_at)
        if anchor_d is not None:
            target_d = _add_months(anchor_d, int(horizon_months))
            window_end = _add_months(anchor_d, int(horizon_months) + 12)
            for st_name, period_str, st_fye in eps_periods:
                if not period_str:
                    continue
                fy_end = _parse_period_end_date(period_str, fiscal_year_end=st_fye)
                if fy_end is not None and not (target_d <= fy_end <= window_end):
                    warnings.append(
                        f"period_misaligned: state[{st_name}] calc.period {period_str!r} (end {fy_end.isoformat()}) "
                        f"falls outside forward window [{target_d.isoformat()}, {window_end.isoformat()}]"
                    )

    return {
        "ok": len(warnings) == 0,
        "warnings": warnings,
        "errors": warnings,
        "periods": [p for _, p, _ in eps_periods],
    }


def check_anchor_staleness(
    anchor: dict[str, Any] | None,
    *,
    now_date: str | date | None = None,
    current_price: float | None = None,
    anchor_stale_days: int = 14,
    anchor_stale_move: float = 0.15,
) -> dict[str, Any]:
    """Evaluate anchor time and price-drift staleness against today's date (`now_date`) and `current_price` (§12.8)."""
    if not isinstance(anchor, dict):
        return {"is_stale": False, "reasons": [], "days_elapsed": None, "price_move": None}

    reasons: list[str] = []
    days_elapsed: int | None = None
    price_move: float | None = None

    anchor_d = _parse_iso_date(anchor.get("price_at"))
    if now_date is None:
        ref_d: date | None = datetime.now(timezone.utc).date()
    elif isinstance(now_date, date) and not isinstance(now_date, datetime):
        ref_d = now_date
    else:
        ref_d = _parse_iso_date(now_date)
    if anchor_d is not None and ref_d is not None:
        days_elapsed = (ref_d - anchor_d).days
        if days_elapsed > int(anchor_stale_days):
            reasons.append(
                f"anchor_age_exceeded: {days_elapsed} days > {anchor_stale_days} days"
            )

    p0 = _to_float(anchor.get("price"), 0.0)
    if current_price is not None and current_price > 0 and p0 > 0:
        price_move = abs(float(current_price) - p0) / p0
        if price_move > float(anchor_stale_move) + EPSILON:
            reasons.append(
                f"anchor_price_move_exceeded: {price_move:.2%} > {anchor_stale_move:.2%}"
            )

    return {
        "is_stale": len(reasons) > 0,
        "label": "需要重估" if reasons else None,
        "reasons": reasons,
        "days_elapsed": days_elapsed,
        "price_move": price_move,
    }


def resolve_hurdles(
    underwriting: dict[str, Any] | None,
    mandate_overrides: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Resolve effective hurdle parameters from `underwriting.hurdles` snapshot or defaults (§5, §12.9)."""
    defaults: dict[str, float] = {
        "long_hurdle_annual": 0.15,
        "short_hurdle_annual": 0.20,
        "min_total_return": 0.10,
        "max_adverse_move_act_now": 0.25,
        "max_entry_distance": 0.25,
        "call_hurdle_annual": 0.15,
        "anchor_stale_days": 14.0,
        "anchor_stale_move": 0.15,
    }
    if isinstance(underwriting, dict) and isinstance(underwriting.get("hurdles"), dict):
        snap = underwriting["hurdles"]
        for k in (
            "long_hurdle_annual",
            "short_hurdle_annual",
            "min_total_return",
            "max_adverse_move_act_now",
            "horizon_months",
        ):
            if snap.get(k) is not None:
                defaults[k] = float(snap[k])
        if "max_adverse_move_act_now" not in snap and snap.get("max_bear_loss_act_now") is not None:
            defaults["max_adverse_move_act_now"] = abs(float(snap["max_bear_loss_act_now"]))
    if isinstance(mandate_overrides, dict):
        for k, v in mandate_overrides.items():
            if v is not None and (k in defaults or k == "horizon_months"):
                defaults[k] = float(v)
        if (
            "max_adverse_move_act_now" not in mandate_overrides
            and mandate_overrides.get("max_bear_loss_act_now") is not None
        ):
            defaults["max_adverse_move_act_now"] = abs(float(mandate_overrides["max_bear_loss_act_now"]))
    return defaults


def evaluate_underwriting(
    thesis_or_underwriting: dict[str, Any],
    *,
    stance: str | None = None,
    horizon_months: int | None = None,
    actionability: str | None = None,
    entry_band: dict[str, Any] | None = None,
    current_price: float | None = None,
    now_date: str | date | None = None,
    fiscal_year_end: str | None = None,
    mandate_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run complete scenario math evaluation on a thesis document or underwriting block (§4, §12)."""
    if "underwriting" in thesis_or_underwriting and isinstance(
        thesis_or_underwriting.get("underwriting"), dict
    ):
        uw = thesis_or_underwriting["underwriting"]
        hv = (
            thesis_or_underwriting.get("house_view")
            if isinstance(thesis_or_underwriting.get("house_view"), dict)
            else {}
        )
        stance = stance if stance is not None else str(hv.get("stance") or "no_view")
        snap_hm = (
            int(uw["hurdles"]["horizon_months"])
            if isinstance(uw.get("hurdles"), dict) and uw["hurdles"].get("horizon_months") is not None
            else None
        )
        horizon_months = (
            horizon_months
            if horizon_months is not None
            else (
                snap_hm
                if snap_hm is not None
                else (int(hv["horizon_months"]) if hv.get("horizon_months") is not None else 12)
            )
        )
        actionability = actionability if actionability is not None else hv.get("actionability")
        entry_band = entry_band if entry_band is not None else hv.get("entry_band")
        if fiscal_year_end is None:
            fiscal_year_end = uw.get("fiscal_year_end") or thesis_or_underwriting.get("fiscal_year_end")
    else:
        uw = thesis_or_underwriting
        stance = stance or "long"
        snap_hm = (
            int(uw["hurdles"]["horizon_months"])
            if isinstance(uw.get("hurdles"), dict) and uw["hurdles"].get("horizon_months") is not None
            else None
        )
        horizon_months = horizon_months if horizon_months is not None else (snap_hm if snap_hm is not None else 12)
        if fiscal_year_end is None:
            fiscal_year_end = uw.get("fiscal_year_end")

    hurdles = resolve_hurdles(uw, mandate_overrides)
    anchor = uw.get("anchor") if isinstance(uw.get("anchor"), dict) else {}
    p0 = _to_float(anchor.get("price"), 0.0)
    errors: list[str] = []
    warnings: list[str] = []
    if p0 <= 0:
        errors.append(f"`underwriting.anchor.price` 必须大于 0（当前为 {anchor.get('price')!r}）")

    raw_states = uw.get("states") if isinstance(uw.get("states"), list) else []
    states_by_name: dict[str, dict[str, Any]] = {}
    for st in raw_states:
        if isinstance(st, dict) and st.get("name"):
            band_info = compute_state_band(st)
            states_by_name[str(st["name"])] = band_info
            errors.extend(band_info["errors"])

    stress_band: dict[str, Any] | None = None
    raw_stress = uw.get("stress")
    if isinstance(raw_stress, dict) and isinstance(raw_stress.get("calc"), dict):
        stress_band = compute_state_band({"name": "stress", **raw_stress}, check_width=False)
        errors.extend(stress_band["errors"])

    order_check = validate_states_order(states_by_name)
    errors.extend(order_check["errors"])
    warnings.extend(order_check.get("warnings") or [])

    period_check = check_period_alignment(
        raw_states,
        anchor.get("price_at"),
        horizon_months,
        fiscal_year_end=fiscal_year_end,
    )
    warnings.extend(period_check["warnings"])

    raw_borrow_cost = uw.get("borrow_cost_annual")
    raw_dividend_yield = uw.get("dividend_yield")
    short_cost_known = raw_borrow_cost is not None and raw_dividend_yield is not None
    short_cost_note = "未计成本" if not short_cost_known else None
    borrow_cost = _to_float(raw_borrow_cost, 0.0) if raw_borrow_cost is not None else 0.0
    dividend_yield = _to_float(raw_dividend_yield, 0.0) if raw_dividend_yield is not None else 0.0

    h_l = compute_long_hurdle(
        p0,
        horizon_months,
        long_hurdle_annual=hurdles["long_hurdle_annual"],
        min_total_return=hurdles["min_total_return"],
    )
    h_s = compute_short_hurdle(
        p0,
        horizon_months,
        short_hurdle_annual=hurdles["short_hurdle_annual"],
        borrow_cost_annual=borrow_cost,
        dividend_yield=dividend_yield,
    )

    breakeven: dict[str, Any] = {
        "note": "测试权重，不是预测",
        "short_cost_note": short_cost_note,
        "long": [],
        "short": [],
    }
    weight_eval: dict[str, Any] | None = None
    adverse_move: dict[str, Any] | None = None
    entry_check: dict[str, Any] | None = None
    multiple_decomp: dict[str, Any] | None = None
    no_bull_needed_at_05 = False
    no_bear_needed_at_05 = False
    no_bull_needed_all_lo = False
    no_bear_needed_all_hi = False

    if all(k in states_by_name for k in ("bear", "base", "bull")) and p0 > 0:
        bear_s = states_by_name["bear"]
        base_s = states_by_name["base"]
        bull_s = states_by_name["bull"]

        breakeven = compute_breakeven_table(
            bear_s["v_tr"],
            base_s["v_tr"],
            bull_s["v_tr"],
            h_l=h_l,
            h_s=h_s,
            short_price_d=bear_s["price"],
            short_price_m=base_s["price"],
            short_price_u=bull_s["price"],
        )
        breakeven["short_cost_note"] = short_cost_note
        for row in breakeven["long"]:
            if abs(row["b"] - 0.50) <= EPSILON and row["mid"]["no_bull_needed"]:
                no_bull_needed_at_05 = True
        for row in breakeven["short"]:
            if abs(row["b"] - 0.50) <= EPSILON and row["mid"]["no_bear_needed"]:
                no_bear_needed_at_05 = True

        # §12.3 Path 2: both b=25% and b=50% pass u=0 / d=0 at conservative endpoints (all_lo for long, all_hi for short)
        no_bull_needed_all_lo = bool(
            breakeven["long"]
            and all(bool(r["all_lo"].get("no_bull_needed")) for r in breakeven["long"])
        )
        no_bear_needed_all_hi = bool(
            breakeven["short"]
            and all(bool(r["all_hi"].get("no_bear_needed")) for r in breakeven["short"])
        )

        wb = uw.get("weight_bounds")
        if isinstance(wb, dict):
            weight_eval = evaluate_weight_bounds(
                wb,
                bear_s["v_tr"],
                base_s["v_tr"],
                bull_s["v_tr"],
                h_l=h_l,
                h_s=h_s,
                short_price_d=bear_s["price"],
                short_price_m=base_s["price"],
                short_price_u=bull_s["price"],
            )
            errors.extend(weight_eval["errors"])

        adverse_move = compute_adverse_move(
            stance,
            p0,
            bear_s["v_tr"]["lo"],
            bull_s["price"]["hi"],
            max_adverse_move_act_now=hurdles["max_adverse_move_act_now"],
        )

        if entry_band is not None or actionability == "wait_price":
            entry_check = check_entry_band(
                stance,
                entry_band,
                p0,
                bear_s["price"]["lo"],
                bull_s["price"]["hi"],
                max_entry_distance=hurdles["max_entry_distance"],
            )

        multiple_decomp = compute_multiple_share(
            p0,
            bull_s,
            base_s,
            bear_s,
            stance=stance,
            anchor_eps_ref=_to_float(anchor.get("eps_ref"), 0.0) or None,
            anchor_multiple=_to_float(anchor.get("multiple"), 0.0) or None,
        )

    staleness = check_anchor_staleness(
        anchor,
        now_date=now_date,
        current_price=current_price,
        anchor_stale_days=int(hurdles["anchor_stale_days"]),
        anchor_stale_move=hurdles["anchor_stale_move"],
    )

    return {
        "ok": len(errors) == 0,
        "stance": stance,
        "horizon_months": horizon_months,
        "anchor_price": p0,
        "hurdles": {
            **hurdles,
            "horizon_months": horizon_months,
            "H_L": h_l,
            "H_S": h_s,
            "borrow_cost_annual": float(raw_borrow_cost) if raw_borrow_cost is not None else None,
            "dividend_yield": float(raw_dividend_yield) if raw_dividend_yield is not None else None,
            "short_cost_known": short_cost_known,
            "short_cost_note": short_cost_note,
        },
        "states": states_by_name,
        "stress": stress_band,
        "order_check": order_check,
        "period_check": period_check,
        "breakeven": breakeven,
        "no_bull_needed_at_b50": no_bull_needed_at_05,
        "no_bear_needed_at_b50": no_bear_needed_at_05,
        "no_bull_needed_all_lo": no_bull_needed_all_lo,
        "no_bear_needed_all_hi": no_bear_needed_all_hi,
        "weight_bounds_eval": weight_eval,
        "adverse_move": adverse_move,
        "entry_band_check": entry_check,
        "multiple_decomposition": multiple_decomp,
        "multiple_share": multiple_decomp,
        "staleness": staleness,
        "warnings": warnings,
        "errors": errors,
    }


def _load_json_arg(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("{"):
        return json.loads(s)
    p = Path(s)
    return json.loads(p.read_text(encoding="utf-8"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.knowledge.scenario_math",
        description=(
            "Compute scenario underwriting price bands, terminal hurdles (H_L / H_S), "
            "breakeven weight tables (u_req / d_req), weight_bounds vertex checks, "
            "multiple expansion share, and anchor staleness (§4, §12)."
        ),
    )
    parser.add_argument(
        "--thesis",
        required=True,
        help="Thesis v2 JSON object (or underwriting JSON object), passed as an inline JSON string or path to a JSON file.",
    )
    parser.add_argument(
        "--stance",
        choices=["long", "short", "neutral", "no_view"],
        default=None,
        help="Optional override for house_view.stance.",
    )
    parser.add_argument(
        "--horizon-months",
        type=int,
        default=None,
        help="Optional override for house_view.horizon_months (1–60).",
    )
    parser.add_argument(
        "--current-price",
        type=float,
        default=None,
        help="Optional latest market price to check anchor price-drift staleness (>15%% move).",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Optional ISO date (YYYY-MM-DD) to check anchor age staleness (>14 days).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON output indentation (default: 2).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    payload = _load_json_arg(args.thesis)
    result = evaluate_underwriting(
        payload,
        stance=args.stance,
        horizon_months=args.horizon_months,
        current_price=args.current_price,
        now_date=args.now,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=args.indent) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
