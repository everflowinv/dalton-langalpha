"""Deterministic figure-to-source matching and discrepancy classification (PK.9)."""

from __future__ import annotations

import dataclasses
import io
import re
from typing import Any

from src.dalton.publish.numbers import (
    ExtractedFigure,
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
    "per_share": (1.0, "per_share"),
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
    lock_unverified: bool = False


def _is_legacy_thesis_evidence(ev: dict[str, Any]) -> bool:
    """Return True if an evidence item comes from migrated legacy thesis (P0I §1.3, L34)."""
    src = ev.get("source")
    if not isinstance(src, dict):
        return False
    if str(src.get("type") or "").strip().lower() == "legacy_thesis":
        return True
    ref = src.get("ref")
    if isinstance(ref, dict):
        rpath = str(ref.get("path") or "").strip().replace("\\", "/")
        if rpath.startswith("sources/legacy/"):
            return True
    return False


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


def _are_dimensions_compatible(
    fig: ExtractedFigure,
    fig_base: float,
    fig_dim: str,
    src_unit: str,
    src_scale: float,
    src_base: float,
    src_dim: str,
) -> bool:
    """Return True if draft figure and source figure have compatible dimensions (P0I §1.5 #1, L36)."""
    if fig_dim == src_dim:
        return True
    # Allow scaled currency to match scaled count (e.g. '$63.4bn' vs '63.4 bn' in English prose)
    if (
        (fig_dim in ("USD", "CNY", "HKD") and src_dim == "count")
        or (src_dim in ("USD", "CNY", "HKD") and fig_dim == "count")
    ) and abs(fig_base) >= 1e4 and abs(src_base) >= 1e4:
        return True
    # Allow per_share to match unscaled currency (e.g. '2.12 美元/股' vs '2.12 USD' in SEC/model tables)
    src_mult = UNIT_BASE.get(src_unit, (1.0, "other"))[0]
    fig_mult = UNIT_BASE.get(fig.unit, (1.0, "other"))[0]
    if (
        fig_dim == "per_share"
        and src_dim in ("USD", "CNY", "HKD")
        and src_scale == 1.0
        and src_mult == 1.0
    ):
        return True
    if (
        src_dim == "per_share"
        and fig_dim in ("USD", "CNY", "HKD")
        and fig.scale == 1.0
        and fig_mult == 1.0
    ):
        return True
    # Pure unscaled numbers ('count' vs 'other' when both unscaled)
    if (
        fig_dim in ("count", "other")
        and src_dim in ("count", "other")
        and fig.scale == 1.0
        and src_scale == 1.0
        and fig_mult == 1.0
        and src_mult == 1.0
    ):
        return True
    return False


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

    # Half-ULP tolerance in the figure's displayed precision; 0.05 pct tolerance ONLY when both are pct
    ulp_half = 0.5 * (10.0 ** (-fig.decimals))
    if fig_dim == "pct" and src_dim == "pct":
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

    dims_compatible = _are_dimensions_compatible(
        fig, fig_base, fig_dim, src_unit, src_scale, src_base, src_dim
    )

    if dims_compatible:
        diff_base = abs(fig_base - src_base)
        if diff_base <= tol_base * 1.0001:
            return True, False, 0.0

    # Percentage vs decimal ratio (e.g. 21% vs 0.21)
    if fig_dim == "pct" and src_dim == "other" and 0 < abs(src_value) <= 1.0:
        tol_pct = max(0.05, ulp_half)
        if abs(fig.value - src_value * 100.0) <= tol_pct * 1.0001:
            return True, False, 0.0

    # Chinese prose '翻倍' (2.0x) vs '+100%' (100 pct)
    if fig.raw in ("翻倍", "翻一番") and src_dim == "pct" and abs(src_value - 100.0) <= 0.5:
        return True, False, 0.0

    # Direct unscaled number match ONLY when source unit is identical to figure unit and same scale
    if abs(fig.value - src_value) <= tol_display * 1.0001:
        if fig.unit == src_unit and fig.scale == (src_scale if src_scale > 1.0 else fig.scale):
            return True, False, 0.0
        return False, True, abs(fig_base - src_base)

    # Check if ratio is a power of 10 (e.g. 512百万 vs 512亿, or mn vs bn) -> unit/scale mismatch
    if dims_compatible and fig_base != 0 and src_base != 0:
        ratio = abs(fig_base / src_base)
        for p10 in (1e-6, 1e-4, 1e-3, 1e-2, 1e-1, 1e1, 1e2, 1e3, 1e4, 1e6):
            if abs(ratio - p10) / p10 <= 0.01:
                return False, True, abs(fig_base - src_base)

    if not dims_compatible and abs(fig.value - src_value) <= max(0.05, ulp_half) * 1.0001:
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


class SourceParseCache:
    """Parsed source bodies for one audit run.

    Every draft figure is compared against every source body, so parsing a body per
    comparison made the audit O(figures x sources) full parses: 784 figures against 43
    sources took about six minutes. One instance lives for one audit and is passed
    down; nothing is kept between requests. Callers only read the parsed figures.
    """

    def __init__(self) -> None:
        self._figures: dict[str, tuple[ExtractedFigure, ...]] = {}
        self._periods: dict[str, frozenset[str]] = {}

    def figures(self, body: str) -> tuple[ExtractedFigure, ...]:
        hit = self._figures.get(body)
        if hit is None:
            hit = self._figures[body] = tuple(extract_figures(body))
        return hit

    def periods(self, body: str) -> frozenset[str]:
        hit = self._periods.get(body)
        if hit is None:
            hit = self._periods[body] = frozenset(
                normalize_period(pm.group(1)) for pm in _PERIOD_TOKEN_RE.finditer(body)
            ) - {""}
        return hit


def _inspect_text_source(
    fig: ExtractedFigure,
    body: str,
    *,
    source_id: str = "",
    kind: str = "prov",
    metric_aliases: dict[str, list[str]] | None = None,
    source_cache: SourceParseCache | None = None,
) -> tuple[bool, str, str | None, str, list[MatchCandidate]]:
    """Unit-normalized numeric comparison in a text body with period and metric compatibility (G3.1 R4-3 / P0I L36).

    Returns ``(matched, excerpt, failure_reason, failure_hint, candidates)``.
    """
    if not body or not body.strip():
        return False, "", None, "", []

    cache = source_cache if source_cache is not None else SourceParseCache()
    src_figures = cache.figures(body)
    fig_p = normalize_period(fig.period)
    all_body_periods = cache.periods(body)

    cands: list[MatchCandidate] = []
    best_reason: str | None = None
    best_hint: str = ""

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

        # Period compatibility check (L36: period mismatch is unverified, NOT contradicted)
        if fig_p:
            if sf_p and sf_p != fig_p:
                if ok_val:
                    best_reason = best_reason or "unverified"
                    best_hint = best_hint or "脚注指向的证据期间与正文不一致"
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
                    best_reason = best_reason or "unverified"
                    best_hint = best_hint or "脚注指向的证据期间与正文不一致"
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
            return True, excerpt[:200], None, "", []

        fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale)
        sf_base, sf_dim = _to_base(sf.value, sf.unit, sf.scale)
        dims_ok = _are_dimensions_compatible(
            fig, fig_base, fig_dim, sf.unit, sf.scale, sf_base, sf_dim
        )

        if unit_mis or not dims_ok:
            best_reason = best_reason or "unverified"
            best_hint = best_hint or "脚注指向的证据量纲与正文不一致"
            cands.append(
                MatchCandidate(
                    kind=kind,
                    id=source_id,
                    value=sf.value,
                    unit=sf.unit,
                    period=sf_p or fig_p,
                    why=f"单位/量纲不匹配：正文为 {fig.raw} ({fig.unit})，来源 {source_id} 为 {sf.raw} ({sf.unit})",
                )
            )
        else:
            best_reason = "source_conflict"
            best_hint = f"数值与来源 {source_id} 冲突"
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

    return False, "", best_reason, best_hint, cands


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


def apply_derived_figure_matching(outcomes: list[MatchOutcome]) -> list[MatchOutcome]:
    """Order 5: Match derived figures (rates, margins, multiples) recomputable from two already-verified figures (P0I §1.2, L34).

    Only figures verified via Order 0–3 footnotes or Step D (`audit.judge` + code anchoring) may serve as inputs.
    """
    matched_by_line: list[ExtractedFigure] = [
        o.figure
        for o in outcomes
        if o.matched and o.kind not in ("exempt", "derived")
    ]
    for idx, out in enumerate(outcomes):
        if (
            out.matched
            or out.lock_unverified
            or bool(out.figure.missing_citations)
            or bool(out.figure.duplicate_citations)
            or out.figure.exempt
            or out.figure.label_hint == "incidental"
            or out.reason == "source_conflict"
            or out.figure.unit not in ("pct", "x")
        ):
            continue
        fig = out.figure
        fig_p = normalize_period(fig.period)
        if not fig_p:
            continue
        nearby = [
            m
            for m in matched_by_line
            if m.id != fig.id
            and m.unit not in ("pct", "bp", "bps", "x")
            and (
                m.location.get("line", 0) == fig.location.get("line", 0)
                or (
                    abs(m.location.get("line", 0) - fig.location.get("line", 0)) <= 1
                    and not str(fig.location.get("sentence") or "").lstrip().startswith(("-", "*", "+"))
                )
            )
        ]
        recomputed_pair: tuple[str, str] | None = None
        for i in range(len(nearby)):
            for j in range(len(nearby)):
                if i == j:
                    continue
                a, dim_a = _to_base(nearby[i].value, nearby[i].unit, nearby[i].scale)
                b, dim_b = _to_base(nearby[j].value, nearby[j].unit, nearby[j].scale)
                if b == 0 or dim_a != dim_b:
                    continue
                pa = normalize_period(nearby[i].period)
                pb = normalize_period(nearby[j].period)
                same_period_as_fig = bool(pa and pb and pa == fig_p and pb == fig_p)
                if fig.unit == "pct":
                    tol_pct = max(0.05, 0.5 * (10.0 ** (-fig.decimals)))
                    candidates_pct: list[float] = []
                    if same_period_as_fig:
                        candidates_pct.append((a / b) * 100.0)
                    if (
                        fig.label_hint == "derived"
                        and pa
                        and pb
                        and pa != pb
                        and canonical_metric_group(nearby[i].metric)
                        == canonical_metric_group(nearby[j].metric)
                    ):
                        candidates_pct.append(((a - b) / b) * 100.0)
                    for derived_pct in candidates_pct:
                        if abs(fig.value - derived_pct) <= tol_pct * 1.0001:
                            recomputed_pair = (nearby[i].id, nearby[j].id)
                            break
                elif fig.unit == "x" and same_period_as_fig:
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


def match_figures(
    figures: list[ExtractedFigure],
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_ledger: dict[str, dict[str, Any]] | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
    source_cache: SourceParseCache | None = None,
) -> list[MatchOutcome]:
    """Step C: Match footnoted figures (Orders 0–3) and collect candidates for unfootnoted figures (P0I §1.2, L34, L36).

    Unfootnoted figures NEVER become `matched=True` here; Order 0b and Order 4 only produce candidates for Step D.
    """
    if source_cache is None:
        source_cache = SourceParseCache()
    evidence_list = (thesis_doc or {}).get("evidence") or []
    evidence_by_id = {
        str(e["id"]): e
        for e in evidence_list
        if isinstance(e, dict) and "id" in e
    }
    prov_map = provenance_records or {}
    facts_map = facts_ledger or {}
    cells_map = dict(model_cells or {})

    # Index key_outputs from models.json into cells_map
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
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Facts ledger entry {fid} not found",
                    lock_unverified=True,
                )
            return _try_match_target(fig, str(frow["ref"]))

        # Order 1: evidence:<E-id>
        if target_ref.startswith("evidence:"):
            eid = target_ref.split(":", 1)[1].strip()
            ev = evidence_by_id.get(eid)
            if not ev:
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
                    lock_unverified=True,
                )
            # P0I §1.3 / L34: legacy_thesis cannot verify figures
            if _is_legacy_thesis_evidence(ev):
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint="旧认知中的说法，尚未用原始来源核实",
                    candidates=[
                        MatchCandidate(
                            kind="legacy_thesis",
                            id=eid,
                            value=fig.value,
                            unit=fig.unit,
                            period=fig.period,
                            why="旧认知中的说法，尚未用原始来源核实",
                        )
                    ],
                )

            cands: list[MatchCandidate] = []
            has_source_conflict = False
            mismatch_hint = ""
            for ef in ev.get("figures") or []:
                if not isinstance(ef, dict):
                    continue
                ef_metric = str(ef.get("metric") or "")
                ef_period = normalize_period(ef.get("period"))
                ef_val = float(ef.get("value", 0.0))
                ef_unit = str(ef.get("unit") or "other")

                if not metrics_match(fig.metric, ef_metric, metric_aliases):
                    continue

                fig_p = normalize_period(fig.period)
                ok_val, unit_mis, _ = values_within_tolerance(fig, ef_val, ef_unit)

                if fig_p and ef_period and fig_p != ef_period:
                    if ok_val:
                        mismatch_hint = mismatch_hint or "脚注指向的证据期间与正文不一致"
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

                fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale)
                ef_base, ef_dim = _to_base(ef_val, ef_unit, 1.0)
                dims_ok = _are_dimensions_compatible(
                    fig, fig_base, fig_dim, ef_unit, 1.0, ef_base, ef_dim
                )

                if unit_mis or not dims_ok:
                    mismatch_hint = mismatch_hint or "脚注指向的证据量纲与正文不一致"
                    cands.append(
                        MatchCandidate(
                            kind="evidence",
                            id=eid,
                            value=ef_val,
                            unit=ef_unit,
                            period=ef_period,
                            why=f"单位/量纲不匹配：正文为 {fig.raw} ({fig.unit})，来源 {eid} 为 {ef_val} {ef_unit}",
                        )
                    )
                else:
                    has_source_conflict = True
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
                f_hit, f_excerpt, f_reason, f_hint, f_cands = _inspect_text_source(
                    fig,
                    fact_text,
                    source_id=eid,
                    kind="evidence",
                    metric_aliases=metric_aliases,
                    source_cache=source_cache,
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
                    if f_reason == "source_conflict":
                        has_source_conflict = True
                    mismatch_hint = mismatch_hint or f_hint

            if cands:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="source_conflict" if has_source_conflict else "unverified",
                    hint=mismatch_hint if not has_source_conflict else f"数值与来源 {eid} 冲突",
                    candidates=cands,
                )
            return None

        # Order 2: model:<path>#<Sheet>!<Cell> or model:<path>!<Sheet>!<Cell>
        if target_ref.startswith("model:"):
            m_spec = target_ref.split(":", 1)[1].strip()
            cell_info = (
                cells_map.get(m_spec)
                or cells_map.get(m_spec.replace("!", "#", 1))
                or cells_map.get(m_spec.replace("#", "!", 1))
            )
            if not cell_info:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Model cell {m_spec} not found",
                    lock_unverified=True,
                )
            if cell_info.get("recalculated") is False:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Model {m_spec} has recalculated=false in models.json; run recalc.py first",
                    lock_unverified=True,
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
                    reason="unverified",
                    hint="脚注指向的证据期间与正文不一致",
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
            fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale)
            c_base, c_dim = _to_base(c_val, c_unit, 1.0)
            dims_ok = _are_dimensions_compatible(
                fig, fig_base, fig_dim, c_unit, 1.0, c_base, c_dim
            )
            if unit_mis or not dims_ok:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint="脚注指向的证据量纲与正文不一致",
                    candidates=[
                        MatchCandidate(
                            kind="model",
                            id=m_spec,
                            value=c_val,
                            unit=c_unit,
                            period=c_period,
                            why=f"单位/量纲不匹配：模型 {m_spec} 为 {c_val} {c_unit}",
                        )
                    ],
                )
            return MatchOutcome(
                figure=fig,
                matched=False,
                reason="source_conflict",
                hint=f"数值与模型单元格 {m_spec} 冲突",
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
                    lock_unverified=True,
                )
            body_text = str(
                prec.get("body")
                or prec.get("body_inline")
                or prec.get("result_snippet")
                or ""
            )
            hit, excerpt, prov_reason, prov_hint, prov_cands = _inspect_text_source(
                fig,
                body_text,
                source_id=pid,
                kind="prov",
                metric_aliases=metric_aliases,
                source_cache=source_cache,
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
                reason="source_conflict" if prov_reason == "source_conflict" else "unverified",
                hint=prov_hint or f"Provenance {pid} body does not match {fig.raw} ({fig.unit}) for {fig.period}",
                candidates=prov_cands,
            )

        return None

    def _merge_failed_outcomes(
        fig: ExtractedFigure, failed_outs: list[MatchOutcome]
    ) -> MatchOutcome:
        if len(failed_outs) == 1:
            return failed_outs[0]
        has_conflict = any(o.reason == "source_conflict" for o in failed_outs)
        locked = any(o.lock_unverified for o in failed_outs)
        hints: list[str] = []
        cands: list[MatchCandidate] = []
        seen_cands: set[tuple[str, str, float]] = set()
        for o in failed_outs:
            if o.hint and o.hint not in hints:
                hints.append(o.hint)
            for c in o.candidates:
                key = (c.kind, c.id, c.value)
                if key not in seen_cands:
                    seen_cands.add(key)
                    cands.append(c)
        return MatchOutcome(
            figure=fig,
            matched=False,
            reason="source_conflict" if has_conflict else "unverified",
            hint="；".join(hints) if hints else failed_outs[0].hint,
            candidates=cands,
            lock_unverified=locked,
        )

    for fig in figures:
        if fig.exempt:
            outcomes.append(
                MatchOutcome(figure=fig, matched=True, kind="exempt", ref=fig.exempt_reason)
            )
            continue

        if fig.label_hint == "incidental":
            outcomes.append(
                MatchOutcome(figure=fig, matched=False, kind="incidental", reason="incidental")
            )
            continue

        targets = (
            list(fig.footnote_targets)
            if fig.footnote_targets
            else ([fig.footnote_target] if fig.footnote_target else [])
        )
        target_evals: list[tuple[str, MatchOutcome | None]] = [
            (t, _try_match_target(fig, t)) for t in targets
        ]

        # Inspect ALL citations on the figure up front: missing, duplicate, or non-existent target locks unverified.
        broken_hints: list[str] = []
        broken_cands: list[MatchCandidate] = []
        if fig.duplicate_citations:
            broken_hints.append(
                f"{'、'.join(fig.duplicate_citations)} 在引用表中重复定义"
            )
        if fig.missing_citations:
            broken_hints.append(
                f"{'、'.join(fig.missing_citations)} 在引用表中没有对应项"
            )
        for _t, out in target_evals:
            if out is not None and out.lock_unverified:
                if out.hint and out.hint not in broken_hints:
                    broken_hints.append(out.hint)
                for c in out.candidates:
                    broken_cands.append(c)
        if broken_hints:
            outcomes.append(
                MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint="；".join(broken_hints),
                    candidates=broken_cands,
                    lock_unverified=True,
                )
            )
            continue

        # House target price / IRR / 3-scenario returns with explicit footnote must point to model cells
        if _RequiresModelCellOnly(fig) and targets:
            model_evals = [
                (t, out) for t, out in target_evals if t.startswith("model:")
            ]
            if model_evals:
                model_outs: list[MatchOutcome] = []
                matched_model_out: MatchOutcome | None = None
                for _t, out in model_evals:
                    if out is not None:
                        if out.matched:
                            matched_model_out = out
                            break
                        model_outs.append(out)
                if matched_model_out is not None:
                    outcomes.append(matched_model_out)
                    continue
                if model_outs:
                    outcomes.append(_merge_failed_outcomes(fig, model_outs))
                    continue
            outcomes.append(
                MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint="目标价、IRR、三情景收益必须指向模型单元格（model:<path>#<Sheet>!<Cell>）",
                    lock_unverified=True,
                )
            )
            continue

        # Step C: Explicit footnote / citation matching (Orders 0, 1, 2, 3)
        if targets:
            matched_out: MatchOutcome | None = None
            failed_outs: list[MatchOutcome] = []
            for _t, out in target_evals:
                if out is not None:
                    if out.matched:
                        matched_out = out
                        break
                    failed_outs.append(out)
            if matched_out is not None:
                outcomes.append(matched_out)
                continue
            if failed_outs:
                outcomes.append(_merge_failed_outcomes(fig, failed_outs))
                continue
            outcomes.append(
                MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"脚注来源 {targets[0]} 中未找到匹配指标或数值",
                )
            )
            continue

        # Order 0b & Order 4 (P0I §1.2 / L34): Unfootnoted figures NEVER become `matched=True` here;
        # auto-search only collects candidate sources for Step D (`audit.judge` + code anchoring).
        auto_candidates: list[MatchCandidate] = []
        legacy_hint = ""

        # Order 0b: facts ledger candidates
        for fid, frow in facts_map.items():
            if metrics_match(fig.metric, str(frow.get("metric") or ""), metric_aliases):
                fp = normalize_period(frow.get("period"))
                if not fig.period or not fp or normalize_period(fig.period) == fp:
                    ok_v, _, _ = values_within_tolerance(
                        fig, float(frow["value"]), str(frow.get("unit") or fig.unit)
                    )
                    if ok_v:
                        auto_candidates.append(
                            MatchCandidate(
                                kind="facts",
                                id=fid,
                                value=float(frow["value"]),
                                unit=str(frow.get("unit") or ""),
                                period=fp,
                                why=f"事实台账候选 {fid} (ref={frow.get('ref') or ''})",
                            )
                        )

        # Order 4a: thesis.evidence candidates
        for eid, ev_obj in evidence_by_id.items():
            if _is_legacy_thesis_evidence(ev_obj):
                fact_str = str(ev_obj.get("fact") or "")
                if (fig.raw and fig.raw in fact_str) or (
                    fig.metric and metrics_match(fig.metric, fact_str, metric_aliases)
                ):
                    legacy_hint = "旧认知中的说法，尚未用原始来源核实"
                    auto_candidates.append(
                        MatchCandidate(
                            kind="legacy_thesis",
                            id=eid,
                            value=fig.value,
                            unit=fig.unit,
                            period=fig.period,
                            why="旧认知中的说法，尚未用原始来源核实",
                        )
                    )
                continue
            cand = _try_match_target(fig, f"evidence:{eid}")
            if cand is not None:
                if cand.matched:
                    auto_candidates.append(
                        MatchCandidate(
                            kind="evidence",
                            id=eid,
                            value=fig.value,
                            unit=fig.unit,
                            period=fig.period,
                            why=f"候选证据 {eid}：{cand.excerpt[:80]}",
                        )
                    )
                else:
                    auto_candidates.extend(cand.candidates)

        # Order 4b: thesis.market_view.figures candidates
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
                auto_candidates.append(
                    MatchCandidate(
                        kind="prov",
                        id="thesis:market_view",
                        value=float(mvf["value"]),
                        unit=str(mvf.get("unit") or ""),
                        period=mv_p,
                        why=f"一致预期候选：{mvf.get('metric')} ({mv_p}) = {mvf.get('value')} {mvf.get('unit') or ''}",
                    )
                )

        # Order 4c: model cells candidates
        for m_spec, c_info in cells_map.items():
            if metrics_match(fig.metric, str(c_info.get("metric") or ""), metric_aliases):
                cand = _try_match_target(fig, f"model:{m_spec}")
                if cand is not None:
                    if cand.matched:
                        auto_candidates.append(
                            MatchCandidate(
                                kind="model",
                                id=m_spec,
                                value=float(c_info["value"]),
                                unit=str(c_info.get("unit") or ""),
                                period=normalize_period(c_info.get("period")),
                                why=f"模型单元格候选：{m_spec}",
                            )
                        )
                    else:
                        auto_candidates.extend(cand.candidates)

        # Order 4d: provenance records candidates
        for pid, prec in prov_map.items():
            body_text = str(
                prec.get("body")
                or prec.get("body_inline")
                or prec.get("result_snippet")
                or ""
            )
            hit, excerpt, _, _, prov_cands = _inspect_text_source(
                fig,
                body_text,
                source_id=pid,
                kind="prov",
                metric_aliases=metric_aliases,
                source_cache=source_cache,
            )
            if hit:
                auto_candidates.append(
                    MatchCandidate(
                        kind="prov",
                        id=pid,
                        value=fig.value,
                        unit=fig.unit,
                        period=fig.period,
                        why=f"来源记录候选 {pid}：{excerpt[:80]}",
                    )
                )
            elif prov_cands:
                auto_candidates.extend(prov_cands)

        outcomes.append(
            MatchOutcome(
                figure=fig,
                matched=False,
                reason="unverified",
                hint=legacy_hint or "请补充 evidence:/model:/prov: 脚注或由 D 环节核对来源引文",
                candidates=auto_candidates[:5],
            )
        )

    # Order 5: Derived figures from Order 0–3 verified figures
    return apply_derived_figure_matching(outcomes)


def retrieve_candidate_sources(
    fig: ExtractedFigure,
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_ledger: dict[str, dict[str, Any]] | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
    limit: int = 8,
    source_cache: SourceParseCache | None = None,
) -> list[dict[str, Any]]:
    """Retrieve candidate source snippets for Step D (P0I §1.6, L34).

    - Excludes `legacy_thesis` entries so migrated legacy thesis can never anchor Step D verification.
    - When total candidate sources <= 30, returns all of them sorted by priority.
    - When total candidate sources > 30, sorts by (footnote_match, value_match, period_match, metric_match)
      and returns the top `max(limit, 8)` entries.
    """
    cache = source_cache if source_cache is not None else SourceParseCache()
    all_entries: list[tuple[tuple[int, int, int, int, int], int, dict[str, Any]]] = []
    fig_p = normalize_period(fig.period)
    fn_target = (fig.footnote_target or "").strip()
    seq = 0

    def _check_text_value_and_period(text: str) -> tuple[int, int]:
        v_hit = 0
        p_hit = 0
        if not text:
            return 0, 0
        if fig_p:
            body_periods = cache.periods(text)
            if fig_p in body_periods or fig_p.lower() in text.lower():
                p_hit = 1
        for sf in cache.figures(text):
            if sf.exempt:
                continue
            ok_v, _, _ = values_within_tolerance(fig, sf.value, sf.unit, sf.scale)
            if ok_v:
                v_hit = 1
                break
        return v_hit, p_hit

    # 1. thesis.json evidence[] (excluding legacy_thesis)
    for ev in (thesis_doc or {}).get("evidence") or []:
        if not isinstance(ev, dict) or "id" not in ev:
            continue
        if _is_legacy_thesis_evidence(ev):
            continue
        eid = str(ev["id"])
        fn_hit = int(
            fn_target == f"evidence:{eid}"
            or fn_target.replace("-", "").upper() == f"EVIDENCE:{eid.replace('-', '').upper()}"
        )
        fact_str = str(ev.get("fact") or "")
        val_hit, per_hit = _check_text_value_and_period(fact_str)
        met_hit = int(bool(fig.metric and fact_str and metrics_match(fig.metric, fact_str, metric_aliases)))

        fig_lines: list[str] = []
        for ef in ev.get("figures") or []:
            if not isinstance(ef, dict):
                continue
            ef_m = str(ef.get("metric") or "")
            ef_p = normalize_period(ef.get("period"))
            ef_u = str(ef.get("unit") or "other")
            if ef.get("value") is not None:
                try:
                    ok_ef, _, _ = values_within_tolerance(fig, float(ef["value"]), ef_u)
                    if ok_ef:
                        val_hit = 1
                except (TypeError, ValueError):
                    pass
            if fig_p and ef_p and fig_p == ef_p:
                per_hit = 1
            if fig.metric and ef_m and metrics_match(fig.metric, ef_m, metric_aliases):
                met_hit = 1
            fig_lines.append(
                f"{ef_m} ({ef.get('period') or ''}): {ef.get('value')} {ef.get('unit') or ''}"
            )

        raw_hit = int(bool(fig.raw and fig.raw in fact_str))
        combined_text = fact_str
        if fig_lines:
            combined_text = f"{fact_str} | Figures: {'; '.join(fig_lines)}".strip(" |")
        if not combined_text:
            continue
        seq += 1
        entry = {
            "source_id": eid,
            "ref": f"evidence:{eid}",
            "kind": "evidence",
            "source_identity": str((ev.get("source") or {}).get("identity") or ""),
            "text": combined_text,
        }
        all_entries.append(((fn_hit, val_hit, per_hit, met_hit, raw_hit), -seq, entry))

    # 1b. thesis.json market_view.figures[]
    mv_figs = ((thesis_doc or {}).get("market_view") or {}).get("figures") or []
    if mv_figs:
        mv_lines: list[str] = []
        val_hit = 0
        per_hit = 0
        met_hit = 0
        for mvf in mv_figs:
            if not isinstance(mvf, dict) or mvf.get("value") is None:
                continue
            m_name = str(mvf.get("metric") or "")
            m_per = normalize_period(mvf.get("period"))
            m_unit = str(mvf.get("unit") or "other")
            try:
                ok_mv, _, _ = values_within_tolerance(fig, float(mvf["value"]), m_unit)
                if ok_mv:
                    val_hit = 1
            except (TypeError, ValueError):
                pass
            if fig_p and m_per and fig_p == m_per:
                per_hit = 1
            if fig.metric and m_name and metrics_match(fig.metric, m_name, metric_aliases):
                met_hit = 1
            mv_lines.append(f"{m_name} ({mvf.get('period') or ''}): {mvf.get('value')} {mvf.get('unit') or ''}")
        if mv_lines:
            seq += 1
            mv_entry = {
                "source_id": "thesis:market_view",
                "ref": "thesis:market_view",
                "kind": "prov",
                "text": "Market consensus figures: " + "; ".join(mv_lines),
            }
            all_entries.append(((0, val_hit, per_hit, met_hit, 0), -seq, mv_entry))

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
        fn_hit = int(
            fn_target == f"model:{m_spec}"
            or fn_target == f"model:{m_spec.replace('#', '!', 1)}"
        )
        cm = str(c_info.get("metric") or "")
        cp = normalize_period(c_info.get("period"))
        cu = str(c_info.get("unit") or "other")
        val_hit = 0
        if c_info.get("value") is not None:
            try:
                ok_c, _, _ = values_within_tolerance(fig, float(c_info["value"]), cu)
                if ok_c:
                    val_hit = 1
            except (TypeError, ValueError):
                pass
        per_hit = int(bool(fig_p and cp and fig_p == cp))
        met_hit = int(bool(fig.metric and cm and metrics_match(fig.metric, cm, metric_aliases)))
        seq += 1
        all_entries.append(
            (
                (fn_hit, val_hit, per_hit, met_hit, 0),
                -seq,
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
        fn_hit = int(fn_target == f"prov:{pid}")
        body = str(
            prec.get("body")
            or prec.get("body_inline")
            or prec.get("result_snippet")
            or ""
        )
        if not body:
            continue
        val_hit, per_hit = _check_text_value_and_period(body)
        met_hit = int(bool(fig.metric and metrics_match(fig.metric, body, metric_aliases)))
        raw_hit = int(bool(fig.raw and fig.raw in body))
        seq += 1
        p_entry = {
            "source_id": pid,
            "ref": f"prov:{pid}",
            "kind": "prov",
            "text": body[:1000],
        }
        all_entries.append(((fn_hit, val_hit, per_hit, met_hit, raw_hit), -seq, p_entry))

    # 4. facts_ledger
    for fid, frow in (facts_ledger or {}).items():
        if not isinstance(frow, dict):
            continue
        fn_hit = int(fn_target == f"f:{fid}")
        fm = str(frow.get("metric") or "")
        fp = normalize_period(frow.get("period"))
        fu = str(frow.get("unit") or "other")
        val_hit = 0
        if frow.get("value") is not None:
            try:
                ok_f, _, _ = values_within_tolerance(fig, float(frow["value"]), fu)
                if ok_f:
                    val_hit = 1
            except (TypeError, ValueError):
                pass
        per_hit = int(bool(fig_p and fp and fig_p == fp))
        met_hit = int(bool(fig.metric and fm and metrics_match(fig.metric, fm, metric_aliases)))
        seq += 1
        all_entries.append(
            (
                (fn_hit, val_hit, per_hit, met_hit, 0),
                -seq,
                {
                    "source_id": fid,
                    "ref": f"f:{fid}",
                    "kind": "facts",
                    "text": f"{fm} ({fp}): {frow.get('value')} {frow.get('unit') or ''} (ref={frow.get('ref') or ''})",
                },
            )
        )

    all_entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    sorted_candidates = [item[2] for item in all_entries]
    if len(sorted_candidates) <= 30:
        return sorted_candidates
    return sorted_candidates[: max(limit, 8)]


