"""SSSS (SuRo Capital Corp) — Custom BDC profile

SSSS has a full SoI with Cost + Fair Value in tables T241+T244+T247 (2025)
and T255+T258+T261 (2024). Structure:
  - Company name row: c0=name, c2=headquarters
  - Instrument row: c0=instrument type, c2=industry, c14=cost, c18=fair value
  - Total row (for multi-instrument): c0="Total", c14=total_cost, c18=total_fair
  - Single-instrument companies have NO total row
  - Values in full dollars
  - Headers contain "Headquarters/ Industry" + "Cost" + "Fair Value"
"""

import re
import requests
import pandas as pd
from io import StringIO
from bs4 import BeautifulSoup

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bdc_analyzer import get_headers


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


_SKIP = [
    'non-controlled', 'non-affiliate', 'controlled investments',
    'total non-controlled', 'total controlled', 'total investments',
    'total portfolio', 'total affiliate', 'total non-affiliate',
    'portfolio investments', 'net assets',
]


def _is_skip(name: str) -> bool:
    low = re.sub(r'\s+', ' ', name).lower().strip()
    return any(low.startswith(k) or low == k for k in _SKIP)


def _find_soi_tables(tables, target_year) -> list:
    """Find SoI tables containing 'Headquarters' + 'Cost' + 'Fair Value' for target year."""
    candidates = []
    for i, tbl in enumerate(tables):
        txt = re.sub(r'\s+', ' ', _clean(tbl.get_text(' ', strip=True))).lower()[:300]
        if 'headquarters' in txt and 'cost' in txt and 'fair value' in txt:
            candidates.append(i)

    if not candidates:
        return []

    # Split into year groups by looking for year in preceding text
    year_groups = {}
    for idx in candidates:
        tbl = tables[idx]
        year = None
        for s in tbl.find_all_previous(string=True, limit=200):
            t = _clean(str(s)).lower()
            m = re.search(r'(december|march|june|september)\s+\d{1,2},\s*(20\d{2})', t)
            if m:
                year = int(m.group(2))
                break
        if year:
            year_groups.setdefault(year, []).append(idx)

    return year_groups.get(target_year, [])


def _detect_cost_fair_cols(df) -> tuple:
    """Find cost and fair value column indices."""
    cost_col = fair_col = None
    for j in range(min(3, len(df))):
        for k in range(df.shape[1]):
            v = re.sub(r'\s+', ' ', _clean(str(df.iloc[j, k]))).lower()
            if 'cost' in v and 'fair' not in v:
                cost_col = k
            if 'fair value' in v:
                fair_col = k

    # For SSSS: header "Cost" is at c11/c13, "Fair Value" at c14/c18
    # The actual numbers are typically +1 or +3 from the label
    # Check a data row to confirm
    return cost_col, fair_col


def parse_period(url: str, target_year: int, target_quarter: int = None) -> pd.DataFrame:
    r = requests.get(url, headers=get_headers(), timeout=120)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')

    soi_indices = _find_soi_tables(tables, target_year)
    if not soi_indices:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    results = {}
    current_company = None
    current_instruments = []  # accumulate instruments for current company

    def _flush_company():
        nonlocal current_company, current_instruments
        if current_company and current_instruments:
            total_cost = sum(c for c, f in current_instruments)
            total_fair = sum(f for c, f in current_instruments)
            if current_company in results:
                results[current_company]['Face'] += total_cost
                results[current_company]['Fair'] += total_fair
            else:
                results[current_company] = {'Face': total_cost, 'Fair': total_fair}
        current_company = None
        current_instruments = []

    for tidx in soi_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        # Find cost and fair columns — detect per table since column count varies
        cost_col_label, fair_col_label = _detect_cost_fair_cols(df)

        # The label is the last of a pair (e.g. c13, c14 both say "Cost")
        # Numbers are at the label position OR label+1 depending on $ sign
        # Find actual number column by checking a data row
        def _find_num_col(label_col, df):
            """Find which column near label_col has actual numbers."""
            if label_col is None:
                return None
            for j in range(2, min(len(df), 15)):
                for offset in [0, -1, 1]:
                    k = label_col + offset
                    if 0 <= k < df.shape[1]:
                        v = _clean(str(df.iloc[j, k]))
                        if v and v not in ('nan', '$', '—', '-') and any(c.isdigit() for c in v):
                            return k
            return label_col

        cost_col = _find_num_col(cost_col_label, df) or 14
        fair_col = _find_num_col(fair_col_label, df) or 18

        for j in range(2, len(df)):
            c0 = _clean(str(df.iloc[j, 0]))
            if not c0 or c0 == 'nan':
                continue

            if _is_skip(c0):
                _flush_company()
                continue

            # Check if this is a "Total" row
            if c0.lower() == 'total':
                # Use total values instead of accumulated instruments
                total_cost = _parse_num(str(df.iloc[j, cost_col])) if cost_col < df.shape[1] else 0
                total_fair = _parse_num(str(df.iloc[j, fair_col])) if fair_col < df.shape[1] else 0
                if current_company and (total_cost > 0 or total_fair > 0):
                    results[current_company] = {'Face': total_cost, 'Fair': total_fair}
                    current_instruments = []  # don't flush accumulated, we used Total
                    current_company = None
                continue

            # Check if this row has numbers (instrument row)
            cost_val = _parse_num(str(df.iloc[j, cost_col])) if cost_col < df.shape[1] else 0
            fair_val = _parse_num(str(df.iloc[j, fair_col])) if fair_col < df.shape[1] else 0
            has_nums = cost_val != 0 or fair_val != 0

            # Check c2 for headquarters (city, state pattern = company row)
            c2 = _clean(str(df.iloc[j, 2])) if df.shape[1] > 2 else ''
            has_location = bool(c2 and re.search(r'[A-Z][a-z]+,\s*[A-Z]{2}', c2))

            if has_location and not has_nums:
                # Company name row
                _flush_company()
                current_company = c0.upper()
                continue

            if has_nums:
                if current_company is None and has_location:
                    # Single-line company (name + numbers on same row... rare for SSSS)
                    _flush_company()
                    current_company = c0.upper()

                if current_company is None:
                    # Instrument without a company — might be a single-instrument company
                    # The company name IS on this row
                    current_company = c0.upper()

                current_instruments.append((cost_val, fair_val))
                continue

            if not has_nums and not has_location:
                # Could be a single-instrument company name
                # Check if NEXT row has numbers
                _flush_company()
                current_company = c0.upper()

    _flush_company()

    if not results:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows)
