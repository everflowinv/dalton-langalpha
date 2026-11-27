"""Dalton d06X: deployment settings revision metadata, append-only audit log, and sandbox scrub state.

Adds three Dalton-owned tables alongside upstream migration 148's ``deployment_settings``:
- ``dalton_deployment_settings_meta``: single-row monotonically increasing revision counter
  used for consistent REPEATABLE READ snapshots and optimistic concurrency control.
- ``dalton_deployment_settings_audit``: append-only audit trail protected by a
  statement-level ``BEFORE UPDATE OR DELETE OR TRUNCATE`` trigger.
- ``dalton_sandbox_feishu_vault_scrub``: per-computer status table for online cleanup of
  dedicated Feishu Vault credentials from sandbox disks (used in Batch 1 Step 2).

Revision ID: d06X
Revises: d057
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d06X"
down_revision: str | None = "d057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_deployment_settings_meta (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            revision BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO dalton_deployment_settings_meta (singleton, revision)
        VALUES (TRUE, 0)
        ON CONFLICT DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_deployment_settings_audit (
            audit_id BIGSERIAL PRIMARY KEY,
            revision BIGINT NOT NULL,
            actor_user_id TEXT NOT NULL,
            setting_name TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('set', 'rotate', 'clear')),
            previous_source TEXT NOT NULL CHECK (previous_source IN ('unset', 'saved')),
            value_fingerprint TEXT NULL,
            client_ip TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_deployment_settings_audit_name_created
            ON dalton_deployment_settings_audit (setting_name, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_deployment_settings_audit_revision
            ON dalton_deployment_settings_audit (revision)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dalton_deployment_settings_audit_append_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'dalton_deployment_settings_audit is append-only (% is forbidden)', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dalton_deployment_settings_audit_append_only
            ON dalton_deployment_settings_audit
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dalton_deployment_settings_audit_append_only
            BEFORE UPDATE OR DELETE OR TRUNCATE
            ON dalton_deployment_settings_audit
            FOR EACH STATEMENT
            EXECUTE FUNCTION dalton_deployment_settings_audit_append_only()
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_sandbox_feishu_vault_scrub (
            computer_id UUID PRIMARY KEY,
            provider_ref TEXT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'deferred_stopped', 'failed', 'cleaned')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            cleaned_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_sandbox_feishu_vault_scrub")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_dalton_deployment_settings_audit_append_only "
        "ON dalton_deployment_settings_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS dalton_deployment_settings_audit_append_only()")
    op.execute("DROP INDEX IF EXISTS ix_dalton_deployment_settings_audit_revision")
    op.execute("DROP INDEX IF EXISTS ix_dalton_deployment_settings_audit_name_created")
    op.execute("DROP TABLE IF EXISTS dalton_deployment_settings_audit")
    op.execute("DROP TABLE IF EXISTS dalton_deployment_settings_meta")
