"""Dalton database helpers for safe, transaction-scoped model reference switching."""

from __future__ import annotations

from typing import Any

from src.server.database.pool import get_db_connection


async def reassign_planned_thread_llm_models(
    user_id: str,
    old: str,
    new: str,
    thread_ids: list[str],
    *,
    conn: Any = None,
) -> int:
    """Move only the planned ``thread_ids`` still on ``old`` to ``new`` in one transaction.

    Preserves the ownership and non-deleted workspace filters of
    ``threads_write.reassign_thread_llm_models`` while limiting updates to
    ``thread_ids`` and verifying ``rowcount == len(thread_ids)`` before commit.
    """
    if not thread_ids:
        return 0
    async with get_db_connection(conn) as db_conn, db_conn.transaction():
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_threads t
                SET llm_model = %(new)s
                FROM workspaces w
                WHERE w.workspace_id = t.workspace_id
                  AND (t.user_id = %(user_id)s OR (t.user_id IS NULL AND w.user_id = %(user_id)s))
                  AND w.status != 'deleted'
                  AND t.conversation_thread_id = ANY(%(thread_ids)s)
                  AND t.llm_model = %(old)s
                """,
                {
                    "user_id": user_id,
                    "old": old,
                    "new": new,
                    "thread_ids": list(thread_ids),
                },
            )
            moved = cur.rowcount
            if moved != len(thread_ids):
                raise RuntimeError(
                    f"Thread reassign mismatch for user={user_id} ({old}->{new}): "
                    f"planned={len(thread_ids)}, actual={moved}; rolled back"
                )
            return moved
