"""Postgres persistence for chat composer large-file uploads.

All cross-worker upload truth lives in ``dalton_chat_file_uploads``. Transitions
use guarded ``UPDATE ... WHERE`` predicates and ``FOR UPDATE SKIP LOCKED``
sweeps so concurrent workers never double-complete, double-abort, or race a
cancelled upload into ``ready``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
import json
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

_USER_UPLOAD_LOCK_NS = 29417

_COLUMN_NAMES = (
    "upload_id",
    "user_id",
    "workspace_id",
    "filename",
    "safe_filename",
    "mime_type",
    "size_bytes",
    "part_size",
    "part_count",
    "object_key",
    "store_upload_id",
    "uploaded_parts",
    "expected_sha256",
    "actual_sha256",
    "state",
    "error",
    "lease_until",
    "used_at",
    "expires_at",
    "cleaned_at",
    "created_at",
    "updated_at",
)

_COLUMNS = ",\n    ".join(_COLUMN_NAMES)
_U_COLUMNS = ",\n    ".join(f"u.{col}" for col in _COLUMN_NAMES)


async def create_upload_guarded(
    *,
    upload_id: str,
    user_id: str,
    workspace_id: str,
    filename: str,
    safe_filename: str,
    mime_type: str,
    size_bytes: int,
    part_size: int,
    part_count: int,
    object_key: str,
    store_upload_id: str,
    expected_sha256: str | None,
    ttl: timedelta,
    max_active_uploads: int,
    max_active_bytes: int,
) -> dict[str, Any] | None:
    """Insert an ``uploading`` row if the user is within active count and byte quotas."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (_USER_UPLOAD_LOCK_NS, user_id),
                )
                await cur.execute(
                    """
                    SELECT
                        COUNT(*)::int AS active_count,
                        COALESCE(SUM(size_bytes), 0)::bigint AS active_bytes
                    FROM dalton_chat_file_uploads
                    WHERE user_id = %s
                      AND state IN ('uploading', 'completing')
                      AND expires_at > NOW()
                    """,
                    (user_id,),
                )
                stats = await cur.fetchone() or {}
                active_count = int(stats.get("active_count") or 0)
                active_bytes = int(stats.get("active_bytes") or 0)
                if (
                    active_count >= max_active_uploads
                    or active_bytes + int(size_bytes) > max_active_bytes
                ):
                    return None

                await cur.execute(
                    f"""
                    INSERT INTO dalton_chat_file_uploads (
                        upload_id,
                        user_id,
                        workspace_id,
                        filename,
                        safe_filename,
                        mime_type,
                        size_bytes,
                        part_size,
                        part_count,
                        object_key,
                        store_upload_id,
                        uploaded_parts,
                        expected_sha256,
                        state,
                        expires_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        '{{}}'::jsonb, %s, 'uploading', NOW() + %s::interval
                    )
                    RETURNING {_COLUMNS}
                    """,
                    (
                        upload_id,
                        user_id,
                        workspace_id,
                        filename,
                        safe_filename,
                        mime_type,
                        int(size_bytes),
                        int(part_size),
                        int(part_count),
                        object_key,
                        store_upload_id,
                        expected_sha256,
                        ttl,
                    ),
                )
                return await cur.fetchone()


async def get_upload(upload_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM dalton_chat_file_uploads
                WHERE upload_id = %s
                """,
                (upload_id,),
            )
            return await cur.fetchone()


async def get_workspace_permission_row(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read ``(workspace_id, user_id, name, status, config)`` including tombstoned rows."""
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id, user_id, name, status, config
                FROM workspaces
                WHERE workspace_id = %s::uuid
                """,
                (workspace_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_uploads_by_ids(upload_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not upload_ids:
        return {}
    unique_ids = list(dict.fromkeys(upload_ids))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM dalton_chat_file_uploads
                WHERE upload_id = ANY(%s)
                """,
                (unique_ids,),
            )
            rows = await cur.fetchall()
    return {str(row["upload_id"]): row for row in rows}


async def record_uploaded_part(
    upload_id: str,
    *,
    part_number: int,
    etag: str,
    size_bytes: int,
    sha256: str,
) -> dict[str, Any] | None:
    """Atomically record one part in ``uploaded_parts`` while the upload is active."""
    part_entry = json.dumps(
        {"etag": etag, "size": int(size_bytes), "sha256": sha256}
    )
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET uploaded_parts = jsonb_set(
                        COALESCE(uploaded_parts, '{{}}'::jsonb),
                        ARRAY[%s],
                        %s::jsonb,
                        true
                    ),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'uploading'
                  AND expires_at > NOW()
                RETURNING {_COLUMNS}
                """,
                (str(int(part_number)), part_entry, upload_id),
            )
            return await cur.fetchone()


async def claim_complete(
    upload_id: str,
    *,
    lease: timedelta,
    expected_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Transition ``uploading`` (or an expired ``completing`` lease) to ``completing``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'completing',
                    lease_until = NOW() + %s::interval,
                    expected_sha256 = COALESCE(%s, expected_sha256),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND expires_at > NOW()
                  AND (
                      state = 'uploading'
                      OR (state = 'completing' AND (lease_until IS NULL OR lease_until < NOW()))
                  )
                RETURNING {_COLUMNS}
                """,
                (lease, expected_sha256, upload_id),
            )
            return await cur.fetchone()


async def reopen_uploading(upload_id: str) -> dict[str, Any] | None:
    """Return a ``completing`` row back to ``uploading`` when parts are missing or transient."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'uploading',
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'completing'
                RETURNING {_COLUMNS}
                """,
                (upload_id,),
            )
            return await cur.fetchone()


async def mark_ready(
    upload_id: str,
    *,
    actual_sha256: str,
    ready_ttl: timedelta,
) -> dict[str, Any] | None:
    """Finalize a ``completing`` upload into ``ready`` with its verified SHA-256."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'ready',
                    actual_sha256 = %s,
                    lease_until = NULL,
                    error = NULL,
                    expires_at = GREATEST(COALESCE(expires_at, NOW()), NOW() + %s::interval),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'completing'
                RETURNING {_COLUMNS}
                """,
                (actual_sha256, ready_ttl, upload_id),
            )
            return await cur.fetchone()


async def mark_failed(
    upload_id: str,
    *,
    error: str,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'failed',
                    error = %s,
                    lease_until = NULL,
                    expires_at = LEAST(COALESCE(expires_at, NOW()), NOW()),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state IN ('uploading', 'completing')
                RETURNING {_COLUMNS}
                """,
                (error[:500], upload_id),
            )
            return await cur.fetchone()


async def abort_upload(
    upload_id: str,
    *,
    user_id: str,
) -> dict[str, Any] | None:
    """Cancel an upload owned by ``user_id`` unless it is already referenced by a chat turn."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'aborted',
                    lease_until = NULL,
                    expires_at = LEAST(COALESCE(expires_at, NOW()), NOW()),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND user_id = %s
                  AND (
                      state IN ('uploading', 'completing', 'failed')
                      OR (state = 'ready' AND used_at IS NULL)
                  )
                RETURNING {_COLUMNS}
                """,
                (upload_id, user_id),
            )
            return await cur.fetchone()


async def mark_used(
    upload_id: str,
    *,
    user_id: str,
    workspace_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Stamp ``used_at`` and clear ``expires_at`` so an accepted upload stays durable for workspace life."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET used_at = COALESCE(used_at, COALESCE(%s::timestamptz, NOW())),
                    expires_at = NULL,
                    updated_at = COALESCE(%s::timestamptz, NOW())
                WHERE upload_id = %s
                  AND user_id = %s
                  AND workspace_id = %s
                  AND state = 'ready'
                  AND cleaned_at IS NULL
                  AND (
                      used_at IS NOT NULL
                      OR (expires_at IS NOT NULL AND expires_at > COALESCE(%s::timestamptz, NOW()))
                  )
                RETURNING {_COLUMNS}
                """,
                (now, now, upload_id, user_id, workspace_id, now),
            )
            return await cur.fetchone()


async def has_retained_workspace_uploads(
    workspace_id: str,
    *,
    conn: Any = None,
) -> bool:
    """True when ``workspace_id`` has at least one active, accepted chat upload."""
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM dalton_chat_file_uploads
                    WHERE workspace_id = %s
                      AND state = 'ready'
                      AND used_at IS NOT NULL
                      AND cleaned_at IS NULL
                ) AS present
                """,
                (workspace_id,),
            )
            row = await cur.fetchone()
            return bool(row and row.get("present"))


async def list_retained_workspace_uploads(
    workspace_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return active accepted chat uploads for ``workspace_id`` ordered chronologically."""
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM dalton_chat_file_uploads
                WHERE workspace_id = %s
                  AND state = 'ready'
                  AND used_at IS NOT NULL
                  AND cleaned_at IS NULL
                ORDER BY created_at ASC, upload_id ASC
                """,
                (workspace_id,),
            )
            return list(await cur.fetchall())


async def retire_uploads_by_ids(
    upload_ids: Sequence[str],
    *,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Retire specific uploads in ``workspace_id`` after explicit file deletion."""
    unique_ids = [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
    if not unique_ids:
        return []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'aborted',
                    lease_until = NULL,
                    expires_at = NOW(),
                    updated_at = NOW()
                WHERE upload_id = ANY(%s)
                  AND workspace_id = %s
                  AND cleaned_at IS NULL
                RETURNING {_COLUMNS}
                """,
                (unique_ids, workspace_id),
            )
            return list(await cur.fetchall())


async def retire_workspace_uploads(workspace_id: str) -> list[dict[str, Any]]:
    """Retire all uncleaned chat uploads belonging to a deleted workspace."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'aborted',
                    lease_until = NULL,
                    expires_at = NOW(),
                    updated_at = NOW()
                WHERE workspace_id = %s
                  AND cleaned_at IS NULL
                RETURNING {_COLUMNS}
                """,
                (workspace_id,),
            )
            return list(await cur.fetchall())


async def claim_cleanup_batch(
    *,
    limit: int = 25,
    claim_lease: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Claim a bounded batch of aborted, failed, expired-unused, or deleted-workspace rows.

    Accepted uploads (``state = 'ready' AND used_at IS NOT NULL``) in live workspaces
    are intentionally excluded so >100 MiB source objects remain durable across
    cold sandbox recreation until explicit file or workspace deletion.
    """
    capped = max(1, min(int(limit), 100))
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    WITH due AS (
                        SELECT u.upload_id
                        FROM dalton_chat_file_uploads AS u
                        WHERE u.cleaned_at IS NULL
                          AND (
                              u.lease_until IS NULL
                              OR u.lease_until < COALESCE(%(now)s::timestamptz, NOW())
                          )
                          AND (
                              u.state IN ('aborted', 'failed')
                              OR (
                                  u.state IN ('uploading', 'completing')
                                  AND u.expires_at IS NOT NULL
                                  AND u.expires_at <= COALESCE(%(now)s::timestamptz, NOW())
                              )
                              OR (
                                  u.state = 'ready'
                                  AND u.used_at IS NULL
                                  AND u.expires_at IS NOT NULL
                                  AND u.expires_at <= COALESCE(%(now)s::timestamptz, NOW())
                              )
                              OR EXISTS (
                                  SELECT 1
                                  FROM workspaces AS w
                                  WHERE w.workspace_id::text = u.workspace_id
                                    AND w.status = 'deleted'
                              )
                          )
                        ORDER BY COALESCE(u.expires_at, u.updated_at) ASC
                        LIMIT %(limit)s
                        FOR UPDATE OF u SKIP LOCKED
                    )
                    UPDATE dalton_chat_file_uploads AS u
                    SET state = CASE
                            WHEN u.state IN ('uploading', 'completing', 'ready') THEN 'aborted'
                            ELSE u.state
                        END,
                        lease_until = COALESCE(%(now)s::timestamptz, NOW()) + %(claim_lease)s::interval,
                        updated_at = COALESCE(%(now)s::timestamptz, NOW())
                    FROM due
                    WHERE u.upload_id = due.upload_id
                    RETURNING {_U_COLUMNS}
                    """,
                    {"now": now, "limit": capped, "claim_lease": claim_lease},
                )
                return list(await cur.fetchall())


async def mark_cleaned(upload_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_chat_file_uploads
                SET cleaned_at = NOW(),
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND cleaned_at IS NULL
                """,
                (upload_id,),
            )
            return cur.rowcount > 0


async def release_cleanup_claim(
    upload_id: str, *, retry_after: timedelta = timedelta(minutes=2)
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_chat_file_uploads
                SET lease_until = NOW() + %s::interval,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND cleaned_at IS NULL
                """,
                (retry_after, upload_id),
            )


async def prune_cleaned_rows(
    *,
    older_than: timedelta = timedelta(days=14),
    limit: int = 200,
    now: datetime | None = None,
) -> int:
    capped = max(1, min(int(limit), 500))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_chat_file_uploads
                WHERE upload_id IN (
                    SELECT upload_id
                    FROM dalton_chat_file_uploads
                    WHERE cleaned_at IS NOT NULL
                      AND cleaned_at < COALESCE(%s::timestamptz, NOW()) - %s::interval
                    ORDER BY cleaned_at ASC
                    LIMIT %s
                )
                """,
                (now, older_than, capped),
            )
            return int(cur.rowcount or 0)
