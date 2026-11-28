#!/usr/bin/env python3
"""Everflow production operations probe for Google Workspace official MCP migration (§7.1(1), §7.4).

Subcommands:
  - ``calendar-purge-drill``: isolated non-production test-stack purge drill.
    Enforces 3 mandatory non-production guards before touching DB or calendar:
      1) Database name ends with ``_test``
      2) ``DALTON_TRACKER_CALENDAR_OWNER_ID`` != production owner UUID
      3) Target calendar is a dedicated test sub-calendar (not primary, not prod,
         and carries a test sub-calendar marker)
  - ``prod-readonly-check`` (alias ``calendar``): verifies production tracker owner
    connectivity via read-only ``health_check`` + 400-day EST/EDT ``list_events``,
    and either runs a scoped ``[PROBE-<uuid>]`` write round-trip or reports
    ``跳过：需要写权限`` when the owner's grant is read-only.
  - ``drain``: polls PostgreSQL ``automation_executions`` (and host mail locks for
    ``--target mail``) until running count reaches 0, exiting with code 2 on timeout.

Never prints or logs OAuth tokens, refresh tokens, or full calendar event contents.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROD_TRACKER_CALENDAR_OWNER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
TEST_CALENDAR_MARKERS: tuple[str, ...] = (
    "test",
    "drill",
    "probe",
    "sandbox",
    "staging",
    "测试",
    "演练",
)
DRAIN_TIMEOUT_EXIT_CODE = 2
READONLY_SKIP_REASON = "跳过：需要写权限"
_ET = ZoneInfo("America/New_York")


def extract_database_name(database_url: str) -> str:
    """Extract the database name from a PostgreSQL connection URL."""
    raw = str(database_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = (parsed.path or "").strip("/")
    return path


def validate_non_prod_drill_guard(
    *,
    database_url: str,
    owner_id: str,
    calendar_id: str,
    prod_owner_id: str = PROD_TRACKER_CALENDAR_OWNER_ID,
    prod_calendar_id: str | None = None,
    calendar_name: str | None = None,
) -> tuple[bool, list[str]]:
    """Enforce the 3 mandatory non-production guards for ``calendar-purge-drill``.

    Returns ``(ok, reasons)``; any non-empty ``reasons`` list must abort with non-zero exit.
    """
    reasons: list[str] = []

    # Guard 1: Database name must end with `_test`.
    db_name = extract_database_name(database_url)
    if not db_name or not db_name.endswith("_test"):
        reasons.append(
            f"database name must end with '_test' (got {db_name or '<empty>'!r})"
        )

    # Guard 2: DALTON_TRACKER_CALENDAR_OWNER_ID must be set and NOT equal to production owner.
    cleaned_owner = str(owner_id or "").strip()
    cleaned_prod_owner = str(prod_owner_id or PROD_TRACKER_CALENDAR_OWNER_ID).strip()
    if not cleaned_owner:
        reasons.append("DALTON_TRACKER_CALENDAR_OWNER_ID must be configured for test drill")
    elif cleaned_owner.lower() == cleaned_prod_owner.lower():
        reasons.append(
            "DALTON_TRACKER_CALENDAR_OWNER_ID must not equal the production tracker owner"
        )

    # Guard 3: Target calendar must be a dedicated test sub-calendar.
    cleaned_cal = str(calendar_id or "").strip()
    cleaned_prod_cal = str(prod_calendar_id or "").strip()
    cleaned_name = str(calendar_name or "").strip()
    if not cleaned_cal:
        reasons.append("target calendar ID must be configured (test sub-calendar required)")
    elif cleaned_cal.lower() == "primary":
        reasons.append("target calendar must be a test sub-calendar, never 'primary'")
    elif cleaned_prod_cal and cleaned_cal.lower() == cleaned_prod_cal.lower():
        reasons.append("target calendar must not equal the production tracker calendar")
    else:
        haystack = f"{cleaned_cal} {cleaned_name}".lower()
        if not any(marker in haystack for marker in TEST_CALENDAR_MARKERS):
            reasons.append(
                "target calendar must be an identifiable test sub-calendar "
                "(include a test/drill/probe/sandbox marker in calendar ID or --calendar-name)"
            )

    return (len(reasons) == 0), reasons


def _is_flock_held(path: Path) -> bool:
    """Return True if an exclusive flock is currently held on ``path``."""
    if not path.exists():
        return False
    try:
        with open(path, "rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
    except OSError:
        return False


def check_mail_host_locks(research_home: Path) -> list[str]:
    """Return names of any currently held host mail/image-ingest lock files."""
    candidates = [
        research_home / "state" / "market-digest" / "batch.lock",
        research_home / "state" / "market-digest" / "inputs.lock",
        research_home / "state" / "research-images" / "worker.lock",
    ]
    return [str(p.relative_to(research_home)) for p in candidates if _is_flock_held(p)]


async def _count_running_executions(
    automation_ids: Sequence[str],
    *,
    conn: Any = None,
) -> int:
    from psycopg.rows import dict_row

    from src.server.database.pool import get_db_connection

    ids = [str(aid).strip() for aid in automation_ids if str(aid).strip()]
    if not ids:
        return 0
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COUNT(*)::int AS running_count
                FROM automation_executions
                WHERE automation_id = ANY(%s::uuid[])
                  AND status = 'running'
                """,
                (ids,),
            )
            row = await cur.fetchone()
            return int(row["running_count"]) if row else 0


async def run_calendar_purge_drill(
    *,
    database_url: str,
    owner_id: str,
    calendar_id: str,
    prod_owner_id: str = PROD_TRACKER_CALENDAR_OWNER_ID,
    prod_calendar_id: str | None = None,
    calendar_name: str | None = None,
    workspace_dir: Path | None = None,
    purge_id: str | None = None,
    dry_run: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Execute Stage A isolated non-production purge drill (§7.1(1))."""
    ok, reasons = validate_non_prod_drill_guard(
        database_url=database_url,
        owner_id=owner_id,
        calendar_id=calendar_id,
        prod_owner_id=prod_owner_id,
        prod_calendar_id=prod_calendar_id,
        calendar_name=calendar_name,
    )
    if not ok:
        return 1, {
            "ok": False,
            "command": "calendar-purge-drill",
            "guard_failed": True,
            "reasons": reasons,
        }

    if dry_run:
        return 0, {
            "ok": True,
            "command": "calendar-purge-drill",
            "dry_run": True,
            "guard_passed": True,
            "database_name": extract_database_name(database_url),
        }

    from src.dalton.database import user_integrations as integrations_db
    from src.dalton.integrations.tracker_calendar import (
        register_default_tracker_purge_handler,
    )
    from src.dalton.services.integration_data_purge import run_local_purge
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect,
    )

    register_default_tracker_purge_handler()
    if workspace_dir is not None:
        os.environ["DALTON_TRACKER_WORKSPACE_DIR"] = str(workspace_dir)

    if purge_id:
        report = await run_local_purge(purge_id)
        return 0, {
            "ok": True,
            "command": "calendar-purge-drill",
            "guard_passed": True,
            "purge_id": report.purge_id,
            "threads_purged": len(report.threads_purged),
            "tracker_reconcile_needed": report.tracker_reconcile_needed,
        }

    conn_row = await integrations_db.get_connection(
        owner_id, "google", "calendar", decrypt_tokens=False
    )
    if conn_row is None:
        return 1, {
            "ok": False,
            "command": "calendar-purge-drill",
            "guard_passed": True,
            "error": "test owner google/calendar connection not found in test database",
        }

    created_purge_id = await begin_disconnect(
        owner_id,
        "google",
        "calendar",
        remote_mode="local_only",
    )
    finished = await finish_disconnect(
        created_purge_id,
        revoker=None,
        run_remote_revoke=False,
    )
    ledger = await integrations_db.get_purge_ledger(
        created_purge_id, decrypt_revoke_token=False
    )
    return 0, {
        "ok": finished is not None and finished.local_purge_status.value == "completed",
        "command": "calendar-purge-drill",
        "guard_passed": True,
        "purge_id": created_purge_id,
        "local_purge_status": (
            finished.local_purge_status.value if finished is not None else "unknown"
        ),
        "tracker_reconcile_needed": bool(
            ledger.tracker_reconcile_needed if ledger is not None else True
        ),
    }


async def run_prod_readonly_check(
    *,
    owner_id: str,
    expected_calendar_id: str | None = None,
    google_client: Any = None,
    oauth_client: Any = None,
    now_et: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify production tracker calendar read-only connectivity and scoped [PROBE-<uuid>] write round-trip."""
    from src.dalton.database import user_integrations as integrations_db
    from src.dalton.integrations.tracker_calendar import (
        TRACKER_GCAL_MARKER,
        execute_tracker_calendar_official_mcp,
        has_tracker_read_scope,
        has_tracker_write_scope,
        is_tracker_event,
    )

    cleaned_owner = str(owner_id or "").strip()
    if not cleaned_owner:
        return 1, {
            "ok": False,
            "command": "prod-readonly-check",
            "error": "DALTON_TRACKER_CALENDAR_OWNER_ID is not configured",
        }

    conn_row = await integrations_db.get_connection(
        cleaned_owner, "google", "calendar", decrypt_tokens=False
    )
    if conn_row is None:
        return 1, {
            "ok": False,
            "command": "prod-readonly-check",
            "error": "owner google/calendar connection not found",
        }

    prefs = dict(conn_row.preferences or {})
    configured_cal_id = str(prefs.get("tracker_calendar_id") or "").strip()
    env_cal_id = str(expected_calendar_id or "").strip()
    calendar_id_matched = (
        True
        if not env_cal_id
        else (configured_cal_id == env_cal_id)
    )
    has_read = has_tracker_read_scope(conn_row.granted_scopes)
    has_write = has_tracker_write_scope(conn_row.granted_scopes)

    ref_now = now_et or datetime.now(_ET)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=_ET)
    else:
        ref_now = ref_now.astimezone(_ET)

    # 1. Read-only health_check (pageSize=1)
    health_payload, _ = await execute_tracker_calendar_official_mcp(
        owner_id=cleaned_owner,
        operation="health_check",
        arguments={},
        google_client=google_client,
        oauth_client=oauth_client,
        now_et=ref_now,
    )
    health_ok = (
        health_payload.get("ok") is True
        and isinstance(health_payload.get("data"), dict)
        and health_payload["data"].get("calendar_ok") is True
    )

    # 2. Read-only 400-day list_events spanning EST & EDT transitions
    list_start = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
    list_end = list_start + timedelta(days=400)
    list_payload, _ = await execute_tracker_calendar_official_mcp(
        owner_id=cleaned_owner,
        operation="list_events",
        arguments={
            "time_min": list_start.isoformat(),
            "time_max": list_end.isoformat(),
            "limit": 10,
        },
        google_client=google_client,
        oauth_client=oauth_client,
        now_et=ref_now,
    )
    list_ok = list_payload.get("ok") is True
    event_count = (
        int(list_payload["data"].get("event_count", 0))
        if list_ok and isinstance(list_payload.get("data"), dict)
        else 0
    )

    # 3. Scoped [PROBE-<uuid>] write round-trip (skipped honestly when read-only)
    write_roundtrip: dict[str, Any]
    write_ok = True
    if not has_write:
        write_roundtrip = {
            "status": "skipped",
            "reason": READONLY_SKIP_REASON,
        }
    else:
        probe_tag = f"[PROBE-{uuid4().hex[:12]}]"
        probe_day = (ref_now.date() + timedelta(days=2)).isoformat()
        insert_payload, _ = await execute_tracker_calendar_official_mcp(
            owner_id=cleaned_owner,
            operation="insert_event",
            arguments={
                "summary": f"Dalton Probe (PROBE) Earnings — {probe_tag} [BMO]",
                "description": f"https://probe.local/{probe_tag}\nScanned {ref_now.date().isoformat()}",
                "start": {"dateTime": f"{probe_day}T08:00:00", "timeZone": "America/New_York"},
                "end": {"dateTime": f"{probe_day}T08:30:00", "timeZone": "America/New_York"},
            },
            google_client=google_client,
            oauth_client=oauth_client,
            now_et=ref_now,
        )
        created = insert_payload.get("data") if insert_payload.get("ok") is True else None
        created_id = (
            str(created.get("id") or "").strip() if isinstance(created, dict) else ""
        )
        marker_ok = (
            isinstance(created, dict)
            and is_tracker_event(created)
            and TRACKER_GCAL_MARKER in str(created.get("description") or "")
        )
        deleted_ok = False
        if created_id:
            del_payload, _ = await execute_tracker_calendar_official_mcp(
                owner_id=cleaned_owner,
                operation="delete_event",
                arguments={"event_id": created_id},
                google_client=google_client,
                oauth_client=oauth_client,
                now_et=ref_now,
            )
            deleted_ok = del_payload.get("ok") is True
        write_ok = bool(created_id) and marker_ok and deleted_ok
        write_roundtrip = {
            "status": "passed" if write_ok else "failed",
            "probe_tag": probe_tag,
            "created": bool(created_id),
            "tracker_marker_ok": marker_ok,
            "deleted": deleted_ok,
        }

    overall_ok = calendar_id_matched and has_read and health_ok and list_ok and write_ok
    return (0 if overall_ok else 1), {
        "ok": overall_ok,
        "command": "prod-readonly-check",
        "owner_id": cleaned_owner,
        "provider_account_bound": bool(conn_row.provider_account_id),
        "calendar_id_matched": calendar_id_matched,
        "has_read_scope": has_read,
        "has_write_scope": has_write,
        "health_check": {
            "ok": health_ok,
            "status": health_payload.get("status"),
        },
        "list_events_400d": {
            "ok": list_ok,
            "event_count": event_count,
            "status": list_payload.get("status"),
        },
        "write_roundtrip": write_roundtrip,
    }


async def run_drain(
    *,
    target: str,
    automation_ids: Sequence[str],
    wait_seconds: int = 600,
    poll_interval: float = 5.0,
    research_home: Path | None = None,
    count_running_fn: Any = None,
    check_locks_fn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Poll until target automation executions (and mail host locks) drain, or exit 2 on timeout."""
    if target not in ("calendar", "mail"):
        return 1, {"ok": False, "command": "drain", "error": f"invalid target: {target}"}

    ids = [str(aid).strip() for aid in automation_ids if str(aid).strip()]
    if not ids:
        return 1, {
            "ok": False,
            "command": "drain",
            "target": target,
            "error": "no target automation IDs configured",
        }

    counter = count_running_fn or _count_running_executions
    lock_checker = check_locks_fn or check_mail_host_locks
    home = research_home or Path(
        os.getenv("DALTON_RESEARCH_HOME", str(Path.home() / ".openclaw" / "workspace"))
    ).expanduser()

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    sleep_s = max(0.05, float(poll_interval))
    running_count = 0
    active_locks: list[str] = []

    while True:
        running_count = int(await counter(ids))
        active_locks = list(lock_checker(home)) if target == "mail" else []
        if running_count == 0 and not active_locks:
            return 0, {
                "ok": True,
                "command": "drain",
                "target": target,
                "drained": True,
                "running_count": 0,
                "active_locks": [],
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return DRAIN_TIMEOUT_EXIT_CODE, {
                "ok": False,
                "command": "drain",
                "target": target,
                "drained": False,
                "running_count": running_count,
                "active_locks": active_locks,
            }
        await asyncio.sleep(min(sleep_s, remaining))


def _resolve_drain_automation_ids(target: str, explicit: Sequence[str] | None) -> list[str]:
    if explicit:
        return [s.strip() for s in explicit if s and s.strip()]
    if target == "calendar":
        val = os.getenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", "").strip()
        return [val] if val else []
    ids: list[str] = []
    for key in (
        "DALTON_MARKET_DIGEST_AM_AUTOMATION_ID",
        "DALTON_MARKET_DIGEST_PM_AUTOMATION_ID",
    ):
        val = os.getenv(key, "").strip()
        if val:
            ids.append(val)
    return ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google Workspace official MCP migration probe & drain CLI (Everflow ops)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_drill = sub.add_parser(
        "calendar-purge-drill",
        help="Run isolated non-production tracker calendar disconnect & 2A purge drill.",
    )
    p_drill.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    p_drill.add_argument(
        "--owner-id", default=os.getenv("DALTON_TRACKER_CALENDAR_OWNER_ID", "")
    )
    p_drill.add_argument(
        "--calendar-id", default=os.getenv("DALTON_GCAL_CALENDAR_ID", "")
    )
    p_drill.add_argument(
        "--calendar-name", default=os.getenv("DALTON_TEST_GCAL_CALENDAR_NAME", "")
    )
    p_drill.add_argument(
        "--prod-owner-id", default=PROD_TRACKER_CALENDAR_OWNER_ID
    )
    p_drill.add_argument(
        "--prod-calendar-id", default=os.getenv("DALTON_PROD_GCAL_CALENDAR_ID", "")
    )
    p_drill.add_argument("--workspace-dir", default=os.getenv("DALTON_TRACKER_WORKSPACE_DIR", ""))
    p_drill.add_argument("--purge-id", default="")
    p_drill.add_argument("--dry-run", action="store_true")

    for name in ("prod-readonly-check", "calendar"):
        p_prod = sub.add_parser(
            name,
            help="Verify production tracker calendar read-only connectivity and scoped [PROBE-<uuid>] round-trip.",
        )
        p_prod.add_argument(
            "--owner-id", default=os.getenv("DALTON_TRACKER_CALENDAR_OWNER_ID", "")
        )
        p_prod.add_argument(
            "--calendar-id", default=os.getenv("DALTON_GCAL_CALENDAR_ID", "")
        )

    p_drain = sub.add_parser(
        "drain",
        help="Poll until target automation executions and host locks finish (exits 2 on timeout).",
    )
    p_drain.add_argument("--target", choices=("calendar", "mail"), required=True)
    p_drain.add_argument("--wait-seconds", type=int, default=600)
    p_drain.add_argument("--poll-interval", type=float, default=5.0)
    p_drain.add_argument("--automation-id", action="append", default=[])
    p_drain.add_argument("--research-home", default="")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "calendar-purge-drill":
        ws = Path(args.workspace_dir).expanduser() if args.workspace_dir else None
        code, payload = asyncio.run(
            run_calendar_purge_drill(
                database_url=args.database_url,
                owner_id=args.owner_id,
                calendar_id=args.calendar_id,
                prod_owner_id=args.prod_owner_id,
                prod_calendar_id=args.prod_calendar_id or None,
                calendar_name=args.calendar_name or None,
                workspace_dir=ws,
                purge_id=args.purge_id or None,
                dry_run=bool(args.dry_run),
            )
        )
    elif args.command in ("prod-readonly-check", "calendar"):
        code, payload = asyncio.run(
            run_prod_readonly_check(
                owner_id=args.owner_id,
                expected_calendar_id=args.calendar_id or None,
            )
        )
    elif args.command == "drain":
        ids = _resolve_drain_automation_ids(args.target, args.automation_id)
        home = Path(args.research_home).expanduser() if args.research_home else None
        code, payload = asyncio.run(
            run_drain(
                target=args.target,
                automation_ids=ids,
                wait_seconds=args.wait_seconds,
                poll_interval=args.poll_interval,
                research_home=home,
            )
        )
    else:
        code, payload = 1, {"ok": False, "error": f"unknown command: {args.command}"}

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
