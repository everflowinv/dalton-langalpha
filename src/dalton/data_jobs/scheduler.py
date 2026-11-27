"""Single-scheduler advisory lock, atomic claim, container lifecycle, and takeover recovery (§3.2–§3.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter
import httpx
import psycopg
from psycopg.rows import dict_row

from src.dalton.data_jobs.container_runner import (
    ContainerInspectionOutcome,
    DataTaskDockerClient,
    DataTaskPathMap,
    build_container_create_config,
    build_path_map,
    cleanup_run_secrets_dir,
    format_container_name,
    is_container_not_found_error,
    read_run_artifacts,
    read_validated_run_json_metadata,
    write_run_secrets_dir,
)
from src.dalton.data_jobs.evaluators import (
    TaskEvaluationContext,
    TaskEvaluationResult,
    TimeoutKind,
    evaluate_task_run,
)
from src.dalton.data_jobs.freeze import (
    freeze_monitor_publish_artifacts,
    freeze_task_run_artifacts,
)
from src.dalton.data_jobs.host_runner import (
    DataTaskHostBridgeClient,
    HostBridgeError,
    HostKillOutcome,
    HostRunOutcome,
)
from src.dalton.data_jobs.manifest import (
    DEFAULT_TIMEOUT_SECONDS,
    load_data_task_manifest,
    sync_manifest_to_db,
)
from src.dalton.data_jobs.notification_payloads import (
    DataTaskNotificationError,
    DataTaskNotificationPrepError,
    T27_JOB_KEY,
    is_t27_outbox_prepared,
    prepare_run_outbox_image_metadata,
    prepare_t27_run_outbox,
)
from src.dalton.data_jobs.runner_wrapper import (
    DEFAULT_WORKSPACE_DIR,
    EXIT_CODE_NO_OUTPUT_TIMEOUT,
    EXIT_CODE_TOTAL_TIMEOUT,
    atomic_write_json,
    extract_last_json_object,
    resolve_task_payload_source,
)
from src.dalton.data_jobs.safe_fs import SafeFsError
from src.dalton.notify.channel_transport import (
    extract_target_uncertain_code,
    is_uncertain_notify_error_code,
    post_data_task_gateway_notify,
)
from src.dalton.notify.dispatch import (
    NON_RETRYABLE_TERMINAL_ERROR_CODES,
    _callable_accepts_kwarg,
    _evaluate_target_results,
    _extract_retry_target_error_code,
    _invoke_mark_delivery,
    _shielded_mark_failed,
    _shielded_mark_uncertain,
    _split_target_channel,
    claim_target_delivery,
    claim_target_delivery_with_attempt,
    dispatch_target_with_payload,
    evaluate_user_notify_eligibility,
    extract_feishu_cards_from_payload,
    fetch_execution_deliveries,
    mark_target_delivery,
)
from src.dalton.settings import (
    get_configured_pm_user_ids,
    get_data_task_min_free_mem_mb,
    is_dalton_local_auth_enabled,
    is_data_jobs_enabled,
)
from src.server.database.pool import (
    _CONNECTION_KWARGS,
    get_db_connection,
    get_db_connection_string,
)
from src.server.database.sql_fences import advisory_key
from src.server.database.user_vault_secrets import get_user_secrets_decrypted

logger = logging.getLogger(__name__)

_SCHEDULER_LOCK_KEY: int = advisory_key("dalton_data_task_scheduler")

LOCK_RETRY_INTERVAL_S: float = 15.0
LOCK_HEARTBEAT_INTERVAL_S: float = 15.0
SCHEDULER_POLL_INTERVAL_S: float = 60.0

NON_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"starting", "running"})
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        "succeeded",
        "failed",
        "timed_out",
        "aborted",
        "skipped_low_memory",
        "skipped_superseded",
        "cancelled",
    }
)


class SchedulerLockLostError(RuntimeError):
    """Raised when the scheduler's session-level advisory lock connection is closed or fails ``SELECT 1``."""


class ManualTaskRunConflictError(ValueError, RuntimeError):
    """Raised when attempting to create a manual run while the task already has a queued or active run."""



class _ClaimRollback(Exception):
    """Internal control-flow exception to force transaction rollback when a claim UPDATE affects != 1 row."""

    def __init__(self, reason: str, *, unclaimable_run_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unclaimable_run_id = unclaimable_run_id


class _TerminalRollback(Exception):
    """Internal control-flow exception to force transaction rollback when terminal UPDATE affects != 1 row."""


@dataclass
class ClaimedTaskRun:
    """Metadata for a run transitioned from ``queued`` to ``starting`` in ``claim_next_queued_run``."""

    run_id: str
    job_key: str
    generation: int
    runner: str
    command_argv: list[str]
    workdir: str
    timeout_seconds: int | None
    no_output_timeout_seconds: int | None
    required_secrets: list[str]
    cos_mode: str
    notify_enabled: bool
    scheduled_slot_utc: datetime
    deadline_utc: datetime
    timezone: str = "UTC"
    host_mode: str | None = None
    trigger_source: str = "cron"
    is_trial: bool = False


def is_data_jobs_scheduler_enabled() -> bool:
    """Whether the data tasks scheduler loop should run (§3.5, Requirement 8)."""
    return is_data_jobs_enabled()


async def open_dedicated_lock_connection(
    dsn: str | None = None,
) -> psycopg.AsyncConnection[Any]:
    """Open a dedicated (non-pooled) Postgres connection in autocommit mode for the session advisory lock (§3.2.1)."""
    conn_str = dsn or get_db_connection_string()
    return await psycopg.AsyncConnection.connect(
        conn_str,
        autocommit=True,
        prepare_threshold=None,
        **_CONNECTION_KWARGS,
    )


async def release_scheduler_lock(lock_conn: Any, *, close_conn: bool = True) -> None:
    """Explicitly release ``pg_advisory_unlock`` and close the dedicated lock connection on loop exit (§3.2.1)."""
    if lock_conn is None:
        return
    if not bool(getattr(lock_conn, "closed", False)):
        try:
            async with lock_conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (_SCHEDULER_LOCK_KEY,))
                await cur.fetchone()
        except Exception:
            pass
        if close_conn and hasattr(lock_conn, "close"):
            try:
                await lock_conn.close()
            except Exception:
                pass


async def try_scheduler_lock(lock_conn: Any) -> bool:
    """Attempt to acquire the session-level advisory lock on ``lock_conn`` (§3.2.1)."""
    async with lock_conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_SCHEDULER_LOCK_KEY,))
        row = await cur.fetchone()
    if row is None:
        return False
    if isinstance(row, Mapping):
        return bool(next(iter(row.values()), False))
    return bool(row[0])


async def check_scheduler_lock(lock_conn: Any) -> None:
    """Verify ``lock_conn`` is still alive (`SELECT 1`) before any external or state-changing action (§3.2.1)."""
    if lock_conn is None or bool(getattr(lock_conn, "closed", False)):
        raise SchedulerLockLostError("Scheduler lock connection is closed")
    try:
        async with lock_conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
            if row is None:
                raise SchedulerLockLostError("SELECT 1 on lock_conn returned no row")
    except SchedulerLockLostError:
        raise
    except Exception as exc:
        raise SchedulerLockLostError(f"Scheduler lock check failed: {exc}") from exc


async def resolve_task_vault_secrets(secret_names: Sequence[str]) -> dict[str, str]:
    """Fetch decrypted Vault secrets for ``secret_names`` from the primary PM admin."""
    names = [str(s).strip() for s in secret_names if str(s).strip()]
    if not names:
        return {}
    if is_dalton_local_auth_enabled():
        from src.dalton.access_tiers import get_primary_admin_user_id

        pm_user_id = str((await get_primary_admin_user_id()) or "").strip()
    else:
        pm_ids = get_configured_pm_user_ids()
        pm_user_id = str(pm_ids[0]).strip() if pm_ids else ""
    if not pm_user_id:
        logger.warning("No primary administrator configured; skipping Vault secret fetch for %s", names)
        return {}
    return await get_user_secrets_decrypted(pm_user_id, names=names)


def read_host_available_memory_mb(meminfo_path: Path | str = "/proc/meminfo") -> int:
    """Read ``MemAvailable`` (in MiB) from ``/proc/meminfo`` (§2.2.2)."""
    path = Path(meminfo_path)
    if not path.is_file():
        return 65536
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    kb = int(parts[1])
                    return kb // 1024
    except Exception:
        pass
    return 65536


def _extract_field(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _locked_def_matches_slot(locked: Any, slot_utc: datetime) -> bool:
    """True when the locked definition is still due for this cron slot.

    The candidate scan does not lock the row. The enqueue transaction locks it
    and skips the insert when a pause or schedule change committed after the scan.
    """
    if locked is None:
        return False
    if not bool(_extract_field(locked, "enabled", 0)):
        return False
    if bool(_extract_field(locked, "paused_by_user", 1)):
        return False
    runner = str(_extract_field(locked, "runner", 5) or "")
    if runner not in ("container", "host"):
        return False
    if _extract_field(locked, "deleted_at", 6) is not None:
        return False
    cron_expr = str(_extract_field(locked, "cron_expr", 2) or "")
    tz_name = str(_extract_field(locked, "timezone", 3) or "")
    try:
        tz = ZoneInfo(tz_name)
        return bool(croniter.match(cron_expr, slot_utc.astimezone(tz)))
    except Exception:
        logger.error(
            "Skipping data task enqueue; locked schedule is not usable cron=%r tz=%r",
            cron_expr,
            tz_name,
        )
        return False


async def enqueue_due_cron_tasks(
    conn: Any,
    *,
    now_utc: datetime | None = None,
) -> list[str]:
    """Scan enabled container and host task defs and enqueue due cron runs, superseding stale queued rows (§2.2.3.A, §3.2.2)."""
    ref_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    slot_utc = ref_utc.replace(second=0, microsecond=0)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT job_key, cron_expr, timezone, timeout_seconds, enabled, paused_by_user, runner
            FROM dalton_data_task_defs
            WHERE enabled = TRUE
              AND paused_by_user = FALSE
              AND deleted_at IS NULL
              AND runner IN ('container', 'host')
            ORDER BY job_key ASC
            """
        )
        defs = list(await cur.fetchall() or [])

    enqueued_run_ids: list[str] = []
    for task_def in defs:
        runner = str(task_def.get("runner") or "container")
        if runner not in ("container", "host"):
            continue
        job_key = str(task_def["job_key"])
        cron_expr = str(task_def["cron_expr"])
        tz_name = str(task_def["timezone"])

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.error("Invalid timezone %r on data task %s", tz_name, job_key)
            continue

        local_slot = slot_utc.astimezone(tz)
        if not croniter.match(cron_expr, local_slot):
            continue

        new_run_id = str(uuid4())

        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Lock the definition before any run update so this cannot deadlock
                # with a pause or schedule write, which locks the definition first.
                await cur.execute(
                    """
                    SELECT enabled, paused_by_user, cron_expr, timezone,
                           timeout_seconds, runner, deleted_at
                    FROM dalton_data_task_defs
                    WHERE job_key = %(job_key)s
                    FOR UPDATE
                    """,
                    {"job_key": job_key},
                )
                locked = await cur.fetchone()
                if not _locked_def_matches_slot(locked, slot_utc):
                    continue
                locked_timeout = _extract_field(locked, "timeout_seconds", 4)
                effective_timeout = (
                    int(locked_timeout)
                    if locked_timeout is not None
                    else DEFAULT_TIMEOUT_SECONDS
                )
                deadline_utc = slot_utc + timedelta(seconds=effective_timeout)
                # §2.2.3.A & §3.2.2: if a prior period's queued cron row has not started yet,
                # mark it skipped_superseded (does NOT increment consecutive_failures)
                # before inserting the new cycle's queued row.
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status = 'skipped_superseded',
                        failure_reason = 'superseded_by_newer_cron_slot',
                        notify_status = 'skipped',
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE job_key = %(job_key)s
                      AND trigger_source = 'cron'
                      AND status = 'queued'
                      AND scheduled_slot_utc < %(slot_utc)s
                    """,
                    {"job_key": job_key, "slot_utc": slot_utc},
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id,
                        job_key,
                        trigger_source,
                        scheduled_slot_utc,
                        deadline_utc,
                        status,
                        created_at,
                        updated_at
                    ) VALUES (
                        %(run_id)s::uuid,
                        %(job_key)s,
                        'cron',
                        %(slot_utc)s,
                        %(deadline_utc)s,
                        'queued',
                        NOW(),
                        NOW()
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING run_id
                    """,
                    {
                        "run_id": new_run_id,
                        "job_key": job_key,
                        "slot_utc": slot_utc,
                        "deadline_utc": deadline_utc,
                    },
                )
                inserted = await cur.fetchone()
                if inserted is not None:
                    enqueued_run_ids.append(str(_extract_field(inserted, "run_id", 0)))

    return enqueued_run_ids


def _normalize_manual_job_key(job_key: str) -> str:
    cleaned = str(job_key or "").strip()
    if cleaned.lower().startswith("data:"):
        cleaned = cleaned[5:].strip()
    if not cleaned:
        raise ValueError("job_key must not be empty")
    by_code = load_data_task_manifest().by_task_code()
    if cleaned.upper() in by_code:
        return by_code[cleaned.upper()].job_key
    return cleaned


async def enqueue_manual_task_run(
    conn: Any,
    job_key: str,
    *,
    triggered_by_user_id: str | None = None,
    now_utc: datetime | None = None,
) -> str:
    """Insert a ``trigger_source = 'manual'`` queued trial run for a disabled task and return ``run_id``.

    Only allows tasks where ``enabled = FALSE`` (for enabled tasks, raises
    ``ManualTaskRunConflictError`` directing the caller to use the in-chat immediate run).
    Also rejects creation with ``ManualTaskRunConflictError`` when the task already has a
    queued or active (`queued`, `starting`, `running`) run from either `cron` or `manual`.
    """
    resolved_job_key = _normalize_manual_job_key(job_key)
    slot_utc = (
        now_utc.astimezone(timezone.utc)
        if now_utc is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )
    new_run_id = str(uuid4())

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT job_key, timeout_seconds, runner, enabled, deleted_at
                FROM dalton_data_task_defs
                WHERE job_key = %(job_key)s
                FOR UPDATE
                """,
                {"job_key": resolved_job_key},
            )
            def_row = await cur.fetchone()
            if def_row is None:
                raise ValueError(f"Unknown data task job_key: {resolved_job_key}")
            if _extract_field(def_row, "deleted_at", 4) is not None:
                raise ManualTaskRunConflictError(
                    f"任务已删除，不能手动运行 (job_key={resolved_job_key})"
                )

            enabled_raw = _extract_field(def_row, "enabled", 3)
            if bool(enabled_raw):
                raise ManualTaskRunConflictError(
                    f"任务已开启，请用对话里的立即运行 (job_key={resolved_job_key})"
                )

            timeout_s = _extract_field(def_row, "timeout_seconds", 1)
            runner = str(_extract_field(def_row, "runner", 2) or "container")
            if runner not in ("container", "host"):
                raise ValueError(f"Unsupported runner {runner!r} for data task {resolved_job_key}")

            await cur.execute(
                """
                SELECT run_id, status, trigger_source
                FROM dalton_data_task_runs
                WHERE job_key = %(job_key)s
                  AND status IN ('queued', 'starting', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"job_key": resolved_job_key},
            )
            existing = await cur.fetchone()
            if existing is not None:
                existing_run_id = str(_extract_field(existing, "run_id", 0))
                existing_status = str(_extract_field(existing, "status", 1))
                existing_source = str(_extract_field(existing, "trigger_source", 2) or "")
                raise ManualTaskRunConflictError(
                    f"Data task {resolved_job_key!r} already has a {existing_status} run "
                    f"(run_id={existing_run_id}, trigger_source={existing_source})"
                )

            effective_timeout = (
                int(timeout_s) if timeout_s is not None else DEFAULT_TIMEOUT_SECONDS
            )
            deadline_utc = slot_utc + timedelta(seconds=effective_timeout)

            await cur.execute(
                """
                INSERT INTO dalton_data_task_runs (
                    run_id,
                    job_key,
                    trigger_source,
                    triggered_by_user_id,
                    scheduled_slot_utc,
                    deadline_utc,
                    status,
                    created_at,
                    updated_at
                ) VALUES (
                    %(run_id)s::uuid,
                    %(job_key)s,
                    'manual',
                    %(triggered_by_user_id)s,
                    %(slot_utc)s,
                    %(deadline_utc)s,
                    'queued',
                    NOW(),
                    NOW()
                )
                ON CONFLICT DO NOTHING
                RETURNING run_id
                """,
                {
                    "run_id": new_run_id,
                    "job_key": resolved_job_key,
                    "triggered_by_user_id": triggered_by_user_id,
                    "slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                },
            )
            inserted = await cur.fetchone()
            if inserted is None:
                raise ManualTaskRunConflictError(
                    f"Data task {resolved_job_key!r} already has a queued run"
                )
            return str(_extract_field(inserted, "run_id", 0))



def _manual_trial_from_locked_def(locked: Any, trigger_source: str) -> bool:
    """A manual run of a disabled task is a trial and does not count as a failure.

    ``enabled`` comes from the definition row locked in this transaction. A
    missing definition is not a trial; there is no failure counter to update.
    """
    if locked is None:
        return False
    enabled = bool(_extract_field(locked, "enabled", 0))
    return trigger_source == "manual" and not enabled


async def _skip_queued_run_for_low_memory(
    conn: Any,
    *,
    run_id: str,
    job_key: str,
    now_utc: datetime,
    require_low_mem_since: bool,
    stamp_low_mem_since: bool,
) -> bool:
    """Skip one queued run that is still past its low-memory deadline.

    The definition lock is the first statement, before the run row changes.
    Pause and schedule writes lock that definition and then cancel queued runs.
    Locking the run first deadlocks those writes. After the lock, the run is
    updated only when it is still the same queued row and still past
    ``deadline_utc``. A pause or schedule change that already cancelled it
    leaves the row and the failure counter untouched. A trial does not
    increment ``consecutive_failures``. The update matches one ``run_id``.
    """
    low_mem_assign = ""
    if stamp_low_mem_since:
        low_mem_assign = (
            "low_mem_since_utc = COALESCE(low_mem_since_utc, %(now_utc)s),"
        )
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT enabled
                FROM dalton_data_task_defs
                WHERE job_key = %(job_key)s
                FOR UPDATE
                """,
                {"job_key": job_key},
            )
            locked = await cur.fetchone()
            await cur.execute(
                f"""
                UPDATE dalton_data_task_runs
                SET status = 'skipped_low_memory',
                    {low_mem_assign}
                    failure_reason = 'low_memory_deadline_exceeded',
                    notify_status = 'skipped',
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE run_id = %(run_id)s::uuid
                  AND job_key = %(job_key)s
                  AND status = 'queued'
                  AND deadline_utc <= %(now_utc)s
                  AND (
                        %(require_low_mem_since)s = FALSE
                        OR low_mem_since_utc IS NOT NULL
                  )
                RETURNING trigger_source
                """,
                {
                    "run_id": run_id,
                    "job_key": job_key,
                    "now_utc": now_utc,
                    "require_low_mem_since": require_low_mem_since,
                },
            )
            skipped = await cur.fetchone()
            if skipped is None or cur.rowcount != 1:
                return False
            trigger_source = str(
                _extract_field(skipped, "trigger_source", 0) or "cron"
            )
            if locked is not None and not _manual_trial_from_locked_def(
                locked, trigger_source
            ):
                await cur.execute(
                    """
                    UPDATE dalton_data_task_defs
                    SET consecutive_failures = consecutive_failures + 1,
                        updated_at = NOW()
                    WHERE job_key = %(job_key)s
                    """,
                    {"job_key": job_key},
                )
            return True


async def expire_low_memory_runs(
    conn: Any,
    *,
    now_utc: datetime | None = None,
    only_run_id: str | None = None,
) -> list[str]:
    """Transition ``queued`` runs with ``low_mem_since_utc IS NOT NULL`` past ``deadline_utc`` to ``skipped_low_memory`` (§2.2.3.B)."""
    ref_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expired_ids: list[str] = []

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT r.run_id, r.job_key
            FROM dalton_data_task_runs r
            WHERE r.status = 'queued'
              AND r.low_mem_since_utc IS NOT NULL
              AND r.deadline_utc <= %(now_utc)s
            ORDER BY r.scheduled_slot_utc ASC, r.created_at ASC
            """,
            {"now_utc": ref_utc},
        )
        rows = list(await cur.fetchall() or [])

    for row in rows:
        run_id = str(_extract_field(row, "run_id", 0))
        if only_run_id is not None and run_id != str(only_run_id):
            continue
        job_key = str(_extract_field(row, "job_key", 1))
        skipped = await _skip_queued_run_for_low_memory(
            conn,
            run_id=run_id,
            job_key=job_key,
            now_utc=ref_utc,
            require_low_mem_since=True,
            stamp_low_mem_since=False,
        )
        if skipped:
            expired_ids.append(run_id)
    return expired_ids


async def claim_next_queued_run(
    conn: Any,
    *,
    now_utc: datetime | None = None,
    available_memory_mb: int | None = None,
    min_free_mem_mb: int | None = None,
    only_run_id: str | None = None,
) -> ClaimedTaskRun | None:
    """Clean unclaimable head rows and atomically claim the first eligible queued run (§2.2.3, §3.2.3).

    Guarantees:
    - When ``dalton_data_task_slot.slot_state != 'idle'`` (`'running'` or `'reclaiming'`), returns
      ``None`` immediately; queued runs wait normally and are never marked ``skipped_low_memory``.
    - When ``slot_state == 'idle'``, expires any low-memory-blocked runs whose ``deadline_utc``
      has passed, then scans ``status = 'queued'`` in ``scheduled_slot_utc ASC, created_at ASC`` order
      (or only ``only_run_id`` when specified, leaving earlier queued runs untouched):
      - Unclaimable head rows (``enabled = FALSE``, ``paused_by_user = TRUE``, or ``runner != 'container'``
        before PR 0-2) are marked ``status = 'cancelled'`` and the loop continues to the next queued row.
      - For ``runner = 'container'``, checks ``MemAvailable >= min_free_mem_mb`` (default 1280 MB);
        if below threshold, sets ``low_mem_since_utc = COALESCE(low_mem_since_utc, NOW())`` (or
        transitions to ``skipped_low_memory`` if already past ``deadline_utc``) and stops claiming.
      - Executes ``SELECT ... FOR SHARE`` on ``dalton_data_task_defs`` and the two claim ``UPDATE``
        statements inside a single transaction; both ``UPDATE``s must affect
        exactly 1 row or the transaction rolls back.
    """
    ref_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT slot_id, generation, slot_state, active_run_id, active_job_key
            FROM dalton_data_task_slot
            WHERE slot_id = 1
            """
        )
        slot_row = await cur.fetchone()

    if slot_row is None or str(_extract_field(slot_row, "slot_state", 2)) != "idle":
        return None

    await expire_low_memory_runs(conn, now_utc=ref_utc, only_run_id=only_run_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                r.run_id,
                r.job_key,
                r.trigger_source,
                r.triggered_by_user_id,
                r.scheduled_slot_utc,
                r.deadline_utc,
                r.low_mem_since_utc,
                d.enabled,
                d.paused_by_user,
                d.runner,
                d.command_argv,
                d.workdir,
                d.timeout_seconds,
                d.no_output_timeout_seconds,
                d.required_secrets,
                d.cos_mode,
                d.notify_enabled,
                d.timezone,
                d.deleted_at
            FROM dalton_data_task_runs r
            LEFT JOIN dalton_data_task_defs d ON d.job_key = r.job_key
            WHERE r.status = 'queued'
            ORDER BY r.scheduled_slot_utc ASC, r.created_at ASC
            """
        )
        candidates = list(await cur.fetchall() or [])

    if not candidates:
        return None

    threshold_mb = (
        int(min_free_mem_mb) if min_free_mem_mb is not None else get_data_task_min_free_mem_mb()
    )

    for cand in candidates:
        run_id = str(cand["run_id"])
        if only_run_id is not None and run_id != str(only_run_id):
            continue
        job_key = str(cand["job_key"])
        trigger_source = str(cand.get("trigger_source") or "cron")
        if cand.get("deleted_at") is not None:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                        SET status = 'cancelled',
                            failure_reason = 'task_deleted',
                            notify_status = 'skipped',
                            completed_at = NOW(),
                            updated_at = NOW()
                        WHERE run_id = %(run_id)s::uuid
                          AND status = 'queued'
                        """,
                        {"run_id": run_id},
                    )
            continue
        enabled = bool(cand.get("enabled"))
        paused = bool(cand.get("paused_by_user"))
        is_trial = trigger_source == "manual" and not enabled
        runner = str(cand.get("runner") or "container")
        deadline_utc = cand["deadline_utc"]
        triggered_by_user_id = str(cand.get("triggered_by_user_id") or "")
        is_takeover_retry = triggered_by_user_id == TAKEOVER_RETRY_TRIGGERED_BY

        # Claim-time deadline enforcement for takeover retries: never start a same-slot
        # rerun once its slot deadline has passed (even if it was queued before expiry).
        if is_takeover_retry:
            cand_deadline_utc = _coerce_utc_datetime(deadline_utc)
            claim_check_now = (
                now_utc.astimezone(timezone.utc)
                if now_utc is not None
                else datetime.now(timezone.utc)
            )
            if cand_deadline_utc is not None and claim_check_now >= cand_deadline_utc:
                await _cancel_and_publish_expired_takeover_retry(
                    conn,
                    run_id=run_id,
                    job_key=job_key,
                    scheduled_slot_utc=_coerce_utc_datetime(cand.get("scheduled_slot_utc")),
                    deadline_utc=cand_deadline_utc,
                )
                continue

        # Unclaimable head row (disabled/paused for non-trial runs, missing def, or unsupported runner):
        # cancel and continue scanning the next queued row (§3.2.3). Trial runs (`trigger_source == 'manual'`
        # while `enabled == False`) ignore `enabled` and `paused_by_user` so disabled tasks can be trial-run.
        if (
            (not is_trial and (not enabled or paused))
            or cand.get("command_argv") is None
            or runner not in ("container", "host")
        ):
            cancel_reason = (
                "unsupported_runner"
                if runner not in ("container", "host")
                else "task_disabled_before_claim"
            )
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                        SET status = 'cancelled',
                            failure_reason = %(cancel_reason)s,
                            notify_status = 'skipped',
                            completed_at = NOW(),
                            updated_at = NOW()
                        WHERE run_id = %(run_id)s::uuid
                          AND status = 'queued'
                        """,
                        {"run_id": run_id, "cancel_reason": cancel_reason},
                    )
            continue

        # Memory admission check for container tasks when slot is idle (§2.2.2, §2.2.3.B, §3.2.3).
        if runner == "container":
            avail_mb = (
                int(available_memory_mb)
                if available_memory_mb is not None
                else read_host_available_memory_mb()
            )
            if avail_mb < threshold_mb:
                if isinstance(deadline_utc, datetime) and ref_utc >= deadline_utc:
                    # Same definition-then-run order as expire_low_memory_runs.
                    # The locked enabled flag, not the earlier scan, decides the trial.
                    await _skip_queued_run_for_low_memory(
                        conn,
                        run_id=run_id,
                        job_key=job_key,
                        now_utc=ref_utc,
                        require_low_mem_since=False,
                        stamp_low_mem_since=True,
                    )
                    continue

                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_runs
                            SET low_mem_since_utc = COALESCE(low_mem_since_utc, %(now_utc)s),
                                updated_at = NOW()
                            WHERE run_id = %(run_id)s::uuid
                              AND status = 'queued'
                            """,
                            {"run_id": run_id, "now_utc": ref_utc},
                        )
                return None

        # Single-transaction atomic claim: lock task def FOR SHARE, update slot, and update run (§3.2.3).
        # 不变式：认领事务通过 FOR SHARE 锁定任务定义行，且试跑进行中禁止开启任务，保证试跑从入队到终态 enabled 恒为 FALSE。
        claimed_generation: int | None = None
        try:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_slot
                        SET generation     = generation + 1,
                            slot_state     = 'running',
                            active_run_id  = %(run_id)s::uuid,
                            active_job_key = %(job_key)s,
                            started_at     = NOW(),
                            updated_at     = NOW()
                        WHERE slot_id = 1 AND slot_state = 'idle'
                        RETURNING generation
                        """,
                        {"run_id": run_id, "job_key": job_key},
                    )
                    if cur.rowcount != 1:
                        raise _ClaimRollback("slot_not_idle")
                    gen_row = await cur.fetchone()
                    if gen_row is None:
                        raise _ClaimRollback("slot_returning_empty")
                    claimed_generation = int(_extract_field(gen_row, "generation", 0))

                    await cur.execute(
                        """
                        SELECT enabled, paused_by_user, deleted_at
                        FROM dalton_data_task_defs
                        WHERE job_key = %(job_key)s
                        FOR SHARE
                        """,
                        {"job_key": job_key},
                    )
                    locked_def = await cur.fetchone()
                    if locked_def is None:
                        raise _ClaimRollback(
                            "run_not_claimable",
                            unclaimable_run_id=run_id,
                        )
                    if _extract_field(locked_def, "deleted_at", 2) is not None:
                        raise _ClaimRollback(
                            "task_deleted",
                            unclaimable_run_id=run_id,
                        )
                    locked_enabled = bool(_extract_field(locked_def, "enabled", 0))
                    locked_paused = bool(_extract_field(locked_def, "paused_by_user", 1))
                    is_trial = trigger_source == "manual" and not locked_enabled

                    claim_now_utc = (
                        now_utc.astimezone(timezone.utc)
                        if now_utc is not None
                        else datetime.now(timezone.utc)
                    )
                    deadline_guard_sql = (
                        "%(claim_now_utc)s < r.deadline_utc"
                        if now_utc is not None
                        else "NOW() < r.deadline_utc"
                    )
                    await cur.execute(
                        f"""
                        UPDATE dalton_data_task_runs r
                        SET status     = 'starting',
                            generation = %(generation)s,
                            updated_at = NOW()
                        FROM dalton_data_task_defs d
                        WHERE r.run_id = %(run_id)s::uuid
                          AND r.status = 'queued'
                          AND d.job_key = r.job_key
                          AND d.deleted_at IS NULL
                          AND d.enabled = %(expected_enabled)s
                          AND d.paused_by_user = %(expected_paused)s
                          AND (
                              (r.trigger_source = 'manual' AND %(expected_enabled)s = FALSE)
                              OR (%(expected_enabled)s = TRUE AND %(expected_paused)s = FALSE)
                          )
                          AND (
                              COALESCE(r.triggered_by_user_id, '') <> %(takeover_retry_user)s
                              OR r.deadline_utc IS NULL
                              OR {deadline_guard_sql}
                          )
                          AND d.runner IN ('container', 'host')
                        """,
                        {
                            "run_id": run_id,
                            "generation": claimed_generation,
                            "expected_enabled": locked_enabled,
                            "expected_paused": locked_paused,
                            "takeover_retry_user": TAKEOVER_RETRY_TRIGGERED_BY,
                            "claim_now_utc": claim_now_utc,
                        },
                    )
                    if cur.rowcount != 1:
                        cand_dl = _coerce_utc_datetime(deadline_utc)
                        expired_at_claim = is_takeover_retry and cand_dl is not None and (
                            (now_utc is not None and claim_now_utc >= cand_dl)
                            or (now_utc is None and datetime.now(timezone.utc) >= cand_dl)
                        )
                        raise _ClaimRollback(
                            "takeover_retry_expired" if expired_at_claim else "run_not_claimable",
                            unclaimable_run_id=run_id,
                        )
        except _ClaimRollback as rb:
            if rb.reason == "slot_not_idle" or rb.reason == "slot_returning_empty":
                return None
            if rb.unclaimable_run_id:
                if rb.reason == "takeover_retry_expired":
                    await _cancel_and_publish_expired_takeover_retry(
                        conn,
                        run_id=rb.unclaimable_run_id,
                        job_key=job_key,
                        scheduled_slot_utc=_coerce_utc_datetime(cand.get("scheduled_slot_utc")),
                        deadline_utc=_coerce_utc_datetime(deadline_utc),
                    )
                else:
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_runs
                                SET status = 'cancelled',
                                    failure_reason = %(failure_reason)s,
                                    notify_status = 'skipped',
                                    completed_at = NOW(),
                                    updated_at = NOW()
                                WHERE run_id = %(run_id)s::uuid
                                  AND status = 'queued'
                                """,
                                {
                                    "run_id": rb.unclaimable_run_id,
                                    "failure_reason": (
                                        "task_deleted"
                                        if rb.reason == "task_deleted"
                                        else "task_disabled_before_claim"
                                    ),
                                },
                            )
            continue

        raw_argv = cand["command_argv"]
        if isinstance(raw_argv, str):
            raw_argv = json.loads(raw_argv)
        raw_secrets = cand.get("required_secrets") or []
        if isinstance(raw_secrets, str):
            raw_secrets = json.loads(raw_secrets)

        manifest_entry = load_data_task_manifest().by_job_key().get(job_key)
        if runner == "host":
            if is_trial:
                host_mode: str | None = "run"
            elif manifest_entry is not None:
                host_mode = str(manifest_entry.host_mode or "mirror")
            else:
                host_mode = "mirror"
        else:
            host_mode = None

        return ClaimedTaskRun(
            run_id=run_id,
            job_key=job_key,
            generation=int(claimed_generation),
            runner=runner,
            command_argv=[str(a) for a in raw_argv],
            workdir=str(cand.get("workdir") or DEFAULT_WORKSPACE_DIR),
            timeout_seconds=cand.get("timeout_seconds"),
            no_output_timeout_seconds=cand.get("no_output_timeout_seconds"),
            required_secrets=[str(s) for s in raw_secrets],
            cos_mode=str(cand.get("cos_mode") or "skip"),
            notify_enabled=bool(cand.get("notify_enabled")),
            scheduled_slot_utc=cand["scheduled_slot_utc"],
            deadline_utc=cand["deadline_utc"],
            timezone=str(cand.get("timezone") or "UTC"),
            host_mode=host_mode,
            trigger_source=trigger_source,
            is_trial=is_trial,
        )

    return None


async def start_claimed_container_run(
    conn: Any,
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient,
    claimed: ClaimedTaskRun,
    decrypted_secrets: Mapping[str, str] | None = None,
    path_map: DataTaskPathMap | None = None,
) -> str | None:
    """Create and start a task container with lock checks and ``status = 'starting'`` guard (§3.2.4).

    Steps:
    1. Fetch decrypted Vault secrets from primary PM admin (`DALTON_PM_USER_IDS[0]`) when needed,
       and write `/app/var/dalton/run-secrets/<run_id>`;
    2. Verify lock (`SELECT 1` on `lock_conn`), then call `docker create`;
    3. Verify lock (`SELECT 1` on `lock_conn`) BEFORE the DB update (if lock is lost, stop immediately
       without any Docker cleanup); then execute `UPDATE dalton_data_task_runs ... WHERE run_id = :run_id::uuid
       AND generation = :generation AND status = 'starting'`;
       If `rowcount != 1`, verify lock, delete the created container (`docker rm -f`), clean up secrets dir,
       and in a single transaction mark the run `aborted` and release the slot back to `idle` without calling
       `docker start`;
    4. Verify lock (`SELECT 1` on `lock_conn`), then call `docker start` (if lock check fails, stop immediately
       without any Docker call).
    """
    paths = path_map or build_path_map()
    effective_secrets = decrypted_secrets
    if effective_secrets is None and claimed.required_secrets:
        effective_secrets = await resolve_task_vault_secrets(claimed.required_secrets)
    write_run_secrets_dir(claimed.run_id, effective_secrets, path_map=paths)
    container_name = format_container_name(claimed.job_key, claimed.run_id)
    effective_notify_enabled = False if claimed.is_trial else claimed.notify_enabled
    create_cfg = build_container_create_config(
        job_key=claimed.job_key,
        run_id=claimed.run_id,
        generation=claimed.generation,
        command_argv=claimed.command_argv,
        workdir=claimed.workdir,
        timeout_seconds=claimed.timeout_seconds,
        no_output_timeout_seconds=claimed.no_output_timeout_seconds,
        notify_enabled=effective_notify_enabled,
        cos_mode=claimed.cos_mode,
        path_map=paths,
    )

    try:
        await check_scheduler_lock(lock_conn)
    except Exception:
        cleanup_run_secrets_dir(claimed.run_id, path_map=paths)
        raise

    container_id = await docker_client.create_container(config=create_cfg, name=container_name)

    # Lock check before DB write: if lock is lost, do NOT touch Docker (leave cleanup to new scheduler).
    await check_scheduler_lock(lock_conn)

    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status         = 'running',
                        container_id   = %(container_id)s,
                        container_name = %(container_name)s,
                        started_at     = NOW(),
                        updated_at     = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND generation = %(generation)s
                      AND status = 'starting'
                    """,
                    {
                        "run_id": claimed.run_id,
                        "generation": claimed.generation,
                        "container_id": container_id,
                        "container_name": container_name,
                    },
                )
                updated_rows = cur.rowcount
    except SchedulerLockLostError:
        raise
    except Exception:
        await check_scheduler_lock(lock_conn)
        try:
            await docker_client.remove_container(container_id, force=True)
        except Exception:
            pass
        cleanup_run_secrets_dir(claimed.run_id, path_map=paths)
        raise

    if updated_rows != 1:
        await check_scheduler_lock(lock_conn)
        try:
            await docker_client.remove_container(container_id, force=True)
        except Exception:
            await check_scheduler_lock(lock_conn)
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_slot
                        SET slot_state = 'reclaiming',
                            updated_at = NOW()
                        WHERE slot_id = 1
                          AND active_run_id = %(run_id)s::uuid
                        """,
                        {"run_id": claimed.run_id},
                    )
            return None

        cleanup_run_secrets_dir(claimed.run_id, path_map=paths)
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status         = 'aborted',
                        failure_reason = 'startup_update_affected_zero_rows',
                        notify_status  = 'skipped',
                        completed_at   = NOW(),
                        updated_at     = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND status IN ('queued', 'starting', 'running')
                    """,
                    {"run_id": claimed.run_id},
                )
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state     = 'idle',
                        active_run_id  = NULL,
                        active_job_key = NULL,
                        started_at     = NULL,
                        updated_at     = NOW()
                    WHERE slot_id = 1
                      AND active_run_id = %(run_id)s::uuid
                    """,
                    {"run_id": claimed.run_id},
                )
        return None

    # Lock check before starting container: if lock is lost, stop immediately without Docker calls.
    await check_scheduler_lock(lock_conn)
    await docker_client.start_container(container_id)
    return container_id


async def start_claimed_host_run(
    conn: Any,
    *,
    lock_conn: Any,
    claimed: ClaimedTaskRun,
) -> bool:
    """Transition a ``runner = 'host'`` run from ``starting`` to ``running`` with pre-write lock check (§3.2.4)."""
    await check_scheduler_lock(lock_conn)
    updated_rows = 0
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                SET status     = 'running',
                     started_at = NOW(),
                     updated_at = NOW()
                WHERE run_id = %(run_id)s::uuid
                  AND generation = %(generation)s
                  AND status = 'starting'
                """,
                {
                    "run_id": claimed.run_id,
                    "generation": claimed.generation,
                },
            )
            updated_rows = int(cur.rowcount or 0)
    if updated_rows != 1:
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status         = 'aborted',
                        failure_reason = 'start_run_row_mismatch',
                        notify_status  = 'skipped',
                        completed_at   = NOW(),
                        updated_at     = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND status IN ('queued', 'starting', 'running')
                    """,
                    {"run_id": claimed.run_id},
                )
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state     = 'idle',
                        active_run_id  = NULL,
                        active_job_key = NULL,
                        started_at     = NULL,
                        updated_at     = NOW()
                    WHERE slot_id = 1
                      AND active_run_id = %(run_id)s::uuid
                    """,
                    {"run_id": claimed.run_id},
                )
        return False
    await check_scheduler_lock(lock_conn)
    return True


async def bump_slot_generation_and_kill_host_task(
    conn: Any,
    *,
    lock_conn: Any,
    host_client: DataTaskHostBridgeClient,
    run_id: str | int | None = None,
    reason: str | None = None,
) -> tuple[int, HostKillOutcome]:
    """Increment `dalton_data_task_slot.generation` in a transaction (after lock check), then call `host-kill` (§2.3 item 3, §3.3)."""
    await check_scheduler_lock(lock_conn)
    new_generation = 1
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_slot
                SET generation = generation + 1,
                    updated_at = NOW()
                WHERE slot_id = 1
                RETURNING generation
                """
            )
            gen_row = await cur.fetchone()
            if gen_row is not None:
                new_generation = int(_extract_field(gen_row, "generation", 0))

    await check_scheduler_lock(lock_conn)
    kill_outcome = await host_client.kill_host_tasks(
        generation=new_generation,
        run_id=run_id,
        reason=reason,
    )
    return new_generation, kill_outcome


async def monitor_container_until_exit(
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient,
    container_id: str,
    timeout_seconds: int | None = None,
    no_output_timeout_seconds: int | None = None,
    poll_interval_seconds: float = 1.0,
    heartbeat_interval_seconds: float = LOCK_HEARTBEAT_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> tuple[ContainerInspectionOutcome, bool]:
    """Monitor a running container for completion, dual timeouts, and periodic lock verification (§3.3).

    Output activity is tracked using incremental Docker logs (`get_container_logs(..., since=...)`)
    rather than the length of the 16 KiB truncated `log_tail`, so output beyond 16 KiB continues to
    refresh the no-output timer.
    """
    effective_timeout = (
        float(timeout_seconds) if timeout_seconds is not None else float(DEFAULT_TIMEOUT_SECONDS)
    )
    effective_no_output = (
        float(no_output_timeout_seconds) if no_output_timeout_seconds is not None else None
    )

    start_mono = clock()
    last_output_mono = start_mono
    last_heartbeat_mono = start_mono
    last_log_since_ts = max(0, int(wall_clock()))
    last_log_tail: str | None = None

    while True:
        now_mono = clock()
        if now_mono - last_heartbeat_mono >= heartbeat_interval_seconds:
            await check_scheduler_lock(lock_conn)
            last_heartbeat_mono = now_mono

        outcome = await docker_client.collect_container_outcome(container_id)
        if not outcome.running:
            if outcome.exit_code == EXIT_CODE_NO_OUTPUT_TIMEOUT:
                outcome.timeout_kind = "no_output"
                return outcome, True
            if outcome.exit_code == EXIT_CODE_TOTAL_TIMEOUT:
                outcome.timeout_kind = "total"
                return outcome, True
            return outcome, False

        # Incremental log check by timestamp (`since`) avoids false stall once log exceeds 16 KiB.
        poll_ts = max(0, int(wall_clock()))
        has_new_output = False
        if effective_no_output is not None:
            try:
                delta_logs = await docker_client.get_container_logs(
                    container_id,
                    since=last_log_since_ts,
                )
            except Exception:
                delta_logs = ""
            if isinstance(delta_logs, str) and delta_logs:
                has_new_output = True
                last_log_since_ts = poll_ts

        if last_log_tail is None:
            last_log_tail = outcome.log_tail
            if outcome.log_tail:
                has_new_output = True
        elif outcome.log_tail != last_log_tail:
            last_log_tail = outcome.log_tail
            has_new_output = True

        if has_new_output:
            last_output_mono = now_mono

        total_exceeded = (now_mono - start_mono) > effective_timeout
        no_output_exceeded = (
            effective_no_output is not None and (now_mono - last_output_mono) > effective_no_output
        )
        if total_exceeded or no_output_exceeded:
            timeout_kind: TimeoutKind = "total" if total_exceeded else "no_output"
            await check_scheduler_lock(lock_conn)
            try:
                await docker_client.stop_container(container_id, timeout=10)
            except Exception:
                pass
            post_stop = await docker_client.collect_container_outcome(container_id)
            if post_stop.running:
                await check_scheduler_lock(lock_conn)
                try:
                    await docker_client.kill_container(container_id)
                except Exception:
                    pass
                post_stop = await docker_client.collect_container_outcome(container_id)
            post_stop.timeout_kind = timeout_kind
            return post_stop, True

        await asyncio.sleep(poll_interval_seconds)


def resolve_notify_status(
    *,
    terminal_status: str,
    notify_enabled: bool,
    has_outbox_payload: bool,
    is_trial: bool = False,
    notify_on_failure: bool = False,
) -> str:
    """Compute ``notify_status`` for a completed run (§3.4.1).

    Trial runs (`is_trial == True`, i.e. manual runs claimed while task `enabled == False`)
    never dispatch live notifications: when the run produced a payload,
    `notify_status` is always `'preview_only'` regardless of `notify_enabled`.

    ``notify_on_failure`` keeps an outbox that OpenClaw itself would send on
    failure (ai-chain DEV text, FDE weekly failure text). Other failed runs
    stay ``skipped``. Live dispatch still requires ``notify_enabled``.
    """
    failure_notify = (
        notify_on_failure
        and terminal_status == "failed"
        and has_outbox_payload
    )
    if terminal_status != "succeeded" and not failure_notify:
        return "skipped"
    if is_trial:
        return "preview_only"
    if not has_outbox_payload:
        return "skipped"
    return "pending_dispatch" if notify_enabled else "preview_only"


async def finalize_task_run(
    conn: Any,
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient | None,
    claimed: ClaimedTaskRun,
    container_id: str | None,
    outcome: ContainerInspectionOutcome,
    evaluation: TaskEvaluationResult,
    outbox_payload: dict[str, Any] | None = None,
    frozen_manifest: dict[str, Any] | None = None,
    path_map: DataTaskPathMap | None = None,
) -> tuple[bool, int | None]:
    """Persist terminal status and ``notify_status`` in a single transaction, then clean up (§3.4.1).

    Returns ``(committed, consecutive_failures)``.
    - If ``outcome.running`` is still ``True`` (container stop/kill not confirmed), the run row is
      NOT finalized and the slot is set to ``slot_state = 'reclaiming'`` so the next tick retries
      reclamation without ever releasing the slot while the container may still be running.
    - If ``remove_container`` fails after committing the terminal run status, the slot is kept in
      ``slot_state = 'reclaiming'`` (not released to ``idle``) for retry on the next tick.
    """
    paths = path_map or build_path_map()
    await check_scheduler_lock(lock_conn)

    if outcome.running:
        logger.warning(
            "Container %s for run %s is still running after stop/kill; keeping slot in reclaiming",
            container_id,
            claimed.run_id,
        )
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state = 'reclaiming',
                        updated_at = NOW()
                    WHERE slot_id = 1
                      AND active_run_id = %(run_id)s::uuid
                    """,
                    {"run_id": claimed.run_id},
                )
        return False, None

    if (
        evaluation.ok
        and evaluation.status == "succeeded"
        and isinstance(outbox_payload, Mapping)
    ):
        run_art_dir = paths.backend_run_artifacts_dir(claimed.run_id)
        try:
            prepared_outbox = prepare_run_outbox_image_metadata(
                outbox_payload,
                run_artifacts_dir=run_art_dir,
                job_key=claimed.job_key,
            )
            if prepared_outbox is not None and prepared_outbox != outbox_payload:
                outbox_file = run_art_dir / "outbox.json"
                if outbox_file.exists():
                    atomic_write_json(
                        outbox_file,
                        prepared_outbox,
                        anchor_dir=paths.backend_data_root,
                    )
                outbox_payload = prepared_outbox
        except (DataTaskNotificationPrepError, DataTaskNotificationError, SafeFsError) as prep_exc:
            outbox_payload = None
            prep_code = getattr(prep_exc, "code", "report_preparation_failed")
            evaluation = TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"report_preparation_failed: {prep_exc}",
                warnings=list(evaluation.warnings),
                details={**evaluation.details, "report_prep_code": prep_code},
            )

    terminal_status = evaluation.status
    has_outbox = outbox_payload is not None
    notify_on_failure = (
        isinstance(outbox_payload, Mapping) and outbox_payload.get("notify_on_failure") is True
    )
    notify_status = resolve_notify_status(
        terminal_status=terminal_status,
        notify_enabled=claimed.notify_enabled,
        has_outbox_payload=has_outbox,
        is_trial=claimed.is_trial,
        notify_on_failure=notify_on_failure,
    )
    if (
        notify_status == "pending_dispatch"
        and isinstance(outbox_payload, dict)
        and isinstance(outbox_payload.get("delivery_writeback"), Mapping)
    ):
        try:
            from src.dalton.data_jobs.batch3_messages import capture_delivery_writeback_snapshot

            started_at = await _load_run_started_at(conn, claimed.run_id)
            writeback = outbox_payload.get("delivery_writeback")
            if isinstance(writeback, dict):
                if started_at:
                    writeback["run_started_at"] = started_at
                writeback["run_id"] = str(claimed.run_id)
            outbox_payload = capture_delivery_writeback_snapshot(
                outbox_payload,
                workspace=paths.backend_data_root,
                run_dir=paths.backend_run_artifacts_dir(claimed.run_id),
            )
        except (DataTaskNotificationPrepError, OSError, ValueError) as cap_exc:
            outbox_payload = None
            evaluation = TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"report_preparation_failed: {cap_exc}",
                warnings=list(evaluation.warnings),
                details={**evaluation.details, "report_prep_code": "writeback_snapshot_failed"},
            )
            terminal_status = evaluation.status
            notify_status = resolve_notify_status(
                terminal_status=terminal_status,
                notify_enabled=claimed.notify_enabled,
                has_outbox_payload=False,
                is_trial=claimed.is_trial,
                notify_on_failure=False,
            )
    serialized_outbox = (
        json.dumps(outbox_payload, ensure_ascii=False) if outbox_payload is not None else None
    )
    serialized_manifest = (
        json.dumps(frozen_manifest, ensure_ascii=False) if frozen_manifest is not None else None
    )

    from src.dalton.capabilities import CapabilityId, has_capability
    from src.dalton.monitors.data_job_registry import get_registered_monitor_bindings
    from src.dalton.settings import is_artifact_publishing_enabled

    monitor_bindings = (
        get_registered_monitor_bindings(claimed.job_key)
        if has_capability(CapabilityId.DATA_JOBS)
        else ()
    )
    mp_status = "not_applicable"
    mp_schedule_now = False
    mp_detail: dict[str, Any] = {}
    if monitor_bindings:
        if not is_artifact_publishing_enabled():
            mp_status = "disabled"
            mp_detail = {
                "reason": "artifact_publishing_disabled",
                "last_error": "DALTON_ARTIFACT_PUBLISHING_ENABLED 未开启，看板发布已禁用",
                "panels": {b.slug: {"status": "disabled"} for b in monitor_bindings},
            }
        elif terminal_status == "succeeded":
            frozen_monitor_dir: Path | None = None
            freeze_err: str | None = None
            try:
                frozen_monitor_dir = freeze_monitor_publish_artifacts(
                    data_root=paths.backend_data_root,
                    job_key=claimed.job_key,
                    run_id=str(claimed.run_id),
                )
            except Exception as freeze_mon_exc:
                freeze_err = str(freeze_mon_exc)
                logger.warning(
                    "Failed to freeze monitor artifacts for data task run %s (%s): %s",
                    claimed.run_id,
                    claimed.job_key,
                    freeze_mon_exc,
                )
            if (
                frozen_monitor_dir is not None
                and frozen_monitor_dir.is_dir()
                and not frozen_monitor_dir.is_symlink()
            ):
                mp_status = "pending"
                mp_schedule_now = True
                mp_detail = {
                    "frozen_dir": str(frozen_monitor_dir),
                    "panels": {b.slug: {"status": "pending"} for b in monitor_bindings},
                }
            else:
                reason = f"冻结看板产物失败: {freeze_err or '未生成有效冻结目录'}"
                mp_status = "failed"
                mp_schedule_now = False
                mp_detail = {
                    "frozen_dir": None,
                    "last_error": reason[:1000],
                    "panels": {
                        b.slug: {"status": "failed", "error": reason[:1000]}
                        for b in monitor_bindings
                    },
                }
        else:
            mp_status = "pending"
            mp_schedule_now = True
            mp_detail = {
                "terminal_status": terminal_status,
                "failure_reason": evaluation.failure_reason,
                "panels": {b.slug: {"status": "pending"} for b in monitor_bindings},
            }
    serialized_mp_detail = json.dumps(mp_detail, ensure_ascii=False)

    consecutive_failures: int | None = None
    try:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status          = %(terminal_status)s,
                        exit_code       = %(exit_code)s,
                        oom_killed      = %(oom_killed)s,
                        failure_reason  = %(failure_reason)s,
                        log_tail        = %(log_tail)s,
                        outbox_payload  = %(outbox_payload)s::jsonb,
                        notify_preview  = %(notify_preview)s::jsonb,
                        notify_status   = %(notify_status)s,
                        frozen_manifest = %(frozen_manifest)s::jsonb,
                        monitor_publish_status        = %(monitor_publish_status)s,
                        monitor_publish_next_retry_at = CASE WHEN %(monitor_publish_schedule_now)s THEN NOW() ELSE NULL END,
                        monitor_publish_detail        = %(monitor_publish_detail)s::jsonb,
                        completed_at    = NOW(),
                        updated_at      = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND status IN ('starting', 'running')
                    """,
                    {
                        "terminal_status": terminal_status,
                        "exit_code": outcome.exit_code,
                        "oom_killed": outcome.oom_killed,
                        "failure_reason": evaluation.failure_reason,
                        "log_tail": outcome.log_tail,
                        "outbox_payload": serialized_outbox,
                        "notify_preview": serialized_outbox,
                        "notify_status": notify_status,
                        "frozen_manifest": serialized_manifest,
                        "monitor_publish_status": mp_status,
                        "monitor_publish_schedule_now": mp_schedule_now,
                        "monitor_publish_detail": serialized_mp_detail,
                        "run_id": claimed.run_id,
                    },
                )
                if cur.rowcount != 1:
                    raise _TerminalRollback()

                if not claimed.is_trial:
                    if terminal_status == "succeeded":
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_defs
                            SET consecutive_failures = 0,
                                updated_at = NOW()
                            WHERE job_key = %(job_key)s
                            RETURNING consecutive_failures
                            """,
                            {"job_key": claimed.job_key},
                        )
                    else:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_defs
                            SET consecutive_failures = consecutive_failures + 1,
                                updated_at = NOW()
                            WHERE job_key = %(job_key)s
                            RETURNING consecutive_failures
                            """,
                            {"job_key": claimed.job_key},
                        )
                    cf_row = await cur.fetchone()
                    if cf_row is not None:
                        consecutive_failures = int(_extract_field(cf_row, "consecutive_failures", 0))
    except _TerminalRollback:
        return False, None

    try:
        freeze_task_run_artifacts(
            data_root=paths.backend_data_root,
            job_key=claimed.job_key,
            scheduled_slot_utc=claimed.scheduled_slot_utc,
            completed_at=datetime.now(timezone.utc),
            exit_code=outcome.exit_code,
        )
    except Exception as freeze_exc:
        logger.warning(
            "Failed to freeze artifacts for data task run %s (%s): %s",
            claimed.run_id,
            claimed.job_key,
            freeze_exc,
        )

    # DALTON-HOOK(monitors): claim & execute monitor publish via shared PostgreSQL lease queue.
    if mp_status == "pending":
        try:
            await process_monitor_publish_for_run(
                conn,
                run_id=str(claimed.run_id),
                path_map=paths,
            )
        except Exception as monitor_exc:
            logger.warning(
                "First-pass monitor publish for data task run %s (%s) deferred to PG replay: %s",
                claimed.run_id,
                claimed.job_key,
                monitor_exc,
            )
    elif mp_status in ("disabled", "failed"):
        err_detail = str(mp_detail.get("last_error") or mp_detail.get("reason") or mp_status)
        if mp_status == "failed":
            try:
                from src.dalton.monitors import service as monitor_service

                await monitor_service.maybe_publish_data_job_monitor(
                    job_key=claimed.job_key,
                    run_id=str(claimed.run_id),
                    terminal_status="failed",
                    failure_reason=err_detail,
                    data_root=paths.backend_data_root,
                )
            except Exception as rec_exc:
                logger.debug(
                    "Recording freeze failure on existing monitors for %s failed: %s",
                    claimed.job_key,
                    rec_exc,
                )
        await _emit_monitor_publish_ops_alert(
            job_key=claimed.job_key,
            run_id=str(claimed.run_id),
            status=mp_status,
            detail_text=err_detail,
        )

    if container_id and docker_client is not None:
        await check_scheduler_lock(lock_conn)
        try:
            await docker_client.remove_container(container_id, force=True)
        except Exception as exc:
            if not is_container_not_found_error(exc):
                logger.warning(
                    "Failed to remove finished container %s; keeping slot in reclaiming: %s",
                    container_id,
                    exc,
                )
                await check_scheduler_lock(lock_conn)
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_slot
                            SET slot_state = 'reclaiming',
                                updated_at = NOW()
                            WHERE slot_id = 1
                              AND active_run_id = %(run_id)s::uuid
                            """,
                            {"run_id": claimed.run_id},
                        )
                return True, consecutive_failures

    cleanup_run_secrets_dir(claimed.run_id, path_map=paths)

    await check_scheduler_lock(lock_conn)
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_slot
                SET slot_state     = 'idle',
                    active_run_id  = NULL,
                    active_job_key = NULL,
                    started_at     = NULL,
                    updated_at     = NOW()
                WHERE slot_id = 1 AND active_run_id = %(run_id)s::uuid
                """,
                {"run_id": claimed.run_id},
            )

    return True, consecutive_failures


async def _stop_and_confirm_container(
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient,
    container_id: str,
) -> bool:
    """Stop or kill ``container_id`` with lock checks before every Docker call, and confirm ``Running == False``."""
    await check_scheduler_lock(lock_conn)
    try:
        await docker_client.stop_container(container_id, timeout=10)
    except Exception:
        await check_scheduler_lock(lock_conn)
        try:
            await docker_client.kill_container(container_id)
        except Exception:
            pass

    try:
        info = await docker_client.inspect_container(container_id)
    except Exception as exc:
        return is_container_not_found_error(exc)
    state = info.get("State") if isinstance(info.get("State"), Mapping) else {}
    if bool(state.get("Running", False)):
        await check_scheduler_lock(lock_conn)
        try:
            await docker_client.kill_container(container_id)
        except Exception as exc:
            if not is_container_not_found_error(exc):
                return False
        try:
            info = await docker_client.inspect_container(container_id)
        except Exception as exc:
            return is_container_not_found_error(exc)
        state = info.get("State") if isinstance(info.get("State"), Mapping) else {}
        if bool(state.get("Running", False)):
            return False
    return True


TAKEOVER_RETRY_TRIGGERED_BY = "system:takeover_retry"
_PENDING_TAKEOVER_RETRIES_FILE = "pending_takeover_retries.json"

MAX_MONITOR_PUBLISH_ATTEMPTS = 3
MONITOR_PUBLISH_LEASE_SECONDS = 120
MONITOR_PUBLISH_RETRY_BACKOFFS_S: tuple[int, ...] = (60, 300)


@dataclass(frozen=True)
class ClaimedMonitorPublishRun:
    """One ``dalton_data_task_runs`` row leased for verified monitor publishing."""

    run_id: str
    job_key: str
    status: str
    failure_reason: str | None
    scheduled_slot_utc: datetime | None
    completed_at: datetime | None
    attempts: int
    claim_epoch: int
    detail: dict[str, Any]


def _default_monitor_publish_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _parse_jsonb_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


async def _emit_monitor_publish_ops_alert(
    *,
    job_key: str,
    run_id: str,
    status: str,
    detail_text: str,
) -> bool:
    """Send a best-effort loopback ops alert to channel gateway on terminal monitor publish failure."""
    from src.dalton.capabilities import CapabilityId, has_capability
    from src.dalton.settings import get_dalton_channel_gateway_url, get_internal_service_token

    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return False
    base_url = get_dalton_channel_gateway_url()
    if not base_url:
        return False
    token = get_internal_service_token()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Service-Token"] = token
    summary = f"{job_key} ({status}, run={run_id[:8]}): {detail_text}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/internal/ops-alerts",
                json={"code": "monitor_publish_failed", "detail": summary[:600]},
                headers=headers,
            )
            return resp.status_code < 400
    except Exception as exc:
        logger.warning(
            "Failed sending monitor_publish_failed ops alert for %s (%s): %s",
            job_key,
            run_id,
            exc,
        )
        return False


async def _is_run_superseded_in_cur(
    cur: Any,
    *,
    run_id: str,
    job_key: str,
    scheduled_slot_utc: datetime | None,
    completed_at: datetime | None,
) -> bool:
    await cur.execute(
        """
        SELECT 1
          FROM dalton_data_task_runs
         WHERE job_key = %(job_key)s
           AND run_id <> %(run_id)s::uuid
           AND status = 'succeeded'
           AND monitor_publish_status IN ('published', 'unchanged', 'partial_failed')
           AND (
               (%(slot_utc)s::timestamptz IS NOT NULL AND scheduled_slot_utc > %(slot_utc)s::timestamptz)
               OR (
                   (%(slot_utc)s::timestamptz IS NULL OR scheduled_slot_utc = %(slot_utc)s::timestamptz)
                   AND COALESCE(completed_at, updated_at) > COALESCE(%(completed_at)s::timestamptz, '-infinity'::timestamptz)
               )
           )
         LIMIT 1
        """,
        {
            "job_key": job_key,
            "run_id": run_id,
            "slot_utc": scheduled_slot_utc,
            "completed_at": completed_at,
        },
    )
    return await cur.fetchone() is not None


async def is_run_superseded_for_monitor_publish(
    conn: Any,
    *,
    run_id: str,
    job_key: str,
    scheduled_slot_utc: datetime | None,
    completed_at: datetime | None,
) -> bool:
    """Return True if a newer succeeded run of ``job_key`` already reached terminal publish status."""
    async with conn.cursor(row_factory=dict_row) as cur:
        return await _is_run_superseded_in_cur(
            cur,
            run_id=run_id,
            job_key=job_key,
            scheduled_slot_utc=scheduled_slot_utc,
            completed_at=completed_at,
        )


def _terminal_status_from_detail(detail: Mapping[str, Any]) -> str:
    panels = detail.get("panels")
    if isinstance(panels, Mapping) and panels:
        statuses = {
            str(v.get("status") or "")
            for v in panels.values()
            if isinstance(v, Mapping)
        }
        if statuses & {"published", "unchanged"}:
            return "partial_failed"
    return "failed"


async def claim_monitor_publish_run_tx(
    conn: Any,
    *,
    run_id: str | None = None,
    claimed_by: str | None = None,
    lease_seconds: int = MONITOR_PUBLISH_LEASE_SECONDS,
    limit: int = 10,
) -> list[ClaimedMonitorPublishRun]:
    """Atomically claim due or lease-expired ``dalton_data_task_runs`` for monitor publishing.

    Shared by both first-pass trigger in ``finalize_task_run`` and background replay scanner.
    Uses ``FOR UPDATE SKIP LOCKED``, enforces 120s lease takeover, settles exhausted attempt-3
    crashes to ``failed``/``partial_failed``, and converges older superseded runs to ``superseded``.
    """
    worker_id = claimed_by or _default_monitor_publish_worker_id()
    claimed_runs: list[ClaimedMonitorPublishRun] = []
    exhausted_alerts: list[tuple[str, str, str, str]] = []

    run_filter_sql = "AND run_id = %(run_id)s::uuid" if run_id is not None else ""
    params: dict[str, Any] = {
        "limit": max(1, int(limit)),
    }
    if run_id is not None:
        params["run_id"] = str(run_id)

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT run_id::text AS run_id,
                       job_key,
                       status,
                       failure_reason,
                       scheduled_slot_utc,
                       completed_at,
                       monitor_publish_status,
                       monitor_publish_attempts,
                       monitor_publish_claim_epoch,
                       monitor_publish_detail
                  FROM dalton_data_task_runs
                 WHERE (
                     (monitor_publish_status IN ('pending', 'retrying')
                      AND COALESCE(monitor_publish_next_retry_at, NOW()) <= NOW())
                     OR
                     (monitor_publish_status = 'publishing'
                      AND monitor_publish_lease_expires_at <= NOW())
                 )
                 {run_filter_sql}
                 ORDER BY COALESCE(monitor_publish_next_retry_at, completed_at, created_at) ASC
                 LIMIT %(limit)s
                   FOR UPDATE SKIP LOCKED
                """,
                params,
            )
            rows = await cur.fetchall()
            for row in rows:
                rid = str(row["run_id"])
                jk = str(row["job_key"])
                run_status = str(row["status"])
                slot_utc = row.get("scheduled_slot_utc")
                comp_at = row.get("completed_at")
                attempts = int(row.get("monitor_publish_attempts") or 0)
                detail = _parse_jsonb_dict(row.get("monitor_publish_detail"))

                if run_status == "succeeded" and await _is_run_superseded_in_cur(
                    cur,
                    run_id=rid,
                    job_key=jk,
                    scheduled_slot_utc=slot_utc,
                    completed_at=comp_at,
                ):
                    detail["superseded_at"] = datetime.now(timezone.utc).isoformat()
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                           SET monitor_publish_status = 'superseded',
                               monitor_publish_detail = %(detail)s::jsonb,
                               monitor_publish_lease_expires_at = NULL,
                               monitor_publish_next_retry_at = NULL,
                               updated_at = NOW()
                         WHERE run_id = %(run_id)s::uuid
                        """,
                        {
                            "run_id": rid,
                            "detail": json.dumps(detail, ensure_ascii=False),
                        },
                    )
                    continue

                if attempts >= MAX_MONITOR_PUBLISH_ATTEMPTS:
                    terminal_mp = _terminal_status_from_detail(detail)
                    detail["exhausted_after_lease_timeout"] = True
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                           SET monitor_publish_status = %(status)s,
                               monitor_publish_detail = %(detail)s::jsonb,
                               monitor_publish_lease_expires_at = NULL,
                               monitor_publish_next_retry_at = NULL,
                               updated_at = NOW()
                         WHERE run_id = %(run_id)s::uuid
                        """,
                        {
                            "run_id": rid,
                            "status": terminal_mp,
                            "detail": json.dumps(detail, ensure_ascii=False),
                        },
                    )
                    exhausted_alerts.append(
                        (jk, rid, terminal_mp, "租约超时且已达到最大重试次数 (3)")
                    )
                    continue

                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                       SET monitor_publish_status = 'publishing',
                           monitor_publish_attempts = monitor_publish_attempts + 1,
                           monitor_publish_claim_epoch = monitor_publish_claim_epoch + 1,
                           monitor_publish_claimed_by = %(claimed_by)s,
                           monitor_publish_lease_expires_at = NOW() + make_interval(secs => %(lease_s)s),
                           updated_at = NOW()
                     WHERE run_id = %(run_id)s::uuid
                    RETURNING monitor_publish_attempts, monitor_publish_claim_epoch
                    """,
                    {
                        "run_id": rid,
                        "claimed_by": worker_id,
                        "lease_s": int(lease_seconds),
                    },
                )
                upd = await cur.fetchone()
                new_attempts = (
                    int(upd["monitor_publish_attempts"]) if upd is not None else attempts + 1
                )
                new_epoch = (
                    int(upd["monitor_publish_claim_epoch"])
                    if upd is not None
                    else int(row.get("monitor_publish_claim_epoch") or 0) + 1
                )
                claimed_runs.append(
                    ClaimedMonitorPublishRun(
                        run_id=rid,
                        job_key=jk,
                        status=run_status,
                        failure_reason=row.get("failure_reason"),
                        scheduled_slot_utc=slot_utc,
                        completed_at=comp_at,
                        attempts=new_attempts,
                        claim_epoch=new_epoch,
                        detail=detail,
                    )
                )

    for jk, rid, st, msg in exhausted_alerts:
        await _emit_monitor_publish_ops_alert(
            job_key=jk, run_id=rid, status=st, detail_text=msg
        )

    return claimed_runs


async def update_monitor_publish_panel_staged_tx(
    conn: Any,
    *,
    run_id: str,
    claim_epoch: int,
    slug: str,
    artifact_id: str,
    candidate_seq: int,
    detail: dict[str, Any],
) -> bool:
    """Persist staged ``(artifact_id, candidate_seq)`` for ``slug`` under claim-epoch fence."""
    panels = detail.setdefault("panels", {})
    if not isinstance(panels, dict):
        panels = {}
        detail["panels"] = panels
    cur_panel = dict(panels.get(slug) or {})
    cur_panel.update(
        {
            "status": "staged",
            "artifact_id": artifact_id,
            "candidate_seq": int(candidate_seq),
        }
    )
    panels[slug] = cur_panel
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                   SET monitor_publish_detail = %(detail)s::jsonb,
                       updated_at = NOW()
                 WHERE run_id = %(run_id)s::uuid
                   AND monitor_publish_status = 'publishing'
                   AND monitor_publish_claim_epoch = %(claim_epoch)s
                """,
                {
                    "run_id": run_id,
                    "claim_epoch": int(claim_epoch),
                    "detail": json.dumps(detail, ensure_ascii=False),
                },
            )
            return cur.rowcount == 1


async def settle_monitor_publish_run_tx(
    conn: Any,
    *,
    run_id: str,
    claim_epoch: int,
    next_status: str,
    detail: dict[str, Any],
    next_retry_delay_s: int | None = None,
    refund_attempt: bool = False,
) -> bool:
    """Fenced settlement of a claimed monitor publish run (ignores slow stale-epoch writers)."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                   SET monitor_publish_status = %(next_status)s,
                       monitor_publish_attempts = CASE
                           WHEN %(refund_attempt)s::boolean
                           THEN GREATEST(monitor_publish_attempts - 1, 0)
                           ELSE monitor_publish_attempts
                       END,
                       monitor_publish_detail = %(detail)s::jsonb,
                       monitor_publish_lease_expires_at = NULL,
                       monitor_publish_next_retry_at = CASE
                           WHEN %(delay_s)s::int IS NOT NULL
                           THEN NOW() + make_interval(secs => %(delay_s)s::int)
                           ELSE NULL
                       END,
                       updated_at = NOW()
                 WHERE run_id = %(run_id)s::uuid
                   AND monitor_publish_status = 'publishing'
                   AND monitor_publish_claim_epoch = %(claim_epoch)s
                """,
                {
                    "run_id": run_id,
                    "claim_epoch": int(claim_epoch),
                    "next_status": next_status,
                    "detail": json.dumps(detail, ensure_ascii=False),
                    "delay_s": next_retry_delay_s,
                    "refund_attempt": bool(refund_attempt),
                },
            )
            return cur.rowcount == 1


async def execute_claimed_monitor_publish(
    conn: Any,
    claimed_pub: ClaimedMonitorPublishRun,
    *,
    path_map: DataTaskPathMap | None = None,
) -> str:
    """Execute one claimed monitor publish run strictly from its immutable ``frozen_dir`` and settle under fence."""
    from src.dalton.monitors import service as monitor_service
    from src.dalton.settings import is_artifact_publishing_enabled

    paths = path_map or build_path_map()
    detail = dict(claimed_pub.detail)
    panels = detail.get("panels")
    if not isinstance(panels, dict):
        panels = {}
        detail["panels"] = panels

    async def _before_promote() -> bool:
        superseded = await is_run_superseded_for_monitor_publish(
            conn,
            run_id=claimed_pub.run_id,
            job_key=claimed_pub.job_key,
            scheduled_slot_utc=claimed_pub.scheduled_slot_utc,
            completed_at=claimed_pub.completed_at,
        )
        return not superseded

    async def _on_staged(slug: str, artifact_id: str, candidate_seq: int) -> None:
        await update_monitor_publish_panel_staged_tx(
            conn,
            run_id=claimed_pub.run_id,
            claim_epoch=claimed_pub.claim_epoch,
            slug=slug,
            artifact_id=artifact_id,
            candidate_seq=candidate_seq,
            detail=detail,
        )

    next_status = "failed"
    retry_delay_s: int | None = None
    refund_attempt = False
    is_deferred = False
    error_summary = ""

    try:
        if claimed_pub.status == "succeeded":
            if not is_artifact_publishing_enabled():
                next_status = "disabled"
                error_summary = "DALTON_ARTIFACT_PUBLISHING_ENABLED 未开启，看板发布已禁用"
                detail["reason"] = "artifact_publishing_disabled"
            else:
                raw_frozen = detail.get("frozen_dir")
                frozen_path = (
                    Path(raw_frozen.strip())
                    if isinstance(raw_frozen, str) and raw_frozen.strip()
                    else None
                )
                if (
                    frozen_path is None
                    or not frozen_path.is_dir()
                    or frozen_path.is_symlink()
                ):
                    error_summary = "冻结目录不存在或不可读，拒绝回退共享 data_root"
                    for slug, pinfo in list(panels.items()):
                        if isinstance(pinfo, dict) and pinfo.get("status") not in (
                            "published",
                            "unchanged",
                        ):
                            updated_p = dict(pinfo)
                            updated_p["status"] = "failed"
                            updated_p["error"] = error_summary
                            panels[slug] = updated_p
                    next_status = _terminal_status_from_detail(detail)
                    try:
                        await monitor_service.maybe_publish_data_job_monitor(
                            job_key=claimed_pub.job_key,
                            run_id=claimed_pub.run_id,
                            terminal_status="failed",
                            failure_reason=error_summary,
                            data_root=paths.backend_data_root,
                            claim_epoch=claimed_pub.claim_epoch,
                        )
                    except Exception:
                        pass
                else:
                    result = await monitor_service.republish_data_job_monitor(
                        job_key=claimed_pub.job_key,
                        run_id=claimed_pub.run_id,
                        data_root=frozen_path,
                        panel_progress=panels,
                        before_promote=_before_promote,
                        on_staged=_on_staged,
                        raise_on_partial=False,
                        claim_epoch=claimed_pub.claim_epoch,
                        check_superseded=True,
                    )
                    res_status = str(result.get("status") or "")
                    if res_status == "fenced_out" or result.get("fenced_out"):
                        return "fenced_out"
                    panel_results = result.get("panel_results")
                    has_panel_results = isinstance(panel_results, dict) and bool(panel_results)
                    if isinstance(panel_results, dict):
                        for slug, pinfo in panel_results.items():
                            if isinstance(pinfo, dict):
                                merged = dict(panels.get(slug) or {})
                                merged.update(pinfo)
                                if pinfo.get("status") != "failed" and "error" not in pinfo:
                                    merged.pop("error", None)
                                if pinfo.get("status") != "deferred" and "deferred" not in pinfo:
                                    merged.pop("deferred", None)
                                panels[slug] = merged
                    error_summary = str(result.get("error") or "").strip()
                    if not error_summary and isinstance(panel_results, dict):
                        for pinfo in panel_results.values():
                            if (
                                isinstance(pinfo, dict)
                                and pinfo.get("status") == "failed"
                                and str(pinfo.get("error") or "").strip()
                            ):
                                error_summary = str(pinfo["error"]).strip()
                                break
                    panel_statuses = {
                        str(p.get("status") or "")
                        for p in panels.values()
                        if isinstance(p, dict)
                    }
                    has_failed_panel = (
                        res_status in ("failed", "partial_failed")
                        or "failed" in panel_statuses
                    )
                    unfinished_panel_statuses = panel_statuses - {"published", "unchanged"}
                    is_pure_deferred = (
                        not has_failed_panel
                        and (res_status == "deferred" or bool(result.get("deferred")))
                        and (
                            not has_panel_results
                            or unfinished_panel_statuses == {"deferred"}
                        )
                    )
                    if res_status == "superseded" or result.get("superseded"):
                        next_status = "superseded"
                    elif is_pure_deferred:
                        is_deferred = True
                        next_status = "retrying"
                        retry_delay_s = None
                        refund_attempt = True
                    elif (
                        has_failed_panel
                        or res_status == "deferred"
                        or result.get("deferred")
                    ):
                        if claimed_pub.attempts < MAX_MONITOR_PUBLISH_ATTEMPTS:
                            next_status = "retrying"
                            idx = min(
                                claimed_pub.attempts - 1,
                                len(MONITOR_PUBLISH_RETRY_BACKOFFS_S) - 1,
                            )
                            retry_delay_s = MONITOR_PUBLISH_RETRY_BACKOFFS_S[idx]
                        else:
                            next_status = (
                                res_status
                                if res_status in ("failed", "partial_failed")
                                else _terminal_status_from_detail(detail)
                            )
                    elif res_status in ("published", "unchanged"):
                        next_status = res_status
                    else:
                        next_status = "published" if result.get("cut") else "unchanged"
        else:
            await monitor_service.maybe_publish_data_job_monitor(
                job_key=claimed_pub.job_key,
                run_id=claimed_pub.run_id,
                terminal_status=claimed_pub.status,
                failure_reason=claimed_pub.failure_reason,
                data_root=paths.backend_data_root,
                claim_epoch=claimed_pub.claim_epoch,
            )
            next_status = "unchanged"
    except Exception as exc:
        error_summary = str(exc)
        detail["last_error"] = error_summary[:1000]
        if claimed_pub.attempts < MAX_MONITOR_PUBLISH_ATTEMPTS:
            next_status = "retrying"
            idx = min(
                claimed_pub.attempts - 1,
                len(MONITOR_PUBLISH_RETRY_BACKOFFS_S) - 1,
            )
            retry_delay_s = MONITOR_PUBLISH_RETRY_BACKOFFS_S[idx]
        else:
            next_status = _terminal_status_from_detail(detail)

    if error_summary:
        detail["last_error"] = error_summary[:1000]

    settled = await settle_monitor_publish_run_tx(
        conn,
        run_id=claimed_pub.run_id,
        claim_epoch=claimed_pub.claim_epoch,
        next_status=next_status,
        detail=detail,
        next_retry_delay_s=retry_delay_s,
        refund_attempt=refund_attempt,
    )
    if settled and next_status in ("failed", "partial_failed", "disabled"):
        await _emit_monitor_publish_ops_alert(
            job_key=claimed_pub.job_key,
            run_id=claimed_pub.run_id,
            status=next_status,
            detail_text=error_summary or "看板发布核验耗尽重试次数",
        )
    if is_deferred:
        return "deferred" if settled else "fenced_out"
    return next_status if settled else "fenced_out"


async def process_monitor_publish_for_run(
    conn: Any,
    *,
    run_id: str,
    path_map: DataTaskPathMap | None = None,
    claimed_by: str | None = None,
) -> str | None:
    """First-pass monitor publish entry point sharing ``claim_monitor_publish_run_tx``."""
    claimed_list = await claim_monitor_publish_run_tx(
        conn,
        run_id=run_id,
        claimed_by=claimed_by,
        limit=1,
    )
    if not claimed_list:
        return None
    return await execute_claimed_monitor_publish(
        conn, claimed_list[0], path_map=path_map
    )


def _read_json_store(path: Path) -> dict[str, dict[str, Any]]:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): dict(v)
                for k, v in data.items()
                if isinstance(v, dict)
            }
    except Exception:
        pass
    return {}


def _write_json_store(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _record_pending_takeover_retry(
    paths: DataTaskPathMap,
    *,
    job_key: str,
    scheduled_slot_utc: datetime,
    deadline_utc: datetime,
    source_run_id: str,
    blocked_by_run_id: str | None = None,
) -> None:
    """Persist a deferred takeover-retry intent deduplicated by ``(job_key, scheduled_slot_utc)``."""
    store_path = paths.backend_var_dir / _PENDING_TAKEOVER_RETRIES_FILE
    records = _read_json_store(store_path)
    dedup_key = f"{job_key}|{scheduled_slot_utc.astimezone(timezone.utc).isoformat()}"
    if dedup_key not in records:
        records[dedup_key] = {
            "job_key": str(job_key),
            "scheduled_slot_utc": scheduled_slot_utc.astimezone(timezone.utc).isoformat(),
            "deadline_utc": deadline_utc.astimezone(timezone.utc).isoformat(),
            "source_run_id": str(source_run_id),
            "blocked_by_run_id": str(blocked_by_run_id) if blocked_by_run_id else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_json_store(store_path, records)
        except Exception as exc:
            logger.warning(
                "Failed persisting deferred takeover retry intent for %s: %s",
                dedup_key,
                exc,
            )


async def _durable_publish_data_job_monitor(
    *,
    job_key: str,
    run_id: str,
    terminal_status: str,
    failure_reason: str,
    paths: DataTaskPathMap,
    conn: Any = None,
) -> bool:
    """Record a failure/cancelled monitor update via PostgreSQL durable queue (no local JSON file)."""
    from src.dalton.monitors.data_job_registry import get_registered_monitor_bindings
    from src.dalton.monitors.service import maybe_publish_data_job_monitor

    if not get_registered_monitor_bindings(job_key):
        return True

    if conn is not None:
        detail = {
            "terminal_status": str(terminal_status),
            "failure_reason": str(failure_reason),
        }
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                           SET monitor_publish_status = 'pending',
                               monitor_publish_next_retry_at = NOW(),
                               monitor_publish_detail = %(detail)s::jsonb,
                               updated_at = NOW()
                         WHERE run_id = %(run_id)s::uuid
                        """,
                        {
                            "run_id": str(run_id),
                            "detail": json.dumps(detail, ensure_ascii=False),
                        },
                    )
        except Exception as db_exc:
            logger.debug(
                "Could not mark PG monitor_publish_status pending for %s: %s",
                run_id,
                db_exc,
            )

    try:
        await maybe_publish_data_job_monitor(
            job_key=job_key,
            run_id=str(run_id),
            terminal_status=terminal_status,
            failure_reason=failure_reason,
            data_root=paths.backend_data_root,
        )
    except Exception as monitor_exc:
        logger.warning(
            "Failed to update monitor for data task run %s (%s, %s); kept pending in PG for replay: %s",
            run_id,
            job_key,
            terminal_status,
            monitor_exc,
        )
        return False

    if conn is not None:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs
                           SET monitor_publish_status = 'unchanged',
                               monitor_publish_lease_expires_at = NULL,
                               monitor_publish_next_retry_at = NULL,
                               updated_at = NOW()
                         WHERE run_id = %(run_id)s::uuid
                           AND monitor_publish_status IN ('pending', 'retrying')
                        """,
                        {"run_id": str(run_id)},
                    )
        except Exception:
            pass
    return True


async def replay_pending_monitor_publishes(
    conn: Any = None,
    *,
    path_map: DataTaskPathMap | None = None,
    limit: int = 10,
    claimed_by: str | None = None,
) -> int:
    """Replay due or lease-expired monitor publishes from PostgreSQL ``dalton_data_task_runs``."""
    paths = path_map or build_path_map()

    async def _run_with_conn(active_conn: Any) -> int:
        claimed_runs = await claim_monitor_publish_run_tx(
            active_conn,
            claimed_by=claimed_by,
            limit=limit,
        )
        processed = 0
        for item in claimed_runs:
            await execute_claimed_monitor_publish(active_conn, item, path_map=paths)
            processed += 1
        return processed

    if conn is not None:
        return await _run_with_conn(conn)

    try:
        from src.server.database.pool import get_db_connection

        async with get_db_connection() as pooled_conn:
            return await _run_with_conn(pooled_conn)
    except Exception as exc:
        logger.debug("Skipping PG monitor publish replay (DB unavailable): %s", exc)
        return 0


async def _cancel_and_publish_expired_takeover_retry(
    conn: Any,
    *,
    run_id: str,
    job_key: str,
    scheduled_slot_utc: datetime | None,
    deadline_utc: datetime | None,
    path_map: DataTaskPathMap | None = None,
) -> None:
    """Shared cancellation + durable monitor registration for both takeover-retry expiry branches."""
    paths = path_map or build_path_map()
    expired_detail = (
        f"takeover_retry_expired: 补跑领取时已过截止时间"
        f"（时段 {scheduled_slot_utc.isoformat() if scheduled_slot_utc else 'unknown'}，"
        f"截止 {deadline_utc.isoformat() if deadline_utc else 'unknown'}）"
    )
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                SET status = 'cancelled',
                    failure_reason = 'takeover_retry_expired',
                    notify_status = 'skipped',
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE run_id = %(run_id)s::uuid
                  AND status = 'queued'
                """,
                {"run_id": run_id},
            )
    await _durable_publish_data_job_monitor(
        job_key=job_key,
        run_id=run_id,
        terminal_status="cancelled",
        failure_reason=expired_detail,
        paths=paths,
    )


async def flush_pending_takeover_retries(
    conn: Any,
    *,
    path_map: DataTaskPathMap | None = None,
    now_utc: datetime | None = None,
) -> int:
    """Attempt to enqueue persisted deferred takeover-retry intents once a job's queue slot frees up.

    Deduplicates strictly by ``(job_key, scheduled_slot_utc)`` and enforces ``deadline_utc``
    against the database clock (``NOW()`` when ``now_utc`` is None) so expired intents are
    dropped and reported rather than enqueued late.
    """
    paths = path_map or build_path_map()
    store_path = paths.backend_var_dir / _PENDING_TAKEOVER_RETRIES_FILE
    records = _read_json_store(store_path)
    if not records:
        return 0

    enqueued = 0
    changed = False
    use_db_clock = now_utc is None
    check_now = (
        now_utc.astimezone(timezone.utc)
        if now_utc is not None
        else datetime.now(timezone.utc)
    )

    for dedup_key, rec in list(records.items()):
        job_key = str(rec.get("job_key") or "").strip()
        slot_dt = _coerce_utc_datetime(rec.get("scheduled_slot_utc"))
        dl_dt = _coerce_utc_datetime(rec.get("deadline_utc"))
        source_run_id = str(rec.get("source_run_id") or dedup_key)
        if not job_key or slot_dt is None or dl_dt is None:
            records.pop(dedup_key, None)
            changed = True
            continue
        if check_now >= dl_dt:
            records.pop(dedup_key, None)
            changed = True
            await _durable_publish_data_job_monitor(
                job_key=job_key,
                run_id=source_run_id,
                terminal_status="aborted",
                failure_reason=(
                    f"takeover_retry_expired: 占槽释放前已过截止时间"
                    f"（时段 {slot_dt.isoformat()}，截止 {dl_dt.isoformat()}，不再补跑）"
                ),
                paths=paths,
            )
            continue

        deadline_pred_sql = "NOW() < %(deadline_utc)s" if use_db_clock else "%(check_now)s < %(deadline_utc)s"
        inserted_id: str | None = None
        should_remove = False
        expired_in_db = False

        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT enabled, paused_by_user, deleted_at
                    FROM dalton_data_task_defs
                    WHERE job_key = %(job_key)s
                    """,
                    {"job_key": job_key},
                )
                def_row = await cur.fetchone()
                if (
                    def_row is None
                    or not bool(_extract_field(def_row, "enabled", 0))
                    or bool(_extract_field(def_row, "paused_by_user", 1))
                    or _extract_field(def_row, "deleted_at", 2) is not None
                ):
                    should_remove = True
                else:
                    await cur.execute(
                        """
                        SELECT run_id
                        FROM dalton_data_task_runs
                        WHERE job_key = %(job_key)s
                          AND scheduled_slot_utc = %(slot_utc)s
                          AND triggered_by_user_id = %(triggered_by_user_id)s
                        LIMIT 1
                        """,
                        {
                            "job_key": job_key,
                            "slot_utc": slot_dt,
                            "triggered_by_user_id": TAKEOVER_RETRY_TRIGGERED_BY,
                        },
                    )
                    if await cur.fetchone() is not None:
                        should_remove = True
                    else:
                        rerun_id = str(uuid4())
                        await cur.execute(
                            f"""
                            INSERT INTO dalton_data_task_runs (
                                run_id,
                                job_key,
                                trigger_source,
                                triggered_by_user_id,
                                scheduled_slot_utc,
                                deadline_utc,
                                status,
                                created_at,
                                updated_at
                            )
                            SELECT
                                %(run_id)s::uuid,
                                %(job_key)s,
                                'manual',
                                %(triggered_by_user_id)s,
                                %(slot_utc)s,
                                %(deadline_utc)s,
                                'queued',
                                NOW(),
                                NOW()
                            WHERE {deadline_pred_sql}
                            ON CONFLICT DO NOTHING
                            RETURNING run_id
                            """,
                            {
                                "run_id": rerun_id,
                                "job_key": job_key,
                                "triggered_by_user_id": TAKEOVER_RETRY_TRIGGERED_BY,
                                "slot_utc": slot_dt,
                                "deadline_utc": dl_dt,
                                "check_now": check_now,
                            },
                        )
                        ins = await cur.fetchone()
                        if ins is not None:
                            inserted_id = str(_extract_field(ins, "run_id", 0))
                            should_remove = True
                        else:
                            await cur.execute(
                                """
                                SELECT run_id
                                FROM dalton_data_task_runs
                                WHERE job_key = %(job_key)s
                                  AND status = 'queued'
                                LIMIT 1
                                """,
                                {"job_key": job_key},
                            )
                            still_blocked = await cur.fetchone()
                            if still_blocked is None:
                                should_remove = True
                                expired_in_db = True

        if inserted_id:
            enqueued += 1
        if should_remove:
            records.pop(dedup_key, None)
            changed = True
        if expired_in_db:
            await _durable_publish_data_job_monitor(
                job_key=job_key,
                run_id=source_run_id,
                terminal_status="aborted",
                failure_reason=(
                    f"takeover_retry_expired: 补跑入队时已过截止时间"
                    f"（时段 {slot_dt.isoformat()}，截止 {dl_dt.isoformat()}，不再补跑）"
                ),
                paths=paths,
            )

    if changed:
        try:
            _write_json_store(store_path, records)
        except Exception as exc:
            logger.warning("Failed updating pending takeover retries store: %s", exc)
    return enqueued


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def _format_takeover_abort_detail(
    *,
    scheduled_slot_utc: datetime | None,
    deadline_utc: datetime | None,
    scheduled_rerun_id: str | None = None,
    retry_exhausted: bool = False,
    blocked_by_queued_run_id: str | None = None,
    slot_still_valid: bool = False,
) -> str:
    has_slot_window = scheduled_slot_utc is not None and deadline_utc is not None
    if scheduled_rerun_id and scheduled_slot_utc is not None:
        return (
            f"scheduler_takeover_aborted: 调度器接管终止"
            f"（时段 {scheduled_slot_utc.isoformat()} 仍在有效期内，已安排补跑 {scheduled_rerun_id}）"
        )
    if blocked_by_queued_run_id and scheduled_slot_utc is not None:
        return (
            f"scheduler_takeover_aborted: 调度器接管终止"
            f"（时段 {scheduled_slot_utc.isoformat()} 因已有排队任务 {blocked_by_queued_run_id} 占槽，已登记待补跑，空槽后在截止前自动补入队）"
        )
    if retry_exhausted and scheduled_slot_utc is not None:
        return (
            f"scheduler_takeover_aborted: 调度器接管终止"
            f"（时段 {scheduled_slot_utc.isoformat()} 已达补跑上限，每时段最多 1 次）"
        )
    if (
        has_slot_window
        and not slot_still_valid
        and scheduled_slot_utc is not None
        and deadline_utc is not None
    ):
        return (
            f"scheduler_takeover_aborted: 调度器接管终止"
            f"（时段 {scheduled_slot_utc.isoformat()} 已过期，截止 {deadline_utc.isoformat()}，不再补跑）"
        )
    return "scheduler_takeover_aborted"


async def _enqueue_takeover_retry_in_tx(
    cur: Any,
    *,
    job_key: str,
    run_trigger_raw: Any,
    triggered_by_user_id: Any,
    scheduled_slot_utc: datetime | None,
    deadline_utc: datetime | None,
    def_enabled: bool,
    def_paused: bool,
    def_deleted_at: Any,
    decision_now_utc: datetime,
    use_db_clock: bool = False,
    source_run_id: str | None = None,
    deferred_retries_out: list[dict[str, Any]] | None = None,
) -> str:
    """Inside the abort transaction, atomically check the slot deadline (using DB ``NOW()``
    in production) and enqueue at most one same-slot takeover rerun, distinguishing
    already-retried, DB-clock-expired, and blocked-by-queued (recording a deferred intent).

    Returns the formatted ``abort_failure_detail`` for monitor registration.
    """
    already_takeover_retry = (
        str(triggered_by_user_id or "") == TAKEOVER_RETRY_TRIGGERED_BY
    )
    is_cron_run = str(run_trigger_raw or "cron") == "cron"
    has_slot_window = scheduled_slot_utc is not None and deadline_utc is not None
    slot_still_valid = bool(
        has_slot_window and deadline_utc is not None and decision_now_utc < deadline_utc
    )

    scheduled_rerun_id: str | None = None
    retry_exhausted = already_takeover_retry
    blocked_by_queued_run_id: str | None = None

    if (
        slot_still_valid
        and is_cron_run
        and not already_takeover_retry
        and def_enabled
        and not def_paused
        and def_deleted_at is None
        and scheduled_slot_utc is not None
        and deadline_utc is not None
    ):
        await cur.execute(
            """
            SELECT run_id
            FROM dalton_data_task_runs
            WHERE job_key = %(job_key)s
              AND scheduled_slot_utc = %(slot_utc)s
              AND triggered_by_user_id = %(triggered_by_user_id)s
            LIMIT 1
            """,
            {
                "job_key": job_key,
                "slot_utc": scheduled_slot_utc,
                "triggered_by_user_id": TAKEOVER_RETRY_TRIGGERED_BY,
            },
        )
        prior_retry = await cur.fetchone()
        if prior_retry is not None:
            retry_exhausted = True
        else:
            rerun_id = str(uuid4())
            deadline_where_sql = (
                "NOW() < %(deadline_utc)s"
                if use_db_clock
                else "%(decision_now_utc)s < %(deadline_utc)s"
            )
            await cur.execute(
                f"""
                INSERT INTO dalton_data_task_runs (
                    run_id,
                    job_key,
                    trigger_source,
                    triggered_by_user_id,
                    scheduled_slot_utc,
                    deadline_utc,
                    status,
                    created_at,
                    updated_at
                )
                SELECT
                    %(run_id)s::uuid,
                    %(job_key)s,
                    'manual',
                    %(triggered_by_user_id)s,
                    %(slot_utc)s,
                    %(deadline_utc)s,
                    'queued',
                    NOW(),
                    NOW()
                WHERE {deadline_where_sql}
                ON CONFLICT DO NOTHING
                RETURNING run_id
                """,
                {
                    "run_id": rerun_id,
                    "job_key": job_key,
                    "triggered_by_user_id": TAKEOVER_RETRY_TRIGGERED_BY,
                    "slot_utc": scheduled_slot_utc,
                    "deadline_utc": deadline_utc,
                    "decision_now_utc": decision_now_utc,
                },
            )
            inserted = await cur.fetchone()
            if inserted is not None:
                scheduled_rerun_id = str(_extract_field(inserted, "run_id", 0))
            else:
                # Distinguish DB-clock expiry while waiting for lock vs blocked by another queued run.
                await cur.execute(
                    """
                    SELECT run_id, trigger_source, triggered_by_user_id, scheduled_slot_utc
                    FROM dalton_data_task_runs
                    WHERE job_key = %(job_key)s
                      AND status = 'queued'
                    LIMIT 1
                    """,
                    {"job_key": job_key},
                )
                blocking_row = await cur.fetchone()
                if blocking_row is not None:
                    b_user = str(_extract_field(blocking_row, "triggered_by_user_id", 2) or "")
                    b_slot = _coerce_utc_datetime(_extract_field(blocking_row, "scheduled_slot_utc", 3))
                    b_run_id = str(_extract_field(blocking_row, "run_id", 0) or "")
                    if b_user == TAKEOVER_RETRY_TRIGGERED_BY and b_slot == scheduled_slot_utc:
                        retry_exhausted = True
                    elif b_run_id:
                        blocked_by_queued_run_id = b_run_id
                        if deferred_retries_out is not None:
                            deferred_retries_out.append(
                                {
                                    "job_key": job_key,
                                    "scheduled_slot_utc": scheduled_slot_utc,
                                    "deadline_utc": deadline_utc,
                                    "source_run_id": str(source_run_id or ""),
                                    "blocked_by_run_id": b_run_id,
                                }
                            )
                elif use_db_clock:
                    # No queued row blocked the insert -> WHERE NOW() < deadline_utc filtered it out.
                    slot_still_valid = False
                else:
                    retry_exhausted = True

    return _format_takeover_abort_detail(
        scheduled_slot_utc=scheduled_slot_utc,
        deadline_utc=deadline_utc,
        scheduled_rerun_id=scheduled_rerun_id,
        retry_exhausted=retry_exhausted,
        blocked_by_queued_run_id=blocked_by_queued_run_id,
        slot_still_valid=slot_still_valid,
    )


async def _reconstruct_takeover_abort_detail_readonly(
    conn: Any,
    *,
    job_key: str,
    run_trigger_raw: Any,
    triggered_by_user_id: Any,
    scheduled_slot_utc: datetime | None,
    deadline_utc: datetime | None,
    decision_now_utc: datetime,
) -> str:
    """Read-only reconstruction of monitor abort detail for replayable registration after restart."""
    already_takeover_retry = (
        str(triggered_by_user_id or "") == TAKEOVER_RETRY_TRIGGERED_BY
    )
    has_slot_window = scheduled_slot_utc is not None and deadline_utc is not None
    slot_still_valid = bool(
        has_slot_window and deadline_utc is not None and decision_now_utc < deadline_utc
    )
    scheduled_rerun_id: str | None = None
    retry_exhausted = already_takeover_retry
    blocked_by_queued_run_id: str | None = None

    if scheduled_slot_utc is not None and not already_takeover_retry:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT run_id
                FROM dalton_data_task_runs
                WHERE job_key = %(job_key)s
                  AND scheduled_slot_utc = %(slot_utc)s
                  AND triggered_by_user_id = %(triggered_by_user_id)s
                LIMIT 1
                """,
                {
                    "job_key": job_key,
                    "slot_utc": scheduled_slot_utc,
                    "triggered_by_user_id": TAKEOVER_RETRY_TRIGGERED_BY,
                },
            )
            existing_retry = await cur.fetchone()
            if existing_retry is not None:
                scheduled_rerun_id = str(_extract_field(existing_retry, "run_id", 0))
            elif slot_still_valid and str(run_trigger_raw or "cron") == "cron":
                await cur.execute(
                    """
                    SELECT run_id
                    FROM dalton_data_task_runs
                    WHERE job_key = %(job_key)s
                      AND status = 'queued'
                    LIMIT 1
                    """,
                    {"job_key": job_key},
                )
                queued_row = await cur.fetchone()
                if queued_row is not None:
                    blocked_by_queued_run_id = str(_extract_field(queued_row, "run_id", 0))

    return _format_takeover_abort_detail(
        scheduled_slot_utc=scheduled_slot_utc,
        deadline_utc=deadline_utc,
        scheduled_rerun_id=scheduled_rerun_id,
        retry_exhausted=retry_exhausted,
        blocked_by_queued_run_id=blocked_by_queued_run_id,
        slot_still_valid=slot_still_valid,
    )


async def _publish_takeover_abort_monitor(
    *,
    job_key: str,
    run_id: str,
    failure_reason: str,
    paths: DataTaskPathMap,
) -> None:
    """Register takeover-abort publish failure durably so failures survive slot reset and restarts."""
    # DALTON-HOOK(monitors): record takeover-abort publish failure for any monitor bound to this job_key.
    await _durable_publish_data_job_monitor(
        job_key=job_key,
        run_id=str(run_id),
        terminal_status="aborted",
        failure_reason=failure_reason,
        paths=paths,
    )


async def recover_on_takeover(
    conn: Any,
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient | None = None,
    host_client: DataTaskHostBridgeClient | None = None,
    path_map: DataTaskPathMap | None = None,
    now_utc: datetime | None = None,
) -> str | None:
    """Reconcile active slot, leftover containers, and in-flight host tasks on scheduler startup/takeover (§3.4.2).

    Contract:
    - Every external Docker or host-bridge call is preceded by `check_scheduler_lock(lock_conn)`.
    - Running containers must be confirmed stopped (`Running == False`) and removed before any terminal
      `aborted` status is written or the execution slot is released to `'idle'`.
    - For a non-terminal `runner = 'host'` run, `dalton_data_task_slot.generation` is incremented in a
      transaction (after lock check) and `host-kill` is called with the new generation; only after
      `host-kill` confirms all processes exited (`confirmed == True`) is `aborted` written and the slot
      released. Otherwise the slot is kept in `'reclaiming'` for retry on the next tick.
    - Marking `aborted` and enqueueing any same-slot takeover rerun execute inside the same DB
      transaction using the post-stop wall clock so a crash between abort and enqueue cannot lose the
      rerun.
    - If the run referenced by ``dalton_data_task_slot.active_run_id`` is ALREADY in a terminal
      status (`succeeded`, `failed`, `timed_out`, `aborted`, `skipped_low_memory`,
      `skipped_superseded`, `cancelled`), **do NOT modify the run row, do NOT increment
      `consecutive_failures`, and do NOT change `notify_status`** — only clean up leftover
      containers, secrets directory, replay monitor registration if the terminal row is a
      `scheduler_takeover_aborted` abort, and reset `dalton_data_task_slot` to `'idle'`.
    """
    paths = path_map or build_path_map()

    def _decision_now() -> datetime:
        return (
            now_utc.astimezone(timezone.utc)
            if now_utc is not None
            else datetime.now(timezone.utc)
        )

    await check_scheduler_lock(lock_conn)

    all_confirmed_stopped = True
    leftover_containers: list[dict[str, Any]] = []
    if docker_client is not None:
        leftover_containers = await docker_client.list_data_task_containers()
        for item in leftover_containers:
            cid = str(item.get("container_id") or "")
            if cid and item.get("running"):
                stopped_ok = await _stop_and_confirm_container(
                    lock_conn=lock_conn,
                    docker_client=docker_client,
                    container_id=cid,
                )
                if not stopped_ok:
                    all_confirmed_stopped = False

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT slot_id, generation, slot_state, active_run_id, active_job_key
            FROM dalton_data_task_slot
            WHERE slot_id = 1
            """
        )
        slot_row = await cur.fetchone()

    slot_state = str(_extract_field(slot_row, "slot_state", 2)) if slot_row else "idle"
    active_run_id_raw = _extract_field(slot_row, "active_run_id", 3) if slot_row else None
    active_run_id = str(active_run_id_raw) if active_run_id_raw else None
    recovery_outcome: str | None = None

    host_job_keys: set[str] = {"bbg-data-pull-asia", "bbg-data-pull-us", "T1", "T29"}
    try:
        for entry in load_data_task_manifest():
            if entry.runner == "host":
                host_job_keys.add(entry.job_key)
                host_job_keys.add(entry.task_id)
    except Exception:
        pass

    all_removed = True
    removed_cids: set[str] = set()
    if active_run_id:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT r.run_id, r.job_key, r.status, r.container_id, d.runner, r.trigger_source,
                       r.scheduled_slot_utc, r.deadline_utc, r.triggered_by_user_id,
                       d.enabled, d.paused_by_user, d.deleted_at, r.failure_reason
                FROM dalton_data_task_runs r
                LEFT JOIN dalton_data_task_defs d
                  ON d.job_key = r.job_key
                WHERE r.run_id = %(run_id)s::uuid
                """,
                {"run_id": active_run_id},
            )
            run_row = await cur.fetchone()

        if run_row is not None:
            run_status = str(_extract_field(run_row, "status", 2))
            job_key = str(_extract_field(run_row, "job_key", 1))
            run_cid = _extract_field(run_row, "container_id", 3)
            run_runner_raw: Any = None
            run_trigger_raw: Any = None
            run_slot_raw: Any = None
            run_deadline_raw: Any = None
            run_triggered_by_raw: Any = None
            run_failure_reason_raw: Any = None
            def_enabled = True
            def_paused = False
            def_deleted_at: Any = None
            if isinstance(run_row, Mapping):
                run_runner_raw = run_row.get("runner")
                run_trigger_raw = run_row.get("trigger_source")
                run_slot_raw = run_row.get("scheduled_slot_utc")
                run_deadline_raw = run_row.get("deadline_utc")
                run_triggered_by_raw = run_row.get("triggered_by_user_id")
                run_failure_reason_raw = run_row.get("failure_reason")
                if "enabled" in run_row and run_row.get("enabled") is not None:
                    def_enabled = bool(run_row.get("enabled"))
                if "paused_by_user" in run_row and run_row.get("paused_by_user") is not None:
                    def_paused = bool(run_row.get("paused_by_user"))
                def_deleted_at = run_row.get("deleted_at")
            elif isinstance(run_row, (tuple, list)):
                if len(run_row) > 4:
                    run_runner_raw = run_row[4]
                if len(run_row) > 5:
                    run_trigger_raw = run_row[5]
                if len(run_row) > 6:
                    run_slot_raw = run_row[6]
                if len(run_row) > 7:
                    run_deadline_raw = run_row[7]
                if len(run_row) > 8:
                    run_triggered_by_raw = run_row[8]
                if len(run_row) > 9 and run_row[9] is not None:
                    def_enabled = bool(run_row[9])
                if len(run_row) > 10 and run_row[10] is not None:
                    def_paused = bool(run_row[10])
                if len(run_row) > 11:
                    def_deleted_at = run_row[11]
                if len(run_row) > 12:
                    run_failure_reason_raw = run_row[12]
            scheduled_slot_utc = _coerce_utc_datetime(run_slot_raw)
            deadline_utc = _coerce_utc_datetime(run_deadline_raw)
            is_host_run = (
                str(run_runner_raw) == "host"
                if run_runner_raw is not None
                else job_key in host_job_keys
            )
            # Takeover aborts of manual runs (trial or in-chat run-now) never change
            # consecutive_failures: an aborted manual run says nothing about the task's
            # health, and the claim-time trial/run-now distinction is not stored, so
            # re-deriving it from the current `enabled` flag would be wrong after a toggle.
            skip_failure_count = str(run_trigger_raw or "cron") == "manual"
            abort_failure_detail: str | None = None

            # Even if leftover container cleanup failed, a non-terminal host task on the slot
            # must still undergo generation+1 and host-kill before deciding slot state.
            if run_status in NON_TERMINAL_RUN_STATUSES and is_host_run:
                effective_host_client = host_client or DataTaskHostBridgeClient()
                _, kill_outcome = await bump_slot_generation_and_kill_host_task(
                    conn,
                    lock_conn=lock_conn,
                    host_client=effective_host_client,
                    run_id=active_run_id,
                    reason="scheduler_takeover",
                )
                if not kill_outcome.confirmed:
                    await check_scheduler_lock(lock_conn)
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_slot
                                SET slot_state = 'reclaiming',
                                    updated_at = NOW()
                                WHERE slot_id = 1
                                """
                            )
                    return "reclaiming"

                await check_scheduler_lock(lock_conn)
                post_kill_now = _decision_now()
                deferred_retries: list[dict[str, Any]] = []
                async with conn.transaction():
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_runs
                            SET status         = 'aborted',
                                failure_reason = 'scheduler_takeover_aborted',
                                notify_status  = 'skipped',
                                completed_at   = NOW(),
                                updated_at     = NOW()
                            WHERE run_id = %(run_id)s::uuid
                              AND status IN ('starting', 'running')
                            """,
                            {"run_id": active_run_id},
                        )
                        if cur.rowcount == 1:
                            if not skip_failure_count:
                                await cur.execute(
                                    """
                                    UPDATE dalton_data_task_defs
                                    SET consecutive_failures = consecutive_failures + 1,
                                        updated_at = NOW()
                                    WHERE job_key = %(job_key)s
                                    """,
                                    {"job_key": job_key},
                                )
                            abort_failure_detail = await _enqueue_takeover_retry_in_tx(
                                cur,
                                job_key=job_key,
                                run_trigger_raw=run_trigger_raw,
                                triggered_by_user_id=run_triggered_by_raw,
                                scheduled_slot_utc=scheduled_slot_utc,
                                deadline_utc=deadline_utc,
                                def_enabled=def_enabled,
                                def_paused=def_paused,
                                def_deleted_at=def_deleted_at,
                                decision_now_utc=post_kill_now,
                                use_db_clock=(now_utc is None),
                                source_run_id=active_run_id,
                                deferred_retries_out=deferred_retries,
                            )
                            recovery_outcome = "aborted"
                        else:
                            recovery_outcome = "cleaned_terminal"

                for item in deferred_retries:
                    _record_pending_takeover_retry(
                        paths,
                        job_key=item["job_key"],
                        scheduled_slot_utc=item["scheduled_slot_utc"],
                        deadline_utc=item["deadline_utc"],
                        source_run_id=item["source_run_id"],
                        blocked_by_run_id=item.get("blocked_by_run_id"),
                    )

                if recovery_outcome == "aborted" and abort_failure_detail is not None:
                    await _publish_takeover_abort_monitor(
                        job_key=job_key,
                        run_id=active_run_id,
                        failure_reason=abort_failure_detail,
                        paths=paths,
                    )

                if not all_confirmed_stopped:
                    await check_scheduler_lock(lock_conn)
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_slot
                                SET slot_state = 'reclaiming',
                                    updated_at = NOW()
                                WHERE slot_id = 1
                                """
                            )
                    return "reclaiming"
            else:
                if not all_confirmed_stopped:
                    await check_scheduler_lock(lock_conn)
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_slot
                                SET slot_state = 'reclaiming',
                                    updated_at = NOW()
                                WHERE slot_id = 1
                                """
                            )
                    return "reclaiming"

                if run_status in NON_TERMINAL_RUN_STATUSES:
                    await check_scheduler_lock(lock_conn)
                    post_stop_now = _decision_now()
                    deferred_retries = []
                    async with conn.transaction():
                        async with conn.cursor(row_factory=dict_row) as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_runs
                                SET status         = 'aborted',
                                    failure_reason = 'scheduler_takeover_aborted',
                                    notify_status  = 'skipped',
                                    completed_at   = NOW(),
                                    updated_at     = NOW()
                                WHERE run_id = %(run_id)s::uuid
                                  AND status IN ('starting', 'running')
                                """,
                                {"run_id": active_run_id},
                            )
                            if cur.rowcount == 1:
                                if not skip_failure_count:
                                    await cur.execute(
                                        """
                                        UPDATE dalton_data_task_defs
                                        SET consecutive_failures = consecutive_failures + 1,
                                            updated_at = NOW()
                                        WHERE job_key = %(job_key)s
                                        """,
                                        {"job_key": job_key},
                                    )
                                abort_failure_detail = await _enqueue_takeover_retry_in_tx(
                                    cur,
                                    job_key=job_key,
                                    run_trigger_raw=run_trigger_raw,
                                    triggered_by_user_id=run_triggered_by_raw,
                                    scheduled_slot_utc=scheduled_slot_utc,
                                    deadline_utc=deadline_utc,
                                    def_enabled=def_enabled,
                                    def_paused=def_paused,
                                    def_deleted_at=def_deleted_at,
                                    decision_now_utc=post_stop_now,
                                    use_db_clock=(now_utc is None),
                                    source_run_id=active_run_id,
                                    deferred_retries_out=deferred_retries,
                                )
                                recovery_outcome = "aborted"
                            else:
                                recovery_outcome = "cleaned_terminal"

                    for item in deferred_retries:
                        _record_pending_takeover_retry(
                            paths,
                            job_key=item["job_key"],
                            scheduled_slot_utc=item["scheduled_slot_utc"],
                            deadline_utc=item["deadline_utc"],
                            source_run_id=item["source_run_id"],
                            blocked_by_run_id=item.get("blocked_by_run_id"),
                        )

                    if recovery_outcome == "aborted" and abort_failure_detail is not None:
                        await _publish_takeover_abort_monitor(
                            job_key=job_key,
                            run_id=active_run_id,
                            failure_reason=abort_failure_detail,
                            paths=paths,
                        )
                else:
                    # Already terminal: do NOT alter dalton_data_task_runs or consecutive_failures!
                    # However, if this terminal row is a prior scheduler_takeover_aborted whose process
                    # restarted before monitor registration completed, replay the monitor registration.
                    recovery_outcome = "cleaned_terminal"
                    if run_status == "aborted" and str(
                        run_failure_reason_raw or ""
                    ).startswith("scheduler_takeover_aborted"):
                        replay_detail = await _reconstruct_takeover_abort_detail_readonly(
                            conn,
                            job_key=job_key,
                            run_trigger_raw=run_trigger_raw,
                            triggered_by_user_id=run_triggered_by_raw,
                            scheduled_slot_utc=scheduled_slot_utc,
                            deadline_utc=deadline_utc,
                            decision_now_utc=_decision_now(),
                        )
                        await _publish_takeover_abort_monitor(
                            job_key=job_key,
                            run_id=active_run_id,
                            failure_reason=replay_detail,
                            paths=paths,
                        )

            if run_cid and docker_client is not None:
                cid_str = str(run_cid)
                removed_cids.add(cid_str)
                await check_scheduler_lock(lock_conn)
                try:
                    await docker_client.remove_container(cid_str, force=True)
                except Exception as exc:
                    if not is_container_not_found_error(exc):
                        all_removed = False
        elif not all_confirmed_stopped:
            await check_scheduler_lock(lock_conn)
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_data_task_slot
                        SET slot_state = 'reclaiming',
                            updated_at = NOW()
                        WHERE slot_id = 1
                        """
                    )
            return "reclaiming"
    elif not all_confirmed_stopped:
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state = 'reclaiming',
                        updated_at = NOW()
                    WHERE slot_id = 1
                    """
                )
        return "reclaiming"

    if docker_client is not None:
        for item in leftover_containers:
            cid = str(item.get("container_id") or "")
            if cid and cid not in removed_cids:
                removed_cids.add(cid)
                await check_scheduler_lock(lock_conn)
                try:
                    await docker_client.remove_container(cid, force=True)
                except Exception as exc:
                    if not is_container_not_found_error(exc):
                        all_removed = False

    if not all_removed:
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state = 'reclaiming',
                        updated_at = NOW()
                    WHERE slot_id = 1
                    """
                )
        return "reclaiming"

    if active_run_id:
        cleanup_run_secrets_dir(active_run_id, path_map=paths)

    if active_run_id or slot_state != "idle":
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state     = 'idle',
                        active_run_id  = NULL,
                        active_job_key = NULL,
                        started_at     = NULL,
                        updated_at     = NOW()
                    WHERE slot_id = 1
                    """
                )

    secrets_root = paths.backend_var_dir / "run-secrets"
    if secrets_root.is_dir():
        for child in secrets_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    return recovery_outcome


async def execute_claimed_container_run(
    conn: Any,
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient,
    claimed: ClaimedTaskRun,
    decrypted_secrets: Mapping[str, str] | None = None,
    path_map: DataTaskPathMap | None = None,
) -> tuple[bool, int | None]:
    """Run the full container lifecycle (`create` -> `UPDATE starting->running` -> `start` -> monitor -> evaluate -> ledger -> `rm`)."""
    paths = path_map or build_path_map()
    started_at = datetime.now(timezone.utc)
    container_id = await start_claimed_container_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        decrypted_secrets=decrypted_secrets,
        path_map=paths,
    )
    if container_id is None:
        return False, None

    outcome, timed_out = await monitor_container_until_exit(
        lock_conn=lock_conn,
        docker_client=docker_client,
        container_id=container_id,
        timeout_seconds=claimed.timeout_seconds,
        no_output_timeout_seconds=claimed.no_output_timeout_seconds,
    )

    try:
        task_tz = ZoneInfo(claimed.timezone)
    except Exception:
        task_tz = timezone.utc
    slot_dt = claimed.scheduled_slot_utc
    if slot_dt.tzinfo is None:
        slot_dt = slot_dt.replace(tzinfo=timezone.utc)
    reference_date = slot_dt.astimezone(task_tz).date()

    raw_timeout_kind = getattr(outcome, "timeout_kind", None)
    timeout_kind: TimeoutKind | None = (
        raw_timeout_kind if raw_timeout_kind in ("total", "no_output") else None
    )

    metadata_error: SafeFsError | None = None
    try:
        outbox_payload, frozen_manifest = read_run_artifacts(
            claimed.run_id,
            path_map=paths,
            strict=True,
        )
    except SafeFsError as meta_exc:
        metadata_error = meta_exc
        outbox_payload = None
        frozen_manifest = None

    evaluation = evaluate_task_run(
        TaskEvaluationContext(
            job_key=claimed.job_key,
            exit_code=outcome.exit_code,
            oom_killed=outcome.oom_killed,
            timed_out=timed_out,
            timeout_kind=timeout_kind,
            stdout=outcome.log_tail,
            data_root=paths.backend_data_root,
            started_at=started_at,
            reference_date=reference_date,
        )
    )
    if metadata_error is not None:
        evaluation = TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"invalid_run_metadata:{metadata_error.code}",
            warnings=list(evaluation.warnings),
            details={
                **evaluation.details,
                "metadata_error_code": metadata_error.code,
            },
        )
    elif (
        claimed.job_key == T27_JOB_KEY
        and evaluation.ok
        and evaluation.status == "succeeded"
        and not is_t27_outbox_prepared(outbox_payload)
    ):
        md_source = ""
        if isinstance(outbox_payload, Mapping):
            md_source = str(
                outbox_payload.get("text")
                or outbox_payload.get("feishu_message")
                or ""
            )
        if not md_source.strip():
            md_source = str(outcome.log_tail or "")
        run_art_dir = paths.backend_run_artifacts_dir(claimed.run_id)
        try:
            prepared_t27 = prepare_t27_run_outbox(
                md_source,
                workspace_dir=paths.backend_data_root,
                run_artifacts_dir=run_art_dir,
            )
            atomic_write_json(
                run_art_dir / "outbox.json",
                prepared_t27,
                anchor_dir=paths.backend_data_root,
            )
            outbox_payload = prepared_t27
        except (DataTaskNotificationPrepError, DataTaskNotificationError, SafeFsError) as prep_exc:
            outbox_payload = None
            evaluation = TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"report_preparation_failed: {prep_exc}",
                warnings=list(evaluation.warnings),
                details={**evaluation.details, "report_prep_code": prep_exc.code},
            )
    if (
        evaluation.ok
        and evaluation.status == "succeeded"
        and isinstance(outbox_payload, Mapping)
    ):
        run_art_dir = paths.backend_run_artifacts_dir(claimed.run_id)
        try:
            prepared_outbox = prepare_run_outbox_image_metadata(
                outbox_payload,
                run_artifacts_dir=run_art_dir,
                job_key=claimed.job_key,
            )
            if prepared_outbox is not None and prepared_outbox != outbox_payload:
                if (run_art_dir / "outbox.json").exists():
                    atomic_write_json(
                        run_art_dir / "outbox.json",
                        prepared_outbox,
                        anchor_dir=paths.backend_data_root,
                    )
                outbox_payload = prepared_outbox
        except (DataTaskNotificationPrepError, DataTaskNotificationError, SafeFsError) as prep_exc:
            outbox_payload = None
            prep_code = getattr(prep_exc, "code", "report_preparation_failed")
            evaluation = TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"report_preparation_failed: {prep_exc}",
                warnings=list(evaluation.warnings),
                details={**evaluation.details, "report_prep_code": prep_code},
            )
    return await finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        container_id=container_id,
        outcome=outcome,
        evaluation=evaluation,
        outbox_payload=outbox_payload,
        frozen_manifest=frozen_manifest,
        path_map=paths,
    )


_BBG_HOST_JOB_KEYS = frozenset({"bbg-data-pull-asia", "bbg-data-pull-us"})


def _manifest_host_command(job_key: str) -> tuple[list[str], str] | None:
    """Return the manifest host argv and cwd, so a stale DB row cannot drift."""
    try:
        entry = load_data_task_manifest().by_job_key().get(job_key)
    except Exception:
        return None
    if entry is None or entry.runner != "host":
        return None
    return list(entry.command_argv), str(entry.cwd)


# DALTON-HOOK(host-job-secrets): manifest plain env and log_tail *** scrubbing for host tasks
def _manifest_host_env(job_key: str) -> dict[str, str]:
    """Return non-secret manifest `env` entries for a host task."""
    try:
        entry = load_data_task_manifest().by_job_key().get(job_key)
    except Exception:
        return {}
    if entry is None or entry.runner != "host" or not entry.env:
        return {}
    return {str(k): str(v) for k, v in entry.env.items() if str(k).strip()}


def _scrub_log_tail_secrets(text: str, secret_values: Sequence[str]) -> str:
    """Mask host secret values in `log_tail` with `***` before persisting to DB."""
    if not text or not secret_values:
        return text
    patterns = sorted(
        {s for raw in secret_values for s in (str(raw or ""), str(raw or "").strip()) if len(s) >= 4},
        key=len,
        reverse=True,
    )
    out = text
    for pattern in patterns:
        if pattern in out:
            out = out.replace(pattern, "***")
    return out


async def execute_claimed_host_run(
    conn: Any,
    *,
    lock_conn: Any,
    host_client: DataTaskHostBridgeClient,
    claimed: ClaimedTaskRun,
    path_map: DataTaskPathMap | None = None,
    heartbeat_interval_seconds: float = LOCK_HEARTBEAT_INTERVAL_S,
) -> tuple[bool, int | None]:
    """Run the full host-task lifecycle (`UPDATE starting->running` -> `host-run` with lease heartbeats -> evaluate -> ledger) (§2.3 item 3, §3.2–§3.4).

    Contract:
    - Calls `host-run` synchronously while checking the scheduler advisory lock every
      `heartbeat_interval_seconds`.
    - When a timeout occurs (either returned by `host-run` or detected by the scheduler),
      first increments `dalton_data_task_slot.generation` in a transaction (after lock check)
      and calls `host-kill` with the new generation; only writes terminal `timed_out` and
      releases the slot if `host-kill` confirms all processes exited (`confirmed == True`).
      If unconfirmed, keeps the slot in `'reclaiming'` for retry on the next tick.
    - When the bridge is unreachable or auth fails, marks the run `failed` with a clear
      `failure_reason` and releases the slot without retrying the same period.
    """
    paths = path_map or build_path_map()
    started_at = datetime.now(timezone.utc)
    started = await start_claimed_host_run(
        conn,
        lock_conn=lock_conn,
        claimed=claimed,
    )
    if not started:
        return False, None

    host_mode = "run" if claimed.is_trial else claimed.host_mode
    if not host_mode:
        try:
            for entry in load_data_task_manifest():
                if entry.job_key == claimed.job_key and entry.host_mode:
                    host_mode = entry.host_mode
                    break
        except Exception:
            pass
    effective_host_mode = host_mode or "mirror"

    slot_dt = claimed.scheduled_slot_utc
    if slot_dt.tzinfo is None:
        slot_dt = slot_dt.replace(tzinfo=timezone.utc)
    scheduled_slot_iso = slot_dt.astimezone(timezone.utc).isoformat()

    # DALTON-HOOK(host-job-secrets): resolve Vault secrets from primary PM admin for host runs
    host_secrets: dict[str, str] = {}
    declared_secret_names = [
        str(s).strip() for s in (claimed.required_secrets or ()) if str(s).strip()
    ]
    if declared_secret_names:
        resolved = await resolve_task_vault_secrets(declared_secret_names)
        missing_secrets = [
            name for name in declared_secret_names if not str(resolved.get(name) or "")
        ]
        if missing_secrets:
            logger.warning(
                "Host task %s (run_id=%s) missing Vault secret(s): %s",
                claimed.job_key,
                claimed.run_id,
                ", ".join(missing_secrets),
            )
        host_secrets = {
            name: str(resolved[name])
            for name in declared_secret_names
            if resolved.get(name) not in (None, "")
        }
    host_plain_env = _manifest_host_env(claimed.job_key)
    host_secret_values = tuple(host_secrets.values())

    async def _run_once_with_heartbeats(gen: int) -> tuple[HostRunOutcome | None, bool]:
        await check_scheduler_lock(lock_conn)
        host_command_kwargs: dict[str, Any] = {}
        if claimed.job_key not in _BBG_HOST_JOB_KEYS:
            manifest_command = _manifest_host_command(claimed.job_key)
            if manifest_command is not None:
                host_command_kwargs["command_argv"] = manifest_command[0]
                host_command_kwargs["cwd"] = manifest_command[1]
            elif claimed.command_argv:
                host_command_kwargs["command_argv"] = list(claimed.command_argv)
                host_command_kwargs["cwd"] = claimed.workdir
        if host_secrets:
            host_command_kwargs["secrets"] = dict(host_secrets)
        if host_plain_env:
            host_command_kwargs["env"] = dict(host_plain_env)
        run_coro = host_client.run_host_task(
            generation=gen,
            run_id=claimed.run_id,
            job_key=claimed.job_key,
            host_mode=effective_host_mode,
            scheduled_slot_utc=scheduled_slot_iso,
            timeout=claimed.timeout_seconds,
            no_output_timeout=claimed.no_output_timeout_seconds,
            **host_command_kwargs,
        )
        run_task: asyncio.Task[HostRunOutcome] = asyncio.create_task(run_coro)
        timed_out_local = False
        outcome_local: HostRunOutcome | None = None
        effective_wait_timeout = float(claimed.timeout_seconds) + 30.0
        start_mono = time.monotonic()

        try:
            while not run_task.done():
                elapsed = time.monotonic() - start_mono
                remaining = effective_wait_timeout - elapsed
                if remaining <= 0:
                    timed_out_local = True
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
                    break
                wait_step = min(heartbeat_interval_seconds, remaining)
                done, _ = await asyncio.wait({run_task}, timeout=wait_step)
                if not done:
                    await check_scheduler_lock(lock_conn)
            if not timed_out_local:
                try:
                    outcome_local = await run_task
                except (asyncio.TimeoutError, TimeoutError):
                    timed_out_local = True
                except HostBridgeError as exc:
                    outcome_local = HostRunOutcome(
                        ok=False,
                        status_code=409 if exc.code == "fence_lost" else 0,
                        exit_code=None,
                        timed_out=False,
                        timeout_kind=None,
                        log_tail="",
                        failure_reason=exc.code if exc.code in ("bridge_unreachable", "bridge_result_unknown", "fence_lost") else str(exc),
                        bridge_unreachable=(exc.code == "bridge_unreachable"),
                        result_unknown=(exc.code == "bridge_result_unknown"),
                        fence_lost=(exc.code == "fence_lost"),
                        auth_failed=(exc.code in ("bridge_unauthorized", "bridge_forbidden")),
                    )
        except BaseException:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            raise
        return outcome_local, timed_out_local

    host_outcome, scheduler_timed_out = await _run_once_with_heartbeats(claimed.generation)
    if host_outcome is not None and host_outcome.log_tail and host_secret_values:
        host_outcome.log_tail = _scrub_log_tail_secrets(host_outcome.log_tail, host_secret_values)

    # On `fence_lost`: bump generation + 1 in tx, call host-kill, and if confirmed retry host-run once.
    if (
        not scheduler_timed_out
        and host_outcome is not None
        and (
            host_outcome.fence_lost
            or host_outcome.failure_reason in ("fence_lost", "host_bridge_error:fence_lost")
        )
    ):
        new_gen, fence_kill_outcome = await bump_slot_generation_and_kill_host_task(
            conn,
            lock_conn=lock_conn,
            host_client=host_client,
            run_id=claimed.run_id,
            reason="fence_lost",
        )
        if not fence_kill_outcome.confirmed:
            evaluation = TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason="fence_lost",
            )
            container_like_outcome = ContainerInspectionOutcome(
                container_id=f"host:{claimed.run_id}",
                running=True,
                exit_code=None,
                oom_killed=False,
                log_tail=host_outcome.log_tail or "",
            )
            return await finalize_task_run(
                conn,
                lock_conn=lock_conn,
                docker_client=None,
                claimed=claimed,
                container_id=None,
                outcome=container_like_outcome,
                evaluation=evaluation,
                path_map=paths,
            )

        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET generation = %(generation)s,
                        updated_at = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND status IN ('starting', 'running')
                    """,
                    {"run_id": claimed.run_id, "generation": new_gen},
                )
        claimed.generation = new_gen
        host_outcome, scheduler_timed_out = await _run_once_with_heartbeats(new_gen)
        if host_outcome is not None and host_outcome.log_tail and host_secret_values:
            host_outcome.log_tail = _scrub_log_tail_secrets(host_outcome.log_tail, host_secret_values)

    is_timeout = scheduler_timed_out or (
        host_outcome is not None
        and (
            host_outcome.timed_out
            or host_outcome.exit_code in (EXIT_CODE_TOTAL_TIMEOUT, EXIT_CODE_NO_OUTPUT_TIMEOUT)
        )
    )

    if is_timeout:
        raw_kind = host_outcome.timeout_kind if host_outcome is not None else None
        if raw_kind not in ("total", "no_output"):
            if (
                host_outcome is not None
                and host_outcome.exit_code == EXIT_CODE_NO_OUTPUT_TIMEOUT
            ):
                raw_kind = "no_output"
            else:
                raw_kind = "total"
        timeout_kind: TimeoutKind = "no_output" if raw_kind == "no_output" else "total"
        exit_code = (
            host_outcome.exit_code
            if host_outcome is not None and host_outcome.exit_code is not None
            else (
                EXIT_CODE_NO_OUTPUT_TIMEOUT
                if timeout_kind == "no_output"
                else EXIT_CODE_TOTAL_TIMEOUT
            )
        )
        log_tail = host_outcome.log_tail if host_outcome is not None else ""

        _, kill_outcome = await bump_slot_generation_and_kill_host_task(
            conn,
            lock_conn=lock_conn,
            host_client=host_client,
            run_id=claimed.run_id,
            reason="host_task_timeout",
        )
        still_running = not bool(kill_outcome.confirmed)
        evaluation = TaskEvaluationResult(
            ok=False,
            status="timed_out",
            failure_reason=(
                "no_output_timeout_exceeded"
                if timeout_kind == "no_output"
                else "total_timeout_exceeded"
            ),
        )
        container_like_outcome = ContainerInspectionOutcome(
            container_id=f"host:{claimed.run_id}",
            running=still_running,
            exit_code=exit_code,
            oom_killed=False,
            log_tail=log_tail,
            timeout_kind=timeout_kind,
        )
        return await finalize_task_run(
            conn,
            lock_conn=lock_conn,
            docker_client=None,
            claimed=claimed,
            container_id=None,
            outcome=container_like_outcome,
            evaluation=evaluation,
            path_map=paths,
        )

    assert host_outcome is not None

    # Error after request was sent (`result_unknown` / `bridge_result_unknown`):
    # bump generation + 1 in tx, call host-kill, and only mark `failed` + release slot once confirmed.
    # A 200 reply whose process group the bridge could not confirm gone takes
    # the same path: the slot is freed only after host-kill confirms.
    exit_unconfirmed = host_outcome.status_code == 200 and not host_outcome.exit_confirmed
    if (
        host_outcome.result_unknown
        or host_outcome.failure_reason == "bridge_result_unknown"
        or exit_unconfirmed
    ):
        unknown_reason = "host_exit_unconfirmed" if exit_unconfirmed and not host_outcome.result_unknown else "bridge_result_unknown"
        _, unknown_kill_outcome = await bump_slot_generation_and_kill_host_task(
            conn,
            lock_conn=lock_conn,
            host_client=host_client,
            run_id=claimed.run_id,
            reason=unknown_reason,
        )
        still_running = not bool(unknown_kill_outcome.confirmed)
        evaluation = TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=unknown_reason,
        )
        container_like_outcome = ContainerInspectionOutcome(
            container_id=f"host:{claimed.run_id}",
            running=still_running,
            exit_code=host_outcome.exit_code,
            oom_killed=False,
            log_tail=host_outcome.log_tail or "",
        )
        return await finalize_task_run(
            conn,
            lock_conn=lock_conn,
            docker_client=None,
            claimed=claimed,
            container_id=None,
            outcome=container_like_outcome,
            evaluation=evaluation,
            path_map=paths,
        )

    if (
        host_outcome.bridge_unreachable
        or host_outcome.fence_lost
        or host_outcome.auth_failed
        or host_outcome.status_code != 200
    ):
        if host_outcome.auth_failed:
            reason = host_outcome.failure_reason or "host_bridge_auth_failed"
        elif host_outcome.bridge_unreachable:
            reason = host_outcome.failure_reason or "bridge_unreachable"
        elif host_outcome.fence_lost:
            reason = "fence_lost"
        else:
            reason = host_outcome.failure_reason or f"host_bridge_http_{host_outcome.status_code}"
        evaluation = TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=reason,
        )
        container_like_outcome = ContainerInspectionOutcome(
            container_id=f"host:{claimed.run_id}",
            running=False,
            exit_code=host_outcome.exit_code,
            oom_killed=False,
            log_tail=host_outcome.log_tail or "",
        )
        return await finalize_task_run(
            conn,
            lock_conn=lock_conn,
            docker_client=None,
            claimed=claimed,
            container_id=None,
            outcome=container_like_outcome,
            evaluation=evaluation,
            path_map=paths,
        )

    try:
        task_tz = ZoneInfo(claimed.timezone)
    except Exception:
        task_tz = timezone.utc
    reference_date = slot_dt.astimezone(task_tz).date()

    # The bridge leaves the redacted outbox at `.dalton-runs/<run_id>/outbox.json`,
    # as a container run does; without reading it here every scheduled host run
    # finalized with no payload, so notify_status was always `skipped` and a host
    # task with notify_enabled (weekly-token-usage, 2026-11-21) never sent its card.
    metadata_error: SafeFsError | None = None
    try:
        outbox_payload, frozen_manifest = read_run_artifacts(
            claimed.run_id,
            path_map=paths,
            strict=True,
        )
    except SafeFsError as meta_exc:
        metadata_error = meta_exc
        outbox_payload = None
        frozen_manifest = None

    evaluation = evaluate_task_run(
        TaskEvaluationContext(
            job_key=claimed.job_key,
            exit_code=host_outcome.exit_code,
            oom_killed=False,
            timed_out=False,
            timeout_kind=None,
            stdout=host_outcome.log_tail,
            data_root=paths.backend_data_root,
            started_at=started_at,
            reference_date=reference_date,
        )
    )
    if metadata_error is not None:
        evaluation = TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"invalid_run_metadata:{metadata_error.code}",
            warnings=list(evaluation.warnings),
            details={
                **evaluation.details,
                "metadata_error_code": metadata_error.code,
            },
        )
    container_like_outcome = ContainerInspectionOutcome(
        container_id=f"host:{claimed.run_id}",
        running=False,
        exit_code=host_outcome.exit_code,
        oom_killed=False,
        log_tail=host_outcome.log_tail or "",
    )
    return await finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=None,
        claimed=claimed,
        container_id=None,
        outcome=container_like_outcome,
        evaluation=evaluation,
        outbox_payload=outbox_payload,
        frozen_manifest=frozen_manifest,
        path_map=paths,
    )


def build_data_task_notify_automation(job_key: str) -> dict[str, Any]:
    """Construct the ``automation`` dict for an OC-1 data task notification (§5.1 item 2)."""
    cleaned = str(job_key).strip()
    if cleaned.lower().startswith("data:"):
        cleaned = cleaned[5:].strip()
    data_key = f"data:{cleaned}"
    return {
        "automation_id": data_key,
        "name": data_key,
        "route_key": data_key,
        "metadata": {"dalton_kind": "data_task"},
    }


_STDOUT_NOTIFY_FALLBACK_JOB_KEYS: frozenset[str] = frozenset(
    {
        "asia-leverage-crossview-daily",
        "quantum-ew-monitor",
        "T27",
        "T32",
    }
)


def _resolve_run_notify_payload(
    run_id: str,
    job_key: str,
    *,
    db_outbox_payload: Any = None,
    log_tail: str | None = None,
    path_map: DataTaskPathMap | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Resolve ``(text, card, payload_with_route_key)`` with DB precedence over bounded disk fallback.

    Persisted ``db_outbox_payload`` is authoritative and performs zero disk metadata reads.
    When absent, reads only ``outbox.json`` (never ``frozen_manifest.json``) with bounded,
    descriptor-based validation. Only T27 (``asia-leverage-crossview-daily``) and T32
    (``quantum-ew-monitor``) may fall back to ``log_tail`` when ``outbox.json`` is genuinely absent.
    """
    cleaned_key = str(job_key).strip()
    if cleaned_key.lower().startswith("data:"):
        cleaned_key = cleaned_key[5:].strip()
    data_key = f"data:{cleaned_key}"

    raw_payload: dict[str, Any] | None = None
    if isinstance(db_outbox_payload, Mapping) and db_outbox_payload:
        raw_payload = dict(db_outbox_payload)
    elif isinstance(db_outbox_payload, str) and db_outbox_payload.strip():
        try:
            loaded = json.loads(db_outbox_payload)
            if isinstance(loaded, Mapping) and loaded:
                raw_payload = dict(loaded)
            elif not isinstance(loaded, Mapping):
                raw_payload = {"text": db_outbox_payload.strip()}
        except Exception:
            raw_payload = {"text": db_outbox_payload.strip()}

    metadata_error_code: str | None = None
    if raw_payload is None:
        paths = path_map or build_path_map()
        try:
            disk_outbox = read_validated_run_json_metadata(
                str(run_id),
                "outbox.json",
                path_map=paths,
            )
            if isinstance(disk_outbox, Mapping) and disk_outbox:
                raw_payload = dict(disk_outbox)
            elif isinstance(disk_outbox, Mapping):
                metadata_error_code = "invalid_metadata"
        except SafeFsError as meta_exc:
            metadata_error_code = meta_exc.code

    if (
        raw_payload is None
        and metadata_error_code is None
        and (
            cleaned_key in _STDOUT_NOTIFY_FALLBACK_JOB_KEYS
            or resolve_task_payload_source(cleaned_key) in ("stdout_markdown", "stdout_text")
        )
    ):
        if log_tail and log_tail.strip():
            parsed_stdout = extract_last_json_object(log_tail)
            if isinstance(parsed_stdout, Mapping):
                raw_payload = dict(parsed_stdout)
            else:
                raw_payload = {"text": log_tail.strip()}

    outbox_missing = raw_payload is None and metadata_error_code is None
    if raw_payload is None:
        raw_payload = {}

    feishu_cards = extract_feishu_cards_from_payload(raw_payload)

    card: dict[str, Any] | None = None
    if isinstance(raw_payload.get("card"), Mapping):
        card = dict(raw_payload["card"])
    elif "header" in raw_payload and (
        "body" in raw_payload
        or "elements" in raw_payload
        or "schema" in raw_payload
    ):
        card = dict(raw_payload)

    text = ""
    for candidate_field in (
        "text",
        "feishu_message",
        "report_text",
        "message",
        "summary",
    ):
        val = raw_payload.get(candidate_field)
        if isinstance(val, str) and val.strip():
            text = val.strip()
            break

    if not text and feishu_cards:
        first_card = feishu_cards[0].get("card")
        if isinstance(first_card, Mapping):
            header = first_card.get("header")
            if isinstance(header, Mapping):
                title = header.get("title")
                if isinstance(title, Mapping) and isinstance(title.get("content"), str):
                    text = title["content"].strip()
        if not text:
            text = f"数据任务：{cleaned_key}"

    if not text and card is not None:
        header = card.get("header")
        if isinstance(header, Mapping):
            title = header.get("title")
            if isinstance(title, Mapping) and isinstance(title.get("content"), str):
                text = title["content"].strip()
        if not text:
            text = f"数据任务：{job_key}"

    if not text and isinstance(raw_payload.get("items"), list) and raw_payload["items"]:
        first_item = raw_payload["items"][0]
        if isinstance(first_item, Mapping) and isinstance(first_item.get("text"), str):
            text = first_item["text"].strip()
        if not text:
            text = f"数据任务：{cleaned_key}"

    if not text and raw_payload and not feishu_cards:
        text = json.dumps(raw_payload, ensure_ascii=False)

    if metadata_error_code is None and not text and card is None and not feishu_cards:
        outbox_missing = True

    if feishu_cards:
        expected_item_count = len(feishu_cards)
    elif isinstance(raw_payload.get("items"), list) and raw_payload["items"]:
        expected_item_count = len(raw_payload["items"])
    elif not outbox_missing and metadata_error_code is None:
        expected_item_count = 1
    else:
        expected_item_count = 0

    payload_with_route_key: dict[str, Any] = {
        **raw_payload,
        "text": text,
        "card": card,
        "category": "data_task",
        "route_key": data_key,
        "automation_id": data_key,
        "automation_name": data_key,
        "job_key": cleaned_key,
        "run_id": str(run_id),
    }
    if expected_item_count > 0:
        payload_with_route_key["expected_item_count"] = expected_item_count
    if feishu_cards:
        payload_with_route_key["feishu_cards"] = feishu_cards
    if metadata_error_code is not None:
        payload_with_route_key["_metadata_error_code"] = metadata_error_code
    elif outbox_missing:
        payload_with_route_key["_outbox_missing"] = True
    return text, card, payload_with_route_key


def _extract_target_error_code(
    target: str,
    bridge_res: Any,
    batch_err: str | None,
    *,
    use_gateway_transport: bool = False,
) -> str:
    return _extract_retry_target_error_code(
        target,
        bridge_res,
        batch_err,
        use_gateway_transport=use_gateway_transport,
    )


def _extract_target_skip_reason(target: str, bridge_res: Any) -> str | None:
    if not isinstance(bridge_res, Mapping):
        return None
    results = bridge_res.get("results")
    if not isinstance(results, list):
        return None
    for item in results:
        if (
            isinstance(item, Mapping)
            and str(item.get("target") or "").strip() == target
        ):
            if (
                item.get("skipped") is True
                or str(item.get("status") or "").strip().lower() == "skipped"
            ):
                return str(
                    item.get("reason") or item.get("error") or "skipped"
                ).strip() or "skipped"
    return None


async def _safe_fetch_deliveries(
    fetch_deliveries_fn: Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]],
    run_id: str,
    *,
    use_default_db: bool,
) -> list[dict[str, Any]]:
    if not use_default_db:
        return []
    try:
        rows = await fetch_deliveries_fn(run_id)
        return [dict(r) for r in (rows or []) if isinstance(r, Mapping)]
    except Exception:
        return []


async def _invoke_claim_target(
    claim_fn: Callable[..., Awaitable[Any]],
    execution_id: str,
    channel: str,
    target: str,
    *,
    user_id: str,
    automation_id: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[bool, int | None]:
    if claim_fn is claim_target_delivery:
        return await claim_target_delivery_with_attempt(
            execution_id,
            channel,
            target,
            user_id=user_id,
            automation_id=automation_id,
            payload=payload,
        )
    if claim_fn is claim_target_delivery_with_attempt:
        return await claim_fn(
            execution_id,
            channel,
            target,
            user_id=user_id,
            automation_id=automation_id,
            payload=payload,
        )
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "automation_id": automation_id,
        "payload": payload,
    }
    if _callable_accepts_kwarg(claim_fn, "return_attempt_count"):
        kwargs["return_attempt_count"] = True
    raw_res = await claim_fn(
        execution_id,
        channel,
        target,
        **kwargs,
    )
    if isinstance(raw_res, tuple) and len(raw_res) == 2:
        claimed_ok = bool(raw_res[0])
        raw_attempt = raw_res[1]
        return (
            claimed_ok,
            int(raw_attempt) if claimed_ok and raw_attempt is not None else None,
        )
    claimed_ok = bool(raw_res)
    return (claimed_ok, 1 if claimed_ok else None)


def _coerce_outbox_payload(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, str) and payload.strip():
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if isinstance(payload, Mapping):
        return payload
    return None


def _outbox_has_delivery_writeback(payload: Any) -> bool:
    mapped = _coerce_outbox_payload(payload)
    return isinstance(mapped, Mapping) and isinstance(mapped.get("delivery_writeback"), Mapping)


# Same bound as notify delivery ``max_attempts`` (default 3 in
# ``src.dalton.notify.dispatch``). A failed writeback is not retried forever.
WRITEBACK_FAILURE_ATTEMPT_LIMIT = 3


def _writeback_abandon_reason(attempts: int) -> str:
    return (
        f"writeback failed {attempts} times; "
        "stopped retrying and did not resend the notification"
    )


def _writeback_attempt_count(payload: Any) -> int:
    mapped = _coerce_outbox_payload(payload)
    if not isinstance(mapped, Mapping):
        return 0
    raw = mapped.get("writeback_attempts")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


async def _load_run_started_at(conn: Any, run_id: str) -> str | None:
    """Read ``dalton_data_task_runs.started_at`` for writeback ordering.

    UUID ``run_id`` is not time-ordered. A datetime is returned as UTC ISO text.
    A scripted row that is not a timestamp returns ``None``.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT started_at
            FROM dalton_data_task_runs
            WHERE run_id = %(run_id)s::uuid
            """,
            {"run_id": str(run_id)},
        )
        row = await cur.fetchone()
    if row is None:
        return None
    value = _extract_field(row, "started_at", 0)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def _persist_writeback_status(
    conn: Any,
    run_id: str,
    status: str,
    *,
    attempts: int | None = None,
    reason: str | None = None,
) -> None:
    """Store writeback status on the existing ``outbox_payload`` JSON object.

    ``attempts`` and ``reason`` are omitted when null, so a ``done`` update does
    not erase a previous attempt count. ``abandoned`` drops out of the retry
    scan, which only selects ``pending`` and ``failed``.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE dalton_data_task_runs
            SET outbox_payload = outbox_payload || jsonb_strip_nulls(jsonb_build_object(
                    'writeback_status', %(status)s::text,
                    'writeback_attempts', %(attempts)s::int,
                    'writeback_abandon_reason', %(reason)s::text
                )),
                updated_at = NOW()
            WHERE run_id = %(run_id)s::uuid
              AND jsonb_typeof(outbox_payload) = 'object'
              AND outbox_payload ? 'delivery_writeback'
            """,
            {
                "run_id": str(run_id),
                "status": status,
                "attempts": attempts,
                "reason": reason,
            },
        )


async def _store_writeback_outcome(
    conn: Any,
    run_id: str,
    status: str,
    payload: Any,
) -> str:
    """Persist one writeback result. Only ``failed`` consumes an attempt.

    The attempt that reaches the cap is stored as ``abandoned`` with a reason.
    ``done`` (including an older-run skip or a hash mismatch) does not count
    and does not send the notification again.
    """
    attempts: int | None = None
    reason: str | None = None
    if status == "failed":
        attempts = _writeback_attempt_count(payload) + 1
        if attempts >= WRITEBACK_FAILURE_ATTEMPT_LIMIT:
            status = "abandoned"
            reason = _writeback_abandon_reason(attempts)
    await _persist_writeback_status(
        conn,
        run_id,
        status,
        attempts=attempts,
        reason=reason,
    )
    return status


async def _complete_confirmed_delivery_writeback(
    conn: Any,
    *,
    run_id: str,
    job_key: str,
    db_outbox_payload: Any,
    path_map: DataTaskPathMap | None,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_data_task_gateway_notify,
) -> str:
    """Write this run's snapshotted state. Never sends the original notification.

    An ETF Discord mirror is a separate gateway send to the snapshotted targets.
    File failures and mirror failures return ``failed`` so the scan can retry.
    A superseded ai-chain file is ``done`` inside ``apply`` and is not retried.
    """
    from src.dalton.data_jobs.batch3_messages import apply_confirmed_delivery_writeback

    paths = path_map or build_path_map()
    mapped = _coerce_outbox_payload(db_outbox_payload)
    try:
        mirror_targets = apply_confirmed_delivery_writeback(
            mapped,
            paths.backend_data_root,
            "dispatched",
            run_artifacts_dir=paths.backend_run_artifacts_dir(str(run_id)),
        )
        if mirror_targets:
            posted = await post_notify_fn(list(mirror_targets), run_id=str(run_id))
            if (
                not isinstance(posted, Mapping)
                or posted.get("ok") is not True
                or str(posted.get("status") or "") != "sent"
            ):
                detail = posted.get("error") if isinstance(posted, Mapping) else posted
                raise RuntimeError(f"etf discord mirror failed: {detail}")
        return "done"
    except Exception as writeback_exc:
        logger.warning(
            "Confirmed delivery for run_id=%s job_key=%s was not written back: %s",
            run_id,
            job_key,
            writeback_exc,
        )
        return "failed"


async def retry_unfinished_delivery_writebacks(
    conn: Any,
    *,
    lock_conn: Any,
    path_map: DataTaskPathMap | None = None,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_data_task_gateway_notify,
) -> list[tuple[str, str]]:
    """Retry file writeback for dispatched runs. Does not send the original message.

    Called beside ``dispatch_pending_data_task_notifications``. An empty scan
    does not take the scheduler lock, so existing lock-step tests stay put.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT r.run_id::text AS run_id,
                   r.job_key,
                   r.outbox_payload
            FROM dalton_data_task_runs r
            WHERE r.notify_status = 'dispatched'
              AND jsonb_typeof(r.outbox_payload) = 'object'
              AND r.outbox_payload ? 'delivery_writeback'
              AND COALESCE(r.outbox_payload->>'writeback_status', '') IN ('pending', 'failed')
            ORDER BY r.completed_at ASC NULLS LAST, r.created_at ASC
            """
        )
        rows = await cur.fetchall()
    if not rows:
        return []

    results: list[tuple[str, str]] = []
    for row in rows:
        await check_scheduler_lock(lock_conn)
        run_id = str(_extract_field(row, "run_id", 0))
        job_key = str(_extract_field(row, "job_key", 1))
        payload = _extract_field(row, "outbox_payload", 2)
        mapped = _coerce_outbox_payload(payload)
        attempts_so_far = _writeback_attempt_count(mapped)
        if (
            isinstance(mapped, Mapping)
            and str(mapped.get("writeback_status") or "") == "failed"
            and attempts_so_far >= WRITEBACK_FAILURE_ATTEMPT_LIMIT
        ):
            await _persist_writeback_status(
                conn,
                run_id,
                "abandoned",
                attempts=attempts_so_far,
                reason=_writeback_abandon_reason(attempts_so_far),
            )
            results.append((run_id, "abandoned"))
            continue
        status = await _complete_confirmed_delivery_writeback(
            conn,
            run_id=run_id,
            job_key=job_key,
            db_outbox_payload=payload,
            path_map=path_map,
            post_notify_fn=post_notify_fn,
        )
        status = await _store_writeback_outcome(conn, run_id, status, payload)
        results.append((run_id, status))
    return results


async def dispatch_data_task_run_notification(
    conn: Any,
    *,
    lock_conn: Any,
    run_id: str,
    job_key: str,
    db_outbox_payload: Any = None,
    log_tail: str | None = None,
    task_enabled: bool = True,
    notify_enabled: bool = True,
    path_map: DataTaskPathMap | None = None,
    evaluate_eligibility_fn: Callable[..., Awaitable[tuple[bool, str | None, list[str]]]] = evaluate_user_notify_eligibility,
    claim_target_fn: Callable[..., Awaitable[Any]] = claim_target_delivery,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_data_task_gateway_notify,
    mark_target_fn: Callable[..., Awaitable[bool]] = mark_target_delivery,
    fetch_deliveries_fn: Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]] = fetch_execution_deliveries,
) -> str:
    """Dispatch initial notification for a single ``pending_dispatch`` data task run (§5.1 item 2)."""
    if not task_enabled or not notify_enabled:
        disabled_reason = "task_disabled" if not task_enabled else "notify_disabled"
        logger.info(
            "Skipping data task notification for run_id=%s job_key=%s (reason=%s)",
            run_id,
            job_key,
            disabled_reason,
        )
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET notify_status = %(notify_status)s,
                        updated_at    = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND notify_status = 'pending_dispatch'
                    """,
                    {
                        "run_id": str(run_id),
                        "notify_status": "skipped",
                    },
                )
        return "skipped"

    text, card, payload_with_route_key = _resolve_run_notify_payload(
        str(run_id),
        job_key,
        db_outbox_payload=db_outbox_payload,
        log_tail=log_tail,
        path_map=path_map,
    )
    metadata_error_code = payload_with_route_key.pop("_metadata_error_code", None)
    if metadata_error_code:
        logger.warning(
            "Failing data task notification for run_id=%s job_key=%s (reason=invalid_outbox:%s)",
            run_id,
            job_key,
            metadata_error_code,
        )
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET notify_status = %(notify_status)s,
                        updated_at    = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND notify_status = 'pending_dispatch'
                    """,
                    {
                        "run_id": str(run_id),
                        "notify_status": "failed",
                    },
                )
        return "failed"

    outbox_missing = bool(payload_with_route_key.pop("_outbox_missing", False))
    if outbox_missing:
        logger.warning(
            "Skipping data task notification for run_id=%s job_key=%s (reason=outbox_missing)",
            run_id,
            job_key,
        )
        await check_scheduler_lock(lock_conn)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET notify_status = %(notify_status)s,
                        updated_at    = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND notify_status = 'pending_dispatch'
                    """,
                    {
                        "run_id": str(run_id),
                        "notify_status": "skipped",
                    },
                )
        return "skipped"

    automation = build_data_task_notify_automation(job_key)
    data_key = str(automation["automation_id"])

    if is_dalton_local_auth_enabled():
        from src.dalton.access_tiers import get_primary_admin_user_id

        pm_user_id = str((await get_primary_admin_user_id()) or "").strip()
    else:
        pm_ids = get_configured_pm_user_ids()
        pm_user_id = str(pm_ids[0]).strip() if pm_ids else ""

    await check_scheduler_lock(lock_conn)
    allowed, skip_reason, targets = await evaluate_eligibility_fn(
        pm_user_id,
        automation,
    )

    from src.dalton.capabilities import is_channel_target_enabled

    valid_targets: list[tuple[str, str]] = []
    for raw_tgt in targets or []:
        ch, cleaned_tgt = _split_target_channel(raw_tgt)
        if (
            ch
            and cleaned_tgt
            and is_channel_target_enabled(cleaned_tgt, channel=ch)
            and (ch, cleaned_tgt) not in valid_targets
        ):
            valid_targets.append((ch, cleaned_tgt))

    use_delivery_db = (
        claim_target_fn in (claim_target_delivery, claim_target_delivery_with_attempt)
        or mark_target_fn is mark_target_delivery
        or fetch_deliveries_fn is not fetch_execution_deliveries
    )
    existing_by_target: dict[tuple[str, str], str] = {}
    existing_error_by_target: dict[tuple[str, str], str] = {}
    existing_payload_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for d_row in await _safe_fetch_deliveries(
        fetch_deliveries_fn,
        str(run_id),
        use_default_db=use_delivery_db,
    ):
        d_ch = str(d_row.get("channel") or "").strip().lower()
        d_tgt = str(d_row.get("target") or "").strip()
        d_st = str(d_row.get("status") or "").strip().lower()
        d_err = str(d_row.get("error_code") or "").strip().lower()
        if d_ch and d_tgt and d_st:
            existing_by_target[(d_ch, d_tgt)] = d_st
            if d_err:
                existing_error_by_target[(d_ch, d_tgt)] = d_err
            d_payload = d_row.get("payload_json")
            if isinstance(d_payload, Mapping):
                existing_payload_by_target[(d_ch, d_tgt)] = dict(d_payload)

    local_by_target: dict[tuple[str, str], str] = {}
    local_error_by_target: dict[tuple[str, str], str] = {}
    pending_targets: list[tuple[str, str]] = []
    claimed_attempt_by_target: dict[tuple[str, str], int | None] = {}

    def _is_terminal_target_state(st: str | None, err: str | None) -> bool:
        if st in ("sent", "skipped"):
            return True
        if st == "failed" and (err or "").strip().lower() in NON_RETRYABLE_TERMINAL_ERROR_CODES:
            return True
        return False

    def _settle_pending_target(
        tgt_key: tuple[str, str], status_val: str, error_code: str | None = None
    ) -> None:
        local_by_target[tgt_key] = status_val
        if error_code:
            local_error_by_target[tgt_key] = str(error_code).strip().lower()
        else:
            local_error_by_target.pop(tgt_key, None)
        if tgt_key in pending_targets:
            pending_targets.remove(tgt_key)

    try:
        if not allowed:
            for ch, cleaned_tgt in valid_targets:
                if _is_terminal_target_state(
                    existing_by_target.get((ch, cleaned_tgt)),
                    existing_error_by_target.get((ch, cleaned_tgt)),
                ):
                    continue
                await check_scheduler_lock(lock_conn)
                claimed_ok, claimed_attempt_count = await _invoke_claim_target(
                    claim_target_fn,
                    str(run_id),
                    ch,
                    cleaned_tgt,
                    user_id=pm_user_id,
                    automation_id=data_key,
                    payload=dict(payload_with_route_key),
                )
                if claimed_ok:
                    claimed_attempt_by_target[(ch, cleaned_tgt)] = claimed_attempt_count
                    local_by_target[(ch, cleaned_tgt)] = "pending"
                    await check_scheduler_lock(lock_conn)
                    await _invoke_mark_delivery(
                        mark_target_fn,
                        str(run_id),
                        ch,
                        cleaned_tgt,
                        status="skipped",
                        reason=skip_reason or "toggle_off",
                        error_code=skip_reason or "toggle_off",
                        expected_status="pending",
                        expected_attempt_count=claimed_attempt_count,
                    )
                    local_by_target[(ch, cleaned_tgt)] = "skipped"
        elif valid_targets:
            for ch, cleaned_tgt in valid_targets:
                if _is_terminal_target_state(
                    existing_by_target.get((ch, cleaned_tgt)),
                    existing_error_by_target.get((ch, cleaned_tgt)),
                ):
                    continue
                target_payload = dict(payload_with_route_key)
                prev_payload = existing_payload_by_target.get((ch, cleaned_tgt))
                if isinstance(prev_payload, Mapping):
                    if "cards_sent" in prev_payload:
                        target_payload["cards_sent"] = prev_payload["cards_sent"]
                    for rec_key in (
                        "reconcile_platform",
                        "reconcile_recipient_id",
                        "reconcile_canonical_target",
                        "expected_item_count",
                        "reconcile_delivery_keys",
                    ):
                        if rec_key in prev_payload:
                            target_payload[rec_key] = prev_payload[rec_key]
                await check_scheduler_lock(lock_conn)
                claimed_ok, claimed_attempt_count = await _invoke_claim_target(
                    claim_target_fn,
                    str(run_id),
                    ch,
                    cleaned_tgt,
                    user_id=pm_user_id,
                    automation_id=data_key,
                    payload=target_payload,
                )
                if not claimed_ok:
                    continue
                claimed_attempt_by_target[(ch, cleaned_tgt)] = claimed_attempt_count
                local_by_target[(ch, cleaned_tgt)] = "pending"
                pending_targets.append((ch, cleaned_tgt))
                active_native_target: tuple[str, str] | None = None
                active_uncertain_code = "delivery_uncertain"
                transport_outcome_uncertain_or_sent = False

                try:
                    if claim_target_fn in (
                        claim_target_delivery,
                        claim_target_delivery_with_attempt,
                    ):
                        from src.dalton.database.data_task_notifications import (
                            ensure_outer_reconcile_identity_persisted,
                        )

                        persisted_rec = await ensure_outer_reconcile_identity_persisted(
                            run_id=str(run_id),
                            channel=ch,
                            target=cleaned_tgt,
                            owner_user_id=pm_user_id,
                            payload=target_payload,
                        )
                        if isinstance(persisted_rec, Mapping):
                            if persisted_rec.get("conflict") is True:
                                conflict_code = str(
                                    persisted_rec.get("code")
                                    or "recipient_identity_conflict"
                                )
                                await _invoke_mark_delivery(
                                    mark_target_fn,
                                    str(run_id),
                                    ch,
                                    cleaned_tgt,
                                    status="failed",
                                    error_code=conflict_code,
                                    expected_status="pending",
                                    expected_attempt_count=claimed_attempt_count,
                                )
                                _settle_pending_target((ch, cleaned_tgt), "failed")
                                continue
                            target_payload.update(persisted_rec)

                    await check_scheduler_lock(lock_conn)
                    use_gateway_transport = (
                        post_notify_fn is post_data_task_gateway_notify
                        or _callable_accepts_kwarg(post_notify_fn, "gateway_url")
                    )
                    if use_gateway_transport:
                        active_native_target = (ch, cleaned_tgt)
                        bridge_res = await post_notify_fn(
                            [cleaned_tgt],
                            run_id=str(run_id),
                        )
                    else:
                        bridge_res = await dispatch_target_with_payload(
                            post_notify_fn,
                            [cleaned_tgt],
                            text,
                            card,
                            target_payload,
                            run_id=str(run_id),
                            channel=ch,
                            check_lock_fn=lambda: check_scheduler_lock(lock_conn),
                        )

                    target_skip_reason = _extract_target_skip_reason(
                        cleaned_tgt, bridge_res
                    )
                    if target_skip_reason is not None:
                        active_native_target = None
                        transport_outcome_uncertain_or_sent = False
                        await _invoke_mark_delivery(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            status="skipped",
                            reason=target_skip_reason,
                            error_code=target_skip_reason,
                            expected_status="pending",
                            expected_attempt_count=claimed_attempt_count,
                        )
                        _settle_pending_target((ch, cleaned_tgt), "skipped")
                        continue

                    uncertain_code = extract_target_uncertain_code(
                        cleaned_tgt, bridge_res
                    )
                    if uncertain_code is not None:
                        active_native_target = (ch, cleaned_tgt)
                        active_uncertain_code = uncertain_code
                        transport_outcome_uncertain_or_sent = True
                        await _invoke_mark_delivery(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            status="pending",
                            error_code=uncertain_code,
                            expected_status="pending",
                            expected_attempt_count=claimed_attempt_count,
                        )
                        _settle_pending_target((ch, cleaned_tgt), "pending")
                        continue

                    outcomes, batch_err = _evaluate_target_results(
                        [cleaned_tgt], bridge_res
                    )
                    if outcomes.get(cleaned_tgt) is True:
                        active_native_target = (ch, cleaned_tgt)
                        active_uncertain_code = "delivery_uncertain"
                        transport_outcome_uncertain_or_sent = True
                        await _invoke_mark_delivery(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            status="sent",
                            error_code=None,
                            expected_status="pending",
                            expected_attempt_count=claimed_attempt_count,
                        )
                        _settle_pending_target((ch, cleaned_tgt), "sent")
                    else:
                        active_native_target = None
                        transport_outcome_uncertain_or_sent = False
                        err_code = _extract_target_error_code(
                            cleaned_tgt,
                            bridge_res,
                            batch_err,
                            use_gateway_transport=use_gateway_transport,
                        )
                        _settle_pending_target((ch, cleaned_tgt), "failed", err_code)
                        await _shielded_mark_failed(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            error_code=err_code,
                            expected_status="pending",
                            expected_attempt_count=claimed_attempt_count,
                            on_settled=lambda _tgt=(ch, cleaned_tgt), _code=err_code: _settle_pending_target(
                                _tgt, "failed", _code
                            ),
                        )
                except asyncio.CancelledError:
                    for rem_ch, rem_tgt in list(pending_targets):
                        rem_attempt = claimed_attempt_by_target.get((rem_ch, rem_tgt))
                        try:
                            if (rem_ch, rem_tgt) == active_native_target:
                                await _shielded_mark_uncertain(
                                    mark_target_fn,
                                    str(run_id),
                                    rem_ch,
                                    rem_tgt,
                                    error_code=active_uncertain_code,
                                    expected_status="pending",
                                    expected_attempt_count=rem_attempt,
                                    on_settled=lambda _tgt=(rem_ch, rem_tgt): _settle_pending_target(
                                        _tgt, "pending"
                                    ),
                                )
                                _settle_pending_target((rem_ch, rem_tgt), "pending")
                            else:
                                await _shielded_mark_failed(
                                    mark_target_fn,
                                    str(run_id),
                                    rem_ch,
                                    rem_tgt,
                                    error_code="CancelledError",
                                    expected_status="pending",
                                    expected_attempt_count=rem_attempt,
                                    on_settled=lambda _tgt=(rem_ch, rem_tgt): _settle_pending_target(
                                        _tgt, "failed"
                                    ),
                                )
                                _settle_pending_target((rem_ch, rem_tgt), "failed")
                        except asyncio.CancelledError:
                            pass
                    pending_targets.clear()
                    raise
                except SchedulerLockLostError:
                    raise
                except Exception as post_exc:
                    is_timeout_exc = isinstance(
                        post_exc,
                        (
                            asyncio.TimeoutError,
                            TimeoutError,
                            httpx.TimeoutException,
                            httpx.TransportError,
                        ),
                    ) and not isinstance(
                        post_exc, (httpx.ConnectTimeout, httpx.ConnectError)
                    )
                    exc_code = getattr(post_exc, "code", None)
                    preserve_uncertain = bool(
                        active_native_target == (ch, cleaned_tgt)
                        and (
                            transport_outcome_uncertain_or_sent
                            or is_timeout_exc
                            or is_uncertain_notify_error_code(exc_code)
                        )
                    )
                    try:
                        if preserve_uncertain:
                            uncertain_err = (
                                str(exc_code)
                                if is_uncertain_notify_error_code(exc_code)
                                else active_uncertain_code
                            )
                            _settle_pending_target((ch, cleaned_tgt), "pending")
                            await _shielded_mark_uncertain(
                                mark_target_fn,
                                str(run_id),
                                ch,
                                cleaned_tgt,
                                error_code=uncertain_err,
                                expected_status="pending",
                                expected_attempt_count=claimed_attempt_count,
                                on_settled=lambda _tgt=(ch, cleaned_tgt): _settle_pending_target(
                                    _tgt, "pending"
                                ),
                            )
                        else:
                            active_native_target = None
                            status_code = getattr(
                                getattr(post_exc, "response", None), "status_code", None
                            )
                            err_code = (
                                f"http_{status_code}"
                                if status_code
                                else type(post_exc).__name__
                            )
                            _settle_pending_target((ch, cleaned_tgt), "failed")
                            await _shielded_mark_failed(
                                mark_target_fn,
                                str(run_id),
                                ch,
                                cleaned_tgt,
                                error_code=err_code,
                                expected_status="pending",
                                expected_attempt_count=claimed_attempt_count,
                                on_settled=lambda _tgt=(ch, cleaned_tgt): _settle_pending_target(
                                    _tgt, "failed"
                                ),
                            )
                    finally:
                        if (ch, cleaned_tgt) in pending_targets:
                            pending_targets.remove((ch, cleaned_tgt))
                    continue
    finally:
        pending_targets.clear()
        claimed_attempt_by_target.clear()

    persisted_by_target: dict[tuple[str, str], str] = dict(existing_by_target)
    persisted_error_by_target: dict[tuple[str, str], str] = dict(existing_error_by_target)
    if (
        mark_target_fn is not mark_target_delivery
        and fetch_deliveries_fn is fetch_execution_deliveries
    ):
        persisted_by_target.update(local_by_target)
        persisted_error_by_target.update(local_error_by_target)
    for d_row in await _safe_fetch_deliveries(
        fetch_deliveries_fn,
        str(run_id),
        use_default_db=use_delivery_db,
    ):
        d_ch = str(d_row.get("channel") or "").strip().lower()
        d_tgt = str(d_row.get("target") or "").strip()
        d_st = str(d_row.get("status") or "").strip().lower()
        d_err = str(d_row.get("error_code") or "").strip().lower()
        if d_ch and d_tgt and d_st:
            persisted_by_target[(d_ch, d_tgt)] = d_st
            if d_err:
                persisted_error_by_target[(d_ch, d_tgt)] = d_err
            else:
                persisted_error_by_target.pop((d_ch, d_tgt), None)

    if not valid_targets and not persisted_by_target:
        logger.info(
            "Skipping data task notification for run_id=%s job_key=%s (reason=%s)",
            run_id,
            job_key,
            skip_reason or "no_targets",
        )
        terminal_notify_status = "skipped"
    else:
        missing_expected = any(
            tgt_key not in persisted_by_target for tgt_key in valid_targets
        )
        statuses = list(persisted_by_target.values())
        any_non_terminal = any(
            not _is_terminal_target_state(st, persisted_error_by_target.get(tgt_key))
            for tgt_key, st in persisted_by_target.items()
        )
        # Every expected target persisted and settled: any `sent` means the
        # run was dispatched; terminal non-retryable failures with no `sent`
        # settle to `failed` so the scheduler stops re-dispatching them; all
        # `skipped` means nothing was sent. Anything else is still in flight.
        if (
            missing_expected
            or not statuses
            or any_non_terminal
        ):
            terminal_notify_status = "pending_dispatch"
        elif "sent" in statuses:
            terminal_notify_status = "dispatched"
        elif any(st == "failed" for st in statuses):
            terminal_notify_status = "failed"
        else:
            terminal_notify_status = "skipped"

    await check_scheduler_lock(lock_conn)
    notify_rowcount = 0
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                SET notify_status = %(notify_status)s,
                    updated_at    = NOW()
                WHERE run_id = %(run_id)s::uuid
                  AND notify_status = 'pending_dispatch'
                """,
                {
                    "run_id": str(run_id),
                    "notify_status": terminal_notify_status,
                },
            )
            notify_rowcount = int(cur.rowcount or 0)
    if (
        terminal_notify_status == "dispatched"
        and notify_rowcount == 1
        and _outbox_has_delivery_writeback(db_outbox_payload)
    ):
        writeback_status = await _complete_confirmed_delivery_writeback(
            conn,
            run_id=str(run_id),
            job_key=job_key,
            db_outbox_payload=db_outbox_payload,
            path_map=path_map,
            post_notify_fn=post_notify_fn,
        )
        await _store_writeback_outcome(
            conn,
            str(run_id),
            writeback_status,
            db_outbox_payload,
        )
    return terminal_notify_status


async def dispatch_pending_data_task_notifications(
    conn: Any,
    *,
    lock_conn: Any,
    path_map: DataTaskPathMap | None = None,
    evaluate_eligibility_fn: Callable[..., Awaitable[tuple[bool, str | None, list[str]]]] = evaluate_user_notify_eligibility,
    claim_target_fn: Callable[..., Awaitable[bool]] = claim_target_delivery,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_data_task_gateway_notify,
    mark_target_fn: Callable[..., Awaitable[bool]] = mark_target_delivery,
    fetch_deliveries_fn: Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]] = fetch_execution_deliveries,
) -> list[tuple[str, str]]:
    """Scan ``dalton_data_task_runs.notify_status = 'pending_dispatch'`` and dispatch notifications (§5.1 item 2)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT r.run_id,
                   r.job_key,
                   r.outbox_payload,
                   r.log_tail,
                   d.enabled AS task_enabled,
                   d.notify_enabled AS notify_enabled
            FROM dalton_data_task_runs r
            LEFT JOIN dalton_data_task_defs d
              ON d.job_key = r.job_key
            WHERE r.notify_status = 'pending_dispatch'
            ORDER BY r.completed_at ASC NULLS LAST, r.created_at ASC
            """
        )
        rows = await cur.fetchall()

    if not rows:
        return []

    results: list[tuple[str, str]] = []
    for row in rows:
        run_id = str(_extract_field(row, "run_id", 0))
        job_key = str(_extract_field(row, "job_key", 1))
        db_outbox = _extract_field(row, "outbox_payload", 2)
        log_tail_val = _extract_field(row, "log_tail", 3)
        task_enabled_val = True
        notify_enabled_val = True
        if isinstance(row, Mapping):
            if "task_enabled" in row:
                task_enabled_val = bool(row.get("task_enabled"))
            elif "enabled" in row:
                task_enabled_val = bool(row.get("enabled"))
            if "notify_enabled" in row:
                notify_enabled_val = bool(row.get("notify_enabled"))
        elif isinstance(row, Sequence) and len(row) >= 6:
            task_enabled_val = bool(row[4])
            notify_enabled_val = bool(row[5])

        status = await dispatch_data_task_run_notification(
            conn,
            lock_conn=lock_conn,
            run_id=run_id,
            job_key=job_key,
            db_outbox_payload=db_outbox,
            log_tail=str(log_tail_val) if log_tail_val is not None else None,
            task_enabled=task_enabled_val,
            notify_enabled=notify_enabled_val,
            path_map=path_map,
            evaluate_eligibility_fn=evaluate_eligibility_fn,
            claim_target_fn=claim_target_fn,
            post_notify_fn=post_notify_fn,
            mark_target_fn=mark_target_fn,
            fetch_deliveries_fn=fetch_deliveries_fn,
        )
        results.append((run_id, status))
    return results


async def run_data_task_scheduler_loop(
    *,
    stop_event: asyncio.Event | None = None,
    docker_client: DataTaskDockerClient | None = None,
    host_client: DataTaskHostBridgeClient | None = None,
    db_conn_factory: Callable[[], Any] = get_db_connection,
    lock_conn_factory: Callable[[], Awaitable[Any]] | None = None,
    poll_interval_seconds: float = SCHEDULER_POLL_INTERVAL_S,
    lock_retry_seconds: float = LOCK_RETRY_INTERVAL_S,
) -> None:
    """Main background coroutine for the singleton OC-1 data task scheduler (§3.2–§3.5, Requirement 8).

    Guarantees:
    - When ``is_data_jobs_scheduler_enabled()`` is ``False`` (the default), exits immediately
      without opening a DB connection or creating any containers.
    - Holds the session advisory lock on a dedicated, non-pooled connection opened via
      ``open_dedicated_lock_connection``; on exit (including exceptions), explicitly calls
      ``pg_advisory_unlock`` and closes the connection.
    - Runs claimed container or host executions in a background ``asyncio.Task`` so the main loop
      continues waking every minute to enqueue due cron tasks even while a task is running, and only
      claims a new run when the execution slot is idle.
    """
    if not is_data_jobs_scheduler_enabled():
        logger.debug("Dalton data jobs scheduler is disabled; skipping loop startup")
        return

    owns_docker = docker_client is None
    client = docker_client or DataTaskDockerClient()
    bridge_client = host_client or DataTaskHostBridgeClient()
    stop = stop_event or asyncio.Event()
    open_lock_conn = lock_conn_factory or open_dedicated_lock_connection
    lock_conn: Any = None
    active_exec_task: asyncio.Task[Any] | None = None

    async def _run_claimed_in_background(claimed_run: ClaimedTaskRun) -> None:
        async with db_conn_factory() as exec_conn:
            if claimed_run.runner == "host":
                await execute_claimed_host_run(
                    exec_conn,
                    lock_conn=lock_conn,
                    host_client=bridge_client,
                    claimed=claimed_run,
                )
            else:
                await execute_claimed_container_run(
                    exec_conn,
                    lock_conn=lock_conn,
                    docker_client=client,
                    claimed=claimed_run,
                )

    try:
        lock_conn = await open_lock_conn()
        if hasattr(lock_conn, "set_autocommit"):
            try:
                await lock_conn.set_autocommit(True)
            except Exception:
                pass

        while not stop.is_set():
            if not is_data_jobs_scheduler_enabled():
                return
            if await try_scheduler_lock(lock_conn):
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=lock_retry_seconds)
                return
            except asyncio.TimeoutError:
                continue

        async with db_conn_factory() as work_conn:
            async with work_conn.transaction():
                await sync_manifest_to_db(work_conn)
            await recover_on_takeover(
                work_conn,
                lock_conn=lock_conn,
                docker_client=client,
                host_client=bridge_client,
            )

        while not stop.is_set():
            if not is_data_jobs_scheduler_enabled():
                return

            if active_exec_task is not None and active_exec_task.done():
                try:
                    await active_exec_task
                except SchedulerLockLostError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Background data task execution failed: %s", exc)
                finally:
                    active_exec_task = None

            await check_scheduler_lock(lock_conn)

            async with db_conn_factory() as work_conn:
                # Enqueue due cron tasks every minute, even while a background task is running.
                await enqueue_due_cron_tasks(work_conn)
                await flush_pending_takeover_retries(work_conn)
                await replay_pending_monitor_publishes(work_conn)
                await expire_low_memory_runs(work_conn)

                if active_exec_task is None and not stop.is_set():
                    await check_scheduler_lock(lock_conn)
                    async with work_conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT slot_state FROM dalton_data_task_slot WHERE slot_id = 1"
                        )
                        slot_row = await cur.fetchone()
                    slot_state = (
                        str(_extract_field(slot_row, "slot_state", 0)) if slot_row else "idle"
                    )
                    if slot_state != "idle":
                        await recover_on_takeover(
                            work_conn,
                            lock_conn=lock_conn,
                            docker_client=client,
                            host_client=bridge_client,
                        )

                    await check_scheduler_lock(lock_conn)
                    claimed = await claim_next_queued_run(work_conn)
                    if claimed is not None and claimed.runner in ("container", "host"):
                        active_exec_task = asyncio.create_task(
                            _run_claimed_in_background(claimed)
                        )

                await dispatch_pending_data_task_notifications(
                    work_conn,
                    lock_conn=lock_conn,
                )
                await retry_unfinished_delivery_writebacks(
                    work_conn,
                    lock_conn=lock_conn,
                )

            stop_waiter = asyncio.create_task(stop.wait())
            wait_tasks: set[asyncio.Task[Any]] = {stop_waiter}
            if active_exec_task is not None:
                wait_tasks.add(active_exec_task)
            try:
                await asyncio.wait(
                    wait_tasks,
                    timeout=poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not stop_waiter.done():
                    stop_waiter.cancel()
                    try:
                        await stop_waiter
                    except asyncio.CancelledError:
                        pass

            if active_exec_task is not None and active_exec_task.done():
                try:
                    await active_exec_task
                except SchedulerLockLostError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Background data task execution failed: %s", exc)
                finally:
                    active_exec_task = None

            if stop.is_set():
                return
    except asyncio.CancelledError:
        raise
    except SchedulerLockLostError as exc:
        logger.warning("Data task scheduler lost advisory lock; exiting loop: %s", exc)
    finally:
        if active_exec_task is not None and not active_exec_task.done():
            active_exec_task.cancel()
            await asyncio.gather(active_exec_task, return_exceptions=True)
        await release_scheduler_lock(lock_conn, close_conn=True)
        if owns_docker:
            await client.close()
