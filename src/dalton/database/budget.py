"""Raw psycopg3 SQL queries for Dalton local daily USD budget and reservations (P0.8, P0N.6, G1)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


def _conservative_pricing_floor_usd() -> Decimal:
    """Return the conservative USD floor retained when a cash call's pricing cannot be resolved."""
    try:
        floor = get_dalton_settings().budget.admission_floor_usd
        if floor is not None and Decimal(str(floor)) > Decimal("0"):
            return Decimal(str(floor)).quantize(Decimal("0.000001"))
    except Exception:
        pass
    return Decimal("0.250000")


async def acquire_budget_advisory_lock(conn: Any, user_id: str, budget_day: date) -> None:
    """Acquire transaction-scoped advisory lock for `(user_id, budget_day)`."""
    lock_key = f"dalton_budget:{user_id}:{budget_day.isoformat()}"
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))


async def fetch_budget_config(conn: Any, user_id: str) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT user_id, daily_cap_usd, default_task_cap_usd, timezone, updated_at
            FROM dalton_budget_config
            WHERE user_id = %s
            """,
            (user_id,),
        )
        return await cur.fetchone()


async def upsert_budget_config(
    user_id: str,
    daily_cap_usd: Decimal,
    default_task_cap_usd: Decimal | None,
    timezone_str: str,
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_budget_config (user_id, daily_cap_usd, default_task_cap_usd, timezone, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET daily_cap_usd = EXCLUDED.daily_cap_usd,
                    default_task_cap_usd = EXCLUDED.default_task_cap_usd,
                    timezone = EXCLUDED.timezone,
                    updated_at = NOW()
                RETURNING user_id, daily_cap_usd, default_task_cap_usd, timezone, updated_at
                """,
                (user_id, daily_cap_usd, default_task_cap_usd, timezone_str),
            )
            row = await cur.fetchone()
            assert row is not None
            return dict(row)


async def list_task_caps(conn: Any, user_id: str) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                tc.user_id,
                tc.scope_kind,
                tc.scope_id,
                tc.cap_usd,
                tc.updated_at,
                COALESCE(w.name, t.title, tc.scope_id::text) AS scope_name
            FROM dalton_budget_task_caps tc
            LEFT JOIN workspaces w
                ON tc.scope_kind = 'workspace' AND w.workspace_id = tc.scope_id
            LEFT JOIN conversation_threads t
                ON tc.scope_kind = 'thread' AND t.conversation_thread_id = tc.scope_id
            WHERE tc.user_id = %s
            ORDER BY tc.scope_kind, tc.updated_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def verify_task_cap_scope_owner(
    user_id: str,
    scope_kind: str,
    scope_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Verify that `scope_id` (workspace or thread) belongs to `user_id` (R2-8 / G2)."""
    async def _check(c: Any) -> bool:
        async with c.cursor() as cur:
            if scope_kind == "workspace":
                await cur.execute(
                    "SELECT 1 FROM workspaces WHERE workspace_id = %s AND user_id = %s",
                    (scope_id, user_id),
                )
            elif scope_kind == "thread":
                await cur.execute(
                    """
                    SELECT 1
                    FROM conversation_threads t
                    JOIN workspaces w ON w.workspace_id = t.workspace_id
                    WHERE t.conversation_thread_id = %s AND w.user_id = %s
                    """,
                    (scope_id, user_id),
                )
            else:
                return False
            return (await cur.fetchone()) is not None

    if conn is not None:
        return await _check(conn)
    async with get_db_connection() as db_conn:
        return await _check(db_conn)


async def upsert_task_cap(
    user_id: str,
    scope_kind: str,
    scope_id: str,
    cap_usd: Decimal,
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_budget_task_caps (user_id, scope_kind, scope_id, cap_usd, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, scope_kind, scope_id) DO UPDATE
                SET cap_usd = EXCLUDED.cap_usd,
                    updated_at = NOW()
                RETURNING user_id, scope_kind, scope_id, cap_usd, updated_at
                """,
                (user_id, scope_kind, scope_id, cap_usd),
            )
            row = await cur.fetchone()
            assert row is not None
            return dict(row)


async def delete_task_cap(user_id: str, scope_kind: str, scope_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_budget_task_caps
                WHERE user_id = %s AND scope_kind = %s AND scope_id = %s
                """,
                (user_id, scope_kind, scope_id),
            )
            return (cur.rowcount or 0) > 0


async def release_reservation(
    conn: Any,
    run_ref: str,
    generation: int | None = None,
) -> int:
    """Mark an open reservation as released, gated on optional generation (G1.4 / R2-7)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE dalton_budget_reservations
            SET state = 'released',
                released_at = NOW()
            WHERE run_ref = %s
              AND state = 'open'
              AND (%s::int IS NULL OR generation = %s::int)
            """,
            (run_ref, generation, generation),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


async def lookup_run_context(conn: Any, run_ref: str) -> tuple[str | None, str | None]:
    """Resolve `(thread_id, workspace_id)` from `conversation_responses` / `conversation_threads`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                cr.conversation_thread_id::text AS thread_id,
                ct.workspace_id::text AS workspace_id
            FROM conversation_responses cr
            LEFT JOIN conversation_threads ct
                ON ct.conversation_thread_id = cr.conversation_thread_id
            WHERE cr.conversation_response_id::text = %s
            """,
            (str(run_ref),),
        )
        row = await cur.fetchone()
        if not row:
            return None, None
        return row.get("thread_id"), row.get("workspace_id")


async def resolve_effective_task_cap(
    conn: Any,
    user_id: str,
    thread_id: str | None,
    workspace_id: str | None,
    default_task_cap_usd: Decimal | None,
) -> Decimal | None:
    """Priority: thread cap > workspace cap > default_task_cap_usd."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if thread_id:
            await cur.execute(
                """
                SELECT cap_usd FROM dalton_budget_task_caps
                WHERE user_id = %s AND scope_kind = 'thread' AND scope_id = %s
                """,
                (user_id, thread_id),
            )
            r = await cur.fetchone()
            if r and r.get("cap_usd") is not None:
                return Decimal(str(r["cap_usd"]))

        if workspace_id:
            await cur.execute(
                """
                SELECT cap_usd FROM dalton_budget_task_caps
                WHERE user_id = %s AND scope_kind = 'workspace' AND scope_id = %s
                """,
                (user_id, workspace_id),
            )
            r = await cur.fetchone()
            if r and r.get("cap_usd") is not None:
                return Decimal(str(r["cap_usd"]))

    return default_task_cap_usd


# Mirrors `src.server.database.runs.credit_ledger._ABANDONED_SETTLE_GRACE`.
_ABANDONED_SETTLE_GRACE = "30 minutes"
# Late subagent usage rows arrive within milliseconds to minutes; 2 days covers midnight rollover without scanning all historical settled rows on every admission.
_LATE_USAGE_RATCHET_WINDOW = "2 days"

# Effective terminal timestamp on `conversation_responses`: prefers `metadata->>'dalton_finalized_at'`
# stamped by `finalize_run`, falling back to `created_at + execution_time` for legacy or seeded rows.
_RESPONSE_FINALIZED_AT_SQL = """COALESCE(
    CASE
        WHEN (cr.metadata->>'dalton_finalized_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
        THEN (cr.metadata->>'dalton_finalized_at')::timestamptz
        ELSE NULL
    END,
    cr.created_at + make_interval(secs => GREATEST(COALESCE(cr.execution_time, 0), 0))
)"""

_HAS_PTC_USAGE_EXISTS_SQL = """EXISTS (
    SELECT 1
    FROM conversation_usages pcu
    WHERE pcu.conversation_response_id = cr.conversation_response_id
      AND pcu.msg_type = 'ptc'
      AND pcu.token_usage IS NOT NULL
      AND jsonb_typeof(pcu.token_usage::jsonb -> 'per_call_costs') = 'array'
      AND jsonb_array_length(pcu.token_usage::jsonb -> 'per_call_costs') > 0
)"""

_PROVEN_NO_MODEL_CALLS_SQL = (
    """COALESCE(cr.metadata @> '{"dalton_no_model_calls": true}'::jsonb, FALSE)"""
)

# Parent grace check when `usage_settled_at` is already stamped:
# - Post-deploy rows with `dalton_finalized_at` wait 30m from `dalton_finalized_at` so a
#   fallback stamp on `usage_settled_at` never restarts a second 30m window.
# - Legacy rows without `dalton_finalized_at` do not start a 30m clock from `usage_settled_at`;
#   each unsettled child's grace is measured solely from its own `sr.finalized_at`.
_PARENT_SETTLED_GRACE_PASSED_SQL = f"""CASE
    WHEN (cr.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
    THEN (cr.metadata->>'dalton_finalized_at')::timestamptz < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
    ELSE TRUE
END"""

# A direct subagent resumed within the same parent turn merges usage from all its same-parent
# executions into a single `msg_type='task'` usage row stamped with the latest `task_run_id`
# (`middleware.py:473`, `subagent_usage.py:145`), while `settle_task_run` stamps `usage_settled_at`
# across the whole `predecessor_run_id` chain. Treat a chain under `r.run_ref` as having usage
# whenever any execution on that same-parent resume chain (linked by `predecessor_run_id` or same
# `(thread_id, task_id)`) has a matching `task` usage row on `r.run_ref`.
_DIRECT_SUBAGENT_CHAIN_HAS_USAGE_SQL = """EXISTS (
    WITH RECURSIVE chain AS (
        SELECT sr.task_run_id, sr.predecessor_run_id, sr.parent_run_id, sr.thread_id, sr.task_id
      UNION
        SELECT s.task_run_id, s.predecessor_run_id, s.parent_run_id, s.thread_id, s.task_id
        FROM subagent_runs s
        JOIN chain c
          ON s.parent_run_id = c.parent_run_id
         AND (
             s.predecessor_run_id = c.task_run_id
             OR s.task_run_id = c.predecessor_run_id
             OR (s.thread_id = c.thread_id AND s.task_id = c.task_id)
         )
    )
    SELECT 1
    FROM chain c
    JOIN conversation_usages scu
      ON scu.conversation_response_id = r.run_ref
     AND scu.msg_type = 'task'
     AND scu.token_usage IS NOT NULL
     AND scu.token_usage::jsonb ->> 'task_run_id' = c.task_run_id::text
)"""

_ALL_USAGE_PRICING_COMPLETE_SQL = """COALESCE(
    BOOL_AND(
        cu.conversation_usage_id IS NULL
        OR (
            (
                NOT COALESCE(cu.is_byok, FALSE)
                OR (
                    cu.token_usage IS NOT NULL
                    AND COALESCE(jsonb_typeof(cu.token_usage::jsonb -> 'per_call_costs') = 'array', FALSE)
                )
            )
            AND NOT COALESCE(cu.token_usage::jsonb @> '{"pricing_failed": true}'::jsonb, FALSE)
            AND NOT (
                cu.token_usage IS NOT NULL
                AND jsonb_typeof(cu.token_usage::jsonb -> 'per_call_costs') = 'array'
                AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(cu.token_usage::jsonb -> 'per_call_costs') c
                    WHERE COALESCE(c @> '{"pricing_failed": true}'::jsonb, FALSE)
                )
            )
        )
    ),
    FALSE
)"""


async def _dalton_reservations_table_exists(cur: Any) -> bool:
    """Return True when `dalton_budget_reservations` is present in the connection's active schema."""
    await cur.execute(
        "SELECT to_regclass('dalton_budget_reservations') AS reg"  # resolved by search_path, like the queries that use it
    )
    row = await cur.fetchone()
    if row is None:
        return False
    if isinstance(row, dict):
        return row.get("reg") is not None
    return bool(row and row[0] is not None)


async def settle_abandoned_runs_without_ptc_sql(
    conn: Any,
    rate: Decimal,
    *,
    user_id: str | None = None,
    check_table_exists: bool = True,
) -> int:
    """Bounded fallback settlement for terminal runs past `_ABANDONED_SETTLE_GRACE` with no `ptc` usage row.

    Skipped cleanly (returns 0) when Dalton is disabled or when `dalton_budget_reservations`
    has not been migrated in an upstream-only deployment.

    Treats platform (`in_flight_credits > 0`) and BYOK (`in_flight_credits = 0`) runs identically:
    1. Stamps `conversation_responses.usage_settled_at = NOW()` (scoped to in-flight, post-deploy
       `dalton_finalized_at`, or reserved rows so historical unreserved rows are untouched) so
       rows never hang indefinitely and child `subagent_runs` abandoned settlement can proceed.
    2. Settles matching open/released `dalton_budget_reservations`:
       - Runs with proven zero model calls (`metadata @> '{"dalton_no_model_calls": true}'`),
         complete pricing on any tool/task usage rows, and complete direct-subagent usage settle
         at actual `usage_usd` without applying the `reserved_usd` floor.
       - All other runs without a `ptc` usage row settle conservatively at
         `GREATEST(reserved_usd, observed_usd, usage_usd)` without releasing `reserved_usd`.
    """
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        return 0
    async with conn.cursor() as cur:
        if check_table_exists and not await _dalton_reservations_table_exists(cur):
            return 0
        await cur.execute(
            f"""
            WITH abandoned_no_ptc_parents AS (
                UPDATE conversation_responses cr
                SET usage_settled_at = NOW()
                WHERE cr.usage_settled_at IS NULL
                  AND cr.status != 'in_progress'
                  AND (
                      %s::text IS NULL
                      OR cr.user_id = %s
                      OR EXISTS (
                          SELECT 1
                          FROM dalton_budget_reservations r
                          WHERE r.run_ref = cr.conversation_response_id
                            AND r.user_id = %s
                      )
                  )
                  AND (
                      (cr.in_flight_credits > 0 AND cr.user_id IS NOT NULL)
                      OR (cr.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                      OR EXISTS (
                          SELECT 1
                          FROM dalton_budget_reservations br
                          WHERE br.run_ref = cr.conversation_response_id
                            AND br.reserved_usd IS NOT NULL
                      )
                  )
                  AND {_RESPONSE_FINALIZED_AT_SQL} < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                  AND NOT {_HAS_PTC_USAGE_EXISTS_SQL}
                RETURNING cr.conversation_response_id
            ),
            abandoned_no_ptc_usage AS (
                SELECT
                    r.reservation_id,
                    r.run_ref,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd,
                    BOOL_OR({_PROVEN_NO_MODEL_CALLS_SQL}) AS proven_no_model_calls,
                    {_ALL_USAGE_PRICING_COMPLETE_SQL} AS all_usage_pricing_complete,
                    NOT EXISTS (
                        SELECT 1
                        FROM subagent_runs sr
                        WHERE sr.parent_run_id = r.run_ref
                          AND NOT {_DIRECT_SUBAGENT_CHAIN_HAS_USAGE_SQL}
                    ) AS all_direct_subagents_have_usage
                FROM dalton_budget_reservations r
                JOIN conversation_responses cr
                    ON cr.conversation_response_id = r.run_ref
                LEFT JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE (%s::text IS NULL OR r.user_id = %s)
                  AND r.state IN ('open', 'released')
                  AND cr.status != 'in_progress'
                  AND (
                      cr.usage_settled_at IS NOT NULL
                      OR cr.conversation_response_id IN (
                          SELECT conversation_response_id FROM abandoned_no_ptc_parents
                      )
                  )
                  AND {_RESPONSE_FINALIZED_AT_SQL} < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                  AND NOT {_HAS_PTC_USAGE_EXISTS_SQL}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM subagent_runs sr
                      WHERE sr.parent_run_id = r.run_ref
                        AND (
                            sr.status = 'in_progress'
                            OR (
                                sr.usage_settled_at IS NULL
                                AND (
                                    sr.finalized_at IS NULL
                                    OR sr.finalized_at >= NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                                )
                            )
                        )
                  )
                GROUP BY r.reservation_id, r.run_ref
            )
            UPDATE dalton_budget_reservations r
            SET state = 'settled',
                settled_at = NOW(),
                settled_usd = CASE
                    WHEN fu.proven_no_model_calls
                     AND fu.all_usage_pricing_complete
                     AND fu.all_direct_subagents_have_usage
                    THEN fu.usage_usd
                    ELSE GREATEST(r.reserved_usd, r.observed_usd, fu.usage_usd)
                END,
                settle_source = CASE
                    WHEN fu.proven_no_model_calls
                     AND fu.all_usage_pricing_complete
                     AND fu.all_direct_subagents_have_usage
                    THEN 'usage'
                    ELSE 'conservative'
                END
            FROM abandoned_no_ptc_usage fu
            WHERE r.reservation_id = fu.reservation_id
              AND r.state IN ('open', 'released')
            """,
            (user_id, user_id, user_id, rate, user_id, user_id),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


async def reconcile_reservations_sql(
    conn: Any,
    user_id: str,
    rate: Decimal,
) -> None:
    """Lazy reconciliation of open, released, and expired reservations (P0.8 §4, P0N.6 §2).

    Uses `dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, rate)`
    so per-call cash costs (`platform` and `byok`) are summed in USD while `oauth` calls are 0.
    """
    async with conn.cursor() as cur:
        # 1. Settle completed runs once parent and all child subagent runs are billing-settled.
        # Upstream terminal status does not imply billing-settled (`subagent_runs` finalizes before collector inserts `task` usage and stamps `usage_settled_at`).
        # Keep the conservative `reserved_usd` floor whenever:
        #   - any usage row on `r.run_ref` is degraded (`is_byok` without a `per_call_costs` array) or marked `pricing_failed`, OR
        #   - no parent `ptc` usage row with non-empty `per_call_costs` exists AND the run was not proven to have zero model calls (`metadata.dalton_no_model_calls = true`), OR
        #   - any direct subagent's same-parent resume chain has no `task` usage row on `r.run_ref`.
        await cur.execute(
            f"""
            WITH run_usage AS (
                SELECT
                    r.reservation_id,
                    r.run_ref,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd,
                    COUNT(cu.conversation_usage_id) AS usage_rows,
                    COALESCE(
                        BOOL_OR(
                            cu.msg_type = 'ptc'
                            AND cu.token_usage IS NOT NULL
                            AND jsonb_typeof(cu.token_usage::jsonb -> 'per_call_costs') = 'array'
                            AND jsonb_array_length(cu.token_usage::jsonb -> 'per_call_costs') > 0
                        ),
                        FALSE
                    ) AS has_ptc_usage,
                    {_ALL_USAGE_PRICING_COMPLETE_SQL} AS all_usage_pricing_complete,
                    NOT EXISTS (
                        SELECT 1
                        FROM subagent_runs sr
                        WHERE sr.parent_run_id = r.run_ref
                          AND NOT {_DIRECT_SUBAGENT_CHAIN_HAS_USAGE_SQL}
                    ) AS all_direct_subagents_have_usage
                FROM dalton_budget_reservations r
                LEFT JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.user_id = %s
                  AND r.state IN ('open', 'released')
                GROUP BY r.reservation_id, r.run_ref
            )
            UPDATE dalton_budget_reservations r
            SET state = 'settled',
                settled_at = NOW(),
                settled_usd = CASE
                    WHEN ru.all_usage_pricing_complete
                     AND (ru.has_ptc_usage OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND ru.all_direct_subagents_have_usage
                    THEN ru.usage_usd
                    ELSE GREATEST(r.reserved_usd, r.observed_usd, ru.usage_usd)
                END,
                settle_source = CASE
                    WHEN ru.all_usage_pricing_complete
                     AND (ru.has_ptc_usage OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND ru.all_direct_subagents_have_usage
                    THEN 'usage'
                    ELSE 'conservative'
                END
            FROM run_usage ru,
                 conversation_responses cr
            WHERE r.reservation_id = ru.reservation_id
              AND cr.conversation_response_id = r.run_ref
              AND cr.usage_settled_at IS NOT NULL
              AND (ru.usage_rows > 0 OR {_PROVEN_NO_MODEL_CALLS_SQL})
              AND r.state IN ('open', 'released')
              AND NOT EXISTS (
                  SELECT 1 FROM subagent_runs sr
                  WHERE sr.parent_run_id = r.run_ref
                    AND (sr.usage_settled_at IS NULL OR sr.status = 'in_progress')
              )
            """,
            (rate, user_id),
        )

        # 2. Settle runs with abandoned unsettled subagent runs after the 30m grace window.
        # Terminal status does not imply billing-settled, and upstream `settle_abandoned` skips zero-credit rows (`in_flight_credits = 0`) if a collector crashes.
        # Mirrors upstream's dual-clock guard (both parent terminal instant and child `finalized_at` older than 30m, with parent already settled) because the collector starts only after the parent run reaches terminal state.
        # Finished RunWorkflow driver rows (`cause = 'init'` and `replay_facts @> '{"workflow_driver": true}'`) never make LLM/tool calls of their own and therefore never receive `usage_settled_at`. Drop the conservative `reserved_usd` floor ONLY when usage is proven complete:
        #   (a) every non-workflow direct subagent under `r.run_ref` is billing-settled AND its same-parent resume chain has a `conversation_usages` (`msg_type='task'`) row on `r.run_ref` (so a `settle_abandoned` stamp without a usage row stays conservative);
        #   (b) every workflow internal child linked via `replay_facts @> '{"workflow_driver_run_id": <driver task_run_id>}'` is finished AND has a `conversation_usages` (`msg_type='task'`) row matching its `task_run_id`;
        #   (c) every `is_byok` usage row on `r.run_ref` carries a JSON array `per_call_costs` AND no usage row or per-call record on `r.run_ref` is marked `pricing_failed: true` (degraded and pricing-failed rows stay conservative);
        #   (d) a parent `ptc` usage row with non-empty `per_call_costs` exists (`has_ptc_usage`) with `usage_rows > 0`, OR the run was proven to have zero model calls (`dalton_no_model_calls: true`), and even then never settle below `observed_usd`.
        await cur.execute(
            f"""
            WITH abandoned_usage AS (
                SELECT
                    r.reservation_id,
                    r.run_ref,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd,
                    COUNT(cu.conversation_usage_id) AS usage_rows,
                    COALESCE(
                        BOOL_OR(
                            cu.msg_type = 'ptc'
                            AND cu.token_usage IS NOT NULL
                            AND jsonb_typeof(cu.token_usage::jsonb -> 'per_call_costs') = 'array'
                            AND jsonb_array_length(cu.token_usage::jsonb -> 'per_call_costs') > 0
                        ),
                        FALSE
                    ) AS has_ptc_usage,
                    {_ALL_USAGE_PRICING_COMPLETE_SQL} AS all_byok_have_per_call_costs,
                    NOT EXISTS (
                        SELECT 1 FROM subagent_runs sr
                        WHERE sr.parent_run_id = r.run_ref
                          AND NOT (
                              sr.status != 'in_progress'
                              AND sr.cause = 'init'
                              AND COALESCE(sr.replay_facts @> '{{"workflow_driver": true}}'::jsonb, FALSE)
                          )
                          AND (
                              sr.usage_settled_at IS NULL
                              OR NOT {_DIRECT_SUBAGENT_CHAIN_HAS_USAGE_SQL}
                          )
                    ) AS all_direct_subagents_settled_with_usage,
                    NOT EXISTS (
                        SELECT 1
                        FROM subagent_runs drv
                        JOIN subagent_runs child
                            ON child.thread_id = drv.thread_id
                           AND child.parent_run_id IS NULL
                           AND child.replay_facts @> jsonb_build_object(
                               'workflow_driver_run_id',
                               drv.task_run_id::text
                           )
                        WHERE drv.parent_run_id = r.run_ref
                          AND drv.status != 'in_progress'
                          AND drv.cause = 'init'
                          AND COALESCE(drv.replay_facts @> '{{"workflow_driver": true}}'::jsonb, FALSE)
                          AND (
                              child.status = 'in_progress'
                              OR NOT EXISTS (
                                  SELECT 1 FROM conversation_usages ccu
                                  WHERE ccu.conversation_response_id = r.run_ref
                                    AND ccu.msg_type = 'task'
                                    AND ccu.token_usage IS NOT NULL
                                    AND ccu.token_usage::jsonb ->> 'task_run_id' = child.task_run_id::text
                              )
                          )
                    ) AS all_workflow_children_accounted
                FROM dalton_budget_reservations r
                LEFT JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.user_id = %s
                  AND r.state IN ('open', 'released')
                GROUP BY r.reservation_id, r.run_ref
            )
            UPDATE dalton_budget_reservations r
            SET state = 'settled',
                settled_at = NOW(),
                settled_usd = CASE
                    WHEN (au.usage_rows > 0 OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND (au.has_ptc_usage OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND au.all_byok_have_per_call_costs
                     AND au.all_direct_subagents_settled_with_usage
                     AND au.all_workflow_children_accounted
                    THEN GREATEST(r.observed_usd, au.usage_usd)
                    ELSE GREATEST(r.reserved_usd, r.observed_usd, au.usage_usd)
                END,
                settle_source = CASE
                    WHEN (au.usage_rows > 0 OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND (au.has_ptc_usage OR {_PROVEN_NO_MODEL_CALLS_SQL})
                     AND au.all_byok_have_per_call_costs
                     AND au.all_direct_subagents_settled_with_usage
                     AND au.all_workflow_children_accounted
                     AND au.usage_usd >= r.observed_usd
                    THEN 'usage'
                    ELSE 'conservative'
                END
            FROM abandoned_usage au,
                 conversation_responses cr
            WHERE r.reservation_id = au.reservation_id
              AND cr.conversation_response_id = r.run_ref
              AND cr.usage_settled_at IS NOT NULL
              AND {_PARENT_SETTLED_GRACE_PASSED_SQL}
              AND r.state IN ('open', 'released')
              AND NOT EXISTS (
                  SELECT 1 FROM subagent_runs sr
                  WHERE sr.parent_run_id = r.run_ref
                    AND sr.status = 'in_progress'
              )
              AND EXISTS (
                  SELECT 1 FROM subagent_runs sr
                  WHERE sr.parent_run_id = r.run_ref
                    AND sr.usage_settled_at IS NULL
                    AND sr.finalized_at < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM subagent_runs sr
                  WHERE sr.parent_run_id = r.run_ref
                    AND sr.usage_settled_at IS NULL
                    AND (
                        sr.finalized_at IS NULL
                        OR sr.finalized_at >= NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                    )
              )
            """,
            (rate, user_id),
        )

        # 2b. Bounded fallback settlement for terminal runs past `_ABANDONED_SETTLE_GRACE`
        # still lacking a parent `ptc` usage row (crash recovery, dispatch reconcile, all calls errored,
        # or unmetered `stream_usage=False`). Stamps `conversation_responses.usage_settled_at` for
        # platform and BYOK (`in_flight_credits=0`) rows alike and settles reservations conservatively
        # at `GREATEST(reserved_usd, observed_usd, usage_usd)` without releasing `reserved_usd`.
        await settle_abandoned_runs_without_ptc_sql(
            conn, rate, user_id=user_id, check_table_exists=False
        )

        # 3. Expire stale open leases conservatively (never release).
        # An open lease past `expires_at` is not billing-settled, so charge `GREATEST(reserved, observed, usage)`.
        await cur.execute(
            """
            WITH stale_usage AS (
                SELECT
                    r.reservation_id,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd
                FROM dalton_budget_reservations r
                LEFT JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.user_id = %s
                  AND r.state = 'open'
                  AND r.expires_at < NOW()
                GROUP BY r.reservation_id
            )
            UPDATE dalton_budget_reservations r
            SET state = 'expired',
                settled_at = NOW(),
                settled_usd = GREATEST(r.reserved_usd, r.observed_usd, su.usage_usd),
                settle_source = 'conservative'
            FROM stale_usage su
            WHERE r.reservation_id = su.reservation_id
              AND r.state = 'open'
              AND r.expires_at < NOW()
            """,
            (rate, user_id),
        )

        # 4. Ratchet expired reservations upward if recovery sweep or collector later persisted higher usage.
        # Terminal status does not imply billing-settled, so usage rows can land after lease expiry.
        await cur.execute(
            """
            WITH exp_usage AS (
                SELECT
                    r.reservation_id,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd
                FROM dalton_budget_reservations r
                JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.user_id = %s
                  AND r.state = 'expired'
                GROUP BY r.reservation_id
            )
            UPDATE dalton_budget_reservations r
            SET settled_usd = GREATEST(COALESCE(r.settled_usd, 0), eu.usage_usd)
            FROM exp_usage eu
            WHERE r.reservation_id = eu.reservation_id
              AND r.state = 'expired'
              AND eu.usage_usd > COALESCE(r.settled_usd, 0)
            """,
            (rate, user_id),
        )

        # 5. Ratchet recently settled reservations upward if late `task` usage rows landed after an earlier settlement (`settle_source` unchanged).
        # Because subagent terminal status precedes billing-settled (`usage_settled_at`), any late usage on `run_ref` must unfreeze `settled_usd`.
        await cur.execute(
            f"""
            WITH settled_usage AS (
                SELECT
                    r.reservation_id,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd
                FROM dalton_budget_reservations r
                JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.user_id = %s
                  AND r.state = 'settled'
                  AND r.settled_at > NOW() - INTERVAL '{_LATE_USAGE_RATCHET_WINDOW}'
                GROUP BY r.reservation_id
            )
            UPDATE dalton_budget_reservations r
            SET settled_usd = GREATEST(COALESCE(r.settled_usd, 0), su.usage_usd)
            FROM settled_usage su
            WHERE r.reservation_id = su.reservation_id
              AND r.state = 'settled'
              AND r.settled_at > NOW() - INTERVAL '{_LATE_USAGE_RATCHET_WINDOW}'
              AND su.usage_usd > COALESCE(r.settled_usd, 0)
            """,
            (rate, user_id),
        )


async def fetch_day_reservations(
    conn: Any,
    user_id: str,
    budget_day: date,
    rate: Decimal,
) -> list[dict[str, Any]]:
    """Return reservations for `(user_id, budget_day)` along with `usage_usd` and `charge_usd`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                r.*,
                COALESCE(u.usage_usd, 0) AS usage_usd,
                CASE
                    WHEN r.state = 'settled' THEN COALESCE(r.settled_usd, COALESCE(u.usage_usd, 0))
                    WHEN r.state = 'expired' THEN COALESCE(
                        r.settled_usd,
                        GREATEST(r.reserved_usd, r.observed_usd, COALESCE(u.usage_usd, 0))
                    )
                    ELSE GREATEST(r.reserved_usd, r.observed_usd, COALESCE(u.usage_usd, 0))
                END AS charge_usd
            FROM dalton_budget_reservations r
            LEFT JOIN (
                SELECT
                    conversation_response_id,
                    COALESCE(
                        SUM(dalton_cash_usd(token_usage::jsonb, total_credits, infrastructure_credits, %s)),
                        0
                    ) AS usage_usd
                FROM conversation_usages
                WHERE user_id = %s
                GROUP BY conversation_response_id
            ) u ON u.conversation_response_id = r.run_ref
            WHERE r.user_id = %s AND r.budget_day = %s
            ORDER BY r.created_at DESC
            """,
            (rate, user_id, user_id, budget_day),
        )
        return [dict(row) for row in await cur.fetchall()]


async def fetch_unreserved_usage_today(
    conn: Any,
    user_id: str,
    day_start_utc: datetime,
    day_end_utc: datetime,
    rate: Decimal,
    *,
    conservative_floor_usd: Decimal | None = None,
) -> Decimal:
    """Sum cash USD for today's `conversation_usages` not tied to any reservation.

    Rows carrying `pricing_failed` add one conservative floor (`admission_floor_usd`) per
    pricing-failed call, matching the provider breakdown and journal cash rules so the daily
    budget gate `spent_usd` never undercounts unpriced unreserved calls as $0.
    """
    floor_usd = (
        conservative_floor_usd
        if conservative_floor_usd is not None
        else _conservative_pricing_floor_usd()
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT COALESCE(
                SUM(
                    CASE
                        WHEN u.token_usage IS NOT NULL
                         AND jsonb_typeof(u.token_usage::jsonb -> 'per_call_costs') = 'array'
                         AND jsonb_array_length(u.token_usage::jsonb -> 'per_call_costs') > 0
                        THEN u.cash_usd + COALESCE((
                            SELECT SUM(
                                GREATEST(
                                    %s::numeric - GREATEST(COALESCE(NULLIF(c->>'cost', '')::numeric, 0), 0),
                                    0
                                )
                            )
                            FROM jsonb_array_elements(u.token_usage::jsonb -> 'per_call_costs') c
                            WHERE COALESCE(c @> '{"pricing_failed": true}'::jsonb, FALSE)
                               OR (
                                   COALESCE(u.token_usage::jsonb @> '{"pricing_failed": true}'::jsonb, FALSE)
                                   AND NOT EXISTS (
                                       SELECT 1
                                       FROM jsonb_array_elements(u.token_usage::jsonb -> 'per_call_costs') c2
                                       WHERE COALESCE(c2 @> '{"pricing_failed": true}'::jsonb, FALSE)
                                   )
                                   AND COALESCE(c->>'billing_type', 'platform') <> 'oauth'
                                   AND COALESCE(NULLIF(c->>'cost', '')::numeric, 0) <= 0
                                   AND (
                                       NOT (
                                           jsonb_typeof(u.token_usage::jsonb -> 'pricing_failed_models') = 'array'
                                           AND jsonb_array_length(u.token_usage::jsonb -> 'pricing_failed_models') > 0
                                       )
                                       OR EXISTS (
                                           SELECT 1
                                           FROM jsonb_array_elements_text(u.token_usage::jsonb -> 'pricing_failed_models') fm(name)
                                           WHERE fm.name = COALESCE(c->>'model_name', '')
                                       )
                                   )
                               )
                        ), 0)
                        WHEN u.token_usage IS NOT NULL
                         AND COALESCE(u.token_usage::jsonb @> '{"pricing_failed": true}'::jsonb, FALSE)
                        THEN GREATEST(%s::numeric, u.cash_usd)
                        ELSE u.cash_usd
                    END
                ),
                0
            ) AS unreserved_usd
            FROM (
                SELECT
                    cu.token_usage,
                    dalton_cash_usd(
                        cu.token_usage::jsonb,
                        cu.total_credits,
                        cu.infrastructure_credits,
                        %s
                    ) AS cash_usd
                FROM conversation_usages cu
                WHERE cu.user_id = %s
                  AND cu.created_at >= %s
                  AND cu.created_at < %s
                  AND NOT EXISTS (
                      SELECT 1 FROM dalton_budget_reservations r
                      WHERE r.run_ref = cu.conversation_response_id
                  )
            ) u
            """,
            (floor_usd, floor_usd, rate, user_id, day_start_utc, day_end_utc),
        )
        row = await cur.fetchone()
        return Decimal(str((row or {}).get("unreserved_usd") or "0"))


async def fetch_journal_cash_today(
    conn_or_cur: Any,
    day_start_utc: datetime,
    day_end_utc: datetime,
    user_id: str | None = None,
) -> Decimal:
    """Sum `cash_usd` for today's one-shot journal attempts attributed by each attempt's
    `occurred_at` (with legacy fallback for `dalton_llm_journal` rows that predate
    `dalton_llm_journal_attempts`) (P0N.6 §5, P0N.7, P0H §5 B2).

    Fails closed by logging ERROR and re-raising on DB query failure.
    """
    query = """
        SELECT COALESCE(SUM(cash_usd), 0) AS journal_cash_usd
        FROM (
            SELECT a.cash_usd
            FROM dalton_llm_journal_attempts a
            WHERE a.occurred_at >= %s
              AND a.occurred_at < %s
              AND (a.outcome IN ('done', 'indeterminate') OR a.cash_usd > 0)
              AND (%s::text IS NULL OR a.user_id = %s OR a.user_id IS NULL)
            UNION ALL
            SELECT j.cash_usd
            FROM dalton_llm_journal j
            WHERE j.created_at >= %s
              AND j.created_at < %s
              AND (j.state IN ('done', 'indeterminate') OR j.cash_usd > 0)
              AND (%s::text IS NULL OR j.user_id = %s OR j.user_id IS NULL)
              AND NOT EXISTS (
                  SELECT 1
                  FROM dalton_llm_journal_attempts a
                  WHERE a.invocation_id = j.invocation_id
              )
        ) AS billed
    """
    params = (
        day_start_utc,
        day_end_utc,
        user_id,
        user_id,
        day_start_utc,
        day_end_utc,
        user_id,
        user_id,
    )
    try:
        if hasattr(conn_or_cur, "cursor"):
            async with conn_or_cur.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
        else:
            await conn_or_cur.execute(query, params)
            row = await conn_or_cur.fetchone()
        if isinstance(row, dict):
            val = row.get("journal_cash_usd") or row.get("cash_usd") or "0"
        elif isinstance(row, (tuple, list)) and row:
            val = row[0] or "0"
        else:
            val = "0"
        return Decimal(str(val))
    except Exception as exc:
        logger.error("[DaltonBudget] fetch_journal_cash_today failed: %s", exc)
        raise


async def fetch_scoped_task_spend(
    conn: Any,
    user_id: str,
    *,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    summary_rows: list[dict[str, Any]] | None = None,
) -> Decimal:
    """Sum cumulative cash spend for a thread or workspace scope (P0H §5 B3).

    Combines reservation charges from `summary_rows` (or `dalton_budget_reservations`)
    and one-shot cash spend in `dalton_llm_journal` matching the scope.
    """
    total = Decimal("0")
    if summary_rows is not None:
        for r in summary_rows:
            if not isinstance(r, dict):
                continue
            r_tid = str(r.get("thread_id") or "")
            r_wid = str(r.get("workspace_id") or "")
            if (thread_id and r_tid == str(thread_id)) or (
                not thread_id and workspace_id and r_wid == str(workspace_id)
            ):
                total += Decimal(str(r.get("charge_usd") or "0"))
    elif hasattr(conn, "cursor"):
        async with conn.cursor(row_factory=dict_row) as cur:
            if thread_id:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(GREATEST(reserved_usd, observed_usd, COALESCE(settled_usd, 0))), 0) AS scoped_usd
                    FROM dalton_budget_reservations
                    WHERE user_id = %s AND thread_id = %s
                    """,
                    (user_id, thread_id),
                )
            elif workspace_id:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(GREATEST(reserved_usd, observed_usd, COALESCE(settled_usd, 0))), 0) AS scoped_usd
                    FROM dalton_budget_reservations
                    WHERE user_id = %s AND workspace_id = %s
                    """,
                    (user_id, workspace_id),
                )
            else:
                return total
            row = await cur.fetchone()
            if isinstance(row, dict):
                total += Decimal(str(row.get("scoped_usd") or "0"))

    scope_token = str(thread_id or workspace_id or "").strip()
    if scope_token and hasattr(conn, "cursor"):
        async with conn.cursor(row_factory=dict_row) as cur:
            scope_like = f"%{scope_token}%"
            await cur.execute(
                """
                SELECT COALESCE(SUM(cash_usd), 0) AS journal_scoped_usd
                FROM (
                    SELECT a.cash_usd
                    FROM dalton_llm_journal_attempts a
                    WHERE (a.outcome IN ('done', 'indeterminate') OR a.cash_usd > 0)
                      AND (%s::text IS NULL OR a.user_id = %s OR a.user_id IS NULL)
                      AND a.scope_ref LIKE %s
                    UNION ALL
                    SELECT j.cash_usd
                    FROM dalton_llm_journal j
                    WHERE (j.state IN ('done', 'indeterminate') OR j.cash_usd > 0)
                      AND (%s::text IS NULL OR j.user_id = %s OR j.user_id IS NULL)
                      AND j.scope_ref LIKE %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM dalton_llm_journal_attempts a
                          WHERE a.invocation_id = j.invocation_id
                      )
                ) AS scoped
                """,
                (user_id, user_id, scope_like, user_id, user_id, scope_like),
            )
            j_row = await cur.fetchone()
            if isinstance(j_row, dict):
                total += Decimal(str(j_row.get("journal_scoped_usd") or "0"))

    return total


async def fetch_today_token_usages(
    conn: Any,
    user_id: str,
    day_start_utc: datetime,
    day_end_utc: datetime,
) -> list[dict[str, Any]]:
    """Fetch `token_usage` JSON payloads for today's usage rows to compute subscription notional USD."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT token_usage, is_byok
            FROM conversation_usages
            WHERE user_id = %s
              AND created_at >= %s
              AND created_at < %s
              AND token_usage IS NOT NULL
            """,
            (user_id, day_start_utc, day_end_utc),
        )
        return [dict(r) for r in await cur.fetchall()]

