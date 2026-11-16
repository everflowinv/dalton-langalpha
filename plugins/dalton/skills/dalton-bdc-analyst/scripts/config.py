"""
config.py — Shared configuration for BDC Analyst skill.

Single source of truth for:
- PROFILE_MAP: ticker → parser module mapping
- SCALE_OVERRIDES: ticker → forced scale (when auto-detection fails)
- UNSUPPORTED: tickers we can't parse
"""

# ── Profile map: ticker → parser module ──────────────────────────────

PROFILE_MAP = {
    'ARCC': 'bdc_profiles.arcc',
    'BBDC': 'bdc_profiles.bbdc',
    'BCIC': 'bdc_profiles.bcic',
    'BCSF': 'bdc_profiles.bcsf',
    'BXSL': 'bdc_profiles.bxsl',
    'CCAP': 'bdc_profiles.ccap',
    'CGBD': 'bdc_profiles.cgbd',
    'CION': 'bdc_profiles.cion',
    'CSWC': 'bdc_profiles.cswc',
    'FDUS': 'bdc_profiles.fdus',
    'FSK':  'bdc_profiles.fsk',
    'GAIN': 'bdc_profiles.gain',
    'GBDC': 'bdc_profiles.gbdc',
    'GECC': 'bdc_profiles.gecc',
    'GLAD': 'bdc_profiles.glad',
    'GSBD': 'bdc_profiles.gsbd',
    'HRZN': 'bdc_profiles.hrzn',
    'HTGC': 'bdc_profiles.htgc',
    'KBDC': 'bdc_profiles.kbdc',
    'MAIN': 'bdc_profiles.main',
    'MFIC': 'bdc_profiles.mfic',
    'MRCC': 'bdc_profiles.mrcc',
    'MSDL': 'bdc_profiles.msdl',
    'MSIF': 'bdc_profiles.msif',
    'NCDL': 'bdc_profiles.ncdl',
    'NMFC': 'bdc_profiles.nmfc',
    'OBDC': 'bdc_profiles.obdc',
    'OCSL': 'bdc_profiles.ocsl',
    'OTF':  'bdc_profiles.otf',
    'OXSQ': 'bdc_profiles.oxsq',
    'PFLT': 'bdc_profiles.pflt',
    'PNNT': 'bdc_profiles.pnnt',
    'PSBD': 'bdc_profiles.psbd',
    'PSEC': 'bdc_profiles.psec',
    'RWAY': 'bdc_profiles.rway',
    'SAR':  'bdc_profiles.sar',
    'SCM':  'bdc_profiles.scm',
    'SLRC': 'bdc_profiles.slrc',
    'SSSS': 'bdc_profiles.ssss',
    'TCPC': 'bdc_profiles.tcpc',
    'TPVG': 'bdc_profiles.tpvg',
    'TRIN': 'bdc_profiles.trin',
    'TSLX': 'bdc_profiles.tslx',
    'WHF':  'bdc_profiles.whf',
}

# ── Scale overrides ──────────────────────────────────────────────────
# When auto-detection fails (median-based heuristic fooled by equity rows
# or unusual value distribution), force a known scale.
# Values: 1 (already millions), 1000 (thousands → millions), 1_000_000 (dollars → millions)

SCALE_OVERRIDES = {
    'HRZN': 1000,       # "dollars in thousands" but low median fools detector
    'SCM':  1_000_000,  # full dollars; profile returns raw dollar amounts
    'OXSQ': 1_000_000,  # full dollars
    'PNNT': 1000,       # "in thousands" per filing; equity rows fool median detection
    'TPVG': 1000,       # "dollars in thousands"; small equity values fool detector
    'MRCC': 1000,       # "in thousands" per filing
}

# ── Unsupported tickers ──────────────────────────────────────────────
# These tickers cannot be parsed (no standard Schedule of Investments).

UNSUPPORTED = {'REFI'}  # Chicago Atlantic — cannabis REIT, no standard SoI

# Tickers missing from SEC company_tickers.json (merged / renamed / delisted).
# Skipped by default during `--ticker all`; historical seed/workspace data remains readable by compare.py.
SEC_TICKER_UNAVAILABLE = {
    'MRCC': 'SEC 代码表中已查不到',
    'SSSS': 'SEC 代码表中已查不到',
}

# ── BDC mergers (known QoQ jumps that are NOT parser bugs) ───────────
# Format: (ticker, period_after, description)
# Used by sanity checks to suppress false warnings.

KNOWN_MERGERS = [
    ('OTF',  '25Q1', 'OTF + OTF II merger (Mar 2025)'),
    ('CGBD', '25Q1', 'Carlyle Secured Lending merger (Jan 2025)'),
    ('MFIC', '25Q1', 'MidCap Financial merger (Jan 2025)'),
    ('OBDC', '25Q1', 'OBDC + OBDE merger (Jan 2025)'),
]
