"""Dalton d054: persist verified run_id on dalton_channel_automation_runs.

Adds nullable ``run_id`` column on ``dalton_channel_automation_runs`` so automation
settlement persists the settlement task's own ``run_id`` (validated against the
execution record and thread) and gateway restart recovery of ``finishing`` rows
settles charts and widgets against that exact run instead of guessing the latest
turn on the thread.

Revision ID: d054
Revises: d053
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d054"
down_revision: str | None = "d053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_automation_runs
            ADD COLUMN IF NOT EXISTS run_id VARCHAR(255) NULL;
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_automation_runs
            DROP COLUMN IF EXISTS run_id;
        """
    )
