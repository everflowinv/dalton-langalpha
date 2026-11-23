"""Database operations for ``dalton_user_roles`` and ``dalton_credential_holder``.

Implements last-admin protection (refusing demotion or deletion when only 1 active
admin remains) under transactional row locks.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

RoleTier = Literal["admin", "internal", "external"]
VALID_ROLE_TIERS: frozenset[str] = frozenset({"admin", "internal", "external"})

LAST_ADMIN_PROTECTED_DETAIL = (
    "Cannot demote or remove the last remaining administrator"
)
CREDENTIAL_HOLDER_PROTECTED_DETAIL = (
    "Cannot demote or remove the current team credential holder; transfer credential holder first"
)


class LastAdminProtectedError(HTTPException):
    """Raised (HTTP 409) when a write would leave zero active administrators."""

    def __init__(self, detail: str = LAST_ADMIN_PROTECTED_DETAIL) -> None:
        super().__init__(status_code=409, detail=detail)


class CredentialHolderProtectedError(HTTPException):
    """Raised (HTTP 409) when attempting to demote or remove the active credential holder."""

    def __init__(self, detail: str = CREDENTIAL_HOLDER_PROTECTED_DETAIL) -> None:
        super().__init__(status_code=409, detail=detail)


def _invalidate_credentials_cache() -> None:
    try:
        from src.dalton.credentials import invalidate_local_credential_cache

        invalidate_local_credential_cache()
    except Exception:
        pass


async def get_user_role(user_id: str, *, conn: Any = None) -> RoleTier | None:
    """Return the ``tier`` for ``user_id`` from ``dalton_user_roles``, or ``None``."""
    uid = str(user_id or "").strip()
    if not uid:
        return None
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT tier FROM dalton_user_roles WHERE user_id = %s",
                (uid,),
            )
            row = await cur.fetchone()
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT tier FROM dalton_user_roles WHERE user_id = %s",
                    (uid,),
                )
                row = await cur.fetchone()
    if not row:
        return None
    tier = str(row.get("tier") or "").strip().lower()
    if tier in VALID_ROLE_TIERS:
        return tier  # type: ignore[return-value]
    return None


async def list_admin_user_ids(*, conn: Any = None) -> list[str]:
    """Return all ``user_id`` rows with ``tier = 'admin'`` ordered by ``created_at``."""
    sql = (
        "SELECT user_id FROM dalton_user_roles "
        "WHERE tier = 'admin' ORDER BY created_at ASC, user_id ASC"
    )
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
    return [str(r["user_id"]).strip() for r in rows if r and r.get("user_id")]


async def set_user_role(
    user_id: str,
    tier: RoleTier,
    *,
    granted_by: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert or update a user's role in ``dalton_user_roles``.

    Enforces last-admin protection: if ``user_id`` is currently the sole ``admin``
    and ``tier != 'admin'``, raises :class:`LastAdminProtectedError` (HTTP 409).
    """
    uid = str(user_id or "").strip()
    norm_tier = str(tier or "").strip().lower()
    if not uid:
        raise ValueError("user_id is required")
    if norm_tier not in VALID_ROLE_TIERS:
        raise ValueError(f"Invalid role tier: {tier!r}")

    async def _run(db_conn: Any) -> dict[str, Any]:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if norm_tier != "admin":
                # Lock all current admin rows to serialize concurrent demotions.
                await cur.execute(
                    "SELECT user_id FROM dalton_user_roles "
                    "WHERE tier = 'admin' ORDER BY user_id ASC FOR UPDATE"
                )
                admin_rows = await cur.fetchall()
                admin_ids = {str(r["user_id"]).strip() for r in admin_rows}
                if uid in admin_ids and len(admin_ids) <= 1:
                    raise LastAdminProtectedError()
                await cur.execute(
                    "SELECT holder_user_id FROM dalton_credential_holder "
                    "WHERE singleton_id = 1 FOR SHARE"
                )
                holder_row = await cur.fetchone()
                if holder_row and str(holder_row.get("holder_user_id") or "").strip() == uid:
                    raise CredentialHolderProtectedError()

            await cur.execute(
                """
                INSERT INTO dalton_user_roles (user_id, tier, granted_by, created_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET tier = EXCLUDED.tier,
                    granted_by = EXCLUDED.granted_by,
                    updated_at = NOW()
                RETURNING user_id, tier, granted_by, created_at, updated_at
                """,
                (uid, norm_tier, granted_by),
            )
            row = await cur.fetchone()
            return dict(row) if row else {"user_id": uid, "tier": norm_tier, "granted_by": granted_by}

    if conn is not None:
        result = await _run(conn)
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.transaction():
                result = await _run(db_conn)
    _invalidate_credentials_cache()
    return result


async def delete_user_role(user_id: str, *, conn: Any = None) -> bool:
    """Delete a user's row from ``dalton_user_roles``.

    Enforces last-admin protection: if ``user_id`` is currently the sole ``admin``,
    raises :class:`LastAdminProtectedError` (HTTP 409). Returns True if a row was deleted.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False

    async def _run(db_conn: Any) -> bool:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT user_id FROM dalton_user_roles "
                "WHERE tier = 'admin' ORDER BY user_id ASC FOR UPDATE"
            )
            admin_rows = await cur.fetchall()
            admin_ids = {str(r["user_id"]).strip() for r in admin_rows}
            if uid in admin_ids and len(admin_ids) <= 1:
                raise LastAdminProtectedError()
            await cur.execute(
                "SELECT holder_user_id FROM dalton_credential_holder "
                "WHERE singleton_id = 1 FOR SHARE"
            )
            holder_row = await cur.fetchone()
            if holder_row and str(holder_row.get("holder_user_id") or "").strip() == uid:
                raise CredentialHolderProtectedError()

            await cur.execute(
                "DELETE FROM dalton_user_roles WHERE user_id = %s RETURNING user_id",
                (uid,),
            )
            deleted = await cur.fetchone()
            return deleted is not None

    if conn is not None:
        removed = await _run(conn)
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.transaction():
                removed = await _run(db_conn)
    if removed:
        _invalidate_credentials_cache()
    return removed


async def get_credential_holder_id(*, conn: Any = None) -> str | None:
    """Return the singleton ``holder_user_id`` from ``dalton_credential_holder``."""
    sql = "SELECT holder_user_id FROM dalton_credential_holder WHERE singleton_id = 1"
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                row = await cur.fetchone()
    if not row or not row.get("holder_user_id"):
        return None
    return str(row["holder_user_id"]).strip() or None


async def set_credential_holder_id(
    holder_user_id: str,
    *,
    updated_by: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Upsert the singleton team credential holder in ``dalton_credential_holder``."""
    uid = str(holder_user_id or "").strip()
    if not uid:
        raise ValueError("holder_user_id is required")

    async def _run(db_conn: Any) -> dict[str, Any]:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_credential_holder (singleton_id, holder_user_id, updated_by, updated_at)
                VALUES (1, %s, %s, NOW())
                ON CONFLICT (singleton_id) DO UPDATE
                SET holder_user_id = EXCLUDED.holder_user_id,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                RETURNING singleton_id, holder_user_id, updated_by, updated_at
                """,
                (uid, updated_by),
            )
            row = await cur.fetchone()
            return dict(row) if row else {"singleton_id": 1, "holder_user_id": uid, "updated_by": updated_by}

    if conn is not None:
        result = await _run(conn)
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.transaction():
                result = await _run(db_conn)
    _invalidate_credentials_cache()
    return result
