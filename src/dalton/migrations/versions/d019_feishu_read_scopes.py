"""Add dalton_feishu_read_scopes whitelist for external-member Feishu doc reads."""

from __future__ import annotations

from alembic import op

revision = "d019"
down_revision = "d018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_feishu_read_scopes (
            space_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            added_by TEXT NOT NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_feishu_read_scopes;")
