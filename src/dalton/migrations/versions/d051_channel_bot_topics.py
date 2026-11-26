"""Dalton d051: group bot topics table (`dalton_channel_bot_topics`) and DM recipient lock column.

Creates ``dalton_channel_bot_topics`` to persist pending reservations and confirmed
bot-created/registered Feishu topics & Discord public threads, locking the
initiator identity and ``canonical_scope_key`` before topic creation so an early
``I-1`` from another member never hijacks topic ownership.

Also adds the nullable ``dm_recipient_platform_user_id`` column on
``dalton_channel_deliveries`` to pin the verified platform DM recipient from the
originating inbound conversation for pre-send / post-upload binding verification.

Revision ID: d051
Revises: d050
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d051"
down_revision: str | None = "d050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_bot_topics (
            platform VARCHAR(32) NOT NULL CHECK (platform IN ('feishu', 'discord')),
            chat_address VARCHAR(255) NOT NULL,
            canonical_scope_key VARCHAR(255) NOT NULL,
            external_topic_id VARCHAR(255) NULL,
            root_message_id VARCHAR(255) NULL,
            first_reply_message_id VARCHAR(255) NULL,
            initiator_dalton_user_id VARCHAR(255) NOT NULL,
            initiator_platform_user_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('pending', 'confirmed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, chat_address, canonical_scope_key)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dalton_channel_bot_topics_external
            ON dalton_channel_bot_topics (platform, chat_address, external_topic_id)
            WHERE external_topic_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dalton_channel_bot_topics_root
            ON dalton_channel_bot_topics (platform, chat_address, root_message_id)
            WHERE root_message_id IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS dm_recipient_platform_user_id VARCHAR(255) NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS dm_recipient_platform_user_id
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_bot_topics_root")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_bot_topics_external")
    op.execute("DROP TABLE IF EXISTS dalton_channel_bot_topics")
