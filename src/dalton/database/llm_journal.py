"""Raw psycopg3 SQL queries for Dalton one-shot LLM call journal (`dalton_llm_journal`, P0N.7, G1.3)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from src.dalton.database.provider_events import sanitize_error_text
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


def _is_stale(claimed_at: Any, stale_after_s: int, now_utc: datetime | None = None) -> bool:
    if not isinstance(claimed_at, datetime):
        return False
    now = now_utc or datetime.now(timezone.utc)
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return (now - claimed_at) > timedelta(seconds=max(1, int(stale_after_s)))


def is_journal_response_replayable(stored: Any) -> bool:
    """Return True only when a `state='done'` journal payload can be replayed without re-calling the LLM."""
    if stored is None:
        return False
    if isinstance(stored, dict):
        if stored.get("replayable") is False:
            return False
        data = stored.get("data")
        if isinstance(data, dict):
            if data.get("replayable") is False:
                return False
            cards = data.get("cards")
            if isinstance(cards, list) and any(
                isinstance(c, dict) and not str(c.get("quote") or "").strip()
                for c in cards
            ):
                return False
    return True


async def fetch_journal_entry(
    invocation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single `dalton_llm_journal` row by `invocation_id`."""
    async def _do_fetch(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT *
                FROM dalton_llm_journal
                WHERE invocation_id = %s
                """,
                (invocation_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    if conn is not None:
        return await _do_fetch(conn)
    async with get_db_connection() as db_conn:
        return await _do_fetch(db_conn)


get_journal_entry = fetch_journal_entry


async def fetch_journal_by_scope(
    scope_ref: str,
    request_hash: str | None = None,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch the most recent `dalton_llm_journal` row matching `scope_ref` (and optional `request_hash`)."""
    async def _do_fetch(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            if request_hash is not None:
                await cur.execute(
                    """
                    SELECT *
                    FROM dalton_llm_journal
                    WHERE scope_ref = %s AND request_hash = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (scope_ref, request_hash),
                )
            else:
                await cur.execute(
                    """
                    SELECT *
                    FROM dalton_llm_journal
                    WHERE scope_ref = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (scope_ref,),
                )
            row = await cur.fetchone()
            return dict(row) if row else None

    if conn is not None:
        return await _do_fetch(conn)
    async with get_db_connection() as db_conn:
        return await _do_fetch(db_conn)


get_journal_by_scope = fetch_journal_by_scope


async def inspect_journal_state(
    invocation_id: str,
    *,
    request_hash: str,
    stale_after_s: int = 900,
    resend_indeterminate: bool = False,
    conn: Any = None,
) -> tuple[str, dict[str, Any] | None]:
    """Inspect existing journal row before budget admission or claim.

    Returns `(action, row)` where `action` is one of:
    - `'proceed'`: no row exists, or row is `'failed'`, or row is `'indeterminate'` with `resend_indeterminate=True`
    - `'done'`: row is already `'done'` (replay cached response)
    - `'in_flight'`: row is `'pending'` and within `stale_after_s`
    - `'indeterminate'`: row is `'indeterminate'` (or stale `'pending'` transitioned to `'indeterminate'`) and `resend_indeterminate=False`
    - `'conflict'`: row exists with a different `request_hash`
    """
    async def _do_inspect(c: Any) -> tuple[str, dict[str, Any] | None]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM dalton_llm_journal WHERE invocation_id = %s",
                (invocation_id,),
            )
            existing = await cur.fetchone()
            if not existing:
                return "proceed", None

            row = dict(existing)
            if row.get("request_hash") and str(row["request_hash"]) != str(request_hash):
                return "conflict", row

            state = str(row.get("state") or "")
            if state == "done":
                if not is_journal_response_replayable(row.get("response")):
                    return "proceed", row
                return "done", row

            if state == "pending":
                if _is_stale(row.get("claimed_at"), stale_after_s):
                    if resend_indeterminate:
                        return "proceed", row
                    await cur.execute(
                        """
                        UPDATE dalton_llm_journal
                        SET state = 'indeterminate',
                            error_kind = COALESCE(error_kind, 'crash'),
                            completed_at = COALESCE(completed_at, NOW())
                        WHERE invocation_id = %s AND state = 'pending'
                        RETURNING *
                        """,
                        (invocation_id,),
                    )
                    updated = await cur.fetchone()
                    return "indeterminate", dict(updated) if updated else {**row, "state": "indeterminate", "error_kind": "crash"}
                return "in_flight", row

            if state == "indeterminate":
                if resend_indeterminate:
                    return "proceed", row
                return "indeterminate", row

            # state == 'failed'
            return "proceed", row

    if conn is not None:
        return await _do_inspect(conn)
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            return await _do_inspect(db_conn)


async def claim_journal_invocation(
    *,
    invocation_id: str,
    user_id: str | None,
    scope_ref: str,
    purpose: str,
    model_key: str,
    provider: str | None,
    billing_type: str | None,
    request_hash: str,
    stale_after_s: int = 900,
    resend_indeterminate: bool = False,
    conn: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Atomically claim or evaluate an invocation in `dalton_llm_journal` (P0N.7 §3).

    Returns `(action, row)` where `action` is one of:
    - `'claimed'`: caller now owns the `'pending'` invocation and must send the request
    - `'done'`: already completed; caller should replay `row['response']`
    - `'in_flight'`: another worker is currently executing this invocation
    - `'indeterminate'`: prior attempt crashed/timed out; do not resend unless `resend_indeterminate=True`
    - `'conflict'`: `invocation_id` exists with a mismatched `request_hash`
    """
    async def _do_claim(c: Any) -> tuple[str, dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_llm_journal (
                    invocation_id, user_id, scope_ref, purpose, model_key,
                    provider, billing_type, request_hash, state, attempt, claimed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', 1, NOW())
                ON CONFLICT (invocation_id) DO NOTHING
                RETURNING *
                """,
                (
                    invocation_id,
                    user_id,
                    scope_ref,
                    purpose,
                    model_key,
                    provider,
                    billing_type,
                    request_hash,
                ),
            )
            inserted = await cur.fetchone()
            if inserted:
                return "claimed", dict(inserted)

            await cur.execute(
                """
                SELECT *
                FROM dalton_llm_journal
                WHERE invocation_id = %s
                FOR UPDATE
                """,
                (invocation_id,),
            )
            existing_raw = await cur.fetchone()
            if not existing_raw:
                return "claimed", {
                    "invocation_id": invocation_id,
                    "user_id": user_id,
                    "scope_ref": scope_ref,
                    "purpose": purpose,
                    "model_key": model_key,
                    "provider": provider,
                    "billing_type": billing_type,
                    "request_hash": request_hash,
                    "state": "pending",
                    "attempt": 1,
                }

            existing = dict(existing_raw)
            if existing.get("request_hash") and str(existing["request_hash"]) != str(request_hash):
                return "conflict", existing

            state = str(existing.get("state") or "")
            if state == "done" and is_journal_response_replayable(existing.get("response")):
                return "done", existing

            if state == "pending":
                if not _is_stale(existing.get("claimed_at"), stale_after_s):
                    return "in_flight", existing
                if not resend_indeterminate:
                    await cur.execute(
                        """
                        UPDATE dalton_llm_journal
                        SET state = 'indeterminate',
                            error_kind = COALESCE(error_kind, 'crash'),
                            completed_at = COALESCE(completed_at, NOW())
                        WHERE invocation_id = %s
                        RETURNING *
                        """,
                        (invocation_id,),
                    )
                    upd = await cur.fetchone()
                    return "indeterminate", dict(upd) if upd else {
                        **existing,
                        "state": "indeterminate",
                        "error_kind": "crash",
                    }

            if state == "indeterminate" and not resend_indeterminate:
                return "indeterminate", existing

            # Either state == 'failed', response was pruned on 'done', or resend_indeterminate=True
            await cur.execute(
                """
                UPDATE dalton_llm_journal
                SET state = 'pending',
                    attempt = attempt + 1,
                    claimed_at = NOW(),
                    completed_at = NULL,
                    error_kind = NULL,
                    error_text = NULL,
                    model_key = %s,
                    provider = COALESCE(%s, provider),
                    billing_type = COALESCE(%s, billing_type)
                WHERE invocation_id = %s
                RETURNING *
                """,
                (model_key, provider, billing_type, invocation_id),
            )
            reclaimed = await cur.fetchone()
            return "claimed", dict(reclaimed) if reclaimed else {
                **existing,
                "state": "pending",
                "attempt": int(existing.get("attempt") or 1) + 1,
            }

    if conn is not None:
        return await _do_claim(conn)
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            return await _do_claim(db_conn)


async def complete_journal_done(
    invocation_id: str,
    *,
    response: dict[str, Any] | None,
    usage: dict[str, Any] | None = None,
    cash_usd: Decimal = Decimal("0"),
    notional_usd: Decimal = Decimal("0"),
    provider: str | None = None,
    billing_type: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Transition a journal row to `state='done'` with response (or NULL when `persist_raw=False`), usage, and costs."""
    resp_json = (
        json.dumps(response, ensure_ascii=False, default=str)
        if response is not None
        else None
    )
    usage_json = json.dumps(usage or {}, ensure_ascii=False, default=str)

    async def _do_update(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_llm_journal
                SET state = 'done',
                    completed_at = NOW(),
                    response = %s::jsonb,
                    usage = %s::jsonb,
                    cash_usd = %s,
                    notional_usd = %s,
                    provider = COALESCE(%s, provider),
                    billing_type = COALESCE(%s, billing_type),
                    error_kind = NULL,
                    error_text = NULL
                WHERE invocation_id = %s
                RETURNING *
                """,
                (
                    resp_json,
                    usage_json,
                    cash_usd,
                    notional_usd,
                    provider,
                    billing_type,
                    invocation_id,
                ),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    if conn is not None:
        return await _do_update(conn)
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            return await _do_update(db_conn)


mark_journal_done = complete_journal_done


async def record_journal_replay(invocation_id: str, *, conn: Any = None) -> None:
    """Increment `usage.replay_count` and stamp `usage.last_replayed_at` (ISO 8601 UTC) when served from cache."""
    async def _do_inc(c: Any) -> None:
        async with c.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_llm_journal
                SET usage = COALESCE(usage, '{}'::jsonb) || jsonb_build_object(
                    'replay_count', COALESCE((usage->>'replay_count')::int, 0) + 1,
                    'last_replayed_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                )
                WHERE invocation_id = %s AND state = 'done'
                """,
                (invocation_id,),
            )

    if conn is not None:
        await _do_inc(conn)
        return
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            await _do_inc(db_conn)


async def complete_journal_failed(
    invocation_id: str,
    *,
    error_kind: str,
    error_text: str | BaseException | None,
    state: str = "failed",
    cash_usd: Decimal | None = None,
    notional_usd: Decimal | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Transition a journal row to `state='failed'` or `state='indeterminate'`."""
    if state not in ("failed", "indeterminate"):
        state = "indeterminate"
    safe_err = sanitize_error_text(error_text, max_len=300)

    async def _do_update(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_llm_journal
                SET state = %s,
                    completed_at = NOW(),
                    error_kind = %s,
                    error_text = %s,
                    cash_usd = COALESCE(%s, cash_usd),
                    notional_usd = COALESCE(%s, notional_usd)
                WHERE invocation_id = %s
                RETURNING *
                """,
                (state, error_kind, safe_err, cash_usd, notional_usd, invocation_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    if conn is not None:
        return await _do_update(conn)
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            return await _do_update(db_conn)


mark_journal_failed = complete_journal_failed


async def fetch_journal_rows_in_window(
    day_start_utc: datetime,
    day_end_utc: datetime,
    *,
    user_id: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch journal rows created within `[day_start_utc, day_end_utc)` for budget and provider summaries.

    P0H §5 B2: Errors are logged at ERROR level and re-raised so budget reconciliation fails closed.
    """
    async def _do_fetch(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    invocation_id, user_id, scope_ref, purpose, model_key,
                    provider, billing_type, state, attempt, usage,
                    cash_usd, notional_usd, error_kind, created_at, completed_at
                FROM dalton_llm_journal
                WHERE created_at >= %s
                  AND created_at < %s
                  AND (%s::text IS NULL OR user_id = %s OR user_id IS NULL)
                ORDER BY created_at DESC
                """,
                (day_start_utc, day_end_utc, user_id, user_id),
            )
            return [dict(r) for r in await cur.fetchall()]

    try:
        if conn is not None:
            return await _do_fetch(conn)
        async with get_db_connection() as db_conn:
            return await _do_fetch(db_conn)
    except Exception as exc:
        logger.error("[DaltonLLMJournal] fetch_journal_rows_in_window failed: %s", exc)
        raise


async def prune_expired_journal_responses(
    conn_or_cur: Any = None,
    *,
    retention_days: int = 30,
) -> int:
    """Clear `response` JSONB on `state='done'` journal rows older than `retention_days` (P0H §5 B1).

    Preserves all metadata and accounting columns (`invocation_id`, `model_key`, `provider`,
    `billing_type`, `request_hash`, `state`, `usage`, `cash_usd`, `notional_usd`, `created_at`).
    """
    days = max(1, int(retention_days))
    query = """
        UPDATE dalton_llm_journal
        SET response = NULL
        WHERE state = 'done'
          AND response IS NOT NULL
          AND created_at < NOW() - make_interval(days => %s)
    """

    async def _run_on_cur(cur: Any) -> int:
        await cur.execute(query, (days,))
        return int(cur.rowcount or 0)

    if conn_or_cur is not None:
        if hasattr(conn_or_cur, "cursor"):
            async with conn_or_cur.cursor() as cur:
                return await _run_on_cur(cur)
        return await _run_on_cur(conn_or_cur)

    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                return await _run_on_cur(cur)

