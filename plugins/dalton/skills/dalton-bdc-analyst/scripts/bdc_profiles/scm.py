"""SCM (Stellus Capital Investment Corp) — Custom BDC profile

Stellus uses heavily formatted iXBRL with zero-width spaces throughout.
SoI spans ~12 continuation tables per year (T242-T253 for 2025, T286-T296 for 2024).
Key differences from generic parser:
  - Col 0: Company name OR instrument type (mixed in same column)
  - Col 4: Security type ("First Lien", "Equity", etc.) — ONLY on instrument rows
  - Col 19: Headquarters/Industry (city, state) — ONLY on company rows
  - Col 22: Principal Amount / Shares — ambiguous (shares for equity, dollars for debt)
  - Col 25: Amortized Cost — USE THIS as Face Value equivalent
  - Col 28: Fair Value
  - Company rows identified by location pattern in col 19 (City, ST)
  - All values in full dollars (not thousands)
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
    """Remove zero-width spaces and normalize whitespace."""
    return re.sub(r'[\u200b\u200c\u200d\ufeff]', '', str(s)).replace(chr(160), ' ').strip()


def _parse_num(s: str) -> float:
    s = _clean(s).replace(',', '').replace('$', '')
    neg = '(' in s
    s = s.replace('(', '').replace(')', '')
    if s in ('', 'nan', '-'):
        return 0
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return 0


def _is_company_row(row, df) -> bool:
    """Company rows have location (City, ST) in col 19."""
    if df.shape[1] <= 19:
        return False
    c19 = _clean(str(row.iloc[19]))
    if c19 in ('', 'nan'):
        return False
    return bool(re.search(r'[A-Za-z]+,\s*[A-Z]{2}', c19))


def _is_skip_row(name: str) -> bool:
    low = name.lower()
    skip_keywords = [
        'control investment', 'non-control', 'non-affiliate', 'total',
        'principal', 'footnotes', 'amortized', 'liabilities', 'net assets',
        'investments', 'security', 'coupon', 'floor', 'cash', 'pik',
        'acquisition', 'headquarters', 'maturity',
    ]
    return any(k in low for k in skip_keywords)


def _find_soi_tables_by_year(tables: list) -> dict:
    """Find all SoI tables grouped by year.
    Returns dict: {year: [table_index, ...]}
    
    SCM repeats headers in every continuation table, so we can't distinguish
    "header" from "continuation" — just find all wide tables with SoI keywords
    and group by year from preceding text.
    """
    year_tables = {}  # year -> [indices]
    
    for i, tbl in enumerate(tables):
        txt = _clean(tbl.get_text(' ', strip=True)).lower()[:300]
        if not ('investment' in txt and 'principal' in txt):
            continue
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue
        if df.shape[1] < 28 or df.shape[0] < 20:
            continue
        
        # Find year from preceding text
        year = None
        for s in tbl.find_all_previous(string=True, limit=300):
            t = _clean(str(s)).lower()
            m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', t)
            if m:
                year = int(m.group(2))
                break
        
        if year:
            year_tables.setdefault(year, []).append(i)
    
    return year_tables


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    """Parse SCM SoI for a given period."""
    r = requests.get(url, headers=get_headers(), timeout=120)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')
    
    year_tables = _find_soi_tables_by_year(tables)
    
    target_indices = year_tables.get(target_year, [])
    if not target_indices:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])
    
    # Parse all tables for target year
    results = {}
    current_company = None
    
    for tidx in target_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue
        
        for j in range(len(df)):
            c0 = _clean(str(df.iloc[j, 0]))
            if c0 in ('', 'nan'):
                continue
            if _is_skip_row(c0):
                continue
            
            if _is_company_row(df.iloc[j], df):
                current_company = c0.upper()
                if current_company not in results:
                    results[current_company] = {'Face': 0, 'Fair': 0}
                continue
            
            # Instrument row — use Cost (col 25) as Face, Fair Value (col 28) as Fair
            if df.shape[1] > 28 and current_company:
                cost = _parse_num(str(df.iloc[j, 25]))
                fair = _parse_num(str(df.iloc[j, 28]))
                if cost > 0 or fair > 0:
                    results[current_company]['Face'] += cost
                    results[current_company]['Fair'] += fair
    
    if not results:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])
    
    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows)
