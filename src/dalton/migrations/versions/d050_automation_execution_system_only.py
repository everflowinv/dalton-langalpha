"""Dalton d050: immutable ``llm_model_system_only`` flag on ``automation_executions``.

Captures ``COALESCE((automations.metadata->>'llm_model_system_only')::boolean, FALSE)``
at execution creation time in the same database transaction. Once written on the
execution row, it is never mutated: every firing turn, deployment recovery,
wakeup, command/subagent report, HITL resume, retry, and ``running_thread``
continuation belonging to that execution reads this single column directly.

Revision ID: d050
Revises: d046
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d050"
down_revision: Union[str, None] = "d046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE automation_executions
            ADD COLUMN IF NOT EXISTS llm_model_system_only BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE automation_executions
            DROP COLUMN IF EXISTS llm_model_system_only
        """
    )
