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
import uuid

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
    "pending_claims",
    "expected_sha256",
    "actual_sha256",
    "state",
    "error",
    "claim_token",
    "lease_until",
    "used_at",
    "expires_at",
    "cleaned_at",
    "created_at",
    "updated_at",
)

_COLUMNS = ",\n    ".join(_COLUMN_NAMES)
_U_COLUMNS = ",\n    ".join(f"u.{col}" for col in _COLUMN_NAMES)


def _live_provisional_sql(alias: str = "", now_expr: str = "NOW()") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        "EXISTS ("
        "SELECT 1 "
        f"FROM jsonb_each_text(COALESCE({prefix}pending_claims, '{{}}'::jsonb)) "
        "AS kv(claim_key, claim_until) "
        f"WHERE kv.claim_until::timestamptz > {now_expr}"
        ")"
    )


def _pruned_claims_sql(
    alias: str = "",
    *,
    now_expr: str = "NOW()",
    drop_key_param: str | None = None,
) -> str:
    prefix = f"{alias}." if alias else ""
    key_guard = f"AND kv.key <> {drop_key_param} " if drop_key_param else ""
    return (
        "COALESCE(("
        "SELECT jsonb_object_agg(kv.key, to_jsonb(kv.value)) "
        f"FROM jsonb_each_text(COALESCE({prefix}pending_claims, '{{}}'::jsonb)) AS kv "
        f"WHERE kv.value::timestamptz > {now_expr} {key_guard}"
        "), '{}'::jsonb)"
    )


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
    """Insert an ``uploading`` row if the user is within unaccepted count and byte quotas."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (_USER_UPLOAD_LOCK_NS, user_id),
                )
                await cur.execute(
                    f"""
                    SELECT
                        COUNT(*)::int AS active_count,
                        COALESCE(SUM(u.size_bytes), 0)::bigint AS active_bytes
                    FROM dalton_chat_file_uploads AS u
                    WHERE u.user_id = %s
                      AND u.cleaned_at IS NULL
                      AND (
                          (u.state IN ('uploading', 'completing') AND u.expires_at > NOW())
                          OR (
                              u.state = 'ready'
                              AND u.used_at IS NULL
                              AND (
                                  (u.expires_at IS NOT NULL AND u.expires_at > NOW())
                                  OR {_live_provisional_sql("u", "NOW()")}
                              )
                          )
                      )
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
    claim_token: str | None = None,
) -> dict[str, Any] | None:
    """Transition ``uploading`` (or an expired ``completing`` lease) to ``completing`` with a unique claim token."""
    token = str(claim_token).strip() if claim_token else str(uuid.uuid4())
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'completing',
                    claim_token = %s,
                    lease_until = NOW() + %s::interval,
                    expected_sha256 = COALESCE(expected_sha256, %s),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND expires_at > NOW()
                  AND cleaned_at IS NULL
                  AND (
                      state = 'uploading'
                      OR (state = 'completing' AND (lease_until IS NULL OR lease_until < NOW()))
                  )
                RETURNING {_COLUMNS}
                """,
                (token, lease, expected_sha256, upload_id),
            )
            return await cur.fetchone()


async def reopen_uploading(
    upload_id: str,
    *,
    claim_token: str,
) -> dict[str, Any] | None:
    """Return a ``completing`` row back to ``uploading`` only when ``claim_token`` still owns it."""
    token = str(claim_token or "").strip()
    if not token:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'uploading',
                    claim_token = NULL,
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'completing'
                  AND claim_token = %s
                  AND cleaned_at IS NULL
                RETURNING {_COLUMNS}
                """,
                (upload_id, token),
            )
            return await cur.fetchone()


async def mark_ready(
    upload_id: str,
    *,
    claim_token: str,
    actual_sha256: str,
    ready_ttl: timedelta,
) -> dict[str, Any] | None:
    """Finalize a ``completing`` upload into ``ready`` only when ``claim_token`` still owns it."""
    token = str(claim_token or "").strip()
    if not token:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'ready',
                    actual_sha256 = %s,
                    claim_token = NULL,
                    lease_until = NULL,
                    error = NULL,
                    expires_at = GREATEST(COALESCE(expires_at, NOW()), NOW() + %s::interval),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'completing'
                  AND claim_token = %s
                  AND cleaned_at IS NULL
                RETURNING {_COLUMNS}
                """,
                (actual_sha256, ready_ttl, upload_id, token),
            )
            return await cur.fetchone()


async def mark_failed(
    upload_id: str,
    *,
    claim_token: str,
    error: str,
) -> dict[str, Any] | None:
    """Transition a ``completing`` upload into ``failed`` only when ``claim_token`` still owns it."""
    token = str(claim_token or "").strip()
    if not token:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET state = 'failed',
                    error = %s,
                    claim_token = NULL,
                    lease_until = NULL,
                    expires_at = LEAST(COALESCE(expires_at, NOW()), NOW()),
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state = 'completing'
                  AND claim_token = %s
                RETURNING {_COLUMNS}
                """,
                (error[:500], upload_id, token),
            )
            return await cur.fetchone()


async def abort_upload(
    upload_id: str,
    *,
    user_id: str,
) -> dict[str, Any] | None:
    """Cancel an upload owned by ``user_id`` unless it is claimed or referenced by a chat turn."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH prior AS (
                    SELECT u.upload_id, u.state AS prev_state
                    FROM dalton_chat_file_uploads AS u
                    WHERE u.upload_id = %s
                      AND u.user_id = %s
                      AND (
                          u.state IN ('uploading', 'completing', 'failed')
                          OR (
                              u.state = 'ready'
                              AND u.used_at IS NULL
                              AND NOT {_live_provisional_sql("u", "NOW()")}
                          )
                      )
                    FOR UPDATE
                )
                UPDATE dalton_chat_file_uploads AS u
                SET state = 'aborted',
                    claim_token = NULL,
                    lease_until = NULL,
                    pending_claims = '{{}}'::jsonb,
                    cleaned_at = NULL,
                    expires_at = LEAST(COALESCE(u.expires_at, NOW()), NOW()),
                    updated_at = NOW()
                FROM prior
                WHERE u.upload_id = prior.upload_id
                RETURNING {_U_COLUMNS}, (prior.prev_state = 'completing') AS had_completing
                """,
                (upload_id, user_id),
            )
            return await cur.fetchone()


async def reopen_terminal_cleanup(upload_id: str) -> dict[str, Any] | None:
    """Clear ``cleaned_at`` on a terminal row so late-assembled storage is re-swept if needed."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads
                SET cleaned_at = NULL,
                    claim_token = NULL,
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state IN ('aborted', 'failed')
                RETURNING {_COLUMNS}
                """,
                (upload_id,),
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
    now_expr = "COALESCE(%(now)s::timestamptz, NOW())"
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads AS u
                SET used_at = COALESCE(u.used_at, {now_expr}),
                    expires_at = NULL,
                    pending_claims = {_pruned_claims_sql("u", now_expr=now_expr)},
                    updated_at = {now_expr}
                WHERE u.upload_id = %(upload_id)s
                  AND u.user_id = %(user_id)s
                  AND u.workspace_id = %(workspace_id)s
                  AND u.state = 'ready'
                  AND u.cleaned_at IS NULL
                  AND (
                      u.used_at IS NOT NULL
                      OR (u.expires_at IS NOT NULL AND u.expires_at > {now_expr})
                      OR {_live_provisional_sql("u", now_expr)}
                  )
                RETURNING {_U_COLUMNS}
                """,
                {
                    "now": now,
                    "upload_id": upload_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                },
            )
            return await cur.fetchone()


async def claim_provisional_batch(
    upload_ids: Sequence[str],
    *,
    user_id: str,
    workspace_id: str,
    claim_key: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Atomically place a bounded provisional claim on all ``upload_ids`` without mutating ``used_at`` or ``expires_at``."""
    unique_ids = [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
    if not unique_ids:
        return {}
    key = str(claim_key or "").strip()
    if not key:
        return None
    now_expr = "COALESCE(%(now)s::timestamptz, NOW())"
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT u.upload_id
                    FROM dalton_chat_file_uploads AS u
                    WHERE u.upload_id = ANY(%(ids)s)
                      AND u.user_id = %(user_id)s
                      AND u.workspace_id = %(workspace_id)s
                      AND u.state = 'ready'
                      AND u.cleaned_at IS NULL
                      AND (
                          u.used_at IS NOT NULL
                          OR (u.expires_at IS NOT NULL AND u.expires_at > {now_expr})
                          OR {_live_provisional_sql("u", now_expr)}
                      )
                    ORDER BY u.upload_id
                    FOR UPDATE
                    """,
                    {
                        "ids": unique_ids,
                        "user_id": user_id,
                        "workspace_id": workspace_id,
                        "now": now,
                    },
                )
                locked = await cur.fetchall()
                if len(locked) != len(unique_ids):
                    return None
                await cur.execute(
                    f"""
                    UPDATE dalton_chat_file_uploads AS u
                    SET pending_claims = {_pruned_claims_sql("u", now_expr=now_expr, drop_key_param="%(claim_key)s")}
                            || jsonb_build_object(
                                %(claim_key)s::text,
                                to_jsonb({now_expr} + %(ttl)s::interval)
                            ),
                        updated_at = {now_expr}
                    WHERE u.upload_id = ANY(%(ids)s)
                    RETURNING {_U_COLUMNS}
                    """,
                    {
                        "ids": unique_ids,
                        "claim_key": key,
                        "ttl": ttl,
                        "now": now,
                    },
                )
                rows = await cur.fetchall()
                return {str(r["upload_id"]): dict(r) for r in rows}


async def confirm_provisional_claims(
    upload_ids: Sequence[str],
    *,
    user_id: str,
    workspace_id: str,
    claim_key: str | None = None,
    conn: Any = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Finalize durable retention (``used_at``, ``expires_at = NULL``) in the caller's admission transaction."""
    unique_ids = [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
    if not unique_ids:
        return {}

    async def _run_on_conn(active_conn: Any) -> dict[str, dict[str, Any]] | None:
        now_expr = "COALESCE(%(now)s::timestamptz, NOW())"
        clean_key = str(claim_key).strip() if claim_key else None
        pruned_expr = (
            _pruned_claims_sql("u", now_expr=now_expr, drop_key_param="%(claim_key)s")
            if clean_key
            else _pruned_claims_sql("u", now_expr=now_expr)
        )
        async with active_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT u.upload_id
                FROM dalton_chat_file_uploads AS u
                WHERE u.upload_id = ANY(%(ids)s)
                  AND u.user_id = %(user_id)s
                  AND u.workspace_id = %(workspace_id)s
                  AND u.state = 'ready'
                  AND u.cleaned_at IS NULL
                  AND (
                      u.used_at IS NOT NULL
                      OR (u.expires_at IS NOT NULL AND u.expires_at > {now_expr})
                      OR {_live_provisional_sql("u", now_expr)}
                  )
                ORDER BY u.upload_id
                FOR UPDATE
                """,
                {
                    "ids": unique_ids,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "now": now,
                },
            )
            locked = await cur.fetchall()
            if len(locked) != len(unique_ids):
                return None
            await cur.execute(
                f"""
                UPDATE dalton_chat_file_uploads AS u
                SET used_at = COALESCE(u.used_at, {now_expr}),
                    expires_at = NULL,
                    pending_claims = {pruned_expr},
                    updated_at = {now_expr}
                WHERE u.upload_id = ANY(%(ids)s)
                RETURNING {_U_COLUMNS}
                """,
                {
                    "ids": unique_ids,
                    "claim_key": clean_key,
                    "now": now,
                },
            )
            rows = await cur.fetchall()
            return {str(r["upload_id"]): dict(r) for r in rows}

    if conn is not None:
        return await _run_on_conn(conn)
    async with get_db_connection() as owned:
        async with owned.transaction():
            return await _run_on_conn(owned)


async def release_provisional_claims(
    *,
    user_id: str,
    claim_key: str,
    request_key: str | None = None,
    upload_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Drop ``claim_key`` from ``pending_claims`` without altering durable ``used_at`` retention."""
    del request_key
    key = str(claim_key or "").strip()
    if not key:
        return 0
    unique_ids = (
        [str(uid) for uid in dict.fromkeys(upload_ids) if uid]
        if upload_ids is not None
        else None
    )
    if unique_ids is not None and not unique_ids:
        return 0
    now_expr = "COALESCE(%(now)s::timestamptz, NOW())"
    id_filter = "AND u.upload_id = ANY(%(ids)s)" if unique_ids is not None else ""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT u.upload_id
                    FROM dalton_chat_file_uploads AS u
                    WHERE u.user_id = %(user_id)s
                      {id_filter}
                      AND COALESCE(u.pending_claims, '{{}}'::jsonb) ? %(claim_key)s
                    ORDER BY u.upload_id
                    FOR UPDATE
                    """,
                    {
                        "user_id": user_id,
                        "ids": unique_ids,
                        "claim_key": key,
                    },
                )
                locked_ids = [str(r["upload_id"]) for r in await cur.fetchall()]
                if not locked_ids:
                    return 0

                await cur.execute(
                    f"""
                    UPDATE dalton_chat_file_uploads AS u
                    SET pending_claims = {_pruned_claims_sql("u", now_expr=now_expr, drop_key_param="%(claim_key)s")},
                        updated_at = {now_expr}
                    WHERE u.upload_id = ANY(%(locked_ids)s)
                    """,
                    {
                        "locked_ids": locked_ids,
                        "claim_key": key,
                        "now": now,
                    },
                )
                return int(cur.rowcount or 0)


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
                WITH prior AS (
                    SELECT upload_id, state AS prev_state
                    FROM dalton_chat_file_uploads
                    WHERE upload_id = ANY(%s)
                      AND workspace_id = %s
                      AND cleaned_at IS NULL
                    FOR UPDATE
                )
                UPDATE dalton_chat_file_uploads AS u
                SET state = 'aborted',
                    claim_token = NULL,
                    lease_until = NULL,
                    pending_claims = '{{}}'::jsonb,
                    cleaned_at = NULL,
                    expires_at = NOW(),
                    updated_at = NOW()
                FROM prior
                WHERE u.upload_id = prior.upload_id
                RETURNING {_U_COLUMNS}, (prior.prev_state = 'completing') AS had_completing
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
                WITH prior AS (
                    SELECT upload_id, state AS prev_state
                    FROM dalton_chat_file_uploads
                    WHERE workspace_id = %s
                      AND cleaned_at IS NULL
                    FOR UPDATE
                )
                UPDATE dalton_chat_file_uploads AS u
                SET state = 'aborted',
                    claim_token = NULL,
                    lease_until = NULL,
                    pending_claims = '{{}}'::jsonb,
                    cleaned_at = NULL,
                    expires_at = NOW(),
                    updated_at = NOW()
                FROM prior
                WHERE u.upload_id = prior.upload_id
                RETURNING {_U_COLUMNS}, (prior.prev_state = 'completing') AS had_completing
                """,
                (workspace_id,),
            )
            return list(await cur.fetchall())


async def claim_cleanup_batch(
    *,
    limit: int = 25,
    claim_lease: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
    claim_token: str | None = None,
) -> list[dict[str, Any]]:
    """Claim a bounded batch of aborted, failed, expired-unused, or deleted-workspace rows.

    Accepted uploads (``state = 'ready' AND used_at IS NOT NULL``) in live workspaces
    are intentionally excluded so >100 MiB source objects remain durable across
    cold sandbox recreation until explicit file or workspace deletion.
    """
    capped = max(1, min(int(limit), 100))
    token = str(claim_token).strip() if claim_token else str(uuid.uuid4())
    now_expr = "COALESCE(%(now)s::timestamptz, NOW())"
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
                              OR u.lease_until < {now_expr}
                          )
                          AND (
                              u.state IN ('aborted', 'failed')
                              OR (
                                  u.state IN ('uploading', 'completing')
                                  AND u.expires_at IS NOT NULL
                                  AND u.expires_at <= {now_expr}
                              )
                              OR (
                                  u.state = 'ready'
                                  AND u.used_at IS NULL
                                  AND u.expires_at IS NOT NULL
                                  AND u.expires_at <= {now_expr}
                                  AND NOT {_live_provisional_sql("u", now_expr)}
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
                        claim_token = %(claim_token)s,
                        pending_claims = '{{}}'::jsonb,
                        lease_until = {now_expr} + %(claim_lease)s::interval,
                        updated_at = {now_expr}
                    FROM due
                    WHERE u.upload_id = due.upload_id
                    RETURNING {_U_COLUMNS}
                    """,
                    {
                        "now": now,
                        "limit": capped,
                        "claim_lease": claim_lease,
                        "claim_token": token,
                    },
                )
                return list(await cur.fetchall())


async def mark_cleaned(
    upload_id: str,
    *,
    claim_token: str | None = None,
) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if claim_token is not None:
                token = str(claim_token).strip()
                if not token:
                    return False
                await cur.execute(
                    """
                    UPDATE dalton_chat_file_uploads
                    SET cleaned_at = NOW(),
                        claim_token = NULL,
                        lease_until = NULL,
                        updated_at = NOW()
                    WHERE upload_id = %s
                      AND state IN ('aborted', 'failed')
                      AND cleaned_at IS NULL
                      AND claim_token = %s
                    """,
                    (upload_id, token),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_chat_file_uploads
                    SET cleaned_at = NOW(),
                        claim_token = NULL,
                        lease_until = NULL,
                        updated_at = NOW()
                    WHERE upload_id = %s
                      AND state IN ('aborted', 'failed')
                      AND cleaned_at IS NULL
                    """,
                    (upload_id,),
                )
            return cur.rowcount > 0


async def release_cleanup_claim(
    upload_id: str,
    *,
    claim_token: str,
    retry_after: timedelta = timedelta(minutes=2),
) -> None:
    token = str(claim_token or "").strip()
    if not token:
        return
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_chat_file_uploads
                SET claim_token = NULL,
                    lease_until = NOW() + %s::interval,
                    updated_at = NOW()
                WHERE upload_id = %s
                  AND state IN ('aborted', 'failed')
                  AND cleaned_at IS NULL
                  AND claim_token = %s
                """,
                (retry_after, upload_id, token),
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
