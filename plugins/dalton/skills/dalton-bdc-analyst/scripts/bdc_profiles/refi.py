"""
refi profile based on generic parser with forced year. — year forced from filing URL.
Works for most BDCs without a custom profile.
Uses the battle-tested patterns from FSK/ARCC/OBDC/PSEC profiles.
"""
import re
import time
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tabulate import tabulate

from bdc_analyzer import (
    is_soi_heading,
    add_simple_business_intro,
    get_headers,
    get_cik,
    get_shareholder_equity,
    fetch_latest_10k_url,
    fetch_filing_url_for_period,
    period_to_year,
    period_to_quarter,
    clean_num,
)

import warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


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
        r'\bCLO SUBORDINATED\b', r'\bCLO EQUITY\b',
        r'\bESTIMATED YIELD\b', r'\bSERIES SEED\b',
        r'\bFOR THE YEAR\b', r'\bFOR THE PERIOD\b',
        r'\bINTEREST EXPENSE\b', r'\bINTEREST INCOME\b',
        r'\bDIVIDEND INCOME\b', r'\bFEE INCOME\b',
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
    # Check table's own text first
    table_txt = tbl.get_text(' ', strip=True).lower().replace('\xa0', ' ')[:500]
    if label in table_txt:
        return True
    for s in tbl.find_all_previous(string=True, limit=260):
        t = ' '.join(str(s).split()).lower()
        if label in t:
            return True
    return False


def _infer_table_context_year(tbl):
    # First check the table's own text (some filings put the full title inside the table)
    table_txt = tbl.get_text(' ', strip=True).lower()[:600]
    if is_soi_heading(table_txt):
        m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', table_txt)
        if m:
            return int(m.group(2))

    # Then check preceding text
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


def _detect_colmap(df: pd.DataFrame):
    """Generic column detection for BDC SoI tables.
    Returns dict with company, invest, amort_cols, fair_cols or None.
    """
    if df.shape[1] < 8:
        return None

    tops = []
    for c in range(df.shape[1]):
        t0 = str(df.iloc[0, c]) if len(df) > 0 else ''
        t1 = str(df.iloc[1, c]) if len(df) > 1 else ''
        t2 = str(df.iloc[2, c]) if len(df) > 2 else ''
        txt = f"{t0} {t1} {t2}".lower().replace('\xa0', ' ')
        tops.append(txt)

    def find_cols(keyword):
        return [i for i, t in enumerate(tops) if keyword in t.replace('\xa0', ' ')]

    company_cols = find_cols('portfolio company') + find_cols('company') + find_cols('description') + find_cols('investment')
    invest_cols = find_cols('type of investment') + find_cols('investment') + find_cols('description')
    amort_cols = find_cols('amortized cost')
    cost_cols = [i for i, t in enumerate(tops)
                 if 'cost' in t.replace('\xa0', ' ').lower()
                 and 'amortized' not in t and 'interest' not in t and 'carry' not in t
                 and 'fair' not in t]
    principal_cols = find_cols('principal')
    fair_cols = find_cols('fair value') + find_cols('fairvalue')

    amount_cols = amort_cols if amort_cols else (cost_cols if cost_cols else principal_cols)

    # For ultra-wide tables, fair_cols may spuriously match header area.
    if amount_cols and fair_cols:
        max_cost = max(amount_cols)
        filtered_fair = [c for c in fair_cols if c > max_cost]
        if filtered_fair:
            fair_cols = filtered_fair

    if not company_cols or not amount_cols or not fair_cols:
        # Fallback for common layouts
        if df.shape[1] >= 36:
            return {'company': 0, 'invest': 6, 'amort_cols': [30], 'fair_cols': [33]}
        if df.shape[1] >= 22:
            return {'company': 0, 'invest': 2, 'amort_cols': [16], 'fair_cols': [20]}
        return None

    company_col = company_cols[0]
    invest_col = invest_cols[0] if invest_cols else min(company_col + 6, df.shape[1] - 1)

    return {
        'company': company_col,
        'invest': invest_col,
        'amort_cols': sorted(set(amount_cols)),
        'fair_cols': sorted(set(fair_cols)),
    }


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    """Parse a single period's SoI from a filing URL.
    Returns DataFrame with columns: CanonKey, CompanyKey, Face, Fair
    """
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')
    tables = soup.find_all('table')

    all_rows = []
    carry_company = None

    # Build candidate list
    candidates = []
    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()
        if 'company' not in txt and 'investments' not in txt and 'investment' not in txt and 'loan' not in txt:
            continue
        if ('amortized cost' not in txt and ' cost ' not in f' {txt} ' and 'principal' not in txt and ' par ' not in f' {txt} ' and 'loan' not in txt):
            continue
        if 'fair value' not in txt and 'fairvalue' not in txt and ' value ' not in f' {txt} ' and 'loan' not in txt:
            continue
        # Skip roll-forward / follow-on / summary tables (check first 300 chars only to avoid false positives from investment descriptions)
        txt_head = txt[:300]
        if 'gross additions' in txt_head or 'follow-on acquisition' in txt_head or 'unused fee' in txt_head or 'derivative instrument' in txt_head or 'counterparty' in txt_head:
            continue
        candidates.append((idx, tbl, txt, _infer_table_context_year(tbl)))

    # Fill missing years by nearest known-year neighbor
    # PSBD: force all unknown-year tables to target_year (filing URL already resolves correctly)
    resolved = []
    for i, tbl, txt, y in candidates:
        if y is None:
            y = target_year
        resolved.append((i, tbl, txt, y))

    for _, tbl, txt, ctx_year in resolved:
        if ctx_year != target_year:
            continue
        if not _table_matches_period(tbl, target_year, target_quarter):
            continue

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

        # Pair amort/fair columns; for latest year use rightmost pair
        pairs = list(zip(amort_cols[:len(fair_cols)], fair_cols[:len(amort_cols)]))
        if not pairs:
            continue

        # Use left-to-right order (most filings have single period per table)
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
                if is_soi_heading(low) or any(k in low for k in ['company', 'industry', 'total',
                                          'control investments', 'non-control', 'non-affiliate',
                                          'for the year ended', 'for the period', 'for the nine',
                                          'for the six', 'for the three']):
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

            if face is None and fair is None:
                continue

            # Guard against date-like misreads
            if face is not None and face > 10000000:
                continue

            f = 0.0 if face is None else face
            r = 0.0 if fair is None else fair

            if comp_blank and inv_blank:
                # Skip ALL subtotal rows (company-level and industry-level)
                closed = True
                continue

            if closed:
                continue
            if inv_blank:
                continue

            a, b = detail.get(current, (0.0, 0.0))
            detail[current] = (a + f, b + r)

        # Prefer subtotal when available
        chosen = {}
        for name, (f, r) in detail.items():
            chosen[name] = (f, r)

        for name, (f, r) in chosen.items():
            if _is_summary_like_name(name):
                continue
            if f > 0:
                all_rows.append({
                    'CanonKey': _canon_key(name),
                    'CompanyKey': name,
                    'Face': f,
                    'Fair': r,
                })

        if current is not None:
            carry_company = current

    if not all_rows:
        return pd.DataFrame(columns=['CanonKey', 'CompanyKey', 'Face', 'Fair'])

    out = pd.DataFrame(all_rows)
    out = out[out['CanonKey'].str.len() > 0]
    out = out.sort_values(['CanonKey', 'Face'], ascending=[True, False])
    out = out.groupby('CanonKey', as_index=False).first()[['CanonKey', 'CompanyKey', 'Face', 'Fair']]
    return out


def analyze(ticker, periodA=None, periodB=None):
    cik = get_cik(ticker)
    equity_usd = get_shareholder_equity(cik) or 1_000_000_000

    fallback_notes = []
    if periodA and periodB:
        year_a = period_to_year(periodA)
        year_b = period_to_year(periodB)
        q_a = period_to_quarter(periodA)
        q_b = period_to_quarter(periodB)
        url_a, resolved_a, fb_a = fetch_filing_url_for_period(cik, periodA, allow_fallback=True, return_meta=True)
        url_b, resolved_b, fb_b = fetch_filing_url_for_period(cik, periodB, allow_fallback=True, return_meta=True)
        if fb_a:
            fallback_notes.append(f"periodA 请求 {periodA} 不可用，已回退到最近可用期 {resolved_a}")
        if fb_b:
            fallback_notes.append(f"periodB 请求 {periodB} 不可用，已回退到最近可用期 {resolved_b}")
        dispA, dispB = periodA, periodB
    else:
        url_a = fetch_latest_10k_url(cik)
        url_b = url_a
        m = re.search(r'-(\d{4})\d{4}\.', url_a)
        year_a = int(m.group(1)) if m else 2025
        year_b = year_a - 1
        q_a, q_b = None, None
        dispA, dispB = str(year_a), str(year_b)

    df_a = parse_period(url_a, year_a, q_a).rename(columns={'Face': 'Face_A', 'Fair': 'Fair_A', 'CompanyKey': 'CompanyKey_A'})
    df_b = parse_period(url_b, year_b, q_b).rename(columns={'Face': 'Face_B', 'Fair': 'Fair_B', 'CompanyKey': 'CompanyKey_B'})

    merged = pd.merge(df_a, df_b, on='CanonKey', how='inner')
    if len(merged) == 0:
        print(f'\n| 公司名 | {dispA} face value（金额百万美元，下同） | {dispA} fair value | {dispA} face/fair（用百分比表示） | {dispB} face | {dispB} fair | {dispB} face/fair（用百分比表示） | 期间face/fair变化 | 行业 | 细分行业 | 公司主要业务简介 |')
        print('|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|')
        return

    merged = merged[(merged['Face_A'] > 0) & (merged['Face_B'] > 0)]
    merged['DisplayName'] = merged['CompanyKey_A'].apply(_clean_display_name)
    merged['DisplayKey'] = merged['DisplayName'].apply(_display_name_key)

    merged = merged.groupby('DisplayKey', as_index=False).agg({
        'DisplayName': 'first',
        'Face_A': 'sum', 'Fair_A': 'sum',
        'Face_B': 'sum', 'Fair_B': 'sum',
    })
    merged['CompanyKey'] = merged['DisplayName']

    # Auto-detect unit scale (PSBD reports in full dollars)
    scale_a = 1_000_000 if merged['Face_A'].median() > 100_000 else (1000 if merged['Face_A'].median() > 2000 else 1)
    scale_b = 1_000_000 if merged['Face_B'].median() > 100_000 else (1000 if merged['Face_B'].median() > 2000 else 1)
    merged['Face_A_M'] = merged['Face_A'] / scale_a
    merged['Fair_A_M'] = merged['Fair_A'] / scale_a
    merged['Face_B_M'] = merged['Face_B'] / scale_b
    merged['Fair_B_M'] = merged['Fair_B'] / scale_b

    # Materiality filters
    threshold_a = (equity_usd / 1_000_000) * 0.001
    threshold_b = (equity_usd / 1_000_000) * 0.0002
    merged = merged[merged['Face_A_M'] > threshold_a]
    merged = merged[merged['Face_B_M'] > threshold_b]

    # Date artifact filter
    merged = merged[~merged['Face_B_M'].between(311.9, 312.2)]
    merged = merged[~merged['Face_A_M'].between(311.9, 312.2)]

    # NOTES exclusion
    merged = merged[~merged['CompanyKey'].str.contains(r'\b\d{4}\s+NOTES\b|\bNOTES\s+PAYABLE\b', case=False, regex=True, na=False)]

    merged['ratio_A'] = merged['Fair_A_M'] / merged['Face_A_M']
    merged['ratio_B'] = merged['Fair_B_M'] / merged['Face_B_M']
    merged['ratio_drop'] = merged['ratio_B'] - merged['ratio_A']

    out = merged[(merged['ratio_drop'] > 0) & (merged['ratio_A'] <= 1.0) & (merged['ratio_B'] <= 1.2)].sort_values('ratio_drop', ascending=False).head(20).copy()
    out = add_simple_business_intro(out)

    out['Face_A_fmt'] = out['Face_A_M'].map('{:.2f}'.format)
    out['Fair_A_fmt'] = out['Fair_A_M'].map('{:.2f}'.format)
    out['ratio_A_fmt'] = (out['ratio_A'] * 100).map('{:.2f}%'.format)
    out['Face_B_fmt'] = out['Face_B_M'].map('{:.2f}'.format)
    out['Fair_B_fmt'] = out['Fair_B_M'].map('{:.2f}'.format)
    out['ratio_B_fmt'] = (out['ratio_B'] * 100).map('{:.2f}%'.format)
    out['ratio_change_fmt'] = (-out['ratio_drop'] * 100).map('{:.2f}%'.format)

    show = out[[
        'CompanyKey', 'Face_A_fmt', 'Fair_A_fmt', 'ratio_A_fmt',
        'Face_B_fmt', 'Fair_B_fmt', 'ratio_B_fmt', 'ratio_change_fmt', '行业', '细分行业', '业务简介'
    ]]
    show.columns = [
        '公司名', f'{dispA} face value（金额百万美元，下同）', f'{dispA} fair value', f'{dispA} face/fair（用百分比表示）',
        f'{dispB} face', f'{dispB} fair', f'{dispB} face/fair（用百分比表示）', '期间face/fair变化', '行业', '细分行业', '公司主要业务简介'
    ]

    if fallback_notes:
        print('\n' + '；'.join(fallback_notes))
    print('\n' + tabulate(show, headers='keys', tablefmt='pipe', showindex=False))


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--ticker', required=True)
    p.add_argument('--periodA', required=False)
    p.add_argument('--periodB', required=False)
    args = p.parse_args()
    analyze(args.ticker, periodA=args.periodA, periodB=args.periodB)
