import re
from collections import defaultdict

import pandas as pd
import requests
from lxml import etree

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
    r = requests.get(base + 'index.json', headers=get_headers(), timeout=120)
    r.raise_for_status()
    items = r.json().get('directory', {}).get('item', [])
    cands = [it.get('name', '') for it in items if it.get('name', '').lower().endswith('_htm.xml')]
    cands = sorted(cands, key=lambda n: (0 if n.lower().startswith('cswc-') else 1, len(n)))
    return base + cands[0] if cands else filing_url


def _company_only(name: str) -> str:
    s = str(name)
    s = s.split('|')[0].strip()
    s = re.sub(r'\(\d+\)', '', s)
    # Strip FKA/F/K/A parenthetical from company name
    s = re.sub(r'\((?:fka|f/k/a|formerly)\s+[^)]+\)', '', s, flags=re.I)
    s = re.split(r',\s*(First lien|Second lien|Unitranche|Revolving|Delayed Draw|Common|Preferred|Warrants|Subordinated|LLC Interest|Equity|Debt|Shares?)\b', s, flags=re.I)[0]
    s = re.sub(r',\s*CLASS\s+[A-Z0-9\-]+\s+(?:Preferred\s+|Common\s+)?(UNITS?|STOCK|SHARES?)\b.*$', '', s, flags=re.I)
    s = re.sub(r',\s*(?:Preferred|Common)\s+(UNITS?|STOCK|SHARES?)\b.*$', '', s, flags=re.I)
    s = re.sub(r',\s*(UNITS?|STOCK|SHARES?)\b.*$', '', s, flags=re.I)
    s = re.sub(r'\s*,\s*', ', ', s)  # normalize commas
    s = re.sub(r'\s+', ' ', s).strip(' ,;')
    return s


def _company_key(name: str) -> str:
    s = _company_only(name).upper()
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _instrument_key(name: str) -> str:
    s = str(name).upper()
    s = re.sub(r'\(\d+\)', '', s)
    # Strip FKA/F/K/A parenthetical: "(FKA OLD NAME)" → ""
    s = re.sub(r'\((?:FKA|F/K/A|FORMERLY)\s+[^)]+\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    # Normalize whitespace around commas: " , " → ", "
    s = re.sub(r'\s*,\s*', ', ', s)
    return s


def _instant_from_period(period: str) -> str:
    y = period_to_year(period)
    q = period_to_quarter(period)
    md = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}[q]
    return f'{y}-{md}'


def _extract_from_xml(xml_url: str, target_instant: str) -> pd.DataFrame:
    root = etree.fromstring(requests.get(xml_url, headers=get_headers(), timeout=120).content)

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

    # context-level dedup: keep max cost/fair in each context (avoid duplicate facts)
    by_ctx = defaultdict(lambda: {'member': '', 'face': 0.0, 'fair': 0.0})
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
        v = float(txt)
        if v < 0:
            continue

        by_ctx[cref]['member'] = ctx_member[cref]
        if tag == 'InvestmentOwnedAtCost':
            by_ctx[cref]['face'] = max(by_ctx[cref]['face'], v)
        else:
            by_ctx[cref]['fair'] = max(by_ctx[cref]['fair'], v)

    # Build FKA mapping from member strings: "NEW CO (fka OLD CO), First Lien"
    # This maps old instrument keys to new instrument keys so duplicates get deduped
    fka_name_map = {}  # old_company_name → new_company_name (for instrument key rewriting)
    for rec in by_ctx.values():
        raw = rec['member']
        m = re.search(r'\((?:fka|f/k/a|formerly)\s+(.+?)\)', raw, re.I)
        if m:
            old_co = _company_only(m.group(1).strip())
            new_co = _company_only(raw)
            if old_co and new_co and old_co.upper() != new_co.upper():
                fka_name_map[old_co.upper()] = new_co.upper()

    # instrument-level dedup: same instrument can appear in multiple equivalent contexts
    # Also normalize FKA names so "RED DOG, First Lien" and "KINDRED (fka RED DOG), First Lien"
    # get the same instrument key and are deduped via max()
    by_instr = defaultdict(lambda: {'raw': '', 'face': 0.0, 'fair': 0.0})
    for rec in by_ctx.values():
        raw = rec['member']
        if not raw:
            continue
        # Normalize instrument key: replace old company name with new name
        ik = _instrument_key(raw)
        for old_upper, new_upper in fka_name_map.items():
            if old_upper in ik:
                ik = ik.replace(old_upper, new_upper)
                break
        # Also normalize instrument type variations: "Class A Preferred units" → "Class A Units"
        ik = re.sub(r'CLASS\s+([A-Z0-9])\s+PREFERRED\s+(UNITS?)', r'CLASS \1 \2', ik)

        by_instr[ik]['raw'] = raw
        by_instr[ik]['face'] = max(by_instr[ik]['face'], rec['face'])
        by_instr[ik]['fair'] = max(by_instr[ik]['fair'], rec['fair'])

    # company-level aggregation across debt/equity instruments
    comp = defaultdict(lambda: {'Name': '', 'Face': 0.0, 'Fair': 0.0})

    # Build company-level FKA mapping
    fka_co_map = {}  # old_company_key → new_company_key
    for rec in by_instr.values():
        raw = rec['raw']
        m = re.search(r'\((?:fka|f/k/a|formerly)\s+(.+?)\)', raw, re.I)
        if m:
            old_key = _company_key(m.group(1).strip())
            new_key = _company_key(raw)
            if old_key and new_key and old_key != new_key:
                fka_co_map[old_key] = new_key

    for rec in by_instr.values():
        key = _company_key(rec['raw'])
        if not key:
            continue
        # Resolve FKA at company level
        if key in fka_co_map:
            key = fka_co_map[key]
        if not comp[key]['Name'] or '(FKA' in rec['raw'].upper() or '(F/K/A' in rec['raw'].upper():
            new_name = _company_only(rec['raw']).upper()
            if len(new_name) > len(comp[key]['Name']):
                comp[key]['Name'] = new_name
        comp[key]['Face'] += rec['face']
        comp[key]['Fair'] += rec['fair']

    rows = []
    for k, v in comp.items():
        if v['Face'] > 0 or v['Fair'] > 0:
            rows.append({'CompanyKey': v['Name'] or k, 'Face': float(v['Face']), 'Fair': float(v['Fair'])})

    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['CompanyKey', 'Face', 'Fair'])

def parse_period(url: str, target_year: int, target_quarter: int | None = None) -> 'pd.DataFrame':
    """Public parse_period interface for harvest.py. Uses XBRL XML extraction."""
    xml_url = _resolve_main_xml_url(url)
    quarter_months = {1: '03', 2: '06', 3: '09', 4: '12'}
    if target_quarter:
        month = quarter_months.get(target_quarter, '12')
        last_days = {'03': '31', '06': '30', '09': '30', '12': '31'}
        instant = f'{target_year}-{month}-{last_days[month]}'
    else:
        instant = f'{target_year}-12-31'
    df = _extract_from_xml(xml_url, instant)
    if df is not None and not df.empty:
        # Normalize to CompanyKey/Face/Fair
        for old, new in [('Company', 'CompanyKey'), ('company', 'CompanyKey')]:
            if old in df.columns:
                df = df.rename(columns={old: 'CompanyKey'})
                break
    return df

