"""Dalton d004: add dalton_source_url and dalton_as_of columns to provenance_records."""

from typing import Sequence, Union

from alembic import op

revision: str = "d004"
down_revision: Union[str, None] = "d003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE IF EXISTS provenance_records
            ADD COLUMN IF NOT EXISTS dalton_source_url TEXT,
            ADD COLUMN IF NOT EXISTS dalton_as_of TEXT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS provenance_records
            DROP COLUMN IF EXISTS dalton_as_of,
            DROP COLUMN IF EXISTS dalton_source_url
        """
    )
