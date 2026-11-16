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
    """Detect key columns and candidate amount blocks for TSLX SoI tables."""
    if df.shape[1] < 9:
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
    # TSLX variants may label amount as Cost or Principal.
    cost_cols = [i for i, t in enumerate(tops) if (' cost' in t or t.strip().startswith('cost')) and 'amortized cost' not in t]
    principal_cols = find_cols('principal')
    fair_cols = find_cols('fair value') + find_cols('fairvalue')

    amount_cols = amort_cols if amort_cols else (cost_cols if cost_cols else principal_cols)

    if not company_cols or not amount_cols or not fair_cols:
        # TSLX continuation tables may lose header labels after pagination.
        # Wider 27-col layout.
        if df.shape[1] >= 22:
            return {
                'company': 0,
                'invest': 2 if df.shape[1] > 2 else 0,
                'amort_cols': [16],
                'fair_cols': [21],
            }
        # Common 17-col layout in 10-Q SoI pages.
        if df.shape[1] >= 17:
            return {
                'company': 0,
                'invest': 2,
                'amort_cols': [11],
                'fair_cols': [14],
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
            # e.g. "June 30, 2024" or "December 31, 2023"
            m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*(20\d{2})', low)
            if m:
                year_dist = i
                year_val = int(m.group(2))
            else:
                # e.g. comparative header: "June 30, December 31, 2024 2023"
                m2 = re.search(r'june\s+30\s*,\s*december\s+31\s*,\s*(20\d{2})\s*(20\d{2})', low)
                if m2:
                    year_dist = i
                    year_val = int(m2.group(1))

        if soi_dist is not None and year_dist is not None:
            break

    # Some filings split heading text across nodes ("Schedule of Inve"),
    # so require year presence but do not require exact SoI phrase hit.
    if year_val is None:
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
    
    # Search preceding text: find the NEAREST period label
    target_dist = None
    nearest_other_dist = None
    for i, s in enumerate(tbl.find_all_previous(string=True, limit=260)):
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
    # If a different period's label is CLOSER (smaller distance), this table belongs to that period
    if nearest_other_dist is not None and nearest_other_dist < target_dist:
        return False
    return True

def _parse_obdc_year(url: str, target_year: int, target_quarter: int | None = None) -> pd.DataFrame:
    soup = BeautifulSoup(_retry_get_content(url), 'lxml')

    all_rows = []
    carry_company = None  # handle page-break continuation tables with blank company cells

    tables = soup.find_all('table')

    # Build candidate list first, then fill year=None by nearest known-year table.
    # --- TSLX SoI tables: find header tables then include continuation tables ---
    # Step 1: Find SoI header tables (have "Company" + "Amortized Cost" + "Fair Value" in header rows)
    soi_headers = []  # (table_index, year)
    for idx, tbl in enumerate(tables):
        txt = tbl.get_text(' ', strip=True).lower()[:500]
        if 'company' in txt and 'amortized cost' in txt and 'fair value' in txt:
            # Skip roll-forward / derivative / summary tables
            if any(k in txt for k in ['gross additions', 'gross reductions', 'net change in unrealized',
                                       'interest rate swap', 'in millions', 'derivative']):
                continue
            year = _infer_table_context_year(tbl)
            soi_headers.append((idx, year))

    # Step 2: For each SoI header, include it + all subsequent tables with same column width
    #         until we hit another SoI header or a clearly non-SoI table
    candidates = []
    for h_pos, (h_idx, h_year) in enumerate(soi_headers):
        # Find next SoI header's index (or end of tables)
        next_h_idx = soi_headers[h_pos + 1][0] if h_pos + 1 < len(soi_headers) else len(tables)

        # Get expected column count from header table
        try:
            h_df = pd.read_html(StringIO(str(tables[h_idx])))[0]
            expected_cols = h_df.shape[1]
        except:
            continue

        # Include header table
        txt = tables[h_idx].get_text(' ', strip=True).lower()
        candidates.append((h_idx, tables[h_idx], txt, h_year))

        # Include continuation tables (same col width, no new header, before next SoI header)
        for cidx in range(h_idx + 1, next_h_idx):
            ctbl = tables[cidx]
            ctxt = ctbl.get_text(' ', strip=True).lower()
            # Stop if this looks like a non-SoI table (swap, summary, roll-forward)
            ctxt_head = ctxt[:200]
            if any(k in ctxt_head for k in ['interest rate swap', 'gross additions', 'gross reductions',
                                              'net change in unrealized', 'in millions']):
                break
            # Check column count matches (±2)
            try:
                cdf = pd.read_html(StringIO(str(ctbl)))[0]
                if abs(cdf.shape[1] - expected_cols) > 2:
                    break
                if cdf.shape[0] < 3:  # too few rows, probably summary
                    break
            except:
                break
            candidates.append((cidx, ctbl, ctxt, h_year))

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

        # map by paired order: left block tends older, right block tends current in OBDC 2025 filing
        pairs = list(zip(amort_cols[:len(fair_cols)], fair_cols[:len(amort_cols)]))
        if not pairs:
            continue
        resolved_years = [yy for _, _, _, yy in resolved if yy is not None]
        primary_year = max(resolved_years) if resolved_years else target_year
        if target_year == primary_year:
            amort_candidates = [p[0] for p in pairs[::-1]]
            fair_candidates = [p[1] for p in pairs[::-1]]
        else:
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

            a, b = detail.get(current, (0.0, 0.0))
            detail[current] = (a + f, b + r)

        # TSLX: subtotal rows are INDUSTRY-level totals, NOT company-level.
        # Always use detail rows (individual instruments per company).
        # Do NOT use subtotal rows — they would overwrite the last company in each
        # industry section with the entire industry's total.
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
    # prefer rows with non-zero fair first, then larger face/fair.
    out['FairPos'] = (out['Fair'] > 0).astype(int)
    out = out.sort_values(['CanonKey', 'FairPos', 'Face', 'Fair', 'IsSubtotal'], ascending=[True, False, False, False, False])
    out = out.groupby('CanonKey', as_index=False).first()[['CanonKey', 'CompanyKey', 'Face', 'Fair']]
    return out


def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Parse a single period's holdings from a filing URL."""
    return _parse_obdc_year(url, target_year, target_quarter)
