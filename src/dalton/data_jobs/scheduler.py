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
    write_run_secrets_dir,
)
from src.dalton.data_jobs.evaluators import (
    TaskEvaluationContext,
    TaskEvaluationResult,
    TimeoutKind,
    evaluate_task_run,
)
from src.dalton.data_jobs.manifest import DEFAULT_TIMEOUT_SECONDS, sync_manifest_to_db
from src.dalton.data_jobs.runner_wrapper import (
    EXIT_CODE_NO_OUTPUT_TIMEOUT,
    EXIT_CODE_TOTAL_TIMEOUT,
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
    """Scan enabled container task defs and enqueue due cron runs, superseding stale queued rows (§2.2.3.A, §3.2.2).

    Before PR 0-2, ``runner = 'host'`` tasks (T1, T29) are excluded from cron enqueueing.
    """
    ref_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    slot_utc = ref_utc.replace(second=0, microsecond=0)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT job_key, cron_expr, timezone, timeout_seconds, enabled, paused_by_user, runner
            FROM dalton_data_task_defs
            WHERE enabled = TRUE
              AND paused_by_user = FALSE
              AND runner = 'container'
            ORDER BY job_key ASC
            """
        )
        defs = list(await cur.fetchall() or [])

    enqueued_run_ids: list[str] = []
    for task_def in defs:
        runner = str(task_def.get("runner") or "container")
        if runner != "container":
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
                # §2.2.3.A & §3.2.2: if a prior period's queued row has not started yet,
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


async def expire_low_memory_runs(
    conn: Any,
    *,
    now_utc: datetime | None = None,
) -> list[str]:
    """Transition ``queued`` runs with ``low_mem_since_utc IS NOT NULL`` past ``deadline_utc`` to ``skipped_low_memory`` (§2.2.3.B)."""
    ref_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expired_ids: list[str] = []

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT run_id, job_key
            FROM dalton_data_task_runs
            WHERE status = 'queued'
              AND low_mem_since_utc IS NOT NULL
              AND deadline_utc <= %(now_utc)s
            ORDER BY scheduled_slot_utc ASC, created_at ASC
            """,
            {"now_utc": ref_utc},
        )
        rows = list(await cur.fetchall() or [])

    for row in rows:
        run_id = str(_extract_field(row, "run_id", 0))
        job_key = str(_extract_field(row, "job_key", 1))
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
) -> ClaimedTaskRun | None:
    """Clean unclaimable head rows and atomically claim the first eligible queued run (§2.2.3, §3.2.3).

    Guarantees:
    - When ``dalton_data_task_slot.slot_state != 'idle'`` (`'running'` or `'reclaiming'`), returns
      ``None`` immediately; queued runs wait normally and are never marked ``skipped_low_memory``.
    - When ``slot_state == 'idle'``, expires any low-memory-blocked runs whose ``deadline_utc``
      has passed, then scans ``status = 'queued'`` in ``scheduled_slot_utc ASC, created_at ASC`` order:
      - Unclaimable head rows (``enabled = FALSE``, ``paused_by_user = TRUE``, or ``runner != 'container'``
        before PR 0-2) are marked ``status = 'cancelled'`` and the loop continues to the next queued row.
      - For ``runner = 'container'``, checks ``MemAvailable >= min_free_mem_mb`` (default 1280 MB);
        if below threshold, sets ``low_mem_since_utc = COALESCE(low_mem_since_utc, NOW())`` (or
        transitions to ``skipped_low_memory`` if already past ``deadline_utc``) and stops claiming.
      - Executes the two claim ``UPDATE`` statements inside a single transaction; both must affect
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

    await expire_low_memory_runs(conn, now_utc=ref_utc)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                r.run_id,
                r.job_key,
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
        job_key = str(cand["job_key"])
        enabled = bool(cand.get("enabled"))
        paused = bool(cand.get("paused_by_user"))
        runner = str(cand.get("runner") or "container")
        deadline_utc = cand["deadline_utc"]

        # Unclaimable head row (disabled, paused, missing def, or host runner before PR 0-2):
        # cancel and continue scanning the next queued row (§3.2.3).
        if not enabled or paused or cand.get("command_argv") is None or runner != "container":
            cancel_reason = (
                "host_runner_not_enabled"
                if runner != "container"
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
                        if cur.rowcount == 1:
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

        # Single-transaction atomic claim: both UPDATEs must affect 1 row or rollback (§3.2.3).
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
                        UPDATE dalton_data_task_runs r
                        SET status     = 'starting',
                            generation = %(generation)s,
                            updated_at = NOW()
                        FROM dalton_data_task_defs d
                        WHERE r.run_id = %(run_id)s::uuid
                          AND r.status = 'queued'
                          AND d.job_key = r.job_key
                          AND d.enabled = TRUE
                          AND d.paused_by_user = FALSE
                          AND d.runner = 'container'
                        """,
                        {"run_id": run_id, "generation": claimed_generation},
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
    create_cfg = build_container_create_config(
        job_key=claimed.job_key,
        run_id=claimed.run_id,
        generation=claimed.generation,
        command_argv=claimed.command_argv,
        workdir=claimed.workdir,
        timeout_seconds=claimed.timeout_seconds,
        no_output_timeout_seconds=claimed.no_output_timeout_seconds,
        notify_enabled=claimed.notify_enabled,
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
            if cur.rowcount != 1:
                return False
    await check_scheduler_lock(lock_conn)
    return True


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
) -> str:
    """Compute ``notify_status`` for a completed run (§3.4.1)."""
    if terminal_status != "succeeded" or not has_outbox_payload:
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

    terminal_status = evaluation.status
    has_outbox = outbox_payload is not None
    notify_status = resolve_notify_status(
        terminal_status=terminal_status,
        notify_enabled=claimed.notify_enabled,
        has_outbox_payload=has_outbox,
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
    path_map: DataTaskPathMap | None = None,
) -> str | None:
    """Reconcile active slot and leftover containers on scheduler startup/takeover (§3.4.2).

    Contract:
    - Every external Docker call (`stop`, `kill`, `remove`) is preceded by `check_scheduler_lock(lock_conn)`.
    - Running containers must be confirmed stopped (`Running == False`) and removed before any terminal
      `aborted` status is written or the execution slot is released to `'idle'`. If any container fails
      to stop or be removed, the slot is set to `'reclaiming'` and retried on the next cycle.
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

    all_removed = True
    removed_cids: set[str] = set()
    if active_run_id:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT run_id, job_key, status, container_id
                FROM dalton_data_task_runs
                WHERE run_id = %(run_id)s::uuid
                """,
                {"run_id": active_run_id},
            )
            run_row = await cur.fetchone()

        if run_row is not None:
            run_status = str(_extract_field(run_row, "status", 2))
            job_key = str(_extract_field(run_row, "job_key", 1))
            run_cid = _extract_field(run_row, "container_id", 3)

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

    outbox_payload, frozen_manifest = read_run_artifacts(claimed.run_id, path_map=paths)
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


async def run_data_task_scheduler_loop(
    *,
    stop_event: asyncio.Event | None = None,
    docker_client: DataTaskDockerClient | None = None,
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
    - Runs claimed container executions in a background ``asyncio.Task`` so the main loop continues
      waking every minute to enqueue due cron tasks even while a container is running, and only
      claims a new run when the execution slot is idle.
    """
    if not is_data_jobs_scheduler_enabled():
        logger.debug("Dalton data jobs scheduler is disabled; skipping loop startup")
        return

    owns_docker = docker_client is None
    client = docker_client or DataTaskDockerClient()
    stop = stop_event or asyncio.Event()
    open_lock_conn = lock_conn_factory or open_dedicated_lock_connection
    lock_conn: Any = None
    active_exec_task: asyncio.Task[Any] | None = None

    async def _run_claimed_in_background(claimed_run: ClaimedTaskRun) -> None:
        async with db_conn_factory() as exec_conn:
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
                        )

                    await check_scheduler_lock(lock_conn)
                    claimed = await claim_next_queued_run(work_conn)
                    if claimed is not None and claimed.runner == "container":
                        active_exec_task = asyncio.create_task(
                            _run_claimed_in_background(claimed)
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
