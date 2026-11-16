#!/usr/bin/env python3
"""
normalize_names.py — Company name normalizer for BDC holdings database.

Rules:
1. Remove pure footnote markers: (1), (2), (K), (Q), (1)(2)(3), etc.
2. DBA/D/B/A: keep the DBA name as primary, legal name as alias
   "FDUS 2KDIRECT, INC. (DBA IPROMOTE)" → "IPROMOTE (2KDIRECT, INC.)"
3. FKA/F/K/A: keep current name, drop former
   "FL HAWK (F/K/A FINELINE TECHNOLOGIES)" → "FL HAWK"
4. Operating company in parens (no DBA/FKA prefix): keep both, primary is the more readable one
   "EBSC HOLDINGS LLC (RIDDELL, INC.)" → "RIDDELL, INC. (EBSC HOLDINGS LLC)"
5. Geographic: keep in name for disambiguation
   "BANDON FITNESS (TEXAS) INC." → keep as-is
6. Instrument details in parens: remove
   "(TERM LOAN B)", "(DELAYED DRAW)", "(SECOND OUT)" → remove
7. Share counts: remove
   "(2,032,258 CLASS A UNITS)" → remove

After normalization, entries with same canonical key should be merged (sum face/fair).
"""

import re


def _is_footnote(inner: str) -> bool:
    """Pure footnote markers: digits, single letters, or combos."""
    # (1), (2), (K), (Q), (1)(2), etc.
    return bool(re.match(r'^[\d]+$', inner) or re.match(r'^[A-Z]{1,2}$', inner))


def _is_instrument_detail(inner: str) -> bool:
    """Instrument/tranche info that should be removed."""
    up = inner.upper()
    keywords = [
        'TERM LOAN', 'REVOLVER', 'DELAYED DRAW', 'FIRST LIEN', 'SECOND LIEN',
        'SUPER SENIOR', 'SECOND OUT', 'FIRST OUT', 'LAST OUT',
        'CLASS A', 'CLASS B', 'CLASS C', 'SERIES A', 'SERIES B',
        'WARRANT', 'PREFERRED', 'COMMON STOCK', 'COMMON EQUITY',
    ]
    return any(k in up for k in keywords)


def _is_share_count(inner: str) -> bool:
    """Share/unit counts: '2,032,258 CLASS A UNITS', '108,333 shares'."""
    return bool(re.match(r'^[\d,]+\s+(shares|units|class|series)', inner, re.I))


def _is_country(inner: str) -> bool:
    """Country/state tags."""
    countries = {
        'CANADA', 'UNITED KINGDOM', 'FRANCE', 'GERMANY', 'AUSTRALIA',
        'ISRAEL', 'INDIA', 'IRELAND', 'SINGAPORE', 'NETHERLANDS',
        'JAPAN', 'SPAIN', 'ITALY', 'BRAZIL', 'MEXICO', 'SWEDEN',
        'NORWAY', 'DENMARK', 'FINLAND', 'SWITZERLAND', 'BELGIUM',
        'LUXEMBOURG', 'CAYMAN ISLANDS', 'BERMUDA', 'UK',
    }
    return inner.upper().strip() in countries


def _is_state(inner: str) -> bool:
    """US state names."""
    states = {
        'TEXAS', 'DELAWARE', 'CALIFORNIA', 'NEW YORK', 'FLORIDA',
        'ILLINOIS', 'OHIO', 'MICHIGAN', 'VIRGINIA', 'PENNSYLVANIA',
        'GEORGIA', 'MARYLAND', 'MASSACHUSETTS', 'NEW JERSEY',
        'NORTH CAROLINA', 'COLORADO', 'WASHINGTON', 'NEVADA',
        'CONNECTICUT', 'OREGON', 'ARIZONA',
    }
    return inner.upper().strip() in states


def _looks_like_company_name(inner: str) -> bool:
    """Does the parenthetical content look like an actual company name?"""
    up = inner.upper().strip()
    # Fund/tranche designators: "Off I", "On II", "Fund III" — NOT company names
    if re.match(r'^(OFF|ON|FUND)\s+[IVX]+$', up):
        return False
    # Has company suffix
    if any(s in up for s in [' LLC', ' INC', ' LTD', ' LP', ' CORP', 'L.P.', 'S.A.', 'GMBH', 'B.V.']):
        return True
    # Multi-word proper name (likely a trading name)
    words = up.split()
    if len(words) >= 2 and not any(c.isdigit() for c in up[:3]):
        return True
    # Single capitalized word that's not a common abbreviation or section term
    skip_words = {
        'HOLDING', 'HOLDINGS', 'OFFSHORE', 'DELAWARE', 'JADE', 'ALTI',
        'FJMF', 'VOFF', 'IPCOM', 'OFF', 'CORPORATE',
    }
    if len(words) == 1 and len(up) >= 4 and up.isalpha() and up not in skip_words:
        return True
    return False


def normalize_company_name(raw_name: str) -> str:
    """
    Normalize a BDC portfolio company name.
    Returns cleaned name suitable for display and matching.
    """
    name = raw_name.strip()
    if not name:
        return name

    # Step 1: Process all parenthetical groups left-to-right
    # Split into base name and list of parenthetical groups
    parts = re.split(r'(\([^)]*\))', name)

    base_parts = []
    dba_name = None
    operating_name = None
    kept_parens = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if part.startswith('(') and part.endswith(')'):
            inner = part[1:-1].strip()

            # Footnote markers: remove
            if _is_footnote(inner):
                continue
            # Chained footnotes like (1)(2)(3) — already split, each is handled

            # DBA / D/B/A
            m_dba = re.match(r'(?:DBA|D/B/A)\s+(.+)', inner, re.I)
            if m_dba:
                dba_name = m_dba.group(1).strip()
                continue

            # FKA / F/K/A / F.K.A. / FORMERLY
            m_fka = re.match(r'(?:FKA|F/K/A|F\.K\.A\.?|FORMERLY)\s+(.+)', inner, re.I)
            if m_fka:
                continue

            # AKA
            m_aka = re.match(r'(?:AKA|A/K/A)\s+(.+)', inner, re.I)
            if m_aka:
                operating_name = m_aka.group(1).strip()
                continue

            # Share counts: remove
            if _is_share_count(inner):
                continue

            # Instrument details: remove
            if _is_instrument_detail(inner):
                continue

            # Country: keep
            if _is_country(inner):
                kept_parens.append(part)
                continue

            # State: keep
            if _is_state(inner):
                kept_parens.append(part)
                continue

            # Looks like an operating company name: treat as DBA-like
            if _looks_like_company_name(inner):
                operating_name = inner
                continue

            # Short identifiers (fund names, tranche IDs): keep
            kept_parens.append(part)

        else:
            base_parts.append(part)

    base = ' '.join(base_parts).strip()
    # Clean up multiple spaces, stray commas from removed parentheticals, and trailing punctuation
    base = re.sub(r'\s+', ' ', base).strip()
    base = re.sub(r'\s+,', ',', base)  # fix " ," → ","
    base = re.sub(r'[,\s]+$', '', base)

    # Step 2: Construct final name
    if dba_name:
        # DBA name becomes primary
        final = dba_name
    elif operating_name:
        # Operating name becomes primary, holdco as context
        final = operating_name
    else:
        final = base

    # Add kept parenthetical groups
    if kept_parens:
        final = final + ' ' + ' '.join(kept_parens)

    # Step 3: Strip trailing instrument/tranche suffixes (outside parentheses)
    # These appear as ", REVOLVER" or " - FUNDED REVOLVER" etc.
    # Must run AFTER parenthetical processing to avoid stripping from (TERM LOAN A2) which is already handled.
    _instrument_suffix_patterns = [
        # " - FUNDED/UNFUNDED REVOLVER/TERM LOAN [qualifier]" (PNNT, PFLT style)
        r'\s*-\s*(?:FUNDED|UNFUNDED)\s+(?:REVOLVER|TERM\s+LOAN)(?:\s+[A-Z0-9\-]+)*',
        # " - INCREMENTAL TERM LOAN" / " - TAKEBACK TL" / " - PRIORITY TL"
        r'\s*-\s*(?:INCREMENTAL|TAKEBACK|PRIORITY)\s+(?:TERM\s+LOAN|TL)',
        # " - FIRST OUT / SECOND OUT / LAST-OUT ... TERM LOANS"
        r'\s*-\s*(?:FIRST|SECOND|LAST)[\s\-]*OUT\s+(?:NEW\s+MONEY\s+)?TERM\s+LOANS?',
        # " - CONVERTIBLE NOTES" / " - UNFUNDED CONVERTIBLE NOTES" / " - CONVERTIBLE NOTE"
        r'\s*-\s*(?:UNFUNDED\s+)?CONVERTIBLE\s+NOTES?',
        # " - UNFUNDED" (bare, without REVOLVER/TERM LOAN qualifier — PNNT equity)
        r'\s*-\s*UNFUNDED$',
        # " - SUB DEBT" / " - SUBORDINATED DEBT"
        r'\s*-\s*(?:SUB|SUBORDINATED)\s+DEBT',
        # " - CLASS A/AA/B/C" (equity class suffix, NOT inside parens)
        r'\s*-\s*CLASS\s+[A-Z]{1,3}\b',
        # " - REVOLVER (8)" style (with optional footnote)
        r'\s*-\s*REVOLVER(?:\s*\(\d+\))?',
        # ", REVOLVER" / ", TERM LOAN" / ", TERM LOAN B" (FSK style)
        r',\s*REVOLVER\b',
        r',\s*TERM\s+LOAN(?:\s+[A-Z0-9])?\b',
        # ", TRANCHE A/B/C TERM LOAN" / ", TRANCHE A/B/C LOAN" / ", ROLL UP TERM LOAN" (CSWC style)
        r',\s*TRANCHE\s+[A-Z]\s+(?:TERM\s+)?LOAN',
        r',\s*ROLL\s+UP\s+TERM\s+LOAN',
        # ", TRANCHE B TERM LOAN" at end
        r',\s*TRANCHE\s+[A-Z]\s+TERM\s+LOAN',
    ]
    final_upper = final.upper()
    for pat in _instrument_suffix_patterns:
        m = re.search(pat, final_upper)
        if m:
            # Strip the matched suffix from the original (preserving original case)
            final = final[:m.start()].rstrip()
            final_upper = final.upper()

    # Final cleanup
    final = re.sub(r'\s+', ' ', final).strip()
    final = re.sub(r'[,\s]+$', '', final)

    return final


def canonical_key(name: str) -> str:
    """Generate a canonical key for matching across periods."""
    n = normalize_company_name(name).upper()
    # Remove all parenthetical content for matching
    n = re.sub(r'\([^)]*\)', '', n)
    # Normalize punctuation before company suffixes for consistent matching:
    # "COMPANY. LLC" → "COMPANY, LLC", "COMPANY LLC" → "COMPANY, LLC" (add comma)
    n = re.sub(r'[.,]\s*(LLC|INC|LTD|LP|CORP)\b', r', \1', n)
    n = re.sub(r'(\w)\s+(LLC|INC|LTD|LP|CORP)\b', r'\1, \2', n)
    # Remove trailing punctuation and common suffixes for fuzzy matching
    n = re.sub(r'\s+', ' ', n).strip()
    n = re.sub(r'[,.\s]+$', '', n)
    return n


# ── Test ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        # Footnotes
        ('FIRST BRANDS GROUP, LLC (4)(8)', 'FIRST BRANDS GROUP, LLC'),
        ('BCSF ACAMS (14)(19)(25)', 'ACAMS'),
        ('HW ACQUISITION, LLC(Q)(R)(T)(V)', 'HW ACQUISITION, LLC'),
        # DBA
        ('2KDIRECT, INC. (DBA IPROMOTE)', 'IPROMOTE'),
        ('301 EDISON HOLDINGS INC. (DBA LGG INDUSTRIAL)', 'LGG INDUSTRIAL'),
        ('ALLREDI, LLC (FKA MARCO GROUP INTERNATIONAL OPCO, LLC)', 'ALLREDI, LLC'),
        # FKA
        ('FL HAWK INTERMEDIATE HOLDINGS, INC. (F/K/A FINELINE TECHNOLOGIES, INC.)', 'FL HAWK INTERMEDIATE HOLDINGS, INC.'),
        # Operating name in parens
        ('EBSC HOLDINGS LLC (RIDDELL, INC.)', 'RIDDELL, INC.'),
        ('CMT OPCO HOLDING, LLC (CONCEPT MACHINE)', 'CONCEPT MACHINE'),
        ('MEDIA RECOVERY, INC. (SPOTSEE)', 'SPOTSEE'),
        # Country
        ('BIG BUS TOURS GROUP LIMITED (UNITED KINGDOM)', 'BIG BUS TOURS GROUP LIMITED (UNITED KINGDOM)'),
        # Share count
        ('ASCENT MIDCO, LLC (2,032,258 CLASS A UNITS)', 'ASCENT MIDCO, LLC'),
        # Instrument
        ('CENTRIC BRANDS INC. (TERM LOAN A2)', 'CENTRIC BRANDS INC.'),
        ('MORAE GLOBAL CORPORATION (TERM LOAN A)', 'MORAE GLOBAL CORPORATION'),
        # AKA
        ('THE CAPROCK GROUP, INC. (AKA TA/TCG HOLDINGS, LLC)', 'TA/TCG HOLDINGS, LLC'),
        # Instrument suffixes (FSK style: ", REVOLVER" / ", TERM LOAN")
        ('BAUSCH HEALTH COS INC, REVOLVER', 'BAUSCH HEALTH COS INC'),
        ('AVENUE ONE PROPCO, TERM LOAN', 'AVENUE ONE PROPCO'),
        ('GREENSKY HOLDINGS LLC, TERM LOAN', 'GREENSKY HOLDINGS LLC'),
        # Instrument suffixes (CSWC style: ", TRANCHE X TERM LOAN")
        ('LIFT BRANDS, INC., TRANCHE A TERM LOAN', 'LIFT BRANDS, INC.'),
        ('LIFT BRANDS, INC., TRANCHE B TERM LOAN', 'LIFT BRANDS, INC.'),
        ('INSURE HOMES CORPORATION, TRANCHE B TERM LOAN', 'INSURE HOMES CORPORATION'),
        ('ZIPS CAR WASH, LLC, ROLL UP TERM LOAN', 'ZIPS CAR WASH, LLC'),
        # Instrument suffixes (PNNT style: " - FUNDED/UNFUNDED REVOLVER")
        ('KINETIC PURCHASER, LLC - FUNDED REVOLVER', 'KINETIC PURCHASER, LLC'),
        ('KINETIC PURCHASER, LLC - UNFUNDED REVOLVER', 'KINETIC PURCHASER, LLC'),
        ('KINETIC PURCHASER, LLC - CLASS AA', 'KINETIC PURCHASER, LLC'),
        ('LAV GEAR HOLDINGS, INC. - INCREMENTAL TERM LOAN', 'LAV GEAR HOLDINGS, INC.'),
        ('STG DISTRIBUTION, LLC - FIRST OUT NEW MONEY TERM LOANS', 'STG DISTRIBUTION, LLC'),
        ('STG DISTRIBUTION, LLC - SECOND OUT TERM LOANS', 'STG DISTRIBUTION, LLC'),
        ('ORL HOLDCO, INC. - CONVERTIBLE NOTES', 'ORL HOLDCO, INC.'),
        ('SATH INDUSTRIES, LLC- UNFUNDED TERM LOAN B', 'SATH INDUSTRIES, LLC'),
        # Country tags should NOT be stripped
        ('JOBANDTALENT USA, INC. (UNITED KINGDOM)', 'JOBANDTALENT USA, INC. (UNITED KINGDOM)'),
        ('STARREZ (AUSTRALIA)', 'STARREZ (AUSTRALIA)'),
    ]

    print(f'{"Input":65s} → {"Output":45s} {"Pass":4s}')
    print('-' * 120)
    passed = 0
    for raw, expected in tests:
        result = normalize_company_name(raw)
        ok = result == expected
        passed += ok
        mark = '✅' if ok else '❌'
        print(f'{raw:65s} → {result:45s} {mark}')
        if not ok:
            print(f'  Expected: {expected}')

    print(f'\n{passed}/{len(tests)} passed')
