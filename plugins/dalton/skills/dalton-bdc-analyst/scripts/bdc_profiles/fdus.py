"""
FDUS (Fidus Investment Corp) — Custom BDC profile.

FDUS SoI format: company name + industry in one row, instrument lines (with numbers) below.
Both company and instrument names are in col 0. Distinguish by: company rows have Industry (col 1),
instrument rows have blank Industry.
Cost at cols 10-11, Fair Value at cols 14-15.
"""
import re
import time
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

from bdc_analyzer import (
    is_soi_heading,
    get_headers,
    clean_num,
)

import warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def _is_blank(v):
    s = '' if pd.isna(v) else str(v).strip()
    return s == '' or s.lower() in ('nan', 'none', '—', '-')


def _collapse_repeated_name(s: str) -> str:
    s = re.sub(r'\s+', ' ', str(s)).strip().upper()
    s = re.split(r'\s[—–-]\s', s)[0]
    return s


def _canon_key(name: str) -> str:
    s = _collapse_repeated_name(name)
    s = re.sub(r'\([^\)]*\)', ' ', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t not in {
        'LLC', 'INC', 'LTD', 'LP', 'L', 'P', 'CORP', 'CORPORATION', 'CO', 'AND', 'THE',
        'HOLDINGS', 'HOLDING', 'PARENT', 'TOPCO', 'MIDCO', 'BIDCO', 'BLOCKER', 'SERIES'
    }]
    return ' '.join(toks[:4])


def _clean_display_name(name: str) -> str:
    s = _collapse_repeated_name(name)
    s = re.sub(r'\(\d+\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,;')
    return s


def _display_name_key(name: str) -> str:
    s = _clean_display_name(name).upper()
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_company_name(name: str, industry: str) -> bool:
    """FDUS: company rows have a non-blank Industry column."""
    if _is_blank(industry):
        return False
    low = name.lower()
    # Skip section headers
    if any(k in low for k in ['total', 'subtotal', 'control investment', 'affiliate investment',
                               'non-control', 'non-affiliate']) or is_soi_heading(low):
        return False
    return True


def _is_instrument_name(name: str) -> bool:
    """FDUS: instrument rows contain keywords like Debt, Equity, Warrant, Revolver."""
    low = name.lower()
    return any(k in low for k in ['debt', 'equity', 'warrant', 'preferred', 'common',
                                   'lien', 'subordinated', 'revolver', 'revolving',
                                   'mezzanine', 'unitranche', 'membership interest'])


def _retry_get_content(url: str, retries: int = 4, sleep_s: int = 3):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=get_headers(), timeout=120)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            time.sleep(sleep_s)
    raise RuntimeError(f'fetch failed after retries: {last}')


def _pick_num(vals, idx):
    if idx >= len(vals):
        return None
    v = clean_num(vals[idx])
    if pd.isna(v):
        return None
    return float(v)


def _period_label(target_year: int, target_quarter: int):
    md = {1: 'march 31', 2: 'june 30', 3: 'september 30', 4: 'december 31'}.get(target_quarter)
    if not md:
        return None
    return f"{md}, {target_year}"


def _table_matches_period(tbl, target_year: int, target_quarter: int | None):
    if target_quarter is None:
        return True
    label = _period_label(target_year, target_quarter)
    if not label:
        return True
    table_txt = tbl.get_text(' ', strip=True).lower().replace('\xa0', ' ')[:500]
    if label in table_txt:
        return True
    for s in tbl.find_all_previous(string=True, limit=260):
        t = ' '.join(str(s).split()).lower()
        if label in t:
            return True
    return False


def _infer_table_context_year(tbl):
    year_dist = None
    year_val = None
    soi_dist = None
    for i, s in enumerate(tbl.find_all_previous(string=True, limit=400)):
        t = ' '.join(str(s).split())
        if not t:
            continue
        low = t.lower()
        if soi_dist is None and is_soi_heading(low):
            soi_dist = i
        if year_dist is None:
            m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', low)
            if m:
                year_dist = i
                year_val = int(m.group(2))
        if soi_dist is not None and year_dist is not None:
            break
    if soi_dist is None or year_val is None:
        return None
    return year_val


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')
    tables = soup.find_all('table')

    all_rows = []

    # Build candidates — FDUS splits SoI across multiple continuation tables
    # First find "header tables" (have "portfolio company" + "cost" + "fair")
    # Then include adjacent continuation tables with same column count
    header_tables = set()
    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()[:300]
        if 'portfolio company' in txt and ('cost' in txt or 'fair' in txt):
            if 'gross additions' in txt or 'follow-on' in txt:
                continue
            header_tables.add(idx)

    candidates = []
    for idx, tbl in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        if idx in header_tables:
            year = _infer_table_context_year(tbl)
            candidates.append((idx, tbl, tbl.get_text(' ', strip=True).lower(), year, False))
        elif df.shape[1] >= 15 and df.shape[0] >= 10:
            # Check if this is a continuation of a nearby header table
            is_continuation = False
            for h in header_tables:
                if 0 < idx - h <= 5:
                    is_continuation = True
                    break
            if is_continuation:
                nearest_header = max(h for h in header_tables if h < idx)
                year = _infer_table_context_year(tables[nearest_header])
                candidates.append((idx, tbl, tbl.get_text(' ', strip=True).lower(), year, True))  # True = continuation

    known = [(i, y) for i, _, _, y, _ in candidates if y is not None]
    resolved = []
    for i, tbl, txt, y, is_cont in candidates:
        if y is not None:
            resolved.append((i, tbl, txt, y, is_cont))
            continue
        if not known:
            continue
        nearest = min(known, key=lambda p: abs(p[0] - i))
        if abs(nearest[0] - i) <= 15:
            resolved.append((i, tbl, txt, nearest[1], is_cont))

    for _, tbl, txt, ctx_year, is_cont in resolved:
        if ctx_year != target_year:
            continue
        # Skip period matching for continuation tables (they inherit from header)
        if not is_cont and not _table_matches_period(tbl, target_year, target_quarter):
            continue

        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:
            continue

        if df.shape[1] < 15 or df.shape[0] < 10:
            continue

        # FDUS layout: col 0 = company/instrument, col 1 = industry
        # Cost at cols 10-11, Fair at cols 14-15
        current_company = None
        detail = {}

        for _, row in df.iterrows():
            vals = list(row.values)
            name = str(vals[0]).strip() if not _is_blank(vals[0]) else ''
            industry = str(vals[1]).strip() if len(vals) > 1 and not _is_blank(vals[1]) else ''

            if not name:
                continue

            # Check if this is a company row (has industry) or instrument row
            if _is_company_name(name, industry):
                current_company = _collapse_repeated_name(name)
                continue

            if current_company is None:
                continue

            # Must be an instrument row — try to extract cost and fair value
            if not _is_instrument_name(name):
                # Might be a subtotal or section header
                if 'total' in name.lower():
                    continue
                continue

            # Try cost cols 10, 11
            face = None
            for c in [11, 10]:
                face = _pick_num(vals, c)
                if face is not None:
                    break

            # Try fair cols 14, 15
            fair = None
            for c in [15, 14]:
                fair = _pick_num(vals, c)
                if fair is not None:
                    break

            if face is None and fair is None:
                continue

            f = 0.0 if face is None else face
            r = 0.0 if fair is None else fair

            a, b = detail.get(current_company, (0.0, 0.0))
            detail[current_company] = (a + f, b + r)

        for name, (f, r) in detail.items():
            if f > 0:
                all_rows.append({
                    'CanonKey': _canon_key(name),
                    'CompanyKey': name,
                    'Face': f,
                    'Fair': r,
                })

    if not all_rows:
        return pd.DataFrame(columns=['CanonKey', 'CompanyKey', 'Face', 'Fair'])

    out = pd.DataFrame(all_rows)
    out = out[out['CanonKey'].str.len() > 0]
    out = out.groupby('CanonKey', as_index=False).agg({'CompanyKey': 'first', 'Face': 'sum', 'Fair': 'sum'})
    return out
