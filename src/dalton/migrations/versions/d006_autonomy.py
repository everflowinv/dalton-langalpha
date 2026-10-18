"""Dalton d006: autonomous agenda watch state, signals, preflight logs, and daily plans (PK.16)."""

from __future__ import annotations

from alembic import op

revision = "d006"
down_revision = "d005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_watch_state (
            workspace_id   UUID PRIMARY KEY,
            ticker         TEXT NOT NULL,
            state          JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_signals (
            signal_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id       UUID,
            ticker             TEXT,
            probe              TEXT NOT NULL,
            signal_key         TEXT NOT NULL,
            priority           TEXT NOT NULL DEFAULT 'P1',
            summary            TEXT NOT NULL,
            payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
            detected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            woken_execution_id UUID,
            woken_at           TIMESTAMPTZ,
            resolved_at        TIMESTAMPTZ,
            UNIQUE (workspace_id, probe, signal_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_signals_open
        ON dalton_signals (workspace_id, detected_at DESC)
        WHERE resolved_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_preflight_log (
            log_id                  BIGSERIAL PRIMARY KEY,
            automation_id           UUID,
            automation_execution_id UUID,
            workspace_id            UUID,
            decision                TEXT NOT NULL CHECK (decision IN ('wake','skip','passthrough')),
            reason                  TEXT,
            signals                 JSONB NOT NULL DEFAULT '[]'::jsonb,
            checked_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_pf_checked_at
        ON dalton_preflight_log (checked_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_daily_plans (
            plan_date          DATE PRIMARY KEY,
            budget_snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
            board_snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,
            dispatches         JSONB NOT NULL DEFAULT '[]'::jsonb,
            skipped            JSONB NOT NULL DEFAULT '[]'::jsonb,
            pm_attention       JSONB NOT NULL DEFAULT '[]'::jsonb,
            director_thread_id UUID,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_daily_plans CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_preflight_log CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_signals CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_watch_state CASCADE")
