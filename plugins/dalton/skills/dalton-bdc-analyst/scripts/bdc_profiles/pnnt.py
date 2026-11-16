"""PNNT (PennantPark Floating Rate Capital) — Custom BDC profile

PNNT uses "Issuer Name" (not "Portfolio Company") and has columns:
  c0: Issuer Name
  c2: Acquisition Date
  c4: Maturity Date
  c6: Industry
  c8/c9: Current Coupon
  c12/c13: Basis Point Spread
  c16/c17: Par / Shares (SHARES for equity, PAR for debt)
  c20/c21: Cost
  c24/c25: Fair Value
Values in thousands. Multiple SoI tables per year (~8-10 continuation tables).
Company rows have industry in c6. Instrument rows below may not.
Some companies have both debt + equity (with share counts in Par column).
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


_SKIP_NAMES = [
    'total ', 'subtotal', 'non-controlled', 'non-affiliate',
    'controlled investments', 'investments in controlled',
    'portfolio company', 'issuer name',
    'funded investments', 'unfunded commitments',
    'equity investments', 'debt investments',
    'subordinated debt', 'subordinated notes',
    'common equity', 'preferred equity',
    'first lien secured debt', 'second lien secured debt',
    'first lien debt', 'second lien debt',
]


def _is_skip(name: str) -> bool:
    low = re.sub(r'\s+', ' ', name).lower().strip()
    return any(low.startswith(k) or low == k for k in _SKIP_NAMES)


def _is_detail_table(tbl, tables) -> bool:
    """Check if a SoI table is a DETAIL table (instrument-level breakdown).
    
    Note: Disabled for PNNT because main SoI tables also contain instrument descriptions
    like "Funded Revolver", "Unfunded Term Loan" etc. The date-based filtering in
    _get_table_period_from_context handles the current vs prior period split.
    """
    # Disabled — PNNT main SoI includes instrument types
    return False


def _get_table_period_from_context(tbl, target_year: int, target_quarter: int) -> bool:
    """Check if table belongs to target period based on surrounding text.
    
    PNNT filings have date markers like "December 31, 2025" or "September 30, 2025"
    in the preceding text. We want only tables matching the target period.
    """
    # Build target date pattern based on quarter
    quarter_months = {
        1: 'march 31',
        2: 'june 30', 
        3: 'september 30',
        4: 'december 31'
    }
    target_date = f"{quarter_months.get(target_quarter, 'december 31')}, {target_year}"
    
    # Look at text before this table.
    # XBRL filings have lots of markup between tables, so we need a wider search.
    prev_text = ''
    for s in tbl.find_all_previous(string=True, limit=300):
        prev_text = str(s).strip() + ' ' + prev_text
    prev_text = re.sub(r'\s+', ' ', prev_text).lower()[:2000]
    
    if target_date in prev_text:
        # Make sure the target date is CLOSER than any other period date
        # to avoid matching tables from a different period section
        target_pos = prev_text.rfind(target_date)
        # Check for prior period dates that might be closer (= between target_date and end of text)
        prior_dates = []
        for y in (target_year, target_year - 1):
            for q, md in quarter_months.items():
                other = f"{md}, {y}"
                if other != target_date:
                    pos = prev_text.rfind(other)
                    if pos > target_pos:
                        prior_dates.append((other, pos))
        if prior_dates:
            # A different period date is closer to this table → not our period
            return False
        return True
    
    return False


def _find_soi_tables(tables, target_year, target_quarter=None) -> list:
    """Find SoI tables for PNNT.
    
    PNNT 10-K has three SoI sections:
    1. Summary SoI for target year (one row per company) — WE WANT THIS
    2. Detail SoI for target year (instrument-level breakdown) — SKIP
    3. Summary SoI for prior year (comparison) — SKIP
    
    PNNT 10-Q has two SoI sections:
    1. Summary SoI for current quarter (e.g., December 31, 2025) — WE WANT THIS
    2. Summary SoI for prior quarter (e.g., September 30, 2025) — SKIP
    
    Strategy:
    1. Find all tables with 'Issuer Name' + 'Cost' + 'Fair Value'
    2. Skip detail tables (contain "Funded Revolver", "Unfunded Term Loan" etc.)
    3. Filter by date context in surrounding text
    """
    # Find all SoI candidate tables
    all_soi = []
    for i, tbl in enumerate(tables):
        txt = re.sub(r'\s+', ' ', _clean(tbl.get_text(' ', strip=True))).lower()[:300]
        if 'issuer name' in txt and 'cost' in txt and 'fair value' in txt and len(tbl.find_all('tr')) > 10:
            all_soi.append(i)
    
    if not all_soi:
        return []
    
    # Filter out detail tables (instrument-level breakdown)
    summary_soi = []
    for tidx in all_soi:
        tbl = tables[tidx]
        if not _is_detail_table(tbl, tables):
            summary_soi.append(tidx)
    
    if not summary_soi:
        # Fallback: if no summary tables found, use all (shouldn't happen)
        summary_soi = all_soi
    
    # Filter by date context - only keep tables for target period
    if target_quarter:
        filtered = []
        for tidx in summary_soi:
            if _get_table_period_from_context(tables[tidx], target_year, target_quarter):
                # Also check if this is a PSLF detail table (in Notes section)
                # PSLF tables are preceded by "pslf's individual investments" text
                # Use narrow search (50 elements) for PSLF since it's in immediate heading
                pslf_prev = ''
                for s in tables[tidx].find_all_previous(string=True, limit=50):
                    pslf_prev = str(s).strip() + ' ' + pslf_prev
                pslf_prev = re.sub(r'\s+', ' ', pslf_prev).lower()
                
                # Skip if this is a PSLF detail table
                if "pslf's individual" in pslf_prev or "pslf" in pslf_prev:
                    continue
                    
                # For notes filter, use wider search
                prev_text = ''
                for s in tables[tidx].find_all_previous(string=True, limit=150):
                    prev_text = str(s).strip() + ' ' + prev_text
                prev_text = re.sub(r'\s+', ' ', prev_text).lower()[:1200]
                # Skip if this is in the actual Notes section
                # Notes section tables have "notes to consolidated financial statements" immediately followed by "continued"
                # e.g., "notes to consolidated financial statements—(continued)" or "notes to consolidated financial statements (unaudited) - continued"
                # But NOT "see notes to consolidated financial statements" (page footer) + "schedule of investments - continued" (main SoI)
                notes_pos = prev_text.find('notes to consolidated financial statements')
                if notes_pos >= 0:
                    # Check if "continued" appears close to "notes to..." (within 100 chars)
                    after_notes = prev_text[notes_pos:notes_pos+100]
                    if 'continued' in after_notes:
                        continue
                    
                filtered.append(tidx)
        if filtered:
            return filtered
    
    # Fallback: use old logic if date filtering doesn't work.
    # Strategy: find where the prior period section starts by detecting a prior date
    # in the table text or preceding text. Take only tables BEFORE that point.
    if len(summary_soi) <= 1:
        return summary_soi
    
    # Method 1: detect prior period date in table or immediate preceding text
    quarter_months = {1: 'march 31', 2: 'june 30', 3: 'september 30', 4: 'december 31'}
    prior_dates = set()
    for y in (target_year, target_year - 1, target_year - 2):
        for q, md in quarter_months.items():
            d = f"{md}, {y}"
            if target_quarter and d == f"{quarter_months.get(target_quarter, '')}, {target_year}":
                continue  # skip the target date itself
            prior_dates.add(d)
    
    split_idx = len(summary_soi)
    first_table_seen = False
    for pos, tidx in enumerate(summary_soi):
        tbl = tables[tidx]
        # Check preceding text (narrow: 50 elements, ~immediate heading)
        prev_text = ''
        for s in tbl.find_all_previous(string=True, limit=50):
            prev_text = str(s).strip() + ' ' + prev_text
        prev_text = re.sub(r'\s+', ' ', prev_text).lower()[:600]
        
        for pd_str in prior_dates:
            if pd_str in prev_text:
                # Found a prior period date in the immediate heading → split here
                if first_table_seen:
                    split_idx = pos
                    break
        if split_idx < len(summary_soi):
            break
        first_table_seen = True
    
    # Method 2 (original): split at second "Investments in Non-Controlled" header
    if split_idx == len(summary_soi):
        non_controlled_count = 0
        for pos, tidx in enumerate(summary_soi):
            tbl = tables[tidx]
            txt = re.sub(r'\s+', ' ', _clean(tbl.get_text(' ', strip=True))).lower()[:200]
            if 'investments in non-controlled' in txt or 'non-controlled/non-affiliate' in txt:
                non_controlled_count += 1
                if non_controlled_count == 2:
                    split_idx = pos
                    break
    
    return summary_soi[:split_idx]


def _detect_cols(df) -> dict:
    """Detect Cost and Fair Value column positions."""
    cost_col = fair_col = par_col = None
    for j in range(min(3, len(df))):
        for k in range(df.shape[1]):
            v = re.sub(r'\s+', ' ', _clean(str(df.iloc[j, k]))).lower()
            if 'cost' in v and 'fair' not in v and cost_col is None:
                cost_col = k
            if 'fair value' in v and fair_col is None:
                fair_col = k
            if ('par' in v or 'shares' in v) and par_col is None:
                par_col = k

    # Numbers are in the RIGHT column of each pair
    # header at c20/c21 both say "Cost" — number is at c21
    if cost_col is not None:
        cost_col += 1
    if fair_col is not None:
        fair_col += 1
    if par_col is not None:
        par_col += 1

    return {
        'cost': cost_col or 21,
        'fair': fair_col or 25,
        'par': par_col or 17,
    }


def parse_period(url: str, target_year: int, target_quarter: int = None) -> pd.DataFrame:
    r = requests.get(url, headers=get_headers(), timeout=120)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')

    soi_indices = _find_soi_tables(tables, target_year, target_quarter)
    if not soi_indices:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    results = {}
    current_company = None

    for tidx in soi_indices:
        tbl = tables[tidx]
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except:
            continue

        cols = _detect_cols(df)
        cost_col = cols['cost']
        fair_col = cols['fair']

        # Detect industry column dynamically from header row
        industry_col = 6  # default
        for hr in range(min(3, len(df))):
            for kk in range(df.shape[1]):
                hv = re.sub(r'\s+', ' ', str(df.iloc[hr, kk])).lower().strip()
                if hv == 'industry' or hv.startswith('industry'):
                    industry_col = kk
                    break

        for j in range(2, len(df)):
            c0 = _clean(str(df.iloc[j, 0]))
            if not c0 or c0 == 'nan':
                continue
            if _is_skip(c0):
                current_company = None
                continue

            # Check if this row has industry text (= company row or combined row)
            c_ind = _clean(str(df.iloc[j, industry_col])) if industry_col < df.shape[1] else ''
            has_industry = c_ind and c_ind != 'nan' and not c_ind.replace('%', '').replace('.', '').replace('-', '').replace('—', '').strip().isdigit()

            # Get Cost and Fair values
            cost = _parse_num(str(df.iloc[j, cost_col])) if cost_col < df.shape[1] else 0
            fair = _parse_num(str(df.iloc[j, fair_col])) if fair_col < df.shape[1] else 0

            if has_industry:
                # New company
                if current_company and current_company in results:
                    pass  # keep accumulated
                current_company = c0.upper()
                if current_company not in results:
                    results[current_company] = {'Face': 0, 'Fair': 0}

                # This row may also have instrument data
                if cost != 0 or fair != 0:
                    results[current_company]['Face'] += cost
                    results[current_company]['Fair'] += fair
            else:
                # Continuation instrument row for current company
                if current_company and (cost != 0 or fair != 0):
                    if current_company not in results:
                        results[current_company] = {'Face': 0, 'Fair': 0}
                    results[current_company]['Face'] += cost
                    results[current_company]['Fair'] += fair

    if not results:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

    rows = [{'CompanyKey': k, 'Face': v['Face'], 'Fair': v['Fair']} for k, v in results.items()]
    return pd.DataFrame(rows)
