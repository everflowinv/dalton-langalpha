"""Dalton d036: store a confirmed Discord thread on the delivery that created it.

Revision ID: d036
Revises: d035
Create Date: 2026-11-10

The column holds a Discord thread address, not a secret. It stays separate from
``target_address`` (the original parent authorization) and from
``first_response_json`` (replaced when a delivery finishes).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d036"
down_revision: Union[str, None] = "d035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS confirmed_thread_address VARCHAR(255) NULL;
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN dalton_channel_deliveries.confirmed_thread_address IS
            'Discord thread address confirmed before the first message. Not a secret. Independent of target_address and first_response_json.';
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS confirmed_thread_address;
        """
    )
