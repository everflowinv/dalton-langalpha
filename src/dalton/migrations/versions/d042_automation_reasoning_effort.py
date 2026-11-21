"""Dalton d042: per-automation reasoning_effort column on automations.

Revision ID: d042
Revises: d041
Create Date: 2026-11-21
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d042"
down_revision: Union[str, None] = "d041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE automations
          ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)
          CONSTRAINT ck_automations_reasoning_effort
          CHECK (
            reasoning_effort IS NULL
            OR reasoning_effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
          )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE automations DROP COLUMN IF EXISTS reasoning_effort")
