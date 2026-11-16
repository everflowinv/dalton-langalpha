"""Dalton d039: idempotency ledger and published-version registry for monitor panels.

Revision ID: d039
Revises: d038
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d039"
down_revision: Union[str, None] = "d038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_monitor_publish_runs (
            monitor_id UUID NOT NULL
                REFERENCES dalton_monitors(monitor_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL,
            published_seq INTEGER NOT NULL CHECK (published_seq >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (monitor_id, run_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_monitor_versions (
            monitor_id UUID NOT NULL
                REFERENCES dalton_monitors(monitor_id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq >= 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (monitor_id, seq)
        )
        """
    )
    op.execute(
        """
        INSERT INTO dalton_monitor_versions (monitor_id, seq)
        SELECT monitor_id, published_seq
          FROM dalton_monitors
         WHERE published_seq > 0
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_monitor_versions")
    op.execute("DROP TABLE IF EXISTS dalton_monitor_publish_runs")
