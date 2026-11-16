"""OXSQ (Oxford Square Capital Corp) — Custom BDC profile

OXSQ is a CLO-focused BDC. Its SoI has:
  - Company names on text-only rows (no numbers)
  - Instrument details on the next row with numbers
  - Section headers: "Senior Secured Notes", "CLO Subordinated Notes", etc.
  - Industry headers: "Business Services", "Healthcare", etc.
  - "Total X" summary rows
  - Values in full dollars
  - Column mapping: c5=Principal, c8=Cost, c11=Fair Value (in pd.read_html)
  - Uses "COMPANY/INVESTMENT" header, not "Portfolio Company"
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


_SECTION_HEADERS = {
    'senior secured notes', 'clo subordinated notes', 'clo equity notes',
    'unsecured notes', 'equity/other', 'clo notes', 'subordinated notes',
    'structured finance', 'collateralized loan obligations',
    'common stock', 'preferred stock', 'common equity warrants',
    'cash equivalents', 'cash and cash equivalents',
    'senior secured notes –',
}

_INDUSTRY_HEADERS = {
    'business services', 'healthcare', 'food and beverage', 'industrials',
    'media', 'technology', 'telecommunications', 'energy', 'consumer',
    'financial services', 'chemicals', 'education', 'insurance',
    'real estate', 'retail', 'transportation', 'utilities', 'diversified',
    'aerospace and defense', 'automotive', 'banking', 'gaming',
    'artificial intelligence', 'it consulting', 'software',
}


def _is_company_row(c0: str, has_nums: bool) -> bool:
    """Company rows: have text in c0, NO numbers, NOT section/industry/total."""
    if has_nums or not c0 or c0 == 'nan':
        return False
    low = c0.lower().strip()
    if low.startswith('total ') or low.startswith('subtotal'):
        return False
    if low in _SECTION_HEADERS or low in _INDUSTRY_HEADERS:
        return False
    # Skip headers
    if 'company/investment' in low or 'acquisition date' in low:
        return False
    # Must have at least 2 words or company suffix
    words = low.split()
    if len(words) <= 1 and not any(s in low for s in [',', 'llc', 'inc', 'ltd', 'corp']):
        return False
    return True


def _infer_year_from_context(tbl) -> int:
    """Find year from preceding text."""
    for s in tbl.find_all_previous(string=True, limit=300):
        t = _clean(str(s)).lower()
        m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', t)
        if m:
            return int(m.group(2))
    return None


def parse_period(url: str, target_year: int, target_quarter: int = None) -> pd.DataFrame:
    r = requests.get(url, headers=get_headers(), timeout=120)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')

    # Find SoI tables: header contains "COMPANY/INVESTMENT"
    soi_indices = []
    for i, tbl in enumerate(tables):
        txt = _clean(tbl.get_text(' ', strip=True)).lower()[:200]
        if 'company/investment' in txt and ('cost' in txt or 'fair' in txt):
            try:
                df = pd.read_html(StringIO(str(tbl)))[0]
            except:
                continue
            if df.shape[0] >= 3 and df.shape[1] >= 10:
                year = _infer_year_from_context(tbl)
                if year == target_year:
                    soi_indices.append(i)

    if not soi_indices:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    # Parse all SoI tables
    results = {}
    current_company = None

    for tidx in soi_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        # Detect cost/fair columns from header row
        # OXSQ uses paired columns: c7="COST" c8="COST", but numbers are in c8
        # So we find the LAST column with "COST"/"FAIR VALUE" label
        cost_col, fair_col = None, None
        for j in range(min(3, len(df))):
            for k in range(df.shape[1]):
                v = _clean(str(df.iloc[j, k])).lower()
                if 'cost' in v and 'fair' not in v:
                    cost_col = k  # keep updating → gets the rightmost
                if 'fair value' in v:
                    fair_col = k
        if cost_col is None:
            cost_col = 8
        if fair_col is None:
            fair_col = 11

        for j in range(1, len(df)):
            c0 = _clean(str(df.iloc[j, 0]))
            if c0 in ('', 'nan'):
                continue

            # Check if row has numbers
            has_nums = False
            for k in [cost_col, fair_col]:
                if k < df.shape[1]:
                    v = _clean(str(df.iloc[j, k]))
                    if v not in ('nan', '', '-', '—') and any(ch.isdigit() for ch in v):
                        has_nums = True
                        break

            if _is_company_row(c0, has_nums):
                current_company = c0.upper()
                # Clean up footnote markers
                current_company = re.sub(r'\s*\([^)]*\)\s*$', '', current_company).strip()
                if current_company not in results:
                    results[current_company] = {'Face': 0, 'Fair': 0}
            elif has_nums and current_company:
                # Skip "Total" rows
                if c0.lower().startswith('total '):
                    continue
                cost = _parse_num(str(df.iloc[j, cost_col])) if cost_col < df.shape[1] else 0
                fair = _parse_num(str(df.iloc[j, fair_col])) if fair_col < df.shape[1] else 0
                if cost > 0 or fair > 0:
                    results[current_company]['Face'] += cost
                    results[current_company]['Fair'] += fair

    if not results:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows)
