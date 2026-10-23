"""Dalton d012: `source` column on `dalton_openclaw_sync_runs` for native-model sync runs."""

from typing import Sequence, Union

from alembic import op

revision: str = "d012"
down_revision: Union[str, None] = "d011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Native-model check/sync runs share the table; existing rows stay 'openclaw'.
    op.execute(
        """
        ALTER TABLE dalton_openclaw_sync_runs
            ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'openclaw'
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Native run rows lose their meaning without the column; drop them first
    # so OpenClaw's history stays intact and no orphaned source value survives.
    op.execute("DELETE FROM dalton_openclaw_sync_runs WHERE source = 'native'")
    op.execute(
        """
        ALTER TABLE dalton_openclaw_sync_runs
            DROP COLUMN IF EXISTS source
        """
    )
