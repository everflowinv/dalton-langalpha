"""SEC EDGAR list and enrichment. Stdlib only.

Listing uses the submissions JSON. Enrichment reads the official filing text.
State files are ``cik_map.json`` and ``enrich_cache.json``, written through the
bound workspace helpers. Network is ``netio.fetch_bytes``.
"""

from __future__ import annotations

import importlib
import json
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")
CIK_MAP_MAX_AGE = timedelta(days=7)
ENRICH_CACHE_MAX = 4000

ITEM_8K = {
    "1.01": "签署重大协议",
    "1.02": "终止重大协议",
    "1.03": "破产或托管",
    "1.05": "重大网络安全事件",
    "2.01": "完成收购或处置资产",
    "2.02": "业绩发布",
    "2.03": "新增重大债务",
    "2.04": "债务加速到期",
    "2.05": "重组或裁员费用",
    "2.06": "资产减值",
    "3.01": "退市通知或上市规则不合规",
    "3.02": "未登记股权发行",
    "3.03": "股东权利变更",
    "4.01": "更换会计师事务所",
    "4.02": "过往财报不可依赖",
    "5.01": "控制权变更",
    "5.02": "董事或高管变动",
    "5.03": "章程或财年变更",
    "5.05": "道德准则变更",
    "5.07": "股东大会投票结果",
    "5.08": "股东提案事项",
    "7.01": "Reg FD 披露",
    "8.01": "其他事项",
    "9.01": "财务报表及附件",
}

FORM_LABEL = {
    "10-K": "年报",
    "10-Q": "季报",
    "20-F": "年报（外国私人发行人）",
    "DEF 14A": "股东大会代理声明",
    "DEFA14A": "代理声明补充材料",
    "S-1": "首次公开发行注册",
    "F-1": "首次公开发行注册（外国发行人）",
}

BOILERPLATE = re.compile(
    r"(?i)^[\s(\[]*("
    r"(EX|8-K|6-K)[-\d.]*\s*$|Document\s*$|"
    r"exhibit\s+\d|ex-?\d+(\.\d+)?\s|"
    r"united states\s*$|securities and exchange commission|washington,?\s*d\.?c\.?|"
    r"form\s+(6-K|8-K|20-F|40-F)\b|report of foreign private issuer|"
    r"pursuant to (rule|the requirements|section)|"
    r"(the\s+)?securities exchange act|the securities act of|"
    r"1934 act registration|commission file number|commission\s+file|"
    r"for the month of|translation of registrant|address of principal executive|"
    r"indicate by check mark|registrant.s telephone|title of each class|"
    r"name of each exchange|securities registered pursuant|"
    r"check the appropriate box|written communications|soliciting material|"
    r"pre-commencement|emerging growth company|if an emerging growth|"
    r"signatures?\s*$|by:?\s*/s/|/s/|date:\s|"
    r"item\s+\d\.\d{2}\b|state or other jurisdiction|i\.r\.s\. employer|"
    r"republic of china\s*$|"
    r"[_\-–—=*·\s]+$"
    r")"
)

NOISE = re.compile(
    r"(?i)(take no responsibility for the contents|"
    r"exchanges and clearing limited|"
    r"representation as to its accuracy|accuracy or completeness|"
    r"disclaim any liability|"
    r"forward-looking statements within the meaning|"
    r"exact name of registrant|former name or former address|"
    r"weighted voting rights|potential risks of investing|"
    r"date of report \(date of earliest)"
)

_cik_lock = threading.Lock()
_submissions_lock = threading.Lock()
_enrich_lock = threading.Lock()
_cik_map_cache: dict[str, str] | None = None
_submissions_cache: dict[str, dict[str, dict[str, str]]] = {}
_enrich_cache: dict[str, Any] | None = None
_read_text: Callable[[str], str | None] | None = None
_write_text: Callable[[str, str], None] | None = None


def _dep(name: str) -> Any:
    return importlib.import_module(f"dalton_daily_filing_{name}")


def _now() -> datetime:
    return datetime.now(HK)


def bind_state(
    read_text: Callable[[str], str | None],
    write_text: Callable[[str, str], None],
) -> None:
    """Attach workspace state readers and clear in-process caches."""
    global _read_text, _write_text, _cik_map_cache, _enrich_cache
    _read_text = read_text
    _write_text = write_text
    _cik_map_cache = None
    _enrich_cache = None
    _submissions_cache.clear()


def _fetch(url: str, timeout: int = 20) -> bytes:
    net = _dep("netio")
    return net.fetch_bytes(url, timeout=timeout, headers=net.sec_headers(), min_interval=0.15)


def _fetch_text(url: str, timeout: int = 20) -> str:
    return _fetch(url, timeout).decode("utf-8", "ignore")


def _fetch_json(url: str, timeout: int = 20) -> dict:
    payload = json.loads(_fetch(url, timeout))
    if not isinstance(payload, dict):
        raise TypeError("SEC JSON object expected")
    return payload


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = text.replace("\u200b", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _styled_headline(raw: str) -> str:
    """Headline from 13pt+ bold text. Issuer legal names are not titles."""
    candidates = []
    font_re = re.compile(r'(?is)<font\s+style="([^"]*)"[^>]*>(.*?)</font>')
    for match in font_re.finditer(raw):
        style, inner = match.groups()
        size_match = re.search(r"font-size\s*:\s*([\d.]+)pt", style, re.IGNORECASE)
        weight_match = re.search(r"font-weight\s*:\s*(\d+)", style, re.IGNORECASE)
        if not size_match or not weight_match:
            continue
        if float(size_match.group(1)) < 13 or int(weight_match.group(1)) < 600:
            continue
        value = re.sub(r"\s+", " ", _strip_html(inner)).strip(" -–—|_")
        if not 10 <= len(value) <= 220:
            continue
        lower = value.lower()
        if BOILERPLATE.match(value) or NOISE.search(value):
            continue
        if any(term in lower for term in (
            "united states",
            "securities and exchange commission",
            "report of foreign private issuer",
            "securities exchange act",
            "commission file number",
            "translation of registrant",
            "address of principal executive",
            "signatures",
        )):
            continue
        if re.search(r"(?i)\b(inc\.?|corp\.?|corporation|company limited|plc|n\.v\.)$", value):
            continue
        candidates.append((match.start(), float(size_match.group(1)), value))
    if not candidates:
        return ""
    _start, size, value = candidates[0]
    parts = [value]
    previous = _start
    for position, next_size, next_value in candidates[1:]:
        if position - previous > 700 or abs(next_size - size) > 0.1:
            break
        parts.append(next_value)
        previous = position
    return " ".join(parts)[:400]


def headline_self_check() -> None:
    sample = (
        '<font style="font-size:14pt;font-weight:700">'
        "Example Company Announces Quarterly Results</font>"
    )
    if _styled_headline(sample) != "Example Company Announces Quarterly Results":
        raise AssertionError("EDGAR headline parser self-test failed")


def load_cik_map(force: bool = False) -> dict[str, str]:
    """Ticker to zero-padded CIK. Disk cache lasts 7 days."""
    global _cik_map_cache
    with _cik_lock:
        if _cik_map_cache is not None and not force:
            return _cik_map_cache
        if not force and _read_text is not None:
            cached = _read_cached_cik_map()
            if cached is not None:
                _cik_map_cache = cached
                return _cik_map_cache
        mapping: dict[str, str] = {}
        fetched_ok = False
        try:
            data = _fetch_json("https://www.sec.gov/files/company_tickers.json", timeout=30)
            rows = data.values() if isinstance(data, dict) else []
            for row in rows:
                if not isinstance(row, dict) or not row.get("ticker") or row.get("cik_str") is None:
                    continue
                mapping[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
            fetched_ok = True
        except Exception as exc:  # noqa: BLE001 - a ticker-map fetch failure falls back to the stale cache
            mapping = _stale_cik_map()
            if not mapping:
                raise RuntimeError("SEC ticker map unavailable") from exc
        if fetched_ok and mapping and _write_text is not None:
            _write_text(
                "cik_map.json",
                json.dumps({"fetched_at": _now().isoformat(), "map": mapping}, ensure_ascii=False),
            )
        _cik_map_cache = mapping
        return _cik_map_cache


def _read_cached_cik_map() -> dict[str, str] | None:
    if _read_text is None:
        return None
    raw = _read_text("cik_map.json")
    if not raw:
        return None
    try:
        blob = json.loads(raw)
        fetched = datetime.fromisoformat(str(blob.get("fetched_at", "")))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=HK)
        if _now() - fetched < CIK_MAP_MAX_AGE and isinstance(blob.get("map"), dict):
            return {str(key).upper(): str(value) for key, value in blob["map"].items()}
    except (ValueError, json.JSONDecodeError, OSError, TypeError):
        return None
    return None


def _stale_cik_map() -> dict[str, str]:
    if _read_text is None:
        return {}
    raw = _read_text("cik_map.json")
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {}
    mapping = blob.get("map", {})
    if not isinstance(mapping, dict):
        return {}
    return {str(key).upper(): str(value) for key, value in mapping.items()}


def _at(values: list, index: int) -> str:
    if index < 0 or index >= len(values):
        return ""
    value = values[index]
    return "" if value is None else str(value)


def load_submissions(cik: str) -> dict[str, dict[str, str]]:
    """Accession to submission fields. Raises when the feed cannot be read."""
    with _submissions_lock:
        cached = _submissions_cache.get(cik)
        if cached is not None:
            return cached
    _ordered, index = _load_recent(cik)
    with _submissions_lock:
        _submissions_cache[cik] = index
    return index


def _load_recent(cik: str) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    data = _fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=30)
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    if not isinstance(accessions, list):
        accessions = []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []
    report_dates = recent.get("reportDate") or []
    primary_docs = recent.get("primaryDocument") or []
    primary_desc = recent.get("primaryDocDescription") or []
    ordered: list[dict[str, str]] = []
    index: dict[str, dict[str, str]] = {}
    for position, accession in enumerate(accessions):
        acc = str(accession or "")
        row = {
            "form": _at(forms, position),
            "items": _at(items, position),
            "reportDate": _at(report_dates, position),
            "primaryDocument": _at(primary_docs, position),
            "primaryDocDescription": _at(primary_desc, position),
        }
        index[acc] = row
        ordered.append({
            "accession": acc,
            "date": _at(dates, position),
            **row,
        })
    return ordered, index


def form_matches(form: str, requested: str) -> bool:
    """Match a SEC form to a watched type. ``4`` does not match ``424B``."""
    form_u = (form or "").upper().strip()
    req = (requested or "").upper().strip()
    if not form_u or not req:
        return False
    if req == "4":
        return form_u in {"4", "4/A"}
    if req == "424B":
        return form_u.startswith("424B")
    return form_u == req or form_u.startswith(req + "/")


def list_recent_filings(ticker: str, forms: list[str], limit_per_form: int = 5) -> dict[str, Any]:
    """Up to ``limit_per_form`` newest filings per watched form, before the date cutoff."""
    symbol = ticker.upper()
    cik = load_cik_map().get(symbol, "")
    if not cik:
        return {"cik": "", "filings": [], "errors": [f"no CIK for {symbol}"]}
    try:
        ordered, index = _load_recent(cik)
    except Exception as exc:  # noqa: BLE001 - one ticker's submissions failure is recorded on that ticker
        return {"cik": cik, "filings": [], "errors": [f"{type(exc).__name__}: {exc}"[:300]]}
    with _submissions_lock:
        _submissions_cache[cik] = index
    chosen: list[dict[str, str]] = []
    seen: set[str] = set()
    for requested in forms:
        kept = 0
        for row in ordered:
            if not form_matches(row.get("form", ""), requested):
                continue
            accession = row.get("accession", "")
            if accession and accession in seen:
                continue
            if accession:
                seen.add(accession)
            display = "Form 4" if (row.get("form") or "").upper() in {"4", "4/A"} else row.get("form", "")
            chosen.append({**row, "type": display})
            kept += 1
            if kept >= limit_per_form:
                break
    return {"cik": cik, "filings": chosen, "errors": []}


def _archive_dir(cik: str, accession: str) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession.replace('-', '')}"
    )


def _list_documents(cik: str, accession: str) -> list[str]:
    try:
        data = _fetch_json(f"{_archive_dir(cik, accession)}/index.json")
    except Exception:  # noqa: BLE001 - a missing archive index is an empty document list
        return []
    items = ((data.get("directory") or {}).get("item")) or []
    return [str(item.get("name", "")) for item in items if isinstance(item, dict)]


def _primary_doc(cik: str, accession: str, meta: dict, docs: list[str]) -> str:
    if meta.get("primaryDocument"):
        return str(meta["primaryDocument"])
    candidates = [
        name for name in docs
        if name.endswith((".htm", ".html")) and not re.search(r"(?i)(ex-?\d|index|-index)", name)
    ]
    return candidates[0] if candidates else ""


def _clean_body(text: str, limit: int = 1200) -> str:
    keep = []
    total = 0
    for line in text.split("\n"):
        line = line.strip(" –-—·|_")
        if len(line) < 20 or BOILERPLATE.match(line) or NOISE.search(line):
            continue
        if re.search(r"(?i)\.(htm|html|xml|jpg|png|pdf)\s*$", line):
            continue
        keep.append(line)
        total += len(line) + 1
        if total >= limit:
            break
    return re.sub(r"\s+", " ", " ".join(keep))[:limit].strip()


def _item_sections(body: str, limit: int = 1200) -> str:
    chunks = []
    matches = list(re.finditer(r"(?im)^\s*item\s+(\d\.\d{2})[.\s–-]+([^\n]{0,120})", body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = _clean_body(body[match.end():end], limit=400)
        if text:
            chunks.append(f"Item {match.group(1)} {match.group(2).strip(' .;:')}: {text}")
        if sum(len(chunk) for chunk in chunks) >= limit:
            break
    return " / ".join(chunks)[:limit]


def _exhibit_excerpt(cik: str, accession: str, docs: list[str], limit: int = 1200) -> str:
    exhibits = sorted(
        name for name in docs
        if re.search(r"(?i)ex-?99", name) and name.endswith((".htm", ".html"))
    )
    if not exhibits:
        return ""
    try:
        raw = _fetch_text(f"{_archive_dir(cik, accession)}/{exhibits[0]}", timeout=25)
    except Exception:  # noqa: BLE001 - an exhibit fetch failure leaves the excerpt empty
        return ""
    return _clean_body(_strip_html(raw[:400_000]), limit)


def _enrich_8k(cik: str, accession: str, meta: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    codes = [code.strip() for code in (meta.get("items") or "").split(",") if code.strip()]
    labels = [f"Item {code} {ITEM_8K[code]}" if code in ITEM_8K else f"Item {code}" for code in codes]
    out["items"] = codes
    out["item_labels"] = labels
    docs = _list_documents(cik, accession)
    primary = _primary_doc(cik, accession, meta, docs)
    body = ""
    titles: list[str] = []
    if primary:
        try:
            body = _strip_html(_fetch_text(f"{_archive_dir(cik, accession)}/{primary}", timeout=25))
            for match in re.finditer(r"(?im)^\s*item\s+(\d\.\d{2})[.\s–-]+([^\n]{4,120})", body):
                title = match.group(2).strip(" .;:")
                if title and title.lower() not in {item.lower() for item in titles}:
                    titles.append(f"Item {match.group(1)} {title}")
        except Exception:  # noqa: BLE001 - a primary-document fetch failure leaves the 8-K body empty
            body = ""
    if titles:
        out["item_titles"] = titles
    if not labels and titles:
        out["item_labels"] = titles
    excerpt = _exhibit_excerpt(cik, accession, docs) if docs else ""
    if not excerpt and body:
        excerpt = _item_sections(body) or _clean_body(body)
    if excerpt:
        out["body_excerpt"] = excerpt
    out["headline"] = "；".join(labels or titles)
    return out


def _enrich_6k(cik: str, accession: str, meta: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    docs = _list_documents(cik, accession)
    primary = _primary_doc(cik, accession, meta, docs)
    body = ""
    styled = ""
    if primary:
        try:
            raw = _fetch_text(f"{_archive_dir(cik, accession)}/{primary}", timeout=25)
            body = _strip_html(raw)
            styled = _styled_headline(raw)
        except Exception:  # noqa: BLE001 - a primary-document fetch failure leaves the 6-K body empty
            body = ""
    summary = ""
    if body:
        matched = re.search(
            r"(?is)INFORMATION\s+(?:CONTAINED|FURNISHED)[^\n]*\n(.{40,900}?)"
            r"(?:INCORPORATION BY REFERENCE|INDEX TO EXHIBITS|SIGNATURES?|EXHIBIT INDEX|$)",
            body,
        )
        if matched:
            summary = re.sub(r"\s+", " ", matched.group(1)).strip()
        if not summary:
            matched = re.search(r"(?is)(?:99\.1|Exhibit\s+99\.1)\s*[\u200b\s]*([^\n]{15,200})", body)
            if matched:
                summary = re.sub(r"\s+", " ", matched.group(1)).strip()
    excerpt = _exhibit_excerpt(cik, accession, docs) if docs else ""
    if not excerpt and body:
        excerpt = _clean_body(body)
    if excerpt:
        out["body_excerpt"] = excerpt
    if summary:
        out["cover_summary"] = summary[:600]
    out["headline"] = summary[:400] or styled
    return out


def _tag(text: str, name: str) -> str:
    matched = re.search(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", text)
    return unescape(matched.group(1)).strip() if matched else ""


def _enrich_ownership(cik: str, accession: str, form: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        xml = _fetch_text(f"{_archive_dir(cik, accession)}/primary_doc.xml", timeout=20)
    except Exception:  # noqa: BLE001 - a missing ownership XML returns no parsed holders
        return out
    text = re.sub(r"\s+", " ", xml)
    raw_names = [
        unescape(match.group(1)).strip()
        for match in re.finditer(
            r"<(?:\w+:)?reportingPersonName>(.*?)</(?:\w+:)?reportingPersonName>",
            text,
        )
    ]
    if not raw_names:
        filing_person = _tag(text, "filingPersonName")
        if filing_person:
            raw_names = [filing_person]
    names: list[str] = []
    for name in raw_names:
        if name and name not in names:
            names.append(name)
    percent = _tag(text, "classPercent")
    shares = _tag(text, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
    amendment = _tag(text, "amendmentNo")
    event_date = _tag(text, "eventDateRequiresFilingThisStatement")
    security = _tag(text, "securitiesClassTitle")
    out["reporting_persons"] = names
    if percent:
        out["percent_of_class"] = percent
    try:
        out["shares"] = int(float(shares))
    except (TypeError, ValueError):
        pass
    if amendment:
        out["amendment_no"] = amendment
    if event_date:
        out["event_date"] = event_date
    if security:
        out["security_class"] = security
    if names:
        head = "、".join(names[:3])
        bits = []
        if out.get("shares"):
            bits.append(f"{out['shares']:,} 股")
        if percent:
            bits.append(f"占已发行股份 {percent}%")
        detail = "，".join(bits)
        amend = f"，{form} 第 {amendment} 次修订" if amendment else ""
        out["headline"] = f"{head} 申报持股" + (f" {detail}" if detail else "") + amend
    return out


def _enrich_144(cik: str, accession: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        xml = _fetch_text(f"{_archive_dir(cik, accession)}/primary_doc.xml", timeout=20)
    except Exception:  # noqa: BLE001 - a missing Form 144 XML returns no sale fields
        return out
    text = re.sub(r"\s+", " ", xml)
    seller = _tag(text, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
    relation = _tag(text, "relationshipToIssuer")
    shares = _tag(text, "noOfUnitsSold")
    value = _tag(text, "aggregateMarketValue")
    sale_date = _tag(text, "approxSaleDate")
    nature = _tag(text, "natureOfAcquisitionTransaction")
    for key, item in {
        "seller": seller,
        "position": relation,
        "sale_date": sale_date,
        "nature": nature,
    }.items():
        if item:
            out[key] = item
    try:
        out["shares"] = int(float(shares))
    except (TypeError, ValueError):
        pass
    try:
        out["market_value"] = float(value)
    except (TypeError, ValueError):
        pass
    if seller:
        pos = f" ({relation})" if relation else ""
        count = out.get("shares")
        amount = out.get("market_value")
        if count and amount:
            shown = f"${amount / 1_000_000:.2f}M" if amount >= 1_000_000 else f"${amount:,.0f}"
            out["headline"] = f"{seller}{pos} — 拟售 {count:,} 股，约 {shown}"
        elif count:
            out["headline"] = f"{seller}{pos} — 拟售 {count:,} 股"
        else:
            out["headline"] = f"{seller}{pos} — 144 减持意向"
        out["summary"] = out["headline"]
    return out


def _xml_tag(text: str, name: str) -> str:
    matched = re.search(
        rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not matched:
        return ""
    return unescape(re.sub(r"<[^>]+>", " ", matched.group(1)))
    # whitespace collapsed by caller


def _xml_value(block: str, tag: str) -> str:
    matched = re.search(
        rf"<(?:\w+:)?{tag}\b[^>]*>.*?<(?:\w+:)?value>(.*?)</(?:\w+:)?value>",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    if not matched:
        return ""
    return unescape(re.sub(r"\s+", "", matched.group(1)))


def _form4_summary(transactions: list[dict[str, str]]) -> str:
    """Same sentence shape as the historical insider text parser."""
    def total(kind: str) -> tuple[int, float]:
        rows = [row for row in transactions if row["type"] == kind]
        shares = sum(int(row["shares"]) for row in rows if row.get("shares"))
        value = sum(float(row["value"]) for row in rows if row.get("value"))
        return shares, value

    parts = []
    sales_n, sales_v = total("SALE")
    if sales_n:
        parts.append(f"卖出 {sales_n:,} 股，约 ${sales_v:,.0f}")
    buy_n, buy_v = total("PURCHASE")
    if buy_n:
        parts.append(f"买入 {buy_n:,} 股，约 ${buy_v:,.0f}")
    ex_n, ex_v = total("EXERCISE")
    if ex_n:
        parts.append(f"行权 {ex_n:,} 股（价值 ${ex_v:,.0f}）")
    award_n, award_v = total("AWARD")
    if award_n:
        if award_v > 0:
            parts.append(f"获授 {award_n:,} 股（价值 ${award_v:,.0f}）")
        else:
            parts.append(f"获授 {award_n:,} 股")
    tax_n, _tax_v = total("TAX")
    if tax_n:
        parts.append(f"税款代扣 {tax_n:,} 股")
    return "；".join(parts)


def _parse_form4_xml(xml: str) -> dict[str, Any]:
    owner = ""
    owners = re.findall(
        r"<(?:\w+:)?reportingOwner\b[^>]*>(.*?)</(?:\w+:)?reportingOwner>",
        xml,
        re.IGNORECASE | re.DOTALL,
    )
    owner_block = owners[0] if owners else xml
    name = re.sub(r"\s+", " ", _xml_tag(owner_block, "rptOwnerName")).strip()
    title = re.sub(r"\s+", " ", _xml_tag(owner_block, "officerTitle")).strip()
    if not title:
        director = _xml_tag(owner_block, "isDirector").strip() in {"1", "true", "TRUE"}
        ten = _xml_tag(owner_block, "isTenPercentOwner").strip() in {"1", "true", "TRUE"}
        if director:
            title = "Director"
        elif ten:
            title = "10% Owner"
    owner = name
    code_type = {
        "S": "SALE",
        "P": "PURCHASE",
        "M": "EXERCISE",
        "A": "AWARD",
        "G": "GIFT",
        "F": "TAX",
        "D": "DISPOSE",
    }
    transactions: list[dict[str, str]] = []
    derivative_only = True
    blocks = re.findall(
        r"<(?:\w+:)?((?:non)?derivativeTransaction)\b[^>]*>(.*?)</(?:\w+:)?\1>",
        xml,
        re.IGNORECASE | re.DOTALL,
    )
    for tag, block in blocks:
        code = re.sub(r"\s+", "", _xml_tag(block, "transactionCode")).upper()
        shares_text = _xml_value(block, "transactionShares")
        price_text = _xml_value(block, "transactionPricePerShare")
        try:
            shares_n = int(float(shares_text)) if shares_text else 0
        except ValueError:
            shares_n = 0
        try:
            price_n = float(price_text) if price_text else 0.0
        except ValueError:
            price_n = 0.0
        value_n = shares_n * price_n
        kind = code_type.get(code, code or "OTHER")
        if not tag.lower().startswith("derivative"):
            derivative_only = False
        if shares_n == 0 and value_n == 0:
            continue
        transactions.append({
            "type": kind,
            "code": code,
            "desc": "derivative" if tag.lower().startswith("derivative") else "non-derivative",
            "shares": str(shares_n),
            "price": price_text or "0",
            "value": f"{value_n:.0f}",
        })
    summary = _form4_summary(transactions)
    raw_types: list[str] = []
    for row in transactions:
        kind = row["type"]
        if kind and kind not in raw_types:
            raw_types.append(kind)
    # Historical text parser: tx_type stays the raw code, and an uncovered
    # code (GIFT, DISPOSE, …) becomes the summary instead of an empty string.
    tx_type = "、".join(raw_types)
    if not summary:
        summary = tx_type
    nonzero = any(float(row["value"]) > 0 for row in transactions)
    award_only = {row["type"] for row in transactions} <= {"AWARD"}
    if derivative_only and transactions and not nonzero and (not summary or award_only):
        summary = "DERIVATIVE ACQUISITION"
    parsed = {
        "insider": owner,
        "position": title,
        "transactions": transactions,
        "summary": summary,
        "tx_type": tx_type,
    }
    return parsed


def _raw_archive_name(name: str) -> str:
    """Drop an ``xsl…/`` render prefix so the archive fetch hits the raw XML."""
    text = (name or "").strip().lstrip("/")
    head, separator, tail = text.partition("/")
    if separator and head.lower().startswith("xsl") and tail:
        return tail
    return text


def _is_ownership_document(xml: str) -> bool:
    return re.search(r"<(?:\w+:)?ownershipDocument\b", xml or "", re.IGNORECASE) is not None


def _form4_names(primary: str, docs: list[str]) -> list[str]:
    """Raw XML names, ``primaryDocument`` first, including ``doc4.xml``."""
    ordered: list[str] = []

    def add(name: str) -> None:
        raw = _raw_archive_name(name)
        if raw.lower().endswith(".xml") and raw not in ordered:
            ordered.append(raw)

    add(primary)
    preferred: list[str] = []
    rest: list[str] = []
    for name in docs:
        raw = _raw_archive_name(name)
        lower = raw.lower()
        if not lower.endswith(".xml"):
            continue
        if any(token in lower for token in ("doc4", "form4", "ownership", "primary_doc", "wf-form4")):
            preferred.append(raw)
        else:
            rest.append(raw)
    for name in preferred + rest:
        add(name)
    add("primary_doc.xml")
    return ordered


def _enrich_form4(cik: str, accession: str, meta: dict | None = None) -> dict[str, Any]:
    docs = _list_documents(cik, accession)
    primary = str((meta or {}).get("primaryDocument") or "")
    last_error = "Form 4 XML missing ownershipDocument"
    saw_fetch_error = False
    for name in _form4_names(primary, docs):
        try:
            xml = _fetch_text(f"{_archive_dir(cik, accession)}/{name}", timeout=20)
        except Exception as exc:  # noqa: BLE001 - a Form 4 fetch failure tries the next document name
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            saw_fetch_error = True
            continue
        if not _is_ownership_document(xml):
            continue
        parsed = _parse_form4_xml(xml)
        out: dict[str, Any] = {"parsed": parsed}
        if parsed.get("summary"):
            out["headline"] = parsed["summary"]
        return out
    if saw_fetch_error:
        return {"enrich_error": last_error}
    return {"enrich_error": "Form 4 XML missing ownershipDocument"}


def _enrich_generic(form: str, meta: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    label = FORM_LABEL.get(form.upper().strip(), "")
    period = meta.get("reportDate") or ""
    desc = meta.get("primaryDocDescription") or ""
    bits = [
        bit for bit in (
            label,
            f"报告期 {period}" if period else "",
            desc if desc and desc.upper() != form.upper() else "",
        ) if bit
    ]
    if bits:
        out["headline"] = "，".join(bits)
    if period:
        out["report_date"] = period
    return out


def _load_enrich_cache() -> dict[str, Any]:
    global _enrich_cache
    with _enrich_lock:
        if _enrich_cache is None:
            _enrich_cache = {}
            if _read_text is not None:
                raw = _read_text("enrich_cache.json")
                if raw:
                    try:
                        loaded = json.loads(raw)
                        if isinstance(loaded, dict):
                            _enrich_cache = loaded
                    except json.JSONDecodeError:
                        _enrich_cache = {}
        return _enrich_cache


def save_enrich_cache() -> None:
    if _write_text is None:
        return
    cache = _load_enrich_cache()
    with _enrich_lock:
        if len(cache) > ENRICH_CACHE_MAX:
            kept = sorted(cache.items(), key=lambda item: str(item[1].get("_ts", "")), reverse=True)
            cache = dict(kept[:ENRICH_CACHE_MAX])
            _enrich_cache = cache
        _write_text("enrich_cache.json", json.dumps(cache, ensure_ascii=False, indent=1))


def enrich_filing(
    ticker: str,
    form: str,
    accession: str,
    cik: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return headline, url, and form-specific fields. Does not raise."""
    if not accession:
        return {}
    cache = _load_enrich_cache()
    if use_cache and accession in cache:
        cached = dict(cache[accession])
        cached.pop("_ts", None)
        if cached.get("headline") or cached.get("body_excerpt") or (cached.get("parsed") or {}).get("summary"):
            return cached
    resolved = cik or load_cik_map().get(ticker.upper(), "")
    if not resolved:
        return {"enrich_error": f"no CIK for {ticker}"}
    try:
        meta = load_submissions(resolved).get(accession, {})
    except Exception as exc:  # noqa: BLE001 - a submissions lookup failure is returned as enrich_error
        return {"enrich_error": f"{type(exc).__name__}: {exc}"[:300]}
    form_u = (form or meta.get("form") or "").upper().strip()
    out: dict[str, Any] = {
        "url": (
            f"https://www.sec.gov/Archives/edgar/data/{int(resolved)}/"
            f"{accession.replace('-', '')}/{accession}-index.htm"
        ),
    }
    if meta.get("primaryDocument"):
        out["doc_url"] = f"{_archive_dir(resolved, accession)}/{meta['primaryDocument']}"
    if meta.get("reportDate"):
        out["report_date"] = meta["reportDate"]
    try:
        if form_u in {"4", "4/A", "FORM 4", "FORM 4/A"}:
            out.update(_enrich_form4(resolved, accession, meta))
        elif form_u == "8-K" or form_u.startswith("8-K/"):
            out.update(_enrich_8k(resolved, accession, meta))
        elif form_u == "6-K" or form_u.startswith("6-K/"):
            out.update(_enrich_6k(resolved, accession, meta))
        elif form_u.startswith(("SC 13", "SCHEDULE 13")):
            out.update(_enrich_ownership(resolved, accession, form_u))
        elif form_u == "144" or form_u.startswith("144/"):
            out.update(_enrich_144(resolved, accession))
        else:
            out.update(_enrich_generic(form_u, meta))
    except Exception as exc:  # noqa: BLE001 - one form parser failure is returned as enrich_error
        out["enrich_error"] = f"{type(exc).__name__}: {exc}"[:300]
    if not out.get("headline"):
        out["headline"] = ""
    if out.get("headline") or out.get("body_excerpt") or (out.get("parsed") or {}).get("summary"):
        cache[accession] = {**out, "_ts": _now().isoformat()}
    else:
        cache.pop(accession, None)
    return out
