"""Deterministic figure-to-source matching and discrepancy classification (PK.9)."""

from __future__ import annotations

import dataclasses
import io
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from src.dalton.publish.numbers import (
    ExtractedFigure,
    _PERIOD_TOKEN_RE,
    _PerShareUnit,
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

class _PctNumberFormatUnit(str):
    """Marker subclass of ``str`` indicating ``"pct"`` came from an Excel cell percentage ``number_format``."""

    is_pct_number_format: bool = True
    stored_as_ratio: bool = True


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
    "unknown_mn": (1e6, "unknown_currency"),
    "unknown_100mn": (1e8, "unknown_currency"),
    "unknown_bn": (1e9, "unknown_currency"),
    "pct": (1.0, "pct"),
    "%": (1.0, "pct"),
    "pp": (1.0, "pct"),
    "bp": (0.01, "pct"),
    "bps": (0.01, "pct"),
    "x": (1.0, "x"),
    "count": (1.0, "count"),
    "count_mn": (1e6, "count"),
    "USD_per_share": (1.0, "USD_per_share"),
    "HKD_per_share": (1.0, "HKD_per_share"),
    "CNY_per_share": (1.0, "CNY_per_share"),
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


def _extract_evidence_ref_path(ev: dict[str, Any]) -> str:
    """Extract the file path from ``ev['source']['ref']`` if present."""
    src = ev.get("source")
    if not isinstance(src, dict):
        return ""
    ref = src.get("ref")
    if isinstance(ref, dict):
        return str(ref.get("path") or "").strip()
    if isinstance(ref, str):
        return ref.strip()
    return ""


def _normalize_vpath(path: str) -> str:
    s = (path or "").replace("\\", "/").strip()
    if not s:
        return ""
    norm = posixpath.normpath(s).lstrip("/")
    return "" if norm == "." else norm


def _is_draft_self_ref(ref_path: str, draft_path: str = "") -> bool:
    """Return True if ``ref_path`` normalized equals ``draft_path`` or ``drafts/<draft_basename>``."""
    norm_ref = _normalize_vpath(ref_path)
    norm_draft = _normalize_vpath(draft_path)
    if not norm_ref or not norm_draft:
        return False
    if norm_ref == norm_draft:
        return True
    draft_base = norm_draft.rsplit("/", 1)[-1]
    if draft_base and norm_ref == f"drafts/{draft_base}":
        return True
    return False


def _resolve_evidence_ref_body(
    ev: dict[str, Any],
    evidence_ref_bodies: dict[str, str] | None,
    *,
    draft_path: str = "",
) -> tuple[str, str]:
    """Return ``(ref_path, body_text)`` for an evidence item's primary file ref, excluding draft self-refs."""
    ref_path = _extract_evidence_ref_path(ev)
    if _is_draft_self_ref(ref_path, draft_path):
        return ref_path, ""
    bodies = evidence_ref_bodies or {}
    norm_ref = _normalize_vpath(ref_path)
    eid = str(ev.get("id") or "")
    body = ""
    if ref_path and ref_path in bodies:
        body = str(bodies[ref_path] or "")
    elif norm_ref and norm_ref in bodies:
        body = str(bodies[norm_ref] or "")
    elif eid and eid in bodies:
        body = str(bodies[eid] or "")
    else:
        src = ev.get("source")
        ref = src.get("ref") if isinstance(src, dict) else None
        if isinstance(ref, dict):
            body = str(ref.get("body") or ref.get("text") or "")
    return ref_path, body


def _is_draft_provenance_record(prec: dict[str, Any], draft_path: str = "") -> bool:
    """Return True if a provenance record was produced by reading the current draft itself or ``drafts/<draft_basename>``."""
    if not _normalize_vpath(draft_path):
        return False
    for field in ("identifier", "title", "detail"):
        val = str(prec.get(field) or "").strip()
        if val and _is_draft_self_ref(val, draft_path):
            return True
    return False


def _alias_in_metric(alias_low: str, metric_low: str) -> bool:
    """Match ``alias_low`` in ``metric_low``, using whole-word boundaries on ASCII alphanumeric edges."""
    if not alias_low or not metric_low:
        return False
    prefix = r"(?<![a-z0-9])" if alias_low[0].isascii() and alias_low[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if alias_low[-1].isascii() and alias_low[-1].isalnum() else ""
    if prefix or suffix:
        return bool(re.search(f"{prefix}{re.escape(alias_low)}{suffix}", metric_low))
    return alias_low in metric_low


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
            if _alias_in_metric(w_low, m_low):
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

    # Prevent matching a level metric ("EPS") against a sensitivity/delta expression ("费用每多 100 亿美元 EPS")
    sens_re = re.compile(
        r"每\s*(?:多|少|增|减|变动|提升|下降|上升|提高|降低|\+|-|\d)|敏感|弹性|sensitivity|per\s+\d",
        re.IGNORECASE,
    )
    if bool(sens_re.search(s1)) != bool(sens_re.search(s2)):
        return False

    if _alias_in_metric(s1, s2) or _alias_in_metric(s2, s1):
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


def _infer_currency_from_text(text: str) -> str | None:
    """Return the single unambiguous currency ('USD', 'HKD', 'CNY') in ``text``, or ``None`` if none or multiple."""
    s = (text or "").strip()
    if not s:
        return None
    low = s.lower()
    found: set[str] = set()
    if re.search(r"(?:港元|港币|\bhkd\b|hk\$)", low):
        found.add("HKD")
    low_no_hk = re.sub(r"hk\$", " ", low)
    if re.search(r"(?:美元|\busd\b|us\$|(?<![a-z])\$)", low_no_hk):
        found.add("USD")
    if re.search(
        r"(?:人民币|亿元|百万元|万元|元/股|\bcny\b|\brmb\b|[¥￥]|(?<![美港多纪单])元(?![素年旦气件]))",
        low,
    ):
        found.add("CNY")
    if len(found) == 1:
        return next(iter(found))
    return None


def _resolve_per_share_currency(
    unit: str, fallback_currency: str | None = None
) -> str | None:
    u_cur = getattr(unit, "currency", None)
    if u_cur in ("USD", "HKD", "CNY"):
        return u_cur
    if isinstance(unit, str):
        if unit in ("USD", "HKD", "CNY"):
            return unit
        if unit.endswith("_per_share"):
            prefix = unit.split("_", 1)[0].upper()
            if prefix in ("USD", "HKD", "CNY"):
                return prefix
    if fallback_currency in ("USD", "HKD", "CNY"):
        return fallback_currency
    return None


def _resolve_ef_per_share_currency(
    ef: dict[str, Any],
    ev: dict[str, Any],
    source_cache: SourceParseCache | None = None,
) -> str | None:
    """Resolve currency for a structured ``per_share`` figure ``ef`` only from currency tightly bound to that figure."""
    cur = _infer_currency_from_text(str(ef.get("currency") or ""))
    if cur:
        return cur
    ef_metric = str(ef.get("metric") or "")
    fact_text = str(ev.get("fact") or "").strip()
    if not fact_text or ef.get("value") is None:
        return None
    try:
        ef_val = float(ef["value"])
    except (TypeError, ValueError):
        return None
    sfs = (
        source_cache.figures(fact_text)
        if source_cache is not None
        else extract_figures(fact_text)
    )
    matched_curs: set[str] = set()
    found_matching_num = False
    for sf in sfs:
        if abs(sf.value - ef_val) <= 1e-6:
            if ef_metric and sf.metric and not metrics_match(ef_metric, sf.metric):
                continue
            found_matching_num = True
            sf_cur = _resolve_per_share_currency(sf.unit, sf.currency)
            if sf_cur:
                matched_curs.add(sf_cur)
            else:
                return None
    if found_matching_num and len(matched_curs) == 1:
        return next(iter(matched_curs))
    return None


def _to_base(
    value: float,
    unit: str,
    scale: float = 1.0,
    currency: str | None = None,
) -> tuple[float, str]:
    mult, dim = UNIT_BASE.get(unit, (1.0, "other"))
    if dim == "per_share":
        cur = _resolve_per_share_currency(unit, currency)
        dim = f"{cur}_per_share" if cur else "unknown_per_share"
    if scale > 1.0 and mult in (1e6, 1e8, 1e9):
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
    if (
        fig_dim in ("unknown_currency", "unknown_per_share")
        or src_dim in ("unknown_currency", "unknown_per_share")
    ):
        return False
    if fig_dim == src_dim:
        return True
    # Allow per_share to match unscaled currency of the SAME currency (e.g. '2.12 美元/股' vs '2.12 USD' in SEC/model tables)
    src_mult = UNIT_BASE.get(src_unit, (1.0, "other"))[0]
    fig_mult = UNIT_BASE.get(fig.unit, (1.0, "other"))[0]
    if (
        fig_dim.endswith("_per_share")
        and src_dim in ("USD", "CNY", "HKD")
        and fig_dim == f"{src_dim}_per_share"
        and src_scale == 1.0
        and src_mult == 1.0
    ):
        return True
    if (
        src_dim.endswith("_per_share")
        and fig_dim in ("USD", "CNY", "HKD")
        and src_dim == f"{fig_dim}_per_share"
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
    *,
    stored_as_ratio: bool = False,
    pct_from_number_format: bool = False,
    src_currency: str | None = None,
) -> tuple[bool, bool, float]:
    """Compare ``fig`` against ``(src_value, src_unit, src_scale)``.

    Returns ``(matched, is_unit_mismatch, relative_or_abs_diff)``.
    """
    is_pct_ratio = (
        stored_as_ratio
        or pct_from_number_format
        or bool(getattr(src_unit, "stored_as_ratio", False))
        or bool(getattr(src_unit, "is_pct_number_format", False))
    )
    eff_src_unit: str = "pct" if is_pct_ratio else src_unit
    eff_src_scale: float = 1.0 if is_pct_ratio else src_scale
    eff_src_currency: str | None = None if is_pct_ratio else src_currency
    eff_src_value = src_value * 100.0 if is_pct_ratio else src_value
    fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale, fig.currency)
    src_base, src_dim = _to_base(
        eff_src_value, eff_src_unit, eff_src_scale, eff_src_currency
    )

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
        fig, fig_base, fig_dim, eff_src_unit, eff_src_scale, src_base, src_dim
    )

    if dims_compatible:
        diff_base = abs(fig_base - src_base)
        if diff_base <= tol_base * 1.0001:
            return True, False, 0.0

    # Chinese prose '翻倍' (2.0x) vs '+100%' (100 pct)
    if fig.raw in ("翻倍", "翻一番") and src_dim == "pct" and abs(eff_src_value - 100.0) <= 0.5:
        return True, False, 0.0

    # Check if ratio is a power of 10 (e.g. 512百万 vs 512亿, or mn vs bn) -> unit/scale mismatch
    if dims_compatible and fig_base != 0 and src_base != 0:
        ratio = abs(fig_base / src_base)
        for p10 in (1e-6, 1e-4, 1e-3, 1e-2, 1e-1, 1e1, 1e2, 1e3, 1e4, 1e6):
            if abs(ratio - p10) / p10 <= 0.01:
                return False, True, abs(fig_base - src_base)

    if abs(fig.value - eff_src_value) <= max(0.05, ulp_half) * 1.0001:
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


def _specific_text_metric_group(
    metric: str, aliases: dict[str, list[str]] | None = None
) -> str:
    """Resolve the specific financial metric concept for unstructured text matching."""
    m_low = " ".join((metric or "").lower().split())
    if not m_low:
        return ""
    # Check specific non-segment aliases first so "Google Cloud 分部营业利润率" maps to operating_margin, not cloud_revenue
    merged = {**DEFAULT_METRIC_ALIASES, **(aliases or {})}
    segment_only = {"google cloud", "aws", "azure"}
    scored: list[tuple[int, str]] = []
    for group, words in merged.items():
        for w in words:
            w_low = w.lower()
            if w_low in segment_only:
                continue
            if _alias_in_metric(w_low, m_low):
                scored.append((len(w_low), group))
    for extra_group, extra_words in (
        ("rpo", ("剩余履约义务", "rpo", "remaining performance obligation")),
        ("pretax_income", ("税前利润", "隐含税前利润", "pretax income", "ebt")),
        ("total_expense", ("总费用", "隐含总费用", "total expense", "total expenses", "opex")),
        ("tax_rate", ("税率", "有效税率", "effective tax rate")),
        ("shares", ("稀释股", "摊薄股", "总股本", "diluted shares")),
    ):
        for w_low in extra_words:
            if _alias_in_metric(w_low, m_low):
                scored.append((len(w_low), extra_group))
    if scored:
        scored.sort(reverse=True)
        return scored[0][1]
    return canonical_metric_group(m_low, aliases)


_GENERIC_ENGLISH_METRIC_TOKENS = frozenset(
    {
        "revenue",
        "revenues",
        "total",
        "income",
        "operating",
        "margin",
        "gross",
        "net",
        "eps",
        "diluted",
        "gaap",
        "non",
        "capex",
        "capital",
        "expenditure",
        "expenditures",
        "fcf",
        "free",
        "cash",
        "flow",
        "dap",
        "family",
        "target",
        "price",
        "irr",
        "research",
        "development",
        "sales",
        "marketing",
        "general",
        "administrative",
        "market",
        "share",
        "ebit",
        "ebitda",
        "opm",
        "gpm",
        "yoy",
        "qoq",
        "cagr",
        "growth",
        "rpo",
        "usd",
        "hkd",
        "cny",
        "mean",
        "avg",
        "average",
        "consensus",
        "est",
        "estimate",
    }
)


_BUILTIN_KNOWN_SEGMENTS: tuple[tuple[str, str], ...] = (
    # Alphabet / Google
    ("google services", "services"),
    ("services", "services"),
    ("google cloud", "cloud"),
    ("cloud", "cloud"),
    ("google search", "search"),
    ("search & other", "search"),
    ("search", "search"),
    ("youtube", "youtube"),
    ("other bets", "other_bets"),
    ("google network", "network"),
    # Amazon
    ("amazon web services", "aws"),
    ("aws", "aws"),
    ("north america", "north_america"),
    ("international", "international"),
    # Microsoft
    ("intelligent cloud", "intelligent_cloud"),
    ("azure", "azure"),
    ("productivity and business processes", "productivity_and_business_processes"),
    ("more personal computing", "more_personal_computing"),
    # Meta
    ("family of apps", "family_of_apps"),
    ("reality labs", "reality_labs"),
    # Apple
    ("iphone", "iphone"),
    ("mac", "mac"),
    ("ipad", "ipad"),
    ("wearables", "wearables"),
    # NVIDIA
    ("data center", "data_center"),
    ("datacenter", "data_center"),
    ("gaming", "gaming"),
    ("professional visualization", "professional_visualization"),
    ("automotive", "automotive"),
)


def _extract_segment_qualifier(raw_metric: str) -> str:
    """Return the canonical segment id for a known business segment in ``raw_metric``, or '' if none."""
    low = " ".join((raw_metric or "").lower().split())
    if not low:
        return ""
    for phrase, canon_id in sorted(
        _BUILTIN_KNOWN_SEGMENTS, key=lambda kv: len(kv[0]), reverse=True
    ):
        pat = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
        if re.search(pat, low):
            return canon_id
    return ""


def _same_text_metric(
    m1: str,
    m2: str,
    aliases: dict[str, list[str]] | None = None,
) -> bool:
    """Return True only when ``m1`` and ``m2`` positively identify the same metric in unstructured text."""
    s1 = " ".join((m1 or "").lower().split())
    s2 = " ".join((m2 or "").lower().split())
    if not s1 or not s2:
        return False
    if not metrics_match(s1, s2, aliases):
        return False

    # Share/proportion words ("占收入", "占比") vs level/growth metric
    share_re = re.compile(r"占比|占.{0,6}(?:收入|营收|比|总额)|share\s+of", re.IGNORECASE)
    if bool(share_re.search(s1)) != bool(share_re.search(s2)):
        return False

    g1 = _specific_text_metric_group(s1, aliases)
    g2 = _specific_text_metric_group(s2, aliases)
    known_groups = set(DEFAULT_METRIC_ALIASES.keys()) | set((aliases or {}).keys()) | {
        "rpo",
        "pretax_income",
        "total_expense",
        "tax_rate",
        "shares",
    }
    if (g1 in known_groups or g2 in known_groups) and g1 != g2:
        # Allow revenue vs cloud_revenue / aws_revenue when one is bare "Cloud" and the other is "Cloud 收入"
        if {g1, g2} <= {"revenue", "cloud_revenue"} and ("cloud" in s1 or "云" in s1) and ("cloud" in s2 or "云" in s2):
            pass
        elif {g1, g2} <= {"revenue", "aws_revenue"} and "aws" in s1 and "aws" in s2:
            pass
        else:
            return False
    elif g1 != g2 and g1 not in s2 and g2 not in s1:
        return False

    # Segment qualifier check: skip conflict ONLY when BOTH sides identify a segment and their canonical ids differ
    seg1 = _extract_segment_qualifier(m1)
    seg2 = _extract_segment_qualifier(m2)
    if seg1 and seg2 and seg1 != seg2:
        return False
    return True


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

    # First pass: check if any figure in the text source matches both value and compatible metric/period
    for sf in src_figures:
        if sf.exempt:
            continue
        if sf.metric and fig.metric:
            if not metrics_match(fig.metric, sf.metric, metric_aliases):
                continue
            seg1 = _extract_segment_qualifier(fig.metric)
            seg2 = _extract_segment_qualifier(sf.metric)
            if seg1 and seg2 and seg1 != seg2:
                continue
        sf_p = normalize_period(sf.period)
        if fig_p:
            if sf_p and sf_p != fig_p:
                continue
            if not sf_p and all_body_periods and fig_p not in all_body_periods:
                continue
        ok_val, _, _ = values_within_tolerance(
            fig, sf.value, sf.unit, sf.scale
        )
        if ok_val:
            excerpt = str(sf.location.get("sentence") or body[:160]).strip()
            return True, excerpt[:200], None, "", []

    cands: list[MatchCandidate] = []
    best_reason: str | None = None
    best_hint: str = ""

    # Second pass: classify discrepancies. In unstructured text, only report source_conflict
    # when both metric name and unit positively locate the same metric and the value disagrees.
    for sf in src_figures:
        if sf.exempt:
            continue
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

        if not _same_text_metric(fig.metric, sf.metric, metric_aliases):
            continue

        fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale, fig.currency)
        sf_base, sf_dim = _to_base(sf.value, sf.unit, sf.scale, sf.currency)
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

    if best_reason is None:
        return False, "", "unverified", "来源文本中未找到匹配的指标与单位", cands

    return False, "", best_reason, best_hint, cands


def _parse_unit_from_label_text(text: str) -> str | None:
    """Extract an explicit canonical unit from a worksheet row label, column header, or unit note."""
    s = (text or "").strip()
    if not s:
        return None
    low = s.lower()
    currency = _infer_currency_from_text(s)
    if re.search(
        r"(?:美元/股|港元/股|港币/股|人民币/股|元/股|\$/sh(?:are)?\b|us\$/sh(?:are)?\b|hk\$/sh(?:are)?\b|(?:usd|hkd|cny|rmb)/sh(?:are)?\b|per\s+share\b|/sh(?:are)?\b|每股)",
        low,
    ):
        return _PerShareUnit("per_share", currency=currency)
    if re.search(r"(?:\bbps\b|\bbp\b|基点)", low):
        return "bp"
    if low in ("%", "pct") or re.search(
        r"(?:[（\(\[【]\s*(?:%|pct)\s*[）\)\]】]|单位\s*[：:]\s*(?:%|pct\b))",
        low,
    ):
        return "pct"
    if low in ("x", "倍") or re.search(
        r"(?:[（\(\[【]\s*(?:x|倍)\s*[）\)\]】]|单位\s*[：:]\s*(?:x|倍))",
        low,
    ):
        return "x"

    mag: str | None = None
    if re.search(
        r"(?:十亿|\bbillions?\b|\bbn\b|(?:us\$|hk\$|\$|\busd|\bhkd|\bcny|\brmb)[\s_]*b\b|\(\s*b\s*\))",
        low,
    ):
        mag = "bn"
    elif re.search(r"(?<![十百千万])亿", low):
        mag = "100mn"
    elif re.search(
        r"(?:百万|\bmillions?\b|\bmn\b|(?:us\$|hk\$|\$|\busd|\bhkd|\bcny|\brmb)[\s_]*m\b|\(\s*m\s*\))",
        low,
    ):
        mag = "mn"

    if currency and mag:
        return f"{currency}_{mag}"
    if mag and not currency:
        return f"unknown_{mag}"
    if currency and (
        low
        in (
            "$",
            "usd",
            "us$",
            "美元",
            "hk$",
            "hkd",
            "港元",
            "港币",
            "cny",
            "rmb",
            "人民币",
            "元",
            "¥",
            "￥",
        )
        or re.search(
            r"(?:[（\(\[【]\s*(?:us\$|hk\$|\$|usd|hkd|cny|rmb|美元|港元|港币|人民币|元|[¥￥])\s*[）\)\]】]|单位\s*[：:]\s*(?:us\$|hk\$|\$|usd|hkd|cny|rmb|美元|港元|港币|人民币|元|[¥￥]))",
            low,
        )
    ):
        return currency
    return None


def _parse_sheet_level_unit_note(text: str) -> str | None:
    """Extract a worksheet-level unit ONLY when the cell explicitly contains ``单位：`` / ``单位:``."""
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"单位\s*[：:]\s*(.+)", s)
    if not m:
        return None
    after = m.group(1).strip()
    return _parse_unit_from_label_text(f"单位：{after}")


def _is_pct_number_format(num_fmt: str) -> bool:
    """Return True only when ``num_fmt`` contains an unquoted, unescaped ``%`` format specifier."""
    fmt = (num_fmt or "").strip()
    if not fmt or fmt.lower() in ("general", "@"):
        return False
    cleaned = re.sub(r'"[^"]*"|\\.|_.|\*.', "", fmt)
    return "%" in cleaned


def _parse_unit_from_number_format(num_fmt: str) -> str | None:
    """Extract a canonical unit from an Excel cell ``number_format`` string when unambiguous."""
    fmt = (num_fmt or "").strip()
    if not fmt or fmt.lower() in ("general", "@"):
        return None
    if _is_pct_number_format(fmt):
        return _PctNumberFormatUnit("pct")
    low = fmt.lower()
    if "bps" in low or "bp" in low:
        return "bp"
    if '"x"' in low or "'x'" in low or low.endswith("x"):
        return "x"
    return None


_CELL_COORD_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")

_BUILTIN_NUM_FMTS: dict[int, str] = {
    9: "0%",
    10: "0.00%",
}


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_child(elem: ET.Element, local_name: str) -> ET.Element | None:
    for ch in elem:
        if _local_tag(ch.tag) == local_name:
            return ch
    return None


def _extract_rich_text(elem: ET.Element) -> str:
    parts: list[str] = []
    for ch in elem:
        ltag = _local_tag(ch.tag)
        if ltag == "t":
            parts.append(ch.text or "")
        elif ltag == "r":
            for sub in ch:
                if _local_tag(sub.tag) == "t":
                    parts.append(sub.text or "")
    return "".join(parts)


def _normalize_cell_coord(cell: str) -> str:
    return re.sub(r"[\s$]+", "", (cell or "")).upper()


def _normalize_model_spec(m_spec: str) -> str:
    s = (m_spec or "").strip()
    if "#" in s:
        path_part, rest = s.split("#", 1)
    elif "!" in s:
        path_part, rest = s.split("!", 1)
    else:
        return s
    if "!" in rest:
        sh, coord = rest.split("!", 1)
        return f"{path_part.strip()}#{sh.strip()}!{_normalize_cell_coord(coord)}"
    return f"{path_part.strip()}#{_normalize_cell_coord(rest)}"


def _split_cell_coord(coord: str) -> tuple[int, int] | None:
    m = _CELL_COORD_RE.match(coord)
    if not m:
        return None
    col_str, row_str = m.group(1), m.group(2)
    col_idx = 0
    for ch in col_str:
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    return int(row_str), col_idx


def _col_idx_to_letters(col_idx: int) -> str:
    out: list[str] = []
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def _resolve_workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Resolve ``(sheet_name, zip_path)`` pairs from ``xl/workbook.xml`` and ``xl/_rels/workbook.xml.rels``."""
    namelist_set = set(zf.namelist())
    rel_map: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in namelist_set:
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels_root:
            if _local_tag(rel.tag) != "Relationship":
                continue
            rid = (rel.attrib.get("Id") or "").strip()
            target = (rel.attrib.get("Target") or "").strip()
            if not rid or not target:
                continue
            if target.startswith("/"):
                cand = posixpath.normpath(target.lstrip("/"))
            else:
                cand = posixpath.normpath(posixpath.join("xl", target))
            if cand not in namelist_set and target.lstrip("/") in namelist_set:
                cand = target.lstrip("/")
            rel_map[rid] = cand

    sheets: list[tuple[str, str]] = []
    if "xl/workbook.xml" in namelist_set:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        for elem in wb_root.iter():
            if _local_tag(elem.tag) != "sheet":
                continue
            sname = (elem.attrib.get("name") or "").strip()
            rid = ""
            for k, v in elem.attrib.items():
                if k.endswith("}id") or k == "r:id":
                    rid = v.strip()
                    break
            ws_path = rel_map.get(rid, "")
            if not ws_path:
                sheet_id = (elem.attrib.get("sheetId") or "").strip()
                if sheet_id:
                    fallback = f"xl/worksheets/sheet{sheet_id}.xml"
                    if fallback in namelist_set:
                        ws_path = fallback
            if sname and ws_path and ws_path in namelist_set:
                sheets.append((sname, ws_path))
    return sheets


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    namelist_set = set(zf.namelist())
    if "xl/sharedStrings.xml" not in namelist_set:
        return []
    sst_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for ch in sst_root:
        if _local_tag(ch.tag) == "si":
            out.append(_extract_rich_text(ch))
    return out


def _load_style_num_formats(zf: zipfile.ZipFile) -> list[str]:
    namelist_set = set(zf.namelist())
    if "xl/styles.xml" not in namelist_set:
        return []
    styles_root = ET.fromstring(zf.read("xl/styles.xml"))
    custom_fmts: dict[int, str] = {}
    num_fmts_elem = _find_child(styles_root, "numFmts")
    if num_fmts_elem is not None:
        for nf in num_fmts_elem:
            if _local_tag(nf.tag) == "numFmt":
                try:
                    fmt_id = int(nf.attrib.get("numFmtId", "-1"))
                except ValueError:
                    continue
                custom_fmts[fmt_id] = nf.attrib.get("formatCode", "")
    xf_formats: list[str] = []
    cell_xfs_elem = _find_child(styles_root, "cellXfs")
    if cell_xfs_elem is not None:
        for xf in cell_xfs_elem:
            if _local_tag(xf.tag) == "xf":
                try:
                    fmt_id = int(xf.attrib.get("numFmtId", "0"))
                except ValueError:
                    fmt_id = 0
                fmt_code = custom_fmts.get(fmt_id) or _BUILTIN_NUM_FMTS.get(fmt_id, "")
                xf_formats.append(fmt_code)
    return xf_formats


def _parse_worksheet_cells(ws_xml_bytes: bytes) -> dict[str, ET.Element]:
    ws_root = ET.fromstring(ws_xml_bytes)
    cells: dict[str, ET.Element] = {}
    sheet_data = _find_child(ws_root, "sheetData")
    rows_iter = sheet_data if sheet_data is not None else ws_root.iter()
    row_num = 0
    for row_elem in rows_iter:
        if _local_tag(row_elem.tag) != "row":
            continue
        r_attr = (row_elem.attrib.get("r") or "").strip()
        if r_attr.isdigit():
            row_num = int(r_attr)
        else:
            row_num += 1
        col_num = 0
        for c_elem in row_elem:
            if _local_tag(c_elem.tag) != "c":
                continue
            c_ref = _normalize_cell_coord(c_elem.attrib.get("r", ""))
            parsed_rc = _split_cell_coord(c_ref) if c_ref else None
            if parsed_rc is not None:
                row_num, col_num = parsed_rc
            else:
                col_num += 1
                c_ref = f"{_col_idx_to_letters(col_num)}{row_num}"
            cells[c_ref] = c_elem
    return cells


def _decode_cell_element(
    c_elem: ET.Element, shared_strings: list[str], loc: str
) -> tuple[float | str | bool | None, str | None]:
    f_elem = _find_child(c_elem, "f")
    v_elem = _find_child(c_elem, "v")
    is_elem = _find_child(c_elem, "is")
    c_type = (c_elem.attrib.get("t") or "").strip()
    v_raw = (v_elem.text or "") if v_elem is not None else ""
    v_text = v_raw.strip()

    if f_elem is not None and c_type != "inlineStr":
        if c_type == "str":
            if v_elem is None:
                return None, "公式没有缓存值（模型未重算，请先运行 recalc.py）"
        elif v_elem is None or v_text == "":
            return None, "公式没有缓存值（模型未重算，请先运行 recalc.py）"

    if c_type == "e":
        return None, f"模型单元格 {loc} 为错误值 ({v_text or 'error'})"

    if c_type == "s":
        if v_elem is None or v_text == "":
            return None, f"模型单元格 {loc} 不存在或为空"
        try:
            idx = int(v_text)
        except ValueError:
            return None, f"模型单元格 {loc} 共享字符串索引无效 ({v_text!r})"
        if 0 <= idx < len(shared_strings):
            return shared_strings[idx], None
        return None, f"模型单元格 {loc} 共享字符串索引越界 ({idx})"

    if c_type == "inlineStr":
        if is_elem is not None:
            return _extract_rich_text(is_elem), None
        if v_elem is not None:
            return v_raw, None
        return None, f"模型单元格 {loc} 不存在或为空"

    if c_type == "str":
        if v_elem is None:
            return None, f"模型单元格 {loc} 不存在或为空"
        return v_raw, None

    if c_type == "b":
        if v_elem is None or v_text == "":
            return None, f"模型单元格 {loc} 不存在或为空"
        return v_text in ("1", "true", "TRUE", "True"), None

    if c_type in ("", "n"):
        if v_elem is None or v_text == "":
            return None, f"模型单元格 {loc} 不存在或为空"
        try:
            return float(v_text), None
        except ValueError:
            return None, f"模型单元格 {loc} 数值解析失败 ({v_text!r})"

    return None, f"模型单元格 {loc} 类型未知 ({c_type!r})"


def _cell_stored_as_ratio(c_elem: ET.Element, xf_formats: list[str]) -> bool:
    """Return True if ``c_elem`` has a percentage ``number_format`` in ``xf_formats``."""
    s_attr = (c_elem.attrib.get("s") or "").strip()
    if s_attr.isdigit():
        s_idx = int(s_attr)
        if 0 <= s_idx < len(xf_formats):
            return _is_pct_number_format(xf_formats[s_idx])
    return False


UNIT_FORMAT_CONFLICT_WARNING = "格式为百分比，单位标签不一致"


class ModelCellReadResult(tuple):
    """Result of ``read_model_cell``: unpacks as ``(value, unit_hint, error)`` while carrying ``stored_as_ratio`` and ``unit_conflict_warning`` directly on the result."""

    value: float | str | bool | None
    unit_hint: str | None
    error: str | None
    stored_as_ratio: bool
    unit_conflict_warning: str | None

    def __new__(
        cls,
        value: float | str | bool | None,
        unit_hint: str | None,
        error: str | None,
        *,
        stored_as_ratio: bool = False,
        unit_conflict_warning: str | None = None,
    ) -> ModelCellReadResult:
        obj = super().__new__(cls, (value, unit_hint, error))
        obj.value = value
        obj.unit_hint = unit_hint
        obj.error = error
        obj.stored_as_ratio = bool(stored_as_ratio)
        obj.unit_conflict_warning = unit_conflict_warning
        return obj


def _infer_xml_cell_unit_and_ratio(
    cells: dict[str, ET.Element],
    c_elem: ET.Element,
    coord: str,
    shared_strings: list[str],
    xf_formats: list[str],
) -> tuple[str | None, bool, str | None]:
    """Infer ``(unit_hint, stored_as_ratio, unit_conflict_warning)`` for ``coord``.

    ``stored_as_ratio`` is determined solely by the cell's Excel ``number_format``.
    When ``stored_as_ratio=True``, the cell's effective unit is always ``"pct"``; any non-``pct``
    unit label on the row, column, or sheet-level ``单位：`` note is invalidated and recorded as
    ``"格式为百分比，单位标签不一致"``.
    """
    stored_as_ratio = _cell_stored_as_ratio(c_elem, xf_formats)
    label_unit: str | None = None
    rc = _split_cell_coord(coord)
    if rc is not None:
        row_idx, col_idx = rc
        for c in range(1, min(col_idx, 4)):
            h_coord = f"{_col_idx_to_letters(c)}{row_idx}"
            h_elem = cells.get(h_coord)
            if h_elem is not None:
                h_val, h_err = _decode_cell_element(h_elem, shared_strings, h_coord)
                if h_err is None and isinstance(h_val, str):
                    u = _parse_unit_from_label_text(h_val)
                    if u:
                        label_unit = u
                        break
        if label_unit is None:
            col_letters = _col_idx_to_letters(col_idx)
            for r in range(1, min(row_idx, 6)):
                h_coord = f"{col_letters}{r}"
                h_elem = cells.get(h_coord)
                if h_elem is not None:
                    h_val, h_err = _decode_cell_element(h_elem, shared_strings, h_coord)
                    if h_err is None and isinstance(h_val, str):
                        u = _parse_unit_from_label_text(h_val)
                        if u:
                            label_unit = u
                            break
        if label_unit is None:
            for r in range(1, min(row_idx, 6)):
                for c in range(1, 4):
                    h_coord = f"{_col_idx_to_letters(c)}{r}"
                    h_elem = cells.get(h_coord)
                    if h_elem is not None:
                        h_val, h_err = _decode_cell_element(
                            h_elem, shared_strings, h_coord
                        )
                        if h_err is None and isinstance(h_val, str):
                            u = _parse_sheet_level_unit_note(h_val)
                            if u:
                                label_unit = u
                                break
                if label_unit is not None:
                    break

    if stored_as_ratio:
        unit_conflict_warning = (
            UNIT_FORMAT_CONFLICT_WARNING
            if (label_unit is not None and label_unit != "pct")
            else None
        )
        return _PctNumberFormatUnit("pct"), True, unit_conflict_warning

    resolved_unit = label_unit
    if resolved_unit is None:
        s_attr = (c_elem.attrib.get("s") or "").strip()
        if s_attr.isdigit():
            s_idx = int(s_attr)
            if 0 <= s_idx < len(xf_formats):
                resolved_unit = _parse_unit_from_number_format(xf_formats[s_idx])
    return resolved_unit, False, None


def _infer_xml_cell_unit_hint(
    cells: dict[str, ET.Element],
    c_elem: ET.Element,
    coord: str,
    shared_strings: list[str],
    xf_formats: list[str],
) -> str | None:
    """Infer unit for ``coord`` from same-row label, same-column header, explicit sheet ``单位：`` note, then cell ``number_format``."""
    unit_hint, _, _ = _infer_xml_cell_unit_and_ratio(
        cells, c_elem, coord, shared_strings, xf_formats
    )
    return unit_hint


def read_model_cell(
    raw_bytes: bytes, sheet: str, cell: str
) -> ModelCellReadResult:
    """Read ``ModelCellReadResult(value, unit_hint, error, stored_as_ratio=..., unit_conflict_warning=...)`` from an ``.xlsx`` model file at ``(sheet, cell)`` using only the Python standard library."""
    try:
        sheet_name = (sheet or "").strip().strip("'\"")
        cell_coord = _normalize_cell_coord(cell)
        if not cell_coord or _split_cell_coord(cell_coord) is None:
            return ModelCellReadResult(
                None, None, f"模型单元格坐标无效 ({cell!r})"
            )
        if not raw_bytes:
            return ModelCellReadResult(None, None, "模型文件为空")
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            sheets = _resolve_workbook_sheets(zf)
            target_ws_path = ""
            resolved_sheet_name = sheet_name
            if sheet_name:
                for sname, spath in sheets:
                    if sname == sheet_name:
                        target_ws_path = spath
                        resolved_sheet_name = sname
                        break
                if not target_ws_path:
                    for sname, spath in sheets:
                        if sname.lower() == sheet_name.lower():
                            target_ws_path = spath
                            resolved_sheet_name = sname
                            break
                if not target_ws_path:
                    return ModelCellReadResult(
                        None, None, f"模型文件中没有工作表 {sheet_name}"
                    )
            else:
                if sheets:
                    resolved_sheet_name, target_ws_path = sheets[0]
                else:
                    return ModelCellReadResult(
                        None, None, "模型文件中没有可用工作表"
                    )

            shared_strings = _load_shared_strings(zf)
            xf_formats = _load_style_num_formats(zf)
            cells = _parse_worksheet_cells(zf.read(target_ws_path))

            loc = (
                f"{resolved_sheet_name}!{cell_coord}"
                if resolved_sheet_name
                else cell_coord
            )
            c_elem = cells.get(cell_coord)
            if c_elem is None:
                return ModelCellReadResult(
                    None, None, f"模型单元格 {loc} 不存在或为空"
                )

            unit_hint, stored_as_ratio, unit_conflict_warning = (
                _infer_xml_cell_unit_and_ratio(
                    cells, c_elem, cell_coord, shared_strings, xf_formats
                )
            )
            val, err = _decode_cell_element(c_elem, shared_strings, loc)
            if err is not None:
                return ModelCellReadResult(
                    None,
                    unit_hint,
                    err,
                    stored_as_ratio=stored_as_ratio,
                    unit_conflict_warning=unit_conflict_warning,
                )
            return ModelCellReadResult(
                val,
                unit_hint,
                None,
                stored_as_ratio=stored_as_ratio,
                unit_conflict_warning=unit_conflict_warning,
            )
    except Exception as e:
        return ModelCellReadResult(None, None, f"模型单元格读取失败：{e}")


def read_xlsx_cell(
    xlsx_bytes: bytes, sheet_and_cell: str
) -> tuple[float | None, str | None]:
    """Read a numeric value from ``xlsx_bytes`` at ``Sheet!Cell`` via ``read_model_cell``."""
    spec = (sheet_and_cell or "").strip().lstrip("#")
    if "!" in spec:
        sheet_name, cell_coord = spec.split("!", 1)
    else:
        sheet_name, cell_coord = "", spec
    val, _unit_hint, err = read_model_cell(xlsx_bytes, sheet_name, cell_coord)
    if err is not None:
        return None, err
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val), None
    return None, f"模型单元格 {spec} 非数值 ({val!r})"


def count_unrecalculated_formulas(raw_bytes: bytes) -> int:
    """Count formula cells (``<c>`` with ``<f>``) across all worksheets in ``raw_bytes`` that lack a cached ``<v>``.

    Raises ``ValueError`` if ``raw_bytes`` is empty or if the ZIP archive or any workbook/worksheet XML is corrupt.
    """
    if not raw_bytes:
        raise ValueError("模型文件为空")
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            sheets = _resolve_workbook_sheets(zf)
            ws_paths = [p for _, p in sheets]
            for n in sorted(zf.namelist()):
                if (
                    n.startswith("xl/worksheets/")
                    and n.endswith(".xml")
                    and n not in ws_paths
                ):
                    ws_paths.append(n)
            if not ws_paths:
                raise ValueError("模型文件中没有可用工作表")
            seen_paths: set[str] = set()
            missing_v_count = 0
            for ws_path in ws_paths:
                if ws_path in seen_paths:
                    continue
                seen_paths.add(ws_path)
                ws_root = ET.fromstring(zf.read(ws_path))
                for elem in ws_root.iter():
                    if _local_tag(elem.tag) != "c":
                        continue
                    f_elem = _find_child(elem, "f")
                    if f_elem is None:
                        continue
                    v_elem = _find_child(elem, "v")
                    is_elem = _find_child(elem, "is")
                    c_type = (elem.attrib.get("t") or "").strip()
                    v_text = (v_elem.text or "").strip() if v_elem is not None else ""
                    if c_type == "str":
                        has_cached = v_elem is not None
                    elif c_type == "inlineStr":
                        has_cached = is_elem is not None or v_elem is not None
                    else:
                        has_cached = bool(v_text)
                    if not has_cached:
                        missing_v_count += 1
            return missing_v_count
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"模型文件解析失败：{e}") from e


_CONSENSUS_METRIC_HINTS = (
    "一致预期",
    "共识",
    "consensus",
    "street",
    "卖方",
    "券商",
    "分析师",
)


def _RequiresModelCellOnly(fig: ExtractedFigure) -> bool:
    """House target price, IRR, and 3-scenario returns must point to a model cell (PK.9)."""
    if (fig.label_hint or "").strip().lower() == "consensus":
        return False
    low = (fig.metric or "").lower()
    if any(h in low for h in _CONSENSUS_METRIC_HINTS):
        return False
    g = canonical_metric_group(fig.metric)
    return g in ("target_price", "irr") or any(
        _alias_in_metric(k, low)
        for k in ("目标价", "target price", "irr", "情景收益", "三情景")
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


def _extract_hit_window(
    body: str,
    fig: ExtractedFigure,
    *,
    window: int = 1000,
    source_cache: SourceParseCache | None = None,
) -> str:
    """Extract a window of up to `window` chars centered around the best figure/raw/period hit in `body`."""
    if not body or len(body) <= window:
        return body

    hit_span: tuple[int, int] | None = None
    if fig.raw:
        idx = body.find(fig.raw)
        if idx >= 0:
            hit_span = (idx, idx + len(fig.raw))

    if hit_span is None:
        sfs = (
            source_cache.figures(body)
            if source_cache is not None
            else extract_figures(body)
        )
        # ExtractedFigure carries no character offsets, only its line, so a
        # figure is found again as the n-th occurrence of its raw text on that
        # line (counting the exempt figures before it). Line starts follow the
        # extractor's own splitlines(), not just "\n".
        line_starts: list[int] = []
        offset = 0
        for chunk in body.splitlines(keepends=True):
            line_starts.append(offset)
            offset += len(chunk)
        seen_on_line: dict[tuple[Any, str], int] = {}
        for sf in sfs:
            line_no = (sf.location or {}).get("line")
            occurrence = seen_on_line.get((line_no, sf.raw), 0)
            seen_on_line[(line_no, sf.raw)] = occurrence + 1
            if sf.exempt:
                continue
            ok_v, _, _ = values_within_tolerance(fig, sf.value, sf.unit, sf.scale)
            if not ok_v or not sf.raw:
                continue
            pos = -1
            if isinstance(line_no, int) and 1 <= line_no <= len(line_starts):
                cursor = line_starts[line_no - 1]
                for _ in range(occurrence + 1):
                    pos = body.find(sf.raw, cursor)
                    if pos < 0:
                        break
                    cursor = pos + len(sf.raw)
            if pos < 0:
                pos = body.find(sf.raw)
            if pos >= 0:
                hit_span = (pos, pos + len(sf.raw))
                break

    if hit_span is None and fig.period:
        fig_p = normalize_period(fig.period)
        if fig_p:
            idx = body.lower().find(fig_p.lower())
            if idx >= 0:
                hit_span = (idx, idx + len(fig_p))

    if hit_span is None:
        return body[:window]

    center = (hit_span[0] + hit_span[1]) // 2
    half = window // 2
    start = max(0, center - half)
    end = min(len(body), start + window)
    if end - start < window:
        start = max(0, end - window)
    return body[start:end]


def _merge_model_cells_with_models_doc(
    model_cells: dict[str, dict[str, Any]] | None,
    models_doc: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Merge ``model_cells`` (including ``from_xlsx`` reads) with ``models_doc`` ``key_outputs``.

    Real ``from_xlsx`` cell values and read errors are never overwritten by ``key_outputs``;
    ``key_outputs`` only supplies metadata (``unit`` at highest priority, ``period``, ``metric``).
    When a model file or cell has a ``from_xlsx`` read error, ``key_outputs`` never fills a fallback numeric value.
    """
    cells_map: dict[str, dict[str, Any]] = {}
    file_level_errors: dict[str, dict[str, Any]] = {}
    for k, v in (model_cells or {}).items():
        if isinstance(v, dict):
            entry_copy = dict(v)
            s_ratio = bool(
                entry_copy.get("stored_as_ratio")
                or entry_copy.get("pct_from_number_format")
                or getattr(entry_copy.get("unit"), "stored_as_ratio", False)
                or getattr(entry_copy.get("unit"), "is_pct_number_format", False)
            )
            if s_ratio:
                raw_u = str(entry_copy.get("unit") or "").strip()
                if raw_u and raw_u.lower() not in ("pct", "%", "other", "none"):
                    entry_copy["unit_conflict_warning"] = (
                        entry_copy.get("unit_conflict_warning")
                        or UNIT_FORMAT_CONFLICT_WARNING
                    )
                entry_copy["unit"] = "pct"
                entry_copy["stored_as_ratio"] = True
                entry_copy["pct_from_number_format"] = True
            cells_map[k] = entry_copy
            norm_k = _normalize_model_spec(k)
            if norm_k not in cells_map:
                cells_map[norm_k] = dict(entry_copy)
            if v.get("from_xlsx") and v.get("error"):
                base_p = (
                    k.split("#", 1)[0]
                    if "#" in k
                    else (k.split("!", 1)[0] if "!" in k else k)
                ).strip()
                if base_p and (
                    k == base_p
                    or "读取失败" in str(v.get("error") or "")
                    or "为空" in str(v.get("error") or "")
                ):
                    file_level_errors[_normalize_vpath(base_p)] = dict(entry_copy)
    for ver in (models_doc or {}).get("versions") or []:
        if not isinstance(ver, dict):
            continue
        vpath = str(ver.get("path") or "").strip()
        norm_vpath = _normalize_vpath(vpath)
        recalc = ver.get("recalculated", True)
        ver_sha = str(ver.get("sha256") or "")
        file_err = file_level_errors.get(norm_vpath)
        for ko in ver.get("key_outputs") or []:
            if not isinstance(ko, dict) or not ko.get("cell"):
                continue
            cell_ref = str(ko["cell"]).strip()
            hash_key = f"{vpath}#{cell_ref}"
            bang_key = f"{vpath}!{cell_ref}"
            norm_hash_key = _normalize_model_spec(hash_key)
            ko_unit_raw = str(ko.get("unit") or "").strip()
            ko_unit = ko_unit_raw if ko_unit_raw.lower() not in ("", "other", "none") else ""
            ko_period = normalize_period(ko.get("period"))
            ko_metric = str(ko.get("name") or "")

            existing = (
                cells_map.get(hash_key)
                or cells_map.get(bang_key)
                or cells_map.get(norm_hash_key)
            )
            if existing is None and file_err is not None:
                existing = dict(file_err)
            if existing is not None and existing.get("from_xlsx"):
                merged = dict(existing)
                stored_ratio = bool(
                    existing.get("stored_as_ratio")
                    or existing.get("pct_from_number_format")
                    or getattr(existing.get("unit"), "stored_as_ratio", False)
                    or getattr(existing.get("unit"), "is_pct_number_format", False)
                )
                merged["stored_as_ratio"] = stored_ratio
                merged["pct_from_number_format"] = stored_ratio
                if stored_ratio:
                    merged["unit"] = "pct"
                    merged["currency"] = None
                    if ko_unit and ko_unit.lower() not in ("pct", "%"):
                        merged["unit_conflict_warning"] = (
                            merged.get("unit_conflict_warning")
                            or UNIT_FORMAT_CONFLICT_WARNING
                        )
                elif ko_unit:
                    if ko_unit == "per_share":
                        ko_cur = (
                            _resolve_per_share_currency(
                                existing.get("unit", ""), existing.get("currency")
                            )
                            or _infer_currency_from_text(ko_metric)
                        )
                        merged["unit"] = _PerShareUnit("per_share", currency=ko_cur)
                        merged["currency"] = ko_cur
                    else:
                        merged["unit"] = ko_unit
                elif str(merged.get("unit") or "") == "per_share":
                    cell_cur = _resolve_per_share_currency(
                        existing.get("unit", ""), existing.get("currency")
                    )
                    merged["unit"] = _PerShareUnit("per_share", currency=cell_cur)
                    merged["currency"] = cell_cur
                if ko_period and not merged.get("period"):
                    merged["period"] = ko_period
                if ko_metric and not merged.get("metric"):
                    merged["metric"] = ko_metric
                merged["recalculated"] = recalc
                if ver_sha and not merged.get("sha256"):
                    merged["sha256"] = ver_sha
                cells_map[hash_key] = merged
                cells_map[norm_hash_key] = merged
                if bang_key in cells_map:
                    cells_map[bang_key] = merged
            elif existing is None and ko.get("value") is not None:
                eff_ko_unit: str = ko_unit
                if ko_unit == "per_share":
                    eff_ko_unit = _PerShareUnit(
                        "per_share", currency=_infer_currency_from_text(ko_metric)
                    )
                ko_entry = {
                    "value": float(ko["value"]),
                    "unit": eff_ko_unit,
                    "stored_as_ratio": False,
                    "pct_from_number_format": False,
                    "period": ko_period,
                    "metric": ko_metric,
                    "recalculated": recalc,
                    "sha256": ver_sha,
                }
                cells_map[hash_key] = ko_entry
                cells_map[norm_hash_key] = ko_entry
    return cells_map


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
    evidence_ref_bodies: dict[str, str] | None = None,
    draft_path: str = "",
    deadline: float | None = None,
) -> list[MatchOutcome]:
    """Step C: Match footnoted figures (Orders 0–3) and collect candidates for unfootnoted figures (P0I §1.2, L34, L36).

    Unfootnoted figures NEVER become `matched=True` here; Order 0b and Order 4 only produce candidates for Step D.

    ``deadline`` (``time.monotonic()``) is checked once per figure: at the
    budget every figure not yet reached is returned marked 核验未完成 and
    locked out of Step D, so a big draft returns its partial results instead
    of running on for hours (A5 — one audit ran 103 minutes past its budget
    because nothing in this loop looked at the clock).
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
    cells_map = _merge_model_cells_with_models_doc(model_cells, models_doc)

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
                ef_unit_raw = ef.get("unit") or "other"
                if isinstance(ef_unit_raw, _PerShareUnit):
                    ef_unit: str = ef_unit_raw
                else:
                    ef_unit = str(ef_unit_raw)
                    if ef_unit == "per_share":
                        ef_cur = _resolve_ef_per_share_currency(
                            ef, ev, source_cache
                        )
                        ef_unit = _PerShareUnit("per_share", currency=ef_cur)

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

                fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale, fig.currency)
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

            # Also inspect primary file body pointed to by evidence source.ref (excluding draft itself),
            # only when structured figures[] / fact had no conclusion (`not cands`).
            ref_path = _extract_evidence_ref_path(ev)
            is_self_ref = _is_draft_self_ref(ref_path, draft_path)
            _resolved_path, ref_body = _resolve_evidence_ref_body(
                ev, evidence_ref_bodies, draft_path=draft_path
            )
            if not cands and ref_body:
                r_hit, r_excerpt, r_reason, r_hint, r_cands = _inspect_text_source(
                    fig,
                    ref_body,
                    source_id=eid,
                    kind="evidence",
                    metric_aliases=metric_aliases,
                    source_cache=source_cache,
                )
                if r_hit:
                    return MatchOutcome(
                        figure=fig,
                        matched=True,
                        kind="evidence",
                        ref=eid,
                        excerpt=r_excerpt or fact_text,
                    )
                if r_cands:
                    cands.extend(r_cands)
                    if r_reason == "source_conflict":
                        has_source_conflict = True
                    mismatch_hint = mismatch_hint or r_hint

            if cands:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="source_conflict" if has_source_conflict else "unverified",
                    hint=mismatch_hint if not has_source_conflict else f"数值与来源 {eid} 冲突",
                    candidates=cands,
                )
            if is_self_ref:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"证据 {eid} 的来源指向草稿自身，不能用于核实",
                )
            return None

        # Order 2: model:<path>#<Sheet>!<Cell> or model:<path>!<Sheet>!<Cell>
        if target_ref.startswith("model:"):
            m_spec = target_ref.split(":", 1)[1].strip()
            norm_spec = _normalize_model_spec(m_spec)
            cell_info = (
                cells_map.get(m_spec)
                or cells_map.get(norm_spec)
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
            if cell_info.get("error"):
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=str(cell_info["error"]),
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
            if cell_info.get("value") is None:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"Model cell {m_spec} has no numeric value",
                    lock_unverified=True,
                )
            c_val = float(cell_info["value"])
            c_unit_obj = cell_info.get("unit")
            raw_c_unit = str(c_unit_obj or "").strip()
            c_stored_as_ratio = bool(
                cell_info.get("stored_as_ratio")
                or cell_info.get("pct_from_number_format")
                or getattr(c_unit_obj, "stored_as_ratio", False)
                or getattr(c_unit_obj, "is_pct_number_format", False)
            )
            unit_conflict_msg = str(
                cell_info.get("unit_conflict_warning") or ""
            ).strip()
            if c_stored_as_ratio:
                if raw_c_unit and raw_c_unit.lower() not in (
                    "",
                    "pct",
                    "%",
                    "other",
                    "none",
                ):
                    unit_conflict_msg = (
                        unit_conflict_msg or UNIT_FORMAT_CONFLICT_WARNING
                    )
                has_reliable_unit = True
                c_unit: str = "pct"
                c_currency: str | None = None
            else:
                has_reliable_unit = bool(
                    raw_c_unit and raw_c_unit.lower() not in ("other", "none")
                )
                c_currency = (
                    getattr(c_unit_obj, "currency", None)
                    or cell_info.get("currency")
                )
                if has_reliable_unit and raw_c_unit == "per_share":
                    c_unit = _PerShareUnit("per_share", currency=c_currency)
                elif has_reliable_unit and isinstance(c_unit_obj, _PerShareUnit):
                    c_unit = c_unit_obj
                else:
                    c_unit = raw_c_unit if has_reliable_unit else "other"
            c_period = normalize_period(cell_info.get("period"))
            fig_p = normalize_period(fig.period)
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
            if not has_reliable_unit:
                ulp_half = 0.5 * (10.0 ** (-fig.decimals))
                fig_is_unitless = (
                    (fig.unit or "") in ("", "other", "count")
                    and fig.scale == 1.0
                    and fig.currency is None
                )
                if fig_is_unitless and abs(fig.value - c_val) <= ulp_half * 1.0001:
                    return MatchOutcome(
                        figure=fig,
                        matched=True,
                        kind="model",
                        ref=m_spec,
                        excerpt=f"{m_spec} = {c_val}",
                        source_sha256=str(cell_info.get("sha256") or ""),
                    )
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"模型单元格 {m_spec} 缺少可靠单位，无法核实",
                    candidates=[
                        MatchCandidate(
                            kind="model",
                            id=m_spec,
                            value=c_val,
                            unit="other",
                            period=c_period,
                            why=f"模型 {m_spec} 为 {c_val}（无可靠单位），正文为 {fig.raw}",
                        )
                    ],
                )

            ok_val, unit_mis, _ = values_within_tolerance(
                fig,
                c_val,
                c_unit,
                stored_as_ratio=c_stored_as_ratio,
                src_currency=c_currency,
            )
            if ok_val:
                excerpt_str = f"{m_spec} = {c_val} {c_unit}"
                if unit_conflict_msg:
                    excerpt_str = f"{excerpt_str}（{unit_conflict_msg}）"
                return MatchOutcome(
                    figure=fig,
                    matched=True,
                    kind="model",
                    ref=m_spec,
                    excerpt=excerpt_str,
                    hint=unit_conflict_msg,
                    source_sha256=str(cell_info.get("sha256") or ""),
                )
            eff_c_val = c_val * 100.0 if (c_stored_as_ratio and c_unit == "pct") else c_val
            fig_base, fig_dim = _to_base(fig.value, fig.unit, fig.scale, fig.currency)
            c_base, c_dim = _to_base(eff_c_val, c_unit, 1.0)
            dims_ok = _are_dimensions_compatible(
                fig, fig_base, fig_dim, c_unit, 1.0, c_base, c_dim
            )
            conflict_suffix = f"（{unit_conflict_msg}）" if unit_conflict_msg else ""
            if unit_mis or not dims_ok:
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"脚注指向的证据量纲与正文不一致{conflict_suffix}",
                    candidates=[
                        MatchCandidate(
                            kind="model",
                            id=m_spec,
                            value=c_val,
                            unit=c_unit,
                            period=c_period,
                            why=f"单位/量纲不匹配：模型 {m_spec} 为 {c_val} {c_unit}{conflict_suffix}",
                        )
                    ],
                )
            return MatchOutcome(
                figure=fig,
                matched=False,
                reason="source_conflict",
                hint=f"数值与模型单元格 {m_spec} 冲突{conflict_suffix}",
                candidates=[
                    MatchCandidate(
                        kind="model",
                        id=m_spec,
                        value=c_val,
                        unit=c_unit,
                        period=c_period,
                        why=f"数值超容差：模型 {m_spec} 为 {c_val} {c_unit}{conflict_suffix}",
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
            if _is_draft_provenance_record(prec, draft_path):
                return MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="unverified",
                    hint=f"来源记录 {pid} 指向草稿自身，不能用于核实",
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

        # A5: the per-figure deadline check — one figure × every source body is
        # where the unbounded work lives, so the budget is enforced here. Cheap
        # classifications (exempt/incidental) above still get their real outcome.
        if deadline is not None and time.monotonic() >= deadline:
            outcomes.append(
                MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="uncertain",
                    hint="核验未完成（截止时间已到）— 交由 PM 决定",
                    lock_unverified=True,
                )
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

        # Order 0b: facts ledger candidates
        for fid, frow in facts_map.items():
            if metrics_match(fig.metric, str(frow.get("metric") or ""), metric_aliases):
                fp = normalize_period(frow.get("period"))
                if not fig.period or not fp or normalize_period(fig.period) == fp:
                    ok_v, _, _ = values_within_tolerance(
                        fig, float(frow["value"]), str(frow.get("unit") or "other")
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
                fig, float(mvf["value"]), str(mvf.get("unit") or "other")
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
            if c_info.get("error") or c_info.get("value") is None:
                continue
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
            if _is_draft_provenance_record(prec, draft_path):
                continue
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
                hint="无引用，未在来源中找到",
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
    evidence_ref_bodies: dict[str, str] | None = None,
    draft_path: str = "",
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

    # 1. thesis.json evidence[] (excluding legacy_thesis; draft self-ref bodies excluded via _resolve_evidence_ref_body)
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
            ef_u_raw = ef.get("unit") or "other"
            if isinstance(ef_u_raw, _PerShareUnit):
                ef_u: str = ef_u_raw
            else:
                ef_u = str(ef_u_raw)
                if ef_u == "per_share":
                    ef_u = _PerShareUnit(
                        "per_share",
                        currency=_resolve_ef_per_share_currency(ef, ev, cache),
                    )
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
        ref_path, ref_body = _resolve_evidence_ref_body(
            ev, evidence_ref_bodies, draft_path=draft_path
        )
        if _is_draft_self_ref(ref_path, draft_path) and not fig_lines:
            continue
        ref_snippet = ""
        if ref_body:
            rv_hit, rp_hit = _check_text_value_and_period(ref_body)
            val_hit = max(val_hit, rv_hit)
            per_hit = max(per_hit, rp_hit)
            if fig.metric and metrics_match(fig.metric, ref_body, metric_aliases):
                met_hit = 1
            if fig.raw and fig.raw in ref_body:
                raw_hit = 1
            ref_snippet = _extract_hit_window(ref_body, fig, window=1000, source_cache=cache)

        combined_text = fact_str
        if fig_lines:
            combined_text = f"{fact_str} | Figures: {'; '.join(fig_lines)}".strip(" |")
        if ref_snippet:
            combined_text = f"{combined_text} | Ref({ref_path}): {ref_snippet}".strip(" |")
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
    merged_cells = _merge_model_cells_with_models_doc(model_cells, models_doc)
    for m_spec, c_info in merged_cells.items():
        if c_info.get("error") or c_info.get("value") is None:
            continue
        fn_hit = int(
            fn_target == f"model:{m_spec}"
            or fn_target == f"model:{m_spec.replace('#', '!', 1)}"
        )
        cm = str(c_info.get("metric") or "")
        cp = normalize_period(c_info.get("period"))
        cu_obj = c_info.get("unit")
        cu: str = cu_obj if isinstance(cu_obj, str) and cu_obj else "other"
        c_cur = getattr(cu_obj, "currency", None) or c_info.get("currency")
        val_hit = 0
        if c_info.get("value") is not None:
            try:
                c_stored_as_ratio = bool(
                    c_info.get("stored_as_ratio")
                    or c_info.get("pct_from_number_format")
                    or getattr(cu_obj, "stored_as_ratio", False)
                    or getattr(cu_obj, "is_pct_number_format", False)
                )
                ok_c, _, _ = values_within_tolerance(
                    fig,
                    float(c_info["value"]),
                    cu,
                    stored_as_ratio=c_stored_as_ratio,
                    src_currency=None if c_stored_as_ratio else c_cur,
                )
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

    # 3. provenance_records (excluding draft self-reads)
    for pid, prec in (provenance_records or {}).items():
        if not isinstance(prec, dict):
            continue
        if _is_draft_provenance_record(prec, draft_path):
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
            "text": _extract_hit_window(body, fig, window=1000, source_cache=cache),
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


