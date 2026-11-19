#!/usr/bin/env python3
"""
Deterministic pre-publish quality gate for dalton-13f-tracker full-universe runs.

Checks:
1. Completeness: All 18 groups (19 SEC filers, Hillhouse = HHLR + Evergreen) present and ok=True.
2. Unverified / newly added tickers: Any active non-biotech ticker not in verified_tickers.json
   is flagged (with optional Yahoo Finance country/sector/industry lookup) so auto-learn never
   silently drops new AI/Semi/SaaS or China tickers into Other US Book.
3. Display name quality: Flags truncated names, SEC abbreviations (Matls, Instrs, Finl, Pwr, etc.),
   trailing legal/preposition tokens (Of, &, The, Com, Tr, Etf, Sa, etc.), 1-2 char stubs, and
   inconsistent names for the same ticker across funds.
4. Empty-ticker / CUSIP case-mismatch / unmerged split rows: Flags rows with missing Ticker or
   same-issuer NEW+CLOSED splits in formatted_report.
5. Category mismatch against verified_tickers.json: Ensures actual rendered category lines in
   each fund's JSON match verified_tickers.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_PY = SCRIPT_DIR / "13f_skill.py"
VERIFIED_LEDGER = SCRIPT_DIR / "verified_tickers.json"

EXPECTED_GROUPS = [
    (1, "hillhouse"),
    (2, "first_beijing"),
    (3, "aspex"),
    (4, "greenwoods"),
    (5, "sequoia_china"),
    (6, "capital_today"),
    (7, "brilliance"),
    (8, "yiheng"),
    (9, "tiger_global"),
    (10, "coatue"),
    (11, "d1_capital"),
    (12, "altimeter"),
    (13, "whale_rock"),
    (14, "dragoneer"),
    (15, "lone_pine"),
    (16, "sequoia_us"),
    (17, "durable"),
    (18, "berkshire"),
]

BAD_END_TOKENS = {
    "of", "&", "and", "the", "com", "tr", "etf", "sa", "nv", "plc", "pl", "ltd",
    "inc", "corp", "incorporated", "limited", "limite", "co", "del", "ag",
    "matls", "instrs", "pwr", "finl", "pmts", "pete", "mkts", "svcs",
    "indl", "ctzns", "fmrs", "entmt", "mng", "mtr", "pac", "cos", "ins",
    "transn", "sys", "hldgs", "hldg", "hldngs", "holdin", "intl",
    "technolo", "technol", "techno", "techn", "incorp", "corpor", "compan",
    "internation", "softwar",
}

BAD_ANYWHERE_TOKENS = {
    "matls", "instrs", "pwr", "finl", "pmts", "pete", "mkts", "svcs",
    "indl", "ctzns", "fmrs", "entmt", "mng", "mtr", "ctzn", "transn",
    "hldngs", "limite", "technolo", "internation", "softwar",
}

ALLOWED_SHORT_NAMES = {"JD"}

LINE_PATTERNS = [
    re.compile(r"^(?:继\d{2}Q\d(?:建仓|加仓|减仓)后，继续)?大幅加仓(.+?)至[\d.]+(?:亿|万)美元"),
    re.compile(r"^(?:继\d{2}Q\d(?:建仓|加仓|减仓)后，继续)?加仓(.+?) \d+%至"),
    re.compile(r"^(?:继\d{2}Q\d(?:建仓|加仓|减仓)后，继续)?减仓(.+?) \d+%至"),
    re.compile(r"^建仓(.+?)，"),
    re.compile(r"^清仓(.+?)（之前持仓"),
    re.compile(r"^(.+?)仓位(?:几乎)?不变，"),
]


def extract_name_from_line(line: str) -> str | None:
    s = line.strip().lstrip("- ").strip()
    for pat in LINE_PATTERNS:
        m = pat.match(s)
        if m:
            return m.group(1).strip()
    return None


def lookup_yf(ticker: str) -> dict:
    """Query Yahoo Finance metadata directly via yfinance when --lookup-new is passed."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
        if not isinstance(info, dict) or not info:
            return {"error": f"No company info for {ticker}"}
        return {
            "shortName": info.get("shortName") or info.get("longName"),
            "country": info.get("country"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }
    except Exception as e:
        return {"error": str(e)}


def _expected_period_prefix(quarter: str) -> str | None:
    m = re.match(r"^(\d{2,4})[Qq]([1-4])$", (quarter or "").strip())
    if not m:
        return None
    year = int(m.group(1))
    if year < 100:
        year += 2000
    month = {1: 3, 2: 6, 3: 9, 4: 12}[int(m.group(2))]
    return f"{year}-{month:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit 13F batch output before publishing.")
    parser.add_argument("--data-dir", required=True, help="Directory holding 01_hillhouse_*.json .. 18_berkshire_*.json")
    parser.add_argument("--quarter", default="26Q3", help="Quarter tag suffix in filenames, e.g. 26Q3 -> 26q3")
    parser.add_argument("--workspace-dir", default=None, help="Optional workspace dir for overlay state")
    parser.add_argument("--lookup-new", action="store_true", help="Query Yahoo Finance for any unverified tickers")
    parser.add_argument("--report-file", default=None, help="Optional assembled 5-section markdown report to validate")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    q_slug = args.quarter.lower()
    expected_ym = _expected_period_prefix(args.quarter)

    spec = importlib.util.spec_from_file_location("skill13f", str(SKILL_PY))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {SKILL_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._init_state(args.workspace_dir)

    verified = json.loads(VERIFIED_LEDGER.read_text(encoding="utf-8")) if VERIFIED_LEDGER.exists() else {}
    if mod._STATE_DIR is not None:
        ws_verified_path = mod._STATE_DIR / "verified_tickers.json"
        if ws_verified_path.is_file():
            ws_verified = json.loads(ws_verified_path.read_text(encoding="utf-8"))
            if isinstance(ws_verified, dict):
                verified.update(ws_verified)

    errors: list[str] = []
    warnings: list[str] = []
    if expected_ym is None:
        errors.append(f"[INVALID QUARTER] Cannot parse --quarter '{args.quarter}'")
    unverified_seen: dict[str, dict] = {}
    rendered_names_by_cat: dict[str, set[str]] = defaultdict(set)

    explicit_canonical_names = {str(v) for k, v in mod._MERGE_RULES.items() if k != "_comment"}

    inst_map_lower = {
        str(k).lower(): str(v).strip()
        for k, v in (getattr(mod, "_INSTITUTION_MAP", {}) or {}).items()
        if isinstance(v, str)
    }

    def _resolve_merge_cik(ref: str) -> str:
        raw = str(ref or "").strip()
        if not raw:
            return ""
        merge_fn = getattr(mod, "_resolve_merge_ref_cik", None)
        if callable(merge_fn):
            resolved = merge_fn(raw)
            if resolved:
                return resolved
        mapped = inst_map_lower.get(raw.lower())
        if mapped:
            raw = mapped
        m = re.fullmatch(r"(?:CIK\s*)?0*(\d+)", raw, flags=re.IGNORECASE)
        if m:
            return (m.group(1).lstrip("0") or "0").zfill(10)
        return raw.lower()

    hillhouse_cik = _resolve_merge_cik("hillhouse")
    expected_cik_by_slug = {
        str(g["slug"]): _resolve_merge_cik(g["cik"])
        for g in mod._batch_groups()
        if g.get("slug") and g.get("cik")
    }
    group_by_slug = {
        str(g["slug"]): g for g in mod._batch_groups() if g.get("slug")
    }
    default_merges = getattr(mod, "_DEFAULT_MERGES", {}) or {}
    hillhouse_raw_merges = (
        default_merges.get(hillhouse_cik.lstrip("0") or "0")
        or default_merges.get(hillhouse_cik)
        or []
    )
    expected_hillhouse_merges = [
        _resolve_merge_cik(c) for c in hillhouse_raw_merges if str(c).strip()
    ]
    exp_short_q = exp_prev_short_q = ""
    if expected_ym is not None:
        exp_short_q, _exp_full_q, exp_prev_short_q, _ym = mod._parse_quarter_tag(args.quarter)

    periods_seen = set()
    for idx, slug in EXPECTED_GROUPS:
        fpath = data_dir / f"{idx:02d}_{slug}_{q_slug}.json"
        if not fpath.exists():
            errors.append(f"[MISSING FILE] {fpath}")
            continue
        d = json.loads(fpath.read_text(encoding="utf-8"))
        if not d.get("ok"):
            errors.append(f"[FAILED GROUP] {slug}: {d.get('error')}")
            merged_with = d.get("merged_with")
            if isinstance(merged_with, list):
                for mw in merged_with:
                    if isinstance(mw, dict) and int(mw.get("rows") or 0) <= 0:
                        errors.append(
                            f"[INCOMPLETE MERGE] {slug}: merged_with entry {mw.get('institution')} has rows={mw.get('rows')}"
                        )
            continue
        base_period = str(d.get("base_period") or "").strip()
        periods_seen.add(base_period)
        if expected_ym and not base_period.startswith(expected_ym):
            errors.append(
                f"[PERIOD MISMATCH] {slug}: base_period='{base_period}' does not match --quarter {args.quarter} (expected {expected_ym}-*)"
            )
        prev_period = str(d.get("previous_period") or "").strip()
        if not prev_period:
            errors.append(
                f"[PREVIOUS PERIOD MISMATCH] {slug}: missing required 'previous_period'"
            )
        elif exp_prev_short_q:
            actual_prev_q = mod._quarter_label(prev_period)
            if actual_prev_q != exp_prev_short_q:
                errors.append(
                    f"[PREVIOUS PERIOD MISMATCH] {slug}: previous_period='{prev_period}' ({actual_prev_q}) "
                    f"!= expected '{exp_prev_short_q}'"
                )

        merged_with = d.get("merged_with")
        valid_merge_ciks: list[str] = []
        if isinstance(merged_with, list):
            for mw in merged_with:
                if not isinstance(mw, dict) or int(mw.get("rows") or 0) <= 0:
                    errors.append(
                        f"[INCOMPLETE MERGE] {slug}: merged_with entry {mw.get('institution') if isinstance(mw, dict) else mw} "
                        f"has rows={mw.get('rows') if isinstance(mw, dict) else None}"
                    )
                else:
                    mcik = _resolve_merge_cik(mw.get("institution", ""))
                    if mcik:
                        valid_merge_ciks.append(mcik)
                    mw_base = str(mw.get("base_period") or "").strip()
                    if not mw_base or mw_base != base_period:
                        errors.append(
                            f"[HILLHOUSE MERGE] {slug}: merged entity base_period='{mw_base}' != '{base_period}'"
                        )
                    mw_prev = str(mw.get("previous_period") or "").strip()
                    if not mw_prev or (prev_period and mw_prev != prev_period):
                        errors.append(
                            f"[HILLHOUSE MERGE] {slug}: merged entity previous_period='{mw_prev}' != '{prev_period}'"
                        )
        raw_cik_str = str(d.get("cik") or "").strip()
        raw_inst_str = str(d.get("institution") or "").strip()
        cik_val = _resolve_merge_cik(raw_cik_str) if raw_cik_str else ""
        inst_is_explicit = raw_inst_str.lower() in inst_map_lower or bool(
            re.fullmatch(r"(?:CIK\s*)?0*\d+", raw_inst_str, flags=re.IGNORECASE)
        )
        inst_cik = _resolve_merge_cik(raw_inst_str) if inst_is_explicit else ""
        primary_cik = cik_val or inst_cik
        exp_cik = expected_cik_by_slug.get(slug, "")
        grp_meta = group_by_slug.get(slug, {})
        allowed_primary = {
            slug.lower(),
            slug.replace("_", " ").lower(),
            *(str(n).strip().lower() for n in (grp_meta.get("primary_names") or []) if str(n).strip()),
            *([] if grp_meta.get("merge_with") else [str(grp_meta.get("display_name") or "").strip().lower()]),
        }
        bad_primary_name = bool(raw_inst_str and not inst_is_explicit and raw_inst_str.lower() not in allowed_primary)

        if slug == "hillhouse":
            if not hillhouse_cik or primary_cik != hillhouse_cik or (inst_cik and inst_cik != hillhouse_cik) or bad_primary_name:
                errors.append(
                    f"[HILLHOUSE MERGE] Hillhouse primary CIK/name mismatch: expected '{hillhouse_cik}', "
                    f"got cik='{d.get('cik')}' institution='{d.get('institution')}'"
                )
            if not isinstance(merged_with, list) or not merged_with:
                errors.append("[HILLHOUSE MERGE] Hillhouse missing Evergreen merged_with metadata")
            elif len(valid_merge_ciks) != len(set(valid_merge_ciks)):
                errors.append(
                    f"[HILLHOUSE MERGE] Duplicate merged_with CIK entries in Hillhouse: {valid_merge_ciks}"
                )
            elif expected_hillhouse_merges and sorted(valid_merge_ciks) != sorted(expected_hillhouse_merges):
                errors.append(
                    f"[HILLHOUSE MERGE] Hillhouse merged_with must be exactly {sorted(expected_hillhouse_merges)} "
                    f"(got {valid_merge_ciks})"
                )
        else:
            if (exp_cik and (primary_cik != exp_cik or (inst_cik and inst_cik != exp_cik))) or bad_primary_name:
                errors.append(
                    f"[INSTITUTION CIK MISMATCH] {slug}: expected CIK '{exp_cik}', "
                    f"got cik='{d.get('cik')}' institution='{d.get('institution')}'"
                )
            if merged_with:
                errors.append(
                    f"[UNEXPECTED MERGE] {slug}: merged_with must be empty (got {merged_with})"
                )

        # Check rendered category bullets for bad names & split NEW+CLOSED anomalies
        cats = d.get("categories")
        formatted_report = d.get("formatted_report")
        if not isinstance(cats, dict) or not cats or not isinstance(formatted_report, str) or not formatted_report.strip():
            errors.append(f"[MISSING REPORT] {slug}: missing non-empty 'categories' or 'formatted_report'")
            cats = cats if isinstance(cats, dict) else {}

        fund_names_actions: dict[str, list[str]] = defaultdict(list)
        fund_name_to_cats: dict[str, set[str]] = defaultdict(set)
        for cat_name, bullets in cats.items():
            for b in bullets:
                nm = extract_name_from_line(b)
                if not nm:
                    warnings.append(f"[UNPARSED LINE] {slug} ({cat_name}): {b}")
                    continue
                base_nm = re.sub(r"（CUSIP\s+[A-Za-z0-9]+）$", "", nm).strip()
                base_nm = base_nm[:-3].strip() if base_nm.endswith(" CB") else base_nm
                cat_key = nm[:-3].strip() if nm.endswith(" CB") else nm
                rendered_names_by_cat[cat_key].add(cat_name)
                fund_name_to_cats[nm].add(cat_name)
                fund_name_to_cats[cat_key].add(cat_name)
                fund_names_actions[nm].append(b)

                tokens = [t.strip("().,") for t in base_nm.split() if t.strip("().,")]
                if not tokens:
                    continue
                last_low = tokens[-1].lower()
                if last_low in BAD_END_TOKENS and not (base_nm in explicit_canonical_names and last_low in {"corp", "inc", "ltd"}):
                    errors.append(f"[BAD NAME SUFFIX] {slug}: '{nm}' ends with '{tokens[-1]}' ({b})")
                for tok in tokens:
                    if tok.lower() in BAD_ANYWHERE_TOKENS:
                        errors.append(f"[ABBREVIATED NAME] {slug}: '{nm}' contains '{tok}' ({b})")
                if len(base_nm) <= 2 and base_nm not in ALLOWED_SHORT_NAMES:
                    errors.append(f"[STUB NAME] {slug}: '{nm}' is too short/ambiguous ({b})")
                if base_nm == "D R":
                    errors.append(f"[TRUNCATED INITIALS] {slug}: '{nm}' ({b})")

        row_cusip_tmap = mod._resolve_cusip_tickers(d.get("rows", []))
        empty_cusips_by_name: dict[str, set[tuple[str, str]]] = defaultdict(set)
        titled_keys_by_name: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        bucket_keys_by_dname: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        active_cusip_counts: dict[str, int] = defaultdict(int)
        for r in d.get("rows", []):
            r_issuer = str(r.get("Issuer") or "").strip()
            r_cusip = str(r.get("Cusip") or "").strip().upper()
            r_raw_t = r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker")
            r_ticker = mod._clean_ticker(r_raw_t)
            r_val = mod._safe_float(r.get("Value"))
            r_pval = mod._safe_float(r.get("PrevValue"))
            if (r_val == 0.0 and r_pval == 0.0) or mod._is_biotech(r_ticker, r_issuer):
                continue
            if r_cusip:
                active_cusip_counts[r_cusip] += 1
            ident = mod._row_identity_keys(r, row_cusip_tmap)
            r_dname = ident["display_name"]
            bucket_keys_by_dname[r_dname].add(ident["bucket_key"])
            if not ident["resolved_t"] and ident["cusip"]:
                empty_cusips_by_name[r_dname].add((ident["cusip"], ident["sec_kind"]))
            else:
                titled_keys_by_name[r_dname].add(ident["bucket_key"])

        for dname, bk_set in bucket_keys_by_dname.items():
            if len(bk_set) > 1:
                errors.append(
                    f"[DUPLICATE DISPLAY NAME] {slug}: distinct positions {sorted(bk_set)} share display name '{dname}'"
                )

        for c_norm, c_cnt in active_cusip_counts.items():
            if c_cnt > 1:
                errors.append(
                    f"[UNMERGED CUSIP IN ROWS] {slug}: CUSIP '{c_norm}' appears {c_cnt} times without merging"
                )

        for nm, blist in fund_names_actions.items():
            expected_count = len(empty_cusips_by_name.get(nm, set())) + len(titled_keys_by_name.get(nm, set()))
            if len(blist) > max(1, expected_count):
                errors.append(f"[DUPLICATE/UNMERGED ENTITY IN SAME FUND] {slug}: '{nm}' appears {len(blist)} times: {blist}")

        # Check raw rows for unverified tickers, category drift, and empty-ticker rows
        issuer_cusips: dict[str, set[str]] = defaultdict(set)
        for r in d.get("rows", []):
            issuer = str(r.get("Issuer") or "").strip()
            cusip = str(r.get("Cusip") or "").strip()
            title = str(r.get("TitleOfClass") or r.get("Class") or r.get("title_of_class") or "").strip()
            raw_t = r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker")
            ticker = mod._clean_ticker(raw_t)
            val = mod._safe_float(r.get("Value"))
            pval = mod._safe_float(r.get("PrevValue"))
            if val == 0.0 and pval == 0.0:
                continue
            if mod._is_biotech(ticker, issuer):
                continue

            if cusip:
                issuer_cusips[issuer].add(cusip)

            if not ticker:
                warnings.append(
                    f"[EMPTY TICKER ROW] {slug}: Issuer='{issuer}' Cusip='{cusip}' Val=${val/1e6:.2f}M PrevVal=${pval/1e6:.2f}M"
                )
                continue

            alias_target = mod._TICKER_ALIASES.get(ticker, "") if not ticker.startswith("_") else ""
            resolved_ticker = mod._clean_ticker(alias_target) or ticker
            lookup_ticker = resolved_ticker if resolved_ticker in verified else ticker

            if lookup_ticker not in verified:
                if resolved_ticker not in unverified_seen:
                    unverified_seen[resolved_ticker] = {
                        "issuer": issuer,
                        "funds": set(),
                        "max_val_m": 0.0,
                        "current_cat": mod._classify(ticker, issuer, cusip=cusip, auto_learn=False),
                        "current_name": (
                            mod._canonical_name_for_ticker(resolved_ticker, cusip_norm=cusip, issuer=issuer)
                            or mod._canonical_name_for_ticker(ticker, cusip_norm=cusip, issuer=issuer)
                            or mod._short_name(issuer, ticker, cusip=cusip)
                        ),
                    }
                unverified_seen[resolved_ticker]["funds"].add(slug)
                unverified_seen[resolved_ticker]["max_val_m"] = max(unverified_seen[resolved_ticker]["max_val_m"], val / 1e6, pval / 1e6)
            elif not mod._ledger_entry_matches(lookup_ticker, cusip, issuer):
                warnings.append(
                    f"[TICKER REUSE / LEDGER MISMATCH] {slug}: {ticker} Cusip='{cusip}' Issuer='{issuer}' "
                    f"does not match verified ledger for {lookup_ticker}; using fallback name/classification"
                )
            else:
                expected_cat = verified[lookup_ticker]["category"]
                actual_cls = mod._classify(ticker, issuer, cusip=cusip, auto_learn=False)
                if actual_cls != expected_cat:
                    errors.append(
                        f"[CATEGORY DRIFT BY TICKER] {slug}: {ticker} (resolved={lookup_ticker}) "
                        f"classified='{actual_cls}' vs verified='{expected_cat}'"
                    )
                sec_kind = mod._detect_security_kind(ticker, issuer, cusip, title)
                base_rendered = (
                    mod._canonical_name_for_ticker(ticker, cusip_norm=cusip, issuer=issuer)
                    or mod._canonical_name_for_ticker(resolved_ticker, cusip_norm=cusip, issuer=issuer)
                    or (verified.get(lookup_ticker) or {}).get("canonical_name")
                    or mod._short_name(issuer, ticker, cusip=cusip)
                )
                rendered_key = mod._apply_security_kind_to_merge_key(base_rendered, sec_kind)
                canon_key = mod._apply_security_kind_to_merge_key(verified[lookup_ticker]["canonical_name"], sec_kind)
                matched_cats: set[str] = set()
                for cand_nm in (rendered_key, base_rendered, canon_key, verified[lookup_ticker]["canonical_name"]):
                    matched_cats.update(fund_name_to_cats.get(cand_nm, ()))
                if not matched_cats:
                    errors.append(
                        f"[MISSING VERIFIED RENDER] {slug}: {ticker} (resolved={lookup_ticker}, expected='{canon_key}') "
                        f"not found in rendered categories"
                    )
                elif matched_cats != {expected_cat}:
                    errors.append(
                        f"[CATEGORY MISMATCH BY TICKER] {slug}: {ticker} (resolved={lookup_ticker}, name='{rendered_key}') "
                        f"rendered={sorted(matched_cats)} vs verified='{expected_cat}'"
                    )

        for iss, cset in issuer_cusips.items():
            lower_map = defaultdict(list)
            for c in cset:
                lower_map[c.upper()].append(c)
            for cu, variants in lower_map.items():
                if len(variants) > 1:
                    warnings.append(f"[CUSIP CASE SPLIT] {slug}: Issuer='{iss}' has case-variant CUSIPs {variants} — verify merged cleanly")

    if len(periods_seen) > 1:
        errors.append(f"[PERIOD MISMATCH] Multiple base_periods across groups: {sorted(periods_seen)}")

    # Check if any rendered company name appeared in DIFFERENT categories across funds
    for nm, cset in rendered_names_by_cat.items():
        if len(cset) > 1:
            errors.append(f"[CROSS-FUND CATEGORY CONFLICT] '{nm}' appeared in multiple categories: {sorted(cset)}")

    # Check rendered category against verified_tickers.json
    by_cname = defaultdict(set)
    for t, info in verified.items():
        by_cname[info["canonical_name"]].add((t, info["category"]))
    for nm, cset in rendered_names_by_cat.items():
        if nm in by_cname:
            expected_cats = {cat for _, cat in by_cname[nm]}
            if cset != expected_cats:
                errors.append(
                    f"[CATEGORY DRIFT VS VERIFIED LEDGER] '{nm}': rendered={sorted(cset)} vs verified={sorted(expected_cats)}"
                )

    if unverified_seen:
        errors.append(f"[UNVERIFIED NEW TICKERS] {len(unverified_seen)} ticker(s) not yet in verified_tickers.json:")
        for t, info in sorted(unverified_seen.items()):
            yf_str = ""
            if args.lookup_new:
                yf_str = f" | YF={lookup_yf(t)}"
            errors.append(
                f"  - {t}: issuer='{info['issuer']}' cur_cat='{info['current_cat']}' "
                f"cur_name='{info['current_name']}' max=${info['max_val_m']:.1f}M funds={sorted(info['funds'])}{yf_str}"
            )

    if args.report_file:
        rpt_path = Path(args.report_file)
        if not rpt_path.is_file():
            errors.append(f"[MISSING REPORT FILE] {rpt_path}")
        else:
            rpt_text = rpt_path.read_text(encoding="utf-8")
            required_headings = [
                "# 13F Tracker_",
                "## 核心结论与跨机构动向",
                "## 持仓变化中的新线索",
                "## China Book 机构（8 组 / 9 申报主体）",
                "## US Growth 机构（9 组）",
                "## Value 机构（1 组）",
            ]
            for h in required_headings:
                if h not in rpt_text:
                    errors.append(f"[REPORT STRUCTURE] Missing required heading '{h}'")
            for idx, _slug in EXPECTED_GROUPS:
                if f"### {idx}. " not in rpt_text:
                    errors.append(f"[REPORT STRUCTURE] Missing group heading '### {idx}. '")
            if re.search(r"\\[_.\+\-&()]", rpt_text):
                errors.append("[MARKDOWN ESCAPE] Found escaped punctuation (\\_, \\., \\+, etc.) in report")
            if "10000万美元" in rpt_text:
                errors.append("[UNIT FORMAT] Found '10000万美元' instead of '1.0亿美元'")
            if expected_ym is not None:
                exp_short_q, exp_full_q, exp_prev_short_q, _ym = mod._parse_quarter_tag(args.quarter)
                title_matches = re.findall(r"^#\s*13F\s+Tracker_(\d{2,4}[Qq][1-4])\b", rpt_text, flags=re.MULTILINE)
                if not title_matches:
                    errors.append(f"[REPORT QUARTER MISMATCH] Missing '# 13F Tracker_{exp_full_q}' title")
                else:
                    for tq in title_matches:
                        _, t_full_q, _, _ = mod._parse_quarter_tag(tq)
                        if t_full_q != exp_full_q:
                            errors.append(
                                f"[REPORT QUARTER MISMATCH] Title quarter '{tq}' ({t_full_q}) != expected '{exp_full_q}'"
                            )
                detail_pairs = re.findall(
                    r"^###\s+\d+\.\s+.+?\s+(\d{2,4}[Qq][1-4])\s+vs\s+(\d{2,4}[Qq][1-4])\b",
                    rpt_text,
                    flags=re.MULTILINE,
                )
                if len(detail_pairs) != len(EXPECTED_GROUPS):
                    errors.append(
                        f"[REPORT QUARTER MISMATCH] Expected {len(EXPECTED_GROUPS)} group quarter headings "
                        f"'{exp_short_q} vs {exp_prev_short_q}', found {len(detail_pairs)}"
                    )
                for cur_q, prev_q in detail_pairs:
                    if cur_q.upper() != exp_short_q or prev_q.upper() != exp_prev_short_q:
                        errors.append(
                            f"[REPORT QUARTER MISMATCH] Detail heading '{cur_q} vs {prev_q}' "
                            f"!= expected '{exp_short_q} vs {exp_prev_short_q}'"
                        )

    print(f"=== 13F QUALITY GATE SUMMARY ({data_dir}) ===")
    print(f"Base period(s): {sorted(periods_seen)}")
    print(f"Errors: {len(errors)} | Warnings: {len(warnings)}")
    for w in warnings:
        print("  WARN:", w)
    for e in errors:
        print("  ERR :", e)

    if errors:
        print("RESULT: FAIL (fix overlays and rerun before generating report)")
        return 1
    print("RESULT: PASS (all 18 groups verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
