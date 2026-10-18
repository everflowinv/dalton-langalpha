"""Raw psycopg3 SQL queries for Dalton local daily USD budget and reservations (P0.8, P0N.6, G1)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


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
        return cur.rowcount or 0


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
            WHERE cr.conversation_response_id = %s
            """,
            (run_ref,),
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
        # 1. Settle completed runs with settled usage
        await cur.execute(
            """
            WITH run_usage AS (
                SELECT
                    r.reservation_id,
                    r.run_ref,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd,
                    COUNT(cu.conversation_usage_id) AS usage_rows
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
                settled_usd = ru.usage_usd,
                settle_source = 'usage'
            FROM run_usage ru,
                 conversation_responses cr
            WHERE r.reservation_id = ru.reservation_id
              AND cr.conversation_response_id = r.run_ref
              AND cr.usage_settled_at IS NOT NULL
              AND ru.usage_rows > 0
              AND r.state IN ('open', 'released')
              AND NOT EXISTS (
                  SELECT 1 FROM subagent_runs sr
                  WHERE sr.parent_run_id = r.run_ref
                    AND sr.status = 'in_progress'
              )
            """,
            (rate, user_id),
        )

        # 2. Expire stale open leases conservatively (never release)
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

        # 3. Ratchet expired reservations upward if recovery sweep later persisted higher usage
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
) -> Decimal:
    """Sum `dalton_cash_usd(...)` for today's `conversation_usages` not tied to any reservation."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT COALESCE(
                SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                0
            ) AS unreserved_usd
            FROM conversation_usages cu
            WHERE cu.user_id = %s
              AND cu.created_at >= %s
              AND cu.created_at < %s
              AND NOT EXISTS (
                  SELECT 1 FROM dalton_budget_reservations r
                  WHERE r.run_ref = cu.conversation_response_id
              )
            """,
            (rate, user_id, day_start_utc, day_end_utc),
        )
        row = await cur.fetchone()
        return Decimal(str((row or {}).get("unreserved_usd") or "0"))


async def fetch_journal_cash_today(
    conn_or_cur: Any,
    day_start_utc: datetime,
    day_end_utc: datetime,
    user_id: str | None = None,
) -> Decimal:
    """Sum `cash_usd` from `dalton_llm_journal` for today where `state IN ('done', 'indeterminate')` (P0N.6 §5, P0N.7, P0H §5 B2).

    Fails closed by logging ERROR and re-raising on DB query failure.
    """
    query = """
        SELECT COALESCE(SUM(cash_usd), 0) AS journal_cash_usd
        FROM dalton_llm_journal
        WHERE created_at >= %s
          AND created_at < %s
          AND state IN ('done', 'indeterminate')
          AND (%s::text IS NULL OR user_id = %s OR user_id IS NULL)
    """
    params = (day_start_utc, day_end_utc, user_id, user_id)
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
            await cur.execute(
                """
                SELECT COALESCE(SUM(cash_usd), 0) AS journal_scoped_usd
                FROM dalton_llm_journal
                WHERE state IN ('done', 'indeterminate')
                  AND (%s::text IS NULL OR user_id = %s OR user_id IS NULL)
                  AND scope_ref LIKE %s
                """,
                (user_id, user_id, f"%{scope_token}%"),
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

