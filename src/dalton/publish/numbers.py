"""Deterministic extraction of financial figures from Markdown prose and tables (PK.9)."""

from __future__ import annotations

import asyncio
import time
import dataclasses
import re
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, model_validator

from src.dalton.llm.model_prefs import (
    resolve_dalton_audit_fallback_models,
    resolve_dalton_audit_model,
    resolve_dalton_audit_timeout_s,
)
from src.dalton.settings import get_dalton_config

try:
    from src.dalton.llm.assist import assist_json
except ImportError:  # pragma: no cover
    assist_json = None  # type: ignore[assignment]

IncidentalCategory = Literal[
    "",
    "点赞数、转发数等社交互动计数",
    "浏览量",
    "HTTP 状态码",
    "文件、文档、邮件和 URL 里的编号",
    "软件和模型的版本号、硬件型号",
    "列表序号、页码",
    "用户名里的数字",
    "机构、产品、节目名称里的数字",
    "期间、日期和时间窗口表达里的数字",
]
INCIDENTAL_CATEGORIES: tuple[str, ...] = tuple(c for c in get_args(IncidentalCategory) if c)
_DECIMAL_UNCERTAIN_CATEGORIES = frozenset({"软件和模型的版本号、硬件型号", "机构、产品、节目名称里的数字"})

# Period tokens: FY2026, FY26, 3Q26, 26Q4, Q4 26, Q4'26, Q3 2026, 2026Q3, 1H26, 2H26, 2026H1, LTM-2026-09, 2026E, NTM
_PERIOD_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"FY(?:(?:19|20)\d{2}|[12]\d)E?|"
    r"[1-4]Q(?:(?:19|20)\d{2}|[12]\d)E?|"
    r"(?:20\d{2}|2\d)\s*Q[1-4]E?|"
    r"Q[1-4](?:\s*['’]\s*(?:(?:19|20)\d{2}|[12]\d)|\s+(?:20\d{2}|2\d)(?!\s*[-\d.%％bp倍万亿美港元QqKk]))E?|"
    r"[12]H(?:(?:19|20)\d{2}|[12]\d)E?|"
    r"(?:20\d{2}|2\d)\s*H[12]E?|"
    r"202\d\s*(?:上半年|下半年|全年|财年)|"
    r"LTM(?:-\d{4}-\d{2})?|"
    r"NTM|"
    r"20\d{2}E"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LTM_EXPR_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:LTM|TTM)(?![A-Za-z0-9])|(?:过去|近)\s*12\s*个月)",
    re.IGNORECASE,
)

# Standalone date / period / filing form / identifier patterns to exclude
_SEC_ACCESSION_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_HYPHEN_DOC_CODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}\b|\b[A-Za-z]{2,}-\d{2,}-\d+\b"
)
_ISO_DATE_RE = re.compile(r"\b20\d{2}[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?\b")
_CN_DATE_RE = re.compile(
    r"(?:20\d{2}\s*年(?:\s*(?:1[0-2]|0?[1-9])\s*月(?:\s*(?:[0-3]?\d)\s*[日号])?)?|"
    r"(?<!\d)(?:1[0-2]|0?[1-9])\s*月(?:\s*(?:[0-3]?\d)\s*[日号])?)"
)
_SHORT_DATE_RE = re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?!\d)")
_SLASH_DATE_RE = re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?:/\d{2,4})?(?!\d)")
_CLOCK_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b")
_LONG_ID_INT_RE = re.compile(r"\b\d{12,}\b")
_BATCH_CODE_RE = re.compile(r"\b\d{8}-\d{4}\b")
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
_CITATION_DEF_RE = re.compile(r"^\s*(?:[-*]\s*)?\[((?:回报)?引用\d+)\]:\s*(.+?)\s*$")
_CITATION_REF_RE = re.compile(r"\[((?:回报)?引用\d+)\]")
_ANY_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]|\[((?:回报)?引用\d+)\]")
_KNOWN_SOURCE_PREFIXES = ("f:", "evidence:", "model:", "prov:")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_HEADING_PREFIX_RE = re.compile(
    r"^\s*#{1,6}\s+(?:(?![1-4]Q\d|[12]H\d|20\d{2}\s*[QH][1-4])\d+(?:\.\d+)*(?:[.)]\s*|\s+))?",
    re.IGNORECASE,
)
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|（\d+）|\(\d+\))\s+")

# Structural prose counts like "3 个支柱", "第 2 季度", "第 1 节", "2 条分歧"
_STRUCTURAL_COUNT_RE = re.compile(
    r"(?:第\s*\d+\s*(?:季度|节|章|步|阶段|部分|页|行|列|条|项|版|次)|"
    r"\b\d+\s*(?:个支柱|条支柱|条分歧|个分歧|个问题|条问题|个驱动|条经验|家公司|个工作区|页|行))"
)

# Single authoritative exemption marker table (P0J J2 §6): 5 words × 3 bracket styles
EXEMPT_MARKER_WORDS: tuple[str, ...] = ("示意", "假设", "估计", "举例", "阈值")
EXEMPT_MARKER_BRACKETS: tuple[tuple[str, str], ...] = (("〔", "〕"), ("[", "]"), ("（", "）"))
EXEMPT_MARKERS: tuple[str, ...] = tuple(
    f"{ob}{w}{cb}" for w in EXEMPT_MARKER_WORDS for ob, cb in EXEMPT_MARKER_BRACKETS
)

# Approximate qualifiers immediately preceding colloquial numbers, ordered longest-first (P0J J5h)
COLLOQUIAL_APPROX_QUALIFIERS: tuple[str, ...] = (
    # Longer negations first, so a suffix match never trims them down to the positive "超过".
    "不能超过",
    "不会超过",
    "不得超过",
    "没有超过",
    "并未超过",
    "不超过",
    "未超过",
    "没超过",
    "将近",
    "接近",
    "大约",
    "约为",
    "超过",
    "不超",
    "未超",
    "不逾",
    "未逾",
    "不到",
    "不足",
    "近乎",
    "几乎",
    "多达",
    "近",
    "约",
    "超",
    "逾",
)

SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING: dict[str, frozenset[str]] = {
    "近": frozenset(("最", "附", "新", "靠", "邻", "贴", "逼", "亲", "就")),
    "约": frozenset(
        ("合", "签", "制", "预", "公", "条", "节", "婚", "违", "践", "履", "契", "相", "邀", "隐")
    ),
    "超": frozenset(("高", "赶")),
    "逾": frozenset(),
}


def has_exempt_marker(text: str) -> bool:
    return any(m in text for m in EXEMPT_MARKERS)


def _expand_colloquial_raw_with_qualifier(
    line_text: str,
    raw_sub: str,
    start: int = 0,
) -> tuple[str, int]:
    """Expand a colloquial figure `raw_sub` leftward when immediately preceded by an approximate qualifier on `line_text`.

    Returns `(expanded_raw, expanded_start_index)` for the occurrence at or after `start`.
    """
    if not line_text or not raw_sub:
        return raw_sub, -1
    pos = line_text.find(raw_sub, max(0, start))
    if pos < 0:
        return raw_sub, -1
    core = raw_sub
    core_pos = pos
    for q in COLLOQUIAL_APPROX_QUALIFIERS:
        if raw_sub.startswith(q) and len(raw_sub) > len(q):
            core = raw_sub[len(q) :]
            core_pos = pos + len(q)
            break
    prefix = line_text[:core_pos]
    for q in COLLOQUIAL_APPROX_QUALIFIERS:
        if not prefix.endswith(q):
            continue
        if len(q) == 1:
            excluded_prev = SINGLE_CHAR_QUALIFIER_EXCLUDED_PRECEDING.get(q, frozenset())
            if len(prefix) >= 2 and prefix[-2] in excluded_prev:
                continue
        return q + core, core_pos - len(q)
    return raw_sub, pos


# Core financial number regex:
# Captures optional sign/parens, optional tight currency prefix (at most one space),
# number with commas/decimals, optional scale (at most one space), and optional tight unit/currency suffix (at most one space).
_NUMBER_RE = re.compile(
    r"(?P<full>"
    r"(?P<open_paren>\()?"
    r"(?P<sign>[+\-−])?\s*"
    r"(?:(?P<currency>(?<![A-Za-z])(?:US\$|HK\$|RMB|CNY|USD|HKD)|\$|¥|￥) ?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"(?: ?(?P<scale>千亿|百亿|百万|万亿|亿|万|billion|million|bn|mn|(?:k|m|b)(?![a-zA-Z])))?"
    r"(?: ?(?P<unit>bps|bp|pp|pts|%|倍|x(?![a-zA-Z])|(?:USD|HKD|CNY|RMB)(?![a-zA-Z])|美元|港元|港币|人民币|元(?![素年旦气件])))?"
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
    "港币": "HKD",
    "¥": "CNY",
    "￥": "CNY",
    "rmb": "CNY",
    "cny": "CNY",
    "人民币": "CNY",
    "元": "CNY",
}


class _PerShareUnit(str):
    """Canonical ``"per_share"`` unit string that preserves its explicit currency (``"USD"``, ``"HKD"``, ``"CNY"``, or ``None``)."""

    currency: str | None = None

    def __new__(cls, value: str = "per_share", currency: str | None = None) -> "_PerShareUnit":
        obj = super().__new__(cls, value)
        obj.currency = currency
        return obj


class FigureLabelItem(BaseModel):
    """LLM-annotated metric, period, unit, and kind for one extracted figure ID."""

    id: str = Field(description="Extracted figure ID, e.g. F-001")
    metric: str = Field(default="", description="Financial metric name")
    period: str = Field(default="", description="Normalized period, e.g. 4Q23 or FY2023")
    unit: str = Field(
        default="",
        description="Optional canonical unit, e.g. USD_bn, USD_mn, pct, x, per_share, count_mn",
    )
    kind: str = Field(
        default="",
        description="Semantic figure kind: reported, estimate, consensus, guidance, derived, or incidental",
    )
    incidental_category: IncidentalCategory = Field(
        default="",
        description="One of the 9 non-verification categories when kind is incidental",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_strings(cls, data: Any) -> Any:
        if isinstance(data, dict):
            out = {
                k: ("" if v is None and k in ("metric", "period", "unit", "kind", "incidental_category") else v)
                for k, v in data.items()
            }
            cat = str(out.get("incidental_category") or "").strip()
            out["incidental_category"] = cat if cat in INCIDENTAL_CATEGORIES else ""
            return out
        return data


class ExtraFigureItem(BaseModel):
    """Additional prose figure identified by LLM (e.g. Chinese expressions like '两成', '翻番', '腰斩')."""

    raw: str = Field(description="Exact substring in the draft sentence, e.g. '两成', '翻番', '腰斩', '过半'")
    line: int = Field(default=1, description="1-based line number in the draft")
    value: float = Field(description="Numeric value (e.g. 20.0 for 两成, 2.0 for 翻番, -50.0 or 50.0 for 腰斩)")
    unit: str = Field(default="pct", description="Canonical unit, e.g. pct or x")
    metric: str = Field(default="", description="Financial metric name")
    period: str = Field(default="", description="Normalized period if any")
    kind: str = Field(
        default="reported",
        description="Semantic figure kind: reported, estimate, consensus, guidance, or derived",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_strings(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: ("" if v is None and k in ("unit", "metric", "period", "kind") else (1 if v is None and k == "line" else v))
                for k, v in data.items()
            }
        return data


_LABEL_DEADLINE_GRACE_S = 5.0


@dataclasses.dataclass(frozen=True)
class _LabelBatchUnfinished:
    """Stand-in result for a label batch cut off by the audit deadline."""

    error: str
    ok: bool = False
    data: Any = None


class FigureLabels(BaseModel):
    """Structured response for LLM-assisted figure labeling (PK.9 / P0H Step B / P0I §1.4)."""

    items: list[FigureLabelItem] = Field(default_factory=list)
    extra_figures: list[ExtraFigureItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_labels_alias(cls, data: Any) -> Any:
        if isinstance(data, dict):
            copied = dict(data)
            if "items" not in copied and "labels" in copied:
                copied["items"] = copied.pop("labels")
            if copied.get("extra_figures") is None:
                copied["extra_figures"] = []
            return copied
        return data


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
    footnote_ids: list[str] = dataclasses.field(default_factory=list)
    footnote_targets: list[str] = dataclasses.field(default_factory=list)
    missing_citations: list[str] = dataclasses.field(default_factory=list)
    duplicate_citations: list[str] = dataclasses.field(default_factory=list)
    location: dict[str, Any] = dataclasses.field(default_factory=dict)
    exempt: bool = False
    exempt_reason: str | None = None
    incidental_category: str = ""

    @property
    def scaled_value(self) -> float:
        return self.value * self.scale


def match_ltm_periods(text: str, *, fullmatch: bool = False) -> list[int]:
    """Match LTM / TTM / '过去 12 个月' / '近 12 个月' period tokens (with word boundaries on LTM/TTM)."""
    if not text:
        return []
    if fullmatch:
        return [0] if _LTM_EXPR_RE.fullmatch(text.strip()) else []
    return [m.start() for m in _LTM_EXPR_RE.finditer(text)]


def normalize_period(raw_period: str | None) -> str:
    """Canonicalize period strings (e.g. ``FY26`` -> ``FY2026``, ``Q3 2026`` -> ``3Q26``, ``过去 12 个月`` -> ``LTM``)."""
    if not raw_period:
        return ""
    s = raw_period.strip().upper().rstrip("E")
    if match_ltm_periods(s, fullmatch=True):
        return "LTM"
    m_fy_short = re.match(r"^FY(\d{2})$", s)
    if m_fy_short:
        return f"FY20{m_fy_short.group(1)}"
    m_fy_long = re.match(r"^FY(\d{4})$", s)
    if m_fy_long:
        return f"FY{m_fy_long.group(1)}"
    m_q_first = re.match(r"^([1-4])Q(?:20)?(\d{2})$", s)
    if m_q_first:
        return f"{m_q_first.group(1)}Q{m_q_first.group(2)}"
    m_q_second = re.match(r"^Q([1-4])\s*['’]?\s*(?:20)?(\d{2})$", s)
    if m_q_second:
        return f"{m_q_second.group(1)}Q{m_q_second.group(2)}"
    m_year_q = re.match(r"^(?:20)?(\d{2})\s*Q([1-4])$", s)
    if m_year_q:
        return f"{m_year_q.group(2)}Q{m_year_q.group(1)}"
    m_h = re.match(r"^([12])H(?:20)?(\d{2})$", s)
    if m_h:
        return f"{m_h.group(1)}H{m_h.group(2)}"
    m_year_h = re.match(r"^(?:20)?(\d{2})\s*H([12])$", s)
    if m_year_h:
        return f"{m_year_h.group(2)}H{m_year_h.group(1)}"
    m_cn = re.match(r"^20(\d{2})\s*(上半年|下半年|全年|财年)$", s)
    if m_cn:
        yy, kind_cn = m_cn.group(1), m_cn.group(2)
        if kind_cn == "上半年":
            return f"1H{yy}"
        if kind_cn == "下半年":
            return f"2H{yy}"
        return f"FY20{yy}"
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


def _infer_currency_from_table_header(header_text: str) -> str | None:
    """Extract currency from a Markdown table row/column/caption unit header when unambiguous."""
    s = (header_text or "").strip()
    if not s:
        return None
    low = s.lower()
    found: set[str] = set()
    if re.search(
        r"(?:[（(][^）)]*(?:港元|港币|\bhkd\b|hk\$)[^）)]*[）)]|(?:亿|百万|万)(?:港元|港币)|(?:港元|港币)/股|\bhkd\s*(?:bn|mn|b|m|k|per\s+share|/sh)\b|hk\$\s*[bmk]\b|单位\s*[：:]\s*[^\n,，;；]*(?:港元|港币|\bhkd\b|hk\$))",
        low,
    ):
        found.add("HKD")
    low_no_hk = re.sub(r"hk\$", " ", low)
    if re.search(
        r"(?:[（(][^）)]*(?:美元|\busd\b|us\$|\$)[^）)]*[）)]|(?:万亿|千亿|百亿|亿|百万|万)美元|美元/股|\busd\s*(?:bn|mn|b|m|k|per\s+share|/sh)\b|(?:us\$|\$)\s*[bmk]\b|单位\s*[：:]\s*[^\n,，;；]*(?:美元|\busd\b|us\$|\$))",
        low_no_hk,
    ):
        found.add("USD")
    if re.search(
        r"(?:[（(][^）)]*(?:人民币|亿元|百万元|万元|元/股|\bcny\b|\brmb\b|[¥￥])[^）)]*[）)]|(?:万亿|千亿|百亿|亿|百万|万)(?:人民币|元)|(?:人民币|元)/股|\b(?:cny|rmb)\s*(?:bn|mn|b|m|k|per\s+share|/sh)\b|单位\s*[：:]\s*[^\n,，;；]*(?:人民币|亿元|百万元|万元|\bcny\b|\brmb\b|[¥￥]))",
        low,
    ):
        found.add("CNY")
    if len(found) == 1:
        return next(iter(found))
    return None


def _determine_unit_and_scale(
    currency_tok: str | None,
    scale_tok: str | None,
    unit_tok: str | None,
    context_text: str = "",
    header_context: str = "",
) -> tuple[str | None, str, float]:
    """Return ``(currency, canonical_unit, scale_multiplier)``.

    Currency is bound ONLY when a currency symbol/code/word is tightly adjacent to the number itself
    (captured in ``currency_tok`` or ``unit_tok``), or specified by a table unit header in ``header_context``
    for non-per-share table cells. Never infer currency from a surrounding prose character window.
    """
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
    token_currency = CURRENCY_MAP.get(cur_raw) or CURRENCY_MAP.get(unit_raw)
    header_currency = _infer_currency_from_table_header(header_context) if header_context else None
    currency = token_currency or header_currency

    scale = SCALE_MULTIPLIERS.get(scale_raw, 1.0)
    if scale == 1.0 and header_context:
        scale = _infer_scale_from_header(header_context)

    if scale == 1.0 and combined_ctx and re.search(
        r"(?:[美港]元/股|港币/股|人民币/股|元/股|每股|\beps\b|per\s+share|/share\b|/sh\b)",
        combined_ctx,
        re.IGNORECASE,
    ):
        return token_currency, _PerShareUnit("per_share", currency=token_currency), 1.0

    if currency:
        if scale == 1e9:
            return currency, f"{currency}_bn", scale
        if scale == 1e6:
            return currency, f"{currency}_mn", scale
        if scale == 1e8:
            return currency, f"{currency}_100mn", scale
        return currency, currency, scale

    if scale == 1e6:
        return None, "count_mn", scale
    if scale > 1.0:
        return None, "count", scale
    return None, "other", 1.0


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


_NUMBER_WITH_SHARE_SUFFIX_RE = re.compile(
    _NUMBER_RE.pattern + r"(?:\s*/\s*(?:股|share|sh)\b)?",
    re.IGNORECASE,
)


def _strip_numbers_from_metric_clause(text: str) -> str:
    """Remove numbers and attached scale/unit suffixes from a metric label."""
    s = _NUMBER_WITH_SHARE_SUFFIX_RE.sub(" ", text)
    s = re.sub(
        r"(?:(?<![A-Za-z\u4e00-\u9fff])(?:万亿|千亿|百亿|百万|亿|万|美元|港元|人民币|%|％|bp|bps|pp|pts|倍)(?![A-Za-z\u4e00-\u9fff]))",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    return " ".join(s.split()).strip(" +-−~～/")


def _extract_metric_from_context(
    sentence: str, match_start: int, match_end: int
) -> str:
    """Heuristically extract the metric phrase preceding or surrounding a number in prose."""
    before = sentence[:match_start]
    # Remove period tokens, dates, and footnote markers from 'before' before splitting on '-'
    cleaned = _PERIOD_TOKEN_RE.sub(" ", before)
    cleaned = _ISO_DATE_RE.sub(" ", cleaned)
    cleaned = _CN_DATE_RE.sub(" ", cleaned)
    cleaned = _ANY_FOOTNOTE_REF_RE.sub(" ", cleaned)
    # Split on clause punctuation (including 、, ；, 。) without splitting thousands commas inside numbers (e.g. 1,234)
    clauses = re.split(
        r"[，；;：:。！？!?\(\)（）—]|(?<!\d),(?!\d)|(?<![A-Za-z0-9])-(?![A-Za-z0-9])",
        cleaned,
    )
    non_empty_cands: list[str] = []
    for clause in reversed(clauses):
        if "、" in clause:
            sub_parts = [
                _strip_numbers_from_metric_clause(p)
                for p in clause.split("、")
                if p.strip()
            ]
            sub_parts = [
                re.sub(
                    r"^(?:其中|以及|而|且|但|约|达到|为|至|录得|实现|高于|低于|维持|预计|我们的|公司)\s*",
                    "",
                    p,
                ).strip()
                for p in sub_parts
            ]
            sub_parts = [
                re.sub(r"(?:截至|约为|达到|为|至|约)\s*$", "", p).strip()
                for p in sub_parts
            ]
            sub_parts = [p for p in sub_parts if len(p) >= 2]
            if len(sub_parts) >= 2:
                last_sub = sub_parts[-1]
                first_no_num = sub_parts[0]
                if last_sub.lower() in _GENERIC_MODIFIER_WORDS and first_no_num:
                    non_empty_cands.append(f"{first_no_num} {last_sub}"[-36:])
                    continue
                m_seg = re.match(r"^([A-Za-z][A-Za-z0-9 &/\-]*)", first_no_num)
                if (
                    m_seg
                    and not re.match(r"^[A-Za-z]", last_sub)
                    and m_seg.group(1).strip().lower() not in last_sub.lower()
                ):
                    non_empty_cands.append(f"{m_seg.group(1).strip()} {last_sub}"[-36:])
                    continue
            for sp in reversed(sub_parts):
                non_empty_cands.append(sp)
            continue

        cand = _strip_numbers_from_metric_clause(clause)
        cand = re.sub(
            r"^(?:其中|以及|而|且|但|约|达到|为|至|录得|实现|高于|低于|维持|预计|我们的|公司)\s*",
            "",
            cand,
        ).strip()
        cand = re.sub(r"(?:截至|约为|达到|为|至|约)\s*$", "", cand).strip()
        if len(cand) >= 2:
            non_empty_cands.append(cand)

    if non_empty_cands:
        nearest = non_empty_cands[0]
        # If the nearest clause is just a generic growth/change modifier (e.g. "同比"),
        # combine it with the preceding subject clause (e.g. "广告收入 512 亿美元") so metric matches "广告收入增速".
        if nearest.lower() in _GENERIC_MODIFIER_WORDS and len(non_empty_cands) >= 2:
            prev_clause = _strip_numbers_from_metric_clause(non_empty_cands[1])
            if prev_clause:
                return f"{prev_clause} {nearest}"[-36:]
        return nearest[-32:]

    after = sentence[match_end : match_end + 24]
    after_clauses = re.split(r"[、，；;：:。！？!?\(\)（）]|(?<!\d),(?!\d)", after)
    if after_clauses and after_clauses[0].strip():
        cleaned_after = _strip_numbers_from_metric_clause(after_clauses[0])
        if cleaned_after:
            return cleaned_after[:24]
    return _strip_numbers_from_metric_clause(sentence)[:32]


_DUPLICATE_CITATION_TARGET = "__DUPLICATE_CITATION__"
_CITATION_SECTION_HEADING_RE = re.compile(r"^##\s+引用\s*$")
_CITATION_ANY_HEADING_RE = re.compile(r"^##\s+引用")
_TOP_HEADING_RE = re.compile(r"^#{1,2}\s+")


def _parse_footnote_namespaces_with_status(
    markdown_text: str,
) -> tuple[dict[str, str], dict[str, str], bool]:
    """Parse traditional footnotes ``[^id]: target`` and ``## 引用`` entries ``[引用N]: target`` into separate namespaces.

    Also returns ``has_parseable_citation_table`` indicating whether a valid ``## 引用`` mapping section exists.
    """
    traditional_footnotes: dict[str, str] = {}
    citation_table: dict[str, str] = {}
    in_code_block = False
    in_html_comment = False
    in_citation_section = False

    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        if "<!--" in stripped:
            if "-->" not in stripped:
                in_html_comment = True
            continue
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue

        if stripped.startswith("#"):
            if _CITATION_SECTION_HEADING_RE.match(stripped):
                in_citation_section = True
            elif _TOP_HEADING_RE.match(stripped):
                in_citation_section = False
            continue

        m = _FOOTNOTE_DEF_RE.match(raw_line)
        is_citation = False
        if not m and in_citation_section:
            m = _CITATION_DEF_RE.match(raw_line)
            is_citation = m is not None
        if not m:
            continue

        fn_id = (
            re.sub(r"\s+", "", m.group(1).strip())
            if is_citation
            else m.group(1).strip()
        )
        raw_target = m.group(2).strip()
        # Keep the primary locator token before any parenthetical commentary
        target_tok = re.split(r"[\s（(]", raw_target, maxsplit=1)[0].strip().strip("`")
        ns = citation_table if is_citation else traditional_footnotes
        if fn_id in ns:
            existing = ns[fn_id]
            if existing == _DUPLICATE_CITATION_TARGET or existing != target_tok:
                ns[fn_id] = _DUPLICATE_CITATION_TARGET
            continue
        ns[fn_id] = target_tok
    has_parseable_citation_table = bool(citation_table)
    return traditional_footnotes, citation_table, has_parseable_citation_table


def _parse_footnote_namespaces(
    markdown_text: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Parse traditional footnotes ``[^id]: target`` and ``## 引用`` entries ``[引用N]: target`` into separate namespaces."""
    traditional_footnotes, citation_table, _ = _parse_footnote_namespaces_with_status(
        markdown_text
    )
    return traditional_footnotes, citation_table


def count_unparsed_citations_when_table_missing(markdown_text: str) -> int:
    """Return the number of ``[引用N]`` citations in the draft body when the ``## 引用`` table is missing."""
    if not markdown_text:
        return 0
    _, _, has_parseable_citation_table = _parse_footnote_namespaces_with_status(
        markdown_text
    )
    if has_parseable_citation_table:
        return 0

    in_code_block = False
    in_html_comment = False
    in_citation_like_section = False
    count = 0
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if "<!--" in stripped:
            if "-->" not in stripped:
                in_html_comment = True
            continue
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        if stripped.startswith("#"):
            if _CITATION_ANY_HEADING_RE.match(stripped):
                in_citation_like_section = True
            elif _TOP_HEADING_RE.match(stripped):
                in_citation_like_section = False
            continue
        if in_citation_like_section:
            continue
        if _FOOTNOTE_DEF_RE.match(raw_line) or _CITATION_DEF_RE.match(raw_line):
            continue
        masked = _INLINE_CODE_RE.sub(" ", raw_line)
        count += len(_CITATION_REF_RE.findall(masked))
    return count


def parse_footnotes(markdown_text: str) -> dict[str, str]:
    """Parse Markdown footnote definitions ``[^id]: target`` and ``[引用N]: target`` (under ``## 引用``) into ``{id: target_prefix}``."""
    traditional_footnotes, citation_table = _parse_footnote_namespaces(markdown_text)
    merged = {
        (f"^{k}" if _is_citation_id(k) else k): v
        for k, v in traditional_footnotes.items()
    }
    merged.update(citation_table)
    return merged


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
    r"[（\(]\s*(?:单位\s*[：:]\s*)?(?:亿美元|亿元|万元|百万美元|百万|十亿|千亿|元|美元|港元|人民币|"
    r"%\s*|pct|bps|bp|\$[BMKbmk]?|USD|CNY|RMB|HKD|in\s+\$?[BMKbmk]|in\s+(?:billions?|millions?))\s*[）\)]",
    re.IGNORECASE,
)


def _clean_metric_header(header_text: str) -> str:
    """Strip unit parentheticals like '（亿美元）' or '($B)' from a table metric header."""
    cleaned = _UNIT_PAREN_STRIP_RE.sub("", header_text).strip(" *")
    return cleaned or header_text.strip(" *")


def _is_citation_id(fn_id: str | None) -> bool:
    return bool(fn_id and re.match(r"^(?:回报)?引用\d+$", fn_id.strip()))


def _format_extracted_ref_id(m: re.Match[str]) -> str:
    if m.group(2) is not None:
        return m.group(2).strip()
    raw_id = (m.group(1) or "").strip()
    return f"^{raw_id}" if _is_citation_id(raw_id) else raw_id


def _resolve_footnote_target(
    fn_id: str | None,
    traditional_footnotes: dict[str, str],
    citation_table: dict[str, str] | None = None,
) -> str | None:
    """Resolve footnote ID to its target prefix, keeping ``[引用N]`` and ``[^id]`` in separate namespaces."""
    if not fn_id:
        return None
    s = fn_id.strip()
    cits = (
        citation_table
        if citation_table is not None
        else {k: v for k, v in traditional_footnotes.items() if _is_citation_id(k)}
    )
    if _is_citation_id(s):
        norm_s = re.sub(r"\s+", "", s)
        target = cits.get(norm_s) or cits.get(s)
        if target and target != _DUPLICATE_CITATION_TARGET and target.startswith(_KNOWN_SOURCE_PREFIXES):
            return target
        return None
    trad_key = s[1:] if s.startswith("^") else s
    if citation_table is None and _is_citation_id(trad_key):
        target = traditional_footnotes.get(f"^{trad_key}")
        if target is not None:
            return None if target == _DUPLICATE_CITATION_TARGET else target
    elif trad_key in traditional_footnotes:
        target = traditional_footnotes[trad_key]
        return None if target == _DUPLICATE_CITATION_TARGET else target
    if re.match(r"^E-?\d+$", trad_key, re.IGNORECASE) or trad_key.upper().startswith("E-"):
        return f"evidence:{trad_key}"
    if trad_key.startswith(("evidence:", "run:", "filing:", "attachment:")):
        return trad_key
    return None


def _find_all_footnotes(text: str) -> list[str]:
    """Extract all ``[^id]`` and ``[引用N]`` identifiers in order of appearance."""
    return [
        _format_extracted_ref_id(m)
        for m in _ANY_FOOTNOTE_REF_RE.finditer(text)
    ]


_SENTENCE_BOUNDARY_RE = re.compile(
    r"[。！？!?；;]|(?<!\d)\.(?=\s|$)|(?<=\d)\.(?=\s+[A-Z\u4e00-\u9fff\[]|\s*$)"
)


def _sentence_bounds_for_span(
    line_text: str, start_pos: int, end_pos: int
) -> tuple[int, int]:
    """Return ``(sent_start, sent_end)`` character offsets for the sentence containing ``[start_pos, end_pos)``."""
    sent_start = 0
    sent_end = len(line_text)
    for bm in _SENTENCE_BOUNDARY_RE.finditer(line_text):
        if bm.end() <= start_pos:
            sent_start = max(sent_start, bm.end())
        elif bm.start() >= end_pos:
            sent_end = min(sent_end, bm.start())
            break
    return sent_start, sent_end


def _sentence_for_span(line_text: str, start_pos: int, end_pos: int) -> str:
    """Return the sentence substring within ``line_text`` containing ``[start_pos, end_pos)``."""
    sent_start, sent_end = _sentence_bounds_for_span(line_text, start_pos, end_pos)
    return line_text[sent_start:sent_end]


def _trailing_header_clause(header: str) -> str:
    """Return the trailing non-empty sentence of ``header`` for table row/column context."""
    s = (header or "").strip()
    if not s or not _SENTENCE_BOUNDARY_RE.search(s):
        return s
    non_empty = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(s) if p.strip()]
    return non_empty[-1] if non_empty else s


def _find_footnotes_for_span(
    line_text: str, end_pos: int, start_pos: int | None = None
) -> list[str]:
    """Find the footnote/citation cluster for a number span within the same sentence only.

    Prefers the first citation cluster at or after ``end_pos`` in the same sentence;
    falls back to the nearest citation cluster before ``start_pos`` in the same sentence.
    Never crosses sentence boundaries.
    """
    eff_start = end_pos if start_pos is None else start_pos
    sent_start, sent_end = _sentence_bounds_for_span(line_text, eff_start, end_pos)

    matches = [
        (m.start(), m.end(), _format_extracted_ref_id(m))
        for m in _ANY_FOOTNOTE_REF_RE.finditer(line_text)
        if m.start() >= sent_start and m.end() <= sent_end
    ]
    if not matches:
        return []

    clusters: list[tuple[int, int, list[str]]] = []
    idx = 0
    while idx < len(matches):
        c_start = matches[idx][0]
        c_end = matches[idx][1]
        c_ids = [matches[idx][2]]
        k = idx + 1
        while k < len(matches):
            between = line_text[matches[k - 1][1] : matches[k][0]]
            if re.fullmatch(r"[\s,，、/]*", between):
                c_end = matches[k][1]
                c_ids.append(matches[k][2])
                k += 1
            else:
                break
        clusters.append((c_start, c_end, c_ids))
        idx = k

    for c_start, _c_end, c_ids in clusters:
        if c_start >= end_pos - 1:
            return c_ids

    preceding = [c_ids for _c_start, c_end, c_ids in clusters if c_end <= eff_start + 1]
    if preceding:
        return preceding[-1]
    return []


def _resolve_footnote_bundle(
    fn_ids: list[str],
    traditional_footnotes: dict[str, str],
    citation_table: dict[str, str] | None = None,
    *,
    citation_table_missing: bool = False,
) -> tuple[str | None, str | None, list[str], list[str], list[str], list[str]]:
    """Resolve a list of footnote/citation IDs into primary ID/target, all targets, missing citations, and duplicate citations."""
    cits = (
        citation_table
        if citation_table is not None
        else {k: v for k, v in traditional_footnotes.items() if _is_citation_id(k)}
    )
    kept_fn_ids: list[str] = []
    targets: list[str] = []
    missing_citations: list[str] = []
    duplicate_citations: list[str] = []
    for fid in fn_ids:
        if _is_citation_id(fid):
            if citation_table_missing:
                # When the entire ## 引用 table is missing from the draft, treat [引用N] as unreferenced
                # so the figure enters Step D retrieval instead of being locked unverified.
                continue
            kept_fn_ids.append(fid)
            s = re.sub(r"\s+", "", fid.strip())
            raw_entry = cits.get(s) or cits.get(fid.strip())
            if raw_entry == _DUPLICATE_CITATION_TARGET:
                label = f"[{s}]"
                if label not in duplicate_citations:
                    duplicate_citations.append(label)
                continue
            target = _resolve_footnote_target(fid, traditional_footnotes, cits)
            if target is not None:
                if target not in targets:
                    targets.append(target)
            else:
                label = f"[{s}]"
                if label not in missing_citations:
                    missing_citations.append(label)
        else:
            kept_fn_ids.append(fid)
            trad_key = fid.strip()[1:] if fid.strip().startswith("^") else fid.strip()
            raw_entry = (
                traditional_footnotes.get(f"^{trad_key}")
                if citation_table is None and _is_citation_id(trad_key)
                else traditional_footnotes.get(trad_key)
            )
            if raw_entry == _DUPLICATE_CITATION_TARGET:
                label = f"[^{trad_key}]"
                if label not in duplicate_citations:
                    duplicate_citations.append(label)
                continue
            target = _resolve_footnote_target(fid, traditional_footnotes, cits)
            if target is not None and target not in targets:
                targets.append(target)
    fn_id = kept_fn_ids[0] if kept_fn_ids else None
    fn_target = targets[0] if targets else None
    return (
        fn_id,
        fn_target,
        kept_fn_ids,
        targets,
        missing_citations,
        duplicate_citations,
    )


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
    _blank(_ANY_FOOTNOTE_REF_RE)
    _blank(_SEC_ACCESSION_RE)
    _blank(_HYPHEN_DOC_CODE_RE)
    _blank(_ISO_DATE_RE)
    _blank(_CN_DATE_RE)
    _blank(_YEAR_RANGE_RE)
    _blank(_SHORT_DATE_RE)
    _blank(_SLASH_DATE_RE)
    _blank(_CLOCK_TIME_RE)
    _blank(_LONG_ID_INT_RE)
    _blank(_BATCH_CODE_RE)
    _blank(_FILING_FORM_RE)
    _blank(_TICKER_CODE_RE)
    _blank(_ENTITY_ID_RE)
    _blank(_PERIOD_TOKEN_RE)
    return "".join(chars)


def extract_figures(markdown_text: str) -> list[ExtractedFigure]:
    """Extract all figures (including exempt ones marked ``exempt=True``) from Markdown text (PK.9)."""
    (
        traditional_footnotes,
        citation_table,
        has_parseable_citation_table,
    ) = _parse_footnote_namespaces_with_status(markdown_text)
    citation_table_missing = not has_parseable_citation_table
    lines = markdown_text.splitlines()

    figures: list[ExtractedFigure] = []
    fig_counter = 0

    in_code_block = False
    in_html_comment = False
    current_section_period = ""
    current_section_title = ""
    last_caption_line = ""

    # Track Markdown table state
    table_headers: list[str] | None = None
    table_shared_footnotes: list[str] = []
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

        # Footnote or citation definition line
        if _FOOTNOTE_DEF_RE.match(raw_line) or _CITATION_DEF_RE.match(raw_line):
            i += 1
            continue

        # Section heading check (detect section-level period)
        if stripped.startswith("#"):
            heading_text = _HEADING_PREFIX_RE.sub("", stripped)
            if stripped.startswith("## ") or not current_section_title:
                current_section_title = heading_text.strip()
            pm = _PERIOD_TOKEN_RE.search(heading_text)
            if pm:
                current_section_period = normalize_period(pm.group(1))
            last_caption_line = heading_text
            table_headers = None
            table_shared_footnotes = []
            table_caption = ""
            # Strip heading numbering before scanning heading for any actual financial numbers
            scan_line = _HEADING_PREFIX_RE.sub(
                lambda m: " " * len(m.group(0)), raw_line
            )
        else:
            scan_line = _LIST_PREFIX_RE.sub(
                lambda m: " " * len(m.group(0)), raw_line
            )

        if not stripped:
            table_headers = None
            table_shared_footnotes = []
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
                hdr_fns = _find_all_footnotes(stripped)
                if hdr_fns:
                    table_shared_footnotes = hdr_fns
                for hdr_cell in cells:
                    if not _SENTENCE_BOUNDARY_RE.search(hdr_cell) and not has_exempt_marker(hdr_cell):
                        continue
                    masked_hdr = _mask_excluded_spans(hdr_cell)
                    for nm in _NUMBER_RE.finditer(masked_hdr):
                        num_str = nm.group("num").replace(",", "")
                        dec_str = nm.group("dec") or ""
                        if not dec_str and _STANDALONE_YEAR_RE.match(num_str):
                            continue
                        if any(
                            sm.start() <= nm.start() and nm.end() <= sm.end()
                            for sm in _STRUCTURAL_COUNT_RE.finditer(hdr_cell)
                        ):
                            continue
                        val = float(num_str + dec_str)
                        decimals = len(dec_str) - 1 if dec_str else 0
                        is_neg = bool(
                            nm.group("sign") in ("-", "−")
                            or (nm.group("open_paren") and nm.group("close_paren"))
                        )
                        if is_neg:
                            val = -val
                        raw_hdr_end = nm.start() + len(nm.group("full").rstrip())
                        sent_start, sent_end = _sentence_bounds_for_span(
                            hdr_cell, nm.start(), raw_hdr_end
                        )
                        hdr_sentence = hdr_cell[sent_start:sent_end]
                        hdr_cell_fns = _find_footnotes_for_span(
                            hdr_cell, raw_hdr_end, start_pos=nm.start()
                        )
                        (
                            fn_id,
                            fn_target,
                            fn_ids,
                            fn_targets,
                            missing_cits,
                            dup_cits,
                        ) = _resolve_footnote_bundle(
                            hdr_cell_fns,
                            traditional_footnotes,
                            citation_table,
                            citation_table_missing=citation_table_missing,
                        )
                        p_hdr = _PERIOD_TOKEN_RE.search(hdr_sentence)
                        eff_period = (
                            normalize_period(p_hdr.group(1))
                            if p_hdr
                            else current_section_period
                        )
                        eff_metric = (
                            _extract_metric_from_context(
                                hdr_sentence,
                                nm.start() - sent_start,
                                nm.end() - sent_start,
                            )
                            or _clean_metric_header(hdr_sentence)
                        )
                        currency, unit, scale = _determine_unit_and_scale(
                            nm.group("currency"),
                            nm.group("scale"),
                            nm.group("unit"),
                            context_text=hdr_sentence,
                            header_context="",
                        )
                        is_exempt = has_exempt_marker(hdr_sentence)
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
                                metric=eff_metric,
                                period=eff_period,
                                label_hint="reported",
                                footnote_id=fn_id,
                                footnote_target=fn_target,
                                footnote_ids=fn_ids,
                                footnote_targets=fn_targets,
                                missing_citations=missing_cits,
                                duplicate_citations=dup_cits,
                                location={
                                    "line": line_no,
                                    "table": True,
                                    "row": "",
                                    "col": _ANY_FOOTNOTE_REF_RE.sub("", hdr_cell).strip(" *"),
                                    "sentence": stripped,
                                    "section": current_section_title,
                                },
                                exempt=is_exempt,
                                exempt_reason="illustrative" if is_exempt else None,
                            )
                        )
                i += 2
                continue
            if _is_separator_row(cells):
                i += 1
                continue

            if table_headers is not None and cells:
                row_header = _ANY_FOOTNOTE_REF_RE.sub("", cells[0]).strip(" *")
                if row_header.lower() in ("来源", "数据来源", "source", "sources"):
                    src_fns = _find_all_footnotes(stripped)
                    if src_fns:
                        table_shared_footnotes = src_fns
                    i += 1
                    continue

                row_fns = _find_all_footnotes(cells[0])
                table_header_0 = (
                    _ANY_FOOTNOTE_REF_RE.sub("", table_headers[0]).strip(" *")
                    if table_headers
                    else ""
                )

                # Support 1-column numeric tables or tables where col 0 is itself a data column or multi-sentence prose
                start_col = 1
                if len(cells) == 1 or (
                    _NUMBER_RE.search(_mask_excluded_spans(cells[0]))
                    and not _PERIOD_TOKEN_RE.fullmatch(row_header)
                    and (
                        _SENTENCE_BOUNDARY_RE.search(cells[0])
                        or (
                            table_header_0
                            and table_header_0.lower()
                            not in ("指标", "项目", "metric", "item", "期间", "period")
                        )
                    )
                ):
                    start_col = 0

                for col_idx in range(start_col, len(cells)):
                    cell_text = cells[col_idx]
                    col_header = (
                        _ANY_FOOTNOTE_REF_RE.sub("", table_headers[col_idx]).strip(" *")
                        if col_idx < len(table_headers)
                        else ""
                    )
                    col_fns = (
                        _find_all_footnotes(table_headers[col_idx])
                        if col_idx < len(table_headers)
                        else []
                    )

                    eff_row_header = "" if col_idx == 0 else row_header
                    row_clause = _trailing_header_clause(eff_row_header)
                    col_clause = _trailing_header_clause(col_header)
                    clean_row = _clean_metric_header(row_clause)
                    clean_col = _clean_metric_header(col_clause)

                    # Determine period and metric from row_header / col_header
                    p_col = _PERIOD_TOKEN_RE.search(col_clause)
                    p_row = _PERIOD_TOKEN_RE.search(row_clause)
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
                    header_ctx = f"{row_clause} {col_clause} {table_header_0} {caption_unit_ctx}".strip()
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

                        raw_cell_end = nm.start() + len(nm.group("full").rstrip())
                        cell_fns = _find_footnotes_for_span(
                            cell_text, raw_cell_end, start_pos=nm.start()
                        )
                        eff_fns = cell_fns or row_fns or col_fns or table_shared_footnotes
                        (
                            fn_id,
                            fn_target,
                            fn_ids,
                            fn_targets,
                            missing_cits,
                            dup_cits,
                        ) = _resolve_footnote_bundle(
                            eff_fns,
                            traditional_footnotes,
                            citation_table,
                            citation_table_missing=citation_table_missing,
                        )

                        sent_start, sent_end = _sentence_bounds_for_span(
                            cell_text, nm.start(), raw_cell_end
                        )
                        cell_sentence = cell_text[sent_start:sent_end]
                        is_first_cell_sentence = sent_start == 0
                        eff_period = period
                        eff_metric = metric
                        p_cell = _PERIOD_TOKEN_RE.search(cell_sentence)
                        if p_cell and (not eff_period or not is_first_cell_sentence):
                            eff_period = normalize_period(p_cell.group(1))
                        if not is_first_cell_sentence or col_idx == 0:
                            sent_metric = _extract_metric_from_context(
                                cell_sentence,
                                nm.start() - sent_start,
                                nm.end() - sent_start,
                            )
                            if sent_metric:
                                eff_metric = sent_metric

                        currency, unit, scale = _determine_unit_and_scale(
                            nm.group("currency"),
                            nm.group("scale"),
                            nm.group("unit"),
                            context_text=f"{cell_sentence} {row_clause} {col_clause}".strip(),
                            header_context=header_ctx,
                        )
                        label_hint = "reported"
                        is_exempt = has_exempt_marker(cell_sentence)
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
                                metric=eff_metric,
                                period=eff_period,
                                label_hint=label_hint,
                                footnote_id=fn_id,
                                footnote_target=fn_target,
                                footnote_ids=fn_ids,
                                footnote_targets=fn_targets,
                                missing_citations=missing_cits,
                                duplicate_citations=dup_cits,
                                location={
                                    "line": line_no,
                                    "table": True,
                                    "row": eff_row_header,
                                    "col": col_header,
                                    "sentence": stripped,
                                    "section": current_section_title,
                                },
                                exempt=is_exempt,
                                exempt_reason="illustrative" if is_exempt else None,
                            )
                        )
                i += 1
                continue
        else:
            table_headers = None
            table_shared_footnotes = []
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

        # Mask filing forms (e.g. "10-Q" in "Q2 10-Q") and inline refs before scanning for period tokens
        period_scan_line = scan_line
        for pat in (_INLINE_CODE_RE, _MARKDOWN_LINK_RE, _ANY_FOOTNOTE_REF_RE, _FILING_FORM_RE):
            period_scan_line = pat.sub(lambda m: " " * len(m.group(0)), period_scan_line)
        line_periods = sorted(
            [
                (pm.start(), normalize_period(pm.group(1)))
                for pm in _PERIOD_TOKEN_RE.finditer(period_scan_line)
            ]
            + [(pos, "LTM") for pos in match_ltm_periods(period_scan_line)]
        )

        line_num_matches = list(_NUMBER_RE.finditer(masked_line))
        for nm_idx, nm in enumerate(line_num_matches):
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
                    bool(
                        re.match(
                            r"^\s*(?:USD|HKD|CNY|RMB|港币|per\s+share|/share\b|/sh\b)",
                            scan_line[nm.end() : nm.end() + 15],
                            re.IGNORECASE,
                        )
                    ),
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

            # Nearest period in same line: prefer preceding period on the line;
            # only look forward within the same clause (no 。；; between number and period)
            period = current_section_period
            if line_periods:
                preceding = [p for pos, p in line_periods if pos <= nm.start()]
                if preceding:
                    period = preceding[-1]
                else:
                    following_same_clause = [
                        p
                        for pos, p in line_periods
                        if pos > nm.start()
                        and not re.search(r"[。；;]", scan_line[nm.end() : pos])
                    ]
                    if following_same_clause:
                        period = following_same_clause[0]

            # Nearest footnote/citation cluster for this number within the same sentence
            raw_tok_end = nm.start() + len(nm.group("full").rstrip())
            sent_text = _sentence_for_span(raw_line, nm.start(), raw_tok_end)
            is_exempt = is_blockquote or has_exempt_marker(sent_text)
            exempt_reason = (
                "blockquote_quote"
                if is_blockquote
                else ("illustrative" if is_exempt else None)
            )
            fn_cluster = _find_footnotes_for_span(
                raw_line, raw_tok_end, start_pos=nm.start()
            )
            (
                fn_id,
                fn_target,
                fn_ids,
                fn_targets,
                missing_cits,
                dup_cits,
            ) = _resolve_footnote_bundle(
                fn_cluster,
                traditional_footnotes,
                citation_table,
                citation_table_missing=citation_table_missing,
            )

            metric = _extract_metric_from_context(scan_line, nm.start(), nm.end())
            sent_start, sent_end = _sentence_bounds_for_span(
                scan_line, nm.start(), raw_tok_end
            )
            prev_nm_end = (
                line_num_matches[nm_idx - 1].end() if nm_idx > 0 else 0
            )
            next_nm_start = (
                line_num_matches[nm_idx + 1].start()
                if nm_idx + 1 < len(line_num_matches)
                else len(scan_line)
            )
            left_bound = max(sent_start, prev_nm_end, nm.start() - 25)
            right_bound = min(sent_end, next_nm_start, nm.end() + 15)
            ctx_left = scan_line[left_bound : nm.start()]
            for sep in ("。", "；", ";", "，", ",", "（", "("):
                if sep in ctx_left:
                    ctx_left = ctx_left.rsplit(sep, 1)[-1]
            ctx_right = scan_line[nm.end() : right_bound]
            for sep in ("。", "；", ";", "，", ",", "）", ")"):
                if sep in ctx_right:
                    ctx_right = ctx_right.split(sep, 1)[0]
            clause_ctx = f"{ctx_left}{scan_line[nm.start() : nm.end()]}{ctx_right}"
            currency, unit, scale = _determine_unit_and_scale(
                nm.group("currency"),
                nm.group("scale"),
                nm.group("unit"),
                context_text=clause_ctx,
                header_context="",
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
                    label_hint="reported",
                    footnote_id=fn_id,
                    footnote_target=fn_target,
                    footnote_ids=fn_ids,
                    footnote_targets=fn_targets,
                    missing_citations=missing_cits,
                    duplicate_citations=dup_cits,
                    location={
                        "line": line_no,
                        "table": False,
                        "row": None,
                        "col": None,
                        "sentence": stripped,
                        "section": current_section_title,
                    },
                    exempt=is_exempt,
                    exempt_reason=exempt_reason,
                )
            )

        i += 1

    return figures


_VALID_LABEL_KINDS = frozenset(
    {"reported", "estimate", "consensus", "guidance", "derived", "incidental"}
)

_FINANCIAL_RAW_TOKENS = (
    "%",
    "％",
    "$",
    "¥",
    "美元",
    "港元",
    "人民币",
    "元",
    "亿",
    "万",
    "百万",
    "bn",
    "mn",
    "billion",
    "million",
    "bp",
    "bps",
    "pp",
    "倍",
)


def _incidental_guard_reject_reasons(fig: ExtractedFigure, item_unit: str = "") -> list[str]:
    """Return guard rejection reasons preventing ``fig`` from being ``incidental`` (P0I §1.4 / L35 / L43)."""
    reasons: list[str] = []
    if fig.location.get("table"):
        reasons.append("table")
    if fig.decimals > 0:
        reasons.append("decimals")
    if fig.currency is not None or fig.scale > 1.0:
        reasons.append("currency_or_scale")
    if (fig.unit or "").strip().lower() not in ("", "other", "count"):
        reasons.append("unit")
    if (item_unit or "").strip().lower() not in ("", "other", "count"):
        reasons.append("item_unit")
    raw_low = (fig.raw or "").strip().lower()
    if any(tok in raw_low for tok in _FINANCIAL_RAW_TOKENS) or re.search(r"\d\s*x$", raw_low):
        reasons.append("financial_token")
    return reasons


def _can_be_incidental(fig: ExtractedFigure, item_unit: str = "") -> bool:
    return not _incidental_guard_reject_reasons(fig, item_unit)


def _split_markdown_sections_and_chunks(
    markdown_text: str,
    max_chunk_chars: int = 6000,
) -> tuple[list[str], list[tuple[str, int, int, str]]]:
    """Split markdown by H2 (`## `) sections and paragraph chunks (<=6000 chars with 1-paragraph overlap).

    Returns ``(outline, chunks)`` where each chunk is ``(section_title, start_line, end_line, chunk_text)``.
    """
    lines = markdown_text.splitlines()
    if not lines:
        return [], []

    outline: list[str] = []
    sections: list[tuple[str, int, int]] = []
    curr_title = ""
    curr_start = 1
    in_code = False

    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("#"):
            if stripped.startswith("# ") or stripped.startswith("## "):
                outline.append(stripped)
            if stripped.startswith("## "):
                if idx > curr_start:
                    sections.append((curr_title, curr_start, idx - 1))
                curr_title = _HEADING_PREFIX_RE.sub("", stripped).strip() or stripped
                curr_start = idx
            elif not curr_title:
                curr_title = _HEADING_PREFIX_RE.sub("", stripped).strip() or stripped

    if curr_start <= len(lines):
        sections.append((curr_title, curr_start, len(lines)))

    chunks: list[tuple[str, int, int, str]] = []
    for sec_title, s_line, e_line in sections:
        sec_text = "\n".join(lines[s_line - 1 : e_line])
        if len(sec_text) <= max_chunk_chars:
            chunks.append((sec_title, s_line, e_line, sec_text))
            continue

        # Split long section by paragraphs (blank lines), overlapping 1 paragraph between chunks
        paragraphs: list[tuple[int, int, str]] = []
        p_start = s_line
        buf: list[str] = []
        for l_no in range(s_line, e_line + 1):
            l_txt = lines[l_no - 1]
            if not l_txt.strip() and buf:
                paragraphs.append((p_start, l_no - 1, "\n".join(buf)))
                buf = []
                p_start = l_no + 1
            else:
                if not buf:
                    p_start = l_no
                buf.append(l_txt)
        if buf:
            paragraphs.append((p_start, e_line, "\n".join(buf)))

        if not paragraphs:
            chunks.append((sec_title, s_line, e_line, sec_text))
            continue

        c_start = paragraphs[0][0]
        c_end = paragraphs[0][1]
        c_paras: list[str] = []
        prev_overlap_para = ""
        for p_s, p_e, p_txt in paragraphs:
            candidate_len = sum(len(x) + 2 for x in c_paras) + len(p_txt)
            if c_paras and candidate_len > max_chunk_chars:
                prefix = f"{prev_overlap_para}\n\n" if prev_overlap_para else ""
                chunks.append((sec_title, c_start, c_end, prefix + "\n\n".join(c_paras)))
                prev_overlap_para = c_paras[-1]
                c_start = p_s
                c_end = p_e
                c_paras = [p_txt]
            else:
                if not c_paras:
                    c_start = p_s
                c_end = p_e
                c_paras.append(p_txt)
        if c_paras:
            prefix = f"{prev_overlap_para}\n\n" if prev_overlap_para else ""
            chunks.append((sec_title, c_start, c_end, prefix + "\n\n".join(c_paras)))

    return outline, chunks


STEP_B_LABEL_INSTRUCTIONS = (
    "You are Step B of the Dalton publication audit pipeline. "
    "1) For each item in extracted_figures, label its financial `metric`, normalized `period` "
    "(e.g. 4Q23, FY2023, LTM, or 'none' if it is an intraday/spot figure with no fiscal period), "
    "canonical `unit` (e.g. USD_bn, USD_mn, USD, pct, bp, x, per_share, count_mn, count), "
    "and semantic `kind` ('reported', 'estimate', 'consensus', 'guidance', 'derived', or 'incidental'). "
    "Use `incidental` ONLY when a number belongs strictly to one of these 9 non-verification categories (L43), "
    "and set `incidental_category` to the exact category name: "
    "(1) 点赞数、转发数等社交互动计数; (2) 浏览量; (3) HTTP 状态码; (4) 文件、文档、邮件和 URL 里的编号; "
    "(5) 软件和模型的版本号、硬件型号; (6) 列表序号、页码; (7) 用户名里的数字; "
    "(8) 机构、产品、节目名称里的数字; (9) 期间、日期和时间窗口表达里的数字. "
    "COUNTEREXAMPLES (MUST NOT be marked `incidental`): Any factual statement in the research report, "
    "regardless of unit, MUST be verified (`reported`/`estimate`/`consensus`/`guidance`/`derived`), "
    "including: 装机容量、合同期限、样本量、覆盖半径、调查家数、量子比特数. "
    "NEVER mark any number with currency, percentage (%), basis points (bp), decimal, or multiple (x/倍) "
    "as `incidental`. "
    "2) Also scan markdown_excerpt for any colloquial Chinese numbers not already in extracted_figures "
    "(such as '两成', '四成', '五分之一', '四分之三', '过半', '减半', '翻番', '腰斩') "
    "and return them in `extra_figures` with exact `raw` substring from the draft "
    "(`raw` 必须包含紧挨在数字前面的约数限定词，如 '将近四成'、'不到五分之一'), `line` number, "
    "numeric `value` (e.g. 20.0 for 两成, 50.0 for 过半/减半/腰斩, 2.0 for 翻番), `unit` ('pct' or 'x'), "
    "`metric`, `period`, and `kind`."
)


async def label_figures_with_status(
    markdown_text: str,
    figures: list[ExtractedFigure],
    *,
    user_id: str,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    stats_out: dict[str, Any] | None = None,
    deadline: float | None = None,
) -> tuple[list[ExtractedFigure], bool]:
    """Step B LLM figure labeling (`audit.label_figures`) across all figures and sections (P0I §1.4, L28, L35, L43)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    total_initial = len(figures)
    if assist_json is None:
        for f in figures:
            f.label_hint = "reported"
            f.incidental_category = ""
        if stats_out is not None:
            stats_out.update(
                {
                    "total": total_initial,
                    "labeled": 0,
                    "batches": 1 if total_initial else 0,
                    "failed_batches": 1 if total_initial else 0,
                    "failed_batch_details": (
                        [{"batch_index": 1, "start_fig": 1, "end_fig": total_initial, "section": ""}]
                        if total_initial
                        else []
                    ),
                    "incidental_excluded": 0,
                    "llm_period_overrides": 0,
                }
            )
        return figures, False

    (
        traditional_footnotes,
        citation_table,
        has_parseable_citation_table,
    ) = _parse_footnote_namespaces_with_status(markdown_text)
    citation_table_missing = not has_parseable_citation_table
    lines = markdown_text.splitlines()
    outline, chunks = _split_markdown_sections_and_chunks(markdown_text, max_chunk_chars=6000)

    # Map each figure to its chunk; batch up to 40 figures per call; send every section to Step B
    batches: list[tuple[str, int, str, list[ExtractedFigure], int, int]] = []
    assigned_ids: set[str] = set()
    fig_ordinal_map = {f.id: idx for idx, f in enumerate(figures, start=1)}

    for sec_title, c_start, c_end, chunk_text in chunks:
        chunk_figs = [
            f
            for f in figures
            if f.id not in assigned_ids
            and c_start <= int(f.location.get("line") or 1) <= c_end
        ]
        for f in chunk_figs:
            assigned_ids.add(f.id)

        if chunk_figs:
            for offset in range(0, len(chunk_figs), 40):
                sub = chunk_figs[offset : offset + 40]
                s_idx = fig_ordinal_map[sub[0].id]
                e_idx = fig_ordinal_map[sub[-1].id]
                batches.append((sec_title, c_start, chunk_text, sub, s_idx, e_idx))
        elif chunk_text.strip() and sec_title.strip() != "引用":
            non_def_lines = [
                ln
                for ln in chunk_text.splitlines()
                if ln.strip()
                and not ln.strip().startswith("#")
                and not _FOOTNOTE_DEF_RE.match(ln)
                and not _CITATION_DEF_RE.match(ln)
            ]
            if non_def_lines:
                batches.append((sec_title, c_start, chunk_text, [], 0, 0))

    leftover = [f for f in figures if f.id not in assigned_ids]
    if leftover:
        for offset in range(0, len(leftover), 40):
            sub = leftover[offset : offset + 40]
            s_idx = fig_ordinal_map[sub[0].id]
            e_idx = fig_ordinal_map[sub[-1].id]
            batches.append(("", 1, markdown_text[:6000], sub, s_idx, e_idx))

    if not batches and markdown_text.strip():
        batches.append(("", 1, markdown_text[:6000], [], 0, 0))

    fig_counter = len(figures)
    existing_raws_by_line: set[tuple[int, int, str]] = set()
    for f in figures:
        f_line = f.location.get("line")
        if isinstance(f_line, int) and 1 <= f_line <= len(lines) and f.raw:
            l_txt = lines[f_line - 1]
            search_pos = 0
            while search_pos <= len(l_txt):
                found_pos = l_txt.find(f.raw, search_pos)
                if found_pos < 0:
                    break
                if (f_line, found_pos, f.raw) not in existing_raws_by_line:
                    existing_raws_by_line.add((f_line, found_pos, f.raw))
                    break
                search_pos = found_pos + max(1, len(f.raw))

    labeled_count = 0
    failed_batches = 0
    failed_batch_details: list[dict[str, Any]] = []
    llm_period_overrides = 0

    pub = get_dalton_config().publish
    sem = asyncio.Semaphore(pub.audit_concurrency)
    audit_model = await resolve_dalton_audit_model(user_id)
    audit_fallback_models = await resolve_dalton_audit_fallback_models(user_id)
    audit_timeout_s = await resolve_dalton_audit_timeout_s(user_id)
    label_schema = FigureLabels.model_json_schema()

    prepared_batches: list[
        tuple[int, str, int, str, list[ExtractedFigure], int, int, dict[str, ExtractedFigure], dict[str, Any]]
    ] = []
    for batch_idx, (sec_title, c_start, chunk_text, batch_figs, s_idx, e_idx) in enumerate(batches, start=1):
        local_to_fig: dict[str, ExtractedFigure] = {}
        payload_figs: list[dict[str, Any]] = []
        for local_i, f in enumerate(batch_figs, start=1):
            local_id = f"B-{local_i:03d}"
            local_to_fig[local_id] = f
            local_to_fig[f.id] = f
            rel_line = max(1, int(f.location.get("line") or c_start) - c_start + 1)
            payload_figs.append(
                {
                    "id": local_id,
                    "raw": f.raw,
                    "value": f.value,
                    "unit": f.unit,
                    "scale": f.scale,
                    "metric": f.metric,
                    "period": f.period,
                    "kind": f.label_hint,
                    "exempt": f.exempt,
                    "line": rel_line,
                    "sentence": f.location.get("sentence"),
                }
            )
        payload = {
            "outline": outline,
            "section_title": sec_title,
            "markdown_excerpt": chunk_text,
            "extracted_figures": payload_figs,
        }
        prepared_batches.append(
            (batch_idx, sec_title, c_start, chunk_text, batch_figs, s_idx, e_idx, local_to_fig, payload)
        )

    async def _call_label_batch(payload: dict[str, Any]) -> Any:
        async with sem:
            return await assist_json(
                task="audit.label_figures",
                instructions=STEP_B_LABEL_INSTRUCTIONS,
                payload=payload,
                schema=label_schema,
                user_id=user_id,
                workspace_id=workspace_id,
                thread_id=thread_id,
                timeout_s=audit_timeout_s,
                model=audit_model,
                fallback_models=audit_fallback_models,
                deadline=deadline,
            )

    if not prepared_batches:
        batch_results = []
    elif deadline is None:
        batch_results = await asyncio.gather(*[_call_label_batch(pb[8]) for pb in prepared_batches])
    else:
        # Outer guard on the audit budget: a batch still running at the deadline is
        # cancelled and counted failed, like any other failed batch. Its journal row
        # stays claimed, so the cancelled call can never be billed twice. The finally
        # also covers the audit itself being cancelled: no batch outlives it.
        label_tasks = [asyncio.create_task(_call_label_batch(pb[8])) for pb in prepared_batches]
        try:
            await asyncio.wait(
                label_tasks, timeout=max(0.0, deadline - time.monotonic()) + _LABEL_DEADLINE_GRACE_S
            )
        finally:
            unfinished = [t for t in label_tasks if not t.done()]
            for t in unfinished:
                t.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        batch_results = []
        for t in label_tasks:
            if t.cancelled() or t.exception() is not None:
                batch_results.append(
                    _LabelBatchUnfinished(error="核验总时限已到，该批未完成" if t.cancelled() else str(t.exception()))
                )
            else:
                batch_results.append(t.result())

    def _apply_label_results(batch_results: list[Any]) -> tuple[list[ExtractedFigure], bool]:
        # Pure CPU over the draft text; run off the event loop (see aaudit_draft).
        nonlocal failed_batches, labeled_count, llm_period_overrides, fig_counter
        for (batch_idx, sec_title, c_start, chunk_text, batch_figs, s_idx, e_idx, local_to_fig, _payload), res in zip(
            prepared_batches, batch_results, strict=False
        ):
            for f in batch_figs:
                f.label_hint = "reported"
                f.incidental_category = ""

            if not res.ok or not res.data:
                failed_batches += 1
                failed_batch_details.append(
                    {
                        "batch_index": batch_idx,
                        "start_fig": s_idx,
                        "end_fig": e_idx,
                        "section": sec_title,
                    }
                )
                continue

            try:
                parsed = FigureLabels.model_validate(res.data)
            except Exception:
                failed_batches += 1
                failed_batch_details.append(
                    {
                        "batch_index": batch_idx,
                        "start_fig": s_idx,
                        "end_fig": e_idx,
                        "section": sec_title,
                    }
                )
                continue

            labeled_count += len(batch_figs)
            for item in parsed.items:
                target = local_to_fig.get(item.id)
                if target is None:
                    continue

                raw_kind = item.kind.strip().lower() if item.kind else "reported"
                if raw_kind not in _VALID_LABEL_KINDS:
                    raw_kind = "reported"
                inc_cat = (item.incidental_category or "").strip()
                reject_reasons = _incidental_guard_reject_reasons(target, item.unit)
                if raw_kind == "incidental" and not reject_reasons:
                    target.label_hint = "incidental"
                    target.incidental_category = inc_cat or "未给类别"
                else:
                    target.label_hint = "reported" if raw_kind == "incidental" else raw_kind
                    if (
                        raw_kind == "incidental"
                        and reject_reasons == ["decimals"]
                        and inc_cat in _DECIMAL_UNCERTAIN_CATEGORIES
                    ):
                        target.incidental_category = inc_cat

                if item.metric and item.metric.strip():
                    target.metric = item.metric.strip()

                if item.period and item.period.strip():
                    p_raw = item.period.strip()
                    if p_raw.lower() in ("none", "n/a", "null", "spot", "intraday", "无", "-"):
                        if target.period:
                            llm_period_overrides += 1
                        target.period = ""
                    else:
                        norm_p = normalize_period(p_raw) or p_raw
                        if norm_p != target.period:
                            llm_period_overrides += 1
                        target.period = norm_p

                if item.unit and item.unit.strip():
                    u_clean = item.unit.strip()
                    if target.label_hint != "incidental" or u_clean in ("other", "count"):
                        if target.scale == 1e8 and u_clean in ("USD_bn", "HKD_bn", "CNY_bn"):
                            u_clean = f"{u_clean[:-3]}_100mn"
                        if u_clean == "per_share":
                            target.unit = _PerShareUnit("per_share", currency=target.currency)
                        else:
                            target.unit = u_clean

            for extra in parsed.extra_figures:
                raw_sub = (extra.raw or "").strip()
                if not raw_sub:
                    continue
                matched_line_no = extra.line if 1 <= extra.line <= len(lines) else 1
                matched_line_text = ""
                if 1 <= matched_line_no <= len(lines) and raw_sub in lines[matched_line_no - 1]:
                    matched_line_text = lines[matched_line_no - 1]
                else:
                    for idx, l_text in enumerate(lines, start=1):
                        if raw_sub in l_text:
                            matched_line_no = idx
                            matched_line_text = l_text
                            break
                if not matched_line_text:
                    continue

                orig_raw_sub = raw_sub
                selected_raw: str | None = None
                raw_pos = -1
                search_start = 0
                while search_start <= len(matched_line_text):
                    occ_pos = matched_line_text.find(orig_raw_sub, search_start)
                    if occ_pos < 0:
                        break
                    cand_raw, cand_pos = _expand_colloquial_raw_with_qualifier(
                        matched_line_text, orig_raw_sub, occ_pos
                    )
                    if (
                        (matched_line_no, occ_pos, orig_raw_sub) not in existing_raws_by_line
                        and (matched_line_no, cand_pos, cand_raw) not in existing_raws_by_line
                    ):
                        selected_raw = cand_raw
                        raw_pos = cand_pos
                        existing_raws_by_line.add((matched_line_no, occ_pos, orig_raw_sub))
                        existing_raws_by_line.add((matched_line_no, cand_pos, cand_raw))
                        break
                    search_start = occ_pos + max(1, len(orig_raw_sub))

                if selected_raw is None or raw_pos < 0:
                    continue
                raw_sub = selected_raw
                fn_cluster = _find_footnotes_for_span(
                    matched_line_text, raw_pos + len(raw_sub), start_pos=raw_pos
                )
                (
                    fn_id,
                    fn_target,
                    fn_ids,
                    fn_targets,
                    missing_cits,
                    dup_cits,
                ) = _resolve_footnote_bundle(
                    fn_cluster,
                    traditional_footnotes,
                    citation_table,
                    citation_table_missing=citation_table_missing,
                )
                extra_sent_text = _sentence_for_span(
                    matched_line_text, raw_pos, raw_pos + len(raw_sub)
                )
                is_exempt = matched_line_text.strip().startswith(">") or has_exempt_marker(
                    extra_sent_text
                )
                kind_val = (
                    extra.kind.lower()
                    if extra.kind and extra.kind.lower() in _VALID_LABEL_KINDS
                    else "reported"
                )
                if kind_val == "incidental":
                    kind_val = "reported"
                period_val = normalize_period(extra.period) if extra.period else ""
                if not period_val:
                    pm = _PERIOD_TOKEN_RE.search(matched_line_text)
                    if pm:
                        period_val = normalize_period(pm.group(1))
                metric_val = extra.metric or _extract_metric_from_context(
                    matched_line_text, raw_pos, raw_pos + len(raw_sub)
                )

                fig_counter += 1
                new_fig = ExtractedFigure(
                    id=f"F-{fig_counter:03d}",
                    raw=raw_sub,
                    value=extra.value,
                    decimals=0,
                    unit=extra.unit or "pct",
                    scale=1.0,
                    currency=None,
                    metric=metric_val,
                    period=period_val,
                    label_hint=kind_val,
                    footnote_id=fn_id,
                    footnote_target=fn_target,
                    footnote_ids=fn_ids,
                    footnote_targets=fn_targets,
                    missing_citations=missing_cits,
                    duplicate_citations=dup_cits,
                    location={
                        "line": matched_line_no,
                        "table": False,
                        "row": None,
                        "col": None,
                        "sentence": matched_line_text.strip(),
                        "section": sec_title,
                    },
                    exempt=is_exempt,
                    exempt_reason="illustrative" if is_exempt else None,
                )
                figures.append(new_fig)
                labeled_count += 1

        incidental_excluded = sum(
            1 for f in figures if not f.exempt and f.label_hint == "incidental"
        )
        if stats_out is not None:
            stats_out.update(
                {
                    "total": len(figures),
                    "labeled": labeled_count,
                    "batches": len(batches),
                    "failed_batches": failed_batches,
                    "failed_batch_details": failed_batch_details,
                    "incidental_excluded": incidental_excluded,
                    "llm_period_overrides": llm_period_overrides,
                }
            )

        return figures, failed_batches == 0

    return await asyncio.to_thread(_apply_label_results, batch_results)



