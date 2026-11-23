"""Team-shared model credential resolution and admin guard (`DALTON-HOOK(team-credentials)`).

- ``supabase`` auth mode (Everflow): when ``credentials.sharing == "team"`` and
  ``DALTON_PM_USER_IDS`` is non-empty, resolves to ``DALTON_PM_USER_IDS[0]`` and checks
  admin membership against ``DALTON_PM_USER_IDS`` (unchanged).
- ``local`` auth mode (standalone core): reads ``dalton_credential_holder`` and
  ``dalton_user_roles`` directly from PostgreSQL with NO in-process role caching.
  Database query failures raise HTTP ``503`` (fail-closed) rather than treating team
  sharing as disabled. Transactional credential writes use
  :func:`resolve_credential_write_holder` to lock and pin both the holder and operator
  admin role inside the write transaction.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.settings import (
    get_configured_pm_user_ids as _settings_configured_pm_user_ids,
    get_dalton_config,
    is_dalton_local_auth_enabled,
    is_dalton_supabase_auth_enabled,
)

logger = logging.getLogger(__name__)

TEAM_CREDENTIAL_DISCONNECT_FORBIDDEN_DETAIL = "团队共用凭据只能由管理员断开"
TEAM_CREDENTIAL_WRITE_FORBIDDEN_DETAIL = "团队共用凭据只能由管理员设置"
TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL = (
    "Database unavailable while verifying team credential permissions"
)


def invalidate_local_credential_cache() -> None:
    """No-op hook retained for callers; local mode reads PostgreSQL directly without caching."""
    return None


def _fetch_local_holder_id_sync() -> str | None:
    """Read singleton ``dalton_credential_holder.holder_user_id`` directly from PostgreSQL.

    Returns ``None`` ONLY when the table genuinely has no row; raises HTTP 503 on DB failure.
    """
    try:
        import psycopg

        from src.server.database.pool import get_db_connection_string

        with psycopg.connect(get_db_connection_string(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT holder_user_id FROM dalton_credential_holder WHERE singleton_id = 1"
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0]).strip() or None
        return None
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to read dalton_credential_holder (fail-closed 503): %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail=TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL
        ) from exc


def _fetch_local_is_admin_sync(user_id: str) -> bool:
    """Check whether ``user_id`` has ``tier = 'admin'`` directly in ``dalton_user_roles``.

    Raises HTTP 503 on database failure (fail-closed, never falls back to cached roles).
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        import psycopg

        from src.server.database.pool import get_db_connection_string

        with psycopg.connect(get_db_connection_string(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier FROM dalton_user_roles WHERE user_id = %s",
                    (uid,),
                )
                row = cur.fetchone()
        return bool(row and str(row[0] or "").strip().lower() == "admin")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to verify local admin role in dalton_user_roles (fail-closed 503): %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail=TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL
        ) from exc


async def get_team_credential_holder_id_async(*, conn: Any = None) -> str | None:
    """Async lookup for the team credential holder; raises HTTP 503 on DB error in local mode."""
    cfg = get_dalton_config()
    if not cfg.enabled or cfg.credentials.sharing != "team":
        return None
    if is_dalton_local_auth_enabled():
        from src.dalton.auth.roles import get_credential_holder_id

        try:
            return await get_credential_holder_id(conn=conn)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Async read of dalton_credential_holder failed (fail-closed 503): %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail=TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL
            ) from exc
    if not is_dalton_supabase_auth_enabled():
        return None
    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        return None
    return pm_ids[0]


@asynccontextmanager
async def credential_write_tx(db: Any, *, external_conn: Any = None) -> AsyncIterator[Any]:
    """Ensure standalone credential writes run inside a DB transaction in ``local`` mode.

    When a caller passes an open transaction (``external_conn is not None``) or when
    running outside ``local`` mode, yields ``db`` untouched so callers and existing
    Supabase/OSS paths preserve their exact transaction boundaries.
    When ``external_conn is None`` in ``local`` mode, opens ``db.transaction()`` so
    :func:`resolve_credential_write_holder`'s ``FOR SHARE`` locks on
    ``dalton_credential_holder`` and ``dalton_user_roles`` remain held until the write
    statement commits.
    """
    if external_conn is None and is_dalton_local_auth_enabled():
        async with db.transaction():
            yield db
        return
    yield db


async def resolve_credential_write_holder(
    actor_user_id: str,
    *,
    conn: Any,
    for_disconnect: bool = False,
) -> str:
    """Pin team credential holder AND authorize ``actor_user_id`` inside the write transaction.

    - Outside ``local`` mode (including ``supabase`` and ``oss``): preserves existing
      ``credential_user_id(actor_user_id)`` without issuing extra SQL statements.
    - In ``local`` mode when team sharing is configured: locks ``dalton_credential_holder``
      and ``dalton_user_roles`` with ``FOR SHARE`` inside ``conn``, raises ``403`` if the
      actor is not an ``admin``, raises ``503`` on any DB failure, and returns the pinned
      ``holder_user_id`` for the write statement to target.
    """
    if not actor_user_id:
        return actor_user_id
    if not is_dalton_local_auth_enabled():
        return credential_user_id(actor_user_id)

    cfg = get_dalton_config()
    if not cfg.enabled or cfg.credentials.sharing != "team":
        return actor_user_id

    uid = str(actor_user_id).strip()
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT holder_user_id FROM dalton_credential_holder "
                "WHERE singleton_id = 1 FOR SHARE"
            )
            holder_row = await cur.fetchone()
            if not holder_row or not holder_row.get("holder_user_id"):
                return actor_user_id
            holder_id = str(holder_row["holder_user_id"]).strip()

            await cur.execute(
                "SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE",
                (uid,),
            )
            role_row = await cur.fetchone()
            tier = str((role_row or {}).get("tier") or "").strip().lower()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Transactional team credential authorization failed (fail-closed 503): %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail=TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL
        ) from exc

    if tier != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                TEAM_CREDENTIAL_DISCONNECT_FORBIDDEN_DETAIL
                if for_disconnect
                else TEAM_CREDENTIAL_WRITE_FORBIDDEN_DETAIL
            ),
        )
    return holder_id or actor_user_id


def get_configured_pm_user_ids() -> list[str]:
    """Return explicitly configured PM user IDs from `DALTON_PM_USER_IDS` (empty when unset or blank)."""
    return [
        str(u).strip()
        for u in (_settings_configured_pm_user_ids() or [])
        if str(u).strip()
    ]


def get_team_credential_holder_id() -> str | None:
    """Return the single team credential holder user_id when sharing is active, else None.

    - ``local`` auth mode: reads ``dalton_credential_holder`` directly from PostgreSQL
      (raises HTTP 503 on DB error; never falls back to None on failure).
    - ``supabase`` auth mode: preserves ``DALTON_PM_USER_IDS[0]`` unchanged.
    """
    cfg = get_dalton_config()
    if not cfg.enabled or cfg.credentials.sharing != "team":
        return None
    if is_dalton_local_auth_enabled():
        return _fetch_local_holder_id_sync()
    if not is_dalton_supabase_auth_enabled():
        return None
    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        return None
    return pm_ids[0]


def is_team_credential_sharing_enabled() -> bool:
    """Return True when team credential sharing is active."""
    return get_team_credential_holder_id() is not None


def credential_user_id(user_id: str) -> str:
    """Resolve `user_id` to the team credential holder when sharing is enabled.

    Returns `user_id` unchanged when sharing is off, when `user_id` is empty,
    or when `user_id` is already the holder.
    """
    if not user_id:
        return user_id
    holder_id = get_team_credential_holder_id()
    if not holder_id or user_id == holder_id:
        return user_id
    return holder_id


def is_credential_admin(user_id: str) -> bool:
    """Return True if `user_id` may disconnect or write team-shared credentials.

    In ``local`` mode, queries live PostgreSQL state without in-process caching and
    raises HTTP 503 on DB errors (fail-closed).
    """
    if not is_team_credential_sharing_enabled():
        return True
    if not user_id:
        return False
    if is_dalton_local_auth_enabled():
        return _fetch_local_is_admin_sync(user_id)
    return str(user_id).strip() in set(get_configured_pm_user_ids())


def require_credential_admin(user_id: str) -> None:
    """Raise HTTP 403 if team credential sharing is active and `user_id` is not an admin."""
    if not is_credential_admin(user_id):
        raise HTTPException(
            status_code=403,
            detail=TEAM_CREDENTIAL_DISCONNECT_FORBIDDEN_DETAIL,
        )


def require_credential_write_admin(user_id: str) -> None:
    """Raise HTTP 403 if `user_id` may not write the shared credentials."""
    if not is_credential_admin(user_id):
        raise HTTPException(
            status_code=403,
            detail=TEAM_CREDENTIAL_WRITE_FORBIDDEN_DETAIL,
        )
