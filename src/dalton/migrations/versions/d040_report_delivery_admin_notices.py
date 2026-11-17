"""Dalton d040: per-automation throttle/coalescing ledger for report-delivery admin notices.

Revision ID: d040
Revises: d039
Create Date: 2026-11-17
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d040"
down_revision: Union[str, None] = "d039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_report_delivery_admin_notices (
            automation_id TEXT PRIMARY KEY,
            last_notified_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_report_delivery_admin_notices")
