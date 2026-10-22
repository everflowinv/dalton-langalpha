"""Dalton d011: data source health status (`dalton_data_source_health`) and daily data map cache (`dalton_data_map_cache`)."""

from __future__ import annotations

from alembic import op

revision = "d011"
down_revision = "d010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_source_health (
            source_id  TEXT PRIMARY KEY,
            category   TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'unprobed'
                CHECK (status IN ('available', 'unavailable', 'unprobed')),
            latency_ms INTEGER,
            latest_date TEXT,
            error_type TEXT,
            detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
            checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_data_source_health_checked_at
            ON dalton_data_source_health (checked_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_map_cache (
            cache_date        DATE PRIMARY KEY,
            rendered_markdown TEXT NOT NULL,
            payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_data_map_cache")
    op.execute("DROP TABLE IF EXISTS dalton_data_source_health")
