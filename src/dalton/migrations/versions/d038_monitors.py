"""Dalton d038: monitor registry, member ACL, realtime data store, and monitor_grant signing key.

Revision ID: d038
Revises: d037
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d038"
down_revision: Union[str, None] = "d037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_monitors (
            monitor_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT NOT NULL UNIQUE,
            artifact_id UUID NOT NULL UNIQUE
                REFERENCES share_links(artifact_id) ON DELETE CASCADE,
            owner_user_id VARCHAR(255) NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other'
                CHECK (category IN ('overview', 'us', 'cn', 'hk', 'asia', 'stocks', 'portfolio', 'other')),
            sort_order INTEGER NOT NULL DEFAULT 0,
            visibility TEXT NOT NULL DEFAULT 'own'
                CHECK (visibility IN ('own', 'members', 'company', 'public')),
            published_seq INTEGER NOT NULL DEFAULT 0 CHECK (published_seq >= 0),
            keep_updated BOOLEAN NOT NULL DEFAULT FALSE,
            source_kind TEXT NOT NULL DEFAULT 'workspace'
                CHECK (source_kind IN ('workspace', 'data_job')),
            source_ref TEXT,
            acl_revision INTEGER NOT NULL DEFAULT 1 CHECK (acl_revision >= 1),
            last_publish_run_id TEXT,
            last_publish_error TEXT,
            last_publish_failed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT dalton_monitors_slug_format CHECK (
                char_length(slug) BETWEEN 1 AND 120
                AND slug ~ '^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$'
            ),
            CONSTRAINT dalton_monitors_title_length CHECK (
                char_length(BTRIM(title)) BETWEEN 1 AND 300
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_monitors_category_sort
        ON dalton_monitors (category, sort_order, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_monitors_owner
        ON dalton_monitors (owner_user_id)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_monitor_members (
            monitor_id UUID NOT NULL
                REFERENCES dalton_monitors(monitor_id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            PRIMARY KEY (monitor_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_monitor_members_user
        ON dalton_monitor_members (user_id)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_monitor_data (
            monitor_id UUID NOT NULL
                REFERENCES dalton_monitors(monitor_id) ON DELETE CASCADE,
            data_key TEXT NOT NULL,
            payload JSONB NOT NULL,
            as_of TIMESTAMPTZ NOT NULL,
            sha256 TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (monitor_id, data_key),
            CONSTRAINT dalton_monitor_data_key_format CHECK (
                char_length(data_key) BETWEEN 1 AND 128
                AND data_key ~ '^[A-Za-z0-9._-]+$'
            ),
            CONSTRAINT dalton_monitor_data_sha256 CHECK (
                sha256 ~ '^[0-9a-f]{64}$'
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO server_keys (name, secret)
        VALUES ('monitor_grant', gen_random_bytes(32))
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM server_keys WHERE name = 'monitor_grant'")
    op.execute("DROP TABLE IF EXISTS dalton_monitor_data")
    op.execute("DROP TABLE IF EXISTS dalton_monitor_members")
    op.execute("DROP TABLE IF EXISTS dalton_monitors")
