"""Deterministic figure-to-source matching and discrepancy classification (PK.9)."""

from __future__ import annotations

import dataclasses
import io
import json
import re
from pathlib import Path
from typing import Any

from src.dalton.publish.numbers import ExtractedFigure, normalize_period
from src.dalton.settings import get_dalton_config

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
}

UNIT_BASE: dict[str, tuple[float, str]] = {
    "USD": (1.0, "USD"),
    "USD_mn": (1e6, "USD"),
    "USD_bn": (1e9, "USD"),
    "HKD": (1.0, "HKD"),
    "HKD_mn": (1e6, "HKD"),
    "HKD_bn": (1e9, "HKD"),
    "CNY": (1.0, "CNY"),
    "CNY_mn": (1e6, "CNY"),
    "CNY_100mn": (1e8, "CNY"),
    "CNY_bn": (1e9, "CNY"),
    "pct": (1.0, "pct"),
    "bp": (0.01, "pct"),
    "x": (1.0, "x"),
    "count": (1.0, "count"),
    "count_mn": (1e6, "count"),
    "per_share": (1.0, "USD"),
    "other": (1.0, "other"),
}


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
    if s1 in s2 or s2 in s1:
        return True
    g1 = canonical_metric_group(s1, aliases)
    g2 = canonical_metric_group(s2, aliases)
    if g1 and g2 and g1 == g2:
        return True
    # Token overlap check for Chinese/English compound metrics
    tokens1 = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", s1))
    tokens2 = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", s2))
    common = tokens1 & tokens2 - {"同比", "环比", "增速", "增长", "一季度", "二季度", "三季度", "四季度"}
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

    # Half-ULP tolerance in the figure's displayed precision
    ulp_half = 0.5 * (10.0 ** (-fig.decimals))
    if fig.unit == "pct" or src_unit == "pct":
        tol_display = max(0.05, ulp_half)
    else:
        tol_display = ulp_half

    fig_effective_scale = fig.scale if fig.scale > 1.0 else UNIT_BASE.get(fig.unit, (1.0, ""))[0]
    tol_base = tol_display * fig_effective_scale

    # Direct base-value match (same dimension or one is generic 'other')
    if fig_dim == src_dim or "other" in (fig_dim, src_dim):
        diff_base = abs(fig_base - src_base)
        if diff_base <= tol_base * 1.0001:
            return True, False, 0.0

    # Direct unscaled number match when source unit is identical to figure unit
    if abs(fig.value - src_value) <= tol_display * 1.0001:
        if fig.unit == src_unit or "other" in (fig.unit, src_unit):
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


def _number_representations(fig: ExtractedFigure) -> list[str]:
    """Generate string representations of ``fig`` to search inside provenance bodies."""
    reps: set[str] = set()
    val = abs(fig.value)
    if fig.decimals == 0:
        ival = int(round(val))
        reps.add(str(ival))
        reps.add(f"{ival:,}")
    else:
        s = f"{val:.{fig.decimals}f}"
        reps.add(s)
        parts = s.split(".")
        reps.add(f"{int(parts[0]):,}.{parts[1]}")

    # If scaled in 亿 (1e8), also check bn (1e9) and mn (1e6) equivalents!
    base_val = abs(fig.scaled_value)
    if base_val >= 1e6:
        in_bn = base_val / 1e9
        in_mn = base_val / 1e6
        in_yi = base_val / 1e8
        for candidate in (in_bn, in_mn, in_yi):
            if candidate >= 0.01:
                if abs(candidate - round(candidate)) < 1e-6:
                    iv = int(round(candidate))
                    reps.add(str(iv))
                    reps.add(f"{iv:,}")
                else:
                    reps.add(f"{candidate:.1f}".rstrip("0").rstrip("."))
                    reps.add(f"{candidate:.2f}".rstrip("0").rstrip("."))
    return sorted(reps, key=len, reverse=True)


def _match_in_provenance_record(
    fig: ExtractedFigure,
    prov: dict[str, Any],
) -> tuple[bool, str]:
    """Search ``prov`` body for ``fig`` value within ±300 chars of metric and period (PK.9 rule 3)."""
    body = str(prov.get("body") or prov.get("body_inline") or prov.get("result_snippet") or "")
    if not body:
        return False, ""

    reps = _number_representations(fig)
    period_norm = normalize_period(fig.period)
    for rep in reps:
        if not rep:
            continue
        pat = re.compile(rf"(?<!\d){re.escape(rep)}(?!\d)")
        for m in pat.finditer(body):
            win_start = max(0, m.start() - 300)
            win_end = min(len(body), m.end() + 300)
            window = body[win_start:win_end]
            # Check period if fig has a period
            if period_norm:
                p_short = period_norm.replace("FY20", "FY")
                if (
                    period_norm.lower() not in window.lower()
                    and p_short.lower() not in window.lower()
                ):
                    continue
            excerpt = " ".join(
                body[max(0, m.start() - 60) : min(len(body), m.end() + 60)].split()
            )
            return True, excerpt
    return False, ""


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
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Evidence {eid} not found in thesis.json",
                )
            src_info = ev.get("source") or {}
            is_ev_consensus = (
                src_info.get("type") == "broker_report"
                and "consensus" in str(src_info.get("identity") or "").lower()
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
                    # Check consensus confusion: source figure is labeled consensus,
                    # or figure claims to be consensus without a consensus prov:
                    if fig.label_hint == "consensus":
                        return MatchOutcome(
                            figure=fig,
                            matched=False,
                            reason="consensus_confusion",
                            hint="一致预期数字必须使用 prov:<record> 脚注并指向一致预期数据接口",
                        )
                    if ef_label == "consensus" and fig.label_hint in (
                        "estimate",
                        "reported",
                        "guidance",
                    ):
                        return MatchOutcome(
                            figure=fig,
                            matched=False,
                            reason="consensus_confusion",
                            hint=f"来源 {eid} 中该数字标签为 consensus（一致预期），正文中被当作 {fig.label_hint} 使用",
                        )
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
            if fig.label_hint == "consensus" and not _is_consensus_provenance(prec):
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="consensus_confusion",
                    hint=(
                        f"一致预期数字引用了非一致预期来源 {pid} "
                        f"(provider={prec.get('provider')!r})；必须来自 estimates:/yf_analysis/street_estimate"
                    ),
                )
            if (
                _is_consensus_provenance(prec)
                and fig.label_hint in ("estimate", "reported", "guidance")
            ):
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="consensus_confusion",
                    hint=f"来源 {pid} 是一致预期接口，但正文将数字表述为 {fig.label_hint}",
                )
            hit, excerpt = _match_in_provenance_record(fig, prec)
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
                reason="source_conflict",
                hint=f"Provenance {pid} body does not contain {fig.raw} for {fig.period}",
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

        # Special rule: Consensus figure without consensus provenance
        if fig.label_hint == "consensus":
            if fig.footnote_target and (
                fig.footnote_target.startswith("prov:")
                or fig.footnote_target.startswith("f:")
            ):
                out = _try_match_target(fig, fig.footnote_target)
                if out:
                    outcomes.append(out)
                    continue
            # Auto-search consensus provenance records
            found_cons: MatchOutcome | None = None
            for pid, prec in prov_map.items():
                if _is_consensus_provenance(prec):
                    hit, excerpt = _match_in_provenance_record(fig, prec)
                    if hit:
                        found_cons = MatchOutcome(
                            figure=fig,
                            matched=True,
                            kind="prov",
                            ref=pid,
                            excerpt=excerpt,
                            source_sha256=str(prec.get("result_sha256") or ""),
                        )
                        break
            if found_cons:
                outcomes.append(found_cons)
            else:
                outcomes.append(
                    MatchOutcome(
                        figure=fig,
                        matched=False,
                        reason="consensus_confusion",
                        hint="加脚注 prov:<record>，来源必须是一致预期数据接口（estimates: / yf_analysis / street_estimate）",
                    )
                )
            continue

        # Explicit footnote matching (Orders 0, 1, 2, 3)
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

        # Order 4: Auto-search across thesis.evidence, model_cells, and provenance_records
        auto_candidates: list[MatchCandidate] = []
        best_failure_reason: str | None = None
        matched_auto: MatchOutcome | None = None

        # 4a. Search thesis.evidence
        for eid in evidence_by_id:
            cand = _try_match_target(fig, f"evidence:{eid}")
            if cand is not None:
                if cand.matched:
                    matched_auto = cand
                    break
                if cand.reason == "consensus_confusion":
                    best_failure_reason = "consensus_confusion"
                elif cand.reason == "unit_mismatch" and best_failure_reason != "consensus_confusion":
                    best_failure_reason = "unit_mismatch"
                elif cand.reason == "period_mismatch" and best_failure_reason not in (
                    "consensus_confusion",
                    "unit_mismatch",
                ):
                    best_failure_reason = "period_mismatch"
                elif cand.reason == "source_conflict" and not best_failure_reason:
                    best_failure_reason = "source_conflict"
                auto_candidates.extend(cand.candidates)

        if matched_auto:
            outcomes.append(matched_auto)
            continue

        # 4b. Search model cells
        for m_spec, c_info in cells_map.items():
            if metrics_match(fig.metric, str(c_info.get("metric") or ""), metric_aliases):
                cand = _try_match_target(fig, f"model:{m_spec}")
                if cand is not None:
                    if cand.matched:
                        matched_auto = cand
                        break
                    if not best_failure_reason and cand.reason:
                        best_failure_reason = cand.reason
                    auto_candidates.extend(cand.candidates)

        if matched_auto:
            outcomes.append(matched_auto)
            continue

        # 4c. Search provenance records
        for pid, prec in prov_map.items():
            if _is_consensus_provenance(prec) and fig.label_hint in (
                "estimate",
                "reported",
                "guidance",
            ):
                hit, _ = _match_in_provenance_record(fig, prec)
                if hit:
                    best_failure_reason = "consensus_confusion"
                    continue
            hit, excerpt = _match_in_provenance_record(fig, prec)
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

        if matched_auto:
            outcomes.append(matched_auto)
            continue

        # Record diagnostic failure
        outcomes.append(
            MatchOutcome(
                figure=fig,
                matched=False,
                reason=best_failure_reason or "unverified",
                hint="请补充 evidence:/model:/prov: 脚注或核对数值、单位与期间",
                candidates=auto_candidates[:5],
            )
        )

    # Order 5: Second pass for derived figures (rates, margins, multiples) recomputable
    # from two already-matched figures on the same line or within ±3 lines
    matched_by_line: list[ExtractedFigure] = [
        o.figure for o in outcomes if o.matched and o.kind != "exempt"
    ]
    for idx, out in enumerate(outcomes):
        if out.matched or out.figure.label_hint != "derived":
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
                    # Check ratio a/b * 100 or growth (a - b)/b * 100
                    for derived_pct in ((a / b) * 100.0, ((a - b) / b) * 100.0):
                        ok_d, _, _ = values_within_tolerance(fig, derived_pct, "pct")
                        if ok_d:
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
