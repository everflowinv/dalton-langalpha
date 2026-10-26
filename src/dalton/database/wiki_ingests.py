"""Database queries for the company-wiki ingestion audit ledger (`dalton_wiki_ingests`)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import strip_pg_nul_str

logger = logging.getLogger(__name__)

_INGEST_COLS = """
    ingest_id, user_id, workspace_id, thread_id,
    target_type, target, content_type, doc_date,
    sha256, doc_id, filepath, status, created_at
"""


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in row.items()}


async def create_pending_wiki_ingest(
    *,
    request_id: str | UUID,
    user_id: str,
    workspace_id: str | None,
    thread_id: str | None,
    target_type: str,
    target: str,
    content_type: str,
    doc_date: str,
    sha256: str,
) -> dict[str, Any]:
    """Insert or reset a `pending` row in `dalton_wiki_ingests` keyed by `request_id`."""
    resolved_id = str(request_id)
    ws_str = strip_pg_nul_str(workspace_id) if workspace_id else None
    th_str = strip_pg_nul_str(thread_id) if thread_id else None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_wiki_ingests (
                    ingest_id, user_id, workspace_id, thread_id,
                    target_type, target, content_type, doc_date,
                    sha256, doc_id, filepath, status
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, NULL, NULL, 'pending'
                )
                ON CONFLICT (ingest_id) DO UPDATE SET
                    status = CASE
                        WHEN dalton_wiki_ingests.status = 'done' THEN 'done'
                        ELSE 'pending'
                    END
                RETURNING {_INGEST_COLS}
                """,
                (
                    resolved_id,
                    strip_pg_nul_str(user_id),
                    ws_str,
                    th_str,
                    strip_pg_nul_str(target_type),
                    strip_pg_nul_str(target),
                    strip_pg_nul_str(content_type),
                    strip_pg_nul_str(doc_date),
                    strip_pg_nul_str(sha256),
                ),
            )
            row = await cur.fetchone()
    return _serialize_row(row) or {
        "ingest_id": resolved_id,
        "user_id": user_id,
        "workspace_id": ws_str,
        "thread_id": th_str,
        "target_type": target_type,
        "target": target,
        "content_type": content_type,
        "doc_date": doc_date,
        "sha256": sha256,
        "doc_id": None,
        "filepath": None,
        "status": "pending",
    }


async def update_wiki_ingest_status(
    ingest_id: str | UUID,
    *,
    status: str,
    doc_id: str | int | None = None,
    filepath: str | None = None,
    sha256: str | None = None,
    target_type: str | None = None,
    target: str | None = None,
    content_type: str | None = None,
    doc_date: str | None = None,
) -> dict[str, Any]:
    """Update a `dalton_wiki_ingests` row (e.g. `pending` -> `done` or `failed`)."""
    resolved_id = str(ingest_id)
    doc_id_str = strip_pg_nul_str(str(doc_id)) if doc_id is not None else None
    filepath_str = strip_pg_nul_str(filepath) if filepath is not None else None
    sha_str = strip_pg_nul_str(sha256) if sha256 is not None else None
    tt_str = strip_pg_nul_str(target_type) if target_type is not None else None
    tgt_str = strip_pg_nul_str(target) if target is not None else None
    ct_str = strip_pg_nul_str(content_type) if content_type is not None else None
    dd_str = strip_pg_nul_str(doc_date) if doc_date is not None else None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_wiki_ingests
                SET status = CASE
                        WHEN dalton_wiki_ingests.status = 'done' THEN 'done'
                        ELSE %s
                    END,
                    doc_id = COALESCE(%s, doc_id),
                    filepath = COALESCE(%s, filepath),
                    sha256 = COALESCE(%s, sha256),
                    target_type = COALESCE(%s, target_type),
                    target = COALESCE(%s, target),
                    content_type = COALESCE(%s, content_type),
                    doc_date = COALESCE(%s, doc_date)
                WHERE ingest_id = %s
                  AND status IS DISTINCT FROM 'done'
                RETURNING {_INGEST_COLS}
                """,
                (
                    strip_pg_nul_str(status),
                    doc_id_str,
                    filepath_str,
                    sha_str,
                    tt_str,
                    tgt_str,
                    ct_str,
                    dd_str,
                    resolved_id,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    f"""
                    SELECT {_INGEST_COLS}
                    FROM dalton_wiki_ingests
                    WHERE ingest_id = %s
                    """,
                    (resolved_id,),
                )
                row = await cur.fetchone()
    return _serialize_row(row) or {
        "ingest_id": resolved_id,
        "doc_id": doc_id_str,
        "filepath": filepath_str,
        "sha256": sha_str,
        "status": status,
    }


async def record_wiki_ingest(
    *,
    user_id: str,
    workspace_id: str | None,
    thread_id: str | None,
    target_type: str,
    target: str,
    content_type: str,
    doc_date: str,
    sha256: str,
    doc_id: str | int | None,
    filepath: str | None,
    status: str,
    ingest_id: str | UUID | None = None,
) -> dict[str, Any]:
    """Insert a row into `dalton_wiki_ingests` and return the serialized record."""
    resolved_id = str(ingest_id or uuid4())
    doc_id_str = strip_pg_nul_str(str(doc_id)) if doc_id is not None else None
    filepath_str = strip_pg_nul_str(filepath) if filepath is not None else None
    ws_str = strip_pg_nul_str(workspace_id) if workspace_id else None
    th_str = strip_pg_nul_str(thread_id) if thread_id else None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_wiki_ingests (
                    ingest_id, user_id, workspace_id, thread_id,
                    target_type, target, content_type, doc_date,
                    sha256, doc_id, filepath, status
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING {_INGEST_COLS}
                """,
                (
                    resolved_id,
                    strip_pg_nul_str(user_id),
                    ws_str,
                    th_str,
                    strip_pg_nul_str(target_type),
                    strip_pg_nul_str(target),
                    strip_pg_nul_str(content_type),
                    strip_pg_nul_str(doc_date),
                    strip_pg_nul_str(sha256),
                    doc_id_str,
                    filepath_str,
                    strip_pg_nul_str(status),
                ),
            )
            row = await cur.fetchone()
    return _serialize_row(row) or {
        "ingest_id": resolved_id,
        "user_id": user_id,
        "workspace_id": ws_str,
        "thread_id": th_str,
        "target_type": target_type,
        "target": target,
        "content_type": content_type,
        "doc_date": doc_date,
        "sha256": sha256,
        "doc_id": doc_id_str,
        "filepath": filepath_str,
        "status": status,
    }


async def list_wiki_ingests_by_user(
    user_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch recent `dalton_wiki_ingests` rows for `user_id`."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INGEST_COLS}
                FROM dalton_wiki_ingests
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (strip_pg_nul_str(user_id), max(1, min(limit, 200))),
            )
            rows = await cur.fetchall()
    return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def resolve_user_email(user_id: str) -> str:
    """Look up `email` for `user_id` from `users` table; return empty string if not found."""
    if not user_id:
        return ""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT email FROM users WHERE user_id = %s LIMIT 1",
                    (strip_pg_nul_str(user_id),),
                )
                row = await cur.fetchone()
                if row and isinstance(row.get("email"), str):
                    return row["email"].strip()
    except Exception as exc:
        logger.debug("Failed looking up user email for %s: %s", user_id, type(exc).__name__)
    return ""
