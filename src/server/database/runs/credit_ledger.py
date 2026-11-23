"""The runtime credit gate's SQL surface, over both run tables.

Everything here exists because a run's platform spend is invisible until
finalize writes its usage row. The ledger columns make it visible in between,
so a reader can aggregate "unsettled in-flight spend per user" while the runs
are still going, and the resume query at the bottom is how a turn that stopped
for money finds the tasks that stopped with it.

Terminal and billing-settled are separate states throughout, deliberately: a
task run turns terminal in one CAS while its usage is collected later, so a row
keeps counting toward in-flight spend until ``usage_settled_at`` is stamped,
whatever its status says.

Held apart from ``lifecycle`` and ``subagent_runs`` rather than split between
them because it is one concept with one client. Both ledgers carry the same
columns and answer the same questions, so a lane names itself with ``kind`` and
the table is resolved below — the shape it replaced was the same two statements
written out twice, in two modules, kept in agreement by hand.
"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Literal, Tuple

from psycopg.rows import dict_row

from src.server.contracts.status import (
    CREDIT_STOP_ERROR_TYPE,
    INTERRUPT_REASON_CREDIT_PAUSE,
)
from src.server.database import pool

logger = logging.getLogger(__name__)

RunKind = Literal["run", "task"]

# The only thing that differs between the two ledgers. Closed literal, and the
# only source of the identifiers interpolated into the statements below.
_TABLES: Dict[RunKind, Tuple[str, str]] = {
    "run": ("conversation_responses", "conversation_response_id"),
    "task": ("subagent_runs", "task_run_id"),
}

_ABANDONED_SETTLE_GRACE = "30 minutes"


async def heartbeat(kind: RunKind, run_id: str, credits: float) -> bool:
    """Absolute write of a live run's cumulative platform spend onto its own row.

    GREATEST keeps the value monotone under reordered or duplicate flushes; the
    status and settle guards make a late heartbeat racing finalize a no-op
    rather than a resurrection of already-billed spend. Always a fresh app-pool
    connection — this fires from a background refresher and must never touch a
    run's pinned checkpointer session. Returns False when the row was already
    terminal or settled.

    Bound as Decimal because GREATEST(numeric, float8) resolves in double
    precision: a float parameter would drag the stored column value out to
    binary float and back on every beat, rounding a money-shaped number for
    no reason.
    """
    table, key = _TABLES[kind]
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE {table}
                SET in_flight_credits = GREATEST(in_flight_credits, %s)
                WHERE {key} = %s
                  AND status = 'in_progress'
                  AND usage_settled_at IS NULL
                """,
                (Decimal(str(credits)), run_id),
            )
            return cur.rowcount == 1


async def settle_task_run(task_run_id: str, *, conn) -> bool:
    """Stamp a task run billing-settled, in the collector's usage transaction.

    ``conn`` is mandatory: the whole point of the stamp is that it commits or
    rolls back with the usage row the collector just inserted, so the row can
    never read settled while its spend is missing from billing, nor billed
    while still counting as in-flight.

    The chain, not the row: a resume re-points the task at a new run id while
    its predecessor's records stay merged into the batch being billed here, so
    the predecessor settles in the same transaction or never.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT task_run_id, predecessor_run_id
                FROM subagent_runs WHERE task_run_id = %s
              -- UNION, not UNION ALL: the pair is functional, so dedup costs
              -- nothing on a real chain and is what terminates the walk if a
              -- predecessor ever points back into it. This runs inside the
              -- collector's open write transaction.
              UNION
                SELECT s.task_run_id, s.predecessor_run_id
                FROM subagent_runs s
                JOIN chain c ON s.task_run_id = c.predecessor_run_id
            )
            UPDATE subagent_runs
            SET usage_settled_at = NOW()
            WHERE task_run_id IN (SELECT task_run_id FROM chain)
              AND usage_settled_at IS NULL
            """,
            (task_run_id,),
        )
        return cur.rowcount >= 1


async def _has_dalton_reservations_table(cur: Any) -> bool:
    """Return True only when Dalton is enabled and `dalton_budget_reservations` exists
    in the connection's active schema.

    DALTON-HOOK(budget): upstream-only deployments (Dalton disabled or only upstream
    migrations applied) lack `dalton_budget_reservations`; fall back to the verbatim
    upstream sweep so abandoned settlement never fails with UndefinedTable or alters
    upstream-only settlement timing.
    """
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        return False
    await cur.execute(
        "SELECT to_regclass('dalton_budget_reservations') AS reg"  # resolved by search_path, like the queries that use it
    )
    row = await cur.fetchone()
    if row is None:
        return False
    if isinstance(row, dict):
        return row.get("reg") is not None
    return bool(row and row[0] is not None)


async def _settle_abandoned_upstream(cur: Any, kind: RunKind) -> int:
    # DALTON-HOOK(budget): Verbatim upstream/main:src/server/database/runs/credit_ledger.py:149-180
    # used unchanged whenever Dalton is disabled or `dalton_budget_reservations` is absent.
    table, _ = _TABLES[kind]
    guard = (
        f"""
                  AND finalized_at < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                  AND NOT EXISTS (
                      SELECT 1 FROM conversation_responses r
                      WHERE r.conversation_response_id = {table}.parent_run_id
                        AND (
                            r.status = 'in_progress'
                            OR r.usage_settled_at IS NULL
                            OR r.usage_settled_at
                                > NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                        )
                  )
        """
        if kind == "task"
        else ""
    )
    await cur.execute(
        f"""
                UPDATE {table}
                SET usage_settled_at = NOW()
                WHERE usage_settled_at IS NULL
                  AND in_flight_credits > 0
                  AND user_id IS NOT NULL
                  AND status != 'in_progress'
                  {guard}
                """
    )
    return cur.rowcount


async def settle_abandoned(kind: RunKind) -> int:
    """Degraded settle: stamp terminal-but-unsettled rows no settle path reaches.

    The two lanes need different proof that none is coming. A terminal response
    row waits out ``_ABANDONED_SETTLE_GRACE`` from its own terminal timestamp
    (``metadata->>'dalton_finalized_at'`` when present, falling back to
    ``created_at + execution_time``). A terminal task run needs its own grace
    (``finalized_at`` older than ``_ABANDONED_SETTLE_GRACE``) and a billing-
    settled parent (``usage_settled_at IS NOT NULL``); when the parent carries
    ``metadata->>'dalton_finalized_at'``, that parent terminal timestamp must
    also be past ``_ABANDONED_SETTLE_GRACE``, while legacy parents without
    ``dalton_finalized_at`` let each child's own ``finalized_at`` govern its
    grace instead of restarting a 30-minute clock from ``usage_settled_at``.

    DALTON-HOOK(budget): when Dalton is disabled or ``dalton_budget_reservations``
    does not exist in the connection's active schema, this runs the verbatim
    upstream sweep unchanged. When Dalton is enabled and the reservations table
    exists, scope is restricted to rows that carry in-flight platform spend
    (``in_flight_credits > 0 AND user_id IS NOT NULL``), rows finalized after
    this deploy (``metadata->>'dalton_finalized_at'``), or rows backed by a
    budget reservation (``dalton_budget_reservations.reserved_usd IS NOT NULL``;
    for direct tasks via ``parent_run_id`` and for workflow internal children
    via ``workflow_driver_run_id`` -> owning parent response). Pre-deploy
    unreserved historical rows with zero in-flight credits (including historical
    workflow internal children) are never touched. The existing partial
    in-flight indexes (``ix_responses_in_flight_by_user`` and
    ``ix_subagent_runs_in_flight_by_user``) cover only the ``in_flight_credits >
    0 AND user_id IS NOT NULL`` branch; covering the zero-credit BYOK/OAuth
    post-deploy branches without a table scan requires dedicated partial
    indexes on unsettled rows.
    """
    table, _ = _TABLES[kind]
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            # DALTON-HOOK(budget): use the extended Dalton sweep only when Dalton is enabled
            # and `dalton_budget_reservations` exists; otherwise run the verbatim upstream sweep.
            if not await _has_dalton_reservations_table(cur):
                return await _settle_abandoned_upstream(cur, kind)
            if kind == "task":
                # DALTON-HOOK(budget): treat platform (in_flight_credits > 0) and BYOK (in_flight_credits = 0)
                # rows alike within the scoped set (in-flight credits, dalton_finalized_at, or
                # dalton_budget_reservations.reserved_usd IS NOT NULL) so historical unreserved rows
                # (including historical workflow internal children) are untouched.
                # Direct task rows require a settled parent (usage_settled_at IS NOT NULL); parents with
                # dalton_finalized_at also wait out their own 30m terminal grace, while legacy parents
                # without dalton_finalized_at let each child's own finalized_at govern its grace.
                # RunWorkflow driver rows remain unstamped so budget reconciliation verifies workflow children.
                guard = f"""
                  AND finalized_at < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                  AND NOT (
                      cause = 'init'
                      AND COALESCE(replay_facts @> '{{"workflow_driver": true}}'::jsonb, FALSE)
                  )
                  AND (
                      (in_flight_credits > 0 AND user_id IS NOT NULL)
                      OR EXISTS (
                          SELECT 1
                          FROM conversation_responses r_scope
                          WHERE r_scope.conversation_response_id = {table}.parent_run_id
                            AND (
                                (r_scope.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                                OR EXISTS (
                                    SELECT 1
                                    FROM dalton_budget_reservations br
                                    WHERE br.run_ref = r_scope.conversation_response_id
                                      AND br.reserved_usd IS NOT NULL
                                )
                            )
                      )
                      OR (
                          {table}.parent_run_id IS NULL
                          AND ({table}.replay_facts->>'workflow_driver_run_id') IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM subagent_runs drv
                              JOIN conversation_responses r_wf
                                ON r_wf.conversation_response_id = drv.parent_run_id
                              WHERE drv.thread_id = {table}.thread_id
                                AND drv.task_run_id::text = {table}.replay_facts->>'workflow_driver_run_id'
                                AND r_wf.status != 'in_progress'
                                AND r_wf.usage_settled_at IS NOT NULL
                                AND (
                                    (r_wf.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                                    OR EXISTS (
                                        SELECT 1
                                        FROM dalton_budget_reservations br
                                        WHERE br.run_ref = r_wf.conversation_response_id
                                          AND br.reserved_usd IS NOT NULL
                                    )
                                )
                                AND CASE
                                    WHEN (r_wf.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                                    THEN (r_wf.metadata->>'dalton_finalized_at')::timestamptz < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                                    ELSE TRUE
                                END
                          )
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM conversation_responses r
                      WHERE r.conversation_response_id = {table}.parent_run_id
                        AND (
                            r.status = 'in_progress'
                            OR r.usage_settled_at IS NULL
                            OR CASE
                                WHEN (r.metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                                THEN (r.metadata->>'dalton_finalized_at')::timestamptz > NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                                ELSE FALSE
                            END
                        )
                  )
                """
            else:
                # DALTON-HOOK(budget): scope response fallback sweep to in-flight, post-deploy dalton_finalized_at,
                # or reserved rows so historical unreserved rows are untouched.
                guard = f"""
                  AND (
                      (in_flight_credits > 0 AND user_id IS NOT NULL)
                      OR (metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                      OR EXISTS (
                          SELECT 1
                          FROM dalton_budget_reservations br
                          WHERE br.run_ref = {table}.conversation_response_id
                            AND br.reserved_usd IS NOT NULL
                      )
                  )
                  AND COALESCE(
                      CASE
                          WHEN (metadata->>'dalton_finalized_at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                          THEN (metadata->>'dalton_finalized_at')::timestamptz
                          ELSE NULL
                      END,
                      created_at + make_interval(secs => GREATEST(COALESCE(execution_time, 0), 0))
                  ) < NOW() - INTERVAL '{_ABANDONED_SETTLE_GRACE}'
                """
            await cur.execute(
                f"""
                UPDATE {table}
                SET usage_settled_at = NOW()
                WHERE usage_settled_at IS NULL
                  AND status != 'in_progress'
                  {guard}
                """
            )
            return cur.rowcount


async def list_credit_stopped_for_resume(
    thread_id: str, resuming_run_id: str
) -> List[Dict[str, Any]]:
    """Task runs the credit gate stopped under the attempt being resumed.

    Feeds the resume-time injection: the model gets a mechanical record of
    which tasks stopped so it can re-enter them via Task(action="resume").

    The scope is the run immediately before ``resuming_run_id``, and it yields
    nothing unless that run is itself the credit pause. Anchoring on "the
    newest credit pause" instead would re-announce the same stops into
    checkpoint state on every later approval the thread ever asks for.

    The resuming run is excluded by attempt chain rather than by being newer
    than the query: a resume opens its row in the START transaction, well
    before this is called, so "latest row in the thread" is that row and every
    predicate below would miss. The chain rather than the single id, because a
    resume that fails retryably is retried as a NEW row chained by
    ``retry_of_run_id`` while the pause stays two rows back — excluding only
    the resuming run would anchor on the failed attempt, whose status is
    'error', and the retry that finally succeeds would announce nothing.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH RECURSIVE attempt_chain AS (
                    SELECT conversation_response_id, retry_of_run_id
                    FROM conversation_responses
                    WHERE conversation_response_id = %s
                  -- UNION, not UNION ALL: same reason as the predecessor walk
                  -- in settle_task_run, and the pair is functional so dedup
                  -- costs nothing on a real chain.
                  UNION
                    SELECT r.conversation_response_id, r.retry_of_run_id
                    FROM conversation_responses r
                    JOIN attempt_chain c
                      ON r.conversation_response_id = c.retry_of_run_id
                ),
                resumed AS (
                    SELECT conversation_response_id, status, interrupt_reason
                    FROM conversation_responses
                    WHERE conversation_thread_id = %s
                      AND conversation_response_id NOT IN (
                          SELECT conversation_response_id FROM attempt_chain
                      )
                    ORDER BY run_seq DESC
                    LIMIT 1
                )
                SELECT s.task_id
                FROM subagent_runs s
                JOIN resumed r ON s.parent_run_id = r.conversation_response_id
                -- Redundant against the join, but it is what makes this an
                -- index scan: parent_run_id carries no index of its own, so
                -- without the thread the join drives off a full table scan.
                WHERE s.thread_id = %s
                  AND r.status = 'interrupted'
                  AND r.interrupt_reason = %s
                  AND s.status = 'cancelled'
                  AND s.failure->>'error_type' = %s
                ORDER BY s.started_at
                """,
                (
                    resuming_run_id,
                    thread_id,
                    thread_id,
                    INTERRUPT_REASON_CREDIT_PAUSE,
                    CREDIT_STOP_ERROR_TYPE,
                ),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


__all__ = [
    "RunKind",
    "heartbeat",
    "list_credit_stopped_for_resume",
    "settle_abandoned",
    "settle_task_run",
]
