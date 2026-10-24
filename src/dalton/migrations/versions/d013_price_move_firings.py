"""Dalton d013: per-trading-day deduplication table (`dalton_price_move_firings`) for stock-move price triggers."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d013"
down_revision: Union[str, None] = "d012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_price_move_firings (
            user_id       TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            direction     TEXT NOT NULL,
            trading_date  TEXT NOT NULL,
            automation_id TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, symbol, direction, trading_date)
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_price_move_firings")
