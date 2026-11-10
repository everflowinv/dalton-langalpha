"""Dalton d036: store a confirmed Discord thread, its body phase, and claim generation.

Revision ID: d036
Revises: d035
Create Date: 2026-11-10

``confirmed_thread_address`` holds a Discord thread address, not a secret. It
stays separate from ``target_address`` (the original parent authorization) and
from ``first_response_json`` (replaced when a delivery finishes).

``discord_body_phase`` records whether a body request has been authorized.
``before_body`` means no body POST has been authorized. ``dispatching`` means a
body POST may already have reached Discord. A null phase does not prove that
the body is unsent.

``delivery_attempt`` is the current claim generation for this delivery key.
A progress write that names an older generation does not change the row.
None of these columns is a secret.
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
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS discord_body_phase VARCHAR(32) NULL;
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN dalton_channel_deliveries.discord_body_phase IS
            'Discord body phase for a confirmed thread. before_body means no body request has been authorized. dispatching means a body request may already have reached Discord. Null does not prove the body is unsent. Not a secret.';
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'ck_dalton_channel_deliveries_discord_body_phase'
            ) THEN
                ALTER TABLE dalton_channel_deliveries
                    ADD CONSTRAINT ck_dalton_channel_deliveries_discord_body_phase
                    CHECK (
                        discord_body_phase IS NULL
                        OR discord_body_phase IN ('before_body', 'dispatching')
                    );
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS delivery_attempt INTEGER NOT NULL DEFAULT 0;
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN dalton_channel_deliveries.delivery_attempt IS
            'Monotonic claim generation for this delivery key. Not a secret. A progress write that names an older attempt does not change the row.';
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP CONSTRAINT IF EXISTS ck_dalton_channel_deliveries_discord_body_phase;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS discord_body_phase;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS delivery_attempt;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS confirmed_thread_address;
        """
    )
