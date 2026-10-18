"""Dalton d007: dalton_cash_usd function, LLM call journal, provider events, and signal expiry (P0N.6, P0N.7, P0N.8, G4.3)."""

from __future__ import annotations

from alembic import op

revision = "d007"
down_revision = "d006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dalton_cash_usd(
            tu JSONB,
            total_credits NUMERIC,
            infra_credits NUMERIC,
            rate NUMERIC
        )
        RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $$
          SELECT CASE
            WHEN tu IS NOT NULL AND jsonb_typeof(tu->'per_call_costs') = 'array' THEN
              COALESCE((
                SELECT SUM((c->>'cost')::numeric)
                FROM jsonb_array_elements(tu->'per_call_costs') c
                WHERE COALESCE(c->>'billing_type', 'platform') <> 'oauth'
              ), 0)
              + COALESCE(infra_credits, 0) / rate
            ELSE COALESCE(total_credits, 0) / rate
          END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_llm_journal (
            invocation_id   TEXT PRIMARY KEY,
            user_id         TEXT,
            scope_ref       TEXT NOT NULL,
            purpose         TEXT NOT NULL,
            model_key       TEXT NOT NULL,
            provider        TEXT,
            billing_type    TEXT,
            request_hash    TEXT NOT NULL,
            state           TEXT NOT NULL CHECK (state IN ('pending','done','failed','indeterminate')),
            attempt         INT  NOT NULL DEFAULT 1,
            claimed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ,
            response        JSONB,
            usage           JSONB,
            cash_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
            notional_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
            error_kind      TEXT,
            error_text      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_user_day
        ON dalton_llm_journal (user_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_llm_journal_scope
        ON dalton_llm_journal (scope_ref)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_provider_events (
            event_id    BIGSERIAL PRIMARY KEY,
            user_id     TEXT,
            provider    TEXT NOT NULL,
            model_key   TEXT,
            kind        TEXT NOT NULL,
            detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_provider_events_p
        ON dalton_provider_events (provider, created_at DESC)
        """
    )
    op.execute(
        "ALTER TABLE IF EXISTS dalton_signals ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;"
    )
    op.execute(
        "ALTER TABLE IF EXISTS dalton_watch_signals ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS dalton_watch_signals DROP COLUMN IF EXISTS expired_at")
    op.execute("ALTER TABLE IF EXISTS dalton_signals DROP COLUMN IF EXISTS expired_at")
    op.execute("DROP TABLE IF EXISTS dalton_provider_events CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_llm_journal CASCADE")
    op.execute("DROP FUNCTION IF EXISTS dalton_cash_usd(JSONB, NUMERIC, NUMERIC, NUMERIC)")
