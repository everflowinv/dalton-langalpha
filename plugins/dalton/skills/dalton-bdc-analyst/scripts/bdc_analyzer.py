import json
import re
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tabulate import tabulate

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from state_store import read_seed_json, resolve_safe_state_dir, resolve_sec_identity


def get_headers():
    identity, _ = resolve_sec_identity()
    return {'User-Agent': identity}


_SOI_HEADING_RE = re.compile(r"\bschedules?\s+of\s+investments\b", re.IGNORECASE)
_CONSOLIDATED_SOI_HEADING_RE = re.compile(
    r"\bconsolidated\s+schedules?\s+of\s+investments\b", re.IGNORECASE
)


def is_soi_heading(text: str, *, consolidated: bool = False) -> bool:
    """Match singular or plural 'Schedule(s) of Investments' headings."""
    if not text:
        return False
    pattern = _CONSOLIDATED_SOI_HEADING_RE if consolidated else _SOI_HEADING_RE
    return bool(pattern.search(text))


def robust_fetch_content(url: str, retries: int = 6, initial_sleep: float = 3,
                         min_content_bytes: int = 50_000) -> bytes:
    """Fetch SEC filing content with rate-limit detection and exponential backoff.

    SEC rate limiting often returns:
    - HTTP 200 with a tiny error/redirect page (< 5KB) instead of the real filing (> 100KB)
    - HTTP 403/429 explicitly

    This function retries with increasing delays when:
    1. HTTP error (4xx/5xx)
    2. Response is suspiciously small (< min_content_bytes for filing HTMLs)
    """
    import time as _time
    last_err = None
    sleep_s = initial_sleep
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=get_headers(), timeout=120)
            r.raise_for_status()
            content = r.content
            # Check for truncated/rate-limited response
            if len(content) < min_content_bytes and attempt < retries - 1:
                # Small response — likely rate limited. Retry with backoff.
                print(f'    [fetch] Response only {len(content)} bytes (expected >{min_content_bytes}), '
                      f'retrying in {sleep_s:.0f}s... (attempt {attempt+1}/{retries})',
                      file=sys.stderr)
                _time.sleep(sleep_s)
                sleep_s = min(sleep_s * 2, 30)  # exponential backoff, max 30s
                continue
            return content
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                _time.sleep(sleep_s)
                sleep_s = min(sleep_s * 2, 30)
    raise RuntimeError(f'fetch failed after {retries} retries: {last_err}')


def get_cik(ticker):
    res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=get_headers(), timeout=60)
    res.raise_for_status()
    data = res.json()
    for _, v in data.items():
        if v['ticker'].upper() == ticker.upper():
            return str(v['cik_str']).zfill(10)
    raise ValueError(f"Ticker {ticker} not found")

def get_shareholder_equity(cik):
    res = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=get_headers(), timeout=60)
    if res.status_code == 200:
        facts = res.json().get('facts', {})
        us_gaap = facts.get('us-gaap', {})
        concepts = [
            'StockholdersEquity',
            'AssetsNet',
            'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
            'NetAssets',
        ]
        candidates = []
        for priority, concept in enumerate(concepts):
            if concept not in us_gaap:
                continue
            units = us_gaap[concept].get('units', {})
            for item in units.get('USD', []):
                if item.get('val') is None:
                    continue
                candidates.append((
                    str(item.get('end', '')),
                    -priority,
                    1 if str(item.get('frame', '')).startswith('CY') else 0,
                    str(item.get('filed', '')),
                    item['val'],
                ))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][-1]
    return None

def fetch_latest_10k_url(cik, filing_year=None):
    if filing_year is None:
        filing_year = datetime.now().year
    res = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=get_headers(), timeout=60)
    res.raise_for_status()
    filings = res.json()['filings']['recent']
    for i, form in enumerate(filings['form']):
        if form == '10-K' and str(filings['filingDate'][i]).startswith(str(filing_year)):
            acc = filings['accessionNumber'][i].replace('-', '')
            doc = filings['primaryDocument'][i]
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
    for i, form in enumerate(filings['form']):
        if form == '10-K' and str(filings['filingDate'][i]).startswith(str(filing_year - 1)):
            acc = filings['accessionNumber'][i].replace('-', '')
            doc = filings['primaryDocument'][i]
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
    raise ValueError(f"No recent 10-K found")


def period_to_year(period: str):
    m = re.match(r'^(\d{2})Q([1-4])$', str(period).upper())
    if not m:
        raise ValueError(f"Invalid period: {period}. Use YYQn, e.g. 25Q4")
    return 2000 + int(m.group(1))


def period_to_quarter(period: str):
    m = re.match(r'^(\d{2})Q([1-4])$', str(period).upper())
    if not m:
        raise ValueError(f"Invalid period: {period}. Use YYQn, e.g. 25Q4")
    return int(m.group(2))


def _date_to_quarter(date_str: str):
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        return None
    return (dt.month - 1) // 3 + 1


class FilingNotFoundForPeriod(ValueError):
    """The submissions list has no 10-Q/10-K for the requested period (and no fallback).

    A ValueError subclass so existing callers keep working; harvest catches only this,
    so an unreadable SEC response is a failure, not a period that was never filed."""


def fetch_filing_url_for_period(cik, period: str, allow_fallback: bool = True, return_meta: bool = False):
    """Return filing URL (10-Q/10-K) for target YYQn.

    If exact period is unavailable and allow_fallback=True, falls back to nearest
    available period by reportDate quarter distance.

    return_meta=True -> (url, resolved_period, used_fallback)
    """
    target_year = period_to_year(period)
    target_q = period_to_quarter(period)
    target_idx = target_year * 4 + target_q

    res = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=get_headers(), timeout=60)
    res.raise_for_status()
    filings = res.json()['filings']['recent']

    exact = []
    all_candidates = []
    n = len(filings.get('form', []))
    for i in range(n):
        form = filings['form'][i]
        if form not in ('10-Q', '10-K'):
            continue
        report_date = str(filings.get('reportDate', [''])[i] or '')
        if not report_date:
            continue
        try:
            y = int(report_date[:4])
        except Exception:
            continue
        q = _date_to_quarter(report_date)
        if q is None:
            continue
        filing_date = str(filings.get('filingDate', [''])[i])
        all_candidates.append((y, q, filing_date, i))
        if y == target_year and q == target_q:
            exact.append((filing_date, i))

    used_fallback = False
    if exact:
        exact.sort(key=lambda x: x[0], reverse=True)
        idx = exact[0][1]
        resolved_y, resolved_q = target_year, target_q
    else:
        if not allow_fallback or not all_candidates:
            raise FilingNotFoundForPeriod(f"No 10-Q/10-K filing found for period {period}")
        used_fallback = True
        # nearest quarter; tie-break by latest filing date
        all_candidates.sort(key=lambda t: (abs((t[0] * 4 + t[1]) - target_idx), -int(t[2].replace('-', '') or '0')))
        resolved_y, resolved_q, _, idx = all_candidates[0]

    acc = filings['accessionNumber'][idx].replace('-', '')
    doc = filings['primaryDocument'][idx]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
    resolved_period = f"{str(resolved_y)[-2:]}Q{resolved_q}"

    if return_meta:
        return url, resolved_period, used_fallback
    return url


def clean_num(x):
    if pd.isna(x): return np.nan
    s = str(x).strip()
    if not s or s in ['-', '—', '$']: return np.nan
    s = re.sub(r'[^\d\.\(\)-]', '', s)
    if not s: return np.nan
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except Exception:
        return np.nan


def normalize_company(name):
    """Normalize company name for aggregation key. Preserves DBA/parenthetical info but strips footnotes."""
    s = str(name)
    # Remove footnote markers like (3), (3)(8), (3)(4)(10)(30), etc.
    s = re.sub(r'\((\d+)\)', '', s)
    # Deduplicate repeated company names (artifact of multi-column HTML join)
    # e.g. "KASEYA INC. KASEYA INC. KASEYA INC." -> "KASEYA INC."
    s = re.sub(r'\s+', ' ', s).strip()
    parts = s.split()
    if len(parts) > 3:
        # Try to detect repeated patterns
        for chunk_len in range(2, len(parts) // 2 + 1):
            chunk = ' '.join(parts[:chunk_len])
            rest = s[len(chunk):].strip()
            if rest.startswith(chunk):
                s = chunk
                break
    s = re.sub(r'\s+', ' ', s).strip()
    return s.upper()

def _base_name(name):
    """Extract the core business name, stripping legal suffixes and parentheticals.
    Used for merging related entities (e.g. KASEYA INC. and KNOCKOUT INTERMEDIATE HOLDINGS I INC. (DBA KASEYA INC.))
    """
    s = str(name).upper()
    # Extract DBA name if present — that's the real business name
    dba = re.search(r'\((?:DBA|D/B/A|F/K/A)\s+(.+?)\)', s)
    if dba:
        s = dba.group(1)
    else:
        # Strip parentheticals
        s = re.sub(r'\(.*?\)', '', s)
    # Strip common legal suffixes
    s = re.sub(r',?\s*(LLC|INC\.?|L\.?P\.?|CORP\.?|LTD\.?|CO\.?|HOLDINGS?|INTERMEDIATE|PARENT|TOPCO|BIDCO|COMBINECO|HOLDCO|ISSUER)\b', '', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def is_valid_company(name):
    n = str(name).lower().strip()
    if len(n) <= 1 or n == 'nan': return False
    # Filter explicit sum rows, sub-totals, and GICS-style industry category headers
    exclusion_patterns = [
        r'^\s*total\b', r'^\s*subtotal\b', r'^\s*net\b',
        r'\btotal\s+investments\b', r'\btotal\s+assets\b',
        r'\btotal\s+(?:asset|senior|subordinated|mezzanine|equity|portfolio|first|second)\b',
        r'\bnet\s+(?:asset|unrealized)\b',
        r'\bschedules?\s+of\s+investments\b', 'interest rate',
        'total asset based finance', 'net asset based finance',
        # GICS industry group names that appear as section headers in some BDC filings
        r'^(?:internet|software|diversified|health care|commercial|capital|consumer|media|'
        r'professional|it |real estate|insurance|aerospace|auto |banks|beverages|building|'
        r'chemicals|construction|containers|distributors|electric|energy|entertainment|'
        r'food |gas |ground|hotels|household|industrial|leisure|life |machinery|marine|'
        r'metals|multi-|office|oil|paper|passenger|personal|pharmaceuticals|road|'
        r'semiconductors|specialty|technology|textiles|tobacco|trading|water|wireless)\b.*'
        r'(?:retail|services|products|equipment|finance|markets|utilities|distribution|'
        r'management|providers|stores|durables|facilities|instruments|& |industries)$',
    ]
    for p in exclusion_patterns:
        if re.search(p, n):
            return False
    # Additional heuristic: if name has no letters (just numbers), skip
    if not re.search(r'[a-zA-Z]', n):
        return False
    return True

def parse_candidate_table(tbl, context_year=None):
    text = tbl.get_text(' ', strip=True).lower()
    if 'fair value' not in text: return None, None
    if 'amortized cost' not in text and 'cost' not in text and 'principal' not in text: return None, None

    year = None
    if 'december 31, 2025' in text or 'as of december 31, 2025' in text: year = 2025
    elif 'december 31, 2024' in text or 'as of december 31, 2024' in text: year = 2024
    elif context_year in (2025, 2024): year = context_year

    try: df = pd.read_html(StringIO(str(tbl)))[0]
    except Exception: return None, year

    if len(df) < 20: return None, year
    df = df.dropna(axis=1, how='all')

    header_idx = None
    for i in range(min(12, len(df))):
        row = ' '.join([str(v).lower() for v in df.iloc[i].values])
        if 'fair value' in row and ('cost' in row or 'principal' in row):
            header_idx = i
            break
    if header_idx is None: return None, year

    cols = [str(v).strip().lower().replace('\n', ' ') for v in df.iloc[header_idx].values]
    df.columns = cols
    df = df.iloc[header_idx + 1:].copy()

    company_col = cost_col = fv_col = None
    amortized_candidates, generic_cost_candidates, principal_candidates = [], [], []

    for c in df.columns:
        lc = str(c).lower()
        if company_col is None and ('portfolio company' in lc or 'issuer' in lc or 'investment' in lc or lc == cols[0]): company_col = c
        if fv_col is None and 'fair value' in lc: fv_col = c
        if 'amortized cost' in lc: amortized_candidates.append(c)
        elif 'cost' in lc: generic_cost_candidates.append(c)
        elif 'principal' in lc: principal_candidates.append(c)

    if amortized_candidates: cost_col = amortized_candidates[0]
    elif generic_cost_candidates: cost_col = generic_cost_candidates[0]
    elif principal_candidates: cost_col = principal_candidates[0]

    if company_col is None or cost_col is None or fv_col is None: return None, year

    def pick_series(frame, col, numeric=False):
        out = frame[col]
        if isinstance(out, pd.DataFrame):
            # Duplicate column names — take the first one only
            return out.iloc[:, 0]
        return out

    out = pd.DataFrame({
        'Company': pick_series(df, company_col),
        'Face': pick_series(df, cost_col, numeric=True),
        'Fair': pick_series(df, fv_col, numeric=True),
    })

    out['Company'] = out['Company'].astype(str).str.replace(r'\n', ' ', regex=True).str.strip()
    out = out[out['Company'].apply(is_valid_company)]

    out['Face'] = out['Face'].apply(clean_num)
    out['Fair'] = out['Fair'].apply(clean_num)
    out = out.dropna(subset=['Face', 'Fair'], how='all')
    out = out[out['Face'] > 0]
    out['CompanyKey'] = out['Company'].apply(normalize_company)

    out = out.groupby('CompanyKey', as_index=False).agg({'Face': 'sum', 'Fair': 'sum'})
    return out, year

def extract_two_year_tables(url):
    res = requests.get(url, headers=get_headers(), timeout=120)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, 'lxml')
    tables = soup.find_all('table')
    context_year = None
    parsed_records = []

    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()
        if is_soi_heading(txt):
            if 'december 31, 2025' in txt: context_year = 2025
            elif 'december 31, 2024' in txt: context_year = 2024

        parsed, year = parse_candidate_table(tbl, context_year=context_year)
        if parsed is None: continue
        parsed_records.append((idx, parsed, year))

    year_frames = {2025: [], 2024: []}
    unknown = []
    for idx, df, year in parsed_records:
        if year in (2025, 2024): year_frames[year].append(df)
        else: unknown.append((idx, df))

    if unknown:
        unknown = sorted(unknown, key=lambda x: x[0])
        clusters = []
        cur = [unknown[0]]
        for rec in unknown[1:]:
            if rec[0] - cur[-1][0] <= 10: cur.append(rec)
            else:
                clusters.append(cur)
                cur = [rec]
        clusters.append(cur)

        clusters = sorted(clusters, key=lambda c: len(c), reverse=True)[:2]
        clusters = sorted(clusters, key=lambda c: c[0][0])

        if len(clusters) >= 1:
            for _, df in clusters[0]: year_frames[2025].append(df)
        if len(clusters) >= 2:
            for _, df in clusters[1]: year_frames[2024].append(df)

    final = {}
    for y in [2025, 2024]:
        if not year_frames[y]: final[y] = pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])
        else:
            tmp = pd.concat(year_frames[y], ignore_index=True)
            tmp = tmp.groupby('CompanyKey', as_index=False).agg({'Face': 'sum', 'Fair': 'sum'})
            final[y] = tmp
    return final[2025], final[2024]

def _load_company_db(workspace_dir=None):
    """Load company database for industry/description lookup from workspace state or read-only seed."""
    if workspace_dir is not None:
        _, state_dir = resolve_safe_state_dir(workspace_dir)
        db_path = state_dir / 'company_db.json'
        if db_path.is_file():
            with open(db_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    return read_seed_json('company_db.json') or {}

_COMPANY_DB = None

def _get_company_db():
    global _COMPANY_DB
    if _COMPANY_DB is None:
        _COMPANY_DB = _load_company_db()
    return _COMPANY_DB

def _lookup_company(name):
    """Look up a company name in the DB, trying multiple matching strategies."""
    db = _get_company_db()
    name = str(name).strip()
    
    # 1. Exact match
    if name in db and db[name].get('industry'):
        return db[name]
    
    # 2. Case-insensitive exact match
    name_upper = name.upper().strip()
    for k, v in db.items():
        if k.upper().strip() == name_upper and v.get('industry'):
            return v
    
    # 3. Normalized match (strip parens, legal suffixes, punctuation)
    def _norm(s):
        s = re.sub(r'\([^)]*\)', '', s.upper())
        s = re.sub(r'[^A-Z0-9 ]', ' ', s)
        s = re.sub(r'\s+', ' ', s).strip()
        return s
    
    norm_name = _norm(name)
    for k, v in db.items():
        if _norm(k) == norm_name and v.get('industry'):
            return v
    
    # 4. Base name match (using _base_name for aggressive normalization)
    base = _base_name(name)
    if len(base) >= 3:
        for k, v in db.items():
            if _base_name(k) == base and v.get('industry'):
                return v
    
    # 5. DBA extraction — if name has "(DBA XYZ)", try looking up "XYZ" variants
    dba_match = re.search(r'\((?:DBA|D/B/A|F/K/A)\s+(.+?)\)', name, re.IGNORECASE)
    if dba_match:
        dba_name = dba_match.group(1).strip()
        for k, v in db.items():
            if dba_name.upper() in k.upper() and v.get('industry'):
                return v
    
    # 6. Substring containment — check if any DB key's base name is contained in our name
    if len(base) >= 5:
        best_match = None
        best_len = 0
        for k, v in db.items():
            kb = _base_name(k)
            if len(kb) >= 5 and kb in base and len(kb) > best_len and v.get('industry'):
                best_match = v
                best_len = len(kb)
        if best_match:
            return best_match
    
    return None


def add_simple_business_intro(df):
    """Enrich dataframe with industry, sub_industry, and description from company_db.json."""
    industries = []
    sub_industries = []
    descriptions = []

    company_col = 'CompanyKey' if 'CompanyKey' in df.columns else df.columns[0]

    for _, row in df.iterrows():
        name = str(row[company_col])
        info = _lookup_company(name)
        if info and info.get('industry'):
            industries.append(info.get('industry', ''))
            sub_industries.append(info.get('sub_industry', ''))
            descriptions.append(info.get('description', ''))
        else:
            industries.append('')
            sub_industries.append('')
            descriptions.append('')

    df['行业'] = industries
    df['细分行业'] = sub_industries
    df['业务简介'] = descriptions
    return df

def parse_htgc_table(tbl):
    text = tbl.get_text(' ', strip=True).lower()
    if 'value' not in text or 'cost' not in text or 'principal' not in text: return None
    try: df = pd.read_html(StringIO(str(tbl)))[0]
    except: return None
    if len(df) < 5: return None
    
    header_idx = None
    for i in range(min(12, len(df))):
        row = ' '.join([str(v).lower() for v in df.iloc[i].values])
        if 'value' in row and ('cost' in row or 'principal' in row):
            header_idx = i
            break
    if header_idx is None: return None
    cols = [str(v).strip().lower().replace('\n', ' ') for v in df.iloc[header_idx].values]
    df.columns = cols
    df = df.iloc[header_idx + 1:].copy()
    
    company_col = None; cost_col = None; fv_col = None
    for c in df.columns:
        if company_col is None and ('portfolio company' in c): company_col = c
        elif fv_col is None and ('value' in c and 'fair' not in c and 'par' not in c): fv_col = c
        elif cost_col is None and ('cost' in c): cost_col = c
    if not (company_col and cost_col and fv_col): return None
    
    def pick_s(frame, col):
        out = frame[col]
        if isinstance(out, pd.DataFrame): 
            return out.iloc[:, 0]
        return out
        
    out = pd.DataFrame({'Company': pick_s(df, company_col), 'Face': pick_s(df, cost_col), 'Fair': pick_s(df, fv_col)})
    out['Company'] = out['Company'].astype(str).str.replace(r'\n', ' ', regex=True).str.strip()
    out = out[out['Company'].apply(is_valid_company)]
    out['Face'] = out['Face'].apply(clean_num)
    out['Fair'] = out['Fair'].apply(clean_num)
    out = out.dropna(subset=['Face', 'Fair'], how='all')
    out = out[out['Face'] > 0]
    out['CompanyKey'] = out['Company'].apply(normalize_company)
    return out.groupby('CompanyKey', as_index=False).agg({'Face': 'sum', 'Fair': 'sum'})

def extract_htgc_style_table(url):
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    
    res = requests.get(url, headers=get_headers(), timeout=120)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, 'lxml')
    frames = []
    for t in soup.find_all('table'):
        df = parse_htgc_table(t)
        if df is not None and len(df) > 0: frames.append(df)
    if not frames:
        return pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])
    final = pd.concat(frames, ignore_index=True)
    return final.groupby('CompanyKey', as_index=False).agg({'Face': 'sum', 'Fair': 'sum'})

def _merge_related_entities(df):
    """Merge rows that belong to the same underlying business using _base_name().
    
    For example, 'KASEYA INC.' and 'KNOCKOUT INTERMEDIATE HOLDINGS I INC. (DBA KASEYA INC.)'
    should merge into a single row. We keep the most descriptive CompanyKey as the display name.
    """
    df = df.copy()
    df['BaseName'] = df['CompanyKey'].apply(_base_name)
    
    # For each base name group, sum Face and Fair, keep the longest CompanyKey as display name
    groups = df.groupby('BaseName', sort=False)
    
    rows = []
    for base, grp in groups:
        if base == '' or len(base) < 2:
            # Don't merge empty/trivial base names, keep as-is
            for _, r in grp.iterrows():
                rows.append(r)
            continue
        # Pick the most descriptive name (longest, which usually has the DBA)
        best_name = max(grp['CompanyKey'].values, key=len)
        rows.append(pd.Series({
            'CompanyKey': best_name,
            'Face': grp['Face'].sum(),
            'Fair': grp['Fair'].sum(),
            'BaseName': base,
        }))
    
    result = pd.DataFrame(rows)
    return result[['CompanyKey', 'Face', 'Fair']].reset_index(drop=True)


def analyze(ticker):
    cik = get_cik(ticker)
    equity_usd = get_shareholder_equity(cik)
    if not equity_usd:
        print("Warning: Could not fetch shareholder equity. Proceeding with fallback 1B.", file=sys.stderr)
        equity_usd = 1000000000  # Fallback
    else:
        print(f"Shareholder Equity: ${equity_usd/1e6:,.0f}M", file=sys.stderr)

    current_year = datetime.now().year
    url = fetch_latest_10k_url(cik, filing_year=current_year)
    print(f"10-K URL: {url}", file=sys.stderr)
    
    df25, df24 = extract_two_year_tables(url)

    if len(df25) == 0:
        print("Falling back to HTGC-style single year extraction for 2025...", file=sys.stderr)
        df25 = extract_htgc_style_table(url)
    if len(df24) == 0:
        try:
            url_prior = fetch_latest_10k_url(cik, filing_year=current_year - 1)
            print("Falling back to prior year 10-K extraction for 2024...", file=sys.stderr)
            _, df24_attempt = extract_two_year_tables(url_prior)
            if len(df24_attempt) > 0:
                df24 = df24_attempt
            else:
                df24 = extract_htgc_style_table(url_prior)
        except Exception as e:
            print(f"Warning: Could not fetch prior 10-K for 2024 data: {e}", file=sys.stderr)

    print(f"Extracted: {len(df25)} companies (2025), {len(df24)} companies (2024)", file=sys.stderr)

    # Merge related entities BEFORE cross-year join
    df25 = _merge_related_entities(df25)
    df24 = _merge_related_entities(df24)

    # Detect table scale (some filings report in thousands)
    if len(df25) > 0 and df25['Face'].median() < 1000000:
        table_scale = 1000
    else:
        table_scale = 1

    merged = pd.merge(df25, df24, on='CompanyKey', how='inner', suffixes=('_2025', '_2024'))
    merged = merged[(merged['Face_2025'] > 0) & (merged['Face_2024'] > 0)]

    merged['Face_2025_M'] = merged['Face_2025'] * table_scale / 1000000
    merged['Fair_2025_M'] = merged['Fair_2025'] * table_scale / 1000000
    merged['Face_2024_M'] = merged['Face_2024'] * table_scale / 1000000
    merged['Fair_2024_M'] = merged['Fair_2024'] * table_scale / 1000000

    equity_m = equity_usd / 1000000
    threshold_m = equity_m * 0.005
    pre_filter = len(merged)
    merged = merged[merged['Face_2025_M'] > threshold_m]
    print(f"Equity filter (0.5% = ${threshold_m:.1f}M): {pre_filter} -> {len(merged)} companies", file=sys.stderr)

    merged['ratio_2025'] = merged['Fair_2025_M'] / merged['Face_2025_M']
    merged['ratio_2024'] = merged['Fair_2024_M'] / merged['Face_2024_M']
    merged['ratio_change'] = merged['ratio_2025'] - merged['ratio_2024']

    # Sort ascending (most negative change first = worst deterioration)
    merged = merged.sort_values('ratio_change', ascending=True)
    
    # Take top 20 or until ratio_change >= 0
    negative = merged[merged['ratio_change'] < 0]
    out = negative.head(20).copy()
    
    if out.empty:
        print("No assets found with declining Fair/Face ratio.")
        return

    # Enrich with business descriptions using CompanyKey (which still has original name info)
    out = add_simple_business_intro(out)

    # Format output columns
    out['Face_2025_fmt'] = out['Face_2025_M'].map('{:.2f}'.format)
    out['Fair_2025_fmt'] = out['Fair_2025_M'].map('{:.2f}'.format)
    out['ratio_2025_fmt'] = (out['ratio_2025'] * 100).map('{:.2f}%'.format)
    
    out['Face_2024_fmt'] = out['Face_2024_M'].map('{:.2f}'.format)
    out['Fair_2024_fmt'] = out['Fair_2024_M'].map('{:.2f}'.format)
    out['ratio_2024_fmt'] = (out['ratio_2024'] * 100).map('{:.2f}%'.format)
    
    out['ratio_change_fmt'] = (out['ratio_change'] * 100).map('{:.2f}%'.format)

    # Build display name: use CompanyKey but clean up for readability
    def _display_name(key):
        # Shorten overly long combined names
        if len(key) > 60:
            # Try to extract DBA name
            dba = re.search(r'\((?:DBA|D/B/A)\s+(.+?)\)', key)
            if dba:
                return dba.group(1).strip()
            # Otherwise truncate
            return key[:57] + '...'
        return key
    
    out['DisplayName'] = out['CompanyKey'].apply(_display_name)
    
    # Fill empty descriptions with a fallback
    out['业务简介'] = out['业务简介'].replace('', '企业借款主体或特定项目控股公司。')

    show = out[[
        'DisplayName', 'Face_2025_fmt', 'Fair_2025_fmt', 'ratio_2025_fmt',
        'Face_2024_fmt', 'Fair_2024_fmt', 'ratio_2024_fmt', 'ratio_change_fmt', '业务简介'
    ]]
    
    show = show.copy()
    show.columns = [
        '公司名', '25Q4 Face Value (M USD)', '25Q4 Fair Value (M USD)', '25Q4 Fair/Face',
        '24Q4 Face (M USD)', '24Q4 Fair (M USD)', '24Q4 Fair/Face', 'YoY Fair/Face变化', '公司主要业务简介'
    ]

    print("\n" + tabulate(show, headers='keys', tablefmt='pipe', showindex=False))

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--ticker', required=True)
    args = p.parse_args()
    analyze(args.ticker)
