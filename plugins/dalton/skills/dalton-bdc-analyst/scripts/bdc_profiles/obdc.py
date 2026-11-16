import re
from lxml import etree
import pandas as pd
import requests

from bdc_analyzer import (
    get_headers,
    period_to_year,
    period_to_quarter,
)


def _resolve_main_xml_url(filing_url: str) -> str:
    m = re.match(r'^(https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/)', filing_url)
    if not m:
        return filing_url
    base = m.group(1)
    idx = requests.get(base + 'index.json', headers=get_headers(), timeout=120)
    idx.raise_for_status()
    items = idx.json().get('directory', {}).get('item', [])

    # Prefer main inline-XBRL xml doc (obdc-YYYYMMDD_htm.xml)
    cands = []
    for it in items:
        n = it.get('name', '')
        low = n.lower()
        if low.endswith('_htm.xml'):
            cands.append(n)
    cands = sorted(cands, key=lambda n: (0 if n.lower().startswith('obdc-') else 1, len(n)))
    if cands:
        return base + cands[0]
    return filing_url


def _company_only(name: str) -> str:
    s = str(name)
    # Newer filings: "Company | Specialty finance ... | Affiliated"
    if '|' in s:
        s = s.split('|')[0].strip()

    # Strip loan/instrument suffix after comma
    s = re.split(r',\s*(First lien|Second lien|Unitranche|Unsecured|Common stock|Preferred|Warrants|Class\b|LLC Interest|Notes?|Term loan|Revolver)\b', s, flags=re.I)[0]

    # remove footnotes like (d), (14)
    s = re.sub(r'\(\w+\)', '', s)
    s = re.sub(r'\(\d+\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,;')
    return s


def _extract_company_rows_from_main_xml(xml_url: str, target_instant: str) -> pd.DataFrame:
    r = requests.get(xml_url, headers=get_headers(), timeout=120)
    r.raise_for_status()
    root = etree.fromstring(r.content)

    # context id -> investment identifier member text (same instant only)
    ctx_member = {}
    for c in root.findall('.//{http://www.xbrl.org/2003/instance}context'):
        cid = c.get('id')
        inst_node = c.find('.//{http://www.xbrl.org/2003/instance}instant')
        inst = inst_node.text.strip() if inst_node is not None and inst_node.text else ''
        if inst != target_instant:
            continue

        member = ''
        for tm in c.findall('.//{http://xbrl.org/2006/xbrldi}typedMember'):
            if 'InvestmentIdentifierAxis' in (tm.get('dimension') or ''):
                member = ''.join(tm.itertext()).strip()
                break
        if member:
            ctx_member[cid] = member

    rec = {}
    for el in root.iter():
        cref = el.get('contextRef')
        if cref not in ctx_member:
            continue

        tag = el.tag.split('}')[-1]
        if tag not in ('InvestmentOwnedAtCost', 'InvestmentOwnedAtFairValue'):
            continue

        txt = (el.text or '').strip()
        if not re.match(r'^-?\d+(\.\d+)?$', txt):
            continue
        val = float(txt)
        if val <= 0:
            continue

        key = ctx_member[cref]
        d = rec.setdefault(key, {'Face': None, 'Fair': None})
        if tag == 'InvestmentOwnedAtCost':
            d['Face'] = val
        elif tag == 'InvestmentOwnedAtFairValue':
            d['Fair'] = val

    rows = []
    for raw_name, vals in rec.items():
        if vals['Face'] and vals['Fair']:
            rows.append((raw_name, vals['Face'], vals['Fair']))

    if not rows:
        return pd.DataFrame(columns=['Company', 'Face', 'Fair'])

    df = pd.DataFrame(rows, columns=['RawName', 'Face', 'Fair'])
    df['Company'] = df['RawName'].map(_company_only)
    df = df[df['Company'].str.len() > 0]
    df = df.groupby('Company', as_index=False).agg({'Face': 'sum', 'Fair': 'sum'})
    return df


def _instant_from_period(period: str) -> str:
    y = period_to_year(period)
    q = period_to_quarter(period)
    md = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}[q]
    return f'{y}-{md}'

def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Public parse_period interface for harvest.py. Uses XBRL XML extraction."""
    xml_url = _resolve_main_xml_url(url)
    # Build instant string from year/quarter
    quarter_months = {1: '03', 2: '06', 3: '09', 4: '12'}
    if target_quarter:
        month = quarter_months.get(target_quarter, '12')
        # Find last day of month
        last_days = {'03': '31', '06': '30', '09': '30', '12': '31'}
        day = last_days[month]
        instant = f'{target_year}-{month}-{day}'
    else:
        instant = f'{target_year}-12-31'
    df = _extract_company_rows_from_main_xml(xml_url, instant)
    if df is not None and not df.empty and 'Company' in df.columns:
        df = df.rename(columns={'Company': 'CompanyKey'})
    return df

