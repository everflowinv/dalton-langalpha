"""Add dalton_user_sharing, conversation_threads.user_id, and wakeups.created_by_run_id.

Revision ID: d020
Revises: d019
"""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "d020"
down_revision = "d019"
branch_labels = None
depends_on = None

_BATCH_SIZE = 2000
_MAX_BATCH_RETRIES = 5


def _ensure_nullable_column(
    conn: Any,
    *,
    table: str,
    column: str,
    col_ddl: str,
    expected_types: tuple[str, ...],
    comment: str,
) -> None:
    """Add nullable column if absent, verify type if present, and stamp column comment."""
    row = conn.execute(
        sa.text(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    ).fetchone()
    if row is not None:
        data_type, is_nullable = row[0], row[1]
        if data_type not in expected_types or is_nullable != "YES":
            raise RuntimeError(
                f"Incompatible existing {table}.{column}: "
                f"data_type={data_type}, is_nullable={is_nullable}"
            )
        return

    escaped_comment = comment.replace("'", "''")
    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {col_ddl}"))
    # DDL statements in PostgreSQL/psycopg3 do not support server-side bound parameters;
    # write the column comment as a SQL string literal.
    conn.execute(
        sa.text(f"COMMENT ON COLUMN {table}.{column} IS '{escaped_comment}'")
    )


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


def _migrate_user_cos_sharing(conn: Any) -> None:
    """Populate dalton_user_sharing(cos_rw_enabled=FALSE) for users whose configured non-Home workspaces are all 'hidden'."""
    from src.server.database.home_workspace import is_home_row

    rows = conn.execute(
        sa.text(
            """
            SELECT w.user_id, w.workspace_id, w.status, s.cos_access
            FROM workspaces w
            JOIN dalton_workspace_sharing s ON s.workspace_id = w.workspace_id
            WHERE w.status <> 'deleted'
            """
        )
    ).fetchall()

    by_user: dict[str, list[str]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            user_id = str(row.get("user_id") or "")
            cos_access = str(row.get("cos_access") or "")
            row_dict: dict[str, Any] = dict(row)
        else:
            user_id, ws_id, status, cos_access = row[0], row[1], row[2], row[3]
            user_id = str(user_id or "")
            cos_access = str(cos_access or "")
            row_dict = {
                "user_id": user_id,
                "workspace_id": ws_id,
                "status": status,
            }
        if not user_id or is_home_row(row_dict):
            continue
        by_user.setdefault(user_id, []).append(cos_access)

    disabled_users = [
        uid
        for uid, accesses in by_user.items()
        if accesses and all(acc == "hidden" for acc in accesses)
    ]
    if disabled_users:
        conn.execute(
            sa.text(
                """
                INSERT INTO dalton_user_sharing (user_id, cos_rw_enabled, updated_at)
                VALUES (:user_id, FALSE, NOW())
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            [{"user_id": uid} for uid in disabled_users],
        )


def upgrade() -> None:
    conn = op.get_bind()

    # Phase 1: Short metadata-only transaction
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS dalton_user_sharing (
                user_id        VARCHAR(255) PRIMARY KEY,
                cos_rw_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    # Migrate users who explicitly set ALL their configured non-Home workspaces to cos_access='hidden'
    # using runtime Home workspace identity (src.server.database.home_workspace.is_home_row).
    _migrate_user_cos_sharing(conn)
    _ensure_nullable_column(
        conn,
        table="conversation_threads",
        column="user_id",
        col_ddl="VARCHAR(255)",
        expected_types=("character varying", "text"),
        comment="dalton:d020:thread_owner_user_id",
    )
    _ensure_nullable_column(
        conn,
        table="wakeups",
        column="created_by_run_id",
        col_ddl="UUID",
        expected_types=("uuid", "character varying", "text"),
        comment="dalton:d020:wakeup_parent_run_id",
    )

    # Phase 2 & 3: Out-of-transaction batched resumable backfill + CONCURRENT indexes
    with op.get_context().autocommit_block():
        conn.execute(sa.text("SET lock_timeout = '5s'"))
        try:
            while True:
                for attempt in range(1, _MAX_BATCH_RETRIES + 1):
                    try:
                        result = conn.execute(
                            sa.text(
                                f"""
                                WITH batch AS (
                                    SELECT ct.conversation_thread_id, w.user_id AS owner_id
                                    FROM conversation_threads ct
                                    JOIN workspaces w ON w.workspace_id = ct.workspace_id
                                    WHERE ct.user_id IS NULL
                                    LIMIT {_BATCH_SIZE}
                                )
                                UPDATE conversation_threads ct
                                SET user_id = batch.owner_id
                                FROM batch
                                WHERE ct.conversation_thread_id = batch.conversation_thread_id
                                """
                            )
                        )
                        break
                    except Exception:
                        if attempt >= _MAX_BATCH_RETRIES:
                            raise
                        time.sleep(0.1 * attempt)
                if (result.rowcount or 0) == 0:
                    break
        finally:
            conn.execute(sa.text("SET lock_timeout = '0'"))

        _ensure_concurrent_index(
            conn,
            index_name="idx_conversation_threads_user_updated",
            create_sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_threads_user_updated
                ON conversation_threads (user_id, updated_at DESC)
            """,
        )
        _ensure_concurrent_index(
            conn,
            index_name="idx_conversation_threads_ws_user",
            create_sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_threads_ws_user
                ON conversation_threads (workspace_id, user_id)
            """,
        )


def downgrade() -> None:
    # Non-destructive downgrade: only drop secondary indexes; NEVER drop user_id or created_by_run_id columns.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(
            sa.text("DROP INDEX CONCURRENTLY IF EXISTS idx_conversation_threads_ws_user")
        )
        conn.execute(
            sa.text(
                "DROP INDEX CONCURRENTLY IF EXISTS idx_conversation_threads_user_updated"
            )
        )
