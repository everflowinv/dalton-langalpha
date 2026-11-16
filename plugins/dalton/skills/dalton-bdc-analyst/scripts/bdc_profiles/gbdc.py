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


def _is_blank(v):
    s = '' if pd.isna(v) else str(v).strip()
    return s == '' or s.lower() in ('nan', 'none', '—', '-')


def _collapse_repeated_name(s: str) -> str:
    s = re.sub(r'\s+', ' ', str(s)).strip().upper()
    s = re.split(r'\s[—–-]\s', s)[0]
    words = s.split()
    if len(words) >= 6:
        n = len(words)
        for k in range(n // 3, n // 2 + 1):
            if words[:k] == words[k:2 * k]:
                return ' '.join(words[:k])
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
    # remove footnote markers like (3)(4)(9)
    s = re.sub(r'\(\d+\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,;')
    return s


def _display_name_key(name: str) -> str:
    s = _clean_display_name(name).upper()
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_summary_like_name(name: str) -> bool:
    n = _clean_display_name(name).upper()
    if not n:
        return True
    patterns = [
        r'^TOTAL\b', r'^SUBTOTAL\b', r'^NET\b',
        r'\bDEBT INVESTMENTS\b', r'\bEQUITY INVESTMENTS\b',
        r'\bFIRST LIEN\b', r'\bSECOND LIEN\b',
        r'\bSENIOR SECURED\b', r'\bPREFERRED STOCK\b', r'\bPREFERRED SHARES\b',
        r'\bLP INTEREST\b', r'\bREVOLVING LINE OF CREDIT\b',
        r'\bASSET BASED\b', r'\bALL ASSETS\b',
        r'\bCONTROL INVESTMENTS\b', r'\bNON-CONTROL\b', r'\bNON-AFFILIATE\b',
        r'\bPURCHASES OF INVESTMENTS\b', r'\bSALES OF INVESTMENTS\b',
        r'\bINTEREST EXPENSE\b', r'\bINTEREST INCOME\b',
        r'\bBORROWINGS\b', r'\bNOTES PAYABLE\b',
        r'^CASH$', r'^CASH AND CASH EQUIVALENTS\b', r'^CASH EQUIVALENTS$',
        r'^EQUITY$', r'^EQUITY INTERESTS IN\b',
        r'^COMMON STOCK$', r'^PREFERRED STOCK$', r'^MEMBERSHIP INTEREST$',
        r'^UNSECURED$', r'^WARRANT$', r'^LP UNITS$',
        r'\bSUBORDINATED DEBT\b', r'\bSUBORDINATED NOTES\b', r'\bSUBORDINATED STRUCTURED\b',
        r'\bUNREALIZED GAIN\b', r'\bUNREALIZED LOSS\b',
        r'\bCHANGE IN UNREALIZED\b', r'\bCHANGES IN UNREALIZED\b',
        r'^SENIOR SECURED NOTES\b',
        r'^NON-AFFILIATED$', r'^AFFILIATED$',
        r'^ONE STOP$', r'^BUILDING PRODUCTS$',
        r'^ARTIFICIAL INTELLIGENCE$', r'^IT CONSULTING$',
    ]
    for p in patterns:
        if re.search(p, n):
            return True
    if len(n.split()) <= 3 and any(k in n for k in ['DEBT', 'CREDIT', 'INVESTMENTS', 'STOCK', 'INTEREST', 'SECURED']):
        return True
    return False



def _retry_get_content(url: str, retries: int = 6, sleep_s: int = 3):
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


def _detect_colmap(df: pd.DataFrame):
    """Detect key columns and candidate amount blocks for BXSL SoI tables."""
    if df.shape[1] < 20:
        return None

    tops = []
    for c in range(df.shape[1]):
        t0 = str(df.iloc[0, c]) if len(df) > 0 else ''
        t1 = str(df.iloc[1, c]) if len(df) > 1 else ''
        t2 = str(df.iloc[2, c]) if len(df) > 2 else ''
        txt = f"{t0} {t1} {t2}".lower()
        tops.append(txt)

    def find_cols(keyword):
        return [i for i, t in enumerate(tops) if keyword in t]

    company_cols = find_cols('company') + find_cols('investment') + find_cols('investments')
    invest_cols = find_cols('investment') + find_cols('investments')
    amort_cols = find_cols('amortized cost')
    # BXSL often labels this as plain "Cost"
    cost_cols = [i for i, t in enumerate(tops) if (' cost' in t or t.strip().startswith('cost')) and 'amortized cost' not in t]
    fair_cols = find_cols('fair value') + find_cols('fairvalue')

    amount_cols = amort_cols if amort_cols else cost_cols

    if not company_cols or not amount_cols or not fair_cols:
        return None

    company_col = company_cols[0]
    invest_col = invest_cols[0] if invest_cols else max(0, company_col + 6)

    return {
        'company': company_col,
        'invest': invest_col,
        'amort_cols': sorted(set(amount_cols)),
        'fair_cols': sorted(set(fair_cols)),
    }


def _infer_table_context_year(tbl):
    """Infer whether a table belongs to 2025 or 2024 SoI section by nearby heading text."""
    year_dist = None
    year_val = None
    soi_dist = None

    for i, s in enumerate(tbl.find_all_previous(string=True, limit=1200)):
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

    # Build set of ALL period labels that could appear in this filing
    all_labels = {}
    for y in (target_year, target_year - 1):
        for q in range(1, 5):
            l = _period_label(y, q)
            if l:
                all_labels[l] = (y, q)

    # Check the table's own text first
    table_txt = tbl.get_text(' ', strip=True).lower().replace('\xa0', ' ')[:500]
    if label in table_txt:
        return True

    target_dist = None
    nearest_other_dist = None

    # Search preceding text: find the NEAREST period label
    for i, s in enumerate(tbl.find_all_previous(string=True, limit=1200)):
        t = ' '.join(str(s).split()).lower()
        for l, (y, q) in all_labels.items():
            if l in t:
                if l == label and target_dist is None:
                    target_dist = i
                elif l != label and nearest_other_dist is None:
                    nearest_other_dist = i
        if target_dist is not None and nearest_other_dist is not None:
            break

    if target_dist is None:
        return False

    # If a different period's label is CLOSER (smaller distance), reject
    if nearest_other_dist is not None and nearest_other_dist < target_dist:
        return False

    return True

def _parse_obdc_year(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')

    all_rows = []
    carry_company = None
    # GBDC: use global detail dict across ALL tables so cross-table instruments
    # (same company in debt table + equity table) get properly summed
    global_detail = {}
    global_display = {}
    tables = soup.find_all('table')
    table_years = [_infer_table_context_year(tbl) for tbl in tables]
    known_years = [y for y in table_years if y is not None]
    primary_year = max(known_years) if known_years else target_year
    last_colmap_by_width = {}

    for tbl, ctx_year in zip(tables, table_years):
        if ctx_year != target_year:
            continue
        if not _table_matches_period(tbl, target_year, target_quarter):
            continue

        txt = tbl.get_text(' ', strip=True).lower()
        if 'gross additions' in txt or 'gross reductions' in txt:
            continue
        has_std_headers = (
            ('company' in txt or 'investments' in txt or 'investment' in txt)
            and ('amortized cost' in txt or ' cost ' in f' {txt} ')
            and 'fair value' in txt
        )

        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:
            continue

        colmap = _detect_colmap(df) if has_std_headers else None
        if colmap is not None:
            last_colmap_by_width[df.shape[1]] = colmap
        elif target_year >= 2026 and df.shape[1] >= 50 and df.shape[1] in last_colmap_by_width:
            colmap = last_colmap_by_width[df.shape[1]]
        else:
            continue

        # GBDC: company is ALWAYS at c0 regardless of table width (63 or 69 cols)
        company_col = 0
        invest_col = colmap.get('invest', 3)

        amort_cols = colmap['amort_cols']
        fair_cols = colmap['fair_cols']

        # map by paired order: left block tends older, right block tends current in OBDC 2025 filing
        pairs = list(zip(amort_cols[:len(fair_cols)], fair_cols[:len(amort_cols)]))
        if not pairs:
            continue
        if target_year == primary_year:
            amort_candidates = [p[0] for p in pairs[::-1]]
            fair_candidates = [p[1] for p in pairs[::-1]]
        else:
            amort_candidates = [p[0] for p in pairs]
            fair_candidates = [p[1] for p in pairs]


        current = carry_company
        current_display = carry_company or ''
        closed = False

        for _, row in df.iterrows():
            vals = list(row.values)

            company_raw = str(vals[company_col]).strip() if company_col < len(vals) else ''
            inv_raw = str(vals[invest_col]).strip() if invest_col < len(vals) else ''

            comp_blank = _is_blank(company_raw)
            inv_blank = _is_blank(inv_raw)

            if not comp_blank:
                low = company_raw.lower()
                if is_soi_heading(low) or any(k in low for k in ['company', 'industry', 'total']):
                    continue
                if _is_summary_like_name(company_raw):
                    current = None
                    closed = False
                    continue
                current = _canon_key(company_raw)
                current_display = _collapse_repeated_name(company_raw)
                closed = False

            if current is None:
                continue

            face = None
            fair = None
            for c in amort_candidates:
                face = _pick_num(vals, c)
                if face is not None:
                    break
            for c in fair_candidates:
                fair = _pick_num(vals, c)
                if fair is not None:
                    break
            if face is None and fair is None:
                continue

            f = 0.0 if face is None else face
            r = 0.0 if fair is None else fair

            if comp_blank and inv_blank:
                # Skip ALL subtotal rows (company-level and industry-level)
                closed = True
                continue

            if closed:
                continue

            # detail rows should carry investment text
            if inv_blank:
                continue

            a, b = global_detail.get(current, (0.0, 0.0))
            global_detail[current] = (a + f, b + r)
            if current not in global_display:
                global_display[current] = current_display

        if current is not None:
            carry_company = current

    # Build output from global_detail (accumulated across all tables)
    for key, (f, r) in global_detail.items():
        if _is_summary_like_name(key):
            continue
        if f > 0:
            display = global_display.get(key, key)
            all_rows.append({'CanonKey': key, 'CompanyKey': display, 'Face': f, 'Fair': r, 'IsSubtotal': 0})

    if not all_rows:
        return pd.DataFrame(columns=['CanonKey', 'CompanyKey', 'Face', 'Fair'])

    out = pd.DataFrame(all_rows)
    out = out[out['CanonKey'].str.len() > 0]
    # Dedupe repeated analytical tables:
    # 1) prefer subtotal-derived company rows,
    # 2) then prefer larger face (more complete row),
    # 3) then lower fair as stable tie-breaker.
    out = out.sort_values(['CanonKey', 'IsSubtotal', 'Face', 'Fair'], ascending=[True, False, False, True])
    out = out.groupby('CanonKey', as_index=False).first()[['CanonKey', 'CompanyKey', 'Face', 'Fair']]
    return out


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Parse a single period's holdings from a filing URL."""
    return _parse_obdc_year(url, target_year, target_quarter)
