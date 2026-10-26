"""d018: per-workspace sharing settings for Chief of Staff and Web UI channels.

Revision ID: d018
Revises: d017
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d018"
down_revision: Union[str, None] = "d017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_workspace_sharing (
            workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            cos_access TEXT NOT NULL DEFAULT 'ro'
                CHECK (cos_access IN ('rw', 'ro', 'hidden')),
            web_access TEXT NOT NULL DEFAULT 'hidden'
                CHECK (web_access IN ('rw', 'ro', 'hidden')),
            updated_by TEXT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS dalton_workspace_sharing;
        """
    )
