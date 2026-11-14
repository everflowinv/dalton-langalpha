"""Dalton d037: persist rendered wake_context on dalton_preflight_log.

Revision ID: d037
Revises: d036
Create Date: 2026-11-13
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d037"
down_revision: Union[str, None] = "d036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_preflight_log
            ADD COLUMN IF NOT EXISTS wake_context TEXT NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN dalton_preflight_log.wake_context
            IS 'Rendered wake directive (with optional coverage_group_refresh block) passed to AutomationExecutor on wake; restored on restart handoff/refire.'
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_preflight_log
            DROP COLUMN IF EXISTS wake_context
        """
    )
