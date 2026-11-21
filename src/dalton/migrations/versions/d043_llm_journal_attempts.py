"""Dalton d043: per-attempt LLM journal billing ledger (`dalton_llm_journal_attempts`).

Records each individual attempt of a `dalton_llm_journal` invocation keyed by
`(invocation_id, attempt)` with its own `occurred_at`, `usage`, `cash_usd`,
`notional_usd`, `usage_known`, and `unknown_usage_calls`.

This enables:
- attributing daily cash budget spend to the day each attempt actually occurred
  (rather than the original journal row's `created_at` on cross-day resends),
- settling a stale attempt's known cost even after a newer attempt took over the
  parent journal row without clobbering the newer attempt's state or response,
- preserving unknown-usage records from earlier timed-out/stream-cut attempts
  even after a subsequent resend succeeds.

Revision ID: d043
Revises: d042
Create Date: 2026-11-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d043"
down_revision: Union[str, None] = "d042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts (
            invocation_id       TEXT NOT NULL REFERENCES dalton_llm_journal(invocation_id) ON DELETE CASCADE,
            attempt             INT  NOT NULL,
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
            PRIMARY KEY (invocation_id, attempt)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_attempts_user_occurred
        ON dalton_llm_journal_attempts (user_id, occurred_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_attempts_scope
        ON dalton_llm_journal_attempts (scope_ref)
        """
    )
    op.execute(
        """
        INSERT INTO dalton_llm_journal_attempts (
            invocation_id,
            attempt,
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
            CASE
                WHEN j.state = 'indeterminate' AND (
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
            END AS usage_known,
            CASE
                WHEN (j.usage->>'unknown_usage_calls') ~ '^[0-9]+$'
                    THEN (j.usage->>'unknown_usage_calls')::int
                WHEN j.state = 'indeterminate' AND (
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
            END AS unknown_usage_calls,
            COALESCE(j.completed_at, j.created_at) AS occurred_at,
            j.created_at
        FROM dalton_llm_journal j
        WHERE j.state IN ('done', 'indeterminate')
           OR j.cash_usd > 0
           OR j.notional_usd > 0
        ON CONFLICT (invocation_id, attempt) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE")
