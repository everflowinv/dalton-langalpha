"""d015: per-user coverage roster, coverage groups, and earnings calendar mirror.

Revision ID: d015
Revises: d014
Create Date: 2026-10-23
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d015"
down_revision: Union[str, None] = "d014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_coverage_groups (
            group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
            default_tier TEXT NOT NULL DEFAULT 'watch'
                CHECK (default_tier IN ('core', 'watch')),
            auto_refresh BOOLEAN NOT NULL DEFAULT TRUE,
            last_resolved_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_groups_user
            ON dalton_coverage_groups (user_id, created_at);

        CREATE TABLE IF NOT EXISTS dalton_coverage (
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'watch'
                CHECK (tier IN ('core', 'watch')),
            market TEXT NOT NULL DEFAULT 'US',
            peer_group TEXT NOT NULL DEFAULT '',
            peers JSONB NOT NULL DEFAULT '[]'::jsonb,
            aliases JSONB NOT NULL DEFAULT '{}'::jsonb,
            earnings JSONB NOT NULL DEFAULT '{}'::jsonb,
            extra JSONB NOT NULL DEFAULT '{}'::jsonb,
            source TEXT NOT NULL DEFAULT 'chat'
                CHECK (source IN ('seed', 'chat', 'ui', 'group')),
            group_id UUID NULL REFERENCES dalton_coverage_groups(group_id) ON DELETE SET NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dalton_coverage_user_ticker UNIQUE (user_id, ticker),
            PRIMARY KEY (user_id, ticker)
        );

        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_user_tier
            ON dalton_coverage (user_id, tier);

        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_user_group
            ON dalton_coverage (user_id, group_id)
            WHERE group_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS dalton_earnings_calendar_mirror (
            user_id TEXT PRIMARY KEY,
            ledger JSONB NOT NULL DEFAULT '{}'::jsonb,
            published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS dalton_earnings_calendar_mirror;
        DROP TABLE IF EXISTS dalton_coverage;
        DROP TABLE IF EXISTS dalton_coverage_groups;
        """
    )
