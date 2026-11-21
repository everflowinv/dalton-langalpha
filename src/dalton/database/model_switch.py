"""Dalton database helpers for safe, transaction-scoped model reference switching."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from src.server.database import pool
from src.server.database.pool import get_db_connection

_UNSET = object()


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


async def set_thread_metadata_key(
    conversation_thread_id: str,
    key: str,
    value: dict[str, Any],
) -> bool:
    """Merge ``{key: value}`` into ``conversation_threads.metadata`` atomically.

    Leaves ``updated_at`` alone so deferred handoff bookkeeping never reorders
    the thread list.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_threads
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                WHERE conversation_thread_id = %s
                """,
                (Json({key: value}), conversation_thread_id),
            )
            return cur.rowcount > 0


async def clear_thread_metadata_key(
    conversation_thread_id: str,
    key: str,
    *,
    expected_version: Any = _UNSET,
) -> bool:
    """Remove ``key`` from ``conversation_threads.metadata`` if present.

    When ``expected_version`` is provided (including ``None`` for unversioned
    markers), deletes only while ``metadata -> key ->> 'version'`` matches it.
    """
    where_extra = ""
    params: list[Any] = [key, conversation_thread_id, key]
    if expected_version is not _UNSET:
        if expected_version is None:
            where_extra = " AND (metadata -> %s ->> 'version') IS NULL"
            params.append(key)
        else:
            where_extra = " AND metadata -> %s ->> 'version' = %s"
            params.extend([key, expected_version])
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE conversation_threads
                SET metadata = metadata - %s
                WHERE conversation_thread_id = %s
                  AND metadata ? %s{where_extra}
                """,
                params,
            )
            return cur.rowcount > 0
