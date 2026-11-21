"""Dalton d042: indexes for computer-scoped execution history lookup by thread and workspace.

Revision ID: d042
Revises: d041
Create Date: 2026-11-21
"""

from __future__ import annotations

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d042"
down_revision: Union[str, None] = "d041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EXECUTION_THREAD_INDEX = "idx_automation_executions_thread_created"
AUTOMATION_USER_WORKSPACE_INDEX = "idx_automations_user_workspace"

CREATE_EXECUTION_THREAD_INDEX_SQL = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {EXECUTION_THREAD_INDEX}
    ON automation_executions (conversation_thread_id, created_at DESC, automation_execution_id DESC)
    WHERE conversation_thread_id IS NOT NULL
"""

CREATE_AUTOMATION_USER_WORKSPACE_INDEX_SQL = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {AUTOMATION_USER_WORKSPACE_INDEX}
    ON automations (user_id, workspace_id)
    WHERE workspace_id IS NOT NULL
"""


def _ensure_concurrent_index(
    conn: Any,
    *,
    index_name: str,
    create_sql: str,
) -> None:
    """Drop an invalid index from a previously interrupted CONCURRENTLY build before creating it."""
    inv_row = conn.execute(
        sa.text(
            """
            SELECT i.indisvalid
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE n.nspname = 'public'
              AND c.relname = :index_name
            """
        ),
        {"index_name": index_name},
    ).fetchone()
    if inv_row is not None and not bool(inv_row[0]):
        conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"))
    conn.execute(sa.text(create_sql))


def upgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        _ensure_concurrent_index(
            conn,
            index_name=EXECUTION_THREAD_INDEX,
            create_sql=CREATE_EXECUTION_THREAD_INDEX_SQL,
        )
        _ensure_concurrent_index(
            conn,
            index_name=AUTOMATION_USER_WORKSPACE_INDEX,
            create_sql=CREATE_AUTOMATION_USER_WORKSPACE_INDEX_SQL,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(
            sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {AUTOMATION_USER_WORKSPACE_INDEX}")
        )
        conn.execute(
            sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {EXECUTION_THREAD_INDEX}")
        )
