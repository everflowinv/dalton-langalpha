"""Dalton d008: add user_id column and (user_id, plan_date) primary key to dalton_daily_plans."""

from __future__ import annotations

from alembic import op

revision = "d008"
down_revision = "d007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE dalton_daily_plans ADD COLUMN IF NOT EXISTS user_id TEXT"
    )
    op.execute(
        """
        UPDATE dalton_daily_plans
        SET user_id = COALESCE(NULLIF(BTRIM(board_snapshot->>'user_id'), ''), 'local-dev-user'),
            board_snapshot = jsonb_set(
                COALESCE(board_snapshot, '{}'::jsonb),
                '{user_id}',
                to_jsonb(COALESCE(NULLIF(BTRIM(board_snapshot->>'user_id'), ''), 'local-dev-user'::text)),
                true
            )
        WHERE user_id IS NULL
           OR BTRIM(user_id) = ''
           OR NULLIF(BTRIM(board_snapshot->>'user_id'), '') IS NULL
        """
    )
    op.execute(
        "ALTER TABLE dalton_daily_plans ALTER COLUMN user_id SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE dalton_daily_plans DROP CONSTRAINT IF EXISTS dalton_daily_plans_pkey"
    )
    op.execute(
        """
        ALTER TABLE dalton_daily_plans
        ADD CONSTRAINT dalton_daily_plans_pkey PRIMARY KEY (user_id, plan_date)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        DELETE FROM dalton_daily_plans a
        USING dalton_daily_plans b
        WHERE a.plan_date = b.plan_date
          AND (
              a.created_at < b.created_at
              OR (a.created_at = b.created_at AND a.user_id > b.user_id)
          )
        """
    )
    op.execute(
        "ALTER TABLE dalton_daily_plans DROP CONSTRAINT IF EXISTS dalton_daily_plans_pkey"
    )
    op.execute(
        """
        ALTER TABLE dalton_daily_plans
        ADD CONSTRAINT dalton_daily_plans_pkey PRIMARY KEY (plan_date)
        """
    )
    op.execute(
        "ALTER TABLE dalton_daily_plans DROP COLUMN IF EXISTS user_id"
    )
