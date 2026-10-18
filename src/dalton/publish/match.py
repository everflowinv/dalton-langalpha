"""Deterministic figure-to-source matching and discrepancy classification (PK.9)."""

from __future__ import annotations

import dataclasses
import io
import json
import re
from pathlib import Path
from typing import Any

from src.dalton.publish.numbers import (
    ExtractedFigure,
    _determine_unit_and_scale,
    _extract_metric_from_context,
    _NUMBER_RE,
    _PERIOD_TOKEN_RE,
    extract_figures,
    normalize_period,
)
from src.dalton.settings import get_dalton_config

# FROZEN (P0H §1.3 #4 / L30): Do NOT add new entries to DEFAULT_METRIC_ALIASES, _GROWTH_WORDS,
# or _MARGIN_WORDS. These tables are used only for candidate retrieval and explicit footnote matching.
DEFAULT_METRIC_ALIASES: dict[str, list[str]] = {
    "revenue": ["revenue", "revenues", "total revenue", "营收", "总营收", "收入", "营业收入", "总收入"],
    "ad_revenue": ["ad revenue", "advertising revenue", "广告收入", "广告营收"],
    "aws_revenue": ["aws revenue", "aws 收入", "aws 营收", "aws"],
    "cloud_revenue": ["cloud revenue", "google cloud", "云收入", "云业务收入", "azure"],
    "operating_income": ["operating income", "ebit", "营业利润", "经营利润", "运营利润"],
    "operating_margin": ["operating margin", "opm", "营业利润率", "经营利润率", "运营利润率"],
    "gross_margin": ["gross margin", "gpm", "毛利率"],
    "net_income": ["net income", "净利润", "归母净利润"],
    "eps": ["eps", "diluted eps", "每股收益", "摊薄每股收益", "gaap eps", "non-gaap eps"],
    "capex": ["capex", "capital expenditure", "capital expenditures", "资本开支", "资本支出"],
    "fcf": ["fcf", "free cash flow", "自由现金流"],
    "dap": ["dap", "family dap", "日活", "日活跃用户"],
    "pe": ["p/e", "pe", "市盈率", "估值倍数"],
    "target_price": ["target price", "tp", "目标价"],
    "irr": ["irr", "内部收益率", "预期回报率"],
    "rd_expense": ["r&d", "research and development", "研发费用", "研发开支", "研发支出", "研发投入"],
    "sm_expense": ["s&m", "sales and marketing", "销售及营销费用", "销售费用", "营销费用"],
    "ga_expense": ["g&a", "general and administrative", "一般及行政费用", "管理费用"],
    "market_share": ["market share", "市场份额", "市占率", "份额"],
}

UNIT_BASE: dict[str, tuple[float, str]] = {
    "USD": (1.0, "USD"),
    "USD_mn": (1e6, "USD"),
    "USD_100mn": (1e8, "USD"),
    "USD_bn": (1e9, "USD"),
    "HKD": (1.0, "HKD"),
    "HKD_mn": (1e6, "HKD"),
    "HKD_100mn": (1e8, "HKD"),
    "HKD_bn": (1e9, "HKD"),
    "CNY": (1.0, "CNY"),
    "CNY_mn": (1e6, "CNY"),
    "CNY_100mn": (1e8, "CNY"),
    "CNY_bn": (1e9, "CNY"),
    "pct": (1.0, "pct"),
    "%": (1.0, "pct"),
    "pp": (1.0, "pct"),
    "bp": (0.01, "pct"),
    "bps": (0.01, "pct"),
    "x": (1.0, "x"),
    "count": (1.0, "count"),
    "count_mn": (1e6, "count"),
    "per_share": (1.0, "USD"),
    "raw": (1.0, "other"),
    "other": (1.0, "other"),
}

_GROWTH_WORDS = (
    "同比",
    "环比",
    "增速",
    "增长",
    "下滑",
    "下降",
    "变动",
    "增幅",
    "降幅",
    "yoy",
    "qoq",
    "cagr",
    "growth",
)
_MARGIN_WORDS = (
    "利润率",
    "毛利率",
    "净利率",
    "费率",
    "税率",
    "转化率",
    "留存率",
    "渗透率",
    "margin",
)


@dataclasses.dataclass
class MatchCandidate:
    """A near-miss or conflicting candidate source for an unmatched figure."""

    kind: str
    id: str
    value: float
    unit: str = ""
    period: str = ""
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "why": self.why,
        }


@dataclasses.dataclass
class MatchOutcome:
    """Result of matching a single ExtractedFigure."""

    figure: ExtractedFigure
    matched: bool
    kind: str | None = None
    ref: str | None = None
    excerpt: str = ""
    source_sha256: str = ""
    reason: str | None = None
    hint: str = ""
    candidates: list[MatchCandidate] = dataclasses.field(default_factory=list)


def canonical_metric_group(
    metric: str, aliases: dict[str, list[str]] | None = None
) -> str:
    """Map a metric name to its canonical alias group or normalized token string."""
    m_low = " ".join((metric or "").lower().split())
    if not m_low:
        return ""
    merged = {**DEFAULT_METRIC_ALIASES, **(aliases or {})}
    # Check longest alias phrases first so "广告收入" matches ad_revenue before revenue
    scored: list[tuple[int, str]] = []
    for group, words in merged.items():
        for w in words:
            w_low = w.lower()
            if w_low in m_low:
                scored.append((len(w_low), group))
    if scored:
        scored.sort(reverse=True)
        return scored[0][1]
    return m_low


def metrics_match(
    m1: str, m2: str, aliases: dict[str, list[str]] | None = None
) -> bool:
    """Return True if two metric descriptions refer to the same financial concept."""
    if not m1 or not m2:
        return True
    s1 = " ".join(m1.lower().split())
    s2 = " ".join(m2.lower().split())
    if s1 == s2:
        return True

    # Prevent matching a level metric ("广告收入") against its growth rate ("广告收入同比增速")
    # or an income level ("营业利润") against a margin ("营业利润率").
    has_growth_1 = any(w in s1 for w in _GROWTH_WORDS)
    has_growth_2 = any(w in s2 for w in _GROWTH_WORDS)
    if has_growth_1 != has_growth_2:
        return False

    has_margin_1 = any(w in s1 for w in _MARGIN_WORDS)
    has_margin_2 = any(w in s2 for w in _MARGIN_WORDS)
    if has_margin_1 != has_margin_2:
        return False

    if s1 in s2 or s2 in s1:
        return True
    g1 = canonical_metric_group(s1, aliases)
    g2 = canonical_metric_group(s2, aliases)
    if g1 and g2 and g1 == g2:
        return True
    # Token overlap check for Chinese/English compound metrics
    tokens1 = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", s1))
    tokens2 = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", s2))
    common = tokens1 & tokens2 - {
        "同比",
        "环比",
        "增速",
        "增长",
        "一季度",
        "二季度",
        "三季度",
        "四季度",
    }
    return len(common) > 0


def _to_base(value: float, unit: str, scale: float = 1.0) -> tuple[float, str]:
    mult, dim = UNIT_BASE.get(unit, (1.0, "other"))
    if scale > 1.0 and mult in (1e6, 1e8, 1e9):
        # ExtractedFigure already stores `scale` (e.g. 1e8 for 亿) while setting unit="USD_bn"
        return value * scale, dim
    return value * mult * scale, dim


def values_within_tolerance(
    fig: ExtractedFigure,
    src_value: float,
    src_unit: str,
    src_scale: float = 1.0,
) -> tuple[bool, bool, float]:
    """Compare ``fig`` against ``(src_value, src_unit, src_scale)``.

    Returns ``(matched, is_unit_mismatch, relative_or_abs_diff)``.
    """
    fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale)
    src_base, src_dim = _to_base(src_value, src_unit, src_scale)

    # Half-ULP tolerance in the figure's displayed precision, or 1% relative for derived percentages
    ulp_half = 0.5 * (10.0 ** (-fig.decimals))
    if fig.unit in ("pct", "%") or src_unit in ("pct", "%"):
        if fig.label_hint == "derived" and fig.value != 0:
            tol_display = max(0.05, ulp_half, abs(fig.value) * 0.01)
        else:
            tol_display = max(0.05, ulp_half)
    else:
        tol_display = ulp_half

    fig_effective_scale = (
        fig.scale if fig.scale > 1.0 else UNIT_BASE.get(fig.unit, (1.0, ""))[0]
    )
    tol_base = tol_display * fig_effective_scale

    # Allow currency-dimension to match currency-agnostic scaled counts ('63.4 bn' vs '$63.4bn')
    dims_compatible = (
        fig_dim == src_dim
        or "other" in (fig_dim, src_dim)
        or (fig_dim in ("USD", "CNY", "HKD") and src_dim == "count")
        or (src_dim in ("USD", "CNY", "HKD") and fig_dim == "count")
    )

    if dims_compatible:
        diff_base = abs(fig_base - src_base)
        if diff_base <= tol_base * 1.0001:
            return True, False, 0.0

    # Percentage vs decimal ratio (e.g. 21% vs 0.21)
    if fig_dim == "pct" and src_dim == "other" and 0 < abs(src_value) <= 1.0:
        if abs(fig.value - src_value * 100.0) <= tol_display * 1.0001:
            return True, False, 0.0

    # Chinese prose '翻倍' (2.0x) vs '+100%' (100 pct)
    if fig.raw in ("翻倍", "翻一番") and src_dim == "pct" and abs(src_value - 100.0) <= 0.5:
        return True, False, 0.0

    # Direct unscaled number match when source unit is identical to figure unit (and same scale)
    if abs(fig.value - src_value) <= tol_display * 1.0001:
        if (fig.unit == src_unit and fig.scale == (src_scale if src_scale > 1.0 else fig.scale)) or (
            src_unit == "other" and fig.scale == 1.0 and src_scale == 1.0
        ):
            return True, False, 0.0
        # Same raw number (or 10x/1000x factor) but different unit/scale -> unit_mismatch!
        return False, True, abs(fig_base - src_base)

    # Check if ratio is a power of 10 (e.g. 512百万 vs 512亿, or mn vs bn) -> unit_mismatch
    if fig_base != 0 and src_base != 0:
        ratio = abs(fig_base / src_base)
        for p10 in (1e-6, 1e-4, 1e-3, 1e-2, 1e-1, 1e1, 1e2, 1e3, 1e4, 1e6):
            if abs(ratio - p10) / p10 <= 0.01:
                return False, True, abs(fig_base - src_base)

    return False, False, abs(fig_base - src_base)


def _is_consensus_provenance(prov_rec: dict[str, Any]) -> bool:
    """Return True if ``prov_rec`` is an authorized street-consensus source (PK.9)."""
    prefixes = get_dalton_config().publish.consensus_providers
    provider = str(prov_rec.get("provider") or "")
    ident = str(prov_rec.get("identifier") or "")
    source_type = str(prov_rec.get("source_type") or "")
    detail = str(prov_rec.get("detail") or "")
    if ident == "street_estimate" or source_type == "street_estimate":
        return True
    for pfx in prefixes:
        if provider.startswith(pfx) or ident.startswith(pfx) or detail.startswith(pfx):
            return True
    return False


def _inspect_text_source(
    fig: ExtractedFigure,
    body: str,
    *,
    source_id: str = "",
    kind: str = "prov",
    metric_aliases: dict[str, list[str]] | None = None,
) -> tuple[bool, str, str | None, list[MatchCandidate]]:
    """Unit-normalized numeric comparison in a text body with period and metric compatibility (G3.1 R4-3).

    Returns ``(matched, excerpt, failure_reason, candidates)``.
    """
    if not body or not body.strip():
        return False, "", None, []

    src_figures = extract_figures(body)
    fig_p = normalize_period(fig.period)
    all_body_periods = {
        normalize_period(pm.group(1)) for pm in _PERIOD_TOKEN_RE.finditer(body)
    } - {""}

    cands: list[MatchCandidate] = []
    best_reason: str | None = None

    for sf in src_figures:
        if sf.exempt:
            continue
        # Check metric compatibility when the source figure has an identifiable metric
        if sf.metric and fig.metric:
            if not metrics_match(fig.metric, sf.metric, metric_aliases):
                continue

        sf_p = normalize_period(sf.period)
        ok_val, unit_mis, _ = values_within_tolerance(
            fig, sf.value, sf.unit, sf.scale
        )

        # Period compatibility check
        if fig_p:
            if sf_p and sf_p != fig_p:
                if ok_val:
                    best_reason = best_reason or "period_mismatch"
                    cands.append(
                        MatchCandidate(
                            kind=kind,
                            id=source_id,
                            value=sf.value,
                            unit=sf.unit,
                            period=sf_p,
                            why=f"期间不匹配：正文为 {fig_p}，来源 {source_id} 为 {sf_p}",
                        )
                    )
                continue
            if not sf_p and all_body_periods and fig_p not in all_body_periods:
                if ok_val:
                    other_p = sorted(all_body_periods)[0]
                    best_reason = best_reason or "period_mismatch"
                    cands.append(
                        MatchCandidate(
                            kind=kind,
                            id=source_id,
                            value=sf.value,
                            unit=sf.unit,
                            period=other_p,
                            why=f"期间不匹配：正文为 {fig_p}，来源 {source_id} 期间为 {other_p}",
                        )
                    )
                continue

        if ok_val:
            excerpt = str(sf.location.get("sentence") or body[:160]).strip()
            return True, excerpt[:200], None, []

        if unit_mis:
            best_reason = "unit_mismatch"
            cands.append(
                MatchCandidate(
                    kind=kind,
                    id=source_id,
                    value=sf.value,
                    unit=sf.unit,
                    period=sf_p or fig_p,
                    why=f"单位/量级不匹配：正文为 {fig.raw} ({fig.unit})，来源 {source_id} 为 {sf.raw} ({sf.unit})",
                )
            )
        else:
            best_reason = best_reason or "source_conflict"
            cands.append(
                MatchCandidate(
                    kind=kind,
                    id=source_id,
                    value=sf.value,
                    unit=sf.unit,
                    period=sf_p or fig_p,
                    why=f"数值冲突：正文为 {fig.raw}，来源 {source_id} 为 {sf.raw}",
                )
            )

    return False, "", best_reason, cands


def _match_in_provenance_record(
    fig: ExtractedFigure,
    prov: dict[str, Any],
    metric_aliases: dict[str, list[str]] | None = None,
) -> tuple[bool, str]:
    """Search ``prov`` body for ``fig`` using unit-normalized numeric comparison + period/metric check (G3.1)."""
    body = str(
        prov.get("body") or prov.get("body_inline") or prov.get("result_snippet") or ""
    )
    hit, excerpt, _, _ = _inspect_text_source(
        fig,
        body,
        source_id=str(prov.get("id") or ""),
        kind="prov",
        metric_aliases=metric_aliases,
    )
    return hit, excerpt


def read_xlsx_cell(
    xlsx_bytes: bytes, sheet_and_cell: str
) -> tuple[float | None, str | None]:
    """Read a numeric value from ``xlsx_bytes`` at ``Sheet!Cell`` with ``data_only=True``."""
    try:
        import openpyxl

        if "!" in sheet_and_cell:
            sheet_name, cell_coord = sheet_and_cell.split("!", 1)
        else:
            sheet_name, cell_coord = "", sheet_and_cell
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        val = ws[cell_coord].value
        wb.close()
        if isinstance(val, (int, float)):
            return float(val), None
        return None, f"Cell {sheet_and_cell} is non-numeric ({val!r})"
    except Exception as e:
        return None, str(e)


def _RequiresModelCellOnly(fig: ExtractedFigure) -> bool:
    """Target price, IRR, and 3-scenario returns must point to a model cell (PK.9)."""
    g = canonical_metric_group(fig.metric)
    low = fig.metric.lower()
    return g in ("target_price", "irr") or any(
        k in low for k in ("目标价", "target price", "irr", "情景收益", "三情景")
    )


def match_figures(
    figures: list[ExtractedFigure],
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_ledger: dict[str, dict[str, Any]] | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
) -> list[MatchOutcome]:
    """Match all extracted figures against evidence, model cells, and provenance records (PK.9)."""
    evidence_list = (thesis_doc or {}).get("evidence") or []
    evidence_by_id = {
        str(e["id"]): e
        for e in evidence_list
        if isinstance(e, dict) and "id" in e
    }
    prov_map = provenance_records or {}
    facts_map = facts_ledger or {}
    cells_map = model_cells or {}

    # Also index key_outputs from models.json into cells_map
    for ver in (models_doc or {}).get("versions") or []:
        if not isinstance(ver, dict):
            continue
        vpath = ver.get("path") or ""
        recalc = ver.get("recalculated", True)
        for ko in ver.get("key_outputs") or []:
            if isinstance(ko, dict) and ko.get("cell") and ko.get("value") is not None:
                key = f"{vpath}#{ko['cell']}"
                cells_map.setdefault(
                    key,
                    {
                        "value": float(ko["value"]),
                        "unit": ko.get("unit") or "other",
                        "period": normalize_period(ko.get("period")),
                        "metric": ko.get("name") or "",
                        "recalculated": recalc,
                        "sha256": ver.get("sha256") or "",
                    },
                )

    outcomes: list[MatchOutcome] = []

    def _try_match_target(
        fig: ExtractedFigure, target_ref: str
    ) -> MatchOutcome | None:
        # Order 0: facts ledger reference `f:<id>`
        if target_ref.startswith("f:"):
            fid = target_ref[2:].strip()
            frow = facts_map.get(fid)
            if not frow or not frow.get("ref"):
                return None
            return _try_match_target(fig, str(frow["ref"]))

        # Order 1: evidence:<E-id>
        if target_ref.startswith("evidence:"):
            eid = target_ref.split(":", 1)[1].strip()
            ev = evidence_by_id.get(eid)
            if not ev:
                # Fallback: try hyphen-normalized ID (e.g. E0001 <-> E-0001)
                norm_eid = eid.replace("-", "").upper()
                for k_id, v_ev in evidence_by_id.items():
                    if k_id.replace("-", "").upper() == norm_eid:
                        ev = v_ev
                        eid = k_id
                        break
            if not ev:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Evidence {eid} not found in thesis.json",
                )
            cands: list[MatchCandidate] = []
            best_reason: str | None = None
            for ef in ev.get("figures") or []:
                if not isinstance(ef, dict):
                    continue
                ef_metric = str(ef.get("metric") or "")
                ef_period = normalize_period(ef.get("period"))
                ef_val = float(ef.get("value", 0.0))
                ef_unit = str(ef.get("unit") or "other")
                ef_label = str(ef.get("label") or "")

                if not metrics_match(fig.metric, ef_metric, metric_aliases):
                    continue

                fig_p = normalize_period(fig.period)
                ok_val, unit_mis, _ = values_within_tolerance(fig, ef_val, ef_unit)

                if fig_p and ef_period and fig_p != ef_period:
                    if ok_val:
                        best_reason = "period_mismatch"
                        cands.append(
                            MatchCandidate(
                                kind="evidence",
                                id=eid,
                                value=ef_val,
                                unit=ef_unit,
                                period=ef_period,
                                why=f"期间不匹配：正文为 {fig_p}，来源 {eid} 为 {ef_period}",
                            )
                        )
                    continue

                if ok_val:
                    return MatchOutcome(
                        figure=fig,
                        matched=True,
                        kind="evidence",
                        ref=eid,
                        excerpt=str(ev.get("fact") or ""),
                    )

                if unit_mis:
                    best_reason = "unit_mismatch"
                    cands.append(
                        MatchCandidate(
                            kind="evidence",
                            id=eid,
                            value=ef_val,
                            unit=ef_unit,
                            period=ef_period,
                            why=f"单位/量级不匹配：正文为 {fig.raw} ({fig.unit})，来源 {eid} 为 {ef_val} {ef_unit}",
                        )
                    )
                else:
                    best_reason = best_reason or "source_conflict"
                    cands.append(
                        MatchCandidate(
                            kind="evidence",
                            id=eid,
                            value=ef_val,
                            unit=ef_unit,
                            period=ef_period,
                            why=f"数值冲突：正文为 {fig.raw}，来源 {eid} 为 {ef_val} {ef_unit}",
                        )
                    )

            # Also inspect evidence fact text if structured figures[] did not match
            fact_text = str(ev.get("fact") or "")
            if not cands and fact_text:
                f_hit, f_excerpt, f_reason, f_cands = _inspect_text_source(
                    fig,
                    fact_text,
                    source_id=eid,
                    kind="evidence",
                    metric_aliases=metric_aliases,
                )
                if f_hit:
                    return MatchOutcome(
                        figure=fig,
                        matched=True,
                        kind="evidence",
                        ref=eid,
                        excerpt=f_excerpt or fact_text,
                    )
                if f_cands:
                    cands.extend(f_cands)
                    best_reason = best_reason or f_reason

            if cands:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason=best_reason or "source_conflict",
                    candidates=cands,
                )
            return None

        # Order 2: model:<path>#<Sheet>!<Cell>
        if target_ref.startswith("model:"):
            m_spec = target_ref.split(":", 1)[1].strip()
            cell_info = cells_map.get(m_spec)
            if not cell_info:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Model cell {m_spec} not found",
                )
            if cell_info.get("recalculated") is False:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Model {m_spec} has recalculated=false in models.json; run recalc.py first",
                )
            c_val = float(cell_info["value"])
            c_unit = str(cell_info.get("unit") or fig.unit)
            c_period = normalize_period(cell_info.get("period"))
            fig_p = normalize_period(fig.period)
            ok_val, unit_mis, _ = values_within_tolerance(fig, c_val, c_unit)
            if fig_p and c_period and fig_p != c_period:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="period_mismatch",
                    candidates=[
                        MatchCandidate(
                            kind="model",
                            id=m_spec,
                            value=c_val,
                            unit=c_unit,
                            period=c_period,
                            why=f"模型单元格期间为 {c_period}，正文为 {fig_p}",
                        )
                    ],
                )
            if ok_val:
                return MatchOutcome(
                    figure=fig,
                    matched=True,
                    kind="model",
                    ref=m_spec,
                    excerpt=f"{m_spec} = {c_val} {c_unit}",
                    source_sha256=str(cell_info.get("sha256") or ""),
                )
            if unit_mis:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unit_mismatch",
                    candidates=[
                        MatchCandidate(
                            kind="model",
                            id=m_spec,
                            value=c_val,
                            unit=c_unit,
                            period=c_period,
                            why=f"单位不匹配：模型 {m_spec} 为 {c_val} {c_unit}",
                        )
                    ],
                )
            return MatchOutcome(
                figure=fig,
                matched=False,
                reason="source_conflict",
                candidates=[
                    MatchCandidate(
                        kind="model",
                        id=m_spec,
                        value=c_val,
                        unit=c_unit,
                        period=c_period,
                        why=f"数值超容差：模型 {m_spec} 为 {c_val} {c_unit}",
                    )
                ],
            )

        # Order 3: prov:<record_id>
        if target_ref.startswith("prov:"):
            pid = target_ref.split(":", 1)[1].strip()
            prec = prov_map.get(pid)
            if not prec:
                # Prefix lookup if short sha/uuid was used in footnote
                for k, v in prov_map.items():
                    if k.startswith(pid):
                        prec = v
                        pid = k
                        break
            if not prec:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Provenance record {pid} not found",
                )
            body_text = str(
                prec.get("body")
                or prec.get("body_inline")
                or prec.get("result_snippet")
                or ""
            )
            hit, excerpt, prov_reason, prov_cands = _inspect_text_source(
                fig,
                body_text,
                source_id=pid,
                kind="prov",
                metric_aliases=metric_aliases,
            )
            if hit:
                return MatchOutcome(
                    figure=fig,
                    matched=True,
                    kind="prov",
                    ref=pid,
                    excerpt=excerpt,
                    source_sha256=str(prec.get("result_sha256") or ""),
                )
            return MatchOutcome(
                figure=fig,
                matched=False,
                reason=prov_reason or "source_conflict",
                hint=f"Provenance {pid} body does not match {fig.raw} ({fig.unit}) for {fig.period}",
                candidates=prov_cands,
            )

        return None

    for fig in figures:
        if fig.exempt:
            outcomes.append(
                MatchOutcome(figure=fig, matched=True, kind="exempt", ref=fig.exempt_reason)
            )
            continue

        # Special rule: Target price / IRR / 3-scenario returns can ONLY match model cells
        if _RequiresModelCellOnly(fig):
            if fig.footnote_target and fig.footnote_target.startswith("model:"):
                out = _try_match_target(fig, fig.footnote_target)
                if out and out.matched:
                    outcomes.append(out)
                    continue
            # Try auto-searching model cells for target price
            matched_model_out: MatchOutcome | None = None
            for m_spec, c_info in cells_map.items():
                if metrics_match(fig.metric, str(c_info.get("metric") or ""), metric_aliases):
                    cand_out = _try_match_target(fig, f"model:{m_spec}")
                    if cand_out and cand_out.matched:
                        matched_model_out = cand_out
                        break
            if matched_model_out:
                outcomes.append(matched_model_out)
            else:
                outcomes.append(
                    MatchOutcome(
                        figure=fig,
                        matched=False,
                        reason="unverified",
                        hint="目标价、IRR、三情景收益必须指向模型单元格（model:<path>#<Sheet>!<Cell>）",
                    )
                )
            continue

        # Step C: Explicit footnote matching (Orders 0, 1, 2, 3)
        # Deterministic contradiction (`source_conflict`, `unit_mismatch`, `period_mismatch`) is ONLY
        # emitted here when an explicit footnote points to a source whose value genuinely differs (L28).
        if fig.footnote_target:
            out = _try_match_target(fig, fig.footnote_target)
            if out is not None:
                outcomes.append(out)
                continue

        # Order 0b: Check facts ledger by metric + period + value even without f:<id> footnote
        facts_outcome: MatchOutcome | None = None
        for fid, frow in facts_map.items():
            if metrics_match(fig.metric, str(frow.get("metric") or ""), metric_aliases):
                fp = normalize_period(frow.get("period"))
                if not fig.period or not fp or normalize_period(fig.period) == fp:
                    ok_v, _, _ = values_within_tolerance(
                        fig, float(frow["value"]), str(frow.get("unit") or fig.unit)
                    )
                    if ok_v and frow.get("ref"):
                        facts_outcome = _try_match_target(fig, str(frow["ref"]))
                        if facts_outcome and facts_outcome.matched:
                            break
        if facts_outcome and facts_outcome.matched:
            outcomes.append(facts_outcome)
            continue

        # Order 4: Unfootnoted auto-search across thesis.evidence, thesis.market_view, model_cells, and provenance.
        # Per P0H §1.2 Step C & L28: unfootnoted figures that do not match here NEVER produce `contradicted`
        # (`source_conflict`, `unit_mismatch`, `period_mismatch`); they are marked `unverified` with candidate
        # sources attached so Step D (`audit.judge` + code anchoring) decides.
        auto_candidates: list[MatchCandidate] = []
        matched_auto: MatchOutcome | None = None
        has_meaningful_metric = bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", fig.metric or ""))

        if has_meaningful_metric:
            # 4a. Search thesis.evidence
            for eid in evidence_by_id:
                cand = _try_match_target(fig, f"evidence:{eid}")
                if cand is not None:
                    if cand.matched:
                        matched_auto = cand
                        break
                    auto_candidates.extend(cand.candidates)

            # 4b. Search thesis.market_view.figures
            if not matched_auto:
                mv_figs = ((thesis_doc or {}).get("market_view") or {}).get("figures") or []
                for mvf in mv_figs:
                    if not isinstance(mvf, dict) or mvf.get("value") is None:
                        continue
                    if not metrics_match(fig.metric, str(mvf.get("metric") or ""), metric_aliases):
                        continue
                    mv_p = normalize_period(mvf.get("period"))
                    fig_p = normalize_period(fig.period)
                    if fig_p and mv_p and fig_p != mv_p:
                        continue
                    ok_v, _, _ = values_within_tolerance(
                        fig, float(mvf["value"]), str(mvf.get("unit") or fig.unit)
                    )
                    if ok_v:
                        matched_auto = MatchOutcome(
                            figure=fig,
                            matched=True,
                            kind="prov",
                            ref="thesis:market_view",
                            excerpt=f"market_view: {mvf.get('metric')} {mvf.get('period')} = {mvf.get('value')} {mvf.get('unit')}",
                        )
                        break

            # 4c. Search model cells
            if not matched_auto:
                for m_spec, c_info in cells_map.items():
                    if metrics_match(fig.metric, str(c_info.get("metric") or ""), metric_aliases):
                        cand = _try_match_target(fig, f"model:{m_spec}")
                        if cand is not None:
                            if cand.matched:
                                matched_auto = cand
                                break
                            auto_candidates.extend(cand.candidates)

            # 4d. Search provenance records
            if not matched_auto:
                for pid, prec in prov_map.items():
                    body_text = str(
                        prec.get("body")
                        or prec.get("body_inline")
                        or prec.get("result_snippet")
                        or ""
                    )
                    hit, excerpt, _, prov_cands = _inspect_text_source(
                        fig,
                        body_text,
                        source_id=pid,
                        kind="prov",
                        metric_aliases=metric_aliases,
                    )
                    if hit:
                        matched_auto = MatchOutcome(
                            figure=fig,
                            matched=True,
                            kind="prov",
                            ref=pid,
                            excerpt=excerpt,
                            source_sha256=str(prec.get("result_sha256") or ""),
                        )
                        break
                    if prov_cands:
                        auto_candidates.extend(prov_cands)

        if matched_auto:
            outcomes.append(matched_auto)
            continue

        outcomes.append(
            MatchOutcome(
                figure=fig,
                matched=False,
                reason="unverified",
                hint="请补充 evidence:/model:/prov: 脚注或核对数值、单位与期间",
                candidates=auto_candidates[:5],
            )
        )

    # Order 5: Second pass for derived figures (rates, margins, multiples) recomputable
    # from two already-matched figures on the same line or within ±5 lines
    matched_by_line: list[ExtractedFigure] = [
        o.figure for o in outcomes if o.matched and o.kind != "exempt"
    ]
    for idx, out in enumerate(outcomes):
        if (
            out.matched
            or out.reason in ("source_conflict", "unit_mismatch", "period_mismatch")
            or out.figure.unit not in ("pct", "x")
        ):
            continue
        fig = out.figure
        nearby = [
            m
            for m in matched_by_line
            if m.id != fig.id
            and abs(m.location.get("line", 0) - fig.location.get("line", 0)) <= 5
        ]
        recomputed_pair: tuple[str, str] | None = None
        for i in range(len(nearby)):
            for j in range(len(nearby)):
                if i == j:
                    continue
                a = nearby[i].scaled_value
                b = nearby[j].scaled_value
                if b == 0:
                    continue
                if fig.unit == "pct":
                    tol_pct = max(0.05, 0.5 * (10.0 ** (-fig.decimals)), abs(fig.value) * 0.01)
                    for derived_pct in ((a / b) * 100.0, ((a - b) / b) * 100.0):
                        if abs(fig.value - derived_pct) <= tol_pct * 1.0001:
                            recomputed_pair = (nearby[i].id, nearby[j].id)
                            break
                elif fig.unit == "x":
                    derived_x = a / b
                    ok_d, _, _ = values_within_tolerance(fig, derived_x, "x")
                    if ok_d:
                        recomputed_pair = (nearby[i].id, nearby[j].id)
                        break
                if recomputed_pair:
                    break
            if recomputed_pair:
                break

        if recomputed_pair:
            outcomes[idx] = MatchOutcome(
                figure=fig,
                matched=True,
                kind="derived",
                ref=f"{recomputed_pair[0]},{recomputed_pair[1]}",
                excerpt=f"Derived from matched figures {recomputed_pair[0]} and {recomputed_pair[1]}",
            )

    return outcomes


def retrieve_candidate_sources(
    fig: ExtractedFigure,
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_ledger: dict[str, dict[str, Any]] | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Retrieve up to ``limit`` (default 5) candidate source snippets for ``fig`` (G3.1 / P0H Step D)."""
    scored: list[tuple[int, dict[str, Any]]] = []
    fallback_pool: list[dict[str, Any]] = []
    fig_p = normalize_period(fig.period)
    fn_target = (fig.footnote_target or "").strip()

    # 1. thesis.json evidence[]
    for ev in (thesis_doc or {}).get("evidence") or []:
        if not isinstance(ev, dict) or "id" not in ev:
            continue
        eid = str(ev["id"])
        score = 0
        if fn_target == f"evidence:{eid}" or fn_target.replace("-", "").upper() == f"EVIDENCE:{eid.replace('-', '').upper()}":
            score += 20
        fact_str = str(ev.get("fact") or "")
        fig_lines: list[str] = []
        for ef in ev.get("figures") or []:
            if not isinstance(ef, dict):
                continue
            ef_m = str(ef.get("metric") or "")
            ef_p = normalize_period(ef.get("period"))
            if fig.metric and ef_m and metrics_match(fig.metric, ef_m, metric_aliases):
                score += 5
            if fig_p and ef_p and fig_p == ef_p:
                score += 3
            fig_lines.append(
                f"{ef_m} ({ef.get('period') or ''}): {ef.get('value')} {ef.get('unit') or ''}"
            )
        if fig.metric and fact_str and metrics_match(fig.metric, fact_str, metric_aliases):
            score += 3
        if fig_p and fig_p.lower() in fact_str.lower():
            score += 2
        combined_text = fact_str
        if fig_lines:
            combined_text = f"{fact_str} | Figures: {'; '.join(fig_lines)}".strip(" |")
        entry = {
            "source_id": eid,
            "ref": f"evidence:{eid}",
            "kind": "evidence",
            "text": combined_text,
        }
        if score > 0:
            scored.append((score, entry))
        elif combined_text:
            fallback_pool.append(entry)

    # 1b. thesis.json market_view.figures[]
    mv_figs = ((thesis_doc or {}).get("market_view") or {}).get("figures") or []
    if mv_figs:
        mv_lines: list[str] = []
        mv_score = 0
        for mvf in mv_figs:
            if not isinstance(mvf, dict) or mvf.get("value") is None:
                continue
            m_name = str(mvf.get("metric") or "")
            m_per = normalize_period(mvf.get("period"))
            if fig.metric and m_name and metrics_match(fig.metric, m_name, metric_aliases):
                mv_score += 5
            if fig_p and m_per and fig_p == m_per:
                mv_score += 3
            mv_lines.append(f"{m_name} ({mvf.get('period') or ''}): {mvf.get('value')} {mvf.get('unit') or ''}")
        if mv_lines:
            mv_entry = {
                "source_id": "thesis:market_view",
                "ref": "thesis:market_view",
                "kind": "prov",
                "text": "Market consensus figures: " + "; ".join(mv_lines),
            }
            if mv_score > 0:
                scored.append((mv_score, mv_entry))
            else:
                fallback_pool.append(mv_entry)

    # 2. models.json + model_cells
    merged_cells = dict(model_cells or {})
    for ver in (models_doc or {}).get("versions") or []:
        if not isinstance(ver, dict):
            continue
        vpath = ver.get("path") or ""
        for ko in ver.get("key_outputs") or []:
            if isinstance(ko, dict) and ko.get("cell") and ko.get("value") is not None:
                key = f"{vpath}#{ko['cell']}"
                merged_cells.setdefault(
                    key,
                    {
                        "value": float(ko["value"]),
                        "unit": ko.get("unit") or "other",
                        "period": normalize_period(ko.get("period")),
                        "metric": ko.get("name") or "",
                    },
                )
    for m_spec, c_info in merged_cells.items():
        score = 0
        if fn_target == f"model:{m_spec}":
            score += 20
        cm = str(c_info.get("metric") or "")
        cp = normalize_period(c_info.get("period"))
        if fig.metric and cm and metrics_match(fig.metric, cm, metric_aliases):
            score += 5
        if fig_p and cp and fig_p == cp:
            score += 3
        if score > 0:
            scored.append(
                (
                    score,
                    {
                        "source_id": m_spec,
                        "ref": f"model:{m_spec}",
                        "kind": "model",
                        "text": f"{m_spec} ({cm} {cp}): {c_info.get('value')} {c_info.get('unit') or ''}".strip(),
                    },
                )
            )

    # 3. provenance_records
    for pid, prec in (provenance_records or {}).items():
        if not isinstance(prec, dict):
            continue
        score = 0
        if fn_target == f"prov:{pid}":
            score += 20
        body = str(
            prec.get("body")
            or prec.get("body_inline")
            or prec.get("result_snippet")
            or ""
        )
        if not body:
            continue
        if fig.metric and metrics_match(fig.metric, body, metric_aliases):
            score += 4
        if fig_p and fig_p.lower() in body.lower():
            score += 2
        p_entry = {
            "source_id": pid,
            "ref": f"prov:{pid}",
            "kind": "prov",
            "text": body[:1000],
        }
        if score > 0:
            scored.append((score, p_entry))
        else:
            fallback_pool.append(p_entry)

    # 4. facts_ledger
    for fid, frow in (facts_ledger or {}).items():
        if not isinstance(frow, dict):
            continue
        score = 0
        if fn_target == f"f:{fid}":
            score += 20
        fm = str(frow.get("metric") or "")
        fp = normalize_period(frow.get("period"))
        if fig.metric and fm and metrics_match(fig.metric, fm, metric_aliases):
            score += 5
        if fig_p and fp and fig_p == fp:
            score += 3
        if score > 0:
            scored.append(
                (
                    score,
                    {
                        "source_id": fid,
                        "ref": f"f:{fid}",
                        "kind": "facts",
                        "text": f"{fm} ({fp}): {frow.get('value')} {frow.get('unit') or ''} (ref={frow.get('ref') or ''})",
                    },
                )
            )

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        return [item[1] for item in scored[:limit]]
    return fallback_pool[:limit]

