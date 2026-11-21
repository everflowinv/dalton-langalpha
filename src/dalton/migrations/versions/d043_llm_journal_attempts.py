"""Dalton d043: per-request LLM journal billing ledger (`dalton_llm_journal_attempts`).

Records each individual upstream request of a `dalton_llm_journal` invocation keyed by
`(invocation_id, attempt, call_seq)` with its own send timestamp `occurred_at`, `usage`,
`cash_usd`, `notional_usd`, `usage_known`, and `unknown_usage_calls`.

This enables:
- attributing daily cash budget spend to the day each upstream request was actually sent
  (rather than settlement time or an earlier attempt's `created_at` on cross-day retries/resends),
- settling a stale attempt's known cost even after a newer attempt took over the
  parent journal row without clobbering the newer attempt's state or response,
- preserving unknown-usage records from earlier timed-out/stream-cut attempts
  even after a subsequent resend succeeds,
- rolling back cleanly to d042 without losing late-settled attempt costs or usage by
  aggregating per-request ledger rows back into `dalton_llm_journal` before drop.

Revision ID: d043
Revises: d042
Create Date: 2026-11-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d043"
down_revision: Union[str, None] = "d042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL_BATCH_SIZE = 1000

_USAGE_KNOWN_EXPR = """
CASE
    WHEN j.state IN ('indeterminate', 'pending') AND (
        j.usage->>'usage_known' = 'false'
        OR (
            j.usage->>'usage_known' IS DISTINCT FROM 'true'
            AND j.cash_usd <= 0
            AND j.notional_usd <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'input_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'output_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'total_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'total_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
        )
    ) THEN FALSE
    ELSE TRUE
END
""".strip()

_UNKNOWN_CALLS_EXPR = """
CASE
    WHEN (j.usage->>'unknown_usage_calls') ~ '^[0-9]+$'
        THEN (j.usage->>'unknown_usage_calls')::int
    WHEN j.state IN ('indeterminate', 'pending') AND (
        j.usage->>'usage_known' = 'false'
        OR (
            j.usage->>'usage_known' IS DISTINCT FROM 'true'
            AND j.cash_usd <= 0
            AND j.notional_usd <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'input_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'output_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
            AND COALESCE(
                CASE WHEN (j.usage->>'total_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (j.usage->>'total_tokens')::numeric ELSE 0 END,
                0
            ) <= 0
        )
    ) THEN 1
    ELSE 0
END
""".strip()

_BACKFILL_ATTEMPT_EXPR = """
CASE
    WHEN j.state = 'pending' THEN GREATEST(1, j.attempt - 1)
    ELSE j.attempt
END
""".strip()

_BACKFILL_OUTCOME_EXPR = """
CASE
    WHEN j.state IN ('done', 'failed', 'indeterminate') THEN j.state
    ELSE 'indeterminate'
END
""".strip()

_ONLINE_BACKFILL_BATCH_SQL = f"""
WITH batch AS (
    SELECT
        j.invocation_id,
        j.attempt,
        j.user_id,
        j.scope_ref,
        j.purpose,
        j.model_key,
        j.provider,
        j.billing_type,
        j.state,
        j.error_kind,
        j.usage,
        j.cash_usd,
        j.notional_usd,
        j.created_at
    FROM dalton_llm_journal j
    WHERE j.invocation_id > :last_id
      AND (
          j.state IN ('done', 'indeterminate')
          OR j.cash_usd > 0
          OR j.notional_usd > 0
      )
    ORDER BY j.invocation_id
    LIMIT :batch_size
),
inserted AS (
    INSERT INTO dalton_llm_journal_attempts (
        invocation_id,
        attempt,
        call_seq,
        user_id,
        scope_ref,
        purpose,
        model_key,
        provider,
        billing_type,
        outcome,
        error_kind,
        usage,
        cash_usd,
        notional_usd,
        usage_known,
        unknown_usage_calls,
        occurred_at,
        created_at
    )
    SELECT
        j.invocation_id,
        {_BACKFILL_ATTEMPT_EXPR} AS attempt,
        1,
        j.user_id,
        j.scope_ref,
        j.purpose,
        j.model_key,
        j.provider,
        j.billing_type,
        {_BACKFILL_OUTCOME_EXPR} AS outcome,
        j.error_kind,
        j.usage,
        j.cash_usd,
        j.notional_usd,
        {_USAGE_KNOWN_EXPR} AS usage_known,
        {_UNKNOWN_CALLS_EXPR} AS unknown_usage_calls,
        j.created_at AS occurred_at,
        j.created_at
    FROM batch j
    WHERE NOT EXISTS (
        SELECT 1
        FROM dalton_llm_journal_attempts a
        WHERE a.invocation_id = j.invocation_id
    )
    ON CONFLICT (invocation_id, attempt, call_seq) DO NOTHING
)
SELECT MAX(invocation_id) AS last_id, COUNT(*) AS scanned
FROM batch
"""

_OFFLINE_BATCHED_BACKFILL_SQL = f"""
DO $$
DECLARE
    v_last_id TEXT := '';
    v_scanned INT := 0;
BEGIN
    LOOP
        WITH batch AS (
            SELECT
                j.invocation_id,
                j.attempt,
                j.user_id,
                j.scope_ref,
                j.purpose,
                j.model_key,
                j.provider,
                j.billing_type,
                j.state,
                j.error_kind,
                j.usage,
                j.cash_usd,
                j.notional_usd,
                j.created_at
            FROM dalton_llm_journal j
            WHERE j.invocation_id > v_last_id
              AND (
                  j.state IN ('done', 'indeterminate')
                  OR j.cash_usd > 0
                  OR j.notional_usd > 0
              )
            ORDER BY j.invocation_id
            LIMIT {_BACKFILL_BATCH_SIZE}
        ),
        inserted AS (
            INSERT INTO dalton_llm_journal_attempts (
                invocation_id,
                attempt,
                call_seq,
                user_id,
                scope_ref,
                purpose,
                model_key,
                provider,
                billing_type,
                outcome,
                error_kind,
                usage,
                cash_usd,
                notional_usd,
                usage_known,
                unknown_usage_calls,
                occurred_at,
                created_at
            )
            SELECT
                j.invocation_id,
                {_BACKFILL_ATTEMPT_EXPR} AS attempt,
                1,
                j.user_id,
                j.scope_ref,
                j.purpose,
                j.model_key,
                j.provider,
                j.billing_type,
                {_BACKFILL_OUTCOME_EXPR} AS outcome,
                j.error_kind,
                j.usage,
                j.cash_usd,
                j.notional_usd,
                {_USAGE_KNOWN_EXPR} AS usage_known,
                {_UNKNOWN_CALLS_EXPR} AS unknown_usage_calls,
                j.created_at AS occurred_at,
                j.created_at
            FROM batch j
            WHERE NOT EXISTS (
                SELECT 1
                FROM dalton_llm_journal_attempts a
                WHERE a.invocation_id = j.invocation_id
            )
            ON CONFLICT (invocation_id, attempt, call_seq) DO NOTHING
        )
        SELECT MAX(invocation_id), COUNT(*)
        INTO v_last_id, v_scanned
        FROM batch;

        COMMIT;

        EXIT WHEN v_scanned < {_BACKFILL_BATCH_SIZE} OR v_last_id IS NULL;
    END LOOP;
END $$
"""

_DOWNGRADE_ROLLUP_SQL = """
DO $$
BEGIN
    IF to_regclass('dalton_llm_journal_attempts') IS NOT NULL THEN
        WITH attempt_calls AS (
            SELECT
                a.invocation_id,
                a.attempt,
                a.call_seq,
                a.outcome,
                a.error_kind,
                a.usage,
                a.cash_usd,
                a.notional_usd,
                a.usage_known,
                a.unknown_usage_calls,
                a.occurred_at,
                CASE
                    WHEN a.usage IS NOT NULL
                         AND jsonb_typeof(a.usage->'per_call_usages') = 'array'
                         AND jsonb_array_length(a.usage->'per_call_usages') > 0
                    THEN a.usage->'per_call_usages'
                    WHEN a.usage IS NOT NULL AND (
                        COALESCE(CASE WHEN (a.usage->>'input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'input_tokens')::numeric ELSE 0 END, 0) > 0
                        OR COALESCE(CASE WHEN (a.usage->>'output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'output_tokens')::numeric ELSE 0 END, 0) > 0
                        OR COALESCE(CASE WHEN (a.usage->>'total_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'total_tokens')::numeric ELSE 0 END, 0) > 0
                        OR COALESCE(CASE WHEN (a.usage->>'cached_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cached_tokens')::numeric ELSE 0 END, 0) > 0
                        OR COALESCE(CASE WHEN (a.usage->>'cache_5m_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cache_5m_tokens')::numeric ELSE 0 END, 0) > 0
                        OR COALESCE(CASE WHEN (a.usage->>'cache_1h_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cache_1h_tokens')::numeric ELSE 0 END, 0) > 0
                    )
                    THEN jsonb_build_array(
                        jsonb_strip_nulls(jsonb_build_object(
                            'input_tokens', CASE WHEN (a.usage->>'input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'input_tokens')::numeric END,
                            'output_tokens', CASE WHEN (a.usage->>'output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'output_tokens')::numeric END,
                            'total_tokens', CASE WHEN (a.usage->>'total_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'total_tokens')::numeric END,
                            'cached_tokens', CASE WHEN (a.usage->>'cached_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cached_tokens')::numeric END,
                            'cache_5m_tokens', CASE WHEN (a.usage->>'cache_5m_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cache_5m_tokens')::numeric END,
                            'cache_1h_tokens', CASE WHEN (a.usage->>'cache_1h_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'cache_1h_tokens')::numeric END,
                            'reasoning_tokens', CASE WHEN (a.usage->>'reasoning_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'reasoning_tokens')::numeric END,
                            'audio_input_tokens', CASE WHEN (a.usage->>'audio_input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'audio_input_tokens')::numeric END,
                            'audio_output_tokens', CASE WHEN (a.usage->>'audio_output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (a.usage->>'audio_output_tokens')::numeric END
                        ))
                    )
                    ELSE '[]'::jsonb
                END AS extracted_calls
            FROM dalton_llm_journal_attempts a
        ),
        per_call_agg AS (
            SELECT
                ac.invocation_id,
                jsonb_agg(elem ORDER BY ac.attempt, ac.call_seq, ord) AS per_call_usages
            FROM attempt_calls ac
            CROSS JOIN LATERAL jsonb_array_elements(ac.extracted_calls) WITH ORDINALITY AS t(elem, ord)
            GROUP BY ac.invocation_id
        ),
        agg AS (
            SELECT
                ac.invocation_id,
                COALESCE(SUM(ac.cash_usd), 0) AS total_cash_usd,
                COALESCE(SUM(ac.notional_usd), 0) AS total_notional_usd,
                COALESCE(SUM(ac.unknown_usage_calls), 0) AS total_unknown_calls,
                BOOL_AND(ac.usage_known) AS all_usage_known,
                BOOL_OR(ac.outcome IN ('done', 'indeterminate')) AS has_billed_outcome,
                COALESCE(SUM(CASE WHEN (ac.usage->>'input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'input_tokens')::numeric ELSE 0 END), 0) AS sum_input_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'output_tokens')::numeric ELSE 0 END), 0) AS sum_output_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'total_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'total_tokens')::numeric ELSE 0 END), 0) AS sum_total_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'cached_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'cached_tokens')::numeric ELSE 0 END), 0) AS sum_cached_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'cache_5m_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'cache_5m_tokens')::numeric ELSE 0 END), 0) AS sum_cache_5m_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'cache_1h_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'cache_1h_tokens')::numeric ELSE 0 END), 0) AS sum_cache_1h_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'reasoning_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'reasoning_tokens')::numeric ELSE 0 END), 0) AS sum_reasoning_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'audio_input_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'audio_input_tokens')::numeric ELSE 0 END), 0) AS sum_audio_input_tokens,
                COALESCE(SUM(CASE WHEN (ac.usage->>'audio_output_tokens') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (ac.usage->>'audio_output_tokens')::numeric ELSE 0 END), 0) AS sum_audio_output_tokens,
                jsonb_agg(
                    jsonb_build_object(
                        'attempt', ac.attempt,
                        'call_seq', ac.call_seq,
                        'outcome', ac.outcome,
                        'error_kind', ac.error_kind,
                        'cash_usd', ac.cash_usd,
                        'notional_usd', ac.notional_usd,
                        'usage_known', ac.usage_known,
                        'unknown_usage_calls', ac.unknown_usage_calls,
                        'occurred_at', ac.occurred_at,
                        'usage', ac.usage
                    )
                    ORDER BY ac.attempt, ac.call_seq
                ) AS attempt_details
            FROM attempt_calls ac
            GROUP BY ac.invocation_id
        )
        UPDATE dalton_llm_journal j
        SET cash_usd = GREATEST(COALESCE(j.cash_usd, 0), agg.total_cash_usd),
            notional_usd = GREATEST(COALESCE(j.notional_usd, 0), agg.total_notional_usd),
            state = CASE
                WHEN j.state = 'done' THEN 'done'
                WHEN agg.total_cash_usd > 0
                     OR agg.total_notional_usd > 0
                     OR agg.total_unknown_calls > 0
                     OR agg.has_billed_outcome
                THEN 'indeterminate'
                ELSE j.state
            END,
            completed_at = CASE
                WHEN j.completed_at IS NOT NULL THEN j.completed_at
                WHEN agg.total_cash_usd > 0
                     OR agg.total_notional_usd > 0
                     OR agg.total_unknown_calls > 0
                     OR agg.has_billed_outcome
                THEN NOW()
                ELSE j.completed_at
            END,
            usage = COALESCE(j.usage, '{}'::jsonb)
                || jsonb_strip_nulls(jsonb_build_object(
                    'input_tokens', CASE WHEN agg.sum_input_tokens > 0 THEN agg.sum_input_tokens END,
                    'output_tokens', CASE WHEN agg.sum_output_tokens > 0 THEN agg.sum_output_tokens END,
                    'total_tokens', CASE WHEN GREATEST(agg.sum_total_tokens, agg.sum_input_tokens + agg.sum_output_tokens) > 0 THEN GREATEST(agg.sum_total_tokens, agg.sum_input_tokens + agg.sum_output_tokens) END,
                    'cached_tokens', CASE WHEN agg.sum_cached_tokens > 0 THEN agg.sum_cached_tokens END,
                    'cache_5m_tokens', CASE WHEN agg.sum_cache_5m_tokens > 0 THEN agg.sum_cache_5m_tokens END,
                    'cache_1h_tokens', CASE WHEN agg.sum_cache_1h_tokens > 0 THEN agg.sum_cache_1h_tokens END,
                    'reasoning_tokens', CASE WHEN agg.sum_reasoning_tokens > 0 THEN agg.sum_reasoning_tokens END,
                    'audio_input_tokens', CASE WHEN agg.sum_audio_input_tokens > 0 THEN agg.sum_audio_input_tokens END,
                    'audio_output_tokens', CASE WHEN agg.sum_audio_output_tokens > 0 THEN agg.sum_audio_output_tokens END,
                    'per_call_usages', pca.per_call_usages
                ))
                || jsonb_build_object(
                    'usage_known', (agg.all_usage_known AND agg.total_unknown_calls = 0),
                    'unknown_usage_calls', agg.total_unknown_calls,
                    'attempt_details', agg.attempt_details
                )
        FROM agg
        LEFT JOIN per_call_agg pca ON pca.invocation_id = agg.invocation_id
        WHERE j.invocation_id = agg.invocation_id;
    END IF;
END $$
"""


_CREATE_INDEX_USER_OCCURRED_SQL = """
CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_attempts_user_occurred
ON dalton_llm_journal_attempts (user_id, occurred_at)
""".strip()

_CREATE_INDEX_SCOPE_SQL = """
CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_attempts_scope
ON dalton_llm_journal_attempts (scope_ref)
""".strip()

_ADD_FK_NOT_VALID_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_dalton_llm_journal_attempts_invocation'
          AND conrelid = 'dalton_llm_journal_attempts'::regclass
    ) THEN
        ALTER TABLE dalton_llm_journal_attempts
        ADD CONSTRAINT fk_dalton_llm_journal_attempts_invocation
        FOREIGN KEY (invocation_id)
        REFERENCES dalton_llm_journal(invocation_id)
        ON DELETE CASCADE
        NOT VALID;
    END IF;
END $$
""".strip()

_VALIDATE_FK_SQL = """
ALTER TABLE dalton_llm_journal_attempts
VALIDATE CONSTRAINT fk_dalton_llm_journal_attempts_invocation
""".strip()


def upgrade() -> None:
    # Phase 1 (DDL transaction): create table with primary key only (NO inline FK yet),
    # so `dalton_llm_journal` is not locked at all during table creation or backfill.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts (
            invocation_id       TEXT NOT NULL,
            attempt             INT  NOT NULL,
            call_seq            INT  NOT NULL DEFAULT 1,
            user_id             TEXT,
            scope_ref           TEXT NOT NULL,
            purpose             TEXT NOT NULL,
            model_key           TEXT NOT NULL,
            provider            TEXT,
            billing_type        TEXT,
            outcome             TEXT NOT NULL CHECK (outcome IN ('done', 'failed', 'indeterminate')),
            error_kind          TEXT,
            usage               JSONB,
            cash_usd            NUMERIC(12,6) NOT NULL DEFAULT 0,
            notional_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
            usage_known         BOOLEAN NOT NULL DEFAULT TRUE,
            unknown_usage_calls INT NOT NULL DEFAULT 0,
            occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (invocation_id, attempt, call_seq)
        )
        """
    )

    # Phase 2 (autocommit): backfill historical rows in bounded keyset batches (preserving
    # `j.created_at` as `occurred_at` and mapping in-flight `pending` rows with prior cost to
    # `GREATEST(1, j.attempt - 1)` / `'indeterminate'`), then build secondary indexes, then
    # attach the FK as `NOT VALID` (brief metadata-only lock) and validate separately
    # (`SHARE UPDATE EXCLUSIVE` on child / `ROW SHARE` on parent — does not block writes).
    ctx = op.get_context()
    with ctx.autocommit_block():
        if getattr(ctx, "as_sql", False):
            op.execute(_OFFLINE_BATCHED_BACKFILL_SQL)
            op.execute(_CREATE_INDEX_USER_OCCURRED_SQL)
            op.execute(_CREATE_INDEX_SCOPE_SQL)
            op.execute("SET lock_timeout = '5s'")
            op.execute(_ADD_FK_NOT_VALID_SQL)
            op.execute(_VALIDATE_FK_SQL)
            op.execute("SET lock_timeout = '0'")
        else:
            conn = op.get_bind()
            conn.execute(sa.text("SET lock_timeout = '5s'"))
            try:
                last_id = ""
                stmt = sa.text(_ONLINE_BACKFILL_BATCH_SQL)
                while True:
                    res = conn.execute(
                        stmt,
                        {"last_id": last_id, "batch_size": _BACKFILL_BATCH_SIZE},
                    )
                    row = res.fetchone()
                    if not row:
                        break
                    next_last_id, scanned = row[0], int(row[1] or 0)
                    if scanned < _BACKFILL_BATCH_SIZE or not next_last_id:
                        break
                    last_id = str(next_last_id)
                conn.execute(sa.text(_CREATE_INDEX_USER_OCCURRED_SQL))
                conn.execute(sa.text(_CREATE_INDEX_SCOPE_SQL))
                conn.execute(sa.text(_ADD_FK_NOT_VALID_SQL))
                conn.execute(sa.text(_VALIDATE_FK_SQL))
            finally:
                conn.execute(sa.text("SET lock_timeout = '0'"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Roll up per-request attempt billing, token usage, per_call_usages, and attempt_details
    # back into `dalton_llm_journal` (the table d042 reads) BEFORE dropping the attempts table.
    op.execute(_DOWNGRADE_ROLLUP_SQL)
    op.execute(
        "ALTER TABLE IF EXISTS dalton_llm_journal_attempts "
        "DROP CONSTRAINT IF EXISTS fk_dalton_llm_journal_attempts_invocation"
    )
    op.execute("DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE")
