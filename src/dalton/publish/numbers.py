"""Deterministic extraction of financial figures from Markdown prose and tables (PK.9)."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, model_validator

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

# Single authoritative exemption marker table (P0J J2 §6): 4 words × 3 bracket styles
EXEMPT_MARKER_WORDS: tuple[str, ...] = ("示意", "假设", "估计", "举例")
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

    if scale == 1.0 and combined_ctx and re.search(
        r"(?:[美港]元/股|人民币/股|元/股|每股|\beps\b|per\s+share|/share\b|/sh\b)",
        combined_ctx,
        re.IGNORECASE,
    ):
        return currency or "USD", "per_share", 1.0

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
    r"[（\(]\s*(?:单位\s*[：:]\s*)?(?:亿美元|亿元|万元|百万美元|百万|十亿|千亿|元|美元|港元|人民币|"
    r"%\s*|pct|bps|bp|\$[BMKbmk]?|USD|CNY|RMB|HKD|in\s+\$?[BMKbmk]|in\s+(?:billions?|millions?))\s*[）\)]",
    re.IGNORECASE,
)


def _clean_metric_header(header_text: str) -> str:
    """Strip unit parentheticals like '（亿美元）' or '($B)' from a table metric header."""
    cleaned = _UNIT_PAREN_STRIP_RE.sub("", header_text).strip(" *")
    return cleaned or header_text.strip(" *")


def _resolve_footnote_target(
    fn_id: str | None, footnotes: dict[str, str]
) -> str | None:
    """Resolve footnote ID to its target prefix, supporting inline [^E0001] and [^E-...] evidence refs."""
    if not fn_id:
        return None
    if fn_id in footnotes:
        return footnotes[fn_id]
    s = fn_id.strip()
    if re.match(r"^E-?\d+$", s, re.IGNORECASE) or s.upper().startswith("E-"):
        return f"evidence:{s}"
    if s.startswith(("evidence:", "run:", "filing:", "attachment:")):
        return s
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
    footnotes = parse_footnotes(markdown_text)
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
                        label_hint = "reported"
                        is_exempt = (
                            has_exempt_marker(cell_text)
                            or has_exempt_marker(eff_row_header)
                            or has_exempt_marker(col_header)
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

        # Mask filing forms (e.g. "10-Q" in "Q2 10-Q") and inline refs before scanning for period tokens
        period_scan_line = scan_line
        for pat in (_INLINE_CODE_RE, _MARKDOWN_LINK_RE, _FOOTNOTE_REF_RE, _FILING_FORM_RE):
            period_scan_line = pat.sub(lambda m: " " * len(m.group(0)), period_scan_line)
        line_periods = sorted(
            [
                (pm.start(), normalize_period(pm.group(1)))
                for pm in _PERIOD_TOKEN_RE.finditer(period_scan_line)
            ]
            + [(pos, "LTM") for pos in match_ltm_periods(period_scan_line)]
        )
        line_footnotes = [
            (fm.start(), fm.group(1))
            for fm in _FOOTNOTE_REF_RE.finditer(raw_line)
        ]

        is_exempt = is_blockquote or has_exempt_marker(raw_line)
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
            ctx_left = scan_line[max(0, nm.start() - 25) : nm.start()]
            for sep in ("。", "；", ";", "，", ",", "（", "）", "(", ")"):
                if sep in ctx_left:
                    ctx_left = ctx_left.rsplit(sep, 1)[-1]
            ctx_right = scan_line[nm.end() : nm.end() + 15]
            for sep in ("。", "；", ";", "，", ",", "（", "）", "(", ")"):
                if sep in ctx_right:
                    ctx_right = ctx_right.split(sep, 1)[0]
            clause_ctx = f"{ctx_left}{scan_line[nm.start() : nm.end()]}{ctx_right}"
            currency, unit, scale = _determine_unit_and_scale(
                nm.group("currency"),
                nm.group("scale"),
                nm.group("unit"),
                context_text=clause_ctx,
                header_context=last_caption_line if "单位" in last_caption_line else "",
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

    footnotes = parse_footnotes(markdown_text)
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
        elif chunk_text.strip():
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

    sem = asyncio.Semaphore(4)
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
                role="audit",
                timeout_s=300.0,
                # Subscription models: a call that timed out may be re-sent rather than
                # refused as "possibly billed", or a re-run after a timeout fails at once.
                resend_indeterminate=True,
            )

    batch_results = await asyncio.gather(*[_call_label_batch(pb[8]) for pb in prepared_batches])

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
            line_fns = [
                (fm.start(), fm.group(1))
                for fm in _FOOTNOTE_REF_RE.finditer(matched_line_text)
            ]
            fn_id: str | None = None
            after_fns = [fid for pos, fid in line_fns if pos >= raw_pos + len(raw_sub) - 1]
            if after_fns:
                fn_id = after_fns[0]
            elif line_fns:
                fn_id = line_fns[-1][1]
            fn_target = _resolve_footnote_target(fn_id, footnotes)
            is_exempt = matched_line_text.strip().startswith(">") or has_exempt_marker(matched_line_text)
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



