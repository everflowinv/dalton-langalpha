"""Daily filing card builder.

Pure function: structured payload in, Card Schema 2.0 dict out. Field order,
key/routine split, and amount totals match the historical filing card. This
module does not read credentials or send anything. Markdown element splitting
stays in the bull/bear renderer; this layout is the filing-specific card.

Threat model: defends against model mistakes, hallucinations, or amount mixing
in the untrusted ``payload`` JSON (model-written ``text``, ``summary``, ``lead``,
``title``, ``tag``, numeric fields, and forged ``_``-prefixed internal fields).
It does NOT defend against an attacker forging the script-generated ``scan_data``
file itself, though render-time self-consistency and identity-matching checks
still reject corrupted or mismatched scan records.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any
import unicodedata

DEFAULT_ROUTINE_THRESHOLD_USD = 10_000_000

TAG_RULES: list[tuple[str, str, int]] = [
    (r"^(10-K|10-Q|20-F|年报|半年报|季报|业绩|财报)", "blue", 10),
    (r"^(8-K|6-K)", "orange", 20),
    (r"^(13[DG]|SC 13|权益变动|举牌|增持|Form\s*4.*买入)", "purple", 30),
    (r"^(S-1|S-3|424|招股|配售|增发|回购|回購|股份购回|股份購回|购回|購回|买回|買回|翌日披露|buy[- ]?backs?|share[- ]?repurchas|repurchas|next\s+day\s+disclosure)", "green", 40),
    (r"^(A股公告|港股公告|公告|HKEX)", "green", 50),
    (r"^(Form 144|144)", "red", 60),
    (r"^(Form 4|4)$|^Form 4", "grey", 70),
]
DEFAULT_TAG_COLOR = "grey"
DEFAULT_TAG_WEIGHT = 55
SUBSTANTIVE_WEIGHT_MAX = 50

MARKET_SHORT = {"us": "美股", "cn": "A股", "hk": "港股", "cnhk": "A股/港股"}
MARKET_ORDER = ["us", "cn", "hk", "cnhk", ""]

AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(亿|万)?\s*美元")
_MULTI_CCY_RE = re.compile(
    r"([\d][\d,]*(?:\.\d+)?)\s*(亿|万)?\s*(美元|港元|元人民币|人民币|HKD|RMB|USD)",
    re.IGNORECASE,
)
_CCY_USD_RATE = {
    "美元": 1.0,
    "USD": 1.0,
    "元人民币": 1.0 / 7.1,
    "人民币": 1.0 / 7.1,
    "RMB": 1.0 / 7.1,
    "港元": 1.0 / 7.8,
    "HKD": 1.0 / 7.8,
}
_CCY_UNIT_CN = {
    "美元": "美元",
    "USD": "美元",
    "元人民币": "元人民币",
    "人民币": "元人民币",
    "RMB": "元人民币",
    "港元": "港元",
    "HKD": "港元",
}
MAX_LEAD_CHARS = 40
_FORM4_BUY_TAG_RE = re.compile(
    r"^(?:权益变动[（(]Form\s*4\s*买入[）)]|Form\s*4.*买入)",
    re.IGNORECASE,
)
_STAKE_INCREASE_TAG_RE = re.compile(r"^增持$", re.IGNORECASE)
_NDR_RE = re.compile(r"(?:翌日披露|next\s+day\s+disclosure)", re.IGNORECASE)
_BUYBACK_TAG_RE = re.compile(
    r"^(?:回购|回購|股份购回|股份購回|购回|購回|买回|買回|buy[- ]?backs?|share[- ]?repurchas\w*|repurchas\w*)",
    re.IGNORECASE,
)
_BUYBACK_TEXT_RE = re.compile(
    r"(?:回购|回購|购回|購回|股份购回|股份購回|买回|買回|"
    r"(?:购入|購入)[^\n。；;]*?(?:股份|股票|H股|A股|普通股)|"
    r"\b(?:share[- ]?)?repurchas\w*|\bbuy[- ]?backs?\b|\bbought\s+back\b)",
    re.IGNORECASE,
)
_BUYBACK_NEG_RE = re.compile(r"(?:非回购|不回购|未回购|非購回|不購回|未購回)")
_DEFAULT_SHARE_CLASSES = {"", "不適用", "不适用", "普通股"}
_STAKE_INCREASE_RE = re.compile(r"增持")
_STAKE_NEG_RE = re.compile(r"(?:不增持|未增持)")
_REDUCTION_TEXT_RE = re.compile(r"(?:减持|減持|出售|卖出|套现)")
_US_SEC_TAG_RE = re.compile(
    r"^(?:Form\s*4|Form\s*144|144|4(?:/A)?|8-K|6-K|10-K|10-Q|20-F|S-1|S-3|F-1|424|DEF\s*14A|DEFA14A)\b",
    re.IGNORECASE,
)
_CN_HK_MARKETS = {"cn", "hk", "cnhk"}
_AMENDMENT_RE = re.compile(r"(?:更正|修訂|修订|補充|补充)")


def tag_style(tag: str) -> tuple[str, int]:
    """Return ``(color, sort weight)`` for a form or announcement tag."""
    name = (tag or "").strip()
    for pattern, color, weight in TAG_RULES:
        if re.match(pattern, name, flags=re.IGNORECASE):
            return color, weight
    return DEFAULT_TAG_COLOR, DEFAULT_TAG_WEIGHT


def short_date(value: str) -> str:
    """2026-08-27 → 08-27. Other text is returned unchanged."""
    matched = re.match(r"^\d{4}-(\d{2}-\d{2})$", (value or "").strip())
    return matched.group(1) if matched else (value or "").strip()


def parse_amount_usd(text: str) -> float:
    """First 「约 X 万/亿美元」 in ``text``, in dollars. Missing amounts are 0."""
    matched = AMOUNT_RE.search(text or "")
    if not matched:
        return 0.0
    try:
        base = float(matched.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    unit = matched.group(2)
    if unit == "亿":
        return base * 100_000_000
    if unit == "万":
        return base * 10_000
    return base


def format_amount_usd(value: float) -> str:
    """7110000 → 711 万美元. 591700000 → 5.92 亿美元."""
    if value <= 0:
        return ""
    if value >= 100_000_000:
        yi = f"{value / 100_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{yi} 亿美元"
    wan = value / 10_000
    if wan >= 100:
        return f"{wan:,.0f} 万美元"
    return f"{wan:,.1f} 万美元"


def company_amount_usd(company: dict[str, Any]) -> float:
    """Explicit ``amount_usd`` wins. Otherwise sum non-buyback USD item amounts."""
    if company.get("_buyback_related"):
        return 0.0
    total = 0.0
    for item in company.get("items", []) or []:
        if is_buyback_item(item):
            continue
        explicit = item.get("amount_usd")
        if isinstance(explicit, (int, float)) and explicit > 0:
            total += float(explicit)
        else:
            total += parse_amount_usd(str(item.get("text", "")))
    return total


def _tx_tokens(item: dict[str, Any]) -> set[str]:
    raw = str(item.get("tx_type", "")).strip().upper()
    if not raw:
        return set()
    return {tok.strip() for tok in re.split(r"[、,;/\s]+", raw) if tok.strip()}


def is_form4_purchase_item(item: dict[str, Any]) -> bool:
    """True for structured Form 4 open-market purchase items (no text guessing)."""
    tx_tokens = _tx_tokens(item)
    code = str(item.get("code", "")).strip().upper()
    if "PURCHASE" in tx_tokens or code == "P":
        return True
    if tx_tokens or code:
        return False
    if item.get("is_purchase"):
        return True
    tag = str(item.get("tag", "")).strip()
    return bool(_FORM4_BUY_TAG_RE.match(tag))


def is_stake_increase_item(item: dict[str, Any], *, market: str = "") -> bool:
    """True for A/HK stake-increase disclosures (excludes reductions and Form 4/known tx types)."""
    if is_form4_purchase_item(item):
        return False
    tx_tokens = _tx_tokens(item)
    code = str(item.get("code", "")).strip().upper()
    if tx_tokens or code:
        return False
    item_market = str(item.get("market", "")).strip().lower() or market.strip().lower()
    if item_market and item_market not in _CN_HK_MARKETS:
        return False
    tag = str(item.get("tag", "")).strip()
    if _US_SEC_TAG_RE.match(tag):
        return False
    if item.get("is_stake_increase"):
        return True
    text = str(item.get("text", "")).strip()
    if _STAKE_NEG_RE.search(text) or _REDUCTION_TEXT_RE.search(text):
        return False
    if _STAKE_INCREASE_TAG_RE.match(tag):
        return True
    return bool(_STAKE_INCREASE_RE.search(text))


def is_purchase_item(item: dict[str, Any], *, market: str = "") -> bool:
    """True for Form 4 open-market purchases or A/HK stake-increase items."""
    return is_form4_purchase_item(item) or is_stake_increase_item(item, market=market)


_UNVERIFIED_BUYBACK_SUMMARY = "发布回购公告，金额未能从原文核对"
_GENERIC_UNVERIFIED_TITLE = "回购相关公告"
_GOVERNED_SAFE_TAGS = frozenset({
    "回购",
    "回購",
    "股份购回",
    "股份購回",
    "购回",
    "購回",
    "翌日披露",
    "A股公告",
    "港股公告",
    "公告",
})
_STRICT_INT_RE = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)$")
_STRICT_NUM_RE = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
_CLAUSE_BUYBACK_NEG_RE = re.compile(
    r"(?:非|不|未|无|無|没有|沒有)(?:涉及)?(?:股份)?(?:回购|回購|购回|購回)"
)
_MODEL_BUYBACK_ONLY_KEYS = frozenset({
    "buyback_amount",
    "buyback_amount_raw",
    "groups",
    "trades",
    "buyback",
    "ndr",
})


def _strip_internal_fields(value: Any) -> Any:
    """Recursively strip all ``_``-prefixed internal fields from untrusted input payload."""
    if isinstance(value, dict):
        return {
            k: _strip_internal_fields(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


def _is_ws_or_cf(ch: str) -> bool:
    return bool(ch) and (ch.isspace() or unicodedata.category(ch) == "Cf")


def _strict_parse_dec(
    val: Any,
    *,
    allow_decimal: bool = True,
    allow_zero: bool = False,
) -> Decimal | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        if not math.isfinite(float(val)):
            return None
        try:
            dec = Decimal(str(val))
        except (InvalidOperation, ValueError):
            return None
        if not allow_decimal and dec != dec.to_integral_value():
            return None
        if allow_zero:
            return dec if dec >= 0 else None
        return dec if dec > 0 else None
    if isinstance(val, str):
        clean = val.strip(" ")
        if not clean or any(_is_ws_or_cf(ch) for ch in clean):
            return None
        pattern = _STRICT_NUM_RE if allow_decimal else _STRICT_INT_RE
        if not pattern.fullmatch(clean):
            return None
        try:
            dec = Decimal(clean.replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        if allow_zero:
            return dec if dec >= 0 else None
        return dec if dec > 0 else None
    return None


_PLACEHOLDER_STOCK_CODES = frozenset({
    "不適用",
    "不适用",
    "說明",
    "说明",
    "無",
    "无",
    "N/A",
    "NA",
    "NIL",
    "NONE",
    "-",
    "--",
    "—",
    "/",
})


def _norm_stock_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if not raw or raw in _PLACEHOLDER_STOCK_CODES:
        return ""
    if raw.endswith((".HK", ".SZ", ".SH")):
        raw = raw[:-3]
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    return raw


def _scan_stock_code_conflict(company_code: str, entry: dict[str, Any]) -> str:
    comp_norm = _norm_stock_code(company_code)
    if not comp_norm or not isinstance(entry, dict):
        return ""
    candidates: list[Any] = [entry.get("stock_code")]
    for field in ("groups", "classes"):
        rows = entry.get(field)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    candidates.append(row.get("stock_code"))
    ndr_obj = entry.get("ndr")
    if isinstance(ndr_obj, dict):
        for field in ("buybacks", "classes"):
            rows = ndr_obj.get(field)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        candidates.append(row.get("stock_code"))
    bb_obj = entry.get("buyback")
    if isinstance(bb_obj, dict):
        candidates.append(bb_obj.get("stock_code"))
        if isinstance(bb_obj.get("groups"), list):
            for grp in bb_obj["groups"]:
                if isinstance(grp, dict):
                    candidates.append(grp.get("stock_code"))
    for raw_sc in candidates:
        sc_norm = _norm_stock_code(raw_sc)
        if sc_norm and sc_norm != comp_norm:
            return f"扫描记录证券代号（{str(raw_sc).strip()}）与条目公司（{company_code or '空'}）不一致"
    return ""


def _item_parse_error(item: dict[str, Any]) -> str:
    err = str(item.get("enrich_error") or item.get("error") or "").strip()
    if err:
        return err
    bb = item.get("buyback")
    if isinstance(bb, dict):
        return str(bb.get("enrich_error") or bb.get("error") or "").strip()
    return ""


_STRICT_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CN_DIGITS = (
    "〇零一二三四五六七八九十百千万亿两"
    "壹贰叁肆伍陆柒捌玖拾佰仟萬億兩貳參叄貮陸"
    "廿卅卌皕卄丗弌弍弎叅"
    "〡〢〣〤〥〦〧〨〩〸〹〺"
    "㈠㈡㈢㈣㈤㈥㈦㈧㈨㈩㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉"
)
_STRIP_MODEL_DIGITS_RE = re.compile(
    rf"(?:[0-9０-９]+(?:[,，.．][0-9０-９]+)*|[0-9０-９{_CN_DIGITS}])"
)
_CODE_FOOTER = "数据源：SEC EDGAR · 巨潮公告 · HKEXnews"


def _strict_iso_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not _STRICT_ISO_DATE_RE.fullmatch(raw):
        return ""
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return ""
    return raw


_FILING_FORM_TAGS = frozenset({
    "8-K", "6-K", "10-K", "10-Q", "20-F", "40-F", "S-1", "S-3", "S-4", "F-1", "F-3",
    "SC 13D", "SC 13G", "13D", "13G", "13F", "Form 3", "Form 4", "Form 5", "Form 144", "144",
    "DEF 14A", "424B", "8-K/A", "10-K/A", "10-Q/A",
})


def _strip_model_digits(text: str) -> str:
    """Remove all Arabic, fullwidth, and Chinese numeral characters from model-authored text."""
    if not text:
        return ""
    cleaned = "".join(ch for ch in str(text) if unicodedata.category(ch) != "Cf")
    stripped = _STRIP_MODEL_DIGITS_RE.sub("", cleaned)
    return re.sub(r"[ \t]{2,}", " ", stripped).strip()


def _load_cn_hk_watchlist_names() -> dict[str, str]:
    path = Path(__file__).resolve().parent.parent / "watchlist_cn_hk.txt"
    if path.is_symlink() or not path.is_file():
        return {}
    names: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        code_norm = _norm_stock_code(re.sub(r"\.hk$", "", parts[0].strip(), flags=re.IGNORECASE))
        comp_name = parts[1].strip()
        if code_norm and comp_name:
            names.setdefault(code_norm, comp_name)
    return names


def _has_affirmative_buyback_mention(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    for matched in _BUYBACK_TEXT_RE.finditer(raw):
        prefix = raw[max(0, matched.start() - 12) : matched.start()]
        if not re.search(
            r"(?:(?:非|不|未|无|無|没有|沒有)(?:涉及)?(?:股份)?|\b(?:no|not|non|without|never)(?:\s+share)?[\s-]*)$",
            prefix,
            flags=re.IGNORECASE,
        ):
            return True
    return False


def _has_buyback_amount_text(text: str) -> bool:
    """True when ``text`` pairs non-negated buyback wording with Arabic or Chinese numerals."""
    raw = str(text or "").strip()
    if not raw or not re.search(rf"[\d０-９{_CN_DIGITS}]", raw):
        return False
    return _has_affirmative_buyback_mention(raw)


def _is_scan_ann_buyback_or_ndr(ann: dict[str, Any]) -> bool:
    ann_title = str(ann.get("title") or "").strip()
    ann_type = str(ann.get("type") or "").strip()
    return bool(
        _BUYBACK_TAG_RE.match(ann_type)
        or _BUYBACK_TEXT_RE.search(ann_type)
        or _BUYBACK_TEXT_RE.search(ann_title)
        or _NDR_RE.search(ann_type)
        or _NDR_RE.search(ann_title)
        or "ndr" in ann
        or "buyback" in ann
    )


def _extract_scan_company_meta(
    scan_data: dict[str, Any] | None,
) -> tuple[dict[str, str], set[str], set[str], set[str]]:
    """Return ``(scan_names, scan_buyback_codes, scan_amendment_codes, confirmed_non_bb_tokens)``."""
    scan_names: dict[str, str] = {}
    scan_buyback_codes: set[str] = set()
    scan_amendment_codes: set[str] = set()
    confirmed_non_bb_tokens: set[str] = set()
    if not isinstance(scan_data, dict):
        return scan_names, scan_buyback_codes, scan_amendment_codes, confirmed_non_bb_tokens

    token_to_code: dict[str, str] = {}
    res_list = scan_data.get("results")
    if isinstance(res_list, list):
        for res_row in res_list:
            if not isinstance(res_row, dict):
                continue
            row_code = _norm_stock_code(res_row.get("ticker"))
            row_name = str(res_row.get("name") or "").strip()
            if row_code and row_name:
                scan_names.setdefault(row_code, row_name)
            for ann in res_row.get("announcements") or []:
                if not isinstance(ann, dict):
                    continue
                ann_code = _norm_stock_code(ann.get("code")) or row_code
                ann_name = str(ann.get("name") or "").strip()
                if ann_code and ann_name:
                    scan_names.setdefault(ann_code, ann_name)
                if ann_code:
                    for candidate in (ann.get("id"), ann.get("url"), ann.get("pdf_url")):
                        tok = str(candidate or "").strip()
                        if tok:
                            token_to_code.setdefault(tok, ann_code)

    raw_bb = scan_data.get("buyback_results")
    if isinstance(raw_bb, dict):
        for key, entry in raw_bb.items():
            if not isinstance(entry, dict):
                continue
            tokens = [
                str(cand or "").strip()
                for cand in (key, entry.get("id"), entry.get("url"), entry.get("pdf_url"))
                if str(cand or "").strip()
            ]
            code_norm = _norm_stock_code(entry.get("code"))
            if not code_norm:
                code_norm = next((token_to_code[t] for t in tokens if t in token_to_code), "")
            is_confirmed_non_bb = (
                entry.get("validated") is True
                and entry.get("is_buyback") is False
                and not _item_parse_error(entry)
                and not (code_norm and _scan_stock_code_conflict(code_norm, entry))
            )
            if is_confirmed_non_bb:
                confirmed_non_bb_tokens.update(tokens)
            # Any NDR or buyback entry (confirmed non-buyback or not) marks the company as NDR/buyback-related.
            if code_norm:
                scan_buyback_codes.add(code_norm)
            if entry.get("is_amendment") is True or _AMENDMENT_RE.search(str(entry.get("title") or "")):
                if code_norm and not _scan_stock_code_conflict(code_norm, entry):
                    scan_amendment_codes.add(code_norm)

    if isinstance(res_list, list):
        for res_row in res_list:
            if not isinstance(res_row, dict):
                continue
            row_code = _norm_stock_code(res_row.get("ticker"))
            for err_msg in res_row.get("errors") or []:
                if "存在更正回购公告" in str(err_msg) and row_code:
                    scan_amendment_codes.add(row_code)
                    scan_buyback_codes.add(row_code)
            for ann in res_row.get("announcements") or []:
                if not isinstance(ann, dict):
                    continue
                ann_code = _norm_stock_code(ann.get("code")) or row_code
                ann_tokens = [
                    str(cand or "").strip()
                    for cand in (ann.get("id"), ann.get("url"), ann.get("pdf_url"))
                    if str(cand or "").strip()
                ]
                is_bb_or_ndr = _is_scan_ann_buyback_or_ndr(ann)
                if is_bb_or_ndr and ann_code:
                    scan_buyback_codes.add(ann_code)
                if (
                    is_bb_or_ndr or any(t in confirmed_non_bb_tokens for t in ann_tokens)
                ) and (ann.get("is_amendment") is True or _AMENDMENT_RE.search(str(ann.get("title") or ""))):
                    if ann_code:
                        scan_amendment_codes.add(ann_code)

    return scan_names, scan_buyback_codes, scan_amendment_codes, confirmed_non_bb_tokens


def _item_has_model_buyback_signal(
    item: dict[str, Any],
    *,
    include_ndr: bool = True,
) -> bool:
    title = str(item.get("title") or "").strip()
    tag = str(item.get("tag") or "").strip()
    text = str(item.get("text") or "").strip()
    if include_ndr and (_NDR_RE.search(title) or _NDR_RE.search(tag) or _NDR_RE.search(text)):
        return True
    if _BUYBACK_TAG_RE.match(tag) or _BUYBACK_TEXT_RE.search(tag) or _BUYBACK_TEXT_RE.search(title):
        return True
    if _BUYBACK_TEXT_RE.search(text):
        return True
    return _has_model_buyback_only_keys(item)


def _build_scan_indexes(
    scan_data: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(scan_data, dict):
        return {}, {}
    buyback_index: dict[str, dict[str, Any]] = {}
    raw_results = scan_data.get("buyback_results")
    if isinstance(raw_results, dict):
        for key, entry in raw_results.items():
            if not isinstance(entry, dict):
                continue
            for candidate in (key, entry.get("id"), entry.get("url"), entry.get("pdf_url")):
                token = str(candidate or "").strip()
                if token:
                    buyback_index[token] = entry

    ann_index: dict[str, dict[str, Any]] = {}
    res_list = scan_data.get("results")
    if isinstance(res_list, list):
        for res_row in res_list:
            if not isinstance(res_row, dict):
                continue
            for ann in res_row.get("announcements") or []:
                if not isinstance(ann, dict):
                    continue
                for candidate in (ann.get("id"), ann.get("url"), ann.get("pdf_url")):
                    token = str(candidate or "").strip()
                    if token:
                        ann_index[token] = ann
    return buyback_index, ann_index


def _build_scan_index(scan_data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    buyback_index, _ann_index = _build_scan_indexes(scan_data)
    return buyback_index


def _lookup_in_index(
    item: dict[str, Any],
    index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if not index:
        return None, ""
    for field in ("id", "announcement_id", "notice_id"):
        token = str(item.get(field) or "").strip()
        if token and token in index:
            return index[token], "id"
    for field in ("url", "pdf_url"):
        token = str(item.get(field) or "").strip()
        if token and token in index:
            return index[token], "url"
    return None, ""


def _check_scan_identity(
    item: dict[str, Any],
    company_code: str,
    entry: dict[str, Any],
    *,
    matched_by: str,
) -> str:
    comp_norm = _norm_stock_code(company_code)
    scan_norm = _norm_stock_code(entry.get("code"))
    if not scan_norm or not comp_norm or scan_norm != comp_norm:
        return f"扫描记录公司代码（{entry.get('code') or '空'}）与条目公司（{company_code or '空'}）不一致"

    sc_conflict = _scan_stock_code_conflict(company_code, entry)
    if sc_conflict:
        return sc_conflict

    item_url = str(item.get("url") or item.get("pdf_url") or "").strip()
    scan_url = str(entry.get("url") or entry.get("pdf_url") or "").strip()
    if item_url and item_url != scan_url:
        return "扫描记录链接与条目链接不一致"

    if matched_by == "url":
        item_id = str(item.get("id") or item.get("announcement_id") or item.get("notice_id") or "").strip()
        scan_id = str(entry.get("id") or "").strip()
        if item_id and scan_id and item_id != scan_id:
            return "扫描记录 ID 与条目 ID 不一致"
    return ""


def _match_scan_record(
    item: dict[str, Any],
    company_code: str,
    buyback_index: dict[str, dict[str, Any]],
    ann_index: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, str]:
    entry, matched_by = _lookup_in_index(item, buyback_index)
    if entry is not None:
        conflict = _check_scan_identity(item, company_code, entry, matched_by=matched_by)
        if conflict:
            return "conflict", entry, conflict
        return "buyback", entry, ""
    ann, ann_by = _lookup_in_index(item, ann_index)
    if ann is not None:
        conflict = _check_scan_identity(item, company_code, ann, matched_by=ann_by)
        if conflict:
            return "conflict", ann, conflict
        return "announcement", ann, ""
    return "none", None, ""


def _has_model_buyback_only_keys(item: dict[str, Any]) -> bool:
    if item.get("is_buyback") is True:
        return True
    return any(key in item and item.get(key) not in (None, "", [], {}) for key in _MODEL_BUYBACK_ONLY_KEYS)


def _is_buyback_or_ndr_governed(
    item: dict[str, Any],
    scan_entry: dict[str, Any] | None = None,
) -> bool:
    if scan_entry is not None:
        return True
    title = str(item.get("title") or "").strip()
    tag = str(item.get("tag") or "").strip()
    text = str(item.get("text") or "").strip()
    if _NDR_RE.search(title) or _NDR_RE.search(tag) or _NDR_RE.search(text):
        return True
    if _BUYBACK_TAG_RE.match(tag) or _BUYBACK_TEXT_RE.search(tag) or _BUYBACK_TEXT_RE.search(title):
        return True
    # No scan record: any buyback keyword in text triggers unverified handling (no negation bypass).
    if _BUYBACK_TEXT_RE.search(text):
        return True
    return _has_model_buyback_only_keys(item)


def is_buyback_item(item: dict[str, Any]) -> bool:
    """True for validated buybacks or unverified NDR/buyback items."""
    state = item.get("_buyback_state")
    if state in ("validated", "unverified"):
        return True
    if item.get("_resolved_non_buyback_ndr"):
        return False
    return _is_buyback_or_ndr_governed(item, None)


def item_weight(item: dict[str, Any], *, market: str = "") -> int:
    _color, weight = tag_style(item.get("tag", ""))
    if is_purchase_item(item, market=market):
        weight = min(weight, 30)
    elif is_buyback_item(item):
        weight = min(weight, 40)
    return weight


def company_weight(company: dict[str, Any], *, market: str = "") -> int:
    eff_market = str(company.get("market", "")).strip().lower() or market
    weights = [item_weight(item, market=eff_market) for item in company.get("items", []) or []]
    return min(weights) if weights else DEFAULT_TAG_WEIGHT


def is_key_company(company: dict[str, Any], threshold_usd: float, *, market: str = "") -> bool:
    """Substantive filings, or sale amounts at or above the threshold, are key."""
    eff_market = str(company.get("market", "")).strip().lower() or market
    if company_weight(company, market=eff_market) <= SUBSTANTIVE_WEIGHT_MAX:
        return True
    if _is_buyback_company(company):
        return True
    return company_amount_usd(company) >= threshold_usd


def company_market(company: dict[str, Any], section_key: str) -> str:
    market = str(company.get("market", "")).strip().lower()
    return market or str(section_key or "").strip().lower()


def tag_badges(company: dict[str, Any]) -> str:
    """Form badges on the company line, ordered by weight. Repeats show ×N."""
    counts: dict[str, int] = {}
    for item in company.get("items", []) or []:
        tag = str(item.get("tag", "")).strip()
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda pair: (tag_style(pair[0])[1], pair[0]))
    parts = []
    for tag, count in ordered:
        color, _weight = tag_style(tag)
        label = f"{tag} ×{count}" if count > 1 else tag
        parts.append(f"<font color='{color}'>{label}</font>")
    return " ".join(parts)


def date_link(item: dict[str, Any]) -> str:
    """The date is the source link. Without a url it is a grey date."""
    date = short_date(str(item.get("date", "")))
    url = str(item.get("url", "")).strip()
    if not date:
        return f"[原文]({url})" if url else ""
    return f"[{date}]({url})" if url else f"<font color='grey'>{date}</font>"


def render_company(company: dict[str, Any], market: str = "", show_market: bool = False) -> str:
    """Key-tier block: title line, summary when there are multiple items, grey lines."""
    code = str(company.get("code", "")).strip()
    name = str(company.get("name", "")).strip()
    head_bits = [f"**{code}**" if code else ""]
    meta = []
    if show_market and MARKET_SHORT.get(market):
        meta.append(MARKET_SHORT[market])
    if name:
        meta.append(name)
    if meta:
        head_bits.append(f"<font color='grey'>{' · '.join(meta)}</font>")
    badges = tag_badges(company)
    if badges:
        head_bits.append(badges)
    amount = format_amount_usd(company_amount_usd(company))
    if amount:
        head_bits.append(f"<font color='grey'>合计 {amount}</font>")
    lines = [" ".join(bit for bit in head_bits if bit)]

    items = [item for item in (company.get("items", []) or []) if str(item.get("text", "")).strip()]
    summary = str(company.get("summary", "")).strip()
    if summary and (len(items) > 1 or not items):
        lines.append(summary)

    for item in items:
        text = str(item.get("text", "")).strip()
        trailing = date_link(item)
        line = f"<font color='grey'>· {text}</font>"
        if trailing:
            line += f"  {trailing}"
        lines.append(line)
    return "\n".join(lines)


def render_routine_company(company: dict[str, Any], market: str = "", show_market: bool = False) -> str:
    """Routine-tier company compressed to one line. Summary wins over the first item."""
    code = str(company.get("code", "")).strip()
    name = str(company.get("name", "")).strip()
    items = [item for item in (company.get("items", []) or []) if str(item.get("text", "")).strip()]
    summary = str(company.get("summary", "")).strip()
    if not summary and items:
        summary = str(items[0].get("text", "")).strip()

    head = f"**{code}**" if code else ""
    market_label = MARKET_SHORT.get(market, "") if show_market else ""
    meta = [bit for bit in (market_label, name) if bit]
    body_bits = []
    if meta:
        body_bits.append(" · ".join(meta))
    if summary:
        body_bits.append(summary)
    line = head
    if body_bits:
        line += f" <font color='grey'>{' · '.join(body_bits)}</font>"
    links = [link for link in (date_link(item) for item in items) if link]
    if links:
        line += "  " + " ".join(links[:4])
    return line.strip()


def total_scanned(stats: dict[str, Any]) -> int:
    """Sum of ``scanned`` across markets. The card shows only this total."""
    total = 0
    for entry in (stats or {}).values():
        if not isinstance(entry, dict):
            continue
        scanned = entry.get("scanned")
        if isinstance(scanned, (int, float)) and scanned > 0:
            total += int(scanned)
    return total


def label_element(text: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "text_size": "notation",
        "content": f"<font color='grey'>{text}</font>",
    }


def collapsible(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "vertical_align": "center",
            "padding": "4px 8px 4px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "12px 12px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "6px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def _is_amendment_governed_item(item: dict[str, Any]) -> bool:
    """True for any governed NDR/buyback item (including confirmed non-buyback NDR) marked as amendment."""
    if not (item.get("_buyback_state") or item.get("_resolved_non_buyback_ndr")):
        return False
    if item.get("_is_amendment"):
        return True
    title = str(item.get("title") or "").strip()
    return bool(title and _AMENDMENT_RE.search(title))


def _has_validated_buyback(item: dict[str, Any]) -> bool:
    return item.get("_buyback_state") == "validated"


def _extract_scan_groups(scan_entry: dict[str, Any]) -> list[dict[str, Any]]:
    if "groups" in scan_entry and scan_entry.get("groups") is not None:
        g = scan_entry.get("groups")
        if not isinstance(g, list) or not g:
            return []
        if not all(isinstance(row, dict) for row in g):
            return []
        return list(g)
    if isinstance(scan_entry.get("trades"), list) and scan_entry.get("trades"):
        return [scan_entry]
    return []


def _validate_scan_group(grp: Any) -> tuple[bool, str]:
    if not isinstance(grp, dict):
        return False, "分组元素结构损坏"
    cur = str(grp.get("currency") or "").strip()
    bcur = str(grp.get("buyback_currency") or "").strip()
    if cur and bcur and _norm_ccy_unit(cur) != _norm_ccy_unit(bcur):
        return False, "分组币种冲突"
    eff_cur = cur or bcur
    norm_unit = _norm_ccy_unit(eff_cur)
    if not eff_cur or norm_unit not in ("港元", "元人民币", "美元"):
        return False, "分组缺少有效币种"

    amt_val = grp.get("total_amount") if "total_amount" in grp else grp.get("buyback_amount")
    amt_raw = grp.get("total_amount_raw") if "total_amount_raw" in grp else grp.get("buyback_amount_raw")
    amt_num_dec = (
        _strict_parse_dec(amt_val, allow_decimal=True)
        if isinstance(amt_val, (int, float)) and not isinstance(amt_val, bool)
        else None
    )
    amt_raw_dec = _strict_parse_dec(amt_raw, allow_decimal=True) if isinstance(amt_raw, str) else None
    if amt_num_dec is None or amt_raw_dec is None:
        return False, "分组缺少有效合共金额或原始金额"
    if abs(amt_raw_dec - amt_num_dec) > Decimal("0.001"):
        return False, "total_amount_raw 与 total_amount 不一致"

    sh_val = grp.get("total_shares")
    sh_raw = grp.get("total_shares_raw")
    sh_num_dec = (
        _strict_parse_dec(sh_val, allow_decimal=False)
        if isinstance(sh_val, int) and not isinstance(sh_val, bool)
        else None
    )
    sh_raw_dec = _strict_parse_dec(sh_raw, allow_decimal=False) if isinstance(sh_raw, str) else None
    if sh_num_dec is None or sh_raw_dec is None:
        return False, "分组缺少有效合共股数或原始股数"
    if sh_num_dec != sh_raw_dec:
        return False, "total_shares_raw 与 total_shares 不一致"

    trades = grp.get("trades")
    if not isinstance(trades, list) or not trades:
        return False, "分组缺少交易明细列表"
    sum_trade_amt = Decimal(0)
    trade_shares_list: list[int | None] = []
    for trade in trades:
        if not isinstance(trade, dict):
            return False, "交易明细元素结构损坏"
        if not str(trade.get("date") or "").strip():
            return False, "交易明细缺少日期"
        t_cur = str(trade.get("currency") or "").strip()
        if t_cur and _norm_ccy_unit(t_cur) != norm_unit:
            return False, "交易明细币种与分组不一致"
        t_amt = trade.get("amount")
        t_amt_dec = (
            _strict_parse_dec(t_amt, allow_decimal=True)
            if isinstance(t_amt, (int, float)) and not isinstance(t_amt, bool)
            else None
        )
        if t_amt_dec is None:
            return False, "交易明细缺少有效金额"
        if trade.get("amount_raw") is not None:
            t_amt_raw_dec = (
                _strict_parse_dec(trade.get("amount_raw"), allow_decimal=True)
                if isinstance(trade.get("amount_raw"), str)
                else None
            )
            if t_amt_raw_dec is None or abs(t_amt_raw_dec - t_amt_dec) > Decimal("0.001"):
                return False, "交易明细 amount_raw 与 amount 不一致"
        sum_trade_amt += t_amt_dec
        t_sh = trade.get("shares")
        if t_sh is not None:
            t_sh_dec = (
                _strict_parse_dec(t_sh, allow_decimal=False)
                if isinstance(t_sh, int) and not isinstance(t_sh, bool)
                else None
            )
            if t_sh_dec is None:
                return False, "交易明细股数格式无效"
            if trade.get("shares_raw") is not None:
                t_sh_raw_dec = (
                    _strict_parse_dec(trade.get("shares_raw"), allow_decimal=False)
                    if isinstance(trade.get("shares_raw"), str)
                    else None
                )
                if t_sh_raw_dec is None or t_sh_raw_dec != t_sh_dec:
                    return False, "交易明细 shares_raw 与 shares 不一致"
            trade_shares_list.append(int(t_sh_dec))
        else:
            trade_shares_list.append(None)

    if any(sh is not None for sh in trade_shares_list):
        if any(sh is None for sh in trade_shares_list):
            return False, "部分交易明细缺少股数"
        sum_sh = sum(sh for sh in trade_shares_list if sh is not None)
        if Decimal(sum_sh) != sh_num_dec:
            return False, "交易行股数合计与合共股数不一致"

    if abs(sum_trade_amt - amt_raw_dec) > Decimal("0.02"):
        return False, "交易行金额合计与合共金额不一致"

    if grp.get("avg_price") is not None:
        avg_dec = (
            _strict_parse_dec(grp.get("avg_price"), allow_decimal=True)
            if isinstance(grp.get("avg_price"), (int, float)) and not isinstance(grp.get("avg_price"), bool)
            else None
        )
        if avg_dec is None or abs(avg_dec - (amt_raw_dec / sh_num_dec)) > Decimal("0.05"):
            return False, "分组均价与合共金额股数不一致"

    lo_dec: Decimal | None = None
    hi_dec: Decimal | None = None
    if grp.get("lo") is not None:
        lo_dec = _strict_parse_dec(grp.get("lo"), allow_decimal=True)
        if lo_dec is None:
            return False, "分组最低价格式无效"
    if grp.get("lo_raw") is not None:
        lo_raw_dec = (
            _strict_parse_dec(grp.get("lo_raw"), allow_decimal=True)
            if isinstance(grp.get("lo_raw"), str)
            else None
        )
        if lo_raw_dec is None or (lo_dec is not None and abs(lo_raw_dec - lo_dec) > Decimal("0.001")):
            return False, "分组 lo_raw 与 lo 不一致"
    if grp.get("hi") is not None:
        hi_dec = _strict_parse_dec(grp.get("hi"), allow_decimal=True)
        if hi_dec is None:
            return False, "分组最高价格式无效"
    if grp.get("hi_raw") is not None:
        hi_raw_dec = (
            _strict_parse_dec(grp.get("hi_raw"), allow_decimal=True)
            if isinstance(grp.get("hi_raw"), str)
            else None
        )
        if hi_raw_dec is None or (hi_dec is not None and abs(hi_raw_dec - hi_dec) > Decimal("0.001")):
            return False, "分组 hi_raw 与 hi 不一致"
    if lo_dec is not None and hi_dec is not None and lo_dec > hi_dec:
        return False, "分组价格区间上下限倒挂"

    to_cancel = grp.get("to_cancel")
    to_treasury = grp.get("to_treasury")
    cancel_dec = _strict_parse_dec(to_cancel, allow_decimal=False, allow_zero=True) if to_cancel is not None else None
    treasury_dec = _strict_parse_dec(to_treasury, allow_decimal=False, allow_zero=True) if to_treasury is not None else None
    if to_cancel is not None and cancel_dec is None:
        return False, "分组拟注销股数无效"
    if to_treasury is not None and treasury_dec is None:
        return False, "分组库存股数无效"
    if grp.get("to_cancel_raw") is not None:
        cancel_raw_dec = (
            _strict_parse_dec(grp.get("to_cancel_raw"), allow_decimal=False, allow_zero=True)
            if isinstance(grp.get("to_cancel_raw"), str)
            else None
        )
        if cancel_raw_dec is None or (cancel_dec is not None and cancel_raw_dec != cancel_dec):
            return False, "分组 to_cancel_raw 与 to_cancel 不一致"
    if grp.get("to_treasury_raw") is not None:
        treasury_raw_dec = (
            _strict_parse_dec(grp.get("to_treasury_raw"), allow_decimal=False, allow_zero=True)
            if isinstance(grp.get("to_treasury_raw"), str)
            else None
        )
        if treasury_raw_dec is None or (treasury_dec is not None and treasury_raw_dec != treasury_dec):
            return False, "分组 to_treasury_raw 与 to_treasury 不一致"
    c_val = int(cancel_dec or 0)
    t_val = int(treasury_dec or 0)
    if (c_val > 0 or t_val > 0) and Decimal(c_val + t_val) != sh_num_dec:
        return False, "分组拟注销与库存股之和与合共股数不一致"

    if grp.get("mandate_cum_shares_raw") is not None:
        cum_sh_raw = _strict_parse_dec(grp.get("mandate_cum_shares_raw"), allow_decimal=False, allow_zero=True)
        cum_sh_num = _strict_parse_dec(grp.get("mandate_cum_shares"), allow_decimal=False, allow_zero=True)
        if cum_sh_raw is None or (cum_sh_num is not None and cum_sh_raw != cum_sh_num):
            return False, "分组授权累计股数格式或数值不一致"
    if grp.get("mandate_cum_pct_raw") is not None:
        cum_pct_raw = _strict_parse_dec(grp.get("mandate_cum_pct_raw"), allow_decimal=True, allow_zero=True)
        cum_pct_num = _strict_parse_dec(grp.get("mandate_cum_pct"), allow_decimal=True, allow_zero=True)
        if cum_pct_raw is None or (cum_pct_num is not None and abs(cum_pct_raw - cum_pct_num) > Decimal("0.001")):
            return False, "分组授权累计占比格式或数值不一致"

    return True, ""


def _valid_scan_group(grp: dict[str, Any]) -> bool:
    ok, _reason = _validate_scan_group(grp)
    return ok


def _validate_scan_buyback_entry(scan_entry: dict[str, Any] | None) -> tuple[bool, str]:
    if not isinstance(scan_entry, dict):
        return False, "扫描记录格式无效"
    if scan_entry.get("validated") is not True or scan_entry.get("is_buyback") is not True:
        return False, _item_parse_error(scan_entry) or "扫描记录未通过校验"
    entry_code = str(scan_entry.get("code") or "").strip()
    if entry_code:
        sc_conflict = _scan_stock_code_conflict(entry_code, scan_entry)
        if sc_conflict:
            return False, sc_conflict
    err = _item_parse_error(scan_entry)
    if err:
        return False, err
    groups = _extract_scan_groups(scan_entry)
    if not groups:
        return False, "扫描记录缺少有效回购分组"
    for grp in groups:
        ok, reason = _validate_scan_group(grp)
        if not ok:
            return False, reason
    if len(groups) == 1:
        g0 = groups[0]
        if scan_entry.get("total_amount_raw") is not None:
            top_raw = _strict_parse_dec(scan_entry.get("total_amount_raw"), allow_decimal=True)
            g_raw = _strict_parse_dec(g0.get("total_amount_raw"), allow_decimal=True)
            if top_raw is None or g_raw is None or top_raw != g_raw:
                return False, "扫描记录顶层 total_amount_raw 与分组不一致"
        if scan_entry.get("total_amount") is not None:
            top_amt = _strict_parse_dec(scan_entry.get("total_amount"), allow_decimal=True)
            g_amt = _strict_parse_dec(g0.get("total_amount"), allow_decimal=True)
            if top_amt is None or g_amt is None or abs(top_amt - g_amt) > Decimal("0.001"):
                return False, "扫描记录顶层 total_amount 与分组不一致"
        if scan_entry.get("total_shares_raw") is not None:
            top_sh_raw = _strict_parse_dec(scan_entry.get("total_shares_raw"), allow_decimal=False)
            g_sh_raw = _strict_parse_dec(g0.get("total_shares_raw"), allow_decimal=False)
            if top_sh_raw is None or g_sh_raw is None or top_sh_raw != g_sh_raw:
                return False, "扫描记录顶层 total_shares_raw 与分组不一致"
        if scan_entry.get("total_shares") is not None:
            top_sh = _strict_parse_dec(scan_entry.get("total_shares"), allow_decimal=False)
            g_sh = _strict_parse_dec(g0.get("total_shares"), allow_decimal=False)
            if top_sh is None or g_sh is None or top_sh != g_sh:
                return False, "扫描记录顶层 total_shares 与分组不一致"
        top_cur = str(scan_entry.get("currency") or "").strip()
        g_cur = str(g0.get("currency") or "").strip()
        if top_cur and g_cur and _norm_ccy_unit(top_cur) != _norm_ccy_unit(g_cur):
            return False, "扫描记录顶层币种与分组不一致"
    return True, ""


def _is_validated_scan_buyback(scan_entry: dict[str, Any] | None) -> bool:
    ok, _reason = _validate_scan_buyback_entry(scan_entry)
    return ok


def _copy_structured_group(grp: dict[str, Any]) -> dict[str, Any]:
    """Copy only validated structured fields (never free-text ``line``/``text``)."""
    allowed = (
        "share_class",
        "stock_code",
        "trades",
        "total_shares",
        "total_shares_raw",
        "total_amount",
        "total_amount_raw",
        "buyback_amount",
        "buyback_amount_raw",
        "currency",
        "buyback_currency",
        "avg_price",
        "hi",
        "hi_raw",
        "lo",
        "lo_raw",
        "to_cancel",
        "to_cancel_raw",
        "to_treasury",
        "to_treasury_raw",
        "mandate_cum_shares",
        "mandate_cum_shares_raw",
        "mandate_cum_pct",
        "mandate_cum_pct_raw",
    )
    out = {k: grp[k] for k in allowed if k in grp}
    if isinstance(out.get("trades"), list):
        out["trades"] = [dict(t) for t in out["trades"] if isinstance(t, dict)]
    return out


def _format_num_str(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _group_amount_raw(grp: dict[str, Any], native: float) -> str:
    raw = str(grp.get("total_amount_raw") or grp.get("buyback_amount_raw") or "").strip()
    if raw and _parse_structured_amount(raw) is not None:
        return raw
    return _format_num_str(native)


def _group_shares_raw(grp: dict[str, Any]) -> str:
    raw = str(grp.get("total_shares_raw") or "").strip()
    if raw and _parse_structured_amount(raw) is not None:
        return raw
    sh_val = _parse_structured_amount(grp.get("total_shares"))
    if sh_val is not None:
        return f"{round(sh_val):,}"
    return ""


def _format_group_text(
    grp: dict[str, Any],
    *,
    multi_group: bool,
    fallback_date: str = "",
) -> str:
    metrics = _group_metrics(grp)
    if metrics is None:
        return ""
    _score, native, unit = metrics
    amt_raw = _group_amount_raw(grp, native)
    sh_raw = _group_shares_raw(grp)

    trades = grp.get("trades")
    days = ""
    if isinstance(trades, list) and trades:
        day_list = list(dict.fromkeys(
            short_date(str(t.get("date") or "").strip())
            for t in trades
            if isinstance(t, dict) and str(t.get("date") or "").strip()
        ))
        days = "、".join(d for d in day_list if d)
    if not days and fallback_date:
        days = short_date(fallback_date)

    prefix = ""
    if multi_group:
        sc = _format_share_class_label(grp.get("share_class"))
        prefix = f"{sc}：" if sc else f"{unit}："

    action = f"回购 {sh_raw} 股" if sh_raw else "回购股份"
    head = f"{prefix}{days} {action}".strip() if days else f"{prefix}{action}"
    line = f"{head}，金额 {amt_raw} {unit}"

    avg = grp.get("avg_price")
    if not isinstance(avg, (int, float)) or avg <= 0:
        sh_num = _parse_structured_amount(grp.get("total_shares")) or _parse_structured_amount(sh_raw)
        if sh_num and sh_num > 0:
            avg = round(native / sh_num, 2)
    if isinstance(avg, (int, float)) and avg > 0:
        line += f"，均价 {avg:.2f} {unit}"
        lo = grp.get("lo")
        hi = grp.get("hi")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > 0 and hi > 0:
            lo_s = str(grp.get("lo_raw") or f"{lo:g}")
            hi_s = str(grp.get("hi_raw") or f"{hi:g}")
            line += f"（区间 {lo_s}–{hi_s}）"

    to_cancel = int(grp.get("to_cancel") or 0) if isinstance(grp.get("to_cancel"), (int, float)) else 0
    to_treasury = int(grp.get("to_treasury") or 0) if isinstance(grp.get("to_treasury"), (int, float)) else 0
    cancel_s = str(grp.get("to_cancel_raw") or f"{to_cancel:,}")
    treasury_s = str(grp.get("to_treasury_raw") or f"{to_treasury:,}")
    if to_cancel > 0 and to_treasury > 0:
        line += f"，{cancel_s} 股拟注销，{treasury_s} 股持作库存股"
    elif to_cancel > 0:
        line += "，拟注销"
    elif to_treasury > 0:
        line += f"，{treasury_s} 股持作库存股"

    cum_sh = grp.get("mandate_cum_shares")
    if isinstance(cum_sh, (int, float)) and cum_sh > 0:
        cum_s = str(grp.get("mandate_cum_shares_raw") or f"{int(cum_sh):,}")
        line += f"；本次授权累计回购 {cum_s} 股"
        cum_pct = grp.get("mandate_cum_pct")
        if isinstance(cum_pct, (int, float)):
            pct_s = str(grp.get("mandate_cum_pct_raw") or f"{cum_pct:.2f}")
            line += f"（占股本 {pct_s}%）"
    return line


def _format_validated_buyback_text(bb_data: dict[str, Any], *, fallback_date: str = "") -> str:
    groups = _extract_scan_groups(bb_data)
    if groups:
        multi = len(groups) > 1
        parts = [_format_group_text(g, multi_group=multi, fallback_date=fallback_date) for g in groups]
        parts = [p for p in parts if p]
        if parts:
            return "；".join(parts)
    return str(bb_data.get("title") or "").strip() or "回购股份"


def _unverified_item_text(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip() or _GENERIC_UNVERIFIED_TITLE
    return f"{title}（金额未能从原文核对）"


def _sanitize_governed_tag(raw_tag: Any, *, is_buyback: bool, market: str = "") -> str:
    tag = str(raw_tag or "").strip()
    if tag in _GOVERNED_SAFE_TAGS:
        return tag
    if is_buyback:
        return "回购"
    if market == "cn":
        return "A股公告"
    if market == "hk":
        return "港股公告"
    return "公告"


def _resolve_governed_item(
    item: dict[str, Any],
    buyback_index: dict[str, dict[str, Any]],
    ann_index: dict[str, dict[str, Any]] | None = None,
    *,
    company_code: str = "",
    market: str = "",
) -> dict[str, Any]:
    source_kind, scan_entry, conflict_reason = _match_scan_record(
        item,
        company_code,
        buyback_index,
        ann_index or {},
    )

    if source_kind == "conflict":
        base_conflict: dict[str, Any] = {
            "tag": _sanitize_governed_tag(item.get("tag"), is_buyback=True, market=market),
            "title": _GENERIC_UNVERIFIED_TITLE,
            "date": _strict_iso_date(item.get("date")),
            "url": "",
        }
        for extra_key in ("id", "market"):
            if item.get(extra_key):
                base_conflict[extra_key] = item[extra_key]
        return {
            **base_conflict,
            "text": _unverified_item_text(base_conflict),
            "_buyback_state": "unverified",
            "_gap_error": conflict_reason,
            "_is_amendment": False,
            "_code_fields": {"text", "title", "tag"},
        }

    if source_kind == "announcement":
        assert scan_entry is not None
        ann_title = str(scan_entry.get("title") or "").strip()
        ann_url = str(scan_entry.get("url") or scan_entry.get("pdf_url") or "").strip()
        scan_date = _strict_iso_date(scan_entry.get("date"))
        scan_is_bb = _is_scan_ann_buyback_or_ndr(scan_entry)
        model_has_bb = _item_has_model_buyback_signal(item)
        if not scan_is_bb and not model_has_bb:
            out = dict(item)
            code_fields: set[str] = set()
            if ann_title:
                out["title"] = ann_title
                code_fields.add("title")
            out["url"] = ann_url
            if scan_date:
                out["date"] = scan_date
                code_fields.add("date")
            else:
                out["date"] = _strict_iso_date(item.get("date"))
            out["_code_fields"] = code_fields
            return out
        # Matched a buyback/NDR announcement in scan results without validated buyback_results,
        # or model attempted to smuggle buyback tags/titles/text into an ordinary announcement.
        is_amend = bool(scan_entry.get("is_amendment") or _AMENDMENT_RE.search(ann_title))
        ann_code_fields = {"text", "title", "tag"}
        if scan_date:
            ann_code_fields.add("date")
        base_ann: dict[str, Any] = {
            "tag": _sanitize_governed_tag(item.get("tag"), is_buyback=True, market=market),
            "title": ann_title or _GENERIC_UNVERIFIED_TITLE,
            "date": scan_date or _strict_iso_date(item.get("date")),
            "url": ann_url,
        }
        for extra_key in ("id", "market"):
            if item.get(extra_key):
                base_ann[extra_key] = item[extra_key]
        return {
            **base_ann,
            "text": _unverified_item_text(base_ann),
            "_buyback_state": "unverified",
            "_gap_error": _item_parse_error(scan_entry) or _item_parse_error(item),
            "_is_amendment": is_amend,
            "_code_fields": ann_code_fields,
        }

    if source_kind == "none":
        if not _is_buyback_or_ndr_governed(item, None):
            out = dict(item)
            out["date"] = _strict_iso_date(item.get("date"))
            out["_code_fields"] = set()
            return out
        base_none: dict[str, Any] = {
            "tag": _sanitize_governed_tag(item.get("tag"), is_buyback=True, market=market),
            "title": _GENERIC_UNVERIFIED_TITLE,
            "date": _strict_iso_date(item.get("date")),
            "url": str(item.get("url") or "").strip(),
        }
        for extra_key in ("id", "market"):
            if item.get(extra_key):
                base_none[extra_key] = item[extra_key]
        return {
            **base_none,
            "text": _unverified_item_text(base_none),
            "_buyback_state": "unverified",
            "_gap_error": _item_parse_error(item),
            "_is_amendment": False,
            "_code_fields": {"text", "title", "tag"},
        }

    # source_kind == "buyback"
    assert scan_entry is not None
    scan_title = str(scan_entry.get("title") or "").strip()
    scan_url = str(scan_entry.get("url") or scan_entry.get("pdf_url") or "").strip()
    scan_date = _strict_iso_date(scan_entry.get("date"))
    eff_date = scan_date or _strict_iso_date(item.get("date"))
    bb_code_fields = {"text", "title", "tag"}
    if scan_date:
        bb_code_fields.add("date")
    is_amend = bool(scan_entry.get("is_amendment") or _AMENDMENT_RE.search(scan_title))

    if (
        scan_entry.get("validated") is True
        and scan_entry.get("is_buyback") is False
        and not _item_parse_error(scan_entry)
    ):
        digest = str(scan_entry.get("digest") or "").strip()
        if digest and not _has_buyback_amount_text(digest):
            base_non_bb: dict[str, Any] = {
                "tag": _sanitize_governed_tag(item.get("tag"), is_buyback=False, market=market),
                "title": scan_title or _GENERIC_UNVERIFIED_TITLE,
                "date": eff_date,
                "url": scan_url,
            }
            for extra_key in ("id", "market"):
                if item.get(extra_key):
                    base_non_bb[extra_key] = item[extra_key]
            return {
                **base_non_bb,
                "text": digest,
                "_resolved_non_buyback_ndr": True,
                "_is_amendment": is_amend,
                "_code_fields": bb_code_fields,
            }

    base_bb: dict[str, Any] = {
        "tag": _sanitize_governed_tag(item.get("tag"), is_buyback=True, market=market),
        "title": scan_title or _GENERIC_UNVERIFIED_TITLE,
        "date": eff_date,
        "url": scan_url,
    }
    for extra_key in ("id", "market"):
        if item.get(extra_key):
            base_bb[extra_key] = item[extra_key]

    valid_ok, invalid_reason = _validate_scan_buyback_entry(scan_entry)
    if valid_ok:
        groups = [_copy_structured_group(g) for g in _extract_scan_groups(scan_entry)]
        bb_data: dict[str, Any] = {
            "groups": groups,
            "share_class": scan_entry.get("share_class"),
            "is_amendment": is_amend,
        }
        structured_text = _format_validated_buyback_text(bb_data, fallback_date=scan_date)
        return {
            **base_bb,
            "text": structured_text,
            "_buyback_state": "validated",
            "_buyback_data": bb_data,
            "_is_amendment": is_amend,
            "_code_fields": bb_code_fields,
        }

    gap_err = _item_parse_error(scan_entry) or invalid_reason
    return {
        **base_bb,
        "text": _unverified_item_text(base_bb),
        "_buyback_state": "unverified",
        "_gap_error": gap_err,
        "_is_amendment": is_amend,
        "_code_fields": bb_code_fields,
    }


def _summarize_item_safe(item: dict[str, Any]) -> str:
    state = item.get("_buyback_state")
    if state == "validated":
        return str(item.get("text") or "").strip()
    if state == "unverified":
        return _UNVERIFIED_BUYBACK_SUMMARY
    return str(item.get("text") or item.get("title") or "").strip()


def _regenerate_summary(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in items:
        part = _summarize_item_safe(item)
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return "；".join(parts)


def _format_gap_note(code: str, msg: str, item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip() or "未命名公告"
    day = str(item.get("date") or "").strip()
    desc = f"{day} {title}".strip()
    full = f"{msg}: {desc}"
    return f"{code}: {full}" if code else full


_BUYBACK_GATE_KEEP_KEYS = frozenset({
    "text",
    "title",
    "tag",
    "date",
    "url",
    "pdf_url",
    "id",
    "announcement_id",
    "notice_id",
    "market",
    "tx_type",
    "code",
    "is_purchase",
    "is_stake_increase",
    "error",
    "enrich_error",
})
_COMPANY_GATE_KEEP_KEYS = frozenset({"code", "name", "market", "summary", "items"})


def _apply_buyback_model_gate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip model digits from display fields and drop all model numeric/currency fields on NDR/buyback companies."""
    gated: list[dict[str, Any]] = []
    for it in items:
        out: dict[str, Any] = {}
        for key, value in it.items():
            if (isinstance(key, str) and key.startswith("_")) or key in _BUYBACK_GATE_KEEP_KEYS:
                out[key] = value
        code_fields = out.get("_code_fields") or set()
        out["date"] = _strict_iso_date(out.get("date")) if "date" in code_fields else ""
        for field in ("text", "title", "tag"):
            if field in code_fields:
                continue
            val = out.get(field)
            if field == "tag" and str(val or "").strip() in _FILING_FORM_TAGS:
                # Standard filing-type labels carry no amounts; keep "8-K" readable.
                continue
            if val is not None and str(val).strip():
                cleaned = _strip_model_digits(str(val))
                if field == "text" and not cleaned:
                    fallback_title = out.get("title")
                    if "title" in code_fields:
                        cleaned = str(fallback_title or "").strip() or "详见原文"
                    else:
                        cleaned = _strip_model_digits(str(fallback_title or "")) or "详见原文"
                out[field] = cleaned
        gated.append(out)
    return gated


def _safe_company_code(code: str) -> str:
    if not code or re.fullmatch(r"[A-Za-z0-9._-]+", code):
        return code
    return _strip_model_digits(code)


def _effective_company(
    company: dict[str, Any],
    *,
    scan_index: dict[str, dict[str, Any]] | None = None,
    ann_index: dict[str, dict[str, Any]] | None = None,
    dropped_notes: list[str] | None = None,
    section_market: str = "",
    scan_names: dict[str, str] | None = None,
    watchlist_names: dict[str, str] | None = None,
    scan_buyback_codes: set[str] | None = None,
    scan_amendment_codes: set[str] | None = None,
) -> dict[str, Any]:
    clean_company = _strip_internal_fields(company)
    raw_items = clean_company.get("items", []) or []
    code = str(clean_company.get("code") or "").strip()
    comp_norm = _norm_stock_code(code)
    eff_market = str(clean_company.get("market") or "").strip().lower() or section_market.strip().lower()

    trusted_name = (watchlist_names or {}).get(comp_norm) or (scan_names or {}).get(comp_norm)
    name_is_code_generated = bool(trusted_name)
    resolved_name = trusted_name if trusted_name else str(clean_company.get("name") or "").strip()

    has_scan_buyback = bool(comp_norm and scan_buyback_codes and comp_norm in scan_buyback_codes)
    has_scan_amendment = bool(comp_norm and scan_amendment_codes and comp_norm in scan_amendment_codes)

    if not raw_items:
        is_bb_related = has_scan_buyback or _is_buyback_company(clean_company)
        if is_bb_related:
            if not name_is_code_generated:
                resolved_name = _strip_model_digits(resolved_name)
            clean_code = _safe_company_code(code)
            base_company = {k: v for k, v in clean_company.items() if k in _COMPANY_GATE_KEEP_KEYS}
            if has_scan_amendment:
                if dropped_notes is not None:
                    note = (
                        f"{clean_code}: 存在更正回购公告，原公告金额未保留"
                        if clean_code
                        else "存在更正回购公告，原公告金额未保留"
                    )
                    dropped_notes.append(note)
                return {
                    **base_company,
                    "code": clean_code,
                    "name": resolved_name,
                    "summary": "存在更正回购公告，原公告已过滤",
                    "_buyback_related": True,
                    "_filtered_to_empty_buyback": True,
                }
            if dropped_notes is not None:
                note = (
                    f"{clean_code}: 回购公告缺少已核对金额，金额未核对"
                    if clean_code
                    else "回购公告缺少已核对金额，金额未核对"
                )
                dropped_notes.append(note)
            return {
                **base_company,
                "code": clean_code,
                "name": resolved_name,
                "summary": _UNVERIFIED_BUYBACK_SUMMARY,
                "_buyback_related": True,
                "_scan_buyback_without_items": True,
            }
        return {**clean_company, "name": resolved_name}

    resolved = [
        _resolve_governed_item(
            it,
            scan_index or {},
            ann_index or {},
            company_code=code,
            market=eff_market,
        )
        for it in raw_items
    ]
    has_governed = any(
        it.get("_buyback_state") or it.get("_resolved_non_buyback_ndr")
        for it in resolved
    )
    raw_summary = str(clean_company.get("summary") or "").strip()
    has_payload_bb = (
        any(_item_has_model_buyback_signal(raw_it, include_ndr=True) for raw_it in raw_items)
        or bool(_BUYBACK_TEXT_RE.search(raw_summary))
        or bool(_NDR_RE.search(raw_summary))
    )

    is_bb_related = (
        has_scan_buyback
        or has_payload_bb
        or any(is_buyback_item(it) or it.get("_resolved_non_buyback_ndr") for it in resolved)
    )

    if is_bb_related:
        if not name_is_code_generated:
            resolved_name = _strip_model_digits(resolved_name)
        code = _safe_company_code(code)

    has_amendment = has_scan_amendment or any(_is_amendment_governed_item(it) for it in resolved)
    filtered = resolved
    dropped: list[dict[str, Any]] = []
    if has_amendment:
        kept = [
            it for it in resolved
            if not is_buyback_item(it) or _is_amendment_governed_item(it)
        ]
        dropped = [
            it for it in resolved
            if is_buyback_item(it) and not _is_amendment_governed_item(it)
        ]
        if dropped_notes is not None:
            for it in dropped:
                dropped_notes.append(_format_gap_note(code, "存在更正回购公告，已过滤", it))
        filtered = kept

    for it in filtered:
        if it.get("_buyback_state") == "unverified" and dropped_notes is not None:
            err = str(it.get("_gap_error") or "").strip()
            if err:
                note = _format_gap_note(code, "回购公告解析失败，金额未核对", it) + f"（{err}）"
            else:
                note = _format_gap_note(code, "回购/翌日披露公告缺少已核对金额，金额未核对", it)
            dropped_notes.append(note)

    if is_bb_related:
        filtered = _apply_buyback_model_gate(filtered)

    filtered_to_empty_bb = bool(has_amendment and not filtered and (dropped or has_scan_buyback))
    if filtered_to_empty_bb:
        new_summary = "存在更正回购公告，原公告已过滤"
        if not dropped and dropped_notes is not None:
            note = (
                f"{code}: 存在更正回购公告，原公告金额未保留"
                if code
                else "存在更正回购公告，原公告金额未保留"
            )
            dropped_notes.append(note)
    elif any(is_buyback_item(it) for it in resolved) or has_governed or has_amendment or _has_buyback_amount_text(raw_summary):
        new_summary = _regenerate_summary(filtered)
    else:
        new_summary = _strip_model_digits(raw_summary) if is_bb_related else raw_summary

    base_out = (
        {k: v for k, v in clean_company.items() if k in _COMPANY_GATE_KEEP_KEYS}
        if is_bb_related
        else clean_company
    )
    return {
        **base_out,
        "code": code,
        "name": resolved_name,
        "items": filtered,
        "summary": new_summary,
        "_buyback_related": is_bb_related,
        "_filtered_to_empty_buyback": filtered_to_empty_bb,
    }


def collect(
    payload: dict[str, Any],
    *,
    dropped_notes: list[str] | None = None,
    scan_data: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """Flatten companies into ``(market, company)`` and split key vs routine."""
    clean_payload = _strip_internal_fields(payload)
    threshold = clean_payload.get("routine_threshold_usd")
    if not isinstance(threshold, (int, float)):
        threshold = DEFAULT_ROUTINE_THRESHOLD_USD
    scan_index, ann_index = _build_scan_indexes(scan_data)
    scan_names, scan_buyback_codes, scan_amendment_codes, _non_bb = _extract_scan_company_meta(scan_data)
    watchlist_names = _load_cn_hk_watchlist_names()

    key_tier: list[tuple[str, dict]] = []
    routine_tier: list[tuple[str, dict]] = []
    for section in clean_payload.get("sections", []) or []:
        section_key = str(section.get("key", "")).strip().lower()
        for raw_company in section.get("companies", []) or []:
            company = _effective_company(
                raw_company,
                scan_index=scan_index,
                ann_index=ann_index,
                dropped_notes=dropped_notes,
                section_market=section_key,
                scan_names=scan_names,
                watchlist_names=watchlist_names,
                scan_buyback_codes=scan_buyback_codes,
                scan_amendment_codes=scan_amendment_codes,
            )
            market = company_market(company, section_key)
            bucket = key_tier if is_key_company(company, float(threshold), market=market) else routine_tier
            bucket.append((market, company))

    def order(pair: tuple[str, dict]) -> tuple[int, int, float]:
        market, company = pair
        market_index = MARKET_ORDER.index(market) if market in MARKET_ORDER else len(MARKET_ORDER)
        return (market_index, company_weight(company, market=market), -_company_sort_amount(company))

    return sorted(key_tier, key=order), sorted(routine_tier, key=order)


def _norm_ccy_unit(ccy: str) -> str:
    raw = (ccy or "").strip()
    return _CCY_UNIT_CN.get(raw, _CCY_UNIT_CN.get(raw.upper(), raw))


def _format_ccy_amount(value: float, ccy: str) -> str:
    if value <= 0:
        return ""
    unit = _norm_ccy_unit(ccy)
    if unit == "美元":
        return format_amount_usd(value)
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿{unit}"
    wan = value / 10_000
    if wan >= 100:
        return f"{wan:,.0f} 万{unit}"
    return f"{wan:,.1f} 万{unit}"


def _parse_multi_ccy_match(matched: re.Match[str] | None) -> tuple[float, float, str, str]:
    if not matched:
        return 0.0, 0.0, "", ""
    try:
        base = float(matched.group(1).replace(",", ""))
    except ValueError:
        return 0.0, 0.0, "", ""
    unit = matched.group(2) or ""
    ccy = matched.group(3)
    mult = 100_000_000.0 if unit == "亿" else (10_000.0 if unit == "万" else 1.0)
    native = base * mult
    rate = _CCY_USD_RATE.get(ccy, _CCY_USD_RATE.get(ccy.upper(), 1.0))
    label = f"{matched.group(1)} {unit}{ccy}" if unit else f"{matched.group(1)} {ccy}"
    return native * rate, native, _norm_ccy_unit(ccy), label.strip()


def _parse_multi_ccy_amount(text: str) -> tuple[float, str]:
    """Extract the first USD/HKD/RMB amount in ``text`` as ``(approx_usd, label)``."""
    usd, _native, _unit, label = _parse_multi_ccy_match(_MULTI_CCY_RE.search(text or ""))
    return usd, label


def _parse_structured_amount(val: Any) -> float | None:
    dec = _strict_parse_dec(val, allow_decimal=True)
    return float(dec) if dec is not None else None


def _format_share_class_label(share_class: str | None) -> str:
    sc = str(share_class or "").strip()
    if sc in _DEFAULT_SHARE_CLASSES:
        return ""
    if re.fullmatch(r"[A-Za-z]", sc):
        return f"{sc.upper()}股"
    return sc


def _group_metrics(grp: dict[str, Any]) -> tuple[float, float, str] | None:
    for amt_key, cur_key in (("total_amount", "currency"), ("buyback_amount", "buyback_currency")):
        amt = _parse_structured_amount(grp.get(amt_key)) or _parse_structured_amount(grp.get(f"{amt_key}_raw"))
        cur = str(grp.get(cur_key) or grp.get("currency") or "").strip()
        if amt is not None and cur:
            rate = _CCY_USD_RATE.get(cur, _CCY_USD_RATE.get(cur.upper(), 1.0))
            return amt * rate, amt, _norm_ccy_unit(cur)
    return None


def _buyback_trade_days(group: dict[str, Any] | None) -> int:
    if not isinstance(group, dict):
        return 1
    trades = group.get("trades")
    if isinstance(trades, list) and trades:
        dates = {
            str(t.get("date") or "").strip()
            for t in trades
            if isinstance(t, dict) and str(t.get("date") or "").strip()
        }
        if dates:
            return len(dates)
    return 1


def _item_best_buyback(item: dict[str, Any]) -> tuple[float, float, str, str, int]:
    """Return ``(usd_score, native_amount, norm_unit, share_class_label, trade_days)`` for the largest group."""
    if not _has_validated_buyback(item):
        return 0.0, 0.0, "", "", 1
    bb = item.get("_buyback_data")
    if not isinstance(bb, dict):
        return 0.0, 0.0, "", "", 1
    groups = _extract_scan_groups(bb)
    if not groups or not all(_valid_scan_group(grp) for grp in groups):
        return 0.0, 0.0, "", "", 1

    multi_group = len(groups) > 1
    best: tuple[float, float, str, str, int] = (0.0, 0.0, "", "", 1)
    for grp in groups:
        metrics = _group_metrics(grp)
        if metrics is None:
            continue
        score, native, unit = metrics
        if score > best[0]:
            sc_label = _format_share_class_label(grp.get("share_class")) if multi_group else ""
            days = _buyback_trade_days(grp)
            best = (score, native, unit, sc_label, days)
    return best


def _item_buyback_metrics(item: dict[str, Any]) -> tuple[float, float, str]:
    """Return ``(usd_score, native_amount, norm_unit)`` for the largest validated group."""
    score, native, unit, _sc, _days = _item_best_buyback(item)
    return score, native, unit


def _company_sort_amount(company: dict[str, Any]) -> float:
    if company.get("_buyback_related"):
        total = 0.0
        for item in company.get("items", []) or []:
            if is_buyback_item(item):
                item_usd, _native, _unit = _item_buyback_metrics(item)
                total += item_usd
        return total
    usd = company_amount_usd(company)
    total = usd
    for item in company.get("items", []) or []:
        if is_buyback_item(item):
            item_usd, _native, _unit = _item_buyback_metrics(item)
            total += item_usd
        elif usd == 0.0:
            amt, _label = _parse_multi_ccy_amount(str(item.get("text", "")))
            total += amt
    if total > 0:
        return total
    amt, _label = _parse_multi_ccy_amount(str(company.get("summary", "")))
    return amt


def _is_buyback_company(company: dict[str, Any]) -> bool:
    if company.get("_filtered_to_empty_buyback") or company.get("_scan_buyback_without_items"):
        return True
    items = company.get("items", []) or []
    if any(is_buyback_item(item) for item in items):
        return True
    if items:
        return False
    summary = str(company.get("summary", "")).strip()
    return bool(summary and (_BUYBACK_TEXT_RE.search(summary) or _NDR_RE.search(summary)))


def _is_stake_increase_company(company: dict[str, Any], *, market: str = "") -> bool:
    eff_market = str(company.get("market", "")).strip().lower() or market.strip().lower()
    if eff_market and eff_market not in _CN_HK_MARKETS:
        return False
    items = company.get("items", []) or []
    if any(is_stake_increase_item(item, market=eff_market) for item in items):
        return True
    if items:
        return False
    summary = str(company.get("summary", "")).strip()
    return bool(
        summary
        and _STAKE_INCREASE_RE.search(summary)
        and not _STAKE_NEG_RE.search(summary)
        and not _REDUCTION_TEXT_RE.search(summary)
    )


def _buyback_rank_and_label(company: dict[str, Any]) -> tuple[float, str, str, int]:
    best_score = 0.0
    best_native = 0.0
    best_unit = ""
    best_sc = ""
    best_days = 1
    best_item_sc = ""
    item_classes: set[str] = set()
    for item in company.get("items", []) or []:
        if not is_buyback_item(item) or not _has_validated_buyback(item):
            continue
        bb_data = item.get("_buyback_data") if isinstance(item.get("_buyback_data"), dict) else {}
        item_sc = _format_share_class_label(bb_data.get("share_class"))
        if item_sc:
            item_classes.add(item_sc)
        score, native, unit, sc_label, days = _item_best_buyback(item)
        if score > best_score:
            best_score = score
            best_native = native
            best_unit = unit
            best_sc = sc_label
            best_days = days
            best_item_sc = item_sc
    if not best_sc and len(item_classes) > 1 and best_item_sc:
        best_sc = best_item_sc
    if best_native > 0 and best_unit:
        return best_score, _format_ccy_amount(best_native, best_unit), best_sc, best_days
    return best_score, "", "", 1


def _form4_buy_amount_usd(company: dict[str, Any]) -> float:
    if company.get("_buyback_related"):
        return 0.0
    total = 0.0
    for item in company.get("items", []) or []:
        if not is_form4_purchase_item(item):
            continue
        explicit = item.get("amount_usd")
        if isinstance(explicit, (int, float)) and explicit > 0:
            total += float(explicit)
    return total


def _stake_increase_rank_and_label(company: dict[str, Any], *, market: str = "") -> tuple[float, str]:
    if company.get("_buyback_related"):
        return 0.0, ""
    eff_market = str(company.get("market", "")).strip().lower() or market.strip().lower()
    total_usd = 0.0
    native_by_unit: dict[str, float] = {}
    for item in company.get("items", []) or []:
        if not is_stake_increase_item(item, market=eff_market):
            continue
        explicit = item.get("amount_usd")
        if isinstance(explicit, (int, float)) and explicit > 0:
            total_usd += float(explicit)
            native_by_unit["美元"] = native_by_unit.get("美元", 0.0) + float(explicit)
            continue
        amt = _parse_structured_amount(item.get("total_amount")) or _parse_structured_amount(item.get("total_amount_raw"))
        cur = str(item.get("currency") or "").strip()
        if amt is not None and cur:
            rate = _CCY_USD_RATE.get(cur, _CCY_USD_RATE.get(cur.upper(), 1.0))
            unit = _norm_ccy_unit(cur)
            total_usd += amt * rate
            native_by_unit[unit] = native_by_unit.get(unit, 0.0) + amt
    if len(native_by_unit) == 1:
        unit, total_native = next(iter(native_by_unit.items()))
        return total_usd, _format_ccy_amount(total_native, unit)
    return total_usd, ""


def _fit_lead_clauses(clauses: list[str], limit: int = MAX_LEAD_CHARS) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for clause in clauses:
        clean = clause.strip()
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    if not unique:
        return ""
    out = unique[0]
    if len(out) > limit:
        return out[: limit - 1].rstrip() + "…"
    for clause in unique[1:]:
        candidate = f"{out}；{clause}"
        if len(candidate) > limit:
            break
        out = candidate
    return out


def _resolve_lead(
    payload: dict[str, Any],
    key_tier: list[tuple[str, dict]],
    *,
    any_buyback_related: bool = False,
) -> str:
    """Generate lead from structured priority events; use model lead only when none exist."""
    buybacks: list[tuple[float, str, str, int, dict[str, Any]]] = []
    purchases: list[tuple[float, str, str, dict[str, Any]]] = []
    for market, company in key_tier:
        items = company.get("items", []) or []
        if _is_buyback_company(company):
            score, label, share_class, trade_days = _buyback_rank_and_label(company)
            buybacks.append((score, label, share_class, trade_days, company))
        elif any(is_form4_purchase_item(it) for it in items):
            amt = _form4_buy_amount_usd(company)
            purchases.append((amt, format_amount_usd(amt), "form4_buy", company))
        elif _is_stake_increase_company(company, market=market):
            score, label = _stake_increase_rank_and_label(company, market=market)
            purchases.append((score, label, "stake_increase", company))

    buybacks.sort(key=lambda row: -row[0])
    purchases.sort(key=lambda row: -row[0])

    def _who(company: dict[str, Any]) -> str:
        # Readers scan the lead for who acted; the trusted name says it, the code is the fallback.
        return str(company.get("name", "")).strip() or str(company.get("code", "")).strip()

    priority_snippets: list[str] = []
    for _score, label, share_class, trade_days, company in buybacks:
        code = _who(company)
        if label:
            action = "单日回购" if trade_days <= 1 else f"{trade_days} 个交易日回购"
            prefix = f"{share_class} " if share_class else ""
            priority_snippets.append(f"{code} {prefix}{action} {label}")
        else:
            priority_snippets.append(f"{code} 回购股份")
    for _score, label, event_kind, company in purchases:
        code = _who(company)
        if event_kind == "form4_buy":
            priority_snippets.append(f"{code} 内部人买入 {label}" if label else f"{code} 内部人买入")
        else:
            priority_snippets.append(f"{code} 增持 {label}" if label else f"{code} 增持")

    if priority_snippets:
        return _fit_lead_clauses(priority_snippets)

    if any_buyback_related:
        return ""

    raw_lead = str(payload.get("lead", "")).strip()
    if not raw_lead:
        return ""
    clauses = [c.strip() for c in re.split(r"[；;]", raw_lead) if c.strip()]
    safe_clauses: list[str] = []
    for clause in clauses:
        if _has_affirmative_buyback_mention(clause) or _NDR_RE.search(clause):
            continue
        safe_clauses.append(clause)
    if not safe_clauses:
        return ""
    if len(safe_clauses) != len(clauses):
        return _fit_lead_clauses(safe_clauses)
    if len(raw_lead) <= MAX_LEAD_CHARS:
        return raw_lead
    return _fit_lead_clauses(safe_clauses)


def build_card_with_notes(
    payload: dict[str, Any],
    *,
    scan_data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build one Card Schema 2.0 object and return render-time gap and filtered-amendment notes."""
    clean_payload = _strip_internal_fields(payload)
    dropped_notes: list[str] = []
    key_tier, routine_tier = collect(clean_payload, dropped_notes=dropped_notes, scan_data=scan_data)
    total_hits = len(key_tier) + len(routine_tier)

    _scan_names, scan_buyback_codes, _scan_amend_codes, _non_bb = _extract_scan_company_meta(scan_data)
    any_buyback_related = bool(scan_buyback_codes) or any(
        bool(company.get("_buyback_related"))
        for _market, company in key_tier + routine_tier
    )

    scan_report_date = _strict_iso_date(scan_data.get("report_date")) if isinstance(scan_data, dict) else ""
    date = (scan_report_date or _strict_iso_date(clean_payload.get("date"))) if any_buyback_related else (
        _strict_iso_date(clean_payload.get("date")) or scan_report_date
    )
    weekday = str(clean_payload.get("weekday", "")).strip()
    window_note = str(clean_payload.get("window_note", "")).strip()
    if any_buyback_related:
        if weekday and date:
            weekday = "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
        else:
            weekday = _strip_model_digits(weekday)
        if window_note not in ("覆盖过去 24 小时", "覆盖上周五至今"):
            window_note = _strip_model_digits(window_note)
    scanned = total_scanned(clean_payload.get("stats", {}) or {})
    scan_note = f"扫描 {scanned} 家" if scanned else ""
    subtitle_bits = [bit for bit in (date, weekday, window_note, scan_note) if bit]

    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": "每日公告提醒"},
        "template": "indigo" if key_tier else ("wathet" if total_hits else "grey"),
    }
    if subtitle_bits:
        header["subtitle"] = {"tag": "plain_text", "content": " · ".join(subtitle_bits)}
    if total_hits:
        tag_text = f"重点 {len(key_tier)}" if key_tier else "仅例行披露"
        if routine_tier and key_tier:
            tag_text += f" · 例行 {len(routine_tier)}"
    else:
        tag_text = "无新动态"
    header["text_tag_list"] = [{
        "tag": "text_tag",
        "text": {"tag": "plain_text", "content": tag_text},
        "color": "blue" if key_tier else "neutral",
    }]

    elements: list[dict[str, Any]] = []
    lead = _resolve_lead(clean_payload, key_tier, any_buyback_related=any_buyback_related)
    if lead:
        elements.append({"tag": "markdown", "content": f"**{lead}**"})

    if not total_hits:
        elements.append({
            "tag": "markdown",
            "content": "<font color='grey'>扫描窗口内无新动态。</font>",
        })

    if key_tier:
        if elements:
            elements.append({"tag": "hr"})
        markets = {market for market, _company in key_tier}
        show_market = len(markets) > 1
        elements.append(label_element(f"重点 · {len(key_tier)} 家"))
        for market, company in key_tier:
            block = render_company(company, market, show_market)
            if block.strip():
                elements.append({"tag": "markdown", "content": block})

    if routine_tier:
        elements.append({"tag": "hr"})
        routine_amount = sum(company_amount_usd(company) for _market, company in routine_tier)
        title = f"**例行披露 · {len(routine_tier)} 家**"
        amount_text = format_amount_usd(routine_amount)
        if amount_text:
            title += f" <font color='grey'>合计 {amount_text}</font>"
        routine_multi_market = len({market for market, _company in routine_tier}) > 1
        inner = [
            {
                "tag": "markdown",
                "content": render_routine_company(company, market, routine_multi_market),
            }
            for market, company in routine_tier
        ]
        elements.append(collapsible(title, inner))

    footer = _CODE_FOOTER
    elements.append({
        "tag": "markdown",
        "text_size": "notation",
        "content": f"<font color='grey'>{footer}</font>",
    })

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "10px",
            "elements": elements,
        },
    }
    return card, dropped_notes


def build_card(
    payload: dict[str, Any],
    *,
    scan_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Card Schema 2.0 object. ``warnings`` and ``stats.*.hit`` are ignored."""
    card, _notes = build_card_with_notes(payload, scan_data=scan_data)
    return card


def self_check() -> None:
    """Offline checks copied from the historical card self-test."""
    sample = {
        "date": "2026-01-02",
        "weekday": "周五",
        "window_note": "覆盖过去 24 小时",
        "stats": {"us": {"scanned": 34}, "cn": {"scanned": 40}},
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [
                {
                    "code": "BIG",
                    "name": "重点公司",
                    "summary": "摘要",
                    "items": [
                        {
                            "tag": "8-K",
                            "text": "重大事项",
                            "date": "2026-01-01",
                            "url": "https://example.com/a",
                        },
                        {"tag": "10-K", "text": "年报", "date": "2026-01-01"},
                    ],
                },
                {
                    "code": "SELL",
                    "name": "大额减持",
                    "items": [{
                        "tag": "Form 144",
                        "text": "拟售 1 股，约 5,000 万美元",
                        "date": "2026-01-01",
                    }],
                },
                {
                    "code": "TINY",
                    "name": "例行公司",
                    "summary": "税款代扣",
                    "items": [{"tag": "Form 4", "text": "Tax Withholding", "date": "2026-01-01"}],
                },
            ],
        }],
    }
    card = build_card(sample)
    elements = card["body"]["elements"]
    if card["header"]["template"] != "indigo":
        raise AssertionError("key tier uses indigo")
    if any(element.get("tag") == "column_set" for element in elements):
        raise AssertionError("no per-market stat block")
    if "扫描 74 家" not in card["header"]["subtitle"]["content"]:
        raise AssertionError("subtitle scanned total")
    if elements[0].get("tag") == "hr":
        raise AssertionError("no leading rule without a lead")
    panels = [element for element in elements if element.get("tag") == "collapsible_panel"]
    if len(panels) != 1 or "TINY" not in panels[0]["elements"][0]["content"]:
        raise AssertionError("routine panel")
    if "重点 2" not in card["header"]["text_tag_list"][0]["text"]["content"]:
        raise AssertionError("header tag")
    if not tag_style("Form 144")[1] < tag_style("Form 4")[1]:
        raise AssertionError("tag weight")
    if short_date("2026-08-27") != "08-27":
        raise AssertionError("short date")
    if parse_amount_usd("约 5,917 万美元") != 59_170_000:
        raise AssertionError("wan amount")
    if parse_amount_usd("约 1.5 亿美元") != 150_000_000:
        raise AssertionError("yi amount")
    if parse_amount_usd("没有金额") != 0:
        raise AssertionError("missing amount")
    if format_amount_usd(7_110_000) != "711 万美元":
        raise AssertionError("format wan")
    if format_amount_usd(59_170_000) != "5,917 万美元":
        raise AssertionError("format thousands")
    if format_amount_usd(150_000_000) != "1.5 亿美元":
        raise AssertionError("format yi")
    if "×2" not in tag_badges({"items": [{"tag": "Form 4"}, {"tag": "Form 4"}]}):
        raise AssertionError("badge count")
    if date_link({"date": "2026-01-01", "url": "u"}) != "[01-01](u)":
        raise AssertionError("date link")
    if total_scanned({"us": {"scanned": 34}, "cn": {}}) != 34 or total_scanned({}) != 0:
        raise AssertionError("scanned sum")
    empty = build_card({"date": "2026-01-02", "sections": []})
    if empty["header"]["template"] != "grey":
        raise AssertionError("empty template")
    if any(element.get("tag") == "collapsible_panel" for element in empty["body"]["elements"]):
        raise AssertionError("empty has no routine panel")
    if _strip_model_digits("回购金额（港元）：148,972,020") != "回购金额（港元）：":
        raise AssertionError("strip digits after punctuation")
    if _strip_model_digits("港币 １４８，９７２，０２０ 与 五亿") != "港币 与":
        raise AssertionError("strip fullwidth and chinese numerals")
    if _strip_model_digits("补充说明：每股贰叁陆港元 壹貳參肆伍陸柒捌玖拾佰仟萬億兩") != "补充说明：每股港元":
        raise AssertionError("strip uppercase simplified and traditional chinese numerals")
    if _strip_model_digits("每股廿港元，另卅股、卌股、皕股 〡〢〣〸〹〺㈠㊀") != "每股港元，另股、股、股":
        raise AssertionError("strip extended chinese counting numerals")
