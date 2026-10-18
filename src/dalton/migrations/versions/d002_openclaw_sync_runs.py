"""Dalton d002: OpenClaw model catalog sync run history."""

from typing import Sequence, Union

from alembic import op

revision: str = "d002"
down_revision: Union[str, None] = "d001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_openclaw_sync_runs (
            run_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            started_by            VARCHAR(255) NOT NULL,
            dry_run               BOOLEAN NOT NULL,
            forced                BOOLEAN NOT NULL DEFAULT FALSE,
            status                TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','blocked')),
            started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at           TIMESTAMPTZ NULL,
            counts                JSONB NULL,
            diff                  JSONB NULL,
            warnings              JSONB NULL,
            error                 TEXT NULL,
            overlay_stamp         TEXT NULL,
            public_extract_sha256 TEXT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dalton_sync_runs_started "
        "ON dalton_openclaw_sync_runs (started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_openclaw_sync_runs")
