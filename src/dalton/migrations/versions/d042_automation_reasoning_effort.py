"""Dalton d042: per-automation reasoning_effort column on automations.

Revision ID: d042
Revises: d041
Create Date: 2026-11-21

Downgrade / Rollback Notes:
- Schema-preserving rollback (reverting application code while keeping d042):
  Before re-deploying newer code, clean only the specific rows verified to have
  had their `llm_model` edited while running older code, scoped by both
  `automation_id` and `config_version`, for example:
      UPDATE automations
         SET reasoning_effort = NULL
       WHERE automation_id = '<verified_automation_id>'::uuid
         AND config_version = <verified_config_version>
         AND reasoning_effort IS NOT NULL;
  Do NOT run a broad wildcard update across `-oauth` tasks (that would wipe
  valid saved efforts on untouched tasks). The executor also ignores stale
  `reasoning_effort` values whenever the automation's current model is a
  built-in Antigravity model.
- Full schema downgrade (`alembic downgrade d041`):
  `downgrade()` drops the `ck_automations_reasoning_effort` constraint and the
  `reasoning_effort` column. Export any non-NULL values first if you plan to
  restore them after re-upgrading (`SELECT automation_id, config_version, llm_model, reasoning_effort FROM automations WHERE reasoning_effort IS NOT NULL`).
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d042"
down_revision: Union[str, None] = "d041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE automations
          ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)
          CONSTRAINT ck_automations_reasoning_effort
          CHECK (
            reasoning_effort IS NULL
            OR reasoning_effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
          )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE automations DROP COLUMN IF EXISTS reasoning_effort")
