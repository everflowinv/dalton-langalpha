"""
SAR (Saratoga Investment Corp) — Custom BDC profile.

SAR SoI format: Feb fiscal year end. Each company gets its own table.: flat layout, every row has company name + investment type + numbers.
Company name in col 0, Investment Type in col 1, Cost in cols 9-10, Fair Value in cols 17-18.
Multiple tables, each per company. Values in FULL DOLLARS (not thousands)., both 2025 and 2024 sections.
"Schedule of Investments December 31, YYYY" is inside each table's header row.
Section headers (Country, Industry, "Debt Investments", "Equity Investments") have no numbers.
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
    s = re.sub(r'\s*\(\d+\)', '', s)  # remove footnote markers (5) (9) etc
    s = re.sub(r'\([^\)]*\)', ' ', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t not in {
        'LLC', 'INC', 'LTD', 'LP', 'L', 'P', 'CORP', 'CORPORATION', 'CO', 'AND', 'THE',
        'HOLDINGS', 'HOLDING', 'PARENT', 'TOPCO', 'MIDCO', 'BIDCO', 'BLOCKER', 'SERIES',
        'B', 'V', 'S', 'A', 'R'
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
    """CCAP section headers: country names, industry categories, investment type categories."""
    low = name.lower().strip()
    if not low or low == 'nan':
        return True
    # Country names
    if low in ('united states', 'united kingdom', 'australia', 'canada', 'europe',
               'cayman islands', 'netherlands', 'germany', 'france', 'luxembourg',
               'ireland', 'sweden', 'norway', 'denmark', 'israel', 'bermuda'):
        return True
    if is_soi_heading(low):
        return True
    # Section headers
    section_kw = [
        'debt investments', 'equity investments', 'total', 'subtotal', 'sub total',
        'investments', 'crescent capital',
        'country/security', 'investment type', 'interest', 'maturity',
        'principal amount', 'cost', 'fair value', 'percentage',
        'non-control', 'non-affiliate', 'control investments', 'affiliate investments',
        'cash and cash equivalents',
    ]
    for kw in section_kw:
        if low == kw or low.startswith(kw):
            return True
    # Industry-only names (no company indicator like LLC/Inc/Corp/Ltd/Holdings)
    company_indicators = ['llc', 'inc', 'corp', 'ltd', 'l.p.', 'lp', 'holdings', 'group',
                          'partners', 'capital', 'services', 'solutions', 'technology',
                          'enterprises', 'acquisition', 'buyer', 'bidco', 'topco', 'parent',
                          'opco', 'midco', 'pbc', 'gmbh', 'b.v.', 's.a.']
    if not any(ci in low for ci in company_indicators):
        # Could be an industry name like "Automobiles & Components"
        if '&' in low or len(low.split()) <= 3:
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


def parse_period(url: str, target_year: int, target_quarter: int | None = None, use_first_section: bool = True) -> pd.DataFrame:
    """Parse SAR SoI. SAR has two sections in each filing (current period + prior period).
    use_first_section=True extracts the first SoI section (current period).
    use_first_section=False extracts the second SoI section (prior period).
    """
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')
    tables = soup.find_all('table')

    # SAR layout: SoI tables → footnotes → prior period SoI tables → prior footnotes
    # Find the boundary: footnotes section starts with "(a) represents" or "percentages are based on"
    soi_tables_first = []
    soi_tables_second = []
    in_footnotes = False
    found_second_section = False

    for idx, tbl in enumerate(tables):
        txt_head = tbl.get_text(' ', strip=True).lower()[:300]

        # Detect footnotes boundary
        if '(a) represents' in txt_head or 'percentages are based on' in txt_head:
            if not in_footnotes:
                in_footnotes = True
                continue

        # If we hit another "company(1)" after footnotes, it's the second SoI section
        if in_footnotes and 'company(1)' in txt_head and 'cost' in txt_head:
            in_footnotes = False
            found_second_section = True

        if 'company' not in txt_head and 'investment' not in txt_head:
            continue
        if 'cost' not in txt_head and 'fair' not in txt_head:
            continue

        if not found_second_section:
            soi_tables_first.append(tbl)
        else:
            soi_tables_second.append(tbl)

    target_tables = soi_tables_first if use_first_section else soi_tables_second
    all_rows = []

    for tbl in target_tables:
        txt_head = tbl.get_text(' ', strip=True).lower()[:600]

        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:
            continue

        if df.shape[1] < 15 or df.shape[0] < 5:
            continue

        # Detect cost and fair value column positions
        # CCAP standard: cost at 9-10, fair at 17-18
        # But verify from header row
        cost_col = None
        fair_col = None
        for j in range(min(4, len(df))):
            for c in range(df.shape[1]):
                val = str(df.iloc[j, c]).lower().replace('\xa0', ' ').strip()
                if val == 'cost' and cost_col is None:
                    cost_col = c
                if ('fair value' in val or 'fair  value' in val) and fair_col is None:
                    fair_col = c
        
        if cost_col is None:
            cost_col = 13  # SAR fallback
        if fair_col is None:
            fair_col = 17  # SAR fallback

        # Parse data rows (skip only the header row)
        # SAR has header in row 0, data starts at row 1
        for j in range(1, len(df)):
            vals = list(df.iloc[j].values)
            name = str(vals[0]).strip() if not _is_blank(vals[0]) else ''

            if not name or _is_section_header(name):
                continue

            # SAR: investment type may not be in col 1 (could be col 4 or merged)
            # Skip the inv_type check for SAR — rely on company name + cost/fair presence

            # Extract cost and fair value, trying column and +1
            face = None
            for c in [cost_col, cost_col + 1, cost_col - 1]:
                face = _pick_num(vals, c)
                if face is not None:
                    break

            fair = None
            for c in [fair_col, fair_col + 1, fair_col - 1]:
                fair = _pick_num(vals, c)
                if fair is not None:
                    break

            if face is None and fair is None:
                continue

            f = 0.0 if face is None else face
            r = 0.0 if fair is None else fair

            # Normalize company name (remove footnote markers: (5), (d), (h)(i), etc.)
            clean_name = re.sub(r'\s*\([a-z0-9,\s]+\)\s*', ' ', name, flags=re.I).strip()
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
    # Aggregate all tranches by company
    out = out.groupby('CanonKey', as_index=False).agg({'CompanyKey': 'first', 'Face': 'sum', 'Fair': 'sum'})
    return out
