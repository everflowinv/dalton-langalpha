"""Deterministic extraction of financial figures from Markdown prose and tables (PK.9)."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from pydantic import BaseModel, Field

# Period tokens: FY2026, FY26, 3Q26, Q3 2026, 2026Q3, 1H26, 2H26, 2026H1, LTM-2026-09, 2026E, NTM
_PERIOD_TOKEN_RE = re.compile(
    r"\b("
    r"FY\d{2,4}E?|"
    r"[1-4]Q\d{2,4}E?|"
    r"Q[1-4]\s*['’]?\d{2,4}E?|"
    r"20\d{2}\s*Q[1-4]E?|"
    r"[12]H\d{2,4}E?|"
    r"20\d{2}\s*H[12]E?|"
    r"LTM(?:-\d{4}-\d{2})?|"
    r"NTM|"
    r"20\d{2}E"
    r")\b",
    re.IGNORECASE,
)

# Standalone date / period / filing form / identifier patterns to exclude
_SEC_ACCESSION_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_HYPHEN_DOC_CODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}\b|\b[A-Za-z]{2,}-\d{2,}-\d+\b"
)
_ISO_DATE_RE = re.compile(r"\b20\d{2}[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?\b")
_CN_DATE_RE = re.compile(r"20\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?")
_SHORT_DATE_RE = re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?!\d)")
_YEAR_RANGE_RE = re.compile(r"\b20\d{2}\s*[-–~至]\s*20\d{2}\b")
_STANDALONE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_FILING_FORM_RE = re.compile(r"\b(?:10-K|10-Q|8-K|6-K|20-F|DEF\s*14A|S-1|13F)\b", re.IGNORECASE)
_TICKER_CODE_RE = re.compile(r"\b\d{4,6}\.(?:HK|SH|SZ|SS|TW)\b", re.IGNORECASE)
_ENTITY_ID_RE = re.compile(
    r"\b(?:P\d+|D\d+|K\d+(?:\.\d+)?|Q-\d{3}|V-\d{3}|T\d+|L-\d{3}|G-\d{3,4}|"
    r"W\d+|C\d+|H\d+|v\d+|E-\d{8}-\d{2,3}|E\d{3,6}|VD-\d{8}-\d{2,3}|F-\d{3})\b"
)
_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^([^\]]+)\]:\s*(.+?)\s*$")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s+(?:\d+(?:\.\d+)*\.?\s*)?")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|（\d+）|\(\d+\))\s+")

# Structural prose counts like "3 个支柱", "第 2 季度", "第 1 节", "2 条分歧"
_STRUCTURAL_COUNT_RE = re.compile(
    r"(?:第\s*\d+\s*(?:季度|节|章|步|阶段|部分|页|行|列|条|项|版|次)|"
    r"\b\d+\s*(?:个支柱|条支柱|条分歧|个分歧|个问题|条问题|个驱动|条经验|家公司|个工作区|页|行))"
)

# Exempt markers for illustrative / assumption / estimate examples with derivation
_EXEMPT_MARKER_RE = re.compile(
    r"(?:〔示意〕|\[示意\]|（示意）|〔假设〕|\[假设\]|（假设）|〔估计〕|\[估计\]|（估计）|（举例）)"
)

# Chinese prose fraction / multiple expressions ("三成", "三成半", "五分之一", "翻倍", "翻一番", "减半")
_CN_DIGIT_MAP: dict[str, float] = {
    "一": 1.0,
    "二": 2.0,
    "两": 2.0,
    "三": 3.0,
    "四": 4.0,
    "五": 5.0,
    "六": 6.0,
    "七": 7.0,
    "八": 8.0,
    "九": 9.0,
    "十": 10.0,
}
_CN_PROSE_FIG_RE = re.compile(
    r"(?P<cheng>(?<![一二三四五六七八九十])[一二三四五六七八九]成(?:半)?(?!不变))|"
    r"(?P<zhe>(?<![一二三四五六七八九十])[一二三四五六七八九]折)|"
    r"(?P<frac>[一二三四五六七八九十]+\s*分之\s*[一二三四五六七八九十]+)|"
    r"(?P<mult>翻两番|翻一番|翻倍|减半)"
)

# Core financial number regex:
# Captures optional sign/parens, optional currency prefix, number with commas/decimals,
# optional scale (k/mn/m/bn/b/billion/million/万/亿/千亿/百亿/百万), optional unit (%/pp/bp/bps/x/倍/美元/港元/元)
_NUMBER_RE = re.compile(
    r"(?P<full>"
    r"(?P<open_paren>\()?"
    r"(?P<sign>[+\-−])?\s*"
    r"(?P<currency>US\$|HK\$|RMB|CNY|USD|HKD|\$|¥)?"
    r"\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s*"
    r"(?P<scale>千亿|百亿|百万|万亿|亿|万|billion|million|bn|mn|(?:k|m|b)(?![a-zA-Z]))?"
    r"\s*"
    r"(?P<unit>bps|bp|pp|pts|%|倍|x(?![a-zA-Z])|美元|港元|人民币|元)?"
    r"(?P<close_paren>\))?"
    r")",
    re.IGNORECASE,
)

SCALE_MULTIPLIERS: dict[str, float] = {
    "k": 1e3,
    "万": 1e4,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "百万": 1e6,
    "亿": 1e8,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "百亿": 1e10,
    "千亿": 1e11,
    "万亿": 1e12,
}

CURRENCY_MAP: dict[str, str] = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "美元": "USD",
    "hk$": "HKD",
    "hkd": "HKD",
    "港元": "HKD",
    "¥": "CNY",
    "rmb": "CNY",
    "cny": "CNY",
    "人民币": "CNY",
    "元": "CNY",
}


class FigureLabelItem(BaseModel):
    """LLM-annotated metric, period, and optional unit for one extracted figure ID."""

    id: str = Field(description="Extracted figure ID, e.g. F-001")
    metric: str = Field(default="", description="Financial metric name")
    period: str = Field(default="", description="Normalized period, e.g. 3Q26 or FY2026")
    unit: str = Field(default="", description="Optional canonical unit, e.g. USD_bn, USD_mn, pct, x, count_mn")


class ExtraFigureItem(BaseModel):
    """Additional prose figure identified by LLM (e.g. Chinese expressions like '三成', '翻倍')."""

    raw: str = Field(description="Exact substring in the draft sentence, e.g. '三成' or '翻倍'")
    line: int = Field(default=1, description="1-based line number in the draft")
    value: float = Field(description="Numeric value (e.g. 30.0 for 三成, 2.0 for 翻倍)")
    unit: str = Field(default="pct", description="Canonical unit, e.g. pct or x")
    metric: str = Field(default="", description="Financial metric name")
    period: str = Field(default="", description="Normalized period if any")


class FigureLabels(BaseModel):
    """Structured response for LLM-assisted prose figure labeling (PK.9 / G3.1)."""

    items: list[FigureLabelItem] = Field(default_factory=list)
    extra_figures: list[ExtraFigureItem] = Field(default_factory=list)


@dataclasses.dataclass
class ExtractedFigure:
    """A single figure extracted from a draft report (PK.9)."""

    id: str
    raw: str
    value: float
    decimals: int
    unit: str
    scale: float
    currency: str | None
    metric: str
    period: str
    label_hint: str
    footnote_id: str | None = None
    footnote_target: str | None = None
    location: dict[str, Any] = dataclasses.field(default_factory=dict)
    exempt: bool = False
    exempt_reason: str | None = None

    @property
    def scaled_value(self) -> float:
        return self.value * self.scale


def normalize_period(raw_period: str | None) -> str:
    """Canonicalize period strings (e.g. ``FY26`` -> ``FY2026``, ``Q3 2026`` -> ``3Q26``, ``3Q2026`` -> ``3Q26``)."""
    if not raw_period:
        return ""
    s = raw_period.strip().upper().rstrip("E")
    m_fy_short = re.match(r"^FY(\d{2})$", s)
    if m_fy_short:
        return f"FY20{m_fy_short.group(1)}"
    m_fy_long = re.match(r"^FY(\d{4})$", s)
    if m_fy_long:
        return f"FY{m_fy_long.group(1)}"
    m_q_first = re.match(r"^([1-4])Q(?:20)?(\d{2})$", s)
    if m_q_first:
        return f"{m_q_first.group(1)}Q{m_q_first.group(2)}"
    m_q_second = re.match(r"^Q([1-4])\s*['’]?(?:20)?(\d{2})$", s)
    if m_q_second:
        return f"{m_q_second.group(1)}Q{m_q_second.group(2)}"
    m_year_q = re.match(r"^20(\d{2})\s*Q([1-4])$", s)
    if m_year_q:
        return f"{m_year_q.group(2)}Q{m_year_q.group(1)}"
    m_h = re.match(r"^([12])H(?:20)?(\d{2})$", s)
    if m_h:
        return f"{m_h.group(1)}H{m_h.group(2)}"
    m_year_h = re.match(r"^20(\d{2})\s*H([12])$", s)
    if m_year_h:
        return f"{m_year_h.group(2)}H{m_year_h.group(1)}"
    m_year = re.match(r"^(20\d{2})$", s)
    if m_year:
        return f"FY{m_year.group(1)}"
    return s


def _infer_scale_from_header(header_text: str) -> float:
    """Extract a scale multiplier from table row/column/caption headers (G3.1 R4-4)."""
    if not header_text:
        return 1.0
    if "万亿" in header_text:
        return 1e12
    if "千亿" in header_text:
        return 1e11
    if "百亿" in header_text:
        return 1e10
    if "亿" in header_text:
        return 1e8
    if "百万" in header_text:
        return 1e6
    if "万" in header_text:
        return 1e4
    low = header_text.lower()
    if re.search(r"(?:\b(?:billion|bn)\b|\$b\b|\(\s*b\s*\)|（\s*b\s*）|\b(?:usd|hkd|cny|rmb)\s*b\b)", low):
        return 1e9
    if re.search(r"(?:\b(?:million|mn)\b|\$m\b|\(\s*m\s*\)|（\s*m\s*）|\b(?:usd|hkd|cny|rmb)\s*m\b)", low):
        return 1e6
    if re.search(r"(?:\$k\b|\(\s*k\s*\)|（\s*k\s*）|\b(?:usd|hkd|cny|rmb)\s*k\b)", low):
        return 1e3
    return 1.0


def _determine_unit_and_scale(
    currency_tok: str | None,
    scale_tok: str | None,
    unit_tok: str | None,
    context_text: str = "",
    header_context: str = "",
) -> tuple[str | None, str, float]:
    """Return ``(currency, canonical_unit, scale_multiplier)``."""
    cur_raw = (currency_tok or "").strip().lower()
    unit_raw = (unit_tok or "").strip().lower()
    scale_raw = (scale_tok or "").strip().lower()

    if unit_raw in ("%", "pp", "pts"):
        return None, "pct", 1.0
    if unit_raw in ("bp", "bps"):
        return None, "bp", 1.0
    if unit_raw in ("x", "倍"):
        return None, "x", 1.0

    # When inside a table and the cell has no explicit currency/scale/unit token,
    # check if the row/col header specifies percentage, basis points, or multiple.
    if header_context and not cur_raw and not scale_raw and not unit_raw:
        if re.search(r"(?:[%％]|\b(?:pct|bps|bp)\b)", header_context, re.IGNORECASE):
            if re.search(r"\b(?:bps|bp)\b", header_context, re.IGNORECASE):
                return None, "bp", 1.0
            return None, "pct", 1.0
        if re.search(r"(?:[（(]\s*(?:倍|x)\s*[)）]|估值倍数|市盈率)", header_context, re.IGNORECASE):
            return None, "x", 1.0

    combined_ctx = f"{header_context} {context_text}".strip()
    currency = CURRENCY_MAP.get(cur_raw) or CURRENCY_MAP.get(unit_raw)
    if not currency and combined_ctx:
        low_ctx = combined_ctx.lower()
        if "美元" in combined_ctx or "usd" in low_ctx or "us$" in low_ctx or "$" in combined_ctx:
            currency = "USD"
        elif "港元" in combined_ctx or "hkd" in low_ctx or "hk$" in low_ctx:
            currency = "HKD"
        elif (
            "人民币" in combined_ctx
            or "亿元" in combined_ctx
            or "百万元" in combined_ctx
            or "万元" in combined_ctx
            or "cny" in low_ctx
            or "rmb" in low_ctx
            or "¥" in combined_ctx
        ):
            currency = "CNY"

    scale = SCALE_MULTIPLIERS.get(scale_raw, 1.0)
    if scale == 1.0 and header_context:
        scale = _infer_scale_from_header(header_context)

    if currency:
        if scale == 1e9:
            return currency, f"{currency}_bn", scale
        if scale == 1e6:
            return currency, f"{currency}_mn", scale
        if scale == 1e8 and currency == "CNY":
            return currency, "CNY_100mn", scale
        if scale == 1e8:
            return currency, f"{currency}_bn", 1e8
        return currency, currency, scale

    if scale == 1e6:
        return None, "count_mn", scale
    if scale > 1.0:
        return None, "count", scale
    return None, "other", 1.0


def _infer_label_hint(text: str) -> str:
    low = text.lower()
    if any(
        w in low
        for w in (
            "一致预期",
            "consensus",
            "street",
            "卖方一致",
            "彭博一致",
            "市场预期",
        )
    ):
        return "consensus"
    if any(w in low for w in ("指引", "guidance", "管理层预计", "公司预计")):
        return "guidance"
    if any(w in low for w in ("假设", "assumption", "假定")):
        return "assumption"
    if any(
        w in low
        for w in (
            "我们预计",
            "我们的预测",
            "我方预测",
            "house view",
            "house case",
            "预测",
            "目标价",
        )
    ):
        return "estimate"
    if any(
        w in low
        for w in (
            "同比",
            "环比",
            "yoy",
            "qoq",
            "增速",
            "增长",
            "利润率",
            "margin",
            "p/e",
            "ev/ebitda",
            "倍数",
            "cagr",
        )
    ):
        return "derived"
    return "reported"


_GENERIC_MODIFIER_WORDS = frozenset(
    {
        "同比",
        "环比",
        "增长",
        "增速",
        "同比增速",
        "同比增长",
        "环比增长",
        "环比增速",
        "上升",
        "下降",
        "增加",
        "减少",
        "提升",
        "扩大",
        "收窄",
        "下滑",
        "回落",
        "yoy",
        "qoq",
        "up",
        "down",
    }
)


def _extract_metric_from_context(
    sentence: str, match_start: int, match_end: int
) -> str:
    """Heuristically extract the metric phrase preceding or surrounding a number in prose."""
    before = sentence[:match_start]
    # Remove period tokens and footnote markers from 'before'
    cleaned = _PERIOD_TOKEN_RE.sub(" ", before)
    cleaned = _FOOTNOTE_REF_RE.sub(" ", cleaned)
    # Split on clause punctuation and take the nearest segment before the number
    clauses = re.split(r"[，,；;：:。！？!?\(\)（）—\-]", cleaned)
    non_empty_cands: list[str] = []
    for clause in reversed(clauses):
        cand = clause.strip()
        cand = re.sub(
            r"^(?:其中|以及|而|且|但|约|达到|为|至|录得|实现|高于|低于|维持|预计|我们的|公司)\s*",
            "",
            cand,
        ).strip()
        if len(cand) >= 2:
            non_empty_cands.append(cand)

    if non_empty_cands:
        nearest = non_empty_cands[0]
        # If the nearest clause is just a generic growth/change modifier (e.g. "同比"),
        # combine it with the preceding subject clause (e.g. "广告收入 512 亿美元") so metric matches "广告收入增速".
        if nearest.lower() in _GENERIC_MODIFIER_WORDS and len(non_empty_cands) >= 2:
            prev_clause = _NUMBER_RE.sub(" ", non_empty_cands[1]).strip()
            prev_clause = " ".join(prev_clause.split())
            if prev_clause:
                return f"{prev_clause} {nearest}"[-36:]
        return nearest[-32:]

    after = sentence[match_end : match_end + 24]
    after_clauses = re.split(r"[，,；;：:。！？!?\(\)（）]", after)
    if after_clauses and after_clauses[0].strip():
        return after_clauses[0].strip()[:24]
    return sentence.strip()[:32]


def parse_footnotes(markdown_text: str) -> dict[str, str]:
    """Parse Markdown footnote definitions ``[^id]: target`` into ``{id: target_prefix}``."""
    footnotes: dict[str, str] = {}
    for line in markdown_text.splitlines():
        m = _FOOTNOTE_DEF_RE.match(line)
        if m:
            fn_id = m.group(1).strip()
            raw_target = m.group(2).strip()
            # Keep the primary locator token before any parenthetical commentary
            target_tok = re.split(r"[\s（(]", raw_target, maxsplit=1)[0].strip()
            footnotes[fn_id] = target_tok
    return footnotes


def parse_facts_ledger(facts_markdown: str) -> dict[str, dict[str, Any]]:
    """Parse ``<!-- FACTS-LEDGER -->`` table rows from a ``role='facts'`` attachment (PK.9)."""
    entries: dict[str, dict[str, Any]] = {}
    in_ledger = False
    for line in facts_markdown.splitlines():
        stripped = line.strip()
        if "<!-- FACTS-LEDGER" in stripped:
            in_ledger = True
            continue
        if in_ledger and stripped.startswith("<!--") and "END" in stripped.upper():
            break
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 6:
            continue
        if cells[0].lower() in ("id", "---") or set(cells[0]) <= {"-", ":"}:
            continue
        row_id, metric, period, val_str, unit, ref = cells[:6]
        try:
            val = float(val_str.replace(",", "").replace("%", "").strip())
        except ValueError:
            continue
        entries[row_id] = {
            "id": row_id,
            "metric": metric,
            "period": normalize_period(period) or period,
            "value": val,
            "unit": unit,
            "ref": ref,
        }
    return entries


_UNIT_PAREN_STRIP_RE = re.compile(
    r"[（\(]\s*(?:单位\s*[：:]\s*)?(?:亿美元|亿元|万元|百万美元|百万|十亿|千亿|元|美元|港元|人民币|%\s*|pct|bps|bp|\$[BMKbmk]?|USD|CNY|RMB|HKD|in\s+\$?[BMKbmk]|in\s+(?:billions?|millions?))\s*[）\)]",
    re.IGNORECASE,
)


def _clean_metric_header(header_text: str) -> str:
    """Strip unit parentheticals like '（亿美元）' or '($B)' from a table metric header."""
    cleaned = _UNIT_PAREN_STRIP_RE.sub("", header_text).strip(" *")
    return cleaned or header_text.strip(" *")


def _resolve_footnote_target(
    fn_id: str | None, footnotes: dict[str, str]
) -> str | None:
    """Resolve footnote ID to its target prefix, supporting inline [^E0001] evidence refs."""
    if not fn_id:
        return None
    if fn_id in footnotes:
        return footnotes[fn_id]
    s = fn_id.strip()
    if re.match(r"^E-?\d+$", s, re.IGNORECASE):
        return f"evidence:{s}"
    if s.startswith(("evidence:", "run:", "filing:", "attachment:")):
        return s
    return None


def _parse_cn_prose_figure(raw_match: str) -> tuple[float, str] | None:
    """Convert Chinese prose quantifiers ('三成', '五分之一', '翻倍') into (value, canonical_unit)."""
    s = re.sub(r"\s+", "", raw_match.strip())
    if s in ("翻倍", "翻一番"):
        return 2.0, "x"
    if s == "翻两番":
        return 4.0, "x"
    if s in ("减半", "一半", "半数"):
        return 50.0, "pct"
    m_cheng = re.match(r"^([一二三四五六七八九十\d]+(?:\.\d+)?)成(半)?$", s)
    if m_cheng:
        num_part = m_cheng.group(1)
        base = (
            float(_CN_DIGIT_MAP[num_part])
            if num_part in _CN_DIGIT_MAP
            else float(num_part)
        )
        val = base * 10.0 + (5.0 if m_cheng.group(2) else 0.0)
        return val, "pct"
    m_zhe = re.match(r"^([一二三四五六七八九十\d]+)折$", s)
    if m_zhe:
        num_part = m_zhe.group(1)
        base = (
            float(_CN_DIGIT_MAP[num_part])
            if num_part in _CN_DIGIT_MAP
            else float(num_part)
        )
        return base * 10.0, "pct"
    m_frac = re.match(r"^([一二三四五六七八九十\d]+)分之([一二三四五六七八九十\d]+)$", s)
    if m_frac:
        den_s, num_s = m_frac.group(1), m_frac.group(2)
        den = _CN_DIGIT_MAP.get(den_s) or (float(den_s) if den_s.isdigit() else 0.0)
        num = _CN_DIGIT_MAP.get(num_s) or (float(num_s) if num_s.isdigit() else 0.0)
        if den > 0:
            return round((num / den) * 100.0, 4), "pct"
    return None


def _split_md_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        bool(re.match(r"^:?-{2,}:?$", c.strip())) for c in cells if c.strip() != ""
    )


def _mask_excluded_spans(line: str) -> str:
    """Replace inline code, URLs, footnote refs, dates, periods, and IDs with spaces of equal length."""
    chars = list(line)

    def _blank(pattern: re.Pattern[str]) -> None:
        current = "".join(chars)
        for m in pattern.finditer(current):
            for i in range(m.start(), m.end()):
                chars[i] = " "

    _blank(_INLINE_CODE_RE)
    _blank(_MARKDOWN_LINK_RE)
    _blank(_FOOTNOTE_REF_RE)
    _blank(_SEC_ACCESSION_RE)
    _blank(_HYPHEN_DOC_CODE_RE)
    _blank(_ISO_DATE_RE)
    _blank(_CN_DATE_RE)
    _blank(_YEAR_RANGE_RE)
    _blank(_SHORT_DATE_RE)
    _blank(_FILING_FORM_RE)
    _blank(_TICKER_CODE_RE)
    _blank(_ENTITY_ID_RE)
    _blank(_PERIOD_TOKEN_RE)
    return "".join(chars)


def extract_figures(markdown_text: str) -> list[ExtractedFigure]:
    """Extract all figures (including exempt ones marked ``exempt=True``) from Markdown text (PK.9)."""
    footnotes = parse_footnotes(markdown_text)
    lines = markdown_text.splitlines()

    figures: list[ExtractedFigure] = []
    fig_counter = 0

    in_code_block = False
    in_html_comment = False
    in_disclaimer = False
    current_section_period = ""
    last_caption_line = ""

    # Track Markdown table state
    table_headers: list[str] | None = None
    table_shared_footnote: str | None = None
    table_caption: str = ""

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line_no = i + 1
        stripped = raw_line.strip()

        # Fenced code block toggle
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            i += 1
            continue
        if in_code_block:
            i += 1
            continue

        # Multi-line HTML comment
        if "<!--" in stripped:
            if "-->" not in stripped:
                in_html_comment = True
            i += 1
            continue
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            i += 1
            continue

        # Footnote definition line
        if _FOOTNOTE_DEF_RE.match(raw_line):
            i += 1
            continue

        # Section heading check (detect disclaimer section or section-level period)
        if stripped.startswith("#"):
            heading_text = _HEADING_PREFIX_RE.sub("", stripped)
            if any(
                k in heading_text.lower()
                for k in ("免责声明", "disclaimer", "风险提示与免责")
            ):
                in_disclaimer = True
                i += 1
                continue
            else:
                in_disclaimer = False
            pm = _PERIOD_TOKEN_RE.search(heading_text)
            if pm:
                current_section_period = normalize_period(pm.group(1))
            last_caption_line = heading_text
            table_headers = None
            table_shared_footnote = None
            table_caption = ""
            # Strip heading numbering before scanning heading for any actual financial numbers
            scan_line = _HEADING_PREFIX_RE.sub(
                lambda m: " " * len(m.group(0)), raw_line
            )
        else:
            scan_line = _LIST_PREFIX_RE.sub(
                lambda m: " " * len(m.group(0)), raw_line
            )

        if in_disclaimer or not stripped:
            if not stripped:
                table_headers = None
                table_shared_footnote = None
                table_caption = ""
            i += 1
            continue

        # Check if this line is part of a Markdown table
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = _split_md_table_row(stripped)
            # Check if next line is separator row -> this line is table header
            if (
                table_headers is None
                and i + 1 < len(lines)
                and lines[i + 1].strip().startswith("|")
                and _is_separator_row(_split_md_table_row(lines[i + 1]))
            ):
                table_headers = cells
                table_caption = last_caption_line
                fn_m = _FOOTNOTE_REF_RE.search(stripped)
                if fn_m:
                    table_shared_footnote = fn_m.group(1)
                i += 2
                continue
            if _is_separator_row(cells):
                i += 1
                continue

            if table_headers is not None and cells:
                row_header = _FOOTNOTE_REF_RE.sub("", cells[0]).strip(" *")
                if row_header.lower() in ("来源", "数据来源", "source", "sources"):
                    fn_m = _FOOTNOTE_REF_RE.search(stripped)
                    if fn_m:
                        table_shared_footnote = fn_m.group(1)
                    i += 1
                    continue

                row_fn_m = _FOOTNOTE_REF_RE.search(cells[0])
                row_fn = row_fn_m.group(1) if row_fn_m else None
                table_header_0 = (
                    _FOOTNOTE_REF_RE.sub("", table_headers[0]).strip(" *")
                    if table_headers
                    else ""
                )

                # Support 1-column numeric tables or tables where col 0 is itself a data column
                start_col = 1
                if len(cells) == 1 or (
                    _NUMBER_RE.search(_mask_excluded_spans(cells[0]))
                    and not _PERIOD_TOKEN_RE.fullmatch(row_header)
                    and table_header_0
                    and table_header_0.lower() not in ("指标", "项目", "metric", "item", "期间", "period")
                ):
                    start_col = 0

                for col_idx in range(start_col, len(cells)):
                    cell_text = cells[col_idx]
                    col_header = (
                        _FOOTNOTE_REF_RE.sub("", table_headers[col_idx]).strip(" *")
                        if col_idx < len(table_headers)
                        else ""
                    )
                    col_fn_m = (
                        _FOOTNOTE_REF_RE.search(table_headers[col_idx])
                        if col_idx < len(table_headers)
                        else None
                    )
                    col_fn = col_fn_m.group(1) if col_fn_m else None

                    cell_fn_m = _FOOTNOTE_REF_RE.search(cell_text)
                    fn_id = (
                        (cell_fn_m.group(1) if cell_fn_m else None)
                        or row_fn
                        or col_fn
                        or table_shared_footnote
                    )
                    fn_target = _resolve_footnote_target(fn_id, footnotes)

                    eff_row_header = "" if col_idx == 0 else row_header
                    clean_row = _clean_metric_header(eff_row_header)
                    clean_col = _clean_metric_header(col_header)

                    # Determine period and metric from row_header / col_header
                    p_col = _PERIOD_TOKEN_RE.search(col_header)
                    p_row = _PERIOD_TOKEN_RE.search(eff_row_header)
                    if p_col:
                        period = normalize_period(p_col.group(1))
                        col_without_period = _PERIOD_TOKEN_RE.sub("", clean_col).strip(" ()（）-")
                        metric = (
                            f"{clean_row} {col_without_period}".strip()
                            if clean_row and col_without_period
                            else (clean_row or col_without_period or clean_col)
                        )
                    elif p_row:
                        period = normalize_period(p_row.group(1))
                        row_without_period = _PERIOD_TOKEN_RE.sub("", clean_row).strip(" ()（）-")
                        metric = (
                            f"{row_without_period} {clean_col}".strip()
                            if row_without_period
                            else clean_col
                        )
                    else:
                        period = current_section_period
                        if clean_row and clean_col:
                            metric = f"{clean_row} ({clean_col})".strip(" ()")
                        else:
                            metric = clean_row or clean_col

                    caption_unit_ctx = (
                        table_caption
                        if re.search(
                            r"(?:单位|unit|\bin\s+\$|\bin\s+(?:billions?|millions?))",
                            table_caption,
                            re.IGNORECASE,
                        )
                        else ""
                    )
                    header_ctx = f"{eff_row_header} {col_header} {table_header_0} {caption_unit_ctx}".strip()
                    masked_cell = _mask_excluded_spans(cell_text)
                    for nm in _NUMBER_RE.finditer(masked_cell):
                        num_str = nm.group("num").replace(",", "")
                        dec_str = nm.group("dec") or ""
                        if not dec_str and _STANDALONE_YEAR_RE.match(num_str):
                            continue
                        val = float(num_str + dec_str)
                        decimals = len(dec_str) - 1 if dec_str else 0
                        is_neg = bool(
                            nm.group("sign") in ("-", "−")
                            or (nm.group("open_paren") and nm.group("close_paren"))
                        )
                        if is_neg:
                            val = -val

                        currency, unit, scale = _determine_unit_and_scale(
                            nm.group("currency"),
                            nm.group("scale"),
                            nm.group("unit"),
                            context_text=f"{eff_row_header} {col_header}",
                            header_context=header_ctx,
                        )
                        label_hint = _infer_label_hint(
                            f"{eff_row_header} {col_header} {cell_text}"
                        )
                        is_exempt = bool(
                            _EXEMPT_MARKER_RE.search(cell_text)
                            or _EXEMPT_MARKER_RE.search(eff_row_header)
                            or _EXEMPT_MARKER_RE.search(col_header)
                        )
                        fig_counter += 1
                        figures.append(
                            ExtractedFigure(
                                id=f"F-{fig_counter:03d}",
                                raw=nm.group("full").strip(),
                                value=val,
                                decimals=decimals,
                                unit=unit,
                                scale=scale,
                                currency=currency,
                                metric=metric,
                                period=period,
                                label_hint=label_hint,
                                footnote_id=fn_id,
                                footnote_target=fn_target,
                                location={
                                    "line": line_no,
                                    "table": True,
                                    "row": eff_row_header,
                                    "col": col_header,
                                    "sentence": stripped,
                                },
                                exempt=is_exempt,
                                exempt_reason="illustrative" if is_exempt else None,
                            )
                        )
                i += 1
                continue
        else:
            table_headers = None
            table_shared_footnote = None
            table_caption = ""
            if len(stripped) <= 80:
                last_caption_line = stripped

        # Prose line scanning
        is_blockquote = stripped.startswith(">")
        masked_line = _mask_excluded_spans(scan_line)
        # Exclude structural prose counts ("3 个支柱", "第 2 季度") completely
        for sm in _STRUCTURAL_COUNT_RE.finditer(scan_line):
            masked_line = (
                masked_line[: sm.start()]
                + (" " * (sm.end() - sm.start()))
                + masked_line[sm.end() :]
            )

        line_periods = [
            (pm.start(), normalize_period(pm.group(1)))
            for pm in _PERIOD_TOKEN_RE.finditer(scan_line)
        ]
        line_footnotes = [
            (fm.start(), fm.group(1))
            for fm in _FOOTNOTE_REF_RE.finditer(raw_line)
        ]

        is_exempt = is_blockquote or bool(_EXEMPT_MARKER_RE.search(raw_line))
        exempt_reason = (
            "blockquote_quote"
            if is_blockquote
            else ("illustrative" if is_exempt else None)
        )

        for nm in _NUMBER_RE.finditer(masked_line):
            num_str = nm.group("num").replace(",", "")
            dec_str = nm.group("dec") or ""
            if not dec_str and _STANDALONE_YEAR_RE.match(num_str):
                continue

            # Skip bare small integers in prose that have no unit, scale, currency, decimal, or sign
            has_qualifier = any(
                [
                    nm.group("currency"),
                    nm.group("scale"),
                    nm.group("unit"),
                    nm.group("dec"),
                    nm.group("sign"),
                ]
            )
            if not has_qualifier and int(num_str) < 10:
                continue

            val = float(num_str + dec_str)
            decimals = len(dec_str) - 1 if dec_str else 0
            is_neg = bool(
                nm.group("sign") in ("-", "−")
                or (nm.group("open_paren") and nm.group("close_paren"))
            )
            if is_neg:
                val = -val

            # Nearest period in same line (prefer preceding, else nearest following, else section)
            period = current_section_period
            if line_periods:
                preceding = [p for pos, p in line_periods if pos <= nm.start()]
                if preceding:
                    period = preceding[-1]
                else:
                    period = line_periods[0][1]

            # Nearest footnote at or after this number on the same line
            fn_id: str | None = None
            raw_tok_end = nm.start() + len(nm.group("full").rstrip())
            after_fns = [fid for pos, fid in line_footnotes if pos >= raw_tok_end - 1]
            if after_fns:
                fn_id = after_fns[0]
            elif line_footnotes:
                fn_id = line_footnotes[-1][1]
            fn_target = _resolve_footnote_target(fn_id, footnotes)

            metric = _extract_metric_from_context(scan_line, nm.start(), nm.end())
            currency, unit, scale = _determine_unit_and_scale(
                nm.group("currency"),
                nm.group("scale"),
                nm.group("unit"),
                context_text=scan_line[max(0, nm.start() - 25) : nm.end() + 15],
                header_context=last_caption_line if "单位" in last_caption_line else "",
            )
            label_hint = _infer_label_hint(scan_line)

            fig_counter += 1
            figures.append(
                ExtractedFigure(
                    id=f"F-{fig_counter:03d}",
                    raw=nm.group("full").strip(),
                    value=val,
                    decimals=decimals,
                    unit=unit,
                    scale=scale,
                    currency=currency,
                    metric=metric,
                    period=period,
                    label_hint=label_hint,
                    footnote_id=fn_id,
                    footnote_target=fn_target,
                    location={
                        "line": line_no,
                        "table": False,
                        "row": None,
                        "col": None,
                        "sentence": stripped,
                    },
                    exempt=is_exempt,
                    exempt_reason=exempt_reason,
                )
            )

        # Also extract Chinese prose quantifiers ("约三成", "五分之一", "翻倍") on non-exempt lines
        if not is_exempt:
            for cm in _CN_PROSE_FIG_RE.finditer(masked_line):
                raw_cn = cm.group(0).strip()
                # Skip "翻倍至 <num>" when the explicit target figure is already given right after
                after_slice = masked_line[cm.end() : cm.end() + 12]
                if raw_cn in ("翻倍", "翻一番", "翻两番") and re.match(
                    r"^\s*(?:至|到|达|为)\s*[\$¥€£]?\d", after_slice
                ):
                    continue
                parsed_cn = _parse_cn_prose_figure(raw_cn)
                if parsed_cn is None:
                    continue
                cn_val, cn_unit = parsed_cn
                period = current_section_period
                if line_periods:
                    preceding = [p for pos, p in line_periods if pos <= cm.start()]
                    period = preceding[-1] if preceding else line_periods[0][1]
                fn_id = None
                after_fns = [fid for pos, fid in line_footnotes if pos >= cm.end() - 1]
                if after_fns:
                    fn_id = after_fns[0]
                elif line_footnotes:
                    fn_id = line_footnotes[-1][1]
                fn_target = _resolve_footnote_target(fn_id, footnotes)
                metric = _extract_metric_from_context(scan_line, cm.start(), cm.end())
                fig_counter += 1
                figures.append(
                    ExtractedFigure(
                        id=f"F-{fig_counter:03d}",
                        raw=raw_cn,
                        value=cn_val,
                        decimals=0,
                        unit=cn_unit,
                        scale=1.0,
                        currency=None,
                        metric=metric,
                        period=period,
                        label_hint=_infer_label_hint(scan_line),
                        footnote_id=fn_id,
                        footnote_target=fn_target,
                        location={
                            "line": line_no,
                            "table": False,
                            "row": None,
                            "col": None,
                            "sentence": stripped,
                        },
                        exempt=False,
                        exempt_reason=None,
                    )
                )

        i += 1

    return figures


async def maybe_label_prose_figures_with_llm(
    markdown_text: str,
    figures: list[ExtractedFigure],
    *,
    user_id: str = "local-dev-user",
    llm_service: Any = None,
) -> list[ExtractedFigure]:
    """Optional LLM slow-path (`audit.label_figures`) to refine labels/units and discover prose figures."""
    try:
        from src.dalton.llm.assist import assist_json
    except ImportError:
        return figures

    payload = {
        "markdown_excerpt": markdown_text[:6000],
        "extracted_figures": [
            {
                "id": f.id,
                "raw": f.raw,
                "value": f.value,
                "unit": f.unit,
                "scale": f.scale,
                "metric": f.metric,
                "period": f.period,
                "exempt": f.exempt,
                "line": f.location.get("line"),
                "sentence": f.location.get("sentence"),
            }
            for f in figures
        ],
    }
    res = await assist_json(
        task="audit.label_figures",
        instructions=(
            "You label extracted financial report figures with accurate metric, period, and unit, "
            "and identify any unextracted Chinese/prose quantitative claims."
        ),
        payload=payload,
        schema=FigureLabels.model_json_schema(),
        user_id=user_id,
        llm_service=llm_service,
    )
    if not res.ok or not res.data:
        return figures

    try:
        parsed = FigureLabels.model_validate(res.data)
    except Exception:
        return figures

    by_id = {f.id: f for f in figures}
    for item in parsed.items:
        target = by_id.get(item.id)
        if target is None:
            continue
        if item.metric:
            target.metric = item.metric
        if item.period:
            target.period = normalize_period(item.period) or item.period
        if item.unit:
            target.unit = item.unit

    fig_counter = len(figures)
    existing_raws_by_line = {
        (f.location.get("line"), f.raw) for f in figures
    }
    for extra in parsed.extra_figures:
        if (extra.line, extra.raw) in existing_raws_by_line:
            continue
        fig_counter += 1
        figures.append(
            ExtractedFigure(
                id=f"F-{fig_counter:03d}",
                raw=extra.raw,
                value=extra.value,
                decimals=0,
                unit=extra.unit or "pct",
                scale=1.0,
                currency=None,
                metric=extra.metric,
                period=normalize_period(extra.period) if extra.period else "",
                label_hint="reported",
                footnote_id=None,
                footnote_target=None,
                location={
                    "line": extra.line,
                    "table": False,
                    "row": None,
                    "col": None,
                    "sentence": extra.raw,
                },
                exempt=False,
                exempt_reason=None,
            )
        )
    return figures


