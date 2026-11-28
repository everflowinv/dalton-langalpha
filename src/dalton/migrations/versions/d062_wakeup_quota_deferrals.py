"""Dalton d062: wakeup shared quota deferrals ledger & budget reservation fallback call idempotency.

Note on revision numbering:
- Anchored on ``down_revision = "d061"`` (the Alembic revision ID of ``d061_monitor_publish_verify.py``).
- Operates exclusively on Dalton-owned ``dalton_*`` tables (per standalone-core.md).

Revision ID: d062
Revises: d061
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d062"
down_revision: str | None = "d061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1. Shared wakeup quota deferral & checkpoint-resume ledger keyed by initial root_job_id
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_wakeup_quota_deferrals (
            root_job_id          UUID PRIMARY KEY,
            wakeup_id            TEXT NOT NULL,
            thread_id            UUID NOT NULL,
            owner_user_id        VARCHAR(255) NOT NULL,
            first_due_at         TIMESTAMPTZ NOT NULL,
            deadline_at          TIMESTAMPTZ NOT NULL,
            defer_count          INTEGER NOT NULL DEFAULT 0
                                 CHECK (defer_count >= 0),
            last_step_key        TEXT NOT NULL,
            resume_from_run_id   VARCHAR(255),
            resume_checkpoint_id TEXT,
            last_reset_at        TIMESTAMPTZ,
            last_reason          TEXT,
            status               TEXT NOT NULL DEFAULT 'deferred'
                                 CHECK (status IN ('deferred', 'settled', 'exhausted_failed')),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wakeup_quota_deferrals_thread_status
            ON dalton_wakeup_quota_deferrals (thread_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wakeup_quota_deferrals_wakeup_status
            ON dalton_wakeup_quota_deferrals (wakeup_id, status)
        """
    )

    # 2. Idempotent (run_ref, call_id) tracking on dalton_budget_reservations for cash fallback leases
    op.execute(
        """
        ALTER TABLE dalton_budget_reservations
            ADD COLUMN IF NOT EXISTS last_fallback_call_id TEXT
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute(
        """
        ALTER TABLE dalton_budget_reservations
            DROP COLUMN IF EXISTS last_fallback_call_id
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_dalton_wakeup_quota_deferrals_wakeup_status")
    op.execute("DROP INDEX IF EXISTS idx_dalton_wakeup_quota_deferrals_thread_status")
    op.execute("DROP TABLE IF EXISTS dalton_wakeup_quota_deferrals")
