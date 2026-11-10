"""Single-scheduler advisory lock, atomic claim, container lifecycle, and takeover recovery (§3.2–§3.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import shutil
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
from src.dalton.data_jobs.freeze import freeze_task_run_artifacts
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
    _callable_accepts_kwarg,
    _evaluate_target_results,
    _invoke_mark_delivery,
    _shielded_mark_failed,
    _shielded_mark_uncertain,
    _split_target_channel,
    claim_target_delivery,
    dispatch_target_with_payload,
    evaluate_user_notify_eligibility,
    extract_feishu_cards_from_payload,
    fetch_execution_deliveries,
    mark_target_delivery,
)
from src.dalton.settings import (
    get_configured_pm_user_ids,
    get_data_task_min_free_mem_mb,
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
    """Fetch decrypted Vault secrets for ``secret_names`` from the primary PM admin (`DALTON_PM_USER_IDS[0]`, §4.4.1)."""
    names = [str(s).strip() for s in secret_names if str(s).strip()]
    if not names:
        return {}
    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        logger.warning("No DALTON_PM_USER_IDS configured; skipping Vault secret fetch for %s", names)
        return {}
    pm_user_id = str(pm_ids[0]).strip()
    if not pm_user_id:
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
        timeout_s = task_def.get("timeout_seconds")
        effective_timeout = int(timeout_s) if timeout_s is not None else DEFAULT_TIMEOUT_SECONDS

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.error("Invalid timezone %r on data task %s", tz_name, job_key)
            continue

        local_slot = slot_utc.astimezone(tz)
        if not croniter.match(cron_expr, local_slot):
            continue

        deadline_utc = slot_utc + timedelta(seconds=effective_timeout)
        new_run_id = str(uuid4())

        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
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
                SELECT job_key, timeout_seconds, runner, enabled
                FROM dalton_data_task_defs
                WHERE job_key = %(job_key)s
                FOR UPDATE
                """,
                {"job_key": resolved_job_key},
            )
            def_row = await cur.fetchone()
            if def_row is None:
                raise ValueError(f"Unknown data task job_key: {resolved_job_key}")

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
            SELECT r.run_id, r.job_key, r.trigger_source, d.enabled
            FROM dalton_data_task_runs r
            LEFT JOIN dalton_data_task_defs d ON d.job_key = r.job_key
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
        trigger_source = (
            str(row.get("trigger_source") or "cron")
            if isinstance(row, Mapping)
            else (str(row[2] or "cron") if len(row) > 2 else "cron")
        )
        enabled_val = (
            row.get("enabled", True)
            if isinstance(row, Mapping)
            else (row[3] if len(row) > 3 else True)
        )
        # 不变式：试跑在 queued/starting/running 期间禁止开启任务，因此当前 d.enabled 与入队/认领时一致，无需另存 is_trial。
        is_trial = trigger_source == "manual" and not bool(enabled_val)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status = 'skipped_low_memory',
                        failure_reason = 'low_memory_deadline_exceeded',
                        notify_status = 'skipped',
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE run_id = %(run_id)s::uuid
                      AND status = 'queued'
                    """,
                    {"run_id": run_id},
                )
                if cur.rowcount == 1:
                    if not is_trial:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_defs
                            SET consecutive_failures = consecutive_failures + 1,
                                updated_at = NOW()
                            WHERE job_key = %(job_key)s
                            """,
                            {"job_key": job_key},
                        )
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
                d.timezone
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
        enabled = bool(cand.get("enabled"))
        paused = bool(cand.get("paused_by_user"))
        is_trial = trigger_source == "manual" and not enabled
        runner = str(cand.get("runner") or "container")
        deadline_utc = cand["deadline_utc"]

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
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_data_task_runs
                                SET status = 'skipped_low_memory',
                                    low_mem_since_utc = COALESCE(low_mem_since_utc, %(now_utc)s),
                                    failure_reason = 'low_memory_deadline_exceeded',
                                    notify_status = 'skipped',
                                    completed_at = NOW(),
                                    updated_at = NOW()
                                WHERE run_id = %(run_id)s::uuid
                                  AND status = 'queued'
                                """,
                                {"run_id": run_id, "now_utc": ref_utc},
                            )
                            # 不变式：试跑在 queued/starting/running 期间禁止开启任务，因此当前 enabled 与入队/认领时一致，无需另存 is_trial。
                            if cur.rowcount == 1 and not is_trial:
                                await cur.execute(
                                    """
                                    UPDATE dalton_data_task_defs
                                    SET consecutive_failures = consecutive_failures + 1,
                                        updated_at = NOW()
                                    WHERE job_key = %(job_key)s
                                    """,
                                    {"job_key": job_key},
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
                        SELECT enabled, paused_by_user
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
                    locked_enabled = bool(_extract_field(locked_def, "enabled", 0))
                    locked_paused = bool(_extract_field(locked_def, "paused_by_user", 1))
                    is_trial = trigger_source == "manual" and not locked_enabled

                    await cur.execute(
                        """
                        UPDATE dalton_data_task_runs r
                        SET status     = 'starting',
                            generation = %(generation)s,
                            updated_at = NOW()
                        FROM dalton_data_task_defs d
                        WHERE r.run_id = %(run_id)s::uuid
                          AND r.status = 'queued'
                          AND d.job_key = r.job_key
                          AND d.enabled = %(expected_enabled)s
                          AND d.paused_by_user = %(expected_paused)s
                          AND (
                              (r.trigger_source = 'manual' AND %(expected_enabled)s = FALSE)
                              OR (%(expected_enabled)s = TRUE AND %(expected_paused)s = FALSE)
                          )
                          AND d.runner IN ('container', 'host')
                        """,
                        {
                            "run_id": run_id,
                            "generation": claimed_generation,
                            "expected_enabled": locked_enabled,
                            "expected_paused": locked_paused,
                        },
                    )
                    if cur.rowcount != 1:
                        raise _ClaimRollback(
                            "run_not_claimable",
                            unclaimable_run_id=run_id,
                        )
        except _ClaimRollback as rb:
            if rb.reason == "slot_not_idle" or rb.reason == "slot_returning_empty":
                return None
            if rb.unclaimable_run_id:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_runs
                            SET status = 'cancelled',
                                failure_reason = 'task_disabled_before_claim',
                                notify_status = 'skipped',
                                completed_at = NOW(),
                                updated_at = NOW()
                            WHERE run_id = %(run_id)s::uuid
                              AND status = 'queued'
                            """,
                            {"run_id": rb.unclaimable_run_id},
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
            workdir=str(cand.get("workdir") or "/Users/everflow/.openclaw/workspace"),
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
) -> str:
    """Compute ``notify_status`` for a completed run (§3.4.1).

    Trial runs (`is_trial == True`, i.e. manual runs claimed while task `enabled == False`)
    never dispatch live notifications: when `terminal_status == 'succeeded'`,
    `notify_status` is always `'preview_only'` regardless of `notify_enabled`.
    """
    if terminal_status != "succeeded":
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
    notify_status = resolve_notify_status(
        terminal_status=terminal_status,
        notify_enabled=claimed.notify_enabled,
        has_outbox_payload=has_outbox,
        is_trial=claimed.is_trial,
    )
    serialized_outbox = (
        json.dumps(outbox_payload, ensure_ascii=False) if outbox_payload is not None else None
    )
    serialized_manifest = (
        json.dumps(frozen_manifest, ensure_ascii=False) if frozen_manifest is not None else None
    )

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


async def recover_on_takeover(
    conn: Any,
    *,
    lock_conn: Any,
    docker_client: DataTaskDockerClient | None = None,
    host_client: DataTaskHostBridgeClient | None = None,
    path_map: DataTaskPathMap | None = None,
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
    - If the run referenced by ``dalton_data_task_slot.active_run_id`` is ALREADY in a terminal
      status (`succeeded`, `failed`, `timed_out`, `aborted`, `skipped_low_memory`,
      `skipped_superseded`, `cancelled`), **do NOT modify the run row, do NOT increment
      `consecutive_failures`, and do NOT change `notify_status`** — only clean up leftover
      containers, secrets directory, and reset `dalton_data_task_slot` to `'idle'`.
    """
    paths = path_map or build_path_map()
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
                SELECT r.run_id, r.job_key, r.status, r.container_id, d.runner, r.trigger_source
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
            if isinstance(run_row, Mapping):
                run_runner_raw = run_row.get("runner")
                run_trigger_raw = run_row.get("trigger_source")
            elif isinstance(run_row, (tuple, list)):
                if len(run_row) > 4:
                    run_runner_raw = run_row[4]
                if len(run_row) > 5:
                    run_trigger_raw = run_row[5]
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
                async with conn.transaction():
                    async with conn.cursor() as cur:
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
                            recovery_outcome = "aborted"
                        else:
                            recovery_outcome = "cleaned_terminal"

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
                    async with conn.transaction():
                        async with conn.cursor() as cur:
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
                                recovery_outcome = "aborted"
                            else:
                                recovery_outcome = "cleaned_terminal"
                else:
                    # Already terminal: do NOT alter dalton_data_task_runs or consecutive_failures!
                    recovery_outcome = "cleaned_terminal"

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
        except (DataTaskNotificationPrepError, SafeFsError) as prep_exc:
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

    async def _run_once_with_heartbeats(gen: int) -> tuple[HostRunOutcome | None, bool]:
        await check_scheduler_lock(lock_conn)
        run_coro = host_client.run_host_task(
            generation=gen,
            run_id=claimed.run_id,
            job_key=claimed.job_key,
            host_mode=effective_host_mode,
            scheduled_slot_utc=scheduled_slot_iso,
            timeout=claimed.timeout_seconds,
            no_output_timeout=claimed.no_output_timeout_seconds,
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
) -> str:
    if isinstance(bridge_res, Mapping):
        results = bridge_res.get("results")
        if isinstance(results, list):
            for item in results:
                if (
                    isinstance(item, Mapping)
                    and str(item.get("target") or "").strip() == target
                ):
                    err = str(item.get("error") or item.get("error_code") or "").strip()
                    if err:
                        return err
    return batch_err or "target_failed"


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
    claim_target_fn: Callable[..., Awaitable[bool]] = claim_target_delivery,
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

    pm_ids = get_configured_pm_user_ids()
    pm_user_id = str(pm_ids[0]).strip() if pm_ids else ""

    await check_scheduler_lock(lock_conn)
    allowed, skip_reason, targets = await evaluate_eligibility_fn(
        pm_user_id,
        automation,
    )

    valid_targets: list[tuple[str, str]] = []
    for raw_tgt in targets or []:
        ch, cleaned_tgt = _split_target_channel(raw_tgt)
        if ch and cleaned_tgt and (ch, cleaned_tgt) not in valid_targets:
            valid_targets.append((ch, cleaned_tgt))

    use_delivery_db = (
        claim_target_fn is claim_target_delivery
        or mark_target_fn is mark_target_delivery
        or fetch_deliveries_fn is not fetch_execution_deliveries
    )
    existing_by_target: dict[tuple[str, str], str] = {}
    existing_payload_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for d_row in await _safe_fetch_deliveries(
        fetch_deliveries_fn,
        str(run_id),
        use_default_db=use_delivery_db,
    ):
        d_ch = str(d_row.get("channel") or "").strip().lower()
        d_tgt = str(d_row.get("target") or "").strip()
        d_st = str(d_row.get("status") or "").strip().lower()
        if d_ch and d_tgt and d_st:
            existing_by_target[(d_ch, d_tgt)] = d_st
            d_payload = d_row.get("payload_json")
            if isinstance(d_payload, Mapping):
                existing_payload_by_target[(d_ch, d_tgt)] = dict(d_payload)

    local_by_target: dict[tuple[str, str], str] = {}
    pending_targets: list[tuple[str, str]] = []

    if not allowed:
        for ch, cleaned_tgt in valid_targets:
            if existing_by_target.get((ch, cleaned_tgt)) in ("sent", "skipped"):
                continue
            await check_scheduler_lock(lock_conn)
            claimed_ok = await claim_target_fn(
                str(run_id),
                ch,
                cleaned_tgt,
                user_id=pm_user_id,
                automation_id=data_key,
                payload=dict(payload_with_route_key),
            )
            if claimed_ok:
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
                )
                local_by_target[(ch, cleaned_tgt)] = "skipped"
    elif valid_targets:
        for ch, cleaned_tgt in valid_targets:
            if existing_by_target.get((ch, cleaned_tgt)) in ("sent", "skipped"):
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
            claimed_ok = await claim_target_fn(
                str(run_id),
                ch,
                cleaned_tgt,
                user_id=pm_user_id,
                automation_id=data_key,
                payload=target_payload,
            )
            if not claimed_ok:
                continue
            local_by_target[(ch, cleaned_tgt)] = "pending"
            pending_targets.append((ch, cleaned_tgt))
            active_native_target: tuple[str, str] | None = None
            active_uncertain_code = "delivery_uncertain"
            transport_outcome_uncertain_or_sent = False

            try:
                if claim_target_fn is claim_target_delivery:
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
                            )
                            local_by_target[(ch, cleaned_tgt)] = "failed"
                            if (ch, cleaned_tgt) in pending_targets:
                                pending_targets.remove((ch, cleaned_tgt))
                            continue
                        target_payload.update(persisted_rec)

                await check_scheduler_lock(lock_conn)
                if (
                    post_notify_fn is post_data_task_gateway_notify
                    or _callable_accepts_kwarg(post_notify_fn, "gateway_url")
                ):
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
                    )
                    local_by_target[(ch, cleaned_tgt)] = "skipped"
                    if (ch, cleaned_tgt) in pending_targets:
                        pending_targets.remove((ch, cleaned_tgt))
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
                    )
                    local_by_target[(ch, cleaned_tgt)] = "pending"
                    if (ch, cleaned_tgt) in pending_targets:
                        pending_targets.remove((ch, cleaned_tgt))
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
                    )
                    local_by_target[(ch, cleaned_tgt)] = "sent"
                else:
                    active_native_target = None
                    transport_outcome_uncertain_or_sent = False
                    err_code = _extract_target_error_code(
                        cleaned_tgt, bridge_res, batch_err
                    )
                    await _shielded_mark_failed(
                        mark_target_fn,
                        str(run_id),
                        ch,
                        cleaned_tgt,
                        error_code=err_code,
                        expected_status="pending",
                    )
                    local_by_target[(ch, cleaned_tgt)] = "failed"
                if (ch, cleaned_tgt) in pending_targets:
                    pending_targets.remove((ch, cleaned_tgt))
            except asyncio.CancelledError:
                for rem_ch, rem_tgt in list(pending_targets):
                    try:
                        if (rem_ch, rem_tgt) == active_native_target:
                            await _shielded_mark_uncertain(
                                mark_target_fn,
                                str(run_id),
                                rem_ch,
                                rem_tgt,
                                error_code=active_uncertain_code,
                                expected_status="pending",
                            )
                            local_by_target[(rem_ch, rem_tgt)] = "pending"
                        else:
                            await _shielded_mark_failed(
                                mark_target_fn,
                                str(run_id),
                                rem_ch,
                                rem_tgt,
                                error_code="CancelledError",
                                expected_status="pending",
                            )
                            local_by_target[(rem_ch, rem_tgt)] = "failed"
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
                        local_by_target[(ch, cleaned_tgt)] = "pending"
                        await _shielded_mark_uncertain(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            error_code=uncertain_err,
                            expected_status="pending",
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
                        local_by_target[(ch, cleaned_tgt)] = "failed"
                        await _shielded_mark_failed(
                            mark_target_fn,
                            str(run_id),
                            ch,
                            cleaned_tgt,
                            error_code=err_code,
                            expected_status="pending",
                        )
                finally:
                    if (ch, cleaned_tgt) in pending_targets:
                        pending_targets.remove((ch, cleaned_tgt))
                continue

    persisted_by_target: dict[tuple[str, str], str] = dict(existing_by_target)
    if (
        mark_target_fn is not mark_target_delivery
        and fetch_deliveries_fn is fetch_execution_deliveries
    ):
        persisted_by_target.update(local_by_target)
    for d_row in await _safe_fetch_deliveries(
        fetch_deliveries_fn,
        str(run_id),
        use_default_db=use_delivery_db,
    ):
        d_ch = str(d_row.get("channel") or "").strip().lower()
        d_tgt = str(d_row.get("target") or "").strip()
        d_st = str(d_row.get("status") or "").strip().lower()
        if d_ch and d_tgt and d_st:
            persisted_by_target[(d_ch, d_tgt)] = d_st

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
        # Every expected target persisted and settled: any `sent` means the
        # run was dispatched (the rest were deliberately skipped); all
        # `skipped` means nothing was sent. Anything else is still in flight.
        if (
            missing_expected
            or not statuses
            or any(st not in ("sent", "skipped") for st in statuses)
        ):
            terminal_notify_status = "pending_dispatch"
        elif "sent" in statuses:
            terminal_notify_status = "dispatched"
        else:
            terminal_notify_status = "skipped"

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
                    "notify_status": terminal_notify_status,
                },
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
