#!/usr/bin/env python3
"""Everflow production operations probe for Google Workspace official MCP migration (§7.1(1), §7.4).

Subcommands:
  - ``calendar-purge-drill``: isolated non-production test-stack purge drill.
    Enforces 3 mandatory non-production guards on ACTUAL resources before touching
    DB or calendar:
      1) Actual pool database name (``current_database()`` & pool conninfo) ends with
         ``_test`` AND matches ``--database-url``
      2) ``DALTON_TRACKER_CALENDAR_OWNER_ID`` (and ledger user) != hardcoded production owner UUID
      3) Effective target calendar ID that cleanup will delete (resolved from connection
         ``preferences.tracker_calendar_id`` or ``DALTON_GCAL_CALENDAR_ID``) is in the
         registered test sub-calendar list (``DALTON_TEST_GCAL_CALENDAR_IDS``)
  - ``prod-readonly-check`` (alias ``calendar``): verifies host ``gws`` account matches
    the owner's bound Google account and ``tracker_calendar_id == DALTON_GCAL_CALENDAR_ID``
    BEFORE any MCP call, then runs read-only ``health_check`` + 400-day EST/EDT ``list_events``
    and skips write round-trip with ``跳过：需要写权限`` under read-only grants.
  - ``drain``: polls PostgreSQL ``automation_executions`` (and host prepare locks
    ``locks/prepare_<period>.lock`` + ``locks/execution_<id>.lock`` resolved from
    the actual prepare config for ``--target mail``) until idle, exiting 2 on timeout
    and 1 if lock files cannot be read.

Never prints or logs OAuth tokens, refresh tokens, or full calendar event contents.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from src.dalton.integrations.tracker_calendar import (  # noqa: E402
    resolve_tracker_calendar_target_id,
)

# Hardcoded production tracker calendar owner UUID — NEVER overridable via CLI flags.
PROD_TRACKER_CALENDAR_OWNER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
REGISTERED_TEST_CALENDARS_ENV = "DALTON_TEST_GCAL_CALENDAR_IDS"
BRIDGE_RUNTIME_INFO_PATH = "/internal/tracker-calendar/runtime-info"
DRAIN_TIMEOUT_EXIT_CODE = 2
READONLY_SKIP_REASON = "跳过：需要写权限"
_ET = ZoneInfo("America/New_York")


class MailLockCheckError(RuntimeError):
    """Raised when mail prepare config, host service config, or lock files cannot be read or validated."""


def extract_database_name(database_url: str) -> str:
    """Extract the effective database name from a PostgreSQL URI or libpq conninfo string.

    Uses ``psycopg.conninfo.conninfo_to_dict`` so query parameters such as
    ``?dbname=...`` override the URL path exactly as libpq does.
    """
    raw = str(database_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = conninfo_to_dict(raw)
    except Exception:
        return ""
    return str(parsed.get("dbname") or "").strip()


def parse_registered_test_calendar_ids(
    explicit: Collection[str] | str | None = None,
) -> tuple[str, ...]:
    """Parse registered test sub-calendar IDs from explicit list or ``DALTON_TEST_GCAL_CALENDAR_IDS``."""
    raw_items: list[str]
    if explicit is None:
        env_val = os.getenv(REGISTERED_TEST_CALENDARS_ENV, "")
        raw_items = env_val.replace("\n", ",").replace(" ", ",").split(",")
    elif isinstance(explicit, str):
        raw_items = explicit.replace("\n", ",").replace(" ", ",").split(",")
    else:
        raw_items = [str(item) for item in explicit]

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw_items:
        val = item.strip()
        if not val or val.lower() == "primary":
            continue
        if val not in seen:
            seen.add(val)
            cleaned.append(val)
    return tuple(cleaned)


def resolve_effective_tracker_calendar_id(
    conn_row: Any | None,
    *,
    env_calendar_id: str | None = None,
) -> str:
    """Resolve the exact calendar ID that tracker cleanup will target (shared with ``tracker_calendar.py``)."""
    return resolve_tracker_calendar_target_id(conn_row, env_calendar_id=env_calendar_id)


@asynccontextmanager
async def open_probe_db_pool() -> AsyncIterator[Any]:
    """Ensure the shared psycopg AsyncConnectionPool is open for probe DB queries, closing if opened here."""
    from src.server.database.pool import get_or_create_pool

    pool = get_or_create_pool()
    opened_here = False
    if pool.closed:
        await pool.open()
        opened_here = True
    try:
        yield pool
    finally:
        if opened_here:
            await pool.close()


async def _query_actual_database_name(conn: Any = None) -> str:
    """Query ``SELECT current_database()`` on the active connection pool."""
    from src.server.database.pool import get_db_connection

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute("SELECT current_database()")
            row = await cur.fetchone()
    if not row:
        return ""
    if isinstance(row, dict):
        return str(row.get("current_database") or "").strip()
    return str(row[0] or "").strip()


def _configured_pool_database_name() -> str:
    from src.server.database.pool import get_db_connection_string

    return extract_database_name(get_db_connection_string())


def validate_non_prod_drill_guard(
    *,
    database_url: str,
    actual_db_name: str,
    owner_id: str,
    effective_calendar_id: str,
    registered_test_calendar_ids: Collection[str] | str | None = None,
    prod_calendar_id: str | None = None,
    expected_calendar_id: str | None = None,
) -> tuple[bool, list[str]]:
    """Enforce the 3 mandatory non-production guards for ``calendar-purge-drill``.

    Checks the ACTUAL connected database name, hardcoded production owner constant,
    and the EFFECTIVE calendar ID that cleanup will delete against the registered
    test sub-calendar allowlist.
    """
    reasons: list[str] = []

    # Guard 1: Both actual pool database name and --database-url must end with `_test` and match.
    url_db_name = extract_database_name(database_url)
    cleaned_actual_db = str(actual_db_name or "").strip()
    if not cleaned_actual_db or not cleaned_actual_db.endswith("_test"):
        reasons.append(
            f"actual pool database name must end with '_test' (got {cleaned_actual_db or '<empty>'!r})"
        )
    if not url_db_name or not url_db_name.endswith("_test"):
        reasons.append(
            f"--database-url database name must end with '_test' (got {url_db_name or '<empty>'!r})"
        )
    elif cleaned_actual_db and url_db_name != cleaned_actual_db:
        reasons.append(
            f"--database-url database ({url_db_name!r}) does not match actual pool database ({cleaned_actual_db!r})"
        )

    # Guard 2: Owner must be set and NEVER equal the hardcoded production owner constant.
    cleaned_owner = str(owner_id or "").strip()
    if not cleaned_owner:
        reasons.append("DALTON_TRACKER_CALENDAR_OWNER_ID must be configured for test drill")
    elif cleaned_owner.lower() == PROD_TRACKER_CALENDAR_OWNER_ID.lower():
        reasons.append(
            "DALTON_TRACKER_CALENDAR_OWNER_ID must not equal the production tracker owner"
        )

    # Guard 3: Effective target calendar ID must be in the registered test sub-calendar allowlist.
    cleaned_cal = str(effective_calendar_id or "").strip()
    cleaned_prod_cal = str(prod_calendar_id or "").strip()
    cleaned_expected_cal = str(expected_calendar_id or "").strip()
    allowed_test_cals = parse_registered_test_calendar_ids(registered_test_calendar_ids)
    allowed_lower = {c.lower() for c in allowed_test_cals}

    if not cleaned_cal:
        reasons.append("effective target calendar ID must be configured (registered test sub-calendar required)")
    elif cleaned_cal.lower() == "primary":
        reasons.append("target calendar must be a registered test sub-calendar, never 'primary'")
    elif cleaned_prod_cal and cleaned_cal.lower() == cleaned_prod_cal.lower():
        reasons.append("target calendar must not equal the production tracker calendar")
    elif not allowed_test_cals:
        reasons.append(
            f"registered test sub-calendar list is empty (configure {REGISTERED_TEST_CALENDARS_ENV})"
        )
    elif cleaned_cal.lower() not in allowed_lower:
        reasons.append(
            "effective target calendar ID is not in the registered test sub-calendar list"
        )

    if cleaned_expected_cal and cleaned_cal and cleaned_expected_cal != cleaned_cal:
        reasons.append(
            "resolved connection/env calendar ID does not match --calendar-id argument"
        )

    return (len(reasons) == 0), reasons


def _probe_flock_state(path: Path) -> bool:
    """Return True if an exclusive flock is currently held on ``path``.

    Raises ``MailLockCheckError`` if ``path`` exists (or is a symlink) and cannot be
    opened/read safely.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MailLockCheckError(f"cannot stat lock file {path.name}: {exc}") from exc

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise MailLockCheckError(f"lock path is not a regular file: {path.name}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MailLockCheckError(f"cannot read lock file {path.name}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            raise MailLockCheckError(f"cannot inspect flock on {path.name}: {exc}") from exc
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def resolve_mail_prepare_state_root(
    *,
    prepare_config_path: Path | None = None,
    state_root: Path | None = None,
) -> Path:
    """Resolve the authoritative market-digest prepare state directory from prepare config."""
    raw_cfg = (
        str(prepare_config_path).strip()
        if prepare_config_path is not None
        else os.getenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", "").strip()
    )
    raw_state = (
        str(state_root).strip()
        if state_root is not None
        else os.getenv("DALTON_MARKET_DIGEST_STATE_ROOT", "").strip()
    )

    resolved_from_cfg: Path | None = None
    if raw_cfg:
        cfg_path = Path(raw_cfg).expanduser()
        if cfg_path.is_symlink() or not cfg_path.is_file():
            raise MailLockCheckError(f"prepare config not found or invalid: {cfg_path}")
        try:
            from scripts.dalton.market_digest_inputs import load_prepare_config

            loaded = load_prepare_config(config_path=cfg_path)
            resolved_from_cfg = Path(loaded.state_root)
        except Exception:
            # Fallback to direct JSON parsing of state_root if sibling tool paths are absent
            # on a control host, still validating absolute path and rejecting symlinks/bad JSON.
            try:
                parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MailLockCheckError(f"cannot read prepare config: {exc}") from exc
            if not isinstance(parsed, dict) or not isinstance(parsed.get("state_root"), str):
                raise MailLockCheckError("prepare config is missing string 'state_root'")
            raw_sr = parsed["state_root"].strip()
            if not raw_sr or not Path(raw_sr).is_absolute():
                raise MailLockCheckError("prepare config 'state_root' must be an absolute path")
            resolved_from_cfg = Path(raw_sr)

    resolved_from_arg: Path | None = None
    if raw_state:
        sr_path = Path(raw_state).expanduser()
        if not sr_path.is_absolute():
            raise MailLockCheckError("state_root must be an absolute path")
        resolved_from_arg = sr_path

    if resolved_from_cfg is not None and resolved_from_arg is not None:
        if resolved_from_cfg.resolve() != resolved_from_arg.resolve():
            raise MailLockCheckError(
                "prepare config state_root does not match DALTON_MARKET_DIGEST_STATE_ROOT"
            )
        return resolved_from_cfg

    chosen = resolved_from_cfg or resolved_from_arg
    if chosen is None:
        raise MailLockCheckError(
            "market-digest prepare state_root is not configured "
            "(set DALTON_MARKET_DIGEST_PREPARE_CONFIG or DALTON_MARKET_DIGEST_STATE_ROOT)"
        )
    if chosen.is_symlink():
        raise MailLockCheckError("market-digest state_root must not be a symlink")
    return chosen


def _read_bridge_env_file(path: Path) -> dict[str, str]:
    """Read simple ``KEY=VALUE`` lines from a host bridge env file without following symlinks."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise MailLockCheckError(f"cannot stat bridge env file {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise MailLockCheckError(f"bridge env file is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MailLockCheckError(f"cannot read bridge env file {path}: {exc}") from exc
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def resolve_mail_research_home(
    *,
    research_home: Path | None = None,
    bridge_env_file: Path | None = None,
) -> Path:
    """Resolve ``DALTON_RESEARCH_HOME`` from actual host service config for ``worker.lock`` checks.

    Priority matches ``scripts/dalton/researchdata_bridge.py`` and
    ``scripts/dalton/ops/host_services_switch.sh``:
      1. Explicit ``--research-home``
      2. ``DALTON_RESEARCH_HOME`` from ``DALTON_BRIDGE_ENV_FILE`` (default
         ``~/.config/dalton/researchdata-bridge.env``) and/or process env
      3. Canonical host default ``~/Library/Application Support/Everflow/research``
    """
    if research_home is not None:
        explicit = Path(research_home).expanduser()
        if explicit.is_symlink():
            raise MailLockCheckError("research_home must not be a symlink")
        if not explicit.is_absolute():
            raise MailLockCheckError("research_home must be an absolute path")
        return explicit

    raw_env_file = (
        str(bridge_env_file).strip()
        if bridge_env_file is not None
        else os.getenv("DALTON_BRIDGE_ENV_FILE", "").strip()
    )
    env_file_explicit = bool(raw_env_file)
    env_path = (
        Path(raw_env_file).expanduser()
        if env_file_explicit
        else Path.home() / ".config" / "dalton" / "researchdata-bridge.env"
    )

    from_env_file: Path | None = None
    env_file_exists = False
    try:
        os.lstat(env_path)
        env_file_exists = True
    except FileNotFoundError:
        if env_file_explicit:
            raise MailLockCheckError(f"bridge env file not found: {env_path}") from None
    except OSError as exc:
        if env_file_explicit:
            raise MailLockCheckError(f"cannot access bridge env file {env_path}: {exc}") from exc

    if env_file_exists:
        parsed_env = _read_bridge_env_file(env_path)
        raw_from_file = str(parsed_env.get("DALTON_RESEARCH_HOME") or "").strip()
        if raw_from_file:
            cand = Path(raw_from_file).expanduser()
            if not cand.is_absolute():
                raise MailLockCheckError(
                    "bridge env DALTON_RESEARCH_HOME must be an absolute path"
                )
            from_env_file = cand

    raw_from_proc = os.getenv("DALTON_RESEARCH_HOME", "").strip()
    from_proc: Path | None = None
    if raw_from_proc:
        cand = Path(raw_from_proc).expanduser()
        if not cand.is_absolute():
            raise MailLockCheckError("DALTON_RESEARCH_HOME must be an absolute path")
        from_proc = cand

    if from_env_file is not None and from_proc is not None:
        if from_env_file.resolve() != from_proc.resolve():
            raise MailLockCheckError(
                "bridge env DALTON_RESEARCH_HOME does not match process DALTON_RESEARCH_HOME"
            )
        chosen = from_env_file
    else:
        chosen = (
            from_env_file
            or from_proc
            or (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
        )

    if chosen.is_symlink():
        raise MailLockCheckError("research_home must not be a symlink")
    return chosen


def check_mail_host_locks(
    state_root: Path,
    *,
    research_home: Path | None = None,
    bridge_env_file: Path | None = None,
) -> list[str]:
    """Return names of currently held mail prepare/execution and image worker lock files.

    Inspects ``<state_root>/locks/prepare_<period>.lock``,
    ``<state_root>/locks/execution_<id>.lock``, AND ALWAYS checks
    ``<research_home>/state/research-images/worker.lock``. Raises
    ``MailLockCheckError`` if any lock directory or lock file cannot be read.
    """
    if state_root.is_symlink():
        raise MailLockCheckError("state_root must not be a symlink")

    lock_dir = state_root / "locks"
    candidates: list[Path] = []
    if lock_dir.is_symlink():
        raise MailLockCheckError("locks directory must not be a symlink")
    if lock_dir.exists():
        if not lock_dir.is_dir():
            raise MailLockCheckError("locks path is not a directory")
        try:
            entries = sorted(lock_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise MailLockCheckError(f"cannot list locks directory: {exc}") from exc

        seen_paths: set[Path] = set()
        for fixed_name in ("prepare_AM.lock", "prepare_PM.lock"):
            fixed = lock_dir / fixed_name
            seen_paths.add(fixed)
            candidates.append(fixed)
        for entry in entries:
            name = entry.name
            if (name.startswith("prepare_") or name.startswith("execution_")) and name.endswith(
                ".lock"
            ):
                if entry not in seen_paths:
                    seen_paths.add(entry)
                    candidates.append(entry)

    held: list[str] = []
    for lock_path in candidates:
        if _probe_flock_state(lock_path):
            held.append(str(lock_path.relative_to(state_root)))

    effective_home = resolve_mail_research_home(
        research_home=research_home,
        bridge_env_file=bridge_env_file,
    )
    state_sub = effective_home / "state"
    images_sub = state_sub / "research-images"
    for parent_dir in (effective_home, state_sub, images_sub):
        try:
            st = os.lstat(parent_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MailLockCheckError(f"cannot inspect {parent_dir.name}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise MailLockCheckError(f"research images lock parent is not a directory: {parent_dir.name}")

    worker_lock = images_sub / "worker.lock"
    if _probe_flock_state(worker_lock):
        held.append(str(worker_lock.relative_to(effective_home)))

    return held


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


async def fetch_bridge_tracker_runtime_info(
    *,
    bridge_url: str | None = None,
    admin_token: str | None = None,
    http_client: Any = None,
    timeout_s: float = 20.0,
) -> dict[str, Any] | None:
    """Query the running researchdata bridge's ``GET /internal/tracker-calendar/runtime-info``.

    Returns the bridge's live ``host_gws_account``, ``calendar_id``, ``gcal_backend``,
    and ``research_home`` so ``prod-readonly-check`` verifies against the actual running
    bridge rather than the current shell's PATH or CLI overrides.
    """
    import httpx

    from src.dalton.settings import get_dalton_bridge_admin_token, get_dalton_bridge_base_url

    base = str(bridge_url or get_dalton_bridge_base_url() or "").strip().rstrip("/")
    token = str(admin_token or get_dalton_bridge_admin_token() or "").strip()
    if not base or not token:
        return None
    url = f"{base}{BRIDGE_RUNTIME_INFO_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Dalton-Bridge-Token": token,
    }
    timeout_cfg = httpx.Timeout(
        connect=min(5.0, timeout_s),
        read=timeout_s,
        write=timeout_s,
        pool=min(5.0, timeout_s),
    )
    try:
        if http_client is not None:
            resp = await http_client.get(url, headers=headers, timeout=timeout_cfg)
        else:
            async with httpx.AsyncClient(timeout=timeout_cfg, trust_env=False) as owned:
                resp = await owned.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        parsed = resp.json()
    except Exception:
        return None
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return None
    return parsed


def _account_matches_connection(host_account: str | None, conn_row: Any) -> bool:
    cleaned_host = str(host_account or "").strip()
    if not cleaned_host:
        return False
    owner_email = str(getattr(conn_row, "provider_email", None) or "").strip()
    owner_sub = str(getattr(conn_row, "provider_account_id", None) or "").strip()
    if owner_email and cleaned_host.lower() == owner_email.lower():
        return True
    if owner_sub and cleaned_host == owner_sub:
        return True
    return False


async def run_calendar_purge_drill(
    *,
    database_url: str,
    owner_id: str,
    calendar_id: str,
    registered_test_calendar_ids: Collection[str] | str | None = None,
    prod_calendar_id: str | None = None,
    workspace_dir: Path | None = None,
    purge_id: str | None = None,
    dry_run: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Execute Stage A isolated non-production purge drill (§7.1(1)).

    Both ``dry_run=True`` and ``dry_run=False`` query the ACTUAL connected database name
    and connection row, resolve the EFFECTIVE target calendar ID using the same function
    as ``tracker_calendar.py`` (never falling back to CLI ``--calendar-id``), and abort
    before any disconnect or purge call if any guard fails.
    """
    pool_cfg_db_name = _configured_pool_database_name()
    pre_ok, pre_reasons = validate_non_prod_drill_guard(
        database_url=database_url,
        actual_db_name=pool_cfg_db_name,
        owner_id=owner_id,
        effective_calendar_id=calendar_id,
        registered_test_calendar_ids=registered_test_calendar_ids,
        prod_calendar_id=prod_calendar_id,
    )
    if not pre_ok:
        return 1, {
            "ok": False,
            "command": "calendar-purge-drill",
            "guard_failed": True,
            "reasons": pre_reasons,
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

    async with open_probe_db_pool():
        # Verify the ACTUAL database the pool connected to (not just env/CLI strings).
        actual_db_name = await _query_actual_database_name()

        target_owner_id = str(owner_id or "").strip()
        if purge_id:
            existing_ledger = await integrations_db.get_purge_ledger(
                purge_id, decrypt_revoke_token=False
            )
            if existing_ledger is None:
                return 1, {
                    "ok": False,
                    "command": "calendar-purge-drill",
                    "guard_failed": True,
                    "reasons": [f"purge ledger {purge_id} not found"],
                }
            if existing_ledger.user_id.strip().lower() == PROD_TRACKER_CALENDAR_OWNER_ID.lower():
                return 1, {
                    "ok": False,
                    "command": "calendar-purge-drill",
                    "guard_failed": True,
                    "reasons": [
                        "purge ledger belongs to the production tracker owner"
                    ],
                }
            if existing_ledger.user_id.strip().lower() != target_owner_id.lower():
                return 1, {
                    "ok": False,
                    "command": "calendar-purge-drill",
                    "guard_failed": True,
                    "reasons": [
                        "purge ledger user_id does not match --owner-id"
                    ],
                }

        conn_row = await integrations_db.get_connection(
            target_owner_id, "google", "calendar", decrypt_tokens=False
        )
        if conn_row is None and not purge_id:
            return 1, {
                "ok": False,
                "command": "calendar-purge-drill",
                "guard_passed": False,
                "error": "test owner google/calendar connection not found in test database",
            }

        # Guard the EXACT calendar ID that the purge handler will target for deletion
        # (preferences.tracker_calendar_id, else DALTON_GCAL_CALENDAR_ID — NEVER CLI --calendar-id).
        effective_calendar_id = resolve_effective_tracker_calendar_id(conn_row)
        actual_ok, actual_reasons = validate_non_prod_drill_guard(
            database_url=database_url,
            actual_db_name=actual_db_name,
            owner_id=target_owner_id,
            effective_calendar_id=effective_calendar_id,
            registered_test_calendar_ids=registered_test_calendar_ids,
            prod_calendar_id=prod_calendar_id,
            expected_calendar_id=calendar_id,
        )
        if not actual_ok:
            return 1, {
                "ok": False,
                "command": "calendar-purge-drill",
                "guard_failed": True,
                "reasons": actual_reasons,
            }

        if dry_run:
            return 0, {
                "ok": True,
                "command": "calendar-purge-drill",
                "dry_run": True,
                "guard_passed": True,
                "database_name": actual_db_name,
                "calendar_guard_passed": True,
            }

        register_default_tracker_purge_handler()
        if workspace_dir is not None:
            os.environ["DALTON_TRACKER_WORKSPACE_DIR"] = str(workspace_dir)

        if purge_id:
            report = await run_local_purge(purge_id)
            return 0, {
                "ok": True,
                "command": "calendar-purge-drill",
                "guard_passed": True,
                "database_name": actual_db_name,
                "purge_id": report.purge_id,
                "threads_purged": len(report.threads_purged),
                "tracker_reconcile_needed": report.tracker_reconcile_needed,
            }

        created_purge_id = await begin_disconnect(
            target_owner_id,
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
            "database_name": actual_db_name,
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
    bridge_url: str | None = None,
    bridge_admin_token: str | None = None,
    bridge_runtime_resolver: Any = None,
    http_client: Any = None,
    google_client: Any = None,
    oauth_client: Any = None,
    now_et: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify production tracker owner account + calendar match the RUNNING research bridge
    BEFORE any MCP call, then run read-only ``health_check`` + 400-day EST/EDT ``list_events``.
    """
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

    async with open_probe_db_pool():
        conn_row = await integrations_db.get_connection(
            cleaned_owner, "google", "calendar", decrypt_tokens=False
        )
        if conn_row is None:
            return 1, {
                "ok": False,
                "command": "prod-readonly-check",
                "error": "owner google/calendar connection not found",
            }

        # Fetch actual host gws account and target calendar ID from the RUNNING research bridge.
        if bridge_runtime_resolver is not None:
            bridge_info = await bridge_runtime_resolver()
        else:
            bridge_info = await fetch_bridge_tracker_runtime_info(
                bridge_url=bridge_url,
                admin_token=bridge_admin_token,
                http_client=http_client,
            )

        bridge_reachable = isinstance(bridge_info, dict) and bridge_info.get("ok") is True
        host_gws_account = (
            str(bridge_info.get("host_gws_account") or "").strip()
            if bridge_reachable
            else ""
        )
        bridge_cal_id = (
            str(bridge_info.get("calendar_id") or "").strip()
            if bridge_reachable
            else ""
        )
        bridge_gcal_backend = (
            str(bridge_info.get("gcal_backend") or "").strip()
            if bridge_reachable
            else ""
        )

        # Pre-call check 1: owner's bound Google account MUST match the running bridge's gws account.
        account_matched = bridge_reachable and _account_matches_connection(
            host_gws_account, conn_row
        )

        # Pre-call check 2: owner's tracker_calendar_id MUST match the running bridge's target
        # calendar ID (and any optional CLI --calendar-id cross-check if supplied).
        prefs = dict(conn_row.preferences or {})
        configured_cal_id = str(prefs.get("tracker_calendar_id") or "").strip()
        cli_cal_id = str(expected_calendar_id or "").strip()
        calendar_id_matched = bool(
            bridge_reachable
            and configured_cal_id
            and bridge_cal_id
            and configured_cal_id == bridge_cal_id
            and (not cli_cal_id or cli_cal_id == bridge_cal_id)
        )

        if not bridge_reachable or not account_matched or not calendar_id_matched:
            mismatch_reasons: list[str] = []
            if not bridge_reachable:
                mismatch_reasons.append(
                    "running researchdata bridge runtime-info could not be queried"
                )
            else:
                if not account_matched:
                    mismatch_reasons.append(
                        "running bridge gws account does not match owner connection account"
                    )
                if not calendar_id_matched:
                    if cli_cal_id and bridge_cal_id and cli_cal_id != bridge_cal_id:
                        mismatch_reasons.append(
                            "--calendar-id does not match running bridge target calendar ID"
                        )
                    else:
                        mismatch_reasons.append(
                            "owner tracker_calendar_id does not match running bridge target calendar ID"
                        )
            return 1, {
                "ok": False,
                "command": "prod-readonly-check",
                "owner_id": cleaned_owner,
                "provider_account_bound": bool(conn_row.provider_account_id),
                "bridge_reachable": bridge_reachable,
                "account_matched": account_matched,
                "calendar_id_matched": calendar_id_matched,
                "error": "precheck_failed",
                "reasons": mismatch_reasons,
            }

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

        # 3. Scoped [PROBE-<uuid>] write round-trip (skipped honestly when read-only;
        # cross-channel gws read-back deferred to Batch 2 when write scope opens)
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

        overall_ok = (
            account_matched
            and calendar_id_matched
            and has_read
            and health_ok
            and list_ok
            and write_ok
        )
        return (0 if overall_ok else 1), {
            "ok": overall_ok,
            "command": "prod-readonly-check",
            "owner_id": cleaned_owner,
            "provider_account_bound": bool(conn_row.provider_account_id),
            "bridge_reachable": bridge_reachable,
            "bridge_gcal_backend": bridge_gcal_backend,
            "account_matched": account_matched,
            "calendar_id_matched": calendar_id_matched,
            "has_read_scope": has_read,
            "has_write_scope": has_write,
            "health_check": {
                "ok": health_ok,
                "status": health_payload.get("status"),
                "backend": (
                    health_payload["data"].get("backend")
                    if isinstance(health_payload.get("data"), dict)
                    else None
                ),
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
    prepare_config_path: Path | None = None,
    state_root: Path | None = None,
    research_home: Path | None = None,
    bridge_env_file: Path | None = None,
    count_running_fn: Any = None,
    check_locks_fn: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Poll until target automation executions (and mail prepare + image worker locks) drain, or exit 2 on timeout."""
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

    resolved_state_root: Path | None = None
    resolved_research_home: Path | None = None
    if target == "mail" and check_locks_fn is None:
        try:
            resolved_state_root = resolve_mail_prepare_state_root(
                prepare_config_path=prepare_config_path,
                state_root=state_root,
            )
            resolved_research_home = resolve_mail_research_home(
                research_home=research_home,
                bridge_env_file=bridge_env_file,
            )
        except MailLockCheckError as exc:
            return 1, {
                "ok": False,
                "command": "drain",
                "target": target,
                "drained": False,
                "error": str(exc),
            }

    counter = count_running_fn or _count_running_executions
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    sleep_s = max(0.05, float(poll_interval))
    running_count = 0
    active_locks: list[str] = []

    async def _poll_loop() -> tuple[int, dict[str, Any]]:
        nonlocal running_count, active_locks
        while True:
            running_count = int(await counter(ids))
            if target == "mail":
                try:
                    if check_locks_fn is not None:
                        active_locks = list(check_locks_fn(resolved_state_root or research_home))
                    else:
                        assert resolved_state_root is not None
                        assert resolved_research_home is not None
                        active_locks = list(
                            check_mail_host_locks(
                                resolved_state_root,
                                research_home=resolved_research_home,
                            )
                        )
                except MailLockCheckError as exc:
                    return 1, {
                        "ok": False,
                        "command": "drain",
                        "target": target,
                        "drained": False,
                        "error": str(exc),
                    }
            else:
                active_locks = []

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

    if count_running_fn is None:
        async with open_probe_db_pool():
            return await _poll_loop()
    return await _poll_loop()


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
        "--prod-calendar-id", default=os.getenv("DALTON_PROD_GCAL_CALENDAR_ID", "")
    )
    p_drill.add_argument("--workspace-dir", default=os.getenv("DALTON_TRACKER_WORKSPACE_DIR", ""))
    p_drill.add_argument("--purge-id", default="")
    p_drill.add_argument("--dry-run", action="store_true")

    for name in ("prod-readonly-check", "calendar"):
        p_prod = sub.add_parser(
            name,
            help="Verify production tracker calendar account/calendar match against running bridge and read-only connectivity.",
        )
        p_prod.add_argument(
            "--owner-id", default=os.getenv("DALTON_TRACKER_CALENDAR_OWNER_ID", "")
        )
        p_prod.add_argument("--calendar-id", default="")
        p_prod.add_argument("--bridge-url", default=os.getenv("DALTON_BRIDGE_URL", ""))

    p_drain = sub.add_parser(
        "drain",
        help="Poll until target automation executions and host locks finish (exits 2 on timeout).",
    )
    p_drain.add_argument("--target", choices=("calendar", "mail"), required=True)
    p_drain.add_argument("--wait-seconds", type=int, default=600)
    p_drain.add_argument("--poll-interval", type=float, default=5.0)
    p_drain.add_argument("--automation-id", action="append", default=[])
    p_drain.add_argument(
        "--prepare-config", default=os.getenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", "")
    )
    p_drain.add_argument(
        "--state-root", default=os.getenv("DALTON_MARKET_DIGEST_STATE_ROOT", "")
    )
    p_drain.add_argument("--research-home", default="")
    p_drain.add_argument(
        "--bridge-env-file", default=os.getenv("DALTON_BRIDGE_ENV_FILE", "")
    )

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
                prod_calendar_id=args.prod_calendar_id or None,
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
                bridge_url=args.bridge_url or None,
            )
        )
    elif args.command == "drain":
        ids = _resolve_drain_automation_ids(args.target, args.automation_id)
        prep_cfg = Path(args.prepare_config).expanduser() if args.prepare_config else None
        sr = Path(args.state_root).expanduser() if args.state_root else None
        home = Path(args.research_home).expanduser() if args.research_home else None
        benv = Path(args.bridge_env_file).expanduser() if args.bridge_env_file else None
        code, payload = asyncio.run(
            run_drain(
                target=args.target,
                automation_ids=ids,
                wait_seconds=args.wait_seconds,
                poll_interval=args.poll_interval,
                prepare_config_path=prep_cfg,
                state_root=sr,
                research_home=home,
                bridge_env_file=benv,
            )
        )
    else:
        code, payload = 1, {"ok": False, "error": f"unknown command: {args.command}"}

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
