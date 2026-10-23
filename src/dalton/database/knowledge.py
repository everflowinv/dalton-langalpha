"""Database queries for company-knowledge document versions (PK.3)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import SafeJson, strip_pg_nul_str

logger = logging.getLogger(__name__)

_KV_COLS = """
    knowledge_version_id, user_id, workspace_id, doc_key, version_no, source,
    content, content_sha256, ops, reason, evidence_refs, warnings,
    schema_valid, edited_path, conversation_thread_id, run_id, tool_call_id,
    files_written_at, created_at
"""


class VersionConflict(Exception):
    """Raised on CAS mismatch when writing a knowledge version."""

    def __init__(
        self,
        doc_key: str,
        expected_version: int | None,
        latest: dict[str, Any] | None,
    ) -> None:
        actual = latest["version_no"] if latest else None
        super().__init__(
            f"Version conflict on {doc_key}: base_version={expected_version}, latest={actual}"
        )
        self.doc_key = doc_key
        self.expected_version = expected_version
        self.latest = latest


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


async def latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
    """Return the latest version row (valid or invalid) for ``(workspace_id, doc_key)``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s AND doc_key = %s
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (workspace_id, doc_key),
            )
            row = await cur.fetchone()
            return _serialize_row(row)


async def latest_valid_version(
    workspace_id: str, doc_key: str
) -> dict[str, Any] | None:
    """Return the latest ``schema_valid=TRUE`` version row for rendering and preflight."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s AND doc_key = %s AND schema_valid = TRUE
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (workspace_id, doc_key),
            )
            row = await cur.fetchone()
            return _serialize_row(row)


async def insert_version(
    *,
    user_id: str,
    workspace_id: str,
    doc_key: str,
    base_version_no: int | None,
    content: dict[str, Any],
    content_sha256: str,
    ops: list[dict[str, Any]] | None = None,
    reason: str,
    evidence_refs: list[dict[str, Any]] | None = None,
    source: str = "tool",
    warnings: list[dict[str, Any]] | None = None,
    schema_valid: bool = True,
    edited_path: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    enforce_base: bool = False,
) -> dict[str, Any]:
    """Insert a new immutable version row under an advisory transaction lock.

    When ``source == 'tool'`` (or ``enforce_base``), enforces CAS against ``base_version_no``;
    reconcile uses ``enforce_base`` so two concurrent passes cannot both append.
    When ``source == 'import'`` and a version already exists, raises ``VersionConflict``.
    When ``source in ('direct_edit', 'publish', 'server')``, appends after the latest version.
    """
    lock_key = f"dalton:kv:{workspace_id}:{doc_key}"
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (lock_key,),
            )
            await cur.execute(
                f"""
                SELECT {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s AND doc_key = %s
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (workspace_id, doc_key),
            )
            latest = _serialize_row(await cur.fetchone())
            latest_no = latest["version_no"] if latest else None

            if source == "tool" or enforce_base:
                if base_version_no != latest_no:
                    raise VersionConflict(doc_key, base_version_no, latest)
            elif source == "import":
                if latest is not None:
                    raise VersionConflict(doc_key, base_version_no, latest)

            next_no = (latest_no or 0) + 1
            await cur.execute(
                f"""
                INSERT INTO dalton_knowledge_versions (
                    user_id, workspace_id, doc_key, version_no, source,
                    content, content_sha256, ops, reason, evidence_refs,
                    warnings, schema_valid, edited_path,
                    conversation_thread_id, run_id, tool_call_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING {_KV_COLS}
                """,
                (
                    strip_pg_nul_str(user_id),
                    workspace_id,
                    strip_pg_nul_str(doc_key),
                    next_no,
                    strip_pg_nul_str(source),
                    SafeJson(content),
                    strip_pg_nul_str(content_sha256),
                    SafeJson(ops) if ops is not None else None,
                    strip_pg_nul_str(reason),
                    SafeJson(evidence_refs or []),
                    SafeJson(warnings or []),
                    bool(schema_valid),
                    strip_pg_nul_str(edited_path) if edited_path else None,
                    thread_id,
                    run_id,
                    strip_pg_nul_str(tool_call_id) if tool_call_id else None,
                ),
            )
            row = await cur.fetchone()
            return _serialize_row(row)  # type: ignore[return-value]


async def mark_files_written(knowledge_version_id: str) -> None:
    """Stamp ``files_written_at = NOW()`` after sandbox files are written."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_knowledge_versions
                SET files_written_at = NOW()
                WHERE knowledge_version_id = %s
                """,
                (knowledge_version_id,),
            )


async def list_versions(
    workspace_id: str,
    doc_key: str,
    limit: int = 50,
    before: int | None = None,
) -> list[dict[str, Any]]:
    """List version rows for ``(workspace_id, doc_key)`` in descending order."""
    capped = max(1, min(limit, 200))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if before is not None:
                await cur.execute(
                    f"""
                    SELECT {_KV_COLS}
                    FROM dalton_knowledge_versions
                    WHERE workspace_id = %s AND doc_key = %s AND version_no < %s
                    ORDER BY version_no DESC
                    LIMIT %s
                    """,
                    (workspace_id, doc_key, before, capped),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_KV_COLS}
                    FROM dalton_knowledge_versions
                    WHERE workspace_id = %s AND doc_key = %s
                    ORDER BY version_no DESC
                    LIMIT %s
                    """,
                    (workspace_id, doc_key, capped),
                )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_version(
    workspace_id: str, doc_key: str, version_no: int
) -> dict[str, Any] | None:
    """Fetch a specific version row by ``(workspace_id, doc_key, version_no)``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s AND doc_key = %s AND version_no = %s
                """,
                (workspace_id, doc_key, version_no),
            )
            row = await cur.fetchone()
            return _serialize_row(row)


async def latest_by_user(user_id: str, doc_key: str) -> list[dict[str, Any]]:
    """Return the latest valid version of ``doc_key`` across all live workspaces owned by ``user_id``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT DISTINCT ON (kv.workspace_id)
                    kv.knowledge_version_id, kv.user_id, kv.workspace_id, kv.doc_key,
                    kv.version_no, kv.source, kv.content, kv.content_sha256,
                    kv.ops, kv.reason, kv.evidence_refs, kv.warnings,
                    kv.schema_valid, kv.edited_path, kv.conversation_thread_id,
                    kv.run_id, kv.tool_call_id, kv.files_written_at, kv.created_at
                FROM dalton_knowledge_versions kv
                JOIN workspaces w ON w.workspace_id = kv.workspace_id
                WHERE kv.user_id = %s
                  AND kv.doc_key = %s
                  AND kv.schema_valid = TRUE
                  AND w.status != 'deleted'
                ORDER BY kv.workspace_id, kv.version_no DESC
                """,
                (user_id, doc_key),
            )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def latest_written_valid_version(
    workspace_id: str, doc_key: str
) -> dict[str, Any] | None:
    """Return the newest valid version whose files were written to the sandbox."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s AND doc_key = %s
                  AND schema_valid = TRUE AND files_written_at IS NOT NULL
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (workspace_id, doc_key),
            )
            return _serialize_row(await cur.fetchone())


async def list_all_latest_for_workspace(
    workspace_id: str, *, valid_only: bool = True
) -> dict[str, dict[str, Any]]:
    """Return a dict mapping ``doc_key -> latest version row`` for one workspace."""
    clause = "AND schema_valid = TRUE" if valid_only else ""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT DISTINCT ON (doc_key) {_KV_COLS}
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s {clause}
                ORDER BY doc_key, version_no DESC
                """,
                (workspace_id,),
            )
            rows = await cur.fetchall()
            return {
                r["doc_key"]: _serialize_row(r)  # type: ignore[misc]
                for r in rows
                if r is not None
            }
