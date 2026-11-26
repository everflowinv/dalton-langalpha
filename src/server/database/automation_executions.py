"""Execution history queries for automations: transitions, settlement, delivery and the run feed."""

import logging
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Mapping, Optional, Sequence
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

# DALTON-HOOK(company-automations): the locked parent read uses these columns
# DALTON-HOOK(history-computer-scope): reuse shared thread-ownership and target-computer workspace SQL
from src.server.database.automation import (
    AUTOMATION_COLUMNS,
    EXECUTION_COLUMNS,
    UNSETTLED_STATUSES,
    not_a_server_skip,
    owned_execution_thread_join_sql,
    target_computer_workspace_predicate_sql,
)
from src.server.contracts.status import (
    RESTART_INTERRUPTED_META,
    RESTART_RESUME_TTL,
    is_restart_interrupted,
    restart_resume_expired,
)
from src.server.database.home_stand_in import home_stand_in_could_run_sql
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection
from src.server.database.ssh_tunnels import awaited_computer_back_sql
from src.server.models.automation import WaitReason
from src.server.utils.db import UpdateQueryBuilder

logger = logging.getLogger(__name__)

# DALTON-HOOK(automation-continuation-identity): single-source execution timeout cap & pending background work SQL
EXECUTION_TIMEOUT_MIN_SECONDS = 3 * 3600
_TIMEOUT_JSONB_FIELDS = ("metadata", "trigger_config")
_POSITIVE_INT_STR_RE = re.compile(r"^[0-9]+(?:\.0+)?$")
UNSET_RUN_ID = object()


def _parse_positive_int_seconds(raw: Any) -> Optional[float]:
    """Accept positive int or string/float losslessly convertible to one; reject junk."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return float(raw) if raw > 0 else None
    if isinstance(raw, float):
        if math.isfinite(raw) and raw > 0 and raw.is_integer():
            return float(int(raw))
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not _POSITIVE_INT_STR_RE.fullmatch(stripped):
            return None
        val = int(stripped.split(".", 1)[0])
        return float(val) if val > 0 else None
    return None


def default_execution_timeout_seconds() -> float:
    """Default execution timeout cap: max(3 hours, platform workflow_timeout)."""
    from src.config.settings import get_workflow_timeout

    try:
        workflow_timeout = float(get_workflow_timeout())
    except Exception:
        workflow_timeout = 0.0
    if not math.isfinite(workflow_timeout) or workflow_timeout < 0:
        workflow_timeout = 0.0
    return float(max(EXECUTION_TIMEOUT_MIN_SECONDS, workflow_timeout))


def execution_timeout_seconds(automation: Optional[Mapping[str, Any]]) -> float:
    """Single timeout parser used by both Python checks and SQL parameter assembly."""
    if isinstance(automation, Mapping):
        for field in _TIMEOUT_JSONB_FIELDS:
            container = automation.get(field)
            raw = container.get("timeout_seconds") if isinstance(container, Mapping) else None
            parsed = _parse_positive_int_seconds(raw)
            if parsed is not None:
                return parsed
    return default_execution_timeout_seconds()


def _bool_row(row: Any) -> bool:
    if isinstance(row, Mapping):
        return bool(next(iter(row.values()), False))
    return bool(row and row[0])


def _pending_background_work_sql(
    *,
    execution_id_sql: str,
    thread_id_sql: str,
    linked_run_id_sql: str,
    exclude_run_id_sql: str = "NULL::uuid",
) -> str:
    """SQL predicate for open background work or continuation turns of an execution."""
    def _belongs_to_exec(run_col: str) -> str:
        return (
            f"(({linked_run_id_sql} IS NOT NULL AND {run_col} = {linked_run_id_sql})"
            f" OR EXISTS (SELECT 1 FROM conversation_responses pr"
            f" WHERE pr.conversation_response_id = {run_col}"
            f" AND pr.metadata->>'automation_execution_id' = ({execution_id_sql})::text))"
        )

    excluded_dispatch = (
        f"({exclude_run_id_sql} IS NULL"
        f" OR COALESCE(o.payload->>'dispatched_run_id', '') <> ({exclude_run_id_sql})::text)"
    )
    return f"""(
        EXISTS (
            SELECT 1 FROM subagent_runs sr
            WHERE sr.thread_id = {thread_id_sql}
              AND sr.status = 'in_progress'
              AND {_belongs_to_exec("sr.parent_run_id")}
        )
        OR EXISTS (
            SELECT 1 FROM command_runs cr
            WHERE cr.conversation_thread_id = {thread_id_sql}
              AND cr.status = 'running'
              AND {_belongs_to_exec("cr.run_id")}
        )
        OR EXISTS (
            SELECT 1 FROM conversation_responses r_live
            WHERE r_live.conversation_thread_id = {thread_id_sql}
              AND r_live.status = 'in_progress'
              AND r_live.metadata->>'automation_execution_id' = ({execution_id_sql})::text
              AND ({exclude_run_id_sql} IS NULL
                   OR r_live.conversation_response_id <> {exclude_run_id_sql})
        )
        OR EXISTS (
            SELECT 1 FROM hook_outbox o
            LEFT JOIN subagent_runs sr
              ON sr.task_run_id::text = o.payload->>'task_run_id'
            JOIN conversation_responses pr
              ON pr.conversation_response_id = COALESCE(o.run_id, sr.parent_run_id)
            WHERE o.ordering_key = ({thread_id_sql})::text
              AND o.hook_type = 'task_report_back'
              AND o.status IN ('pending', 'claimed')
              AND (sr.task_run_id IS NULL OR sr.result_delivered_at IS NULL)
              AND pr.metadata->>'automation_execution_id' = ({execution_id_sql})::text
              AND {excluded_dispatch}
        )
        OR EXISTS (
            SELECT 1 FROM hook_outbox o
            JOIN command_runs cr
              ON cr.command_id = o.payload->>'command_id'
            JOIN conversation_responses pr
              ON pr.conversation_response_id = cr.run_id
            WHERE o.ordering_key = ({thread_id_sql})::text
              AND o.hook_type = 'command_report_back'
              AND o.status IN ('pending', 'claimed')
              AND cr.result_delivered_at IS NULL
              AND (cr.status IN ('completed', 'failed')
                   OR (cr.status = 'stopped' AND cr.stopped_by = 'user'))
              AND pr.metadata->>'automation_execution_id' = ({execution_id_sql})::text
              AND {excluded_dispatch}
        )
    )"""


async def _count_strike(cur, automation_id: str, *, fuse: bool = False) -> int:
    """Count a failure, disabling the automation once it reaches its limit,
    or at once for a ``fuse``.

    A disable writes ``disable_reason`` ('provider_auth' for a fuse, else
    'max_failures'), so the user is told which of the two switched it off.
    It keeps a one-time automation's time, as a pause does: a manual run that
    fused it leaves the scheduled one for a resume to restore. Only a live
    automation is switched to ``disabled``. A paused one keeps its status and
    still stores the reason. A finish or an earlier disable, and its reason,
    stand. A later strike does not replace ``provider_auth`` with
    ``max_failures``. A later success does not clear the reason; only resume
    or enable does.
    """
    # DALTON-HOOK(no-strike): P0F.3 dalton-* automations never auto-disable on consecutive failures
    strike_limit = (
        "(%(fuse)s OR (failure_count + 1 >= max_failures AND name NOT LIKE 'dalton-%%'))"
    )
    disables = f"{strike_limit} AND status IN ('active', 'executing')"
    # A paused parent is not flipped to disabled, but the reason is kept.
    records_reason = f"{strike_limit} AND status IN ('active', 'executing', 'paused')"
    dalton_backoff = (
        "GREATEST(COALESCE(next_run_at, NOW()), NOW() + make_interval("
        "secs => LEAST(604800, 3600 * (1 << LEAST(failure_count, 8)))))"
    )
    await cur.execute(f"""
        UPDATE automations
        SET failure_count = failure_count + 1,
            status = CASE WHEN {disables} THEN 'disabled' ELSE status END,
            next_run_at = CASE WHEN {disables} AND trigger_type <> 'once'
                               THEN NULL
                               WHEN name LIKE 'dalton-%%' AND trigger_type <> 'price'
                               THEN {dalton_backoff}
                               ELSE next_run_at END,
            disable_reason = CASE
                WHEN disable_reason = 'provider_auth' THEN 'provider_auth'
                WHEN {records_reason} THEN %(reason)s
                ELSE disable_reason END
        FROM (SELECT status AS was FROM automations
              WHERE automation_id = %(automation_id)s) before_strike
        WHERE automation_id = %(automation_id)s
        RETURNING failure_count, status = 'disabled' AND before_strike.was <> 'disabled' AS disabled
    """, {
        "fuse": fuse,
        "reason": "provider_auth" if fuse else "max_failures",
        "automation_id": automation_id,
    })
    row = await cur.fetchone()
    if not row:
        return 0
    if row["disabled"]:
        logger.warning(
            f"[automation_db] Auto-disabled automation {automation_id} "
            f"(reason={'provider_auth' if fuse else 'max_failures'}, "
            f"failure_count={row['failure_count']})"
        )
    return row["failure_count"]


def _execution_update(
    to: str, where_clause: str, where_params: list, fields: Dict[str, Any]
) -> tuple[str, tuple]:
    builder = UpdateQueryBuilder()
    builder.add_field("status", to)
    for column, value in fields.items():
        builder.add_field(column, value)
    return builder.build(
        table="automation_executions",
        where_clause=where_clause,
        where_params=where_params,
        returning_columns=[
            "conversation_thread_id", "conversation_response_id",
            "scheduled_at", "wait_reason", "awaits_computer", "checked_computer_id",
            "llm_model",  # DALTON-HOOK(cos-settings-tools)
            "reasoning_effort",  # DALTON-HOOK(cos-settings-tools)
            "llm_model_system_only",  # DALTON-HOOK(cos-settings-tools)
            "snapshot_taken",  # DALTON-HOOK(cos-settings-tools)
        ],
        include_updated_at=False,  # no updated_at column on executions
    )


async def transition_execution(
    execution_id: str,
    *,
    from_statuses: Sequence[str],
    to: str,
    automation_id: Optional[str] = None,
    wait_reason: Optional[WaitReason] = None,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """Move an execution to ``to`` only while its status is one of
    ``from_statuses``; the row's thread and run, when it was due and what it
    last waited for, or None when it was not.

    The guarded write every move of a live firing makes, so the executor, a
    skip and the sweep can race and exactly one of them wins. Ending a firing
    is ``settle_execution``. A field passed as None leaves its column as it
    is.
    """
    where_clause = "automation_execution_id = %s AND status = ANY(%s)"
    where_params: list = [execution_id, list(from_statuses)]
    if automation_id is not None:
        where_clause += " AND automation_id = %s"
        where_params.append(automation_id)
    if wait_reason is not None:
        fields["wait_reason"] = wait_reason
    query, params = _execution_update(to, where_clause, where_params, fields)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()
            return dict(row) if row else None


async def begin_execution_if_not_deleted(
    execution_id: str,
    *,
    started_at: datetime,
) -> Optional[Dict[str, Any]]:
    """Move a pending execution to running only while its automation is live.

    DALTON-HOOK(company-automations): a soft-deleted automation does not start.
    DALTON-HOOK(coverage-units): serialize execution entry against unit handoff with FOR SHARE on unit row,
    require active status and lead match, and stamp coverage_unit_id/coverage_unit_version.
    The shared ``transition_execution`` helper is left unchanged.
    """
    from src.dalton.autonomy.classification import (
        managed_unit_automation_for_unit_sql_predicate,
        managed_unit_automation_sql_predicate,
        sql_trim_scope_text,
    )
    from src.dalton.settings import is_coverage_units_enabled

    async with get_db_connection() as conn:
        if not is_coverage_units_enabled():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE automation_executions e
                    SET status = 'running',
                        started_at = %s,
                        llm_model = CASE WHEN e.snapshot_taken THEN e.llm_model ELSE a.llm_model END,
                        reasoning_effort = CASE WHEN e.snapshot_taken THEN e.reasoning_effort ELSE a.reasoning_effort END,
                        llm_model_system_only = CASE
                            WHEN e.snapshot_taken THEN e.llm_model_system_only
                            ELSE COALESCE((a.metadata->>'llm_model_system_only')::boolean, FALSE)
                        END,
                        snapshot_taken = TRUE
                    FROM automations a
                    WHERE e.automation_execution_id = %s
                      AND e.status = 'pending'
                      AND a.automation_id = e.automation_id
                      AND a.deleted_at IS NULL
                    RETURNING e.conversation_thread_id, e.conversation_response_id,
                              e.scheduled_at, e.wait_reason, e.awaits_computer, e.checked_computer_id,
                              e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken
                    """,  # DALTON-HOOK(cos-settings-tools)
                    (started_at, execution_id),
                )
                row = await cur.fetchone()
                return dict(row) if row else None

        managed_base_pred = managed_unit_automation_sql_predicate("a")
        managed_unit_pred = managed_unit_automation_for_unit_sql_predicate("a", "u")
        unit_lead_expr = (
            "COALESCE(NULLIF("
            + sql_trim_scope_text("u.lead_analyst_user_id")
            + ", ''), "
            + sql_trim_scope_text("u.owner_user_id")
            + ")"
        )
        auto_owner_expr = sql_trim_scope_text("a.user_id")

        class _WsChangedRetry(Exception):
            pass

        for _attempt in range(3):
            try:
                async with conn.transaction():
                    async with conn.cursor(row_factory=dict_row) as cur:
                        if _attempt == 0:
                            # Fast path for ordinary (non-managed) tasks: 1 SQL query, zero probe/unit locks.
                            await cur.execute(
                                f"""
                                UPDATE automation_executions e
                                SET status = 'running',
                                    started_at = %s,
                                    llm_model = CASE WHEN e.snapshot_taken THEN e.llm_model ELSE a.llm_model END,
                                    reasoning_effort = CASE WHEN e.snapshot_taken THEN e.reasoning_effort ELSE a.reasoning_effort END,
                                    llm_model_system_only = CASE
                                        WHEN e.snapshot_taken THEN e.llm_model_system_only
                                        ELSE COALESCE((a.metadata->>'llm_model_system_only')::boolean, FALSE)
                                    END,
                                    snapshot_taken = TRUE
                                FROM automations a
                                WHERE e.automation_execution_id = %s
                                  AND e.status = 'pending'
                                  AND a.automation_id = e.automation_id
                                  AND a.deleted_at IS NULL
                                  AND NOT ({managed_base_pred})
                                RETURNING e.conversation_thread_id, e.conversation_response_id,
                                          e.scheduled_at, e.wait_reason, e.awaits_computer, e.checked_computer_id,
                                          e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
                                          NULL::text AS coverage_unit_id, NULL::timestamptz AS coverage_unit_version
                                """,  # DALTON-HOOK(cos-settings-tools)
                                (started_at, execution_id),
                            )
                            fast_row = await cur.fetchone()
                            if fast_row is not None:
                                return dict(fast_row)

                        # Managed-task candidate path: probe current workspace_id, lock its active coverage
                        # unit FOR SHARE BEFORE locking automation_executions FOR UPDATE, and verify under
                        # the execution lock that workspace_id did not change concurrently.
                        # If ws_changed occurs, raising _WsChangedRetry rolls back this attempt's transaction,
                        # releasing both the old unit FOR SHARE lock and FOR UPDATE OF e before retrying.
                        await cur.execute(
                            f"""
                            SELECT a.workspace_id,
                                   CASE
                                       WHEN jsonb_typeof(a.metadata) = 'object'
                                        AND NULLIF(BTRIM(COALESCE(a.metadata->>'unit_id', '')), '') ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                                       THEN BTRIM(a.metadata->>'unit_id')
                                       ELSE NULL
                                   END AS meta_unit_id,
                                   ({managed_base_pred}) AS is_managed_candidate
                            FROM automation_executions e
                            JOIN automations a ON a.automation_id = e.automation_id
                            WHERE e.automation_execution_id = %s
                              AND e.status = 'pending'
                              AND a.deleted_at IS NULL
                            """,
                            (execution_id,),
                        )
                        ws_probe = await cur.fetchone()
                        if ws_probe is None:
                            return None
                        if not ws_probe.get("is_managed_candidate", True):
                            return None
                        probe_ws_id = ws_probe.get("workspace_id")
                        probe_meta_unit_id = ws_probe.get("meta_unit_id")
                        if probe_ws_id is not None and probe_meta_unit_id is not None:
                            await cur.execute(
                                """
                                SELECT unit_id, updated_at
                                FROM dalton_coverage_units
                                WHERE (workspace_id = %s::uuid AND LOWER(BTRIM(status)) = 'active')
                                   OR unit_id = %s::uuid
                                ORDER BY unit_id
                                FOR SHARE
                                """,
                                (probe_ws_id, probe_meta_unit_id),
                            )
                            await cur.fetchall()
                        elif probe_ws_id is not None:
                            await cur.execute(
                                """
                                SELECT unit_id, updated_at
                                FROM dalton_coverage_units
                                WHERE workspace_id = %s::uuid
                                  AND LOWER(BTRIM(status)) = 'active'
                                ORDER BY unit_id
                                FOR SHARE
                                """,
                                (probe_ws_id,),
                            )
                            await cur.fetchall()
                        elif probe_meta_unit_id is not None:
                            await cur.execute(
                                """
                                SELECT unit_id, updated_at
                                FROM dalton_coverage_units
                                WHERE unit_id = %s::uuid
                                FOR SHARE
                                """,
                                (probe_meta_unit_id,),
                            )
                            await cur.fetchall()

                        stale_unit_ref_pred = f"""
                            EXISTS (
                                SELECT 1
                                FROM dalton_coverage_units u_ref
                                WHERE {managed_base_pred}
                                  AND jsonb_typeof(a.metadata) = 'object'
                                  AND NULLIF(BTRIM(COALESCE(a.metadata->>'unit_id', '')), '') ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                                  AND BTRIM(a.metadata->>'unit_id') = u_ref.unit_id::text
                                  AND (
                                      LOWER(BTRIM(u_ref.status)) <> 'active'
                                      OR u_ref.workspace_id IS DISTINCT FROM a.workspace_id
                                      OR COALESCE(NULLIF({sql_trim_scope_text("u_ref.lead_analyst_user_id")}, ''), {sql_trim_scope_text("u_ref.owner_user_id")}) <> {auto_owner_expr}
                                  )
                            )
                        """
                        await cur.execute(
                            f"""
                            WITH target AS (
                                SELECT e.automation_execution_id,
                                       a.automation_id,
                                       (a.workspace_id IS DISTINCT FROM %s::uuid) AS ws_changed,
                                       (
                                           EXISTS (
                                               SELECT 1
                                               FROM dalton_coverage_units u
                                               WHERE a.workspace_id IS NOT NULL
                                                 AND u.workspace_id = a.workspace_id
                                                 AND LOWER(BTRIM(u.status)) = 'active'
                                                 AND {managed_unit_pred}
                                                 AND {unit_lead_expr} <> {auto_owner_expr}
                                           )
                                           OR ({stale_unit_ref_pred})
                                       ) AS lead_mismatch,
                                       (
                                           EXISTS (
                                               SELECT 1
                                               FROM dalton_coverage_units u
                                               WHERE a.workspace_id IS NOT NULL
                                                 AND u.workspace_id = a.workspace_id
                                                 AND LOWER(BTRIM(u.status)) = 'active'
                                                 AND {managed_unit_pred}
                                                 AND (a.status NOT IN ('active', 'executing') OR {unit_lead_expr} <> {auto_owner_expr})
                                           )
                                           OR ({stale_unit_ref_pred})
                                       ) AS reject_handoff,
                                       (
                                           SELECT u.unit_id
                                           FROM dalton_coverage_units u
                                           WHERE a.workspace_id IS NOT NULL
                                             AND u.workspace_id = a.workspace_id
                                             AND LOWER(BTRIM(u.status)) = 'active'
                                             AND {managed_unit_pred}
                                             AND {unit_lead_expr} = {auto_owner_expr}
                                           LIMIT 1
                                       ) AS stamp_unit_id,
                                       (
                                           SELECT u.updated_at
                                           FROM dalton_coverage_units u
                                           WHERE a.workspace_id IS NOT NULL
                                             AND u.workspace_id = a.workspace_id
                                             AND LOWER(BTRIM(u.status)) = 'active'
                                             AND {managed_unit_pred}
                                             AND {unit_lead_expr} = {auto_owner_expr}
                                           LIMIT 1
                                       ) AS stamp_unit_version,
                                       -- DALTON-HOOK(cos-settings-tools): capture automation config for lazy rolling-upgrade snapshot backfill
                                       a.llm_model AS auto_llm_model,
                                       a.reasoning_effort AS auto_reasoning_effort,
                                       COALESCE((a.metadata->>'llm_model_system_only')::boolean, FALSE) AS auto_system_only
                                FROM automation_executions e
                                JOIN automations a ON a.automation_id = e.automation_id
                                WHERE e.automation_execution_id = %s
                                  AND e.status = 'pending'
                                  AND a.deleted_at IS NULL
                                FOR UPDATE OF e
                            ),
                            skipped AS (
                                UPDATE automation_executions e
                                SET status = 'skipped',
                                    skip_reason = COALESCE(NULLIF(e.skip_reason, ''), 'unit_handoff'),
                                    completed_at = %s,
                                    error_message = 'Skipped: managed unit automation is inactive or owner is no longer active lead analyst'
                                FROM target t
                                WHERE e.automation_execution_id = t.automation_execution_id
                                  AND NOT t.ws_changed
                                  AND t.reject_handoff
                                RETURNING e.automation_id
                            ),
                            restored AS (
                                UPDATE automations a
                                SET status = CASE WHEN t.lead_mismatch THEN 'paused' ELSE 'active' END,
                                    next_run_at = CASE WHEN t.lead_mismatch THEN NULL ELSE a.next_run_at END
                                FROM target t
                                WHERE a.automation_id = t.automation_id
                                  AND NOT t.ws_changed
                                  AND t.reject_handoff
                                  AND a.status = 'executing'
                                RETURNING a.automation_id
                            ),
                            started AS (
                                UPDATE automation_executions e
                                SET status = 'running',
                                    started_at = %s,
                                    coverage_unit_id = t.stamp_unit_id,
                                    coverage_unit_version = t.stamp_unit_version,
                                    -- DALTON-HOOK(cos-settings-tools): lazily snapshot rolling-upgrade rows (snapshot_taken=FALSE) and freeze
                                    llm_model = CASE WHEN e.snapshot_taken THEN e.llm_model ELSE t.auto_llm_model END,
                                    reasoning_effort = CASE WHEN e.snapshot_taken THEN e.reasoning_effort ELSE t.auto_reasoning_effort END,
                                    llm_model_system_only = CASE WHEN e.snapshot_taken THEN e.llm_model_system_only ELSE t.auto_system_only END,
                                    snapshot_taken = TRUE
                                FROM target t
                                WHERE e.automation_execution_id = t.automation_execution_id
                                  AND NOT t.ws_changed
                                  AND NOT t.reject_handoff
                                RETURNING e.conversation_thread_id, e.conversation_response_id,
                                          e.scheduled_at, e.wait_reason, e.awaits_computer, e.checked_computer_id,
                                          e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
                                          e.coverage_unit_id, e.coverage_unit_version
                            )
                            SELECT s.conversation_thread_id, s.conversation_response_id,
                                   s.scheduled_at, s.wait_reason, s.awaits_computer, s.checked_computer_id,
                                   s.llm_model, s.reasoning_effort, s.llm_model_system_only, s.snapshot_taken,
                                   s.coverage_unit_id, s.coverage_unit_version,
                                   FALSE AS ws_changed
                            FROM started s
                            UNION ALL
                            SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL::text, NULL::varchar, NULL::boolean, NULL::boolean, NULL, NULL,
                                   TRUE AS ws_changed
                            FROM target t
                            WHERE t.ws_changed
                            """,  # DALTON-HOOK(cos-settings-tools)
                            (probe_ws_id, execution_id, started_at, started_at),
                        )
                        row = await cur.fetchone()
                        if row and row.get("ws_changed"):
                            raise _WsChangedRetry()
                        if not row:
                            return None
                        result = dict(row)
                        result.pop("ws_changed", None)
                        return result
            except _WsChangedRetry:
                continue
        return None


# How long a one-shot whose firing was put off waits to come due again.
DEFER_INTERVAL = "interval '1 hour'"

# What a settled firing leaves on its automation. A close only lands while the
# automation is still on the firing being settled: a reschedule, pause or
# disable made during the run wins, and a manual run never closes anything.
# The firing that claimed the automation is recognised by the stamp the claim
# shares with it (see claim_due_automations and claim_price_firing). A price
# alert with no stamp was claimed before claims wrote one, and only the
# 'executing' status its trigger set can speak for it.
_SCHEDULE_SQL = {
    "close_once": """
        UPDATE automations SET status = 'completed', next_run_at = NULL
        WHERE automation_id = %s AND status = 'active'
          AND next_run_at IS NULL AND last_run_at = %s
    """,
    # DALTON-HOOK(precheck-outcome): A9 defer one-time automation by 1 day on budget exhaustion
    "defer_once_1d": """
        UPDATE automations
        SET status = 'active',
            next_run_at = COALESCE(next_run_at, COALESCE(last_run_at, NOW()) + INTERVAL '1 day')
        WHERE automation_id = %s AND status IN ('active', 'executing')
          AND (last_run_at = %s OR last_run_at IS NULL)
    """,
    "close_price": """
        UPDATE automations SET status = 'completed', next_run_at = NULL
        WHERE automation_id = %s AND status = 'executing'
          AND (last_run_at = %s OR last_run_at IS NULL)
    """,
    "rearm_price": """
        UPDATE automations SET status = 'active'
        WHERE automation_id = %s AND status = 'executing'
          AND (last_run_at = %s OR last_run_at IS NULL)
    """,
    # Put off rather than used up: due again after DEFER_INTERVAL, which is
    # what keeps a firing refused for as long as a suspension lasts from
    # coming due on every pass. A price alert's ``next_run_at`` is when it
    # watches again (``get_active_price_automations``).
    "defer_once": f"""
        UPDATE automations SET next_run_at = NOW() + {DEFER_INTERVAL}
        WHERE automation_id = %s AND status = 'active'
          AND next_run_at IS NULL AND last_run_at = %s
    """,
    "defer_price": f"""
        UPDATE automations SET status = 'active', next_run_at = NOW() + {DEFER_INTERVAL}
        WHERE automation_id = %s AND status = 'executing'
          AND (last_run_at = %s OR last_run_at IS NULL)
    """,
}

ScheduleAction = Literal[
    "close_once", "close_price", "rearm_price", "defer_once", "defer_once_1d", "defer_price"
]


# DALTON-HOOK(automation-continuation-identity): in-lock liveness predicate and shared pending-work check
def _is_run_live(run_row: Optional[Mapping[str, Any]]) -> bool:
    """True when ``run_row`` is still ``in_progress`` or awaiting restart resume."""
    if run_row is None:
        return False
    status = run_row.get("status")
    if status == "in_progress":
        return True
    return bool(
        status == "cancelled"
        and is_restart_interrupted(run_row)
        and not restart_resume_expired(run_row.get("cancel_requested_at"))
    )


async def _has_pending_background_work_on_cur(
    cur: Any,
    execution_id: str,
    *,
    thread_id: Optional[str],
    excluding_run_id: Optional[str] = None,
) -> bool:
    if not thread_id:
        return False
    pred = _pending_background_work_sql(
        execution_id_sql="e.automation_execution_id",
        thread_id_sql="%(thread_id)s::uuid",
        linked_run_id_sql="e.conversation_response_id",
        exclude_run_id_sql="%(exclude_run_id)s::uuid",
    )
    await cur.execute(
        f"""
        SELECT EXISTS (
            SELECT 1 FROM automation_executions e
            WHERE e.automation_execution_id = %(execution_id)s::uuid
              AND e.status = 'running'
              AND {pred}
        )
        """,
        {
            "execution_id": execution_id,
            "thread_id": thread_id,
            "exclude_run_id": excluding_run_id,
        },
    )
    row = await cur.fetchone()
    return _bool_row(row)


async def settle_execution(
    execution_id: str,
    *,
    automation_id: str,
    from_statuses: Sequence[str],
    to: str,
    strike: Optional[Literal["count", "fuse", "reset"]] = None,
    schedule: Optional[ScheduleAction] = None,
    quiet_for: Optional[int] = None,
    # DALTON-HOOK(automation-continuation-identity): lock/CAS options for settlement
    expected_run_id: Any = UNSET_RUN_ID,
    check_run_state_in_lock: Optional[Literal["terminal", "completed_idle", "timeout"]] = None,
    parked: bool = False,
    in_transaction: Optional[Callable[[Any, Dict[str, Any]], Awaitable[Any]]] = None,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """End a firing and write what it leaves on its automation, in one
    transaction; None when the firing was not in ``from_statuses``.

    One transaction, so a failure between the two can never leave a price
    alert 'executing' behind a settled firing. ``quiet_for`` settles only a
    firing whose heartbeat has been quiet that many seconds, which is what
    keeps the sweep off a firing whose process came back; ``parked`` only one
    still parked for its computer, which keeps the parked sweep's verdict,
    read before the row was locked, off a firing released and waiting since
    for something else. Returns the row's
    thread and run, ``settled_from``, the status it left, the ``delivery``
    its start kept (``stamp_delivery``), and the ``previous_failure_reason``
    and ``previous_delivery_result`` of the automation's newest firing
    settled before this one, a server skip aside, so a caller can tell a
    repeat the channel already heard from news. ``in_transaction`` is
    awaited with the connection and that row before the commit, for work
    that must commit with the settle or not at all.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Automation first, execution second: the order a delete's
                # cascade takes the same two rows in.
                await cur.execute("""
                    SELECT 1 FROM automations WHERE automation_id = %s::uuid
                    FOR NO KEY UPDATE
                """, (automation_id,))
                if await cur.fetchone() is None:
                    return None
                quiet = ""
                params: list = [execution_id, automation_id]
                if quiet_for is not None:
                    quiet = "AND heartbeat_at < NOW() - make_interval(secs => %s::double precision)"
                    params.append(quiet_for)
                if parked:
                    quiet += f" AND {_PARKED.format(e='automation_executions')}"
                await cur.execute(f"""
                    SELECT status, created_at, delivery,
                           conversation_thread_id, conversation_response_id,
                           coverage_unit_id, coverage_unit_version
                    FROM automation_executions
                    WHERE automation_execution_id = %s::uuid AND automation_id = %s::uuid
                      {quiet}
                    FOR UPDATE
                """, tuple(params))
                prior = await cur.fetchone()
                if prior is None or prior["status"] not in from_statuses:
                    return None

                # DALTON-HOOK(automation-continuation-identity): CAS on linked run and re-read state under FOR UPDATE
                prior_run_id = (
                    str(prior["conversation_response_id"])
                    if prior.get("conversation_response_id") is not None
                    else None
                )
                exp_run = None
                if expected_run_id is not UNSET_RUN_ID:
                    exp_run = str(expected_run_id) if expected_run_id is not None else None
                    if prior_run_id != exp_run:
                        return None

                thread_id_val = (
                    str(prior["conversation_thread_id"])
                    if prior.get("conversation_thread_id") is not None
                    else None
                )
                if thread_id_val is not None:
                    await cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM conversation_responses r_live
                            WHERE r_live.conversation_thread_id = %s::uuid
                              AND r_live.status = 'in_progress'
                              AND r_live.metadata->>'automation_execution_id' = %s::text
                        )
                    """, (thread_id_val, execution_id))
                    if _bool_row(await cur.fetchone()):
                        return None

                if check_run_state_in_lock is not None and prior_run_id is not None:
                    await cur.execute("""
                        SELECT status, metadata, cancel_requested_at
                        FROM conversation_responses
                        WHERE conversation_response_id = %s::uuid
                    """, (prior_run_id,))
                    live_row = await cur.fetchone()
                    if _is_run_live(live_row):
                        return None
                    if check_run_state_in_lock == "completed_idle":
                        if live_row is None or live_row.get("status") != "completed":
                            return None
                        if thread_id_val and await _has_pending_background_work_on_cur(
                            cur,
                            execution_id,
                            thread_id=thread_id_val,
                            excluding_run_id=prior_run_id,
                        ):
                            return None
                    elif check_run_state_in_lock == "timeout":
                        if live_row is not None and live_row.get("status") == "completed":
                            if not thread_id_val or not await _has_pending_background_work_on_cur(
                                cur,
                                execution_id,
                                thread_id=thread_id_val,
                                excluding_run_id=prior_run_id,
                            ):
                                return None

                # DALTON-HOOK(automation-continuation-identity): explicit uuid casts for prior/CAS queries
                await cur.execute(f"""
                    SELECT p.failure_reason, p.delivery_result
                    FROM automation_executions p
                    WHERE p.automation_id = %s::uuid AND p.automation_execution_id <> %s::uuid
                      AND p.status NOT IN {UNSETTLED_STATUSES}
                      AND {not_a_server_skip("p")}
                    ORDER BY p.created_at DESC, p.automation_execution_id DESC
                    LIMIT 1
                """, (automation_id, execution_id))
                previous = await cur.fetchone()

                if expected_run_id is not UNSET_RUN_ID:
                    where_clause = (
                        "automation_execution_id = %s::uuid "
                        "AND conversation_response_id IS NOT DISTINCT FROM %s::uuid"
                    )
                    where_params = [execution_id, exp_run]
                else:
                    where_clause = "automation_execution_id = %s::uuid"
                    where_params = [execution_id]
                query, update_params = _execution_update(
                    to, where_clause, where_params, fields
                )
                await cur.execute(query, update_params)
                updated_row = await cur.fetchone()
                if updated_row is None:
                    return None
                row = dict(updated_row)

                if strike in ("count", "fuse"):
                    await _count_strike(cur, automation_id, fuse=strike == "fuse")
                elif strike == "reset":
                    # DALTON-HOOK(company-automations): success keeps disable_reason
                    # A success keeps disable_reason. Only an explicit resume
                    # or enable clears it. A disabled automation also keeps its
                    # count: a firing already running when another one disabled
                    # it does not explain the disable away.
                    await cur.execute("""
                        UPDATE automations
                        SET failure_count = CASE WHEN status = 'disabled'
                                                 THEN failure_count ELSE 0 END
                        WHERE automation_id = %s
                    """, (automation_id,))
                # After the strike, which may have disabled the automation.
                if schedule is not None:
                    await cur.execute(
                        _SCHEDULE_SQL[schedule], (automation_id, prior["created_at"])
                    )
                settled = {
                    **row,
                    "settled_from": prior["status"],
                    "delivery": prior["delivery"],
                    "coverage_unit_id": prior.get("coverage_unit_id"),
                    "coverage_unit_version": prior.get("coverage_unit_version"),
                    "previous_failure_reason": (
                        previous["failure_reason"] if previous else None
                    ),
                    "previous_delivery_result": (
                        previous["delivery_result"] if previous else None
                    ),
                }
            if in_transaction is not None:
                await in_transaction(conn, settled)
            return settled


async def stamp_delivery(execution_id: str, stamp: Optional[Dict[str, Any]]) -> bool:
    """Keep how the firing's start left its delivery in its ``delivery``
    column, or clear it with None; False once the firing settled.

    Its turn's run row carries the same stamp only from START, and a process
    that dies before then leaves the sweep this row alone to settle by: the
    settle reads the column under its lock (``settle_execution``). Only an
    unsettled firing is written, so a settled one keeps the start its settle
    went by.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if stamp is not None:
                # DALTON-HOOK(automation-subscription-fallback): preserve fallback marker across delivery re-stamps
                await cur.execute(f"""
                    UPDATE automation_executions
                    SET delivery = CASE
                        WHEN delivery ? 'subscription_fallback' THEN
                            %s::jsonb || jsonb_build_object('subscription_fallback', delivery->'subscription_fallback')
                        ELSE %s::jsonb
                    END
                    WHERE automation_execution_id = %s
                      AND status IN {UNSETTLED_STATUSES}
                """, (Json(stamp), Json(stamp), execution_id))
            else:
                # DALTON-HOOK(automation-subscription-fallback): keep fallback marker even when clearing delivery targets
                await cur.execute(f"""
                    UPDATE automation_executions
                    SET delivery = CASE
                        WHEN delivery ? 'subscription_fallback' THEN
                            jsonb_build_object('subscription_fallback', delivery->'subscription_fallback')
                        ELSE NULL
                    END
                    WHERE automation_execution_id = %s
                      AND status IN {UNSETTLED_STATUSES}
                """, (execution_id,))
            return cur.rowcount > 0


async def record_delivery(execution_id: str, delivery_result: list, *, conn=None) -> None:
    """Record what the webhook delivery returned, whatever the status.
    ``conn`` joins the caller's transaction."""
    async with get_db_connection(conn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE automation_executions SET delivery_result = %s
                WHERE automation_execution_id = %s
            """, (Json(delivery_result), execution_id))


# DALTON-HOOK(execution-delivery-status): clear retrying flag on saved delivery_result without replacing the record
async def clear_delivery_retrying(execution_id: str, *, conn=None) -> None:
    """Set ``retrying`` to ``false`` on saved ``delivery_result`` entries once retries stop."""
    async with get_db_connection(conn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET delivery_result = (
                    SELECT jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(elem) = 'object'
                                 AND elem -> 'retrying' = 'true'::jsonb
                            THEN jsonb_set(elem, '{retrying}', 'false'::jsonb, true)
                            ELSE elem
                        END
                        ORDER BY ord
                    )
                    FROM jsonb_array_elements(delivery_result) WITH ORDINALITY AS t(elem, ord)
                )
                WHERE automation_execution_id = %s
                  AND delivery_result IS NOT NULL
                  AND jsonb_typeof(delivery_result) = 'array'
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(delivery_result) AS e
                      WHERE jsonb_typeof(e) = 'object'
                        AND e -> 'retrying' = 'true'::jsonb
                  )
                """,
                (execution_id,),
            )


async def dismiss_execution(execution_id: str, *, automation_id: str) -> bool:
    """Mark a failed run of this automation dismissed. False when it has no
    such failed run. A second dismissal keeps the first one's time."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE automation_executions
                SET dismissed_at = COALESCE(dismissed_at, NOW())
                WHERE automation_execution_id = %s AND automation_id = %s
                  AND status IN ('failed', 'timeout')
            """, (execution_id, automation_id))
            return cur.rowcount > 0


async def list_executions(
    user_id: str,
    *,
    automation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    status: Optional[str] = None,
    wait_reason: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    scope: Optional[str] = None,
    computer_id: Optional[str] = None,  # DALTON-HOOK(history-computer-scope)
) -> tuple[List[Dict[str, Any]], bool]:
    """A page of a user's executions newest first, narrowed to one
    automation, thread, status or wait reason when given, and whether more
    follow it.

    Every row is scoped by its automation's owner, which is the ownership
    check: another user's automation lists nothing. ``workspace_id`` prefers
    the run thread's workspace, where the run happened, over the one the
    automation names now, which a move may have changed since. One row past
    the page answers whether another follows, where a count would read every
    run the user has.

    ``scope`` and ``computer_id`` are optional. Omitted, the query is the
    caller's own executions with no extra predicate. Company runs are not
    selected here.

    A computer token matches executions whose automation workspace OR owned
    thread workspace belongs to that computer, outside Home (a run with no
    thread, a deleted thread, or an inaccessible thread belongs by its
    automation workspace). To avoid a cross-table ``OR`` scanning the caller's
    full history on other computers, the computer-scoped query narrows to the
    target computer's caller-owned workspaces first, fetches the top window
    from the task side and thread side separately, deduplicates with ``UNION``,
    and paginates the merged result.
    """
    # DALTON-HOOK(company-automations): optional scope and computer reach on the owner feed
    task_where_parts = ["a.user_id = %s"]
    task_params: list = [user_id]
    exec_where_parts: list[str] = []
    exec_params: list = []
    for column, value in (
        ("e.automation_id", automation_id),
        ("e.conversation_thread_id", thread_id),
        ("e.status", status),
        # A wait from before reasons were kept has none, and was for a thread.
        ("COALESCE(e.wait_reason, CASE WHEN e.status = 'waiting'"
         " THEN 'thread_busy' END)", wait_reason),
    ):
        if value:
            exec_where_parts.append(f"{column} = %s")
            exec_params.append(value)
    if scope is not None:
        task_where_parts.append("a.scope = %s")
        task_params.append(scope)
    where_parts = [*task_where_parts, *exec_where_parts]
    params = [*task_params, *exec_params]
    where_clause = " AND ".join(where_parts)
    task_where_clause = " AND ".join(task_where_parts)
    exec_where_and = (
        " AND " + " AND ".join(exec_where_parts) if exec_where_parts else ""
    )
    # DALTON-HOOK(history-computer-scope): verify thread workspace ownership on join and split computer-scoped branches
    thread_join = owned_execution_thread_join_sql("e", "a", "t")

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if computer_id is None:
                await cur.execute(f"""
                    SELECT
                        {EXECUTION_COLUMNS},
                        a.name AS automation_name, a.trigger_type,
                        COALESCE(t.workspace_id, a.workspace_id) AS workspace_id
                    FROM automation_executions e
                    JOIN automations a ON a.automation_id = e.automation_id
                    {thread_join}
                    WHERE {where_clause}
                    ORDER BY e.created_at DESC, e.automation_execution_id DESC
                    LIMIT %s OFFSET %s
                """, (*params, limit + 1, offset))
            else:
                home_id = get_home_workspace_id(user_id)
                branch_limit = max(offset + limit + 1, 1)
                task_ws_pred = target_computer_workspace_predicate_sql(
                    "task_ws", "%s"
                )
                thread_ws_pred = target_computer_workspace_predicate_sql(
                    "thread_ws", "%s"
                )
                await cur.execute(f"""
                    WITH matched_execution_ids AS (
                        SELECT task_side.automation_execution_id, task_side.created_at
                        FROM (
                            SELECT e.automation_execution_id, e.created_at
                            FROM workspaces task_ws
                            JOIN automations a
                              ON a.workspace_id = task_ws.workspace_id
                            JOIN automation_executions e
                              ON e.automation_id = a.automation_id
                            WHERE {task_ws_pred}
                              AND a.workspace_id IS NOT NULL
                              AND {where_clause}
                            ORDER BY e.created_at DESC, e.automation_execution_id DESC
                            LIMIT %s
                        ) task_side
                        UNION
                        SELECT thread_side.automation_execution_id, thread_side.created_at
                        FROM (
                            SELECT e.automation_execution_id, e.created_at
                            FROM workspaces thread_ws
                            JOIN conversation_threads t
                              ON t.workspace_id = thread_ws.workspace_id
                            JOIN automation_executions e
                              ON e.conversation_thread_id = t.conversation_thread_id
                            WHERE {thread_ws_pred}
                              AND e.conversation_thread_id IS NOT NULL{exec_where_and}
                              AND EXISTS (
                                  SELECT 1 FROM automations a
                                  WHERE a.automation_id = e.automation_id
                                    AND {task_where_clause}
                              )
                            ORDER BY e.created_at DESC, e.automation_execution_id DESC
                            LIMIT %s
                        ) thread_side
                    ),
                    paged_execution_ids AS (
                        SELECT automation_execution_id
                        FROM matched_execution_ids
                        ORDER BY created_at DESC, automation_execution_id DESC
                        LIMIT %s OFFSET %s
                    )
                    SELECT
                        {EXECUTION_COLUMNS},
                        a.name AS automation_name, a.trigger_type,
                        COALESCE(t.workspace_id, a.workspace_id) AS workspace_id
                    FROM paged_execution_ids p
                    JOIN automation_executions e
                      ON e.automation_execution_id = p.automation_execution_id
                    JOIN automations a
                      ON a.automation_id = e.automation_id
                    {thread_join}
                    ORDER BY e.created_at DESC, e.automation_execution_id DESC
                """, (
                    home_id,
                    computer_id,
                    user_id,
                    *params,
                    branch_limit,
                    home_id,
                    computer_id,
                    user_id,
                    *exec_params,
                    *task_params,
                    branch_limit,
                    limit + 1,
                    offset,
                ))

            rows = [dict(row) for row in await cur.fetchall()]
            return rows[:limit], len(rows) > limit


async def list_settled_since(
    user_id: str, since: datetime, *, limit: int = 10
) -> List[Dict[str, Any]]:
    """A user's runs that finished or failed at or after ``since``, newest first."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {EXECUTION_COLUMNS}, a.name AS automation_name
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE a.user_id = %s AND e.completed_at >= %s
                  AND e.status IN ('completed', 'failed', 'timeout')
                ORDER BY e.completed_at DESC, e.automation_execution_id DESC
                LIMIT %s
            """, (user_id, since, limit))
            return [dict(row) for row in await cur.fetchall()]


async def count_executions(automation_id: str) -> int:
    """How many runs an automation has: one index range, unlike a count
    across every automation of a user."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT COUNT(*) FROM automation_executions
                WHERE automation_id = %s
            """, (automation_id,))
            return (await cur.fetchone())[0]


# =============================================================================
# Waiting for a busy thread
# =============================================================================
#
# An execution whose thread already has a turn running waits for it to end,
# rather than steering into it.


async def has_earlier_waiting_execution(
    automation_id: str, execution_id: str
) -> bool:
    """Whether a firing of this automation that came before this one is
    waiting for the thread too.

    Strictly earlier, by creation then id, so of two firings that start
    waiting together exactly one gives way. A firing parked for its computer
    is not in this line: this one takes its wait over (``_SUPERSEDED``), so
    giving way to it would skip both.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"""
                SELECT 1
                FROM automation_executions w
                JOIN automation_executions me
                  ON me.automation_execution_id = %s
                WHERE w.automation_id = %s AND w.status = 'waiting'
                  AND NOT {_PARKED.format(e="w")}
                  AND (w.created_at, w.automation_execution_id)
                      < (me.created_at, me.automation_execution_id)
                LIMIT 1
            """, (execution_id, automation_id))
            return await cur.fetchone() is not None


async def get_execution_status(
    execution_id: str, *, automation_id: Optional[str] = None
) -> Optional[str]:
    """The execution's status; None when there is none, or none of
    ``automation_id``'s when that is given."""
    query = """
        SELECT status FROM automation_executions
        WHERE automation_execution_id = %s
    """
    params: tuple = (execution_id,)
    if automation_id is not None:
        query += " AND automation_id = %s"
        params += (automation_id,)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()
            return row["status"] if row else None


# =============================================================================
# Firings whose process died
# =============================================================================
#
# The process holding a firing touches ``heartbeat_at`` while the firing is
# pending, waiting or running. The row, not a process id, says whether anyone
# still holds it: every restart gets a fresh process, so an id recorded by the
# old one is never asked about again.


async def touch_execution(execution_id: str) -> None:
    """Beat a firing's heartbeat, never a parked one's: a touch already queued
    behind the park would otherwise commit after it and undo the heartbeat
    the park dated to its deadline."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"""
                UPDATE automation_executions SET heartbeat_at = NOW()
                WHERE automation_execution_id = %s
                  AND status IN {UNSETTLED_STATUSES}
                  AND NOT {_PARKED.format(e="automation_executions")}
            """, (execution_id,))


async def settle_legacy_executions(error_message: str) -> int:
    """Close firings a build before the heartbeat left unsettled; how many.

    A NULL heartbeat is a row that predates the column. A process on the
    previous build may still be running it through a deploy, so it gets a
    day. After that nothing is waiting on it, and it is closed as a row write
    alone: a failure notice weeks late, or reviving whatever it left on its
    automation, would do more harm than the stale row. None of these rows is
    ``waiting``: that status came in with the heartbeat, and every firing
    that can wait was written with one.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"""
                UPDATE automation_executions
                SET status = 'failed', failure_reason = 'interrupted',
                    error_message = %s, completed_at = NOW()
                WHERE status IN {UNSETTLED_STATUSES}
                  AND heartbeat_at IS NULL
                  AND created_at < NOW() - INTERVAL '1 day'
            """, (error_message,))
            return cur.rowcount


async def list_abandoned_executions(
    quiet_seconds: int, limit: int = 50
) -> List[Dict[str, Any]]:
    """Firings whose heartbeat has been quiet ``quiet_seconds``, or past their
    hard timeout cap with no live turn running, oldest first, with what
    settling each needs: its owner, thread, workspace and run.

    A firing whose run is still ``in_progress``, owed a restart's resume, or
    waiting on unfinished background work / continuation turns (within its
    hard timeout cap) is left out.
    """
    # DALTON-HOOK(automation-continuation-identity): compute per-automation timeouts in Python once and bind into SQL
    pending_sql = _pending_background_work_sql(
        execution_id_sql="e.automation_execution_id",
        thread_id_sql="e.conversation_thread_id",
        linked_run_id_sql="e.conversation_response_id",
    )
    default_timeout = default_execution_timeout_seconds()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT DISTINCT a.automation_id, a.metadata, a.trigger_config
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE e.status = 'running'
                  AND (a.metadata ? 'timeout_seconds'
                       OR a.trigger_config ? 'timeout_seconds')
            """)
            override_ids: list[str] = []
            override_secs: list[float] = []
            for ov in await cur.fetchall():
                override_ids.append(str(ov["automation_id"]))
                override_secs.append(execution_timeout_seconds(ov))

            # DALTON-HOOK(automation-continuation-identity): bind Python-parsed timeouts and live-turn exclusion
            # DALTON-HOOK(automation-subscription-fallback): fetch e.delivery to avoid extra notice queries on settle
            await cur.execute(f"""
                WITH ov AS (
                    SELECT * FROM unnest(%s::uuid[], %s::double precision[])
                        AS t(automation_id, timeout_secs)
                )
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.conversation_response_id,
                       e.started_at, e.created_at, e.delivery,
                       a.user_id, t.workspace_id
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                LEFT JOIN ov ON ov.automation_id = a.automation_id
                LEFT JOIN conversation_threads t
                    ON t.conversation_thread_id = e.conversation_thread_id
                WHERE e.status IN {UNSETTLED_STATUSES}
                  AND NOT {_PARKED.format(e="e")}
                  AND NOT EXISTS (
                      SELECT 1 FROM conversation_responses r
                      WHERE (
                          r.conversation_response_id = e.conversation_response_id
                          AND (r.status = 'in_progress'
                               OR (r.status = 'cancelled' AND r.metadata ? %s::text
                                   AND r.cancel_requested_at > NOW() - %s::interval))
                      ) OR (
                          e.conversation_thread_id IS NOT NULL
                          AND r.conversation_thread_id = e.conversation_thread_id
                          AND r.status = 'in_progress'
                          AND r.metadata->>'automation_execution_id' = e.automation_execution_id::text
                      )
                  )
                  AND (
                      (e.status = 'running'
                       AND COALESCE(e.started_at, e.created_at)
                           < NOW() - make_interval(
                               secs => COALESCE(ov.timeout_secs, %s::double precision)
                           ))
                      OR (
                          e.heartbeat_at < NOW() - make_interval(secs => %s::double precision)
                          AND (
                              e.conversation_thread_id IS NULL
                              OR NOT {pending_sql}
                          )
                      )
                  )
                ORDER BY e.heartbeat_at
                LIMIT %s::integer
            """, (
                override_ids,
                override_secs,
                RESTART_INTERRUPTED_META,
                RESTART_RESUME_TTL,
                default_timeout,
                quiet_seconds,
                limit,
            ))
            return [dict(row) for row in await cur.fetchall()]


# --- DALTON-HOOK(automation-continuation-identity): execution background work & identity queries ---
async def get_execution_for_settlement(execution_id: str) -> Optional[Dict[str, Any]]:
    """Columns of ``execution_id`` consulted before settling a finished run."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(automation-subscription-fallback): select delivery for execution fallback marker check
            await cur.execute("""
                SELECT automation_execution_id, automation_id, status,
                       conversation_thread_id, conversation_response_id,
                       started_at, created_at, heartbeat_at, delivery
                FROM automation_executions
                WHERE automation_execution_id = %s::uuid
            """, (execution_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_running_executions_for_thread(thread_id: str) -> List[Dict[str, Any]]:
    """All ``running`` automation executions pinned to ``thread_id``, oldest first."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(automation-subscription-fallback): select delivery for execution fallback marker check
            # DALTON-HOOK(cos-settings-tools): select execution-level model config snapshot for continuation turns
            await cur.execute("""
                SELECT e.automation_execution_id, e.automation_id, e.status,
                       e.conversation_thread_id, e.conversation_response_id,
                       e.started_at, e.created_at, e.delivery,
                       e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken,
                       a.user_id, t.workspace_id
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                LEFT JOIN conversation_threads t
                    ON t.conversation_thread_id = e.conversation_thread_id
                WHERE e.conversation_thread_id = %s::uuid
                  AND e.status = 'running'
                ORDER BY e.created_at ASC, e.automation_execution_id ASC
            """, (thread_id,))
            return [dict(row) for row in await cur.fetchall()]


# DALTON-HOOK(cos-settings-tools): lazily snapshot rolling-upgrade execution rows (snapshot_taken=FALSE) under a row lock
async def ensure_execution_snapshot(
    execution_id: str,
    *,
    conn: Any = None,
) -> Optional[Dict[str, Any]]:
    """Ensure ``automation_executions.snapshot_taken`` is TRUE for ``execution_id``.

    During a rolling upgrade window after migration ``d050`` commits and before
    old backend instances finish draining, an old backend can insert rows whose
    ``snapshot_taken`` defaults to ``FALSE``. On first read by the new backend,
    this locks the execution row ``FOR UPDATE OF e`` in one transaction, copies
    ``(llm_model, reasoning_effort, llm_model_system_only)`` from ``automations``,
    and flips ``snapshot_taken = TRUE``; subsequent reads return the frozen
    snapshot unchanged.
    """
    sql = """
        WITH locked AS (
            SELECT e.automation_execution_id,
                   e.snapshot_taken,
                   e.llm_model AS cur_llm_model,
                   e.reasoning_effort AS cur_reasoning_effort,
                   e.llm_model_system_only AS cur_system_only,
                   a.llm_model AS auto_llm_model,
                   a.reasoning_effort AS auto_reasoning_effort,
                   COALESCE((a.metadata->>'llm_model_system_only')::boolean, FALSE) AS auto_system_only
            FROM automation_executions e
            JOIN automations a ON a.automation_id = e.automation_id
            WHERE e.automation_execution_id = %s::uuid
            FOR UPDATE OF e
        ),
        updated AS (
            UPDATE automation_executions e
            SET llm_model = l.auto_llm_model,
                reasoning_effort = l.auto_reasoning_effort,
                llm_model_system_only = l.auto_system_only,
                snapshot_taken = TRUE
            FROM locked l
            WHERE e.automation_execution_id = l.automation_execution_id
              AND NOT l.snapshot_taken
            RETURNING e.llm_model, e.reasoning_effort, e.llm_model_system_only, e.snapshot_taken
        )
        SELECT llm_model, reasoning_effort, llm_model_system_only, snapshot_taken
        FROM updated
        UNION ALL
        SELECT cur_llm_model, cur_reasoning_effort, cur_system_only, snapshot_taken
        FROM locked
        WHERE snapshot_taken
    """
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (execution_id,))
            row = await cur.fetchone()
            return dict(row) if row else None
    async with get_db_connection() as own_conn:
        async with own_conn.transaction():
            async with own_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (execution_id,))
                row = await cur.fetchone()
                return dict(row) if row else None


async def has_pending_background_work(
    execution_id: str,
    *,
    thread_id: Optional[str],
    excluding_run_id: Optional[str] = None,
) -> bool:
    """Whether ``execution_id`` on ``thread_id`` still has unfinished background
    commands, subagent tasks, unclosed report-back jobs, or another live turn."""
    if not thread_id:
        return False
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            return await _has_pending_background_work_on_cur(
                cur,
                execution_id,
                thread_id=thread_id,
                excluding_run_id=excluding_run_id,
            )


async def get_open_execution_for_command(
    thread_id: str, command_id: str
) -> Optional[Dict[str, str]]:
    """Open automation firing that launched ``command_id`` on ``thread_id``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT e.automation_execution_id, e.automation_id
                FROM command_runs cr
                JOIN conversation_responses pr
                  ON pr.conversation_response_id = cr.run_id
                JOIN automation_executions e
                  ON e.automation_execution_id::text = pr.metadata->>'automation_execution_id'
                WHERE cr.command_id = %s::text
                  AND cr.conversation_thread_id = %s::uuid
                  AND e.conversation_thread_id = %s::uuid
                  AND e.status = 'running'
                LIMIT 1
            """, (command_id, thread_id, thread_id))
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "automation_execution_id": str(row["automation_execution_id"]),
                "automation_id": str(row["automation_id"]),
            }


async def get_open_execution_for_task(
    thread_id: str, task_run_id: str
) -> Optional[Dict[str, str]]:
    """Open automation firing that launched the exact subagent run ``task_run_id``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT e.automation_execution_id, e.automation_id
                FROM subagent_runs sr
                JOIN conversation_responses pr
                  ON pr.conversation_response_id = sr.parent_run_id
                JOIN automation_executions e
                  ON e.automation_execution_id::text = pr.metadata->>'automation_execution_id'
                WHERE sr.thread_id = %s::uuid
                  AND sr.task_run_id = %s::uuid
                  AND e.conversation_thread_id = %s::uuid
                  AND e.status = 'running'
                LIMIT 1
            """, (thread_id, task_run_id, thread_id))
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "automation_execution_id": str(row["automation_execution_id"]),
                "automation_id": str(row["automation_id"]),
            }


async def take_over_execution_run(
    execution_id: str, run_id: str, *, conn: Any = None
) -> bool:
    """Advance a running execution's ``conversation_response_id`` to ``run_id``.

    Acquires ``FOR UPDATE`` on the execution row first (on the caller's START
    transaction connection when supplied) so settlement cannot interleave with
    the START transaction, then updates only while ``execution_id`` is
    ``running`` and ``run_id`` is an ``in_progress`` turn on that execution's
    thread with non-older ``run_seq``.
    """
    @asynccontextmanager
    async def _tx():
        if conn is not None:
            yield conn
            return
        async with get_db_connection() as pooled:
            async with pooled.transaction():
                yield pooled

    async with _tx() as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute("""
                SELECT 1
                FROM automation_executions
                WHERE automation_execution_id = %(execution_id)s::uuid
                FOR UPDATE
            """, {"execution_id": execution_id})
            await cur.execute("""
                UPDATE automation_executions e
                SET conversation_response_id = %(run_id)s::uuid,
                    heartbeat_at = NOW()
                WHERE e.automation_execution_id = %(execution_id)s::uuid
                  AND e.status = 'running'
                  AND EXISTS (
                      SELECT 1 FROM conversation_responses r
                      WHERE r.conversation_response_id = %(run_id)s::uuid
                        AND r.conversation_thread_id = e.conversation_thread_id
                        AND r.status = 'in_progress'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM conversation_responses cur
                      JOIN conversation_responses cand
                        ON cand.conversation_response_id = %(run_id)s::uuid
                      WHERE cur.conversation_response_id = e.conversation_response_id
                        AND cur.conversation_thread_id = e.conversation_thread_id
                        AND cur.conversation_response_id <> cand.conversation_response_id
                        AND cur.run_seq > cand.run_seq
                  )
            """, {"execution_id": execution_id, "run_id": run_id})
            return cur.rowcount == 1
# --- DALTON-HOOK(automation-continuation-identity): end ---


# =============================================================================
# Waiting for a computer
# =============================================================================
#
# A firing whose computer cannot be reached is parked as ``waiting`` naming
# that computer (``awaits_computer``), and no process holds it: the
# scheduler's sweep starts it again once the computer's tunnel is live, or
# gives it up. Its heartbeat is quiet meanwhile, so the abandoned-firing sweep
# passes it over.

# Never NULL, so ``NOT`` of it holds for a row with no ``wait_reason``.
_PARKED = (
    "({e}.status = 'waiting'"
    " AND {e}.wait_reason IS NOT DISTINCT FROM 'computer_unreachable')"
)

# A firing of the same automation came after ``e``, whatever became of it: it
# takes the wait over, so a schedule's missed runs come back as one run.
_SUPERSEDED = """EXISTS (
    SELECT 1 FROM automation_executions w
    WHERE w.automation_id = e.automation_id
      AND (w.created_at, w.automation_execution_id)
          > (e.created_at, e.automation_execution_id))"""

# Its computer is back, or its automation's workspace is on another one now.
_REACHABLE = awaited_computer_back_sql(
    "e.awaits_computer::text",
    "(SELECT m.workspace_id FROM automations m WHERE m.automation_id = e.automation_id)",
)
# A firing of the user's Home, which a turn in Home's stand-in can run now
# though the machine Home lives on is still away.
_STAND_IN = f"""EXISTS (
    SELECT 1 FROM automations m
    JOIN workspaces ws ON ws.workspace_id = m.workspace_id
    WHERE m.automation_id = e.automation_id
      AND {home_stand_in_could_run_sql("ws")})"""
_EXPIRED = "e.awaits_until < NOW()"


async def park_execution(
    execution_id: str,
    *,
    awaits_computer: str,
    parked_status: str,
    wait_seconds: int,
) -> bool:
    """Park a pending or running firing as ``waiting`` for ``awaits_computer``;
    False when it had moved on.

    The wait's deadline is set by the first park and kept by every later
    one, so a machine that comes back only to drop again does not start the
    wait over. The heartbeat is dated to that deadline: nothing touches the
    row while it waits, and a build that predates parking (the other color
    through a cutover) must not take the quiet heartbeat for a dead process.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE automation_executions
                SET status = 'waiting', started_at = NOW(),
                    wait_reason = 'computer_unreachable',
                    awaits_computer = %(computer)s, parked_status = %(parked_status)s,
                    awaits_until = COALESCE(
                        awaits_until, NOW() + make_interval(secs => %(wait_s)s)),
                    heartbeat_at = COALESCE(
                        awaits_until, NOW() + make_interval(secs => %(wait_s)s))
                WHERE automation_execution_id = %(execution_id)s
                  AND status IN ('pending', 'running')
            """, {
                "execution_id": execution_id, "computer": awaits_computer,
                "parked_status": parked_status, "wait_s": wait_seconds,
            })
            return cur.rowcount == 1


async def list_parked_executions(limit: int = 50) -> List[Dict[str, Any]]:
    """Firings parked for a computer that the sweep has something to do with,
    soonest deadline first, with their owner, the automation's status when
    each parked, and the sweep's verdict on each: ``superseded`` by a later firing,
    ``reachable`` (its computer's tunnel is live, it names none, or its
    workspace is on another computer now), ``stand_in`` (a firing of Home,
    which Home's stand-in can run now) or ``expired`` (past its
    ``awaits_until``).

    Rows with none of those are left out before the limit: a page of
    machines asleep for the night must not starve one that is back.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT e.automation_execution_id, e.automation_id, e.parked_status,
                       a.user_id,
                       {_SUPERSEDED} AS superseded,
                       {_REACHABLE} AS reachable,
                       {_STAND_IN} AS stand_in,
                       {_EXPIRED} AS expired
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE {_PARKED.format(e="e")}
                  -- DALTON-HOOK(company-automations): skip parked firings of soft-deleted parents
                  AND a.deleted_at IS NULL
                  AND ({_SUPERSEDED} OR {_REACHABLE} OR {_STAND_IN} OR {_EXPIRED})
                ORDER BY e.awaits_until NULLS LAST
                LIMIT %(limit)s
            """, {"limit": limit})
            return [dict(row) for row in await cur.fetchall()]


async def release_parked_execution(execution_id: str) -> bool:
    """Put a parked firing back in line as ``pending`` for the ordinary start,
    once its computer is reachable, its workspace is on another one now, or
    it is Home's and Home's stand-in can run it; False when another worker
    took it, it moved on, a later firing took its wait over, its deadline
    passed since the sweep read it (the next sweep skips it), or none of
    those holds any more.

    One guarded statement, so of the workers whose sweeps reach the row
    exactly one starts it. The heartbeat starts again with it, and the thread
    is let go: the run takes the automation's thread as it stands after the
    wait, as it would had it waited in process.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"""
                UPDATE automation_executions e
                SET status = 'pending', heartbeat_at = NOW(),
                    conversation_thread_id = NULL
                WHERE e.automation_execution_id = %(execution_id)s
                  -- DALTON-HOOK(company-automations): refuse release if parent was soft-deleted
                  AND EXISTS (
                      SELECT 1 FROM automations a
                      WHERE a.automation_id = e.automation_id
                        AND a.deleted_at IS NULL
                  )
                  AND {_PARKED.format(e="e")}
                  AND NOT {_SUPERSEDED}
                  AND NOT COALESCE({_EXPIRED}, false)
                  AND ({_REACHABLE} OR {_STAND_IN})
            """, {"execution_id": execution_id})
            return cur.rowcount == 1


async def hand_off_execution(
    execution_id: str, *, from_statuses: Sequence[str], thread_id: str, job: Any
) -> bool:
    """Put a live firing back to ``pending`` on ``thread_id`` and write
    ``job``, which the next process starts it from, in one transaction;
    False when it was settled or moved first. The heartbeat is touched, so the
    sweep gives the next process its full margin."""
    from src.server.database.runs.outbox import enqueue_hooks

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE automation_executions
                    SET status = 'pending', heartbeat_at = NOW(),
                        conversation_thread_id = %s
                    WHERE automation_execution_id = %s AND status = ANY(%s)
                """, (thread_id, execution_id, list(from_statuses)))
                if cur.rowcount != 1:
                    return False
            await enqueue_hooks(conn, thread_id=thread_id, jobs=[job])
    return True


async def get_running_execution_for_run(run_id: str) -> Optional[Dict[str, Any]]:
    """The running firing whose linked run is ``run_id``, with its owner,
    thread and workspace; None when there is none."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(automation-subscription-fallback): select e.delivery for execution fallback marker check
            await cur.execute("""
                SELECT e.automation_execution_id, e.automation_id,
                       e.conversation_thread_id, e.delivery, a.user_id, t.workspace_id
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                LEFT JOIN conversation_threads t
                    ON t.conversation_thread_id = e.conversation_thread_id
                WHERE e.conversation_response_id = %s AND e.status = 'running'
            """, (run_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_settling_run(run_id: str) -> Optional[Dict[str, Any]]:
    """The columns of a run's ledger row that settling its firing reads."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT conversation_response_id, status, interrupt_reason,
                       metadata, errors, cancel_requested_at
                FROM conversation_responses
                WHERE conversation_response_id = %s
            """, (run_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


async def skip_unstarted_executions_of_deleted_parents() -> int:
    """Skip pending and waiting firings whose parent is still soft-deleted.

    DALTON-HOOK(company-automations): one sweep UPDATE skips those firings.
    The sweep calls this once. The recycle module reads ``deleted_at`` in
    that same UPDATE.
    """
    from src.dalton.database.automation_recycle import (
        skip_unstarted_executions_of_deleted_parents as skip_deleted,
    )

    return await skip_deleted()


_TRIGGERABLE_STATUSES = ("active", "paused", "completed")


class _WorkspaceDriftRetry(Exception):
    """Internal retry signal when automation.workspace_id drifted before FOR UPDATE."""


async def create_execution_for_live_parent(
    automation_id: str,
    user_id: str,
    scheduled_at: datetime,
    server_id: str,
    *,
    checked_computer_id: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """Lock the parent, refuse a deleted or non-triggerable row, then insert.

    The lock and the insert share one transaction, so a delete that commits
    first leaves no pending execution. Raises ``ValueError`` with the same
    texts ``trigger_automation`` used when the read and the insert were separate.
    """
    # DALTON-HOOK(coverage-units): enforce unit FOR SHARE lock & lead check on immediate trigger
    from uuid import UUID as _ExecUUID
    from fastapi import HTTPException
    from src.dalton.autonomy.classification import (
        is_managed_unit_automation,
        is_managed_unit_automation_for_unit,
    )
    from src.dalton.database.coverage_units import (
        get_active_unit_by_workspace,
        get_coverage_unit,
    )
    from src.dalton.settings import is_coverage_units_enabled

    units_on = is_coverage_units_enabled()
    execution_id = str(uuid4())
    async with get_db_connection() as conn:
        for _attempt in range(3):
            try:
                async with conn.transaction():
                    unit_row: dict[str, Any] | None = None
                    meta_unit_row: dict[str, Any] | None = None
                    probe_ws_id = ""
                    probe_meta_unit_id = ""
                    async with conn.cursor(row_factory=dict_row) as cur:
                        if units_on:
                            await cur.execute(
                                """
                                SELECT workspace_id,
                                       CASE
                                           WHEN jsonb_typeof(metadata) = 'object'
                                            AND NULLIF(BTRIM(COALESCE(metadata->>'unit_id', '')), '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                                           THEN BTRIM(metadata->>'unit_id')
                                           ELSE NULL
                                       END AS meta_unit_id
                                FROM automations
                                WHERE automation_id = %s AND user_id = %s
                                """,
                                (automation_id, user_id),
                            )
                            ws_probe = await cur.fetchone()
                            probe_ws_id = (
                                str(ws_probe["workspace_id"])
                                if ws_probe and ws_probe.get("workspace_id") is not None
                                else ""
                            )
                            probe_meta_unit_id = (
                                str(ws_probe["meta_unit_id"])
                                if ws_probe and ws_probe.get("meta_unit_id") is not None
                                else ""
                            )
                            if probe_ws_id:
                                unit_row = await get_active_unit_by_workspace(
                                    probe_ws_id, conn=conn, for_share=True
                                )
                            if probe_meta_unit_id:
                                meta_unit_row = await get_coverage_unit(
                                    probe_meta_unit_id, conn=conn, for_share=True
                                )
                        await cur.execute(
                            f"""
                            SELECT {AUTOMATION_COLUMNS}
                            FROM automations
                            WHERE automation_id = %s AND user_id = %s
                            FOR UPDATE
                            """,
                            (automation_id, user_id),
                        )
                        parent = await cur.fetchone()
                        if parent is None or parent.get("deleted_at") is not None:
                            raise ValueError("Automation not found")
                        parent_row = dict(parent)
                        if parent_row.get("status") not in _TRIGGERABLE_STATUSES:
                            raise ValueError(
                                f"Cannot trigger automation in '{parent_row.get('status')}' status"
                            )
                        if units_on:
                            parent_ws = (
                                str(parent_row["workspace_id"])
                                if parent_row.get("workspace_id") is not None
                                else ""
                            )
                            parent_meta = parent_row.get("metadata")
                            parent_meta_unit_id = ""
                            if isinstance(parent_meta, dict) and parent_meta.get("unit_id"):
                                try:
                                    parent_meta_unit_id = str(
                                        _ExecUUID(str(parent_meta.get("unit_id")).strip())
                                    )
                                except (ValueError, TypeError, AttributeError):
                                    parent_meta_unit_id = ""
                            if parent_ws != probe_ws_id or parent_meta_unit_id != probe_meta_unit_id:
                                # Workspace or unit_id changed before automations FOR UPDATE; roll back and retry under unified FOR SHARE lock order.
                                raise _WorkspaceDriftRetry()
                            if (
                                parent_meta_unit_id
                                and is_managed_unit_automation(parent_row)
                            ):
                                if meta_unit_row is None:
                                    meta_unit_row = await get_coverage_unit(
                                        parent_meta_unit_id, conn=conn, for_share=True
                                    )
                                if meta_unit_row is not None:
                                    meta_status = str(meta_unit_row.get("status") or "").strip().lower()
                                    meta_ws = str(meta_unit_row.get("workspace_id") or "").strip().lower()
                                    meta_lead = str(
                                        meta_unit_row.get("lead_analyst_user_id")
                                        or meta_unit_row.get("owner_user_id")
                                        or ""
                                    ).strip()
                                    owner_uid = str(parent_row.get("user_id") or "").strip()
                                    if (
                                        meta_status != "active"
                                        or meta_ws != parent_ws.lower()
                                        or (meta_lead and owner_uid != meta_lead)
                                    ):
                                        raise HTTPException(
                                            status_code=403,
                                            detail="受管自动任务关联的覆盖单元已解绑、改绑或归档，拒绝触发。",
                                        )
                            if (
                                parent_ws
                                and unit_row is not None
                                and is_managed_unit_automation_for_unit(
                                    parent_row, unit=unit_row
                                )
                            ):
                                lead_uid = str(
                                    unit_row.get("lead_analyst_user_id")
                                    or unit_row.get("owner_user_id")
                                    or ""
                                ).strip()
                                owner_uid = str(parent_row.get("user_id") or "").strip()
                                if lead_uid and owner_uid != lead_uid:
                                    raise HTTPException(
                                        status_code=403,
                                        detail=(
                                            f"受管自动任务仅可由当前主分析师（{lead_uid}）运行，"
                                            "当前持有者已不再担任该单元主分析师。"
                                        ),
                                    )
                        # DALTON-HOOK(coverage-units): stamp actual write time so concurrent pause cutoffs never mis-skip live-parent inserts
                        # DALTON-HOOK(cos-settings-tools): freeze automation's model config snapshot onto automation_executions in the same transaction
                        exec_system_only = bool(
                            (parent_row.get("metadata") or {}).get("llm_model_system_only")
                        )
                        await cur.execute(
                            """
                            INSERT INTO automation_executions (
                                automation_execution_id, automation_id,
                                status, scheduled_at, server_id, created_at,
                                heartbeat_at, checked_computer_id,
                                llm_model, reasoning_effort, llm_model_system_only,
                                snapshot_taken
                            )
                            VALUES (%s, %s, 'pending', %s, %s, clock_timestamp(), NOW(), %s, %s, %s, %s, TRUE)
                            """,
                            (
                                execution_id,
                                automation_id,
                                scheduled_at,
                                server_id,
                                checked_computer_id,
                                parent_row.get("llm_model"),
                                parent_row.get("reasoning_effort"),
                                exec_system_only,
                            ),
                        )
                        return execution_id, parent_row
            except _WorkspaceDriftRetry:
                continue
        raise HTTPException(
            status_code=409,
            detail="Automation workspace changed concurrently; please retry trigger.",
        )


async def get_db_now(*, conn: Any = None) -> datetime:
    """Return current database timestamp (`clock_timestamp()`) inside caller's transaction."""
    async with get_db_connection(conn) as active_conn:
        async with active_conn.cursor() as cur:
            await cur.execute("SELECT clock_timestamp() AS db_now")
            row = await cur.fetchone()
    if isinstance(row, Mapping):
        for val in row.values():
            if isinstance(val, datetime):
                return val
    elif isinstance(row, (tuple, list)) and row and isinstance(row[0], datetime):
        return row[0]
    return datetime.now(timezone.utc)


async def skip_unstarted_executions_for_automations(
    automation_ids: list[str] | tuple[str, ...],
    *,
    reason: str = "unit_handoff",
    error_message: str = "Skipped unstarted managed unit execution due to coverage unit handoff.",
    created_at_lte: datetime | None = None,
    conn: Any = None,
) -> int:
    """Mark pending/waiting executions for ``automation_ids`` as ``skipped`` inside caller's transaction."""
    clean_ids = [str(aid).strip() for aid in automation_ids if str(aid or "").strip()]
    if not clean_ids:
        return 0
    clean_reason = str(reason or "unit_handoff").strip() or "unit_handoff"
    cutoff_clause = "\n                  AND created_at <= %s" if created_at_lte is not None else ""
    params: list[Any] = [clean_reason, error_message, clean_ids]
    if created_at_lte is not None:
        params.append(created_at_lte)

    async def _run(active_conn: Any) -> int:
        async with active_conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE automation_executions
                SET status = 'skipped',
                    skip_reason = COALESCE(NULLIF(skip_reason, ''), %s),
                    error_message = COALESCE(NULLIF(error_message, ''), %s),
                    completed_at = COALESCE(completed_at, NOW())
                WHERE automation_id = ANY(%s::uuid[])
                  AND status IN ('pending', 'waiting'){cutoff_clause}
                """,
                tuple(params),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as new_conn, new_conn.transaction():
        return await _run(new_conn)


async def create_execution(
    automation_id: str,
    scheduled_at: datetime,
    server_id: str,
    *,
    checked_computer_id: Optional[str] = None,
) -> str:
    """Create a new execution record (for manual triggers); its id.

    ``checked_computer_id`` is the computer a computer token's call was
    checked to run it on, which every start of the firing reads off the row.
    """
    execution_id = str(uuid4())
    # DALTON-HOOK(cos-settings-tools): freeze automation's model config snapshot onto automation_executions in the same transaction; raise if parent automation does not exist
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id,
                    status, scheduled_at, server_id, created_at,
                    heartbeat_at, checked_computer_id,
                    llm_model, reasoning_effort, llm_model_system_only,
                    snapshot_taken
                )
                SELECT %s, a.automation_id, 'pending', %s, %s, NOW(), NOW(), %s,
                       a.llm_model, a.reasoning_effort,
                       COALESCE((a.metadata->>'llm_model_system_only')::boolean, FALSE),
                       TRUE
                FROM automations a
                WHERE a.automation_id = %s
            """, (execution_id, scheduled_at, server_id, checked_computer_id, automation_id))
            if getattr(cur, "rowcount", None) == 0:
                raise ValueError(f"Automation not found: {automation_id}")
    return execution_id
