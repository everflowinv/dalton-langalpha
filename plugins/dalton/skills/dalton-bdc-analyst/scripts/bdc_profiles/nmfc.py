"""NMFC (New Mountain Finance Corp) — Custom BDC profile

NMFC has a complex SoI format:
  - Main SoI: ~24 continuation tables per year, 63 columns (triple-spanning)
  - Company name on own row (c0 only, no c6 instrument type)
  - Next row: industry (c0) + first instrument (c6+) + numbers
  - Subsequent rows: more instruments
  - Cost: c49, Fair Value: c55 (in pd.read_html 63-col layout)
  - SLP (Senior Loan Program): separate tables (T132+) with different layout
  - Values in thousands
"""

import re
import requests
import pandas as pd
from io import StringIO
from bs4 import BeautifulSoup

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bdc_analyzer import get_headers

# ── helpers ──────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r'[\u200b\u200c\u200d\ufeff]', '', str(s)).replace(chr(160), ' ').strip()


def _parse_num(s: str) -> float:
    s = _clean(s).replace(',', '').replace('$', '')
    neg = '(' in s
    s = s.replace('(', '').replace(')', '')
    if s in ('', 'nan', '-', '—'):
        return 0
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return 0


_SECTION_KEYWORDS = [
    'non-controlled', 'non-affiliate', 'control investments',
    'funded debt', 'unfunded', 'equity investments',
    'total ', 'subtotal', 'portfolio company',
    'funded investments', 'net assets', 'net asset value',
]


def _is_section_header(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in _SECTION_KEYWORDS)


def _find_year_for_table(tbl, tables_list, table_idx) -> int:
    """Find year from preceding text."""
    for s in tbl.find_all_previous(string=True, limit=300):
        t = _clean(str(s)).lower()
        m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', t)
        if m:
            return int(m.group(2))
    return None


def _detect_columns(df) -> dict:
    """Detect Cost and Fair Value column positions from header row."""
    cost_col = fair_col = principal_col = None
    for j in range(min(3, len(df))):
        for k in range(df.shape[1]):
            v = _clean(str(df.iloc[j, k])).lower()
            if 'cost' in v and 'fair' not in v and cost_col is None:
                cost_col = k
            if 'fair value' in v and fair_col is None:
                fair_col = k
            if 'principal' in v and principal_col is None:
                principal_col = k

    # For NMFC's triple-span: headers are at c48, c49, c50 for Cost
    # Numbers typically in the middle column of each triple
    # Use c+1 as the number column (after $ sign)
    if cost_col is not None:
        cost_col += 1  # 48 → 49
    if fair_col is not None:
        fair_col += 1  # 54 → 55
    if principal_col is not None:
        principal_col += 1  # 42 → 43

    return {
        'cost': cost_col or 49,
        'fair': fair_col or 55,
        'principal': principal_col or 43,
    }


def _is_instrument_row(row, df, cols) -> bool:
    """Row has instrument type (c6+) or numbers in cost/fair columns."""
    c6 = _clean(str(row.iloc[6])) if df.shape[1] > 6 else ''
    has_type = c6 and c6 != 'nan'

    has_nums = False
    for col in [cols['cost'], cols['fair']]:
        if col < df.shape[1]:
            v = _clean(str(row.iloc[col]))
            if v not in ('nan', '', '-', '—', '$') and any(c.isdigit() for c in v):
                has_nums = True
                break

    return has_type or has_nums


def _parse_main_soi(tables, soi_indices, target_year) -> pd.DataFrame:
    """Parse main SoI tables."""
    results = {}
    current_company = None

    for tidx in soi_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        if df.shape[1] < 30:
            continue

        cols = _detect_columns(df)

        for j in range(2, len(df)):
            c0 = _clean(str(df.iloc[j, 0]))

            if c0 and c0 != 'nan':
                if _is_section_header(c0):
                    current_company = None
                    continue

                is_inst = _is_instrument_row(df.iloc[j], df, cols)

                if not is_inst:
                    # This is a company name row (no instrument data)
                    current_company = c0.upper()
                    if current_company not in results:
                        results[current_company] = {'Face': 0, 'Fair': 0}
                    continue

            # Blank c0 rows: only read if they have instrument type text in cols 6-14
            # This avoids double-counting from subtotal rows
            if not c0 or c0 == 'nan':
                has_instrument = False
                for ic in range(6, min(15, df.shape[1])):
                    iv = _clean(str(df.iloc[j, ic]))
                    # Must look like instrument type (First lien, Subordinated, etc.)
                    if iv and iv != 'nan' and len(iv) > 3:
                        low = iv.lower()
                        if any(k in low for k in ['first', 'second', 'senior', 'subordin', 'mezzanine', 'equity', 'preferred', 'common', 'warrant', 'llc interest', 'lp interest']):
                            has_instrument = True
                            break
                if not has_instrument:
                    continue  # subtotal row, skip

            if current_company:
                cost = _parse_num(str(df.iloc[j, cols['cost']])) if cols['cost'] < df.shape[1] else 0
                fair = _parse_num(str(df.iloc[j, cols['fair']])) if cols['fair'] < df.shape[1] else 0
                if cost > 0 or fair > 0:
                    results[current_company]['Face'] += cost
                    results[current_company]['Fair'] += fair

    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])


def _parse_slp_tables(tables, slp_indices) -> pd.DataFrame:
    """Parse NMFC Senior Loan Program (SLP) tables."""
    results = {}

    for tidx in slp_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        # SLP tables have different column layout
        # Find cost/fair columns
        cost_col = fair_col = None
        for j in range(min(3, len(df))):
            for k in range(df.shape[1]):
                v = _clean(str(df.iloc[j, k])).lower()
                if 'cost' in v and 'fair' not in v:
                    cost_col = k
                if 'fair value' in v:
                    fair_col = k

        # SLP uses single columns (not triple-span): numbers are in the detected column
        # But we need to check if it's triple-span too
        if cost_col is None or fair_col is None:
            continue

        # Check if triple-span by looking at adjacent columns
        sample_row = None
        for j in range(3, min(8, len(df))):
            for k in [cost_col, cost_col + 1, cost_col + 2]:
                if k < df.shape[1]:
                    v = _clean(str(df.iloc[j, k]))
                    if v and any(c.isdigit() for c in v):
                        cost_col = k
                        sample_row = j
                        break
            if sample_row:
                break

        for j in range(3, min(8, len(df))):
            for k in [fair_col, fair_col + 1, fair_col + 2]:
                if k < df.shape[1]:
                    v = _clean(str(df.iloc[j, k]))
                    if v and any(c.isdigit() for c in v):
                        fair_col = k
                        break
            if sample_row:
                break

        for j in range(2, len(df)):
            c0 = _clean(str(df.iloc[j, 0]))
            if not c0 or c0 == 'nan':
                continue
            if _is_section_header(c0):
                continue

            cost = _parse_num(str(df.iloc[j, cost_col])) if cost_col < df.shape[1] else 0
            fair = _parse_num(str(df.iloc[j, fair_col])) if fair_col < df.shape[1] else 0

            if cost > 0 or fair > 0:
                key = c0.upper()
                if key not in results:
                    results[key] = {'Face': cost, 'Fair': fair}
                else:
                    results[key]['Face'] += cost
                    results[key]['Fair'] += fair

    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])


def _retry_get(url: str, retries: int = 5, sleep_s: int = 3):
    """Retry HTTP GET with exponential backoff for SEC rate limits."""
    import time
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=get_headers(), timeout=120)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            time.sleep(sleep_s * (attempt + 1))
    raise RuntimeError(f'fetch failed after {retries} retries: {last}')


def parse_period(url: str, target_year: int, target_quarter: int = None) -> pd.DataFrame:
    content = _retry_get(url)
    soup = BeautifulSoup(content, 'lxml')
    tables = soup.find_all('table')

    # Classify tables: main SoI vs SLP vs other
    main_soi_indices = []
    slp_indices = []

    for i, tbl in enumerate(tables):
        txt = _clean(tbl.get_text(' ', strip=True)).lower()[:200]
        rows = len(tbl.find_all('tr'))
        if rows < 10:
            continue

        # Main SoI: "Portfolio Company, Location and Industry"
        if 'portfolio company, location' in txt:
            year = _find_year_for_table(tbl, tables, i)
            if year == target_year:
                main_soi_indices.append(i)

        # SLP: "Portfolio Company and Type of Investment"
        elif 'portfolio company and type of investment' in txt:
            year = _find_year_for_table(tbl, tables, i)
            if year == target_year:
                slp_indices.append(i)

    # Parse both sections
    df_main = _parse_main_soi(tables, main_soi_indices, target_year)
    df_slp = _parse_slp_tables(tables, slp_indices)

    # Combine
    if df_main.empty:
        return df_slp
    if df_slp.empty:
        return df_main

    combined = pd.concat([df_main, df_slp], ignore_index=True)

    # Merge duplicates (same company in main + SLP)
    grouped = combined.groupby('CompanyKey').agg({'Face': 'sum', 'Fair': 'sum'}).reset_index()
    return grouped
