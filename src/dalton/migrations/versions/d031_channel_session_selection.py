"""Dalton d031: CH-1 shared workspace commands & 6-hour session selection persistence.

Revision ID: d031
Revises: d030
Create Date: 2026-11-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d031"
down_revision: Union[str, None] = "d030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1. Explicit conversation activity timestamp and initialization marker on session rows
    op.execute(
        """
        ALTER TABLE dalton_channel_session_generations
            ADD COLUMN IF NOT EXISTS last_conversation_activity_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS selection_initialized_at TIMESTAMPTZ NULL;
        """
    )

    # 2. Inbound command persistence, immutable generation snapshot, and interrupt snapshot
    op.execute(
        """
        ALTER TABLE dalton_channel_inbound_events
            ADD COLUMN IF NOT EXISTS event_kind VARCHAR(32) NOT NULL DEFAULT 'message',
            ADD COLUMN IF NOT EXISTS session_generation INTEGER NULL,
            ADD COLUMN IF NOT EXISTS command_request JSONB NULL,
            ADD COLUMN IF NOT EXISTS command_result JSONB NULL,
            ADD COLUMN IF NOT EXISTS command_effect_status VARCHAR(32) NULL,
            ADD COLUMN IF NOT EXISTS command_reply_pending BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS pending_interrupt_snapshot JSONB NULL;
        """
    )

    # Rebuild recovery partial index to include pending command replies and cancellations
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_inbound_events_recovery;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_inbound_events_recovery
            ON dalton_channel_inbound_events(status, fallback_pending, updated_at)
            WHERE status IN ('pending', 'received', 'accepted', 'dispatched', 'streaming', 'steered', 'pending_resubmit')
               OR fallback_pending = TRUE
               OR command_reply_pending = TRUE
               OR command_effect_status = 'pending_cancel';
        """
    )

    # 3. Delivery origin metadata for activity time gating
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS source_scope_key VARCHAR(255) NULL,
            ADD COLUMN IF NOT EXISTS source_generation INTEGER NULL,
            ADD COLUMN IF NOT EXISTS activity_origin VARCHAR(32) NOT NULL DEFAULT 'none';
        """
    )

    # 4. Per-thread HITL pending interrupt persistence
    op.execute(
        """
        ALTER TABLE dalton_channel_thread_watches
            ADD COLUMN IF NOT EXISTS pending_interrupt JSONB NULL;
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute(
        """
        ALTER TABLE dalton_channel_thread_watches
            DROP COLUMN IF EXISTS pending_interrupt;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS activity_origin,
            DROP COLUMN IF EXISTS source_generation,
            DROP COLUMN IF EXISTS source_scope_key;
        """
    )

    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_inbound_events_recovery;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_inbound_events_recovery
            ON dalton_channel_inbound_events(status, fallback_pending, updated_at)
            WHERE status IN ('pending', 'received', 'accepted', 'dispatched', 'streaming', 'steered', 'pending_resubmit')
               OR fallback_pending = TRUE;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_inbound_events
            DROP COLUMN IF EXISTS pending_interrupt_snapshot,
            DROP COLUMN IF EXISTS command_reply_pending,
            DROP COLUMN IF EXISTS command_effect_status,
            DROP COLUMN IF EXISTS command_result,
            DROP COLUMN IF EXISTS command_request,
            DROP COLUMN IF EXISTS session_generation,
            DROP COLUMN IF EXISTS event_kind;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_session_generations
            DROP COLUMN IF EXISTS selection_initialized_at,
            DROP COLUMN IF EXISTS last_conversation_activity_at;
        """
    )
