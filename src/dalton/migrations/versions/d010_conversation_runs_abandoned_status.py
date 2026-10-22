"""Dalton d010: allow `status = 'abandoned'` on `dalton_conversation_runs` for crash recovery."""

from __future__ import annotations

from alembic import op

revision = "d010"
down_revision = "d009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_conversation_runs
        DROP CONSTRAINT IF EXISTS dalton_conversation_runs_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_conversation_runs
        ADD CONSTRAINT dalton_conversation_runs_status_check
        CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'abandoned'))
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE dalton_conversation_runs
        SET status = 'interrupted'
        WHERE status = 'abandoned'
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_conversation_runs
        DROP CONSTRAINT IF EXISTS dalton_conversation_runs_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_conversation_runs
        ADD CONSTRAINT dalton_conversation_runs_status_check
        CHECK (status IN ('running', 'completed', 'failed', 'interrupted'))
        """
    )
