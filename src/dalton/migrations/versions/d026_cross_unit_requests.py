"""Dalton d026: cross-unit research request ledger (sector layer P2).

One row is one structured evidence request in the Home-coordinated flow
(design §Ⅲ.5): a sector analyst reports a gap, the director (Home) persists
it here, dispatches a company analyst through the EXISTING
``delegate_to_analyst``/``report_back`` machinery, and records the outcome
back on the row. Failed, stopped or permission-missing dispatches are kept as
``gap`` rows — an explicit deficiency, never silently dropped.

``kind='linkage_retry'`` rows are the same ledger reused for propagation
failures (codex r1 #4): a linkage item that could not be written to the target
agenda (typically: no agenda document yet) is persisted as a ``gap`` with the
full retry payload, and re-fired on the target's next valid agenda write or a
director retry.

Why a new table: ``dalton_daily_plans`` is a day-scoped plan snapshot replaced
on every rewrite, automations are recurring schedules, and no existing table
carries a task dependency's lifecycle (open -> dispatched -> fulfilled / gap)
plus its result references.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d026"
down_revision: Union[str, None] = "d025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_cross_unit_requests (
            request_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id     TEXT NOT NULL,
            kind              TEXT NOT NULL DEFAULT 'research_request'
                CHECK (kind IN ('research_request', 'linkage_retry')),
            sector_unit_id    UUID,
            sector_workspace_id UUID,
            target_workspace_id UUID,
            question          TEXT NOT NULL,
            evidence_needed   JSONB NOT NULL DEFAULT '[]',
            completion_condition TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','dispatched','fulfilled','gap','cancelled')),
            dispatch_thread_id UUID,
            result            JSONB NOT NULL DEFAULT '[]',
            payload           JSONB NOT NULL DEFAULT '{}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_cur_owner_status
            ON dalton_cross_unit_requests (owner_user_id, status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_cur_linkage_retry_target
            ON dalton_cross_unit_requests (target_workspace_id, status)
            WHERE kind = 'linkage_retry'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dalton_cur_linkage_retry_target")
    op.execute("DROP INDEX IF EXISTS idx_dalton_cur_owner_status")
    op.execute("DROP TABLE IF EXISTS dalton_cross_unit_requests")
