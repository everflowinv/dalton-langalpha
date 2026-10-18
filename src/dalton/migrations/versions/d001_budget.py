"""Dalton d001: local daily USD budget and reservation tables."""

from typing import Sequence, Union

from alembic import op

revision: str = "d001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_budget_config (
            user_id              VARCHAR(255) PRIMARY KEY,
            daily_cap_usd        NUMERIC(12,6) NOT NULL CHECK (daily_cap_usd >= 0),
            default_task_cap_usd NUMERIC(12,6) NULL CHECK (default_task_cap_usd IS NULL OR default_task_cap_usd > 0),
            timezone             TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_budget_task_caps (
            user_id     VARCHAR(255) NOT NULL,
            scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('workspace','thread')),
            scope_id    UUID NOT NULL,
            cap_usd     NUMERIC(12,6) NOT NULL CHECK (cap_usd > 0),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, scope_kind, scope_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_budget_reservations (
            reservation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          VARCHAR(255) NOT NULL,
            budget_day       DATE NOT NULL,
            run_ref          UUID NOT NULL UNIQUE,
            thread_id        UUID NULL,
            workspace_id     UUID NULL,
            reserved_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
            observed_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (observed_usd >= 0),
            task_cap_usd     NUMERIC(12,6) NULL,
            state            TEXT NOT NULL DEFAULT 'open'
                             CHECK (state IN ('open','released','settled','expired')),
            generation       INTEGER NOT NULL DEFAULT 1,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            renewed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at       TIMESTAMPTZ NOT NULL,
            released_at      TIMESTAMPTZ NULL,
            settled_at       TIMESTAMPTZ NULL,
            settled_usd      NUMERIC(12,6) NULL,
            settle_source    TEXT NULL CHECK (settle_source IN ('usage','conservative'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dalton_resv_user_day "
        "ON dalton_budget_reservations (user_id, budget_day)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dalton_resv_unsettled "
        "ON dalton_budget_reservations (user_id) WHERE state <> 'settled'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_budget_reservations")
    op.execute("DROP TABLE IF EXISTS dalton_budget_task_caps")
    op.execute("DROP TABLE IF EXISTS dalton_budget_config")
