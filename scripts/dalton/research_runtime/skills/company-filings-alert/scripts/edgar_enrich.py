#!/usr/bin/env python3
"""EDGAR filing enrichment — turn a bare (form, accession) pair into a headline.

The scan CLI only knows form type / date / accession. Without a headline the
downstream push can only say "未取得正文". This module fetches the minimum
official SEC data needed to describe each filing:

- 8-K   : item codes from the submissions API + official item titles and the
          EX-99 press-release headline from the filing itself
- 6-K   : the "INFORMATION CONTAINED IN THIS REPORT" block of the cover page,
          falling back to the first EX-99 heading
- 13D/G : reporting person, stake percent, share count, amendment number from
          primary_doc.xml
- 144   : seller, shares, market value from primary_doc.xml
- other : report period + primary document description

Everything comes from sec.gov / data.sec.gov. Results are cached by accession
so repeated scans do not refetch.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def _resolve_state_dir(default_code_root: Path) -> Path:
    _ = default_code_root
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return home_root / "state" / "company-filings-alert"


STATE_DIR = _resolve_state_dir(SKILL_DIR.parent.parent)
CIK_MAP_FILE = STATE_DIR / "cik_map.json"
ENRICH_CACHE_FILE = STATE_DIR / "enrich_cache.json"
CIK_MAP_MAX_AGE_DAYS = 7
ENRICH_CACHE_MAX = 4000

USER_AGENT = os.environ.get("EDGAR_IDENTITY", "").strip()

# SEC allows 10 requests/second. Keep a shared floor across scan threads.
_RATE_LOCK = threading.Lock()
_MIN_INTERVAL = 0.15
_last_request = [0.0]

# 8-K item codes → 中文标签. Covers the codes that actually show up in a
# coverage-list scan; unknown codes fall through as the raw code.
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


class EnrichError(Exception):
    pass


def _get_user_agent() -> str:
    ua = os.environ.get("EDGAR_IDENTITY", "").strip() or USER_AGENT
    if not ua:
        raise EnrichError("EDGAR_IDENTITY environment variable is required by SEC")
    return ua


def _sec_get(url: str, timeout: int = 20) -> bytes:
    user_agent = _get_user_agent()
    with _RATE_LOCK:
        gap = time.time() - _last_request[0]
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        _last_request[0] = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": urllib.parse.urlparse(url).netloc,
    })
    resp = urllib.request.urlopen(req, timeout=timeout)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    return raw


def _sec_text(url: str, timeout: int = 20) -> str:
    return _sec_get(url, timeout).decode("utf-8", "ignore")


def _sec_json(url: str, timeout: int = 20) -> dict:
    return json.loads(_sec_get(url, timeout))


def _strip_html(raw: str) -> str:
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = unescape(txt)
    txt = txt.replace("​", " ").replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


def _styled_headline(raw: str) -> str:
    """Best-effort headline from a filing's large bold text.

    Some foreign issuers embed the press release directly in the primary 6-K
    document instead of attaching an EX-99.  Those filings often have no cover
    summary, but their announcement title is still marked up as 13pt+ bold
    text.  Keep this deliberately conservative so an issuer name or SEC cover
    heading is not presented as the event title.
    """
    candidates = []
    font_re = re.compile(r'(?is)<font\s+style="([^"]*)"[^>]*>(.*?)</font>')
    for match in font_re.finditer(raw):
        style, inner = match.groups()
        size_match = re.search(r"font-size\s*:\s*([\d.]+)pt", style, re.I)
        weight_match = re.search(r"font-weight\s*:\s*(\d+)", style, re.I)
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
            "united states", "securities and exchange commission",
            "report of foreign private issuer", "securities exchange act",
            "commission file number", "translation of registrant",
            "address of principal executive", "signatures",
        )):
            continue
        # A large legal name on the cover is the issuer, not the announcement.
        if re.search(r"(?i)\b(inc\.?|corp\.?|corporation|company limited|plc|n\.v\.)$", value):
            continue
        candidates.append((match.start(), float(size_match.group(1)), value))

    if not candidates:
        return ""

    # Consecutive equal-size fragments usually form a wrapped two-line title.
    start, size, value = candidates[0]
    parts = [value]
    previous = start
    for position, next_size, next_value in candidates[1:]:
        if position - previous > 700 or abs(next_size - size) > 0.1:
            break
        parts.append(next_value)
        previous = position
    return " ".join(parts)[:400]


# ---------------------------------------------------------------- ticker→CIK

_cik_map_cache = None
_cik_lock = threading.Lock()


def load_cik_map(force: bool = False) -> dict:
    """Ticker → zero-padded CIK, cached on disk for a week."""
    global _cik_map_cache
    with _cik_lock:
        if _cik_map_cache is not None and not force:
            return _cik_map_cache
        if CIK_MAP_FILE.exists() and not force:
            try:
                blob = json.loads(CIK_MAP_FILE.read_text())
                fetched = datetime.fromisoformat(blob.get("fetched_at", ""))
                if datetime.now() - fetched < timedelta(days=CIK_MAP_MAX_AGE_DAYS):
                    _cik_map_cache = blob.get("map", {})
                    return _cik_map_cache
            except (ValueError, json.JSONDecodeError, OSError):
                pass
        mapping = {}
        try:
            data = _sec_json("https://www.sec.gov/files/company_tickers.json", timeout=30)
            for row in data.values():
                mapping[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
        except Exception:
            # Fall back to a stale cache rather than losing every headline.
            if CIK_MAP_FILE.exists():
                try:
                    mapping = json.loads(CIK_MAP_FILE.read_text()).get("map", {})
                except (json.JSONDecodeError, OSError):
                    mapping = {}
        if mapping:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            CIK_MAP_FILE.write_text(json.dumps(
                {"fetched_at": datetime.now().isoformat(), "map": mapping},
                ensure_ascii=False))
        _cik_map_cache = mapping
        return _cik_map_cache


_submissions_cache = {}
_submissions_lock = threading.Lock()


def load_submissions(cik: str) -> dict:
    """accession → {items, primaryDocument, primaryDocDescription, reportDate}."""
    with _submissions_lock:
        if cik in _submissions_cache:
            return _submissions_cache[cik]
    index = {}
    try:
        data = _sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=30)
        recent = data.get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        for i, acc in enumerate(accs):
            index[acc] = {
                "form": recent.get("form", [""] * len(accs))[i],
                "items": recent.get("items", [""] * len(accs))[i],
                "reportDate": recent.get("reportDate", [""] * len(accs))[i],
                "primaryDocument": recent.get("primaryDocument", [""] * len(accs))[i],
                "primaryDocDescription": recent.get("primaryDocDescription", [""] * len(accs))[i],
            }
    except Exception:
        index = {}
    with _submissions_lock:
        _submissions_cache[cik] = index
    return index


# ------------------------------------------------------------------- caching

_enrich_cache = None
_enrich_lock = threading.Lock()


def _load_enrich_cache() -> dict:
    global _enrich_cache
    with _enrich_lock:
        if _enrich_cache is None:
            try:
                _enrich_cache = json.loads(ENRICH_CACHE_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                _enrich_cache = {}
        return _enrich_cache


def save_enrich_cache():
    cache = _load_enrich_cache()
    with _enrich_lock:
        if len(cache) > ENRICH_CACHE_MAX:
            keep = sorted(cache.items(), key=lambda kv: kv[1].get("_ts", ""), reverse=True)
            cache = dict(keep[:ENRICH_CACHE_MAX])
            _enrich_cache.clear()
            _enrich_cache.update(cache)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ENRICH_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))


# ------------------------------------------------------------ per-form detail

def _archive_dir(cik: str, accession: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession.replace('-', '')}")


def _list_documents(cik: str, accession: str) -> list:
    try:
        data = _sec_json(f"{_archive_dir(cik, accession)}/index.json")
        return [i.get("name", "") for i in data.get("directory", {}).get("item", [])]
    except Exception:
        return []


def _primary_doc(cik: str, accession: str, meta: dict, docs: list) -> str:
    """Primary document name, falling back to the archive listing when the
    submissions index does not carry this filing (older or amended entries)."""
    if meta.get("primaryDocument"):
        return meta["primaryDocument"]
    candidates = [d for d in docs
                  if d.endswith((".htm", ".html"))
                  and not re.search(r"(?i)(ex-?\d|index|-index)", d)]
    return candidates[0] if candidates else ""


# Cover-page and EDGAR-viewer boilerplate that carries no news value. Dropping
# these lines is what makes the excerpt start at the actual announcement.
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
    r")")

# Lines that are pure disclaimer/legend regardless of where they start.
NOISE = re.compile(
    r"(?i)(take no responsibility for the contents|"
    r"exchanges and clearing limited|"
    r"representation as to its accuracy|accuracy or completeness|"
    r"disclaim any liability|"
    r"forward-looking statements within the meaning|"
    r"exact name of registrant|former name or former address|"
    r"weighted voting rights|potential risks of investing|"
    r"date of report \(date of earliest)")


def _clean_body(text: str, limit: int = 1200) -> str:
    """Strip cover-page boilerplate and return the first substantive text."""
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
    """Text that follows each `Item X.XX` header of an 8-K cover page. This is
    where the substance lives when the filing carries no EX-99 press release."""
    chunks = []
    matches = list(re.finditer(r"(?im)^\s*item\s+(\d\.\d{2})[.\s–-]+([^\n]{0,120})", body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section = body[m.end():end]
        text = _clean_body(section, limit=400)
        if text:
            chunks.append(f"Item {m.group(1)} {m.group(2).strip(' .;:')}: {text}")
        if sum(len(c) for c in chunks) >= limit:
            break
    return " / ".join(chunks)[:limit]


def _exhibit_excerpt(cik: str, accession: str, docs: list, limit: int = 1200) -> str:
    """Cleaned text of the EX-99 press release, when the filing has one."""
    ex = sorted(d for d in docs
                if re.search(r"(?i)ex-?99", d) and d.endswith((".htm", ".html")))
    if not ex:
        return ""
    try:
        raw = _sec_text(f"{_archive_dir(cik, accession)}/{ex[0]}", timeout=25)
    except Exception:
        return ""
    return _clean_body(_strip_html(raw[:400_000]), limit)


def _enrich_8k(cik: str, accession: str, meta: dict) -> dict:
    out = {}
    codes = [c.strip() for c in (meta.get("items") or "").split(",") if c.strip()]
    labels = [f"Item {c} {ITEM_8K[c]}" if c in ITEM_8K else f"Item {c}" for c in codes]
    out["items"] = codes
    out["item_labels"] = labels

    docs = _list_documents(cik, accession)
    primary = _primary_doc(cik, accession, meta, docs)
    body = ""
    titles = []
    if primary:
        try:
            body = _strip_html(_sec_text(f"{_archive_dir(cik, accession)}/{primary}", timeout=25))
            for m in re.finditer(r"(?im)^\s*item\s+(\d\.\d{2})[.\s–-]+([^\n]{4,120})", body):
                title = m.group(2).strip(" .;:")
                if title and title.lower() not in {t.lower() for t in titles}:
                    titles.append(f"Item {m.group(1)} {title}")
        except Exception:
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


def _enrich_6k(cik: str, accession: str, meta: dict) -> dict:
    out = {}
    docs = _list_documents(cik, accession)
    primary = _primary_doc(cik, accession, meta, docs)
    body = ""
    styled_headline = ""
    if primary:
        try:
            raw = _sec_text(f"{_archive_dir(cik, accession)}/{primary}", timeout=25)
            body = _strip_html(raw)
            styled_headline = _styled_headline(raw)
        except Exception:
            body = ""

    summary = ""
    if body:
        m = re.search(r"(?is)INFORMATION\s+(?:CONTAINED|FURNISHED)[^\n]*\n(.{40,900}?)"
                      r"(?:INCORPORATION BY REFERENCE|INDEX TO EXHIBITS|SIGNATURES?|EXHIBIT INDEX|$)",
                      body)
        if m:
            summary = re.sub(r"\s+", " ", m.group(1)).strip()
        if not summary:
            # Some 6-K covers only carry an exhibit table.
            m = re.search(r"(?is)(?:99\.1|Exhibit\s+99\.1)\s*[​\s]*([^\n]{15,200})", body)
            if m:
                summary = re.sub(r"\s+", " ", m.group(1)).strip()

    excerpt = _exhibit_excerpt(cik, accession, docs) if docs else ""
    if not excerpt and body:
        excerpt = _clean_body(body)
    if excerpt:
        out["body_excerpt"] = excerpt
    if summary:
        out["cover_summary"] = summary[:600]

    # Only the cover statement is a trustworthy one-liner; a raw excerpt can
    # start on an address or a signature block, so it stays in body_excerpt.
    out["headline"] = summary[:400] or styled_headline
    return out


def _enrich_ownership(cik: str, accession: str, form: str) -> dict:
    """SC 13D / 13G — reporting person, stake, amendment number."""
    out = {}
    try:
        xml = _sec_text(f"{_archive_dir(cik, accession)}/primary_doc.xml", timeout=20)
    except Exception:
        return out
    txt = re.sub(r"\s+", " ", xml)

    def tag(name):
        m = re.search(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", txt)
        return unescape(m.group(1)).strip() if m else ""

    raw_names = [unescape(m.group(1)).strip() for m in
                 re.finditer(r"<(?:\w+:)?reportingPersonName>(.*?)</(?:\w+:)?reportingPersonName>", txt)]
    if not raw_names:
        n = tag("filingPersonName")
        if n:
            raw_names = [n]
    names = []
    for n in raw_names:
        if n and n not in names:
            names.append(n)
    percent = tag("classPercent")
    shares = tag("reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
    amendment = tag("amendmentNo")
    event_date = tag("eventDateRequiresFilingThisStatement")
    security = tag("securitiesClassTitle")

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


def _enrich_144(cik: str, accession: str) -> dict:
    out = {}
    try:
        xml = _sec_text(f"{_archive_dir(cik, accession)}/primary_doc.xml", timeout=20)
    except Exception:
        return out
    txt = re.sub(r"\s+", " ", xml)

    def tag(name):
        m = re.search(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", txt)
        return unescape(m.group(1)).strip() if m else ""

    seller = tag("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
    relation = tag("relationshipToIssuer")
    shares = tag("noOfUnitsSold")
    value = tag("aggregateMarketValue")
    sale_date = tag("approxSaleDate")
    nature = tag("natureOfAcquisitionTransaction")

    out.update({k: v for k, v in {
        "seller": seller, "position": relation, "sale_date": sale_date,
        "nature": nature,
    }.items() if v})
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
        n = out.get("shares")
        v = out.get("market_value")
        if n and v:
            val = f"${v/1_000_000:.2f}M" if v >= 1_000_000 else f"${v:,.0f}"
            out["headline"] = f"{seller}{pos} — 拟售 {n:,} 股，约 {val}"
        elif n:
            out["headline"] = f"{seller}{pos} — 拟售 {n:,} 股"
        else:
            out["headline"] = f"{seller}{pos} — 144 减持意向"
        out["summary"] = out["headline"]
    return out


def _enrich_generic(form: str, meta: dict) -> dict:
    out = {}
    label = FORM_LABEL.get(form.upper().strip(), "")
    period = meta.get("reportDate") or ""
    desc = meta.get("primaryDocDescription") or ""
    bits = [b for b in [label, f"报告期 {period}" if period else "",
                        desc if desc and desc.upper() != form.upper() else ""] if b]
    if bits:
        out["headline"] = "，".join(bits)
    if period:
        out["report_date"] = period
    return out


# ------------------------------------------------------------------ dispatch

def enrich_filing(ticker: str, form: str, accession: str, cik: str = None,
                  use_cache: bool = True) -> dict:
    """Return {headline, url, ...form-specific fields}. Never raises."""
    if not accession:
        return {}
    cache = _load_enrich_cache()
    key = accession
    if use_cache and key in cache:
        cached = dict(cache[key])
        cached.pop("_ts", None)
        # Do not make a transient empty response permanent.  Older versions
        # cached URL-only results after an SEC timeout, which recreated the
        # original "no title/body" symptom on every later scan.
        if cached.get("headline") or cached.get("body_excerpt"):
            return cached

    cik = cik or load_cik_map().get(ticker.upper(), "")
    if not cik:
        return {"enrich_error": f"no CIK for {ticker}"}

    meta = load_submissions(cik).get(accession, {})
    form_u = (form or meta.get("form") or "").upper().strip()
    out = {
        "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession.replace('-', '')}/{accession}-index.htm"),
    }
    if meta.get("primaryDocument"):
        out["doc_url"] = f"{_archive_dir(cik, accession)}/{meta['primaryDocument']}"
    if meta.get("reportDate"):
        out["report_date"] = meta["reportDate"]

    try:
        if form_u == "8-K" or form_u.startswith("8-K/"):
            out.update(_enrich_8k(cik, accession, meta))
        elif form_u == "6-K" or form_u.startswith("6-K/"):
            out.update(_enrich_6k(cik, accession, meta))
        elif form_u.startswith(("SC 13", "SCHEDULE 13")):
            out.update(_enrich_ownership(cik, accession, form_u))
        elif form_u == "144":
            out.update(_enrich_144(cik, accession))
        else:
            out.update(_enrich_generic(form_u, meta))
    except Exception as e:  # enrichment must never break a scan
        out["enrich_error"] = f"{type(e).__name__}: {e}"

    if not out.get("headline"):
        out["headline"] = ""
    if out.get("headline") or out.get("body_excerpt"):
        cache[key] = {**out, "_ts": datetime.now().isoformat()}
    else:
        cache.pop(key, None)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("usage: edgar_enrich.py TICKER FORM ACCESSION")
        sys.exit(1)
    try:
        _get_user_agent()
    except EnrichError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    r = enrich_filing(sys.argv[1], sys.argv[2], sys.argv[3], use_cache=False)
    print(json.dumps(r, ensure_ascii=False, indent=2))
