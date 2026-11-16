from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

from bdc_analyzer import (
    get_headers,
    clean_num,
    normalize_company,
    extract_two_year_tables,
)


def _is_blank(v):
    s = '' if pd.isna(v) else str(v).strip()
    return s == '' or s.lower() in ('nan', 'none', '—', '-')


def _pick_series(df, col):
    out = df[col]
    if isinstance(out, pd.DataFrame):
        return out.apply(lambda row: ' '.join(row.fillna('').astype(str)), axis=1)
    return out


def _find_header_idx(df0):
    for i in range(min(16, len(df0))):
        row = ' '.join([str(v).lower() for v in df0.iloc[i].values])
        if 'fair value' in row and ('amortized cost' in row or 'cost' in row or 'principal' in row):
            return i
    return None


def _parse_group_subtotals(tbl):
    """Return (subtotals, members) from one SoI table.

    subtotals: {GROUP_KEY: (face, fair)}
    members: {GROUP_KEY: set(member_company_keys)}
    """
    try:
        df0 = pd.read_html(StringIO(str(tbl)))[0]
    except Exception:
        return {}, {}

    if len(df0) < 20:
        return {}, {}

    df0 = df0.dropna(axis=1, how='all')
    header_idx = _find_header_idx(df0)
    if header_idx is None:
        return {}, {}

    cols = [str(v).strip().lower().replace('\n', ' ') for v in df0.iloc[header_idx].values]
    df = df0.iloc[header_idx + 1:].copy()
    df.columns = cols

    company_col = None
    desc_col = None
    fair_candidates = []
    cost_candidates = []

    for c in df.columns:
        lc = str(c).lower()
        if company_col is None and ('portfolio company' in lc or 'issuer' in lc or lc == cols[0]):
            company_col = c
        if desc_col is None and ('investment' in lc or 'debt' in lc or 'equity' in lc or 'type' in lc):
            desc_col = c
        if 'fair value' in lc:
            fair_candidates.append(c)
        if 'amortized cost' in lc or ('cost' in lc and 'fair' not in lc) or 'principal' in lc:
            cost_candidates.append(c)

    if company_col is None or not fair_candidates or not cost_candidates:
        return {}, {}

    # use rightmost duplicated columns in this table block (MFIC layout)
    fair_col = fair_candidates[-1]
    cost_col = cost_candidates[-1]

    tmp = pd.DataFrame({
        'company': _pick_series(df, company_col).astype(str).str.replace(r'\n', ' ', regex=True).str.strip(),
        'desc': _pick_series(df, desc_col).astype(str).str.replace(r'\n', ' ', regex=True).str.strip() if desc_col is not None else '',
        'face': _pick_series(df, cost_col).apply(clean_num),
        'fair': _pick_series(df, fair_col).apply(clean_num),
    })

    subtotals = {}
    members = {}
    active_group = None

    for _, r in tmp.iterrows():
        comp = r['company']
        desc = r['desc']
        face = r['face']
        fair = r['fair']

        has_num = pd.notna(face) or pd.notna(fair)
        comp_blank = _is_blank(comp)
        desc_blank = _is_blank(desc)

        # group header row: text only
        if (not comp_blank) and (not has_num):
            low = comp.lower()
            if 'total' not in low and 'industry' not in low and 'schedule' not in low and 'portfolio' not in low:
                active_group = normalize_company(comp)
                members.setdefault(active_group, set())
            continue

        if not has_num:
            continue

        fv = 0.0 if pd.isna(face) else float(face)
        fr = 0.0 if pd.isna(fair) else float(fair)

        # subtotal row for active group
        if active_group and comp_blank and desc_blank:
            subtotals[active_group] = (fv, fr)
            active_group = None
            continue

        # detail rows under active group: collect member company keys
        if active_group and (not comp_blank):
            ckey = normalize_company(comp)
            members.setdefault(active_group, set()).add(ckey)

    # only keep groups that actually have subtotals
    members = {k: v for k, v in members.items() if k in subtotals}
    return subtotals, members


def _collect_group_candidates(soup):
    """Collect subtotal candidates per group from all parseable SoI-like tables.

    Returns {group: [{'face':..,'fair':..,'members':set(),'idx':table_idx}, ...]}
    """
    out = {}
    for idx, tbl in enumerate(soup.find_all('table')):
        txt = tbl.get_text(' ', strip=True).lower()
        if 'fair value' not in txt:
            continue

        subs, mems = _parse_group_subtotals(tbl)
        if not subs:
            continue

        for g, (f, r) in subs.items():
            out.setdefault(g, []).append({
                'face': float(f),
                'fair': float(r),
                'members': set(mems.get(g, set())),
                'idx': idx,
            })
    return out


def _split_candidates_to_year_maps(candidates):
    """Split subtotal candidates into current/prior maps using table-order heuristic.

    MFIC may show multiple subtotal-like candidates for the same group where "largest face"
    is not always the target period value. We therefore prefer earlier table blocks first,
    and only use face as a tie-breaker within the same table index.
    """
    subs25, mem25 = {}, {}
    subs24, mem24 = {}, {}

    for g, rows in candidates.items():
        rows = sorted(rows, key=lambda x: (x.get('idx', 10**9), -x.get('face', 0.0)))
        if not rows:
            continue

        subs25[g] = (rows[0]['face'], rows[0]['fair'])
        mem25[g] = set(rows[0]['members'])

        if len(rows) > 1:
            subs24[g] = (rows[1]['face'], rows[1]['fair'])
            mem24[g] = set(rows[1]['members'])

    return (subs25, mem25), (subs24, mem24)


def _apply_group_subtotals(df, subtotals, members):
    df = df.copy()

    # remove member rows + existing group row, then insert group subtotal row
    for g, (f, r) in subtotals.items():
        member_keys = set(members.get(g, set()))
        drop_mask = df['CompanyKey'].eq(g)
        if member_keys:
            drop_mask = drop_mask | df['CompanyKey'].isin(member_keys)
        df = df.loc[~drop_mask]
        df = pd.concat([df, pd.DataFrame([{'CompanyKey': g, 'Face': f, 'Fair': r}])], ignore_index=True)

    return df


def extract_two_year_tables_mfic(url):
    # base extraction from generic parser
    df25, df24 = extract_two_year_tables(url)

    res = requests.get(url, headers=get_headers(), timeout=120)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, 'lxml')

    candidates = _collect_group_candidates(soup)
    (subs25, mem25), (subs24, mem24) = _split_candidates_to_year_maps(candidates)

    if subs25:
        df25 = _apply_group_subtotals(df25, subs25, mem25)
    if subs24:
        df24 = _apply_group_subtotals(df24, subs24, mem24)

    return df25, df24

def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Public parse_period interface for harvest.py."""
    # MFIC extracts two years simultaneously from one URL
    # We return the df matching target_year
    try:
        df_a, df_b = extract_two_year_tables_mfic(url)
        # df_a is the primary (most recent) year, df_b is the prior year
        # Heuristic: if target_year matches the URL filename year, return df_a
        import re
        m = re.search(r'(\d{4})\d{2}\d{2}', url)
        file_year = int(m.group(1)) if m else None
        if file_year and file_year == target_year:
            return df_a
        else:
            return df_b
    except Exception:
        return __import__('pandas').DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

