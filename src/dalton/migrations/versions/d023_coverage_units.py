"""Dalton d023: coverage units — the sector-layer identity registry (P0).

One row is one coverage subject, company or sector. It names the research
object; the knowledge layer, automations and sharing keep living on the
workspace it may be bound to. This migration only adds the table and its
indexes: no backfill, no change to existing rows. The membership and
cross-reference tables are P2; watch_state/publications ticker identity is P3.

Sector units never carry a ticker (CHECK), and a workspace can hold at most
one ACTIVE unit binding (partial unique index) — an archived unit leaves the
slot free without being deleted.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d023"
down_revision: Union[str, None] = "d022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_coverage_units (
            unit_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind          TEXT NOT NULL CHECK (kind IN ('company', 'sector')),
            owner_user_id TEXT NOT NULL,
            slug          TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT '',
            workspace_id  UUID,
            ticker        TEXT,
            status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived')),
            research_tz   TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dalton_coverage_units_owner_kind_slug
                UNIQUE (owner_user_id, kind, slug),
            CONSTRAINT ck_dalton_coverage_units_sector_no_ticker
                CHECK (kind <> 'sector' OR ticker IS NULL),
            CONSTRAINT ck_dalton_coverage_units_ticker_shape
                CHECK (ticker IS NULL OR ticker = UPPER(BTRIM(ticker)))
        )
        """
    )
    # NULL workspace_id rows are distinct under a unique index, so unbound
    # active units stay unlimited while a bound one is exclusive per workspace.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_coverage_units_active_workspace
            ON dalton_coverage_units (workspace_id)
            WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_units_owner_kind
            ON dalton_coverage_units (owner_user_id, kind, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_units_owner_ticker
            ON dalton_coverage_units (owner_user_id, ticker)
            WHERE ticker IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dalton_coverage_units_owner_ticker")
    op.execute("DROP INDEX IF EXISTS idx_dalton_coverage_units_owner_kind")
    op.execute("DROP INDEX IF EXISTS uq_dalton_coverage_units_active_workspace")
    op.execute("DROP TABLE IF EXISTS dalton_coverage_units")
