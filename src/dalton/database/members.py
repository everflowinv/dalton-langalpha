"""Raw psycopg3 SQL queries for Dalton team members (`dalton_members`) and invites (`dalton_invites`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


def _normalize_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("invite_id") is not None:
        out["invite_id"] = str(out["invite_id"])
    return out


async def list_member_emails(*, conn: Any = None) -> set[str]:
    """Return all lowercase member emails stored in ``dalton_members``."""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT email FROM dalton_members")
            rows = await cur.fetchall()
            return {
                str(r["email"]).strip().lower()
                for r in rows
                if r and r.get("email") and str(r["email"]).strip()
            }


async def get_member_by_email(
    email: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return the ``dalton_members`` row for ``email`` (case-insensitive via lowercase PK)."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT email, username, source, invited_by_user_id, created_at
                FROM dalton_members
                WHERE email = %s
                """,
                (normalized,),
            )
            row = await cur.fetchone()
            return _normalize_row(row)


async def get_member_by_username(
    username: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return the ``dalton_members`` row for ``username`` (lowercase)."""
    normalized = str(username or "").strip().lower()
    if not normalized:
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT email, username, source, invited_by_user_id, created_at
                FROM dalton_members
                WHERE username = %s
                """,
                (normalized,),
            )
            row = await cur.fetchone()
            return _normalize_row(row)


async def insert_member(
    *,
    email: str,
    username: str | None,
    source: str,
    invited_by_user_id: str | None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert a new row into ``dalton_members``."""
    normalized_email = str(email or "").strip().lower()
    normalized_username = str(username).strip().lower() if username is not None else None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_members (email, username, source, invited_by_user_id)
                VALUES (%s, %s, %s, %s)
                RETURNING email, username, source, invited_by_user_id, created_at
                """,
                (normalized_email, normalized_username, source, invited_by_user_id),
            )
            row = await cur.fetchone()
            return _normalize_row(row) or {
                "email": normalized_email,
                "username": normalized_username,
                "source": source,
                "invited_by_user_id": invited_by_user_id,
            }


async def insert_invite(
    *,
    invite_id: str | None = None,
    token_sha256: str,
    created_by_user_id: str,
    expires_at: datetime,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert a new single-use invite into ``dalton_invites`` (storing only ``token_sha256``)."""
    resolved_id = str(uuid.UUID(invite_id)) if invite_id else str(uuid.uuid4())
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_invites (
                    invite_id, token_sha256, created_by_user_id, expires_at
                )
                VALUES (%s, %s, %s, %s)
                RETURNING
                    invite_id::text AS invite_id,
                    token_sha256,
                    created_by_user_id,
                    created_at,
                    expires_at,
                    redeeming_username,
                    redeeming_at,
                    used_at,
                    used_by_email,
                    supabase_user_id,
                    revoked_at
                """,
                (resolved_id, token_sha256, created_by_user_id, expires_at),
            )
            row = await cur.fetchone()
            return _normalize_row(row) or {
                "invite_id": resolved_id,
                "token_sha256": token_sha256,
                "created_by_user_id": created_by_user_id,
                "expires_at": expires_at,
                "redeeming_username": None,
                "redeeming_at": None,
                "used_at": None,
                "used_by_email": None,
                "supabase_user_id": None,
                "revoked_at": None,
            }


async def list_invites(*, conn: Any = None) -> list[dict[str, Any]]:
    """List all invites ordered by ``created_at DESC`` without exposing ``token_sha256``."""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    i.invite_id::text AS invite_id,
                    i.created_by_user_id,
                    i.created_at,
                    i.expires_at,
                    i.redeeming_username,
                    i.redeeming_at,
                    i.used_at,
                    i.used_by_email,
                    i.supabase_user_id,
                    i.revoked_at,
                    m.username AS used_by_username
                FROM dalton_invites i
                LEFT JOIN dalton_members m ON m.email = i.used_by_email
                ORDER BY i.created_at DESC
                """
            )
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_invite_by_id(
    invite_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch an invite by UUID without exposing ``token_sha256``."""
    try:
        normalized_id = str(uuid.UUID(str(invite_id).strip()))
    except (ValueError, TypeError):
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    invite_id::text AS invite_id,
                    created_by_user_id,
                    created_at,
                    expires_at,
                    redeeming_username,
                    redeeming_at,
                    used_at,
                    used_by_email,
                    supabase_user_id,
                    revoked_at
                FROM dalton_invites
                WHERE invite_id = %s
                """,
                (normalized_id,),
            )
            row = await cur.fetchone()
            return _normalize_row(row)


async def revoke_invite(
    invite_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Mark an unused invite as revoked (`revoked_at = COALESCE(revoked_at, NOW())`)."""
    try:
        normalized_id = str(uuid.UUID(str(invite_id).strip()))
    except (ValueError, TypeError):
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_invites
                SET revoked_at = COALESCE(revoked_at, NOW())
                WHERE invite_id = %s AND used_at IS NULL
                RETURNING
                    invite_id::text AS invite_id,
                    created_by_user_id,
                    created_at,
                    expires_at,
                    redeeming_username,
                    redeeming_at,
                    used_at,
                    used_by_email,
                    supabase_user_id,
                    revoked_at
                """,
                (normalized_id,),
            )
            row = await cur.fetchone()
            return _normalize_row(row)


async def get_invite_by_token_hash(
    token_sha256: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return the invite row matching ``token_sha256``."""
    digest = str(token_sha256 or "").strip().lower()
    if not digest:
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    invite_id::text AS invite_id,
                    token_sha256,
                    created_by_user_id,
                    created_at,
                    expires_at,
                    redeeming_username,
                    redeeming_at,
                    used_at,
                    used_by_email,
                    supabase_user_id,
                    revoked_at
                FROM dalton_invites
                WHERE token_sha256 = %s
                """,
                (digest,),
            )
            row = await cur.fetchone()
            return _normalize_row(row)


async def lock_invite_by_token_hash(
    token_sha256: str,
    *,
    conn: Any,
) -> dict[str, Any] | None:
    """Lock and return the invite row matching ``token_sha256`` via ``SELECT ... FOR UPDATE``."""
    digest = str(token_sha256 or "").strip().lower()
    if not digest:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                invite_id::text AS invite_id,
                token_sha256,
                created_by_user_id,
                created_at,
                expires_at,
                redeeming_username,
                redeeming_at,
                used_at,
                used_by_email,
                supabase_user_id,
                revoked_at
            FROM dalton_invites
            WHERE token_sha256 = %s
            FOR UPDATE
            """,
            (digest,),
        )
        row = await cur.fetchone()
        return _normalize_row(row)


async def mark_invite_redeeming(
    *,
    invite_id: str,
    redeeming_username: str,
    conn: Any,
) -> dict[str, Any] | None:
    """Mark a locked invite row as in-progress redeeming (`redeeming_username`, `redeeming_at`)."""
    normalized_id = str(uuid.UUID(str(invite_id).strip()))
    normalized_username = str(redeeming_username or "").strip().lower()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE dalton_invites
            SET
                redeeming_username = %s,
                redeeming_at = COALESCE(redeeming_at, NOW())
            WHERE invite_id = %s
              AND used_at IS NULL
              AND revoked_at IS NULL
              AND (redeeming_username IS NULL OR redeeming_username = %s)
            RETURNING
                invite_id::text AS invite_id,
                token_sha256,
                created_by_user_id,
                created_at,
                expires_at,
                redeeming_username,
                redeeming_at,
                used_at,
                used_by_email,
                supabase_user_id,
                revoked_at
            """,
            (normalized_username, normalized_id, normalized_username),
        )
        row = await cur.fetchone()
        return _normalize_row(row)


async def clear_invite_redeeming(
    *,
    invite_id: str,
    redeeming_username: str,
    conn: Any = None,
) -> None:
    """Reset `redeeming_username` and `redeeming_at` on an unused invite matching `redeeming_username`."""
    normalized_id = str(uuid.UUID(str(invite_id).strip()))
    normalized_username = str(redeeming_username or "").strip().lower()
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_invites
                SET redeeming_username = NULL, redeeming_at = NULL
                WHERE invite_id = %s AND used_at IS NULL AND redeeming_username = %s
                """,
                (normalized_id, normalized_username),
            )


async def mark_invite_used(
    *,
    invite_id: str,
    used_by_email: str,
    supabase_user_id: str | None = None,
    conn: Any,
) -> dict[str, Any] | None:
    """Mark a locked invite row as used (`used_at = NOW()`, `used_by_email`, `supabase_user_id`)."""
    normalized_id = str(uuid.UUID(str(invite_id).strip()))
    normalized_email = str(used_by_email or "").strip().lower()
    normalized_sb_uid = str(supabase_user_id).strip() if supabase_user_id else None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE dalton_invites
            SET
                used_at = NOW(),
                used_by_email = %s,
                supabase_user_id = COALESCE(%s, supabase_user_id)
            WHERE invite_id = %s AND used_at IS NULL AND revoked_at IS NULL
            RETURNING
                invite_id::text AS invite_id,
                created_by_user_id,
                created_at,
                expires_at,
                redeeming_username,
                redeeming_at,
                used_at,
                used_by_email,
                supabase_user_id,
                revoked_at
            """,
            (normalized_email, normalized_sb_uid, normalized_id),
        )
        row = await cur.fetchone()
        return _normalize_row(row)
