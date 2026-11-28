"""Dalton d061: monitor publish verification pointers, PG lease queue on data_task_runs,
and dual-axis realtime feed health columns on dalton_monitor_data.

Note on revision numbering:
- Anchored on ``down_revision = "d060"`` (main head after D4 PR #521 ``d059``–``d060``).
- Operates exclusively on Dalton-owned ``dalton_*`` tables (per standalone-core.md).

Revision ID: d061
Revises: d060
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d061"
down_revision: str | None = "d060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONITOR_PUBLISH_STATUSES: tuple[str, ...] = (
    "not_applicable",
    "pending",
    "publishing",
    "retrying",
    "published",
    "unchanged",
    "partial_failed",
    "failed",
    "superseded",
    "disabled",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1. dalton_monitors: verified visitor pointer columns
    op.execute(
        """
        ALTER TABLE dalton_monitors
            ADD COLUMN IF NOT EXISTS last_verified_seq INTEGER
                CHECK (last_verified_seq IS NULL OR last_verified_seq >= 0),
            ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE dalton_monitors
           SET last_verified_seq = NULLIF(published_seq, 0),
               last_verified_at = CASE WHEN published_seq > 0 THEN updated_at ELSE NULL END
         WHERE last_verified_seq IS NULL
           AND published_seq > 0
        """
    )

    # 2. dalton_data_task_runs: PostgreSQL-backed durable monitor publish lease queue
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            ADD COLUMN IF NOT EXISTS monitor_publish_status VARCHAR(32) NOT NULL DEFAULT 'not_applicable',
            ADD COLUMN IF NOT EXISTS monitor_publish_attempts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS monitor_publish_claim_epoch BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS monitor_publish_claimed_by TEXT,
            ADD COLUMN IF NOT EXISTS monitor_publish_lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS monitor_publish_next_retry_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS monitor_publish_detail JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            ADD CONSTRAINT dalton_data_task_runs_monitor_publish_status_check
            CHECK (
                monitor_publish_status IN (
                    'not_applicable',
                    'pending',
                    'publishing',
                    'retrying',
                    'published',
                    'unchanged',
                    'partial_failed',
                    'failed',
                    'superseded',
                    'disabled'
                )
            )
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_attempts_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            ADD CONSTRAINT dalton_data_task_runs_monitor_publish_attempts_check
            CHECK (monitor_publish_attempts >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_claim_epoch_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            ADD CONSTRAINT dalton_data_task_runs_monitor_publish_claim_epoch_check
            CHECK (monitor_publish_claim_epoch >= 0)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_data_task_runs_monitor_publish_due
            ON dalton_data_task_runs (
                monitor_publish_status,
                monitor_publish_next_retry_at,
                monitor_publish_lease_expires_at
            )
            WHERE monitor_publish_status IN ('pending', 'retrying', 'publishing')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_data_task_runs_monitor_publish_issues
            ON dalton_data_task_runs (job_key, completed_at DESC)
            WHERE monitor_publish_status IN ('failed', 'partial_failed', 'disabled')
        """
    )

    # 3. dalton_monitor_data: nullable snapshot triple (all-null or all-non-null) + independent health axis
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ALTER COLUMN payload DROP NOT NULL,
            ALTER COLUMN as_of DROP NOT NULL,
            ALTER COLUMN sha256 DROP NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_sha256
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ADD CONSTRAINT dalton_monitor_data_sha256
            CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$')
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_snapshot_consistency
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ADD CONSTRAINT dalton_monitor_data_snapshot_consistency
            CHECK (
                (payload IS NULL AND as_of IS NULL AND sha256 IS NULL)
                OR (payload IS NOT NULL AND as_of IS NOT NULL AND sha256 IS NOT NULL)
            )
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ADD COLUMN IF NOT EXISTS attempt_seq BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS is_stale BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_error TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_attempt_seq_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ADD CONSTRAINT dalton_monitor_data_attempt_seq_check
            CHECK (attempt_seq >= 0)
        """
    )
    op.execute(
        """
        UPDATE dalton_monitor_data
           SET is_stale = COALESCE((payload->>'stale')::boolean, FALSE)
         WHERE payload IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Reverse dalton_monitor_data changes
    op.execute(
        """
        DELETE FROM dalton_monitor_data
         WHERE payload IS NULL OR as_of IS NULL OR sha256 IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_attempt_seq_check,
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_snapshot_consistency,
            DROP CONSTRAINT IF EXISTS dalton_monitor_data_sha256,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS last_attempt_at,
            DROP COLUMN IF EXISTS is_stale,
            DROP COLUMN IF EXISTS attempt_seq
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_monitor_data
            ALTER COLUMN payload SET NOT NULL,
            ALTER COLUMN as_of SET NOT NULL,
            ALTER COLUMN sha256 SET NOT NULL,
            ADD CONSTRAINT dalton_monitor_data_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
        """
    )

    # Reverse dalton_data_task_runs changes
    op.execute("DROP INDEX IF EXISTS idx_dalton_data_task_runs_monitor_publish_issues")
    op.execute("DROP INDEX IF EXISTS idx_dalton_data_task_runs_monitor_publish_due")
    op.execute(
        """
        ALTER TABLE dalton_data_task_runs
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_claim_epoch_check,
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_attempts_check,
            DROP CONSTRAINT IF EXISTS dalton_data_task_runs_monitor_publish_status_check,
            DROP COLUMN IF EXISTS monitor_publish_detail,
            DROP COLUMN IF EXISTS monitor_publish_next_retry_at,
            DROP COLUMN IF EXISTS monitor_publish_lease_expires_at,
            DROP COLUMN IF EXISTS monitor_publish_claimed_by,
            DROP COLUMN IF EXISTS monitor_publish_claim_epoch,
            DROP COLUMN IF EXISTS monitor_publish_attempts,
            DROP COLUMN IF EXISTS monitor_publish_status
        """
    )

    # Reverse dalton_monitors changes
    op.execute(
        """
        ALTER TABLE dalton_monitors
            DROP COLUMN IF EXISTS last_verified_at,
            DROP COLUMN IF EXISTS last_verified_seq
        """
    )
