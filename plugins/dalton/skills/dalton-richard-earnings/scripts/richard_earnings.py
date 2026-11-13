"""Richard earnings weekly scan: roll, record, calendar apply, and summary.

The script calls ``research(route="gcal")``. It does not run ``gws`` and does
not import the watchlist. Calendar ids stay on the host.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
UNIVERSE_PATH = SKILL_ROOT / "references" / "watchlist_universe.json"
WATCHLIST_REL = "state/richard-earnings/watchlist.json"
SCAN_ROOT = "state/richard-earnings/scans"
ET = ZoneInfo("America/New_York")
EXPECTED_COUNT = 105
GROUP_COUNTS = {"A": 27, "B": 34, "C": 22, "D": 22}
GROUPS = ("A", "B", "C", "D")
STATUSES = ("announced", "already_reported", "not_announced", "scan_failed")
TIMINGS = ("BMO", "AMC", "unknown")
CONF_RANK = {"high": 2, "medium": 1}
STATUS_RANK = {"announced": 2, "already_reported": 1, "not_announced": 0}
RECORD_KEYS = (
    "ticker",
    "name",
    "status",
    "earnings_date",
    "timing",
    "quarter_label",
    "confidence",
    "source_url",
    "evidence",
    "notes",
    "group",
)
NULLABLE_KEYS = (
    "earnings_date",
    "timing",
    "quarter_label",
    "confidence",
    "source_url",
    "evidence",
    "notes",
)
GATE_MINUTES = 75
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_QUARTER_RE = re.compile(r"^(FQ|Q)([1-4])\s+(FY)?(20\d{2})$", re.IGNORECASE)
_YEAR_RE = re.compile(r"(20\d{2})")
_INTERIM_RE = re.compile(r"中期|中报|半年报")
_ANNUAL_RE = re.compile(r"全年业绩|年报|全年")
_PATH_LEAKS = ("/Users/", "/private/", "/home/")


def _load_script(filename: str, module_name: str) -> Any:
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{filename} is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _card_io() -> Any:
    return _load_script("card_io.py", "card_io_dalton_richard_earnings")


def workspace_file(workspace: Path, relative: str) -> Path:
    return _card_io().workspace_file(workspace, relative)


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    _card_io().write_bytes_exclusive(path, payload)


def require_workspace(value: str | None) -> Path:
    if not value:
        raise ValueError("missing --workspace-dir")
    path = Path(value)
    if path.is_symlink():
        raise ValueError("path must stay inside the workspace")
    if not path.is_dir():
        raise ValueError("workspace not found")
    return path


def _public_error(exc: BaseException) -> str:
    text = str(exc)
    if any(marker in text for marker in _PATH_LEAKS):
        return exc.__class__.__name__
    return text


def now_et() -> datetime:
    """Clock for the scan date and the 75-minute gate. Tests replace this function."""
    return datetime.now(ET)


def today_et() -> date:
    return now_et().date()


def report_run_id(day: date) -> str:
    return f"richard-earnings-{day.isoformat()}"


def _fail(error: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return payload


def _dump(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json(workspace: Path, relative: str, data: Any) -> None:
    write_bytes_exclusive(workspace_file(workspace, relative), _dump(data))


def _read_json_file(path: Path) -> Any:
    if path.is_symlink():
        raise ValueError("refusing symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def _scan_rel(day: str, name: str) -> str:
    if _DATE_RE.fullmatch(day) is None:
        raise ValueError("bad scan date")
    return f"{SCAN_ROOT}/{day}/{name}"


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def load_universe() -> list[dict[str, Any]]:
    data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("universe")
    return data


def universe_index() -> dict[str, dict[str, Any]]:
    return {str(row["ticker"]): row for row in load_universe()}


def _universe_ok(rows: list[dict[str, Any]]) -> bool:
    if len(rows) != EXPECTED_COUNT:
        return False
    seen: set[str] = set()
    counts = {group: 0 for group in GROUPS}
    for row in rows:
        ticker = row.get("ticker")
        group = row.get("group")
        if not isinstance(ticker, str) or ticker in seen or group not in counts:
            return False
        seen.add(ticker)
        counts[group] += 1
    return counts == GROUP_COUNTS


def _watchlist_exists(workspace: Path) -> bool:
    try:
        path = workspace_file(workspace, WATCHLIST_REL)
    except ValueError:
        return False
    return path.is_file() and not path.is_symlink()


def call_research(**arguments: Any) -> dict[str, Any]:
    """Call ``research`` from the sandbox researchdata tool and return an object."""
    from tools.researchdata import research

    result = research(**arguments)
    if isinstance(result, str):
        parsed = json.loads(result)
        if not isinstance(parsed, dict):
            raise TypeError("research returned a non-object")
        return parsed
    if not isinstance(result, dict):
        raise TypeError("research returned a non-object")
    return result


def advance_period(label: str, earnings_date: str) -> str | None:
    """Return the next period label, or None when the label has no rule.

    ``Q2 2026`` and ``Q2 FY2027`` both roll forward. A fiscal label keeps
    ``FY``: ``Q4 FY2027`` becomes ``Q1 FY2028``. ``全年业绩`` / ``年报`` /
    ``全年`` advance to next year's ``中期``.
    """
    token = label.split("/", 1)[0].strip()
    matched = _QUARTER_RE.fullmatch(token)
    if matched:
        prefix = matched.group(1).upper()
        quarter = int(matched.group(2))
        fiscal = "FY" if matched.group(3) else ""
        year = int(matched.group(4))
        if quarter == 4:
            return f"{prefix}1 {fiscal}{year + 1}"
        return f"{prefix}{quarter + 1} {fiscal}{year}"
    year_match = _YEAR_RE.search(token)
    if year_match:
        year: int | None = int(year_match.group(1))
    elif len(earnings_date) >= 4 and earnings_date[:4].isdigit():
        year = int(earnings_date[:4])
    else:
        year = None
    if year is None:
        return None
    if _INTERIM_RE.search(token):
        return f"{year} 全年业绩"
    if _ANNUAL_RE.search(token):
        return f"{year + 1} 中期"
    return None


def _next_note(label: str, earnings_date: str) -> str:
    advanced = advance_period(label, earnings_date)
    shown = advanced if advanced is not None else label.strip()
    return f"下一期 {shown}".strip()


def _load_watchlist(workspace: Path) -> dict[str, Any] | None:
    path = workspace_file(workspace, WATCHLIST_REL)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("refusing symlink")
    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise ValueError("watchlist_unreadable")
    for key in ("confirmed", "pending", "reported"):
        if key not in data:
            data[key] = []
        if not isinstance(data[key], list):
            raise ValueError("watchlist_unreadable")
    return data


def _save_watchlist(workspace: Path, watchlist: dict[str, Any]) -> None:
    _write_json(workspace, WATCHLIST_REL, watchlist)


def _ticker_map(rows: list[Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in rows:
        if isinstance(item, dict) and isinstance(item.get("ticker"), str):
            found[item["ticker"]] = item
    return found


def _load_scans(workspace: Path, day: str) -> tuple[dict[str, dict[str, Any]], list[Any]]:
    scan_dir = workspace_file(workspace, _scan_rel(day, "placeholder")).parent
    if not scan_dir.is_dir():
        return {}, []
    recs: dict[str, dict[str, Any]] = {}
    bad: list[Any] = []
    paths = sorted(
        path
        for path in scan_dir.glob("*.jsonl")
        if path.is_file() and not path.is_symlink()
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                bad.append(f"{path.name}:{index + 1} {exc}")
                continue
            if not isinstance(row, dict):
                bad.append(f"{path.name}:{index + 1} not an object")
                continue
            checked = _normalize_record(row, date.fromisoformat(day))
            if checked.get("ok") is not True:
                ticker = row.get("ticker") if isinstance(row.get("ticker"), str) else None
                bad.append(
                    {
                        "file": path.name,
                        "line": index + 1,
                        "ticker": ticker,
                        "error": checked.get("error"),
                    }
                )
                continue
            row = checked["row"]
            ticker = row["ticker"]
            prev = recs.get(ticker)
            rank = STATUS_RANK.get(str(row.get("status")), 0)
            prev_rank = -1 if prev is None else STATUS_RANK.get(str(prev.get("status")), 0)
            if prev is None or rank >= prev_rank:
                recs[ticker] = row
    return recs, bad


def _blank(value: Any) -> Any:
    if value == "":
        return None
    return value


def _normalize_record(raw: dict[str, Any], day: date) -> dict[str, Any]:
    missing = [key for key in RECORD_KEYS if key not in raw]
    if missing:
        return _fail("missing_fields", fields=missing)
    unknown = sorted(key for key in raw if key not in RECORD_KEYS)
    if unknown:
        return _fail("unknown_fields", fields=unknown)
    row = {key: _blank(raw[key]) for key in RECORD_KEYS}
    ticker = row["ticker"]
    name = row["name"]
    status = row["status"]
    group = row["group"]
    if not isinstance(ticker, str) or not ticker:
        return _fail("invalid_field", field="ticker")
    if not isinstance(name, str) or not name.strip():
        return _fail("invalid_field", field="name")
    if not isinstance(status, str) or status not in STATUSES:
        return _fail("invalid_status")
    if not isinstance(group, str) or group not in GROUPS:
        return _fail("group_mismatch")
    known = universe_index().get(ticker)
    if known is None:
        return _fail("unknown_ticker")
    if known.get("group") != group:
        return _fail("group_mismatch")
    for key in ("quarter_label", "source_url", "evidence", "notes"):
        if row[key] is not None and not isinstance(row[key], str):
            return _fail("invalid_field", field=key)
    timing = row["timing"]
    if timing is not None and (not isinstance(timing, str) or timing not in TIMINGS):
        return _fail("invalid_field", field="timing")
    confidence = row["confidence"]
    if confidence is not None and (not isinstance(confidence, str) or confidence not in CONF_RANK):
        return _fail("invalid_field", field="confidence")
    earnings_date = row["earnings_date"]
    if earnings_date is not None and parse_iso_date(earnings_date) is None:
        return _fail("invalid_field", field="earnings_date")
    if status == "announced":
        parsed_day = parse_iso_date(earnings_date)
        if parsed_day is None or parsed_day < day or timing not in TIMINGS or confidence not in CONF_RANK:
            return _fail("invalid_announced")
    row["name"] = name.strip()
    return {"ok": True, "row": row}


def _append_jsonl(workspace: Path, relative: str, row: dict[str, Any]) -> None:
    path = workspace_file(workspace, relative)
    if path.is_symlink():
        raise ValueError("refusing symlink")
    existing = path.read_bytes() if path.is_file() else b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    write_bytes_exclusive(path, existing + line)


def _research_error(result: dict[str, Any]) -> Any:
    if "error" in result and result["error"] not in (None, ""):
        return result["error"]
    return result


def _events_from(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    if result.get("ok") is not True:
        return None
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    return [item for item in data["events"] if isinstance(item, dict)]


def _event_id_from(result: dict[str, Any]) -> str | None:
    if result.get("ok") is not True:
        return None
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"].strip():
        return data["id"].strip()
    return None


def _event_times(day: str, timing: str) -> tuple[dict[str, str], dict[str, str]]:
    if timing == "BMO":
        return (
            {"dateTime": f"{day}T08:00:00", "timeZone": "America/New_York"},
            {"dateTime": f"{day}T08:30:00", "timeZone": "America/New_York"},
        )
    if timing == "AMC":
        return (
            {"dateTime": f"{day}T16:00:00", "timeZone": "America/New_York"},
            {"dateTime": f"{day}T16:30:00", "timeZone": "America/New_York"},
        )
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {"date": day}, {"date": nxt}


def _event_fields(plan: dict[str, Any], scan_date: str) -> dict[str, Any]:
    timing = str(plan["timing"])
    tag = timing if timing in ("BMO", "AMC") else "时间未定"
    quarter = plan.get("quarter") or ""
    start, end = _event_times(str(plan["date"]), timing)
    source = plan.get("source") or ""
    confidence = plan.get("confidence") or ""
    return {
        "summary": f"{plan['name']} ({plan['ticker']}) Earnings — {quarter} [{tag}]",
        "description": f"{source}\nScanned {scan_date}\nConfidence: {confidence}",
        "start": start,
        "end": end,
    }


def _drop_ticker(rows: list[Any], ticker: str) -> list[Any]:
    return [
        item
        for item in rows
        if not (isinstance(item, dict) and item.get("ticker") == ticker)
    ]


def _delete_is_fatal(result: dict[str, Any]) -> bool:
    if result.get("ok") is True:
        return False
    if result.get("status") == "event_not_found" or result.get("error_code") == "event_not_found":
        return False
    return True


def _delete_is_refused(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "refused_not_tracker_event"
        or result.get("error_code") == "refused_not_tracker_event"
    )


def _current_event_ids(watchlist: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ("confirmed", "pending"):
        rows = watchlist.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            event_id = item.get("calendar_event_id")
            if isinstance(event_id, str) and event_id:
                found.add(event_id)
    return found


def _event_clock(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    stamp = value.get("dateTime")
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        zone_name = value.get("timeZone")
        if not isinstance(zone_name, str) or not zone_name.strip():
            zone_name = "America/New_York"
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
        except (KeyError, ValueError):
            return None
    return parsed.astimezone(ET)


def _slot_matches(event: dict[str, Any], day: str, timing: str) -> bool:
    """True when start and end match the whole BMO, AMC, or all-day window in ET."""
    start = event.get("start")
    end = event.get("end")
    target = date.fromisoformat(day)
    if timing in ("BMO", "AMC"):
        start_at = _event_clock(start)
        end_at = _event_clock(end)
        if start_at is None or end_at is None:
            return False
        hour = 8 if timing == "BMO" else 16
        expected_start = datetime.combine(target, datetime.min.time(), tzinfo=ET).replace(hour=hour)
        expected_end = expected_start + timedelta(minutes=30)
        return start_at == expected_start and end_at == expected_end
    if not isinstance(start, dict) or not isinstance(end, dict):
        return False
    if "dateTime" in start or "dateTime" in end:
        return False
    nxt = (target + timedelta(days=1)).isoformat()
    return start.get("date") == day and end.get("date") == nxt


def _current_pending_deletes(watchlist: dict[str, Any]) -> set[str]:
    raw = watchlist.get("pending_delete_event_ids")
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and item}


def _apply_scan_errors(bad: list[Any], recs: dict[str, dict[str, Any]]) -> list[Any]:
    """Bad JSON always counts. An invalid line counts only while that ticker has no finished row."""
    finished = {ticker for ticker, row in recs.items() if row.get("status") != "scan_failed"}
    chosen: list[Any] = []
    for item in bad:
        if not isinstance(item, dict):
            chosen.append(item)
            continue
        ticker = item.get("ticker")
        if isinstance(ticker, str) and ticker in finished:
            continue
        chosen.append(item)
    return chosen


def _remember_pending_delete(watchlist: dict[str, Any], event_id: str) -> None:
    raw = watchlist.get("pending_delete_event_ids")
    found = [item for item in raw if isinstance(item, str) and item] if isinstance(raw, list) else []
    if event_id not in found:
        found.append(event_id)
    watchlist["pending_delete_event_ids"] = found


def _retry_pending_deletes(watchlist: dict[str, Any], errors: list[Any]) -> None:
    raw = watchlist.get("pending_delete_event_ids")
    if not isinstance(raw, list) or not raw:
        return
    current = _current_event_ids(watchlist)
    kept: list[str] = []
    for event_id in raw:
        if not isinstance(event_id, str) or not event_id:
            continue
        if event_id in current:
            continue
        deleted = call_research(route="gcal", operation="delete_event", event_id=event_id)
        if _delete_is_refused(deleted):
            errors.append(_research_error(deleted))
            continue
        if _delete_is_fatal(deleted):
            errors.append(_research_error(deleted))
            if event_id not in kept:
                kept.append(event_id)
    watchlist["pending_delete_event_ids"] = kept


def _scan_failure(row: dict[str, Any]) -> dict[str, str]:
    detail = row.get("notes") or row.get("evidence") or row.get("source_url") or "scan_failed"
    if not isinstance(detail, str) or not detail.strip():
        detail = "scan_failed"
    name = row.get("name") if isinstance(row.get("name"), str) else ""
    ticker = row.get("ticker") if isinstance(row.get("ticker"), str) else ""
    return {"ticker": ticker, "name": name, "error": detail.strip()}


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    day = today_et()
    start, end = _day_bounds(day)
    calendar_ok = False
    try:
        result = call_research(
            route="gcal",
            operation="list_events",
            time_min=start,
            time_max=end,
            limit=1,
        )
        calendar_ok = isinstance(result, dict) and result.get("ok") is True
    except Exception:  # noqa: BLE001 - self-test records calendar failure without the exception text
        calendar_ok = False
    try:
        rows = load_universe()
        universe_ok = _universe_ok(rows)
        universe_count = len(rows)
    except (OSError, ValueError):
        universe_ok = False
        universe_count = 0
    return {
        "ok": calendar_ok and universe_ok,
        "calendar_ok": calendar_ok,
        "universe_ok": universe_ok,
        "universe_count": universe_count,
        "watchlist_exists": _watchlist_exists(workspace),
        "report_run_id": report_run_id(day),
        "date": day.isoformat(),
    }


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    rows = load_universe()
    counts = {group: 0 for group in GROUPS}
    for row in rows:
        group = str(row.get("group"))
        if group in counts:
            counts[group] += 1
    return {
        "ok": _universe_ok(rows),
        "count": len(rows),
        "groups": counts,
        "companies": rows,
    }


def cmd_roll(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    day = today_et()
    watchlist = _load_watchlist(workspace)
    if watchlist is None:
        return _fail("watchlist_missing", moved=[])
    pending = [item for item in watchlist["pending"]]
    pending_by = _ticker_map(pending)
    reported = list(watchlist["reported"])
    kept: list[Any] = []
    moved: list[dict[str, Any]] = []
    for entry in watchlist["confirmed"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            kept.append(entry)
            continue
        parsed = parse_iso_date(entry.get("date"))
        if parsed is None or parsed >= day:
            kept.append(entry)
            continue
        reported.append(dict(entry))
        label = entry.get("quarter") if isinstance(entry.get("quarter"), str) else ""
        earnings_date = entry.get("date") if isinstance(entry.get("date"), str) else ""
        note = _next_note(label, earnings_date)
        ticker = entry["ticker"]
        name = entry.get("name") if isinstance(entry.get("name"), str) and entry.get("name") else ticker
        existing = pending_by.get(ticker)
        if existing is None:
            created = {"ticker": ticker, "name": name, "note": note}
            pending.append(created)
            pending_by[ticker] = created
        else:
            existing["note"] = note
            if not existing.get("name"):
                existing["name"] = name
        moved.append(
            {"ticker": ticker, "name": name, "date": entry.get("date"), "note": note}
        )
    if moved:
        watchlist["confirmed"] = kept
        watchlist["pending"] = pending
        watchlist["reported"] = reported
        watchlist["updated"] = day.isoformat()
        _save_watchlist(workspace, watchlist)
    _write_json(
        workspace,
        _scan_rel(day.isoformat(), "roll.json"),
        {"date": day.isoformat(), "moved": moved},
    )
    return {"ok": True, "date": day.isoformat(), "moved": moved, "moved_count": len(moved)}


def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    source = workspace_file(workspace, args.input)
    if source.is_symlink():
        raise ValueError("refusing symlink")
    raw = _read_json_file(source)
    if not isinstance(raw, dict):
        return _fail("invalid_record")
    day_value = today_et()
    checked = _normalize_record(raw, day_value)
    if checked.get("ok") is not True:
        return checked
    row = checked["row"]
    day = day_value.isoformat()
    recs, _bad = _load_scans(workspace, day)
    existing = recs.get(row["ticker"])
    if existing is not None and existing.get("status") != "scan_failed":
        return {
            "ok": True,
            "status": "already_recorded",
            "ticker": row["ticker"],
            "date": day,
        }
    relative = _scan_rel(day, f"{row['group']}.jsonl")
    _append_jsonl(workspace, relative, row)
    return {"ok": True, "status": "recorded", "ticker": row["ticker"], "date": day, "group": row["group"]}


def _queue(workspace: Path, day: date, recorded: set[str]) -> list[dict[str, str]]:
    """A→D, and inside each group: pending, then future confirmed, then the rest."""
    watchlist = _load_watchlist(workspace)
    pending_ids: set[str] = set()
    confirmed_by: dict[str, dict[str, Any]] = {}
    if watchlist is not None:
        pending_ids = set(_ticker_map(watchlist["pending"]))
        confirmed_by = _ticker_map(watchlist["confirmed"])
    buckets: dict[str, dict[str, list[dict[str, str]]]] = {
        group: {"pending": [], "confirmed": [], "rest": []} for group in GROUPS
    }
    for row in load_universe():
        ticker = str(row["ticker"])
        if ticker in recorded:
            continue
        group = str(row.get("group") or "")
        if group not in buckets:
            continue
        item = {"ticker": ticker, "name": str(row.get("name") or ticker), "group": group}
        if ticker in pending_ids:
            buckets[group]["pending"].append(item)
            continue
        confirmed = confirmed_by.get(ticker)
        parsed = parse_iso_date(confirmed.get("date")) if isinstance(confirmed, dict) else None
        if parsed is not None and parsed >= day:
            buckets[group]["confirmed"].append(item)
            continue
        buckets[group]["rest"].append(item)
    ordered: list[dict[str, str]] = []
    for group in GROUPS:
        ordered.extend(buckets[group]["pending"])
        ordered.extend(buckets[group]["confirmed"])
        ordered.extend(buckets[group]["rest"])
    return ordered


def cmd_progress(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    now = now_et()
    day = now.date()
    started_rel = _scan_rel(day.isoformat(), "started.json")
    started_path = workspace_file(workspace, started_rel)
    if started_path.is_file():
        started = _read_json_file(started_path)
        if not isinstance(started, dict) or not isinstance(started.get("started_at"), str):
            return _fail("started_unreadable")
        started_at = started["started_at"]
    else:
        started_at = now.isoformat()
        _write_json(workspace, started_rel, {"started_at": started_at, "date": day.isoformat()})
    parsed = datetime.fromisoformat(started_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    elapsed = (now - parsed).total_seconds() / 60
    recs, bad = _load_scans(workspace, day.isoformat())
    finished = {
        ticker: row for ticker, row in recs.items() if row.get("status") != "scan_failed"
    }
    counts = {group: 0 for group in GROUPS}
    for row in finished.values():
        group = row.get("group")
        if group in counts:
            counts[group] += 1
    remaining = _queue(workspace, day, set(finished))
    unique = len(finished)
    return {
        "ok": True,
        "date": day.isoformat(),
        "started_at": started_at,
        "elapsed_minutes": elapsed,
        "gate_open": elapsed >= GATE_MINUTES or unique == EXPECTED_COUNT,
        "counts": counts,
        "unique": unique,
        "invalid_lines": bad,
        "invalid_count": len(bad),
        "remaining": remaining,
        "next": remaining[0] if remaining else None,
        "watchlist_exists": _watchlist_exists(workspace),
    }


def _apply_one(
    workspace: Path,
    watchlist: dict[str, Any],
    plan: dict[str, Any],
    scan_date: str,
    errors: list[Any],
) -> dict[str, Any] | None:
    ticker = plan["ticker"]
    start, end = _day_bounds(date.fromisoformat(str(plan["date"])))
    listed = call_research(
        route="gcal",
        operation="list_events",
        query=ticker,
        time_min=start,
        time_max=end,
        limit=50,
    )
    events = _events_from(listed)
    if events is None:
        errors.append(_research_error(listed))
        return None
    raw_old = plan.get("old_event_id")
    old_id = raw_old.strip() if isinstance(raw_old, str) else ""
    blocked = _current_pending_deletes(watchlist)
    if old_id:
        blocked.add(old_id)
    event_id = None
    for event in events:
        summary = event.get("summary") if isinstance(event.get("summary"), str) else ""
        candidate = event.get("id").strip() if isinstance(event.get("id"), str) else ""
        if f"({ticker})" not in summary or not candidate or candidate in blocked:
            continue
        if not _slot_matches(event, str(plan["date"]), str(plan["timing"])):
            continue
        event_id = candidate
        break
    if event_id is None:
        fields = _event_fields(plan, scan_date)
        inserted = call_research(route="gcal", operation="insert_event", **fields)
        event_id = _event_id_from(inserted)
        if event_id is None:
            errors.append(_research_error(inserted))
            return None
    delete_failed = False
    if plan["action"] == "replace" and old_id and old_id != event_id:
        deleted = call_research(route="gcal", operation="delete_event", event_id=old_id)
        if _delete_is_refused(deleted):
            errors.append(_research_error(deleted))
            delete_failed = True
        elif _delete_is_fatal(deleted):
            errors.append(_research_error(deleted))
            _remember_pending_delete(watchlist, old_id)
            delete_failed = True
    entry = {
        "ticker": ticker,
        "name": plan["name"],
        "date": plan["date"],
        "timing": plan["timing"],
        "quarter": plan.get("quarter") or "",
        "calendar_event_id": event_id,
        "source": plan.get("source") or "",
        "confidence": plan["confidence"],
        "marked_at": scan_date,
    }
    watchlist["pending"] = _drop_ticker(watchlist["pending"], ticker)
    watchlist["confirmed"] = _drop_ticker(watchlist["confirmed"], ticker)
    watchlist["confirmed"].append(entry)
    watchlist["confirmed"].sort(key=lambda item: str(item.get("date") or "") if isinstance(item, dict) else "")
    watchlist["updated"] = scan_date
    _save_watchlist(workspace, watchlist)
    if delete_failed:
        return None
    return {**plan, "event_id": event_id}


def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    scan_date = today_et().isoformat()
    recs, bad = _load_scans(workspace, scan_date)
    try:
        watchlist = _load_watchlist(workspace)
    except ValueError as exc:
        result = _fail(str(exc), mode="apply", scan_date=scan_date, errors=[str(exc)])
        _write_json(workspace, _scan_rel(scan_date, "apply.json"), result)
        return result
    if watchlist is None:
        if not recs:
            result = _fail(
                "no scan records",
                mode="apply",
                scan_date=scan_date,
                errors=_apply_scan_errors(bad, recs),
                applied=[],
                skipped=[],
            )
        else:
            result = _fail(
                "watchlist_missing",
                mode="apply",
                scan_date=scan_date,
                errors=["watchlist_missing"],
                applied=[],
                skipped=[],
            )
        _write_json(workspace, _scan_rel(scan_date, "apply.json"), result)
        return result
    errors = _apply_scan_errors(bad, recs)
    _retry_pending_deletes(watchlist, errors)
    if not recs:
        _save_watchlist(workspace, watchlist)
        result = _fail(
            "no scan records",
            mode="apply",
            scan_date=scan_date,
            errors=errors,
            applied=[],
            skipped=[],
        )
        _write_json(workspace, _scan_rel(scan_date, "apply.json"), result)
        return result
    pending = _ticker_map(watchlist["pending"])
    confirmed = _ticker_map(watchlist["confirmed"])
    universe = set(pending) | set(confirmed)
    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for ticker in sorted(recs):
        row = recs[ticker]
        if ticker not in universe:
            skipped.append({"ticker": ticker, "reason": "not in watchlist"})
            continue
        if row.get("status") == "scan_failed":
            errors.append(_scan_failure(row))
            continue
        if row.get("status") != "announced":
            continue
        earnings_date = row.get("earnings_date")
        timing = row.get("timing") or "unknown"
        confidence = row.get("confidence")
        if (
            not isinstance(earnings_date, str)
            or parse_iso_date(earnings_date) is None
            or earnings_date < scan_date
            or confidence not in CONF_RANK
            or timing not in TIMINGS
        ):
            skipped.append(
                {
                    "ticker": ticker,
                    "reason": f"invalid record date={earnings_date} timing={timing} conf={confidence}",
                }
            )
            continue
        if ticker in pending:
            plan.append(
                {
                    "action": "create",
                    "ticker": ticker,
                    "name": pending[ticker].get("name") or ticker,
                    "quarter": row.get("quarter_label") or "",
                    "date": earnings_date,
                    "timing": timing,
                    "confidence": confidence,
                    "source": row.get("source_url") or "",
                }
            )
            continue
        old = confirmed[ticker]
        old_timing = old.get("timing") or "unknown"
        if old.get("date") == earnings_date and old_timing == timing:
            continue
        if old.get("date") == earnings_date and timing == "unknown":
            continue
        old_rank = CONF_RANK.get(old.get("confidence"), 0)
        if CONF_RANK[confidence] < old_rank:
            skipped.append(
                {
                    "ticker": ticker,
                    "reason": (
                        f"scan {earnings_date} {timing} ({confidence}) weaker than "
                        f"recorded {old.get('date')} ({old.get('confidence')})"
                    ),
                }
            )
            continue
        quarter = old.get("quarter") or row.get("quarter_label") or ""
        plan.append(
            {
                "action": "replace",
                "ticker": ticker,
                "name": old.get("name") or ticker,
                "quarter": quarter,
                "date": earnings_date,
                "timing": timing,
                "confidence": confidence,
                "source": row.get("source_url") or "",
                "old_date": old.get("date"),
                "old_timing": old.get("timing"),
                "old_event_id": old.get("calendar_event_id"),
            }
        )
    applied: list[dict[str, Any]] = []
    for item in plan:
        try:
            done = _apply_one(workspace, watchlist, item, scan_date, errors)
        except Exception as exc:  # noqa: BLE001 - keep going, report at end
            errors.append(_public_error(exc))
            continue
        if done is not None:
            applied.append(done)
    watchlist["updated"] = scan_date
    _save_watchlist(workspace, watchlist)
    confirmed_ids = set(_ticker_map(watchlist["confirmed"]))
    pending_ids = set(_ticker_map(watchlist["pending"]))
    status_counts: dict[str, int] = {}
    for row in recs.values():
        status = str(row.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    result = {
        "ok": not errors,
        "mode": "apply",
        "scan_date": scan_date,
        "scanned_unique": len(recs),
        "status_counts": status_counts,
        "planned": plan,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "confirmed": len(confirmed_ids),
        "pending": len(pending_ids),
        "union": len(confirmed_ids | pending_ids),
        "overlap": sorted(confirmed_ids & pending_ids),
    }
    _write_json(workspace, _scan_rel(scan_date, "apply.json"), result)
    return result


def _timing_tag(timing: Any) -> str:
    if timing in ("BMO", "AMC"):
        return str(timing)
    return "时间未定"


def _section_names(rows: list[dict[str, Any]]) -> str:
    names = []
    for item in rows:
        name = item.get("name") or item.get("ticker") or ""
        if name:
            names.append(str(name))
    return "、".join(names)


def _gate_remaining(workspace: Path, day: date, recorded: set[str]) -> list[dict[str, str]]:
    """Unfinished names once the 75-minute gate has opened. Empty before that."""
    started_path = workspace_file(workspace, _scan_rel(day.isoformat(), "started.json"))
    if not started_path.is_file() or started_path.is_symlink():
        return []
    started = _read_json_file(started_path)
    if not isinstance(started, dict) or not isinstance(started.get("started_at"), str):
        return []
    parsed = datetime.fromisoformat(started["started_at"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    elapsed = (now_et() - parsed).total_seconds() / 60
    if elapsed < GATE_MINUTES:
        return []
    return _queue(workspace, day, recorded)


def cmd_summary(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    day = today_et()
    day_text = day.isoformat()
    apply_path = workspace_file(workspace, _scan_rel(day_text, "apply.json"))
    base = {
        "ok": True,
        "report_run_id": report_run_id(day),
        "date": day_text,
    }
    if not apply_path.is_file():
        return {**base, "text": "日历未写入：apply 没有运行"}
    applied_doc = _read_json_file(apply_path)
    if not isinstance(applied_doc, dict):
        raise ValueError("apply_unreadable")
    roll_path = workspace_file(workspace, _scan_rel(day_text, "roll.json"))
    moved: list[dict[str, Any]] = []
    if roll_path.is_file():
        roll_doc = _read_json_file(roll_path)
        if isinstance(roll_doc, dict) and isinstance(roll_doc.get("moved"), list):
            moved = [item for item in roll_doc["moved"] if isinstance(item, dict)]
    recs, _bad = _load_scans(workspace, day_text)
    already = [row for row in recs.values() if row.get("status") == "already_reported"]
    not_announced = [row for row in recs.values() if row.get("status") == "not_announced"]
    failed = [row for row in recs.values() if row.get("status") == "scan_failed"]
    applied = applied_doc.get("applied") if isinstance(applied_doc.get("applied"), list) else []
    skipped = applied_doc.get("skipped") if isinstance(applied_doc.get("skipped"), list) else []
    errors = applied_doc.get("errors") if isinstance(applied_doc.get("errors"), list) else []
    union = applied_doc.get("union") if isinstance(applied_doc.get("union"), int) else 0
    confirmed = applied_doc.get("confirmed") if isinstance(applied_doc.get("confirmed"), int) else 0
    pending = applied_doc.get("pending") if isinstance(applied_doc.get("pending"), int) else 0
    unfinished = _gate_remaining(workspace, day, set(recs))
    quiet = (
        union == EXPECTED_COUNT
        and not applied
        and not skipped
        and not moved
        and not already
        and not errors
        and not failed
        and not unfinished
    )
    if quiet:
        text = f"{confirmed} 家已确认 / {pending} 家未披露"
        return {**base, "text": text}
    if union != EXPECTED_COUNT:
        line_1 = f"日历未写入：并集 {union}，不等于 105"
    else:
        line_1 = f"已核查 {len(recs)}/105"
    creates = sum(1 for item in applied if isinstance(item, dict) and item.get("action") == "create")
    replaces = sum(1 for item in applied if isinstance(item, dict) and item.get("action") == "replace")
    line_2 = (
        f"日历：新增 {creates} / 改期 {replaces} / 跳过 {len(skipped)}，"
        f"watchlist confirmed {confirmed} + pending {pending} = {union}"
    )
    lines = [line_1, line_2]
    for item in skipped:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker") or ""
        reason = item.get("reason") or ""
        lines.append(f"跳过：{ticker} {reason}".rstrip())
    if moved:
        lines.append(f"滚动移回 pending：{_section_names(moved)}")
    for item in applied:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or ""
        lines.append(
            f"新增或改期：{item.get('name')} {item.get('date')} {_timing_tag(item.get('timing'))} {source}".rstrip()
        )
    if already:
        lines.append(f"already_reported：{_section_names(already)}")
    lines.append(f"仍待官宣：{len(not_announced)}")
    for row in failed:
        detail = row.get("notes") or row.get("evidence") or ""
        if not isinstance(detail, str):
            detail = ""
        who = row.get("name") or row.get("ticker") or ""
        lines.append(f"扫描失败：{who} {detail}".rstrip())
    if unfinished:
        shown = "、".join(f"{item['name']} ({item['ticker']})" for item in unfinished)
        lines.append(f"未完成：{shown}")
    if errors:
        lines.append("errors：")
        for err in errors:
            if isinstance(err, str):
                lines.append(err)
            else:
                lines.append(json.dumps(err, ensure_ascii=False))
    return {**base, "text": "\n".join(lines)}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Richard earnings weekly helper")
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--json", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("self-test", parents=[shared])
    probe.set_defaults(func=cmd_self_test)

    listed = sub.add_parser("list", parents=[shared])
    listed.set_defaults(func=cmd_list)

    roll = sub.add_parser("roll", parents=[shared])
    roll.set_defaults(func=cmd_roll)

    record = sub.add_parser("record", parents=[shared])
    record.add_argument("--input", required=True)
    record.set_defaults(func=cmd_record)

    progress = sub.add_parser("progress", parents=[shared])
    progress.set_defaults(func=cmd_progress)

    apply = sub.add_parser("apply", parents=[shared])
    apply.set_defaults(func=cmd_apply)

    summary = sub.add_parser("summary", parents=[shared])
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit:
        return 2
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": _public_error(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
