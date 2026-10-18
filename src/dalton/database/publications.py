"""Database queries for publication requests, immutable publications, and archived file bytes (PK.8)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import SafeJson, strip_pg_nul_str

_REQ_COLS = """
    publish_request_id, user_id, workspace_id, conversation_thread_id,
    tool_call_id, kind, title, slug, draft_path, attachments, bundle_sha256,
    updates_thesis, thesis_version_no, status, audit, review,
    decided_by, decided_at, decision_message, publication_id, error,
    created_at, updated_at
"""

_PUB_COLS = """
    publication_id, publish_request_id, user_id, workspace_id, ticker,
    kind, title, published_date, files, bundle_sha256, sources,
    audit_summary, reviewer, approver_user_id, approved_at,
    thesis_version_no, supersedes_publication_id, created_at
"""

_ALLOWED_TRANSITION_COLS = frozenset(
    {
        "bundle_sha256",
        "attachments",
        "updates_thesis",
        "thesis_version_no",
        "audit",
        "review",
        "decided_by",
        "decided_at",
        "decision_message",
        "publication_id",
        "error",
    }
)


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: (str(v) if isinstance(v, UUID) else v)
        for k, v in row.items()
    }


async def get_or_create_request(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    tool_call_id: str,
    kind: str,
    title: str,
    slug: str,
    draft_path: str,
    attachments: list[dict[str, Any]] | None = None,
    bundle_sha256: str,
    updates_thesis: bool = False,
    thesis_version_no: int | None = None,
    status: str = "auditing",
) -> tuple[dict[str, Any], bool]:
    """Idempotently create or fetch a publish request by ``(conversation_thread_id, tool_call_id)``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_publish_requests (
                    user_id, workspace_id, conversation_thread_id, tool_call_id,
                    kind, title, slug, draft_path, attachments, bundle_sha256,
                    updates_thesis, thesis_version_no, status
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (conversation_thread_id, tool_call_id)
                DO UPDATE SET updated_at = dalton_publish_requests.updated_at
                RETURNING {_REQ_COLS}, (xmax = 0) AS inserted
                """,
                (
                    strip_pg_nul_str(user_id),
                    workspace_id,
                    thread_id,
                    strip_pg_nul_str(tool_call_id),
                    strip_pg_nul_str(kind),
                    strip_pg_nul_str(title),
                    strip_pg_nul_str(slug),
                    strip_pg_nul_str(draft_path),
                    SafeJson(attachments or []),
                    strip_pg_nul_str(bundle_sha256),
                    bool(updates_thesis),
                    thesis_version_no,
                    strip_pg_nul_str(status),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            created = bool(row.pop("inserted", False))
            return _serialize_row(row), created  # type: ignore[return-value]


async def get_request(publish_request_id: str) -> dict[str, Any] | None:
    """Fetch a publish request by primary key."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REQ_COLS}
                FROM dalton_publish_requests
                WHERE publish_request_id = %s
                """,
                (publish_request_id,),
            )
            return _serialize_row(await cur.fetchone())


async def transition(
    publish_request_id: str,
    from_statuses: Sequence[str],
    to: str,
    **cols: Any,
) -> dict[str, Any] | None:
    """Guarded state transition on ``dalton_publish_requests``."""
    sets = ["status = %s", "updated_at = NOW()"]
    params: list[Any] = [strip_pg_nul_str(to)]
    for col, val in cols.items():
        if col not in _ALLOWED_TRANSITION_COLS:
            raise ValueError(f"Unsupported transition column: {col!r}")
        sets.append(f"{col} = %s")
        if col in ("attachments", "audit", "review") and val is not None:
            params.append(SafeJson(val))
        elif isinstance(val, str):
            params.append(strip_pg_nul_str(val))
        else:
            params.append(val)

    params.append(publish_request_id)
    params.append(list(from_statuses))
    set_sql = ", ".join(sets)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_publish_requests
                SET {set_sql}
                WHERE publish_request_id = %s AND status = ANY(%s::text[])
                RETURNING {_REQ_COLS}
                """,
                tuple(params),
            )
            return _serialize_row(await cur.fetchone())


async def consume_approval(
    publish_request_id: str, bundle_sha256: str
) -> dict[str, Any] | None:
    """Atomically claim an ``approved`` request whose ``bundle_sha256`` matches (approve-once, execute-once)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_publish_requests
                SET status = 'publishing', updated_at = NOW()
                WHERE publish_request_id = %s
                  AND status = 'approved'
                  AND bundle_sha256 = %s
                RETURNING {_REQ_COLS}
                """,
                (publish_request_id, bundle_sha256),
            )
            return _serialize_row(await cur.fetchone())


async def insert_publication(
    *,
    publish_request_id: str,
    user_id: str,
    workspace_id: str,
    ticker: str,
    kind: str,
    title: str,
    published_date: date | str,
    files: list[dict[str, Any]],
    file_blobs: list[dict[str, Any]],
    bundle_sha256: str,
    sources: list[dict[str, Any]] | dict[str, Any],
    audit_summary: dict[str, Any],
    reviewer: dict[str, Any],
    approver_user_id: str,
    approved_at: datetime | str,
    thesis_version_no: int | None = None,
    supersedes_publication_id: str | None = None,
) -> dict[str, Any]:
    """Insert ``dalton_publications``, ``dalton_publication_files``, and mark request ``published`` in one transaction."""
    if isinstance(published_date, str):
        pub_date = date.fromisoformat(published_date[:10])
    else:
        pub_date = published_date

    if isinstance(approved_at, str):
        app_dt = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    else:
        app_dt = approved_at

    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            # Check idempotency if already inserted for this publish_request_id
            await cur.execute(
                f"""
                SELECT {_PUB_COLS}
                FROM dalton_publications
                WHERE publish_request_id = %s
                """,
                (publish_request_id,),
            )
            existing = await cur.fetchone()
            if existing is not None:
                return _serialize_row(existing)  # type: ignore[return-value]

            await cur.execute(
                f"""
                INSERT INTO dalton_publications (
                    publish_request_id, user_id, workspace_id, ticker,
                    kind, title, published_date, files, bundle_sha256,
                    sources, audit_summary, reviewer, approver_user_id,
                    approved_at, thesis_version_no, supersedes_publication_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING {_PUB_COLS}
                """,
                (
                    publish_request_id,
                    strip_pg_nul_str(user_id),
                    workspace_id,
                    strip_pg_nul_str(ticker),
                    strip_pg_nul_str(kind),
                    strip_pg_nul_str(title),
                    pub_date,
                    SafeJson(files),
                    strip_pg_nul_str(bundle_sha256),
                    SafeJson(sources),
                    SafeJson(audit_summary),
                    SafeJson(reviewer),
                    strip_pg_nul_str(approver_user_id),
                    app_dt,
                    thesis_version_no,
                    supersedes_publication_id,
                ),
            )
            pub_row = await cur.fetchone()
            assert pub_row is not None
            pub_id = str(pub_row["publication_id"])

            for fb in file_blobs:
                await cur.execute(
                    """
                    INSERT INTO dalton_publication_files (
                        publication_id, path, sha256, mime, content
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        pub_id,
                        strip_pg_nul_str(fb["path"]),
                        strip_pg_nul_str(fb["sha256"]),
                        strip_pg_nul_str(fb.get("mime") or "application/octet-stream"),
                        bytes(fb["content"]),
                    ),
                )

            await cur.execute(
                """
                UPDATE dalton_publish_requests
                SET status = 'published', publication_id = %s, updated_at = NOW()
                WHERE publish_request_id = %s
                """,
                (pub_id, publish_request_id),
            )
            return _serialize_row(pub_row)  # type: ignore[return-value]


async def list_publications(
    workspace_id: str,
    *,
    user_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List publications for ``workspace_id`` ordered by ``published_date DESC``."""
    capped = max(1, min(limit, 200))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if user_id:
                await cur.execute(
                    f"""
                    SELECT {_PUB_COLS}
                    FROM dalton_publications
                    WHERE workspace_id = %s AND user_id = %s
                    ORDER BY published_date DESC, created_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, user_id, capped),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_PUB_COLS}
                    FROM dalton_publications
                    WHERE workspace_id = %s
                    ORDER BY published_date DESC, created_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, capped),
                )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_publication(
    publication_id: str, *, user_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch a publication by ID."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if user_id:
                await cur.execute(
                    f"""
                    SELECT {_PUB_COLS}
                    FROM dalton_publications
                    WHERE publication_id = %s AND user_id = %s
                    """,
                    (publication_id, user_id),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_PUB_COLS}
                    FROM dalton_publications
                    WHERE publication_id = %s
                    """,
                    (publication_id,),
                )
            return _serialize_row(await cur.fetchone())


async def list_publication_files(workspace_id: str) -> list[dict[str, Any]]:
    """List ``(publication_id, path, sha256, mime)`` for all published files in ``workspace_id``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT pf.publication_id, pf.path, pf.sha256, pf.mime
                FROM dalton_publication_files pf
                JOIN dalton_publications p ON p.publication_id = pf.publication_id
                WHERE p.workspace_id = %s
                ORDER BY p.created_at DESC
                """,
                (workspace_id,),
            )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def list_pending(
    user_id: str, status: str = "awaiting_pm"
) -> list[dict[str, Any]]:
    """List publish requests for ``user_id`` matching ``status``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REQ_COLS}
                FROM dalton_publish_requests
                WHERE user_id = %s AND status = %s
                ORDER BY created_at DESC
                """,
                (user_id, status),
            )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]
