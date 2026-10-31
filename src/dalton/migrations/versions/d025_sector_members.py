"""Dalton d025: sector-membership query projection (sector layer P2).

``dalton_sector_members`` is a PROJECTION of the ``members`` array of each
sector workspace's ``sector`` knowledge document — never a second authority.
The authoritative list lives in ``data/sector.json`` (versioned through the
knowledge pipeline); this table exists so the board, the director tools and
membership lookups can query memberships without loading and parsing every
sector document. It is rewritten inside the same transaction as the sector
version insert (``database/knowledge.insert_version`` ->
``database/sector_members.sync_members_projection``), so it can always be
rebuilt from the documents alone (``verify``/``rebuild`` in that module).

No foreign keys to ``dalton_coverage_units`` on purpose, matching d023's
style: an archived unit keeps its rows until its document changes or a
rebuild drops them, and nothing here should ever block a unit-status write.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d025"
down_revision: Union[str, None] = "d024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_sector_members (
            sector_unit_id  UUID NOT NULL,
            member_id       TEXT NOT NULL,
            member_name     TEXT NOT NULL,
            ticker          TEXT,
            company_unit_id UUID,
            workspace_created BOOLEAN NOT NULL DEFAULT FALSE,
            scope           TEXT NOT NULL DEFAULT '',
            reason          TEXT NOT NULL DEFAULT '',
            valid_until     DATE,
            position        INTEGER NOT NULL DEFAULT 0,
            synced_version_no INTEGER NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (sector_unit_id, member_id)
        )
        """
    )
    # 反查：这家公司/单元被哪些行业登记为成员（一家公司可属多个行业）。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_sector_members_company_unit
            ON dalton_sector_members (company_unit_id)
            WHERE company_unit_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_sector_members_ticker
            ON dalton_sector_members (ticker)
            WHERE ticker IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dalton_sector_members_ticker")
    op.execute("DROP INDEX IF EXISTS idx_dalton_sector_members_company_unit")
    op.execute("DROP TABLE IF EXISTS dalton_sector_members")
