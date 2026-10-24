"""Dalton d014: personal notification delivery ledger (`dalton_notify_deliveries`)."""

from __future__ import annotations

from alembic import op

revision = "d014"
down_revision = "d013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_notify_deliveries (
            delivery_id   UUID PRIMARY KEY,
            execution_id  TEXT NOT NULL,
            automation_id TEXT,
            user_id       TEXT NOT NULL,
            channel       TEXT NOT NULL,
            target        TEXT NOT NULL,
            status        TEXT NOT NULL
                CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            error_code    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (execution_id, channel, target)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_notify_deliveries_retry
            ON dalton_notify_deliveries (status, attempt_count, created_at)
            WHERE status IN ('failed', 'pending')
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_notify_deliveries")
