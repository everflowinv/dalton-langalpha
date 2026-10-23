#!/usr/bin/env python3
"""Build and validate the canonical Dalton coverage roster (`config/dalton/coverage.yaml`).

Reads four OpenClaw sources in read-only mode:
1. `~/.openclaw/workspace/skills/coverage-update-richard/scripts/screen_movers.py` (`TICKERS`, `EXTRA_PEERS`)
2. `~/.openclaw/workspace/skills/coverage-update-richard/scripts/coverage_update_richard_cli.py` (`CORE`)
3. `~/.openclaw/workspace/scripts/richard_earnings_tracker_cli.py` (`COMPANIES`)
4. `~/.openclaw/workspace/state/earnings_scan/watchlist.json` (`confirmed`, `pending`, `reported`)

Python source files are parsed with `ast` (`ast.parse` + `ast.literal_eval`) and NEVER executed.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_OUT = REPO_ROOT / "config" / "dalton" / "coverage.yaml"
DEFAULT_OVERRIDES = REPO_ROOT / "config" / "dalton" / "coverage.overrides.yaml"

AS_OF_DATE = "2026-10-09"
EXPECTED_CORE = 27
EXPECTED_WATCH = 78
EXPECTED_TOTAL = 105

LEGACY_IMPORTED_TICKERS = {"META", "AMZN", "GOOGL", "MSFT"}

# Explicit canonical ticker mapping for known cross-source symbol discrepancies.
TRACKER_TO_CANONICAL_TICKER = {
    "MPNGY": "3690.HK",
    "601100.SH": "601100.SS",
    "BRK.B": "BRK-B",
}

SPCX_NOTE = "非上市或特殊标的，数据可得性有限；没有 filing 和行情时按'本轮未取得'处理"


def _ocw_root() -> Path:
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return Path("~/.openclaw/workspace").expanduser().resolve()


def _source_paths(ocw: Path) -> dict[str, tuple[str, Path]]:
    return {
        "screen_movers": (
            "~/.openclaw/workspace/skills/coverage-update-richard/scripts/screen_movers.py",
            ocw / "skills" / "coverage-update-richard" / "scripts" / "screen_movers.py",
        ),
        "coverage_cli": (
            "~/.openclaw/workspace/skills/coverage-update-richard/scripts/coverage_update_richard_cli.py",
            ocw / "skills" / "coverage-update-richard" / "scripts" / "coverage_update_richard_cli.py",
        ),
        "earnings_tracker": (
            "~/.openclaw/workspace/scripts/richard_earnings_tracker_cli.py",
            ocw / "scripts" / "richard_earnings_tracker_cli.py",
        ),
        "watchlist": (
            "~/.openclaw/workspace/state/earnings_scan/watchlist.json",
            ocw / "state" / "earnings_scan" / "watchlist.json",
        ),
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_ast_literals(py_path: Path, var_names: set[str]) -> dict[str, Any]:
    """Extract top-level literal assignments from a Python file using `ast` without executing it."""
    source_text = py_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(py_path))
    extracted: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in var_names:
                    extracted[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in var_names and node.value is not None:
                extracted[node.target.id] = ast.literal_eval(node.value)
    missing = var_names - set(extracted.keys())
    if missing:
        raise ValueError(f"Missing AST literal(s) {sorted(missing)} in {py_path}")
    return extracted


def _infer_market(ticker: str) -> str:
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SS", ".SZ")):
        return "CN"
    return "US"


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def build_coverage_data(ocw: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse all 4 sources, reconcile the 5 known discrepancies, and return (coverage_dict, discrepancy_records)."""
    ocw_dir = ocw or _ocw_root()
    paths = _source_paths(ocw_dir)

    sm_vars = _extract_ast_literals(paths["screen_movers"][1], {"TICKERS", "EXTRA_PEERS"})
    tickers_map: dict[str, tuple[str, str, str, str]] = sm_vars["TICKERS"]
    extra_peers: dict[str, list[str]] = sm_vars["EXTRA_PEERS"]

    cli_vars = _extract_ast_literals(paths["coverage_cli"][1], {"CORE"})
    core_list_raw: list[dict[str, Any]] = cli_vars["CORE"]

    et_vars = _extract_ast_literals(paths["earnings_tracker"][1], {"COMPANIES"})
    tracker_companies: list[dict[str, Any]] = et_vars["COMPANIES"]

    watchlist_raw = json.loads(paths["watchlist"][1].read_text(encoding="utf-8"))

    discrepancies: list[dict[str, Any]] = []

    # 1. Index CORE search_names by name key
    core_search_by_name: dict[str, list[str]] = {}
    for entry in core_list_raw:
        core_search_by_name[entry["name"]] = list(entry.get("search_names", []))

    # 2. Index peer groups from TICKERS (all 324 rows, including peer-only rows with tier=="")
    peers_by_group: dict[str, list[str]] = {}
    covered_rows: list[tuple[str, str, str, str, str]] = []
    covered_by_ticker: dict[str, tuple[str, str, str, str]] = {}

    for name_key, val in tickers_map.items():
        ticker, peer_group, sheet_section, tier = val
        peers_by_group.setdefault(peer_group, []).append(ticker)
        if tier in ("Core", "Watch"):
            covered_rows.append((name_key, ticker, peer_group, sheet_section, tier))
            covered_by_ticker[ticker] = (name_key, peer_group, sheet_section, tier)

    # 3. Reconcile tracker COMPANIES against TICKERS covered set
    tracker_by_canonical: dict[str, dict[str, Any]] = {}
    for comp in tracker_companies:
        raw_ticker = comp["ticker"]
        canonical = TRACKER_TO_CANONICAL_TICKER.get(raw_ticker, raw_ticker)
        if raw_ticker != canonical:
            discrepancies.append(
                {
                    "id": f"ticker_alias:{canonical}",
                    "known": True,
                    "category": "symbol_mismatch",
                    "canonical_ticker": canonical,
                    "source_ticker": raw_ticker,
                    "source": "richard_earnings_tracker_cli.py & watchlist.json",
                    "resolution": (
                        f"Canonical ticker set to '{canonical}' (from screen_movers.py); "
                        f"'{raw_ticker}' recorded in aliases."
                    ),
                }
            )
        if canonical not in covered_by_ticker:
            discrepancies.append(
                {
                    "id": f"unmatched_tracker:{canonical}",
                    "known": False,
                    "category": "unmatched_company",
                    "canonical_ticker": canonical,
                    "source_ticker": raw_ticker,
                    "source": "richard_earnings_tracker_cli.py",
                    "resolution": "UNRESOLVED: present in tracker CLI but missing from TICKERS Core/Watch",
                }
            )
        else:
            expected_tier = "Core" if comp["group"] == "A" else "Watch"
            actual_tier = covered_by_ticker[canonical][3]
            if expected_tier != actual_tier:
                discrepancies.append(
                    {
                        "id": f"tier_mismatch:{canonical}",
                        "known": False,
                        "category": "tier_mismatch",
                        "canonical_ticker": canonical,
                        "source_ticker": raw_ticker,
                        "source": "richard_earnings_tracker_cli.py",
                        "resolution": f"UNRESOLVED: group {comp['group']} ({expected_tier}) != TICKERS tier {actual_tier}",
                    }
                )
        tracker_by_canonical[canonical] = comp

    # 4. Inspect watchlist.json (confirmed, pending, reported) for IR URLs and duplicates in reported
    reported_counts: dict[str, list[str]] = {}
    for item in watchlist_raw.get("reported", []):
        raw_t = item["ticker"]
        reported_counts.setdefault(raw_t, []).append(item.get("date", ""))

    duplicate_reported = {t: dates for t, dates in reported_counts.items() if len(dates) > 1}
    if duplicate_reported:
        discrepancies.append(
            {
                "id": "watchlist_reported_duplicates",
                "known": True,
                "category": "watchlist_duplicate",
                "canonical_ticker": ",".join(sorted(duplicate_reported.keys())),
                "source_ticker": ",".join(
                    f"{t}({len(dates)}x: {'/'.join(dates)})"
                    for t, dates in sorted(duplicate_reported.items())
                ),
                "source": "watchlist.json (reported)",
                "resolution": "Deduplicated by keeping latest entry with IR source URL; does not affect tier.",
            }
        )

    ir_url_by_canonical: dict[str, str] = {}
    # Priority: confirmed -> pending -> reported (latest date first)
    for section in ("confirmed", "pending", "reported"):
        items = list(watchlist_raw.get(section, []))
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        for item in items:
            raw_t = item.get("ticker", "")
            canonical = TRACKER_TO_CANONICAL_TICKER.get(raw_t, raw_t)
            src_url = (item.get("source") or "").strip()
            if src_url and canonical not in ir_url_by_canonical:
                ir_url_by_canonical[canonical] = src_url

    # 5. Check SPCX special private/non-standard ticker
    if "SPCX" in covered_by_ticker:
        discrepancies.append(
            {
                "id": "special_ticker:SPCX",
                "known": True,
                "category": "non_public_or_special_security",
                "canonical_ticker": "SPCX",
                "source_ticker": "SPCX",
                "source": "screen_movers.py (US Mega Tech / Core)",
                "resolution": f"Kept in Core per owner decision; note set to: {SPCX_NOTE}",
            }
        )

    # 6. Build canonical companies list (preserving TICKERS order: Core and Watch)
    companies_out: list[dict[str, Any]] = []
    for name_key, ticker, peer_group, sheet_section, tier_raw in covered_rows:
        tier = tier_raw.lower()
        market = _infer_market(ticker)
        group_peers = [p for p in peers_by_group.get(peer_group, []) if p != ticker]
        group_etfs = list(extra_peers.get(peer_group, []))

        tracker_entry = tracker_by_canonical.get(ticker, {})
        tracker_ticker = tracker_entry.get("ticker", ticker)
        tracker_name = tracker_entry.get("name", name_key)
        tracker_group = tracker_entry.get("group")
        tracker_note = (tracker_entry.get("note") or "").strip()

        # Construct display name & search_names
        display_name = tracker_name if (name_key.isascii() and len(tracker_name) > len(name_key)) else name_key
        raw_search_names: list[str] = []
        if name_key in core_search_by_name:
            raw_search_names.extend(core_search_by_name[name_key])
        raw_search_names.extend([name_key, tracker_name, ticker])
        if tracker_ticker != ticker:
            raw_search_names.append(tracker_ticker)
        search_names = _dedupe_preserve_order(raw_search_names)

        adr_alias: str | None = "MPNGY" if ticker == "3690.HK" else None

        if ticker in LEGACY_IMPORTED_TICKERS:
            ws_status = "active"
            cov_stage = "legacy_imported"
        elif market == "US":
            ws_status = "planned"
            cov_stage = "none"
        else:
            ws_status = "none"
            cov_stage = "none"

        if ticker == "SPCX":
            note_val: str | None = SPCX_NOTE
        else:
            note_val = tracker_note if tracker_note else None

        companies_out.append(
            {
                "ticker": ticker,
                "name": display_name,
                "tier": tier,
                "market": market,
                "peer_group": peer_group,
                "sheet_section": sheet_section,
                "peers": group_peers,
                "etfs": group_etfs,
                "aliases": {
                    "search_names": search_names,
                    "adr": adr_alias,
                    "earnings_tracker": tracker_ticker,
                },
                "earnings": {
                    "group": tracker_group,
                    "ir_url": ir_url_by_canonical.get(ticker),
                },
                "workspace": {
                    "name": ticker,
                    "status": ws_status,
                },
                "coverage_stage": cov_stage,
                "note": note_val,
            }
        )

    # 7. Apply optional overrides if config/dalton/coverage.overrides.yaml exists
    if DEFAULT_OVERRIDES.is_file():
        overrides_doc = yaml.safe_load(DEFAULT_OVERRIDES.read_text(encoding="utf-8")) or {}
        company_overrides = overrides_doc.get("companies") or {}
        if isinstance(company_overrides, dict):
            for comp_item in companies_out:
                t = comp_item["ticker"]
                if t in company_overrides and isinstance(company_overrides[t], dict):
                    for k, v in company_overrides[t].items():
                        if isinstance(v, dict) and isinstance(comp_item.get(k), dict):
                            comp_item[k].update(v)
                        else:
                            comp_item[k] = v

    core_count = sum(1 for c in companies_out if c["tier"] == "core")
    watch_count = sum(1 for c in companies_out if c["tier"] == "watch")

    sources_meta = [
        {"path": display_path, "sha256": _sha256_file(real_path)}
        for _, (display_path, real_path) in paths.items()
    ]

    coverage_doc: dict[str, Any] = {
        "schema_version": 1,
        "as_of": AS_OF_DATE,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources_meta,
        "tiers": {
            "core": {
                "meaning": "每周五源复盘、每日跟踪、财报前瞻和点评全链",
                "count": core_count,
            },
            "watch": {
                "meaning": "只看异动；财报只做快讯",
                "count": watch_count,
            },
        },
        "companies": companies_out,
    }

    return coverage_doc, discrepancies


def _strip_generated_at(doc: dict[str, Any]) -> dict[str, Any]:
    copy = dict(doc)
    copy.pop("generated_at", None)
    return copy


def _dump_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(
        doc,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def write_coverage_yaml(out_path: Path, doc: dict[str, Any]) -> bool:
    """Write `coverage.yaml` idempotently (preserving `generated_at` if nothing else changed)."""
    if out_path.is_file():
        try:
            existing = yaml.safe_load(out_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and _strip_generated_at(existing) == _strip_generated_at(doc):
                doc["generated_at"] = existing.get("generated_at", doc["generated_at"])
                rendered = _dump_yaml(doc)
                if out_path.read_text(encoding="utf-8") == rendered:
                    return False
        except Exception:
            pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_dump_yaml(doc), encoding="utf-8")
    return True


def sync_to_user_coverage_db(user_id: str, yaml_path: Path) -> int:
    """Seed the specified user's coverage roster in PostgreSQL from `yaml_path`."""
    from scripts.dalton.seed_user_coverage import run_seed

    return asyncio.run(run_seed(user_id=user_id, yaml_path=yaml_path, apply=True))


def print_report(doc: dict[str, Any], discrepancies: list[dict[str, Any]]) -> None:
    core_count = doc["tiers"]["core"]["count"]
    watch_count = doc["tiers"]["watch"]["count"]
    total_count = len(doc["companies"])
    print(f"Coverage summary: Core={core_count}, Watch={watch_count}, Total={total_count} (as_of={doc['as_of']})")
    print("\nSource hashes:")
    for src in doc["sources"]:
        print(f"  - {src['path']}: {src['sha256'][:16]}...")
    print(f"\nDiscrepancies reconciled ({len(discrepancies)}):")
    for idx, d in enumerate(discrepancies, 1):
        status = "KNOWN" if d["known"] else "UNREGISTERED"
        print(
            f"  {idx}. [{status}] {d['category']} | canonical={d['canonical_ticker']} "
            f"| source={d['source_ticker']} ({d['source']}) -> {d['resolution']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and check Dalton canonical coverage.yaml")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output YAML path")
    parser.add_argument("--check", action="store_true", help="Validate counts, discrepancies, and existing YAML")
    parser.add_argument("--sync", action="store_true", help="Seed coverage.yaml into PostgreSQL for --user")
    parser.add_argument(
        "--user",
        default=os.environ.get("DALTON_USER_ID"),
        help="Target user_id when --sync is specified",
    )
    parser.add_argument("--api", default="http://localhost:8000", help="Deprecated (retained for CLI compatibility)")
    args = parser.parse_args(argv)

    doc, discrepancies = build_coverage_data()
    core_count = doc["tiers"]["core"]["count"]
    watch_count = doc["tiers"]["watch"]["count"]
    total_count = len(doc["companies"])
    unregistered = [d for d in discrepancies if not d["known"]]

    if args.check:
        print_report(doc, discrepancies)
        errors: list[str] = []
        if core_count != EXPECTED_CORE:
            errors.append(f"Expected Core={EXPECTED_CORE}, got {core_count}")
        if watch_count != EXPECTED_WATCH:
            errors.append(f"Expected Watch={EXPECTED_WATCH}, got {watch_count}")
        if total_count != EXPECTED_TOTAL:
            errors.append(f"Expected Total={EXPECTED_TOTAL}, got {total_count}")
        if unregistered:
            errors.append(f"Found {len(unregistered)} unregistered discrepancies: {unregistered}")
        if len(discrepancies) != 5:
            errors.append(f"Expected exactly 5 registered discrepancies, got {len(discrepancies)}")
        if not args.out.is_file():
            errors.append(f"Canonical YAML missing at {args.out}")
        else:
            on_disk = yaml.safe_load(args.out.read_text(encoding="utf-8"))
            if _strip_generated_at(on_disk) != _strip_generated_at(doc):
                errors.append(f"{args.out} is out of sync with sources; re-run build_coverage.py")
        if errors:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print("\nCHECK PASSED: Core=27, Watch=78, Total=105, all 5 known discrepancies accounted for.")
        return 0

    wrote = write_coverage_yaml(args.out, doc)
    action = "Wrote" if wrote else "Unchanged"
    print(f"[{action}] {args.out}")
    print_report(doc, discrepancies)

    if args.sync:
        target_user = str(args.user or "").strip()
        if not target_user:
            print("ERROR: --sync requires --user <user_id> (or DALTON_USER_ID)", file=sys.stderr)
            return 2
        return sync_to_user_coverage_db(target_user, args.out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
