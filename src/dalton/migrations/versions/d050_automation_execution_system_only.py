"""Dalton d050: immutable model-config snapshot columns on ``automation_executions``.

Captures ``automations.llm_model``, ``automations.reasoning_effort``, and
``COALESCE((automations.metadata->>'llm_model_system_only')::boolean, FALSE)``
from the same locked read at execution creation time in the same database
transaction. Once written on the execution row, the snapshot is never mutated:
every firing turn (initial or after waiting for a busy thread), deployment
recovery, wakeup, command/subagent report, HITL resume, retry, and
``running_thread`` continuation belonging to that execution reads these three
columns directly.

Revision ID: d050
Revises: d052
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d050"
down_revision: Union[str, None] = "d052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE automation_executions
            ADD COLUMN IF NOT EXISTS llm_model TEXT,
            ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16),
            ADD COLUMN IF NOT EXISTS llm_model_system_only BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS snapshot_taken BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    # Backfill existing execution rows and mark snapshot_taken = TRUE:
    # - Executions that already have runs (linked via conversation_response_id,
    #   metadata.automation_execution_id, metadata.automation_source_execution_id,
    #   or metadata.automation_delivery.id) take their earliest run by
    #   (created_at ASC, run_seq ASC).
    # - Executions with no runs yet take the parent automation's current config.
    # - Any rows inserted by an older backend during a rolling upgrade after this
    #   migration commits keep snapshot_taken = FALSE and are lazily snapshotted
    #   on first read under a row lock.
    op.execute(
        """
        UPDATE automation_executions AS e
        SET llm_model = src.llm_model,
            reasoning_effort = src.reasoning_effort,
            llm_model_system_only = src.llm_model_system_only,
            snapshot_taken = TRUE
        FROM (
            SELECT
                e0.automation_execution_id,
                COALESCE(
                    first_run.run_llm_model,
                    NULLIF(BTRIM(a.llm_model), '')
                ) AS llm_model,
                CASE
                    WHEN COALESCE(first_run.effort_configured, FALSE)
                        THEN first_run.run_reasoning_effort
                    ELSE COALESCE(
                        first_run.run_reasoning_effort,
                        LEFT(NULLIF(BTRIM(a.reasoning_effort), ''), 16)
                    )
                END AS reasoning_effort,
                CASE
                    WHEN first_run.run_system_only IS NOT NULL
                        THEN first_run.run_system_only
                    WHEN LOWER(COALESCE(a.metadata->>'llm_model_system_only', '')) IN ('true', 'false')
                        THEN (a.metadata->>'llm_model_system_only')::boolean
                    ELSE FALSE
                END AS llm_model_system_only
            FROM automation_executions AS e0
            JOIN automations AS a
              ON a.automation_id = e0.automation_id
            LEFT JOIN LATERAL (
                SELECT
                    COALESCE(
                        NULLIF(BTRIM(r.metadata->>'llm_model'), ''),
                        NULLIF(BTRIM(q.metadata->>'llm_model'), '')
                    ) AS run_llm_model,
                    (
                        r.metadata ? 'automation_reasoning_effort_configured'
                        AND LOWER(COALESCE(r.metadata->>'automation_reasoning_effort_configured', '')) = 'true'
                    ) AS effort_configured,
                    LEFT(
                        COALESCE(
                            NULLIF(BTRIM(r.metadata->>'automation_reasoning_effort'), ''),
                            CASE
                                WHEN r.metadata ? 'automation_reasoning_effort_configured'
                                     AND LOWER(COALESCE(r.metadata->>'automation_reasoning_effort_configured', '')) = 'true'
                                    THEN NULL
                                ELSE NULLIF(BTRIM(r.metadata->>'reasoning_effort'), '')
                            END
                        ),
                        16
                    ) AS run_reasoning_effort,
                    CASE
                        WHEN LOWER(COALESCE(r.metadata->>'llm_model_system_only', '')) IN ('true', 'false')
                            THEN (r.metadata->>'llm_model_system_only')::boolean
                        ELSE NULL
                    END AS run_system_only
                FROM conversation_responses AS r
                LEFT JOIN conversation_queries AS q
                  ON q.conversation_thread_id = r.conversation_thread_id
                 AND q.turn_index = r.turn_index
                WHERE r.conversation_response_id = e0.conversation_response_id
                   OR (
                       (e0.conversation_thread_id IS NULL OR r.conversation_thread_id = e0.conversation_thread_id)
                       AND (
                           r.metadata->>'automation_execution_id' = e0.automation_execution_id::text
                           OR r.metadata->>'automation_source_execution_id' = e0.automation_execution_id::text
                           OR NULLIF(split_part(COALESCE(r.metadata->'automation_delivery'->>'id', ''), '.', 1), '') = e0.automation_execution_id::text
                       )
                   )
                ORDER BY r.created_at ASC, r.run_seq ASC
                LIMIT 1
            ) AS first_run ON TRUE
        ) AS src
        WHERE e.automation_execution_id = src.automation_execution_id
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE automation_executions
            DROP COLUMN IF EXISTS snapshot_taken,
            DROP COLUMN IF EXISTS llm_model_system_only,
            DROP COLUMN IF EXISTS reasoning_effort,
            DROP COLUMN IF EXISTS llm_model
        """
    )
