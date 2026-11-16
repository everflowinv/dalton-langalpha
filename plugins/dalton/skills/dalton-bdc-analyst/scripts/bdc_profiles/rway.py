"""
RWAY (Runway Growth Finance Corp) — Custom BDC profile.

RWAY SoI format: 27-column tables, company names in col 2 (not col 0).
Col 0-2 = Portfolio Company (spans 3 cols), Col 4 = Investment Type,
Col 6 = Investment Description, Col 12-13 = Principal, Col 16-18 = Cost ($),
Col 21-23 = Fair Value ($). Values in thousands.
Industry names appear as section headers spanning cols 2-6.
Multiple tables per period, split by page.
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


def _canon_key(name: str) -> str:
    s = re.sub(r'\s+', ' ', str(name)).strip().upper()
    s = re.sub(r'\s*\([^)]*\)', '', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t not in {
        'LLC', 'INC', 'LTD', 'LP', 'L', 'P', 'CORP', 'CORPORATION', 'CO', 'AND', 'THE',
        'HOLDINGS', 'HOLDING', 'PARENT', 'TOPCO', 'MIDCO', 'BIDCO', 'BLOCKER', 'SERIES'
    }]
    return ' '.join(toks[:4])


def _clean_display_name(name: str) -> str:
    s = re.sub(r'\s+', ' ', str(name)).strip().upper()
    s = re.sub(r'\s*\(\d+\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,;')
    return s


def _display_name_key(name: str) -> str:
    s = _clean_display_name(name)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_section_header(name: str) -> bool:
    """RWAY section headers: industry names, investment categories."""
    low = name.lower().strip()
    if not low or low == 'nan':
        return True
    skip_kw = [
        'total', 'subtotal', 'sub total', 'debt investments', 'equity investments',
        'non-control', 'non-affiliate', 'affiliate investments', 'control investments',
        'portfolio company', 'investment type', 'investment description',
        'cash and cash equivalents', 'fair value at',
    ]
    for kw in skip_kw:
        if low.startswith(kw) or low == kw:
            return True
    # Industry names: no company suffix, typically 2-4 words with & or spaces
    company_suffixes = ['llc', 'inc', 'ltd', 'l.p.', 'lp', 'corp', 'gmbh', 'b.v.', 's.a.',
                        'holdings', 'group', 'partners', 'solutions', 'services']
    if not any(s in low for s in company_suffixes):
        # Likely an industry header like "Application Software" or "Health Care Equipment & Services"
        if len(low.split()) <= 5:
            return True
    return False


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


def _infer_table_context_year(tbl):
    # Check table's own text
    table_txt = tbl.get_text(' ', strip=True).lower()[:600]
    if is_soi_heading(table_txt):
        m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', table_txt)
        if m:
            return int(m.group(2))
    # Check preceding text
    year_val = None
    soi_dist = None
    for i, s in enumerate(tbl.find_all_previous(string=True, limit=400)):
        t = ' '.join(str(s).split()).lower()
        if not t:
            continue
        if soi_dist is None and is_soi_heading(t):
            soi_dist = i
        if year_val is None:
            m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', t)
            if m:
                year_val = int(m.group(2))
        if soi_dist is not None and year_val is not None:
            break
    if soi_dist is None or year_val is None:
        return None
    return year_val


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


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')
    tables = soup.find_all('table')

    all_rows = []

    # Build candidates
    candidates = []
    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()
        if 'portfolio company' not in txt and 'company' not in txt:
            continue
        if 'cost' not in txt and 'fair' not in txt:
            continue
        # Skip summary/roll-forward tables
        txt_head = txt[:300]
        if 'gross additions' in txt_head or 'forgone' in txt_head or 'geographic region' in txt_head:
            continue
        candidates.append((idx, tbl, txt, _infer_table_context_year(tbl)))

    known = [(i, y) for i, _, _, y in candidates if y is not None]
    resolved = []
    for i, tbl, txt, y in candidates:
        if y is not None:
            resolved.append((i, tbl, txt, y))
            continue
        if not known:
            continue
        nearest = min(known, key=lambda p: abs(p[0] - i))
        if abs(nearest[0] - i) <= 15:
            resolved.append((i, tbl, txt, nearest[1]))

    for _, tbl, txt, ctx_year in resolved:
        if ctx_year != target_year:
            continue
        if not _table_matches_period(tbl, target_year, target_quarter):
            continue

        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:
            continue

        if df.shape[1] < 20 or df.shape[0] < 5:
            continue

        # RWAY layout: company at col 2, type at col 4, cost at col 18, fair at col 23
        # Detect dynamically from header
        cost_col = 18  # default
        fair_col = 23  # default
        for j in range(min(3, len(df))):
            for c in range(df.shape[1]):
                val = str(df.iloc[j, c]).lower().replace('\xa0', ' ').strip()
                if 'cost' in val and '($)' in val and cost_col == 18:
                    cost_col = c
                if 'fair value' in val and fair_col == 23:
                    fair_col = c

        for j in range(3, len(df)):
            vals = list(df.iloc[j].values)

            # Company name is in col 2
            name = str(vals[2]).strip() if len(vals) > 2 and not _is_blank(vals[2]) else ''
            inv_type = str(vals[4]).strip() if len(vals) > 4 and not _is_blank(vals[4]) else ''

            if not name:
                continue
            if _is_section_header(name):
                continue

            # Must have investment type to be a data row (not section header)
            if not inv_type:
                continue

            # Extract cost and fair value (try +0, +1, +2 for multi-span columns)
            face = None
            for c in [cost_col, cost_col + 1, cost_col + 2]:
                face = _pick_num(vals, c)
                if face is not None:
                    break

            fair = None
            for c in [fair_col, fair_col + 1, fair_col + 2]:
                fair = _pick_num(vals, c)
                if fair is not None:
                    break

            if face is None and fair is None:
                continue

            f = 0.0 if face is None else face
            r = 0.0 if fair is None else fair

            # Clean company name
            clean_name = re.sub(r'\s*\*+\s*$', '', name).strip()
            clean_name = re.sub(r'\s+', ' ', clean_name).upper()

            all_rows.append({
                'CanonKey': _canon_key(clean_name),
                'CompanyKey': clean_name,
                'Face': f,
                'Fair': r,
            })

    if not all_rows:
        return pd.DataFrame(columns=['CanonKey', 'CompanyKey', 'Face', 'Fair'])

    out = pd.DataFrame(all_rows)
    out = out[out['CanonKey'].str.len() > 0]
    out = out.groupby('CanonKey', as_index=False).agg({'CompanyKey': 'first', 'Face': 'sum', 'Fair': 'sum'})
    return out
