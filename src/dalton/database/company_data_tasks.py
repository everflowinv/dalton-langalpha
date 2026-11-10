"""SQL for the unified company task API.

Company agent rows must match both ``scope = 'company'`` and the fixed company
user. Script rows are limited to caller-supplied manifest job keys. Reads do
not insert or seed. Script schedule writes update an existing definition row
and may cancel queued runs; they do not insert a definition.
"""

from __future__ import annotations

from typing import Any
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.server.database.pool import get_db_connection


def _clean_uuid(raw: Any) -> str | None:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        return None


async def find_present_job_keys(
    job_keys: list[str],
    *,
    conn: Any = None,
) -> set[str]:
    """Return manifest keys that have a definition row, including soft-deleted rows."""
    if not job_keys:
        return set()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT job_key
                FROM dalton_data_task_defs
                WHERE job_key = ANY(%s)
                """,
                (list(job_keys),),
            )
            rows = await cur.fetchall()
    return {str(row["job_key"]) for row in rows if row.get("job_key")}


async def list_company_agent_rows(
    *,
    status: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return every non-deleted company agent, optionally filtered by ``automations.status``.

    The caller sorts and pages the merged catalog. This query is not itself a page.
    """
    from src.server.database.automation import AUTOMATION_COLUMNS, _last_execution_join

    where_parts = [
        "automations.deleted_at IS NULL",
        "automations.scope = 'company'",
        "automations.user_id = %s",
    ]
    params: list[Any] = [COMPANY_SYSTEM_USER_ID]
    if status:
        where_parts.append("automations.status = %s")
        params.append(status)
    where_clause = " AND ".join(where_parts)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution
                FROM automations
                {_last_execution_join("automations")}
                WHERE {where_clause}
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_company_agent_detail(
    automation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return one company-owned agent, including a soft-deleted row.

    A row matches only when both ``scope`` and ``user_id`` are the company
    identity. ``deleted_at`` is left for the caller so a refusal can happen
    before a not-found answer.
    """
    from src.server.database.automation import AUTOMATION_COLUMNS, _last_execution_join

    clean_id = _clean_uuid(automation_id)
    if clean_id is None:
        return None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution
                FROM automations
                {_last_execution_join("automations")}
                WHERE automations.automation_id = %s::uuid
                  AND automations.scope = 'company'
                  AND automations.user_id = %s
                """,
                (clean_id, COMPANY_SYSTEM_USER_ID),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_automation_owner_identity(
    automation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return owner, scope, workspace, and deleted_at for any automation id."""
    clean_id = _clean_uuid(automation_id)
    if clean_id is None:
        return None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    automation_id,
                    user_id,
                    scope,
                    workspace_id,
                    deleted_at
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (clean_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


_DEF_READ_COLUMNS = """
    job_key,
    title,
    cron_expr,
    timezone,
    enabled,
    paused_by_user,
    notify_enabled,
    notify_routes,
    notify_storage_mode,
    cos_mode,
    config_version,
    active_version,
    updated_by,
    updated_at,
    migration_state,
    deleted_at
"""


async def list_active_manifest_def_rows(
    job_keys: list[str],
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return non-deleted definition rows whose keys are in ``job_keys``."""
    if not job_keys:
        return []
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_DEF_READ_COLUMNS}
                FROM dalton_data_task_defs
                WHERE deleted_at IS NULL
                  AND job_key = ANY(%s)
                """,
                (list(job_keys),),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_script_def_row(
    job_key: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return one definition row, including a soft-deleted row."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_DEF_READ_COLUMNS}
                FROM dalton_data_task_defs
                WHERE job_key = %s
                """,
                (job_key,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_script_history_row(
    job_key: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read script version counters and audit_history without writing."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    job_key,
                    config_version,
                    active_version,
                    audit_history,
                    updated_at,
                    deleted_at
                FROM dalton_data_task_defs
                WHERE job_key = %s
                """,
                (job_key,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_latest_script_runs(
    job_keys: list[str],
    *,
    conn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Return the newest run per active manifest job.

    Newest is ``created_at DESC, run_id ASC``, the same tie break as the
    company execution feed. A soft-deleted definition contributes no run.
    """
    if not job_keys:
        return {}
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (r.job_key)
                    r.run_id,
                    r.job_key,
                    r.status,
                    r.exit_code,
                    r.failure_reason,
                    r.notify_status,
                    r.triggered_by_user_id,
                    r.log_tail,
                    r.trigger_source,
                    r.scheduled_slot_utc,
                    r.started_at,
                    r.completed_at,
                    r.created_at
                FROM dalton_data_task_runs r
                JOIN dalton_data_task_defs d ON d.job_key = r.job_key
                WHERE d.deleted_at IS NULL
                  AND r.job_key = ANY(%s)
                ORDER BY r.job_key, r.created_at DESC, r.run_id ASC
                """,
                (list(job_keys),),
            )
            rows = await cur.fetchall()
    return {str(row["job_key"]): dict(row) for row in rows if row.get("job_key")}


async def list_company_agent_execution_window(
    *,
    automation_id: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    fetch_limit: int,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return the newest company-agent executions, already ordered for the merge.

    ``fetch_limit`` is the global window ``offset + limit + 1``. Any row in the
    global top of that size is inside the top of its own engine, so the caller
    can merge this window with the script window.
    """
    from src.server.database.automation import EXECUTION_COLUMNS

    if fetch_limit < 1:
        return []
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT
                    {EXECUTION_COLUMNS},
                    a.name AS automation_name,
                    a.trigger_type,
                    COALESCE(t.workspace_id, a.workspace_id) AS workspace_id,
                    'agent' AS engine_kind
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                LEFT JOIN conversation_threads t
                    ON t.conversation_thread_id = e.conversation_thread_id
                WHERE a.user_id = %s
                  AND a.scope = 'company'
                  AND a.deleted_at IS NULL
                  AND (%s::uuid IS NULL OR e.automation_id = %s::uuid)
                  AND (%s::uuid IS NULL OR e.conversation_thread_id = %s::uuid)
                  AND (%s::text IS NULL OR e.status = %s)
                ORDER BY e.created_at DESC, e.automation_execution_id ASC
                LIMIT %s
                """,
                (
                    COMPANY_SYSTEM_USER_ID,
                    automation_id,
                    automation_id,
                    thread_id,
                    thread_id,
                    status,
                    status,
                    fetch_limit,
                ),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def list_script_execution_window(
    *,
    job_keys: list[str],
    job_key: str | None = None,
    status: str | None = None,
    fetch_limit: int,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return the newest manifest-script runs, ordered for the company merge."""
    if fetch_limit < 1 or not job_keys:
        return []
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    r.run_id AS automation_execution_id,
                    ('data:' || r.job_key) AS automation_id,
                    d.title AS automation_name,
                    r.status,
                    r.scheduled_slot_utc AS scheduled_at,
                    r.started_at,
                    r.completed_at,
                    r.created_at,
                    r.exit_code,
                    r.failure_reason,
                    r.notify_status,
                    r.triggered_by_user_id,
                    r.log_tail,
                    r.trigger_source,
                    'script' AS engine_kind
                FROM dalton_data_task_runs r
                JOIN dalton_data_task_defs d ON d.job_key = r.job_key
                WHERE d.deleted_at IS NULL
                  AND d.job_key = ANY(%s)
                  AND (%s::text IS NULL OR r.job_key = %s)
                  AND (%s::text IS NULL OR r.status = %s)
                ORDER BY r.created_at DESC, r.run_id ASC
                LIMIT %s
                """,
                (
                    list(job_keys),
                    job_key,
                    job_key,
                    status,
                    status,
                    fetch_limit,
                ),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def read_user_model_preferences(
    user_ids: list[str],
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Return ``model_preference`` for the given users.

    A missing row is omitted. Database errors propagate; this function does not
    turn a failed preference read into an empty route list.
    """
    cleaned = [str(user_id) for user_id in user_ids if str(user_id or "").strip()]
    if not cleaned:
        return {}
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, model_preference
                FROM user_preferences
                WHERE user_id = ANY(%s)
                """,
                (cleaned,),
            )
            rows = await cur.fetchall()
    return {str(row["user_id"]): row.get("model_preference") for row in rows}


_DEF_LOCK_COLUMNS = _DEF_READ_COLUMNS + ",\n    audit_history"


def _require_write_conn(conn: Any) -> None:
    """Definition writes must stay on the caller's open transaction."""
    if conn is None:
        raise RuntimeError("script definition writes require the caller's transaction")


async def lock_script_def_for_update(
    job_key: str,
    *,
    conn: Any,
) -> dict[str, Any] | None:
    """Lock one existing definition row. Does not insert a missing row."""
    _require_write_conn(conn)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_DEF_LOCK_COLUMNS}
                FROM dalton_data_task_defs
                WHERE job_key = %s
                FOR UPDATE
                """,
                (job_key,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_locked_script_def(
    job_key: str,
    *,
    enabled: bool,
    paused_by_user: bool,
    cron_expr: str,
    timezone: str,
    config_version: int,
    active_version: int,
    updated_by: str,
    audit_history: list[Any],
    expected_config_version: int,
    conn: Any,
) -> bool:
    """Update schedule and version columns of the row locked by the caller.

    Notification, cos, command, migration, and deletion columns are not written.
    ``False`` means the locked version no longer matches ``expected_config_version``.
    """
    _require_write_conn(conn)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_defs
                SET
                    enabled = %s,
                    paused_by_user = %s,
                    cron_expr = %s,
                    timezone = %s,
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
                    cron_expr,
                    timezone,
                    config_version,
                    active_version,
                    updated_by,
                    Jsonb(audit_history),
                    job_key,
                    expected_config_version,
                ),
            )
            return cur.rowcount == 1


async def cancel_queued_script_runs(
    job_key: str,
    *,
    failure_reason: str,
    trigger_source: str | None = None,
    conn: Any,
) -> int:
    """Cancel queued runs that have not started. Leave starting and running rows."""
    _require_write_conn(conn)
    sql = """
        UPDATE dalton_data_task_runs
        SET
            status = 'cancelled',
            failure_reason = %s,
            notify_status = 'skipped',
            completed_at = NOW(),
            updated_at = NOW()
        WHERE job_key = %s
          AND status = 'queued'
    """
    params: list[Any] = [failure_reason, job_key]
    if trigger_source is not None:
        sql += " AND trigger_source = %s"
        params.append(trigger_source)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            return int(cur.rowcount or 0)
