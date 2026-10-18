"""Deterministic extraction of financial figures from Markdown prose and tables (PK.9)."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from pydantic import BaseModel, Field

# Period tokens: FY2026, FY26, 3Q26, Q3 2026, 1H26, 2H26, LTM-2026-09, 2026E, NTM
_PERIOD_TOKEN_RE = re.compile(
    r"\b("
    r"FY\d{2,4}E?|"
    r"[1-4]Q\d{2,4}E?|"
    r"Q[1-4]\s*['’]?\d{2,4}E?|"
    r"[12]H\d{2,4}E?|"
    r"LTM(?:-\d{4}-\d{2})?|"
    r"NTM|"
    r"20\d{2}E"
    r")\b",
    re.IGNORECASE,
)

# Standalone date / period / filing form / identifier patterns to exclude
_ISO_DATE_RE = re.compile(r"\b20\d{2}[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?\b")
_CN_DATE_RE = re.compile(r"20\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?")
_SHORT_DATE_RE = re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?!\d)")
_YEAR_RANGE_RE = re.compile(r"\b20\d{2}\s*[-–~至]\s*20\d{2}\b")
_STANDALONE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_FILING_FORM_RE = re.compile(r"\b(?:10-K|10-Q|8-K|6-K|20-F|DEF\s*14A|S-1|13F)\b", re.IGNORECASE)
_TICKER_CODE_RE = re.compile(r"\b\d{4,6}\.(?:HK|SH|SZ|SS|TW)\b", re.IGNORECASE)
_ENTITY_ID_RE = re.compile(
    r"\b(?:P\d+|D\d+|K\d+(?:\.\d+)?|Q-\d{3}|V-\d{3}|T\d+|L-\d{3}|G-\d{3,4}|"
    r"W\d+|C\d+|H\d+|v\d+|E-\d{8}-\d{2,3}|VD-\d{8}-\d{2,3}|F-\d{3})\b"
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

# Exempt markers for illustrative / assumption examples
_EXEMPT_MARKER_RE = re.compile(r"(?:〔示意〕|\[示意\]|（示意）|〔假设〕|\[假设\]|（举例）)")

# Core financial number regex:
# Captures optional sign/parens, optional currency prefix, number with commas/decimals,
# optional scale (k/mn/m/bn/b/万/亿/千亿/百亿/百万), optional unit (%/pp/bp/bps/x/倍/美元/港元/元)
_NUMBER_RE = re.compile(
    r"(?P<full>"
    r"(?P<open_paren>\()?"
    r"(?P<sign>[+\-−])?\s*"
    r"(?P<currency>US\$|HK\$|RMB|CNY|USD|HKD|\$|¥)?"
    r"\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s*"
    r"(?P<scale>千亿|百亿|百万|万亿|亿|万|bn|mn|k|m|b)?"
    r"\s*"
    r"(?P<unit>bps|bp|pp|pts|%|倍|x|美元|港元|人民币|元)?"
    r"(?P<close_paren>\))?"
    r")",
    re.IGNORECASE,
)

SCALE_MULTIPLIERS: dict[str, float] = {
    "k": 1e3,
    "万": 1e4,
    "m": 1e6,
    "mn": 1e6,
    "百万": 1e6,
    "亿": 1e8,
    "b": 1e9,
    "bn": 1e9,
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
    """LLM-annotated metric and period for one extracted figure ID."""

    id: str = Field(description="Extracted figure ID, e.g. F-001")
    metric: str = Field(description="Financial metric name")
    period: str = Field(description="Normalized period, e.g. 3Q26 or FY2026")


class FigureLabels(BaseModel):
    """Structured response for LLM-assisted prose figure labeling (PK.9)."""

    items: list[FigureLabelItem] = Field(default_factory=list)


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
    m_h = re.match(r"^([12])H(?:20)?(\d{2})$", s)
    if m_h:
        return f"{m_h.group(1)}H{m_h.group(2)}"
    m_year = re.match(r"^(20\d{2})$", s)
    if m_year:
        return f"FY{m_year.group(1)}"
    return s


def _determine_unit_and_scale(
    currency_tok: str | None,
    scale_tok: str | None,
    unit_tok: str | None,
    context_text: str = "",
) -> tuple[str | None, str, float]:
    """Return ``(currency, canonical_unit, scale_multiplier)``."""
    cur_raw = (currency_tok or "").strip().lower()
    unit_raw = (unit_tok or "").strip().lower()
    scale_raw = (scale_tok or "").strip().lower()

    currency = CURRENCY_MAP.get(cur_raw) or CURRENCY_MAP.get(unit_raw)
    if not currency:
        if "美元" in context_text or "usd" in context_text.lower() or "$" in context_text:
            currency = "USD"
        elif "港元" in context_text or "hkd" in context_text.lower():
            currency = "HKD"
        elif "人民币" in context_text or "cny" in context_text.lower() or "rmb" in context_text.lower():
            currency = "CNY"

    scale = SCALE_MULTIPLIERS.get(scale_raw, 1.0)

    if unit_raw in ("%", "pp", "pts"):
        return None, "pct", 1.0
    if unit_raw in ("bp", "bps"):
        return None, "bp", 1.0
    if unit_raw in ("x", "倍"):
        return None, "x", 1.0

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
    for clause in reversed(clauses):
        cand = clause.strip()
        cand = re.sub(
            r"^(?:其中|以及|而|且|但|约|达到|为|至|录得|实现|高于|低于|维持|预计|我们的|公司)\s*",
            "",
            cand,
        ).strip()
        if len(cand) >= 2:
            return cand[-32:]
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

    # Track Markdown table state
    table_headers: list[str] | None = None
    table_shared_footnote: str | None = None

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
            table_headers = None
            table_shared_footnote = None
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

                for col_idx in range(1, len(cells)):
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
                    fn_target = footnotes.get(fn_id) if fn_id else None

                    # Determine period and metric from row_header / col_header
                    p_col = _PERIOD_TOKEN_RE.search(col_header)
                    p_row = _PERIOD_TOKEN_RE.search(row_header)
                    if p_col:
                        period = normalize_period(p_col.group(1))
                        metric = row_header
                    elif p_row:
                        period = normalize_period(p_row.group(1))
                        metric = f"{row_header} {col_header}".strip()
                    else:
                        period = current_section_period
                        metric = f"{row_header} ({col_header})".strip(" ()")

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
                            context_text=f"{row_header} {col_header}",
                        )
                        label_hint = _infer_label_hint(
                            f"{row_header} {col_header} {cell_text}"
                        )
                        is_exempt = bool(
                            _EXEMPT_MARKER_RE.search(cell_text)
                            or _EXEMPT_MARKER_RE.search(row_header)
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
                                    "row": row_header,
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
            fn_target = footnotes.get(fn_id) if fn_id else None

            metric = _extract_metric_from_context(scan_line, nm.start(), nm.end())
            currency, unit, scale = _determine_unit_and_scale(
                nm.group("currency"),
                nm.group("scale"),
                nm.group("unit"),
                context_text=scan_line[max(0, nm.start() - 25) : nm.end() + 15],
            )
            label_hint = _infer_label_hint(scan_line)

            is_exempt = is_blockquote or bool(_EXEMPT_MARKER_RE.search(raw_line))
            exempt_reason = (
                "blockquote_quote"
                if is_blockquote
                else ("illustrative" if is_exempt else None)
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
                        "table": False,
                        "row": None,
                        "col": None,
                        "sentence": stripped,
                    },
                    exempt=is_exempt,
                    exempt_reason=exempt_reason,
                )
            )

        i += 1

    return figures
