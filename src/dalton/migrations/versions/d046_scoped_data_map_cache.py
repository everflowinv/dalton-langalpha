"""Dalton d046: capability-scoped daily data map cache (`dalton_data_map_scoped_cache`).

Adds a new cache table keyed by ``(cache_date, capabilities_fingerprint)`` so
Standalone Core and Everflow (or different capability deployments sharing a
database during rolling upgrades) never cross-contaminate cached ``<data_map>``
markdown.

Rolling-upgrade / expand-contract note:
- The legacy ``dalton_data_map_cache`` table (created in ``d011`` with single
  primary key ``cache_date``) is intentionally left untouched so previous
  backend processes can keep reading and writing it during upgrades.

Revision ID: d046
Revises: d047
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d046"
down_revision: str | None = "d047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_map_scoped_cache (
            cache_date               DATE NOT NULL,
            capabilities_fingerprint TEXT NOT NULL,
            rendered_markdown        TEXT NOT NULL,
            payload                  JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (cache_date, capabilities_fingerprint)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_data_map_scoped_cache_updated_at
            ON dalton_data_map_scoped_cache (updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_data_map_scoped_cache")
