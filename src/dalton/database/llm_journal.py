"""Raw psycopg3 SQL queries for Dalton one-shot LLM call journal (`dalton_llm_journal`, P0N.7, G1.3)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from psycopg.rows import dict_row

from src.dalton.credentials import (  # DALTON-HOOK(team-credentials): stamp credential holder in journal usage while user_id stays caller
    credential_user_id,
    is_team_credential_sharing_enabled,
)
from src.dalton.database.provider_events import sanitize_error_text
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_REPORTED_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_5m_tokens",
    "cache_1h_tokens",
)


def has_reported_token_usage(usage: Any) -> bool:
    """Return True when ``usage`` carries positive provider-reported token counts."""
    if not isinstance(usage, Mapping):
        return False
    raw_calls = usage.get("per_call_usages")
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if isinstance(item, Mapping):
                for key in _REPORTED_TOKEN_KEYS:
                    val = item.get(key)
                    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                        return True
    for key in _REPORTED_TOKEN_KEYS:
        val = usage.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return True
    return False


def count_unknown_usage_in_row(row: Mapping[str, Any]) -> int:
    """Return the number of unbilled/unknown-usage upstream requests represented by a
    journal or journal-attempt row (0 when every sent call reported usage or never ran)."""
    if not isinstance(row, Mapping):
        return 0
    explicit_count = row.get("unknown_usage_calls")
    if explicit_count is not None and not isinstance(explicit_count, bool):
        try:
            return max(0, int(explicit_count))
        except (TypeError, ValueError):
            pass
    explicit_known = row.get("usage_known")
    if isinstance(explicit_known, bool):
        return 0 if explicit_known else 1

    usage = row.get("usage")
    if isinstance(usage, Mapping):
        u_count = usage.get("unknown_usage_calls")
        if u_count is not None and not isinstance(u_count, bool):
            try:
                return max(0, int(u_count))
            except (TypeError, ValueError):
                pass
        u_known = usage.get("usage_known")
        if isinstance(u_known, bool):
            return 0 if u_known else 1

    if str(row.get("state") or "") != "indeterminate":
        return 0
    if has_reported_token_usage(usage):
        return 0
    try:
        if Decimal(str(row.get("cash_usd") or "0")) > Decimal("0"):
            return 0
        if Decimal(str(row.get("notional_usd") or "0")) > Decimal("0"):
            return 0
    except Exception:
        pass
    return 1


def is_unknown_usage_journal_row(row: Mapping[str, Any]) -> bool:
    """Return True when a journal or journal-attempt row carries at least one
    upstream request whose provider usage could not be determined (timeout, 5xx,
    stream cut, or crash before usage was reported)."""
    return count_unknown_usage_in_row(row) > 0


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


def _resend_capped(row: Mapping[str, Any] | None, resend_max_attempts: int | None) -> bool:
    """True when the cumulative re-send cap for this invocation is reached
    (R2a fix1): the original send plus the allowed re-sends have all ended
    uncertain, so another send would be an unbounded retry of a call the
    provider may already have billed."""
    if resend_max_attempts is None or row is None:
        return False
    try:
        attempt = int(row.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    return attempt >= max(resend_max_attempts, 1)


async def inspect_journal_state(
    invocation_id: str,
    *,
    request_hash: str,
    stale_after_s: int = 900,
    resend_indeterminate: bool = False,
    resend_max_attempts: int | None = None,
    conn: Any = None,
) -> tuple[str, dict[str, Any] | None]:
    """Inspect existing journal row before budget admission or claim.

    Returns `(action, row)` where `action` is one of:
    - `'proceed'`: no row exists, or row is `'failed'`, or row is `'indeterminate'` with `resend_indeterminate=True` and the attempt cap not reached (`resend_max_attempts`)
    - `'done'`: row is already `'done'` (replay cached response)
    - `'in_flight'`: row is `'pending'` and within `stale_after_s`
    - `'indeterminate'`: row is `'indeterminate'` (or stale `'pending'` transitioned to `'indeterminate'`) and `resend_indeterminate=False` or the attempt cap is reached
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
                    if resend_indeterminate and not _resend_capped(row, resend_max_attempts):
                        return "proceed", row
                    await cur.execute(
                        """
                        WITH updated_journal AS (
                            UPDATE dalton_llm_journal
                            SET state = 'indeterminate',
                                error_kind = COALESCE(error_kind, 'crash'),
                                completed_at = COALESCE(completed_at, NOW())
                            WHERE invocation_id = %s AND state = 'pending'
                            RETURNING *
                        ),
                        recorded_crash AS (
                            INSERT INTO dalton_llm_journal_attempts (
                                invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                                provider, billing_type, outcome, error_kind, usage,
                                cash_usd, notional_usd, usage_known, unknown_usage_calls, occurred_at
                            )
                            SELECT
                                u.invocation_id, u.attempt, u.user_id, u.scope_ref, u.purpose, u.model_key,
                                u.provider, u.billing_type, 'indeterminate', COALESCE(u.error_kind, 'crash'),
                                jsonb_build_object('usage_known', false, 'unknown_usage_calls', 1),
                                0, 0, FALSE, 1, COALESCE(u.claimed_at, NOW())
                            FROM updated_journal u
                            ON CONFLICT (invocation_id, attempt) DO NOTHING
                        )
                        SELECT * FROM updated_journal
                        """,
                        (invocation_id,),
                    )
                    updated = await cur.fetchone()
                    return "indeterminate", dict(updated) if updated else {**row, "state": "indeterminate", "error_kind": "crash"}
                return "in_flight", row

            if state == "indeterminate":
                if resend_indeterminate and not _resend_capped(row, resend_max_attempts):
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
    resend_max_attempts: int | None = None,
    stamp_credential_holder: bool = True,
    conn: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Atomically claim or evaluate an invocation in `dalton_llm_journal` (P0N.7 §3).

    Returns `(action, row)` where `action` is one of:
    - `'claimed'`: caller now owns the `'pending'` invocation and must send the request
    - `'done'`: already completed; caller should replay `row['response']`
    - `'in_flight'`: another worker is currently executing this invocation
    - `'indeterminate'`: prior attempt crashed/timed out; do not resend unless `resend_indeterminate=True` and the attempt cap (`resend_max_attempts`) is not reached
    - `'conflict'`: `invocation_id` exists with a mismatched `request_hash`
    """
    # DALTON-HOOK(team-credentials): keep user_id as caller and stamp credential_user_id in usage JSONB only when sharing is enabled and not deployment-keyed
    sharing_enabled = is_team_credential_sharing_enabled()
    initial_usage = (
        {"credential_user_id": credential_user_id(str(user_id))}
        if stamp_credential_holder and sharing_enabled and user_id
        else None
    )

    async def _do_claim(c: Any) -> tuple[str, dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            if initial_usage is not None:
                initial_usage_json = json.dumps(initial_usage, ensure_ascii=False, default=str)
                await cur.execute(
                    """
                    INSERT INTO dalton_llm_journal (
                        invocation_id, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, request_hash, usage, state, attempt, claimed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending', 1, NOW())
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
                        initial_usage_json,
                    ),
                )
            else:
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
                fallback_row: dict[str, Any] = {
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
                if initial_usage is not None:
                    fallback_row["usage"] = initial_usage
                return "claimed", fallback_row

            existing = dict(existing_raw)
            if existing.get("request_hash") and str(existing["request_hash"]) != str(request_hash):
                return "conflict", existing

            state = str(existing.get("state") or "")
            if state == "done" and is_journal_response_replayable(existing.get("response")):
                return "done", existing

            if state == "pending":
                if not _is_stale(existing.get("claimed_at"), stale_after_s):
                    return "in_flight", existing
                if not (
                    resend_indeterminate
                    and not _resend_capped(existing, resend_max_attempts)
                ):
                    await cur.execute(
                        """
                        WITH updated_journal AS (
                            UPDATE dalton_llm_journal
                            SET state = 'indeterminate',
                                error_kind = COALESCE(error_kind, 'crash'),
                                completed_at = COALESCE(completed_at, NOW())
                            WHERE invocation_id = %s
                            RETURNING *
                        ),
                        recorded_crash AS (
                            INSERT INTO dalton_llm_journal_attempts (
                                invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                                provider, billing_type, outcome, error_kind, usage,
                                cash_usd, notional_usd, usage_known, unknown_usage_calls, occurred_at
                            )
                            SELECT
                                u.invocation_id, u.attempt, u.user_id, u.scope_ref, u.purpose, u.model_key,
                                u.provider, u.billing_type, 'indeterminate', COALESCE(u.error_kind, 'crash'),
                                jsonb_build_object('usage_known', false, 'unknown_usage_calls', 1),
                                0, 0, FALSE, 1, COALESCE(u.claimed_at, NOW())
                            FROM updated_journal u
                            ON CONFLICT (invocation_id, attempt) DO NOTHING
                        )
                        SELECT * FROM updated_journal
                        """,
                        (invocation_id,),
                    )
                    upd = await cur.fetchone()
                    return "indeterminate", dict(upd) if upd else {
                        **existing,
                        "state": "indeterminate",
                        "error_kind": "crash",
                    }

            if state == "indeterminate" and not (
                resend_indeterminate
                and not _resend_capped(existing, resend_max_attempts)
            ):
                return "indeterminate", existing

            # Either state == 'failed', response was pruned on 'done', or resend_indeterminate=True.
            # Preserve prior accumulated token usage while stripping only `credential_user_id`
            # when resending under deployment credentials (`stamp_credential_holder=False`).
            # If reclaiming a stale `'pending'` row, also record a `'crash'` placeholder for
            # the superseded attempt so its unknown usage is preserved unless that worker
            # returns late and upgrades its own `(invocation_id, attempt)` row.
            usage_reset_sql = (
                ", usage = CASE WHEN usage IS NULL THEN NULL ELSE usage - 'credential_user_id' END"
                if not stamp_credential_holder
                else ""
            )
            await cur.execute(
                f"""
                WITH prev AS (
                    SELECT invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                           provider, billing_type, state, claimed_at
                    FROM dalton_llm_journal
                    WHERE invocation_id = %s
                ),
                recorded_stale AS (
                    INSERT INTO dalton_llm_journal_attempts (
                        invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, outcome, error_kind, usage,
                        cash_usd, notional_usd, usage_known, unknown_usage_calls, occurred_at
                    )
                    SELECT
                        p.invocation_id, p.attempt, p.user_id, p.scope_ref, p.purpose, p.model_key,
                        p.provider, p.billing_type, 'indeterminate', 'crash',
                        jsonb_build_object('usage_known', false, 'unknown_usage_calls', 1),
                        0, 0, FALSE, 1, COALESCE(p.claimed_at, NOW())
                    FROM prev p
                    WHERE p.state = 'pending'
                    ON CONFLICT (invocation_id, attempt) DO NOTHING
                )
                UPDATE dalton_llm_journal
                SET state = 'pending',
                    attempt = attempt + 1,
                    claimed_at = NOW(),
                    completed_at = NULL,
                    error_kind = NULL,
                    error_text = NULL,
                    model_key = %s,
                    provider = COALESCE(%s, provider),
                    billing_type = COALESCE(%s, billing_type){usage_reset_sql}
                WHERE invocation_id = %s
                RETURNING *
                """,
                (invocation_id, model_key, provider, billing_type, invocation_id),
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
    attempt: int,
    response: dict[str, Any] | None,
    usage: dict[str, Any] | None = None,
    cash_usd: Decimal = Decimal("0"),
    notional_usd: Decimal = Decimal("0"),
    provider: str | None = None,
    billing_type: str | None = None,
    stamp_credential_holder: bool = True,
    attempt_usage: dict[str, Any] | None = None,
    unknown_usage_calls: int = 0,
    occurred_at: datetime | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Transition a journal row to `state='done'` with response (or NULL when `persist_raw=False`), usage, and costs.

    R6: only the attempt that claimed the row may write back the parent row's
    `state` and `response`; a stale worker returning after a newer attempt took
    over finds no matching parent row, logs a warning, and returns None instead
    of clobbering the newer attempt's state or response. Regardless of takeover,
    its own `(invocation_id, attempt)` billing record is uniquely settled in
    `dalton_llm_journal_attempts` so completed provider charges are never lost
    or double-counted and are attributed to that attempt's `occurred_at`.
    """
    resp_json = (
        json.dumps(response, ensure_ascii=False, default=str)
        if response is not None
        else None
    )
    usage_json = json.dumps(usage or {}, ensure_ascii=False, default=str)
    eff_attempt_usage = attempt_usage if attempt_usage is not None else (usage or {})
    attempt_usage_json = json.dumps(eff_attempt_usage, ensure_ascii=False, default=str)
    eff_unknown_calls = max(0, int(unknown_usage_calls or 0))
    eff_usage_known = eff_unknown_calls == 0

    usage_clause = (
        "COALESCE(usage, '{}'::jsonb) || %s::jsonb"
        if (stamp_credential_holder and is_team_credential_sharing_enabled())
        else "%s::jsonb"
    )

    async def _do_update(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH updated_journal AS (
                    UPDATE dalton_llm_journal
                    SET state = 'done',
                        completed_at = NOW(),
                        response = %s::jsonb,
                        usage = {usage_clause},
                        cash_usd = COALESCE(cash_usd, 0) + %s,
                        notional_usd = COALESCE(notional_usd, 0) + %s,
                        provider = COALESCE(%s, provider),
                        billing_type = COALESCE(%s, billing_type),
                        error_kind = NULL,
                        error_text = NULL
                    -- Only the attempt that claimed the row writes it back. A stale
                    -- mark (pending -> indeterminate) keeps the attempt number, so
                    -- that attempt may still settle it; a takeover bumps the attempt
                    -- and shuts the older worker out of updating state/response while
                    -- `recorded_attempt` below still settles its unique attempt cost.
                    WHERE invocation_id = %s AND attempt = %s
                      AND state IN ('pending', 'indeterminate')
                    RETURNING *
                ),
                recorded_attempt AS (
                    INSERT INTO dalton_llm_journal_attempts (
                        invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, outcome, error_kind, usage,
                        cash_usd, notional_usd, usage_known, unknown_usage_calls, occurred_at
                    )
                    SELECT
                        j.invocation_id, v.att, j.user_id, j.scope_ref, j.purpose, j.model_key,
                        COALESCE(%s::text, j.provider), COALESCE(%s::text, j.billing_type),
                        'done', NULL, %s::jsonb,
                        %s::numeric, %s::numeric, %s::boolean, %s::int, COALESCE(%s::timestamptz, NOW())
                    FROM dalton_llm_journal j
                    JOIN (VALUES (%s::text, %s::int)) AS v(inv_id, att)
                      ON j.invocation_id = v.inv_id
                    ON CONFLICT (invocation_id, attempt) DO UPDATE
                    SET outcome = EXCLUDED.outcome,
                        error_kind = EXCLUDED.error_kind,
                        usage = EXCLUDED.usage,
                        cash_usd = EXCLUDED.cash_usd,
                        notional_usd = EXCLUDED.notional_usd,
                        usage_known = EXCLUDED.usage_known,
                        unknown_usage_calls = EXCLUDED.unknown_usage_calls,
                        occurred_at = EXCLUDED.occurred_at
                    WHERE dalton_llm_journal_attempts.error_kind = 'crash'
                      AND dalton_llm_journal_attempts.usage_known = FALSE
                      AND dalton_llm_journal_attempts.cash_usd = 0
                      AND dalton_llm_journal_attempts.notional_usd = 0
                )
                SELECT * FROM updated_journal
                """,
                (
                    resp_json,
                    usage_json,
                    cash_usd,
                    notional_usd,
                    provider,
                    billing_type,
                    invocation_id,
                    int(attempt),
                    provider,
                    billing_type,
                    attempt_usage_json,
                    cash_usd,
                    notional_usd,
                    eff_usage_known,
                    eff_unknown_calls,
                    occurred_at,
                    invocation_id,
                    int(attempt),
                ),
            )
            row = await cur.fetchone()
            if row is None:
                logger.warning(
                    "[DaltonLLMJournal] complete_journal_done refused for %s attempt=%s "
                    "(row superseded by a newer attempt or already settled)",
                    invocation_id,
                    attempt,
                )
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
    attempt: int,
    error_kind: str,
    error_text: str | BaseException | None,
    state: str = "failed",
    usage: dict[str, Any] | None = None,
    cash_usd: Decimal | None = None,
    notional_usd: Decimal | None = None,
    stamp_credential_holder: bool = True,
    attempt_usage: dict[str, Any] | None = None,
    usage_known: bool | None = None,
    unknown_usage_calls: int | None = None,
    occurred_at: datetime | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Transition a journal row to `state='failed'` or `state='indeterminate'`.

    R6: only the attempt that claimed the row may write back the parent row's
    `state`; a stale worker returning after a newer attempt took over finds no
    matching parent row, logs a warning, and returns None instead of clobbering
    the newer attempt's state. Regardless of takeover, its own
    `(invocation_id, attempt)` billing/unknown-usage record is uniquely settled
    in `dalton_llm_journal_attempts`.
    """
    if state not in ("failed", "indeterminate"):
        state = "indeterminate"
    safe_err = sanitize_error_text(error_text, max_len=300)

    usage_sql = ""
    usage_params: tuple[Any, ...] = ()
    if usage is not None:
        usage_json = json.dumps(usage, ensure_ascii=False, default=str)
        usage_clause = (
            "COALESCE(usage, '{}'::jsonb) || %s::jsonb"
            if (stamp_credential_holder and is_team_credential_sharing_enabled())
            else "%s::jsonb"
        )
        usage_sql = f",\n                        usage = {usage_clause}"
        usage_params = (usage_json,)

    eff_attempt_usage = attempt_usage if attempt_usage is not None else usage
    attempt_usage_json = (
        json.dumps(eff_attempt_usage, ensure_ascii=False, default=str)
        if eff_attempt_usage is not None
        else None
    )
    if unknown_usage_calls is not None:
        eff_unknown_calls = max(0, int(unknown_usage_calls))
    elif isinstance(eff_attempt_usage, Mapping) and eff_attempt_usage.get("unknown_usage_calls") is not None:
        eff_unknown_calls = max(0, int(eff_attempt_usage["unknown_usage_calls"]))
    elif usage_known is False or (
        isinstance(eff_attempt_usage, Mapping) and eff_attempt_usage.get("usage_known") is False
    ):
        eff_unknown_calls = 1
    elif usage_known is True or (
        isinstance(eff_attempt_usage, Mapping) and eff_attempt_usage.get("usage_known") is True
    ):
        eff_unknown_calls = 0
    else:
        eff_unknown_calls = (
            1
            if state == "indeterminate"
            and not has_reported_token_usage(eff_attempt_usage)
            and (cash_usd or Decimal("0")) <= Decimal("0")
            and (notional_usd or Decimal("0")) <= Decimal("0")
            else 0
        )
    if usage_known is False and eff_unknown_calls == 0:
        eff_unknown_calls = 1
    eff_usage_known = eff_unknown_calls == 0

    async def _do_update(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH updated_journal AS (
                    UPDATE dalton_llm_journal
                    SET state = %s,
                        completed_at = NOW(),
                        error_kind = %s,
                        error_text = %s,
                        cash_usd = COALESCE(cash_usd, 0) + COALESCE(%s, 0),
                        notional_usd = COALESCE(notional_usd, 0) + COALESCE(%s, 0){usage_sql}
                    -- Only the attempt that claimed the row writes it back. A stale
                    -- mark (pending -> indeterminate) keeps the attempt number, so
                    -- that attempt may still settle it; a takeover bumps the attempt
                    -- and shuts the older worker out of updating state while
                    -- `recorded_attempt` below still settles its unique attempt cost.
                    WHERE invocation_id = %s AND attempt = %s
                      AND state IN ('pending', 'indeterminate')
                    RETURNING *
                ),
                recorded_attempt AS (
                    INSERT INTO dalton_llm_journal_attempts (
                        invocation_id, attempt, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, outcome, error_kind, usage,
                        cash_usd, notional_usd, usage_known, unknown_usage_calls, occurred_at
                    )
                    SELECT
                        j.invocation_id, v.att, j.user_id, j.scope_ref, j.purpose, j.model_key,
                        j.provider, j.billing_type,
                        %s::text, %s::text, %s::jsonb,
                        COALESCE(%s::numeric, 0), COALESCE(%s::numeric, 0),
                        %s::boolean, %s::int, COALESCE(%s::timestamptz, NOW())
                    FROM dalton_llm_journal j
                    JOIN (VALUES (%s::text, %s::int)) AS v(inv_id, att)
                      ON j.invocation_id = v.inv_id
                    ON CONFLICT (invocation_id, attempt) DO UPDATE
                    SET outcome = EXCLUDED.outcome,
                        error_kind = EXCLUDED.error_kind,
                        usage = EXCLUDED.usage,
                        cash_usd = EXCLUDED.cash_usd,
                        notional_usd = EXCLUDED.notional_usd,
                        usage_known = EXCLUDED.usage_known,
                        unknown_usage_calls = EXCLUDED.unknown_usage_calls,
                        occurred_at = EXCLUDED.occurred_at
                    WHERE dalton_llm_journal_attempts.error_kind = 'crash'
                      AND dalton_llm_journal_attempts.usage_known = FALSE
                      AND dalton_llm_journal_attempts.cash_usd = 0
                      AND dalton_llm_journal_attempts.notional_usd = 0
                )
                SELECT * FROM updated_journal
                """,
                (
                    state,
                    error_kind,
                    safe_err,
                    cash_usd,
                    notional_usd,
                    *usage_params,
                    invocation_id,
                    int(attempt),
                    state,
                    error_kind,
                    attempt_usage_json,
                    cash_usd,
                    notional_usd,
                    eff_usage_known,
                    eff_unknown_calls,
                    occurred_at,
                    invocation_id,
                    int(attempt),
                ),
            )
            row = await cur.fetchone()
            if row is None:
                logger.warning(
                    "[DaltonLLMJournal] complete_journal_failed refused for %s attempt=%s "
                    "(row superseded by a newer attempt or already settled)",
                    invocation_id,
                    attempt,
                )
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
    """Fetch per-attempt journal rows occurring within `[day_start_utc, day_end_utc)`
    (with legacy fallback for `dalton_llm_journal` rows without attempt records)
    for budget, provider, and unknown-usage summaries.

    P0H §5 B2: Errors are logged at ERROR level and re-raised so budget reconciliation fails closed.
    """
    async def _do_fetch(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    invocation_id, user_id, scope_ref, purpose, model_key,
                    provider, billing_type, state, attempt, usage,
                    cash_usd, notional_usd, error_kind, usage_known,
                    unknown_usage_calls, created_at, completed_at
                FROM (
                    SELECT
                        a.invocation_id, a.user_id, a.scope_ref, a.purpose, a.model_key,
                        a.provider, a.billing_type, a.outcome AS state, a.attempt, a.usage,
                        a.cash_usd, a.notional_usd, a.error_kind, a.usage_known,
                        a.unknown_usage_calls, a.occurred_at AS created_at, a.occurred_at AS completed_at
                    FROM dalton_llm_journal_attempts a
                    WHERE a.occurred_at >= %s
                      AND a.occurred_at < %s
                      AND (%s::text IS NULL OR a.user_id = %s OR a.user_id IS NULL)
                    UNION ALL
                    SELECT
                        j.invocation_id, j.user_id, j.scope_ref, j.purpose, j.model_key,
                        j.provider, j.billing_type, j.state, j.attempt, j.usage,
                        j.cash_usd, j.notional_usd, j.error_kind, NULL::boolean AS usage_known,
                        NULL::int AS unknown_usage_calls, j.created_at, j.completed_at
                    FROM dalton_llm_journal j
                    WHERE j.created_at >= %s
                      AND j.created_at < %s
                      AND (%s::text IS NULL OR j.user_id = %s OR j.user_id IS NULL)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM dalton_llm_journal_attempts a
                          WHERE a.invocation_id = j.invocation_id
                      )
                ) AS rows
                ORDER BY created_at DESC
                """,
                (
                    day_start_utc,
                    day_end_utc,
                    user_id,
                    user_id,
                    day_start_utc,
                    day_end_utc,
                    user_id,
                    user_id,
                ),
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

