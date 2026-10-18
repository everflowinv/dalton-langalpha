#!/usr/bin/env python3
"""
calendar_state.py — Atomic ledger management CLI for Dalton earnings calendar (PS.7).

Subcommands:
  list   : Read coverage.yaml and earnings-calendar.json, print unique counts and entries.
  check  : Check a ticker's status and date against the ledger (`--ticker --date`).
  mark   : Atomically update a company's ledger entry (confirmed or pending/estimated), recording history on date changes.
  roll   : Roll confirmed entries whose release date (`date`) is strictly before `--today` into `reported`, and re-register as `pending` for the next quarter.
  diff   : Reconcile scan JSONL file(s) (`--scan <jsonl>`) against the ledger and optionally verify automation JSON files.
"""

import argparse
from datetime import date, datetime, timezone
import glob
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo


CHAIN_BY_TIER = {
    "core": ["tm3", "t0", "tp1"],
    "watch": ["t0"],
}


def _default_state_path() -> Path:
    env_path = os.environ.get("DALTON_EARNINGS_CALENDAR_PATH")
    if env_path:
        return Path(env_path)
    candidates = [
        Path("Home/dalton/earnings-calendar.json"),
        Path("dalton/earnings-calendar.json"),
        Path("../Home/dalton/earnings-calendar.json"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path("Home/dalton/earnings-calendar.json")


def _default_coverage_path() -> Path | None:
    env_path = os.environ.get("DALTON_COVERAGE_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    candidates = [
        Path("Home/dalton/coverage.yaml"),
        Path("dalton/coverage.yaml"),
        Path("../Home/dalton/coverage.yaml"),
        Path("config/dalton/coverage.yaml"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _load_yaml_or_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        return json.loads(text)
    except Exception:
        return _minimal_coverage_yaml_parse(text)


def _minimal_coverage_yaml_parse(text: str) -> dict:
    """Stdlib fallback parser for `companies:` list in coverage.yaml if PyYAML is unavailable."""
    companies: list[dict] = []
    cur: dict | None = None
    in_companies = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("companies:"):
            in_companies = True
            continue
        if not in_companies:
            continue
        if re.match(r"^\S", line):
            break
        m_item = re.match(r"^\s*-\s+ticker:\s*(.+)$", line)
        if m_item:
            if cur:
                companies.append(cur)
            cur = {"ticker": m_item.group(1).strip().strip("\"'")}
            continue
        if cur is not None:
            m_kv = re.match(r"^\s+([a-zA-Z0-9_]+):\s*(.+)$", line)
            if m_kv:
                k, v = m_kv.group(1), m_kv.group(2).strip()
                if k in ("name", "tier", "market", "note", "coverage_stage"):
                    cur[k] = None if v == "null" else v.strip("\"'")
                elif k == "workspace" and "status:" in v:
                    m_ws = re.search(r"status:\s*([a-zA-Z0-9_]+)", v)
                    cur["workspace"] = {"status": m_ws.group(1) if m_ws else "unknown"}
                elif k == "earnings" and "group:" in v:
                    m_gr = re.search(r"group:\s*([A-Za-z0-9]+)", v)
                    m_ir = re.search(r"ir_url:\s*([^\s,}]+)", v)
                    cur["earnings"] = {
                        "group": m_gr.group(1).strip("\"'") if m_gr else None,
                        "ir_url": m_ir.group(1).strip("\"'") if m_ir else None,
                    }
    if cur:
        companies.append(cur)
    return {"companies": companies}


def load_coverage(coverage_path: Path | None = None) -> dict[str, dict]:
    p = coverage_path or _default_coverage_path()
    if not p or not p.exists():
        return {}
    raw = _load_yaml_or_json(p)
    result: dict[str, dict] = {}
    for item in raw.get("companies") or []:
        if isinstance(item, dict) and item.get("ticker"):
            t = str(item["ticker"]).upper()
            result[t] = item
    return result


def load_ledger(state_path: Path, create: bool = False) -> dict:
    if not state_path.exists():
        st = {
            "updated": date.today().isoformat(),
            "entries": {},
            "reported": [],
        }
        if create:
            save_ledger_atomic(state_path, st)
        return st
    with state_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if "entries" not in data or not isinstance(data["entries"], dict):
        data["entries"] = {}
    if "reported" not in data or not isinstance(data["reported"], list):
        data["reported"] = []
    return data


def save_ledger_atomic(state_path: Path, ledger: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + f".{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)


def advance_quarter_label(q_label: str | None) -> str | None:
    """Advance quarter label to next sequential period (e.g. FY26Q3 -> FY26Q4, FY26Q4 -> FY27Q1)."""
    if not q_label:
        return None
    s = q_label.strip()
    # Pattern 1: FY26Q3 or FY2026Q3
    m = re.match(r"^(FY\d{2,4})Q([1-4])$", s, re.IGNORECASE)
    if m:
        fy_prefix, q_num = m.group(1), int(m.group(2))
        if q_num < 4:
            return f"{fy_prefix.upper()}Q{q_num + 1}"
        yr_str = fy_prefix[2:]
        next_yr = int(yr_str) + 1
        fmt = f"{next_yr:02d}" if len(yr_str) == 2 else f"{next_yr:04d}"
        return f"FY{fmt}Q1"
    # Pattern 2: Q2 2026 or FQ4 2026
    m2 = re.match(r"^(F?Q)([1-4])\s+(\d{4})$", s, re.IGNORECASE)
    if m2:
        prefix, q_num, yr = m2.group(1).upper(), int(m2.group(2)), int(m2.group(3))
        if q_num < 4:
            return f"{prefix}{q_num + 1} {yr}"
        return f"{prefix}1 {yr + 1}"
    # Pattern 3: FY26H1 / FY26H2
    m3 = re.match(r"^(FY\d{2,4})H([12])$", s, re.IGNORECASE)
    if m3:
        fy_prefix, h_num = m3.group(1), int(m3.group(2))
        if h_num == 1:
            return f"{fy_prefix.upper()}H2"
        yr_str = fy_prefix[2:]
        next_yr = int(yr_str) + 1
        fmt = f"{next_yr:02d}" if len(yr_str) == 2 else f"{next_yr:04d}"
        return f"FY{fmt}H1"
    return s


def slug_ticker(ticker: str) -> str:
    return ticker.strip().lower().replace(".", "-")


def default_automations_for(ticker: str, quarter: str, tier: str = "Core") -> dict[str, str]:
    t_slug = slug_ticker(ticker)
    q_slug = quarter.strip().lower().replace(" ", "")
    keys = CHAIN_BY_TIER.get(tier.lower(), ["tm3", "t0", "tp1"])
    return {k: f"dalton-{t_slug}-earn-{q_slug}-{k}.json" for k in keys}


def cmd_list(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else _default_state_path()
    cov_path = Path(args.coverage) if args.coverage else _default_coverage_path()
    coverage = load_coverage(cov_path)
    ledger = load_ledger(state_path, create=False)
    entries = ledger.get("entries", {})

    merged: list[dict] = []
    all_tickers = list(dict.fromkeys(list(coverage.keys()) + list(entries.keys())))
    for t in all_tickers:
        cov = coverage.get(t, {})
        ent = entries.get(t, {})
        tier = str(ent.get("tier") or cov.get("tier") or "Watch").capitalize()
        grp = (cov.get("earnings") or {}).get("group") if isinstance(cov.get("earnings"), dict) else None
        status = ent.get("status", "pending")
        date_status = ent.get("date_status", "unannounced")
        ws_status = (
            (cov.get("workspace") or {}).get("status")
            if isinstance(cov.get("workspace"), dict)
            else ("active" if ent.get("workspace_id") else "unknown")
        )
        row = {
            "ticker": t,
            "name": ent.get("name") or cov.get("name") or t,
            "tier": tier,
            "group": grp,
            "workspace_status": ws_status,
            "workspace_id": ent.get("workspace_id"),
            "status": status,
            "date_status": date_status,
            "quarter": ent.get("quarter"),
            "date": ent.get("date") or ent.get("tentative_date"),
            "timing": ent.get("timing"),
            "fiscal_note": ent.get("fiscal_note") or cov.get("note"),
        }
        if args.tier and row["tier"].lower() != args.tier.lower():
            continue
        if args.group and (row["group"] or "").upper() != args.group.upper():
            continue
        if args.status and row["status"].lower() != args.status.lower():
            continue
        merged.append(row)

    counts = {
        "total": len(merged),
        "confirmed": sum(1 for r in merged if r["status"] == "confirmed"),
        "pending": sum(1 for r in merged if r["status"] == "pending"),
        "estimated_pending": sum(1 for r in merged if r["date_status"] == "estimated"),
        "reported_history_count": len(ledger.get("reported", [])),
    }

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "state_path": str(state_path),
                    "coverage_path": str(cov_path) if cov_path else None,
                    "updated": ledger.get("updated"),
                    "counts": counts,
                    "companies": merged,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"Ledger: {state_path} (updated {ledger.get('updated')}) | "
            f"total={counts['total']} confirmed={counts['confirmed']} "
            f"pending={counts['pending']} (待确认={counts['estimated_pending']}) "
            f"reported={counts['reported_history_count']}"
        )
        for r in merged:
            d_str = r["date"] or "-"
            if r["date_status"] == "estimated" and r["date"]:
                d_str = f"{r['date']}(待确认)"
            print(
                f"  [{r['tier']:5s}] {r['ticker']:8s} {r['status']:9s} {r['quarter'] or '-':8s} "
                f"{d_str:18s} {r['timing'] or '-':7s} {r['name']}"
            )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else _default_state_path()
    ledger = load_ledger(state_path, create=False)
    ticker = args.ticker.upper()
    entry = ledger.get("entries", {}).get(ticker)

    is_confirmed = bool(entry and entry.get("status") == "confirmed" and entry.get("date_status") == "confirmed")
    old_date = entry.get("date") if entry else None
    same_date = bool(is_confirmed and old_date == args.date)
    has_autos = bool(entry and entry.get("automations"))
    recorded = bool(same_date and has_autos)
    date_changed = bool(is_confirmed and old_date and old_date != args.date)

    action_hint = "skip" if recorded else ("reschedule_in_place" if date_changed else "create_and_mark")
    payload = {
        "ok": True,
        "ticker": ticker,
        "query_date": args.date,
        "recorded": recorded,
        "date_changed": date_changed,
        "old_date": old_date,
        "action": action_hint,
        "entry": entry,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else _default_state_path()
    cov_path = Path(args.coverage) if args.coverage else _default_coverage_path()
    coverage = load_coverage(cov_path)
    ledger = load_ledger(state_path, create=True)

    ticker = args.ticker.upper()
    cov = coverage.get(ticker, {})
    existing = ledger["entries"].get(ticker, {})
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    name = args.name or existing.get("name") or cov.get("name") or ticker
    tier = (args.tier or existing.get("tier") or cov.get("tier") or "Core").capitalize()
    workspace_id = args.workspace_id if args.workspace_id is not None else existing.get("workspace_id")
    fiscal_note = args.fiscal_note or existing.get("fiscal_note") or cov.get("note") or "FY=CY"
    history = list(existing.get("history") or [])

    status = args.status.lower()
    if status == "confirmed":
        if not args.date:
            raise ValueError("--date is required when --status=confirmed")
        if not args.quarter:
            raise ValueError("--quarter is required when --status=confirmed")
        old_date = existing.get("date")
        if old_date and old_date != args.date:
            history.append(
                {
                    "at": now_iso,
                    "from": {"date": old_date, "timing": existing.get("timing")},
                    "to": {"date": args.date, "timing": args.timing or existing.get("timing")},
                    "reason": args.reason or "公司改期或更新披露日期",
                }
            )
        if args.automations_json:
            automations = json.loads(args.automations_json)
        elif any([args.tm3, args.t0, args.tp1]):
            automations = {}
            if args.tm3:
                automations["tm3"] = args.tm3
            if args.t0:
                automations["t0"] = args.t0
            if args.tp1:
                automations["tp1"] = args.tp1
        elif existing.get("automations") and existing.get("quarter") == args.quarter:
            automations = existing["automations"]
        else:
            automations = default_automations_for(ticker, args.quarter, tier=tier)

        new_entry = {
            "name": name,
            "tier": tier,
            "workspace_id": workspace_id,
            "fiscal_note": fiscal_note,
            "status": "confirmed",
            "date_status": "confirmed",
            "quarter": args.quarter,
            "date": args.date,
            "timing": args.timing or existing.get("timing") or "unknown",
            "release_time_et": args.release_time_et or existing.get("release_time_et"),
            "call_time_et": args.call_time_et or existing.get("call_time_et"),
            "confidence": args.confidence or "high",
            "source_url": args.source_url or existing.get("source_url"),
            "source_type": args.source_type or existing.get("source_type") or "company_ir",
            "automations": automations,
            "checked_at": now_iso,
            "history": history,
        }
    elif status == "pending":
        tentative_date = args.tentative_date or args.date
        date_status = "estimated" if tentative_date else "unannounced"
        tentative_sources = (
            [s.strip() for s in args.tentative_sources.split(",") if s.strip()]
            if args.tentative_sources
            else existing.get("tentative_sources", [])
        )
        new_entry = {
            "name": name,
            "tier": tier,
            "workspace_id": workspace_id,
            "fiscal_note": fiscal_note,
            "status": "pending",
            "date_status": date_status,
            "quarter": args.quarter or existing.get("quarter"),
            "date": None,
            "tentative_date": tentative_date,
            "tentative_sources": tentative_sources,
            "timing": args.timing or existing.get("timing") or "unknown",
            "confidence": args.confidence or ("low" if tentative_date else None),
            "source_url": args.source_url,
            "source_type": args.source_type or ("aggregator" if tentative_date else None),
            "automations": {},
            "checked_at": now_iso,
            "history": history,
        }
        if args.recheck_automation:
            new_entry["recheck_automation"] = args.recheck_automation
    else:
        raise ValueError(f"Unsupported --status '{args.status}'; must be 'confirmed' or 'pending'")

    ledger["entries"][ticker] = new_entry
    ledger["updated"] = date.today().isoformat()
    save_ledger_atomic(state_path, ledger)

    print(
        json.dumps(
            {
                "ok": True,
                "ticker": ticker,
                "status": new_entry["status"],
                "date_status": new_entry["date_status"],
                "date": new_entry.get("date") or new_entry.get("tentative_date"),
                "state_path": str(state_path),
                "entry": new_entry,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_roll(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else _default_state_path()
    today_d = date.fromisoformat(args.today) if args.today else date.today()
    ledger = load_ledger(state_path, create=True)

    rolled: list[dict] = []
    entries = ledger.get("entries", {})
    reported = ledger.get("reported", [])
    existing_reported_keys = {(r.get("ticker"), r.get("quarter"), r.get("date")) for r in reported}

    for ticker, ent in list(entries.items()):
        if ent.get("status") != "confirmed" or not ent.get("date"):
            continue
        try:
            rel_d = date.fromisoformat(ent["date"])
        except ValueError:
            continue
        # Roll only when earnings release date (not call date) has strictly passed
        if rel_d < today_d:
            rep_item = {
                "ticker": ticker,
                "quarter": ent.get("quarter"),
                "date": ent.get("date"),
                "timing": ent.get("timing"),
                "automations": ent.get("automations", {}),
            }
            rep_key = (rep_item["ticker"], rep_item["quarter"], rep_item["date"])
            if rep_key not in existing_reported_keys:
                reported.append(rep_item)
                existing_reported_keys.add(rep_key)

            next_q = advance_quarter_label(ent.get("quarter"))
            rolled.append(
                {
                    "ticker": ticker,
                    "reported_quarter": ent.get("quarter"),
                    "reported_date": ent.get("date"),
                    "next_quarter": next_q,
                }
            )
            entries[ticker] = {
                "name": ent.get("name", ticker),
                "tier": ent.get("tier", "Core"),
                "workspace_id": ent.get("workspace_id"),
                "fiscal_note": ent.get("fiscal_note", "FY=CY"),
                "status": "pending",
                "date_status": "unannounced",
                "quarter": next_q,
                "date": None,
                "timing": "unknown",
                "release_time_et": None,
                "call_time_et": None,
                "confidence": None,
                "source_url": None,
                "source_type": None,
                "automations": {},
                "checked_at": ent.get("checked_at"),
                "history": ent.get("history", []),
            }

    ledger["updated"] = today_d.isoformat()
    save_ledger_atomic(state_path, ledger)

    print(
        json.dumps(
            {
                "ok": True,
                "today": today_d.isoformat(),
                "rolled_count": len(rolled),
                "rolled": rolled,
                "state_path": str(state_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _verify_automation_file(auto_path: Path, expected_date: str, expected_tz: str = "America/New_York") -> dict:
    """Read back an automation JSON file and verify active status & timezone-converted date."""
    if not auto_path.exists():
        return {"ok": False, "reason": "file_missing", "path": str(auto_path)}
    try:
        data = json.loads(auto_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": f"json_parse_error: {exc}", "path": str(auto_path)}

    status = data.get("status")
    state_block = data.get("state") or {}
    raw_next = state_block.get("next_run_at") or data.get("next_run_at")
    file_tz = data.get("timezone")

    if status != "active":
        return {"ok": False, "reason": f"status_not_active ({status})", "path": str(auto_path)}
    if not raw_next:
        return {"ok": False, "reason": "missing_next_run_at", "path": str(auto_path)}

    try:
        dt = datetime.fromisoformat(str(raw_next).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            if not file_tz:
                return {
                    "ok": False,
                    "reason": "待人工核对（next_run_at 无 UTC 偏移且无 timezone 字段）",
                    "path": str(auto_path),
                }
            dt = dt.replace(tzinfo=ZoneInfo(file_tz))
        market_dt = dt.astimezone(ZoneInfo(expected_tz))
        market_date_str = market_dt.date().isoformat()
    except Exception as exc:
        return {"ok": False, "reason": f"timezone_parse_error: {exc}", "path": str(auto_path)}

    if expected_date and market_date_str != expected_date:
        return {
            "ok": False,
            "reason": f"market_date_mismatch: expected {expected_date}, got {market_date_str} ({raw_next})",
            "path": str(auto_path),
        }
    return {
        "ok": True,
        "path": str(auto_path),
        "next_run_at": raw_next,
        "market_date": market_date_str,
        "market_time": market_dt.strftime("%H:%M"),
    }


def cmd_diff(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else _default_state_path()
    ledger = load_ledger(state_path, create=False)
    entries = ledger.get("entries", {})

    scan_files = sorted(glob.glob(args.scan))
    if not scan_files and Path(args.scan).exists():
        scan_files = [args.scan]

    if not scan_files:
        payload = {
            "ok": False,
            "error": f"No scan JSONL files matched '{args.scan}'",
            "hint": "Provide a valid --scan path or glob pattern.",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    records: list[dict] = []
    bad_lines: list[dict] = []
    for sf in scan_files:
        with open(sf, "r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, 1):
                s = raw.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                    if not isinstance(rec, dict) or not rec.get("ticker"):
                        raise ValueError("Missing 'ticker' key in JSON object")
                    records.append(rec)
                except Exception as exc:
                    bad_lines.append(
                        {
                            "file": sf,
                            "line": line_no,
                            "error": str(exc)[:120],
                            "raw": s[:240],
                        }
                    )

    confirmed_scans: list[dict] = []
    pending_estimated_scans: list[dict] = []
    scan_failed_list: list[dict] = []

    for r in records:
        st = str(r.get("status") or "").lower()
        if st in ("confirmed", "announced+high", "announced"):
            # Only official sources count as confirmed in Dalton PS.7
            conf = str(r.get("confidence") or "").lower()
            if st == "confirmed" or conf == "high":
                confirmed_scans.append(r)
            else:
                pending_estimated_scans.append(r)
        elif st in ("pending", "estimated", "announced+medium", "tentative"):
            pending_estimated_scans.append(r)
        elif st == "scan_failed":
            scan_failed_list.append(
                {
                    "ticker": str(r.get("ticker")).upper(),
                    "name": r.get("name"),
                    "notes": r.get("notes") or r.get("evidence") or "scan_failed",
                }
            )

    missing: list[dict] = []
    date_mismatch: list[dict] = []
    automation_issues: list[dict] = []

    for r in confirmed_scans:
        t = str(r["ticker"]).upper()
        scan_date = r.get("earnings_date")
        ent = entries.get(t)
        if not ent or ent.get("status") != "confirmed" or ent.get("date_status") != "confirmed":
            missing.append(
                {
                    "ticker": t,
                    "scan_date": scan_date,
                    "ledger_status": ent.get("status") if ent else "absent",
                    "reason": "not_confirmed_in_ledger",
                }
            )
            continue
        if ent.get("date") != scan_date:
            date_mismatch.append(
                {
                    "ticker": t,
                    "scan_date": scan_date,
                    "ledger_date": ent.get("date"),
                    "reason": "date_mismatch",
                }
            )
            continue

        # Check automations presence when workspace_id is active/present
        if ent.get("workspace_id"):
            tier_key = str(ent.get("tier") or "Core").lower()
            expected_keys = CHAIN_BY_TIER.get(tier_key, ["tm3", "t0", "tp1"])
            autos = ent.get("automations") or {}
            missing_keys = [k for k in expected_keys if not autos.get(k)]
            if missing_keys:
                missing.append(
                    {
                        "ticker": t,
                        "scan_date": scan_date,
                        "missing_automations": missing_keys,
                        "reason": "missing_automation_entries_in_ledger",
                    }
                )
            elif args.automations_dir:
                for k in expected_keys:
                    auto_file = Path(args.automations_dir) / autos[k]
                    # Only t0 is guaranteed to have the same market date as scan_date
                    exp_d = scan_date if k == "t0" else ""
                    v_res = _verify_automation_file(auto_file, expected_date=exp_d)
                    if not v_res["ok"]:
                        automation_issues.append({"ticker": t, "window": k, **v_res})

    reconciled = (
        len(missing) == 0
        and len(date_mismatch) == 0
        and len(bad_lines) == 0
        and len(automation_issues) == 0
    )

    out = {
        "ok": reconciled,
        "reconciled": reconciled,
        "scan_files": scan_files,
        "total_parsed_records": len(records),
        "confirmed_in_scan": len(confirmed_scans),
        "pending_estimated_in_scan": len(pending_estimated_scans),
        "missing": missing,
        "date_mismatch": date_mismatch,
        "automation_issues": automation_issues,
        "bad_lines": bad_lines,
        "scan_failed": scan_failed_list,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if reconciled else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dalton earnings calendar ledger CLI (PS.7)")
    p.add_argument("--json", action="store_true", help="Output JSON format")
    p.add_argument("--state", default=None, help="Path to earnings-calendar.json")
    p.add_argument("--coverage", default=None, help="Path to coverage.yaml")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_list = sub.add_parser("list", help="List coverage and ledger status")
    sp_list.add_argument("--tier", choices=["core", "watch", "Core", "Watch"], default=None)
    sp_list.add_argument("--group", default=None)
    sp_list.add_argument("--status", choices=["pending", "confirmed", "reported"], default=None)
    sp_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sp_list.set_defaults(fn=cmd_list)

    sp_check = sub.add_parser("check", help="Check if ticker + date is already recorded in ledger")
    sp_check.add_argument("--ticker", required=True)
    sp_check.add_argument("--date", required=True, help="YYYY-MM-DD")
    sp_check.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sp_check.set_defaults(fn=cmd_check)

    sp_mark = sub.add_parser("mark", help="Atomically update a ticker entry in the ledger")
    sp_mark.add_argument("--ticker", required=True)
    sp_mark.add_argument("--status", default="confirmed", choices=["confirmed", "pending"])
    sp_mark.add_argument("--quarter", default=None, help="e.g. FY26Q3")
    sp_mark.add_argument("--date", default=None, help="Official earnings release date YYYY-MM-DD")
    sp_mark.add_argument("--tentative-date", default=None, help="Aggregator tentative date YYYY-MM-DD (for pending)")
    sp_mark.add_argument("--tentative-sources", default=None, help="Comma-separated aggregator sources")
    sp_mark.add_argument("--timing", default=None, help="BMO | AMC | unknown")
    sp_mark.add_argument("--release-time-et", default=None, help="e.g. 16:05")
    sp_mark.add_argument("--call-time-et", default=None, help="e.g. 16:30")
    sp_mark.add_argument("--confidence", default=None, help="high | medium | low")
    sp_mark.add_argument("--source-url", default=None)
    sp_mark.add_argument("--source-type", default=None, help="company_ir | ir_events | sec_filing | aggregator")
    sp_mark.add_argument("--name", default=None)
    sp_mark.add_argument("--tier", default=None)
    sp_mark.add_argument("--workspace-id", default=None)
    sp_mark.add_argument("--fiscal-note", default=None)
    sp_mark.add_argument("--tm3", default=None)
    sp_mark.add_argument("--t0", default=None)
    sp_mark.add_argument("--tp1", default=None)
    sp_mark.add_argument("--automations-json", default=None)
    sp_mark.add_argument("--recheck-automation", default=None)
    sp_mark.add_argument("--reason", default=None, help="Reason for date change (recorded in history)")
    sp_mark.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sp_mark.set_defaults(fn=cmd_mark)

    sp_roll = sub.add_parser("roll", help="Roll past confirmed entries into reported and reset to pending")
    sp_roll.add_argument("--today", default=None, help="Current date YYYY-MM-DD (default: today)")
    sp_roll.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sp_roll.set_defaults(fn=cmd_roll)

    sp_diff = sub.add_parser("diff", help="Reconcile scan JSONL file(s) against ledger")
    sp_diff.add_argument("--scan", required=True, help="Path or glob to earnings-scan-*.jsonl")
    sp_diff.add_argument("--automations-dir", default=None, help="Optional directory to read back automation JSON files")
    sp_diff.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sp_diff.set_defaults(fn=cmd_diff)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc), "hint": "Run with --help to check arguments."}, ensure_ascii=False),
            file=sys.stdout,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
