"""Unit tests for Batch 4a (P-1): schema v2, scenario_math, consistency matrix, and odds card rendering."""

from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

import pytest

from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.knowledge.ops import apply_ops
from src.dalton.knowledge.render import render_agent_block, render_document_md
from src.dalton.knowledge.scenario_math import (
    STANDARD_B_ROWS,
    check_anchor_staleness,
    check_entry_band,
    check_period_alignment,
    compute_adverse_move,
    compute_d_req_row,
    compute_long_hurdle,
    compute_multiple_share,
    compute_short_hurdle,
    compute_state_band,
    compute_u_req_row,
    evaluate_underwriting,
    main as scenario_math_cli_main,
    round_outward_5pct,
    validate_states_order,
)
from src.dalton.knowledge.validate import validate_schema, validate_update
from src.dalton.settings import DaltonMandateSettings, get_dalton_mandate_settings


def _sample_v2_underwriting() -> dict:
    """Hand-calculable underwriting block:
    P0 = 100.0 (2026-10-15), T = 12m -> H_L = 115.0, H_S = 80.0.
    States (FY2027):
      - bear: eps [4.0, 4.2] * mult [19, 20] -> price [76.0, 84.0], mid = 80.0, width = 8/80 = 10.0% <= 25%
              (lo = 76.0 -> adverse move (76 - 100)/100 = -24% >= -25% for act_now)
      - base: eps [5.0, 5.5] * mult [20, 22] -> price [100.0, 121.0], mid = 110.5, width = 21/110.5 = 19.00% <= 25%
              (base.eps.mid = 5.25 -> M0 = 100 / 5.25 = 19.0476x)
      - bull: eps [6.4, 6.8] * mult [21, 22] -> price [134.4, 149.6], mid = 142.0, width = 15.2/142.0 = 10.70% <= 25%
    At b = 0.50:
      u_req(0.5) = (115.0 - 0.5 * 110.5 - 0.5 * 80.0) / (142.0 - 80.0)
                 = (115.0 - 55.25 - 40.0) / 62.0
                 = 19.75 / 62.0 ≈ 0.318548
    """
    return {
        "anchor": {
            "price": 100.0,
            "price_at": "2026-10-15",
            "currency": "USD",
            "source": "closing_price",
        },
        "scenario_version": 1,
        "states": [
            {
                "name": "bear",
                "condition": "广告提价受阻与折旧压制利润率",
                "debate_ids": ["D1"],
                "driver_ids": ["K1"],
                "calc": {
                    "method": "eps_pe",
                    "eps": {"lo": 4.0, "hi": 4.2, "unit": "USD"},
                    "multiple": {"lo": 19.0, "hi": 20.0, "unit": "x"},
                    "basis": "GAAP_PE",
                    "period": "FY2027",
                    "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
                },
                "cash_return_per_share": 0.0,
            },
            {
                "name": "base",
                "condition": "核心广告稳健增长，AI 投入按指引推进",
                "debate_ids": ["D1"],
                "driver_ids": ["K1"],
                "calc": {
                    "method": "eps_pe",
                    "eps": {"lo": 5.0, "hi": 5.5, "unit": "USD"},
                    "multiple": {"lo": 20.0, "hi": 22.0, "unit": "x"},
                    "basis": "GAAP_PE",
                    "period": "FY2027",
                    "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
                },
                "cash_return_per_share": 0.0,
            },
            {
                "name": "bull",
                "condition": "AI 转化率跳升驱动 EPS 与估值双击",
                "debate_ids": ["D1"],
                "driver_ids": ["K1"],
                "calc": {
                    "method": "eps_pe",
                    "eps": {"lo": 6.4, "hi": 6.8, "unit": "USD"},
                    "multiple": {"lo": 21.0, "hi": 22.0, "unit": "x"},
                    "basis": "GAAP_PE",
                    "period": "FY2027",
                    "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
                },
                "cash_return_per_share": 0.0,
            },
        ],
        "stress": {
            "calc": {
                "method": "reference",
                "price_range": {"lo": 55.0, "hi": 65.0, "unit": "USD"},
                "ref": "E-20261015-03",
                "note": "极端衰退下 12x 底部 EPS",
            },
            "condition": "北美广告预算同比下滑 15% 且监管罚款落地",
            "needs_financing": "no",
        },
        "protection": {
            "survival": "净现金 450 亿美元且自由现金流为正，无再融资压力",
            "needs_financing": "no",
            "value": "保守核心主业 15x EPS 估值区间",
            "value_range": {"lo": 72.0, "hi": 82.0, "unit": "USD"},
            "path": "董事会已授权 500 亿美元回购计划，下跌自动加速注销",
            "failure": "监管强制拆分核心广告资产或 FCF 转负",
        },
        "weight_bounds": {
            "bear": {"lo": 0.10, "hi": 0.15},
            "base": {"lo": 0.45, "hi": 0.50},
            "bull": {"lo": 0.35, "hi": 0.45},
            "rationale": "3Q 广告主调研显示转化率提升已兑现，Bull 权重至少 35%",
            "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
        },
        "breakeven_verdict": [
            {
                "side": "long",
                "verdict": "clearly_above",
                "b_row": 0.5,
                "basis": "中点下仅需 Bull 权重 32%，我方 Bull 权重下限 35% 在全部顶点均高于门槛",
                "debate_ids": ["D1"],
            }
        ],
    }


def _sample_v2_thesis() -> dict:
    return {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "one_liner": "AI 推荐提升时长与广告转化，驱动 FY27 EPS 超预期",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "horizon_months": 12,
            "actionability": "act_now",
            "entry_band": None,
            "binding_constraint": "none",
            "binding_constraint_text": None,
            "flip_conditions": {
                "upgrade_if": "4Q26 广告增速超 22%",
                "downgrade_if": "4Q26 广告增速跌破 12% 或 FY27 Capex 超 900 亿",
            },
            "trigger_refs": ["D1", "V-001"],
            "summary": "做多 META：12 个月三情景加权期望回报显著跨过 15% 门槛。",
            "variant_perception": "市场过度担忧 Capex 折旧，低估广告单价提升弹性。",
            "updated_at": "2026-10-18T12:00:00Z",
        },
        "house_view_history": [],
        "market_view": {
            "summary": "卖方一致预期 FY27 EPS 5.15 美元，买方交易台反馈仓位偏拥挤。",
            "as_of": "2026-10-18T12:00:00Z",
            "updated_at": "2026-10-18T12:00:00Z",
            "observations": [
                {
                    "claim": "买方普遍按 5.1–5.3 美元交易 FY27 EPS",
                    "who": "Tier-1 Long-only 与对冲基金调研汇总",
                    "as_of": "2026-10-15",
                    "sources": [{"ref": "E-20261015-02", "kind": "evidence"}],
                    "confidence": "medium",
                }
            ],
            "positioning": "crowded_long",
            "positioning_sources": [{"ref": "E-20261015-02", "kind": "evidence"}],
        },
        "underwriting": _sample_v2_underwriting(),
        "pillars": [
            {
                "id": "P1",
                "claim": "AI 推荐引擎拉动 Reels 时长与转化率",
                "priority": "core",
                "baseline": {"text": "2Q26 广告展示 +10%，单价 +10%", "as_of": "2026-07-31"},
                "expected_path": "FY27 保持双位数单价与转化提升",
                "thresholds": {
                    "confirm": [
                        {
                            "id": "T1",
                            "metric": "Ad impressions growth",
                            "operator": ">=",
                            "value": 8.0,
                            "unit": "pct",
                            "period": "3Q26",
                            "label": "estimate",
                        }
                    ],
                    "warn": [],
                    "break": [
                        {
                            "id": "T2",
                            "metric": "Ad impressions growth",
                            "operator": "<",
                            "value": 3.0,
                            "unit": "pct",
                            "period": "3Q26",
                            "label": "estimate",
                        }
                    ],
                },
                "status": "intact",
                "next_proof_point": {
                    "what": "3Q26 财报验证广告展示与单价增速",
                    "due": "2026-10-28",
                    "validation_id": "V-001",
                },
                "provenance": "draft",
                "created_at": "2026-10-15T10:00:00Z",
            }
        ],
        "evidence": [
            {
                "id": "E-20261015-01",
                "at": "2026-10-15T10:00:00Z",
                "period": "2Q26",
                "source": {
                    "type": "filing",
                    "identity": "META 2Q26 10-Q",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-Q", "period": "2Q26"},
                },
                "fact": "2Q26 广告营收同比增长 22%。",
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            },
            {
                "id": "E-20261015-02",
                "at": "2026-10-15T11:00:00Z",
                "period": "FY2027",
                "source": {
                    "type": "market_color",
                    "identity": "GS Prime Brokerage Positioning Note",
                    "tier": 4,
                    "ref": {"kind": "file", "path": "sources/gs_positioning.md"},
                },
                "fact": "TMT 对冲基金在 META 上的多头净敞口处于过去 12 个月 85% 分位。",
                "pillar_ids": [],
                "signal": "neutral",
                "quality": "medium",
            },
            {
                "id": "E-20261015-03",
                "at": "2026-10-15T12:00:00Z",
                "period": "FY2027",
                "source": {
                    "type": "model",
                    "identity": "META Valuation Model v1",
                    "tier": 2,
                    "ref": {"kind": "model_cell", "path": "model/META_dcf.xlsx", "cell": "Summary!G42"},
                },
                "fact": "冻结情景与压力测试计算记录：压力下 12x 底部 EPS 对应 55–65 美元。",
                "pillar_ids": ["P1"],
                "signal": "neutral",
                "quality": "high",
            },
        ],
        "official": None,
    }


def test_scenario_math_hand_calculated_formulas() -> None:
    """Verify every scenario_math formula against hand calculations (§4, §12.1–§12.3, §12.8)."""
    assert STANDARD_B_ROWS == (0.25, 0.50)

    # 1. State band endpoint multiplication & width <= 25%
    state = {
        "name": "base",
        "calc": {
            "method": "eps_pe",
            "eps": {"lo": 5.0, "hi": 5.5},
            "multiple": {"lo": 20.0, "hi": 22.0},
            "period": "FY2027",
        },
        "cash_return_per_share": 1.5,
    }
    band = compute_state_band(state)
    assert band["errors"] == []
    assert band["price"]["lo"] == pytest.approx(100.0)
    assert band["price"]["hi"] == pytest.approx(121.0)
    assert band["price"]["mid"] == pytest.approx(110.5)
    assert band["price"]["rel_width"] == pytest.approx(21.0 / 110.5)
    assert band["price"]["width_ok"] is True
    assert band["v_tr"]["lo"] == pytest.approx(101.5)
    assert band["v_tr"]["mid"] == pytest.approx(112.0)
    assert band["v_tr"]["hi"] == pytest.approx(122.5)

    # method="reference" requires ref and price_range, does NOT require note
    ref_ok = compute_state_band(
        {
            "name": "bear",
            "calc": {
                "method": "reference",
                "price_range": {"lo": 70.0, "hi": 80.0},
                "ref": "model/dcf.xlsx#Summary!B10",
            },
        }
    )
    assert ref_ok["errors"] == []
    ref_missing = compute_state_band(
        {
            "name": "bear",
            "calc": {
                "method": "reference",
                "price_range": {"lo": 70.0, "hi": 80.0},
            },
        }
    )
    assert any("calc.ref" in e for e in ref_missing["errors"])

    # Width > 25% is rejected
    wide_state = {
        "name": "bull",
        "calc": {
            "method": "eps_pe",
            "eps": {"lo": 4.0, "hi": 6.0},
            "multiple": {"lo": 20.0, "hi": 20.0},
        },
    }
    wide_band = compute_state_band(wide_state)
    assert wide_band["price"]["width_ok"] is False
    assert any("超过上限 25.00%" in e for e in wide_band["errors"])

    # eps.lo <= 0 rejected for eps_pe
    neg_eps_state = {
        "name": "bear",
        "calc": {
            "method": "eps_pe",
            "eps": {"lo": -1.0, "hi": 1.0},
            "multiple": {"lo": 15.0, "hi": 18.0},
        },
    }
    assert any("`calc.eps.lo` > 0" in e for e in compute_state_band(neg_eps_state)["errors"])

    # 2. Three-state ordering & §12.2 V_TR non-monotonicity degradation
    s_bear = compute_state_band(
        {
            "name": "bear",
            "calc": {"method": "reference", "price_range": {"lo": 70.0, "hi": 80.0}, "ref": "E-1"},
            "cash_return_per_share": 0.0,
        }
    )
    s_base = compute_state_band(
        {
            "name": "base",
            "calc": {"method": "reference", "price_range": {"lo": 95.0, "hi": 105.0}, "ref": "E-1"},
            "cash_return_per_share": 0.0,
        }
    )
    s_bull = compute_state_band(
        {
            "name": "bull",
            "calc": {"method": "reference", "price_range": {"lo": 120.0, "hi": 140.0}, "ref": "E-1"},
            "cash_return_per_share": 0.0,
        }
    )
    assert validate_states_order({"bear": s_bear, "base": s_base, "bull": s_bull})["ok"] is True

    # Cash return inverting V_TR order degrades to cannot_judge (§12.2, not hard error)
    s_bear_high_cash = copy.deepcopy(s_bear)
    s_bear_high_cash["v_tr"]["mid"] = 105.0
    order_deg = validate_states_order({"bear": s_bear_high_cash, "base": s_base, "bull": s_bull})
    assert order_deg["ok"] is True
    assert order_deg["v_tr_monotonic"] is False
    u_deg = compute_u_req_row(0.5, s_bear_high_cash["v_tr"], s_base["v_tr"], s_bull["v_tr"], 115.0)
    assert u_deg["mid"]["status"] == "无法判断"
    assert u_deg["mid"]["verdict"] == "cannot_judge"
    assert u_deg["mid"]["ev_u_zero"] == pytest.approx(0.5 * 100.0 + 0.5 * 105.0)
    assert u_deg["mid"]["ev_u_max"] == pytest.approx(0.5 * 100.0 + 0.5 * 130.0)

    # 3. Long hurdle H_L = P0 * max((1+h)^T, 1+min_total_return)
    assert compute_long_hurdle(100.0, 12, 0.15, 0.10) == pytest.approx(115.0)
    assert compute_long_hurdle(100.0, 3, 0.15, 0.10) == pytest.approx(110.0)
    assert compute_long_hurdle(100.0, 24, 0.15, 0.10) == pytest.approx(132.25)

    # 4. Short hurdle H_S = P0 * (1 - min(short_hurdle_annual*T, 0.5) - borrow*T - div*T)
    assert compute_short_hurdle(100.0, 12, 0.20, borrow_cost_annual=0.03, dividend_yield=0.02) == pytest.approx(75.0)
    assert compute_short_hurdle(100.0, 36, 0.20, borrow_cost_annual=0.01, dividend_yield=0.0) == pytest.approx(47.0)

    # 5. Breakeven u_req(b) & d_req(b) hand-calculated
    v_d = {"lo": 75.0, "mid": 80.0, "hi": 85.0}
    v_m = {"lo": 105.0, "mid": 110.0, "hi": 115.0}
    v_u = {"lo": 135.0, "mid": 140.0, "hi": 145.0}
    u_row = compute_u_req_row(0.5, v_d, v_m, v_u, 115.0)
    assert u_row["mid"]["raw"] == pytest.approx(1.0 / 3.0)
    assert u_row["mid"]["clamped"] == pytest.approx(1.0 / 3.0)
    assert u_row["mid"]["d_implied"] == pytest.approx(1.0 / 6.0)
    assert u_row["mid"]["status"] == "可达"
    ev_check = u_row["mid"]["d_implied"] * 80.0 + 0.5 * 110.0 + u_row["mid"]["clamped"] * 140.0
    assert ev_check == pytest.approx(115.0)

    # Unreachable conservative endpoint in range display explicitly says "该组合不可达" (not clamped to 1-b)
    u_partial = compute_u_req_row(
        0.5,
        {"lo": 70.0, "mid": 80.0, "hi": 90.0},
        {"lo": 95.0, "mid": 110.0, "hi": 118.0},
        {"lo": 120.0, "mid": 140.0, "hi": 150.0},
        115.0,
    )
    assert u_partial["all_hi"]["reachable"] is True
    assert u_partial["all_lo"]["reachable"] is False
    assert "该组合不可达" in u_partial["display"]

    # u_req < 0 -> "无需 bull 即达标"
    u_easy = compute_u_req_row(0.6, {"lo": 95.0, "mid": 100.0, "hi": 105.0}, {"lo": 125.0, "mid": 130.0, "hi": 135.0}, v_u, 115.0)
    assert u_easy["mid"]["raw"] < 0
    assert u_easy["mid"]["status"] == "无需 bull 即达标"

    # u_req > 1 - b -> "该组合不可达"
    u_impossible = compute_u_req_row(0.6, v_d, v_m, {"lo": 115.0, "mid": 120.0, "hi": 125.0}, 130.0)
    assert u_impossible["mid"]["raw"] > 0.4
    assert u_impossible["mid"]["reachable"] is False
    assert u_impossible["mid"]["status"] == "该组合不可达"

    # Short closure d_req(0.5) uses price-only terminal values (not v_tr cash returns)
    s_d = {"lo": 55.0, "mid": 60.0, "hi": 65.0}
    s_m = {"lo": 85.0, "mid": 90.0, "hi": 95.0}
    s_u = {"lo": 115.0, "mid": 120.0, "hi": 125.0}
    d_row = compute_d_req_row(0.5, s_d, s_m, s_u, 80.0)
    assert d_row["mid"]["raw"] == pytest.approx(25.0 / 60.0)
    assert d_row["mid"]["u_implied"] == pytest.approx(5.0 / 60.0)
    short_ev = d_row["mid"]["clamped"] * 60.0 + 0.5 * 90.0 + d_row["mid"]["u_implied"] * 120.0
    assert short_ev == pytest.approx(80.0)

    # Verify evaluate_underwriting short breakeven ignores cash_return_per_share (uses price band)
    short_thesis_with_div = _sample_v2_thesis()
    for st in short_thesis_with_div["underwriting"]["states"]:
        st["cash_return_per_share"] = 5.0
    eval_div = evaluate_underwriting(short_thesis_with_div)
    short_thesis_no_div = _sample_v2_thesis()
    eval_no_div = evaluate_underwriting(short_thesis_no_div)
    assert eval_div["breakeven"]["short"] == eval_no_div["breakeven"]["short"]

    # Outward 5% rounding
    assert round_outward_5pct(0.261, 0.339) == (25, 35)

    # 6. Adverse move directional check uses endpoints (§12.4) & entry_band directional check
    adv_long = compute_adverse_move("long", 100.0, bear_v_tr_lo=78.0, bull_price_hi=135.0)
    assert adv_long["adverse_move"] == pytest.approx(0.22)
    assert adv_long["ok_for_act_now"] is True
    adv_short_bad = compute_adverse_move("short", 100.0, bear_v_tr_lo=65.0, bull_price_hi=130.0)
    assert adv_short_bad["adverse_move"] == pytest.approx(0.30)
    assert adv_short_bad["ok_for_act_now"] is False

    eb_long_ok = check_entry_band("long", {"lo": 82.0, "hi": 90.0}, 100.0, bear_price_lo=75.0, bull_price_hi=140.0)
    assert eb_long_ok["ok"] is True
    eb_short_ok = check_entry_band("short", {"lo": 108.0, "hi": 116.0}, 100.0, bear_price_lo=65.0, bull_price_hi=130.0)
    assert eb_short_ok["ok"] is True
    eb_short_bad = check_entry_band("short", {"lo": 95.0, "hi": 105.0}, 100.0, bear_price_lo=65.0, bull_price_hi=130.0)
    assert eb_short_bad["ok"] is False

    # 7. Multiple expansion log decomposition uses base midpoint EPS (§4.3, §12.3) + short mirror
    base_for_decomp = compute_state_band(
        {
            "name": "base",
            "calc": {"method": "eps_pe", "eps": {"lo": 4.9, "hi": 5.1}, "multiple": {"lo": 19.0, "hi": 21.0}},
        }
    )
    bull_rerate = compute_state_band(
        {
            "name": "bull",
            "calc": {"method": "eps_pe", "eps": {"lo": 5.2, "hi": 5.4}, "multiple": {"lo": 26.0, "hi": 28.0}},
        }
    )
    bear_derate = compute_state_band(
        {
            "name": "bear",
            "calc": {"method": "eps_pe", "eps": {"lo": 4.7, "hi": 4.9}, "multiple": {"lo": 12.0, "hi": 14.0}},
        }
    )
    decomp = compute_multiple_share(100.0, bull_rerate, base_band=base_for_decomp, bear_band=bear_derate)
    assert decomp["status"] == "ok"
    assert decomp["multiple_share_bull"] + decomp["eps_share_bull"] == pytest.approx(1.0)
    assert decomp["multiple_share_bull"] > 0.5
    assert decomp["multiple_share_bear"] > 0.5
    assert decomp["tag"] == "bull 主要靠拔估值"

    # Bull price <= P0 but multiple > M0 -> "EPS 下滑被拔估值抵消"
    bull_offset = compute_state_band(
        {
            "name": "bull",
            "calc": {"method": "eps_pe", "eps": {"lo": 3.5, "hi": 3.7}, "multiple": {"lo": 25.0, "hi": 26.0}},
        }
    )
    decomp_offset = compute_multiple_share(100.0, bull_offset, base_band=base_for_decomp)
    assert decomp_offset["tag"] == "EPS 下滑被拔估值抵消"

    # 8. Period alignment emits soft warnings (period_misaligned) & supports non-December fiscal year
    p_mismatch = check_period_alignment(
        [
            {"name": "bear", "calc": {"method": "eps_pe", "period": "FY2027"}},
            {"name": "base", "calc": {"method": "eps_pe", "period": "FY2028"}},
        ],
        "2026-10-15",
        12,
    )
    assert p_mismatch["ok"] is False
    assert any("period_misaligned" in w for w in p_mismatch["warnings"])

    # Apple-style Sep 30 fiscal year: anchor 2026-10-15 + 12m = 2027-10-15; FY2028 ends 2028-09-30 (inside [2027-10-15, 2028-10-15])
    p_non_dec = check_period_alignment(
        [
            {"name": "bear", "calc": {"method": "eps_pe", "period": "FY2028", "fiscal_year_end": "09-30"}},
            {"name": "base", "calc": {"method": "eps_pe", "period": "FY2028", "fiscal_year_end": "09-30"}},
            {"name": "bull", "calc": {"method": "eps_pe", "period": "FY2028", "fiscal_year_end": "09-30"}},
        ],
        "2026-10-15",
        12,
    )
    assert p_non_dec["ok"] is True
    assert p_non_dec["warnings"] == []

    stale_age = check_anchor_staleness({"price": 100.0, "price_at": "2026-10-01"}, now_date=date(2026, 10, 20))
    assert stale_age["is_stale"] is True
    assert stale_age["label"] == "需要重估"
    stale_move = check_anchor_staleness({"price": 100.0, "price_at": "2026-10-18"}, current_price=118.0)
    assert stale_move["is_stale"] is True


def test_scenario_math_cli_and_mandate_settings() -> None:
    """Verify CLI entry point and DaltonMandateSettings snapshot (§4, §5, §12.9)."""
    settings = get_dalton_mandate_settings()
    assert isinstance(settings, DaltonMandateSettings)
    assert settings.long_hurdle_annual == pytest.approx(0.15)
    assert settings.short_hurdle_annual == pytest.approx(0.20)
    assert settings.min_total_return == pytest.approx(0.10)
    assert settings.max_adverse_move_act_now == pytest.approx(0.25)
    assert settings.max_bear_loss_act_now == pytest.approx(-0.25)
    assert settings.anchor_stale_days == 14
    assert settings.anchor_stale_move == pytest.approx(0.15)

    snap = settings.snapshot_hurdles(horizon_months=18)
    assert snap["horizon_months"] == 18
    assert "max_entry_distance" not in snap

    legacy_cfg = DaltonMandateSettings.model_validate({"max_bear_loss_act_now": -0.30})
    assert legacy_cfg.max_adverse_move_act_now == pytest.approx(0.30)

    thesis_doc = _sample_v2_thesis()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = scenario_math_cli_main(["--thesis", json.dumps(thesis_doc), "--current-price", "120"])
    assert rc == 0
    cli_out = json.loads(buf.getvalue())
    assert cli_out["ok"] is True
    assert cli_out["hurdles"]["H_L"] == pytest.approx(115.0)
    assert cli_out["staleness"]["is_stale"] is True
    assert cli_out["staleness"]["label"] == "需要重估"


def test_v2_validation_consistency_matrix_and_i3_downgrade() -> None:
    """Verify §12.4 consistency matrix, I3 downgrade, market_color rules, and ops hurdle snapshot."""
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    thesis = _sample_v2_thesis()

    # Apply ops: snapshots hurdles (including horizon_months) and sets schema_version = 2
    created_doc, _ = apply_ops("thesis", None, [{"op": "create", "value": thesis}], reason="Init v2 thesis", now=now)
    assert created_doc["schema_version"] == 2
    assert created_doc["underwriting"]["hurdles"]["long_hurdle_annual"] == pytest.approx(0.15)
    assert created_doc["underwriting"]["hurdles"]["max_adverse_move_act_now"] == pytest.approx(0.25)
    assert created_doc["underwriting"]["hurdles"]["horizon_months"] == 12
    assert "max_entry_distance" not in created_doc["underwriting"]["hurdles"]

    val_ok = validate_update("thesis", None, created_doc, reason="Validate v2 thesis")
    assert val_ok.ok, val_ok.format_problems()

    # Modifying underwriting.states or horizon_months increments scenario_version automatically
    new_states = copy.deepcopy(created_doc["underwriting"]["states"])
    new_states[2]["calc"]["multiple"] = {"lo": 21.5, "hi": 22.5, "unit": "x"}
    updated_doc, _ = apply_ops(
        "thesis",
        created_doc,
        [{"op": "set", "path": "/underwriting/states", "value": new_states}],
        reason="Bump bull multiple",
        now=now,
    )
    assert updated_doc["underwriting"]["scenario_version"] == 2

    updated_hm_doc, _ = apply_ops(
        "thesis",
        updated_doc,
        [{"op": "set", "path": "/house_view/horizon_months", "value": 18}],
        reason="Extend horizon to 18m",
        now=now,
    )
    assert updated_hm_doc["underwriting"]["scenario_version"] == 3
    assert updated_hm_doc["underwriting"]["hurdles"]["horizon_months"] == 18

    # Explicit scenario_version regression is rejected
    regressed_doc = copy.deepcopy(updated_hm_doc)
    regressed_doc["underwriting"]["scenario_version"] = 1
    res_reg = validate_update("thesis", updated_hm_doc, regressed_doc, reason="Regress scenario_version")
    assert not res_reg.ok
    assert any(e["code"] == "scenario_version_regression" for e in res_reg.errors)

    # I3 downgraded to warning when underwriting is present
    doc_with_bad_target = copy.deepcopy(created_doc)
    doc_with_bad_target["house_view"]["target"] = {
        "value": 145.0,
        "currency": "USD",
        "method": "22x FY27E EPS",
        "model_ref": {"kind": "file", "path": "notes.md"},
    }
    res_i3_warn = validate_update("thesis", created_doc, doc_with_bad_target, reason="Legacy target with underwriting")
    assert res_i3_warn.ok, res_i3_warn.format_problems()
    assert any(w["code"] == "target_needs_model_cell" for w in res_i3_warn.warnings)

    # market_color forbidden on pillars AND on underwriting (weight_bounds.sources / states.calc)
    doc_bad_mc = copy.deepcopy(created_doc)
    doc_bad_mc["evidence"].append(
        {
            "id": "E-20261018-09",
            "at": "2026-10-18T12:00:00Z",
            "period": "3Q26",
            "source": {
                "type": "market_color",
                "identity": "Sell-side desk chat",
                "tier": 5,
                "ref": {"kind": "file", "path": "sources/chat.md"},
            },
            "fact": "Rumored 3Q revenue beat.",
            "pillar_ids": ["P1"],
            "signal": "confirming",
            "quality": "low",
        }
    )
    res_bad_mc = validate_update("thesis", created_doc, doc_bad_mc, reason="Misuse market_color on P1")
    assert not res_bad_mc.ok
    assert any(e["code"] == "market_color_pillar_forbidden" for e in res_bad_mc.errors)

    doc_bad_mc_uw = copy.deepcopy(created_doc)
    doc_bad_mc_uw["underwriting"]["weight_bounds"]["sources"] = [{"ref": "E-20261015-02", "kind": "evidence"}]
    res_bad_mc_uw = validate_update("thesis", created_doc, doc_bad_mc_uw, reason="Misuse market_color in weight_bounds")
    assert not res_bad_mc_uw.ok
    assert any(e["code"] == "market_color_underwriting_forbidden" for e in res_bad_mc_uw.errors)

    # Consistency Matrix Row 1: act_now with bear lower endpoint loss > 25% rejected (even if midpoint >= -25%)
    doc_deep_bear = copy.deepcopy(created_doc)
    doc_deep_bear["underwriting"]["states"][0]["calc"] = {
        "method": "reference",
        "price_range": {"lo": 72.0, "hi": 88.0, "unit": "USD"},
        "ref": "E-20261015-03",
    }
    res_deep_bear = validate_update("thesis", None, doc_deep_bear, reason="Bear lo=72 (-28%) with act_now")
    assert not res_deep_bear.ok
    assert any(e["code"] == "consistency_matrix_violation" for e in res_deep_bear.errors)

    # Consistency Matrix Row 5: stance=neutral requires both long & short breakeven_verdict, neither clearly_above
    # UNLESS binding_constraint is implementation or timing
    doc_neutral_bad = copy.deepcopy(created_doc)
    doc_neutral_bad["house_view"]["stance"] = "neutral"
    doc_neutral_bad["house_view"]["conviction"] = "medium"
    doc_neutral_bad["house_view"]["actionability"] = "wait_price"
    doc_neutral_bad["house_view"]["entry_band"] = {"lo": 85.0, "hi": 92.0, "unit": "USD"}
    doc_neutral_bad["house_view"]["binding_constraint"] = "price"
    doc_neutral_bad["house_view"]["binding_constraint_text"] = "等待回撤至 85–92 美元区间"
    doc_neutral_bad["underwriting"]["breakeven_verdict"] = [
        {"side": "long", "verdict": "clearly_above", "b_row": 0.5, "basis": "多头过线", "debate_ids": ["D1"]},
        {"side": "short", "verdict": "below", "b_row": 0.5, "basis": "空头不过线", "debate_ids": ["D1"]},
    ]
    res_neutral_bad = validate_update("thesis", None, doc_neutral_bad, reason="Neutral with clearly_above long and bc=price")
    assert not res_neutral_bad.ok
    assert any(e["code"] == "consistency_matrix_violation" for e in res_neutral_bad.errors)

    # Neutral + clearly_above IS allowed when binding_constraint is implementation or timing
    doc_neutral_impl = copy.deepcopy(doc_neutral_bad)
    doc_neutral_impl["house_view"]["actionability"] = "pass"
    doc_neutral_impl["house_view"]["binding_constraint"] = "implementation"
    doc_neutral_impl["house_view"]["binding_constraint_text"] = "ADR 流动性受限且合规禁买期无法建仓"
    res_neutral_impl = validate_update("thesis", None, doc_neutral_impl, reason="Neutral with implementation constraint")
    assert res_neutral_impl.ok, res_neutral_impl.format_problems()

    # §2.2 & §12.3: multiple_share > 0.5 caps conviction at medium unless rerating_observed + rerating_sources
    doc_high_mult = copy.deepcopy(created_doc)
    doc_high_mult["underwriting"]["states"][2]["calc"] = {
        "method": "eps_pe",
        "eps": {"lo": 5.3, "hi": 5.6, "unit": "USD"},
        "multiple": {"lo": 26.0, "hi": 28.0, "unit": "x"},
        "basis": "GAAP_PE",
        "period": "FY2027",
        "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
    }
    res_high_mult_fail = validate_update("thesis", None, doc_high_mult, reason="High conviction with >50% multiple share")
    assert not res_high_mult_fail.ok
    assert any(e["path"] == "/house_view/conviction" for e in res_high_mult_fail.errors)

    doc_high_mult["underwriting"]["rerating_observed"] = True
    doc_high_mult["underwriting"]["rerating_sources"] = [{"ref": "E-20261015-01", "kind": "evidence"}]
    res_high_mult_pass = validate_update("thesis", None, doc_high_mult, reason="High conviction with rerating proof")
    assert res_high_mult_pass.ok, res_high_mult_pass.format_problems()

    # §12.3 Path 2: weight_bounds omitted (optional), high conviction valid when b=25% & 50% need no bull at all_lo
    doc_path2 = copy.deepcopy(created_doc)
    doc_path2["underwriting"].pop("weight_bounds", None)
    doc_path2["underwriting"]["states"][0]["calc"] = {
        "method": "reference",
        "price_range": {"lo": 112.0, "hi": 116.0, "unit": "USD"},
        "ref": "E-20261015-03",
    }
    doc_path2["underwriting"]["states"][1]["calc"] = {
        "method": "reference",
        "price_range": {"lo": 125.0, "hi": 135.0, "unit": "USD"},
        "ref": "E-20261015-03",
    }
    doc_path2["underwriting"]["states"][2]["calc"] = {
        "method": "reference",
        "price_range": {"lo": 150.0, "hi": 160.0, "unit": "USD"},
        "ref": "E-20261015-03",
    }
    res_path2 = validate_update("thesis", None, doc_path2, reason="High conviction via Path 2 without weight_bounds")
    assert res_path2.ok, res_path2.format_problems()

    # All vertices pass in weight_bounds is NOT vetoed by b_row=0.25 outside feasible base weight interval (Codex 19)
    doc_wb_vertices = copy.deepcopy(created_doc)
    doc_wb_vertices["underwriting"]["breakeven_verdict"][0]["b_row"] = 0.25
    assert validate_update("thesis", None, doc_wb_vertices, reason="All vertices pass while b_row=0.25").ok

    # v1 backward compatibility (Muse 9): setting /house_view/entry_band = null on v1 thesis keeps schema_version == 1
    v1_thesis = {
        "schema_version": 1,
        "as_of": "2026-10-10T00:00:00Z",
        "one_liner": "Legacy v1 thesis",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "medium",
            "summary": "Legacy summary",
            "updated_at": "2026-10-10T00:00:00Z",
        },
        "house_view_history": [],
        "pillars": created_doc["pillars"],
        "evidence": [created_doc["evidence"][0]],
        "official": None,
    }
    v1_after_null, _ = apply_ops(
        "thesis",
        v1_thesis,
        [{"op": "set", "path": "/house_view/entry_band", "value": None}],
        reason="Touch null v2 field on v1 doc",
        now=now,
    )
    assert v1_after_null["schema_version"] == 1
    res_v1_null = validate_update("thesis", v1_thesis, v1_after_null, reason="Validate v1 doc with null entry_band")
    assert res_v1_null.ok, res_v1_null.format_problems()

    # §12.7 Drift re-underwrite: when price drops into entry_band and a pillar breaks, upgrading to act_now
    # without closing the broken pillar and incrementing scenario_version is rejected
    doc_wait_price = copy.deepcopy(created_doc)
    doc_wait_price["house_view"]["conviction"] = "medium"
    doc_wait_price["house_view"]["actionability"] = "wait_price"
    doc_wait_price["house_view"]["binding_constraint"] = "price"
    doc_wait_price["house_view"]["binding_constraint_text"] = "等待回调至 85–92 美元区间"
    doc_wait_price["house_view"]["entry_band"] = {"lo": 85.0, "hi": 92.0, "unit": "USD"}
    assert validate_update("thesis", None, doc_wait_price, reason="Initial wait_price").ok

    bad_drift_upgrade = copy.deepcopy(doc_wait_price)
    bad_drift_upgrade["underwriting"]["anchor"]["price"] = 88.0
    bad_drift_upgrade["pillars"][0]["status"] = "broken"
    bad_drift_upgrade["house_view"]["actionability"] = "act_now"
    res_bad_drift = validate_update("thesis", doc_wait_price, bad_drift_upgrade, reason="Mechanical act_now on broken pillar")
    assert not res_bad_drift.ok
    assert any(
        e["code"] in ("drift_broken_pillar_unclosed", "drift_reunderwrite_required")
        for e in res_bad_drift.errors
    )

    # Round 3 Lead Decision 2 (Codex 5 & Muse Medium 4):
    # - Unchanged rewrite of medium + act_now + impaired does NOT require scenario_version increment
    doc_med_impaired = copy.deepcopy(created_doc)
    doc_med_impaired["house_view"]["conviction"] = "medium"
    doc_med_impaired["pillars"][0]["status"] = "impaired"
    assert validate_update("thesis", None, doc_med_impaired, reason="Initial medium + act_now + impaired").ok
    rewritten_same_impaired = copy.deepcopy(doc_med_impaired)
    rewritten_same_impaired["one_liner"] = "微调一句话摘要但未改三档/锚价/支柱状态"
    res_same_impaired = validate_update(
        "thesis", doc_med_impaired, rewritten_same_impaired, reason="Rewrite unchanged impaired doc"
    )
    assert res_same_impaired.ok, res_same_impaired.format_problems()

    # - Changing anchor on wait_price re-runs entry_band check (not exempted by drift_exempt)
    bad_anchor_wait = copy.deepcopy(doc_wait_price)
    bad_anchor_wait["underwriting"]["anchor"]["price"] = 50.0  # entry_band [85, 92] > P0=50 is invalid for long
    bad_anchor_wait["underwriting"]["scenario_version"] = 2
    res_bad_anchor_wait = validate_update("thesis", doc_wait_price, bad_anchor_wait, reason="Anchor changed below entry_band")
    assert not res_bad_anchor_wait.ok
    assert any(e["path"] == "/house_view/entry_band" for e in res_bad_anchor_wait.errors)

    # Round 3 Lead Decision 3 (Codex 6, 7 & Muse High 1):
    # - needs_financing="yes" does NOT cap conviction="high"
    doc_nf_yes_high = copy.deepcopy(created_doc)
    doc_nf_yes_high["underwriting"]["protection"]["needs_financing"] = "yes"
    doc_nf_yes_high["underwriting"]["stress"]["needs_financing"] = "yes"
    res_nf_yes_high = validate_update("thesis", None, doc_nf_yes_high, reason="High conviction with needs_financing=yes")
    assert res_nf_yes_high.ok, res_nf_yes_high.format_problems()

    # - clearly_above with conviction="medium" does NOT require all vertices in weight_bounds to pass
    doc_med_partial_wb = copy.deepcopy(created_doc)
    doc_med_partial_wb["house_view"]["conviction"] = "medium"
    doc_med_partial_wb["underwriting"]["weight_bounds"] = {
        "bear": {"lo": 0.10, "hi": 0.30},
        "base": {"lo": 0.40, "hi": 0.50},
        "bull": {"lo": 0.25, "hi": 0.45},
        "rationale": "部分顶点过线、b=50% 需 u>=32%，u 区间 [25%, 45%] 支持 medium + clearly_above",
        "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
    }
    res_med_partial_wb = validate_update("thesis", None, doc_med_partial_wb, reason="Medium conviction with partial vertex pass")
    assert res_med_partial_wb.ok, res_med_partial_wb.format_problems()

    # Round 3 Codex 3 & 9: method=reference must resolve to real model/calc record; eps_pe requires sources
    doc_bad_ref = copy.deepcopy(created_doc)
    doc_bad_ref["underwriting"]["stress"]["calc"]["ref"] = "missing/never-existed.json"
    res_bad_ref = validate_update("thesis", None, doc_bad_ref, reason="Fake file path ref")
    assert not res_bad_ref.ok
    assert any(e["code"] == "unresolvable_reference_ref" for e in res_bad_ref.errors)

    doc_filing_ref = copy.deepcopy(created_doc)
    doc_filing_ref["underwriting"]["stress"]["calc"]["ref"] = "E-20261015-01"  # filing, not model/calc
    res_filing_ref = validate_update("thesis", None, doc_filing_ref, reason="Filing evidence as calc ref")
    assert not res_filing_ref.ok
    assert any(e["code"] == "unresolvable_reference_ref" for e in res_filing_ref.errors)

    doc_no_eps_src = copy.deepcopy(created_doc)
    doc_no_eps_src["underwriting"]["states"][0]["calc"].pop("sources", None)
    res_no_eps_src = validate_update("thesis", None, doc_no_eps_src, reason="eps_pe missing sources")
    assert not res_no_eps_src.ok

    # Round 3 Codex 8 & Round 4 Fix 4 (Codex 3):
    # wait_evidence and no_view must point to dated items that actually exist in agenda (pillar validation_id cannot substitute)
    empty_agenda = {"schema_version": 2, "as_of": "2026-10-18T12:00:00Z", "next_validations": [], "watch": {"events": []}}
    valid_agenda_v001 = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "next_validations": [{"id": "V-001", "due": "2026-11-05", "status": "pending", "metric": "3Q26 ROI"}],
        "watch": {"events": []},
    }
    doc_bad_trig = copy.deepcopy(created_doc)
    doc_bad_trig["house_view"]["actionability"] = "wait_evidence"
    doc_bad_trig["house_view"]["binding_constraint"] = "evidence"
    doc_bad_trig["house_view"]["binding_constraint_text"] = "等待 3Q26 财报验证"
    doc_bad_trig["house_view"]["trigger_refs"] = ["V-999"]
    res_bad_trig = validate_update("thesis", None, doc_bad_trig, agenda_doc=valid_agenda_v001, reason="Nonexistent V-999 trigger")
    assert not res_bad_trig.ok
    assert any(e["code"] == "wait_evidence_invalid_triggers" for e in res_bad_trig.errors)

    # Pillar has V-001 + due, but agenda is empty -> rejected for wait_evidence
    doc_good_trig = copy.deepcopy(doc_bad_trig)
    doc_good_trig["house_view"]["trigger_refs"] = ["V-001"]
    res_pillar_only_wait = validate_update(
        "thesis", None, doc_good_trig, agenda_doc=empty_agenda, reason="Pillar V-001 cannot substitute for empty agenda"
    )
    assert not res_pillar_only_wait.ok
    assert any(e["code"] == "wait_evidence_invalid_triggers" for e in res_pillar_only_wait.errors)

    # Passes when V-001 + due exists in agenda_doc
    assert validate_update(
        "thesis", None, doc_good_trig, agenda_doc=valid_agenda_v001, reason="Existing V-001 trigger in agenda"
    ).ok

    # Round 5 Fix 1: wake_conditions with kind="date" and params.date (or date.at) is recognized as a dated trigger
    agenda_with_date_wake = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "next_validations": [],
        "watch": {"events": []},
        "wake_conditions": [
            {
                "id": "C1",
                "kind": "date",
                "params": {"date": "2026-11-05"},
                "action": "3Q26 财报日复核",
                "enabled": True,
            }
        ],
    }
    doc_wake_trig = copy.deepcopy(doc_bad_trig)
    doc_wake_trig["house_view"]["trigger_refs"] = ["C1"]
    res_wake_trig = validate_update(
        "thesis", None, doc_wake_trig, agenda_doc=agenda_with_date_wake, reason="wait_evidence with C1(kind=date, params.date)"
    )
    assert res_wake_trig.ok, res_wake_trig.format_problems()

    # Pillar has V-001 + due, but agenda is empty -> rejected for no_view as well
    doc_no_view_bad = copy.deepcopy(created_doc)
    doc_no_view_bad["house_view"] = {
        "stance": "no_view",
        "conviction": "none",
        "actionability": "pass",
        "missing": ["缺少云业务分部利润率拆解"],
        "review_by": "2026-11-05",
        "summary": "暂不立论",
        "updated_at": "2026-10-18T12:00:00Z",
    }
    res_no_view_pillar_only = validate_update(
        "thesis", None, doc_no_view_bad, agenda_doc=empty_agenda, reason="no_view with only pillar V-001 and empty agenda"
    )
    assert not res_no_view_pillar_only.ok
    assert any(e["code"] == "no_view_missing_agenda_validation" for e in res_no_view_pillar_only.errors)
    assert validate_update(
        "thesis", None, doc_no_view_bad, agenda_doc=valid_agenda_v001, reason="no_view with dated V-001 in agenda"
    ).ok

    # Round 5 Fix 5: no_view 30-day validation requires agenda.next_validations; a dated watch event alone is rejected
    agenda_watch_only = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "next_validations": [],
        "watch": {
            "events": [
                {
                    "id": "W1",
                    "type": "earnings",
                    "date": "2026-10-28",
                    "date_status": "confirmed",
                }
            ]
        },
        "wake_conditions": [],
    }
    res_no_view_watch_only = validate_update(
        "thesis", None, doc_no_view_bad, agenda_doc=agenda_watch_only, reason="no_view with only watch event W1"
    )
    assert not res_no_view_watch_only.ok
    assert any(e["code"] == "no_view_missing_agenda_validation" for e in res_no_view_watch_only.errors)

    # Round 4 Fix 1: debate existence cannot self-certify via states[].debate_ids
    debate_only_d1 = {"schema_version": 2, "as_of": "2026-10-18T12:00:00Z", "debates": [{"id": "D1", "status": "open"}]}
    doc_self_cert_d99 = copy.deepcopy(created_doc)
    doc_self_cert_d99["underwriting"]["states"][0]["debate_ids"] = ["D99"]
    doc_self_cert_d99["underwriting"]["breakeven_verdict"][0]["debate_ids"] = ["D99"]
    res_d99 = validate_update(
        "thesis", None, doc_self_cert_d99, debate_doc=debate_only_d1, reason="Self-certified D99 not in debate_doc"
    )
    assert not res_d99.ok
    assert any(e["code"] == "state_unknown_debate_ref" for e in res_d99.errors)
    assert any(e["code"] == "breakeven_missing_debate_ref" for e in res_d99.errors)

    # When debate_doc / models_doc is None, emits soft warnings instead of hard errors
    res_no_deb_doc = validate_update("thesis", None, created_doc, debate_doc=None, models_doc=None, reason="No debate_doc or models_doc passed")
    assert res_no_deb_doc.ok
    assert any(w["code"] == "debate_refs_unverified" for w in res_no_deb_doc.warnings)
    assert any(w["code"] == "model_refs_unverified" for w in res_no_deb_doc.warnings)

    # Round 5 Fix 2: when workspace has no models doc (models_doc={}), source.type=model evidence & cell cannot self-certify
    res_no_models_in_ws = validate_update(
        "thesis", None, created_doc, models_doc={}, reason="Workspace has no models doc, model evidence cannot self-certify"
    )
    assert not res_no_models_in_ws.ok
    assert any(e["code"] == "unresolvable_reference_ref" for e in res_no_models_in_ws.errors)

    # Round 4 Fix 2 & Round 5 Fix 3: reference must resolve to real calculation record; worksheet list comes ONLY from models_doc
    doc_plain_note_ref = copy.deepcopy(created_doc)
    doc_plain_note_ref["evidence"].append(
        {
            "id": "E-20261015-99",
            "statement": "估值待研究，尚无公式、输入、口径或结果",
            "source": {"type": "internal_note", "ref": "notes/todo.md", "date": "2026-10-15"},
            "supports": ["P1"],
            "created_at": "2026-10-15T10:00:00Z",
        }
    )
    doc_plain_note_ref["underwriting"]["stress"]["calc"]["ref"] = "E-20261015-99"
    res_plain_note = validate_update("thesis", None, doc_plain_note_ref, reason="Plain note with keyword 估值")
    assert not res_plain_note.ok
    assert any(e["code"] == "unresolvable_reference_ref" for e in res_plain_note.errors)

    models_reg = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/META_dcf.xlsx",
                "sha256": "a" * 64,
                "created_at": "2026-10-15T10:00:00Z",
                "reason": "Initial DCF model",
                "key_outputs": [
                    {"name": "fair_value", "cell": "Summary!G42", "value": 115.0, "unit": "per_share", "period": "FY2027"}
                ],
            }
        ],
    }
    doc_bad_sheet = copy.deepcopy(created_doc)
    doc_bad_sheet["underwriting"]["stress"]["calc"]["ref"] = "model/META_dcf.xlsx#NoSuchSheet!G42"
    res_bad_sheet = validate_update(
        "thesis", None, doc_bad_sheet, models_doc=models_reg, reason="Nonexistent worksheet in registered model"
    )
    assert not res_bad_sheet.ok
    assert any(e["code"] == "unresolvable_reference_ref" for e in res_bad_sheet.errors)

    doc_composite_ref = copy.deepcopy(created_doc)
    doc_composite_ref["underwriting"]["stress"]["calc"]["ref"] = "E-20261015-03#Summary!G42"
    res_composite_ref = validate_update(
        "thesis", None, doc_composite_ref, models_doc=models_reg, reason="Composite E-xxxx#Sheet!Cell ref"
    )
    assert res_composite_ref.ok, res_composite_ref.format_problems()

    # Round 5 Fix 3: registered model without worksheet list allows evidence on Summary!G42 AND stress ref on model/X.xlsx#Stress!G42
    models_no_sheets = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/X.xlsx",
                "sha256": "b" * 64,
                "created_at": "2026-10-15T10:00:00Z",
                "reason": "Registered model without key_outputs or sheet list",
            }
        ],
    }
    doc_no_sheets_model = copy.deepcopy(created_doc)
    doc_no_sheets_model["evidence"][2]["source"]["ref"] = {
        "kind": "model_cell",
        "path": "model/X.xlsx",
        "cell": "Summary!G42",
    }
    doc_no_sheets_model["underwriting"]["stress"]["calc"]["ref"] = "model/X.xlsx#Stress!G42"
    res_no_sheets_model = validate_update(
        "thesis",
        None,
        doc_no_sheets_model,
        models_doc=models_no_sheets,
        reason="Registered model without sheet list allows Stress!G42 alongside Summary!G42 evidence",
    )
    assert res_no_sheets_model.ok, res_no_sheets_model.format_problems()

    # Round 4 Fix 3: changing pillar status intact -> impaired without changing anchor/states/weight_bounds/horizon_months
    # does NOT require scenario_version increment
    base_med_intact = copy.deepcopy(created_doc)
    base_med_intact["house_view"]["conviction"] = "medium"
    doc_pillar_imp, _ = apply_ops(
        "thesis",
        base_med_intact,
        [{"op": "set", "path": "/pillars/P1/status", "value": "impaired"}],
        reason="Pillar P1 intact -> impaired without scenario change",
        now=now,
    )
    assert doc_pillar_imp["underwriting"]["scenario_version"] == base_med_intact["underwriting"]["scenario_version"]
    res_pillar_imp = validate_update(
        "thesis", base_med_intact, doc_pillar_imp, reason="Pillar intact -> impaired without scenario_version bump"
    )
    assert res_pillar_imp.ok, res_pillar_imp.format_problems()

    # Round 3 Codex 10 & Muse Medium 3: breakeven_verdict requires b_row in (0.25, 0.5) & debate_ids; protection.value requires text/mechanism
    doc_bad_b_row = copy.deepcopy(created_doc)
    doc_bad_b_row["underwriting"]["breakeven_verdict"][0]["b_row"] = 0.33
    assert not validate_update("thesis", None, doc_bad_b_row, reason="Invalid b_row=0.33").ok

    doc_bad_bv_deb = copy.deepcopy(created_doc)
    doc_bad_bv_deb["underwriting"]["breakeven_verdict"][0]["debate_ids"] = []
    assert not validate_update("thesis", None, doc_bad_bv_deb, reason="Empty debate_ids in breakeven_verdict").ok

    doc_bad_prot_val = copy.deepcopy(created_doc)
    doc_bad_prot_val["underwriting"]["protection"]["value"] = {
        "value_range": {"lo": 72.0, "hi": 82.0, "unit": "USD"}
    }
    res_bad_prot_val = validate_update("thesis", None, doc_bad_prot_val, reason="Protection value object without text")
    assert not res_bad_prot_val.ok
    assert any(e["code"] == "protection_incomplete" for e in res_bad_prot_val.errors)


@pytest.mark.asyncio
async def test_debate_drivers_agenda_v2_and_rendering() -> None:
    """Verify debate expectations (§12.6), drivers response, agenda price_level (§12.10), and odds card rendering (§8)."""
    # 1. Debate v2 + street/buyside/house + frozen_pre_event rules (§3.4, §12.6)
    debate_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "debates": [
            {
                "id": "D1",
                "variable": "FY27 AI Capex 与折旧节奏",
                "question": "FY27 Capex 是否突破 850 亿美元并压制利润率？",
                "why_it_matters": "影响 FY27 EPS 约 0.45 美元及 2–3x PE 倍数",
                "rank": 1,
                "transmission": "both",
                "expectations": [
                    {
                        "metric": "FY27 Capex",
                        "period": "FY2027",
                        "unit": "USD_bn",
                        "street": {
                            "value": {"lo": 75.0, "hi": 80.0, "unit": "USD_bn"},
                            "basis": "GAAP",
                            "as_of": "2026-10-15",
                            "sources": [{"ref": "E-20261015-01", "kind": "evidence"}],
                            "note": "卖方一致预期中位数 77.5B",
                        },
                        "buyside": None,
                        "house": {
                            "value": {"lo": 72.0, "hi": 76.0, "unit": "USD_bn"},
                            "basis": "GAAP",
                            "as_of": "2026-10-18",
                            "sources": ["model/meta_dcf.xlsx#Capex!F12"],
                            "note": "我方供应链跟踪预测",
                        },
                        "frozen_pre_event": True,
                        "event_id": "W1",
                        "t0": "2026-10-28T20:00:00Z",
                        "published_at": "2026-10-15T10:00:00Z",
                        "acquired_at": "2026-10-16T10:00:00Z",
                        "frozen_at": "2026-10-18T12:00:00Z",
                    }
                ],
                "bull": {"case": "Capex 控制在 780 亿以内"},
                "bear": {"case": "Capex 飙升至 900 亿以上"},
                "strongest_rebuttal": {"against": "my_view", "text": "供应链订单显示服务器采购仍在加速"},
                "my_view": {"side": "bull", "confidence": "medium", "text": "管理层注重 ROI 纪律"},
                "market_lean": {"side": "bear", "evidence": "买方普遍担忧 Capex 上调"},
                "flip_condition": {"text": "3Q26 电话会指引 FY27 Capex > 880 亿"},
                "status": "open",
                "opened_at": "2026-10-15T10:00:00Z",
                "history": [],
            }
        ],
    }
    res_deb_ok = validate_update("debate", None, debate_doc, reason="Valid v2 debate")
    assert res_deb_ok.ok, res_deb_ok.format_problems()

    # Lead Decision 1 (Codex 4 & Muse Medium 2):
    # Inconsistent basis across street and house WITHOUT gap is allowed with a soft warning and renders "口径不同，未比较";
    # computing gap with inconsistent or unknown basis is rejected as a hard error.
    diff_basis_no_gap = copy.deepcopy(debate_doc)
    diff_basis_no_gap["debates"][0]["expectations"][0]["house"]["basis"] = "non_GAAP"
    res_diff_basis_warn = validate_update("debate", None, diff_basis_no_gap, reason="Inconsistent basis without gap")
    assert res_diff_basis_warn.ok, res_diff_basis_warn.format_problems()
    assert any(w["code"] == "expectation_basis_mismatch" for w in res_diff_basis_warn.warnings)
    deb_md = render_document_md("debate", diff_basis_no_gap, version_no=1, ticker="META")
    assert deb_md is not None and "口径不同，未比较" in deb_md

    bad_basis_with_gap = copy.deepcopy(diff_basis_no_gap)
    bad_basis_with_gap["debates"][0]["expectations"][0]["gap"] = "-3.5B vs street"
    res_bad_basis = validate_update("debate", None, bad_basis_with_gap, reason="Inconsistent basis with computed gap")
    assert not res_bad_basis.ok
    assert any(e["code"] == "expectation_basis_mismatch" for e in res_bad_basis.errors)

    # Post-event acquired_at > t0 or published_at == t0 cannot set frozen_pre_event=True (§12.6 strict < t0)
    bad_frozen = copy.deepcopy(debate_doc)
    bad_frozen["debates"][0]["expectations"][0]["acquired_at"] = "2026-10-29T01:00:00Z"
    res_deb_bad = validate_update("debate", None, bad_frozen, reason="Post-event acquired_at")
    assert not res_deb_bad.ok
    assert any(e["code"] == "frozen_expectation_post_event" for e in res_deb_bad.errors)

    eq_t0_frozen = copy.deepcopy(debate_doc)
    eq_t0_frozen["debates"][0]["expectations"][0]["frozen_at"] = "2026-10-28T20:00:00Z"  # == t0
    res_eq_t0 = validate_update("debate", None, eq_t0_frozen, reason="frozen_at == t0 must fail strict < t0")
    assert not res_eq_t0.ok
    assert any(e["code"] == "frozen_expectation_post_event" for e in res_eq_t0.errors)

    # Frozen item cannot be deleted (including deleting the whole debate /debates=[]), cannot flip frozen_pre_event to False
    deleted_frozen = copy.deepcopy(debate_doc)
    deleted_frozen["debates"][0]["expectations"] = []
    res_del_frozen = validate_update("debate", debate_doc, deleted_frozen, reason="Delete frozen item")
    assert not res_del_frozen.ok
    assert any(e["code"] == "frozen_expectation_immutable" for e in res_del_frozen.errors)

    deleted_all_debates = copy.deepcopy(debate_doc)
    deleted_all_debates["debates"] = []
    res_del_all_deb = validate_update("debate", debate_doc, deleted_all_debates, reason="Delete entire debate with frozen item")
    assert not res_del_all_deb.ok
    assert any(e["code"] == "frozen_expectation_immutable" for e in res_del_all_deb.errors)

    unfrozen_deb = copy.deepcopy(debate_doc)
    unfrozen_deb["debates"][0]["expectations"][0]["frozen_pre_event"] = False
    res_unfreeze = validate_update("debate", debate_doc, unfrozen_deb, reason="Unfreeze item")
    assert not res_unfreeze.ok
    assert any(e["code"] == "frozen_expectation_immutable" for e in res_unfreeze.errors)

    # Adding a new frozen item after t0 (as_of > t0) or forging a later t0 for the same event_id is rejected (Codex 2)
    post_t0_add = copy.deepcopy(debate_doc)
    post_t0_add["as_of"] = "2026-10-30T12:00:00Z"
    new_frozen_exp = copy.deepcopy(debate_doc["debates"][0]["expectations"][0])
    new_frozen_exp["metric"] = "FY27 Revenue"
    post_t0_add["debates"][0]["expectations"].append(new_frozen_exp)
    res_post_t0_add = validate_update("debate", debate_doc, post_t0_add, reason="Add frozen item after t0")
    assert not res_post_t0_add.ok
    assert any(e["code"] == "frozen_expectation_post_event" for e in res_post_t0_add.errors)

    forged_t0_add = copy.deepcopy(post_t0_add)
    forged_t0_add["debates"][0]["expectations"][1]["t0"] = "2026-11-15T20:00:00Z"  # forging later t0 on event W1
    res_forged_t0 = validate_update("debate", debate_doc, forged_t0_add, reason="Forge later t0 on same event_id W1")
    assert not res_forged_t0.ok
    assert any(e["code"] == "frozen_expectation_post_event" for e in res_forged_t0.errors)

    # 2. Drivers v2 nonlinear response requires threshold_text OR >= 3 non-empty calculation points (§3.4, Codex 11)
    drivers_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "drivers": [
            {
                "id": "K1",
                "name": "Reels 广告加载率与转化率",
                "kind": "price",
                "description": "AI 推荐模型升级提升广告单价",
                "leading_indicators": [],
                "response": {
                    "mechanism": "转化率跨过盈亏平衡点后毛利率非线性释放",
                    "shape": "lag",
                    "threshold_text": None,
                },
                "updated_at": "2026-10-18T12:00:00Z",
            }
        ],
    }
    res_drv_bad = validate_update("drivers", None, drivers_doc, reason="Missing threshold_text and points")
    assert not res_drv_bad.ok
    assert any(e["code"] == "driver_response_missing_threshold_text" for e in res_drv_bad.errors)

    # 3 empty objects [{}, {}, {}] do NOT count as 3 calculation points (Codex 11)
    drivers_empty_pts = copy.deepcopy(drivers_doc)
    drivers_empty_pts["drivers"][0]["response"]["points"] = [{}, {}, {}]
    res_empty_pts = validate_update("drivers", None, drivers_empty_pts, reason="3 empty points")
    assert not res_empty_pts.ok

    # Satisfied via 3 valid points with input + output (even without threshold_text)
    drivers_doc["drivers"][0]["response"]["points"] = [
        {"x": "1Q27", "y": "+0.05 EPS"},
        {"x": "2Q27", "y": "+0.15 EPS"},
        {"x": "3Q27", "y": "+0.40 EPS"},
    ]
    assert validate_update("drivers", None, drivers_doc, reason="Added 3 valid points").ok

    # Round 4 Fix 6: numeric 0 is a valid point coordinate (-1, 1), (0, 0), (1, 1)
    drivers_zero_pt = copy.deepcopy(drivers_doc)
    drivers_zero_pt["drivers"][0]["response"]["points"] = [
        {"driver_move": -1, "eps_impact": 1},
        {"driver_move": 0, "eps_impact": 0},
        {"driver_move": 1, "eps_impact": 1},
    ]
    res_zero_pt = validate_update("drivers", None, drivers_zero_pt, reason="3 points with (0, 0) numeric zero")
    assert res_zero_pt.ok, res_zero_pt.format_problems()

    # 3. Agenda v2 price_level wake condition with spec enums (to_bull | to_bear | review, eps_only | multiple_only | both)
    agenda_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "open_questions": [],
        "next_validations": [],
        "watch": {
            "events": [
                {
                    "id": "W1",
                    "type": "earnings",
                    "date": "2026-10-28",
                    "date_status": "estimated",
                    "review_by": "2026-10-25",
                }
            ]
        },
        "wake_conditions": [
            {
                "id": "C1",
                "kind": "price_level",
                "price_level": {"band": {"lo": 85.0, "hi": 92.0, "unit": "USD"}, "transition": "enter"},
                "direction": "to_bull",
                "transmission": "multiple_only",
                "underwriting_version": 1,
                "action": "进入买点区间，复核是否从 wait_price 上调为 act_now",
                "enabled": True,
            }
        ],
    }
    res_ag_ok = validate_update("agenda", None, agenda_doc, reason="Valid v2 agenda")
    assert res_ag_ok.ok, res_ag_ok.format_problems()

    # Agenda probe explicitly ignores price_level wake conditions
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=agenda_doc,
        today=date(2026, 10, 20),
        now=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
    )
    assert sigs == []

    # Unimplemented wake kind returns wake_kind_unimplemented
    bad_wake_agenda = copy.deepcopy(agenda_doc)
    bad_wake_agenda["wake_conditions"][0]["kind"] = "astrology_signal"
    errs_wake = validate_schema("agenda", bad_wake_agenda)
    assert any(e["code"] == "wake_kind_unimplemented" for e in errs_wake)

    # 4. Rendering agent_block.md and thesis.md (§8, §12.8, and Codex 12 short "未计成本")
    thesis_doc = _sample_v2_thesis()
    block = render_agent_block(
        ticker="META",
        company_doc={"ticker": "META", "name": "Meta Platforms, Inc.", "coverage_tier": "core", "coverage_stage": "full"},
        thesis_doc=thesis_doc,
        thesis_version_no=2,
        now_date="2026-10-18",
    )
    assert "**三情景与赔率**：基准股价 100（2026-10-15）｜Bear 76–84（EPS 4–4.2×19–20x） / Base 100–121（EPS 5–5.5×20–22x） / Bull 134.4–149.6（EPS 6.4–6.8×21–22x）" in block
    assert "达标所需Bull 权重（给定 Base 权重）：Base 50% 时需 Bull 权重至少 15%–50%" in block
    assert "Base 25% 时需 Bull 权重至少 30%–60%" in block
    assert "我方给 Bull 权重 35%–45% → 显著高于门槛" in block
    assert "下行保护 72–82（依赖外部融资：否）" in block
    assert "需要重估" not in block

    # Stale anchor (>14 days from today by default, or >15% move when current_price is passed) renders 需要重估
    stale_block = render_agent_block(
        ticker="META",
        company_doc={"ticker": "META", "name": "Meta Platforms, Inc."},
        thesis_doc=thesis_doc,
        thesis_version_no=2,
        current_price=120.0,
    )
    assert "需要重估" in stale_block

    thesis_md = render_document_md("thesis", thesis_doc, version_no=2, ticker="META", current_price=120.0)
    assert thesis_md is not None
    assert "三情景估值与赔率（v1）" in thesis_md
    assert "【需要重估】" in thesis_md

    # Codex 12: short stance with unknown borrow_cost_annual (None) marks "未计成本" in evaluation and rendering
    short_doc = copy.deepcopy(thesis_doc)
    short_doc["house_view"]["stance"] = "short"
    short_doc["house_view"]["conviction"] = "medium"
    short_doc["house_view"]["actionability"] = "wait_evidence"
    short_doc["house_view"]["binding_constraint"] = "evidence"
    short_doc["house_view"]["binding_constraint_text"] = "等待融券费率确认与 3Q26 财报验证"
    short_doc["house_view"]["trigger_refs"] = ["V-001"]
    short_doc["underwriting"]["borrow_cost_annual"] = None
    short_doc["underwriting"]["dividend_yield"] = None
    short_doc["underwriting"]["breakeven_verdict"] = [
        {"side": "short", "verdict": "around", "b_row": 0.5, "basis": "待借券成本确认", "debate_ids": ["D1"]}
    ]
    short_eval = evaluate_underwriting(short_doc)
    assert short_eval["hurdles"]["short_cost_known"] is False
    assert short_eval["hurdles"]["short_cost_note"] == "未计成本"
    short_block = render_agent_block(
        ticker="META",
        company_doc={"ticker": "META", "name": "Meta Platforms, Inc."},
        thesis_doc=short_doc,
        thesis_version_no=2,
        now_date="2026-10-18",
    )
    assert "未计成本" in short_block
    short_md = render_document_md("thesis", short_doc, version_no=2, ticker="META")
    assert short_md is not None and "（未计成本）" in short_md


@pytest.mark.asyncio
async def test_reconcile_direct_edit_passes_cross_doc_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 4 Fix 5: reconcile.detect_direct_edits passes agenda/debate/models to validate_update."""
    import hashlib
    from typing import Any
    from unittest.mock import create_autospec
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.database import verdicts as db_verdicts
    from src.dalton.knowledge import reconcile
    from src.dalton.knowledge.paths import data_path
    from src.dalton.knowledge.service import canonical_json
    from src.server.database import workspace as ws_db
    from src.server.database import workspace_file as wf_db
    from src.server.services.persistence import resolve as resolve_mod

    thesis_doc = _sample_v2_thesis()
    thesis_doc["house_view"]["actionability"] = "wait_evidence"
    thesis_doc["house_view"]["binding_constraint"] = "evidence"
    thesis_doc["house_view"]["binding_constraint_text"] = "等待 agenda 独有条目 V-002 验证"
    thesis_doc["house_view"]["trigger_refs"] = ["V-002"]
    thesis_doc["underwriting"]["states"][0]["debate_ids"] = ["D2"]
    thesis_doc["underwriting"]["breakeven_verdict"][0]["debate_ids"] = ["D2"]

    agenda_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "open_questions": [],
        "next_validations": [
            {"id": "V-002", "due": "2026-11-12", "status": "pending", "metric": "4Q26 Capex guidance"}
        ],
        "watch": {"events": []},
        "wake_conditions": [],
    }
    old_debate_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "debates": [
            {
                "id": "D1",
                "variable": "FY27 Capex",
                "question": "Capex 是否超预期？",
                "why_it_matters": "影响 EPS 0.45 美元",
                "rank": 1,
                "transmission": "both",
                "bull": {"case": "控制在 780 亿内"},
                "bear": {"case": "超过 900 亿"},
                "strongest_rebuttal": {"against": "my_view", "text": "订单加速"},
                "my_view": {"side": "bull", "confidence": "medium", "text": "ROI 纪律"},
                "market_lean": {"side": "bear", "evidence": "买方担忧 Capex"},
                "flip_condition": {"text": "指引 > 880 亿"},
                "status": "open",
                "opened_at": "2026-10-15T10:00:00Z",
                "history": [],
            }
        ],
    }
    new_debate_doc = copy.deepcopy(old_debate_doc)
    d2_item = copy.deepcopy(old_debate_doc["debates"][0])
    d2_item["id"] = "D2"
    d2_item["rank"] = 2
    new_debate_doc["debates"].append(d2_item)

    models_doc = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/META_dcf.xlsx",
                "sha256": "a" * 64,
                "created_at": "2026-10-15T10:00:00Z",
                "reason": "DCF model",
                "key_outputs": [
                    {"name": "fair_value", "cell": "Summary!G42", "value": 115.0, "unit": "per_share", "period": "FY2027"}
                ],
            }
        ],
    }

    _, ag_sha = canonical_json(agenda_doc)
    _, deb_sha = canonical_json(old_debate_doc)
    _, mod_sha = canonical_json(models_doc)
    existing_valid = {
        "agenda": {
            "knowledge_version_id": "kv-ag-1",
            "workspace_id": "ws-r4",
            "doc_key": "agenda",
            "version_no": 1,
            "content": agenda_doc,
            "content_sha256": ag_sha,
            "schema_valid": True,
            "files_written_at": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
        },
        "debate": {
            "knowledge_version_id": "kv-deb-1",
            "workspace_id": "ws-r4",
            "doc_key": "debate",
            "version_no": 1,
            "content": old_debate_doc,
            "content_sha256": deb_sha,
            "schema_valid": True,
            "files_written_at": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
        },
        "models": {
            "knowledge_version_id": "kv-mod-1",
            "workspace_id": "ws-r4",
            "doc_key": "models",
            "version_no": 1,
            "content": models_doc,
            "content_sha256": mod_sha,
            "schema_valid": True,
            "files_written_at": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
        },
    }

    thesis_path = data_path("thesis")
    debate_path = data_path("debate")
    thesis_text, _ = canonical_json(thesis_doc)
    debate_text, _ = canonical_json(new_debate_doc)
    files_map = {thesis_path: thesis_text, debate_path: debate_text}
    inserted: list[dict[str, Any]] = []

    async def list_all_latest_impl(workspace_id: str, *, valid_only: bool = True):
        return copy.deepcopy(existing_valid)

    async def insert_impl(**kw: Any) -> dict[str, Any]:
        row = {
            "knowledge_version_id": f"kv-new-{len(inserted) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": (kw.get("base_version_no") or 0) + 1,
            "source": kw.get("source", "direct_edit"),
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "warnings": kw.get("warnings") or [],
            "schema_valid": kw.get("schema_valid", True),
            "edited_path": kw.get("edited_path"),
            "files_written_at": None,
            "created_at": datetime(2026, 10, 18, 12, 5, tzinfo=timezone.utc),
        }
        inserted.append(row)
        return row

    async def meta_impl(workspace_id: str, *, conn=None):
        return {
            p: {"kind": "file", "content_hash": hashlib.sha256(t.encode()).hexdigest()}
            for p, t in files_map.items()
        }

    async def get_file_impl(workspace_id, file_path, *, include_content=True, conn=None):
        t = files_map.get(file_path)
        return None if t is None else {"file_path": file_path, "content_text": t}

    async def resolve_impl(file_record: dict, *, user_id: str, context: str):
        return file_record["content_text"]

    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, side_effect=list_all_latest_impl),
    )
    monkeypatch.setattr(
        db_knowledge,
        "insert_version",
        create_autospec(db_knowledge.insert_version, side_effect=insert_impl),
    )
    monkeypatch.setattr(
        db_verdicts, "list_verdicts", create_autospec(db_verdicts.list_verdicts, return_value=[])
    )
    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(db_publications.list_publication_files, return_value=[]),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value={"workspace_id": "ws-r4", "user_id": "u-1"}),
    )
    monkeypatch.setattr(
        reconcile,
        "get_file_metadata_for_sync",
        create_autospec(wf_db.get_file_metadata_for_sync, side_effect=meta_impl),
    )
    monkeypatch.setattr(
        reconcile,
        "db_get_file",
        create_autospec(wf_db.get_file, side_effect=get_file_impl),
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_file_text_or_none",
        create_autospec(resolve_mod.resolve_file_text_or_none, side_effect=resolve_impl),
    )

    res = await reconcile.detect_direct_edits(user_id="u-1", workspace_id="ws-r4")
    assert [r["doc_key"] for r in res["recorded_versions"]] == ["debate", "thesis"]
    deb_rec, thesis_rec = res["recorded_versions"]
    assert deb_rec["schema_valid"] is True, f"Direct edit of debate failed: {deb_rec['warnings']}"
    assert thesis_rec["schema_valid"] is True, f"Direct edit of thesis failed: {thesis_rec['warnings']}"

