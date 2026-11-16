import re
import time
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

from bdc_analyzer import (
    is_soi_heading,
    get_headers,
    fetch_filing_url_for_period,
    period_to_year,
    period_to_quarter,
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


def _fetch_period_url_ocsl(cik: str, period: str):
    """OCSL override: prefer 10-Q for requested period when available."""
    y = period_to_year(period)
    q = period_to_quarter(period)
    res = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=get_headers(), timeout=60)
    res.raise_for_status()
    filings = res.json()['filings']['recent']

    exact_10q = []
    exact_any = []
    for i, form in enumerate(filings.get('form', [])):
        if form not in ('10-Q', '10-K'):
            continue
        report_date = str(filings.get('reportDate', [''])[i] or '')
        if not report_date:
            continue
        try:
            ry = int(report_date[:4])
            rq = (int(report_date[5:7]) - 1) // 3 + 1
        except Exception:
            continue
        if ry == y and rq == q:
            rec = (str(filings.get('filingDate', [''])[i]), i)
            exact_any.append(rec)
            if form == '10-Q':
                exact_10q.append(rec)

    pool = exact_10q if exact_10q else exact_any
    if pool:
        pool.sort(key=lambda x: x[0], reverse=True)
        idx = pool[0][1]
        acc = filings['accessionNumber'][idx].replace('-', '')
        doc = filings['primaryDocument'][idx]
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"

    return fetch_filing_url_for_period(cik, period)


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
        # Normalize non-breaking spaces before matching
        return [i for i, t in enumerate(tops) if keyword in t.replace('\xa0', ' ')]

    company_cols = find_cols('portfolio company') + find_cols('company') + find_cols('investment') + find_cols('investments')
    invest_cols = find_cols('type of investment') + find_cols('investment') + find_cols('investments')
    amort_cols = find_cols('amortized cost')
    # Cost columns: only match standalone "cost" header, not "interest rate" or similar
    cost_cols = [i for i, t in enumerate(tops)
                 if 'cost' in t.replace('\xa0', ' ').lower()
                 and 'amortized' not in t and 'interest' not in t and 'carry' not in t
                 and 'fair' not in t]
    principal_cols = find_cols('principal')
    fair_cols = find_cols('fair value') + find_cols('fairvalue')

    amount_cols = amort_cols if amort_cols else (cost_cols if cost_cols else principal_cols)

    # For ultra-wide tables (CGBD-like), fair_cols may spuriously match the company header area.
    # Filter: keep only fair_cols that are to the right of the rightmost cost/amort column.
    if amount_cols and fair_cols:
        max_cost = max(amount_cols)
        filtered_fair = [c for c in fair_cols if c > max_cost]
        if filtered_fair:
            fair_cols = filtered_fair

    if not company_cols or not amount_cols or not fair_cols:
        # OCSL / TSLX 39-col layout: col 0=company, col 27-29=principal, col 30-32=cost, col 33-35=fair value
        if df.shape[1] >= 36:
            return {
                'company': 0,
                'invest': 6,
                'amort_cols': [30],
                'fair_cols': [33],
            }
        # Narrower fallback
        if df.shape[1] >= 22:
            return {
                'company': 0,
                'invest': 2 if df.shape[1] > 2 else 0,
                'amort_cols': [16],
                'fair_cols': [21],
            }
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


def _parse_obdc_year(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')

    all_rows = []
    carry_company = None  # handle page-break continuation tables with blank company cells

    tables = soup.find_all('table')

    # Build candidate list first, then fill year=None by nearest known-year table.
    candidates = []
    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()
        if 'company' not in txt and 'investments' not in txt and 'investment' not in txt and 'loan' not in txt:
            continue
        if ('amortized cost' not in txt and ' cost ' not in f' {txt} ' and 'principal' not in txt and ' par ' not in f' {txt} ' and 'loan' not in txt):
            continue
        if 'fair value' not in txt and 'fairvalue' not in txt and ' value ' not in f' {txt} ' and 'loan' not in txt:
            continue
        candidates.append((idx, tbl, txt, _infer_table_context_year(tbl)))

    # Fill missing years by nearest known-year neighbor within SoI run.
    known = [(i, y) for i, _, _, y in candidates if y is not None]
    resolved = []
    for i, tbl, txt, y in candidates:
        if y is not None:
            resolved.append((i, tbl, txt, y))
            continue
        if not known:
            continue
        nearest = min(known, key=lambda p: abs(p[0] - i))
        if abs(nearest[0] - i) <= 12:
            y = nearest[1]
            resolved.append((i, tbl, txt, y))

    for _, tbl, txt, ctx_year in resolved:
        if ctx_year != target_year:
            continue
        if not _table_matches_period(tbl, target_year, target_quarter):
            continue

        # TSLX layouts may omit explicit % of net assets in some split tables.
        try:
            df = pd.read_html(StringIO(str(tbl)))[0]
        except Exception:
            continue

        colmap = _detect_colmap(df)
        if not colmap:
            continue

        company_col = colmap['company']
        invest_col = colmap['invest']

        amort_cols = colmap['amort_cols']
        fair_cols = colmap['fair_cols']

        # For OCSL, each filing is period-specific; use stable left-to-right pairing.
        pairs = list(zip(amort_cols[:len(fair_cols)], fair_cols[:len(amort_cols)]))
        if not pairs:
            continue
        amort_candidates = [p[0] for p in pairs]
        fair_candidates = [p[1] for p in pairs]

        detail = {}

        current = carry_company
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
                current = _collapse_repeated_name(company_raw)
                closed = False

            if current is None:
                continue

            face = None
            fair = None
            for c in amort_candidates:
                # Try the exact column and adjacent +1/+2 (for "$" prefix columns in wide tables)
                for offset in (c, c + 1, c + 2):
                    face = _pick_num(vals, offset)
                    if face is not None:
                        break
                if face is not None:
                    break
            for c in fair_candidates:
                for offset in (c, c + 1, c + 2):
                    fair = _pick_num(vals, offset)
                    if fair is not None:
                        break
                if fair is not None:
                    break
            # Require a valid face value row; fair can be missing and treated as 0.
            if face is None:
                continue

            f = face
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

            a, b = detail.get(current, (0.0, 0.0))
            detail[current] = (a + f, b + r)

        # Prefer explicit subtotal rows (blank company/investment line) when present,
        # because detail lines can be partial tranches while subtotal reflects the
        # intended company-level amount for that table block.
        chosen = {}
        for name, (f, r) in detail.items():
            chosen[name] = (f, r, 0)

        for name, (f, r, is_subtotal) in chosen.items():
            if _is_summary_like_name(name):
                continue
            if f > 0:
                all_rows.append({'CanonKey': _canon_key(name), 'CompanyKey': name, 'Face': f, 'Fair': r, 'IsSubtotal': is_subtotal})

        # Preserve last seen company for continuation tables of the same year section.
        if current is not None:
            carry_company = current

    if not all_rows:
        return pd.DataFrame(columns=['CanonKey', 'CompanyKey', 'Face', 'Fair'])

    out = pd.DataFrame(all_rows)
    out = out[out['CanonKey'].str.len() > 0]
    # Dedupe repeated analytical tables for TSLX:
    # prioritize larger face first (more likely true instrument amount),
    # then lower fair as tie-breaker. Do not force subtotal priority because
    # continuation tables may surface section subtotal lines under current company.
    out = out.sort_values(['CanonKey', 'Face', 'Fair', 'IsSubtotal'], ascending=[True, False, True, False])
    out = out.groupby('CanonKey', as_index=False).first()[['CanonKey', 'CompanyKey', 'Face', 'Fair']]
    return out


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Parse a single period's holdings from a filing URL."""
    return _parse_obdc_year(url, target_year, target_quarter)
