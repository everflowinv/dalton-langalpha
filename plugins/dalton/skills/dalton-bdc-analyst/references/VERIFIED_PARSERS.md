# Verified Parsers — DO NOT MODIFY

Parsers on this list have been manually reviewed and confirmed correct by CY.
**They must NOT be modified without explicit approval from CY.**

Any code change to `bdc_profiles/{ticker}.py` for a verified ticker requires CY's sign-off first.

## Verified Tickers

- ARCC ✅ (verified 2026-03-28)
- BBDC ✅ (verified 2026-03-28)
- BCIC ✅ (verified 2026-03-28)
- BCSF ✅ (verified 2026-03-28)
- BXSL ✅ (verified 2026-03-28)
- CCAP ✅ (verified 2026-03-28)

## Clean (QoQ <20%, full 8Q 24Q1-25Q4)

32 tickers with consistent results across all 8 quarters:

ARCC, BBDC, BCSF, BXSL, CCAP, CSWC, FDUS, FSK, GAIN, GLAD, GSBD, HRZN, HTGC,
KBDC, MAIN, MSDL, MSIF, NCDL, NMFC, OCSL, OXSQ, PNNT, PSEC, RWAY, SAR, SCM,
SSSS, TCPC, TPVG, TRIN, TSLX, WHF

## Known Mergers (large QoQ jumps, NOT parser bugs)

| Ticker | Jump | Event |
|--------|------|-------|
| OTF | 24Q4→25Q1 +90% | OTF + OTF II merger (Mar 2025) |
| CGBD | 24Q4→25Q1 +60% | Carlyle Secured Lending merger (Jan 2025) |
| MFIC | 24Q4→25Q1 +44% | MidCap Financial merger (Jan 2025) |
| OBDC | 24Q4→25Q1 +36% | OBDC + OBDE merger (Jan 2025) |

## Needs Investigation

| Ticker | Issue | Status |
|--------|-------|--------|
| PFLT | 24Q3→24Q4 +65% ($1,411M→$2,324M) | Not a corporate merger. Possibly PSSL JV consolidation or parser issue. |
| SLRC | 24Q4→25Q1 +53% ($1,295M→$1,982M) | Possibly Eagle Point merger or parser issue. |
| GBDC | 24Q1→24Q2 +31% | May be genuine growth or filing format change. |

## Known Missing Periods

| Ticker | Period | Reason |
|--------|--------|--------|
| BCIC | 24Q1 | Filing format incompatible (0 holdings parsed) |
| MRCC | 24Q1 | Filing format incompatible (0 holdings parsed) |
| SLRC | 24Q1 | 35-col XBRL format, parser reads equity section only (48 holdings/$3M vs expected ~$1,500M) |

## 2026 Format Adaptations (`target_year >= 2026`)

Starting with 2026 SEC filings, several parsers introduced guarded 2026+ handling around `target_year >= 2026` (or dynamic `primary_year` detection) so 24Q1–25Q4 parsing stays regression-free:

- **`BCSF`** (`scripts/bdc_profiles/bcsf.py`): For `target_year >= 2026`, accepts `"Market Value"` alongside `"Fair Value"`, skips the column-width mismatch `break` across alternating 22-col and 34–36-col tables, and restricts adjacent-cell probing (`probe_offsets=(0, 1)`) so blank `Cost` cells do not read into `Market Value`.
- **`GBDC`** (`scripts/bdc_profiles/gbdc.py`): For `target_year >= 2026`, reuses cached `last_colmap_by_width` across headerless wide continuation tables (`>= 50` columns), with extended lookback `limit=1200`.
- **Plural SoI heading & multi-year date regex**: `bdc_analyzer.is_soi_heading` matches both singular `"Schedule of Investments"` and plural `"Schedules of Investments"` across all profiles; **`ARCC`**, **`BXSL`**, **`FSK`**, **`HTGC`**, **`MAIN`**, **`PSEC`** replace hardcoded `202[45]` / `target_year == 2025` checks with `20\d{2}` and `primary_year = max(...)` (and `ARCC` filters out `for the ... months ended` rollforward tables).

If filing formats change again in **2027**, these `target_year >= 2026` branches and table-layout assumptions must be re-verified against live 2027 filings before harvesting.

---
*Last updated: 2026-11-15*
