"""Dalton d057: one-time web confirmation credentials for report_delivery REST writes.

Stores short-lived, single-use confirmation tokens issued by the web confirmation
card before `POST /api/v1/automations` or `PATCH /api/v1/automations/{id}` saves
a `metadata.report_profile` containing group/channel targets or non-caller DM targets.

Revision ID: d057
Revises: d055
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d057"
down_revision: str | None = "d055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_report_delivery_confirmations (
            confirmation_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            actor_user_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('create', 'update')),
            automation_id TEXT NULL,
            expected_config_version INTEGER NULL,
            payload_digest TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_report_delivery_confirmations_actor_expires
            ON dalton_report_delivery_confirmations (actor_user_id, expires_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_report_delivery_hitl_snapshots (
            thread_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            caller_user_id TEXT NOT NULL,
            automation_id TEXT NOT NULL,
            automation_name TEXT NOT NULL,
            config_version INTEGER NULL,
            profile TEXT NOT NULL,
            report_targets JSONB NOT NULL,
            text_targets JSONB NULL,
            failure_targets JSONB NOT NULL,
            group_targets JSONB NOT NULL,
            non_caller_dm_targets JSONB NOT NULL,
            target_labels JSONB NOT NULL,
            needs_confirmation BOOLEAN NOT NULL,
            confirm_token TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, tool_call_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_report_delivery_hitl_snapshots_created_at
            ON dalton_report_delivery_hitl_snapshots (created_at)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_dalton_report_delivery_hitl_snapshots_created_at")
    op.execute("DROP TABLE IF EXISTS dalton_report_delivery_hitl_snapshots")
    op.execute("DROP INDEX IF EXISTS ix_dalton_report_delivery_confirmations_actor_expires")
    op.execute("DROP TABLE IF EXISTS dalton_report_delivery_confirmations")
