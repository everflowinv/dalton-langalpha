"""d028: workspace and user settings for Team Memory toggle.

Revision ID: d028
Revises: d027
Create Date: 2026-11-05
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d028"
down_revision: Union[str, None] = "d027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_workspace_settings (
            workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            team_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_by TEXT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_user_settings (
            user_id VARCHAR(255) PRIMARY KEY,
            home_team_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS dalton_user_settings;
        """
    )
    op.execute(
        """
        DROP TABLE IF EXISTS dalton_workspace_settings;
        """
    )
