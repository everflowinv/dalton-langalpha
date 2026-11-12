"""SQL for the automation recycle bin.

Mutations lock the task row before execution rows. Preference locks, when a
caller needs them, are taken by the service before these statements. This
module does not import the recycle service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection

# Transaction-scoped lock shared by every worker. Not an in-process leader flag.
PURGE_LOCK_NAMESPACE = "dalton"
PURGE_LOCK_NAME = "automation_recycle_purge"


def _first_value(row: Any) -> Any:
    """First column of a tuple row or a dict row."""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]

PURGE_AUTOMATIONS_SQL = """
DELETE FROM automations a
WHERE a.deleted_at IS NOT NULL
  AND a.deleted_at < NOW() - INTERVAL '30 days'
  AND NOT EXISTS (
      SELECT 1
      FROM automation_executions e
      WHERE e.automation_id = a.automation_id
        AND e.status IN ('pending', 'waiting', 'running')
  )
"""

_AGENT_LOCK_COLUMNS = """
    automation_id, user_id, name, file_name, description,
    trigger_type, cron_expression, timezone, trigger_config,
    next_run_at, last_run_at,
    instruction, workspace_id, llm_model, additional_context,
    thread_strategy, conversation_thread_id,
    status, max_failures, failure_count, disable_reason,
    delivery_config, metadata,
    scope, notify_enabled, notify_routes, notify_storage_mode,
    config_version, active_version, audit_history,
    created_by, updated_by, created_at, updated_at,
    deleted_at, deleted_by, pre_delete_desired_status
"""

_SCRIPT_LOCK_COLUMNS = """
    job_key, title, cron_expr, timezone, enabled, paused_by_user,
    notify_enabled, notify_routes, notify_storage_mode, cos_mode,
    config_version, active_version, audit_history, updated_by, updated_at,
    migration_state, deleted_at, deleted_by, pre_delete_enabled
"""


async def lock_automation_for_recycle(
    automation_id: str,
    *,
    owner_user_id: str | None,
    conn: Any,
) -> dict[str, Any] | None:
    """Lock one automation. ``owner_user_id`` limits the row to that owner."""
    sql = f"""
        SELECT {_AGENT_LOCK_COLUMNS}
        FROM automations
        WHERE automation_id = %s::uuid
    """
    params: list[Any] = [automation_id]
    if owner_user_id is not None:
        sql += " AND user_id = %s"
        params.append(owner_user_id)
    sql += " FOR UPDATE"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, tuple(params))
        row = await cur.fetchone()
    return dict(row) if row else None


async def lock_unfinished_executions(
    automation_id: str,
    *,
    conn: Any,
) -> list[dict[str, Any]]:
    """Lock unfinished executions after the automation row is already locked."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT automation_execution_id, status
            FROM automation_executions
            WHERE automation_id = %s::uuid
              AND status IN ('pending', 'waiting', 'running')
            ORDER BY automation_execution_id
            FOR UPDATE
            """,
            (automation_id,),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def skip_unstarted_executions(
    automation_id: str,
    *,
    skip_reason: str,
    conn: Any,
) -> int:
    """Skip pending and waiting executions. Leave running executions alone."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE automation_executions
            SET status = 'skipped',
                skip_reason = %s,
                completed_at = NOW()
            WHERE automation_id = %s::uuid
              AND status IN ('pending', 'waiting')
            """,
            (skip_reason, automation_id),
        )
        return int(cur.rowcount or 0)


async def skip_unstarted_executions_of_deleted_parents() -> int:
    """Skip pending and waiting executions whose parent is still soft-deleted.

    One UPDATE. ``deleted_at`` is read in that statement, so a restore that
    commits first keeps the execution the restore just created. Running
    executions stay for the normal settle. The statement takes no extra lock.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions AS e
                SET status = 'skipped',
                    skip_reason = 'task_deleted',
                    completed_at = NOW()
                WHERE e.status IN ('pending', 'waiting')
                  AND EXISTS (
                      SELECT 1
                      FROM automations AS a
                      WHERE a.automation_id = e.automation_id
                        AND a.deleted_at IS NOT NULL
                  )
                """
            )
            return int(cur.rowcount or 0)


async def once_schedule_completed(
    automation_id: str,
    when: Any,
    *,
    conn: Any,
) -> bool:
    """True when a completed execution was scheduled at ``when``."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM automation_executions
            WHERE automation_id = %s::uuid
              AND status = 'completed'
              AND scheduled_at = %s
            LIMIT 1
            """,
            (automation_id, when),
        )
        return await cur.fetchone() is not None


async def has_active_price_claim(
    automation_id: str,
    *,
    conn: Any,
) -> bool:
    """True when an unsettled execution shares the price claim's ``last_run_at``."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM automation_executions e
            JOIN automations a ON a.automation_id = e.automation_id
            WHERE e.automation_id = %s::uuid
              AND e.status IN ('pending', 'waiting', 'running')
              AND a.last_run_at IS NOT NULL
              AND e.created_at = a.last_run_at
            LIMIT 1
            """,
            (automation_id,),
        )
        return await cur.fetchone() is not None


async def live_file_name_taken(
    user_id: str,
    file_name: str,
    *,
    exclude_automation_id: str,
    conn: Any,
) -> bool:
    """True when another live row of ``user_id`` already uses ``file_name``."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM automations
            WHERE user_id = %s
              AND file_name = %s
              AND deleted_at IS NULL
              AND automation_id <> %s::uuid
            LIMIT 1
            """,
            (user_id, file_name, exclude_automation_id),
        )
        return await cur.fetchone() is not None


async def update_automation_recycle(
    automation_id: str,
    *,
    expected_config_version: int,
    fields: dict[str, Any],
    conn: Any,
) -> bool:
    """Write recycle columns. False when the locked version moved."""
    assignments = [
        "status = %s",
        "next_run_at = %s",
        "file_name = %s",
        "deleted_by = %s",
        "pre_delete_desired_status = %s",
        "config_version = %s",
        "active_version = %s",
        "updated_by = %s",
        "audit_history = %s",
        "name = %s",
        "description = %s",
        "trigger_type = %s",
        "cron_expression = %s",
        "timezone = %s",
        "instruction = %s",
        "workspace_id = %s",
        "llm_model = %s",
        "thread_strategy = %s",
        "conversation_thread_id = %s",
        "max_failures = %s",
        "trigger_config = %s",
        "additional_context = %s",
        "delivery_config = %s",
        "notify_enabled = %s",
        "notify_routes = %s",
        "updated_at = NOW()",
    ]
    params: list[Any] = [
        fields["status"],
        fields["next_run_at"],
        fields["file_name"],
        fields["deleted_by"],
        fields["pre_delete_desired_status"],
        fields["config_version"],
        fields["active_version"],
        fields["updated_by"],
        Jsonb(fields["audit_history"]),
        fields["name"],
        fields["description"],
        fields["trigger_type"],
        fields["cron_expression"],
        fields["timezone"],
        fields["instruction"],
        fields["workspace_id"],
        fields["llm_model"],
        fields["thread_strategy"],
        fields["conversation_thread_id"],
        fields["max_failures"],
        Jsonb(fields["trigger_config"]),
        Jsonb(fields["additional_context"]) if fields["additional_context"] is not None else None,
        Jsonb(fields["delivery_config"]),
        fields["notify_enabled"],
        Jsonb(fields["notify_routes"]),
    ]
    if fields.get("set_deleted_at"):
        assignments.append("deleted_at = NOW()")
    else:
        assignments.append("deleted_at = NULL")
    sql = f"""
        UPDATE automations
        SET {", ".join(assignments)}
        WHERE automation_id = %s::uuid
          AND config_version = %s
    """
    params.extend([automation_id, expected_config_version])
    async with conn.cursor() as cur:
        await cur.execute(sql, tuple(params))
        return cur.rowcount == 1


async def fetch_automation_read_row(
    automation_id: str,
    *,
    conn: Any,
) -> dict[str, Any] | None:
    """Read one automation, including a soft-deleted row, with its newest run."""
    from src.server.database.automation import AUTOMATION_COLUMNS, _last_execution_join

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {AUTOMATION_COLUMNS}, le.last_execution
            FROM automations
            {_last_execution_join("automations")}
            WHERE automations.automation_id = %s::uuid
            """,
            (automation_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def fetch_schedule_guard(
    automation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read the columns a claimed cron uses to decide whether to write next_run_at."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT trigger_type, status, deleted_at, next_run_at,
                       cron_expression, timezone, config_version
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (automation_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def automation_is_soft_deleted(
    automation_id: str,
    *,
    conn: Any = None,
) -> bool:
    """True when the row is missing or already in the recycle bin."""
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT deleted_at IS NOT NULL
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (automation_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return True
    return bool(_first_value(row))


async def purge_expired_automations(*, conn: Any = None) -> int:
    """Physically delete automations soft-deleted more than 30 days ago.

    Returns 0 when another session holds the advisory lock, or when no row
    qualifies. Data-task definitions are not in this statement.
    """
    async with get_db_connection(conn) as db:
        async with db.transaction():
            async with db.cursor() as cur:
                await cur.execute(
                    """
                    SELECT pg_try_advisory_xact_lock(
                        hashtext(%s),
                        hashtext(%s)
                    )
                    """,
                    (PURGE_LOCK_NAMESPACE, PURGE_LOCK_NAME),
                )
                locked = await cur.fetchone()
                if not bool(_first_value(locked)):
                    return 0
                await cur.execute(PURGE_AUTOMATIONS_SQL)
                return int(cur.rowcount or 0)


async def lock_script_for_recycle(
    job_key: str,
    *,
    conn: Any,
) -> dict[str, Any] | None:
    """Lock one data-task definition, including recycle columns."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {_SCRIPT_LOCK_COLUMNS}
            FROM dalton_data_task_defs
            WHERE job_key = %s
            FOR UPDATE
            """,
            (job_key,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def update_script_recycle(
    job_key: str,
    *,
    expected_config_version: int,
    enabled: bool,
    paused_by_user: bool,
    deleted_by: str | None,
    pre_delete_enabled: bool | None,
    set_deleted_at: bool,
    config_version: int,
    active_version: int,
    updated_by: str,
    audit_history: list[Any],
    conn: Any,
) -> bool:
    """Write script recycle columns. Schedule and notify columns stay as they are."""
    deleted_sql = "deleted_at = NOW()" if set_deleted_at else "deleted_at = NULL"
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE dalton_data_task_defs
            SET enabled = %s,
                paused_by_user = %s,
                deleted_by = %s,
                pre_delete_enabled = %s,
                {deleted_sql},
                config_version = %s,
                active_version = %s,
                updated_by = %s,
                audit_history = %s,
                updated_at = NOW()
            WHERE job_key = %s
              AND config_version = %s
            """,
            (
                enabled,
                paused_by_user,
                deleted_by,
                pre_delete_enabled,
                config_version,
                active_version,
                updated_by,
                Jsonb(audit_history),
                job_key,
                expected_config_version,
            ),
        )
        return cur.rowcount == 1
